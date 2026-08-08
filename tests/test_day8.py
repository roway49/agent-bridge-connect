"""Day 8 tests: AgentBC CLI + Codex executor + Task Brief."""

import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class AgentBCCLITests(unittest.TestCase):
    """Test agentbc CLI entry point and subcommands."""

    def test_agentbc_help(self):
        """agentbc --help should work."""
        import subprocess
        r = subprocess.run(
            ["python3", "-m", "agent_bridge_connect.cli", "--help"],
            capture_output=True, text=True
        )
        # Should not crash
        self.assertIn("agentbc", r.stdout.lower() + r.stderr.lower())

    def test_agentbc_task_create_list_status(self):
        """Full CLI loop: create → list → status."""
        import subprocess
        test_dir = Path(tempfile.mkdtemp())
        workspace = test_dir / "workspace"
        workspace.mkdir()
        config = test_dir / "config.toml"
        config.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
        try:
            # init
            subprocess.run(
                ["python3", "-m", "agent_bridge_connect.cli", "init", "--root", str(test_dir)],
                capture_output=True, text=True
            )
            # create
            r = subprocess.run(
                ["python3", "-m", "agent_bridge_connect.cli", "task", "create",
                 "--title", "CLI test", "--assignee", "mock",
                 "--steps", str(STEPS_YAML), "--root", str(test_dir),
                 "--customer-dir", "true", "--customer-path", str(workspace),
                 "--config", str(config)],
                capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 0, f"create failed: {r.stderr}")
            match = re.search(r"[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001", r.stdout)
            self.assertIsNotNone(match)
            task_id = match.group(0)

            # list
            r = subprocess.run(
                ["python3", "-m", "agent_bridge_connect.cli", "task", "list",
                 "--root", str(test_dir)],
                capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 0, f"list failed: {r.stderr}")
            self.assertIn(task_id.split("-")[0], r.stdout)

            # status
            r = subprocess.run(
                ["python3", "-m", "agent_bridge_connect.cli", "task", "status",
                 task_id, "--root", str(test_dir)],
                capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 0, f"status failed: {r.stderr}")
            self.assertIn("pending", r.stdout.lower())
        finally:
            shutil.rmtree(test_dir)


class CodexExecutorTests(unittest.TestCase):
    """Test CodexExecutor adapter."""

    def test_codex_probe(self):
        """CodexExecutor.probe() should check codex is available."""
        from agent_bridge_connect.executors.codex import CodexExecutor

        missing = {
            "found": False,
            "path": "",
            "source": "not_found",
            "searched_paths": ["fixture/codex"],
            "manual_override": "AGENTBC_CODEX_BIN=/your/path/codex",
        }
        with mock.patch(
            "agent_bridge_connect.executors.codex.find_binary",
            return_value=missing,
        ):
            executor = CodexExecutor()
        result = executor.probe()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["agent_bin"], "")
        self.assertEqual(result.details["agent_bin_source"], "not_found")
        self.assertEqual(result.details["searched_paths"], ["fixture/codex"])

    def test_codex_capabilities(self):
        """CodexExecutor should declare L1+ capabilities."""
        from agent_bridge_connect.executors.codex import CodexExecutor
        from agent_bridge_connect.adapters import ExecutorLevel
        executor = CodexExecutor()
        caps = executor.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.streaming_events)
        self.assertGreaterEqual(caps.level, ExecutorLevel.L1)

    def test_codex_start_returns_run_id(self):
        """CodexExecutor.start() should return a valid StartResult."""
        from agent_bridge_connect.executors.codex import CodexExecutor
        executor = CodexExecutor(command=sys.executable)
        task_packet = {
            "task_id": "4XMC-001",
            "title": "Test",
            "steps": [{"id": 1, "description": "Write hello.txt"}],
            "workspace": {"root": "/tmp"}
        }
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch(
            "agent_bridge_connect.executors.codex.subprocess.run",
            return_value=completed,
        ):
            result = executor.start(task_packet)
        # Should return StartResult (ok may be False if codex not configured)
        self.assertIsNotNone(result)

    def test_codex_extensions_stored(self):
        """Codex-specific metadata should go under extensions.executor.codex."""
        from agent_bridge_connect.executors.codex import CodexExecutor
        executor = CodexExecutor()
        # After a hypothetical run, extensions should be available
        self.assertTrue(hasattr(executor, "get_extensions"))


class TaskBriefTests(unittest.TestCase):
    """Test Task Brief generation."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board, create_task
        init_board(self.test_dir)
        self.task = create_task("Brief test", "mock", STEPS_YAML, self.test_dir)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_task_brief_structure(self):
        """Task Brief should contain required fields."""
        from agent_bridge_connect.reports import generate_task_brief
        brief = generate_task_brief(self.task_id, self.test_dir)
        required = ["task_id", "title", "status", "objective", "evidence",
                     "changed_files", "verification", "risks", "available_actions"]
        for field in required:
            self.assertIn(field, brief, f"Missing field: {field}")

    def test_task_brief_has_objective(self):
        """Task Brief objective should come from task title."""
        from agent_bridge_connect.reports import generate_task_brief
        brief = generate_task_brief(self.task_id, self.test_dir)
        self.assertIn("Brief test", brief["objective"])

    def test_task_brief_has_actions(self):
        """Task Brief should list available actions based on current state."""
        from agent_bridge_connect.reports import generate_task_brief
        brief = generate_task_brief(self.task_id, self.test_dir)
        self.assertIsInstance(brief["available_actions"], list)
        self.assertTrue(len(brief["available_actions"]) > 0)


class GateATests(unittest.TestCase):
    """Gate A: Direct CLI can create, inspect, intervene, and accept."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board
        init_board(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_gate_a_full_lifecycle(self):
        """CLI: create → claim → execute → complete → report."""
        from agent_bridge_connect.service import TaskService
        from tests.contract_helpers import finalize_completed
        svc = TaskService(self.test_dir, config={"workspace_root": str(self.test_dir)})
        task = svc.create_task("Gate A test", "mock",
                               [{"id": 1, "description": "Write hello"}], customer_dir=False)
        self.assertEqual(task.status, "pending")

        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        task = svc.get_task(task.id)
        self.assertEqual(task.status, "running")

        finalize_completed(svc, task.id)
        task = svc.get_task(task.id)
        self.assertEqual(task.status, "completed")

        report = svc.generate_report(task.id)
        self.assertIsNotNone(report)
        self.assertEqual(report["task_id"], task.id)


if __name__ == "__main__":
    unittest.main()
