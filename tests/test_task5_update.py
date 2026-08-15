"""Task-5 (PERM-103-005) supported-update preflight and maintenance tests.

The new-task / new-runtime tests here never issue or consume permission
grants and never detect permission markers outside the cutover gate: the
supported ``agentbc update`` preflight either returns
``legacy_permission_cutover_blocked`` with per-task evidence or, only when
the old version is explicitly cleared, ``cutover-ready``.  A manual
wheel/bundle bypass enters maintenance mode that permits doctor/status/
report and explicit termination only and blocks create/dispatch until the
board is cleared.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.cli import main
from agent_bridge_connect.migration import (
    assert_maintenance_command_allowed,
    is_maintenance_mode,
    maintenance_mode_view,
)
from agent_bridge_connect.permission_grants import build_permission_grant
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.update import (
    CUTOVER_READY_FILE,
    CUTOVER_READY_STATE,
    cutover_ready_stamp,
    manual_bypass_install,
    update_preflight,
)

SESSION_ID = "019feed0-0000-7000-8000-0000000000bb"


class _Harness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )

    def close(self) -> None:
        self.temp.cleanup()

    def create(self, status: str = "pending") -> str:
        task = self.service.create_task(
            "task5 update task",
            "claude",
            [{"id": 1, "description": "one step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        if status != "pending":
            self.set_status(task.id, status)
        return task.id

    def set_status(self, task_id: str, status: str) -> None:
        task = self.service.get_task(task_id)
        task.status = status
        self.service.store.write_task(task_id, task.to_dict())

    def inject_issued_grant(self, task_id: str) -> None:
        grant = build_permission_grant(
            executor="claude",
            task_id=task_id,
            input_id="input-g",
            session_id=SESSION_ID,
            source_run_id="claude-run-1",
        )
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.permission_grant"] = grant
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())

    def inject_permission_marker(self, task_id: str) -> None:
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.input"] = {
            "input_id": "input-m",
            "type": "permission",
            "requested_permission": "full",
            "status": "waiting",
            "summary": "legacy marker",
        }
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())


class UpdatePreflightTests(unittest.TestCase):
    def test_clear_board_produces_cutover_ready(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        result = update_preflight(harness.service)
        self.assertEqual(result["state"], CUTOVER_READY_STATE)
        self.assertTrue(result["cutover_ready"])
        self.assertEqual(result["code"], "")
        self.assertEqual(result["blockers"], [])
        self.assertFalse(result["maintenance_active"])
        stamp = result["stamp"]
        self.assertTrue(stamp["cutover_ready"])
        self.assertIn("installed_version", stamp)
        # The durable stamp is board-scoped and survives reloads.
        self.assertEqual(cutover_ready_stamp(harness.service)["state"], CUTOVER_READY_STATE)
        self.assertTrue((harness.board / CUTOVER_READY_FILE).is_file())

    def test_pending_task_blocks_with_task_evidence(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="pending")
        result = update_preflight(harness.service)
        self.assertEqual(result["state"], "legacy_permission_cutover_blocked")
        self.assertEqual(result["code"], "legacy_permission_cutover_blocked")
        self.assertFalse(result["cutover_ready"])
        self.assertEqual(
            result["blockers"],
            [
                {
                    "task_id": task_id,
                    "status": "pending",
                    "reasons": ["old_channel_status:pending"],
                }
            ],
        )

    def test_every_legacy_nonterminal_status_blocks(self) -> None:
        for status in ("pending", "running", "input_required", "needs_recovery"):
            with self.subTest(status=status):
                harness = _Harness()
                self.addCleanup(harness.close)
                harness.create(status=status)
                result = update_preflight(harness.service)
                self.assertFalse(result["cutover_ready"])
                self.assertEqual(result["code"], "legacy_permission_cutover_blocked")
                self.assertEqual(
                    result["blockers"][0]["reasons"],
                    [f"old_channel_status:{status}"],
                )

    def test_unconsumed_grant_and_marker_are_task_evidence(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        harness.inject_permission_marker(task_id)
        result = update_preflight(harness.service)
        self.assertFalse(result["cutover_ready"])
        reasons = result["blockers"][0]["reasons"]
        self.assertIn("unconsumed_permission_grant", reasons)
        self.assertIn("legacy_permission_marker", reasons)

    def test_only_explicitly_cleared_old_version_is_cutover_ready(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        self.assertFalse(update_preflight(harness.service)["cutover_ready"])
        # Explicitly clear the old channel: terminate the task and revoke the
        # unconsumed grant so no marker/grant remains.
        task = harness.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions.pop("agentbc.permission_grant", None)
        extensions.pop("agentbc.input", None)
        task.extensions = extensions
        task.status = "completed"
        harness.service.store.write_task(task_id, task.to_dict())
        result = update_preflight(harness.service)
        self.assertEqual(result["state"], CUTOVER_READY_STATE)
        self.assertTrue(result["cutover_ready"])

    def test_terminal_history_is_not_rewritten_by_preflight(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        harness.set_status(task_id, "failed")
        extensions_before = harness.service.get_task(task_id).extensions
        update_preflight(harness.service)
        extensions_after = harness.service.get_task(task_id).extensions
        self.assertEqual(extensions_after, extensions_before)


class ManualBypassMaintenanceTests(unittest.TestCase):
    def test_bypass_enters_maintenance_that_blocks_create_and_dispatch(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        result = manual_bypass_install(harness.service)
        self.assertEqual(result["state"], "legacy_permission_cutover_maintenance")
        self.assertTrue(result["maintenance"]["active"])
        self.assertTrue(is_maintenance_mode(harness.service))
        # doctor/status/report and explicit termination stay allowed.
        for command in ("doctor", "status", "report", "cancel", "close", "delete"):
            assert_maintenance_command_allowed(harness.service, command)
        # create/dispatch fail closed.
        with self.assertRaises(ABCError) as create_error:
            harness.service.create_task(
                "blocked",
                "claude",
                [{"id": 1, "description": "step"}],
                customer_dir=True,
                customer_path=harness.project,
            )
        self.assertEqual(create_error.exception.code, "legacy_permission_cutover_maintenance")
        # Dispatch is blocked through the same gate.
        with self.assertRaises(ABCError) as dispatch_error:
            assert_maintenance_command_allowed(harness.service, "dispatch")
        self.assertEqual(dispatch_error.exception.code, "legacy_permission_cutover_maintenance")

    def test_supported_update_after_clearing_exits_maintenance(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="pending")
        manual_bypass_install(harness.service)
        self.assertTrue(is_maintenance_mode(harness.service))
        # Clearing the old channel makes the supported update succeed and
        # automatically exits maintenance mode.
        harness.set_status(task_id, "cancelled")
        result = update_preflight(harness.service)
        self.assertTrue(result["cutover_ready"])
        self.assertFalse(is_maintenance_mode(harness.service))
        self.assertFalse(maintenance_mode_view(harness.service)["active"])

    def test_blocked_supported_update_keeps_maintenance(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="running")
        manual_bypass_install(harness.service)
        result = update_preflight(harness.service)
        self.assertFalse(result["cutover_ready"])
        self.assertTrue(result["maintenance_active"])
        self.assertTrue(is_maintenance_mode(harness.service))


class UpdateCliTests(unittest.TestCase):
    def test_cli_update_blocked_returns_code_and_evidence(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="pending")
        code = main(["update", "--root", str(harness.board)])
        self.assertEqual(code, 1)
        self.assertFalse((harness.board / CUTOVER_READY_FILE).is_file())

    def test_cli_update_cutover_ready(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        code = main(["update", "--root", str(harness.board)])
        self.assertEqual(code, 0)
        self.assertTrue((harness.board / CUTOVER_READY_FILE).is_file())
        stamp = cutover_ready_stamp(harness.service) or {}
        self.assertEqual(stamp.get("state"), CUTOVER_READY_STATE)

    def test_cli_bypass_enters_maintenance_and_blocks_create(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        code = main(["update", "--root", str(harness.board), "--bypass"])
        self.assertEqual(code, 0)
        self.assertTrue(is_maintenance_mode(harness.service))
        steps = harness.root / "steps.yaml"
        steps.write_text("steps:\n  - id: 1\n    description: step\n", encoding="utf-8")
        create_code = main(
            [
                "task",
                "create",
                "--root",
                str(harness.board),
                "--title",
                "blocked",
                "--assignee",
                "claude",
                "--steps",
                str(steps),
                "--customer-path",
                str(harness.project),
            ]
        )
        self.assertEqual(create_code, 1)

    def test_cli_update_blocked_output_is_stable_json(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="needs_recovery")
        code = main(["update", "--root", str(harness.board)])
        self.assertEqual(code, 1)
        stamp = cutover_ready_stamp(harness.service)
        self.assertIsNone(stamp)


if __name__ == "__main__":
    unittest.main()
