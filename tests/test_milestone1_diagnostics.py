"""Milestone 1: synthetic sources, temporary state and fake transport only."""
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from src import diagnostics as telemetry, pipeline, chunk_generator, local_rag_chat as web
from src.chunk_generator import generate_chunks
from src.chunk_validator import validate_chunks
from src.providers.azure import AzureChunkGenerator
from src.providers.base import ChunkGenerationError, GenerationResponse
from test_source_packages import fixture, response, TEXT


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def run_packages(tmp_path, provider, *, pages=1, directory=None, observation=None):
    e, mapped, plan = fixture(tmp_path, pages=pages)
    audit = {}
    with telemetry.bind(observation):
        generated = generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan,
            diagnostics=audit, attempt_directory=directory, package_concurrency=2)
    return generated, audit, e, mapped, plan


def test_nested_fake_clock_excludes_children_and_records_failure():
    clock = Clock()
    observation = telemetry.Observation(clock)
    with telemetry.bind(observation):
        with telemetry.stage("outer"):
            clock.advance(2)
            with telemetry.stage("inner"):
                clock.advance(3)
            clock.advance(1)
        with pytest.raises(ValueError):
            with telemetry.stage("failed"):
                clock.advance(4)
                raise ValueError("confidential")
    stages = observation.snapshot()["stages"]
    assert stages["outer"]["inclusive_seconds"] == 6
    assert stages["outer"]["exclusive_seconds"] == 3
    assert stages["inner"]["exclusive_seconds"] == 3
    assert stages["failed"]["exclusive_seconds"] == 4
    assert stages["failed"]["failures"] == 1


def test_synthetic_pipeline_timings_and_storage_preserve_decisions(tmp_path, monkeypatch):
    import fitz
    e, _, _ = fixture(tmp_path)
    path = tmp_path / "prose.pdf"
    with fitz.open(path) as pdf:
        pdf[0].insert_text((40, 30), "doi:10.1234/fixture123", fontsize=10)
        pdf.saveIncr()
    monkeypatch.setattr(chunk_generator, "_provider_from_environment", lambda: provider)
    provider = Mock()
    provider.generate.return_value = response(e)
    result = pipeline.process_pdf(path, draft_packages=True, test_generation=True,
        logger=logging.getLogger("synthetic-milestone1"))
    assert result["status"] == "partial_success"
    assert result["validation_status"] == "REVIEW_REQUIRED"
    snapshot = result["observability"]
    assert {"checkpoint_lookup", "native_extraction", "evidence_construction", "source_mapping",
        "native_layout", "planning", "generation", "local_chunk_validation",
        "final_document_validation", "output_persistence"} <= snapshot["stages"].keys()
    with telemetry.bind(telemetry.Observation()):
        stored = web.store_generated_chunks_for_result(result)
        assert stored["readback_verified"]
        final = telemetry.current().snapshot()
    assert {"sqlite_storage", "sqlite_readback"} <= final["stages"].keys()
    assert final["counts"]["final_stored_rows"] == 1
    assert snapshot["counts"]["saved_review_drafts"] == 1
    assert snapshot["request_totals"]["logical_calls"] == 1
    assert snapshot["run_complete"] and snapshot["wall_seconds"] >= 0
    report = json.loads((Path(result["output_dir"])/"validation_report.json").read_text())
    assert not report["diagnostics"]["observability"]["run_complete"]
    saved = json.loads((Path(result["output_dir"])/"chunks.json").read_text())
    assert saved["chunks"][0]["original_text"] == TEXT
    assert set(saved["chunks"][0]) == {"topic", "subtopic", "section", "reference", "original_text", "chunk_number"}


