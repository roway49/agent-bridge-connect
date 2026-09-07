from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.executors.hermes import HermesExecutor, _task_resume_session
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService


class Phase3SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def _service(self, *, retain: bool = True) -> TaskService:
        return TaskService(
            self.root / "record",
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": retain},
            },
        )

    def _task(self, service: TaskService, executor: str):
        return service.create_task(
            "phase 3 session lifecycle",
            executor,
            [{"id": 1, "description": "continue the same executor session"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="inherit",
        )

    @staticmethod
    def _receipt(executor: str, session_id: str, *, resumed: bool) -> dict:
        return {
            "version": 1,
            "executor": executor,
            "session_id": session_id,
            "resumed": resumed,
            "persistence": "persistent",
            "source": {
                "claude": "preallocated",
                "hermes": "stderr_receipt",
                "codex": "jsonl_thread_started",
            }[executor],
        }

    def test_claude_input_response_resumes_same_session(self) -> None:
        service = self._service()
        task = self._task(service, "claude")
        session_id = task.extensions[SESSION_EXTENSION_KEY]["session_id"]

        service.start_task_run(task.id, "claude")
        service.record_executor_run_started(task.id, "claude-run-1")
        callback = {
            "version": 1,
            "task_id": task.id,
            "final_state": "input_required",
            "summary": "Need one answer",
            "executor_run_id": "claude-run-1",
            "input": {"type": "message", "reason": "continue"},
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        service.finalize_task_from_executor_exit(
            task.id,
            executor_run_id="claude-run-1",
            callback=callback,
            execution_session=self._receipt("claude", session_id, resumed=False),
        )
        waiting = service.get_task(task.id)
        session = waiting.extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_state"], "input_required")
        self.assertEqual(session["run_ids"], ["claude-run-1"])

        request = waiting.extensions["agentbc.input"]
        service.respond_to_input(
            task.id,
            request["input_id"],
            response_type="message",
            message="continue",
        )
        service.start_task_run(task.id, "claude")
        started = service.record_executor_run_started(task.id, "claude-run-2")
        self.assertTrue(started["resumed"])

        completed_callback = {
            "version": 1,
            "task_id": task.id,
            "final_state": "completed",
            "summary": "continued successfully",
            "executor_run_id": "claude-run-2",
            "step_results": [{"id": 1, "status": "done"}],
        }
        service.finalize_task_from_executor_exit(
            task.id,
            executor_run_id="claude-run-2",
            callback=completed_callback,
            execution_session=self._receipt("claude", session_id, resumed=True),
        )
        completed = service.get_task(task.id)
        session = completed.extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(completed.status, "completed")
        self.assertEqual(session["session_id"], session_id)
        self.assertEqual(session["session_state"], "terminal")
        self.assertEqual(session["run_ids"], ["claude-run-1", "claude-run-2"])
        self.assertEqual(session["resume_count"], 1)
        self.assertEqual(session["cleanup"]["state"], "not_requested")

    def test_hermes_first_receipt_fills_pending_session_id(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        service.start_task_run(task.id, "hermes")
        service.record_executor_run_started(task.id, "hermes-run-1")
        receipt = self._receipt("hermes", "20260810_010203_a1b2c3", resumed=False)
        service.validate_executor_session_result(task.id, "hermes-run-1", receipt)
        service.mark_task_needs_recovery(
            task.id,
            "transport_test",
            "retry later",
            executor_run_id="hermes-run-1",
            execution_session=receipt,
        )
        session = service.get_task(task.id).extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_id"], "20260810_010203_a1b2c3")
        self.assertEqual(session["session_state"], "needs_recovery")

    def test_mismatched_resume_receipt_is_rejected_without_overwrite(self) -> None:
        service = self._service()
        task = self._task(service, "codex")
        raw = service.store.read_task(task.id)
        raw["extensions"][SESSION_EXTENSION_KEY].update(
            {
                "session_id": "019feed0-0000-7000-8000-000000000001",
                "session_state": "needs_recovery",
                "run_ids": ["codex-run-1"],
            }
        )
        service.store.write_task(task.id, raw)
        service.start_task_run(task.id, "codex")
        service.record_executor_run_started(task.id, "codex-run-2")

        with self.assertRaisesRegex(ABCError, "authoritative task session"):
            service.validate_executor_session_result(
                task.id,
                "codex-run-2",
                self._receipt(
                    "codex",
                    "019feed0-0000-7000-8000-000000000002",
                    resumed=True,
                ),
            )
        session = service.get_task(task.id).extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_id"], "019feed0-0000-7000-8000-000000000001")

    def test_fresh_registration_freezes_false_and_duplicate_is_idempotent(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        service.start_task_run(task.id, "hermes")
        first = service.record_executor_run_started(task.id, "hermes-fresh-1")
        duplicate = service.record_executor_run_started(task.id, "hermes-fresh-1")
        self.assertFalse(first["resumed"])
        self.assertFalse(duplicate["resumed"])
        session = service.get_task(task.id).extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["run_ids"], ["hermes-fresh-1"])
        self.assertEqual(session["run_resume_facts"], {"hermes-fresh-1": False})

    def test_genuine_resume_uses_frozen_official_session_for_command(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        service.start_task_run(task.id, "hermes")
        first = service.record_executor_run_started(task.id, "hermes-resume-1")
        session_id = "20260905_010203_resume"
        receipt = self._receipt("hermes", session_id, resumed=False)
        service.mark_task_needs_recovery(
            task.id,
            "transport_test",
            "first ACP run ended after its official receipt",
            executor_run_id=first["run_id"],
            execution_session=receipt,
        )
        service.requeue_task(task.id)
        service.start_task_run(task.id, "hermes")
        second = service.record_executor_run_started(task.id, "hermes-resume-2")
        self.assertTrue(second["resumed"])
        packet = service.store.read_task(task.id)
        packet["_agentbc_resume_fact"] = {
            "run_id": second["run_id"],
            "resumed": second["resumed"],
            "session_id": session_id,
        }
        self.assertEqual(
            _task_resume_session(
                packet,
                resume_fact=False,
                resume_session_id="",
            ),
            (False, ""),
        )
        self.assertEqual(
            _task_resume_session(
                packet,
                resume_fact=True,
                resume_session_id=session_id,
            ),
            (True, session_id),
        )
        executor = HermesExecutor(command="/bin/echo", transport="direct", quiet=True)
        command = executor._build_command(
            "continue",
            permission={"effective_mode": "full"},
            task_packet=packet,
        )
        self.assertIn("--resume", command)
        self.assertIn(session_id, command)

    def test_missing_resume_receipt_is_rejected_before_new_run(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        raw = service.store.read_task(task.id)
        session = raw["extensions"][SESSION_EXTENSION_KEY]
        session.update(
            {
                "run_ids": ["hermes-missing-receipt"],
                "run_resume_facts": {"hermes-missing-receipt": False},
            }
        )
        service.store.write_task(task.id, raw)
        service.start_task_run(task.id, "hermes")
        with self.assertRaisesRegex(ABCError, "authoritative session ID"):
            service.record_executor_run_started(task.id, "hermes-missing-receipt-2")


if __name__ == "__main__":
    unittest.main()
