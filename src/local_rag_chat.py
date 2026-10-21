"""Local PDF-to-chunks review chat for GastroAGI.

This localhost server queues PDF uploads and lets reviewers ask source-bound
questions against generated draft chunks in the local SQLite review database.
Production upload remains blocked until the backend contract is verified.
"""

from __future__ import annotations

from . import diagnostics as telemetry

import argparse
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
import html
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from . import pipeline
from .queue_recovery import RETRY_DELAYS, recovery_decision
from .local_chunk_store import (
    build_db_rag_response,
    stream_db_rag_response,
    LOCAL_DB_PATH,
    db_status as local_db_status,
    ingest_chunks_file,
    verify_chunks_file_ingested,
    extract_relevant_excerpt as stored_excerpt,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PDF_UPLOAD_DIR = PROJECT_ROOT / "input_pdfs"
OUTPUT_DIR = PROJECT_ROOT / "output"
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
MAX_UPLOAD_FILES = 30
PDF_EXTENSION = ".pdf"
QUEUE_ACTIVE_STAGES = {
    "EXTRACTING",
    "PAGES_EXTRACTED",
    "CLEANING",
    "CLEANING_COMPLETED",
    "GENERATING_CHUNKS",
    "CHUNK_BATCH_COMPLETED",
    "VALIDATING",
    "CHUNKS_VALIDATED",
    "SAVING_OUTPUT",
    "JSON_SAVED",
    "STORING_DATABASE",
    "DATABASE_INGESTION_COMPLETED",
    "VERIFYING_STORAGE",
}
QUEUE_TERMINAL_STAGES = {"COMPLETED", "FAILED", "ACTION_REQUIRED", "SOURCE_REVIEW_REQUIRED", "LOCAL_REVIEW_READY", "BLOCKED_STORAGE", "SKIPPED", "REMOVED"}
QUEUE_STALE_SECONDS = 120
QUEUE_INSTANCE = uuid4().hex


class LocalWorkflowError(ValueError):
    """Raised when a local PDF/chunk workflow request is invalid."""


class ChunkFileError(ValueError):
    """Raised when a generated chunk file cannot be parsed safely."""


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content: bytes


@dataclass(frozen=True)
class RejectedUpload:
    filename: str
    error: str


@dataclass(frozen=True)
class ParsedUploadBatch:
    uploads: list[UploadedFile]
    rejected: list[RejectedUpload]


@dataclass
class ProcessingQueueItem:
    id: str
    filename: str
    status: str
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class RagChunk:
    source_file: str
    chunk_number: int | None
    topic: str
    subtopic: str
    section: str
    reference: str
    original_text: str
    output_dir: str = ""
    validation_status: str = "UNKNOWN"

    @property
    def citation_label(self) -> str:
        number = f"chunk {self.chunk_number}" if self.chunk_number is not None else "chunk"
        parts = [self.source_file, number]
        if self.section:
            parts.append(self.section)
        if self.reference:
            parts.append(self.reference)
        return " | ".join(parts)


@dataclass(frozen=True)
class RetrievalHit:
    chunk: RagChunk
    score: float
    matched_terms: tuple[str, ...]
    excerpt: str


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "with", "what", "when", "where", "which", "who", "why",
}
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")
CHUNK_HEADING_RE = re.compile(r"^##\s+Chunk\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def secure_pdf_filename(filename: str, max_length: int = 120) -> str:
    name = Path(filename or "").name.strip()
    if not name:
        raise LocalWorkflowError("Missing file name.")
    if Path(name).suffix.lower() != PDF_EXTENSION:
        raise LocalWorkflowError("Only PDF uploads are supported in this workflow.")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    suffix = Path(safe).suffix or PDF_EXTENSION
    if len(safe) > max_length:
        stem_limit = max(16, max_length - len(suffix))
        safe = Path(safe).stem[:stem_limit].rstrip("._-") + suffix
    if safe in {".", ".."}:
        raise LocalWorkflowError("Invalid file name.")
    return safe


def parse_multipart_pdf_upload_batch(headers: dict[str, str], body: bytes) -> ParsedUploadBatch:
    content_type = headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise LocalWorkflowError("Expected multipart/form-data upload.")
    raw = b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
    message = BytesParser(policy=email_policy).parsebytes(raw)
    uploads: list[UploadedFile] = []
    rejected: list[RejectedUpload] = []
    saw_upload_field = False
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != "pdf_file":
            continue
        saw_upload_field = True
        raw_filename = Path(part.get_filename() or "").name.strip() or "unnamed upload"
        payload = part.get_payload(decode=True) or b""
        try:
            filename = secure_pdf_filename(raw_filename)
            if not payload:
                raise LocalWorkflowError("Uploaded PDF is empty.")
        except LocalWorkflowError as exc:
            rejected.append(RejectedUpload(filename=raw_filename, error=str(exc)))
            continue
        if len(uploads) >= MAX_UPLOAD_FILES:
            rejected.append(
                RejectedUpload(
                    filename=filename,
                    error=f"Upload batch limit is {MAX_UPLOAD_FILES} PDFs.",
                )
            )
            continue
        uploads.append(UploadedFile(filename=filename, content=payload))
    if not saw_upload_field:
        raise LocalWorkflowError("Upload must include a pdf_file field.")
    return ParsedUploadBatch(uploads=uploads, rejected=rejected)


def parse_multipart_pdf_uploads(headers: dict[str, str], body: bytes) -> list[UploadedFile]:
    batch = parse_multipart_pdf_upload_batch(headers, body)
    if not batch.uploads:
        if batch.rejected:
            reasons = "; ".join(f"{item.filename}: {item.error}" for item in batch.rejected)
            raise LocalWorkflowError(f"No valid PDFs were uploaded. {reasons}")
        raise LocalWorkflowError("Upload must include at least one valid PDF.")
    return batch.uploads


def parse_multipart_pdf_upload(headers: dict[str, str], body: bytes) -> UploadedFile:
    return parse_multipart_pdf_uploads(headers, body)[0]


def unique_upload_path(upload_dir: Path, filename: str) -> Path:
    target = upload_dir / secure_pdf_filename(filename)
    if not target.exists():
        return target
    timestamp = int(time.time())
    for index in range(1, 10_000):
        unique_suffix = f"_{timestamp}_{index}"
        stem_limit = max(16, 120 - len(target.suffix) - len(unique_suffix))
        candidate_stem = target.stem[:stem_limit].rstrip("._-")
        candidate = upload_dir / f"{candidate_stem}{unique_suffix}{target.suffix}"
        if not candidate.exists():
            return candidate
    raise LocalWorkflowError("Could not allocate a unique file name for the uploaded PDF.")


def save_uploaded_pdf(upload: UploadedFile, upload_dir: Path = PDF_UPLOAD_DIR) -> Path:
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = unique_upload_path(upload_dir, upload.filename)
    target.write_bytes(upload.content)
    return target


def list_pdf_inputs(upload_dir: Path = PDF_UPLOAD_DIR) -> list[dict[str, Any]]:
    upload_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(upload_dir.glob("*.pdf")):
        output_dir = OUTPUT_DIR / pipeline.safe_output_dir_name(path.stem)
        records.append(
            {
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "output_dir": output_dir.name,
                "chunks_available": (output_dir / "chunks.json").exists(),
                "validation_status": read_validation_status(output_dir),
            }
        )
    return records


def read_validation_status(output_dir: Path) -> str:
    report_path = output_dir / "validation_report.json"
    if not report_path.exists():
        return "NOT_GENERATED"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "UNKNOWN"
    return str(report.get("status") or "UNKNOWN")


@telemetry.observed_run
def process_uploaded_pdf(
    filename: str,
    force: bool = False,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    safe_name = secure_pdf_filename(filename)
    pdf_path = PDF_UPLOAD_DIR / safe_name
    if not pdf_path.exists():
        raise LocalWorkflowError(f"Uploaded PDF not found: {safe_name}")
    if pipeline.load_dotenv is not None:
        pipeline.load_dotenv(PROJECT_ROOT / ".env")
    process_kwargs: dict[str, Any] = {
        "force": force,
        "dry_run": False,
        "test_generation": True,
        "draft_packages": True,
        "logger": pipeline.configure_logging(),
    }
    if progress_callback is not None:
        process_kwargs["progress_callback"] = progress_callback
    result = pipeline.process_pdf(pdf_path, **process_kwargs)
    # Preserve typed recovery outcomes even when usable chunks survived.
    result["recovery_signals"] = []
    if result.get("output_dir"):
        try:
            report = json.loads((Path(result["output_dir"])/"validation_report.json").read_text(encoding="utf-8"))
            diagnostics = report.get("diagnostics") or {}
            result["recovery_signals"] = list(diagnostics.get("unit_failures") or []) + list(
                (diagnostics.get("visual_recovery_outcome") or {}).get("failures") or [])
        except (OSError, ValueError):
            pass
    result["production_database_upload"] = database_upload_status()
    if progress_callback is not None:
        progress_callback("STORING_DATABASE", {"message": "Local database storage started."})
    result["local_database"] = store_generated_chunks_for_result(result)
    result["chunk_counts"] = build_chunk_count_summary(result)
    if progress_callback is not None:
        local_status = result["local_database"].get("status") if isinstance(result.get("local_database"), dict) else "UNKNOWN"
        progress_callback("DATABASE_INGESTION_COMPLETED", {"message": f"Local database storage completed: {local_status}.", "local_database_status": local_status})
        progress_callback("VERIFYING_STORAGE", {"message": "Verifying storage and production backend state."})
    return result


def database_upload_status() -> dict[str, Any]:
    return {
        "status": "BLOCKED_UNVERIFIED_BACKEND",
        "enabled": False,
        "reason": (
            "No verified GastroAGI production database ingestion endpoint, authentication model, "
            "metadata contract, embedding model, collection name, or duplicate policy exists in this repository."
        ),
    }


def store_generated_chunks_for_result(result: dict[str, Any]) -> dict[str, Any]:
    """Store generated or verified existing chunks in the local review DB when safe."""
    output_dir_value = result.get("output_dir")
    status = result.get("status")
    is_verified_existing = status == "skipped" and result.get("reason") == "valid_identical_output_exists"
    is_partial_success = status == "partial_success"
    if status != "processed" and not is_verified_existing and not is_partial_success:
        return {"status": "NOT_STORED", "enabled": False, "reason": "Pipeline did not produce a chunk package."}
    if not output_dir_value:
        return {"status": "NOT_STORED", "enabled": False, "reason": "Pipeline did not report an output directory."}
    output_dir = Path(str(output_dir_value))
    chunks_path = Path(str(result.get("validated_chunks_path") or (output_dir / "validated_chunks.json"))) if is_partial_success else output_dir / "chunks.json"
    if not chunks_path.exists():
        filename = "validated_chunks.json" if is_partial_success else "chunks.json"
        return {"status": "NOT_STORED", "enabled": False, "reason": f"{filename} was not produced."}
    validation_status = (
        "REVIEW_REQUIRED"
        if is_partial_success
        else str(result.get("validation_status") or read_validation_status(output_dir))
    )
    ingest_result = ingest_chunks_file(chunks_path, validation_status=validation_status)
    if ingest_result.get("status") != "LOCAL_REVIEW_DB_STORED":
        return ingest_result
    readback = verify_chunks_file_ingested(chunks_path, validation_status=validation_status)
    if readback.get("verified") is True:
        telemetry.count("final_stored_rows", int(readback.get("stored_chunks") or 0), replace=True)
    else:
        telemetry.count("storage_readback_unverified")
    return ingest_result | {
        "validation_status": validation_status,
        "readback": readback,
        "readback_verified": bool(readback.get("verified")),
        "stored_chunks_verified": int(readback.get("stored_chunks") or 0),
    }


def build_chunk_count_summary(result: dict[str, Any]) -> dict[str, int]:
    existing_counts = result.get("chunk_counts") if isinstance(result.get("chunk_counts"), dict) else {}
    output_dir_value = result.get("output_dir")
    generated = int(existing_counts.get("generated") or result.get("chunks") or 0)
    report: dict[str, Any] = {}
    if output_dir_value:
        try:
            loaded = json.loads((Path(str(output_dir_value)) / "validation_report.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                report = loaded
        except (OSError, ValueError):
            pass
        chunks_path = Path(str(output_dir_value)) / "chunks.json"
        if chunks_path.exists() and result.get("status") != "failed":
            try:
                payload = json.loads(chunks_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("chunks"), list):
                    generated = len(payload["chunks"])
            except (OSError, json.JSONDecodeError):
                pass
    local_db = result.get("local_database") if isinstance(result.get("local_database"), dict) else {}
    readback = local_db.get("readback") if isinstance(local_db.get("readback"), dict) else {}
    stored = int(readback["stored_chunks"] if "stored_chunks" in readback
                 else local_db.get("chunks_seen") or existing_counts.get("stored") or 0)
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if summary:
        generated = int(summary.get("generated_candidates", summary.get("chunks", generated)))
    supported = len(pipeline.eligible_chunk_numbers(report, {"PASS"})) if report else int(existing_counts.get("source_supported") or 0)
    review = len(pipeline.eligible_chunk_numbers(report, {"REVIEW_REQUIRED"})) if report else int(existing_counts.get("review_required") or 0)
    rejected = len(pipeline.eligible_chunk_numbers(report, {"REJECTED"})) if report else int(existing_counts.get("rejected") or 0)
    structural = int(summary.get("structurally_valid_chunks", existing_counts.get("structurally_valid", 0)))
    return {"generated": generated, "structurally_valid": structural, "source_supported": supported,
            "validated": supported, "review_required": review, "rejected": rejected, "stored": stored,
            "clinically_approved": 0}


_queue_worker_running = False
_queue_worker_thread: threading.Thread | None = None
_queue_start_lock = threading.RLock()


def init_processing_queue_db(db_path: Path = LOCAL_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table if not exists pdf_processing_queue (
                id text primary key,
                filename text not null,
                status text not null,
                submitted_at real not null,
                started_at real,
                finished_at real,
                updated_at real not null,
                result_json text,
                error text not null default '',
                failure_stage text not null default '',
                worker_id text not null default '',
                progress_message text not null default '',
                progress_detail_json text not null default '{}',
                page_count integer,
                generated_chunks integer not null default 0,
                validated_chunks integer not null default 0,
                stored_chunks integer not null default 0
            )
            """
        )
        _ensure_queue_column(conn, "progress_message", "text not null default ''")
        _ensure_queue_column(conn, "progress_detail_json", "text not null default '{}'")
        _ensure_queue_column(conn, "page_count", "integer")
        _ensure_queue_column(conn, "generated_chunks", "integer not null default 0")
        _ensure_queue_column(conn, "validated_chunks", "integer not null default 0")
        _ensure_queue_column(conn, "stored_chunks", "integer not null default 0")
        _ensure_queue_column(conn, "attempt_count", "integer not null default 0")
        _ensure_queue_column(conn, "next_attempt_at", "real")
        _ensure_queue_column(conn, "last_error_code", "text not null default ''")
        conn.execute(
            """
            create table if not exists pdf_queue_control (
                key text primary key,
                value text not null
            )
            """
        )
        conn.execute("create index if not exists idx_pdf_queue_status on pdf_processing_queue(status)")
        conn.execute("create index if not exists idx_pdf_queue_fifo on pdf_processing_queue(submitted_at)")


def _ensure_queue_column(conn: sqlite3.Connection, column_name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute("pragma table_info(pdf_processing_queue)").fetchall()}
    if column_name not in columns:
        conn.execute(f"alter table pdf_processing_queue add column {column_name} {definition}")


def _queue_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    detail = json.loads(row["progress_detail_json"] or "{}")
    now = time.time()
    elapsed_start = row["started_at"] or row["submitted_at"]
    elapsed_end = row["finished_at"] or now
    payload: dict[str, Any] = {
        "id": row["id"],
        "filename": row["filename"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        "failure_stage": row["failure_stage"],
        "worker_id": row["worker_id"],
        "progress_message": row["progress_message"],
        "progress_detail": detail,
        "elapsed_seconds": max(0.0, elapsed_end - elapsed_start),
        "page_count": row["page_count"],
        "generated_chunks": row["generated_chunks"],
        "validated_chunks": row["validated_chunks"],
        "stored_chunks": row["stored_chunks"],
        "attempt_count": row["attempt_count"],
        "next_attempt_at": row["next_attempt_at"],
        "last_error_code": row["last_error_code"],
    }
    if result is not None:
        payload["result"] = result
        payload["pipeline_status"] = result.get("status")
        payload["validation_status"] = result.get("validation_status")
        payload["output_dir"] = result.get("output_dir")
        payload["local_database"] = result.get("local_database")
        payload["production_database_upload"] = result.get("production_database_upload")
    return payload


def _load_queue_item(job_id: str, db_path: Path = LOCAL_DB_PATH) -> ProcessingQueueItem | None:
    init_processing_queue_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from pdf_processing_queue where id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return ProcessingQueueItem(
        id=row["id"],
        filename=row["filename"],
        status=row["status"],
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=result,
        error=row["error"],
    )


def enqueue_processing_jobs(filenames: list[str]) -> list[dict[str, Any]]:
    init_processing_queue_db()
    now = time.time()
    rows: list[tuple[str, str, str, float, float]] = []
    for filename in filenames:
        safe_name = secure_pdf_filename(filename)
        if not (PDF_UPLOAD_DIR / safe_name).exists():
            raise LocalWorkflowError(f"Uploaded PDF not found: {safe_name}")
        rows.append((uuid4().hex, safe_name, "QUEUED", now, now))
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.executemany(
            """
            insert into pdf_processing_queue (
                id, filename, status, submitted_at, updated_at, progress_message, progress_detail_json
            )
            values (?, ?, ?, ?, ?, 'Upload completed. Waiting in queue.', '{"event":"upload_completed"}')
            """,
            rows,
        )
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in rows)
        selected = conn.execute(
            f"select * from pdf_processing_queue where id in ({placeholders}) order by submitted_at, rowid",
            tuple(row[0] for row in rows),
        ).fetchall()
    return [_queue_row_payload(row) for row in selected]


def processing_queue_status() -> dict[str, Any]:
    init_processing_queue_db()
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from pdf_processing_queue order by submitted_at, rowid").fetchall()
    items = [_queue_row_payload(row) for row in rows]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    active_count = sum(counts.get(stage, 0) for stage in QUEUE_ACTIVE_STAGES)
    waiting_count = counts.get("RETRY_WAIT", 0)
    paused = is_queue_paused()
    running = active_count > 0 or _queue_worker_running
    return {
        "status": "running" if running else "idle",
        "running": running,
        "paused": paused,
        "items": items,
        "counts": counts,
        "queued_count": counts.get("QUEUED", 0),
        "active_count": active_count,
        "waiting_count": waiting_count,
        "terminal_count": len(items) - counts.get("QUEUED", 0) - active_count - waiting_count,
    }


def is_queue_paused() -> bool:
    init_processing_queue_db()
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        row = conn.execute("select value from pdf_queue_control where key = 'paused'").fetchone()
    return bool(row and row[0] == "1")


def set_queue_paused(paused: bool) -> dict[str, Any]:
    init_processing_queue_db()
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.execute(
            """
            insert into pdf_queue_control (key, value) values ('paused', ?)
            on conflict(key) do update set value=excluded.value
            """,
            ("1" if paused else "0",),
        )
    return {"status": "paused" if paused else "resumed", "queue": processing_queue_status()}


def start_processing_queue() -> dict[str, Any]:
    with _queue_start_lock:
        return _start_processing_queue_locked()


def _start_processing_queue_locked() -> dict[str, Any]:
    global _queue_worker_running, _queue_worker_thread
    recovered = recover_stale_queue_jobs()
    if is_queue_paused():
        return {"status": "paused", "recovered_stale_jobs": recovered, "queue": processing_queue_status()}
    if _queue_worker_running:
        return {"status": "already_running", "recovered_stale_jobs": recovered, "queue": processing_queue_status()}
    initial_item = _claim_next_queue_item()
    if initial_item is None:
        status = processing_queue_status()
        return {
            "status": "already_running" if status["active_count"] else "no_queued_items",
            "recovered_stale_jobs": recovered,
            "queue": status,
        }
    _queue_worker_running = True
    _queue_worker_thread = threading.Thread(
        target=_process_queue_worker,
        args=(initial_item,),
        name="gastroagi-pdf-queue",
        daemon=True,
    )
    _queue_worker_thread.start()
    return {"status": "started", "recovered_stale_jobs": recovered, "queue": processing_queue_status()}


def recover_stale_queue_jobs(max_age_seconds: int = QUEUE_STALE_SECONDS) -> int:
    """Return stale active jobs to QUEUED after a conservative timeout."""
    if _queue_worker_running:
        return 0
    init_processing_queue_db()
    cutoff = time.time() - max_age_seconds
    active_stages = tuple(QUEUE_ACTIVE_STAGES)
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        placeholders = ",".join("?" for _ in active_stages)
        cursor = conn.execute(
            f"""
            update pdf_processing_queue
            set status = case when attempt_count >= 5 then 'ACTION_REQUIRED' else 'QUEUED' end,
                finished_at = case when attempt_count >= 5 then ? else null end,
                updated_at = ?,
                error = '',
                failure_stage = '',
                worker_id = '',
                progress_message = case when attempt_count >= 5 then 'Repeatedly interrupted job needs attention; completed drafts are preserved.'
                    else 'Recovered interrupted job; resuming saved work automatically.' end,
                progress_detail_json = ?
            where status in ({placeholders}) and updated_at < ?
            """,
            (
                time.time(),
                time.time(),
                json.dumps({"event": "stale_job_recovered", "stale_after_seconds": max_age_seconds}),
                *active_stages,
                cutoff,
            ),
        )
        return int(cursor.rowcount or 0)


def retry_queue_job(job_id: str) -> dict[str, Any]:
    init_processing_queue_db()
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        row = conn.execute("select status, result_json from pdf_processing_queue where id = ?", (job_id,)).fetchone()
        if row is None:
            raise LocalWorkflowError("Queue job not found.")
        if not _queue_job_is_retryable(row[0], row[1]):
            raise LocalWorkflowError("Only failed or partially failed jobs can be retried.")
        conn.execute(
            """
            update pdf_processing_queue
            set status='QUEUED', started_at=null, finished_at=null, updated_at=?,
                error='', failure_stage='', worker_id='',
                attempt_count=0, next_attempt_at=null, last_error_code='',
                progress_message='Queued for retry.', progress_detail_json='{"event":"retry_queued"}'
            where id=?
            """,
            (time.time(), job_id),
        )
    return {"status": "queued_for_retry", "queue": processing_queue_status()}


def _queue_job_is_retryable(status: str, result_json: str | None) -> bool:
    if status in {"FAILED","ACTION_REQUIRED","SOURCE_REVIEW_REQUIRED","BLOCKED_STORAGE"}:
        return True
    if status != "LOCAL_REVIEW_READY":
        return False
    try:
        result = json.loads(result_json or "{}")
    except json.JSONDecodeError:
        return False
    return (
        str(result.get("status") or "").lower() == "partial_success"
        or str(result.get("validation_status") or "").upper() == "FAILED"
    )


def remove_queued_job(job_id: str) -> dict[str, Any]:
    init_processing_queue_db()
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        row = conn.execute("select status from pdf_processing_queue where id = ?", (job_id,)).fetchone()
        if row is None:
            raise LocalWorkflowError("Queue job not found.")
        if row[0] != "QUEUED":
            raise LocalWorkflowError("Only queued jobs can be removed before processing.")
        conn.execute(
            """
            update pdf_processing_queue
            set status='REMOVED', finished_at=?, updated_at=?,
                progress_message='Removed from queue; uploaded PDF preserved on disk.',
                progress_detail_json='{"event":"queued_job_removed","file_preserved":true}'
            where id=?
            """,
            (time.time(), time.time(), job_id),
        )
    return {"status": "removed", "queue": processing_queue_status()}


def load_job_chunks(job_id: str) -> dict[str, Any]:
    item = _load_queue_item(job_id)
    if item is None or not item.result:
        raise LocalWorkflowError("No generated chunks are available for this job.")
    output_dir = item.result.get("output_dir")
    if not output_dir:
        raise LocalWorkflowError("Job did not report an output directory.")
    chunks_path = Path(str(output_dir)) / "chunks.json"
    if not chunks_path.exists():
        raise LocalWorkflowError("chunks.json was not found for this job.")
    return json.loads(chunks_path.read_text(encoding="utf-8"))


def _claim_next_queue_item() -> ProcessingQueueItem | None:
    init_processing_queue_db()
    if is_queue_paused():
        return None
    worker_id = QUEUE_INSTANCE + ":" + uuid4().hex
    now = time.time()
    with sqlite3.connect(LOCAL_DB_PATH, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("begin immediate")
        try:
            active = conn.execute(
                f"select id from pdf_processing_queue where status in ({','.join('?' for _ in QUEUE_ACTIVE_STAGES)}) limit 1",
                tuple(QUEUE_ACTIVE_STAGES),
            ).fetchone()
            if active is not None:
                conn.execute("commit")
                return None
            row = conn.execute(
                "select * from pdf_processing_queue where status = 'QUEUED' or "
                "(status = 'RETRY_WAIT' and next_attempt_at <= ?) order by submitted_at, rowid limit 1", (now,)
            ).fetchone()
            if row is None:
                conn.execute("commit")
                return None
            conn.execute(
                """
                update pdf_processing_queue
                set status = 'EXTRACTING', started_at = coalesce(started_at, ?),
                    updated_at = ?, error = '', failure_stage = '', worker_id = ?,
                    attempt_count = attempt_count + 1, next_attempt_at = null,
                    progress_message = 'Extraction started.',
                    progress_detail_json = '{"event":"extraction_started"}'
                where id = ?
                """,
                (now, now, worker_id, row["id"]),
            )
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise
    return _load_queue_item(row["id"])


def _update_queue_stage(item_id: str, stage: str, detail: dict[str, Any] | None = None) -> None:
    init_processing_queue_db()
    detail = detail or {}
    message = str(detail.get("message") or _default_progress_message(stage))
    page_count = detail.get("page_count")
    generated = detail.get("chunks_generated")
    validated = detail.get("chunks_validated")
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.execute(
            """
            update pdf_processing_queue
            set status = ?, updated_at = ?, error = '', failure_stage = '',
                progress_message = ?, progress_detail_json = ?,
                page_count = coalesce(?, page_count),
                generated_chunks = case when ? is null then generated_chunks else ? end,
                validated_chunks = case when ? is null then validated_chunks else ? end
            where id = ?
            """,
            (
                stage,
                time.time(),
                message,
                json.dumps(detail, ensure_ascii=False),
                page_count,
                generated,
                generated,
                validated,
                validated,
                item_id,
            ),
        )


def _default_progress_message(stage: str) -> str:
    return stage.replace("_", " ").title() + "."


def _finish_queue_item(item_id: str, status: str, result: dict[str, Any] | None = None, error: str = "") -> None:
    init_processing_queue_db()
    failure_stage = ""
    counts = {"generated": 0, "validated": 0, "stored": 0}
    if result is not None:
        failure_stage = str(result.get("failure_stage") or "")
        if isinstance(result.get("chunk_counts"), dict):
            counts = {
                "generated": int(result["chunk_counts"].get("generated") or 0),
                "validated": int(result["chunk_counts"].get("validated") or 0),
                "stored": int(result["chunk_counts"].get("stored") or 0),
            }
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.execute(
            """
            update pdf_processing_queue
            set status = ?, result_json = ?, error = ?, failure_stage = ?,
                finished_at = ?, updated_at = ?, progress_message = ?,
                progress_detail_json = ?,
                generated_chunks = ?,
                validated_chunks = ?,
                stored_chunks = ?
            where id = ?
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error,
                failure_stage,
                time.time(),
                time.time(),
                _terminal_progress_message(status, result, error),
                json.dumps(_terminal_progress_detail(status, result, error), ensure_ascii=False),
                counts["generated"],
                counts["validated"],
                counts["stored"],
                item_id,
            ),
        )


def _process_queue_worker(initial_item: ProcessingQueueItem | None = None) -> None:
    global _queue_worker_running
    try:
        item = initial_item
        while True:
            if item is None:
                return
            try:
                result = process_uploaded_pdf(
                    item.filename,
                    force=False,
                    progress_callback=lambda stage, detail=None, item_id=item.id: _update_queue_stage(item_id, stage, detail or {}),
                )
            except Exception as exc:  # noqa: BLE001 - queue status must preserve unexpected pipeline failures.
                code = getattr(exc, "code", "")
                diagnostics = getattr(exc, "diagnostics", {})
                retryable = isinstance(exc, (TimeoutError, ConnectionError)) or (
                    isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower())
                # Never persist arbitrary exception bodies (credentials or source text).
                _complete_queue_attempt(item.id, {"status":"failed", "failure_code":code or type(exc).__name__,
                    "failure_classification":telemetry.failure(exc, code or "UNCLASSIFIED_ERROR"),
                    "observability":getattr(exc, "observability", None),
                    "retryable":retryable or (diagnostics.get("error") or {}).get("retryable") is True,
                    "reason":"Processing was interrupted. Completed source attempts are retained."})
                item = _claim_next_queue_item()
                continue
            if "chunk_counts" not in result:
                result["chunk_counts"] = build_chunk_count_summary(result)
            _complete_queue_attempt(item.id, result)
            item = _claim_next_queue_item()
    finally:
        with _queue_start_lock:
            _queue_worker_running = False


def _complete_queue_attempt(item_id: str, result: dict[str, Any]) -> None:
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        row = conn.execute("select result_json from pdf_processing_queue where id=?", (item_id,)).fetchone()
    previous = json.loads(row[0]) if row and row[0] else {}
    if previous.get("chunk_counts", {}).get("stored", 0) > result.get("chunk_counts", {}).get("stored", 0):
        # Keep access to already stored drafts when a later attempt fails sooner.
        result = dict(result)
        for key in ("output_dir", "chunk_counts", "local_database"):
            if key in previous:
                result[key] = previous[key]
    decision = recovery_decision(result)
    if decision:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            attempt = conn.execute("select attempt_count from pdf_processing_queue where id=?", (item_id,)).fetchone()[0]
        if decision.retry and attempt <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[max(0, attempt-1)]
            _finish_queue_item(item_id, "RETRY_WAIT", result=result)
            with sqlite3.connect(LOCAL_DB_PATH) as conn:
                conn.execute("update pdf_processing_queue set next_attempt_at=?, finished_at=null, "
                    "last_error_code=?, progress_message=? where id=?",
                    (time.time()+delay, decision.code, decision.message+f" Next attempt in {delay} seconds.", item_id))
            return
        _finish_queue_item(item_id, "ACTION_REQUIRED", result=result)
        message = decision.message if not decision.retry else (
            "Automatic retries exhausted after five attempts. Completed drafts are preserved. Check Azure availability and retry here.")
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("update pdf_processing_queue set last_error_code=?, progress_message=? where id=?",
                         (decision.code, message, item_id))
        return
    _finish_queue_item(item_id, _terminal_queue_status_for_result(result), result=result)
    if result.get("failure_code"):
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("update pdf_processing_queue set last_error_code=? where id=?", (result["failure_code"],item_id))


def queue_supervisor_tick() -> dict[str, Any]:
    """Maintain this process's lease even while a bounded Azure request is running."""
    init_processing_queue_db()
    if _queue_worker_thread is not None and _queue_worker_thread.is_alive():
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute(f"update pdf_processing_queue set updated_at=? where worker_id like ? "
                         f"and status in ({','.join('?' for _ in QUEUE_ACTIVE_STAGES)})",
                         (time.time(), QUEUE_INSTANCE+":%", *QUEUE_ACTIVE_STAGES))
    return start_processing_queue()


def supervise_processing_queue(stop: threading.Event) -> None:
    """Durable jobs survive browser closure and restart; no sleeping worker slots."""
    while not stop.is_set():
        try:
            queue_supervisor_tick()
        except Exception as exc:
            logging.getLogger(__name__).warning("Queue supervisor will retry after %s", type(exc).__name__)
        stop.wait(2)


def _terminal_queue_status_for_result(result: dict[str, Any]) -> str:
    if result.get("status") == "failed" and result.get("failure_code") in {
            "EXTRACTION_BLOCKED", "SOURCE_VALIDATION_FAILED", "REFERENCE_OR_CONTENT_INVALID", "REFERENCE_IDENTITY_UNRESOLVED", "SOURCE_PASSAGE_HELD"}:
        return "SOURCE_REVIEW_REQUIRED"
    if str(result.get("status") or "").lower() == "failed":
        return "SOURCE_REVIEW_REQUIRED" if result.get("failure_stage") in {"extraction", "chunk_validation"} else "ACTION_REQUIRED"
    if str(result.get("status") or "").lower() == "skipped":
        return "SKIPPED"
    local_database = result.get("local_database") if isinstance(result.get("local_database"), dict) else {}
    production_status = result.get("production_database_upload") if isinstance(result.get("production_database_upload"), dict) else {}
    if (
        local_database.get("status") == "LOCAL_REVIEW_DB_STORED"
        and local_database.get("readback_verified") is True
    ):
        return "LOCAL_REVIEW_READY"
    if local_database.get("status") == "LOCAL_REVIEW_DB_STORED":
        return "BLOCKED_STORAGE"
    if production_status.get("enabled") is False:
        return "BLOCKED_STORAGE"
    if str(result.get("status") or "").lower() == "partial_success":
        return "FAILED"
    return "COMPLETED"


def _terminal_progress_message(status: str, result: dict[str, Any] | None, error: str) -> str:
    if status == "ACTION_REQUIRED":
        return "Processing needs attention. Completed drafts are preserved; inspect the error code and retry here after resolving it."
    if status == "RETRY_WAIT":
        return "Temporary service interruption; waiting for automatic retry."
    if status == "SOURCE_REVIEW_REQUIRED":
        if (result or {}).get("chunk_counts", {}).get("stored", 0):
            return "Earlier drafts remain stored. Remaining source content needs review; completed output is preserved."
        return "No sufficiently supported source package is available; extraction/source review is required. Nothing was stored."
    if status == "FAILED":
        return f"Job failed: {error or (result or {}).get('reason', 'unknown error')}."
    if status == "LOCAL_REVIEW_READY":
        if result and result.get("status") == "partial_success":
            return "Partial local review package is ready; rejected chunks were quarantined."
        return "Local review package is ready."
    if status == "BLOCKED_STORAGE":
        return "Local conversion completed, but storage did not complete."
    if status == "SKIPPED":
        return "Skipped because a valid identical output already exists."
    return "Job completed."


def _terminal_progress_detail(status: str, result: dict[str, Any] | None, error: str) -> dict[str, Any]:
    detail = {"terminal_status": status}
    if error:
        detail["error"] = error
    if result:
        detail["pipeline_status"] = result.get("status")
        detail["validation_status"] = result.get("validation_status")
        detail["output_dir"] = result.get("output_dir")
        if isinstance(result.get("chunk_counts"), dict):
            detail["chunk_counts"] = result["chunk_counts"]
        detail["failure_code"] = result.get("failure_code")
        if isinstance(result.get("local_database"), dict):
            detail["local_database_status"] = result["local_database"].get("status")
            detail["readback_verified"] = result["local_database"].get("readback_verified")
        if isinstance(result.get("production_database_upload"), dict):
            detail["production_database_status"] = result["production_database_upload"].get("status")
    return detail


def _reset_processing_queue_for_tests() -> None:
    global _queue_worker_running, _queue_worker_thread
    if LOCAL_DB_PATH.resolve() == (PROJECT_ROOT/"local_db"/"gastroagi_chunks.sqlite3").resolve():
        raise RuntimeError("Test queue resets require an isolated temporary database.")
    init_processing_queue_db()
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.execute("delete from pdf_processing_queue")
        conn.execute("delete from pdf_queue_control")
    _queue_worker_running = False
    _queue_worker_thread = None


def sync_existing_outputs_to_local_db() -> dict[str, Any]:
    synced = []
    for chunks_path in sorted(OUTPUT_DIR.glob("*/chunks.json")):
        validation_status = read_validation_status(chunks_path.parent)
        synced.append(ingest_chunks_file(chunks_path, validation_status=validation_status))
    return {"status": "SYNC_COMPLETE", "packages": synced, "database": local_db_status()}


def load_generated_chunks(output_dir: Path = OUTPUT_DIR) -> tuple[list[RagChunk], list[dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[RagChunk] = []
    files: list[dict[str, Any]] = []
    for chunks_path in sorted(output_dir.glob("*/chunks.json")):
        validation_status = read_validation_status(chunks_path.parent)
        try:
            parsed = load_chunk_file(chunks_path, validation_status=validation_status)
        except ChunkFileError as exc:
            files.append({"filename": str(chunks_path), "chunk_count": 0, "status": "error", "message": str(exc)})
            continue
        chunks.extend(parsed)
        package_metadata = _chunk_package_metadata(chunks_path, parsed, validation_status)
        files.append(
            {
                "filename": str(chunks_path.relative_to(PROJECT_ROOT)),
                "chunk_count": len(parsed),
                "status": "loaded",
                "validation_status": validation_status,
                "message": "",
                **package_metadata,
            }
        )
    return chunks, files


def _chunk_package_metadata(
    chunks_path: Path,
    parsed: list[RagChunk],
    validation_status: str,
) -> dict[str, Any]:
    article: dict[str, Any] = {}
    try:
        payload = json.loads(chunks_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("article"), dict):
            article = payload["article"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass

    review_path = chunks_path.parent / "local_review_chunks.json"
    reviewable_count = len(parsed)
    if validation_status == "FAILED":
        reviewable_count = 0
        if review_path.exists():
            try:
                review_payload = json.loads(review_path.read_text(encoding="utf-8"))
                if isinstance(review_payload, dict) and isinstance(review_payload.get("chunks"), list):
                    reviewable_count = len(review_payload["chunks"])
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                reviewable_count = 0

    source_file = str(article.get("source_file") or "").strip()
    topic = str(article.get("topic") or "").strip()
    subtopic = str(article.get("subtopic") or "").strip()
    if not source_file and parsed:
        source_file = parsed[0].source_file
    if not topic and parsed:
        topic = parsed[0].topic
    if not subtopic and parsed:
        subtopic = parsed[0].subtopic
    return {
        "source_file": source_file or chunks_path.parent.name,
        "topic": topic,
        "subtopic": subtopic,
        "reviewable_count": reviewable_count,
        "rejected_count": max(len(parsed) - reviewable_count, 0),
        "availability": "partial" if validation_status == "FAILED" else "reviewable",
    }


def load_chunk_file(path: Path, validation_status: str = "UNKNOWN") -> list[RagChunk]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ChunkFileError("Chunk file must be UTF-8 text.") from exc
    if path.suffix.lower() == ".json":
        return parse_chunks_json(raw, path.name, path.parent.name, validation_status)
    if path.suffix.lower() in {".md", ".markdown"}:
        return parse_chunks_markdown(raw, path.name, path.parent.name, validation_status)
    raise ChunkFileError("Unsupported chunk file extension.")


def parse_chunks_json(raw: str, source_file: str, output_dir: str = "", validation_status: str = "UNKNOWN") -> list[RagChunk]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChunkFileError(f"Invalid JSON: {exc.msg}.") from exc
    if isinstance(data, dict) and isinstance(data.get("chunks"), list):
        records = data["chunks"]
    elif isinstance(data, list):
        records = data
    else:
        raise ChunkFileError("JSON must be a list of chunks or an object with a chunks list.")
    chunks = [_chunk_from_mapping(item, source_file, i + 1, output_dir, validation_status) for i, item in enumerate(records)]
    valid = [chunk for chunk in chunks if chunk is not None]
    if not valid:
        raise ChunkFileError("No valid GastroAGI chunks were found.")
    return valid


def _chunk_from_mapping(item: Any, source_file: str, fallback_number: int, output_dir: str, validation_status: str) -> RagChunk | None:
    if not isinstance(item, dict):
        return None
    topic = _first_text(item, "topic", "Topic")
    subtopic = _first_text(item, "subtopic", "Subtopic")
    section = _first_text(item, "section", "Section")
    reference = _first_text(item, "reference", "Reference")
    original_text = _first_text(item, "original_text", "Original Text", "originalText")
    number_value = item.get("chunk_number", item.get("chunkNumber", item.get("Chunk Number")))
    chunk_number = _safe_int(number_value) or fallback_number
    if any(not value.strip() for value in [topic, subtopic, section, reference, original_text]):
        return None
    return RagChunk(source_file, chunk_number, topic.strip(), subtopic.strip(), section.strip(), reference.strip(), original_text.strip(), output_dir, validation_status)


def _first_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return ""


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_chunks_markdown(raw: str, source_file: str, output_dir: str = "", validation_status: str = "UNKNOWN") -> list[RagChunk]:
    headings = list(CHUNK_HEADING_RE.finditer(raw))
    if not headings:
        raise ChunkFileError("Markdown does not contain any '## Chunk N' sections.")
    chunks: list[RagChunk] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(raw)
        block = raw[heading.end():end].strip()
        chunk = RagChunk(
            source_file=source_file,
            chunk_number=_safe_int(heading.group(1)),
            topic=_markdown_field(block, "Topic"),
            subtopic=_markdown_field(block, "Subtopic"),
            section=_markdown_field(block, "Section"),
            reference=_markdown_field(block, "Reference"),
            original_text=_markdown_original_text(block),
            output_dir=output_dir,
            validation_status=validation_status,
        )
        if all([chunk.topic, chunk.subtopic, chunk.section, chunk.reference, chunk.original_text]):
            chunks.append(chunk)
    if not chunks:
        raise ChunkFileError("Markdown did not contain complete five-field chunks.")
    return chunks


def _markdown_field(block: str, field_name: str) -> str:
    match = re.search(rf"^{re.escape(field_name)}:\s*(.+?)\s*$", block, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _markdown_original_text(block: str) -> str:
    match = re.search(r"^Original Text:\s*$", block, re.IGNORECASE | re.MULTILINE)
    return block[match.end():].strip() if match else ""


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text) if token.lower() not in STOPWORDS}


def retrieve_chunks(question: str, chunks: list[RagChunk], top_k: int = 4) -> list[RetrievalHit]:
    query_terms = tokenize(question)
    if not query_terms:
        return []
    hits: list[RetrievalHit] = []
    for chunk in chunks:
        weighted_text = " ".join([chunk.section, chunk.section, chunk.section, chunk.topic, chunk.topic, chunk.subtopic, chunk.subtopic, chunk.reference, chunk.original_text])
        matched = tuple(sorted(query_terms & tokenize(weighted_text)))
        if not matched:
            continue
        score = len(matched) / max(len(query_terms), 1)
        score += 0.1 * len(query_terms & tokenize(chunk.section))
        score += 0.05 * len(query_terms & tokenize(chunk.topic + " " + chunk.subtopic))
        hits.append(RetrievalHit(chunk, score, matched, extract_relevant_excerpt(chunk.original_text, query_terms)))
    hits.sort(key=lambda hit: (hit.score, len(hit.matched_terms)), reverse=True)
    return hits[:top_k]


def extract_relevant_excerpt(text: str, query_terms: set[str], max_chars: int = 520) -> str:
    return stored_excerpt(text, list(query_terms), max_chars=max_chars)


def build_rag_response(question: str, chunks: list[RagChunk]) -> dict[str, Any]:
    clean_question = " ".join(question.split())
    if not clean_question:
        return {"grounding_status": "INVALID_QUESTION", "answer": "Please enter a question.", "citations": [], "retrieved_chunks": []}
    if not chunks:
        return {"grounding_status": "NO_GENERATED_CHUNKS", "answer": "Upload a PDF and generate chunks before asking the bot.", "citations": [], "retrieved_chunks": []}
    hits = retrieve_chunks(clean_question, chunks)
    if not hits:
        return {"grounding_status": "NO_RELEVANT_CHUNKS", "answer": "I could not find supporting evidence in the generated chunks. Because this review chat is source-bound, I will not answer from outside knowledge.", "citations": [], "retrieved_chunks": []}
    lines = ["Based only on generated chunks:"]
    for index, hit in enumerate(hits[:3], start=1):
        lines.append(f"{index}. {hit.excerpt} [{hit.chunk.citation_label}]")
    return {
        "grounding_status": "ANSWERED_FROM_GENERATED_CHUNKS",
        "answer": "\n".join(lines),
        "citations": [hit.chunk.citation_label for hit in hits],
        "retrieved_chunks": [
            {
                "source_file": hit.chunk.source_file,
                "output_dir": hit.chunk.output_dir,
                "validation_status": hit.chunk.validation_status,
                "chunk_number": hit.chunk.chunk_number,
                "topic": hit.chunk.topic,
                "subtopic": hit.chunk.subtopic,
                "section": hit.chunk.section,
                "reference": hit.chunk.reference,
                "score": round(hit.score, 4),
                "matched_terms": list(hit.matched_terms),
                "excerpt": hit.excerpt,
            }
            for hit in hits
        ],
    }


def _shared_page_style() -> str:
    return """
  <style>
    :root { --bg:#f7f8fa; --panel:#fff; --text:#18202a; --muted:#637083; --border:#d7dde5; --accent:#146c5f; --accent-dark:#0f5248; --warn:#fff7df; --warn-border:#e2c36b; --danger:#8a2a1d; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.45; }
    main { width:min(1180px, calc(100vw - 32px)); margin:0 auto; padding:28px 0 40px; }
    header { display:flex; justify-content:space-between; gap:20px; align-items:flex-end; padding-bottom:18px; border-bottom:1px solid var(--border); }
    h1 { margin:0 0 6px; font-size:clamp(1.6rem, 2.5vw, 2.2rem); letter-spacing:0; }
    p { margin:0; color:var(--muted); }
    nav { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    nav a { color:var(--accent); text-decoration:none; border:1px solid var(--border); border-radius:6px; padding:8px 10px; background:white; font-weight:650; }
    nav a.active { background:var(--accent); border-color:var(--accent); color:white; }
    .status { text-align:right; color:var(--muted); font-size:.95rem; white-space:nowrap; }
    .layout { display:grid; grid-template-columns:380px minmax(0, 1fr); gap:18px; margin-top:20px; align-items:start; }
    .single { margin-top:20px; }
    section { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:18px; }
    h2 { margin:0 0 12px; font-size:1rem; letter-spacing:0; }
    .box { display:grid; gap:12px; }
    input[type=file], textarea { width:100%; border:1px solid var(--border); border-radius:6px; background:white; padding:10px; font:inherit; }
    textarea { min-height:132px; resize:vertical; }
    button { border:0; border-radius:6px; background:var(--accent); color:white; min-height:38px; padding:0 14px; font:inherit; font-weight:650; cursor:pointer; }
    button:hover { background:var(--accent-dark); }
    button:disabled { background:#9aa6b2; cursor:not-allowed; }
    ul { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
    li { border:1px solid var(--border); border-radius:6px; padding:10px; display:grid; gap:8px; }
    li span, li small { color:var(--muted); }
    .notice { margin-top:14px; border:1px solid var(--warn-border); background:var(--warn); border-radius:6px; padding:12px; color:#64500b; font-size:.93rem; }
    .blocked { color:var(--danger); font-weight:650; }
    .progress-grid { display:grid; gap:10px; }
    .progress-row { border:1px solid var(--border); border-radius:6px; padding:10px; background:#fbfcfd; display:grid; gap:6px; }
    .progress-row strong { font-size:.92rem; }
    .progress-row small { color:var(--muted); }
    progress { width:100%; height:14px; accent-color:var(--accent); }
    .queue-table { width:100%; border-collapse:collapse; font-size:.88rem; }
    .queue-table th,.queue-table td { border-bottom:1px solid var(--border); padding:8px 6px; text-align:left; vertical-align:top; }
    .queue-table th { color:var(--muted); font-weight:750; }
    .queue-actions { display:flex; gap:6px; flex-wrap:wrap; }
    .queue-actions button { min-height:28px; padding:0 8px; font-size:.8rem; }
    .answer { min-height:220px; border:1px solid var(--border); border-radius:8px; padding:14px; background:#fbfcfd; white-space:pre-wrap; }
    .evidence { display:grid; gap:10px; }
    .hit { border:1px solid var(--border); border-radius:6px; padding:12px; background:white; }
    .hit strong { display:block; margin-bottom:6px; }
    .hit small { display:block; color:var(--muted); margin-top:8px; }
    @media (max-width:900px) { header,.layout { display:block; } .status { text-align:left; margin-top:10px; } section { margin-top:16px; } nav { justify-content:flex-start; margin-top:12px; } }
  </style>"""


def render_admin_page() -> bytes:
    chunks, chunk_files = load_generated_chunks()
    pdfs = list_pdf_inputs()
    pdf_items = "".join(
        f"<li><strong>{html.escape(item['filename'])}</strong> "
        f"<span>{item['validation_status']} - {item['output_dir']}</span> "
        f"<button type='button' onclick=\"processPdf('{html.escape(item['filename'])}')\">Generate now</button></li>"
        for item in pdfs
    ) or "<li>No PDFs uploaded yet.</li>"
    chunk_items = "".join(
        f"<li><strong>{html.escape(item['filename'])}</strong> "
        f"<span>{item['chunk_count']} chunks - {html.escape(item.get('validation_status', 'UNKNOWN'))}</span></li>"
        for item in chunk_files
    ) or "<li>No generated chunks found yet.</li>"
    db_status = database_upload_status()
    local_status = local_db_status()
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GastroAGI Chunks Admin</title>
{_shared_page_style()}
</head>
<body>
  <main>
    <header>
      <div><h1>Chunks Admin Panel</h1><p>Upload PDFs, then generate reviewable GastroAGI chunks.</p></div>
      <div><nav><a class="active" href="/admin">Chunks Admin</a><a href="/bot">BOT Page</a></nav><div class="status">{len(pdfs)} PDFs - {len(chunks)} chunks loaded</div></div>
    </header>
    <div class="layout">
      <section>
        <h2>PDF Upload</h2>
        <form class="box" id="uploadForm">
          <input name="pdf_file" type="file" accept=".pdf,application/pdf" multiple required>
          <button type="submit">Upload PDFs</button>
        </form>
        <div id="uploadResult" class="notice" style="display:none;"></div>
        <div class="notice">Upload up to {MAX_UPLOAD_FILES} PDFs. Processing starts automatically, retries temporary service errors, and resumes saved work after restart. Keep the server running; you can close this page. Supported drafts are stored in the local review database and require clinical review.</div>
        <h2 style="margin-top:18px;">Processing Queue</h2>
        <div class="box">
          <button type="button" id="startQueueButton" onclick="startQueue()">Start Generating</button>
          <button type="button" id="pauseQueueButton" class="ghost" onclick="pauseQueue()">Pause After Current PDF</button>
          <button type="button" id="resumeQueueButton" class="ghost" onclick="resumeQueue()">Resume Queue</button>
          <button type="button" class="ghost" onclick="refreshQueue()">Refresh Queue</button>
          <div id="queueStatus" class="notice" style="margin-top:10px;">Loading queue...</div>
          <div class="progress-grid" aria-live="polite">
            <div class="progress-row">
              <strong id="overallProgressText">Overall queue progress</strong>
              <progress id="overallProgressBar" max="1" value="0"></progress>
              <small id="overallElapsed">Elapsed: 0s</small>
            </div>
            <div class="progress-row">
              <strong id="currentProgressText">No PDF is currently processing.</strong>
              <progress id="currentProgressBar"></progress>
              <small id="currentElapsed">Elapsed: 0s</small>
            </div>
          </div>
          <div style="overflow:auto;">
            <table class="queue-table">
              <thead><tr><th>Queue position</th><th>PDF filename</th><th>Page count</th><th>Status</th><th>Progress</th><th>Passed / Review</th><th>Stored chunks</th><th>Elapsed time</th><th>Actions</th></tr></thead>
              <tbody id="queueItems"><tr><td colspan="9">Loading queue...</td></tr></tbody>
            </table>
          </div>
        </div>
        <h2 style="margin-top:18px;">Uploaded PDFs</h2>
        <ul>{pdf_items}</ul>
      </section>
      <section>
        <h2>Generated Chunk Packages</h2>
        <ul>{chunk_items}</ul>
        <h2 style="margin-top:18px;">Local Retrieval Database</h2>
        <div class="notice"><strong>{local_status['status']}</strong><br>{local_status['chunk_count']} chunks stored using {html.escape(local_status['embedding_algorithm'])}.</div>
        <button type="button" onclick="syncLocalDb()" style="margin-top:10px;">Sync generated chunks</button>
        <h2 style="margin-top:18px;">Production Database Upload</h2>
        <div class="notice"><span class="blocked">{db_status['status']}</span><br>{html.escape(db_status['reason'])}</div>
        <button type="button" disabled style="margin-top:10px;">Upload to production database</button>
      </section>
    </div>
  </main>
  <script>
    const maxUploadFiles = {MAX_UPLOAD_FILES};
    const uploadForm = document.getElementById('uploadForm');
    uploadForm.addEventListener('submit', async (event) => {{
      event.preventDefault();
      const fileInput = uploadForm.querySelector('input[type="file"]');
      if (fileInput.files.length > maxUploadFiles) {{
        alert(`Upload up to ${{maxUploadFiles}} PDFs in one batch. Extra PDFs must be uploaded in a second batch.`);
        return;
      }}
      const submitButton = uploadForm.querySelector('button[type="submit"]');
      submitButton.disabled = true;
      const response = await fetch('/upload-pdf', {{ method: 'POST', body: new FormData(uploadForm) }});
      const result = await response.json();
      submitButton.disabled = false;
      if (!response.ok) {{ alert(result.error || 'Upload failed.'); return; }}
      const rejected = result.rejected || [];
      const uploadResult = document.getElementById('uploadResult');
      uploadResult.style.display = 'block';
      uploadResult.textContent = rejected.length
        ? `Uploaded ${{(result.files || []).length}} PDF(s). Rejected ${{rejected.length}} file(s): ${{rejected.map(item => `${{item.filename}} (${{item.error}})`).join('; ')}}`
        : `Uploaded ${{(result.files || []).length}} PDF(s). Added to queue.`;
      uploadForm.reset();
      await refreshQueue();
    }});
    function escapeHtml(value) {{
      return String(value || '').replace(/[&<>"']/g, (char) => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }}[char]));
    }}
    function compactResult(item) {{
      const pieces = [];
      if (item.progress_message) pieces.push(item.progress_message);
      if (item.result?.chunk_counts) pieces.push(`chunks generated/validated/stored: ${{item.result.chunk_counts.generated || 0}}/${{item.result.chunk_counts.validated || 0}}/${{item.result.chunk_counts.stored || 0}}`);
      if (item.validation_status) pieces.push(`validation: ${{item.validation_status}}`);
      if (item.local_database?.status) pieces.push(`local DB: ${{item.local_database.status}}`);
      if (item.production_database_upload?.status) pieces.push(`production: ${{item.production_database_upload.status}}`);
      if (item.error) pieces.push(`error: ${{item.error}}`);
      if (item.last_error_code) pieces.push(`code: ${{item.last_error_code}}`);
      if (item.next_attempt_at) pieces.push(`next retry: ${{new Date(item.next_attempt_at * 1000).toLocaleTimeString()}}`);
      return pieces.join(' | ');
    }}
    function jobActions(item) {{
      const actions = [];
      if (item.output_dir || item.result?.output_dir) actions.push(`<button type="button" onclick="viewChunks('${{item.id}}')">View Generated Chunks</button>`);
      if (['FAILED','ACTION_REQUIRED','BLOCKED_STORAGE','SOURCE_REVIEW_REQUIRED'].includes(item.status) || (item.status === 'LOCAL_REVIEW_READY' && item.validation_status === 'FAILED')) actions.push(`<button type="button" onclick="retryJob('${{item.id}}')">Retry Job</button>`);
      if (item.status === 'QUEUED') actions.push(`<button type="button" onclick="removeJob('${{item.id}}')">Remove Queued File</button>`);
      return `<div class="queue-actions">${{actions.join('') || '<small>No actions</small>'}}</div>`;
    }}
    function queueRow(item, index) {{
      const detail = item.progress_detail || {{}};
      const counts = item.result?.chunk_counts || {{}};
      const pageCount = item.page_count || detail.page_count || '';
      const validated = item.validated_chunks || counts.validated || 0;
      const needsReview = counts.review_required || 0;
      const stored = item.stored_chunks || counts.stored || 0;
      const progress = compactResult(item) || item.status;
      const statusLabel = ({{LOCAL_REVIEW_READY:'Drafts stored',SOURCE_REVIEW_REQUIRED:'Source review needed',RETRY_WAIT:'Retrying automatically',ACTION_REQUIRED:'Action needed'}})[item.status] || item.status;
      return `<tr><td>${{index + 1}}</td><td>${{escapeHtml(item.filename)}}</td><td>${{escapeHtml(pageCount)}}</td><td>${{escapeHtml(statusLabel)}}</td><td>${{escapeHtml(progress)}}</td><td>${{validated}} / ${{needsReview}}</td><td>${{stored}}</td><td>${{formatElapsed(item.elapsed_seconds)}}</td><td>${{jobActions(item)}}</td></tr>`;
    }}
    function formatElapsed(seconds) {{
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      const mins = Math.floor(total / 60);
      const secs = total % 60;
      return mins ? `${{mins}}m ${{secs}}s` : `${{secs}}s`;
    }}
    function isActiveStatus(status) {{
      return ['EXTRACTING','PAGES_EXTRACTED','CLEANING','CLEANING_COMPLETED','GENERATING_CHUNKS','CHUNK_BATCH_COMPLETED','VALIDATING','CHUNKS_VALIDATED','SAVING_OUTPUT','JSON_SAVED','STORING_DATABASE','DATABASE_INGESTION_COMPLETED','VERIFYING_STORAGE'].includes(status);
    }}
    function updateProgressDisplays(queue) {{
      const items = queue.items || [];
      const total = items.length;
      const terminal = queue.terminal_count || 0;
      const overallText = document.getElementById('overallProgressText');
      const overallBar = document.getElementById('overallProgressBar');
      const overallElapsed = document.getElementById('overallElapsed');
      overallText.textContent = total ? `${{terminal}} of ${{total}} PDFs completed` : 'No PDFs queued yet';
      overallBar.max = Math.max(total, 1);
      overallBar.value = terminal;
      const queueElapsed = items.reduce((max, item) => Math.max(max, Number(item.elapsed_seconds) || 0), 0);
      overallElapsed.textContent = `Elapsed: ${{formatElapsed(queueElapsed)}}`;

      const activeIndex = items.findIndex((item) => isActiveStatus(item.status));
      const currentText = document.getElementById('currentProgressText');
      const currentBar = document.getElementById('currentProgressBar');
      const currentElapsed = document.getElementById('currentElapsed');
      if (activeIndex === -1) {{
        currentText.textContent = total ? 'No PDF is currently processing.' : 'No PDF is currently processing.';
        currentBar.removeAttribute('value');
        currentElapsed.textContent = 'Elapsed: 0s';
        return;
      }}
      const active = items[activeIndex];
      const detail = active.progress_detail || {{}};
      const message = active.progress_message || active.status;
      const pageInfo = detail.page_count ? ` Pages extracted: ${{detail.page_count}}.` : '';
      currentText.textContent = `PDF ${{activeIndex + 1}} of ${{total}} - ${{message}}${{pageInfo}}`;
      if (active.status === 'GENERATING_CHUNKS') {{
        currentBar.removeAttribute('value');
      }} else {{
        currentBar.max = 1;
        currentBar.value = 0;
      }}
      currentElapsed.textContent = `Elapsed: ${{formatElapsed(active.elapsed_seconds)}}`;
    }}
    let queueRefreshTimer;
    async function refreshQueue() {{
      clearTimeout(queueRefreshTimer);
      try {{
      const response = await fetch('/api/queue');
      const result = await response.json();
      if (!response.ok) {{ document.getElementById('queueStatus').textContent = result.error || 'Queue status failed.'; return; }}
      const queue = result.queue || result;
      const counts = queue.counts || {{}};
      document.getElementById('queueStatus').textContent =
        `Queue is ${{queue.paused ? 'paused' : queue.status}}. Queued: ${{counts.QUEUED || 0}}, active: ${{queue.active_count || 0}}, retrying: ${{queue.waiting_count || 0}}, finished: ${{queue.terminal_count || 0}}.`;
      document.getElementById('startQueueButton').disabled = Boolean(queue.running) || Boolean(queue.paused) || !(queue.queued_count > 0);
      document.getElementById('pauseQueueButton').disabled = Boolean(queue.paused);
      document.getElementById('resumeQueueButton').disabled = !Boolean(queue.paused);
      const items = queue.items || [];
      updateProgressDisplays(queue);
      document.getElementById('queueItems').innerHTML = items.length
        ? items.map((item, index) => queueRow(item, index)).join('')
        : '<tr><td colspan="9">No PDFs queued yet.</td></tr>';
      }} catch (error) {{
        document.getElementById('queueStatus').textContent = 'Reconnecting to the server. Saved jobs resume when it is available.';
      }} finally {{
        clearTimeout(queueRefreshTimer);
        queueRefreshTimer = setTimeout(refreshQueue, 2000);
      }}
    }}
    async function startQueue() {{
      const button = document.getElementById('startQueueButton');
      button.disabled = true;
      const response = await fetch('/queue/start', {{ method: 'POST' }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Queue start failed.'); button.disabled = false; return; }}
      await refreshQueue();
    }}
    async function pauseQueue() {{
      const response = await fetch('/queue/pause', {{ method: 'POST' }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Pause failed.'); return; }}
      await refreshQueue();
    }}
    async function resumeQueue() {{
      const response = await fetch('/queue/resume', {{ method: 'POST' }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Resume failed.'); return; }}
      await refreshQueue();
    }}
    async function retryJob(id) {{
      const response = await fetch('/queue/retry', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ id }}) }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Retry failed.'); return; }}
      await refreshQueue();
    }}
    async function removeJob(id) {{
      if (!confirm('Remove this queued job? The uploaded PDF file will be preserved on disk.')) return;
      const response = await fetch('/queue/remove', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ id }}) }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Remove failed.'); return; }}
      await refreshQueue();
    }}
    function viewChunks(id) {{
      window.open(`/api/job-chunks?id=${{encodeURIComponent(id)}}`, '_blank');
    }}
    async function processPdf(filename) {{
      const response = await fetch('/process-pdf', {{
        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ filename, force: false }})
      }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Generation failed.'); return; }}
      alert(JSON.stringify(result, null, 2));
      window.location.reload();
    }}
    async function syncLocalDb() {{
      const response = await fetch('/sync-local-db', {{ method: 'POST' }});
      const result = await response.json();
      if (!response.ok) {{ alert(result.error || 'Sync failed.'); return; }}
      alert(JSON.stringify(result, null, 2));
      window.location.reload();
    }}
    refreshQueue();
  </script>
</body>
</html>"""
    return page.encode("utf-8")


def render_bot_page() -> bytes:
    local_status = local_db_status()
    chat_style = """
  <style>
    .chat-page { margin-top:20px; }
    .chat-shell { min-height:74vh; height:calc(100vh - 150px); display:grid; grid-template-rows:auto 1fr auto; background:#fff; border:1px solid var(--border); border-radius:8px; overflow:hidden; box-shadow:0 10px 26px rgba(24,32,42,.06); }
    .chat-head { padding:14px 16px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; gap:12px; align-items:center; background:#fff; }
    .chat-head strong { display:block; }
    .chat-head small { color:var(--muted); }
    .chat-actions { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    .chat-log { padding:20px; overflow:auto; scroll-behavior:smooth; display:flex; flex-direction:column; gap:16px; background:#fbfcfd; }
    .turn { display:grid; gap:6px; max-width:min(900px, 94%); min-width:0; }
    .turn.user { align-self:flex-end; justify-items:end; }
    .turn.assistant { align-self:flex-start; justify-items:start; }
    .msg-label { font-size:.78rem; color:var(--muted); padding:0 4px; }
    .msg { padding:12px 14px; border-radius:8px; overflow-wrap:anywhere; font-size:.98rem; line-height:1.55; min-width:0; max-width:100%; }
    .turn.user .msg { background:#146c5f; color:white; border-top-right-radius:3px; }
    .turn.assistant .msg { background:white; border:1px solid var(--border); color:var(--text); border-top-left-radius:3px; }
    .msg p { margin:.35rem 0; color:inherit; }
    .msg h3 { margin:.65rem 0 .3rem; font-size:1rem; color:inherit; }
    .msg ul { list-style:disc; margin:.45rem 0 .45rem 1.25rem; padding:0; display:block; }
    .msg ol { margin:.45rem 0 .45rem 1.35rem; padding:0; display:block; }
    .msg li { border:0; border-radius:0; padding:0; display:list-item; margin:.22rem 0; }
    .msg strong { font-weight:750; }
    .table-wrap { width:100%; max-width:100%; min-width:0; overflow-x:auto; margin:.55rem 0; border:1px solid var(--border); border-radius:6px; }
    .msg table { width:100%; border-collapse:collapse; min-width:480px; font-size:.92rem; }
    .msg th,.msg td { padding:8px 10px; text-align:left; vertical-align:top; border-bottom:1px solid var(--border); }
    .msg th { background:#f2f6f5; font-weight:750; }
    .msg tr:last-child td { border-bottom:0; }
    .msg-actions { display:flex; gap:6px; flex-wrap:wrap; padding:0 4px; }
    .mini { min-height:28px; padding:0 9px; font-size:.82rem; background:white; color:var(--accent); border:1px solid var(--border); }
    .mini:hover,.ghost:hover { background:#eef5f3; color:var(--accent-dark); }
    .sources { align-self:flex-start; max-width:min(900px, 94%); width:min(900px, 94%); border:1px solid var(--border); border-radius:8px; background:white; padding:10px 12px; }
    .sources summary { cursor:pointer; color:var(--accent); font-weight:700; }
    .source-list { display:grid; gap:8px; margin-top:10px; }
    .source { border:1px solid var(--border); border-radius:6px; padding:10px; background:#fbfcfd; font-size:.9rem; }
    .source small { display:block; color:var(--muted); margin-top:4px; }
    .composer { position:sticky; bottom:0; border-top:1px solid var(--border); padding:12px; display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:10px; align-items:end; background:white; }
    .composer textarea { min-height:54px; max-height:150px; resize:vertical; line-height:1.45; }
    .composer textarea:focus, button:focus-visible { outline:3px solid rgba(20,108,95,.22); outline-offset:2px; }
    .ghost { background:white; color:var(--accent); border:1px solid var(--border); }
    .stop { background:white; color:#a33b21; border:1px solid #d6aa9c; }
    .stop:hover { background:#fff4f0; color:#7f2d18; }
    .stop[hidden] { display:none; }
    .error-note { align-self:flex-start; max-width:min(900px, 94%); border:1px solid #e1b4a8; background:#fff5f2; color:#7f2d18; border-radius:6px; padding:10px 12px; font-size:.92rem; }
    .thinking { display:inline-flex; gap:4px; align-items:center; min-width:46px; }
    .thinking span { width:6px; height:6px; border-radius:50%; background:#7a8794; animation:pulse 1s infinite ease-in-out; }
    .thinking span:nth-child(2) { animation-delay:.15s; }
    .thinking span:nth-child(3) { animation-delay:.3s; }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    @keyframes pulse { 0%, 80%, 100% { opacity:.25; transform:translateY(0); } 40% { opacity:1; transform:translateY(-2px); } }
    @media (max-width:900px) { main { width:min(100vw - 20px, 1180px); padding-top:18px; } .chat-shell { min-height:76vh; height:calc(100vh - 170px); } .composer { grid-template-columns:1fr; } .chat-actions { justify-content:flex-start; } }
  </style>"""
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GastroAGI Assistant</title>
{_shared_page_style()}
{chat_style}
</head>
<body>
  <main>
    <header>
      <div><h1>GastroAGI Assistant</h1><p>Source-grounded clinical review chat.</p></div>
      <div><nav><a href="/admin">Chunks Admin</a><a class="active" href="/bot">BOT Page</a></nav><div class="status">{local_status['chunk_count']} searchable chunks</div></div>
    </header>
    <div class="chat-page">
      <div class="chat-shell" aria-label="GastroAGI conversational assistant">
        <div class="chat-head">
          <div><strong>Clinical Assistant</strong><small id="chatStatus" aria-live="polite">Ready</small></div>
          <div class="chat-actions">
            <button class="ghost" id="newChat" type="button">New chat</button>
            <button class="ghost" id="clearChat" type="button">Clear</button>
          </div>
        </div>
        <div class="chat-log" id="chatLog" role="log" aria-live="polite" aria-relevant="additions text"></div>
        <div class="composer">
          <label class="sr-only" for="question">Message GastroAGI</label>
          <textarea id="question" placeholder="Ask about generated clinical chunks" autocomplete="off"></textarea>
          <button id="askButton" type="button">Send</button>
          <button class="stop" id="stopButton" type="button" hidden>Stop</button>
        </div>
      </div>
    </div>
  </main>
  <script>
    const askButton = document.getElementById('askButton');
    const stopButton = document.getElementById('stopButton');
    const newChat = document.getElementById('newChat');
    const clearChat = document.getElementById('clearChat');
    const input = document.getElementById('question');
    const chatLog = document.getElementById('chatLog');
    const chatStatus = document.getElementById('chatStatus');
    let messages = [];
    let activeController = null;
    let stickToBottom = true;
    let lastUserQuestion = '';
    let responseSequence = 0;

    addAssistantIntro();

    askButton.addEventListener('click', () => sendCurrentMessage());
    stopButton.addEventListener('click', () => {{
      if (activeController) activeController.abort();
    }});
    chatLog.addEventListener('scroll', () => {{
      const distanceFromBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight;
      stickToBottom = distanceFromBottom < 56;
    }});
    newChat.addEventListener('click', () => {{
      resetChat('New chat started.');
    }});
    clearChat.addEventListener('click', () => {{
      if (confirm('Clear this conversation?')) resetChat('Conversation cleared.');
    }});
    input.addEventListener('keydown', (event) => {{
      if (event.key === 'Enter' && !event.shiftKey) {{
        event.preventDefault();
        sendCurrentMessage();
      }}
    }});

    function addAssistantIntro() {{
      const intro = 'Hi. I can help you explore gastroenterology and hepatology research. What would you like to discuss?';
      const turn = addTurn('assistant', intro);
      addAssistantActions(turn, intro, '');
    }}

    function resetChat(message) {{
      if (activeController) activeController.abort();
      messages = [];
      lastUserQuestion = '';
      input.value = '';
      chatLog.innerHTML = '';
      setGenerating(false, 'Ready');
      const turn = addTurn('assistant', message);
      addAssistantActions(turn, message, '');
    }}

    function sendCurrentMessage() {{
      sendMessage(input.value, {{ echoUser: true, preserveOnFail: true }});
    }}

    async function sendMessage(rawQuestion, options) {{
      const question = String(rawQuestion || '').trim();
      if (!question || activeController) return;
      const echoUser = options?.echoUser !== false;
      const preserveOnFail = options?.preserveOnFail !== false;
      const history = messages.slice(-10);
      if (echoUser) {{
        input.value = '';
        addTurn('user', question);
        messages.push({{ role: 'user', content: question }});
      }}
      lastUserQuestion = question;
      setGenerating(true, 'Thinking...');
      activeController = new AbortController();
      const assistantTurn = addTurn('assistant', '');
      const assistantBubble = assistantTurn.querySelector('.msg');
      const responseId = `response-${{++responseSequence}}`;
      const thinking = showThinking(assistantBubble);
      let assistantText = '';
      let doneReceived = false;
      let completedResult = {{}};
      try {{
        const response = await fetch('/chat-stream', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ question, history }}),
          signal: activeController.signal
        }});
        if (!response.ok || !response.body) throw new Error(`Network request failed with status ${{response.status}}.`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {{
          const {{ value, done }} = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, {{ stream: true }});
          const lines = buffer.split('\\n');
          buffer = lines.pop() || '';
          for (const line of lines) {{
            if (!line.trim()) continue;
            const event = parseStreamEvent(line);
            if (event.type === 'meta') {{
              chatStatus.textContent = 'Responding...';
            }} else if (event.type === 'delta') {{
              if (thinking.parentNode) thinking.remove();
              assistantText += event.text || '';
              renderFormatted(assistantBubble, assistantText);
              maybeScrollToBottom();
            }} else if (event.type === 'error') {{
              displayError(event.message || 'The stream reported an error.', event.recoverable ? 'Trying local fallback.' : 'Please retry.');
              chatStatus.textContent = event.recoverable ? 'Using local fallback' : 'Error';
            }} else if (event.type === 'done') {{
              doneReceived = true;
              const result = event.result || {{}};
              completedResult = result;
              renderFormatted(assistantBubble, assistantText);
              renderSources(result, responseId);
              chatStatus.textContent = 'Ready';
            }}
          }}
        }}
        if (buffer.trim()) throw new Error('Malformed stream event received.');
        if (!doneReceived) throw new Error('Connection closed before the answer completed.');
      }} catch (error) {{
        if (error.name === 'AbortError') {{
          assistantText = assistantText || 'Generation stopped.';
          renderFormatted(assistantBubble, assistantText);
          chatStatus.textContent = 'Stopped';
        }} else {{
          const message = error.message || 'Unknown network error.';
          assistantText = assistantText || `I could not complete that chat request. ${{message}}`;
          renderFormatted(assistantBubble, assistantText);
          displayError(message, 'Check the local server/API configuration and try again.');
          chatStatus.textContent = 'Error';
          if (preserveOnFail && !input.value.trim()) input.value = question;
        }}
      }} finally {{
        if (thinking.parentNode) thinking.remove();
        messages.push({{ role: 'assistant', content: assistantText }});
        addAssistantActions(assistantTurn, assistantText, question, completedResult);
        activeController = null;
        setGenerating(false, chatStatus.textContent === 'Ready' ? 'Ready' : chatStatus.textContent);
        input.focus();
        maybeScrollToBottom();
      }}
    }}

    function parseStreamEvent(line) {{
      try {{
        return JSON.parse(line);
      }} catch (parseError) {{
        throw new Error('Malformed stream event received.');
      }}
    }}

    function setGenerating(isGenerating, status) {{
      askButton.disabled = isGenerating;
      input.disabled = isGenerating;
      stopButton.hidden = !isGenerating;
      newChat.disabled = isGenerating;
      clearChat.disabled = isGenerating;
      chatStatus.textContent = status;
    }}

    function showThinking(node) {{
      const indicator = document.createElement('span');
      indicator.className = 'thinking';
      indicator.setAttribute('aria-label', 'Assistant is thinking');
      indicator.innerHTML = '<span></span><span></span><span></span>';
      node.appendChild(indicator);
      maybeScrollToBottom();
      return indicator;
    }}

    function addTurn(role, text) {{
      const turn = document.createElement('article');
      turn.className = `turn ${{role}}`;
      const label = document.createElement('div');
      label.className = 'msg-label';
      label.textContent = role === 'user' ? 'You' : 'GastroAGI';
      const bubble = document.createElement('div');
      bubble.className = 'msg';
      bubble.setAttribute('tabindex', '0');
      if (role === 'user') {{
        bubble.textContent = text || '';
      }} else {{
        renderFormatted(bubble, text || '');
      }}
      turn.append(label, bubble);
      chatLog.appendChild(turn);
      stickToBottom = true;
      maybeScrollToBottom();
      return turn;
    }}

    function addAssistantActions(turn, text, question, result = {{}}) {{
      if (!turn || !turn.classList.contains('assistant')) return;
      const existing = turn.querySelector('.msg-actions');
      if (existing) existing.remove();
      const actions = document.createElement('div');
      actions.className = 'msg-actions';
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'mini';
      copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {{
        try {{
          await navigator.clipboard.writeText(formatCopiedAnswer(text, result));
          copy.textContent = 'Copied';
          setTimeout(() => copy.textContent = 'Copy', 1200);
        }} catch (error) {{
          displayError('Copy failed.', 'Select the response text manually and copy it.');
        }}
      }});
      actions.appendChild(copy);
      if (question) {{
        const regenerate = document.createElement('button');
        regenerate.type = 'button';
        regenerate.className = 'mini';
        regenerate.textContent = 'Regenerate';
        regenerate.addEventListener('click', () => {{
          if (activeController) return;
          sendMessage(question, {{ echoUser: false, preserveOnFail: false }});
        }});
        actions.appendChild(regenerate);
      }}
      turn.appendChild(actions);
    }}

    function normalizeCitationSources(sources) {{
      const normalized = [];
      const seen = new Set();
      (Array.isArray(sources) ? sources : []).forEach(source => {{
        const number = Number(source?.number);
        if (!Number.isInteger(number) || number < 1 || seen.has(number)) return;
        seen.add(number);
        normalized.push({{ ...source, number }});
      }});
      return normalized;
    }}

    function formatCopiedAnswer(text, result = {{}}) {{
      const answer = String(text || '')
        .replace(/\\*\\*([^*]+)\\*\\*/g, '$1')
        .replace(/\\s*\\[\\d+\\]/g, '')
        .replace(/\\s+([,.;:])/g, '$1');
      const sources = normalizeCitationSources(result.citation_details || []);
      if (!sources.length) return answer;
      const newline = String.fromCharCode(10);
      const sourceLines = sources.map(source => {{
        const reference = source.reference || source.section || 'Reference not specified';
        const section = source.section || 'not specified';
        const sourceFile = source.source_file || 'local document';
        const chunkNumber = source.chunk_number || 'unknown';
        const reviewStatus = source.validation_status || 'UNKNOWN';
        return `[${{source.number}}] ${{reference}}${{newline}}Section: ${{section}}${{newline}}Document: ${{sourceFile}}; chunk ${{chunkNumber}}${{newline}}Review status: ${{reviewStatus}}`;
      }});
      return `${{answer}}${{newline}}${{newline}}References:${{newline}}${{sourceLines.join(newline + newline)}}`;
    }}

    function renderFormatted(node, text) {{
      node.textContent = '';
      const normalized = String(text || '')
        .replace(/\\r\\n/g, '\\n')
        .replace(/\\s*\\[\\d+\\]/g, '')
        .replace(/\\s+([,.;:])/g, '$1')
        .trimEnd();
      if (!normalized) return;
      const lines = normalized.split('\\n');
      let index = 0;
      while (index < lines.length) {{
        const line = lines[index];
        if (!line.trim()) {{ index += 1; continue; }}
        const heading = line.match(/^#{{2,4}}\\s+(.+)$/);
        if (heading) {{
          const h3 = document.createElement('h3');
          appendInlineFormatting(h3, heading[1]);
          node.appendChild(h3);
          index += 1;
          continue;
        }}
        if (/^\\s*\\|.*\\|\\s*$/.test(line) && index + 1 < lines.length && isTableDivider(lines[index + 1])) {{
          const tableWrap = document.createElement('div');
          tableWrap.className = 'table-wrap';
          const table = document.createElement('table');
          const thead = document.createElement('thead');
          const headRow = document.createElement('tr');
          parseTableCells(line).forEach(cellText => {{
            const th = document.createElement('th');
            appendInlineFormatting(th, cellText);
            headRow.appendChild(th);
          }});
          thead.appendChild(headRow);
          table.appendChild(thead);
          const tbody = document.createElement('tbody');
          index += 2;
          while (index < lines.length && /^\\s*\\|.*\\|\\s*$/.test(lines[index])) {{
            const row = document.createElement('tr');
            parseTableCells(lines[index]).forEach(cellText => {{
              const td = document.createElement('td');
              appendInlineFormatting(td, cellText);
              row.appendChild(td);
            }});
            tbody.appendChild(row);
            index += 1;
          }}
          table.appendChild(tbody);
          tableWrap.appendChild(table);
          node.appendChild(tableWrap);
          continue;
        }}
        if (/^\\s*[-*]\\s+/.test(line)) {{
          const list = document.createElement('ul');
          while (index < lines.length && /^\\s*[-*]\\s+/.test(lines[index])) {{
            const item = document.createElement('li');
            appendInlineFormatting(item, lines[index].replace(/^\\s*[-*]\\s+/, ''));
            list.appendChild(item);
            index += 1;
          }}
          node.appendChild(list);
          continue;
        }}
        if (/^\\s*\\d+\\.\\s+/.test(line)) {{
          const list = document.createElement('ol');
          while (index < lines.length && /^\\s*\\d+\\.\\s+/.test(lines[index])) {{
            const item = document.createElement('li');
            appendInlineFormatting(item, lines[index].replace(/^\\s*\\d+\\.\\s+/, ''));
            list.appendChild(item);
            index += 1;
          }}
          node.appendChild(list);
          continue;
        }}
        const paragraphLines = [];
        while (index < lines.length && lines[index].trim() && !isBlockStart(lines, index)) {{
          paragraphLines.push(lines[index].trim());
          index += 1;
        }}
        if (!paragraphLines.length) {{ paragraphLines.push(line.trim()); index += 1; }}
        const p = document.createElement('p');
        appendInlineFormatting(p, paragraphLines.join(' '));
        node.appendChild(p);
      }}
    }}

    function isBlockStart(lines, index) {{
      const line = lines[index] || '';
      return /^#{{2,4}}\\s+/.test(line) || /^\\s*[-*]\\s+/.test(line) || /^\\s*\\d+\\.\\s+/.test(line) ||
        (/^\\s*\\|.*\\|\\s*$/.test(line) && index + 1 < lines.length && isTableDivider(lines[index + 1]));
    }}

    function isTableDivider(line) {{
      const cells = parseTableCells(line);
      return cells.length > 0 && cells.every(cell => /^:?-{{3,}}:?$/.test(cell.trim()));
    }}

    function parseTableCells(line) {{
      const text = String(line || '').trim().replace(/^\\|/, '').replace(/\\|$/, '');
      const cells = [];
      let current = '';
      let escaped = false;
      for (const char of text) {{
        if (escaped) {{ current += char; escaped = false; continue; }}
        if (char === '\\\\') {{ escaped = true; continue; }}
        if (char === '|') {{ cells.push(current.trim()); current = ''; continue; }}
        current += char;
      }}
      cells.push(current.trim());
      return cells;
    }}

    function appendInlineFormatting(parent, text) {{
      const parts = String(text || '').split(/(\\*\\*[^*]+\\*\\*|\\[\\d+\\])/g);
      parts.forEach(part => {{
        if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {{
          parent.appendChild(document.createTextNode(part.slice(2, -2)));
        }} else if (/^\\[\\d+\\]$/.test(part)) {{
          return;
        }} else if (part) {{
          parent.appendChild(document.createTextNode(part));
        }}
      }});
    }}

    function renderSources(result, responseId = '') {{
      const sources = normalizeCitationSources(result.citation_details || []);
      if (!sources.length) return;
      const wrap = document.createElement('details');
      wrap.className = 'sources';
      const summary = document.createElement('summary');
      summary.textContent = `References (${{sources.length}})`;
      const list = document.createElement('div');
      list.className = 'source-list';
      sources.forEach(source => {{
        const item = document.createElement('div');
        item.className = 'source';
        item.id = `${{responseId}}-source-${{source.number || ''}}`;
        item.dataset.citationNumber = String(source.number || '');
        item.tabIndex = -1;
        const strong = document.createElement('strong');
        strong.textContent = source.reference || source.section || `Reference ${{source.number || ''}}`;
        const section = document.createElement('small');
        section.textContent = `Section: ${{source.section || 'not specified'}}`;
        const provenance = document.createElement('small');
        provenance.textContent = `Document: ${{source.source_file || 'local document'}}; chunk ${{source.chunk_number || 'unknown'}}; review status ${{source.validation_status || 'UNKNOWN'}}.`;
        item.append(strong, section, provenance);
        list.appendChild(item);
      }});
      wrap.append(summary, list);
      chatLog.appendChild(wrap);
      maybeScrollToBottom();
    }}

    function displayError(message, action) {{
      const node = document.createElement('div');
      node.className = 'error-note';
      node.textContent = `${{message}} ${{action || ''}}`.trim();
      chatLog.appendChild(node);
      maybeScrollToBottom();
    }}

    function maybeScrollToBottom() {{
      if (stickToBottom) chatLog.scrollTop = chatLog.scrollHeight;
    }}
  </script>
</body>
</html>"""
    return page.encode("utf-8")

def render_index() -> bytes:
    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GastroAGI Local Tools</title>
{style}
</head>
<body>
  <main>
    <header><div><h1>GastroAGI Local Tools</h1><p>Select a workspace.</p></div></header>
    <div class="layout">
      <section><h2>Chunks Admin Panel</h2><p>Upload PDFs and generate chunks.</p><p style="margin-top:14px;"><a href="/admin">Open Chunks Admin</a></p></section>
      <section><h2>BOT Page</h2><p>Check RAG answers against generated chunks.</p><p style="margin-top:14px;"><a href="/bot">Open BOT Page</a></p></section>
    </div>
  </main>
</body>
</html>""".format(style=_shared_page_style())
    return page.encode("utf-8")


def _stream_text_units(text: str) -> list[str]:
    """Split assistant text into small readable units for browser streaming."""
    units = re.findall(r"\S+\s*", text)
    return units or [text]
class LocalRagChatHandler(BaseHTTPRequestHandler):
    server_version = "GastroAGIPdfRagBuilder/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, render_index(), "text/html; charset=utf-8")
            return
        if parsed.path == "/admin":
            self._send(HTTPStatus.OK, render_admin_page(), "text/html; charset=utf-8")
            return
        if parsed.path == "/bot":
            self._send(HTTPStatus.OK, render_bot_page(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            chunks, files = load_generated_chunks()
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "pdfs": list_pdf_inputs(),
                    "chunk_count": len(chunks),
                    "chunk_files": files,
                    "local_database": local_db_status(),
                    "production_database_upload": database_upload_status(),
                    "mode": "pdf_generation_local_review",
                },
            )
            return
        if parsed.path == "/api/queue":
            self._send_json(HTTPStatus.OK, {"queue": processing_queue_status()})
            return
        if parsed.path == "/api/job-chunks":
            query = parse_qs(parsed.query)
            job_id = (query.get("id") or [""])[0]
            try:
                if not job_id:
                    raise LocalWorkflowError("Queue job id is required.")
                self._send_json(HTTPStatus.OK, load_job_chunks(job_id))
            except LocalWorkflowError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Generated chunks JSON is malformed."})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload-pdf":
            self._handle_upload_pdf()
            return
        if parsed.path == "/process-pdf":
            self._handle_process_pdf()
            return
        if parsed.path == "/queue/start":
            self._send_json(HTTPStatus.OK, start_processing_queue())
            return
        if parsed.path == "/queue/pause":
            self._send_json(HTTPStatus.OK, set_queue_paused(True))
            return
        if parsed.path == "/queue/resume":
            set_queue_paused(False)
            self._send_json(HTTPStatus.OK, {"status": "resumed", "start": start_processing_queue(), "queue": processing_queue_status()})
            return
        if parsed.path == "/queue/retry":
            self._handle_queue_job_action(retry_queue_job)
            return
        if parsed.path == "/queue/remove":
            self._handle_queue_job_action(remove_queued_job)
            return
        if parsed.path == "/sync-local-db":
            self._send_json(HTTPStatus.OK, sync_existing_outputs_to_local_db())
            return
        if parsed.path == "/chat":
            self._handle_chat()
            return
        if parsed.path == "/chat-stream":
            self._handle_chat_stream()
            return
        if parsed.path == "/ingest":
            self._send_json(HTTPStatus.CONFLICT, database_upload_status())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def _handle_upload_pdf(self) -> None:
        try:
            body = self._read_limited_body(MAX_UPLOAD_BYTES)
            headers = {key.lower(): value for key, value in self.headers.items()}
            batch = parse_multipart_pdf_upload_batch(headers, body)
            if not batch.uploads:
                if batch.rejected:
                    reasons = "; ".join(f"{item.filename}: {item.error}" for item in batch.rejected)
                    raise LocalWorkflowError(f"No valid PDFs were uploaded. {reasons}")
                raise LocalWorkflowError("Upload must include at least one valid PDF.")
            saved_paths = [save_uploaded_pdf(upload) for upload in batch.uploads]
            queued = enqueue_processing_jobs([path.name for path in saved_paths])
            start_processing_queue()
        except LocalWorkflowError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
            return
        response: dict[str, Any] = {
            "status": "uploaded",
            "files": [{"filename": path.name, "size_bytes": path.stat().st_size} for path in saved_paths],
            "rejected": [{"filename": item.filename, "error": item.error} for item in batch.rejected],
            "queued": queued,
            "queue": processing_queue_status(),
        }
        if len(saved_paths) == 1:
            response["filename"] = saved_paths[0].name
            response["size_bytes"] = saved_paths[0].stat().st_size
        self._send_json(HTTPStatus.OK, response)

    def _handle_queue_job_action(self, action: Any) -> None:
        try:
            body = self._read_limited_body(64 * 1024)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
                raise LocalWorkflowError("Request must include a queue job id string.")
            result = action(payload["id"])
            if action is retry_queue_job:
                start_processing_queue()
                result["queue"] = processing_queue_status()
            self._send_json(HTTPStatus.OK, result)
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON request."})
        except LocalWorkflowError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})

    def _handle_process_pdf(self) -> None:
        try:
            body = self._read_limited_body(64 * 1024)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("filename"), str):
                raise LocalWorkflowError("Request must include a PDF filename string.")
            result = process_uploaded_pdf(payload["filename"], force=bool(payload.get("force", False)))
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON request."})
            return
        except LocalWorkflowError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, result)

    def _handle_chat(self) -> None:
        try:
            payload = self._read_chat_payload()
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON request."})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
            return
        except LocalWorkflowError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            build_db_rag_response(payload["question"], history=payload.get("history", []), use_llm=True),
        )

    def _handle_chat_stream(self) -> None:
        try:
            payload = self._read_chat_payload()
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON request."})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
            return
        except LocalWorkflowError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in stream_db_rag_response(payload["question"], history=payload.get("history", [])):
                self._write_stream_event(event)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self._write_stream_event({"type": "error", "message": f"Chat streaming failed: {exc.__class__.__name__}", "recoverable": False})
                self._write_stream_event({"type": "done", "result": {"grounding_status": "STREAM_ERROR", "answer_mode": "error"}})
            except (BrokenPipeError, ConnectionResetError):
                return

    def _read_chat_payload(self) -> dict[str, Any]:
        body = self._read_limited_body(128 * 1024)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("question"), str):
            raise LocalWorkflowError("Question must be a string.")
        history = payload.get("history", [])
        if history is not None and not isinstance(history, list):
            raise LocalWorkflowError("History must be a list when provided.")
        clean_history = []
        for item in (history or [])[-12:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                clean_history.append({"role": role, "content": content[:2000]})
        return {"question": payload["question"], "history": clean_history}

    def _write_stream_event(self, payload: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()
    def _read_limited_body(self, max_bytes: int) -> bytes:
        try:
            length = int(self.headers.get("content-length", "0"))
        except (TypeError, ValueError) as exc:
            raise LocalWorkflowError("Content-Length must be a non-negative integer.") from exc
        if length < 0:
            raise LocalWorkflowError("Content-Length must be a non-negative integer.")
        if length > max_bytes:
            raise ValueError(f"Request exceeds {max_bytes} bytes.")
        return self.rfile.read(length)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local GastroAGI PDF RAG builder.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8766, help="Port to bind.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    if pipeline.load_dotenv is not None:
        pipeline.load_dotenv(PROJECT_ROOT / '.env')
    PDF_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), LocalRagChatHandler)
    stop_supervisor = threading.Event()
    supervisor = threading.Thread(target=supervise_processing_queue, args=(stop_supervisor,), daemon=True,
                                  name="gastroagi-queue-supervisor")
    supervisor.start()
    print(f"Local PDF RAG builder running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local PDF RAG builder.")
    finally:
        stop_supervisor.set()
        supervisor.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()






















