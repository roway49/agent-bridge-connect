from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.permission_grants import PERMISSION_GRANT_EXTENSION_KEY, build_permission_grant
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.run_lease import RunLeaseState, create_lease, load_lease, recover_task, save_lease
from agent_bridge_connect.service import PERMISSION_DIALOG_TIMEOUT_RESPONSE, TaskService
import copy as _permission_grant_copy
import tempfile as _permission_grant_tempfile
import unittest as _permission_grant_unittest
from pathlib import Path as _permission_grant_Path
from agent_bridge_connect.execution_contract import validate_callback_payload as _permission_grant_validate_callback_payload
from agent_bridge_connect.execution_policy import apply_resource_input_decision as _permission_grant_apply_resource_input_decision, build_resource_snapshot as _permission_grant_build_resource_snapshot, is_resource_decision_request as _permission_grant_is_resource_decision_request
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY as _permission_grant_PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant as _permission_grant_build_permission_grant,
    consume_permission_grant as _permission_grant_consume_permission_grant,
    permission_grant_from_extensions as _permission_grant_permission_grant_from_extensions,
    permission_grant_public_projection as _permission_grant_permission_grant_public_projection,
    revoke_permission_grant as _permission_grant_revoke_permission_grant,
    validate_permission_grant as _permission_grant_validate_permission_grant,
)
from agent_bridge_connect.protocol import ABCError as _permission_grant_ABCError
from agent_bridge_connect.service import TaskService as _permission_grant_TaskService

RECEIPT_SOURCE = {"codex": "jsonl_thread_started", "claude": "preallocated", "hermes": "stderr_receipt"}
DEFAULT_SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"
RECOVERY_CODE = "permission_resume_session_unavailable"


class PermissionLifecycleHarness:
    """Fake executor harness that drives a task to a permission wait or recovery."""

    def __init__(self, executor: str, permission_mode: str | None = "safe") -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        config = {"workspace_root": str(self.root)}
        if permission_mode is not None:
            config["permission_mode"] = permission_mode
        self.service = TaskService(self.board, config=config)
        self.executor = executor
        self.permission_mode = permission_mode

    def close(self) -> None:
        self.temp.cleanup()

    def create_task(self):
        return self.service.create_task("permission lifecycle", self.executor, [{"id": 1, "description": "one step"}], customer_dir=True, customer_path=self.project, permission_mode=self.permission_mode)

    @staticmethod
    def _session_id(task) -> str:
        session = (task.extensions or {}).get(SESSION_EXTENSION_KEY) or {}
        return str(session.get("session_id") or "")

    def prepare_wait(self, *, receipt: dict | None | bool = True, run_id: str | None = None, callback_run_id: str | None = None, session_override: str | None = None) -> tuple[str, str, str]:
        """Return (task_id, run_id, receipt_session_id)."""
        service = self.service
        task = self.create_task()
        service.start_task_run(task.id, self.executor)
        resolved_run_id = run_id or f"{self.executor}-permission-run-1"
        service.record_executor_run_started(task.id, resolved_run_id)
        lease = create_lease(task.id, self.executor, 0, str(self.project))
        lease.run_id = resolved_run_id
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, service.board_root)
        existing_session_id = self._session_id(task)
        receipt_session_id = session_override or existing_session_id or DEFAULT_SESSION_ID
        receipt_payload = None
        if receipt is True:
            receipt_payload = {"version": 1, "executor": self.executor, "session_id": receipt_session_id, "resumed": False, "persistence": "persistent", "source": RECEIPT_SOURCE[self.executor]}
        elif isinstance(receipt, dict):
            receipt_payload = receipt
        finalize_run_id = callback_run_id or resolved_run_id
        callback = {
            "version": 1,
            "task_id": task.id,
            "final_state": "input_required",
            "summary": "requesting full permission",
            "executor_run_id": finalize_run_id,
            "input": {"type": "permission", "requested_permission": "full", "reason": "The next continuation needs temporary full permission."},
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        service.finalize_task_from_executor_exit(task.id, executor_run_id=finalize_run_id, callback=callback, execution_session=receipt_payload)
        return (task.id, resolved_run_id, receipt_session_id)

    def resume_without_receipt(self, task_id: str, *, resume_run_id: str | None = None) -> str:
        """Simulate a resumed run whose result carries no execution_session.

        The task already has an authoritative persisted session (from a prior
        valid receipt). The current run starts and then exits with a permission
        input_required callback but no current execution_session receipt.
        Returns the resumed run id.
        """
        service = self.service
        run2 = resume_run_id or f"{self.executor}-permission-run-2"
        service.record_executor_run_started(task_id, run2)
        lease = create_lease(task_id, self.executor, 0, str(self.project))
        lease.run_id = run2
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, service.board_root)
        callback = {
            "version": 1,
            "task_id": task_id,
            "final_state": "input_required",
            "summary": "requesting full permission again",
            "executor_run_id": run2,
            "input": {"type": "permission", "requested_permission": "full", "reason": "The resumed run needs full permission again."},
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        service.finalize_task_from_executor_exit(task_id, executor_run_id=run2, callback=callback, execution_session=None)
        return run2

    def inject_grant(self, task_id: str, input_id: str, session_id: str, source_run_id: str) -> None:
        grant = build_permission_grant(executor=self.executor, task_id=task_id, input_id=input_id, session_id=session_id, source_run_id=source_run_id)
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = grant
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())

    @staticmethod
    def approval_grant(service: TaskService, task_id: str) -> dict:
        return service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]


