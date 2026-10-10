"""Test abc task board: create, list, init commands."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from agent_bridge_connect.schema import validate_task
import tempfile as _task_policy_tempfile
import unittest as _task_policy_unittest
import uuid as _task_policy_uuid
from pathlib import Path as _task_policy_Path
from agent_bridge_connect.execution_policy import RESOURCE_EXTENSION_KEY as _task_policy_RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY as _task_policy_SESSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError as _task_policy_ABCError
from agent_bridge_connect.service import TaskService as _task_policy_TaskService
import tempfile as _path_plan_tempfile
import unittest as _path_plan_unittest
from pathlib import Path as _path_plan_Path
from agent_bridge_connect.path_model import build_path_plan as _path_plan_build_path_plan, canonical_executor_project_root as _path_plan_canonical_executor_project_root, validate_path_plan_workspace as _path_plan_validate_path_plan_workspace
from agent_bridge_connect.protocol import ABCError as _path_plan_ABCError

FIXTURES = Path(__file__).parents[1] / "fixtures"


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

        task = create_task(title="Add user authentication", assignee="codex", steps_path=self.steps_file, board_root=self.board)
        self.assertEqual(task.title, "Add user authentication")
        self.assertEqual(task.assignee, "codex")
        self.assertEqual(task.status, "pending")
        self.assertEqual(len(task.steps), 3)
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
        self.assertRegex(t1.id, "^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertRegex(t2.id, "^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")

    def test_create_stores_session_id(self):
        """Task should store session_id when provided."""
        from agent_bridge_connect.task_board import create_task

        task = create_task("Test", "codex", self.steps_file, self.board, session_id="test-session-001")
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
        create_task("Pending task", "codex", self.steps_file, self.board)
        create_task("Another pending", "claude", self.steps_file, self.board)
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
        self.assertEqual(len(codex_tasks), 2)
        claude_tasks = board.list_all(assignee="claude")
        self.assertEqual(len(claude_tasks), 1)

    def test_list_empty_board(self):
        """Empty task board should return empty list."""
        empty_dir = self.test_dir / "empty-board"
        empty_dir.mkdir()
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
        self.assertEqual(ids, sorted(ids, reverse=True))


class StepIdSchemaRegressionTests(unittest.TestCase):
    """PERM-104-002 / FLOW-104-001 correction (T2A7-001): string step ids
    used to pass task creation and later fail the matching final callback
    with ``completion_marker_task_steps_invalid`` after the whole run had
    executed.  Canonical new tasks require integer ids and invalid input
    fails BEFORE dispatch with a stable ``task_create_error``."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board

        init_board(self.board)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_create_rejects_string_step_id_before_dispatch(self) -> None:
        from agent_bridge_connect.protocol import ABCError
        from agent_bridge_connect.service import TaskService

        service = TaskService(self.board, config={"workspace_root": str(self.test_dir), "permission_mode": "safe"})
        with self.assertRaises(ABCError) as raised:
            service.create_task("string id", "claude", [{"id": "1", "description": "finish"}], customer_dir=True, customer_path=self.test_dir)
        self.assertEqual(raised.exception.code, "task_create_error")
        self.assertIn("integer", str(raised.exception))

    def test_create_rejects_other_non_integer_step_ids(self) -> None:
        from agent_bridge_connect.protocol import ABCError
        from agent_bridge_connect.service import TaskService

        service = TaskService(self.board, config={"workspace_root": str(self.test_dir), "permission_mode": "safe"})
        for bad in (1.5, True, [1], {"id": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(ABCError) as raised:
                    service.create_task("bad id", "claude", [{"id": bad, "description": "finish"}], customer_dir=True, customer_path=self.test_dir)
                self.assertEqual(raised.exception.code, "task_create_error")

    def test_create_defaults_missing_ids_and_keeps_integers(self) -> None:
        from agent_bridge_connect.service import TaskService

        service = TaskService(self.board, config={"workspace_root": str(self.test_dir), "permission_mode": "safe"})
        task = service.create_task("integer ids", "claude", [{"description": "first"}, {"id": 2, "description": "second"}], customer_dir=True, customer_path=self.test_dir)
        ids = [step["id"] for step in task.steps]
        self.assertEqual(ids, [1, 2])
        from agent_bridge_connect.execution_contract import validate_callback_payload

        validation = validate_callback_payload({"version": 1, "task_id": task.id, "final_state": "completed", "summary": "done", "step_results": [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}]}, task.id, task.steps)
        self.assertTrue(validation.valid, validation.message)

    def test_string_id_yaml_steps_rejected_at_creation(self) -> None:
        from agent_bridge_connect.protocol import ABCError
        from agent_bridge_connect.service import TaskService, load_steps

        steps_file = self.test_dir / "steps.yaml"
        steps_file.write_text('steps:\n  - id: "1"\n    description: finish\n', encoding="utf-8")
        service = TaskService(self.board, config={"workspace_root": str(self.test_dir), "permission_mode": "safe"})
        with self.assertRaises(ABCError) as raised:
            service.create_task("yaml string id", "claude", load_steps(str(steps_file)), customer_dir=True, customer_path=self.test_dir)
        self.assertEqual(raised.exception.code, "task_create_error")


class _task_policy_Phase2TaskPolicyTests(_task_policy_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _task_policy_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _task_policy_Path(self.temporary.name)
        self.board = self.root / "record"
        self.customer = self.root / "customer"
        self.customer.mkdir()

    def _service(self, **config: object) -> _task_policy_TaskService:
        return _task_policy_TaskService(self.board, config={"workspace_root": str(self.root / "workspace"), **config})

    def _create(self, service: _task_policy_TaskService, executor: str):
        return service.create_task("Phase 2 policy", executor, [{"id": 1, "description": "verify the frozen policy"}], customer_dir=True, customer_path=self.customer)

    def test_custom_claude_policy_preassigns_uuid_and_native_project(self) -> None:
        service = self._service(executors={"claude": {"max_budget_usd": 12.5}}, sessions={"retain_executor_sessions": True})
        task = self._create(service, "claude")
        resources = task.extensions[_task_policy_RESOURCE_EXTENSION_KEY]
        session = task.extensions[_task_policy_SESSION_EXTENSION_KEY]
        self.assertEqual(resources["current_limit"], 12.5)
        self.assertEqual(resources["source"], "configured")
        self.assertTrue(session["retain"])
        self.assertEqual(session["project_mode"], "native")
        self.assertEqual(session["project_path"], str(self.customer.resolve()))
        self.assertEqual(str(_task_policy_uuid.UUID(session["session_id"])), session["session_id"])

    def test_default_hermes_and_codex_policies_are_frozen(self) -> None:
        hermes = self._create(self._service(), "hermes")
        resources = hermes.extensions[_task_policy_RESOURCE_EXTENSION_KEY]
        session = hermes.extensions[_task_policy_SESSION_EXTENSION_KEY]
        self.assertEqual(resources["current_limit"], 90)
        self.assertEqual(resources["source"], "hermes_default_90")
        self.assertFalse(session["retain"])
        self.assertEqual(session["session_id"], "")
        self.assertEqual(session["session_state"], "pending")
        codex = self._create(self._service(), "codex")
        self.assertNotIn(_task_policy_RESOURCE_EXTENSION_KEY, codex.extensions)
        self.assertEqual(codex.extensions[_task_policy_SESSION_EXTENSION_KEY]["session_id"], "")
        self.assertEqual(codex.extensions[_task_policy_SESSION_EXTENSION_KEY]["project_mode"], "none")

    def test_default_claude_uses_canonical_ephemeral_path(self) -> None:
        task = self._create(self._service(), "claude")
        resources = task.extensions[_task_policy_RESOURCE_EXTENSION_KEY]
        session = task.extensions[_task_policy_SESSION_EXTENSION_KEY]
        expected = (self.root / "workspace" / "tasks" / "artifacts" / task.workspace["task_date"] / task.workspace["task_code"] / task.id / "claude").resolve()
        self.assertEqual(resources["current_limit"], 10.0)
        self.assertEqual(resources["source"], "claude_default_10")
        self.assertFalse(session["retain"])
        self.assertEqual(session["project_mode"], "ephemeral")
        self.assertEqual(_task_policy_Path(session["project_path"]), expected)
        self.assertFalse(expected.exists())

    def test_existing_executor_project_root_wins_for_task2_compatibility(self) -> None:
        service = self._service()
        task = self._create(service, "codex")
        workspace = dict(task.workspace)
        workspace["executor_project_root"] = str(self.root / "task2" / task.id / "claude")
        from agent_bridge_connect.execution_policy import build_task_execution_policy

        (_, session) = build_task_execution_policy("claude", service.config, workspace)
        self.assertIsNotNone(session)
        self.assertEqual(session["project_path"], workspace["executor_project_root"])

    def test_present_invalid_config_fields_fail_closed(self) -> None:
        invalid = ({"executors": {"claude": {"max_budget_usd": True}}}, {"executors": {"hermes": {"max_turns": 2.5}}}, {"executors": {"claude": "invalid-table"}}, {"sessions": {"retain_executor_sessions": "yes"}})
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaises(_task_policy_ABCError) as raised:
                    self._create(self._service(**config), "claude")
                self.assertEqual(raised.exception.code, "config_invalid")

    def test_resume_retry_recover_and_redispatch_style_requeue_do_not_drift(self) -> None:
        config = {"executors": {"hermes": {"max_turns": 40}}, "sessions": {"retain_executor_sessions": False}}
        service = self._service(**config)
        task = self._create(service, "hermes")
        frozen_resources = dict(task.extensions[_task_policy_RESOURCE_EXTENSION_KEY])
        frozen_session = dict(task.extensions[_task_policy_SESSION_EXTENSION_KEY])
        service.config["executors"]["hermes"]["max_turns"] = 99
        service.config["sessions"]["retain_executor_sessions"] = True
        raw = service.store.read_task(task.id)
        raw["status"] = "running"
        raw["intervention"]["paused"] = True
        raw["steps"][0]["status"] = "failed"
        service.store.write_task(task.id, raw)
        service.resume_task(task.id)
        service.retry_step(task.id, 1)
        raw = service.store.read_task(task.id)
        raw["status"] = "needs_recovery"
        service.store.write_task(task.id, raw)
        service.requeue_task(task.id)
        after = service.get_task(task.id)
        self.assertEqual(after.extensions[_task_policy_RESOURCE_EXTENSION_KEY], frozen_resources)
        self.assertEqual(after.extensions[_task_policy_SESSION_EXTENSION_KEY], frozen_session)

    def test_handoff_reads_current_config_for_the_new_target(self) -> None:
        service = self._service(executors={"claude": {"max_budget_usd": 15.0}}, sessions={"retain_executor_sessions": False})
        source = self._create(service, "claude")
        raw = service.store.read_task(source.id)
        raw["status"] = "completed"
        service.store.write_task(source.id, raw)
        service.config["executors"]["hermes"] = {"max_turns": 55}
        service.config["sessions"]["retain_executor_sessions"] = True
        handoff = service.handoff_task(source.id, "hermes")
        self.assertEqual(handoff.extensions[_task_policy_RESOURCE_EXTENSION_KEY]["current_limit"], 55)
        self.assertTrue(handoff.extensions[_task_policy_SESSION_EXTENSION_KEY]["retain"])
        self.assertEqual(handoff.extensions[_task_policy_SESSION_EXTENSION_KEY]["session_id"], "")

    def test_reassign_rebuilds_policy_and_records_only_public_views(self) -> None:
        service = self._service(executors={"claude": {"max_budget_usd": 11.0}, "hermes": {"max_turns": 66}})
        task = self._create(service, "claude")
        raw = service.store.read_task(task.id)
        raw["status"] = "needs_recovery"
        service.store.write_task(task.id, raw)
        service.reassign_task(task.id, "hermes")
        reassigned = service.get_task(task.id)
        self.assertNotIn("project_path", service.store.read_events(task.id)[-1]["execution_policy_before"]["session"])
        self.assertEqual(reassigned.extensions[_task_policy_RESOURCE_EXTENSION_KEY]["current_limit"], 66)
        self.assertEqual(reassigned.extensions[_task_policy_SESSION_EXTENSION_KEY]["executor"], "hermes")


class _path_plan_Phase2PathPlanTests(_path_plan_unittest.TestCase):
    """Phase 2 Claude Project Path Plan: planning and validation only.

    The canonical executor project root is strictly
    ``<agentbc_root>/tasks/artifacts/<task_date>/<task_code>/<task_id>/claude``.
    Building a plan never creates directories; validation only ever runs on
    path metadata.
    """

    def setUp(self) -> None:
        self.temporary = _path_plan_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _path_plan_Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.config = {"workspace_root": str(self.workspace)}
        self.task_date = "2026-08-10"

    def _plan(self, *, customer_dir: bool = False, customer_path: str | _path_plan_Path | None = None, task_code: str = "KRMB", iteration: int | str = 1):
        return _path_plan_build_path_plan(customer_dir=customer_dir, customer_path=customer_path, task_code=task_code, iteration=iteration, config=self.config, task_date=self.task_date)

    def _managed_executor_root(self, task_code: str, iteration: int) -> _path_plan_Path:
        task_id = f"{task_code}-{int(iteration):03d}"
        return self.workspace.resolve() / "tasks" / "artifacts" / self.task_date / task_code / task_id / "claude"

    def test_default_mode_canonical_executor_project_root(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        self.assertEqual(_path_plan_Path(workspace["executor_project_root"]), self._managed_executor_root("KRMB", 1))
        self.assertFalse(_path_plan_Path(workspace["executor_project_root"]).exists())
        _path_plan_validate_path_plan_workspace(workspace)

    def test_canonical_path_structure(self) -> None:
        root = _path_plan_Path(self._plan(task_code="ABCD", iteration=3).executor_project_root)
        self.assertEqual(root.name, "claude")
        self.assertEqual(root.parent.name, "ABCD-003")
        self.assertEqual(root.parent.parent.name, "ABCD")
        self.assertEqual(root.parent.parent.parent.name, self.task_date)
        self.assertEqual(root.parent.parent.parent.parent.name, "artifacts")
        self.assertEqual(root, _path_plan_canonical_executor_project_root(self.workspace.resolve(), self.task_date, "ABCD", "ABCD-003"))

    def test_customer_mode_preserves_customer_path_but_plans_managed_root(self) -> None:
        customer = self.root / "customer"
        customer.mkdir()
        plan = self._plan(customer_dir=True, customer_path=customer, task_code="KRMB", iteration=1)
        workspace = plan.to_workspace()
        self.assertEqual(_path_plan_Path(workspace["customer_path"]), customer.resolve())
        self.assertEqual(_path_plan_Path(workspace["project_root"]), customer.resolve())
        executor_root = _path_plan_Path(workspace["executor_project_root"])
        self.assertTrue(executor_root.is_relative_to(self.workspace.resolve() / "tasks" / "artifacts"))
        self.assertFalse(executor_root.is_relative_to(customer.resolve()))
        _path_plan_validate_path_plan_workspace(workspace)

    def test_iteration_isolation_uses_full_task_id(self) -> None:
        first = self._plan(task_code="KRMB", iteration=1)
        second = self._plan(task_code="KRMB", iteration=2)
        self.assertNotEqual(first.executor_project_root, second.executor_project_root)
        self.assertIn("KRMB-001", first.executor_project_root.parts)
        self.assertIn("KRMB-002", second.executor_project_root.parts)

    def test_iteration_roots_share_chain_dir_but_isolate_task_id(self) -> None:
        first = self._plan(task_code="KRMB", iteration=1)
        second = self._plan(task_code="KRMB", iteration=2)
        self.assertEqual(first.executor_project_root.parent.parent, second.executor_project_root.parent.parent)
        self.assertNotEqual(first.executor_project_root.parent, second.executor_project_root.parent)

    def test_branch_and_task_code_isolation(self) -> None:
        branch_a = self._plan(task_code="KRMB", iteration=1)
        branch_b = self._plan(task_code="KRMC", iteration=1)
        self.assertNotEqual(branch_a.executor_project_root, branch_b.executor_project_root)

    def test_validate_rejects_wrong_task_id_segment(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        parts = list(_path_plan_Path(workspace["executor_project_root"]).parts)
        parts[-2] = "KRMB-002"
        workspace["executor_project_root"] = str(_path_plan_Path(*parts))
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_wrong_date_segment(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        parts = list(_path_plan_Path(workspace["executor_project_root"]).parts)
        parts[parts.index(self.task_date)] = "2026-08-11"
        workspace["executor_project_root"] = str(_path_plan_Path(*parts))
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_wrong_task_code_segment(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        parts = list(_path_plan_Path(workspace["executor_project_root"]).parts)
        parts[-3] = "KRMC"
        workspace["executor_project_root"] = str(_path_plan_Path(*parts))
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_metadata_that_disagrees_with_path(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["task_code"] = "KRMC"
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["iteration"] = "002"
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_invalid_metadata_shape(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["iteration"] = "not-a-number"
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_path_outside_managed_artifacts(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["executor_project_root"] = str(self.workspace.resolve() / "tasks" / "report" / self.task_date / "KRMB" / "KRMB-001" / "claude")
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_path_under_customer_project(self) -> None:
        customer = self.root / "customer"
        customer.mkdir()
        workspace = self._plan(customer_dir=True, customer_path=customer, task_code="KRMB", iteration=1).to_workspace()
        workspace["executor_project_root"] = str(customer.resolve())
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_existing_parent_symlink_escape(self) -> None:
        date_dir = self.workspace / "tasks" / "artifacts" / self.task_date
        date_dir.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        date_dir.symlink_to(outside, target_is_directory=True)
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_validate_rejects_symlink_to_non_canonical_managed_branch(self) -> None:
        date_dir = self.workspace / "tasks" / "artifacts" / self.task_date
        date_dir.parent.mkdir(parents=True)
        alternative = self.workspace / "tasks" / "artifacts" / "other-date"
        alternative.mkdir()
        date_dir.symlink_to(alternative, target_is_directory=True)
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        with self.assertRaises(_path_plan_ABCError):
            _path_plan_validate_path_plan_workspace(workspace)

    def test_legacy_workspace_without_executor_root_still_validates(self) -> None:
        chain_root = self.workspace.resolve() / "tasks" / "artifacts" / self.task_date / "KRMB"
        report_root = self.workspace.resolve() / "tasks" / "report" / self.task_date / "KRMB"
        workspace = {
            "customer_dir": False,
            "customer_path": "",
            "project_root": str(chain_root),
            "default_path": str(chain_root),
            "agentbc_root": str(self.workspace.resolve()),
            "artifact_root": str(chain_root),
            "report_root": str(report_root),
            "task_file": str(report_root / "KRMB-001-task.md"),
            "report_file": str(report_root / "KRMB-001-report.md"),
            "task_code": "KRMB",
            "iteration": "001",
            "task_date": self.task_date,
        }
        _path_plan_validate_path_plan_workspace(workspace)
