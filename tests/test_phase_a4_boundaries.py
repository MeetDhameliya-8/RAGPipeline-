"""Physical source-hold characterization and explicit reconciliation boundaries."""
import json
from pathlib import Path
import sqlite3

import pytest

from coverage_reconciliation import reconcile_coverage,capture_generation_context,StageObservation
from coverage_support import run,again
from editorial_integration_support import BODY,REFERENCE,FakeProvider,execute
from clinical_proposals import propose_concepts


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*_,**__):
        pytest.fail('No network in coverage reconciliation')
    monkeypatch.setattr(socket.socket,'connect',forbidden)


def test_context_only_does_not_create_independent_concept(tmp_path):
    report,*_=run(tmp_path)
    headings=[r for r in report.regions if r.source_state=='supporting_context_only']
    assert headings and all(not r.reviewable_proposals and not r.verified_storage_proposals for r in headings)
    assert report.counts['unique_owned_regions']==1


def test_source_review_exclusion_record_distinct_from_structural_candidate(tmp_path):
    # Reuse a pre-existing fictional source-review fixture, never fabricate one
    # inside the engine or derive one from an automatic proposal.
    from test_milestone3_document_concepts import inventory,region,review_stamp,locate
    from src.document_concepts import ReviewedExclusion,build_document_concept_plan
    extraction,source,original=inventory(['Methods\n\nAdults received treatment alpha.\n\nReferences\n\nA synthetic bibliography entry.'])
    target=region(original,extraction,'A synthetic')
    stub=ReviewedExclusion(review=review_stamp(original),region_id=target.region_id,
        reason='reference_list',source_basis=locate(extraction,'A synthetic bibliography entry.'))
    reviewed=build_document_concept_plan(extraction,source,exclusions=[stub])
    before=reconcile_coverage(source,original,propose_concepts(source,original))
    after=reconcile_coverage(source,reviewed,propose_concepts(source,reviewed))
    assert next(r for r in before.regions if r.region_id==target.region_id).source_state=='structural_candidate_exclusion'
    assert next(r for r in after.regions if r.region_id==target.region_id).source_state=='reviewed_nonclinical_exclusion'
    assert after.clinically_approved_chunks==0


def test_local_approved_string_is_not_authenticated_clinical_approval(tmp_path):
    _,inputs,plan,result,context=run(tmp_path)
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        conn.execute("UPDATE chunks SET review_status='APPROVED'")
    before=(tmp_path/'review.sqlite3').read_bytes()
    report=again(tmp_path,inputs,plan,result,context)
    assert report.clinically_approved_chunks==0 and not report.clinically_complete
    assert 'UNAUTHENTICATED_REVIEW_MARKER' in report.proposals[0].verification_issues
    assert (tmp_path/'review.sqlite3').read_bytes()==before


@pytest.mark.parametrize('what',['text','vector','sidecar','run','policy'])
def test_tampered_readback_or_proof_is_not_trusted(tmp_path,what):
    _,inputs,plan,result,context=run(tmp_path)
    if what=='text':
        with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
            conn.execute("UPDATE chunks SET original_text='SECRET altered text'")
    elif what=='vector':
        with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
            conn.execute("UPDATE embeddings SET vector_json='[]'")
    elif what=='sidecar':
        p=next((tmp_path/'editorial'/'local').glob('*/source_link.json'))
        d=json.loads(p.read_text());d['evidence_fingerprint']='b'*64;p.write_text(json.dumps(d))
    elif what=='run':
        p=next((tmp_path/'editorial'/'runs').glob('*.json'));p.write_text('{}')
    elif what=='policy':
        p=next((tmp_path/'editorial'/'units').glob('*/validation-*.json'))
        d=json.loads(p.read_text());d['validation_policy']='b'*64;p.write_text(json.dumps(d))
    report=again(tmp_path,inputs,plan,result,context)
    assert not report.technical_reconciliation_complete
    if what!='run':
        assert report.counts['verified_stored_review_drafts']==0
    assert 'SECRET' not in report.model_dump_json()


def test_artifact_changed_during_snapshot_revokes_verification(tmp_path,monkeypatch):
    from coverage_reconciliation.artifacts import ArtifactReader
    _,inputs,plan,result,context=run(tmp_path)
    monkeypatch.setattr(ArtifactReader,'stable',lambda self:False)
    report=again(tmp_path,inputs,plan,result,context)
    assert not report.technical_reconciliation_complete and report.counts['verified_stored_review_drafts']==0
    assert all(not r.verified_storage_proposals for r in report.regions)


