from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.cli import _print_atomic_dispatch, _print_task_status
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.reports import (
    generate_report,
    generate_report_md,
    generate_task_brief,
)
from agent_bridge_connect.runner import RunnerState
from agent_bridge_connect.protocol import PreflightResult
from agent_bridge_connect.service import TaskService, task_to_status


class Phase2PublicViewTests(unittest.TestCase):
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

    def _task(self, executor: str = "claude"):
        return self.service.create_task(
            "Public policy view",
            executor,
            [{"id": 1, "description": "render public views"}],
            customer_dir=False,
        )

    def _inject_task2_path(self, task) -> tuple[str, str]:
        internal_root = str(self.root / "workspace" / "private-task2-root")
        session_path = str(self.root / "workspace" / "private-session-path")
        raw = self.service.store.read_task(task.id)
        raw["workspace"]["executor_project_root"] = internal_root
        raw["extensions"][SESSION_EXTENSION_KEY]["project_path"] = session_path
        self.service.store.write_task(task.id, raw)
        return internal_root, session_path

    def test_status_json_text_and_preflight_share_path_free_policy(self) -> None:
        task = self._task()
        internal_root, session_path = self._inject_task2_path(task)
        stored = self.service.get_task(task.id)
        status = task_to_status(stored)
        policy = status["execution_policy"]

        self.assertEqual(policy["resources"]["limit"], 12.0)
        self.assertEqual(policy["resources"]["source"], "configured")
        self.assertTrue(policy["resources"]["frozen"])
        self.assertEqual(policy["session"]["project_mode"], "ephemeral")
        self.assertNotIn("executor_project_root", status["workspace"])
        self.assertNotIn("project_path", status["extensions"][SESSION_EXTENSION_KEY])
        self.assertNotIn(internal_root, json.dumps(status))
        self.assertNotIn(session_path, json.dumps(status))

        preflight = self.service.preflight(task.id)
        self.assertEqual(preflight.execution_policy, policy)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_task_status(status)
        rendered = output.getvalue()
        self.assertIn("Resources: max_budget_usd=12.0", rendered)
        self.assertIn("project_mode=ephemeral", rendered)
        self.assertNotIn(internal_root, rendered)
        self.assertNotIn(session_path, rendered)

    def test_preflight_result_extension_keeps_old_constructor_compatible(self) -> None:
        result = PreflightResult(ok=True, errors=[])
        self.assertEqual(result.execution_policy, {})

    def test_report_json_markdown_and_review_brief_share_public_policy(self) -> None:
        task = self._task()
        internal_root, session_path = self._inject_task2_path(task)
        report = generate_report(task.id, self.board)
        markdown = generate_report_md(task.id, self.board)
        brief = generate_task_brief(task.id, self.board)

        self.assertEqual(report["execution_policy"], brief["evidence"]["execution_policy"])
        self.assertEqual(report["execution_policy"]["resources"]["limit"], 12.0)
        self.assertNotIn("executor_project_root", report["workspace"])
        for rendered in (json.dumps(report), json.dumps(brief), markdown):
            self.assertNotIn(internal_root, rendered)
            self.assertNotIn(session_path, rendered)
        self.assertIn("## Execution Policy", markdown)
        self.assertIn("- Resource frozen: `yes`", markdown)

    def test_generated_task_requirements_show_policy_without_internal_path(self) -> None:
        task = self._task()
        text = Path(task.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertIn("- Resource policy: `max_budget_usd`", text)
        self.assertIn("- Effective resource limit: `12.0`", text)
        self.assertIn("- Executor project mode: `ephemeral`", text)
        self.assertNotIn(task.extensions[SESSION_EXTENSION_KEY]["project_path"], text)

    def test_atomic_accepted_view_is_sanitized_and_prints_policy(self) -> None:
        task = self._task()
        internal_root, session_path = self._inject_task2_path(task)
        task = self.service.get_task(task.id)
        state = object.__new__(RunnerState)
        state.dispatch_worker = mock.Mock(
            return_value={
                "run_id": "run-1",
                "dispatch_status": "accepted",
                "monitor_status": "disabled",
            }
        )
        state._ensure_task_list_dashboard = mock.Mock()
        result = state._atomic_dispatch_task(self.service, task, None, {})

        self.assertNotIn("executor_project_root", result["workspace"])
        self.assertEqual(result["execution_policy"]["resources"]["limit"], 12.0)
        encoded = json.dumps(result)
        self.assertNotIn(internal_root, encoded)
        self.assertNotIn(session_path, encoded)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_atomic_dispatch(result)
        self.assertIn("Resources: max_budget_usd=12.0", output.getvalue())

    def test_codex_public_resources_are_null(self) -> None:
        task = self._task("codex")
        status = task_to_status(task)
        self.assertIsNone(status["execution_policy"]["resources"])
        self.assertIsNotNone(status["execution_policy"]["session"])

    def test_ephemeral_claude_project_is_not_an_artifact(self) -> None:
        task = self._task()
        session_path = Path(task.extensions[SESSION_EXTENSION_KEY]["project_path"])
        session_path.mkdir(parents=True)
        (session_path / "receipt.json").write_text("{}", encoding="utf-8")
        artifact_root = Path(task.workspace["artifact_root"])
        (artifact_root / "deliverable.txt").write_text("done", encoding="utf-8")

        report = generate_report(task.id, self.board)
        self.assertIn("deliverable.txt", report["artifacts"])
        self.assertFalse(any("receipt.json" in str(item) for item in report["artifacts"]))
        self.assertFalse(any(task.id in str(item) and "claude" in str(item) for item in report["artifacts"]))


if __name__ == "__main__":
    unittest.main()
