"""E2E/canary executor session supervisor (SESSION-103-002).

This module is the single supported AgentBC E2E/canary session supervisor.
Every test session is created through a receipt-driven two-phase path:

1. a durable reservation is journaled before ``thread/start``, ``session/new``,
   or Claude preallocation may run;
2. the official executor receipt is captured by the ``creator`` callback and
   atomically bound in the journal before any test action may proceed.

Teardown runs under ``try/finally`` plus SIGINT/SIGTERM/KeyboardInterrupt
handling, so success, Deny, timeout, transport loss, process exception, and
test interruption all reach the official exact-session cleanup adapter.  Only
an official ``succeeded`` cleanup result is cleanup proof: ``process.terminate()``,
deleting a ``/tmp`` canary root, or exit code 0 are never accepted.

On restart, ``replay_unresolved`` re-runs only this journal's own unresolved
exact receipts before a new session may be created.  The dispatcher
conversation is never collected or deleted, and public evidence uses stable
redacted session refs and safe cleanup fields only.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import signal
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .adapters import SessionCleanupRequest, SessionCleanupResult
from .auxiliary_sessions import (
    AUXILIARY_EXTENSION_KEY,
    auxiliary_cleanup_strategy,
    bind_auxiliary_receipt,
    build_auxiliary_ledger,
    mark_auxiliary_terminal,
    read_auxiliary_ledger,
    redact_session_ref,
    reserve_auxiliary_session,
    validate_auxiliary_entry,
)
from .execution_policy import (
    MAX_SESSION_CLEANUP_ATTEMPTS,
    RESOLVED_CLEANUP_STATES,
    build_session_cleanup_receipt,
    read_session_cleanup_receipt,
    session_cleanup_view,
    validate_session_cleanup_receipt,
)
from .protocol import ABCError


E2E_JOURNAL_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _add_delay(value: str, delay_s: int) -> str:
    return (
        _parse_utc(value) + timedelta(seconds=delay_s)
    ).isoformat().replace("+00:00", "Z")


def _sanitize_error_code(value: Any) -> str:
    text = str(value or "").strip()
    if text and all(char.isalnum() or char == "_" for char in text) and text[0].islower():
        return text[:64]
    return "session_cleanup_failed"


def _sanitize_strategy(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"official_session_delete", "claude_project_purge"}:
        return text
    return ""


def _validated_receipt(value: dict[str, Any]) -> dict[str, Any]:
    errors = validate_session_cleanup_receipt(value)
    if errors:
        raise ABCError(
            "invalid_auxiliary_session_cleanup_transition",
            "; ".join(errors),
            {"errors": errors},
        )
    return value


def default_e2e_journal_root() -> Path:
    """Return the durable journal root for standalone canary runs."""
    base = Path(os.environ.get("AGENTBC_E2E_JOURNAL_ROOT") or "").expanduser()
    if not base.is_absolute():
        base = Path(tempfile.gettempdir()) / "agentbc-e2e-journal"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


class CanarySessionJournal:
    """Durable append-only JSONL journal of one supervisor's own sessions.

    Each line is a complete auxiliary-session entry; the latest occurrence of
    an ``aux_id`` wins.  The journal is the replay source for unresolved exact
    receipts after a process restart.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _append(self, entry: dict[str, Any]) -> None:
        errors = validate_auxiliary_entry(entry)
        if errors:
            raise ABCError(
                "auxiliary_ledger_invalid",
                "journal entry is invalid",
                {"errors": errors},
            )
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def latest(self, aux_id: str) -> dict[str, Any]:
        match = None
        for entry in self.all():
            if str(entry.get("aux_id") or "") == aux_id:
                match = entry
        if match is None:
            raise ABCError(
                "auxiliary_receipt_missing",
                "Unknown journal session.",
                {"aux_id": aux_id},
            )
        return match

    _latest = latest

    def all(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not self.path.exists():
            return entries
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            entries.append(value)
        return entries

    def latest_entries(self) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for entry in self.all():
            by_id[str(entry.get("aux_id") or "")] = entry
        return list(by_id.values())

    def reserve(
        self,
        *,
        owner_task_id: str,
        owner_run_id: str,
        parent_executor: str,
        parent_session_id: str,
        executor: str,
        purpose: str,
        retain: bool,
        project_mode: str = "none",
        project_path: str = "",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        _, entry = reserve_auxiliary_session(
            extensions,
            owner_task_id=owner_task_id,
            owner_run_id=owner_run_id,
            parent_executor=parent_executor,
            parent_session_id=parent_session_id,
            executor=executor,
            purpose=purpose,
            retain=retain,
            project_mode=project_mode,
            project_path=project_path,
            created_at=created_at,
        )
        self._append(entry)
        return copy.deepcopy(entry)

    def bind(
        self,
        aux_id: str,
        receipt: Any,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        ledger = build_auxiliary_ledger()
        ledger["sessions"] = [self._latest(aux_id)]
        _, entry = bind_auxiliary_receipt(
            {AUXILIARY_EXTENSION_KEY: ledger},
            aux_id=aux_id,
            receipt=receipt,
            expected_session_id=expected_session_id,
        )
        self._append(entry)
        return copy.deepcopy(entry)

    def mark_terminal(self, aux_id: str) -> dict[str, Any]:
        ledger = build_auxiliary_ledger()
        ledger["sessions"] = [self._latest(aux_id)]
        _, entry = mark_auxiliary_terminal(
            {AUXILIARY_EXTENSION_KEY: ledger},
            aux_id=aux_id,
        )
        self._append(entry)
        return copy.deepcopy(entry)

    def update_cleanup(self, aux_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        entry = copy.deepcopy(self._latest(aux_id))
        entry["cleanup"] = copy.deepcopy(receipt)
        entry["updated_at"] = _utc_now()
        self._append(entry)
        return entry

    def unresolved(self) -> list[dict[str, Any]]:
        """Return this journal's own bound-but-not-confirmed-cleaned sessions."""
        unresolved: list[dict[str, Any]] = []
        for entry in self.latest_entries():
            if str(entry.get("session_id") or "").strip() == "":
                continue
            try:
                receipt = read_session_cleanup_receipt(entry.get("cleanup"))
            except ABCError:
                unresolved.append(entry)
                continue
            if receipt["state"] in RESOLVED_CLEANUP_STATES:
                continue
            unresolved.append(entry)
        return unresolved


class E2ESessionSupervisor:
    """Receipt-driven supervisor for AgentBC E2E/canary executor sessions."""

    def __init__(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        retain: bool = False,
        journal_path: str | Path | None = None,
        board_root: str | Path | None = None,
        cleanup_port: Any = None,
        cleanup_port_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.task_id = str(task_id or "").strip()
        self.run_id = str(run_id or "").strip()
        self.retain = bool(retain)
        self.board_root = Path(board_root).expanduser().resolve() if board_root else None
        if journal_path is not None:
            self.journal_path = Path(journal_path).expanduser().resolve()
        else:
            self.journal_path = default_e2e_journal_root() / f"{self._journal_stem()}.jsonl"
        self.journal = CanarySessionJournal(self.journal_path)
        self._cleanup_port = cleanup_port
        self._port_resolver = cleanup_port_resolver
        self._parent_executor = ""
        self._parent_session_id = ""

    def _journal_stem(self) -> str:
        base = str(self.task_id or self.run_id or uuid.uuid4().hex[:8])
        safe = "".join(char for char in base if char.isalnum() or char in "-_")[:64] or "canary"
        return f"{safe}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------ dispatcher
    def set_parent_session(self, executor: str, session_id: str) -> None:
        """Bind the parent (primary or dispatcher-adjacent) session for children."""
        self._parent_executor = str(executor or "").strip().lower()
        self._parent_session_id = str(session_id or "").strip()

    # ------------------------------------------------------------ two-phase
    def open_session(
        self,
        executor: str,
        purpose: str,
        creator: Callable[[], dict[str, Any]],
        *,
        parent_executor: str | None = None,
        parent_session_id: str | None = None,
        project_mode: str = "none",
        project_path: str = "",
    ) -> "AuxSessionHandle":
        """Reserve, create, and atomically bind one auxiliary session.

        ``creator`` must perform the official session creation and return the
        official executor receipt; the receipt is bound and durably journaled
        before ``open_session`` returns, so no test action can precede it.
        """
        parent_executor = (parent_executor or self._parent_executor or "claude").strip().lower()
        parent_session_id = str(parent_session_id or self._parent_session_id or "").strip()
        reserved = self.journal.reserve(
            owner_task_id=self.task_id or "e2e",
            owner_run_id=self.run_id or f"e2e-{uuid.uuid4().hex[:8]}",
            parent_executor=parent_executor,
            parent_session_id=parent_session_id,
            executor=executor,
            purpose=purpose,
            retain=self.retain,
            project_mode=project_mode,
            project_path=project_path,
        )
        receipt = creator()
        bound = self.journal.bind(reserved["aux_id"], receipt)
        self._sync_to_task_ledger(bound)
        return AuxSessionHandle(self, bound)

    def mark_terminal(self, handle: "AuxSessionHandle") -> dict[str, Any]:
        entry = self.journal.mark_terminal(handle.aux_id)
        self._sync_to_task_ledger(entry)
        return entry

    # ------------------------------------------------------------- replay
    def replay_unresolved(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Replay only this journal's own unresolved exact receipts.

        Must be called before creating a new session after a restart.
        """
        occurred_at = now or _utc_now()
        results: list[dict[str, Any]] = []
        for entry in self.journal.unresolved():
            results.append(self._cleanup_entry(entry, occurred_at=occurred_at))
        return results

    # ------------------------------------------------------------ teardown
    def teardown(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Clean every own unresolved session through the official adapter."""
        occurred_at = now or _utc_now()
        results: list[dict[str, Any]] = []
        for entry in self.journal.unresolved():
            results.append(self._cleanup_entry(entry, occurred_at=occurred_at))
        return results

    @contextlib.contextmanager
    def guarded(self, *, install_signals: bool = True):
        """Run a test body with try/finally teardown plus signal handling.

        SIGINT/SIGTERM are translated into ``KeyboardInterrupt`` so the
        ``finally`` teardown always runs.  Teardown itself is idempotent and
        runs under the existing signal handlers.
        """
        previous_int: Any = None
        previous_term: Any = None
        if install_signals:
            previous_int = signal.getsignal(signal.SIGINT)
            previous_term = signal.getsignal(signal.SIGTERM)

            def _interrupt(signum: int, _frame: Any) -> None:  # pragma: no cover
                raise KeyboardInterrupt

            signal.signal(signal.SIGINT, _interrupt)
            signal.signal(signal.SIGTERM, _interrupt)
        try:
            yield self
        finally:
            if install_signals:
                signal.signal(signal.SIGINT, previous_int)
                signal.signal(signal.SIGTERM, previous_term)
            self.teardown()

    # ---------------------------------------------------------- cleanup
    def _cleanup_entry(
        self,
        entry: dict[str, Any],
        *,
        occurred_at: str,
    ) -> dict[str, Any]:
        aux_id = str(entry.get("aux_id") or "")
        executor = str(entry.get("executor") or "").strip().lower()
        ref = redact_session_ref(str(entry.get("session_id") or ""))
        receipt = read_session_cleanup_receipt(copy.deepcopy(entry.get("cleanup")))
        if receipt["state"] in RESOLVED_CLEANUP_STATES:
            return {
                "aux_id": aux_id,
                "ref": ref,
                "executor": executor,
                "status": "resolved",
                "actioned": False,
                "blockers": [],
                "receipt": session_cleanup_view(receipt),
            }
        if entry.get("retain") is True:
            retained = self._retained_receipt(receipt, occurred_at)
            self.journal.update_cleanup(aux_id, retained)
            self._sync_to_task_ledger(self.journal._latest(aux_id))
            return {
                "aux_id": aux_id,
                "ref": ref,
                "executor": executor,
                "status": "retained",
                "actioned": False,
                "blockers": [],
                "receipt": session_cleanup_view(retained),
            }
        if not str(entry.get("session_id") or "").strip():
            return {
                "aux_id": aux_id,
                "ref": "",
                "executor": executor,
                "status": "skipped",
                "actioned": False,
                "blockers": ["auxiliary_session_pending_reservation"],
                "receipt": session_cleanup_view(receipt),
            }
        if receipt["state"] == "failed" and (
            not receipt["retryable"] or receipt["attempts"] >= MAX_SESSION_CLEANUP_ATTEMPTS
        ):
            return {
                "aux_id": aux_id,
                "ref": ref,
                "executor": executor,
                "status": "final",
                "actioned": False,
                "blockers": [],
                "receipt": session_cleanup_view(receipt),
            }
        pending = self._to_pending(entry, receipt, occurred_at)
        self.journal.update_cleanup(aux_id, pending)
        pending_entry = self.journal.latest(aux_id)
        self._sync_to_task_ledger(pending_entry)
        return self._execute_cleanup(pending_entry, pending, aux_id, ref, executor, occurred_at)

    def _execute_cleanup(
        self,
        entry: dict[str, Any],
        pending: dict[str, Any],
        aux_id: str,
        ref: str,
        executor: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        request = SessionCleanupRequest(
            executor=executor,
            session_id=str(entry.get("session_id") or ""),
            task_id=self.task_id,
            retain=bool(entry.get("retain")),
            project_mode=str(entry.get("project_mode") or "none"),
            strategy=str(pending["strategy"]) or auxiliary_cleanup_strategy(entry),
            project_path=str(entry.get("project_path") or ""),
        )
        try:
            port = self._resolve_port(executor)
            result = port.cleanup_session(request)
            if not isinstance(result, SessionCleanupResult):
                raise TypeError("cleanup_session must return SessionCleanupResult")
        except Exception:
            result = SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy=str(pending["strategy"]) or "official_session_delete",
                error_code="session_cleanup_failed",
                retryable=True,
            )
        return self._apply_result(entry, result, aux_id, ref, executor, occurred_at)

    def _apply_result(
        self,
        entry: dict[str, Any],
        result: SessionCleanupResult,
        aux_id: str,
        ref: str,
        executor: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        current = read_session_cleanup_receipt(copy.deepcopy(entry.get("cleanup")))
        if current["state"] != "pending":
            return {
                "aux_id": aux_id,
                "ref": ref,
                "executor": executor,
                "status": "superseded",
                "actioned": False,
                "blockers": [],
                "receipt": session_cleanup_view(current),
            }
        if result.state == "succeeded":
            updated = self._resolved_receipt(
                current,
                "succeeded",
                occurred_at,
                capability="supported",
                strategy=_sanitize_strategy(result.strategy) or current["strategy"],
            )
        elif result.state == "unsupported":
            updated = self._resolved_receipt(
                current,
                "unsupported",
                occurred_at,
                capability="unsupported",
                strategy="none",
                error_code=_sanitize_error_code(result.error_code) or "session_cleanup_unsupported",
            )
        else:
            retryable = bool(result.retryable) and current["attempts"] < MAX_SESSION_CLEANUP_ATTEMPTS
            next_attempt_at = _add_delay(occurred_at, 60) if retryable else ""
            updated = copy.deepcopy(current)
            updated.update(
                {
                    "capability": current["capability"] or "supported",
                    "strategy": current["strategy"] or "official_session_delete",
                    "state": "failed",
                    "last_attempt_at": occurred_at,
                    "completed_at": "",
                    "error_code": _sanitize_error_code(result.error_code),
                    "retryable": retryable,
                    "next_attempt_at": next_attempt_at,
                }
            )
            _validated_receipt(updated)
        self.journal.update_cleanup(aux_id, updated)
        self._sync_to_task_ledger(self.journal._latest(aux_id))
        status = updated["state"] if updated["state"] != "unsupported" else "unsupported"
        return {
            "aux_id": aux_id,
            "ref": ref,
            "executor": executor,
            "status": status,
            "actioned": updated["state"] == "succeeded",
            "blockers": [],
            "receipt": session_cleanup_view(updated),
        }

    def _to_pending(
        self,
        entry: dict[str, Any],
        receipt: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        updated = copy.deepcopy(receipt)
        updated.update(
            {
                "capability": receipt["capability"] or "supported",
                "strategy": receipt["strategy"] or auxiliary_cleanup_strategy(entry),
                "state": "pending",
                "attempts": receipt["attempts"] + 1,
                "requested_at": receipt["requested_at"] or occurred_at,
                "last_attempt_at": occurred_at,
                "next_attempt_at": "",
                "completed_at": "",
                "error_code": "",
                "retryable": False,
            }
        )
        return _validated_receipt(updated)

    def _resolved_receipt(
        self,
        receipt: dict[str, Any],
        state: str,
        occurred_at: str,
        *,
        capability: str,
        strategy: str,
        error_code: str = "",
    ) -> dict[str, Any]:
        updated = copy.deepcopy(receipt)
        updated.update(
            {
                "capability": capability,
                "strategy": strategy,
                "state": state,
                "last_attempt_at": occurred_at,
                "completed_at": occurred_at,
                "error_code": error_code,
                "retryable": False,
                "next_attempt_at": "",
            }
        )
        return _validated_receipt(updated)

    def _retained_receipt(self, receipt: dict[str, Any], occurred_at: str) -> dict[str, Any]:
        return _validated_receipt(
            {
                "version": receipt["version"],
                "capability": "not_applicable",
                "strategy": "retain",
                "state": "retained",
                "attempts": receipt["attempts"],
                "requested_at": receipt["requested_at"],
                "last_attempt_at": receipt["last_attempt_at"],
                "next_attempt_at": "",
                "completed_at": occurred_at,
                "error_code": "",
                "retryable": False,
            }
        )

    def _resolve_port(self, executor: str) -> Any:
        if self._cleanup_port is not None:
            return self._cleanup_port
        if self._port_resolver is not None:
            return self._port_resolver(str(executor or "").strip())
        from .session_cleanup import default_cleanup_port

        return default_cleanup_port(str(executor or "").strip())

    # ------------------------------------------------------ task integration
    def _sync_to_task_ledger(self, entry: dict[str, Any]) -> None:
        """Mirror one journal entry into the task-attached auxiliary ledger.

        Idempotent: the reservation/bind/terminal steps reuse the ledger's
        conflict-free duplicate semantics, and the cleanup receipt is copied
        from the journal's authoritative v1 receipt.
        """
        if self.board_root is None or not self.task_id:
            return
        from .service import TaskService

        service = TaskService(self.board_root)
        raw = service.store.read_task(self.task_id)
        extensions = dict(raw.get("extensions") or {})
        try:
            ledger = read_auxiliary_ledger(extensions)
        except ABCError:
            ledger = build_auxiliary_ledger()
        ext = dict(extensions)
        ext[AUXILIARY_EXTENSION_KEY] = ledger
        owner_run_id = str(entry.get("owner_run_id") or self.run_id or "e2e")
        ext, reserved = reserve_auxiliary_session(
            ext,
            owner_task_id=self.task_id,
            owner_run_id=owner_run_id,
            parent_executor=str(entry.get("parent_executor") or "claude"),
            parent_session_id=str(entry.get("parent_session_id") or ""),
            executor=str(entry.get("executor") or ""),
            purpose=str(entry.get("purpose") or ""),
            retain=bool(entry.get("retain")),
            project_mode=str(entry.get("project_mode") or "none"),
            project_path=str(entry.get("project_path") or ""),
            created_at=entry.get("created_at"),
        )
        task_aux_id = reserved["aux_id"]
        if str(entry.get("session_id") or "").strip():
            ext, _ = bind_auxiliary_receipt(
                ext,
                aux_id=task_aux_id,
                receipt={
                    "version": 1,
                    "executor": entry["executor"],
                    "session_id": entry["session_id"],
                    "resumed": False,
                    "persistence": "persistent",
                    "source": str(entry.get("source") or ""),
                },
                expected_session_id=str(entry.get("session_id") or ""),
            )
        if entry.get("session_state") == "terminal":
            ext, _ = mark_auxiliary_terminal(ext, aux_id=task_aux_id)
        ledger = read_auxiliary_ledger(ext)
        for item in ledger["sessions"]:
            if str(item.get("aux_id") or "") != task_aux_id:
                continue
            item["cleanup"] = copy.deepcopy(entry.get("cleanup") or build_session_cleanup_receipt())
            item["session_state"] = str(entry.get("session_state") or item.get("session_state") or "active")
            break
        ext[AUXILIARY_EXTENSION_KEY] = ledger
        raw["extensions"] = ext
        service.store.write_task(self.task_id, raw)


class AuxSessionHandle:
    """Handle for one bound auxiliary session; exposes redacted public fields."""

    def __init__(self, supervisor: E2ESessionSupervisor, entry: dict[str, Any]) -> None:
        self._supervisor = supervisor
        self.aux_id = str(entry.get("aux_id") or "")
        self.executor = str(entry.get("executor") or "").strip().lower()
        self.session_id = str(entry.get("session_id") or "")
        self.purpose = str(entry.get("purpose") or "")
        self.source = str(entry.get("source") or "")
        self.retain = bool(entry.get("retain"))
        self.session_state = str(entry.get("session_state") or "").strip().lower()

    @property
    def ref(self) -> str:
        return redact_session_ref(self.session_id)

    def public_view(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "executor": self.executor,
            "purpose": self.purpose,
            "retain": self.retain,
            "session_state": self.session_state,
            "cleanup": session_cleanup_view(
                self._supervisor.journal._latest(self.aux_id).get("cleanup")
            ),
        }

    def mark_terminal(self) -> dict[str, Any]:
        return self._supervisor.mark_terminal(self)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"AuxSessionHandle(executor={self.executor!r}, "
            f"ref={self.ref!r}, purpose={self.purpose!r}, retain={self.retain!r})"
        )


__all__ = [
    "AuxSessionHandle",
    "CanarySessionJournal",
    "E2E_JOURNAL_VERSION",
    "E2ESessionSupervisor",
    "default_e2e_journal_root",
]
