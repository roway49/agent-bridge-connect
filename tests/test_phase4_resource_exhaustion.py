from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import PollResult, ProbeResult, StartResult
from agent_bridge_connect.execution_contract import (
    FINAL_CALLBACK_PREFIX,
    extract_callback_validation_from_output,
    route_executor_terminal,
)
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.executors.claude import (
    ClaudeExecutor,
    _claude_resource_exhaustion,
)
from agent_bridge_connect.executors.hermes import (
    HermesExecutor,
    _iteration_budget_diagnostics,
    _route_hermes_terminal,
)
from agent_bridge_connect.run_lease import RunLeaseState, load_lease
from agent_bridge_connect.service import TaskService


def _claude_packet(*, budget: float = 2.5) -> dict:
    return {
        "task_id": "P4CL-001",
        "title": "phase four Claude",
        "steps": [{"id": 1, "description": "run"}],
        "workspace": {"root": "/tmp", "project_root": "/tmp"},
        "task_board": {"root": "/tmp/board"},
        "extensions": {
            "agentbc.resources": {
                "version": 1,
                "executor": "claude",
                "resource": "max_budget_usd",
                "configured_limit": budget,
                "current_limit": budget,
                "multiplier": 2,
                "exhaustion_count": 0,
                "last_decision": "",
                "source": "configured",
                "created_at": "2026-08-10T00:00:00Z",
            }
        },
    }


