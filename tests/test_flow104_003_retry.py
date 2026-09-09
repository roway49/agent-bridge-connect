"""Focused FLOW-104-003 failed-task full-retry coverage."""

from __future__ import annotations

import copy
import contextlib
from io import StringIO
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService, task_to_status


class FailedTaskRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.board = root / "record"
        self.workspace = root / "workspace"
        self.config = {"workspace_root": str(self.workspace)}
        self.service = TaskService(self.board, config=self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _failed_task(
        self,
        *,
        customer_dir: bool = False,
        assignee: str = "codex",
    ):
        customer = None
        if customer_dir:
            customer = Path(self.temporary.name) / "customer"
            customer.mkdir(parents=True, exist_ok=True)
        task = self.service.create_task(
            "Retry target",
            assignee,
            [
                {"id": 1, "description": "first requirement"},
                {"id": 2, "description": "second requirement"},
                {"id": 3, "description": "third requirement"},
            ],
            customer_dir=customer_dir,
            customer_path=customer,
        )
        task.status = "failed"
        task.steps[0].update({"status": "done", "result": "old result"})
        task.steps[1].update({"status": "failed", "error": "old failure"})
        task.steps[2].update({"status": "blocked", "artifacts": ["old.txt"]})
        task.errors = [{"code": "executor_failed", "message": "old failure"}]
        extensions = dict(task.extensions or {})
        extensions["agentbc.input"] = {
            "input_id": "input-old",
            "status": "answered",
            "summary": "old answer",
        }
        extensions["agentbc.permission_runtime"] = {"state": "old-runtime"}
        extensions["agentbc.completion_intent"] = {"state": "old-intent"}
        extensions["agentbc.final_callback"] = {"marker_valid": True}
        extensions["agentbc.terminal_delivery"] = {"state": "delivered"}
        from agent_bridge_connect.permission_grants import build_permission_grant

        extensions["agentbc.permission_grant"] = build_permission_grant(
            executor=assignee,
            task_id=task.id,
            input_id="input-old",
            session_id="old-session",
            source_run_id="old-run",
        )
        execution = dict(extensions.get("agentbc.execution") or {})
        execution.update(
            {
                "internal_status": "failed",
                "worker_run_id": "old-worker",
                "dispatch_status": "accepted",
                "worker_pid": 999999,
            }
        )
        extensions["agentbc.execution"] = execution
        session = dict(extensions.get("agentbc.session") or {})
        cleanup = dict(session.get("cleanup") or {})
        cleanup.update({"state": "unsupported", "capability": "unsupported"})
        session.update({"session_state": "terminal", "cleanup": cleanup})
        extensions["agentbc.session"] = session
        task.extensions = extensions
        self.service.store.write_task(task.id, task.to_dict())
        report = Path(task.workspace["report_file"])
        report.write_bytes(b"old failure report\n")
        return task

    def test_managed_retry_resets_all_steps_and_freezes_policy(self) -> None:
        task = self._failed_task()
        artifact_root = Path(task.workspace["artifact_root"])
        (artifact_root / "old-output.bin").write_bytes(b"old artifact")
        requirements = Path(task.workspace["task_file"]).read_bytes()
        workspace = copy.deepcopy(task.workspace)
        policy = {
            key: copy.deepcopy(task.extensions.get(key))
            for key in (
                "agentbc.permission",
                "agentbc.resource",
                "agentbc.input_policy",
            )
        }
        session = copy.deepcopy(task.extensions.get("agentbc.session") or {})
        frozen_session = {
            key: session.get(key)
            for key in ("executor", "retain", "project_mode", "project_path")
        }
        original_errors = copy.deepcopy(task.errors)
        original_id = task.id

        retried = self.service.retry_failed_task(task.id)

        self.assertEqual(retried.id, original_id)
        self.assertEqual(retried.title, "Retry target")
        self.assertEqual(retried.assignee, "codex")
        self.assertEqual(retried.status, "pending")
        self.assertEqual([step["status"] for step in retried.steps], ["pending"] * 3)
        self.assertEqual([step["description"] for step in retried.steps], [
            "first requirement",
            "second requirement",
            "third requirement",
        ])
        self.assertEqual(retried.errors, original_errors)
        self.assertEqual(retried.workspace, workspace)
        self.assertEqual(Path(task.workspace["task_file"]).read_bytes(), requirements)
        self.assertFalse(Path(task.workspace["report_file"]).exists())
        self.assertTrue(artifact_root.is_dir())
        self.assertEqual(list(artifact_root.iterdir()), [])

        for key, value in policy.items():
            self.assertEqual(retried.extensions.get(key), value)
        new_session = retried.extensions["agentbc.session"]
        self.assertEqual(
            {key: new_session.get(key) for key in frozen_session},
            frozen_session,
        )
        self.assertEqual(new_session["session_state"], "pending")
        self.assertEqual(new_session["run_ids"], [])
        self.assertNotEqual(new_session["created_at"], session["created_at"])
        self.assertNotIn("agentbc.input", retried.extensions)
        self.assertEqual(
            retried.extensions["agentbc.input_history"][-1]["status"],
            "revoked",
        )
        for key in (
            "agentbc.permission_runtime",
            "agentbc.completion_intent",
            "agentbc.final_callback",
            "agentbc.terminal_delivery",
        ):
            self.assertNotIn(key, retried.extensions)
        self.assertEqual(
            retried.extensions["agentbc.permission_grant"]["state"]["status"],
            "revoked",
        )
        execution = retried.extensions["agentbc.execution"]
        self.assertEqual(execution["internal_status"], "pending")
        self.assertNotIn("worker_run_id", execution)
        self.assertNotIn("dispatch_status", execution)
        revival = retried.extensions["agentbc.revival"]
        self.assertEqual(revival["target_attempt_id"], "attempt-1")
        self.assertEqual(revival["cleanup_scope"], "managed_default_artifacts")
        self.assertEqual(revival["resumed_step_ids"], [1, 2, 3])
        from agent_bridge_connect import retry_flow
        from agent_bridge_connect.revival import validate_revival_reservation

        self.assertEqual(
            retry_flow.build_revival_reservation.__module__,
            "agent_bridge_connect.revival",
        )
        self.assertEqual(validate_revival_reservation(revival), [])

    def test_custom_retry_is_report_only_and_byte_identical(self) -> None:
        task = self._failed_task(customer_dir=True)
        customer = Path(task.workspace["project_root"])
        (customer / "keep.bin").write_bytes(b"\x00\xff\n")
        (customer / "nested").mkdir()
        (customer / "nested" / "keep.txt").write_bytes(b"keep\x00")
        before = {
            path.relative_to(customer): path.read_bytes()
            for path in customer.rglob("*")
            if path.is_file()
        }

        retried = self.service.retry_failed_task(task.id)

        after = {
            path.relative_to(customer): path.read_bytes()
            for path in customer.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(
            retried.extensions["agentbc.revival"]["cleanup_scope"],
            "managed_default_artifacts",
        )
        self.assertFalse(Path(task.workspace["report_file"]).exists())
        self.assertTrue(Path(task.workspace["task_file"]).exists())

    def test_status_and_report_project_revival_without_reservation_secret(self) -> None:
        task = self._failed_task()
        task.extensions.pop("agentbc.permission_runtime", None)
        self.service.store.write_task(task.id, task.to_dict())

        status = task_to_status(self.service.get_task(task.id), self.service)
        from agent_bridge_connect.reports import generate_report

        report = generate_report(task.id, self.board)

        self.assertEqual(status["revival"]["allowed_next_actions"], ["retry", "handoff"])
        self.assertNotIn("revival_id", status["revival"])
        self.assertEqual(report["revival"]["version"], 1)
        self.assertIs(report["revival"]["eligible"], True)

    def test_cleanup_failure_restores_report_artifact_and_failed_state(self) -> None:
        task = self._failed_task()
        artifact = Path(task.workspace["artifact_root"])
        shutil.rmtree(artifact)
        artifact.write_bytes(b"customer-owned-invalid-root")
        report = Path(task.workspace["report_file"])
        before = self.service.store.read_task(task.id)

        with self.assertRaises(ABCError) as raised:
            self.service.retry_failed_task(task.id)

        self.assertEqual(raised.exception.code, "revival_path_plan_invalid")
        self.assertEqual(self.service.store.read_task(task.id), before)
        self.assertEqual(report.read_bytes(), b"old failure report\n")
        self.assertEqual(artifact.read_bytes(), b"customer-owned-invalid-root")

        artifact.unlink()
        artifact.mkdir()
        self.assertEqual(self.service.retry_failed_task(task.id).status, "pending")

    def test_preflight_rejects_input_cleanup_and_active_lease(self) -> None:
        waiting = self._failed_task()
        waiting.extensions["agentbc.input"]["status"] = "waiting"
        waiting.extensions["agentbc.session"]["cleanup"] = {
            "state": "pending"
        }
        self.service.store.write_task(waiting.id, waiting.to_dict())
        errors = self.service.retry_preflight(waiting.id)["errors"]
        codes = {item["code"] for item in errors}
        self.assertIn("revival_input_unresolved", codes)
        self.assertIn("revival_session_cleanup_unstable", codes)

        leased = self._failed_task()
        token = self.service.store.acquire_lease(leased.id, "runner", ttl_s=60)
        self.assertIsNotNone(token)
        errors = self.service.retry_preflight(leased.id)["errors"]
        self.assertIn("revival_run_lease_open", {item["code"] for item in errors})

    def test_stale_worker_projection_is_restart_safe(self) -> None:
        task = self._failed_task()
        preflight = self.service.retry_preflight(task.id)
        self.assertTrue(preflight["ok"], preflight)
        self.service.retry_failed_task(task.id)
        self.assertEqual(self.service.get_task(task.id).status, "pending")

    def test_retry_requires_the_current_chain_head(self) -> None:
        source = self.service.create_task(
            "Chain source", "codex", [{"id": 1, "description": "source"}]
        )
        source.status = "completed"
        source.steps[0]["status"] = "done"
        self.service.store.write_task(source.id, source.to_dict())
        Path(source.workspace["report_file"]).write_text("source report\n", encoding="utf-8")
        head = self.service.handoff_task(source.id, "codex")
        source = self.service.get_task(source.id)
        source.status = "failed"
        self.service.store.write_task(source.id, source.to_dict())
        Path(source.workspace["report_file"]).write_text("source failure\n", encoding="utf-8")

        errors = self.service.retry_preflight(source.id)["errors"]

        self.assertIn("revival_source_not_chain_head", {item["code"] for item in errors})
        self.assertEqual(self.service.get_task(head.id).status, "pending")

    def test_duplicate_requests_create_one_attempt(self) -> None:
        task = self._failed_task(customer_dir=True)

        def retry_once():
            service = TaskService(self.board, config=self.config)
            try:
                return ("ok", service.retry_failed_task(task.id).id)
            except ABCError as exc:
                return ("error", exc.code)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: retry_once(), range(4)))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(
            self.service.get_task(task.id).extensions["agentbc.revival"]["target_attempt_id"],
            "attempt-1",
        )
        retry_events = [
            event
            for event in self.service.store.read_events(task.id)
            if event.get("event_type") == "task.retry"
        ]
        self.assertEqual(len(retry_events), 1)

    def test_failed_retry_can_be_retried_again_with_next_attempt(self) -> None:
        task = self._failed_task()
        self.service.retry_failed_task(task.id)
        failed_again = self.service.get_task(task.id)
        failed_again.status = "failed"
        failed_again.steps[1]["status"] = "failed"
        failed_again.errors.append({"code": "executor_failed_again", "message": "again"})
        session = dict(failed_again.extensions.get("agentbc.session") or {})
        cleanup = dict(session.get("cleanup") or {})
        cleanup.update({"state": "unsupported", "capability": "unsupported"})
        session.update({"session_state": "terminal", "cleanup": cleanup})
        failed_again.extensions["agentbc.session"] = session
        self.service.store.write_task(task.id, failed_again.to_dict())
        Path(task.workspace["report_file"]).write_text("second failure\n", encoding="utf-8")

        retried_again = self.service.retry_failed_task(task.id)

        self.assertEqual(retried_again.id, task.id)
        self.assertEqual(retried_again.extensions["agentbc.revival"]["target_attempt_id"], "attempt-2")
        self.assertEqual(retried_again.extensions["agentbc.revival"]["source_attempt_id"], "attempt-1")

    def test_retry_rejects_non_failed_and_step_flag_is_separate(self) -> None:
        task = self._failed_task()
        from agent_bridge_connect.cli import main

        output = StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "task",
                    "retry",
                    task.id,
                    "--step",
                    "2",
                    "--root",
                    str(self.board),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("retry_step_not_supported:", output.getvalue())
        self.assertEqual(self.service.get_task(task.id).status, "failed")

        self.service.retry_failed_task(task.id)
        with self.assertRaises(ABCError) as raised:
            self.service.retry_failed_task(task.id)
        self.assertEqual(raised.exception.code, "revival_source_status_invalid")

        from agent_bridge_connect.cli import build_parser

        args = build_parser().parse_args(
            ["task", "retry-step", task.id, "--step", "2", "--root", str(self.board)]
        )
        self.assertEqual(args.task_command, "retry-step")
        self.assertEqual(args.step, 2)

    def test_dispatch_option_submits_exactly_one_fresh_attempt(self) -> None:
        task = self._failed_task(customer_dir=True)
        from agent_bridge_connect.cli import main

        dispatch_result = {
            "task_id": task.id,
            "assignee": task.assignee,
            "workspace": task.workspace,
            "run_id": "new-worker",
            "dispatch_status": "accepted",
            "monitor_status": "not_requested",
        }
        with mock.patch(
            "agent_bridge_connect.runner.RunnerClient.dispatch_task",
            return_value=dispatch_result,
        ) as dispatch:
            code = main(
                [
                    "task",
                    "retry",
                    task.id,
                    "--dispatch",
                    "--root",
                    str(self.board),
                ]
            )
        self.assertEqual(code, 0)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0], task.id)
        self.assertEqual(self.service.get_task(task.id).status, "pending")


if __name__ == "__main__":
    unittest.main()
