"""Uncertainty, duplicate/context arithmetic, failures and adversarial claims."""
from copy import deepcopy
import json
import sqlite3
import threading

import pytest

from clinical_proposals import propose_concepts,ProposalLimits
from coverage_reconciliation import reconcile_coverage,capture_generation_context,ReconciliationLimits,StageObservation,write_report
from coverage_support import run,again
from editorial_integration_support import BODY,GUIDELINE,REFERENCE,FakeProvider,prepared,response,execute
from proposal_support import CASES,fixture
from proposal_editorial import WorkflowConfig,WorkflowInterrupted,WorkflowError
from src.providers.base import ChunkGenerationError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*_,**__):
        pytest.fail('A4 must not use network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)


def test_one_region_multiple_proposals_not_multiple_facts(tmp_path):
    text=GUIDELINE+' Applicability was restricted to the study center.'
    report,*_=run(tmp_path,paragraphs=[text])
    assert report.counts['proposals']>=2
    assert report.counts['unique_owned_regions']==1
    assert report.counts['independent_clinical_facts'] is None
    assert sum(report.source_characters_by_state.values())==report.counts['canonical_characters']


def test_exact_duplicate_proposals_not_counted_twice(tmp_path):
    report,*_=run(tmp_path,paragraphs=[BODY,BODY])
    assert report.counts['proposals']==2
    assert report.counts['nonduplicate_proposal_units']==1
    assert report.counts['verified_stored_review_drafts']==1
    assert report.proposals[1].duplicate_of==report.proposals[0].proposal_id


def test_generated_duplicates_have_two_responses_one_row(tmp_path):
    report,*_=run(tmp_path,paragraphs=[BODY,BODY+' Study records remained available for review.'],
        provider=FakeProvider(lambda r,*_:response(r,BODY)))
    assert report.counts['verified_response_units']==2
    assert report.counts['known_eligible_candidate_events']==2
    assert report.counts['unique_reviewable_drafts']==1
    assert report.counts['verified_stored_review_drafts']==1


def test_reused_context_counts_union_once(tmp_path):
    report,inputs,plan,*_=run(tmp_path,paragraphs=CASES['adjacent_qualification'])
    usage=[c.region_id for p in plan.proposals for c in p.supporting_context]
    assert len(usage)>len(set(usage)),usage
    assert report.counts['context_link_characters']>report.counts['context_characters_union']
    assert report.counts['ownership_and_context_characters_union']<=report.counts['canonical_characters']


@pytest.mark.parametrize('case,cue',[('negative_result','primary_outcome'),('adverse_events','safety'),
    ('conditional_low_certainty','evidence_certainty'),('table',None),('damaged_passage',None)])
def test_held_important_or_structured_content_remains_visible(tmp_path,case,cue):
    report,*_=run(tmp_path,paragraphs=CASES[case])
    assert not report.clinically_complete and report.counts['verified_stored_review_drafts']==0
    assert any(r.source_state=='existing_source_hold' or r.proposal_ids for r in report.regions)
    if cue:
        assert any(cue in r.literal_concern_cues for r in report.regions)
        assert any(t.reason=='LITERAL_'+cue.upper()+'_COVERAGE_CONCERN' for t in report.review_tasks)
    if case=='table':
        assert any(t.reason=='UNVERIFIED_TABLE_FIGURE_RELATIONSHIP' for t in report.review_tasks)


def test_conditional_recommendation_and_uncertainty_not_approved(tmp_path):
    report,*_=run(tmp_path,paragraphs=[GUIDELINE])
    assert report.counts['verified_stored_review_drafts']==1
    assert any({'recommendation_strength','evidence_certainty'}<=set(r.literal_concern_cues) for r in report.regions)
    assert any({'conditional_recommendation','low_certainty_evidence'}<=set(r.literal_concern_cues) for r in report.regions)
    assert report.clinically_approved_chunks==0 and not report.clinically_complete


def test_missing_safety_proposal_visible_under_a2_budget(tmp_path):
    inputs=prepared([BODY,'Results\n\nAdverse events were assessed in adults with condition delta.'])
    plan=propose_concepts(inputs[1],inputs[2],limits=ProposalLimits(max_proposals=1))
    provider=FakeProvider()
    result=execute(tmp_path,inputs=inputs,provider=provider,proposal_plan=plan)
    report=again(tmp_path,inputs,plan,result,capture_generation_context(provider))
    assert any(not r.proposal_ids and 'safety' in r.literal_concern_cues for r in report.regions)
    assert any(t.reason=='LITERAL_SAFETY_COVERAGE_CONCERN' for t in report.review_tasks)


def test_unassessed_region_is_not_hidden_by_successful_draft(tmp_path):
    report,*_=run(tmp_path,paragraphs=[BODY,'Introduction\n\nThe report provides background material.'])
    assert report.counts['verified_stored_review_drafts']==1
    assert any(r.source_state=='not_yet_assessed' for r in report.regions)
    assert any(t.reason=='SOURCE_REGION_UNASSESSED' for t in report.review_tasks)
    assert not report.clinically_complete


def test_request_budget_not_service_stop(tmp_path):
    report,*_=run(tmp_path,paragraphs=[BODY,BODY.replace('alpha','beta')],config=WorkflowConfig(max_requests=1))
    assert report.proposals[1].generation=='request_budget_deferred'
    assert report.proposals[1].checkpoint=='not_scheduled'
    assert report.counts['generated_candidates']==1


@pytest.mark.parametrize('code',['AUTHENTICATION_FAILED','QUOTA_EXHAUSTED'])
def test_service_stop_retains_verified_success_and_unknown_counts(tmp_path,code):
    from src.bounded_scheduler import SubmissionControl
    control=SubmissionControl();started=threading.Event()
    def action(r,*_):
        order=json.loads(r.split('\n\n')[0])['source_order']
        if order==0:
            assert started.wait(5)
            raise ChunkGenerationError('CONFIDENTIAL raw provider error',code=code)
        started.set()
        assert control.stopped.wait(5)
        return response(r)
    report,*_=run(tmp_path,paragraphs=[BODY,BODY.replace('alpha','beta'),BODY.replace('alpha','gamma')],
        provider=FakeProvider(action),control=control)
    assert report.counts['verified_stored_review_drafts']==1
    assert report.proposals[0].generation=='provider_failure' and report.proposals[0].failure_code==code
    assert report.proposals[2].generation=='service_stop_deferred'
    assert report.counts['generated_candidates'] is None
    assert 'CONFIDENTIAL' not in report.model_dump_json()


def test_interrupted_run_keeps_known_successes(tmp_path):
    inputs=prepared([BODY,BODY.replace('alpha','beta'),BODY.replace('alpha','gamma')]);provider=FakeProvider()
    def progress(n):
        if n:
            raise KeyboardInterrupt()
    with pytest.raises(WorkflowInterrupted) as caught:
        execute(tmp_path,inputs=inputs,provider=provider,progress=progress)
    report=again(tmp_path,inputs,propose_concepts(inputs[1],inputs[2]),caught.value.result,capture_generation_context(provider))
    assert report.counts['verified_stored_review_drafts']==2
    assert not report.technical_reconciliation_complete
    assert next(s for s in report.stages if s.stage=='generation').state=='incomplete'


def test_unknown_committed_request_is_not_zero_generation(tmp_path):
    inputs=prepared();provider=FakeProvider(lambda *_:(_ for _ in ()).throw(RuntimeError('secret')))
    with pytest.raises(WorkflowError) as caught:
        execute(tmp_path,inputs=inputs,provider=provider)
    report=again(tmp_path,inputs,propose_concepts(inputs[1],inputs[2]),caught.value.result,capture_generation_context(provider))
    assert report.proposals[0].checkpoint=='request_outcome_unknown'
    assert report.proposals[0].generated_candidate_count is None
    assert report.counts['generated_candidates'] is None and not report.technical_reconciliation_complete


def test_cached_resume_still_zero_calls_and_same_draft_identity(tmp_path):
    first,inputs,plan,result,context=run(tmp_path)
    provider=FakeProvider()
    cached=execute(tmp_path,inputs=inputs,provider=provider)
    report=again(tmp_path,inputs,plan,cached,context)
    assert not provider.calls
    assert report.counts['resumed_units']==1
    assert report.proposals[0].chunk_id==first.proposals[0].chunk_id
    assert report.technical_reconciliation_complete,report


def test_stricter_policy_revocation_is_observed_without_writes(tmp_path,monkeypatch):
    from proposal_editorial import requests,validation
    _,inputs,plan,_,context=run(tmp_path)
    original=validation.assess_response
    def reject(*args,**kwargs):
        a=original(*args,**kwargs)
        return validation.Assessment(a.document,dict(a.report,status='FAILED'),'SOURCE_VALIDATION_FAILED')
    monkeypatch.setattr(requests,'validation_policy',lambda:'b'*64)
    monkeypatch.setattr(validation,'assess_response',reject)
    result=execute(tmp_path,inputs=inputs)
    db_before=(tmp_path/'review.sqlite3').read_bytes()
    report=again(tmp_path,inputs,plan,result,context)
    assert report.proposals[0].persistence=='eligibility_revoked',report
    assert report.counts['verified_stored_review_drafts']==0
    assert report.counts['revoked_storage_units']==1
    assert (tmp_path/'review.sqlite3').read_bytes()==db_before


def test_sqlite_readback_failure_not_verified(tmp_path,monkeypatch):
    from src import local_chunk_store
    monkeypatch.setattr(local_chunk_store,'verify_chunks_file_ingested',lambda *a,**kw:{'verified':False})
    report,*_=run(tmp_path)
    assert report.proposals[0].persistence=='readback_incomplete'
    assert report.counts['verified_stored_review_drafts']==0
    assert not report.technical_reconciliation_complete


@pytest.mark.parametrize('tamper',['source','proposal','workflow','approval','key','validation','draft'])
def test_stale_or_adversarial_inputs_never_claim_completion(tmp_path,tamper):
    _,inputs,plan,result,context=run(tmp_path)
    if tamper=='source':
        inputs[1].warnings.append('CONFIDENTIAL newly added source hold')
    elif tamper=='proposal':
        plan=plan.model_copy(update={'plan_id':'invented'})
    elif tamper=='workflow':
        result=result.model_copy(update={'units':()})
    elif tamper=='approval':
        result=result.model_copy(update={'clinically_approved':True})
    elif tamper=='key':
        result=result.model_copy(update={'units':(result.units[0].model_copy(update={'candidate_key':'a'*64}),)})
    elif tamper=='validation':
        result=result.model_copy(update={'units':(result.units[0].model_copy(update={'validation':{'status':'PASS'}}),)})
    elif tamper=='draft':
        draft=result.drafts[0].model_copy(update={'document':{'article':{},'chunks':[]}})
        result=result.model_copy(update={'drafts':(draft,)})
    report=again(tmp_path,inputs,plan,result,context)
    assert not report.technical_reconciliation_complete,report
    assert not report.clinically_complete and report.clinically_approved_chunks==0
    assert 'CONFIDENTIAL' not in report.model_dump_json()


@pytest.mark.parametrize('record',['response','request-intent','validation'])
@pytest.mark.parametrize('action',['missing','corrupt'])
def test_missing_or_corrupt_artifacts_are_unresolved(tmp_path,record,action):
    _,inputs,plan,result,context=run(tmp_path)
    if record=='validation':
        path=next((tmp_path/'editorial'/'units').glob('*/validation-*.json'))
    else:
        path=next((tmp_path/'editorial'/'units').glob('*/'+record+'.json'))
    if action=='missing':
        path.unlink()
    else:
        path.write_text('{corrupt confidential record')
    report=again(tmp_path,inputs,plan,result,context)
    assert not report.technical_reconciliation_complete
    assert report.counts['verified_stored_review_drafts']==0
    assert 'confidential' not in report.model_dump_json()


@pytest.mark.parametrize('limits',[ReconciliationLimits(max_regions=1),ReconciliationLimits(max_proposals=1),
    ReconciliationLimits(max_drafts=1),ReconciliationLimits(max_input_records=1),ReconciliationLimits(max_artifact_bytes=10)])
def test_limits_report_incomplete_not_silent_truncation(tmp_path,limits):
    _,inputs,plan,result,context=run(tmp_path,paragraphs=[BODY,BODY.replace('alpha','beta')])
    report=again(tmp_path,inputs,plan,result,context,limits=limits)
    assert not report.technical_reconciliation_complete
    assert report.verification_issues or any(p.verification_issues for p in report.proposals)


def test_storage_claim_without_row_never_promoted(tmp_path):
    _,inputs,plan,result,context=run(tmp_path)
    # An explicitly absent alternate disposable DB cannot substantiate the claim.
    report=again(tmp_path,inputs,plan,result,context,db_path=tmp_path/'absent.sqlite3')
    assert report.counts['verified_stored_review_drafts']==0
    assert report.proposals[0].persistence=='not_stored'
    assert not report.technical_reconciliation_complete
    assert not (tmp_path/'absent.sqlite3').exists()


def test_export_is_new_directory_only_and_rejects_tampered_report(tmp_path):
    report,*_=run(tmp_path)
    paths=write_report(report,tmp_path/'report')
    assert json.loads(paths[0].read_text())['clinically_complete'] is False
    with pytest.raises(ValueError):
        write_report(report,tmp_path/'report')
    with pytest.raises(ValueError):
        write_report(report.model_copy(update={'counts':{'verified_stored_review_drafts':999}}),tmp_path/'forged')
    assert not (tmp_path/'forged').exists()


def test_malformed_inputs_return_structured_unknown_result():
    for kwargs in ({'source':{'file_name':'SECRET.pdf'}},{'limits':{}},{'observations':({'code':'secret'},)}):
        report=reconcile_coverage(**kwargs)
        assert not report.technical_reconciliation_complete and report.counts['generated_candidates'] is None
        assert 'SECRET' not in report.model_dump_json()
