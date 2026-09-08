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
import socket
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
CODEX_APP_TOOLS_MCP_CONTEXT_ENV = "CODEX_APP_TOOLS_MCP_CONTEXT"
CODEX_APP_TOOLS_MCP_RUNTIME_ENV = "CODEX_APP_TOOLS_MCP_RUNTIME"
CODEX_APP_TOOLS_MCP_RESOURCE_ENV = "CODEX_APP_TOOLS_MCP_RESOURCE"
CODEX_APP_TOOLS_MCP_RUNTIME_CONTEXT_ENV = "CODEX_APP_TOOLS_MCP_RUNTIME_CONTEXT"
CODEX_APP_TOOLS_MCP_RESOURCE_CONTEXT_ENV = "CODEX_APP_TOOLS_MCP_RESOURCE_CONTEXT"

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

    The optional JSON context is accepted only as a transport envelope.  Its
    values are never logged or included in the returned public route state.
    """
    source = env if env is not None else os.environ
    pipe_path = _bounded_context_value(source.get(CODEX_APP_TOOLS_PIPE_PATH_ENV))
    dispatcher_id = _canonical_uuid(source.get(CODEX_THREAD_ID_ENV))
    if not pipe_path or not Path(pipe_path).is_absolute() or not dispatcher_id:
        return None

    runtime = _bounded_context_value(source.get(CODEX_APP_TOOLS_MCP_RUNTIME_ENV))
    resource = _bounded_context_value(source.get(CODEX_APP_TOOLS_MCP_RESOURCE_ENV))
    envelope = _bounded_context_value(source.get(CODEX_APP_TOOLS_MCP_CONTEXT_ENV))
    if envelope:
        try:
            parsed = json.loads(envelope)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            runtime = runtime or _bounded_context_value(parsed.get("runtime"))
            resource = resource or _bounded_context_value(parsed.get("resource"))
    runtime = runtime or _bounded_context_value(source.get(CODEX_APP_TOOLS_MCP_RUNTIME_CONTEXT_ENV))
    resource = resource or _bounded_context_value(source.get(CODEX_APP_TOOLS_MCP_RESOURCE_CONTEXT_ENV))
    if not runtime or not resource:
        return None
    return CodexDesktopRouteContext(
        pipe_path=pipe_path,
        dispatcher_thread_id=dispatcher_id,
        mcp_runtime=runtime,
        mcp_resource=resource,
        host=socket.gethostname(),
    )


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


class _UnixMcpTransport:
    def __init__(self, path: str, timeout_s: float) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._buffer = b""

    def start(self) -> None:
        if self._socket is not None:
            return
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout_s)
        try:
            connection.connect(self._path)
        except Exception:
            connection.close()
            raise
        self._socket = connection

    def send(self, message: dict[str, Any]) -> None:
        if self._socket is None:
            raise TransportClosed("Codex Desktop route is not connected")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise TransportClosed("Codex Desktop MCP request is too large")
        self._socket.sendall(encoded + b"\n")

    def recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        if self._socket is None:
            raise TransportClosed("Codex Desktop route is not connected")
        self._socket.settimeout(max(float(timeout_s or self._timeout_s), 0.01))
        while b"\n" not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise TransportClosed("Codex Desktop route closed")
            self._buffer += chunk
            if len(self._buffer) > _MAX_RESPONSE_BYTES:
                raise TransportClosed("Codex Desktop MCP response is too large")
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TransportClosed("Codex Desktop MCP response is invalid") from exc
        if not isinstance(message, dict):
            raise TransportClosed("Codex Desktop MCP response is not an object")
        return message

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


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
            return self._route is not None and self._capability == "supported"

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
            self._invalidate()
            return self._registration_result(CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST)
        except (OSError, TransportClosed, RuntimeError, ValueError):
            self._invalidate()
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
        elif capability != "supported":
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
            )
        with self._lock:
            if self._generation == generation and self._route == route:
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
    ) -> CodexDesktopArchiveResult:
        transport = None
        try:
            transport = self._new_transport(route)
            self._start(transport)
            self._initialize(transport)
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
            self._invalidate(route)
            return self._failure(
                CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
                request_digest=request_digest,
                route=route,
            )
        except (OSError, TransportClosed, RuntimeError, ValueError):
            self._invalidate(route)
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
            return _UnixMcpTransport(context.pipe_path, self.timeout_s)
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

    def _invalidate(self, route: CodexDesktopRouteContext | None = None) -> None:
        with self._lock:
            if route is None or self._route == route:
                self._route = None
                self._capability = "unavailable"


# Short aliases make the production seam easy to discover without creating a
# second implementation or a version-specific capability table.
DesktopArchiveBroker = CodexDesktopArchiveBroker
DesktopRouteContext = CodexDesktopRouteContext
