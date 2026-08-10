"""Focused coverage for atomic Phase 4 resource approve/deny decisions."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.execution_policy import RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    create_lease,
    load_lease,
    save_lease,
)
from agent_bridge_connect.runner import RunnerError, RunnerState
from agent_bridge_connect.service import TaskService


class Phase4ResourceDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def _waiting_task(self, executor: str, limit: int | float):
        config = {
            "workspace_root": str(self.root / "workspace"),
            "executors": {
                executor: {
                    "max_budget_usd" if executor == "claude" else "max_turns": limit,
                }
            },
            "sessions": {"retain_executor_sessions": executor == "claude"},
        }
        service = TaskService(self.board, config=config)
        task = service.create_task(
            "resource decision",
            executor,
            [
                {"id": 1, "description": "done work"},
                {"id": 2, "description": "blocked work"},
            ],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="inherit",
        )
        raw = service.store.read_task(task.id)
        raw["status"] = "input_required"
        raw["steps"][0]["status"] = "done"
        raw["steps"][1]["status"] = "blocked"
        resources = raw["extensions"][RESOURCE_EXTENSION_KEY]
        resources["exhaustion_count"] = 1
        session = raw["extensions"][SESSION_EXTENSION_KEY]
        if not session["session_id"]:
            session["session_id"] = "20260810_010203_a1b2c3"
        session["session_state"] = "input_required"
        session["run_ids"] = [f"{executor}-run-1"]
        now = datetime.now(timezone.utc)
        input_id = f"input-{executor}-1"
        raw["extensions"]["agentbc.input"] = {
            "input_id": input_id,
            "executor_run_id": f"{executor}-run-1",
            "blocked_step_id": 2,
            "type": "choice",
            "kind": "resource_limit",
            "response_protocol": "approve_deny",
            "executor": executor,
            "resource": resources["resource"],
            "current_limit": limit,
            "next_limit": limit * 2,
            "summary": "Executor resource limit exhausted",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "deadline_at": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "status": "waiting",
        }
        raw["extensions"]["agentbc.execution"] = {
            "internal_status": "waiting",
            "lease_state": "suspended",
        }
        service.store.write_task(task.id, raw)
        lease = create_lease(task.id, executor, 0, str(self.project))
        lease.run_id = f"{executor}-run-1"
        lease.state = RunLeaseState.SUSPENDED
        save_lease(lease, self.board)
        return service, service.get_task(task.id), config

    def _next_wait(self, service: TaskService, task_id: str, input_id: str) -> None:
        raw = service.store.read_task(task_id)
        resources = raw["extensions"][RESOURCE_EXTENSION_KEY]
        raw["status"] = "input_required"
        raw["steps"][1]["status"] = "blocked"
        raw["extensions"][SESSION_EXTENSION_KEY]["session_state"] = "input_required"
        resources["exhaustion_count"] += 1
        previous = raw["extensions"]["agentbc.input"]
        raw["extensions"]["agentbc.input_history"] = [previous]
        now = datetime.now(timezone.utc)
        raw["extensions"]["agentbc.input"] = {
            **previous,
            "input_id": input_id,
            "current_limit": resources["current_limit"],
            "next_limit": resources["current_limit"] * 2,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "deadline_at": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "status": "waiting",
        }
        service.store.write_task(task_id, raw)

    def _runner(self, executor: str) -> RunnerState:
        state = RunnerState(
            self.root / "runner",
            [self.root],
            {executor: Path(sys.executable)},
        )
        state.known_boards = {self.board.resolve()}
        return state

    def _respond_request(self, task_id: str, input_id: str, response_type: str) -> dict:
        return {
            "task_id": task_id,
            "input_id": input_id,
            "response_type": response_type,
            "message": "",
            "board_root": str(self.board),
            "config_path": "",
        }

    def test_multiround_approve_doubles_only_task_snapshot(self) -> None:
        cases = (("claude", 10.0, (20.0, 40.0)), ("hermes", 60, (120, 240)))
        for executor, initial, expected in cases:
            with self.subTest(executor=executor):
                # Each subtest needs an isolated board because only one chain head is current.
                subroot = self.root / executor
                subroot.mkdir()
                self.board = subroot / "record"
                self.project = subroot / "project"
                self.project.mkdir()
                service, task, original_config = self._waiting_task(executor, initial)
                frozen_config = copy.deepcopy(original_config)
                first = task.extensions["agentbc.input"]
                result = service.respond_to_input(
                    task.id,
                    first["input_id"],
                    response_type="approve",
                )
                current = service.get_task(task.id)
                self.assertEqual(result["status"], "resuming")
                self.assertEqual(
                    current.extensions[RESOURCE_EXTENSION_KEY]["current_limit"], expected[0]
                )
                self.assertEqual(
                    current.extensions[RESOURCE_EXTENSION_KEY]["last_decision"], "increase"
                )
                self.assertEqual(current.steps[0]["status"], "done")
                self.assertEqual(current.steps[1]["status"], "pending")

                self._next_wait(service, task.id, f"input-{executor}-2")
                service.respond_to_input(
                    task.id,
                    f"input-{executor}-2",
                    response_type="approve",
                )
                current = service.get_task(task.id)
                self.assertEqual(
                    current.extensions[RESOURCE_EXTENSION_KEY]["current_limit"], expected[1]
                )
                self.assertEqual(service.config, frozen_config)

    def test_runner_restart_redispatches_same_task_session_and_project(self) -> None:
        service, task, _ = self._waiting_task("claude", 10.0)
        request = task.extensions["agentbc.input"]
        original_session = copy.deepcopy(task.extensions[SESSION_EXTENSION_KEY])
        original_project = task.workspace["project_root"]
        state = self._runner("claude")
        dispatched = {
            "run_id": "runner-worker-resume",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }
        with mock.patch.object(state, "dispatch_worker", return_value=dispatched) as launch:
            result = state.respond_and_dispatch(
                self._respond_request(task.id, request["input_id"], "approve")
            )

        restarted = TaskService(self.board)
        current = restarted.get_task(task.id)
        self.assertEqual(result["task_id"], task.id)
        self.assertTrue(result["same_task"])
        self.assertEqual(launch.call_args.args[:3], (task.id, "claude", str(self.board.resolve())))
        self.assertTrue(launch.call_args.kwargs["resuming"])
        self.assertEqual(current.extensions[SESSION_EXTENSION_KEY], original_session)
        self.assertEqual(current.workspace["project_root"], original_project)

    def test_duplicate_and_stale_responses_never_redouble_or_redispatch(self) -> None:
        service, task, _ = self._waiting_task("hermes", 60)
        request = task.extensions["agentbc.input"]
        state = self._runner("hermes")
        with self.assertRaises(ABCError) as stale:
            service.respond_to_input(task.id, "input-old", response_type="approve")
        self.assertEqual(stale.exception.code, "stale_input")
        self.assertEqual(
            service.get_task(task.id).extensions[RESOURCE_EXTENSION_KEY]["current_limit"], 60
        )

        with mock.patch.object(
            state,
            "dispatch_worker",
            return_value={"run_id": "resume", "dispatch_status": "accepted"},
        ) as launch:
            state.respond_and_dispatch(
                self._respond_request(task.id, request["input_id"], "approve")
            )
            duplicate = state.respond_and_dispatch(
                self._respond_request(task.id, request["input_id"], "approve")
            )
        self.assertEqual(duplicate["status"], "already_answered")
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(
            service.get_task(task.id).extensions[RESOURCE_EXTENSION_KEY]["current_limit"], 120
        )

    def test_stale_resource_snapshot_fails_before_any_mutation(self) -> None:
        service, task, _ = self._waiting_task("claude", 10.0)
        raw = service.store.read_task(task.id)
        raw["extensions"]["agentbc.input"]["next_limit"] = 30.0
        service.store.write_task(task.id, raw)
        request = service.get_task(task.id).extensions["agentbc.input"]
        with self.assertRaises(ABCError) as raised:
            service.respond_to_input(task.id, request["input_id"], response_type="approve")
        self.assertEqual(raised.exception.code, "resource_decision_stale")
        current = service.get_task(task.id)
        self.assertEqual(current.extensions[RESOURCE_EXTENSION_KEY]["current_limit"], 10.0)
        self.assertEqual(current.extensions["agentbc.input"]["status"], "waiting")

    def test_expired_resource_input_enters_recovery_without_doubling(self) -> None:
        service, task, _ = self._waiting_task("hermes", 60)
        raw = service.store.read_task(task.id)
        raw["extensions"]["agentbc.input"]["deadline_at"] = "2026-01-01T00:00:00Z"
        service.store.write_task(task.id, raw)
        state = self._runner("hermes")
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(True, "shown"),
        ):
            expired = state.maintain_waiting_inputs(now="2026-01-02T00:00:00Z")
        current = service.get_task(task.id)
        self.assertEqual(expired, [{"task_id": task.id, "input_id": "input-hermes-1"}])
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.extensions[RESOURCE_EXTENSION_KEY]["current_limit"], 60)
        self.assertEqual(current.extensions["agentbc.input"]["status"], "expired")

    def test_resume_dispatch_failure_is_recoverable_and_not_reapplied(self) -> None:
        service, task, _ = self._waiting_task("claude", 10.0)
        request = task.extensions["agentbc.input"]
        state = self._runner("claude")
        with mock.patch.object(
            state,
            "dispatch_worker",
            side_effect=RunnerError("resume unavailable"),
        ) as launch, mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(True, "shown"),
        ):
            with self.assertRaisesRegex(RunnerError, "resume unavailable"):
                state.respond_and_dispatch(
                    self._respond_request(task.id, request["input_id"], "approve")
                )
            duplicate = state.respond_and_dispatch(
                self._respond_request(task.id, request["input_id"], "approve")
            )
        current = service.get_task(task.id)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(duplicate["status"], "already_answered")
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.errors[-1]["code"], "input_resume_dispatch_failed")
        self.assertEqual(current.extensions[RESOURCE_EXTENSION_KEY]["current_limit"], 20.0)

    def test_deny_fails_without_dispatch_and_writes_terminal_evidence(self) -> None:
        cases = (
            ("claude", 10.0, "budget_exhausted_user_terminated"),
            ("hermes", 60, "iteration_exhausted_user_terminated"),
        )
        for executor, limit, failure_kind in cases:
            with self.subTest(executor=executor):
                subroot = self.root / f"deny-{executor}"
                subroot.mkdir()
                self.board = subroot / "record"
                self.project = subroot / "project"
                self.project.mkdir()
                service, task, _ = self._waiting_task(executor, limit)
                request = task.extensions["agentbc.input"]
                state = self._runner(executor)
                with mock.patch.object(state, "dispatch_worker") as launch, mock.patch(
                    "agent_bridge_connect.notifications.DialogNotifier.send",
                    return_value=DeliveryResult(True, "shown"),
                ):
                    result = state.respond_and_dispatch(
                        self._respond_request(task.id, request["input_id"], "deny")
                    )
                    duplicate = state.respond_and_dispatch(
                        self._respond_request(task.id, request["input_id"], "deny")
                    )

                current = service.get_task(task.id)
                failure = current.errors[-1]["details"]["failure"]
                report = generate_report(task.id, self.board)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(duplicate["status"], "already_answered")
                self.assertFalse(result["dispatch_required"])
                self.assertFalse(launch.called)
                self.assertEqual(current.status, "failed")
                self.assertEqual(current.extensions[RESOURCE_EXTENSION_KEY]["current_limit"], limit)
                self.assertEqual(current.extensions[RESOURCE_EXTENSION_KEY]["last_decision"], "terminate")
                self.assertEqual(current.extensions["agentbc.input"]["status"], "answered")
                self.assertEqual(failure["kind"], failure_kind)
                self.assertFalse(failure["retryable"])
                self.assertEqual(report["failure_code"], failure_kind)
                self.assertEqual(load_lease(task.id, self.board).state, RunLeaseState.CLOSED)
                self.assertTrue(Path(current.workspace["report_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
