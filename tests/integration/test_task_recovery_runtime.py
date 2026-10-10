from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from tests.contract_helpers import finalize_completed


class TaskRecoveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.board = Path(tempfile.mkdtemp())
        self.config = {"workspace_root": str(self.board)}
        from agent_bridge_connect.service import TaskService

        self.service = TaskService(self.board, config=self.config)
        self.task = self.service.create_task("Integration task", "shell", [{"id": 1, "description": "printf hello"}], customer_dir=False)

    def tearDown(self):
        shutil.rmtree(self.board)

    def test_terminal_cleanup_never_removes_empty_customer_project(self):
        customer = self.board.parent / "empty-customer-project"
        customer.mkdir(exist_ok=True)
        task = self.service.create_task("Empty customer project", "shell", [{"id": 1, "description": "inspect only"}], customer_path=customer)
        self.service.claim_task(task.id, "shell")
        finalize_completed(self.service, task.id)
        self.assertTrue(customer.is_dir())

    def test_create_task_rejects_task_id_as_assignee(self):
        from agent_bridge_connect.protocol import ABCError

        before = {task.id for task in self.service.list_tasks()}
        with self.assertRaises(ABCError) as raised:
            self.service.create_task("Invalid assignee", "T-011", [{"id": 1, "description": "should not create"}], customer_dir=False)
        self.assertEqual(raised.exception.code, "invalid_executor")
        self.assertIn("not a task id", raised.exception.message)
        self.assertEqual({task.id for task in self.service.list_tasks()}, before)

    def test_handoff_rejects_task_id_target_before_creating_followup(self):
        from agent_bridge_connect.protocol import ABCError

        source = self.service.create_task("Completed source", "hermes", [{"id": 1, "description": "produce evidence"}], customer_dir=False)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        before = {task.id for task in self.service.list_tasks()}
        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "T-011", "wrongly linking two tasks")
        self.assertEqual(raised.exception.code, "invalid_executor")
        self.assertEqual(raised.exception.details["value"], "T-011")
        self.assertEqual({task.id for task in self.service.list_tasks()}, before)

    def test_reassign_rejects_task_id_as_executor(self):
        from agent_bridge_connect.protocol import ABCError

        task = self.service.create_task("Recoverable task", "hermes", [{"id": 1, "description": "recover"}], customer_dir=False)
        self.service.mark_task_needs_recovery(task.id, "test", "needs reassignment")
        with self.assertRaises(ABCError) as raised:
            self.service.reassign_task(task.id, "T-011")
        self.assertEqual(raised.exception.code, "invalid_executor")
        self.assertEqual(self.service.get_task(task.id).assignee, "hermes")

    def test_cli_handoff_does_not_rewrite_source_internal_report(self):
        from agent_bridge_connect.cli import command_task_intervention

        source = self.service.create_task("Completed source", "hermes", [{"id": 1, "description": "produce evidence"}], customer_dir=False)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        from agent_bridge_connect.reports import write_report_files

        write_report_files(source.id, self.board)
        source_report = Path(source.workspace["report_file"])
        source_report.chmod(256)
        args = mock.Mock(root=self.board, task_command="handoff", id=source.id, to="codex", message="continue", dispatch=False, config=None, interval=2, monitor=True, branch=False)
        output = io.StringIO()

        def dispatch_handoff(**kwargs):
            followup = self.service.handoff_task(
                kwargs["source_task_id"],
                kwargs["target_assignee"],
                kwargs["message"],
                branch=kwargs["branch"],
                session_id=kwargs["session_id"],
                source_platform=kwargs["source_platform"],
                images=kwargs["images"],
                permission_mode=kwargs["permission_mode"],
            )
            return {"task_id": followup.id, "assignee": followup.assignee, "workspace": followup.workspace, "run_id": "test-run", "dispatch_status": "accepted", "monitor_status": "opened"}

        try:
            with (
                mock.patch("builtins.input", return_value="y"),
                mock.patch("agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch", side_effect=dispatch_handoff),
                contextlib.redirect_stdout(output),
            ):
                code = command_task_intervention(args)
        finally:
            source_report.chmod(384)
        self.assertEqual(code, 0)
        self.assertIn(f"handoff_created: {source.id} ->", output.getvalue())

    def test_handoff_references_source_record_paths_and_writes_new_v2_record_files(self):
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        source = self.service.create_task("Source record path", "hermes", [{"id": 1, "description": "produce evidence"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        source = self.service.get_task(source.id)
        source_task_record = Path(source.workspace["output_dir"]) / "TASK.md"
        source_report_record = Path(source.workspace["output_dir"]) / "REPORT.md"
        source_task_record.write_text("# Source task\n", encoding="utf-8")
        source_report_record.write_text("# Source report\n", encoding="utf-8")
        raw = self.service.store.read_task(source.id)
        raw["workspace"]["task_file"] = str(source_task_record)
        raw["workspace"]["report_file"] = str(source_report_record)
        self.service.store.write_task(source.id, raw)
        followup = self.service.handoff_task(source.id, "codex", "继续修改")
        self.assertIn(str(source_task_record), followup.steps[0]["description"])
        self.assertIn(str(source_report_record), followup.steps[0]["description"])
        self.assertEqual(followup.workspace["chain_task_id"], "002")
        self.assertEqual(Path(followup.workspace["task_file"]).name, f"{followup.id}-task.md")
        self.assertEqual(Path(followup.workspace["report_file"]).name, f"{followup.id}-report.md")
        self.assertNotEqual(followup.workspace["task_file"], followup.workspace["report_file"])

    def test_task_logs_falls_back_to_worker_run_when_executor_is_not_runner_managed(self):
        from agent_bridge_connect.cli import command_task_logs
        from agent_bridge_connect.run_lease import create_lease, save_lease
        from agent_bridge_connect.runner import RunnerError
        from agent_bridge_connect.task_store import TaskStore

        store = TaskStore(self.board)
        current = store.read_task(self.task.id)
        current.setdefault("extensions", {})["agentbc.execution"] = {"worker_run_id": "runner-worker-123"}
        store.write_task(self.task.id, current)
        lease = create_lease(self.task.id, "codex", os.getpid(), str(self.board))
        lease.run_id = f"codex-{self.task.id}-12345678"
        save_lease(lease, self.board)

        def fake_status(run_id: str):
            if run_id.startswith("codex-"):
                raise RunnerError(f"unknown runner run: {run_id}")
            return {"status": "completed", "stdout": "worker heartbeat\n", "stderr": ""}

        args = mock.Mock(root=self.board, id=self.task.id, follow=False, interval=0.01)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with mock.patch("agent_bridge_connect.runner.RunnerClient.status", side_effect=fake_status):
                code = command_task_logs(args)
        self.assertEqual(code, 0)
        self.assertIn("worker heartbeat", output.getvalue())

    def test_detached_dispatch_failure_does_not_leave_task_pending(self):
        from agent_bridge_connect.cli import command_worker_run
        from agent_bridge_connect.runner import RunnerError

        args = mock.Mock(root=self.board, executor="shell", task_id=self.task.id, detach=True, monitor=True, config=None, interval=0.1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with mock.patch("agent_bridge_connect.runner.RunnerClient.dispatch_worker", side_effect=RunnerError("task workspace is outside allowed roots")):
                code = command_worker_run(args)
        task = self.service.get_task(self.task.id)
        self.assertEqual(code, 1)
        self.assertEqual(task.status, "needs_recovery")
        self.assertEqual(task.errors[-1]["code"], "runner_dispatch_failed")
        self.assertIn("async dispatch failed", output.getvalue())

    def test_needs_recovery_task_can_be_requeued_with_closed_lease(self):
        self.service.mark_task_needs_recovery(self.task.id, "test_error", "test")
        task = self.service.requeue_task(self.task.id)
        self.assertEqual(task.status, "pending")
        events = self.service.store.read_events(self.task.id)
        self.assertEqual(events[-1]["event_type"], "task.requeued")

    def test_requeue_clears_run_without_an_official_session_receipt(self):
        from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY

        task = self.service.create_task("retry pre-session failure", "codex", [{"id": 1, "description": "run"}], customer_dir=False, permission_mode="full")
        pending = dict(task.extensions[SESSION_EXTENSION_KEY])
        pending["session_state"] = "pending"
        pending["session_id"] = ""
        pending["run_ids"] = ["codex-pre-session-failure"]
        task.extensions = dict(task.extensions)
        task.extensions[SESSION_EXTENSION_KEY] = pending
        self.service.store.write_task(task.id, task.to_dict())
        self.service.mark_task_needs_recovery(task.id, "executor_start_failed", "pre-session failure")
        requeued = self.service.requeue_task(task.id)
        session = requeued.extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["session_state"], "pending")
        self.assertEqual(session["session_id"], "")
        self.assertEqual(session["run_ids"], [])
        self.assertEqual(session["resume_count"], 0)
        events = self.service.store.read_events(task.id)
        self.assertEqual(events[-1]["event_type"], "executor.unbound_session_runs_cleared")
        self.assertEqual(events[-1]["cleared_run_count"], 1)

    def test_needs_recovery_report_requires_recovery(self):
        from agent_bridge_connect.reports import generate_report

        self.service.mark_task_needs_recovery(self.task.id, "test_error", "test")
        report = generate_report(self.task.id, self.board)
        self.assertIn("Recovery required", report["recovery_recommendation"])
        self.assertIn(f"task dispatch {self.task.id}", report["recovery_recommendation"])

    def test_handoff_preserves_workspace_for_codex_hermes_loop(self):
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        source = self.service.create_task("Hermes source", "hermes", [{"id": 1, "description": "produce evidence"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        from agent_bridge_connect.reports import write_report_files

        write_report_files(source.id, self.board)
        codex_task = self.service.handoff_task(source.id, "codex", "验收源任务")
        from agent_bridge_connect.reports import generate_report_md

        codex_report = generate_report_md(codex_task.id, self.board)
        self.assertIn(f"Previous report: `{source.workspace['report_file']}`", codex_report)
        self.service.claim_task(codex_task.id, "codex")
        self.service.execute_step(codex_task.id, 1, {"status": "done"})
        finalize_completed(self.service, codex_task.id)
        codex_task = self.service.get_task(codex_task.id)
        hermes_task = self.service.handoff_task(codex_task.id, "hermes", "根据验收意见继续")
        self.assertEqual(codex_task.assignee, "codex")
        self.assertEqual(hermes_task.assignee, "hermes")
        self.assertEqual(codex_task.workspace["root"], str(workspace.resolve()))
        self.assertEqual(hermes_task.workspace["root"], str(workspace.resolve()))
        self.assertEqual(codex_task.workspace["artifacts_dir"], source.workspace["artifacts_dir"])
        self.assertEqual(hermes_task.workspace["artifacts_dir"], source.workspace["artifacts_dir"])
        self.assertIn(source.id, codex_task.steps[0]["description"])
        self.assertIn("report", codex_task.steps[0]["description"].lower())

    def test_handoff_rejects_stale_source_and_suggests_chain_head(self):
        from agent_bridge_connect.protocol import ABCError

        source = self.service.create_task("Root task", "hermes", [{"id": 1, "description": "root work"}], customer_dir=False)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        first = self.service.handoff_task(source.id, "codex", "first continuation")
        self.service.claim_task(first.id, "codex")
        self.service.execute_step(first.id, 1, {"status": "done"})
        finalize_completed(self.service, first.id)
        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(source.id, "hermes", "stale continuation")
        self.assertEqual(raised.exception.code, "stale_handoff_source")
        self.assertEqual(raised.exception.details["current_head_task_id"], first.id)
        self.assertIn(first.id, raised.exception.details["suggested_command"])

    def test_handoff_branch_allows_explicit_sibling(self):
        source = self.service.create_task("Root task", "hermes", [{"id": 1, "description": "root work"}], customer_dir=False)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        first = self.service.handoff_task(source.id, "codex", "first continuation")
        self.service.claim_task(first.id, "codex")
        self.service.execute_step(first.id, 1, {"status": "done"})
        finalize_completed(self.service, first.id)
        sibling = self.service.handoff_task(source.id, "hermes", "intentional branch", branch=True)
        chain = self.service.resolve_chain(source.id)
        self.assertEqual(sibling.extensions["agentbc.lineage"]["branch_mode"], "explicit_branch")
        self.assertCountEqual(chain.head_task_ids, [first.id, sibling.id])

    def test_handoff_rejects_ambiguous_chain_without_branch(self):
        from agent_bridge_connect.protocol import ABCError

        source = self.service.create_task("Root task", "hermes", [{"id": 1, "description": "root work"}], customer_dir=False)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        first = self.service.handoff_task(source.id, "codex", "first continuation")
        self.service.claim_task(first.id, "codex")
        self.service.execute_step(first.id, 1, {"status": "done"})
        finalize_completed(self.service, first.id)
        sibling = self.service.handoff_task(source.id, "hermes", "intentional branch", branch=True)
        self.service.claim_task(sibling.id, "hermes")
        self.service.execute_step(sibling.id, 1, {"status": "done"})
        finalize_completed(self.service, sibling.id)
        with self.assertRaises(ABCError) as raised:
            self.service.handoff_task(first.id, "hermes", "ambiguous continuation")
        self.assertEqual(raised.exception.code, "ambiguous_chain_head")
        self.assertCountEqual(raised.exception.details["head_task_ids"], [first.id, sibling.id])

    def test_task_status_json_includes_chain_head_fields(self):
        source = self.service.create_task("Root task", "hermes", [{"id": 1, "description": "root work"}], customer_dir=False)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        followup = self.service.handoff_task(source.id, "codex", "continue")
        status = self.service.resolve_task(followup.id)["current_task"]
        self.assertEqual(status["chain_root_task_id"], source.id)
        self.assertEqual(status["parent_task_id"], source.id)
        self.assertEqual(status["head_task_ids"], [followup.id])
        self.assertTrue(status["is_chain_head"])
        self.assertEqual(status["chain_dir"], source.workspace["chain_dir"])
        self.assertEqual(status["chain_output_dir"], source.workspace["output_dir"])

    def test_shell_executor_closes_lease_after_steps(self):
        from agent_bridge_connect.executors.shell import ShellExecutor

        executor = ShellExecutor()
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "command": "printf hello"}], "workspace": {"root": str(self.board)}})
        lease_data = json.loads((self.board / self.task.id.split("-")[0] / self.task.id.split("-")[1] / "run_lease.json").read_text())
        self.assertTrue(result.ok)
        self.assertEqual(executor.poll(result.run_id).status, "completed")
        self.assertEqual(lease_data["state"], "closed")

    def test_executor_lease_uses_task_board_when_workspace_is_separate(self):
        from agent_bridge_connect.executors.shell import ShellExecutor

        workspace = self.board / "lease-workspace"
        workspace.mkdir(exist_ok=True)
        executor = ShellExecutor()
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "command": "printf hello"}], "workspace": {"root": str(workspace)}, "task_board": {"root": str(self.board)}})
        self.assertTrue(result.ok)
        self.assertTrue((self.board / self.task.id.split("-")[0] / self.task.id.split("-")[1] / "run_lease.json").exists())
        self.assertFalse((workspace / "tasks" / self.task.id.split("-")[0] / self.task.id.split("-")[1] / "run_lease.json").exists())

    def test_worker_uses_task_workspace_not_board_root(self):
        from agent_bridge_connect.cli import command_worker_run
        from agent_bridge_connect.executors.mock import MockExecutor

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        _task = self.service.create_task("Worker workspace", "mock", [{"id": 1, "description": "noop"}], customer_dir=True, customer_path=workspace)
        seen: dict[str, str] = {}
        original_start = MockExecutor.start

        def capture_start(self, task_packet):
            seen["root"] = task_packet["workspace"]["root"]
            return original_start(self, task_packet)

        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=MockExecutor()):
            with mock.patch.object(MockExecutor, "start", capture_start):
                code = command_worker_run(mock.Mock(root=self.board, executor="mock", once=True, interval=0.01, config=None))
        self.assertEqual(code, 0)
        self.assertEqual(seen["root"], str(workspace.resolve()))

    def test_worker_execution_error_marks_failed_and_writes_report(self):
        from agent_bridge_connect.adapters import ExecutorCapabilities, ExecutorLevel, PollResult, ProbeResult, StartResult
        from agent_bridge_connect.adapters import ExecutorPort
        from agent_bridge_connect.cli import command_worker_run

        class FailingExecutor(ExecutorPort):
            def probe(self):
                return ProbeResult(ok=True, message="ready")

            def capabilities(self):
                return ExecutorCapabilities(level=ExecutorLevel.L0)

            def start(self, task_packet):
                return StartResult(ok=True, run_id="failing-run", message="started")

            def poll(self, run_id):
                return PollResult(status="failed", result={"stderr": "boom", "failure": {"kind": "executor_exit_nonzero", "layer": "executor", "message": "boom", "retryable": True}})

        task = self.service.create_task("Failing worker", "failing", [{"id": 1, "description": "fail"}], customer_dir=True, customer_path=self.board.parent)
        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=FailingExecutor()):
            code = command_worker_run(mock.Mock(root=self.board, executor="failing", once=True, interval=0.01, config=None))
        self.assertEqual(code, 1)
        failed = self.service.get_task(task.id)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.errors[0]["code"], "executor_exit_nonzero")
        self.assertTrue(Path(failed.workspace["report_file"]).exists())

    def test_worker_normal_exit_without_agent_callback_fails(self):
        from agent_bridge_connect.adapters import ExecutorCapabilities, ExecutorLevel, PollResult, ProbeResult, StartResult
        from agent_bridge_connect.adapters import ExecutorPort
        from agent_bridge_connect.cli import command_worker_run

        class ExitedExecutor(ExecutorPort):
            def probe(self):
                return ProbeResult(ok=True, message="ready")

            def capabilities(self):
                return ExecutorCapabilities(level=ExecutorLevel.L0)

            def start(self, task_packet):
                return StartResult(ok=True, run_id="normal-exit-run", message="started")

            def poll(self, run_id):
                return PollResult(status="completed", progress={"returncode": 0}, result={"returncode": 0, "summary": "executor process ended", "agent_callback": None})

        task = self.service.create_task("Normal exit without callback", "exited", [{"id": 1, "description": "finish"}], customer_dir=True, customer_path=self.board.parent)
        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=ExitedExecutor()):
            code = command_worker_run(mock.Mock(root=self.board, executor="exited", once=True, interval=0.01, config=None))
        failed = self.service.get_task(task.id)
        self.assertEqual(code, 1)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.errors[-1]["code"], "completion_marker_missing")
        self.assertNotIn("agentbc.final_callback", failed.extensions)

    def test_worker_keeps_codex_native_approval_in_same_run(self):
        from agent_bridge_connect.adapters import ExecutorCapabilities, ExecutorLevel, PollResult, ProbeResult, StartResult
        from agent_bridge_connect.adapters import ExecutorPort
        from agent_bridge_connect.cli import command_worker_run

        class NativeApprovalExecutor(ExecutorPort):
            def __init__(self):
                self.task_id = ""
                self.approved = False
                self.run_id = "codex-native-worker-run"
                self.poll_run_ids: list[str] = []

            def probe(self):
                return ProbeResult(ok=True, message="ready")

            def capabilities(self):
                return ExecutorCapabilities(level=ExecutorLevel.L2)

            def start(self, task_packet):
                self.task_id = task_packet["task_id"]
                return StartResult(ok=True, run_id=self.run_id, message="started")

            def poll(self, run_id):
                self.poll_run_ids.append(run_id)
                receipt = {"version": 1, "executor": "codex", "session_id": "thread-native-worker-1", "resumed": False, "persistence": "persistent", "source": "jsonl_thread_started"}
                if not self.approved:
                    return PollResult(
                        status="input_required",
                        result={
                            "execution_session": receipt,
                            "approval_request": {
                                "type": "permission",
                                "scope": "single_action",
                                "request_id": "native-request-1",
                                "request_fingerprint": "fp-" + "a" * 40,
                                "session_id": "thread-native-worker-1",
                                "operation": "command",
                                "summary": "Command execution approval requested",
                            },
                        },
                    )
                return PollResult(
                    status="completed",
                    result={
                        "execution_session": receipt,
                        "returncode": 0,
                        "summary": "same run completed",
                        "agent_callback": {"version": 1, "task_id": self.task_id, "final_state": "completed", "summary": "same run completed", "step_results": [{"id": 1, "status": "done"}]},
                    },
                )

        task = self.service.create_task("Codex native worker approval", "codex", [{"id": 1, "description": "approve once"}], customer_dir=True, customer_path=self.board.parent, permission_mode="inherit")
        executor = NativeApprovalExecutor()
        notices: list[str] = []

        def respond_in_process(service, task_id, **_kwargs):
            current = service.get_task(task_id)
            waiting = current.extensions["agentbc.input"]
            service.respond_to_input(task_id, waiting["input_id"], response_type="approve")
            executor.approved = True
            notices.append(str(waiting["request_id"]))
            return {"dialog_action": "approve", "response": {"same_session": True}}

        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor), mock.patch("agent_bridge_connect.cli._notify_input_required", side_effect=respond_in_process):
            code = command_worker_run(mock.Mock(root=self.board, executor="codex", once=True, interval=0.01, config=None))
        completed = self.service.get_task(task.id)
        self.assertEqual(code, 0)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(notices, ["native-request-1"])
        self.assertEqual(set(executor.poll_run_ids), {executor.run_id})
        self.assertEqual(completed.extensions["agentbc.session"]["session_id"], "thread-native-worker-1")


