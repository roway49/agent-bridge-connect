"""Minimal OpenCode CLI executor adapter (EXEC-105-001-02).

This module provides the L0 OpenCode adapter registered for fresh dispatch
and execution. Full session lifecycle, approval continuation, retry and
handoff remain unsupported; the existing three executor adapters retain
their own contracts.

Frozen native call contract (EXEC-105-001-01)::

    opencode run --format json --dir <absolute_project>
                 [--agent build] [--model provider/model] -- <prompt>

with ``shell=False``, ``Popen.cwd == --dir`` and child ``PWD == --dir``.
``inherit`` never injects permission configuration.  ``full`` fixes the
``build`` agent and installs the process-scoped allow overlay derived from the
official ``opencode debug agent build`` permission list.  ``safe``, approval
continuation, resume and every other unauthenticated action are rejected with
a stable diagnostic instead of being approximated.

Nothing here fabricates a final callback, a completed step or a ``completed``
state: only a real ``AGENTBC_FINAL_CALLBACK`` line parsed by the shared
validator can produce one.  Credentials, the raw prompt, the raw stdout and
raw tool input never enter the public poll projection.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

from agent_bridge_connect.adapters import (
    AdapterResult,
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    StartResult,
)
from agent_bridge_connect.execution_contract import (
    CallbackValidation,
    detect_retryable_transport_failure,
    extract_callback_validation_from_output,
    route_executor_terminal,
)
from agent_bridge_connect.executors.base import CLIExecutorBase
from agent_bridge_connect.path_provider import find_binary
from agent_bridge_connect.permission_modes import permission_record_from_extensions
from agent_bridge_connect.prompt_contract import (
    PromptPlatformExtras,
    build_prompt_contract,
)
from agent_bridge_connect.protocol import ABCError

OPENCODE_EXECUTOR_ID = "opencode"
#: The only accepted transport: one blocking ``opencode run`` JSON-lines call.
OPENCODE_TRANSPORT = "cli-json"
DEFAULT_TIMEOUT_S = 600
#: Fixed agent used by full mode.  ``--auto`` alone is never treated as full.
FULL_AGENT = "build"
#: Permission keys whose official resolution uses an action string, not a
#: pattern map (EXEC-105-001-01 normalized algorithm).
ACTION_PERMISSION_KEYS = (
    "doom_loop",
    "question",
    "plan_enter",
    "plan_exit",
    "todoread",
    "todowrite",
)
#: Bounds keep a hostile or corrupt stream from growing without limit.  The
#: streams are read concurrently and incrementally, so these caps bound real
#: worker memory rather than only the post-hoc parse.
MAX_EVENT_LINE_CHARS = 1_000_000
MAX_STREAM_CHARS = 4_000_000
MAX_STREAM_LINES = 100_000
READ_CHUNK_BYTES = 65_536
MAX_TOOL_DIAGNOSTICS = 32
TERMINATE_GRACE_S = 5.0
OFFICIAL_RULES_TIMEOUT_S = 30
HEARTBEAT_INTERVAL_S = 30.0

#: Tool-error text is only ever inspected to classify a failure; it is never
#: projected.  A permission denial is one specific kind of tool error, not a
#: synonym for "any tool error".
_PERMISSION_ERROR_MARKERS = (
    "permission",
    "auto-reject",
    "auto reject",
    "not permitted",
    "denied",
    "approval",
)

_RESUME_FLAGS = ("resume", "continue", "continue_session", "fork", "attach", "share")


class _LeaseHeartbeat:
    """Beat one RunLease while the blocking OpenCode turn is in flight."""

    def __init__(
        self,
        executor: "OpenCodeExecutor",
        run_id: str,
        interval_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._executor = executor
        self._run_id = run_id
        self._interval_s = max(float(interval_s), 0.01)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"agentbc-opencode-heartbeat-{self._run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            try:
                self._executor._heartbeat_run(self._run_id)
            except Exception:  # noqa: BLE001 - heartbeat is never fatal
                pass


def iter_event_lines(chunks: Iterable[str]) -> Iterator[str]:
    """Yield complete lines from arbitrarily chunked stdout text."""
    buffer = ""
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def parse_event_chunks(
    chunks: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse bounded JSON objects from chunked JSON-lines output.

    Malformed, non-object and oversized lines are dropped and counted; the
    raw line is never stored, so credentials or prompts in a broken line can
    not reach the public projection.
    """
    events: list[dict[str, Any]] = []
    lines = malformed = oversized = 0
    for raw in iter_event_lines(chunks):
        line = raw.strip()
        if not line:
            continue
        lines += 1
        if len(line) > MAX_EVENT_LINE_CHARS:
            oversized += 1
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        events.append(payload)
    return events, {
        "lines": lines,
        "malformed_lines": malformed,
        "oversized_lines": oversized,
    }


