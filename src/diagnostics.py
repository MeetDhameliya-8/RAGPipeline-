"""Content-free, run-scoped observations. Never used for processing decisions."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
import inspect
from threading import RLock
import time
from typing import Any, Callable, Iterator, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field

P = ParamSpec("P")
R = TypeVar("R")


class StageTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: int = 0
    failures: int = 0
    inclusive_seconds: float = 0.0
    exclusive_seconds: float = 0.0
    samples_seconds: list[float] = Field(default_factory=list)


class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: int
    kind: str
    status: str = "started"
    seconds: float = 0.0
    http_attempts: int | None = None
    http_attempts_complete: bool = False
    http_successes: int = 0
    http_failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_observations: int = 0
    unknown_usage_attempts: int = 0
    http_statuses: list[int | None] = Field(default_factory=list)
    provider_error_codes: list[str] = Field(default_factory=list)


class DiagnosticSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = "milestone1-v1"
    run_complete: bool = False
    wall_seconds: float = 0.0
    timing_semantics: str = "exclusive per execution context; concurrent work seconds are not wall time"
    stages: dict[str, StageTiming] = Field(default_factory=dict)
    model_calls: list[ModelCall] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    request_totals: dict[str, int | bool] = Field(default_factory=dict)
    candidate_accounting_complete: bool = True


@dataclass
class _Frame:
    started: float
    children: float = 0.0


_current: ContextVar[Observation | None] = ContextVar("gastroagi_observation", default=None)
_stack: ContextVar[tuple[_Frame, ...]] = ContextVar("gastroagi_timing_stack", default=())
_request: ContextVar[int | None] = ContextVar("gastroagi_request", default=None)
_correction: ContextVar[bool] = ContextVar("gastroagi_correction", default=False)

# Only repository-defined codes cross the content-free diagnostic boundary.
# Unknown provider codes/messages can contain secrets or medical passages.
_CODES = frozenset({
    "GENERATION_FAILED", "INVALID_JSON", "SCHEMA_INVALID", "REFERENCE_OR_CONTENT_INVALID",
    "SOURCE_VALIDATION_FAILED", "SOURCE_PASSAGE_HELD", "EXTRACTION_BLOCKED",
    "REFERENCE_IDENTITY_UNRESOLVED", "REQUEST_BUDGET_EXCEEDED", "CONFIGURATION_ERROR",
    "AUTHENTICATION_FAILED", "QUOTA_EXHAUSTED", "TRANSPORT_TIMEOUT", "TRANSPORT_ERROR",
    "EMPTY_RESPONSE", "MALFORMED_RESPONSE", "OUTPUT_TRUNCATED", "MODEL_REFUSAL",
    "CONTENT_FILTERED", "UNEXPECTED_COMPLETION", "EXTRACTION_FAILED", "VALIDATION_FAILED",
    "VISUAL_SOURCE_UNRESOLVED", "SOURCE_FIDELITY", "SOURCE_GROUNDING", "WORD_COUNT",
    "SECTION_QUALITY", "NUMERIC_RELATION", "SCHEMA", "UNCLASSIFIED_ERROR",
})
_REASONS = frozenset({"JSON_NOT_STRING", "JSON_EMPTY", "JSON_PARSE_ERROR", "JSON_NOT_OBJECT",
    "JSON_DUPLICATE_KEYS", "RAW_SCHEMA_INVALID", "CHUNK_COUNT_MISMATCH", "DUPLICATE_CHUNK_NUMBER",
    "ARTICLE_NOT_OBJECT", "CHUNKS_NOT_LIST", "NO_CLINICAL_CHUNKS", "CHUNK_NOT_OBJECT",
    "EXTRA_CONTENT_FIELD", "REQUIRED_FIELD_EMPTY", "REFERENCE_BINDING_UNAVAILABLE",
    "REFERENCE_IDENTITY_CONFLICT", "REFERENCE_DOI_NOT_IN_SOURCE", "REFERENCE_DOI_CONFLICT",
    "REFERENCE_DOI_UNBOUND", "REFERENCE_DOI_AMBIGUOUS", "REFERENCE_BIBLIOGRAPHY_UNBOUND",
    "REFERENCE_CITATION_UNVERIFIED", "REFERENCE_CITATION_COMPONENT_MISMATCH", "REFERENCE_NOT_TRACEABLE"})
_PROVIDER_CODES = frozenset({"rate_limit_exceeded", "internal_error", "server_error", "service_unavailable",
    "authentication_error", "invalid_api_key", "permission_denied", "insufficient_quota", "quota_exceeded",
    "billing_hard_limit_reached", "model_not_found", "deployment_not_found", "deploymentnotfound",
    "bad_request", "invalid_request", "invalid_request_error", "context_length_exceeded"})


def safe_code(value: Any) -> str:
    return value if isinstance(value, str) and value in _CODES else "UNCLASSIFIED_ERROR"


class Observation:
    """Lock-protected accumulator; source inputs/exception messages are never accepted."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.perf_counter
        self._started = self.clock()
        self._finished: float | None = None
        self.data = DiagnosticSnapshot()
        self._lock = RLock()
        self._targets: list[dict[str, Any]] = []

    def attach(self, target: dict[str, Any]) -> None:
        with self._lock:
            if not any(t is target for t in self._targets):
                self._targets.append(target)
            target["observability"] = self.snapshot()

    def publish(self) -> None:
        with self._lock:
            snap = self.snapshot()
            for target in self._targets:
                target["observability"] = deepcopy(snap)

    def count(self, name: str, amount: int = 1, *, replace: bool = False) -> None:
        with self._lock:
            self.data.counts[name] = amount if replace else self.data.counts.get(name, 0) + amount

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            calls = self.data.model_calls
            self.data.wall_seconds = max(0.0, (self._finished if self._finished is not None else self.clock()) - self._started)
            counts = self.data.counts
            observed = counts.get("generated_candidates", 0) + counts.get("resumed_candidates", 0)
            assessed = counts.get("assessed_candidate_events", 0)
            prevalidation_rejected = counts.get("schema_rejected_candidates", 0) + counts.get("reference_or_content_rejected_candidates", 0)
            disposed = assessed + prevalidation_rejected + counts.get("unassessed_duplicate_candidates", 0)
            counts["unclassified_candidate_events"] = max(0, observed - disposed)
            self.data.request_totals = {
                "logical_calls": len(calls),
                "successful_responses": sum(c.status == "succeeded" for c in calls),
                "failed_calls": sum(c.status == "failed" for c in calls),
                "correction_calls": sum(c.kind == "correction" for c in calls),
                "generation_calls": sum(c.kind in {"generation", "correction"} for c in calls),
                "transcription_calls": sum(c.kind in {"visual_transcription", "citation_transcription"} for c in calls),
                "citation_transcription_calls": sum(c.kind == "citation_transcription" for c in calls),
                "http_attempts_observed": sum(c.http_attempts or 0 for c in calls),
                "http_attempts_complete": all(c.http_attempts_complete for c in calls),
                "calls_with_unknown_http_attempts": sum(not c.http_attempts_complete for c in calls),
                "http_successes": sum(c.http_successes for c in calls),
                "http_failures": sum(c.http_failures for c in calls),
                "prompt_tokens_observed": sum(c.prompt_tokens for c in calls),
                "completion_tokens_observed": sum(c.completion_tokens for c in calls),
                "unknown_usage_attempts": sum(c.unknown_usage_attempts for c in calls),
                "token_usage_complete": all(c.http_attempts_complete and c.usage_observations == c.http_attempts
                    and c.usage_observations > 0
                    and c.unknown_usage_attempts == 0 for c in calls),
            }
            result = self.data.model_dump(mode="json")
            result["candidate_accounting_complete"] = (self.data.candidate_accounting_complete
                and observed == disposed)
            return result


