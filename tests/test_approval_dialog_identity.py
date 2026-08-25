"""PERM-103-006 approval-dialog task identity presentation.

Covers the identity slice of the macOS permission decision view:
- every permission decision payload carries explicit sanitized bounded identity
  fields (Task ID, bounded task title, Executor, blocked step, permission
  scope);
- ``build_input_required_notification`` produces a compact bounded
  ``AgentBC · <Executor> · <Task ID>`` title for permission inputs and keeps
  the task title / context readable in the body;
- single_action approval receipts and the legacy ``full`` fallback both render
  deterministically;
- bounded long task titles, all three Executor labels, and missing optional
  fields with a safe fallback;
- parallel tasks produce distinguishable titles/bodies;
- Approve / Deny / timeout decision semantics are unchanged.

This file is intentionally separate from the Codex summary/detail test file so
parallel ownership does not collide.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.approval import (
    APPROVAL_EXTENSION_KEY,
    APPROVAL_SCOPE,
    build_approval_receipt,
)
from agent_bridge_connect.notifications import build_input_required_notification
from agent_bridge_connect.notifiers.dialog import DialogNotifier
from agent_bridge_connect.service import TaskService

TASK_ID = "FMDA-001"
RUN_ID = "claude-FMDA-001-run1"
SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


def _approval_receipt(*, executor: str = "claude", reason_detail: str = "") -> dict:
    return build_approval_receipt(
        task_id=TASK_ID,
        executor_run_id=RUN_ID,
        executor=executor,
        session_id=SESSION_ID,
        request_id="approval-request-identity",
        request_fingerprint="fp-" + "i" * 40,
        operation="Bash",
        summary=f"{executor} needs one-time permission for: Bash",
        reason_summary="Run the requested tests",
        reason_detail=reason_detail,
    )


def _waiting_input(
    *,
    input_id: str = "input-identity-1",
    blocked_step_id: int = 2,
    scope: str = APPROVAL_SCOPE,
    requested_permission: str = "",
    request_id: str = "approval-request-identity",
    summary: str = "Run the requested tests",
    executor_run_id: str = RUN_ID,
) -> dict:
    request: dict = {
        "input_id": input_id,
        "executor_run_id": executor_run_id,
        "blocked_step_id": blocked_step_id,
        "type": "permission",
        "summary": summary,
        "reason_summary": summary,
        "created_at": "2026-01-01T00:00:00Z",
        "deadline_at": "2099-01-01T00:00:00Z",
        "status": "waiting",
    }
    if scope:
        request["scope"] = scope
    if requested_permission:
        request["requested_permission"] = requested_permission
    if request_id:
        request["request_id"] = request_id
    return request


class IdentityService:
    """Small helper building a synthetic task with a waiting permission input."""

    def __init__(
        self,
        *,
        title: str = "approval identity task",
        executor: str = "claude",
        task_id: str = TASK_ID,
        reason_detail: str = "",
    ) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace")},
        )
        self.task = self.service.create_task(
            title,
            executor,
            [{"id": 1, "description": "first step"}, {"id": 2, "description": "blocked step"}],
            customer_dir=False,
        )
        self.task_id = self.task.id
        self.executor = executor

    def cleanup(self) -> None:
        self.temporary.cleanup()

    def _write_extensions(
        self,
        *,
        request: dict,
        receipt: dict | None,
        assignee: str | None = None,
        title: str | None = None,
    ) -> None:
        raw = self.service.store.read_task(self.task_id)
        raw["status"] = "input_required"
        if assignee is not None:
            raw["assignee"] = assignee
        if title is not None:
            raw["title"] = title
        raw["extensions"]["agentbc.input"] = request
        if receipt is not None:
            raw["extensions"][APPROVAL_EXTENSION_KEY] = receipt
        self.service.store.write_task(self.task_id, raw)

    def make_single_action(self, *, reason_detail: str = "") -> None:
        self._write_extensions(
            request=_waiting_input(scope=APPROVAL_SCOPE),
            receipt=_approval_receipt(executor=self.executor, reason_detail=reason_detail),
        )

    def make_full_fallback(self, *, reason_detail: str = "") -> None:
        self._write_extensions(
            request=_waiting_input(
                scope="",
                requested_permission="full",
                request_id="",
            ),
            receipt=_approval_receipt(executor=self.executor, reason_detail=reason_detail),
        )

    def make_bare(self) -> None:
        """A waiting permission input with no optional identity detail."""
        self._write_extensions(
            request=_waiting_input(scope="", requested_permission="", request_id=""),
            receipt=None,
        )


class SingleActionIdentityTests(unittest.TestCase):
    def test_single_action_payload_carries_sanitized_identity_fields(self) -> None:
        helper = IdentityService()
        self.addCleanup(helper.cleanup)
        helper.make_single_action()
        notification = build_input_required_notification(helper.service, helper.task_id)

        self.assertEqual(notification["task_id"], helper.task_id)
        self.assertEqual(notification["identity_task_id"], helper.task_id)
        self.assertEqual(notification["identity_task_title"], "approval identity task")
        self.assertEqual(notification["identity_executor"], "claude")
        self.assertEqual(notification["identity_blocked_step"], "2")
        self.assertEqual(notification["identity_scope"], APPROVAL_SCOPE)
        self.assertEqual(
            notification["dialog_title"],
            f"AgentBC · claude · {helper.task_id}",
        )
        self.assertEqual(notification["title"], notification["dialog_title"])
        self.assertEqual(notification["input_type"], "permission")

    def test_single_action_body_readable_after_identity_header(self) -> None:
        helper = IdentityService()
        self.addCleanup(helper.cleanup)
        helper.make_single_action(reason_detail="The bounded detail")
        notification = build_input_required_notification(helper.service, helper.task_id)

        message = notification["message"]
        self.assertIn(f"Task: {helper.task_id} needs your input", message)
        self.assertIn("Run the requested tests", message)
        self.assertEqual(notification["identity_scope"], APPROVAL_SCOPE)

    def test_single_action_decision_view_renders_identity(self) -> None:
        helper = IdentityService()
        self.addCleanup(helper.cleanup)
        helper.make_single_action(reason_detail="The bounded detail")
        payload = build_input_required_notification(helper.service, helper.task_id)

        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(payload)
        argv = run.call_args.args[0]
        self.assertEqual(argv[2], f"AgentBC · claude · {helper.task_id}")
        body = argv[3]
        self.assertIn(f"Task: {helper.task_id}", body)
        self.assertIn("Title: approval identity task", body)
        self.assertIn("Blocked step: 2", body)
        self.assertIn(f"Permission scope: {APPROVAL_SCOPE}", body)
        self.assertIn("Executor: claude", body)
        self.assertIn("Run the requested tests", body)


class FullFallbackIdentityTests(unittest.TestCase):
    def test_full_fallback_payload_carries_identity_fields(self) -> None:
        helper = IdentityService(executor="codex")
        self.addCleanup(helper.cleanup)
        helper.make_full_fallback()
        notification = build_input_required_notification(helper.service, helper.task_id)

        self.assertEqual(notification["identity_scope"], "full")
        self.assertEqual(notification["identity_executor"], "codex")
        self.assertEqual(
            notification["dialog_title"],
            f"AgentBC · codex · {helper.task_id}",
        )
        # Full fallback keeps the full-permission contract body text.
        message = notification["message"]
        self.assertIn("Requested access:", message)
        self.assertIn("Deny terminates the task as failed.", message)

    def test_full_fallback_decision_view_renders_scope_full(self) -> None:
        helper = IdentityService(executor="codex")
        self.addCleanup(helper.cleanup)
        helper.make_full_fallback()
        payload = build_input_required_notification(helper.service, helper.task_id)

        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(payload)
        argv = run.call_args.args[0]
        self.assertEqual(argv[2], f"AgentBC · codex · {helper.task_id}")
        body = argv[3]
        self.assertIn("Permission scope: full", body)
        self.assertIn("Executor: codex", body)
        self.assertIn(f"Task: {helper.task_id}", body)


class LongTitleBoundingTests(unittest.TestCase):
    def test_long_task_title_is_bounded_in_identity_title_and_body(self) -> None:
        long_title = "A very long permission title that should be bounded down " * 4
        helper = IdentityService(title=long_title)
        self.addCleanup(helper.cleanup)
        helper.make_single_action()
        notification = build_input_required_notification(helper.service, helper.task_id)

        identity_title = notification["identity_task_title"]
        self.assertLessEqual(len(identity_title), 96)
        # The bounded title keeps the original prefix, not a truncation artifact.
        self.assertTrue(identity_title.startswith("A very long permission title"))
        self.assertTrue(identity_title.endswith("..."))

        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(notification)
        body = run.call_args.args[0][3]
        self.assertIn(f"Title: {identity_title}", body)
        # The raw untruncated title must never appear in the decision view.
        self.assertNotIn(long_title, body)


class ExecutorLabelTests(unittest.TestCase):
    def _render_decision_body(self, executor: str) -> str:
        helper = IdentityService(executor=executor)
        self.addCleanup(helper.cleanup)
        helper.make_single_action()
        payload = build_input_required_notification(helper.service, helper.task_id)
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(payload)
        argv = run.call_args.args[0]
        self.assertEqual(argv[2], f"AgentBC · {executor} · {helper.task_id}")
        return argv[3]

    def test_all_three_executor_labels_appear_in_title_and_body(self) -> None:
        for executor in ("claude", "codex", "hermes"):
            with self.subTest(executor=executor):
                body = self._render_decision_body(executor)
                self.assertIn(f"Executor: {executor}", body)
                self.assertIn(f"Permission scope: {APPROVAL_SCOPE}", body)


class MissingOptionalFieldTests(unittest.TestCase):
    def test_missing_optional_identity_fields_fall_back_safely(self) -> None:
        # A waiting permission with no scope / requested_permission / request_id
        # and no approval receipt still renders a readable decision view.
        helper = IdentityService()
        self.addCleanup(helper.cleanup)
        helper.make_bare()
        notification = build_input_required_notification(helper.service, helper.task_id)

        self.assertEqual(notification["identity_scope"], "unknown")
        self.assertEqual(notification["identity_task_title"], "approval identity task")
        self.assertEqual(notification["dialog_title"], f"AgentBC · claude · {helper.task_id}")

        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(notification)
        argv = run.call_args.args[0]
        body = argv[3]
        self.assertIn(f"Task: {helper.task_id}", body)
        self.assertIn("Permission scope: unknown", body)
        self.assertIn("Executor: claude", body)
        self.assertIn("Run the requested tests", body)

    def test_empty_task_title_yields_no_title_line(self) -> None:
        helper = IdentityService(title="placeholder")
        self.addCleanup(helper.cleanup)
        raw = helper.service.store.read_task(helper.task_id)
        raw["title"] = "   "
        helper.service.store.write_task(helper.task_id, raw)
        helper.make_single_action()
        notification = build_input_required_notification(helper.service, helper.task_id)
        self.assertEqual(notification["identity_task_title"], "")
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(notification)
        body = run.call_args.args[0][3]
        self.assertNotIn("Title:", body)
        self.assertIn(f"Task: {helper.task_id}", body)


class ParallelTaskDistinguishabilityTests(unittest.TestCase):
    def test_parallel_tasks_produce_distinguishable_titles_and_bodies(self) -> None:
        first = IdentityService(title="first identity task", executor="claude")
        self.addCleanup(first.cleanup)
        first.make_single_action()
        second = IdentityService(title="second identity task", executor="codex")
        self.addCleanup(second.cleanup)
        second.make_full_fallback()

        notification_one = build_input_required_notification(first.service, first.task_id)
        notification_two = build_input_required_notification(second.service, second.task_id)

        self.assertNotEqual(notification_one["dialog_title"], notification_two["dialog_title"])
        self.assertIn("claude", notification_one["dialog_title"])
        self.assertIn("codex", notification_two["dialog_title"])
        self.assertNotEqual(
            notification_one["identity_task_title"],
            notification_two["identity_task_title"],
        )

        notifier = DialogNotifier()
        bodies: list[str] = []
        for payload in (notification_one, notification_two):
            with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
                run.return_value = mock.Mock(
                    returncode=0,
                    stdout="button returned:Deny, gave up:false",
                    stderr="",
                )
                notifier.send(payload)
            bodies.append(run.call_args.args[0][3])
        self.assertNotEqual(bodies[0], bodies[1])
        self.assertIn("first identity task", bodies[0])
        self.assertIn("second identity task", bodies[1])
        self.assertIn("Permission scope: single_action", bodies[0])
        self.assertIn("Permission scope: full", bodies[1])


class DecisionSemanticsUnchangedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = IdentityService()
        self.addCleanup(self.helper.cleanup)
        self.helper.make_single_action()
        self.payload = build_input_required_notification(
            self.helper.service,
            self.helper.task_id,
        )

    def test_approve_still_maps_to_user_approve(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Approve, gave up:false",
                stderr="",
            )
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "approve", "decision_source": "user"})

    def test_deny_still_maps_to_user_deny(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "deny", "decision_source": "user"})

    def test_timeout_still_auto_denies_with_timeout_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:true",
                stderr="",
            )
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})

    def test_dialog_close_still_auto_denies_with_dialog_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr="execution error: User canceled. (-128)",
            )
            result = notifier.send(self.payload)
        self.assertEqual(
            result.details,
            {"action": "deny", "decision_source": "dialog_closed"},
        )

    def test_view_details_back_keeps_identity_and_returns_to_decision(self) -> None:
        self.helper.make_single_action(reason_detail="The bounded detail")
        self.payload = build_input_required_notification(
            self.helper.service,
            self.helper.task_id,
        )
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""),
                mock.Mock(returncode=0, stdout="button returned:Back, gave up:false", stderr=""),
                mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr=""),
            ]
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "approve", "decision_source": "user"})
        # Decision view (twice) carries the identity body; detail view carries detail.
        decision_body = run.call_args_list[0].args[0][3]
        self.assertIn(f"Task: {self.helper.task_id}", decision_body)
        self.assertEqual(run.call_args_list[1].args[0][3], "The bounded detail")
        self.assertEqual(run.call_args_list[2].args[0][3], decision_body)


if __name__ == "__main__":
    unittest.main()
