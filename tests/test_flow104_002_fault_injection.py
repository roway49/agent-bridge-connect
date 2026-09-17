"""FLOW-104-002 fault-injection evidence scenarios.

Each test injects one exact fault and asserts three invariants:

1. the business terminal state, final callback and step results are unchanged;
2. the independent delivery stages continue and are recorded on the receipt;
3. eligible executor-session cleanup still runs with no report or notification
   evidence.

It also proves the terminal coordinator cannot interfere with the approval
system: while a task is ``input_required``, Runner maintenance must not mutate
``agentbc.input``, approval/elevation receipts, the deadline, the dialog count,
the permission mode, the Executor session, the worker count or the continuation
count, and must never call ``respond_to_input``.
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
)
from agent_bridge_connect.permission_elevation import (
    PERMISSION_ELEVATION_EXTENSION_KEY,
)
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    create_lease,
    save_lease,
)
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.terminal_delivery import (
    DELIVERY_EVENTS_FILE,
    TERMINAL_DELIVERY_EXTENSION_KEY,
    StageOutcome,
    build_terminal_delivery_receipt,
    delivery_health_view,
    read_terminal_delivery_receipt,
    transition_terminal_delivery_stage,
)
from agent_bridge_connect.terminal_delivery_coordinator import (
    TerminalDeliveryCoordinator,
)

T0 = "2026-09-01T00:00:00Z"
HERMES_SESSION_ID = "20260901_000000_a1b2c3"
INPUT_DEADLINE = "2026-09-30T00:00:00Z"


def _at(seconds: int) -> str:
    parsed = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class Flow104BoardTestCase(unittest.TestCase):
    """Shared board fixtures.

    ``_coordinator`` stubs the two notification stages by default so a unit test
    never opens a real dialog; pass ``stub_notifications=False`` to opt in.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    def _coordinator(self, **kwargs) -> TerminalDeliveryCoordinator:
        executors = dict(kwargs.pop("stage_executors", {}) or {})
        if kwargs.pop("stub_notifications", True):
            executors.setdefault("file_notification", lambda: StageOutcome(True))
            executors.setdefault("ui_notification", lambda: StageOutcome(True))
        return TerminalDeliveryCoordinator(self.board, stage_executors=executors)

    def _closed_lease(self, task_id: str) -> None:
        lease = create_lease(task_id, "hermes", os.getpid(), str(self.root))
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)


