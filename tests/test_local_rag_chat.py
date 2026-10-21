import json
import threading
import time

import pytest

from src import local_rag_chat
from src import local_chunk_store
from src.local_rag_chat import (
    ChunkFileError,
    LocalWorkflowError,
    UploadedFile,
    build_rag_response,
    database_upload_status,
    enqueue_processing_jobs,
    load_chunk_file,
    parse_chunks_json,
    parse_chunks_markdown,
    parse_multipart_pdf_upload_batch,
    parse_multipart_pdf_uploads,
    processing_queue_status,
    process_uploaded_pdf,
    retrieve_chunks,
    save_uploaded_pdf,
    secure_pdf_filename,
    start_processing_queue,
)


@pytest.fixture(autouse=True)
def reset_processing_queue():
    local_rag_chat._reset_processing_queue_for_tests()
    yield
    local_rag_chat._reset_processing_queue_for_tests()


def test_secure_pdf_filename_accepts_only_pdf():
    assert secure_pdf_filename("clinical paper.pdf") == "clinical_paper.pdf"
    long_name = secure_pdf_filename("a" * 180 + ".pdf")
    assert long_name.endswith(".pdf")
    assert len(long_name) <= 120
    try:
        secure_pdf_filename("chunks.json")
    except LocalWorkflowError as exc:
        assert "Only PDF" in str(exc)
    else:
        raise AssertionError("Expected non-PDF upload to be rejected.")


def test_save_uploaded_pdf(tmp_path):
    saved = save_uploaded_pdf(UploadedFile("paper.pdf", b"%PDF-1.4"), tmp_path)

    assert saved.name == "paper.pdf"
    assert saved.read_bytes() == b"%PDF-1.4"


def test_save_uploaded_pdf_avoids_overwriting_existing_file(tmp_path):
    first = save_uploaded_pdf(UploadedFile("paper.pdf", b"%PDF-1.4 first"), tmp_path)
    second = save_uploaded_pdf(UploadedFile("paper.pdf", b"%PDF-1.4 second"), tmp_path)

    assert first.name == "paper.pdf"
    assert second.name != first.name
    assert first.read_bytes() == b"%PDF-1.4 first"
    assert second.read_bytes() == b"%PDF-1.4 second"


def test_save_uploaded_pdf_preserves_unique_suffix_for_long_duplicate_names(tmp_path):
    long_name = "Effect_of_Stress_Ulcer_Prophylaxis_With_Proton_Pump_Inhibitors_vs_Histamine-2_Receptor_Blockers_on_In-Hospital_Morta.pdf"
    first = save_uploaded_pdf(UploadedFile(long_name, b"%PDF-1.4 first"), tmp_path)
    second = save_uploaded_pdf(UploadedFile(long_name, b"%PDF-1.4 second"), tmp_path)

    assert second.name != first.name
    assert len(second.name) <= 120
    assert secure_pdf_filename(second.name) == second.name
    assert second.read_bytes() == b"%PDF-1.4 second"


def test_parse_multipart_pdf_uploads_accepts_multiple_files():
    boundary = "----gastroagi-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="pdf_file"; filename="one.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.4 one\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="pdf_file"; filename="two.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.4 two\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    uploads = parse_multipart_pdf_uploads(headers, body)

    assert [upload.filename for upload in uploads] == ["one.pdf", "two.pdf"]
    assert uploads[0].content.startswith(b"%PDF-1.4 one")
    assert uploads[1].content.startswith(b"%PDF-1.4 two")


def test_parse_multipart_pdf_upload_batch_flags_invalid_files_without_losing_valid_files():
    boundary = "----gastroagi-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="pdf_file"; filename="one.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.4 one\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="pdf_file"; filename="notes.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "not a pdf\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="pdf_file"; filename="empty.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="pdf_file"; filename="two.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.4 two\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    batch = parse_multipart_pdf_upload_batch(headers, body)

    assert [upload.filename for upload in batch.uploads] == ["one.pdf", "two.pdf"]
    assert [item.filename for item in batch.rejected] == ["notes.txt", "empty.pdf"]
    assert "Only PDF" in batch.rejected[0].error
    assert "empty" in batch.rejected[1].error


