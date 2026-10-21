"""Source-map and checkpoint support for long clinical PDFs."""

from __future__ import annotations

from .diagnostics import timed

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from .chunk_validator import eligible_chunk_numbers
from .concept_planner import ClinicalPlan
from .multiview import MultiViewSelection
from .source_packages import SourcePackagePlan
from .evidence import EvidenceDocument, EvidenceSpan, EvidenceTrace, get_evidence, add_buffer, align_buffers, span, trace_to_raw, validate_evidence, warning_blocks_coverage, warning_pages


SectionStatus = Literal["pending", "processed", "deferred", "failed", "excluded"]
SectionKind = Literal["body", "appendix", "references", "table_or_figure"]

HEADING_KEYWORDS = {
    "abstract",
    "introduction",
    "background",
    "methods",
    "methodology",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "recommendations",
    "recommendation",
    "eligibility criteria",
    "inclusion criteria",
    "exclusion criteria",
    "outcomes",
    "primary outcome",
    "secondary outcomes",
    "limitations",
    "evidence summary",
    "treatment algorithm",
    "appendix",
    "appendices",
    "references",
    "bibliography",
}
REFERENCE_HEADINGS = {"references", "bibliography"}
APPENDIX_RE = re.compile(r"^(appendix|appendices)\b", re.IGNORECASE)
NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.)\s+[A-Z][A-Za-z0-9,;/()\- ]{2,120}$")
TABLE_FIGURE_RE = re.compile(r"^(table|figure|fig\.)\s*\d+[.:\s-]", re.IGNORECASE)
PAGE_NUMBER_RE = re.compile(r"^(?:page\s*)?\d+\s*(?:of\s+\d+)?$", re.IGNORECASE)
SENTENCE_END_RE = re.compile(r"[.!?:;)](?:\s|$)")


class SourceMapConfig(BaseModel):
    """Configuration that affects section detection and checkpoint validity."""

    model_config = ConfigDict(extra="forbid")

    max_section_words: int = 1800
    repeated_line_min_pages: int = 2
    min_heading_context_words: int = 0
    version: str = "source-map-v1"

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class SourceSpan(BaseModel):
    """A source span with page and character provenance."""

    model_config = ConfigDict(extra="forbid")

    page_start: int
    page_end: int
    char_start: int
    char_end: int
    buffer_id: str = "legacy:unidentified"


class SourceSection(BaseModel):
    """A semantic source section for section-level processing."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    kind: SectionKind = "body"
    text: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    status: SectionStatus = "pending"
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    buffer_id: str = "legacy:unidentified"
    source_spans: list[EvidenceSpan] = Field(default_factory=list)
    source_trace: EvidenceTrace | None = None


class SourceDocument(BaseModel):
    """Structure and provenance map for one extracted PDF."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    source_file: str
    page_count: int
    content_hash: str
    config_hash: str
    sections: list[SourceSection]
    warnings: list[str] = Field(default_factory=list)
    buffer_id: str = "legacy:unidentified"
    evidence: EvidenceDocument | None = None
    clinical_plan: ClinicalPlan | None = None
    multiview_selection: MultiViewSelection | None = None
    source_package_plan: SourcePackagePlan | None = None


class SectionProcessingRecord(BaseModel):
    """Resume state for one section."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    status: SectionStatus = "pending"
    reason: str = ""
    chunk_fingerprints: list[str] = Field(default_factory=list)


class ProcessingState(BaseModel):
    """Checkpoint state for section-level processing."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    source_file: str
    content_hash: str
    config_hash: str
    invalidated: bool = False
    invalidation_reason: str = ""
    sections: list[SectionProcessingRecord]


