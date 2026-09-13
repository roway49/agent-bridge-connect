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
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # pragma: no cover - POSIX is required by the config/runner runtime
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .adapters import ExecutorPort, SessionCleanupRequest, SessionCleanupResult
from .auxiliary_sessions import (
    AUXILIARY_EXTENSION_KEY,
    auxiliary_cleanup_strategy,
    read_auxiliary_ledger,
    redact_session_ref,
    transition_auxiliary_cleanup,
    validate_auxiliary_entry,
)
from .codex_session_cleanup import (
    CODEX_DESKTOP_UI_STALE_CODE,
    CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE,
    CODEX_SESSION_DELETE_FAILED_CODE,
    CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
)
from .codex_desktop_archive import (
    CODEX_DESKTOP_ARCHIVE_REJECTED,
    CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
    CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
)
from .execution_policy import (
    CLEANUP_STRATEGIES,
    CLEANUP_VERIFICATION_SIDES,
    MAX_SESSION_CLEANUP_ATTEMPTS,
    RESOLVED_CLEANUP_STATES,
    SESSION_EXTENSION_KEY,
    SESSION_RECEIPT_SOURCES,
    TERMINAL_SESSION_CLEANUP_STATUSES,
    _empty_cleanup_verification,
    cleanup_verification_public_view,
    normalize_cleanup_commands,
    normalize_cleanup_verification,
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


def _codex_archive_strategy(strategy: Any) -> bool:
    """Return True when this receipt runs the official archive-then-delete gate."""
    return str(strategy or "") == "official_session_archive_then_delete"


def _strict_codex_success_result(
    result: SessionCleanupResult,
    verification: dict[str, dict[str, str]] | None,
    commands: dict[str, dict[str, str]] | None = None,
) -> tuple[SessionCleanupResult, dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Fail closed unless Desktop archive and delete are acknowledged.

    Under the archive-then-delete strategy the command acknowledgements are
    the success proof; ``desktop_live`` becomes ``not_applicable`` and the
    backend/read/list sides are non-gating diagnostics.  A delete-only
    ``official_session_delete`` result keeps the historical strict sides.
    """
    checked = verification or normalize_cleanup_verification(None)
    checked_commands = normalize_cleanup_commands(commands) if commands else None
    if _codex_archive_strategy(result.strategy):
        statuses = {
            (checked_commands or {}).get("desktop_archive", {}).get("status"),
            (checked_commands or {}).get("delete", {}).get("status"),
        }
        if statuses <= {"acknowledged", "confirmed"} and None not in statuses:
            checked["desktop_live"] = {
                "status": "not_applicable",
                "checked_at": checked["desktop_live"]["checked_at"]
                or (checked_commands or {}).get("delete", {}).get("checked_at", ""),
            }
            return result, checked, checked_commands or normalize_cleanup_commands(None)
        # Command proof missing: choose the stable archive-scoped code.
        code = CODEX_DESKTOP_ARCHIVE_REJECTED
        return (
            SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy=result.strategy or "official_session_archive_then_delete",
                error_code=code,
                retryable=False,
                verification=checked,
                commands=checked_commands or normalize_cleanup_commands(None),
            ),
            checked,
            checked_commands or normalize_cleanup_commands(None),
        )
    cli_status = checked["cli"]["status"]
    backend_status = checked["desktop_backend"]["status"]
    live_status = checked["desktop_live"]["status"]
    if all(checked[side]["status"] == "absent" for side in CLEANUP_VERIFICATION_SIDES):
        return result, checked, checked_commands or {}
    if backend_status == "absent" and live_status == "present":
        code = CODEX_DESKTOP_UI_STALE_CODE
    elif cli_status == "present" or backend_status == "present":
        code = CODEX_SESSION_DELETE_STILL_PRESENT_CODE
    elif (
        cli_status == "absent"
        and backend_status in {"absent", "unavailable", "unverified"}
        and live_status in {"unknown", "unavailable", "unverified"}
    ):
        code = CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE
    else:
        code = CODEX_SESSION_DELETE_FAILED_CODE
    return (
        SessionCleanupResult(
            state="failed",
            capability="supported",
            strategy=result.strategy or "official_session_delete",
            error_code=code,
            retryable=False,
            verification=checked,
        ),
        checked,
        checked_commands or {},
    )


class SessionCleanupCoordinator:
    """Authoritative post-terminal session cleanup controller for one board."""

    def __init__(
        self,
        board_root: str | Path,
        *,
        executor_port: ExecutorPort | None = None,
        port_resolver: Callable[[str], ExecutorPort] | None = None,
        store: TaskStore | None = None,
        desktop_archive_broker: Any | None = None,
    ) -> None:
        self.board = Path(board_root).expanduser().resolve()
        self.store = store or TaskStore(self.board)
        self._executor_port = executor_port
        self._port_resolver = port_resolver or default_cleanup_port
        self._desktop_archive_broker = desktop_archive_broker

    # ------------------------------------------------------------------ time
    @staticmethod
    def _now(value: str | None = None) -> str:
        return _utc_now() if value is None else str(value)

    # ---------------------------------------------------------------- public
    def request_cleanup(
        self,
        task_id: str,
        *,
        now: str | None = None,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        """Run one authoritative cleanup pass for a single exact task.

        Re-reads the task/session from disk under the per-task lock, verifies
        every gate, and either performs a single transition + at most one
        Executor call, or returns a zero-side-effect skip.  When the task
        registers auxiliary sessions, the primary ``agentbc.session`` is
        processed first, then every auxiliary session deepest/newest first;
        auxiliary attempts continue even when the primary pass fails.
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
            primary = self._request_primary(task, occurred_at, force_retry=force_retry)
            if primary.get("status") == "skipped":
                # The shared terminal/report/notification/RunLease gates are not
                # met, so auxiliary sessions remain in use; return the primary
                # skip without touching the auxiliary ledger.
                return primary
            return self._request_auxiliary(task, primary, occurred_at, force_retry=force_retry)

    def _request_primary(
        self,
        task: dict[str, Any],
        occurred_at: str,
        *,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        """Run the existing primary ``agentbc.session`` cleanup pass."""
        task_id = str(task.get("id") or task.get("task_id") or "")
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
            if self._codex_waiting_for_desktop(session):
                return self._result(task_id, "waiting_for_desktop", [], receipt=pending)
            return self._execute(task_id, pending, occurred_at)

        if state == "failed":
            if blockers:
                return self._result(task_id, "skipped", blockers, receipt=receipt)
            execution = (task.get("extensions") or {}).get("agentbc.execution") or {}
            authoritative_ack = self._can_replace_failed_desktop_route(
                receipt,
                task_id=task_id,
                executor_run_id=str(execution.get("executor_run_id") or ""),
                session_id=str(session.get("session_id") or ""),
            )
            if not receipt["retryable"] and not authoritative_ack:
                return self._result(task_id, "final", [], receipt=receipt)
            if (
                not authoritative_ack
                and (
                    not receipt["next_attempt_at"]
                    or not _is_iso_utc(receipt["next_attempt_at"])
                )
            ):
                return self._result(task_id, "waiting", [], receipt=receipt)
            if (
                not authoritative_ack
                and not force_retry
                and _parse_utc(occurred_at) < _parse_utc(receipt["next_attempt_at"])
            ):
                return self._result(task_id, "waiting", [], receipt=receipt)
            if (
                receipt["attempts"] >= MAX_SESSION_CLEANUP_ATTEMPTS
                and not authoritative_ack
            ):
                return self._result(task_id, "final", [], receipt=receipt)
            pending = self._to_pending(
                task,
                session,
                occurred_at,
                authoritative_archive_ack=authoritative_ack,
            )
            self._persist_receipt(task, pending, "retry", occurred_at)
            if self._codex_waiting_for_desktop(session):
                return self._result(task_id, "waiting_for_desktop", [], receipt=pending)
            return self._execute(task_id, pending, occurred_at)

        if state == "pending":
            if blockers:
                return self._result(task_id, "skipped", blockers, receipt=receipt)
            # Runner restart reconstructs this work from the receipt but must
            # wait for a newly registered Desktop route. Once available, the
            # pending request is safe to replay under the same task lock.
            if self._codex_waiting_for_desktop(session):
                return self._result(task_id, "waiting_for_desktop", [], receipt=receipt)
            if str(session.get("executor") or "").strip().lower() == "codex":
                return self._execute(task_id, receipt, occurred_at)
            failed = self._crash_recovery_receipt(task, session, receipt, occurred_at)
            self._persist_receipt(task, failed, "interrupted", occurred_at)
            return self._result(task_id, "recovered", [], receipt=failed)

        return self._result(task_id, "noop", [], receipt=receipt)

    def maintain_board(
        self,
        *,
        now: str | None = None,
        force_retry: bool = False,
    ) -> list[dict[str, Any]]:
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
                result = self.request_cleanup(task_id, now=now, force_retry=force_retry)
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

    # ---------------------------------------------------------- auxiliary
    def _request_auxiliary(
        self,
        task: dict[str, Any],
        primary: dict[str, Any],
        occurred_at: str,
        *,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        """Process all registered auxiliary sessions deepest/newest first."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        # The primary pass re-reads and persists its own snapshot; refresh the
        # in-memory task so auxiliary writes never overwrite fresh primary state.
        fresh = self._read_task(task_id)
        if fresh is not None:
            task = fresh
        extensions = task.get("extensions")
        if not isinstance(extensions, dict) or AUXILIARY_EXTENSION_KEY not in extensions:
            return primary
        try:
            ledger = read_auxiliary_ledger(extensions)
        except ABCError:
            blocked = {
                "aux_id": "",
                "ref": "",
                "executor": "auxiliary",
                "status": "skipped",
                "actioned": False,
                "blockers": ["auxiliary_ledger_invalid"],
                "receipt": None,
            }
            return self._auxiliary_aggregate(primary, [blocked], task_id)
        entries = self._auxiliary_depth_order(
            self._primary_session_id(task),
            ledger.get("sessions") or [],
        )
        results: list[dict[str, Any]] = []
        for entry in entries:
            results.append(
                self._auxiliary_cleanup_pass(
                    task, entry, occurred_at, force_retry=force_retry
                )
            )
        return self._auxiliary_aggregate(primary, results, task_id)

    def _auxiliary_cleanup_pass(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        occurred_at: str,
        *,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        aux_id = str(entry.get("aux_id") or "")
        executor = str(entry.get("executor") or "").strip().lower()
        ref = redact_session_ref(str(entry.get("session_id") or ""))
        base = {"aux_id": aux_id, "executor": executor, "ref": ref}
        entry = self._auxiliary_terminal_entry(task, entry, occurred_at)
        try:
            receipt = read_session_cleanup_receipt(entry.get("cleanup"))
        except ABCError:
            return {
                **base,
                "status": "skipped",
                "actioned": False,
                "blockers": ["auxiliary_ledger_invalid"],
                "receipt": None,
            }
        state = receipt["state"]
        if state in RESOLVED_CLEANUP_STATES:
            return {**base, "status": "resolved", "actioned": False, "blockers": [], "receipt": receipt}
        blockers = self._auxiliary_gates(task, entry)

        if entry.get("retain") is True:
            return self._auxiliary_handle_retained(task, entry, blockers, occurred_at, base)

        if state == "not_requested":
            if blockers:
                return {**base, "status": "skipped", "actioned": False, "blockers": blockers, "receipt": receipt}
            pending = self._auxiliary_to_pending(task, entry, occurred_at)
            updated = self._auxiliary_with_receipt(entry, pending, occurred_at)
            self._persist_auxiliary(task, updated, "requested", occurred_at)
            if self._codex_waiting_for_desktop(entry):
                return {
                    **base,
                    "status": "waiting_for_desktop",
                    "actioned": True,
                    "blockers": [],
                    "receipt": pending,
                }
            return self._auxiliary_execute(task, updated, occurred_at, base)

        if state == "failed":
            if blockers:
                return {**base, "status": "skipped", "actioned": False, "blockers": blockers, "receipt": receipt}
            authoritative_ack = self._can_replace_failed_desktop_route(
                receipt,
                task_id=str(entry.get("owner_task_id") or ""),
                executor_run_id=str(entry.get("owner_run_id") or ""),
                session_id=str(entry.get("session_id") or ""),
            )
            if not receipt["retryable"] and not authoritative_ack:
                return {**base, "status": "final", "actioned": False, "blockers": [], "receipt": receipt}
            if (
                not authoritative_ack
                and (
                    not receipt["next_attempt_at"]
                    or not _is_iso_utc(receipt["next_attempt_at"])
                )
            ):
                return {**base, "status": "waiting", "actioned": False, "blockers": [], "receipt": receipt}
            if (
                not authoritative_ack
                and not force_retry
                and _parse_utc(occurred_at) < _parse_utc(receipt["next_attempt_at"])
            ):
                return {**base, "status": "waiting", "actioned": False, "blockers": [], "receipt": receipt}
            if (
                receipt["attempts"] >= MAX_SESSION_CLEANUP_ATTEMPTS
                and not authoritative_ack
            ):
                return {**base, "status": "final", "actioned": False, "blockers": [], "receipt": receipt}
            pending = self._auxiliary_to_pending(
                task,
                entry,
                occurred_at,
                authoritative_archive_ack=authoritative_ack,
            )
            updated = self._auxiliary_with_receipt(entry, pending, occurred_at)
            self._persist_auxiliary(task, updated, "retry", occurred_at)
            if self._codex_waiting_for_desktop(entry):
                return {
                    **base,
                    "status": "waiting_for_desktop",
                    "actioned": True,
                    "blockers": [],
                    "receipt": pending,
                }
            return self._auxiliary_execute(task, updated, occurred_at, base)

        if state == "pending":
            if blockers:
                return {**base, "status": "skipped", "actioned": False, "blockers": blockers, "receipt": receipt}
            if self._codex_waiting_for_desktop(entry):
                return {**base, "status": "waiting_for_desktop", "actioned": False, "blockers": [], "receipt": receipt}
            if str(entry.get("executor") or "").strip().lower() == "codex":
                return self._auxiliary_execute(task, entry, occurred_at, base)
            failed = self._auxiliary_crash_recovery(task, entry, receipt, occurred_at)
            updated = self._auxiliary_with_receipt(entry, failed, occurred_at)
            self._persist_auxiliary(task, updated, "interrupted", occurred_at)
            return {**base, "status": "recovered", "actioned": True, "blockers": [], "receipt": failed}

        return {**base, "status": "noop", "actioned": False, "blockers": [], "receipt": receipt}

    def _auxiliary_terminal_entry(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        """A bound auxiliary session is terminal once the task is terminal."""
        if str(task.get("status") or "") not in TERMINAL_SESSION_CLEANUP_STATUSES:
            return entry
        if not str(entry.get("session_id") or "").strip():
            return entry
        if str(entry.get("session_state") or "") == "terminal":
            return entry
        updated = copy.deepcopy(entry)
        updated["session_state"] = "terminal"
        updated["updated_at"] = occurred_at
        return updated

    def _auxiliary_handle_retained(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        blockers: list[str],
        occurred_at: str,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        receipt = read_session_cleanup_receipt(entry.get("cleanup"))
        if receipt["state"] != "not_requested":
            return {**base, "status": "resolved", "actioned": False, "blockers": [], "receipt": receipt}
        retention_blockers = [item for item in blockers if item != "retention_enabled"]
        if retention_blockers:
            return {**base, "status": "skipped", "actioned": False, "blockers": blockers, "receipt": receipt}
        retained = self._transition_auxiliary(
            task,
            entry,
            "retained",
            occurred_at,
        )
        updated = self._auxiliary_with_receipt(entry, retained, occurred_at)
        self._persist_auxiliary(task, updated, "retained", occurred_at)
        return {**base, "status": "retained", "actioned": True, "blockers": [], "receipt": retained}

    def _auxiliary_to_pending(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        occurred_at: str,
        *,
        authoritative_archive_ack: bool = False,
    ) -> dict[str, Any]:
        strategy = self._auxiliary_request_strategy(entry)
        return self._transition_auxiliary(
            task,
            entry,
            "pending",
            occurred_at,
            capability="supported",
            strategy=strategy
            or (
                "official_session_archive_then_delete"
                if str(entry.get("executor") or "").strip().lower() == "codex"
                else "official_session_delete"
            ),
            authoritative_archive_ack=authoritative_archive_ack,
        )

    def _auxiliary_crash_recovery(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        receipt: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        retryable, next_attempt_at = self._next_retry(receipt["attempts"], occurred_at)
        return self._transition_auxiliary(
            task,
            entry,
            "failed",
            occurred_at,
            capability=receipt["capability"] or "supported",
            strategy=receipt["strategy"] or "official_session_delete",
            error_code="session_cleanup_interrupted",
            retryable=retryable,
            next_attempt_at=next_attempt_at,
        )

    def _transition_auxiliary(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        target: str,
        occurred_at: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        task_id = str(task.get("id") or task.get("task_id") or "")
        return transition_auxiliary_cleanup(
            entry,
            target,
            task_status=str(task.get("status") or ""),
            lease_state=self._lease_state(task_id),
            occurred_at=occurred_at,
            **kwargs,
        )

    def _auxiliary_execute(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        occurred_at: str,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        request = self._auxiliary_build_request(task, entry)
        try:
            port = self._resolve_port(str(entry.get("executor") or ""))
            result = port.cleanup_session(request)
            if not isinstance(result, SessionCleanupResult):
                raise TypeError("cleanup_session must return SessionCleanupResult")
        except Exception:  # noqa: BLE001
            result = SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy=(entry.get("cleanup") or {}).get("strategy") or "official_session_delete",
                error_code="session_cleanup_failed",
                retryable=True,
            )
        return self._apply_auxiliary_result(task, entry, result, occurred_at, base)

    def _apply_auxiliary_result(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        result: SessionCleanupResult,
        occurred_at: str,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        current = read_session_cleanup_receipt(entry.get("cleanup"))
        if current["state"] != "pending":
            return {**base, "status": "superseded", "actioned": False, "blockers": [], "receipt": current}
        result_verification = (
            normalize_cleanup_verification(result.verification)
            if result.verification
            else None
        )
        result_commands = (
            normalize_cleanup_commands(result.commands)
            if result.commands
            else None
        )
        if result_verification is not None and str(
            entry.get("executor") or ""
        ).strip().lower() != "codex":
            result_verification = _empty_cleanup_verification("not_applicable")
            result_commands = normalize_cleanup_commands("not_applicable")
        if (
            result.state == "succeeded"
            and str(entry.get("executor") or "").strip().lower() == "codex"
        ):
            result, result_verification, result_commands = _strict_codex_success_result(
                result,
                result_verification,
                result_commands,
            )
        if result.state == "succeeded":
            new_receipt = self._transition_auxiliary(
                task,
                entry,
                "succeeded",
                occurred_at,
                capability="supported",
                strategy=_sanitize_strategy(result.strategy) or current["strategy"],
                verification=result_verification,
                commands=result_commands,
            )
        elif result.state == "unsupported":
            new_receipt = self._transition_auxiliary(
                task,
                entry,
                "unsupported",
                occurred_at,
                capability="unsupported",
                strategy="none",
                error_code=_sanitize_error_code(result.error_code, "session_cleanup_unsupported"),
                verification=result_verification,
                commands=result_commands,
            )
        else:
            retryable, next_attempt_at = (
                self._next_retry(current["attempts"], occurred_at)
                if result.retryable
                else (False, "")
            )
            new_receipt = self._transition_auxiliary(
                task,
                entry,
                "failed",
                occurred_at,
                capability=current["capability"] or "supported",
                strategy=current["strategy"] or self._auxiliary_request_strategy(entry),
                error_code=_sanitize_error_code(result.error_code),
                retryable=retryable,
                next_attempt_at=next_attempt_at,
                verification=result_verification,
                commands=result_commands,
            )
        updated = self._auxiliary_with_receipt(entry, new_receipt, occurred_at)
        self._persist_auxiliary(task, updated, "result", occurred_at)
        status = new_receipt["state"]
        return {**base, "status": status, "actioned": status != "failed", "blockers": [], "receipt": new_receipt}

    def _auxiliary_with_receipt(
        self,
        entry: dict[str, Any],
        receipt: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        updated = copy.deepcopy(entry)
        updated["cleanup"] = copy.deepcopy(receipt)
        updated["updated_at"] = occurred_at
        return updated

    def _auxiliary_gates(self, task: dict[str, Any], entry: dict[str, Any]) -> list[str]:
        task_id = str(task.get("id") or task.get("task_id") or "")
        blockers: list[str] = []
        if str(task.get("status") or "").strip().lower() not in TERMINAL_SESSION_CLEANUP_STATUSES:
            blockers.append("task_not_terminal")
        if str(self._lease_state(task_id) or "").strip().lower() != "closed":
            blockers.append("run_lease_not_closed")
        # FLOW-104-002: report/notification evidence is no longer an auxiliary
        # cleanup gate; terminal delivery is tracked independently by Runner.
        entry_errors = validate_auxiliary_entry(entry)
        if entry_errors:
            blockers.append("auxiliary_ledger_invalid")
            return blockers
        if (
            str(entry.get("executor") or "").strip().lower() == "codex"
            and str(entry.get("source") or "") != SESSION_RECEIPT_SOURCES["codex"]
        ):
            blockers.append("auxiliary_session_receipt_unbound")
        if entry.get("retain") is True:
            blockers.append("retention_enabled")
        if not str(entry.get("session_id") or "").strip():
            blockers.append("auxiliary_session_pending_reservation")
        cleanup = read_session_cleanup_receipt(entry.get("cleanup"))
        if cleanup["state"] in RESOLVED_CLEANUP_STATES:
            blockers.append("cleanup_already_resolved")
        return blockers

    def _auxiliary_build_request(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
    ) -> SessionCleanupRequest:
        task_id = str(task.get("id") or task.get("task_id") or "")
        retain = bool(entry.get("retain"))
        request = SessionCleanupRequest(
            executor=str(entry.get("executor") or ""),
            session_id=str(entry.get("session_id") or ""),
            task_id=task_id,
            executor_run_id=str(entry.get("owner_run_id") or ""),
            retain=retain,
            project_mode=str(entry.get("project_mode") or "none"),
            strategy=self._auxiliary_request_strategy(entry),
            project_path=str(entry.get("project_path") or ""),
            workspace=dict(task.get("workspace") or {}),
            receipt_source=str(entry.get("source") or ""),
            official_receipt_bound=bool(str(entry.get("source") or "").strip()),
            archive_acknowledged=entry.get("archive_acknowledged") is True,
            archive_checked_at=str(entry.get("archive_checked_at") or ""),
        )
        return request

    @staticmethod
    def _auxiliary_request_strategy(entry: dict[str, Any]) -> str:
        return auxiliary_cleanup_strategy(entry)

    def _auxiliary_depth_order(
        self,
        primary_session_id: str,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        depth = {str(primary_session_id or ""): 0}
        remaining = list(entries)
        ordered: list[tuple[int, str, dict[str, Any]]] = []
        for _ in range(len(entries) + 1):
            progressed = False
            for entry in list(remaining):
                parent = str(entry.get("parent_session_id") or "")
                if parent not in depth:
                    continue
                entry_depth = depth[parent] + 1
                session_id = str(entry.get("session_id") or "")
                if session_id:
                    depth[session_id] = entry_depth
                ordered.append((entry_depth, str(entry.get("created_at") or ""), entry))
                remaining.remove(entry)
                progressed = True
            if not progressed:
                break
        for entry in remaining:
            ordered.append((1, str(entry.get("created_at") or ""), entry))
        ordered.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in ordered]

    def _auxiliary_aggregate(
        self,
        primary: dict[str, Any],
        results: list[dict[str, Any]],
        task_id: str,
    ) -> dict[str, Any]:
        if not results:
            return primary
        unresolved_statuses = {"skipped", "failed", "final", "waiting", "recovered", "noop"}
        unresolved = [item for item in results if item["status"] in unresolved_statuses]
        combined = dict(primary)
        combined["auxiliary"] = list(results)
        combined["aggregate"] = {
            "total": len(results),
            "resolved": len(results) - len(unresolved),
            "unresolved": len(unresolved),
            "state": "blocked" if unresolved else "resolved",
        }
        if unresolved:
            combined["status"] = "blocked"
            combined["blockers"] = ["auxiliary_session_cleanup_incomplete"]
            combined["actioned"] = bool(primary.get("actioned")) or any(
                item.get("actioned") for item in results
            )
        return combined

    def _persist_auxiliary(
        self,
        task: dict[str, Any],
        entry: dict[str, Any],
        event_kind: str,
        occurred_at: str,
    ) -> None:
        """Persist one full auxiliary entry back into the task ledger."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        extensions = dict(task.get("extensions") or {})
        ledger = read_auxiliary_ledger(extensions)
        aux_id = str(entry.get("aux_id") or "")
        for index, item in enumerate(ledger["sessions"]):
            if str(item.get("aux_id") or "") == aux_id:
                ledger["sessions"][index] = copy.deepcopy(entry)
                break
        else:
            raise ABCError(
                "auxiliary_receipt_missing",
                "Cannot persist cleanup for an unknown auxiliary session.",
                {"aux_id": aux_id},
            )
        extensions[AUXILIARY_EXTENSION_KEY] = ledger
        task["extensions"] = extensions
        self.store.write_task(task_id, task)
        receipt = read_session_cleanup_receipt(entry.get("cleanup"))
        task_dir = self.store.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        append_bounded_jsonl(
            task_dir / CLEANUP_EVENTS_FILE,
            {
                "event_type": CLEANUP_EVENT_TYPE,
                "task_id": task_id,
                "aux_id": aux_id,
                "aux_ref": redact_session_ref(str(entry.get("session_id") or "")),
                "executor": str(entry.get("executor") or ""),
                "cleanup_event": str(event_kind),
                "state": receipt["state"],
                "capability": receipt["capability"],
                "strategy": receipt["strategy"],
                "attempts": int(receipt["attempts"]),
                "retryable": bool(receipt["retryable"]),
                "next_attempt_at": receipt["next_attempt_at"],
                "error_code": receipt["error_code"],
                "verification": cleanup_verification_public_view(receipt.get("verification")),
                # SESSION-104-001: auxiliary partial command evidence is
                # persisted with the same durability rules as the primary.
                "commands": {
                    command: dict((receipt.get("commands") or {}).get(command) or {})
                    for command in ("desktop_archive", "app_server_archive", "delete")
                },
                "created_at": occurred_at,
            },
        )

    def _primary_session_id(self, task: dict[str, Any]) -> str:
        session = self._authoritative_session(task)
        if session is None:
            return ""
        return str(session.get("session_id") or "")

    # ---------------------------------------------------------- pending steps
    def _to_pending(
        self,
        task: dict[str, Any],
        session: dict[str, Any],
        occurred_at: str,
        *,
        authoritative_archive_ack: bool = False,
    ) -> dict[str, Any]:
        request = self._build_request(task, session)
        return self._transition(
            session,
            "pending",
            task=task,
            occurred_at=occurred_at,
            capability="supported",
            strategy=request.strategy
            or (
                "official_session_archive_then_delete"
                if str(session.get("executor") or "").strip().lower() == "codex"
                else "official_session_delete"
            ),
            authoritative_archive_ack=authoritative_archive_ack,
        )

    def _can_replace_failed_desktop_route(
        self,
        receipt: dict[str, Any],
        *,
        task_id: str,
        executor_run_id: str,
        session_id: str,
    ) -> bool:
        """Accept one exact native ack after the old Desktop route exhausted retries."""
        broker = self._desktop_archive_broker
        if broker is None or getattr(broker, "authoritative_ack", False) is not True:
            return False
        if (
            str(getattr(broker, "task_id", "") or "").strip() != str(task_id or "").strip()
            or str(getattr(broker, "executor_run_id", "") or "").strip()
            != str(executor_run_id or "").strip()
            or str(getattr(broker, "session_id", "") or "").strip().lower()
            != str(session_id or "").strip().lower()
        ):
            return False
        commands = receipt.get("commands") if isinstance(receipt.get("commands"), dict) else {}
        desktop = commands.get("desktop_archive") if isinstance(commands, dict) else {}
        error_code = receipt.get("error_code")
        desktop_status = desktop.get("status") if isinstance(desktop, dict) else ""
        return (
            error_code
            in {
                CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
                CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
                CODEX_DESKTOP_ARCHIVE_REJECTED,
            }
            and desktop_status in {"rejected", "unavailable", "not_requested"}
        ) or (
            error_code == CODEX_SESSION_DELETE_FAILED_CODE
            and desktop_status in {"acknowledged", "confirmed"}
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
        except Exception:  # noqa: BLE001
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
        result_verification = (
            normalize_cleanup_verification(result.verification)
            if result.verification
            else None
        )
        result_commands = (
            normalize_cleanup_commands(result.commands)
            if result.commands
            else None
        )
        if result_verification is not None and str(
            session.get("executor") or ""
        ).strip().lower() != "codex":
            result_verification = _empty_cleanup_verification("not_applicable")
            result_commands = normalize_cleanup_commands("not_applicable")
        if (
            result.state == "succeeded"
            and str(session.get("executor") or "").strip().lower() == "codex"
        ):
            result, result_verification, result_commands = _strict_codex_success_result(
                result,
                result_verification,
                result_commands,
            )
        if result.state == "succeeded":
            new_receipt = self._transition(
                session,
                "succeeded",
                task=task,
                occurred_at=occurred_at,
                capability="supported",
                strategy=_sanitize_strategy(result.strategy) or pending["strategy"],
                verification=result_verification,
                commands=result_commands,
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
                verification=result_verification,
                commands=result_commands,
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
                verification=result_verification,
                commands=result_commands,
            )
        self._persist_receipt(task, new_receipt, "result", occurred_at)
        return self._result(task_id, new_receipt["state"], [], receipt=new_receipt)

    # --------------------------------------------------------------- helpers
    def _resolve_port(self, executor: str) -> ExecutorPort:
        if self._executor_port is not None:
            port = self._executor_port
        else:
            port = self._port_resolver(str(executor or "").strip())
        if str(executor or "").strip().lower() == "codex" and self._desktop_archive_broker is not None:
            try:
                setattr(port, "desktop_archive_broker", self._desktop_archive_broker)
            except (AttributeError, TypeError):
                pass
        return port

    def _codex_waiting_for_desktop(self, session_or_entry: dict[str, Any]) -> bool:
        if str(session_or_entry.get("executor") or "").strip().lower() != "codex":
            return False
        broker = self._desktop_archive_broker
        if broker is None:
            return True
        available = getattr(broker, "route_available", None)
        if callable(available):
            try:
                return not bool(available())
            except Exception:  # noqa: BLE001 - dead route is unavailable.
                return True
        # Test doubles that expose only archive() provide their own bounded
        # route verdict and should still be exercised.
        return False

    def _build_request(self, task: dict[str, Any], session: dict[str, Any]) -> SessionCleanupRequest:
        task_id = str(task.get("id") or task.get("task_id") or "")
        workspace = dict(task.get("workspace") or {})
        retain = bool(session.get("retain"))
        project_mode = str(session.get("project_mode") or "none")
        project_path = str(session.get("project_path") or "")
        extensions = task.get("extensions") if isinstance(task.get("extensions"), dict) else {}
        execution = extensions.get("agentbc.execution") if isinstance(extensions, dict) else {}
        return SessionCleanupRequest(
            executor=str(session.get("executor") or ""),
            session_id=str(session.get("session_id") or ""),
            task_id=task_id,
            executor_run_id=str((execution or {}).get("executor_run_id") or ""),
            retain=retain,
            project_mode=project_mode,
            strategy=self._request_strategy(session, retain, project_mode),
            project_path=project_path,
            workspace=workspace,
            receipt_source=str(session.get("receipt_source") or ""),
            official_receipt_bound=session.get("official_receipt_bound") is True,
            archive_acknowledged=session.get("archive_acknowledged") is True,
            archive_checked_at=str(session.get("archive_checked_at") or ""),
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
        if executor == "codex":
            # SESSION-104-001: official Codex cleanup always archives first.
            return "official_session_archive_then_delete"
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
        # FLOW-104-002: report_written / notification_recorded are no longer
        # cleanup gates.  They are independent ``agentbc.terminal_delivery``
        # stages owned by Runner; a report or notification failure must never
        # block executor-session cleanup.
        return session_cleanup_blockers(
            task_status=str(task.get("status") or ""),
            lease_state=self._lease_state(task_id),
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
                "verification": cleanup_verification_public_view(receipt.get("verification")),
                # SESSION-104-001: bounded per-command evidence survives in the
                # durable event log so retries and Runner restarts never lose
                # an acknowledged archive.
                "commands": {
                    command: dict((receipt.get("commands") or {}).get(command) or {})
                    for command in ("desktop_archive", "app_server_archive", "delete")
                },
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