def test_parse_multipart_pdf_upload_batch_accepts_thirty_and_rejects_extras():
    boundary = "----gastroagi-thirty-pdf-boundary"
    parts = []
    for index in range(1, 32):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="pdf_file"; filename="paper-{index}.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            f"%PDF-1.4 paper {index}\r\n"
        )
    body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    batch = parse_multipart_pdf_upload_batch(headers, body)

    assert [upload.filename for upload in batch.uploads] == [f"paper-{index}.pdf" for index in range(1, 31)]
    assert [item.filename for item in batch.rejected] == ["paper-31.pdf"]
    assert "30 PDFs" in batch.rejected[0].error


def test_uploading_thirty_pdfs_enqueues_fifo_without_starting(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    boundary = "----gastroagi-thirty-pdf-boundary"
    parts = []
    for index in range(1, 31):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="pdf_file"; filename="paper-{index}.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            f"%PDF-1.4 paper {index}\r\n"
        )
    body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    batch = parse_multipart_pdf_upload_batch(headers, body)
    saved = [save_uploaded_pdf(upload, pdf_dir) for upload in batch.uploads]
    queued = enqueue_processing_jobs([path.name for path in saved])
    status = processing_queue_status()

    assert batch.rejected == []
    assert [path.name for path in saved] == [f"paper-{index}.pdf" for index in range(1, 31)]
    assert [item["filename"] for item in queued] == [f"paper-{index}.pdf" for index in range(1, 31)]
    assert [item["status"] for item in status["items"]] == ["QUEUED"] * 30
    assert status["running"] is False