@timed("source_mapping")
def build_source_map(
    extraction: dict[str, Any], config: SourceMapConfig | None = None
) -> SourceDocument:
    """Build a section/source map from extracted PDF text without losing provenance."""
    config = config or SourceMapConfig()
    source_file = str(extraction.get("file_name", "")).strip()
    page_count = int(extraction.get("page_count") or 0)
    pages = extraction.get("pages") if isinstance(extraction.get("pages"), list) else []
    if not pages:
        full_text = str(extraction.get("full_text") or extraction.get("raw_full_text") or "")
        pages = [{"page_number": 1, "text": full_text, "raw_text": full_text}]
        page_count = 1 if full_text else page_count

    paragraphs = _paragraph_records(pages, config)
    content_text = "\n\n".join(record["text"] for record in paragraphs)
    content_hash = sha256(content_text.encode("utf-8")).hexdigest()
    document_id = _document_id(source_file, content_hash)

    warnings: list[str] = []
    if not content_text.strip():
        warnings.append("No source text available for section mapping.")
    extraction_warnings = extraction.get("extraction_warnings")
    if isinstance(extraction_warnings, list):
        warnings.extend(str(warning) for warning in extraction_warnings if str(warning).strip())

    sections = _sections_from_paragraphs(paragraphs, document_id, config)
    if not sections and content_text.strip():
        sections = [
            SourceSection(
                section_id=f"{document_id}:s001",
                title="Document Body",
                text=content_text,
                page_start=_min_page(paragraphs),
                page_end=_max_page(paragraphs),
                char_start=0,
                char_end=len(content_text),
            )
        ]

    evidence = get_evidence(extraction)
    buffer_id = add_buffer(evidence, "section_map", config.fingerprint(), "\n\n".join(s.text for s in sections))
    align_buffers(evidence, evidence.cleaned_buffer_id, buffer_id, "existing_section_mapping")
    for section in sections:
        section.buffer_id = buffer_id
        section.source_trace = trace_to_raw(evidence, span(buffer_id, section.char_start, section.char_end))
        section.source_spans = [f.source for f in section.source_trace.fragments]
    validate_evidence(evidence)
    return SourceDocument(
        document_id=document_id,
        source_file=source_file,
        page_count=page_count,
        content_hash=content_hash,
        config_hash=config.fingerprint(),
        sections=sections,
        warnings=warnings,
        buffer_id=buffer_id,
        evidence=evidence,
    )


def initial_processing_state(source_doc: SourceDocument) -> ProcessingState:
    """Create initial section checkpoint state."""
    return ProcessingState(
        document_id=source_doc.document_id,
        source_file=source_doc.source_file,
        content_hash=source_doc.content_hash,
        config_hash=source_doc.config_hash,
        sections=[
            SectionProcessingRecord(
                section_id=section.section_id,
                status=section.status,
                reason=section.reason,
            )
            for section in source_doc.sections
        ],
    )


def load_or_initialize_processing_state(
    state_path: str | Path, source_doc: SourceDocument
) -> ProcessingState:
    """Load a checkpoint when compatible, otherwise return an invalidated fresh state."""
    path = Path(state_path)
    if not path.exists():
        return initial_processing_state(source_doc)
    try:
        loaded = ProcessingState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = initial_processing_state(source_doc)
        return state.model_copy(
            update={"invalidated": True, "invalidation_reason": "checkpoint_unreadable"}
        )
    if loaded.content_hash != source_doc.content_hash:
        state = initial_processing_state(source_doc)
        return state.model_copy(
            update={"invalidated": True, "invalidation_reason": "source_content_changed"}
        )
    if loaded.config_hash != source_doc.config_hash:
        state = initial_processing_state(source_doc)
        return state.model_copy(
            update={"invalidated": True, "invalidation_reason": "source_map_config_changed"}
        )
    current_sections = {section.section_id for section in source_doc.sections}
    loaded_sections = {section.section_id for section in loaded.sections}
    if current_sections != loaded_sections:
        state = initial_processing_state(source_doc)
        return state.model_copy(
            update={"invalidated": True, "invalidation_reason": "section_set_changed"}
        )
    return loaded


