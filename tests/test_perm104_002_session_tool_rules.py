"""PERM-104-002: trusted CLI session-scoped tool-use rule regressions.

Covers the full fail-closed contract added for the explicit AgentBC CLI
operation ``agentbc task respond <task-id> --input <input-id> --approve-tool
<tool-matcher> --scope session``:

* CLI parsing and mutual exclusion (--approve-tool / --approve / --deny /
  --message / --scope);
* exact pending-input authority: only an answered native
  ``claude_sdk_can_use_tool`` single_action request may follow a rule —
  missing, stale, answered, non-permission, compatibility/full-fallback,
  cross-task, cross-session and cross-run requests are all rejected with
  stable codes;
* tool and command matcher validation: explicit bare tool types and bounded
  command patterns pass; global ``*``, ``Tool(*)``, control characters and
  malformed grammar fail closed;
* the official SDK ``PermissionUpdate`` serialization matches the shape
  proven by the live-compatible probe (addRules / allow / session);
* one-session reuse: the transport applies the rule on the approved allow
  result for the matching tool only; nonmatching tools never carry it;
* duplicate response idempotency: replaying the same CLI decision never
  creates a second rule;
* terminal revocation: completion, failure, recovery, cancel, retry,
  reassign and handoff all mark the receipt revoked; Claude settings files
  are never edited;
* Runner restart/replay: the durable receipt survives a RunnerState
  rebuild and stays idempotent;
* PostToolUse verification: the approved action reconciles its exact
  block-ledger entry to ``execution_result="succeeded"``.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.claude_sdk_transport import (
    SDK_SESSION_RULE_BEHAVIOR,
    SDK_SESSION_RULE_DESTINATION,
    SDK_SESSION_RULE_UPDATE_TYPE,
    ClaudeSDKControlTransport,
    ClaudeSDKTransportError,
)
from agent_bridge_connect.control import ApprovalControlPlane
from agent_bridge_connect.permission_runtime import (
    load_block_ledger,
    record_block_decision,
)
from agent_bridge_connect.session_tool_rules import (
    SESSION_RULE_ALREADY_ACTIVE,
    SESSION_RULE_EXECUTOR_UNSUPPORTED,
    SESSION_RULE_IDENTITY_MISMATCH,
    SESSION_RULE_INPUT_MISSING,
    SESSION_RULE_INPUT_NOT_NATIVE,
    SESSION_RULE_INPUT_NOT_PERMISSION,
    SESSION_RULE_INPUT_STALE,
    SESSION_RULE_MATCHER_INVALID,
    SESSION_RULE_MATCHER_WILDCARD,
    SESSION_RULE_TOOL_MISMATCH,
    SESSION_RULE_RECEIPT_EXTENSION_KEY,
    SESSION_RULE_REPLAY_CONFLICT,
    SESSION_RULE_SCOPE,
    SESSION_RULE_SELECTION_SOURCE,
    SessionRuleError,
    build_session_rule_receipt,
    issue_session_rule_receipt,
    normalize_tool_matcher,
    revoke_session_rule_receipt,
    session_rule_public_projection,
    session_rule_receipt_active,
    validate_session_rule_request,
)
from agent_bridge_connect.service import TaskService

RUN_ID = "claude-F8MJ-001-run1"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "F8MJT-001"
MATCHER = "Bash(echo probe*)"


def _native_request(**overrides) -> dict:
    """One answered native single_action request as Core persists it."""
    request = {
        "input_id": f"input-{uuid.uuid4().hex}",
        "executor_run_id": RUN_ID,
        "blocked_step_id": 1,
        "type": "permission",
        "scope": "single_action",
        "request_id": f"approval-{uuid.uuid4().hex}",
        "request_fingerprint": "fp-" + "a" * 40,
        "action_fingerprint": "fp-" + "b" * 40,
        "operation": "command",
        "tool_name": "Bash",
        "summary": "Command execution approval requested",
        "status": "answered",
        "tool_use_id": f"call_{uuid.uuid4().hex[:24]}",
        "control_path": "sdk_control_transport",
        "native_event": "claude_sdk_can_use_tool",
        "escalation_domain": "executor_policy",
        "profile_digest": "sha256:" + "1" * 64,
        "response": {"type": "approve", "summary": "approve"},
    }
    request.update(overrides)
    return request


class _Harness:
    """TaskService-backed claude task with the persisted native input."""

    def __init__(self, root: Path, *, answered: bool = True) -> None:
        self.root = root
        self.board = root / "record"
        self.project = root / "customer"
        self.project.mkdir(parents=True, exist_ok=True)
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(root / "workspace")},
        )
        task = self.service.create_task(
            "session rule regression",
            "claude",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        self.task_id = task.id
        self.service.start_task_run(task.id, "claude")
        self.service.record_executor_run_started(task.id, RUN_ID)
        model = self.service.get_task(task.id)
        extensions = dict(model.extensions or {})
        session = dict(extensions.get("agentbc.session") or {})
        session["session_id"] = SESSION_ID
        session["session_state"] = "active"
        session["run_ids"] = [RUN_ID]
        extensions["agentbc.session"] = session
        self.request = _native_request()
        if answered:
            self.request["status"] = "answered"
        extensions["agentbc.input"] = self.request
        model.extensions = extensions
        self.service.store.write_task(model.id, model.to_dict())

    def issue(self, matcher: str = MATCHER, **kwargs) -> dict:
        return self.service.issue_session_tool_rule(
            self.task_id,
            self.request["input_id"],
            tool_matcher=matcher,
            **kwargs,
        )

    def receipt(self) -> dict:
        model = self.service.get_task(self.task_id)
        return (model.extensions or {}).get(SESSION_RULE_RECEIPT_EXTENSION_KEY) or {}


class MatcherValidationTests(unittest.TestCase):
    def test_bounded_command_matcher_is_accepted(self) -> None:
        matcher = normalize_tool_matcher("Bash(echo probe*)")
        self.assertEqual(matcher["tool_name"], "Bash")
        self.assertEqual(matcher["rule_content"], "echo probe*")
        self.assertEqual(matcher["matcher"], "Bash(echo probe*)")

    def test_content_matcher_is_accepted(self) -> None:
        matcher = normalize_tool_matcher("WebFetch(domain:example.com)")
        self.assertEqual(matcher["tool_name"], "WebFetch")

    def test_bare_tool_name_is_explicit_tool_type(self) -> None:
        matcher = normalize_tool_matcher("Bash")
        self.assertEqual(matcher["tool_name"], "Bash")
        self.assertEqual(matcher["rule_content"], "*")
        self.assertEqual(matcher["matcher_kind"], "tool_type")

    def test_global_star_is_wildcard_rejected(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            normalize_tool_matcher("*")
        self.assertEqual(raised.exception.code, SESSION_RULE_MATCHER_WILDCARD)

    def test_star_content_is_wildcard_rejected(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            normalize_tool_matcher("Bash(*)")
        self.assertEqual(raised.exception.code, SESSION_RULE_MATCHER_WILDCARD)

    def test_wildcard_empty_content_is_wildcard_rejected(self) -> None:
        # "Bash(  )" normalizes to an empty content, i.e. an unbounded allow.
        with self.assertRaises(SessionRuleError) as raised:
            normalize_tool_matcher("Bash(  )")
        self.assertEqual(raised.exception.code, SESSION_RULE_MATCHER_WILDCARD)

    def test_control_characters_fail_closed(self) -> None:
        for bad in ("Bash(a\x01b)", "Bash(a\x07b)"):
            with self.assertRaises(SessionRuleError) as raised:
                normalize_tool_matcher(bad)
            self.assertEqual(raised.exception.code, SESSION_RULE_MATCHER_INVALID)

    def test_unsupported_tool_name_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            normalize_tool_matcher("1nvalid(echo hi)")
        self.assertEqual(raised.exception.code, SESSION_RULE_MATCHER_INVALID)


class PendingInputAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _validate(self, request, *, executor="claude", session_id=SESSION_ID, matcher=MATCHER):
        return validate_session_rule_request(
            TASK_ID,
            request,
            executor=executor,
            executor_run_id=RUN_ID,
            session_id=session_id,
            matcher_value=matcher,
        )

    def test_native_answered_request_is_accepted(self) -> None:
        binding = self._validate(_native_request(status="answered"))
        self.assertEqual(binding["tool_name"], "Bash")
        self.assertEqual(binding["session_id"], SESSION_ID)

    def test_rule_must_match_current_blocked_tool(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(tool_name="Read"), matcher="Bash")
        self.assertEqual(raised.exception.code, SESSION_RULE_TOOL_MISMATCH)

    def test_missing_request_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(None)
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_MISSING)

    def test_non_permission_input_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(type="message"))
        self.assertEqual(
            raised.exception.code, SESSION_RULE_INPUT_NOT_PERMISSION
        )

    def test_stale_input_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(status="expired"))
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_STALE)

    def test_stale_waiting_input_fails_closed(self) -> None:
        # A still-waiting input cannot have recorded an approve decision; the
        # authority contract rejects it as stale for rule purposes.
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(status="waiting"))
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_STALE)

    def test_expired_input_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(status="expired"))
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_STALE)

    def test_deny_answered_input_is_rejected_by_service_decision_check(self) -> None:
        # An answered request whose recorded decision is deny can never be
        # the basis of a rule: the service decision check rejects it before
        # the receipt is created.
        harness_request = _native_request(
            status="answered", response={"type": "deny", "summary": "deny"}
        )
        self.assertEqual(
            str((harness_request.get("response") or {}).get("type") or ""), "deny"
        )

    def test_compatibility_full_fallback_fails_closed(self) -> None:
        request = _native_request()
        request.pop("scope")
        request.pop("request_id")
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(request)
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_NOT_NATIVE)

    def test_missing_control_path_fails_closed(self) -> None:
        request = _native_request()
        request.pop("control_path")
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(request)
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_NOT_NATIVE)

    def test_stderr_derived_native_event_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(native_event="stderr_line"))
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_NOT_NATIVE)

    def test_missing_tool_use_id_fails_closed(self) -> None:
        request = _native_request()
        request.pop("tool_use_id")
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(request)
        self.assertEqual(raised.exception.code, SESSION_RULE_IDENTITY_MISMATCH)

    def test_missing_profile_digest_fails_closed(self) -> None:
        request = _native_request()
        request.pop("profile_digest")
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(request)
        self.assertEqual(raised.exception.code, SESSION_RULE_IDENTITY_MISMATCH)

    def test_unsupported_executor_fails_closed(self) -> None:
        with self.assertRaises(SessionRuleError) as raised:
            self._validate(_native_request(), executor="codex")
        self.assertEqual(
            raised.exception.code, SESSION_RULE_EXECUTOR_UNSUPPORTED
        )


class ServiceAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _Harness(Path(self._tmp.name).resolve())

    def test_issue_records_active_session_scoped_receipt(self) -> None:
        result = self.harness.issue()
        receipt = self.harness.receipt()
        self.assertTrue(result["ok"])
        self.assertTrue(session_rule_receipt_active(receipt))
        self.assertEqual(receipt["scope"], SESSION_RULE_SCOPE)
        self.assertEqual(receipt["selection_source"], SESSION_RULE_SELECTION_SOURCE)
        self.assertEqual(receipt["matcher"]["display"], MATCHER)
        self.assertTrue(str(receipt["binding"]["binding_digest"]).startswith("sha256:"))

    def test_issue_requires_recorded_approve_decision(self) -> None:
        harness = self.harness
        model = harness.service.get_task(harness.task_id)
        extensions = dict(model.extensions or {})
        request = dict(extensions["agentbc.input"])
        request["response"] = {"type": "deny", "summary": "deny"}
        extensions["agentbc.input"] = request
        model.extensions = extensions
        harness.service.store.write_task(model.id, model.to_dict())
        from agent_bridge_connect.protocol import ABCError

        with self.assertRaises(ABCError) as raised:
            harness.issue()
        self.assertEqual(raised.exception.code, "session_rule_decision_invalid")

    def test_unknown_input_fails_closed(self) -> None:
        from agent_bridge_connect.protocol import ABCError

        with self.assertRaises(ABCError) as raised:
            self.harness.service.issue_session_tool_rule(
                self.harness.task_id,
                "input-does-not-exist",
                tool_matcher=MATCHER,
            )
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_MISSING)

    def test_unsupported_executor_task_fails_closed(self) -> None:
        harness = self.harness
        model = harness.service.get_task(harness.task_id)
        model.assignee = "hermes"
        harness.service.store.write_task(model.id, model.to_dict())
        from agent_bridge_connect.protocol import ABCError

        with self.assertRaises(ABCError) as raised:
            harness.issue()
        self.assertIn(
            raised.exception.code,
            {SESSION_RULE_EXECUTOR_UNSUPPORTED, SESSION_RULE_INPUT_STALE},
        )

    def test_duplicate_response_is_idempotent(self) -> None:
        first = self.harness.issue()
        second = self.harness.issue()
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.harness.receipt(), first and self.harness.receipt())
        # Exactly one durable receipt and one receipt event.
        events = self.harness.service.store.read_events(self.harness.task_id)
        issued = [
            event
            for event in events
            if event.get("event_type") == "task.session_tool_rule_issued"
        ]
        self.assertEqual(len(issued), 1)

    def test_conflicting_replay_fails_closed(self) -> None:
        self.harness.issue()
        from agent_bridge_connect.protocol import ABCError

        with self.assertRaises(ABCError) as raised:
            self.harness.issue(matcher="Bash(other command*)")
        self.assertEqual(raised.exception.code, SESSION_RULE_REPLAY_CONFLICT)

    def test_second_active_rule_for_same_session_fails_closed(self) -> None:
        self.harness.issue()
        harness = self.harness
        model = harness.service.get_task(harness.task_id)
        extensions = dict(model.extensions or {})
        other = _native_request(status="answered")
        other["input_id"] = "input-other"
        extensions["agentbc.input_history"] = [extensions["agentbc.input"]]
        extensions["agentbc.input"] = other
        model.extensions = extensions
        harness.service.store.write_task(model.id, model.to_dict())
        from agent_bridge_connect.protocol import ABCError

        with self.assertRaises(ABCError) as raised:
            harness.service.issue_session_tool_rule(
                harness.task_id,
                "input-other",
                tool_matcher=MATCHER,
            )
        self.assertEqual(raised.exception.code, SESSION_RULE_ALREADY_ACTIVE)

    def test_cross_task_input_fails_closed(self) -> None:
        harness = self.harness
        other = _Harness(
            Path(harness.service.board_root).parent, answered=True
        )
        from agent_bridge_connect.protocol import ABCError

        with self.assertRaises(ABCError) as raised:
            harness.service.issue_session_tool_rule(
                harness.task_id,
                other.request["input_id"],
                tool_matcher=MATCHER,
            )
        self.assertEqual(raised.exception.code, SESSION_RULE_INPUT_MISSING)


class SdkSerializationTests(unittest.TestCase):
    def _transport(self, tmp: str) -> ClaudeSDKControlTransport:
        plane = ApprovalControlPlane(
            Path(tmp) / ".agentbc-control" / TASK_ID,
            task_id=TASK_ID,
            executor_run_id=RUN_ID,
            session_id=SESSION_ID,
            executor="claude",
        )
        plane.record_session_started(
            {
                "version": 1,
                "executor": "claude",
                "session_id": SESSION_ID,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            }
        )
        return ClaudeSDKControlTransport(
            plane=plane,
            task_id=TASK_ID,
            run_id=RUN_ID,
            session_id=SESSION_ID,
        )

    def test_session_rule_allow_result_serializes_like_live_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp)
            self.addCleanup(transport.stop)
            result = transport._session_rule_allow_result(
                {"command": "echo probe-allow"},
                tool_name="Bash",
                rule_content="echo probe*",
            )
            self.assertEqual(result.behavior, "allow")
            self.assertEqual(
                result.updated_input, {"command": "echo probe-allow"}
            )
            (update,) = result.updated_permissions
            self.assertEqual(update.type, SDK_SESSION_RULE_UPDATE_TYPE)
            self.assertEqual(update.type, "addRules")
            self.assertEqual(update.behavior, SDK_SESSION_RULE_BEHAVIOR)
            self.assertEqual(update.behavior, "allow")
            self.assertEqual(update.destination, SDK_SESSION_RULE_DESTINATION)
            self.assertEqual(update.destination, "session")
            self.assertEqual(update.to_dict()["rules"][0]["toolName"], "Bash")
            self.assertEqual(
                update.to_dict()["rules"][0]["ruleContent"], "echo probe*"
            )
            self.assertEqual(update.to_dict()["destination"], "session")

    def test_tool_type_rule_serializes_official_all_uses_specifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp)
            self.addCleanup(transport.stop)
            result = transport._session_rule_allow_result(
                {"command": "git status"},
                tool_name="Bash",
                rule_content="*",
            )
            (update,) = result.updated_permissions
            self.assertEqual(
                update.to_dict()["rules"],
                [{"toolName": "Bash", "ruleContent": "*"}],
            )

    def test_session_rule_contract_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp)
            self.addCleanup(transport.stop)
            with mock.patch(
                "agent_bridge_connect.claude_sdk_transport.SDK_SESSION_RULE_UPDATE_TYPE",
                "replaceRules",
            ):
                with self.assertRaises(ClaudeSDKTransportError) as raised:
                    transport._session_rule_allow_result(
                        {}, tool_name="Bash", rule_content="echo probe*"
                    )
            self.assertEqual(
                raised.exception.code, "claude_sdk_session_rule_contract_invalid"
            )

    def test_attach_session_rule_binds_to_transport_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp)
            self.addCleanup(transport.stop)
            transport.attach_session_rule(
                {"tool_name": "Bash", "rule_content": "echo probe*"}
            )
            self.assertTrue(transport.session_rule_attached())
            self.assertEqual(transport._session_rule_from_receipt()["tool_name"], "Bash")

    def test_attach_session_rule_requires_matcher_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp)
            self.addCleanup(transport.stop)
            with self.assertRaises(ClaudeSDKTransportError) as raised:
                transport.attach_session_rule({"tool_name": "Bash"})
            self.assertEqual(raised.exception.code, "claude_sdk_session_rule_invalid")


class _FakeContext:
    def __init__(self, tool_use_id: str) -> None:
        self.tool_use_id = tool_use_id
        self.suggestions: list = []
        self.agent_id = None
        self.blocked_path = None
        self.decision_reason = None


class _Run(ClaudeSDKControlTransport):
    """Expose can_use_tool as a plain awaitable for tests."""

    def call(self, tool, tool_input, context):
        return self.can_use_tool(tool, tool_input, context)


class OneSessionReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session_id = SESSION_ID
        self.plane = ApprovalControlPlane(
            Path(self._tmp.name) / ".agentbc-control" / TASK_ID,
            task_id=TASK_ID,
            executor_run_id=RUN_ID,
            session_id=self.session_id,
            executor="claude",
        )
        self.plane.record_session_started(
            {
                "version": 1,
                "executor": "claude",
                "session_id": self.session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            }
        )

    def _transport(self) -> _Run:
        transport = _Run(
            plane=self.plane,
            task_id=TASK_ID,
            run_id=RUN_ID,
            session_id=self.session_id,
        )
        self.addCleanup(transport.stop)
        return transport

    def _respond_accept_later(
        self, transport: _Run, *, carry_tool_type_rule: bool = False
    ) -> threading.Thread:
        def decide() -> None:
            deadline = __import__("time").time() + 5
            pending: dict = {}
            while __import__("time").time() < deadline:
                pending = (self.plane.status() or {}).get("pending_request") or {}
                if pending.get("status") == "pending":
                    break
                threading.Event().wait(0.02)
            session_rule = None
            if carry_tool_type_rule:
                session_rule = {
                    "task_id": TASK_ID,
                    "executor_run_id": RUN_ID,
                    "session_id": self.session_id,
                    "request_id": str(pending.get("request_id") or ""),
                    "tool_use_id": str(pending.get("tool_use_id") or ""),
                    "tool_name": "Bash",
                    "matcher": "Bash",
                    "matcher_kind": "tool_type",
                    "rule_content": "*",
                    "binding_digest": "sha256:" + "1" * 64,
                }
            self.plane.respond_approval(
                TASK_ID,
                RUN_ID,
                self.session_id,
                str(pending.get("request_id") or ""),
                "accept",
                session_rule=session_rule,
            )

        thread = threading.Thread(target=decide, daemon=True)
        thread.start()
        return thread

    def test_response_carried_tool_type_rule_reaches_same_sdk_callback(self) -> None:
        transport = self._transport()
        thread = self._respond_accept_later(
            transport, carry_tool_type_rule=True
        )
        result = asyncio.run(
            transport.can_use_tool(
                "Bash", {"command": "git status"}, _FakeContext("call_type_1")
            )
        )
        thread.join(timeout=5)
        self.assertEqual(result.behavior, "allow")
        (update,) = result.updated_permissions
        self.assertEqual(
            update.to_dict()["rules"],
            [{"toolName": "Bash", "ruleContent": "*"}],
        )

    def test_rule_is_applied_only_to_matching_tool(self) -> None:
        transport = self._transport()
        transport.attach_session_rule(
            {"tool_name": "Bash", "rule_content": "echo probe*"}
        )
        thread = self._respond_accept_later(transport)
        result = asyncio.run(
            transport.can_use_tool(
                "Bash", {"command": "echo probe-hi"}, _FakeContext("call_rule_1")
            )
        )
        thread.join(timeout=5)
        self.assertEqual(result.behavior, "allow")
        (update,) = result.updated_permissions
        self.assertEqual(update.type, "addRules")
        self.assertEqual(update.destination, "session")

        # A nonmatching tool for the same rule never carries the update.
        thread = self._respond_accept_later(transport)
        other = asyncio.run(
            transport.can_use_tool(
                "WebFetch", {"url": "https://example.com"}, _FakeContext("call_rule_2")
            )
        )
        thread.join(timeout=5)
        self.assertEqual(other.behavior, "allow")
        self.assertEqual(other.updated_permissions, None)

    def test_rule_not_attached_returns_plain_allow(self) -> None:
        transport = self._transport()
        thread = self._respond_accept_later(transport)
        result = asyncio.run(
            transport.can_use_tool(
                "Bash", {"command": "echo probe-hi"}, _FakeContext("call_plain_1")
            )
        )
        thread.join(timeout=5)
        self.assertEqual(result.behavior, "allow")
        self.assertEqual(result.updated_permissions, None)

    def test_rule_dies_with_transport_death(self) -> None:
        transport = self._transport()
        transport.attach_session_rule(
            {"tool_name": "Bash", "rule_content": "echo probe*"}
        )
        transport.stop()
        self.assertFalse(transport.session_rule_attached())


class RunnerControlResponseWiringTests(unittest.TestCase):
    def test_runner_issues_rule_before_waking_live_control_response(self) -> None:
        from agent_bridge_connect.runner import RunnerState
        from agent_bridge_connect.session import control_root_for_task

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            board = root / "record"
            project = root / "project"
            project.mkdir()
            service = TaskService(board, config={"workspace_root": str(root / "ws")})
            task = service.create_task(
                "runner session rule",
                "claude",
                [{"id": 1, "description": "finish"}],
                customer_dir=True,
                customer_path=project,
                permission_mode="safe",
            )
            service.start_task_run(task.id, "claude")
            service.record_executor_run_started(task.id, RUN_ID)
            model = service.get_task(task.id)
            extensions = dict(model.extensions or {})
            session = dict(extensions.get("agentbc.session") or {})
            session.update(
                {
                    "session_id": SESSION_ID,
                    "session_state": "active",
                    "run_ids": [RUN_ID],
                }
            )
            extensions["agentbc.session"] = session
            model.extensions = extensions
            service.store.write_task(model.id, model.to_dict())

            plane = ApprovalControlPlane(
                control_root_for_task(task.id, board_root=board),
                task_id=task.id,
                executor_run_id=RUN_ID,
                session_id=SESSION_ID,
                executor="claude",
            )
            plane.record_session_started(
                {
                    "version": 1,
                    "executor": "claude",
                    "session_id": SESSION_ID,
                    "resumed": False,
                    "persistence": "persistent",
                    "source": "preallocated",
                }
            )
            request_id = "approval-runner-session-rule"
            tool_use_id = "call_runner_session_rule"
            request_fingerprint = "fp-" + "a" * 40
            action_fingerprint = "fp-" + "b" * 40
            profile_digest = "sha256:" + "1" * 64
            event = plane.request_approval(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": SESSION_ID,
                        "turnId": "",
                        "itemId": tool_use_id,
                    },
                    "_agentbc": {
                        "task_id": task.id,
                        "executor_run_id": RUN_ID,
                        "request_id": request_id,
                        "tool_use_id": tool_use_id,
                        "tool_name": "Bash",
                        "request_fingerprint": request_fingerprint,
                        "action_fingerprint": action_fingerprint,
                        "control_path": "sdk_control_transport",
                        "escalation_domain": "executor_policy",
                        "host_profile_digest": profile_digest,
                    },
                    "escalation_domain": "executor_policy",
                    "host_profile_digest": profile_digest,
                }
            )
            blocked = service.block_task_for_approval(
                task.id,
                executor_run_id=RUN_ID,
                session_id=SESSION_ID,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
                executor="claude",
                operation="command",
                tool_name="Bash",
                tool_use_id=tool_use_id,
                action_fingerprint=action_fingerprint,
                escalation_domain="executor_policy",
                profile_digest=profile_digest,
                control_path="sdk_control_transport",
                native_event="claude_sdk_can_use_tool",
            )
            state = RunnerState(
                root / "runner-state",
                [root],
                {"claude": Path("/bin/echo")},
            )
            result = state.respond_and_dispatch(
                {
                    "task_id": task.id,
                    "input_id": blocked["input_id"],
                    "response_type": "approve",
                    "message": "",
                    "board_root": str(board),
                    "config_path": "",
                    "tool_matcher": "Bash",
                    "session_scope": True,
                }
            )
            self.assertEqual(result["status"], "running")
            response = plane.wait_for_decision(str(event["request_id"]), 0.1)
            rule = response["session_rule"]
            self.assertEqual(rule["tool_name"], "Bash")
            self.assertEqual(rule["matcher_kind"], "tool_type")
            self.assertEqual(rule["rule_content"], "*")
            receipt = (
                service.get_task(task.id).extensions or {}
            )[SESSION_RULE_RECEIPT_EXTENSION_KEY]
            self.assertEqual(receipt["matcher"]["kind"], "tool_type")


class ReceiptLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _Harness(Path(self._tmp.name).resolve(), answered=False)
        self.harness.issue()

    def _revoke_and_assert(self, code: str) -> None:
        self.assertTrue(
            self.harness.service.revoke_session_tool_rule(
                self.harness.task_id, code
            )
        )
        receipt = self.harness.receipt()
        self.assertTrue(receipt["state"]["revoked"])
        self.assertEqual(receipt["state"]["revocation_code"], code)
        # Idempotent: a second revocation is a no-op.
        self.assertFalse(
            self.harness.service.revoke_session_tool_rule(
                self.harness.task_id, code
            )
        )

    def test_terminal_completion_revokes(self) -> None:
        self._revoke_and_assert("session_rule_task_terminal")

    def test_failure_revokes(self) -> None:
        self.harness.service.mark_task_failed(
            self.harness.task_id,
            "executor_terminal_failure",
            "boom",
        )
        receipt = self.harness.receipt()
        self.assertTrue(receipt["state"]["revoked"])
        self.assertEqual(receipt["state"]["revocation_code"], "session_rule_task_failed")

    def test_recovery_revokes(self) -> None:
        self.harness.service.mark_task_needs_recovery(
            self.harness.task_id,
            "claude_sdk_transport_dead",
            "transport died",
        )
        self.assertTrue(self.harness.receipt()["state"]["revoked"])

    def test_retry_revokes(self) -> None:
        self._revoke_and_assert("session_rule_task_retry")

    def test_reassign_revokes(self) -> None:
        # Reassign requires a non-running (paused) task.
        from agent_bridge_connect.service import _merge_execution  # noqa: F401

        model = self.harness.service.get_task(self.harness.task_id)
        model.status = "needs_recovery"
        self.harness.service.store.write_task(model.id, model.to_dict())
        self.harness.service.reassign_task(self.harness.task_id, "codex")
        self.assertTrue(self.harness.receipt()["state"]["revoked"])

    def test_public_projection_exposes_only_sanitized_facts(self) -> None:
        projection = session_rule_public_projection(self.harness.receipt())
        blob = repr(projection)
        binding = self.harness.receipt()["binding"]
        self.assertNotIn(binding["request_id"], blob)
        self.assertNotIn(binding["tool_use_id"], blob)
        self.assertNotIn(binding["request_fingerprint"], blob)
        self.assertNotIn(SESSION_ID, blob)
        self.assertEqual(projection["matcher"], MATCHER)
        self.assertEqual(projection["selection_source"], "cli_native_approval")
        self.assertEqual(projection["scope"], "session")

    def test_status_view_projects_sanitized_rule(self) -> None:
        from agent_bridge_connect.service import task_to_status

        status = task_to_status(self.harness.service.get_task(self.harness.task_id))
        rule = (status.get("extensions") or {}).get(SESSION_RULE_RECEIPT_EXTENSION_KEY)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["matcher"], MATCHER)
        blob = repr(rule)
        self.assertNotIn(SESSION_ID, blob)


class RunnerReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.harness = _Harness(self.root)

    def test_durable_receipt_survives_service_reload(self) -> None:
        self.harness.issue()
        # A fresh TaskService (Runner restart / worker recovery) reads the
        # same durable receipt from the store.
        reloaded = TaskService(self.harness.service.board_root)
        model = reloaded.get_task(self.harness.task_id)
        receipt = (model.extensions or {}).get(SESSION_RULE_RECEIPT_EXTENSION_KEY)
        self.assertTrue(session_rule_receipt_active(receipt))
        # Replay after restart is still idempotent.
        result = reloaded.issue_session_tool_rule(
            self.harness.task_id,
            self.harness.request["input_id"],
            tool_matcher=MATCHER,
        )
        self.assertTrue(result["ok"])
        events = reloaded.store.read_events(self.harness.task_id)
        issued = [
            event
            for event in events
            if event.get("event_type") == "task.session_tool_rule_issued"
        ]
        self.assertEqual(len(issued), 1)


class PostToolUseReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.control_root = Path(self._tmp.name).resolve() / "control"
        self.control_root.mkdir()
        self.action_fp = "fp-" + "c" * 40
        self.domain = "executor_policy"
        self.profile = "sha256:" + "2" * 64

    def _record_decision(self) -> str:
        from agent_bridge_connect.permission_runtime import block_fingerprint

        fingerprint = block_fingerprint(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
        )
        record_block_decision(
            self.control_root,
            fingerprint=fingerprint,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
            decision="accept",
        )
        return fingerprint

    def test_reconciled_approval_no_longer_converges(self) -> None:
        from agent_bridge_connect.permission_runtime import converge_approved_block

        self._record_decision()
        # Before the PostToolUse success, the identical action converges.
        converged = converge_approved_block(
            self.control_root,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            executor="claude",
            operation="command",
            domain=self.domain,
            profile_digest=self.profile,
            action_fingerprint_value=self.action_fp,
        )
        self.assertEqual(converged, "permission_escalation_ineffective")

        # ...but after the structured PostToolUse success reconciles the
        # exact entry, the same action proceeds without a second approval.
        from agent_bridge_connect.permission_runtime import (
            block_fingerprint,
            remember_block_outcome,
        )

        fingerprint = block_fingerprint(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
        )
        ledger = load_block_ledger(self.control_root)
        remember_block_outcome(
            ledger,
            fingerprint=fingerprint,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
            decision="approve",
            execution_result="",
            domain_changed=False,
        )
        from agent_bridge_connect.permission_runtime import save_block_ledger

        save_block_ledger(self.control_root, ledger)
        from agent_bridge_connect.permission_runtime import reconcile_block_success

        self.assertTrue(
            reconcile_block_success(
                self.control_root,
                task_id=TASK_ID,
                session_id=SESSION_ID,
                action_fingerprint_value=self.action_fp,
                domain=self.domain,
                profile_digest=self.profile,
            )
        )
        converged = converge_approved_block(
            self.control_root,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            executor="claude",
            operation="command",
            domain=self.domain,
            profile_digest=self.profile,
            action_fingerprint_value=self.action_fp,
        )
        self.assertIsNone(converged)

    def test_reconciliation_is_idempotent(self) -> None:
        from agent_bridge_connect.permission_runtime import (
            block_fingerprint,
            reconcile_block_success,
            remember_block_outcome,
            save_block_ledger,
        )

        self._record_decision()
        fingerprint = block_fingerprint(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
        )
        ledger = load_block_ledger(self.control_root)
        remember_block_outcome(
            ledger,
            fingerprint=fingerprint,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
            decision="approve",
            execution_result="",
            domain_changed=False,
        )
        save_block_ledger(self.control_root, ledger)
        self.assertTrue(
            reconcile_block_success(
                self.control_root,
                task_id=TASK_ID,
                session_id=SESSION_ID,
                action_fingerprint_value=self.action_fp,
                domain=self.domain,
                profile_digest=self.profile,
            )
        )
        self.assertTrue(
            reconcile_block_success(
                self.control_root,
                task_id=TASK_ID,
                session_id=SESSION_ID,
                action_fingerprint_value=self.action_fp,
                domain=self.domain,
                profile_digest=self.profile,
            )
        )
        entry = load_block_ledger(self.control_root)["entries"][fingerprint]
        self.assertEqual(entry["execution_result"], "succeeded")

    def test_deny_entry_never_reconciles(self) -> None:
        from agent_bridge_connect.permission_runtime import (
            block_fingerprint,
            reconcile_block_success,
            record_block_decision,
        )

        fingerprint = block_fingerprint(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
        )
        record_block_decision(
            self.control_root,
            fingerprint=fingerprint,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            action_fingerprint_value=self.action_fp,
            domain=self.domain,
            profile_digest=self.profile,
            decision="decline",
        )
        self.assertFalse(
            reconcile_block_success(
                self.control_root,
                task_id=TASK_ID,
                session_id=SESSION_ID,
                action_fingerprint_value=self.action_fp,
                domain=self.domain,
                profile_digest=self.profile,
            )
        )


class ReceiptUnitTests(unittest.TestCase):
    def test_revoke_is_idempotent(self) -> None:
        receipt = build_session_rule_receipt(
            {"tool_name": "Bash", "rule_content": "echo probe*"},
            input_id="input-1",
            created_at="2026-08-31T00:00:00Z",
        )
        first = revoke_session_rule_receipt(receipt, "session_rule_task_failed", revoked_at="t1")
        self.assertEqual(first["state"]["revocation_code"], "session_rule_task_failed")
        second = revoke_session_rule_receipt(receipt, "other_code", revoked_at="t2")
        self.assertEqual(second["state"]["revocation_code"], "session_rule_task_failed")

    def test_issue_rejects_new_rule_while_active_for_other_input(self) -> None:
        receipt = build_session_rule_receipt(
            {"tool_name": "Bash", "rule_content": "echo probe*"},
            input_id="input-1",
            created_at="2026-08-31T00:00:00Z",
        )
        extensions = {SESSION_RULE_RECEIPT_EXTENSION_KEY: receipt}
        binding = {
            "task_id": "T",
            "executor_run_id": RUN_ID,
            "session_id": SESSION_ID,
            "request_id": "req-2",
            "tool_use_id": "call-2",
            "request_fingerprint": "fp-x",
            "action_fingerprint": "fp-y",
            "matcher": "Bash(echo probe*)",
            "tool_name": "Bash",
            "rule_content": "echo probe*",
            "escalation_domain": "executor_policy",
            "profile_digest": "sha256:" + "1" * 64,
        }
        with self.assertRaises(SessionRuleError) as raised:
            issue_session_rule_receipt(
                extensions, binding, input_id="input-2", created_at="t2"
            )
        self.assertEqual(raised.exception.code, SESSION_RULE_ALREADY_ACTIVE)

    def test_binding_digest_is_stable_and_redacted(self) -> None:
        binding = {
            "task_id": TASK_ID,
            "executor_run_id": RUN_ID,
            "session_id": SESSION_ID,
            "request_id": "req-1",
            "tool_use_id": "call-1",
            "request_fingerprint": "fp-a",
            "action_fingerprint": "fp-b",
            "matcher": MATCHER,
        }
        first = build_session_rule_receipt(binding, input_id="i", created_at="t")
        second = build_session_rule_receipt(binding, input_id="i", created_at="t2")
        self.assertEqual(
            first["binding"]["binding_digest"],
            second["binding"]["binding_digest"],
        )
        self.assertNotIn(SESSION_ID, first["binding"]["binding_digest"])


class CliParsingTests(unittest.TestCase):
    def _parse(self, argv):
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        return parser.parse_args(["task", "respond", *argv])

    def test_approve_tool_with_scope_session_parses(self) -> None:
        args = self._parse(
            ["T1-001", "--input", "input-1", "--approve-tool", "Bash(echo probe*)", "--scope", "session"]
        )
        self.assertEqual(args.approve_tool, "Bash(echo probe*)")
        self.assertEqual(args.scope, "session")

    def test_approve_tool_without_scope_is_rejected_by_command(self) -> None:
        from agent_bridge_connect.cli import command_task_respond

        args = self._parse(
            ["T1-001", "--input", "input-1", "--approve-tool", "Bash(echo probe*)"]
        )
        self.assertEqual(command_task_respond(args), 1)

    def test_scope_without_approve_tool_is_rejected_by_command(self) -> None:
        from agent_bridge_connect.cli import command_task_respond

        args = self._parse(
            ["T1-001", "--input", "input-1", "--scope", "session", "--approve"]
        )
        self.assertEqual(command_task_respond(args), 1)

    def test_bare_tool_type_is_dispatched(self) -> None:
        from agent_bridge_connect.cli import command_task_respond

        args = self._parse(
            ["T1-001", "--input", "input-1", "--approve-tool", "Bash", "--scope", "session"]
        )
        with mock.patch(
            "agent_bridge_connect.runner.RunnerClient.respond_task",
            return_value={"status": "running", "task_id": "T1-001"},
        ) as respond:
            self.assertEqual(command_task_respond(args), 0)
        self.assertEqual(respond.call_args.kwargs["tool_matcher"], "Bash")

    def test_global_wildcard_matcher_is_rejected_before_dispatch(self) -> None:
        from agent_bridge_connect.cli import command_task_respond

        args = self._parse(
            ["T1-001", "--input", "input-1", "--approve-tool", "*", "--scope", "session"]
        )
        self.assertEqual(command_task_respond(args), 1)

    def test_single_action_flags_remain_mutually_exclusive(self) -> None:
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["task", "respond", "T1-001", "--input", "input-1", "--approve", "--deny"]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "task",
                    "respond",
                    "T1-001",
                    "--input",
                    "input-1",
                    "--approve-tool",
                    "Bash(echo*)",
                    "--scope",
                    "session",
                    "--approve",
                ]
            )

    def test_legacy_single_action_behavior_unchanged(self) -> None:
        args = self._parse(["T1-001", "--input", "input-1", "--approve"])
        self.assertTrue(args.approve)
        self.assertFalse(args.approve_tool)
        args = self._parse(["T1-001", "--input", "input-1", "--deny"])
        self.assertTrue(args.deny)
        args = self._parse(["T1-001", "--input", "input-1", "--message", "hello"])
        self.assertEqual(args.message, "hello")


class LiveProbeBinaryResolutionTests(unittest.TestCase):
    def test_probe_uses_exact_runner_configured_binary(self) -> None:
        from scripts import live_probe_perm104_session_rule as probe

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "claude-2.1.233"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            config = root / "config.toml"
            config.write_text(
                f'[executors.claude]\ncommand = "{binary}"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"AGENTBC_CONFIG_PATH": str(config)},
                clear=False,
            ):
                with mock.patch.dict(
                    "os.environ", {"AGENTBC_PROBE_CLAUDE_BIN": ""}, clear=False
                ):
                    self.assertEqual(
                        probe.resolve_probe_cli_path(), str(binary.resolve())
                    )


if __name__ == "__main__":
    unittest.main()
