"""PERM-104-002 v2 tests: native choice broker, exact adapters, retirement."""

from __future__ import annotations

import unittest

from agent_bridge_connect.approval import (
    approval_choice_for_handle,
    approval_public_projection_v2,
    build_approval_receipt,
    build_approval_receipt_v2,
    record_approval_selection,
    validate_approval_receipt,
    validate_approval_receipt_any_version,
)
from agent_bridge_connect.control import (
    ApprovalRequest,
    ControlPlaneError,
    approval_response_payload,
    approval_response_payload_v2,
    claude_offered_choices,
    codex_offered_choices,
    hermes_offered_choices,
)
from agent_bridge_connect.migration import legacy_permission_cutover_blocked
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.session_tool_rules import (
    LEGACY_SESSION_TOOL_RULE_REMOVED,
    session_rule_receipt_public_projection,
)


def _v2_receipt(**overrides):
    kwargs: dict = dict(
        task_id="T-1",
        executor_run_id="run-1",
        executor="codex",
        session_id="sess-1",
        request_id="approval-abc",
        request_fingerprint="fp-abc123def456",
        operation="command",
        authority_protocol="codex_app_server",
        authority_protocol_version=2,
        authority_method="item/commandExecution/requestApproval",
        broker_request_id="approval-abc",
        provider_request_id="srv-1",
        native_item_id="item-1",
        offered_choices=[
            {"native_option_id": "accept", "kind": "once", "label": "Approve once"},
            {
                "native_option_id": "acceptForSession",
                "kind": "session",
                "label": "Approve for this session",
            },
            {"native_option_id": "decline", "kind": "deny", "label": "Deny"},
        ],
    )
    kwargs.update(overrides)
    return build_approval_receipt_v2(**kwargs)


