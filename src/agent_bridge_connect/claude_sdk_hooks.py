"""Claude SDK hook bridge into ``agentbc.permission_runtime`` (PERM-104-002).

The official SDK exposes ``PreToolUse``/``PostToolUse``/``PostToolUseFailure``
hook events.  AgentBC feeds them into a task-scoped hook-event log so the
``agentbc.permission_runtime`` receipt can be verified only by structured
``PostToolUse`` success — never by callback text, stderr, or exit status.

Only redacted, bounded, structured facts are persisted: tool name,
``tool_use_id``, a bounded reason, and the event kind.  Tool input, raw argv,
paths, and output bodies never enter the hook log.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from agent_bridge_connect.permission_runtime import classify_block_domain  # noqa: F401

HOOK_LOG_VERSION = 1
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
_HOOK_LOG_NAME = "claude_sdk_hooks.jsonl"
_MAX_TEXT = 240


def _bounded(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def sanitize_hook_input(hook_input: Any) -> dict[str, Any]:
    """Project one SDK hook input to a redacted structured record."""
    event = _bounded(getattr(hook_input, "hook_event_name", ""), 40)
    if event not in HOOK_EVENTS:
        raise ValueError(f"Unsupported Claude hook event: {event or 'unknown'}")
    record: dict[str, Any] = {
        "version": HOOK_LOG_VERSION,
        "event": event,
        "tool_use_id": _bounded(getattr(hook_input, "tool_use_id", ""), 160),
        "tool_name": _bounded(getattr(hook_input, "tool_name", ""), 80),
    }
    return record


def append_hook_record(
    control_root: str | Path,
    record: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one redacted hook record to the task-scoped hook log."""
    path = Path(control_root).expanduser() / _HOOK_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = {
        "version": HOOK_LOG_VERSION,
        "event": str(record.get("event") or ""),
        "tool_use_id": str(record.get("tool_use_id") or ""),
        "tool_name": str(record.get("tool_name") or ""),
        "recorded_at": _utc_now(),
    }
    if extra:
        for key in ("domain", "blocked", "tool_input_digest"):
            if key in extra:
                entry[key] = extra[key]
    lock = _hook_lock(path)
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _hook_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_sdk_hooks(
    control_root: str | Path,
) -> dict[str, list[Any]]:
    """Build the official SDK ``hooks`` mapping feeding permission_runtime.

    Every ``PreToolUse``/``PostToolUse``/``PostToolUseFailure`` event is
    appended to the task-scoped hook log; ``PostToolUse`` records carry the
    structured success marker that ``verify`` requires.  Hook failures never
    block the tool call path (they are diagnostics, not permission gates —
    the ``can_use_tool`` bridge owns the actual approval).
    """
    from claude_agent_sdk import HookMatcher

    def _make(event_name: str) -> Any:
        async def _hook(hook_input: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
            try:
                record = sanitize_hook_input(hook_input)
                extra: dict[str, Any] = {}
                if event_name == "PostToolUse":
                    extra["blocked"] = False
                elif event_name == "PostToolUseFailure":
                    extra["blocked"] = True
                append_hook_record(control_root, record, extra=extra)
            except Exception:  # noqa: BLE001 - hook diagnostics never crash runs.
                pass
            return {}

        return _hook

    hooks: dict[str, list[Any]] = {}
    for event_name in HOOK_EVENTS:
        hooks[event_name] = [HookMatcher(matcher=None, hooks=[_make(event_name)])]
    return hooks


def hook_log_path(control_root: str | Path) -> Path:
    return Path(control_root).expanduser() / _HOOK_LOG_NAME


def load_hook_records(control_root: str | Path) -> list[dict[str, Any]]:
    path = hook_log_path(control_root)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def has_structured_post_tool_use_success(
    control_root: str | Path,
    *,
    tool_use_id: str = "",
) -> bool:
    """Return whether the hook log proves structured PostToolUse success.

    PERM-104-002: the ``agentbc.permission_runtime`` verify step accepts only
    this structured evidence — never callback text, stderr, or exit status.
    """
    for record in load_hook_records(control_root):
        if record.get("event") != "PostToolUse":
            continue
        if record.get("blocked") is True:
            continue
        if tool_use_id and record.get("tool_use_id") != tool_use_id:
            continue
        return True
    return False


__all__ = [
    "HOOK_EVENTS",
    "HOOK_LOG_VERSION",
    "append_hook_record",
    "build_sdk_hooks",
    "has_structured_post_tool_use_success",
    "hook_log_path",
    "load_hook_records",
    "sanitize_hook_input",
]
