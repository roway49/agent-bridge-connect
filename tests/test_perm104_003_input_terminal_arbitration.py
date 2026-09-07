"""PERM-104-003: a waiting v3 task elevation outranks a terminal Hermes poll.

Regression coverage for Y7SW-001.  The recorded failure timeline is:

* ``2026-09-06T14:49:54.215654Z`` the Hermes ACP adapter persisted one
  waiting v3 task-elevation input for official session
  ``7dc9f492-45b0-4982-bc74-3ec077e064d4`` (native
  ``hermes_acp.session/request_permission``, ``request_id=0``,
  ``tool_use_id=perm-check-1``) with zero notifications and zero human
  decisions.
* ``2026-09-06T14:49:56.117113Z`` the same run was finalized as
  ``completion_marker_missing`` because the worker never arbitrated the
  durable wait against the terminal poll.

These tests reproduce that race deterministically against a real
``TaskStore`` with asynchronous Hermes ACP ordering, and lock the whole
decision lifecycle: exactly one notification, no failure while waiting, the
original run lease closed, no terminal cleanup, one full continuation on
approve, zero continuation on deny, and idempotency across duplicate events
and worker restarts.  A control case proves a genuinely completed Hermes run
without a callback still fails ``completion_marker_missing``.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import (
    DeliveryResult,
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    SessionCleanupResult,
    StartResult,
)
from agent_bridge_connect.cli import command_worker_run
from agent_bridge_connect.execution_policy import build_session_snapshot
from agent_bridge_connect.hermes_acp import HermesAcpElevationRequired
from agent_bridge_connect.notifications import build_input_required_notification
from agent_bridge_connect.permission_elevation import permission_elevation_from_extensions
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.permission_runtime import host_profile_digest, path_plan_digest
from agent_bridge_connect.permission_registry import TRANSPORT_HERMES_ACP
from agent_bridge_connect.runner import RunnerState
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    close_lease,
    create_lease,
    load_lease,
    save_lease,
)
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.task_store import _utc_now

FAKE_SESSION_ID = "acp-session-fake-1"


def _permission_frame(request_id: int = 0, tool_call_id: str = "perm-check-1") -> dict:
    """The exact canonical ACP shape recorded by Y7SW-001."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": FAKE_SESSION_ID,
            "toolCall": {
                "toolCallId": tool_call_id,
                "kind": "execute",
                "title": "run the approved command",
                "status": "pending",
                "content": [{"type": "text", "text": "$ hermes acp --check"}],
                "rawInput": {"command": "hermes acp --check"},
            },
            "options": [
                {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"},
                {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
            ],
        },
    }


class _ElevationThenTerminalTransport:
    """Fake ACP transport that persists one elevation, then dies.

    ``prompt`` raises :class:`HermesAcpElevationRequired` after the adapter
    durably persisted the waiting input - exactly the Y7SW ordering - and the
    injected ``after_elevation`` hook lets a test make the run also look
    terminal (a real transport close, an empty final text or a stopReason)
    before the worker polls again.
    """

    def __init__(self, board: Path, task_id: str, after_elevation=None) -> None:
        self.board = board
        self.task_id = task_id
        self.after_elevation = after_elevation
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.closed = False
        self.session_id = FAKE_SESSION_ID

    def start(self) -> None:
        self.calls.append("start")

    def close(self) -> None:
        self.closed = True
        self.calls.append("close")

    def initialize(self) -> int:
        self.calls.append("initialize")
        return 1

    def stderr_evidence(self) -> str:
        return ""

    def message_text(self) -> str:
        return ""

    def new_session(self, cwd: str) -> str:
        self.calls.append("new_session")
        return FAKE_SESSION_ID

    def load_session(self, cwd: str, session_id: str) -> str:
        self.calls.append("load_session")
        return session_id

    def prompt(self, session_id, blocks, *, on_permission, timeout_s=None, on_progress=None):
        self.calls.append("prompt")
        raise HermesAcpElevationRequired(
            {
                "approval_version": 3,
                "type": "permission",
                "scope": "task_elevation",
                "elevation_mode": "contained_full",
                "requested_permission": "full",
                "session_id": FAKE_SESSION_ID,
                "request_id": "0",
                "request_fingerprint": "fp-" + "a" * 40,
                "tool_use_id": "perm-check-1",
                "operation": "execute",
                "native_event": "hermes_acp.session/request_permission",
                "summary": "Security scan - Pipe to interpreter",
            }
        )


