from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.execution_contract import validate_callback_payload
from agent_bridge_connect.execution_policy import (
    apply_resource_input_decision,
    build_resource_snapshot,
    is_resource_decision_request,
)
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
    consume_permission_grant,
    permission_grant_from_extensions,
    permission_grant_public_projection,
    revoke_permission_grant,
    validate_permission_grant,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService


ISSUED_AT = "2026-08-12T00:00:00Z"
CONSUMED_AT = "2026-08-12T00:01:00Z"
REVOKED_AT = "2026-08-12T00:02:00Z"


class PermissionGrantContractTests(unittest.TestCase):
    def _grant(self) -> dict:
        return build_permission_grant(
            executor="codex",
            task_id="TEST-001",
            input_id="input-abc123",
            session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a",
            source_run_id="codex-run-1",
            grant_id="grant-abc123",
            issued_at=ISSUED_AT,
        )

    def test_builder_freezes_v1_safe_full_single_use_contract(self) -> None:
        grant = self._grant()
        self.assertEqual(grant["version"], 1)
        self.assertEqual(grant["transition"], {"from": "safe", "to": "full"})
        self.assertEqual(
            grant["binding"],
            {
                "executor": "codex",
                "task_id": "TEST-001",
                "input_id": "input-abc123",
                "session_id": "019fe6f1-3ff9-76e3-8001-52b6c0ae357a",
                "source_run_id": "codex-run-1",
                "target_run_id": "",
            },
        )
        self.assertEqual(grant["scope"], {"kind": "next_executor_run", "max_uses": 1})
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["audit"]["source"], "permission_input")
        self.assertEqual(
            permission_grant_from_extensions(
                {PERMISSION_GRANT_EXTENSION_KEY: grant},
                executor="codex",
                task_id="TEST-001",
                input_id="input-abc123",
                session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a",
                source_run_id="codex-run-1",
            ),
            grant,
        )
        self.assertIsNone(permission_grant_from_extensions({}))

    def test_validator_preserves_unknown_additive_fields_losslessly(self) -> None:
        grant = self._grant()
        grant["vendor_extension"] = {"revision": 2}
        grant["binding"]["future_binding"] = "opaque-1"
        grant["scope"]["future_scope"] = {"enabled": True}
        grant["state"]["future_state"] = ["a", "b"]
        grant["audit"]["future_audit"] = "opaque"
        original = copy.deepcopy(grant)

        validated = validate_permission_grant(grant)
        consumed = consume_permission_grant(
            validated,
            "codex-run-2",
            consumed_at=CONSUMED_AT,
        )
        revoked = revoke_permission_grant(
            consumed,
            "task_terminal",
            revoked_at=REVOKED_AT,
        )

        self.assertEqual(validated, original)
        self.assertEqual(revoked["vendor_extension"], original["vendor_extension"])
        self.assertEqual(revoked["binding"]["future_binding"], "opaque-1")
        self.assertEqual(revoked["scope"]["future_scope"], {"enabled": True})
        self.assertEqual(revoked["state"]["future_state"], ["a", "b"])
        self.assertEqual(revoked["audit"]["future_audit"], "opaque")

    def test_future_version_and_sensitive_additions_fail_closed(self) -> None:
        future = self._grant()
        future["version"] = 2
        with self.assertRaises(ABCError) as version_error:
            validate_permission_grant(future)
        self.assertEqual(version_error.exception.code, "permission_grant_version_unsupported")

        sensitive_cases = (
            ("raw_command", "git commit"),
            ("executor_output", "success"),
            ("prompt", "customer request"),
            ("secret", "hidden"),
            ("private_database_path", "/Users/customer/private.sqlite"),
            ("session_content", "private transcript"),
            ("future_note", "token=hidden"),
        )
        for field, value in sensitive_cases:
            with self.subTest(field=field):
                grant = self._grant()
                grant[field] = value
                with self.assertRaises(ABCError) as raised:
                    validate_permission_grant(grant)
                self.assertEqual(raised.exception.code, "permission_grant_sensitive_field")

    def test_only_approval_capable_base_to_full_next_run_and_one_use_are_valid(self) -> None:
        inherited = build_permission_grant(
            executor="codex",
            task_id="TEST-001",
            input_id="input-abc123",
            session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a",
            source_run_id="codex-run-1",
            base_mode="native",
            grant_id="grant-inherit",
            issued_at=ISSUED_AT,
        )
        self.assertEqual(
            validate_permission_grant(inherited)["transition"],
            {"from": "native", "to": "full"},
        )

        invalid = []
        from_full = self._grant()
        from_full["transition"]["from"] = "full"
        invalid.append(from_full)
        to_safe = self._grant()
        to_safe["transition"]["to"] = "safe"
        invalid.append(to_safe)
        wrong_scope = self._grant()
        wrong_scope["scope"]["kind"] = "all_future_runs"
        invalid.append(wrong_scope)
        multiple_uses = self._grant()
        multiple_uses["scope"]["max_uses"] = 2
        invalid.append(multiple_uses)

        for grant in invalid:
            with self.subTest(grant=grant), self.assertRaises(ABCError):
                validate_permission_grant(grant)

    def test_binding_expectations_cover_identity_source_and_consumed_target(self) -> None:
        grant = self._grant()
        validated = validate_permission_grant(
            grant,
            executor="codex",
            task_id="TEST-001",
            input_id="input-abc123",
            session_id="019fe6f1-3ff9-76e3-8001-52b6c0ae357a",
            source_run_id="codex-run-1",
            target_run_id="codex-run-2",
        )
        self.assertEqual(validated, grant)
        mismatches = {
            "executor": "claude",
            "task_id": "OTHER-001",
            "input_id": "input-other",
            "session_id": "session-other",
            "source_run_id": "codex-run-other",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field), self.assertRaises(ABCError) as raised:
                validate_permission_grant(grant, **{field: value})
            self.assertEqual(raised.exception.code, "permission_grant_binding_mismatch")

        consumed = consume_permission_grant(grant, "codex-run-2", consumed_at=CONSUMED_AT)
        self.assertEqual(
            validate_permission_grant(consumed, target_run_id="codex-run-2"),
            consumed,
        )
        with self.assertRaises(ABCError) as target_mismatch:
            validate_permission_grant(consumed, target_run_id="codex-run-other")
        self.assertEqual(target_mismatch.exception.code, "permission_grant_binding_mismatch")

    def test_source_and_target_run_fields_are_required_by_state(self) -> None:
        missing_source = self._grant()
        del missing_source["binding"]["source_run_id"]
        with self.assertRaises(ABCError) as source_error:
            validate_permission_grant(missing_source)
        self.assertEqual(source_error.exception.code, "permission_grant_binding_invalid")

        missing_target = self._grant()
        del missing_target["binding"]["target_run_id"]
        with self.assertRaises(ABCError) as target_error:
            validate_permission_grant(missing_target)
        self.assertEqual(target_error.exception.code, "permission_grant_binding_invalid")

        prematurely_bound = self._grant()
        prematurely_bound["binding"]["target_run_id"] = "codex-run-2"
        with self.assertRaises(ABCError) as issued_error:
            validate_permission_grant(prematurely_bound)
        self.assertEqual(issued_error.exception.code, "permission_grant_state_invalid")

    def test_consume_and_revoke_are_idempotent_but_replays_fail(self) -> None:
        grant = self._grant()
        directly_revoked = revoke_permission_grant(
            grant,
            "input_expired",
            revoked_at=REVOKED_AT,
        )
        self.assertEqual(directly_revoked["state"], {"status": "revoked", "uses": 0})
        self.assertEqual(directly_revoked["binding"]["source_run_id"], "codex-run-1")
        self.assertEqual(directly_revoked["binding"]["target_run_id"], "")
        self.assertEqual(
            revoke_permission_grant(directly_revoked, "input_expired", revoked_at=REVOKED_AT),
            directly_revoked,
        )

        consumed = consume_permission_grant(grant, "codex-run-2", consumed_at=CONSUMED_AT)
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(consumed["state"], {"status": "consumed", "uses": 1})
        self.assertEqual(consumed["binding"]["source_run_id"], "codex-run-1")
        self.assertEqual(consumed["binding"]["target_run_id"], "codex-run-2")
        self.assertEqual(
            consume_permission_grant(consumed, "codex-run-2", consumed_at=REVOKED_AT),
            consumed,
        )
        with self.assertRaises(ABCError) as replay:
            consume_permission_grant(consumed, "codex-run-3", consumed_at=REVOKED_AT)
        self.assertEqual(replay.exception.code, "permission_grant_replay")

        revoked = revoke_permission_grant(
            consumed,
            "task_terminal",
            revoked_at=REVOKED_AT,
        )
        self.assertEqual(
            revoke_permission_grant(revoked, "task_terminal", revoked_at=REVOKED_AT),
            revoked,
        )
        with self.assertRaises(ABCError) as changed_reason:
            revoke_permission_grant(revoked, "dispatch_failed", revoked_at=REVOKED_AT)
        self.assertEqual(changed_reason.exception.code, "permission_grant_replay")
        with self.assertRaises(ABCError) as revoked_replay:
            consume_permission_grant(revoked, "codex-run-2", consumed_at=REVOKED_AT)
        self.assertEqual(revoked_replay.exception.code, "permission_grant_replay")

    def test_public_projection_exposes_no_binding_or_internal_identifiers(self) -> None:
        projection = permission_grant_public_projection(self._grant())
        self.assertEqual(
            projection,
            {
                "version": 1,
                "temporary": True,
                "active": True,
                "source": "permission_input",
                "from_mode": "safe",
                "to_mode": "full",
                "scope": "next_executor_run",
                "max_uses": 1,
                "state": "issued",
                "uses": 0,
                "reason_code": "",
                "issued_at": ISSUED_AT,
                "consumed_at": "",
                "revoked_at": "",
            },
        )
        serialized = repr(projection)
        self.assertNotIn("TEST-001", serialized)
        self.assertNotIn("input-abc123", serialized)
        self.assertNotIn("019fe6f1", serialized)
        self.assertNotIn("grant-abc123", serialized)
        self.assertNotIn("codex-run-1", serialized)

        consumed = consume_permission_grant(
            self._grant(),
            "codex-run-2",
            consumed_at=CONSUMED_AT,
        )
        consumed_projection = permission_grant_public_projection(consumed)
        self.assertNotIn("source_run_id", consumed_projection)
        self.assertNotIn("target_run_id", consumed_projection)
        self.assertNotIn("codex-run-1", repr(consumed_projection))
        self.assertNotIn("codex-run-2", repr(consumed_projection))

        revoked = revoke_permission_grant(
            consumed,
            "task_terminal",
            revoked_at=REVOKED_AT,
        )
        revoked_projection = permission_grant_public_projection(revoked)
        self.assertEqual(revoked_projection["state"], "revoked")
        self.assertFalse(revoked_projection["active"])
        self.assertEqual(revoked_projection["reason_code"], "task_terminal")
        self.assertEqual(revoked_projection["uses"], 1)
        self.assertEqual(revoked_projection["consumed_at"], CONSUMED_AT)
        self.assertEqual(revoked_projection["revoked_at"], REVOKED_AT)


