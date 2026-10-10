"""Test abc task status, watch, and json output."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.task_board import create_task, init_board


FIXTURES = Path(__file__).parents[1] / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class TaskStatusTests(unittest.TestCase):
    """Test abc task status command."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        init_board(self.board)

        # Create a task with mixed step statuses
        self.task = create_task("Status test", "codex", STEPS_YAML, self.board)
        # Manually update step 1 to done, step 2 to in_progress
        task_path = self.board / self.task.id.split("-")[0] / self.task.id.split("-")[1] / "task.json"
        data = json.loads(task_path.read_text())
        data["status"] = "in_progress"
        data["steps"][0]["status"] = "done"
        data["steps"][1]["status"] = "in_progress"
        task_path.write_text(json.dumps(data, indent=2))
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_status_output(self):
        """Status should return task details with all step states."""
        from agent_bridge_connect.task_board import get_task_status

        status = get_task_status(self.task_id, self.board)
        self.assertEqual(status["id"], self.task_id)
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(len(status["steps"]), 3)
        # Check step statuses
        step_statuses = [s["status"] for s in status["steps"]]
        self.assertEqual(step_statuses, ["done", "in_progress", "pending"])

    def test_status_json_format(self):
        """Status --json should produce valid machine-readable JSON."""
        from agent_bridge_connect.task_board import get_task_status

        status = get_task_status(self.task_id, self.board)
        output = json.dumps(status, indent=2)
        parsed = json.loads(output)
        self.assertEqual(parsed["id"], self.task_id)
        self.assertIn("steps", parsed)
        self.assertIn("status", parsed)
        self.assertIn("title", parsed)
        self.assertIn("assignee", parsed)

    def test_status_includes_timestamps(self):
        """Status should include created_at and updated_at."""
        from agent_bridge_connect.task_board import get_task_status

        status = get_task_status(self.task_id, self.board)
        self.assertIn("created_at", status)
        self.assertIn("updated_at", status)

    def test_status_includes_intervention(self):
        """Status should include intervention state."""
        from agent_bridge_connect.task_board import get_task_status

        status = get_task_status(self.task_id, self.board)
        self.assertIn("intervention", status)
        self.assertFalse(status["intervention"]["paused"])

    def test_status_includes_errors(self):
        """Status should include errors list."""
        from agent_bridge_connect.task_board import get_task_status

        status = get_task_status(self.task_id, self.board)
        self.assertIn("errors", status)
        self.assertIsInstance(status["errors"], list)

    def test_status_nonexistent_task(self):
        """Status for nonexistent task should raise TaskNotFoundError."""
        from agent_bridge_connect.task_board import get_task_status, TaskNotFoundError

        with self.assertRaises(TaskNotFoundError):
            get_task_status("T-999", self.board)


class TaskWatchTests(unittest.TestCase):
    """Test abc task status --watch behavior."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        init_board(self.board)
        self.task = create_task("Watch test", "codex", STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_poll_detects_change(self):
        """Watch poll should detect status changes."""
        from agent_bridge_connect.task_board import poll_task

        # Initial state
        initial = poll_task(self.task_id, self.board)
        self.assertEqual(initial["status"], "pending")

        # Change status on disk
        task_path = self.board / self.task_id.split("-")[0] / self.task_id.split("-")[1] / "task.json"
        data = json.loads(task_path.read_text())
        data["status"] = "in_progress"
        task_path.write_text(json.dumps(data, indent=2))

        # Poll again should detect change
        updated = poll_task(self.task_id, self.board)
        self.assertEqual(updated["status"], "in_progress")

    def test_poll_no_change_returns_same(self):
        """Poll without changes should return same status."""
        from agent_bridge_connect.task_board import poll_task

        first = poll_task(self.task_id, self.board)
        second = poll_task(self.task_id, self.board)
        self.assertEqual(first["status"], second["status"])


class TaskListFilterTests(unittest.TestCase):
    """Test abc task list with status filtering."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        init_board(self.board)

        # Create tasks with different statuses
        t1 = create_task("Pending A", "codex", STEPS_YAML, self.board)
        _t2 = create_task("Pending B", "claude", STEPS_YAML, self.board)
        t3 = create_task("Done task", "codex", STEPS_YAML, self.board)

        # Set t3 to done
        path = self.board / t3.id.split("-")[0] / t3.id.split("-")[1] / "task.json"
        data = json.loads(path.read_text())
        data["status"] = "done"
        path.write_text(json.dumps(data, indent=2))

        # Set t1 to in_progress
        path = self.board / t1.id.split("-")[0] / t1.id.split("-")[1] / "task.json"
        data = json.loads(path.read_text())
        data["status"] = "in_progress"
        path.write_text(json.dumps(data, indent=2))

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_filter_by_status_pending(self):
        """Filter by pending should return only pending tasks."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        pending = board.list_all(status="pending")
        self.assertEqual(len(pending), 1)

    def test_filter_by_status_done(self):
        """Filter by done should return only done tasks."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        done = board.list_all(status="done")
        self.assertEqual(len(done), 1)

    def test_filter_by_status_in_progress(self):
        """Filter by in_progress should return matching tasks."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        in_progress = board.list_all(status="in_progress")
        self.assertEqual(len(in_progress), 1)

    def test_filter_combined_status_and_assignee(self):
        """Combined status and assignee filter should narrow results."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        # in_progress + codex should match exactly 1
        result = board.list_all(status="in_progress", assignee="codex")
        self.assertEqual(len(result), 1)
        # done + codex should match 1
        result = board.list_all(status="done", assignee="codex")
        self.assertEqual(len(result), 1)
        # pending + codex should match 0 (the pending one is claude)
        result = board.list_all(status="pending", assignee="codex")
        self.assertEqual(len(result), 0)


