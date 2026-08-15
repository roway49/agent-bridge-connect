"""Targeted tests for the Claude stream/control permission-prompt broker."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.executors.claude import (
    PERMISSION_PROMPT_TOOL_FLAG,
    ClaudeExecutor,
    ClaudePermissionPromptBroker,
    _parse_stream_json_line,
)


def _can_use_tool_event(**overrides: str) -> dict:
    payload = {
        "type": "can_use_tool",
        "session_id": "019feed0-0000-7000-8000-0000000000aa",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "user_prompt": "list files",
    }
    payload.update(overrides)
    return payload


class ClaudePermissionBrokerTests(unittest.TestCase):
    def test_allow_only_returns_allow(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "allow"},
        )
        line = '{"type":"can_use_tool","tool_name":"Bash"}'
        response = broker.handle_request_line(line)
        self.assertEqual(response, '{"permission": "allow"}')

    def test_deny_only_returns_deny(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "deny"},
        )
        response = broker.handle_request_line('{"tool_name":"Bash"}')
        self.assertEqual(response, '{"permission": "deny"}')

    def test_never_applies_updated_permissions(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {
                "permission": "allow",
                "updated_permissions": ["Write"],
            },
        )
        response = broker.handle_request_line('{"tool_name":"Write"}')
        self.assertEqual(response, '{"permission": "allow"}')
        self.assertNotIn("updated_permissions", response)

    def test_invalid_json_is_denied(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "allow"},
        )
        response = broker.handle_request_line("not-json")
        self.assertTrue(response.startswith('{"permission": "deny"'))
        self.assertNotIn("allow", response)

    def test_init_receipt_verification(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "deny"},
        )
        self.assertTrue(
            broker._verify_init_receipt(
                {"type": "system", "subtype": "init", "session_id": "019feed0-0000-7000-8000-0000000000aa"}
            )
        )
        self.assertFalse(
            broker._verify_init_receipt(
                {"type": "system", "subtype": "init", "session_id": "other"}
            )
        )

    def test_stream_line_parser(self) -> None:
        parsed = _parse_stream_json_line('{"type":"init"}')
        self.assertEqual(parsed, {"type": "init"})
        self.assertIsNone(_parse_stream_json_line("garbage"))
        self.assertIsNone(_parse_stream_json_line(""))


class ClaudeControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "customer"
        self.project.mkdir()
        self.session_id = str(uuid.uuid4())

    def _packet(self) -> dict:
        return {
            "task_id": "ABCD-001",
            "title": "control path",
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {
                "customer_dir": True,
                "customer_path": str(self.project),
                "root": str(self.project),
                "project_root": str(self.project),
                "default_path": str(self.project),
                "executor_project_root": str(self.project / "claude"),
                "agentbc_root": str(self.root / "workspace"),
                "artifact_root": str(self.project),
                "report_root": str(self.root / "workspace" / "tasks" / "report"),
                "task_file": str(self.root / "workspace" / "tasks" / "task.md"),
                "report_file": str(self.root / "workspace" / "tasks" / "report.md"),
                "task_code": "ABCD",
                "iteration": "001",
                "task_date": "2026-08-10",
            },
            "task_board": {"root": str(self.root / "board")},
            "extensions": {
                "agentbc.resources": {
                    "version": 1,
                    "executor": "claude",
                    "resource": "max_budget_usd",
                    "current_limit": 2.5,
                },
                "agentbc.session": {
                    "version": 1,
                    "executor": "claude",
                    "retain": False,
                    "session_id": self.session_id,
                    "session_state": "pending",
                    "project_mode": "ephemeral",
                    "project_path": str(self.project / "claude"),
                    "run_ids": [],
                },
                "agentbc.permission": {
                    "requested_mode": "safe",
                    "effective_mode": "safe",
                    "selection_source": "configured_default",
                },
            },
        }

    @mock.patch("agent_bridge_connect.executors.claude.ClaudeExecutor.supports_permission_prompt_tool")
    def test_control_command_preallocates_session_and_adds_prompt_tool(
        self, supports: mock.Mock
    ) -> None:
        supports.return_value = True
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        broker = ClaudePermissionPromptBroker(
            session_id=self.session_id,
            decision_callback=lambda request: {"permission": "deny"},
        )
        command = executor._build_control_command(
            "prompt",
            self.project,
            self._packet(),
            {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "x"},
            broker,
        )
        self.assertEqual(command[command.index("--session-id") + 1], self.session_id)
        self.assertIn(PERMISSION_PROMPT_TOOL_FLAG, command)
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")

    @mock.patch("agent_bridge_connect.executors.claude.ClaudeExecutor.supports_permission_prompt_tool")
    def test_control_command_without_flag_uses_local_broker(self, supports: mock.Mock) -> None:
        supports.return_value = False
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        broker = ClaudePermissionPromptBroker(
            session_id=self.session_id,
            decision_callback=lambda request: {"permission": "deny"},
        )
        command = executor._build_control_command(
            "prompt",
            self.project,
            self._packet(),
            {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "x"},
            broker,
        )
        self.assertEqual(command[command.index("--session-id") + 1], self.session_id)
        self.assertNotIn(PERMISSION_PROMPT_TOOL_FLAG, command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")


if __name__ == "__main__":
    unittest.main()
