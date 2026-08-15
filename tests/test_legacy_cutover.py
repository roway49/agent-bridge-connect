"""Targeted tests for the strict legacy-permission cutover and maintenance mode."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.migration import (
    LEGACY_CUTOVER_BLOCKED,
    assert_legacy_cutover_clear,
    assert_maintenance_command_allowed,
    enter_maintenance_mode,
    exit_maintenance_mode,
    is_maintenance_mode,
    legacy_permission_cutover_blocked,
    maintenance_mode_view,
    permission_mode_double_read,
    terminal_historical_projection,
)
from agent_bridge_connect.permission_grants import build_permission_grant
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService

SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


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
            "cutover task",
            "claude",
            [{"id": 1, "description": "one step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        if status != "pending":
            self.service.start_task_run(task.id, "claude")
        return task.id

    def inject_issued_grant(self, task_id: str, input_id: str = "input-x") -> None:
        grant = build_permission_grant(
            executor="claude",
            task_id=task_id,
            input_id=input_id,
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
            "input_id": "input-marker",
            "type": "permission",
            "requested_permission": "full",
            "status": "waiting",
            "summary": "legacy marker",
        }
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())


class LegacyCutoverGateTests(unittest.TestCase):
    def test_empty_board_is_not_blocked(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertFalse(gate["blocked"])
        self.assertEqual(gate["code"], "")

    def test_pending_old_channel_blocks(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="pending")
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["code"], LEGACY_CUTOVER_BLOCKED)
        with self.assertRaises(ABCError) as exc:
            assert_legacy_cutover_clear(harness.service)
        self.assertEqual(exc.exception.code, LEGACY_CUTOVER_BLOCKED)

    def test_running_old_channel_blocks(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="running")
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertTrue(gate["blocked"])

    def test_unconsumed_grant_blocks(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertTrue(gate["blocked"])
        self.assertTrue(
            any("unconsumed_permission_grant" in item["reasons"] for item in gate["blockers"])
        )

    def test_permission_marker_blocks(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_permission_marker(task_id)
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertTrue(gate["blocked"])
        self.assertTrue(
            any("legacy_permission_marker" in item["reasons"] for item in gate["blockers"])
        )

    def test_terminal_historical_task_does_not_block(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        task = harness.service.get_task(task_id)
        task.status = "completed"
        harness.service.store.write_task(task_id, task.to_dict())
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertFalse(gate["blocked"])

    def test_cutover_preflight_is_supported_gate(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        self.assertFalse(harness.service.cutover_preflight()["blocked"])
        harness.create(status="pending")
        gate = harness.service.cutover_preflight()
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["code"], LEGACY_CUTOVER_BLOCKED)
        with self.assertRaises(ABCError):
            harness.service.assert_legacy_cutover_clear()

    def test_upgrade_clearing_reopens_the_gate(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        self.assertTrue(harness.service.cutover_preflight()["blocked"])
        # Clearing the unconsumed grant and the old channel opens the cutover.
        task = harness.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions.pop("agentbc.permission_grant", None)
        task.extensions = extensions
        task.status = "completed"
        harness.service.store.write_task(task_id, task.to_dict())
        self.assertFalse(harness.service.cutover_preflight()["blocked"])
        harness.service.assert_legacy_cutover_clear()


class TerminalHistoricalProjectionTests(unittest.TestCase):
    def test_terminal_projection_preserves_legacy_mode(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        task = harness.service.get_task(task_id)
        task.status = "failed"
        harness.service.store.write_task(task_id, task.to_dict())
        projection = terminal_historical_projection(harness.service.get_task(task_id))
        self.assertTrue(projection["terminal"])
        self.assertTrue(projection["read_only"])
        self.assertEqual(projection["legacy_permission_mode"], "safe")


class PermissionModeDoubleReadTests(unittest.TestCase):
    def test_inherit_is_preserved(self) -> None:
        result = permission_mode_double_read({"permission_mode": "inherit"})
        self.assertEqual(result["legacy_permission_mode"], "inherit")
        self.assertTrue(result["preserve_inherit"])

    def test_safe_reads_through(self) -> None:
        result = permission_mode_double_read({"permission_mode": "safe"})
        self.assertEqual(result["legacy_permission_mode"], "safe")
        self.assertFalse(result["preserve_inherit"])

    def test_missing_falls_back_to_legacy_safe(self) -> None:
        result = permission_mode_double_read({})
        self.assertEqual(result["legacy_permission_mode"], "safe")
        self.assertEqual(result["source"], "legacy_default")


class MaintenanceModeTests(unittest.TestCase):
    def test_maintenance_mode_gates_non_allowed_command(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        enter_maintenance_mode(harness.service, reason="manual bypass install")
        self.assertTrue(is_maintenance_mode(harness.service))
        view = maintenance_mode_view(harness.service)
        self.assertTrue(view["active"])
        self.assertIn("doctor", view["allowed_commands"])
        self.assertIn("status", view["allowed_commands"])
        self.assertIn("report", view["allowed_commands"])
        # Termination commands stay allowed.
        assert_maintenance_command_allowed(harness.service, "cancel")
        assert_maintenance_command_allowed(harness.service, "close")
        assert_maintenance_command_allowed(harness.service, "report")
        # Non-allowed operations fail closed.
        with self.assertRaises(ABCError) as exc:
            assert_maintenance_command_allowed(harness.service, "create")
        self.assertEqual(exc.exception.code, "legacy_permission_cutover_maintenance")
        exit_maintenance_mode(harness.service)
        self.assertFalse(is_maintenance_mode(harness.service))

    def test_maintenance_mode_service_methods(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.service.enter_maintenance_mode("bypass")
        self.assertTrue(harness.service.maintenance_mode()["active"])
        with self.assertRaises(ABCError):
            harness.service.create_task(
                "blocked",
                "claude",
                [{"id": 1, "description": "step"}],
                customer_dir=True,
                customer_path=harness.project,
            )
        harness.service.exit_maintenance_mode()
        self.assertFalse(harness.service.maintenance_mode()["active"])


if __name__ == "__main__":
    unittest.main()
