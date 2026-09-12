"""FLOW-104-003: mechanical handoff recovery for a failed chain head.

Covers the failed-current-head handoff contract end to end at the service
level: source selection and rejection, mechanical requirement/report import,
locked inherited steps, deterministic brief/prompt projection, the all-steps-done
terminal-verification closeout, source immutability, single-iteration
guarantees under duplicate and concurrent replay, Runner restart, chained
re-failure, the strict callback contract and status/report lineage.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import time as time_module
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_contract import (
    INHERITED_DONE_STATUS,
    validate_callback_payload,
)
from agent_bridge_connect.execution_policy import execution_policy_view
from agent_bridge_connect.handoff_recovery import (
    HANDOFF_RECOVERY_EXTENSION_KEY,
    HANDOFF_RECOVERY_LOCK_STALE_S,
    HANDOFF_RECOVERY_REVIVAL_ERROR,
    SOURCE_REPORT_STEP_MISMATCH,
    parse_report_step_statuses,
)
from agent_bridge_connect.permission_modes import permission_record_from_extensions
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report as core_report
from agent_bridge_connect.service import HANDOFF_SOURCE_STATUSES, TaskService
from agent_bridge_connect.executors.claude import _build_prompt as build_claude_prompt
from tests.contract_helpers import completed_callback


def recovery_record(task) -> dict:
    return dict(task.extensions or {})[HANDOFF_RECOVERY_EXTENSION_KEY]


class HandoffRecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.board = self.root / "board"
        self.project = self.root / "project"
        self.project.mkdir(parents=True)
        self.config = {"workspace_root": str(self.board)}
        self.service = TaskService(self.board, config=self.config)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    def failed_task(
        self,
        title="Failed source",
        assignee="claude",
        steps=(("first requirement", True), ("second requirement", False), ("third requirement", False)),
        code="completion_marker_missing",
        recover=False,
    ):
        task = self.service.create_task(
            title,
            assignee,
            [{"id": index, "description": description} for index, (description, _) in enumerate(steps, 1)],
            customer_dir=True,
            customer_path=self.project,
        )
        self.service.claim_task(task.id, assignee)
        for index, (_, done) in enumerate(steps, 1):
            if done:
                self.service.execute_step(task.id, index, {"status": "done"})
        if recover:
            self.service.mark_task_needs_recovery(task.id, code, "executor stopped", {})
        else:
            self.service.mark_task_failed(task.id, code, "executor stopped", {})
        task = self.service.get_task(task.id)
        extensions = dict(task.extensions or {})
        session = dict(extensions.get("agentbc.session") or {})
        cleanup = dict(session.get("cleanup") or {})
        cleanup.update({"state": "unsupported", "capability": "unsupported"})
        session.update({"session_state": "terminal", "cleanup": cleanup})
        extensions["agentbc.session"] = session
        task.extensions = extensions
        self.service.store.write_task(task.id, task.to_dict())
        return task

    def task_packet(self, task) -> dict:
        return {
            "task_id": task.id,
            "assignee": task.assignee,
            "title": task.title,
            "steps": task.steps,
            "workspace": task.workspace,
            "task_board": {"root": str(self.board)},
            "extensions": task.extensions,
        }

    def settle_session_cleanup(self, task_id: str) -> None:
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        session = dict(extensions.get("agentbc.session") or {})
        cleanup = dict(session.get("cleanup") or {})
        cleanup.update({"state": "unsupported", "capability": "unsupported"})
        session.update({"session_state": "terminal", "cleanup": cleanup})
        extensions["agentbc.session"] = session
        task.extensions = extensions
        self.service.store.write_task(task.id, task.to_dict())

    def assertNoRecoveryEvent(self, source):
        events = [event["event_type"] for event in self.service.store.read_events(source.id)]
        self.assertNotIn("handoff_recovery_created", events)


class HandoffSourceSelectionTests(HandoffRecoveryTestCase):
    def test_failed_statuses_are_valid_handoff_sources(self):
        self.assertEqual(
            HANDOFF_SOURCE_STATUSES,
            {"completed", "failed", "needs_recovery"},
        )

    def test_same_executor_failed_handoff_creates_next_iteration(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "claude", "keep going")

        self.assertEqual(handoff.id, f"{source.workspace['task_code']}-002")
        self.assertEqual(handoff.assignee, "claude")
        self.assertEqual(handoff.status, "pending")
        lineage = handoff.extensions["agentbc.lineage"]
        self.assertEqual(lineage["parent_task_id"], source.id)
        self.assertEqual(lineage["chain_root_task_id"], source.id)
        self.assertEqual(lineage["iteration_index"], 2)
        self.assertEqual(handoff.workspace["customer_path"], source.workspace["customer_path"])
        self.assertEqual(handoff.workspace["task_date"], source.workspace["task_date"])
        self.assertEqual(handoff.workspace["artifact_root"], source.workspace["artifact_root"])
        from agent_bridge_connect.revival import validate_revival_reservation

        source_revival = self.service.get_task(source.id).extensions["agentbc.revival"]
        target_revival = handoff.extensions["agentbc.revival"]
        self.assertEqual(source_revival, target_revival)
        self.assertEqual(target_revival["operation"], "handoff")
        self.assertEqual(target_revival["state"], "committed")
        self.assertEqual(target_revival["target_task_id"], handoff.id)
        self.assertEqual(validate_revival_reservation(target_revival), [])
        self.assertNoRecoveryEvent(source)

    def test_cross_executor_failed_handoff_keeps_artifact_lineage(self):
        source = self.failed_task(assignee="claude")
        handoff = self.service.handoff_task(source.id, "codex", "hand over")

        self.assertEqual(source.assignee, "claude")
        self.assertEqual(handoff.assignee, "codex")
        self.assertEqual(handoff.workspace["project_root"], source.workspace["project_root"])
        self.assertEqual(handoff.workspace["artifact_root"], source.workspace["artifact_root"])
        self.assertEqual(
            handoff.extensions["agentbc.lineage"]["base_task_id"],
            source.id,
        )

    def test_needs_recovery_source_uses_the_same_handoff_protocol(self):
        source = self.failed_task(recover=True)
        handoff = self.service.handoff_task(source.id, "codex")

        self.assertEqual(handoff.id, f"{source.workspace['task_code']}-002")
        self.assertEqual(handoff.status, "pending")
        self.assertEqual(recovery_record(handoff)["source_status"], "needs_recovery")
        refreshed = self.service.get_task(source.id)
        self.assertEqual(refreshed.status, "needs_recovery")
        self.assertEqual(refreshed.extensions["agentbc.revival"]["state"], "committed")

    def test_source_immutability_after_handoff(self):
        source = self.failed_task()
        task_dir = Path(source.workspace["internal_task_dir"])

        def evidence():
            return {
                "brief": Path(source.workspace["task_file"]).read_bytes(),
                "report": Path(source.workspace["report_file"]).read_bytes(),
                "events": (task_dir / "events.jsonl").read_bytes(),
                "interventions": self._file_bytes(task_dir / "interventions.jsonl"),
                "delivery": self._file_bytes(task_dir / "delivery.jsonl"),
            }

        before = evidence()
        self.service.handoff_task(source.id, "codex", "continue")
        self.assertEqual(before, evidence())

        refreshed = self.service.get_task(source.id)
        self.assertEqual(refreshed.status, "failed")
        self.assertEqual(refreshed.extensions["agentbc.terminal_delivery"]["terminal_state"], "failed")
        self.assertEqual(refreshed.extensions["agentbc.revival"]["state"], "committed")
        self.assertEqual(len(refreshed.errors), 1)

    @staticmethod
    def _file_bytes(path: Path) -> bytes:
        return path.read_bytes() if path.exists() else b""

    def test_rejection_of_stale_non_head_source(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "hermes")
        self.assertEqual(raised.exception.code, "stale_handoff_source")
        self.assertEqual(
            raised.exception.details["suggested_command"],
            f"agentbc task handoff {handoff.id} --to hermes",
        )

    def test_rejection_of_source_with_active_lease(self):
        source = self.failed_task()
        self.service.store.acquire_lease(source.id, "claude")

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "codex")
        self.assertEqual(raised.exception.code, "handoff_source_leased")

    def test_rejection_of_source_with_waiting_input(self):
        source = self.failed_task()
        task = self.service.get_task(source.id)
        task.extensions = dict(task.extensions or {})
        task.extensions["agentbc.input"] = {"status": "waiting", "input_id": "in-1"}
        self.service.store.write_task(source.id, task.to_dict())

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "codex")
        self.assertEqual(raised.exception.code, "input_pending")
        self.assertEqual(raised.exception.details["input_id"], "in-1")

    def test_rejection_of_source_with_cleanup_in_progress(self):
        source = self.failed_task()
        task = self.service.get_task(source.id)
        task.extensions = dict(task.extensions or {})
        session = dict(task.extensions.get("agentbc.session") or {})
        session["cleanup"] = {"state": "pending"}
        task.extensions["agentbc.session"] = session
        self.service.store.write_task(source.id, task.to_dict())

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "codex")
        self.assertEqual(raised.exception.code, "handoff_source_cleanup_pending")

    def test_non_failed_status_still_uses_completed_handoff_contract(self):
        source = self.failed_task()
        self.service.store.write_task(
            source.id,
            {**self.service.store.read_task(source.id), "status": "completed"},
        )
        handoff = self.service.handoff_task(source.id, "codex", "next step")
        self.assertNotIn(HANDOFF_RECOVERY_EXTENSION_KEY, handoff.extensions or {})

    def test_plain_handoff_confirms_baseline_then_dispatches_atomically(self):
        source = self.failed_task()
        from agent_bridge_connect.cli import main

        result = {
            "task_id": f"{source.workspace['task_code']}-002",
            "assignee": "claude",
            "workspace": source.workspace,
            "run_id": "handoff-worker",
            "dispatch_status": "accepted",
            "monitor_status": "opened",
        }
        output = StringIO()
        with (
            mock.patch("builtins.input", return_value="y") as confirm,
            mock.patch(
                "agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch",
                return_value=result,
            ) as dispatch,
            contextlib.redirect_stdout(output),
        ):
            code = main(
                [
                    "task",
                    "handoff",
                    source.id,
                    "--to",
                    "claude",
                    "--root",
                    str(self.board),
                ]
            )

        self.assertEqual(code, 0)
        confirm.assert_called_once_with("Continue and dispatch? [y/N]: ")
        dispatch.assert_called_once()
        text = output.getvalue()
        self.assertIn(f"Continue {source.id} from its existing baseline.", text)
        self.assertIn(f"create {source.workspace['task_code']}-002", text)
        self.assertIn("preserve its report and artifacts", text)
        self.assertIn(f"dispatched: {source.workspace['task_code']}-002", text)

    def test_plain_needs_recovery_handoff_uses_the_same_confirmation(self):
        source = self.failed_task(recover=True)
        from agent_bridge_connect.cli import main

        result = {
            "task_id": f"{source.workspace['task_code']}-002",
            "assignee": "claude",
            "workspace": source.workspace,
            "run_id": "recovery-handoff-worker",
            "dispatch_status": "accepted",
            "monitor_status": "opened",
        }
        with (
            mock.patch("builtins.input", return_value="y") as confirm,
            mock.patch(
                "agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch",
                return_value=result,
            ) as dispatch,
        ):
            code = main(
                [
                    "task",
                    "handoff",
                    source.id,
                    "--to",
                    "claude",
                    "--root",
                    str(self.board),
                ]
            )

        self.assertEqual(code, 0)
        confirm.assert_called_once_with("Continue and dispatch? [y/N]: ")
        dispatch.assert_called_once()

    def test_plain_handoff_no_creates_nothing_and_does_not_dispatch(self):
        source = self.failed_task()
        before = {task.id for task in self.service.list_tasks()}
        from agent_bridge_connect.cli import main

        output = StringIO()
        with (
            mock.patch("builtins.input", return_value="n"),
            mock.patch(
                "agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch"
            ) as dispatch,
            contextlib.redirect_stdout(output),
        ):
            code = main(
                [
                    "task",
                    "handoff",
                    source.id,
                    "--to",
                    "claude",
                    "--root",
                    str(self.board),
                ]
            )

        self.assertEqual(code, 0)
        dispatch.assert_not_called()
        self.assertIn("handoff_cancelled", output.getvalue())
        self.assertEqual({task.id for task in self.service.list_tasks()}, before)


class RequirementImportTests(HandoffRecoveryTestCase):
    def test_full_requirement_and_report_import(self):
        source = self.failed_task()
        brief_bytes = Path(source.workspace["task_file"]).read_bytes()
        report_bytes = Path(source.workspace["report_file"]).read_bytes()

        handoff = self.service.handoff_task(source.id, "codex", "finish it")
        record = recovery_record(handoff)

        self.assertEqual(record["source_task_id"], source.id)
        self.assertEqual(record["source_status"], "failed")
        self.assertEqual(record["source_failure_code"], "completion_marker_missing")
        self.assertEqual(record["source_task_brief"]["path"], source.workspace["task_file"])
        self.assertEqual(record["source_task_brief"]["bytes"], len(brief_bytes))
        self.assertEqual(
            record["source_task_brief"]["sha256"],
            hashlib.sha256(brief_bytes).hexdigest(),
        )
        self.assertEqual(record["source_report"]["path"], source.workspace["report_file"])
        self.assertEqual(record["source_report"]["bytes"], len(report_bytes))
        self.assertFalse(record["source_report"]["regenerated"])
        self.assertEqual(record["source_report_step_mismatch"], [])
        self.assertEqual(record["additive_message"], "finish it")

        snapshot_brief = Path(record["source_task_brief"]["snapshot_path"]).read_bytes()
        snapshot_report = Path(record["source_report"]["snapshot_path"]).read_bytes()
        self.assertEqual(snapshot_brief, brief_bytes)
        self.assertEqual(snapshot_report, report_bytes)

    def test_report_step_statuses_are_parsed_from_the_canonical_report(self):
        source = self.failed_task()
        report_text = Path(source.workspace["report_file"]).read_text(encoding="utf-8")
        statuses = parse_report_step_statuses(report_text)
        self.assertEqual(statuses[1], "done")
        self.assertEqual(statuses[2], "pending")
        self.assertEqual(statuses[3], "pending")

    def test_report_mismatch_continues_from_task_state(self):
        source = self.failed_task()
        report_file = Path(source.workspace["report_file"])
        report_file.write_text(
            report_file.read_text(encoding="utf-8").replace("1. [done] first", "1. [blocked] first"),
            encoding="utf-8",
        )

        handoff = self.service.handoff_task(source.id, "codex")
        record = recovery_record(handoff)

        self.assertEqual(
            record[SOURCE_REPORT_STEP_MISMATCH],
            [
                {
                    "step_id": 1,
                    "task_status": "done",
                    "report_status": "blocked",
                    "reason": "status_differs",
                }
            ],
        )
        # Task state stays authoritative: the done step is still locked.
        self.assertEqual(handoff.steps[0]["status"], INHERITED_DONE_STATUS)
        self.assertEqual(handoff.steps[0]["origin_status"], "done")

    def test_missing_report_step_is_reported_as_a_mismatch(self):
        source = self.failed_task()
        report_file = Path(source.workspace["report_file"])
        report_file.write_text(
            report_file.read_text(encoding="utf-8").replace(
                "3. [pending] third requirement\n", ""
            ),
            encoding="utf-8",
        )

        handoff = self.service.handoff_task(source.id, "codex")
        mismatch = recovery_record(handoff)[SOURCE_REPORT_STEP_MISMATCH]
        self.assertEqual(mismatch[-1]["step_id"], 3)
        self.assertEqual(mismatch[-1]["reason"], "missing_from_report")

    def test_canonical_report_regeneration_when_report_is_absent(self):
        source = self.failed_task()
        report_file = Path(source.workspace["report_file"])
        report_file.unlink()
        self.assertFalse(report_file.exists())

        handoff = self.service.handoff_task(source.id, "codex")
        record = recovery_record(handoff)

        self.assertTrue(record["source_report"]["regenerated"])
        self.assertTrue(report_file.is_file())
        self.assertTrue(Path(record["source_report"]["path"]).is_file())
        self.assertEqual(record["source_report_step_mismatch"], [])
        # The imported snapshot is byte-identical to the regenerated canonical
        # report so the recovery evidence cannot drift from Core's projection.
        self.assertEqual(
            Path(record["source_report"]["snapshot_path"]).read_bytes(),
            report_file.read_bytes(),
        )

    def test_unreadable_requirements_fail_with_the_shared_revival_error(self):
        source = self.failed_task()
        task = self.service.get_task(source.id)
        task.workspace = dict(task.workspace)
        task.workspace["task_file"] = str(self.root / "absent-task.md")
        self.service.store.write_task(source.id, task.to_dict())

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "codex")
        self.assertEqual(raised.exception.code, HANDOFF_RECOVERY_REVIVAL_ERROR)
        self.assertEqual(raised.exception.details["reason"], "requirements_unreadable")

    def test_invalid_path_plan_fails_with_the_shared_revival_error(self):
        source = self.failed_task()
        task = self.service.get_task(source.id)
        task.workspace = dict(task.workspace)
        task.workspace.pop("report_root")
        self.service.store.write_task(source.id, task.to_dict())

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "codex")
        self.assertEqual(raised.exception.code, HANDOFF_RECOVERY_REVIVAL_ERROR)
        self.assertEqual(raised.exception.details["reason"], "path_plan_invalid")

    def test_invalid_lineage_fails_with_the_shared_revival_error(self):
        source = self.failed_task()
        sibling = self.service.handoff_task(source.id, "hermes", branch=True)
        data = self.service.store.read_task(sibling.id)
        data["extensions"]["agentbc.lineage"]["parent_task_id"] = f"{source.workspace['task_code']}-999"
        self.service.store.write_task(sibling.id, data)

        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "codex")
        self.assertEqual(raised.exception.code, HANDOFF_RECOVERY_REVIVAL_ERROR)
        self.assertEqual(raised.exception.details["reason"], "lineage_invalid")


class RecoveryStepPlanTests(HandoffRecoveryTestCase):
    def test_done_steps_are_locked_inherited_done_with_origin(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")

        self.assertEqual(
            [step["status"] for step in handoff.steps],
            [INHERITED_DONE_STATUS, "pending", "pending"],
        )
        locked = handoff.steps[0]
        self.assertIs(locked["locked"], True)
        self.assertEqual(locked["origin_task_id"], source.id)
        self.assertEqual(locked["origin_step_id"], 1)
        self.assertEqual(locked["origin_status"], "done")
        self.assertEqual(locked["description"], "first requirement")
        self.assertEqual(recovery_record(handoff)["locked_step_ids"], [1])
        self.assertEqual(recovery_record(handoff)["remaining_step_ids"], [2, 3])

    def test_failed_blocked_and_pending_steps_reset_to_pending(self):
        source = self.failed_task()
        task = self.service.get_task(source.id)
        task.steps[1]["status"] = "failed"
        task.steps[2]["status"] = "blocked"
        self.service.store.write_task(source.id, task.to_dict())

        handoff = self.service.handoff_task(source.id, "codex")
        self.assertEqual(
            [step["status"] for step in handoff.steps],
            [INHERITED_DONE_STATUS, "pending", "pending"],
        )
        self.assertEqual(handoff.steps[1]["origin_status"], "failed")
        self.assertEqual(handoff.steps[2]["origin_status"], "blocked")

    def test_all_steps_done_adds_one_terminal_verification_closeout(self):
        source = self.failed_task(
            steps=(("only requirement", True),),
            code="completion_marker_missing",
        )
        handoff = self.service.handoff_task(source.id, "codex")

        self.assertEqual(len(handoff.steps), 2)
        closeout = handoff.steps[-1]
        self.assertEqual(closeout["id"], 2)
        self.assertEqual(closeout["status"], "pending")
        self.assertIs(closeout["terminal_verification"], True)
        self.assertIsNone(closeout["origin_step_id"])
        self.assertEqual(
            recovery_record(handoff)["terminal_verification_step_id"],
            2,
        )

    def test_closeout_is_not_duplicated_across_a_chained_recovery(self):
        source = self.failed_task(steps=(("only requirement", True),))
        first = self.service.handoff_task(source.id, "codex")
        self.service.store.write_task(
            first.id,
            {**self.service.store.read_task(first.id), "status": "failed"},
        )
        self.settle_session_cleanup(first.id)
        second = self.service.handoff_task(first.id, "hermes")
        self.assertEqual(len(second.steps), 2)
        self.assertEqual(
            [step["id"] for step in second.steps],
            [1, 2],
        )

    def test_locked_step_cannot_execute_again(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        self.service.start_task_run(handoff.id, "codex")

        with self.assertRaises(ABCError) as raised:
            self.service.execute_step(handoff.id, 1, {"status": "failed"})
        self.assertEqual(raised.exception.code, "inherited_step_locked")
        self.assertEqual(raised.exception.details["origin_task_id"], source.id)

        # The remaining step still executes normally.
        self.service.execute_step(handoff.id, 2, {"status": "done"})
        self.assertEqual(self.service.get_task(handoff.id).steps[1]["status"], "done")

    def test_remaining_steps_execute_and_finalize_with_locked_steps_done(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        self.service.start_task_run(handoff.id, "codex")
        self.service.execute_step(handoff.id, 2, {"status": "done"})
        self.service.execute_step(handoff.id, 3, {"status": "done"})

        task = self.service.get_task(handoff.id)
        finalized = self.service.finalize_task_from_agent(
            handoff.id,
            completed_callback(task, summary="recovery finished"),
        )

        self.assertTrue(finalized)
        completed = self.service.get_task(handoff.id)
        self.assertEqual(completed.status, "completed")
        report = core_report(handoff.id, self.board)
        self.assertEqual(report["summary"]["steps_done"], report["summary"]["steps_total"])
        self.assertTrue(report["flow_contract_satisfied"])


class CallbackContractTests(HandoffRecoveryTestCase):
    def test_locked_step_may_only_be_reported_as_done(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        validation = validate_callback_payload(
            {
                "version": 1,
                "task_id": handoff.id,
                "final_state": "input_required",
                "summary": "needs a decision",
                "step_results": [
                    {"id": 1, "status": "failed"},
                    {"id": 2, "status": "blocked"},
                ],
                "input": {
                    "type": "permission",
                    "reason": "needs filesystem access",
                    "requested_permission": "full",
                },
            },
            handoff.id,
            handoff.steps,
        )
        self.assertFalse(validation.valid)
        self.assertEqual(validation.code, "completion_marker_locked_step_invalid")

    def test_valid_callback_reports_locked_steps_as_done(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        validation = validate_callback_payload(
            {
                "version": 1,
                "task_id": handoff.id,
                "final_state": "completed",
                "summary": "done",
                "step_results": [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}, {"id": 3, "status": "done"}],
            },
            handoff.id,
            handoff.steps,
        )
        self.assertTrue(validation.valid)


class FrozenPolicyInheritanceTests(HandoffRecoveryTestCase):
    def test_permission_is_inherited_unless_overridden(self):
        source = self.failed_task()
        inherited = permission_record_from_extensions(source.extensions)
        handoff = self.service.handoff_task(source.id, "claude")
        projected = permission_record_from_extensions(handoff.extensions)
        self.assertEqual(projected["requested_mode"], inherited["requested_mode"])
        self.assertEqual(projected["effective_mode"], inherited["effective_mode"])
        self.assertEqual(projected["selection_source"], "inherited_task")
        self.assertFalse(recovery_record(handoff)["permission_override"])

        override_source = self.failed_task(title="Override source")
        overridden = self.service.handoff_task(
            override_source.id, "hermes", permission_mode="safe"
        )
        projected_override = permission_record_from_extensions(overridden.extensions)
        self.assertEqual(projected_override["requested_mode"], "safe")
        self.assertEqual(projected_override["selection_source"], "explicit_task")
        self.assertTrue(recovery_record(overridden)["permission_override"])

    def test_frozen_resource_policy_is_inherited_for_the_same_executor(self):
        source = self.failed_task(assignee="claude")
        source_view = execution_policy_view(source.extensions)["resources"]
        handoff = self.service.handoff_task(source.id, "claude")
        inherited_view = execution_policy_view(handoff.extensions)["resources"]

        self.assertEqual(inherited_view["resource"], source_view["resource"])
        self.assertEqual(inherited_view["limit"], source_view["limit"])
        self.assertEqual(inherited_view["configured_limit"], source_view["configured_limit"])
        self.assertEqual(inherited_view["source"], source_view["source"])
        self.assertTrue(inherited_view["frozen"])
        self.assertEqual(inherited_view["exhaustion_count"], 0)

    def test_cross_executor_handoff_rebuilds_target_policy(self):
        source = self.failed_task(assignee="claude")
        handoff = self.service.handoff_task(source.id, "codex")
        inherited_view = execution_policy_view(handoff.extensions)
        self.assertNotEqual(
            (inherited_view["resources"] or {}).get("executor"),
            "claude",
        )
        # A new iteration never resumes the source executor session.
        self.assertNotEqual(
            (handoff.extensions or {}).get("agentbc.session", {}).get("session_id"),
            (source.extensions or {}).get("agentbc.session", {}).get("session_id"),
        )

    def test_inherited_images_and_additive_message_are_carried(self):
        source = self.failed_task()
        image = self.project / "input.png"
        image.write_bytes(b"png")
        task = self.service.get_task(source.id)
        task.extensions = dict(task.extensions or {})
        task.extensions["agentbc.media"] = {"images": [str(image)]}
        self.service.store.write_task(source.id, task.to_dict())

        handoff = self.service.handoff_task(source.id, "codex", "add a changelog entry")
        self.assertEqual(
            (handoff.extensions or {}).get("agentbc.media", {}).get("images"),
            [str(image.resolve())],
        )
        self.assertEqual(recovery_record(handoff)["additive_message"], "add a changelog entry")


class DeterministicProjectionTests(HandoffRecoveryTestCase):
    def test_task_brief_lists_requirements_locked_steps_and_digests(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex", "add release notes")
        brief = Path(handoff.workspace["task_file"]).read_text(encoding="utf-8")

        self.assertIn("## Handoff Recovery", brief)
        self.assertIn(f"- Source task: `{source.id}`", brief)
        self.assertIn("- Source status: `failed`", brief)
        self.assertIn("- Source failure code: `completion_marker_missing`", brief)
        self.assertIn(
            f"- Imported source report: `{source.workspace['report_file']}`", brief
        )
        self.assertIn(
            recovery_record(handoff)["source_report"]["sha256"], brief
        )
        self.assertIn(
            "- Locked inherited steps (already done, never re-execute): `1`", brief
        )
        self.assertIn("- Executable remaining steps: `2, 3`", brief)
        self.assertIn("- Additive handoff goal: add release notes", brief)
        self.assertIn("### Inherited Requirements", brief)
        self.assertIn("1. first requirement [inherited status: inherited_done]", brief)
        self.assertIn("3. third requirement [inherited status: pending]", brief)
        self.assertIn("### Recovery Rules", brief)

    def test_task_brief_is_deterministic_for_one_task_state(self):
        from agent_bridge_connect.service import _write_task_requirements

        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        brief_file = Path(handoff.workspace["task_file"])
        brief_first = brief_file.read_bytes()

        _write_task_requirements(self.service.get_task(handoff.id), brief_file)
        self.assertEqual(brief_first, brief_file.read_bytes())

    def test_executor_prompt_lists_recovery_contract(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex", "polish the README")
        prompt = build_claude_prompt(self.task_packet(handoff))

        self.assertIn("Handoff recovery:", prompt)
        self.assertIn(f"- Source task: {source.id} (status: failed", prompt)
        self.assertIn("- Imported source report: ", prompt)
        self.assertIn("(sha256 ", prompt)
        self.assertIn("- Locked inherited steps (already done, never re-execute): 1", prompt)
        self.assertIn("- Remaining executable steps: 2, 3", prompt)
        self.assertIn("- Additive handoff goal: polish the README", prompt)
        self.assertIn(
            "- Never re-execute a locked inherited step; report it as done only.", prompt
        )
        self.assertIn(
            "cannot choose the resume step",
            prompt,
        )
        self.assertIn("[status: inherited_done]", prompt)
        self.assertIn("[status: pending]", prompt)

    def test_non_recovery_prompt_has_no_recovery_block(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        plain = self.service.create_task(
            "Plain task",
            "codex",
            [{"id": 1, "description": "plain"}],
            customer_dir=False,
        )
        self.assertNotIn("Handoff recovery:", build_claude_prompt(self.task_packet(plain)))
        self.assertIn("Handoff recovery:", build_claude_prompt(self.task_packet(handoff)))


class SingleIterationTests(HandoffRecoveryTestCase):
    def test_duplicate_handoff_returns_the_existing_iteration(self):
        source = self.failed_task()
        first = self.service.handoff_task(source.id, "codex", "first")
        known = {task.id for task in self.service.list_tasks()}

        second = self.service.handoff_task(source.id, "hermes", branch=True)

        self.assertEqual(second.id, first.id)
        self.assertEqual({task.id for task in self.service.list_tasks()}, known)

    def test_concurrent_replay_creates_at_most_one_iteration(self):
        source = self.failed_task()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.handoff_task(source.id, "codex"), range(4)))

        self.assertEqual({task.id for task in results}, {f"{source.workspace['task_code']}-002"})
        chain_dir = self.board / source.workspace["task_code"]
        self.assertEqual(
            sorted(path.name for path in chain_dir.iterdir() if path.is_dir() and path.name.isdigit()),
            ["001", "002"],
        )
        self.assertEqual(len(results[0].steps), 3)

    def test_lock_held_rejects_with_a_stable_code(self):
        from agent_bridge_connect import handoff_recovery
        from agent_bridge_connect.handoff_recovery import handoff_lock_path

        source = self.failed_task()
        lock_path = handoff_lock_path(self.board, source.workspace["task_code"])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("held", encoding="utf-8")
        try:
            with mock.patch.object(handoff_recovery, "HANDOFF_RECOVERY_LOCK_WAIT_S", 0.05):
                with self.assertRaises(ABCError) as raised:
                    self.service.handoff_task(source.id, "codex")
        finally:
            lock_path.unlink(missing_ok=True)
        self.assertEqual(raised.exception.code, "handoff_in_progress")

    def test_stale_lock_is_broken_and_handoff_proceeds(self):
        from agent_bridge_connect.handoff_recovery import handoff_lock_path

        source = self.failed_task()
        lock_path = handoff_lock_path(self.board, source.workspace["task_code"])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("stale", encoding="utf-8")
        old = time_module.time() - HANDOFF_RECOVERY_LOCK_STALE_S - 10
        os.utime(lock_path, (old, old))

        handoff = self.service.handoff_task(source.id, "codex")
        self.assertEqual(handoff.id, f"{source.workspace['task_code']}-002")
        self.assertFalse(lock_path.exists())


class RestartAndChainTests(HandoffRecoveryTestCase):
    def test_runner_restart_sees_the_recovery_iteration(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex", "continue")

        restarted = TaskService(self.board, config=self.config)
        recovered = restarted.get_task(handoff.id)
        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.assignee, "codex")
        self.assertEqual(
            recovery_record(recovered)["source_task_id"], source.id
        )
        self.assertEqual(
            restarted.resolve_chain(handoff.id).current_head_task_id,
            handoff.id,
        )
        self.assertTrue(restarted.preflight(handoff.id).ok)

    def test_chained_re_failure_keeps_inherited_work_locked(self):
        source = self.failed_task()
        first = self.service.handoff_task(source.id, "codex")
        self.service.start_task_run(first.id, "codex")
        self.service.execute_step(first.id, 2, {"status": "done"})
        self.service.mark_task_failed(first.id, "executor_terminal_failure", "step failed", {})
        self.settle_session_cleanup(first.id)

        second = self.service.handoff_task(first.id, "hermes")
        self.assertEqual(second.id, f"{source.workspace['task_code']}-003")
        self.assertEqual(
            [step["status"] for step in second.steps],
            [INHERITED_DONE_STATUS, INHERITED_DONE_STATUS, "pending"],
        )
        self.assertEqual(second.steps[0]["origin_task_id"], first.id)
        self.assertEqual(second.steps[1]["origin_status"], "done")

    def test_status_and_report_lineage_point_at_the_new_head(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")

        status = self.service._task_status_with_chain(self.service.get_task(handoff.id))
        self.assertEqual(status["is_chain_head"], True)
        self.assertEqual(status["parent_task_id"], source.id)
        self.assertEqual(status["chain_root_task_id"], source.id)
        self.assertEqual(status["iteration_index"], 2)
        self.assertEqual(status["task_code"], source.workspace["task_code"])
        self.assertEqual(status["chain_anomalies"], [])

        report = core_report(handoff.id, self.board)
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["chain"]["current_head_task_id"], handoff.id)
        self.assertTrue(report["chain"]["requested_is_head"])
        self.assertEqual(report["lineage"]["parent_task_id"], source.id)
        self.assertEqual(report["lineage"]["iteration_index"], 2)

    def test_source_report_digest_is_stable_across_a_restart(self):
        source = self.failed_task()
        handoff = self.service.handoff_task(source.id, "codex")
        digest = recovery_record(handoff)["source_report"]["sha256"]

        restarted = TaskService(self.board, config=self.config)
        self.assertEqual(
            recovery_record(restarted.get_task(handoff.id))["source_report"]["sha256"],
            digest,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