class PermissionWaitContractTests(unittest.TestCase):
    def test_valid_official_receipt_waits_for_all_executors(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                (task_id, run_id, session_id) = harness.prepare_wait()
                task = harness.service.get_task(task_id)
                self.assertEqual(task.status, "input_required")
                request = task.extensions["agentbc.input"]
                self.assertEqual(request["type"], "permission")
                self.assertEqual(request["requested_permission"], "full")
                self.assertEqual(request["executor_run_id"], run_id)
                self.assertEqual(request["status"], "waiting")
                self.assertEqual(task.extensions[SESSION_EXTENSION_KEY]["session_id"], session_id)
                self.assertEqual(task.extensions[SESSION_EXTENSION_KEY]["session_state"], "input_required")
                self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)

    def test_hermes_missing_receipt_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("hermes")
        self.addCleanup(harness.close)
        (task_id, _, _) = harness.prepare_wait(receipt=None)
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
        self.assertEqual(task.extensions[SESSION_EXTENSION_KEY]["session_state"], "pending")

    def test_hermes_malformed_receipt_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("hermes")
        self.addCleanup(harness.close)
        malformed = {"version": 1, "executor": "hermes", "session_id": DEFAULT_SESSION_ID, "resumed": False, "persistence": "ephemeral", "source": "wrong_source"}
        (task_id, _, _) = harness.prepare_wait(receipt=malformed)
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_stale_persisted_session_without_current_receipt_moves_to_needs_recovery(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                service = harness.service
                (task_id, run1, _) = harness.prepare_wait()
                self.assertEqual(service.get_task(task_id).status, "input_required")
                self.assertTrue((service.get_task(task_id).extensions[SESSION_EXTENSION_KEY] or {}).get("session_id"))
                run2 = harness.resume_without_receipt(task_id)
                self.assertNotEqual(run2, run1)
                task = service.get_task(task_id)
                self.assertEqual(task.status, "needs_recovery")
                self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
                self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
                request = task.extensions.get("agentbc.input")
                if request is not None:
                    self.assertEqual(request.get("executor_run_id"), run1)
                    self.assertNotEqual(request.get("executor_run_id"), run2)


class PermissionResponseTests(unittest.TestCase):
    def _waiting_input(self, service: TaskService, task_id: str) -> dict:
        return service.get_task(task_id).extensions["agentbc.input"]

    def test_approve_issues_one_bound_grant_and_preserves_base_permission(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, run_id, session_id) = harness.prepare_wait()
        before = dict(service.get_task(task_id).extensions[PERMISSION_EXTENSION_KEY])
        request = self._waiting_input(service, task_id)
        result = service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertTrue(result["dispatch_required"])
        self.assertEqual(result["status"], "resuming")
        after = service.get_task(task_id)
        self.assertEqual(after.status, "running")
        self.assertEqual(after.extensions[PERMISSION_EXTENSION_KEY], before)
        answered = after.extensions["agentbc.input"]
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["response"]["type"], "approve")
        self.assertEqual(after.steps[0]["status"], "pending")
        grant = after.extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"], {"executor": "codex", "task_id": task_id, "input_id": request["input_id"], "session_id": session_id, "source_run_id": run_id, "target_run_id": ""})

    def test_deny_fails_task_without_dispatch(self) -> None:
        harness = PermissionLifecycleHarness("claude")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        before = dict(service.get_task(task_id).extensions[PERMISSION_EXTENSION_KEY])
        request = self._waiting_input(service, task_id)
        result = service.respond_to_input(task_id, request["input_id"], response_type="deny")
        self.assertFalse(result["dispatch_required"])
        self.assertTrue(result["permission_denied"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["kind"], "permission_denied_by_user")
        after = service.get_task(task_id)
        self.assertEqual(after.status, "failed")
        self.assertEqual(after.errors[-1]["code"], "permission_denied_by_user")
        self.assertEqual(after.extensions[PERMISSION_EXTENSION_KEY], before)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, after.extensions)
        self.assertEqual(after.extensions["agentbc.input"]["status"], "answered")

    def test_dialog_timeout_auto_denies_permission_without_dispatch(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        result = service.respond_to_input(task_id, request["input_id"], response_type="deny", message=PERMISSION_DIALOG_TIMEOUT_RESPONSE)
        self.assertFalse(result["dispatch_required"])
        self.assertEqual(result["failure"]["kind"], "permission_denied_by_timeout")
        task = service.get_task(task_id)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.errors[-1]["code"], "permission_denied_by_timeout")
        self.assertEqual(task.extensions["agentbc.input"]["response"], {"type": "deny", "summary": "deny", "source": "timeout"})

    def test_permission_input_only_accepts_approve_or_deny(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        with self.assertRaises(ABCError) as raised:
            service.respond_to_input(task_id, request["input_id"], response_type="message", message="hello")
        self.assertEqual(raised.exception.code, "invalid_input_response")

    def test_inherit_waits_on_runtime_block_but_full_is_not_escalatable(self) -> None:
        inherited = PermissionLifecycleHarness("codex", permission_mode="inherit")
        self.addCleanup(inherited.close)
        (task_id, _, _) = inherited.prepare_wait()
        task = inherited.service.get_task(task_id)
        self.assertEqual(task.status, "input_required")
        self.assertEqual(task.extensions["agentbc.input"]["type"], "permission")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
        request = self._waiting_input(inherited.service, task_id)
        inherited.service.respond_to_input(task_id, request["input_id"], response_type="approve")
        grant = inherited.service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["transition"], {"from": "native", "to": "full"})
        full = PermissionLifecycleHarness("codex", permission_mode="full")
        self.addCleanup(full.close)
        (task_id, _, _) = full.prepare_wait()
        task = full.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)

    def test_wrong_session_id_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("claude")
        self.addCleanup(harness.close)
        (task_id, _, _) = harness.prepare_wait(session_override="019feed0-0000-7000-8000-0000000000ff")
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_wrong_source_run_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        (task_id, _, _) = harness.prepare_wait(callback_run_id="codex-stale-run")
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_stale_and_expired_responses_are_rejected(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        with self.assertRaises(ABCError) as stale:
            service.respond_to_input(task_id, "input-stale", response_type="approve")
        self.assertEqual(stale.exception.code, "stale_input")
        task = service.get_task(task_id)
        extensions = dict(task.extensions or {})
        expired_request = dict(extensions["agentbc.input"])
        expired_request["deadline_at"] = "2020-01-01T00:00:00Z"
        extensions["agentbc.input"] = expired_request
        task.extensions = extensions
        service.store.write_task(task_id, task.to_dict())
        with self.assertRaises(ABCError) as expired:
            service.respond_to_input(task_id, expired_request["input_id"], response_type="approve")
        self.assertEqual(expired.exception.code, "input_expired")

    def test_replayed_response_is_idempotent(self) -> None:
        harness = PermissionLifecycleHarness("hermes")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        first = service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertTrue(first["dispatch_required"])
        grant_after_first = dict(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY])
        second = service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertEqual(second["status"], "already_answered")
        self.assertFalse(second["dispatch_required"])
        self.assertEqual(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY], grant_after_first)

    def test_concurrent_active_executor_blocks_response(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, run_id, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        lease = load_lease(task_id, service.board_root)
        self.assertIsNotNone(lease)
        lease.state = RunLeaseState.ACTIVE
        save_lease(lease, service.board_root)
        with self.assertRaises(ABCError) as raised:
            service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertEqual(raised.exception.code, "executor_active")
        self.assertEqual(run_id, lease.run_id)


class PermissionGrantRevocationTests(unittest.TestCase):
    def _waiting_input(self, service: TaskService, task_id: str) -> dict:
        return service.get_task(task_id).extensions["agentbc.input"]

    def test_terminal_finalization_revokes_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertEqual(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"], "issued")
        workspace = service.get_task(task_id).workspace or {}
        callback = {"version": 1, "task_id": task_id, "final_state": "completed", "summary": "completed after approval", "report_file": workspace["report_file"], "step_results": [{"id": 1, "status": "done"}]}
        self.assertTrue(service.finalize_task_from_agent(task_id, callback))
        task = service.get_task(task_id)
        self.assertEqual(task.status, "completed")
        grant = task.extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "task_terminal")

    def test_failed_task_revokes_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        service.mark_task_failed(task_id, "executor_crash", "executor crashed")
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "executor_crash")

    def test_needs_recovery_and_recover_path_revoke_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, run_id, session_id) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertTrue(service.mark_task_needs_recovery(task_id, "executor_recovery_required", "executor must be recovered", {"executor_run_id": run_id}))
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "executor_recovery_required")
        harness.inject_grant(task_id, request["input_id"], session_id, run_id)
        self.assertEqual(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"], "issued")
        recover_task(task_id, service.board_root)
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "task_recover")

    def test_recover_fails_closed_when_grant_revocation_cannot_persist(self) -> None:
        from unittest import mock
        from agent_bridge_connect.task_store import TaskStore

        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, run_id, session_id) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        service.mark_task_needs_recovery(task_id, "executor_recovery_required", "executor must be recovered", {"executor_run_id": run_id})
        harness.inject_grant(task_id, request["input_id"], session_id, run_id)
        self.assertEqual(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"], "issued")

        def failing_write(self, task_id_: str, data: dict) -> None:
            raise OSError("simulated durable write failure")

        with mock.patch.object(TaskStore, "write_task", new=failing_write):
            result = recover_task(task_id, service.board_root)
        self.assertEqual(result["recovery_status"], "failed")
        self.assertEqual(result["status"], "needs_recovery")
        self.assertEqual(result["error"]["code"], "permission_grant_revocation_persist_failed")
        task = service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        run_lease_meta = task.extensions.get("run_lease")
        if run_lease_meta is not None:
            self.assertNotEqual(run_lease_meta.get("recovery_status"), "ready_for_retry")
        events = service.store.read_events(task_id)
        self.assertEqual(events[-1]["event_type"], "task.recovery_failed")

    def test_cancel_revokes_grant(self) -> None:
        harness = PermissionLifecycleHarness("hermes")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        service.cancel_task(task_id)
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "task_cancelled")

    def test_reassign_revokes_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        service.pause_task(task_id, "pausing for reassignment")
        service.reassign_task(task_id, "claude")
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "task_reassign")

    def test_retry_step_revokes_grant(self) -> None:
        harness = PermissionLifecycleHarness("claude")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        service.retry_step(task_id, 1)
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "task_retry")

    def test_input_expiry_and_superseded_input_revoke_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, run_id, session_id) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        task = service.get_task(task_id)
        extensions = dict(task.extensions or {})
        expired_request = dict(extensions["agentbc.input"])
        expired_request["deadline_at"] = "2020-01-01T00:00:00Z"
        extensions["agentbc.input"] = expired_request
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = build_permission_grant(executor="codex", task_id=task_id, input_id=request["input_id"], session_id=session_id, source_run_id=run_id)
        task.extensions = extensions
        service.store.write_task(task_id, task.to_dict())
        expired = service.expire_waiting_inputs()
        self.assertEqual([item["task_id"] for item in expired], [task_id])
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "permission_denied_by_timeout")
        expired_task = service.get_task(task_id)
        self.assertEqual(expired_task.status, "failed")
        self.assertEqual(expired_task.errors[-1]["code"], "permission_denied_by_timeout")
        harness2 = PermissionLifecycleHarness("codex")
        self.addCleanup(harness2.close)
        service2 = harness2.service
        (task_id2, run_id2, _) = harness2.prepare_wait()
        request2 = self._waiting_input(service2, task_id2)
        service2.respond_to_input(task_id2, request2["input_id"], response_type="approve")
        callback = {"version": 1, "task_id": task_id2, "final_state": "input_required", "summary": "need a message response", "executor_run_id": run_id2, "input": {"type": "message", "reason": "provide feedback"}, "step_results": [{"id": 1, "status": "blocked"}]}
        self.assertTrue(service2.finalize_task_from_agent(task_id2, callback))
        grant2 = service2.get_task(task_id2).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant2["state"]["status"], "revoked")
        self.assertEqual(grant2["audit"]["revocation_code"], "input_superseded")

    def test_repeated_revocation_is_idempotent(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertTrue(service.revoke_permission_grant(task_id, "task_terminal"))
        revoked = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(revoked["state"]["status"], "revoked")
        self.assertEqual(revoked["audit"]["revocation_code"], "task_terminal")
        self.assertTrue(service.revoke_permission_grant(task_id, "task_terminal"))
        self.assertEqual(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY], revoked)
        self.assertFalse(service.revoke_permission_grant(task_id, "task_cancelled"))
        fresh = harness.create_task()
        self.assertFalse(service.revoke_permission_grant(fresh.id, "task_terminal"))

    def test_handoff_and_new_task_do_not_inherit_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        (task_id, _, _) = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        workspace = service.get_task(task_id).workspace or {}
        callback = {"version": 1, "task_id": task_id, "final_state": "completed", "summary": "completed before handoff", "report_file": workspace["report_file"], "step_results": [{"id": 1, "status": "done"}]}
        self.assertTrue(service.finalize_task_from_agent(task_id, callback))
        target = service.handoff_task(task_id, "claude", "continue from here")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, target.extensions)
        fresh = harness.create_task()
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, fresh.extensions)