class PermissionCallbackContractTests(unittest.TestCase):
    declared_steps = [
        {"id": 1, "description": "first"},
        {"id": 2, "description": "second"},
    ]

    def _callback(
        self,
        *,
        input_details: dict | None,
        step_results: list[dict] | None = None,
        **extra,
    ) -> dict:
        callback = {
            "version": 1,
            "task_id": "TEST-001",
            "final_state": "input_required",
            "summary": "blocked",
            "step_results": step_results
            if step_results is not None
            else [{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}],
        }
        if input_details is not None:
            callback["input"] = input_details
        callback.update(extra)
        return callback

    def _validate(self, callback: dict):
        return validate_callback_payload(callback, "TEST-001", self.declared_steps)

    def test_valid_permission_marker_requires_full_reason_and_one_blocked_step(self) -> None:
        validation = self._validate(
            self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": " full ",
                    "reason": "The next continuation must update protected Git metadata.",
                }
            )
        )
        self.assertTrue(validation.valid)
        self.assertEqual(
            validation.callback["input"],
            {
                "type": "permission",
                "requested_permission": "full",
                "reason": "The next continuation must update protected Git metadata.",
            },
        )

    def test_permission_reason_is_safely_truncated_instead_of_losing_the_wait(self) -> None:
        exact = "x" * 240
        exact_validation = self._validate(
            self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": "full",
                    "reason": exact,
                }
            )
        )
        self.assertTrue(exact_validation.valid)
        self.assertEqual(exact_validation.callback["input"]["reason"], exact)

        overlong = "x" * 240 + "never persist this tail"
        overlong_validation = self._validate(
            self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": "full",
                    "reason": overlong,
                }
            )
        )
        self.assertTrue(overlong_validation.valid)
        reason = overlong_validation.callback["input"]["reason"]
        self.assertEqual(len(reason), 240)
        self.assertEqual(reason, "x" * 239 + "…")
        self.assertNotIn("never persist this tail", str(overlong_validation.callback))

    def test_permission_marker_rejects_missing_reason_requested_safe_and_block_count(self) -> None:
        cases = {
            "missing_reason": self._callback(
                input_details={"type": "permission", "requested_permission": "full"}
            ),
            "requested_safe": self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": "safe",
                    "reason": "Full access is required.",
                }
            ),
            "zero_blocked": self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": "full",
                    "reason": "Full access is required.",
                },
                step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}],
            ),
            "multiple_blocked": self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": "full",
                    "reason": "Full access is required.",
                },
                step_results=[{"id": 1, "status": "blocked"}, {"id": 2, "status": "blocked"}],
            ),
            "native_flags": self._callback(
                input_details={
                    "type": "permission",
                    "requested_permission": "full",
                    "reason": "Full access is required.",
                    "native_executor_flags": ["--yolo"],
                }
            ),
            "callback_level": self._callback(
                input_details={
                    "type": "permission",
                    "reason": "Full access is required.",
                },
                requested_permission="full",
            ),
        }
        for name, callback in cases.items():
            with self.subTest(name=name):
                self.assertFalse(self._validate(callback).valid)

    def test_message_and_choice_fields_do_not_become_permission_or_resource_grants(self) -> None:
        message = self._validate(
            self._callback(
                input_details={"type": "message", "requested_permission": "full"},
                requested_permission="full",
            )
        )
        self.assertTrue(message.valid)
        self.assertEqual(message.callback["input"]["type"], "message")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, message.callback)

        choice = self._validate(
            self._callback(
                input_details={
                    "type": "choice",
                    "reason": "Choose whether to continue.",
                    "requested_permission": "full",
                    "kind": "resource_limit",
                    "options": [
                        {"label": "Continue", "description": "Continue normally."},
                        {"label": "Stop", "description": "Stop this task."},
                    ],
                }
            )
        )
        self.assertTrue(choice.valid)
        self.assertFalse(is_resource_decision_request(choice.callback["input"]))
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, choice.callback)

    def test_callback_level_permission_text_is_not_persisted_as_permission_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            service = TaskService(root / "record", config={"workspace_root": str(root)})
            task = service.create_task(
                "permission masquerade",
                "codex",
                [{"description": "first"}, {"description": "second"}],
                customer_path=project,
            )
            callback = self._callback(
                input_details={"type": "message"},
                requested_permission="full",
            )
            callback["task_id"] = task.id
            service.finalize_task_from_agent(task.id, callback)
            request = service.get_task(task.id).extensions["agentbc.input"]
            self.assertEqual(request["type"], "message")
            self.assertNotIn("requested_permission", request)
            self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, service.get_task(task.id).extensions)


class ResourceDecisionRecognitionTests(unittest.TestCase):
    def test_only_complete_choice_kind_protocol_tuple_reaches_resource_logic(self) -> None:
        complete = {
            "type": "choice",
            "kind": "resource_limit",
            "response_protocol": "approve_deny",
        }
        self.assertTrue(is_resource_decision_request(complete))
        masquerades = (
            {**complete, "type": "message"},
            {**complete, "type": "permission"},
            {"type": "choice", "kind": "resource_limit"},
            {"type": "choice", "response_protocol": "approve_deny"},
            {"kind": "resource_limit", "response_protocol": "approve_deny"},
        )
        resource = build_resource_snapshot(
            "hermes",
            60,
            source="test",
            created_at=ISSUED_AT,
        )
        for request in masquerades:
            with self.subTest(request=request):
                self.assertFalse(is_resource_decision_request(request))
                with self.assertRaises(ABCError) as raised:
                    apply_resource_input_decision(
                        resource,
                        {**request, "current_limit": 60, "next_limit": 120},
                        "approve",
                        executor="hermes",
                    )
                self.assertEqual(raised.exception.code, "resource_decision_invalid")


if __name__ == "__main__":
    unittest.main()
