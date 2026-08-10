from __future__ import annotations

import copy
import math
import unittest

from agent_bridge_connect.execution_policy import (
    RESOURCE_EXTENSION_KEY,
    SESSION_EXTENSION_KEY,
    attach_execution_policy,
    build_resource_snapshot,
    build_session_snapshot,
    extract_hermes_session_id,
    is_session_cleanup_eligible,
    session_cleanup_blockers,
    validate_execution_policy_extensions,
    validate_resource_snapshot,
    validate_session_snapshot,
)
from agent_bridge_connect.protocol import ABCError


class ResourceSnapshotTests(unittest.TestCase):
    def test_builds_claude_budget_contract(self) -> None:
        snapshot = build_resource_snapshot(
            "claude",
            10,
            source="default",
            created_at="2026-08-10T00:00:00Z",
        )
        self.assertEqual(snapshot["resource"], "max_budget_usd")
        self.assertEqual(snapshot["configured_limit"], 10.0)
        self.assertEqual(snapshot["current_limit"], 10.0)
        self.assertEqual(snapshot["multiplier"], 2)
        self.assertEqual(validate_resource_snapshot(snapshot), [])

    def test_builds_hermes_turn_contract(self) -> None:
        snapshot = build_resource_snapshot(
            "hermes",
            60,
            source="hermes_config",
            created_at="2026-08-10T00:00:00Z",
        )
        self.assertEqual(snapshot["resource"], "max_turns")
        self.assertEqual(snapshot["current_limit"], 60)
        self.assertEqual(validate_resource_snapshot(snapshot), [])

    def test_rejects_non_finite_boolean_and_fractional_turn_limits(self) -> None:
        for executor, value in (
            ("claude", math.inf),
            ("claude", math.nan),
            ("claude", True),
            ("hermes", 1.5),
            ("hermes", False),
        ):
            with self.subTest(executor=executor, value=value):
                with self.assertRaises(ABCError) as raised:
                    build_resource_snapshot(executor, value)
                self.assertEqual(raised.exception.code, "invalid_execution_resource_limit")

    def test_rejects_mutated_multiplier_and_executor_resource_pair(self) -> None:
        snapshot = build_resource_snapshot("claude", 10)
        snapshot["multiplier"] = 3
        snapshot["resource"] = "max_turns"
        errors = validate_resource_snapshot(snapshot)
        self.assertTrue(any("multiplier" in item for item in errors))
        self.assertTrue(any("resource" in item for item in errors))


class SessionSnapshotTests(unittest.TestCase):
    def test_builds_native_retained_claude_contract(self) -> None:
        snapshot = build_session_snapshot(
            "claude",
            retain=True,
            session_id="55d9aa92-1261-493d-bba7-6490b22e17da",
            project_path="/tmp/customer-project",
            session_state="active",
            run_ids=["claude-ABCD-001-run1"],
            created_at="2026-08-10T00:00:00Z",
        )
        self.assertEqual(snapshot["project_mode"], "native")
        self.assertEqual(validate_session_snapshot(snapshot), [])

    def test_builds_ephemeral_claude_contract(self) -> None:
        snapshot = build_session_snapshot(
            "claude",
            retain=False,
            session_id="55d9aa92-1261-493d-bba7-6490b22e17da",
            project_path="/tmp/artifacts/ABCD/ABCD-001/claude",
            session_state="active",
        )
        self.assertEqual(snapshot["project_mode"], "ephemeral")
        self.assertEqual(validate_session_snapshot(snapshot), [])

    def test_allows_pending_hermes_receipt_without_guessing_an_id(self) -> None:
        snapshot = build_session_snapshot("hermes", retain=False)
        self.assertEqual(snapshot["session_id"], "")
        self.assertEqual(snapshot["project_mode"], "none")
        self.assertEqual(validate_session_snapshot(snapshot), [])

    def test_requires_session_id_after_pending(self) -> None:
        snapshot = build_session_snapshot("hermes", retain=False)
        snapshot["session_state"] = "input_required"
        errors = validate_session_snapshot(snapshot)
        self.assertTrue(any("session_id" in item for item in errors))

    def test_rejects_relative_or_wrong_claude_project_mode(self) -> None:
        with self.assertRaises(ABCError):
            build_session_snapshot(
                "claude",
                retain=False,
                project_mode="native",
                project_path="relative/project",
            )

    def test_extracts_only_one_exact_hermes_receipt(self) -> None:
        self.assertEqual(
            extract_hermes_session_id("done\nsession_id: 20260810_010203_a1b2c3\n"),
            "20260810_010203_a1b2c3",
        )
        self.assertIsNone(
            extract_hermes_session_id("session_id: first\nsession_id: second\n")
        )
        self.assertIsNone(extract_hermes_session_id("Session ID: guessed"))

    def test_attach_returns_copy_and_preserves_other_extensions(self) -> None:
        original = {"agentbc.provenance": {"source_platform": "codex"}}
        resources = build_resource_snapshot("hermes", 60)
        session = build_session_snapshot("hermes", retain=False)
        updated = attach_execution_policy(
            original,
            resources=resources,
            session=session,
        )
        self.assertNotIn(RESOURCE_EXTENSION_KEY, original)
        self.assertEqual(updated[RESOURCE_EXTENSION_KEY], resources)
        self.assertEqual(updated[SESSION_EXTENSION_KEY], session)
        self.assertEqual(validate_execution_policy_extensions(updated), [])

    def test_nested_mutation_does_not_change_validation_baseline(self) -> None:
        session = build_session_snapshot("hermes", retain=False)
        invalid = copy.deepcopy(session)
        invalid["cleanup"]["attempts"] = -1
        errors = validate_session_snapshot(invalid)
        self.assertTrue(any("cleanup.attempts" in item for item in errors))

    def test_cleanup_requires_terminal_report_notification_and_closed_lease(self) -> None:
        session = build_session_snapshot(
            "hermes",
            retain=False,
            session_id="20260810_010203_a1b2c3",
            session_state="terminal",
        )
        self.assertTrue(
            is_session_cleanup_eligible(
                task_status="completed",
                lease_state="closed",
                report_written=True,
                notification_recorded=True,
                session=session,
            )
        )
        blockers = session_cleanup_blockers(
            task_status="input_required",
            lease_state="suspended",
            report_written=False,
            notification_recorded=False,
            session=session,
        )
        self.assertEqual(
            blockers,
            [
                "task_not_terminal",
                "run_lease_not_closed",
                "report_not_written",
                "notification_not_recorded",
            ],
        )

    def test_cleanup_never_runs_for_retained_session(self) -> None:
        session = build_session_snapshot(
            "hermes",
            retain=True,
            session_id="20260810_010203_a1b2c3",
            session_state="terminal",
        )
        blockers = session_cleanup_blockers(
            task_status="failed",
            lease_state="closed",
            report_written=True,
            notification_recorded=True,
            session=session,
        )
        self.assertIn("retention_enabled", blockers)


if __name__ == "__main__":
    unittest.main()
