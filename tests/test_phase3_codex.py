from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_policy import build_session_snapshot
from agent_bridge_connect.executors.codex import (
    CodexExecutor,
    _execution_session_receipt,
    _extract_codex_session_id,
    _parse_jsonl,
)
from agent_bridge_connect.permission_modes import build_permission_record


FIXTURE = Path(__file__).parent / "fixtures" / "executor_runtime" / "codex_outputs.json"


class CodexSessionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.image = self.root / "input.png"
        self.image.write_bytes(b"fixture")
        self.executor = CodexExecutor(command=sys.executable, transport="cli")

    def _packet(self, *, session_id: str = "", run_ids: list[str] | None = None) -> dict:
        session = build_session_snapshot(
            "codex",
            retain=False,
            session_id=session_id,
            session_state="active" if session_id else "pending",
            run_ids=run_ids,
        )
        return {
            "task_id": "CDEX-001",
            "title": "Codex session",
            "steps": [{"id": 1, "description": "continue"}],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.root / "record")},
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                "agentbc.session": session,
                "agentbc.media": {"images": [str(self.image)]},
            },
        }

    def test_fresh_command_is_persistent_and_does_not_select_a_session(self) -> None:
        command, prompt_input = self.executor._build_command(
            self._packet(), "fresh prompt", self.root
        )

        self.assertEqual(command[:3], [str(self.executor.agent_bin), "exec", "--json"])
        self.assertNotIn("resume", command)
        self.assertNotIn("--last", command)
        self.assertNotIn("--ephemeral", command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertIn("--add-dir", command)
        self.assertEqual(command[-1], "-")
        self.assertEqual(prompt_input, "fresh prompt")

    def test_resume_command_uses_only_the_explicit_frozen_session_id(self) -> None:
        session_id = "019fe9f4-6306-7d31-9dca-11b67ee67701"
        command, prompt_input = self.executor._build_command(
            self._packet(session_id=session_id, run_ids=["codex-CDEX-001-first"]),
            "resume prompt",
            self.root,
        )

        resume_index = command.index("resume")
        self.assertEqual(command[resume_index + 1], session_id)
        self.assertEqual(command.count("resume"), 1)
        self.assertNotIn("--last", command)
        self.assertNotIn("--ephemeral", command)
        self.assertLess(command.index("--sandbox"), resume_index)
        self.assertLess(command.index("--add-dir"), resume_index)
        self.assertIn("--image", command[resume_index + 2 :])
        self.assertEqual(command[-1], "-")
        self.assertEqual(prompt_input, "resume prompt")

        packet_without_image = self._packet(
            session_id=session_id,
            run_ids=["codex-CDEX-001-first"],
        )
        packet_without_image["extensions"].pop("agentbc.media")
        command, prompt_input = self.executor._build_command(
            packet_without_image,
            "text-only resume",
            self.root,
        )
        self.assertEqual(command[-1], "text-only resume")
        self.assertIsNone(prompt_input)

    def test_resume_without_a_session_id_fails_before_process_start(self) -> None:
        packet = self._packet()
        packet["extensions"]["agentbc.session"]["run_ids"] = ["prior-run"]

        with (
            mock.patch.object(self.executor, "_start_run_lease"),
            mock.patch.object(self.executor, "_close_run_lease"),
            mock.patch("agent_bridge_connect.executors.codex.subprocess.run") as run,
        ):
            result = self.executor.start(packet)

        self.assertFalse(result.ok)
        self.assertIn("requires an explicit task session ID", result.message)
        run.assert_not_called()


class CodexSessionReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_extracts_only_one_well_formed_thread_started_receipt(self) -> None:
        events = _parse_jsonl(self.fixture["unique_receipt_jsonl"])
        self.assertEqual(_extract_codex_session_id(events), self.fixture["thread_id"])
        self.assertEqual(
            _execution_session_receipt(events, resumed=False),
            {
                "version": 1,
                "executor": "codex",
                "session_id": self.fixture["thread_id"],
                "resumed": False,
                "persistence": "persistent",
                "source": "jsonl_thread_started",
            },
        )

    def test_missing_or_duplicate_thread_started_receipt_is_not_emitted(self) -> None:
        for key in ("missing_receipt_jsonl", "duplicate_receipt_jsonl"):
            with self.subTest(key=key):
                events = _parse_jsonl(self.fixture[key])
                self.assertEqual(_extract_codex_session_id(events), "")
                self.assertIsNone(_execution_session_receipt(events, resumed=False))

    def test_start_exposes_fresh_and_resume_receipts_for_core_validation(self) -> None:
        session_id = self.fixture["thread_id"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executor = CodexExecutor(command=sys.executable, transport="cli")
            for resumed in (False, True):
                with self.subTest(resumed=resumed):
                    session = build_session_snapshot(
                        "codex",
                        retain=False,
                        session_id=session_id if resumed else "",
                        session_state="active" if resumed else "pending",
                        run_ids=["prior-run"] if resumed else [],
                    )
                    packet = {
                        "task_id": "CDEX-001",
                        "title": "Codex session receipt",
                        "steps": [{"id": 1, "description": "continue"}],
                        "workspace": {"root": str(root), "project_root": str(root)},
                        "extensions": {"agentbc.session": session},
                    }
                    completed = subprocess.CompletedProcess(
                        [], 0, stdout=self.fixture["unique_receipt_jsonl"], stderr=""
                    )
                    with (
                        mock.patch.object(executor, "_start_run_lease"),
                        mock.patch.object(executor, "_heartbeat_run"),
                        mock.patch.object(executor, "_close_run_lease"),
                        mock.patch(
                            "agent_bridge_connect.executors.codex.subprocess.run",
                            return_value=completed,
                        ),
                    ):
                        started = executor.start(packet)

                    self.assertTrue(started.ok)
                    receipt = executor.poll(started.run_id).result["execution_session"]
                    self.assertEqual(receipt["session_id"], session_id)
                    self.assertIs(receipt["resumed"], resumed)


if __name__ == "__main__":
    unittest.main()
