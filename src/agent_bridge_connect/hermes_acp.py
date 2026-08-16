"""Narrow Hermes ACP session-first transport (PERM-103-003 / PERM-103-004).

This module is the Task 6 Hermes control path: it speaks the official Agent
Client Protocol (ACP) over stdio to a spawned ``hermes acp`` process through
the configured executable, and it bridges ``session/request_permission`` into
the executor-neutral approval receipt and ControlPlane **without changing
those public interfaces**.

Contract invariants (fail closed):

* Session-first ordering: the official ``session/new`` (fresh) or explicit
  ``session/load`` (resume) must complete and be persisted as the
  task/run-bound official session receipt **before** ``session/prompt`` is
  ever sent.  The transport itself never persists anything - it returns the
  official session ID so the adapter can persist the receipt through the
  frozen ``SessionFirstGate`` before calling :meth:`HermesAcpTransport.prompt`.
* No private scanning: the transport never reads Hermes databases, logs, or
  process tables, never uses ``--last`` / ``--continue`` / ``--accept-hooks``
  / ``--yolo`` flags, and never overrides global configuration.  Full mode is
  expressed exclusively through the registry-frozen subprocess-scoped
  ``HERMES_YOLO_MODE`` environment on the spawned ACP subprocess.
* Strict framing: every stdin/stdout frame must be a JSON object; malformed,
  duplicate, or out-of-order frames fail closed before any unsafe execution.
* Strict versioning: ``initialize`` must return exactly the supported
  protocol version (``1``); anything else is ``hermes_acp_unsupported_version``.
* Strict permission surface: the bridge exposes **only** ``allow_once`` and
  ``deny`` (``cancelled``) outcomes to AgentBC.  Requests whose option list
  cannot express a one-time approval, requests for a different session,
  duplicate/concurrent requests, mismatched identities, and late responses
  fail closed.  ``allow_session``, ``allow_always`` and ``deny_always`` are
  never selected and never returned.
* Transport death (broken pipe, process exit, EOF, timeout) surfaces as an
  explicit failure so the adapter can enter ``needs_recovery``; nothing is
  retried silently.

The module is deliberately dependency-free (AgentBC has no runtime
dependencies) and mirrors the official ``agent-client-protocol`` wire shapes
with camelCase field aliases.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Official ACP protocol version supported by this transport (the installed
# ``agent-client-protocol`` SDK pins ``PROTOCOL_VERSION = 1``).
HERMES_ACP_PROTOCOL_VERSION = 1
HERMES_ACP_CLIENT_NAME = "agentbc"
HERMES_ACP_CLIENT_VERSION = "1.0.3A"

# The two exact native outcomes AgentBC may return to a Hermes ACP
# ``session/request_permission`` request.  ``allow_once`` authorizes exactly
# one action; ``cancelled`` denies.  No session/always grant is ever issued.
HERMES_ACP_ALLOW_ONCE_OPTION = "allow_once"
HERMES_ACP_DENIED_OUTCOME = "cancelled"

# Frozen capability id (registry) bound by this transport.
HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID = "hermes.acp.session.request_permission"

# Default per-RPC bounds.  The whole prompt turn is bounded by the adapter's
# safety runtime instead of this value.
HERMES_ACP_RPC_TIMEOUT_S = 30.0
_HERMES_ACP_START_TIMEOUT_S = 60.0

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_TOOL_CALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_SUMMARY_LIMIT = 240
_STDERR_KEEP_BYTES = 8192

_JSONRPC = "2.0"


class HermesAcpError(RuntimeError):
    """Fail-closed transport error with a stable code and bounded evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "hermes_acp_error")
        self.details = dict(details or {})
        super().__init__(message)

    def __str__(self) -> str:
        # The stable code prefixes the human message so failure evidence and
        # audit trails always carry the exact fail-closed reason.
        return f"{self.code}: {super().__str__()}"


class HermesAcpUnsupported(HermesAcpError):
    """The installed Hermes ACP surface cannot express this contract."""


