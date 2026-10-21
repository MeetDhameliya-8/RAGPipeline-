"""Use existing Azure/JSON generation with isolated, immutable source-unit attempts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from .evidence import text_hash, get_evidence
from .providers.base import ChunkGenerationError
from .source_packages import materialize_package, validate_source_package_plan
from .chunk_validator import validate_chunks, eligible_chunk_numbers
from . import diagnostics as telemetry
from .bounded_scheduler import SubmissionControl, run_bounded as _run_source_schedule


def validation_fingerprint() -> str:
    """Invalidate held validation outcomes when implementation rules change."""
    root = Path(__file__).parent
    return text_hash(json.dumps({str(p.relative_to(root)):text_hash(p.read_text(encoding='utf-8'))
        for p in sorted(root.rglob('*.py'))},sort_keys=True))


def generate_source_packages(extraction, plan, source_doc, prompt_path, provider, audit, attempt_directory, progress_callback=None, *, max_concurrency: int = 1):
    from .chunk_generator import (_generate_single_batch, _provider_from_environment,
        load_generation_prompt, validate_generation_schema, validate_generation_content, CONTENT_FIELDS)
    if max_concurrency not in (1, 2):
        raise ValueError("Package concurrency must be one or two.")
    from .chunk_generator import DEFAULT_PROMPT_PATH
    if Path(prompt_path) == DEFAULT_PROMPT_PATH:
        prompt_path = DEFAULT_PROMPT_PATH.with_name("gastroagi_source_package_rules.md")
    packages = plan.source_package_plan
    evidence = source_doc.evidence
    incoming = get_evidence(extraction)
    if incoming.pdf_sha256 != packages.pdf_sha256 or incoming.canonical_buffer_id != evidence.canonical_buffer_id:
        raise ChunkGenerationError("Source package generation identity mismatch.",code="EXTRACTION_BLOCKED")
    validate_source_package_plan(packages,evidence)
    if (source_doc.source_package_plan != packages
            or [b.unit_id for b in plan.batches] != [p.unit_id for p in packages.packages]
            or any(batch.ownership != package.ownership for package, batch in zip(packages.packages, plan.batches))):
        raise ChunkGenerationError("Source package schedule mismatch.",code="EXTRACTION_BLOCKED")
    if not packages.packages:
        raise ChunkGenerationError("No sufficiently supported coherent native prose; targeted extraction/source review is required.",code="EXTRACTION_BLOCKED")
    from .generation_preflight import require_source_reference
    require_source_reference(extraction)
    selected = provider or _provider_from_environment()
    instruction = ("This request is one bounded, independently witnessed NATIVE SOURCE EVIDENCE PACKAGE, not an entire article. "
        "Produce one coherent clinical chunk for 90-300 source words, or up to three distinct chunks for longer evidence. "
        "Each chunk must contain 90-250 words when the source supports it. Do not split a short source into undersized sentence chunks, "
        "pad, add clinical facts, infer missing arms/endpoints, or use medical memory. Preserve source qualifiers, "
        "population, treatment, comparator, units, signs, endpoints and timeframes exactly. "
        "Do not report a table or figure result unless it is explicitly stated in the supplied prose. "
        "Select complete, independently understandable source passages, retaining exact wording and qualifications. "
        "This is a draft for mandatory clinical review, with incomplete document coverage. "
        "Use the original five clinical fields and original verified reference authority. Return strict JSON only.")
    config = {key:getattr(selected,key,None) for key in ("deployment","endpoint","api_version")}
    config = {k:v if isinstance(v,str) else type(selected).__name__ for k,v in config.items()}
    common = text_hash(json.dumps({"prompt":load_generation_prompt(prompt_path),"instruction":instruction,"provider":config,
        "pdf":packages.pdf_sha256,"source_file":extraction["file_name"],"evidence_config":packages.evidence_config_hash,
        "reference":extraction.get("reference_metadata")},sort_keys=True))
    directory = Path(attempt_directory) if attempt_directory else None
    if directory:
        directory.mkdir(parents=True,exist_ok=True)
    merged, seen = None, {}
    audit.update(source_package_plan_id=packages.plan_id,document_source_complete=False,chunk_ownership=[],
                 unit_failures=[],validation_rejections=[],units_resumed=0,source_holds=len(packages.holds))
    audit.update(prompt_path=str(prompt_path), package_concurrency=max_concurrency,
                 package_prompt_version="source-package-v2")
    validation_version = validation_fingerprint()
    observation = telemetry.current()
    control = SubmissionControl()
    schedule = list(zip(packages.packages, plan.batches))
    worker_audits = [{"units": [], "requests_started": 0, "completed_units": 0,
        "generated_candidates": 0, "structurally_valid_chunks": 0,
        "characters_sent_to_model": 0, "system_prompt_characters_sent": 0,
        "units_resumed": 0, "unit_failures": [], "validation_rejections": []} for _ in schedule]

    def attempt(index):
        with telemetry.bind(observation):
            try:
                return observed_attempt(*schedule[index], worker_audits[index])
            except BaseException:
                telemetry.incomplete_candidates()
                control.stop()
                raise

    def observed_attempt(package, batch, unit_audit):
        # Each worker owns its counters and candidate files. Evidence is read-only.
        if batch.ownership != package.ownership:
            raise ChunkGenerationError("Source package ownership was changed.",code="EXTRACTION_BLOCKED")
        text = materialize_package(package,evidence)
        package_instruction = instruction
        if package.visual_source_ids:
            package_instruction += (" This packet is a VISUAL TRANSCRIPTION DRAFT, not a native clinical certificate. "
                "Prefer source-grounded qualitative prose. Do not introduce quantitative findings or recommendations. "
                "Do not omit numerical qualifications if omission changes the clinical scope; choose another source passage instead.")
        provenance = {"unit_id":package.unit_id,"pdf_sha256":packages.pdf_sha256,
            "ownership":[s.model_dump() for s in package.ownership],"page_numbers":package.page_numbers,
            "draft_evidence":True,"section_hint":package.section_hint,"visual_source_ids":package.visual_source_ids}
        bounded = dict(extraction,full_text=text,generation_unit_provenance=provenance)
        fingerprint = text_hash(common+package.unit_id+text_hash(text)+(text_hash(package_instruction) if package.visual_source_ids else ""))
        candidate_path = directory/(fingerprint+".json") if directory else None
        recovered_path = directory/(fingerprint+"-recovered-v1.json") if directory else None
        hold_path = directory/(fingerprint+"-hold-v1.json") if directory else None
        validation_hold_path = directory/(fingerprint+"-validation-hold-v1.json") if directory else None
        validation_hold = {"unit_id":package.unit_id,"code":"SOURCE_VALIDATION_FAILED","fingerprint":fingerprint,
                           "retryable":False,"validation_fingerprint":validation_version}
        if validation_hold_path and validation_hold_path.exists():
            try:
                if json.loads(validation_hold_path.read_text(encoding='utf-8')) == validation_hold:
                    telemetry.count("cached_validation_hold_units")
                    unit_audit['unit_failures'].append(validation_hold)
                    unit_audit['units'].append({'source_provenance':provenance,'outcome':'resumed_validation_hold'})
                    return None, unit_audit
            except (ValueError,OSError):
                pass
        hold = {"unit_id":package.unit_id,"code":"SOURCE_PASSAGE_HELD","fingerprint":fingerprint,"retryable":False}
        if hold_path and hold_path.exists():
            try:
                if json.loads(hold_path.read_text(encoding='utf-8')) == hold:
                    telemetry.count("cached_source_hold_units")
                    unit_audit['unit_failures'].append(hold)
                    unit_audit['units'].append({'source_provenance':provenance,'outcome':'resumed_source_hold'})
                    return None, unit_audit
            except (ValueError,OSError):
                pass
        if recovered_path and recovered_path.exists():
            candidate_path = recovered_path
        result = None
        if candidate_path and candidate_path.exists():
            try:
                saved = json.loads(candidate_path.read_text(encoding="utf-8"))
                if saved["fingerprint"] != fingerprint:
                    raise ValueError("Attempt identity mismatch.")
                result = saved["candidate"]
                validate_generation_schema(result);validate_generation_content(result,bounded)
                if result["article"]["source_file"] != extraction["file_name"]:
                    raise ValueError("Candidate source filename mismatch.")
                unit_audit["units_resumed"] += 1
                telemetry.count("resumed_candidate_units")
                telemetry.count("resumed_candidates", len(result["chunks"]))
                unit_audit["generated_candidates"] += len(result["chunks"])
                unit_audit["structurally_valid_chunks"] += len(result["chunks"])
                unit_audit["completed_units"] += 1
                unit_audit["units"].append({"source_provenance":provenance,"outcome":"resumed_compatible_candidate","input_fingerprint":fingerprint})
            except (ValueError,KeyError,ChunkGenerationError):
                result = None
        if result is None:
            try:
                result = _generate_single_batch(bounded,prompt_path,selected,package_instruction,unit_audit,
                                                allow_source_abstention=True)
            except ChunkGenerationError as exc:
                if exc.code in {"AUTHENTICATION_FAILED", "QUOTA_EXHAUSTED"}:
                    control.stop(exc.code)
                failure = {"unit_id":package.unit_id,"code":exc.code,"fingerprint":fingerprint,
                           "retryable":(exc.diagnostics.get("error") or {}).get("retryable")}
                failure["failure_classification"] = telemetry.failure(exc, exc.code)
                unit_audit["unit_failures"].append(failure)
                if exc.code == 'SOURCE_PASSAGE_HELD' and hold_path:
                    from .preparation_cache import _atomic_write
                    _atomic_write(hold_path,json.dumps(hold).encode())
                if directory:
                    (directory/(fingerprint[:24]+"-failed-"+uuid4().hex[:16]+".json")).write_text(json.dumps(failure),encoding="utf-8")
                return None, unit_audit
            if directory:
                attempt = directory/(fingerprint[:24]+"-attempt-"+uuid4().hex[:16]+".json")
                attempt.write_text(json.dumps({"fingerprint":fingerprint,"candidate":result,"provenance":provenance}),encoding="utf-8")
                try:
                    os.link(attempt,candidate_path)  # exclusive complete candidate publication
                except FileExistsError:
                    pass
        @telemetry.timed("local_chunk_validation")
        def assess(candidate):
            packet={"text":text,"review_required":True}
            if package.visual_source_ids:
                packet.update(visual_transcription=True,native_numeric_source="\n\n".join(evidence.buffers[p.selected_buffer_id].text
                    for p in evidence.pages if p.page_number in package.page_numbers))
            assessment = validate_chunks(candidate,chunk_sources={i:[packet]
                for i in range(1,len(candidate["chunks"])+1)})
            accepted = len(eligible_chunk_numbers(assessment, {"PASS", "REVIEW_REQUIRED"}))
            telemetry.count("assessed_candidate_events", len(candidate["chunks"]))
            telemetry.count("accepted_candidate_events", accepted)
            telemetry.count("rejected_candidate_events", len(candidate["chunks"]) - accepted)
            return assessment
        assessment = assess(result)
        allowed = eligible_chunk_numbers(assessment,{"PASS","REVIEW_REQUIRED"})
        if assessment["status"] == "FAILED":
            rules = sorted({i["rule"] for i in assessment["issues"] if i["severity"] == "ERROR"})
            unit_audit["validation_rejections"].append({"unit_id":package.unit_id,"rules":rules,
                "rejected_candidates":len(result["chunks"])-len(allowed)})
            if directory:
                (directory/(fingerprint[:24]+"-validation-"+uuid4().hex[:16]+".json")).write_text(
                    json.dumps({"candidate":result,"validation":assessment,"provenance":provenance}),encoding="utf-8")
            if not allowed:
                with control.gate:
                    if control.stopped.is_set():
                        # A correction is a new request commitment. Retain the
                        # failed assessment/candidate but do not create a
                        # permanent clinical hold for an unattempted correction.
                        telemetry.count("corrections_deferred_after_stop")
                        unit_audit["unit_failures"].append({"unit_id":package.unit_id,
                            "code":control.fatal_code or "GENERATION_CANCELLED",
                            "phase":"correction_deferred_after_stop", "provider_call_started":False})
                        return None, unit_audit
                correction = (package_instruction+"\nYour previous candidate failed these actual checks: "+", ".join(rules)+
                    ". Regenerate ONLY from the supplied source. For Original Text, select a coherent contiguous source passage "
                    "of at least 90 words, retaining its exact words, negation and qualifications (whitespace may be joined). "
                    "Do not add recommendations, advice, conclusions, numerics or explanatory facts. "
                    "Keep headings/classification specific; the Original Text must remain source-faithful.")
                try:
                    with telemetry.correcting():
                        result = _generate_single_batch(bounded,prompt_path,selected,correction,unit_audit,
                                                        allow_source_abstention=True)
                    assessment = assess(result)
                    allowed = eligible_chunk_numbers(assessment,{"PASS","REVIEW_REQUIRED"})
                    if directory:
                        repaired = directory/(fingerprint[:24]+"-repair-"+uuid4().hex[:16]+".json")
                        repaired.write_text(json.dumps({"fingerprint":fingerprint,"candidate":result,
                            "validation":assessment,"provenance":provenance}),encoding="utf-8")
                except ChunkGenerationError as exc:
                    if exc.code in {"AUTHENTICATION_FAILED", "QUOTA_EXHAUSTED"}:
                        control.stop(exc.code)
                    unit_audit["unit_failures"].append({"unit_id":package.unit_id,"code":exc.code,"phase":"bounded_regeneration",
                        "failure_classification":telemetry.failure(exc, exc.code),
                        "retryable":(exc.diagnostics.get("error") or {}).get("retryable")})
                    if exc.code == 'SOURCE_PASSAGE_HELD' and hold_path:
                        from .preparation_cache import _atomic_write
                        _atomic_write(hold_path,json.dumps(hold).encode())
                    return None, unit_audit
            if not allowed:
                telemetry.count("source_validation_held_units")
                unit_audit["unit_failures"].append({"unit_id":package.unit_id,"code":"SOURCE_VALIDATION_FAILED"})
                if validation_hold_path:
                    from .preparation_cache import _atomic_write
                    _atomic_write(validation_hold_path,json.dumps(validation_hold).encode())
                return None, unit_audit
            result = {"article":dict(result["article"]),"chunks":[dict(c,chunk_number=i) for i,c in enumerate(
                (c for i,c in enumerate(result["chunks"],1) if i in allowed),1)]}
            result["article"]["number_of_chunks"]=len(result["chunks"])
            if directory and recovered_path and not recovered_path.exists():
                saved = directory/(fingerprint[:24]+"-accepted-"+uuid4().hex[:16]+".json")
                saved.write_text(json.dumps({"fingerprint":fingerprint,"candidate":result,"provenance":provenance}),encoding="utf-8")
                try:
                    os.link(saved,recovered_path)
                except FileExistsError:
                    pass
        return result, unit_audit

    def merge_audit(part):
        for name in ("requests_started", "completed_units", "generated_candidates",
                     "structurally_valid_chunks", "characters_sent_to_model",
                     "system_prompt_characters_sent", "units_resumed"):
            audit[name] = audit.get(name, 0) + part.get(name, 0)
        for unit in part["units"]:
            unit["unit_number"] = len(audit["units"]) + 1
            audit["units"].append(unit)
        audit["unit_failures"].extend(part["unit_failures"])
        audit["validation_rejections"].extend(part["validation_rejections"])

    def progress(completed):
        if progress_callback is not None:
            progress_callback("GENERATING_CHUNKS", {
                "message":f"Drafting source packages: {completed} of {len(schedule)} visited.",
                "source_packages_total":len(schedule), "source_packages_completed":completed})

    run = _run_source_schedule(attempt, len(schedule), max_concurrency, control, progress)
    audit["scheduler"] = {"mode":"rolling", "max_in_flight":run.max_in_flight,
        "submitted_units":len(run.outcomes), "completed_units":sum(not o.cancelled for o in run.outcomes),
        "cancelled_units":sum(o.cancelled for o in run.outcomes),
        "unsubmitted_units":len(schedule)-len(run.outcomes), "submission_stopped":control.stopped.is_set(),
        "stopped_early":len(run.outcomes)<len(schedule) or any(o.cancelled for o in run.outcomes)}
    errors = []
    for outcome in run.outcomes:
        package, batch = schedule[outcome.index]
        merge_audit(worker_audits[outcome.index])
        if outcome.error is not None:
            errors.append(outcome.error)
            audit.setdefault("scheduler_errors", []).append({"source_index":outcome.index,
                "cancelled":outcome.cancelled,
                "failure_classification":telemetry.failure(outcome.error, "UNCLASSIFIED_ERROR")})
            continue
        result, _ = outcome.value
        if result is None:
            continue
        if merged is None:
            merged = {"article":dict(result["article"]),"chunks":[]}
        for chunk in result["chunks"]:
            key = tuple(" ".join(chunk[field].split()) for field in CONTENT_FIELDS)
            if key in seen:
                telemetry.count("duplicate_candidates")
                origin = audit["chunk_ownership"][seen[key]-1]
                if package.unit_id not in origin["unit_ids"]:
                    origin["unit_ids"].append(package.unit_id)
                continue
            number = len(merged["chunks"])+1;seen[key]=number
            merged["chunks"].append(dict(chunk,chunk_number=number))
            audit["chunk_ownership"].append({"chunk_number":number,"unit_ids":[package.unit_id],
                "pdf_sha256":packages.pdf_sha256,"page_numbers":package.page_numbers,"ownership":[s.model_dump() for s in package.ownership]})
    if run.interruption is not None:
        errors.insert(0, run.interruption)
        audit.setdefault("scheduler_errors", []).append({"source_index":None,"cancelled":True,
            "failure_classification":telemetry.failure(run.interruption, "UNCLASSIFIED_ERROR")})
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Source package work was interrupted; all submitted outcomes were accounted for.", errors)
    if merged is None:
        raise ChunkGenerationError("Every attempted source package failed; source evidence and immutable failure outcomes were retained.",
            code=audit["unit_failures"][0]["code"] if audit["unit_failures"] else "GENERATION_FAILED",diagnostics=audit)
    merged["article"]["number_of_chunks"]=len(merged["chunks"])
    telemetry.count("merged_candidates", len(merged["chunks"]), replace=True)
    validate_generation_schema(merged);validate_generation_content(merged,extraction)
    audit["candidate_count_complete"] = not audit["unit_failures"]
    return merged
