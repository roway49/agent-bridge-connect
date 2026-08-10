from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.execution_contract import extract_callback_validation_from_output
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.executors.hermes import (
    HermesExecutor,
    _iteration_budget_diagnostics,
    _route_hermes_terminal,
)
from agent_bridge_connect.path_model import build_path_plan
from agent_bridge_connect.setup import _executor_config_for


class Phase0ExpectedGapTests(unittest.TestCase):
    """Executable TODOs. Remove expectedFailure as each later phase lands."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.packet = {
            "task_id": "P0GAP-001",
            "title": "phase zero gap",
            "steps": [{"id": 1, "description": "finish later"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.root / "record")},
            "extensions": {},
        }

    def test_claude_command_persists_a_preassigned_session(self) -> None:
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        packet = dict(self.packet)
        packet["extensions"] = {
            "agentbc.session": {
                "executor": "claude",
                "session_id": "123e4567-e89b-12d3-a456-426614174000",
                "run_ids": [],
            }
        }
        command = executor._build_command("prompt", self.root, packet)
        self.assertNotIn("--no-session-persistence", command)
        self.assertIn("--session-id", command)

    def test_claude_setup_default_budget_is_ten_dollars(self) -> None:
        config = _executor_config_for(
            {
                "name": "claude",
                "path": sys.executable,
                "binary": "claude",
                "source": "fixture",
                "capability_level": 1,
                "version": "2.1.226",
            }
        )
        self.assertEqual(config["max_budget_usd"], 10.0)

    def test_hermes_constructor_and_command_accept_max_turns(self) -> None:
        executor = HermesExecutor(
            command=sys.executable,
            transport="direct",
            max_turns=60,
        )
        command = executor._build_command("prompt")
        self.assertIn("--max-turns", command)
        self.assertIn("60", command)

    @unittest.expectedFailure
    def test_hermes_exhaustion_routes_to_system_input_required(self) -> None:
        output = "Iteration budget exhausted (60/60)"
        validation = extract_callback_validation_from_output(output, self.packet, "run-1")
        terminal = _route_hermes_terminal(
            validation,
            0,
            stderr="",
            failure=None,
            iteration=_iteration_budget_diagnostics(output, ""),
        )
        self.assertEqual(terminal.status, "input_required")
        self.assertEqual(terminal.failure["kind"], "resource_limit_exhausted")

    def test_terminal_compaction_preserves_resource_and_session_receipts(self) -> None:
        from agent_bridge_connect.record_management import _compact_terminal_extensions

        compact = _compact_terminal_extensions(
            {
                "agentbc.resources": {"version": 1},
                "agentbc.session": {"version": 1},
            }
        )
        self.assertIn("agentbc.resources", compact)
        self.assertIn("agentbc.session", compact)

    def test_path_plan_owns_iteration_scoped_claude_project(self) -> None:
        config = {"workspace_root": str(self.root / "workspace")}
        workspace = build_path_plan(
            customer_dir=True,
            customer_path=self.root / "customer",
            task_code="ABCD",
            iteration=1,
            config=config,
            task_date="2026-08-10",
        ).to_workspace()
        claude_project = Path(workspace["executor_project_root"])
        managed_artifacts = (
            self.root / "workspace" / "tasks" / "artifacts"
        ).resolve()
        self.assertTrue(claude_project.is_relative_to(managed_artifacts))
        self.assertIn("ABCD-001", claude_project.parts)
        self.assertFalse(claude_project.is_relative_to(self.root / "customer"))


if __name__ == "__main__":
    unittest.main()
