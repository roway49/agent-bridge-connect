from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX
from agent_bridge_connect.executor_registry import get_executor
from agent_bridge_connect.executors.hermes import HermesExecutor


class Phase3HermesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _packet(
        self,
        *,
        limit: int = 60,
        session_id: str = "",
        run_ids: list[str] | None = None,
    ) -> dict:
        return {
            "task_id": "P3HM-001",
            "title": "phase three Hermes",
            "steps": [{"id": 1, "description": "verify Hermes policy"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.root / "record")},
            "extensions": {
                "agentbc.resources": {
                    "version": 1,
                    "executor": "hermes",
                    "resource": "max_turns",
                    "configured_limit": limit,
                    "current_limit": limit,
                    "multiplier": 2,
                    "exhaustion_count": 0,
                    "last_decision": "",
                    "source": "configured",
                    "created_at": "2026-08-10T00:00:00Z",
                },
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
        callback = {
            "version": 1,
            "task_id": "P3HM-001",
            "final_state": "completed",
            "summary": "done",
            "step_results": [{"id": 1, "status": "done"}],
        }
        return f"{FINAL_CALLBACK_PREFIX} {json.dumps(callback)}"

    def test_constructor_rejects_non_positive_or_non_integer_limits(self) -> None:
        for value in (True, False, 0, -1, 1.5, "60"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    HermesExecutor(
                        command=sys.executable,
                        transport="direct",
                        max_turns=value,  # type: ignore[arg-type]
                    )

    def test_registry_injects_max_turns_at_runtime(self) -> None:
        executor = get_executor(
            "hermes",
            {"command": sys.executable, "transport": "direct", "max_turns": 73},
        )
        self.assertEqual(executor.max_turns, 73)
        command = executor._build_command("prompt")
        self.assertEqual(command.count("--max-turns"), 1)
        self.assertEqual(command[command.index("--max-turns") + 1], "73")

    def test_frozen_task_limit_overrides_current_executor_config(self) -> None:
        executor = HermesExecutor(
            command=sys.executable,
            transport="direct",
            max_turns=99,
        )
        command = executor._build_command("prompt", task_packet=self._packet(limit=41))
        self.assertEqual(command.count("--max-turns"), 1)
        self.assertEqual(command[command.index("--max-turns") + 1], "41")

    def test_fresh_and_resume_commands_are_explicit_and_unambiguous(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="direct")
        fresh = executor._build_command("prompt", task_packet=self._packet(limit=12))
        self.assertEqual(fresh.count("--max-turns"), 1)
        self.assertEqual(fresh.count("-Q"), 1)
        self.assertNotIn("--resume", fresh)
        self.assertNotIn("--continue", fresh)

        resumed = executor._build_command(
            "prompt",
            task_packet=self._packet(
                limit=12,
                session_id="20260810_010203_a1b2c3",
                run_ids=["hermes-run-1"],
            ),
        )
        self.assertEqual(resumed.count("--max-turns"), 1)
        self.assertEqual(resumed.count("-Q"), 1)
        self.assertEqual(resumed.count("--resume"), 1)
        self.assertEqual(
            resumed[resumed.index("--resume") + 1],
            "20260810_010203_a1b2c3",
        )
        self.assertNotIn("--continue", resumed)

    def test_resume_without_session_id_fails_closed(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="direct")
        with self.assertRaisesRegex(ValueError, "session_id is required"):
            executor._build_command(
                "prompt",
                task_packet=self._packet(limit=12, run_ids=["hermes-run-1"]),
            )

    def test_direct_result_exposes_official_session_receipt(self) -> None:
        packet = self._packet(limit=31)
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=self._completed_output(),
            stderr="session_id: 20260810_010203_a1b2c3\n",
        )
        executor = HermesExecutor(command=sys.executable, transport="direct")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_close_run_lease"),
            mock.patch(
                "agent_bridge_connect.executors.hermes.subprocess.run",
                return_value=completed,
            ),
        ):
            started = executor.start(packet)
        self.assertTrue(started.ok)
        receipt = executor.poll(started.run_id).result["execution_session"]
        self.assertEqual(
            receipt,
            {
                "version": 1,
                "executor": "hermes",
                "session_id": "20260810_010203_a1b2c3",
                "resumed": False,
                "persistence": "persistent",
                "source": "stderr_receipt",
            },
        )

    def test_runner_result_exposes_resume_receipt_and_missing_receipt_is_omitted(self) -> None:
        packet = self._packet(
            limit=22,
            session_id="20260810_010203_a1b2c3",
            run_ids=["hermes-run-1"],
        )
        executor = HermesExecutor(command=sys.executable, transport="runner")
        executor._runner_client.health = mock.Mock(return_value={"executors": ["hermes"]})
        executor._runner_client.submit = mock.Mock(
            return_value={"run_id": "runner-hermes-2", "pid": 123}
        )
        executor._runner_client.status = mock.Mock(
            return_value={
                "status": "completed",
                "stdout": self._completed_output(),
                "stderr": "session_id: 20260810_010203_a1b2c3\n",
                "returncode": 0,
                "cwd": str(self.root),
                "output_truncated": False,
            }
        )
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(packet)
            poll = executor.poll(started.run_id)
        self.assertTrue(poll.result["execution_session"]["resumed"])
        command = executor._runner_client.submit.call_args.args[1]
        self.assertEqual(command[command.index("--max-turns") + 1], "22")
        self.assertEqual(
            command[command.index("--resume") + 1],
            "20260810_010203_a1b2c3",
        )

        duplicate = subprocess.CompletedProcess(
            [],
            0,
            stdout=self._completed_output(),
            stderr="session_id: first\nsession_id: second\n",
        )
        direct = HermesExecutor(command=sys.executable, transport="direct")
        with (
            mock.patch.object(direct, "_start_run_lease"),
            mock.patch.object(direct, "_heartbeat_run"),
            mock.patch.object(direct, "_close_run_lease"),
            mock.patch(
                "agent_bridge_connect.executors.hermes.subprocess.run",
                return_value=duplicate,
            ),
        ):
            missing = direct.start(self._packet())
        self.assertNotIn("execution_session", direct.poll(missing.run_id).result)


if __name__ == "__main__":
    unittest.main()
