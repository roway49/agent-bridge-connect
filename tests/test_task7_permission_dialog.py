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
from agent_bridge_connect.approval import (
    APPROVAL_EXTENSION_KEY,
    APPROVAL_REASON_DETAIL_LIMIT,
    APPROVAL_REASON_SUMMARY_LIMIT,
    APPROVAL_SCOPE,
    approval_public_projection,
    build_approval_receipt,
    normalize_reason_summary,
    sanitize_reason_detail,
    validate_approval_receipt,
)
from agent_bridge_connect.notifications import (
    build_input_required_notification,
    notify_input_required,
)
from agent_bridge_connect.notifiers.dialog import DialogNotifier
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, task_to_status

TASK_ID = "ABCD-001"
RUN_ID = "claude-ABCD-001-run1"
SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


def _receipt(
    reason_summary: str = "",
    reason_detail: str = "",
) -> dict:
    return build_approval_receipt(
        task_id=TASK_ID,
        executor_run_id=RUN_ID,
        executor="claude",
        session_id=SESSION_ID,
        request_id="approval-request-1",
        request_fingerprint="fp-" + "a" * 40,
        operation="Bash",
        summary="claude needs one-time permission for: Bash",
        reason_summary=reason_summary,
        reason_detail=reason_detail,
    )


