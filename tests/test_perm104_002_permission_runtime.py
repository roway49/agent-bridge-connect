"""PERM-104-002: authoritative runtime permission capability closure tests.

Covers the ``agentbc.permission_runtime`` v1 envelope (three ``full``
sources, fixed hierarchy, strict lifecycle), the stable block ledger and
convergence contract, the sanitized projections, and the Hermes canary:
agent callbacks/stderr/exit codes are diagnostics only and can never create
a permission input or grant.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
)
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.permission_runtime import (
    HOST_CONTAINMENT_UNLIFTABLE,
    LINKED_WORKTREE_CAPABILITY_INVALID,
    PERMISSION_ACTION_ALREADY_BLOCKED,
    PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
    PERMISSION_ESCALATION_INEFFECTIVE,
    PERMISSION_PROTOCOL_HANDSHAKE_FAILED,
    PERMISSION_PROTOCOL_SHAPE_UNSUPPORTED,
    PERMISSION_PROTOCOL_UNAVAILABLE,
    PERMISSION_RUNTIME_BLOCK_CODES,
    PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE,
    PERMISSION_RUNTIME_DOMAINS,
    PERMISSION_RUNTIME_EXTENSION_KEY,
    PERMISSION_RUNTIME_SOURCES,
    PERMISSION_RUNTIME_STATES,
    PERMISSION_RUNTIME_VERSION,
    PERMISSION_TRANSPORT_UNSUPPORTED,
    PERMISSION_TRANSPORT_LOST,
    action_fingerprint,
    activate_permission_runtime_record,
    authorize_permission_runtime_record,
    block_fingerprint,
    block_ledger_public_projection,
    block_permission_runtime_record,
    build_permission_runtime_record,
    classify_block_domain,
    converge_approved_block,
    host_profile_digest,
    load_block_ledger,
    path_plan_digest,
    permission_runtime_from_extensions,
    permission_runtime_public_projection,
    record_block_decision,
    remember_block_outcome,
    replay_blocked_after_approval,
    runtime_source_for_permission,
    save_block_ledger,
    supersede_block,
    validate_permission_runtime_record,
    verify_permission_runtime_record,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService

ISSUED_AT = "2026-08-28T00:00:00Z"
PLAN_DIGEST = "sha256:" + "0" * 64
PROFILE_DIGEST = "sha256:" + "1" * 64


def _base_record(**overrides: str) -> dict:
    values = {
        "task_id": "E52M-001",
        "chain_head_id": "E52M-001",
        "executor": "hermes",
        "executor_run_id": "worker-run-1",
        "session_id": "",
        "permission_source": "explicit_task",
        "path_plan_digest": PLAN_DIGEST,
        "host_profile_digest": PROFILE_DIGEST,
        "runtime_id": "rt-test-1",
        "created_at": ISSUED_AT,
    }
    values.update(overrides)
    return build_permission_runtime_record(**values)


class PermissionRuntimeRecordTests(unittest.TestCase):
    def test_prepared_record_freezes_v1_contract(self) -> None:
        record = _base_record()
        self.assertEqual(record["version"], PERMISSION_RUNTIME_VERSION)
        self.assertEqual(record["mode"], "full")
        self.assertEqual(record["state"]["status"], "prepared")
        self.assertEqual(record["binding"]["task_id"], "E52M-001")
        self.assertEqual(record["binding"]["chain_head_id"], "E52M-001")
        self.assertEqual(record["binding"]["permission_source"], "explicit_task")
        self.assertEqual(record["scope"]["path_plan_digest"], PLAN_DIGEST)
        self.assertEqual(record["scope"]["host_profile_digest"], PROFILE_DIGEST)
        self.assertEqual(
            list(record["hierarchy"]),
            [
                "executor_policy",
                "agentbc_policy",
                "runner_pathplan",
                "host_containment",
                "linked_worktree_metadata",
            ],
        )

    def test_fixed_hierarchy_domains_are_exactly_the_five(self) -> None:
        self.assertEqual(
            PERMISSION_RUNTIME_DOMAINS,
            (
                "executor_policy",
                "agentbc_policy",
                "runner_pathplan",
                "host_containment",
                "linked_worktree_metadata",
            ),
        )

    def test_sources_and_states_are_frozen(self) -> None:
        self.assertEqual(
            PERMISSION_RUNTIME_SOURCES,
            {
                "explicit_task",
                "one_shot_permission_grant",
                "inherited_task",
                "task_elevation",
            },
        )
        self.assertEqual(
            PERMISSION_RUNTIME_STATES,
            {"prepared", "authorized", "activated", "verified", "blocked"},
        )

    def test_strict_lifecycle_transitions(self) -> None:
        record = _base_record()
        with self.assertRaises(ABCError):
            verify_permission_runtime_record(record)  # prepared -> verified denied
        with self.assertRaises(ABCError):
            activate_permission_runtime_record(record, host_profile_digest=PROFILE_DIGEST)
        authorized = authorize_permission_runtime_record(
            record, decision="approve", request_id="req-1", grant_id="grant-1"
        )
        self.assertEqual(authorized["state"]["status"], "authorized")
        self.assertEqual(authorized["binding"]["request_id"], "req-1")
        with self.assertRaises(ABCError):
            authorize_permission_runtime_record(  # double authorization denied
                authorized, decision="approve", request_id="req-2"
            )
        activated = activate_permission_runtime_record(
            authorized, host_profile_digest=PROFILE_DIGEST
        )
        self.assertEqual(activated["state"]["status"], "activated")
        # Verification binds the real official session id in production; a
        # record without that binding can never be verified.
        with self.assertRaises(ABCError):
            verify_permission_runtime_record(activated)
        verified = verify_permission_runtime_record(
            activated, session_id="sess-official-1"
        )
        self.assertEqual(verified["state"]["status"], "verified")
        self.assertEqual(verified["binding"]["session_id"], "sess-official-1")
        self.assertTrue(verified["audit"]["verified_at"])

    def test_activation_requires_same_host_profile(self) -> None:
        authorized = authorize_permission_runtime_record(
            _base_record(), decision="approve", request_id="req-1"
        )
        with self.assertRaises(ABCError) as raised:
            activate_permission_runtime_record(
                authorized,
                host_profile_digest="sha256:" + "2" * 64,
            )
        self.assertEqual(raised.exception.code, "permission_runtime_profile_mismatch")

    def test_deny_never_authorizes(self) -> None:
        with self.assertRaises(ABCError):
            authorize_permission_runtime_record(
                _base_record(), decision="deny", request_id="req-1"
            )

    def test_block_with_stable_code_and_domain(self) -> None:
        record = block_permission_runtime_record(
            _base_record(),
            code=HOST_CONTAINMENT_UNLIFTABLE,
            domain="host_containment",
        )
        self.assertEqual(record["state"]["status"], "blocked")
        self.assertEqual(record["state"]["block_code"], HOST_CONTAINMENT_UNLIFTABLE)
        self.assertEqual(record["state"]["block_domain"], "host_containment")
        with self.assertRaises(ABCError):
            block_permission_runtime_record(
                _base_record(), code="not-a-stable-code", domain="host_containment"
            )

    def test_stable_codes_include_protocol_failures(self) -> None:
        self.assertEqual(
            PERMISSION_RUNTIME_BLOCK_CODES,
            {
                PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE,
                PERMISSION_TRANSPORT_UNSUPPORTED,
                PERMISSION_PROTOCOL_UNAVAILABLE,
                PERMISSION_PROTOCOL_SHAPE_UNSUPPORTED,
                PERMISSION_PROTOCOL_HANDSHAKE_FAILED,
                PERMISSION_TRANSPORT_LOST,
                PERMISSION_ESCALATION_INEFFECTIVE,
                PERMISSION_ACTION_ALREADY_BLOCKED,
                LINKED_WORKTREE_CAPABILITY_INVALID,
                HOST_CONTAINMENT_UNLIFTABLE,
                PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
            },
        )

    def test_envelope_rejects_sensitive_material(self) -> None:
        with self.assertRaises(ABCError) as raised:
            build_permission_runtime_record(
                task_id="E52M-001",
                chain_head_id="E52M-001",
                executor="hermes",
                executor_run_id="worker-run-1",
                session_id="",
                permission_source="explicit_task",
                path_plan_digest=PLAN_DIGEST,
                host_profile_digest=PROFILE_DIGEST,
                operation="rm -rf /tmp/secret",
            )
        self.assertEqual(raised.exception.code, "permission_runtime_action_invalid")
        with self.assertRaises(ABCError):
            validate_permission_runtime_record(
                {
                    **_base_record(),
                    "binding": {
                        **_base_record()["binding"],
                        "executor": "/private/etc/passwd",
                    },
                }
            )

    def test_public_projection_is_sanitized(self) -> None:
        record = _base_record(permission_source="inherited_task")
        projection = permission_runtime_public_projection(record)
        self.assertEqual(projection["mode"], "full")
        self.assertEqual(projection["permission_source"], "inherited_task")
        self.assertEqual(projection["state"], "prepared")
        self.assertEqual(projection["path_plan_digest"], PLAN_DIGEST)
        self.assertNotIn("task_id", projection)
        self.assertNotIn("executor_run_id", projection)
        self.assertNotIn("runtime_id", projection)
        self.assertNotIn("request_id", projection)
        self.assertNotIn("grant_id", projection)
        self.assertNotIn("session_id", projection)

    def test_extensions_round_trip_and_dual_read(self) -> None:
        record = _base_record()
        extensions = {PERMISSION_RUNTIME_EXTENSION_KEY: record}
        loaded = permission_runtime_from_extensions(extensions)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["state"]["status"], "prepared")
        self.assertIsNone(permission_runtime_from_extensions(None))
        self.assertIsNone(permission_runtime_from_extensions({}))


class PermissionRuntimeSourceTests(unittest.TestCase):
    def test_three_full_sources_resolve(self) -> None:
        for source in ("explicit_task", "inherited_task"):
            record = build_permission_record(
                explicit_mode="full" if source == "explicit_task" else None,
                inherited=(
                    build_permission_record(explicit_mode="full")
                    if source == "inherited_task"
                    else None
                ),
            )
            self.assertEqual(runtime_source_for_permission(record), source)
        one_shot = {
            "requested_mode": "full",
            "effective_mode": "full",
            "selection_source": "one_shot_permission_grant",
            "base_mode": "safe",
            "temporary": True,
        }
        self.assertEqual(
            runtime_source_for_permission(one_shot), "one_shot_permission_grant"
        )

    def test_non_full_base_needs_no_runtime_capability(self) -> None:
        for mode in ("inherit", "safe"):
            record = build_permission_record(explicit_mode=mode)
            self.assertIsNone(runtime_source_for_permission(record))

    def test_unproven_full_source_fails_closed(self) -> None:
        with self.assertRaises(ABCError) as raised:
            runtime_source_for_permission(
                {"effective_mode": "full", "selection_source": "configured_default"}
            )
        self.assertEqual(
            raised.exception.code, PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE
        )

    def test_concrete_full_never_requests_full_again(self) -> None:
        # A concrete full base resolves directly; it never produces the old
        # escalation codes.
        record = build_permission_record(explicit_mode="full")
        self.assertEqual(record["approval_policy"], "none")
        self.assertEqual(runtime_source_for_permission(record), "explicit_task")


class PermissionRuntimeDigestTests(unittest.TestCase):
    def test_path_plan_digest_is_stable_and_sanitized(self) -> None:
        workspace = {
            "customer_dir": True,
            "project_root": "/Users/user/project",
            "artifact_root": "/Users/user/project",
            "report_root": "/Users/user/workspace/tasks/report",
            "agentbc_root": "/Users/user/workspace",
            "customer_path": "/Users/user/project",
        }
        digest = path_plan_digest(workspace)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(len(digest), 7 + 64)
        self.assertEqual(path_plan_digest(workspace), digest)
        changed = dict(workspace)
        changed["project_root"] = "/Users/user/other"
        self.assertNotEqual(path_plan_digest(changed), digest)
        self.assertNotIn("/Users/user", digest)

    def test_host_profile_digest_is_deterministic(self) -> None:
        facts = {"platform": "darwin", "seatbelt_available": True}
        self.assertEqual(host_profile_digest(facts), host_profile_digest(facts))
        self.assertNotEqual(
            host_profile_digest(facts),
            host_profile_digest({"platform": "darwin", "seatbelt_available": False}),
        )

    def test_fingerprints_are_stable_and_bounded(self) -> None:
        action = action_fingerprint(
            executor="claude", session_id="sess-1", operation="Bash"
        )
        self.assertEqual(
            action,
            action_fingerprint(
                executor="claude", session_id="sess-1", operation="Bash"
            ),
        )
        self.assertNotEqual(
            action,
            action_fingerprint(
                executor="claude", session_id="sess-1", operation="Write"
            ),
        )
        block = block_fingerprint(
            task_id="E52M-001",
            session_id="sess-1",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        self.assertTrue(block.startswith("fp-"))
        self.assertEqual(len(block), 3 + 40)


class PermissionRuntimeLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = {"version": 1, "entries": {}}

    def _entry(self, *, decision: str = "approve", result: str = "") -> dict:
        action = action_fingerprint(
            executor="hermes", session_id="sess-1", operation="git-commit"
        )
        fingerprint = block_fingerprint(
            task_id="E52M-001",
            session_id="sess-1",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        remember_block_outcome(
            self.ledger,
            fingerprint=fingerprint,
            task_id="E52M-001",
            session_id="sess-1",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
            decision=decision,
            execution_result=result,
            domain_changed=False,
        )
        return {
            "fingerprint": fingerprint,
            "action_fingerprint": action,
        }

    def test_replay_after_approve_converges_when_still_blocked(self) -> None:
        entry = self._entry(decision="approve", result="")
        self.assertIsNotNone(
            replay_blocked_after_approval(
                self.ledger,
                fingerprint=entry["fingerprint"],
                task_id="E52M-001",
                session_id="sess-1",
                action_fingerprint_value=entry["action_fingerprint"],
                domain="host_containment",
                profile_digest=PROFILE_DIGEST,
            )
        )
        entry = self._entry(decision="approve", result="blocked")
        self.assertIsNotNone(
            replay_blocked_after_approval(
                self.ledger,
                fingerprint=entry["fingerprint"],
                task_id="E52M-001",
                session_id="sess-1",
                action_fingerprint_value=entry["action_fingerprint"],
                domain="host_containment",
                profile_digest=PROFILE_DIGEST,
            )
        )

    def test_succeeded_execution_never_replays(self) -> None:
        entry = self._entry(decision="approve", result="succeeded")
        self.assertIsNone(
            replay_blocked_after_approval(
                self.ledger,
                fingerprint=entry["fingerprint"],
                task_id="E52M-001",
                session_id="sess-1",
                action_fingerprint_value=entry["action_fingerprint"],
                domain="host_containment",
                profile_digest=PROFILE_DIGEST,
            )
        )

    def test_deny_never_replays(self) -> None:
        entry = self._entry(decision="deny")
        self.assertIsNone(
            replay_blocked_after_approval(
                self.ledger,
                fingerprint=entry["fingerprint"],
                task_id="E52M-001",
                session_id="sess-1",
                action_fingerprint_value=entry["action_fingerprint"],
                domain="host_containment",
                profile_digest=PROFILE_DIGEST,
            )
        )

    def test_domain_or_profile_change_supersedes(self) -> None:
        entry = self._entry(decision="approve")
        supersede_block(self.ledger, entry["fingerprint"], "runner_pathplan")
        self.assertTrue(
            self.ledger["entries"][entry["fingerprint"]]["domain_changed"]
        )
        self.assertIsNone(
            replay_blocked_after_approval(
                self.ledger,
                fingerprint=entry["fingerprint"],
                task_id="E52M-001",
                session_id="sess-1",
                action_fingerprint_value=entry["action_fingerprint"],
                domain="runner_pathplan",
                profile_digest=PROFILE_DIGEST,
            )
        )

    def test_ledger_persistence_and_replay_across_restart(self) -> None:
        entry = self._entry(decision="approve", result="blocked")
        save_block_ledger(self.root, self.ledger)
        reloaded = load_block_ledger(self.root)
        self.assertEqual(reloaded["version"], 1)
        self.assertIn(entry["fingerprint"], reloaded["entries"])
        replay = replay_blocked_after_approval(
            reloaded,
            fingerprint=entry["fingerprint"],
            task_id="E52M-001",
            session_id="sess-1",
            action_fingerprint_value=entry["action_fingerprint"],
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        self.assertIsNotNone(replay)

    def test_public_ledger_projection_is_bounded(self) -> None:
        self._entry(decision="approve")
        projection = block_ledger_public_projection(self.ledger)
        self.assertEqual(projection["entry_count"], 1)
        self.assertNotIn("task_id", projection)
        self.assertNotIn("fingerprint", projection)


class PermissionRuntimeConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_converged_block_returns_stable_code_and_persists(self) -> None:
        action = action_fingerprint(
            executor="hermes", session_id="sess-1", operation="git-commit"
        )
        fingerprint = block_fingerprint(
            task_id="E52M-001",
            session_id="sess-1",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        record_block_decision(
            self.root,
            fingerprint=fingerprint,
            task_id="E52M-001",
            session_id="sess-1",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
            decision="approve",
        )
        code = converge_approved_block(
            self.root,
            task_id="E52M-001",
            session_id="sess-1",
            executor="hermes",
            operation="git-commit",
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        self.assertEqual(code, PERMISSION_ESCALATION_INEFFECTIVE)
        ledger = load_block_ledger(self.root)
        self.assertEqual(
            ledger["entries"][fingerprint]["execution_result"], "blocked"
        )

    def test_first_block_is_not_converged(self) -> None:
        self.assertIsNone(
            converge_approved_block(
                self.root,
                task_id="E52M-001",
                session_id="sess-1",
                executor="hermes",
                operation="git-commit",
                domain="host_containment",
                profile_digest=PROFILE_DIGEST,
            )
        )

    def test_classify_block_domain_requires_trusted_evidence(self) -> None:
        self.assertEqual(
            classify_block_domain({"host_containment": True}),
            "host_containment",
        )
        self.assertEqual(
            classify_block_domain({"source": "linked_worktree_metadata"}),
            "linked_worktree_metadata",
        )
        with self.assertRaises(ABCError) as raised:
            classify_block_domain(
                {"stderr": "operation not permitted", "exit_code": 1}
            )
        self.assertEqual(
            raised.exception.code, PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE
        )
        with self.assertRaises(ABCError):
            classify_block_domain(None)


class PermissionRuntimeConvergenceSideEffectTests(unittest.TestCase):
    """PERM-104-002-R1 review fix: a repeated identical block after Approve
    must converge with ZERO new inputs/grants/workers/continuations."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "board"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace")},
        )
        created = self.service.create_task(
            "convergence canary",
            "hermes",
            [{"id": 1, "description": "one"}],
            session_id="",
            customer_path="default path",
        )
        self.task_id = created.id

    def _snapshot(self) -> dict:
        task = self.service.get_task(self.task_id)
        extensions = task.extensions or {}
        return {
            "status": task.status,
            "inputs": len(extensions.get("agentbc.input", {}).get("inputs", [])
            if isinstance(extensions.get("agentbc.input"), dict)
            else []),
            "grants": 1 if extensions.get("agentbc.permission_grant") else 0,
            "continuations": 1 if extensions.get("agentbc.continuation") else 0,
        }

    def test_repeat_block_after_approve_converges_without_new_inputs(self) -> None:
        action = action_fingerprint(
            executor="hermes",
            session_id="sess-canary",
            operation="git-commit",
        )
        block_fp = block_fingerprint(
            task_id=self.task_id,
            session_id="sess-canary",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        # First approve is recorded (pending execution result).
        record_block_decision(
            self.root,
            fingerprint=block_fp,
            task_id=self.task_id,
            session_id="sess-canary",
            action_fingerprint_value=action,
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
            decision="approve",
        )
        before = self._snapshot()
        # The identical block reappears after the approved escalation still
        # failed: converge to the stable code with zero side effects.
        code = converge_approved_block(
            self.root,
            task_id=self.task_id,
            session_id="sess-canary",
            executor="hermes",
            operation="git-commit",
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        self.assertEqual(code, PERMISSION_ESCALATION_INEFFECTIVE)
        after = self._snapshot()
        self.assertEqual(before["inputs"], after["inputs"])
        self.assertEqual(before["grants"], after["grants"])
        self.assertEqual(before["continuations"], after["continuations"])
        # Convergence is stable: a third occurrence replays the same code.
        code_again = converge_approved_block(
            self.root,
            task_id=self.task_id,
            session_id="sess-canary",
            executor="hermes",
            operation="git-commit",
            domain="host_containment",
            profile_digest=PROFILE_DIGEST,
        )
        self.assertEqual(code_again, PERMISSION_ESCALATION_INEFFECTIVE)
        final = self._snapshot()
        self.assertEqual(before["inputs"], final["inputs"])
        self.assertEqual(before["grants"], final["grants"])
        self.assertEqual(before["continuations"], final["continuations"])


class PermissionRuntimeHermesCanaryTests(unittest.TestCase):
    """Hermes canary: no approval loop - callbacks and stderr are inert.

    PERM-104-002 review fix: every canary task is created with a config that
    pins ``workspace_root`` to a temporary directory.  Without it,
    ``TaskService`` resolves the real AgentBC workspace from the default
    config root and canary task creation wrote task/report files (and raised
    permission errors on restricted hosts) inside
    ``/Users/<user>/Documents/AgentBC/workspace``.  Unit tests must never
    touch the real workspace.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "board"
        self.board.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace")},
        )

    def _task(self) -> str:
        created = self.service.create_task(
            "hermes canary",
            "hermes",
            [{"id": 1, "description": "one"}],
            session_id="",
            customer_path="default path",
        )
        return created.id

    def test_callback_with_requested_full_creates_no_input_or_grant(self) -> None:
        task_id = self._task()
        callback = {
            "final_state": "completed",
            "summary": "done",
            "requested_permission": "full",
            "stderr": "operation not permitted",
            "executor_run_id": "hermes-run-1",
        }
        self.service.record_agent_callback(task_id, callback)
        task = self.service.get_task(task_id)
        extensions = task.extensions or {}
        self.assertIn("agentbc.completion_intent", extensions)
        self.assertNotIn("agentbc.input", extensions)
        self.assertNotIn("agentbc.approval", extensions)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, extensions)
        self.assertNotIn(PERMISSION_RUNTIME_EXTENSION_KEY, extensions)
        self.assertEqual(task.status, "pending")

    def test_callback_with_stderr_text_is_diagnostics_only(self) -> None:
        task_id = self._task()
        self.service.record_agent_callback(
            task_id,
            {
                "final_state": "input_required",
                "summary": "waiting",
                "stderr": "EPERM: git ref update denied",
            },
        )
        task = self.service.get_task(task_id)
        extensions = task.extensions or {}
        self.assertIn("agentbc.completion_intent", extensions)
        self.assertNotIn("agentbc.input", extensions)
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, extensions)

    def test_invalid_callback_state_fails_closed(self) -> None:
        task_id = self._task()
        with self.assertRaises(ABCError):
            self.service.record_agent_callback(
                task_id,
                {
                    "final_state": "granted",
                    "summary": "fake full grant",
                    "requested_permission": "full",
                },
            )
        task = self.service.get_task(task_id)
        self.assertNotIn(
            "agentbc.completion_intent", task.extensions or {}
        )
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, task.extensions or {})

    def test_legacy_receipt_dual_read_keeps_grant_inert(self) -> None:
        # A revoked grant is inert: the resolver returns the base record and
        # never a full upgrade.
        from agent_bridge_connect.effective_permissions import (
            resolve_effective_permission,
        )
        from agent_bridge_connect.permission_grants import revoke_permission_grant

        grant = build_permission_grant(
            executor="hermes",
            task_id="E52M-001",
            input_id="input-1",
            session_id="sess-1",
            source_run_id="run-1",
        )
        revoked = revoke_permission_grant(grant, "user_denied")
        extensions = {
            PERMISSION_GRANT_EXTENSION_KEY: revoked,
            "agentbc.permission": build_permission_record(
                explicit_mode="safe"
            ),
        }
        resolved = resolve_effective_permission(
            {"task_id": "E52M-001", "extensions": extensions},
            "hermes",
            "run-2",
        )
        self.assertEqual(resolved["effective_mode"], "safe")
        self.assertNotEqual(resolved.get("selection_source"), "one_shot_permission_grant")


class DispatchContainmentTests(unittest.TestCase):
    """Concrete full retains task-scoped Runner containment.

    Plain projects and linked worktrees receive the same frozen PathPlan
    boundary; linked worktrees additionally pin their exact Git metadata.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "board"
        self.project = self.root / "project"
        self.project.mkdir()

    def _runner(self, executors: dict):
        from agent_bridge_connect.runner import RunnerState

        state = RunnerState(
            self.root / "runner-state",
            [self.root],
            executors,
        )
        return state

    def _full_task(self, executor: str, service):
        created = service.create_task(
            "containment canary",
            executor,
            [{"id": 1, "description": "one"}],
            session_id="",
            customer_dir=True,
            customer_path=self.project,
            permission_mode="full",
        )
        return created

    def _assert_full_dispatch_does_not_require_sandbox_exec(self, executor: str):
        fake = self.root / f"fake-{executor}"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = chat ] && [ "$2" = --help ]; then printf -- "--yolo\\n"; exit 0; fi\n'
            "printf ok\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | 0o100)
        service_config = {"workspace_root": str(self.root)}
        service = TaskService(self.board, config=service_config)
        created = self._full_task(executor, service)
        state = self._runner({executor: fake})
        if not state.allowed_executables:
            self.skipTest("executor resolution unavailable")
        fake_run = {
            "ok": True,
            "run_id": "mock-full",
            "pid": 1,
            "status": "running",
        }
        from agent_bridge_connect.runner import RunnerState

        with (
            mock.patch.object(
                RunnerState, "_spawn_process", return_value=fake_run
            ) as spawn,
            mock.patch("agent_bridge_connect.seatbelt.preflight_host_containment"),
        ):
            result = state.dispatch_worker(
                created.id, executor, str(self.board), "", 0.2, False
            )
        self.assertEqual(result["dispatch_status"], "accepted")
        _args, kwargs = spawn.call_args
        containment = kwargs.get("containment")
        self.assertIsNone(containment)

    def test_plain_project_full_dispatch_does_not_require_sandbox_exec(self) -> None:
        self._assert_full_dispatch_does_not_require_sandbox_exec("hermes")

    def test_safe_dispatch_unaffected_by_containment_preflight(self) -> None:
        # A safe task dispatches without containment (mocked spawn), proving
        # the preflight only gates concrete full.
        fake = self.root / "fake-hermes"
        fake.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | 0o100)
        service = TaskService(
            self.board, config={"workspace_root": str(self.root)}
        )
        created = service.create_task(
            "safe canary",
            "hermes",
            [{"id": 1, "description": "one"}],
            session_id="",
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        state = self._runner({"hermes": fake})
        fake_run = {
            "ok": True,
            "run_id": "mock-safe",
            "pid": 1,
            "status": "running",
        }
        from agent_bridge_connect.runner import RunnerState

        with mock.patch.object(
            RunnerState, "_spawn_process", return_value=fake_run
        ) as spawn:
            result = state.dispatch_worker(
                created.id, "hermes", str(self.board), "", 0.2, False
            )
        self.assertEqual(result["dispatch_status"], "accepted")
        self.assertTrue(spawn.called)
        # No containment argument was passed for the safe task.
        _args, kwargs = spawn.call_args
        self.assertIsNone(kwargs.get("containment"))

    def test_spawn_process_without_agentbc_containment(self) -> None:
        fake = self.root / "fake-hermes"
        fake.write_text(
            '#!/bin/sh\nprintf "tmp=%s" "$TMPDIR"\n',
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | 0o100)
        state = self._runner({"hermes": fake})
        result = state._spawn_process(
            "hermes",
            [str(fake)],
            self.project,
            "runner-hermes",
            containment=None,
        )
        deadline = 30.0
        while result["status"] == "running" and deadline > 0:
            time.sleep(0.1)
            deadline -= 0.1
            result = state.status(result["run_id"])
        self.assertEqual(result["status"], "completed", result.get("stderr"))
        record = state.runs[result["run_id"]]
        self.assertFalse(record["containment"])
        self.assertFalse(record.get("profile_path"))
        profiles = list((self.root / "runner-state" / "seatbelt").glob("task-*.sb"))
        self.assertEqual(profiles, [])

    def test_concurrent_contained_launches_keep_both_profiles_until_registered(self) -> None:
        from agent_bridge_connect.seatbelt import seatbelt_available

        if not seatbelt_available():
            self.skipTest("sandbox-exec unavailable")
        state = self._runner({})
        task_temp = self.root / "record" / "temp" / "E52M-003"
        containment = {
            "writable_roots": [str(self.project), str(task_temp)],
            "writable_files": [],
            "task_temp_root": str(task_temp),
            "linked_worktree": None,
        }
        results: list[dict] = []
        errors: list[BaseException] = []
        start = threading.Barrier(3)

        def launch(index: int) -> None:
            try:
                start.wait(timeout=5)
                results.append(
                    state._spawn_process(
                        "hermes",
                        ["/bin/sh", "-c", "sleep 1"],
                        self.project,
                        "runner-hermes",
                        run_id=f"runner-hermes-concurrent-{index}",
                        containment=containment,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        threads = [threading.Thread(target=launch, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        profiles = list((self.root / "runner-state" / "seatbelt").glob("task-*.sb"))
        self.assertEqual(len(profiles), 2)
        for result in results:
            state.cancel(result["run_id"])


class VerificationClosureTests(unittest.TestCase):
    """E52M-003 review fix 3: verified comes only from the structured success
    receipt; verification failure never leaves a completed task."""

    def test_verify_requires_structured_success_inputs(self) -> None:
        from agent_bridge_connect.cli import command_worker_run  # noqa: F401

        # The closure predicate mirrors cli.py: completed + finalized from a
        # valid callback + official session receipt.  The runtime contract
        # itself still refuses to verify without the session binding.
        record = _base_record()
        authorized = authorize_permission_runtime_record(
            record, decision="approve", request_id="req-1"
        )
        activated = activate_permission_runtime_record(
            authorized, host_profile_digest=PROFILE_DIGEST
        )
        with self.assertRaises(ABCError):
            verify_permission_runtime_record(activated)
        verified = verify_permission_runtime_record(
            activated, session_id="official-session-1"
        )
        self.assertEqual(verified["state"]["status"], "verified")

    def test_completed_without_receipt_blocks_not_verifies(self) -> None:
        # A completed poll without the structured success receipt must block
        # the record (stable code) - it can never stay activated or verify.
        record = _base_record()
        authorized = authorize_permission_runtime_record(
            record, decision="approve", request_id="req-1"
        )
        activated = activate_permission_runtime_record(
            authorized, host_profile_digest=PROFILE_DIGEST
        )
        blocked = block_permission_runtime_record(
            activated,
            code=PERMISSION_TRANSPORT_UNSUPPORTED,
            domain="executor_policy",
        )
        self.assertEqual(blocked["state"]["status"], "blocked")
        self.assertEqual(
            blocked["state"]["block_code"], PERMISSION_TRANSPORT_UNSUPPORTED
        )


class ClaudeWorkerTransportGateTests(unittest.TestCase):
    """E52M-003 review fix 4: the production worker fails closed with
    permission_transport_unsupported; it never launches a fabricated
    broker command under an official flag."""

    def test_start_routes_approval_capable_base_to_control(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            fake = Path(temporary) / "claude"
            fake.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o100)
            executor = ClaudeExecutor(command=str(fake), transport="direct")
            packet = {
                "task_id": "E52M-003",
                "steps": [{"id": 1, "description": "one"}],
                "workspace": {"project_root": str(workspace), "root": str(workspace)},
                "extensions": {
                    "agentbc.permission": build_permission_record(explicit_mode="safe")
                },
                "runner_authorization_required": True,
            }
            sentinel = object()
            with mock.patch.object(
                executor, "start_control", return_value=sentinel
            ) as control:
                self.assertIs(executor.start(packet), sentinel)
            control.assert_called_once_with(packet)

    def test_full_sources_bypass_sdk_control(self) -> None:
        from agent_bridge_connect.executors.claude import _claude_control_required

        full_packet = {
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="full")
            },
            "runner_authorization_required": True,
        }
        self.assertFalse(_claude_control_required(full_packet))

        grant = build_permission_grant(
            executor="claude",
            task_id="E52M-003",
            input_id="input-1",
            session_id="session-1",
            source_run_id="run-source",
        )
        granted_packet = {
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                PERMISSION_GRANT_EXTENSION_KEY: grant,
            },
            "runner_authorization_required": True,
        }
        self.assertTrue(_claude_control_required(granted_packet))

    def test_non_runner_packet_keeps_direct_path(self) -> None:
        from agent_bridge_connect.executors.claude import _claude_control_required

        self.assertFalse(_claude_control_required({"extensions": {}}))

    def test_start_control_does_not_reject_unknown_cli_version(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            fake = Path(temporary) / "claude"
            fake.write_text("#!/bin/sh\nprintf '2.1.247 (Claude Code)'\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o100)
            executor = ClaudeExecutor(command=str(fake), transport="direct")
            executor._version = "2.1.247 (Claude Code)"
            packet = {
                "task_id": "E52M-003",
                "steps": [{"id": 1, "description": "one"}],
                "workspace": {"project_root": str(workspace), "root": str(workspace)},
                "extensions": {},
                "runner_authorization_required": True,
            }
            result = executor.start_control(packet)
            self.assertFalse(result.ok)
            self.assertNotIn("permission_transport_unsupported", result.message)
            self.assertIn("approval_control_invalid", result.message)

    def test_control_command_never_carries_broker_value(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor
        from agent_bridge_connect.executors.claude import ClaudePermissionPromptBroker

        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "claude"
            fake.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o100)
            executor = ClaudeExecutor(command=str(fake), transport="direct")
            # Even when the live probe claims the flag exists, the built
            # control command must not embed a self-authored broker command.
            with mock.patch.object(
                ClaudeExecutor, "supports_permission_prompt_tool", return_value=True
            ):
                broker = ClaudePermissionPromptBroker(
                    session_id="sess-1", decision_callback=lambda request: {}
                )
                with self.assertRaises(ABCError) as raised:
                    executor._build_control_command(
                        "prompt",
                        Path(temporary),
                        {
                            "task_id": "E52M-003",
                            "extensions": {},
                            "workspace": {},
                        },
                        {"effective_mode": "safe"},
                        broker,
                    )
                self.assertEqual(
                    raised.exception.code, PERMISSION_TRANSPORT_UNSUPPORTED
                )


if __name__ == "__main__":
    unittest.main()
