from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_bridge_connect.execution_policy import extract_hermes_session_id
from agent_bridge_connect.executors.hermes import _iteration_budget_diagnostics


FIXTURES = Path(__file__).parent / "fixtures" / "executor_runtime"


class ExecutorContractFixtureTests(unittest.TestCase):
    def test_claude_2_1_226_capability_snapshot(self) -> None:
        text = (FIXTURES / "claude_2.1.226_help.txt").read_text(encoding="utf-8")
        for flag in (
            "--max-budget-usd",
            "--no-session-persistence",
            "--resume",
            "--session-id",
            "project purge",
            "--yes",
        ):
            self.assertIn(flag, text)

    def test_claude_budget_exhaustion_snapshot_is_not_mislabeled_live(self) -> None:
        payload = json.loads(
            (FIXTURES / "claude_budget_exhaustion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["structured_subtype"], "error_max_budget_usd")
        self.assertFalse(payload["verified_by_live_paid_canary"])

    def test_hermes_0_17_0_capability_snapshot(self) -> None:
        text = (FIXTURES / "hermes_0.17.0_help.txt").read_text(encoding="utf-8")
        self.assertIn("--max-turns", text)
        self.assertIn("--resume", text)
        self.assertIn("sessions delete", text)

    def test_hermes_session_receipt_and_exhaustion_snapshots(self) -> None:
        payload = json.loads(
            (FIXTURES / "hermes_outputs.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            extract_hermes_session_id(payload["session_receipt_stderr"]),
            "20260810_010203_a1b2c3",
        )
        for output in payload["iteration_exhaustion"]:
            with self.subTest(output=output):
                diagnostics = _iteration_budget_diagnostics(output, "")
                self.assertTrue(diagnostics["iteration_exhausted"])


if __name__ == "__main__":
    unittest.main()
