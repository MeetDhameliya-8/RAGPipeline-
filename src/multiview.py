"""Bounded native-view selection. Representation never transfers source ownership."""
from __future__ import annotations

from typing import Literal, TYPE_CHECKING
import re

from pydantic import Field

from .concept_planner import (Contract, ClinicalPlan, EvidenceIndex, build_clinical_plan,
    validate_clinical_plan, reconcile_concept_coverage, _atoms)
from .draft_support import NativeSupportIndex, overlaps
from .evidence import EvidenceDocument, EvidenceSpan, text_hash, validate_evidence
from .numeric_evidence import numeric_evidence, _quantity_context

if TYPE_CHECKING:
    from .source_map import SourceDocument

VERSION = "native-multiview-v1"


class UnitSelection(Contract):
    view: Literal["original", "alternate"]
    unit_id: str
    outcome: Literal["supported_original", "supported_alternate", "supported_both",
        "duplicate_representation", "conflicting_evidence", "unresolved_source_association",
        "requires_extraction_repair", "requires_source_review"]
    selected: bool = False
    native_ownership: list[EvidenceSpan] = Field(default_factory=list)
    related_unit_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class MultiViewSelection(Contract):
    version: Literal["native-multiview-v1"] = VERSION
    selection_id: str
    pdf_sha256: str
    original_plan_id: str
    alternate_evidence: EvidenceDocument
    alternate_plan: ClinicalPlan
    decisions: list[UnitSelection]
    max_units: int
    blockers: list[str] = Field(default_factory=list)
    document_complete: Literal[False] = False

    @property
    def selected(self) -> list[UnitSelection]:
        return [d for d in self.decisions if d.selected]


def _value(doc: EvidenceDocument, s: EvidenceSpan) -> str:
    return " ".join(doc.buffers[s.buffer_id].text[s.char_start:s.char_end].split())


def _native_readings(doc: EvidenceDocument) -> dict:
    result = {}
    for page in doc.pages:
        for candidate in page.candidates:
            if candidate.extractor not in {"pypdf", "pymupdf"}:
                continue
            text = doc.buffers[candidate.buffer_id].text
            for a, b in _atoms(text):
                value = text[a:b]
                numbers = numeric_evidence(value)
                if numbers:
                    key = (page.page_number, candidate.extractor, _quantity_context(value))
                    result.setdefault(key, []).append([(n.value, n.operator, n.unit) for n in numbers])
    return result


def _conflicting_native_readings(concept, doc: EvidenceDocument, index: dict) -> bool:
    """Only uniquely aligned literal propositions; never a document number set.

    Two differently scoped occurrences are not interchangeable evidence. If a
    candidate contains repeated literal contexts, association stays unresolved.
    """
    if concept.role != "clinical_proposition" or len(concept.page_numbers) != 1:
        return False
    value = _value(doc, concept.ownership[0])
    if not numeric_evidence(value):
        return False
    context = _quantity_context(value)
    if not context:
        return False
    readings = {}
    for extractor in ("pypdf", "pymupdf"):
        aligned = index.get((concept.page_numbers[0], extractor, context), [])
        if len(aligned) == 1:
            readings[extractor] = aligned[0]
    return len(readings) == 2 and readings["pypdf"] != readings["pymupdf"]


def _candidates(view: str, plan: ClinicalPlan, doc: EvidenceDocument):
    index, native = EvidenceIndex(doc), NativeSupportIndex(doc)
    readings = _native_readings(doc)
    concepts = {c.concept_id: c for c in plan.concepts}
    rows, signatures = [], {}
    for unit in plan.units:
        row = UnitSelection(view=view, unit_id=unit.unit_id, outcome="requires_source_review",
                            reasons=list(unit.block_reasons))
        rows.append(row)
        if any(_conflicting_native_readings(concepts[cid], doc, readings) for cid in unit.concept_ids + unit.context_ids):
            row.outcome = "conflicting_evidence"
            row.reasons.append("conflicting_native_quantity_readings_in_literal_context")
            continue
        if unit.generation_eligibility != "eligible_draft":
            if any("source_mapping" in r or "nonexact" in r for r in row.reasons):
                row.outcome = "requires_extraction_repair"
            continue
        owned, context = [], []
        for cid in unit.concept_ids + unit.context_ids:
            concept = concepts[cid]
            # Every occurrence must have a certificate; no exact-text dedup can
            # turn uncertain native ownership into an independently owned claim.
            proofs = [native.prove(s, index.trace(s)) for s in concept.ownership]
            if not all(proofs):
                row.reasons.append("native_ownership_unresolved")
                break
            target = owned if cid in unit.concept_ids else context
            target.extend((p.native_block_span, _value(doc, p.canonical_span)) for p in proofs)
        else:
            row.selected = True
            row.outcome = "supported_original" if view == "original" else "supported_alternate"
            row.native_ownership = [s for s, _ in owned]
            signature = lambda items: sorted((s.buffer_id, s.char_start, s.char_end, value) for s, value in items)
            signatures[unit.unit_id] = (signature(owned), signature(context))
            continue
        row.outcome = "unresolved_source_association"
    return rows, signatures