class ApprovalV2BrokerTests(unittest.TestCase):
    def test_v2_receipt_binds_choices_and_handles(self) -> None:
        receipt = _v2_receipt()
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(len(receipt["choices"]), 3)
        for choice in receipt["choices"]:
            self.assertTrue(choice["handle"].startswith("opt-"))
            self.assertEqual(len(choice["offered_digest"]), 64)
        self.assertEqual(receipt["authority"]["protocol"], "codex_app_server")
        self.assertEqual(receipt["broker_request_id"], "approval-abc")

    def test_v1_receipt_still_validates(self) -> None:
        receipt = build_approval_receipt(
            task_id="T-1",
            executor_run_id="run-1",
            executor="codex",
            session_id="sess-1",
            request_id="approval-old",
            request_fingerprint="fp-old",
            operation="command",
            summary="codex needs one-time permission for: command",
        )
        validated = validate_approval_receipt(receipt)
        self.assertEqual(validated["version"], 1)
        # Dual-read returns the same v1 receipt untouched.
        self.assertEqual(validate_approval_receipt_any_version(receipt)["version"], 1)

    def test_handle_bound_to_exact_request(self) -> None:
        receipt = _v2_receipt()
        handle = receipt["choices"][0]["handle"]
        # A different request id must produce a different handle for the same
        # offered shape: handles are not transferable across requests.
        other = _v2_receipt(request_id="approval-other")
        self.assertNotEqual(other["choices"][0]["handle"], handle)

    def test_selection_replay_idempotent_conflict_rejected(self) -> None:
        receipt = _v2_receipt()
        handle = receipt["choices"][2]["handle"]
        answered = record_approval_selection(
            receipt, handle, source="user", decided_type="deny"
        )
        self.assertEqual(answered["selection"]["native_option_id"], "decline")
        # Identical replay returns unchanged.
        replayed = record_approval_selection(
            answered, handle, source="user", decided_type="deny"
        )
        self.assertEqual(replayed["selection"], answered["selection"])
        # Conflicting replay (different handle) is rejected.
        with self.assertRaises(ABCError) as ctx:
            record_approval_selection(
                answered,
                receipt["choices"][0]["handle"],
                source="user",
                decided_type="approve",
            )
        self.assertEqual(ctx.exception.code, "approval_replay")

    def test_unknown_handle_rejected(self) -> None:
        receipt = _v2_receipt()
        with self.assertRaises(ABCError) as ctx:
            record_approval_selection(
                receipt, "opt-doesnotexist", source="user", decided_type="deny"
            )
        self.assertEqual(ctx.exception.code, "approval_handle_mismatch")

    def test_non_selectable_choice_rejected(self) -> None:
        receipt = _v2_receipt(
            offered_choices=[
                {
                    "native_option_id": "amendment",
                    "kind": "other",
                    "label": "Execpolicy amendment",
                    "selectable": False,
                },
            ]
        )
        handle = receipt["choices"][0]["handle"]
        with self.assertRaises(ABCError) as ctx:
            record_approval_selection(
                receipt, handle, source="user", decided_type="approve"
            )
        self.assertEqual(ctx.exception.code, "approval_choice_not_selectable")

    def test_public_projection_hides_binding_and_raw_payloads(self) -> None:
        receipt = _v2_receipt()
        projection = approval_public_projection_v2(receipt)
        text = repr(projection)
        self.assertNotIn("request_fingerprint", text)
        self.assertNotIn("fp-abc123", text)
        self.assertNotIn("executor_run_id", projection)
        self.assertNotIn("session_id", projection)
        # Handles and labels are exposed.
        self.assertTrue(all(c["handle"] for c in projection["choices"]))
        self.assertEqual(projection["choices"][0]["label"], "Approve once")

    def test_empty_choices_rejected(self) -> None:
        with self.assertRaises(ABCError) as ctx:
            _v2_receipt(offered_choices=[])
        self.assertEqual(ctx.exception.code, "approval_choices_missing")

    def test_choice_lookup(self) -> None:
        receipt = _v2_receipt()
        handle = receipt["choices"][1]["handle"]
        choice = approval_choice_for_handle(receipt, handle)
        self.assertEqual(choice["native_option_id"], "acceptForSession")
        self.assertIsNone(approval_choice_for_handle(receipt, "opt-bogus"))
        # v1 receipts have no choices.
        v1 = build_approval_receipt(
            task_id="T",
            executor_run_id="r",
            executor="codex",
            session_id="s",
            request_id="q",
            request_fingerprint="fp-x",
            operation="command",
            summary="codex needs one-time permission for: command",
        )
        self.assertIsNone(approval_choice_for_handle(v1, handle))


