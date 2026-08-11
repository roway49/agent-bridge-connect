from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

from agent_bridge_connect.adapters import (
    SessionCleanupRequest,
    SessionCleanupResult,
)
from agent_bridge_connect.execution_policy import (
    CLEANUP_CAPABILITIES,
    CLEANUP_STATES,
    CLEANUP_STRATEGIES,
    RESOLVED_CLEANUP_STATES,
    build_session_cleanup_receipt,
    build_session_snapshot,
    is_session_cleanup_resolved,
    read_session_cleanup_receipt,
    session_cleanup_blockers,
    transition_session_cleanup,
    validate_session_cleanup_receipt,
    validate_session_snapshot,
)
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.executors.mock import MockExecutor
from agent_bridge_connect.executors.shell import ShellExecutor
from agent_bridge_connect.protocol import ABCError


FIXTURE = Path(__file__).parent / "fixtures" / "session_cleanup_receipts.json"
T0 = "2026-08-11T00:00:00Z"
T1 = "2026-08-11T00:00:01Z"
T2 = "2026-08-11T00:00:02Z"
T3 = "2026-08-11T00:00:03Z"


class CleanupReceiptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_default_receipt_matches_frozen_fixture_and_enums(self) -> None:
        receipt = build_session_cleanup_receipt()
        self.assertEqual(receipt, self.fixture["default"])
        self.assertEqual(
            CLEANUP_CAPABILITIES,
            {"unknown", "supported", "unsupported", "not_applicable"},
        )
        self.assertEqual(
            CLEANUP_STRATEGIES,
            {"none", "retain", "claude_project_purge", "official_session_delete"},
        )
        self.assertEqual(
            CLEANUP_STATES,
            {"not_requested", "retained", "pending", "succeeded", "unsupported", "failed"},
        )
        self.assertEqual(validate_session_cleanup_receipt(receipt), [])

    def test_exact_legacy_receipt_is_read_with_inert_defaults_without_mutation(self) -> None:
        legacy = self.fixture["legacy_minimal"]
        before = copy.deepcopy(legacy)
        self.assertEqual(
            validate_session_cleanup_receipt(legacy, allow_legacy=True),
            [],
        )
        self.assertTrue(validate_session_cleanup_receipt(legacy))
        projected = read_session_cleanup_receipt(legacy)
        self.assertEqual(legacy, before)
        self.assertEqual(projected["capability"], "unknown")
        self.assertEqual(projected["strategy"], "none")
        self.assertFalse(projected["retryable"])
        self.assertEqual(projected["next_attempt_at"], "")

        session = self._session(retain=False)
        session["cleanup"] = copy.deepcopy(legacy)
        self.assertEqual(validate_session_snapshot(session), [])
        self.assertEqual(session["cleanup"], before)

    def test_partial_or_unknown_schema_is_not_treated_as_legacy(self) -> None:
        partial = {"state": "not_requested", "attempts": 0, "version": 1}
        errors = validate_session_cleanup_receipt(partial, allow_legacy=True)
        self.assertTrue(any("missing fields" in item for item in errors))
        self.assertTrue(
            validate_session_cleanup_receipt(
                {**self.fixture["default"], "prompt": "delete everything"}
            )
        )

    def test_strict_types_enums_timestamps_and_error_codes(self) -> None:
        cases = {
            "version": True,
            "capability": "maybe",
            "strategy": "recursive_delete",
            "state": "complete",
            "attempts": False,
            "requested_at": 1,
            "last_attempt_at": "tomorrow",
            "retryable": 1,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.fixture["default"])
                receipt[field] = value
                self.assertTrue(validate_session_cleanup_receipt(receipt))

        receipt = copy.deepcopy(self.fixture["unsupported"])
        receipt["error_code"] = "/Users/example/.agent/private.db"
        self.assertTrue(validate_session_cleanup_receipt(receipt))

    def test_sensitive_or_raw_receipt_fields_are_rejected(self) -> None:
        for field in (
            "prompt",
            "command",
            "raw_output",
            "secret",
            "executor_database_path",
            "session_content",
        ):
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.fixture["default"])
                receipt[field] = "sensitive"
                errors = validate_session_cleanup_receipt(receipt)
                self.assertTrue(any("unsupported fields" in item for item in errors))

    @staticmethod
    def _session(*, retain: bool) -> dict:
        return build_session_snapshot(
            "hermes",
            retain=retain,
            session_id="20260811_000000_a1b2c3",
            session_state="terminal",
            created_at=T0,
        )


