from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import ABCError


EXECUTION_POLICY_VERSION = 1
RESOURCE_EXTENSION_KEY = "agentbc.resources"
SESSION_EXTENSION_KEY = "agentbc.session"
RESOURCE_MULTIPLIER = 2

RESOURCE_KIND_BY_EXECUTOR = {
    "claude": "max_budget_usd",
    "hermes": "max_turns",
}
PROJECT_MODES = frozenset({"native", "ephemeral", "none"})
SESSION_STATES = frozenset(
    {"pending", "active", "input_required", "needs_recovery", "terminal"}
)
CLEANUP_STATES = frozenset(
    {"not_requested", "retained", "pending", "succeeded", "unsupported", "failed"}
)
RESOURCE_DECISIONS = frozenset({"", "increase", "terminate"})
TERMINAL_SESSION_CLEANUP_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "rejected"}
)

_HERMES_SESSION_RECEIPT_RE = re.compile(
    r"(?m)^[ \t]*session_id:[ \t]*([^\s]+)[ \t]*$"
)


def build_resource_snapshot(
    executor: str,
    limit: int | float,
    *,
    source: str = "config",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the durable per-task resource-policy snapshot."""
    normalized_executor = str(executor or "").strip().lower()
    resource = RESOURCE_KIND_BY_EXECUTOR.get(normalized_executor, "")
    normalized_limit = _normalize_resource_limit(normalized_executor, limit)
    snapshot: dict[str, Any] = {
        "version": EXECUTION_POLICY_VERSION,
        "executor": normalized_executor,
        "resource": resource,
        "configured_limit": normalized_limit,
        "current_limit": normalized_limit,
        "multiplier": RESOURCE_MULTIPLIER,
        "exhaustion_count": 0,
        "last_decision": "",
        "source": str(source or "").strip(),
        "created_at": created_at or _utc_now(),
    }
    _raise_policy_errors(validate_resource_snapshot(snapshot), RESOURCE_EXTENSION_KEY)
    return snapshot


def validate_resource_snapshot(
    value: Any,
    *,
    executor: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{RESOURCE_EXTENSION_KEY} must be an object"]
    if value.get("version") != EXECUTION_POLICY_VERSION:
        errors.append(
            f"{RESOURCE_EXTENSION_KEY}.version must be {EXECUTION_POLICY_VERSION}"
        )
    actual_executor = str(value.get("executor") or "").strip().lower()
    if actual_executor not in RESOURCE_KIND_BY_EXECUTOR:
        errors.append(f"{RESOURCE_EXTENSION_KEY}.executor is unsupported: {actual_executor}")
    if executor is not None and actual_executor != str(executor).strip().lower():
        errors.append(f"{RESOURCE_EXTENSION_KEY}.executor does not match {executor}")
    expected_resource = RESOURCE_KIND_BY_EXECUTOR.get(actual_executor)
    if value.get("resource") != expected_resource:
        errors.append(
            f"{RESOURCE_EXTENSION_KEY}.resource must be {expected_resource or 'executor-specific'}"
        )
    for field in ("configured_limit", "current_limit"):
        if not _valid_resource_limit(actual_executor, value.get(field)):
            errors.append(f"{RESOURCE_EXTENSION_KEY}.{field} is invalid")
    if value.get("multiplier") != RESOURCE_MULTIPLIER:
        errors.append(
            f"{RESOURCE_EXTENSION_KEY}.multiplier must be {RESOURCE_MULTIPLIER}"
        )
    exhaustion_count = value.get("exhaustion_count")
    if (
        isinstance(exhaustion_count, bool)
        or not isinstance(exhaustion_count, int)
        or exhaustion_count < 0
    ):
        errors.append(f"{RESOURCE_EXTENSION_KEY}.exhaustion_count must be a non-negative integer")
    if str(value.get("last_decision") or "") not in RESOURCE_DECISIONS:
        errors.append(f"{RESOURCE_EXTENSION_KEY}.last_decision is invalid")
    for field in ("source", "created_at"):
        if not isinstance(value.get(field), str) or not str(value.get(field)).strip():
            errors.append(f"{RESOURCE_EXTENSION_KEY}.{field} must be non-empty")
    return errors


def build_session_snapshot(
    executor: str,
    *,
    retain: bool,
    session_id: str = "",
    project_mode: str | None = None,
    project_path: str = "",
    session_state: str = "pending",
    run_ids: list[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the durable executor-session snapshot without touching Executor storage."""
    normalized_executor = str(executor or "").strip().lower()
    if project_mode is None:
        normalized_mode = "native" if normalized_executor == "claude" and retain else (
            "ephemeral" if normalized_executor == "claude" else "none"
        )
    else:
        normalized_mode = str(project_mode).strip().lower()
    snapshot: dict[str, Any] = {
        "version": EXECUTION_POLICY_VERSION,
        "executor": normalized_executor,
        "retain": retain,
        "session_id": str(session_id or "").strip(),
        "session_state": str(session_state or "").strip().lower(),
        "project_mode": normalized_mode,
        "project_path": str(project_path or "").strip(),
        "run_ids": [str(item).strip() for item in (run_ids or []) if str(item).strip()],
        "resume_count": 0,
        "cleanup": {"state": "not_requested", "attempts": 0},
        "created_at": created_at or _utc_now(),
    }
    _raise_policy_errors(validate_session_snapshot(snapshot), SESSION_EXTENSION_KEY)
    return snapshot


def validate_session_snapshot(
    value: Any,
    *,
    executor: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{SESSION_EXTENSION_KEY} must be an object"]
    if value.get("version") != EXECUTION_POLICY_VERSION:
        errors.append(
            f"{SESSION_EXTENSION_KEY}.version must be {EXECUTION_POLICY_VERSION}"
        )
    actual_executor = str(value.get("executor") or "").strip().lower()
    if actual_executor not in {"claude", "hermes", "codex"}:
        errors.append(f"{SESSION_EXTENSION_KEY}.executor is unsupported: {actual_executor}")
    if executor is not None and actual_executor != str(executor).strip().lower():
        errors.append(f"{SESSION_EXTENSION_KEY}.executor does not match {executor}")
    if not isinstance(value.get("retain"), bool):
        errors.append(f"{SESSION_EXTENSION_KEY}.retain must be a boolean")
    session_state = str(value.get("session_state") or "").strip().lower()
    if session_state not in SESSION_STATES:
        errors.append(f"{SESSION_EXTENSION_KEY}.session_state is invalid")
    session_id = value.get("session_id")
    if not isinstance(session_id, str):
        errors.append(f"{SESSION_EXTENSION_KEY}.session_id must be a string")
    elif session_state != "pending" and not session_id.strip():
        errors.append(
            f"{SESSION_EXTENSION_KEY}.session_id is required after the pending state"
        )
    project_mode = str(value.get("project_mode") or "").strip().lower()
    if project_mode not in PROJECT_MODES:
        errors.append(f"{SESSION_EXTENSION_KEY}.project_mode is invalid")
    project_path = value.get("project_path")
    if not isinstance(project_path, str):
        errors.append(f"{SESSION_EXTENSION_KEY}.project_path must be a string")
        project_path = ""
    if actual_executor == "claude":
        expected_mode = "native" if value.get("retain") is True else "ephemeral"
        if project_mode != expected_mode:
            errors.append(
                f"{SESSION_EXTENSION_KEY}.project_mode must be {expected_mode} for Claude"
            )
        if not project_path or not Path(project_path).expanduser().is_absolute():
            errors.append(f"{SESSION_EXTENSION_KEY}.project_path must be absolute for Claude")
    elif project_mode != "none" or project_path:
        errors.append(
            f"{SESSION_EXTENSION_KEY} project fields are only supported for Claude"
        )
    run_ids = value.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in run_ids)
        or len(run_ids) != len(set(run_ids))
    ):
        errors.append(f"{SESSION_EXTENSION_KEY}.run_ids must contain unique non-empty strings")
    resume_count = value.get("resume_count")
    if (
        isinstance(resume_count, bool)
        or not isinstance(resume_count, int)
        or resume_count < 0
    ):
        errors.append(f"{SESSION_EXTENSION_KEY}.resume_count must be a non-negative integer")
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict):
        errors.append(f"{SESSION_EXTENSION_KEY}.cleanup must be an object")
    else:
        if str(cleanup.get("state") or "") not in CLEANUP_STATES:
            errors.append(f"{SESSION_EXTENSION_KEY}.cleanup.state is invalid")
        attempts = cleanup.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            errors.append(
                f"{SESSION_EXTENSION_KEY}.cleanup.attempts must be a non-negative integer"
            )
    if not isinstance(value.get("created_at"), str) or not str(value.get("created_at")).strip():
        errors.append(f"{SESSION_EXTENSION_KEY}.created_at must be non-empty")
    return errors