class _TerminalHermesExecutor:
    """Fake Hermes executor whose poll is already terminal.

    This is the worker's view of the Y7SW race: by the time the worker polls,
    the ACP turn has ended and the executor reports a terminal failure even
    though the durable v3 elevation input is waiting.

    ``start`` reproduces the production adapter ordering - register the run,
    persist the official session receipt, then persist the waiting elevation -
    exactly like ``_start_with_acp`` + ``_run_acp_session`` do against the real
    ``TaskStore``.
    """

    def __init__(
        self,
        poll_result: PollResult,
        harness: "_WorkerHarness",
        *,
        persist_elevation: bool = True,
        session_id: str = FAKE_SESSION_ID,
    ) -> None:
        self.poll_result = poll_result
        self.harness = harness
        self.persist_elevation = persist_elevation
        self.session_id = session_id
        self.task_packet: dict | None = None
        self.poll_count = 0
        self.cancelled: list[str] = []
        self.cleaned: list[str] = []

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, message="ready")

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(level=ExecutorLevel.L2, resume=True)

    def start(self, task_packet: dict) -> StartResult:
        self.task_packet = task_packet
        task_id = str(task_packet["task_id"])
        run_id = f"hermes-{task_id}-race"
        service = self.harness.service
        service.record_executor_run_started(task_id, run_id)
        service.record_executor_session_started(
            task_id,
            run_id,
            {
                "version": 1,
                "executor": "hermes",
                "session_id": self.session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "stderr_receipt",
            },
        )
        self.elevation_input = (
            self.harness.persist_waiting_elevation(task_id)
            if self.persist_elevation
            else None
        )
        # The real adapter opens one RunLease for the ACP run; the elevation
        # path closes exactly that lease when the turn ends.
        self.lease = create_lease(task_id, "hermes", 0, str(self.harness.project))
        self.lease.run_id = run_id
        save_lease(self.lease, self.harness.board)
        return StartResult(ok=True, run_id=run_id, message="hermes ACP session started")

    def poll(self, run_id: str) -> PollResult:
        self.poll_count += 1
        if self.poll_count == 1:
            # Terminal poll: the original ACP transport/run lease closes.
            lease = load_lease(self.lease.task_id, self.harness.board)
            if lease is not None and lease.run_id == self.lease.run_id:
                close_lease(lease, self.harness.board)
        return self.poll_result

    def cancel(self, run_id: str):
        self.cancelled.append(run_id)
        from agent_bridge_connect.adapters import AdapterResult

        return AdapterResult(True, "cancelled")

    def cleanup_session(self, request):
        self.cleaned.append(str(getattr(request, "session_id", "")))
        from agent_bridge_connect.adapters import SessionCleanupResult

        return SessionCleanupResult(
            state="completed",
            capability="supported",
            strategy="official_session_delete",
        )


def _terminal_failure_result(session_id: str = FAKE_SESSION_ID) -> PollResult:
    """The poll the Y7SW worker actually saw: no callback, run over."""
    return PollResult(
        status="failed",
        progress={"events_seen": 4},
        result={
            "stdout": "",
            "final_text": "",
            "stderr": "hermes acp transport closed",
            "returncode": 0,
            "stop_reason": "end_turn",
            "marker_valid": False,
            "marker_seen": False,
            "failure": {
                "kind": "completion_marker_missing",
                "layer": "flow_contract",
                "message": "Executor exited without a valid AGENTBC_FINAL_CALLBACK",
                "retryable": False,
            },
            "execution_session": {
                "version": 1,
                "executor": "hermes",
                "session_id": session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "stderr_receipt",
            },
        },
    )


