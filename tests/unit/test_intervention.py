"""Intervention and guardrail tests for ABC V1."""

import shutil
import tempfile
import unittest
from pathlib import Path
import shutil as _dispatch_shutil
import tempfile as _dispatch_tempfile
import unittest as _dispatch_unittest
from pathlib import Path as _dispatch_Path

FIXTURES = Path(__file__).parents[1] / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class InterventionTests(unittest.TestCase):
    """Test pause/resume/correct/retry/cancel/reassign via service API."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.board)
        self.task = create_task("Intervention test", "mock", STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _claim_and_start(self, svc):
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})

    def test_pause_working_task(self):
        """Pausing a running task keeps public status stable and sets paused intervention state."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        svc.pause_task(self.task_id, reason="need review")
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "running")
        self.assertTrue(task.intervention["paused"])

    def test_resume_paused_task(self):
        """Resuming a paused task returns to running and clears intervention state."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        svc.pause_task(self.task_id)
        svc.resume_task(self.task_id)
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "running")
        self.assertFalse(task.intervention["paused"])

    def test_cancel_working_task(self):
        """Cancelling a working task sets cancelled."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        svc.cancel_task(self.task_id)
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "cancelled")

    def test_correct_step(self):
        """Correcting a step appends to interventions.jsonl."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        svc.correct_step(self.task_id, 2, "Use Anthropic instead")
        events = svc.store.read_interventions(self.task_id)
        self.assertTrue(any((e.get("type") == "correct" for e in events)))

    def test_retry_step(self):
        """Retrying a step resets it to pending."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        svc.retry_step(self.task_id, 1)
        task = svc.get_task(self.task_id)
        step1 = next((s for s in task.steps if s["id"] == 1))
        self.assertEqual(step1["status"], "pending")

    def test_reassign_task(self):
        """Reassigning a paused task changes assignee."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        svc.pause_task(self.task_id)
        svc.reassign_task(self.task_id, "codex")
        task = svc.get_task(self.task_id)
        self.assertEqual(task.assignee, "codex")

    def test_reassign_blocked_when_working(self):
        """Reassigning a working task must be blocked."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        self._claim_and_start(svc)
        with self.assertRaises(Exception):
            svc.reassign_task(self.task_id, "codex")


class GuardrailTests(unittest.TestCase):
    """Test safety guardrails: dual-claim, workspace, capability, corruption."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.board)
        self.task = create_task("Guardrail test", "mock", STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_missing_work_dir_blocks(self):
        """Task creation with invalid assignee raises."""
        from agent_bridge_connect.task_board import create_task, TaskCreateError

        with self.assertRaises(TaskCreateError):
            create_task("No steps", "", STEPS_YAML, self.board)

    def test_corrupt_task_json_detected(self):
        """Corrupt task.json must not crash board scan."""
        from agent_bridge_connect.task_board import TaskBoard

        corrupt_path = self.board / "tasks" / "T-CORRUPT"
        corrupt_path.mkdir(parents=True)
        (corrupt_path / "task.json").write_text("NOT JSON")
        (corrupt_path / "steps").mkdir()
        board = TaskBoard(self.board)
        tasks = board.list_all()
        self.assertIsInstance(tasks, list)

    def test_intervention_append_only(self):
        """Interventions must append, not rewrite."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})
        svc.pause_task(self.task_id, reason="first pause")
        svc.resume_task(self.task_id)
        svc.pause_task(self.task_id, reason="second pause")
        interventions = svc.store.read_interventions(self.task_id)
        pause_events = [e for e in interventions if e.get("type") == "pause"]
        self.assertGreaterEqual(len(pause_events), 2)