class ClaudeExhaustionDetectionTests(unittest.TestCase):
    def test_structured_error_max_budget_usd_is_detected(self) -> None:
        packet = _claude_packet(budget=2.5)
        parsed = {
            "type": "error",
            "subtype": "error_max_budget_usd",
            "message": "Exceeded USD budget ($2.5) while running tools",
        }
        detection = _claude_resource_exhaustion(
            json.dumps(parsed),
            "",
            parsed,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
        )
        self.assertIsNotNone(detection)
        self.assertTrue(detection["detected"])
        self.assertEqual(detection["source"], "structured_error_max_budget_usd")
        self.assertEqual(detection["limit"], 2.5)
        self.assertIs(detection["limit_matches_snapshot"], True)

    def test_structured_detection_works_with_zero_exit_and_stream_events(self) -> None:
        packet = _claude_packet(budget=2.5)
        parsed = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "work"}]}},
            {"type": "error", "subtype": "error_max_budget_usd", "message": "Exceeded USD budget ($2.5)"},
        ]
        detection = _claude_resource_exhaustion(
            "",
            "",
            parsed,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            0,
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection["limit"], 2.5)

    def test_structured_subtype_without_amount_uses_snapshot_limit(self) -> None:
        packet = _claude_packet(budget=2.5)
        parsed = {
            "type": "error",
            "subtype": "error_max_budget_usd",
            "message": "Budget limit reached",
        }
        detection = _claude_resource_exhaustion(
            json.dumps(parsed),
            "",
            parsed,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
        )
        self.assertIsNotNone(detection)
        self.assertIsNone(detection["limit"])
        self.assertIsNone(detection["limit_matches_snapshot"])

    def test_text_fallback_requires_nonzero_exit_and_exact_phrase(self) -> None:
        packet = _claude_packet(budget=2.5)
        stderr = "Error: Exceeded USD budget ($2.5). This run used too much."
        detection = _claude_resource_exhaustion(
            "some stdout",
            stderr,
            None,
            packet,
            extract_callback_validation_from_output("some stdout", packet, "run-1"),
            1,
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection["source"], "text_exceeded_usd_budget")
        self.assertEqual(detection["limit"], 2.5)
        self.assertIs(detection["limit_matches_snapshot"], True)

    def test_text_fallback_rejects_zero_exit(self) -> None:
        packet = _claude_packet(budget=2.5)
        stderr = "Error: Exceeded USD budget ($2.5)"
        detection = _claude_resource_exhaustion(
            "",
            stderr,
            None,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            0,
        )
        self.assertIsNone(detection)

    def test_text_fallback_rejects_pseudo_signal_wording(self) -> None:
        packet = _claude_packet(budget=2.5)
        for pseudo in (
            "the budget was exceeded by USD 100",
            "exceeded USD budget last run",
            "budget exceeded: USD 2.5",
        ):
            with self.subTest(pseudo=pseudo):
                detection = _claude_resource_exhaustion(
                    "",
                    pseudo,
                    None,
                    packet,
                    extract_callback_validation_from_output("", packet, "run-1"),
                    1,
                )
                self.assertIsNone(detection)

    def test_text_fallback_rejects_prompt_echo_in_stdout(self) -> None:
        packet = _claude_packet(budget=2.5)
        detection = _claude_resource_exhaustion(
            "Task text says Exceeded USD budget ($2.5)",
            "",
            None,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
        )
        self.assertIsNone(detection)

    def test_text_fallback_rejects_valid_callback(self) -> None:
        packet = _claude_packet(budget=2.5)
        callback = {
            "version": 1,
            "task_id": "P4CL-001",
            "final_state": "completed",
            "summary": "done",
            "step_results": [{"id": 1, "status": "done"}],
        }
        output = f"{FINAL_CALLBACK_PREFIX} {json.dumps(callback)}"
        validation = extract_callback_validation_from_output(output, packet, "run-1")
        detection = _claude_resource_exhaustion(
            output,
            "Error: Exceeded USD budget ($2.5)",
            None,
            packet,
            validation,
            1,
        )
        self.assertIsNone(detection)

    def test_receipt_limit_mismatch_fails_closed(self) -> None:
        packet = _claude_packet(budget=5.0)
        parsed = {
            "type": "error",
            "subtype": "error_max_budget_usd",
            "message": "Exceeded USD budget ($2.5)",
        }
        detection = _claude_resource_exhaustion(
            json.dumps(parsed),
            "",
            parsed,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
        )
        self.assertIsNotNone(detection)
        self.assertIs(detection["limit_matches_snapshot"], False)
        terminal = route_executor_terminal(
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
            executor_name="claude",
            stderr="",
            resource_exhaustion=detection,
        )
        self.assertEqual(terminal.status, "needs_recovery")
        self.assertEqual(
            terminal.failure["kind"],
            "resource_exhaustion_receipt_mismatch",
        )

    def test_valid_callback_wins_over_exhaustion_priority(self) -> None:
        packet = _claude_packet(budget=2.5)
        callback = {
            "version": 1,
            "task_id": "P4CL-001",
            "final_state": "completed",
            "summary": "done",
            "step_results": [{"id": 1, "status": "done"}],
        }
        output = f"{FINAL_CALLBACK_PREFIX} {json.dumps(callback)}"
        validation = extract_callback_validation_from_output(output, packet, "run-1")
        detection = _claude_resource_exhaustion(
            output,
            "Error: Exceeded USD budget ($2.5)",
            None,
            packet,
            validation,
            0,
        )
        self.assertIsNone(detection)
        terminal = route_executor_terminal(
            validation,
            0,
            executor_name="claude",
            resource_exhaustion=detection,
        )
        self.assertEqual(terminal.status, "completed")
        self.assertIsNotNone(terminal.callback)

    def test_transport_failure_wins_over_exhaustion_priority(self) -> None:
        packet = _claude_packet(budget=2.5)
        detection = _claude_resource_exhaustion(
            "",
            "Error: Exceeded USD budget ($2.5)",
            None,
            packet,
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
        )
        terminal = route_executor_terminal(
            extract_callback_validation_from_output("", packet, "run-1"),
            1,
            executor_name="claude",
            stderr="Connection error: reset",
            runtime_failure={
                "kind": "executor_transport_failure",
                "layer": "transport",
                "message": "Connection error: reset",
                "retryable": True,
            },
            resource_exhaustion=detection,
        )
        self.assertEqual(terminal.status, "needs_recovery")
        self.assertTrue(terminal.failure["retryable"])
        self.assertEqual(terminal.failure["kind"], "executor_transport_failure")


