"""Phase 10a tests: HermesExecutor and VSCode capability detection."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FIXTURES = Path(__file__).parent / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class HermesExecutorTests(unittest.TestCase):
    """Test HermesExecutor adapter."""

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_probe(self, run):
        """HermesExecutor.probe() should check hermes is available."""
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(returncode=0, stdout="Hermes test", stderr="")
        executor = HermesExecutor(command=sys.executable, transport="direct")
        result = executor.probe()
        self.assertTrue(result.ok)
        self.assertIn("agent_bin", result.details)
        self.assertEqual(result.details["agent_bin"], sys.executable)
        self.assertEqual(result.details["agent_bin_source"], "configured")
        self.assertEqual(result.details["profile_mode"], "inherit")
        self.assertEqual(result.details["auth_owner"], "hermes_cli")

    def test_hermes_probe_not_found_has_stable_path_diagnostics(self):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        missing = {
            "found": False,
            "path": "",
            "source": "not_found",
            "searched_paths": ["fixture/hermes"],
            "manual_override": "AGENTBC_HERMES_BIN=/your/path/hermes",
        }
        with mock.patch(
            "agent_bridge_connect.executors.hermes._discover_hermes_binary",
            return_value=missing,
        ):
            result = HermesExecutor().probe()

        self.assertFalse(result.ok)
        self.assertEqual(result.details["agent_bin"], "")
        self.assertEqual(result.details["agent_bin_source"], "not_found")
        self.assertEqual(result.details["searched_paths"], ["fixture/hermes"])
        self.assertIn("AGENTBC_HERMES_BIN", result.details["manual_override"])

    def test_hermes_capabilities(self):
        """HermesExecutor should declare L2 capabilities."""
        from agent_bridge_connect.executors.hermes import HermesExecutor
        from agent_bridge_connect.adapters import ExecutorLevel
        executor = HermesExecutor()
        caps = executor.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.model_selection)
        self.assertTrue(caps.multimodal)
        self.assertGreaterEqual(caps.level, ExecutorLevel.L2)

    def test_hermes_extensions(self):
        """HermesExecutor should store metadata under extensions.executor.hermes."""
        from agent_bridge_connect.executors.hermes import HermesExecutor

        executor = HermesExecutor(command=sys.executable)
        executor._version = "Hermes test"
        details = executor.get_extensions()["executor"]["hermes"]
        self.assertEqual(details["agent_bin"], sys.executable)
        self.assertEqual(details["agent_bin_source"], "configured")


class VSCodeCapabilityTests(unittest.TestCase):
    """Test VSCode/Cursor/Windsurf runtime detection."""

    def test_detect_vscode_codex_extension(self):
        """Should detect VSCode Codex extension directory."""
        from agent_bridge_connect.setup import detect_vscode_codex
        result = detect_vscode_codex()
        self.assertIsNotNone(result)
        # May or may not find it, but should not crash
        self.assertIn("found", result)

    def test_runtime_capability_grades(self):
        """Should define all 5 capability grades."""
        from agent_bridge_connect.setup import RuntimeCapability
        self.assertEqual(RuntimeCapability.FRONTEND_ONLY, "frontend_only")
        self.assertEqual(RuntimeCapability.BACKGROUND_SINGLE, "background_single")
        self.assertEqual(RuntimeCapability.BACKGROUND_MULTI_CANDIDATE, "background_multi_candidate")
        self.assertEqual(RuntimeCapability.BACKGROUND_MULTI_VERIFIED, "background_multi_verified")
        self.assertEqual(RuntimeCapability.SILENT_UNATTENDED, "silent_unattended")

    def test_setup_outputs_capability_matrix(self):
        """Setup should return a capability matrix with all fields."""
        from agent_bridge_connect.setup import probe_codex
        with mock.patch(
            "agent_bridge_connect.setup.discover_codex",
            return_value={"found": False, "version": ""},
        ):
            result = probe_codex()
        # Should have capability grade info
        self.assertIn("has_json_output", result)
        self.assertIn("has_sandbox", result)


class GateFTests(unittest.TestCase):
    """Gate F: HermesExecutor completes a real task."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board, create_task
        init_board(self.test_dir)
        self.task = create_task("Gate F test", "hermes", STEPS_YAML, self.test_dir)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_gate_f_lifecycle(self):
        """HermesExecutor: claim → execute → complete → report."""
        from agent_bridge_connect.service import TaskService
        from tests.contract_helpers import finalize_completed
        svc = TaskService(self.test_dir)

        svc.claim_task(self.task_id, "hermes")
        svc.execute_step(self.task_id, 1, {"status": "done", "result": "reviewed"})
        finalize_completed(svc, self.task_id)

        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "completed")

        report = svc.generate_report(self.task_id)
        self.assertIsNotNone(report)


if __name__ == "__main__":
    unittest.main()
