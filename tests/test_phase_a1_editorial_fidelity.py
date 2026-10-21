"""Synthetic regression contracts; engineering evidence, not clinician labels."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from editorial_support import Provider, cases, fixture, response
from src.chunk_validator import validate_chunks
from src.claim_assessment import assess_editorial_chunk
from src.editorial_generation import generate_editorial_draft
from src.editorial_grounding import recommendation_metadata_equivalent
from src.providers.base import ChunkGenerationError


CASES = {case['id']: case for case in cases()}
CONTRACT = json.loads((Path(__file__).parent / 'fixtures/phase_a1_regression_contract.json').read_text(encoding='utf-8'))
ROWS = CONTRACT['rows']


def variant(case, name):
    if name == 'editorial_example':
        return case['editorial']
    if name == 'extractive_comparison':
        return case['source']
    attack = next(a for a in case['adversaries'] if a['id'] == name)
    return case['editorial'].replace(attack['old'], attack['new'])


@pytest.mark.parametrize('row', ROWS, ids=lambda r: r['case_id'] + '-' + r['variant'])
def test_entire_legacy_report_matches_protected_before_change_contract(row):
    case = CASES[row['case_id']]
    extraction, _, _ = fixture(case)
    generated = json.loads(response(case, variant(case, row['variant'])))
    generated['chunks'][0]['chunk_number'] = 1
    report = validate_chunks(generated, chunk_sources={1: [{'text': extraction['full_text'], 'review_required': True}]})
    actual = sha256(json.dumps(report, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Includes exact issue ordering/messages, source-evidence offsets and decisions.
    assert actual == row['legacy_report_sha256']


@pytest.mark.parametrize('row', ROWS, ids=lambda r: r['case_id'] + '-' + r['variant'])
def test_all_original_corpus_statuses_conflicts_and_review_boundaries_remain(row):
    case = CASES[row['case_id']]
    extraction, mapped, plan = fixture(case)
    original = deepcopy((extraction, mapped.model_dump(), plan.model_dump()))
    text = variant(case, row['variant'])
    provider = Provider(response(case, text))
    audit = {}
    draft = generate_editorial_draft(extraction, mapped, plan, provider, audit)
    report = draft.validation
    assert report['chunk_statuses'][0]['status'] == row['editorial_status']
    assert not report['clinically_approved'] and not report['document_complete']
    assert not report['qualified_clinician_reviewed']
    assert draft.generated['chunks'][0]['original_text'] == text
    assert len(provider.calls) == 1
    assert (extraction, mapped.model_dump(), plan.model_dump()) == original
    if row['expected_reason']:
        assessment = report['claim_assessments'][0]
        reasons = assessment['reasons'] + [r for c in assessment['claims'] for r in c['reasons']]
        assert row['expected_reason'] in reasons
    assert case['source'] not in json.dumps(audit) and text not in json.dumps(audit)


def test_complete_metadata_paraphrase_has_only_narrow_editorial_exception():
    case = CASES['conditional_guideline']
    extraction, mapped, plan = fixture(case)
    # Separate controlled example, not a change to the eight-case evaluation.
    text = case['source'].replace(
        'The recommendation is conditional, with low certainty of evidence.',
        'The recommendation remains conditional and is based on low certainty of evidence.')
    draft = generate_editorial_draft(extraction, mapped, plan, Provider(response(case, text)), {})
    report = draft.validation
    assert report['chunk_statuses'][0]['status'] == 'REVIEW_REQUIRED'
    assert any(i['rule'] == 'EDITORIAL_GROUNDING_REVIEW' and i['severity'] == 'WARNING' for i in report['issues'])
    assert not any(i['rule'] == 'SOURCE_GROUNDING' and i['severity'] == 'ERROR' for i in report['issues'])
    legacy = validate_chunks(draft.generated, source_text=extraction['full_text'])
    assert any(i['rule'] == 'SOURCE_GROUNDING' and i['severity'] == 'ERROR' for i in legacy['issues'])
    assert not report['clinically_approved']


@pytest.mark.parametrize('source,candidate', [
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation remains strong and is based on low certainty of evidence.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation remains conditional and is based on high certainty of evidence.'),
    ('The recommendation is not conditional, with low certainty of evidence.',
     'The recommendation remains conditional and is based on low certainty of evidence.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation is not conditional, with low certainty of evidence.'),
    ('The recommendation is conditional. The study has low certainty of evidence.',
     'The recommendation remains conditional and is based on low certainty of evidence.'),
    ('The recommendation is conditional, with low certainty of evidence except in children.',
     'The recommendation remains conditional and is based on low certainty of evidence.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation remains conditional and is based on low certainty of evidence in children.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation remains conditional and is based on low certainty of evidence; all adults should receive treatment.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation remains conditional and is based on low certainty of evidence after 12 weeks.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'The recommendation remains conditional and is based on low certainty of evidence for prevention of mortality.'),
    ('The recommendation is conditional, with low certainty of evidence.',
     'A different panel strongly recommends treatment alpha.'),
    ('', 'The recommendation remains conditional and is based on low certainty of evidence.'),
])
def test_narrow_equivalence_cannot_erase_clinical_differences_or_assemble_evidence(source, candidate):
    assert not recommendation_metadata_equivalent(candidate, source)


@pytest.mark.parametrize('replacement', [
    'The recommendation remains strong and is based on low certainty of evidence.',
    'The recommendation remains conditional and is based on high certainty of evidence.',
])
def test_metadata_change_does_not_bypass_independent_strength_or_certainty_checks(replacement):
    case = CASES['conditional_guideline']
    extraction, mapped, plan = fixture(case)
    text = case['source'].replace('The recommendation is conditional, with low certainty of evidence.', replacement)
    report = generate_editorial_draft(extraction, mapped, plan, Provider(response(case, text)), {}).validation
    assert report['chunk_statuses'][0]['status'] == 'REJECTED'
    assert not any(i['rule'] == 'EDITORIAL_GROUNDING_REVIEW' for i in report['issues'])


def test_matching_metadata_does_not_mask_additional_unsupported_recommendation():
    case = CASES['conditional_guideline']
    extraction, mapped, plan = fixture(case)
    text = case['source'].replace('The recommendation is conditional, with low certainty of evidence.',
        'The recommendation remains conditional and is based on low certainty of evidence.')
    text += ' All children should receive treatment alpha as an emergency cure.'
    report = generate_editorial_draft(extraction, mapped, plan, Provider(response(case, text)), {}).validation
    assert report['chunk_statuses'][0]['status'] == 'REJECTED'
    assert any(i['rule'] == 'EDITORIAL_GROUNDING_REVIEW' for i in report['issues'])
    assert any(i['rule'] == 'SOURCE_GROUNDING' and i['severity'] == 'ERROR' for i in report['issues'])


def test_literal_recommendation_negation_reversal_is_still_a_hard_error():
    from src.chunk_validator import _validate_editorial_source_grounding
    case = CASES['review_uncertainty']
    extraction, mapped, plan = fixture(case)
    text = case['source'].replace('The review did not recommend', 'The review did recommend')
    report = generate_editorial_draft(extraction, mapped, plan, Provider(response(case, text)), {}).validation
    assert report['chunk_statuses'][0]['status'] == 'REJECTED'
    assert any(i['rule'] == 'SOURCE_GROUNDING' and i['severity'] == 'ERROR' for i in report['issues'])
    findings = []
    _validate_editorial_source_grounding(1, text, extraction['full_text'], findings)
    assert any(i['severity'] == 'ERROR' and i['message'] ==
        'Recommendation negation conflicts with the same local source proposition.' for i in findings)


@pytest.mark.parametrize('case_id,text,reason', [
    ('randomized_primary', 'Children with condition delta received treatment alpha.', 'population_preservation_unverified'),
    ('randomized_primary', 'Treatment alpha reduced pain at 8 weeks.', 'outcome_preservation_unverified'),
    ('randomized_primary', 'Treatment alpha is equivalent to placebo for remission.', 'nonsignificance_does_not_establish_equivalence'),
    ('conditional_guideline', 'For adults with condition delta, the panel suggests treatment alpha.', 'limitation_preservation_unverified'),
    ('uncertain_dose', 'Incomplete outcome data prevented a quantitative estimate of benefit.', 'semantic_relationship_unverified'),
])
def test_unresolved_scope_qualifications_equivalence_and_causation_remain_visible(case_id, text, reason):
    case = CASES[case_id]
    extraction, mapped, plan = fixture(case)
    report = generate_editorial_draft(extraction, mapped, plan, Provider(response(case, text)), {}).validation
    assessment = report['claim_assessments'][0]
    reasons = assessment['reasons'] + [r for c in assessment['claims'] for r in c['reasons']]
    assert reason in reasons
    assert report['chunk_statuses'][0]['status'] in {'REVIEW_REQUIRED', 'REJECTED'}
    assert not report['clinically_approved']


def test_missing_owned_evidence_cannot_be_replaced_by_matching_metadata_context():
    case = CASES['conditional_guideline']
    _, mapped, plan = fixture(case)
    question = next(q for q in plan.concepts if q.status == 'source_bound')
    owned = [r.ownership for r in plan.regions if r.region_id in question.owned_region_ids]
    finding = assess_editorial_chunk(1, case['editorial'], mapped.evidence, [], owned, question.roles)
    assert finding.status == 'REJECTED'
    assert all(c.status == 'missing_evidence' for c in finding.claims)


def test_metadata_context_does_not_make_a_chunk_independent():
    case = CASES['conditional_guideline']
    extraction, mapped, plan = fixture(case)
    text = 'As mentioned above, the recommendation remains conditional and is based on low certainty of evidence.'
    report = generate_editorial_draft(extraction, mapped, plan, Provider(response(case, text)), {}).validation
    assert any(i['rule'] == 'INDEPENDENCE' for i in report['issues'])
    assert report['chunk_statuses'][0]['status'] == 'REJECTED'


def test_matching_metadata_never_repairs_a_citation_mismatch():
    case = CASES['conditional_guideline']
    extraction, mapped, plan = fixture(case)
    payload = json.loads(response(case))
    payload['chunks'][0]['reference'] = 'doi:10.1234/unrelated'
    with pytest.raises(ChunkGenerationError) as exc:
        generate_editorial_draft(extraction, mapped, plan, Provider(json.dumps(payload)), {})
    assert exc.value.code == 'REFERENCE_OR_CONTENT_INVALID'


def test_successful_legacy_candidate_reuses_checkpoint_across_validation_version_change(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from test_source_packages import fixture as package_fixture, response as package_response
    from src.chunk_generator import generate_chunks
    from src import package_generation

    extraction, mapped, plan = package_fixture(tmp_path)
    provider = Mock()
    provider.generate.return_value = package_response(extraction)
    directory = tmp_path / 'attempts'
    monkeypatch.setattr(package_generation, 'validation_fingerprint', lambda: '1' * 64)
    before = generate_chunks(extraction, source_doc=mapped, generation_plan=plan,
        provider=provider, diagnostics={}, attempt_directory=directory)
    assert provider.generate.call_count == 1
    files_before = {p.name: p.read_bytes() for p in directory.glob('*.json')}
    assert files_before and not any('hold' in name for name in files_before)

    monkeypatch.setattr(package_generation, 'validation_fingerprint', lambda: '2' * 64)
    provider.generate.reset_mock()
    provider.generate.side_effect = AssertionError('A compatible successful checkpoint must not call a provider.')
    audit = {}
    after = generate_chunks(extraction, source_doc=mapped, generation_plan=plan,
        provider=provider, diagnostics=audit, attempt_directory=directory)
    assert after == before
    assert not provider.generate.called and audit['units_resumed'] == 1
    assert {p.name: p.read_bytes() for p in directory.glob('*.json')} == files_before
