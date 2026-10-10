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
from agent_bridge_connect.approval import APPROVAL_EXTENSION_KEY, APPROVAL_SCOPE, build_approval_receipt
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


def _waiting_input(*, input_id: str = "input-identity-1", blocked_step_id: int = 2, scope: str = APPROVAL_SCOPE, requested_permission: str = "", request_id: str = "approval-request-identity", summary: str = "Run the requested tests", executor_run_id: str = RUN_ID) -> dict:
    request: dict = {"input_id": input_id, "executor_run_id": executor_run_id, "blocked_step_id": blocked_step_id, "type": "permission", "summary": summary, "reason_summary": summary, "created_at": "2026-01-01T00:00:00Z", "deadline_at": "2099-01-01T00:00:00Z", "status": "waiting"}
    if scope:
        request["scope"] = scope
    if requested_permission:
        request["requested_permission"] = requested_permission
    if request_id:
        request["request_id"] = request_id
    return request


class IdentityService:
    """Small helper building a synthetic task with a waiting permission input."""

    def __init__(self, *, title: str = "approval identity task", executor: str = "claude", task_id: str = TASK_ID, reason_detail: str = "") -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(self.board, config={"workspace_root": str(self.root / "workspace")})
        self.task = self.service.create_task(title, executor, [{"id": 1, "description": "first step"}, {"id": 2, "description": "blocked step"}], customer_dir=False)
        self.task_id = self.task.id
        self.executor = executor

    def cleanup(self) -> None:
        self.temporary.cleanup()

    def _write_extensions(self, *, request: dict, receipt: dict | None, assignee: str | None = None, title: str | None = None) -> None:
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
        self._write_extensions(request=_waiting_input(scope=APPROVAL_SCOPE), receipt=_approval_receipt(executor=self.executor, reason_detail=reason_detail))

    def make_full_fallback(self, *, reason_detail: str = "") -> None:
        self._write_extensions(request=_waiting_input(scope="", requested_permission="full", request_id=""), receipt=_approval_receipt(executor=self.executor, reason_detail=reason_detail))

    def make_bare(self) -> None:
        """A waiting permission input with no optional identity detail."""
        self._write_extensions(request=_waiting_input(scope="", requested_permission="", request_id=""), receipt=None)


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
        self.assertEqual(notification["dialog_title"], f"AgentBC · claude · {helper.task_id}")
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
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
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
        self.assertEqual(notification["dialog_title"], f"AgentBC · codex · {helper.task_id}")
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
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
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
        self.assertTrue(identity_title.startswith("A very long permission title"))
        self.assertTrue(identity_title.endswith("..."))
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
            notifier.send(notification)
        body = run.call_args.args[0][3]
        self.assertIn(f"Title: {identity_title}", body)
        self.assertNotIn(long_title, body)


