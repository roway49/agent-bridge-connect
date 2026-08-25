"""Focused PERM-103-006 summary truncation and fallback-detail coverage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.approval import (
    APPROVAL_REASON_DETAIL_LIMIT,
    APPROVAL_REASON_SUMMARY_LIMIT,
    APPROVAL_SCOPE,
    normalize_reason_summary_details,
)
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.notifications import build_input_required_notification
from agent_bridge_connect.notifiers.dialog import DialogNotifier
from agent_bridge_connect.reports import generate_report
from agent_bridge_connect.run_lease import RunLeaseState, create_lease, save_lease
from agent_bridge_connect.service import TaskService, task_to_status


SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


class PermissionWaitHarness:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def prepare_running(self, executor: str = "codex") -> tuple[str, str]:
        task = self.service.create_task(
            "summary detail task",
            executor,
            [{"id": 1, "description": "one permission step"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        self.service.start_task_run(task.id, executor)
        run_id = f"{executor}-summary-detail-run"
        self.service.record_executor_run_started(task.id, run_id)
        lease = create_lease(task.id, executor, 0, str(self.project))
        lease.run_id = run_id
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.board)
        return task.id, run_id

    def wait_for_fallback(
        self,
        reason: str,
        *,
        reason_detail: str | None = None,
    ) -> str:
        task_id, run_id = self.prepare_running()
        input_details = {
            "type": "permission",
            "requested_permission": "full",
            "reason": reason,
        }
        if reason_detail is not None:
            input_details["reason_detail"] = reason_detail
        callback = {
            "version": 1,
            "task_id": task_id,
            "final_state": "input_required",
            "summary": "raw outer callback summary must not become the decision summary",
            "executor_run_id": run_id,
            "input": input_details,
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        receipt = {
            "version": 1,
            "executor": "codex",
            "session_id": SESSION_ID,
            "resumed": False,
            "persistence": "persistent",
            "source": "jsonl_thread_started",
        }
        self.service.finalize_task_from_executor_exit(
            task_id,
            executor_run_id=run_id,
            callback=callback,
            execution_session=receipt,
        )
        return task_id

    def single_action_wait(self) -> str:
        task_id, run_id = self.prepare_running("claude")
        task = self.service.get_task(task_id)
        official_session_id = task.extensions[SESSION_EXTENSION_KEY]["session_id"]
        self.service._apply_executor_session_result(
            task,
            run_id,
            {
                "version": 1,
                "executor": "claude",
                "session_id": official_session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            },
            "input_required",
        )
        self.service.store.write_task(task.id, task.to_dict())
        self.service.block_task_for_approval(
            task_id,
            executor_run_id=run_id,
            session_id=official_session_id,
            request_id="approval-summary-detail",
            request_fingerprint="fp-" + "s" * 40,
            executor="claude",
            operation="Bash",
            reason="short action summary",
            reason_detail="bounded single-action detail",
        )
        return task_id


class SummaryCompactionTests(unittest.TestCase):
    def test_exact_boundary_is_not_truncated_and_over_boundary_uses_ellipsis(self) -> None:
        exact, exact_truncated = normalize_reason_summary_details(
            "x" * APPROVAL_REASON_SUMMARY_LIMIT,
            executor="claude",
            operation="Bash",
        )
        over, over_truncated = normalize_reason_summary_details(
            "x" * (APPROVAL_REASON_SUMMARY_LIMIT + 1),
            executor="claude",
            operation="Bash",
        )
        self.assertEqual(exact, "x" * APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertFalse(exact_truncated)
        self.assertEqual(len(over), APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertTrue(over.endswith("…"))
        self.assertTrue(over_truncated)
        self.assertNotIn("...", over)

    def test_unicode_and_whitespace_are_normalized_before_bounding(self) -> None:
        short, truncated = normalize_reason_summary_details(
            "  保留\t中文   摘要  ",
            executor="claude",
            operation="Bash",
        )
        self.assertEqual(short, "保留 中文 摘要")
        self.assertFalse(truncated)
        unicode_exact, unicode_truncated = normalize_reason_summary_details(
            "界" * APPROVAL_REASON_SUMMARY_LIMIT,
            executor="claude",
            operation="Bash",
        )
        self.assertEqual(unicode_exact, "界" * APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertFalse(unicode_truncated)


class FullFallbackSummaryDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = PermissionWaitHarness()
        self.addCleanup(self.harness.close)

    def test_fallback_persists_compacted_summary_and_sanitized_bounded_detail(self) -> None:
        reason = (
            "  Need temporary full access\n"
            "to continue the blocked step. "
            "Please use the approved path for this operation. "
            "password=hunter2 token: abc123\tadditional context follows."
        )
        task_id = self.harness.wait_for_fallback(reason)
        task = self.harness.service.get_task(task_id)
        request = task.extensions["agentbc.input"]
        notification = build_input_required_notification(self.harness.service, task_id)

        self.assertEqual(task.status, "input_required")
        self.assertEqual(request["summary"], request["reason_summary"])
        self.assertTrue(request["summary_truncated"])
        self.assertTrue(request["summary"].endswith("…"))
        self.assertLessEqual(len(request["reason_detail"]), APPROVAL_REASON_DETAIL_LIMIT)
        self.assertNotIn("hunter2", json.dumps(request))
        self.assertNotIn("abc123", json.dumps(request))
        self.assertNotIn("\n", request["reason_detail"])
        self.assertNotIn("\t", request["reason_detail"])

        self.assertEqual(notification["reason_summary"], request["reason_summary"])
        self.assertEqual(notification["summary"], request["reason_summary"])
        self.assertEqual(notification["input_reason"], request["reason_summary"])
        self.assertTrue(notification["summary_truncated"])
        self.assertEqual(notification["reason_detail"], request["reason_detail"])
        self.assertNotIn("raw outer callback summary", notification["message"])

        status = task_to_status(task)
        report = generate_report(task_id, self.harness.board)
        self.assertNotIn("reason_detail", status["extensions"]["agentbc.input"])
        self.assertNotIn("reason_detail", report["input"])
        self.assertNotIn(request["reason_detail"], json.dumps(status))
        self.assertNotIn(request["reason_detail"], json.dumps(report))

    def test_overlong_current_callback_reason_keeps_safe_detail_not_legacy_marker(self) -> None:
        reason = "The current continuation needs temporary permission. " + ("context " * 50)
        task_id = self.harness.wait_for_fallback(reason)
        request = self.harness.service.get_task(task_id).extensions["agentbc.input"]

        self.assertEqual(len(request["reason"]), APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertTrue(request["summary_truncated"])
        self.assertTrue(request["reason_detail"].startswith("The current continuation"))
        self.assertGreater(len(request["reason_detail"]), 240)
        self.assertNotEqual(request["reason_detail"], request["reason"])

    def test_explicit_structured_detail_is_sanitized_and_bounded(self) -> None:
        reason = "Please continue the same task with temporary full access. " + ("context " * 30)
        detail = (
            "The structured fallback detail explains the continuation.\n"
            "password=hunter2 token: abc123 "
            + ("additional safe detail " * 150)
        )
        task_id = self.harness.wait_for_fallback(reason, reason_detail=detail)
        request = self.harness.service.get_task(task_id).extensions["agentbc.input"]

        self.assertLessEqual(len(request["reason_detail"]), APPROVAL_REASON_DETAIL_LIMIT)
        self.assertNotIn("hunter2", request["reason_detail"])
        self.assertNotIn("abc123", request["reason_detail"])
        self.assertNotIn("\n", request["reason_detail"])
        self.assertIn("structured fallback detail", request["reason_detail"])

    def test_forbidden_fallback_detail_fails_closed_without_leaking_input(self) -> None:
        cases = (
            "argv: codex --token hunter2 --cwd /private/tmp/work",
            "Reason: inspect /Users/alice/.claude/state.db",
            "token hunter2",
        )
        for reason in cases:
            with self.subTest(reason=reason):
                harness = PermissionWaitHarness()
                self.addCleanup(harness.close)
                task_id = harness.wait_for_fallback(reason)
                request = harness.service.get_task(task_id).extensions["agentbc.input"]
                notification = build_input_required_notification(harness.service, task_id)
                self.assertNotIn("reason_detail", request)
                self.assertEqual(notification["reason_detail"], "")
                self.assertNotIn("hunter2", json.dumps(notification))
                self.assertNotIn("/Users/alice", json.dumps(notification))
                self.assertNotIn("/private/tmp/work", json.dumps(notification))

    def test_truncated_fallback_summary_has_view_details(self) -> None:
        task_id = self.harness.wait_for_fallback("detail " * 40)
        notification = build_input_required_notification(self.harness.service, task_id)
        self.assertTrue(notification["summary_truncated"])
        self.assertTrue(notification["reason_detail"])

        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            DialogNotifier().send(notification)
        script = run.call_args.kwargs["input"]
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', script)
        self.assertIn('default button "Deny"', script)


class SingleActionCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = PermissionWaitHarness()
        self.addCleanup(self.harness.close)

    def test_single_action_receipt_detail_and_summary_metadata_remain_compatible(self) -> None:
        task_id = self.harness.single_action_wait()
        task = self.harness.service.get_task(task_id)
        request = task.extensions["agentbc.input"]
        notification = build_input_required_notification(self.harness.service, task_id)

        self.assertEqual(request["scope"], APPROVAL_SCOPE)
        self.assertNotIn("reason_detail", request)
        self.assertFalse(notification["summary_truncated"])
        self.assertEqual(notification["reason_detail"], "bounded single-action detail")
        self.assertEqual(notification["approval_request_id"], "approval-summary-detail")

    def test_single_action_timeout_and_close_still_deny_with_auditable_source(self) -> None:
        notification = build_input_required_notification(
            self.harness.service,
            self.harness.single_action_wait(),
        )
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr="execution error: User canceled. (-128)",
            )
            closed = notifier.send(notification)
        self.assertEqual(closed.details, {"action": "deny", "decision_source": "dialog_closed"})

        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:true",
                stderr="",
            )
            timed_out = notifier.send(notification)
        self.assertEqual(timed_out.details, {"action": "deny", "decision_source": "timeout"})


if __name__ == "__main__":
    unittest.main()
