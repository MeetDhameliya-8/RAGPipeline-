"""Offline proposal-to-editorial-to-existing-SQLite boundary tests."""
import json
import sqlite3
from pathlib import Path

import pytest

from clinical_proposals import propose_concepts
from proposal_editorial import WorkflowConfig, WorkflowError
from proposal_editorial.eligibility import assess_eligibility
from editorial_integration_support import BODY, GUIDELINE, FakeProvider, prepared, response, execute


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import socket
    def forbidden(*_,**__):
        pytest.fail('No network connections are allowed in A3 tests')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)


def eligible(inputs, config=None):
    _,source,inventory = inputs
    return assess_eligibility(source,propose_concepts(source,inventory),config or WorkflowConfig())


def test_valid_review_draft_persisted_with_exact_readback(tmp_path):
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider)
    assert len(provider.calls) == 1, result
    assert len(result.drafts) == 1, result
    unit = next(u for u in result.units if u.status == 'stored_review')
    assert unit.storage['readback_verified']
    assert not result.document_complete and not result.clinically_approved and not result.ingestion_allowed
    assert result.drafts[0].validation['status'] == 'REVIEW_REQUIRED'
    chunk = result.drafts[0].document['chunks'][0]
    assert set(chunk) == {'topic','subtopic','section','reference','original_text','chunk_number'}
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        row = conn.execute('select original_text,validation_status,review_status from chunks').fetchone()
    assert row[0] == chunk['original_text'] and row[1] == 'REVIEW_REQUIRED'
    assert len(list((tmp_path/'editorial'/'units').glob('*/response.json'))) == 1


@pytest.mark.parametrize('text',[GUIDELINE,BODY])
def test_smoke_eligibility(text):
    values = eligible(prepared([text]))
    assert any(e.state == 'draftable_review' for e in values),values


@pytest.mark.parametrize('case',['two_arm_trial','negative_result','ambiguous_numbers','table','explicit_cross_page','adjacent_qualification','damaged_passage','cited_study','orphan_numbers','empty'])
def test_unsafe_relationships_never_call_provider(tmp_path,case):
    from proposal_support import CASES
    provider = FakeProvider()
    inputs = prepared(CASES[case])
    result = execute(tmp_path,inputs=inputs,provider=provider)
    assert not provider.calls
    assert not result.drafts and not (tmp_path/'review.sqlite3').exists()
    assert result.diagnostics['source_proposal_gaps'] or not result.units


@pytest.mark.parametrize('change,code',[
    ({'reference':'doi:10.9999/invented'},'REFERENCE_OR_CONTENT_INVALID'),
    ({'original_text':BODY+' Remission was 99%.'},'SOURCE_VALIDATION_FAILED'),
    ({'original_text':BODY.replace('Adults','Children')},'SOURCE_VALIDATION_FAILED'),
    ({'topic':'Unrelated medical procedure'},'SOURCE_VALIDATION_FAILED'),
])
def test_adversarial_candidate_quarantined_not_stored(tmp_path,change,code):
    result = execute(tmp_path,provider=FakeProvider(lambda r,*_:response(r,**change)))
    unit = next(u for u in result.units if u.candidate_key)
    assert unit.status == 'quarantined' and unit.code == code, unit
    assert not result.drafts and not (tmp_path/'review.sqlite3').exists()
    assert list((tmp_path/'editorial'/'units').glob('*/response.json'))
    assert list((tmp_path/'editorial'/'units').glob('*/validation-*.json'))


def test_conditional_low_certainty_recommendation_retained(tmp_path):
    result = execute(tmp_path,inputs=prepared([GUIDELINE]))
    assert result.drafts, result
    chunk = result.drafts[0].document['chunks'][0]
    assert 'conditional' in chunk['original_text'] and 'low certainty' in chunk['original_text']
    assert result.drafts[0].validation['status'] == 'REVIEW_REQUIRED'


def test_supported_paraphrase_is_preserved_without_post_generation_rewriting(tmp_path):
    paraphrase = ('Adults with condition delta received treatment alpha according to the study protocol. '
        'Scheduled clinic assessments used a consistent questionnaire, and study personnel documented medication use on the designated forms. '
        'The clinical team reviewed participant diaries alongside the visit assessments. '
        'The diaries were collected at the study center and retained by the investigators for later review. '
        'Each assessment record included its date and location. '
        'Concomitant medication use was recorded as part of this documentation. '
        'Clinical assessments and diary entries were retained as source records. '
        'The documented protocol describes the collection, review and retention of these records during scheduled visits '
        'for adults with condition delta receiving treatment alpha.')
    assert 100 <= len(paraphrase.split()) <= 200 and paraphrase != BODY
    result = execute(tmp_path,provider=FakeProvider(lambda r,*_:response(r,paraphrase)))
    assert result.drafts,result
    assert result.drafts[0].validation['status'] == 'REVIEW_REQUIRED'
    assert result.drafts[0].document['chunks'][0]['original_text'] == paraphrase


