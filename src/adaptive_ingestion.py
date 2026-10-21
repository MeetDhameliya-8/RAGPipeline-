"""Deterministic pre-generation routing and bounded semantic context planning.

These batches are model inputs, never final clinical chunks. Offsets refer to
the exact cleaned source, not to PDF byte offsets or reconstructed table cells.
"""
from __future__ import annotations

from .diagnostics import timed

import re
from hashlib import sha256
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from .evidence import EvidenceSpan, get_evidence, span, text_hash, trace_to_raw, warning_pages, warning_blocks_coverage, WarningClassification
from .concept_planner import ClinicalPlan, build_clinical_plan
from .multiview import MultiViewSelection
from .source_packages import SourcePackagePlan
from .document_concepts import DocumentConceptPlan, build_document_concept_plan

if TYPE_CHECKING:
    from .source_map import SourceDocument


class AdaptiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_batch_chars: int = Field(default=20000, ge=1000)
    max_batches: int = Field(default=80, ge=1)
    version: str = "adaptive-v1"
    multiview: bool = False
    draft_packages: bool = False
    all_draft_packages: bool = False


class WarningDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warning_id: str
    classification: WarningClassification
    critical: bool
    scope: Literal["owned_span", "page", "document"]


class GenerationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_number: int
    char_start: int
    char_end: int
    text: str
    unit_id: str = ""
    ownership: list[EvidenceSpan] = Field(default_factory=list)
    context_dependencies: list[EvidenceSpan] = Field(default_factory=list)
    dependency_status: Literal["not_planned", "source_anchored"] = "not_planned"
    critical_warning_ids: list[str] = Field(default_factory=list)
    warning_dependencies: list[WarningDependency] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)


class IngestionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    source_hash: str
    document_type: Literal["guideline_or_consensus", "systematic_review", "qualitative_study", "narrative_review", "original_research", "unclassified"]
    batches: list[GenerationBatch]
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    buffer_id: str = "legacy:unidentified"
    evidence_document_id: str = ""
    evidence_config_hash: str = ""
    critical_warning_ids: list[str] = Field(default_factory=list)
    clinical_plan: ClinicalPlan | None = None
    multiview_selection: MultiViewSelection | None = None
    source_package_plan: SourcePackagePlan | None = None
    document_concept_plan: DocumentConceptPlan | None = None


@timed("planning")
def build_ingestion_plan(extraction: dict[str, Any], config: AdaptiveConfig | None = None, *, source_doc: SourceDocument | None = None) -> IngestionPlan:
    # This independent inventory precedes final writing. It cannot authorize,
    # regroup or suppress the existing draft-generation batches.
    document_plan = build_document_concept_plan(extraction, source_doc) if source_doc is not None else None
    result = _build_ingestion_plan(extraction, config, source_doc=source_doc)
    result.document_concept_plan = document_plan
    return result


