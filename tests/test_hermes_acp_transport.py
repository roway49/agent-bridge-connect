"""Deterministic fake-ACP tests for the Task 6 Hermes ACP control path.

Covers the fail-closed session-first transport (``hermes_acp`` module) and
its bridge into the frozen approval receipt / ControlPlane (Hermes executor
``transport=acp``): handshake ordering, receipt persistence before the first
prompt, explicit resume, allow_once/deny mapping, unsupported option lists,
identity mismatches, duplicate requests, approval timeout, and transport
death.

Two fake layers keep the tests deterministic and Hermes-free:

* :class:`FakeAcpTransport` - an in-process fake injected through the
  executor's transport seam (``_acp_transport_override``).
* ``hermes_acp_fake_server.py`` - a real stdio subprocess speaking ACP,
  driven by fixed modes, for the wire-level transport tests.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.control import ApprovalControlPlane
from agent_bridge_connect.execution_policy import build_session_snapshot
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.hermes_acp import (
    HERMES_ACP_ALLOW_ONCE_OPTION,
    HERMES_ACP_DENIED_OUTCOME,
    HermesAcpError,
    HermesAcpTransport,
    HermesAcpUnsupported,
    approval_outcome_for_decision,
    build_approval_message,
    frame_kind,
    permission_request_options,
    validate_initialize_result,
)
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.permission_registry import (
    HERMES_ACP_ALLOWED_DECISIONS,
    HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
    probe_executor_capability,
)
from agent_bridge_connect.session import control_root_for_task

FAKE_SERVER = (
    Path(__file__).parent
    / "fixtures"
    / "executor_runtime"
    / "hermes_acp_fake_server.py"
)

FAKE_SESSION_ID = "acp-session-fake-1"
FAKE_OTHER_SESSION_ID = "acp-session-other-1"


def _permission_frame(
    *,
    request_id: int = 100,
    session_id: str = FAKE_SESSION_ID,
    include_allow_once: bool = True,
    tool_call_id: str = "perm-check-1",
) -> dict:
    options = []
    if include_allow_once:
        options.append({"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"})
    options.append({"optionId": "deny", "kind": "reject_once", "name": "Deny"})
    options.append({"optionId": "allow_session", "kind": "allow_always", "name": "Allow for session"})
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "id": tool_call_id,
                "kind": "execute",
                "title": "run the approved command",
                "status": "pending",
                "content": [{"type": "text", "text": "$ hermes acp --check"}],
                "rawInput": {"command": "hermes acp --check", "description": "verify ACP"},
            },
            "options": options,
        },
    }


class FakeAcpTransport:
    """In-process fake ACP transport with scripted permission behavior.

    Mirrors the ``HermesAcpTransport`` interface used by the executor.
    ``prompt`` calls ``on_permission`` synchronously in the worker thread so
    the ControlPlane decision flow is exercised exactly like production.
    """

    def __init__(self, board: Path, task_id: str) -> None:
        self.board = board
        self.task_id = task_id
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.receipt_before_turn = False
        self.session_id = FAKE_SESSION_ID
        self.mode = "allow"
        self.prompt_result = {"stopReason": "end_turn"}
        self.prompt_raises: Exception | None = None
        self.permission_frames: list[dict] = []
        self.closed = False

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.calls.append("start")

    def close(self) -> None:
        self.closed = True
        self.calls.append("close")

    def is_alive(self) -> bool:
        return not self.closed

    def stderr_evidence(self) -> str:
        return ""

    def message_text(self) -> str:
        # A real Hermes turn ends with the structured callback marker; the
        # fake emits one so the frozen flow-contract routing classifies the
        # turn exactly like production output would.
        return (
            "fake assistant summary\n"
            f'AGENTBC_FINAL_CALLBACK: {{"version":1,"task_id":"{self.task_id}",'
            '"final_state":"completed","summary":"fake turn completed",'
            '"step_results":[{"id":1,"status":"done"}]}'
        )

    # ---- protocol surface --------------------------------------------------

    def initialize(self) -> dict:
        self.calls.append("initialize")
        return {"protocolVersion": 1, "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}}

    def new_session(self, cwd: str) -> str:
        self.calls.append("new_session")
        self.sent.append({"method": "session/new", "params": {"cwd": cwd}})
        return self.session_id

    def load_session(self, cwd: str, session_id: str) -> str:
        self.calls.append("load_session")
        self.sent.append(
            {"method": "session/load", "params": {"cwd": cwd, "sessionId": session_id}}
        )
        return session_id

    def prompt(self, session_id: str, blocks: list[dict], *, on_permission, timeout_s=None) -> dict:
        self.calls.append("prompt")
        self.receipt_before_turn = (
            self.board / ".agentbc-control" / self.task_id / "session_receipt.json"
        ).is_file()
        self.sent.append(
            {
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": blocks},
            }
        )
        if self.prompt_raises is not None:
            raise self.prompt_raises
        for frame in self.permission_frames:
            outcome = on_permission(frame)
            self.sent.append({"method": "permission_response", "outcome": outcome})
        return dict(self.prompt_result)

    def respond_permission(self, request_id, outcome) -> None:
        self.sent.append({"method": "permission_response", "request_id": request_id, "outcome": outcome})

    def cancel_session(self, session_id: str) -> None:
        self.sent.append({"method": "session/cancel", "params": {"sessionId": session_id}})


class HermesAcpTransportUnitTests(unittest.TestCase):
    """Wire-level helpers: framing, version gate, option surface, outcomes."""

    def test_frame_kind_classification(self) -> None:
        self.assertEqual(frame_kind({"jsonrpc": "2.0", "id": 1, "result": {}}), "response")
        self.assertEqual(
            frame_kind({"jsonrpc": "2.0", "id": 2, "method": "session/request_permission"}),
            "request",
        )
        self.assertEqual(
            frame_kind({"jsonrpc": "2.0", "method": "session/update"}),
            "notification",
        )
        with self.assertRaises(HermesAcpError):
            frame_kind(["not", "an", "object"])
        with self.assertRaises(HermesAcpError):
            frame_kind({"jsonrpc": "2.0", "id": 1, "method": "x", "result": {}})

    def test_initialize_version_gate_fails_closed(self) -> None:
        self.assertEqual(validate_initialize_result({"protocolVersion": 1}), 1)
        with self.assertRaises(HermesAcpUnsupported):
            validate_initialize_result({"protocolVersion": 2})
        with self.assertRaises(HermesAcpUnsupported):
            validate_initialize_result({"protocolVersion": "1"})
        with self.assertRaises(HermesAcpUnsupported):
            validate_initialize_result(None)

    def test_permission_option_surface(self) -> None:
        has_once, offered = permission_request_options(
            [
                {"optionId": "allow_once", "kind": "allow_once"},
                {"optionId": "allow_session", "kind": "allow_always"},
                {"optionId": "deny", "kind": "reject_once"},
            ]
        )
        self.assertTrue(has_once)
        self.assertEqual(offered, ["allow_once", "allow_session", "deny"])
        has_once, offered = permission_request_options(
            [{"optionId": "deny", "kind": "reject_once"}]
        )
        self.assertFalse(has_once)
        self.assertEqual(offered, ["deny"])
        self.assertFalse(permission_request_options(None)[0])
        self.assertFalse(permission_request_options("nope")[0])

    def test_permission_request_validation_fails_closed(self) -> None:
        message = build_approval_message(
            _permission_frame(),
            task_id="T-1",
            executor_run_id="hermes-run-1",
            session_id=FAKE_SESSION_ID,
        )
        self.assertEqual(message["id"], 100)
        self.assertEqual(message["params"]["itemId"], "perm-check-1")
        self.assertEqual(message["params"]["threadId"], FAKE_SESSION_ID)

        # Wrong official session -> identity mismatch.
        with self.assertRaisesRegex(HermesAcpError, "session_mismatch"):
            build_approval_message(
                _permission_frame(session_id=FAKE_OTHER_SESSION_ID),
                task_id="T-1",
                executor_run_id="hermes-run-1",
                session_id=FAKE_SESSION_ID,
            )
        # Missing allow_once option -> unsupported.
        with self.assertRaisesRegex(HermesAcpError, "options_unsupported"):
            build_approval_message(
                _permission_frame(include_allow_once=False),
                task_id="T-1",
                executor_run_id="hermes-run-1",
                session_id=FAKE_SESSION_ID,
            )
        # Missing tool call -> malformed.
        broken = _permission_frame()
        broken["params"].pop("toolCall")
        with self.assertRaisesRegex(HermesAcpError, "tool_call_missing"):
            build_approval_message(
                broken,
                task_id="T-1",
                executor_run_id="hermes-run-1",
                session_id=FAKE_SESSION_ID,
            )

    def test_approval_outcome_mapping_is_exact(self) -> None:
        self.assertEqual(
            approval_outcome_for_decision("accept"),
            {"outcome": {"optionId": HERMES_ACP_ALLOW_ONCE_OPTION}},
        )
        self.assertEqual(
            approval_outcome_for_decision("decline"),
            {"outcome": {"outcome": HERMES_ACP_DENIED_OUTCOME}},
        )
        for invalid in ("allow_session", "allow_always", "deny_always", "maybe", "", None):
            with self.assertRaises(HermesAcpError):
                approval_outcome_for_decision(invalid)

    def test_registry_binds_allow_once_deny_surface(self) -> None:
        self.assertEqual(HERMES_ACP_ALLOWED_DECISIONS, ("allow_once", "deny"))
        probe = {
            "ok": True,
            "transport": "hermes-acp",
            "capability_id": "hermes.acp.check",
            "reason": "",
            "returncode": 0,
            "check_summary": "Hermes ACP check OK",
            "version": "0.20.1",
        }
        with mock.patch(
            "agent_bridge_connect.permission_registry.probe_hermes_acp",
            return_value=probe,
        ):
            capability = probe_executor_capability("hermes", "safe", executable="/fake/hermes")
        details = capability["details"]["session_request_permission"]
        self.assertEqual(details["state"], "bound")
        self.assertEqual(details["capability_id"], HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID)
        self.assertEqual(details["decisions"], ["allow_once", "deny"])


class HermesAcpSubprocessTests(unittest.TestCase):
    """Real stdio transport against the deterministic fake ACP server."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log_path = self.root / "server.jsonl"

    def _transport(self, mode: str) -> HermesAcpTransport:
        transport = HermesAcpTransport(
            sys.executable,
            cwd=self.root,
            command=[
                sys.executable,
                str(FAKE_SERVER),
                mode,
                str(self.log_path),
            ],
            rpc_timeout_s=5.0,
        )
        transport.start()
        self.addCleanup(transport.close)
        return transport

    def _server_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_handshake_ordering_and_allow_once_outcome(self) -> None:
        transport = self._transport("happy")
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        self.assertEqual(session_id, FAKE_SESSION_ID)
        outcome = transport.prompt(
            session_id,
            [{"type": "text", "text": "run the task"}],
            on_permission=lambda frame: approval_outcome_for_decision("accept"),
            timeout_s=10.0,
        )
        self.assertEqual(outcome["stopReason"], "end_turn")
        entries = self._server_log()
        self.assertTrue(
            any(
                entry.get("event") == "permission_outcome"
                and entry["frame"]["result"]["outcome"]["optionId"] == "allow_once"
                for entry in entries
            ),
            entries,
        )

    def test_deny_outcome_is_cancelled(self) -> None:
        transport = self._transport("happy")
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        transport.prompt(
            session_id,
            [{"type": "text", "text": "run the task"}],
            on_permission=lambda frame: approval_outcome_for_decision("decline"),
            timeout_s=10.0,
        )
        entries = self._server_log()
        self.assertTrue(
            any(
                entry.get("event") == "permission_outcome"
                and entry["frame"]["result"]["outcome"]["outcome"] == "cancelled"
                for entry in entries
            ),
            entries,
        )

    def test_explicit_load_resume(self) -> None:
        transport = self._transport("resume")
        transport.initialize()
        session_id = transport.load_session(str(self.root), FAKE_SESSION_ID)
        self.assertEqual(session_id, FAKE_SESSION_ID)
        outcome = transport.prompt(
            session_id,
            [{"type": "text", "text": "continue"}],
            on_permission=lambda frame: approval_outcome_for_decision("decline"),
            timeout_s=10.0,
        )
        self.assertEqual(outcome["stopReason"], "end_turn")

    def test_unsupported_protocol_version_fails_closed(self) -> None:
        transport = self._transport("wrong_version")
        with self.assertRaisesRegex(HermesAcpUnsupported, "unsupported_version"):
            transport.initialize()

    def test_malformed_frame_fails_closed(self) -> None:
        transport = self._transport("malformed")
        with self.assertRaisesRegex(HermesAcpError, "malformed"):
            transport.initialize()

    def test_duplicate_response_fails_closed(self) -> None:
        transport = self._transport("dup_response")
        transport.initialize()
        with self.assertRaisesRegex(HermesAcpError, "response_mismatch"):
            transport.new_session(str(self.root))

    def test_load_unknown_session_fails_closed(self) -> None:
        transport = self._transport("load_unknown")
        transport.initialize()
        with self.assertRaisesRegex(HermesAcpError, "rpc_error"):
            transport.load_session(str(self.root), FAKE_SESSION_ID)

    def test_transport_exit_mid_prompt_fails_closed(self) -> None:
        transport = self._transport("exit_mid_prompt")
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        with self.assertRaisesRegex(HermesAcpError, "transport_exited"):
            transport.prompt(
                session_id,
                [{"type": "text", "text": "run the task"}],
                on_permission=lambda frame: approval_outcome_for_decision("decline"),
                timeout_s=10.0,
            )

    def test_unsupported_permission_options_fail_closed(self) -> None:
        transport = self._transport("unsupported_options")
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        with self.assertRaisesRegex(HermesAcpError, "options_unsupported"):
            transport.prompt(
                session_id,
                [{"type": "text", "text": "run the task"}],
                on_permission=lambda frame: approval_outcome_for_decision("accept"),
                timeout_s=10.0,
            )

    def test_permission_session_mismatch_fails_closed(self) -> None:
        transport = self._transport("wrong_session_permission")
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        with self.assertRaisesRegex(HermesAcpError, "session_mismatch"):
            transport.prompt(
                session_id,
                [{"type": "text", "text": "run the task"}],
                on_permission=lambda frame: approval_outcome_for_decision("accept"),
                timeout_s=10.0,
            )


