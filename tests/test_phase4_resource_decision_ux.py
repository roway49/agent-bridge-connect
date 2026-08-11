"""Phase 4 regression: resource-decision UX (dialog, notification, views, docs).

Covers the 1.0.2A CFG-002 UX slice:
- kind=resource_limit + response_protocol=approve_deny choice dialogs map the
  first button (提高预算并继续) to approve and the second (终止任务) to deny;
  Later / close / timeout stay dismissed (task keeps waiting);
- the fallback respond command exposes --approve / --deny;
- ordinary choices keep submitting options as --message with unchanged semantics;
- public execution policy views keep ``limit`` and add ``configured_limit``,
  ``exhaustion_count`` and ``last_decision``, rendered consistently by
  status/preflight/report without internal paths;
- the three Phase 4 documents describe only the landed CFG-002 slice and keep
  SESSION-001 cleanup / purge / delete open.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.cli import _print_execution_policy, _print_task_status
from agent_bridge_connect.execution_policy import (
    RESOURCE_EXTENSION_KEY,
    build_resource_snapshot,
    execution_policy_view,
)
from agent_bridge_connect.notifications import (
    RESOURCE_DECISION_APPROVE_LABEL,
    RESOURCE_DECISION_DENY_LABEL,
    build_input_required_notification,
    notify_input_required,
)
from agent_bridge_connect.notifiers.dialog import DialogNotifier
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, task_to_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resource_decision_callback(task_id: str, **updates) -> dict:
    callback = {
        "version": 1,
        "task_id": task_id,
        "final_state": "input_required",
        "summary": "Iteration budget exhausted; needs a decision",
        "input": {
            "type": "choice",
            "kind": "resource_limit",
            "response_protocol": "approve_deny",
            "reason": "已使用 60 / 上限 60",
            "options": [
                {"label": RESOURCE_DECISION_APPROVE_LABEL, "description": "Double this task resource and continue."},
                {"label": RESOURCE_DECISION_DENY_LABEL, "description": "Terminate the task as failed."},
            ],
        },
        "step_results": [{"id": 1, "status": "blocked"}],
    }
    callback.update(updates)
    return callback


class ResourceDecisionDialogTests(unittest.TestCase):
    def test_input_action_maps_resource_decision_buttons(self) -> None:
        options = (RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL)
        for button, expected in (
            (RESOURCE_DECISION_APPROVE_LABEL, "approve"),
            (RESOURCE_DECISION_DENY_LABEL, "deny"),
            ("Later", "dismissed"),
            ("unknown", "dismissed"),
        ):
            with self.subTest(button=button):
                self.assertEqual(
                    DialogNotifier._input_action(
                        button,
                        "choice",
                        False,
                        options,
                        input_kind="resource_limit",
                        response_protocol="approve_deny",
                    ),
                    expected,
                )
        self.assertEqual(
            DialogNotifier._input_action(
                RESOURCE_DECISION_APPROVE_LABEL,
                "choice",
                True,
                options,
                input_kind="resource_limit",
                response_protocol="approve_deny",
            ),
            "dismissed",
        )

    def test_input_action_keeps_ordinary_choice_semantics(self) -> None:
        options = ("Option A", "Option B")
        self.assertEqual(
            DialogNotifier._input_action("Option B", "choice", False, options),
            "message",
        )
        self.assertEqual(
            DialogNotifier._input_action("Later", "choice", False, options),
            "dismissed",
        )
        # Resource-decision metadata applies positional approve/deny mapping.
        self.assertEqual(
            DialogNotifier._input_action(
                "Option A",
                "choice",
                False,
                options,
                input_kind="resource_limit",
                response_protocol="approve_deny",
            ),
            "approve",
        )
        self.assertEqual(
            DialogNotifier._input_action(
                "Option B",
                "choice",
                False,
                options,
                input_kind="resource_limit",
                response_protocol="approve_deny",
            ),
            "deny",
        )
        self.assertEqual(
            DialogNotifier._input_action("Deny", "permission", False),
            "deny",
        )

    def test_send_renders_resource_decision_buttons_and_maps_approve(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout=(
                    f"button returned:{RESOURCE_DECISION_APPROVE_LABEL}, gave up:false"
                ),
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "Decide",
                    "input_type": "choice",
                    "input_kind": "resource_limit",
                    "response_protocol": "approve_deny",
                    "input_options": [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL],
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "approve"})
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/osascript",
                "-",
                "Agent-Bridge-Connect",
                "Decide",
                RESOURCE_DECISION_APPROVE_LABEL,
                RESOURCE_DECISION_DENY_LABEL,
            ],
        )
        self.assertIn(
            'buttons {"Later", (item 3 of argv), (item 4 of argv)}',
            run.call_args.kwargs["input"],
        )
        self.assertIn('default button "Later"', run.call_args.kwargs["input"])

    def test_send_later_keeps_task_waiting(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Later, gave up:false",
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "Decide",
                    "input_type": "choice",
                    "input_kind": "resource_limit",
                    "response_protocol": "approve_deny",
                    "input_options": [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL],
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "dismissed"})

    def test_send_timeout_keeps_task_waiting(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout=(
                    f"button returned:{RESOURCE_DECISION_DENY_LABEL}, gave up:true"
                ),
                stderr="",
            )
            result = notifier.send(
                {
                    "event_type": "task.input_required",
                    "message": "Decide",
                    "input_type": "choice",
                    "input_kind": "resource_limit",
                    "response_protocol": "approve_deny",
                    "input_options": [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL],
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "dismissed"})


class ResourceDecisionNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.board = self.base / "record"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.base / "workspace")},
        )
        self.task = self.service.create_task(
            "resource decision",
            "shell",
            [{"id": 1, "description": "run with budget"}],
            customer_dir=False,
        )
        self.assertTrue(
            self.service.finalize_task_from_agent(
                self.task.id,
                _resource_decision_callback(self.task.id),
            )
        )

    def _request(self) -> dict:
        return dict(self.service.get_task(self.task.id).extensions["agentbc.input"])

    def test_request_persists_kind_and_response_protocol(self) -> None:
        request = self._request()
        self.assertEqual(request["type"], "choice")
        self.assertEqual(request["kind"], "resource_limit")
        self.assertEqual(request["response_protocol"], "approve_deny")
        self.assertEqual(request["options"], [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL])
        self.assertEqual(len(request["option_descriptions"]), 2)

    def test_notification_uses_canonical_buttons_and_approve_deny_fallback(self) -> None:
        notification = build_input_required_notification(self.service, self.task.id)
        request = self._request()
        self.assertEqual(notification["input_type"], "choice")
        self.assertEqual(notification["input_kind"], "resource_limit")
        self.assertEqual(notification["response_protocol"], "approve_deny")
        self.assertEqual(
            notification["input_options"],
            [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL],
        )
        self.assertEqual(
            notification["respond_command"],
            f"agentbc task respond {self.task.id} --input {request['input_id']} --approve (or --deny)",
        )
        self.assertIn(RESOURCE_DECISION_APPROVE_LABEL, notification["message"])
        self.assertIn(RESOURCE_DECISION_DENY_LABEL, notification["message"])
        self.assertNotIn("Deadline:", notification["message"])
        self.assertNotIn("Respond:", notification["message"])
        self.assertNotIn("hidden", json.dumps(notification, ensure_ascii=False))

    def test_dialog_approve_responds_and_resumes_same_task(self) -> None:
        request = self._request()
        responder = mock.Mock(
            return_value={
                "task_id": self.task.id,
                "input_id": request["input_id"],
                "status": "running",
                "same_task": True,
            }
        )
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(
                True,
                "shown",
                details={"action": "approve"},
            ),
        ):
            result = notify_input_required(
                self.service,
                self.task.id,
                responder=responder,
            )
        responder.assert_called_once_with(request["input_id"], "approve", "")
        self.assertEqual(result["response"]["status"], "running")
        events = self.service.store.read_events(self.task.id)
        self.assertEqual(events[-1]["response_type"], "approve")

    def test_dismissed_dialog_does_not_respond_and_task_stays_waiting(self) -> None:
        responder = mock.Mock()
        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=DeliveryResult(
                True,
                "shown",
                details={"action": "dismissed"},
            ),
        ):
            result = notify_input_required(
                self.service,
                self.task.id,
                responder=responder,
            )
        responder.assert_not_called()
        self.assertEqual(result["response"], {})
        self.assertEqual(self.service.get_task(self.task.id).status, "input_required")

    def test_ordinary_choice_keeps_message_fallback(self) -> None:
        choice_task = self.service.create_task(
            "ordinary choice",
            "shell",
            [{"id": 1, "description": "choose"}],
            customer_dir=False,
        )
        callback = {
            "version": 1,
            "task_id": choice_task.id,
            "final_state": "input_required",
            "summary": "Choose an option",
            "input": {
                "type": "choice",
                "reason": "The output format must be selected.",
                "options": [
                    {"label": "Option A", "description": "Compact text result."},
                    {"label": "Option B", "description": "Detailed JSON result."},
                ],
            },
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        self.assertTrue(
            self.service.finalize_task_from_agent(choice_task.id, callback)
        )
        request = self.service.get_task(choice_task.id).extensions["agentbc.input"]
        self.assertNotIn("kind", request)
        self.assertNotIn("response_protocol", request)
        notification = build_input_required_notification(self.service, choice_task.id)
        self.assertEqual(notification["input_kind"], "")
        self.assertEqual(notification["response_protocol"], "")
        self.assertIn("--message", notification["respond_command"])
        self.assertNotIn("--approve", notification["respond_command"])


class ResourceDecisionPublicViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.config = {
            "workspace_root": str(self.root / "workspace"),
            "executors": {"claude": {"max_budget_usd": 12.0}},
            "sessions": {"retain_executor_sessions": False},
        }
        self.service = TaskService(self.board, config=self.config)
        self.task = self.service.create_task(
            "public view",
            "claude",
            [{"id": 1, "description": "render"}],
            customer_dir=False,
        )

    def _inject_decision(self) -> None:
        raw = self.service.store.read_task(self.task.id)
        raw["extensions"][RESOURCE_EXTENSION_KEY] = build_resource_snapshot(
            "claude",
            12.0,
            source="configured",
        )
        snapshot = raw["extensions"][RESOURCE_EXTENSION_KEY]
        snapshot["current_limit"] = 24.0
        snapshot["exhaustion_count"] = 1
        snapshot["last_decision"] = "increase"
        self.service.store.write_task(self.task.id, raw)

    def test_execution_policy_view_adds_fields_and_keeps_limit(self) -> None:
        stored = self.service.get_task(self.task.id)
        resources = execution_policy_view(stored.extensions)["resources"]
        self.assertEqual(resources["limit"], 12.0)
        self.assertEqual(resources["configured_limit"], 12.0)
        self.assertEqual(resources["exhaustion_count"], 0)
        self.assertEqual(resources["last_decision"], "")
        self.assertTrue(resources["frozen"])

    def test_status_json_report_and_cli_render_decision_consistently(self) -> None:
        self._inject_decision()
        status = task_to_status(self.service.get_task(self.task.id))
        resources = status["execution_policy"]["resources"]
        self.assertEqual(resources["limit"], 24.0)
        self.assertEqual(resources["configured_limit"], 12.0)
        self.assertEqual(resources["exhaustion_count"], 1)
        self.assertEqual(resources["last_decision"], "increase")
        self.assertNotIn("executor_project_root", json.dumps(status))
        self.assertNotIn("project_path", json.dumps(status))

        markdown = generate_report_md(self.task.id, self.board)
        self.assertIn("- Effective limit: `24.0`", markdown)
        self.assertIn("- Configured limit: `12.0`", markdown)
        self.assertIn("- Exhaustion count: `1`", markdown)
        self.assertIn("- Last decision: `increase`", markdown)
        report = generate_report(self.task.id, self.board)
        self.assertEqual(
            report["execution_policy"]["resources"]["configured_limit"],
            12.0,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_task_status(status)
        rendered = output.getvalue()
        self.assertIn("Resources: max_budget_usd=24.0", rendered)
        self.assertIn("configured=12.0", rendered)
        self.assertIn("exhaustions=1", rendered)
        self.assertIn("last_decision=increase", rendered)
        self.assertNotIn("executor_project_root", rendered)

        policy_output = io.StringIO()
        with contextlib.redirect_stdout(policy_output):
            _print_execution_policy(status["execution_policy"])
        self.assertIn("configured=12.0", policy_output.getvalue())

    def test_preflight_shares_the_same_public_policy(self) -> None:
        self._inject_decision()
        status = task_to_status(self.service.get_task(self.task.id))
        preflight = self.service.preflight(self.task.id)
        self.assertEqual(preflight.execution_policy, status["execution_policy"])

    def test_report_marks_resource_decision_input(self) -> None:
        self.assertTrue(
            self.service.finalize_task_from_agent(
                self.task.id,
                _resource_decision_callback(self.task.id),
            )
        )
        markdown = generate_report_md(self.task.id, self.board)
        self.assertIn(
            "- Kind/response protocol: `resource_limit` / `approve_deny`",
            markdown,
        )
        self.assertIn(f"- Option `{RESOURCE_DECISION_APPROVE_LABEL}`", markdown)
        self.assertIn(f"- Option `{RESOURCE_DECISION_DENY_LABEL}`", markdown)


class Phase4DocsRegressionTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        path = PROJECT_ROOT / relative
        self.assertTrue(path.is_file(), f"missing doc: {path}")
        return path.read_text(encoding="utf-8")

    def test_checklist_describes_phase4_slice_and_keeps_cleanup_open(self) -> None:
        checklist = self._read("AGENTBC_1.0.2A_DEVELOPMENT_CHECKLIST.md")
        self.assertIn(RESOURCE_DECISION_APPROVE_LABEL, checklist)
        self.assertIn("configured_limit", checklist)
        self.assertIn("exhaustion_count", checklist)
        self.assertIn("last_decision", checklist)
        self.assertIn("--approve", checklist)
        self.assertIn("SESSION-001", checklist)
        self.assertIn("保持打开", checklist)
        self.assertIn("本切片不实现 purge/delete", checklist)

    def test_handbook_describes_phase4_ux_without_closing_cleanup(self) -> None:
        handbook = self._read("AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md")
        self.assertIn(RESOURCE_DECISION_APPROVE_LABEL, handbook)
        self.assertIn(RESOURCE_DECISION_DENY_LABEL, handbook)
        self.assertIn("approve_deny", handbook)
        self.assertIn("configured_limit", handbook)
        self.assertIn("exhaustion_count", handbook)
        self.assertIn("last_decision", handbook)
        self.assertIn("--approve", handbook)
        self.assertIn("--deny", handbook)
        self.assertIn("SESSION-001", handbook)
        self.assertIn("cleanup", handbook)

    def test_chinese_user_guide_documents_respond_fallback(self) -> None:
        guide = self._read("docs/USER_GUIDE_ZH.md")
        self.assertIn(RESOURCE_DECISION_APPROVE_LABEL, guide)
        self.assertIn(RESOURCE_DECISION_DENY_LABEL, guide)
        self.assertIn("--approve", guide)
        self.assertIn("--deny", guide)
        self.assertIn("agentbc task respond", guide)
        self.assertIn("cleanup", guide)


if __name__ == "__main__":
    unittest.main()
