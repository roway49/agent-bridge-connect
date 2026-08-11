"""Targeted tests for the Phase 5 Task 2 terminal session cleanup coordinator.

Uses a fake ExecutorPort and a temporary task board; never invokes a real
Executor, real purge, or any outside service.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import SessionCleanupResult
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.run_lease import create_lease, save_lease
from agent_bridge_connect.service import TaskService

T0 = "2026-08-11T00:00:00Z"
HERMES_SESSION_ID = "20260811_000000_a1b2c3"


def _add_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class FakeCleanupExecutor:
    """Fake ExecutorPort cleanup_session returning a configurable result."""

    def __init__(self, result: SessionCleanupResult | None = None) -> None:
        self.result = result or SessionCleanupResult(
            state="failed",
            capability="supported",
            strategy="official_session_delete",
            error_code="session_delete_busy",
            retryable=True,
        )
        self.calls: list = []
        self.raise_error: BaseException | None = None

    def cleanup_session(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


class CleanupCoordinatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    # ------------------------------------------------------------- builders
    def _base_task(
        self,
        *,
        executor: str = "hermes",
        retain: bool = False,
        session_state: str = "terminal",
        session_id: str = HERMES_SESSION_ID,
        status: str = "completed",
        report: bool = True,
        notification: bool = True,
        final_callback: bool = True,
    ):
        task = self.service.create_task(
            "cleanup coordinator",
            executor,
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        raw = self.service.store.read_task(task.id)
        raw["status"] = status
        raw["updated_at"] = T0
        session = raw["extensions"][SESSION_EXTENSION_KEY]
        session["session_state"] = session_state
        session["retain"] = retain
        if session_id is None:
            session["session_id"] = ""
        else:
            session["session_id"] = session_id
        # Keep the session snapshot valid: Claude retain requires native mode
        # with an absolute project path; ephemeral mode otherwise.
        if executor == "claude":
            if retain:
                session["project_mode"] = "native"
                session["project_path"] = str(
                    raw["workspace"].get("project_root") or self.root
                )
            else:
                session["project_mode"] = "ephemeral"
                session["project_path"] = str(
                    raw["workspace"].get("executor_project_root") or self.root
                )
        raw["extensions"][SESSION_EXTENSION_KEY] = session
        if final_callback:
            raw["extensions"]["agentbc.final_callback"] = {
                "version": 1,
                "task_id": task.id,
                "final_state": "completed",
                "summary": "done",
                "report_file": str(raw["workspace"].get("report_file") or ""),
            }
        report_file = Path(str(raw["workspace"]["report_file"]))
        if report:
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text("# terminal report\n", encoding="utf-8")
        self.service.store.write_task(task.id, raw)
        if notification:
            self.service.store.append_event(
                task.id,
                {
                    "event_type": "notification_delivery",
                    "task_id": task.id,
                    "notification_event": "task.finalized",
                    "file_ok": True,
                    "dialog_ok": True,
                    "dialog_message": "",
                    "dialog_delay_s": 0,
                    "created_at": T0,
                },
            )
        return task.id

    def _set_lease(self, task_id: str, state: str) -> None:
        lease = create_lease(task_id, "hermes", os.getpid(), str(self.root))
        lease.state = state
        save_lease(lease, self.board)

    def _set_cleanup(self, task_id: str, receipt: dict) -> None:
        raw = self.service.store.read_task(task_id)
        raw["extensions"][SESSION_EXTENSION_KEY]["cleanup"] = receipt
        self.service.store.write_task(task_id, raw)

    def _coordinator(self, executor):
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        return SessionCleanupCoordinator(self.board, executor_port=executor)

    def _session(self, task_id: str) -> dict:
        return copy.deepcopy(
            self.service.store.read_task(task_id)["extensions"][SESSION_EXTENSION_KEY]
        )

    def _cleanup_events(self, task_id: str) -> list[dict]:
        from agent_bridge_connect.session_cleanup import CLEANUP_EVENTS_FILE

        path = self.service.store.task_dir(task_id) / CLEANUP_EVENTS_FILE
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ------------------------------------------------------------- gate order
    def test_gate_order_and_zero_side_effects(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        cases = [
            ({"status": "input_required"}, "task_not_terminal"),
            ({"status": "needs_recovery"}, "task_not_terminal"),
            ({"status": "running"}, "task_not_terminal"),
            ({"report": False}, "report_not_written"),
            ({"notification": False}, "notification_not_recorded"),
            ({"session_state": "input_required"}, "session_not_terminal"),
            ({"session_state": "needs_recovery"}, "session_not_terminal"),
            ({"session_state": "active"}, "session_not_terminal"),
            ({"session_id": None}, "session_receipt_invalid"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides, expected=expected):
                task_id = self._base_task(**overrides)
                executor = FakeCleanupExecutor()
                coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
                result = coordinator.request_cleanup(task_id, now=T0)
                self.assertEqual(result["status"], "skipped")
                self.assertIn(expected, result["blockers"])
                self.assertFalse(result["actioned"])
                self.assertEqual(self._session(task_id)["cleanup"]["state"], "not_requested")
                self.assertEqual(self._cleanup_events(task_id), [])
                self.assertEqual(executor.calls, [])

    def test_active_lease_blocks_and_stale_lease_blocks_zero_side_effects(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        for state in ("active", "stale"):
            with self.subTest(lease_state=state):
                task_id = self._base_task()
                self._set_lease(task_id, state)
                executor = FakeCleanupExecutor()
                coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
                result = coordinator.request_cleanup(task_id, now=T0)
                self.assertEqual(result["status"], "skipped")
                self.assertIn("run_lease_not_closed", result["blockers"])
                self.assertFalse(result["actioned"])
                self.assertEqual(self._session(task_id)["cleanup"]["state"], "not_requested")
                self.assertEqual(executor.calls, [])

    def test_multiple_blockers_preserve_gate_order(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task(status="running", notification=False)
        self._set_lease(task_id, "active")
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(
            result["blockers"],
            ["task_not_terminal", "run_lease_not_closed", "notification_not_recorded"],
        )
        self.assertFalse(result["actioned"])
        self.assertEqual(executor.calls, [])

    # ----------------------------------------------------------------- retain
    def test_retain_terminal_session_records_retained_without_executor(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task(executor="claude", retain=True)
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "retained")
        self.assertTrue(result["actioned"])
        self.assertEqual(result["receipt"]["state"], "retained")
        self.assertEqual(result["receipt"]["capability"], "not_applicable")
        self.assertEqual(result["receipt"]["strategy"], "retain")
        self.assertEqual(executor.calls, [])
        session = self._session(task_id)
        self.assertEqual(session["cleanup"]["state"], "retained")
        events = self._cleanup_events(task_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["cleanup_event"], "retained")

    def test_retain_requires_terminal_gates_before_retained(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task(executor="claude", retain=True, report=False)
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("report_not_written", result["blockers"])
        self.assertFalse(result["actioned"])
        self.assertEqual(self._session(task_id)["cleanup"]["state"], "not_requested")
        self.assertEqual(executor.calls, [])

    # ------------------------------------------------- supported / unsupported
    def test_supported_success_and_request_fields(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        steps_before = self.service.store.read_task(task_id)["steps"]
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
            )
        )
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["actioned"])
        self.assertEqual(result["receipt"]["state"], "succeeded")
        self.assertEqual(result["receipt"]["attempts"], 1)
        self.assertEqual(len(executor.calls), 1)
        request = executor.calls[0]
        self.assertEqual(request.task_id, task_id)
        self.assertEqual(request.session_id, HERMES_SESSION_ID)
        self.assertFalse(request.retain)
        self.assertEqual(request.project_mode, "none")
        self.assertEqual(request.strategy, "official_session_delete")
        self.assertIsInstance(request.workspace, dict)
        self.assertEqual(request.workspace.get("task_code"), task_id.split("-")[0])

        task = self.service.store.read_task(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertIsNotNone(task["extensions"].get("agentbc.final_callback"))
        self.assertTrue(Path(str(task["workspace"]["report_file"])).is_file())
        self.assertEqual(task["steps"], steps_before)

    def test_unsupported_is_resolved_and_not_retried(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="unsupported",
                capability="unsupported",
                strategy="none",
                error_code="official_session_delete_unavailable",
            )
        )
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        first = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(first["status"], "unsupported")
        self.assertFalse(first["receipt"]["retryable"])
        second = coordinator.request_cleanup(task_id, now=_add_seconds(T0, 3600))
        self.assertEqual(second["status"], "resolved")
        self.assertEqual(len(executor.calls), 1)

    # -------------------------------------------------------------- retries
    def test_retry_backoff_60s_then_5m_then_attempt_cap(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        executor = FakeCleanupExecutor()  # always failed, retryable=True
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)

        first = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(first["status"], "failed")
        self.assertTrue(first["receipt"]["retryable"])
        self.assertEqual(first["receipt"]["attempts"], 1)
        self.assertEqual(first["receipt"]["next_attempt_at"], _add_seconds(T0, 60))

        # Too early -> waiting, no Executor call.
        early = coordinator.request_cleanup(task_id, now=_add_seconds(T0, 30))
        self.assertEqual(early["status"], "waiting")
        self.assertEqual(len(executor.calls), 1)

        second = coordinator.request_cleanup(task_id, now=_add_seconds(T0, 60))
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["receipt"]["attempts"], 2)
        self.assertTrue(second["receipt"]["retryable"])
        self.assertEqual(second["receipt"]["next_attempt_at"], _add_seconds(T0, 360))

        third = coordinator.request_cleanup(task_id, now=_add_seconds(T0, 360))
        self.assertEqual(third["status"], "failed")
        self.assertEqual(third["receipt"]["attempts"], 3)
        self.assertFalse(third["receipt"]["retryable"])
        self.assertEqual(third["receipt"]["next_attempt_at"], "")

        final = coordinator.request_cleanup(task_id, now=_add_seconds(T0, 361))
        self.assertEqual(final["status"], "final")
        self.assertEqual(len(executor.calls), 3)
        session = self._session(task_id)
        self.assertEqual(session["cleanup"]["state"], "failed")
        self.assertFalse(session["cleanup"]["retryable"])

    def test_executor_exception_becomes_retryable_failure(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        executor = FakeCleanupExecutor()
        executor.raise_error = RuntimeError("boom")
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["receipt"]["retryable"])
        self.assertEqual(result["receipt"]["error_code"], "session_cleanup_failed")
        self.assertEqual(len(executor.calls), 1)

    # ------------------------------------------------------ pending recovery
    def _pending_receipt(self, attempts: int = 1) -> dict:
        return {
            "version": 1,
            "capability": "supported",
            "strategy": "official_session_delete",
            "state": "pending",
            "attempts": attempts,
            "requested_at": T0,
            "last_attempt_at": T0,
            "next_attempt_at": "",
            "completed_at": "",
            "error_code": "",
            "retryable": False,
        }

    def test_pending_crash_recovery_forms_stable_failure_then_retries(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        self._set_cleanup(task_id, self._pending_receipt())
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)

        recovered = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(recovered["status"], "recovered")
        self.assertTrue(recovered["actioned"])
        self.assertEqual(recovered["receipt"]["state"], "failed")
        self.assertEqual(recovered["receipt"]["error_code"], "session_cleanup_interrupted")
        self.assertTrue(recovered["receipt"]["retryable"])
        self.assertEqual(recovered["receipt"]["next_attempt_at"], _add_seconds(T0, 60))
        # The crashed pending must not trigger a duplicate purge in the same pass.
        self.assertEqual(executor.calls, [])

        # Backoff elapses: the stable failed receipt is retried exactly once.
        retry = coordinator.request_cleanup(task_id, now=_add_seconds(T0, 60))
        self.assertEqual(retry["status"], "failed")
        self.assertEqual(retry["receipt"]["attempts"], 2)
        self.assertEqual(len(executor.calls), 1)

    def test_process_restart_recovers_crashed_pending(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        self._set_cleanup(task_id, self._pending_receipt())
        # A fresh coordinator instance models a restarted Runner process.
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(self._session(task_id)["cleanup"]["state"], "failed")
        self.assertEqual(self._session(task_id)["cleanup"]["retryable"], True)
        self.assertEqual(self._session(task_id)["cleanup"]["next_attempt_at"], _add_seconds(T0, 60))

    def test_pending_with_unmet_gates_is_left_untouched(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task(report=False)
        self._set_cleanup(task_id, self._pending_receipt())
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("report_not_written", result["blockers"])
        self.assertFalse(result["actioned"])
        self.assertEqual(self._session(task_id)["cleanup"]["state"], "pending")
        self.assertEqual(executor.calls, [])

    # ------------------------------------------------------------ maintenance
    def test_repeated_maintenance_is_idempotent(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
            )
        )
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        first = coordinator.maintain_board(now=T0)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["task_id"], task_id)
        self.assertEqual(self._session(task_id)["cleanup"]["state"], "succeeded")

        second = coordinator.maintain_board(now=T0)
        self.assertEqual(second, [])
        self.assertEqual(len(executor.calls), 1)

    def test_maintenance_recovers_crashed_pending(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        self._set_cleanup(task_id, self._pending_receipt())
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        results = coordinator.maintain_board(now=T0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "recovered")
        self.assertEqual(executor.calls, [])

    # -------------------------------------------------- atomic write failures
    def test_atomic_write_failure_leaves_task_unchanged(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        executor = FakeCleanupExecutor()
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        before = self.service.store.read_task(task_id)
        with mock.patch.object(
            coordinator.store,
            "write_task",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                coordinator.request_cleanup(task_id, now=T0)
        after = self.service.store.read_task(task_id)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["extensions"], before["extensions"])
        self.assertEqual(self._cleanup_events(task_id), [])
        self.assertEqual(executor.calls, [])

    # --------------------------------------- terminal state immutability + no leaks
    def test_cleanup_failure_does_not_change_terminal_state(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        steps_before = self.service.store.read_task(task_id)["steps"]
        executor = FakeCleanupExecutor()  # failed retryable
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "failed")
        task = self.service.store.read_task(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["extensions"]["agentbc.final_callback"]["final_state"], "completed")
        self.assertTrue(Path(str(task["workspace"]["report_file"])).is_file())
        self.assertEqual(task["steps"], steps_before)
        self.assertEqual(task["extensions"][SESSION_EXTENSION_KEY]["session_state"], "terminal")

    def test_receipt_and_event_never_leak_paths_prompts_or_secrets(self) -> None:
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        task_id = self._base_task()
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy="official_session_delete",
                error_code="Delete failed: /Users/example/.codex/private.db token=super-secret",
            )
        )
        coordinator = SessionCleanupCoordinator(self.board, executor_port=executor)
        result = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["receipt"]["error_code"], "session_cleanup_failed")
        task = self.service.store.read_task(task_id)
        receipt = task["extensions"][SESSION_EXTENSION_KEY]["cleanup"]
        self.assertEqual(receipt["error_code"], "session_cleanup_failed")
        serialized = repr(task)
        self.assertNotIn("private.db", serialized)
        self.assertNotIn("super-secret", serialized)
        for event in self._cleanup_events(task_id):
            serialized_event = repr(event)
            self.assertNotIn("private.db", serialized_event)
            self.assertNotIn("super-secret", serialized_event)
            self.assertNotIn("/Users/example", serialized_event)


if __name__ == "__main__":
    unittest.main()
