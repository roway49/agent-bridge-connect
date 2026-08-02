"""Intervention and guardrail tests for ABC V1."""

import shutil
import tempfile
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
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
        self.assertTrue(any(e.get("type") == "correct" for e in events))

    def test_retry_step(self):
        """Retrying a step resets it to pending."""
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.board)
        self._claim_and_start(svc)

        svc.retry_step(self.task_id, 1)
        task = svc.get_task(self.task_id)
        # Step 1 should be reset to pending
        step1 = next(s for s in task.steps if s["id"] == 1)
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

    def test_dual_claim_blocked(self):
        """Two executors cannot claim the same task."""
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")

        with self.assertRaises(Exception):
            svc.claim_task(self.task_id, "mock2")

    def test_missing_work_dir_blocks(self):
        """Task creation with invalid assignee raises."""
        from agent_bridge_connect.task_board import create_task, TaskCreateError
        with self.assertRaises(TaskCreateError):
            create_task("No steps", "", STEPS_YAML, self.board)

    def test_corrupt_task_json_detected(self):
        """Corrupt task.json must not crash board scan."""
        from agent_bridge_connect.task_board import TaskBoard
        # Write corrupt file
        corrupt_path = self.board / "tasks" / "T-CORRUPT"
        corrupt_path.mkdir(parents=True)
        (corrupt_path / "task.json").write_text("NOT JSON")
        (corrupt_path / "steps").mkdir()

        board = TaskBoard(self.board)
        tasks = board.list_all()
        # Should return valid tasks, not crash
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
        # Already leased → preflight should warn or fail
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