def save_processing_state(path: str | Path, state: ProcessingState) -> None:
    """Persist processing state atomically enough for local checkpoint recovery."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)


def mark_processed_sections_from_validation(
    source_doc: SourceDocument,
    state: ProcessingState,
    validation_report: dict[str, Any],
    *, concept_coverage: dict[str, Any] | None = None,
) -> ProcessingState:
    """Update section states using chunk evidence without regenerating duplicate work."""
    passed_chunks = eligible_chunk_numbers(validation_report, {"PASS"})
    linked_section_ids: set[str] = set()
    for evidence in validation_report.get("source_evidence", []) if isinstance(validation_report, dict) else []:
        if not isinstance(evidence, dict):
            continue
        chunk_number = evidence.get("chunk")
        if not isinstance(chunk_number, int) or chunk_number not in passed_chunks:
            continue
        for section_id in evidence.get("source_section_ids") or []:
            linked_section_ids.add(str(section_id))

    if concept_coverage is not None:
        linked_section_ids.intersection_update(concept_coverage.get("complete_section_ids", []))
    records: list[SectionProcessingRecord] = []
    previous = {record.section_id: record for record in state.sections}
    for section in source_doc.sections:
        record = previous.get(section.section_id) or SectionProcessingRecord(section_id=section.section_id)
        if section.status == "excluded":
            records.append(
                record.model_copy(update={"status": "excluded", "reason": section.reason})
            )
        elif section.status == "deferred":
            records.append(
                record.model_copy(update={"status": "deferred", "reason": section.reason})
            )
        elif section.section_id in linked_section_ids:
            records.append(
                record.model_copy(update={"status": "processed", "reason": "linked_to_generated_chunk"})
            )
        elif record.status == "processed" and concept_coverage is None:
            records.append(record)
        else:
            records.append(
                record.model_copy(update={"status": "deferred", "reason": "not_linked_to_generated_chunk"})
            )
    return state.model_copy(update={"sections": records})


def build_coverage_report(
    source_doc: SourceDocument, state: ProcessingState | None = None, *, concept_coverage: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return reviewable section coverage with explicit reasons."""
    state_by_id = {record.section_id: record for record in state.sections} if state else {}
    records = []
    counts: Counter[str] = Counter()
    for section in source_doc.sections:
        state_record = state_by_id.get(section.section_id)
        status = state_record.status if state_record else section.status
        reason = state_record.reason if state_record and state_record.reason else section.reason
        counts[status] += 1
        records.append(
            {
                "section_id": section.section_id,
                "title": section.title,
                "kind": section.kind,
                "status": status,
                "reason": reason,
                "page_range": [section.page_start, section.page_end],
                "warnings": section.warnings,
                "word_count": _word_count(section.text),
            }
        )
    unresolved = ([w.model_dump(mode="json") for w in source_doc.evidence.warnings if w.critical and not w.resolved]
                  if source_doc.evidence else [{"code": "evidence_unavailable", "critical": True}])
    if source_doc.evidence:
        validate_evidence(source_doc.evidence)
    uncertain = ([w.model_dump(mode="json") | {"affected_pages": warning_pages(source_doc.evidence, w)}
                  for w in source_doc.evidence.warnings if not w.critical and warning_blocks_coverage(w)]
                 if source_doc.evidence else [])
    all_warnings = ([w.model_dump(mode="json") | {"affected_pages": warning_pages(source_doc.evidence, w)}
                     for w in source_doc.evidence.warnings] if source_doc.evidence else [])
    if source_doc.multiview_selection is not None:
        from .multiview import validate_multiview_selection
        validate_multiview_selection(source_doc.multiview_selection, source_doc)
        alternate = source_doc.multiview_selection.alternate_evidence
        known = {w["warning_id"] for w in all_warnings}
        all_warnings.extend(w.model_dump(mode="json") | {"affected_pages": warning_pages(alternate, w), "view": "alternate"}
                            for w in alternate.warnings if w.warning_id not in known)
        unresolved = [w for w in all_warnings if w["critical"] and not w["resolved"]]
        uncertain.extend(w.model_dump(mode="json") | {"affected_pages": warning_pages(alternate, w), "view": "alternate"}
                         for w in alternate.warnings if w.warning_id not in known and not w.critical and warning_blocks_coverage(w))
    return {
        "source_package_holds": [h.model_dump(mode="json") for h in source_doc.source_package_plan.holds] if source_doc.source_package_plan else [],
        "source_coverage_complete": source_doc.source_package_plan is None and source_doc.multiview_selection is None and bool(records) and not unresolved and not uncertain and all(r["status"] in {"processed", "excluded"} for r in records)
            and (source_doc.clinical_plan is None or bool(concept_coverage and concept_coverage.get("complete"))),
        "clinical_concept_coverage": concept_coverage if concept_coverage is not None else
            {"plan_id": source_doc.clinical_plan.plan_id, "complete": False, "status": "not_reconciled"} if source_doc.clinical_plan else None,
        "unresolved_critical_evidence": unresolved,
        "unresolved_uncertain_evidence": uncertain,
        "evidence_warnings": all_warnings,
        "warning_classification_counts": dict(Counter(w["classification"] for w in all_warnings)),
        "buffer_id": source_doc.buffer_id,
        "document_id": source_doc.document_id,
        "source_file": source_doc.source_file,
        "content_hash": source_doc.content_hash,
        "config_hash": source_doc.config_hash,
        "summary": {status: counts.get(status, 0) for status in ("pending", "processed", "deferred", "failed", "excluded")},
        "warnings": source_doc.warnings,
        "sections": records,
    }


