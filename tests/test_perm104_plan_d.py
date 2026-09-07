"""Plan D: native full launch and retired legacy authorization regression."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.executors.claude import ClaudeExecutor, _claude_control_required
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.permission_elevation import (
    build_permission_elevation,
    record_permission_elevation_decision,
)
from agent_bridge_connect.permission_modes import build_permission_record


class PlanDTests(unittest.TestCase):
    def test_full_commands_have_native_flags_without_agentbc_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permission = build_permission_record(explicit_mode="full")
            packet = {"workspace": {"root": directory}, "extensions": {
                "agentbc.permission": permission}}
            claude = ClaudeExecutor(command=sys.executable)
            with mock.patch(
                "agent_bridge_connect.executors.claude.claude_ephemeral_path_capability",
                side_effect=AssertionError("full must not construct sandbox capabilities"),
            ):
                commands = {
                    "claude": claude._build_command("prompt", root, packet, permission),
                    "codex": CodexExecutor(command=sys.executable)._build_command(
                        packet, "prompt", root, permission)[0],
                    "hermes": HermesExecutor(command=sys.executable, transport="direct")._build_command(
                        "prompt", permission=permission, task_packet=packet),
                }
            flags = {"claude": "--dangerously-skip-permissions",
                     "codex": "--dangerously-bypass-approvals-and-sandbox",
                     "hermes": "--yolo"}
            for executor, command in commands.items():
                with self.subTest(executor=executor):
                    self.assertIn(flags[executor], command)
                    for forbidden in ("--settings", "--add-dir", "--tools", "--allowedTools",
                                      "--disallowedTools", "--sandbox", "sandbox-exec"):
                        self.assertNotIn(forbidden, command)

    def test_legacy_grants_are_inert_even_when_malformed(self):
        for executor in ("codex", "claude", "hermes"):
            for grant in ({"version": 999}, {"state": {"status": "issued"}}, None):
                with self.subTest(executor=executor, grant=grant):
                    packet = {"extensions": {
                        "agentbc.permission": build_permission_record(explicit_mode="safe"),
                        "agentbc.permission_grant": grant,
                    }}
                    self.assertEqual(resolve_effective_permission(
                        packet, executor, "run-1")["effective_mode"], "safe")

    def test_approved_elevation_resolves_full_for_each_executor_without_runtime(self):
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                elevation = build_permission_elevation(
                    task_id="ABCD-001", path_plan_digest="", executor=executor,
                    executor_run_id="run-1", session_id="session-1", request_id="request-1",
                    request_fingerprint="sha256:" + "a" * 64,
                    operation="tool", containment_profile_digest="",
                )
                elevation = record_permission_elevation_decision(
                    elevation, "approve_full", source="human")
                packet = {"task_id": "ABCD-001", "runner_authorization_required": True,
                          "extensions": {
                              "agentbc.permission": build_permission_record(explicit_mode="safe"),
                              "agentbc.permission_elevation": elevation,
                          }}
                self.assertEqual(resolve_effective_permission(
                    packet, executor, "run-2")["effective_mode"], "full")
                self.assertNotIn("agentbc.permission_runtime", packet["extensions"])
                self.assertFalse(_claude_control_required(packet))
                self.assertFalse(CodexExecutor(command=sys.executable)._uses_app_server_transport(packet))
