"""Operational recovery decisions; never relax clinical validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RETRY_DELAYS = (30, 120, 600, 1800)
RETRY_CODES = {"TRANSPORT_TIMEOUT", "EMPTY_RESPONSE", "INVALID_JSON", "MALFORMED_RESPONSE",
               "UNEXPECTED_COMPLETION", "SCHEMA_INVALID", "OUTPUT_TRUNCATED"}
ACTION_CODES = {"AUTHENTICATION_FAILED", "QUOTA_EXHAUSTED", "CONFIGURATION_ERROR"}
SOURCE_HOLD_CODES = {"SOURCE_PASSAGE_HELD", "SOURCE_VALIDATION_FAILED", "EXTRACTION_BLOCKED",
                     "REFERENCE_IDENTITY_UNRESOLVED", "CONTENT_FILTERED", "MODEL_REFUSAL"}


@dataclass(frozen=True)
class RecoveryDecision:
    code: str
    retry: bool
    message: str


def recovery_decision(result: dict[str, Any]) -> RecoveryDecision | None:
    """Consume explicit provider outcomes, including partial package failures."""
    signals = list(result.get("recovery_signals") or [])
    database = result.get("local_database") or {}
    if database.get("status")=="LOCAL_REVIEW_DB_STORED" and database.get("readback_verified") is False:
        signals.append({"code":"STORAGE_READBACK_INCOMPLETE", "retryable":True})
    if result.get("status") == "failed":
        signals.insert(0, {"code": result.get("failure_code", ""),
                           "retryable": result.get("retryable")})
    for signal in signals:
        code = str(signal.get("code") or "")
        if code in ACTION_CODES:
            return RecoveryDecision(code, False,
                "Azure credentials or quota need attention. Completed drafts are preserved; fix the service and retry here.")
    for signal in signals:
        code = str(signal.get("code") or "")
        if code in SOURCE_HOLD_CODES:
            continue  # A stray retryable hint cannot override a source/review hold.
        if signal.get("retryable") is True or code in RETRY_CODES:
            return RecoveryDecision(code or "TEMPORARY_SERVICE_ERROR", True,
                "Temporary service interruption. Completed drafts are preserved; remaining work will retry automatically.")
    return None
