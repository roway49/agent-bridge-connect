"""In-memory broker for the official Codex Desktop App Tools MCP route.

The route is deliberately a narrow, transient capability.  AgentBC receives
the pipe and MCP context from the current Codex Desktop process, negotiates the
bundled ``set_thread_archived`` tool, and keeps only bounded digests after the
call.  No socket path, MCP argument, Desktop database value, or session index
is written to a task record or returned to a caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .control import TransportClosed

CODEX_APP_TOOLS_PIPE_PATH_ENV = "CODEX_APP_TOOLS_PIPE_PATH"
CODEX_THREAD_ID_ENV = "CODEX_THREAD_ID"
CODEX_MCP_NODE_PATH_ENV = "CODEX_MCP_NODE_PATH"
AGENTBC_DESKTOP_RELAY_SOCKET_ENV = "AGENTBC_DESKTOP_RELAY_SOCKET"
AGENTBC_DESKTOP_RELAY_TOKEN_ENV = "AGENTBC_DESKTOP_RELAY_TOKEN"

_APP_TOOLS_RESOURCE = Path(
    "plugins/openai-bundled/plugins/codex-app-tools/server.mjs"
)

CODEX_DESKTOP_ARCHIVE_TOOL = "set_thread_archived"
CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE = "codex_desktop_archive_route_unavailable"
CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST = "codex_desktop_archive_transport_lost"
CODEX_DESKTOP_ARCHIVE_REJECTED = "codex_desktop_archive_rejected"
CODEX_DESKTOP_ARCHIVE_UNSUPPORTED = "codex_desktop_archive_unsupported"

_MAX_CONTEXT_VALUE = 4096
_MAX_RESPONSE_BYTES = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()[:24]}"


def _bounded_context_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > _MAX_CONTEXT_VALUE:
        return ""
    return text


def _canonical_uuid(value: Any) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = uuid.UUID(candidate)
    except (AttributeError, ValueError):
        return ""
    if str(parsed) != candidate.lower():
        return ""
    return candidate.lower()


@dataclass(frozen=True)
class CodexDesktopRouteContext:
    """Transient current-process context used to register one Desktop route."""

    pipe_path: str = field(repr=False)
    dispatcher_thread_id: str = field(repr=False)
    mcp_runtime: str = field(repr=False)
    mcp_resource: str = field(repr=False)
    relay_socket: str = field(default="", repr=False)
    relay_token: str = field(default="", repr=False)
    host: str = field(default_factory=socket.gethostname, repr=False)

    @property
    def route_digest(self) -> str:
        return _digest(
            {
                "pipe_path": self.pipe_path,
                "dispatcher_thread_id": self.dispatcher_thread_id,
                "host": self.host,
                "runtime": self.mcp_runtime,
                "resource": self.mcp_resource,
                "relay": bool(self.relay_socket),
            },
            prefix="route_",
        )

    @property
    def app_instance_digest(self) -> str:
        return _digest(
            {
                "dispatcher_thread_id": self.dispatcher_thread_id,
                "host": self.host,
                "runtime": self.mcp_runtime,
                "resource": self.mcp_resource,
            },
            prefix="app_",
        )


def read_desktop_route_context(
    env: Mapping[str, str] | None = None,
) -> CodexDesktopRouteContext | None:
    """Read only mechanically supplied Desktop context from the environment.

    Codex Desktop currently supplies the native pipe, dispatcher thread and
    bundled Node runtime mechanically.  The App Tools MCP resource is derived
    from that runtime's application Resources root; AgentBC does not require
    invented environment variables or a version allow-list.
    """
    source = env if env is not None else os.environ
    pipe_path = _bounded_context_value(source.get(CODEX_APP_TOOLS_PIPE_PATH_ENV))
    dispatcher_id = _canonical_uuid(source.get(CODEX_THREAD_ID_ENV))
    if not pipe_path or not Path(pipe_path).is_absolute() or not dispatcher_id:
        return None

    runtime = _bounded_context_value(source.get(CODEX_MCP_NODE_PATH_ENV))
    resource = _app_tools_resource_for_runtime(runtime)
    if not runtime or not resource:
        return None
    return CodexDesktopRouteContext(
        pipe_path=pipe_path,
        dispatcher_thread_id=dispatcher_id,
        mcp_runtime=runtime,
        mcp_resource=resource,
        relay_socket=_bounded_context_value(source.get(AGENTBC_DESKTOP_RELAY_SOCKET_ENV)),
        relay_token=_bounded_context_value(source.get(AGENTBC_DESKTOP_RELAY_TOKEN_ENV)),
        host=socket.gethostname(),
    )


def _app_tools_resource_for_runtime(runtime: str) -> str:
    """Derive the bundled App Tools server from a Desktop-owned Node path."""
    value = _bounded_context_value(runtime)
    if not value:
        return ""
    node = Path(value).expanduser()
    if not node.is_absolute():
        return ""
    for parent in node.parents:
        if parent.name != "Resources":
            continue
        return str(parent / _APP_TOOLS_RESOURCE)
    return ""


def _route_context_paths_valid(
    context: CodexDesktopRouteContext,
    *,
    require_installed: bool,
) -> bool:
    runtime = Path(context.mcp_runtime).expanduser()
    resource = Path(context.mcp_resource).expanduser()
    expected = _app_tools_resource_for_runtime(str(runtime))
    if (
        not runtime.is_absolute()
        or not resource.is_absolute()
        or not expected
        or resource != Path(expected)
    ):
        return False
    if require_installed and (
        not runtime.is_file()
        or not os.access(runtime, os.X_OK)
        or not resource.is_file()
    ):
        return False
    return True


@dataclass(frozen=True)
class CodexDesktopArchiveResult:
    status: str
    error_code: str = ""
    checked_at: str = ""
    request_digest: str = ""
    route_digest: str = ""
    app_instance_digest: str = ""

    @property
    def acknowledged(self) -> bool:
        return self.status == "acknowledged" and not self.error_code

    def public(self) -> dict[str, str]:
        return {
            "status": self.status,
            "error_code": self.error_code,
            "checked_at": self.checked_at,
            "request_digest": self.request_digest,
            "route_digest": self.route_digest,
            "app_instance_digest": self.app_instance_digest,
        }


class _StdioMcpTransport:
    """Run Codex's bundled MCP facade and let it own native pipe framing.

    The native Desktop socket is *not* an MCP socket: it uses a private framed
    host protocol.  The bundled facade is the supported adapter from newline
    delimited MCP stdio to that host protocol and supplies the dispatcher
    identity required by App Tools.
    """

    def __init__(self, context: CodexDesktopRouteContext, timeout_s: float) -> None:
        self._context = context
        self._timeout_s = timeout_s
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                [
                    self._context.mcp_runtime,
                    self._context.mcp_resource,
                    "--interaction-client-id",
                    self._context.dispatcher_thread_id,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={
                    **os.environ,
                    CODEX_APP_TOOLS_PIPE_PATH_ENV: self._context.pipe_path,
                },
            )
        except OSError as exc:
            raise TransportClosed("Codex Desktop MCP facade failed to start") from exc

    def send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise TransportClosed("Codex Desktop route is not connected")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise TransportClosed("Codex Desktop MCP request is too large")
        try:
            process.stdin.write(encoded + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransportClosed("Codex Desktop MCP facade closed") from exc

    def recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None or process.poll() is not None:
            raise TransportClosed("Codex Desktop route is not connected")
        wait_s = max(float(timeout_s or self._timeout_s), 0.01)
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(wait_s):
                raise TimeoutError("Codex Desktop MCP response timed out")
            line = process.stdout.readline(_MAX_RESPONSE_BYTES + 1)
        finally:
            selector.close()
        if not line:
            raise TransportClosed("Codex Desktop MCP facade closed")
        if len(line) > _MAX_RESPONSE_BYTES:
            raise TransportClosed("Codex Desktop MCP response is too large")
        try:
            message = json.loads(line.decode("utf-8").strip())
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TransportClosed("Codex Desktop MCP response is invalid") from exc
        if not isinstance(message, dict):
            raise TransportClosed("Codex Desktop MCP response is not an object")
        return message

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass


class _RelayMcpTransport:
    """MCP-shaped client for a host-attached Desktop relay."""

    def __init__(self, context: CodexDesktopRouteContext, timeout_s: float) -> None:
        self._context = context
        self._timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._reader: Any | None = None

    def start(self) -> None:
        if self._socket is not None:
            return
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self._timeout_s)
        try:
            client.connect(self._context.relay_socket)
        except OSError:
            client.close()
            raise
        self._socket = client
        self._reader = client.makefile("rb")

    def send(self, message: dict[str, Any]) -> None:
        client = self._socket
        if client is None:
            raise TransportClosed("Codex Desktop relay is not connected")
        envelope = {
            "token": self._context.relay_token,
            "message": message,
        }
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise TransportClosed("Codex Desktop relay request is too large")
        try:
            client.sendall(encoded + b"\n")
        except OSError as exc:
            raise TransportClosed("Codex Desktop relay closed") from exc

    def recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        client = self._socket
        reader = self._reader
        if client is None or reader is None:
            raise TransportClosed("Codex Desktop relay is not connected")
        client.settimeout(max(float(timeout_s or self._timeout_s), 0.01))
        try:
            line = reader.readline(_MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise TransportClosed("Codex Desktop relay closed") from exc
        if not line or len(line) > _MAX_RESPONSE_BYTES:
            raise TransportClosed("Codex Desktop relay response is unavailable")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise TransportClosed("Codex Desktop relay response is invalid") from exc
        if not isinstance(response, dict):
            raise TransportClosed("Codex Desktop relay response is invalid")
        return response

    def close(self) -> None:
        reader = self._reader
        client = self._socket
        self._reader = None
        self._socket = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        if client is not None:
            try:
                client.close()
            except OSError:
                pass


class CodexDesktopArchiveBroker:
    """Register, negotiate, and call one in-memory Desktop App Tools route."""

    def __init__(self, *, transport_factory: Any | None = None, timeout_s: float = 10.0) -> None:
        self.transport_factory = transport_factory
        self.timeout_s = max(float(timeout_s), 0.1)
        self._lock = threading.RLock()
        self._route: CodexDesktopRouteContext | None = None
        self._capability = "unavailable"
        self._generation = 0
        self._next_id = 1
        self._results: dict[tuple[str, str, str, bool], CodexDesktopArchiveResult] = {}
        self._inflight: dict[tuple[str, str, str, bool], threading.Event] = {}

    @property
    def route(self) -> CodexDesktopRouteContext | None:
        with self._lock:
            return self._route

    def route_available(self) -> bool:
        with self._lock:
            # A mechanically supplied Desktop route remains usable while its
            # capability is unknown.  The app-owned native pipe may reject a
            # second facade connection while another App Tools client is
            # active; treating that transient collision as route absence made
            # terminal cleanup permanently skip every later retry.
            return self._route is not None and self._capability != "unsupported"

    def public_status(self) -> dict[str, str]:
        with self._lock:
            route = self._route
            return {
                "status": "registered" if route is not None else "unavailable",
                "capability": self._capability,
                "route_digest": route.route_digest if route else "",
                "app_instance_digest": route.app_instance_digest if route else "",
            }

    def register(self, context: CodexDesktopRouteContext) -> dict[str, str]:
        """Replace the previous Desktop generation after dynamic ``tools/list``."""
        if (
            not isinstance(context, CodexDesktopRouteContext)
            or context.host != socket.gethostname()
            or not isinstance(context.pipe_path, str)
            or not Path(context.pipe_path).is_absolute()
            or not isinstance(context.dispatcher_thread_id, str)
            or not _canonical_uuid(context.dispatcher_thread_id)
            or not isinstance(context.mcp_runtime, str)
            or not _bounded_context_value(context.mcp_runtime)
            or not isinstance(context.mcp_resource, str)
            or not _bounded_context_value(context.mcp_resource)
            or bool(context.relay_socket) != bool(context.relay_token)
            or (context.relay_socket and not Path(context.relay_socket).is_absolute())
            or not _route_context_paths_valid(
                context,
                require_installed=self.transport_factory is None,
            )
        ):
            with self._lock:
                stale_events = tuple(self._inflight.values())
                self._inflight.clear()
                self._route = None
                self._capability = "unavailable"
                self._generation += 1
                self._results.clear()
            for event in stale_events:
                event.set()
            return self._registration_result(CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE)
        with self._lock:
            stale_events = tuple(self._inflight.values())
            self._inflight.clear()
            self._route = context
            self._capability = "unknown"
            self._generation += 1
            self._results.clear()
        for event in stale_events:
            event.set()
        transport = None
        try:
            transport = self._new_transport(context)
            self._start(transport)
            self._initialize(transport)
            tools_response = self._request(transport, "tools/list", {})
            tools = (
                tools_response.get("result")
                if isinstance(tools_response.get("result"), dict)
                else {}
            )
            names = {
                str(item.get("name") or "")
                for item in tools.get("tools", [])
                if isinstance(item, dict)
            }
            if CODEX_DESKTOP_ARCHIVE_TOOL not in names:
                with self._lock:
                    self._capability = "unsupported"
                return self._registration_result(CODEX_DESKTOP_ARCHIVE_UNSUPPORTED)
            with self._lock:
                self._capability = "supported"
            return self._registration_result("")
        except (TimeoutError, socket.timeout):
            self._mark_retryable(context)
            return self._registration_result(CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST)
        except (OSError, TransportClosed, RuntimeError, ValueError):
            self._mark_retryable(context)
            return self._registration_result(CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE)
        finally:
            self._close(transport)

    def archive(self, request: Any) -> CodexDesktopArchiveResult:
        """Acknowledge archiving of exactly one receipt-bound Executor UUID."""
        task_id = str(getattr(request, "task_id", "") or "").strip()
        executor_run_id = str(getattr(request, "executor_run_id", "") or "").strip()
        session_id = _canonical_uuid(getattr(request, "session_id", ""))
        request_digest = _digest(
            {"threadId": session_id, "archived": True}, prefix="request_"
        )
        with self._lock:
            route = self._route
            capability = self._capability
            generation = self._generation
            key = (task_id, executor_run_id, session_id, True)
            cached = self._results.get(key)
            if cached is not None:
                return cached
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                owner = True
            else:
                owner = False
        if not owner:
            event.wait(self.timeout_s)
            with self._lock:
                return self._results.get(
                    key,
                    self._failure(
                        CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
                        request_digest=request_digest,
                        route=route,
                    ),
                )

        if not session_id:
            result = self._failure(
                CODEX_DESKTOP_ARCHIVE_REJECTED,
                request_digest=request_digest,
                route=route,
            )
        elif route is None:
            result = self._failure(
                CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
                request_digest=request_digest,
                route=None,
            )
        elif capability == "unsupported":
            result = self._failure(
                CODEX_DESKTOP_ARCHIVE_UNSUPPORTED,
                request_digest=request_digest,
                route=route,
            )
        else:
            result = self._archive_on_route(
                route,
                session_id,
                request_digest,
                generation=generation,
                negotiate=capability != "supported",
            )
        with self._lock:
            # Transport loss is retryable and must never become a permanent
            # exact-request cache entry.  Acknowledgement and deterministic
            # rejection/unsupported results remain idempotent.
            cacheable = result.acknowledged or result.error_code in {
                CODEX_DESKTOP_ARCHIVE_REJECTED,
                CODEX_DESKTOP_ARCHIVE_UNSUPPORTED,
            }
            if cacheable and self._generation == generation and self._route == route:
                self._results[key] = result
            pending = self._inflight.get(key)
            if pending is event:
                self._inflight.pop(key, None)
                pending.set()
        return result

    def _archive_on_route(
        self,
        route: CodexDesktopRouteContext,
        session_id: str,
        request_digest: str,
        *,
        generation: int,
        negotiate: bool,
    ) -> CodexDesktopArchiveResult:
        transport = None
        try:
            transport = self._new_transport(route)
            self._start(transport)
            self._initialize(transport)
            if negotiate:
                tools_response = self._request(transport, "tools/list", {})
                tools = (
                    tools_response.get("result")
                    if isinstance(tools_response.get("result"), dict)
                    else {}
                )
                names = {
                    str(item.get("name") or "")
                    for item in tools.get("tools", [])
                    if isinstance(item, dict)
                }
                if CODEX_DESKTOP_ARCHIVE_TOOL not in names:
                    with self._lock:
                        if self._generation == generation and self._route == route:
                            self._capability = "unsupported"
                    return self._failure(
                        CODEX_DESKTOP_ARCHIVE_UNSUPPORTED,
                        request_digest=request_digest,
                        route=route,
                    )
            response = self._request(
                transport,
                "tools/call",
                {
                    "name": CODEX_DESKTOP_ARCHIVE_TOOL,
                    "arguments": {"threadId": session_id, "archived": True},
                },
            )
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            if result.get("isError") is True or result.get("error"):
                return self._failure(
                    CODEX_DESKTOP_ARCHIVE_REJECTED,
                    request_digest=request_digest,
                    route=route,
                )
            observed = self._response_thread_id(result)
            if observed and observed != session_id:
                return self._failure(
                    CODEX_DESKTOP_ARCHIVE_REJECTED,
                    request_digest=request_digest,
                    route=route,
                )
            with self._lock:
                current = self._generation == generation and self._route == route
                if current:
                    self._capability = "supported"
            if not current:
                return self._failure(
                    CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
                    request_digest=request_digest,
                    route=route,
                )
            return CodexDesktopArchiveResult(
                status="acknowledged",
                checked_at=_utc_now(),
                request_digest=request_digest,
                route_digest=route.route_digest,
                app_instance_digest=route.app_instance_digest,
            )
        except (TimeoutError, socket.timeout):
            self._mark_retryable(route)
            return self._failure(
                CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
                request_digest=request_digest,
                route=route,
            )
        except (OSError, TransportClosed, RuntimeError, ValueError):
            self._mark_retryable(route)
            return self._failure(
                CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
                request_digest=request_digest,
                route=route,
            )
        finally:
            self._close(transport)

    def _registration_result(self, error_code: str) -> dict[str, str]:
        status = self.public_status()
        if error_code:
            status["error_code"] = error_code
        return status

    @staticmethod
    def _failure(
        code: str,
        *,
        request_digest: str,
        route: CodexDesktopRouteContext | None,
    ) -> CodexDesktopArchiveResult:
        return CodexDesktopArchiveResult(
            status="rejected" if code == CODEX_DESKTOP_ARCHIVE_REJECTED else "unavailable",
            error_code=code,
            checked_at=_utc_now(),
            request_digest=request_digest,
            route_digest=route.route_digest if route else "",
            app_instance_digest=route.app_instance_digest if route else "",
        )

    def _new_transport(self, context: CodexDesktopRouteContext) -> Any:
        factory = self.transport_factory
        if factory is None:
            if context.relay_socket and context.relay_token:
                return _RelayMcpTransport(context, self.timeout_s)
            return _StdioMcpTransport(context, self.timeout_s)
        attempts = (
            lambda: factory(context=context),
            lambda: factory(context),
            lambda: factory(),
        )
        last_error: Exception | None = None
        for attempt in attempts:
            try:
                result = attempt()
            except TypeError as exc:
                last_error = exc
                continue
            if result is not None:
                return result
        raise TransportClosed("Codex Desktop route transport factory returned no transport") from last_error

    @staticmethod
    def _start(transport: Any) -> None:
        starter = getattr(transport, "start", None)
        if callable(starter):
            starter()

    @staticmethod
    def _close(transport: Any) -> None:
        closer = getattr(transport, "close", None)
        if callable(closer):
            closer()

    def _send(self, transport: Any, message: dict[str, Any]) -> None:
        sender = getattr(transport, "send", None)
        if not callable(sender):
            raise TransportClosed("Codex Desktop route transport has no send")
        sender(message)

    def _receive(self, transport: Any, timeout_s: float) -> dict[str, Any]:
        receiver = getattr(transport, "recv", None) or getattr(transport, "receive", None)
        if not callable(receiver):
            raise TransportClosed("Codex Desktop route transport has no recv")
        try:
            message = receiver(timeout_s=timeout_s)
        except TypeError:
            message = receiver()
        if not isinstance(message, dict):
            raise TransportClosed("Codex Desktop route response is not an object")
        return message

    def _request(self, transport: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        self._send(
            transport,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex Desktop MCP request timed out")
            message = self._receive(transport, remaining)
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                raise ValueError("Codex Desktop MCP request was rejected")
            return message

    def _initialize(self, transport: Any) -> None:
        self._request(
            transport,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "agentbc", "version": "1.0.3A"},
            },
        )
        self._send(transport, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _response_thread_id(result: dict[str, Any]) -> str:
        for key in ("threadId", "thread_id"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        for key in ("thread", "data"):
            nested = result.get(key)
            if isinstance(nested, dict):
                found = CodexDesktopArchiveBroker._response_thread_id(nested)
                if found:
                    return found
        return ""

    def _mark_retryable(self, route: CodexDesktopRouteContext) -> None:
        """Keep a valid Desktop route after a transient facade collision."""
        with self._lock:
            if self._route == route:
                self._capability = "unknown"


class AcknowledgedCodexDesktopArchiveBroker:
    """One-shot bridge for an acknowledgement returned by Codex Desktop.

    The Codex controller calls the native ``set_thread_archived`` tool first.
    Runner constructs this broker only after validating the exact persisted
    task/session/run binding supplied to the acknowledgement operation.
    """

    authoritative_ack = True

    def __init__(
        self,
        *,
        task_id: str,
        executor_run_id: str,
        session_id: str,
        request_executor_run_id: str | None = None,
    ) -> None:
        self.task_id = str(task_id or "").strip()
        self.executor_run_id = str(executor_run_id or "").strip()
        self.session_id = _canonical_uuid(session_id)
        self.request_executor_run_id = (
            self.executor_run_id
            if request_executor_run_id is None
            else str(request_executor_run_id or "").strip()
        )

    def route_available(self) -> bool:
        return bool(self.task_id and self.executor_run_id and self.session_id)

    def archive(self, request: Any) -> CodexDesktopArchiveResult:
        task_id = str(getattr(request, "task_id", "") or "").strip()
        executor_run_id = str(getattr(request, "executor_run_id", "") or "").strip()
        session_id = _canonical_uuid(getattr(request, "session_id", ""))
        request_digest = _digest(
            {"threadId": session_id, "archived": True}, prefix="request_"
        )
        binding = {
            "task_id": task_id,
            "executor_run_id": executor_run_id,
            "session_id": session_id,
        }
        if (
            task_id != self.task_id
            or executor_run_id != self.request_executor_run_id
            or session_id != self.session_id
        ):
            return CodexDesktopArchiveResult(
                status="rejected",
                error_code=CODEX_DESKTOP_ARCHIVE_REJECTED,
                checked_at=_utc_now(),
                request_digest=request_digest,
            )
        return CodexDesktopArchiveResult(
            status="acknowledged",
            checked_at=_utc_now(),
            request_digest=request_digest,
            route_digest=_digest(binding, prefix="route_"),
            app_instance_digest=_digest(
                {"source": "codex_app_control_plane"}, prefix="app_"
            ),
        )


# Short aliases make the production seam easy to discover without creating a
# second implementation or a version-specific capability table.
DesktopArchiveBroker = CodexDesktopArchiveBroker
DesktopRouteContext = CodexDesktopRouteContext
