from __future__ import annotations
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.adapters import ExecutorCapabilities, ExecutorLevel, PollResult, ProbeResult, StartResult
from agent_bridge_connect.cli import command_task_progress, command_worker_run
from agent_bridge_connect.execution_policy import SESSION_RECEIPT_SOURCES
from agent_bridge_connect.notifications import build_notification_payload
from agent_bridge_connect.progress_receipts import PROGRESS_EXTENSION_KEY, progress_public_projection, validate_progress_record
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, _finalize_steps, task_to_status
import copy as _terminal_fault_copy
import json as _terminal_fault_json
import os as _terminal_fault_os
import tempfile as _terminal_fault_tempfile
import unittest as _terminal_fault_unittest
from datetime import datetime as _terminal_fault_datetime, timedelta as _terminal_fault_timedelta
from pathlib import Path as _terminal_fault_Path
from unittest import mock as _terminal_fault_mock
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY as _terminal_fault_SESSION_EXTENSION_KEY
from agent_bridge_connect.permission_elevation import PERMISSION_ELEVATION_EXTENSION_KEY as _terminal_fault_PERMISSION_ELEVATION_EXTENSION_KEY
from agent_bridge_connect.record_management import MAX_TASK_RECORD_BYTES as _terminal_fault_MAX_TASK_RECORD_BYTES, task_record_size as _terminal_fault_task_record_size
from agent_bridge_connect.run_lease import RunLeaseState as _terminal_fault_RunLeaseState, create_lease as _terminal_fault_create_lease, save_lease as _terminal_fault_save_lease
from agent_bridge_connect.service import TaskService as _terminal_fault_TaskService
from agent_bridge_connect.terminal_delivery import (
    DELIVERY_EVENTS_FILE as _terminal_fault_DELIVERY_EVENTS_FILE,
    TERMINAL_DELIVERY_EXTENSION_KEY as _terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY,
    StageOutcome as _terminal_fault_StageOutcome,
    build_terminal_delivery_receipt as _terminal_fault_build_terminal_delivery_receipt,
    delivery_health_view as _terminal_fault_delivery_health_view,
    read_terminal_delivery_receipt as _terminal_fault_read_terminal_delivery_receipt,
    transition_terminal_delivery_stage as _terminal_fault_transition_terminal_delivery_stage,
)
from agent_bridge_connect.terminal_delivery_coordinator import TerminalDeliveryCoordinator as _terminal_fault_TerminalDeliveryCoordinator


