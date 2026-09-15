"""FLOW-104-003: shared revival protocol and failed-current-head preflight.

Covers the ``agentbc.revival`` v1 record (core fields, deterministic
validation, serialization, backward-compatible absence handling, redacted
public projection, idempotent replay), the mechanical failed-current-head
revival preflight (status/lease/worker/dispatch/input/cleanup/requirements/
lineage/PathPlan/single-reservation gates, ``allowed_next_actions`` and
``recommended_action``), and the source_report_step_mismatch contract.

Every test is pure and filesystem-free unless a real temporary file is
needed for the readability derivation; no Executor, service or store is
invoked.  Retry filesystem cleanup and failed handoff creation belong to the
sibling implementations and are deliberately not exercised here.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import agent_bridge_connect.revival as revival
from agent_bridge_connect.execution_policy import (
    build_session_snapshot,
    transition_session_cleanup,
)
from agent_bridge_connect.revival import (
    REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS,
    REVIVAL_CLEANUP_SCOPE_NONE,
    REVIVAL_ERROR_CODES,
    REVIVAL_HANDOFF_CLEANUP_SCOPE,
    REVIVAL_OPERATIONS,
    REVIVAL_OPERATION_HANDOFF,
    REVIVAL_OPERATION_RETRY,
    REVIVAL_PROTOCOL_VERSION,
    REVIVAL_RECORD_FIELDS,
    REVIVAL_RESERVATION_CONFLICT,
    REVIVAL_RESERVATION_INVALID,
    REVIVAL_RETRY_CLEANUP_SCOPE,
    REVIVAL_SOURCE_NOT_CHAIN_HEAD,
    REVIVAL_SOURCE_STATUS_INVALID,
    REVIVAL_STABLE_CLEANUP_STATES,
    REVIVAL_WARNING_REVIVAL_REPLAYED,
    REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH,
    RevivalPreflight,
    build_revival_reservation,
    commit_revival_reservation,
    evaluate_revival_preflight,
    open_revival_reservation,
    release_revival_reservation,
    revival_digest,
    revival_from_extensions,
    revival_intent_fingerprint,
    revival_path_plan_digest,
    revival_policy_digest,
    revival_public_view,
    revival_replay_or_reserve,
    revival_status_view,
    revival_step_bindings,
    revival_to_extensions,
    validate_revival_reservation,
)
from agent_bridge_connect.protocol import ABCError

T0 = "2026-09-09T00:00:00Z"


def _elapsed(seconds: int) -> str:
    return f"2026-09-09T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _reservation(
    operation: str = REVIVAL_OPERATION_RETRY,
    *,
    source_task_id: str = "RFT2-001",
    source_attempt_id: str = "run-1",
    steps: list[dict[str, Any]] | None = None,
    now: str = T0,
    **overrides: Any,
) -> dict[str, Any]:
    steps = steps if steps is not None else [{"id": 1, "status": "failed"}]
    record = build_revival_reservation(
        operation=operation,
        source_task_id=source_task_id,
        source_attempt_id=source_attempt_id,
        steps=steps,
        now=now,
        **overrides,
    )
    return record


def _passing_facts(**overrides: Any) -> dict[str, Any]:
    facts = {
        "status": "failed",
        "is_chain_head": True,
        "lease_state": "closed",
        "worker_active": False,
        "dispatch_active": False,
        "input_unresolved": False,
        "session_cleanup_state": "succeeded",
        "requirements_readable": True,
        "lineage_valid": True,
        "path_plan_valid": True,
        "report_state": "readable",
        "failure_kind": "hermes_acp_transport_failed",
        "failure_layer": "transport",
        "source_task_id": "RFT2-001",
        "source_attempt_id": "run-1",
    }
    facts.update(overrides)
    return facts


class RevivalReservationTests(unittest.TestCase):
    def test_record_has_fixed_v1_core_fields(self) -> None:
        record = _reservation()
        self.assertEqual(record["version"], REVIVAL_PROTOCOL_VERSION)
        self.assertEqual(set(record), set(REVIVAL_RECORD_FIELDS))
        self.assertRegex(record["revival_id"], r"^REV-[0-9a-f]{32}$")
        self.assertEqual(record["state"], "reserved")
        self.assertEqual(record["operation"], REVIVAL_OPERATION_RETRY)
        self.assertEqual(record["source_task_id"], "RFT2-001")
        self.assertEqual(record["source_attempt_id"], "run-1")
        self.assertEqual(record["target_task_id"], "")
        self.assertEqual(record["cleanup_scope"], REVIVAL_RETRY_CLEANUP_SCOPE)

    def test_retry_keeps_task_id_and_resets_every_step(self) -> None:
        record = _reservation(
            steps=[{"id": 1, "status": "done"}, {"id": 2, "status": "failed"}]
        )
        self.assertEqual(record["target_task_id"], "")
        self.assertEqual(record["inherited_done_step_ids"], [])
        self.assertEqual(record["resumed_step_ids"], [1, 2])
        with self.assertRaises(ABCError) as ctx:
            build_revival_reservation(
                operation=REVIVAL_OPERATION_RETRY,
                source_task_id="RFT2-001",
                steps=[{"id": 1, "status": "failed"}],
                target_task_id="OTHER-001",
                now=T0,
            )
        self.assertEqual(ctx.exception.code, "revival_operation_invalid")

    def test_retry_cleanup_scope_is_managed_default_artifacts_only(self) -> None:
        record = _reservation()
        self.assertEqual(
            record["cleanup_scope"],
            REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS,
        )

    def test_handoff_locks_done_steps_and_resumes_remainder(self) -> None:
        record = _reservation(
            REVIVAL_OPERATION_HANDOFF,
            steps=[
                {"id": 1, "status": "done"},
                {"id": 2, "status": "completed"},
                {"id": 3, "status": "failed"},
                {"id": 4, "status": "pending"},
            ],
        )
        self.assertEqual(record["cleanup_scope"], REVIVAL_HANDOFF_CLEANUP_SCOPE)
        self.assertEqual(record["cleanup_scope"], REVIVAL_CLEANUP_SCOPE_NONE)
        self.assertEqual(record["inherited_done_step_ids"], [1, 2])
        self.assertEqual(record["resumed_step_ids"], [3, 4])

    def test_validation_rejects_unknown_fields_and_bad_values(self) -> None:
        record = _reservation()
        self.assertEqual(validate_revival_reservation(record), [])
        malformed = dict(record)
        malformed["extra"] = True
        self.assertEqual(validate_revival_reservation(malformed), ["record: unknown fields extra"])
        bad_state = dict(record)
        bad_state["state"] = "committed_early"
        errors = validate_revival_reservation(bad_state)
        self.assertIn("state: must be reserved, committed or released", errors)
        bad_digest = dict(record)
        bad_digest["policy_digest"] = "sha256:zz"
        self.assertTrue(
            any("policy_digest" in error for error in validate_revival_reservation(bad_digest))
        )
        bad_steps = dict(record)
        bad_steps["resumed_step_ids"] = [2, 1]
        self.assertTrue(
            any(
                "resumed_step_ids" in error
                for error in validate_revival_reservation(bad_steps)
            )
        )
        bad_warning = dict(record)
        bad_warning["warnings"] = ["not_a_warning"]
        self.assertTrue(
            any("warnings" in error for error in validate_revival_reservation(bad_warning))
        )

    def test_serialization_round_trips_through_json(self) -> None:
        record = _reservation(
            REVIVAL_OPERATION_HANDOFF,
            steps=[{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}],
        )
        record["warnings"] = ["source_report_step_mismatch"]
        restored = json.loads(json.dumps(record))
        self.assertEqual(validate_revival_reservation(restored), [])
        self.assertEqual(restored, record)

    def test_backward_compatible_absence_projects_none(self) -> None:
        self.assertIsNone(revival_from_extensions({}))
        self.assertIsNone(revival_from_extensions({"agentbc.other": {}}))
        self.assertIsNone(revival_from_extensions(None))
        self.assertIsNone(revival_public_view(None))
        self.assertIsNone(open_revival_reservation({}))

    def test_public_view_is_field_fixed_and_path_free(self) -> None:
        record = _reservation()
        record["path_plan_digest"] = revival_path_plan_digest({"task_code": "RFT2"})
        view = revival_public_view(record)
        self.assertEqual(set(view), set(REVIVAL_RECORD_FIELDS))
        blob = json.dumps(view)
        self.assertNotIn("/Users", blob)
        self.assertNotIn("prompt", blob)
        self.assertNotIn("token", blob)
        self.assertTrue(view["path_plan_digest"].startswith("sha256:"))
        # A malformed record never projects.
        self.assertIsNone(revival_public_view({**record, "operation": "restart"}))

    def test_replay_is_idempotent_for_the_same_intent(self) -> None:
        record = _reservation()
        extensions = revival_to_extensions(record)
        stored, replayed = revival_replay_or_reserve(extensions, record)
        self.assertTrue(replayed)
        self.assertEqual(stored["revival_id"], record["revival_id"])
        self.assertEqual(stored["state"], "reserved")

    def test_replay_creates_a_new_reservation_for_a_different_intent(self) -> None:
        record = _reservation()
        extensions = revival_to_extensions(record)
        other = _reservation(
            REVIVAL_OPERATION_HANDOFF,
            now=_elapsed(1),
        )
        candidate, replayed = revival_replay_or_reserve(extensions, other)
        self.assertFalse(replayed)
        self.assertEqual(candidate["operation"], REVIVAL_OPERATION_HANDOFF)

    def test_intent_fingerprint_is_deterministic_and_scope_bounded(self) -> None:
        first = revival_intent_fingerprint("retry", "RFT2-001", "run-1")
        second = revival_intent_fingerprint("retry", "RFT2-001", "run-1")
        third = revival_intent_fingerprint("retry", "RFT2-002", "run-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertTrue(first.startswith("sha256:"))

    def test_commit_and_release_lifecycle_is_stable(self) -> None:
        record = _reservation()
        committed = commit_revival_reservation(record, now=_elapsed(1))
        self.assertEqual(committed["state"], "committed")
        self.assertEqual(committed["updated_at"], _elapsed(1))
        with self.assertRaises(ABCError) as ctx:
            commit_revival_reservation(committed, now=_elapsed(2))
        self.assertEqual(ctx.exception.code, REVIVAL_RESERVATION_INVALID)
        handoff = _reservation(
            REVIVAL_OPERATION_HANDOFF,
            steps=[{"id": 1, "status": "failed"}],
        )
        with self.assertRaises(ABCError):
            commit_revival_reservation(handoff, now=_elapsed(2))
        bound = commit_revival_reservation(
            handoff,
            target_task_id="RFT2-002",
            target_attempt_id="run-2",
            now=_elapsed(2),
        )
        self.assertEqual(bound["target_task_id"], "RFT2-002")
        self.assertEqual(bound["target_attempt_id"], "run-2")
        released = release_revival_reservation(record, now=_elapsed(3))
        self.assertEqual(released["state"], "released")
        again = release_revival_reservation(released, now=_elapsed(4))
        self.assertEqual(again["updated_at"], _elapsed(3))
        # Committing over a released record stays blocked.
        with self.assertRaises(ABCError):
            commit_revival_reservation(released, now=_elapsed(5))

    def test_open_reservation_blocks_new_intent_but_history_does_not(self) -> None:
        record = _reservation()
        self.assertIsNotNone(open_revival_reservation(revival_to_extensions(record)))
        released = release_revival_reservation(record, now=_elapsed(1))
        self.assertIsNone(open_revival_reservation(revival_to_extensions(released)))
        committed = commit_revival_reservation(record, now=_elapsed(2))
        self.assertIsNone(open_revival_reservation(revival_to_extensions(committed)))


class RevivalDigestTests(unittest.TestCase):
    def test_digest_is_stable_sha256_hex(self) -> None:
        digest = revival_digest("payload")
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(digest, revival_digest(b"payload"))
        self.assertNotEqual(digest, revival_digest("payload2"))

    def test_path_plan_and_policy_digests_are_deterministic(self) -> None:
        workspace = {"task_code": "RFT2", "iteration": "001"}
        self.assertEqual(
            revival_path_plan_digest(workspace),
            revival_path_plan_digest(dict(workspace)),
        )
        self.assertNotEqual(
            revival_path_plan_digest(workspace),
            revival_path_plan_digest({"task_code": "RFT2", "iteration": "002"}),
        )
        policy = revival_policy_digest({"agentbc.permission": {"effective_mode": "full"}})
        self.assertRegex(policy, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            policy,
            revival_policy_digest({"agentbc.permission": {"effective_mode": "full"}}),
        )


class RevivalStepBindingTests(unittest.TestCase):
    def test_task_record_wins_with_mismatch_warning(self) -> None:
        bindings, warnings = revival_step_bindings(
            [{"id": 1, "status": "done"}, {"id": 2, "status": "failed"}],
            [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}],
        )
        self.assertEqual(warnings, (REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH,))
        by_id = {binding["step_id"]: binding for binding in bindings}
        self.assertEqual(by_id[2]["task_status"], "failed")
        self.assertEqual(by_id[2]["report_status"], "done")

    def test_matching_report_and_absent_report_stay_warning_free(self) -> None:
        _, warnings = revival_step_bindings(
            [{"id": 1, "status": "done"}],
            [{"id": 1, "status": "done"}],
        )
        self.assertEqual(warnings, ())
        _, absent = revival_step_bindings([{"id": 1, "status": "pending"}], None)
        self.assertEqual(absent, ())

    def test_bindings_carry_no_step_text(self) -> None:
        bindings, _ = revival_step_bindings(
            [{"id": 1, "status": "done", "description": "secret prompt text"}]
        )
        self.assertEqual(
            bindings,
            ({"step_id": 1, "task_status": "done", "report_status": ""},),
        )


class RevivalFactsTests(unittest.TestCase):
    def _prepare_files(self) -> tuple[str, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        task_file = Path(tmp.name) / "task.md"
        report_file = Path(tmp.name) / "report.md"
        task_file.write_text("# Task Requirements: demo\n", encoding="utf-8")
        report_file.write_text("# Report\n", encoding="utf-8")
        return str(task_file), str(report_file)

    def _workspace(self) -> dict[str, Any]:
        task_file, report_file = self._prepare_files()
        return {
            "customer_dir": True,
            "customer_path": os.path.dirname(task_file),
            "project_root": os.path.dirname(task_file),
            "default_path": os.path.dirname(task_file),
            "agentbc_root": "/tmp/agentbc",
            "artifact_root": os.path.dirname(task_file),
            "report_root": "/tmp/r",
            "task_file": task_file,
            "report_file": report_file,
            "task_code": "RFT2",
            "iteration": "001",
            "task_date": "2026-09-09",
            "executor_project_root": "/tmp/agentbc/tasks/artifacts/2026-09-09/RFT2/RFT2-001/claude",
        }

    def _failed_task(self, **overrides: Any) -> dict[str, Any]:
        snap = build_session_snapshot(
            executor="hermes",
            retain=False,
            session_id="s-1",
            session_state="terminal",
            project_mode="none",
        )
        pending = transition_session_cleanup(
            snap,
            "pending",
            task_status="failed",
            lease_state="closed",
            task_end_dialog_delivered=True,
        )
        snap["cleanup"] = pending
        succeeded = transition_session_cleanup(
            snap,
            "succeeded",
            task_status="failed",
            lease_state="closed",
            task_end_dialog_delivered=True,
            capability="supported",
            strategy="official_session_delete",
            verification={
                "cli": {"status": "absent", "checked_at": T0},
                "desktop_backend": {"status": "absent", "checked_at": T0},
                "desktop_live": {"status": "absent", "checked_at": T0},
            },
        )
        task = {
            "id": "RFT2-001",
            "status": "failed",
            "errors": [
                {
                    "code": "executor_terminal_failure",
                    "details": {
                        "failure": {
                            "kind": "hermes_acp_transport_failed",
                            "layer": "transport",
                        }
                    },
                }
            ],
            "workspace": self._workspace(),
            "extensions": {
                "agentbc.lineage": {"task_code": "RFT2", "iteration_index": 1},
                "agentbc.session": {
                    "retain": False,
                    "session_id": "s-1",
                    "session_state": "terminal",
                    "project_mode": "none",
                    "cleanup": succeeded,
                },
            },
        }
        task.update(overrides)
        return task

    def test_facts_derive_from_authoritative_task_state(self) -> None:
        facts = revival.revival_facts_from_task(
            self._failed_task(), is_chain_head=True, lease_state="closed"
        )
        self.assertEqual(facts["status"], "failed")
        self.assertTrue(facts["is_chain_head"])
        self.assertEqual(facts["lease_state"], "closed")
        self.assertFalse(facts["worker_active"])
        self.assertFalse(facts["input_unresolved"])
        self.assertEqual(facts["session_cleanup_state"], "succeeded")
        self.assertTrue(facts["requirements_readable"])
        self.assertTrue(facts["lineage_valid"])
        self.assertTrue(facts["path_plan_valid"])
        self.assertEqual(facts["report_state"], "readable")
        self.assertEqual(facts["failure_kind"], "hermes_acp_transport_failed")
        self.assertEqual(facts["failure_layer"], "transport")

    def test_defaults_fail_closed(self) -> None:
        facts = revival.revival_facts_from_task({"id": "RFT2-001", "status": "failed"})
        self.assertFalse(facts["is_chain_head"])
        self.assertEqual(facts["lease_state"], "")
        self.assertFalse(facts["requirements_readable"])
        preflight = evaluate_revival_preflight(facts)
        self.assertFalse(preflight.ok)
        self.assertIn("revival_requirements_unreadable", preflight.error_codes)

    def test_unresolved_waiting_input_is_detected(self) -> None:
        task = self._failed_task()
        task["extensions"]["agentbc.input"] = {"status": "waiting", "input_id": "I-1"}
        facts = revival.revival_facts_from_task(
            task, is_chain_head=True, lease_state="closed"
        )
        self.assertTrue(facts["input_unresolved"])
        preflight = evaluate_revival_preflight(facts)
        self.assertIn("revival_input_unresolved", preflight.error_codes)


class RevivalPreflightTests(unittest.TestCase):
    def test_all_mechanical_gates_map_to_stable_codes(self) -> None:
        gates = {
            "status": (_passing_facts(status="completed"), REVIVAL_SOURCE_STATUS_INVALID),
            "head": (
                _passing_facts(is_chain_head=False),
                REVIVAL_SOURCE_NOT_CHAIN_HEAD,
            ),
            "lease": (_passing_facts(lease_state="active"), "revival_run_lease_open"),
            "worker": (_passing_facts(worker_active=True), "revival_worker_active"),
            "dispatch": (
                _passing_facts(dispatch_active=True),
                "revival_dispatch_active",
            ),
            "input": (
                _passing_facts(input_unresolved=True),
                "revival_input_unresolved",
            ),
            "cleanup": (
                _passing_facts(session_cleanup_state="pending"),
                "revival_session_cleanup_unstable",
            ),
            "requirements": (
                _passing_facts(requirements_readable=False),
                "revival_requirements_unreadable",
            ),
            "lineage": (_passing_facts(lineage_valid=False), "revival_lineage_invalid"),
            "path_plan": (
                _passing_facts(path_plan_valid=False),
                "revival_path_plan_invalid",
            ),
        }
        for name, (facts, expected_code) in gates.items():
            with self.subTest(gate=name):
                preflight = evaluate_revival_preflight(facts)
                self.assertFalse(preflight.ok)
                self.assertIn(expected_code, preflight.error_codes)
                self.assertEqual(preflight.allowed_next_actions, ())

    def test_eligible_task_allows_both_operations(self) -> None:
        preflight = evaluate_revival_preflight(_passing_facts())
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.error_codes, ())
        self.assertEqual(preflight.allowed_next_actions, REVIVAL_OPERATIONS)

    def test_needs_recovery_task_allows_the_same_operations(self) -> None:
        preflight = evaluate_revival_preflight(
            _passing_facts(
                status="needs_recovery", session_cleanup_state="not_requested"
            )
        )
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.error_codes, ())
        self.assertEqual(preflight.allowed_next_actions, REVIVAL_OPERATIONS)

        failed_without_cleanup = evaluate_revival_preflight(
            _passing_facts(status="failed", session_cleanup_state="not_requested")
        )
        self.assertFalse(failed_without_cleanup.ok)
        self.assertIn(
            "revival_session_cleanup_unstable",
            failed_without_cleanup.error_codes,
        )

    def test_allowed_next_actions_and_recommended_action_are_mechanical_data(
        self,
    ) -> None:
        preflight = evaluate_revival_preflight(
            _passing_facts(
                failure_kind="hermes_acp_transport_failed", failure_layer="transport"
            )
        )
        self.assertEqual(preflight.recommended_action, REVIVAL_OPERATION_RETRY)
        self.assertEqual(
            preflight.allowed_next_actions, (REVIVAL_OPERATION_RETRY, REVIVAL_OPERATION_HANDOFF)
        )
        view = revival_status_view(preflight)
        self.assertEqual(view["allowed_next_actions"], ["retry", "handoff"])
        self.assertEqual(view["recommended_action"], "retry")
        self.assertTrue(view["eligible"])

    def test_taxonomy_recommendation_never_suppresses_a_valid_choice(self) -> None:
        # A permission failure mechanically recommends handoff, but retry
        # stays allowed: the taxonomy cannot remove a valid user choice.
        preflight = evaluate_revival_preflight(
            _passing_facts(
                failure_kind="permission_denied_by_user", failure_layer="permission"
            ),
            requested_operation=REVIVAL_OPERATION_RETRY,
        )
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.recommended_action, REVIVAL_OPERATION_HANDOFF)
        self.assertIn(REVIVAL_OPERATION_RETRY, preflight.allowed_next_actions)
        # And the inverse: a transport failure recommending retry leaves
        # handoff allowed too.
        inverse = evaluate_revival_preflight(
            _passing_facts(
                failure_kind="hermes_acp_transport_failed", failure_layer="transport"
            ),
            requested_operation=REVIVAL_OPERATION_HANDOFF,
        )
        self.assertTrue(inverse.ok)
        self.assertEqual(inverse.recommended_action, REVIVAL_OPERATION_RETRY)
        self.assertIn(REVIVAL_OPERATION_HANDOFF, inverse.allowed_next_actions)

    def test_incomplete_normal_exit_allows_audited_retry_and_handoff(self) -> None:
        preflight = evaluate_revival_preflight(
            _passing_facts(
                failure_kind="incomplete_normal_exit",
                failure_layer="flow_contract",
            )
        )
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.allowed_next_actions, REVIVAL_OPERATIONS)
        self.assertEqual(preflight.recommended_action, REVIVAL_OPERATION_HANDOFF)
        view = revival_status_view(preflight)
        self.assertEqual(view["allowed_next_actions"], ["retry", "handoff"])
        self.assertTrue(view["eligible"])

    def test_unknown_operation_is_rejected(self) -> None:
        preflight = evaluate_revival_preflight(
            _passing_facts(), requested_operation="restart"
        )
        self.assertFalse(preflight.ok)
        self.assertIn("revival_operation_invalid", preflight.error_codes)

    def test_open_reservation_replays_same_intent_idempotently(self) -> None:
        record = _reservation()
        preflight = evaluate_revival_preflight(
            _passing_facts(reservation_raw=record),
            requested_operation=REVIVAL_OPERATION_RETRY,
        )
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.replay_revival_id, record["revival_id"])
        self.assertIn(REVIVAL_WARNING_REVIVAL_REPLAYED, preflight.warnings)
        # Same intent, no requested operation: the replay is still detected
        # and the allowed set stays mechanical.
        without_request = evaluate_revival_preflight(
            _passing_facts(reservation_raw=record)
        )
        self.assertTrue(without_request.ok)
        self.assertEqual(without_request.replay_revival_id, record["revival_id"])
        self.assertEqual(without_request.allowed_next_actions, REVIVAL_OPERATIONS)

    def test_open_reservation_conflicts_with_a_different_intent(self) -> None:
        record = _reservation()
        preflight = evaluate_revival_preflight(
            _passing_facts(reservation_raw=record),
            requested_operation=REVIVAL_OPERATION_HANDOFF,
        )
        self.assertFalse(preflight.ok)
        self.assertIn(REVIVAL_RESERVATION_CONFLICT, preflight.error_codes)
        other_task = _reservation(source_task_id="OTHER-001")
        blocked = evaluate_revival_preflight(_passing_facts(reservation_raw=other_task))
        self.assertFalse(blocked.ok)
        self.assertIn(REVIVAL_RESERVATION_CONFLICT, blocked.error_codes)

    def test_malformed_reservation_fails_closed(self) -> None:
        for raw in ({"version": 99}, "not-a-record", [1, 2]):
            with self.subTest(raw=raw):
                preflight = evaluate_revival_preflight(
                    _passing_facts(reservation_raw=raw)
                )
                self.assertFalse(preflight.ok)
                self.assertIn(REVIVAL_RESERVATION_INVALID, preflight.error_codes)

    def test_resolved_reservations_do_not_block(self) -> None:
        record = _reservation()
        released = release_revival_reservation(record, now=_elapsed(1))
        preflight = evaluate_revival_preflight(_passing_facts(reservation_raw=released))
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.allowed_next_actions, REVIVAL_OPERATIONS)
        committed = commit_revival_reservation(record, now=_elapsed(2))
        after_commit = evaluate_revival_preflight(
            _passing_facts(reservation_raw=committed)
        )
        self.assertTrue(after_commit.ok)

    def test_report_states_degrade_to_warnings(self) -> None:
        absent = evaluate_revival_preflight(_passing_facts(report_state="absent"))
        self.assertTrue(absent.ok)
        self.assertIn("source_report_absent", absent.warnings)
        unreadable = evaluate_revival_preflight(
            _passing_facts(report_state="unreadable")
        )
        self.assertTrue(unreadable.ok)
        self.assertIn("source_report_unreadable", unreadable.warnings)

    def test_unstable_cleanup_states_are_rejected_and_stable_accepted(self) -> None:
        for state in ("pending", "failed", "not_requested", "unknown"):
            preflight = evaluate_revival_preflight(
                _passing_facts(session_cleanup_state=state)
            )
            self.assertFalse(preflight.ok, state)
            self.assertIn("revival_session_cleanup_unstable", preflight.error_codes)
        for state in sorted(REVIVAL_STABLE_CLEANUP_STATES):
            preflight = evaluate_revival_preflight(
                _passing_facts(session_cleanup_state=state)
            )
            self.assertTrue(preflight.ok, state)

    def test_preflight_projection_is_bounded_and_json_safe(self) -> None:
        preflight = evaluate_revival_preflight(_passing_facts())
        self.assertIsInstance(preflight, RevivalPreflight)
        blob = json.dumps(preflight.to_dict())
        self.assertNotIn("/Users", blob)
        self.assertLessEqual(len(blob), 512)


class RevivalStatusViewTests(unittest.TestCase):
    def test_status_view_projects_reservation_summary(self) -> None:
        record = _reservation()
        preflight = evaluate_revival_preflight(
            _passing_facts(reservation_raw=record),
            requested_operation=REVIVAL_OPERATION_RETRY,
        )
        view = revival_status_view(preflight, record)
        self.assertEqual(
            view["reservation"],
            {
                "revival_id": record["revival_id"],
                "operation": "retry",
                "state": "reserved",
                "created_at": T0,
            },
        )
        self.assertIn(REVIVAL_WARNING_REVIVAL_REPLAYED, view["warnings"])
        # No reservation projected when none supplied.
        plain = revival_status_view(evaluate_revival_preflight(_passing_facts()))
        self.assertIsNone(plain["reservation"])

    def test_status_view_omits_malformed_reservation(self) -> None:
        preflight = evaluate_revival_preflight(_passing_facts())
        self.assertIsNone(
            revival_status_view(preflight, {"not": "a record"})["reservation"]
        )

    def test_error_code_vocabulary_is_complete(self) -> None:
        observed = {
            REVIVAL_SOURCE_STATUS_INVALID,
            REVIVAL_SOURCE_NOT_CHAIN_HEAD,
            "revival_run_lease_open",
            "revival_worker_active",
            "revival_dispatch_active",
            "revival_input_unresolved",
            "revival_session_cleanup_unstable",
            "revival_requirements_unreadable",
            "revival_lineage_invalid",
            "revival_path_plan_invalid",
            REVIVAL_RESERVATION_CONFLICT,
            REVIVAL_RESERVATION_INVALID,
            "revival_operation_invalid",
        }
        self.assertEqual(observed, REVIVAL_ERROR_CODES)


if __name__ == "__main__":
    unittest.main()