_permission_grant_ISSUED_AT = "2026-08-12T00:00:00Z"
_permission_grant_CONSUMED_AT = "2026-08-12T00:01:00Z"
_permission_grant_REVOKED_AT = "2026-08-12T00:02:00Z"


class _permission_grant_PermissionGrantContractTests(_permission_grant_unittest.TestCase):
    def _grant(self) -> dict:
        return _permission_grant_build_permission_grant(executor="codex", task_id="TEST-001", input_id="input-abc123", session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a", source_run_id="codex-run-1", grant_id="grant-abc123", issued_at=_permission_grant_ISSUED_AT)

    def test_builder_freezes_v1_safe_full_single_use_contract(self) -> None:
        grant = self._grant()
        self.assertEqual(grant["version"], 1)
        self.assertEqual(grant["transition"], {"from": "safe", "to": "full"})
        self.assertEqual(grant["binding"], {"executor": "codex", "task_id": "TEST-001", "input_id": "input-abc123", "session_id": "019fe6f1-3ff9-76e3-8001-52b6c0ae357a", "source_run_id": "codex-run-1", "target_run_id": ""})
        self.assertEqual(grant["scope"], {"kind": "next_executor_run", "max_uses": 1})
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["audit"]["source"], "permission_input")
        self.assertEqual(_permission_grant_permission_grant_from_extensions({_permission_grant_PERMISSION_GRANT_EXTENSION_KEY: grant}, executor="codex", task_id="TEST-001", input_id="input-abc123", session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a", source_run_id="codex-run-1"), grant)
        self.assertIsNone(_permission_grant_permission_grant_from_extensions({}))

    def test_validator_preserves_unknown_additive_fields_losslessly(self) -> None:
        grant = self._grant()
        grant["vendor_extension"] = {"revision": 2}
        grant["binding"]["future_binding"] = "opaque-1"
        grant["scope"]["future_scope"] = {"enabled": True}
        grant["state"]["future_state"] = ["a", "b"]
        grant["audit"]["future_audit"] = "opaque"
        original = _permission_grant_copy.deepcopy(grant)
        validated = _permission_grant_validate_permission_grant(grant)
        consumed = _permission_grant_consume_permission_grant(validated, "codex-run-2", consumed_at=_permission_grant_CONSUMED_AT)
        revoked = _permission_grant_revoke_permission_grant(consumed, "task_terminal", revoked_at=_permission_grant_REVOKED_AT)
        self.assertEqual(validated, original)
        self.assertEqual(revoked["vendor_extension"], original["vendor_extension"])
        self.assertEqual(revoked["binding"]["future_binding"], "opaque-1")
        self.assertEqual(revoked["scope"]["future_scope"], {"enabled": True})
        self.assertEqual(revoked["state"]["future_state"], ["a", "b"])
        self.assertEqual(revoked["audit"]["future_audit"], "opaque")

    def test_future_version_and_sensitive_additions_fail_closed(self) -> None:
        future = self._grant()
        future["version"] = 2
        with self.assertRaises(_permission_grant_ABCError) as version_error:
            _permission_grant_validate_permission_grant(future)
        self.assertEqual(version_error.exception.code, "permission_grant_version_unsupported")
        sensitive_cases = (("raw_command", "git commit"), ("executor_output", "success"), ("prompt", "customer request"), ("secret", "hidden"), ("private_database_path", "/Users/customer/private.sqlite"), ("session_content", "private transcript"), ("future_note", "token=hidden"))
        for field, value in sensitive_cases:
            with self.subTest(field=field):
                grant = self._grant()
                grant[field] = value
                with self.assertRaises(_permission_grant_ABCError) as raised:
                    _permission_grant_validate_permission_grant(grant)
                self.assertEqual(raised.exception.code, "permission_grant_sensitive_field")

    def test_only_approval_capable_base_to_full_next_run_and_one_use_are_valid(self) -> None:
        inherited = _permission_grant_build_permission_grant(executor="codex", task_id="TEST-001", input_id="input-abc123", session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a", source_run_id="codex-run-1", base_mode="native", grant_id="grant-inherit", issued_at=_permission_grant_ISSUED_AT)
        self.assertEqual(_permission_grant_validate_permission_grant(inherited)["transition"], {"from": "native", "to": "full"})
        invalid = []
        from_full = self._grant()
        from_full["transition"]["from"] = "full"
        invalid.append(from_full)
        to_safe = self._grant()
        to_safe["transition"]["to"] = "safe"
        invalid.append(to_safe)
        wrong_scope = self._grant()
        wrong_scope["scope"]["kind"] = "all_future_runs"
        invalid.append(wrong_scope)
        multiple_uses = self._grant()
        multiple_uses["scope"]["max_uses"] = 2
        invalid.append(multiple_uses)
        for grant in invalid:
            with self.subTest(grant=grant), self.assertRaises(_permission_grant_ABCError):
                _permission_grant_validate_permission_grant(grant)

    def test_binding_expectations_cover_identity_source_and_consumed_target(self) -> None:
        grant = self._grant()
        validated = _permission_grant_validate_permission_grant(grant, executor="codex", task_id="TEST-001", input_id="input-abc123", session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a", source_run_id="codex-run-1", target_run_id="codex-run-2")
        self.assertEqual(validated, grant)
        mismatches = {"executor": "claude", "task_id": "OTHER-001", "input_id": "input-other", "session_id": "session-other", "source_run_id": "codex-run-other"}
        for field, value in mismatches.items():
            with self.subTest(field=field), self.assertRaises(_permission_grant_ABCError) as raised:
                _permission_grant_validate_permission_grant(grant, **{field: value})
            self.assertEqual(raised.exception.code, "permission_grant_binding_mismatch")
        consumed = _permission_grant_consume_permission_grant(grant, "codex-run-2", consumed_at=_permission_grant_CONSUMED_AT)
        self.assertEqual(_permission_grant_validate_permission_grant(consumed, target_run_id="codex-run-2"), consumed)
        with self.assertRaises(_permission_grant_ABCError) as target_mismatch:
            _permission_grant_validate_permission_grant(consumed, target_run_id="codex-run-other")
        self.assertEqual(target_mismatch.exception.code, "permission_grant_binding_mismatch")

    def test_source_and_target_run_fields_are_required_by_state(self) -> None:
        missing_source = self._grant()
        del missing_source["binding"]["source_run_id"]
        with self.assertRaises(_permission_grant_ABCError) as source_error:
            _permission_grant_validate_permission_grant(missing_source)
        self.assertEqual(source_error.exception.code, "permission_grant_binding_invalid")
        missing_target = self._grant()
        del missing_target["binding"]["target_run_id"]
        with self.assertRaises(_permission_grant_ABCError) as target_error:
            _permission_grant_validate_permission_grant(missing_target)
        self.assertEqual(target_error.exception.code, "permission_grant_binding_invalid")
        prematurely_bound = self._grant()
        prematurely_bound["binding"]["target_run_id"] = "codex-run-2"
        with self.assertRaises(_permission_grant_ABCError) as issued_error:
            _permission_grant_validate_permission_grant(prematurely_bound)
        self.assertEqual(issued_error.exception.code, "permission_grant_state_invalid")

    def test_consume_and_revoke_are_idempotent_but_replays_fail(self) -> None:
        grant = self._grant()
        directly_revoked = _permission_grant_revoke_permission_grant(grant, "input_expired", revoked_at=_permission_grant_REVOKED_AT)
        self.assertEqual(directly_revoked["state"], {"status": "revoked", "uses": 0})
        self.assertEqual(directly_revoked["binding"]["source_run_id"], "codex-run-1")
        self.assertEqual(directly_revoked["binding"]["target_run_id"], "")
        self.assertEqual(_permission_grant_revoke_permission_grant(directly_revoked, "input_expired", revoked_at=_permission_grant_REVOKED_AT), directly_revoked)
        consumed = _permission_grant_consume_permission_grant(grant, "codex-run-2", consumed_at=_permission_grant_CONSUMED_AT)
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(consumed["state"], {"status": "consumed", "uses": 1})
        self.assertEqual(consumed["binding"]["source_run_id"], "codex-run-1")
        self.assertEqual(consumed["binding"]["target_run_id"], "codex-run-2")
        self.assertEqual(_permission_grant_consume_permission_grant(consumed, "codex-run-2", consumed_at=_permission_grant_REVOKED_AT), consumed)
        with self.assertRaises(_permission_grant_ABCError) as replay:
            _permission_grant_consume_permission_grant(consumed, "codex-run-3", consumed_at=_permission_grant_REVOKED_AT)
        self.assertEqual(replay.exception.code, "permission_grant_replay")
        revoked = _permission_grant_revoke_permission_grant(consumed, "task_terminal", revoked_at=_permission_grant_REVOKED_AT)
        self.assertEqual(_permission_grant_revoke_permission_grant(revoked, "task_terminal", revoked_at=_permission_grant_REVOKED_AT), revoked)
        with self.assertRaises(_permission_grant_ABCError) as changed_reason:
            _permission_grant_revoke_permission_grant(revoked, "dispatch_failed", revoked_at=_permission_grant_REVOKED_AT)
        self.assertEqual(changed_reason.exception.code, "permission_grant_replay")
        with self.assertRaises(_permission_grant_ABCError) as revoked_replay:
            _permission_grant_consume_permission_grant(revoked, "codex-run-2", consumed_at=_permission_grant_REVOKED_AT)
        self.assertEqual(revoked_replay.exception.code, "permission_grant_replay")

    def test_public_projection_exposes_no_binding_or_internal_identifiers(self) -> None:
        projection = _permission_grant_permission_grant_public_projection(self._grant())
        self.assertEqual(
            projection,
            {"version": 1, "temporary": True, "active": True, "source": "permission_input", "from_mode": "safe", "to_mode": "full", "scope": "next_executor_run", "max_uses": 1, "state": "issued", "uses": 0, "reason_code": "", "issued_at": _permission_grant_ISSUED_AT, "consumed_at": "", "revoked_at": ""},
        )
        serialized = repr(projection)
        self.assertNotIn("TEST-001", serialized)
        self.assertNotIn("input-abc123", serialized)
        self.assertNotIn("019fe6f1", serialized)
        self.assertNotIn("grant-abc123", serialized)
        self.assertNotIn("codex-run-1", serialized)
        consumed = _permission_grant_consume_permission_grant(self._grant(), "codex-run-2", consumed_at=_permission_grant_CONSUMED_AT)
        consumed_projection = _permission_grant_permission_grant_public_projection(consumed)
        self.assertNotIn("source_run_id", consumed_projection)
        self.assertNotIn("target_run_id", consumed_projection)
        self.assertNotIn("codex-run-1", repr(consumed_projection))
        self.assertNotIn("codex-run-2", repr(consumed_projection))
        revoked = _permission_grant_revoke_permission_grant(consumed, "task_terminal", revoked_at=_permission_grant_REVOKED_AT)
        revoked_projection = _permission_grant_permission_grant_public_projection(revoked)
        self.assertEqual(revoked_projection["state"], "revoked")
        self.assertFalse(revoked_projection["active"])
        self.assertEqual(revoked_projection["reason_code"], "task_terminal")
        self.assertEqual(revoked_projection["uses"], 1)
        self.assertEqual(revoked_projection["consumed_at"], _permission_grant_CONSUMED_AT)
        self.assertEqual(revoked_projection["revoked_at"], _permission_grant_REVOKED_AT)


class _permission_grant_PermissionCallbackContractTests(_permission_grant_unittest.TestCase):
    declared_steps = [{"id": 1, "description": "first"}, {"id": 2, "description": "second"}]

    def _callback(self, *, input_details: dict | None, step_results: list[dict] | None = None, **extra) -> dict:
        callback = {"version": 1, "task_id": "TEST-001", "final_state": "input_required", "summary": "blocked", "step_results": step_results if step_results is not None else [{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}]}
        if input_details is not None:
            callback["input"] = input_details
        callback.update(extra)
        return callback

    def _validate(self, callback: dict):
        return _permission_grant_validate_callback_payload(callback, "TEST-001", self.declared_steps)

    def test_valid_permission_marker_requires_full_reason_and_one_blocked_step(self) -> None:
        validation = self._validate(self._callback(input_details={"type": "permission", "requested_permission": " full ", "reason": "The next continuation must update protected Git metadata."}))
        self.assertTrue(validation.valid)
        self.assertEqual(validation.callback["input"], {"type": "permission", "requested_permission": "full", "reason": "The next continuation must update protected Git metadata."})

    def test_permission_reason_is_safely_truncated_instead_of_losing_the_wait(self) -> None:
        exact = "x" * 240
        exact_validation = self._validate(self._callback(input_details={"type": "permission", "requested_permission": "full", "reason": exact}))
        self.assertTrue(exact_validation.valid)
        self.assertEqual(exact_validation.callback["input"]["reason"], exact)
        overlong = "x" * 240 + "never persist this tail"
        overlong_validation = self._validate(self._callback(input_details={"type": "permission", "requested_permission": "full", "reason": overlong}))
        self.assertTrue(overlong_validation.valid)
        reason = overlong_validation.callback["input"]["reason"]
        self.assertEqual(len(reason), 240)
        self.assertEqual(reason, "x" * 239 + "…")
        self.assertNotIn("never persist this tail", str(overlong_validation.callback))

    def test_permission_marker_rejects_missing_reason_requested_safe_and_block_count(self) -> None:
        cases = {
            "missing_reason": self._callback(input_details={"type": "permission", "requested_permission": "full"}),
            "requested_safe": self._callback(input_details={"type": "permission", "requested_permission": "safe", "reason": "Full access is required."}),
            "zero_blocked": self._callback(input_details={"type": "permission", "requested_permission": "full", "reason": "Full access is required."}, step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}]),
            "multiple_blocked": self._callback(input_details={"type": "permission", "requested_permission": "full", "reason": "Full access is required."}, step_results=[{"id": 1, "status": "blocked"}, {"id": 2, "status": "blocked"}]),
            "native_flags": self._callback(input_details={"type": "permission", "requested_permission": "full", "reason": "Full access is required.", "native_executor_flags": ["--yolo"]}),
            "callback_level": self._callback(input_details={"type": "permission", "reason": "Full access is required."}, requested_permission="full"),
        }
        for name, callback in cases.items():
            with self.subTest(name=name):
                self.assertFalse(self._validate(callback).valid)

    def test_message_and_choice_fields_do_not_become_permission_or_resource_grants(self) -> None:
        message = self._validate(self._callback(input_details={"type": "message", "requested_permission": "full"}, requested_permission="full"))
        self.assertTrue(message.valid)
        self.assertEqual(message.callback["input"]["type"], "message")
        self.assertNotIn(_permission_grant_PERMISSION_GRANT_EXTENSION_KEY, message.callback)
        choice = self._validate(
            self._callback(input_details={"type": "choice", "reason": "Choose whether to continue.", "requested_permission": "full", "kind": "resource_limit", "options": [{"label": "Continue", "description": "Continue normally."}, {"label": "Stop", "description": "Stop this task."}]})
        )
        self.assertTrue(choice.valid)
        self.assertFalse(_permission_grant_is_resource_decision_request(choice.callback["input"]))
        self.assertNotIn(_permission_grant_PERMISSION_GRANT_EXTENSION_KEY, choice.callback)

    def test_callback_level_permission_text_is_not_persisted_as_permission_request(self) -> None:
        with _permission_grant_tempfile.TemporaryDirectory() as temporary:
            root = _permission_grant_Path(temporary)
            project = root / "project"
            project.mkdir()
            service = _permission_grant_TaskService(root / "record", config={"workspace_root": str(root)})
            task = service.create_task("permission masquerade", "codex", [{"description": "first"}, {"description": "second"}], customer_path=project)
            callback = self._callback(input_details={"type": "message"}, requested_permission="full")
            callback["task_id"] = task.id
            service.finalize_task_from_agent(task.id, callback)
            request = service.get_task(task.id).extensions["agentbc.input"]
            self.assertEqual(request["type"], "message")
            self.assertNotIn("requested_permission", request)
            self.assertNotIn(_permission_grant_PERMISSION_GRANT_EXTENSION_KEY, service.get_task(task.id).extensions)


class _permission_grant_ResourceDecisionRecognitionTests(_permission_grant_unittest.TestCase):
    def test_only_complete_choice_kind_protocol_tuple_reaches_resource_logic(self) -> None:
        complete = {"type": "choice", "kind": "resource_limit", "response_protocol": "approve_deny"}
        self.assertTrue(_permission_grant_is_resource_decision_request(complete))
        masquerades = ({**complete, "type": "message"}, {**complete, "type": "permission"}, {"type": "choice", "kind": "resource_limit"}, {"type": "choice", "response_protocol": "approve_deny"}, {"kind": "resource_limit", "response_protocol": "approve_deny"})
        resource = _permission_grant_build_resource_snapshot("hermes", 60, source="test", created_at=_permission_grant_ISSUED_AT)
        for request in masquerades:
            with self.subTest(request=request):
                self.assertFalse(_permission_grant_is_resource_decision_request(request))
                with self.assertRaises(_permission_grant_ABCError) as raised:
                    _permission_grant_apply_resource_input_decision(resource, {**request, "current_limit": 60, "next_limit": 120}, "approve", executor="hermes")
                self.assertEqual(raised.exception.code, "resource_decision_invalid")
