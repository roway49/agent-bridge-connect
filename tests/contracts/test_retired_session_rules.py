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
from agent_bridge_connect.claude_sdk_transport import ClaudeSDKControlTransport, ClaudeSDKTransportError
from agent_bridge_connect.control import ApprovalControlPlane, ControlPlaneError
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.runner import RunnerClient, RunnerState
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.session import control_root_for_task
from agent_bridge_connect.session_tool_rules import LEGACY_SESSION_TOOL_RULE_REMOVED, SESSION_RULE_RECEIPT_EXTENSION_KEY, session_rule_receipt_public_projection
import copy as _cleanup_receipt_copy
import json as _cleanup_receipt_json
import unittest as _cleanup_receipt_unittest
from pathlib import Path as _cleanup_receipt_Path
from agent_bridge_connect.adapters import SessionCleanupRequest as _cleanup_receipt_SessionCleanupRequest, SessionCleanupResult as _cleanup_receipt_SessionCleanupResult
from agent_bridge_connect.execution_policy import (
    CLEANUP_CAPABILITIES as _cleanup_receipt_CLEANUP_CAPABILITIES,
    CLEANUP_STATES as _cleanup_receipt_CLEANUP_STATES,
    CLEANUP_STRATEGIES as _cleanup_receipt_CLEANUP_STRATEGIES,
    RESOLVED_CLEANUP_STATES as _cleanup_receipt_RESOLVED_CLEANUP_STATES,
    build_session_cleanup_receipt as _cleanup_receipt_build_session_cleanup_receipt,
    build_session_snapshot as _cleanup_receipt_build_session_snapshot,
    is_session_cleanup_resolved as _cleanup_receipt_is_session_cleanup_resolved,
    normalize_cleanup_commands as _cleanup_receipt_normalize_cleanup_commands,
    read_session_cleanup_receipt as _cleanup_receipt_read_session_cleanup_receipt,
    session_cleanup_blockers as _cleanup_receipt_session_cleanup_blockers,
    transition_session_cleanup as _cleanup_receipt_transition_session_cleanup,
    validate_session_cleanup_receipt as _cleanup_receipt_validate_session_cleanup_receipt,
    validate_session_snapshot as _cleanup_receipt_validate_session_snapshot,
)
from agent_bridge_connect.executors.mock import MockExecutor as _cleanup_receipt_MockExecutor
from agent_bridge_connect.executors.shell import ShellExecutor as _cleanup_receipt_ShellExecutor
from agent_bridge_connect.protocol import ABCError as _cleanup_receipt_ABCError


def _receipt(session_id: str = "thread-fake-1") -> dict:
    return {"version": 1, "executor": "codex", "session_id": session_id, "resumed": False, "persistence": "persistent", "source": "jsonl_thread_started"}


def _v2_permission_request(session_id: str = "thread-fake-1") -> dict:
    """One v2 Codex permission request with schema-supported choices."""
    from agent_bridge_connect.control import codex_offered_choices

    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "item/permissions/requestApproval",
        "params": {"threadId": session_id, "turnId": "turn-1", "permissions": {"fileSystem": "workspace-write"}},
        "approval_version": 2,
        "authority": {"executor": "codex", "protocol": "codex_app_server", "protocol_version": 2, "method": "item/permissions/requestApproval"},
        "offered_choices": [dict(choice) for choice in codex_offered_choices("permissions", session_decisions_supported=True)],
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
            self.assertFalse(hasattr(module, removed), f"retired symbol still present: {removed}")

    def test_tombstone_code_and_projection_remain(self) -> None:
        historical = {
            "version": 1,
            "selection_source": "cli_native_approval",
            "scope": "session",
            "state": {"status": "revoked", "revoked": True, "revocation_code": "session_rule_task_terminal", "revoked_at": "2026-08-31T00:00:00Z"},
            "matcher": {"display": "Bash(echo probe*)", "kind": "command_pattern"},
            "binding": {"binding_digest": "sha256:" + "a" * 64, "profile_digest": "sha256:" + "b" * 64},
            "audit": {"created_at": "t1", "updated_at": "t2"},
        }
        projection = session_rule_receipt_public_projection(historical)
        self.assertTrue(projection["retired"])
        self.assertEqual(projection["retirement_code"], LEGACY_SESSION_TOOL_RULE_REMOVED)
        self.assertEqual(projection["matcher"], "Bash(echo probe*)")
        self.assertNotIn("request_id", projection)
        self.assertIsNone(session_rule_receipt_public_projection("nope"))