class CleanupTransitionTests(unittest.TestCase):
    def _session(self, *, retain: bool = False) -> dict:
        return build_session_snapshot(
            "hermes",
            retain=retain,
            session_id="20260811_000000_a1b2c3",
            session_state="terminal",
            created_at=T0,
        )

    @staticmethod
    def _transition(session: dict, target: str, **kwargs: object) -> dict:
        defaults = {
            "task_status": "completed",
            "lease_state": "closed",
            "report_written": True,
            "notification_recorded": True,
            "occurred_at": T1,
        }
        defaults.update(kwargs)
        receipt = transition_session_cleanup(session, target, **defaults)
        session["cleanup"] = receipt
        return receipt

    def test_retain_terminal_session_resolves_without_cleanup_attempt(self) -> None:
        session = self._session(retain=True)
        retained = self._transition(session, "retained")
        self.assertEqual(retained["capability"], "not_applicable")
        self.assertEqual(retained["strategy"], "retain")
        self.assertEqual(retained["attempts"], 0)
        self.assertTrue(is_session_cleanup_resolved(retained))

    def test_legacy_resolved_receipt_is_an_exact_no_op(self) -> None:
        session = self._session()
        session["cleanup"] = {"state": "succeeded", "attempts": 1}
        before = copy.deepcopy(session["cleanup"])
        result = self._transition(session, "pending", occurred_at=T3)
        self.assertEqual(result, before)
        self.assertEqual(session["cleanup"], before)

    def test_supported_success_path_and_resolved_idempotence(self) -> None:
        session = self._session()
        pending = self._transition(
            session,
            "pending",
            capability="supported",
            strategy="official_session_delete",
        )
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["requested_at"], T1)
        succeeded = self._transition(
            session,
            "succeeded",
            capability="supported",
            strategy="official_session_delete",
            occurred_at=T2,
        )
        before = copy.deepcopy(succeeded)
        repeated = self._transition(session, "pending", occurred_at=T3)
        self.assertEqual(repeated, before)
        self.assertTrue(is_session_cleanup_resolved(repeated))

    def test_unsupported_path_is_resolved_and_not_retried(self) -> None:
        session = self._session()
        self._transition(session, "pending")
        unsupported = self._transition(
            session,
            "unsupported",
            error_code="official_session_delete_unavailable",
            occurred_at=T2,
        )
        self.assertEqual(unsupported["capability"], "unsupported")
        self.assertFalse(unsupported["retryable"])
        self.assertIn(unsupported["state"], RESOLVED_CLEANUP_STATES)
        self.assertEqual(self._transition(session, "unsupported", occurred_at=T3), unsupported)

    def test_retryable_failure_honors_backoff_and_attempt_limit(self) -> None:
        session = self._session()
        self._transition(
            session,
            "pending",
            capability="supported",
            strategy="official_session_delete",
        )
        failed = self._transition(
            session,
            "failed",
            capability="supported",
            strategy="official_session_delete",
            error_code="session_delete_busy",
            retryable=True,
            next_attempt_at=T3,
            occurred_at=T2,
        )
        self.assertFalse(is_session_cleanup_resolved(failed))
        with self.assertRaisesRegex(ABCError, "backoff"):
            self._transition(session, "pending", occurred_at=T2)
        retry = self._transition(session, "pending", occurred_at=T3)
        self.assertEqual(retry["attempts"], 2)

        self._transition(
            session,
            "failed",
            error_code="session_delete_busy",
            retryable=True,
            next_attempt_at="2026-08-11T00:00:05Z",
            occurred_at="2026-08-11T00:00:04Z",
        )
        self._transition(session, "pending", occurred_at="2026-08-11T00:00:05Z")
        self._transition(
            session,
            "failed",
            error_code="session_delete_busy",
            retryable=True,
            next_attempt_at="2026-08-11T00:00:07Z",
            occurred_at="2026-08-11T00:00:06Z",
        )
        with self.assertRaisesRegex(ABCError, "attempt limit"):
            self._transition(session, "pending", occurred_at="2026-08-11T00:00:07Z")

    def test_all_direct_illegal_transitions_fail_closed(self) -> None:
        for target in ("succeeded", "unsupported", "failed"):
            with self.subTest(source="not_requested", target=target):
                with self.assertRaises(ABCError):
                    self._transition(self._session(), target)

        pending_session = self._session()
        self._transition(pending_session, "pending")
        with self.assertRaises(ABCError):
            self._transition(pending_session, "retained")

        failed_session = self._session()
        self._transition(failed_session, "pending")
        self._transition(
            failed_session,
            "failed",
            error_code="permanent_failure",
            retryable=False,
            occurred_at=T2,
        )
        with self.assertRaisesRegex(ABCError, "not retryable"):
            self._transition(failed_session, "pending", occurred_at=T3)
        with self.assertRaises(ABCError):
            self._transition(failed_session, "succeeded", occurred_at=T3)

    def test_all_fail_closed_blockers_prevent_request(self) -> None:
        cases = (
            ({"task_status": "input_required"}, "task_not_terminal"),
            ({"task_status": "needs_recovery"}, "task_not_terminal"),
            ({"task_status": "running"}, "task_not_terminal"),
            ({"lease_state": "active"}, "run_lease_not_closed"),
            ({"lease_state": "stale"}, "run_lease_not_closed"),
            ({"report_written": False}, "report_not_written"),
            ({"notification_recorded": False}, "notification_not_recorded"),
        )
        for override, expected in cases:
            with self.subTest(override=override):
                session = self._session()
                blockers = session_cleanup_blockers(
                    task_status=str(override.get("task_status", "completed")),
                    lease_state=str(override.get("lease_state", "closed")),
                    report_written=bool(override.get("report_written", True)),
                    notification_recorded=bool(override.get("notification_recorded", True)),
                    session=session,
                )
                self.assertIn(expected, blockers)
                with self.assertRaises(ABCError):
                    self._transition(session, "pending", **override)

        for session_state in ("input_required", "needs_recovery", "active"):
            with self.subTest(session_state=session_state):
                session = self._session()
                session["session_state"] = session_state
                with self.assertRaises(ABCError):
                    self._transition(session, "pending")

    def test_invalid_result_metadata_cannot_store_raw_or_private_data(self) -> None:
        session = self._session()
        self._transition(session, "pending")
        for error_code in (
            "Delete failed: raw output",
            "/Users/example/.codex/state.sqlite",
            "token=super-secret",
        ):
            with self.subTest(error_code=error_code):
                with self.assertRaises(ABCError):
                    self._transition(
                        session,
                        "failed",
                        error_code=error_code,
                        retryable=False,
                        occurred_at=T2,
                    )