@pytest.mark.parametrize('text',[
    GUIDELINE.replace('suggests','recommends').replace('conditional','strong'),
    GUIDELINE.replace('low certainty','high certainty'),
])
def test_recommendation_strengthening_or_certainty_rejected(tmp_path,text):
    result = execute(tmp_path,inputs=prepared([GUIDELINE]),provider=FakeProvider(lambda r,*_:response(r,text)))
    assert any(u.status == 'quarantined' for u in result.units),result
    assert not result.drafts


def test_missing_population_is_review_required_not_fabricated(tmp_path):
    result = execute(tmp_path,provider=FakeProvider(lambda r,*_:response(r,BODY.replace('Adults with condition delta','The study population'))))
    assert result.drafts, result
    claim = result.drafts[0].validation['claim_assessments'][0]
    assert claim['status'] == 'REVIEW_REQUIRED' and claim['reasons']
    assert 'Adults' not in result.drafts[0].document['chunks'][0]['original_text']


def test_duplicate_proposal_and_budget_deferred(tmp_path):
    provider = FakeProvider()
    result = execute(tmp_path,inputs=prepared([BODY,BODY]),provider=provider)
    assert len(provider.calls) == 1
    assert any(e.state == 'duplicate_deferred' for e in result.eligibility)
    result = execute(tmp_path,workspace=tmp_path/'b',inputs=prepared([BODY,BODY.replace('alpha','beta')]),
        config=WorkflowConfig(max_requests=1))
    assert sum(e.state == 'draftable_review' for e in result.eligibility) == 1
    assert any('generation_request_budget_exceeded' in e.reasons for e in result.eligibility)


def test_no_arbitrary_source_truncation(tmp_path):
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider,config=WorkflowConfig(max_unit_characters=200))
    assert not provider.calls
    assert any('source_context_budget_exceeded' in e.reasons for e in result.eligibility)


def test_stale_plan_and_source_mutation_fail_before_network(tmp_path):
    inputs = prepared()
    plan = propose_concepts(inputs[1],inputs[2])
    changed = plan.model_copy(update={'plan_id':'stale'})
    provider = FakeProvider()
    with pytest.raises(WorkflowError):
        execute(tmp_path,inputs=inputs,provider=provider,proposal_plan=changed)
    inputs[0]['full_text'] += ' tampered'
    with pytest.raises(WorkflowError):
        execute(tmp_path,inputs=inputs,provider=provider)
    assert not provider.calls


def test_successful_resume_zero_calls_and_stable_id(tmp_path):
    first = execute(tmp_path)
    provider = FakeProvider(lambda *_:pytest.fail('Compatible resume must not call provider'))
    second = execute(tmp_path,provider=provider)
    assert not provider.calls and second.diagnostics['resumed_units'] == 1
    assert first.drafts == second.drafts
    assert [u.storage for u in second.units if u.storage][0]['unchanged'] == 1
    assert second.diagnostics['observability']['request_totals']['logical_calls'] == 0
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select count(*) from chunks').fetchone()[0] == 1


def test_held_decision_revalidated_after_policy_change_without_network(tmp_path,monkeypatch):
    from proposal_editorial import requests, validation
    first = execute(tmp_path,provider=FakeProvider(lambda r,*_:response(r,reference='doi:10.9999/invented')))
    key = next(u.candidate_key for u in first.units if u.candidate_key)
    monkeypatch.setattr(requests,'validation_policy',lambda:'new-policy')
    spy = []
    original = validation.assess_response
    def assess(*args,**kw):
        spy.append(kw['resumed'])
        return original(*args,**kw)
    monkeypatch.setattr(validation,'assess_response',assess)
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider)
    assert spy == [True] and not provider.calls
    assert result.diagnostics['observability']['counts']['validation_policy_reconsidered_units'] == 1
    assert result.diagnostics['validation_rejections'] == 1
    assert (tmp_path/'editorial'/'units'/key/'validation-new-policy.json').exists()


def test_rolling_refill_out_of_order_and_two_request_limit(tmp_path):
    import threading
    third = threading.Event()
    completed = []
    def action(request,*_):
        order = json.loads(request.split('\n\n')[0])['source_order']
        if order == 0:
            assert third.wait(5),'Scheduler did not refill while first request was waiting'
        elif order == 2:
            third.set()
        completed.append(order)
        return response(request)
    provider = FakeProvider(action)
    result = execute(tmp_path,inputs=prepared([BODY,BODY.replace('alpha','beta'),BODY.replace('alpha','gamma')]),provider=provider)
    assert provider.high_water == 2
    assert result.diagnostics['scheduler']['max_in_flight'] == 2
    assert completed.index(2) < completed.index(0)
    assert [d.source_order for d in result.drafts] == [0,1,2]
    assert len(result.drafts) == 3,result


