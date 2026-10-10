"""SESSION-103-002 E2E/canary session supervisor tests.

Covers receipt-driven create/bind journaling before test actions, try/finally
plus SIGINT/SIGTERM/KeyboardInterrupt teardown, success/Deny/timeout/transport
loss/crash/Runner-restart paths, bounded retries, immutable retain, public
redaction, idempotent delete replay, dispatcher exclusion, and task-attached
auxiliary ledger integration.
"""

from __future__ import annotations
import copy
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from agent_bridge_connect.adapters import SessionCleanupResult
from agent_bridge_connect.auxiliary_sessions import AUXILIARY_EXTENSION_KEY, read_auxiliary_ledger
from agent_bridge_connect.e2e_session_supervisor import E2ESessionSupervisor
from agent_bridge_connect.service import TaskService
import tempfile as _session_lifecycle_tempfile
import unittest as _session_lifecycle_unittest
from pathlib import Path as _session_lifecycle_Path
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY as _session_lifecycle_SESSION_EXTENSION_KEY
from agent_bridge_connect.executors.hermes import HermesExecutor as _session_lifecycle_HermesExecutor, _task_resume_session as _session_lifecycle__task_resume_session
from agent_bridge_connect.protocol import ABCError as _session_lifecycle_ABCError
from agent_bridge_connect.service import TaskService as _session_lifecycle_TaskService

T0 = "2026-08-25T00:00:00Z"


