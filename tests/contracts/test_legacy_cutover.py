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
import sys as _runtime_contract_sys
import tempfile as _runtime_contract_tempfile
import unittest as _runtime_contract_unittest
from pathlib import Path as _runtime_contract_Path
from agent_bridge_connect.execution_contract import extract_callback_validation_from_output as _runtime_contract_extract_callback_validation_from_output
from agent_bridge_connect.executors.claude import ClaudeExecutor as _runtime_contract_ClaudeExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor as _runtime_contract_HermesExecutor, _iteration_budget_diagnostics as _runtime_contract__iteration_budget_diagnostics, _route_hermes_terminal as _runtime_contract__route_hermes_terminal
from agent_bridge_connect.path_model import build_path_plan as _runtime_contract_build_path_plan
from agent_bridge_connect.setup import _executor_config_for as _runtime_contract__executor_config_for

SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


class _Harness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})

    def close(self) -> None:
        self.temp.cleanup()

    def create(self, status: str = "pending") -> str:
        task = self.service.create_task("cutover task", "claude", [{"id": 1, "description": "one step"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        if status != "pending":
            self.service.start_task_run(task.id, "claude")
        return task.id

    def inject_issued_grant(self, task_id: str, input_id: str = "input-x") -> None:
        grant = build_permission_grant(executor="claude", task_id=task_id, input_id=input_id, session_id=SESSION_ID, source_run_id="claude-run-1")
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.permission_grant"] = grant
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())

    def inject_permission_marker(self, task_id: str) -> None:
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.input"] = {"input_id": "input-marker", "type": "permission", "requested_permission": "full", "status": "waiting", "summary": "legacy marker"}
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
        self.assertTrue(any(("unconsumed_permission_grant" in item["reasons"] for item in gate["blockers"])))

    def test_permission_marker_blocks(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_permission_marker(task_id)
        gate = legacy_permission_cutover_blocked(harness.service)
        self.assertTrue(gate["blocked"])
        self.assertTrue(any(("legacy_permission_marker" in item["reasons"] for item in gate["blockers"])))

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

    def test_v1_terminal_projection_projects_missing_sources_as_null(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        task = harness.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.permission"] = {"requested_mode": "full", "effective_mode": "full", "selection_source": "legacy_task"}
        task.extensions = extensions
        task.status = "completed"
        harness.service.store.write_task(task_id, task.to_dict())
        projection = terminal_historical_projection(harness.service.get_task(task_id))
        permission = projection["permission"]
        self.assertEqual(permission["effective_mode"], "full")
        self.assertEqual(projection["legacy_permission_mode"], "full")
        self.assertIsNone(permission["configured_mode"])
        self.assertIsNone(permission["inherited_mode"])
        self.assertIsNone(permission["task_override"])
        self.assertIsNone(permission["version"])
        self.assertEqual(permission["selection_source"], "legacy_task")

    def test_v2_terminal_projection_keeps_source_fields(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        task = harness.service.get_task(task_id)
        task.status = "cancelled"
        harness.service.store.write_task(task_id, task.to_dict())
        projection = terminal_historical_projection(harness.service.get_task(task_id))
        permission = projection["permission"]
        self.assertEqual(permission["effective_mode"], "safe")
        self.assertEqual(permission["configured_mode"], "safe")
        self.assertEqual(permission["version"], 2)
        self.assertIsNone(permission["inherited_mode"])
        self.assertEqual(permission["task_override"], "safe")
        self.assertEqual(permission["selection_source"], "explicit_task")


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
        assert_maintenance_command_allowed(harness.service, "cancel")
        assert_maintenance_command_allowed(harness.service, "close")
        assert_maintenance_command_allowed(harness.service, "report")
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
            harness.service.create_task("blocked", "claude", [{"id": 1, "description": "step"}], customer_dir=True, customer_path=harness.project)
        harness.service.exit_maintenance_mode()
        self.assertFalse(harness.service.maintenance_mode()["active"])


class _runtime_contract_Phase0ExpectedGapTests(_runtime_contract_unittest.TestCase):
    """Executable TODOs. Remove expectedFailure as each later phase lands."""

    def setUp(self) -> None:
        self.temporary = _runtime_contract_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _runtime_contract_Path(self.temporary.name)
        self.packet = {"task_id": "P0GAP-001", "title": "phase zero gap", "steps": [{"id": 1, "description": "finish later"}], "workspace": {"root": str(self.root), "project_root": str(self.root)}, "task_board": {"root": str(self.root / "record")}, "extensions": {}}

    def test_claude_command_persists_a_preassigned_session(self) -> None:
        executor = _runtime_contract_ClaudeExecutor(command=_runtime_contract_sys.executable, transport="direct")
        packet = dict(self.packet)
        packet["extensions"] = {"agentbc.session": {"executor": "claude", "session_id": "123e4567-e89b-12d3-a456-426614174000", "run_ids": []}}
        command = executor._build_command("prompt", self.root, packet)
        self.assertNotIn("--no-session-persistence", command)
        self.assertIn("--session-id", command)

    def test_claude_setup_default_budget_is_ten_dollars(self) -> None:
        config = _runtime_contract__executor_config_for({"name": "claude", "path": _runtime_contract_sys.executable, "binary": "claude", "source": "fixture", "capability_level": 1, "version": "2.1.226"})
        self.assertEqual(config["max_budget_usd"], 10.0)

    def test_hermes_constructor_and_command_accept_max_turns(self) -> None:
        executor = _runtime_contract_HermesExecutor(command=_runtime_contract_sys.executable, transport="direct", max_turns=60)
        command = executor._build_command("prompt")
        self.assertIn("--max-turns", command)
        self.assertIn("60", command)

    def test_hermes_exhaustion_routes_to_system_input_required(self) -> None:
        packet = dict(self.packet)
        packet["extensions"] = {"agentbc.resources": {"version": 1, "executor": "hermes", "resource": "max_turns", "configured_limit": 60, "current_limit": 60, "multiplier": 2, "exhaustion_count": 0, "last_decision": "", "source": "configured", "created_at": "2026-08-10T00:00:00Z"}}
        output = "Iteration budget exhausted (60/60)"
        validation = _runtime_contract_extract_callback_validation_from_output(output, packet, "run-1")
        terminal = _runtime_contract__route_hermes_terminal(validation, 0, stderr="", failure=None, iteration=_runtime_contract__iteration_budget_diagnostics(output, ""), task_packet=packet)
        self.assertEqual(terminal.status, "input_required")
        self.assertEqual(terminal.failure["kind"], "resource_limit_exhausted")
        self.assertIsNone(terminal.callback)
        self.assertTrue(terminal.resource_exhaustion["detected"])
        self.assertEqual(terminal.resource_exhaustion["limit"], 60)
        self.assertIs(terminal.resource_exhaustion["limit_matches_snapshot"], True)

    def test_terminal_compaction_preserves_resource_and_session_receipts(self) -> None:
        from agent_bridge_connect.record_management import _compact_terminal_extensions

        compact = _compact_terminal_extensions({"agentbc.resources": {"version": 1}, "agentbc.session": {"version": 1}})
        self.assertIn("agentbc.resources", compact)
        self.assertIn("agentbc.session", compact)

    def test_path_plan_owns_iteration_scoped_claude_project(self) -> None:
        config = {"workspace_root": str(self.root / "workspace")}
        workspace = _runtime_contract_build_path_plan(customer_dir=True, customer_path=self.root / "customer", task_code="ABCD", iteration=1, config=config, task_date="2026-08-10").to_workspace()
        claude_project = _runtime_contract_Path(workspace["executor_project_root"])
        managed_artifacts = (self.root / "workspace" / "tasks" / "artifacts").resolve()
        self.assertTrue(claude_project.is_relative_to(managed_artifacts))
        self.assertIn("ABCD-001", claude_project.parts)
        self.assertFalse(claude_project.is_relative_to(self.root / "customer"))