def _build_ingestion_plan(extraction: dict[str, Any], config: AdaptiveConfig | None = None, *, source_doc: SourceDocument | None = None) -> IngestionPlan:
    config = config or AdaptiveConfig()
    if (config.multiview or config.draft_packages) and source_doc is None:
        raise ValueError("Multi-view planning requires an original source map with verified PDF evidence.")
    if source_doc is not None:
        if config.draft_packages:
            from .source_packages import build_source_package_plan
            packages = build_source_package_plan(extraction,source_doc,max_unit_chars=config.max_batch_chars,max_units=config.max_batches)
            if config.all_draft_packages and len(packages.packages)>config.max_batches:
                packages = build_source_package_plan(extraction,source_doc,max_unit_chars=config.max_batch_chars,
                    max_units=len(packages.packages))
            source_doc.source_package_plan = packages
            source_doc.clinical_plan = None;source_doc.multiview_selection = None
            batches = [GenerationBatch(batch_number=i, char_start=0,char_end=p.request_characters,text="",unit_id=p.unit_id,
                ownership=p.ownership,dependency_status="source_anchored") for i,p in enumerate(packages.packages,1)]
            return IngestionPlan(version=packages.version,source_hash=text_hash(extraction["full_text"]),
                document_type=_document_type(extraction["full_text"]),batches=batches,blockers=packages.blockers,
                evidence_document_id=packages.evidence_document_id,evidence_config_hash=packages.evidence_config_hash,
                buffer_id=source_doc.evidence.canonical_buffer_id,source_package_plan=packages,
                warnings=["Witnessed source text supports drafts only; clinical relationships and incomplete source coverage require review."])
        return _source_anchored_plan(extraction, source_doc, config)
    text = str(extraction.get("full_text") or "")
    kind = _document_type(text)
    blockers = list(extraction.get("adaptive_blockers") or [])
    warnings = ["Document type is a text-derived routing hint, not verified bibliographic metadata."]
    if not text.strip():
        blockers.append("No cleaned source text is available.")
    spans, errors = _semantic_spans(text, config.max_batch_chars)
    blockers.extend(errors)
    batches = []
    start = 0
    end = 0
    for a, b in spans:
        if b - start > config.max_batch_chars and end > start:
            batches.append(GenerationBatch(batch_number=len(batches)+1, char_start=start, char_end=end, text=text[start:end]))
            start = end
        end = b
    if end > start:
        batches.append(GenerationBatch(batch_number=len(batches)+1, char_start=start, char_end=end, text=text[start:end]))
    if len(batches) > config.max_batches:
        blockers.append(f"Planned {len(batches)} requests exceeds the configured limit {config.max_batches}; explicit configuration is required.")
    if any(b.char_end - b.char_start > config.max_batch_chars for b in batches):
        blockers.append("An indivisible source unit exceeds the context budget; structured extraction or review is required.")
    if text and (not batches or batches[0].char_start != 0 or batches[-1].char_end != len(text)):
        blockers.append("Source coverage is incomplete; generation is blocked.")
    evidence = get_evidence(extraction)
    critical_warning_ids = [w.warning_id for w in evidence.warnings if w.critical and not w.resolved]
    warnings_with_pages = [(w, set(warning_pages(evidence, w))) for w in evidence.warnings if warning_blocks_coverage(w)]
    for batch in batches:
        batch.ownership = [span(evidence.canonical_buffer_id, batch.char_start, batch.char_end)]
        batch.unit_id = text_hash(f"{evidence.document_id}:{evidence.config_hash}:{evidence.canonical_buffer_id}:{config.model_dump_json()}:{batch.char_start}:{batch.char_end}")
        batch.critical_warning_ids = list(critical_warning_ids)
        trace = trace_to_raw(evidence, batch.ownership[0]) if warnings_with_pages else None
        owned_pages = {evidence.buffers[f.source.buffer_id].page_number for f in trace.fragments} if trace else set()
        for warning, pages in warnings_with_pages:
            if warning.span and warning.span.buffer_id == evidence.canonical_buffer_id and warning.span.char_end > warning.span.char_start:
                if warning.span.char_start >= batch.char_end or warning.span.char_end <= batch.char_start:
                    continue
                scope = "owned_span"
            elif pages:
                if not pages.intersection(owned_pages):
                    continue
                scope = "page"
            else:
                scope = "document"
            batch.warning_dependencies.append(WarningDependency(warning_id=warning.warning_id,
                classification=warning.classification, critical=warning.critical, scope=scope))
    return IngestionPlan(version=config.version, source_hash=sha256(text.encode()).hexdigest(), document_type=kind,
                         batches=batches, blockers=blockers, warnings=warnings,
                         buffer_id=evidence.canonical_buffer_id, evidence_document_id=evidence.document_id,
                         evidence_config_hash=evidence.config_hash,
                         critical_warning_ids=critical_warning_ids)


def _document_type(text: str) -> str:
    head = text[:6000].lower()
    if any(term in head for term in ("practice guideline", "guidance paper", "practice guidance", "consensus document",
                                     "position paper", "recommendation statement", "nice guideline", "guidance for use",
                                     "recommendations of the advisory committee", "immunization schedule")):
        kind = "guideline_or_consensus"
    elif "systematic review" in head or "meta-analysis" in head:
        kind = "systematic_review"
    elif "qualitative" in head:
        kind = "qualitative_study"
    elif re.search(r"\boriginal (?:article|research)\b", head):
        kind = "original_research"
    elif re.search(r"\breview\b", head):
        kind = "narrative_review"
    else:
        kind = "unclassified"
    return kind