class FaultInjectionTestCase(Flow104BoardTestCase):

    # ------------------------------------------------------------- builders
    def _terminal_task(
        self,
        *,
        status: str = "completed",
        steps: int = 2,
    ) -> str:
        step_list = [{"id": i, "description": f"step {i}"} for i in range(1, steps + 1)]
        task = self.service.create_task(
            "flow104 fault injection",
            "hermes",
            step_list,
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = status
        raw["updated_at"] = T0
        raw["steps"] = [
            {**step, "status": "done"} for step in raw.get("steps") or step_list
        ]
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "terminal"
        raw["extensions"][SESSION_EXTENSION_KEY]["retain"] = False
        raw["extensions"][SESSION_EXTENSION_KEY]["session_id"] = HERMES_SESSION_ID
        raw["extensions"][SESSION_EXTENSION_KEY]["created_at"] = T0
        raw["extensions"]["agentbc.final_callback"] = {
            "version": 1,
            "task_id": task.id,
            "final_state": status,
            "summary": "terminal",
            "marker_valid": True,
            "report_file": str(raw["workspace"].get("report_file") or ""),
            "step_results": [
                {"id": step["id"], "status": "done"} for step in raw["steps"]
            ],
        }
        raw["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY] = (
            build_terminal_delivery_receipt(
                task.id,
                terminal_state=status,
                terminal_event="task.finalized",
                committed_at=T0,
            )
        )
        self.service.store.write_task(task.id, raw)
        self._closed_lease(task.id)
        return task.id

    def _snapshot(self, task_id: str) -> dict:
        raw = self.service.store.read_task(task_id)
        return {
            "status": raw["status"],
            "steps": copy.deepcopy(raw["steps"]),
            "final_callback": copy.deepcopy(
                (raw.get("extensions") or {}).get("agentbc.final_callback")
            ),
        }

    def _receipt(self, task_id: str) -> dict:
        return self.service.store.read_task(task_id)["extensions"][
            TERMINAL_DELIVERY_EXTENSION_KEY
        ]

    def _cleanup_pass(self, task_id: str, *, now: str = T0) -> dict:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        return SessionCleanupCoordinator(self.board).request_cleanup(task_id, now=now)

    # --------------------------------------------------- fault: report perms
    def test_report_permission_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _denied() -> StageOutcome:
            raise PermissionError("report directory is read-only")

        handlers = {"report": _denied}
        coordinator = self._coordinator(stage_executors=handlers)
        with mock.patch.object(
            coordinator, "_report_executor", return_value=_denied
        ):
            coordinator.deliver_now(task_id, now=T0)

        self.assertEqual(self._snapshot(task_id), before)
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["record"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["index"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["report"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["report"]["last_error_code"], "report_write_failed")
        # Cleanup is still eligible with no report and no notification evidence.
        result = self._cleanup_pass(task_id)
        self.assertNotIn("report_not_written", result.get("blockers") or [])
        self.assertNotEqual(result["status"], "skipped")
        self.assertEqual(self._snapshot(task_id), before)

    # ------------------------------------------------ fault: record > 50 KiB
    def test_record_beyond_50kib(self) -> None:
        task_id = self._terminal_task(steps=6)
        raw = self.service.store.read_task(task_id)
        raw["steps"] = [
            {
                "id": 1,
                "status": "done",
                "description": "x" * 4096,
                "result": {"summary": "y" * 8192, "artifacts": ["z" * 4096]},
            }
            for _ in range(20)
        ]
        self.service.store.write_task(task_id, raw)
        before_status = self.service.store.read_task(task_id)["status"]
        receipt_before = self._receipt(task_id)["delivery_id"]

        result = self._coordinator().deliver_now(task_id, now=T0)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(self.service.store.read_task(task_id)["status"], before_status)
        receipt = self._receipt(task_id)
        # The receipt survived compaction with the same identity and stages.
        self.assertEqual(receipt["delivery_id"], receipt_before)
        self.assertEqual(
            read_terminal_delivery_receipt(receipt)["stages"], receipt["stages"]
        )
        for stage in ("report", "record", "index"):
            self.assertEqual(receipt["stages"][stage]["state"], "succeeded")
        # Notifications are Runner-owned and confirmed independently of the
        # record compaction outcome.
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        self._cleanup_pass(task_id)
        self.assertEqual(self.service.store.read_task(task_id)["status"], before_status)

    # ------------------------------------------------------ fault: index fail
    def test_index_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _index_boom() -> StageOutcome:
            raise OSError("index directory unavailable")

        coordinator = self._coordinator(stage_executors={"index": _index_boom})
        with mock.patch.object(coordinator, "_index_executor", return_value=_index_boom):
            coordinator.deliver_now(task_id, now=T0)

        self.assertEqual(self._snapshot(task_id), before)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["index"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["index"]["last_error_code"], "index_refresh_failed")
        # The report and record stages still completed independently.
        self.assertEqual(receipt["stages"]["report"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["record"]["state"], "succeeded")
        health = delivery_health_view(receipt)
        self.assertEqual(health["state"], "retry_wait")
        self.assertFalse(health["healthy"])
        # A later pass retries the failed stage and the terminal state is stable.
        self._coordinator().deliver_now(task_id, now=_at(400))
        self.assertEqual(self._snapshot(task_id), before)
        self.assertEqual(self._receipt(task_id)["stages"]["index"]["state"], "succeeded")

    # ------------------------------------------- fault: file / ui notifier fail
    def test_file_notifier_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _file() -> StageOutcome:
            return StageOutcome(False, error_code="file_notification_failed")

        coordinator = self._coordinator(stage_executors={"file_notification": _file})
        with mock.patch.object(coordinator, "_file_executor", return_value=_file):
            coordinator.deliver_now(task_id, now=T0)

        self.assertEqual(self._snapshot(task_id), before)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["index"]["state"], "succeeded")
        result = self._cleanup_pass(task_id)
        self.assertNotIn("notification_not_recorded", result.get("blockers") or [])
        self.assertEqual(self._snapshot(task_id), before)

    def test_ui_notifier_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _ui() -> StageOutcome:
            return StageOutcome(False, error_code="ui_notification_failed")

        coordinator = self._coordinator(stage_executors={"ui_notification": _ui})
        with mock.patch.object(coordinator, "_ui_executor", return_value=_ui):
            coordinator.deliver_now(task_id, now=T0)

        self.assertEqual(self._snapshot(task_id), before)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["ui_notification"]["last_error_code"], "ui_notification_failed")
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "succeeded")
        result = self._cleanup_pass(task_id)
        self.assertIn("task_end_dialog_not_delivered", result.get("blockers") or [])
        self.assertEqual(result["status"], "skipped")

    # ------------------------------------------ fault: concurrent terminal callbacks
    def test_concurrent_terminal_callbacks(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)
        coordinator_a = self._coordinator()
        coordinator_b = self._coordinator()
        # Two independent coordinators (as if two Runner processes) race.
        first = coordinator_a.deliver_now(task_id, now=T0)
        second = coordinator_b.deliver_now(task_id, now=_at(1))
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(self._snapshot(task_id), before)
        # Attempts never inflate: confirmed stages are immutable.
        for stage in ("report", "record", "index"):
            self.assertEqual(
                second["receipt"]["stages"][stage]["attempts"],
                first["receipt"]["stages"][stage]["attempts"],
            )

    # --------------------------------- fault: Runner death between reservation and result
    def test_runner_death_between_reservation_and_result(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)
        # Reserve the two notification stages, then "die" before recording results.
        reserved = read_terminal_delivery_receipt(self._receipt(task_id))
        for stage in ("file_notification", "ui_notification"):
            reserved = transition_terminal_delivery_stage(
                reserved, stage, "in_progress", occurred_at=T0
            )
        raw = self.service.store.read_task(task_id)
        raw["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY] = reserved
        self.service.store.write_task(task_id, raw)

        # The restarted Runner must never repeat them blindly: it reconciles with
        # delivery_uncertain evidence and schedules the capped backoff.
        from agent_bridge_connect.terminal_delivery import reconcile_interrupted_stages

        reconciled, stages = reconcile_interrupted_stages(reserved, now=_at(1))
        self.assertEqual(sorted(stages), ["file_notification", "ui_notification"])
        for stage in ("file_notification", "ui_notification"):
            self.assertEqual(reconciled["stages"][stage]["state"], "retry_wait")
            self.assertEqual(
                reconciled["stages"][stage]["last_error_code"],
                "delivery_stage_uncertain",
            )
            self.assertEqual(reconciled["stages"][stage]["next_attempt_at"], _at(301))
        self.assertEqual(self._snapshot(task_id), before)

    # ------------------------------------------------ fault: Runner restart
    def test_runner_restart_replays_only_incomplete_stages(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _failed() -> StageOutcome:
            return StageOutcome(False, error_code="file_notification_failed")

        first = self._coordinator(
            stage_executors={
                "file_notification": _failed,
                "ui_notification": _failed,
            }
        ).deliver_now(task_id, now=T0)
        self.assertEqual(
            first["receipt"]["stages"]["file_notification"]["state"], "retry_wait"
        )
        # Restart: a brand new coordinator with no memory.
        calls: list[str] = []

        def _spy(stage: str):
            def _run() -> StageOutcome:
                calls.append(stage)
                return StageOutcome(True)

            return _run

        restarted = self._coordinator(
            stage_executors={
                "report": _spy("report"),
                "record": _spy("record"),
                "index": _spy("index"),
                "file_notification": _spy("file_notification"),
                "ui_notification": _spy("ui_notification"),
            }
        )
        restarted.deliver_now(task_id, now=_at(1))
        self.assertEqual(self._snapshot(task_id), before)
        self.assertTrue(first["delivery_id"])
        # Confirmed stages from the first process are never repeated; only the
        # two still-outstanding notification stages are replayed.
        self.assertEqual(sorted(calls), ["file_notification", "ui_notification"])
        health = delivery_health_view(self._receipt(task_id))
        self.assertTrue(health["healthy"])
        self.assertEqual(health["state"], "succeeded")


class ApprovalSystemIsolationTests(Flow104BoardTestCase):
    """FLOW-104-002 must not interfere with the current approval system."""

    def setUp(self) -> None:
        super().setUp()
        self.task = self.service.create_task(
            "flow104 approval isolation",
            "hermes",
            [{"id": 1, "description": "blocked step"}],
            customer_dir=False,
        )
        self.task_id = self.task.id
        raw = self.service.store.read_task(self.task_id)
        raw["status"] = "input_required"
        raw["extensions"]["agentbc.input"] = {
            "version": 1,
            "input_id": "I-001",
            "type": "permission",
            "kind": "",
            "scope": "task_elevation",
            "mode": "full",
            "approval_version": 3,
            "elevation_mode": "full",
            "status": "waiting",
            "reason_summary": "needs full access",
            "blocked_step_id": 1,
            "deadline_at": INPUT_DEADLINE,
            "dialog_count": 1,
            "requested_permission": "full",
            "decision": "approve_full",
            "request_id": "req-flow104-001",
            "executor_run_id": "run-flow104-001",
            "created_at": T0,
            "continuation_count": 2,
            "worker_count": 1,
        }
        raw["extensions"][PERMISSION_ELEVATION_EXTENSION_KEY] = {
            "version": 1,
            "mode": "full",
            "scope": "task_elevation",
            "cardinality": {"notifications": 1, "dialogs": 1},
            "reserved_at": T0,
        }
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "input_required"
        raw["extensions"][SESSION_EXTENSION_KEY]["session_id"] = HERMES_SESSION_ID
        raw["extensions"][SESSION_EXTENSION_KEY]["created_at"] = T0
        self.service.store.write_task(self.task_id, raw)
        lease = create_lease(self.task_id, "hermes", os.getpid(), str(self.root))
        lease.state = RunLeaseState.SUSPENDED
        save_lease(lease, self.board)

    def _snapshot(self) -> dict:
        raw = self.service.store.read_task(self.task_id)
        return {
            "status": raw["status"],
            "input": copy.deepcopy(raw["extensions"]["agentbc.input"]),
            "elevation": copy.deepcopy(
                raw["extensions"].get(PERMISSION_ELEVATION_EXTENSION_KEY)
            ),
            "session": copy.deepcopy(raw["extensions"][SESSION_EXTENSION_KEY]),
        }

    def _assert_unchanged(self, before: dict, after: dict) -> None:
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["status"], "input_required")
        self.assertEqual(after["input"], before["input"])
        self.assertEqual(after["elevation"], before["elevation"])
        self.assertEqual(after["session"], before["session"])
        self.assertEqual(before["input"]["deadline_at"], INPUT_DEADLINE)
        self.assertEqual(before["input"]["dialog_count"], 1)
        self.assertEqual(before["input"]["continuation_count"], 2)
        self.assertEqual(before["input"]["worker_count"], 1)

    def test_maintenance_leaves_input_required_task_untouched(self) -> None:
        before = self._snapshot()
        coordinator = TerminalDeliveryCoordinator(self.board)
        with mock.patch.object(
            TaskService,
            "respond_to_input",
            side_effect=AssertionError("respond_to_input must never be called"),
        ) as respond:
            with mock.patch.object(
                TaskService,
                "respond_to_live_claude_elevation",
                side_effect=AssertionError("native respond must never be called"),
            ):
                results = coordinator.maintain_board(now=_at(400))
            respond.assert_not_called()
        self.assertEqual(results, [])
        self._assert_unchanged(before, self._snapshot())
        # No delivery receipt was invented for a nonterminal task.
        self.assertIsNone(
            self.service.store.read_task(self.task_id)["extensions"].get(
                TERMINAL_DELIVERY_EXTENSION_KEY
            )
        )

    def test_deliver_now_refuses_input_required_task(self) -> None:
        before = self._snapshot()
        result = TerminalDeliveryCoordinator(self.board).deliver_now(
            self.task_id, now=_at(400)
        )
        self.assertEqual(result["status"], "skipped")
        self.assertIn("task_not_business_terminal", result["blockers"])
        self._assert_unchanged(before, self._snapshot())

    def test_needs_recovery_task_end_delivery_runs_without_status_rewrite(self) -> None:
        raw = self.service.store.read_task(self.task_id)
        raw["status"] = "needs_recovery"
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "needs_recovery"
        self.service.store.write_task(self.task_id, raw)
        results = TerminalDeliveryCoordinator(self.board).maintain_board(now=_at(400))
        self.assertEqual(len(results), 1)
        self.assertEqual(self.service.store.read_task(self.task_id)["status"], "needs_recovery")
        self.assertEqual(
            self.service.store.read_task(self.task_id)["extensions"][SESSION_EXTENSION_KEY]["session_state"],
            "needs_recovery",
        )

    def test_permission_mode_and_session_survive_a_terminal_delivery_pass(self) -> None:
        """A sibling terminal task's delivery must not disturb the waiting task."""
        sibling = FaultInjectionTestCase._terminal_task(self)
        before = self._snapshot()
        TerminalDeliveryCoordinator(self.board).deliver_now(sibling, now=T0)
        self._assert_unchanged(before, self._snapshot())
        # The index refresh is board-wide; the waiting task keeps its projection.
        report = self.service.get_task(self.task_id)
        self.assertEqual(report.status, "input_required")


class BoundedEventAndProjectionTests(Flow104BoardTestCase):

    def test_delivery_events_never_carry_private_paths_or_bodies(self) -> None:
        task = self.service.create_task(
            "bounded events",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = "completed"
        raw["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY] = (
            build_terminal_delivery_receipt(
                task.id,
                terminal_state="completed",
                terminal_event="task.finalized",
                committed_at=T0,
            )
        )
        self.service.store.write_task(task.id, raw)
        TerminalDeliveryCoordinator(self.board).deliver_now(task.id, now=T0)
        path = self.service.store.task_dir(task.id) / DELIVERY_EVENTS_FILE
        self.assertTrue(path.exists())
        blob = path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn(str(self.root.resolve()), blob)
        self.assertNotIn("report_file", blob)
        self.assertNotIn("reason_summary", blob)
        for line in blob.splitlines():
            self.assertTrue(line.strip())
            json.loads(line)

    def test_public_status_projection_is_path_free(self) -> None:
        task = self.service.create_task(
            "public projection",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = "completed"
        raw["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY] = (
            build_terminal_delivery_receipt(
                task.id,
                terminal_state="completed",
                terminal_event="task.finalized",
                committed_at=T0,
            )
        )
        self.service.store.write_task(task.id, raw)
        from agent_bridge_connect.execution_policy import public_task_view

        view = public_task_view(raw)
        policy = view["execution_policy"]
        delivery = json.dumps(
            {
                "extensions": view["extensions"][TERMINAL_DELIVERY_EXTENSION_KEY],
                "terminal_delivery": policy["terminal_delivery"],
                "delivery_health": policy["delivery_health"],
            }
        )
        self.assertNotIn(str(self.root), delivery)
        self.assertNotIn(str(self.root.resolve()), delivery)
        self.assertNotIn("next_attempt_at", delivery)
        self.assertIn("terminal_delivery", delivery)
        self.assertIn("delivery_health", delivery)
        self.assertEqual(policy["delivery_health"]["stages_total"], 5)


if __name__ == "__main__":
    unittest.main()
