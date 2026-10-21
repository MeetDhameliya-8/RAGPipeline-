"""Opt-in editorial prototype. No files, checkpoints, storage or approval writes."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from . import diagnostics as telemetry
from .claim_assessment import assess_editorial_chunk, claim_totals
from .document_concepts import DocumentConceptPlan, validate_document_concept_plan
from .evidence import text_hash
from .providers.base import ChunkGenerationError, ChunkGeneratorProvider

EDITORIAL_PROMPT = Path(__file__).resolve().parents[1]/"prompts"/"gastroagi_editorial_rules.md"
VERSION = "editorial-v1"


class EditorialDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    generated: dict[str, Any]
    validation: dict[str, Any]


@telemetry.observed_run
@telemetry.timed("editorial_generation")
def generate_editorial_draft(extraction: dict, source_doc: Any, concept_plan: DocumentConceptPlan,
                             provider: ChunkGeneratorProvider, diagnostics: dict,
                             *, max_requests: int = 80, max_source_characters: int = 20000) -> EditorialDraft:
    """Write one chunk per explicitly bound question using an injected provider.

    The provider never supplies trusted evidence mappings or its own validation.
    Unresolved/held M3 concepts are inventoried, not silently treated as covered.
    This prototype intentionally has no checkpoint/retry/correction adapter.
    """
    from .chunk_generator import (_generate_single_batch, validate_generation_schema,
        validate_generation_content, load_generation_prompt)
    from .chunk_validator import validate_chunks, _validation_report, _issue
    from .generation_preflight import require_source_reference
    if source_doc is None or provider is None:
        raise ChunkGenerationError("Editorial mode requires explicit source evidence and an injected provider.", code="EXTRACTION_BLOCKED")
    if type(max_requests) is not int or max_requests < 1 or type(max_source_characters) is not int or max_source_characters < 200:
        raise ValueError("Invalid editorial request budget.")
    # Pin the source and review records, never a mutable object-ID proof.
    source = source_doc.model_copy(deep=True)
    plan = DocumentConceptPlan.model_validate(concept_plan.model_dump())
    try:
        validate_document_concept_plan(plan, source)
        if extraction.get("full_text") != source.evidence.buffers[plan.canonical_buffer_id].text:
            raise ValueError("Editorial extraction mismatch.")
        reference = require_source_reference(extraction)
    except (ValueError, KeyError, TypeError):
        raise ChunkGenerationError("Editorial source identity or planning evidence is invalid.", code="EXTRACTION_BLOCKED") from None
    questions = [q for q in plan.concepts if q.status == "source_bound" and q.relationship_basis == "human_source_review"]
    if not questions:
        raise ChunkGenerationError("No source-bound reviewed clinical question is available for editorial drafting.", code="EXTRACTION_BLOCKED")
    if len(questions) > max_requests:
        raise ChunkGenerationError("Editorial question count exceeds the explicit request budget.", code="REQUEST_BUDGET_EXCEEDED")
    regions = {r.region_id:r for r in plan.regions}
    packets = []
    for q in questions:
        owned = [regions[x].ownership for x in q.owned_region_ids]
        context = [regions[x].ownership for x in q.context_region_ids]
        evidence_text = "\n\n".join(source.evidence.buffers[s.buffer_id].text[s.char_start:s.char_end] for s in owned+context)
        if len(evidence_text) > max_source_characters:
            raise ChunkGenerationError("Required editorial evidence exceeds the explicit context budget.", code="REQUEST_BUDGET_EXCEEDED")
        packets.append((q,owned,context,evidence_text))
    prompt = load_generation_prompt(EDITORIAL_PROMPT)
    audit = diagnostics
    audit.update(planned_requests=len(questions), requests_started=0, completed_units=0,
        generated_candidates=0, structurally_valid_chunks=0, units=[], candidate_count_complete=False)
    audit["editorial"] = {
        "version": VERSION, "prompt_sha256": text_hash(prompt), "plan_id":plan.plan_id,
        "evidence_fingerprint":plan.evidence_fingerprint,
        "unscheduled_concepts":[{"concept_id":q.concept_id,"status":q.status} for q in plan.concepts if q not in questions],
        "document_complete":False, "clinically_approved":False,
    }
    telemetry.count("planned_source_units",len(questions),replace=True)
    generated = None
    owners, packets_by_chunk, assessments, label_findings = [], {}, [], []
    for q,owned,context,evidence_text in packets:
        # The full exact evidence remains here/in the source model. Only span
        # references are exported to diagnostics. Never send unrelated PDF text.
        blocks = [f"CLINICAL QUESTION CATEGORY: {q.question}"]
        for label,spans in (("OWNED EVIDENCE",owned),("SUPPORTING CONTEXT",context)):
            for item in spans:
                blocks.append(f"[{label}]\n"+source.evidence.buffers[item.buffer_id].text[item.char_start:item.char_end])
        bounded = dict(extraction, full_text="\n\n".join(blocks),
            generation_unit_provenance={"editorial_question_id":q.concept_id,"ownership":[s.model_dump() for s in owned],
                "context":[s.model_dump() for s in context],"evidence_fingerprint":plan.evidence_fingerprint})
        try:
            result = _generate_single_batch(bounded,EDITORIAL_PROMPT,provider,diagnostics=audit,allow_source_abstention=True)
            if len(result["chunks"]) != 1:
                raise ChunkGenerationError("An editorial question must return one independent concept draft.", code="SCHEMA_INVALID")
            if result['article']['source_file'] != extraction.get('file_name'):
                raise ChunkGenerationError("Editorial output source identity mismatch.",code="EXTRACTION_BLOCKED")
        except Exception as exc:
            telemetry.incomplete_candidates()
            audit["editorial"]["failure_classification"] = telemetry.failure(exc,getattr(exc,"code","GENERATION_FAILED"))
            raise ChunkGenerationError("Editorial generation failed; no complete draft package returned.",
                code=getattr(exc,"code","GENERATION_FAILED")) from None
        if generated is None:
            generated = {"article":dict(result["article"]),"chunks":[]}
        number = len(generated["chunks"])+1
        chunk = dict(result["chunks"][0],chunk_number=number)
        topics = [source.evidence.buffers[r.evidence.buffer_id].text[r.evidence.char_start:r.evidence.char_end]
                  for r in q.roles if r.role == "topic"]
        if not any(" ".join(t.casefold().split()) in " ".join(chunk['topic'].casefold().split()) for t in topics):
            label_findings.append(_issue(number,"ERROR","EDITORIAL_TOPIC_IDENTITY",
                "The editorial topic does not retain the explicitly source-bound clinical entity."))
        # No text rewriting, truncation or silent removal of unsafe candidates.
        # Rejected candidates remain visible in this review result and are
        # expressly excluded by its validation report.
        generated["chunks"].append(chunk)
        owners.append({"chunk_number":number,"question_id":q.concept_id,
            "ownership":[s.model_dump() for s in owned],"context":[s.model_dump() for s in context]})
        packets_by_chunk[number] = [{"text":evidence_text,"review_required":True}]
        assessments.append(assess_editorial_chunk(number,chunk["original_text"],source.evidence,owned,context,q.roles))
    generated["article"]["number_of_chunks"] = len(generated["chunks"])
    validate_generation_schema(generated)
    validate_generation_content(generated,extraction)
    # Source reference authority is unchanged; a mismatching supplied reference
    # has already failed deterministic binding. Do not infer citation details.
    if any(c["reference"] != reference for c in generated["chunks"]) or generated["article"]["reference"] != reference:
        raise ChunkGenerationError("Editorial output must retain the exact source reference authority.",code="REFERENCE_OR_CONTENT_INVALID")
    with telemetry.stage("final_document_validation"):
        baseline = validate_chunks(generated,chunk_sources=packets_by_chunk,editorial=True)
    # Existing validator findings/decisions remain. Its snippets/messages are
    # not exported in the new diagnostic contract: no clinical text or labels.
    issues = [{k:v for k,v in item.items() if k != "message"} | {
        "message":"Deterministic validation finding; inspect the indicated rule and source evidence."}
        for item in baseline["issues"]]
    issues.extend(label_findings)
    for a in assessments:
        if a.status == "REJECTED":
            issues.append(_issue(a.chunk_number,"ERROR","EDITORIAL_CLAIM_CONFLICT",
                "An independently checked claim conflicts with or lacks source evidence."))
        if a.reasons:
            issues.append(_issue(a.chunk_number,"WARNING","EDITORIAL_SCOPE_REVIEW",
                "Clinical scope or qualification preservation requires source review."))
    report = _validation_report(issues,len(generated["chunks"]))
    report.update(editorial_version=VERSION, claim_assessments=[a.model_dump(mode="json") for a in assessments],
        claim_totals=claim_totals(assessments), source_ownership=owners,
        document_complete=False, clinically_approved=False, qualified_clinician_reviewed=False,
        assessment_method="deterministic_consistency_prototype_not_semantic_proof")
    audit["editorial"]["validation"] = deepcopy(report)
    telemetry.count("assessed_candidate_events",len(assessments))
    telemetry.count("accepted_candidate_events",sum(r["status"]=="REVIEW_REQUIRED" for r in report["chunk_statuses"]))
    telemetry.count("rejected_candidate_events",sum(r["status"]=="REJECTED" for r in report["chunk_statuses"]))
    telemetry.count("merged_candidates",len(generated["chunks"]),replace=True)
    return EditorialDraft(generated=generated,validation=report)
