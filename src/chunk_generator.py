"""Generate GastroAGI chunks from extracted article text."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .providers.azure import AzureChunkGenerator
from .providers.base import ChunkGenerationError, ChunkGeneratorProvider
from .providers.registry import provider_from_environment
from . import diagnostics as telemetry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "gastroagi_chunk_rules.md"
CONTENT_FIELDS = ("topic", "subtopic", "section", "reference", "original_text")
ALLOWED_GENERATED_CHUNK_FIELDS = set(CONTENT_FIELDS) | {"chunk_number"}
NUMBER_RE = re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?%?\b")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
CITATION_COMPONENT_RE = re.compile(
    r"(?P<journal>n\s+engl\s+j\s+med|new\s+england\s+journal\s+of\s+medicine|[A-Z][A-Za-z][A-Za-z &.-]{1,40})"
    r"\.?\s+(?P<year>\d{4})\s*;\s*"
    r"(?P<volume>\d(?:\s*\d){1,4})"
    r"(?:\s*\(\s*(?P<issue>\d+)\s*\))?"
    r"\s*:\s*(?P<pages>[A-Za-z]?\d+\s*[-–]\s*\d+)",
    re.IGNORECASE,
)
TOP_LEVEL_ARTICLE_FIELDS = (
    "source_file",
    "topic",
    "subtopic",
    "reference",
    "number_of_chunks",
)

class GeneratedArticleModel(BaseModel):
    """Strict generated article metadata schema."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_file: str
    topic: str
    subtopic: str
    reference: str = ""
    number_of_chunks: int = Field(ge=1)


