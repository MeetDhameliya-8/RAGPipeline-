"""Read-only A4 ledgers against actual fictional A2/A3 artifacts."""
import json
import sqlite3
import pytest
from coverage_support import run,again
from editorial_integration_support import BODY,GUIDELINE,FakeProvider,response,execute
from coverage_reconciliation import reconcile_coverage,ReconciliationLimits,StageObservation


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*_,**__):
        pytest.fail('A4 must not use network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)


def test_one_proposal_one_verified_row_and_no_approval(tmp_path):
    report,*_=run(tmp_path)
    assert report.technical_reconciliation_complete,report
    assert report.counts['verified_stored_review_drafts']==1
    assert report.counts['generated_candidates']==1
    assert not report.clinically_complete and report.clinically_approved_chunks==0
    assert not report.production_ingestion_allowed
    assert any(t.reason=='CLINICAL_PARAPHRASE_FIDELITY_REVIEW' for t in report.review_tasks)


def test_deterministic_readonly_reconciliation(tmp_path):
    report,inputs,plan,result,context=run(tmp_path)
    before={p:p.read_bytes() for p in (tmp_path/'editorial').rglob('*') if p.is_file()}
    db_before=(tmp_path/'review.sqlite3').read_bytes()
    second=again(tmp_path,inputs,plan,result,context)
    assert second==report
    assert db_before==(tmp_path/'review.sqlite3').read_bytes()
    assert before=={p:p.read_bytes() for p in (tmp_path/'editorial').rglob('*') if p.is_file()}


def test_several_independent_units_are_ordered(tmp_path):
    report,*_=run(tmp_path,paragraphs=[BODY,BODY.replace('alpha','beta')])
    assert report.technical_reconciliation_complete,report
    assert report.counts['verified_stored_review_drafts']==2
    assert len(report.proposals)==2


@pytest.mark.parametrize('change',[{'original_text':BODY+' Remission was 99%.'},{'reference':'doi:10.9999/invented'}])
def test_rejected_candidates_not_counted_as_reviewable(tmp_path,change):
    report,*_=run(tmp_path,provider=FakeProvider(lambda r,*_:response(r,**change)))
    assert report.counts['rejected_units']==1,report
    assert report.counts['unique_reviewable_drafts']==0
    assert report.counts['verified_stored_review_drafts']==0


def test_preflight_without_inventory_is_unknown_not_zero():
    report=reconcile_coverage(observations=(StageObservation(stage='extraction',state='failed',code='EXTRACTION_FAILED'),))
    assert not report.technical_reconciliation_complete
    assert report.counts['source_regions'] is None and report.counts['generated_candidates'] is None
    assert next(s for s in report.stages if s.stage=='extraction').state=='failed'


def test_missing_generation_context_is_structured_incomplete(tmp_path):
    _,inputs,plan,result,context=run(tmp_path)
    report=again(tmp_path,inputs,plan,result,context,generation=None)
    assert not report.technical_reconciliation_complete
    assert 'GENERATION_CONFIGURATION_UNVERIFIED' in report.verification_issues
