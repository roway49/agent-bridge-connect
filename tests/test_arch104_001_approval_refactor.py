"""ARCH-104-001 Slice A import and lifecycle characterization tests."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect import approval as approval_schema
from agent_bridge_connect import approval_lifecycle, approval_protocol, control
from agent_bridge_connect.approval import APPROVAL_EXTENSION_KEY
from agent_bridge_connect.control import (
    ApprovalControlPlane,
    ControlPlaneError,
    approval_response_payload_v2,
    claude_offered_choices,
    codex_offered_choices,
    hermes_offered_choices,
    normalize_approval_request,
)
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.service import TaskService


class ApprovalProtocolRefactorTests(unittest.TestCase):
    SESSION_ID = "11111111-1111-4111-8111-111111111111"
    RUN_ID = "arch-104-run-1"

    def test_facade_reexports_and_signatures_remain_stable(self) -> None:
        from agent_bridge_connect import control_transport

        self.assertIs(control.ApprovalRequest, approval_protocol.ApprovalRequest)
        self.assertIs(control.ControlEvent, approval_protocol.ControlEvent)
        self.assertIs(control.StdioJsonRpcTransport, control_transport.StdioJsonRpcTransport)
        self.assertIs(control.CodexAppServerTransport, control_transport.CodexAppServerTransport)
        self.assertEqual(
            str(inspect.signature(control.normalize_approval_request)),
            "(message: 'dict[str, Any]', *, task_id: 'str', executor_run_id: 'str', session_id: 'str', executor: 'str' = 'codex') -> 'ApprovalRequest'",
        )
        self.assertEqual(
            str(inspect.signature(TaskService.respond_to_input)),
            "(self, task_id: 'str', input_id: 'str', *, response_type: 'str', message: 'str' = '') -> 'dict[str, Any]'",
        )
        block_parameters = inspect.signature(TaskService.block_task_for_approval).parameters
        self.assertEqual(
            list(block_parameters)[:6],
            [
                "self",
                "task_id",
                "executor_run_id",
                "session_id",
                "request_id",
                "request_fingerprint",
            ],
        )
        self.assertEqual(block_parameters["executor_run_id"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_codex_claude_and_hermes_requests_normalize_executor_neutrally(self) -> None:
        codex_choices = list(codex_offered_choices("command", session_decisions_supported=True))
        codex_request = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": "codex-request-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": self.SESSION_ID,
                    "turnId": "turn-1",
                    "itemId": "item-1",
                },
                "approval_version": 2,
                "offered_choices": codex_choices,
                "authority": {
                    "executor": "codex",
                    "protocol": "codex_app_server",
                    "protocol_version": 2,
                    "method": "item/commandExecution/requestApproval",
                },
            },
            task_id="ARCH-104-001",
            executor_run_id=self.RUN_ID,
            session_id=self.SESSION_ID,
            executor="codex",
        )
        self.assertEqual(codex_request.approval_version, 2)
        self.assertEqual([choice["native_option_id"] for choice in codex_request.offered_choices], [
            "accept",
            "acceptForSession",
            "decline",
        ])
        self.assertTrue(all(choice["handle"].startswith("opt-") for choice in codex_request.offered_choices))

        claude_request = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": "claude-request-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": self.SESSION_ID,
                    "turnId": "turn-2",
                    "itemId": "toolu-1",
                },
                "approval_version": 3,
                "scope": "task_elevation",
                "elevation_mode": "full",
                "native_event": "claude_sdk_can_use_tool",
                "path_plan_digest": "sha256:" + "a" * 64,
                "containment_profile_digest": "sha256:" + "b" * 64,
                "authority": {
                    "executor": "claude",
                    "protocol": "claude_agent_sdk",
                    "protocol_version": 1,
                    "method": "sdk.can_use_tool",
                },
                "_agentbc": {
                    "request_fingerprint": "fp-" + "c" * 40,
                    "tool_use_id": "toolu-1",
                    "tool_name": "Read",
                    "action_fingerprint": "fp-" + "d" * 40,
                },
            },
            task_id="ARCH-104-001",
            executor_run_id=self.RUN_ID,
            session_id=self.SESSION_ID,
            executor="claude",
        )
        self.assertEqual(claude_request.approval_version, 3)
        self.assertEqual(claude_request.scope, "task_elevation")
        self.assertEqual(claude_request.elevation_mode, "full")
        self.assertEqual(claude_request.native_event, "claude_sdk_can_use_tool")

        hermes_choices = list(
            hermes_offered_choices(
                [
                    {"native_option_id": "allow_once", "kind": "once", "label": "Allow"},
                    {"native_option_id": "reject", "kind": "deny", "label": "Reject", "selectable": True},
                ]
            )
        )
        hermes_request = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": "hermes-request-1",
                "method": "item/permissions/requestApproval",
                "params": {
                    "threadId": self.SESSION_ID,
                    "turnId": "turn-3",
                    "itemId": "permission-1",
                    "permissions": {"filesystem": "workspace"},
                },
                "approval_version": 2,
                "offered_choices": hermes_choices,
                "authority": {
                    "executor": "hermes",
                    "protocol": "hermes_acp",
                    "protocol_version": 1,
                    "method": "session/request_permission",
                },
            },
            task_id="ARCH-104-001",
            executor_run_id=self.RUN_ID,
            session_id=self.SESSION_ID,
            executor="hermes",
        )
        self.assertEqual(hermes_request.approval_version, 2)
        self.assertEqual(hermes_request.offered_choices[1]["native_option_id"], "reject")
        self.assertEqual(
            approval_response_payload_v2(
                hermes_request,
                choice_kind="deny",
                native_option_id="reject",
                decision="decline",
            ),
            {"outcome": {"optionId": "reject"}},
        )

    def test_offered_choices_keep_native_contracts_and_session_gates(self) -> None:
        self.assertEqual(
            [choice["native_option_id"] for choice in codex_offered_choices("command", session_decisions_supported=False)],
            ["accept", "decline"],
        )
        self.assertEqual(
            [choice["native_option_id"] for choice in claude_offered_choices(session_bundle_supported=False)],
            ["deny", "allow_once"],
        )
        self.assertEqual(
            [choice["native_option_id"] for choice in claude_offered_choices(session_bundle_supported=True)],
            ["deny", "allow_once", "allow_session"],
        )
        self.assertEqual(
            hermes_offered_choices(
                [{"native_option_id": "native-2", "kind": "other", "label": "Other", "selectable": False}]
            )[0],
            {
                "native_option_id": "native-2",
                "kind": "other",
                "label": "Other",
                "selectable": False,
            },
        )

    def test_control_rejects_out_of_order_duplicate_replay_and_cross_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def make_plane(name: str) -> ApprovalControlPlane:
                plane = ApprovalControlPlane(
                    root / name,
                    task_id="ARCH-104-001",
                    executor_run_id=self.RUN_ID,
                    session_id=self.SESSION_ID,
                    executor="codex",
                )
                plane.record_session_started(
                    {
                        "version": 1,
                        "executor": "codex",
                        "session_id": self.SESSION_ID,
                        "resumed": False,
                        "persistence": "persistent",
                        "source": "jsonl_thread_started",
                    }
                )
                return plane

            out_of_order_plane = make_plane("out-of-order")
            with self.assertRaises(ControlPlaneError) as out_of_order:
                out_of_order_plane.respond_approval(
                    "ARCH-104-001",
                    self.RUN_ID,
                    self.SESSION_ID,
                    "request-before-event",
                    "accept",
            )
            self.assertEqual(out_of_order.exception.code, "approval_identity_mismatch")

            request = {
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": self.SESSION_ID, "turnId": "turn-1", "itemId": "item-1"},
            }
            duplicate_plane = make_plane("duplicate")
            duplicate_plane.request_approval(request)
            with self.assertRaises(ControlPlaneError) as duplicate:
                duplicate_plane.request_approval(request)
            self.assertEqual(duplicate.exception.code, "approval_request_duplicate")

            concurrent_plane = make_plane("concurrent")
            concurrent_plane.request_approval(request)
            with self.assertRaises(ControlPlaneError) as out_of_order_request:
                concurrent_plane.request_approval({**request, "id": "request-2"})
            self.assertEqual(out_of_order_request.exception.code, "approval_concurrent_request")

            wrong_session_plane = make_plane("wrong-session")
            with self.assertRaises(ControlPlaneError) as wrong_session:
                wrong_session_plane.request_approval(
                    {
                        **request,
                        "id": "request-other-session",
                        "params": {**request["params"], "threadId": "other-session"},
                    }
                )
            self.assertEqual(wrong_session.exception.code, "approval_session_mismatch")

            plane = make_plane("success")
            plane.request_approval(request)
            plane.respond_approval(
                "ARCH-104-001",
                self.RUN_ID,
                self.SESSION_ID,
                "request-1",
                "accept",
            )
            with self.assertRaises(ControlPlaneError) as replay:
                plane.respond_approval(
                    "ARCH-104-001",
                    self.RUN_ID,
                    self.SESSION_ID,
                    "request-1",
                    "accept",
                )
            self.assertEqual(replay.exception.code, "approval_request_expired")


class ApprovalLifecycleRefactorTests(unittest.TestCase):
    RUN_ID = "arch-104-lifecycle-run"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.board = root / "record"
        self.project = root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(root), "permission_mode": "safe"},
        )

    def _started_task(self, request_id: str) -> tuple[str, str]:
        task = self.service.create_task(
            "ARCH-104 lifecycle fixture",
            "claude",
            [{"id": 1, "description": "one approval-bound step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        self.service.start_task_run(task.id, "claude")
        self.service.record_executor_run_started(task.id, self.RUN_ID)
        current = self.service.get_task(task.id)
        session = dict(current.extensions["agentbc.session"])
        session["session_state"] = "active"
        session["run_ids"] = [self.RUN_ID]
        current.extensions["agentbc.session"] = session
        self.service.store.write_task(task.id, current.to_dict())
        self.service.block_task_for_approval(
            task.id,
            executor_run_id=self.RUN_ID,
            session_id=str(session["session_id"]),
            request_id=request_id,
            request_fingerprint="fp-" + request_id.replace("-", "")[:40].ljust(40, "a"),
            executor="claude",
            operation="Bash",
        )
        return task.id, str(session["session_id"])

    def test_approve_deny_timeout_and_same_session_binding(self) -> None:
        task_id, session_id = self._started_task("approval-approve")
        request = self.service.get_task(task_id).extensions["agentbc.input"]
        approved = self.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="approve",
        )
        self.assertEqual(approved["approval_source"], "user")
        self.assertTrue(approved["same_session"])
        self.assertEqual(
            self.service.get_task(task_id).extensions[APPROVAL_EXTENSION_KEY]["decision"]["type"],
            "approve",
        )
        self.assertEqual(
            self.service.get_task(task_id).extensions["agentbc.session"]["session_id"],
            session_id,
        )

        denied_id, _ = self._started_task("approval-deny")
        denied_request = self.service.get_task(denied_id).extensions["agentbc.input"]
        denied = self.service.respond_to_input(
            denied_id,
            denied_request["input_id"],
            response_type="deny",
        )
        self.assertTrue(denied["dispatch_required"])
        self.assertEqual(denied["approval_decision"], "deny")

        timeout_id, _ = self._started_task("approval-timeout")
        timeout_task = self.service.get_task(timeout_id)
        timeout_request = timeout_task.extensions["agentbc.input"]
        timeout_at = timeout_task.extensions[APPROVAL_EXTENSION_KEY]["created_at"]
        timeout_request["deadline_at"] = timeout_at
        timeout_task.extensions["agentbc.input"] = timeout_request
        self.service.store.write_task(timeout_id, timeout_task.to_dict())
        self.assertEqual(
            [item["task_id"] for item in self.service.expire_waiting_inputs(now=timeout_at)],
            [timeout_id],
        )
        timed_out = self.service.get_task(timeout_id)
        self.assertEqual(timed_out.status, "needs_recovery")
        self.assertEqual(
            timed_out.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"],
            "timeout",
        )

    def test_full_permission_has_zero_approval_and_one_lifecycle_delegate(self) -> None:
        full_task = self.service.create_task(
            "ARCH-104 full fixture",
            "codex",
            [{"id": 1, "description": "full mode work"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="full",
        )
        self.assertEqual(
            full_task.extensions[PERMISSION_EXTENSION_KEY]["effective_mode"],
            "full",
        )
        self.assertNotIn(APPROVAL_EXTENSION_KEY, full_task.extensions)

        service_source = Path(__import__("agent_bridge_connect.service", fromlist=["__file__"]).__file__).read_text()
        lifecycle_source = Path(approval_lifecycle.__file__).read_text()
        schema_source = Path(approval_schema.__file__).read_text()
        self.assertEqual(service_source.count("self._respond_to_permission_input("), 1)
        self.assertIn("def record_approval_decision(", schema_source)
        self.assertNotIn("def record_approval_decision(", lifecycle_source)
        self.assertNotIn("def record_approval_decision(", service_source)
        self.assertNotIn("from .service", lifecycle_source)
        self.assertNotIn("from .control", lifecycle_source)


if __name__ == "__main__":
    unittest.main()
