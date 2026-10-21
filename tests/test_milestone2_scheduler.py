"""Rolling scheduling must preserve source order, checkpoints and failure safety."""
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
import json
from threading import Barrier, Event, Lock
from unittest.mock import Mock

import pytest

from src import bounded_scheduler as bs, package_generation as pg
from src.chunk_generator import generate_chunks
from src.providers.base import ChunkGenerationError
from milestone2_support import fixed_groups, packet_index, packet_response, source


def generate(extraction, mapped, plan, provider, audit, directory=None, **kwargs):
    return generate_chunks(extraction, source_doc=mapped, generation_plan=plan, provider=provider,
        diagnostics=audit, attempt_directory=directory, package_concurrency=2, **kwargs)


def test_free_worker_is_replenished_before_unrelated_earlier_request_finishes():
    third = Event()
    lock = Lock()
    active = maximum = 0
    order = []
    def worker(i):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if i == 0:
                assert third.wait(3), "A group barrier left the free worker idle."
            if i == 2:
                third.set()
            with lock:
                order.append(i)
            return i
        finally:
            with lock:
                active -= 1
    result = bs.run_bounded(worker, 8, 2, bs.SubmissionControl())
    assert result.interruption is None and not any(o.error for o in result.outcomes)
    assert [o.value for o in result.outcomes] == list(range(8))
    assert order.index(2) < order.index(0)
    assert maximum == result.max_in_flight == 2


def test_executor_never_has_a_document_sized_submission_queue(monkeypatch):
    class Executor(ThreadPoolExecutor):
        outstanding = maximum = 0
        lock = Lock()
        def submit(self, fn, *args, **kwargs):
            with self.lock:
                self.outstanding += 1
                self.maximum = max(self.maximum, self.outstanding)
            future = super().submit(fn, *args, **kwargs)
            def completed(_):
                with self.lock:
                    self.outstanding -= 1
            future.add_done_callback(completed)
            return future
    instances = []
    def factory(**kwargs):
        executor = Executor(**kwargs)
        instances.append(executor)
        return executor
    monkeypatch.setattr(bs, "ThreadPoolExecutor", factory)
    result = bs.run_bounded(lambda i: i, 100, 2, bs.SubmissionControl())
    assert len(result.outcomes) == 100 and instances[0].maximum <= 2


@pytest.mark.parametrize("concurrency", [1, 2])
def test_serial_limit_and_empty_or_pre_cancelled_schedule(concurrency):
    result = bs.run_bounded(lambda i: i, 5, concurrency, bs.SubmissionControl())
    assert result.max_in_flight == concurrency
    assert [o.value for o in result.outcomes] == list(range(5))
    control = bs.SubmissionControl()
    control.stop()
    worker = Mock(side_effect=AssertionError("Cancelled work must not start"))
    assert not bs.run_bounded(worker, 5, concurrency, control).outcomes
    assert not bs.run_bounded(worker, 0, concurrency, bs.SubmissionControl()).outcomes
    worker.assert_not_called()


def test_full_generation_is_identical_to_group_baseline_in_source_order(tmp_path, monkeypatch):
    e, mapped, plan = source(tmp_path)
    provider = Mock()
    provider.generate.side_effect = lambda text, *_: packet_response(e, packet_index(text))
    old = {}
    rolling = pg._run_source_schedule
    monkeypatch.setattr(pg, "_run_source_schedule", fixed_groups)
    expected = generate(e, mapped, plan, provider, old)
    monkeypatch.setattr(pg, "_run_source_schedule", rolling)
    actual_audit = {}
    actual = generate(e, mapped, plan, provider, actual_audit)
    assert actual == expected
    assert actual_audit["chunk_ownership"] == old["chunk_ownership"]
    assert actual_audit["validation_rejections"] == old["validation_rejections"]
    assert actual_audit["unit_failures"] == old["unit_failures"]
    from src.chunk_validator import validate_chunks
    from src.source_packages import package_validation_sources
    assert validate_chunks(actual, chunk_sources=package_validation_sources(plan.source_package_plan, mapped.evidence, actual_audit)) == validate_chunks(
        expected, chunk_sources=package_validation_sources(plan.source_package_plan, mapped.evidence, old))
    assert [c["chunk_number"] for c in actual["chunks"]] == list(range(1, 7))
    assert actual_audit["scheduler"]["max_in_flight"] == 2


def test_out_of_order_provider_completion_preserves_full_output_and_audit_order(tmp_path):
    e, mapped, plan = source(tmp_path)
    later_started = Event()
    finished = []
    def call(text, *_):
        index = packet_index(text)
        if index == 0:
            assert later_started.wait(3)
        if index == 2:
            later_started.set()
        finished.append(index)
        return packet_response(e, index)
    provider = Mock()
    provider.generate.side_effect = call
    audit = {}
    generated = generate(e, mapped, plan, provider, audit)
    assert finished.index(2) < finished.index(0)
    expected = [json.loads(packet_response(e, i))["chunks"][0] for i in range(6)]
    assert generated["chunks"] == [dict(c, chunk_number=i) for i, c in enumerate(expected, 1)]
    assert [r["unit_ids"] for r in audit["chunk_ownership"]] == [[p.unit_id] for p in plan.source_package_plan.packages]
    assert [u["unit_number"] for u in audit["units"]] == list(range(1, 7))


