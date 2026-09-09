"""PERM-104-002 final deterministic three-executor regression matrix.

The fixtures model the durable Core boundary only.  They never invoke a real
executor prompt, approve a live request, or perform a destructive action.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.approval import compute_request_fingerprint
from agent_bridge_connect.claude_elevation import stable_input_digest
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.execution_policy import SESSION_RECEIPT_SOURCES
from agent_bridge_connect.notifications import notify_input_required
from agent_bridge_connect.permission_modes import (
    PERMISSION_EXTENSION_KEY,
    permission_flags,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService


class FinalPermissionMatrixTests(unittest.TestCase):
    EXECUTORS = ("codex", "claude", "hermes")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )

    def _new_task(self, executor: str, *, permission_mode: str = "safe"):
        return self.service.create_task(
            f"PERM-104-002 deterministic {executor}",
            executor,
            [{"id": 1, "description": "run the deterministic fixture"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode=permission_mode,
        )

    def _running_fixture(self, executor: str):
        task = self._new_task(executor)
        self.service.start_task_run(task.id, executor)
        run_id = f"{executor}-perm104-final-{task.id}"
        self.service.record_executor_run_started(task.id, run_id)
        before_receipt = self.service.get_task(task.id)
        session_id = str(
            before_receipt.extensions["agentbc.session"].get("session_id") or ""
        ) or f"{executor}-official-session-{task.id}"
        self.service.record_executor_session_started(
            task.id,
            run_id,
            {
                "version": 1,
                "executor": executor,
                "session_id": session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": SESSION_RECEIPT_SOURCES[executor],
            },
        )
        current = self.service.get_task(task.id)
        self.assertEqual(
            current.extensions["agentbc.session"].get("session_id"), session_id
        )
        return current, run_id, session_id

    def _elevation_kwargs(
        self, executor: str, run_id: str, session_id: str, *, request_id: str = ""
    ) -> dict:
        operation = "native_fixture"
        tool_input = {"fixture": "no-op", "executor": executor}
        request_id = request_id or f"{executor}-perm104-request"
        request_fingerprint = compute_request_fingerprint(
            executor=executor,
            session_id=session_id,
            tool_name=operation,
            tool_input=tool_input,
            extra={"request_id": request_id},
        )
        action_fingerprint = compute_request_fingerprint(
            executor=executor,
            session_id=session_id,
            tool_name=operation,
            tool_input=tool_input,
        )
        protocol = {
            "codex": "codex_app_server",
            "claude": "claude_agent_sdk",
            "hermes": "hermes_acp",
        }[executor]
        native_event = {
            "codex": "codex_app_server.requestApproval",
            "claude": "claude_sdk_can_use_tool",
            "hermes": "hermes_acp.session/request_permission",
        }[executor]
        return {
            "executor_run_id": run_id,
            "session_id": session_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "executor": executor,
            "operation": operation,
            "summary": "deterministic native permission fixture",
            "blocked_step_id": 1,
            "tool_name": operation,
            "tool_use_id": f"{executor}-tool-1",
            "input_fingerprint": stable_input_digest(tool_input),
            "action_fingerprint": action_fingerprint,
            "control_path": "deterministic_fixture",
            "native_event": native_event,
            "authority": {
                "executor": executor,
                "protocol": protocol,
                "protocol_version": 1,
                "method": "requestApproval",
            },
        }

    def _waiting_fixture(self, executor: str):
        task, run_id, session_id = self._running_fixture(executor)
        kwargs = self._elevation_kwargs(executor, run_id, session_id)
        blocked = self.service.block_task_for_elevation(task.id, **kwargs)
        self.assertEqual(blocked["status"], "input_required")
        return task, run_id, session_id, kwargs, blocked

    def test_full_handoff_and_retry_keep_native_full_for_all_executors(self) -> None:
        expected_flags = {
            "codex": ["--dangerously-bypass-approvals-and-sandbox"],
            "claude": ["--dangerously-skip-permissions"],
            "hermes": ["--yolo"],
        }
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor):
                source = self._new_task(executor, permission_mode="full")
                source.status = "completed"
                source.steps[0]["status"] = "done"
                self.service.store.write_task(source.id, source.to_dict())

                handed = self.service.handoff_task(
                    source.id, executor, "continue the verified full task"
                )
                permission = handed.extensions[PERMISSION_EXTENSION_KEY]
                self.assertEqual(permission["effective_mode"], "full")
                self.assertEqual(permission["selection_source"], "inherited_task")
                self.assertEqual(
                    resolve_effective_permission(
                        handed.to_dict(), executor, f"{executor}-handoff"
                    )["effective_mode"],
                    "full",
                )
                self.assertEqual(permission_flags(executor, "full"), expected_flags[executor])

                retried = self._new_task(executor, permission_mode="full")
                retried.status = "running"
                retried.steps[0]["status"] = "failed"
                self.service.store.write_task(retried.id, retried.to_dict())
                self.service.retry_step(retried.id, 1)
                after_retry = self.service.get_task(retried.id)
                self.assertEqual(
                    after_retry.extensions[PERMISSION_EXTENSION_KEY]["effective_mode"],
                    "full",
                )
                self.assertEqual(
                    resolve_effective_permission(
                        after_retry.to_dict(), executor, f"{executor}-retry"
                    )["effective_mode"],
                    "full",
                )

    def test_duplicate_and_out_of_order_native_events_reserve_one_input_and_dialog(self) -> None:
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor):
                task, _run_id, _session_id, kwargs, first = self._waiting_fixture(executor)
                duplicate = self.service.block_task_for_elevation(task.id, **kwargs)
                self.assertTrue(duplicate["idempotent"])
                self.assertEqual(duplicate["input_id"], first["input_id"])

                late_kwargs = dict(kwargs)
                late_kwargs["request_id"] = f"{executor}-late-request"
                with self.assertRaises(ABCError) as raised:
                    self.service.block_task_for_elevation(task.id, **late_kwargs)
                self.assertEqual(raised.exception.code, "approval_already_pending")

                with mock.patch(
                    "agent_bridge_connect.notifiers.dialog.DialogNotifier.send",
                    return_value=DeliveryResult(
                        True,
                        "deterministic dialog",
                        "dialog:fixture",
                        {"action": "dismissed"},
                    ),
                ) as dialog_send:
                    notify_input_required(self.service, task.id)
                    notify_input_required(self.service, task.id)

                self.assertEqual(dialog_send.call_count, 1)
                persisted = self.service.get_task(task.id)
                elevation = persisted.extensions["agentbc.permission_elevation"]
                self.assertEqual(elevation["cardinality"]["permission_requests"], 1)
                self.assertEqual(elevation["cardinality"]["notifications"], 1)
                events = self.service.store.read_events(task.id)
                self.assertEqual(
                    sum(
                        event.get("event_type")
                        == "task.permission_elevation_notification_reserved"
                        for event in events
                    ),
                    1,
                )

    def test_deny_and_timeout_start_no_continuation_for_all_executors(self) -> None:
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor, outcome="deny"):
                task, run_id, session_id, _kwargs, blocked = self._waiting_fixture(executor)
                denied = self.service.respond_to_input(
                    task.id,
                    blocked["input_id"],
                    response_type="deny",
                )
                self.assertFalse(denied["dispatch_required"])
                self.assertTrue(denied["permission_denied"])
                after = self.service.get_task(task.id)
                elevation = after.extensions["agentbc.permission_elevation"]
                self.assertEqual(after.status, "failed")
                self.assertEqual(elevation["state"]["status"], "denied")
                self.assertEqual(elevation["decision"]["type"], "deny")
                self.assertEqual(elevation["cardinality"]["human_decisions"], 1)
                self.assertEqual(elevation["cardinality"]["full_continuations"], 0)
                self.assertEqual(
                    after.extensions["agentbc.session"]["run_ids"], [run_id]
                )

    def test_approve_resumes_one_same_session_full_continuation_for_all_executors(self) -> None:
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor):
                task, run_id, session_id, _kwargs, blocked = self._waiting_fixture(executor)
                approved = self.service.respond_to_input(
                    task.id,
                    blocked["input_id"],
                    response_type="approve_full",
                )
                self.assertTrue(approved["dispatch_required"])
                self.assertTrue(approved["same_session"])

                continuation_run_id = f"{executor}-perm104-final-continuation-{task.id}"
                self.service.record_executor_run_started(task.id, continuation_run_id)
                activated = self.service.activate_task_elevation(
                    task.id,
                    executor_run_id=continuation_run_id,
                    session_id=session_id,
                )
                self.assertEqual(activated["elevation_state"], "active")

                after = self.service.get_task(task.id)
                elevation = after.extensions["agentbc.permission_elevation"]
                self.assertEqual(elevation["continuation"]["count"], 1)
                self.assertEqual(
                    elevation["continuation"]["session_id"], session_id
                )
                self.assertEqual(
                    resolve_effective_permission(
                        after.to_dict(), executor, continuation_run_id
                    )["effective_mode"],
                    "full",
                )
                self.assertEqual(
                    after.extensions["agentbc.session"]["run_ids"],
                    [run_id, continuation_run_id],
                )

                replay_activation = self.service.activate_task_elevation(
                    task.id,
                    executor_run_id=continuation_run_id,
                    session_id=session_id,
                )
                self.assertEqual(replay_activation["elevation_state"], "active")
                replay_answer = self.service.respond_to_input(
                    task.id,
                    blocked["input_id"],
                    response_type="approve_full",
                )
                self.assertEqual(replay_answer["status"], "already_answered")
                replayed = self.service.get_task(task.id)
                self.assertEqual(
                    replayed.extensions["agentbc.permission_elevation"][
                        "cardinality"
                    ]["full_continuations"],
                    1,
                )
                replay = self.service.respond_to_input(
                    task.id,
                    blocked["input_id"],
                    response_type="deny",
                )
                self.assertEqual(replay["status"], "already_answered")

            with self.subTest(executor=executor, outcome="timeout"):
                task, run_id, _session_id, _kwargs, blocked = self._waiting_fixture(executor)
                expired = self.service.expire_waiting_inputs(now="2099-01-01T00:00:00Z")
                self.assertEqual(
                    [item["task_id"] for item in expired],
                    [task.id],
                )
                self.assertEqual(self.service.expire_waiting_inputs(now="2099-01-01T00:00:00Z"), [])
                after = self.service.get_task(task.id)
                elevation = after.extensions["agentbc.permission_elevation"]
                self.assertEqual(after.status, "failed")
                self.assertEqual(elevation["state"]["status"], "denied")
                self.assertEqual(elevation["decision"]["source"], "timeout")
                self.assertEqual(elevation["cardinality"]["human_decisions"], 1)
                self.assertEqual(elevation["cardinality"]["full_continuations"], 0)
                self.assertEqual(
                    after.extensions["agentbc.session"]["run_ids"], [run_id]
                )


if __name__ == "__main__":
    unittest.main()
