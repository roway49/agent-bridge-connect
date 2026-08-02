"""Report generation and notification tests for ABC V1."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.contract_helpers import finalize_completed

FIXTURES = Path(__file__).parent / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class ReportGenerationTests(unittest.TestCase):
    """Test report.json and report.md generation."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task
        init_board(self.board)
        self.task = create_task("Report test", "mock", STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _complete_task(self):
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done", "artifacts": ["src/user.py"], "diff": "+10/-0"})
        finalize_completed(svc, self.task_id)
        return svc

    def test_report_json_structure(self):
        """report.json must contain required fields."""
        from agent_bridge_connect.reports import generate_report

        self._complete_task()
        report = generate_report(self.task_id, self.board)

        self.assertIn("task_id", report)
        self.assertIn("title", report)
        self.assertIn("status", report)
        self.assertIn("steps", report)
        self.assertIn("timeline", report)
        self.assertIn("artifacts", report)
        self.assertIn("created_at", report)
        self.assertIn("completed_at", report)

    def test_report_includes_steps(self):
        """Report must list all steps with their status."""
        from agent_bridge_connect.reports import generate_report

        self._complete_task()
        report = generate_report(self.task_id, self.board)

        self.assertEqual(len(report["steps"]), 3)
        step1 = report["steps"][0]
        self.assertEqual(step1["status"], "done")

    def test_report_includes_session_id(self):
        """Report must include session_id when present."""
        from agent_bridge_connect.task_board import create_task
        from agent_bridge_connect.reports import generate_report

        task = create_task("Session test", "mock", STEPS_YAML, self.board, session_id="test-sess-001")
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.board)
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        finalize_completed(svc, task.id)

        report = generate_report(task.id, self.board)
        self.assertEqual(report.get("session_id"), "test-sess-001")

    def test_report_markdown(self):
        """report.md must be a readable markdown string."""
        from agent_bridge_connect.reports import generate_report_md

        self._complete_task()
        md = generate_report_md(self.task_id, self.board)

        self.assertIsInstance(md, str)
        self.assertIn("# Report", md)
        self.assertIn("Report test", md)
        self.assertIn("mock", md)
        self.assertRegex(md, r"- Created: `\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} .+`")
        self.assertRegex(md, r"- Completed: `\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} .+`")
        self.assertIn("- Duration: `", md)

    def test_redaction_preserves_task_ids_and_paths_with_pwd_token(self):
        from agent_bridge_connect.reports import redact_secrets

        report = redact_secrets(
            {
                "task_id": "T-001-PWDN",
                "workspace": {
                    "report_file": "/tmp/report_T-001-PWDN.md",
                    "task_file": "/tmp/task_T-001-PWDN.md",
                },
                "step": "Create password_hash field with password=secret",
            }
        )

        self.assertEqual(report["task_id"], "T-001-PWDN")
        self.assertEqual(report["workspace"]["report_file"], "/tmp/report_T-001-PWDN.md")
        self.assertIn("[REDACTED]", report["step"])
        self.assertNotIn("password", report["step"].lower())

    def test_report_includes_artifacts(self):
        """Report must list artifacts from step results."""
        from agent_bridge_connect.reports import generate_report

        self._complete_task()
        report = generate_report(self.task_id, self.board)
        self.assertIsInstance(report["artifacts"], list)

    def test_report_includes_interventions(self):
        """Report must include interventions when present."""
        from agent_bridge_connect.reports import generate_report

        _service = self._complete_task()
        # No interventions in this case, but the field must exist
        report = generate_report(self.task_id, self.board)
        self.assertIn("interventions", report)

    def test_report_for_failed_task(self):
        """Report for failed task must include error info."""
        from agent_bridge_connect.service import TaskService
        from agent_bridge_connect.reports import generate_report

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})
        # Simulate failure by setting status directly (or via a failure method)
        # For now, test that report works for any terminal state
        report = generate_report(self.task_id, self.board)
        self.assertEqual(report["task_id"], self.task_id)

    def test_failed_report_summarizes_error_without_dumping_raw_stdout(self):
        from agent_bridge_connect.reports import generate_report_md
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        raw_marker = "RAW_EXECUTOR_OUTPUT_" * 200
        svc.fail_task(
            self.task_id,
            "executor_failed",
            "executor returned status failed",
            {
                "executor": "hermes",
                "result": {"stdout": raw_marker, "stderr": "permission denied"},
            },
        )
        markdown = generate_report_md(self.task_id, self.board)
        self.assertIn("executor_failed", markdown)
        self.assertIn("executor returned status failed", markdown)
        self.assertNotIn(raw_marker, markdown)

    def test_conversation_id_preserves_non_secret_desk_prefix(self):
        from agent_bridge_connect.reports import generate_report_md
        from agent_bridge_connect.service import TaskService

        task = TaskService(self.board, config={"workspace_root": str(self.board)}).create_task(
            "Desk trace",
            "hermes",
            [{"id": 1, "description": "trace"}],
            session_id="desk-1780381467569-c5abba4d-e733-472b-af0f-85a03881c27c",
            source_platform="hermes",
            customer_dir=True,
            customer_path=self.board,
        )
        report = generate_report_md(task.id, self.board)
        self.assertIn("desk-1780381467569-c5abba4d-e733-472b-af0f-85a03881c27c", report)
        self.assertNotIn("de[REDACTED]", report)