class AdapterCleanupDefaultsTests(unittest.TestCase):
    def test_cleanup_request_carries_redacted_path_plan_context(self) -> None:
        workspace = {
            "agentbc_root": "/managed/root",
            "executor_project_root": "/managed/root/tasks/artifacts/2026-08-11/TEST/TEST-001/claude",
            "task_code": "TEST",
            "iteration": "001",
            "task_date": "2026-08-11",
        }
        request = SessionCleanupRequest(
            executor="claude",
            session_id="exact-session-id",
            task_id="TEST-001",
            retain=False,
            project_mode="ephemeral",
            strategy="claude_project_purge",
            project_path=workspace["executor_project_root"],
            workspace=workspace,
        )
        self.assertFalse(request.retain)
        self.assertEqual(request.project_mode, "ephemeral")
        self.assertEqual(request.workspace, workspace)
        self.assertNotIn(request.project_path, repr(request))
        self.assertNotIn(workspace["agentbc_root"], repr(request))

    def test_existing_adapters_inherit_non_destructive_unsupported_default(self) -> None:
        adapters = (
            MockExecutor(),
            ShellExecutor(),
            ClaudeExecutor(command=sys.executable, transport="direct"),
            CodexExecutor(command=sys.executable),
            HermesExecutor(command=sys.executable, transport="direct"),
        )
        request = SessionCleanupRequest(
            executor="test",
            session_id="exact-session-id",
            task_id="TEST-001",
            strategy="official_session_delete",
        )
        for adapter in adapters:
            with self.subTest(adapter=type(adapter).__name__):
                result = adapter.cleanup_session(request)
                self.assertIsInstance(result, SessionCleanupResult)
                self.assertEqual(result.state, "unsupported")
                self.assertEqual(result.capability, "unsupported")
                self.assertEqual(result.strategy, "none")
                self.assertFalse(result.retryable)


if __name__ == "__main__":
    unittest.main()
