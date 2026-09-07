"""FLOW-104-002: independent terminal delivery and cleanup.

Covers the ``agentbc.terminal_delivery`` v1 durable receipt, the independent
report / record / index / file_notification / ui_notification stages, the
decoupling of terminal side effects from the business terminal state, the
removal of ``report_written`` / ``notification_recorded`` as session-cleanup
gates, and the Runner-owned replay coordinator.

Every test uses a temporary task board and injected stage executors; no real
Executor, dialog, or notification service is invoked.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_policy import (
    SESSION_EXTENSION_KEY,
    public_task_view,
)
from agent_bridge_connect.terminal_delivery import (
    DELIVERY_EVENTS_FILE,
    TERMINAL_DELIVERY_EXTENSION_KEY,
    TERMINAL_DELIVERY_MAX_ATTEMPTS,
    TERMINAL_DELIVERY_STAGES,
    StageOutcome,
    build_terminal_delivery_receipt,
    delivery_health_view,
    import_legacy_terminal_delivery,
    read_terminal_delivery_receipt,
    reconcile_interrupted_stages,
    run_delivery_stages,
    terminal_delivery_eligible,
    terminal_delivery_view,
    transition_terminal_delivery_stage,
)
from agent_bridge_connect.terminal_delivery_coordinator import (
    TerminalDeliveryCoordinator,
)
from agent_bridge_connect.notifications import notify_input_required
from agent_bridge_connect.reports import generate_report

T0 = "2026-09-01T00:00:00Z"
HERMES_SESSION_ID = "20260901_000000_a1b2c3"


def _at(seconds: int) -> str:
    parsed = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class _Stub:
    """Minimal service double for notification payload construction."""

    def __init__(self, board: Path, task: dict) -> None:
        self.board_root = board
        self._task = task

    def get_task(self, task_id: str):
        from agent_bridge_connect.service import TaskModel

        return TaskModel.from_dict(copy.deepcopy(self._task))


class TerminalDeliveryReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = build_terminal_delivery_receipt(
            "YBNW-001",
            terminal_state="completed",
            terminal_event="task.finalized",
            committed_at=T0,
        )

    def test_receipt_has_one_stable_identity_and_frozen_terminal_facts(self) -> None:
        again = build_terminal_delivery_receipt(
            "YBNW-001",
            terminal_state="completed",
            terminal_event="task.finalized",
            committed_at=T0,
        )
        self.assertNotEqual(self.receipt["delivery_id"], again["delivery_id"])
        self.assertEqual(self.receipt["terminal_state"], "completed")
        self.assertEqual(self.receipt["terminal_event"], "task.finalized")
        self.assertEqual(self.receipt["committed_at"], T0)

    def test_receipt_starts_with_five_pending_bounded_stages(self) -> None:
        self.assertEqual(tuple(self.receipt["stages"]), TERMINAL_DELIVERY_STAGES)
        for stage in TERMINAL_DELIVERY_STAGES:
            entry = self.receipt["stages"][stage]
            self.assertEqual(entry["state"], "pending")
            self.assertEqual(entry["attempts"], 0)
            self.assertEqual(entry["next_attempt_at"], "")
            self.assertEqual(entry["last_error_code"], "")

    def test_receipt_is_bounded_and_carries_no_payload_or_path(self) -> None:
        blob = json.dumps(self.receipt)
        for forbidden in ("command", "prompt", "message", "body", "/Users/"):
            self.assertNotIn(forbidden, blob)
        self.assertLess(len(blob.encode("utf-8")), 1024)

    def test_confirmed_stage_never_repeats(self) -> None:
        confirmed = transition_terminal_delivery_stage(
            self.receipt, "report", "succeeded"
        )
        replayed = transition_terminal_delivery_stage(confirmed, "report", "pending")
        self.assertEqual(replayed["stages"]["report"]["state"], "succeeded")
        self.assertEqual(
            confirmed["stages"]["report"]["attempts"],
            replayed["stages"]["report"]["attempts"],
        )

    def test_in_progress_reservation_counts_one_attempt(self) -> None:
        reserved = transition_terminal_delivery_stage(self.receipt, "index", "in_progress")
        self.assertEqual(reserved["stages"]["index"]["attempts"], 1)
        succeeded = transition_terminal_delivery_stage(reserved, "index", "succeeded")
        self.assertEqual(succeeded["stages"]["index"]["attempts"], 1)

    def test_backoff_is_immediate_then_60s_then_capped_300s(self) -> None:
        # Attempt 1 fails -> immediate retry (empty next_attempt_at).
        first = transition_terminal_delivery_stage(
            self.receipt, "record", "in_progress", occurred_at=T0
        )
        self.assertEqual(first["stages"]["record"]["attempts"], 1)
        failed_once = transition_terminal_delivery_stage(
            first, "record", "retry_wait", error_code="record_compaction_failed", occurred_at=T0
        )
        self.assertEqual(failed_once["stages"]["record"]["state"], "retry_wait")
        self.assertEqual(failed_once["stages"]["record"]["next_attempt_at"], "")
        # Attempt 2 fails -> earliest 60s.
        second = transition_terminal_delivery_stage(
            failed_once, "record", "in_progress", occurred_at=T0
        )
        self.assertEqual(second["stages"]["record"]["attempts"], 2)
        failed_twice = transition_terminal_delivery_stage(
            second, "record", "retry_wait", error_code="record_compaction_failed", occurred_at=T0
        )
        self.assertEqual(reserved_backoff(failed_twice), 60)
        # Attempt 3 fails -> capped at 300s.
        third = transition_terminal_delivery_stage(failed_twice, "record", "in_progress")
        self.assertEqual(third["stages"]["record"]["attempts"], TERMINAL_DELIVERY_MAX_ATTEMPTS)
        failed_thrice = transition_terminal_delivery_stage(
            third,
            "record",
            "retry_wait",
            error_code="record_compaction_failed",
            occurred_at=T0,
        )
        self.assertEqual(reserved_backoff(failed_thrice), 300)
        self.assertEqual(
            failed_thrice["stages"]["record"]["next_attempt_at"],
            _at(300),
        )

    def test_attempt_limit_blocks_further_reservations(self) -> None:
        receipt = self.receipt
        for _ in range(TERMINAL_DELIVERY_MAX_ATTEMPTS):
            receipt = transition_terminal_delivery_stage(receipt, "index", "in_progress")
        self.assertEqual(
            receipt["stages"]["index"]["attempts"], TERMINAL_DELIVERY_MAX_ATTEMPTS
        )
        with self.assertRaises(Exception):
            transition_terminal_delivery_stage(receipt, "index", "in_progress")

    def test_interrupted_notification_stage_is_reconciled_with_uncertainty(self) -> None:
        reserved = transition_terminal_delivery_stage(
            self.receipt, "ui_notification", "in_progress", occurred_at=T0
        )
        reconciled, stages = reconcile_interrupted_stages(reserved, now=_at(10))
        self.assertEqual(stages, ["ui_notification"])
        entry = reconciled["stages"]["ui_notification"]
        self.assertEqual(entry["state"], "retry_wait")
        self.assertEqual(entry["last_error_code"], "delivery_stage_uncertain")
        self.assertEqual(reserved_backoff(reconciled, "ui_notification"), 300)

    def test_interrupted_pure_stage_returns_to_pending(self) -> None:
        reserved = transition_terminal_delivery_stage(
            self.receipt, "record", "in_progress", occurred_at=T0
        )
        reconciled, stages = reconcile_interrupted_stages(reserved, now=_at(10))
        self.assertEqual(stages, ["record"])
        self.assertEqual(reconciled["stages"]["record"]["state"], "pending")

    def test_run_delivery_stages_isolates_each_stage_failure(self) -> None:
        executors = {
            "report": lambda: StageOutcome(True),
            "record": lambda: StageOutcome(False, error_code="record_budget_exceeded"),
            "index": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            "file_notification": lambda: StageOutcome(
                False, error_code="file_notification_failed"
            ),
            "ui_notification": lambda: StageOutcome(True),
        }
        updated, results = run_delivery_stages(self.receipt, executors, now=T0)
        statuses = {item["stage"]: item["status"] for item in results}
        self.assertEqual(statuses["report"], "succeeded")
        self.assertEqual(statuses["ui_notification"], "succeeded")
        self.assertEqual(updated["stages"]["record"]["last_error_code"], "record_budget_exceeded")
        self.assertEqual(
            updated["stages"]["file_notification"]["last_error_code"],
            "file_notification_failed",
        )
        # A stage that raised is still bounded and scheduled, never fatal.
        self.assertEqual(
            updated["stages"]["index"]["state"],
            "retry_wait",
        )

    def test_run_delivery_stages_never_repeats_a_confirmed_stage(self) -> None:
        receipt = transition_terminal_delivery_stage(self.receipt, "report", "succeeded")
        calls: list[str] = []

        def _report() -> StageOutcome:
            calls.append("report")
            return StageOutcome(True)

        updated, results = run_delivery_stages(
            receipt, {"report": _report}, now=T0
        )
        self.assertEqual(calls, [])
        self.assertEqual(results, [])
        self.assertEqual(updated["stages"]["report"]["state"], "succeeded")

    def test_delivery_eligibility_excludes_input_required_and_recovery(self) -> None:
        self.assertTrue(terminal_delivery_eligible({"status": "completed"}))
        self.assertTrue(terminal_delivery_eligible({"status": "failed"}))
        self.assertTrue(terminal_delivery_eligible({"status": "cancelled"}))
        self.assertTrue(terminal_delivery_eligible({"status": "rejected"}))
        self.assertFalse(terminal_delivery_eligible({"status": "input_required"}))
        self.assertFalse(terminal_delivery_eligible({"status": "needs_recovery"}))
        self.assertFalse(terminal_delivery_eligible({"status": "running"}))

    def test_public_projections_are_path_free_and_total(self) -> None:
        view = terminal_delivery_view(self.receipt)
        health = delivery_health_view(self.receipt)
        self.assertNotIn("stages", health["outstanding_stages"] or [])
        self.assertIn("state", health)
        self.assertIn("stages_total", health)
        self.assertIn("healthy", health)
        self.assertFalse("/" in json.dumps(view))
        self.assertEqual(len(view["stages"]), 5)

    def test_malformed_receipt_projects_not_applicable_without_raising(self) -> None:
        view = terminal_delivery_view({"version": 99})
        self.assertEqual(view["delivery_id"], "")
        for stage in TERMINAL_DELIVERY_STAGES:
            self.assertEqual(view["stages"][stage]["state"], "not_applicable")


def reserved_backoff(receipt: dict, stage: str = "record") -> int:
    entry = receipt["stages"][stage]
    if not entry["next_attempt_at"]:
        return 0
    from agent_bridge_connect.terminal_delivery import _parse_utc

    committed = receipt["updated_at"]
    delta = _parse_utc(entry["next_attempt_at"]) - _parse_utc(committed)
    return int(round(delta.total_seconds()))


class TerminalDeliveryBoardSetup(unittest.TestCase):
    """Shared temporary board and receipt builders.

    Splitting the fixture out keeps YBNW-002's production-routing tests from
    re-running every ``TerminalDeliveryBoardTests`` case as a subclass.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        from agent_bridge_connect.service import TaskService

        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    # ------------------------------------------------------------- builders
    def _terminal_task(self, *, status: str = "completed", **kwargs) -> str:
        task = self.service.create_task(
            "flow104 terminal delivery",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = status
        raw["updated_at"] = T0
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "terminal"
        raw["extensions"][SESSION_EXTENSION_KEY]["retain"] = False
        raw["extensions"]["agentbc.final_callback"] = {
            "version": 1,
            "task_id": task.id,
            "final_state": status,
            "summary": "done",
            "report_file": str(raw["workspace"].get("report_file") or ""),
        }
        if terminal_delivery_eligible({"status": status}):
            raw["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY] = (
                build_terminal_delivery_receipt(
                    task.id,
                    terminal_state=status,
                    terminal_event="task.finalized",
                    committed_at=T0,
                )
            )
        self.service.store.write_task(task.id, raw)
        return task.id

    def _receipt(self, task_id: str) -> dict:
        raw = self.service.store.read_task(task_id)
        return raw["extensions"].get(TERMINAL_DELIVERY_EXTENSION_KEY) or {}

    def _write_receipt(self, task_id: str, receipt: dict) -> None:
        raw = self.service.store.read_task(task_id)
        raw["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY] = receipt
        self.service.store.write_task(task_id, raw)

    def _delivery_events(self, task_id: str) -> list[dict]:
        path = self.service.store.task_dir(task_id) / DELIVERY_EVENTS_FILE
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _coordinator(self, **kwargs) -> TerminalDeliveryCoordinator:
        return TerminalDeliveryCoordinator(self.board, **kwargs)



class TerminalDeliveryBoardTests(TerminalDeliveryBoardSetup):
    """End-to-end delivery/cleanup behaviour on a real temporary board."""

    # ---------------------------------------------------------------- tests
    def test_receipt_is_created_in_the_same_authoritative_task_write(self) -> None:
        from agent_bridge_connect.service import TaskService

        task = self.service.create_task(
            "same write receipt",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        service = TaskService(self.board)
        service.store.append_event(
            task.id,
            {"event_type": "task.started", "task_id": task.id, "created_at": T0},
        )
        raw_before = self.service.store.read_task(task.id)
        self.assertNotIn(TERMINAL_DELIVERY_EXTENSION_KEY, raw_before["extensions"])
        service.finalize_task_from_agent(
            task.id,
            {
                "version": 1,
                "task_id": task.id,
                "final_state": "completed",
                "summary": "flow104 same-write receipt",
                "step_results": [{"id": 1, "status": "done"}],
            },
        )
        stored = self._receipt(task.id)
        self.assertTrue(stored, "receipt must exist in the authoritative task write")
        self.assertIn(TERMINAL_DELIVERY_EXTENSION_KEY, self.service.store.read_task(task.id)["extensions"])
        self.assertEqual(stored["terminal_state"], "completed")

    def test_report_permission_failure_keeps_completed_terminal_state(self) -> None:
        task_id = self._terminal_task()
        executors = {
            "report": lambda: (_ for _ in ()).throw(PermissionError("denied")),
        }
        result = self._coordinator(stage_executors=executors).deliver_now(task_id, now=T0)
        status = self.service.store.read_task(task_id)["status"]
        self.assertEqual(status, "completed")
        self.assertNotEqual(result["status"], "skipped")
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["terminal_state"], "completed")
        self.assertNotEqual(receipt["stages"]["report"]["state"], "succeeded")

    def test_record_beyond_50kib_preserves_receipt_and_terminal_state(self) -> None:
        task_id = self._terminal_task()
        # Bloat the task record far beyond the 50 KiB budget.
        raw = self.service.store.read_task(task_id)
        raw["steps"] = [
            {"id": 1, "description": "x" * 4096, "result": {"summary": "y" * 8192}}
            for _ in range(24)
        ]
        self.service.store.write_task(task_id, raw)
        result = self._coordinator().deliver_now(task_id, now=T0)
        status = self.service.store.read_task(task_id)["status"]
        self.assertEqual(status, "completed")
        self.assertEqual(result["status"], "delivered")
        receipt = self._receipt(task_id)
        self.assertTrue(receipt["delivery_id"])
        self.assertEqual(receipt["stages"]["record"]["state"], "succeeded")
        try:
            stored = read_terminal_delivery_receipt(receipt)
        except Exception as exc:  # pragma: no cover - assertion below handles it
            self.fail(f"receipt did not survive compaction: {exc}")
        self.assertEqual(stored["stages"], receipt["stages"])

    def test_index_failure_does_not_block_notifications(self) -> None:
        task_id = self._terminal_task()
        executors = {
            "index": lambda: StageOutcome(False, error_code="index_refresh_failed"),
            "file_notification": lambda: StageOutcome(True),
            "ui_notification": lambda: StageOutcome(True),
        }
        self._coordinator(stage_executors=executors).deliver_now(task_id, now=T0)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["index"]["last_error_code"], "index_refresh_failed")
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")

    def test_file_and_ui_notifier_failures_are_independent(self) -> None:
        task_id = self._terminal_task()
        executors = {
            "file_notification": lambda: StageOutcome(
                False, error_code="file_notification_failed"
            ),
            "ui_notification": lambda: StageOutcome(
                False, error_code="ui_notification_failed"
            ),
        }
        self._coordinator(stage_executors=executors).deliver_now(task_id, now=T0)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["report"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["record"]["state"], "succeeded")
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")
        health = delivery_health_view(receipt)
        self.assertEqual(health["state"], "retry_wait")
        self.assertFalse(health["healthy"])
        self.assertEqual(
            health["error_codes"],
            ["file_notification_failed", "ui_notification_failed"],
        )

    def test_ui_notifier_failure_reports_delivery_uncertainty(self) -> None:
        task_id = self._terminal_task()
        executors = {
            "ui_notification": lambda: StageOutcome(
                False, error_code="ui_notification_failed", delivery_uncertain=True
            ),
        }
        self._coordinator(stage_executors=executors).deliver_now(task_id, now=T0)
        entry = self._receipt(task_id)["stages"]["ui_notification"]
        self.assertEqual(entry["state"], "retry_wait")
        self.assertEqual(entry["last_error_code"], "ui_notification_failed")
        self.assertEqual(reserved_backoff(self._receipt(task_id), "ui_notification"), 300)

    def test_concurrent_terminal_callbacks_keep_one_delivery_identity(self) -> None:
        task_id = self._terminal_task()
        first = self._coordinator().deliver_now(task_id, now=T0)
        second = self._coordinator().deliver_now(task_id, now=_at(1))
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        status = self.service.store.read_task(task_id)["status"]
        self.assertEqual(status, "completed")
        # A confirmed stage must not gain attempts from the second pass.
        if first["receipt"]["stages"]["report"]["state"] == "succeeded":
            self.assertEqual(
                second["receipt"]["stages"]["report"]["state"], "succeeded"
            )

    def test_runner_death_between_reservation_and_result_is_reconciled(self) -> None:
        task_id = self._terminal_task()
        # Simulate a crash after the stage reservation was persisted.
        reserved = read_terminal_delivery_receipt(self._receipt(task_id))
        reserved = transition_terminal_delivery_stage(
            reserved, "ui_notification", "in_progress"
        )
        reserved = transition_terminal_delivery_stage(
            reserved, "file_notification", "in_progress"
        )
        self._write_receipt(task_id, reserved)
        # The restarted Runner reconciles with delivery_uncertain evidence and
        # schedules the capped 300s backoff; a later pass then delivers.
        executors = {
            "ui_notification": lambda: StageOutcome(True),
            "file_notification": lambda: StageOutcome(True),
        }
        coordinator = self._coordinator(stage_executors=executors)
        coordinator.deliver_now(task_id, now=_at(1))
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "retry_wait")
        self.assertEqual(
            receipt["stages"]["ui_notification"]["last_error_code"],
            "delivery_stage_uncertain",
        )
        self.assertEqual(reserved_backoff(receipt, "ui_notification"), 300)
        coordinator.deliver_now(task_id, now=_at(301))
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "succeeded")
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")

    def test_runner_restart_replays_only_outstanding_stages(self) -> None:
        task_id = self._terminal_task()
        receipt = read_terminal_delivery_receipt(self._receipt(task_id))
        for stage in ("report", "record", "index"):
            receipt = transition_terminal_delivery_stage(receipt, stage, "succeeded")
        receipt = transition_terminal_delivery_stage(
            receipt,
            "file_notification",
            "retry_wait",
            error_code="file_notification_failed",
        )
        self._write_receipt(task_id, receipt)
        calls: list[str] = []

        def _spy(stage: str):
            def _run() -> StageOutcome:
                calls.append(stage)
                return StageOutcome(True)

            return _run

        self._coordinator(
            stage_executors={
                "report": _spy("report"),
                "record": _spy("record"),
                "index": _spy("index"),
                "file_notification": _spy("file_notification"),
                "ui_notification": _spy("ui_notification"),
            }
        ).deliver_now(task_id, now=_at(400))
        self.assertEqual(calls, ["file_notification", "ui_notification"])
        health = delivery_health_view(self._receipt(task_id))
        self.assertTrue(health["healthy"])
        self.assertEqual(health["state"], "succeeded")

    def test_maintenance_never_touches_input_required_or_recovery(self) -> None:
        input_task = self._terminal_task(status="input_required")
        recovery_task = self._terminal_task()
        raw = self.service.store.read_task(recovery_task)
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "needs_recovery"
        raw["status"] = "needs_recovery"
        self.service.store.write_task(recovery_task, raw)

        results = self._coordinator().maintain_board(now=T0)
        processed = {item["task_id"] for item in results}
        self.assertNotIn(input_task, processed)
        self.assertNotIn(recovery_task, processed)
        self.assertEqual(
            self.service.store.read_task(input_task)["status"], "input_required"
        )
        self.assertEqual(
            self.service.store.read_task(recovery_task)["status"], "needs_recovery"
        )

    def test_input_required_maintenance_is_a_no_op(self) -> None:
        task_id = self._terminal_task(status="input_required")
        coordinator = self._coordinator()
        with mock.patch.object(
            coordinator, "_executors", side_effect=AssertionError("respond_to_input")
        ) as executors:
            coordinator.maintain_board(now=T0)
            executors.assert_not_called()
        self.assertEqual(
            self.service.store.read_task(task_id)["status"], "input_required"
        )

    def test_delivery_events_are_bounded_and_path_free(self) -> None:
        task_id = self._terminal_task()
        self._coordinator(
            stage_executors={
                "ui_notification": lambda: StageOutcome(
                    False, error_code="ui_notification_failed"
                ),
            }
        ).deliver_now(task_id, now=T0)
        events = self._delivery_events(task_id)
        self.assertTrue(events)
        blob = json.dumps(events)
        self.assertNotIn(str(self.board), blob)
        self.assertNotIn("message", blob.replace('"message"', ""))
        for event in events:
            self.assertEqual(event["event_type"], "terminal.delivery")

    def test_public_task_view_exposes_terminal_delivery_and_delivery_health(self) -> None:
        task_id = self._terminal_task()
        raw = self.service.store.read_task(task_id)
        public = public_task_view(raw)
        policy = public["execution_policy"]
        self.assertIn("terminal_delivery", policy)
        self.assertIn("delivery_health", policy)
        self.assertEqual(policy["delivery_health"]["stages_total"], 5)
        self.assertFalse(str(self.root) in json.dumps(policy["delivery_health"]))

    def test_report_projection_exposes_delivery_health(self) -> None:
        task_id = self._terminal_task()
        report = generate_report(task_id, self.board)
        self.assertIn("terminal_delivery", report)
        self.assertIn("delivery_health", report)
        self.assertEqual(report["delivery_health"]["stages_total"], 5)
        # flow_contract_satisfied stays false until callback, steps and report
        # contract are all satisfied.
        self.assertIsInstance(report["flow_contract_satisfied"], bool)