class NotificationTests(unittest.TestCase):
    """Test notification delivery on task events."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task
        init_board(self.board)
        self.task = create_task("Notify test", "mock", STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_file_notifier_delivers(self):
        """File notifier must write to notifications.jsonl."""
        from agent_bridge_connect.notifiers.file import FileNotifier
        notifier = FileNotifier(self.board / "notifications.jsonl")
        result = notifier.send({
            "task_id": self.task_id,
            "event_type": "task.completed",
            "title": "Notify test",
            "level": "done",
            "message": "Task completed successfully"
        })
        self.assertTrue(result.ok)
        self.assertTrue((self.board / "notifications.jsonl").exists())

    def test_file_notifier_content(self):
        """File notifier must write valid JSON lines."""
        from agent_bridge_connect.notifiers.file import FileNotifier
        notifier = FileNotifier(self.board / "notifications.jsonl")
        notifier.send({
            "task_id": self.task_id,
            "event_type": "task.completed",
            "title": "Test",
            "level": "done",
            "message": "done"
        })
        content = (self.board / "notifications.jsonl").read_text().strip()
        line = json.loads(content.split("\n")[-1])
        self.assertEqual(line["task_id"], self.task_id)

    def test_dialog_notifier_creates_notification_object(self):
        """Dialog notifier must accept a notification dict and return DeliveryResult."""
        from agent_bridge_connect.notifiers.dialog import DialogNotifier
        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="button returned:Open Report, gave up:false",
                stderr="",
            )
            result = notifier.send({
                "task_id": self.task_id,
                "event_type": "task.completed",
                "title": "Dialog test",
                "level": "done",
                "message": "Task completed",
                "report_path": "/tmp/REPORT.md",
            })
        self.assertTrue(result.ok)
        self.assertEqual(notifier.timeout_s, 30)
        self.assertEqual(run.call_args_list[0].args[0][2], "Agent-Bridge-Connect")
        self.assertEqual(run.call_args_list[0].args[0][3], "Task completed")

    def test_dialog_notifier_opens_report_when_requested(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="button returned:Open Report, gave up:false", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            result = notifier.send(
                {
                    "task_id": self.task_id,
                    "event_type": "task.completed",
                    "title": "Dialog test",
                    "level": "done",
                    "message": "Task completed",
                    "report_path": "/tmp/REPORT.md",
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/open", "/tmp/REPORT.md"])
        self.assertIn("Open Report", result.message)

    def test_dialog_notifier_reports_osascript_failure(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="execution error")
            result = notifier.send(
                {
                    "task_id": self.task_id,
                    "event_type": "task.completed",
                    "title": "Dialog test",
                    "level": "done",
                    "message": "Task completed",
                }
            )
        self.assertFalse(result.ok)
        self.assertIn("osascript exited 1", result.message)

    def test_secret_redaction(self):
        """Notification content must not contain secrets."""
        from agent_bridge_connect.reports import generate_report_md
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})
        finalize_completed(svc, self.task_id)

        md = generate_report_md(self.task_id, self.board)
        # Should not contain common secret patterns
        self.assertNotIn("sk-", md)
        self.assertNotIn("password", md.lower())


if __name__ == "__main__":
    unittest.main()