def parse_json_lines(text: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse a complete JSON-lines stdout string."""
    return parse_event_chunks([text or ""])


def build_full_permission_overlay(
    rules: Any,
) -> dict[str, dict[str, Any]]:
    """Build the process-scoped allow overlay from official permission rules.

    Implements the EXEC-105-001-01 normalized algorithm: every observed
    ``permission``/``pattern`` pair becomes ``allow``, ``*`` and the known
    action keys become the ``allow`` action string, and both the top-level
    ``permission`` table and ``agent.build.permission`` are overridden.  A
    single wildcard or ``--auto`` is never sufficient.
    """
    if not isinstance(rules, list):
        raise ABCError(
            "opencode_full_rules_invalid",
            "Official OpenCode permission rules must be a JSON list.",
        )
    permissions: dict[str, Any] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise ABCError(
                "opencode_full_rules_invalid",
                "Every official permission rule must be a JSON object.",
            )
        permission = rule.get("permission")
        pattern = rule.get("pattern")
        if not isinstance(permission, str) or not permission.strip():
            raise ABCError(
                "opencode_full_rules_invalid",
                "Official permission rule is missing a permission name.",
            )
        if not isinstance(pattern, str):
            raise ABCError(
                "opencode_full_rules_invalid",
                "Official permission rule is missing a pattern.",
            )
        bucket = permissions.get(permission)
        if not isinstance(bucket, dict):
            bucket = {}
            permissions[permission] = bucket
        bucket[pattern] = "allow"
    permissions["*"] = "allow"
    for key in ACTION_PERMISSION_KEYS:
        permissions[key] = "allow"
    return {
        "permission": permissions,
        "agent": {FULL_AGENT: {"permission": permissions}},
    }


def merge_config_content(existing: Any, overlay: dict[str, Any]) -> str:
    """Overlay permission tables while preserving non-permission config.

    ``OPENCODE_CONFIG_CONTENT`` may carry unrelated inline configuration.  A
    non-object or syntactically invalid value is a stable, fail-closed
    diagnostic; it is never silently replaced with a default.
    """
    base: dict[str, Any] = {}
    if existing is not None and existing != "":
        if not isinstance(existing, str):
            raise ABCError(
                "opencode_config_invalid",
                "OPENCODE_CONFIG_CONTENT must be a JSON string.",
            )
        if existing.strip():
            try:
                parsed = json.loads(existing)
            except ValueError as exc:
                raise ABCError(
                    "opencode_config_invalid",
                    f"OPENCODE_CONFIG_CONTENT is not valid JSON: {exc}",
                ) from exc
            if not isinstance(parsed, dict):
                raise ABCError(
                    "opencode_config_invalid",
                    "OPENCODE_CONFIG_CONTENT must be a JSON object.",
                )
            base = dict(parsed)
    permissions = overlay.get("permission")
    agent = base.get("agent")
    if agent is None:
        agent = {}
    if not isinstance(agent, dict):
        raise ABCError(
            "opencode_config_invalid",
            "OPENCODE_CONFIG_CONTENT agent must be a JSON object.",
        )
    agent = dict(agent)
    build = agent.get(FULL_AGENT)
    if build is None:
        build = {}
    if not isinstance(build, dict):
        raise ABCError(
            "opencode_config_invalid",
            f"OPENCODE_CONFIG_CONTENT agent.{FULL_AGENT} must be a JSON object.",
        )
    build = dict(build)
    build["permission"] = permissions
    agent[FULL_AGENT] = build
    base["permission"] = permissions
    base["agent"] = agent
    return json.dumps(base, ensure_ascii=False)


class _BoundedStreamReader(threading.Thread):
    """Drain one child pipe concurrently with hard, enforced bounds.

    Reading happens in a dedicated thread so the child never blocks on a full
    pipe, and so stdout/stderr are consumed incrementally instead of being
    buffered by ``communicate()``.  A single line longer than
    ``max_line_chars`` is discarded without ever being buffered, and the total
    captured text is capped at ``max_chars``; once a cap is hit the reader
    keeps draining (to avoid deadlocking the child) but records the truncation
    instead of growing memory.
    """

    def __init__(self, stream: Any, name: str) -> None:
        super().__init__(name=name, daemon=True)
        self._stream = stream
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._discarding_line = False
        self.lines: list[str] = []
        self.bytes = 0
        self.total_lines = 0
        self.oversized_lines = 0
        self.dropped_lines = 0
        self.captured_chars = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                self.bytes += len(chunk)
                self._feed(self._decoder.decode(chunk))
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._feed(tail)
            self._flush_tail()
        except (OSError, ValueError):  # pragma: no cover - defensive drain
            self.truncated = True
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def _feed(self, text: str) -> None:
        if not text:
            return
        if self._discarding_line:
            newline = text.find("\n")
            if newline == -1:
                return
            self._discarding_line = False
            self.total_lines += 1
            self.dropped_lines += 1
            text = text[newline + 1 :]
            if not text:
                return
        combined = self._buffer + text
        if combined.endswith("\n"):
            segments = combined.split("\n")[:-1]
            self._buffer = ""
        else:
            segments = combined.split("\n")
            self._buffer = segments.pop()
        for segment in segments:
            self.total_lines += 1
            self._store_line(segment)
        if len(self._buffer) > MAX_EVENT_LINE_CHARS:
            self._buffer = ""
            self._discarding_line = True
            self.oversized_lines += 1
            self.truncated = True

    def _store_line(self, line: str) -> None:
        if self.captured_chars + len(line) + 1 > MAX_STREAM_CHARS:
            self.truncated = True
            self.dropped_lines += 1
            return
        if len(self.lines) >= MAX_STREAM_LINES:
            self.truncated = True
            self.dropped_lines += 1
            return
        self.lines.append(line)
        self.captured_chars += len(line) + 1

    def _flush_tail(self) -> None:
        if self._discarding_line:
            self._buffer = ""
            return
        if self._buffer:
            self.total_lines += 1
            self._store_line(self._buffer)
            self._buffer = ""

    def text(self) -> str:
        return "\n".join(self.lines)

    def diagnostics(self) -> dict[str, int | bool]:
        return {
            "bytes": self.bytes,
            "lines": self.total_lines,
            "captured_lines": len(self.lines),
            "captured_chars": self.captured_chars,
            "oversized_lines": self.oversized_lines,
            "dropped_lines": self.dropped_lines,
            "truncated": self.truncated,
        }


def _raw_error_text(part: dict[str, Any]) -> str:
    """Extract tool-error text for classification only; never projected."""
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    error = state.get("error")
    pieces: list[str] = []
    if isinstance(error, str):
        pieces.append(error)
    elif isinstance(error, dict):
        for key in ("message", "name", "type", "reason"):
            value = error.get(key)
            if isinstance(value, str):
                pieces.append(value)
    status = state.get("status")
    if isinstance(status, str):
        pieces.append(status)
    return " ".join(pieces).lower()


def _tool_call_diagnostic(
    part: dict[str, Any], child_session_ids: list[str]
) -> dict[str, Any]:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    child = metadata.get("sessionId")
    child_session = child if isinstance(child, str) and child.strip() else ""
    if child_session and child_session not in child_session_ids:
        child_session_ids.append(child_session)
    status = str(state.get("status") or "")
    error = state.get("error")
    state_error = bool(error not in (None, "", {}, [])) or status == "error"
    raw_error = _raw_error_text(part)
    permission_rejected = state_error and any(
        marker in raw_error for marker in _PERMISSION_ERROR_MARKERS
    )
    # Raw tool input/output/error text is deliberately never projected.
    return {
        "tool": str(part.get("tool") or ""),
        "call_id": str(part.get("callID") or ""),
        "status": status,
        "state_error": state_error,
        "permission_rejected": permission_rejected,
        "child_session_present": bool(child_session),
    }


def _part_message_id(part: Any) -> str:
    """Return the official message/part association id, when the stream has one."""
    if not isinstance(part, dict):
        return ""
    message_id = part.get("messageID")
    return message_id.strip() if isinstance(message_id, str) else ""


def _turn_holds_callback(turn: dict[str, Any]) -> bool:
    return any(
        line.strip().startswith("AGENTBC_FINAL_CALLBACK:")
        for line in "\n".join(turn["texts"]).splitlines()
    )


def _turn_stop_is_final(turn: dict[str, Any]) -> bool:
    """True only when this turn's own finish is the normal ``stop``.

    A ``stop`` observed in an earlier turn can never prove that a later turn
    finished normally, and when the official ``messageID`` association exists
    the closing finish must share it with the response text it ends.
    """
    if turn.get("finish_reason") != "stop":
        return False
    finish_message = str(turn.get("finish_message_id") or "")
    text_messages = [str(item) for item in turn.get("text_message_ids") or []]
    if finish_message and text_messages and finish_message not in text_messages:
        return False
    return True


def collect_protocol_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind the run to the official main session and normalize JSON events.

    The main session is the top-level ``sessionID`` only.  A tool
    ``metadata.sessionId`` is a native child session and is recorded
    separately so it can never impersonate the main session.

    Events are grouped into ordered turns delimited by ``step_finish`` so the
    final response can be bound to *its own* normal ``stop`` instead of any
    historical one.  A tool error is only treated as recovered by a later
    tool event that reached the official ``completed`` status *and* is
    verifiably associated with the failed call and input; running,
    pending or unrelated successes never clear an unresolved error.
    """
    event_types: Counter[str] = Counter()
    main_session_ids: list[str] = []
    child_session_ids: list[str] = []
    text_parts: list[str] = []
    step_finishes: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    action_keys: list[tuple[str, str, str]] = []
    turn_active = False
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] = {
        "texts": [],
        "text_message_ids": [],
        "finish_reason": None,
        "finish_message_id": "",
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "unknown")
        event_types[event_type] += 1
        session_id = event.get("sessionID")
        if (
            isinstance(session_id, str)
            and session_id.strip()
            and session_id not in main_session_ids
        ):
            main_session_ids.append(session_id)
        if event_type in {"step_start", "text", "tool_use", "reasoning"}:
            turn_active = True
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        message_id = _part_message_id(part)
        if event_type == "tool_use":
            diagnostic = _tool_call_diagnostic(part, child_session_ids)
            tool_calls.append(diagnostic)
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            # Internal only: neither input nor its digest enters the projection.
            fingerprint = hashlib.sha256(
                json.dumps(state.get("input"), sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            action_keys.append((diagnostic["tool"], diagnostic["call_id"], fingerprint))
        elif event_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text)
                current["texts"].append(text)
                if message_id:
                    current["text_message_ids"].append(message_id)
        elif event_type == "step_finish":
            reason = str(part.get("reason") or "")
            step_finishes.append(reason)
            current["finish_reason"] = reason
            current["finish_message_id"] = message_id
            turns.append(current)
            turn_active = False
            current = {
                "texts": [],
                "text_message_ids": [],
                "finish_reason": None,
                "finish_message_id": "",
            }
    if turn_active:
        # An unterminated trailing turn: the response has no normal finish.
        turns.append(current)
    text = "\n".join(text_parts)
    final_stop_seen = bool(
        turns and _turn_holds_callback(turns[-1]) and _turn_stop_is_final(turns[-1])
    )
    # Only completion of the same identified call with unchanged input can
    # resolve its failure. A fresh call is not proof of retry association.
    pending: list[tuple[dict[str, Any], tuple[str, str, str]]] = []
    for item, key in zip(tool_calls, action_keys):
        if item["state_error"]:
            pending.append((item, key))
        elif item["status"] == "completed" and key[0] and key[1]:
            pending = [(error, prior) for error, prior in pending if prior != key]
    pending_errors = [error for error, _ in pending]
    tool_error_count = sum(1 for item in tool_calls if item["state_error"])
    return {
        "event_types": sorted(event_types),
        "event_type_counts": dict(event_types),
        "main_session_ids": main_session_ids,
        "child_session_ids": child_session_ids,
        "main_session_count": len(main_session_ids),
        "session_id": main_session_ids[0] if len(main_session_ids) == 1 else "",
        "session_ambiguous": len(main_session_ids) > 1,
        "text": text,
        "text_parts": len(text_parts),
        "final_marker_count": sum(
            1
            for line in text.splitlines()
            if line.strip().startswith("AGENTBC_FINAL_CALLBACK:")
        ),
        "step_finishes": step_finishes,
        "stop_finish_seen": "stop" in step_finishes,
        "final_stop_seen": final_stop_seen,
        "tool_calls": tool_calls,
        "tool_error_count": tool_error_count,
        "unrecovered_tool_error_count": len(pending_errors),
        "recovered_tool_error_count": tool_error_count - len(pending_errors),
        "permission_rejected": any(
            bool(item["permission_rejected"])
            for item in tool_calls
            if item["state_error"]
        ),
        "unrecovered_permission_rejected": any(
            bool(pending["permission_rejected"]) for pending in pending_errors
        ),
    }


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ABCError(
            "opencode_argument_invalid",
            f"OpenCode {field} must be a string or null.",
        )
    text = value.strip()
    return text or None


def _kill_owned_group(pid: int, sig: int) -> None:
    """Signal only the exact process group created for this adapter run."""
    if pid <= 0:
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _process_group_alive(pgid: int) -> bool:
    """Return True only when the owned process group still has members."""
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_group_gone(pgid: int, grace_s: float, interval_s: float = 0.02) -> bool:
    """Poll the owned group until it is empty or the bounded grace expires.

    A surviving writer keeps the group alive even after its parent is reaped,
    so "parent reaped" alone is never enough to call the run stopped.
    """
    deadline = time.monotonic() + max(float(grace_s), 0.0)
    while True:
        if not _process_group_alive(pgid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval_s, remaining))