class LegacyTerminalRecordImportTests(unittest.TestCase):
    def test_existing_report_satisfies_report_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "YBNW-001-report.md"
            report.write_text("# legacy\n", encoding="utf-8")
            imported = import_legacy_terminal_delivery(
                None,
                task={
                    "id": "YBNW-001",
                    "status": "completed",
                    "updated_at": T0,
                    "workspace": {"report_file": str(report)},
                },
            )
        self.assertEqual(imported["stages"]["report"]["state"], "succeeded")
        self.assertEqual(
            imported["stages"]["ui_notification"]["state"], "not_applicable"
        )

    def test_terminal_notification_event_satisfies_notification_stages(self) -> None:
        imported = import_legacy_terminal_delivery(
            None,
            task={
                "id": "YBNW-001",
                "status": "completed",
                "updated_at": T0,
                "workspace": {"report_file": ""},
            },
            events=[
                {
                    "event_type": "notification_delivery",
                    "notification_event": "task.finalized",
                    "file_ok": True,
                    "dialog_ok": True,
                }
            ],
        )
        self.assertEqual(imported["stages"]["file_notification"]["state"], "succeeded")
        self.assertEqual(imported["stages"]["ui_notification"]["state"], "succeeded")

    def test_historical_ui_delivery_is_never_replayed(self) -> None:
        imported = import_legacy_terminal_delivery(
            None,
            task={"id": "YBNW-001", "status": "failed", "updated_at": T0},
        )
        for stage in ("file_notification", "ui_notification"):
            self.assertEqual(imported["stages"][stage]["state"], "not_applicable")
        # Record compaction / index refresh remain replayable.
        self.assertEqual(imported["stages"]["record"]["state"], "pending")
        self.assertEqual(imported["stages"]["index"]["state"], "pending")

    def test_real_receipt_is_returned_unchanged(self) -> None:
        receipt = build_terminal_delivery_receipt(
            "YBNW-001",
            terminal_state="completed",
            terminal_event="task.finalized",
            committed_at=T0,
        )
        receipt = transition_terminal_delivery_stage(receipt, "report", "succeeded")
        self.assertEqual(import_legacy_terminal_delivery(receipt), receipt)


