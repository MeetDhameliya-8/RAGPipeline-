"""Hard-stop recovery and local storage isolation, using disposable artifacts."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from editorial_integration_support import BODY, FakeProvider, prepared, response, execute
from proposal_editorial import WorkflowError


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import socket
    def forbidden(*_,**__):
        pytest.fail('No network connections are allowed in A3 tests')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)


def test_actual_process_exit_after_publication_resumes_zero_calls(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = '''import os,sys
from pathlib import Path
sys.path[:0] = [str(Path.cwd()),str(Path.cwd()/'tests')]
from editorial_integration_support import execute
def hard_stop(count):
    if count: os._exit(23)
execute(Path(sys.argv[1]),progress=hard_stop)
'''
    child = subprocess.run([sys.executable,'-B','-c',code,str(tmp_path)],cwd=root,capture_output=True,timeout=20)
    assert child.returncode == 23,child.stderr.decode(errors='replace')
    assert not (tmp_path/'review.sqlite3').exists()
    assert len(list((tmp_path/'editorial'/'units').glob('*/response.json'))) == 1
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider)
    assert not provider.calls and len(result.drafts) == 1
    assert result.units[0].storage['readback_verified']


def test_process_exit_after_sqlite_commit_recovers_readback(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = '''import os,sys
from pathlib import Path
sys.path[:0] = [str(Path.cwd()),str(Path.cwd()/'tests')]
from editorial_integration_support import execute
from src import local_chunk_store
local_chunk_store.verify_chunks_file_ingested = lambda *a,**k:os._exit(24)
execute(Path(sys.argv[1]))
'''
    child = subprocess.run([sys.executable,'-B','-c',code,str(tmp_path)],cwd=root,capture_output=True,timeout=20)
    assert child.returncode == 24,child.stderr.decode(errors='replace')
    provider = FakeProvider()
    result = execute(tmp_path,provider=provider)
    assert not provider.calls and result.units[0].storage['readback_verified']
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select count(*) from chunks').fetchone()[0] == 1


def test_global_historical_content_identity_is_preserved(tmp_path):
    from src import local_chunk_store as store
    from proposal_editorial.checkpoints import atomic_json
    from proposal_editorial import requests,WorkflowConfig
    from proposal_editorial.eligibility import assess_eligibility
    from clinical_proposals import propose_concepts
    extraction,source,inventory = prepared()
    plan = propose_concepts(source,inventory)
    item = assess_eligibility(source,plan,WorkflowConfig())[0]
    request,_,_ = requests.build_request(source,plan,item,extraction['reference_metadata']['reference'],'fixture','fixture')
    package = tmp_path/'historical'
    atomic_json(package/'chunks.json',json.loads(response(request)))
    atomic_json(package/'validation_report.json',{'diagnostics':{'local_storage_identity':{
        'version':'pdf-content-v1','pdf_sha256':source.evidence.pdf_sha256}}})
    store.ingest_chunks_file(package/'chunks.json','REVIEW_REQUIRED',db_path=tmp_path/'review.sqlite3')
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        before = conn.execute('select * from chunks').fetchall()
    result = execute(tmp_path)
    assert result.units[0].status == 'existing_row_preserved'
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select * from chunks').fetchall() == before


def test_midflight_source_hold_change_blocks_persistence(tmp_path):
    inputs = prepared()
    def action(r,*_):
        inputs[1].warnings.append('new source hold')
        return response(r)
    with pytest.raises(WorkflowError) as caught:
        execute(tmp_path,inputs=inputs,provider=FakeProvider(action))
    assert not caught.value.result.drafts and not (tmp_path/'review.sqlite3').exists()
    assert list((tmp_path/'editorial'/'units').glob('*/response.json'))


def test_duplicate_generated_content_keeps_checkpoint_excludes_second_row(tmp_path):
    inputs = prepared([BODY,BODY+' Study records remained available for review.'])
    result = execute(tmp_path,inputs=inputs,provider=FakeProvider(lambda r,*_:response(r,BODY)))
    assert [u.status for u in result.units] == ['stored_review','duplicate_generated'],result
    assert len(list((tmp_path/'editorial'/'units').glob('*/response.json'))) == 2
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select count(*) from chunks').fetchone()[0] == 1


@pytest.mark.parametrize('corrupt',['content','embedding','verification','exception'])
def test_bad_readback_quarantines_own_new_row(tmp_path,monkeypatch,corrupt):
    from src import local_chunk_store as store
    original = store.verify_chunks_file_ingested
    def altered(*args,**kwargs):
        result = original(*args,**kwargs)
        if corrupt == 'exception':
            raise sqlite3.OperationalError('confidential database error')
        with sqlite3.connect(kwargs['db_path']) as conn:
            if corrupt == 'content':
                conn.execute("update chunks set original_text='invalid readback'")
            elif corrupt == 'embedding':
                conn.execute("update embeddings set vector_json='[]'")
        if corrupt == 'verification':
            result['verified'] = False
        return result
    monkeypatch.setattr(store,'verify_chunks_file_ingested',altered)
    result = execute(tmp_path)
    assert result.units[0].status == 'storage_failed'
    assert result.units[0].storage['code'] == 'STORAGE_READBACK_INCOMPLETE'
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select validation_status from chunks').fetchone()[0] == 'FAILED'
    assert not store.retrieve('treatment alpha',db_path=tmp_path/'review.sqlite3')


def test_policy_rejection_excludes_previously_saved_own_row(tmp_path,monkeypatch):
    from proposal_editorial import requests,validation
    from src import local_chunk_store as store
    first = execute(tmp_path)
    assert store.retrieve('treatment alpha',db_path=tmp_path/'review.sqlite3')
    original = validation.assess_response
    def reject(*args,**kwargs):
        verdict = original(*args,**kwargs)
        return validation.Assessment(verdict.document,dict(verdict.report,status='FAILED'),'SOURCE_VALIDATION_FAILED')
    monkeypatch.setattr(requests,'validation_policy',lambda:'new-stricter-policy')
    monkeypatch.setattr(validation,'assess_response',reject)
    provider = FakeProvider()
    second = execute(tmp_path,provider=provider)
    assert not provider.calls and not second.drafts
    assert not store.retrieve('treatment alpha',db_path=tmp_path/'review.sqlite3')
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select original_text from chunks').fetchone()[0] == first.drafts[0].document['chunks'][0]['original_text']


def test_existing_human_review_status_never_overwritten(tmp_path):
    execute(tmp_path)
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        conn.execute("update chunks set review_status='REJECTED'")
        before = conn.execute('select * from chunks').fetchall()
    result = execute(tmp_path)
    assert result.units[0].status == 'existing_row_preserved'
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute('select * from chunks').fetchall() == before


def test_unrelated_historical_rows_and_embeddings_unchanged(tmp_path):
    from src import local_chunk_store as store
    legacy = tmp_path/'legacy'
    legacy.mkdir()
    # A separate synthetic historical package; never a user's database.
    from editorial_integration_support import prepared,response
    from proposal_editorial import requests
    from clinical_proposals import propose_concepts
    from proposal_editorial.eligibility import assess_eligibility
    from proposal_editorial import WorkflowConfig
    extraction,source,inventory = prepared()
    plan = propose_concepts(source,inventory)
    item = assess_eligibility(source,plan,WorkflowConfig())[0]
    req,_,_ = requests.build_request(source,plan,item,extraction['reference_metadata']['reference'],'fixture','fixture')
    path = legacy/'chunks.json'
    path.write_text(response(req),encoding='utf-8')
    store.ingest_chunks_file(path,'REVIEW_REQUIRED',db_path=tmp_path/'review.sqlite3')
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        rows = conn.execute('select * from chunks').fetchall()
        embeddings = conn.execute('select * from embeddings').fetchall()
    execute(tmp_path)
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        assert conn.execute("select * from chunks where output_dir='legacy'").fetchall() == rows
        assert conn.execute('select * from embeddings where chunk_id=?',(rows[0][0],)).fetchall() == embeddings


def test_completed_diagnostics_redacted_counts_usage_and_timings(tmp_path):
    result = execute(tmp_path)
    report = json.loads(next((tmp_path/'editorial'/'runs').glob('*.json')).read_text())
    assert report['observability']['run_complete']
    assert 'proposal_editorial_workflow' in report['observability']['stages']
    totals = report['observability']['request_totals']
    assert totals['logical_calls'] == 1 and totals['successful_responses'] == 1
    assert totals['prompt_tokens_observed'] == 50 and totals['completion_tokens_observed'] == 110
    assert not totals['http_attempts_complete'] and totals['calls_with_unknown_http_attempts'] == 1
    assert not totals['token_usage_complete'] and report['corrective_calls'] == 0
    assert BODY not in json.dumps(report) and 'synthetic-secret-patient' not in json.dumps(report)
    assert report['observability']['counts']['accepted_candidate_events'] == 1


def test_existing_schema_mismatch_fails_before_model(tmp_path):
    with sqlite3.connect(tmp_path/'review.sqlite3') as conn:
        conn.execute('create table chunks (chunk_id text)')
    provider = FakeProvider()
    with pytest.raises(WorkflowError):
        execute(tmp_path,provider=provider)
    assert not provider.calls


def test_windows_path_budget_fails_before_provider(tmp_path,monkeypatch):
    from proposal_editorial import checkpoints
    monkeypatch.setattr(checkpoints.os,'name','nt')
    provider = FakeProvider()
    with pytest.raises(WorkflowError):
        execute(tmp_path,provider=provider,workspace=tmp_path/('long-path-'+('x'*150)))
    assert not provider.calls


def test_synthetic_pdf_citation_cleaning_hold_survives_preparation_cache(tmp_path):
    import fitz
    from src.pdf_extractor import extract_pdf
    from src.native_layout import recover_native_layout
    from src.source_map import build_source_map
    from src.document_concepts import build_document_concept_plan
    from src.preparation_cache import save_preparation,load_preparation
    from editorial_integration_support import REFERENCE
    pdf = tmp_path/'fictional-new-document.pdf'
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40,45),REFERENCE,fontsize=9)
        page.insert_text((40,80),'Methods')
        assert page.insert_textbox(fitz.Rect(40,110,550,700),BODY,fontsize=10) > 0
        document.save(pdf)
    extraction = recover_native_layout(extract_pdf(pdf))
    from src.generation_preflight import require_source_reference
    assert require_source_reference(extraction) == REFERENCE
    source = build_source_map(extraction)
    save_preparation(pdf,extraction,source,tmp_path/'preparation')
    cached = load_preparation(pdf,tmp_path/'preparation')
    assert cached is not None
    extraction,source = cached
    inventory = build_document_concept_plan(extraction,source)
    # Existing extraction drops the citation from the clinical buffer and
    # retains a critical transformation/unmapped-evidence hold. A3 must honor
    # that real preparation outcome rather than manufacture a source review.
    provider = FakeProvider()
    result = execute(tmp_path,inputs=(extraction,source,inventory),provider=provider)
    assert not provider.calls and not result.drafts
    assert any(e.state == 'blocked_source' for e in result.eligibility)
    assert any(w.code == 'text_transformation_unreviewed' for w in source.evidence.warnings)
    assert not (tmp_path/'review.sqlite3').exists()
