"""Focused coverage for the resumable input-response lifecycle."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.cli import build_parser
from agent_bridge_connect.executors.codex import _build_prompt as build_codex_prompt
from agent_bridge_connect.notifications import build_input_required_notification, notify_input_required
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report, generate_report_md, generate_task_brief
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    create_lease,
    load_lease,
    save_lease,
)
from agent_bridge_connect.runner import RunnerError, RunnerState
from agent_bridge_connect.service import TaskService


class InputResponseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.board = self.base / "record"
        self.project = self.base / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.base / "workspace")},
        )
        self.task = self.service.create_task(
            "input lifecycle",
            "shell",
            [
                {"id": 1, "description": "completed work"},
                {"id": 2, "description": "blocked work"},
            ],
            customer_path=self.project,
        )
        self.service.start_task_run(self.task.id, "shell")
        lease = create_lease(self.task.id, "shell", 0, str(self.project))
        lease.run_id = "shell-first-run"
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)
        self._suspend()

    def _input_callback(self, **updates):
        callback = {
            "version": 1,
            "task_id": self.task.id,
            "final_state": "input_required",
            "summary": "Need approval; password=hidden-value",
            "executor_run_id": "shell-first-run",
            "input": {
                "type": "permission",
                "requested_permission": "Allow network; password=hidden-value",
            },
            "step_results": [
                {"id": 1, "status": "done"},
                {"id": 2, "status": "blocked"},
            ],
        }
        callback.update(updates)
        return callback

    def _suspend(self) -> None:
        self.assertTrue(
            self.service.finalize_task_from_executor_exit(
                self.task.id,
                executor_run_id="shell-first-run",
                callback=self._input_callback(),
            )
        )

    def _input(self) -> dict:
        return dict(self.service.get_task(self.task.id).extensions["agentbc.input"])

    def _runner_state(self) -> RunnerState:
        state = RunnerState(
            self.base / "runner",
            [self.base],
            {"shell": Path(sys.executable)},
        )
        # RunnerState includes the user's default board for production discovery.
        # Unit tests must replace that set so fake clocks can never mutate live tasks.
        state.known_boards = {self.board.resolve()}
        return state

    def test_suspension_has_request_without_final_callback_or_terminal_report(self) -> None:
        current = self.service.get_task(self.task.id)
        request = self._input()

        self.assertEqual(current.status, "input_required")
        self.assertNotIn("agentbc.final_callback", current.extensions)
        self.assertFalse(Path(current.workspace["report_file"]).exists())
        self.assertEqual(request["executor_run_id"], "shell-first-run")
        self.assertEqual(request["blocked_step_id"], 2)
        self.assertEqual(request["type"], "permission")
        self.assertEqual(request["status"], "waiting")
        self.assertNotIn("hidden-value", json.dumps(request))
        deadline = datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(request["created_at"].replace("Z", "+00:00"))
        self.assertEqual(deadline - created, timedelta(hours=24))
        self.assertEqual(load_lease(self.task.id, self.board).state, RunLeaseState.SUSPENDED)
        self.assertEqual(current.steps[0]["status"], "done")
        self.assertEqual(current.steps[1]["status"], "blocked")

    def test_notification_and_dashboard_are_nonterminal_and_actionable(self) -> None:
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(True, "shown"),
        ):
            notify_input_required(self.service, self.task.id)

        notification = json.loads(
            (self.board / "notifications.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        request = self._input()
        exact_command = (
            f"agentbc task respond {self.task.id} --input {request['input_id']} --approve"
        )
        self.assertEqual(notification["event_type"], "task.input_required")
        self.assertEqual(notification["respond_command"], f"{exact_command} (or --deny)")
        self.assertEqual(notification["deadline_at"], request["deadline_at"])
        self.assertNotIn("Respond:", notification["message"])
        self.assertNotIn("Deadline:", notification["message"])
        self.assertIn("Why this is blocked:", notification["message"])
        health = self.service.task_summary(self.task.id)["health"]
        self.assertEqual((health["state"], health["color"]), ("waiting_for_input", "yellow"))
        self.assertTrue(self.service.task_summary(self.task.id)["is_active"])

    def test_choice_options_are_preserved_and_exposed_to_the_dialog(self) -> None:
        choice_task = self.service.create_task(
            "choice input",
            "shell",
            [{"id": 1, "description": "choose"}],
            customer_path=self.project,
        )
        callback = {
            "version": 1,
            "task_id": choice_task.id,
            "final_state": "input_required",
            "summary": "Choose an option",
            "input": {
                "type": "choice",
                "reason": "The implementation cannot continue until the output format is selected.",
                "options": [
                    {"label": "Option A", "description": "Write a compact text result."},
                    {"label": "Option B", "description": "Write a detailed JSON result."},
                ],
            },
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        self.assertTrue(self.service.finalize_task_from_agent(choice_task.id, callback))

        request = self.service.get_task(choice_task.id).extensions["agentbc.input"]
        notification = build_input_required_notification(self.service, choice_task.id)
        self.assertEqual(request["type"], "choice")
        self.assertEqual(
            request["reason"],
            "The implementation cannot continue until the output format is selected.",
        )
        self.assertEqual(request["options"], ["Option A", "Option B"])
        self.assertEqual(
            request["option_descriptions"],
            ["Write a compact text result.", "Write a detailed JSON result."],
        )
        self.assertEqual(notification["input_options"], ["Option A", "Option B"])
        self.assertEqual(
            notification["input_option_descriptions"],
            ["Write a compact text result.", "Write a detailed JSON result."],
        )
        self.assertIn("Why this is blocked:", notification["message"])
        self.assertIn("• Option A — Write a compact text result.", notification["message"])
        self.assertIn("• Option B — Write a detailed JSON result.", notification["message"])
        self.assertNotIn("Deadline:", notification["message"])
        self.assertNotIn("Respond:", notification["message"])
        self.assertIn("--message", notification["respond_command"])
        report_md = generate_report_md(choice_task.id, self.board)
        self.assertIn("- Decision reason: The implementation cannot continue", report_md)
        self.assertIn("- Option `Option A`: Write a compact text result.", report_md)

    def test_input_dialog_action_responds_and_resumes_same_task(self) -> None:
        request = self._input()
        response = {
            "task_id": self.task.id,
            "input_id": request["input_id"],
            "status": "running",
            "same_task": True,
        }
        responder = mock.Mock(return_value=response)
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(
                True,
                "shown",
                details={"action": "approve"},
            ),
        ):
            result = notify_input_required(
                self.service,
                self.task.id,
                responder=responder,
            )

        responder.assert_called_once_with(request["input_id"], "approve", "")
        self.assertEqual(result["response"]["status"], "running")
        events = self.service.store.read_events(self.task.id)
        self.assertEqual(events[-2]["dialog_action"], "approve")
        event = events[-1]
        self.assertEqual(event["event_type"], "task.input_dialog_response")
        self.assertEqual(event["response_status"], "running")
        self.assertTrue(event["same_task"])

    def test_dismissed_input_dialog_keeps_task_waiting(self) -> None:
        responder = mock.Mock()
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(
                True,
                "shown",
                details={"action": "dismissed"},
            ),
        ):
            notify_input_required(self.service, self.task.id, responder=responder)

        responder.assert_not_called()
        self.assertEqual(self.service.get_task(self.task.id).status, "input_required")
        self.assertEqual(load_lease(self.task.id, self.board).state, RunLeaseState.SUSPENDED)

    def test_worker_notification_wires_dialog_response_to_runner(self) -> None:
        from agent_bridge_connect.cli import _notify_input_required

        request = self._input()
        with (
            mock.patch(
                "agent_bridge_connect.notifications.DialogNotifier.send",
                return_value=DeliveryResult(
                    True,
                    "shown",
                    details={"action": "approve"},
                ),
            ),
            mock.patch(
                "agent_bridge_connect.runner.RunnerClient.respond_task",
                return_value={
                    "task_id": self.task.id,
                    "input_id": request["input_id"],
                    "status": "running",
                    "same_task": True,
                },
            ) as respond,
        ):
            result = _notify_input_required(
                self.service,
                self.task.id,
                config_path=self.base / "config.toml",
                interval_s=3.0,
            )

        respond.assert_called_once_with(
            self.task.id,
            request["input_id"],
            "approve",
            "",
            self.service.board_root,
            self.base / "config.toml",
            3.0,
        )
        self.assertEqual(result["response"]["status"], "running")

    def test_legacy_input_required_without_id_never_emits_blank_response_command(self) -> None:
        task = self.service.get_task(self.task.id)
        task.extensions.pop("agentbc.input", None)
        self.service.store.write_task(task.id, task.to_dict())

        report = generate_report(task.id, self.board)
        brief = generate_task_brief(task.id, self.board)
        actions = "\n".join(brief["available_actions"])

        self.assertIn("Legacy input_required record has no response ID", report["recovery_recommendation"])
        self.assertNotIn("--input  ", report["recovery_recommendation"])
        self.assertNotIn("task respond", actions)
        self.assertIn(f"agentbc task close {task.id}", actions)

    def test_notification_rejects_waiting_request_without_response_id(self) -> None:
        task = self.service.get_task(self.task.id)
        task.extensions["agentbc.input"] = {"status": "waiting"}
        self.service.store.write_task(task.id, task.to_dict())

        with self.assertRaisesRegex(ValueError, "no response ID"):
            build_input_required_notification(self.service, task.id)

    def test_response_resets_only_blocked_steps_and_preserves_completed_evidence(self) -> None:
        request = self._input()
        result = self.service.respond_to_input(
            self.task.id,
            request["input_id"],
            response_type="message",
            message="Proceed with token sk-abcdefghijklmnop",
        )
        current = self.service.get_task(self.task.id)

        self.assertEqual(result["status"], "resuming")
        self.assertEqual(current.status, "running")
        self.assertEqual(current.extensions["agentbc.execution"]["internal_status"], "resuming")
        self.assertEqual(current.steps[0]["status"], "done")
        self.assertEqual(current.steps[1]["status"], "pending")
        self.assertNotIn(
            "abcdefghijklmnop",
            current.extensions["agentbc.input"]["response"]["summary"],
        )
        self.assertEqual(self.service.task_summary(self.task.id)["health_color"], "green")

        packet = {
            "task_id": current.id,
            "title": current.title,
            "steps": current.steps,
            "workspace": current.workspace,
            "task_board": {"root": str(self.board)},
            "extensions": current.extensions,
        }
        prompt = build_codex_prompt(packet)
        self.assertIn("Prior input request", prompt)
        self.assertIn("User response", prompt)
        self.assertIn("1. completed work [status: done]", prompt)
        self.assertIn("2. blocked work [status: pending]", prompt)

    def test_wrong_stale_duplicate_ids_and_denial(self) -> None:
        request = self._input()
        with self.assertRaisesRegex(ABCError, "not current") as raised:
            self.service.respond_to_input(
                self.task.id,
                "input-wrong",
                response_type="approve",
            )
        self.assertEqual(raised.exception.code, "stale_input")

        denial = self.service.respond_to_input(
            self.task.id,
            request["input_id"],
            response_type="deny",
        )
        self.assertEqual(denial["status"], "resuming")
        duplicate = self.service.respond_to_input(
            self.task.id,
            request["input_id"],
            response_type="deny",
        )
        self.assertEqual(duplicate["status"], "already_answered")
        self.assertFalse(duplicate["dispatch_required"])
        self.assertEqual(
            self.service.get_task(self.task.id).extensions["agentbc.input"]["response"]["type"],
            "deny",
        )

    def test_runner_restart_persists_request_and_redispatches_same_task(self) -> None:
        restarted = TaskService(self.board)
        request = dict(restarted.get_task(self.task.id).extensions["agentbc.input"])
        state = self._runner_state()
        dispatched = {
            "run_id": "runner-worker-resume",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }
        with mock.patch.object(state, "dispatch_worker", return_value=dispatched) as launch:
            result = state.respond_and_dispatch(
                {
                    "task_id": self.task.id,
                    "input_id": request["input_id"],
                    "response_type": "approve",
                    "message": "",
                    "board_root": str(self.board),
                    "config_path": "",
                }
            )

        self.assertEqual(result["task_id"], self.task.id)
        self.assertTrue(result["same_task"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(launch.call_args.args[0], self.task.id)
        self.assertTrue(launch.call_args.kwargs["resuming"])
        self.assertEqual(len(restarted.list_tasks()), 1)

    def test_active_executor_blocks_response_before_mutation(self) -> None:
        request = self._input()
        lease = load_lease(self.task.id, self.board)
        lease.state = RunLeaseState.ACTIVE
        save_lease(lease, self.board)
        with self.assertRaises(ABCError) as raised:
            self.service.respond_to_input(
                self.task.id,
                request["input_id"],
                response_type="approve",
            )
        self.assertEqual(raised.exception.code, "executor_active")
        self.assertEqual(self._input()["status"], "waiting")

    def test_fake_clock_expiry_occurs_in_runner_maintenance_not_report_read(self) -> None:
        request = self._input()
        after_deadline = (
            datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")

        report = generate_report(self.task.id, self.board)
        self.assertEqual(report["status"], "input_required")
        self.assertEqual(self.service.get_task(self.task.id).status, "input_required")

        state = self._runner_state()
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(True, "shown"),
        ):
            expired = state.maintain_waiting_inputs(now=after_deadline)

        current = self.service.get_task(self.task.id)
        self.assertEqual(expired[0]["task_id"], self.task.id)
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.extensions["agentbc.input"]["status"], "expired")
        self.assertEqual(current.errors[-1]["code"], "input_deadline_expired")
        self.assertEqual(load_lease(self.task.id, self.board).state, RunLeaseState.CLOSED)

    def test_waiting_time_is_excluded_from_execution_duration(self) -> None:
        task = self.service.get_task(self.task.id)
        request = dict(task.extensions["agentbc.input"])
        request.update(
            {
                "created_at": "2026-01-01T00:00:00Z",
                "deadline_at": "2026-01-02T00:00:00Z",
                "responded_at": "2026-01-01T02:00:00Z",
                "status": "answered",
                "response": {"type": "approve", "summary": "approve"},
            }
        )
        task.created_at = "2025-12-31T23:00:00Z"
        task.status = "running"
        task.steps[1]["status"] = "pending"
        task.extensions["agentbc.input"] = request
        self.service.store.write_task(task.id, task.to_dict())
        lease = load_lease(task.id, self.board)
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)

        self.service.finalize_task_from_agent(
            task.id,
            {
                "version": 1,
                "task_id": task.id,
                "final_state": "completed",
                "summary": "completed after response",
                "finished_at": "2026-01-01T03:00:00Z",
                "step_results": [
                    {"id": 1, "status": "done"},
                    {"id": 2, "status": "done"},
                ],
            },
        )
        report = generate_report(task.id, self.board)
        self.assertEqual(report["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(report["waiting_duration_s"], 2 * 60 * 60)
        self.assertEqual(report["execution_duration_s"], 2 * 60 * 60)

    def test_resume_launch_failure_becomes_recovery_with_precise_evidence(self) -> None:
        request = self._input()
        state = self._runner_state()
        with (
            mock.patch.object(state, "dispatch_worker", side_effect=RunnerError("launch context missing")),
            mock.patch(
                "agent_bridge_connect.notifications.DialogNotifier.send",
                return_value=DeliveryResult(True, "shown"),
            ),
            self.assertRaisesRegex(RunnerError, "launch context missing"),
        ):
            state.respond_and_dispatch(
                {
                    "task_id": self.task.id,
                    "input_id": request["input_id"],
                    "response_type": "approve",
                    "message": "",
                    "board_root": str(self.board),
                    "config_path": "",
                }
            )
        current = self.service.get_task(self.task.id)
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.errors[-1]["code"], "input_resume_dispatch_failed")
        self.assertEqual(current.errors[-1]["details"]["phase"], "resume_dispatch")

    def test_handoff_is_rejected_while_input_is_pending(self) -> None:
        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(self.task.id, "codex", "continue")
        self.assertEqual(raised.exception.code, "input_pending")
        self.assertEqual(len(self.service.list_tasks()), 1)

    def test_final_completion_after_response_writes_only_real_callback(self) -> None:
        request = self._input()
        self.service.respond_to_input(
            self.task.id,
            request["input_id"],
            response_type="approve",
        )
        lease = load_lease(self.task.id, self.board)
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)
        self.service.finalize_task_from_agent(
            self.task.id,
            {
                "version": 1,
                "task_id": self.task.id,
                "final_state": "completed",
                "summary": "real completion",
                "step_results": [
                    {"id": 1, "status": "done"},
                    {"id": 2, "status": "done"},
                ],
            },
        )
        current = self.service.get_task(self.task.id)
        final_events = [
            event
            for event in self.service.store.read_events(self.task.id)
            if event.get("event_type") == "task.finalized"
        ]
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.extensions["agentbc.final_callback"]["summary"], "real completion")
        self.assertEqual(len(final_events), 1)
        self.assertTrue(Path(current.workspace["report_file"]).exists())

    def test_cli_response_forms_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        approve = parser.parse_args(
            ["task", "respond", self.task.id, "--input", self._input()["input_id"], "--approve"]
        )
        self.assertTrue(approve.approve)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "task",
                    "respond",
                    self.task.id,
                    "--input",
                    self._input()["input_id"],
                    "--approve",
                    "--deny",
                ]
            )


if __name__ == "__main__":
    unittest.main()
