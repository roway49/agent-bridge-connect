from __future__ import annotations
import stat
import tempfile
import unittest
from pathlib import Path
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.runner import RunnerError, RunnerState
from agent_bridge_connect.service import TaskService
import json as _hermes_executor_json
import subprocess as _hermes_executor_subprocess
import sys as _hermes_executor_sys
import tempfile as _hermes_executor_tempfile
import unittest as _hermes_executor_unittest
from pathlib import Path as _hermes_executor_Path
from unittest import mock as _hermes_executor_mock
from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX as _hermes_executor_FINAL_CALLBACK_PREFIX
from agent_bridge_connect.executor_registry import get_executor as _hermes_executor_get_executor
from agent_bridge_connect.executors.hermes import HermesExecutor as _hermes_executor_HermesExecutor


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
        config = {"workspace_root": str(self.root / "workspace"), "executors": {"claude": {"max_budget_usd": 12.5}, "hermes": {"max_turns": 77}}, "sessions": {"retain_executor_sessions": True}}
        task = TaskService(self.board, config=config).create_task("phase 3 runner argv", executor, [{"id": 1, "description": "validate command"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        packet = task.to_dict()
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(self.board)}
        return packet

    def _resume_packet(self, executor: str, session_id: str) -> dict:
        packet = self._packet(executor)
        raw = TaskService(self.board).store.read_task(packet["task_id"])
        session = raw["extensions"][SESSION_EXTENSION_KEY]
        session.update({"session_id": session_id, "session_state": "needs_recovery", "run_ids": [f"{executor}-run-1"]})
        TaskService(self.board).store.write_task(packet["task_id"], raw)
        refreshed = dict(raw)
        refreshed["task_id"] = raw["id"]
        refreshed["task_board"] = {"root": str(self.board)}
        return refreshed

    def test_claude_requires_exact_budget_session_and_project_cwd(self) -> None:
        packet = self._packet("claude")
        session_id = packet["extensions"][SESSION_EXTENSION_KEY]["session_id"]
        command = [str(self.binaries["claude"]), "-p", "--safe-mode", "--permission-mode", "acceptEdits", "--max-budget-usd", "12.5", "--session-id", session_id, "prompt"]
        result = self.state.authorize_command("claude", command, str(self.project), packet)
        self.assertTrue(result["authorized"])
        with self.assertRaisesRegex(RunnerError, "runner_resource_argument_mismatch"):
            self.state.authorize_command("claude", [*command[:-3], "--max-budget-usd=12.5", *command[-3:]], str(self.project), packet)
        with self.assertRaisesRegex(RunnerError, "runner_executor_cwd_mismatch"):
            self.state.authorize_command("claude", command, str(self.root), packet)

    def test_hermes_requires_frozen_turns_and_explicit_resume(self) -> None:
        fresh = self._packet("hermes")
        command = [str(self.binaries["hermes"]), "chat", "-q", "--max-turns", "77", "prompt"]
        self.assertTrue(self.state.authorize_command("hermes", command, str(self.project), fresh)["authorized"])
        with self.assertRaisesRegex(RunnerError, "runner_resource_argument_mismatch"):
            self.state.authorize_command("hermes", [str(self.binaries["hermes"]), "chat", "-q", "prompt"], str(self.project), fresh)
        session_id = "20260810_010203_phase3"
        resumed = self._resume_packet("hermes", session_id)
        resume_command = [*command[:-1], "--resume", session_id, "prompt"]
        self.assertTrue(self.state.authorize_command("hermes", resume_command, str(self.project), resumed)["authorized"])
        with self.assertRaisesRegex(RunnerError, "runner_session_argument_mismatch"):
            self.state.authorize_command("hermes", [*command[:-1], "--continue", "prompt"], str(self.project), resumed)

    def test_hermes_acp_authorization_uses_frozen_transport(self) -> None:
        packet = self._packet("hermes")
        command = [str(self.binaries["hermes"]), "acp"]
        self.assertTrue(self.state.authorize_command("hermes", command, str(self.project), packet)["authorized"])
        with self.assertRaisesRegex(RunnerError, "exact headless protocol entrypoint"):
            self.state.authorize_command("hermes", [*command, "--unexpected"], str(self.project), packet)
        drifted = dict(packet)
        drifted["extensions"] = dict(packet["extensions"])
        permission = dict(drifted["extensions"]["agentbc.permission"])
        permission["mapping"] = dict(permission["mapping"])
        permission["mapping"]["hermes"] = dict(permission["mapping"]["hermes"])
        permission["mapping"]["hermes"]["transport"] = "direct"
        drifted["extensions"]["agentbc.permission"] = permission
        with self.assertRaisesRegex(RunnerError, "stale or command-injected permission authorization"):
            self.state.authorize_command("hermes", command, str(self.project), drifted)

    def test_fresh_registered_acp_run_stays_fresh_after_run_id_append(self) -> None:
        packet = self._packet("hermes")
        task_id = packet["task_id"]
        raw = TaskService(self.board).store.read_task(task_id)
        session = raw["extensions"][SESSION_EXTENSION_KEY]
        run_id = "hermes-T2AG-001-first"
        session.update({"run_ids": [run_id], "run_resume_facts": {run_id: False}, "session_state": "pending"})
        TaskService(self.board).store.write_task(task_id, raw)
        packet = dict(raw)
        packet["task_id"] = task_id
        packet["task_board"] = {"root": str(self.board)}
        packet["_agentbc_resume_fact"] = {"run_id": run_id, "resumed": False, "session_id": ""}
        self.assertTrue(self.state.authorize_command("hermes", [str(self.binaries["hermes"]), "acp"], str(self.project), packet, executor_run_id=run_id)["authorized"])

    def test_codex_resume_requires_exact_id_and_forbids_last(self) -> None:
        session_id = "019feed0-0000-7000-8000-000000000003"
        packet = self._resume_packet("codex", session_id)
        command = [str(self.binaries["codex"]), "exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check", "resume", session_id, "prompt"]
        self.assertTrue(self.state.authorize_command("codex", command, str(self.project), packet)["authorized"])
        with self.assertRaisesRegex(RunnerError, "runner_session_argument_mismatch"):
            self.state.authorize_command("codex", [*command[:-3], "resume", "--last", "prompt"], str(self.project), packet)


class _hermes_executor_Phase3HermesTests(_hermes_executor_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _hermes_executor_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _hermes_executor_Path(self.temporary.name)

    def _packet(self, *, limit: int = 60, session_id: str = "", run_ids: list[str] | None = None) -> dict:
        return {
            "task_id": "P3HM-001",
            "title": "phase three Hermes",
            "steps": [{"id": 1, "description": "verify Hermes policy"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.root / "record")},
            "extensions": {
                "agentbc.resources": {"version": 1, "executor": "hermes", "resource": "max_turns", "configured_limit": limit, "current_limit": limit, "multiplier": 2, "exhaustion_count": 0, "last_decision": "", "source": "configured", "created_at": "2026-08-10T00:00:00Z"},
                "agentbc.session": {
                    "version": 1,
                    "executor": "hermes",
                    "retain": False,
                    "session_id": session_id,
                    "session_state": "pending" if not run_ids else "active",
                    "project_mode": "none",
                    "project_path": "",
                    "run_ids": list(run_ids or []),
                    "resume_count": 0,
                    "cleanup": {"state": "not_requested", "attempts": 0},
                    "created_at": "2026-08-10T00:00:00Z",
                },
            },
        }

    @staticmethod
    def _completed_output() -> str:
        callback = {"version": 1, "task_id": "P3HM-001", "final_state": "completed", "summary": "done", "step_results": [{"id": 1, "status": "done"}]}
        return f"{_hermes_executor_FINAL_CALLBACK_PREFIX} {_hermes_executor_json.dumps(callback)}"

    def test_constructor_rejects_non_positive_or_non_integer_limits(self) -> None:
        for value in (True, False, 0, -1, 1.5, "60"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="direct", max_turns=value)

    def test_registry_injects_max_turns_at_runtime(self) -> None:
        executor = _hermes_executor_get_executor("hermes", {"command": _hermes_executor_sys.executable, "transport": "direct", "max_turns": 73})
        self.assertEqual(executor.max_turns, 73)
        command = executor._build_command("prompt")
        self.assertEqual(command.count("--max-turns"), 1)
        self.assertEqual(command[command.index("--max-turns") + 1], "73")

    def test_frozen_task_limit_overrides_current_executor_config(self) -> None:
        executor = _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="direct", max_turns=99)
        command = executor._build_command("prompt", task_packet=self._packet(limit=41))
        self.assertEqual(command.count("--max-turns"), 1)
        self.assertEqual(command[command.index("--max-turns") + 1], "41")

    def test_fresh_and_resume_commands_are_explicit_and_unambiguous(self) -> None:
        executor = _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="direct")
        fresh = executor._build_command("prompt", task_packet=self._packet(limit=12))
        self.assertEqual(fresh.count("--max-turns"), 1)
        self.assertEqual(fresh.count("--oneshot"), 1)
        self.assertNotIn("-Q", fresh)
        self.assertNotIn("--resume", fresh)
        self.assertNotIn("--continue", fresh)
        resumed = executor._build_command("prompt", task_packet=self._packet(limit=12, session_id="20260810_010203_a1b2c3", run_ids=["hermes-run-1"]))
        self.assertEqual(resumed.count("--max-turns"), 1)
        self.assertEqual(resumed.count("--oneshot"), 1)
        self.assertNotIn("-Q", resumed)
        self.assertEqual(resumed.count("--resume"), 1)
        self.assertEqual(resumed[resumed.index("--resume") + 1], "20260810_010203_a1b2c3")
        self.assertNotIn("--continue", resumed)

    def test_resume_without_session_id_fails_closed(self) -> None:
        executor = _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="direct")
        with self.assertRaisesRegex(ValueError, "session_id is required"):
            executor._build_command("prompt", task_packet=self._packet(limit=12, run_ids=["hermes-run-1"]))

    def test_direct_result_exposes_official_session_receipt(self) -> None:
        packet = self._packet(limit=31)
        completed = _hermes_executor_subprocess.CompletedProcess([], 0, stdout=self._completed_output(), stderr="session_id: 20260810_010203_a1b2c3\n")
        executor = _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="direct")
        with _hermes_executor_mock.patch.object(executor, "_start_run_lease"), _hermes_executor_mock.patch.object(executor, "_heartbeat_run"), _hermes_executor_mock.patch.object(executor, "_close_run_lease"), _hermes_executor_mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", return_value=completed):
            started = executor.start(packet)
        self.assertTrue(started.ok)
        receipt = executor.poll(started.run_id).result["execution_session"]
        self.assertEqual(receipt, {"version": 1, "executor": "hermes", "session_id": "20260810_010203_a1b2c3", "resumed": False, "persistence": "persistent", "source": "stderr_receipt"})

    def test_runner_result_exposes_resume_receipt_and_missing_receipt_is_omitted(self) -> None:
        packet = self._packet(limit=22, session_id="20260810_010203_a1b2c3", run_ids=["hermes-run-1"])
        executor = _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="runner")
        executor._runner_client.health = _hermes_executor_mock.Mock(return_value={"executors": ["hermes"]})
        executor._runner_client.submit = _hermes_executor_mock.Mock(side_effect=lambda *args, **kwargs: {"run_id": kwargs["executor_run_id"], "pid": 123})
        executor._runner_client.status = _hermes_executor_mock.Mock(return_value={"status": "completed", "stdout": self._completed_output(), "stderr": "session_id: 20260810_010203_a1b2c3\n", "returncode": 0, "cwd": str(self.root), "output_truncated": False})
        with _hermes_executor_mock.patch.object(executor, "_start_run_lease"), _hermes_executor_mock.patch.object(executor, "_heartbeat_run"), _hermes_executor_mock.patch.object(executor, "_close_run_lease"):
            started = executor.start(packet)
            poll = executor.poll(started.run_id)
        self.assertTrue(poll.result["execution_session"]["resumed"])
        command = executor._runner_client.submit.call_args.args[1]
        self.assertEqual(command[command.index("--max-turns") + 1], "22")
        self.assertEqual(command[command.index("--resume") + 1], "20260810_010203_a1b2c3")
        duplicate = _hermes_executor_subprocess.CompletedProcess([], 0, stdout=self._completed_output(), stderr="session_id: first\nsession_id: second\n")
        direct = _hermes_executor_HermesExecutor(command=_hermes_executor_sys.executable, transport="direct")
        with _hermes_executor_mock.patch.object(direct, "_start_run_lease"), _hermes_executor_mock.patch.object(direct, "_heartbeat_run"), _hermes_executor_mock.patch.object(direct, "_close_run_lease"), _hermes_executor_mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", return_value=duplicate):
            missing = direct.start(self._packet())
        self.assertNotIn("execution_session", direct.poll(missing.run_id).result)