def test_transient_timeout_retry_only_failed_unit(tmp_path):
    from src.providers.base import ChunkGenerationError
    def action(r,*_):
        if json.loads(r.split('\n\n')[0])['source_order'] == 1:
            raise ChunkGenerationError('SECRET provider message',code='TRANSPORT_TIMEOUT')
        return response(r)
    inputs = prepared([BODY,BODY.replace('alpha','beta')])
    first = execute(tmp_path,inputs=inputs,provider=FakeProvider(action))
    assert len(first.drafts) == 1 and not first.document_complete
    assert any(u.code == 'TRANSPORT_TIMEOUT' for u in first.units)
    provider = FakeProvider()
    second = execute(tmp_path,inputs=inputs,provider=provider)
    assert len(provider.calls) == 1 and len(second.drafts) == 2
    assert second.diagnostics['resumed_units'] == 1


@pytest.mark.parametrize('code',['AUTHENTICATION_FAILED','QUOTA_EXHAUSTED'])
def test_auth_quota_stop_and_drain_existing_success(tmp_path,code):
    import threading
    from src.providers.base import ChunkGenerationError
    started = threading.Event()
    def action(r,*_):
        order = json.loads(r.split('\n\n')[0])['source_order']
        if order == 0:
            assert started.wait(5)
            raise ChunkGenerationError('secret-key/confidential patient',code=code)
        started.set()
        import time
        time.sleep(.03)
        return response(r)
    provider = FakeProvider(action)
    inputs = prepared([BODY,BODY.replace('alpha','beta'),BODY.replace('alpha','gamma')])
    result = execute(tmp_path,inputs=inputs,provider=provider)
    assert len(provider.calls) == 2 and len(result.drafts) == 1
    assert result.diagnostics['scheduler']['submission_stopped']
    assert result.units[2].status == 'not_submitted'
    assert any(u.code == code for u in result.units)


def test_interruption_drains_and_saves_then_resumes(tmp_path):
    from proposal_editorial import WorkflowInterrupted
    def progress(n):
        if n:
            raise KeyboardInterrupt()
    inputs = prepared([BODY,BODY.replace('alpha','beta'),BODY.replace('alpha','gamma')])
    with pytest.raises(WorkflowInterrupted) as caught:
        execute(tmp_path,inputs=inputs,progress=progress)
    assert caught.value.result.status == 'INTERRUPTED'
    assert len(caught.value.result.drafts) == 2
    provider = FakeProvider()
    result = execute(tmp_path,inputs=inputs,provider=provider)
    assert len(result.drafts) == 3 and len(provider.calls) == 1


def test_unexpected_exception_not_silently_discarded_and_redacted(tmp_path):
    def action(*_):
        raise RuntimeError('patient name secret-key sensitive-filename.pdf')
    with pytest.raises(WorkflowError) as caught:
        execute(tmp_path,provider=FakeProvider(action))
    assert caught.value.result.units[0].status == 'unexpected_failure'
    serialized = str(caught.value)+json.dumps(caught.value.result.diagnostics)
    assert all(s not in serialized for s in ('patient name','secret-key','sensitive-filename','synthetic-secret-patient'))
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider)
    assert not provider.calls and result.units[0].status == 'unknown_request_outcome'
    result = execute(tmp_path,provider=provider,config=WorkflowConfig(retry_uncertain_requests=True))
    assert len(provider.calls) == 1 and len(result.drafts) == 1


@pytest.mark.parametrize('raw',['not json','{}','{"article":{},"chunks":[]}'])
def test_malformed_candidates_saved_but_never_reviewed(tmp_path,raw):
    result = execute(tmp_path,provider=FakeProvider(lambda *_:raw))
    assert not result.drafts and not (tmp_path/'review.sqlite3').exists()
    assert result.units[0].status == 'quarantined'


def test_no_fabricated_attestation_or_production_path(tmp_path,monkeypatch):
    import src.document_concepts as contracts
    def prohibited(*_,**__):
        pytest.fail('Human review object was fabricated')
    monkeypatch.setattr(contracts,'ReviewedQuestion',prohibited)
    monkeypatch.setattr(contracts,'SourceReview',prohibited)
    result = execute(tmp_path)
    assert result.drafts
    assert all(not e.human_source_reviewed and not e.clinically_approved for e in result.eligibility)
    assert not result.ingestion_allowed
    assert not any('production' in p.name and p.suffix == '.sqlite3' for p in tmp_path.rglob('*'))