def _decide(original: ClinicalPlan, old: EvidenceDocument, alternate: ClinicalPlan,
            new: EvidenceDocument, max_units: int):
    rows, signatures = _candidates("original", original, old)
    other, other_signatures = _candidates("alternate", alternate, new)
    signatures.update(other_signatures)
    # Build edges before changing selection flags. A later conflict cannot be
    # hidden by an earlier duplicate or by iteration order.
    duplicates, conflicts = [], set()
    candidates = [r for r in rows + other if r.selected]
    for i, a in enumerate(candidates):
        for b in candidates[i+1:]:
            if not any(overlaps(x, y) for x in a.native_ownership for y in b.native_ownership):
                continue
            a.related_unit_ids.append(b.unit_id); b.related_unit_ids.append(a.unit_id)
            if signatures[a.unit_id] == signatures[b.unit_id]:
                duplicates.append((a, b))
            else:
                conflicts.update((a.unit_id, b.unit_id))
    # A duplicate of a conflicting representation is held as well.
    changed = True
    while changed:
        changed = False
        for a, b in duplicates:
            if (a.unit_id in conflicts) != (b.unit_id in conflicts):
                conflicts.update((a.unit_id, b.unit_id)); changed = True
    for row in candidates:
        if row.unit_id in conflicts:
            row.selected = False; row.outcome = "conflicting_evidence"
            row.reasons.append("overlapping_native_ownership_or_context_differs")
    for a, b in duplicates:
        if a.unit_id in conflicts:
            continue
        if a.selected:
            a.outcome = "supported_both" if a.view != b.view else a.outcome
            b.selected = False; b.outcome = "duplicate_representation"
    blockers = []
    if sum(r.selected for r in rows + other) > max_units:
        blockers.append("Unique selected requests exceeds the configured request budget; no truncation.")
    return rows + other, blockers


def _identity(original: ClinicalPlan, alternate: ClinicalPlan, max_units: int) -> str:
    return "selection:" + text_hash(f"{VERSION}:{original.plan_id}:{alternate.plan_id}:{max_units}")


def build_multiview_selection(extraction: dict, source_doc: SourceDocument, *,
                             max_unit_chars: int = 20000, max_units: int = 80) -> MultiViewSelection:
    from .native_layout import recover_native_layout
    from .source_map import build_source_map
    old = source_doc.evidence
    if old is None or old.identity_quality != "pdf_bytes" or not old.pdf_sha256:
        raise ValueError("Multi-view selection requires verified PDF byte identity.")
    if old.native_layout_views:
        raise ValueError("Multi-view planning must start from the retained original view.")
    original = build_clinical_plan(extraction, source_doc, max_unit_chars=max_unit_chars, max_units=max_units)
    recovered = recover_native_layout(dict(extraction, evidence=old.model_dump(mode="json")))
    alternate_doc = build_source_map(recovered)
    alternate = build_clinical_plan(recovered, alternate_doc, max_unit_chars=max_unit_chars, max_units=max_units)
    decisions, blockers = _decide(original, old, alternate, alternate_doc.evidence, max_units)
    result = MultiViewSelection(selection_id=_identity(original, alternate, max_units),
        pdf_sha256=old.pdf_sha256, original_plan_id=original.plan_id,
        alternate_evidence=alternate_doc.evidence, alternate_plan=alternate,
        decisions=decisions, max_units=max_units, blockers=blockers)
    source_doc.clinical_plan = original
    validate_multiview_selection(result, source_doc)
    return result