def _empty_stream_diag() -> dict[str, int | bool]:
    return {
        "bytes": 0,
        "lines": 0,
        "captured_lines": 0,
        "captured_chars": 0,
        "oversized_lines": 0,
        "dropped_lines": 0,
        "truncated": False,
    }


class OpenCodeExecutor(CLIExecutorBase):
    """L0 executor using ``opencode run --format json``."""

    def __init__(
        self,
        command: str | None = None,
        model: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__()
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ABCError(
                "opencode_argument_invalid",
                "OpenCode timeout_s must be a positive number.",
            )
        if not math.isfinite(float(timeout_s)) or float(timeout_s) <= 0:
            raise ABCError(
                "opencode_argument_invalid",
                "OpenCode timeout_s must be a positive number.",
            )
        self.timeout_s = float(timeout_s)
        self.model = _optional_text(model, field="model")
        configured = _optional_text(command, field="command")
        extra = [configured] if configured else None
        self._discovery = find_binary(OPENCODE_EXECUTOR_ID, extra_paths=extra)
        resolved = str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._version = ""
        self._last_run_id: str | None = None
        # Test seam: a controlled provider replaces the native permission
        # discovery subprocess.  Production leaves this ``None``.
        self._official_rules_provider: Any = None

    # -- port surface ---------------------------------------------------

    def probe(self) -> ProbeResult:
        """Report availability without ever invoking a model."""
        if self.agent_bin is None or not self.agent_bin.is_file():
            return ProbeResult(
                ok=False,
                message="opencode unavailable",
                details={
                    "agent_bin": "",
                    "agent_bin_source": self._discovery.get("source") or "not_found",
                    "transport": OPENCODE_TRANSPORT,
                    "manual_override": self._discovery.get("manual_override") or "",
                },
            )
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "--version"],
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult(
                ok=False,
                message=f"opencode unavailable: {exc}",
                details={
                    "agent_bin": str(self.agent_bin),
                    "agent_bin_source": self._discovery.get("source") or "unknown",
                    "transport": OPENCODE_TRANSPORT,
                },
            )
        version = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode == 0:
            self._version = version
        return ProbeResult(
            ok=completed.returncode == 0,
            message=version or f"opencode exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
                "agent_bin_source": self._discovery.get("source") or "unknown",
                "transport": OPENCODE_TRANSPORT,
                "auth_owner": "opencode_cli",
                "version": version,
                "returncode": completed.returncode,
            },
        )

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            structured_output=True,
            streaming_events=False,
            resume=False,
            cancel=False,
            input_required=False,
            model_selection=True,
            multimodal=False,
            image_input=False,
            image_generation=False,
            image_editing=False,
            parallelism=1,
            level=ExecutorLevel.L0,
        )

    def cancel(self, run_id: str) -> AdapterResult:
        return AdapterResult(False, "opencode cancel is not supported")

    def send_input(self, run_id: str, message: str) -> AdapterResult:
        return AdapterResult(
            False,
            "opencode input and approval continuation are not supported",
        )

    def start(self, task_packet: dict[str, Any]) -> StartResult:
        try:
            return self._start(task_packet)
        except ABCError as exc:
            return StartResult(ok=False, run_id="", message=f"{exc.code}: {exc.message}")

    # -- start pipeline -------------------------------------------------

    def _start(self, task_packet: dict[str, Any]) -> StartResult:
        if not isinstance(task_packet, dict):
            raise ABCError("opencode_task_invalid", "Task packet must be an object.")
        task_id = str(task_packet.get("task_id") or "").strip()
        if not task_id:
            raise ABCError("opencode_task_invalid", "Task packet is missing task_id.")
        steps = task_packet.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ABCError(
                "opencode_steps_missing",
                "OpenCode adapter requires at least one declared step.",
            )
        if self.agent_bin is None:
            raise ABCError(
                "opencode_binary_unavailable",
                "OpenCode binary was not found; set AGENTBC_OPENCODE_BIN "
                "or pass command.",
            )
        workspace = (
            task_packet.get("workspace")
            if isinstance(task_packet.get("workspace"), dict)
            else {}
        )
        root_value = workspace.get("project_root") or workspace.get("root") or ""
        project_root = Path(str(root_value)).expanduser()
        if not project_root.is_dir():
            raise ABCError(
                "opencode_workspace_invalid",
                f"Project root is not a directory: {root_value}",
            )
        project_root = project_root.resolve()
        self._reject_unsupported_actions(task_packet)
        mode = self._permission_mode(task_packet)
        prompt = build_prompt_contract(
            task_packet, PromptPlatformExtras(
                task_id_line=True,
                extra_rules=(
                    "The complete declared steps are embedded below. Task brief, report and "
                    "record paths are AgentBC Core bookkeeping metadata, not prerequisite inputs. "
                    "Do not open them to rediscover the task or inspect status; execute the embedded steps.",
                    "Only read project files and explicit task inputs needed by a declared step. "
                    "If a required input is outside native allowed roots, stop with the actual blocker; "
                    "do not copy it, use a different tool to bypass the rejection, or change permissions.",
                ),
                progress_instructions=(
                    "This OpenCode transport submits step results only in the final callback below. "
                    "Do not invoke the AgentBC CLI for progress or callback, repair session receipts, "
                    "or edit task records. Mid-run step progress is not supported.",
                    "Execute and verify every declared step, then include each actual step result "
                    "and concise evidence in the final callback. AgentBC Core generates the report.",
                ),
                native_permission_rule=(
                    "This transport cannot resume approvals or elevate permissions. Keep the "
                    "startup permission policy unchanged. If a native permission blocks execution, "
                    "stop and describe the blocker; never claim completed, retry with broader "
                    "permissions, or ask for a full grant through the callback."
                ),
            )
        )
        if not prompt.strip():
            raise ABCError(
                "opencode_prompt_invalid", "OpenCode prompt contract is empty."
            )
        argv = self._build_command(project_root, prompt, mode)
        env = self._build_environment(project_root, mode)
        run_id = self._resolve_run_id(task_id, task_packet)
        self._start_run_lease(task_packet, run_id, OPENCODE_EXECUTOR_ID)
        heartbeat = _LeaseHeartbeat(self, run_id)
        heartbeat.start()
        try:
            try:
                execution = self._run_command(argv, project_root, env)
            except Exception as exc:  # noqa: BLE001 - never leave an active lease
                self._mark_run_stale(run_id)
                self._store_recovery_result(
                    run_id, "opencode_execution_error", exc
                )
                return StartResult(
                    ok=True,
                    run_id=run_id,
                    message="opencode execution needs recovery",
                )
            try:
                poll_result = self._build_poll_result(
                    task_packet, run_id, mode, execution
                )
            except Exception as exc:  # noqa: BLE001 - never leave an active lease
                self._mark_run_stale(run_id)
                self._store_recovery_result(
                    run_id, "opencode_result_parse_failed", exc, execution
                )
                return StartResult(
                    ok=True,
                    run_id=run_id,
                    message="opencode result parsing needs recovery",
                )
        finally:
            heartbeat.stop()
        self._runs[run_id] = poll_result
        self._finalize_run_lease(run_id, execution)
        self._last_run_id = run_id
        return StartResult(ok=True, run_id=run_id, message="started")

    def _resolve_run_id(
        self, task_id: str, task_packet: dict[str, Any]
    ) -> str:
        """Bind the run to the trusted Runner ID, never silently replacing it.

        This mirrors the existing adapters' Runner contract: a trusted
        (``runner_authorization_required``) dispatch must carry the
        pre-allocated ``_agentbc_executor_run_id``, which then flows through
        the lease, the poll result and the final callback receipt.  A controlled
        direct call without Runner authorization generates its own ID and never
        adopts an untrusted identity.
        """
        trusted = task_packet.get("runner_authorization_required") is True
        provided = task_packet.get("_agentbc_executor_run_id")
        if not trusted:
            return f"{OPENCODE_EXECUTOR_ID}-{task_id}-{uuid.uuid4().hex[:8]}"
        if provided is None or (isinstance(provided, str) and not provided.strip()):
            raise ABCError(
                "opencode_run_id_missing",
                "Trusted Runner dispatch requires _agentbc_executor_run_id; "
                "refusing to fabricate a substitute run ID.",
            )
        if not isinstance(provided, str):
            raise ABCError(
                "opencode_run_id_invalid",
                "OpenCode _agentbc_executor_run_id must be a non-empty string.",
            )
        run_id = provided.strip()
        resume_fact = task_packet.get("_agentbc_resume_fact")
        if isinstance(resume_fact, dict):
            resume_run_id = str(resume_fact.get("run_id") or "").strip()
            if resume_run_id and resume_run_id != run_id:
                raise ABCError(
                    "opencode_run_id_conflict",
                    "OpenCode _agentbc_executor_run_id conflicts with the "
                    "Runner resume fact; refusing to pick one silently.",
                )
        return run_id

    def _finalize_run_lease(
        self, run_id: str, execution: dict[str, Any]
    ) -> None:
        """Close the lease only when the owned process is proven stopped.

        A confirmed reap closes the lease.  An unconfirmed termination, a
        missing exit code or an unconfirmed process group keeps the lease in
        the ``stale`` recovery state so nothing claims a still-live writer was
        stopped.
        """
        if self._execution_stopped(execution):
            self._close_run_lease(run_id)
        else:
            self._mark_run_stale(run_id)

    @staticmethod
    def _execution_stopped(execution: dict[str, Any]) -> bool:
        if execution.get("spawn_error"):
            # No child was ever created, so there is nothing left to stop.
            return True
        return execution.get("returncode") is not None and bool(
            execution.get("cleanup_confirmed")
        )

    def _store_recovery_result(
        self,
        run_id: str,
        kind: str,
        exc: Exception,
        execution: dict[str, Any] | None = None,
    ) -> None:
        """Persist an explained recoverable result instead of an orphan lease."""
        execution = execution or {}
        self._runs[run_id] = PollResult(
            status="needs_recovery",
            progress={},
            result={
                "executor": OPENCODE_EXECUTOR_ID,
                "run_id": run_id,
                "transport": OPENCODE_TRANSPORT,
                "returncode": execution.get("returncode"),
                "timed_out": bool(execution.get("timed_out")),
                "cleanup_confirmed": bool(execution.get("cleanup_confirmed")),
                "agent_callback": None,
                "failure": {
                    "kind": kind,
                    "layer": "executor",
                    "message": f"OpenCode {kind}: {exc}",
                    "retryable": True,
                },
                "summary": f"OpenCode {kind}",
            },
        )

    def _reject_unsupported_actions(self, task_packet: dict[str, Any]) -> None:
        for key in _RESUME_FLAGS:
            if task_packet.get(key):
                raise ABCError(
                    "opencode_action_unsupported",
                    f"opencode {key} is not supported by this adapter.",
                )
        extensions = task_packet.get("extensions")
        if isinstance(extensions, dict) and extensions.get("agentbc.resume"):
            raise ABCError(
                "opencode_action_unsupported",
                "OpenCode resume is not supported by this adapter.",
            )

    def _permission_mode(self, task_packet: dict[str, Any]) -> str:
        extensions = task_packet.get("extensions")
        record = permission_record_from_extensions(
            extensions if isinstance(extensions, dict) else None
        )
        mode = str(record.get("effective_mode") or "").strip().lower()
        if mode == "inherit":
            return "inherit"
        if mode == "full":
            return "full"
        if mode == "safe":
            raise ABCError(
                "opencode_permission_mode_unsupported",
                "OpenCode adapter supports only inherit and full; safe mode "
                "requires an approval transport that is not implemented.",
            )
        raise ABCError(
            "opencode_permission_mode_unsupported",
            f"Unsupported OpenCode permission mode: {mode!r}.",
        )

    def _build_command(
        self, project_root: Path, prompt: str, mode: str
    ) -> list[str]:
        """Build the frozen argv; the prompt is exactly one argv element."""
        command = [
            str(self.agent_bin),
            "run",
            "--format",
            "json",
            "--dir",
            str(project_root),
        ]
        if mode == "full":
            command.extend(["--agent", FULL_AGENT])
        if self.model:
            command.extend(["--model", self.model])
        command.extend(["--", prompt])
        return command

    def _build_environment(self, project_root: Path, mode: str) -> dict[str, str]:
        """Build a child-only environment; ``os.environ`` is never mutated."""
        env = dict(os.environ)
        env["PWD"] = str(project_root)
        if mode == "full":
            rules = self._read_official_permission_rules(project_root, env)
            overlay = build_full_permission_overlay(rules)
            env["OPENCODE_CONFIG_CONTENT"] = merge_config_content(
                env.get("OPENCODE_CONFIG_CONTENT"), overlay
            )
            env["OPENCODE_PERMISSION"] = json.dumps(
                overlay["permission"], ensure_ascii=False
            )
        return env

    def _read_official_permission_rules(
        self, project_root: Path, env: dict[str, str]
    ) -> Any:
        if self._official_rules_provider is not None:
            return self._official_rules_provider(project_root, env)
        command = [str(self.agent_bin), "debug", "agent", FULL_AGENT]
        try:
            completed = subprocess.run(
                command,
                cwd=str(project_root),
                env=env,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=OFFICIAL_RULES_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ABCError(
                "opencode_full_rules_unavailable",
                f"Official OpenCode permission rules are unavailable: {exc}",
            ) from exc
        if completed.returncode != 0:
            raise ABCError(
                "opencode_full_rules_unavailable",
                "opencode debug agent "
                f"{FULL_AGENT} exited with {completed.returncode}",
            )
        try:
            payload = json.loads(completed.stdout or "")
        except ValueError as exc:
            raise ABCError(
                "opencode_full_rules_invalid",
                f"Official permission output is not valid JSON: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise ABCError(
                "opencode_full_rules_invalid",
                "Official permission output must be a JSON object.",
            )
        return payload.get("permission")

    # -- process execution ----------------------------------------------

    def _run_command(
        self, argv: list[str], project_root: Path, env: dict[str, str]
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(project_root),
                env=env,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return {
                "spawn_error": str(exc),
                "timed_out": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "stdout_diag": _empty_stream_diag(),
                "stderr_diag": _empty_stream_diag(),
                "output_overflow": False,
                "cleanup_confirmed": True,
                "duration_s": time.monotonic() - started,
            }
        # stdout and stderr are consumed concurrently and incrementally, so the
        # child can never block on a full pipe and no unbounded buffering is
        # required.  The readers are the only consumers, which is what makes a
        # timeout unable to duplicate already-consumed bytes.
        stdout_reader = _BoundedStreamReader(process.stdout, "opencode-stdout")
        stderr_reader = _BoundedStreamReader(process.stderr, "opencode-stderr")
        stdout_reader.start()
        stderr_reader.start()
        timed_out = False
        returncode: int | None = None
        process_confirmed = False
        try:
            process.wait(timeout=self.timeout_s)
            returncode = process.returncode
            # The parent being reaped is not proof the run stopped: a surviving
            # writer in our own process group (often holding the stdout pipe)
            # must be reclaimed or the cleanup stays unconfirmed.
            process_confirmed = returncode is not None and self._reclaim_owned_group(
                process.pid
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode, process_confirmed = self._terminate_owned_process(process)
        finally:
            stdout_reader.join(timeout=TERMINATE_GRACE_S)
            stderr_reader.join(timeout=TERMINATE_GRACE_S)
        readers_ended = not (stdout_reader.is_alive() or stderr_reader.is_alive())
        # An unfinished reader means the captured text is a partial view; it is
        # never delivered as complete output and never confirms cleanup.
        cleanup_confirmed = bool(process_confirmed and readers_ended)
        return {
            "spawn_error": "",
            "timed_out": timed_out,
            "returncode": returncode,
            "stdout": stdout_reader.text(),
            "stderr": stderr_reader.text(),
            "stdout_diag": stdout_reader.diagnostics(),
            "stderr_diag": stderr_reader.diagnostics(),
            "output_overflow": bool(
                stdout_reader.truncated or stderr_reader.truncated
            ),
            "readers_ended": readers_ended,
            "cleanup_confirmed": cleanup_confirmed,
            "duration_s": time.monotonic() - started,
        }

    @staticmethod
    def _reclaim_owned_group(pgid: int) -> bool:
        """Boundedly reclaim survivors of *this run's* own process group.

        Only the exact group created for this adapter run is signalled; no
        unrelated process is scanned or killed and no broader session cleanup
        is attempted.  Returns True once the group is empty.
        """
        if not _process_group_alive(pgid):
            return True
        _kill_owned_group(pgid, signal.SIGTERM)
        if _wait_group_gone(pgid, TERMINATE_GRACE_S):
            return True
        _kill_owned_group(pgid, signal.SIGKILL)
        return _wait_group_gone(pgid, TERMINATE_GRACE_S)

    def _terminate_owned_process(
        self, process: subprocess.Popen[bytes]
    ) -> tuple[int | None, bool]:
        """Reap exactly our own process group and prove the exit.

        TERM is followed by a bounded wait, then KILL.  ``cleanup_confirmed``
        stays False unless the child is actually reaped *and* the owned process
        group is empty, so an unconfirmed stop can never be reported as a clean
        exit and a surviving writer is never mistaken for a stopped one.  The
        KILL fallback is applied to surviving group members even when the
        parent was already reaped.
        """
        pgid = process.pid
        _kill_owned_group(pgid, signal.SIGTERM)
        reaped = self._wait_reaped(process, TERMINATE_GRACE_S)
        if not reaped:
            _kill_owned_group(pgid, signal.SIGKILL)
            reaped = self._wait_reaped(process, TERMINATE_GRACE_S)
        group_gone = self._reclaim_owned_group(pgid)
        return process.returncode, bool(reaped and group_gone)

    @staticmethod
    def _wait_reaped(process: subprocess.Popen[bytes], grace_s: float) -> bool:
        try:
            process.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            return False
        return process.returncode is not None

    # -- result projection ----------------------------------------------

    def _build_poll_result(
        self,
        task_packet: dict[str, Any],
        run_id: str,
        mode: str,
        execution: dict[str, Any],
    ) -> PollResult:
        stdout = execution.get("stdout") or ""
        stderr = execution.get("stderr") or ""
        stdout_diag = execution.get("stdout_diag") or _empty_stream_diag()
        stderr_diag = execution.get("stderr_diag") or _empty_stream_diag()
        output_overflow = bool(execution.get("output_overflow"))
        events, parse_diag = parse_json_lines(stdout)
        protocol = collect_protocol_events(events)
        process_stopped = self._execution_stopped(execution)
        readers_ended = bool(execution.get("readers_ended", True))
        validation = extract_callback_validation_from_output(
            protocol["text"], task_packet, run_id
        )
        validation = self._gate_protocol(
            validation,
            protocol,
            output_overflow,
            parse_diag,
            process_stopped,
            readers_ended,
        )
        returncode = execution.get("returncode")
        failure: dict[str, Any] | None = None
        if execution.get("spawn_error"):
            failure = {
                "kind": "opencode_spawn_failed",
                "layer": "executor",
                "message": "OpenCode process could not be started.",
                "retryable": False,
            }
        elif execution.get("timed_out") and not execution.get("cleanup_confirmed"):
            failure = {
                "kind": "opencode_termination_unconfirmed",
                "layer": "executor",
                "message": "OpenCode timeout termination could not be confirmed.",
                "retryable": True,
            }
        elif execution.get("timed_out"):
            failure = {
                "kind": "opencode_timeout",
                "layer": "executor",
                "message": (
                    f"OpenCode exceeded the {self.timeout_s:g}s safety timeout."
                ),
                "retryable": False,
            }
        elif output_overflow:
            failure = {
                "kind": "opencode_output_limit_exceeded",
                "layer": "executor",
                "message": (
                    "OpenCode output exceeded the bounded capture limit; "
                    "the run needs recovery instead of a fabricated completion."
                ),
                "retryable": True,
            }
        elif returncode == 0 and not execution.get("cleanup_confirmed"):
            failure = {
                "kind": "opencode_process_unstopped",
                "layer": "executor",
                "message": (
                    "OpenCode exited but an owned process or output reader did "
                    "not stop; the run needs recovery instead of a fabricated "
                    "completion."
                ),
                "retryable": True,
            }
        elif returncode not in (0, None):
            transport = detect_retryable_transport_failure(stdout, stderr)
            if transport is not None:
                failure = {
                    "kind": "executor_transport_failure",
                    "layer": "transport",
                    "message": "OpenCode transport failure detected.",
                    "retryable": True,
                }
            else:
                failure = {
                    "kind": "opencode_exit_nonzero",
                    "layer": "executor",
                    "message": f"opencode exited with {returncode}",
                    "retryable": False,
                }
        elif returncode is None:
            failure = {
                "kind": "opencode_exit_unconfirmed",
                "layer": "executor",
                "message": "OpenCode exit could not be confirmed.",
                "retryable": True,
            }
        terminal = route_executor_terminal(
            validation,
            1 if returncode is None else int(returncode),
            executor_name=OPENCODE_EXECUTOR_ID,
            runtime_failure=failure,
        )
        steps_total = len(task_packet.get("steps") or [])
        result = {
            "executor": OPENCODE_EXECUTOR_ID,
            "run_id": run_id,
            "transport": OPENCODE_TRANSPORT,
            "permission_mode": mode,
            "session_id": protocol["session_id"],
            "session_ids": protocol["main_session_ids"],
            "session_ambiguous": protocol["session_ambiguous"],
            "main_session_count": protocol["main_session_count"],
            "child_session_count": len(protocol["child_session_ids"]),
            "returncode": returncode,
            "timed_out": bool(execution.get("timed_out")),
            "cleanup_confirmed": bool(execution.get("cleanup_confirmed")),
            "process_stopped": process_stopped,
            "readers_ended": readers_ended,
            "duration_s": round(float(execution.get("duration_s") or 0.0), 3),
            "event_types": protocol["event_types"],
            "event_type_counts": protocol["event_type_counts"],
            "events_seen": len(events),
            "text_parts": protocol["text_parts"],
            "final_marker_count": protocol["final_marker_count"],
            "tool_calls": protocol["tool_calls"][:MAX_TOOL_DIAGNOSTICS],
            "tool_error_count": protocol["tool_error_count"],
            "unrecovered_tool_error_count": protocol[
                "unrecovered_tool_error_count"
            ],
            "permission_rejected": protocol["permission_rejected"],
            "unrecovered_permission_rejected": protocol[
                "unrecovered_permission_rejected"
            ],
            "step_finishes": protocol["step_finishes"],
            "stop_finish_seen": protocol["stop_finish_seen"],
            "final_stop_seen": protocol["final_stop_seen"],
            "recovered_tool_error_count": protocol["recovered_tool_error_count"],
            "auto_rejected": protocol["permission_rejected"],
            "parse_diagnostics": parse_diag,
            "stdout_diagnostics": stdout_diag,
            "stderr_diagnostics": stderr_diag,
            "output_overflow": output_overflow,
            "stdout_truncated": bool(stdout_diag.get("truncated")),
            "stderr_truncated": bool(stderr_diag.get("truncated")),
            "stdout_captured_chars": int(stdout_diag.get("captured_chars") or 0),
            "stderr_captured_chars": int(stderr_diag.get("captured_chars") or 0),
            "stdout_bytes": int(stdout_diag.get("bytes") or 0),
            "stderr_bytes": int(stderr_diag.get("bytes") or 0),
            "summary": self._summary_for(terminal, validation),
            "agent_callback": terminal.callback,
            "failure": terminal.failure,
        }
        return PollResult(
            status=terminal.status,
            progress={
                "events_seen": len(events),
                "tool_calls": len(protocol["tool_calls"]),
                "text_parts": protocol["text_parts"],
                "steps_total": steps_total,
            },
            result=result,
        )

    @staticmethod
    def _gate_protocol(
        validation: CallbackValidation,
        protocol: dict[str, Any],
        output_overflow: bool,
        parse_diagnostics: dict[str, Any],
        process_stopped: bool,
        readers_ended: bool,
    ) -> CallbackValidation:
        """Fail closed when the OpenCode protocol does not support the callback.

        A callback is only actionable when the stream bound exactly one
        official main session.  A ``completed`` callback additionally requires
        the final response's *own* normal ``stop`` finish boundary, an
        uncorrupted stream tail, no unrecovered tool error, finished output
        readers and a fully stopped owned process group.  The shared
        three-agent terminal priority is never consulted here, and no callback
        is ever fabricated to compensate.
        """
        if not validation.valid or validation.callback is None:
            return validation
        reasons: list[tuple[str, str]] = []
        if protocol["main_session_count"] != 1:
            reasons.append(
                (
                    "opencode_session_ambiguous",
                    "OpenCode stream did not bind exactly one official main "
                    "session; the callback cannot be trusted as completed.",
                )
            )
        if output_overflow:
            reasons.append(
                (
                    "opencode_output_limit_exceeded",
                    "OpenCode output exceeded the bounded capture limit.",
                )
            )
        if str(validation.callback.get("final_state") or "") == "completed":
            malformed = int(parse_diagnostics.get("malformed_lines") or 0)
            oversized = int(parse_diagnostics.get("oversized_lines") or 0)
            if malformed or oversized:
                reasons.append(
                    (
                        "opencode_stream_corrupt",
                        "OpenCode stream carries malformed or oversized JSON "
                        "after the final response; the corrupted tail cannot be "
                        "silently ignored to claim completion.",
                    )
                )
            if not protocol["final_stop_seen"]:
                reasons.append(
                    (
                        "opencode_stop_boundary_missing",
                        "OpenCode final response is not closed by its own normal "
                        "'stop' step_finish; a historical stop or a 'tool-calls' "
                        "finish is not a final success.",
                    )
                )
            if protocol["unrecovered_tool_error_count"] > 0:
                reasons.append(
                    (
                        "opencode_tool_error_unrecovered",
                        "OpenCode stream ends with an unrecovered tool error; "
                        "the run cannot be reported as completed.",
                    )
                )
            if not readers_ended:
                reasons.append(
                    (
                        "opencode_reader_incomplete",
                        "OpenCode left an unfinished output reader; partial "
                        "output is not a complete delivery.",
                    )
                )
            if not process_stopped:
                reasons.append(
                    (
                        "opencode_process_unstopped",
                        "OpenCode exited but an owned process is still running; "
                        "cleanup is not confirmed.",
                    )
                )
        if not reasons:
            return validation
        code, message = reasons[0]
        return CallbackValidation(
            marker_seen=True, valid=False, code=code, message=message
        )

    @staticmethod
    def _summary_for(terminal: Any, validation: Any) -> str:
        if terminal.callback is not None:
            return str(terminal.callback.get("summary") or "")
        if terminal.failure is not None:
            return str(terminal.failure.get("message") or "")
        return str(getattr(validation, "message", "") or "")
