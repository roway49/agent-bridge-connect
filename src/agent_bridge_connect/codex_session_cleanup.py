"""Narrow official Codex App Server session-cleanup protocol.

The cleanup path intentionally owns only the small protocol surface needed to
delete one already-bound thread and verify it through a fresh connection.  It
does not inspect Codex storage, session indexes, desktop caches, or transcript
content.  The larger App Server execution contract remains in
``codex_app_server`` (and can adopt this capability group later).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .control import StdioJsonRpcTransport, TransportClosed

CODEX_SESSION_CLEANUP_CAPABILITY_GROUP = "codex.session_cleanup"
CODEX_SESSION_CLEANUP_CLIENT_METHODS = frozenset(
    {"initialize", "thread/delete", "thread/read"}
)
CODEX_SESSION_CLEANUP_NOTIFICATIONS = frozenset({"thread/deleted"})
CODEX_DESKTOP_VISIBILITY_METHOD = "thread/list"
CODEX_THREAD_SOURCE_KINDS = (
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
)
CODEX_THREAD_LIST_PAGE_LIMIT = 1000
CODEX_THREAD_LIST_MAX_PAGES = 1000

CODEX_SESSION_DELETE_FAILED_CODE = "codex_session_delete_failed"
CODEX_SESSION_DELETE_INVALID_ID_CODE = "codex_session_delete_invalid_session_id"
CODEX_SESSION_DELETE_NOTIFICATION_UNCONFIRMED_CODE = (
    "codex_session_delete_notification_unconfirmed"
)
CODEX_SESSION_DELETE_STILL_PRESENT_CODE = "codex_session_delete_still_present"
CODEX_SESSION_DELETE_TIMEOUT_CODE = "codex_session_delete_timeout"
CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE = "codex_session_delete_transport_lost"
CODEX_DESKTOP_UI_STALE_CODE = "codex_desktop_ui_stale"
CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE = (
    "codex_desktop_verification_unavailable"
)

# Compatibility aliases for callers that used the descriptive term "missing"
# before the v2 receipt name was frozen.
CODEX_SESSION_DELETE_NOTIFICATION_MISSING_CODE = (
    CODEX_SESSION_DELETE_NOTIFICATION_UNCONFIRMED_CODE
)

CODEX_CLEANUP_VERIFICATION_STATUSES = frozenset(
    {"unknown", "absent", "present", "unavailable", "unverified"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _checked_at(value: Any) -> str:
    return str(value) if _valid_timestamp(value) else _utc_now()


@dataclass(frozen=True)
class CodexSessionCleanupObservation:
    """Safe backend observation returned by the official protocol client."""

    cli_status: str = "unknown"
    cli_checked_at: str = ""
    error_code: str = ""
    retryable: bool = False

    def verification(self) -> dict[str, dict[str, str]]:
        return {
            "cli": {
                "status": self.cli_status,
                "checked_at": _checked_at(self.cli_checked_at),
            }
        }


class CodexSessionCleanupError(RuntimeError):
    """Ephemeral protocol failure reduced to a stable public error code."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        cli_status: str = "unknown",
        cli_checked_at: str = "",
    ) -> None:
        self.code = str(code or CODEX_SESSION_DELETE_FAILED_CODE)
        self.retryable = bool(retryable)
        self.cli_status = str(cli_status or "unknown")
        self.cli_checked_at = str(cli_checked_at or "")
        super().__init__(self.code)


