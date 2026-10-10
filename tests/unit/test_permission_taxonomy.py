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
from agent_bridge_connect.permission_grants import PERMISSION_GRANT_EXTENSION_KEY, build_permission_grant
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.run_lease import RunLeaseState, create_lease, load_lease, save_lease
from agent_bridge_connect.service import TaskService, task_to_status
import copy as _resource_decision_copy
import sys as _resource_decision_sys
import tempfile as _resource_decision_tempfile
import unittest as _resource_decision_unittest
from datetime import datetime as _resource_decision_datetime, timedelta as _resource_decision_timedelta, timezone as _resource_decision_timezone
from pathlib import Path as _resource_decision_Path
from unittest import mock as _resource_decision_mock
from agent_bridge_connect.adapters import DeliveryResult as _resource_decision_DeliveryResult
from agent_bridge_connect.execution_policy import RESOURCE_EXTENSION_KEY as _resource_decision_RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY as _resource_decision_SESSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError as _resource_decision_ABCError
from agent_bridge_connect.reports import generate_report as _resource_decision_generate_report
from agent_bridge_connect.run_lease import RunLeaseState as _resource_decision_RunLeaseState, create_lease as _resource_decision_create_lease, load_lease as _resource_decision_load_lease, save_lease as _resource_decision_save_lease
from agent_bridge_connect.runner import RunnerError as _resource_decision_RunnerError, RunnerState as _resource_decision_RunnerState
from agent_bridge_connect.service import TaskService as _resource_decision_TaskService

RECEIPT_SOURCE = {"codex": "jsonl_thread_started", "claude": "preallocated", "hermes": "stderr_receipt"}
DEFAULT_SESSION_ID = "019feed0-0000-7000-8000-0000000000aa"


def _callback(task_id: str, run_id: str, *, requested_permission: str = "full", reason: str | None = "needs one temporary permission", step_results: list[dict] | None = None, input_overrides: dict | None = None) -> dict:
    input_details = {"type": "permission", "requested_permission": requested_permission}
    if reason is not None:
        input_details["reason"] = reason
    if input_overrides:
        input_details.update(input_overrides)
    return {"version": 1, "task_id": task_id, "final_state": "input_required", "summary": "requesting one temporary permission", "executor_run_id": run_id, "input": input_details, "step_results": step_results or [{"id": 1, "status": "blocked"}]}


def _receipt(executor: str, session_id: str = DEFAULT_SESSION_ID, **overrides: object) -> dict:
    value = {"version": 1, "executor": executor, "session_id": session_id, "resumed": False, "persistence": "persistent", "source": RECEIPT_SOURCE[executor]}
    value.update(overrides)
    return value


class PermissionTaxonomyHarness:
    def __init__(self, executor: str = "codex", *, steps: list[dict] | None = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})
        self.executor = executor
        self.steps = steps or [{"id": 1, "description": "one permission step"}]

    def close(self) -> None:
        self.temp.cleanup()

    def context(self, *, run_id: str | None = None) -> dict:
        task = self.service.create_task("permission taxonomy", self.executor, self.steps, customer_dir=True, customer_path=self.project, permission_mode="safe")
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
        grant = build_permission_grant(executor=self.executor, task_id=task.id, input_id="input-taxonomy", session_id=session_id, source_run_id=run_id)
        extensions = dict(model.extensions or {})
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = grant
        model.extensions = extensions
        self.service.store.write_task(task.id, model.to_dict())
        return {"task": self.service.get_task(task.id), "task_id": task.id, "run_id": run_id, "receipt": receipt, "callback": _callback(task.id, run_id)}


