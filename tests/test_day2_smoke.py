"""Minimal Day 0-2 smoke test for the abc task board."""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from agent_bridge_connect.cli import main
from agent_bridge_connect.schema import validate_task


FIXTURES = Path(__file__).parent / "fixtures"


class Day2SmokeTests(unittest.TestCase):
    def test_init_create_list_and_disk_protocol(self) -> None:
        """Verify the smallest useful Day 0-2 CLI workflow."""
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "abc-tasks"
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            config = Path(tmp) / "config.toml"
            config.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
            steps = FIXTURES / "sample_steps.yaml"

            setup_out = StringIO()
            with contextlib.redirect_stdout(setup_out):
                self.assertEqual(main(["init", "--root", str(board)]), 0)
                self.assertEqual(main([
                    "task", "create",
                    "--root", str(board),
                    "--title", "Smoke codex task",
                    "--assignee", "codex",
                    "--steps", str(steps),
                    "--session-id", "smoke-session-001",
                    "--customer-dir", "true",
                    "--customer-path", str(workspace),
                    "--config", str(config),
                ]), 0)
                self.assertEqual(main([
                    "task", "create",
                    "--root", str(board),
                    "--title", "Smoke claude task",
                    "--assignee", "claude",
                    "--steps", str(steps),
                    "--customer-dir", "true",
                    "--customer-path", str(workspace),
                    "--config", str(config),
                ]), 0)
            self.assertTrue(board.exists())
            self.assertTrue((board / "agents.yaml").exists())

            all_out = StringIO()
            with contextlib.redirect_stdout(all_out):
                self.assertEqual(main(["task", "list", "--root", str(board)]), 0)
            all_text = all_out.getvalue()
            codex_match = re.search(r"([23456789ABCDEFGHJKMNPQRSTVWXYZ]{4})-001\t001\t[^\t]+ -> codex\t", all_text)
            claude_match = re.search(r"([23456789ABCDEFGHJKMNPQRSTVWXYZ]{4})-001\t001\t[^\t]+ -> claude\t", all_text)
            self.assertIsNotNone(codex_match)
            self.assertIsNotNone(claude_match)
            codex_id = f"{codex_match.group(1)}-001"
            claude_id = f"{claude_match.group(1)}-001"
            self.assertNotEqual(codex_id, claude_id)
            self.assertIn("Smoke claude task", all_out.getvalue())
            self.assertIn("Smoke codex task", all_out.getvalue())

            codex_out = StringIO()
            with contextlib.redirect_stdout(codex_out):
                self.assertEqual(main([
                    "task", "list",
                    "--root", str(board),
                    "--assignee", "codex",
                ]), 0)
            self.assertRegex(codex_out.getvalue(), rf"{codex_id}\t001\t[^\t]+ -> codex\t")
            self.assertIn("Smoke codex task", codex_out.getvalue())
            self.assertNotIn("claude", codex_out.getvalue())

            task_path = board / codex_id.split("-")[0] / "001" / "task.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            self.assertEqual(validate_task(task), [])
            self.assertEqual(task["session_id"], "smoke-session-001")
            self.assertEqual([step["record"] for step in task["steps"]], [
                "steps/01.json", "steps/02.json", "steps/03.json",
            ])
            for index in range(1, 4):
                self.assertTrue((task_path.parent / "steps" / f"{index:02d}.json").exists())


if __name__ == "__main__":
    unittest.main()
