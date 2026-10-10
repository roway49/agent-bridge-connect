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
from agent_bridge_connect.execution_policy import RESOURCE_EXTENSION_KEY, build_resource_snapshot, execution_policy_view
from agent_bridge_connect.notifications import RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL, build_input_required_notification, notify_input_required
from agent_bridge_connect.notifiers.dialog import DialogNotifier
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, task_to_status
import tempfile as _input_manifest_tempfile
import unittest as _input_manifest_unittest
from pathlib import Path as _input_manifest_Path
from agent_bridge_connect.input_manifest import (
    INPUTS_EXTENSION_KEY as _input_manifest_INPUTS_EXTENSION_KEY,
    cleanup_task_input_root as _input_manifest_cleanup_task_input_root,
    prepare_task_inputs as _input_manifest_prepare_task_inputs,
    public_inputs_view as _input_manifest_public_inputs_view,
    task_input_paths as _input_manifest_task_input_paths,
)
from agent_bridge_connect.protocol import ABCError as _input_manifest_ABCError
from agent_bridge_connect.prompt_contract import build_prompt_contract as _input_manifest_build_prompt_contract
from agent_bridge_connect.service import TaskService as _input_manifest_TaskService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
            "options": [{"label": RESOURCE_DECISION_APPROVE_LABEL, "description": "Double this task resource and continue."}, {"label": RESOURCE_DECISION_DENY_LABEL, "description": "Terminate the task as failed."}],
        },
        "step_results": [{"id": 1, "status": "blocked"}],
    }
    callback.update(updates)
    return callback


class ResourceDecisionDialogTests(unittest.TestCase):
    def test_input_action_maps_resource_decision_buttons(self) -> None:
        options = (RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL)
        for button, expected in ((RESOURCE_DECISION_APPROVE_LABEL, "approve"), (RESOURCE_DECISION_DENY_LABEL, "deny"), ("Later", "dismissed"), ("unknown", "dismissed")):
            with self.subTest(button=button):
                self.assertEqual(DialogNotifier._input_action(button, "choice", False, options, input_kind="resource_limit", response_protocol="approve_deny"), expected)
        self.assertEqual(DialogNotifier._input_action(RESOURCE_DECISION_APPROVE_LABEL, "choice", True, options, input_kind="resource_limit", response_protocol="approve_deny"), "dismissed")

    def test_input_action_keeps_ordinary_choice_semantics(self) -> None:
        options = ("Option A", "Option B")
        self.assertEqual(DialogNotifier._input_action("Option B", "choice", False, options), "message")
        self.assertEqual(DialogNotifier._input_action("Later", "choice", False, options), "dismissed")
        self.assertEqual(DialogNotifier._input_action("Option A", "choice", False, options, input_kind="resource_limit", response_protocol="approve_deny"), "approve")
        self.assertEqual(DialogNotifier._input_action("Option B", "choice", False, options, input_kind="resource_limit", response_protocol="approve_deny"), "deny")
        self.assertEqual(DialogNotifier._input_action("Deny", "permission", False), "deny")
        self.assertEqual(DialogNotifier._input_action("Deny", "permission", True), "deny")
        self.assertEqual(DialogNotifier._input_action("unknown", "permission", False), "deny")

    def test_permission_dialog_timeout_and_close_fail_closed_to_deny(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Deny\ngave up:true", stderr="")
            timed_out = notifier.send({"event_type": "task.input_required", "message": "Allow access?", "input_type": "permission"})
        self.assertEqual(timed_out.details, {"action": "deny", "decision_source": "timeout"})
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', run.call_args.kwargs["input"])
        self.assertNotIn('"Later"', run.call_args.kwargs["input"])
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")
            closed = notifier.send({"event_type": "task.input_required", "message": "Allow access?", "input_type": "permission"})
        self.assertEqual(closed.details, {"action": "deny", "decision_source": "dialog_closed"})

    def test_send_renders_resource_decision_buttons_and_maps_approve(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=f"button returned:{RESOURCE_DECISION_APPROVE_LABEL}, gave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "Decide", "input_type": "choice", "input_kind": "resource_limit", "response_protocol": "approve_deny", "input_options": [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL]})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "approve"})
        self.assertEqual(run.call_args.args[0], ["/usr/bin/osascript", "-", "Agent-Bridge-Connect", "Decide", RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL])
        self.assertIn('buttons {"Later", (item 3 of argv), (item 4 of argv)}', run.call_args.kwargs["input"])
        self.assertIn('default button "Later"', run.call_args.kwargs["input"])

    def test_send_later_keeps_task_waiting(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="button returned:Later, gave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "Decide", "input_type": "choice", "input_kind": "resource_limit", "response_protocol": "approve_deny", "input_options": [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL]})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "dismissed"})

    def test_send_timeout_keeps_task_waiting(self) -> None:
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=f"button returned:{RESOURCE_DECISION_DENY_LABEL}, gave up:true", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "Decide", "input_type": "choice", "input_kind": "resource_limit", "response_protocol": "approve_deny", "input_options": [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL]})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "dismissed"})


class ResourceDecisionNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.board = self.base / "record"
        self.service = TaskService(self.board, config={"workspace_root": str(self.base / "workspace")})
        self.task = self.service.create_task("resource decision", "shell", [{"id": 1, "description": "run with budget"}], customer_dir=False)
        self.assertTrue(self.service.finalize_task_from_agent(self.task.id, _resource_decision_callback(self.task.id)))

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
        self.assertEqual(notification["input_options"], [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL])
        self.assertEqual(notification["respond_command"], f"agentbc task respond {self.task.id} --input {request['input_id']} --approve (or --deny)")
        self.assertIn(RESOURCE_DECISION_APPROVE_LABEL, notification["message"])
        self.assertIn(RESOURCE_DECISION_DENY_LABEL, notification["message"])
        self.assertNotIn("Deadline:", notification["message"])
        self.assertNotIn("Respond:", notification["message"])
        self.assertNotIn("hidden", json.dumps(notification, ensure_ascii=False))

    def test_dialog_approve_responds_and_resumes_same_task(self) -> None:
        request = self._request()
        responder = mock.Mock(return_value={"task_id": self.task.id, "input_id": request["input_id"], "status": "running", "same_task": True})
        with mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=DeliveryResult(True, "shown", details={"action": "approve"})):
            result = notify_input_required(self.service, self.task.id, responder=responder)
        responder.assert_called_once_with(request["input_id"], "approve", "")
        self.assertEqual(result["response"]["status"], "running")
        events = self.service.store.read_events(self.task.id)
        self.assertEqual(events[-1]["response_type"], "approve")

    def test_dismissed_dialog_does_not_respond_and_task_stays_waiting(self) -> None:
        responder = mock.Mock()
        with mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=DeliveryResult(True, "shown", details={"action": "dismissed"})):
            result = notify_input_required(self.service, self.task.id, responder=responder)
        responder.assert_not_called()
        self.assertEqual(result["response"], {})
        self.assertEqual(self.service.get_task(self.task.id).status, "input_required")

    def test_ordinary_choice_keeps_message_fallback(self) -> None:
        choice_task = self.service.create_task("ordinary choice", "shell", [{"id": 1, "description": "choose"}], customer_dir=False)
        callback = {
            "version": 1,
            "task_id": choice_task.id,
            "final_state": "input_required",
            "summary": "Choose an option",
            "input": {"type": "choice", "reason": "The output format must be selected.", "options": [{"label": "Option A", "description": "Compact text result."}, {"label": "Option B", "description": "Detailed JSON result."}]},
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        self.assertTrue(self.service.finalize_task_from_agent(choice_task.id, callback))
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
        self.config = {"workspace_root": str(self.root / "workspace"), "executors": {"claude": {"max_budget_usd": 12.0}}, "sessions": {"retain_executor_sessions": False}}
        self.service = TaskService(self.board, config=self.config)
        self.task = self.service.create_task("public view", "claude", [{"id": 1, "description": "render"}], customer_dir=False)

    def _inject_decision(self) -> None:
        raw = self.service.store.read_task(self.task.id)
        raw["extensions"][RESOURCE_EXTENSION_KEY] = build_resource_snapshot("claude", 12.0, source="configured")
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
        self.assertEqual(report["execution_policy"]["resources"]["configured_limit"], 12.0)
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
        self.assertTrue(self.service.finalize_task_from_agent(self.task.id, _resource_decision_callback(self.task.id)))
        markdown = generate_report_md(self.task.id, self.board)
        self.assertIn("- Kind/response protocol: `resource_limit` / `approve_deny`", markdown)
        self.assertIn(f"- Option `{RESOURCE_DECISION_APPROVE_LABEL}`", markdown)
        self.assertIn(f"- Option `{RESOURCE_DECISION_DENY_LABEL}`", markdown)


class Phase4DocsRegressionTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        path = PROJECT_ROOT / relative
        self.assertTrue(path.is_file(), f"missing doc: {path}")
        return path.read_text(encoding="utf-8")

    def _read_private(self, relative: str) -> str:
        path = PROJECT_ROOT / relative
        if not path.is_file():
            self.skipTest(f"private development document is not published: {relative}")
        return path.read_text(encoding="utf-8")

    def test_chinese_user_guide_documents_respond_fallback(self) -> None:
        guide = self._read("docs/USER_GUIDE_ZH.md")
        self.assertIn(RESOURCE_DECISION_APPROVE_LABEL, guide)
        self.assertIn(RESOURCE_DECISION_DENY_LABEL, guide)
        self.assertIn("--approve", guide)
        self.assertIn("--deny", guide)
        self.assertIn("agentbc task respond", guide)
        self.assertIn("cleanup", guide)


class _input_manifest_InputManifestTests(_input_manifest_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _input_manifest_tempfile.TemporaryDirectory()
        self.root = _input_manifest_Path(self.temporary.name)
        self.project = self.root / "project"
        self.external = self.root / "outside"
        self.project.mkdir()
        self.external.mkdir()
        self.workspace = {"agentbc_root": str(self.root / "workspace"), "project_root": str(self.project), "task_date": "2026-09-13", "task_code": "ABCD", "iteration": "001", "task_id": "ABCD-001"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_external_image_and_file_are_frozen_without_type_or_size_policy(self) -> None:
        image = self.external / "input.unlisted-image-type"
        file_input = self.external / "payload.custom"
        image.write_bytes(b"image-bytes")
        file_input.write_bytes(b"x" * (1024 * 1024 + 7))
        (extension, committed) = _input_manifest_prepare_task_inputs(images=[image], files=[file_input], workspace=self.workspace)
        self.assertIsNotNone(committed)
        packet = {"extensions": extension}
        frozen_images = _input_manifest_task_input_paths(packet, kind="image")
        frozen_files = _input_manifest_task_input_paths(packet, kind="file")
        self.assertEqual(frozen_images[0].read_bytes(), b"image-bytes")
        self.assertEqual(frozen_files[0].read_bytes(), file_input.read_bytes())
        self.assertNotEqual(frozen_images[0], image)
        self.assertEqual(extension[_input_manifest_INPUTS_EXTENSION_KEY]["entries"][0]["source_scope"], "external_import")
        _input_manifest_cleanup_task_input_root(committed)

    def test_project_input_is_direct_and_public_projection_redacts_path(self) -> None:
        source = self.project / "notes.txt"
        source.write_text("hello", encoding="utf-8")
        (extension, committed) = _input_manifest_prepare_task_inputs(images=[], files=[source], workspace=self.workspace)
        self.assertIsNone(committed)
        packet = {"extensions": extension}
        self.assertEqual(_input_manifest_task_input_paths(packet, kind="file"), [source.resolve()])
        public = _input_manifest_public_inputs_view(extension[_input_manifest_INPUTS_EXTENSION_KEY])
        self.assertNotIn("materialized_path", public["entries"][0])
        self.assertEqual(public["entries"][0]["source_scope"], "project")

    def test_symlink_source_fails_before_staging(self) -> None:
        target = self.external / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = self.external / "link.txt"
        link.symlink_to(target)
        with self.assertRaisesRegex(_input_manifest_ABCError, "input source must not be a symlink"):
            _input_manifest_prepare_task_inputs(images=[], files=[link], workspace=self.workspace)
        self.assertFalse((self.root / "workspace" / "tasks" / "inputs").exists())

    def test_generic_file_is_in_executor_prompt_and_does_not_create_approval(self) -> None:
        source = self.external / "query.txt"
        source.write_text("needle", encoding="utf-8")
        (extension, committed) = _input_manifest_prepare_task_inputs(images=[], files=[source], workspace=self.workspace)
        packet = {
            "task_id": "ABCD-001",
            "title": "input test",
            "workspace": {**self.workspace, "artifact_root": str(self.project), "report_root": str(self.root / "reports"), "task_file": str(self.root / "task.md"), "report_file": str(self.root / "report.md")},
            "task_board": {"root": str(self.root / "record")},
            "steps": [{"id": 1, "description": "inspect input", "status": "pending"}],
            "extensions": extension,
        }
        prompt = _input_manifest_build_prompt_contract(packet)
        self.assertIn("Frozen file inputs:", prompt)
        self.assertIn(str(_input_manifest_task_input_paths(packet, kind="file")[0]), prompt)
        self.assertNotIn("agentbc.approval", extension)
        self.assertNotIn("agentbc.input", extension)
        _input_manifest_cleanup_task_input_root(committed)

    def test_task_creation_freezes_external_file_before_source_disappears(self) -> None:
        source = self.external / "external.txt"
        source.write_text("frozen", encoding="utf-8")
        service = _input_manifest_TaskService(self.root / "record", config={"workspace_root": str(self.root / "workspace")})
        task = service.create_task("external input", "codex", [{"id": 1, "description": "read the file"}], customer_dir=True, customer_path=self.project, files=[source], permission_mode="full")
        source.unlink()
        frozen = _input_manifest_task_input_paths(task.to_dict(), kind="file")
        self.assertEqual(frozen[0].read_text(encoding="utf-8"), "frozen")
        self.assertNotIn("agentbc.approval", task.extensions)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_input_import_does_not_change_any_executor_permission_mode(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            for mode in ("safe", "full"):
                with self.subTest(executor=executor, mode=mode):
                    source = self.external / f"{executor}-{mode}.txt"
                    source.write_text("payload", encoding="utf-8")
                    service = _input_manifest_TaskService(self.root / f"record-{executor}-{mode}", config={"workspace_root": str(self.root / f"workspace-{executor}-{mode}")})
                    task = service.create_task("permission invariant", executor, [{"id": 1, "description": "read input"}], customer_dir=True, customer_path=self.project, files=[source], permission_mode=mode)
                    permission = task.extensions["agentbc.permission"]
                    self.assertEqual(permission["effective_mode"], mode)
                    self.assertNotIn("agentbc.approval", task.extensions)
                    self.assertNotIn("agentbc.input", task.extensions)

    def test_task_delete_removes_only_agentbc_owned_frozen_inputs(self) -> None:
        source = self.external / "delete-me-source.txt"
        source.write_text("preserve source", encoding="utf-8")
        service = _input_manifest_TaskService(self.root / "delete-record", config={"workspace_root": str(self.root / "delete-workspace")})
        task = service.create_task("delete frozen input", "codex", [{"id": 1, "description": "read input"}], customer_dir=True, customer_path=self.project, files=[source], permission_mode="full")
        input_root = _input_manifest_Path(task.workspace["input_root"])
        task.status = "completed"
        service.store.write_task(task.id, task.to_dict())
        service._refresh_task_index()
        result = service.delete_task_chain(task.workspace["task_code"], confirmed=True)
        self.assertEqual(result["status"], "deleted")
        self.assertFalse(input_root.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "preserve source")
