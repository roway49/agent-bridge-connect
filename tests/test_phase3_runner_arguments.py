from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.runner import RunnerError, RunnerState
from agent_bridge_connect.service import TaskService


def _executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Phase3RunnerArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.root / "record"
        self.binaries = {name: self.root / name for name in ("claude", "hermes", "codex")}
        for path in self.binaries.values():
            _executable(path)
        self.state = RunnerState(self.root / "runner", [self.root], self.binaries)

    def _packet(self, executor: str) -> dict:
        config = {
            "workspace_root": str(self.root / "workspace"),
            "executors": {
                "claude": {"max_budget_usd": 12.5},
                "hermes": {"max_turns": 77},
            },
            "sessions": {"retain_executor_sessions": True},
        }
        task = TaskService(self.board, config=config).create_task(
            "phase 3 runner argv",
            executor,
            [{"id": 1, "description": "validate command"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        packet = task.to_dict()
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(self.board)}
        return packet

    def _resume_packet(self, executor: str, session_id: str) -> dict:
        packet = self._packet(executor)
        raw = TaskService(self.board).store.read_task(packet["task_id"])
        session = raw["extensions"][SESSION_EXTENSION_KEY]
        session.update(
            {
                "session_id": session_id,
                "session_state": "needs_recovery",
                "run_ids": [f"{executor}-run-1"],
            }
        )
        TaskService(self.board).store.write_task(packet["task_id"], raw)
        refreshed = dict(raw)
        refreshed["task_id"] = raw["id"]
        refreshed["task_board"] = {"root": str(self.board)}
        return refreshed

    def test_claude_requires_exact_budget_session_and_project_cwd(self) -> None:
        packet = self._packet("claude")
        session_id = packet["extensions"][SESSION_EXTENSION_KEY]["session_id"]
        command = [
            str(self.binaries["claude"]),
            "-p",
            "--safe-mode",
            "--permission-mode",
            "acceptEdits",
            "--max-budget-usd",
            "12.5",
            "--session-id",
            session_id,
            "prompt",
        ]
        result = self.state.authorize_command("claude", command, str(self.project), packet)
        self.assertTrue(result["authorized"])

        with self.assertRaisesRegex(RunnerError, "runner_resource_argument_mismatch"):
            self.state.authorize_command(
                "claude",
                [*command[:-3], "--max-budget-usd=12.5", *command[-3:]],
                str(self.project),
                packet,
            )
        with self.assertRaisesRegex(RunnerError, "runner_executor_cwd_mismatch"):
            self.state.authorize_command("claude", command, str(self.root), packet)

    def test_hermes_requires_frozen_turns_and_explicit_resume(self) -> None:
        fresh = self._packet("hermes")
        command = [
            str(self.binaries["hermes"]),
            "chat",
            "-q",
            "--max-turns",
            "77",
            "prompt",
        ]
        self.assertTrue(
            self.state.authorize_command("hermes", command, str(self.project), fresh)[
                "authorized"
            ]
        )
        with self.assertRaisesRegex(RunnerError, "runner_resource_argument_mismatch"):
            self.state.authorize_command(
                "hermes",
                [str(self.binaries["hermes"]), "chat", "-q", "prompt"],
                str(self.project),
                fresh,
            )

        session_id = "20260810_010203_phase3"
        resumed = self._resume_packet("hermes", session_id)
        resume_command = [*command[:-1], "--resume", session_id, "prompt"]
        self.assertTrue(
            self.state.authorize_command(
                "hermes", resume_command, str(self.project), resumed
            )["authorized"]
        )
        with self.assertRaisesRegex(RunnerError, "runner_session_argument_mismatch"):
            self.state.authorize_command(
                "hermes",
                [*command[:-1], "--continue", "prompt"],
                str(self.project),
                resumed,
            )

    def test_codex_resume_requires_exact_id_and_forbids_last(self) -> None:
        session_id = "019feed0-0000-7000-8000-000000000003"
        packet = self._resume_packet("codex", session_id)
        command = [
            str(self.binaries["codex"]),
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "resume",
            session_id,
            "prompt",
        ]
        self.assertTrue(
            self.state.authorize_command("codex", command, str(self.project), packet)[
                "authorized"
            ]
        )
        with self.assertRaisesRegex(RunnerError, "runner_session_argument_mismatch"):
            self.state.authorize_command(
                "codex",
                [*command[:-3], "resume", "--last", "prompt"],
                str(self.project),
                packet,
            )


if __name__ == "__main__":
    unittest.main()