def validate_execution_policy_extensions(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["extensions must be an object"]
    errors: list[str] = []
    if RESOURCE_EXTENSION_KEY in value:
        errors.extend(validate_resource_snapshot(value[RESOURCE_EXTENSION_KEY]))
    if SESSION_EXTENSION_KEY in value:
        errors.extend(validate_session_snapshot(value[SESSION_EXTENSION_KEY]))
    return errors


def attach_execution_policy(
    extensions: dict[str, Any] | None,
    *,
    resources: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy with validated policy snapshots attached under canonical keys."""
    updated = dict(extensions or {})
    if resources is not None:
        _raise_policy_errors(validate_resource_snapshot(resources), RESOURCE_EXTENSION_KEY)
        updated[RESOURCE_EXTENSION_KEY] = dict(resources)
    if session is not None:
        _raise_policy_errors(validate_session_snapshot(session), SESSION_EXTENSION_KEY)
        updated[SESSION_EXTENSION_KEY] = dict(session)
    return updated


def extract_hermes_session_id(stderr: str) -> str | None:
    """Extract only Hermes' official single-query session receipt."""
    matches = _HERMES_SESSION_RECEIPT_RE.findall(str(stderr or ""))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def session_cleanup_blockers(
    *,
    task_status: str,
    lease_state: str,
    report_written: bool,
    notification_recorded: bool,
    session: Any,
) -> list[str]:
    """Return the ordered reasons post-terminal session cleanup must not run."""
    blockers: list[str] = []
    if str(task_status or "").strip().lower() not in TERMINAL_SESSION_CLEANUP_STATUSES:
        blockers.append("task_not_terminal")
    if str(lease_state or "").strip().lower() != "closed":
        blockers.append("run_lease_not_closed")
    if report_written is not True:
        blockers.append("report_not_written")
    if notification_recorded is not True:
        blockers.append("notification_not_recorded")
    session_errors = validate_session_snapshot(session)
    if session_errors:
        blockers.append("session_receipt_invalid")
        return blockers
    if session.get("retain") is True:
        blockers.append("retention_enabled")
    if str(session.get("session_state") or "") != "terminal":
        blockers.append("session_not_terminal")
    if not str(session.get("session_id") or "").strip():
        blockers.append("session_id_missing")
    cleanup = session.get("cleanup") or {}
    if str(cleanup.get("state") or "") in {"retained", "succeeded", "unsupported"}:
        blockers.append("cleanup_already_resolved")
    return blockers


def is_session_cleanup_eligible(**kwargs: Any) -> bool:
    return not session_cleanup_blockers(**kwargs)


def _normalize_resource_limit(executor: str, value: int | float) -> int | float:
    if not _valid_resource_limit(executor, value):
        raise ABCError(
            "invalid_execution_resource_limit",
            f"Invalid {RESOURCE_KIND_BY_EXECUTOR.get(executor) or 'resource'} limit for {executor}",
            {"executor": executor, "value": value},
        )
    if executor == "hermes":
        return int(value)
    return float(value)


def _valid_resource_limit(executor: str, value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if executor == "hermes":
        return isinstance(value, int) and value > 0
    if executor == "claude":
        return isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0
    return False


def _raise_policy_errors(errors: list[str], key: str) -> None:
    if errors:
        raise ABCError(
            "invalid_execution_policy",
            f"Invalid {key}: {'; '.join(errors)}",
            {"extension": key, "errors": errors},
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
