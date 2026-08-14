"""Terminal session cleanup coordinator (AgentBC 1.0.2A Phase 5 Task 2).

This module owns the post-terminal executor-session cleanup lifecycle.  Every
pass re-reads the authoritative task/session snapshot from disk, re-validates
every eligibility gate under a per-task file lock, then either:

- marks ``retain=true`` terminal sessions ``retained`` without touching an
  Executor;
- transitions a gated terminal session to ``pending`` and dispatches exactly
  one ``ExecutorPort.cleanup_session`` request, atomically persisting the
  receipt/event afterwards;
- converts a ``pending`` receipt left over from a crashed process into a stable
  ``failed``/fallback state scheduled for backoff, never hot-looping;
- retries ``failed`` receipts at most ``MAX_SESSION_CLEANUP_ATTEMPTS`` times
  (immediately, then earliest 60s, then earliest 5min).

Cleanup ``succeeded``/``unsupported``/``failed`` receipts never mutate the
original task terminal state, final callback, report readiness, or completed
steps.  Receipts and events never carry the full request, project paths, raw
output, prompts, secrets, or private Executor database paths.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:  # pragma: no cover - POSIX is required by the config/runner runtime
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .adapters import ExecutorPort, SessionCleanupRequest, SessionCleanupResult
from .execution_policy import (
    CLEANUP_STRATEGIES,
    MAX_SESSION_CLEANUP_ATTEMPTS,
    RESOLVED_CLEANUP_STATES,
    SESSION_EXTENSION_KEY,
    TERMINAL_SESSION_CLEANUP_STATUSES,
    read_session_cleanup_receipt,
    session_cleanup_blockers,
    transition_session_cleanup,
    validate_session_snapshot,
)
from .protocol import ABCError
from .record_management import append_bounded_jsonl
from .run_lease import load_lease
from .task_id import split_task_ref
from .task_store import TaskStore


CLEANUP_EVENT_TYPE = "session.cleanup"
CLEANUP_EVENTS_FILE = "cleanup.jsonl"
CLEANUP_CRASH_RECOVERY_DELAY_S = 60
# Backoff schedule: the second attempt is earliest 60s after the first failure,
# the third attempt is earliest 300s after the second failure.
CLEANUP_RETRY_BACKOFF_S = (60, 300)
CLEANUP_LOCK_NAME = ".cleanup.lock"
CLEANUP_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Statuses where the coordinator persisted a transition (side-effectful).
_ACTIONED_STATUSES = frozenset(
    {"retained", "recovered", "succeeded", "unsupported", "failed"}
)


def default_cleanup_port(executor: str) -> ExecutorPort:
    """Resolve the built-in ExecutorPort; adapters fail closed on cleanup."""
    from .config import get_executor_config, load_config
    from .executor_registry import get_executor

    config = load_config()
    return get_executor(executor, get_executor_config(config, executor))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _add_delay(value: str, delay_s: int) -> str:
    return (
        _parse_utc(value) + timedelta(seconds=delay_s)
    ).isoformat().replace("+00:00", "Z")


def _is_iso_utc(value: str) -> bool:
    try:
        parsed = _parse_utc(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _sanitize_error_code(value: Any, fallback: str = "session_cleanup_failed") -> str:
    """Reduce an adapter reason to a stable lowercase code or the fallback."""
    text = str(value or "").strip()
    if CLEANUP_ERROR_CODE_RE.fullmatch(text):
        return text
    return fallback


def _sanitize_strategy(value: Any) -> str:
    """Return a supported delete strategy; reject retain/none/raw text."""
    text = str(value or "").strip()
    if text in CLEANUP_STRATEGIES and text not in {"none", "retain"}:
        return text
    return ""


class SessionCleanupCoordinator:
    """Authoritative post-terminal session cleanup controller for one board."""

    def __init__(
        self,
        board_root: str | Path,
        *,
        executor_port: ExecutorPort | None = None,
        port_resolver: Callable[[str], ExecutorPort] | None = None,
        store: TaskStore | None = None,
    ) -> None:
        self.board = Path(board_root).expanduser().resolve()
        self.store = store or TaskStore(self.board)
        self._executor_port = executor_port
        self._port_resolver = port_resolver or default_cleanup_port

    # ------------------------------------------------------------------ time
    @staticmethod
    def _now(value: str | None = None) -> str:
        return _utc_now() if value is None else str(value)

    # ---------------------------------------------------------------- public
    def request_cleanup(self, task_id: str, *, now: str | None = None) -> dict[str, Any]:
        """Run one authoritative cleanup pass for a single exact task.

        Re-reads the task/session from disk under the per-task lock, verifies
        every gate, and either performs a single transition + at most one
        Executor call, or returns a zero-side-effect skip.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            return self._result("", "skipped", ["task_id_missing"])
        if not self.store.task_exists(task_id):
            return self._result(task_id, "skipped", ["task_not_found"])
        occurred_at = self._now(now)
        with self._task_lock(task_id):
            task = self._read_task(task_id)
            if task is None:
                return self._result(task_id, "skipped", ["task_read_failed"])
            task_id = str(task.get("id") or task.get("task_id") or task_id)
            session = self._authoritative_session(task)
            if session is None:
                return self._result(task_id, "skipped", ["session_receipt_invalid"])
            receipt = read_session_cleanup_receipt(session.get("cleanup"))
            state = receipt["state"]
            if state in RESOLVED_CLEANUP_STATES:
                return self._result(task_id, "resolved", [], receipt=receipt)
            blockers = self._gates(task)

            if session.get("retain") is True:
                return self._handle_retained(task, session, blockers, occurred_at)

            if state == "not_requested":
                if blockers:
                    return self._result(task_id, "skipped", blockers, receipt=receipt)
                pending = self._to_pending(task, session, occurred_at)
                self._persist_receipt(task, pending, "requested", occurred_at)
                return self._execute(task_id, pending, occurred_at)

            if state == "failed":
                if blockers:
                    return self._result(task_id, "skipped", blockers, receipt=receipt)
                if not receipt["retryable"]:
                    return self._result(task_id, "final", [], receipt=receipt)
                if not receipt["next_attempt_at"] or not _is_iso_utc(receipt["next_attempt_at"]):
                    return self._result(task_id, "waiting", [], receipt=receipt)
                if _parse_utc(occurred_at) < _parse_utc(receipt["next_attempt_at"]):
                    return self._result(task_id, "waiting", [], receipt=receipt)
                if receipt["attempts"] >= MAX_SESSION_CLEANUP_ATTEMPTS:
                    return self._result(task_id, "final", [], receipt=receipt)
                pending = self._to_pending(task, session, occurred_at)
                self._persist_receipt(task, pending, "retry", occurred_at)
                return self._execute(task_id, pending, occurred_at)

            if state == "pending":
                # A pending receipt under the lock is always a crashed-process
                # leftover: form a stable failed/fallback state, then backoff.
                if blockers:
                    return self._result(task_id, "skipped", blockers, receipt=receipt)
                failed = self._crash_recovery_receipt(task, session, receipt, occurred_at)
                self._persist_receipt(task, failed, "interrupted", occurred_at)
                return self._result(task_id, "recovered", [], receipt=failed)

            return self._result(task_id, "noop", [], receipt=receipt)

    def maintain_board(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Scan this board for terminal sessions needing a cleanup pass."""
        results: list[dict[str, Any]] = []
        tasks = self._list_tasks()
        for task in tasks:
            task_id = str(task.get("id") or task.get("task_id") or "")
            if not task_id:
                continue
            if str(task.get("status") or "") not in TERMINAL_SESSION_CLEANUP_STATUSES:
                continue
            session = (task.get("extensions") or {}).get(SESSION_EXTENSION_KEY)
            if not isinstance(session, dict):
                continue
            try:
                result = self.request_cleanup(task_id, now=now)
            except (ABCError, OSError, ValueError, json.JSONDecodeError):
                continue
            if result.get("actioned"):
                results.append(result)
        return results

    # ------------------------------------------------------------ retain path
    def _handle_retained(
        self,
        task: dict[str, Any],
        session: dict[str, Any],
        blockers: list[str],
        occurred_at: str,
    ) -> dict[str, Any]:
        task_id = str(task.get("id") or task.get("task_id") or "")
        receipt = read_session_cleanup_receipt(session.get("cleanup"))
        if receipt["state"] != "not_requested":
            return self._result(task_id, "resolved", [], receipt=receipt)
        retention_blockers = [item for item in blockers if item != "retention_enabled"]
        if retention_blockers:
            return self._result(task_id, "skipped", blockers, receipt=receipt)
        try:
            retained = self._transition(
                session,
                "retained",
                task=task,
                occurred_at=occurred_at,
            )
        except ABCError:
            return self._result(task_id, "skipped", blockers, receipt=receipt)
        self._persist_receipt(task, retained, "retained", occurred_at)
        return self._result(task_id, "retained", [], receipt=retained)

    # ---------------------------------------------------------- pending steps
    def _to_pending(
        self,
        task: dict[str, Any],
        session: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        request = self._build_request(task, session)
        return self._transition(
            session,
            "pending",
            task=task,
            occurred_at=occurred_at,
            capability="supported",
            strategy=request.strategy or "official_session_delete",
        )

    def _crash_recovery_receipt(
        self,
        task: dict[str, Any],
        session: dict[str, Any],
        receipt: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        retryable, next_attempt_at = self._next_retry(
            receipt["attempts"],
            occurred_at,
        )
        return self._transition(
            session,
            "failed",
            task=task,
            occurred_at=occurred_at,
            capability=receipt["capability"] or "supported",
            strategy=receipt["strategy"] or "official_session_delete",
            error_code="session_cleanup_interrupted",
            retryable=retryable,
            next_attempt_at=next_attempt_at,
        )

    def _transition(
        self,
        session: dict[str, Any],
        target: str,
        *,
        task: dict[str, Any],
        occurred_at: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        task_id = str(task.get("id") or task.get("task_id") or "")
        return transition_session_cleanup(
            session,
            target,
            task_status=str(task.get("status") or ""),
            lease_state=self._lease_state(task_id),
            report_written=self._report_written(task),
            notification_recorded=self._notification_recorded(task_id),
            occurred_at=occurred_at,
            **kwargs,
        )

    # --------------------------------------------------------- executor call
    def _execute(
        self,
        task_id: str,
        pending: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        task = self._read_task(task_id)
        if task is None:
            return self._result(task_id, "skipped", ["task_read_failed"], receipt=pending)
        session = self._authoritative_session(task)
        if session is None:
            return self._result(task_id, "skipped", ["session_receipt_invalid"], receipt=pending)
        request = self._build_request(task, session)
        try:
            port = self._resolve_port(session["executor"])
            result = port.cleanup_session(request)
            if not isinstance(result, SessionCleanupResult):
                raise TypeError("cleanup_session must return SessionCleanupResult")
        except Exception:
            result = SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy=pending["strategy"],
                error_code="session_cleanup_failed",
                retryable=True,
            )
        return self._apply_result(task_id, pending, result, occurred_at)

    def _apply_result(
        self,
        task_id: str,
        pending: dict[str, Any],
        result: SessionCleanupResult,
        occurred_at: str,
    ) -> dict[str, Any]:
        task = self._read_task(task_id)
        if task is None:
            return self._result(task_id, "skipped", ["task_read_failed"], receipt=pending)
        session = self._authoritative_session(task)
        if session is None:
            return self._result(task_id, "skipped", ["session_receipt_invalid"], receipt=pending)
        current = read_session_cleanup_receipt(session.get("cleanup"))
        if current["state"] != "pending":
            return self._result(task_id, "superseded", [], receipt=current)
        if result.state == "succeeded":
            new_receipt = self._transition(
                session,
                "succeeded",
                task=task,
                occurred_at=occurred_at,
                capability="supported",
                strategy=_sanitize_strategy(result.strategy) or pending["strategy"],
            )
        elif result.state == "unsupported":
            new_receipt = self._transition(
                session,
                "unsupported",
                task=task,
                occurred_at=occurred_at,
                capability="unsupported",
                strategy="none",
                error_code=_sanitize_error_code(result.error_code, "session_cleanup_unsupported"),
            )
        else:
            retryable, next_attempt_at = (
                self._next_retry(current["attempts"], occurred_at)
                if result.retryable
                else (False, "")
            )
            new_receipt = self._transition(
                session,
                "failed",
                task=task,
                occurred_at=occurred_at,
                capability=current["capability"] or "supported",
                strategy=current["strategy"] or pending["strategy"],
                error_code=_sanitize_error_code(result.error_code),
                retryable=retryable,
                next_attempt_at=next_attempt_at,
            )
        self._persist_receipt(task, new_receipt, "result", occurred_at)
        return self._result(task_id, new_receipt["state"], [], receipt=new_receipt)

    # --------------------------------------------------------------- helpers
    def _resolve_port(self, executor: str) -> ExecutorPort:
        if self._executor_port is not None:
            return self._executor_port
        return self._port_resolver(str(executor or "").strip())

    def _build_request(self, task: dict[str, Any], session: dict[str, Any]) -> SessionCleanupRequest:
        task_id = str(task.get("id") or task.get("task_id") or "")
        workspace = dict(task.get("workspace") or {})
        retain = bool(session.get("retain"))
        project_mode = str(session.get("project_mode") or "none")
        project_path = str(session.get("project_path") or "")
        return SessionCleanupRequest(
            executor=str(session.get("executor") or ""),
            session_id=str(session.get("session_id") or ""),
            task_id=task_id,
            retain=retain,
            project_mode=project_mode,
            strategy=self._request_strategy(session, retain, project_mode),
            project_path=project_path,
            workspace=workspace,
        )

    @staticmethod
    def _request_strategy(
        session: dict[str, Any],
        retain: bool,
        project_mode: str,
    ) -> str:
        if retain:
            return "retain"
        executor = str(session.get("executor") or "").strip().lower()
        if executor == "claude" and project_mode == "ephemeral":
            return "claude_project_purge"
        return "official_session_delete"

    @staticmethod
    def _next_retry(attempts: int, occurred_at: str) -> tuple[bool, str]:
        if attempts >= MAX_SESSION_CLEANUP_ATTEMPTS:
            return False, ""
        delay = CLEANUP_RETRY_BACKOFF_S[0] if attempts < 2 else CLEANUP_RETRY_BACKOFF_S[1]
        return True, _add_delay(occurred_at, delay)

    def _read_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.store.read_task(task_id)
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return None

    def _list_tasks(self) -> list[dict[str, Any]]:
        try:
            return self.store.list_tasks()
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return []

    def _authoritative_session(self, task: dict[str, Any]) -> dict[str, Any] | None:
        extensions = task.get("extensions")
        if not isinstance(extensions, dict):
            return None
        session = extensions.get(SESSION_EXTENSION_KEY)
        if not isinstance(session, dict):
            return None
        if validate_session_snapshot(session):
            return None
        return copy.deepcopy(session)

    def _gates(self, task: dict[str, Any]) -> list[str]:
        task_id = str(task.get("id") or task.get("task_id") or "")
        session = self._authoritative_session(task)
        if session is None:
            return ["session_receipt_invalid"]
        return session_cleanup_blockers(
            task_status=str(task.get("status") or ""),
            lease_state=self._lease_state(task_id),
            report_written=self._report_written(task),
            notification_recorded=self._notification_recorded(task_id),
            session=session,
        )

    def _lease_state(self, task_id: str) -> str:
        try:
            lease = load_lease(task_id, self.board)
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return "active"
        if lease is None:
            return "missing"
        state = str(getattr(lease, "state", "") or "")
        if state == "closed":
            return "closed"
        return state or "active"

    def _report_path(self, task: dict[str, Any]) -> Path:
        workspace = task.get("workspace")
        if isinstance(workspace, dict):
            report = str(workspace.get("report_file") or "").strip()
            if report:
                return Path(report).expanduser()
        task_id = str(task.get("id") or task.get("task_id") or "")
        return Path(self.store.task_dir(task_id)) / f"{task_id}-report.md"

    def _report_written(self, task: dict[str, Any]) -> bool:
        return self._report_path(task).is_file()

    def _notification_recorded(self, task_id: str) -> bool:
        for event in self._read_events(task_id):
            if event.get("event_type") != "notification_delivery":
                continue
            if event.get("terminal") is False:
                continue
            if event.get("notification_event") == "task.input_required":
                continue
            return True
        return False

    def _read_events(self, task_id: str) -> list[dict[str, Any]]:
        try:
            return self.store.read_events(task_id)
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return []

    def _persist_receipt(
        self,
        task: dict[str, Any],
        receipt: dict[str, Any],
        event_kind: str,
        occurred_at: str,
    ) -> None:
        task_id = str(task.get("id") or task.get("task_id") or "")
        extensions = dict(task.get("extensions") or {})
        session = dict(extensions.get(SESSION_EXTENSION_KEY) or {})
        session["cleanup"] = copy.deepcopy(receipt)
        extensions[SESSION_EXTENSION_KEY] = session
        task["extensions"] = extensions
        self.store.write_task(task_id, task)
        # Cleanup events live in a dedicated bounded log so they never evict
        # meaningful lifecycle events (e.g. terminal notification_delivery)
        # from the 1536-byte events.jsonl, which would flip the notification
        # gate mid-retry.
        task_dir = self.store.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        append_bounded_jsonl(
            task_dir / CLEANUP_EVENTS_FILE,
            {
                "event_type": CLEANUP_EVENT_TYPE,
                "task_id": task_id,
                "cleanup_event": str(event_kind),
                "state": receipt["state"],
                "capability": receipt["capability"],
                "strategy": receipt["strategy"],
                "attempts": int(receipt["attempts"]),
                "retryable": bool(receipt["retryable"]),
                "next_attempt_at": receipt["next_attempt_at"],
                "error_code": receipt["error_code"],
                "created_at": occurred_at,
            },
        )

    def _task_lock(self, task_id: str):
        try:
            code, iteration = split_task_ref(task_id)
        except ValueError:
            return contextlib.nullcontext()
        if iteration is None:
            return contextlib.nullcontext()
        task_dir = self.store.tasks_dir / code / iteration
        task_dir.mkdir(parents=True, exist_ok=True)
        lock_path = task_dir / CLEANUP_LOCK_NAME

        @contextlib.contextmanager
        def _locked() -> Any:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        return _locked()

    @staticmethod
    def _result(
        task_id: str,
        status: str,
        blockers: list[str],
        *,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "task_id": str(task_id),
            "status": status,
            "actioned": status in _ACTIONED_STATUSES,
            "blockers": list(blockers),
            "receipt": copy.deepcopy(receipt) if receipt else None,
        }