@pytest.mark.parametrize("code", ["AUTHENTICATION_FAILED", "QUOTA_EXHAUSTED"])
def test_fatal_failure_stops_refill_while_slow_inflight_work_is_saved(tmp_path, monkeypatch, code):
    e, mapped, plan = source(tmp_path)
    control = bs.SubmissionControl()
    monkeypatch.setattr(pg, "SubmissionControl", lambda: control)
    slow_started = Event()
    calls = []
    def call(text, *_):
        index = packet_index(text)
        calls.append(index)
        if index == 0:
            slow_started.set()
            assert control.stopped.wait(3)
            return packet_response(e, index)
        assert slow_started.wait(3)
        raise ChunkGenerationError("private provider message", code=code)
    provider = Mock()
    provider.generate.side_effect = call
    audit = {}
    result = generate(e, mapped, plan, provider, audit, tmp_path/"attempts")
    assert sorted(calls) == [0, 1] and len(result["chunks"]) == 1
    assert audit["scheduler"]["unsubmitted_units"] == 4
    assert audit["scheduler"]["submission_stopped"] and audit["scheduler"]["stopped_early"]
    assert audit["unit_failures"][0]["code"] == code
    assert list((tmp_path/"attempts").glob("*-attempt-*.json"))
    monkeypatch.setattr(pg, "SubmissionControl", bs.SubmissionControl)
    provider.generate.side_effect = lambda text, *_: packet_response(e, packet_index(text))
    provider.generate.reset_mock()
    resumed = {}
    assert len(generate(e, mapped, plan, provider, resumed, tmp_path/"attempts")["chunks"]) == 6
    assert resumed["units_resumed"] == 1 and provider.generate.call_count == 5


def test_unexpected_worker_error_is_drained_accounted_and_propagated(tmp_path, monkeypatch):
    e, mapped, plan = source(tmp_path)
    control = bs.SubmissionControl()
    monkeypatch.setattr(pg, "SubmissionControl", lambda: control)
    started = Event()
    def call(text, *_):
        index = packet_index(text)
        if index == 0:
            started.set()
            assert control.stopped.wait(3)
            return packet_response(e, 0)
        assert started.wait(3)
        raise RuntimeError("SECRET unexpected worker body")
    provider = Mock()
    provider.generate.side_effect = call
    audit = {}
    with pytest.raises(RuntimeError, match="unexpected worker body"):
        generate(e, mapped, plan, provider, audit, tmp_path/"attempts")
    assert audit["requests_started"] == 2
    assert len(audit["scheduler_errors"]) == 1
    assert audit["scheduler"]["submitted_units"] == 2
    assert "SECRET" not in json.dumps(audit["scheduler_errors"])
    assert not audit["observability"]["candidate_accounting_complete"]
    monkeypatch.setattr(pg, "SubmissionControl", bs.SubmissionControl)
    provider.generate.side_effect = lambda text, *_: packet_response(e, packet_index(text))
    provider.generate.reset_mock()
    resumed = {}
    generate(e, mapped, plan, provider, resumed, tmp_path/"attempts")
    assert resumed["units_resumed"] == 1 and provider.generate.call_count == 5


def test_multiple_worker_errors_are_all_propagated(tmp_path):
    e, mapped, plan = source(tmp_path, 2)
    barrier = Barrier(2)
    def call(*_):
        barrier.wait(3)
        raise RuntimeError("private")
    provider = Mock()
    provider.generate.side_effect = call
    audit = {}
    with pytest.raises(ExceptionGroup) as caught:
        generate(e, mapped, plan, provider, audit)
    assert len(caught.value.exceptions) == len(audit["scheduler_errors"]) == 2


def test_service_stop_does_not_start_an_uncommitted_correction_or_permanent_hold(tmp_path, monkeypatch):
    e, mapped, plan = source(tmp_path, 2)
    control = bs.SubmissionControl()
    monkeypatch.setattr(pg, "SubmissionControl", lambda: control)
    rejected = Event()
    original = pg.eligible_chunk_numbers
    def allowed(report, statuses):
        result = original(report, statuses)
        if not result:
            rejected.set()
            assert control.stopped.wait(3)
        return result
    monkeypatch.setattr(pg, "eligible_chunk_numbers", allowed)
    def call(text, *_):
        index = packet_index(text)
        if index == 1:
            assert rejected.wait(3)
            raise ChunkGenerationError("private", code="QUOTA_EXHAUSTED")
        payload = json.loads(packet_response(e, 0))
        payload["chunks"][0]["original_text"] += " There were 999999999 participants."
        return json.dumps(payload)
    provider = Mock()
    provider.generate.side_effect = call
    audit = {}
    with pytest.raises(ChunkGenerationError) as caught:
        generate(e, mapped, plan, provider, audit, tmp_path/"attempts")
    assert caught.value.code == "QUOTA_EXHAUSTED"
    assert provider.generate.call_count == 2
    assert audit["observability"]["request_totals"]["correction_calls"] == 0
    assert audit["observability"]["counts"]["corrections_deferred_after_stop"] == 1
    assert not list((tmp_path/"attempts").glob("*validation-hold-v1.json"))