def test_unavailable_stages_remain_unknown(tmp_path):
    _,inputs,plan,result,context=run(tmp_path)
    no_workflow=again(tmp_path,inputs,plan,result,context,workflow=None)
    assert no_workflow.counts['generated_candidates'] is None
    assert no_workflow.counts['verified_stored_review_drafts']==1  # independently verifiable earlier work survives
    assert not no_workflow.technical_reconciliation_complete
    no_plan=again(tmp_path,inputs,plan,result,context,proposals=None)
    assert no_plan.counts['proposals'] is None
    assert not any(r.proposal_links_verified for r in no_plan.regions)


def test_citation_preflight_failure_preserves_source_inventory(tmp_path):
    _,inputs,plan,result,context=run(tmp_path)
    inputs[0]['reference_metadata']={}
    report=again(tmp_path,inputs,plan,result,context,workflow=None,
        observations=(StageObservation(stage='generation',state='not_started',code='REFERENCE_IDENTITY_UNRESOLVED'),))
    assert report.counts['source_regions'] is not None
    assert report.counts['generated_candidates'] is None
    assert next(s for s in report.stages if s.stage=='citation').state=='failed'


def test_missing_storage_evidence_does_not_create_database(tmp_path):
    _,inputs,plan,result,context=run(tmp_path)
    report=again(tmp_path,inputs,plan,result,context,db_path=None)
    assert report.counts['verified_stored_review_drafts']==0
    assert report.proposals[0].persistence=='reconciliation_required'


def test_storage_budget_failure_is_explicit(tmp_path):
    from coverage_reconciliation import ReconciliationLimits
    _,inputs,plan,result,context=run(tmp_path)
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        conn.execute('UPDATE embeddings SET vector_json=?',('x'*2000001,))
    report=again(tmp_path,inputs,plan,result,context)
    assert 'STORAGE_BUDGET_EXCEEDED' in report.proposals[0].verification_issues
    assert report.counts['verified_stored_review_drafts']==0


def test_no_fabricated_review_objects_or_new_generation(tmp_path,monkeypatch):
    _,inputs,plan,result,context=run(tmp_path)
    import src.document_concepts as contracts
    from src import local_chunk_store
    def forbidden(*_,**__):
        pytest.fail('A4 must not generate, ingest, or manufacture source reviews')
    monkeypatch.setattr(contracts,'ReviewedQuestion',forbidden)
    monkeypatch.setattr(contracts,'SourceReview',forbidden)
    monkeypatch.setattr(local_chunk_store,'ingest_chunks_file',forbidden)
    monkeypatch.setattr(local_chunk_store,'init_db',forbidden)
    monkeypatch.setattr(FakeProvider,'generate',forbidden)
    report=again(tmp_path,inputs,plan,result,context)
    assert report.technical_reconciliation_complete


def test_physical_pdf_hold_stage_and_accounting(tmp_path):
    import fitz
    from src.pdf_extractor import extract_pdf
    from src.native_layout import recover_native_layout
    from src.source_map import build_source_map
    from src.evidence import get_evidence
    from src.document_concepts import build_document_concept_plan
    from src.generation_preflight import require_source_reference
    pdf=tmp_path/'synthetic.pdf'
    with fitz.open() as document:
        page=document.new_page()
        page.insert_text((40,45),REFERENCE,fontsize=9)
        page.insert_text((40,80),'Methods')
        assert page.insert_textbox(fitz.Rect(40,110,550,700),BODY,fontsize=10)>0
        document.save(pdf)
    extraction=extract_pdf(pdf)
    first=get_evidence(extraction)
    before={w.warning_id for w in first.warnings}
    recovered=recover_native_layout(extraction)
    after_layout=get_evidence(recovered)
    source=build_source_map(recovered)
    assert require_source_reference(recovered)==REFERENCE
    inventory=build_document_concept_plan(recovered,source)
    provider=FakeProvider()
    workflow=execute(tmp_path,inputs=(recovered,source,inventory),provider=provider)
    report=reconcile_coverage(source,inventory,propose_concepts(source,inventory),workflow,
        extraction=recovered,workspace=tmp_path/'editorial',db_path=tmp_path/'review.sqlite3',
        generation=capture_generation_context(provider))
    assert not provider.calls and report.counts['verified_stored_review_drafts']==0
    assert any(r.basis=='unmapped_native' for r in report.regions)
    assert any(r.source_state=='existing_source_hold' for r in report.regions)
    assert sum(report.source_characters_by_state.values())==len(recovered['full_text'])
    # Record stage findings without assuming the earlier report identified
    # the introducing operation correctly. The evaluation tool gives details.
    assert any(w.code=='text_transformation_unreviewed' for w in source.evidence.warnings)
    assert ' '.join(BODY.split()) in ' '.join(source.evidence.buffers[source.evidence.canonical_buffer_id].text.split())
    assert any(REFERENCE in b.text for b in source.evidence.buffers.values() if b.stage=='raw')
