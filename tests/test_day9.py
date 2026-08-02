"""Day 9 tests: agentbc setup + Codex natural-language integration."""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SetupCommandTests(unittest.TestCase):
    """Test agentbc setup command."""

    def test_setup_subcommand_exists(self):
        """agentbc setup should be a valid subcommand."""
        import subprocess
        r = subprocess.run(
            ["python3", "-m", "agent_bridge_connect.cli", "setup", "--help"],
            capture_output=True, text=True
        )
        # Should not crash with "invalid choice"
        self.assertNotIn("invalid choice", r.stderr.lower() + r.stdout.lower())

    def test_codex_desktop_candidates_include_current_and_legacy_apps(self):
        from agent_bridge_connect.path_provider import _macos_candidates

        candidates = dict((str(path), source) for path, source in _macos_candidates("codex"))
        self.assertEqual(
            candidates["/Applications/ChatGPT.app/Contents/Resources/codex"],
            "chatgpt_desktop",
        )
        self.assertEqual(
            candidates["/Applications/Codex.app/Contents/Resources/codex"],
            "codex_desktop_legacy",
        )

    def test_setup_discover_codex(self):
        """Setup should discover codex binary path and version."""
        from agent_bridge_connect.setup import discover_codex
        result = discover_codex()
        self.assertIsNotNone(result)
        self.assertIn("path", result)
        self.assertIn("version", result)

    def test_setup_probe_codex_capabilities(self):
        """Setup should probe Codex capabilities."""
        from agent_bridge_connect.setup import probe_codex
        result = probe_codex()
        self.assertIsNotNone(result)
        self.assertIn("has_json_output", result)
        self.assertIn("has_sandbox", result)

    def test_setup_creates_config(self):
        """Setup should create ~/.abc/config.toml with codex executor."""
        from agent_bridge_connect.setup import generate_default_config
        config = generate_default_config()
        self.assertIn("workspace_root", config)
        self.assertIn("executors", config)
        self.assertIn("codex", config["executors"])
        self.assertEqual(config["executors"]["codex"]["type"], "codex")

    def test_discovers_opencode_from_npm_global_home(self):
        """Setup should find npm-installed tools even when they are not on PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            bin_dir = fake_home / ".npm-global" / "bin"
            bin_dir.mkdir(parents=True)
            opencode = bin_dir / "opencode"
            opencode.write_text("#!/bin/sh\necho opencode 1.2.3\n", encoding="utf-8")
            opencode.chmod(0o755)

            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "PATH": ""}, clear=False):
                from agent_bridge_connect.setup import discover_opencode
                result = discover_opencode()

            self.assertTrue(result["found"])
            self.assertEqual(Path(result["path"]).resolve(), opencode.resolve())
            self.assertEqual(result["source"], "npm")
            self.assertIn("1.2.3", result["version"])

    def test_command_env_override_for_nonstandard_paths(self):
        """Users should be able to point setup at a manually installed binary."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "tools" / "opencode"
            custom.parent.mkdir()
            custom.write_text("#!/bin/sh\necho opencode custom\n", encoding="utf-8")
            custom.chmod(0o755)

            with mock.patch.dict(os.environ, {"AGENTBC_OPENCODE_BIN": str(custom), "PATH": ""}, clear=False):
                from agent_bridge_connect.setup import discover_opencode
                result = discover_opencode()

            self.assertTrue(result["found"])
            self.assertEqual(Path(result["path"]).resolve(), custom.resolve())
            self.assertEqual(result["source"], "env_override")


class NaturalLanguageTests(unittest.TestCase):
    """Test natural-language task operations."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board, create_task
        init_board(self.test_dir)
        self.task = create_task(
            "NL test",
            "mock",
            Path("tests/fixtures/sample_steps.yaml"),
            self.test_dir,
        )
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_shorthand_create(self):
        """agentbc 'description' should create a task."""
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.test_dir, config={"workspace_root": str(self.test_dir)})
        # Simulate: agentbc "Write unit tests"
        task = svc.create_task("Write unit tests", "codex",
                               [{"id": 1, "description": "Write unit tests for auth module"}],
                               customer_dir=False)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.assignee, "codex")

    def test_shorthand_status(self):
        """agentbc <task-id> should show task status."""
        from agent_bridge_connect.service import TaskService
        svc = TaskService(self.test_dir)
        task = svc.get_task(self.task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.id, self.task_id)

    def test_list_alias_expands_to_task_list(self):
        from agent_bridge_connect.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["list", "--root", str(self.test_dir)])
        self.assertEqual(code, 0)
        self.assertIn(self.task_id.split("-")[0], output.getvalue())

    def test_single_word_typo_is_rejected_instead_of_creating_task(self):
        from agent_bridge_connect.cli import main
        from agent_bridge_connect.service import TaskService

        before = len(TaskService(self.test_dir).list_tasks())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["satus", "--root", str(self.test_dir)])
        after = len(TaskService(self.test_dir).list_tasks())
        self.assertEqual(code, 1)
        self.assertEqual(after, before)
        self.assertIn("unknown command", output.getvalue().lower())

    def test_context_free_review(self):
        """A new session should be able to review a task by ID only."""
        from agent_bridge_connect.service import TaskService
        from agent_bridge_connect.reports import generate_task_brief
        _service = TaskService(self.test_dir)
        brief = generate_task_brief(self.task_id, self.test_dir)
        # Brief should be self-contained
        self.assertIn("task_id", brief)
        self.assertIn("objective", brief)
        self.assertIn("status", brief)


if __name__ == "__main__":
    unittest.main()
