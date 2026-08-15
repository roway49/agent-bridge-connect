"""Supported ``agentbc update`` preflight and cutover-ready stamp (PERM-103-005).

This narrow module is the supported update/install entry point for the
1.0.3A cutover.  ``agentbc update`` runs the strict legacy-permission gate
(:mod:`migration`) before any install:

- while any old-channel task is ``pending`` / ``running`` /
  ``input_required`` / ``needs_recovery``, an unconsumed one-shot grant
  exists, or a legacy permission marker is waiting, the preflight returns
  ``legacy_permission_cutover_blocked`` together with per-task evidence;
- only an explicitly cleared old version (every non-terminal task
  terminated or completed in the old version, all grants consumed or
  revoked, no pending markers) produces ``cutover-ready``.  The preflight
  records a durable board-scoped stamp so the supported path is auditable
  and repeatable.

A manual wheel/bundle install that bypasses the supported preflight enters
cutover maintenance mode via :func:`manual_bypass_install`; maintenance
permits ``doctor`` / ``status`` / ``report`` and explicit termination only
and blocks task creation and dispatch until the board is cleared and a
supported update completes.

This module never issues or consumes permission grants, never detects
permission markers outside the cutover gate, and never rewrites terminal
history or the original ``agentbc.permission`` extensions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULT_BOARD_ROOT
from .migration import (
    LEGACY_CUTOVER_BLOCKED,
    enter_maintenance_mode,
    exit_maintenance_mode,
    is_maintenance_mode,
    legacy_permission_cutover_blocked,
)

CUTOVER_READY_STATE = "cutover-ready"
CUTOVER_BLOCKED_STATE = "legacy_permission_cutover_blocked"
MAINTENANCE_STATE = "legacy_permission_cutover_maintenance"
CUTOVER_READY_FILE = ".agentbc-cutover-ready"

_UPDATE_NOTE = (
    "create/dispatch blocked until the old channel is cleared and a "
    "supported update completes"
)


def update_preflight(service: Any) -> dict[str, Any]:
    """Run the supported ``agentbc update`` preflight.

    The gate scans every board task and blocks with
    ``legacy_permission_cutover_blocked`` and per-task evidence while any
    old-channel activity remains.  When the old version is explicitly
    cleared the preflight records the durable ``cutover-ready`` stamp and
    exits cutover maintenance mode if a manual bypass had entered it.
    """
    maintenance_active = is_maintenance_mode(service)
    gate = legacy_permission_cutover_blocked(service)
    if gate["blocked"]:
        return {
            "state": CUTOVER_BLOCKED_STATE,
            "code": LEGACY_CUTOVER_BLOCKED,
            "cutover_ready": False,
            "blockers": gate["blockers"],
            "maintenance_active": maintenance_active,
        }
    stamp = record_cutover_ready(service)
    if maintenance_active:
        exit_maintenance_mode(service)
    return {
        "state": CUTOVER_READY_STATE,
        "code": "",
        "cutover_ready": True,
        "blockers": [],
        "maintenance_active": False,
        "stamp": stamp,
    }


def record_cutover_ready(service: Any) -> dict[str, Any]:
    """Persist the durable board-scoped ``cutover-ready`` stamp.

    The stamp records the installed version and the moment the explicitly
    cleared old version passed the supported preflight.  It never touches
    task records, terminal history, or the global config.
    """
    board = _board_root(service)
    board.mkdir(parents=True, exist_ok=True)
    stamp = {
        "state": CUTOVER_READY_STATE,
        "installed_version": __version__,
        "cutover_ready": True,
        "cleared_at": _utc_now(),
    }
    stamp_path = board / CUTOVER_READY_FILE
    stamp_path.write_text(
        json.dumps(stamp, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return dict(stamp)


def cutover_ready_stamp(service: Any) -> dict[str, Any] | None:
    """Return the durable stamp when the supported cutover already ran."""
    board = _board_root(service)
    stamp_path = board / CUTOVER_READY_FILE
    if not stamp_path.is_file():
        return None
    try:
        payload = json.loads(stamp_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("cutover_ready") is not True:
        return None
    return payload


def manual_bypass_install(
    service: Any,
    reason: str = "manual wheel/bundle bypass",
) -> dict[str, Any]:
    """Enter cutover maintenance mode for a manual install that skipped preflight.

    A hand-installed wheel or bundle is not a supported update: the board
    drops into maintenance mode which permits ``doctor`` / ``status`` /
    ``report`` and explicit termination only and blocks create/dispatch
    until the old channel is cleared and a supported update completes.
    """
    view = enter_maintenance_mode(service, reason=reason)
    return {
        "state": MAINTENANCE_STATE,
        "code": MAINTENANCE_STATE,
        "maintenance": view,
        "note": _UPDATE_NOTE,
    }


def assert_update_maintenance_allowed(service: Any, command: str) -> None:
    """Fail closed inside maintenance: raise for anything but the allowed set.

    Used by new-task / new-runtime entry points that must not mutate the
    board while the cutover is unresolved.
    """
    from .migration import assert_maintenance_command_allowed

    assert_maintenance_command_allowed(service, command)


def _board_root(service: Any) -> Path:
    board = Path(getattr(service, "board_root", "") or DEFAULT_BOARD_ROOT)
    return board.expanduser().resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