class TombstoneCliTests(unittest.TestCase):
    def test_approve_tool_flag_fails_with_tombstone(self) -> None:
        from agent_bridge_connect.cli import build_parser, command_task_respond

        parser = build_parser()
        args = parser.parse_args(["task", "respond", "T-1", "--input", "input-1", "--deny", "--approve-tool", "Bash"])
        self.assertEqual(args.approve_tool, "Bash")
        with mock.patch("agent_bridge_connect.runner.RunnerClient.respond_task") as respond_task:
            exit_code = command_task_respond(args)
        self.assertEqual(exit_code, 1)
        respond_task.assert_not_called()

    def test_scope_flag_alone_fails_with_tombstone(self) -> None:
        from agent_bridge_connect.cli import build_parser, command_task_respond

        parser = build_parser()
        args = parser.parse_args(["task", "respond", "T-1", "--input", "input-1", "--approve", "--scope", "session"])
        with mock.patch("agent_bridge_connect.runner.RunnerClient.respond_task") as respond_task:
            exit_code = command_task_respond(args)
        self.assertEqual(exit_code, 1)
        respond_task.assert_not_called()

    def test_permission_option_flag_parses(self) -> None:
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["task", "respond", "T-1", "--input", "input-1", "--permission-option", "opt-abc123"])
        self.assertEqual(args.permission_option, "opt-abc123")
        self.assertFalse(args.approve)
        self.assertFalse(args.deny)


class TombstoneTransportTests(unittest.TestCase):
    def test_attach_session_rule_fails_closed(self) -> None:
        transport = ClaudeSDKControlTransport(plane=_StubPlane(), task_id="T", run_id="run", session_id="sess")
        with self.assertRaises(ClaudeSDKTransportError) as ctx:
            transport.attach_session_rule({"tool_name": "Bash", "rule_content": "*"})
        self.assertEqual(ctx.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)
        self.assertFalse(transport.session_rule_attached())
        self.assertIsNone(transport._session_rule_from_receipt())

    def test_session_bundle_validation_exact(self) -> None:
        transport = ClaudeSDKControlTransport(plane=_StubPlane(), task_id="T", run_id="run", session_id="sess")
        valid = [{"type": "addRules", "behavior": "allow", "destination": "session", "rules": [{"tool_name": "Bash", "rule_content": "echo *"}]}]
        self.assertIsNotNone(transport.validate_session_bundle(valid))
        persistent = [{"type": "addRules", "behavior": "allow", "destination": "userSettings", "rules": [{"tool_name": "Bash", "rule_content": "echo *"}]}]
        self.assertIsNone(transport.validate_session_bundle(persistent))
        bypass = [{"type": "setMode", "mode": "bypassPermissions", "destination": "session"}]
        self.assertIsNone(transport.validate_session_bundle(bypass))
        self.assertIsNone(transport.validate_session_bundle(valid + bypass))
        self.assertIsNone(transport.validate_session_bundle([{"type": "unknown"}]))
        self.assertIsNone(transport.validate_session_bundle(None))
        self.assertIsNone(transport.validate_session_bundle([]))


class TombstoneServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / "record"
        self.board.mkdir(parents=True, exist_ok=True)
        self.service = TaskService(self.board, config={"workspace_root": str(self.root)})
        self.task = self.service.create_task("session rule retirement", "claude", [{"id": 1, "description": "exercise retirement"}], customer_dir=True, customer_path=self.root, permission_mode="safe")

    def test_issue_and_mark_are_tombstoned(self) -> None:
        with self.assertRaises(ABCError) as ctx:
            self.service.issue_session_tool_rule(self.task.id, "input-1", tool_matcher="Bash")
        self.assertEqual(ctx.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)
        with self.assertRaises(ABCError) as ctx2:
            self.service.mark_session_tool_rule_applied(self.task.id, session_id="sess-claude-1", applied=True)
        self.assertEqual(ctx2.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)

    def test_revoke_is_read_only_noop(self) -> None:
        historical = {"version": 1, "state": {"status": "active", "revoked": False}, "matcher": {"display": "Bash", "kind": "tool_type"}, "binding": {}, "audit": {}}
        task = self.service.get_task(self.task.id)
        task.extensions = {**(task.extensions or {}), SESSION_RULE_RECEIPT_EXTENSION_KEY: historical}
        self.service.store.write_task(task.id, task.to_dict())
        self.assertFalse(self.service.revoke_session_tool_rule(self.task.id, "session_rule_task_terminal"))
        reread = self.service.get_task(self.task.id)
        self.assertEqual(reread.extensions[SESSION_RULE_RECEIPT_EXTENSION_KEY]["state"]["status"], "active")

    def test_no_new_session_rule_write_on_terminal_paths(self) -> None:
        from tests.contract_helpers import finalize_completed

        task_id = self.task.id
        self.assertTrue(finalize_completed(self.service, task_id))
        reread = self.service.get_task(task_id)
        self.assertNotIn(SESSION_RULE_RECEIPT_EXTENSION_KEY, reread.extensions or {})


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
            plane = ApprovalControlPlane(control_root_for_task(task_id, board_root=board), task_id=task_id, executor_run_id="codex-run-1", session_id="thread-fake-1")
            plane.record_session_started(_receipt())
            event = plane.request_approval(_v2_permission_request())
            request_id = str(event.get("request_id") or "")
            pending = plane.status()["pending_request"]
            once_handle = next((choice["handle"] for choice in pending["offered_choices"] if choice["kind"] == "once"))
            with self.assertRaises(ControlPlaneError) as ctx:
                plane.respond_approval(task_id, "codex-run-1", "thread-fake-1", request_id, "accept", session_rule={"tool_name": "Bash", "rule_content": "*"}, choice_handle=once_handle)
            self.assertEqual(ctx.exception.code, "session_rule_response_invalid")
            fresh_plane = ApprovalControlPlane(control_root_for_task("CDEX-V2-RULE-002", board_root=board), task_id="CDEX-V2-RULE-002", executor_run_id="codex-run-2", session_id="thread-fake-1")
            fresh_plane.record_session_started(_receipt())
            fresh_event = fresh_plane.request_approval(_v2_permission_request())
            fresh_pending = fresh_plane.status()["pending_request"]
            fresh_handle = next((choice["handle"] for choice in fresh_pending["offered_choices"] if choice["kind"] == "once"))
            fresh_response = fresh_plane.respond_approval("CDEX-V2-RULE-002", "codex-run-2", "thread-fake-1", str(fresh_event.get("request_id") or ""), "accept", choice_handle=fresh_handle)
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
        self.assertEqual(historical["state"]["status"], "revoked")

    def test_json_shape_stability(self) -> None:
        projection = session_rule_receipt_public_projection({"version": 1, "state": {"status": "active", "revoked": False}, "matcher": {"display": "Bash", "kind": "tool_type"}, "binding": {}, "audit": {}})
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


_cleanup_receipt_FIXTURE = _cleanup_receipt_Path(__file__).parents[1] / "fixtures" / "session_cleanup_receipts.json"
_cleanup_receipt_T0 = "2026-08-11T00:00:00Z"
_cleanup_receipt_T1 = "2026-08-11T00:00:01Z"
_cleanup_receipt_T2 = "2026-08-11T00:00:02Z"
_cleanup_receipt_T3 = "2026-08-11T00:00:03Z"


