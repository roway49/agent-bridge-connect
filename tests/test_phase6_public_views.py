"""Phase 6 public permission-grant projection views.

Proves status, preflight, report JSON/Markdown, the public task/extensions
views and the input notification all consume the same sanitized
``permission_grant_public_projection`` for issued, consumed and revoked
grants, and that malformed or future-version grants fail closed without
leaking binding identifiers or sensitive fields.  The base
``agentbc.permission`` record remains authoritative and visible separately.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.cli import _print_execution_policy, _print_task_status
from agent_bridge_connect.execution_policy import (
    execution_policy_view,
    public_extensions_view,
    public_task_view,
)
from agent_bridge_connect.notifications import build_input_required_notification
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
    consume_permission_grant,
    permission_grant_public_projection,
    revoke_permission_grant,
)
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.reports import (
    generate_report,
    generate_report_md,
    generate_task_brief,
)
from agent_bridge_connect.service import TaskService, task_to_status

ISSUED_AT = "2026-08-12T00:00:00Z"
CONSUMED_AT = "2026-08-12T00:01:00Z"
REVOKED_AT = "2026-08-12T00:02:00Z"

GRANT_ID = "grant-public-secret-9"
SOURCE_RUN_ID = "codex-source-run-77"
TARGET_RUN_ID = "codex-target-run-88"
BINDING_SESSION_ID = "session-binding-secret-99"
BINDING_INPUT_ID = "input-binding-secret-66"


def _issued_grant(task_id: str) -> dict:
    return build_permission_grant(
        executor="codex",
        task_id=task_id,
        input_id=BINDING_INPUT_ID,
        session_id=BINDING_SESSION_ID,
        source_run_id=SOURCE_RUN_ID,
        grant_id=GRANT_ID,
        issued_at=ISSUED_AT,
    )


class PermissionPublicViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace")},
        )
        self.task = self.service.create_task(
            "public permission views",
            "codex",
            [{"id": 1, "description": "render public permission views"}],
            customer_dir=False,
        )

    def _store_grant(self, grant: dict) -> None:
        raw = self.service.store.read_task(self.task.id)
        raw["extensions"][PERMISSION_GRANT_EXTENSION_KEY] = grant
        self.service.store.write_task(self.task.id, raw)

    def _grant_identifiers(self) -> tuple[str, ...]:
        return (
            GRANT_ID,
            SOURCE_RUN_ID,
            TARGET_RUN_ID,
            BINDING_SESSION_ID,
            BINDING_INPUT_ID,
            "grant_id",
            "target_run_id",
            "source_run_id",
        )

    def _assert_no_grant_identifiers(self, serialized: str) -> None:
        for identifier in self._grant_identifiers():
            self.assertNotIn(identifier, serialized)

    def test_status_preflight_and_report_share_the_issued_projection(self) -> None:
        grant = _issued_grant(self.task.id)
        self._store_grant(grant)
        expected = permission_grant_public_projection(grant)

        status = task_to_status(self.service.get_task(self.task.id))
        self.assertEqual(status["execution_policy"]["permission_grant"], expected)
        self.assertEqual(
            status["extensions"][PERMISSION_GRANT_EXTENSION_KEY],
            expected,
        )
        self._assert_no_grant_identifiers(json.dumps(status))
        # The authoritative base permission record stays visible separately.
        self.assertIn(PERMISSION_EXTENSION_KEY, status["extensions"])
        self.assertEqual(
            status["extensions"][PERMISSION_EXTENSION_KEY]["effective_mode"],
            "safe",
        )

        preflight = self.service.preflight(self.task.id)
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.execution_policy["permission_grant"], expected)

        report = generate_report(self.task.id, self.board)
        self.assertEqual(report["execution_policy"]["permission_grant"], expected)
        brief = generate_task_brief(self.task.id, self.board)
        self.assertEqual(
            brief["evidence"]["execution_policy"]["permission_grant"],
            expected,
        )
        for rendered in (json.dumps(report), json.dumps(brief)):
            self._assert_no_grant_identifiers(rendered)

        markdown = generate_report_md(self.task.id, self.board)
        self.assertIn("- Permission grant: `issued`", markdown)
        self.assertIn("- Permission grant active: `yes`", markdown)
        self.assertIn(
            "- Permission grant transition: `safe` -> `full`",
            markdown,
        )
        self.assertIn(
            "- Permission grant scope: `next_executor_run` with `1` use(s)",
            markdown,
        )
        self.assertIn("- Permission grant reason: `none`", markdown)
        self._assert_no_grant_identifiers(markdown)

    def test_public_task_and_extensions_views_replace_the_raw_envelope(self) -> None:
        grant = _issued_grant(self.task.id)
        self._store_grant(grant)
        expected = permission_grant_public_projection(grant)
        model = self.service.get_task(self.task.id)

        public_extensions = public_extensions_view(model.extensions)
        self.assertEqual(
            public_extensions[PERMISSION_GRANT_EXTENSION_KEY],
            expected,
        )
        self.assertNotIn("grant_id", public_extensions[PERMISSION_GRANT_EXTENSION_KEY])

        public_task = public_task_view(model.to_dict())
        self.assertEqual(
            public_task["extensions"][PERMISSION_GRANT_EXTENSION_KEY],
            expected,
        )
        self.assertEqual(public_task["execution_policy"]["permission_grant"], expected)

    def test_consumed_and_revoked_grants_flow_through_every_public_view(self) -> None:
        consumed = consume_permission_grant(
            _issued_grant(self.task.id),
            TARGET_RUN_ID,
            consumed_at=CONSUMED_AT,
        )
        self._store_grant(consumed)
        expected_consumed = permission_grant_public_projection(consumed)
        self.assertEqual(expected_consumed["state"], "consumed")
        self.assertFalse(expected_consumed["active"])
        self.assertEqual(expected_consumed["uses"], 1)
        self.assertEqual(expected_consumed["reason_code"], "")

        status = task_to_status(self.service.get_task(self.task.id))
        preflight = self.service.preflight(self.task.id)
        report = generate_report(self.task.id, self.board)
        for view in (
            status["execution_policy"]["permission_grant"],
            status["extensions"][PERMISSION_GRANT_EXTENSION_KEY],
            preflight.execution_policy["permission_grant"],
            report["execution_policy"]["permission_grant"],
        ):
            self.assertEqual(view, expected_consumed)
        for rendered in (
            json.dumps(status),
            json.dumps(report),
            generate_report_md(self.task.id, self.board),
        ):
            self._assert_no_grant_identifiers(rendered)

        revoked = revoke_permission_grant(
            consumed,
            "task_terminal",
            revoked_at=REVOKED_AT,
        )
        self._store_grant(revoked)
        expected_revoked = permission_grant_public_projection(revoked)
        self.assertEqual(expected_revoked["state"], "revoked")
        self.assertEqual(expected_revoked["reason_code"], "task_terminal")

        status = task_to_status(self.service.get_task(self.task.id))
        preflight = self.service.preflight(self.task.id)
        report = generate_report(self.task.id, self.board)
        markdown = generate_report_md(self.task.id, self.board)
        for view in (
            status["execution_policy"]["permission_grant"],
            status["extensions"][PERMISSION_GRANT_EXTENSION_KEY],
            preflight.execution_policy["permission_grant"],
            report["execution_policy"]["permission_grant"],
        ):
            self.assertEqual(view, expected_revoked)
        self.assertIn("- Permission grant: `revoked`", markdown)
        self.assertIn("- Permission grant active: `no`", markdown)
        self.assertIn("- Permission grant reason: `task_terminal`", markdown)
        for rendered in (json.dumps(status), json.dumps(report), markdown):
            self._assert_no_grant_identifiers(rendered)

    def test_malformed_and_future_version_grants_fail_closed_everywhere(self) -> None:
        future = _issued_grant(self.task.id)
        future["version"] = 2
        malformed = {"version": 1, "grant_id": GRANT_ID}
        tampered = _issued_grant(self.task.id)
        tampered["command"] = "git push --force"
        tampered["prompt"] = "customer secret text"
        for label, grant in (
            ("future_version", future),
            ("malformed", malformed),
            ("tampered_sensitive", tampered),
        ):
            with self.subTest(grant=label):
                self._store_grant(grant)
                status = task_to_status(self.service.get_task(self.task.id))
                self.assertIsNone(
                    status["execution_policy"]["permission_grant"],
                    label,
                )
                self.assertNotIn(
                    PERMISSION_GRANT_EXTENSION_KEY,
                    status["extensions"],
                    label,
                )
                encoded = json.dumps(status)
                self._assert_no_grant_identifiers(encoded)
                self.assertNotIn("git push --force", encoded)
                self.assertNotIn("customer secret text", encoded)

                preflight = self.service.preflight(self.task.id)
                self.assertTrue(preflight.ok, label)
                self.assertIsNone(
                    preflight.execution_policy["permission_grant"],
                    label,
                )

                report = generate_report(self.task.id, self.board)
                self.assertIsNone(
                    report["execution_policy"]["permission_grant"],
                    label,
                )
                markdown = generate_report_md(self.task.id, self.board)
                self.assertNotIn("Permission grant:", markdown)
                self._assert_no_grant_identifiers(markdown)

    def test_cli_status_and_policy_text_render_the_shared_projection(self) -> None:
        grant = _issued_grant(self.task.id)
        self._store_grant(grant)
        expected = permission_grant_public_projection(grant)
        status = task_to_status(self.service.get_task(self.task.id))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_task_status(status)
        rendered = output.getvalue()
        self.assertIn(
            "Permission grant: state=issued active=yes single_use=yes",
            rendered,
        )
        self.assertIn(
            "transition=safe->full scope=next_executor_run uses=0 reason=-",
            rendered,
        )
        self._assert_no_grant_identifiers(rendered)

        policy = execution_policy_view(self.service.get_task(self.task.id).extensions)
        self.assertEqual(policy["permission_grant"], expected)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_execution_policy(policy)
        self.assertIn("Permission grant: state=issued", output.getvalue())
        self._assert_no_grant_identifiers(output.getvalue())


class PermissionNotificationViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace")},
        )
        self.task = self.service.create_task(
            "permission notification",
            "codex",
            [{"id": 1, "description": "ask for permission"}],
            customer_dir=False,
        )

    def _make_waiting_permission(self, grant: dict | None) -> None:
        raw = self.service.store.read_task(self.task.id)
        raw["status"] = "input_required"
        raw["extensions"]["agentbc.input"] = {
            "input_id": "input-notification-secret-5",
            "executor_run_id": SOURCE_RUN_ID,
            "blocked_step_id": 1,
            "type": "permission",
            "summary": "The next continuation requires full executor permission",
            "requested_permission": "full",
            "reason": "Full access is required for the next continuation",
            "created_at": ISSUED_AT,
            "deadline_at": "2099-01-01T00:00:00Z",
            "status": "waiting",
        }
        if grant is not None:
            raw["extensions"][PERMISSION_GRANT_EXTENSION_KEY] = grant
        self.service.store.write_task(self.task.id, raw)

    def test_permission_notification_uses_projection_and_contract_dialog_text(self) -> None:
        grant = _issued_grant(self.task.id)
        self._make_waiting_permission(grant)
        notification = build_input_required_notification(self.service, self.task.id)

        self.assertEqual(
            notification["permission_grant"],
            permission_grant_public_projection(grant),
        )
        self.assertNotIn("grant_id", json.dumps(notification))
        self._assert_no_grant_identifiers(json.dumps(notification))
        self.assertEqual(notification["input_type"], "permission")
        self.assertEqual(
            notification["respond_command"],
            f"agentbc task respond {self.task.id} --input input-notification-secret-5 "
            "--approve (or --deny)",
        )
        message = notification["message"]
        self.assertIn("Requested access:", message)
        self.assertIn(
            "Approve grants the corresponding Executor its complete full permission "
            "for exactly the next continuation of this same task/session.",
            message,
        )
        self.assertIn(
            "The technical scope is not limited to Git or the blocked command.",
            message,
        )
        self.assertIn("The grant is single-use.", message)
        self.assertIn("Deny terminates the task as failed.", message)

    def test_permission_notification_fails_closed_on_invalid_grant(self) -> None:
        future = _issued_grant(self.task.id)
        future["version"] = 2
        self._make_waiting_permission(future)
        notification = build_input_required_notification(self.service, self.task.id)

        self.assertIsNone(notification["permission_grant"])
        self._assert_no_grant_identifiers(json.dumps(notification))
        self.assertIn("Deny terminates the task as failed.", notification["message"])

    def test_waiting_permission_without_grant_still_carries_none_projection(self) -> None:
        self._make_waiting_permission(None)
        notification = build_input_required_notification(self.service, self.task.id)
        self.assertIsNone(notification["permission_grant"])
        self.assertIn(
            "Approve grants the corresponding Executor its complete full permission "
            "for exactly the next continuation of this same task/session.",
            notification["message"],
        )

    def _assert_no_grant_identifiers(self, serialized: str) -> None:
        for identifier in (
            GRANT_ID,
            SOURCE_RUN_ID,
            TARGET_RUN_ID,
            BINDING_SESSION_ID,
            BINDING_INPUT_ID,
        ):
            self.assertNotIn(identifier, serialized)


if __name__ == "__main__":
    unittest.main()