def test_provider_timeout_counts_every_http_attempt_and_unknown_usage(tmp_path):
    client = Mock()
    client.chat.completions.create.side_effect = TimeoutError("private.pdf SECRET clinical text")
    provider = AzureChunkGenerator("offline", "SECRET", "https://example.invalid", "offline",
        client_factory=lambda **_: client, sleep=lambda _: None)
    e, mapped, plan = fixture(tmp_path)
    audit = {}
    with pytest.raises(ChunkGenerationError) as caught:
        generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan, diagnostics=audit)
    assert caught.value.code == "TRANSPORT_TIMEOUT"
    snap = audit["observability"]
    totals = snap["request_totals"]
    assert totals["logical_calls"] == 1 and totals["failed_calls"] == 1
    assert totals["http_attempts_observed"] == totals["http_failures"] == 3
    assert totals["unknown_usage_attempts"] == 3 and not totals["token_usage_complete"]
    assert not snap["candidate_accounting_complete"]
    assert "SECRET" not in json.dumps(snap)


def test_retry_then_success_keeps_observed_tokens_partial():
    client = Mock()
    client.chat.completions.create.side_effect = [TimeoutError("private"), NS(
        choices=[NS(finish_reason="stop", message=NS(content="{}", refusal=None))],
        usage=NS(prompt_tokens=10, completion_tokens=4, total_tokens=14), id="test")]
    provider = AzureChunkGenerator("offline", "secret", "https://example.invalid", "offline",
        client_factory=lambda **_: client, sleep=lambda _: None)
    observation = telemetry.Observation()
    with telemetry.bind(observation):
        assert telemetry.model_call("generation", provider.generate, "private", "private.pdf", "private") == "{}"
    totals = observation.snapshot()["request_totals"]
    assert totals["logical_calls"] == totals["successful_responses"] == 1
    assert totals["http_attempts_observed"] == 2
    assert totals["http_failures"] == totals["http_successes"] == 1
    assert totals["prompt_tokens_observed"] == 10 and totals["completion_tokens_observed"] == 4
    assert totals["unknown_usage_attempts"] == 1 and not totals["token_usage_complete"]


def test_resumed_candidate_has_zero_calls_and_identical_content(tmp_path):
    e, mapped, plan = fixture(tmp_path)
    provider = Mock()
    provider.generate.return_value = response(e)
    kwargs = dict(provider=provider, source_doc=mapped, generation_plan=plan, attempt_directory=tmp_path/"units")
    first = generate_chunks(e, **kwargs)
    provider.generate.side_effect = AssertionError("Network prohibited")
    audit = {}
    assert generate_chunks(e, diagnostics=audit, **kwargs) == first
    snap = audit["observability"]
    assert snap["counts"]["resumed_candidate_units"] == snap["counts"]["resumed_candidates"] == 1
    assert snap["request_totals"]["logical_calls"] == snap["request_totals"]["http_attempts_observed"] == 0
    assert snap["candidate_accounting_complete"]


def test_source_validation_failure_and_cached_hold_do_not_pay_again(tmp_path):
    e, mapped, plan = fixture(tmp_path)
    provider = Mock()
    provider.generate.return_value = response(e, TEXT+" There were 999999999 participants.")
    kwargs = dict(provider=provider, source_doc=mapped, generation_plan=plan, attempt_directory=tmp_path/"units")
    first = {}
    with pytest.raises(ChunkGenerationError) as caught:
        generate_chunks(e, diagnostics=first, **kwargs)
    assert caught.value.code == "SOURCE_VALIDATION_FAILED"
    snap = first["observability"]
    assert snap["counts"]["rejected_candidate_events"] == 2
    assert snap["counts"]["source_validation_held_units"] == 1
    assert snap["request_totals"]["correction_calls"] == 1
    assert snap["candidate_accounting_complete"]
    provider.generate.side_effect = AssertionError("Network prohibited")
    second = {}
    with pytest.raises(ChunkGenerationError):
        generate_chunks(e, diagnostics=second, **kwargs)
    assert second["observability"]["counts"]["cached_validation_hold_units"] == 1
    assert second["observability"]["request_totals"]["logical_calls"] == 0


