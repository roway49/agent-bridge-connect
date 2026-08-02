"""Test abc task board: create, list, init commands."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.schema import validate_task


FIXTURES = Path(__file__).parent / "fixtures"


class TaskBoardInitTests(unittest.TestCase):
    """Test abc init command."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_init_creates_directory(self):
        """abc init should create the task board directory."""
        from agent_bridge_connect.task_board import init_board

        init_board(self.board)
        self.assertTrue(self.board.exists())
        self.assertTrue((self.board / "agents.yaml").exists())
        self.assertTrue((self.board / "README.md").exists())

    def test_init_creates_agents_yaml(self):
        """agents.yaml should contain a valid template."""
        from agent_bridge_connect.task_board import init_board

        init_board(self.board)
        agents_yaml = self.board / "agents.yaml"
        content = agents_yaml.read_text()
        self.assertIn("agents", content)

    def test_init_idempotent(self):
        """Calling init twice should not error."""
        from agent_bridge_connect.task_board import init_board

        init_board(self.board)
        init_board(self.board)
        self.assertTrue(self.board.exists())


class TaskCreateTests(unittest.TestCase):
    """Test abc task create command."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        self.steps_file = FIXTURES / "sample_steps.yaml"
        from agent_bridge_connect.task_board import init_board
        init_board(self.board)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _to_dict(self, p: Path) -> dict:
        return json.loads(p.read_text())

    def test_create_basic(self):
        """Create a basic task and verify task.json structure."""
        from agent_bridge_connect.task_board import create_task

        task = create_task(
            title="Add user authentication",
            assignee="codex",
            steps_path=self.steps_file,
            board_root=self.board,
        )
        # Verify return value
        self.assertEqual(task.title, "Add user authentication")
        self.assertEqual(task.assignee, "codex")
        self.assertEqual(task.status, "pending")
        self.assertEqual(len(task.steps), 3)

        # Verify file on disk
        task_path = self.board / task.id.split("-")[0] / task.id.split("-")[1] / "task.json"
        self.assertTrue(task_path.exists())
        data = self._to_dict(task_path)
        self.assertEqual(data["id"], task.id)
        self.assertEqual(data["title"], "Add user authentication")
        self.assertEqual(data["assignee"], "codex")
        self.assertEqual(data["status"], "pending")
        workspace = data["workspace"]
        expected_report_root = self.board.parent / "tasks" / "report" / workspace["task_date"] / workspace["task_code"]
        self.assertEqual(Path(workspace["report_root"]), expected_report_root.resolve())
        self.assertNotEqual(workspace["task_file"], workspace["report_file"])
        self.assertEqual(Path(workspace["task_file"]).name, f"{task.id}-task.md")
        self.assertEqual(Path(workspace["report_file"]).name, f"{task.id}-report.md")
        self.assertIn("/tasks/artifacts/", workspace["artifact_root"])
        self.assertNotIn("agentbc_tasks", workspace["artifact_root"])

    def test_create_assigns_unique_id(self):
        """Each root task gets a unique chain-local ID with a short chain token."""
        from agent_bridge_connect.task_board import create_task

        t1 = create_task("First", "codex", self.steps_file, self.board)
        t2 = create_task("Second", "codex", self.steps_file, self.board)
        self.assertNotEqual(t1.id, t2.id)
        self.assertRegex(t1.id, r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertRegex(t2.id, r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")

    def test_create_stores_session_id(self):
        """Task should store session_id when provided."""
        from agent_bridge_connect.task_board import create_task

        task = create_task("Test", "codex", self.steps_file, self.board,
                           session_id="test-session-001")
        data = self._to_dict(self.board / task.id.split("-")[0] / task.id.split("-")[1] / "task.json")
        self.assertEqual(data.get("session_id"), "test-session-001")

    def test_create_step_index(self):
        """Steps in task.json should have correct ids and record paths."""
        from agent_bridge_connect.task_board import create_task

        task = create_task("Test", "codex", self.steps_file, self.board)
        self.assertEqual(len(task.steps), 3)
        for i, step in enumerate(task.steps, 1):
            self.assertEqual(step["id"], i)
            self.assertEqual(step["record"], f"steps/{i:02d}.json")

    def test_create_validates(self):
        """Created task should pass schema validation."""
        from agent_bridge_connect.task_board import create_task

        task = create_task("Test", "codex", self.steps_file, self.board)
        task_path = self.board / task.id.split("-")[0] / task.id.split("-")[1] / "task.json"
        data = self._to_dict(task_path)
        errors = validate_task(data)
        self.assertEqual(errors, [], f"Task validation failed: {errors}")

    def test_create_missing_title(self):
        """Missing title should raise."""
        from agent_bridge_connect.task_board import create_task, TaskCreateError

        with self.assertRaises(TaskCreateError):
            create_task("", "codex", self.steps_file, self.board)

    def test_create_missing_assignee(self):
        """Missing assignee should raise."""
        from agent_bridge_connect.task_board import create_task, TaskCreateError

        with self.assertRaises(TaskCreateError):
            create_task("Test", "", self.steps_file, self.board)

    def test_create_missing_steps_file(self):
        """Missing steps.yaml should raise."""
        from agent_bridge_connect.task_board import create_task, TaskCreateError

        fake = self.test_dir / "nonexistent.yaml"
        with self.assertRaises(TaskCreateError):
            create_task("Test", "codex", fake, self.board)


class TaskListTests(unittest.TestCase):
    """Test abc task list command."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        self.steps_file = FIXTURES / "sample_steps.yaml"
        from agent_bridge_connect.task_board import init_board, create_task
        init_board(self.board)

        # Create a few tasks with different statuses and assignees
        create_task("Pending task", "codex", self.steps_file, self.board)
        create_task("Another pending", "claude", self.steps_file, self.board)
        # Manually set one to in_progress
        import json
        t3 = create_task("In progress task", "codex", self.steps_file, self.board)
        t3_path = self.board / t3.id.split("-")[0] / t3.id.split("-")[1] / "task.json"
        data = json.loads(t3_path.read_text())
        data["status"] = "in_progress"
        t3_path.write_text(json.dumps(data, indent=2))

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_list_all(self):
        """List should return all tasks."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        tasks = board.list_all()
        self.assertEqual(len(tasks), 3)

    def test_list_filter_by_status(self):
        """Filter by status should return matching tasks."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        pending = board.list_all(status="pending")
        self.assertEqual(len(pending), 2)
        in_progress = board.list_all(status="in_progress")
        self.assertEqual(len(in_progress), 1)

    def test_list_filter_by_assignee(self):
        """Filter by assignee should return matching tasks."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        codex_tasks = board.list_all(assignee="codex")
        # 2 codex tasks + the one that was manually set to in_progress but still codex
        self.assertEqual(len(codex_tasks), 2)
        claude_tasks = board.list_all(assignee="claude")
        self.assertEqual(len(claude_tasks), 1)

    def test_list_empty_board(self):
        """Empty task board should return empty list."""
        empty_dir = self.test_dir / "empty-board"
        empty_dir.mkdir()
        # Create a minimal record root without task-code directories.
        (empty_dir / "agents.yaml").write_text("agents: {}\n")
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(empty_dir)
        tasks = board.list_all()
        self.assertEqual(tasks, [])

    def test_list_sorts_by_id_desc(self):
        """TaskBoard list order is stable by canonical ID."""
        from agent_bridge_connect.task_board import TaskBoard

        board = TaskBoard(self.board)
        tasks = board.list_all()
        ids = [t.id for t in tasks]
        # Should be descending
        self.assertEqual(ids, sorted(ids, reverse=True))
