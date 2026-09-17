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
    {"initialize", "thread/archive", "thread/delete", "thread/read"}
)
CODEX_SESSION_CLEANUP_NOTIFICATIONS = frozenset(
    {"thread/archived", "thread/deleted"}
)
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

# SESSION-104-001 stable archive codes.  ``target missing`` also covers the
# ordering regression ordering finding: deleting first and archiving afterwards returns
# a target-not-found style error, which is exactly why archive must run first.
CODEX_SESSION_ARCHIVE_FAILED_CODE = "codex_session_archive_failed"
CODEX_SESSION_ARCHIVE_INVALID_ID_CODE = "codex_session_archive_invalid_session_id"
CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE = "codex_session_archive_target_missing"
CODEX_SESSION_ARCHIVE_TIMEOUT_CODE = "codex_session_archive_timeout"
CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE = "codex_session_archive_transport_lost"

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
    command_evidence: dict[str, dict[str, str]] | None = None

    def verification(self) -> dict[str, dict[str, str]]:
        return {
            "cli": {
                "status": self.cli_status,
                "checked_at": _checked_at(self.cli_checked_at),
            }
        }

    def commands(self) -> dict[str, dict[str, str]]:
        """Return the bounded v4 per-command evidence for this observation."""
        if self.command_evidence is not None:
            return {
                name: dict(value)
                for name, value in self.command_evidence.items()
                if name in {"archive", "delete"} and isinstance(value, dict)
            }
        checked_at = _checked_at(self.cli_checked_at)
        if self.cli_status == "absent":
            return {
                "archive": {"status": "acknowledged", "checked_at": checked_at},
                "delete": {"status": "acknowledged", "checked_at": checked_at},
            }
        return {
            "archive": {"status": "unverified", "checked_at": checked_at},
            "delete": {"status": "unverified", "checked_at": checked_at},
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
        commands: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.code = str(code or CODEX_SESSION_DELETE_FAILED_CODE)
        self.retryable = bool(retryable)
        self.cli_status = str(cli_status or "unknown")
        self.cli_checked_at = str(cli_checked_at or "")
        # Bounded partial command evidence for the phase that failed.  None
        # means no command was even attempted (e.g. an invalid session id).
        self.commands = commands
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


def _archive_error_is_target_missing(message: dict[str, Any]) -> bool:
    """Return True when an archive error names the exact target as missing.

    ordering regression evidence: delete followed by archive returns a target-not-found
    error for the deleted thread.  The same error on a fresh archive means
    the exact UUID no longer exists, so the archive precondition cannot be
    established and the cleanup must fail closed before any delete call.
    """
    return _is_not_found(message)


def _partial_commands_after_archive(
    checked_at: str,
    archive_status: str = "acknowledged",
) -> dict[str, dict[str, str]]:
    """Bounded evidence after the archive gate: delete was never requested."""
    return {
        "archive": {"status": archive_status, "checked_at": checked_at},
        "delete": {"status": "not_requested", "checked_at": checked_at},
    }


def _archive_phase_unverified_evidence(checked_at: str) -> dict[str, dict[str, str]]:
    """Evidence before the archive result is known; delete was never sent."""
    return {
        "archive": {"status": "unverified", "checked_at": checked_at},
        "delete": {"status": "not_requested", "checked_at": checked_at},
    }


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

    def delete_and_verify(
        self,
        session_id: str,
        *,
        archive_acknowledged: bool = False,
        archive_checked_at: str = "",
    ) -> CodexSessionCleanupObservation:
        """Archive then delete one UUID and verify absence on a new connection.

        SESSION-104-001 sequence, in exact order:

        1. Require a bounded ``thread/archive`` acknowledgement. It may be
           supplied by the original Executor connection, which avoids Codex's
           active-writer rejection, or requested here for legacy callers.
           ``thread/archived`` notifications are advisory. No acknowledgement
           means zero ``thread/delete`` calls are ever sent. ordering regression proved
           the reverse order is unusable: deleting first and archiving
           afterwards returns target-not-found.
        2. connection A: ``thread/delete`` with a mandatory RPC
           acknowledgement; ``thread/deleted`` stays advisory.
        3. connection B: fresh ``thread/read`` absence proof.

        Only bounded statuses leave this method; raw RPC text never does.
        """
        exact_id = self._validate_session_id(session_id)
        delete_sent = False
        prearchived = bool(archive_acknowledged)
        archive_at = _checked_at(archive_checked_at) if prearchived else ""
        try:
            first = self._new_transport()
        except (TransportClosed, OSError, RuntimeError) as exc:
            raise CodexSessionCleanupError(
                CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE
                if prearchived
                else CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE,
                retryable=True,
                commands=_partial_commands_after_archive(archive_at)
                if prearchived
                else _archive_phase_unverified_evidence(_utc_now()),
            ) from exc
        try:
            self._start(first)
            self._initialize(first)
            if not prearchived:
                archive_id = self._send(first, "thread/archive", {"threadId": exact_id})
                self._wait_archive(first, archive_id)
                archive_at = _utc_now()
            delete_id = self._send(first, "thread/delete", {"threadId": exact_id})
            delete_sent = True
            delete_status = self._wait_delete(first, delete_id)
        except CodexSessionCleanupError:
            raise
        except (TransportClosed, OSError, RuntimeError) as exc:
            if delete_sent or prearchived:
                # Delete was already sent, which proves the archive gate had
                # passed: never downgrade the archive evidence.
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
                    retryable=True,
                    commands=_partial_commands_after_archive(archive_at, "acknowledged"),
                ) from exc
            raise CodexSessionCleanupError(
                CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE,
                retryable=True,
                commands=_archive_phase_unverified_evidence(_utc_now()),
            ) from exc
        finally:
            self._close(first)

        second = None
        try:
            second = self._new_transport()
            self._start(second)
            self._initialize(second)
            read_id = self._send(second, "thread/read", {"threadId": exact_id})
            observation = self._read_absence(second, read_id, exact_id)
            checked_at = _checked_at(observation.cli_checked_at)
            return CodexSessionCleanupObservation(
                cli_status=observation.cli_status,
                cli_checked_at=checked_at,
                error_code=observation.error_code,
                retryable=observation.retryable,
                command_evidence={
                    "archive": {
                        "status": "acknowledged",
                        "checked_at": archive_at or checked_at,
                    },
                    "delete": {"status": delete_status, "checked_at": checked_at},
                },
            )
        except CodexSessionCleanupError:
            # A bounded protocol verdict (e.g. still_present) is authoritative
            # and must never be re-labeled as a transport failure.
            raise
        except (TransportClosed, OSError, RuntimeError) as exc:
            # Both commands were acknowledged; only the fresh-read diagnostic
            # connection died, so the evidence keeps both acknowledgements.
            raise CodexSessionCleanupError(
                CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
                retryable=True,
                commands=_partial_commands_after_archive(_utc_now(), "acknowledged"),
            ) from exc
        finally:
            if second is not None:
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

    def _wait_archive(self, transport: Any, request_id: int) -> None:
        """Require the archive RPC acknowledgement before any delete call.

        ``thread/archived`` is an advisory notification and never satisfies
        the gate.  Timeout and transport loss raise stable archive-scoped
        codes carrying the partial ``commands`` evidence (archive attempted,
        delete not_requested) so a retry or Runner restart never loses an
        acknowledged archive and never invents a delete attempt.
        """
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_ARCHIVE_TIMEOUT_CODE,
                    retryable=True,
                    commands={
                        "archive": {"status": "unverified", "checked_at": _utc_now()},
                        "delete": {"status": "not_requested", "checked_at": _utc_now()},
                    },
                )
            try:
                message = self._receive(transport, remaining)
            except TimeoutError as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_ARCHIVE_TIMEOUT_CODE,
                    retryable=True,
                    commands={
                        "archive": {"status": "unverified", "checked_at": _utc_now()},
                        "delete": {"status": "not_requested", "checked_at": _utc_now()},
                    },
                ) from exc
            except (TransportClosed, OSError) as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE,
                    retryable=True,
                    commands={
                        "archive": {"status": "unverified", "checked_at": _utc_now()},
                        "delete": {"status": "not_requested", "checked_at": _utc_now()},
                    },
                ) from exc
            if message.get("id") == request_id:
                if isinstance(message.get("error"), dict):
                    code = (
                        CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE
                        if _archive_error_is_target_missing(message)
                        else CODEX_SESSION_ARCHIVE_FAILED_CODE
                    )
                    raise CodexSessionCleanupError(
                        code,
                        commands={
                            "archive": {"status": "failed", "checked_at": _utc_now()},
                            "delete": {"status": "not_requested", "checked_at": _utc_now()},
                        },
                    )
                # The bound RPC acknowledgement is mandatory.  A server that
                # reports the thread was already archived still acknowledges
                # the archive state, so the sequence may continue.
                return
            if message.get("method") == "thread/archived":
                # Advisory only: keep waiting for the bound RPC response.
                continue

    def _wait_delete(self, transport: Any, request_id: int) -> str:
        """Require a delete RPC response after the archive gate.

        A failure here carries partial evidence: the archive was already
        acknowledged, so a retry or Runner restart must never lose it.
        A bounded RPC error still proves the exact request reached the server;
        the fresh read/list phase must then prove absence before that delivery
        may be recorded as ``confirmed``.
        """
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TIMEOUT_CODE,
                    retryable=True,
                    commands=_partial_commands_after_archive(_utc_now()),
                )
            try:
                message = self._receive(transport, remaining)
            except TimeoutError as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TIMEOUT_CODE,
                    retryable=True,
                    commands=_partial_commands_after_archive(_utc_now()),
                ) from exc
            except (TransportClosed, OSError) as exc:
                raise CodexSessionCleanupError(
                    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
                    retryable=True,
                    commands=_partial_commands_after_archive(_utc_now()),
                ) from exc

            if message.get("id") == request_id:
                if isinstance(message.get("error"), dict):
                    return "confirmed"
                # Current supported and candidate Codex builds expose the
                # thread/deleted schema but do not reliably emit it on stdio.
                # The RPC acknowledgement remains mandatory; authoritative
                # absence is proved next by fresh thread/read and thread/list.
                return "acknowledged"
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
    "CODEX_SESSION_ARCHIVE_FAILED_CODE",
    "CODEX_SESSION_ARCHIVE_INVALID_ID_CODE",
    "CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE",
    "CODEX_SESSION_ARCHIVE_TIMEOUT_CODE",
    "CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE",
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