def validate_multiview_selection(selection: MultiViewSelection, source_doc: SourceDocument) -> None:
    old, new, original = source_doc.evidence, selection.alternate_evidence, source_doc.clinical_plan
    if old is None or original is None:
        raise ValueError("Missing original multi-view source.")
    for doc in (old, new):
        validate_evidence(doc)
        if (doc.identity_quality != "pdf_bytes" or doc.pdf_sha256 != selection.pdf_sha256 or
                not re.fullmatch(r"[0-9a-f]{64}", selection.pdf_sha256) or doc.document_id != "pdf:" + selection.pdf_sha256):
            raise ValueError("Multi-view PDF identity mismatch.")
    if (old.document_id != new.document_id or old.config_hash != new.config_hash or
            old.pages != new.pages or old.raw_buffer_id != new.raw_buffer_id or
            any(new.buffers.get(k) != b for k, b in old.buffers.items()) or
            any(new.mappings.get(k) != m for k, m in old.mappings.items()) or
            any(w not in new.warnings for w in old.warnings)):
        raise ValueError("Multi-view source inventory or original evidence changed.")
    if new.canonical_buffer_id != old.canonical_buffer_id:
        if len(new.native_layout_views) != 1 or new.native_layout_views[0].original_canonical_id != old.canonical_buffer_id:
            raise ValueError("Alternate view is not derived from the retained original.")
    validate_clinical_plan(original, old)
    validate_clinical_plan(selection.alternate_plan, new)
    if selection.original_plan_id != original.plan_id or selection.selection_id != _identity(original, selection.alternate_plan, selection.max_units):
        raise ValueError("Multi-view plan identity mismatch.")
    decisions, blockers = _decide(original, old, selection.alternate_plan, new, selection.max_units)
    if decisions != selection.decisions or blockers != selection.blockers:
        raise ValueError("Multi-view selection metadata differs from recomputed source support.")


def selected_units(selection: MultiViewSelection, source_doc: SourceDocument):
    """Validated schedule with a separate canonical buffer for each request."""
    validate_multiview_selection(selection, source_doc)
    for row in selection.selected:
        plan, evidence = ((source_doc.clinical_plan, source_doc.evidence) if row.view == "original"
                          else (selection.alternate_plan, selection.alternate_evidence))
        unit = next(u for u in plan.units if u.unit_id == row.unit_id)
        yield row, plan, unit, evidence


def reconcile_multiview_coverage(selection: MultiViewSelection, source_doc: SourceDocument,
                                generated: dict, report: dict, diagnostics: dict) -> dict:
    validate_multiview_selection(selection, source_doc)
    views = {}
    for name, plan, doc in (("original", source_doc.clinical_plan, source_doc.evidence),
                           ("alternate", selection.alternate_plan, selection.alternate_evidence)):
        views[name] = reconcile_concept_coverage(plan, doc, generated, report, diagnostics)
    return {"plan_id": selection.selection_id, "complete": False, "complete_section_ids": [],
        "views": views, "selection_decisions": [r.model_dump(mode="json") for r in selection.decisions],
        "unique_selected_units": len(selection.selected), "clinically_approved_concepts": 0}


def validation_sources(selection: MultiViewSelection, source_doc: SourceDocument,
                       diagnostics: dict) -> dict[int, list[dict]]:
    """Resolve chunk origins into bounded evidence. Unknown/mixed origins fail closed."""
    schedule = {u.unit_id: (r, p, u, d) for r, p, u, d in selected_units(selection, source_doc)}
    result = {}
    for origin in diagnostics.get("chunk_ownership", []):
        ids = origin["unit_ids"]
        if not ids or any(i not in schedule for i in ids):
            raise ValueError("Unknown selected chunk origin.")
        # Deduplicated output may have multiple independently selected owners.
        # All must be validated; the validator uses each evidence packet separately.
        if origin["chunk_number"] in result:
            raise ValueError("Duplicate chunk origin record.")
        result[origin["chunk_number"]] = [
            {"text": "\n\n".join(d.buffers[s.buffer_id].text[s.char_start:s.char_end]
                for cid in u.context_ids + u.concept_ids
                for s in next(c for c in p.concepts if c.concept_id == cid).ownership[:1]),
             "view": r.view, "buffer_id": p.buffer_id,
             "evidence_document_id": d.document_id, "pdf_sha256": d.pdf_sha256,
             "unit_id": u.unit_id, "ownership": [s.model_dump() for s in u.ownership],
             "concept_ids": u.concept_ids, "context_ids": u.context_ids,
             "context_dependencies": [s.model_dump() for c in p.concepts if c.concept_id in u.context_ids for s in c.ownership],
             "native_ownership": [s.model_dump() for s in r.native_ownership],
             "page_numbers": sorted({page for c in p.concepts if c.concept_id in u.concept_ids for page in c.page_numbers})}
            for r, p, u, d in (schedule[i] for i in ids)]
    return result
