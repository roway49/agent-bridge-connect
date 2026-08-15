"""Targeted tests for the ``agentbc.approval`` v1 receipt and Core approval flow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.approval import (
    APPROVAL_EXTENSION_KEY,
    APPROVAL_SCOPE,
    build_approval_receipt,
    compute_request_fingerprint,
    core_bounded_summary,
    record_approval_decision,
    validate_approval_receipt,
)
from agent_bridge_connect.permission_grants import PERMISSION_GRANT_EXTENSION_KEY
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService

TASK_ID = "ABCD-001"
RUN_ID = "claude-ABCD-001-run1"
SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


def _receipt() -> dict:
    return build_approval_receipt(
        task_id=TASK_ID,
        executor_run_id=RUN_ID,
        executor="claude",
        session_id=SESSION_ID,
        request_id="approval-request-1",
        request_fingerprint="fp-" + "a" * 40,
        operation="Bash",
        summary="claude needs one-time permission for: Bash",
    )


class ApprovalReceiptContractTests(unittest.TestCase):
    def test_build_links_all_binding_fields(self) -> None:
        receipt = _receipt()
        self.assertEqual(receipt["version"], 1)
        self.assertEqual(receipt["task_id"], TASK_ID)
        self.assertEqual(receipt["executor_run_id"], RUN_ID)
        self.assertEqual(receipt["executor"], "claude")
        self.assertEqual(receipt["session_id"], SESSION_ID)
        self.assertEqual(receipt["request_id"], "approval-request-1")
        self.assertEqual(receipt["scope"], APPROVAL_SCOPE)
        self.assertEqual(receipt["state"], {"status": "pending"})
        self.assertEqual(
            receipt["decision"],
            {"type": "", "source": "", "decided_at": ""},
        )

    def test_validate_fail_closed_on_tampered_binding(self) -> None:
        receipt = _receipt()
        receipt["task_id"] = "OTHER-999"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt, task_id=TASK_ID)
        self.assertEqual(exc.exception.code, "approval_task_mismatch")

    def test_validate_rejects_sensitive_fields(self) -> None:
        receipt = _receipt()
        receipt["command"] = "rm -rf"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_record_decision_replies_to_same_request(self) -> None:
        receipt = _receipt()
        decided = record_approval_decision(
            receipt,
            "approve",
            source="user",
            executor="claude",
            task_id=TASK_ID,
            session_id=SESSION_ID,
            request_id="approval-request-1",
        )
        self.assertEqual(decided["state"]["status"], "answered")
        self.assertEqual(decided["decision"]["type"], "approve")
        self.assertEqual(decided["decision"]["source"], "user")
        self.assertTrue(decided["decision"]["decided_at"])

    def test_record_decision_rejects_replay(self) -> None:
        receipt = _receipt()
        receipt = record_approval_decision(receipt, "deny", source="timeout")
        with self.assertRaises(ABCError) as exc:
            record_approval_decision(receipt, "approve", source="user")
        self.assertEqual(exc.exception.code, "approval_replay")

    def test_fingerprint_is_stable_and_content_derived(self) -> None:
        first = compute_request_fingerprint(
            executor="claude",
            session_id=SESSION_ID,
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        second = compute_request_fingerprint(
            executor="claude",
            session_id=SESSION_ID,
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        different = compute_request_fingerprint(
            executor="claude",
            session_id=SESSION_ID,
            tool_name="Bash",
            tool_input={"command": "pwd"},
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertTrue(first.startswith("fp-"))

    def test_core_bounded_summary_is_short(self) -> None:
        summary = core_bounded_summary(executor="claude", operation="Bash")
        self.assertLessEqual(len(summary), 120)
        self.assertIn("Bash", summary)


class ApprovalServiceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )

    def _create_task(self) -> str:
        task = self.service.create_task(
            "approval flow",
            "claude",
            [{"id": 1, "description": "one step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        return task.id

    def _started_task(self) -> tuple[str, str]:
        """Return (task_id, official_session_id) for a running task."""
        task_id = self._create_task()
        self.service.start_task_run(task_id, "claude")
        self.service.record_executor_run_started(task_id, RUN_ID)
        task = self.service.get_task(task_id)
        session = dict((task.extensions or {})["agentbc.session"])
        session["session_state"] = "active"
        session["run_ids"] = [RUN_ID]
        task.extensions = dict(task.extensions or {})
        task.extensions["agentbc.session"] = session
        self.service.store.write_task(task_id, task.to_dict())
        return task_id, str(session["session_id"])

    def test_block_task_for_approval_persists_receipt_and_waits(self) -> None:
        task_id, session_id = self._started_task()
        result = self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-1",
            request_fingerprint="fp-" + "b" * 40,
            executor="claude",
            operation="Bash",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "input_required")
        self.assertEqual(result["scope"], APPROVAL_SCOPE)
        task = self.service.get_task(task_id)
        self.assertEqual(task.status, "input_required")
        self.assertEqual(task.steps[0]["status"], "blocked")
        receipt = task.extensions[APPROVAL_EXTENSION_KEY]
        self.assertEqual(receipt["request_id"], "approval-request-1")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
        input_request = task.extensions["agentbc.input"]
        self.assertEqual(input_request["type"], "permission")
        self.assertEqual(input_request["scope"], APPROVAL_SCOPE)
        self.assertEqual(input_request["request_id"], "approval-request-1")

    def test_approve_does_not_issue_grant_and_keeps_effective_mode(self) -> None:
        task_id, session_id = self._started_task()
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-1",
            request_fingerprint="fp-" + "c" * 40,
            executor="claude",
            operation="Bash",
        )
        before = dict(self.service.get_task(task_id).extensions[PERMISSION_EXTENSION_KEY])
        request = self.service.get_task(task_id).extensions["agentbc.input"]

        result = self.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="approve",
        )
        self.assertTrue(result["dispatch_required"])
        self.assertEqual(result["approval_decision"], "approve")
        self.assertEqual(result["approval_source"], "user")
        after = self.service.get_task(task_id)
        self.assertEqual(after.status, "running")
        self.assertEqual(after.steps[0]["status"], "pending")
        self.assertEqual(after.extensions[PERMISSION_EXTENSION_KEY], before)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, after.extensions)
        self.assertEqual(
            after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"],
            "user",
        )

    def test_deny_resumes_same_session_with_source(self) -> None:
        task_id, session_id = self._started_task()
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-2",
            request_fingerprint="fp-" + "d" * 40,
            executor="claude",
            operation="Bash",
        )
        request = self.service.get_task(task_id).extensions["agentbc.input"]

        result = self.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="deny",
        )
        self.assertTrue(result["dispatch_required"])
        self.assertEqual(result["approval_decision"], "deny")
        self.assertEqual(result["approval_source"], "user")
        task = self.service.get_task(task_id)
        self.assertEqual(task.status, "running")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions)
        self.assertEqual(
            task.extensions[APPROVAL_EXTENSION_KEY]["decision"]["type"],
            "deny",
        )

    def test_approval_timeout_records_decision_and_recovery(self) -> None:
        task_id, session_id = self._started_task()
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-3",
            request_fingerprint="fp-" + "e" * 40,
            executor="claude",
            operation="Bash",
        )
        task = self.service.get_task(task_id)
        request = task.extensions["agentbc.input"]
        # Force the deadline into the past.
        import datetime as dt

        expired_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        request["deadline_at"] = (
            dt.datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
            - dt.timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        task.extensions["agentbc.input"] = request
        self.service.store.write_task(task_id, task.to_dict())

        expired = self.service.expire_waiting_inputs(now=expired_at)
        self.assertTrue(any(item["task_id"] == task_id for item in expired))
        after = self.service.get_task(task_id)
        self.assertEqual(after.status, "needs_recovery")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, after.extensions)
        self.assertEqual(
            after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"],
            "timeout",
        )
        self.assertEqual(
            after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["type"],
            "deny",
        )

    def test_dialog_closed_records_dialog_closed_source(self) -> None:
        task_id, session_id = self._started_task()
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-close",
            request_fingerprint="fp-" + "9" * 40,
            executor="claude",
            operation="Bash",
        )
        request = self.service.get_task(task_id).extensions["agentbc.input"]
        result = self.service.respond_to_input(
            task_id,
            request["input_id"],
            response_type="deny",
            message="agentbc_permission_dialog_closed",
        )
        self.assertEqual(result["approval_source"], "dialog_closed")
        task = self.service.get_task(task_id)
        self.assertEqual(
            task.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"],
            "dialog_closed",
        )

    def test_stale_input_is_rejected(self) -> None:
        task_id, session_id = self._started_task()
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-stale",
            request_fingerprint="fp-" + "7" * 40,
            executor="claude",
            operation="Bash",
        )
        with self.assertRaises(ABCError) as exc:
            self.service.respond_to_input(
                task_id,
                "input-wrong",
                response_type="approve",
            )
        self.assertEqual(exc.exception.code, "stale_input")

    def test_crash_after_approval_wait_moves_to_recovery(self) -> None:
        task_id, session_id = self._started_task()
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-request-crash",
            request_fingerprint="fp-" + "8" * 40,
            executor="claude",
            operation="Bash",
        )
        # Simulate an executor crash: the run lease becomes stale and the task
        # moves to needs_recovery without losing the approval decision record.
        from agent_bridge_connect.run_lease import (
            RunLeaseState,
            load_lease,
            save_lease,
        )

        lease = load_lease(task_id, self.service.board_root)
        self.assertIsNotNone(lease)
        lease.state = RunLeaseState.STALE
        save_lease(lease, self.service.board_root)
        changed = self.service.mark_task_needs_recovery(
            task_id,
            "executor_crash_after_approval",
            "Executor crashed while an approval was pending",
            {"request_id": "approval-request-crash"},
        )
        self.assertTrue(changed)
        after = self.service.get_task(task_id)
        self.assertEqual(after.status, "needs_recovery")
        self.assertEqual(
            after.extensions[APPROVAL_EXTENSION_KEY]["state"]["status"],
            "pending",
        )

    def test_approval_requires_official_session(self) -> None:
        task_id, _session_id = self._started_task()
        # A wrong session id must be rejected fail closed.
        with self.assertRaises(ABCError) as exc:
            self.service.block_task_for_approval(
                task_id,
                executor_run_id=RUN_ID,
                session_id=SESSION_ID,
                request_id="approval-request-4",
                request_fingerprint="fp-" + "f" * 40,
                executor="claude",
                operation="Bash",
            )
        self.assertEqual(exc.exception.code, "approval_session_mismatch")


if __name__ == "__main__":
    unittest.main()