def _source_anchored_plan(extraction, source_doc, config):
    if config.multiview:
        from .multiview import build_multiview_selection, selected_units
        selection = build_multiview_selection(extraction, source_doc,
            max_unit_chars=config.max_batch_chars, max_units=config.max_batches)
        source_doc.multiview_selection = selection
        clinical = source_doc.clinical_plan
        batches = []
        for row, view_plan, unit, evidence in selected_units(selection, source_doc):
            by_id = {c.concept_id: c for c in view_plan.concepts}
            batches.append(GenerationBatch(batch_number=len(batches)+1,
                char_start=min(s.char_start for s in unit.ownership), char_end=max(s.char_end for s in unit.ownership),
                text="", unit_id=unit.unit_id, ownership=unit.ownership,
                context_dependencies=[s for cid in unit.context_ids for s in by_id[cid].ownership],
                dependency_status="source_anchored", concept_ids=unit.concept_ids,
                critical_warning_ids=[w.warning_id for w in evidence.warnings if w.critical and not w.resolved]))
        return IngestionPlan(version="native-multiview-v1", source_hash=sha256(extraction["full_text"].encode()).hexdigest(),
            document_type=_document_type(extraction["full_text"]), batches=batches, blockers=selection.blockers,
            buffer_id=clinical.buffer_id, evidence_document_id=clinical.evidence_document_id,
            evidence_config_hash=clinical.evidence_config_hash, clinical_plan=clinical, multiview_selection=selection,
            critical_warning_ids=list(dict.fromkeys(w.warning_id for d in (source_doc.evidence, selection.alternate_evidence)
                for w in d.warnings if w.critical and not w.resolved)),
            warnings=["Opt-in native-view selection; incomplete source coverage and mandatory clinical review."])
    source_doc.multiview_selection = None
    clinical=build_clinical_plan(extraction,source_doc,max_unit_chars=config.max_batch_chars,max_units=config.max_batches)
    source_doc.clinical_plan=clinical
    evidence=source_doc.evidence
    by_id={c.concept_id:c for c in clinical.concepts}
    critical=[w.warning_id for w in evidence.warnings if w.critical and not w.resolved]
    batches=[]
    for unit in clinical.units:
        if unit.generation_eligibility!="eligible_draft":continue
        batches.append(GenerationBatch(batch_number=len(batches)+1,char_start=min(s.char_start for s in unit.ownership),
            char_end=max(s.char_end for s in unit.ownership),text="",unit_id=unit.unit_id,ownership=unit.ownership,
            context_dependencies=[s for cid in unit.context_ids for s in by_id[cid].ownership],dependency_status="source_anchored",
            critical_warning_ids=critical,concept_ids=unit.concept_ids))
    return IngestionPlan(version="source-anchored-v1",source_hash=sha256(str(extraction.get("full_text") or "").encode()).hexdigest(),
        document_type=_document_type(str(extraction.get("full_text") or "")),batches=batches,blockers=clinical.blockers,buffer_id=clinical.buffer_id,
        evidence_document_id=clinical.evidence_document_id,evidence_config_hash=clinical.evidence_config_hash,
        critical_warning_ids=critical,clinical_plan=clinical,
        warnings=["Only eligible source-owned draft units are scheduled; document completeness and human approval remain unresolved."])


def _semantic_spans(text: str, limit: int) -> tuple[list[tuple[int, int]], list[str]]:
    """Use paragraph boundaries, then sentence boundaries for oversized prose.

    Never split an oversized numeric/table-like paragraph into unrelated cells.
    All whitespace belongs to a span, so recombination is byte-for-byte exact.
    """
    boundaries = [0] + [m.end() for m in re.finditer(r"\n\s*\n+", text)]
    if boundaries[-1] != len(text): boundaries.append(len(text))
    spans, errors = [], []
    for a, b in zip(boundaries, boundaries[1:]):
        paragraph = text[a:b]
        if b-a <= limit:
            spans.append((a,b))
            continue
        numeric_ratio = sum(c.isdigit() for c in paragraph)/max(1,len(paragraph))
        if numeric_ratio > .15 or re.match(r"\s*(?:table|figure|fig\.)\s*\d", paragraph, re.I):
            spans.append((a,b))
            errors.append(f"Oversized table/figure-like unit at cleaned characters {a}:{b} requires structured review.")
            continue
        cuts = [a] + [a+m.end() for m in re.finditer(r"[.!?][\"')]*\s+(?=[A-Z])", paragraph)]
        if cuts[-1] != b: cuts.append(b)
        spans.extend(zip(cuts,cuts[1:]))
    return spans, errors


def assess_page_text(text: str, page_number: int, has_visual_content: bool = False) -> list[str]:
    """Return explicit routes for incomplete pages, without inventing OCR text."""
    flags = []
    if len("".join(text.split())) < 200 and has_visual_content:
        flags.append(f"Page {page_number}: sparse text with visual content requires OCR/layout review.")
    if text.count("\ufffd") > 3 or len(re.findall(r"\(cid:\d+\)|/H\d{4,}|/C\d{2,}", text)) > 3:
        flags.append(f"Page {page_number}: unresolved text encoding requires an alternate extractor or OCR review.")
    return flags