class _WorkerHarness:
    """One real TaskStore plus the pieces needed to drive ``worker run``."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            f'workspace_root = "{self.root / "workspace"}"\n'
            'permission_mode = "safe"\n'
            "[sessions]\nretain_executor_sessions = false\n",
            encoding="utf-8",
        )
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "permission_mode": "safe",
                "sessions": {"retain_executor_sessions": False},
            },
        )
        self.dialog_results: list[DeliveryResult] = []
        self.dialog_calls = 0

    def close(self) -> None:
        self.temporary.cleanup()

    def create_task(self) -> str:
        task = self.service.create_task(
            "Y7SW-001 race reproduction",
            "hermes",
            [{"id": 1, "description": "delete the elevation probe file"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        # The task stays ``pending``; the worker claims it exactly like the
        # Runner does, and the fake adapter persists run/session/elevation
        # inside ``start`` in production order.
        self.run_id = f"hermes-{task.id}-race"
        return task.id

    def elevation_kwargs(self, task_id: str) -> dict:
        """The exact v3 elevation facts the real Hermes adapter persists."""
        task = self.service.get_task(task_id)
        return {
            "executor_run_id": self.run_id,
            "session_id": FAKE_SESSION_ID,
            "request_id": "0",
            "request_fingerprint": "fp-" + "a" * 40,
            "executor": "hermes",
            "operation": "execute",
            "summary": "Security scan - Pipe to interpreter",
            "reason": "Security scan - Pipe to interpreter",
            "reason_detail": "",
            "execution_session": {
                "version": 1,
                "executor": "hermes",
                "session_id": FAKE_SESSION_ID,
                "resumed": False,
                "persistence": "persistent",
                "source": "stderr_receipt",
            },
            "tool_name": "execute",
            "tool_use_id": "perm-check-1",
            "action_fingerprint": "fp-" + "b" * 40,
            "escalation_domain": "hermes_acp",
            "profile_digest": host_profile_digest(),
            "control_path": "hermes-acp",
            "native_event": "hermes_acp.session/request_permission",
            "authority": {
                "executor": "hermes",
                "protocol": "hermes_acp",
                "protocol_version": 1,
                "method": "session/request_permission",
            },
            "path_plan_digest": path_plan_digest(task.workspace or {}),
            "containment_profile_digest": host_profile_digest(),
            "full_preflight": {
                "ok": True,
                "status": "passed",
                "mode": "contained_full",
            },
        }

    def persist_waiting_elevation(self, task_id: str) -> dict:
        """Persist the v3 elevation the real adapter persists, verbatim."""
        kwargs = self.elevation_kwargs(task_id)
        self._last_elevation_kwargs = dict(kwargs)
        return self.service.block_task_for_elevation(task_id, **kwargs)

    def run_worker(self, task_id: str, executor) -> int:
        with (
            mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor),
            mock.patch(
                "agent_bridge_connect.notifiers.dialog.DialogNotifier.send",
                side_effect=self._dialog,
            ),
        ):
            return command_worker_run(
                mock.Mock(
                    root=self.board,
                    executor="hermes",
                    once=True,
                    interval=0.01,
                    config=self.config_path,
                    detach=False,
                    task_id=task_id,
                    runner_authorize=False,
                )
            )

    def _dialog(self, payload, **_kwargs) -> DeliveryResult:
        self.dialog_calls += 1
        self.dialog_results.append(DeliveryResult(True, "shown"))
        return self.dialog_results[-1]

    def notification_events(self, task_id: str) -> list[dict]:
        return [
            event
            for event in self.service.store.read_events(task_id)
            if event.get("event_type") == "notification_delivery"
        ]


class InputTerminalArbitrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _WorkerHarness()
        self.addCleanup(self.harness.close)

    # -- the Y7SW-001 race -------------------------------------------------

    def test_waiting_elevation_outlives_completion_marker_missing(self) -> None:
        """The exact Y7SW-001 race: waiting input beats completion_marker_missing."""
        harness = self.harness
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)

        code = harness.run_worker(task_id, executor)

        # The worker ends successfully instead of failing the task.
        self.assertEqual(code, 0)
        persisted = harness.service.get_task(task_id)
        self.assertEqual(persisted.status, "input_required")
        waiting = persisted.extensions["agentbc.input"]
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["request_id"], "0")
        self.assertEqual(waiting["tool_use_id"], "perm-check-1")
        self.assertEqual(waiting["session_id"], FAKE_SESSION_ID)
        self.assertEqual(waiting["native_event"], "hermes_acp.session/request_permission")

        # Exactly one input-required notification and no terminal one.
        events = harness.notification_events(task_id)
        self.assertEqual(
            [event["notification_event"] for event in events],
            ["task.input_required"],
        )
        self.assertEqual(harness.dialog_calls, 1)
        self.assertFalse(
            any(event["notification_event"] == "task.failed" for event in events)
        )
        self.assertEqual(
            build_input_required_notification(harness.service, task_id)["input_type"],
            "permission",
        )

        # Only stale execution-run pointers are cleared, for the planned
        # continuation; nothing else about the task was degraded.
        execution = persisted.extensions["agentbc.execution"]
        self.assertNotIn("worker_run_id", execution)
        self.assertNotIn("executor_run_id", execution)

        # The failure event the old worker wrote is gone from the timeline.
        event_types = [
            event["event_type"] for event in harness.service.store.read_events(task_id)
        ]
        self.assertNotIn("task.failed", event_types)

    def test_worker_restart_replays_exactly_one_notification(self) -> None:
        """A restart/replay of the same run still delivers one notification."""
        harness = self.harness
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)

        self.assertEqual(harness.run_worker(task_id, executor), 0)
        first_events = harness.notification_events(task_id)
        self.assertEqual(len(first_events), 1)

        # Simulate a Runner restart: a fresh worker process re-runs the same
        # task, re-registers the same run id and polls the same terminal
        # result against the same durable waiting input.
        replay = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        self.assertEqual(harness.run_worker(task_id, replay), 0)
        replay_events = harness.notification_events(task_id)
        self.assertEqual(len(replay_events), 1)
        self.assertEqual(replay_events, first_events)
        self.assertEqual(harness.dialog_calls, 1)
        self.assertEqual(harness.service.get_task(task_id).status, "input_required")

    def test_original_run_lease_is_closed_and_no_terminal_cleanup_runs(self) -> None:
        harness = self.harness
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)

        self.assertEqual(harness.run_worker(task_id, executor), 0)

        # Terminal cleanup must not run while the wait is live.
        self.assertEqual(executor.cleaned, [])
        self.assertEqual(executor.cancelled, [])
        # The task's own RunLease is closed, never left suspended: only the
        # original ACP transport/run lease ends for the planned continuation.
        adapter_lease = load_lease(task_id, harness.board)
        self.assertIsNotNone(adapter_lease)
        self.assertEqual(adapter_lease.state, RunLeaseState.CLOSED)
        self.assertEqual(adapter_lease.run_id, harness.run_id)

    # -- approval cardinality ---------------------------------------------

    def test_approve_dispatches_exactly_one_full_continuation_same_session(self) -> None:
        harness = self.harness
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        self.assertEqual(harness.run_worker(task_id, executor), 0)

        request = harness.service.get_task(task_id).extensions["agentbc.input"]
        answered = harness.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="approve_full",
        )
        self.assertTrue(answered["dispatch_required"])
        self.assertTrue(answered["same_session"])
        self.assertEqual(answered["request_id"], "0")

        persisted = harness.service.get_task(task_id)
        elevation = persisted.extensions["agentbc.permission_elevation"]
        self.assertEqual(elevation["state"]["status"], "approved")
        self.assertEqual(elevation["cardinality"]["human_decisions"], 1)
        self.assertEqual(elevation["cardinality"]["notifications"], 1)
        self.assertEqual(elevation["continuation"]["count"], 0)
        self.assertEqual(elevation["binding"]["session_id"], FAKE_SESSION_ID)
        self.assertEqual(elevation["binding"]["executor_run_id"], harness.run_id)

        # A duplicate or replayed decision is idempotent: it never produces a
        # second input, decision, grant, worker or continuation.
        replayed = harness.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="approve_full",
        )
        self.assertNotEqual(replayed.get("dispatch_required"), True)
        replayed_elevation = harness.service.get_task(task_id).extensions[
            "agentbc.permission_elevation"
        ]
        self.assertEqual(replayed_elevation["cardinality"]["human_decisions"], 1)
        self.assertEqual(replayed_elevation["cardinality"]["notifications"], 1)
        self.assertEqual(replayed_elevation["continuation"]["count"], 0)
        self.assertEqual(replayed_elevation["state"]["status"], "approved")

    def test_deny_creates_zero_continuation_and_terminates(self) -> None:
        harness = self.harness
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        self.assertEqual(harness.run_worker(task_id, executor), 0)

        request = harness.service.get_task(task_id).extensions["agentbc.input"]
        answered = harness.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="deny",
        )
        self.assertFalse(answered["dispatch_required"])
        self.assertTrue(answered["permission_denied"])
        self.assertEqual(harness.service.get_task(task_id).status, "failed")
        elevation = harness.service.get_task(task_id).extensions[
            "agentbc.permission_elevation"
        ]
        self.assertEqual(elevation["continuation"]["count"], 0)

    # -- control case ------------------------------------------------------

    def test_completed_hermes_run_without_callback_still_fails(self) -> None:
        """Control: no waiting input means completion_marker_missing is real."""
        harness = self.harness
        task_id = harness.create_task()
        result = _terminal_failure_result()
        result.result["stop_reason"] = "end_turn"
        result.result["stdout"] = "the task finished without a callback"
        result.result["final_text"] = "the task finished without a callback"
        executor = _TerminalHermesExecutor(result, harness, persist_elevation=False)

        code = harness.run_worker(task_id, executor)

        self.assertEqual(code, 1)
        persisted = harness.service.get_task(task_id)
        self.assertEqual(persisted.status, "failed")
        self.assertEqual(persisted.errors[-1]["code"], "completion_marker_missing")
        events = harness.notification_events(task_id)
        self.assertTrue(
            any(event["notification_event"] == "task.failed" for event in events)
        )

    def test_unrelated_waiting_input_does_not_arbitrate_this_run(self) -> None:
        """A wait bound to another run/session must not mask a real failure."""
        harness = self.harness
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        original_start = _TerminalHermesExecutor.start

        def start_with_foreign_wait(self_executor, task_packet):
            result = original_start(self_executor, task_packet)
            # Rebind the durable wait to a different executor run, which is the
            # only difference from the arbitrated case above.
            persisted = harness.service.get_task(str(task_packet["task_id"]))
            waiting = dict(persisted.extensions["agentbc.input"])
            waiting["executor_run_id"] = "hermes-some-other-run"
            extensions = dict(persisted.extensions)
            extensions["agentbc.input"] = waiting
            persisted.extensions = extensions
            persisted.updated_at = _utc_now()
            harness.service.store.write_task(
                str(task_packet["task_id"]), persisted.to_dict()
            )
            return result

        with mock.patch.object(
            _TerminalHermesExecutor, "start", start_with_foreign_wait
        ):
            self.assertEqual(harness.run_worker(task_id, executor), 1)
        self.assertEqual(harness.service.get_task(task_id).status, "failed")


class _CompletedFullContinuation:
    """One resumed Hermes full turn with a real callback/session receipt."""

    def __init__(self) -> None:
        self.task_packet: dict | None = None
        self.run_id = ""
        self.poll_count = 0

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, message="ready")

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(level=ExecutorLevel.L2, resume=True)

    def start(self, task_packet: dict) -> StartResult:
        from agent_bridge_connect.effective_permissions import resolve_effective_permission
        from agent_bridge_connect.executors.hermes import (
            HermesExecutor,
            _hermes_transport_from_permission,
        )

        self.task_packet = task_packet
        self.run_id = str(task_packet["_agentbc_executor_run_id"])
        permission = resolve_effective_permission(
            task_packet,
            "hermes",
            self.run_id,
            trusted_runner_managed=True,
        )
        if permission["effective_mode"] != "full":
            return StartResult(ok=False, run_id="", message="continuation is not full")
        if _hermes_transport_from_permission(permission) != "direct":
            return StartResult(ok=False, run_id="", message="continuation returned to ACP")
        command = HermesExecutor(command="/bin/echo", transport="acp")._build_command(
            "probe",
            permission=permission,
            task_packet=task_packet,
        )
        if command[:2] != ["/bin/echo", "chat"] or "--yolo" not in command:
            return StartResult(ok=False, run_id="", message="full command is not headless")
        if (
            "--resume" not in command
            or command[command.index("--resume") + 1] != FAKE_SESSION_ID
        ):
            return StartResult(ok=False, run_id="", message="full command lost its session")
        elevation = permission_elevation_from_extensions(
            task_packet["extensions"], task_id=task_packet["task_id"]
        )
        if elevation is None or elevation["state"]["status"] != "active":
            return StartResult(ok=False, run_id="", message="elevation is not active")
        if elevation["continuation"] != {
            "count": 1,
            "executor_run_id": self.run_id,
            "session_id": FAKE_SESSION_ID,
        }:
            return StartResult(ok=False, run_id="", message="continuation binding mismatch")
        return StartResult(ok=True, run_id=self.run_id, message="full continuation started")

    def poll(self, run_id: str) -> PollResult:
        self.poll_count += 1
        if self.task_packet is None:
            raise AssertionError("continuation was not started")
        task_id = str(self.task_packet["task_id"])
        return PollResult(
            status="completed",
            progress={"returncode": 0},
            result={
                "returncode": 0,
                "summary": "full continuation completed",
                "execution_session": {
                    "version": 1,
                    "executor": "hermes",
                    "session_id": FAKE_SESSION_ID,
                    "resumed": True,
                    "persistence": "persistent",
                    "source": "stderr_receipt",
                },
                "agent_callback": {
                    "version": 1,
                    "task_id": task_id,
                    "final_state": "completed",
                    "summary": "full continuation completed",
                    "step_results": [{"id": 1, "status": "done"}],
                },
            },
        )

    def cleanup_session(self, _request) -> SessionCleanupResult:
        return SessionCleanupResult(
            state="completed",
            capability="supported",
            strategy="official_session_delete",
        )


class RunnerFullContinuationTests(unittest.TestCase):
    """Runner consumes Approve and the resumed full worker closes the task."""

    def setUp(self) -> None:
        self.harness = _WorkerHarness()
        self.addCleanup(self.harness.close)

    def test_runner_approve_reaches_verified_full_continuation(self) -> None:
        harness = self.harness
        task_id = harness.create_task()
        first = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        self.assertEqual(harness.run_worker(task_id, first), 0)
        waiting = harness.service.get_task(task_id).extensions["agentbc.input"]

        fake_bin = harness.root / "hermes"
        fake_bin.write_text("fake", encoding="utf-8")
        runner = RunnerState(
            harness.root / "runner-state",
            [harness.root],
            {"hermes": fake_bin},
        )
        spawned = {
            "ok": True,
            "run_id": "runner-worker-continuation",
            "pid": 12345,
            "status": "running",
        }
        with (
            mock.patch.object(runner, "_validate_executor_config"),
            mock.patch.object(runner, "_spawn_process", return_value=spawned) as spawn,
            mock.patch.object(
                runner, "_open_task_monitor", return_value={"status": "disabled"}
            ),
            mock.patch("agent_bridge_connect.runner.assert_executor_permission_supported"),
        ):
            response = runner.respond_and_dispatch(
                {
                    "board_root": str(harness.board),
                    "config_path": "",
                    "task_id": task_id,
                    "input_id": waiting["input_id"],
                    "response_type": "approve_full",
                    "message": "",
                    "interval_s": 0.01,
                }
            )
        self.assertEqual(response["run_id"], "runner-worker-continuation")
        self.assertEqual(spawn.call_count, 1)
        worker_command = spawn.call_args.args[1]
        self.assertIn("--runner-authorize", worker_command)
        self.assertIn("--task-id", worker_command)
        self.assertEqual(worker_command[worker_command.index("--task-id") + 1], task_id)

        continuation = _CompletedFullContinuation()
        with (
            mock.patch("agent_bridge_connect.cli.get_executor", return_value=continuation),
            mock.patch("agent_bridge_connect.cli._notify_terminal"),
            mock.patch("agent_bridge_connect.cli._request_task_list_refresh_for_service"),
        ):
            code = command_worker_run(
                mock.Mock(
                    root=harness.board,
                    executor="hermes",
                    once=True,
                    interval=0.01,
                    config=None,
                    detach=False,
                    task_id=task_id,
                    runner_authorize=True,
                )
            )
        self.assertEqual(code, 0)
        self.assertEqual(continuation.poll_count, 1)

        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "completed")
        elevation = permission_elevation_from_extensions(
            task.extensions, task_id=task_id
        )
        self.assertIsNotNone(elevation)
        self.assertEqual(elevation["state"]["status"], "verified")
        self.assertEqual(elevation["cardinality"]["permission_requests"], 1)
        self.assertEqual(elevation["cardinality"]["notifications"], 1)
        self.assertEqual(elevation["cardinality"]["human_decisions"], 1)
        self.assertEqual(elevation["cardinality"]["full_continuations"], 1)
        self.assertEqual(elevation["continuation"]["session_id"], FAKE_SESSION_ID)
        approval = task.extensions["agentbc.approval"]
        self.assertEqual(approval["cardinality"]["full_continuations"], 1)
        self.assertEqual(task.extensions["agentbc.session"]["session_id"], FAKE_SESSION_ID)
        self.assertTrue(task.extensions["agentbc.final_callback"]["marker_valid"])


class ElevationLatchTests(unittest.TestCase):
    """The adapter-side latch: one publication, immutable afterwards."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.board.mkdir()
        self.task_id = "HERMES-ACP-LATCH"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": True},
            },
        )
        task = self.service.create_task(
            "Hermes ACP latch",
            "hermes",
            [{"id": 1, "description": "one contained-full elevation"}],
            customer_dir=True,
            customer_path=self.root,
            permission_mode="safe",
        )
        self.service.start_task_run(task.id, "hermes")
        self.packet = self.service.store.read_task(task.id)
        self.packet["task_id"] = task.id
        self.packet["task_board"] = {"root": str(self.board)}
        self.packet["runner_authorization_required"] = True
        self.packet["_agentbc_executor_run_id"] = f"hermes-{task.id}-initial"

    def _packet_with_v3(self) -> dict:
        session = build_session_snapshot(
            "hermes",
            retain=False,
            session_id="",
            session_state="pending",
            run_ids=[],
        )
        packet = dict(self.packet)
        packet["extensions"] = {
            "agentbc.permission": build_permission_record(explicit_mode="safe"),
            "agentbc.session": session,
        }
        return packet

    def _executor(self, fake) -> object:
        from agent_bridge_connect.executors.hermes import HermesExecutor

        executor = HermesExecutor(
            command=sys.executable,
            transport="acp",
            approval_timeout_s=2.0,
        )
        executor._acp_transport_override = fake
        return executor

    def _wait_status(self, executor, run_id: str, statuses: set[str], timeout_s: float = 5.0) -> str:
        deadline = time.monotonic() + timeout_s
        status = ""
        while time.monotonic() < deadline:
            status = executor.poll(run_id).status
            if status in statuses:
                return status
            time.sleep(0.01)
        return status

    def test_duplicate_polls_return_the_identical_latched_input_required(self) -> None:
        fake = _ElevationThenTerminalTransport(self.board, self.packet["task_id"])
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_close_run_lease") as close_lease,
            mock.patch.object(
                executor._runner_client, "authorize_command", return_value={"ok": True}
            ),
            mock.patch(
                "agent_bridge_connect.executors.hermes.assert_executor_permission_supported"
            ),
        ):
            started = executor.start(self._packet_with_v3())
            status = self._wait_status(executor, started.run_id, {"input_required"})

        self.assertEqual(status, "input_required")
        self.assertTrue(close_lease.called)
        self.assertTrue(fake.closed)

        first = executor.poll(started.run_id)
        self.assertEqual(first.status, "input_required")
        self.assertEqual(first.progress.get("elevation_state"), "suspended_for_elevation")
        self.assertEqual(first.result["request_id"], "0")
        self.assertEqual(first.result["session_id"], FAKE_SESSION_ID)
        self.assertEqual(first.result["tool_use_id"], "perm-check-1")
        self.assertEqual(
            first.result["execution_session"]["session_id"], FAKE_SESSION_ID
        )

        # The latched publication is stable across any number of polls.
        for _ in range(5):
            again = executor.poll(started.run_id)
            self.assertEqual(again.status, "input_required")
            self.assertEqual(again.result, first.result)
            self.assertEqual(again.progress, first.progress)

    def test_latch_survives_transport_close_and_late_stop_reason(self) -> None:
        fake = _ElevationThenTerminalTransport(self.board, self.packet["task_id"])
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_close_run_lease"),
            mock.patch.object(
                executor._runner_client, "authorize_command", return_value={"ok": True}
            ),
            mock.patch(
                "agent_bridge_connect.executors.hermes.assert_executor_permission_supported"
            ),
        ):
            started = executor.start(self._packet_with_v3())
            self._wait_status(executor, started.run_id, {"input_required"})

        latched = executor.poll(started.run_id)

        # A late client-side stopReason/empty final text publication and a
        # transport close must not overwrite the latched input_required.
        executor._set_acp_run_status(
            started.run_id,
            "completed",
            progress={"stop_reason": "end_turn"},
            result={"stdout": "", "final_text": "", "marker_valid": False},
            record=executor._acp_runs.get(started.run_id),
        )
        executor._acp_runs.pop(started.run_id, None)
        after = executor.poll(started.run_id)
        self.assertEqual(after.status, "input_required")
        self.assertEqual(after.result, latched.result)

    def test_task_elevation_request_is_never_answered_on_the_native_stream(self) -> None:
        fake = _ElevationThenTerminalTransport(self.board, self.packet["task_id"])
        executor = self._executor(fake)
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_close_run_lease"),
            mock.patch.object(
                executor._runner_client, "authorize_command", return_value={"ok": True}
            ),
            mock.patch(
                "agent_bridge_connect.executors.hermes.assert_executor_permission_supported"
            ),
        ):
            started = executor.start(self._packet_with_v3())
            self._wait_status(executor, started.run_id, {"input_required"})

        # No allow_once/grant/permission response of any kind was minted, and
        # the original ACP transport closed exactly once.
        self.assertFalse(
            [
                message
                for message in fake.sent
                if message.get("method") == "permission_response"
            ]
        )
        self.assertEqual(fake.calls.count("close"), 1)
        self.assertIn("input_required", executor.poll(started.run_id).status)


