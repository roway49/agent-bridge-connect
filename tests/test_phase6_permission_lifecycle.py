from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
)
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    create_lease,
    load_lease,
    recover_task,
    save_lease,
)
from agent_bridge_connect.service import TaskService


RECEIPT_SOURCE = {
    "codex": "jsonl_thread_started",
    "claude": "preallocated",
    "hermes": "stderr_receipt",
}
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
        return self.service.create_task(
            "permission lifecycle",
            self.executor,
            [{"id": 1, "description": "one step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode=self.permission_mode,
        )

    @staticmethod
    def _session_id(task) -> str:
        session = (task.extensions or {}).get(SESSION_EXTENSION_KEY) or {}
        return str(session.get("session_id") or "")

    def prepare_wait(
        self,
        *,
        receipt: dict | None | bool = True,
        run_id: str | None = None,
        callback_run_id: str | None = None,
        session_override: str | None = None,
    ) -> tuple[str, str, str]:
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
        receipt_session_id = (
            session_override
            or existing_session_id
            or DEFAULT_SESSION_ID
        )
        receipt_payload = None
        if receipt is True:
            receipt_payload = {
                "version": 1,
                "executor": self.executor,
                "session_id": receipt_session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": RECEIPT_SOURCE[self.executor],
            }
        elif isinstance(receipt, dict):
            receipt_payload = receipt

        finalize_run_id = callback_run_id or resolved_run_id
        callback = {
            "version": 1,
            "task_id": task.id,
            "final_state": "input_required",
            "summary": "requesting full permission",
            "executor_run_id": finalize_run_id,
            "input": {
                "type": "permission",
                "requested_permission": "full",
                "reason": "The next continuation needs temporary full permission.",
            },
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        service.finalize_task_from_executor_exit(
            task.id,
            executor_run_id=finalize_run_id,
            callback=callback,
            execution_session=receipt_payload,
        )
        return task.id, resolved_run_id, receipt_session_id

    def resume_without_receipt(
        self,
        task_id: str,
        *,
        resume_run_id: str | None = None,
    ) -> str:
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
            "input": {
                "type": "permission",
                "requested_permission": "full",
                "reason": "The resumed run needs full permission again.",
            },
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        service.finalize_task_from_executor_exit(
            task_id,
            executor_run_id=run2,
            callback=callback,
            execution_session=None,
        )
        return run2

    def inject_grant(self, task_id: str, input_id: str, session_id: str, source_run_id: str) -> None:
        grant = build_permission_grant(
            executor=self.executor,
            task_id=task_id,
            input_id=input_id,
            session_id=session_id,
            source_run_id=source_run_id,
        )
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
                task_id, run_id, session_id = harness.prepare_wait()
                task = harness.service.get_task(task_id)
                self.assertEqual(task.status, "input_required")
                request = task.extensions["agentbc.input"]
                self.assertEqual(request["type"], "permission")
                self.assertEqual(request["requested_permission"], "full")
                self.assertEqual(request["executor_run_id"], run_id)
                self.assertEqual(request["status"], "waiting")
                self.assertEqual(
                    task.extensions[SESSION_EXTENSION_KEY]["session_id"],
                    session_id,
                )
                self.assertEqual(task.extensions[SESSION_EXTENSION_KEY]["session_state"], "input_required")
                self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)

    def test_hermes_missing_receipt_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("hermes")
        self.addCleanup(harness.close)
        task_id, _, _ = harness.prepare_wait(receipt=None)
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
        # Hermes has no authoritative session ID yet, so the pending snapshot
        # cannot be advanced; only the recovery decision is recorded.
        self.assertEqual(task.extensions[SESSION_EXTENSION_KEY]["session_state"], "pending")

    def test_hermes_malformed_receipt_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("hermes")
        self.addCleanup(harness.close)
        malformed = {
            "version": 1,
            "executor": "hermes",
            "session_id": DEFAULT_SESSION_ID,
            "resumed": False,
            "persistence": "ephemeral",
            "source": "wrong_source",
        }
        task_id, _, _ = harness.prepare_wait(receipt=malformed)
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_stale_persisted_session_without_current_receipt_moves_to_needs_recovery(self) -> None:
        # A persisted agentbc.session snapshot is never sufficient on its own:
        # every executor (codex, claude, hermes) must require the current result
        # to carry a valid execution_session receipt for the exact current run.
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                service = harness.service
                task_id, run1, _ = harness.prepare_wait()
                self.assertEqual(service.get_task(task_id).status, "input_required")
                self.assertTrue(
                    (service.get_task(task_id).extensions[SESSION_EXTENSION_KEY] or {}).get(
                        "session_id"
                    )
                )
                run2 = harness.resume_without_receipt(task_id)
                self.assertNotEqual(run2, run1)
                task = service.get_task(task_id)
                self.assertEqual(task.status, "needs_recovery")
                self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
                self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
                # No fresh permission wait for the resumed run was created; any
                # surviving input belongs to the earlier valid run only.
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
        task_id, run_id, session_id = harness.prepare_wait()
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
        self.assertEqual(
            grant["binding"],
            {
                "executor": "codex",
                "task_id": task_id,
                "input_id": request["input_id"],
                "session_id": session_id,
                "source_run_id": run_id,
                "target_run_id": "",
            },
        )

    def test_deny_fails_task_without_dispatch(self) -> None:
        harness = PermissionLifecycleHarness("claude")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, _, _ = harness.prepare_wait()
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

    def test_permission_input_only_accepts_approve_or_deny(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, _, _ = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        with self.assertRaises(ABCError) as raised:
            service.respond_to_input(task_id, request["input_id"], response_type="message", message="hello")
        self.assertEqual(raised.exception.code, "invalid_input_response")

    def test_inherit_and_full_permission_waits_are_rejected(self) -> None:
        for mode in ("inherit", "full"):
            with self.subTest(mode=mode):
                harness = PermissionLifecycleHarness("codex", permission_mode=mode)
                self.addCleanup(harness.close)
                task_id, _, _ = harness.prepare_wait()
                task = harness.service.get_task(task_id)
                self.assertEqual(task.status, "needs_recovery")
                self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
                self.assertNotIn("agentbc.input", task.extensions)
                self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)

    def test_wrong_session_id_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("claude")
        self.addCleanup(harness.close)
        task_id, _, _ = harness.prepare_wait(
            session_override="019feed0-0000-7000-8000-0000000000ff"
        )
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_wrong_source_run_moves_to_needs_recovery(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        task_id, _, _ = harness.prepare_wait(callback_run_id="codex-stale-run")
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], RECOVERY_CODE)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_stale_and_expired_responses_are_rejected(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, _, _ = harness.prepare_wait()

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
        task_id, _, _ = harness.prepare_wait()
        request = self._waiting_input(service, task_id)

        first = service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertTrue(first["dispatch_required"])
        grant_after_first = dict(service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY])
        second = service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertEqual(second["status"], "already_answered")
        self.assertFalse(second["dispatch_required"])
        self.assertEqual(
            service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY],
            grant_after_first,
        )

    def test_concurrent_active_executor_blocks_response(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, run_id, _ = harness.prepare_wait()
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
        task_id, _, _ = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertEqual(
            service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"],
            "issued",
        )
        workspace = service.get_task(task_id).workspace or {}
        callback = {
            "version": 1,
            "task_id": task_id,
            "final_state": "completed",
            "summary": "completed after approval",
            "report_file": workspace["report_file"],
            "step_results": [{"id": 1, "status": "done"}],
        }
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
        task_id, _, _ = harness.prepare_wait()
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
        task_id, run_id, session_id = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")

        self.assertTrue(
            service.mark_task_needs_recovery(
                task_id,
                "executor_recovery_required",
                "executor must be recovered",
                {"executor_run_id": run_id},
            )
        )
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "executor_recovery_required")

        # A grant present at explicit recover time is revoked with task_recover.
        harness.inject_grant(task_id, request["input_id"], session_id, run_id)
        self.assertEqual(
            service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"],
            "issued",
        )
        recover_task(task_id, service.board_root)
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "task_recover")

    def test_recover_fails_closed_when_grant_revocation_cannot_persist(self) -> None:
        # If the durable write of the revoked grant raises OSError, recovery must
        # not mark the task ready for retry while an issued grant may remain live.
        from unittest import mock

        from agent_bridge_connect.task_store import TaskStore

        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, run_id, session_id = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        service.mark_task_needs_recovery(
            task_id,
            "executor_recovery_required",
            "executor must be recovered",
            {"executor_run_id": run_id},
        )
        harness.inject_grant(task_id, request["input_id"], session_id, run_id)
        self.assertEqual(
            service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"],
            "issued",
        )

        def failing_write(self, task_id_: str, data: dict) -> None:
            raise OSError("simulated durable write failure")

        with mock.patch.object(TaskStore, "write_task", new=failing_write):
            result = recover_task(task_id, service.board_root)

        self.assertEqual(result["recovery_status"], "failed")
        self.assertEqual(result["status"], "needs_recovery")
        self.assertEqual(
            result["error"]["code"],
            "permission_grant_revocation_persist_failed",
        )
        # No ready-for-retry transition was persisted, so the task must not be
        # dispatched while the grant is still present (but not live-allowed).
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
        task_id, _, _ = harness.prepare_wait()
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
        task_id, _, _ = harness.prepare_wait()
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
        task_id, _, _ = harness.prepare_wait()
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
        task_id, run_id, session_id = harness.prepare_wait()
        request = self._waiting_input(service, task_id)

        task = service.get_task(task_id)
        extensions = dict(task.extensions or {})
        expired_request = dict(extensions["agentbc.input"])
        expired_request["deadline_at"] = "2020-01-01T00:00:00Z"
        extensions["agentbc.input"] = expired_request
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = build_permission_grant(
            executor="codex",
            task_id=task_id,
            input_id=request["input_id"],
            session_id=session_id,
            source_run_id=run_id,
        )
        task.extensions = extensions
        service.store.write_task(task_id, task.to_dict())

        expired = service.expire_waiting_inputs()
        self.assertEqual([item["task_id"] for item in expired], [task_id])
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "revoked")
        self.assertEqual(grant["audit"]["revocation_code"], "input_expired")

        # A second input_required supersedes and revokes any earlier grant.
        harness2 = PermissionLifecycleHarness("codex")
        self.addCleanup(harness2.close)
        service2 = harness2.service
        task_id2, run_id2, _ = harness2.prepare_wait()
        request2 = self._waiting_input(service2, task_id2)
        service2.respond_to_input(task_id2, request2["input_id"], response_type="approve")
        callback = {
            "version": 1,
            "task_id": task_id2,
            "final_state": "input_required",
            "summary": "need a message response",
            "executor_run_id": run_id2,
            "input": {"type": "message", "reason": "provide feedback"},
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        self.assertTrue(service2.finalize_task_from_agent(task_id2, callback))
        grant2 = service2.get_task(task_id2).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant2["state"]["status"], "revoked")
        self.assertEqual(grant2["audit"]["revocation_code"], "input_superseded")

    def test_repeated_revocation_is_idempotent(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, _, _ = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")

        self.assertTrue(service.revoke_permission_grant(task_id, "task_terminal"))
        revoked = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(revoked["state"]["status"], "revoked")
        self.assertEqual(revoked["audit"]["revocation_code"], "task_terminal")
        self.assertTrue(service.revoke_permission_grant(task_id, "task_terminal"))
        self.assertEqual(
            service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY],
            revoked,
        )
        self.assertFalse(service.revoke_permission_grant(task_id, "task_cancelled"))

        fresh = harness.create_task()
        self.assertFalse(service.revoke_permission_grant(fresh.id, "task_terminal"))

    def test_handoff_and_new_task_do_not_inherit_grant(self) -> None:
        harness = PermissionLifecycleHarness("codex")
        self.addCleanup(harness.close)
        service = harness.service
        task_id, _, _ = harness.prepare_wait()
        request = self._waiting_input(service, task_id)
        service.respond_to_input(task_id, request["input_id"], response_type="approve")
        workspace = service.get_task(task_id).workspace or {}
        callback = {
            "version": 1,
            "task_id": task_id,
            "final_state": "completed",
            "summary": "completed before handoff",
            "report_file": workspace["report_file"],
            "step_results": [{"id": 1, "status": "done"}],
        }
        self.assertTrue(service.finalize_task_from_agent(task_id, callback))
        target = service.handoff_task(task_id, "claude", "continue from here")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, target.extensions)

        fresh = harness.create_task()
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, fresh.extensions)


if __name__ == "__main__":
    unittest.main()
