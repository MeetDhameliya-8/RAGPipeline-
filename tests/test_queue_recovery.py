"""Exercise saved retries, leases and queue advancement without network calls."""
import json
import sqlite3
import threading
import time

import pytest

from src import local_rag_chat as web
from src.queue_recovery import recovery_decision


@pytest.fixture(autouse=True)
def reset():
    web._reset_processing_queue_for_tests()
    yield
    if web._queue_worker_thread:
        web._queue_worker_thread.join(5)
    web._reset_processing_queue_for_tests()


def enqueue(*names):
    for name in names:
        (web.PDF_UPLOAD_DIR/name).write_bytes(b"%PDF-test")
    return web.enqueue_processing_jobs(list(names))


def drain():
    if web._queue_worker_thread:
        web._queue_worker_thread.join(5)
        assert not web._queue_worker_thread.is_alive()


def ready():
    return {"status":"partial_success", "chunk_counts":{"stored":1},
            "local_database":{"status":"LOCAL_REVIEW_DB_STORED", "readback_verified":True}}


def due(job):
    with sqlite3.connect(web.LOCAL_DB_PATH) as conn:
        conn.execute("update pdf_processing_queue set next_attempt_at=0 where id=?", (job,))


def test_temporary_failure_advances_queue_and_resumes_from_saved_schedule(monkeypatch):
    jobs=enqueue("outage.pdf", "good.pdf"); calls=[]
    def process(name, **kwargs):
        calls.append(name)
        if calls.count("outage.pdf")==1 and name=="outage.pdf":
            return {"status":"failed", "failure_code":"TRANSPORT_ERROR", "retryable":True}
        return ready()
    monkeypatch.setattr(web,"process_uploaded_pdf",process)
    web.start_processing_queue(); drain()
    state=web.processing_queue_status()
    assert calls==["outage.pdf","good.pdf"]
    assert state["waiting_count"]==1 and state["terminal_count"]==1
    pending=state["items"][0]
    assert pending["status"]=="RETRY_WAIT" and pending["next_attempt_at"]>time.time()
    # No in-memory timer is needed after a restart.
    web._queue_worker_thread=None
    web.queue_supervisor_tick();drain()
    assert calls==["outage.pdf","good.pdf"]
    due(jobs[0]["id"]);web.queue_supervisor_tick();drain()
    assert web.processing_queue_status()["counts"]=={"LOCAL_REVIEW_READY":2}
    assert calls==["outage.pdf","good.pdf","outage.pdf"]


def test_retries_are_bounded_and_auth_does_not_loop(monkeypatch):
    job=enqueue("outage.pdf")[0]
    monkeypatch.setattr(web,"process_uploaded_pdf",lambda *a,**k:{"status":"failed","failure_code":"TRANSPORT_TIMEOUT"})
    for i in range(5):
        due(job["id"]);web.queue_supervisor_tick();drain()
    item=web.processing_queue_status()["items"][0]
    assert item["status"]=="ACTION_REQUIRED" and item["attempt_count"]==5
    for code in ("AUTHENTICATION_FAILED","QUOTA_EXHAUSTED"):
        assert recovery_decision({"status":"failed","failure_code":code}).retry is False
    assert recovery_decision({"status":"failed","failure_code":"TRANSPORT_ERROR","retryable":False}) is None
    assert recovery_decision({"status":"failed","failure_code":"SOURCE_VALIDATION_FAILED"}) is None


def test_exception_timeout_is_retryable_but_raw_error_is_not_persisted(monkeypatch):
    enqueue("outage.pdf")
    monkeypatch.setattr(web,"process_uploaded_pdf",lambda *a,**k:(_ for _ in ()).throw(TimeoutError("secret body")))
    web.start_processing_queue();drain()
    state=web.processing_queue_status()
    assert state["items"][0]["status"]=="RETRY_WAIT"
    assert "secret body" not in json.dumps(state)


def test_partial_chunks_remain_accessible_during_retry(monkeypatch):
    job=enqueue("partial.pdf")[0]
    first=ready() | {"output_dir":"retained-review-output", "recovery_signals":[{"code":"TRANSPORT_TIMEOUT"}]}
    results=iter([first,{"status":"failed","failure_code":"TRANSPORT_TIMEOUT"},ready()])
    monkeypatch.setattr(web,"process_uploaded_pdf",lambda *a,**k:next(results))
    for _ in range(2):
        due(job["id"]);web.queue_supervisor_tick();drain()
    item=web.processing_queue_status()["items"][0]
    assert item["status"]=="RETRY_WAIT" and item["stored_chunks"]==1
    assert item["output_dir"]=="retained-review-output"
    due(job["id"]);web.queue_supervisor_tick();drain()
    assert web.processing_queue_status()["items"][0]["status"]=="LOCAL_REVIEW_READY"


def test_live_lease_protects_slow_request_and_dead_lease_recovers(monkeypatch):
    job=enqueue("slow.pdf")[0]
    entered=threading.Event();release=threading.Event()
    def process(*a,**k):
        entered.set();assert release.wait(4);return ready()
    monkeypatch.setattr(web,"process_uploaded_pdf",process)
    web.start_processing_queue();assert entered.wait(2)
    try:
        with sqlite3.connect(web.LOCAL_DB_PATH) as conn:
            conn.execute("update pdf_processing_queue set updated_at=0 where id=?",(job["id"],))
        web.queue_supervisor_tick()
        with sqlite3.connect(web.LOCAL_DB_PATH) as conn:
            assert conn.execute("select updated_at from pdf_processing_queue").fetchone()[0]>time.time()-5
    finally:
        release.set();drain()
    # Simulate another process dying while active, with its heartbeat expired.
    with sqlite3.connect(web.LOCAL_DB_PATH) as conn:
        conn.execute("update pdf_processing_queue set status='EXTRACTING',worker_id='dead:job',updated_at=0")
    web.queue_supervisor_tick();drain()
    assert web.processing_queue_status()["items"][0]["attempt_count"]==2


