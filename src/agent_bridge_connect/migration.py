"""Strict legacy-permission cutover and maintenance-mode migration.

This narrow module owns the 1.0.3A cutover gate.  A supported update/preflight
returns ``legacy_permission_cutover_blocked`` while any old-channel task is
``pending`` / ``running`` / ``input_required`` / ``needs_recovery``, an
unconsumed one-shot grant exists, or a legacy permission marker is present.
Manual bypass installs drop the board into a maintenance mode that only permits
``doctor`` / ``status`` / ``report`` / explicit termination.

Terminal historical tasks and the old ``agentbc.permission`` record stay
read-only, and the legacy top-level ``permission_mode`` is double-read and only
normalized on the first actual setting modification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    permission_grant_from_extensions,
)
from .permission_modes import CANONICAL_PERMISSION_MODES, LEGACY_PERMISSION_MODE
from .protocol import ABCError
from .terminal_states import TASK_TERMINAL_STATES

LEGACY_CUTOVER_BLOCKED = "legacy_permission_cutover_blocked"
LEGACY_ACTIVE_STATUSES = frozenset(
    {"pending", "running", "input_required", "needs_recovery"}
)
MAINTENANCE_ALLOWED_COMMANDS = frozenset({"doctor", "status", "report"})
MAINTENANCE_TERMINATE_COMMANDS = frozenset(
    {"cancel", "close", "delete", "abort_close", "commit_close"}
)
MAINTENANCE_EXTENSION_KEY = "agentbc.maintenance"
LEGACY_PERMISSION_EXTENSION_KEY = "agentbc.permission"
MAINTENANCE_MARKER_FILE = ".agentbc-maintenance"


def legacy_permission_cutover_blocked(
    service: Any,
    *,
    board_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a structured cutover gate result for a supported update/preflight.

    The gate scans every board task for old-channel activity.  A task blocks the
    cutover when it is in an active legacy status, carries an issued/unconsumed
    grant, or carries a waiting permission input request (the legacy marker).

    ``completed`` / ``failed`` / ``cancelled`` / ``rejected`` historical tasks are
    never blockers and remain read-only.
    """
    blocked: list[dict[str, Any]] = []
    for task in service.list_tasks():
        reasons = _task_cutover_reasons(task)
        if reasons:
            blocked.append({"task_id": task.id, "status": task.status, "reasons": reasons})
    return {
        "blocked": bool(blocked),
        "code": LEGACY_CUTOVER_BLOCKED if blocked else "",
        "blockers": blocked,
    }


def assert_legacy_cutover_clear(service: Any) -> dict[str, Any]:
    """Raise ``ABCError`` with ``legacy_permission_cutover_blocked`` when gated."""
    gate = legacy_permission_cutover_blocked(service)
    if gate["blocked"]:
        raise ABCError(
            LEGACY_CUTOVER_BLOCKED,
            "Legacy permission channels must be cleared before the cutover",
            {"blockers": gate["blockers"]},
        )
    return gate


def terminal_historical_projection(task: Any) -> dict[str, Any]:
    """Return the read-only historical projection of a terminal legacy task.

    The old ``agentbc.permission`` record and any unconsumed grant facts are
    preserved verbatim in the durable task; this projection only exposes the
    stable legacy mode and never rewrites history.
    """
    status = str(getattr(task, "status", "") or "").strip().lower()
    extensions = dict(getattr(task, "extensions", None) or {})
    permission = extensions.get(LEGACY_PERMISSION_EXTENSION_KEY)
    legacy_mode = str(permission.get("effective_mode") or LEGACY_PERMISSION_MODE)
    if isinstance(permission, dict) and permission.get("effective_mode"):
        legacy_mode = str(permission["effective_mode"])
    grant = None
    if PERMISSION_GRANT_EXTENSION_KEY in extensions:
        try:
            grant = permission_grant_from_extensions(extensions)
        except ABCError:
            grant = None
    projection = {
        "task_id": str(getattr(task, "id", "") or ""),
        "status": status,
        "terminal": status in TASK_TERMINAL_STATES,
        "legacy_permission_mode": legacy_mode,
        "read_only": True,
    }
    if grant is not None:
        projection["legacy_grant"] = {
            "state": grant["state"]["status"],
            "scope": grant["scope"]["kind"],
        }
    return projection


def permission_mode_double_read(config: dict[str, Any] | None) -> dict[str, Any]:
    """Double-read the legacy top-level ``permission_mode`` without rewriting.

    Returns the effective legacy mode plus a flag describing whether the value is
    a historical ``inherit`` that must stay untouched until the first actual
    setting modification.
    """
    loaded = config if isinstance(config, dict) else {}
    raw = str(loaded.get("permission_mode") or "").strip().lower()
    if raw not in CANONICAL_PERMISSION_MODES:
        return {
            "legacy_permission_mode": LEGACY_PERMISSION_MODE,
            "source": "legacy_default",
            "needs_normalization": False,
            "preserve_inherit": False,
        }
    if raw == "inherit":
        return {
            "legacy_permission_mode": "inherit",
            "source": "configured",
            "needs_normalization": False,
            "preserve_inherit": True,
        }
    return {
        "legacy_permission_mode": raw,
        "source": "configured",
        "needs_normalization": False,
        "preserve_inherit": False,
    }