class FullContinuationTransportTests(unittest.TestCase):
    """The approved continuation uses the same session, never ACP again."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_full_mode_selects_the_noninteractive_headless_transport(self) -> None:
        from agent_bridge_connect.executors.hermes import _hermes_transport_from_permission

        # Concrete full is Hermes' separate headless ``chat --yolo`` transport;
        # it can never come back through the ACP permission channel.
        self.assertEqual(
            _hermes_transport_from_permission({"effective_mode": "full"}), "direct"
        )
        # inherit/safe keep native ACP as the interactive permission path.
        self.assertEqual(
            _hermes_transport_from_permission({"effective_mode": "inherit"}),
            TRANSPORT_HERMES_ACP,
        )
        self.assertEqual(
            _hermes_transport_from_permission({"effective_mode": "safe"}),
            TRANSPORT_HERMES_ACP,
        )
        self.assertEqual(
            _hermes_transport_from_permission({}), TRANSPORT_HERMES_ACP
        )

    def test_approved_elevation_binds_the_same_official_session_and_run(self) -> None:
        """Approve consumes the one bound input and resumes the same session."""
        harness = _WorkerHarness()
        self.addCleanup(harness.close)
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        self.assertEqual(harness.run_worker(task_id, executor), 0)

        request = harness.service.get_task(task_id).extensions["agentbc.input"]
        answered = harness.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="approve_full",
        )
        self.assertTrue(answered["dispatch_required"])
        self.assertTrue(answered["same_session"])

        persisted = harness.service.get_task(task_id)
        session = persisted.extensions["agentbc.session"]
        self.assertEqual(session["session_id"], FAKE_SESSION_ID)
        self.assertEqual(session["session_state"], "input_required")

        # The approved elevation is bound to the exact official session and to
        # the run that will become the one full continuation.
        elevation = persisted.extensions["agentbc.permission_elevation"]
        self.assertEqual(elevation["binding"]["session_id"], FAKE_SESSION_ID)
        self.assertEqual(elevation["binding"]["executor_run_id"], harness.run_id)
        self.assertEqual(
            elevation["authority"]["method"], "session/request_permission"
        )
        self.assertEqual(elevation["cardinality"]["full_continuations"], 0)
        # v3 never mints a legacy permission grant for the approval.
        self.assertIsNone(persisted.extensions.get("agentbc.permission_grant"))

    def test_duplicate_native_event_cannot_create_a_second_waiting_input(self) -> None:
        """A duplicate/late permission event is idempotent at the store."""
        harness = _WorkerHarness()
        self.addCleanup(harness.close)
        task_id = harness.create_task()
        executor = _TerminalHermesExecutor(_terminal_failure_result(), harness)
        self.assertEqual(harness.run_worker(task_id, executor), 0)

        first = harness.service.get_task(task_id).extensions["agentbc.input"]
        replayed = harness.persist_waiting_elevation(task_id)
        self.assertTrue(replayed["idempotent"])

        persisted = harness.service.get_task(task_id)
        second = persisted.extensions["agentbc.input"]
        self.assertEqual(second["input_id"], first["input_id"])
        self.assertEqual(second["status"], "waiting")
        elevation = persisted.extensions["agentbc.permission_elevation"]
        self.assertEqual(elevation["cardinality"]["permission_requests"], 1)
        self.assertEqual(elevation["cardinality"]["elevation_receipts"], 1)

        # A replay that changes the native fingerprint fails closed instead of
        # rebinding the same wait to a different request.
        task = harness.service.get_task(task_id)
        kwargs = dict(self._elevation_kwargs(harness, task))
        kwargs["request_fingerprint"] = "fp-" + "f" * 40
        with self.assertRaises(Exception):
            harness.service.block_task_for_elevation(task_id, **kwargs)

        # A different request id while one is already waiting is refused too.
        kwargs = dict(self._elevation_kwargs(harness, task))
        kwargs["request_id"] = "1"
        with self.assertRaises(Exception):
            harness.service.block_task_for_elevation(task_id, **kwargs)

    @staticmethod
    def _elevation_kwargs(harness: "_WorkerHarness", task) -> dict:
        return dict(harness._last_elevation_kwargs, task_id=task.id)


if __name__ == "__main__":
    unittest.main()
