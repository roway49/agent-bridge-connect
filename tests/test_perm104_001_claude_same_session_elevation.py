"""PERM-104-001 Claude default -> same-session bypass elevation.

These tests exercise the official SDK result objects and the installed SDK's
own control-response serializer.  The live path is deliberately separate from
the historical v2 choice/session-rule compatibility tests.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.claude_elevation import (
    CLAUDE_ELEVATION_ACTIVE,
    CLAUDE_ELEVATION_BLOCKED,
    CLAUDE_ELEVATION_DENIED,
    CLAUDE_ELEVATION_RESPONSE_READY,
    stable_input_digest,
)
from agent_bridge_connect.claude_sdk_transport import (
    SDK_SESSION_MODE_UPDATE,
    ClaudeSDKControlTransport,
    ClaudeSDKTransportError,
)
from agent_bridge_connect.control import ApprovalControlPlane
from agent_bridge_connect.notifications import (
    build_input_required_notification,
    notify_input_required,
)
from agent_bridge_connect.permission_runtime import host_profile_digest, path_plan_digest
from agent_bridge_connect.permission_transport import (
    claude_control_path_capability,
    claude_sdk_protocol_capability,
    select_claude_control_path,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    create_lease,
    load_lease,
    save_lease,
)
from agent_bridge_connect.service import TaskService


PATH_DIGEST = "sha256:" + "1" * 64
PROFILE_DIGEST = "sha256:" + "2" * 64


class _CaptureTransport:
    def __init__(self) -> None:
        self.writes: list[str] = []

    async def write(self, value: str) -> None:
        self.writes.append(value)


class _FakeClaudeCliStream:
    """A tiny official-shaped stream that honors the returned setMode update."""

    def __init__(self, callback, event_sink) -> None:
        self.callback = callback
        self.event_sink = event_sink
        self.callback_count = 0
        self.permission_mode = "default"
        self.completed: list[str] = []

    async def _permission_callback(self, tool_name: str, input_data: dict, tool_id: str):
        self.callback_count += 1
        return await self.callback(
            tool_name,
            input_data,
            types.SimpleNamespace(tool_use_id=tool_id, suggestions=[]),
        )

    async def run(self) -> object:
        first = await self._permission_callback(
            "Read", {"file_path": "/tmp/read.txt"}, "stream-read-1"
        )
        if getattr(first, "behavior", "") != "allow":
            return first
        for update in getattr(first, "updated_permissions", None) or []:
            if update.to_dict() == SDK_SESSION_MODE_UPDATE:
                self.permission_mode = "bypassPermissions"
        self.event_sink.capture_tool_event(
            event="PostToolUse",
            tool_use_id="stream-read-1",
            tool_name="Read",
        )
        for tool_name in ("Read", "Write", "Edit", "Bash"):
            # A real Claude CLI stops asking once the returned session update
            # takes effect.  These heterogeneous actions are therefore stream
            # completions, not synthetic second permission callbacks.
            if self.permission_mode != "bypassPermissions":
                await self._permission_callback(tool_name, {}, f"stream-{tool_name}")
                continue
            self.completed.append(tool_name)
        return first


class ClaudeSameSessionElevationTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.task_id = "RM7A-001"
        self.run_id = "claude-RM7A-run"
        self.session_id = str(uuid.uuid4())
        self.plane = ApprovalControlPlane(
            self.root / ".agentbc-control" / self.task_id,
            task_id=self.task_id,
            executor_run_id=self.run_id,
            session_id=self.session_id,
            executor="claude",
        )
        self.plane.record_session_started(
            {
                "version": 1,
                "executor": "claude",
                "session_id": self.session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            }
        )

    def _transport(self) -> ClaudeSDKControlTransport:
        transport = ClaudeSDKControlTransport(
            plane=self.plane,
            task_id=self.task_id,
            run_id=self.run_id,
            session_id=self.session_id,
            executor="claude",
            safe_to_full=True,
            path_plan_digest=PATH_DIGEST,
            containment_profile_digest=PROFILE_DIGEST,
            full_preflight={
                "ok": True,
                "status": "passed",
                "mode": "contained_full",
            },
        )
        self.addCleanup(transport.stop)
        return transport

    async def _wait_pending(self) -> dict:
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            pending = (self.plane.status() or {}).get("pending_request") or {}
            if pending.get("status") == "pending":
                return pending
            await asyncio.sleep(0.01)
        raise AssertionError("native Claude approval request did not become pending")

    async def _decide(
        self,
        transport: ClaudeSDKControlTransport,
        *,
        tool_name: str,
        input_data: dict,
        tool_use_id: str,
        decision: str,
    ) -> object:
        callback = asyncio.create_task(
            transport.can_use_tool(
                tool_name,
                input_data,
                types.SimpleNamespace(
                    tool_use_id=tool_use_id,
                    suggestions=[
                        {
                            "type": "addRules",
                            "behavior": "allow",
                            "rules": [{"tool_name": tool_name}],
                        }
                    ],
                ),
            )
        )
        pending = await self._wait_pending()
        await asyncio.to_thread(
            self.plane.respond_approval,
            self.task_id,
            self.run_id,
            self.session_id,
            str(pending["request_id"]),
            decision,
        )
        return await callback

    def test_approve_is_one_atomic_allow_with_exact_set_mode_and_original_input(self) -> None:
        transport = self._transport()
        original = {"file_path": "/tmp/blocked.txt", "content": "sensitive input"}

        async def scenario() -> tuple[object, dict, dict]:
            with mock.patch.object(
                transport,
                "validate_session_bundle",
                wraps=transport.validate_session_bundle,
            ) as validate_bundle:
                callback = asyncio.create_task(
                    transport.can_use_tool(
                        "Write",
                        original,
                        types.SimpleNamespace(
                            tool_use_id="tool-write-1",
                            suggestions=[{"type": "setMode", "mode": "bypassPermissions"}],
                        ),
                    )
                )
                pending = await self._wait_pending()
                self.assertTrue(pending["native_live_elevation"])
                self.assertEqual(pending["native_elevation_protocol"], "claude.can_use_tool.setMode")
                self.assertNotIn("offered_choices", pending)
                self.assertEqual(pending["session_id"], self.session_id)
                response = await asyncio.to_thread(
                    self.plane.respond_approval,
                    self.task_id,
                    self.run_id,
                    self.session_id,
                    str(pending["request_id"]),
                    "accept",
                )
                result = await callback
                validate_bundle.assert_not_called()
                return result, response, pending

        result, response, pending = asyncio.run(scenario())
        self.assertEqual(type(result).__name__, "PermissionResultAllow")
        self.assertEqual(result.behavior, "allow")
        self.assertEqual(result.updated_input, original)
        self.assertEqual(len(result.updated_permissions or []), 1)
        self.assertEqual(result.updated_permissions[0].to_dict(), SDK_SESSION_MODE_UPDATE)
        self.assertEqual(
            response["response_payload"],
            {
                "behavior": "allow",
                "updatedPermissions": [SDK_SESSION_MODE_UPDATE],
                "updatedInput": "original_blocked_input",
            },
        )
        receipt = transport.elevation_receipt
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["state"], CLAUDE_ELEVATION_RESPONSE_READY)
        self.assertEqual(receipt["session_id"], self.session_id)
        self.assertEqual(receipt["request_id"], pending["request_id"])
        self.assertEqual(receipt["tool_use_id"], "tool-write-1")
        self.assertEqual(receipt["input_fingerprint"], stable_input_digest(original))
        self.assertEqual(receipt["cardinality"]["permission_requests"], 1)
        self.assertEqual(receipt["cardinality"]["human_decisions"], 1)
        self.assertEqual(receipt["cardinality"]["set_mode_updates"], 1)
        self.assertEqual(len(receipt["transition_history"]), 2)
        self.assertNotIn("sensitive input", json.dumps(receipt))

        transport.capture_tool_event(
            event="PostToolUse",
            tool_use_id="tool-write-1",
            tool_name="Write",
            session_id=self.session_id,
        )
        receipt = transport.elevation_receipt
        self.assertEqual(receipt["state"], CLAUDE_ELEVATION_ACTIVE)
        self.assertEqual(len(receipt["transition_history"]), 3)

    def test_deny_is_native_deny_without_update_or_mode_change(self) -> None:
        transport = self._transport()
        result = asyncio.run(
            self._decide(
                transport,
                tool_name="Bash",
                input_data={"command": "echo blocked"},
                tool_use_id="tool-bash-deny",
                decision="decline",
            )
        )
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertEqual(result.behavior, "deny")
        self.assertFalse(hasattr(result, "updated_input"))
        self.assertFalse(hasattr(result, "updated_permissions"))
        self.assertEqual((self.plane.status() or {}).get("pending_request", {}).get("status"), "responded")
        receipt = transport.elevation_receipt
        self.assertEqual(receipt["state"], CLAUDE_ELEVATION_DENIED)
        self.assertEqual(receipt["cardinality"]["set_mode_updates"], 0)
        self.assertEqual(receipt["cardinality"]["human_decisions"], 1)

    def test_replayed_or_post_activation_callback_creates_no_second_request(self) -> None:
        transport = self._transport()
        request_calls = mock.Mock(wraps=self.plane.request_approval)
        with mock.patch.object(self.plane, "request_approval", request_calls):
            first = asyncio.run(
                self._decide(
                    transport,
                    tool_name="Read",
                    input_data={"file_path": "/tmp/a"},
                    tool_use_id="tool-read-1",
                    decision="accept",
                )
            )
            self.assertEqual(first.behavior, "allow")
            self.assertEqual(
                transport.elevation_receipt["state"],
                CLAUDE_ELEVATION_RESPONSE_READY,
            )
            second = asyncio.run(
                transport.can_use_tool(
                    "Edit",
                    {"file_path": "/tmp/a", "old_string": "a", "new_string": "b"},
                    types.SimpleNamespace(tool_use_id="tool-edit-1", suggestions=[]),
                )
            )
            replay = asyncio.run(
                transport.can_use_tool(
                    "Bash",
                    {"command": "echo replay"},
                    types.SimpleNamespace(tool_use_id="tool-bash-replay", suggestions=[]),
                )
            )
        self.assertEqual(second.behavior, "deny")
        self.assertEqual(replay.behavior, "deny")
        self.assertEqual(request_calls.call_count, 1)
        receipt = transport.elevation_receipt
        self.assertEqual(receipt["state"], CLAUDE_ELEVATION_BLOCKED)
        self.assertEqual(receipt["error_code"], "claude_full_mode_ineffective")
        self.assertEqual(receipt["cardinality"]["protocol_anomalies"], 1)

    def test_concurrent_callback_is_denied_without_a_second_dialog_request(self) -> None:
        transport = self._transport()

        async def scenario() -> tuple[object, object]:
            first = asyncio.create_task(
                transport.can_use_tool(
                    "Read",
                    {"file_path": "/tmp/a"},
                    types.SimpleNamespace(tool_use_id="tool-concurrent-1", suggestions=[]),
                )
            )
            await self._wait_pending()
            second = await transport.can_use_tool(
                "Bash",
                {"command": "echo second"},
                types.SimpleNamespace(tool_use_id="tool-concurrent-2", suggestions=[]),
            )
            pending = (self.plane.status() or {}).get("pending_request") or {}
            await asyncio.to_thread(
                self.plane.respond_approval,
                self.task_id,
                self.run_id,
                self.session_id,
                str(pending["request_id"]),
                "decline",
            )
            return second, await first

        second, first = asyncio.run(scenario())
        self.assertEqual(second.behavior, "deny")
        self.assertEqual(first.behavior, "deny")
        events = self.plane.events()
        self.assertEqual(sum(event["event_type"] == "approval_requested" for event in events), 1)

    def test_rejected_set_mode_and_transport_loss_fail_closed(self) -> None:
        transport = self._transport()
        with mock.patch.object(
            transport,
            "_set_mode_update",
            side_effect=ClaudeSDKTransportError(
                "claude_sdk_set_mode_rejected", "rejected setMode"
            ),
        ):
            callback = asyncio.run(
                self._rejected_approval(transport)
            )
        self.assertEqual(callback, "claude_sdk_set_mode_rejected")
        receipt = transport.elevation_receipt
        self.assertEqual(receipt["state"], CLAUDE_ELEVATION_BLOCKED)
        self.assertEqual(receipt["error_code"], "claude_sdk_set_mode_rejected")
        self.assertEqual(receipt["cardinality"]["set_mode_updates"], 0)

        loss_transport = self._transport()

        async def loss_scenario() -> str:
            callback_task = asyncio.create_task(
                loss_transport.can_use_tool(
                    "Read",
                    {"file_path": "/tmp/lost"},
                    types.SimpleNamespace(tool_use_id="tool-loss-1", suggestions=[]),
                )
            )
            await self._wait_pending()
            loss_transport.record_transport_death("test transport loss")
            with self.assertRaises(ClaudeSDKTransportError) as caught:
                await callback_task
            return caught.exception.code

        asyncio.run(loss_scenario())
        loss_receipt = loss_transport.elevation_receipt
        self.assertEqual(loss_receipt["state"], CLAUDE_ELEVATION_BLOCKED)
        self.assertEqual(loss_receipt["error_code"], "claude_sdk_transport_lost")

    async def _rejected_approval(self, transport: ClaudeSDKControlTransport) -> str:
        callback = asyncio.create_task(
            transport.can_use_tool(
                "Write",
                {"file_path": "/tmp/rejected", "content": "x"},
                types.SimpleNamespace(tool_use_id="tool-rejected-1", suggestions=[]),
            )
        )
        pending = await self._wait_pending()
        await asyncio.to_thread(
            self.plane.respond_approval,
            self.task_id,
            self.run_id,
            self.session_id,
            str(pending["request_id"]),
            "accept",
        )
        with self.assertRaises(ClaudeSDKTransportError) as caught:
            await callback
        return caught.exception.code

    def test_installed_sdk_serializes_exact_wire_and_deny_shape(self) -> None:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, PermissionUpdate
        from claude_agent_sdk._internal.query import Query

        original = {"file_path": "/tmp/wire", "content": "x"}

        async def allow_callback(tool_name, input_data, context):
            return PermissionResultAllow(
                updated_input=dict(input_data),
                updated_permissions=[
                    PermissionUpdate(
                        type="setMode",
                        mode="bypassPermissions",
                        destination="session",
                    )
                ],
            )

        allow_transport = _CaptureTransport()
        allow_query = Query(allow_transport, True, can_use_tool=allow_callback)
        asyncio.run(
            allow_query._handle_control_request(
                {
                    "request_id": "sdk-wire-allow",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Write",
                        "input": original,
                        "tool_use_id": "wire-tool-1",
                        "permission_suggestions": [],
                    },
                }
            )
        )
        allow_wire = json.loads(allow_transport.writes[0])
        self.assertEqual(
            allow_wire["response"]["response"],
            {
                "behavior": "allow",
                "updatedInput": original,
                "updatedPermissions": [SDK_SESSION_MODE_UPDATE],
            },
        )
        allow_query._message_send.close()
        allow_query.close_receive_stream()

        async def deny_callback(tool_name, input_data, context):
            return PermissionResultDeny(message="denied")

        deny_transport = _CaptureTransport()
        deny_query = Query(deny_transport, True, can_use_tool=deny_callback)
        asyncio.run(
            deny_query._handle_control_request(
                {
                    "request_id": "sdk-wire-deny",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Bash",
                        "input": {"command": "echo no"},
                        "tool_use_id": "wire-tool-2",
                        "permission_suggestions": [],
                    },
                }
            )
        )
        deny_wire = json.loads(deny_transport.writes[0])
        self.assertEqual(
            deny_wire["response"]["response"],
            {"behavior": "deny", "message": "denied"},
        )
        deny_query._message_send.close()
        deny_query.close_receive_stream()

    def test_fake_cli_stream_completes_heterogeneous_actions_after_one_callback(self) -> None:
        transport = self._transport()

        async def scenario() -> _FakeClaudeCliStream:
            stream = _FakeClaudeCliStream(transport.can_use_tool, transport)
            run_task = asyncio.create_task(stream.run())
            pending = await self._wait_pending()
            await asyncio.to_thread(
                self.plane.respond_approval,
                self.task_id,
                self.run_id,
                self.session_id,
                str(pending["request_id"]),
                "accept",
            )
            await run_task
            return stream

        stream = asyncio.run(scenario())
        self.assertEqual(stream.callback_count, 1)
        self.assertEqual(stream.permission_mode, "bypassPermissions")
        self.assertEqual(stream.completed, ["Read", "Write", "Edit", "Bash"])
        self.assertEqual(transport.session_id, self.session_id)
        self.assertEqual(transport.elevation_receipt["state"], CLAUDE_ELEVATION_ACTIVE)

    def test_protocol_admission_is_shape_based_and_rejects_unsupported_shape_once(self) -> None:
        old = claude_control_path_capability("0.0.1")
        new = claude_control_path_capability("999.999.999")
        self.assertEqual(old["control_paths"], new["control_paths"])
        self.assertTrue(old["version_is_diagnostic"])
        self.assertEqual(
            select_claude_control_path("0.0.1", None),
            select_claude_control_path("999.999.999", None),
        )
        capability = claude_sdk_protocol_capability()
        self.assertEqual(capability["set_mode_update"], SDK_SESSION_MODE_UPDATE)

        import claude_agent_sdk

        class UnsupportedPermissionUpdate:
            def __init__(self, type, rules=None, behavior=None, destination=None):
                self.type = type
                self.rules = rules
                self.behavior = behavior
                self.destination = destination

        with mock.patch.object(
            claude_agent_sdk, "PermissionUpdate", UnsupportedPermissionUpdate
        ):
            with self.assertRaises(ABCError) as caught:
                claude_sdk_protocol_capability()
        self.assertEqual(caught.exception.code, "permission_protocol_shape_unsupported")


class ClaudeSameSessionElevationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.board = root / "record"
        self.customer = root / "customer"
        self.customer.mkdir(parents=True)
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(root), "permission_mode": "safe"},
        )
        self.task = self.service.create_task(
            "live Claude elevation",
            "claude",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=self.customer,
            permission_mode="safe",
        )
        self.service.start_task_run(self.task.id, "claude")
        self.run_id = "claude-service-live-run"
        self.service.record_executor_run_started(self.task.id, self.run_id)
        task = self.service.get_task(self.task.id)
        self.session_id = str(task.extensions["agentbc.session"]["session_id"])
        lease = create_lease(self.task.id, "claude", 0, str(self.customer))
        lease.run_id = self.run_id
        save_lease(lease, self.board)

    def test_live_wait_reserves_one_dialog_and_answers_same_active_lease(self) -> None:
        task = self.service.get_task(self.task.id)
        plan_digest = path_plan_digest(task.workspace)
        profile_digest = host_profile_digest()
        request_fingerprint = "fp-" + "a" * 40
        action_fingerprint = "fp-" + "b" * 40
        result = self.service.block_task_for_approval(
            self.task.id,
            executor_run_id=self.run_id,
            session_id=self.session_id,
            request_id="approval-service-live",
            request_fingerprint=request_fingerprint,
            executor="claude",
            operation="Write",
            summary="Claude requested a file write",
            reason="Claude requested a file write",
            reason_detail="private input is not persisted",
            tool_name="Write",
            tool_use_id="service-tool-1",
            action_fingerprint=action_fingerprint,
            escalation_domain="executor_policy",
            profile_digest=profile_digest,
            control_path="sdk_control_transport",
            native_event="claude_sdk_can_use_tool",
            authority={
                "executor": "claude",
                "protocol": "claude_agent_sdk",
                "protocol_version": 1,
                "method": "sdk.can_use_tool",
                "update": dict(SDK_SESSION_MODE_UPDATE),
            },
            approval_version=3,
            elevation_mode="contained_full",
            path_plan_digest=plan_digest,
            containment_profile_digest=profile_digest,
            full_preflight={"ok": True, "status": "passed", "mode": "contained_full"},
            native_live_elevation=True,
        )
        self.assertTrue(result["same_session"])
        self.assertFalse(result["dispatch_required"])
        waiting = self.service.get_task(self.task.id)
        input_request = waiting.extensions["agentbc.input"]
        self.assertTrue(input_request["native_live_elevation"])
        self.assertEqual(input_request["session_id"], self.session_id)
        self.assertEqual(input_request["input_fingerprint"], stable_input_digest({"request_fingerprint": request_fingerprint}))
        notification = build_input_required_notification(self.service, self.task.id)
        self.assertTrue(notification["native_live_elevation"])
        self.assertIn("--approve", notification["respond_command"])
        self.assertNotIn("--approve-full", notification["respond_command"])
        self.assertIn("setMode/bypassPermissions/session", notification["message"])

        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(
                True,
                "test dialog",
                "dialog:test",
                {"action": "dismissed"},
            ),
        ) as dialog_send:
            first_notice = notify_input_required(self.service, self.task.id)
            second_notice = notify_input_required(self.service, self.task.id)
        self.assertEqual(dialog_send.call_count, 1)
        self.assertEqual(first_notice["dialog_action"], "dismissed")
        self.assertEqual(second_notice["dialog_action"], "already_delivered")
        receipt = self.service.get_task(self.task.id).extensions["agentbc.claude_elevation"]
        self.assertEqual(receipt["cardinality"]["dialogs"], 1)

        answered = self.service.respond_to_live_claude_elevation(
            self.task.id,
            input_request["input_id"],
            response_type="approve",
        )
        self.assertEqual(answered["approval_decision"], "approve")
        self.assertFalse(answered["dispatch_required"])
        self.assertTrue(answered["same_session"])
        self.assertEqual(answered["status"], "running")
        approval = self.service.get_task(self.task.id).extensions["agentbc.approval"]
        self.assertEqual(approval["state"]["status"], "answered")
        self.assertEqual(approval["decision"]["type"], "approve_full")
        lease = load_lease(self.task.id, self.board)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.state, RunLeaseState.ACTIVE)
        self.assertEqual(lease.run_id, self.run_id)
        self.assertEqual(
            self.service.get_task(self.task.id).extensions["agentbc.session"]["session_id"],
            self.session_id,
        )


if __name__ == "__main__":
    unittest.main()