class CodexExactAdapterTests(unittest.TestCase):
    def _request(self, operation: str = "command") -> ApprovalRequest:
        choices = codex_offered_choices(
            operation, session_decisions_supported=True
        )
        return ApprovalRequest(
            request_id="approval-x1",
            request_fingerprint="fp-x1",
            rpc_id=7,
            task_id="T",
            executor_run_id="run",
            session_id="sess",
            kind="permission",
            operation=operation,
            summary="s",
            approval_version=2,
            offered_choices=choices,
        )

    def test_exact_once_decision(self) -> None:
        payload = approval_response_payload_v2(
            self._request("command"),
            choice_kind="once",
            native_option_id="accept",
            decision="accept",
        )
        self.assertEqual(payload, {"decision": "accept"})

    def test_exact_session_decision(self) -> None:
        payload = approval_response_payload_v2(
            self._request("command"),
            choice_kind="session",
            native_option_id="acceptForSession",
            decision="accept",
        )
        self.assertEqual(payload, {"decision": "acceptForSession"})

    def test_exact_decline(self) -> None:
        payload = approval_response_payload_v2(
            self._request("command"),
            choice_kind="deny",
            native_option_id="decline",
            decision="decline",
        )
        self.assertEqual(payload, {"decision": "decline"})

    def test_permissions_turn_and_session_payloads(self) -> None:
        request = self._request("permissions")
        request = ApprovalRequest(
            **{
                **request.__dict__,
                "requested_permissions": {"network": {"allow": ["example.com"]}},
            }
        )
        turn = approval_response_payload_v2(
            request,
            choice_kind="once",
            native_option_id="accept_turn",
            decision="accept",
        )
        self.assertEqual(turn["scope"], "turn")
        self.assertEqual(turn["permissions"], {"network": {"allow": ["example.com"]}})
        session = approval_response_payload_v2(
            request,
            choice_kind="session",
            native_option_id="accept_session",
            decision="accept",
        )
        self.assertEqual(session["scope"], "session")
        deny = approval_response_payload_v2(
            request,
            choice_kind="deny",
            native_option_id="decline",
            decision="decline",
        )
        self.assertEqual(deny, {"decision": "decline"})

    def test_amendment_native_id_never_selectable(self) -> None:
        with self.assertRaises(ControlPlaneError):
            approval_response_payload_v2(
                self._request("command"),
                choice_kind="other",
                native_option_id="acceptWithExecpolicyAmendment",
                decision="accept",
            )

    def test_codex_choices_exclude_amendments(self) -> None:
        choices = codex_offered_choices("command", session_decisions_supported=True)
        ids = [choice["native_option_id"] for choice in choices]
        self.assertEqual(ids, ["accept", "acceptForSession", "decline"])
        self.assertNotIn("cancel", ids)
        self.assertFalse(any("Amendment" in item for item in ids))

    def test_session_choices_gated_by_schema_support(self) -> None:
        choices = codex_offered_choices("command", session_decisions_supported=False)
        self.assertEqual(
            [choice["native_option_id"] for choice in choices],
            ["accept", "decline"],
        )

    def test_v1_payload_builder_unchanged(self) -> None:
        request = self._request("permissions")
        payload = approval_response_payload(request, "accept")
        self.assertEqual(payload["scope"], "turn")


class ClaudeExactAdapterTests(unittest.TestCase):
    def test_choices_once_and_deny_without_bundle(self) -> None:
        choices = claude_offered_choices(session_bundle_supported=False)
        self.assertEqual(
            [choice["native_option_id"] for choice in choices],
            ["deny", "allow_once"],
        )

    def test_session_choice_only_with_bundle(self) -> None:
        choices = claude_offered_choices(session_bundle_supported=True)
        self.assertEqual(
            [choice["native_option_id"] for choice in choices],
            ["deny", "allow_once", "allow_session"],
        )

    def test_validate_session_bundle_rejects_mixed_and_persistent(self) -> None:
        from agent_bridge_connect.claude_sdk_transport import (
            ClaudeSDKControlTransport,
        )

        plane = _stub_plane()
        transport = ClaudeSDKControlTransport(
            plane=plane, task_id="T", run_id="run", session_id="sess"
        )
        # Valid session bundle.
        valid = [
            {
                "type": "addRules",
                "behavior": "allow",
                "destination": "session",
                "rules": [{"tool_name": "Bash", "rule_content": "echo *"}],
            }
        ]
        self.assertIsNotNone(transport.validate_session_bundle(valid))
        # The CLI may leave destination unset until the user chooses scope.
        # AgentBC may bind that exact suggested rule to the live session, but
        # must not infer or rewrite the matcher itself.
        undecided_scope = [
            {
                "type": "addRules",
                "behavior": "allow",
                "rules": [{"tool_name": "Bash", "rule_content": "echo *"}],
            }
        ]
        normalized = transport.validate_session_bundle(undecided_scope)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized[0]["destination"], "session")
        self.assertEqual(normalized[0]["rules"], undecided_scope[0]["rules"])
        # Persistent destination rejected.
        persistent = [
            {
                "type": "addRules",
                "behavior": "allow",
                "destination": "userSettings",
                "rules": [{"tool_name": "Bash", "rule_content": "echo *"}],
            }
        ]
        self.assertIsNone(transport.validate_session_bundle(persistent))
        # setMode bypassPermissions rejected.
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
        self.assertIsNone(transport.validate_session_bundle([{"type": "wat"}]))
        # Empty/None rejected.
        self.assertIsNone(transport.validate_session_bundle(None))
        self.assertIsNone(transport.validate_session_bundle([]))

    def test_attach_session_rule_is_tombstoned(self) -> None:
        from agent_bridge_connect.claude_sdk_transport import (
            ClaudeSDKControlTransport,
            ClaudeSDKTransportError,
        )

        transport = ClaudeSDKControlTransport(
            plane=_stub_plane(), task_id="T", run_id="run", session_id="sess"
        )
        with self.assertRaises(ClaudeSDKTransportError) as ctx:
            transport.attach_session_rule({"tool_name": "Bash", "rule_content": "*"})
        self.assertEqual(ctx.exception.code, LEGACY_SESSION_TOOL_RULE_REMOVED)
        self.assertFalse(transport.session_rule_attached())