def test_callback_interruption_drains_workers_and_preserves_restart(tmp_path):
    e, mapped, plan = source(tmp_path)
    release = Event()
    def call(text, *_):
        index = packet_index(text)
        if index == 0:
            assert release.wait(3)
        return packet_response(e, index)
    def progress(stage, detail):
        if detail["source_packages_completed"]:
            release.set()
            raise KeyboardInterrupt("synthetic cancellation")
    provider = Mock()
    provider.generate.side_effect = call
    audit = {}
    with pytest.raises(KeyboardInterrupt):
        generate(e, mapped, plan, provider, audit, tmp_path/"attempts", progress_callback=progress)
    assert provider.generate.call_count == 2
    assert audit["scheduler"]["submitted_units"] == 2
    provider.generate.side_effect = lambda text, *_: packet_response(e, packet_index(text))
    provider.generate.reset_mock()
    resumed = {}
    generate(e, mapped, plan, provider, resumed, tmp_path/"attempts")
    assert resumed["units_resumed"] == 2 and provider.generate.call_count == 4


def test_cancelled_future_is_not_silently_discarded(tmp_path, monkeypatch):
    e, mapped, plan = source(tmp_path)
    class Executor:
        def __init__(self, **kwargs):
            self.pool = ThreadPoolExecutor(**kwargs)
        def submit(self, fn, index):
            if index == 0:
                future = Future()
                future.cancel()
                future.set_running_or_notify_cancel()
                return future
            return self.pool.submit(fn, index)
        def shutdown(self, **kwargs):
            self.pool.shutdown(**kwargs)
    monkeypatch.setattr(bs, "ThreadPoolExecutor", Executor)
    provider = Mock()
    provider.generate.side_effect = lambda text, *_: packet_response(e, packet_index(text))
    audit = {}
    with pytest.raises(CancelledError):
        generate(e, mapped, plan, provider, audit, tmp_path/"attempts")
    assert audit["scheduler"]["cancelled_units"] == 1
    assert audit["scheduler"]["submitted_units"] == 2
    assert provider.generate.call_count == 1


@pytest.mark.parametrize("change", ["duplicate_unit_id", "schedule", "pdf", "ownership"])
def test_mismatched_or_duplicate_source_identity_stops_before_provider(tmp_path, change):
    e, mapped, plan = source(tmp_path)
    if change == "duplicate_unit_id":
        plan.source_package_plan.packages[1].unit_id = plan.source_package_plan.packages[0].unit_id
    elif change == "schedule":
        plan.batches[0].unit_id = "different"
    elif change == "pdf":
        e["evidence"]["pdf_sha256"] = "0"*64
    else:
        plan.batches[0].ownership = []
    provider = Mock(side_effect=AssertionError("Source mismatch must not call provider"))
    with pytest.raises((ValueError, ChunkGenerationError)):
        generate(e, mapped, plan, provider, {})
    provider.generate.assert_not_called()


@pytest.mark.parametrize("damage", ["json", "fingerprint", "schema", "source_file"])
def test_invalid_candidate_checkpoint_is_revalidated_or_regenerated(tmp_path, damage):
    e, mapped, plan = source(tmp_path, 2)
    provider = Mock()
    provider.generate.side_effect = lambda text, *_: packet_response(e, packet_index(text))
    directory = tmp_path/"attempts"
    expected = generate(e, mapped, plan, provider, {}, directory)
    canonical = sorted(p for p in directory.glob("*.json") if len(p.stem) == 64)
    assert len(canonical) == 2
    target = canonical[0]
    saved = json.loads(target.read_text())
    if damage == "json":
        target.write_text("not JSON")
    else:
        if damage == "fingerprint":
            saved["fingerprint"] = "wrong"
        elif damage == "schema":
            saved["candidate"]["chunks"][0]["original_text"] = None
        else:
            saved["candidate"]["article"]["source_file"] = "different.pdf"
        target.write_text(json.dumps(saved))
    provider.generate.reset_mock()
    audit = {}
    assert generate(e, mapped, plan, provider, audit, directory) == expected
    assert audit["units_resumed"] == 1 and provider.generate.call_count == 1


def test_repeated_candidates_keep_ordered_deduplication_and_provenance(tmp_path, monkeypatch):
    from test_source_packages import fixture, response
    e, mapped, plan = fixture(tmp_path, pages=4)
    provider = Mock()
    provider.generate.return_value = response(e)
    rolling = pg._run_source_schedule
    before = {}
    monkeypatch.setattr(pg, "_run_source_schedule", fixed_groups)
    expected = generate(e, mapped, plan, provider, before)
    monkeypatch.setattr(pg, "_run_source_schedule", rolling)
    after = {}
    assert generate(e, mapped, plan, provider, after) == expected
    assert after["chunk_ownership"] == before["chunk_ownership"]
    assert after["observability"]["counts"]["duplicate_candidates"] == 3
