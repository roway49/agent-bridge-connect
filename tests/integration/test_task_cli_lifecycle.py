from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tests.contract_helpers import finalize_completed


class TaskCliLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.board = Path(tempfile.mkdtemp())
        self.config = {"workspace_root": str(self.board)}
        from agent_bridge_connect.service import TaskService

        self.service = TaskService(self.board, config=self.config)
        self.task = self.service.create_task("Integration task", "shell", [{"id": 1, "description": "printf hello"}], customer_dir=False)

    def tearDown(self):
        shutil.rmtree(self.board)

    def test_cli_exposes_reap_and_recover(self):
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        reap = parser.parse_args(["worker", "reap", "--root", str(self.board)])
        recover = parser.parse_args(["task", "recover", self.task.id, "--root", str(self.board)])
        self.assertEqual(reap.worker_command, "reap")
        self.assertEqual(recover.task_command, "recover")

    def test_cli_task_status_id_is_optional(self):
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        parsed = parser.parse_args(["task", "status", "--root", str(self.board)])
        self.assertIsNone(parsed.id)

    def test_cli_exposes_detached_worker_and_live_logs(self):
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        worker = parser.parse_args(["worker", "run", "--executor", "hermes", "--task-id", "T-001", "--detach"])
        logs = parser.parse_args(["task", "logs", "T-001", "--follow"])
        self.assertTrue(worker.detach)
        self.assertFalse(worker.monitor)
        self.assertEqual(worker.task_id, "T-001")
        self.assertTrue(logs.follow)
        visible = parser.parse_args(["worker", "run", "--executor", "hermes", "--task-id", "T-001", "--detach", "--monitor"])
        self.assertTrue(visible.monitor)
        create = parser.parse_args(["task", "create", "--title", "atomic", "--assignee", "hermes", "--steps", str(self.board / "steps.yaml"), "--customer-dir", "false", "--dispatch"])
        handoff = parser.parse_args(["task", "handoff", "T-001", "--to", "hermes", "--dispatch"])
        existing = parser.parse_args(["task", "dispatch", "T-001"])
        self.assertTrue(create.dispatch)
        self.assertFalse(create.monitor)
        self.assertTrue(handoff.dispatch)
        self.assertFalse(handoff.monitor)
        self.assertEqual(existing.task_command, "dispatch")
        self.assertFalse(existing.monitor)
        explicit = parser.parse_args(["task", "dispatch", "T-001", "--monitor"])
        self.assertTrue(explicit.monitor)

    def test_cli_detects_codex_thread_origin(self):
        from agent_bridge_connect.cli import _origin_context

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-auto"}, clear=False):
            (session_id, platform) = _origin_context(None, None)
        self.assertEqual(session_id, "thread-auto")
        self.assertEqual(platform, "codex")

    def test_worker_reap_reports_no_orphans(self):
        from agent_bridge_connect.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["worker", "reap", "--root", str(self.board)])
        self.assertEqual(code, 0)
        self.assertIn("no orphaned tasks", output.getvalue())

    def test_report_and_brief_include_run_lease_health(self):
        from agent_bridge_connect.reports import generate_report, generate_task_brief
        from agent_bridge_connect.run_lease import create_lease, save_lease

        lease = create_lease(self.task.id, "shell", os.getpid(), str(self.board))
        save_lease(lease, self.board)
        report = generate_report(self.task.id, self.board)
        brief = generate_task_brief(self.task.id, self.board)
        self.assertEqual(report["run_lease_state"], "active")
        self.assertIn("time_since_last_heartbeat_s", report)
        self.assertIn("recovery_recommendation", brief)

    def test_task_create_records_engineering_workspace_and_user_files(self):
        workspace = self.board / "project"
        workspace.mkdir(exist_ok=True)
        task = self.service.create_task("Workspace task", "shell", [{"id": 1, "description": "write output"}], customer_dir=True, customer_path=workspace)
        code = task.workspace["task_code"]
        self.assertEqual(task.workspace["root"], str(workspace.resolve()))
        self.assertEqual(task.workspace["project_root"], str(workspace.resolve()))
        self.assertEqual(task.workspace["artifact_root"], str(workspace.resolve()))
        expected_report_root = self.board.resolve() / "tasks" / "report" / task.workspace["task_date"] / code
        self.assertEqual(Path(task.workspace["report_root"]), expected_report_root)
        self.assertFalse((workspace / "agentbc_tasks").exists())
        self.assertRegex(task.id, "^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertTrue(Path(task.workspace["task_file"]).exists())
        self.assertEqual(task.workspace["chain_task_id"], "001")
        self.assertRegex(task.workspace["chain_token"], "^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}$")
        self.assertEqual(Path(task.workspace["task_file"]).name, f"{task.id}-task.md")
        self.assertEqual(Path(task.workspace["report_file"]).name, f"{task.id}-report.md")
        self.assertNotEqual(task.workspace["task_file"], task.workspace["report_file"])

    def test_service_uses_configured_workspace_root_when_workspace_is_omitted(self):
        configured_workspace = self.board.parent / "configured-workspace"
        service = type(self.service)(self.board, config={"workspace_root": str(configured_workspace)})
        task = service.create_task("Configured workspace task", "shell", [{"id": 1, "description": "write output"}], customer_dir=False)
        self.assertEqual(task.workspace["agentbc_root"], str(configured_workspace.resolve()))
        today = datetime.now().strftime("%Y-%m-%d")
        code = task.workspace["task_code"]
        self.assertEqual(Path(task.workspace["artifact_root"]), configured_workspace.resolve() / "tasks" / "artifacts" / today / code)
        self.assertEqual(Path(task.workspace["report_root"]), configured_workspace.resolve() / "tasks" / "report" / today / code)

    def test_service_uses_builtin_workspace_root_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "board"
            board.mkdir()
            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=False):
                from agent_bridge_connect.service import TaskService

                task = TaskService(board).create_task("Built-in workspace task", "shell", [{"id": 1, "description": "write output"}], customer_dir=False)
            expected = Path(tmp).resolve() / "Documents" / "AgentBC" / "workspace"
            self.assertEqual(task.workspace["agentbc_root"], str(expected))
            today = datetime.now().strftime("%Y-%m-%d")
            code = task.workspace["task_code"]
            self.assertEqual(Path(task.workspace["artifact_root"]), expected / "tasks" / "artifacts" / today / code)
            self.assertEqual(Path(task.workspace["report_root"]), expected / "tasks" / "report" / today / code)

    def test_new_task_codes_use_date_scoped_artifact_and_report_roots(self):
        workspace = self.board / "dated-workspace"
        service = type(self.service)(self.board, config={"workspace_root": str(workspace)})
        first = service.create_task("First dated chain", "shell", [{"id": 1, "description": "write output"}], customer_dir=False)
        second = service.create_task("Second dated chain", "shell", [{"id": 1, "description": "write output"}], customer_dir=False)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(first.workspace["task_date"], today)
        self.assertEqual(second.workspace["task_date"], today)
        self.assertEqual(Path(first.workspace["artifact_root"]).parent, workspace.resolve() / "tasks" / "artifacts" / today)
        self.assertEqual(Path(second.workspace["artifact_root"]).parent, workspace.resolve() / "tasks" / "artifacts" / today)
        self.assertEqual(first.workspace["chain_dir"], first.workspace["task_code"])
        self.assertEqual(second.workspace["chain_dir"], second.workspace["task_code"])
        self.assertEqual(first.workspace["chain_task_id"], "001")
        self.assertEqual(second.workspace["chain_task_id"], "001")
        self.assertRegex(first.id, "^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertRegex(second.id, "^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.workspace["chain_token"], second.workspace["chain_token"])

    def test_task_json_records_workspace_and_lineage_metadata(self):
        task = self.service.create_task("Chain file task", "shell", [{"id": 1, "description": "write output"}], customer_dir=False)
        payload = self.service.store.read_task(task.id)
        lineage = payload["extensions"]["agentbc.lineage"]
        self.assertEqual(lineage["chain_id"], task.workspace["chain_id"])
        self.assertEqual(lineage["chain_token"], task.workspace["chain_token"])
        self.assertEqual(lineage["task_code"], task.workspace["task_code"])
        self.assertEqual(lineage["chain_root_task_id"], task.id)
        self.assertEqual(payload["workspace"]["report_root"], task.workspace["report_root"])
        self.assertEqual(payload["workspace"]["artifacts_dir"], task.workspace["artifacts_dir"])

    def test_task_code_allocator_is_unique_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = list(pool.map(lambda index: self.service.create_task(f"Concurrent task {index}", "shell", [{"id": 1, "description": "write output"}], customer_dir=False), range(4)))
        codes = [task.workspace["task_code"] for task in tasks]
        self.assertEqual(len(codes), len(set(codes)))

    def test_task_requirements_and_report_record_codex_conversation(self):
        from agent_bridge_connect.reports import generate_report_md

        task = self.service.create_task("Traceable task", "hermes", [{"id": 1, "description": "trace"}], session_id="thread-123", source_platform="codex", customer_dir=False)
        requirements = Path(task.workspace["task_file"]).read_text(encoding="utf-8")
        report = generate_report_md(task.id, self.board)
        self.assertIn("Dispatcher platform: `codex`", requirements)
        self.assertIn("Dispatcher conversation ID: `thread-123`", requirements)
        self.assertIn("Status snapshot: `pending`", requirements)
        self.assertIn("non-authoritative", requirements)
        self.assertIn("Dispatcher platform: `codex`", report)
        self.assertIn("Dispatcher conversation ID: `thread-123`", report)
        self.assertNotIn("hermes session search", report)
        self.assertNotIn("Source platform:", report)
        self.assertNotIn("Conversation ID:", report)
        self.assertTrue(Path(task.workspace["artifacts_dir"]).is_dir())
        self.assertIn(f"Report: `{task.workspace['report_file']}`", requirements)

    def test_action_only_steps_are_canonicalized_across_task_contract(self):
        from agent_bridge_connect.executors.codex import _build_prompt as codex_prompt
        from agent_bridge_connect.executors.hermes import _build_prompt as hermes_prompt
        from agent_bridge_connect.reports import generate_report_md

        requirement = "实现带明确参数和验收证据的 shader"
        task = self.service.create_task("Action compatibility task", "codex", [{"id": 1, "action": requirement}], customer_dir=False)
        self.assertEqual(task.steps[0]["description"], requirement)
        task_brief = Path(task.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertIn(f"1. {requirement}", task_brief)
        self.assertIn(requirement, generate_report_md(task.id, self.board))
        packet = task.to_dict()
        self.assertIn(f"1. {requirement}", codex_prompt(packet))
        self.assertIn(f"1. {requirement}", hermes_prompt(packet))

    def test_task_creation_rejects_blank_step_text(self):
        from agent_bridge_connect.protocol import ABCError

        for step in ({"id": 1}, {"id": 1, "description": "  ", "action": ""}):
            with self.subTest(step=step), self.assertRaisesRegex(ABCError, "non-empty description or action"):
                self.service.create_task("Blank requirement task", "codex", [step], customer_dir=False)

    def test_steps_loader_accepts_block_scalars_without_pyyaml(self):
        from agent_bridge_connect.service import load_steps

        steps = self.board / "block-steps.yaml"
        steps.write_text("steps:\n  - description: |\n      Create a shader artifact.\n      Verify it with browser evidence.\n", encoding="utf-8")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            loaded = load_steps(steps)
        self.assertEqual(len(loaded), 1)
        self.assertIn("Create a shader artifact.", loaded[0]["description"])
        self.assertIn("Verify it with browser evidence.", loaded[0]["description"])

    def test_old_workspace_and_output_dir_arguments_are_rejected(self):
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        requested_artifacts_dir = workspace / "handoff-artifacts" / "artifacts"
        with self.assertRaisesRegex(Exception, "customer_dir/customer_path"):
            self.service.create_task("Flat output layout", "shell", [{"id": 1, "description": "write output"}], customer_dir=True, customer_path=workspace, output_dir=requested_artifacts_dir)

    def test_report_is_written_to_report_record_path(self):
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        task = self.service.create_task("Mirror report", "shell", [{"id": 1, "description": "write output"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(task.id, "shell")
        self.service.execute_step(task.id, 1, {"status": "done"})
        finalize_completed(self.service, task.id)
        from agent_bridge_connect.reports import write_report_files

        write_report_files(task.id, self.board)
        self.assertTrue(Path(task.workspace["report_file"]).exists())

    def test_task_status_without_id_returns_unique_active_task(self):
        from agent_bridge_connect.cli import command_task_status

        baseline = self.service.store.read_task(self.task.id)
        baseline["status"] = "completed"
        self.service.store.write_task(self.task.id, baseline)
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        active = self.service.create_task("Active task", "shell", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(active.id, "shell")
        args = mock.Mock(root=self.board, id=None, json=False, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(active.id, rendered)
        self.assertIn("Resolution: current active task", rendered)

    def test_task_status_without_id_returns_latest_updated_when_no_active_task(self):
        from agent_bridge_connect.cli import command_task_status

        baseline = self.service.store.read_task(self.task.id)
        baseline["status"] = "completed"
        self.service.store.write_task(self.task.id, baseline)
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        older = self.service.create_task("Older task", "shell", [{"id": 1, "description": "old"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(older.id, "shell")
        self.service.execute_step(older.id, 1, {"status": "done"})
        finalize_completed(self.service, older.id)
        latest = self.service.create_task("Latest task", "shell", [{"id": 1, "description": "new"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(latest.id, "shell")
        self.service.execute_step(latest.id, 1, {"status": "done"})
        finalize_completed(self.service, latest.id)
        args = mock.Mock(root=self.board, id=None, json=False, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(latest.id, rendered)
        self.assertIn("Resolution: latest reportable task", rendered)
        self.assertIn("No task is currently running", rendered)
        self.assertNotIn(older.id, rendered)

    def test_old_pending_task_does_not_override_latest_completed_task(self):
        from agent_bridge_connect.cli import command_task_status

        pending = self.task
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        completed = self.service.create_task("Latest completed", "shell", [{"id": 1, "description": "done"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(completed.id, "shell")
        self.service.execute_step(completed.id, 1, {"status": "done"})
        finalize_completed(self.service, completed.id)
        args = mock.Mock(root=self.board, id=None, json=True, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["resolved_task_id"], completed.id)
        self.assertEqual(payload["resolution_mode"], "latest_updated")
        self.assertNotEqual(payload["resolved_task_id"], pending.id)

    def test_only_pending_tasks_are_not_implicitly_resolved(self):
        from agent_bridge_connect.cli import command_task_status

        args = mock.Mock(root=self.board, id=None, json=True, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertIsNone(payload["resolved_task_id"])
        self.assertEqual(payload["resolution_mode"], "no_reportable_task")
        self.assertEqual(payload["pending_count"], 1)
        self.assertFalse(payload["has_active_task"])

    def test_task_status_without_id_returns_candidates_when_ambiguous(self):
        from agent_bridge_connect.cli import command_task_status

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        first = self.service.create_task("First active", "shell", [{"id": 1, "description": "run first"}], customer_dir=True, customer_path=workspace)
        second = self.service.create_task("Second active", "hermes", [{"id": 1, "description": "run second"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(first.id, "shell")
        self.service.claim_task(second.id, "hermes")
        args = mock.Mock(root=self.board, id=None, json=False, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Ambiguous current task", rendered)
        self.assertIn(first.id, rendered)
        self.assertIn(second.id, rendered)

    def test_task_status_without_id_returns_candidates_when_latest_updated_is_tied(self):
        from agent_bridge_connect.cli import command_task_status

        baseline = self.service.store.read_task(self.task.id)
        baseline["status"] = "completed"
        self.service.store.write_task(self.task.id, baseline)
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        first = self.service.create_task("First latest", "shell", [{"id": 1, "description": "first"}], customer_dir=True, customer_path=workspace)
        second = self.service.create_task("Second latest", "hermes", [{"id": 1, "description": "second"}], customer_dir=True, customer_path=workspace)
        for task in (first, second):
            self.service.claim_task(task.id, task.assignee)
            self.service.execute_step(task.id, 1, {"status": "done"})
            finalize_completed(self.service, task.id)
        tied_timestamp = "2099-06-20T12:00:00Z"
        for task in (first, second):
            payload = self.service.store.read_task(task.id)
            payload["updated_at"] = tied_timestamp
            self.service.store.write_task(task.id, payload)
        args = mock.Mock(root=self.board, id=None, json=False, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Ambiguous current task", rendered)
        self.assertIn(first.id, rendered)
        self.assertIn(second.id, rendered)

    def test_task_status_json_includes_resolution_fields(self):
        from agent_bridge_connect.cli import command_task_status

        baseline = self.service.store.read_task(self.task.id)
        baseline["status"] = "completed"
        self.service.store.write_task(self.task.id, baseline)
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        task = self.service.create_task("JSON active", "shell", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(task.id, "shell")
        args = mock.Mock(root=self.board, id=None, json=True, watch=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_status(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["resolved_task_id"], task.id)
        self.assertEqual(payload["resolution_mode"], "single_active")
        self.assertTrue(payload["has_active_task"])
        self.assertEqual(payload["current_task"]["id"], task.id)
        self.assertIsInstance(payload["active_candidates"], list)

    def test_task_create_without_workspace_uses_configured_workspace_root(self):
        from agent_bridge_connect.cli import command_task_create

        workspace = self.board.parent / "configured-cli-workspace"
        config_path = self.board / "config.toml"
        config_path.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
        steps = self.board / "steps.yaml"
        steps.write_text("steps:\n- description: write output\n", encoding="utf-8")
        args = mock.Mock(
            root=self.board,
            title="CLI workspace task",
            assignee="shell",
            steps=steps,
            session_id=None,
            source_platform=None,
            workspace=None,
            output_dir=None,
            customer_dir="false",
            customer_path=None,
            dispatch=False,
            config=None,
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False):
            with contextlib.redirect_stdout(output):
                code = command_task_create(args)
        created = next((item for item in self.service.list_tasks() if item.title == "CLI workspace task"))
        self.assertEqual(code, 0)
        self.assertEqual(created.workspace["agentbc_root"], str(workspace.resolve()))
        self.assertEqual(created.workspace["root"], created.workspace["artifact_root"])

    def test_task_create_derives_default_path_without_customer_dir(self):
        from agent_bridge_connect.cli import command_task_create

        workspace = self.board.parent / "derived-default-workspace"
        config_path = self.board / "derived-config.toml"
        config_path.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
        steps = self.board / "derived-steps.yaml"
        steps.write_text("steps:\n- description: write output\n", encoding="utf-8")
        args = mock.Mock(
            root=self.board,
            title="Derived default path task",
            assignee="shell",
            steps=steps,
            session_id=None,
            source_platform=None,
            workspace=None,
            output_dir=None,
            customer_path="default path",
            dispatch=False,
            config=None,
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False):
            with contextlib.redirect_stdout(output):
                code = command_task_create(args)
        created = next((item for item in self.service.list_tasks() if item.title == "Derived default path task"))
        self.assertEqual(code, 0)
        self.assertFalse(created.workspace["customer_dir"])
        self.assertEqual(created.workspace["agentbc_root"], str(workspace.resolve()))
        self.assertEqual(created.workspace["root"], created.workspace["artifact_root"])

    def test_existing_file_customer_path_uses_parent_without_managed_artifact_directory(self):
        customer = self.board.parent / "customer-documents"
        customer.mkdir(exist_ok=True)
        document = customer / "market-analysis.md"
        document.write_text("# Existing report\n", encoding="utf-8")
        created = self.service.create_task("Update existing document", "shell", [{"id": 1, "description": f"Update {document}"}], customer_path=document)
        self.assertTrue(created.workspace["customer_dir"])
        self.assertEqual(created.workspace["customer_path"], str(customer.resolve()))
        self.assertEqual(created.workspace["project_root"], str(customer.resolve()))
        self.assertEqual(created.workspace["artifact_root"], str(customer.resolve()))
        managed = Path(created.workspace["agentbc_root"]) / "tasks" / "artifacts" / created.workspace["task_date"] / created.workspace["task_code"]
        self.assertFalse(managed.exists())

    def test_shorthand_create_uses_configured_workspace_root_not_cwd(self):
        from agent_bridge_connect.cli import command_shorthand

        workspace = self.board.parent / "configured-shorthand-workspace"
        config_path = self.board / "config.toml"
        config_path.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
        cwd = self.board.parent / "unrelated-cwd"
        cwd.mkdir(exist_ok=True)
        args = mock.Mock(root=self.board, description="Write configured output")
        output = io.StringIO()
        with mock.patch("agent_bridge_connect.cli.command_worker_run", return_value=0):
            with mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False):
                previous = Path.cwd()
                os.chdir(cwd)
                try:
                    with contextlib.redirect_stdout(output):
                        code = command_shorthand(args)
                finally:
                    os.chdir(previous)
        created = next((item for item in self.service.list_tasks() if item.title == "Write configured output"))
        self.assertEqual(code, 0)
        self.assertEqual(created.workspace["agentbc_root"], str(workspace.resolve()))
        self.assertEqual(created.workspace["root"], created.workspace["artifact_root"])
        self.assertNotEqual(created.workspace["root"], str(cwd.resolve()))

    def test_atomic_create_does_not_create_task_when_runner_is_unavailable(self):
        from agent_bridge_connect.cli import command_task_create
        from agent_bridge_connect.runner import RunnerError

        steps = self.board / "atomic-steps.yaml"
        steps.write_text("steps:\n- description: run atomically\n", encoding="utf-8")
        args = mock.Mock(
            root=self.board,
            title="Atomic task",
            assignee="hermes",
            steps=steps,
            session_id=None,
            source_platform=None,
            workspace=None,
            output_dir=None,
            customer_dir="false",
            customer_path=None,
            dispatch=True,
            config=None,
            interval=2,
            monitor=True,
        )
        before = {task.id for task in self.service.list_tasks()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with mock.patch("agent_bridge_connect.runner.RunnerClient.create_and_dispatch", side_effect=RunnerError("runner unavailable")):
                code = command_task_create(args)
        after = {task.id for task in self.service.list_tasks()}
        self.assertEqual(code, 1)
        self.assertEqual(after, before)
        self.assertIn("atomic_dispatch_error", output.getvalue())

    def test_atomic_handoff_dispatches_the_created_task(self):
        from agent_bridge_connect.cli import command_task_intervention

        source = self.service.get_task(self.task.id)
        args = mock.Mock(root=self.board, task_command="handoff", id=source.id, to="hermes", message="continue atomically", dispatch=True, config=None, interval=2, monitor=True)
        dispatched = {
            "task_id": "T-099",
            "assignee": "hermes",
            "workspace": {"root": str(self.board), "task_file": "task_T-099.md", "artifacts_dir": "artifacts"},
            "run_id": "runner-worker-atomic",
            "dispatch_status": "accepted",
            "monitor_status": "opened",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with mock.patch("agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch", return_value=dispatched) as dispatch:
                code = command_task_intervention(args)
        self.assertEqual(code, 0)
        self.assertEqual(dispatch.call_args.kwargs["source_task_id"], source.id)
        self.assertEqual(dispatch.call_args.kwargs["target_assignee"], "hermes")
        self.assertIn("dispatched: T-099", output.getvalue())

    def test_existing_task_dispatch_uses_runner_gateway_only(self):
        from agent_bridge_connect.cli import command_task_dispatch

        args = mock.Mock(root=self.board, id=self.task.id, config=None, interval=2, monitor=True)
        result = {"run_id": "runner-worker-existing", "dispatch_status": "accepted", "monitor_status": "opened"}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with mock.patch("agent_bridge_connect.runner.RunnerClient.dispatch_task", return_value=result) as dispatch:
                code = command_task_dispatch(args)
        self.assertEqual(code, 0)
        self.assertEqual(dispatch.call_args.args[0], self.task.id)
        self.assertIn("dispatched: ", output.getvalue())

    def test_task_list_shows_active_tasks_first_with_lineage_fields(self):
        from agent_bridge_connect.cli import command_task_list

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        first = self.service.create_task("First active", "shell", [{"id": 1, "description": "first"}], customer_dir=True, customer_path=workspace)
        second = self.service.create_task("Completed task", "hermes", [{"id": 1, "description": "second"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(first.id, "shell")
        self.service.claim_task(second.id, "hermes")
        self.service.execute_step(second.id, 1, {"status": "done"})
        finalize_completed(self.service, second.id)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_list(args)
        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "task_id\titer\troute\ttimer\ttitle")
        self.assertRegex(lines[1], f"^{first.id}\\t001\\t.+ -> shell\\t[0-9]+s\\tFirst active$")
        self.assertNotIn(str(workspace.resolve()), lines[1])
        self.assertNotIn("-report.md", lines[1])

    def test_task_progress_refreshes_health_marker(self):
        from agent_bridge_connect.cli import command_task_progress

        task = self.service.create_task("Progress task", "shell", [{"id": 1, "description": "progress"}], customer_dir=False)
        self.service.claim_task(task.id, "shell")
        args = mock.Mock(root=self.board, id=task.id, state="running", summary="halfway", source="test")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_progress(args)
        self.assertEqual(code, 0)
        refreshed = self.service.get_task(task.id)
        self.assertIn("progress: ", output.getvalue())
        self.assertTrue(Path(refreshed.workspace["internal_task_dir"], f".{task.id}.run.temp").exists())
        self.assertTrue(Path(refreshed.workspace["internal_task_dir"], f"{task.id}-run.log").exists())

    def test_task_health_uses_yellow_orange_red_runtime_colors(self):
        from agent_bridge_connect.run_lease import create_lease, save_lease
        from agent_bridge_connect.task_health import task_health, task_run_temp_path, write_task_progress

        task = self.service.create_task("Health color task", "shell", [{"id": 1, "description": "health"}], customer_dir=False)
        self.service.claim_task(task.id, "shell")
        running = self.service.get_task(task.id)
        self.assertEqual(task_health(running)["color"], "green")
        old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat().replace("+00:00", "Z")
        path = task_run_temp_path(running)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = old
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(task_health(running)["color"], "yellow")
        older = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat().replace("+00:00", "Z")
        payload["updated_at"] = older
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(task_health(running)["color"], "orange")
        write_task_progress(running, message="alive again")
        lease = create_lease(running.id, "shell", 99999999, str(self.board))
        save_lease(lease, self.board)
        self.assertEqual(task_health(running)["color"], "green")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = old
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(task_health(running)["color"], "red")

    def test_pending_dispatched_task_health_is_green_starting(self):
        from agent_bridge_connect.task_health import task_health

        task = self.service.create_task("Accepted pending task", "shell", [{"id": 1, "description": "wait for worker start"}], customer_dir=False)
        self.service.update_execution_metadata(task.id, {"dispatch_status": "accepted", "worker_run_id": "runner-worker-test"})
        refreshed = self.service.get_task(task.id)
        health = task_health(refreshed)
        summary = self.service.task_summary(task.id)
        self.assertEqual(health["state"], "pending")
        self.assertEqual(health["color"], "gray")
        self.assertFalse(summary["is_active"])
        self.assertEqual(summary["health_color"], "gray")
        self.assertNotIn("elapsed", summary)
        self.assertNotIn("elapsed_s", summary)

    def test_cancel_cleans_progress_and_report_records_then_requeue_rebuilds(self):
        from agent_bridge_connect.task_health import task_run_temp_path

        task = self.service.create_task("Cancel cleanup task", "shell", [{"id": 1, "description": "cleanup"}], customer_dir=False)
        self.service.claim_task(task.id, "shell")
        running = self.service.get_task(task.id)
        artifact_root = Path(running.workspace["artifact_root"])
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "managed.txt").write_text("managed", encoding="utf-8")
        self.assertTrue(task_run_temp_path(running).exists())
        self.assertTrue(Path(running.workspace["task_file"]).exists())
        self.service.cancel_task(running.id)
        self.assertFalse(task_run_temp_path(running).exists())
        self.assertFalse(Path(running.workspace["task_file"]).exists())
        self.assertTrue(Path(running.workspace["report_file"]).exists())
        self.assertTrue((Path(running.workspace["internal_task_dir"]) / "task.json").exists())
        self.assertFalse(artifact_root.exists())
        requeued = self.service.requeue_task(running.id)
        self.assertEqual(requeued.status, "pending")
        self.assertEqual(requeued.id, running.id)
        self.assertTrue(Path(requeued.workspace["task_file"]).exists())

    def test_cancel_preserves_customer_project_artifacts(self):
        customer_path = self.board.parent / "customer-project"
        customer_path.mkdir(exist_ok=True)
        artifact = customer_path / "keep.txt"
        artifact.write_text("customer data", encoding="utf-8")
        task = self.service.create_task("Customer project task", "shell", [{"id": 1, "description": "edit customer project"}], customer_dir=True, customer_path=customer_path)
        self.service.cancel_task(task.id)
        self.assertTrue(artifact.exists())
        self.assertTrue((Path(task.workspace["internal_task_dir"]) / "task.json").exists())

    def test_cancel_handoff_preserves_previous_iteration_reports(self):
        source = self.service.create_task("Source iteration", "shell", [{"id": 1, "description": "finish source"}], customer_dir=False)
        self.service.claim_task(source.id, "shell")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        source = self.service.get_task(source.id)
        handoff = self.service.handoff_task(source.id, "codex", "continue")
        self.service.cancel_task(handoff.id)
        self.assertTrue(Path(source.workspace["task_file"]).exists())
        self.assertTrue(Path(source.workspace["report_file"]).exists())
        self.assertFalse(Path(handoff.workspace["task_file"]).exists())
        self.assertTrue(Path(handoff.workspace["report_file"]).exists())
        self.assertTrue(Path(source.workspace["report_root"]).exists())

    def test_task_list_current_excludes_pending_tasks(self):
        from agent_bridge_connect.cli import command_task_list

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        running = self.service.create_task("Running task", "shell", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(running.id, "shell")
        args = mock.Mock(root=self.board, status=None, assignee=None, current=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command_task_list(args)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(running.id, rendered)
        self.assertNotIn(self.task.id, rendered)

    def test_task_list_watch_keeps_completed_and_active_cohort_tasks(self):
        from agent_bridge_connect.cli import _task_list_summaries
        from agent_bridge_connect.task_health import register_dashboard_task

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        watched = self.service.create_task("Watched done", "shell", [{"id": 1, "description": "done"}], customer_dir=True, customer_path=workspace)
        active = self.service.create_task("Still running", "codex", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(watched.id, "shell")
        self.service.execute_step(watched.id, 1, {"status": "done"})
        finalize_completed(self.service, watched.id)
        self.service.claim_task(active.id, "codex")
        register_dashboard_task(self.board, watched.id, reset=True)
        register_dashboard_task(self.board, active.id)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False, all_iterations=False, watch_task_id=watched.id)
        summaries = _task_list_summaries(args)
        self.assertEqual([summary["task_id"] for summary in summaries], [watched.id, active.id])
        self.assertFalse(summaries[0]["is_active"])
        self.assertTrue(summaries[1]["is_active"])

    def test_task_list_watch_keeps_completed_cohort_until_window_closes(self):
        from agent_bridge_connect.cli import _summary_keeps_dashboard_alive, _task_list_summaries
        from agent_bridge_connect.task_health import register_dashboard_task

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        watched = self.service.create_task("Watched done", "shell", [{"id": 1, "description": "done"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(watched.id, "shell")
        self.service.execute_step(watched.id, 1, {"status": "done"})
        finalize_completed(self.service, watched.id)
        register_dashboard_task(self.board, watched.id, reset=True)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False, all_iterations=False, watch_task_id=watched.id)
        summaries = _task_list_summaries(args)
        self.assertEqual([summary["task_id"] for summary in summaries], [watched.id])
        self.assertFalse(_summary_keeps_dashboard_alive(summaries[0]))

    def test_task_list_watch_does_not_restore_task_removed_from_cohort(self):
        from agent_bridge_connect.cli import _task_list_summaries
        from agent_bridge_connect.task_health import register_dashboard_task, remove_dashboard_task

        register_dashboard_task(self.board, self.task.id, reset=True)
        remove_dashboard_task(self.board, self.task.id)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False, all_iterations=False, watch_task_id=self.task.id)
        self.assertEqual(_task_list_summaries(args), [])

    def test_task_list_watch_keeps_recovery_task_visible(self):
        from agent_bridge_connect.cli import _task_list_summaries
        from agent_bridge_connect.task_health import register_dashboard_task

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        recovery = self.service.create_task("Needs recovery", "codex", [{"id": 1, "description": "recover"}], customer_dir=True, customer_path=workspace)
        self.service.mark_task_needs_recovery(recovery.id, "executor_error", "capacity")
        register_dashboard_task(self.board, recovery.id, reset=True)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False, all_iterations=False, watch_task_id=recovery.id)
        summaries = _task_list_summaries(args)
        self.assertEqual([summary["task_id"] for summary in summaries], [recovery.id])
        self.assertEqual(summaries[0]["health_color"], "red")

    def test_task_list_watch_excludes_historical_recovery_tasks(self):
        from agent_bridge_connect.cli import _task_list_summaries
        from agent_bridge_connect.task_health import register_dashboard_task

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        historical = self.service.create_task("Historical recovery", "codex", [{"id": 1, "description": "old"}], customer_dir=True, customer_path=workspace)
        self.service.mark_task_needs_recovery(historical.id, "executor_error", "old")
        watched = self.service.create_task("Current active", "codex", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(watched.id, "codex")
        register_dashboard_task(self.board, watched.id, reset=True)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False, all_iterations=False, watch_task_id=watched.id)
        summaries = _task_list_summaries(args)
        self.assertEqual([summary["task_id"] for summary in summaries], [watched.id])

    def test_task_list_watch_appends_new_dispatch_to_existing_cohort(self):
        from agent_bridge_connect.cli import _task_list_summaries
        from agent_bridge_connect.task_health import register_dashboard_task

        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        first = self.service.create_task("First active", "codex", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        second = self.service.create_task("Second active", "hermes", [{"id": 1, "description": "run"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(first.id, "codex")
        self.service.claim_task(second.id, "hermes")
        register_dashboard_task(self.board, first.id, reset=True)
        register_dashboard_task(self.board, second.id)
        args = mock.Mock(root=self.board, status=None, assignee=None, current=False, all_iterations=False, watch_task_id=first.id)
        summaries = _task_list_summaries(args)
        self.assertEqual([summary["task_id"] for summary in summaries], [first.id, second.id])

    def test_handoff_reuses_output_dir_and_records_lineage(self):
        workspace = self.board.parent / "project"
        workspace.mkdir(exist_ok=True)
        source = self.service.create_task("Shader source", "hermes", [{"id": 1, "description": "produce evidence"}], customer_dir=True, customer_path=workspace)
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        followup = self.service.handoff_task(source.id, "codex", "在最新基础上继续改")
        lineage = followup.extensions["agentbc.lineage"]
        self.assertEqual(followup.workspace["root"], source.workspace["root"])
        self.assertEqual(followup.workspace["output_dir"], source.workspace["output_dir"])
        self.assertEqual(followup.workspace["artifacts_dir"], source.workspace["artifacts_dir"])
        self.assertEqual(lineage["parent_task_id"], source.id)
        self.assertEqual(lineage["base_task_id"], source.id)
        self.assertEqual(lineage["chain_root_task_id"], source.id)
        self.assertEqual(lineage["iteration_index"], 2)
        self.assertEqual(lineage["chain_task_id"], "002")
        self.assertEqual(lineage["chain_token"], source.workspace["chain_token"])
        self.assertEqual(followup.id, f"{source.workspace['task_code']}-002")
        self.assertEqual(followup.workspace["chain_task_id"], "002")
        self.assertEqual(followup.workspace["chain_token"], source.workspace["chain_token"])
        self.assertEqual(Path(followup.workspace["task_file"]).name, f"{followup.id}-task.md")
        self.assertEqual(Path(followup.workspace["report_file"]).name, f"{followup.id}-report.md")
        self.assertNotEqual(followup.workspace["task_file"], followup.workspace["report_file"])

    def test_create_rejects_existing_managed_artifact_path_and_requires_handoff(self):
        from agent_bridge_connect.protocol import ABCError

        source = self.service.create_task("Managed source", "hermes", [{"id": 1, "description": "create managed output"}], customer_dir=False, source_platform="hermes")
        artifact_root = Path(source.workspace["artifact_root"])
        (artifact_root / "output.txt").write_text("baseline", encoding="utf-8")
        self.service.claim_task(source.id, "hermes")
        self.service.execute_step(source.id, 1, {"status": "done"})
        finalize_completed(self.service, source.id)
        before = {task.id for task in self.service.list_tasks()}
        with self.assertRaises(ABCError) as raised:
            self.service.create_task("Modify managed source as a new root", "codex", [{"id": 1, "description": "modify output.txt"}], customer_path=artifact_root, source_platform="claude")
        self.assertEqual(raised.exception.code, "handoff_required")
        self.assertEqual(raised.exception.details["source_task_id"], source.id)
        self.assertIn(f"task handoff {source.id} --to codex", raised.exception.details["suggested_command"])
        self.assertIn("--source-platform claude", raised.exception.details["suggested_command"])
        self.assertEqual({task.id for task in self.service.list_tasks()}, before)

    def test_terminal_cleanup_removes_only_empty_managed_artifacts(self):
        empty = self.service.create_task("Empty managed output", "shell", [{"id": 1, "description": "report only"}], customer_dir=False)
        empty_root = Path(empty.workspace["artifact_root"])
        self.service.claim_task(empty.id, "shell")
        finalize_completed(self.service, empty.id)
        self.assertFalse(empty_root.exists())
        self.assertTrue(Path(empty.workspace["report_file"]).is_file())
        nonempty = self.service.create_task("Nonempty managed output", "shell", [{"id": 1, "description": "write output"}], customer_dir=False)
        nonempty_root = Path(nonempty.workspace["artifact_root"])
        (nonempty_root / "keep.txt").write_text("keep", encoding="utf-8")
        self.service.claim_task(nonempty.id, "shell")
        finalize_completed(self.service, nonempty.id)
        self.assertTrue(nonempty_root.is_dir())
        self.assertTrue((nonempty_root / "keep.txt").is_file())

    def test_requeue_recreates_cleaned_managed_artifact_root(self):
        task = self.service.create_task("Recover empty managed task", "shell", [{"id": 1, "description": "retry later"}], customer_dir=False)
        artifact_root = Path(task.workspace["artifact_root"])
        self.service.claim_task(task.id, "shell")
        self.service.mark_task_needs_recovery(task.id, "network", "offline")
        self.assertFalse(artifact_root.exists())
        requeued = self.service.requeue_task(task.id)
        self.assertEqual(requeued.status, "pending")
        self.assertTrue(artifact_root.is_dir())