class _cleanup_receipt_CleanupReceiptContractTests(_cleanup_receipt_unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _cleanup_receipt_json.loads(_cleanup_receipt_FIXTURE.read_text(encoding="utf-8"))

    def test_default_receipt_matches_frozen_fixture_and_enums(self) -> None:
        receipt = _cleanup_receipt_build_session_cleanup_receipt()
        self.assertEqual(receipt, self.fixture["default"])
        self.assertEqual(_cleanup_receipt_CLEANUP_CAPABILITIES, {"unknown", "supported", "unsupported", "not_applicable"})
        self.assertEqual(_cleanup_receipt_CLEANUP_STRATEGIES, {"none", "retain", "claude_project_purge", "official_session_delete", "official_session_archive_then_delete", "container_session_archive_then_delete"})
        self.assertEqual(_cleanup_receipt_CLEANUP_STATES, {"not_requested", "retained", "pending", "succeeded", "unsupported", "failed"})
        self.assertEqual(_cleanup_receipt_validate_session_cleanup_receipt(receipt), [])

    def test_exact_legacy_receipt_is_read_with_inert_defaults_without_mutation(self) -> None:
        legacy = self.fixture["legacy_minimal"]
        before = _cleanup_receipt_copy.deepcopy(legacy)
        self.assertEqual(_cleanup_receipt_validate_session_cleanup_receipt(legacy, allow_legacy=True), [])
        self.assertTrue(_cleanup_receipt_validate_session_cleanup_receipt(legacy))
        projected = _cleanup_receipt_read_session_cleanup_receipt(legacy)
        self.assertEqual(legacy, before)
        self.assertEqual(projected["capability"], "unknown")
        self.assertEqual(projected["strategy"], "none")
        self.assertFalse(projected["retryable"])
        self.assertEqual(projected["next_attempt_at"], "")
        session = self._session(retain=False)
        session["cleanup"] = _cleanup_receipt_copy.deepcopy(legacy)
        self.assertEqual(_cleanup_receipt_validate_session_snapshot(session), [])
        self.assertEqual(session["cleanup"], before)

    def test_partial_or_unknown_schema_is_not_treated_as_legacy(self) -> None:
        partial = {"state": "not_requested", "attempts": 0, "version": 1}
        errors = _cleanup_receipt_validate_session_cleanup_receipt(partial, allow_legacy=True)
        self.assertTrue(any(("missing fields" in item for item in errors)))
        self.assertTrue(_cleanup_receipt_validate_session_cleanup_receipt({**self.fixture["default"], "prompt": "delete everything"}))

    def test_strict_types_enums_timestamps_and_error_codes(self) -> None:
        cases = {"version": True, "capability": "maybe", "strategy": "recursive_delete", "state": "complete", "attempts": False, "requested_at": 1, "last_attempt_at": "tomorrow", "retryable": 1}
        for field, value in cases.items():
            with self.subTest(field=field):
                receipt = _cleanup_receipt_copy.deepcopy(self.fixture["default"])
                receipt[field] = value
                self.assertTrue(_cleanup_receipt_validate_session_cleanup_receipt(receipt))
        receipt = _cleanup_receipt_copy.deepcopy(self.fixture["unsupported"])
        receipt["error_code"] = "/Users/example/.agent/private.db"
        self.assertTrue(_cleanup_receipt_validate_session_cleanup_receipt(receipt))

    def test_sensitive_or_raw_receipt_fields_are_rejected(self) -> None:
        for field in ("prompt", "command", "raw_output", "secret", "executor_database_path", "session_content"):
            with self.subTest(field=field):
                receipt = _cleanup_receipt_copy.deepcopy(self.fixture["default"])
                receipt[field] = "sensitive"
                errors = _cleanup_receipt_validate_session_cleanup_receipt(receipt)
                self.assertTrue(any(("unsupported fields" in item for item in errors)))

    @staticmethod
    def _session(*, retain: bool) -> dict:
        return _cleanup_receipt_build_session_snapshot("hermes", retain=retain, session_id="20260811_000000_a1b2c3", session_state="terminal", created_at=_cleanup_receipt_T0)


class _cleanup_receipt_CleanupTransitionTests(_cleanup_receipt_unittest.TestCase):
    def _session(self, *, retain: bool = False) -> dict:
        return _cleanup_receipt_build_session_snapshot("hermes", retain=retain, session_id="20260811_000000_a1b2c3", session_state="terminal", created_at=_cleanup_receipt_T0)

    @staticmethod
    def _transition(session: dict, target: str, **kwargs: object) -> dict:
        defaults = {"task_status": "completed", "lease_state": "closed", "task_end_dialog_delivered": True, "occurred_at": _cleanup_receipt_T1}
        defaults.update(kwargs)
        receipt = _cleanup_receipt_transition_session_cleanup(session, target, **defaults)
        session["cleanup"] = receipt
        return receipt

    def test_retain_terminal_session_resolves_without_cleanup_attempt(self) -> None:
        session = self._session(retain=True)
        retained = self._transition(session, "retained")
        self.assertEqual(retained["capability"], "not_applicable")
        self.assertEqual(retained["strategy"], "retain")
        self.assertEqual(retained["attempts"], 0)
        self.assertTrue(_cleanup_receipt_is_session_cleanup_resolved(retained))

    def test_legacy_resolved_receipt_is_an_exact_no_op(self) -> None:
        session = self._session()
        session["cleanup"] = {"state": "succeeded", "attempts": 1}
        before = _cleanup_receipt_copy.deepcopy(session["cleanup"])
        result = self._transition(session, "pending", occurred_at=_cleanup_receipt_T3)
        self.assertEqual(result, before)
        self.assertEqual(session["cleanup"], before)

    def test_supported_success_path_and_resolved_idempotence(self) -> None:
        session = self._session()
        pending = self._transition(session, "pending", capability="supported", strategy="official_session_delete")
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["requested_at"], _cleanup_receipt_T1)
        succeeded = self._transition(session, "succeeded", capability="supported", strategy="official_session_delete", occurred_at=_cleanup_receipt_T2)
        before = _cleanup_receipt_copy.deepcopy(succeeded)
        repeated = self._transition(session, "pending", occurred_at=_cleanup_receipt_T3)
        self.assertEqual(repeated, before)
        self.assertTrue(_cleanup_receipt_is_session_cleanup_resolved(repeated))

    def test_unsupported_path_is_resolved_and_not_retried(self) -> None:
        session = self._session()
        self._transition(session, "pending")
        unsupported = self._transition(session, "unsupported", error_code="official_session_delete_unavailable", occurred_at=_cleanup_receipt_T2)
        self.assertEqual(unsupported["capability"], "unsupported")
        self.assertFalse(unsupported["retryable"])
        self.assertIn(unsupported["state"], _cleanup_receipt_RESOLVED_CLEANUP_STATES)
        self.assertEqual(self._transition(session, "unsupported", occurred_at=_cleanup_receipt_T3), unsupported)

    def test_retryable_failure_honors_backoff_and_attempt_limit(self) -> None:
        session = self._session()
        self._transition(session, "pending", capability="supported", strategy="official_session_delete")
        failed = self._transition(session, "failed", capability="supported", strategy="official_session_delete", error_code="session_delete_busy", retryable=True, next_attempt_at=_cleanup_receipt_T3, occurred_at=_cleanup_receipt_T2)
        self.assertFalse(_cleanup_receipt_is_session_cleanup_resolved(failed))
        with self.assertRaisesRegex(_cleanup_receipt_ABCError, "backoff"):
            self._transition(session, "pending", occurred_at=_cleanup_receipt_T2)
        retry = self._transition(session, "pending", occurred_at=_cleanup_receipt_T3)
        self.assertEqual(retry["attempts"], 2)
        self._transition(session, "failed", error_code="session_delete_busy", retryable=True, next_attempt_at="2026-08-11T00:00:05Z", occurred_at="2026-08-11T00:00:04Z")
        self._transition(session, "pending", occurred_at="2026-08-11T00:00:05Z")
        self._transition(session, "failed", error_code="session_delete_busy", retryable=True, next_attempt_at="2026-08-11T00:00:07Z", occurred_at="2026-08-11T00:00:06Z")
        with self.assertRaisesRegex(_cleanup_receipt_ABCError, "attempt limit"):
            self._transition(session, "pending", occurred_at="2026-08-11T00:00:07Z")

    def test_exact_codex_desktop_ack_can_replace_exhausted_transport_failure(self) -> None:
        session = _cleanup_receipt_build_session_snapshot("codex", retain=False, session_id="01a081be-3e15-7f93-9b1c-2fb032b6c279", session_state="terminal", created_at=_cleanup_receipt_T0)
        session["receipt_source"] = "jsonl_thread_started"
        session["official_receipt_bound"] = True
        receipt = _cleanup_receipt_build_session_cleanup_receipt()
        receipt.update(
            {
                "capability": "supported",
                "strategy": "official_session_archive_then_delete",
                "state": "failed",
                "attempts": 3,
                "requested_at": _cleanup_receipt_T0,
                "last_attempt_at": _cleanup_receipt_T1,
                "error_code": "codex_desktop_archive_transport_lost",
                "retryable": False,
                "commands": _cleanup_receipt_normalize_cleanup_commands("not_requested"),
            }
        )
        receipt["commands"]["desktop_archive"]["status"] = "unavailable"
        receipt["commands"]["desktop_archive"]["checked_at"] = _cleanup_receipt_T1
        session["cleanup"] = receipt
        with self.assertRaisesRegex(_cleanup_receipt_ABCError, "not retryable"):
            self._transition(session, "pending", occurred_at=_cleanup_receipt_T2)
        pending = self._transition(session, "pending", occurred_at=_cleanup_receipt_T2, authoritative_archive_ack=True)
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["attempts"], 4)

    def test_all_direct_illegal_transitions_fail_closed(self) -> None:
        for target in ("succeeded", "unsupported", "failed"):
            with self.subTest(source="not_requested", target=target):
                with self.assertRaises(_cleanup_receipt_ABCError):
                    self._transition(self._session(), target)
        pending_session = self._session()
        self._transition(pending_session, "pending")
        with self.assertRaises(_cleanup_receipt_ABCError):
            self._transition(pending_session, "retained")
        failed_session = self._session()
        self._transition(failed_session, "pending")
        self._transition(failed_session, "failed", error_code="permanent_failure", retryable=False, occurred_at=_cleanup_receipt_T2)
        with self.assertRaisesRegex(_cleanup_receipt_ABCError, "not retryable"):
            self._transition(failed_session, "pending", occurred_at=_cleanup_receipt_T3)
        with self.assertRaises(_cleanup_receipt_ABCError):
            self._transition(failed_session, "succeeded", occurred_at=_cleanup_receipt_T3)

    def test_all_fail_closed_blockers_prevent_request(self) -> None:
        cases = (({"task_status": "needs_recovery"}, "recovery_session_preserved"), ({"task_end_dialog_delivered": False}, "task_end_dialog_not_delivered"), ({"lease_state": "active"}, "run_lease_not_closed"), ({"lease_state": "stale"}, "run_lease_not_closed"))
        for override, expected in cases:
            with self.subTest(override=override):
                session = self._session()
                blockers = _cleanup_receipt_session_cleanup_blockers(task_status=str(override.get("task_status", "completed")), lease_state=str(override.get("lease_state", "closed")), session=session, task_end_dialog_delivered=bool(override.get("task_end_dialog_delivered", True)))
                self.assertIn(expected, blockers)
                with self.assertRaises(_cleanup_receipt_ABCError):
                    self._transition(session, "pending", **override)
        for session_state in ("input_required", "needs_recovery", "active"):
            with self.subTest(session_state=session_state):
                session = self._session()
                session["session_state"] = session_state
                receipt = self._transition(session, "pending")
                self.assertEqual(receipt["state"], "pending")

    def test_invalid_result_metadata_cannot_store_raw_or_private_data(self) -> None:
        session = self._session()
        self._transition(session, "pending")
        for error_code in ("Delete failed: raw output", "/Users/example/.codex/state.sqlite", "token=super-secret"):
            with self.subTest(error_code=error_code):
                with self.assertRaises(_cleanup_receipt_ABCError):
                    self._transition(session, "failed", error_code=error_code, retryable=False, occurred_at=_cleanup_receipt_T2)