class HermesAcpExecutorTests(unittest.TestCase):
    """Executor bridge: receipt-before-prompt, ControlPlane, suspend/resume."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task_id = "HERMES-ACP-001"
        self.board = self.root / "record"
        self.board.mkdir()
        self.calls: list[str] = []

    def _packet(self, *, resumed: bool = False) -> dict:
        session = build_session_snapshot(
            "hermes",
            retain=False,
            session_id=FAKE_SESSION_ID if resumed else "",
            session_state="active" if resumed else "pending",
            run_ids=["prior-run"] if resumed else [],
        )
        return {
            "task_id": self.task_id,
            "assignee": "hermes",
            "title": "hermes acp control path",
            "steps": [{"id": 1, "description": "exercise ACP approval"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.board)},
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                "agentbc.session": session,
            },
        }

    def _executor(self, fake: FakeAcpTransport, *, approval_timeout_s: float = 2.0) -> HermesExecutor:
        executor = HermesExecutor(
            command=sys.executable,
            transport="acp",
            approval_timeout_s=approval_timeout_s,
        )
        executor._acp_transport_override = fake
        return executor

    def _wait_status(self, executor: HermesExecutor, run_id: str, statuses: set[str], timeout_s: float = 5.0) -> str:
        deadline = time.monotonic() + timeout_s
        status = ""
        while time.monotonic() < deadline:
            status = executor.poll(run_id).status
            if status in statuses:
                return status
            time.sleep(0.01)
        return status

    def _plane(self, executor_run_id: str) -> ApprovalControlPlane:
        return ApprovalControlPlane(
            control_root_for_task(self.task_id, board_root=self.board),
            task_id=self.task_id,
            executor_run_id=executor_run_id,
            session_id=FAKE_SESSION_ID,
            executor="hermes",
            create=False,
        )

    def test_session_first_receipt_before_prompt_and_allow_once(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame()]
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
            self.assertEqual(approval["session_id"], FAKE_SESSION_ID)
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                FAKE_SESSION_ID,
                approval["request_id"],
                "accept",
            )
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "completed")
        self.assertTrue(suspend_lease.called)
        self.assertTrue(resume_lease.called)
        calls = [call for call in fake.calls if call != "close"]
        self.assertEqual(calls, ["start", "initialize", "new_session", "prompt"])
        permission_response = next(
            message for message in fake.sent if message.get("method") == "permission_response"
        )
        self.assertEqual(
            permission_response["outcome"],
            {"outcome": {"optionId": "allow_once"}},
        )
        control_events = result.result["control_events"]
        self.assertEqual(
            [event["event_type"] for event in control_events],
            ["session_started", "approval_requested", "turn_completed"],
        )
        self.assertEqual(result.result["execution_session"]["session_id"], FAKE_SESSION_ID)
        self.assertFalse(result.result["execution_session"]["resumed"])

    def test_deny_maps_to_cancelled(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame()]
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
            approval = executor.poll(started.run_id).result["approval_request"]
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                FAKE_SESSION_ID,
                approval["request_id"],
                "decline",
            )
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
        self.assertEqual(status, "completed")
        permission_response = next(
            message for message in fake.sent if message.get("method") == "permission_response"
        )
        self.assertEqual(
            permission_response["outcome"],
            {"outcome": {"outcome": "cancelled"}},
        )

    def test_explicit_resume_loads_only_persisted_session(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = []
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet(resumed=True))
            self.assertTrue(started.ok)
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "completed")
        self.assertIn("load_session", fake.calls)
        self.assertNotIn("new_session", fake.calls)
        load_message = next(
            message for message in fake.sent if message.get("method") == "session/load"
        )
        self.assertEqual(load_message["params"]["sessionId"], FAKE_SESSION_ID)
        receipt = result.result["execution_session"]
        self.assertTrue(receipt["resumed"])
        self.assertEqual(receipt["session_id"], FAKE_SESSION_ID)
        persisted = json.loads(
            (
                self.board / ".agentbc-control" / self.task_id / "session_receipt.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["receipt"]["session_id"], FAKE_SESSION_ID)
        self.assertTrue(persisted["receipt"]["resumed"])

    def test_unsupported_options_never_reach_control_plane(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame(include_allow_once=False)]
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "needs_recovery")
        self.assertIn("options_unsupported", result.result["failure"]["message"])
        event_types = [event["event_type"] for event in result.result["control_events"]]
        self.assertNotIn("approval_requested", event_types)

    def test_permission_identity_mismatch_fails_closed(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame(session_id=FAKE_OTHER_SESSION_ID)]
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "needs_recovery")
        self.assertIn("session_mismatch", result.result["failure"]["message"])
        event_types = [event["event_type"] for event in result.result["control_events"]]
        self.assertNotIn("approval_requested", event_types)

    def test_duplicate_request_fails_closed(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame(request_id=100), _permission_frame(request_id=100)]
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
            approval = executor.poll(started.run_id).result["approval_request"]
            self._plane(started.run_id).respond_approval(
                self.task_id,
                started.run_id,
                FAKE_SESSION_ID,
                approval["request_id"],
                "accept",
            )
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "needs_recovery")
        self.assertIn("approval", result.result["failure"]["message"].lower())
        recovery = json.loads(
            (
                self.board / ".agentbc-control" / self.task_id / "recovery.json"
            ).read_text(encoding="utf-8")
        )
        codes = [entry.get("code") for entry in recovery["history"]]
        self.assertIn("approval_request_duplicate", codes)

    def test_approval_timeout_fails_closed(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame()]
        executor = self._executor(fake, approval_timeout_s=0.3)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"input_required"})
            self.assertEqual(status, "input_required")
            # No decision: the approval wait must expire and fail closed.
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
        self.assertEqual(status, "needs_recovery")
        recovery = json.loads(
            (
                self.board / ".agentbc-control" / self.task_id / "recovery.json"
            ).read_text(encoding="utf-8")
        )
        codes = [entry.get("code") for entry in recovery["history"]]
        self.assertIn("approval_request_expired", codes)

    def test_cancel_during_approval_wait_is_auditable_and_fails_closed(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.permission_frames = [_permission_frame()]
        executor = self._executor(fake, approval_timeout_s=1.0)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"input_required"})
            self.assertEqual(status, "input_required")
            cancelled = executor.cancel(started.run_id)
            self.assertTrue(cancelled.ok)
            status = self._wait_status(executor, started.run_id, {"cancelled", "needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "cancelled")
        self.assertEqual(result.result["failure"]["kind"], "hermes_acp_cancelled")
        cancel_sent = [
            message
            for message in fake.sent
            if message.get("method") == "session/cancel"
        ]
        self.assertEqual(len(cancel_sent), 1)
        self.assertEqual(cancel_sent[0]["params"]["sessionId"], FAKE_SESSION_ID)
        event_types = [event["event_type"] for event in result.result["control_events"]]
        self.assertIn("transport_failed", event_types)

    def test_transport_death_fails_closed(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)
        fake.prompt_raises = HermesAcpError(
            "hermes_acp_transport_exited",
            "Hermes ACP process exited before the frame completed.",
            {"exit_code": 1},
        )
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "needs_recovery")
        self.assertEqual(result.result["failure"]["kind"], "hermes_acp_transport_failed")
        event_types = [event["event_type"] for event in result.result["control_events"]]
        self.assertIn("transport_failed", event_types)
        self.assertIn("session_started", event_types)

    def test_transport_start_failure_returns_failed_start(self) -> None:
        fake = FakeAcpTransport(self.board, self.task_id)

        def boom_start() -> None:
            raise HermesAcpError("hermes_acp_start_failed", "cannot spawn")

        fake.start = boom_start  # type: ignore[method-assign]
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "needs_recovery")
        self.assertIn("hermes_acp_start_failed", result.result["failure"]["message"])


if __name__ == "__main__":
    unittest.main()