class WebSnapshotProjectionTests(unittest.TestCase):
    """Pure public DTO: no live task operations or inferred completion."""

    def test_projection_is_allowlisted_redacted_and_does_not_mutate(self):
        import copy
        from agent_bridge_connect.web_snapshot import project_task
        raw = {"id": "ABCD-001", "status": "failed", "title": "中文\n  keep spaces  ",
               "assignee": "opencode", "steps": [{"id": 1, "description": "<script>x</script>", "status": "pending", "result": "private"}],
               "errors": [{"code": "completion_marker_missing", "message": "api_key=privatevalue", "details": {"secret": "hidden"}}],
               "extensions": {"agentbc.execution": {"executor_run_id": "run-1", "attempt_index": 0, "token": "internal"},
                              "agentbc.input": {"type": "permission", "status": "waiting", "summary": "Authorization: Bearer abc.def", "input_id": "private-id", "reason_detail": "private"},
                              "unknown": {"secret": "hidden"}}, "workspace": {"root": "/private/path"}}
        before = copy.deepcopy(raw)
        result = project_task(raw)
        self.assertEqual(raw, before)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["title"], raw["title"])
        self.assertEqual(result["attempt_id"], "attempt-0")
        self.assertEqual(result["callback_state"], "missing")
        self.assertEqual(result["capabilities"]["full_lifecycle"], "unverified")
        encoded = json.dumps(result)
        for secret in ("privatevalue", "abc.def", "private-id", "hidden", "/private/path", "internal"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(result["steps"][0]["description"], "<script>x</script>")
        self.assertEqual(result["waiting"]["status"], "waiting")

    def test_callback_and_attempt_require_explicit_matching_facts(self):
        from agent_bridge_connect.web_snapshot import project_task
        for callback, expected in [(None, "missing"), ({}, "unknown"),
                ({"task_id": "ABCD-001", "executor_run_id": "run-1", "marker_valid": True}, "received"),
                ({"task_id": "ABCD-001", "executor_run_id": "old-run", "marker_valid": True}, "unknown"),
                ({"task_id": "OTHER-001", "executor_run_id": "run-1", "marker_valid": True}, "unknown")]:
            with self.subTest(expected=expected, callback=callback):
                raw = {"id": "ABCD-001", "status": "future_state", "steps": [], "extensions": {
                    "agentbc.execution": {"executor_run_id": "run-1"}, "agentbc.final_callback": callback}}
                view = project_task(raw)
                self.assertEqual(view["callback_state"], expected)
                self.assertIsNone(view["attempt_id"])
                self.assertEqual(view["status"], "future_state")
                self.assertIsNone(view["updated_at"])

    def test_existing_executor_states_keep_their_meaning(self):
        from agent_bridge_connect.web_snapshot import project_task
        for executor in ("codex", "claude", "hermes"):
            for state in ("pending", "running", "input_required", "needs_recovery", "completed", "failed", "cancelled"):
                with self.subTest(executor=executor, state=state):
                    raw = {"id": "ABCD-001", "assignee": executor, "status": state, "steps": [], "extensions": {}}
                    view = project_task(raw)
                    self.assertEqual(view["status"], state)
                    self.assertEqual(view["executor"], executor)
                    self.assertEqual(view["callback_state"], "missing")
                    self.assertIsNone(view["run_id"])