class _cleanup_receipt_AdapterCleanupDefaultsTests(_cleanup_receipt_unittest.TestCase):
    def test_cleanup_request_carries_redacted_path_plan_context(self) -> None:
        workspace = {"agentbc_root": "/managed/root", "executor_project_root": "/managed/root/tasks/artifacts/2026-08-11/TEST/TEST-001/claude", "task_code": "TEST", "iteration": "001", "task_date": "2026-08-11"}
        request = _cleanup_receipt_SessionCleanupRequest(executor="claude", session_id="exact-session-id", task_id="TEST-001", retain=False, project_mode="ephemeral", strategy="claude_project_purge", project_path=workspace["executor_project_root"], workspace=workspace)
        self.assertFalse(request.retain)
        self.assertEqual(request.project_mode, "ephemeral")
        self.assertEqual(request.workspace, workspace)
        self.assertNotIn(request.project_path, repr(request))
        self.assertNotIn(workspace["agentbc_root"], repr(request))

    def test_unimplemented_adapters_inherit_non_destructive_unsupported_default(self) -> None:
        adapters = (_cleanup_receipt_MockExecutor(), _cleanup_receipt_ShellExecutor())
        request = _cleanup_receipt_SessionCleanupRequest(executor="test", session_id="exact-session-id", task_id="TEST-001", strategy="official_session_delete")
        for adapter in adapters:
            with self.subTest(adapter=type(adapter).__name__):
                result = adapter.cleanup_session(request)
                self.assertIsInstance(result, _cleanup_receipt_SessionCleanupResult)
                self.assertEqual(result.state, "unsupported")
                self.assertEqual(result.capability, "unsupported")
                self.assertEqual(result.strategy, "none")
                self.assertFalse(result.retryable)
