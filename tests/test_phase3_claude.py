from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX
from agent_bridge_connect.executors.claude import ClaudeExecutor


class Phase3ClaudeExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "customer"
        self.project.mkdir()
        self.claude_project = (
            self.root / "workspace" / "tasks" / "artifacts" / "2026-08-10"
            / "ABCD" / "ABCD-001" / "claude"
        )
        self.session_id = str(uuid.uuid4())

    def _packet(
        self,
        *,
        retain: bool = False,
        run_ids: list[str] | None = None,
        budget: float = 2.5,
    ) -> dict:
        project_path = self.project if retain else self.claude_project
        return {
            "task_id": "ABCD-001",
            "title": "phase three Claude",
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {
                "customer_dir": True,
                "customer_path": str(self.project),
                "root": str(self.project),
                "project_root": str(self.project),
                "default_path": str(self.project),
                "executor_project_root": str(self.claude_project),
                "agentbc_root": str(self.root / "workspace"),
                "artifact_root": str(self.project),
                "report_root": str(self.root / "workspace" / "tasks" / "report"),
                "task_file": str(self.root / "workspace" / "tasks" / "task.md"),
                "report_file": str(self.root / "workspace" / "tasks" / "report.md"),
                "task_code": "ABCD",
                "iteration": "001",
                "task_date": "2026-08-10",
            },
            "task_board": {"root": str(self.root / "board")},
            "extensions": {
                "agentbc.resources": {
                    "version": 1,
                    "executor": "claude",
                    "resource": "max_budget_usd",
                    "current_limit": budget,
                },
                "agentbc.session": {
                    "version": 1,
                    "executor": "claude",
                    "retain": retain,
                    "session_id": self.session_id,
                    "session_state": "pending",
                    "project_mode": "native" if retain else "ephemeral",
                    "project_path": str(project_path),
                    "run_ids": list(run_ids or []),
                },
            },
        }

    @staticmethod
    def _completed_output() -> str:
        callback = {
            "version": 1,
            "task_id": "ABCD-001",
            "final_state": "completed",
            "summary": "done",
            "step_results": [{"id": 1, "status": "done"}],
        }
        return f"{FINAL_CALLBACK_PREFIX} {json.dumps(callback)}\n"

    def test_capability_and_constructor_default_enable_persistent_sessions(self) -> None:
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        self.assertTrue(executor.capabilities().resume)
        self.assertEqual(executor.max_budget_usd, 10.0)

    def test_fresh_command_uses_snapshot_budget_and_preallocated_session(self) -> None:
        executor = ClaudeExecutor(
            command=sys.executable,
            transport="direct",
            max_budget_usd=99.0,
        )
        command = executor._build_command("prompt", self.project, self._packet())

        self.assertNotIn("--no-session-persistence", command)
        self.assertNotIn("--resume", command)
        self.assertEqual(command[command.index("--session-id") + 1], self.session_id)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.5")
        self.assertIn("--safe-mode", command)
        self.assertIn("--add-dir", command)

    def test_resume_command_uses_the_same_session_id(self) -> None:
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        command = executor._build_command(
            "prompt",
            self.project,
            self._packet(run_ids=["claude-ABCD-001-first"]),
        )

        self.assertNotIn("--session-id", command)
        self.assertEqual(command[command.index("--resume") + 1], self.session_id)

    @mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_ephemeral_start_creates_only_path_plan_directory_and_emits_receipt(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=self._completed_output(), stderr=""
        )
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())

        self.assertTrue(started.ok)
        self.assertTrue(self.claude_project.is_dir())
        self.assertEqual(Path(run.call_args.kwargs["cwd"]), self.claude_project)
        receipt = executor.poll(started.run_id).result["execution_session"]
        self.assertEqual(
            receipt,
            {
                "version": 1,
                "executor": "claude",
                "session_id": self.session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            },
        )

    @mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_retained_start_uses_user_directory_without_creating_ephemeral_path(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=self._completed_output(), stderr=""
        )
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet(retain=True))

        self.assertTrue(started.ok)
        self.assertEqual(Path(run.call_args.kwargs["cwd"]), self.project)
        self.assertFalse(self.claude_project.exists())

    @mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_timeout_preserves_available_session_receipt(self, run: mock.Mock) -> None:
        run.side_effect = subprocess.TimeoutExpired(
            cmd=[sys.executable], timeout=1, output="partial", stderr="timeout"
        )
        executor = ClaudeExecutor(
            command=sys.executable,
            transport="direct",
            timeout_s=1,
        )
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_mark_run_stale"),
        ):
            started = executor.start(
                self._packet(run_ids=["claude-ABCD-001-first"])
            )

        polled = executor.poll(started.run_id)
        self.assertEqual(polled.status, "needs_recovery")
        self.assertEqual(polled.result["execution_session"]["session_id"], self.session_id)
        self.assertTrue(polled.result["execution_session"]["resumed"])

    def test_ephemeral_path_mismatch_fails_without_creating_either_directory(self) -> None:
        packet = self._packet()
        injected = self.root / "outside" / "claude"
        packet["extensions"]["agentbc.session"]["project_path"] = str(injected)
        executor = ClaudeExecutor(command=sys.executable, transport="direct")

        started = executor.start(packet)

        self.assertFalse(started.ok)
        self.assertIn("does not match", started.message)
        self.assertFalse(injected.exists())
        self.assertFalse(self.claude_project.exists())


if __name__ == "__main__":
    unittest.main()