def test_partial_acceptance_does_not_regenerate(tmp_path):
    e, mapped, plan = fixture(tmp_path)
    payload = json.loads(response(e))
    bad = dict(payload["chunks"][0], section="Unsupported numerical outcome",
        original_text=TEXT+" There were 999999999 participants.")
    payload["chunks"].append(bad)
    payload["article"]["number_of_chunks"] = 2
    provider = Mock()
    provider.generate.return_value = json.dumps(payload)
    audit = {}
    generated = generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan, diagnostics=audit)
    counts = audit["observability"]["counts"]
    assert counts["generated_candidates"] == 2
    assert counts["accepted_candidate_events"] == counts["rejected_candidate_events"] == 1
    assert len(generated["chunks"]) == counts["merged_candidates"] == 1
    assert audit["observability"]["request_totals"]["correction_calls"] == 0
    assert audit["observability"]["candidate_accounting_complete"]


def test_correction_events_reconcile_without_hiding_rejected_attempt(tmp_path):
    e, mapped, plan = fixture(tmp_path)
    provider = Mock()
    provider.generate.side_effect = [response(e, TEXT+" There were 999999999 participants."), response(e)]
    audit = {}
    generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan, diagnostics=audit)
    snap = audit["observability"]
    assert snap["request_totals"]["logical_calls"] == 2
    assert snap["request_totals"]["correction_calls"] == 1
    assert snap["counts"]["generated_candidates"] == snap["counts"]["assessed_candidate_events"] == 2
    assert snap["counts"]["accepted_candidate_events"] == snap["counts"]["rejected_candidate_events"] == 1
    assert {"correction", "local_chunk_validation"} <= snap["stages"].keys()


def test_concurrent_diagnostics_do_not_lose_events_or_change_duplicate_decision(tmp_path):
    e, mapped, plan = fixture(tmp_path, pages=4)
    provider = Mock()
    provider.generate.return_value = response(e)
    audit = {}
    generated = generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan,
        diagnostics=audit, package_concurrency=2)
    snap = audit["observability"]
    assert snap["request_totals"]["logical_calls"] == 4
    assert sorted(c["call_id"] for c in snap["model_calls"]) == [1, 2, 3, 4]
    assert snap["counts"]["generated_candidates"] == 4
    assert snap["counts"]["duplicate_candidates"] == 3 and len(generated["chunks"]) == 1
    assert snap["candidate_accounting_complete"]
    observation = telemetry.Observation()
    def worker():
        with telemetry.bind(observation):
            for _ in range(100):
                telemetry.count("events")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: worker(), range(2)))
    assert observation.snapshot()["counts"]["events"] == 200


@pytest.mark.parametrize("phase,category", [("json", "INVALID_JSON"), ("schema", "SCHEMA_INVALID"),
    ("reference", "REFERENCE_OR_CONTENT_INVALID")])
def test_original_code_preserved_separately_from_legacy_category(tmp_path, monkeypatch, phase, category):
    e, mapped, plan = fixture(tmp_path)
    provider = Mock()
    provider.generate.return_value = response(e)
    original = ChunkGenerationError("SECRET clinical text private.pdf", code="SOURCE_FIDELITY")
    target = {"json": "parse_model_json", "schema": "normalize_generation_result", "reference": "bind_verified_references"}[phase]
    monkeypatch.setattr(chunk_generator, target, Mock(side_effect=original))
    audit = {}
    with pytest.raises(ChunkGenerationError) as caught:
        generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan, diagnostics=audit)
    assert caught.value.code == category  # Existing retry/queue behavior preserved.
    snap = audit["observability"]
    assert any(f["original_code"] == "SOURCE_FIDELITY" and f["category"] == category for f in snap["failures"])
    assert "SECRET" not in json.dumps(snap) and "private.pdf" not in json.dumps(snap)


