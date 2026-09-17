"""PERM-104-002 1.04A: legacy session tool rule retirement regressions.

The matcher-grammar path (``--approve-tool <matcher> --scope session``) was
REMOVED by the executor-native choice broker (``agentbc.approval`` v2,
``task respond --permission-option <handle>``).  This module proves the
retirement contract:

* the legacy module keeps ONLY its tombstone code and the audit-only
  projection of historical receipts — the matcher grammar, rule APIs and
  receipt lifecycle are gone;
* the CLI ``--approve-tool/--scope session`` flags parse for one release and
  always fail with ``legacy_session_tool_rule_removed``;
* the transport tombstones ``attach_session_rule`` and never reports an
  attached rule; the SDK session choice exists only for a fully valid
  ``destination=session`` suggestion bundle;
* the service tombstones ``issue_session_tool_rule`` /
  ``mark_session_tool_rule_applied`` and never writes a new session-rule
  receipt on any terminal path;
* the Runner no longer routes a ``respond_session_rule`` op;
* the control plane rejects a session rule on any v2 response;
* historical receipts stay read-only: terminal history is never rewritten,
  and zero new session-rule writes occur anywhere.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.claude_sdk_transport import (
    ClaudeSDKControlTransport,
    ClaudeSDKTransportError,
)
from agent_bridge_connect.control import ApprovalControlPlane, ControlPlaneError
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.runner import RunnerClient, RunnerState
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.session import control_root_for_task
from agent_bridge_connect.session_tool_rules import (
    LEGACY_SESSION_TOOL_RULE_REMOVED,
    SESSION_RULE_RECEIPT_EXTENSION_KEY,
    session_rule_receipt_public_projection,
)


def _receipt(session_id: str = "thread-fake-1") -> dict:
    return {
        "version": 1,
        "executor": "codex",
        "session_id": session_id,
        "resumed": False,
        "persistence": "persistent",
        "source": "jsonl_thread_started",
    }


def _v2_permission_request(session_id: str = "thread-fake-1") -> dict:
    """One v2 Codex permission request with schema-supported choices."""
    from agent_bridge_connect.control import codex_offered_choices

    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": session_id,
            "turnId": "turn-1",
            "permissions": {"fileSystem": "workspace-write"},
        },
        "approval_version": 2,
        "authority": {
            "executor": "codex",
            "protocol": "codex_app_server",
            "protocol_version": 2,
            "method": "item/permissions/requestApproval",
        },
        "offered_choices": [
            dict(choice)
            for choice in codex_offered_choices(
                "permissions", session_decisions_supported=True
            )
        ],
    }


class TombstoneModuleTests(unittest.TestCase):
    def test_matcher_grammar_is_gone(self) -> None:
        import agent_bridge_connect.session_tool_rules as module

        for removed in (
            "normalize_tool_matcher",
            "validate_session_rule_request",
            "build_session_rule_receipt",
            "issue_session_rule_receipt",
            "revoke_session_rule_receipt",
            "session_rule_receipt_active",
            "session_rule_receipt_for_session",
            "SessionRuleError",
            "SESSION_RULE_MATCHER_INVALID",
            "SESSION_RULE_MATCHER_WILDCARD",
            "SESSION_RULE_INPUT_MISSING",
            "SUPPORTED_SESSION_RULE_EXECUTORS",
        ):
            self.assertFalse(
                hasattr(module, removed),
                f"retired symbol still present: {removed}",
            )

    def test_tombstone_code_and_projection_remain(self) -> None:
        historical = {
            "version": 1,
            "selection_source": "cli_native_approval",
            "scope": "session",
            "state": {
                "status": "revoked",
                "revoked": True,
                "revocation_code": "session_rule_task_terminal",
                "revoked_at": "2026-08-31T00:00:00Z",
            },
            "matcher": {
                "display": "Bash(echo probe*)",
                "kind": "command_pattern",
            },
            "binding": {
                "binding_digest": "sha256:" + "a" * 64,
                "profile_digest": "sha256:" + "b" * 64,
            },
            "audit": {"created_at": "t1", "updated_at": "t2"},
        }
        projection = session_rule_receipt_public_projection(historical)
        self.assertTrue(projection["retired"])
        self.assertEqual(
            projection["retirement_code"], LEGACY_SESSION_TOOL_RULE_REMOVED
        )
        self.assertEqual(projection["matcher"], "Bash(echo probe*)")
        self.assertNotIn("request_id", projection)
        self.assertIsNone(session_rule_receipt_public_projection("nope"))


class TombstoneCliTests(unittest.TestCase):
    def test_approve_tool_flag_fails_with_tombstone(self) -> None:
        from agent_bridge_connect.cli import build_parser, command_task_respond

        parser = build_parser()
        # The tombstone flags parse for one release; the combination still
        # needs one primary response flag to satisfy the CLI contract.
        args = parser.parse_args(
            [
                "task",
                "respond",
                "T-1",
                "--input",
                "input-1",
                "--deny",
                "--approve-tool",
                "Bash",
            ]
        )
        self.assertEqual(args.approve_tool, "Bash")
        with mock.patch(
            "agent_bridge_connect.runner.RunnerClient.respond_task"
        ) as respond_task:
            exit_code = command_task_respond(args)
        self.assertEqual(exit_code, 1)
        respond_task.assert_not_called()

    def test_scope_flag_alone_fails_with_tombstone(self) -> None:
        from agent_bridge_connect.cli import build_parser, command_task_respond

        parser = build_parser()
        args = parser.parse_args(
            [
                "task",
                "respond",
                "T-1",
                "--input",
                "input-1",
                "--approve",
                "--scope",
                "session",
            ]
        )
        with mock.patch(
            "agent_bridge_connect.runner.RunnerClient.respond_task"
        ) as respond_task:
            exit_code = command_task_respond(args)
        self.assertEqual(exit_code, 1)
        respond_task.assert_not_called()

    def test_permission_option_flag_parses(self) -> None:
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "task",
                "respond",
                "T-1",
                "--input",
                "input-1",
                "--permission-option",
                "opt-abc123",
            ]
        )
        self.assertEqual(args.permission_option, "opt-abc123")
        self.assertFalse(args.approve)
        self.assertFalse(args.deny)


class TombstoneTransportTests(unittest.TestCase):
    def test_attach_session_rule_fails_closed(self) -> None:
        transport = ClaudeSDKControlTransport(
            plane=_StubPlane(), task_id="T", run_id="run", session_id="sess"
        )
        with self.assertRaises(ClaudeSDKTransportError) as ctx:
            transport.attach_session_rule(
                {"tool_name": "Bash", "rule_content": "*"}
            )
        self.assertEqual(ctx.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)
        self.assertFalse(transport.session_rule_attached())
        self.assertIsNone(transport._session_rule_from_receipt())

    def test_session_bundle_validation_exact(self) -> None:
        transport = ClaudeSDKControlTransport(
            plane=_StubPlane(), task_id="T", run_id="run", session_id="sess"
        )
        valid = [
            {
                "type": "addRules",
                "behavior": "allow",
                "destination": "session",
                "rules": [{"tool_name": "Bash", "rule_content": "echo *"}],
            }
        ]
        self.assertIsNotNone(transport.validate_session_bundle(valid))
        persistent = [
            {
                "type": "addRules",
                "behavior": "allow",
                "destination": "userSettings",
                "rules": [{"tool_name": "Bash", "rule_content": "echo *"}],
            }
        ]
        self.assertIsNone(transport.validate_session_bundle(persistent))
        bypass = [
            {
                "type": "setMode",
                "mode": "bypassPermissions",
                "destination": "session",
            }
        ]
        self.assertIsNone(transport.validate_session_bundle(bypass))
        # Mixed bundle rejected.
        self.assertIsNone(transport.validate_session_bundle(valid + bypass))
        # Unknown shape rejected.
        self.assertIsNone(transport.validate_session_bundle([{"type": "unknown"}]))
        # Empty / missing rejected.
        self.assertIsNone(transport.validate_session_bundle(None))
        self.assertIsNone(transport.validate_session_bundle([]))


class TombstoneServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / "record"
        self.board.mkdir(parents=True, exist_ok=True)
        self.service = TaskService(
            self.board, config={"workspace_root": str(self.root)}
        )
        self.task = self.service.create_task(
            "session rule retirement",
            "claude",
            [{"id": 1, "description": "exercise retirement"}],
            customer_dir=True,
            customer_path=self.root,
            permission_mode="safe",
        )

    def test_issue_and_mark_are_tombstoned(self) -> None:
        with self.assertRaises(ABCError) as ctx:
            self.service.issue_session_tool_rule(
                self.task.id, "input-1", tool_matcher="Bash"
            )
        self.assertEqual(ctx.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)
        with self.assertRaises(ABCError) as ctx2:
            self.service.mark_session_tool_rule_applied(
                self.task.id, session_id="sess-claude-1", applied=True
            )
        self.assertEqual(ctx2.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)

    def test_revoke_is_read_only_noop(self) -> None:
        historical = {
            "version": 1,
            "state": {"status": "active", "revoked": False},
            "matcher": {"display": "Bash", "kind": "tool_type"},
            "binding": {},
            "audit": {},
        }
        task = self.service.get_task(self.task.id)
        task.extensions = {
            **(task.extensions or {}),
            SESSION_RULE_RECEIPT_EXTENSION_KEY: historical,
        }
        self.service.store.write_task(task.id, task.to_dict())
        # Tombstone revoke never rewrites the historical receipt.
        self.assertFalse(
            self.service.revoke_session_tool_rule(
                self.task.id, "session_rule_task_terminal"
            )
        )
        reread = self.service.get_task(self.task.id)
        self.assertEqual(
            reread.extensions[SESSION_RULE_RECEIPT_EXTENSION_KEY]["state"]["status"],
            "active",
        )

    def test_no_new_session_rule_write_on_terminal_paths(self) -> None:
        from tests.contract_helpers import finalize_completed

        task_id = self.task.id
        self.assertTrue(finalize_completed(self.service, task_id))
        reread = self.service.get_task(task_id)
        self.assertNotIn(
            SESSION_RULE_RECEIPT_EXTENSION_KEY, reread.extensions or {}
        )


class TombstoneRunnerControlTests(unittest.TestCase):
    def test_runner_has_no_respond_session_rule(self) -> None:
        self.assertFalse(hasattr(RunnerState, "respond_session_rule"))
        self.assertFalse(hasattr(RunnerClient, "respond_session_rule"))

    def test_control_plane_rejects_rule_on_v2_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            board = root / "record"
            board.mkdir()
            task_id = "CDEX-V2-RULE-001"
            plane = ApprovalControlPlane(
                control_root_for_task(task_id, board_root=board),
                task_id=task_id,
                executor_run_id="codex-run-1",
                session_id="thread-fake-1",
            )
            plane.record_session_started(_receipt())
            event = plane.request_approval(_v2_permission_request())
            request_id = str(event.get("request_id") or "")
            pending = plane.status()["pending_request"]
            once_handle = next(
                choice["handle"]
                for choice in pending["offered_choices"]
                if choice["kind"] == "once"
            )
            with self.assertRaises(ControlPlaneError) as ctx:
                plane.respond_approval(
                    task_id,
                    "codex-run-1",
                    "thread-fake-1",
                    request_id,
                    "accept",
                    session_rule={"tool_name": "Bash", "rule_content": "*"},
                    choice_handle=once_handle,
                )
            # The rule is rejected before the handle is even consulted.
            self.assertEqual(ctx.exception.code, "session_rule_response_invalid")
            # A plain v2 response with the handle succeeds (rule ignored).
            fresh_plane = ApprovalControlPlane(
                control_root_for_task("CDEX-V2-RULE-002", board_root=board),
                task_id="CDEX-V2-RULE-002",
                executor_run_id="codex-run-2",
                session_id="thread-fake-1",
            )
            fresh_plane.record_session_started(_receipt())
            fresh_event = fresh_plane.request_approval(
                _v2_permission_request()
            )
            fresh_pending = fresh_plane.status()["pending_request"]
            fresh_handle = next(
                choice["handle"]
                for choice in fresh_pending["offered_choices"]
                if choice["kind"] == "once"
            )
            fresh_response = fresh_plane.respond_approval(
                "CDEX-V2-RULE-002",
                "codex-run-2",
                "thread-fake-1",
                str(fresh_event.get("request_id") or ""),
                "accept",
                choice_handle=fresh_handle,
            )
            self.assertNotIn("session_rule", fresh_response)
            self.assertEqual(fresh_response["decision"], "accept")


class HistoricalProjectionTests(unittest.TestCase):
    def test_status_projection_keeps_historical_receipt_read_only(self) -> None:
        """The audit-only projection surfaces retired receipts (no rewrite)."""
        historical = {
            "version": 1,
            "selection_source": "cli_native_approval",
            "scope": "session",
            "state": {"status": "revoked", "revoked": True},
            "matcher": {"display": "Bash(echo x*)", "kind": "command_pattern"},
            "binding": {"binding_digest": "sha256:" + "a" * 64},
            "audit": {"created_at": "t1", "updated_at": "t2"},
        }
        projected = session_rule_receipt_public_projection(historical)
        self.assertTrue(projected["retired"])
        self.assertEqual(projected["matcher"], "Bash(echo x*)")
        self.assertNotIn("request_id", projected)
        # The durable record itself is unchanged (read-only history).
        self.assertEqual(historical["state"]["status"], "revoked")

    def test_json_shape_stability(self) -> None:
        projection = session_rule_receipt_public_projection(
            {
                "version": 1,
                "state": {"status": "active", "revoked": False},
                "matcher": {"display": "Bash", "kind": "tool_type"},
                "binding": {},
                "audit": {},
            }
        )
        encoded = json.dumps(projection, sort_keys=True)
        self.assertIn('"retired": true', encoded)
        self.assertIn('"retirement_code": "legacy_session_tool_rule_removed"', encoded)


class _StubPlane:
    def request_approval(self, message):
        return {"request_id": "approval-stub"}

    def wait_for_decision(self, request_id, timeout_s):
        return {"decision": "decline"}

    def status(self):
        return {"pending_request": None}


if __name__ == "__main__":
    unittest.main()
