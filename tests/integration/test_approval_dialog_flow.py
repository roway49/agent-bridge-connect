"""Task 7 / PERM-103-006: minimal permission dialog and bounded details.

Covers the 1.0.3A approval/input contract slice:
- optional ``reason_summary`` (Core single-line, at most 120 chars) and
  ``reason_detail`` (redacted, control-character-free, at most 2000 chars) on
  the durable approval receipt; legacy receipts without either field stay valid;
- public status/report/text/JSON projections expose only the summary by default
  and never the bounded detail;
- the macOS permission dialog shows only Approve/Deny as decisions plus a
  non-decision ``View Details`` interaction; closing either view or reaching the
  total deadline auto-denies with an auditable ``decision_source``, the default
  remains Deny, and View Details/Back never responds, changes input state,
  issues a grant or resets the original absolute deadline.
"""

from __future__ import annotations
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.approval import APPROVAL_EXTENSION_KEY, APPROVAL_REASON_DETAIL_LIMIT, APPROVAL_REASON_SUMMARY_LIMIT, APPROVAL_SCOPE, approval_public_projection, build_approval_receipt, normalize_reason_summary, sanitize_reason_detail, validate_approval_receipt
from agent_bridge_connect.notifications import build_input_required_notification, notify_input_required
from agent_bridge_connect.notifiers.dialog import DialogNotifier
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, task_to_status
import sys as _native_approval_failure_sys
import tempfile as _native_approval_failure_tempfile
import unittest as _native_approval_failure_unittest
import os as _native_approval_failure_os
from pathlib import Path as _native_approval_failure_Path
from unittest import mock as _native_approval_failure_mock
from agent_bridge_connect.adapters import DeliveryResult as _native_approval_failure_DeliveryResult
from agent_bridge_connect.approval import compute_request_fingerprint as _native_approval_failure_compute_request_fingerprint
from agent_bridge_connect.claude_elevation import build_claude_elevation_receipt as _native_approval_failure_build_claude_elevation_receipt, stable_input_digest as _native_approval_failure_stable_input_digest, transition_claude_elevation as _native_approval_failure_transition_claude_elevation
from agent_bridge_connect.control import ApprovalControlPlane as _native_approval_failure_ApprovalControlPlane, ControlPlaneError as _native_approval_failure_ControlPlaneError, normalize_approval_request as _native_approval_failure_normalize_approval_request, SessionRecoveryRequired as _native_approval_failure_SessionRecoveryRequired
from agent_bridge_connect.execution_contract import CallbackValidation as _native_approval_failure_CallbackValidation, route_executor_terminal as _native_approval_failure_route_executor_terminal
from agent_bridge_connect.executors.claude import _build_prompt as _native_approval_failure__build_prompt
from agent_bridge_connect.executors.codex import _build_prompt as _native_approval_failure__build_codex_prompt
from agent_bridge_connect.notifications import notify_input_required as _native_approval_failure_notify_input_required, notify_terminal as _native_approval_failure_notify_terminal
from agent_bridge_connect.permission_modes import build_permission_record as _native_approval_failure_build_permission_record
from agent_bridge_connect.protocol import ABCError as _native_approval_failure_ABCError
from agent_bridge_connect.record_management import MAX_TASK_RECORD_BYTES as _native_approval_failure_MAX_TASK_RECORD_BYTES, task_record_size as _native_approval_failure_task_record_size
from agent_bridge_connect.runner import RunnerState as _native_approval_failure_RunnerState
from agent_bridge_connect.run_lease import create_lease as _native_approval_failure_create_lease, save_lease as _native_approval_failure_save_lease
from agent_bridge_connect.service import TaskService as _native_approval_failure_TaskService

TASK_ID = "ABCD-001"
RUN_ID = "claude-ABCD-001-run1"
SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


def _receipt(reason_summary: str = "", reason_detail: str = "") -> dict:
    return build_approval_receipt(
        task_id=TASK_ID, executor_run_id=RUN_ID, executor="claude", session_id=SESSION_ID, request_id="approval-request-1", request_fingerprint="fp-" + "a" * 40, operation="Bash", summary="claude needs one-time permission for: Bash", reason_summary=reason_summary, reason_detail=reason_detail
    )


