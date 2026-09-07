"""Runner-owned terminal delivery coordinator (FLOW-104-002).

This module turns the durable ``agentbc.terminal_delivery`` receipt into real
terminal side effects.  It is deliberately the *only* production owner of
terminal delivery replay:

- it attempts delivery immediately when a terminal task write lands;
- during Runner maintenance it replays only stages that are not confirmed and
  whose backoff has elapsed (immediate, 60s, then capped 300s);
- confirmed stages are never repeated;
- an interrupted ``in_progress`` noninteractive notification may be retried once
  with ``delivery_uncertain`` evidence;
- it never processes ``input_required`` tasks and never mutates a
  ``needs_recovery`` session;
- it never changes a business terminal state, a final callback, step results,
  session terminal state, a RunLease, or the ``agentbc.session`` cleanup state.

Cleanup stays owned exclusively by ``agentbc.session.cleanup`` and the
auxiliary cleanup receipts.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import fcntl  # noqa: I001  (POSIX-only runtime, mirrors session_cleanup)
from pathlib import Path
from typing import Any, Callable

from .protocol import ABCError
from .record_management import append_bounded_jsonl
from .run_lease import load_lease
from .task_id import split_task_ref
from .task_store import TaskStore
from .terminal_delivery import (
    DELIVERY_EVENT_TYPE,
    DELIVERY_EVENTS_FILE,
    DELIVERY_LOCK_NAME,
    RESOLVED_TERMINAL_DELIVERY_STAGES,
    TERMINAL_DELIVERY_EXTENSION_KEY,
    TERMINAL_DELIVERY_MAX_ATTEMPTS,
    TERMINAL_DELIVERY_NOTIFICATION_STAGES,
    TERMINAL_DELIVERY_STAGES,
    StageExecutors,
    StageOutcome,
    build_terminal_delivery_receipt,
    delivery_event_payload,
    import_legacy_terminal_delivery,
    pending_terminal_delivery_stages,
    read_terminal_delivery_receipt,
    reconcile_interrupted_stages,
    run_delivery_stages,
    terminal_delivery_eligible,
)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TerminalDeliveryCoordinator:
    """Authoritative terminal delivery controller for one board."""

    def __init__(
        self,
        board_root: str | Path,
        *,
        store: TaskStore | None = None,
        stage_executors: StageExecutors | None = None,
        file_notifier: Callable[[dict[str, Any]], StageOutcome] | None = None,
        ui_notifier: Callable[[dict[str, Any]], StageOutcome] | None = None,
    ) -> None:
        self.board = Path(board_root).expanduser().resolve()
        self.store = store or TaskStore(self.board)
        self._stage_executors = dict(stage_executors or {})
        self._file_notifier = file_notifier
        self._ui_notifier = ui_notifier

    # ------------------------------------------------------------------ public
    def receipt(self, task_id: str) -> dict[str, Any]:
        """Read the authoritative receipt for one task (legacy-aware)."""
        task = self._read_task(task_id)
        if task is None:
            return {}
        return self._authoritative_receipt(task)

    def attach_receipt(
        self,
        task: dict[str, Any],
        *,
        terminal_state: str,
        terminal_event: str,
        committed_at: str | None = None,
    ) -> dict[str, Any]:
        """Build the receipt to embed in the same authoritative task write.

        Pure: returns a detached receipt for the caller to store.
        """
        task_id = str(task.get("id") or task.get("task_id") or "")
        return build_terminal_delivery_receipt(
            task_id,
            terminal_state=terminal_state,
            terminal_event=terminal_event,
            committed_at=committed_at,
        )

    def deliver_now(
        self,
        task_id: str,
        *,
        now: str | None = None,
        executors: StageExecutors | None = None,
    ) -> dict[str, Any]:
        """Attempt delivery immediately for one exact terminal task."""
        return self._pass(task_id, now=now, executors=executors)

    def maintain_board(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Scan this board for terminal tasks with incomplete delivery stages.

        Tasks whose receipt is already fully confirmed are skipped without
        taking the per-task lock, so a fully delivered board costs one cheap
        read per task per maintenance pass.
        """
        results: list[dict[str, Any]] = []
        for task in self._list_terminal_tasks():
            task_id = str(task.get("id") or task.get("task_id") or "")
            if not task_id:
                continue
            try:
                if not pending_terminal_delivery_stages(self._authoritative_receipt(task)):
                    continue
                result = self.deliver_now(task_id, now=now)
            except (ABCError, OSError, ValueError, json.JSONDecodeError):
                continue
            if result.get("attempted"):
                results.append(result)
        return results

    # ------------------------------------------------------------------- pass
    def _pass(
        self,
        task_id: str,
        *,
        now: str | None = None,
        executors: StageExecutors | None = None,
    ) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not task_id:
            return {"task_id": "", "status": "skipped", "blockers": ["task_id_missing"], "attempted": []}
        if not self.store.task_exists(task_id):
            return {"task_id": task_id, "status": "skipped", "blockers": ["task_not_found"], "attempted": []}
        occurred_at = _sanitize_now(now)
        with self._task_lock(task_id):
            task = self._read_task(task_id)
            if task is None:
                return {
                    "task_id": task_id,
                    "status": "skipped",
                    "blockers": ["task_read_failed"],
                    "attempted": [],
                }
            blockers = self._eligibility_blockers(task)
            if blockers:
                return {
                    "task_id": task_id,
                    "status": "skipped",
                    "blockers": blockers,
                    "attempted": [],
                }
            receipt = self._authoritative_receipt(task)
            try:
                # A persisted ``in_progress`` reservation is evidence the process
                # died between the stage reservation and its result.  Noninteractive
                # notification stages are recorded with delivery_uncertain evidence
                # and retried at the capped backoff; pure recomputations retry now.
                receipt, reconciled = reconcile_interrupted_stages(
                    receipt, now=occurred_at
                )
                if reconciled:
                    self._persist_receipt(
                        task,
                        receipt,
                        [
                            {
                                "stage": stage,
                                "status": "reconciled",
                                "error_code": "delivery_stage_uncertain",
                                "delivery_uncertain": True,
                            }
                            for stage in reconciled
                        ],
                        occurred_at,
                    )
                updated, results = run_delivery_stages(
                    receipt,
                    self._executors(task, executors or {}),
                    now=occurred_at,
                )
            except ABCError as exc:
                return {
                    "task_id": task_id,
                    "status": "skipped",
                    "blockers": [str(exc.code or "terminal_delivery_invalid")],
                    "attempted": [],
                }
            attempted = [item for item in results if item.get("status") != "skipped"]
            if attempted:
                self._persist_receipt(task, updated, results, occurred_at)
            return {
                "task_id": task_id,
                "status": "delivered" if not [
                    item
                    for item in updated["stages"].values()
                    if item["state"] not in RESOLVED_TERMINAL_DELIVERY_STAGES
                ] else "partial",
                "delivery_id": updated["delivery_id"],
                "receipt": updated,
                "attempted": attempted,
                "blockers": [],
            }

    # ------------------------------------------------------------ eligibility
    def _eligibility_blockers(self, task: dict[str, Any]) -> list[str]:
        """Fail-closed gates; input_required and needs_recovery are never touched."""
        blockers: list[str] = []
        if not terminal_delivery_eligible(task):
            blockers.append("task_not_business_terminal")
        session = (task.get("extensions") or {}).get("agentbc.session")
        if isinstance(session, dict) and str(session.get("session_state") or "") == "needs_recovery":
            blockers.append("session_needs_recovery")
        return blockers

    def _authoritative_receipt(self, task: dict[str, Any]) -> dict[str, Any]:
        """Read the stored receipt, importing legacy evidence when absent.

        A legacy import never replays a historical UI dialog: an existing report
        or terminal ``notification_delivery`` event satisfies the matching
        stage, otherwise historical UI delivery is ``not_applicable``.
        """
        extensions = task.get("extensions") if isinstance(task.get("extensions"), dict) else {}
        stored = extensions.get(TERMINAL_DELIVERY_EXTENSION_KEY)
        if isinstance(stored, dict):
            try:
                return read_terminal_delivery_receipt(stored)
            except ABCError:
                pass
        return import_legacy_terminal_delivery(
            stored,
            task=task,
            events=self._read_events(str(task.get("id") or "")),
        )

    def _executors(
        self, task: dict[str, Any], overrides: StageExecutors
    ) -> StageExecutors:
        resolved: StageExecutors = dict(self._stage_executors)
        resolved.update(overrides)
        resolved.setdefault("report", self._report_executor(task))
        resolved.setdefault("record", self._record_executor(task))
        resolved.setdefault("index", self._index_executor(task))
        resolved.setdefault("file_notification", self._file_executor(task))
        resolved.setdefault("ui_notification", self._ui_executor(task))
        return resolved

    # ------------------------------------------------------- stage executors
    def _report_executor(self, task: dict[str, Any]) -> Callable[[], StageOutcome]:
        task_id = str(task.get("id") or "")

        def _run() -> StageOutcome:
            from .reports import write_report_markdown

            report, _markdown = write_report_markdown(task_id, self.board)
            report_file = str((report.get("workspace") or {}).get("report_file") or "")
            if report_file and not Path(report_file).expanduser().is_file():
                return StageOutcome(False, error_code="report_missing")
            return StageOutcome(True)

        return _run

    def _record_executor(self, task: dict[str, Any]) -> Callable[[], StageOutcome]:
        task_id = str(task.get("id") or "")

        def _run() -> StageOutcome:
            from .reports import compact_task_record

            compact_task_record(task_id, self.board)
            return StageOutcome(True)

        return _run

    def _index_executor(self, task: dict[str, Any]) -> Callable[[], StageOutcome]:
        def _run() -> StageOutcome:
            from .reports import refresh_board_index

            refresh_board_index(self.board)
            return StageOutcome(True)

        return _run

    def _file_executor(self, task: dict[str, Any]) -> Callable[[], StageOutcome]:
        task_id = str(task.get("id") or "")

        def _run() -> StageOutcome:
            from .adapters import DeliveryResult
            from .notifications import build_notification_payload
            from .service import TaskService

            service = TaskService(self.board)
            payload = build_notification_payload(
                service, task_id, "task.terminal", "info", ""
            )
            notifier = self._file_notifier
            if notifier is None:
                from .notifiers.file import FileNotifier

                def notifier(payload: dict[str, Any]) -> StageOutcome:
                    result: DeliveryResult = FileNotifier(
                        self.board / "notifications.jsonl"
                    ).send(payload)
                    return StageOutcome(bool(result.ok), error_code="file_notification_failed")

            return notifier(payload)

        return _run

    def _ui_executor(self, task: dict[str, Any]) -> Callable[[], StageOutcome]:
        task_id = str(task.get("id") or "")

        def _run() -> StageOutcome:
            from .notifications import build_notification_payload
            from .service import TaskService

            service = TaskService(self.board)
            payload = build_notification_payload(
                service, task_id, "task.terminal", "info", ""
            )
            notifier = self._ui_notifier
            if notifier is None:
                from .notifiers.dialog import DialogNotifier

                def notifier(payload: dict[str, Any]) -> StageOutcome:
                    result = DialogNotifier().send(payload)
                    if result.ok:
                        return StageOutcome(True)
                    # A noninteractive dialog may genuinely be unobservable
                    # after an interrupted process: record the uncertainty
                    # instead of asserting a confirmed delivery.
                    return StageOutcome(
                        False,
                        error_code="ui_notification_failed",
                        delivery_uncertain=True,
                    )

            return notifier(payload)

        return _run

    # ------------------------------------------------------------- persistence
    def _persist_receipt(
        self,
        task: dict[str, Any],
        receipt: dict[str, Any],
        results: list[dict[str, Any]],
        occurred_at: str,
    ) -> None:
        task_id = str(task.get("id") or task.get("task_id") or "")
        updated = copy.deepcopy(task)
        extensions = dict(updated.get("extensions") or {})
        extensions[TERMINAL_DELIVERY_EXTENSION_KEY] = copy.deepcopy(receipt)
        updated["extensions"] = extensions
        self.store.write_task(task_id, updated)
        task_dir = self.store.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        append_bounded_jsonl(
            task_dir / DELIVERY_EVENTS_FILE,
            delivery_event_payload(receipt, results, occurred_at=occurred_at),
        )

    # -------------------------------------------------------------- utilities
    def _read_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.store.read_task(task_id)
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return None

    def _list_terminal_tasks(self) -> list[dict[str, Any]]:
        try:
            tasks = self.store.list_tasks()
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return []
        return [task for task in tasks if terminal_delivery_eligible(task)]

    def _read_events(self, task_id: str) -> list[dict[str, Any]]:
        try:
            return self.store.read_events(task_id)
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            return []

    def _task_lock(self, task_id: str):
        try:
            code, iteration = split_task_ref(task_id)
        except ValueError:
            return contextlib.nullcontext()
        if iteration is None:
            return contextlib.nullcontext()
        task_dir = self.store.tasks_dir / code / iteration
        task_dir.mkdir(parents=True, exist_ok=True)
        lock_path = task_dir / DELIVERY_LOCK_NAME

        @contextlib.contextmanager
        def _locked() -> Any:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

        return _locked()


def _sanitize_now(value: str | None) -> str:
    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _utc_now()
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def lease_is_closed(task_id: str, board_root: str | Path) -> bool:
    """Return whether the RunLease for one task is authoritatively closed."""
    try:
        lease = load_lease(task_id, Path(board_root).expanduser().resolve())
    except (ABCError, OSError, ValueError, json.JSONDecodeError):
        return False
    if lease is None:
        return False
    return str(getattr(lease, "state", "") or "") == "closed"


__all__ = [
    "DELIVERY_EVENT_TYPE",
    "DELIVERY_EVENTS_FILE",
    "DELIVERY_LOCK_NAME",
    "TERMINAL_DELIVERY_EXTENSION_KEY",
    "TERMINAL_DELIVERY_MAX_ATTEMPTS",
    "TERMINAL_DELIVERY_NOTIFICATION_STAGES",
    "TERMINAL_DELIVERY_STAGES",
    "TerminalDeliveryCoordinator",
    "lease_is_closed",
]