class PermissionFailureTaxonomyTests(unittest.TestCase):
    def _assert_recovery(self, harness: PermissionTaxonomyHarness, task_id: str, reason: str, *, require_grant: bool = True) -> dict:
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
        failure = harness.service._permission_wait_contract_failure(task, context["callback"], context["run_id"], [item for item in context["callback"]["step_results"] if item.get("status") == "blocked"])
        self.assertIsNotNone(failure, name)
        self.assertEqual(failure.reason_code, reason, name)
        harness.service._fail_closed_permission_wait(task, context["run_id"], failure=failure, blocked_step_id=1)
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
            chain = mock.Mock(anomalies=[], head_task_ids=[context["task_id"], "QPN9-002"], requested_is_head=True)
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
            session = context["task"].extensions[SESSION_EXTENSION_KEY]
            session["run_ids"] = ["old-run"]
            session["run_resume_facts"] = {"old-run": False}

        cases = [
            ("non-escalatable full base", PERMISSION_MODE_UNSUPPORTED, mode),
            ("malformed permission input", PERMISSION_INPUT_INVALID, malformed_input),
            ("requested scope", PERMISSION_REQUESTED_SCOPE_INVALID, requested_scope),
            ("blocked step cardinality", PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID, blocked_cardinality),
            ("stale chain head", PERMISSION_CHAIN_HEAD_STALE, stale_chain),
            ("ambiguous chain head", PERMISSION_CHAIN_HEAD_AMBIGUOUS, ambiguous_chain),
            ("invalid RunLease", PERMISSION_RUN_LEASE_INVALID, invalid_lease),
            ("RunLease run mismatch", PERMISSION_RUN_LEASE_RUN_MISMATCH, lease_run_mismatch),
            ("stale session state", PERMISSION_SESSION_STATE_STALE, stale_session),
            ("missing resume session", PERMISSION_RESUME_SESSION_MISSING, missing_resume_session),
            ("invalid session snapshot", PERMISSION_SESSION_SNAPSHOT_INVALID, malformed_session),
            ("executor session run mismatch", PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH, executor_session_run_mismatch),
        ]
        for name, reason, mutate in cases:
            with self.subTest(name=name):
                self._run_direct_case(name, reason, mutate)

    def test_receipt_failure_table_covers_missing_invalid_executor_session_and_run(self) -> None:
        cases = [
            ("missing", "hermes", None, {}, PERMISSION_SESSION_RECEIPT_MISSING),
            ("malformed", "hermes", {"version": 1, "executor": "hermes", "session_id": DEFAULT_SESSION_ID, "resumed": False, "persistence": "ephemeral", "source": "wrong_source", "argv": "prompt-secret --dangerously-bypass", "logs": "executor-log-secret"}, {}, PERMISSION_SESSION_RECEIPT_INVALID),
            ("executor mismatch", "codex", _receipt("claude"), {}, "permission_executor_session_mismatch"),
            ("session mismatch", "claude", True, {"session_override": "019feed0-0000-7000-8000-0000000000ff"}, "permission_executor_session_mismatch"),
            ("run mismatch", "codex", True, {"callback_run_id": "codex-stale-run"}, PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH),
        ]
        for name, executor, receipt, options, reason in cases:
            with self.subTest(name=name):
                from tests.unit.test_permission_lifecycle import PermissionLifecycleHarness

                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                (task_id, _, _) = harness.prepare_wait(receipt=receipt, **options)
                error = self._assert_recovery(harness, task_id, reason, require_grant=False)
                self.assertEqual(error["details"]["reason_code"], reason)

    def test_invalid_callback_permission_contracts_project_to_one_reason(self) -> None:
        cases = [
            ("zero blocked steps", PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID, [{"id": 1, "status": "pending"}], "full", "reason"),
            ("two blocked steps", PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID, [{"id": 1, "status": "blocked"}, {"id": 2, "status": "blocked"}], "full", "reason"),
            ("requested scope", PERMISSION_REQUESTED_SCOPE_INVALID, [{"id": 1, "status": "blocked"}], "safe", "reason"),
            ("missing reason", PERMISSION_INPUT_INVALID, [{"id": 1, "status": "blocked"}], "full", None),
        ]
        for name, reason_code, results, requested, reason in cases:
            with self.subTest(name=name):
                steps = [{"id": 1, "description": "one"}, {"id": 2, "description": "two"}] if len(results) == 2 else None
                harness = PermissionTaxonomyHarness(steps=steps)
                self.addCleanup(harness.close)
                context = harness.context()
                callback = _callback(context["task_id"], context["run_id"], requested_permission=requested, reason=reason, step_results=results)
                harness.service.finalize_task_from_executor_exit(context["task_id"], executor_run_id=context["run_id"], callback=callback, execution_session=context["receipt"])
                self._assert_recovery(harness, context["task_id"], reason_code)

    def test_valid_official_receipts_and_compatibility_projection(self) -> None:
        from tests.unit.test_permission_lifecycle import PermissionLifecycleHarness

        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                harness = PermissionLifecycleHarness(executor)
                self.addCleanup(harness.close)
                (task_id, run_id, _) = harness.prepare_wait()
                task = harness.service.get_task(task_id)
                self.assertEqual(task.status, "input_required")
                self.assertEqual(task.extensions["agentbc.input"]["executor_run_id"], run_id)
                self.assertEqual(task.errors, [])
        harness = PermissionTaxonomyHarness()
        self.addCleanup(harness.close)
        context = harness.context()
        task = context["task"]
        task.extensions[SESSION_EXTENSION_KEY]["session_state"] = "active"
        failure = harness.service._permission_wait_contract_failure(task, context["callback"], context["run_id"], [{"id": 1, "status": "blocked"}])
        self.assertEqual(failure.reason_code, PERMISSION_SESSION_STATE_STALE)
        harness.service._fail_closed_permission_wait(task, context["run_id"], failure=failure, blocked_step_id=1)
        status = task_to_status(harness.service.get_task(context["task_id"]))
        report = generate_report(context["task_id"], harness.board)
        markdown = generate_report_md(context["task_id"], harness.board)
        for projection in (status, report):
            self.assertEqual(projection["errors"][-1]["details"]["reason_code"], PERMISSION_SESSION_STATE_STALE)
            self.assertEqual(projection["errors"][-1]["code"], PERMISSION_WAIT_COMPATIBILITY_CODE)
        self.assertIn(PERMISSION_WAIT_COMPATIBILITY_CODE, markdown)
        self.assertIn(PERMISSION_SESSION_STATE_STALE, markdown)
        self.assertNotIn("private-session-storage", markdown)

    def test_failure_replay_is_idempotent_and_recovery_keeps_reason(self) -> None:
        harness = PermissionTaxonomyHarness()
        self.addCleanup(harness.close)
        context = harness.context()
        task = context["task"]
        task.extensions[SESSION_EXTENSION_KEY]["session_state"] = "active"
        failure = harness.service._permission_wait_contract_failure(task, context["callback"], context["run_id"], [{"id": 1, "status": "blocked"}])
        self.assertEqual(failure.reason_code, PERMISSION_SESSION_STATE_STALE)
        harness.service._fail_closed_permission_wait(task, context["run_id"], failure=failure, blocked_step_id=1)
        first = harness.service.get_task(context["task_id"])
        first_error_count = len(first.errors)
        self.assertEqual(first_error_count, 1)
        harness.service._fail_closed_permission_wait(first, context["run_id"], failure=failure, blocked_step_id=1)
        replayed = harness.service.get_task(context["task_id"])
        self.assertEqual(len(replayed.errors), first_error_count)
        self.assertEqual(replayed.errors[-1]["details"]["reason_code"], PERMISSION_SESSION_STATE_STALE)
        self.assertEqual(replayed.extensions[PERMISSION_GRANT_EXTENSION_KEY]["state"]["status"], "revoked")


