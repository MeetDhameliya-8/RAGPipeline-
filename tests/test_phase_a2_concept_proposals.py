"""Offline source-bound proposals: no providers, user PDFs or review fabrication."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from clinical_proposals import (ProposalError, ProposalLimits, ProposalPlan, SpanRef,
    propose_concepts, validate_proposal_plan)
from proposal_support import CASES, GUIDELINE, PRIMARY, TRIAL, fixture, text_for
from src.document_concepts import DocumentConceptPlan, build_document_concept_plan
from src.evidence import get_evidence, span, warn
from src.source_map import build_source_map


@pytest.mark.parametrize('name', CASES)
def test_synthetic_proposals_preserve_exact_source_uncertainty_accounting_and_inputs(name):
    extraction, mapped, inventory = fixture(CASES[name])
    before = deepcopy((extraction, mapped.model_dump(), inventory.model_dump(), mapped.evidence.__pydantic_private__))
    result = propose_concepts(mapped, inventory)
    assert result == propose_concepts(mapped, inventory)
    assert result == ProposalPlan.model_validate_json(result.model_dump_json())
    validate_proposal_plan(result, mapped, inventory)
    assert sum(c.value for c in result.region_counts) == len(inventory.regions)
    assert sum(c.value for c in result.canonical_character_counts) == len(extraction['full_text'])
    assert (extraction, mapped.model_dump(), inventory.model_dump(), mapped.evidence.__pydantic_private__) == before
    assert not result.document_complete and not result.clinically_approved and not result.generation_eligible
    assert [p.ownership[0].char_start for p in result.proposals] == sorted(p.ownership[0].char_start for p in result.proposals)
    by_id = {r.region_id: r for r in inventory.regions}
    for proposal in result.proposals:
        assert len(proposal.ownership) == 1
        assert proposal.ownership[0].to_evidence() == by_id[proposal.source_region_ids[0]].ownership
        assert not proposal.generation_eligible and not proposal.human_source_reviewed and not proposal.clinically_approved
        assert proposal.unresolved_relationships
        for role in proposal.potential_roles:
            assert role.relationship == 'unresolved' and role.basis == 'literal_cue_only'
            assert role.evidence.buffer_id == result.canonical_buffer_id
            assert proposal.ownership[0].char_start <= role.evidence.char_start < role.evidence.char_end <= proposal.ownership[0].char_end
        for context in proposal.supporting_context:
            assert context.relationship == 'unresolved'
            assert context.evidence.to_evidence() == by_id[context.region_id].ownership
        if by_id[proposal.source_region_ids[0]].coverage == 'held_for_source_review':
            assert proposal.status == 'blocked_source_evidence'
    assert not inventory.reviews
    assert all(q.relationship_basis == 'unresolved' for q in inventory.concepts)


def test_trial_primary_secondary_and_numbers_are_not_arm_mappings():
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    result = propose_concepts(mapped, inventory)
    primary = [p for p in result.proposals if p.category == 'primary_outcome']
    secondary = [p for p in result.proposals if p.category == 'secondary_outcome']
    assert primary and secondary
    assert any(text_for(mapped, p.ownership[0]).strip() == PRIMARY for p in primary)
    for p in primary + secondary:
        assert all(r.relationship == 'unresolved' for r in p.potential_roles)
        if any(r.role == 'numerical_finding' for r in p.potential_roles):
            assert 'numeric_associations_unresolved' in p.unresolved_relationships
    method_primary = next(p for p in primary if TRIAL in text_for(mapped, p.ownership[0]))
    assert 'multiple_endpoint_relationships_unresolved' in method_primary.unresolved_relationships
    assert 'not statistically significant' in text_for(mapped, next(p for p in primary if PRIMARY in text_for(mapped,p.ownership[0])).ownership[0])


def test_guideline_retains_conditional_low_certainty_and_exclusion_without_strengthening():
    _, mapped, inventory = fixture(CASES['conditional_low_certainty'])
    result = propose_concepts(mapped, inventory)
    p = next(p for p in result.proposals if p.category == 'recommendation')
    assert text_for(mapped, p.ownership[0]).strip() == GUIDELINE
    assert any(text_for(mapped, r.evidence) == 'conditional' for r in p.potential_roles)
    assert any('low certainty' in text_for(mapped, r.evidence) for r in p.potential_roles)
    assert p.status == 'needs_relationship_review'
    # Condition/treatment and included/excluded populations are distinct literal
    # candidates, not permission to choose one as a verified clinical label.
    assert p.candidate_topic is None and p.candidate_subtopic is None
    assert {'topic_label_unresolved', 'subtopic_label_unresolved'} <= set(p.unresolved_relationships)
    assert any(text_for(mapped, r.evidence) == 'treatment alpha' for r in p.potential_roles if r.role == 'topic')


def test_distinct_populations_remain_in_separate_ownership_and_labels():
    _, mapped, inventory = fixture(CASES['distinct_populations'])
    result = propose_concepts(mapped, inventory)
    assert len(result.proposals) == 2
    passages = [text_for(mapped, p.ownership[0]) for p in result.proposals]
    assert 'Adults' in passages[0] and 'Children' not in passages[0]
    assert 'Children' in passages[1] and 'Adults' not in passages[1]
    assert all(not p.supporting_context and not p.duplicate_of for p in result.proposals)


def test_explicit_nonadjacent_context_is_candidate_only_not_role_transfer():
    _, mapped, inventory = fixture(CASES['explicit_cross_page'])
    result = propose_concepts(mapped, inventory)
    p = next(p for p in result.proposals if p.category == 'primary_outcome' and p.page_numbers == (3,))
    assert p.supporting_context
    assert any(c.page_numbers == (1,) and c.kind == 'explicit_section_reference_candidate' for c in p.supporting_context)
    assert not any(c.page_numbers == (2,) for c in p.supporting_context)
    assert 'cross_page_relationship_unresolved' in p.unresolved_relationships
    assert all(r.evidence.char_start >= p.ownership[0].char_start for r in p.potential_roles)
    assert any('described in Methods' in text_for(mapped, link) for c in p.supporting_context for link in c.link_evidence)


def test_adjacent_qualification_is_retained_but_not_declared_semantically_linked():
    _, mapped, inventory = fixture(CASES['adjacent_qualification'])
    result = propose_concepts(mapped, inventory)
    p = next(p for p in result.proposals if PRIMARY in text_for(mapped, p.ownership[0]))
    assert any('delayed adverse events were not assessed' in text_for(mapped, c.evidence) for c in p.supporting_context)
    assert all(c.relationship == 'unresolved' for c in p.supporting_context)


def test_unproven_cross_page_referent_and_structured_results_remain_held():
    for name in ('unproven_cross_page', 'table'):
        _, mapped, inventory = fixture(CASES[name])
        result = propose_concepts(mapped, inventory)
        held_ids = {r.region_id for r in inventory.regions if r.coverage == 'held_for_source_review'}
        assert held_ids
        assert all(r.state == 'blocked_source_region' for r in result.regions if r.region_id in held_ids)
        assert all(p.status == 'blocked_source_evidence' for p in result.proposals if set(p.source_region_ids) & held_ids)


def test_duplicate_question_keeps_both_provenances_without_cross_population_deduplication():
    _, mapped, inventory = fixture(CASES['duplicate'])
    result = propose_concepts(mapped, inventory)
    assert len(result.proposals) == 2
    first, second = result.proposals
    assert second.status == 'duplicate_proposal' and second.duplicate_of == first.proposal_id
    assert first.ownership != second.ownership
    assert text_for(mapped, first.ownership[0]).strip() == text_for(mapped, second.ownership[0]).strip()
    _, mapped, inventory = fixture(['Methods\n\nAdults received treatment alpha.', 'Methods\n\nAdults received treatment alpha.'])
    assert not any(p.duplicate_of for p in propose_concepts(mapped, inventory).proposals)


def test_ambiguous_numbers_and_cited_study_do_not_create_relationship_assertions():
    for name, reason in [('ambiguous_numbers','numeric_associations_unresolved'),
                         ('cited_study','article_vs_cited_study_attribution_unresolved')]:
        _, mapped, inventory = fixture(CASES[name])
        result = propose_concepts(mapped, inventory)
        assert any(reason in p.unresolved_relationships for p in result.proposals)
        assert all(r.relationship == 'unresolved' for p in result.proposals for r in p.potential_roles)


@pytest.mark.parametrize('mutation', ['buffer', 'region', 'fingerprint', 'section', 'missing_evidence', 'pdf_identity'])
def test_corrupt_missing_or_stale_source_fails_closed_with_sanitized_code(mutation):
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    if mutation == 'buffer':
        mapped.evidence.buffers[mapped.evidence.canonical_buffer_id].text += ' PRIVATE_SECRET'
    elif mutation == 'region':
        inventory.regions.pop()
    elif mutation == 'fingerprint':
        inventory.evidence_fingerprint = 'b' * 64
    elif mutation == 'section':
        mapped.sections[0].text += ' PRIVATE_SECRET'
    elif mutation == 'missing_evidence':
        mapped.evidence = None
    else:
        mapped.evidence.pdf_sha256 = 'b' * 64
    with pytest.raises(ProposalError) as exc:
        propose_concepts(mapped, inventory)
    assert exc.value.code == 'SOURCE_IDENTITY_INVALID'
    assert str(exc.value) == 'SOURCE_IDENTITY_INVALID'
    assert 'PRIVATE' not in str(exc.value)


def test_changed_source_and_saved_plan_tampering_rejected_by_reconstruction():
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    p = propose_concepts(mapped, inventory)
    _, changed, new_inventory = fixture([CASES['two_arm_trial'][0].replace('8 weeks', '9 weeks')])
    assert propose_concepts(changed, new_inventory).plan_id != p.plan_id
    with pytest.raises(ProposalError):
        validate_proposal_plan(p, changed, new_inventory)
    forged = p.model_copy(update={'proposals': ()})
    with pytest.raises(ProposalError) as exc:
        validate_proposal_plan(forged, mapped, inventory)
    assert exc.value.code == 'PROPOSAL_IDENTITY_INVALID'


@pytest.mark.parametrize('limits', [ProposalLimits(max_context_characters=10), ProposalLimits(max_context_regions=0)])
def test_context_budget_overflow_holds_whole_candidate_and_reports_omission(limits):
    _, mapped, inventory = fixture(CASES['explicit_cross_page'])
    result = propose_concepts(mapped, inventory, limits=limits)
    p = next(p for p in result.proposals if p.category == 'primary_outcome' and p.page_numbers == (3,))
    assert p.status == 'blocked_source_evidence'
    assert p.context_overflow and p.context_overflow.candidate_regions > 0
    assert not p.supporting_context
    assert 'context_budget_exceeded' in p.unresolved_relationships
    assert text_for(mapped, p.ownership[0]).strip().startswith(PRIMARY)
    assert not result.document_complete


def test_count_region_role_and_input_budgets_are_visible_and_bounded(monkeypatch):
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    small = propose_concepts(mapped, inventory, limits=ProposalLimits(max_proposals=1))
    assert len(small.proposals) == 1
    assert dict((n.name,n.value) for n in small.diagnostics)['omitted_questions_by_limit'] > 0
    assert any('proposal_count_budget_exceeded' in r.reasons for r in small.regions)
    for limits, reason in [(ProposalLimits(max_region_characters=10), 'region_character_budget_exceeded'),
                           (ProposalLimits(max_role_candidates=1), 'role_candidate_budget_exceeded')]:
        p = propose_concepts(mapped, inventory, limits=limits)
        assert any(reason in q.unresolved_relationships and q.status == 'blocked_source_evidence' for q in p.proposals)
        assert all(len(q.potential_roles) <= limits.max_role_candidates for q in p.proposals)
    from clinical_proposals import source_adapter
    monkeypatch.setattr(source_adapter, 'validate_document_concept_plan', Mock(side_effect=AssertionError('Budget must precede validation.')))
    for limits in (ProposalLimits(max_input_characters=1), ProposalLimits(max_input_regions=1), ProposalLimits(max_input_records=1)):
        with pytest.raises(ProposalError) as exc:
            propose_concepts(mapped, inventory, limits=limits)
        assert exc.value.code == 'INPUT_BUDGET_EXCEEDED'


def test_every_warning_empty_region_and_package_hold_remains_visible_without_text(capsys):
    from src.source_packages import build_source_package_plan
    extraction, mapped, _ = fixture(CASES['two_arm_trial'], name='CONFIDENTIAL-PERSON.pdf')
    doc = mapped.evidence
    warn(doc, 'existing_extraction_blocker', 'PRIVATE_SOURCE_PASSAGE_DO_NOT_LOG', page_number=1, critical=True)
    inventory = build_document_concept_plan({'full_text': doc.buffers[doc.canonical_buffer_id].text}, mapped)
    mapped.source_package_plan = build_source_package_plan(extraction, mapped)
    result = propose_concepts(mapped, inventory)
    assert len([c for c in result.source_concerns if c.kind == 'evidence_warning']) == len(doc.warnings)
    assert len([c for c in result.source_concerns if c.kind == 'package_hold']) == len(mapped.source_package_plan.holds)
    assert any(c.kind == 'region_hold' for c in result.source_concerns)
    assert all(p.status == 'blocked_source_evidence' for p in result.proposals)
    encoded = result.model_dump_json()
    assert 'PRIVATE_SOURCE_PASSAGE' not in encoded and 'CONFIDENTIAL' not in encoded and TRIAL not in encoded
    assert not capsys.readouterr().out
    _, mapped, inventory = fixture(CASES['empty'])
    result = propose_concepts(mapped, inventory)
    assert not result.proposals and result.source_concerns
    assert any(r.state == 'blocked_source_region' for r in result.regions)


def test_auxiliary_hold_revision_changes_identity_without_mutating_inventory():
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    before = propose_concepts(mapped, inventory)
    mapped.warnings.append('PRIVATE_CONTEXT_WARNING')
    after = propose_concepts(mapped, inventory)
    assert before.evidence_fingerprint == after.evidence_fingerprint
    assert before.auxiliary_source_sha256 != after.auxiliary_source_sha256 and before.plan_id != after.plan_id
    assert all(p.status == 'blocked_source_evidence' for p in after.proposals)
    assert 'PRIVATE_CONTEXT_WARNING' not in after.model_dump_json()
    with pytest.raises(ProposalError):
        validate_proposal_plan(before, mapped, inventory)


def test_no_human_review_or_generation_adapter_and_no_approval_mutation():
    from src.editorial_generation import generate_editorial_draft
    from src.providers.base import ChunkGenerationError
    extraction, mapped, inventory = fixture(CASES['two_arm_trial'])
    result = propose_concepts(mapped, inventory)
    assert not inventory.reviews and not inventory.exclusions
    payload = result.model_dump(mode='json')
    assert 'reviews' not in payload and 'review_id' not in result.model_dump_json()
    with pytest.raises(ValidationError):
        ProposalPlan.model_validate(dict(payload, clinically_approved=True))
    with pytest.raises(ValidationError):
        DocumentConceptPlan.model_validate(payload)
    provider = Mock()
    with pytest.raises((ValueError, ChunkGenerationError)):
        generate_editorial_draft(extraction, mapped, result, provider, {})
    assert not provider.generate.called
    with pytest.raises(ValidationError):
        result.proposals[0].generation_eligible = True
    with pytest.raises(ValidationError):
        result.proposals[0].ownership[0].char_start = 4
    with pytest.raises(ValidationError):
        SpanRef(buffer_id='synthetic', char_start=4, char_end=2)


def test_reference_and_unassessed_regions_not_counted_as_complete_clinical_coverage():
    for name in ('references', 'unclassified'):
        extraction, mapped, inventory = fixture(CASES[name])
        result = propose_concepts(mapped, inventory)
        assert sum(c.value for c in result.canonical_character_counts) == len(extraction['full_text'])
        assert not result.document_complete
        expected = 'excluded_or_nonclinical' if name == 'references' else 'not_yet_assessed'
        assert any(r.state == expected for r in result.regions)


def test_fingerprint_and_legacy_source_remain_outside_proposal_package():
    from src.package_generation import validation_fingerprint
    root = Path(__file__).resolve().parents[1]
    before = validation_fingerprint()
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    propose_concepts(mapped, inventory)
    assert validation_fingerprint() == before
    import src.editorial_generation as editorial
    import src.pipeline as pipeline
    assert 'clinical_proposals' not in Path(editorial.__file__).read_text(encoding='utf-8')
    assert 'clinical_proposals' not in Path(pipeline.__file__).read_text(encoding='utf-8')
    assert (root/'clinical_proposals/planner.py').is_file()


@pytest.mark.parametrize('pages', [
    ['Results\n\nThe primary outcome was remission; population definitions are described in Methods.'],
    ['Methods\n\nAdults received treatment alpha.', 'Methods\n\nChildren received treatment beta.',
     'Results\n\nThe primary outcome was remission; population definitions are described in Methods.'],
])
def test_missing_or_ambiguous_explicit_context_is_never_chosen_by_proximity(pages):
    _, mapped, inventory = fixture(pages)
    result = propose_concepts(mapped, inventory)
    p = next(p for p in result.proposals if p.category == 'primary_outcome')
    assert 'missing_or_ambiguous_explicit_context_reference' in p.unresolved_relationships
    assert not any(c.kind == 'explicit_section_reference_candidate' for c in p.supporting_context)
    assert not p.generation_eligible


def test_concern_link_budget_does_not_drop_document_source_holds():
    _, mapped, inventory = fixture(CASES['two_arm_trial'])
    mapped.warnings.extend(['synthetic concern one', 'synthetic concern two', 'synthetic concern three'])
    result = propose_concepts(mapped, inventory, limits=ProposalLimits(max_concern_links=1))
    assert len(result.source_concerns) == 3
    assert all(p.status == 'blocked_source_evidence' and p.concern_overflow_count == 3
               and not p.concern_ids and 'concern_link_budget_exceeded' in p.unresolved_relationships for p in result.proposals)


def test_optional_existing_clinical_plan_holds_are_validated_and_preserved():
    from src.concept_planner import build_clinical_plan
    extraction, mapped, inventory = fixture(CASES['two_arm_trial'])
    mapped.clinical_plan = build_clinical_plan(extraction, mapped)
    result = propose_concepts(mapped, inventory)
    holds = [c for c in mapped.clinical_plan.concepts if c.generation_eligibility == 'blocked' or c.block_reasons]
    if holds:
        assert any(c.kind == 'clinical_plan_hold' for c in result.source_concerns)
    mapped.clinical_plan.evidence_config_hash = 'b' * 64
    with pytest.raises(ProposalError) as exc:
        propose_concepts(mapped, inventory)
    assert exc.value.code == 'SOURCE_IDENTITY_INVALID'
