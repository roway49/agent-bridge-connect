"""Approval deadlines shared by Core and native transports (never grants)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_APPROVAL_MAX_TIME, get_executor_config
from .session import SESSION_STATE_FILE, control_root_for_task, read_json


def approval_timing(
    *, board_root: Path, config: dict[str, Any], task_id: str,
    executor: str, executor_run_id: str, session_id: str, request_id: str,
    now: datetime,
) -> tuple[str, float]:
    """Reuse only the exact native request's frozen deadline, else freeze Core's.

    Hermes v3 persists directly in Core; broker-backed requests have already
    started their clock before the worker observes them. Neither notification
    delivery nor opening a details view may restart that clock.
    """
    seconds = float(get_executor_config(config, executor).get("approval_timeout_s", DEFAULT_APPROVAL_MAX_TIME))
    state = read_json(control_root_for_task(task_id, board_root=board_root) / SESSION_STATE_FILE) or {}
    pending = state.get("pending_request") or {}
    identity = {"task_id": task_id, "executor": executor, "executor_run_id": executor_run_id, "session_id": session_id}
    if all(state.get(key) == value for key, value in identity.items()) and pending.get("request_id") == request_id:
        expires_at = pending.get("expires_at")
        if isinstance(expires_at, (int, float)) and math.isfinite(expires_at):
            deadline = datetime.fromtimestamp(expires_at, timezone.utc)
            seconds = float(pending.get("approval_max_time_s", seconds))
            return deadline.isoformat().replace("+00:00", "Z"), seconds
    return (now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"), seconds


def waiting_input_candidates(list_tasks: Any, board_root: Path) -> list[Any]:
    """Include native SDK timeouts that reached failed before maintenance.

    A terminal candidate must have the same closed run lease. This selects
    timeout reconciliation only; it never revives tasks or authorizes tools.
    """
    from .run_lease import RunLeaseState, load_lease

    candidates = list_tasks(status="input_required")
    for task in list_tasks(status="failed"):
        request = (task.extensions or {}).get("agentbc.input")
        if not isinstance(request, dict) or not (
            task.assignee == "claude"
            and request.get("status") == "waiting"
            and request.get("type") == "permission"
            and request.get("native_live_elevation") is True
            and request.get("approval_version") == 3
            and any(error.get("code") in {
                "approval_request_expired", "claude_sdk_approval_timeout"
            } for error in (task.errors or []))
        ):
            continue
        lease = load_lease(task.id, board_root)
        if (lease is not None and lease.state == RunLeaseState.CLOSED
                and lease.run_id == request.get("executor_run_id")):
            candidates.append(task)
    return candidates


def permission_denial_source(response_type: str, message: str) -> str:
    """Preserve notification provenance only for a denial, never a grant."""
    if response_type != "deny":
        return "user"
    return {"agentbc_permission_dialog_timeout": "timeout",
            "agentbc_permission_dialog_closed": "dialog_closed"}.get(message, "user")
