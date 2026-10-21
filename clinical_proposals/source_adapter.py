"""Read-only adapters to existing evidence contracts; no review construction."""
from __future__ import annotations

import json
from pydantic import BaseModel
from src.document_concepts import DocumentConceptPlan, validate_document_concept_plan
from src.evidence import text_hash, warning_blocks_coverage
from src.source_map import SourceDocument
from src.source_packages import validate_source_package_plan
from .models import ProposalError, ProposalLimits, SourceConcern, SpanRef


def digest(value: object) -> str:
    return text_hash(json.dumps(value, sort_keys=True, separators=(',', ':')))


def _check_budget(source: SourceDocument, inventory: DocumentConceptPlan, limits: ProposalLimits) -> None:
    if len(inventory.regions) > limits.max_input_regions:
        raise ProposalError('INPUT_BUDGET_EXCEEDED')
    # Count before deep copying/serializing. Iterators avoid a speculative list
    # of every object. Private mutable evidence indexes are deliberately ignored.
    stack = [iter((source, inventory))]
    characters = records = 0
    while stack:
        try:
            value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        records += 1
        if isinstance(value, str):
            characters += len(value)
        elif isinstance(value, BaseModel):
            stack.append(iter(map(value.__getattribute__, type(value).model_fields)))
        elif isinstance(value, dict):
            stack.append(iter(value.values()))
        elif isinstance(value, (list, tuple)):
            stack.append(iter(value))
        if characters > limits.max_input_characters or records > limits.max_input_records:
            raise ProposalError('INPUT_BUDGET_EXCEEDED')


def pin_inputs(source: SourceDocument, inventory: DocumentConceptPlan, limits: ProposalLimits
               ) -> tuple[SourceDocument, DocumentConceptPlan, str, str]:
    """Validate pinned copies: existing validators update private derived indexes."""
    if not isinstance(source, SourceDocument) or not isinstance(inventory, DocumentConceptPlan):
        raise ProposalError('SOURCE_IDENTITY_INVALID')
    try:
        _check_budget(source, inventory, limits)
    except ProposalError:
        raise
    except (AttributeError, TypeError, KeyError):
        raise ProposalError('SOURCE_IDENTITY_INVALID') from None
    try:
        source = SourceDocument.model_validate(source.model_dump())
        inventory = DocumentConceptPlan.model_validate(inventory.model_dump())
        if source.evidence is None:
            raise ValueError()
        doc = source.evidence
        if source.buffer_id not in doc.buffers or source.page_count != len(doc.pages):
            raise ValueError()
        for section in source.sections:
            if (section.buffer_id != source.buffer_id or
                not 0 <= section.char_start <= section.char_end <= len(doc.buffers[source.buffer_id].text) or
                section.text != doc.buffers[source.buffer_id].text[section.char_start:section.char_end]):
                raise ValueError()
        validate_document_concept_plan(inventory, source)
        if source.source_package_plan is not None:
            validate_source_package_plan(source.source_package_plan, doc)
        if source.clinical_plan is not None:
            from src.concept_planner import validate_clinical_plan
            validate_clinical_plan(source.clinical_plan, doc)
        if source.multiview_selection is not None:
            from src.multiview import validate_multiview_selection
            validate_multiview_selection(source.multiview_selection, source)
        # Includes optional planning holds/warnings not included in M3's source
        # fingerprint. A modification changes proposal identity, never a cache.
        auxiliary = digest({
            'source_map_warnings': source.warnings,
            'source_packages': source.source_package_plan.model_dump(mode='json') if source.source_package_plan else None,
            'clinical_plan': source.clinical_plan.model_dump(mode='json') if source.clinical_plan else None,
            'multiview_selection': source.multiview_selection.model_dump(mode='json') if source.multiview_selection else None,
        })
        return source, inventory, digest(inventory.model_dump(mode='json')), auxiliary
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError):
        raise ProposalError('SOURCE_IDENTITY_INVALID') from None


def source_concerns(source: SourceDocument, inventory: DocumentConceptPlan) -> tuple[SourceConcern, ...]:
    records = []

    def add(kind, value, *, region_ids=(), pages=(), evidence=None, blocking=True):
        fingerprint = digest(value)
        records.append(SourceConcern(concern_id='concern:' + digest([kind, fingerprint, len(records)]),
            kind=kind, source_record_sha256=fingerprint, region_ids=tuple(region_ids),
            page_numbers=tuple(pages), evidence=SpanRef.from_evidence(evidence) if evidence else None,
            blocking=blocking))

    for warning in source.evidence.warnings:
        add('evidence_warning', warning.model_dump(mode='json'), pages=[warning.page_number] if warning.page_number else [],
            evidence=warning.span, blocking=warning_blocks_coverage(warning))
    for region in inventory.regions:
        if region.coverage == 'held_for_source_review':
            add('region_hold', region.model_dump(mode='json'), region_ids=[region.region_id],
                pages=region.page_numbers, evidence=region.ownership)
    for warning in source.warnings:
        add('source_map_warning', warning)
    if source.source_package_plan:
        for hold in source.source_package_plan.holds:
            add('package_hold', hold.model_dump(mode='json'), pages=[hold.page_number], evidence=hold.source_span,
                blocking=hold.reason != 'source_heading_or_label')
        for blocker in source.source_package_plan.blockers:
            add('package_blocker', blocker)
    if source.clinical_plan:
        plan = source.clinical_plan
        for blocker in plan.blockers:
            add('clinical_plan_hold', blocker)
        for concept in plan.concepts:
            if concept.generation_eligibility == 'blocked' or concept.block_reasons:
                add('clinical_plan_hold', concept.model_dump(mode='json'), pages=concept.page_numbers,
                    evidence=concept.ownership[0] if concept.ownership else None)
        by_id = {c.concept_id: c for c in plan.concepts}
        for unit in plan.units:
            if unit.generation_eligibility == 'blocked' or unit.block_reasons:
                pages = sorted({p for cid in unit.concept_ids + unit.context_ids for p in by_id[cid].page_numbers})
                add('clinical_plan_hold', unit.model_dump(mode='json'), pages=pages,
                    evidence=unit.ownership[0] if unit.ownership else None)
    if source.multiview_selection:
        selection = source.multiview_selection
        for blocker in selection.blockers + selection.alternate_plan.blockers:
            add('multiview_hold', blocker)
        for decision in selection.decisions:
            if decision.outcome in {'conflicting_evidence', 'unresolved_source_association',
                    'requires_extraction_repair', 'requires_source_review'}:
                add('multiview_hold', decision.model_dump(mode='json'))
        for warning in selection.alternate_evidence.warnings:
            add('multiview_hold', warning.model_dump(mode='json'), pages=[warning.page_number] if warning.page_number else [],
                evidence=warning.span, blocking=warning_blocks_coverage(warning))
    return tuple(records)