def test_processing_queue_runs_uploaded_pdfs_sequentially(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    for name in ("one.pdf", "two.pdf", "three.pdf"):
        (pdf_dir / name).write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    state_lock = threading.Lock()
    active = 0
    max_active = 0
    processed_order = []

    def fake_process(filename, force=False, progress_callback=None):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            processed_order.append(filename)
        if progress_callback is not None:
            progress_callback("GENERATING_CHUNKS")
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return {
            "status": "processed",
            "output_dir": str(tmp_path / "output" / filename),
            "validation_status": "PASS",
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1, "readback_verified": True},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    enqueue_processing_jobs(["one.pdf", "two.pdf", "three.pdf"])
    started = start_processing_queue()

    assert started["status"] == "started"
    assert started["queue"]["items"][0]["status"] in {"EXTRACTING", "GENERATING_CHUNKS"}
    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    status = processing_queue_status()
    assert status["running"] is False
    assert processed_order == ["one.pdf", "two.pdf", "three.pdf"]
    assert max_active == 1
    assert status["counts"]["LOCAL_REVIEW_READY"] == 3
    assert [item["local_database"]["status"] for item in status["items"]] == ["LOCAL_REVIEW_DB_STORED"] * 3
    assert [item["result"]["chunk_counts"]["stored"] for item in status["items"]] == [1, 1, 1]


def test_processing_queue_runs_thirty_pdfs_fifo_single_click_and_auto_advances(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    filenames = [f"paper-{index}.pdf" for index in range(1, 31)]
    for name in filenames:
        (pdf_dir / name).write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    state_lock = threading.Lock()
    active = 0
    max_active = 0
    processed_order = []

    def fake_process(filename, force=False, progress_callback=None):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            processed_order.append(filename)
        if progress_callback is not None:
            progress_callback("PAGES_EXTRACTED", {"message": "Pages extracted: 2.", "page_count": 2})
            progress_callback("GENERATING_CHUNKS", {"message": "LLM chunk generation started."})
            progress_callback("CHUNKS_VALIDATED", {"message": "Validated 2 chunk(s).", "validated_chunks": 2})
        time.sleep(0.01)
        with state_lock:
            active -= 1
        return {
            "status": "processed",
            "validation_status": "PASS",
            "chunk_counts": {"generated": 2, "validated": 2, "stored": 2},
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 2, "readback_verified": True},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    enqueue_processing_jobs(filenames)
    started = start_processing_queue()

    assert started["status"] == "started"

    deadline = time.time() + 6
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    status = processing_queue_status()
    assert processed_order == filenames
    assert max_active == 1
    assert status["running"] is False
    assert status["counts"]["LOCAL_REVIEW_READY"] == 30
    assert [item["validated_chunks"] for item in status["items"]] == [2] * 30
    assert [item["stored_chunks"] for item in status["items"]] == [2] * 30
    assert [item["page_count"] for item in status["items"]] == [2] * 30


def test_processing_queue_rejects_duplicate_start_while_worker_is_running(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    for name in ("one.pdf", "two.pdf"):
        (pdf_dir / name).write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    started_processing = threading.Event()
    allow_stage_update = threading.Event()
    release_processing = threading.Event()

    def fake_process(filename, force=False, progress_callback=None):
        started_processing.set()
        assert allow_stage_update.wait(timeout=2)
        if progress_callback is not None:
            progress_callback("GENERATING_CHUNKS")
        assert release_processing.wait(timeout=2)
        return {
            "status": "processed",
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1, "readback_verified": True},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    enqueue_processing_jobs(["one.pdf", "two.pdf"])
    first_start = start_processing_queue()
    assert first_start["status"] == "started"
    assert first_start["queue"]["items"][0]["status"] == "EXTRACTING"
    assert first_start["queue"]["items"][1]["status"] == "QUEUED"
    assert started_processing.wait(timeout=2)
    allow_stage_update.set()
    deadline = time.time() + 2
    while processing_queue_status()["items"][0]["status"] != "GENERATING_CHUNKS" and time.time() < deadline:
        time.sleep(0.01)

    duplicate_start = start_processing_queue()

    assert duplicate_start["status"] == "already_running"
    assert duplicate_start["queue"]["active_count"] == 1
    assert duplicate_start["queue"]["queued_count"] == 1
    assert duplicate_start["queue"]["items"][0]["status"] == "GENERATING_CHUNKS"

    release_processing.set()
    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)
    assert processing_queue_status()["counts"]["LOCAL_REVIEW_READY"] == 2


def test_processing_queue_records_pipeline_failure(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "bad.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    enqueue_processing_jobs(["bad.pdf"])
    start_processing_queue()

    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    item = processing_queue_status()["items"][0]
    assert item["status"] == "ACTION_REQUIRED"
    assert item["result"]["failure_code"] == "RuntimeError"


def test_processing_queue_continues_after_failed_pdf_to_next_success(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    for name in ("bad.pdf", "good.pdf"):
        (pdf_dir / name).write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    processed_order = []

    def fake_process(filename, force=False, progress_callback=None):
        processed_order.append(filename)
        if filename == "bad.pdf":
            raise RuntimeError("corrupt pdf")
        if progress_callback is not None:
            progress_callback("CHUNKS_VALIDATED", {"message": "Validated 1 chunk(s).", "validated_chunks": 1})
        return {
            "status": "processed",
            "validation_status": "PASS",
            "chunk_counts": {"generated": 1, "validated": 1, "stored": 1},
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1, "readback_verified": True},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    enqueue_processing_jobs(["bad.pdf", "good.pdf"])
    start_processing_queue()

    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    status = processing_queue_status()
    assert processed_order == ["bad.pdf", "good.pdf"]
    assert [item["status"] for item in status["items"]] == ["ACTION_REQUIRED", "LOCAL_REVIEW_READY"]
    assert status["items"][0]["result"]["failure_code"] == "RuntimeError"
    assert status["items"][1]["validated_chunks"] == 1


def test_processing_queue_state_is_durable_outside_worker_memory(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "one.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    enqueued = enqueue_processing_jobs(["one.pdf"])
    local_rag_chat._queue_worker_running = False
    local_rag_chat._queue_worker_thread = None

    status = processing_queue_status()

    assert status["queued_count"] == 1
    assert status["items"][0]["id"] == enqueued[0]["id"]
    assert status["items"][0]["filename"] == "one.pdf"
    assert status["items"][0]["status"] == "QUEUED"
    assert status["items"][0]["progress_message"] == "Upload completed. Waiting in queue."


def test_stale_active_queue_job_is_recovered_for_retry(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "one.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    enqueue_processing_jobs(["one.pdf"])
    claimed = local_rag_chat._claim_next_queue_item()

    assert claimed is not None
    assert processing_queue_status()["items"][0]["status"] == "EXTRACTING"

    recovered = local_rag_chat.recover_stale_queue_jobs(max_age_seconds=0)
    status = processing_queue_status()

    assert recovered == 1
    assert status["items"][0]["status"] == "QUEUED"
    assert "resuming saved work automatically" in status["items"][0]["progress_message"]


def test_processing_queue_records_backend_progress_detail(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "one.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    started_processing = threading.Event()
    release_processing = threading.Event()

    def fake_process(filename, force=False, progress_callback=None):
        if progress_callback is not None:
            progress_callback("PAGES_EXTRACTED", {"message": "Pages extracted: 21.", "page_count": 21})
        started_processing.set()
        assert release_processing.wait(timeout=2)
        return {
            "status": "processed",
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    enqueue_processing_jobs(["one.pdf"])
    start_processing_queue()
    assert started_processing.wait(timeout=2)
    item = processing_queue_status()["items"][0]

    assert item["status"] == "PAGES_EXTRACTED"
    assert item["progress_message"] == "Pages extracted: 21."
    assert item["progress_detail"]["page_count"] == 21

    release_processing.set()
    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)


def test_processing_queue_pause_resume_and_remove(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "one.pdf").write_bytes(b"%PDF-1.4")
    (pdf_dir / "two.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    queued = enqueue_processing_jobs(["one.pdf", "two.pdf"])

    paused = local_rag_chat.set_queue_paused(True)
    start_result = start_processing_queue()

    assert paused["queue"]["paused"] is True
    assert start_result["status"] == "paused"
    assert processing_queue_status()["items"][0]["status"] == "QUEUED"

    removed = local_rag_chat.remove_queued_job(queued[1]["id"])

    assert removed["status"] == "removed"
    assert processing_queue_status()["items"][1]["status"] == "REMOVED"
    assert (pdf_dir / "two.pdf").exists()

    local_rag_chat.set_queue_paused(False)
    assert processing_queue_status()["paused"] is False


def test_processing_queue_retry_failed_job(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "bad.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    queued = enqueue_processing_jobs(["bad.pdf"])
    start_processing_queue()

    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    retry = local_rag_chat.retry_queue_job(queued[0]["id"])

    assert retry["status"] == "queued_for_retry"
    item = processing_queue_status()["items"][0]
    assert item["status"] == "QUEUED"
    assert item["error"] == ""
    assert item["progress_message"] == "Queued for retry."


def test_processing_queue_retry_partial_validation_job(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "partial.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    def fake_process(filename, force=False, progress_callback=None):
        return {
            "status": "partial_success",
            "failure_stage": "chunk_validation",
            "output_dir": str(tmp_path / "output" / filename),
            "validation_status": "FAILED",
            "chunk_counts": {"generated": 6, "validated": 5, "rejected": 1, "stored": 5},
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 5, "readback_verified": True},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    queued = enqueue_processing_jobs(["partial.pdf"])
    start_processing_queue()

    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    before = processing_queue_status()["items"][0]
    assert before["status"] == "LOCAL_REVIEW_READY"
    assert before["validation_status"] == "FAILED"

    retry = local_rag_chat.retry_queue_job(queued[0]["id"])

    assert retry["status"] == "queued_for_retry"
    item = processing_queue_status()["items"][0]
    assert item["status"] == "QUEUED"
    assert item["progress_message"] == "Queued for retry."


def test_load_job_chunks_reads_completed_output(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    output_dir = tmp_path / "output" / "paper"
    pdf_dir.mkdir()
    output_dir.mkdir(parents=True)
    (pdf_dir / "paper.pdf").write_bytes(b"%PDF-1.4")
    chunks_payload = {"chunks": [{"Topic": "Demo", "Subtopic": "Trial", "Section": "Results", "Reference": "Paper", "Original Text": "Study text."}]}
    (output_dir / "chunks.json").write_text(json.dumps(chunks_payload), encoding="utf-8")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)

    def fake_process(filename, force=False, progress_callback=None):
        return {
            "status": "processed",
            "output_dir": str(output_dir),
            "validation_status": "PASS",
            "chunk_counts": {"generated": 1, "validated": 1, "stored": 1},
            "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1},
            "production_database_upload": database_upload_status(),
        }

    monkeypatch.setattr(local_rag_chat, "process_uploaded_pdf", fake_process)

    queued = enqueue_processing_jobs(["paper.pdf"])
    start_processing_queue()

    deadline = time.time() + 2
    while processing_queue_status()["running"] and time.time() < deadline:
        time.sleep(0.01)

    loaded = local_rag_chat.load_job_chunks(queued[0]["id"])

    assert loaded == chunks_payload


def test_parse_chunks_json_supports_pipeline_lowercase_fields():
    raw = json.dumps(
        {
            "article": {"title": "Demo"},
            "chunks": [
                {
                    "chunk_number": 3,
                    "topic": "IBD",
                    "subtopic": "Crohn disease",
                    "section": "Induction therapy",
                    "reference": "Guideline 2026",
                    "original_text": "Vedolizumab was discussed for induction therapy in adults.",
                }
            ],
        }
    )

    chunks = parse_chunks_json(raw, "chunks.json", "article-output", "PASS")

    assert len(chunks) == 1
    assert chunks[0].chunk_number == 3
    assert chunks[0].section == "Induction therapy"
    assert chunks[0].output_dir == "article-output"
    assert chunks[0].validation_status == "PASS"


def test_parse_chunks_json_supports_admin_panel_capitalized_fields():
    raw = json.dumps(
        [
            {
                "Topic": "Liver",
                "Subtopic": "Cirrhosis",
                "Section": "Ascites management",
                "Reference": "Clinical review",
                "Original Text": "Salt restriction and diuretics were described with clinical qualifications.",
            }
        ]
    )

    chunks = parse_chunks_json(raw, "admin.json")

    assert chunks[0].topic == "Liver"
    assert chunks[0].reference == "Clinical review"


def test_parse_chunks_markdown_supports_pipeline_markdown_shape():
    raw = """# GastroAGI Chunk Output

## Chunk 1

Topic: Pancreas
Subtopic: Acute pancreatitis
Section: Severity assessment
Reference: Example guideline
Original Text:

The guideline describes early severity assessment and organ failure monitoring.

## Chunk 2

Topic: Esophagus
Subtopic: GERD
Section: Maintenance therapy
Reference: Example guideline
Original Text:

Maintenance therapy is discussed for selected patients with relapse.
"""

    chunks = parse_chunks_markdown(raw, "chunks.md")

    assert [chunk.section for chunk in chunks] == ["Severity assessment", "Maintenance therapy"]
    assert chunks[0].original_text.startswith("The guideline describes")


def test_retrieve_chunks_ranks_matching_section_and_text():
    chunks = parse_chunks_json(
        json.dumps(
            [
                {
                    "Topic": "Esophagus",
                    "Subtopic": "GERD",
                    "Section": "Maintenance therapy",
                    "Reference": "Ref A",
                    "Original Text": "Maintenance proton pump inhibitor therapy is described.",
                },
                {
                    "Topic": "Colon",
                    "Subtopic": "Screening",
                    "Section": "Colonoscopy timing",
                    "Reference": "Ref B",
                    "Original Text": "Screening intervals are described.",
                },
            ]
        ),
        "chunks.json",
    )

    hits = retrieve_chunks("maintenance therapy for GERD", chunks)

    assert hits[0].chunk.section == "Maintenance therapy"
    assert "maintenance" in hits[0].matched_terms


def test_build_rag_response_refuses_outside_knowledge_when_no_match():
    chunks = parse_chunks_json(
        json.dumps(
            [
                {
                    "Topic": "Liver",
                    "Subtopic": "Ascites",
                    "Section": "Diuretics",
                    "Reference": "Ref",
                    "Original Text": "The passage discusses diuretic treatment.",
                }
            ]
        ),
        "chunks.json",
    )

    response = build_rag_response("What is the role of biologics in Crohn disease?", chunks)

    assert response["grounding_status"] == "NO_RELEVANT_CHUNKS"
    assert "outside knowledge" in response["answer"]


def test_build_rag_response_returns_citations_for_supported_answer():
    chunks = parse_chunks_json(
        json.dumps(
            [
                {
                    "chunk_number": 8,
                    "Topic": "IBD",
                    "Subtopic": "Crohn disease",
                    "Section": "Biologic therapy",
                    "Reference": "Ref",
                    "Original Text": "Biologic therapy is discussed for adults with moderate disease.",
                }
            ]
        ),
        "chunks.json",
    )

    response = build_rag_response("Which biologic therapy population is discussed?", chunks)

    assert response["grounding_status"] == "ANSWERED_FROM_GENERATED_CHUNKS"
    assert "chunks.json | chunk 8 | Biologic therapy | Ref" in response["answer"]


def test_load_chunk_file_reads_generated_json(tmp_path):
    payload = json.dumps(
        [
            {
                "Topic": "Topic",
                "Subtopic": "Subtopic",
                "Section": "Section",
                "Reference": "Reference",
                "Original Text": "Original evidence text.",
            }
        ]
    )
    path = tmp_path / "chunks.json"
    path.write_text(payload, encoding="utf-8")

    chunks = load_chunk_file(path)

    assert chunks[0].original_text == "Original evidence text."


def test_database_upload_is_explicitly_blocked_until_backend_is_verified():
    status = database_upload_status()

    assert status["enabled"] is False
    assert status["status"] == "BLOCKED_UNVERIFIED_BACKEND"


def test_process_uploaded_pdf_calls_existing_pipeline(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    monkeypatch.setattr(local_rag_chat.pipeline, "load_dotenv", None)
    monkeypatch.setattr(local_rag_chat.pipeline, "configure_logging", lambda: None)

    def fake_process_pdf(path, force, dry_run, test_generation, logger, draft_packages):
        assert draft_packages is True
        return {
            "status": "processed",
            "pdf": str(path),
            "force": force,
            "dry_run": dry_run,
            "test_generation": test_generation,
        }

    monkeypatch.setattr(local_rag_chat.pipeline, "process_pdf", fake_process_pdf)

    result = process_uploaded_pdf("paper.pdf", force=True)

    assert result["status"] == "processed"
    assert result["force"] is True
    assert result["dry_run"] is False
    assert result["test_generation"] is True
    assert result["production_database_upload"]["enabled"] is False
    assert result["local_database"]["status"] == "NOT_STORED"


def test_process_uploaded_pdf_stores_existing_skipped_package(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "output" / "paper"
    output_dir.mkdir(parents=True)
    (output_dir / "chunks.json").write_text(
        json.dumps(
            {
                "article": {"source_file": "paper.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Topic",
                        "subtopic": "Subtopic",
                        "section": "Section",
                        "reference": "Reference",
                        "original_text": "Source-grounded text for review.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    monkeypatch.setattr(local_rag_chat.pipeline, "load_dotenv", None)
    monkeypatch.setattr(local_rag_chat.pipeline, "configure_logging", lambda: None)
    monkeypatch.setattr(
        local_rag_chat.pipeline,
        "process_pdf",
        lambda *args, **kwargs: {
            "status": "skipped",
            "output_dir": str(output_dir),
            "validation_status": "REVIEW_REQUIRED",
            "reason": "valid_identical_output_exists",
        },
    )
    captured = {}

    def fake_ingest(chunks_path, validation_status):
        captured["chunks_path"] = chunks_path
        captured["validation_status"] = validation_status
        return {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1}

    monkeypatch.setattr(local_rag_chat, "ingest_chunks_file", fake_ingest)

    result = process_uploaded_pdf("paper.pdf")

    assert result["status"] == "skipped"
    assert result["local_database"]["status"] == "LOCAL_REVIEW_DB_STORED"
    assert result["local_database"]["chunks_seen"] == 1



def test_unverified_skipped_result_is_not_stored_in_local_db(monkeypatch, tmp_path):
    output_dir = tmp_path / "output" / "paper"
    output_dir.mkdir(parents=True)
    (output_dir / "chunks.json").write_text(
        json.dumps(
            {
                "article": {"source_file": "paper.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Topic",
                        "subtopic": "Subtopic",
                        "section": "Section",
                        "reference": "Reference",
                        "original_text": "Source-grounded text for review.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Unverified skipped output must not be stored.")

    monkeypatch.setattr(local_rag_chat, "ingest_chunks_file", fail_if_called)

    result = local_rag_chat.store_generated_chunks_for_result(
        {"status": "skipped", "reason": "legacy_or_unverified_skip", "output_dir": str(output_dir)}
    )

    assert result["status"] == "NOT_STORED"


def test_backend_disabled_while_processed_conversion_stores_local_review_chunks(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "input_pdfs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "output" / "paper"
    output_dir.mkdir(parents=True)
    (output_dir / "chunks.json").write_text(
        json.dumps(
            {
                "article": {"source_file": "paper.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Topic",
                        "subtopic": "Subtopic",
                        "section": "Section",
                        "reference": "Reference",
                        "original_text": "Source-grounded text for review.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_rag_chat, "PDF_UPLOAD_DIR", pdf_dir)
    monkeypatch.setattr(local_rag_chat.pipeline, "load_dotenv", None)
    monkeypatch.setattr(local_rag_chat.pipeline, "configure_logging", lambda: None)
    monkeypatch.setattr(
        local_rag_chat.pipeline,
        "process_pdf",
        lambda *args, **kwargs: {
            "status": "processed",
            "output_dir": str(output_dir),
            "validation_status": "PASS",
        },
    )

    def fake_ingest(chunks_path, validation_status):
        return {"status": "LOCAL_REVIEW_DB_STORED", "chunks_seen": 1, "validation_status": validation_status}

    monkeypatch.setattr(local_rag_chat, "ingest_chunks_file", fake_ingest)

    result = process_uploaded_pdf("paper.pdf")

    assert result["status"] == "processed"
    assert result["production_database_upload"]["status"] == "BLOCKED_UNVERIFIED_BACKEND"
    assert result["production_database_upload"]["enabled"] is False
    assert result["local_database"]["status"] == "LOCAL_REVIEW_DB_STORED"
    assert result["local_database"]["validation_status"] == "PASS"


def test_store_generated_chunks_uses_available_test_db_with_verified_readback(monkeypatch, tmp_path):
    output_dir = tmp_path / "output" / "paper"
    output_dir.mkdir(parents=True)
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "paper.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Stress ulcer prophylaxis",
                        "subtopic": "PEPTIC trial",
                        "section": "Results",
                        "reference": "PEPTIC trial report",
                        "original_text": "The trial compared proton pump inhibitors with histamine-2 receptor blockers.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Stress ulcer prophylaxis",
                        "subtopic": "Safety",
                        "section": "Adverse events",
                        "reference": "PEPTIC trial report",
                        "original_text": "Safety outcomes were reported from the source article for clinical review.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "local_review.sqlite3"

    monkeypatch.setattr(
        local_rag_chat,
        "ingest_chunks_file",
        lambda chunks_path, validation_status: local_chunk_store.ingest_chunks_file(
            chunks_path, validation_status, db_path=db_path
        ),
    )
    monkeypatch.setattr(
        local_rag_chat,
        "verify_chunks_file_ingested",
        lambda chunks_path, validation_status: local_chunk_store.verify_chunks_file_ingested(
            chunks_path, validation_status, db_path=db_path
        ),
    )

    first = local_rag_chat.store_generated_chunks_for_result(
        {"status": "processed", "output_dir": str(output_dir), "validation_status": "PASS"}
    )
    second = local_rag_chat.store_generated_chunks_for_result(
        {"status": "processed", "output_dir": str(output_dir), "validation_status": "PASS"}
    )

    assert first["status"] == "LOCAL_REVIEW_DB_STORED"
    assert first["readback_verified"] is True
    assert first["stored_chunks_verified"] == 2
    assert second["status"] == "LOCAL_REVIEW_DB_STORED"
    assert second["unchanged"] == 2
    assert second["readback_verified"] is True



def test_partial_success_stores_only_validated_chunk_subset(monkeypatch, tmp_path):
    output_dir = tmp_path / "output" / "paper"
    output_dir.mkdir(parents=True)
    all_chunks = {
        "article": {"source_file": "paper.pdf", "reference": "doi:10.1000/example", "number_of_chunks": 2},
        "chunks": [
            {"chunk_number": 1, "topic": "Topic", "subtopic": "Subtopic", "section": "Valid", "reference": "doi:10.1000/example", "original_text": "Valid source-grounded text."},
            {"chunk_number": 2, "topic": "Topic", "subtopic": "Subtopic", "section": "Rejected", "reference": "doi:10.1000/example", "original_text": "Rejected text."},
        ],
    }
    valid_chunks = {**all_chunks, "chunks": [all_chunks["chunks"][0]]}
    (output_dir / "chunks.json").write_text(json.dumps(all_chunks), encoding="utf-8")
    (output_dir / "validated_chunks.json").write_text(json.dumps(valid_chunks), encoding="utf-8")
    db_path = tmp_path / "local_review.sqlite3"

    monkeypatch.setattr(
        local_rag_chat,
        "ingest_chunks_file",
        lambda chunks_path, validation_status: local_chunk_store.ingest_chunks_file(
            chunks_path, validation_status, db_path=db_path
        ),
    )
    monkeypatch.setattr(
        local_rag_chat,
        "verify_chunks_file_ingested",
        lambda chunks_path, validation_status: local_chunk_store.verify_chunks_file_ingested(
            chunks_path, validation_status, db_path=db_path
        ),
    )

    result = local_rag_chat.store_generated_chunks_for_result(
        {
            "status": "partial_success",
            "output_dir": str(output_dir),
            "validated_chunks_path": str(output_dir / "validated_chunks.json"),
            "validation_status": "FAILED",
        }
    )

    assert result["status"] == "LOCAL_REVIEW_DB_STORED"
    assert result["chunks_seen"] == 1
    assert result["validation_status"] == "REVIEW_REQUIRED"
    assert result["readback_verified"] is True
def test_admin_and_bot_pages_are_separate():
    admin = local_rag_chat.render_admin_page().decode("utf-8")
    bot = local_rag_chat.render_bot_page().decode("utf-8")

    assert "Chunks Admin Panel" in admin
    assert "PDF Upload" in admin
    assert 'multiple required' in admin
    assert "Upload up to 30 PDFs" in admin
    assert "Processing Queue" in admin
    assert "Start Generating" in admin
    assert "Pause After Current PDF" in admin
    assert "Resume Queue" in admin
    assert "View Generated Chunks" in admin
    assert "Retry Job" in admin
    assert "Remove Queued File" in admin
    assert "/queue/start" in admin
    assert "/queue/pause" in admin
    assert "/queue/resume" in admin
    assert "/queue/retry" in admin
    assert "/queue/remove" in admin
    assert "/api/job-chunks" in admin
    assert "/api/queue" in admin
    assert "Queue position" in admin
    assert "Passed / Review" in admin
    assert "Stored chunks" in admin
    assert "overallProgressBar" in admin
    assert "currentProgressBar" in admin
    assert "GENERATING_CHUNKS" in admin
    assert "Elapsed:" in admin
    assert 'Check RAG Response' not in admin
    assert 'Clinical Assistant' not in admin
    assert "BOT Page" in bot
    assert 'Clinical Assistant' in bot
    assert 'Evidence Packages' not in bot
    assert 'Available Chunk Packages' not in bot
    assert 'packageSearch' not in bot
    assert 'function filterPackages()' not in bot
    assert 'class="chat-page"' in bot
    assert '/chat-stream' in bot
    assert 'id="stopButton"' in bot
    assert 'id="clearChat"' in bot
    assert 'Regenerate' in bot
    assert 'navigator.clipboard.writeText' in bot
    assert 'AbortController' in bot
    assert 'aria-live="polite"' in bot
    assert 'function renderFormatted' in bot
    assert 'References' in bot
    assert 'review status' in bot
    assert "PDF Upload" not in bot


def test_chunk_package_metadata_marks_failed_package_as_partial(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    package_dir = output_dir / "clinical-paper"
    package_dir.mkdir(parents=True)
    chunks = [
        {
            "chunk_number": index,
            "topic": "Chronic Pancreatitis",
            "subtopic": "Drainage comparison",
            "section": f"Section {index}",
            "reference": "doi:10.1000/example",
            "original_text": "Source-grounded clinical evidence for reviewer assessment.",
        }
        for index in range(1, 4)
    ]
    payload = {
        "article": {
            "source_file": "clinical-paper.pdf",
            "topic": "Chronic Pancreatitis",
            "subtopic": "Drainage comparison",
        },
        "chunks": chunks,
    }
    (package_dir / "chunks.json").write_text(json.dumps(payload), encoding="utf-8")
    (package_dir / "local_review_chunks.json").write_text(
        json.dumps({**payload, "chunks": chunks[:2]}), encoding="utf-8"
    )
    (package_dir / "validation_report.json").write_text(
        json.dumps({"status": "FAILED", "issues": []}), encoding="utf-8"
    )
    monkeypatch.setattr(local_rag_chat, "PROJECT_ROOT", tmp_path)

    loaded_chunks, packages = local_rag_chat.load_generated_chunks(output_dir)

    assert len(loaded_chunks) == 3
    assert packages == [
        {
            "filename": "output\\clinical-paper\\chunks.json",
            "chunk_count": 3,
            "status": "loaded",
            "validation_status": "FAILED",
            "message": "",
            "source_file": "clinical-paper.pdf",
            "topic": "Chronic Pancreatitis",
            "subtopic": "Drainage comparison",
            "reviewable_count": 2,
            "rejected_count": 1,
            "availability": "partial",
        }
    ]


def test_index_links_to_admin_and_bot_pages():
    index = local_rag_chat.render_index().decode("utf-8")

    assert 'href="/admin"' in index
    assert 'href="/bot"' in index