def source_sections_for_validation(source_doc: SourceDocument) -> list[dict[str, Any]]:
    """Expose source sections in the shape expected by chunk validation."""
    return [
        {
            "document_id": source_doc.document_id,
            "source_file": source_doc.source_file,
            "section_id": section.section_id,
            "title": section.title,
            "kind": section.kind,
            "page_start": section.page_start,
            "page_end": section.page_end,
            "text": section.text,
            "buffer_id": section.buffer_id,
            "source_spans": [s.model_dump() for s in section.source_spans],
            "source_trace": section.source_trace.model_dump(mode="json") if section.source_trace else None,
        }
        for section in source_doc.sections
        if section.status != "excluded"
    ]


def section_batches(source_doc: SourceDocument, max_words: int | None = None) -> list[list[str]]:
    """Plan adjacent processable section batches without arbitrary source truncation."""
    limit = max_words or SourceMapConfig().max_section_words
    batches: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    for section in source_doc.sections:
        if section.status in {"excluded", "deferred", "failed"}:
            continue
        section_words = _word_count(section.text)
        if current and current_words + section_words > limit:
            batches.append(current)
            current = []
            current_words = 0
        current.append(section.section_id)
        current_words += section_words
    if current:
        batches.append(current)
    return batches


def _paragraph_records(pages: list[Any], config: SourceMapConfig) -> list[dict[str, Any]]:
    page_paragraphs: list[dict[str, Any]] = []
    repeated = _repeated_margin_texts(pages, config)
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_number = int(page.get("page_number") or len(page_paragraphs) + 1)
        text = str(page.get("text") or page.get("raw_text") or "")
        for paragraph in re.split(r"\n\s*\n+", text.replace("\r\n", "\n").replace("\r", "\n")):
            cleaned = re.sub(r"[ \t]+", " ", paragraph).strip()
            if not cleaned:
                continue
            if PAGE_NUMBER_RE.match(cleaned):
                continue
            if _line_key(cleaned) in repeated:
                continue
            page_paragraphs.append(
                {"text": cleaned, "page_start": page_number, "page_end": page_number}
            )
    return _merge_cross_page_paragraphs(page_paragraphs)


def _merge_cross_page_paragraphs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for record in records:
        text = record["text"]
        if (
            merged
            and record["page_start"] == merged[-1]["page_end"] + 1
            and not _looks_like_heading(text)
            and not _looks_like_table_or_figure(text)
            and not _ends_like_complete_context(merged[-1]["text"])
        ):
            merged[-1]["text"] = f"{merged[-1]['text']} {text}"
            merged[-1]["page_end"] = record["page_end"]
            continue
        merged.append(record.copy())
    return merged