class HermesExactAdapterTests(unittest.TestCase):
    def test_options_normalized_exactly(self) -> None:
        from agent_bridge_connect.hermes_acp import normalize_permission_options

        options = normalize_permission_options(
            [
                {"optionId": "opt-allow-once", "kind": "allow_once", "name": "Allow once"},
                {"optionId": "opt-session", "kind": "allow_session", "name": "Allow session"},
                {"optionId": "opt-reject", "kind": "reject_once", "name": "Reject"},
            ]
        )
        self.assertEqual(
            [o["native_option_id"] for o in options],
            ["opt-allow-once", "opt-session", "opt-reject"],
        )
        self.assertEqual(options[0]["kind"], "once")
        self.assertEqual(options[1]["kind"], "session")
        self.assertEqual(options[2]["kind"], "deny")

    def test_exact_optionid_returned_verbatim(self) -> None:
        from agent_bridge_connect.hermes_acp import approval_outcome_for_decision

        outcome = approval_outcome_for_decision(
            {"native_option_id": "opt-allow-once-42"}
        )
        self.assertEqual(outcome, {"outcome": {"optionId": "opt-allow-once-42"}})
        # Order/label independence: the optionId is the identity.
        weird = approval_outcome_for_decision(
            {"outcome": {"optionId": "weird-id-Ω"}}
        )
        self.assertEqual(weird["outcome"]["optionId"], "weird-id-Ω")

    def test_no_allow_once_requirement(self) -> None:
        from agent_bridge_connect.hermes_acp import validate_permission_request

        frame = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-7",
                "toolCall": {"id": "tc-1", "title": "terminal"},
                "options": [
                    {"optionId": "only-session", "kind": "allow_session", "name": "Session"},
                ],
            },
        }
        request_id, tool_call = validate_permission_request(frame, session_id="sess-7")
        self.assertEqual(request_id, 5)
        self.assertEqual(tool_call["id"], "tc-1")

    def test_empty_options_fail_closed(self) -> None:
        from agent_bridge_connect.hermes_acp import (
            HermesAcpError,
            validate_permission_request,
        )

        frame = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-7",
                "toolCall": {"id": "tc-1"},
                "options": [],
            },
        }
        with self.assertRaises(HermesAcpError):
            validate_permission_request(frame, session_id="sess-7")

    def test_hermes_choice_builder(self) -> None:
        choices = hermes_offered_choices(
            [
                {"native_option_id": "a", "kind": "once", "label": "A"},
                {"native_option_id": "b", "kind": "deny", "label": "B"},
            ]
        )
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0]["native_option_id"], "a")


class ControlPlaneV2RespondTests(unittest.TestCase):
    def test_v2_request_requires_handle(self) -> None:
        request = ApprovalRequest(
            request_id="approval-v2",
            request_fingerprint="fp-v2",
            rpc_id=1,
            task_id="T",
            executor_run_id="run",
            session_id="sess",
            kind="permission",
            operation="command",
            summary="s",
            approval_version=2,
            offered_choices=codex_offered_choices(
                "command", session_decisions_supported=True
            ),
        )
        with self.assertRaises(ControlPlaneError) as ctx:
            approval_response_payload_v2(
                request,
                choice_kind="once",
                native_option_id="accept",
                decision="not-a-decision",
            )
        self.assertIn("decision", str(ctx.exception))


