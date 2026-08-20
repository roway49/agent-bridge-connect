from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.control import (
    APPROVAL_DECISIONS,
    ApprovalControlPlane,
    ControlPlaneError,
    STABLE_EVENTS,
    approval_response_payload,
)
from agent_bridge_connect.execution_policy import build_session_snapshot
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.run_lease import RunLeaseState, load_lease
from agent_bridge_connect.runner import RunnerState
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.session import (
    SessionFirstGate,
    SessionRecoveryRequired,
    control_root_for_task,
)


SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "executor_runtime" / "codex_app_server_protocol.v2.schema.json"


def _app_server_capability_override() -> dict:
    """A pre-verified App Server capability report for fake-transport tests."""
    return {
        "ok": True,
        "transport": "app-server",
        "protocol_version": 2,
        "version": "0.146.0",
        "version_parsed": [0, 146, 0],
        "schema_missing": [],
        "evidence": ["version_gate", "schema_methods_verified"],
        "schema_summary": "CodexAppServerProtocol",
    }


class BlockingFakeTransport:
    def __init__(self, root: Path, task_id: str) -> None:
        self.root = root
        self.task_id = task_id
        self.sent: list[dict] = []
        self.queue: list[dict] = []
        self.condition = threading.Condition()
        self.closed = False
        self.receipt_before_turn = False

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
                    (self.root / ".agentbc-control" / self.task_id / "session_receipt.json").is_file()
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


class CodexControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task_id = "CDEX-CTRL-001"
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

    def test_session_first_gate_rejects_missing_duplicate_and_mismatch(self) -> None:
        root = control_root_for_task(self.task_id, board_root=self.board)
        gate = SessionFirstGate(
            root,
            task_id=self.task_id,
            executor="codex",
            executor_run_id="codex-run-1",
        )
        with self.assertRaisesRegex(SessionRecoveryRequired, "not persisted"):
            gate.require_before_turn("thread-fake-1")
        gate.persist_official_receipt(self.receipt)
        self.assertEqual(gate.require_before_turn("thread-fake-1")["session_id"], "thread-fake-1")
        with self.assertRaisesRegex(SessionRecoveryRequired, "already exists"):
            gate.persist_official_receipt(self.receipt)
        resume_gate = SessionFirstGate(
            root,
            task_id=self.task_id,
            executor="codex",
            executor_run_id="codex-run-2",
            expected_session_id="thread-fake-1",
        )
        resumed = dict(self.receipt, resumed=True)
        resume_gate.persist_official_receipt(resumed)
        with self.assertRaisesRegex(SessionRecoveryRequired, "does not match"):
            SessionFirstGate(
                root,
                task_id=self.task_id,
                executor="codex",
                executor_run_id="codex-run-3",
                expected_session_id="thread-other",
            ).persist_official_receipt(dict(self.receipt, resumed=True))

    def test_control_plane_fail_closed_identity_concurrency_expiry_and_crash(self) -> None:
        root = control_root_for_task(self.task_id, board_root=self.board)
        plane = ApprovalControlPlane(
            root,
            task_id=self.task_id,
            executor_run_id="codex-run-1",
            session_id="thread-fake-1",
            approval_timeout_s=0.05,
        )
        plane.record_session_started(self.receipt)
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "thread-fake-1",
                "turnId": "turn-1",
                "permissions": {"fileSystem": "workspace-write"},
            },
        }
        plane.request_approval(request)
        with self.assertRaisesRegex(ControlPlaneError, "second approval"):
            plane.request_approval(dict(request, id=8))
        with self.assertRaisesRegex(ControlPlaneError, "identity"):
            plane.respond_approval(self.task_id, "codex-other", "thread-fake-1", "7", "accept")

        for suffix, bad_run, bad_session in (
            ("RUN", "codex-other", "thread-fake-1"),
            ("SESSION", "codex-run-1", "thread-other"),
        ):
            cross_task = f"CDEX-CROSS-{suffix}-001"
            cross_root = control_root_for_task(cross_task, board_root=self.board)
            cross_plane = ApprovalControlPlane(
                cross_root,
                task_id=cross_task,
                executor_run_id="codex-run-1",
                session_id="thread-fake-1",
            )
            cross_plane.record_session_started(dict(self.receipt))
            cross_plane.request_approval(
                dict(request, params={"threadId": "thread-fake-1", "turnId": "turn-cross"})
            )
            with self.assertRaises(ControlPlaneError):
                ApprovalControlPlane(
                    cross_root,
                    task_id=cross_task,
                    executor_run_id=bad_run,
                    session_id=bad_session,
                    create=False,
                ).respond_approval(cross_task, bad_run, bad_session, "7", "accept")

        duplicate_task = "CDEX-DUPLICATE-001"
        duplicate_root = control_root_for_task(duplicate_task, board_root=self.board)
        duplicate_plane = ApprovalControlPlane(
            duplicate_root,
            task_id=duplicate_task,
            executor_run_id="codex-run-duplicate",
            session_id="thread-duplicate",
        )
        duplicate_plane.record_session_started(dict(self.receipt, session_id="thread-duplicate"))
        duplicate_plane.request_approval(
            dict(request, params={"threadId": "thread-duplicate", "turnId": "turn-duplicate"})
        )
        duplicate_plane.respond_approval(
            duplicate_task, "codex-run-duplicate", "thread-duplicate", "7", "accept"
        )
        with self.assertRaises(ControlPlaneError):
            duplicate_plane.request_approval(
                dict(request, params={"threadId": "thread-duplicate", "turnId": "turn-duplicate"})
            )
        with self.assertRaisesRegex(ControlPlaneError, "recovery|timed out|expired|no longer"):
            plane.wait_for_decision("7", 0.06)
        with self.assertRaisesRegex(ControlPlaneError, "recovery|expired|no longer current"):
            plane.respond_approval(self.task_id, "codex-run-1", "thread-fake-1", "7", "accept")
        recovery = json.loads((root / "recovery.json").read_text(encoding="utf-8"))
        self.assertTrue(recovery["history"])

        crash_root = control_root_for_task("CDEX-CRASH-001", board_root=self.board)
        crash_plane = ApprovalControlPlane(
            crash_root,
            task_id="CDEX-CRASH-001",
            executor_run_id="codex-run-crash",
            session_id="thread-crash",
        )
        crash_plane.record_session_started(dict(self.receipt, session_id="thread-crash"))
        crash_plane.request_approval(
            dict(request, params={"threadId": "thread-crash", "turnId": "turn-crash"})
        )
        crash_plane.invalidate_request("7", "stdio transport died", evidence={"phase": "approval_wait"})
        with self.assertRaises(ControlPlaneError):
            crash_plane.respond_approval("CDEX-CRASH-001", "codex-run-crash", "thread-crash", "7", "decline")
        event_types = [event["event_type"] for event in crash_plane.events()]
        self.assertIn("transport_failed", event_types)

    def test_response_payload_is_single_action_only(self) -> None:
        request = {
            "operation": "permissions",
            "requested_permissions": {"fileSystem": "workspace-write"},
        }
        accepted = approval_response_payload(request, "accept")
        declined = approval_response_payload(request, "decline")
        self.assertEqual(accepted["scope"], "turn")
        self.assertEqual(accepted["permissions"], request["requested_permissions"])
        self.assertEqual(declined["permissions"], {})
        self.assertNotIn("acceptForSession", json.dumps(accepted))
        self.assertEqual(APPROVAL_DECISIONS, {"accept", "decline"})
        self.assertEqual(
            STABLE_EVENTS,
            {"session_started", "approval_requested", "turn_completed", "transport_failed"},
        )

    def test_protocol_fixture_records_generated_schema_surface(self) -> None:
        schema = json.loads(SCHEMA_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(schema["generated_from"], "codex app-server generate-json-schema --experimental")
        self.assertIn("thread/start", schema["client_methods"])
        self.assertIn("item/permissions/requestApproval", schema["server_request_methods"])
        self.assertEqual(schema["agentbc_constraints"]["approval_decisions"], ["accept", "decline"])

    def _packet(self, *, resumed: bool = False) -> dict:
        session = build_session_snapshot(
            "codex",
            retain=False,
            session_id="thread-fake-1" if resumed else "",
            session_state="active" if resumed else "pending",
            run_ids=["prior-run"] if resumed else [],
        )
        return {
            "task_id": self.task_id,
            "assignee": "codex",
            "title": "control plane",
            "steps": [{"id": 1, "description": "exercise approval"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.board)},
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                "agentbc.session": session,
            },
        }

    def test_codex_app_server_orders_receipt_before_turn_and_resumes_lease(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = CodexExecutor(
            command=sys.executable,
            transport="app-server",
            transport_factory=lambda **_: fake,
            approval_timeout_s=2,
        )
        executor._app_server_capability_override = _app_server_capability_override()
        packet = self._packet()
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(packet)
            self.assertTrue(started.ok)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and executor.poll(started.run_id).status != "input_required":
                time.sleep(0.01)
            waiting = executor.poll(started.run_id)
            self.assertEqual(waiting.status, "input_required")
            approval = waiting.result["approval_request"]
            plane = ApprovalControlPlane(
                control_root_for_task(self.task_id, board_root=self.board),
                task_id=self.task_id,
                executor_run_id=started.run_id,
                session_id="thread-fake-1",
                create=False,
            )
            response = plane.respond_approval(
                self.task_id,
                started.run_id,
                "thread-fake-1",
                approval["request_id"],
                "accept",
            )
            self.assertEqual(response["decision"], "accept")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and executor.poll(started.run_id).status not in {"completed", "needs_recovery", "failed"}:
                time.sleep(0.01)
            result = executor.poll(started.run_id)
        self.assertEqual(result.status, "completed")
        self.assertTrue(fake.receipt_before_turn)
        methods = [message.get("method") for message in fake.sent if message.get("method")]
        self.assertLess(methods.index("thread/start"), methods.index("turn/start"))
        control_events = result.result["control_events"]
        self.assertEqual(
            [event["event_type"] for event in control_events],
            ["session_started", "approval_requested", "turn_completed"],
        )
        self.assertNotIn("acceptForSession", json.dumps(fake.sent))

    def test_codex_app_server_resume_uses_only_explicit_thread_id(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = CodexExecutor(
            command=sys.executable,
            transport="app-server",
            transport_factory=lambda **_: fake,
            approval_timeout_s=2,
        )
        executor._app_server_capability_override = _app_server_capability_override()
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet(resumed=True))
            self.assertTrue(started.ok)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and executor.poll(started.run_id).status != "input_required":
                time.sleep(0.01)
            waiting = executor.poll(started.run_id)
            self.assertEqual(waiting.status, "input_required")
            request = waiting.result["approval_request"]
            plane = ApprovalControlPlane(
                control_root_for_task(self.task_id, board_root=self.board),
                task_id=self.task_id,
                executor_run_id=started.run_id,
                session_id="thread-fake-1",
                create=False,
            )
            plane.respond_approval(
                self.task_id,
                started.run_id,
                "thread-fake-1",
                request["request_id"],
                "decline",
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and executor.poll(started.run_id).status not in {"completed", "needs_recovery", "failed"}:
                time.sleep(0.01)
            result = executor.poll(started.run_id)
        self.assertEqual(result.status, "completed")
        methods = [message.get("method") for message in fake.sent if message.get("method")]
        self.assertIn("thread/resume", methods)
        self.assertNotIn("thread/start", methods)
        resume_message = next(message for message in fake.sent if message.get("method") == "thread/resume")
        self.assertEqual(resume_message["params"]["threadId"], "thread-fake-1")
        self.assertNotIn("--last", json.dumps(fake.sent))
        self.assertNotIn("--continue", json.dumps(fake.sent))

    def test_transport_death_during_approval_invalidates_old_request(self) -> None:
        fake = BlockingFakeTransport(self.board, self.task_id)
        executor = CodexExecutor(
            command=sys.executable,
            transport="app-server",
            transport_factory=lambda **_: fake,
            approval_timeout_s=3,
        )
        executor._app_server_capability_override = _app_server_capability_override()
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and executor.poll(started.run_id).status != "input_required":
                time.sleep(0.01)
            waiting = executor.poll(started.run_id)
            self.assertEqual(waiting.status, "input_required")
            request_id = waiting.result["approval_request"]["request_id"]
            fake.close()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and executor.poll(started.run_id).status != "needs_recovery":
                time.sleep(0.01)
            result = executor.poll(started.run_id)
            plane = ApprovalControlPlane(
                control_root_for_task(self.task_id, board_root=self.board),
                task_id=self.task_id,
                executor_run_id=started.run_id,
                session_id="thread-fake-1",
                create=False,
            )
            with self.assertRaises(ControlPlaneError):
                plane.respond_approval(
                    self.task_id,
                    started.run_id,
                    "thread-fake-1",
                    request_id,
                    "accept",
                )
        self.assertEqual(result.status, "needs_recovery")
        self.assertIn("transport_failed", [event["event_type"] for event in result.result["control_events"]])

    def test_run_lease_transitions_active_suspended_active(self) -> None:
        service = TaskService(self.board, config={"workspace_root": str(self.root)})
        task = service.create_task(
            "lease control",
            "codex",
            [{"id": 1, "description": "lease"}],
            customer_dir=True,
            customer_path=self.root,
            permission_mode="safe",
        )
        packet = task.to_dict()
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(self.board)}
        executor = CodexExecutor(command=sys.executable)
        run_id = f"codex-{task.id}-lease"
        executor._start_run_lease(packet, run_id, "codex")
        self.assertEqual(executor._run_leases[run_id].state, RunLeaseState.ACTIVE)
        executor._suspend_run(run_id)
        self.assertEqual(load_lease(task.id, self.board).state, RunLeaseState.SUSPENDED)
        executor._resume_run(run_id)
        self.assertEqual(load_lease(task.id, self.board).state, RunLeaseState.ACTIVE)
        executor._close_run_lease(run_id)

    def test_runner_control_ipc_binds_the_full_identity_tuple(self) -> None:
        root = control_root_for_task("CDEX-IPC-001", board_root=self.board)
        plane = ApprovalControlPlane(
            root,
            task_id="CDEX-IPC-001",
            executor_run_id="codex-ipc-run",
            session_id="thread-ipc",
        )
        plane.record_session_started(dict(self.receipt, session_id="thread-ipc"))
        plane.request_approval(
            {
                "jsonrpc": "2.0",
                "id": 33,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": "thread-ipc", "turnId": "turn-ipc"},
            }
        )
        state = RunnerState(self.root / "runner", [self.root], {})
        response = state.respond_approval(
            {
                "task_id": "CDEX-IPC-001",
                "executor_run_id": "codex-ipc-run",
                "session_id": "thread-ipc",
                "request_id": "33",
                "decision": "decline",
                "control_root": str(root),
            }
        )
        self.assertEqual(response["decision"], "decline")
        status = state.control_status(
            {
                "task_id": "CDEX-IPC-001",
                "executor_run_id": "codex-ipc-run",
                "session_id": "thread-ipc",
                "control_root": str(root),
            }
        )
        self.assertEqual(status["control"]["status"], "approval_responded")

    def test_explicit_control_root_requires_exact_task_scope(self) -> None:
        task_id = "CDEX-ROOT-001"
        valid = control_root_for_task(task_id, board_root=self.board)
        self.assertEqual(
            control_root_for_task(task_id, explicit_root=valid),
            valid,
        )
        with self.assertRaisesRegex(ValueError, "not task scoped"):
            control_root_for_task(
                task_id,
                explicit_root=valid.parent / "CDEX-OTHER-001",
            )
        with self.assertRaisesRegex(ValueError, "not task scoped"):
            control_root_for_task(
                task_id,
                explicit_root=valid.parent.parent / task_id,
            )


if __name__ == "__main__":
    unittest.main()
