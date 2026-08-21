"""Production-chain tests for the Codex App Server single-action approval.

Covers the PERM-103-009 freeze:

* the canonical ``app-server`` transport value and the capability gate that
  verifies the configured executable against the frozen schema contract for
  both the Runner-pinned ``0.146.0`` and local ``0.147.0`` surfaces,
* fail-closed behavior for unknown versions, missing schema methods,
  malformed receipts, cross-task/run/session requests, duplicate/concurrent/
  late responses and transport death,
* the executor-neutral exact-action receipt (accept/decline only) and the
  same-process App Server flow that returns the decision to the live session,
* ``inherit`` preserving native settings while using App Server for trusted
  runtime-block detection, plus the existing CLI/one-time full fallback.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.codex_app_server import (
    CODEX_APP_SERVER_MAX_VERSION,
    CODEX_APP_SERVER_MIN_VERSION,
    CODEX_APP_SERVER_TRANSPORT,
    CODEX_APP_SERVER_TRANSPORT_ALIASES,
    codex_app_server_contract,
    parse_codex_version,
)
from agent_bridge_connect.control import (
    ApprovalControlPlane,
    ControlPlaneError,
    approval_response_payload,
    normalize_approval_request,
)
from agent_bridge_connect.execution_policy import build_session_snapshot
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.permission_modes import build_permission_record, permission_record_from_extensions
from agent_bridge_connect.permission_registry import (
    TRANSPORT_CODEX_APP_SERVER,
    executor_permission_mapping,
    probe_executor_capability,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.runner import RunnerError, RunnerState
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.session import control_root_for_task

FIXTURES = Path(__file__).parent / "fixtures" / "executor_runtime"

# Both surfaces share the frozen schema bundle shape.
CONTRACT_FIXTURES = {
    "0.146.0": FIXTURES / "codex_app_server_protocol.0.146.0.contract.json",
    "0.147.0": FIXTURES / "codex_app_server_protocol.0.147.0.contract.json",
}


def load_contract_fixture(version: str) -> dict:
    return json.loads(CONTRACT_FIXTURES[version].read_text(encoding="utf-8"))


def capability_override(version: str = "0.146.0") -> dict:
    return {
        "ok": True,
        "transport": CODEX_APP_SERVER_TRANSPORT,
        "protocol_version": 2,
        "version": f"codex-cli {version}",
        "version_parsed": tuple(int(part) for part in version.split(".")),
        "schema_missing": [],
        "evidence": ["version_gate", "schema_methods_verified"],
        "schema_summary": "CodexAppServerProtocol",
    }


class SchemaContractTests(unittest.TestCase):
    def test_both_versions_pass_the_frozen_surface(self) -> None:
        for version, path in CONTRACT_FIXTURES.items():
            with self.subTest(version=version):
                bundle = json.loads(path.read_text(encoding="utf-8"))
                result = codex_app_server_contract(
                    "/tmp/fake-codex",
                    version_output=f"codex-cli {version}",
                    schema_bundle=bundle,
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["transport"], CODEX_APP_SERVER_TRANSPORT)
                self.assertEqual(result["protocol_version"], 2)
                self.assertEqual(result["schema_missing"], [])
                self.assertEqual(result["evidence"], ["version_gate", "schema_methods_verified"])

    def test_unknown_version_fails_closed(self) -> None:
        for version in ("codex-cli 0.145.0", "codex-cli 0.148.0", "codex-cli 9.9.9"):
            with self.subTest(version=version):
                result = codex_app_server_contract(
                    "/tmp/fake-codex",
                    version_output=version,
                    schema_bundle=load_contract_fixture("0.147.0"),
                )
                self.assertFalse(result["ok"])
                self.assertIn("outside the frozen", result["reason"])

    def test_missing_method_fails_closed(self) -> None:
        bundle = load_contract_fixture("0.147.0")
        # Drop the item/permissions/requestApproval method from the ServerRequest.
        definitions = bundle["definitions"]
        for name in list(definitions):
            if name.startswith("ServerRequest"):
                value = definitions[name]
                text = json.dumps(value)
                if "item/permissions/requestApproval" in text:
                    serialized = json.dumps(value).replace(
                        '"item/permissions/requestApproval"', '"item/removed"'
                    )
                    definitions[name] = json.loads(serialized)
        result = codex_app_server_contract(
            "/tmp/fake-codex",
            version_output="codex-cli 0.147.0",
            schema_bundle=bundle,
        )
        self.assertFalse(result["ok"])
        self.assertIn("item/permissions/requestApproval", result["schema_missing"])

    def test_missing_item_completed_fails_closed(self) -> None:
        for version in ("0.146.0", "0.147.0"):
            with self.subTest(version=version):
                bundle = load_contract_fixture(version)
                definitions = bundle["definitions"]
                for name in list(definitions):
                    if name.startswith("ServerNotification"):
                        definitions[name] = json.loads(
                            json.dumps(definitions[name]).replace(
                                '"item/completed"', '"item/removed"'
                            )
                        )
                result = codex_app_server_contract(
                    "/tmp/fake-codex",
                    version_output=f"codex-cli {version}",
                    schema_bundle=bundle,
                )
                self.assertFalse(result["ok"])
                self.assertIn("item/completed", result["schema_missing"])

    def test_malformed_bundle_fails_closed(self) -> None:
        result = codex_app_server_contract(
            "/tmp/fake-codex",
            version_output="codex-cli 0.147.0",
            schema_bundle={"definitions": "not-a-dict"},
        )
        self.assertFalse(result["ok"])
        self.assertIn("missing frozen surface", result["reason"])

    def test_parse_codex_version(self) -> None:
        self.assertEqual(parse_codex_version("codex-cli 0.146.0"), (0, 146, 0))
        self.assertEqual(parse_codex_version("0.147.0"), (0, 147, 0))
        self.assertIsNone(parse_codex_version(""))
        self.assertIsNone(parse_codex_version("codex-cli"))

    def test_min_max_version_bounds_are_frozen(self) -> None:
        self.assertEqual(CODEX_APP_SERVER_MIN_VERSION, (0, 146, 0))
        self.assertEqual(CODEX_APP_SERVER_MAX_VERSION, (0, 147, 0))

    def test_transport_aliases_are_only_backward_compatible(self) -> None:
        self.assertEqual(CODEX_APP_SERVER_TRANSPORT, "app-server")
        self.assertTrue({"app-server", "app_server", "stdio", "codex-app-server"} <= CODEX_APP_SERVER_TRANSPORT_ALIASES)


class CapabilityGateTests(unittest.TestCase):
    def test_executor_mapping_accepts_app_server_transport_for_codex(self) -> None:
        entry = executor_permission_mapping("codex", "safe", transport=TRANSPORT_CODEX_APP_SERVER)
        self.assertEqual(entry["transport"], TRANSPORT_CODEX_APP_SERVER)
        self.assertEqual(entry["mode"], "safe")
        self.assertEqual(entry["direct_args"], ["--sandbox", "workspace-write"])

    def test_executor_mapping_rejects_unknown_codex_transport(self) -> None:
        with self.assertRaises(ABCError) as raised:
            executor_permission_mapping("codex", "safe", transport="tcp")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")

    def test_probe_fails_closed_without_verified_capability(self) -> None:
        # The fake executable is the python interpreter; the real schema probe
        # would fail. The probe must fail closed rather than approximate safe.
        with mock.patch(
            "agent_bridge_connect.codex_app_server.assert_codex_app_server_capability",
            side_effect=ABCError(
                "permission_capability_unsupported",
                "schema missing",
                {"reason": "schema missing", "version": "0.147.0"},
            ),
        ):
            with self.assertRaises(ABCError) as raised:
                probe_executor_capability(
                    "codex", "safe", "/tmp/fake-codex", transport=TRANSPORT_CODEX_APP_SERVER
                )
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")
        self.assertEqual(raised.exception.details["transport"], TRANSPORT_CODEX_APP_SERVER)
        self.assertEqual(raised.exception.details["permission_mode"], "safe")

    def test_probe_success_freezes_transport_and_surface(self) -> None:
        probe = {
            **capability_override("0.147.0"),
            "evidence": ["version_gate", "schema_methods_verified"],
        }
        with mock.patch(
            "agent_bridge_connect.codex_app_server.assert_codex_app_server_capability",
            return_value=probe,
        ):
            report = probe_executor_capability(
                "codex", "safe", "/tmp/fake-codex", transport=TRANSPORT_CODEX_APP_SERVER
            )
        self.assertTrue(report["supported"])
        self.assertEqual(report["transport"], TRANSPORT_CODEX_APP_SERVER)
        self.assertEqual(report["capability_id"], "codex.sandbox_workspace_write")
        self.assertIn("version_gate", report["evidence"])
        self.assertEqual(report["details"]["decisions"], ["accept", "decline"])
        self.assertEqual(report["details"]["scope"], "single_action")
        self.assertIn("item/commandExecution/requestApproval", report["details"]["request_methods"])

    def test_executor_capability_gate_accepts_inherit_and_rejects_full(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="app-server")
        executor._app_server_capability_override = capability_override()
        report = executor._freeze_app_server_capability({"effective_mode": "inherit"})
        self.assertTrue(report["ok"])
        with self.assertRaises(ABCError) as raised:
            executor._freeze_app_server_capability({"effective_mode": "full"})
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")

    def test_executor_capability_gate_accepts_safe_with_verified_report(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="app-server")
        executor._app_server_capability_override = capability_override("0.147.0")
        report = executor._freeze_app_server_capability({"effective_mode": "safe"})
        self.assertTrue(report["ok"])
        self.assertEqual(executor._app_server_capability["transport"], "app-server")


class BlockingFakeTransport:
    """Deterministic in-memory App Server transport for both version surfaces."""

    def __init__(
        self,
        board: Path,
        task_id: str,
        *,
        version: str = "0.146.0",
        emit_callback: bool = True,
    ) -> None:
        self.board = board
        self.task_id = task_id
        self.version = version
        self.sent: list[dict] = []
        self.queue: list[dict] = []
        self.condition = threading.Condition()
        self.closed = False
        self.receipt_before_turn = False
        self.approval_count = 0
        self.emit_callback = emit_callback

    def start(self) -> None:
        return None

    def send(self, message: dict) -> None:
        with self.condition:
            self.sent.append(message)
            method = message.get("method")
            if method == "initialize":
                self.queue.append({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            elif method == "thread/start":
                self.queue.append(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"thread": {"id": "thread-fake-1"}},
                    }
                )
            elif method == "thread/resume":
                self.queue.append(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"thread": {"id": message["params"]["threadId"]}},
                    }
                )
            elif method == "turn/start":
                self.receipt_before_turn = (
                    (self.board / ".agentbc-control" / self.task_id / "session_receipt.json").is_file()
                )
                self.queue.append(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"turn": {"id": "turn-fake-1", "status": "in_progress"}},
                    }
                )
                self.queue.append(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-fake-1",
                            "turnId": "turn-fake-1",
                            "item": {
                                "id": "item-user-1",
                                "type": "userMessage",
                                "text": "AGENTBC_FINAL_CALLBACK: prompt example only",
                            },
                        },
                    }
                )
                self.queue.append(
                    {
                        "jsonrpc": "2.0",
                        "id": 90,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": "thread-fake-1",
                            "turnId": "turn-fake-1",
                            "itemId": "item-fake-1",
                            "reason": "run the approved command",
                        },
                    }
                )
            elif message.get("id") == 90:
                self.approval_count += 1
                if self.emit_callback:
                    callback = {
                        "version": 1,
                        "task_id": self.task_id,
                        "final_state": "completed",
                        "summary": "app server callback accepted",
                        "step_results": [{"id": 1, "status": "done"}],
                    }
                    self.queue.append(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": "thread-fake-1",
                                "turnId": "turn-fake-1",
                                "item": {
                                    "id": "item-agent-1",
                                    "type": "agentMessage",
                                    "text": (
                                        "done\nAGENTBC_FINAL_CALLBACK: "
                                        + json.dumps(callback, separators=(",", ":"))
                                    ),
                                },
                            },
                        }
                    )
                self.queue.append(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-fake-1",
                            "turn": {"id": "turn-fake-1", "status": "completed"},
                        },
                    }
                )
            self.condition.notify_all()

    def recv(self) -> dict:
        with self.condition:
            deadline = time.monotonic() + 3.0
            while not self.queue and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("fake transport timed out")
                self.condition.wait(remaining)
            if not self.queue:
                raise RuntimeError("fake transport closed")
            return self.queue.pop(0)

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def is_alive(self) -> bool:
        return not self.closed


class CodexAppServerProductionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task_id = "CDEX-PROD-001"
        self.board = self.root / "record"
        self.board.mkdir()
        self.receipt = {
            "version": 1,
            "executor": "codex",
            "session_id": "thread-fake-1",
            "resumed": False,
            "persistence": "persistent",
            "source": "jsonl_thread_started",
        }

    def _packet(self, *, resumed: bool = False, transport: str = "app-server") -> dict:
        session = build_session_snapshot(
            "codex",
            retain=False,
            session_id="thread-fake-1" if resumed else "",
            session_state="active" if resumed else "pending",
            run_ids=["prior-run"] if resumed else [],
        )
        permission = build_permission_record(explicit_mode="safe")
        permission["mapping"] = permission["mapping"]
        # Freeze the canonical app-server transport into the mapping.
        mapping = dict(permission.get("mapping") or {})
        codex_entry = dict(mapping.get("codex") or {})
        codex_entry["transport"] = transport
        mapping["codex"] = codex_entry
        permission["mapping"] = mapping
        return {
            "task_id": self.task_id,
            "assignee": "codex",
            "title": "production app server chain",
            "steps": [{"id": 1, "description": "exercise approval"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.board)},
            "extensions": {
                "agentbc.permission": permission,
                "agentbc.session": session,
            },
        }

    def _executor(self, fake: BlockingFakeTransport, *, version: str = "0.146.0") -> CodexExecutor:
        executor = CodexExecutor(
            command=sys.executable,
            transport="app-server",
            transport_factory=lambda **_: fake,
            approval_timeout_s=2,
        )
        executor._app_server_capability_override = capability_override(version)
        return executor

    def _plane(self, executor_run_id: str, session_id: str = "thread-fake-1") -> ApprovalControlPlane:
        return ApprovalControlPlane(
            control_root_for_task(self.task_id, board_root=self.board),
            task_id=self.task_id,
            executor_run_id=executor_run_id,
            session_id=session_id,
            create=False,
        )

    def _wait_status(self, executor: CodexExecutor, run_id: str, statuses: set[str], timeout_s: float = 5.0) -> str:
        deadline = time.monotonic() + timeout_s
        status = ""
        while time.monotonic() < deadline:
            status = executor.poll(run_id).status
            if status in statuses:
                return status
            time.sleep(0.01)
        return status

    def test_approval_request_approve_same_session_completed(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run") as suspend_lease,
            mock.patch.object(executor, "_resume_run") as resume_lease,
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            self.assertTrue(started.ok)
            status = self._wait_status(executor, started.run_id, {"input_required"})
            self.assertEqual(status, "input_required")
            self.assertTrue(fake.receipt_before_turn)
            waiting = executor.poll(started.run_id)
            approval = waiting.result["approval_request"]
            self.assertEqual(approval["type"], "permission")
            self.assertEqual(approval["scope"], "single_action")
            self.assertEqual(approval["session_id"], "thread-fake-1")
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                "thread-fake-1",
                approval["request_id"],
                "accept",
            )
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "completed")
        self.assertTrue(suspend_lease.called)
        self.assertTrue(resume_lease.called)
        # The decision is returned to the same live App Server session.
        approval_response = next(
            message for message in fake.sent if message.get("id") == 90
        )
        self.assertEqual(approval_response["result"]["decision"], "accept")
        control_events = result.result["control_events"]
        self.assertEqual(
            [event["event_type"] for event in control_events],
            ["session_started", "approval_requested", "turn_completed"],
        )
        self.assertEqual(result.result["execution_session"]["session_id"], "thread-fake-1")
        self.assertFalse(result.result["execution_session"]["resumed"])
        self.assertTrue(result.result["marker_valid"])
        self.assertEqual(
            result.result["agent_callback"]["summary"],
            "app server callback accepted",
        )

    def test_completed_turn_without_agent_marker_fails(self) -> None:
        fake = BlockingFakeTransport(
            self.board,
            self.task_id,
            emit_callback=False,
        )
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            self.assertEqual(
                self._wait_status(executor, started.run_id, {"input_required"}),
                "input_required",
            )
            approval = executor.poll(started.run_id).result["approval_request"]
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                "thread-fake-1",
                approval["request_id"],
                "decline",
            )
            status = self._wait_status(
                executor,
                started.run_id,
                {"completed", "needs_recovery", "failed"},
            )
            result = executor.poll(started.run_id)
        self.assertEqual(status, "failed")
        self.assertEqual(
            result.result["failure"]["kind"],
            "completion_marker_missing",
        )

    def test_native_request_fingerprint_is_content_derived_and_redacted(self) -> None:
        first = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-fake-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "command": ["git", "switch", "-c", "probe-a"],
                    "reason": "write /private/secret/path using token=raw-secret",
                },
            },
            task_id=self.task_id,
            executor_run_id="run-1",
            session_id="thread-fake-1",
        )
        second = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-fake-1",
                    "turnId": "turn-1",
                    "itemId": "item-2",
                    "command": ["git", "switch", "-c", "probe-b"],
                },
            },
            task_id=self.task_id,
            executor_run_id="run-1",
            session_id="thread-fake-1",
        )
        self.assertNotEqual(first.request_fingerprint, second.request_fingerprint)
        persisted = json.dumps(first.to_dict())
        self.assertNotIn("probe-a", persisted)
        self.assertNotIn('"params"', persisted)
        self.assertNotIn('"argv"', persisted)
        self.assertNotIn('"switch"', persisted)
        self.assertNotIn("/private/secret/path", persisted)
        self.assertNotIn("raw-secret", persisted)

    def test_deny_returns_decision_to_same_session(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = self._executor(fake, version="0.147.0")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"input_required"})
            self.assertEqual(status, "input_required")
            approval = executor.poll(started.run_id).result["approval_request"]
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                "thread-fake-1",
                approval["request_id"],
                "decline",
            )
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            executor.poll(started.run_id)
        self.assertEqual(status, "completed")
        approval_response = next(
            message for message in fake.sent if message.get("id") == 90
        )
        self.assertEqual(approval_response["result"]["decision"], "decline")

    def test_resume_uses_explicit_thread_id_only(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet(resumed=True))
            self.assertTrue(started.ok)
            status = self._wait_status(executor, started.run_id, {"input_required"})
            self.assertEqual(status, "input_required")
            request = executor.poll(started.run_id).result["approval_request"]
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                "thread-fake-1",
                request["request_id"],
                "decline",
            )
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
        self.assertEqual(status, "completed")
        methods = [message.get("method") for message in fake.sent if message.get("method")]
        self.assertIn("thread/resume", methods)
        self.assertNotIn("thread/start", methods)
        resume_message = next(message for message in fake.sent if message.get("method") == "thread/resume")
        self.assertEqual(resume_message["params"]["threadId"], "thread-fake-1")
        self.assertNotIn("--last", json.dumps(fake.sent))

    def test_cross_session_request_fails_closed(self) -> None:
        root = control_root_for_task(self.task_id, board_root=self.board)
        plane = ApprovalControlPlane(
            root,
            task_id=self.task_id,
            executor_run_id="codex-prod-run",
            session_id="thread-fake-1",
            approval_timeout_s=2,
        )
        plane.record_session_started(self.receipt)
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "thread-other",
                "turnId": "turn-1",
                "permissions": {"fileSystem": "workspace-write"},
            },
        }
        with self.assertRaises(ControlPlaneError) as raised:
            plane.request_approval(request)
        self.assertEqual(raised.exception.code, "approval_session_mismatch")

    def test_duplicate_and_concurrent_requests_fail_closed(self) -> None:
        root = control_root_for_task(self.task_id, board_root=self.board)
        plane = ApprovalControlPlane(
            root,
            task_id=self.task_id,
            executor_run_id="codex-prod-run",
            session_id="thread-fake-1",
            approval_timeout_s=2,
        )
        plane.record_session_started(self.receipt)
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-fake-1", "turnId": "turn-1", "itemId": "item-1"},
        }
        plane.request_approval(request)
        # Same request id is a duplicate.
        with self.assertRaises(ControlPlaneError) as raised:
            plane.request_approval(request)
        self.assertEqual(raised.exception.code, "approval_request_duplicate")
        # A different request id while one is pending is concurrent.
        with self.assertRaises(ControlPlaneError) as raised:
            plane.request_approval(dict(request, id=8))
        self.assertEqual(raised.exception.code, "approval_concurrent_request")

    def test_timeout_and_late_response_fail_closed(self) -> None:
        root = control_root_for_task(self.task_id, board_root=self.board)
        plane = ApprovalControlPlane(
            root,
            task_id=self.task_id,
            executor_run_id="codex-prod-run",
            session_id="thread-fake-1",
            approval_timeout_s=0.05,
        )
        plane.record_session_started(self.receipt)
        plane.request_approval(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": "thread-fake-1", "turnId": "turn-1"},
            }
        )
        with self.assertRaises(ControlPlaneError) as raised:
            plane.wait_for_decision("7", 0.06)
        self.assertEqual(raised.exception.code, "approval_request_expired")
        with self.assertRaises(ControlPlaneError):
            plane.respond_approval(self.task_id, "codex-prod-run", "thread-fake-1", "7", "accept")

    def test_transport_death_invalidates_pending_request(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"input_required"})
            self.assertEqual(status, "input_required")
            request_id = executor.poll(started.run_id).result["approval_request"]["request_id"]
            fake.close()
            status = self._wait_status(executor, started.run_id, {"needs_recovery"})
            result = executor.poll(started.run_id)
            plane = self._plane(started.run_id)
            with self.assertRaises(ControlPlaneError):
                plane.respond_approval(
                    self.task_id,
                    started.run_id,
                    "thread-fake-1",
                    request_id,
                    "accept",
                )
        self.assertEqual(status, "needs_recovery")
        self.assertIn("transport_failed", [event["event_type"] for event in result.result["control_events"]])

    def test_accept_never_contains_session_wide_escalation(self) -> None:
        request = {"operation": "command", "requested_permissions": {}}
        accepted = approval_response_payload(request, "accept")
        serialized = json.dumps(accepted)
        self.assertNotIn("acceptForSession", serialized)
        self.assertNotIn("acceptWithExecpolicyAmendment", serialized)
        self.assertNotIn("allow_always", serialized)
        request = {"operation": "permissions", "requested_permissions": {"fileSystem": "workspace-write"}}
        accepted = approval_response_payload(request, "accept")
        self.assertEqual(accepted["scope"], "turn")
        self.assertNotIn("acceptForSession", json.dumps(accepted))

    def test_get_extensions_freezes_transport_and_capability(self) -> None:
        executor = self._executor(BlockingFakeTransport(self.board, self.task_id), version="0.147.0")
        executor._freeze_app_server_capability({"effective_mode": "safe"})
        details = executor.get_extensions()["executor"]["codex"]
        capability = details["app_server_capability"]
        self.assertEqual(capability["transport"], "app-server")
        self.assertTrue(capability["ok"])
        self.assertEqual(capability["version"], "codex-cli 0.147.0")
        self.assertEqual(capability["version_parsed"], (0, 147, 0))
        self.assertEqual(capability["protocol_version"], 2)

    def test_run_metadata_records_frozen_transport(self) -> None:
        executor = self._executor(BlockingFakeTransport(self.board, self.task_id))
        run_id = "codex-prod-meta"
        packet = self._packet()
        executor._task_packets[run_id] = dict(packet)
        executor._app_runs[run_id] = {
            "session_id": "thread-fake-1",
            "events": [
                {"event_type": "approval_requested", "source": "agentbc.control"},
                {"event_type": "turn_completed", "source": "agentbc.control"},
            ],
        }
        executor._store_metadata(run_id, self.root, [], returncode=0)
        metadata = executor._run_metadata[run_id]
        self.assertEqual(metadata["transport"], "app-server")
        self.assertEqual(metadata["session_id"], "thread-fake-1")
        self.assertEqual(metadata["approval_events"], 1)


class RunnerCapabilityValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / "record"
        self.board.mkdir(parents=True)
        self.codex = self.root / "codex"
        self.codex.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *--help*) printf '%s\\n' '--dangerously-bypass-approvals-and-sandbox'; exit 0;;\n"
            "  *--version*) printf '%s\\n' 'codex-cli 0.147.0'; exit 0;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.codex.chmod(self.codex.stat().st_mode | 0o755)
        self.state = RunnerState(
            self.root / "runner",
            [self.root],
            {"codex": self.codex},
        )

    TASK_ID = "AB7C-001"

    def test_single_action_response_keeps_live_worker_and_session(self) -> None:
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "inherit"},
        )
        task = service.create_task(
            "native response bridge",
            "codex",
            [{"id": 1, "description": "request one action"}],
            customer_dir=True,
            customer_path=self.root,
            permission_mode="inherit",
        )
        run_id = "codex-native-run-1"
        service.start_task_run(task.id, "codex")
        service.record_executor_run_started(task.id, run_id)
        receipt = {
            "version": 1,
            "executor": "codex",
            "session_id": "thread-native-1",
            "resumed": False,
            "persistence": "persistent",
            "source": "jsonl_thread_started",
        }
        plane = ApprovalControlPlane(
            control_root_for_task(task.id, board_root=self.board),
            task_id=task.id,
            executor_run_id=run_id,
            session_id="thread-native-1",
        )
        plane.record_session_started(receipt)
        event = plane.request_approval(
            {
                "jsonrpc": "2.0",
                "id": 77,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-native-1",
                    "turnId": "turn-native-1",
                    "itemId": "item-native-1",
                    "command": ["git", "switch", "-c", "probe"],
                },
            }
        )
        blocked = service.block_task_for_approval(
            task.id,
            executor_run_id=run_id,
            session_id="thread-native-1",
            request_id="77",
            request_fingerprint=str(event["request_fingerprint"]),
            executor="codex",
            operation="command",
            execution_session=receipt,
        )
        with (
            mock.patch(
                "agent_bridge_connect.config.load_config",
                return_value={
                    "workspace_root": str(self.root),
                    "permission_mode": "inherit",
                },
            ),
            mock.patch.object(self.state, "dispatch_worker") as dispatch_worker,
        ):
            result = self.state.respond_and_dispatch(
                {
                    "task_id": task.id,
                    "input_id": blocked["input_id"],
                    "response_type": "approve",
                    "message": "",
                    "board_root": str(self.board),
                    "config_path": "",
                    "interval_s": 0.01,
                }
            )
        dispatch_worker.assert_not_called()
        self.assertFalse(result["dispatch_required"])
        self.assertTrue(result["same_session"])
        self.assertEqual(result["native_response"]["decision"], "accept")
        response = plane.wait_for_decision("77", 0.1)
        self.assertEqual(response["decision"], "accept")
        resumed = service.get_task(task.id)
        self.assertEqual(resumed.status, "running")
        self.assertEqual(
            resumed.extensions["agentbc.session"]["session_id"],
            "thread-native-1",
        )

    def _packet(
        self,
        *,
        mode: str = "safe",
        transport: str | None = "app-server",
    ) -> dict:
        session = build_session_snapshot(
            "codex",
            retain=False,
            session_id="thread-fake-1",
            session_state="active",
            run_ids=["prior-run"],
        )
        permission = build_permission_record(explicit_mode=mode)
        mapping = dict(permission.get("mapping") or {})
        codex_entry = dict(mapping.get("codex") or {})
        if transport is not None:
            codex_entry["transport"] = transport
        else:
            codex_entry.pop("transport", None)
        mapping["codex"] = codex_entry
        permission["mapping"] = mapping
        return {
            "task_id": self.TASK_ID,
            "assignee": "codex",
            "title": "runner capability",
            "steps": [{"id": 1, "description": "exercise"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.board)},
            "extensions": {
                "agentbc.permission": permission,
                "agentbc.session": session,
            },
        }

    def _persisted_task(self, *, mode: str = "safe", transport: str | None = "app-server") -> dict:
        from agent_bridge_connect.task_store import TaskStore

        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace")},
        )
        task = service.create_task(
            "runner capability",
            "codex",
            [{"id": 1, "description": "exercise"}],
            customer_dir=True,
            customer_path=self.root,
            permission_mode=mode,
        )
        session = dict(task.extensions.get("agentbc.session") or {})
        session.update(
            {
                "session_id": "thread-fake-1",
                "session_state": "active",
                "run_ids": ["prior-run"],
            }
        )
        permission = permission_record_from_extensions(task.extensions, allow_legacy=False)
        permission = dict(permission)
        if permission.get("version") is None:
            permission = build_permission_record(explicit_mode=mode)
        if transport is not None:
            mapping = dict(permission.get("mapping") or {})
            codex_entry = dict(mapping.get("codex") or {})
            codex_entry["transport"] = transport
            mapping["codex"] = codex_entry
            permission["mapping"] = mapping
        else:
            # "Unfrozen": drop the transport from the codex mapping so the
            # persisted task does not claim a transport.
            mapping = dict(permission.get("mapping") or {})
            codex_entry = dict(mapping.get("codex") or {})
            codex_entry.pop("transport", None)
            mapping["codex"] = codex_entry
            permission["mapping"] = mapping
        extensions = dict(task.extensions or {})
        extensions["agentbc.session"] = session
        extensions["agentbc.permission"] = permission
        raw = dict(task.to_dict())
        raw["status"] = "running"
        raw["extensions"] = extensions
        TaskStore(self.board).write_task(task.id, raw)
        return raw

    def test_runner_accepts_app_server_safe_command(self) -> None:
        persisted = self._persisted_task()
        packet = self._packet()
        packet["task_id"] = persisted["id"]
        packet["workspace"] = persisted["workspace"]
        packet["extensions"] = persisted["extensions"]
        packet["task_board"] = {"root": str(self.board)}
        result = self.state.authorize_command(
            "codex",
            [str(self.codex), "app-server", "--stdio"],
            str(self.root),
            packet,
            executor_run_id="codex-runner-1",
        )
        self.assertTrue(result["authorized"])
        self.assertEqual(result["effective_permission_mode"], "safe")

    def test_runner_accepts_default_safe_mapping_on_app_server_command(self) -> None:
        # A task created with default (unfrozen) CLI mapping is still safe and
        # may select the App Server chain; the executor capability gate
        # verifies the executable before the run.
        persisted = self._persisted_task(transport=None)
        packet = self._packet(transport=None)
        packet["task_id"] = persisted["id"]
        packet["workspace"] = persisted["workspace"]
        packet["extensions"] = persisted["extensions"]
        packet["task_board"] = {"root": str(self.board)}
        result = self.state.authorize_command(
            "codex",
            [str(self.codex), "app-server", "--stdio"],
            str(self.root),
            packet,
            executor_run_id="codex-runner-1",
        )
        self.assertTrue(result["authorized"])
        self.assertEqual(result["effective_permission_mode"], "safe")

    def test_runner_accepts_inherit_app_server_without_permission_override(self) -> None:
        persisted = self._persisted_task(mode="inherit")
        packet = self._packet(mode="inherit")
        packet["task_id"] = persisted["id"]
        packet["workspace"] = persisted["workspace"]
        packet["extensions"] = persisted["extensions"]
        packet["task_board"] = {"root": str(self.board)}
        result = self.state.authorize_command(
            "codex",
            [str(self.codex), "app-server", "--stdio"],
            str(self.root),
            packet,
            executor_run_id="codex-runner-inherit",
        )
        self.assertTrue(result["authorized"])
        self.assertEqual(result["effective_permission_mode"], "inherit")

    def test_runner_rejects_cli_frozen_transport_on_app_server_command(self) -> None:
        persisted = self._persisted_task(transport="cli")
        packet = self._packet(transport="cli")
        packet["task_id"] = persisted["id"]
        packet["workspace"] = persisted["workspace"]
        packet["extensions"] = persisted["extensions"]
        packet["task_board"] = {"root": str(self.board)}
        with self.assertRaises(RunnerError) as raised:
            self.state.authorize_command(
                "codex",
                [str(self.codex), "app-server", "--stdio"],
                str(self.root),
                packet,
                executor_run_id="codex-runner-1",
            )
        self.assertIn("runner_capability_mismatch", str(raised.exception))

    def test_runner_rejects_full_permission_on_app_server_command(self) -> None:
        persisted = self._persisted_task(mode="full", transport="app-server")
        packet = self._packet()
        packet["task_id"] = persisted["id"]
        packet["workspace"] = persisted["workspace"]
        packet["extensions"] = persisted["extensions"]
        packet["task_board"] = {"root": str(self.board)}
        with self.assertRaises(RunnerError) as raised:
            self.state.authorize_command(
                "codex",
                [str(self.codex), "app-server", "--stdio"],
                str(self.root),
                packet,
                executor_run_id="codex-runner-1",
            )
        self.assertIn("runner_capability_mismatch", str(raised.exception))


class InheritAndFallbackTests(unittest.TestCase):
    def test_inherit_mapping_uses_app_server_without_permission_override(self) -> None:
        entry = executor_permission_mapping("codex", "inherit")
        self.assertEqual(entry["transport"], "app-server")
        self.assertEqual(entry["args"], [])
        self.assertEqual(entry["direct_args"], [])
        self.assertEqual(entry["env"], {})
        self.assertFalse(entry["overrides_native"])
        self.assertEqual(entry["capability_id"], "codex.inherit")

    def test_codex_cli_fallback_unchanged(self) -> None:
        entry = executor_permission_mapping("codex", "full")
        self.assertEqual(entry["transport"], "cli")
        self.assertEqual(
            entry["args"], ["--dangerously-bypass-approvals-and-sandbox"]
        )
        self.assertTrue(entry["overrides_native"])

    def test_auto_transport_uses_receipted_runtime_for_inherit_and_safe_only(self) -> None:
        executor = CodexExecutor(command=sys.executable)
        inherit_packet = {
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="inherit")
            }
        }
        safe_packet = {
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe")
            }
        }
        full_packet = {
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="full")
            }
        }
        self.assertTrue(executor._uses_app_server_transport(inherit_packet))
        self.assertTrue(executor._uses_app_server_transport(safe_packet))
        self.assertFalse(executor._uses_app_server_transport(full_packet))

    def test_executor_cli_path_never_selects_app_server(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="cli")
        self.assertFalse(executor._uses_app_server_transport())


if __name__ == "__main__":
    unittest.main()