def test_modified_checkpoint_fails_closed_without_new_call(tmp_path):
    execute(tmp_path)
    checkpoint = next((tmp_path/'editorial'/'units').glob('*/response.json'))
    record = json.loads(checkpoint.read_text())
    record['raw'] = 'corrupted'
    checkpoint.write_text(json.dumps(record))
    provider = FakeProvider()
    with pytest.raises(WorkflowError) as caught:
        execute(tmp_path,provider=provider)
    assert not provider.calls and not caught.value.result.drafts


def test_candidate_identity_changes_for_prompt_or_provider(tmp_path,monkeypatch):
    from proposal_editorial import requests
    first = execute(tmp_path)
    provider = FakeProvider()
    provider.generation_identity = {'provider':'offline-fixture','model':'fictional-v2'}
    second = execute(tmp_path,provider=provider)
    assert len(provider.calls) == 1
    assert first.units[0].candidate_key != second.units[0].candidate_key
    monkeypatch.setattr(requests,'editorial_prompt',lambda:requests.EDITORIAL_PROMPT.read_text(encoding='utf-8')+'\nSynthetic contract revision')
    third = execute(tmp_path)
    assert third.units[0].candidate_key != first.units[0].candidate_key


def test_workspace_lock_and_foreign_directory_protection(tmp_path):
    from proposal_editorial.checkpoints import workspace_lock
    root = tmp_path/'owned'
    with workspace_lock(root):
        with pytest.raises(WorkflowError):
            execute(tmp_path,workspace=root)
    foreign = tmp_path/'editorial'
    foreign.mkdir()
    (foreign/'historical.txt').write_text('preserve')
    provider = FakeProvider()
    with pytest.raises(WorkflowError):
        execute(tmp_path,provider=provider)
    assert not provider.calls and (foreign/'historical.txt').read_text() == 'preserve'


def test_known_http_attempts_not_conflated_with_logical_calls(tmp_path):
    from src import diagnostics
    def action(r,*_):
        diagnostics.http_attempt(started=True)
        diagnostics.http_attempt(succeeded=False,http_status=429)
        diagnostics.http_attempt(started=True)
        diagnostics.http_attempt(succeeded=True,http_status=200,usage={'prompt_tokens':51,'completion_tokens':111})
        return response(r)
    result = execute(tmp_path,provider=FakeProvider(action))
    totals = result.diagnostics['observability']['request_totals']
    assert totals['logical_calls'] == 1 and totals['http_attempts_observed'] == 2
    assert totals['http_failures'] == 1 and totals['http_successes'] == 1
    assert totals['unknown_usage_attempts'] == 1


def test_generation_abstention_retained_as_source_hold(tmp_path):
    def action(r,*_):
        value = json.loads(response(r))
        value['article']['number_of_chunks'] = 0
        value['chunks'] = []
        return json.dumps(value)
    result = execute(tmp_path,provider=FakeProvider(action))
    assert result.units[0].status == 'source_hold' and not result.drafts
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider)
    assert not provider.calls and result.units[0].resumed


def test_cancelled_before_submission_never_calls_provider(tmp_path):
    from src.bounded_scheduler import SubmissionControl
    control = SubmissionControl()
    control.stop()
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider,control=control)
    assert not provider.calls and result.units[0].status == 'not_submitted'


def test_unexpected_progress_exception_propagated_after_saving(tmp_path):
    def progress(n):
        if n:
            raise RuntimeError('secret progress callback contents')
    with pytest.raises(WorkflowError) as caught:
        execute(tmp_path,progress=progress)
    assert caught.value.result.status == 'FAILED_WITH_RECOVERABLE_CANDIDATES'
    assert caught.value.result.drafts
    assert 'secret progress' not in json.dumps(caught.value.result.diagnostics)


def test_request_contains_selected_evidence_only_and_no_attestation(tmp_path):
    inputs = prepared([BODY,'Background\n\nUnrelated confidential background passage.'])
    provider = FakeProvider()
    result = execute(tmp_path,inputs=inputs,provider=provider)
    assert result.drafts and len(provider.calls) == 1
    request = provider.calls[0]
    assert 'Unrelated confidential background' not in request
    header = json.loads(request.split('\n\n')[0])
    assert not header['human_source_reviewed'] and not header['clinically_approved']
    assert all(r['basis'] == 'literal_consistency_only' for r in header['literal_scope_candidates'])


def test_cross_page_context_remains_held(tmp_path):
    from proposal_support import fixture,TRIAL,PRIMARY
    from editorial_integration_support import REFERENCE
    inputs = fixture(['Methods\n\n'+TRIAL,'Results\n\n'+PRIMARY+' Population definitions are described in Methods.'])
    inputs[0].update(reference_source_text=REFERENCE,reference_metadata={'doi':REFERENCE[4:],'reference':REFERENCE})
    provider = FakeProvider()
    result = execute(tmp_path,inputs=inputs,provider=provider)
    assert not provider.calls and all(e.state != 'draftable_review' for e in result.eligibility)
