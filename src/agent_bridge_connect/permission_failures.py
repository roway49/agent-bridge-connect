"""Stable, public failure reasons for permission waits.

The task error ``permission_resume_session_unavailable`` is intentionally kept
as the compatibility envelope.  This module supplies the bounded, structured
reason carried in that envelope; it never accepts or stores executor output,
argv, prompts, session ids, or private control-root data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PERMISSION_WAIT_COMPATIBILITY_CODE = "permission_resume_session_unavailable"

PERMISSION_MODE_UNSUPPORTED = "permission_mode_unsupported"
PERMISSION_INPUT_INVALID = "permission_input_invalid"
PERMISSION_REQUESTED_SCOPE_INVALID = "permission_requested_scope_invalid"
PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID = "permission_blocked_step_cardinality_invalid"
PERMISSION_CHAIN_HEAD_STALE = "permission_chain_head_stale"
PERMISSION_CHAIN_HEAD_AMBIGUOUS = "permission_chain_head_ambiguous"
PERMISSION_RUN_LEASE_INVALID = "permission_run_lease_invalid"
PERMISSION_RUN_LEASE_RUN_MISMATCH = "permission_run_lease_run_mismatch"
PERMISSION_SESSION_RECEIPT_MISSING = "permission_session_receipt_missing"
PERMISSION_SESSION_RECEIPT_INVALID = "permission_session_receipt_invalid"
PERMISSION_EXECUTOR_SESSION_MISMATCH = "permission_executor_session_mismatch"
PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH = "permission_executor_session_run_mismatch"
PERMISSION_SESSION_STATE_STALE = "permission_session_state_stale"
PERMISSION_SESSION_SNAPSHOT_INVALID = "permission_session_snapshot_invalid"
PERMISSION_RESUME_SESSION_MISSING = "permission_resume_session_missing"

PERMISSION_WAIT_REASON_CODES = frozenset(
    {
        PERMISSION_MODE_UNSUPPORTED,
        PERMISSION_INPUT_INVALID,
        PERMISSION_REQUESTED_SCOPE_INVALID,
        PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
        PERMISSION_CHAIN_HEAD_STALE,
        PERMISSION_CHAIN_HEAD_AMBIGUOUS,
        PERMISSION_RUN_LEASE_INVALID,
        PERMISSION_RUN_LEASE_RUN_MISMATCH,
        PERMISSION_SESSION_RECEIPT_MISSING,
        PERMISSION_SESSION_RECEIPT_INVALID,
        PERMISSION_EXECUTOR_SESSION_MISMATCH,
        PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
        PERMISSION_SESSION_STATE_STALE,
        PERMISSION_SESSION_SNAPSHOT_INVALID,
        PERMISSION_RESUME_SESSION_MISSING,
    }
)

_DIAGNOSTICS = {
    PERMISSION_MODE_UNSUPPORTED: "effective permission mode is not safe",
    PERMISSION_INPUT_INVALID: "permission input is malformed",
    PERMISSION_REQUESTED_SCOPE_INVALID: "permission requested scope is not full",
    PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID: (
        "permission wait must identify exactly one blocked step"
    ),
    PERMISSION_CHAIN_HEAD_STALE: "permission wait task is not the current chain head",
    PERMISSION_CHAIN_HEAD_AMBIGUOUS: "permission wait chain head is not uniquely provable",
    PERMISSION_RUN_LEASE_INVALID: "permission wait RunLease is missing or invalid",
    PERMISSION_RUN_LEASE_RUN_MISMATCH: "permission wait RunLease does not match the executor run",
    PERMISSION_SESSION_RECEIPT_MISSING: "official executor session receipt is missing",
    PERMISSION_SESSION_RECEIPT_INVALID: "official executor session receipt is malformed",
    PERMISSION_EXECUTOR_SESSION_MISMATCH: "executor and authoritative session do not match",
    PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH: "executor session does not match the executor run",
    PERMISSION_SESSION_STATE_STALE: "authoritative executor session state is stale",
    PERMISSION_SESSION_SNAPSHOT_INVALID: "authoritative executor session snapshot is invalid",
    PERMISSION_RESUME_SESSION_MISSING: "authoritative resume session is missing",
}

_KNOWN_EXECUTORS = frozenset({"codex", "claude", "hermes"})
_KNOWN_MODES = frozenset({"inherit", "safe", "full"})
_KNOWN_LEASE_STATES = frozenset(
    {"active", "suspended", "stale", "orphaned", "closing", "closed", "missing", "invalid"}
)
_KNOWN_SESSION_STATES = frozenset(
    {"pending", "active", "input_required", "needs_recovery", "terminal"}
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_MAX_DIAGNOSTIC_VALUE_LENGTH = 48


@dataclass(frozen=True)
class PermissionWaitFailure:
    """A stable reason and only safe observations for a failed permission wait."""

    reason_code: str
    observed: tuple[tuple[str, Any], ...] = ()

    @property
    def diagnostic(self) -> str:
        return _DIAGNOSTICS[self.reason_code]

    def to_details(
        self,
        *,
        executor_run_id: str = "",
        blocked_step_id: int | None = None,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "input_type": "permission",
            "reason_code": self.reason_code,
            "compatibility_code": PERMISSION_WAIT_COMPATIBILITY_CODE,
            "diagnostic": self.diagnostic,
        }
        for key, value in self.observed:
            safe = _safe_observed_value(key, value)
            if safe is not None:
                details[key] = safe
        details["executor_run_id"] = _safe_opaque_identifier(executor_run_id)
        if isinstance(blocked_step_id, int) and not isinstance(blocked_step_id, bool):
            details["blocked_step_id"] = blocked_step_id
        return details


def permission_wait_failure(
    reason_code: str,
    **observed: Any,
) -> PermissionWaitFailure:
    """Build one of the enumerated permission wait failures."""

    if reason_code not in PERMISSION_WAIT_REASON_CODES:
        raise ValueError(f"unknown permission wait reason code: {reason_code}")
    return PermissionWaitFailure(
        reason_code=reason_code,
        observed=tuple(sorted(observed.items())),
    )


def _safe_observed_value(key: str, value: Any) -> Any:
    if key == "executor":
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _KNOWN_EXECUTORS else "invalid"
    if key in {"effective_mode", "requested_mode"}:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _KNOWN_MODES else "invalid"
    if key == "lease_state":
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _KNOWN_LEASE_STATES else "invalid"
    if key == "session_state":
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _KNOWN_SESSION_STATES else "invalid"
    if key in {"receipt_state", "chain_state", "field"}:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return "missing"
        if len(normalized) > _MAX_DIAGNOSTIC_VALUE_LENGTH:
            return "invalid"
        if not re.fullmatch(r"[a-z0-9_.-]+", normalized):
            return "invalid"
        return normalized
    if key in {"blocked_step_count", "head_count"}:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(0, min(value, 1000))
    if key in {"session_id_present", "run_id_present", "run_match"}:
        return bool(value)
    return None


def _safe_opaque_identifier(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if re.search(r"(?i)(secret|token|password|prompt|argv|executor-log|sk-)", normalized):
        return "[redacted]"
    if _SAFE_IDENTIFIER_RE.fullmatch(normalized):
        return normalized
    return "[redacted]"


__all__ = [
    "PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID",
    "PERMISSION_CHAIN_HEAD_AMBIGUOUS",
    "PERMISSION_CHAIN_HEAD_STALE",
    "PERMISSION_EXECUTOR_SESSION_MISMATCH",
    "PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH",
    "PERMISSION_INPUT_INVALID",
    "PERMISSION_MODE_UNSUPPORTED",
    "PERMISSION_REQUESTED_SCOPE_INVALID",
    "PERMISSION_RESUME_SESSION_MISSING",
    "PERMISSION_RUN_LEASE_INVALID",
    "PERMISSION_RUN_LEASE_RUN_MISMATCH",
    "PERMISSION_SESSION_RECEIPT_INVALID",
    "PERMISSION_SESSION_RECEIPT_MISSING",
    "PERMISSION_SESSION_SNAPSHOT_INVALID",
    "PERMISSION_SESSION_STATE_STALE",
    "PERMISSION_WAIT_COMPATIBILITY_CODE",
    "PERMISSION_WAIT_REASON_CODES",
    "PermissionWaitFailure",
    "permission_wait_failure",
]