def _thread_id(message: dict[str, Any]) -> str:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    for key in ("threadId", "thread_id"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    result = message.get("result") if isinstance(message.get("result"), dict) else {}
    for key in ("threadId", "thread_id"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    for key in ("id", "threadId", "thread_id"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _error_text(message: dict[str, Any]) -> str:
    error = message.get("error") if isinstance(message.get("error"), dict) else {}
    parts = [error.get("code"), error.get("message")]
    return " ".join(str(item).strip().lower() for item in parts if item is not None)


def _is_not_found(message: dict[str, Any]) -> bool:
    text = _error_text(message)
    return any(
        marker in text
        for marker in (
            "not_found",
            "not found",
            "does not exist",
            "unknown thread",
            "unknown session",
            "thread_not_found",
            "session_not_found",
            "thread not loaded",
        )
    )


class CodexSessionCleanupClient:
    """Execute the exact delete/notification/fresh-read verification chain."""

    def __init__(
        self,
        executable: str | Path,
        *,
        cwd: str | Path,
        transport_factory: Any | None = None,
        transport: Any | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.executable = str(executable)
        self.cwd = Path(cwd).expanduser().resolve()
        self.command = [self.executable, "app-server", "--stdio"]
        self.transport_factory = transport_factory
        self.transport = transport
        self.timeout_s = max(float(timeout_s), 0.1)
        self._connection_count = 0
        self._next_id = 1

    def delete_and_verify(self, session_id: str) -> CodexSessionCleanupObservation:
        """Delete one UUID and verify absence using a separate App Server connection."""
        exact_id = self._validate_session_id(session_id)
        first = self._new_transport()
        try:
            self._start(first)
            self._initialize(first)
            delete_id = self._send(first, "thread/delete", {"threadId": exact_id})
            self._wait_delete(first, delete_id)
        finally:
            self._close(first)

        second = self._new_transport()
        try:
            self._start(second)
            self._initialize(second)
            read_id = self._send(second, "thread/read", {"threadId": exact_id})
            return self._read_absence(second, read_id, exact_id)
        finally:
            self._close(second)

    def verify_desktop_absence(self, session_id: str) -> dict[str, str]:
        """Verify that the exact thread is absent from Desktop's official list surface.

        Codex Desktop consumes the App Server thread list.  Querying every source
        kind and both archive partitions through a fresh connection avoids private
        database inspection while still detecting a stale Desktop-visible entry.
        Only the bounded absent/present result leaves this process.
        """
        exact_id = self._validate_session_id(session_id)
        transport = self._new_transport()
        try:
            self._start(transport)
            self._initialize(transport)
            for archived in (False, True):
                cursor: str | None = None
                for _ in range(CODEX_THREAD_LIST_MAX_PAGES):
                    params: dict[str, Any] = {
                        "archived": archived,
                        "limit": CODEX_THREAD_LIST_PAGE_LIMIT,
                        "sourceKinds": list(CODEX_THREAD_SOURCE_KINDS),
                    }
                    if cursor:
                        params["cursor"] = cursor
                    request_id = self._send(
                        transport,
                        CODEX_DESKTOP_VISIBILITY_METHOD,
                        params,
                    )
                    message = self._wait_response(
                        transport,
                        request_id,
                        allow_error=True,
                    )
                    if isinstance(message.get("error"), dict):
                        raise CodexSessionCleanupError(
                            CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE
                        )
                    result = (
                        message.get("result")
                        if isinstance(message.get("result"), dict)
                        else None
                    )
                    data = result.get("data") if isinstance(result, dict) else None
                    if not isinstance(data, list):
                        raise CodexSessionCleanupError(
                            CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE
                        )
                    if any(
                        isinstance(item, dict)
                        and str(item.get("id") or item.get("threadId") or "")
                        == exact_id
                        for item in data
                    ):
                        return {"status": "present", "checked_at": _utc_now()}
                    next_cursor = result.get("nextCursor")
                    if not isinstance(next_cursor, str) or not next_cursor:
                        break
                    cursor = next_cursor
                else:
                    raise CodexSessionCleanupError(
                        CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE
                    )
            return {"status": "absent", "checked_at": _utc_now()}
        finally:
            self._close(transport)

    @staticmethod
    def _validate_session_id(value: str) -> str:
        candidate = str(value or "").strip()
        try:
            parsed = uuid.UUID(candidate)
        except (AttributeError, ValueError):
            raise CodexSessionCleanupError(CODEX_SESSION_DELETE_INVALID_ID_CODE)
        if str(parsed) != candidate.lower():
            raise CodexSessionCleanupError(CODEX_SESSION_DELETE_INVALID_ID_CODE)
        return candidate

    def _new_transport(self) -> Any:
        factory = self.transport_factory
        if factory is not None:
            attempts = (
                lambda: factory(
                    run_id="agentbc-session-cleanup",
                    task_packet={},
                    cwd=self.cwd,
                    command=list(self.command),
                ),
                lambda: factory("agentbc-session-cleanup", {}, self.cwd),
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
                    self._connection_count += 1
                    return result
            if last_error is not None:
                raise TransportClosed("Codex cleanup transport factory failed") from last_error
            raise TransportClosed("Codex cleanup transport factory returned no transport")

        # A sequence is a useful narrow test seam and still guarantees a new
        # object for the verification connection.  A single injected object is
        # accepted only for the first connection; production uses the stdio
        # constructor below for the second connection.
        if isinstance(self.transport, (list, tuple)):
            if self._connection_count < len(self.transport):
                result = self.transport[self._connection_count]
                self._connection_count += 1
                return result
            raise TransportClosed("Codex cleanup transport sequence is exhausted")
        if self.transport is not None and self._connection_count == 0:
            self._connection_count += 1
            return self.transport
        self._connection_count += 1
        return StdioJsonRpcTransport(self.executable, cwd=self.cwd, command=self.command)

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

    @staticmethod
    def _send_message(transport: Any, message: dict[str, Any]) -> None:
        sender = getattr(transport, "send", None)
        if not callable(sender):
            raise TransportClosed("Codex cleanup transport has no send method")
        sender(message)

    def _receive(self, transport: Any, timeout_s: float) -> dict[str, Any]:
        receiver = getattr(transport, "recv", None) or getattr(transport, "receive", None)
        if not callable(receiver):
            raise TransportClosed("Codex cleanup transport has no recv method")
        try:
            message = receiver(timeout_s=max(float(timeout_s), 0.0))
        except TypeError:
            # Existing protocol fakes expose the older no-argument seam.  The
            # real stdio transport always accepts the bounded timeout.
            message = receiver()
        if not isinstance(message, dict):
            raise TransportClosed("Codex cleanup transport returned a non-object")
        return message

    def _initialize(self, transport: Any) -> None:
        request_id = self._send(
            transport,
            "initialize",
            {
                "clientInfo": {"name": "agentbc", "version": "1.0.3A"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._wait_response(transport, request_id)
        self._send_message(
            transport,
            {"jsonrpc": "2.0", "method": "initialized"},
        )

    def _send(
        self,
        transport: Any,
        method: str,
        params: dict[str, Any],
    ) -> int:
        request_id = self._next_id
        self._next_id += 1
        self._send_message(
            transport,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        return request_id

    def _wait_response(
        self,
        transport: Any,
        request_id: int,
        *,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TIMEOUT_CODE,
                    retryable=True,
                )
            try:
                message = self._receive(transport, remaining)
            except TimeoutError as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TIMEOUT_CODE,
                    retryable=True,
                ) from exc
            except (TransportClosed, OSError) as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
                    retryable=True,
                ) from exc
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict) and not allow_error:
                raise CodexSessionCleanupError(CODEX_SESSION_DELETE_FAILED_CODE)
            return message

    def _wait_delete(self, transport: Any, request_id: int) -> None:
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TIMEOUT_CODE,
                    retryable=True,
                )
            try:
                message = self._receive(transport, remaining)
            except TimeoutError as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TIMEOUT_CODE,
                    retryable=True,
                ) from exc
            except (TransportClosed, OSError) as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
                    retryable=True,
                ) from exc

            if message.get("id") == request_id:
                if isinstance(message.get("error"), dict):
                    raise CodexSessionCleanupError(CODEX_SESSION_DELETE_FAILED_CODE)
                # Current supported and candidate Codex builds expose the
                # thread/deleted schema but do not reliably emit it on stdio.
                # The RPC acknowledgement remains mandatory; authoritative
                # absence is proved next by fresh thread/read and thread/list.
                return
            if message.get("method") == "thread/deleted":
                # A notification is advisory. Ignore unrelated IDs and keep
                # waiting for the bound RPC response.
                continue

    def _read_absence(
        self,
        transport: Any,
        request_id: int,
        exact_id: str,
    ) -> CodexSessionCleanupObservation:
        message = self._wait_response(transport, request_id, allow_error=True)
        if isinstance(message.get("error"), dict):
            if _is_not_found(message):
                return CodexSessionCleanupObservation(
                    cli_status="absent",
                    cli_checked_at=_utc_now(),
                )
            raise CodexSessionCleanupError(CODEX_SESSION_DELETE_FAILED_CODE)

        result = message.get("result") if isinstance(message.get("result"), dict) else None
        if result is None:
            raise CodexSessionCleanupError(CODEX_SESSION_DELETE_FAILED_CODE)
        if result.get("thread") is None and "thread" in result:
            return CodexSessionCleanupObservation(
                cli_status="absent",
                cli_checked_at=_utc_now(),
            )
        observed_id = _thread_id(message)
        if observed_id == exact_id:
            raise CodexSessionCleanupError(
                CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
                cli_status="present",
                cli_checked_at=_utc_now(),
            )
        if observed_id:
            raise CodexSessionCleanupError(
                CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
                cli_status="present",
                cli_checked_at=_utc_now(),
            )
        raise CodexSessionCleanupError(CODEX_SESSION_DELETE_FAILED_CODE)


__all__ = [
    "CODEX_CLEANUP_VERIFICATION_STATUSES",
    "CODEX_DESKTOP_UI_STALE_CODE",
    "CODEX_DESKTOP_VISIBILITY_METHOD",
    "CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE",
    "CODEX_SESSION_CLEANUP_CAPABILITY_GROUP",
    "CODEX_SESSION_CLEANUP_CLIENT_METHODS",
    "CODEX_SESSION_CLEANUP_NOTIFICATIONS",
    "CODEX_SESSION_DELETE_FAILED_CODE",
    "CODEX_SESSION_DELETE_INVALID_ID_CODE",
    "CODEX_SESSION_DELETE_NOTIFICATION_MISSING_CODE",
    "CODEX_SESSION_DELETE_NOTIFICATION_UNCONFIRMED_CODE",
    "CODEX_SESSION_DELETE_STILL_PRESENT_CODE",
    "CODEX_SESSION_DELETE_TIMEOUT_CODE",
    "CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE",
    "CodexSessionCleanupClient",
    "CodexSessionCleanupError",
    "CodexSessionCleanupObservation",
]