def _sections_from_paragraphs(
    paragraphs: list[dict[str, Any]], document_id: str, config: SourceMapConfig
) -> list[SourceSection]:
    sections: list[SourceSection] = []
    current_title = "Document Body"
    current_kind: SectionKind = "body"
    current_records: list[dict[str, Any]] = []
    after_references = False
    char_cursor = 0

    def flush(reason: str = "") -> None:
        nonlocal char_cursor, current_records, current_title, current_kind
        if not current_records:
            return
        section_text = "\n\n".join(record["text"] for record in current_records).strip()
        start = char_cursor
        end = start + len(section_text)
        section_id = f"{document_id}:s{len(sections) + 1:03d}"
        status: SectionStatus = "pending"
        warnings: list[str] = []
        section_reason = reason
        if current_kind == "references":
            status = "excluded"
            section_reason = "reference_list_excluded"
        elif current_kind == "table_or_figure":
            status = "deferred"
            section_reason = "table_or_figure_requires_review"
            warnings.append("Potentially important table or figure text was not converted into clinical claims automatically.")
        elif _word_count(section_text) > config.max_section_words:
            status = "deferred"
            section_reason = "section_exceeds_configured_word_limit"
            warnings.append("Long section should be processed in a controlled adjacent-section batch; no source text was truncated.")
        if current_title == "Document Body":
            warnings.append("Section boundary is inferred because no explicit heading preceded this text.")
        sections.append(
            SourceSection(
                section_id=section_id,
                title=current_title,
                kind=current_kind,
                text=section_text,
                page_start=_min_page(current_records),
                page_end=_max_page(current_records),
                char_start=start,
                char_end=end,
                status=status,
                reason=section_reason,
                warnings=warnings,
            )
        )
        char_cursor = end + 2
        current_records = []

    for record in paragraphs:
        text = record["text"]
        is_table = _looks_like_table_or_figure(text)
        is_heading = _looks_like_heading(text)
        normalized = _normalize_heading(text)
        if is_heading or is_table:
            flush()
            current_title = _clean_heading(text)
            if normalized in REFERENCE_HEADINGS:
                after_references = True
                current_kind = "references"
            elif after_references:
                current_kind = "references"
            elif is_table:
                current_kind = "table_or_figure"
            elif APPENDIX_RE.search(normalized):
                current_kind = "appendix"
            else:
                current_kind = "body"
            current_records = [record]
            continue
        if after_references:
            if current_kind != "references":
                flush()
                current_title = "References"
                current_kind = "references"
            current_records.append(record)
            continue
        if not current_records:
            current_title = "Document Body"
            current_kind = "body"
        current_records.append(record)
    flush()
    return sections


def _repeated_margin_texts(pages: list[Any], config: SourceMapConfig) -> set[str]:
    candidates: Counter[str] = Counter()
    for page in pages:
        if not isinstance(page, dict):
            continue
        text = str(page.get("text") or page.get("raw_text") or "")
        paragraphs = [
            _line_key(part)
            for part in re.split(r"\n\s*\n+", text)
            if _line_key(part) and not PAGE_NUMBER_RE.match(part.strip())
        ]
        if not paragraphs:
            continue
        candidates.update(paragraphs[:1] + paragraphs[-1:])
    return {key for key, count in candidates.items() if count >= config.repeated_line_min_pages}


def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    normalized = _normalize_heading(stripped)
    if normalized in HEADING_KEYWORDS:
        return True
    if NUMBERED_HEADING_RE.match(stripped) and _word_count(stripped) <= 14:
        return True
    if _word_count(stripped) <= 8 and stripped.isupper() and any(char.isalpha() for char in stripped):
        return True
    if _word_count(stripped) <= 10 and normalized in {keyword.rstrip("s") for keyword in HEADING_KEYWORDS}:
        return True
    return False


def _looks_like_table_or_figure(text: str) -> bool:
    return bool(TABLE_FIGURE_RE.match(text.strip()))


def _ends_like_complete_context(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped and SENTENCE_END_RE.search(stripped[-3:]))


def _clean_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:160]


def _normalize_heading(text: str) -> str:
    text = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", text.strip())
    return re.sub(r"\s+", " ", text.strip(" .:-")).lower()


def _line_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def _min_page(records: list[dict[str, Any]]) -> int:
    return min(int(record["page_start"]) for record in records) if records else 1


def _max_page(records: list[dict[str, Any]]) -> int:
    return max(int(record["page_end"]) for record in records) if records else 1


def _document_id(source_file: str, content_hash: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(source_file).stem).strip("-._") or "document"
    return f"{safe.lower()}-{content_hash[:12]}"