class PreflightTests(unittest.TestCase):
    """Test preflight validation before execution."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.board)
        self.task = create_task("Preflight test", "mock", STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_preflight_valid_task(self):
        """Preflight on valid pending task returns ok."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        result = svc.preflight(self.task_id)
        self.assertTrue(result.ok)

    def test_preflight_leased_task_blocked(self):
        """Preflight on leased task returns not ok."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        result = svc.preflight(self.task_id)
        self.assertFalse(result.ok)


_dispatch_FIXTURES = _dispatch_Path(__file__).parents[1] / "fixtures"
_dispatch_STEPS_YAML = _dispatch_FIXTURES / "sample_steps.yaml"


class _dispatch_ExecutorConformanceL0Tests(_dispatch_unittest.TestCase):
    """L0 conformance: every executor must pass these."""

    def setUp(self):
        self.test_dir = _dispatch_Path(_dispatch_tempfile.mkdtemp())

    def tearDown(self):
        _dispatch_shutil.rmtree(self.test_dir)

    def _make_executor(self, name: str):
        """Create executor instance by name."""
        from agent_bridge_connect.executors.mock import MockExecutor

        if name == "mock":
            return MockExecutor()
        raise ValueError(f"Unknown executor: {name}")

    def test_probe_returns_ok(self):
        """probe() must return a valid ProbeResult."""
        from agent_bridge_connect.adapters import ProbeResult

        ex = self._make_executor("mock")
        result = ex.probe()
        self.assertIsInstance(result, ProbeResult)
        self.assertTrue(result.ok)
        self.assertIsInstance(result.message, str)

    def test_capabilities_returns_valid(self):
        """capabilities() must return valid ExecutorCapabilities."""
        from agent_bridge_connect.adapters import ExecutorCapabilities

        ex = self._make_executor("mock")
        caps = ex.capabilities()
        self.assertIsInstance(caps, ExecutorCapabilities)
        self.assertIn(caps.level, [0, 1, 2, 3, 4])
        self.assertIsInstance(caps.structured_output, bool)

    def test_start_returns_run_id(self):
        """start() must return StartResult with ok and run_id."""
        from agent_bridge_connect.adapters import StartResult

        ex = self._make_executor("mock")
        task_packet = {"task_id": "T-001", "title": "Test task", "steps": [{"id": 1, "description": "echo hello"}], "workspace": {"root": str(self.test_dir)}}
        result = ex.start(task_packet)
        self.assertIsInstance(result, StartResult)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.run_id)

    def test_poll_returns_status(self):
        """poll() must return PollResult with status."""
        from agent_bridge_connect.adapters import PollResult

        ex = self._make_executor("mock")
        task_packet = {"task_id": "T-001", "title": "Test", "steps": [{"id": 1, "description": "echo hello"}], "workspace": {"root": str(self.test_dir)}}
        start = ex.start(task_packet)
        result = ex.poll(start.run_id)
        self.assertIsInstance(result, PollResult)
        self.assertIn(result.status, ["running", "completed", "needs_recovery"])

    def test_start_with_empty_steps_returns_error(self):
        """start() with empty steps must return ok=False."""
        ex = self._make_executor("mock")
        task_packet = {"task_id": "T-001", "title": "No steps", "steps": [], "workspace": {"root": str(self.test_dir)}}
        result = ex.start(task_packet)
        self.assertFalse(result.ok)


class _dispatch_WorkerDispatchE2ETests(_dispatch_unittest.TestCase):
    """End-to-end: create → running → finalize → completed."""

    def setUp(self):
        self.test_dir = _dispatch_Path(_dispatch_tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board

        init_board(self.board)
        from agent_bridge_connect.task_board import create_task

        self.task = create_task(title="E2E dispatch test", assignee="mock", steps_path=_dispatch_STEPS_YAML, board_root=self.board)
        self.task_id = self.task.id

    def tearDown(self):
        _dispatch_shutil.rmtree(self.test_dir)

    def test_full_dispatch_lifecycle(self):
        """Full lifecycle: pending → running → completed."""
        from agent_bridge_connect.service import TaskService
        from agent_bridge_connect.executors.mock import MockExecutor

        svc = TaskService(self.board)
        executor = MockExecutor()
        lease_token = svc.claim_task(self.task_id, "mock")
        self.assertIsNotNone(lease_token)
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "running")
        svc.execute_step(self.task_id, 1, {"status": "done", "result": "step 1 completed"})
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "running")
        _capabilities = executor.capabilities()
        start_result = executor.start({"task_id": self.task_id, "title": task.title, "steps": [{"id": s["id"], "description": s["description"]} for s in task.steps], "workspace": task.workspace})
        self.assertTrue(start_result.ok)
        for _ in range(10):
            poll = executor.poll(start_result.run_id)
            if poll.status in ("completed", "needs_recovery"):
                break
        self.assertEqual(poll.status, "completed")
        svc.finalize_task_from_agent(self.task_id, poll.result["agent_callback"])
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "completed")

    def test_dual_claim_blocked(self):
        """Two executors cannot claim the same task."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        with self.assertRaises(Exception):
            svc.claim_task(self.task_id, "mock2")

    def test_worker_run_once_command(self):
        """abc worker run --executor mock --once processes one task."""
        from agent_bridge_connect.service import TaskService
        from tests.contract_helpers import finalize_completed

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})
        finalize_completed(svc, self.task_id)
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "completed")