def normalize_permission_mode_once(
    config: dict[str, Any] | None,
    *,
    normalized: str,
) -> dict[str, Any]:
    """Normalize the top-level ``permission_mode`` only on an actual setting change.

    ``inherit`` is preserved as a legacy/advanced compatibility value and is never
    silently rewritten.  The returned copy carries ``permission_mode`` set to the
    requested normalized mode only when the caller is performing a real
    modification (not a read).
    """
    loaded = dict(config) if isinstance(config, dict) else {}
    clean = str(normalized or "").strip().lower()
    if clean not in CANONICAL_PERMISSION_MODES:
        raise ABCError(
            "invalid_permission_mode",
            f"Unknown permission mode: {normalized!r}",
            {"permission_mode": normalized, "allowed": list(CANONICAL_PERMISSION_MODES)},
        )
    current = permission_mode_double_read(loaded)
    if current["preserve_inherit"] and clean == "inherit":
        return loaded
    loaded["permission_mode"] = clean
    return loaded


def is_maintenance_mode(service: Any) -> bool:
    """Return whether the board is in cutover maintenance mode.

    The marker is a board-scoped file, so maintenance never touches the global
    config and is fully reversible by deleting the marker.
    """
    board = Path(getattr(service, "board_root", "") or "").expanduser().resolve()
    if not board.is_dir():
        return False
    marker_path = board / MAINTENANCE_MARKER_FILE
    if not marker_path.is_file():
        return False
    try:
        marker = _read_marker(marker_path)
    except ABCError:
        return True
    return marker.get("active") is True


def maintenance_mode_view(service: Any) -> dict[str, Any]:
    board = Path(getattr(service, "board_root", "") or "").expanduser().resolve()
    if not board.is_dir():
        return {"active": False, "reason": "", "allowed_commands": []}
    marker_path = board / MAINTENANCE_MARKER_FILE
    if not marker_path.is_file():
        return {"active": False, "reason": "", "allowed_commands": []}
    try:
        marker = _read_marker(marker_path)
    except ABCError:
        marker = {"active": True, "reason": "manual bypass install", "entered_at": ""}
    return {
        "active": True,
        "reason": str(marker.get("reason") or "manual bypass install"),
        "entered_at": str(marker.get("entered_at") or ""),
        "allowed_commands": sorted(
            MAINTENANCE_ALLOWED_COMMANDS | MAINTENANCE_TERMINATE_COMMANDS
        ),
    }


def enter_maintenance_mode(service: Any, reason: str = "") -> dict[str, Any]:
    """Enter cutover maintenance mode by writing the board marker."""
    from .config import DEFAULT_BOARD_ROOT

    board = Path(getattr(service, "board_root", "") or DEFAULT_BOARD_ROOT).expanduser().resolve()
    board.mkdir(parents=True, exist_ok=True)
    marker = {
        "active": True,
        "reason": str(reason or "manual bypass install"),
        "entered_at": _utc_now(),
    }
    marker_path = board / MAINTENANCE_MARKER_FILE
    marker_path.write_text(
        json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return maintenance_mode_view(service)


def exit_maintenance_mode(service: Any) -> dict[str, Any]:
    """Exit cutover maintenance mode by removing the board marker."""
    from .config import DEFAULT_BOARD_ROOT

    board = Path(getattr(service, "board_root", "") or DEFAULT_BOARD_ROOT).expanduser().resolve()
    marker_path = board / MAINTENANCE_MARKER_FILE
    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass
    return {"active": False, "reason": "", "allowed_commands": []}


def assert_maintenance_command_allowed(service: Any, command: str) -> None:
    """Fail closed: maintenance mode permits doctor/status/report/termination only."""
    if not is_maintenance_mode(service):
        return
    clean = str(command or "").strip().lower()
    allowed = MAINTENANCE_ALLOWED_COMMANDS | MAINTENANCE_TERMINATE_COMMANDS
    if clean in allowed:
        return
    raise ABCError(
        "legacy_permission_cutover_maintenance",
        f"Maintenance mode only allows: {', '.join(sorted(allowed))}",
        {"command": clean, "allowed_commands": sorted(allowed)},
    )


def _read_marker(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        raise ABCError("maintenance_marker_invalid", "Maintenance marker is unreadable")
    if not isinstance(payload, dict):
        raise ABCError("maintenance_marker_invalid", "Maintenance marker must be an object")
    return payload


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _task_cutover_reasons(task: Any) -> list[str]:
    status = str(getattr(task, "status", "") or "").strip().lower()
    reasons: list[str] = []
    if status in LEGACY_ACTIVE_STATUSES:
        reasons.append(f"old_channel_status:{status}")
    extensions = dict(getattr(task, "extensions", None) or {})
    try:
        grant = permission_grant_from_extensions(extensions)
    except ABCError:
        grant = None
    if grant is not None and grant["state"]["status"] == "issued":
        reasons.append("unconsumed_permission_grant")
    input_request = extensions.get("agentbc.input")
    if isinstance(input_request, dict):
        request_type = str(input_request.get("type") or "").strip().lower()
        request_status = str(input_request.get("status") or "").strip().lower()
        if request_type == "permission" and request_status in {"waiting", "answered"}:
            reasons.append("legacy_permission_marker")
    return reasons
