"""PERM-104-002-R3 fail-closed and lifecycle regressions.

These tests exercise the narrow contracts added for the Claude SDK native
approval path and for Runner-owned contained-worker cleanup.  The full live
SDK session matrix remains in ``test_perm104_002_production_wiring.py``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.control import (
    ApprovalControlPlane,
    ControlPlaneError,
    SessionRecoveryRequired,
)
from agent_bridge_connect.execution_contract import (
    CallbackValidation,
    route_executor_terminal,
)
from agent_bridge_connect.executors.claude import _build_prompt
from agent_bridge_connect.executors.codex import _build_prompt as _build_codex_prompt
from agent_bridge_connect.notifications import notify_terminal
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.record_management import (
    MAX_TASK_RECORD_BYTES,
    task_record_size,
)
from agent_bridge_connect.runner import RunnerState
from agent_bridge_connect.service import TaskService


class NativeApprovalFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_native_prompt_has_no_compatibility_full_contradiction(self) -> None:
        prompt = _build_prompt(
            {
                "task_id": "RAXT-001",
                "steps": [{"id": 1, "description": "run the native canary"}],
                "workspace": {
                    "root": str(self.root),
                    "project_root": str(self.root),
                    "artifact_root": str(self.root),
                },
                "task_board": {"root": str(self.root / "record")},
                "runner_authorization_required": True,
            }
        )

        self.assertIn("only the native can_use_tool event can block", prompt)
        self.assertNotIn("all blockers request full", prompt)
        self.assertNotIn("requested_permission full", prompt)

    def test_model_input_required_is_recovery_for_native_authority(self) -> None:
        validation = CallbackValidation(
            marker_seen=True,
            valid=True,
            callback={
                "version": 1,
                "task_id": "RAXT-001",
                "final_state": "input_required",
                "summary": "model asks for full",
                "input": {
                    "type": "permission",
                    "requested_permission": "full",
                    "reason": "model text is not native evidence",
                },
                "step_results": [{"id": 1, "status": "blocked"}],
            },
        )

        routed = route_executor_terminal(
            validation,
            0,
            executor_name="claude",
            native_approval_authoritative=True,
        )

        self.assertEqual(routed.status, "needs_recovery")
        self.assertIsNone(routed.callback)
        self.assertEqual(
            (routed.failure or {}).get("kind"),
            "native_permission_callback_ignored",
        )

    def test_codex_native_prompt_has_no_compatibility_full_contradiction(self) -> None:
        prompt = _build_codex_prompt(
            {
                "task_id": "CMF2-001",
                "steps": [{"id": 1, "description": "repair the worker"}],
                "workspace": {
                    "root": str(self.root),
                    "project_root": str(self.root),
                    "artifact_root": str(self.root),
                },
                "task_board": {"root": str(self.root / "record")},
            },
            native_single_action=True,
        )

        self.assertIn("structured requestApproval event", prompt)
        self.assertNotIn("requested_permission full", prompt)

    def test_incomplete_native_event_cannot_create_pending_input(self) -> None:
        session_id = "22222222-2222-4222-8222-222222222222"
        plane = ApprovalControlPlane(
            self.root / "control",
            task_id="RAXT-001",
            executor_run_id="claude-raxt-run",
            session_id=session_id,
            executor="claude",
        )
        plane.record_session_started(
            {
                "version": 1,
                "executor": "claude",
                "session_id": session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            }
        )

        with self.assertRaises(ControlPlaneError) as raised:
            plane.request_approval(
                {
                    "jsonrpc": "2.0",
                    "id": "req-raxt-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": session_id,
                        "turnId": "turn-1",
                        "itemId": "toolu-raxt-1",
                    },
                }
            )

        self.assertEqual(raised.exception.code, "permission_block_evidence_unavailable")
        self.assertIsNone(plane.status().get("pending_request"))
        self.assertEqual(plane.status().get("status"), "needs_recovery")

    def test_receipt_must_precede_prompt_and_missing_receipt_has_no_wait(self) -> None:
        board = self.root / "record"
        board.mkdir()
        project = self.root / "project"
        project.mkdir()
        fake = self.root / "claude"
        fake.write_text(
            "#!/bin/sh\nprintf '2.1.233 (Claude Code)\\n'\n", encoding="utf-8"
        )
        fake.chmod(fake.stat().st_mode | 0o100)

        from agent_bridge_connect.executors.claude import ClaudeExecutor

        executor = ClaudeExecutor(command=str(fake), transport="direct")
        executor._version = "2.1.233 (Claude Code)"
        packet = {
            "task_id": "RAXT-001",
            "assignee": "claude",
            "steps": [{"id": 1, "description": "run the native canary"}],
            "workspace": {
                "root": str(project),
                "project_root": str(project),
                "artifact_root": str(project),
            },
            "task_board": {"root": str(board)},
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe")
            },
            "runner_authorization_required": True,
        }

        with (
            mock.patch.object(
                executor, "supports_permission_prompt_tool", return_value=True
            ),
            mock.patch(
                "agent_bridge_connect.permission_transport.assert_claude_sdk_environment",
                return_value={"supported": True},
            ),
            mock.patch(
                "agent_bridge_connect.executors.claude._build_prompt",
                wraps=_build_prompt,
            ) as prompt,
        ):
            result = executor.start_control(packet)

        self.assertTrue(result.ok)
        self.assertEqual(executor.poll(result.run_id).status, "needs_recovery")
        self.assertEqual(
            (executor.poll(result.run_id).result.get("failure") or {}).get("kind"),
            "session_receipt_missing",
        )
        prompt.assert_not_called()
        plane = executor._control_planes[result.run_id]
        self.assertIsNone(plane.status().get("pending_request"))

    def test_session_receipt_identity_mismatch_fails_closed(self) -> None:
        session_id = "33333333-3333-4333-8333-333333333333"
        plane = ApprovalControlPlane(
            self.root / "identity-control",
            task_id="RAXT-001",
            executor_run_id="claude-raxt-run",
            session_id=session_id,
            executor="claude",
        )
        with self.assertRaises(SessionRecoveryRequired) as raised:
            plane.record_session_started(
                {
                    "version": 1,
                    "executor": "claude",
                    "session_id": session_id,
                    "resumed": True,
                    "persistence": "persistent",
                    "source": "stderr_receipt",
                },
                expected_task_id="RAXT-001",
                expected_executor_run_id="claude-raxt-run",
                expected_session_id=session_id,
                expected_resumed=False,
                expected_source="preallocated",
            )

        self.assertEqual(raised.exception.code, "session_receipt_run_mismatch")
        self.assertEqual(plane.status().get("status"), "needs_recovery")


class ContainedWorkerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def _started_service(self) -> tuple[TaskService, str, str, str]:
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        task = service.create_task(
            "contained worker lifecycle",
            "claude",
            [{"id": 1, "description": "wait for approval"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        service.start_task_run(task.id, "claude")
        run_id = "claude-raxt-worker"
        service.record_executor_run_started(task.id, run_id)
        session_id = str(
            service.get_task(task.id).extensions["agentbc.session"]["session_id"]
        )
        return service, task.id, run_id, session_id

    def test_runner_reap_invalidates_native_input_and_clears_worker_refs(self) -> None:
        service, task_id, executor_run_id, session_id = self._started_service()
        worker_run_id = "runner-worker-raxt01"
        service.update_execution_metadata(
            task_id,
            {
                "worker_run_id": worker_run_id,
                "worker_pid": 99999,
                "dispatch_status": "accepted",
            },
        )
        service.block_task_for_approval(
            task_id,
            executor_run_id=executor_run_id,
            session_id=session_id,
            request_id="native-raxt-request",
            request_fingerprint="fp-" + "a" * 40,
            executor="claude",
            operation="Bash",
            tool_use_id="toolu-raxt-1",
            action_fingerprint="fp-" + "b" * 40,
            escalation_domain="executor_policy",
            profile_digest="sha256:" + "c" * 64,
            control_path="sdk_control_transport",
            native_event="claude_sdk_can_use_tool",
        )

        runner = RunnerState(
            self.root / "runner-state",
            [self.root],
            {"claude": Path(sys.executable)},
            {"claude": "test"},
        )
        runner._reconcile_worker_exit(
            {
                "run_id": worker_run_id,
                "task_id": task_id,
                "board_root": str(self.board),
                "executor": "claude",
                "returncode": 1,
            }
        )

        after = service.get_task(task_id)
        self.assertEqual(after.status, "needs_recovery")
        self.assertNotIn("agentbc.input", after.extensions)
        execution = after.extensions.get("agentbc.execution") or {}
        self.assertNotIn("worker_run_id", execution)
        self.assertNotIn("worker_pid", execution)

    def test_contained_worker_does_not_write_global_index_or_board_notifications(
        self,
    ) -> None:
        service, task_id, _run_id, _session_id = self._started_service()
        index_before = (self.board / "task_index.jsonl").read_bytes()
        markdown_before = (self.board / "TASK_INDEX.md").read_bytes()
        worker = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root),
                "permission_mode": "safe",
                "_runner_worker": True,
            },
        )
        worker.mark_task_needs_recovery(
            task_id,
            "worker_test_recovery",
            "test recovery is task-scoped",
        )
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(True, "test dialog"),
        ):
            notify_terminal(
                worker,
                task_id,
                "task.recovery_required",
                "warning",
                "test recovery notification",
            )

        self.assertEqual((self.board / "task_index.jsonl").read_bytes(), index_before)
        self.assertEqual((self.board / "TASK_INDEX.md").read_bytes(), markdown_before)
        self.assertFalse((self.board / "notifications.jsonl").exists())

    def test_contained_worker_start_does_not_refresh_global_indexes(self) -> None:
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        task = service.create_task(
            "contained worker claim",
            "codex",
            [{"id": 1, "description": "start without global index writes"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        index_before = (self.board / "task_index.jsonl").read_bytes()
        markdown_before = (self.board / "TASK_INDEX.md").read_bytes()
        worker = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root),
                "permission_mode": "safe",
                "_runner_worker": True,
            },
        )

        worker.start_task_run(task.id, "codex")

        self.assertEqual((self.board / "task_index.jsonl").read_bytes(), index_before)
        self.assertEqual((self.board / "TASK_INDEX.md").read_bytes(), markdown_before)


class TerminalRecordBudgetProjectionTests(unittest.TestCase):
    def test_high_volume_control_events_preserve_terminal_state_under_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            board = root / "record"
            project = root / "project"
            project.mkdir()
            service = TaskService(board, config={"workspace_root": str(root)})
            task = service.create_task(
                "bounded control diagnostics",
                "codex",
                [
                    {
                        "id": index,
                        "description": "preserve bounded causal evidence " * 4,
                    }
                    for index in range(1, 8)
                ],
                customer_dir=True,
                customer_path=project,
                permission_mode="safe",
            )
            service.start_task_run(task.id, "codex")
            control_events = [
                {
                    "created_at": f"2026-08-31T00:00:{index % 60:02d}Z",
                    "event_type": "approval_requested",
                    "executor": "codex",
                    "executor_run_id": "codex-budget-run",
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "request_id": str(index),
                    "request_fingerprint": "fp-" + ("a" * 40),
                    "scope": "single_action",
                    "operation": "command",
                }
                for index in range(7700)
            ]
            control_events.append(
                {
                    "created_at": "2026-08-31T01:00:00Z",
                    "event_type": "transport_failed",
                    "executor": "codex",
                    "executor_run_id": "codex-budget-run",
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "reason": "approval timed out",
                    "recovery": True,
                }
            )

            service.mark_task_failed(
                task.id,
                "codex_app_server_transport_failed",
                "Approval request timed out without a decision.",
                {
                    "executor": "codex",
                    "result": {
                        "events_seen": 7701,
                        "failure": {
                            "kind": "codex_app_server_transport_failed",
                            "layer": "executor",
                            "message": "Approval request timed out without a decision.",
                            "retryable": True,
                        },
                        "control_events": control_events,
                    },
                },
            )

            failed = service.get_task(task.id)
            self.assertEqual(failed.status, "failed")
            self.assertLessEqual(
                task_record_size(service.store.task_dir(task.id)),
                MAX_TASK_RECORD_BYTES,
            )
            projection = failed.errors[-1]["details"]["result"]["control_events"]
            self.assertEqual(projection["events_seen"], 7701)
            self.assertEqual(
                projection["selected"][0]["event_type"], "approval_requested"
            )
            self.assertEqual(
                projection["selected"][-1]["event_type"], "transport_failed"
            )


if __name__ == "__main__":
    unittest.main()
