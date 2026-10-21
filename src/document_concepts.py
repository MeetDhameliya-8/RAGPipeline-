"""Document-wide evidence inventory and explicitly reviewed clinical questions.

Lexical detectors propose roles and review hazards; they never establish a
population/arm/outcome relationship. Only a caller's explicit source-review
record can bind those roles. This layer does not schedule or approve chunks.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .concept_planner import EvidenceIndex
from .diagnostics import timed
from .evidence import (EvidenceDocument, EvidenceSpan, EvidenceTrace, _check_span,
                       span, text_hash, validate_evidence, warning_blocks_coverage)

if TYPE_CHECKING:
    from .source_map import SourceDocument

VERSION = "document-concepts-v1"
Role = Literal["topic", "subtopic", "study_design", "population", "intervention",
               "comparator", "arm", "primary_outcome", "secondary_outcome", "outcome",
               "numerical_finding", "time_point", "safety", "recommendation",
               "recommendation_strength", "evidence_certainty", "limitation",
               "applicability", "qualification"]
Question = Literal["study_scope", "comparative_outcome", "primary_outcome",
                   "secondary_outcome", "safety", "recommendation", "applicability",
                   "source_statement"]
Coverage = Literal["represented_in_concept", "supporting_context_only",
                   "intentionally_excluded", "held_for_source_review", "not_yet_assessed"]
STATUSES = ("represented_in_concept", "supporting_context_only", "intentionally_excluded",
            "held_for_source_review", "not_yet_assessed")

# These detect literal cues only. Even a primary-outcome label does not link its
# numbers to a treatment, comparator, population or time point.
CUES = {
    "study_design": r"\b(?:randomi[sz]ed|trial|cohort|case.control|systematic review)\b",
    "population": r"\b(?:patients?|participants?|adults?|children|population)\b",
    "intervention": r"\b(?:received|intervention|treatment|therapy|assigned to)\b",
    "comparator": r"\b(?:placebo|comparator|versus|vs\.)",
    "arm": r"\b(?:arm|group)\s+[A-Za-z0-9]+\b",
    "primary_outcome": r"\bprimary\s+(?:outcome|endpoint)\b",
    "secondary_outcome": r"\bsecondary\s+(?:outcomes?|endpoints?)\b",
    "outcome": r"\b(?:outcomes?|endpoints?|remission|response|mortality|survival)\b",
    "numerical_finding": r"(?<!\w)[<>=+-]?\d+(?:\.\d+)?(?:%|\b)",
    "time_point": r"\b\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|years?)\b",
    "safety": r"\b(?:safety|adverse|toxicity|harms?)\b",
    "recommendation": r"\b(?:recommend\w*|suggest\w*|should)\b",
    "recommendation_strength": r"\b(?:conditional|strong|weak)\b",
    "evidence_certainty": r"\b(?:certainty|quality of evidence)\b",
    "limitation": r"\b(?:limitations?|underpowered|imprecision|bias)\b",
    "applicability": r"\b(?:applicability|generaliz\w*|excluding|restricted to)\b",
    "qualification": r"\b(?:not|non.significant|only|except|unless|however|although|may|conditional)\b",
}
STRUCTURED = re.compile(r"\b(?:table|figure|fig\.)\s*\d|^[ \t]*[*†‡]", re.I | re.M)
REFERENT = re.compile(r"\b(?:these|those|they|their|respectively|former|latter|above|below)\b", re.I)
DAMAGE = re.compile(r"\ufffd|\(cid:\d+\)|/(?:H\d{4,}|C\d{2,})|[\x00-\x08\x0b\x0c\x0e-\x1f]")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LiteralCue(Record):
    role: Role
    evidence: EvidenceSpan
    relationship: Literal["literal_cue_only"] = "literal_cue_only"


class SourceRegion(Record):
    region_id: str
    basis: Literal["canonical", "warning", "unmapped_native", "empty_page"]
    ownership: EvidenceSpan | None
    trace: EvidenceTrace
    literal_duplicate_native_spans: list[EvidenceSpan] = Field(default_factory=list)
    page_numbers: list[int]
    section_ids: list[str]
    section_relationship: Literal["raw_overlap_candidates"] = "raw_overlap_candidates"
    cues: list[LiteralCue] = Field(default_factory=list)
    warning_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    coverage: Coverage = "not_yet_assessed"
    concept_ids: list[str] = Field(default_factory=list)


class BoundRole(Record):
    role: Role
    evidence: EvidenceSpan


class SourceReview(Record):
    """Explicit human source reading, not a lexical proof or clinical approval.

    Review identity is opaque; no reviewer name, source quote or free text is stored.
    The application cannot verify the truth of an attestation: callers must obtain
    it through real source review, never manufacture it from cue matches.
    """
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis: Literal["human_source_review"]


class ReviewedQuestion(Record):
    review: SourceReview
    question: Question
    question_anchor: EvidenceSpan
    owned_region_ids: list[str] = Field(min_length=1)
    context_region_ids: list[str] = Field(default_factory=list)
    roles: list[BoundRole] = Field(min_length=1)
    # Full source statements that the reviewer read to bind the roles, including
    # any cross-page definition. Span validity alone never proves the semantics.
    relationship_evidence: list[EvidenceSpan] = Field(min_length=1)
    same_population_domain_and_interventions: Literal[True]
    arm_outcome_timepoint_checked: Literal[True]
    all_qualifications_included: Literal[True]


class ReviewedExclusion(Record):
    review: SourceReview
    region_id: str
    reason: Literal["reference_list", "nonclinical_front_matter", "nonclinical_boilerplate"]
    source_basis: EvidenceSpan


class ConceptQuestion(Record):
    concept_id: str
    question: Question
    question_anchor: EvidenceSpan
    owned_region_ids: list[str]
    context_region_ids: list[str] = Field(default_factory=list)
    roles: list[BoundRole] = Field(default_factory=list)
    relationship_evidence: list[EvidenceSpan] = Field(default_factory=list)
    relationship_basis: Literal["unresolved", "human_source_review"]
    review_id: str | None = None
    status: Literal["unresolved", "source_bound", "held"]
    reasons: list[str]


class DocumentConceptPlan(Record):
    version: Literal["document-concepts-v1"] = VERSION
    plan_id: str
    evidence_document_id: str
    evidence_config_hash: str
    canonical_buffer_id: str
    evidence_fingerprint: str
    source_warning_ids: list[str]
    regions: list[SourceRegion]
    concepts: list[ConceptQuestion]
    reviews: list[ReviewedQuestion]
    exclusions: list[ReviewedExclusion]
    coverage_counts: dict[str, int]
    canonical_character_counts: dict[str, int]
    planning_accounted: bool
    document_complete: Literal[False] = False
    clinically_approved: Literal[False] = False


def evidence_fingerprint(source: SourceDocument) -> str:
    """Bind every current buffer, mapping, warning, page, section and config.

    No proof is cached by object identity, filename, page number or text alone.
    """
    if source.evidence is None:
        raise ValueError("Document concept planning requires source evidence.")
    payload = {"version": VERSION, "evidence": source.evidence.model_dump(mode="json"),
               "source_map_config_hash": source.config_hash, "source_content_hash": source.content_hash,
               "source_map_buffer_id": source.buffer_id, "page_count": source.page_count,
               "sections": [s.model_dump(mode="json") for s in source.sections]}
    return text_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _overlap(a: EvidenceSpan, b: EvidenceSpan) -> bool:
    return a.buffer_id == b.buffer_id and a.char_start < b.char_end and b.char_start < a.char_end


def _paragraphs(text: str):
    # Inventory boundaries, NOT semantic relationships or generation units.
    edges = [0] + [m.end() for m in re.finditer(r"\n\s*\n+", text)]
    if edges[-1] != len(text):
        edges.append(len(text))
    yield from zip(edges, edges[1:])


def _inventory(source: SourceDocument) -> list[SourceRegion]:
    from .source_map import HEADING_KEYWORDS, _looks_like_heading
    doc = source.evidence
    index = EvidenceIndex(doc)
    text = doc.buffers[doc.canonical_buffer_id].text
    blocking = [w for w in doc.warnings if warning_blocks_coverage(w)]
    warning_traces = {w.warning_id: index.trace(w.span) if w.span else EvidenceTrace() for w in blocking}
    regions = []
    for a, b in _paragraphs(text):
        owned = span(doc.canonical_buffer_id, a, b)
        value = text[a:b]
        trace = index.trace(owned)
        pages = index.pages(trace)
        # Existing section IDs include the source filename stem. Preserve an
        # opaque, reproducible identity instead of exporting that sensitive name.
        sections = ["section:"+text_hash(s.section_id) for s in source.sections if any(
            _overlap(f.source, r) for f in trace.fragments for r in s.source_spans)]
        reasons, warning_ids = [], []
        if doc.identity_quality != "pdf_bytes":
            reasons.append("pdf_identity_unverified")
        if not trace.fragments or trace.unmapped_spans or any(
            doc.buffers[s.buffer_id].text[s.char_start:s.char_end].strip() for s in trace.synthetic_spans):
            reasons.append("source_mapping_unresolved")
        if any(f.precision != "exact" and doc.buffers[f.source.buffer_id].text[f.source.char_start:f.source.char_end].strip()
               for f in trace.fragments):
            reasons.append("nonexact_source_mapping")
        if not sections and value.strip():
            reasons.append("section_identity_unresolved")
        for w in blocking:
            wt = warning_traces[w.warning_id]
            wp = [w.page_number] if w.page_number else index.pages(wt)
            # Conservative page/document scope; no new warning-independence proof.
            if (not wp or set(wp).intersection(pages) or (w.span and _overlap(w.span, owned)) or
                    any(_overlap(x.source, y.source) for x in trace.fragments for y in wt.fragments)):
                warning_ids.append(w.warning_id)
        if warning_ids:
            reasons.append("extraction_uncertainty")
        if STRUCTURED.search(value):
            reasons.append("structured_relationship_unproven")
        if REFERENT.search(value):
            reasons.append("referent_requires_source_review")
        if re.search(r"\b[A-Z]{2,6}\b", value):
            reasons.append("abbreviation_requires_source_review")
        if DAMAGE.search(value):
            reasons.append("damaged_source_passage")
        cues = [LiteralCue(role=role, evidence=span(owned.buffer_id, a+m.start(), a+m.end()))
                for role, expression in CUES.items() for m in re.finditer(expression, value, re.I)]
        heading = value.strip().lower().rstrip(":") in HEADING_KEYWORDS
        if not heading and _looks_like_heading(value.strip()):
            cues.append(LiteralCue(role="topic", evidence=owned))
        coverage = "held_for_source_review" if reasons else "supporting_context_only" if heading else "not_yet_assessed"
        if not value.strip() and not reasons:
            coverage = "intentionally_excluded"
            reasons = ["separator_only"]
        regions.append(SourceRegion(region_id="region:"+text_hash(f"{VERSION}:{doc.document_id}:{owned.model_dump_json()}"),
            basis="canonical", ownership=owned, trace=trace, page_numbers=pages, section_ids=sections,
            cues=cues, warning_ids=warning_ids, reasons=reasons, coverage=coverage))
    # Concerns without a canonical passage must stay visible. They count as
    # regions/concerns, never as extra canonical characters or recovered content.
    for w in blocking:
        regions.append(SourceRegion(region_id="concern:"+w.warning_id, basis="warning", ownership=None,
            trace=warning_traces[w.warning_id], page_numbers=[w.page_number] if w.page_number else index.pages(warning_traces[w.warning_id]),
            section_ids=[], warning_ids=[w.warning_id], reasons=["unresolved_evidence_warning"], coverage="held_for_source_review"))
    mapped = [f.source for r in regions if r.basis == "canonical" for f in r.trace.fragments]
    for page in doc.pages:
        selected = doc.buffers[page.selected_buffer_id]
        if not selected.text.strip():
            regions.append(SourceRegion(region_id="empty:"+page.page_id, basis="empty_page", ownership=None,
                trace=EvidenceTrace(), page_numbers=[page.page_number], section_ids=[],
                reasons=["no_selected_page_text"], coverage="held_for_source_review"))
        # Retain native characters lost during cleaning/canonical assembly,
        # including blocks that never appeared in the selected representation.
        sources = [span(selected.buffer_id, 0, len(selected.text))]
        sources += [b.span for b in page.blocks if b.span and b.span.buffer_id != selected.buffer_id]
        for native in sources:
            native_mapped = list(mapped)
            if native.buffer_id != selected.buffer_id:
                # Different raw representations are not missing source simply
                # because their buffer IDs differ. Credit only a unique exact
                # character match on THIS page, and retain the duplicate spans.
                # This is textual identity, never a clinical relationship proof.
                value = doc.buffers[native.buffer_id].text[native.char_start:native.char_end]
                offset = selected.text.find(value) if value else -1
                if offset >= 0 and selected.text.find(value, offset+1) < 0:
                    equivalent = span(selected.buffer_id, offset, offset+len(value))
                    for region in regions:
                        if region.basis != "canonical":
                            continue
                        for f in region.trace.fragments:
                            if f.precision != "exact" or not _overlap(f.source, equivalent):
                                continue
                            x, y = max(f.source.char_start, offset), min(f.source.char_end, equivalent.char_end)
                            duplicate = span(native.buffer_id, native.char_start+x-offset, native.char_start+y-offset)
                            native_mapped.append(duplicate)
                            region.literal_duplicate_native_spans.append(duplicate)
            intervals = sorted((max(native.char_start, s.char_start), min(native.char_end, s.char_end))
                               for s in native_mapped if _overlap(native, s))
            cursor = native.char_start
            gaps = []
            for x, y in intervals:
                if x > cursor:
                    gaps.append((cursor, x))
                cursor = max(cursor, y)
            if cursor < native.char_end:
                gaps.append((cursor, native.char_end))
            for x, y in gaps:
                if not doc.buffers[native.buffer_id].text[x:y].strip():
                    continue
                missing = span(native.buffer_id, x, y)
                trace = index.trace(missing)
                regions.append(SourceRegion(region_id="unmapped:"+text_hash(missing.model_dump_json()),
                    basis="unmapped_native", ownership=None, trace=trace, page_numbers=[page.page_number], section_ids=[],
                    reasons=["native_representation_not_mapped"], coverage="held_for_source_review"))
    return regions


def _contains(regions: list[SourceRegion], item: EvidenceSpan) -> bool:
    return any(r.ownership and r.ownership.buffer_id == item.buffer_id and
               r.ownership.char_start <= item.char_start < item.char_end <= r.ownership.char_end for r in regions)


def _whole_passage(doc: EvidenceDocument, region: SourceRegion, item: EvidenceSpan) -> bool:
    if not _contains([region], item):
        return False
    owned = region.ownership
    text = doc.buffers[owned.buffer_id].text
    return not (text[owned.char_start:item.char_start].strip() or text[item.char_end:owned.char_end].strip())


def _check_review(review: SourceReview, fingerprint: str) -> None:
    if review.evidence_fingerprint != fingerprint:
        raise ValueError("Source review evidence identity mismatch.")


def _questions(regions: list[SourceRegion], reviews: list[ReviewedQuestion], doc: EvidenceDocument, fingerprint: str) -> list[ConceptQuestion]:
    by_id = {r.region_id: r for r in regions}
    original_holds = {r.region_id: list(r.reasons) for r in regions if r.coverage == "held_for_source_review"}
    questions = []
    reviewed_owned = set()
    for review in reviews:
        _check_review(review.review, fingerprint)
        owned_ids, ctx_ids = review.owned_region_ids, review.context_region_ids
        if (len(set(owned_ids+ctx_ids)) != len(owned_ids+ctx_ids) or
                any(x not in by_id or by_id[x].basis != "canonical" for x in owned_ids+ctx_ids)):
            raise ValueError("Invalid reviewed question region identity.")
        if reviewed_owned.intersection(owned_ids):
            raise ValueError("Reviewed questions have overlapping ownership.")
        reviewed_owned.update(owned_ids)
        selected = [by_id[x] for x in owned_ids+ctx_ids]
        anchors = [review.question_anchor] + [r.evidence for r in review.roles] + review.relationship_evidence
        for item in anchors:
            _check_span(doc, item)
            if not _contains(selected, item):
                raise ValueError("Reviewed question anchor is outside its source regions.")
        kinds = [r.role for r in review.roles]
        if kinds.count("population") > 1:
            raise ValueError("Separate populations require separate reviewed questions.")
        required = {"topic", "population"}
        if review.question in {"comparative_outcome", "primary_outcome", "secondary_outcome", "safety"}:
            required |= {"intervention", "time_point"}
        if review.question == "comparative_outcome":
            required |= {"comparator", "outcome"}
        if review.question in {"primary_outcome", "secondary_outcome", "safety", "recommendation"}:
            required.add(review.question)
        if not required.issubset(kinds):
            raise ValueError("Reviewed question is missing explicit clinical scope roles.")
        if not all(any(_whole_passage(doc, r, a) for a in review.relationship_evidence) for r in selected if r.cues):
            raise ValueError("Reviewed clinical region lacks relationship evidence.")
        reasons = sorted({reason for r in selected for reason in original_holds.get(r.region_id, [])})
        # A neighboring qualifier is a hazard requiring inclusion, never proof of
        # a relationship. The complete paragraph is preserved, not just its cue.
        canonical = [r for r in regions if r.basis == "canonical"]
        included = set(owned_ids+ctx_ids)
        for i, r in enumerate(canonical):
            if r.region_id not in included:
                continue
            for n in canonical[max(0, i-1):i+2]:
                if n.region_id not in included and any(c.role in {"qualification", "safety", "limitation", "applicability"} for c in n.cues):
                    reasons.append("adjacent_qualification_not_included")
        cid = "question:"+text_hash(review.model_dump_json())
        question = ConceptQuestion(concept_id=cid, question=review.question,
            question_anchor=review.question_anchor.model_copy(deep=True),
            owned_region_ids=list(owned_ids), context_region_ids=list(ctx_ids),
            roles=[r.model_copy(deep=True) for r in review.roles],
            relationship_evidence=[s.model_copy(deep=True) for s in review.relationship_evidence], relationship_basis="human_source_review",
            review_id=review.review.review_id, status="held" if reasons else "source_bound", reasons=sorted(set(reasons)))
        questions.append(question)
        for rid in owned_ids+ctx_ids:
            r = by_id[rid]
            r.concept_ids.append(cid)
            if reasons:
                r.coverage = "held_for_source_review"
                r.reasons = sorted(set(r.reasons+reasons))
            elif r.coverage != "held_for_source_review":
                if rid in owned_ids:
                    r.coverage = "represented_in_concept"
                elif r.coverage != "represented_in_concept":
                    r.coverage = "supporting_context_only"
    # Shared contexts never allow an earlier question to remain source-bound
    # after a later dependent review was held. Propagate to a fixed point.
    changed = True
    while changed:
        changed = False
        for q in questions:
            selected = [by_id[x] for x in q.owned_region_ids+q.context_region_ids]
            if q.status == "source_bound" and any(r.coverage == "held_for_source_review" for r in selected):
                q.status = "held"
                q.reasons = ["shared_context_requires_source_review"]
                for r in selected:
                    r.coverage = "held_for_source_review"
                    r.reasons = sorted(set(r.reasons+q.reasons))
                changed = True
    for region in regions:
        if region.basis != "canonical" or region.region_id in reviewed_owned or region.coverage in {"supporting_context_only", "intentionally_excluded"}:
            continue
        kinds = {c.role for c in region.cues}
        questions_here = [q for q in ("primary_outcome", "secondary_outcome", "safety", "recommendation") if q in kinds]
        if kinds.intersection({"limitation", "applicability"}):
            questions_here.append("applicability")
        questions_here = questions_here or (["study_scope"] if kinds.intersection({"study_design", "population", "intervention"}) else ["source_statement"])
        for q in questions_here:
            cid = "candidate:"+text_hash(region.region_id+":"+q)
            questions.append(ConceptQuestion(concept_id=cid, question=q, question_anchor=region.ownership,
                owned_region_ids=[region.region_id], relationship_basis="unresolved",
                status="held" if region.coverage == "held_for_source_review" else "unresolved",
                reasons=region.reasons+["clinical_relationships_not_assessed"]))
    return questions


@timed("document_concept_planning")
def build_document_concept_plan(extraction: dict, source: SourceDocument, *,
                                reviews: list[ReviewedQuestion] | None = None,
                                exclusions: list[ReviewedExclusion] | None = None) -> DocumentConceptPlan:
    """Inventory all source, propose questions, and bind only explicit reviews.

    No network, mutation of source models, file writes or chunk decisions. Missing
    meaning remains unassessed/held; invalid source identities fail closed.
    """
    doc = source.evidence
    if doc is None:
        raise ValueError("Document concept planning requires source evidence.")
    validate_evidence(doc)
    if doc.identity_quality == "pdf_bytes" and (not doc.pdf_sha256 or
            not re.fullmatch(r"[a-f0-9]{64}", doc.pdf_sha256) or doc.document_id != "pdf:"+doc.pdf_sha256):
        raise ValueError("Document concept PDF identity is inconsistent.")
    if len({s.section_id for s in source.sections}) != len(source.sections):
        raise ValueError("Duplicate document section identity.")
    for section in source.sections:
        for item in section.source_spans:
            _check_span(doc, item)
    if doc.buffers[doc.canonical_buffer_id].text != str(extraction.get("full_text") or ""):
        raise ValueError("Document concept source differs from canonical evidence.")
    fingerprint = evidence_fingerprint(source)
    reviews = [ReviewedQuestion.model_validate(r.model_dump()) for r in reviews or []]
    exclusions = [ReviewedExclusion.model_validate(r.model_dump()) for r in exclusions or []]
    regions = _inventory(source)
    by_id = {r.region_id: r for r in regions}
    if len(by_id) != len(regions):
        raise ValueError("Duplicate document source region identity.")
    questions = _questions(regions, reviews, doc, fingerprint)
    excluded = set()
    for exclusion in exclusions:
        _check_review(exclusion.review, fingerprint)
        r = by_id.get(exclusion.region_id)
        if r is None or r.basis != "canonical" or r.region_id in excluded or r.concept_ids:
            raise ValueError("Invalid or conflicting source exclusion.")
        if r.coverage == "held_for_source_review":
            raise ValueError("Source uncertainty cannot be hidden by exclusion.")
        _check_span(doc, exclusion.source_basis)
        if not _contains([r], exclusion.source_basis):
            raise ValueError("Exclusion basis is outside its source region.")
        r.coverage = "intentionally_excluded"
        r.reasons = ["reviewed_"+exclusion.reason]
        excluded.add(r.region_id)
    # Excluded regions remain in the inventory with the explicit review basis,
    # rather than also appearing as unresolved clinical questions.
    questions = [q for q in questions if not set(q.owned_region_ids).intersection(excluded)]
    counts = {s: sum(r.coverage == s for r in regions) for s in STATUSES}
    chars = {s: sum(r.ownership.char_end-r.ownership.char_start for r in regions if r.ownership and r.coverage == s) for s in STATUSES}
    if sum(chars.values()) != len(doc.buffers[doc.canonical_buffer_id].text):
        raise ValueError("Document source character accounting mismatch.")
    payload = {"evidence": fingerprint, "reviews": [r.model_dump(mode="json") for r in reviews],
               "exclusions": [e.model_dump(mode="json") for e in exclusions]}
    return DocumentConceptPlan(plan_id="document-plan:"+text_hash(json.dumps(payload, sort_keys=True)),
        evidence_document_id=doc.document_id, evidence_config_hash=doc.config_hash,
        canonical_buffer_id=doc.canonical_buffer_id, evidence_fingerprint=fingerprint,
        source_warning_ids=[w.warning_id for w in doc.warnings],
        regions=regions, concepts=questions, reviews=reviews, exclusions=exclusions,
        coverage_counts=counts, canonical_character_counts=chars,
        planning_accounted=bool(regions) and not counts["held_for_source_review"] and not counts["not_yet_assessed"]).model_copy(deep=True)


def validate_document_concept_plan(plan: DocumentConceptPlan, source: SourceDocument) -> None:
    """Recompute against CURRENT evidence; reject tampering and stale reviews."""
    if source.evidence is None:
        raise ValueError("Document concept planning requires source evidence.")
    expected = build_document_concept_plan({"full_text": source.evidence.buffers[source.evidence.canonical_buffer_id].text},
                                         source, reviews=plan.reviews, exclusions=plan.exclusions)
    if expected != plan:
        raise ValueError("Document concept plan differs from its source-bound reconstruction.")