def current() -> Observation | None:
    return _current.get()


@contextmanager
def bind(observation: Observation | None) -> Iterator[None]:
    """Explicit worker binding; never inherit the submitting thread's timing frames."""
    token = _current.set(observation)
    stack = _stack.set(())
    request = _request.set(None)
    try:
        yield
    finally:
        _request.reset(request)
        _stack.reset(stack)
        _current.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    observation = current()
    if observation is None:
        yield
        return
    parents = _stack.get()
    frame = _Frame(observation.clock())
    token = _stack.set(parents + (frame,))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        elapsed = max(0.0, observation.clock() - frame.started)
        _stack.reset(token)
        if parents:
            parents[-1].children += elapsed
        with observation._lock:
            metric = observation.data.stages.setdefault(name, StageTiming())
            metric.calls += 1
            metric.failures += int(failed)
            metric.inclusive_seconds += elapsed
            metric.exclusive_seconds += max(0.0, elapsed - frame.children)
            metric.samples_seconds.append(elapsed)


def timed(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with stage(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def observed_run(fn: Callable[P, R]) -> Callable[P, R]:
    signature = inspect.signature(fn)

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        observation = current() or Observation()
        owner = current() is None
        token = _current.set(observation)
        arguments = signature.bind(*args, **kwargs).arguments
        diagnostics = arguments.get("diagnostics")
        if isinstance(diagnostics, dict):
            observation.attach(diagnostics)
        captured_exception = None
        try:
            result = fn(*args, **kwargs)
            # Only orchestration results, never the five-field generated document.
            if isinstance(result, dict) and "status" in result:
                observation.attach(result)
            return result
        except Exception as exc:
            captured_exception = exc
            failure(exc, "UNCLASSIFIED_ERROR")
            if isinstance(getattr(exc, "diagnostics", None), dict):
                observation.attach(exc.diagnostics)
            raise
        finally:
            if owner:
                observation._finished = observation.clock()
                observation.data.run_complete = True
                observation.publish()
            if captured_exception is not None:
                captured_exception.observability = observation.snapshot()
            _current.reset(token)
    return wrapped


def attach(target: dict[str, Any]) -> None:
    if (observation := current()) is not None:
        observation.attach(target)


def count(name: str, amount: int = 1, *, replace: bool = False) -> None:
    if (observation := current()) is not None:
        observation.count(name, amount, replace=replace)


def incomplete_candidates() -> None:
    if (observation := current()) is not None:
        with observation._lock:
            observation.data.candidate_accounting_complete = False


def failure(exc: Exception, category: str) -> dict[str, str]:
    root = safe_code(getattr(exc, "code", "UNCLASSIFIED_ERROR"))
    detail = {"original_code": root, "category": safe_code(category)}
    if isinstance(getattr(exc, "diagnostics", None), dict):
        previous = exc.diagnostics.get("failure_classification")
        if isinstance(previous, dict):
            detail = {"original_code": safe_code(previous.get("original_code")), "category": safe_code(category)}
        else:
            exc.diagnostics["failure_classification"] = detail
        reason = exc.diagnostics.get("reason_code")
        if isinstance(reason, str) and reason in _REASONS:
            detail["reason_code"] = reason
    kind = type(exc).__name__
    if kind in {"ChunkGenerationError", "PdfExtractionError", "OSError", "FileNotFoundError",
                "PermissionError", "TimeoutError", "ConnectionError", "OperationalError",
                "RuntimeError", "ValueError", "ValidationError", "JSONDecodeError"}:
        detail["exception_type"] = kind
    errno = getattr(exc, "errno", None)
    if type(errno) is int:
        detail["errno"] = str(errno)
    if (observation := current()) is not None:
        with observation._lock:
            # Construct afresh; never trust arbitrary nested exception diagnostics.
            observation.data.failures.append(dict(detail))
    return detail


@contextmanager
def correcting() -> Iterator[None]:
    token = _correction.set(True)
    try:
        with stage("correction"):
            yield
    finally:
        _correction.reset(token)


def _usage(call: ModelCall, usage: Any) -> None:
    def value(name: str) -> Any:
        return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    prompt, completion = value("prompt_tokens"), value("completion_tokens")
    prompt_known = type(prompt) is int and prompt >= 0
    completion_known = type(completion) is int and completion >= 0
    if prompt_known:
        call.prompt_tokens += prompt
    if completion_known:
        call.completion_tokens += completion
    if prompt_known and completion_known:
        call.usage_observations += 1
    else:
        call.unknown_usage_attempts += 1


def model_call(kind: str, fn: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    observation = current()
    if observation is None:
        return fn(*args, **kwargs)
    with observation._lock:
        call = ModelCall(call_id=len(observation.data.model_calls) + 1,
            kind="correction" if _correction.get() else kind)
        observation.data.model_calls.append(call)
    token = _request.set(call.call_id - 1)
    started = observation.clock()
    try:
        with stage("transcription_request" if kind == "visual_transcription" else "llm_request"):
            result = fn(*args, **kwargs)
        with observation._lock:
            call.status = "succeeded"
            if call.http_attempts is None:
                metadata = getattr(result, "metadata", {})
                _usage(call, metadata.get("usage") if isinstance(metadata, dict) else None)
        return result
    except Exception as exc:
        with observation._lock:
            call.status = "failed"
            if call.http_attempts is None and call.unknown_usage_attempts == 0:
                call.unknown_usage_attempts += 1
        failure(exc, getattr(exc, "code", "TRANSPORT_ERROR"))
        raise
    finally:
        with observation._lock:
            call.seconds = max(0.0, observation.clock() - started)
        _request.reset(token)


def http_attempt(*, started: bool = False, succeeded: bool = False, usage: Any = None,
                 http_status: Any = None, provider_code: Any = None, uncertain: bool = False) -> None:
    observation, index = current(), _request.get()
    if observation is None or index is None:
        return
    with observation._lock:
        call = observation.data.model_calls[index]
        if uncertain:
            call.http_attempts_complete = False
            call.unknown_usage_attempts += 1
        elif started:
            call.http_attempts = (call.http_attempts or 0) + 1
            call.http_attempts_complete = True
        else:
            call.http_successes += int(succeeded)
            call.http_failures += int(not succeeded)
            call.http_statuses.append(http_status if type(http_status) is int and 100 <= http_status <= 599 else None)
            if isinstance(provider_code, str) and provider_code.lower() in _PROVIDER_CODES:
                call.provider_error_codes.append(provider_code.lower())
            _usage(call, usage)


def transport_metadata(value: Any) -> dict[str, Any]:
    """Whitelist only numeric/controlled transport metadata from arbitrary providers."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    attempts = value.get("transport_attempts")
    if type(attempts) is int and attempts >= 0:
        result["transport_attempts"] = attempts
    usage = value.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {key: val if type(val) is int and val >= 0 else None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            for val in [usage.get(key)]}
    finish = value.get("finish_reason")
    if isinstance(finish, str) and finish in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
        result["finish_reason"] = finish
    return result
