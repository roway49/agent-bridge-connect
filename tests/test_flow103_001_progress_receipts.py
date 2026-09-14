from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import (
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    StartResult,
)
from agent_bridge_connect.cli import command_task_progress, command_worker_run
from agent_bridge_connect.execution_policy import SESSION_RECEIPT_SOURCES
from agent_bridge_connect.notifications import build_notification_payload
from agent_bridge_connect.progress_receipts import (
    PROGRESS_EXTENSION_KEY,
    progress_public_projection,
    validate_progress_record,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, _finalize_steps, task_to_status


class Flow103ProgressReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "full"},
        )

    def _running(self, executor: str = "codex"):
        task = self.service.create_task(
            "FLOW-103-001 receipt canary",
            executor,
            [
                {"id": 1, "description": "complete first step"},
                {"id": 2, "description": "complete second step"},
            ],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="full",
        )
        self.service.start_task_run(task.id, executor)
        run_id = f"{executor}-flow103-{task.id}"
        self.service.record_executor_run_started(task.id, run_id)
        current = self.service.get_task(task.id)
        session_id = str(current.extensions["agentbc.session"].get("session_id") or "")
        session_id = session_id or f"{executor}-official-{task.id}"
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
        self.service.update_execution_metadata(task.id, {"executor_run_id": run_id})
        return self.service.get_task(task.id), run_id, session_id

    def test_three_executors_record_bounded_monotonic_done_receipts(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                task, run_id, session_id = self._running(executor)
                first = self.service.record_step_progress(task.id, 1)
                reloaded_service = TaskService(
                    self.board,
                    config={"workspace_root": str(self.root), "permission_mode": "full"},
                )
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
        task, _, _ = self._running()
        with self.assertRaisesRegex(ABCError, "Unknown declared step"):
            self.service.record_step_progress(task.id, 99)

        current = self.service.get_task(task.id)
        current.extensions["agentbc.execution"]["executor_run_id"] = "different-run"
        self.service.store.write_task(current.id, current.to_dict())
        with self.assertRaisesRegex(ABCError, "active Runner run"):
            self.service.record_step_progress(task.id, 1)

    def test_callback_merge_cannot_regress_confirmed_done(self) -> None:
        merged = _finalize_steps(
            [
                {"id": 1, "description": "one", "status": "done"},
                {"id": 2, "description": "two", "status": "pending"},
            ],
            [{"id": 1, "status": "pending"}, {"id": 2, "status": "blocked"}],
        )
        self.assertEqual([step["status"] for step in merged], ["done", "blocked"])

    def test_input_wait_merges_existing_progress_without_regression(self) -> None:
        task, run_id, _ = self._running()
        self.service.record_step_progress(task.id, 1)
        changed = self.service.finalize_task_from_executor_exit(
            task.id,
            executor_run_id=run_id,
            callback={
                "version": 1,
                "task_id": task.id,
                "final_state": "input_required",
                "summary": "Need one user choice",
                "input": {"type": "message", "reason": "Choose the next target"},
                "step_results": [
                    {"id": 1, "status": "pending"},
                    {"id": 2, "status": "blocked"},
                ],
            },
        )
        self.assertTrue(changed)
        waiting = self.service.get_task(task.id)
        self.assertEqual(waiting.status, "input_required")
        self.assertEqual([step["status"] for step in waiting.steps], ["done", "blocked"])
        self.assertEqual(
            waiting.extensions[PROGRESS_EXTENSION_KEY]["latest_sequence"],
            1,
        )

    def test_status_report_and_notification_share_redacted_projection(self) -> None:
        task, run_id, session_id = self._running()
        self.service.record_step_progress(task.id, 1)
        current = self.service.get_task(task.id)
        expected = progress_public_projection(current.extensions[PROGRESS_EXTENSION_KEY])

        status = task_to_status(current, self.service)
        report = generate_report(task.id, self.board)
        notification = build_notification_payload(
            self.service,
            task.id,
            "task.failed",
            "error",
            "fixture",
        )
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
        task = self.service.create_task(
            "Hermes live progress bridge",
            "hermes",
            [{"id": 1, "description": "record one step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="full",
        )
        self.service.start_task_run(task.id, "hermes")
        run_id = f"hermes-{task.id}-live"
        self.service.record_executor_run_started(task.id, run_id)
        self.service.update_execution_metadata(task.id, {"executor_run_id": run_id})
        args = mock.Mock(
            root=self.board,
            id=task.id,
            step=1,
            state="running",
            summary="step one complete",
            source="agent",
        )
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "AGENTBC_RUNNER_SPOOL": str(self.root / "spool"),
                    "AGENTBC_RUNNER_CHANNEL": "flow103-test",
                    "HERMES_SESSION_ID": "20260915_010203_flow103",
                },
                clear=False,
            ),
            contextlib.redirect_stdout(output),
        ):
            code = command_task_progress(args)

        self.assertEqual(code, 0, output.getvalue())
        current = self.service.get_task(task.id)
        self.assertEqual(current.steps[0]["status"], "done")
        self.assertTrue(current.extensions["agentbc.session"]["official_receipt_bound"])
        self.assertEqual(
            current.extensions["agentbc.session"]["session_id"],
            "20260915_010203_flow103",
        )

    def test_hermes_progress_env_without_runner_context_cannot_bind(self) -> None:
        task = self.service.create_task(
            "Hermes untrusted progress bridge",
            "hermes",
            [{"id": 1, "description": "record one step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="full",
        )
        self.service.start_task_run(task.id, "hermes")
        run_id = f"hermes-{task.id}-live"
        self.service.record_executor_run_started(task.id, run_id)
        self.service.update_execution_metadata(task.id, {"executor_run_id": run_id})
        args = mock.Mock(
            root=self.board,
            id=task.id,
            step=1,
            state="running",
            summary="step one complete",
            source="agent",
        )
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"HERMES_SESSION_ID": "20260915_010203_untrusted"},
                clear=True,
            ),
            contextlib.redirect_stdout(output),
        ):
            code = command_task_progress(args)

        self.assertEqual(code, 1)
        self.assertIn("progress_session_receipt_unbound", output.getvalue())

    def test_worker_preallocates_full_hermes_run_before_synchronous_start(self) -> None:
        task = self.service.create_task(
            "Hermes preallocated full run",
            "hermes",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="full",
        )
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
                assert packet["_agentbc_resume_fact"] == {
                    "run_id": self.run_id,
                    "resumed": False,
                    "session_id": "",
                }
                return StartResult(ok=True, run_id=self.run_id, message="started")

            def poll(self, run_id):
                return PollResult(
                    status="completed",
                    result={
                        "returncode": 0,
                        "summary": "done",
                        "agent_callback": {
                            "version": 1,
                            "task_id": task.id,
                            "final_state": "completed",
                            "summary": "done",
                            "step_results": [{"id": 1, "status": "done"}],
                        },
                        "execution_session": {
                            "version": 1,
                            "executor": "hermes",
                            "session_id": "20260915_010203_prealloc",
                            "resumed": False,
                            "persistence": "persistent",
                            "source": "stderr_receipt",
                        },
                    },
                )

        executor = InspectingHermesExecutor()
        with (
            mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor),
            mock.patch("agent_bridge_connect.cli._notify_terminal"),
        ):
            code = command_worker_run(
                mock.Mock(
                    root=self.board,
                    executor="hermes",
                    task_id=task.id,
                    once=True,
                    interval=0.01,
                    config=None,
                    detach=False,
                    monitor=False,
                    runner_authorize=True,
                )
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.service.get_task(task.id).status, "completed")


if __name__ == "__main__":
    unittest.main()