class GeneratedChunkModel(BaseModel):
    """Strict generated chunk schema before clinical validation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    chunk_number: int = Field(default=1, ge=1)
    topic: str
    subtopic: str
    section: str
    reference: str = ""
    original_text: str


class GeneratedDocumentModel(BaseModel):
    """Strict generated document schema."""

    model_config = ConfigDict(extra="forbid", strict=True)

    article: GeneratedArticleModel
    chunks: list[GeneratedChunkModel]



@telemetry.observed_run
@telemetry.timed("generation")
def generate_chunks(
    extraction: dict[str, Any],
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    provider: ChunkGeneratorProvider | None = None,
    retry_feedback: str = "",
    diagnostics: dict[str, Any] | None = None,
    *, source_doc: Any = None, generation_plan: Any = None,
    attempt_directory: Path | None = None,
    progress_callback: Any = None,
    package_concurrency: int = 1,
    editorial_plan: Any = None,
) -> dict[str, Any]:
    """Generate over every bounded context; reject incomplete source routes."""
    if editorial_plan is not None:
        # Explicit Python opt-in only. The queue/CLI, extractive prompt and
        # checkpoint publication path remain unchanged and are not reused.
        if (diagnostics is None or generation_plan is not None or attempt_directory is not None
                or progress_callback is not None or package_concurrency != 1 or retry_feedback
                or Path(prompt_path) != DEFAULT_PROMPT_PATH):
            raise ChunkGenerationError("Editorial opt-in requires diagnostics and cannot use legacy scheduling, prompts or checkpoints.",code="EXTRACTION_BLOCKED")
        from .editorial_generation import generate_editorial_draft
        return generate_editorial_draft(extraction,source_doc,editorial_plan,provider,diagnostics).generated
    from .adaptive_ingestion import build_ingestion_plan

    extraction = dict(extraction)
    pages = extraction.get("pages") or []
    extraction["reference_source_text"] = extraction.get("reference_source_text") or (
        "\n\n".join(str(p.get("raw_text") or "") for p in pages[:2]) if pages
        else str(extraction.get("full_text") or "")
    )
    if extraction.get("evidence") is not None and source_doc is None:
        from .source_map import build_source_map
        source_doc=build_source_map(extraction)
    plan = generation_plan or build_ingestion_plan(extraction, source_doc=source_doc)
    telemetry.count("planned_source_units", len(plan.batches), replace=True)
    if plan.source_package_plan is not None:
        telemetry.count("planned_source_packages", len(plan.batches), replace=True)
    if extraction.get("evidence") is not None and plan.clinical_plan is None and plan.source_package_plan is None:
        raise ChunkGenerationError("Real source evidence requires a source-anchored clinical plan.",code="EXTRACTION_BLOCKED")
    audit = diagnostics if diagnostics is not None else {}
    audit.update(planned_requests=len(plan.batches), requests_started=0, completed_units=0,
                 generated_candidates=0, structurally_valid_chunks=0, characters_sent_to_model=0,
                 system_prompt_characters_sent=0, units=[], candidate_count_complete=False)
    audit["generation_units"] = [b.model_dump(mode="json", exclude={"text"}) for b in plan.batches]
    audit["evidence_document_id"] = plan.evidence_document_id
    audit["critical_evidence_warning_ids"] = plan.critical_warning_ids
    if plan.blockers:
        code = "REQUEST_BUDGET_EXCEEDED" if any("requests exceeds" in b for b in plan.blockers) else "EXTRACTION_BLOCKED"
        audit["failure_code"] = code
        raise ChunkGenerationError("ADAPTIVE_INGESTION_BLOCKED: " + " ".join(plan.blockers), code=code)
    if plan.source_package_plan is not None:
        from .package_generation import generate_source_packages
        return generate_source_packages(extraction,plan,source_doc,prompt_path,provider,audit,attempt_directory,progress_callback,
                                        max_concurrency=package_concurrency)
    if plan.clinical_plan is not None:
        return _generate_clinical_units(extraction,plan,source_doc,prompt_path,provider,retry_feedback,audit)
    if len(plan.batches) <= 1:
        extraction["generation_unit_provenance"] = audit["generation_units"][0] if plan.batches else {}
        result = _generate_single_batch(extraction, prompt_path, provider, retry_feedback, audit)
        telemetry.count("merged_candidates", len(result["chunks"]), replace=True)
        return result
    selected_provider = provider or _provider_from_environment()
    results = []
    for batch in plan.batches:
        bounded_extraction = dict(extraction, full_text=batch.text)
        bounded_extraction["generation_unit_provenance"] = batch.model_dump(mode="json", exclude={"text"})
        instruction = (
            f"This is source context {batch.batch_number} of {len(plan.batches)}; "
            f"cleaned-source character range {batch.char_start}:{batch.char_end}. "
            f"Text-derived document routing hint: {plan.document_type}. "
            "Generate only claims supported by this context. Do not infer absent context. "
            "Retain recommendation strength, evidence certainty, populations, units, "
            "table headers and footnotes when present. If a table relationship cannot "
            "be verified, do not reconstruct it from memory. The usual chunk count "
            "is guidance, never a document-wide cap. Return the existing JSON schema."
        )
        try:
            results.append(_generate_single_batch(bounded_extraction, prompt_path, selected_provider,
                                                 retry_feedback + "\n\n" + instruction, audit))
        except ChunkGenerationError as exc:
            raise ChunkGenerationError(f"Adaptive context {batch.batch_number}/{len(plan.batches)} failed; no complete package returned. {exc}",
                                       code=exc.code, diagnostics=exc.diagnostics) from exc
    merged = {"article": dict(results[0]["article"]), "chunks": []}
    seen = set()
    for result in results:
        for chunk in result["chunks"]:
            key = tuple(re.sub(r"\s+", " ", chunk[field]).strip() for field in CONTENT_FIELDS)
            if key in seen:
                telemetry.count("duplicate_candidates")
                telemetry.count("unassessed_duplicate_candidates")
                continue
            seen.add(key)
            merged["chunks"].append(dict(chunk, chunk_number=len(merged["chunks"])+1))
    merged["article"]["number_of_chunks"] = len(merged["chunks"])
    telemetry.count("merged_candidates", len(merged["chunks"]), replace=True)
    validate_generation_schema(merged)
    validate_generation_content(merged, extraction)
    return merged


def _generate_clinical_units(extraction, plan, source_doc, prompt_path, provider, retry_feedback, audit):
    from .concept_planner import materialize_unit, validate_clinical_plan
    from .evidence import get_evidence
    evidence=source_doc.evidence if source_doc is not None else get_evidence(extraction)
    clinical=plan.clinical_plan
    validate_clinical_plan(clinical,evidence)
    schedule = None
    if plan.multiview_selection is not None:
        from .multiview import selected_units
        if (source_doc is None or plan.clinical_plan != source_doc.clinical_plan or
                source_doc.multiview_selection != plan.multiview_selection or
                get_evidence(extraction).canonical_buffer_id != source_doc.evidence.canonical_buffer_id or
                get_evidence(extraction).document_id != source_doc.evidence.document_id):
            raise ChunkGenerationError("Multi-view generation source identity mismatch.", code="EXTRACTION_BLOCKED")
        schedule = {u.unit_id: (r, p, u, d) for r, p, u, d in selected_units(plan.multiview_selection, source_doc)}
        if [b.unit_id for b in plan.batches] != [r.unit_id for r in plan.multiview_selection.selected]:
            raise ChunkGenerationError("Multi-view batches differ from the selected schedule.", code="EXTRACTION_BLOCKED")
        audit["multiview_selection_id"] = plan.multiview_selection.selection_id
    audit["clinical_plan_id"]=clinical.plan_id
    audit["blocked_source_units"]=sum(u.generation_eligibility=="blocked" for u in clinical.units)
    audit["document_source_complete"]=False
    audit["chunk_ownership"]=[]
    if not plan.batches:
        raise ChunkGenerationError("ADAPTIVE_INGESTION_BLOCKED: No source-supported eligible draft units; human evidence adjudication is required.",code="EXTRACTION_BLOCKED")
    selected=provider or _provider_from_environment()
    units={u.unit_id:u for u in clinical.units}
    merged=None
    seen={}
    for batch in plan.batches:
        if schedule is not None:
            row, clinical, unit, evidence = schedule[batch.unit_id]
            by_id = {c.concept_id: c for c in clinical.concepts}
            if (batch.ownership != unit.ownership or batch.concept_ids != unit.concept_ids or
                    batch.context_dependencies != [s for cid in unit.context_ids for s in by_id[cid].ownership] or
                    batch.char_start != min(s.char_start for s in unit.ownership) or
                    batch.char_end != max(s.char_end for s in unit.ownership)):
                raise ChunkGenerationError("Multi-view batch provenance differs from source ownership.", code="EXTRACTION_BLOCKED")
        else:
            unit=units[batch.unit_id]
        if unit.generation_eligibility!="eligible_draft":
            raise ChunkGenerationError("Ineligible clinical unit was scheduled.",code="EXTRACTION_BLOCKED")
        packet=materialize_unit(clinical,unit,evidence)
        if len(packet)>clinical.max_unit_chars:
            raise ChunkGenerationError("Clinical unit exceeds its source/context budget.",code="EXTRACTION_BLOCKED")
        provenance = batch.model_dump(mode="json",exclude={"text"})
        if schedule is not None:
            provenance.update(view=row.view, buffer_id=clinical.buffer_id, pdf_sha256=evidence.pdf_sha256,
                              selection_id=plan.multiview_selection.selection_id)
        bounded=dict(extraction,full_text=packet,generation_unit_provenance=provenance)
        instruction=("This request contains source-anchored draft concepts. Generate claims only from OWNED SOURCE blocks. "
            "CONTEXT ONLY blocks supply qualifications and definitions; do not create extra chunks for them. "
            "Identifiers and labels are internal metadata, never clinical output fields. Do not invent relationships. "
            "Group compatible owned propositions when appropriate. A concept does not require its own 150-250 word chunk. "
            "Do not pad short evidence; shorter drafts remain subject to existing validation and human review. "
            "This is partial document evidence and cannot establish complete-document coverage. Return the existing five-field JSON contract.")
        try:
            result=_generate_single_batch(bounded,prompt_path,selected,retry_feedback+"\n\n"+instruction,audit)
        except ChunkGenerationError as exc:
            raise ChunkGenerationError(f"Source-owned unit failed; no complete package returned. {exc}",code=exc.code,diagnostics=exc.diagnostics) from exc
        if merged is None:merged={"article":dict(result["article"]),"chunks":[]}
        for chunk in result["chunks"]:
            key=tuple(re.sub(r"\s+"," ",chunk[field]).strip() for field in CONTENT_FIELDS)
            if key in seen:
                telemetry.count("duplicate_candidates")
                telemetry.count("unassessed_duplicate_candidates")
                record=audit["chunk_ownership"][seen[key]-1]
                if unit.unit_id not in record["unit_ids"]:record["unit_ids"].append(unit.unit_id)
                continue
            number=len(merged["chunks"])+1
            seen[key]=number
            merged["chunks"].append(dict(chunk,chunk_number=number))
            origin = {"chunk_number":number,"unit_ids":[unit.unit_id]}
            if schedule is not None:
                origin.update(view=row.view, buffer_id=clinical.buffer_id, pdf_sha256=evidence.pdf_sha256)
            audit["chunk_ownership"].append(origin)
    merged["article"]["number_of_chunks"]=len(merged["chunks"])
    telemetry.count("merged_candidates", len(merged["chunks"]), replace=True)
    validate_generation_schema(merged)
    validate_generation_content(merged,extraction)
    return merged


def _generate_single_batch(
    extraction: dict[str, Any],
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    provider: ChunkGeneratorProvider | None = None,
    retry_feedback: str = "",
    diagnostics: dict[str, Any] | None = None,
    *, allow_source_abstention: bool = False,
) -> dict[str, Any]:
    """Generate and parse chunks for one extracted PDF."""
    article_text = build_generation_input(extraction)
    source_file = str(extraction.get("file_name", "")).strip()
    if not article_text:
        raise ChunkGenerationError("Cannot generate chunks from empty article text.")

    prompt = load_generation_prompt(prompt_path)
    if retry_feedback.strip():
        prompt = f"{prompt.rstrip()}\n\n{retry_feedback.strip()}\n"
    selected_provider = provider or _provider_from_environment()
    audit = diagnostics if diagnostics is not None else {}
    unit = {"unit_number": audit.get("requests_started", 0) + 1,
            "input_characters": len(article_text), "system_prompt_characters": len(prompt),
            "input_fingerprint": sha256(article_text.encode()).hexdigest(), "outcome": "started"}
    audit.setdefault("units", []).append(unit)
    unit["source_provenance"] = extraction.get("generation_unit_provenance", {})
    audit["requests_started"] = unit["unit_number"]
    audit["characters_sent_to_model"] = audit.get("characters_sent_to_model", 0) + len(article_text)
    audit["system_prompt_characters_sent"] = audit.get("system_prompt_characters_sent", 0) + len(prompt)
    request_started = time.perf_counter()
    try:
        raw_json = telemetry.model_call("generation", selected_provider.generate, article_text, source_file, prompt)
    except ChunkGenerationError as exc:
        unit["failure_classification"] = telemetry.failure(exc, exc.code)
        telemetry.incomplete_candidates()
        # Only transport fields with controlled scalar values are copied below.
        unit.update(outcome=exc.code)
        audit["failure_code"] = exc.code
        raise
    finally:
        unit["provider_seconds"] = round(time.perf_counter() - request_started, 3)
    unit["response"] = telemetry.transport_metadata(getattr(raw_json, "metadata", {}))
    try:
        try:
            parsed = parse_model_json(raw_json)
        except ChunkGenerationError:
            repaired = repair_json_locally(raw_json)
            if repaired is None:
                raise
            parsed = parse_model_json(repaired)
        candidates = parsed.get("chunks")
        unit["candidate_count_known"] = isinstance(candidates, list)
        unit["generated_candidates"] = len(candidates) if isinstance(candidates, list) else 0
        audit["generated_candidates"] = audit.get("generated_candidates", 0) + unit["generated_candidates"]
        telemetry.count("generated_candidates", unit["generated_candidates"])
        audit["candidate_count_complete"] = (
            len(audit["units"]) == audit.get("planned_requests", 1)
            and all(u.get("candidate_count_known") is True for u in audit["units"])
        )
    except ChunkGenerationError as exc:
        unit["outcome"] = "invalid_json"
        unit["failure_classification"] = telemetry.failure(exc, "INVALID_JSON")
        telemetry.incomplete_candidates()
        audit["failure_code"] = exc.code = "INVALID_JSON"
        raise
    if allow_source_abstention:
        from .source_abstention import is_source_abstention
        if is_source_abstention(parsed, source_file):
            unit['outcome'] = 'source_passage_held'
            telemetry.count("source_held_units")
            raise ChunkGenerationError('No suitable source passage was selected; retained for source review.',
                                       code='SOURCE_PASSAGE_HELD')
    try:
        generated = normalize_generation_result(parsed, source_file=source_file)
    except ChunkGenerationError as exc:
        unit["outcome"] = "schema_invalid"
        unit["failure_classification"] = telemetry.failure(exc, "SCHEMA_INVALID")
        telemetry.count("schema_rejected_candidates", unit.get("generated_candidates", 0))
        audit["failure_code"] = exc.code = "SCHEMA_INVALID"
        raise
    audit["structurally_valid_chunks"] = audit.get("structurally_valid_chunks", 0) + len(generated["chunks"])
    try:
        bind_verified_references(generated, extraction)
        validate_generation_content(generated, extraction)
    except ChunkGenerationError as exc:
        unit["outcome"] = "reference_or_content_invalid"
        unit["failure_classification"] = telemetry.failure(exc, "REFERENCE_OR_CONTENT_INVALID")
        telemetry.count("reference_or_content_rejected_candidates", len(generated["chunks"]))
        audit["failure_code"] = exc.code = "REFERENCE_OR_CONTENT_INVALID"
        raise
    unit["outcome"] = "generated_not_clinically_validated"
    audit["completed_units"] = audit.get("completed_units", 0) + 1
    return generated


def check_llm_connectivity() -> dict[str, Any]:
    """Run a minimal non-clinical connectivity check for the configured provider."""
    provider = _provider_from_environment()
    check = getattr(provider, "check_connectivity", None)
    if not callable(check):
        raise ChunkGenerationError("Configured provider does not support connectivity checks.")
    return check()


def build_generation_input(extraction: dict[str, Any]) -> str:
    """Build the complete cleaned article payload sent to the LLM."""
    article_text = str(extraction.get("full_text", "")).strip()
    if not article_text:
        return ""

    reference_metadata = extraction.get("reference_metadata") or {}
    if not isinstance(reference_metadata, dict):
        reference_metadata = {}

    metadata_lines = [
        "Source-supported citation candidates detected from the PDF (not human verification):",
        f"Identity status: {reference_metadata.get('identity_status', 'legacy_source_check_required')}",
        f"Source file: {extraction.get('file_name', '')}",
        f"Candidate article title: {reference_metadata.get('article_title', '')}",
        f"Journal: {reference_metadata.get('journal', '')}",
        f"Publication year: {reference_metadata.get('publication_year', '')}",
        f"Volume: {reference_metadata.get('volume', '')}",
        f"Issue: {reference_metadata.get('issue', '')}",
        f"Pages: {reference_metadata.get('pages', '')}",
        f"DOI: {reference_metadata.get('doi', '')}",
        f"Verified reference: {reference_metadata.get('reference', '')}",
        "If Verified reference is non-empty, use it exactly for article.reference and every chunk.reference.",
        "If Verified reference is empty, do not invent journal, year, DOI, volume, issue, or page details.",
        "",
        "Cleaned scientific article text begins below.",
        "",
    ]
    return "\n".join(metadata_lines) + article_text


def load_generation_prompt(prompt_path: str | Path = DEFAULT_PROMPT_PATH) -> str:
    """Load the complete GastroAGI chunking prompt."""
    path = Path(prompt_path)
    try:
        prompt = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChunkGenerationError(f"GENERATION_FAILED: Prompt unavailable: {path}") from exc
    if not prompt.strip():
        raise ChunkGenerationError(f"GENERATION_FAILED: Prompt is empty: {path}")
    return prompt


def parse_model_json(raw_output: str) -> dict[str, Any]:
    """Parse a model response that should contain only one JSON object."""
    if not isinstance(raw_output, str):
        raise ChunkGenerationError("Model response content must be a string.", code="MALFORMED_RESPONSE", diagnostics={"reason_code": "JSON_NOT_STRING"})
    text = raw_output.strip()
    if not text:
        raise ChunkGenerationError("Model returned no JSON.", diagnostics={"reason_code": "JSON_EMPTY"})

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ChunkGenerationError("Model output was not parseable JSON.", diagnostics={"reason_code": "JSON_PARSE_ERROR"})
        try:
            parsed = json.loads(text[start : end + 1], object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise ChunkGenerationError("Model output was malformed JSON.", diagnostics={"reason_code": "JSON_PARSE_ERROR"}) from exc

    if not isinstance(parsed, dict):
        raise ChunkGenerationError("Model JSON must be an object.", diagnostics={"reason_code": "JSON_NOT_OBJECT"})
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChunkGenerationError("Model JSON contains duplicate field names.", code="INVALID_JSON", diagnostics={"reason_code": "JSON_DUPLICATE_KEYS"})
        result[key] = value
    return result


def validate_generation_schema(data: dict[str, Any]) -> None:
    """Validate generated JSON shape before clinical validation."""
    if not isinstance(data, dict):
        raise ChunkGenerationError("Generated JSON must be an object.", diagnostics={"reason_code": "JSON_NOT_OBJECT"})

    article = data.get("article")
    chunks = data.get("chunks")
    if not isinstance(article, dict):
        raise ChunkGenerationError("Generated JSON article must be an object.", diagnostics={"reason_code": "ARTICLE_NOT_OBJECT"})
    if not isinstance(chunks, list):
        raise ChunkGenerationError("Generated JSON chunks must be a list.", diagnostics={"reason_code": "CHUNKS_NOT_LIST"})

    canonical = dict(data)
    canonical.update({
        "article": _lower_key_dict(article),
        "chunks": [
            _lower_key_dict(chunk) if isinstance(chunk, dict) else chunk
            for chunk in chunks
        ],
    })
    try:
        GeneratedDocumentModel.model_validate(canonical)
    except ValidationError as exc:
        raise ChunkGenerationError(
            "Generated JSON failed schema validation: "
            + _format_validation_errors(exc),
            diagnostics={"reason_code": "RAW_SCHEMA_INVALID"},
        ) from exc
    if canonical["article"]["number_of_chunks"] != len(chunks):
        raise ChunkGenerationError("Generated JSON schema: declared number_of_chunks does not match returned chunks.", diagnostics={"reason_code": "CHUNK_COUNT_MISMATCH"})
    numbers = [chunk.get("chunk_number", index) for index, chunk in enumerate(canonical["chunks"], 1)]
    if len(set(numbers)) != len(numbers):
        raise ChunkGenerationError("Generated JSON schema: duplicate chunk_number values.", diagnostics={"reason_code": "DUPLICATE_CHUNK_NUMBER"})



def validate_generation_content(generated: dict[str, Any], extraction: dict[str, Any]) -> None:
    """Reject incomplete or unsupported clinical generation before validation."""
    article = generated.get("article") if isinstance(generated, dict) else None
    chunks = generated.get("chunks") if isinstance(generated, dict) else None
    if not isinstance(article, dict):
        raise ChunkGenerationError("Generated JSON article must be an object.", diagnostics={"reason_code": "ARTICLE_NOT_OBJECT"})
    if not isinstance(chunks, list):
        raise ChunkGenerationError("Generated JSON chunks must be a list.", diagnostics={"reason_code": "CHUNKS_NOT_LIST"})
    if not chunks:
        raise ChunkGenerationError("Generated JSON did not contain any clinical chunks.", diagnostics={"reason_code": "NO_CLINICAL_CHUNKS"})

    expected_count = article.get("number_of_chunks")
    if isinstance(expected_count, int) and expected_count != len(chunks):
        raise ChunkGenerationError(
            f"Generated JSON article number_of_chunks is {expected_count}, but {len(chunks)} chunk(s) were returned.",
            diagnostics={"reason_code": "CHUNK_COUNT_MISMATCH"},
        )

    source_text = str(extraction.get("reference_source_text") or extraction.get("full_text") or "")
    reference_metadata = extraction.get("reference_metadata") or {}
    if not isinstance(reference_metadata, dict):
        reference_metadata = {}

    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            raise ChunkGenerationError(f"Chunk {index} must be an object.", diagnostics={"reason_code": "CHUNK_NOT_OBJECT"})
        extra_fields = sorted(set(chunk) - ALLOWED_GENERATED_CHUNK_FIELDS)
        if extra_fields:
            raise ChunkGenerationError(
                f"Chunk {index} contains non-GastroAGI field(s): {', '.join(extra_fields)}.",
                diagnostics={"reason_code": "EXTRA_CONTENT_FIELD"},
            )
        for field in CONTENT_FIELDS:
            if not str(chunk.get(field, "")).strip():
                raise ChunkGenerationError(f"Chunk {index} is missing required field `{field}`.", diagnostics={"reason_code": "REQUIRED_FIELD_EMPTY"})
        _validate_generated_reference_against_source(index, str(chunk.get("reference", "")), source_text, reference_metadata)

    article_reference = str(article.get("reference", ""))
    if article_reference.strip():
        _validate_generated_reference_against_source(None, article_reference, source_text, reference_metadata)

def repair_json_locally(raw_output: str) -> str | None:
    """Attempt one conservative local repair by extracting the outer JSON object."""
    if not isinstance(raw_output, str):
        return None
    start = raw_output.find("{")
    end = raw_output.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = raw_output[start : end + 1].strip()
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return candidate


def normalize_generation_result(
    data: dict[str, Any], source_file: str = ""
) -> dict[str, Any]:
    """Normalize common key casing without hiding malformed output shape."""
    validate_generation_schema(data)
    article = _lower_key_dict(data["article"])
    chunks = data["chunks"]

    normalized_chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            raise ChunkGenerationError(f"Chunk {index} must be an object.")
        normalized = _lower_key_dict(chunk)
        normalized["chunk_number"] = normalized.get("chunk_number", index)
        for field in CONTENT_FIELDS:
            normalized[field] = normalized.get(field, "").strip()
        normalized_chunks.append(normalized)

    for field in ("source_file", "topic", "subtopic", "reference"):
        article[field] = article.get(field, "").strip()

    return {"article": article, "chunks": normalized_chunks}


def bind_verified_references(generated: dict[str, Any], extraction: dict[str, Any]) -> None:
    """Attach a verified article reference to blank generated reference fields only."""
    article = generated.get("article") if isinstance(generated, dict) else None
    chunks = generated.get("chunks") if isinstance(generated, dict) else None
    if not isinstance(article, dict) or not isinstance(chunks, list):
        return

    needs_reference = not str(article.get("reference", "")).strip() or any(
        isinstance(chunk, dict) and not str(chunk.get("reference", "")).strip()
        for chunk in chunks
    )
    if not needs_reference:
        return

    verified_reference = _verified_reference_from_extraction(extraction)
    if not verified_reference:
        raise ChunkGenerationError(
            "Generated JSON is missing reference field(s), and no verified source reference is available for deterministic binding.",
            diagnostics={"reason_code": "REFERENCE_BINDING_UNAVAILABLE"},
        )

    if not str(article.get("reference", "")).strip():
        article["reference"] = verified_reference
    for chunk in chunks:
        if isinstance(chunk, dict) and not str(chunk.get("reference", "")).strip():
            chunk["reference"] = verified_reference


def _verified_reference_from_extraction(extraction: dict[str, Any]) -> str:
    reference_metadata = extraction.get("reference_metadata") or {}
    if not isinstance(reference_metadata, dict):
        return ""
    reference = str(reference_metadata.get("reference", "")).strip()
    if not reference:
        return ""
    source_text = str(extraction.get("reference_source_text") or extraction.get("full_text") or "")
    try:
        _validate_generated_reference_against_source(
            None, reference, source_text, reference_metadata
        )
    except ChunkGenerationError:
        return ""
    return reference



def _validate_generated_numbers_against_source(index: int, original_text: str, source_text: str) -> None:
    source_numbers = {_normalize_number(number) for number in NUMBER_RE.findall(source_text)}
    missing = [
        number
        for number in sorted({_normalize_number(number) for number in NUMBER_RE.findall(original_text)})
        if number and number not in source_numbers
    ]
    if missing:
        raise ChunkGenerationError(
            f"Chunk {index} contains number(s) not found in extracted source text: {', '.join(missing[:8])}."
        )


def _validate_generated_reference_against_source(
    index: int | None,
    reference: str,
    source_text: str,
    reference_metadata: dict[str, Any],
) -> None:
    reference = reference.strip()
    if not reference:
        return
    from .pdf_extractor import _normalize_for_citation_matching
    source_blob = _normalize_for_citation_matching(source_text)
    label = "Article" if index is None else f"Chunk {index}"
    if reference_metadata.get("identity_status") in {"conflict", "unresolved"}:
        raise ChunkGenerationError(f"{label} reference source identity is unresolved or conflicting.", diagnostics={"reason_code": "REFERENCE_IDENTITY_CONFLICT"})
    authority = str(reference_metadata.get("reference") or reference_metadata.get("citation") or "")
    reference_dois = {doi.rstrip(".").lower() for doi in DOI_RE.findall(reference)}
    source_dois = {doi.rstrip(".").lower() for doi in DOI_RE.findall(source_blob)}
    authority_dois = {doi.rstrip(".").lower() for doi in DOI_RE.findall(authority)}
    if reference_dois and not reference_metadata.get("identity_status") and len(source_dois) != 1:
        raise ChunkGenerationError(f"{label} reference DOI ownership is ambiguous in legacy source metadata.", diagnostics={"reason_code": "REFERENCE_DOI_AMBIGUOUS"})
    missing_dois = sorted(reference_dois - source_dois)
    if missing_dois:
        raise ChunkGenerationError(
            f"{label} reference contains DOI(s) not found in extracted source text or metadata: {', '.join(missing_dois)}.",
            diagnostics={"reason_code": "REFERENCE_DOI_NOT_IN_SOURCE"},
        )
    if authority_dois and reference_dois and authority_dois != reference_dois:
        raise ChunkGenerationError(f"{label} reference DOI conflicts with focal source identity.", diagnostics={"reason_code": "REFERENCE_DOI_CONFLICT"})
    if authority and not authority_dois and reference_dois:
        raise ChunkGenerationError(f"{label} reference DOI is not bound to focal source identity.", diagnostics={"reason_code": "REFERENCE_DOI_UNBOUND"})
    if not authority_dois and reference_dois and len(source_dois) != 1:
        raise ChunkGenerationError(f"{label} reference DOI ownership is ambiguous.", diagnostics={"reason_code": "REFERENCE_DOI_AMBIGUOUS"})
    if reference_dois:
        remainder = DOI_RE.sub("", reference)
        remainder = re.sub(r"(?:https?://(?:dx\.)?doi\.org/|\bdoi\s*:?)", "", remainder, flags=re.I).strip(" .;,()")
        if not remainder:
            return
        if _citation_components(authority) is None:
            raise ChunkGenerationError(f"{label} reference bibliography is not bound to focal source identity.", diagnostics={"reason_code": "REFERENCE_BIBLIOGRAPHY_UNBOUND"})
        if _citation_components(remainder) is None:
            raise ChunkGenerationError(f"{label} reference contains unverified citation text.", diagnostics={"reason_code": "REFERENCE_CITATION_UNVERIFIED"})
        matched = CITATION_COMPONENT_RE.search(remainder)
        if matched and (remainder[:matched.start()] + remainder[matched.end():]).strip(" .;,()"):
            raise ChunkGenerationError(f"{label} reference contains unverified title or ownership text.", diagnostics={"reason_code": "REFERENCE_CITATION_UNVERIFIED"})
    candidate = _citation_components(reference)
    focal = _citation_components(authority)
    if candidate and focal and not _citation_components_match(reference, authority):
        raise ChunkGenerationError(f"{label} reference citation conflicts with focal source identity.", diagnostics={"reason_code": "REFERENCE_CITATION_COMPONENT_MISMATCH"})
    if not _reference_traceable_without_doi(reference, source_blob, {}):
        raise ChunkGenerationError(
            f"{label} reference is not traceable to extracted source text or verified metadata.",
            diagnostics={"reason_code": "REFERENCE_NOT_TRACEABLE"},
        )


def _normalize_number(number: str) -> str:
    return number.replace(",", "").strip()


def _normalize_text_for_matching(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _reference_traceable_without_doi(
    reference: str,
    source_blob: str,
    reference_metadata: dict[str, Any],
) -> bool:
    reference_key = _normalize_reference_key(reference)
    if not reference_key:
        return False
    metadata_candidates = [
        str(reference_metadata.get("reference", "")),
        str(reference_metadata.get("citation", "")),
    ]
    for candidate in metadata_candidates:
        if candidate and reference_key == _normalize_reference_key(candidate):
            return True
        if candidate and _citation_components_match(reference, candidate):
            return True
    if reference_key in _normalize_reference_key(source_blob):
        return True
    for candidate in _citation_candidates(source_blob):
        if _citation_components_match(reference, candidate):
            return True
    return False


def _citation_candidates(text: str) -> list[str]:
    # Overlapping starts avoid a preceding prose fragment becoming part of
    # the journal name and hiding a supported citation later in that fragment.
    return [match.group(1) for match in re.finditer(
        "(?=(" + CITATION_COMPONENT_RE.pattern + "))", text, CITATION_COMPONENT_RE.flags
    )]


def _citation_components_match(left: str, right: str) -> bool:
    left_components = _citation_components(left)
    right_components = _citation_components(right)
    if not left_components or not right_components:
        return False
    return (
        left_components["journal"] == right_components["journal"]
        and left_components["year"] == right_components["year"]
        and left_components["volume"] == right_components["volume"]
        and _pages_equivalent(left_components["pages"], right_components["pages"])
        and (not left_components["issue"] or left_components["issue"] == right_components["issue"])
    )


def _citation_components(text: str) -> dict[str, str] | None:
    match = CITATION_COMPONENT_RE.search(text)
    if not match:
        return None
    return {
        "journal": _normalize_journal(match.group("journal")),
        "year": match.group("year"),
        "volume": re.sub(r"\s+", "", match.group("volume")),
        "pages": _normalize_pages(match.group("pages")),
        "issue": match.group("issue") or "",
    }


def _normalize_journal(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    if normalized in {"n engl j med", "new england journal of medicine"}:
        return "n engl j med"
    return normalized


def _normalize_pages(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("–", "-").strip().rstrip(".")).lower()


def _pages_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    left_start, left_end = _split_page_range(left)
    right_start, right_end = _split_page_range(right)
    if left_start != right_start:
        return False
    return left_end == right_end or left_end.endswith(right_end) or right_end.endswith(left_end)


def _split_page_range(value: str) -> tuple[str, str]:
    parts = value.split("-", 1)
    if len(parts) != 2:
        return value, value
    return parts[0], parts[1]


def _normalize_reference_key(text: str) -> str:
    normalized = text.lower().replace("new england journal of medicine", "n engl j med")
    normalized = normalized.replace("n. engl. j. med.", "n engl j med")
    normalized = re.sub(r"\b36\s+4\b", "364", normalized)
    return re.sub(r"[^a-z0-9]+", "", normalized)

def _provider_from_environment() -> ChunkGeneratorProvider:
    return provider_from_environment()


def _lower_key_dict(data: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ChunkGenerationError("Generated JSON schema: field names must be strings.")
        alias = key.strip().lower().replace(" ", "_")
        if alias in canonical:
            raise ChunkGenerationError("Generated JSON schema: conflicting field-name aliases.")
        canonical[alias] = value
    return canonical


def _format_validation_errors(exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors()[:8]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        details.append(f"{location or '<root>'}: {error.get('type', 'invalid')}")
    return "; ".join(details) or "invalid schema"













