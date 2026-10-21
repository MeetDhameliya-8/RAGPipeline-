"""Regressions for workflow defects found by the whole-project audit."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import io
from types import SimpleNamespace

import pytest

from src import local_chunk_store as store, local_rag_chat as web
from src import pdf_extractor
from src.pipeline import validated_chunk_subset, reviewable_chunk_subset
from src.source_map import build_source_map, initial_processing_state, mark_processed_sections_from_validation


def _package(tmp_path, name="article"):
    folder = tmp_path / name
    folder.mkdir()
    path = folder / "chunks.json"
    path.write_text(
        json.dumps({
            "article": {"source_file": f"{name}.pdf"},
            "chunks": [{
                "chunk_number": 1,
                "topic": "Crohn disease",
                "subtopic": "Treatment A",
                "section": "Primary endpoint",
                "reference": "Example Journal. 2024;1:1-8.",
                "original_text": "Treatment A did not significantly improve remission at week 6; remission was 15.2%.",
            }],
        }),
        encoding="utf-8",
    )
    return path


def test_reingestion_replaces_obsolete_package_chunks_only(tmp_path):
    db = tmp_path / "review.sqlite3"
    package = _package(tmp_path)
    other = _package(tmp_path, "other")
    store.ingest_chunks_file(package, "PASS", db_path=db)
    store.ingest_chunks_file(other, "PASS", db_path=db)
    data = json.loads(package.read_text(encoding="utf-8"))
    data["chunks"][0]["original_text"] = "Corrected Crohn disease assessment describes endoscopic recurrence."
    package.write_text(json.dumps(data), encoding="utf-8")

    result = store.ingest_chunks_file(package, "PASS", db_path=db)

    assert result["removed"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from chunks").fetchone()[0] == 2
        assert conn.execute("select count(*) from embeddings").fetchone()[0] == 2
        assert conn.execute("select count(*) from chunks where output_dir='article'").fetchone()[0] == 1
    assert store.verify_chunks_file_ingested(package, "PASS", db_path=db)["verified"]


def test_unchanged_reingestion_reuses_embedding_and_does_not_grow_audit(monkeypatch, tmp_path):
    db = tmp_path / "review.sqlite3"
    package = _package(tmp_path)
    store.ingest_chunks_file(package, "PASS", db_path=db)
    with sqlite3.connect(db) as conn:
        audit_before = conn.execute("select count(*) from ingestion_audit").fetchone()[0]
    monkeypatch.setattr(store, "embed_text", lambda *_: pytest.fail("Unchanged content was re-embedded"))

    result = store.ingest_chunks_file(package, "PASS", db_path=db)

    assert result["unchanged"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from ingestion_audit").fetchone()[0] == audit_before


def test_invalid_replacement_cannot_remove_existing_package(tmp_path):
    db = tmp_path / "review.sqlite3"
    package = _package(tmp_path)
    store.ingest_chunks_file(package, "PASS", db_path=db)
    data = json.loads(package.read_text(encoding="utf-8"))
    data["chunks"].append({"chunk_number": 2, "topic": "Incomplete"})
    package.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        store.ingest_chunks_file(package, "PASS", db_path=db)

    assert store.db_status(db)["chunk_count"] == 1


def test_package_replacement_rolls_back_if_embedding_fails(monkeypatch, tmp_path):
    db = tmp_path / "review.sqlite3"
    package = _package(tmp_path)
    store.ingest_chunks_file(package, "PASS", db_path=db)
    data = json.loads(package.read_text(encoding="utf-8"))
    data["chunks"][0]["original_text"] = "Corrected Crohn disease finding."
    package.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(store, "embed_text", lambda *_: (_ for _ in ()).throw(RuntimeError("injected failure")))

    with pytest.raises(RuntimeError, match="injected failure"):
        store.ingest_chunks_file(package, "PASS", db_path=db)

    with sqlite3.connect(db) as conn:
        assert "15.2%" in conn.execute("select original_text from chunks").fetchone()[0]
        assert conn.execute("select count(*) from embeddings").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["UNKNOWN", "", "REJECTED"])
def test_unverified_validation_state_is_not_indexed(tmp_path, status):
    db = tmp_path / "review.sqlite3"
    result = store.ingest_chunks_file(_package(tmp_path), status, db_path=db)
    assert result["enabled"] is False
    assert store.db_status(db)["chunk_count"] == 0


def test_missing_chunk_status_is_never_promoted_to_pass(tmp_path):
    data = json.loads(_package(tmp_path).read_text(encoding="utf-8"))
    report = {"status": "FAILED", "chunk_statuses": []}
    assert validated_chunk_subset(data, report)["chunks"] == []
    assert reviewable_chunk_subset(data, report)["chunks"] == []


def test_document_error_blocks_chunk_approval_and_checkpoint_completion(tmp_path):
    data = json.loads(_package(tmp_path).read_text(encoding="utf-8"))
    source = build_source_map({
        "file_name": "article.pdf",
        "page_count": 1,
        "pages": [{"page_number": 1, "text": "Results\n\n" + "Clinical remission was assessed in adults. " * 30}],
    })
    section = next(section for section in source.sections if section.status == "pending")
    report = {
        "status": "FAILED",
        "issues": [{"severity": "ERROR", "rule": "SCHEMA", "message": "Invalid article contract."}],
        "chunk_statuses": [{"chunk": 1, "status": "PASS"}],
        "source_evidence": [{"chunk": 1, "source_section_ids": [section.section_id]}],
    }

    assert validated_chunk_subset(data, report)["chunks"] == []
    assert reviewable_chunk_subset(data, report)["chunks"] == []
    state = mark_processed_sections_from_validation(source, initial_processing_state(source), report)
    assert all(record.status != "processed" for record in state.sections)


def test_future_publication_cannot_satisfy_recent_window(tmp_path):
    package = _package(tmp_path)
    data = json.loads(package.read_text(encoding="utf-8"))
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    data["chunks"][0]["reference"] = f"Example Journal. {tomorrow};1:1-8."
    package.write_text(json.dumps(data), encoding="utf-8")
    db = tmp_path / "review.sqlite3"
    store.ingest_chunks_file(package, "PASS", db_path=db)

    response = store.build_db_rag_response("Recent Crohn research from the last 7 days", db_path=db)

    assert response["grounding_status"] == "RECENT_RESEARCH_UNAVAILABLE"
    assert response["retrieved_chunks"] == []
    assert "7-day" in response["answer"]
    assert "30-day" not in response["answer"]


@pytest.mark.parametrize("answer", [
    "Treatment A improved remission.",
    "Treatment A produced remission in 99.9% at week 6 [1].",
    "Treatment A significantly improved remission at week 6 [1].",
    "Treatment A permanently cures Crohn disease [1].",
])
def test_valid_citation_id_does_not_approve_unsupported_answer(tmp_path, answer):
    db = tmp_path / "review.sqlite3"
    store.ingest_chunks_file(_package(tmp_path), "PASS", db_path=db)
    hits = store.retrieve("Crohn Treatment A remission", db_path=db)
    assert store._validated_grounded_answer(answer, hits) is None


def test_supported_statistical_answer_passes_grounding_gate(tmp_path):
    db = tmp_path / "review.sqlite3"
    store.ingest_chunks_file(_package(tmp_path), "PASS", db_path=db)
    hits = store.retrieve("Crohn Treatment A remission", db_path=db)
    answer = "Treatment A did not significantly improve remission at week 6; remission was 15.2% [1]."
    assert store._validated_grounded_answer(answer, hits) == answer


def test_unsupported_stream_is_discarded_before_any_text_is_released(monkeypatch, tmp_path):
    db = tmp_path / "review.sqlite3"
    store.ingest_chunks_file(_package(tmp_path), "PASS", db_path=db)

    class Provider:
        def stream_generate(self, *_):
            yield "Remission was 99.9%"
            yield " at week 6 [1]."

    monkeypatch.setattr(store.AzureChunkGenerator, "from_environment", staticmethod(lambda: Provider()))
    events = list(store.stream_db_rag_response("Crohn remission findings", db_path=db))
    released = "".join(event.get("text", "") for event in events if event["type"] == "delta")

    assert "99.9%" not in released
    assert "15.2%" in released
    assert events[-1]["result"]["answer_mode"] == "extractive_synthesis_after_stream_error"


def test_storage_ready_requires_readback_verification():
    result = {
        "status": "processed",
        "local_database": {"status": "LOCAL_REVIEW_DB_STORED", "readback_verified": False},
        "production_database_upload": {"enabled": False},
    }
    assert web._terminal_queue_status_for_result(result) == "BLOCKED_STORAGE"


@pytest.mark.parametrize("length", ["-1", "invalid"])
def test_invalid_content_length_is_rejected_before_read(length):
    handler = object.__new__(web.LocalRagChatHandler)
    handler.headers = {"content-length": length}
    handler.rfile = io.BytesIO(b"must not be read")
    with pytest.raises(web.LocalWorkflowError, match="Content-Length"):
        handler._read_limited_body(128)
    assert handler.rfile.tell() == 0


def test_stale_recovery_cannot_requeue_work_owned_by_running_worker(monkeypatch):
    monkeypatch.setattr(web, "_queue_worker_running", True)
    monkeypatch.setattr(web, "init_processing_queue_db", lambda: pytest.fail("Touched live queue"))
    assert web.recover_stale_queue_jobs(max_age_seconds=0) == 0


def test_raw_document_evidence_is_not_replaced_by_cleaned_pages(monkeypatch, tmp_path):
    import pypdf

    raw = "Clinical article\n\nRemission was 15.2% at week 6.\n\nDownloaded from example.org"
    page = SimpleNamespace(extract_text=lambda **_: raw)
    monkeypatch.setattr(pypdf, "PdfReader", lambda *_: SimpleNamespace(pages=[page]))
    monkeypatch.setattr(pdf_extractor, "_extract_pymupdf_pages", lambda *_: [])
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    extraction = pdf_extractor.extract_pdf(pdf)

    assert extraction["raw_full_text"] == raw
    assert "Downloaded from" not in extraction["full_text"]
    assert "15.2%" in extraction["full_text"]


def test_fifty_local_packages_remain_idempotent_and_searchable(tmp_path):
    db = tmp_path / "review.sqlite3"
    packages = [_package(tmp_path, f"article-{number}") for number in range(50)]
    for package in packages:
        store.ingest_chunks_file(package, "PASS", db_path=db)
    for package in packages:
        assert store.ingest_chunks_file(package, "PASS", db_path=db)["unchanged"] == 1

    assert store.db_status(db)["package_count"] == 50
    assert store.db_status(db)["chunk_count"] == 50
    assert len(store.retrieve("Crohn remission", top_k=50, db_path=db)) == 50


def test_excerpt_never_truncates_statistics_or_qualifications_mid_sentence():
    sentence = "Crohn disease " + "clinical assessment " * 30
    sentence += "remission was 15.2% versus 12.1% (P = .433; 95% CI 0.7-2.2), not statistically significant."
    excerpt = store.extract_relevant_excerpt(sentence, ["crohn"], max_chars=100)
    assert excerpt == sentence
    assert excerpt.endswith("not statistically significant.")


def test_hybrid_page_selection_rejects_font_codes_beside_clinical_numbers():
    clean = "The platelet count was 50,000 and the reported statistic was 0.44. " * 10
    corrupt = clean.replace("50,000", "/H1102150,000").replace("0.44", "/H110030.44")
    assert pdf_extractor._should_use_pymupdf_page(corrupt, clean) is True
    assert pdf_extractor._should_use_pymupdf_page(clean, corrupt) is False


def test_unresolved_font_codes_raise_extraction_review_warning():
    text = "Clinical article body " * 50 + "/H1102150,000"
    warnings = pdf_extractor._quality_warnings([{"page_number": 1, "raw_text": text}], text)
    assert any("unresolved PDF font codes" in warning and "require review" in warning for warning in warnings)


def test_test_reset_refuses_the_real_user_database(monkeypatch):
    import pytest
    from src import local_rag_chat as web
    monkeypatch.setattr(web,'LOCAL_DB_PATH',web.PROJECT_ROOT/'local_db'/'gastroagi_chunks.sqlite3')
    with pytest.raises(RuntimeError,match='isolated'):
        web._reset_processing_queue_for_tests()
