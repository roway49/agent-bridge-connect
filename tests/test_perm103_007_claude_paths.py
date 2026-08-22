from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.claude_path_capability import (
    assert_claude_path_capability_command,
    assert_claude_path_capability_supported,
    claude_ephemeral_path_capability,
)
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.path_model import build_path_plan
from agent_bridge_connect.permission_modes import (
    build_permission_record,
    validate_permission_command,
)
from agent_bridge_connect.permission_registry import probe_executor_capability
from agent_bridge_connect.protocol import ABCError


class Perm103007ClaudePathCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.customer = self.root / "customer"
        self.customer.mkdir()
        self.plan = build_path_plan(
            customer_dir=True,
            customer_path=self.customer,
            task_code="PTHS",
            iteration=1,
            config={"workspace_root": str(self.root / "workspace")},
            task_date="2026-08-22",
        )
        self.packet = {
            "task_id": "PTHS-001",
            "title": "Claude path capability",
            "steps": [{"id": 1, "description": "write the deliverable"}],
            "workspace": self.plan.to_workspace(),
            "task_board": {"root": str(self.root / "private-board")},
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                "agentbc.session": {
                    "version": 1,
                    "executor": "claude",
                    "retain": False,
                    "session_id": str(uuid.uuid4()),
                    "session_state": "pending",
                    "project_mode": "ephemeral",
                    "project_path": str(self.plan.executor_project_root),
                    "run_ids": [],
                },
            },
        }

    def _command(self, mode: str = "safe") -> list[str]:
        packet = dict(self.packet)
        packet["extensions"] = dict(self.packet["extensions"])
        record = build_permission_record(explicit_mode=mode)
        packet["extensions"]["agentbc.permission"] = record
        return ClaudeExecutor(command=sys.executable, transport="direct")._build_command(
            "prompt",
            self.plan.project_root,
            packet,
            record,
        )

    def test_all_permission_modes_receive_the_same_orthogonal_path_boundary(self) -> None:
        expected_settings = None
        for mode in ("inherit", "safe", "full"):
            with self.subTest(mode=mode):
                command = self._command(mode)
                self.assertEqual(command.count("--settings"), 1)
                self.assertEqual(command.count("--add-dir"), 1)
                settings_text = command[command.index("--settings") + 1]
                settings = json.loads(settings_text)
                self.assertEqual(
                    settings["sandbox"]["filesystem"]["allowWrite"],
                    [str(self.plan.artifact_root)],
                )
                self.assertEqual(
                    settings["sandbox"]["filesystem"]["denyWrite"],
                    [str(self.plan.executor_project_root)],
                )
                self.assertFalse(settings["sandbox"]["autoAllowBashIfSandboxed"])
                self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
                self.assertTrue(settings["sandbox"]["failIfUnavailable"])
                self.assertEqual(
                    command[command.index("--add-dir") + 1],
                    str(self.plan.artifact_root),
                )
                self.assertNotIn(str(self.root / "private-board"), settings_text)
                self.assertNotIn(str(self.plan.report_root), settings_text)
                if expected_settings is None:
                    expected_settings = settings_text
                self.assertEqual(settings_text, expected_settings)

    def test_runner_reconstructs_exact_settings_and_rejects_tampering(self) -> None:
        command = self._command("safe")
        capability = assert_claude_path_capability_command(
            command,
            self.packet,
            execution_root=self.plan.executor_project_root,
        )
        self.assertIsNotNone(capability)
        validate_permission_command(
            "claude",
            command,
            self.packet["extensions"]["agentbc.permission"],
            authorized_claude_settings=capability["settings_json"],
            authorized_claude_add_dir=True,
        )

        tampered_settings = list(command)
        settings_index = tampered_settings.index("--settings") + 1
        tampered_settings[settings_index] = '{"sandbox":{"enabled":false}}'
        with self.assertRaises(ABCError) as raised:
            assert_claude_path_capability_command(
                tampered_settings,
                self.packet,
                execution_root=self.plan.executor_project_root,
            )
        self.assertEqual(raised.exception.code, "claude_path_capability_mismatch")

        tampered_root = list(command)
        add_dir_index = tampered_root.index("--add-dir") + 1
        tampered_root[add_dir_index] = str(self.root)
        with self.assertRaises(ABCError):
            assert_claude_path_capability_command(
                tampered_root,
                self.packet,
                execution_root=self.plan.executor_project_root,
            )

        inline = [
            token
            for index, token in enumerate(command)
            if index not in {command.index("--settings"), command.index("--settings") + 1}
        ]
        inline.append(f"--settings={capability['settings_json']}")
        with self.assertRaises(ABCError):
            assert_claude_path_capability_command(
                inline,
                self.packet,
                execution_root=self.plan.executor_project_root,
            )

    def test_pathplan_mismatch_and_shared_project_artifact_fail_closed(self) -> None:
        mismatch = dict(self.packet)
        mismatch["extensions"] = dict(self.packet["extensions"])
        mismatch["extensions"]["agentbc.session"] = dict(
            self.packet["extensions"]["agentbc.session"]
        )
        mismatch["extensions"]["agentbc.session"]["project_path"] = str(
            self.root / "outside"
        )
        with self.assertRaises(ABCError) as raised:
            claude_ephemeral_path_capability(
                mismatch,
                execution_root=self.plan.executor_project_root,
            )
        self.assertEqual(raised.exception.code, "claude_path_capability_mismatch")

        shared = dict(self.packet)
        shared["workspace"] = dict(self.packet["workspace"])
        shared["workspace"]["artifact_root"] = str(self.plan.executor_project_root)
        with self.assertRaises(ABCError):
            claude_ephemeral_path_capability(
                shared,
                execution_root=self.plan.executor_project_root,
            )

    def test_version_and_help_probe_is_bounded_and_fail_closed(self) -> None:
        supported = (
            subprocess.CompletedProcess([], 0, stdout="2.1.226 (Claude Code)", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON", stderr=""),
        )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch(
                "agent_bridge_connect.claude_path_capability.subprocess.run",
                side_effect=supported,
            ),
        ):
            result = assert_claude_path_capability_supported("/bin/claude")
        self.assertTrue(result["supported"])
        self.assertEqual(result["version"], "2.1.226")

        cases = (
            (
                "Darwin",
                subprocess.CompletedProcess([], 0, stdout="2.2.0", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON", stderr=""),
            ),
            (
                "Darwin",
                subprocess.CompletedProcess([], 0, stdout="2.1.226", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="--add-dir X", stderr=""),
            ),
            (
                "Windows",
                subprocess.CompletedProcess([], 0, stdout="2.1.226", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON", stderr=""),
            ),
        )
        for system, version_result, help_result in cases:
            with self.subTest(system=system, version=version_result.stdout):
                with (
                    mock.patch("platform.system", return_value=system),
                    mock.patch(
                        "agent_bridge_connect.claude_path_capability.subprocess.run",
                        side_effect=(version_result, help_result),
                    ),
                    self.assertRaises(ABCError) as raised,
                ):
                    assert_claude_path_capability_supported("/bin/claude")
                self.assertEqual(
                    raised.exception.code,
                    "claude_path_capability_unsupported",
                )

    def test_permission_registry_reports_path_capability_for_inherit(self) -> None:
        completed = (
            subprocess.CompletedProcess([], 0, stdout="2.1.226 (Claude Code)", stderr=""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    "--add-dir X\n--settings JSON\n"
                    "--dangerously-skip-permissions\n--safe-mode\n"
                    "--permission-mode MODE"
                ),
                stderr="",
            ),
            subprocess.CompletedProcess([], 0, stdout="2.1.226 (Claude Code)", stderr=""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    "--add-dir X\n--settings JSON\n"
                    "--dangerously-skip-permissions\n--safe-mode\n"
                    "--permission-mode MODE"
                ),
                stderr="",
            ),
        )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch(
                "agent_bridge_connect.claude_path_capability.subprocess.run",
                side_effect=completed[:2],
            ),
        ):
            report = probe_executor_capability(
                "claude",
                "inherit",
                "/bin/claude",
            )
        self.assertTrue(report["supported"])
        self.assertEqual(
            report["details"]["path_capability"]["capability_id"],
            "claude.ephemeral_project_isolation.v1",
        )


if __name__ == "__main__":
    unittest.main()