class ClaudeExecutorEndToEndTests(unittest.TestCase):
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

    def _packet(self, *, budget: float = 2.5) -> dict:
        return {
            "task_id": "ABCD-001",
            "title": "phase four Claude",
            "steps": [{"id": 1, "description": "run"}],
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
                    "configured_limit": budget,
                    "current_limit": budget,
                    "multiplier": 2,
                    "exhaustion_count": 0,
                    "last_decision": "",
                    "source": "configured",
                    "created_at": "2026-08-10T00:00:00Z",
                },
                "agentbc.session": {
                    "version": 1,
                    "executor": "claude",
                    "retain": False,
                    "session_id": self.session_id,
                    "session_state": "pending",
                    "project_mode": "ephemeral",
                    "project_path": str(self.claude_project),
                    "run_ids": [],
                },
            },
        }

    def _start(
        self,
        packet: dict,
        completed: subprocess.CompletedProcess,
        *,
        output_format: str = "text",
    ):
        executor = ClaudeExecutor(
            command=sys.executable,
            transport="direct",
            output_format=output_format,
        )
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_close_run_lease"),
            mock.patch(
                "agent_bridge_connect.executors.claude.subprocess.run",
                return_value=completed,
            ),
        ):
            started = executor.start(packet)
        self.assertTrue(started.ok)
        return executor.poll(started.run_id)

    def test_structured_json_output_routes_to_input_required(self) -> None:
        structured = json.dumps(
            {
                "type": "error",
                "subtype": "error_max_budget_usd",
                "message": "Exceeded USD budget ($2.5)",
            }
        )
        completed = subprocess.CompletedProcess([], 1, stdout=structured, stderr="")
        poll = self._start(self._packet(), completed, output_format="json")
        self.assertEqual(poll.status, "input_required")
        self.assertEqual(poll.result["failure"]["kind"], "resource_limit_exhausted")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(poll.result["resource_exhaustion"]["limit"], 2.5)

    def test_text_stderr_budget_exhaustion_routes_to_input_required(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            1,
            stdout="ordinary text",
            stderr="Error: Exceeded USD budget ($2.5)\n",
        )
        poll = self._start(self._packet(), completed)
        self.assertEqual(poll.status, "input_required")
        self.assertEqual(poll.result["failure"]["kind"], "resource_limit_exhausted")
        self.assertEqual(
            poll.result["resource_exhaustion"]["source"],
            "text_exceeded_usd_budget",
        )

    def test_text_exit_zero_is_not_misclassified(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout="The assistant mentioned: Exceeded USD budget is possible\n",
            stderr="",
        )
        poll = self._start(self._packet(), completed)
        self.assertEqual(poll.status, "failed")
        self.assertEqual(poll.result["failure"]["kind"], "completion_marker_missing")
        self.assertIsNone(poll.result["resource_exhaustion"])


class HermesExhaustionRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.root = root
        self.packet = {
            "task_id": "P4HM-001",
            "title": "phase four Hermes",
            "steps": [{"id": 1, "description": "run"}],
            "workspace": {"root": str(root), "project_root": str(root)},
            "task_board": {"root": str(root / "record")},
            "extensions": {
                "agentbc.resources": {
                    "version": 1,
                    "executor": "hermes",
                    "resource": "max_turns",
                    "configured_limit": 60,
                    "current_limit": 60,
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
                    "session_id": "",
                    "session_state": "pending",
                    "project_mode": "none",
                    "project_path": "",
                    "run_ids": [],
                },
            },
        }

    def _terminal(self, output: str, *, returncode: int = 0) -> object:
        validation = extract_callback_validation_from_output(output, self.packet, "run-1")
        return _route_hermes_terminal(
            validation,
            returncode,
            stderr="",
            failure=None,
            iteration=_iteration_budget_diagnostics(output, ""),
            task_packet=self.packet,
        )

    def test_four_anchored_signals_route_to_input_required(self) -> None:
        signals = [
            "turn_exit_reason=max_iterations_reached(60/60)",
            "turn_exit_reason: budget_exhausted",
            "⚠️  Iteration budget exhausted (60/60) — asking model to summarise",
            "⚠️  Reached maximum iterations (60). Requesting summary...",
        ]
        for signal in signals:
            with self.subTest(signal=signal):
                terminal = self._terminal(signal)
                self.assertEqual(terminal.status, "input_required")
                self.assertEqual(
                    terminal.failure["kind"],
                    "resource_limit_exhausted",
                )
                self.assertIsNone(terminal.callback)
                self.assertTrue(terminal.resource_exhaustion["detected"])

    def test_budget_exhausted_without_numeric_limit_uses_frozen_snapshot(self) -> None:
        terminal = self._terminal("turn_exit_reason: budget_exhausted")
        self.assertEqual(terminal.status, "input_required")
        self.assertIsNone(terminal.resource_exhaustion["limit"])
        self.assertIsNone(terminal.resource_exhaustion["limit_matches_snapshot"])

    def test_pseudo_signal_is_not_exhaustion(self) -> None:
        for pseudo in (
            "Iteration budget was almost exhausted (60/60)",
            "max iterations were reached for a friend",
            "The agent reached its maximum iterations limit discussion",
        ):
            with self.subTest(pseudo=pseudo):
                terminal = self._terminal(pseudo)
                self.assertEqual(terminal.status, "failed")
                self.assertEqual(
                    terminal.failure["kind"],
                    "completion_marker_missing",
                )
                self.assertIsNone(terminal.resource_exhaustion)

    def test_receipt_limit_mismatch_fails_closed(self) -> None:
        output = "turn_exit_reason=max_iterations_reached(30/30)"
        validation = extract_callback_validation_from_output(output, self.packet, "run-1")
        terminal = _route_hermes_terminal(
            validation,
            0,
            stderr="",
            failure=None,
            iteration=_iteration_budget_diagnostics(output, ""),
            task_packet=self.packet,
        )
        self.assertEqual(terminal.status, "needs_recovery")
        self.assertEqual(
            terminal.failure["kind"],
            "resource_exhaustion_receipt_mismatch",
        )
        self.assertIs(terminal.resource_exhaustion["limit_matches_snapshot"], False)

    def test_direct_and_runner_are_isomorphic(self) -> None:
        output = "⚠️  Iteration budget exhausted (60/60) — asking model to summarise\nno marker"

        def run_direct():
            executor = HermesExecutor(command=sys.executable, transport="direct")
            completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
            with (
                mock.patch.object(executor, "_start_run_lease"),
                mock.patch.object(executor, "_heartbeat_run"),
                mock.patch.object(executor, "_close_run_lease"),
                mock.patch(
                    "agent_bridge_connect.executors.hermes.subprocess.run",
                    return_value=completed,
                ),
            ):
                started = executor.start(self.packet)
                self.assertTrue(started.ok)
                return executor.poll(started.run_id)

        def run_runner():
            executor = HermesExecutor(command=sys.executable, transport="runner")
            remote = {
                "status": "completed",
                "stdout": output,
                "stderr": "",
                "returncode": 0,
                "cwd": str(self.root),
                "output_truncated": False,
            }
            with (
                mock.patch.object(executor, "_start_run_lease"),
                mock.patch.object(executor, "_heartbeat_run"),
                mock.patch.object(executor, "_close_run_lease"),
                mock.patch.object(
                    executor._runner_client,
                    "health",
                    return_value={"executors": ["hermes"]},
                ),
                mock.patch.object(
                    executor._runner_client,
                    "submit",
                    return_value={"run_id": "hermes-runner-1", "pid": 9999},
                ),
                mock.patch.object(
                    executor._runner_client,
                    "status",
                    return_value=remote,
                ),
            ):
                started = executor.start(self.packet)
                self.assertTrue(started.ok)
                return executor.poll(started.run_id)

        direct = run_direct()
        runner = run_runner()
        self.assertEqual(direct.status, runner.status)
        self.assertEqual(direct.result["failure"], runner.result["failure"])
        self.assertEqual(
            direct.result["resource_exhaustion"],
            runner.result["resource_exhaustion"],
        )


class CoreResourceBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "executors": {"hermes": {"max_turns": 60}},
            },
        )

    def _hermes_task(self, *, steps: list[dict] | None = None) -> object:
        return self.service.create_task(
            "phase four Core resource block",
            "hermes",
            steps or [{"id": 1, "description": "first"}, {"id": 2, "description": "second"}],
            customer_dir=True,
            customer_path=self.project,
        )

    @staticmethod
    def _receipt(executor: str, session_id: str, *, resumed: bool) -> dict:
        return {
            "version": 1,
            "executor": executor,
            "session_id": session_id,
            "resumed": resumed,
            "persistence": "persistent",
            "source": {
                "claude": "preallocated",
                "hermes": "stderr_receipt",
                "codex": "jsonl_thread_started",
            }[executor],
        }

    def _exhaustion(self, *, limit: int = 60) -> dict:
        return {
            "detected": True,
            "executor": "hermes",
            "resource": "max_turns",
            "used": 60,
            "limit": limit,
            "source": "max_iterations_reached",
            "limit_matches_snapshot": True,
        }

    def _started_run(self, task_id: str) -> str:
        self.service.start_task_run(task_id, "hermes")
        self.service.record_executor_run_started(task_id, "hermes-run-1")
        return "hermes-run-1"

    def test_block_task_for_resource_creates_approve_deny_wait(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        session_id = task.extensions[SESSION_EXTENSION_KEY]["session_id"]
        receipt = self._receipt("hermes", session_id or "20260810_000000_a1b2c3", resumed=False)

        result = self.service.block_task_for_resource(
            task.id,
            run_id,
            self._exhaustion(),
            execution_session=receipt,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "input_required")
        current = self.service.get_task(task.id)
        self.assertEqual(current.status, "input_required")
        # done steps preserved, first incomplete blocked
        self.assertEqual(current.steps[0]["status"], "blocked")
        self.assertEqual(current.steps[1]["status"], "pending")
        resources = current.extensions["agentbc.resources"]
        self.assertEqual(resources["exhaustion_count"], 1)
        request = current.extensions["agentbc.input"]
        self.assertEqual(request["kind"], "resource_limit")
        self.assertEqual(request["response_protocol"], "approve_deny")
        self.assertEqual(request["type"], "choice")
        self.assertEqual(request["resource"], "max_turns")
        self.assertEqual(request["executor"], "hermes")
        self.assertEqual(request["current_limit"], 60)
        self.assertEqual(request["next_limit"], 120)
        self.assertEqual(request["status"], "waiting")
        self.assertEqual(request["blocked_step_id"], 1)
        session = current.extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_state"], "input_required")
        self.assertIn(run_id, session["run_ids"])
        lease = load_lease(task.id, self.board)
        self.assertEqual(lease.state, RunLeaseState.SUSPENDED)
        events = self.service.store.read_events(task.id)
        self.assertEqual(events[-1]["event_type"], "task.resource_limit_blocked")

    def test_block_accepts_budget_exhausted_without_reported_limit(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        exhaustion = self._exhaustion()
        exhaustion.update(
            {
                "used": None,
                "limit": None,
                "source": "budget_exhausted",
                "limit_matches_snapshot": None,
            }
        )
        result = self.service.block_task_for_resource(task.id, run_id, exhaustion)
        self.assertTrue(result["ok"])
        request = self.service.get_task(task.id).extensions["agentbc.input"]
        self.assertEqual(request["current_limit"], 60)
        self.assertEqual(request["next_limit"], 120)

    def test_block_preserves_prior_done_steps(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        task_model = self.service.get_task(task.id)
        task_model.steps[0]["status"] = "done"
        self.service.store.write_task(task.id, task_model.to_dict())

        result = self.service.block_task_for_resource(
            task.id,
            run_id,
            self._exhaustion(),
        )
        self.assertTrue(result["ok"])
        current = self.service.get_task(task.id)
        self.assertEqual(current.steps[0]["status"], "done")
        self.assertEqual(current.steps[1]["status"], "blocked")
        self.assertEqual(result["blocked_step_id"], 2)

    def test_fail_closed_when_no_incomplete_step(self) -> None:
        task = self._hermes_task(steps=[{"id": 1, "description": "only"}])
        run_id = self._started_run(task.id)
        task_model = self.service.get_task(task.id)
        task_model.steps[0]["status"] = "done"
        self.service.store.write_task(task.id, task_model.to_dict())

        result = self.service.block_task_for_resource(
            task.id,
            run_id,
            self._exhaustion(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "needs_recovery")
        self.assertEqual(result["code"], "resource_block_no_step")
        self.assertEqual(self.service.get_task(task.id).status, "needs_recovery")

    def test_fail_closed_on_damaged_receipt(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        damaged = self._exhaustion()
        damaged["detected"] = False
        result = self.service.block_task_for_resource(task.id, run_id, damaged)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "resource_block_invalid_receipt")
        self.assertEqual(self.service.get_task(task.id).status, "needs_recovery")

    def test_fail_closed_on_executor_mismatch(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        mismatch = self._exhaustion()
        mismatch["executor"] = "claude"
        result = self.service.block_task_for_resource(task.id, run_id, mismatch)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "resource_block_executor_mismatch")
        self.assertEqual(self.service.get_task(task.id).status, "needs_recovery")

    def test_fail_closed_ignores_invalid_session_receipt_without_raising(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        # All steps done means no blockable step; the invalid receipt must not
        # turn the fail-closed transition into an unhandled exception.
        task_model = self.service.get_task(task.id)
        task_model.steps[0]["status"] = "done"
        task_model.steps[1]["status"] = "done"
        self.service.store.write_task(task.id, task_model.to_dict())
        broken_receipt = {"version": 99, "executor": "hermes"}
        result = self.service.block_task_for_resource(
            task.id,
            run_id,
            self._exhaustion(),
            execution_session=broken_receipt,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "needs_recovery")
        self.assertEqual(self.service.get_task(task.id).status, "needs_recovery")

    def test_fail_closed_on_run_not_in_session(self) -> None:
        task = self._hermes_task()
        self.service.start_task_run(task.id, "hermes")
        result = self.service.block_task_for_resource(
            task.id,
            "not-a-recorded-run",
            self._exhaustion(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "resource_block_run_mismatch")

    def test_fail_closed_on_stale_chain_head(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        # A newer iteration makes this task a non-head member.
        child = self.service.create_task(
            "child iteration",
            "hermes",
            [{"id": 1, "description": "next"}],
            customer_dir=True,
            customer_path=self.project,
            lineage={
                "parent_task_id": task.id,
                "base_task_id": task.id,
                "chain_root_task_id": task.id,
                "iteration_index": 1,
                "task_code": task.workspace["task_code"],
                "task_date": task.workspace["task_date"],
                "chain_task_id": "002",
            },
        )
        self.assertNotEqual(child.id, task.id)
        result = self.service.block_task_for_resource(
            task.id,
            run_id,
            self._exhaustion(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "resource_block_stale_chain")
        self.assertEqual(self.service.get_task(task.id).status, "needs_recovery")

    def test_resource_input_remains_respondable_through_existing_branches(self) -> None:
        task = self._hermes_task()
        run_id = self._started_run(task.id)
        self.service.block_task_for_resource(task.id, run_id, self._exhaustion())
        request = self.service.get_task(task.id).extensions["agentbc.input"]
        response = self.service.respond_to_input(
            task.id,
            request["input_id"],
            response_type="approve",
            message="",
        )
        self.assertTrue(response["dispatch_required"])
        resumed = self.service.get_task(task.id)
        self.assertEqual(resumed.status, "running")
        self.assertEqual(resumed.steps[0]["status"], "pending")


class WorkerResourceBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "executors": {"hermes": {"max_turns": 60}},
            },
        )

    def _task(self):
        return self.service.create_task(
            "worker resource block",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=self.project,
        )

    def test_worker_blocks_task_for_resource_exhaustion(self) -> None:
        from agent_bridge_connect.cli import command_worker_run

        task = self._task()
        resources = task.extensions["agentbc.resources"]
        limit = resources["current_limit"]
        receipt = {
            "version": 1,
            "executor": "hermes",
            "session_id": "20260810_010203_a1b2c3",
            "resumed": False,
            "persistence": "persistent",
            "source": "stderr_receipt",
        }
        fake_executor = mock.Mock()
        fake_executor.probe.return_value = ProbeResult(ok=True, message="ok")
        fake_executor.start.return_value = StartResult(ok=True, run_id="hermes-run-1")
        fake_executor.poll.return_value = PollResult(
            status="input_required",
            progress={"steps_total": 1},
            result={
                "resource_exhaustion": {
                    "detected": True,
                    "executor": "hermes",
                    "resource": "max_turns",
                    "used": limit,
                    "limit": limit,
                    "source": "max_iterations_reached",
                    "limit_matches_snapshot": True,
                },
                "execution_session": receipt,
                "returncode": 0,
                "summary": "iteration budget exhausted",
            },
        )
        args = mock.Mock(
            root=str(self.board),
            executor="hermes",
            task_id=task.id,
            detach=False,
            monitor=False,
            config=None,
            interval=0.1,
            once=True,
            runner_authorize=True,
        )
        with (
            mock.patch(
                "agent_bridge_connect.cli.get_executor",
                return_value=fake_executor,
            ),
            mock.patch(
                "agent_bridge_connect.cli._notify_input_required",
                return_value={"ok": True},
            ) as notify,
        ):
            code = command_worker_run(args)

        self.assertEqual(code, 0)
        notify.assert_called_once()
        current = self.service.get_task(task.id)
        self.assertEqual(current.status, "input_required")
        request = current.extensions["agentbc.input"]
        self.assertEqual(request["kind"], "resource_limit")
        self.assertEqual(request["response_protocol"], "approve_deny")
        self.assertEqual(request["current_limit"], limit)
        self.assertEqual(request["next_limit"], limit * 2)
        self.assertEqual(
            current.extensions["agentbc.resources"]["exhaustion_count"],
            1,
        )
        self.assertEqual(
            current.extensions[SESSION_EXTENSION_KEY]["session_state"],
            "input_required",
        )

    def test_worker_fails_closed_when_resource_exhaustion_lacks_session_receipt(self) -> None:
        from agent_bridge_connect.cli import command_worker_run

        task = self._task()
        resources = task.extensions["agentbc.resources"]
        limit = resources["current_limit"]
        fake_executor = mock.Mock()
        fake_executor.probe.return_value = ProbeResult(ok=True, message="ok")
        fake_executor.start.return_value = StartResult(ok=True, run_id="hermes-run-1")
        fake_executor.poll.return_value = PollResult(
            status="input_required",
            progress={"steps_total": 1},
            result={
                "resource_exhaustion": {
                    "detected": True,
                    "executor": "hermes",
                    "resource": "max_turns",
                    "used": limit,
                    "limit": limit,
                    "source": "max_iterations_reached",
                    "limit_matches_snapshot": True,
                },
                "returncode": 0,
                "summary": "iteration budget exhausted",
            },
        )
        args = mock.Mock(
            root=str(self.board),
            executor="hermes",
            task_id=task.id,
            detach=False,
            monitor=False,
            config=None,
            interval=0.1,
            once=True,
            runner_authorize=True,
        )
        with mock.patch(
            "agent_bridge_connect.cli.get_executor",
            return_value=fake_executor,
        ):
            code = command_worker_run(args)

        self.assertEqual(code, 1)
        current = self.service.get_task(task.id)
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.errors[-1]["code"], "executor_session_receipt_invalid")
        self.assertNotIn("agentbc.input", current.extensions)

    def test_worker_fails_closed_when_receipt_mismatch_is_detected(self) -> None:
        from agent_bridge_connect.cli import command_worker_run

        task = self._task()
        fake_executor = mock.Mock()
        fake_executor.probe.return_value = ProbeResult(ok=True, message="ok")
        fake_executor.start.return_value = StartResult(ok=True, run_id="hermes-run-1")
        fake_executor.poll.return_value = PollResult(
            status="needs_recovery",
            progress={"steps_total": 1},
            result={
                "failure": {
                    "kind": "resource_exhaustion_receipt_mismatch",
                    "layer": "flow_contract",
                    "message": "limit mismatch",
                    "retryable": False,
                },
                "execution_session": {
                    "version": 1,
                    "executor": "hermes",
                    "session_id": "20260810_010203_a1b2c3",
                    "resumed": False,
                    "persistence": "persistent",
                    "source": "stderr_receipt",
                },
                "returncode": 0,
                "summary": "mismatch",
            },
        )
        args = mock.Mock(
            root=str(self.board),
            executor="hermes",
            task_id=task.id,
            detach=False,
            monitor=False,
            config=None,
            interval=0.1,
            once=True,
            runner_authorize=True,
        )
        with mock.patch(
            "agent_bridge_connect.cli.get_executor",
            return_value=fake_executor,
        ):
            code = command_worker_run(args)

        self.assertEqual(code, 1)
        current = self.service.get_task(task.id)
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(
            current.errors[-1]["code"],
            "resource_exhaustion_receipt_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