class ExecutorLabelTests(unittest.TestCase):
    def _render_decision_body(self, executor: str) -> str:
        helper = IdentityService(executor=executor)
        self.addCleanup(helper.cleanup)
        helper.make_single_action()
        payload = build_input_required_notification(helper.service, helper.task_id)
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
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
        helper = IdentityService()
        self.addCleanup(helper.cleanup)
        helper.make_bare()
        notification = build_input_required_notification(helper.service, helper.task_id)
        self.assertEqual(notification["identity_scope"], "unknown")
        self.assertEqual(notification["identity_task_title"], "approval identity task")
        self.assertEqual(notification["dialog_title"], f"AgentBC · claude · {helper.task_id}")
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
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
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
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
        self.assertNotEqual(notification_one["identity_task_title"], notification_two["identity_task_title"])
        notifier = DialogNotifier()
        bodies: list[str] = []
        for payload in (notification_one, notification_two):
            with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
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
        self.payload = build_input_required_notification(self.helper.service, self.helper.task_id)

    def test_approve_still_maps_to_user_approve(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr="")
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "approve", "decision_source": "user"})

    def test_deny_still_maps_to_user_deny(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "deny", "decision_source": "user"})

    def test_timeout_still_auto_denies_with_timeout_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:true", stderr="")
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})

    def test_dialog_close_still_auto_denies_with_dialog_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "deny", "decision_source": "dialog_closed"})

    def test_view_details_back_keeps_identity_and_returns_to_decision(self) -> None:
        self.helper.make_single_action(reason_detail="The bounded detail")
        self.payload = build_input_required_notification(self.helper.service, self.helper.task_id)
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""), mock.Mock(returncode=0, stdout="button returned:Back, gave up:false", stderr=""), mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr="")]
            result = notifier.send(self.payload)
        self.assertEqual(result.details, {"action": "approve", "decision_source": "user"})
        decision_body = run.call_args_list[0].args[0][3]
        self.assertIn(f"Task: {self.helper.task_id}", decision_body)
        self.assertEqual(run.call_args_list[1].args[0][3], "The bounded detail")
        self.assertEqual(run.call_args_list[2].args[0][3], decision_body)

    def test_native_choices_with_details_stay_within_macos_three_button_limit(self) -> None:
        notifier = DialogNotifier()
        payload = {
            "event_type": "task.input_required",
            "input_type": "permission",
            "dialog_title": "AgentBC · claude · TEST-001",
            "reason_summary": "Claude requested Bash permission",
            "reason_detail": "Exact structured request detail",
            "deadline_at": "2099-01-01T00:00:00Z",
            "identity_task_id": "TEST-001",
            "identity_executor": "claude",
            "identity_scope": "single_action",
            "native_options": [{"handle": "opt-deny", "kind": "deny", "label": "Deny"}, {"handle": "opt-once", "kind": "once", "label": "Allow once"}, {"handle": "opt-session", "kind": "session", "label": "Allow for this session"}, {"handle": "opt-fork", "kind": "fork", "label": "Fork choice"}],
        }
        with mock.patch.object(notifier, "_run_script") as run:
            run.side_effect = ["Approve", "This Session"]
            result = notifier.send(payload)
        self.assertEqual(result.details["action"], "permission_option")
        self.assertEqual(result.details["option_handle"], "opt-session")
        decision_script = run.call_args_list[0].args[2]
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', decision_script)
        self.assertIn('buttons {"Back", "Once", "This Session"}', run.call_args_list[1].args[2])
        for call in run.call_args_list:
            script = call.args[2]
            button_clause = script.split("buttons {", 1)[1].split("}", 1)[0]
            self.assertLessEqual(len(button_clause.split(", ")), 3)

    def test_native_deny_returns_the_exact_deny_handle(self) -> None:
        notifier = DialogNotifier()
        payload = {
            "event_type": "task.input_required",
            "input_type": "permission",
            "reason_summary": "Native request",
            "reason_detail": "Details",
            "deadline_at": "2099-01-01T00:00:00Z",
            "native_options": [{"handle": "opt-deny", "kind": "deny", "label": "Deny"}, {"handle": "opt-once", "kind": "once", "label": "Once"}],
        }
        with mock.patch.object(notifier, "_run_script", return_value="Deny"):
            result = notifier.send(payload)
        self.assertEqual(result.details["action"], "permission_option")
        self.assertEqual(result.details["option_handle"], "opt-deny")

    def test_view_details_approve_and_back_are_navigation_only(self) -> None:
        notifier = DialogNotifier()
        payload = {
            "event_type": "task.input_required",
            "input_type": "permission",
            "reason_summary": "Native request",
            "reason_detail": "Bounded native details",
            "deadline_at": "2099-01-01T00:00:00Z",
            "native_options": [{"handle": "opt-deny", "kind": "deny", "label": "Deny"}, {"handle": "opt-once", "kind": "once", "label": "Once"}, {"handle": "opt-session", "kind": "session", "label": "Session"}],
        }
        with mock.patch.object(notifier, "_run_script") as run:
            run.side_effect = ["View Details", "Back", "Approve", "Back", "Approve", "Once"]
            result = notifier.send(payload)
        self.assertEqual(len(run.call_args_list), 6)
        self.assertEqual(result.details["action"], "permission_option")
        self.assertEqual(result.details["option_handle"], "opt-once")


class HeadlessNotificationTests(unittest.TestCase):
    def test_terminal_notice_uses_log_sink_without_dialog_or_payload_secrets(self):
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"AGENTBC_HEADLESS_NOTIFICATIONS": "stdout"}), mock.patch("subprocess.run") as run, redirect_stdout(output):
            result = DialogNotifier().send({"event_type": "task.finalized", "task_id": "TEST-001", "report_path": "/data/report.md", "message": "private payload"})
        self.assertFalse(result.ok)
        self.assertIn("host terminal notification pending", result.message)
        self.assertIn("TEST-001", output.getvalue())
        self.assertNotIn("private payload", output.getvalue())
        run.assert_not_called()

    def test_headless_approval_stays_pending_without_decision(self):
        with mock.patch.dict("os.environ", {"AGENTBC_HEADLESS_NOTIFICATIONS": "stdout"}), mock.patch("subprocess.run") as run:
            result = DialogNotifier().send({"event_type": "task.input_required", "input_type": "permission"})
        self.assertFalse(result.ok)
        self.assertIn("pending", result.message)
        run.assert_not_called()