def _bounded_text(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    text = "" if value is None else str(value)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
    if len(cleaned) > limit:
        return cleaned[:limit]
    return cleaned


def _session_identifier(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or _SESSION_ID_RE.fullmatch(normalized) is None:
        raise HermesAcpError(
            "hermes_acp_invalid_identifier",
            f"ACP {field} is not a safe non-empty identifier.",
            {field: _bounded_text(value, 80)},
        )
    return normalized


def _tool_call_identifier(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or _TOOL_CALL_ID_RE.fullmatch(normalized) is None:
        raise HermesAcpError(
            "hermes_acp_invalid_tool_call_id",
            "ACP permission tool call ID is not a safe identifier.",
            {"tool_call_id": _bounded_text(value, 80)},
        )
    return normalized


def validate_initialize_result(result: Any) -> int:
    """Validate the ``initialize`` response, fail closed on version drift."""
    if not isinstance(result, dict):
        raise HermesAcpUnsupported(
            "hermes_acp_unsupported_version",
            "Hermes ACP initialize response is not an object.",
            {"result_kind": type(result).__name__},
        )
    version = result.get("protocolVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        raise HermesAcpUnsupported(
            "hermes_acp_unsupported_version",
            "Hermes ACP initialize response has no numeric protocol version.",
            {"protocol_version": _bounded_text(version, 40)},
        )
    if version != HERMES_ACP_PROTOCOL_VERSION:
        raise HermesAcpUnsupported(
            "hermes_acp_unsupported_version",
            f"Hermes ACP protocol version {version} is unsupported.",
            {
                "protocol_version": version,
                "supported_protocol_version": HERMES_ACP_PROTOCOL_VERSION,
            },
        )
    return version


def validate_session_id(value: Any) -> str:
    """Validate an official ACP session id, fail closed."""
    return _session_identifier(value, field="session_id")


def permission_request_options(options: Any) -> tuple[bool, list[str]]:
    """Return whether ``allow_once`` is offered and the offered option ids.

    ``allow_session``, ``allow_always`` and ``deny_always`` may be offered by
    the agent; AgentBC never selects them.  A request that cannot express a
    one-time approval (no ``allow_once`` option) is unsupported.
    """
    if not isinstance(options, list):
        return False, []
    offered: list[str] = []
    has_allow_once = False
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("optionId") or option.get("option_id") or "").strip()
        if not option_id:
            continue
        offered.append(option_id)
        if option_id == HERMES_ACP_ALLOW_ONCE_OPTION:
            has_allow_once = True
    return has_allow_once, offered


def validate_permission_request(
    frame: dict[str, Any],
    *,
    session_id: str,
) -> tuple[str, dict[str, Any]]:
    """Validate one inbound ``session/request_permission`` request.

    Returns ``(json_rpc_id, tool_call)``.  Raises :class:`HermesAcpError` for
    malformed frames, requests bound to a different official session, missing
    tool-call identity, or option lists that cannot express ``allow_once``.
    """
    if not isinstance(frame, dict):
        raise HermesAcpError(
            "hermes_acp_malformed_frame",
            "ACP permission frame is not an object.",
        )
    if frame.get("method") != "session/request_permission":
        raise HermesAcpError(
            "hermes_acp_permission_method_invalid",
            "ACP permission frame uses an unsupported method.",
            {"method": _bounded_text(frame.get("method"), 80)},
        )
    if "id" not in frame or frame.get("id") is None:
        raise HermesAcpError(
            "hermes_acp_permission_id_missing",
            "ACP permission request has no JSON-RPC request ID.",
        )
    params = frame.get("params")
    if not isinstance(params, dict):
        raise HermesAcpError(
            "hermes_acp_permission_params_invalid",
            "ACP permission request has no params object.",
        )
    actual_session = str(params.get("sessionId") or params.get("session_id") or "").strip()
    if not actual_session or actual_session != str(session_id or "").strip():
        raise HermesAcpError(
            "hermes_acp_permission_session_mismatch",
            "ACP permission request is bound to a different official session.",
            {
                "expected_session_id": _bounded_text(session_id, 80),
                "actual_session_id": _bounded_text(actual_session, 80),
            },
        )
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        raise HermesAcpError(
            "hermes_acp_permission_tool_call_missing",
            "ACP permission request has no tool call details.",
        )
    _tool_call_identifier(tool_call.get("id") or tool_call.get("tool_call_id"))
    has_allow_once, offered = permission_request_options(params.get("options"))
    if not has_allow_once:
        raise HermesAcpError(
            "hermes_acp_permission_options_unsupported",
            "ACP permission request cannot express a one-time approval.",
            {"offered_options": offered[:16]},
        )
    return frame["id"], tool_call


def permission_summary(tool_call: dict[str, Any]) -> str:
    """Return a sanitized, bounded one-line summary for one ACP tool call.

    The summary is derived only from the structured tool-call title and
    description; it is bounded and control-character-free and never includes
    raw session content, tokens, or secrets.
    """
    title = _bounded_text(tool_call.get("title"))
    description = _bounded_text(tool_call.get("description"))
    if title and description and title != description:
        return _bounded_text(f"{description}: {title}")
    return title or description or "Hermes terminal action"


def build_approval_message(
    frame: dict[str, Any],
    *,
    task_id: str,
    executor_run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Translate an ACP permission request into the frozen control-plane shape.

    The executor-neutral :class:`ApprovalControlPlane` accepts the Codex
    App-Server approval message shape; the transport preserves that public
    interface and only translates the wire format.  ``threadId`` is the
    official ACP session id so the plane's session-first gate binds the
    request to the exact persisted receipt.
    """
    request_id, tool_call = validate_permission_request(
        frame,
        session_id=session_id,
    )
    summary = permission_summary(tool_call)
    return {
        "jsonrpc": _JSONRPC,
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": str(session_id).strip(),
            "turnId": "",
            "itemId": _tool_call_identifier(tool_call.get("id") or tool_call.get("tool_call_id")),
            "reason": summary,
        },
        "_agentbc": {
            "task_id": str(task_id or "").strip(),
            "executor_run_id": str(executor_run_id or "").strip(),
        },
    }


def approval_outcome_for_decision(decision: Any) -> dict[str, Any]:
    """Map one ControlPlane decision to the exact ACP permission outcome.

    ``accept`` maps to ``allow_once`` (one action only); ``decline`` maps to
    ``cancelled``.  Any other value raises so a malformed decision can never
    be forwarded as a grant.
    """
    selected = str(decision or "").strip().lower()
    if selected == "accept":
        return {"outcome": {"optionId": HERMES_ACP_ALLOW_ONCE_OPTION}}
    if selected == "decline":
        return {"outcome": {"outcome": HERMES_ACP_DENIED_OUTCOME}}
    raise HermesAcpError(
        "hermes_acp_approval_decision_invalid",
        "Only the exact allow_once and deny outcomes may be returned to Hermes ACP.",
        {"decision": _bounded_text(decision, 40)},
    )


def frame_kind(frame: Any) -> str:
    """Classify one parsed frame, fail closed on malformed shapes.

    Returns ``response``, ``request``, or ``notification``.  A frame that is
    not a JSON object, or mixes response and request fields, is malformed.
    """
    if not isinstance(frame, dict):
        raise HermesAcpError(
            "hermes_acp_malformed_frame",
            "ACP frame is not a JSON object.",
            {"frame_kind": type(frame).__name__},
        )
    has_id = "id" in frame
    has_method = isinstance(frame.get("method"), str)
    has_result = "result" in frame
    has_error = isinstance(frame.get("error"), dict)
    if has_id and has_method and (has_result or has_error):
        # A frame that is simultaneously a request and a response is never
        # valid on the wire; fail closed instead of guessing intent.
        raise HermesAcpError(
            "hermes_acp_malformed_frame",
            "ACP frame mixes request and response fields.",
            {"keys": sorted(str(key) for key in frame.keys())[:16]},
        )
    if has_id and (has_result or has_error):
        return "response"
    if has_id and has_method:
        return "request"
    if not has_id and has_method:
        return "notification"
    raise HermesAcpError(
        "hermes_acp_malformed_frame",
        "ACP frame mixes or omits response/request fields.",
        {"keys": sorted(str(key) for key in frame.keys())[:16]},
    )


class HermesAcpTransport:
    """Dependency-free ACP stdio client for one spawned ``hermes acp`` process.

    The transport owns one subprocess and one JSON-RPC stream.  It never
    scans Hermes state, never mutates global configuration, and applies only
    the caller-provided subprocess-scoped environment (the registry-frozen
    full-mode override).  All receive paths are bounded by explicit timeouts.
    """

    def __init__(
        self,
        executable: str | Path,
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
        rpc_timeout_s: float = HERMES_ACP_RPC_TIMEOUT_S,
        start_timeout_s: float = _HERMES_ACP_START_TIMEOUT_S,
    ) -> None:
        self.executable = str(executable)
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.command = list(command or [self.executable, "acp"])
        self.env = dict(env or {})
        self.rpc_timeout_s = max(float(rpc_timeout_s), 0.1)
        self.start_timeout_s = max(float(start_timeout_s), 0.1)
        self.process: subprocess.Popen[str] | None = None
        self._send_lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        self._message_chunks: list[str] = []
        self._message_bytes = 0
        self._closed = False

    # ---- process lifecycle -------------------------------------------------

    def start(self) -> None:
        """Spawn the ACP subprocess; raise :class:`HermesAcpError` on failure."""
        if self.process is not None:
            return
        try:
            process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise HermesAcpError(
                "hermes_acp_start_failed",
                f"Failed to start hermes acp: {exc}",
            ) from exc
        self.process = process
        self._stderr_tail = []
        drain = threading.Thread(
            target=self._drain_stderr,
            name="agentbc-hermes-acp-stderr",
            daemon=True,
        )
        drain.start()

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                with self._stderr_lock:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 32:
                        del self._stderr_tail[:-32]
        except (OSError, ValueError):
            pass

    def stderr_evidence(self) -> str:
        """Return bounded stderr tail for failure evidence (never parsed for ids)."""
        with self._stderr_lock:
            joined = "".join(self._stderr_tail)
        if len(joined.encode("utf-8", errors="replace")) > _STDERR_KEEP_BYTES:
            return joined[-_STDERR_KEEP_BYTES:]
        return joined

    def _collect_message_chunks(self, frame: dict[str, Any]) -> None:
        """Accumulate bounded assistant text from ``session/update`` frames.

        Only plain text blocks of ``agent_message_chunk`` updates are kept,
        with a hard byte cap, so the adapter can run the standard final
        callback extraction over the turn's assistant output.
        """
        if self._message_bytes >= _STDERR_KEEP_BYTES:
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            return
        update = params.get("sessionUpdate")
        if not isinstance(update, dict):
            return
        if update.get("type") not in {"agent_message_chunk", "agent_message"}:
            return
        message = update.get("message")
        if not isinstance(message, dict) or str(message.get("role") or "") != "assistant":
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text") or "")
            if not text:
                continue
            encoded = text.encode("utf-8", errors="replace")
            if self._message_bytes + len(encoded) > _STDERR_KEEP_BYTES:
                remaining = max(_STDERR_KEEP_BYTES - self._message_bytes, 0)
                text = encoded[:remaining].decode("utf-8", errors="replace")
                self._message_chunks.append(text)
                self._message_bytes = _STDERR_KEEP_BYTES
                return
            self._message_chunks.append(text)
            self._message_bytes += len(encoded)

    def message_text(self) -> str:
        """Return the bounded accumulated assistant text for the current turn."""
        return "".join(self._message_chunks)

    def is_alive(self) -> bool:
        process = self.process
        if process is None:
            return False
        return process.poll() is None

    def close(self) -> None:
        """Terminate the ACP subprocess and close the stream, idempotently."""
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        self.process = None

    # ---- framing -----------------------------------------------------------

    def _send(self, frame: dict[str, Any]) -> None:
        if self._closed or self.process is None or self.process.stdin is None:
            raise HermesAcpError(
                "hermes_acp_transport_closed",
                "Hermes ACP transport is not started or already closed.",
            )
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        with self._send_lock:
            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise HermesAcpError(
                    "hermes_acp_broken_pipe",
                    "Hermes ACP stdin closed before the request completed.",
                ) from exc

    def _recv_frame(self, timeout_s: float) -> dict[str, Any]:
        if self._closed or self.process is None or self.process.stdout is None:
            raise HermesAcpError(
                "hermes_acp_transport_closed",
                "Hermes ACP transport is not started or already closed.",
            )
        stream = self.process.stdout
        deadline = time.monotonic() + max(float(timeout_s), 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Hermes ACP receive timed out without a complete frame."
                )
            try:
                ready, _, _ = select.select([stream], [], [], min(remaining, 1.0))
            except (OSError, ValueError) as exc:
                raise HermesAcpError(
                    "hermes_acp_transport_closed",
                    f"Hermes ACP stdout is unavailable: {exc}",
                ) from exc
            if not ready:
                if self.process.poll() is not None:
                    raise HermesAcpError(
                        "hermes_acp_transport_exited",
                        "Hermes ACP process exited before the frame completed.",
                        {"exit_code": self.process.returncode},
                    )
                continue
            line = stream.readline()
            if line == "":
                raise HermesAcpError(
                    "hermes_acp_transport_exited",
                    "Hermes ACP process closed stdout before the frame completed.",
                    {"exit_code": self.process.returncode if self.process is not None else None},
                )
            if not line.endswith("\n"):
                raise HermesAcpError(
                    "hermes_acp_malformed_frame",
                    "Hermes ACP emitted an unterminated frame.",
                )
            stripped = line.strip()
            if not stripped:
                continue
            try:
                frame = json.loads(stripped)
            except (ValueError, json.JSONDecodeError) as exc:
                raise HermesAcpError(
                    "hermes_acp_malformed_frame",
                    "Hermes ACP emitted a malformed JSON frame.",
                    {"line": _bounded_text(stripped, 120)},
                ) from exc
            kind = frame_kind(frame)
            if kind == "notification" and frame.get("method") == "session/update":
                self._collect_message_chunks(frame)
            return frame

    # ---- RPC helpers -------------------------------------------------------

    def _request(self, method: str, params: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        request_id = f"agentbc-{method.replace('/', '-')}-{os.getpid()}-{time.monotonic_ns()}"
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + max(float(timeout_s), 0.1)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Hermes ACP {method} timed out.")
            frame = self._recv_frame(remaining)
            kind = frame_kind(frame)
            if kind == "request":
                if frame.get("method") == "session/request_permission":
                    raise HermesAcpError(
                        "hermes_acp_permission_out_of_order",
                        "Hermes ACP requested permission before the session handshake completed.",
                    )
                self._respond_error(frame.get("id"), -32601, "Method not found")
                continue
            if kind == "notification":
                continue
            if frame.get("id") != request_id:
                raise HermesAcpError(
                    "hermes_acp_response_mismatch",
                    "Hermes ACP responded to an unknown request.",
                    {"expected_id": _bounded_text(request_id, 80)},
                )
            error = frame.get("error")
            if isinstance(error, dict):
                raise HermesAcpError(
                    "hermes_acp_rpc_error",
                    "Hermes ACP rejected the request.",
                    {
                        "method": method,
                        "code": error.get("code"),
                        "message": _bounded_text(error.get("message"), 160),
                    },
                )
            result = frame.get("result")
            if not isinstance(result, dict):
                raise HermesAcpError(
                    "hermes_acp_rpc_result_invalid",
                    f"Hermes ACP {method} returned a non-object result.",
                    {"method": method},
                )
            return result

    def _respond(self, request_id: Any, result: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": _JSONRPC, "id": request_id}
        if result is not None:
            frame["result"] = result
        else:
            frame["result"] = None
        self._send(frame)

    def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    # ---- protocol surface --------------------------------------------------

    def initialize(self, timeout_s: float | None = None) -> dict[str, Any]:
        """Run the ACP initialize handshake and return the validated result.

        Sends ``initialize``, validates the negotiated protocol version, then
        sends the ``notifications/initialized`` notification.
        """
        result = self._request(
            "initialize",
            {
                "protocolVersion": HERMES_ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": HERMES_ACP_CLIENT_NAME,
                    "version": HERMES_ACP_CLIENT_VERSION,
                },
            },
            timeout_s=timeout_s if timeout_s is not None else self.rpc_timeout_s,
        )
        validate_initialize_result(result)
        self._send({"jsonrpc": _JSONRPC, "method": "notifications/initialized"})
        return result

    def new_session(self, cwd: str, timeout_s: float | None = None) -> str:
        """Create a fresh official ACP session and return its session id."""
        result = self._request(
            "session/new",
            {"cwd": str(cwd), "mcpServers": []},
            timeout_s=timeout_s if timeout_s is not None else self.rpc_timeout_s,
        )
        session_id = validate_session_id(result.get("sessionId") or result.get("session_id"))
        return session_id

    def load_session(self, cwd: str, session_id: str, timeout_s: float | None = None) -> str:
        """Explicitly load one persisted official session (resume).

        The server returns an error or a null result when the session is
        unknown; both fail closed.  The official session id is the explicit
        id the caller requested - never a guessed or scanned id.
        """
        exact = validate_session_id(session_id)
        self._request(
            "session/load",
            {"cwd": str(cwd), "sessionId": exact, "mcpServers": []},
            timeout_s=timeout_s if timeout_s is not None else self.rpc_timeout_s,
        )
        return exact

    def prompt(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        on_permission: Callable[[dict[str, Any]], dict[str, Any]],
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send one prompt turn and run the event loop until the response.

        ``on_permission`` receives each validated ``session/request_permission``
        request and must return the exact outcome dict (``allow_once`` /
        ``cancelled``); any exception it raises aborts the turn fail closed.
        Returns the response result (``stopReason`` ...).
        """
        exact = validate_session_id(session_id)
        request_id = f"agentbc-prompt-{os.getpid()}-{time.monotonic_ns()}"
        deadline = time.monotonic() + (
            max(float(timeout_s), 0.1) if timeout_s is not None else 86400.0 * 7
        )
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "id": request_id,
                "method": "session/prompt",
                "params": {"sessionId": exact, "prompt": blocks},
            }
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes ACP prompt turn exceeded the safety runtime.")
            frame = self._recv_frame(min(remaining, self.rpc_timeout_s))
            kind = frame_kind(frame)
            if kind == "request":
                method = str(frame.get("method") or "")
                if method == "session/request_permission":
                    # Validate before invoking the callback so malformed,
                    # cross-session, or option-unsupported requests fail
                    # closed before any decision can be returned.
                    validate_permission_request(frame, session_id=exact)
                    outcome = on_permission(frame)
                    self._respond(frame.get("id"), outcome)
                    continue
                if method in {"session/cancel", "session/close"}:
                    self._respond_error(
                        frame.get("id"),
                        -32601,
                        f"Unsupported request: {method}",
                    )
                    continue
                self._respond_error(frame.get("id"), -32601, "Method not found")
                continue
            if kind == "notification":
                continue
            if frame.get("id") != request_id:
                raise HermesAcpError(
                    "hermes_acp_response_mismatch",
                    "Hermes ACP responded to an unknown prompt request.",
                )
            error = frame.get("error")
            if isinstance(error, dict):
                raise HermesAcpError(
                    "hermes_acp_rpc_error",
                    "Hermes ACP rejected the prompt.",
                    {
                        "code": error.get("code"),
                        "message": _bounded_text(error.get("message"), 160),
                    },
                )
            result = frame.get("result")
            if not isinstance(result, dict):
                raise HermesAcpError(
                    "hermes_acp_rpc_result_invalid",
                    "Hermes ACP prompt returned a non-object result.",
                )
            return result

    def respond_permission(self, request_id: Any, outcome: dict[str, Any]) -> None:
        """Answer one permission request with the exact validated outcome."""
        self._respond(request_id, outcome)

    def cancel_session(self, session_id: str) -> None:
        """Send the official ``session/cancel`` notification for one session."""
        exact = validate_session_id(session_id)
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "method": "session/cancel",
                "params": {"sessionId": exact},
            }
        )


__all__ = [
    "HERMES_ACP_ALLOW_ONCE_OPTION",
    "HERMES_ACP_CLIENT_NAME",
    "HERMES_ACP_CLIENT_VERSION",
    "HERMES_ACP_DENIED_OUTCOME",
    "HERMES_ACP_PROTOCOL_VERSION",
    "HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID",
    "HERMES_ACP_RPC_TIMEOUT_S",
    "HermesAcpError",
    "HermesAcpTransport",
    "HermesAcpUnsupported",
    "approval_outcome_for_decision",
    "build_approval_message",
    "frame_kind",
    "permission_request_options",
    "permission_summary",
    "validate_initialize_result",
    "validate_permission_request",
    "validate_session_id",
]
