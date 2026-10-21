"""Minimal sanitized Azure OpenAI connectivity diagnostic."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
from dataclasses import dataclass
from http.client import HTTPSConnection, HTTPException
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_TOKENS = 16
MAX_LIVE_PROBE_OUTPUT_TOKENS = 64
DEFAULT_TOKEN_LIMIT_PARAMETER = "max_completion_tokens"
TOKEN_LIMIT_PARAMETERS = {"max_completion_tokens", "max_tokens"}


@dataclass(frozen=True)
class AzureConfig:
    provider: str
    api_key: str
    endpoint: str
    deployment: str
    api_version: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration shape without making the live inference request.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Network timeout in seconds for the single request.",
    )
    parser.add_argument(
        "--max-output-tokens",
        "--max-tokens",
        dest="max_output_tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help=(
            "Completion token cap for the single opt-in live probe. "
            "Values are clamped to 1-64."
        ),
    )
    parser.add_argument(
        "--token-limit-parameter",
        choices=sorted(TOKEN_LIMIT_PARAMETERS),
        default=DEFAULT_TOKEN_LIMIT_PARAMETER,
        help=(
            "Azure chat-completions token-limit parameter for the live probe. "
            "Use max_tokens only for deployments or API versions that require it."
        ),
    )
    args = parser.parse_args()

    _load_env()
    config = _read_config()
    result = _build_configuration_report(config)

    if not result["configuration"]["ok"]:
        result["live_request_attempted"] = False
        _print(result)
        return 1

    if args.dry_run:
        result["ok"] = True
        result["status"] = "configuration_ok"
        result["live_request_attempted"] = False
        _print(result)
        return 0

    result.update(
        _run_probe(
            config,
            timeout=args.timeout,
            max_output_tokens=args.max_output_tokens,
            token_limit_parameter=args.token_limit_parameter,
        )
    )
    result["live_request_attempted"] = True
    _print(result)
    return 0 if result.get("ok") else 1


def _load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if load_dotenv is not None:
        load_dotenv(env_path)
        return
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def _read_config() -> AzureConfig:
    return AzureConfig(
        provider=os.getenv("GASTROAGI_LLM_PROVIDER", "").strip().lower(),
        api_key=os.getenv("AZURE_OPENAI_API_KEY", "").strip(),
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", "").strip(),
        deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip(),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "").strip(),
    )


def _build_configuration_report(config: AzureConfig) -> dict[str, Any]:
    endpoint = _endpoint_report(config.endpoint)
    issues: list[str] = []
    warnings: list[str] = []

    if config.provider != "azure":
        issues.append("GASTROAGI_LLM_PROVIDER must be set to `azure`.")
    if not config.api_key:
        issues.append("AZURE_OPENAI_API_KEY is not configured.")
    if not config.endpoint:
        issues.append("AZURE_OPENAI_ENDPOINT is not configured.")
    if not config.deployment:
        issues.append("AZURE_OPENAI_DEPLOYMENT is not configured.")
    if not config.api_version:
        issues.append("AZURE_OPENAI_API_VERSION is not configured.")

    if endpoint["configured"] and not endpoint["valid_url"]:
        issues.append("AZURE_OPENAI_ENDPOINT must be an absolute HTTPS URL.")
    if endpoint["valid_url"] and endpoint["scheme"] != "https":
        issues.append("AZURE_OPENAI_ENDPOINT must use https.")
    if endpoint["service_type"] == "azure_ai_foundry_models":
        issues.append(
            "Endpoint looks like an Azure AI Foundry Models endpoint, not a classic Azure OpenAI resource endpoint."
        )
    if endpoint["service_type"] == "unknown" and endpoint["valid_url"]:
        warnings.append(
            "Endpoint host does not match the expected Azure OpenAI shape `<resource>.openai.azure.com`."
        )
    if config.deployment.lower() in {"gpt-5.4", "gpt-5", "gpt-4.1", "gpt-4o"}:
        warnings.append(
            "Deployment value looks like a model name. Azure requires the deployment name shown on the resource Deployments page."
        )

    return {
        "ok": False,
        "status": "configuration_error" if issues else "not_run",
        "configuration": {
            "ok": not issues,
            "provider": config.provider or None,
            "api_key_configured": bool(config.api_key),
            "endpoint": endpoint,
            "deployment_configured": bool(config.deployment),
            "deployment": _sanitize_label(config.deployment),
            "api_version": _sanitize_label(config.api_version),
            "issues": issues,
            "warnings": warnings,
        },
    }


def _endpoint_report(endpoint: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "configured": bool(endpoint),
        "valid_url": False,
        "scheme": None,
        "masked_host": None,
        "path": None,
        "service_type": None,
    }
    if not endpoint:
        return report

    parsed = urlparse(endpoint)
    report["scheme"] = parsed.scheme or None
    report["masked_host"] = _mask_host(parsed.hostname or "")
    report["path"] = parsed.path or "/"
    report["valid_url"] = bool(parsed.scheme and parsed.netloc)

    host = (parsed.hostname or "").lower()
    if host.endswith(".openai.azure.com"):
        report["service_type"] = "azure_openai"
    elif host.endswith(".services.ai.azure.com") or host.endswith(".inference.ai.azure.com"):
        report["service_type"] = "azure_ai_foundry_models"
    else:
        report["service_type"] = "unknown"
    return report


def _run_probe(
    config: AzureConfig,
    timeout: float,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    token_limit_parameter: str = DEFAULT_TOKEN_LIMIT_PARAMETER,
) -> dict[str, Any]:
    parsed = urlparse(config.endpoint)
    base_path = parsed.path.rstrip("/")
    request_path = (
        f"{base_path}/openai/deployments/{quote(config.deployment, safe='')}"
        f"/chat/completions?api-version={quote(config.api_version, safe='')}"
    )
    token_cap = _bounded_max_output_tokens(max_output_tokens)
    token_parameter = _token_limit_parameter(token_limit_parameter)
    body = {
        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        token_parameter: token_cap,
    }
    headers = {
        "Content-Type": "application/json",
        "api-key": config.api_key,
    }

    try:
        connection = HTTPSConnection(parsed.hostname, parsed.port, timeout=timeout)
        connection.request("POST", request_path, body=json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read()
    except socket.gaierror as exc:
        return _with_probe_details(_network_error("dns_error", exc), token_cap, token_parameter)
    except (TimeoutError, socket.timeout) as exc:
        return _with_probe_details(_network_error("timeout", exc), token_cap, token_parameter)
    except ssl.SSLError as exc:
        return _with_probe_details(_network_error("tls_error", exc), token_cap, token_parameter)
    except (OSError, HTTPException) as exc:
        return _with_probe_details(_network_error("network_error", exc), token_cap, token_parameter)
    finally:
        try:
            connection.close()  # type: ignore[name-defined]
        except Exception:
            pass

    payload = _decode_payload(raw)
    return _with_probe_details(
        _classify_response(response.status, response.headers, payload),
        token_cap,
        token_parameter,
    )



def _bounded_max_output_tokens(value: int) -> int:
    try:
        token_count = int(value)
    except (TypeError, ValueError):
        token_count = DEFAULT_MAX_OUTPUT_TOKENS
    return max(1, min(token_count, MAX_LIVE_PROBE_OUTPUT_TOKENS))


def _token_limit_parameter(value: str) -> str:
    return value if value in TOKEN_LIMIT_PARAMETERS else DEFAULT_TOKEN_LIMIT_PARAMETER


def _with_probe_details(
    result: dict[str, Any],
    token_cap: int,
    token_limit_parameter: str,
) -> dict[str, Any]:
    result["probe"] = {
        "prompt": "non_clinical_connectivity_check",
        "max_output_tokens": token_cap,
        "token_limit_parameter": token_limit_parameter,
    }
    return result

def _classify_response(http_status: int, headers: Any, payload: dict[str, Any] | None) -> dict[str, Any]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = {}
    request_id = (
        headers.get("apim-request-id")
        or headers.get("x-ms-request-id")
        or headers.get("x-request-id")
        or headers.get("openai-request-id")
    )
    base = {
        "http_status": http_status,
        "request_id": _sanitize_label(request_id),
        "error_code": _sanitize_label(error.get("code")),
        "error_type": _sanitize_label(error.get("type")),
        "message": _diagnostic_message(http_status, error),
    }

    if 200 <= http_status < 300:
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        content = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = str(message.get("content") or "")
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        return {
            **base,
            "ok": bool(content.strip()),
            "status": "success" if content.strip() else "empty_response",
            "response_id": _sanitize_label(payload.get("id") if isinstance(payload, dict) else None),
            "model": _sanitize_label(payload.get("model") if isinstance(payload, dict) else None),
            "output_text_length": len(content),
            "usage": _safe_usage(usage),
        }

    category = {
        400: "invalid_request_or_api_compatibility",
        401: "authentication_or_endpoint_mismatch",
        403: "permission_or_subscription_denied",
        404: "deployment_or_endpoint_not_found",
        408: "timeout",
        409: "conflict_retryable",
        422: "invalid_request_or_api_compatibility",
        429: "quota_or_rate_limit",
        500: "server_error_retryable",
        502: "server_error_retryable",
        503: "server_error_retryable",
        504: "server_error_retryable",
    }.get(http_status, "http_error")
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    return {
        **base,
        "ok": False,
        "status": category,
        "retry_after": _sanitize_label(retry_after),
        "retryable": http_status in {408, 409, 429, 500, 502, 503, 504},
    }


def _diagnostic_message(http_status: int, error: dict[str, Any]) -> str:
    code = str(error.get("code") or "").strip()
    if http_status in {401, 403}:
        return (
            "Azure rejected authentication or authorization. Verify the key comes from the same resource as the endpoint, "
            "the subscription/resource is active, and the endpoint is the Azure OpenAI resource endpoint."
        )
    if http_status == 404:
        return "Deployment or route was not found. Verify AZURE_OPENAI_DEPLOYMENT is the deployment name, not just the model name."
    if http_status in {400, 422}:
        return "Azure rejected the request shape or API version. Verify the API version and whether this resource uses Azure OpenAI or Foundry Models."
    if http_status == 429:
        return "Quota or rate limit was reached. Check quota, TPM/RPM limits, and Retry-After."
    if 200 <= http_status < 300:
        return "Inference request succeeded."
    if code:
        return f"Azure returned HTTP {http_status} with error code `{_sanitize_label(code)}`."
    return f"Azure returned HTTP {http_status}."


def _network_error(status: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "live_request_attempted": True,
        "http_status": None,
        "exception_class": exc.__class__.__name__,
        "message": "Network-level failure before an HTTP response was received.",
    }


def _decode_payload(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    safe: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            safe[key] = value
    return safe


def _sanitize_label(value: Any, max_length: int = 160) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return "".join(ch for ch in text if ch.isalnum() or ch in "._:-/")[:max_length]


def _mask_host(host: str) -> str | None:
    if not host:
        return None
    parts = host.split(".")
    if len(parts) >= 3:
        return "<redacted>." + ".".join(parts[1:])
    return "<redacted-host>"


def _print(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())