class CleanupGateRemovalTests(unittest.TestCase):
    """FLOW-104-002: cleanup eligibility no longer requires report/notification."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        from agent_bridge_connect.service import TaskService

        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    def _task(
        self,
        *,
        status: str = "completed",
        report: bool = False,
        session_id: str = HERMES_SESSION_ID,
    ) -> str:
        task = self.service.create_task(
            "cleanup gate removal",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = status
        raw["updated_at"] = T0
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "terminal"
        raw["extensions"][SESSION_EXTENSION_KEY]["retain"] = False
        raw["extensions"][SESSION_EXTENSION_KEY]["session_id"] = session_id
        raw["extensions"][SESSION_EXTENSION_KEY]["created_at"] = T0
        raw["extensions"]["agentbc.final_callback"] = {
            "version": 1,
            "task_id": task.id,
            "final_state": status,
            "summary": "done",
            "marker_valid": True,
            "report_file": str(raw["workspace"].get("report_file") or ""),
        }
        self.service.store.write_task(task.id, raw)
        if report:
            report_file = Path(str(raw["workspace"]["report_file"]))
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text("# report\n", encoding="utf-8")
        from agent_bridge_connect.run_lease import (
            RunLeaseState,
            create_lease,
            save_lease,
        )

        lease = create_lease(task.id, "hermes", os.getpid(), str(self.root))
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)
        return task.id

    def test_cleanup_is_eligible_without_report_or_notification_evidence(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        # No report file, no notification_delivery event at all.
        task_id = self._task(report=False)
        coordinator = SessionCleanupCoordinator(self.board)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertNotIn("report_not_written", result.get("blockers") or [])
        self.assertNotIn("notification_not_recorded", result.get("blockers") or [])
        self.assertNotEqual(result["status"], "skipped")
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")

    def test_cleanup_failure_never_changes_task_status(self) -> None:
        from agent_bridge_connect.adapters import SessionCleanupResult
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._task(report=False)

        class _Failing:
            def cleanup_session(self, request):
                return SessionCleanupResult(
                    state="failed",
                    capability="supported",
                    strategy="official_session_delete",
                    error_code="session_cleanup_failed",
                    retryable=True,
                )

        SessionCleanupCoordinator(self.board, executor_port=_Failing()).request_cleanup(
            task_id, now=T0
        )
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")
        self.assertTrue(
            self._task_has_final_callback(task_id),
            "cleanup failure must not remove the final callback",
        )

    def _task_has_final_callback(self, task_id: str) -> bool:
        raw = self.service.store.read_task(task_id)
        return bool((raw.get("extensions") or {}).get("agentbc.final_callback"))


class FlowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        from agent_bridge_connect.service import TaskService

        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    def test_missing_report_keeps_flow_contract_unsatisfied(self) -> None:
        task = self.service.create_task(
            "flow contract",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = "completed"
        raw["extensions"]["agentbc.final_callback"] = {
            "version": 1,
            "task_id": task.id,
            "final_state": "completed",
            "summary": "done",
            "marker_valid": True,
            "report_file": str(raw["workspace"].get("report_file") or ""),
        }
        self.service.store.write_task(task.id, raw)
        report = generate_report(task.id, self.board)
        self.assertFalse(report["flow_contract_satisfied"])
        self.assertFalse(report["report_ready"])
        self.assertEqual(report["status"], "completed")


class ProductionRoutingTests(TerminalDeliveryBoardSetup):
    """YBNW-002: production terminal side effects are receipt-owned.

    Before this correction the Runner, Core completion and the worker CLI all
    called ``write_report_files`` / ``notify_terminal`` directly, so the receipt's
    notification stages stayed unconfirmed and Runner maintenance replayed a
    terminal notification the user had already seen.
    """

    def _finalize(self, task_id: str) -> None:
        from agent_bridge_connect.service import TaskService

        TaskService(self.board).finalize_task_from_agent(
            task_id,
            {
                "version": 1,
                "task_id": task_id,
                "final_state": "completed",
                "summary": "production routing",
                "step_results": [{"id": 1, "status": "done"}],
            },
        )

    def test_core_leaves_notification_stages_pending_for_the_runner(self) -> None:
        from agent_bridge_connect.terminal_delivery import (
            pending_terminal_delivery_stages,
        )

        task_id = self.service.create_task(
            "core reservation",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        ).id
        self._finalize(task_id)
        stages = pending_terminal_delivery_stages(self._receipt(task_id))
        self.assertEqual(
            stages,
            ["file_notification", "ui_notification"],
            "Core must not reserve a notification stage it does not own",
        )
        for stage in ("report", "record", "index"):
            self.assertEqual(self._receipt(task_id)["stages"][stage]["state"], "succeeded")

    def test_immediate_runner_delivery_shows_the_terminal_dialog(self) -> None:
        task_id = self.service.create_task(
            "immediate dialog",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        ).id
        self._finalize(task_id)
        dialogs: list[dict] = []

        def _dialog(payload: dict) -> StageOutcome:
            dialogs.append(payload)
            return StageOutcome(True)

        with mock.patch("agent_bridge_connect.notifications.time.sleep"):
            result = self._coordinator(ui_notifier=_dialog).deliver_now(task_id, now=T0)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(dialogs), 1, "the terminal dialog must not be deferred")
        self.assertEqual(self._receipt(task_id)["stages"]["ui_notification"]["state"], "succeeded")

    def test_maintenance_never_repeats_a_confirmed_terminal_notification(self) -> None:
        task_id = self.service.create_task(
            "no duplicate dialog",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        ).id
        self._finalize(task_id)
        dialogs: list[dict] = []

        def _dialog(payload: dict) -> StageOutcome:
            dialogs.append(payload)
            return StageOutcome(True)

        coordinator = self._coordinator(ui_notifier=_dialog)
        with mock.patch("agent_bridge_connect.notifications.time.sleep"):
            first = coordinator.deliver_now(task_id, now=T0)
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(len(dialogs), 1)
        # Runner restart: a brand new coordinator with no memory.
        for now in (T0, _at(1), _at(400), _at(1000)):
            TerminalDeliveryCoordinator(self.board).maintain_board(now=now)
        self.assertEqual(
            len(dialogs),
            1,
            "maintenance replayed a terminal notification the user already saw",
        )

    def test_apply_agent_completion_records_the_notification_on_the_receipt(self) -> None:
        from agent_bridge_connect.task_completion import apply_agent_completion

        task_id = self.service.create_task(
            "completion routing",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        ).id
        dialogs: list[dict] = []

        def _dialog(payload: dict) -> StageOutcome:
            dialogs.append(payload)
            return StageOutcome(True)

        with mock.patch("agent_bridge_connect.notifications.time.sleep"), mock.patch(
            "agent_bridge_connect.notifiers.dialog.DialogNotifier.send"
        ) as dialog, mock.patch(
            "agent_bridge_connect.notifiers.file.FileNotifier.send"
        ) as file_send:
            dialog.return_value = mock.Mock(ok=True, message="dialog shown")
            file_send.return_value = mock.Mock(ok=True, message="file written")
            result = apply_agent_completion(
                self.service,
                task_id,
                state="completed",
                summary="routing through the receipt",
                step_results=[{"id": 1, "status": "done"}],
            )
        self.assertTrue(result["notified"])
        self.assertEqual(dialog.call_count, 1)
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")
        coordinator = TerminalDeliveryCoordinator(self.board)
        receipt = coordinator.receipt(task_id)
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        # A second, identical completion pass must not notify again.
        TerminalDeliveryCoordinator(
            self.board, ui_notifier=_dialog
        ).maintain_board(now=_at(400))
        self.assertEqual(len(dialogs), 0)
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")

    def test_apply_agent_completion_keeps_the_input_notice_and_approval_state(self) -> None:
        from agent_bridge_connect.task_completion import apply_agent_completion

        task_id = self.service.create_task(
            "input routing",
            "hermes",
            [{"id": 1, "description": "blocked"}],
            customer_dir=False,
        ).id
        raw = self.service.store.read_task(task_id)
        raw["extensions"]["agentbc.input"] = {
            "version": 1,
            "input_id": "I-001",
            "type": "permission",
            "scope": "task_elevation",
            "approval_version": 3,
            "elevation_mode": "full",
            "status": "waiting",
            "reason_summary": "needs full access",
            "blocked_step_id": 1,
            "deadline_at": "2026-09-30T00:00:00Z",
            "dialog_count": 1,
            "request_id": "req-ybnw-002",
            "created_at": T0,
        }
        raw["extensions"]["agentbc.permission_elevation"] = {
            "version": 1,
            "mode": "full",
            "scope": "task_elevation",
            "cardinality": {"notifications": 1, "dialogs": 1},
            "reserved_at": T0,
        }
        self.service.store.write_task(task_id, raw)
        before = self.service.store.read_task(task_id)
        with mock.patch(
            "agent_bridge_connect.task_completion.notify_input_required",
            wraps=notify_input_required,
        ) as notice:
            result = apply_agent_completion(
                self.service,
                task_id,
                state="input_required",
                summary="waiting on the user",
                step_results=[{"id": 1, "status": "blocked"}],
            )
        self.assertTrue(result["notified"])
        self.assertEqual(result["event_type"], "task.input_required")
        notice.assert_called_once()
        after = self.service.store.read_task(task_id)
        self.assertEqual(after["status"], "input_required")
        request = after["extensions"]["agentbc.input"]
        self.assertEqual(request["status"], "waiting")
        # The elevation receipt and its notification cardinality are untouched:
        # FLOW-104-002 never enters the approval flow.
        self.assertEqual(
            after["extensions"]["agentbc.permission_elevation"],
            before["extensions"]["agentbc.permission_elevation"],
        )
        self.assertNotIn(
            TERMINAL_DELIVERY_EXTENSION_KEY,
            after["extensions"],
            "an input_required task must never gain a delivery receipt",
        )
        deliveries = [
            event
            for event in self.service.store.read_events(task_id)
            if event.get("event_type") == "notification_delivery"
        ]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["notification_event"], "task.input_required")
        self.assertIs(deliveries[0].get("terminal"), False)

    def test_delivery_pass_never_reverts_a_concurrent_run_interval(self) -> None:
        """FLOW-104-002 must not project a stale active execution interval.

        The delivery pass holds a task snapshot while the report/record/index
        stages run.  A lifecycle write that lands in between must survive the
        receipt persistence instead of being reverted by it.
        """
        task_id = self.service.create_task(
            "stale interval",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        ).id
        self._finalize(task_id)
        raw = self.service.store.read_task(task_id)
        execution = dict(raw["extensions"].get("agentbc.execution") or {})
        execution["run_intervals"] = [
            {
                "run_id": "run-concurrent",
                "executor_id": "hermes",
                "started_at": T0,
                "ended_at": _at(30),
                "duration_s": 30.0,
                "state": "closed",
            }
        ]
        raw["extensions"]["agentbc.execution"] = execution
        self.service.store.write_task(task_id, raw)

        def _record_interval_then_fail() -> StageOutcome:
            concurrent = self.service.store.read_task(task_id)
            extensions = dict(concurrent["extensions"] or {})
            current = dict(extensions.get("agentbc.execution") or {})
            current["run_intervals"] = [
                {
                    "run_id": "run-concurrent",
                    "executor_id": "hermes",
                    "started_at": T0,
                    "ended_at": _at(30),
                    "duration_s": 30.0,
                    "state": "closed",
                },
                {
                    "run_id": "run-active",
                    "executor_id": "hermes",
                    "started_at": _at(30),
                    "ended_at": _at(45),
                    "duration_s": 15.0,
                    "state": "closed",
                },
            ]
            extensions["agentbc.execution"] = current
            concurrent["extensions"] = extensions
            self.service.store.write_task(task_id, concurrent)
            return StageOutcome(False, error_code="index_refresh_failed")

        result = self._coordinator(
            stage_executors={
                "file_notification": _record_interval_then_fail,
                "ui_notification": lambda: StageOutcome(True),
            }
        ).deliver_now(task_id, now=_at(60))
        self.assertEqual(result["status"], "partial")
        intervals = (self.service.store.read_task(task_id)["extensions"] or {}).get(
            "agentbc.execution", {}
        ).get("run_intervals") or []
        self.assertEqual(
            [item["run_id"] for item in intervals],
            ["run-concurrent", "run-active"],
            "the delivery receipt write reverted a concurrent execution interval",
        )

    def test_delivery_pass_keeps_a_closed_run_lease_authoritative(self) -> None:
        """The delivery pass must not resurrect a closed RunLease projection."""
        from agent_bridge_connect.run_lease import RunLeaseState, create_lease, save_lease
        from agent_bridge_connect.timing_view import build_timing_view

        task_id = self.service.create_task(
            "closed lease authority",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        ).id
        self._finalize(task_id)
        raw = self.service.store.read_task(task_id)
        execution = dict(raw["extensions"].get("agentbc.execution") or {})
        execution["lease_state"] = "active"
        raw["extensions"]["agentbc.execution"] = execution
        self.service.store.write_task(task_id, raw)
        lease = create_lease(task_id, "hermes", 1, str(self.root))
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)

        self._coordinator(
            stage_executors={
                "file_notification": lambda: StageOutcome(True),
                "ui_notification": lambda: StageOutcome(True),
            }
        ).deliver_now(task_id, now=T0)

        view = build_timing_view(self.service.store.read_task(task_id), self.board, now=T0)
        self.assertEqual(
            view["lease_state"],
            RunLeaseState.CLOSED,
            "a stale active lease snapshot must not override the closed RunLease",
        )


if __name__ == "__main__":
    unittest.main()
