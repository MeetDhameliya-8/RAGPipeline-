import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import pipeline, local_rag_chat
from src.chunk_generator import generate_chunks, ChunkGenerationError
from src.providers.azure import AzureChunkGenerator
from test_p0_raw_schema import document


def extraction():
    return {"file_name": "paper.pdf", "full_text": "doi:10.1000/source123\n\n" + ("A complete source sentence. " * 400 + "\n\n") * 3}


def test_preflight_is_not_reported_as_sent_request():
    diagnostics = pipeline.build_generation_diagnostics(extraction())
    assert diagnostics["characters_sent_to_model"] == 0
    assert diagnostics["requests_started"] == 0
    assert diagnostics["planned_requests"] > 1


def test_later_unit_failure_preserves_truthful_candidate_counts():
    provider = Mock()
    provider.generate.side_effect = [json.dumps(document()), ChunkGenerationError("timeout")]
    diagnostics = {}
    with pytest.raises(ChunkGenerationError):
        generate_chunks(extraction(), provider=provider, diagnostics=diagnostics)
    assert diagnostics["requests_started"] == 2
    assert diagnostics["generated_candidates"] == 1
    assert diagnostics["structurally_valid_chunks"] == 1
    assert diagnostics["completed_units"] == 1
    report = pipeline.build_pipeline_failure_report("GENERATION_FAILED", "timeout", diagnostics)
    assert report["summary"]["generated_candidates"] == 1
    assert report["summary"]["clinically_approved"] == 0


def test_schema_rejected_candidates_are_counted_separately():
    provider = Mock()
    data = document()
    data["chunks"][0]["original_text"] = None
    provider.generate.return_value = json.dumps(data)
    diagnostics = {}
    with pytest.raises(ChunkGenerationError):
        generate_chunks(extraction(), provider=provider, diagnostics=diagnostics)
    assert diagnostics["generated_candidates"] == 1
    assert diagnostics["structurally_valid_chunks"] == 0
    assert diagnostics["units"][0]["outcome"] == "schema_invalid"


@pytest.mark.parametrize("finish, refusal, code", [
    ("length", None, "OUTPUT_TRUNCATED"), ("content_filter", None, "CONTENT_FILTERED"),
    ("stop", "Refused", "MODEL_REFUSAL"),
    (None, None, "MALFORMED_RESPONSE"),
])
def test_valid_json_is_not_accepted_after_abnormal_provider_outcome(finish, refusal, code):
    response = SimpleNamespace(id="test-id", usage=None,
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=json.dumps(document()), refusal=refusal))])
    client = Mock()
    client.chat.completions.create.return_value = response
    provider = AzureChunkGenerator("test-deployment", "never-log-this-key", "https://example.openai.azure.com", "2024-12-01-preview", client_factory=lambda **_: client)
    with pytest.raises(ChunkGenerationError) as captured:
        provider.generate("source", "paper.pdf", "prompt")
    assert captured.value.code == code
    assert "never-log-this-key" not in str(captured.value.diagnostics)


def test_review_counts_do_not_become_pass_or_approval(tmp_path):
    (tmp_path / "chunks.json").write_text(json.dumps(document()), encoding="utf-8")
    (tmp_path / "validation_report.json").write_text(json.dumps({
        "status": "REVIEW_REQUIRED", "summary": {"chunks": 1, "passed": 0, "review_required_chunks": 1, "rejected_chunks": 0},
        "chunk_statuses": [{"chunk": 1, "status": "REVIEW_REQUIRED"}], "issues": []}), encoding="utf-8")
    counts = local_rag_chat.build_chunk_count_summary({"output_dir": str(tmp_path), "validation_status": "REVIEW_REQUIRED"})
    assert counts["generated"] == 1
    assert counts["source_supported"] == 0
    assert counts["review_required"] == 1
    assert counts["clinically_approved"] == 0
    assert counts["validated"] == 0


def test_unknown_state_does_not_infer_validated_from_generated():
    counts = local_rag_chat.build_chunk_count_summary({"chunks": 3})
    assert counts["validated"] == 0
    assert counts["clinically_approved"] == 0


def test_partial_cli_job_is_in_summary_and_not_success(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "load_dotenv", None)
    monkeypatch.setattr(pipeline, "configure_logging", lambda: logging.getLogger("p0-test"))
    monkeypatch.setattr(pipeline, "process_pdf", lambda *_a, **_k: {"status": "partial_success"})
    assert pipeline.main(["--file", "paper.pdf"]) == 2
    assert "Partial/incomplete: 1" in capsys.readouterr().out


def test_partial_queue_job_is_not_complete_without_verified_local_drafts():
    assert local_rag_chat._terminal_queue_status_for_result({"status": "partial_success"}) == "FAILED"


def test_explicit_zero_readback_is_not_replaced_by_write_attempt_count():
    counts = local_rag_chat.build_chunk_count_summary({"local_database": {
        "chunks_seen": 6, "readback": {"stored_chunks": 0}}})
    assert counts["stored"] == 0


def test_unparseable_response_count_is_explicitly_incomplete():
    provider = Mock()
    provider.generate.return_value = '{"chunks": ['
    diagnostics = {}
    with pytest.raises(ChunkGenerationError):
        generate_chunks(extraction(), provider=provider, diagnostics=diagnostics)
    assert diagnostics["generated_candidates"] == 0
    assert diagnostics["candidate_count_complete"] is False