def test_untrusted_metadata_and_exception_contents_are_never_exported():
    observation = telemetry.Observation()
    provider = Mock(return_value=GenerationResponse("private clinical passage", {
        "usage": {"prompt_tokens": 5, "completion_tokens": 3}, "secret": "SECRET", "filename": "private.pdf"}))
    with telemetry.bind(observation):
        telemetry.model_call("generation", provider)
        exc = ChunkGenerationError("SECRET private clinical passage", code="SECRET_private.pdf",
            diagnostics={"failure_classification": {"original_code": "SECRET"}, "error": "SECRET"})
        telemetry.failure(exc, "SCHEMA_INVALID")
    serialized = json.dumps(observation.snapshot())
    assert all(value not in serialized for value in ["SECRET", "private.pdf", "private clinical passage"])
    assert not observation.snapshot()["request_totals"]["http_attempts_complete"]
    assert observation.snapshot()["request_totals"]["prompt_tokens_observed"] == 5
    assert not observation.snapshot()["request_totals"]["token_usage_complete"]


def test_logging_does_not_emit_source_filenames_or_exception_text(tmp_path, monkeypatch, caplog):
    path = tmp_path/"confidential-patient.pdf"
    path.write_bytes(b"synthetic")
    monkeypatch.setattr(pipeline, "extract_pdf", Mock(side_effect=RuntimeError("SECRET medical passage")))
    with caplog.at_level(logging.INFO):
        result = pipeline.process_pdf(path, logger=logging.getLogger("synthetic-redaction"))
        pipeline._log_validation_issues({"issues": [{"message": "SECRET medical passage", "rule": "SECRET"}]},
            logging.getLogger("synthetic-redaction"))
    assert result["status"] == "failed"
    assert "confidential-patient" not in caplog.text and "SECRET" not in caplog.text
    assert "Traceback" not in caplog.text
    report = json.loads((Path(result["output_dir"])/"validation_report.json").read_text())
    assert "SECRET" not in json.dumps(report)
    assert report["diagnostics"]["observability"]["failures"][0]["category"] == "EXTRACTION_FAILED"
    assert "output_persistence" in result["observability"]["stages"]
    assert "SECRET" not in result["reason"]


def test_instrumentation_does_not_change_validation_report(tmp_path):
    e, _, _ = fixture(tmp_path)
    generated = json.loads(response(e))
    kwargs = {"chunk_sources": {1: [{"text": TEXT, "review_required": True}]}}
    baseline = validate_chunks(generated, **kwargs)
    observation = telemetry.Observation()
    with telemetry.bind(observation), telemetry.stage("final_document_validation"):
        observed = validate_chunks(generated, **kwargs)
    assert observed == baseline and observed["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("change,reason", [("schema", "RAW_SCHEMA_INVALID"),
    ("reference", "REFERENCE_DOI_NOT_IN_SOURCE")])
def test_real_failure_reason_is_precise_without_exception_message(tmp_path, change, reason):
    e, mapped, plan = fixture(tmp_path)
    payload = json.loads(response(e))
    if change == "schema":
        payload["chunks"][0]["original_text"] = None
    else:
        payload["chunks"][0]["reference"] = "doi:10.1234/not-owned"
    provider = Mock()
    provider.generate.return_value = json.dumps(payload)
    audit = {}
    with pytest.raises(ChunkGenerationError):
        generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan, diagnostics=audit)
    assert any(f.get("reason_code") == reason for f in audit["observability"]["failures"])
    assert audit["unit_failures"][0]["failure_classification"]["reason_code"] == reason


def test_visual_pass_timings_and_cache_have_zero_second_run_requests(tmp_path):
    from test_visual_source import scan
    from src.visual_source import recover_visual_source
    path, extraction, text = scan(tmp_path)
    provider = Mock()
    provider.transcribe_image.return_value = json.dumps({"paragraphs": [text], "uncertainties": []})
    first = telemetry.Observation()
    with telemetry.bind(first):
        recover_visual_source(path, extraction, provider=provider, cache_directory=tmp_path/"visual")
    assert first.snapshot()["stages"]["transcription_pass"]["calls"] == 2
    assert len(first.snapshot()["stages"]["transcription_pass"]["samples_seconds"]) == 2
    assert first.snapshot()["request_totals"]["logical_calls"] == 2
    provider.transcribe_image.side_effect = AssertionError("Network prohibited")
    second = telemetry.Observation()
    with telemetry.bind(second):
        recover_visual_source(path, extraction, provider=provider, cache_directory=tmp_path/"visual")
    assert second.snapshot()["request_totals"]["logical_calls"] == 0
    assert second.snapshot()["counts"]["cached_transcription_passes"] == 2