class WebSnapshotReadOnlyTests(unittest.TestCase):
    def setUp(self):
        from agent_bridge_connect.web_snapshot import SnapshotProvider
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / "record"
        self.task_dir = self.board / "ABCD" / "001"
        self.task_dir.mkdir(parents=True)
        self.reports = self.root / "reports"
        self.report = self.reports / "2026-09-21" / "ABCD" / "ABCD-001-report.md"
        self.report.parent.mkdir(parents=True)
        self.report.write_text("中文\n  spaces\n<script>plain</script>\napi_key=privatevalue\n")
        self.record = self.task_dir / "task.json"
        self.raw = {"id": "ABCD-001", "title": "中文", "status": "failed", "assignee": "opencode", "steps": [],
                    "extensions": {}, "workspace": {"report_file": str(self.report)}}
        self.save()
        for name in ("lease.json", "input.json", "route.json"):
            (self.root / name).write_text('{"sentinel":"unchanged"}')
        self.provider = SnapshotProvider(self.board, report_root=self.reports)

    def save(self):
        self.record.write_text(json.dumps(self.raw))

    def test_reads_do_not_mutate_any_files_and_revision_is_stable(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        a = self.provider.snapshot("ABCD-001")
        b = self.provider.snapshot("ABCD-001")
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(a["snapshot_revision"], b["snapshot_revision"])
        self.assertEqual(a["task"]["status"], "failed")
        self.assertNotIn("privatevalue", a["text"]["report"]["text"])
        self.assertIn("<script>plain</script>", a["text"]["report"]["text"])
        self.assertEqual(a["text"]["execution"]["state"], "unavailable")
        self.report.write_text("new text")
        self.assertNotEqual(a["snapshot_revision"], self.provider.snapshot("ABCD-001")["snapshot_revision"])

    def test_changed_record_retries_once_then_fails_without_mixing_runs(self):
        from unittest.mock import patch
        from agent_bridge_connect.protocol import ABCError
        original = self.provider._report
        def churn(raw, code):
            result = original(raw, code)
            self.raw["title"] += "x"
            self.save()
            return result
        with patch.object(self.provider, "_report", side_effect=churn):
            with self.assertRaises(ABCError) as caught:
                self.provider.snapshot("ABCD-001")
        self.assertEqual(caught.exception.code, "snapshot_changed")
        self.assertTrue(caught.exception.details["retryable"])

    def test_report_change_during_read_is_retried(self):
        from unittest.mock import patch
        original = self.provider._report
        count = 0
        def change_once(raw, code):
            nonlocal count
            result = original(raw, code)
            count += 1
            if count == 1:
                self.report.write_text("replacement")
            return result
        with patch.object(self.provider, "_report", side_effect=change_once):
            result = self.provider.snapshot("ABCD-001")
        self.assertEqual(result["text"]["report"]["text"], "replacement")
        self.assertEqual(count, 4)

    def test_missing_root_is_not_created_and_bad_ids_are_rejected(self):
        from agent_bridge_connect.web_snapshot import SnapshotProvider
        from agent_bridge_connect.protocol import ABCError
        missing = self.root / "missing"
        with self.assertRaises(ABCError):
            SnapshotProvider(missing)
        self.assertFalse(missing.exists())
        for value in ("../task", "ABCD", "abcd-001", "/ABCD-001", "ABCD-001/../../"):
            with self.subTest(value=value), self.assertRaises(ABCError):
                self.provider.snapshot(value)

    def test_path_and_symlink_escape_never_expose_other_files(self):
        outside = self.root / "private.txt"
        outside.write_text("DO_NOT_EXPOSE")
        for value in (str(outside), str(self.report.parent / "OTHER-001-report.md"),
                      str(self.reports / ".." / "private.txt")):
            self.raw["workspace"]["report_file"] = value
            self.save()
            self.assertEqual(self.provider.snapshot("ABCD-001")["text"]["report"]["state"], "unavailable")
        self.raw["workspace"]["report_file"] = str(self.report)
        self.save()
        self.report.unlink()
        self.report.symlink_to(outside)
        self.assertEqual(self.provider.snapshot("ABCD-001")["text"]["report"]["state"], "unavailable")
        self.record.unlink()
        self.record.symlink_to(outside)
        from agent_bridge_connect.protocol import ABCError
        with self.assertRaises(ABCError):
            self.provider.snapshot("ABCD-001")

    def test_size_limits_empty_report_and_unknown_state(self):
        from agent_bridge_connect.web_snapshot import MAX_TEXT_BYTES
        from agent_bridge_connect.protocol import ABCError
        self.report.write_text("中" * MAX_TEXT_BYTES)
        text = self.provider.snapshot("ABCD-001")["text"]["report"]
        self.assertTrue(text["truncated"])
        self.assertLessEqual(len(text["text"].encode()), MAX_TEXT_BYTES)
        self.report.write_text("")
        self.assertEqual(self.provider.snapshot("ABCD-001")["text"]["report"]["text"], "")
        self.report.unlink()
        self.assertEqual(self.provider.snapshot("ABCD-001")["text"]["report"]["state"], "missing")
        self.raw["title"] = "x" * (300 * 1024)
        self.save()
        with self.assertRaises(ABCError) as caught:
            self.provider.snapshot("ABCD-001")
        self.assertEqual(caught.exception.code, "snapshot_response_too_large")

    def test_malformed_record_and_root_replacement_fail_closed(self):
        from agent_bridge_connect.protocol import ABCError
        for payload in ('[]', '{"id":"OTHER-001"}', '{"id":"ABCD-001","steps":[null]}', '{'):
            self.record.write_text(payload)
            with self.assertRaises(ABCError):
                self.provider.snapshot("ABCD-001")
        self.save()
        self.board.rename(self.root / "old-record")
        self.board.mkdir()
        with self.assertRaises(ABCError) as caught:
            self.provider.snapshot("ABCD-001")
        self.assertEqual(caught.exception.code, "snapshot_source_changed")

    def test_special_files_and_parent_symlinks_fail_closed(self):
        import os
        from agent_bridge_connect.protocol import ABCError
        self.report.unlink()
        os.mkfifo(self.report)
        self.assertEqual(self.provider.snapshot("ABCD-001")["text"]["report"]["state"], "unavailable")
        self.report.unlink()
        old = self.report.parent.with_name("old")
        self.report.parent.rename(old)
        self.report.parent.symlink_to(old, target_is_directory=True)
        (old / self.report.name).write_text("untrusted")
        self.assertEqual(self.provider.snapshot("ABCD-001")["text"]["report"]["state"], "unavailable")
        self.record.unlink()
        os.mkfifo(self.record)
        with self.assertRaises(ABCError):
            self.provider.snapshot("ABCD-001")

    def test_invalid_unicode_is_a_path_free_error(self):
        from agent_bridge_connect.protocol import ABCError
        self.raw["title"] = "\ud800"
        self.save()
        with self.assertRaises(ABCError) as caught:
            self.provider.snapshot("ABCD-001")
        self.assertEqual(caught.exception.code, "snapshot_record_invalid")
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_provider_never_calls_controller_or_runner(self):
        from unittest.mock import patch
        with patch("agent_bridge_connect.service.TaskService.__init__", side_effect=AssertionError("write-capable service")), \
             patch("agent_bridge_connect.runner_ipc.RunnerClient.__init__", side_effect=AssertionError("Runner IPC")):
            result = self.provider.snapshot("ABCD-001")
        self.assertEqual(result["consistency"], "consistent")


class WebGatewayTests(unittest.TestCase):
    """HTTP boundary against synthetic records; no executor dispatch."""

    save = WebSnapshotReadOnlyTests.save

    def setUp(self):
        WebSnapshotReadOnlyTests.setUp(self)
        from agent_bridge_connect.web_gateway import WebServer
        import threading
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        (self.assets / 'index.html').write_text('<!doctype html><title>test</title>')
        with mock.patch('socket.getfqdn', side_effect=AssertionError('startup must not resolve DNS')):
            self.server = WebServer(self.provider, self.assets)
        self.assertEqual(self.server.server_name, '127.0.0.1')
        self.assertEqual(self.server.server_port, self.server.socket.getsockname()[1])
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, auth=True, method='GET', headers=None):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        values = {'Authorization': 'Bearer '+self.server.token} if auth else {}
        values.update(headers or {})
        connection.request(method, path, headers=values)
        response = connection.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        connection.close()
        return result

    def test_groups_and_report_link(self):
        code, body, _ = self.request('/api/v1/tasks?group=active')
        self.assertEqual((code, json.loads(body)['items']), (200, []))
        code, body, _ = self.request('/api/v1/tasks?group=failed')
        self.assertEqual(json.loads(body)['items'][0]['task_id'], 'ABCD-001')
        self.assertEqual(self.request('/api/v1/tasks?group=bad')[0], 400)
        self.assertEqual(self.request('/api/v1/tasks/ABCD-001/report', auth=False)[0], 401)
        code, body, headers = self.request('/api/v1/tasks/ABCD-001/report')
        self.assertEqual(code, 200)
        self.assertIn('text/plain', headers['Content-Type'])
        self.assertIn('<script>plain</script>', body.decode())
        self.assertNotIn('privatevalue', body.decode())
        self.report.unlink()
        self.assertEqual(self.request('/api/v1/tasks/ABCD-001/report')[0], 404)

    def test_captured_timing_reuses_shared_view_without_live_reads(self):
        from unittest.mock import patch
        from agent_bridge_connect.timing_view import build_timing_view
        from agent_bridge_connect.run_lease import RunLease
        from dataclasses import asdict
        now = '2026-09-21T00:02:00Z'
        self.raw.update(created_at='2026-09-21T00:00:00Z', status='running')
        lease = RunLease('test-run', 'ABCD-001', 'codex', 0, 0, '',
                         '2026-09-21T00:01:00Z', now, 'none', 'active')
        (self.task_dir/'run_lease.json').write_text(json.dumps(asdict(lease)))
        self.raw['extensions'] = {'agentbc.execution': {'attempt_index': 1}}
        self.save()
        expected = build_timing_view(self.raw, self.board, now=now)
        with patch('agent_bridge_connect.timing_view.load_lease', side_effect=AssertionError('live read')), patch('agent_bridge_connect.timing_view.TaskStore', side_effect=AssertionError('live store')):
            actual = self.provider._timing(self.raw, ('ABCD','001','task.json'), now)
        self.assertEqual(actual['seconds'], expected['attempt_execution_duration_s'])
        self.assertEqual(actual['seconds'], 60)
        self.assertEqual(actual['label'], '本次执行时长')
        self.raw['extensions'] = {}
        actual = self.provider._timing(self.raw, ('ABCD','001','task.json'), now)
        self.assertEqual(actual['seconds'], 120)

    def test_http_auth_origin_and_write_rejection(self):
        self.assertEqual(self.request('/api/v1/tasks', auth=False)[0], 401)
        self.assertEqual(self.request('/api/v1/tasks', headers={'Authorization': 'Bearer wrong'})[0], 401)
        for headers in ({'Origin': 'https://outside.example'}, {'Host': 'outside.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/api/v1/tasks', headers=headers)[0], 403)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'):
            self.assertEqual(self.request('/api/v1/tasks', method=method)[0], 405)
        before = self.record.read_bytes()
        code, body, headers = self.request('/api/v1/tasks/ABCD-001/snapshot')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['task']['status'], 'failed')
        self.assertEqual(self.record.read_bytes(), before)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_static_traversal_and_errors(self):
        self.assertEqual(self.request('/', auth=False)[0], 200)
        for path in ('/../task.json', '/%2e%2e/task.json', '/?token=secret', '/assets/../../private', '/api/v1/unknown'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/v1/tasks?limit=0')[0], 400)
        self.assertEqual(self.request('/api/v1/tasks?limit=50&limit=50')[0], 400)
        self.assertEqual(self.request('/api/v1/tasks/ABCD-002/snapshot')[0], 404)
        self.record.write_text('bad-json')
        self.assertEqual(self.request('/api/v1/tasks/ABCD-001/snapshot')[0], 422)
        code, body, _ = self.request('/api/v1/tasks')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['items'][0]['error_code'], 'snapshot_record_invalid')
        self.raw.update(title='x' * 300000, assignee='x' * 129)
        self.save()
        code, body, _ = self.request('/api/v1/tasks')
        self.assertEqual(code, 200)
        row = json.loads(body)['items'][0]
        self.assertIsNone(row['title'])
        self.assertEqual(row['error_code'], 'snapshot_record_invalid')

    def test_catalogue_order_frozen_pages_and_expiry(self):
        from unittest.mock import patch
        from agent_bridge_connect.protocol import ABCError
        from agent_bridge_connect.web_catalogue import TaskCatalogue
        second = self.board/'ABCD'/'002'
        second.mkdir()
        record = dict(self.raw, id='ABCD-002', status='running', updated_at='2026-01-01T00:00:00Z')
        (second/'task.json').write_text(json.dumps(record))
        catalogue = TaskCatalogue(self.provider)
        first = catalogue.page(limit=1)
        self.assertEqual(first['items'][0]['task_id'], 'ABCD-002')
        self.raw['status']='running'
        self.raw['updated_at']='2027-01-01T00:00:00Z'
        self.save()
        last = catalogue.page(limit=1, cursor=first['next_cursor'])
        self.assertEqual(last['items'][0]['status'], 'failed')
        self.assertIsNone(last['next_cursor'])
        self.assertEqual(catalogue.page(limit=1)['items'][0]['task_id'], 'ABCD-001')
        with self.assertRaises(ABCError):
            catalogue.page(limit=1, cursor=first['next_cursor']+'x')
        with patch('agent_bridge_connect.web_catalogue.time.monotonic', return_value=float('inf')):
            with self.assertRaises(ABCError) as caught:
                catalogue.page(limit=1, cursor=first['next_cursor'])
        self.assertEqual(caught.exception.code, 'cursor_expired')

    def test_cli_does_not_bootstrap_relay(self):
        from unittest.mock import patch
        from agent_bridge_connect.cli import main
        with patch('sys.argv', ['agentbc','webui']), \
             patch('agent_bridge_connect.cli._bootstrap_desktop_relay', side_effect=AssertionError('relay')), \
             patch('agent_bridge_connect.web_gateway.run_webui', return_value=0) as run:
            self.assertEqual(main(), 0)
            run.assert_called_once()