class ReasonContractTests(unittest.TestCase):
    def test_legacy_receipt_without_reason_fields_remains_valid(self) -> None:
        raw = {
            "version": 1,
            "task_id": TASK_ID,
            "executor_run_id": RUN_ID,
            "executor": "claude",
            "session_id": SESSION_ID,
            "request_id": "approval-request-legacy",
            "request_fingerprint": "fp-" + "b" * 40,
            "kind": "permission",
            "operation": "Bash",
            "summary": "claude needs one-time permission for: Bash",
            "scope": APPROVAL_SCOPE,
            "created_at": "2026-01-01T00:00:00Z",
            "state": {"status": "pending"},
            "decision": {"type": "", "source": "", "decided_at": ""},
        }
        validated = validate_approval_receipt(raw)
        self.assertNotIn("reason_detail", validated)
        self.assertNotIn("reason_summary", validated)
        self.assertEqual(validated["state"]["status"], "pending")

    def test_reason_summary_is_single_line_and_bounded(self) -> None:
        summary = normalize_reason_summary("Run the\nrequested\t\ttests\x00 with a very long tail " + "x" * 300, executor="claude", operation="Bash")
        self.assertLessEqual(len(summary), APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertNotIn("\n", summary)
        self.assertNotIn("\t", summary)
        self.assertNotIn("\x00", summary)
        self.assertNotIn("  ", summary)
        self.assertTrue(summary.startswith("Run the requested tests"))

    def test_reason_summary_falls_back_to_core_summary(self) -> None:
        summary = normalize_reason_summary("", executor="claude", operation="Bash")
        self.assertEqual(summary, "claude needs one-time permission for: Bash")
        self.assertLessEqual(len(summary), APPROVAL_REASON_SUMMARY_LIMIT)

    def test_reason_summary_redacts_secrets(self) -> None:
        summary = normalize_reason_summary("token: abc123def password=hunter2 credential=secretword access token: alphakey", executor="claude", operation="Bash")
        self.assertNotIn("abc123def", summary)
        self.assertNotIn("hunter2", summary)
        self.assertNotIn("secretword", summary)
        self.assertNotIn("alphakey", summary)
        self.assertIn("[REDACTED]", summary)

    def test_reason_detail_is_bounded_to_2000(self) -> None:
        detail = sanitize_reason_detail("x" * (APPROVAL_REASON_DETAIL_LIMIT + 500))
        self.assertEqual(len(detail), APPROVAL_REASON_DETAIL_LIMIT)

    def test_reason_detail_redacts_and_strips_control_characters(self) -> None:
        detail = sanitize_reason_detail("First line\npassword=hunter2 token: abc123\tsecond\x00third credential=secretword access token: alphakey")
        self.assertNotIn("\n", detail)
        self.assertNotIn("\t", detail)
        self.assertNotIn("\x00", detail)
        self.assertNotIn("hunter2", detail)
        self.assertNotIn("abc123", detail)
        self.assertNotIn("secretword", detail)
        self.assertNotIn("alphakey", detail)
        self.assertIn("First line", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertIn("second", detail)
        self.assertIn("third", detail)

    def test_reason_detail_leading_path_is_dropped(self) -> None:
        self.assertEqual(sanitize_reason_detail("/Users/me/private/db.sqlite"), "")
        self.assertEqual(sanitize_reason_detail("~/private/session.db"), "")
        self.assertEqual(sanitize_reason_detail("run tests in /tmp/sandbox"), "run tests in /tmp/sandbox")

    def test_reason_detail_embedded_private_path_fails_closed(self) -> None:
        self.assertEqual(sanitize_reason_detail("Reason: inspect /Users/alice/.hermes/state.db"), "")
        self.assertEqual(sanitize_reason_detail("see ~/private/session.db for the schema"), "")

    def test_reason_detail_argv_and_raw_output_fail_closed(self) -> None:
        self.assertEqual(sanitize_reason_detail("argv: hermes --token secret-value --cwd /private/tmp/x"), "")
        self.assertEqual(sanitize_reason_detail("command: ls -la /Users/me"), "")
        self.assertEqual(sanitize_reason_detail("stdout: hello world"), "")
        self.assertEqual(sanitize_reason_detail("raw output: see /tmp/log"), "")

    def test_reason_detail_secret_flag_fails_closed(self) -> None:
        self.assertEqual(sanitize_reason_detail("run with --token secret-value"), "")
        self.assertEqual(sanitize_reason_detail("--password hunter2 passed"), "")

    def test_reason_detail_legitimate_short_text_still_usable(self) -> None:
        detail = sanitize_reason_detail("Requested operation: run the focused unit tests in the workspace")
        self.assertEqual(detail, "Requested operation: run the focused unit tests in the workspace")
        self.assertLessEqual(len(detail), APPROVAL_REASON_DETAIL_LIMIT)

    def test_validation_rejects_embedded_sensitive_detail_fail_closed(self) -> None:
        receipt = _receipt(reason_detail="safe")
        receipt["reason_detail"] = "Reason: inspect /Users/alice/.hermes/state.db"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_sensitive_field")
        receipt["reason_detail"] = "argv: hermes --token secret-value"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_build_receipt_persists_reason_fields(self) -> None:
        receipt = _receipt(reason_summary="Run the requested tests", reason_detail="The bounded detail")
        self.assertEqual(receipt["reason_summary"], "Run the requested tests")
        self.assertEqual(receipt["reason_detail"], "The bounded detail")

    def test_validation_rejects_out_of_bounds_detail(self) -> None:
        receipt = _receipt(reason_detail="x")
        receipt["reason_detail"] = "y" * (APPROVAL_REASON_DETAIL_LIMIT + 1)
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_reason_detail_invalid")

    def test_validation_rejects_control_characters_in_summary(self) -> None:
        receipt = _receipt(reason_summary="one line")
        receipt["reason_summary"] = "two\nlines"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_reason_summary_invalid")

    def test_validation_rejects_sensitive_detail_fail_closed(self) -> None:
        receipt = _receipt(reason_detail="safe")
        receipt["reason_detail"] = "see token: abc123"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_reason_detail_space_separated_credentials_fail_closed(self) -> None:
        samples = ["token huntertwo", "Authorization hunter", "password letmein", "credential secretword", "api key alphakey", "Bearer abc123", "Authorization abc123", "Authorization: Bearer abc123", "token abc123", "access token abc123", "api key abc123", "password hunter2", "credential abc123"]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_reason_detail(sample), "", f"sanitize_reason_detail leaked {sample!r}")
                receipt = _receipt(reason_detail="safe")
                receipt["reason_detail"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(exc.exception.code, "approval_sensitive_field", f"validate_approval_receipt leaked {sample!r}")

    def test_reason_detail_case_variants_fail_closed(self) -> None:
        samples = ["TOKEN huntertwo", "BEARER hunter", "Access Token alphakey", "API Key secretword", "Password LETMEIN", "PASSWD passphrase", "Credential secretword", "AUTHORIZATION hunter"]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_reason_detail(sample), "", f"case variant leaked {sample!r}")
                receipt = _receipt(reason_detail="safe")
                receipt["reason_detail"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_reason_detail_embedded_credentials_fail_closed(self) -> None:
        samples = ["The access token huntertwo is required", "Use token letmein for this action", "mixed text Bearer hunter end", "pass the password secretword please", "see credential alphakey, then proceed", "The api key hunter is used here"]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_reason_detail(sample), "", f"embedded credential leaked {sample!r}")

    def test_reason_detail_prose_does_not_false_positive(self) -> None:
        samples = [
            "Requested operation: run the focused unit tests in the workspace",
            "The access token is required for the endpoint",
            "token is required",
            "tokenization is performed by the pipeline",
            "the token for the api is sent over tls",
            "api key rotation completed",
            "run tests in /tmp/sandbox",
            "password policy requires rotation",
            "credential store is sealed",
            "authorization header is set",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_reason_detail(sample), sample)
                self.assertEqual(normalize_reason_summary(sample, executor="claude", operation="Bash"), sample)
                self.assertLessEqual(len(sanitize_reason_detail(sample)), APPROVAL_REASON_DETAIL_LIMIT)

    def test_reason_detail_bearer_jwt_fragment_fails_closed(self) -> None:
        for sample in ("Bearer eyJhbGciOi", "Authorization: Bearer eyJhbGciOi"):
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_reason_detail(sample), "")
                receipt = _receipt(reason_detail="safe")
                receipt["reason_detail"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_build_approval_receipt_never_persists_credential(self) -> None:
        for sample, credential in (("The access token huntertwo is required", "huntertwo"), ("Bearer hunter", "hunter"), ("api key alphakey", "alphakey"), ("password letmein", "letmein"), ("credential secretword", "secretword")):
            with self.subTest(sample=sample):
                receipt = build_approval_receipt(task_id=TASK_ID, executor_run_id=RUN_ID, executor="claude", session_id=SESSION_ID, request_id="approval-request-1", request_fingerprint="fp-" + "a" * 40, operation="Bash", summary="claude needs one-time permission for: Bash", reason_detail=sample)
                serialized = json.dumps(receipt)
                self.assertNotIn(credential, serialized)
                self.assertNotIn("reason_detail", receipt)

    def test_reason_summary_space_separated_credentials_redacted(self) -> None:
        for sample, credential in (("token huntertwo", "huntertwo"), ("Authorization hunter", "hunter"), ("The access token alphakey is required", "alphakey"), ("api key secretword", "secretword"), ("password letmein", "letmein"), ("credential passphrase", "passphrase")):
            with self.subTest(sample=sample):
                summary = normalize_reason_summary(sample, executor="claude", operation="Bash")
                self.assertNotIn(credential, summary)
                self.assertIn("[REDACTED]", summary)

    def test_build_receipt_redacts_summary_and_detail_credentials(self) -> None:
        for sample, credential in (("token huntertwo", "huntertwo"), ("Authorization hunter", "hunter"), ("password letmein", "letmein"), ("credential secretword", "secretword"), ("api key alphakey", "alphakey")):
            with self.subTest(sample=sample):
                receipt = build_approval_receipt(
                    task_id=TASK_ID, executor_run_id=RUN_ID, executor="claude", session_id=SESSION_ID, request_id="approval-request-1", request_fingerprint="fp-" + "a" * 40, operation="Bash", summary="claude needs one-time permission for: Bash", reason_summary=sample, reason_detail=sample
                )
                serialized = json.dumps(receipt)
                self.assertNotIn(credential, serialized)
                self.assertNotIn("reason_detail", receipt)
                self.assertIn("[REDACTED]", receipt["reason_summary"])

    def test_validation_rejects_credential_in_manual_summary(self) -> None:
        for sample in ("token huntertwo", "Authorization hunter", "The access token alphakey is required", "api key secretword", "password letmein", "credential passphrase"):
            with self.subTest(sample=sample):
                receipt = _receipt(reason_summary="safe")
                receipt["reason_summary"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(exc.exception.code, "approval_sensitive_field", f"manual summary leaked {sample!r}")


class PublicProjectionTests(unittest.TestCase):
    def test_approval_public_projection_exposes_only_summary(self) -> None:
        receipt = _receipt(reason_summary="Run the requested tests", reason_detail="A bounded detail that must never be projected")
        projection = approval_public_projection(receipt)
        self.assertNotIn("reason_detail", projection)
        self.assertNotIn("request_id", projection)
        self.assertNotIn("session_id", projection)
        self.assertEqual(projection["summary"], receipt["summary"])
        self.assertEqual(projection.get("reason_summary"), "Run the requested tests")


class PermissionDialogTests(unittest.TestCase):
    def test_view_details_button_only_with_detail(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
            notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission"})
        script = run.call_args.kwargs["input"]
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', script)
        self.assertIn('default button "Deny"', script)
        self.assertNotIn('"Later"', script)
        self.assertNotIn("default answer", script)
        self.assertNotIn('"Submit"', script)
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
            notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "input_type": "permission"})
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', run.call_args.kwargs["input"])

    def test_input_action_maps_view_details_as_non_decision(self) -> None:
        self.assertEqual(DialogNotifier._input_action("View Details", "permission", False), "view_details")
        self.assertEqual(DialogNotifier._input_action("Approve", "permission", False), "approve")
        self.assertEqual(DialogNotifier._input_action("Deny", "permission", False), "deny")
        self.assertEqual(DialogNotifier._input_action("unknown", "permission", False), "deny")
        self.assertEqual(DialogNotifier._input_action("Approve", "permission", True), "deny")

    def test_view_details_back_returns_to_decision_without_responding(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""), mock.Mock(returncode=0, stdout="button returned:Back, gave up:false", stderr=""), mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr="")]
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission", "deadline_at": "2099-01-01T00:00:00Z"})
        self.assertEqual(result.details, {"action": "approve", "decision_source": "user"})
        self.assertEqual(run.call_args_list[0].args[0][3], "short summary")
        self.assertEqual(run.call_args_list[1].args[0][3], "The bounded detail")
        self.assertEqual(run.call_args_list[2].args[0][3], "short summary")
        self.assertEqual(len(run.call_args_list), 3)

    def test_close_detail_view_auto_denies_with_dialog_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""), mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")]
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission"})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "dialog_closed"})

    def test_close_decision_view_auto_denies_with_dialog_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission"})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "dialog_closed"})

    def test_timeout_auto_denies_with_source_timeout(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:true", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission"})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})

    def test_detail_timeout_auto_denies_with_source_timeout(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""), mock.Mock(returncode=0, stdout="button returned:Back, gave up:true", stderr="")]
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission"})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})

    def test_configured_limit_above_default_reaches_native_popup(self) -> None:
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:true", stderr="")
            result = DialogNotifier().send({"event_type": "task.input_required", "input_type": "permission", "approval_max_time_s": 900, "deadline_at": "2099-01-01T00:00:00Z"})
        self.assertIn("giving up after 900", run.call_args.kwargs["input"])
        self.assertEqual(result.details["decision_source"], "timeout")

    def test_expired_deadline_does_not_start_approvable_dialog(self) -> None:
        notifier = DialogNotifier()
        expired = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(milliseconds=5)).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "input_type": "permission", "deadline_at": expired})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        run.assert_not_called()

    def test_critical_sub_second_deadline_cannot_cross_deadline(self) -> None:
        notifier = DialogNotifier()
        critical = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(milliseconds=900)).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "input_type": "permission", "deadline_at": critical})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        run.assert_not_called()

    def test_view_details_loop_keeps_absolute_deadline(self) -> None:
        notifier = DialogNotifier()
        far_future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        with mock.patch.object(DialogNotifier, "_permission_give_up_seconds") as countdown:
            countdown.side_effect = [5, 5, 0]
            with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
                run.side_effect = [mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""), mock.Mock(returncode=0, stdout="button returned:Back, gave up:false", stderr="")]
                result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission", "deadline_at": far_future})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        self.assertEqual(len(run.call_args_list), 2)

    def test_unknown_button_auto_denies_with_fail_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Mystery, gave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission"})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "fail_closed"})

    def test_deadline_bounds_dialog_countdown_and_never_resets(self) -> None:
        notifier = DialogNotifier()
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission", "deadline_at": past})
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        run.assert_not_called()
        far_future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny, gave up:false", stderr="")
            notifier.send({"event_type": "task.input_required", "message": "short summary", "reason_summary": "short summary", "reason_detail": "The bounded detail", "input_type": "permission", "deadline_at": far_future})
        self.assertIn("giving up after 300", run.call_args.kwargs["input"])


class ApprovalFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})
        self.task_id = self._started_task()

    def _create_task(self) -> str:
        task = self.service.create_task("approval flow", "claude", [{"id": 1, "description": "one step"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        return task.id

    def _started_task(self) -> str:
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
        return task_id

    def _session_id(self) -> str:
        return str((self.service.get_task(self.task_id).extensions or {})["agentbc.session"]["session_id"])

    def _block_approval(self, *, reason: str = "", reason_detail: str = "") -> None:
        self.service.block_task_for_approval(
            self.task_id, executor_run_id=RUN_ID, session_id=self._session_id(), request_id="approval-request-task7", request_fingerprint="fp-" + "t" * 40, executor="claude", operation="Bash", summary="claude needs one-time permission for: Bash", reason=reason, reason_detail=reason_detail
        )

    def test_configured_core_deadline_is_frozen_and_carried_to_dialog(self) -> None:
        self.service.config["approval"] = {"max_time": 900}
        self._block_approval()
        request = self.service.get_task(self.task_id).extensions["agentbc.input"]
        created = dt.datetime.fromisoformat(request["created_at"].replace("Z", "+00:00"))
        deadline = dt.datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
        self.assertEqual((deadline - created).total_seconds(), 900)
        self.service.config["approval"]["max_time"] = 1
        payload = build_input_required_notification(self.service, self.task_id)
        self.assertEqual(payload["approval_max_time_s"], 900)
        self.assertEqual(payload["deadline_at"], request["deadline_at"])

    def test_block_approval_persists_reason_summary_and_bounded_detail(self) -> None:
        self._block_approval(reason="Run the requested tests\nwith token: abc123", reason_detail="A bounded detail " * 300)
        task = self.service.get_task(self.task_id)
        receipt = task.extensions[APPROVAL_EXTENSION_KEY]
        self.assertLessEqual(len(receipt["reason_summary"]), APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertNotIn("\n", receipt["reason_summary"])
        self.assertNotIn("abc123", json.dumps(receipt))
        self.assertLessEqual(len(receipt["reason_detail"]), APPROVAL_REASON_DETAIL_LIMIT)
        request = task.extensions["agentbc.input"]
        self.assertEqual(request["reason_summary"], receipt["reason_summary"])
        self.assertNotIn("reason_detail", request)

    def test_notification_message_and_projection_expose_only_summary(self) -> None:
        self._block_approval(reason="Run the requested tests", reason_detail="A very private bounded detail")
        notification = build_input_required_notification(self.service, self.task_id)
        self.assertIn("Run the requested tests", notification["message"])
        self.assertNotIn("A very private bounded detail", notification["message"])
        self.assertEqual(notification["reason_summary"], "Run the requested tests")
        self.assertEqual(notification["reason_detail"], "A very private bounded detail")
        self.assertEqual(notification["input_type"], "permission")
        self.assertEqual(notification["approval_request_id"], "approval-request-task7")
        status = task_to_status(self.service.get_task(self.task_id))
        self.assertNotIn("A very private bounded detail", json.dumps(status))
        report = generate_report(self.task_id, self.board)
        self.assertNotIn("A very private bounded detail", json.dumps(report))
        self.assertNotIn("A very private bounded detail", generate_report_md(self.task_id, self.board))

    def test_view_details_back_never_changes_deadline_or_input_state(self) -> None:
        self._block_approval(reason="Run the requested tests", reason_detail="The bounded detail")
        before = self.service.get_task(self.task_id)
        deadline_before = before.extensions["agentbc.input"]["deadline_at"]
        request = before.extensions["agentbc.input"]

        def respond(input_id: str, action: str, message: str) -> dict:
            return self.service.respond_to_input(self.task_id, input_id, response_type=action, message=message)

        responder = mock.Mock(side_effect=respond)
        with mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=DeliveryResult(True, "dialog shown", details={"action": "deny", "decision_source": "user"})):
            result = notify_input_required(self.service, self.task_id, responder=responder)
        responder.assert_called_once_with(request["input_id"], "deny", "")
        self.assertEqual(result["response"]["request_id"], request["request_id"])
        after = self.service.get_task(self.task_id)
        self.assertEqual(after.extensions["agentbc.input"]["deadline_at"], deadline_before)
        self.assertEqual(after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["type"], "deny")
        self.assertEqual(after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"], "user")

    def test_respond_after_viewing_detail_binds_exact_request(self) -> None:
        self._block_approval(reason="Run the requested tests", reason_detail="The bounded detail")
        request = self.service.get_task(self.task_id).extensions["agentbc.input"]
        result = self.service.respond_to_input(self.task_id, request["input_id"], response_type="approve")
        self.assertEqual(result["approval_decision"], "approve")
        self.assertEqual(result["request_id"], "approval-request-task7")
        self.assertEqual(self.service.get_task(self.task_id).extensions[APPROVAL_EXTENSION_KEY]["request_id"], "approval-request-task7")


class _native_approval_failure_NativeApprovalFailClosedTests(_native_approval_failure_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _native_approval_failure_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _native_approval_failure_Path(self.temporary.name).resolve()

    def test_native_prompt_has_no_compatibility_full_contradiction(self) -> None:
        prompt = _native_approval_failure__build_prompt(
            {"task_id": "T2AB-001", "steps": [{"id": 1, "description": "run the native canary"}], "workspace": {"root": str(self.root), "project_root": str(self.root), "artifact_root": str(self.root)}, "task_board": {"root": str(self.root / "record")}, "runner_authorization_required": True}
        )
        self.assertIn("only the native can_use_tool event can block", prompt)
        self.assertNotIn("all blockers request full", prompt)
        self.assertNotIn("requested_permission full", prompt)

    def test_model_input_required_is_recovery_for_native_authority(self) -> None:
        validation = _native_approval_failure_CallbackValidation(
            marker_seen=True,
            valid=True,
            callback={"version": 1, "task_id": "T2AB-001", "final_state": "input_required", "summary": "model asks for full", "input": {"type": "permission", "requested_permission": "full", "reason": "model text is not native evidence"}, "step_results": [{"id": 1, "status": "blocked"}]},
        )
        routed = _native_approval_failure_route_executor_terminal(validation, 0, executor_name="claude", native_approval_authoritative=True)
        self.assertEqual(routed.status, "needs_recovery")
        self.assertIsNone(routed.callback)
        self.assertEqual((routed.failure or {}).get("kind"), "native_permission_callback_ignored")

    def test_codex_native_prompt_has_no_compatibility_full_contradiction(self) -> None:
        prompt = _native_approval_failure__build_codex_prompt(
            {"task_id": "T2A4-001", "steps": [{"id": 1, "description": "repair the worker"}], "workspace": {"root": str(self.root), "project_root": str(self.root), "artifact_root": str(self.root)}, "task_board": {"root": str(self.root / "record")}}, native_single_action=True
        )
        self.assertIn("structured requestApproval event", prompt)
        self.assertNotIn("requested_permission full", prompt)

    def test_incomplete_native_event_cannot_create_pending_input(self) -> None:
        session_id = "22222222-2222-4222-8222-222222222222"
        plane = _native_approval_failure_ApprovalControlPlane(self.root / "control", task_id="T2AB-001", executor_run_id="claude-raxt-run", session_id=session_id, executor="claude")
        plane.record_session_started({"version": 1, "executor": "claude", "session_id": session_id, "resumed": False, "persistence": "persistent", "source": "preallocated"})
        with self.assertRaises(_native_approval_failure_ControlPlaneError) as raised:
            plane.request_approval({"jsonrpc": "2.0", "id": "req-raxt-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": session_id, "turnId": "turn-1", "itemId": "toolu-raxt-1"}})
        self.assertEqual(raised.exception.code, "permission_block_evidence_unavailable")
        self.assertIsNone(plane.status().get("pending_request"))
        self.assertEqual(plane.status().get("status"), "needs_recovery")


class _native_approval_failure_ClaudeV3IdentityClosureTests(_native_approval_failure_unittest.TestCase):
    """The normalized v3 request keeps native Claude identity end to end."""

    PATH_DIGEST = "sha256:" + "1" * 64
    PROFILE_DIGEST = "sha256:" + "2" * 64

    def setUp(self) -> None:
        self.temporary = _native_approval_failure_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _native_approval_failure_Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def _message(self, *, session_id: str, tool_name: str = "Read", tool_use_id: str = "tool-read-1", input_data: dict | None = None) -> dict:
        input_value = input_data or {"file_path": "/tmp/native-read.txt"}
        request_fingerprint = _native_approval_failure_compute_request_fingerprint(executor="claude", session_id=session_id, tool_name=tool_name, tool_input=input_value, extra={"tool_use_id": tool_use_id})
        action_fingerprint = _native_approval_failure_compute_request_fingerprint(executor="claude", session_id=session_id, tool_name=tool_name, tool_input=input_value)
        input_fingerprint = _native_approval_failure_stable_input_digest(input_value)
        return {
            "jsonrpc": "2.0",
            "id": "approval-native-read-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": session_id, "turnId": "turn-native-1", "itemId": tool_use_id, "reason": "native SDK approval"},
            "_agentbc": {
                "task_id": "R3CLOSE-001",
                "executor_run_id": "claude-R3CLOSE-run",
                "tool_use_id": tool_use_id,
                "request_id": "approval-native-read-1",
                "tool_name": tool_name,
                "request_fingerprint": request_fingerprint,
                "input_fingerprint": input_fingerprint,
                "action_fingerprint": action_fingerprint,
                "control_path": "sdk_control_transport",
                "native_event": "claude_sdk_can_use_tool",
                "escalation_domain": "executor_policy",
                "host_profile_digest": self.PROFILE_DIGEST,
                "path_plan_digest": self.PATH_DIGEST,
                "containment_profile_digest": self.PROFILE_DIGEST,
            },
            "escalation_domain": "executor_policy",
            "host_profile_digest": self.PROFILE_DIGEST,
            "approval_version": 3,
            "scope": "task_elevation",
            "elevation_mode": "full",
            "native_live_elevation": True,
            "native_elevation_protocol": "claude.can_use_tool.setMode",
            "native_event": "claude_sdk_can_use_tool",
            "path_plan_digest": self.PATH_DIGEST,
            "containment_profile_digest": self.PROFILE_DIGEST,
            "preflight": {"ok": True, "status": "passed", "mode": "full"},
            "authority": {"executor": "claude", "protocol": "claude_agent_sdk", "protocol_version": 1, "method": "sdk.can_use_tool", "update": {"type": "setMode", "mode": "bypassPermissions", "destination": "session"}},
        }

    def test_normalized_v3_preserves_native_tool_and_rejects_conflict(self) -> None:
        session_id = "44444444-4444-4444-8444-444444444444"
        message = self._message(session_id=session_id)
        normalized = _native_approval_failure_normalize_approval_request(message, task_id="R3CLOSE-001", executor_run_id="claude-R3CLOSE-run", session_id=session_id, executor="claude")
        self.assertEqual(normalized.operation, "command")
        self.assertEqual(normalized.tool_name, "Read")
        self.assertEqual(normalized.tool_use_id, "tool-read-1")
        self.assertEqual(normalized.input_fingerprint, message["_agentbc"]["input_fingerprint"])
        plane = _native_approval_failure_ApprovalControlPlane(self.root / "control", task_id="R3CLOSE-001", executor_run_id="claude-R3CLOSE-run", session_id=session_id, executor="claude")
        plane.record_session_started({"version": 1, "executor": "claude", "session_id": session_id, "resumed": False, "persistence": "persistent", "source": "preallocated"})
        plane.request_approval(message)
        pending = plane.status()["pending_request"]
        self.assertEqual(pending["operation"], "command")
        self.assertEqual(pending["tool_name"], "Read")
        self.assertEqual(pending["input_fingerprint"], message["_agentbc"]["input_fingerprint"])
        conflict = self._message(session_id=session_id, tool_name="Bash")
        with self.assertRaises(_native_approval_failure_ControlPlaneError) as raised:
            plane.request_approval(conflict)
        self.assertEqual(raised.exception.code, "permission_block_evidence_unavailable")

    def _pending_live_approval(self):
        service = _native_approval_failure_TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})
        task = service.create_task("Claude native identity closure", "claude", [{"id": 1, "description": "read one file"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        service.start_task_run(task.id, "claude")
        run_id = "claude-R3CLOSE-run"
        service.record_executor_run_started(task.id, run_id)
        run_lease = _native_approval_failure_create_lease(task.id, "claude", _native_approval_failure_os.getpid(), str(self.project))
        run_lease.run_id = run_id
        _native_approval_failure_save_lease(run_lease, self.board)
        current = service.get_task(task.id)
        session_id = str(current.extensions["agentbc.session"]["session_id"])
        input_data = {"file_path": "/tmp/native-read.txt"}
        request_fingerprint = _native_approval_failure_compute_request_fingerprint(executor="claude", session_id=session_id, tool_name="Read", tool_input=input_data, extra={"tool_use_id": "tool-read-1"})
        action_fingerprint = _native_approval_failure_compute_request_fingerprint(executor="claude", session_id=session_id, tool_name="Read", tool_input=input_data)
        input_fingerprint = _native_approval_failure_stable_input_digest(input_data)
        receipt = _native_approval_failure_transition_claude_elevation(
            _native_approval_failure_build_claude_elevation_receipt(
                task_id=task.id,
                executor_run_id=run_id,
                session_id=session_id,
                request_id="approval-native-read-1",
                tool_use_id="tool-read-1",
                request_fingerprint=request_fingerprint,
                input_fingerprint=input_fingerprint,
                action_fingerprint=action_fingerprint,
                operation="Read",
                path_plan_digest=self.PATH_DIGEST,
                containment_profile_digest=self.PROFILE_DIGEST,
            ),
            "elevation_pending",
        )
        service.record_claude_elevation_transition(task.id, receipt)
        kwargs = {
            "executor_run_id": run_id,
            "session_id": session_id,
            "request_id": "approval-native-read-1",
            "request_fingerprint": request_fingerprint,
            "executor": "claude",
            "operation": "command",
            "summary": "native SDK approval",
            "tool_name": "Read",
            "tool_use_id": "tool-read-1",
            "input_fingerprint": input_fingerprint,
            "action_fingerprint": action_fingerprint,
            "escalation_domain": "executor_policy",
            "profile_digest": self.PROFILE_DIGEST,
            "control_path": "sdk_control_transport",
            "native_event": "claude_sdk_can_use_tool",
            "authority": {"executor": "claude", "protocol": "claude_agent_sdk", "protocol_version": 1, "method": "sdk.can_use_tool", "update": {"type": "setMode", "mode": "bypassPermissions", "destination": "session"}},
            "approval_version": 3,
            "elevation_mode": "full",
            "path_plan_digest": self.PATH_DIGEST,
            "containment_profile_digest": self.PROFILE_DIGEST,
            "full_preflight": {"ok": True, "status": "passed", "mode": "full"},
            "native_live_elevation": True,
        }
        first = service.block_task_for_approval(task.id, **kwargs)
        return service, task, run_id, first, kwargs

    def test_live_denial_preserves_timeout_and_close_sources(self):
        for source, message in (("timeout", "agentbc_permission_dialog_timeout"), ("dialog_closed", "agentbc_permission_dialog_closed")):
            service, task, run_id, first, kwargs = self._pending_live_approval()
            service.respond_to_live_claude_elevation(task.id, first["input_id"], response_type="deny", message=message)
            answered = service.get_task(task.id)
            self.assertEqual(answered.extensions["agentbc.input"]["response"]["source"], source)
            self.assertEqual(answered.extensions["agentbc.input"]["approval_decision"], "deny")

    def test_native_timeout_after_failed_terminal_is_reconciled_without_revival(self) -> None:
        service, task, run_id, first, kwargs = self._pending_live_approval()
        service.mark_task_failed(task.id, "approval_request_expired", "native SDK timeout")
        from agent_bridge_connect.run_lease import load_lease, close_lease
        lease = load_lease(task.id, self.board)
        close_lease(lease, self.board)
        before = service.get_task(task.id)
        self.assertEqual(before.status, "failed")
        self.assertEqual(before.extensions["agentbc.input"]["status"], "waiting")
        errors = list(before.errors)
        from types import SimpleNamespace
        for state, bound_run in (("active", run_id), ("closed", "different-run")):
            with mock.patch("agent_bridge_connect.run_lease.load_lease", return_value=SimpleNamespace(state=state, run_id=bound_run)):
                self.assertEqual(service.expire_waiting_inputs(now="2099-01-01T00:00:00Z"), [])
                self.assertEqual(service.get_task(task.id).extensions["agentbc.input"]["status"], "waiting")
        result = service.expire_waiting_inputs(now="2099-01-01T00:00:00Z")
        self.assertEqual(result, [{"task_id": task.id, "input_id": first["input_id"]}])
        after = service.get_task(task.id)
        self.assertEqual(after.status, "failed")
        self.assertEqual(after.errors, errors)
        self.assertEqual(after.extensions["agentbc.input"]["status"], "answered")
        self.assertEqual(after.extensions["agentbc.input"]["response"]["source"], "timeout")
        self.assertEqual(service.expire_waiting_inputs(now="2099-01-01T00:00:00Z"), [])

    def test_task_store_duplicate_is_idempotent_and_outer_operation_is_display_only(self) -> None:
        service, task, run_id, first, kwargs = self._pending_live_approval()
        changed_outer_operation = dict(kwargs)
        changed_outer_operation["operation"] = "file_change"
        duplicate = service.block_task_for_approval(task.id, **changed_outer_operation)
        self.assertEqual(first["input_id"], duplicate["input_id"])
        self.assertTrue(duplicate["idempotent"])
        with _native_approval_failure_mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=_native_approval_failure_DeliveryResult(True, "native dialog delivered")) as send:
            _native_approval_failure_notify_input_required(service, task.id)
            _native_approval_failure_notify_input_required(service, task.id)
        self.assertEqual(send.call_count, 1)
        with self.assertRaisesRegex(_native_approval_failure_ABCError, "native identity or input"):
            conflicting = dict(kwargs)
            conflicting["tool_name"] = "Bash"
            service.block_task_for_approval(task.id, **conflicting)
        after = service.get_task(task.id)
        self.assertEqual(after.extensions["agentbc.input"]["input_id"], first["input_id"])
        self.assertEqual(after.extensions["agentbc.permission_elevation"]["cardinality"]["notifications"], 1)
        response = service.respond_to_live_claude_elevation(task.id, first["input_id"], response_type="approve")
        self.assertFalse(response["dispatch_required"])
        self.assertEqual(service.get_task(task.id).status, "running")
        self.assertEqual(service.get_task(task.id).extensions["agentbc.session"]["session_state"], "active")


class _native_approval_failure_NativeApprovalFailClosedContinuationTests(_native_approval_failure_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _native_approval_failure_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _native_approval_failure_Path(self.temporary.name).resolve()

    def test_receipt_must_precede_prompt_and_missing_receipt_has_no_wait(self) -> None:
        board = self.root / "record"
        board.mkdir()
        project = self.root / "project"
        project.mkdir()
        fake = self.root / "claude"
        fake.write_text("#!/bin/sh\nprintf '2.1.233 (Claude Code)\\n'\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | 64)
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        executor = ClaudeExecutor(command=str(fake), transport="direct")
        executor._version = "2.1.233 (Claude Code)"
        packet = {
            "task_id": "T2AB-001",
            "assignee": "claude",
            "steps": [{"id": 1, "description": "run the native canary"}],
            "workspace": {"root": str(project), "project_root": str(project), "artifact_root": str(project)},
            "task_board": {"root": str(board)},
            "extensions": {"agentbc.permission": _native_approval_failure_build_permission_record(explicit_mode="safe")},
            "runner_authorization_required": True,
        }
        with (
            _native_approval_failure_mock.patch.object(executor, "supports_permission_prompt_tool", return_value=True),
            _native_approval_failure_mock.patch("agent_bridge_connect.permission_transport.assert_claude_sdk_environment", return_value={"supported": True}),
            _native_approval_failure_mock.patch("agent_bridge_connect.executors.claude._build_prompt", wraps=_native_approval_failure__build_prompt) as prompt,
        ):
            result = executor.start_control(packet)
        self.assertTrue(result.ok)
        self.assertEqual(executor.poll(result.run_id).status, "needs_recovery")
        self.assertEqual((executor.poll(result.run_id).result.get("failure") or {}).get("kind"), "session_receipt_missing")
        prompt.assert_not_called()
        plane = executor._control_planes[result.run_id]
        self.assertIsNone(plane.status().get("pending_request"))

    def test_session_receipt_identity_mismatch_fails_closed(self) -> None:
        session_id = "33333333-3333-4333-8333-333333333333"
        plane = _native_approval_failure_ApprovalControlPlane(self.root / "identity-control", task_id="T2AB-001", executor_run_id="claude-raxt-run", session_id=session_id, executor="claude")
        with self.assertRaises(_native_approval_failure_SessionRecoveryRequired) as raised:
            plane.record_session_started(
                {"version": 1, "executor": "claude", "session_id": session_id, "resumed": True, "persistence": "persistent", "source": "stderr_receipt"},
                expected_task_id="T2AB-001",
                expected_executor_run_id="claude-raxt-run",
                expected_session_id=session_id,
                expected_resumed=False,
                expected_source="preallocated",
            )
        self.assertEqual(raised.exception.code, "session_receipt_run_mismatch")
        self.assertEqual(plane.status().get("status"), "needs_recovery")


class _native_approval_failure_ContainedWorkerLifecycleTests(_native_approval_failure_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _native_approval_failure_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _native_approval_failure_Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def _started_service(self) -> tuple[_native_approval_failure_TaskService, str, str, str]:
        service = _native_approval_failure_TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})
        task = service.create_task("contained worker lifecycle", "claude", [{"id": 1, "description": "wait for approval"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        service.start_task_run(task.id, "claude")
        run_id = "claude-raxt-worker"
        service.record_executor_run_started(task.id, run_id)
        session_id = str(service.get_task(task.id).extensions["agentbc.session"]["session_id"])
        return (service, task.id, run_id, session_id)

    def test_runner_reap_invalidates_native_input_and_clears_worker_refs(self) -> None:
        (service, task_id, executor_run_id, session_id) = self._started_service()
        worker_run_id = "runner-worker-raxt01"
        service.update_execution_metadata(task_id, {"worker_run_id": worker_run_id, "worker_pid": 99999, "dispatch_status": "accepted"})
        service.block_task_for_approval(
            task_id,
            executor_run_id=executor_run_id,
            session_id=session_id,
            request_id="native-raxt-request",
            request_fingerprint="fp-" + "a" * 40,
            executor="claude",
            operation="Bash",
            tool_use_id="toolu-raxt-1",
            action_fingerprint="fp-" + "b" * 40,
            escalation_domain="executor_policy",
            profile_digest="sha256:" + "c" * 64,
            control_path="sdk_control_transport",
            native_event="claude_sdk_can_use_tool",
        )
        runner = _native_approval_failure_RunnerState(self.root / "runner-state", [self.root], {"claude": _native_approval_failure_Path(_native_approval_failure_sys.executable)}, {"claude": "test"})
        runner._reconcile_worker_exit({"run_id": worker_run_id, "task_id": task_id, "board_root": str(self.board), "executor": "claude", "returncode": 1})
        after = service.get_task(task_id)
        self.assertEqual(after.status, "needs_recovery")
        self.assertNotIn("agentbc.input", after.extensions)
        execution = after.extensions.get("agentbc.execution") or {}
        self.assertNotIn("worker_run_id", execution)
        self.assertNotIn("worker_pid", execution)

    def test_contained_worker_does_not_write_global_index_or_board_notifications(self) -> None:
        (service, task_id, _run_id, _session_id) = self._started_service()
        index_before = (self.board / "task_index.jsonl").read_bytes()
        markdown_before = (self.board / "TASK_INDEX.md").read_bytes()
        worker = _native_approval_failure_TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe", "_runner_worker": True})
        worker.mark_task_needs_recovery(task_id, "worker_test_recovery", "test recovery is task-scoped")
        with _native_approval_failure_mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=_native_approval_failure_DeliveryResult(True, "test dialog")):
            _native_approval_failure_notify_terminal(worker, task_id, "task.recovery_required", "warning", "test recovery notification")
        self.assertEqual((self.board / "task_index.jsonl").read_bytes(), index_before)
        self.assertEqual((self.board / "TASK_INDEX.md").read_bytes(), markdown_before)
        self.assertFalse((self.board / "notifications.jsonl").exists())

    def test_contained_worker_start_does_not_refresh_global_indexes(self) -> None:
        service = _native_approval_failure_TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})
        task = service.create_task("contained worker claim", "codex", [{"id": 1, "description": "start without global index writes"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        index_before = (self.board / "task_index.jsonl").read_bytes()
        markdown_before = (self.board / "TASK_INDEX.md").read_bytes()
        worker = _native_approval_failure_TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe", "_runner_worker": True})
        worker.start_task_run(task.id, "codex")
        self.assertEqual((self.board / "task_index.jsonl").read_bytes(), index_before)
        self.assertEqual((self.board / "TASK_INDEX.md").read_bytes(), markdown_before)


class _native_approval_failure_TerminalRecordBudgetProjectionTests(_native_approval_failure_unittest.TestCase):
    def test_high_volume_control_events_preserve_terminal_state_under_budget(self) -> None:
        with _native_approval_failure_tempfile.TemporaryDirectory() as temporary:
            root = _native_approval_failure_Path(temporary).resolve()
            board = root / "record"
            project = root / "project"
            project.mkdir()
            service = _native_approval_failure_TaskService(board, config={"workspace_root": str(root)})
            task = service.create_task("bounded control diagnostics", "codex", [{"id": index, "description": "preserve bounded causal evidence " * 4} for index in range(1, 8)], customer_dir=True, customer_path=project, permission_mode="safe")
            service.start_task_run(task.id, "codex")
            control_events = [
                {
                    "created_at": f"2026-08-31T00:00:{index % 60:02d}Z",
                    "event_type": "approval_requested",
                    "executor": "codex",
                    "executor_run_id": "codex-budget-run",
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "request_id": str(index),
                    "request_fingerprint": "fp-" + "a" * 40,
                    "scope": "single_action",
                    "operation": "command",
                }
                for index in range(7700)
            ]
            control_events.append({"created_at": "2026-08-31T01:00:00Z", "event_type": "transport_failed", "executor": "codex", "executor_run_id": "codex-budget-run", "session_id": "11111111-1111-4111-8111-111111111111", "reason": "approval timed out", "recovery": True})
            service.mark_task_failed(
                task.id,
                "codex_app_server_transport_failed",
                "Approval request timed out without a decision.",
                {"executor": "codex", "result": {"events_seen": 7701, "failure": {"kind": "codex_app_server_transport_failed", "layer": "executor", "message": "Approval request timed out without a decision.", "retryable": True}, "control_events": control_events}},
            )
            failed = service.get_task(task.id)
            self.assertEqual(failed.status, "failed")
            self.assertLessEqual(_native_approval_failure_task_record_size(service.store.task_dir(task.id)), _native_approval_failure_MAX_TASK_RECORD_BYTES)
            projection = failed.errors[-1]["details"]["result"]["control_events"]
            self.assertEqual(projection["events_seen"], 7701)
            self.assertEqual(projection["selected"][0]["event_type"], "approval_requested")
            self.assertEqual(projection["selected"][-1]["event_type"], "transport_failed")


class ClaudeLiveHeartbeatTests(unittest.TestCase):
    def test_live_sdk_poll_refreshes_lease_but_terminal_does_not(self):
        import threading
        from agent_bridge_connect.executors.claude import ClaudeExecutor
        from agent_bridge_connect.claude_sdk_transport import ClaudeSDKControlTransport
        from agent_bridge_connect.adapters import PollResult
        executor = object.__new__(ClaudeExecutor)
        executor._sdk_runs_lock = threading.Lock()
        transport = mock.Mock(spec=ClaudeSDKControlTransport)
        transport.plane = mock.Mock()
        transport.plane.status.return_value = {'status': 'ready'}
        transport.is_alive.return_value = True
        executor._sdk_runs = {'bound-run': {'transport': transport, 'steps_total': 1}}
        with mock.patch.object(executor, '_heartbeat_run') as beat:
            self.assertEqual(executor.poll('bound-run').status, 'running')
            beat.assert_called_once_with('bound-run')
            beat.reset_mock()
            executor._sdk_runs['bound-run']['poll_result'] = PollResult(status='completed')
            self.assertEqual(executor.poll('bound-run').status, 'completed')
            beat.assert_not_called()
