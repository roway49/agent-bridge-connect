"""Regression coverage for the PERM-103-008 permission wait taxonomy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.permission_failures import (
    PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
    PERMISSION_CHAIN_HEAD_AMBIGUOUS,
    PERMISSION_CHAIN_HEAD_STALE,
    PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
    PERMISSION_INPUT_INVALID,
    PERMISSION_MODE_UNSUPPORTED,
    PERMISSION_REQUESTED_SCOPE_INVALID,
    PERMISSION_RESUME_SESSION_MISSING,
    PERMISSION_RUN_LEASE_INVALID,
    PERMISSION_RUN_LEASE_RUN_MISMATCH,
    PERMISSION_SESSION_RECEIPT_INVALID,
    PERMISSION_SESSION_RECEIPT_MISSING,
    PERMISSION_SESSION_SNAPSHOT_INVALID,
    PERMISSION_SESSION_STATE_STALE,
    PERMISSION_WAIT_COMPATIBILITY_CODE,
)
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
)
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.run_lease import RunLeaseState, create_lease, load_lease, save_lease
from agent_bridge_connect.service import TaskService, task_to_status


RECEIPT_SOURCE = {
    "codex": "jsonl_thread_started",
    "claude": "preallocated",
    "hermes": "stderr_receipt",
}
DEFAULT_SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


def _callback(
    task_id: str,
    run_id: str,
    *,
    requested_permission: str = "full",
    reason: str | None = "needs one temporary permission",
    step_results: list[dict] | None = None,
    input_overrides: dict | None = None,
) -> dict:
    input_details = {
        "type": "permission",
        "requested_permission": requested_permission,
    }
    if reason is not None:
        input_details["reason"] = reason
    if input_overrides:
        input_details.update(input_overrides)
    return {
        "version": 1,
        "task_id": task_id,
        "final_state": "input_required",
        "summary": "requesting one temporary permission",
        "executor_run_id": run_id,
        "input": input_details,
        "step_results": step_results or [{"id": 1, "status": "blocked"}],
    }


def _receipt(executor: str, session_id: str = DEFAULT_SESSION_ID, **overrides: object) -> dict:
    value = {
        "version": 1,
        "executor": executor,
        "session_id": session_id,
        "resumed": False,
        "persistence": "persistent",
        "source": RECEIPT_SOURCE[executor],
    }
    value.update(overrides)
    return value


class PermissionTaxonomyHarness:
    def __init__(self, executor: str = "codex", *, steps: list[dict] | None = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        self.executor = executor
        self.steps = steps or [{"id": 1, "description": "one permission step"}]

    def close(self) -> None:
        self.temp.cleanup()

    def context(self, *, run_id: str | None = None) -> dict:
        task = self.service.create_task(
            "permission taxonomy",
            self.executor,
            self.steps,
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        self.service.start_task_run(task.id, self.executor)
        run_id = run_id or f"{self.executor}-taxonomy-run-1"
        self.service.record_executor_run_started(task.id, run_id)

        lease = create_lease(task.id, self.executor, 0, str(self.project))
        lease.run_id = run_id
        lease.state = RunLeaseState.CLOSED
        save_lease(lease, self.service.board_root)

        stored = self.service.get_task(task.id)
        session = stored.extensions[SESSION_EXTENSION_KEY]
        session_id = str(session.get("session_id") or "") or DEFAULT_SESSION_ID
        receipt = _receipt(self.executor, session_id)
        model = self.service.get_task(task.id)
        self.service._apply_executor_session_result(model, run_id, receipt, "input_required")
        self.service.store.write_task(task.id, model.to_dict())
        model = self.service.get_task(task.id)
        grant = build_permission_grant(
            executor=self.executor,
            task_id=task.id,
            input_id="input-taxonomy",
            session_id=session_id,
            source_run_id=run_id,
        )
        extensions = dict(model.extensions or {})
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = grant
        model.extensions = extensions
        self.service.store.write_task(task.id, model.to_dict())
        return {
            "task": self.service.get_task(task.id),
            "task_id": task.id,
            "run_id": run_id,
            "receipt": receipt,
            "callback": _callback(task.id, run_id),
        }


class PermissionFailureTaxonomyTests(unittest.TestCase):
    def _assert_recovery(
        self,
        harness: PermissionTaxonomyHarness,
        task_id: str,
        reason: str,
        *,
        require_grant: bool = True,
    ) -> dict:
        task = harness.service.get_task(task_id)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(len(task.errors), 1)
        error = task.errors[-1]
        self.assertEqual(error["code"], PERMISSION_WAIT_COMPATIBILITY_CODE)
        details = error["details"]
        self.assertEqual(details["reason_code"], reason)
        self.assertEqual(details["compatibility_code"], PERMISSION_WAIT_COMPATIBILITY_CODE)
        self.assertEqual(details["input_type"], "permission")
        self.assertNotIn("argv", json.dumps(error))
        self.assertNotIn("prompt-secret", json.dumps(error))
        self.assertNotIn("sk-secret-token", json.dumps(error))
        self.assertNotIn("private-session-storage", json.dumps(error))
        self.assertNotIn("executor-log-secret", json.dumps(error))
        self.assertNotIn("agentbc.input", task.extensions)
        grant = task.extensions.get(PERMISSION_GRANT_EXTENSION_KEY)
        if require_grant:
            self.assertIsNotNone(grant)
            self.assertEqual(grant["state"]["status"], "revoked")
        elif grant is not None:
            self.assertEqual(grant["state"]["status"], "revoked")
        return error

    def _run_direct_case(self, name: str, reason: str, mutate) -> None:
        harness = PermissionTaxonomyHarness()
        self.addCleanup(harness.close)
        context = harness.context()
        task = context["task"]
        mutate(harness, context)
        harness.service.store.write_task(task.id, task.to_dict())
        failure = harness.service._permission_wait_contract_failure(
            task,
            context["callback"],
            context["run_id"],
            [
                item
                for item in context["callback"]["step_results"]
                if item.get("status") == "blocked"
            ],
        )
        self.assertIsNotNone(failure, name)
        self.assertEqual(failure.reason_code, reason, name)
        harness.service._fail_closed_permission_wait(
            task,
            context["run_id"],
            failure=failure,
            blocked_step_id=1,
        )
        self._assert_recovery(harness, context["task_id"], reason)

    def test_table_driven_contract_gates_are_specific_and_fail_closed(self) -> None:
        def mode(harness, context):
            record = dict(context["task"].extensions[PERMISSION_EXTENSION_KEY])
            record["requested_mode"] = "full"
            record["effective_mode"] = "full"
            record["resolved_base_mode"] = "full"
            record["resolution_state"] = "frozen"
            record["approval_policy"] = "none"
            context["task"].extensions[PERMISSION_EXTENSION_KEY] = record

        def malformed_input(harness, context):
            context["callback"]["input"]["reason"] = ""

        def requested_scope(harness, context):
            context["callback"]["input"]["requested_permission"] = "inherit"

        def blocked_cardinality(harness, context):
            context["callback"]["step_results"] = []

        def stale_chain(harness, context):
            chain = mock.Mock(anomalies=[], head_task_ids=[context["task_id"]], requested_is_head=False)
            harness.service.resolve_chain = mock.Mock(return_value=chain)

        def ambiguous_chain(harness, context):
            chain = mock.Mock(
                anomalies=[],
                head_task_ids=[context["task_id"], "QPN9-002"],
                requested_is_head=True,
            )
            harness.service.resolve_chain = mock.Mock(return_value=chain)

        def invalid_lease(harness, context):
            lease = load_lease(context["task_id"], harness.service.board_root)
            self.assertIsNotNone(lease)
            lease.state = RunLeaseState.ACTIVE
            save_lease(lease, harness.service.board_root)

        def lease_run_mismatch(harness, context):
            lease = load_lease(context["task_id"], harness.service.board_root)
            self.assertIsNotNone(lease)
            lease.run_id = "different-run"
            save_lease(lease, harness.service.board_root)

        def stale_session(harness, context):
            context["task"].extensions[SESSION_EXTENSION_KEY]["session_state"] = "active"

        def missing_resume_session(harness, context):
            session = context["task"].extensions[SESSION_EXTENSION_KEY]
            session["session_id"] = ""

        def malformed_session(harness, context):
            context["task"].extensions[SESSION_EXTENSION_KEY]["resume_count"] = -1

        def executor_session_run_mismatch(harness, context):
            context["task"].extensions[SESSION_EXTENSION_KEY]["run_ids"] = ["old-run"]

        cases = [
            ("non-escalatable full base", PERMISSION_MODE_UNSUPPORTED, mode),
            ("malformed permission input", PERMISSION_INPUT_INVALID, malformed_input),
            ("requested scope", PERMISSION_REQUESTED_SCOPE_INVALID, requested_scope),
            (
                "blocked step cardinality",
                PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
                blocked_cardinality,
            ),
            ("stale chain head", PERMISSION_CHAIN_HEAD_STALE, stale_chain),
            ("ambiguous chain head", PERMISSION_CHAIN_HEAD_AMBIGUOUS, ambiguous_chain),
            ("invalid RunLease", PERMISSION_RUN_LEASE_INVALID, invalid_lease),
            ("RunLease run mismatch", PERMISSION_RUN_LEASE_RUN_MISMATCH, lease_run_mismatch),
            ("stale session state", PERMISSION_SESSION_STATE_STALE, stale_session),
            ("missing resume session", PERMISSION_RESUME_SESSION_MISSING, missing_resume_session),
            ("invalid session snapshot", PERMISSION_SESSION_SNAPSHOT_INVALID, malformed_session),
            (
                "executor session run mismatch",
                PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
                executor_session_run_mismatch,
            ),
        ]
        for name, reason, mutate in cases:
            with self.subTest(name=name):
                self._run_direct_case(name, reason, mutate)

    def test_receipt_failure_table_covers_missing_invalid_executor_session_and_run(self) -> None:
        cases = [
            (
                "missing",
                "hermes",
                None,
                {},
                PERMISSION_SESSION_RECEIPT_MISSING,
            ),
            (
                "malformed",
                "hermes",
                {
                    "version": 1,
                    "executor": "hermes",
                    "session_id": DEFAULT_SESSION_ID,
                    "resumed": False,
                    "persistence": "ephemeral",
                    "source": "wrong_source",
                    "argv": "prompt-secret --dangerously-bypass",
                    "logs": "executor-log-secret",
                },
                {},
                PERMISSION_SESSION_RECEIPT_INVALID,
            ),
            (
                "executor mismatch",
                "codex",
                _receipt("claude"),
                {},
                "permission_executor_session_mismatch",
            ),
            (
                "session mismatch",
                "claude",
                True,
                {"session_override": "019feed0-0000-7000-8000-0000000000ff"},
                "permission_executor_session_mismatch",
            ),
            (
                "run mismatch",
                "codex",
                True,
                {"callback_run_id": "codex-stale-run"},
                PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
            ),
        ]
        for name, executor, receipt, options, reason in cases:
            with self.subTest(name=name):
                from tests.test_phase6_permission_lifecycle import PermissionLifecycleHarness

                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                task_id, _, _ = harness.prepare_wait(
                    receipt=receipt,
                    **options,
                )
                error = self._assert_recovery(
                    harness,
                    task_id,
                    reason,
                    require_grant=False,
                )
                self.assertEqual(error["details"]["reason_code"], reason)

    def test_invalid_callback_permission_contracts_project_to_one_reason(self) -> None:
        cases = [
            (
                "zero blocked steps",
                PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
                [{"id": 1, "status": "pending"}],
                "full",
                "reason",
            ),
            (
                "two blocked steps",
                PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
                [{"id": 1, "status": "blocked"}, {"id": 2, "status": "blocked"}],
                "full",
                "reason",
            ),
            (
                "requested scope",
                PERMISSION_REQUESTED_SCOPE_INVALID,
                [{"id": 1, "status": "blocked"}],
                "safe",
                "reason",
            ),
            (
                "missing reason",
                PERMISSION_INPUT_INVALID,
                [{"id": 1, "status": "blocked"}],
                "full",
                None,
            ),
        ]
        for name, reason_code, results, requested, reason in cases:
            with self.subTest(name=name):
                steps = (
                    [{"id": 1, "description": "one"}, {"id": 2, "description": "two"}]
                    if len(results) == 2
                    else None
                )
                harness = PermissionTaxonomyHarness(steps=steps)
                self.addCleanup(harness.close)
                context = harness.context()
                callback = _callback(
                    context["task_id"],
                    context["run_id"],
                    requested_permission=requested,
                    reason=reason,
                    step_results=results,
                )
                harness.service.finalize_task_from_executor_exit(
                    context["task_id"],
                    executor_run_id=context["run_id"],
                    callback=callback,
                    execution_session=context["receipt"],
                )
                self._assert_recovery(harness, context["task_id"], reason_code)

    def test_valid_official_receipts_and_compatibility_projection(self) -> None:
        from tests.test_phase6_permission_lifecycle import PermissionLifecycleHarness

        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                task_id, run_id, _ = harness.prepare_wait()
                task = harness.service.get_task(task_id)
                self.assertEqual(task.status, "input_required")
                self.assertEqual(task.extensions["agentbc.input"]["executor_run_id"], run_id)
                self.assertEqual(task.errors, [])

        harness = PermissionTaxonomyHarness()
        self.addCleanup(harness.close)
        context = harness.context()
        task = context["task"]
        task.extensions[SESSION_EXTENSION_KEY]["session_state"] = "active"
        failure = harness.service._permission_wait_contract_failure(
            task,
            context["callback"],
            context["run_id"],
            [{"id": 1, "status": "blocked"}],
        )
        self.assertEqual(failure.reason_code, PERMISSION_SESSION_STATE_STALE)
        harness.service._fail_closed_permission_wait(
            task,
            context["run_id"],
            failure=failure,
            blocked_step_id=1,
        )
        status = task_to_status(harness.service.get_task(context["task_id"]))
        report = generate_report(context["task_id"], harness.board)
        markdown = generate_report_md(context["task_id"], harness.board)
        for projection in (status, report):
            self.assertEqual(
                projection["errors"][-1]["details"]["reason_code"],
                PERMISSION_SESSION_STATE_STALE,
            )
            self.assertEqual(
                projection["errors"][-1]["code"],
                PERMISSION_WAIT_COMPATIBILITY_CODE,
            )
        self.assertIn(PERMISSION_WAIT_COMPATIBILITY_CODE, markdown)
        self.assertIn(PERMISSION_SESSION_STATE_STALE, markdown)
        self.assertNotIn("private-session-storage", markdown)

    def test_failure_replay_is_idempotent_and_recovery_keeps_reason(self) -> None:
        harness = PermissionTaxonomyHarness()
        self.addCleanup(harness.close)
        context = harness.context()
        task = context["task"]
        task.extensions[SESSION_EXTENSION_KEY]["session_state"] = "active"
        failure = harness.service._permission_wait_contract_failure(
            task,
            context["callback"],
            context["run_id"],
            [{"id": 1, "status": "blocked"}],
        )
        self.assertEqual(failure.reason_code, PERMISSION_SESSION_STATE_STALE)
        harness.service._fail_closed_permission_wait(
            task,
            context["run_id"],
            failure=failure,
            blocked_step_id=1,
        )
        first = harness.service.get_task(context["task_id"])
        first_error_count = len(first.errors)
        self.assertEqual(first_error_count, 1)
        harness.service._fail_closed_permission_wait(
            first,
            context["run_id"],
            failure=failure,
            blocked_step_id=1,
        )
        replayed = harness.service.get_task(context["task_id"])
        self.assertEqual(len(replayed.errors), first_error_count)
        self.assertEqual(replayed.errors[-1]["details"]["reason_code"], PERMISSION_SESSION_STATE_STALE)
        self.assertEqual(
            replayed.extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"],
            "revoked",
        )


if __name__ == "__main__":
    unittest.main()
