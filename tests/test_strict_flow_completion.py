from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bridge_connect.cli import _is_explicit_retryable_failure
from agent_bridge_connect.execution_contract import (
    FINAL_CALLBACK_PREFIX,
    extract_callback_validation_from_output,
)
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report
from agent_bridge_connect.service import TaskService


class ExecutorFlowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.packet = {
            "task_id": "TEST-001",
            "title": "strict marker",
            "steps": [
                {"id": 1, "description": "first"},
                {"id": 2, "description": "second"},
            ],
            "workspace": {"root": str(root), "project_root": str(root)},
            "task_board": {"root": str(root / "record")},
        }

    def _marker(
        self,
        *,
        task_id: str = "TEST-001",
        final_state: str = "completed",
        step_results: list[dict] | None = None,
    ) -> str:
        payload = {
            "version": 1,
            "task_id": task_id,
            "final_state": final_state,
            "summary": "flow declared",
            "step_results": step_results
            if step_results is not None
            else [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}],
        }
        return f"{FINAL_CALLBACK_PREFIX} {json.dumps(payload)}"

    def _run_executor(
        self, name: str, output: str, *, returncode: int = 0, stderr: str = ""
    ):
        if name == "codex":
            executor = CodexExecutor(command="/bin/true")
            stdout = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": output},
                }
            )
            module = "agent_bridge_connect.executors.codex.subprocess.run"
        elif name == "claude":
            executor = ClaudeExecutor(command="/bin/true", transport="direct")
            stdout = output
            module = "agent_bridge_connect.executors.claude.subprocess.run"
        else:
            executor = HermesExecutor(command="/bin/true", transport="direct")
            stdout = output
            module = "agent_bridge_connect.executors.hermes.subprocess.run"
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        with (
            patch.object(executor, "_start_run_lease"),
            patch.object(executor, "_heartbeat_run"),
            patch.object(executor, "_close_run_lease"),
            patch(module, return_value=completed),
        ):
            start = executor.start(self.packet)
        self.assertTrue(start.ok)
        return executor.poll(start.run_id)

    def test_all_executors_accept_only_full_valid_completed_marker(self) -> None:
        invalid_outputs = {
            "missing": "ordinary final summary",
            "invalid_json": f"{FINAL_CALLBACK_PREFIX} not-json",
            "wrong_task": self._marker(task_id="NOPE-001"),
            "missing_step": self._marker(step_results=[{"id": 1, "status": "done"}]),
            "duplicate_step": self._marker(
                step_results=[{"id": 1, "status": "done"}, {"id": 1, "status": "done"}]
            ),
            "unknown_step": self._marker(
                step_results=[{"id": 1, "status": "done"}, {"id": 9, "status": "done"}]
            ),
            "non_done_step": self._marker(
                step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}]
            ),
            "plain_permission": "Permission denied; approval required before continuing",
        }
        for executor_name in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor_name, case="valid"):
                poll = self._run_executor(executor_name, self._marker())
                self.assertEqual(poll.status, "completed")
                self.assertIsNotNone(poll.result["agent_callback"])
            for case, output in invalid_outputs.items():
                with self.subTest(executor=executor_name, case=case):
                    poll = self._run_executor(executor_name, output)
                    self.assertEqual(poll.status, "failed")
                    self.assertFalse(poll.result["failure"]["retryable"])

    def test_all_executors_preserve_valid_input_required(self) -> None:
        marker = self._marker(
            final_state="input_required",
            step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}],
        )
        for executor_name in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor_name):
                poll = self._run_executor(executor_name, marker)
                self.assertEqual(poll.status, "input_required")

    def test_all_executors_keep_explicit_transport_failure_recoverable(self) -> None:
        output = "executor stopped before a final marker"
        for executor_name in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor_name):
                poll = self._run_executor(
                    executor_name,
                    output,
                    stderr="Connection error: transport unavailable",
                )
                self.assertEqual(poll.status, "needs_recovery")
                self.assertTrue(poll.result["failure"]["retryable"])


class CoreFlowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.workspace = base / "workspace"
        self.project = base / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.workspace / "record",
            config={"workspace_root": str(self.workspace)},
        )
        self.task = self.service.create_task(
            "strict core",
            "codex",
            [{"description": "first"}, {"description": "second"}],
            customer_path=self.project,
        )

    def _callback(self, **updates):
        callback = {
            "version": 1,
            "task_id": self.task.id,
            "final_state": "completed",
            "summary": "complete",
            "step_results": [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}],
        }
        callback.update(updates)
        return callback

    def test_core_rejects_missing_mismatched_duplicate_unknown_and_incomplete_data(self) -> None:
        invalid = [
            self._callback(task_id="NOPE-001"),
            self._callback(step_results=[{"id": 1, "status": "done"}]),
            self._callback(
                step_results=[{"id": 1, "status": "done"}, {"id": 1, "status": "done"}]
            ),
            self._callback(
                step_results=[{"id": 1, "status": "done"}, {"id": 9, "status": "done"}]
            ),
            self._callback(
                step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "failed"}]
            ),
        ]
        for callback in invalid:
            with self.subTest(callback=callback), self.assertRaises(ABCError):
                self.service.finalize_task_from_agent(self.task.id, callback)
        with self.assertRaisesRegex(ABCError, "AGENTBC_FINAL_CALLBACK"):
            self.service.finalize_task_from_executor_exit(
                self.task.id, executor_run_id="run", callback=None
            )
        invalid_json = extract_callback_validation_from_output(
            f"{FINAL_CALLBACK_PREFIX} nope",
            {"task_id": self.task.id, "steps": self.task.steps},
            "run",
        )
        self.assertEqual(invalid_json.code, "completion_marker_json_invalid")

    def test_core_completed_report_requires_valid_marker_all_steps_and_report(self) -> None:
        self.service.finalize_task_from_executor_exit(
            self.task.id,
            executor_run_id="run",
            exit_code=0,
            callback=self._callback(),
        )
        report = generate_report(self.task.id, self.service.board_root)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["marker_valid"])
        self.assertEqual(report["summary"]["steps_done"], 2)
        self.assertTrue(report["flow_contract_satisfied"])
        self.assertEqual(report["failure_code"], "")

    def test_core_preserves_input_required_and_blocked_step_report(self) -> None:
        callback = self._callback(
            final_state="input_required",
            step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}],
        )
        self.service.finalize_task_from_agent(self.task.id, callback)
        report = generate_report(self.task.id, self.service.board_root)
        self.assertEqual(report["status"], "input_required")
        self.assertEqual(report["blocked_steps"], [2])
        self.assertFalse(report["flow_contract_satisfied"])

    def test_failed_report_exposes_marker_validity_and_failure_code(self) -> None:
        self.service.mark_task_failed(
            self.task.id,
            "completion_marker_missing",
            "Executor exited without a final marker",
        )
        report = generate_report(self.task.id, self.service.board_root)
        self.assertFalse(report["marker_valid"])
        self.assertEqual(report["failure_code"], "completion_marker_missing")
        self.assertFalse(report["flow_contract_satisfied"])

    def test_retryable_transport_recovery_does_not_dispatch_automatically(self) -> None:
        failure = {
            "kind": "executor_transport_failure",
            "layer": "transport",
            "message": "connection reset",
            "retryable": True,
        }
        self.assertTrue(_is_explicit_retryable_failure(failure))
        self.service.mark_task_needs_recovery(
            self.task.id, failure["kind"], failure["message"], failure
        )
        current = self.service.get_task(self.task.id)
        events = self.service.store.read_events(self.task.id)
        self.assertEqual(current.status, "needs_recovery")
        self.assertNotIn("worker_dispatched", {event.get("event_type") for event in events})


if __name__ == "__main__":
    unittest.main()