class _resource_decision_Phase4ResourceDecisionTests(_resource_decision_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _resource_decision_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _resource_decision_Path(self.temporary.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def _waiting_task(self, executor: str, limit: int | float):
        config = {"workspace_root": str(self.root / "workspace"), "executors": {executor: {"max_budget_usd" if executor == "claude" else "max_turns": limit}}, "sessions": {"retain_executor_sessions": executor == "claude"}}
        service = _resource_decision_TaskService(self.board, config=config)
        task = service.create_task("resource decision", executor, [{"id": 1, "description": "done work"}, {"id": 2, "description": "blocked work"}], customer_dir=True, customer_path=self.project, permission_mode="inherit")
        raw = service.store.read_task(task.id)
        raw["status"] = "input_required"
        raw["steps"][0]["status"] = "done"
        raw["steps"][1]["status"] = "blocked"
        resources = raw["extensions"][_resource_decision_RESOURCE_EXTENSION_KEY]
        resources["exhaustion_count"] = 1
        session = raw["extensions"][_resource_decision_SESSION_EXTENSION_KEY]
        if not session["session_id"]:
            session["session_id"] = "20260810_010203_a1b2c3"
        session["session_state"] = "input_required"
        session["run_ids"] = [f"{executor}-run-1"]
        now = _resource_decision_datetime.now(_resource_decision_timezone.utc)
        input_id = f"input-{executor}-1"
        raw["extensions"]["agentbc.input"] = {
            "input_id": input_id,
            "executor_run_id": f"{executor}-run-1",
            "blocked_step_id": 2,
            "type": "choice",
            "kind": "resource_limit",
            "response_protocol": "approve_deny",
            "executor": executor,
            "resource": resources["resource"],
            "current_limit": limit,
            "next_limit": limit * 2,
            "summary": "Executor resource limit exhausted",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "deadline_at": (now + _resource_decision_timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "status": "waiting",
        }
        raw["extensions"]["agentbc.execution"] = {"internal_status": "waiting", "lease_state": "suspended"}
        service.store.write_task(task.id, raw)
        lease = _resource_decision_create_lease(task.id, executor, 0, str(self.project))
        lease.run_id = f"{executor}-run-1"
        lease.state = _resource_decision_RunLeaseState.SUSPENDED
        _resource_decision_save_lease(lease, self.board)
        return (service, service.get_task(task.id), config)

    def _next_wait(self, service: _resource_decision_TaskService, task_id: str, input_id: str) -> None:
        raw = service.store.read_task(task_id)
        resources = raw["extensions"][_resource_decision_RESOURCE_EXTENSION_KEY]
        raw["status"] = "input_required"
        raw["steps"][1]["status"] = "blocked"
        raw["extensions"][_resource_decision_SESSION_EXTENSION_KEY]["session_state"] = "input_required"
        resources["exhaustion_count"] += 1
        previous = raw["extensions"]["agentbc.input"]
        raw["extensions"]["agentbc.input_history"] = [previous]
        now = _resource_decision_datetime.now(_resource_decision_timezone.utc)
        raw["extensions"]["agentbc.input"] = {
            **previous,
            "input_id": input_id,
            "current_limit": resources["current_limit"],
            "next_limit": resources["current_limit"] * 2,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "deadline_at": (now + _resource_decision_timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "status": "waiting",
        }
        service.store.write_task(task_id, raw)

    def _runner(self, executor: str) -> _resource_decision_RunnerState:
        state = _resource_decision_RunnerState(self.root / "runner", [self.root], {executor: _resource_decision_Path(_resource_decision_sys.executable)})
        state.known_boards = {self.board.resolve()}
        return state

    def _respond_request(self, task_id: str, input_id: str, response_type: str) -> dict:
        return {"task_id": task_id, "input_id": input_id, "response_type": response_type, "message": "", "board_root": str(self.board), "config_path": ""}

    def test_multiround_approve_doubles_only_task_snapshot(self) -> None:
        cases = (("claude", 10.0, (20.0, 40.0)), ("hermes", 60, (120, 240)))
        for executor, initial, expected in cases:
            with self.subTest(executor=executor):
                subroot = self.root / executor
                subroot.mkdir()
                self.board = subroot / "record"
                self.project = subroot / "project"
                self.project.mkdir()
                (service, task, original_config) = self._waiting_task(executor, initial)
                frozen_config = _resource_decision_copy.deepcopy(original_config)
                first = task.extensions["agentbc.input"]
                result = service.respond_to_input(task.id, first["input_id"], response_type="approve")
                current = service.get_task(task.id)
                self.assertEqual(result["status"], "resuming")
                self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], expected[0])
                self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["last_decision"], "increase")
                self.assertEqual(current.steps[0]["status"], "done")
                self.assertEqual(current.steps[1]["status"], "pending")
                self.assertEqual(current.extensions[_resource_decision_SESSION_EXTENSION_KEY]["session_state"], "active")
                self.assertEqual(service.store.read_events(task.id)[-1]["event_type"], "task.resource_limit_increased")
                self._next_wait(service, task.id, f"input-{executor}-2")
                service.respond_to_input(task.id, f"input-{executor}-2", response_type="approve")
                current = service.get_task(task.id)
                self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], expected[1])
                self.assertEqual(service.config, frozen_config)

    def test_runner_restart_redispatches_same_task_session_and_project(self) -> None:
        (service, task, _) = self._waiting_task("claude", 10.0)
        request = task.extensions["agentbc.input"]
        original_session = _resource_decision_copy.deepcopy(task.extensions[_resource_decision_SESSION_EXTENSION_KEY])
        original_project = task.workspace["project_root"]
        state = self._runner("claude")
        dispatched = {"run_id": "runner-worker-resume", "dispatch_status": "accepted", "monitor_status": "disabled"}
        with _resource_decision_mock.patch.object(state, "dispatch_worker", return_value=dispatched) as launch:
            result = state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "approve"))
        restarted = _resource_decision_TaskService(self.board)
        current = restarted.get_task(task.id)
        self.assertEqual(result["task_id"], task.id)
        self.assertTrue(result["same_task"])
        self.assertEqual(launch.call_args.args[:3], (task.id, "claude", str(self.board.resolve())))
        self.assertTrue(launch.call_args.kwargs["resuming"])
        current_session = current.extensions[_resource_decision_SESSION_EXTENSION_KEY]
        self.assertEqual(current_session["session_id"], original_session["session_id"])
        self.assertEqual(current_session["project_path"], original_session["project_path"])
        self.assertEqual(current_session["run_ids"], original_session["run_ids"])
        self.assertEqual(current_session["session_state"], "active")
        self.assertEqual(current.workspace["project_root"], original_project)

    def test_duplicate_and_stale_responses_never_redouble_or_redispatch(self) -> None:
        (service, task, _) = self._waiting_task("hermes", 60)
        request = task.extensions["agentbc.input"]
        state = self._runner("hermes")
        with self.assertRaises(_resource_decision_ABCError) as stale:
            service.respond_to_input(task.id, "input-old", response_type="approve")
        self.assertEqual(stale.exception.code, "stale_input")
        self.assertEqual(service.get_task(task.id).extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], 60)
        with _resource_decision_mock.patch.object(state, "dispatch_worker", return_value={"run_id": "resume", "dispatch_status": "accepted"}) as launch:
            state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "approve"))
            duplicate = state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "approve"))
        self.assertEqual(duplicate["status"], "already_answered")
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(service.get_task(task.id).extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], 120)

    def test_stale_resource_snapshot_fails_before_any_mutation(self) -> None:
        (service, task, _) = self._waiting_task("claude", 10.0)
        raw = service.store.read_task(task.id)
        raw["extensions"]["agentbc.input"]["next_limit"] = 30.0
        service.store.write_task(task.id, raw)
        request = service.get_task(task.id).extensions["agentbc.input"]
        with self.assertRaises(_resource_decision_ABCError) as raised:
            service.respond_to_input(task.id, request["input_id"], response_type="approve")
        self.assertEqual(raised.exception.code, "resource_decision_stale")
        current = service.get_task(task.id)
        self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], 10.0)
        self.assertEqual(current.extensions["agentbc.input"]["status"], "waiting")

    def test_expired_resource_input_enters_recovery_without_doubling(self) -> None:
        (service, task, _) = self._waiting_task("hermes", 60)
        raw = service.store.read_task(task.id)
        raw["extensions"]["agentbc.input"]["deadline_at"] = "2026-01-01T00:00:00Z"
        service.store.write_task(task.id, raw)
        state = self._runner("hermes")
        with _resource_decision_mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=_resource_decision_DeliveryResult(True, "shown")):
            expired = state.maintain_waiting_inputs(now="2026-01-02T00:00:00Z")
        current = service.get_task(task.id)
        self.assertEqual(expired, [{"task_id": task.id, "input_id": "input-hermes-1"}])
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], 60)
        self.assertEqual(current.extensions["agentbc.input"]["status"], "expired")

    def test_resume_dispatch_failure_is_recoverable_and_not_reapplied(self) -> None:
        (service, task, _) = self._waiting_task("claude", 10.0)
        request = task.extensions["agentbc.input"]
        state = self._runner("claude")
        with _resource_decision_mock.patch.object(state, "dispatch_worker", side_effect=_resource_decision_RunnerError("resume unavailable")) as launch, _resource_decision_mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=_resource_decision_DeliveryResult(True, "shown")):
            with self.assertRaisesRegex(_resource_decision_RunnerError, "resume unavailable"):
                state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "approve"))
            duplicate = state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "approve"))
        current = service.get_task(task.id)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(duplicate["status"], "already_answered")
        self.assertEqual(current.status, "needs_recovery")
        self.assertEqual(current.errors[-1]["code"], "input_resume_dispatch_failed")
        self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], 20.0)

    def test_deny_fails_without_dispatch_and_writes_terminal_evidence(self) -> None:
        cases = (("claude", 10.0, "budget_exhausted_user_terminated"), ("hermes", 60, "iteration_exhausted_user_terminated"))
        for executor, limit, failure_kind in cases:
            with self.subTest(executor=executor):
                subroot = self.root / f"deny-{executor}"
                subroot.mkdir()
                self.board = subroot / "record"
                self.project = subroot / "project"
                self.project.mkdir()
                (service, task, _) = self._waiting_task(executor, limit)
                request = task.extensions["agentbc.input"]
                state = self._runner(executor)
                with _resource_decision_mock.patch.object(state, "dispatch_worker") as launch, _resource_decision_mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=_resource_decision_DeliveryResult(True, "shown")):
                    result = state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "deny"))
                    duplicate = state.respond_and_dispatch(self._respond_request(task.id, request["input_id"], "deny"))
                current = service.get_task(task.id)
                failure = current.errors[-1]["details"]["failure"]
                report = _resource_decision_generate_report(task.id, self.board)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(duplicate["status"], "already_answered")
                self.assertFalse(result["dispatch_required"])
                self.assertFalse(launch.called)
                self.assertEqual(current.status, "failed")
                self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["current_limit"], limit)
                self.assertEqual(current.extensions[_resource_decision_RESOURCE_EXTENSION_KEY]["last_decision"], "terminate")
                self.assertEqual(current.extensions["agentbc.input"]["status"], "answered")
                self.assertEqual(failure["kind"], failure_kind)
                self.assertFalse(failure["retryable"])
                self.assertEqual(report["failure_code"], failure_kind)
                self.assertEqual(_resource_decision_load_lease(task.id, self.board).state, _resource_decision_RunLeaseState.CLOSED)
                self.assertTrue(_resource_decision_Path(current.workspace["report_file"]).is_file())
