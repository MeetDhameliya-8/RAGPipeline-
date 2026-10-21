"""Azure OpenAI provider adapter for GastroAGI chunk generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
from hashlib import sha256
import json
import os
import re
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from .base import ChunkGenerationError, GenerationResponse
from .. import diagnostics as telemetry


AZURE_REQUEST_TIMEOUT_SECONDS = 120.0
AZURE_MAX_RETRIES = 2
AZURE_RETRY_BASE_DELAY_SECONDS = 1.0
AZURE_MAX_RETRY_DELAY_SECONDS = 60.0
AZURE_CONNECTIVITY_MAX_OUTPUT_TOKENS = 16
AZURE_CONNECTIVITY_TOKEN_PARAMETERS = {"max_completion_tokens", "max_tokens"}
AZURE_CONNECTIVITY_DEFAULT_TOKEN_PARAMETER = "max_completion_tokens"

logger = logging.getLogger("src.chunk_generator")


@dataclass(frozen=True)
class AzureChunkGenerator:
    """Azure OpenAI-backed chunk generator using environment-provided credentials."""

    deployment: str
    api_key: str
    endpoint: str
    api_version: str
    client_factory: Callable[..., Any] | None = None
    request_timeout_seconds: float = AZURE_REQUEST_TIMEOUT_SECONDS
    max_retries: int = AZURE_MAX_RETRIES
    sleep: Callable[[float], None] = time.sleep
    connectivity_max_output_tokens: int = AZURE_CONNECTIVITY_MAX_OUTPUT_TOKENS
    connectivity_token_parameter: str = AZURE_CONNECTIVITY_DEFAULT_TOKEN_PARAMETER

    @classmethod
    def from_environment(cls) -> "AzureChunkGenerator":
        api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
        token_parameter = os.getenv(
            "AZURE_OPENAI_CONNECTIVITY_TOKEN_LIMIT_PARAMETER",
            AZURE_CONNECTIVITY_DEFAULT_TOKEN_PARAMETER,
        ).strip()
        missing = []
        if not api_key:
            missing.append("AZURE_OPENAI_API_KEY")
        if not endpoint:
            missing.append("AZURE_OPENAI_ENDPOINT")
        if not deployment:
            missing.append("AZURE_OPENAI_DEPLOYMENT")
        if not api_version:
            missing.append("AZURE_OPENAI_API_VERSION")
        if missing:
            raise ChunkGenerationError(
                "Azure OpenAI configuration is incomplete: " + ", ".join(missing) + ".", code="CONFIGURATION_ERROR"
            )
        endpoint_issues = validate_azure_endpoint(endpoint)
        if endpoint_issues:
            raise ChunkGenerationError(
                "Azure OpenAI configuration is invalid: " + "; ".join(endpoint_issues), code="CONFIGURATION_ERROR"
            )
        return cls(
            deployment=deployment,
            api_key=api_key,
            endpoint=endpoint,
            api_version=api_version,
            connectivity_token_parameter=_token_limit_parameter(token_parameter),
        )

    def generate(self, article_text: str, source_file: str, prompt: str) -> str:
        client = self._client(max_retries=0)
        user_content = (
            f"Source file: {source_file}\n\n"
            "Article text begins below.\n\n"
            f"{article_text}"
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]

        audit = {"configuration_fingerprint": sha256(json.dumps({
            "provider": "azure", "deployment": self.deployment, "endpoint": self.endpoint,
            "api_version": self.api_version, "prompt_hash": sha256(prompt.encode()).hexdigest(),
            "input_hash": sha256(article_text.encode()).hexdigest(),
        }, sort_keys=True).encode()).hexdigest(), "transport_attempts": 0}
        def count_attempt(attempt: int) -> None:
            audit["transport_attempts"] = attempt
        try:
            response = self._create_chat_completion(client=client, messages=messages, attempt_callback=count_attempt)
        except Exception as exc:  # pragma: no cover - network/API dependent
            error = _provider_error_payload("azure", exc, self.deployment)
            audit.update(outcome="transport_error", error={
                name: error.get(name) for name in ("http_status", "retryable", "request_id")})
            code = ("AUTHENTICATION_FAILED" if _is_authentication_error(exc) else
                    "QUOTA_EXHAUSTED" if _is_quota_error(exc) else
                    "TRANSPORT_TIMEOUT" if _is_timeout_error(exc) else "TRANSPORT_ERROR")
            raise ChunkGenerationError(
                f"LLM generation failed: {_safe_llm_error_message(exc, provider='azure')}", code=code, diagnostics=audit
            ) from exc

        choices = getattr(response, "choices", None)
        finish = getattr(choices[0], "finish_reason", None) if isinstance(choices, list) and choices else None
        usage = getattr(response, "usage", None)
        audit.update(finish_reason=finish, response_id=_sanitize_diagnostic_value(getattr(response, "id", None)),
                     usage={name: _get_usage_value(usage, name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")})
        try:
            output_text = _extract_chat_message_content(response)
        except ChunkGenerationError as exc:
            exc.diagnostics.update(audit, outcome=exc.code)
            raise
        self._log_token_usage(response)
        if not output_text.strip():
            raise ChunkGenerationError("LLM returned an empty response.", code="EMPTY_RESPONSE", diagnostics=audit)
        audit["outcome"] = "completed" if finish == "stop" else "finish_reason_unreported"
        return GenerationResponse(output_text, audit)


    def stream_generate(self, article_text: str, source_file: str, prompt: str) -> Iterator[str]:
        """Yield Azure OpenAI chat-completion deltas as they arrive."""
        client = self._client(max_retries=0)
        user_content = (
            f"Source file: {source_file}\n\n"
            "Article text begins below.\n\n"
            f"{article_text}"
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            stream = self._create_chat_completion(
                client=client,
                messages=messages,
                extra_params={"stream": True},
            )
        except Exception as exc:  # pragma: no cover - network/API dependent
            raise ChunkGenerationError(
                f"LLM streaming failed: {_safe_llm_error_message(exc, provider='azure')}"
            ) from exc

        yielded = False
        try:
            for chunk in stream:
                delta = _extract_chat_stream_delta(chunk)
                if delta:
                    yielded = True
                    yield delta
        except Exception as exc:  # pragma: no cover - network/API dependent
            raise ChunkGenerationError(
                f"LLM streaming interrupted: {_safe_llm_error_message(exc, provider='azure')}"
            ) from exc
        if not yielded:
            raise ChunkGenerationError("LLM returned an empty streamed response.")

    def transcribe_image(self, png: bytes, prompt: str) -> str:
        """Use the configured Azure route only; unsupported image inputs fail closed."""
        import base64
        messages = [{"role":"system","content":prompt},{"role":"user","content":[
            {"type":"text","text":"Transcribe this source page; do not answer from medical knowledge."},
            {"type":"image_url","image_url":{"url":"data:image/png;base64,"+base64.b64encode(png).decode("ascii"),"detail":"high"}}]}]
        try:
            response=self._create_chat_completion(client=self._client(max_retries=0),messages=messages)
        except Exception as exc:
            error = _provider_error_payload("azure", exc, self.deployment)
            code = ("AUTHENTICATION_FAILED" if _is_authentication_error(exc) else
                    "QUOTA_EXHAUSTED" if _is_quota_error(exc) else
                    "TRANSPORT_TIMEOUT" if _is_timeout_error(exc) else "TRANSPORT_ERROR")
            raise ChunkGenerationError("Azure visual transcription failed: "+_safe_llm_error_message(exc,provider="azure"),
                code=code,diagnostics={"error":{"retryable":error.get("retryable"),"http_status":error.get("http_status")}}) from exc
        self._log_token_usage(response)
        return _extract_chat_message_content(response)
    def check_connectivity(self) -> dict[str, Any]:
        """Run a minimal non-clinical Azure OpenAI connectivity check."""
        client = self._client(max_retries=0)
        token_parameter = _token_limit_parameter(self.connectivity_token_parameter)
        token_cap = _bounded_connectivity_tokens(self.connectivity_max_output_tokens)
        try:
            response = self._create_chat_completion(
                client=client,
                messages=[{"role": "user", "content": "Reply OK."}],
                max_retries=0,
                extra_params={token_parameter: token_cap},
            )
            output_text = _extract_chat_message_content(response)
            self._log_token_usage(response)
            response_id = _sanitize_diagnostic_value(getattr(response, "id", ""))
        except ChunkGenerationError as exc:
            return {
                "provider": "azure",
                "model": self.deployment,
                "endpoint": self.endpoint,
                "api_version": self.api_version,
                "ok": False,
                "status": "malformed_response",
                "message": str(exc),
                "probe": _probe_details(token_cap, token_parameter),
            }
        except Exception as exc:  # pragma: no cover - network/API dependent
            result = _provider_error_payload("azure", exc, self.deployment)
            result["probe"] = _probe_details(token_cap, token_parameter)
            return result

        return {
            "provider": "azure",
            "model": self.deployment,
            "endpoint": self.endpoint,
            "api_version": self.api_version,
            "ok": bool(output_text.strip()),
            "status": "success" if output_text.strip() else "empty_response",
            "response_id": response_id,
            "output_text_length": len(output_text),
            "probe": _probe_details(token_cap, token_parameter),
        }

    def _client(self, max_retries: int) -> Any:
        if self.client_factory is not None:
            return self.client_factory(
                azure_endpoint=self.endpoint,
                api_key=self.api_key,
                api_version=self.api_version,
                timeout=self.request_timeout_seconds,
                max_retries=max_retries,
            )
        try:
            from openai import AzureOpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ChunkGenerationError(
                "The openai package is required for Azure OpenAI generation."
            ) from exc
        return AzureOpenAI(
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
            api_version=self.api_version,
            timeout=self.request_timeout_seconds,
            max_retries=max_retries,
        )

    def _create_chat_completion(
        self,
        client: Any,
        messages: list[dict[str, str]],
        max_retries: int | None = None,
        extra_params: dict[str, Any] | None = None,
        attempt_callback: Callable[[int], None] | None = None,
    ) -> Any:
        retries = self.max_retries if max_retries is None else max_retries
        attempts = max(0, retries) + 1
        request_params = dict(extra_params or {})
        for attempt_index in range(attempts):
            if attempt_callback is not None:
                attempt_callback(attempt_index + 1)
            try:
                with telemetry.stage("http_attempt"):
                    response = client.chat.completions.create(
                        model=self.deployment,
                        messages=messages,
                        **request_params,
                    )
                telemetry.http_attempt(started=True)
                telemetry.http_attempt(succeeded=True, usage=getattr(response, "usage", None))
                return response
            except Exception as exc:
                error = _extract_api_error(exc)
                status = getattr(exc, "status_code", None)
                transport_failure = (type(status) is int and 100 <= status <= 599) or _is_timeout_error(exc) or "connection" in type(exc).__name__.lower()
                if transport_failure:
                    telemetry.http_attempt(started=True)
                    telemetry.http_attempt(http_status=status, provider_code=error.get("code"))
                else:
                    # SDK validation/client failures do not prove an HTTP send.
                    telemetry.http_attempt(uncertain=True)
                final_attempt = attempt_index >= attempts - 1
                if final_attempt or not _is_retryable_llm_error(exc):
                    raise
                delay = _retry_delay_seconds(exc, attempt_index)
                logger.warning(
                    "Azure OpenAI request retrying attempt=%s/%s delay_seconds=%.2f",
                    attempt_index + 1,
                    attempts,
                    delay,
                )
                if delay > 0:
                    with telemetry.stage("retry_backoff"):
                        self.sleep(delay)
        raise RuntimeError("Azure OpenAI retry loop exited unexpectedly.")

    def _log_token_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = _get_usage_value(usage, "prompt_tokens", "input_tokens")
        completion_tokens = _get_usage_value(usage, "completion_tokens", "output_tokens")
        total_tokens = _get_usage_value(usage, "total_tokens")
        logger.info(
            "Azure OpenAI token usage prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )


def validate_azure_endpoint(endpoint: str) -> list[str]:
    """Return sanitized Azure OpenAI endpoint configuration issues."""
    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return ["AZURE_OPENAI_ENDPOINT must be an absolute https URL."]
    if parsed.scheme.lower() != "https":
        return ["AZURE_OPENAI_ENDPOINT must use https."]

    host = (parsed.hostname or "").lower()
    if host.endswith(".services.ai.azure.com") or host.endswith(".inference.ai.azure.com"):
        return [
            "AZURE_OPENAI_ENDPOINT appears to be an Azure AI Foundry Models endpoint; "
            "configure an Azure OpenAI resource endpoint ending in `.openai.azure.com`."
        ]
    if not host.endswith(".openai.azure.com"):
        return [
            "AZURE_OPENAI_ENDPOINT must be an Azure OpenAI resource endpoint ending in `.openai.azure.com`."
        ]
    return []



def _extract_chat_stream_delta(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None)
    if not isinstance(choices, list) or not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None)
    return content if isinstance(content, str) else ""
def _extract_chat_message_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ChunkGenerationError("LLM returned a malformed response: missing choices.", code="MALFORMED_RESPONSE")
    message = getattr(choices[0], "message", None)
    finish = getattr(choices[0], "finish_reason", None)
    if getattr(message, "refusal", None):
        raise ChunkGenerationError("LLM refused generation.", code="MODEL_REFUSAL")
    if finish == "length":
        raise ChunkGenerationError("LLM output was incomplete or truncated.", code="OUTPUT_TRUNCATED")
    if finish == "content_filter":
        raise ChunkGenerationError("LLM output was content filtered.", code="CONTENT_FILTERED")
    if finish is None:
        raise ChunkGenerationError("LLM returned a malformed response: missing finish reason.", code="MALFORMED_RESPONSE")
    if finish != "stop":
        raise ChunkGenerationError("LLM returned an unexpected completion outcome.", code="UNEXPECTED_COMPLETION")
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        raise ChunkGenerationError(
            "LLM returned a malformed response: missing choices[0].message.content.", code="MALFORMED_RESPONSE"
        )
    return content


def _get_usage_value(usage: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return None


def _is_retryable_llm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if _is_authentication_error(exc) or _is_quota_error(exc):
        return False
    if _is_deployment_not_found_error(exc) or _is_invalid_request_error(exc):
        return False
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    class_name = exc.__class__.__name__.lower()
    return _is_timeout_error(exc) or "connection" in class_name


def _is_authentication_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    code = _error_code(exc)
    return status_code in {401, 403} or code in {
        "authentication_error",
        "invalid_api_key",
        "permission_denied",
    }


def _is_quota_error(exc: Exception) -> bool:
    code = _error_code(exc)
    return code in {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"}


def _is_deployment_not_found_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    code = _error_code(exc)
    return status_code == 404 or code in {
        "model_not_found",
        "deployment_not_found",
        "deploymentnotfound",
    }


def _is_invalid_request_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    code = _error_code(exc)
    return status_code in {400, 422} or code in {
        "bad_request",
        "invalid_request",
        "invalid_request_error",
        "context_length_exceeded",
    }


def _error_code(exc: Exception) -> str:
    error = _extract_api_error(exc)
    code = error.get("code") or getattr(exc, "code", "")
    return str(code or "").strip().lower()


def _retry_delay_seconds(exc: Exception, attempt_index: int) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    delay = AZURE_RETRY_BASE_DELAY_SECONDS * (2 ** attempt_index)
    return min(delay, AZURE_MAX_RETRY_DELAY_SECONDS)


def _retry_after_seconds(exc: Exception) -> float | None:
    raw = _extract_api_error(exc).get("retry_after")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, AZURE_MAX_RETRY_DELAY_SECONDS))


def _with_request_id(message: str, error: dict[str, Any]) -> str:
    request_id = error.get("request_id")
    if request_id:
        return f"{message} request_id={request_id}."
    return message


def _safe_llm_error_message(exc: Exception, provider: str = "openai") -> str:
    """Return an API failure message without credentials or raw provider payloads."""
    status_code = getattr(exc, "status_code", None)
    error = _extract_api_error(exc)
    provider_label = "Azure OpenAI" if provider == "azure" else "OpenAI"

    if _is_authentication_error(exc):
        return _with_request_id(
            f"{provider_label} rejected the API credentials or permissions.", error
        )
    if _is_quota_error(exc):
        return _with_request_id(f"{provider_label} quota is exhausted.", error)
    if status_code == 429:
        return _with_request_id(f"{provider_label} rate limit was reached (429).", error)
    if _is_deployment_not_found_error(exc):
        return _with_request_id(
            f"{provider_label} deployment or model was not found.", error
        )
    if _is_invalid_request_error(exc):
        return _with_request_id(f"{provider_label} rejected the request as invalid.", error)
    if _is_timeout_error(exc):
        return _with_request_id(f"{provider_label} API request timed out.", error)
    if status_code and int(status_code) >= 500:
        return _with_request_id(
            f"{provider_label} API request failed with transient status {status_code}.",
            error,
        )
    if status_code:
        return _with_request_id(
            f"{provider_label} API request failed with status {status_code}.", error
        )
    return exc.__class__.__name__


def _extract_api_error(exc: Exception) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    headers = dict(getattr(response, "headers", {}) or {}) if response is not None else {}
    error: dict[str, Any] = {}
    try:
        body = response.json() if response is not None else None
        if isinstance(body, dict):
            body_error = body.get("error")
            if isinstance(body_error, dict):
                error.update(body_error)
            else:
                error.update(body)
    except Exception:
        pass

    error["request_id"] = _sanitize_diagnostic_value(
        getattr(exc, "request_id", None)
        or headers.get("X-Request-ID")
        or headers.get("x-request-id")
        or headers.get("openai-request-id")
    )
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    error["retry_after"] = _sanitize_diagnostic_value(retry_after, max_length=64)
    return error


def _provider_error_payload(provider: str, exc: Exception, model: str) -> dict[str, Any]:
    error = _extract_api_error(exc)
    status_code = getattr(exc, "status_code", None)
    safe_message = _safe_llm_error_message(exc, provider=provider)
    return {
        "provider": provider,
        "model": model,
        "ok": False,
        "status": "error",
        "exception_class": exc.__class__.__name__,
        "http_status": status_code,
        "error_type": error.get("type"),
        "error_code": error.get("code") or getattr(exc, "code", None),
        "message": safe_message,
        "request_id": error.get("request_id"),
        "retry_after": error.get("retry_after"),
        "retryable": _is_retryable_llm_error(exc),
    }


def _sanitize_diagnostic_value(value: Any, max_length: int = 160) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    sanitized = re.sub(r"[^A-Za-z0-9._:\-]", "", text)
    return sanitized[:max_length] or None


def _is_timeout_error(exc: Exception) -> bool:
    return "timeout" in exc.__class__.__name__.lower()


def _bounded_connectivity_tokens(value: int) -> int:
    try:
        token_count = int(value)
    except (TypeError, ValueError):
        token_count = AZURE_CONNECTIVITY_MAX_OUTPUT_TOKENS
    return max(1, min(token_count, AZURE_CONNECTIVITY_MAX_OUTPUT_TOKENS))


def _token_limit_parameter(value: str) -> str:
    return value if value in AZURE_CONNECTIVITY_TOKEN_PARAMETERS else AZURE_CONNECTIVITY_DEFAULT_TOKEN_PARAMETER


def _probe_details(token_cap: int, token_parameter: str) -> dict[str, Any]:
    return {
        "prompt": "non_clinical_connectivity_check",
        "max_output_tokens": token_cap,
        "token_limit_parameter": token_parameter,
    }