class DockerApprovalBridgeTests(unittest.TestCase):
    def test_command_error_reports_phase_without_sensitive_body(self):
        from agent_bridge_connect.docker_approval_bridge import execute
        result = mock.Mock(returncode=1, stdout='respond_error: stale_input: secret-body', stderr='')
        with mock.patch('subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'phase=respond; code=stale_input') as error:
                execute(['docker', 'exec', 'container', 'agentbc', 'task', 'respond'])
        self.assertNotIn('secret-body', str(error.exception))

    def snapshot(self):
        return {'status': 'input_required', 'request': {'input_id': 'input-1',
                'request_fingerprint': 'fp-1', 'deadline_at': '2099-01-01T00:00:00Z'},
                'payload': {'approval_version': 3, 'dialog_title': 'test'}}

    def test_requires_explicit_user_approval(self):
        from agent_bridge_connect.docker_approval_bridge import decision_args
        self.assertEqual(decision_args({'approval_version': 3}, {'action': 'approve', 'decision_source': 'user'}), ['--approve-full'])
        for source in ('timeout', 'dialog_closed', ''):
            with self.assertRaises(ValueError):
                decision_args({'approval_version': 3}, {'action': 'approve', 'decision_source': source})
        self.assertEqual(decision_args({'approval_version': 3}, {'action': 'deny'}), ['--deny'])

    def test_expired_request_never_opens_dialog(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge
        bridge = object.__new__(Bridge)
        snap = self.snapshot()
        snap['request']['deadline_at'] = '2000-01-01T00:00:00Z'
        with mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval') as notifier:
            self.assertEqual(bridge.deliver('TEST-001', snap), 'expired or no longer pending')
            notifier.assert_not_called()

    def test_replaced_request_discards_user_decision(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge
        from agent_bridge_connect.adapters import DeliveryResult
        bridge = object.__new__(Bridge)
        before = self.snapshot()
        after = self.snapshot()
        after['request']['request_fingerprint'] = 'fp-new'
        with mock.patch.object(bridge, 'snapshot', return_value=after), mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval') as notifier, mock.patch('agent_bridge_connect.docker_approval_bridge.execute') as execute:
            notifier.return_value = DeliveryResult(True, details={'action': 'approve', 'decision_source': 'user'})
            self.assertEqual(bridge.deliver('TEST-001', before), 'stale decision discarded')
            execute.assert_not_called()

    def test_bound_decision_uses_official_container_cli(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge
        from agent_bridge_connect.adapters import DeliveryResult
        bridge = object.__new__(Bridge)
        bridge.container = 'fixed-container-id'
        before = self.snapshot()
        for action, flag in [('approve', '--approve-full'), ('approve_full', '--approve-full'), ('deny', '--deny')]:
            with mock.patch.object(bridge, 'snapshot', return_value=before), mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval') as notifier, mock.patch('agent_bridge_connect.docker_approval_bridge.execute') as execute:
                notifier.return_value = DeliveryResult(True, details={'action': action, 'decision_source': 'user'})
                bridge.deliver('TEST-001', before)
                args = execute.call_args.args[0]
                self.assertEqual(args[:6], ['docker', 'exec', 'fixed-container-id', 'agentbc', 'task', 'respond'])
                self.assertEqual(args[-3:], ['--input', 'input-1', flag])

    def test_v2_unknown_handle_rejected(self):
        from agent_bridge_connect.docker_approval_bridge import decision_args
        with self.assertRaises(ValueError):
            decision_args({'approval_version': 2, 'native_options': [{'handle': 'bound'}]}, {'action': 'permission_option', 'option_handle': 'foreign'})

    def test_failed_dialog_never_submits(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge
        from agent_bridge_connect.adapters import DeliveryResult
        bridge = object.__new__(Bridge)
        with mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval') as notifier, mock.patch('agent_bridge_connect.docker_approval_bridge.execute') as execute:
            notifier.return_value = DeliveryResult(False, 'no desktop')
            with self.assertRaisesRegex(RuntimeError, 'not delivered'):
                bridge.deliver('TEST-001', self.snapshot())
            execute.assert_not_called()

    def test_timeout_cannot_approve(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge
        from agent_bridge_connect.adapters import DeliveryResult
        bridge = object.__new__(Bridge)
        before = self.snapshot()
        after = self.snapshot()
        after['request']['deadline_at'] = '2000-01-01T00:00:00Z'
        with mock.patch.object(bridge, 'snapshot', return_value=after), mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval') as notifier, mock.patch('agent_bridge_connect.docker_approval_bridge.execute') as execute:
            notifier.return_value = DeliveryResult(True, details={'action': 'deny', 'decision_source': 'timeout'})
            self.assertEqual(bridge.deliver('TEST-001', before), 'stale decision discarded')
            execute.assert_not_called()

    def test_denial_provenance_survives_bridge_and_cli(self):
        from agent_bridge_connect.docker_approval_bridge import decision_args
        from agent_bridge_connect.cli import build_parser, command_task_respond
        from agent_bridge_connect.approval_timing import permission_denial_source
        for source in ('user', 'timeout', 'dialog_closed'):
            flags = decision_args({'approval_version': 3}, {'action': 'deny', 'decision_source': source})
            args = build_parser().parse_args(['task', 'respond', 'TEST-001', '--input', 'input-1', *flags])
            with mock.patch('agent_bridge_connect.runner.RunnerClient') as client, mock.patch('builtins.print'):
                client.return_value.respond_task.return_value = {'status': 'failed'}
                self.assertEqual(command_task_respond(args), 0)
                sent = client.return_value.respond_task.call_args.args
                self.assertEqual(sent[2], 'deny')
                self.assertEqual(permission_denial_source(sent[2], sent[3]), source)

    def test_denial_source_cannot_annotate_approval(self):
        from agent_bridge_connect.cli import build_parser, command_task_respond
        args = build_parser().parse_args(['task', 'respond', 'TEST-001', '--input', 'input-1', '--approve-full', '--denial-source', 'timeout'])
        with mock.patch('agent_bridge_connect.runner.RunnerClient') as client, mock.patch('builtins.print'):
            self.assertEqual(command_task_respond(args), 1)
            client.assert_not_called()


class DockerApprovalDiscoveryTests(unittest.TestCase):
    def test_discovery_uses_exact_container_and_rejects_invalid_ids(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge
        bridge = object.__new__(Bridge)
        bridge.container = 'pinned-id'
        with mock.patch('agent_bridge_connect.docker_approval_bridge.execute', return_value='["ABCD-001"]') as execute:
            self.assertEqual(bridge.discover(), ['ABCD-001'])
            self.assertEqual(execute.call_args.args[0][:3], ['docker', 'exec', 'pinned-id'])
        with mock.patch('agent_bridge_connect.docker_approval_bridge.execute', return_value='["../../other"]'):
            with self.assertRaises(RuntimeError):
                bridge.discover()

    def test_discovery_reads_store_without_reconciling_business_tasks(self):
        from agent_bridge_connect.docker_approval_bridge import DISCOVER
        self.assertIn('TaskStore', DISCOVER)
        self.assertNotIn('TaskService', DISCOVER)
        self.assertIn("status='input_required'", DISCOVER)

    def test_watch_does_not_claim_coverage_from_explicit_task_owner(self):
        import fcntl
        import hashlib
        import json
        import os
        import tempfile
        from pathlib import Path
        from agent_bridge_connect import docker_approval_bridge as module
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / f'agentbc-docker-approval-{os.getuid()}'
            directory.mkdir(mode=0o700)
            lock_path = directory / hashlib.sha256(b'container').hexdigest()
            with lock_path.open('w+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for watch in (False, True):
                    lock.seek(0)
                    lock.truncate()
                    json.dump({'watch': watch}, lock)
                    lock.flush()
                    with mock.patch.object(module.tempfile, 'gettempdir', return_value=tmp), mock.patch.object(module.sys, 'argv', ['bridge', '--container', 'container', '--watch']), mock.patch.object(module.sys, 'platform', 'darwin'), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(module, 'Bridge') as bridge:
                        bridge.return_value.container = 'container'
                        bridge.return_value.discover.return_value = []
                        if watch:
                            module.main()
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'explicit-task bridge'):
                                module.main()

class DockerApprovalDeliveryRecoveryTests(unittest.TestCase):
    def test_failed_delivery_retries_bounded_without_duplicate_inflight(self):
        from agent_bridge_connect.docker_approval_bridge import DeliveryQueue, DeliveryFailure
        queue = DeliveryQueue()
        key = ('TEST-001', 'bound-request')
        self.assertTrue(queue.eligible(key, 0))
        queue.start(key)
        self.assertFalse(queue.eligible(key, 100))
        queue.finish(key, now=0, error=DeliveryFailure('osascript exited 1 (-1719)'))
        self.assertFalse(queue.eligible(key, 1))
        self.assertTrue(queue.eligible(key, 2))
        queue.start(key)
        queue.finish(key, now=2, error=DeliveryFailure('no display'))
        self.assertFalse(queue.eligible(key, 5))
        queue.start(key)
        queue.finish(key, now=6, error=DeliveryFailure('no display'))
        self.assertFalse(queue.eligible(key, 10000))
        self.assertEqual(queue.requests[key]['attempts'], 3)
        self.assertEqual(queue.requests[key]['state'], 'manual_action_required')
        self.assertIn('agentbc task status TEST-001', queue.requests[key]['next_step'])

    def test_submission_failure_never_reopens_dialog_and_new_request_is_independent(self):
        from agent_bridge_connect.docker_approval_bridge import DeliveryQueue
        queue = DeliveryQueue()
        key = ('TEST-001', 'old-request')
        queue.start(key)
        queue.finish(key, now=0, error=RuntimeError('response outcome unknown'))
        self.assertFalse(queue.eligible(key, 100))
        new = ('TEST-001', 'new-request')
        self.assertTrue(queue.eligible(new, 100))
        queue.start(new)
        queue.finish(new, now=100, result='accepted')
        self.assertFalse(queue.eligible(new, 200))
        queue.prune(set())
        self.assertFalse(queue.requests)
        self.assertEqual(len(queue.recent_failures), 1)

    def test_failed_delivery_retains_reason_and_never_submits_decision(self):
        from agent_bridge_connect.docker_approval_bridge import Bridge, DeliveryFailure
        from agent_bridge_connect.adapters import DeliveryResult
        bridge = object.__new__(Bridge)
        before = DockerApprovalBridgeTests().snapshot()
        with mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval') as notifier, mock.patch('agent_bridge_connect.docker_approval_bridge.execute') as execute:
            notifier.return_value = DeliveryResult(False, 'osascript exited 1 (-1719)')
            with self.assertRaisesRegex(DeliveryFailure, '-1719'):
                bridge.deliver('TEST-001', before)
            execute.assert_not_called()

    def test_process_timeout_is_delivery_failure_not_a_human_denial(self):
        import subprocess
        from agent_bridge_connect.adapters import DeliveryResult
        notifier = DialogNotifier()
        with mock.patch('agent_bridge_connect.notifiers.dialog.subprocess.run', side_effect=subprocess.TimeoutExpired('osascript', 8)) as run:
            result = notifier._run_script('test', 'body', notifier._task_elevation_decision_script(3), 3)
        self.assertIsInstance(result, DeliveryResult)
        self.assertFalse(result.ok)
        self.assertNotIn('action', result.details)
        self.assertIn('activate', run.call_args.kwargs['input'])

    def test_doctor_delivery_state_is_bound_fresh_and_private(self):
        from agent_bridge_connect.docker_approval_bridge import DeliveryQueue, DeliveryFailure, read_delivery_state
        import json
        queue = DeliveryQueue()
        key = ('TEST-001', 'request')
        queue.start(key)
        queue.finish(key, now=0, error=DeliveryFailure('osascript failed'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'approval-bridge-state.json'
            queue.save(path, 'container-a')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_delivery_state(root, 'container-a')['state'], 'degraded')
            self.assertEqual(read_delivery_state(root, 'container-b')['state'], 'unknown')
            value = json.loads(path.read_text())
            value['updated_at'] = '2000-01-01T00:00:00+00:00'
            path.write_text(json.dumps(value))
            self.assertEqual(read_delivery_state(root, 'container-a')['state'], 'stale')

    def test_restart_preserves_retry_budget_and_does_not_reopen_unknown_delivery(self):
        from agent_bridge_connect.docker_approval_bridge import DeliveryQueue, DeliveryFailure
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            first = DeliveryQueue()
            retry = ('TEST-001', 'request-a')
            active = ('TEST-002', 'request-b')
            first.start(retry)
            first.finish(retry, now=0, error=DeliveryFailure('no window'))
            first.start(active)
            first.save(path, 'owned')
            restored = DeliveryQueue()
            restored.restore(path, 'owned')
            self.assertEqual(restored.requests[retry]['attempts'], 1)
            self.assertFalse(restored.eligible(active, 100000))
            self.assertEqual(restored.requests[active]['state'], 'manual_action_required')
            other = DeliveryQueue()
            other.restore(path, 'replacement')
            self.assertFalse(other.requests)


class DockerTerminalRelayTests(unittest.TestCase):
    def test_restart_unknown_ui_never_replays_and_confirmed_ack_is_preserved(self):
        import json
        import tempfile
        from pathlib import Path
        from agent_bridge_connect.docker_terminal_bridge import TerminalRelay
        from agent_bridge_connect.installation import private_json
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            private_json(path, {'entries': {'a': {'state': 'delivering'}, 'b': {'state': 'ack_pending'}}})
            relay = TerminalRelay('replacement-container', path)
            try:
                self.assertEqual(relay.entries['a']['state'], 'manual_action_required')
                self.assertEqual(relay.entries['b']['state'], 'ack_pending')
                self.assertEqual(json.loads(path.read_text())['state'], 'degraded')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                relay.close()

    def test_informational_popup_has_no_permission_or_container_open_action(self):
        from agent_bridge_connect.docker_terminal_bridge import show_notice
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(show_notice({'task_id': 'ABCD-001', 'message': 'Task completed\nReport: /private/container/path'}))
            call = run.call_args
            self.assertIn('buttons {"OK"}', call.kwargs['input'])
            self.assertNotIn('/private/container/path', call.args[0][-1])
            self.assertIn('agentbc task report ABCD-001', call.args[0][-1])

    def test_confirmed_ui_ack_retry_does_not_show_again_and_failures_are_bounded(self):
        import io
        import json
        import tempfile
        from pathlib import Path
        from agent_bridge_connect.docker_terminal_bridge import TerminalRelay
        value = {'task_id': 'ABCD-001', 'delivery_id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}
        request = dict(value, payload={'task_id': value['task_id'], 'message': 'completed'})
        committed = {'result': {'delivery_id': value['delivery_id'], 'receipt': {'stages': {'ui_notification': {'state': 'succeeded'}}}}}
        def child(fail=False):
            p = mock.Mock(returncode=0)
            p.stdout = io.StringIO(json.dumps(request) + '\n')
            p.poll.return_value = 0
            if fail:
                p.communicate.side_effect = TimeoutError('ack lost')
            else:
                p.communicate.return_value = (json.dumps(committed), '')
            return p
        with tempfile.TemporaryDirectory() as temp:
            relay = TerminalRelay('container', Path(temp) / 'state.json')
            try:
                with mock.patch('agent_bridge_connect.docker_terminal_bridge.subprocess.Popen', side_effect=[child(True), child()]), mock.patch('agent_bridge_connect.docker_terminal_bridge.show_notice', return_value=True) as show:
                    relay.deliver(value)
                    self.assertEqual(relay.entries[relay.key(value)]['state'], 'ack_pending')
                    relay.deliver(value)
                    self.assertEqual(relay.entries[relay.key(value)]['state'], 'done')
                    self.assertEqual(show.call_count, 1)
                relay.entries.clear()
                with mock.patch('agent_bridge_connect.docker_terminal_bridge.subprocess.Popen', side_effect=[child(), child(), child()]), mock.patch('agent_bridge_connect.docker_terminal_bridge.show_notice', return_value=False):
                    for _ in range(3):
                        relay.deliver(value)
                    self.assertEqual(relay.entries[relay.key(value)]['state'], 'manual_action_required')
                    self.assertNotEqual(relay.entries[relay.key(value)]['state'], 'ack_pending')
            finally:
                relay.close()


class WindowsDesktopContractTests(unittest.TestCase):
    def payload(self, version=3):
        return {'approval_version': version, 'task_id': 'WIN-001', 'approval_max_time_s': 300,
                'deadline_at': '2099-01-01T00:00:00Z', 'reason_summary': 'declared action',
                'native_options': [{'kind': 'deny', 'handle': 'deny-bound'},
                                   {'kind': 'once', 'handle': 'once-bound'},
                                   {'kind': 'session', 'handle': 'session-bound'},
                                   {'kind': 'always', 'handle': 'audit-only', 'selectable': False}]}

    def test_explicit_grants_and_navigation_keep_native_handles(self):
        from agent_bridge_connect.desktop_dialog import windows_approval
        render = mock.Mock(side_effect=[{'outcome': 'selected', 'button': 102}, {'outcome': 'selected', 'button': 112}])
        result = windows_approval(self.payload(2), render=render)
        self.assertEqual(result.details['option_handle'], 'session-bound')
        self.assertEqual(result.details['decision_source'], 'user')
        self.assertEqual(render.call_args_list[0].args[0]['default'], 101)
        self.assertNotIn('audit-only', str(render.call_args_list))

    def test_full_only_from_explicit_button(self):
        from agent_bridge_connect.desktop_dialog import windows_approval
        result = windows_approval(self.payload(), render=lambda _: {'outcome': 'selected', 'button': 102})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {'action': 'approve_full', 'decision_source': 'user'})
        for outcome, source in [('closed', 'dialog_closed'), ('timeout', 'timeout')]:
            result = windows_approval(self.payload(), render=lambda _: {'outcome': outcome})
            self.assertEqual(result.details, {'action': 'deny', 'decision_source': source})

    def test_details_do_not_extend_deadline(self):
        from agent_bridge_connect.desktop_dialog import windows_approval
        now = [0]
        windows = []
        def render(request):
            windows.append(request)
            now[0] += 100
            return {'outcome': 'selected', 'button': 100}
        result = windows_approval(self.payload(), render=render, clock=lambda: now[0])
        self.assertEqual([w['timeout_s'] for w in windows], [300, 200, 100])
        self.assertEqual(result.details['decision_source'], 'timeout')

    def test_missing_desktop_and_unknown_visibility_do_not_deny(self):
        from agent_bridge_connect.desktop_dialog import windows_approval
        for outcome in ('delivery_failed', 'uncertain'):
            result = windows_approval(self.payload(), render=lambda _: {'outcome': outcome})
            self.assertFalse(result.ok)
            self.assertNotIn('action', result.details)
            self.assertEqual(result.details['uncertain'], outcome == 'uncertain')

    def test_v2_close_returns_only_offered_deny(self):
        from agent_bridge_connect.desktop_dialog import windows_approval
        result = windows_approval(self.payload(2), render=lambda _: {'outcome': 'closed'})
        self.assertEqual(result.details['option_handle'], 'deny-bound')
        payload = self.payload(2)
        payload['native_options'] = []
        render = mock.Mock()
        self.assertFalse(windows_approval(payload, render=render).ok)
        render.assert_not_called()

    def test_uncertain_approval_is_not_retryable_delivery_failure(self):
        from agent_bridge_connect.desktop_dialog import DeliveryResult
        from agent_bridge_connect.docker_approval_bridge import Bridge, DeliveryFailure
        bridge = object.__new__(Bridge)
        with mock.patch('agent_bridge_connect.docker_approval_bridge.show_approval', return_value=DeliveryResult(False, details={'uncertain': True})), mock.patch('agent_bridge_connect.docker_approval_bridge.execute') as execute:
            with self.assertRaises(RuntimeError) as failure:
                bridge.deliver('TEST-001', DockerApprovalBridgeTests().snapshot())
            self.assertNotIsInstance(failure.exception, DeliveryFailure)
            execute.assert_not_called()

    def test_notice_requires_actual_shown_and_unknown_raises(self):
        from agent_bridge_connect.desktop_dialog import windows_notice
        for value, accepted in [({'outcome': 'timeout', 'shown': True}, True), ({'outcome': 'delivery_failed', 'shown': False}, False)]:
            with mock.patch('agent_bridge_connect.desktop_dialog.windows_request', return_value=value):
                self.assertEqual(windows_notice({'task_id': 'WIN-001'}), accepted)
        with mock.patch('agent_bridge_connect.desktop_dialog.windows_request', return_value={'outcome': 'uncertain', 'shown': True}):
            with self.assertRaises(RuntimeError):
                windows_notice({'task_id': 'WIN-001'})

    def test_native_request_rejects_ambiguous_buttons_and_duration(self):
        from agent_bridge_connect.windows_dialog import validate_request
        for value in ({'timeout_s': float('inf')}, {'timeout_s': True}, {'body': 'bad\0text'},
                      {'buttons': [{'id': 100, 'text': 'A'}, {'id': 100, 'text': 'B'}]}, {'default': 999}):
            with self.assertRaises(ValueError):
                validate_request(value)

    def test_bounded_pipe_reaps_timeout_and_oversize(self):
        import subprocess
        import sys
        from agent_bridge_connect.desktop_dialog import read_line
        for code, timeout, error in [('import time;time.sleep(30)', 0.05, TimeoutError),
                                     ('print("x"*100)', 5, ValueError)]:
            p = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE)
            try:
                with self.assertRaises(error):
                    read_line(p, timeout, limit=64)
                self.assertIsNotNone(p.poll())
            finally:
                p.stdout.close()

    def test_desktop_ready_requires_exact_live_identity(self):
        import tempfile
        import json
        from pathlib import Path
        from agent_bridge_connect.windows_desktop import status
        binding = {'installation_id': 'install', 'container_id': 'container', 'instance_key': 'instance', 'sid': 'owner'}
        state = dict(binding, state='ready', heartbeat=100, pid=8, session_id=1, created=20)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'windows-desktop-state.json').write_text(json.dumps(state))
            with mock.patch('agent_bridge_connect.windows_desktop.read_binding', return_value=binding), mock.patch('agent_bridge_connect.windows_desktop.validate_private'), mock.patch('agent_bridge_connect.windows_desktop.time.time', return_value=101), mock.patch('agent_bridge_connect.windows_desktop.identity', return_value=state) as identity:
                self.assertEqual(status(root)['state'], 'ready')
                for mutation in ({'created': 21}, {'sid': 'other'}, {'session_id': 0}):
                    identity.return_value = dict(state, **mutation)
                    self.assertEqual(status(root)['state'], 'degraded')
                identity.return_value = state
                for mutation in ({'heartbeat': 0}, {'heartbeat': 200}, {'container_id': 'replacement'}, {'instance_key': 'replacement'}):
                    (root / 'windows-desktop-state.json').write_text(json.dumps(dict(state, **mutation)))
                    self.assertEqual(status(root)['state'], 'degraded')

    def test_formal_registration_uses_managed_python_not_maintenance_interpreter(self):
        import json
        from agent_bridge_connect import windows_desktop as desktop
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / 'venv/Scripts/pythonw.exe'
            python.parent.mkdir(parents=True)
            python.touch()
            path = root / 'docker-host.json'
            path.write_text(json.dumps({'schema': 2, 'installation_id': 'owned', 'container_id': 'container'}))
            with mock.patch.object(desktop, 'ensure_private_directory'), mock.patch.object(desktop, 'validate_private'), \
                 mock.patch.object(desktop, 'identity', return_value={'sid': 'owner'}), \
                 mock.patch.object(desktop, 'desktop_engine', return_value={'context': 'owned'}), \
                 mock.patch.object(desktop, 'desktop_docker', return_value=json.dumps([{'Id': 'container', 'Image': 'image'}])), \
                 mock.patch.object(desktop, 'task_operation'), mock.patch.object(desktop.shutil, 'which', return_value='docker'), \
                 mock.patch('agent_bridge_connect.docker_install_runtime.verify_binding'):
                binding = desktop.register(path, enable=True, start=False)
                self.assertEqual(binding['python'], str(python))
                self.assertNotIn('versions', binding['python'])

    def test_ssh_ensure_uses_registered_owner_and_bounded_wait(self):
        from pathlib import Path
        from agent_bridge_connect.windows_desktop import ensure
        with mock.patch('agent_bridge_connect.windows_desktop.status', return_value={'state': 'waiting_for_session'}), mock.patch('agent_bridge_connect.windows_desktop.register') as register, mock.patch('agent_bridge_connect.windows_desktop.time.monotonic', side_effect=[0, 41]):
            with self.assertRaisesRegex(RuntimeError, 'waiting_for_session'):
                ensure(Path('/host/docker-host.json'))
            register.assert_called_once_with(Path('/host/docker-host.json'), enable=True)
        with mock.patch('agent_bridge_connect.windows_desktop.status', return_value={'state': 'ready'}), mock.patch('agent_bridge_connect.windows_desktop.register') as register:
            ensure(Path('/host/docker-host.json'))
            register.assert_not_called()


class PreviewRecoveryTests(unittest.TestCase):
    def test_recovery_respects_close_and_isolates_webui_failure(self):
        import json
        from agent_bridge_connect import docker_preview_setup as setup
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'docker-host.json'
            path.write_text(json.dumps({'container_id': 'owned', 'settings': {'autostart': True}}))
            def execute(args):
                return json.dumps([{'State': {'Running': False}}]) if args[1] == 'inspect' else ''
            with mock.patch.object(setup, 'validate_private'), mock.patch.object(setup, 'verify', return_value=(Path(tmp) / 'compose.json', {})), mock.patch.object(setup, 'execute', side_effect=execute) as run, mock.patch('agent_bridge_connect.docker_webui.command', side_effect=RuntimeError('port conflict')) as web:
                for n in ('runner.closed', 'webui.closed'):
                    (path.parent / n).touch()
                self.assertEqual(setup.restore_services(path, reset_closed=False), '')
                web.assert_not_called()
                self.assertFalse(any('start' in c.args[0] for c in run.call_args_list))
                error = setup.restore_services(path, reset_closed=True)
                self.assertIn('Approval bridge remains active', error)
                self.assertFalse((path.parent / 'runner.closed').exists())
                self.assertFalse((path.parent / 'webui.closed').exists())
                self.assertTrue(any(c.args[0][-2:] == ['start', 'runner'] for c in run.call_args_list))
                web.side_effect = None
                self.assertEqual(setup.restore_services(path, reset_closed=False), '')
                with mock.patch.object(setup, 'verify', side_effect=ValueError('foreign identity')):
                    with self.assertRaisesRegex(ValueError, 'foreign identity'):
                        setup.restore_services(path, reset_closed=True)

    def test_runner_stop_checks_idle_and_stopped_start_uses_compose(self):
        import json
        import contextlib
        import io
        from agent_bridge_connect import docker_preview_setup as setup
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'docker-host.json'
            p = {'container_id': 'owned'}
            with mock.patch.object(setup, 'verify', return_value=(Path(tmp) / 'compose', {})), mock.patch.object(setup, 'execute', return_value=json.dumps([{'State': {'Running': True}}])) as run, mock.patch.object(setup, 'idle', side_effect=ValueError('unfinished')):
                with self.assertRaisesRegex(ValueError, 'unfinished'):
                    setup.runner_command(path, p, 'stop')
                self.assertFalse((path.parent / 'runner.closed').exists())
                self.assertEqual(run.call_count, 1)
            (path.parent / 'runner.closed').touch()
            with mock.patch.object(setup, 'verify', return_value=(Path(tmp) / 'compose', {})), mock.patch.object(setup, 'execute', side_effect=[json.dumps([{'State': {'Running': False}}]), '', '{"ok": true, "status": "ready"}']) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.runner_command(path, p, 'start'), 0)
                self.assertEqual(run.call_args_list[1].args[0][-2:], ['start', 'runner'])
                self.assertFalse((path.parent / 'runner.closed').exists())