class TombstoneTests(unittest.TestCase):
    def test_session_tool_rules_module_is_tombstone(self) -> None:
        import agent_bridge_connect.session_tool_rules as module

        # The matcher grammar is gone.
        self.assertFalse(hasattr(module, "normalize_tool_matcher"))
        self.assertFalse(hasattr(module, "issue_session_rule_receipt"))
        self.assertFalse(hasattr(module, "validate_session_rule_request"))
        # The tombstone code and audit-only projection remain.
        self.assertEqual(
            module.LEGACY_SESSION_TOOL_RULE_REMOVED,
            "legacy_session_tool_rule_removed",
        )
        historical = {
            "version": 1,
            "selection_source": "cli_native_approval",
            "scope": "session",
            "state": {"status": "revoked", "revoked": True, "revocation_code": "x", "revoked_at": "t"},
            "matcher": {"display": "Bash(echo probe*)", "kind": "command_pattern"},
            "binding": {"binding_digest": "sha256:" + "a" * 64, "profile_digest": "sha256:" + "b" * 64},
            "audit": {"created_at": "t1", "updated_at": "t2"},
        }
        projection = session_rule_receipt_public_projection(historical)
        self.assertTrue(projection["retired"])
        self.assertEqual(projection["matcher"], "Bash(echo probe*)")
        self.assertNotIn("request_id", projection)


class MigrationGateV2Tests(unittest.TestCase):
    def test_gate_lists_reasons_with_task_ids(self) -> None:
        class Task:
            def __init__(self, id, status, extensions):
                self.id = id
                self.status = status
                self.extensions = extensions
                self.assignee = "claude"

        class Service:
            def __init__(self, tasks):
                self._tasks = tasks
                self.board_root = "/tmp/nonexistent-board"

            def list_tasks(self):
                return self._tasks

        waiting_v1 = Task(
            "V1WAIT",
            "input_required",
            {
                "agentbc.input": {
                    "type": "permission",
                    "status": "waiting",
                    "scope": "single_action",
                    "request_id": "r1",
                }
            },
        )
        active_rule = Task(
            "RULETASK",
            "running",
            {
                "agentbc.session_tool_rule": {
                    "state": {"status": "active", "revoked": False}
                }
            },
        )
        clean = Task("CLEAN", "completed", {})
        gate = legacy_permission_cutover_blocked(Service([waiting_v1, active_rule, clean]))
        self.assertTrue(gate["blocked"])
        ids = {blocker["task_id"] for blocker in gate["blockers"]}
        self.assertEqual(ids, {"V1WAIT", "RULETASK"})
        rule_blocker = next(b for b in gate["blockers"] if b["task_id"] == "RULETASK")
        self.assertIn("active_session_tool_rule", rule_blocker["reasons"])
        # Terminal history with a retired receipt is still listed but not as
        # an active rule.
        historical = Task(
            "OLDTASK",
            "completed",
            {
                "agentbc.session_tool_rule": {
                    "state": {"status": "revoked", "revoked": True}
                }
            },
        )
        gate2 = legacy_permission_cutover_blocked(Service([historical]))
        self.assertTrue(gate2["blocked"])
        self.assertIn(
            "historical_session_tool_rule", gate2["blockers"][0]["reasons"]
        )


def _stub_plane():
    class StubPlane:
        def request_approval(self, message):
            return {"request_id": "approval-stub"}

        def wait_for_decision(self, request_id, timeout_s):
            return {"decision": "decline"}

        def status(self):
            return {"pending_request": None}

    return StubPlane()


if __name__ == "__main__":
    unittest.main()