class ReasonContractTests(unittest.TestCase):
    def test_legacy_receipt_without_reason_fields_remains_valid(self) -> None:
        # New receipts carry a Core-generated reason_summary but never an empty
        # detail; a hand-built legacy receipt without either field still
        # validates fail-closed against the same strict schema.
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
        summary = normalize_reason_summary(
            "Run the\nrequested\t\ttests\x00 with a very long tail " + "x" * 300,
            executor="claude",
            operation="Bash",
        )
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
        summary = normalize_reason_summary(
            "token: abc123def password=hunter2 credential=secretword "
            "access token: alphakey",
            executor="claude",
            operation="Bash",
        )
        self.assertNotIn("abc123def", summary)
        self.assertNotIn("hunter2", summary)
        self.assertNotIn("secretword", summary)
        self.assertNotIn("alphakey", summary)
        self.assertIn("[REDACTED]", summary)

    def test_reason_detail_is_bounded_to_2000(self) -> None:
        detail = sanitize_reason_detail("x" * (APPROVAL_REASON_DETAIL_LIMIT + 500))
        self.assertEqual(len(detail), APPROVAL_REASON_DETAIL_LIMIT)

    def test_reason_detail_redacts_and_strips_control_characters(self) -> None:
        detail = sanitize_reason_detail(
            "First line\npassword=hunter2 token: abc123\tsecond\x00third "
            "credential=secretword access token: alphakey"
        )
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
        # A private database path embedded mid-string (not just at the start)
        # must never be persisted.
        self.assertEqual(
            sanitize_reason_detail("Reason: inspect /Users/alice/.hermes/state.db"),
            "",
        )
        self.assertEqual(
            sanitize_reason_detail("see ~/private/session.db for the schema"),
            "",
        )

    def test_reason_detail_argv_and_raw_output_fail_closed(self) -> None:
        # Unprocessed argv/command lines and raw output anywhere in the detail
        # are rejected fail-closed even after secret redaction.
        self.assertEqual(
            sanitize_reason_detail("argv: hermes --token secret-value --cwd /private/tmp/x"),
            "",
        )
        self.assertEqual(sanitize_reason_detail("command: ls -la /Users/me"), "")
        self.assertEqual(sanitize_reason_detail("stdout: hello world"), "")
        self.assertEqual(sanitize_reason_detail("raw output: see /tmp/log"), "")

    def test_reason_detail_secret_flag_fails_closed(self) -> None:
        self.assertEqual(sanitize_reason_detail("run with --token secret-value"), "")
        self.assertEqual(sanitize_reason_detail("--password hunter2 passed"), "")

    def test_reason_detail_legitimate_short_text_still_usable(self) -> None:
        detail = sanitize_reason_detail(
            "Requested operation: run the focused unit tests in the workspace"
        )
        self.assertEqual(
            detail,
            "Requested operation: run the focused unit tests in the workspace",
        )
        self.assertLessEqual(len(detail), APPROVAL_REASON_DETAIL_LIMIT)

    def test_validation_rejects_embedded_sensitive_detail_fail_closed(self) -> None:
        # A hand-crafted receipt carrying an embedded private database path or
        # argv line is rejected fail closed before it can be persisted.
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
        receipt = _receipt(
            reason_summary="Run the requested tests",
            reason_detail="The bounded detail",
        )
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
        # Core sanitization redacts assignments; a hand-crafted receipt that
        # still carries one is rejected fail closed before it can be persisted.
        receipt = _receipt(reason_detail="safe")
        receipt["reason_detail"] = "see token: abc123"
        with self.assertRaises(ABCError) as exc:
            validate_approval_receipt(receipt)
        self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_reason_detail_space_separated_credentials_fail_closed(self) -> None:
        # Credential labels followed by a whitespace-separated value (no colon
        # or ``=``) must never be persisted.  Both the Core sanitizer and the
        # strict receipt validator reject them fail closed.
        samples = [
            "token huntertwo",
            "Authorization hunter",
            "password letmein",
            "credential secretword",
            "api key alphakey",
            "Bearer abc123",
            "Authorization abc123",
            "Authorization: Bearer abc123",
            "token abc123",
            "access token abc123",
            "api key abc123",
            "password hunter2",
            "credential abc123",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(
                    sanitize_reason_detail(sample),
                    "",
                    f"sanitize_reason_detail leaked {sample!r}",
                )
                receipt = _receipt(reason_detail="safe")
                receipt["reason_detail"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(
                    exc.exception.code,
                    "approval_sensitive_field",
                    f"validate_approval_receipt leaked {sample!r}",
                )

    def test_reason_detail_case_variants_fail_closed(self) -> None:
        samples = [
            "TOKEN huntertwo",
            "BEARER hunter",
            "Access Token alphakey",
            "API Key secretword",
            "Password LETMEIN",
            "PASSWD passphrase",
            "Credential secretword",
            "AUTHORIZATION hunter",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(
                    sanitize_reason_detail(sample),
                    "",
                    f"case variant leaked {sample!r}",
                )
                receipt = _receipt(reason_detail="safe")
                receipt["reason_detail"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_reason_detail_embedded_credentials_fail_closed(self) -> None:
        # A credential embedded in otherwise-innocent prose is still rejected.
        samples = [
            "The access token huntertwo is required",
            "Use token letmein for this action",
            "mixed text Bearer hunter end",
            "pass the password secretword please",
            "see credential alphakey, then proceed",
            "The api key hunter is used here",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(
                    sanitize_reason_detail(sample),
                    "",
                    f"embedded credential leaked {sample!r}",
                )

    def test_reason_detail_prose_does_not_false_positive(self) -> None:
        # Unrelated substrings and ordinary prose that merely mentions a label
        # must remain usable, so legitimate short details are never dropped.
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
                self.assertNotEqual(
                    sanitize_reason_detail(sample),
                    "",
                    f"legitimate prose was dropped: {sample!r}",
                )
                self.assertLessEqual(
                    len(sanitize_reason_detail(sample)),
                    APPROVAL_REASON_DETAIL_LIMIT,
                )

    def test_reason_detail_bearer_jwt_fragment_fails_closed(self) -> None:
        # A Bearer auth payload is always a credential even when the fragment is
        # a short alphanumeric-only opaque string (e.g. a JWT header).
        for sample in ("Bearer eyJhbGciOi", "Authorization: Bearer eyJhbGciOi"):
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_reason_detail(sample), "")
                receipt = _receipt(reason_detail="safe")
                receipt["reason_detail"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(exc.exception.code, "approval_sensitive_field")

    def test_build_approval_receipt_never_persists_credential(self) -> None:
        # Even when a caller passes a credential directly to
        # ``build_approval_receipt``, the resulting receipt must not carry the
        # real value anywhere -- the detail is dropped fail closed and the
        # summary redacts it.
        for sample, credential in (
            ("The access token huntertwo is required", "huntertwo"),
            ("Bearer hunter", "hunter"),
            ("api key alphakey", "alphakey"),
            ("password letmein", "letmein"),
            ("credential secretword", "secretword"),
        ):
            with self.subTest(sample=sample):
                receipt = build_approval_receipt(
                    task_id=TASK_ID,
                    executor_run_id=RUN_ID,
                    executor="claude",
                    session_id=SESSION_ID,
                    request_id="approval-request-1",
                    request_fingerprint="fp-" + "a" * 40,
                    operation="Bash",
                    summary="claude needs one-time permission for: Bash",
                    reason_detail=sample,
                )
                serialized = json.dumps(receipt)
                self.assertNotIn(credential, serialized)
                self.assertNotIn("reason_detail", receipt)

    def test_reason_summary_space_separated_credentials_redacted(self) -> None:
        # The single-line summary Core persists must also never expose a real
        # whitespace-separated credential value.
        for sample, credential in (
            ("token huntertwo", "huntertwo"),
            ("Authorization hunter", "hunter"),
            ("The access token alphakey is required", "alphakey"),
            ("api key secretword", "secretword"),
            ("password letmein", "letmein"),
            ("credential passphrase", "passphrase"),
        ):
            with self.subTest(sample=sample):
                summary = normalize_reason_summary(
                    sample,
                    executor="claude",
                    operation="Bash",
                )
                self.assertNotIn(credential, summary)
                self.assertIn("[REDACTED]", summary)

    def test_build_receipt_redacts_summary_and_detail_credentials(self) -> None:
        for sample, credential in (
            ("token huntertwo", "huntertwo"),
            ("Authorization hunter", "hunter"),
            ("password letmein", "letmein"),
            ("credential secretword", "secretword"),
            ("api key alphakey", "alphakey"),
        ):
            with self.subTest(sample=sample):
                receipt = build_approval_receipt(
                    task_id=TASK_ID,
                    executor_run_id=RUN_ID,
                    executor="claude",
                    session_id=SESSION_ID,
                    request_id="approval-request-1",
                    request_fingerprint="fp-" + "a" * 40,
                    operation="Bash",
                    summary="claude needs one-time permission for: Bash",
                    reason_summary=sample,
                    reason_detail=sample,
                )
                serialized = json.dumps(receipt)
                self.assertNotIn(credential, serialized)
                self.assertNotIn("reason_detail", receipt)
                self.assertIn("[REDACTED]", receipt["reason_summary"])

    def test_validation_rejects_credential_in_manual_summary(self) -> None:
        # A hand-crafted receipt carrying a credential value in the persisted
        # single-line summary is rejected fail closed before it can survive.
        for sample in (
            "token huntertwo",
            "Authorization hunter",
            "The access token alphakey is required",
            "api key secretword",
            "password letmein",
            "credential passphrase",
        ):
            with self.subTest(sample=sample):
                receipt = _receipt(reason_summary="safe")
                receipt["reason_summary"] = sample
                with self.assertRaises(ABCError) as exc:
                    validate_approval_receipt(receipt)
                self.assertEqual(
                    exc.exception.code,
                    "approval_sensitive_field",
                    f"manual summary leaked {sample!r}",
                )


class PublicProjectionTests(unittest.TestCase):
    def test_approval_public_projection_exposes_only_summary(self) -> None:
        receipt = _receipt(
            reason_summary="Run the requested tests",
            reason_detail="A bounded detail that must never be projected",
        )
        projection = approval_public_projection(receipt)
        self.assertNotIn("reason_detail", projection)
        self.assertNotIn("request_id", projection)
        self.assertNotIn("session_id", projection)
        self.assertEqual(projection["summary"], receipt["summary"])
        self.assertEqual(
            projection.get("reason_summary"),
            "Run the requested tests",
        )


class PermissionDialogTests(unittest.TestCase):
    def test_view_details_button_only_with_detail(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                }
            )
        script = run.call_args.kwargs["input"]
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', script)
        self.assertIn('default button "Deny"', script)
        self.assertNotIn('"Later"', script)
        self.assertNotIn('default answer', script)
        self.assertNotIn('"Submit"', script)

        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "input_type": "permission",
                }
            )
        self.assertIn('buttons {"Deny", "Approve"}', run.call_args.kwargs["input"])
        self.assertNotIn("View Details", run.call_args.kwargs["input"])

    def test_input_action_maps_view_details_as_non_decision(self) -> None:
        self.assertEqual(
            DialogNotifier._input_action("View Details", "permission", False),
            "view_details",
        )
        self.assertEqual(
            DialogNotifier._input_action("Approve", "permission", False),
            "approve",
        )
        self.assertEqual(
            DialogNotifier._input_action("Deny", "permission", False),
            "deny",
        )
        self.assertEqual(
            DialogNotifier._input_action("unknown", "permission", False),
            "deny",
        )
        # Gave up (timeout) always auto-denies, never approve.
        self.assertEqual(
            DialogNotifier._input_action("Approve", "permission", True),
            "deny",
        )

    def test_view_details_back_returns_to_decision_without_responding(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""),
                mock.Mock(returncode=0, stdout="button returned:Back, gave up:false", stderr=""),
                mock.Mock(returncode=0, stdout="button returned:Approve, gave up:false", stderr=""),
            ]
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                    "deadline_at": "2099-01-01T00:00:00Z",
                }
            )
        self.assertEqual(result.details, {"action": "approve", "decision_source": "user"})
        # The main view shows the short summary; the detail view shows the detail.
        self.assertEqual(run.call_args_list[0].args[0][3], "short summary")
        self.assertEqual(run.call_args_list[1].args[0][3], "The bounded detail")
        self.assertEqual(run.call_args_list[2].args[0][3], "short summary")
        self.assertEqual(len(run.call_args_list), 3)

    def test_close_detail_view_auto_denies_with_dialog_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""),
                mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)"),
            ]
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                }
            )
        self.assertEqual(
            result.details,
            {"action": "deny", "decision_source": "dialog_closed"},
        )

    def test_close_decision_view_auto_denies_with_dialog_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr="execution error: User canceled. (-128)",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                }
            )
        self.assertEqual(
            result.details,
            {"action": "deny", "decision_source": "dialog_closed"},
        )

    def test_timeout_auto_denies_with_source_timeout(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:true",
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                }
            )
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})

    def test_detail_timeout_auto_denies_with_source_timeout(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""),
                mock.Mock(returncode=0, stdout="button returned:Back, gave up:true", stderr=""),
            ]
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                }
            )
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})

    def test_expired_deadline_does_not_start_approvable_dialog(self) -> None:
        notifier = DialogNotifier()
        expired = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(milliseconds=5)
        ).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Approve, gave up:false",
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "input_type": "permission",
                    "deadline_at": expired,
                }
            )
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        # A mock approving at an expired deadline must never be reached.
        run.assert_not_called()

    def test_critical_sub_second_deadline_cannot_cross_deadline(self) -> None:
        # A deadline less than one full second away (e.g. 0.9s) must not force
        # a 1-second dialog: the dialog countdown is 0 and the request fails
        # closed immediately.
        notifier = DialogNotifier()
        critical = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(milliseconds=900)
        ).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Approve, gave up:false",
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "input_type": "permission",
                    "deadline_at": critical,
                }
            )
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        run.assert_not_called()

    def test_view_details_loop_keeps_absolute_deadline(self) -> None:
        # The View Details/Back loop re-computes the countdown from the same
        # absolute deadline each iteration; once the deadline is reached while
        # returning to the decision view the dialog fails closed and never
        # approves.
        notifier = DialogNotifier()
        far_future = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        ).isoformat().replace("+00:00", "Z")
        with mock.patch.object(
            DialogNotifier, "_permission_give_up_seconds"
        ) as countdown:
            countdown.side_effect = [5, 5, 0]
            with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
                run.side_effect = [
                    mock.Mock(returncode=0, stdout="button returned:View Details, gave up:false", stderr=""),
                    mock.Mock(returncode=0, stdout="button returned:Back, gave up:false", stderr=""),
                    # The loop returns to the decision view and re-checks the
                    # deadline before showing it again.
                ]
                result = notifier.send(
                    {
                        "event_type": "task.input_required",
                        "message": "short summary",
                        "reason_summary": "short summary",
                        "reason_detail": "The bounded detail",
                        "input_type": "permission",
                        "deadline_at": far_future,
                    }
                )
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        self.assertEqual(len(run.call_args_list), 2)

    def test_unknown_button_auto_denies_with_fail_closed_source(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Mystery, gave up:false",
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                }
            )
        self.assertEqual(
            result.details,
            {"action": "deny", "decision_source": "fail_closed"},
        )

    def test_deadline_bounds_dialog_countdown_and_never_resets(self) -> None:
        notifier = DialogNotifier()
        # An expired absolute deadline must fail closed before any decision view
        # is shown: no osascript runs and the result is a timeout deny.
        past = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                    "deadline_at": past,
                }
            )
        self.assertEqual(result.details, {"action": "deny", "decision_source": "timeout"})
        run.assert_not_called()
        # The countdown is bounded by the input timeout, never extended past it.
        far_future = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        ).isoformat().replace("+00:00", "Z")
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Deny, gave up:false",
                stderr="",
            )
            notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "short summary",
                    "reason_summary": "short summary",
                    "reason_detail": "The bounded detail",
                    "input_type": "permission",
                    "deadline_at": far_future,
                }
            )
        self.assertIn("giving up after 300", run.call_args.kwargs["input"])


class ApprovalFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        self.task_id = self._started_task()

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
        return str(
            (self.service.get_task(self.task_id).extensions or {})["agentbc.session"]["session_id"]
        )

    def _block_approval(self, *, reason: str = "", reason_detail: str = "") -> None:
        self.service.block_task_for_approval(
            self.task_id,
            executor_run_id=RUN_ID,
            session_id=self._session_id(),
            request_id="approval-request-task7",
            request_fingerprint="fp-" + "t" * 40,
            executor="claude",
            operation="Bash",
            summary="claude needs one-time permission for: Bash",
            reason=reason,
            reason_detail=reason_detail,
        )

    def test_block_approval_persists_reason_summary_and_bounded_detail(self) -> None:
        self._block_approval(
            reason="Run the requested tests\nwith token: abc123",
            reason_detail="A bounded detail " * 300,
        )
        task = self.service.get_task(self.task_id)
        receipt = task.extensions[APPROVAL_EXTENSION_KEY]
        self.assertLessEqual(len(receipt["reason_summary"]), APPROVAL_REASON_SUMMARY_LIMIT)
        self.assertNotIn("\n", receipt["reason_summary"])
        self.assertNotIn("abc123", json.dumps(receipt))
        self.assertLessEqual(len(receipt["reason_detail"]), APPROVAL_REASON_DETAIL_LIMIT)
        # The input request carries only the short summary; the detail stays in
        # the durable approval receipt so public projections of ``agentbc.input``
        # never expose it.
        request = task.extensions["agentbc.input"]
        self.assertEqual(request["reason_summary"], receipt["reason_summary"])
        self.assertNotIn("reason_detail", request)

    def test_notification_message_and_projection_expose_only_summary(self) -> None:
        self._block_approval(
            reason="Run the requested tests",
            reason_detail="A very private bounded detail",
        )
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
        self._block_approval(
            reason="Run the requested tests",
            reason_detail="The bounded detail",
        )
        before = self.service.get_task(self.task_id)
        deadline_before = before.extensions["agentbc.input"]["deadline_at"]
        request = before.extensions["agentbc.input"]

        def respond(input_id: str, action: str, message: str) -> dict:
            return self.service.respond_to_input(
                self.task_id,
                input_id,
                response_type=action,
                message=message,
            )

        responder = mock.Mock(side_effect=respond)
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(
                True,
                "dialog shown",
                details={"action": "deny", "decision_source": "user"},
            ),
        ):
            result = notify_input_required(
                self.service,
                self.task_id,
                responder=responder,
            )
        # View Details/Back is non-decision: the responder fires exactly once for
        # the final decision and resolves to the exact same request.
        responder.assert_called_once_with(request["input_id"], "deny", "")
        self.assertEqual(result["response"]["request_id"], request["request_id"])
        after = self.service.get_task(self.task_id)
        self.assertEqual(
            after.extensions["agentbc.input"]["deadline_at"],
            deadline_before,
        )
        self.assertEqual(
            after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["type"],
            "deny",
        )
        self.assertEqual(
            after.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"],
            "user",
        )

    def test_respond_after_viewing_detail_binds_exact_request(self) -> None:
        self._block_approval(
            reason="Run the requested tests",
            reason_detail="The bounded detail",
        )
        request = self.service.get_task(self.task_id).extensions["agentbc.input"]
        result = self.service.respond_to_input(
            self.task_id,
            request["input_id"],
            response_type="approve",
        )
        self.assertEqual(result["approval_decision"], "approve")
        self.assertEqual(result["request_id"], "approval-request-task7")
        self.assertEqual(
            self.service.get_task(self.task_id).extensions[APPROVAL_EXTENSION_KEY]["request_id"],
            "approval-request-task7",
        )


if __name__ == "__main__":
    unittest.main()