def test_partial_token_usage_retains_known_values_without_claiming_completeness():
    observation = telemetry.Observation()
    with telemetry.bind(observation):
        telemetry.model_call("generation", lambda: GenerationResponse("{}", {
            "usage": {"prompt_tokens": 12, "completion_tokens": None}}))
    totals = observation.snapshot()["request_totals"]
    assert totals["prompt_tokens_observed"] == 12
    assert totals["unknown_usage_attempts"] == 1 and not totals["token_usage_complete"]


def test_http_error_metadata_is_allowlisted_and_response_usage_is_known():
    class RateLimitError(Exception):
        status_code = 429
        response = NS(headers={}, json=lambda: {"error": {"code": "rate_limit_exceeded", "message": "SECRET"}})
    client = Mock()
    client.chat.completions.create.side_effect = [RateLimitError("private.pdf SECRET"), NS(
        choices=[NS(finish_reason="stop", message=NS(content="{}", refusal=None))],
        usage=NS(prompt_tokens=10, completion_tokens=4, total_tokens=14), id="SECRET")]
    provider = AzureChunkGenerator("SECRET", "SECRET", "https://example.invalid", "offline",
        client_factory=lambda **_: client, sleep=lambda _: None)
    observation = telemetry.Observation()
    with telemetry.bind(observation):
        telemetry.model_call("generation", provider.generate, "SECRET", "private.pdf", "SECRET")
    record = observation.snapshot()["model_calls"][0]
    assert record["http_statuses"] == [429, None]
    assert record["provider_error_codes"] == ["rate_limit_exceeded"]
    assert "SECRET" not in json.dumps(observation.snapshot())


def test_fake_clock_separates_http_work_from_backoff_without_double_counting():
    clock = Clock()
    observation = telemetry.Observation(clock)
    calls = []
    def create(**kwargs):
        clock.advance(2)
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("private")
        return NS(choices=[NS(finish_reason="stop", message=NS(content="{}", refusal=None))],
            usage=NS(prompt_tokens=10, completion_tokens=4), id="fake")
    client = NS(chat=NS(completions=NS(create=create)))
    provider = AzureChunkGenerator("offline", "secret", "https://example.invalid", "offline",
        client_factory=lambda **_: client, sleep=clock.advance)
    with telemetry.bind(observation):
        telemetry.model_call("generation", provider.generate, "private", "private.pdf", "private")
    stages = observation.snapshot()["stages"]
    assert stages["http_attempt"]["samples_seconds"] == [2, 2]
    assert stages["http_attempt"]["failures"] == 1
    assert stages["retry_backoff"]["exclusive_seconds"] == 1
    assert stages["llm_request"]["inclusive_seconds"] == 5
    assert stages["llm_request"]["exclusive_seconds"] == 0


def test_sdk_local_failure_is_not_reported_as_a_proven_http_attempt():
    client = Mock()
    client.chat.completions.create.side_effect = TypeError("SECRET invalid local SDK input")
    provider = AzureChunkGenerator("offline", "secret", "https://example.invalid", "offline",
        client_factory=lambda **_: client)
    observation = telemetry.Observation()
    with telemetry.bind(observation), pytest.raises(ChunkGenerationError):
        telemetry.model_call("generation", provider.generate, "private", "private.pdf", "private")
    totals = observation.snapshot()["request_totals"]
    assert totals["logical_calls"] == totals["failed_calls"] == 1
    assert totals["http_attempts_observed"] == 0
    assert totals["unknown_usage_attempts"] == 1
    assert not totals["http_attempts_complete"] and not totals["token_usage_complete"]
