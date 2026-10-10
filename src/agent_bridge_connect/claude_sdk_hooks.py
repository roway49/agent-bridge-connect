"""Claude SDK hook bridge into ``agentbc.permission_runtime`` (PERM-104-002).

The official SDK exposes ``PreToolUse``/``PostToolUse``/``PostToolUseFailure``
hook events.  AgentBC feeds them into a task-scoped hook-event log so the
``agentbc.permission_runtime`` receipt can be verified only by structured
``PostToolUse`` success — never by callback text, stderr, or exit status.

runtime verification: the official SDK 0.2.142 hook callback receives the raw wire dict
(no ``session_id`` on tool-lifecycle events), so inputs may be dicts or
attribute objects and the log is bound to the official session out-of-band
(``bind_hook_log_session``) before the prompt.  Verification via
:func:`has_structured_post_tool_use_success` fails closed on an unbound or
foreign log, so cross-run hook records can never verify this session's run.

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

HOOK_LOG_VERSION = 2
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
_HOOK_LOG_NAME = "claude_sdk_hooks.jsonl"
_HOOK_SESSION_NAME = "claude_sdk_hooks_session.json"
_MAX_TEXT = 240


def _bounded(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def sanitize_hook_input(hook_input: Any) -> dict[str, Any]:
    """Project one SDK hook input to a redacted structured record.

    The official SDK 0.2.142 hook callback receives the raw wire dict
    (``request_data.get("input")``), so the input may arrive as a plain
    mapping OR as an object with attributes (older/future shapes).  Both are
    accepted; an unsupported event name still fails closed.
    """
    raw_event = _field(hook_input, "hook_event_name")
    event = _bounded(raw_event, 40)
    if event not in HOOK_EVENTS:
        raise ValueError(f"Unsupported Claude hook event: {event or 'unknown'}")
    record: dict[str, Any] = {
        "version": HOOK_LOG_VERSION,
        "event": event,
        "tool_use_id": _bounded(_field(hook_input, "tool_use_id"), 160),
        "tool_name": _bounded(_field(hook_input, "tool_name"), 80),
        # The official 0.2.142 hook wire dict carries no session_id (it is
        # not part of BaseHookInput for tool-lifecycle events), so the
        # session is bound out-of-band via :func:`bind_hook_log_session`
        # and stamped onto every record at write time.
        "session_id": _bounded(_field(hook_input, "session_id"), 160),
    }
    return record


def _field(hook_input: Any, name: str) -> str:
    """Read one field from a dict-or-object hook input."""
    if isinstance(hook_input, dict):
        value = hook_input.get(name)
    else:
        value = getattr(hook_input, name, None)
    return str(value or "").strip()


def _hook_session_path(control_root: str | Path) -> Path:
    return Path(control_root).expanduser() / _HOOK_SESSION_NAME


def bind_hook_log_session(control_root: str | Path, session_id: str) -> bool:
    """Bind the hook log to the official session before the prompt runs.

    Fail closed: returns ``False`` on any persistence error so the caller can
    refuse to verify from a hook log whose provenance was never pinned.
    """
    try:
        path = _hook_session_path(control_root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "version": HOOK_LOG_VERSION,
            "session_id": str(session_id or "").strip(),
            "bound_at": _utc_now(),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def bound_hook_log_session(control_root: str | Path) -> str:
    """Return the official session id pinned to this hook log (or empty)."""
    try:
        payload = json.loads(_hook_session_path(control_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("session_id") or "").strip()


def append_hook_record(
    control_root: str | Path,
    record: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """Append one redacted hook record to the task-scoped hook log.

    The official SDK 0.2.142 tool-lifecycle hook wire dict carries no
    ``session_id``, so production binds the session out-of-band before the
    prompt (``bind_hook_log_session``) and it is stamped onto every record at
    write time.  An explicit ``session_id`` argument (from a hook input that
    does carry one) must match the bound session or the record is rejected.
    """
    path = Path(control_root).expanduser() / _HOOK_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    bound_session = bound_hook_log_session(control_root)
    explicit_session = str(session_id or "").strip()
    if explicit_session and bound_session and explicit_session != bound_session:
        raise ValueError(
            "Hook record session does not match the bound official session"
        )
    entry = {
        "version": HOOK_LOG_VERSION,
        "event": str(record.get("event") or ""),
        "tool_use_id": str(record.get("tool_use_id") or ""),
        "tool_name": str(record.get("tool_name") or ""),
        "session_id": explicit_session or bound_session,
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
    *,
    event_sink: Any = None,
) -> dict[str, list[Any]]:
    """Build the official SDK ``hooks`` mapping feeding permission_runtime.

    Every ``PreToolUse``/``PostToolUse``/``PostToolUseFailure`` event is
    appended to the task-scoped hook log; ``PostToolUse`` records carry the
    structured success marker that ``verify`` requires.  When ``event_sink``
    (the run's :class:`ClaudeSDKControlTransport`) is supplied, the same
    structured event is captured on the transport so duplicate, failed,
    out-of-window, and cross-run identities are rejected at verification
    time.  Hook failures never block the tool call path (they are
    diagnostics, not permission gates — the ``can_use_tool`` bridge owns the
    actual approval).
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
                # The official 0.2.142 wire dict for tool-lifecycle hooks
                # carries no session_id; the log is pre-bound to the official
                # session (fail closed) and stamped here.
                append_hook_record(control_root, record, extra=extra)
                if event_sink is not None:
                    capture = getattr(event_sink, "capture_tool_event", None)
                    if callable(capture):
                        capture(
                            event=event_name,
                            tool_use_id=str(record.get("tool_use_id") or ""),
                            tool_name=str(record.get("tool_name") or ""),
                            session_id=str(record.get("session_id") or ""),
                        )
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
    session_id: str = "",
) -> bool:
    """Return whether the hook log proves structured PostToolUse success.

    PERM-104-002 (runtime verification): the ``agentbc.permission_runtime`` verify step
    accepts only this structured evidence — never callback text, stderr, or
    exit status.  Fail closed: when an official session id is supplied it
    must match the session bound to the log and stamped on the record, so a
    foreign or cross-run hook log can never verify this session's receipt.
    An unbound log (no session pin) never verifies for a session-bound run.
    """
    bound_session = bound_hook_log_session(control_root)
    if session_id and bound_session != str(session_id or "").strip():
        return False
    for record in load_hook_records(control_root):
        if record.get("event") != "PostToolUse":
            continue
        if record.get("blocked") is True:
            continue
        if tool_use_id and record.get("tool_use_id") != tool_use_id:
            continue
        if session_id and record.get("session_id") != str(session_id or "").strip():
            continue
        return True
    return False


__all__ = [
    "HOOK_EVENTS",
    "HOOK_LOG_VERSION",
    "append_hook_record",
    "bind_hook_log_session",
    "bound_hook_log_session",
    "build_sdk_hooks",
    "has_structured_post_tool_use_success",
    "hook_log_path",
    "load_hook_records",
    "sanitize_hook_input",
]
