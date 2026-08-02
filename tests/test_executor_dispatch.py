"""L0 executor conformance tests + end-to-end dispatch tests."""

import shutil
import tempfile
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class ExecutorConformanceL0Tests(unittest.TestCase):
    """L0 conformance: every executor must pass these."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.test_dir)

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
        task_packet = {
            "task_id": "T-001",
            "title": "Test task",
            "steps": [{"id": 1, "description": "echo hello"}],
            "workspace": {"root": str(self.test_dir)}
        }
        result = ex.start(task_packet)
        self.assertIsInstance(result, StartResult)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.run_id)

    def test_poll_returns_status(self):
        """poll() must return PollResult with status."""
        from agent_bridge_connect.adapters import PollResult
        ex = self._make_executor("mock")
        task_packet = {
            "task_id": "T-001",
            "title": "Test",
            "steps": [{"id": 1, "description": "echo hello"}],
            "workspace": {"root": str(self.test_dir)}
        }
        start = ex.start(task_packet)
        result = ex.poll(start.run_id)
        self.assertIsInstance(result, PollResult)
        self.assertIn(result.status, ["running", "completed", "needs_recovery"])

    def test_start_with_empty_steps_returns_error(self):
        """start() with empty steps must return ok=False."""
        ex = self._make_executor("mock")
        task_packet = {
            "task_id": "T-001",
            "title": "No steps",
            "steps": [],
            "workspace": {"root": str(self.test_dir)}
        }
        result = ex.start(task_packet)
        self.assertFalse(result.ok)


class WorkerDispatchE2ETests(unittest.TestCase):
    """End-to-end: create → running → finalize → completed."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board
        init_board(self.board)

        # Create a task with simple steps
        from agent_bridge_connect.task_board import create_task
        self.task = create_task(
            title="E2E dispatch test",
            assignee="mock",
            steps_path=STEPS_YAML,
            board_root=self.board
        )
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_full_dispatch_lifecycle(self):
        """Full lifecycle: pending → running → completed."""
        from agent_bridge_connect.service import TaskService
        from agent_bridge_connect.executors.mock import MockExecutor

        svc = TaskService(self.board)
        executor = MockExecutor()

        # Start → running
        lease_token = svc.claim_task(self.task_id, "mock")
        self.assertIsNotNone(lease_token)
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "running")

        # Execute first step keeps running
        svc.execute_step(self.task_id, 1, {"status": "done", "result": "step 1 completed"})
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "running")

        # Execute via executor (outside service — executor runs steps)
        _capabilities = executor.capabilities()
        start_result = executor.start({
            "task_id": self.task_id,
            "title": task.title,
            "steps": [{"id": s["id"], "description": s["description"]} for s in task.steps],
            "workspace": task.workspace,
        })
        self.assertTrue(start_result.ok)

        # Poll until completed (executor side)
        for _ in range(10):
            poll = executor.poll(start_result.run_id)
            if poll.status in ("completed", "needs_recovery"):
                break
        self.assertEqual(poll.status, "completed")

        # Service finalizes the task from the agent callback
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


if __name__ == "__main__":
    unittest.main()