class Flow103ProgressReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "full"})

    def _running(self, executor: str = "codex"):
        task = self.service.create_task("FLOW-103-001 receipt canary", executor, [{"id": 1, "description": "complete first step"}, {"id": 2, "description": "complete second step"}], customer_dir=True, customer_path=self.project, permission_mode="full")
        self.service.start_task_run(task.id, executor)
        run_id = f"{executor}-flow103-{task.id}"
        self.service.record_executor_run_started(task.id, run_id)
        current = self.service.get_task(task.id)
        session_id = str(current.extensions["agentbc.session"].get("session_id") or "")
        session_id = session_id or f"{executor}-official-{task.id}"
        self.service.record_executor_session_started(task.id, run_id, {"version": 1, "executor": executor, "session_id": session_id, "resumed": False, "persistence": "persistent", "source": SESSION_RECEIPT_SOURCES[executor]})
        self.service.update_execution_metadata(task.id, {"executor_run_id": run_id})
        return (self.service.get_task(task.id), run_id, session_id)

    def test_live_cli_binding_accepts_progress_and_rejects_session_drift(self) -> None:
        from agent_bridge_connect.executors.base import CLIExecutorBase
        for executor in ("codex", "claude"):
            with self.subTest(executor=executor):
                task = self.service.create_task("live binding", executor, [{"id": 1, "description": "write"}], customer_path=self.project, permission_mode="full")
                self.service.start_task_run(task.id, executor)
                run_id = executor + "-live"
                task = self.service.get_task(task.id)
                session_id = task.extensions["agentbc.session"].get("session_id") or "official-codex-live"
                receipt = {"version": 1, "executor": executor, "session_id": session_id, "resumed": False, "persistence": "persistent", "source": SESSION_RECEIPT_SOURCES[executor]}
                packet = {"task_id": task.id, "task_board": {"root": str(self.board)}}
                adapter = mock.Mock()
                CLIExecutorBase._bind_live_session(adapter, packet, run_id, receipt)
                self.assertEqual(self.service.record_step_progress(task.id, 1)["latest_sequence"], 1)
                with self.assertRaises(ABCError):
                    CLIExecutorBase._bind_live_session(adapter, packet, run_id, dict(receipt, session_id="wrong-session"))

    def test_managed_full_adapters_bind_before_in_turn_progress(self) -> None:
        import json
        import sys
        import subprocess
        from agent_bridge_connect.executors.codex import CodexExecutor
        from agent_bridge_connect.executors.claude import ClaudeExecutor
        for name, adapter_type in (("codex", CodexExecutor), ("claude", ClaudeExecutor)):
            with self.subTest(executor=name):
                task = self.service.create_task("full progress", name, [{"id": 1, "description": "write"}], customer_path=self.project, permission_mode="full")
                self.service.start_task_run(task.id, name)
                task = self.service.get_task(task.id)
                packet = {"task_id": task.id, "title": task.title, "assignee": name, "steps": task.steps, "workspace": dict(task.workspace, root=str(self.project), executor_project_root=task.extensions["agentbc.session"].get("project_path", "")), "task_board": {"root": str(self.board)}, "extensions": task.extensions, "runner_authorization_required": True}
                seen = []
                def execute(command, **kwargs):
                    if name == "codex":
                        kwargs["on_line"](json.dumps({"type": "thread.started", "thread_id": "official-full"}) + "\n")
                    seen.append(self.service.record_step_progress(task.id, 1))
                    return subprocess.CompletedProcess(command, 0, "", "")
                target = "agent_bridge_connect.executors.base.run_with_stdout_receipts" if name == "codex" else "agent_bridge_connect.executors.claude.subprocess.run"
                with mock.patch("agent_bridge_connect.executors." + name + ".RunnerClient"), mock.patch(target, side_effect=execute):
                    adapter = adapter_type(command=sys.executable)
                    result = adapter.start(packet)
                self.assertTrue(result.ok, result.message)
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0]["latest_sequence"], 1)

    def test_streaming_receipt_runs_before_child_exit_and_drains_stderr(self) -> None:
        import sys
        from agent_bridge_connect.executors.base import run_with_stdout_receipts
        marker = self.root / "bound"
        script = "import sys,time,pathlib; print('receipt',flush=True); p=pathlib.Path(sys.argv[1]); end=time.monotonic()+3\nwhile not p.exists() and time.monotonic()<end: time.sleep(.01)\nassert p.exists(); sys.stderr.write('x'*100000); print(sys.stdin.read())"
        result = run_with_stdout_receipts([sys.executable, "-c", script, str(marker)], cwd=self.root, input="prompt", timeout=5, on_line=lambda line: marker.touch() if line.strip() == "receipt" else None)
        self.assertEqual(result.returncode, 0)
        self.assertIn("prompt", result.stdout)
        self.assertEqual(len(result.stderr), 100000)

    def test_streaming_timeout_and_binding_failure_stop_child(self) -> None:
        import sys
        import subprocess
        from agent_bridge_connect.executors.base import run_with_stdout_receipts
        command = [sys.executable, "-c", "import time; print('receipt',flush=True); time.sleep(30)"]
        with self.assertRaises(subprocess.TimeoutExpired):
            run_with_stdout_receipts(command, cwd=self.root, input="", timeout=.1, on_line=lambda line: None)
        def reject(line):
            raise ValueError("invalid receipt")
        with self.assertRaisesRegex(ValueError, "invalid receipt"):
            run_with_stdout_receipts(command, cwd=self.root, input="", timeout=3, on_line=reject)

    def test_three_executors_record_bounded_monotonic_done_receipts(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                (task, run_id, session_id) = self._running(executor)
                first = self.service.record_step_progress(task.id, 1)
                reloaded_service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "full"})
                replay = reloaded_service.record_step_progress(task.id, 1)
                second = self.service.record_step_progress(task.id, 2)
                self.assertEqual(first["latest_sequence"], 1)
                self.assertFalse(first["replayed"])
                self.assertEqual(replay["latest_sequence"], 1)
                self.assertTrue(replay["replayed"])
                self.assertEqual(second["latest_sequence"], 2)
                stored = self.service.get_task(task.id)
                record = stored.extensions[PROGRESS_EXTENSION_KEY]
                self.assertEqual(len(record["receipts"]), 2)
                self.assertEqual(record["receipts"][0]["binding"]["executor_run_id"], run_id)
                self.assertEqual(record["receipts"][0]["binding"]["session_id"], session_id)
                self.assertEqual([step["status"] for step in stored.steps], ["done", "done"])
                self.assertEqual(stored.status, "running")

    def test_unknown_step_unbound_session_and_run_drift_fail_closed(self) -> None:
        (task, _, _) = self._running()
        with self.assertRaisesRegex(ABCError, "Unknown declared step"):
            self.service.record_step_progress(task.id, 99)
        current = self.service.get_task(task.id)
        current.extensions["agentbc.execution"]["executor_run_id"] = "different-run"
        self.service.store.write_task(current.id, current.to_dict())
        with self.assertRaisesRegex(ABCError, "active Runner run"):
            self.service.record_step_progress(task.id, 1)

    def test_callback_merge_cannot_regress_confirmed_done(self) -> None:
        merged = _finalize_steps([{"id": 1, "description": "one", "status": "done"}, {"id": 2, "description": "two", "status": "pending"}], [{"id": 1, "status": "pending"}, {"id": 2, "status": "blocked"}])
        self.assertEqual([step["status"] for step in merged], ["done", "blocked"])

    def test_input_wait_merges_existing_progress_without_regression(self) -> None:
        (task, run_id, _) = self._running()
        self.service.record_step_progress(task.id, 1)
        changed = self.service.finalize_task_from_executor_exit(
            task.id,
            executor_run_id=run_id,
            callback={"version": 1, "task_id": task.id, "final_state": "input_required", "summary": "Need one user choice", "input": {"type": "message", "reason": "Choose the next target"}, "step_results": [{"id": 1, "status": "pending"}, {"id": 2, "status": "blocked"}]},
        )
        self.assertTrue(changed)
        waiting = self.service.get_task(task.id)
        self.assertEqual(waiting.status, "input_required")
        self.assertEqual([step["status"] for step in waiting.steps], ["done", "blocked"])
        self.assertEqual(waiting.extensions[PROGRESS_EXTENSION_KEY]["latest_sequence"], 1)

    def test_status_report_and_notification_share_redacted_projection(self) -> None:
        (task, run_id, session_id) = self._running()
        self.service.record_step_progress(task.id, 1)
        current = self.service.get_task(task.id)
        expected = progress_public_projection(current.extensions[PROGRESS_EXTENSION_KEY])
        status = task_to_status(current, self.service)
        report = generate_report(task.id, self.board)
        notification = build_notification_payload(self.service, task.id, "task.failed", "error", "fixture")
        self.assertEqual(status["execution_policy"]["progress"], expected)
        self.assertEqual(status["extensions"][PROGRESS_EXTENSION_KEY], expected)
        self.assertEqual(report["progress"], expected)
        self.assertEqual(notification["progress"], expected)
        rendered = generate_report_md(task.id, self.board)
        self.assertIn("## Confirmed Progress", rendered)
        self.assertNotIn(run_id, str(status["execution_policy"]["progress"]))
        self.assertNotIn(session_id, str(status["extensions"][PROGRESS_EXTENSION_KEY]))
        self.assertNotIn(run_id, str(report["progress"]))
        self.assertNotIn(session_id, str(notification["progress"]))

    def test_malformed_or_future_receipt_is_not_publicly_projected(self) -> None:
        self.assertIsNone(progress_public_projection({"version": 99}))
        with self.assertRaises(ABCError):
            validate_progress_record({"version": 99})

    def test_runner_owned_hermes_progress_binds_live_process_session(self) -> None:
        task = self.service.create_task("Hermes live progress bridge", "hermes", [{"id": 1, "description": "record one step"}], customer_dir=True, customer_path=self.project, permission_mode="full")
        self.service.start_task_run(task.id, "hermes")
        run_id = f"hermes-{task.id}-live"
        worker_run_id = "runner-worker-flow103live"
        self.service.record_executor_run_started(task.id, run_id)
        self.service.update_execution_metadata(task.id, {"executor_run_id": run_id, "worker_run_id": worker_run_id})
        args = mock.Mock(root=self.board, id=task.id, step=1, state="running", summary="step one complete", source="agent")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_TASK_ID": task.id, "AGENTBC_RUNNER_WORKER_ID": worker_run_id, "HERMES_SESSION_ID": "20260915_010203_flow103"}, clear=False), contextlib.redirect_stdout(output):
            code = command_task_progress(args)
        self.assertEqual(code, 0, output.getvalue())
        current = self.service.get_task(task.id)
        self.assertEqual(current.steps[0]["status"], "done")
        self.assertTrue(current.extensions["agentbc.session"]["official_receipt_bound"])
        self.assertEqual(current.extensions["agentbc.session"]["session_id"], "20260915_010203_flow103")

    def test_hermes_progress_env_without_runner_context_cannot_bind(self) -> None:
        task = self.service.create_task("Hermes untrusted progress bridge", "hermes", [{"id": 1, "description": "record one step"}], customer_dir=True, customer_path=self.project, permission_mode="full")
        self.service.start_task_run(task.id, "hermes")
        run_id = f"hermes-{task.id}-live"
        self.service.record_executor_run_started(task.id, run_id)
        self.service.update_execution_metadata(task.id, {"executor_run_id": run_id, "worker_run_id": "runner-worker-real"})
        args = mock.Mock(root=self.board, id=task.id, step=1, state="running", summary="step one complete", source="agent")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_TASK_ID": task.id, "AGENTBC_RUNNER_WORKER_ID": "runner-worker-wrong", "HERMES_SESSION_ID": "20260915_010203_untrusted"}, clear=True), contextlib.redirect_stdout(output):
            code = command_task_progress(args)
        self.assertEqual(code, 1)
        self.assertIn("progress_session_receipt_unbound", output.getvalue())

    def test_worker_preallocates_full_hermes_run_before_synchronous_start(self) -> None:
        task = self.service.create_task("Hermes preallocated full run", "hermes", [{"id": 1, "description": "finish"}], customer_dir=True, customer_path=self.project, permission_mode="full")
        service = self.service

        class InspectingHermesExecutor:
            def __init__(self) -> None:
                self.run_id = ""

            def probe(self):
                return ProbeResult(ok=True, message="ready")

            def capabilities(self):
                return ExecutorCapabilities(level=ExecutorLevel.L2)

            def start(self, packet):
                self.run_id = packet["_agentbc_executor_run_id"]
                persisted = service.get_task(task.id)
                session = persisted.extensions["agentbc.session"]
                execution = persisted.extensions["agentbc.execution"]
                assert session["run_ids"] == [self.run_id]
                assert session["run_resume_facts"] == {self.run_id: False}
                assert execution["executor_run_id"] == self.run_id
                assert packet["_agentbc_resume_fact"] == {"run_id": self.run_id, "resumed": False, "session_id": ""}
                return StartResult(ok=True, run_id=self.run_id, message="started")

            def poll(self, run_id):
                return PollResult(
                    status="completed",
                    result={
                        "returncode": 0,
                        "summary": "done",
                        "agent_callback": {"version": 1, "task_id": task.id, "final_state": "completed", "summary": "done", "step_results": [{"id": 1, "status": "done"}]},
                        "execution_session": {"version": 1, "executor": "hermes", "session_id": "20260915_010203_prealloc", "resumed": False, "persistence": "persistent", "source": "stderr_receipt"},
                    },
                )

        executor = InspectingHermesExecutor()
        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor), mock.patch("agent_bridge_connect.cli._notify_terminal"):
            code = command_worker_run(mock.Mock(root=self.board, executor="hermes", task_id=task.id, once=True, interval=0.01, config=None, detach=False, monitor=False, runner_authorize=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.service.get_task(task.id).status, "completed")


_terminal_fault_T0 = "2026-09-01T00:00:00Z"
_terminal_fault_HERMES_SESSION_ID = "20260901_000000_a1b2c3"
_terminal_fault_INPUT_DEADLINE = "2026-09-30T00:00:00Z"


def _terminal_fault__at(seconds: int) -> str:
    parsed = _terminal_fault_datetime.fromisoformat(_terminal_fault_T0.replace("Z", "+00:00"))
    return (parsed + _terminal_fault_timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class _terminal_fault_Flow104BoardTestCase(_terminal_fault_unittest.TestCase):
    """Shared board fixtures.

    ``_coordinator`` stubs the two notification stages by default so a unit test
    never opens a real dialog; pass ``stub_notifications=False`` to opt in.
    """

    def setUp(self) -> None:
        self.temporary = _terminal_fault_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _terminal_fault_Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = _terminal_fault_TaskService(self.board, config={"workspace_root": str(self.root / "workspace"), "sessions": {"retain_executor_sessions": False}})

    def _coordinator(self, **kwargs) -> _terminal_fault_TerminalDeliveryCoordinator:
        executors = dict(kwargs.pop("stage_executors", {}) or {})
        if kwargs.pop("stub_notifications", True):
            executors.setdefault("file_notification", lambda: _terminal_fault_StageOutcome(True))
            executors.setdefault("ui_notification", lambda: _terminal_fault_StageOutcome(True))
        return _terminal_fault_TerminalDeliveryCoordinator(self.board, stage_executors=executors)

    def _closed_lease(self, task_id: str) -> None:
        lease = _terminal_fault_create_lease(task_id, "hermes", _terminal_fault_os.getpid(), str(self.root))
        lease.state = _terminal_fault_RunLeaseState.CLOSED
        _terminal_fault_save_lease(lease, self.board)


class _terminal_fault_FaultInjectionTestCase(_terminal_fault_Flow104BoardTestCase):
    def _terminal_task(self, *, status: str = "completed", steps: int = 2) -> str:
        step_list = [{"id": i, "description": f"step {i}"} for i in range(1, steps + 1)]
        task = self.service.create_task("flow104 fault injection", "hermes", step_list, customer_dir=False)
        raw = self.service.store.read_task(task.id)
        raw["status"] = status
        raw["updated_at"] = _terminal_fault_T0
        raw["steps"] = [{**step, "status": "done"} for step in raw.get("steps") or step_list]
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["session_state"] = "terminal"
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["retain"] = False
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["session_id"] = _terminal_fault_HERMES_SESSION_ID
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["created_at"] = _terminal_fault_T0
        raw["extensions"]["agentbc.final_callback"] = {"version": 1, "task_id": task.id, "final_state": status, "summary": "terminal", "marker_valid": True, "report_file": str(raw["workspace"].get("report_file") or ""), "step_results": [{"id": step["id"], "status": "done"} for step in raw["steps"]]}
        raw["extensions"][_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY] = _terminal_fault_build_terminal_delivery_receipt(task.id, terminal_state=status, terminal_event="task.finalized", committed_at=_terminal_fault_T0)
        self.service.store.write_task(task.id, raw)
        self._closed_lease(task.id)
        return task.id

    def _snapshot(self, task_id: str) -> dict:
        raw = self.service.store.read_task(task_id)
        return {"status": raw["status"], "steps": _terminal_fault_copy.deepcopy(raw["steps"]), "final_callback": _terminal_fault_copy.deepcopy((raw.get("extensions") or {}).get("agentbc.final_callback"))}

    def _receipt(self, task_id: str) -> dict:
        return self.service.store.read_task(task_id)["extensions"][_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY]

    def _cleanup_pass(self, task_id: str, *, now: str = _terminal_fault_T0) -> dict:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        return SessionCleanupCoordinator(self.board).request_cleanup(task_id, now=now)

    def test_report_permission_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _denied() -> _terminal_fault_StageOutcome:
            raise PermissionError("report directory is read-only")

        handlers = {"report": _denied}
        coordinator = self._coordinator(stage_executors=handlers)
        with _terminal_fault_mock.patch.object(coordinator, "_report_executor", return_value=_denied):
            coordinator.deliver_now(task_id, now=_terminal_fault_T0)
        self.assertEqual(self._snapshot(task_id), before)
        self.assertEqual(self.service.store.read_task(task_id)["status"], "completed")
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["record"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["index"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["report"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["report"]["last_error_code"], "report_write_failed")
        result = self._cleanup_pass(task_id)
        self.assertNotIn("report_not_written", result.get("blockers") or [])
        self.assertNotEqual(result["status"], "skipped")
        self.assertEqual(self._snapshot(task_id), before)

    def test_record_beyond_50kib(self) -> None:
        task_id = self._terminal_task()
        raw = self.service.store.read_task(task_id)
        raw["extensions"]["agentbc.test.large"] = {"payload": "x" * (_terminal_fault_MAX_TASK_RECORD_BYTES + 4096)}
        self.service.store.write_task(task_id, raw)
        self.assertGreater(_terminal_fault_task_record_size(self.service.store.task_dir(task_id)), _terminal_fault_MAX_TASK_RECORD_BYTES)
        before_status = self.service.store.read_task(task_id)["status"]
        receipt_before = self._receipt(task_id)["delivery_id"]
        result = self._coordinator().deliver_now(task_id, now=_terminal_fault_T0)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(self.service.store.read_task(task_id)["status"], before_status)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["delivery_id"], receipt_before)
        self.assertEqual(_terminal_fault_read_terminal_delivery_receipt(receipt)["stages"], receipt["stages"])
        for stage in ("report", "record", "index"):
            self.assertEqual(receipt["stages"][stage]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        self._cleanup_pass(task_id)
        self.assertEqual(self.service.store.read_task(task_id)["status"], before_status)

    def test_index_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _index_boom() -> _terminal_fault_StageOutcome:
            raise OSError("index directory unavailable")

        coordinator = self._coordinator(stage_executors={"index": _index_boom})
        with _terminal_fault_mock.patch.object(coordinator, "_index_executor", return_value=_index_boom):
            coordinator.deliver_now(task_id, now=_terminal_fault_T0)
        self.assertEqual(self._snapshot(task_id), before)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["index"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["index"]["last_error_code"], "index_refresh_failed")
        self.assertEqual(receipt["stages"]["report"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["record"]["state"], "succeeded")
        health = _terminal_fault_delivery_health_view(receipt)
        self.assertEqual(health["state"], "retry_wait")
        self.assertFalse(health["healthy"])
        self._coordinator().deliver_now(task_id, now=_terminal_fault__at(400))
        self.assertEqual(self._snapshot(task_id), before)
        self.assertEqual(self._receipt(task_id)["stages"]["index"]["state"], "succeeded")

    def test_file_notifier_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _file() -> _terminal_fault_StageOutcome:
            return _terminal_fault_StageOutcome(False, error_code="file_notification_failed")

        coordinator = self._coordinator(stage_executors={"file_notification": _file})
        with _terminal_fault_mock.patch.object(coordinator, "_file_executor", return_value=_file):
            coordinator.deliver_now(task_id, now=_terminal_fault_T0)
        self.assertEqual(self._snapshot(task_id), before)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "succeeded")
        self.assertEqual(receipt["stages"]["index"]["state"], "succeeded")
        result = self._cleanup_pass(task_id)
        self.assertNotIn("notification_not_recorded", result.get("blockers") or [])
        self.assertEqual(self._snapshot(task_id), before)

    def test_ui_notifier_failure(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _ui() -> _terminal_fault_StageOutcome:
            return _terminal_fault_StageOutcome(False, error_code="ui_notification_failed")

        coordinator = self._coordinator(stage_executors={"ui_notification": _ui})
        with _terminal_fault_mock.patch.object(coordinator, "_ui_executor", return_value=_ui):
            coordinator.deliver_now(task_id, now=_terminal_fault_T0)
        self.assertEqual(self._snapshot(task_id), before)
        receipt = self._receipt(task_id)
        self.assertEqual(receipt["stages"]["ui_notification"]["state"], "retry_wait")
        self.assertEqual(receipt["stages"]["ui_notification"]["last_error_code"], "ui_notification_failed")
        self.assertEqual(receipt["stages"]["file_notification"]["state"], "succeeded")
        result = self._cleanup_pass(task_id)
        self.assertIn("task_end_dialog_not_delivered", result.get("blockers") or [])
        self.assertEqual(result["status"], "skipped")

    def test_concurrent_terminal_callbacks(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)
        coordinator_a = self._coordinator()
        coordinator_b = self._coordinator()
        first = coordinator_a.deliver_now(task_id, now=_terminal_fault_T0)
        second = coordinator_b.deliver_now(task_id, now=_terminal_fault__at(1))
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(self._snapshot(task_id), before)
        for stage in ("report", "record", "index"):
            self.assertEqual(second["receipt"]["stages"][stage]["attempts"], first["receipt"]["stages"][stage]["attempts"])

    def test_runner_death_between_reservation_and_result(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)
        reserved = _terminal_fault_read_terminal_delivery_receipt(self._receipt(task_id))
        for stage in ("file_notification", "ui_notification"):
            reserved = _terminal_fault_transition_terminal_delivery_stage(reserved, stage, "in_progress", occurred_at=_terminal_fault_T0)
        raw = self.service.store.read_task(task_id)
        raw["extensions"][_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY] = reserved
        self.service.store.write_task(task_id, raw)
        from agent_bridge_connect.terminal_delivery import reconcile_interrupted_stages

        (reconciled, stages) = reconcile_interrupted_stages(reserved, now=_terminal_fault__at(1))
        self.assertEqual(sorted(stages), ["file_notification", "ui_notification"])
        for stage in ("file_notification", "ui_notification"):
            self.assertEqual(reconciled["stages"][stage]["state"], "retry_wait")
            self.assertEqual(reconciled["stages"][stage]["last_error_code"], "delivery_stage_uncertain")
            self.assertEqual(reconciled["stages"][stage]["next_attempt_at"], _terminal_fault__at(301))
        self.assertEqual(self._snapshot(task_id), before)

    def test_runner_restart_replays_only_incomplete_stages(self) -> None:
        task_id = self._terminal_task()
        before = self._snapshot(task_id)

        def _failed() -> _terminal_fault_StageOutcome:
            return _terminal_fault_StageOutcome(False, error_code="file_notification_failed")

        first = self._coordinator(stage_executors={"file_notification": _failed, "ui_notification": _failed}).deliver_now(task_id, now=_terminal_fault_T0)
        self.assertEqual(first["receipt"]["stages"]["file_notification"]["state"], "retry_wait")
        calls: list[str] = []

        def _spy(stage: str):

            def _run() -> _terminal_fault_StageOutcome:
                calls.append(stage)
                return _terminal_fault_StageOutcome(True)

            return _run

        restarted = self._coordinator(stage_executors={"report": _spy("report"), "record": _spy("record"), "index": _spy("index"), "file_notification": _spy("file_notification"), "ui_notification": _spy("ui_notification")})
        restarted.deliver_now(task_id, now=_terminal_fault__at(1))
        self.assertEqual(self._snapshot(task_id), before)
        self.assertTrue(first["delivery_id"])
        self.assertEqual(sorted(calls), ["file_notification", "ui_notification"])
        health = _terminal_fault_delivery_health_view(self._receipt(task_id))
        self.assertTrue(health["healthy"])
        self.assertEqual(health["state"], "succeeded")


class _terminal_fault_ApprovalSystemIsolationTests(_terminal_fault_Flow104BoardTestCase):
    """FLOW-104-002 must not interfere with the current approval system."""

    def setUp(self) -> None:
        super().setUp()
        self.task = self.service.create_task("flow104 approval isolation", "hermes", [{"id": 1, "description": "blocked step"}], customer_dir=False)
        self.task_id = self.task.id
        raw = self.service.store.read_task(self.task_id)
        raw["status"] = "input_required"
        raw["extensions"]["agentbc.input"] = {
            "version": 1,
            "input_id": "I-001",
            "type": "permission",
            "kind": "",
            "scope": "task_elevation",
            "mode": "full",
            "approval_version": 3,
            "elevation_mode": "full",
            "status": "waiting",
            "reason_summary": "needs full access",
            "blocked_step_id": 1,
            "deadline_at": _terminal_fault_INPUT_DEADLINE,
            "dialog_count": 1,
            "requested_permission": "full",
            "decision": "approve_full",
            "request_id": "req-flow104-001",
            "executor_run_id": "run-flow104-001",
            "created_at": _terminal_fault_T0,
            "continuation_count": 2,
            "worker_count": 1,
        }
        raw["extensions"][_terminal_fault_PERMISSION_ELEVATION_EXTENSION_KEY] = {"version": 1, "mode": "full", "scope": "task_elevation", "cardinality": {"notifications": 1, "dialogs": 1}, "reserved_at": _terminal_fault_T0}
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["session_state"] = "input_required"
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["session_id"] = _terminal_fault_HERMES_SESSION_ID
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["created_at"] = _terminal_fault_T0
        self.service.store.write_task(self.task_id, raw)
        lease = _terminal_fault_create_lease(self.task_id, "hermes", _terminal_fault_os.getpid(), str(self.root))
        lease.state = _terminal_fault_RunLeaseState.SUSPENDED
        _terminal_fault_save_lease(lease, self.board)

    def _snapshot(self) -> dict:
        raw = self.service.store.read_task(self.task_id)
        return {
            "status": raw["status"],
            "input": _terminal_fault_copy.deepcopy(raw["extensions"]["agentbc.input"]),
            "elevation": _terminal_fault_copy.deepcopy(raw["extensions"].get(_terminal_fault_PERMISSION_ELEVATION_EXTENSION_KEY)),
            "session": _terminal_fault_copy.deepcopy(raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]),
        }

    def _assert_unchanged(self, before: dict, after: dict) -> None:
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["status"], "input_required")
        self.assertEqual(after["input"], before["input"])
        self.assertEqual(after["elevation"], before["elevation"])
        self.assertEqual(after["session"], before["session"])
        self.assertEqual(before["input"]["deadline_at"], _terminal_fault_INPUT_DEADLINE)
        self.assertEqual(before["input"]["dialog_count"], 1)
        self.assertEqual(before["input"]["continuation_count"], 2)
        self.assertEqual(before["input"]["worker_count"], 1)

    def test_maintenance_leaves_input_required_task_untouched(self) -> None:
        before = self._snapshot()
        coordinator = _terminal_fault_TerminalDeliveryCoordinator(self.board)
        with _terminal_fault_mock.patch.object(_terminal_fault_TaskService, "respond_to_input", side_effect=AssertionError("respond_to_input must never be called")) as respond:
            with _terminal_fault_mock.patch.object(_terminal_fault_TaskService, "respond_to_live_claude_elevation", side_effect=AssertionError("native respond must never be called")):
                results = coordinator.maintain_board(now=_terminal_fault__at(400))
            respond.assert_not_called()
        self.assertEqual(results, [])
        self._assert_unchanged(before, self._snapshot())
        self.assertIsNone(self.service.store.read_task(self.task_id)["extensions"].get(_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY))

    def test_deliver_now_refuses_input_required_task(self) -> None:
        before = self._snapshot()
        result = _terminal_fault_TerminalDeliveryCoordinator(self.board).deliver_now(self.task_id, now=_terminal_fault__at(400))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("task_not_business_terminal", result["blockers"])
        self._assert_unchanged(before, self._snapshot())

    def test_needs_recovery_task_end_delivery_runs_without_status_rewrite(self) -> None:
        raw = self.service.store.read_task(self.task_id)
        raw["status"] = "needs_recovery"
        raw["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["session_state"] = "needs_recovery"
        self.service.store.write_task(self.task_id, raw)
        results = _terminal_fault_TerminalDeliveryCoordinator(self.board).maintain_board(now=_terminal_fault__at(400))
        self.assertEqual(len(results), 1)
        self.assertEqual(self.service.store.read_task(self.task_id)["status"], "needs_recovery")
        self.assertEqual(self.service.store.read_task(self.task_id)["extensions"][_terminal_fault_SESSION_EXTENSION_KEY]["session_state"], "needs_recovery")

    def test_permission_mode_and_session_survive_a_terminal_delivery_pass(self) -> None:
        """A sibling terminal task's delivery must not disturb the waiting task."""
        sibling = _terminal_fault_FaultInjectionTestCase._terminal_task(self)
        before = self._snapshot()
        _terminal_fault_TerminalDeliveryCoordinator(self.board).deliver_now(sibling, now=_terminal_fault_T0)
        self._assert_unchanged(before, self._snapshot())
        report = self.service.get_task(self.task_id)
        self.assertEqual(report.status, "input_required")


class _terminal_fault_BoundedEventAndProjectionTests(_terminal_fault_Flow104BoardTestCase):
    def test_delivery_events_never_carry_private_paths_or_bodies(self) -> None:
        task = self.service.create_task("bounded events", "hermes", [{"id": 1, "description": "run"}], customer_dir=False)
        raw = self.service.store.read_task(task.id)
        raw["status"] = "completed"
        raw["extensions"][_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY] = _terminal_fault_build_terminal_delivery_receipt(task.id, terminal_state="completed", terminal_event="task.finalized", committed_at=_terminal_fault_T0)
        self.service.store.write_task(task.id, raw)
        _terminal_fault_TerminalDeliveryCoordinator(self.board).deliver_now(task.id, now=_terminal_fault_T0)
        path = self.service.store.task_dir(task.id) / _terminal_fault_DELIVERY_EVENTS_FILE
        self.assertTrue(path.exists())
        blob = path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn(str(self.root.resolve()), blob)
        self.assertNotIn("report_file", blob)
        self.assertNotIn("reason_summary", blob)
        for line in blob.splitlines():
            self.assertTrue(line.strip())
            _terminal_fault_json.loads(line)

    def test_public_status_projection_is_path_free(self) -> None:
        task = self.service.create_task("public projection", "hermes", [{"id": 1, "description": "run"}], customer_dir=False)
        raw = self.service.store.read_task(task.id)
        raw["status"] = "completed"
        raw["extensions"][_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY] = _terminal_fault_build_terminal_delivery_receipt(task.id, terminal_state="completed", terminal_event="task.finalized", committed_at=_terminal_fault_T0)
        self.service.store.write_task(task.id, raw)
        from agent_bridge_connect.execution_policy import public_task_view

        view = public_task_view(raw)
        policy = view["execution_policy"]
        delivery = _terminal_fault_json.dumps({"extensions": view["extensions"][_terminal_fault_TERMINAL_DELIVERY_EXTENSION_KEY], "terminal_delivery": policy["terminal_delivery"], "delivery_health": policy["delivery_health"]})
        self.assertNotIn(str(self.root), delivery)
        self.assertNotIn(str(self.root.resolve()), delivery)
        self.assertNotIn("next_attempt_at", delivery)
        self.assertIn("terminal_delivery", delivery)
        self.assertIn("delivery_health", delivery)
        self.assertEqual(policy["delivery_health"]["stages_total"], 5)