class FakeCleanupPort:
    """Configurable official exact-session cleanup port."""

    def __init__(self, result: SessionCleanupResult | None = None) -> None:
        self.result = result or SessionCleanupResult(state="succeeded", capability="supported", strategy="official_session_delete")
        self.calls: list = []
        self.raise_error: BaseException | None = None
        self.not_found_ids: set = set()

    def cleanup_session(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.raise_error is not None:
            raise self.raise_error
        if request.session_id in self.not_found_ids:
            return SessionCleanupResult(state="succeeded", capability="supported", strategy="official_session_delete", error_code="")
        return self.result


def _creator(executor: str, session_id: str, source: str):

    def _create():
        return {"version": 1, "executor": executor, "session_id": session_id, "resumed": False, "persistence": "persistent", "source": source}

    return _create


class E2ESessionSupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = self.root / "journal.jsonl"
        self.port = FakeCleanupPort()
        self.supervisor = E2ESessionSupervisor(task_id="E2E-001", run_id="run-1", retain=False, journal_path=self.journal, cleanup_port=self.port)

    def _journal(self) -> list[dict]:
        return self.supervisor.journal.latest_entries()

    def test_success_path_journals_receipt_before_actions_and_tears_down(self) -> None:
        created_at_before = None
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-1", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
            latest = self._journal()[-1]
            self.assertEqual(latest["session_id"], "SESS-1")
            self.assertEqual(latest["session_state"], "active")
            self.assertTrue(handle.ref.startswith("sess_"))
            handle.mark_terminal()
            created_at_before = len(self.port.calls)
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-1")
        self.assertEqual(self.port.calls[0].executor, "hermes")
        latest = self._journal()[-1]
        self.assertEqual(latest["cleanup"]["state"], "succeeded")
        self.assertEqual(latest["cleanup"]["attempts"], 1)
        self.assertGreater(len(self._journal()), created_at_before)

    def test_deny_and_process_exception_still_reach_teardown(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.supervisor.guarded(install_signals=False):
                self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-DENY", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
                raise RuntimeError("user denied the permission")
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-DENY")

    def test_creator_failure_never_binds_or_cleans_a_session(self) -> None:

        def _failing_creator():
            raise RuntimeError("transport lost during thread/start")

        with self.assertRaises(RuntimeError):
            with self.supervisor.guarded(install_signals=False):
                self.supervisor.open_session("hermes", "canary_probe", _failing_creator, parent_executor="claude", parent_session_id="PARENT-1")
        self.assertEqual(self.port.calls, [])
        entries = self._journal()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_state"], "reserved")
        self.assertEqual(entries[0]["session_id"], "")

    def test_test_interruption_via_keyboard_interrupt_reaches_teardown(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with self.supervisor.guarded(install_signals=False):
                self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-INT", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
                raise KeyboardInterrupt
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-INT")

    def test_sigterm_is_translated_to_interrupt_and_tears_down(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with self.supervisor.guarded(install_signals=True):
                self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-SIG", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
                os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-SIG")

    def test_cleanup_exception_is_journaled_as_retryable_failure(self) -> None:
        self.port.raise_error = RuntimeError("transport loss during delete")
        with self.supervisor.guarded(install_signals=False):
            self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-BUSY", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
        latest = self._journal()[-1]
        self.assertEqual(latest["cleanup"]["state"], "failed")
        self.assertEqual(latest["cleanup"]["error_code"], "session_cleanup_failed")
        self.assertTrue(latest["cleanup"]["retryable"])
        restarted = E2ESessionSupervisor(task_id="E2E-001", run_id="run-1", retain=False, journal_path=self.journal, cleanup_port=self.port)
        self.port.raise_error = None
        self.port.result = SessionCleanupResult(state="succeeded", capability="supported", strategy="official_session_delete")
        replayed = restarted.replay_unresolved()
        self.assertEqual(len(replayed), 1)
        self.assertEqual(replayed[0]["status"], "succeeded")
        self.assertEqual(len(self.port.calls), 2)

    def test_restart_replays_only_own_unresolved_receipts_before_new_session(self) -> None:
        self.port.result = SessionCleanupResult(state="failed", capability="supported", strategy="official_session_delete", error_code="session_delete_busy", retryable=True)
        with self.supervisor.guarded(install_signals=False):
            self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-UNRESOLVED", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
        self.assertEqual(self._journal()[-1]["cleanup"]["state"], "failed")
        restarted = E2ESessionSupervisor(task_id="E2E-001", run_id="run-1", retain=False, journal_path=self.journal, cleanup_port=self.port)
        self.port.result = SessionCleanupResult(state="succeeded", capability="supported", strategy="official_session_delete")
        replayed = restarted.replay_unresolved()
        self.assertEqual(len(replayed), 1)
        self.assertTrue(replayed[0]["ref"].startswith("sess_"))
        self.assertEqual(len(self.port.calls), 2)
        with restarted.guarded(install_signals=False):
            handle = restarted.open_session("hermes", "canary_probe", _creator("hermes", "SESS-NEW", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
            handle.mark_terminal()
        self.assertEqual(len(self.port.calls), 3)

    def test_retain_true_is_immutable_and_skips_cleanup(self) -> None:
        retained = E2ESessionSupervisor(task_id="E2E-001", run_id="run-1", retain=True, journal_path=self.root / "retain.jsonl", cleanup_port=self.port)
        with retained.guarded(install_signals=False):
            handle = retained.open_session("hermes", "canary_probe", _creator("hermes", "SESS-RETAIN", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
            self.assertTrue(handle.retain)
            handle.mark_terminal()
        self.assertEqual(self.port.calls, [])
        latest = retained.journal.latest_entries()[-1]
        self.assertEqual(latest["cleanup"]["state"], "retained")
        self.assertEqual(latest["cleanup"]["capability"], "not_applicable")

    def test_public_evidence_uses_only_redacted_refs_and_safe_fields(self) -> None:
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SECRET-SESSION-ID", "stderr_receipt"), parent_executor="claude", parent_session_id="SECRET-PARENT")
            view = handle.public_view()
            handle.mark_terminal()
        serialized = json.dumps(view)
        for sensitive in ("SECRET-SESSION-ID", "SECRET-PARENT", "stderr_receipt"):
            self.assertNotIn(sensitive, serialized)
        self.assertTrue(view["ref"].startswith("sess_"))
        self.assertEqual(view["executor"], "hermes")
        self.assertEqual(view["purpose"], "canary_probe")
        self.assertFalse(view["retain"])

    def test_dispatcher_conversation_is_never_collected_or_deleted(self) -> None:
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-OWN", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
            handle.mark_terminal()
        journal_ids = {entry.get("session_id") for entry in self._journal()}
        self.assertNotIn("dispatcher-conversation-id", journal_ids)
        self.assertEqual(journal_ids, {"SESS-OWN"})
        self.assertEqual([call.session_id for call in self.port.calls], ["SESS-OWN"])

    def test_task_attached_run_mirrors_into_auxiliary_ledger(self) -> None:
        board = self.root / "record"
        service = TaskService(board, config={"workspace_root": str(self.root / "workspace"), "sessions": {"retain_executor_sessions": False}})
        task = service.create_task("supervised canary", "codex", [{"id": 1, "description": "run"}], customer_dir=False)
        task_id = task.id
        attached = E2ESessionSupervisor(task_id=task_id, run_id="run-1", retain=False, journal_path=self.root / "attached.jsonl", board_root=board, cleanup_port=self.port)
        with attached.guarded(install_signals=False):
            handle = attached.open_session("hermes", "canary_probe", _creator("hermes", "SESS-TASK", "stderr_receipt"), parent_executor="codex", parent_session_id="PRIMARY-SESS")
            handle.mark_terminal()
        raw = service.store.read_task(task_id)
        ledger = read_auxiliary_ledger(raw["extensions"])
        self.assertEqual(len(ledger["sessions"]), 1)
        entry = ledger["sessions"][0]
        self.assertEqual(entry["session_id"], "SESS-TASK")
        self.assertEqual(entry["executor"], "hermes")
        self.assertEqual(entry["cleanup"]["state"], "succeeded")
        from agent_bridge_connect.auxiliary_sessions import auxiliary_ledger_view

        view = auxiliary_ledger_view(raw["extensions"][AUXILIARY_EXTENSION_KEY])
        serialized = json.dumps(view)
        self.assertNotIn("SESS-TASK", serialized)
        self.assertNotIn("PRIMARY-SESS", serialized)

    def test_idempotent_delete_replay_never_duplicates_cleanup(self) -> None:
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session("hermes", "canary_probe", _creator("hermes", "SESS-1", "stderr_receipt"), parent_executor="claude", parent_session_id="PARENT-1")
            handle.mark_terminal()
        calls_after_first = len(self.port.calls)
        self.assertEqual(calls_after_first, 1)
        self.supervisor.teardown()
        self.assertEqual(len(self.port.calls), calls_after_first)


class _session_lifecycle_Phase3SessionLifecycleTests(_session_lifecycle_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _session_lifecycle_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _session_lifecycle_Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def _service(self, *, retain: bool = True) -> _session_lifecycle_TaskService:
        return _session_lifecycle_TaskService(self.root / "record", config={"workspace_root": str(self.root / "workspace"), "sessions": {"retain_executor_sessions": retain}})

    def _task(self, service: _session_lifecycle_TaskService, executor: str):
        return service.create_task("phase 3 session lifecycle", executor, [{"id": 1, "description": "continue the same executor session"}], customer_dir=True, customer_path=self.project, permission_mode="inherit")

    @staticmethod
    def _receipt(executor: str, session_id: str, *, resumed: bool) -> dict:
        return {"version": 1, "executor": executor, "session_id": session_id, "resumed": resumed, "persistence": "persistent", "source": {"claude": "preallocated", "hermes": "stderr_receipt", "codex": "jsonl_thread_started"}[executor]}

    def test_claude_input_response_resumes_same_session(self) -> None:
        service = self._service()
        task = self._task(service, "claude")
        session_id = task.extensions[_session_lifecycle_SESSION_EXTENSION_KEY]["session_id"]
        service.start_task_run(task.id, "claude")
        service.record_executor_run_started(task.id, "claude-run-1")
        callback = {"version": 1, "task_id": task.id, "final_state": "input_required", "summary": "Need one answer", "executor_run_id": "claude-run-1", "input": {"type": "message", "reason": "continue"}, "step_results": [{"id": 1, "status": "blocked"}]}
        service.finalize_task_from_executor_exit(task.id, executor_run_id="claude-run-1", callback=callback, execution_session=self._receipt("claude", session_id, resumed=False))
        waiting = service.get_task(task.id)
        session = waiting.extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_state"], "input_required")
        self.assertEqual(session["run_ids"], ["claude-run-1"])
        request = waiting.extensions["agentbc.input"]
        service.respond_to_input(task.id, request["input_id"], response_type="message", message="continue")
        service.start_task_run(task.id, "claude")
        started = service.record_executor_run_started(task.id, "claude-run-2")
        self.assertTrue(started["resumed"])
        completed_callback = {"version": 1, "task_id": task.id, "final_state": "completed", "summary": "continued successfully", "executor_run_id": "claude-run-2", "step_results": [{"id": 1, "status": "done"}]}
        service.finalize_task_from_executor_exit(task.id, executor_run_id="claude-run-2", callback=completed_callback, execution_session=self._receipt("claude", session_id, resumed=True))
        completed = service.get_task(task.id)
        session = completed.extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
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
        first_session = service.get_task(task.id).extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
        self.assertEqual(first_session["run_resume_facts"], {"hermes-run-1": False})
        receipt = self._receipt("hermes", "20260810_010203_a1b2c3", resumed=False)
        service.validate_executor_session_result(task.id, "hermes-run-1", receipt)
        service.mark_task_needs_recovery(task.id, "transport_test", "retry later", executor_run_id="hermes-run-1", execution_session=receipt)
        session = service.get_task(task.id).extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_id"], "20260810_010203_a1b2c3")
        self.assertEqual(session["session_state"], "needs_recovery")

    def test_duplicate_run_registration_preserves_frozen_resume_fact(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        first = service.record_executor_run_started(task.id, "hermes-run-1")
        duplicate = service.record_executor_run_started(task.id, "hermes-run-1")
        self.assertFalse(first["resumed"])
        self.assertFalse(duplicate["resumed"])
        session = service.get_task(task.id).extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
        self.assertEqual(session["run_ids"], ["hermes-run-1"])
        self.assertEqual(session["run_resume_facts"], {"hermes-run-1": False})

    def test_mismatched_resume_receipt_is_rejected_without_overwrite(self) -> None:
        service = self._service()
        task = self._task(service, "codex")
        raw = service.store.read_task(task.id)
        raw["extensions"][_session_lifecycle_SESSION_EXTENSION_KEY].update({"session_id": "019feed0-0000-7000-8000-000000000001", "session_state": "needs_recovery", "run_ids": ["codex-run-1"]})
        service.store.write_task(task.id, raw)
        service.start_task_run(task.id, "codex")
        service.record_executor_run_started(task.id, "codex-run-2")
        with self.assertRaisesRegex(_session_lifecycle_ABCError, "authoritative task session"):
            service.validate_executor_session_result(task.id, "codex-run-2", self._receipt("codex", "019feed0-0000-7000-8000-000000000002", resumed=True))
        session = service.get_task(task.id).extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_id"], "019feed0-0000-7000-8000-000000000001")

    def test_fresh_registration_freezes_false_and_duplicate_is_idempotent(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        service.start_task_run(task.id, "hermes")
        first = service.record_executor_run_started(task.id, "hermes-fresh-1")
        duplicate = service.record_executor_run_started(task.id, "hermes-fresh-1")
        self.assertFalse(first["resumed"])
        self.assertFalse(duplicate["resumed"])
        session = service.get_task(task.id).extensions[_session_lifecycle_SESSION_EXTENSION_KEY]
        self.assertEqual(session["run_ids"], ["hermes-fresh-1"])
        self.assertEqual(session["run_resume_facts"], {"hermes-fresh-1": False})

    def test_genuine_resume_uses_frozen_official_session_for_command(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        service.start_task_run(task.id, "hermes")
        first = service.record_executor_run_started(task.id, "hermes-resume-1")
        session_id = "20260905_010203_resume"
        receipt = self._receipt("hermes", session_id, resumed=False)
        service.mark_task_needs_recovery(task.id, "transport_test", "first ACP run ended after its official receipt", executor_run_id=first["run_id"], execution_session=receipt)
        service.requeue_task(task.id)
        service.start_task_run(task.id, "hermes")
        second = service.record_executor_run_started(task.id, "hermes-resume-2")
        self.assertTrue(second["resumed"])
        packet = service.store.read_task(task.id)
        packet["_agentbc_resume_fact"] = {"run_id": second["run_id"], "resumed": second["resumed"], "session_id": session_id}
        self.assertEqual(_session_lifecycle__task_resume_session(packet, resume_fact=False, resume_session_id=""), (False, ""))
        self.assertEqual(_session_lifecycle__task_resume_session(packet, resume_fact=True, resume_session_id=session_id), (True, session_id))
        executor = _session_lifecycle_HermesExecutor(command="/bin/echo", transport="direct", quiet=True)
        command = executor._build_command("continue", permission={"effective_mode": "full"}, task_packet=packet)
        self.assertIn("--resume", command)
        self.assertIn(session_id, command)

    def test_missing_resume_receipt_is_rejected_before_new_run(self) -> None:
        service = self._service(retain=False)
        task = self._task(service, "hermes")
        raw = service.store.read_task(task.id)
        session = raw["extensions"][_session_lifecycle_SESSION_EXTENSION_KEY]
        session.update({"run_ids": ["hermes-missing-receipt"], "run_resume_facts": {"hermes-missing-receipt": False}})
        service.store.write_task(task.id, raw)
        service.start_task_run(task.id, "hermes")
        with self.assertRaisesRegex(_session_lifecycle_ABCError, "authoritative session ID"):
            service.record_executor_run_started(task.id, "hermes-missing-receipt-2")