def test_source_hold_does_not_retry_or_block_next_document(monkeypatch):
    enqueue("unreadable.pdf","good.pdf")
    monkeypatch.setattr(web,"process_uploaded_pdf",lambda name,**k:
        {"status":"failed","failure_code":"EXTRACTION_BLOCKED"} if name=="unreadable.pdf" else ready())
    web.start_processing_queue();drain()
    assert [i["status"] for i in web.processing_queue_status()["items"]]==["SOURCE_REVIEW_REQUIRED","LOCAL_REVIEW_READY"]


def test_supervisor_recovers_from_tick_error_and_stops_cleanly(monkeypatch):
    stop=threading.Event();calls=[]
    def tick():
        calls.append(1)
        if len(calls)==1:raise sqlite3.OperationalError("locked")
        stop.set()
    monkeypatch.setattr(web,"queue_supervisor_tick",tick)
    thread=threading.Thread(target=web.supervise_processing_queue,args=(stop,))
    thread.start();thread.join(4)
    assert not thread.is_alive() and len(calls)==2


def test_http_upload_starts_queue_without_start_button(monkeypatch):
    import http.client
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(web,"process_uploaded_pdf",lambda *a,**k:ready())
    server=ThreadingHTTPServer(('127.0.0.1',0),web.LocalRagChatHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
    try:
        boundary='queue-test-boundary'
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="pdf_file"; filename="automatic.pdf"\r\n'
              f'Content-Type: application/pdf\r\n\r\n%PDF-test\r\n--{boundary}--\r\n').encode()
        connection.request('POST','/upload-pdf',body,{'Content-Type':f'multipart/form-data; boundary={boundary}'})
        response=connection.getresponse();payload=json.loads(response.read())
        assert response.status==200 and len(payload['queued'])==1
        drain()
        assert web.processing_queue_status()['items'][0]['status']=='LOCAL_REVIEW_READY'
    finally:
        connection.close();server.shutdown();server.server_close();thread.join(3)


def test_storage_readback_failure_retries_without_dropping_available_chunks():
    decision=recovery_decision({'local_database':{'status':'LOCAL_REVIEW_DB_STORED','readback_verified':False}})
    assert decision.retry and decision.code=='STORAGE_READBACK_INCOMPLETE'


def test_repeated_crash_recovery_is_bounded(monkeypatch):
    enqueue('crashing.pdf')
    with sqlite3.connect(web.LOCAL_DB_PATH) as conn:
        conn.execute("update pdf_processing_queue set status='EXTRACTING',attempt_count=5,updated_at=0")
    monkeypatch.setattr(web,'process_uploaded_pdf',lambda *a,**k:pytest.fail('Exhausted crash budget must not run'))
    web.queue_supervisor_tick();drain()
    assert web.processing_queue_status()['items'][0]['status']=='ACTION_REQUIRED'


def test_actual_pipeline_storage_and_queue_resume_after_later_azure_timeout(monkeypatch,tmp_path):
    from unittest.mock import Mock
    from src import chunk_generator,local_chunk_store
    from src.providers.base import ChunkGenerationError
    from test_source_packages import fixture,response,TEXT
    extraction,_,_=fixture(tmp_path,pages=2)
    (web.PDF_UPLOAD_DIR/'prose.pdf').write_bytes((tmp_path/'prose.pdf').read_bytes())
    monkeypatch.setattr(web.pipeline,'OUTPUT_DIR',web.OUTPUT_DIR)
    monkeypatch.setattr(web.pipeline,'extract_pdf',lambda path:extraction)
    provider=Mock();provider.generate.side_effect=[response(extraction),
        ChunkGenerationError('temporary',code='TRANSPORT_TIMEOUT')]
    monkeypatch.setattr(chunk_generator,'_provider_from_environment',lambda:provider)
    original_process=web.process_uploaded_pdf
    exceptions=[]
    def traced_process(*args,**kwargs):
        try:return original_process(*args,**kwargs)
        except Exception:
            import traceback
            exceptions.append(traceback.format_exc())
            raise
    monkeypatch.setattr(web,'process_uploaded_pdf',traced_process)
    job=web.enqueue_processing_jobs(['prose.pdf'])[0]
    web.start_processing_queue();drain()
    first=web.processing_queue_status()['items'][0]
    assert first['status']=='RETRY_WAIT' and first['stored_chunks']==1, exceptions or first.get('result')
    assert first['local_database']['readback_verified']
    provider.generate.side_effect=None
    provider.generate.return_value=response(extraction,TEXT.replace('Adults','Children').replace('alpha','beta'))
    due(job['id']);web.queue_supervisor_tick();drain()
    final=web.processing_queue_status()['items'][0]
    assert final['status']=='LOCAL_REVIEW_READY' and final['stored_chunks']==2
    assert final['local_database']['readback_verified']
    assert provider.generate.call_count==3  # First unit was not regenerated.
    assert local_chunk_store.db_status()['chunk_count']==2  # First unit was not reinserted.
