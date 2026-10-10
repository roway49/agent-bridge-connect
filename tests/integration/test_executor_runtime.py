from __future__ import annotations
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from tests.contract_helpers import completed_callback


class ExecutorRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform != "win32", "POSIX signal contract")
    def test_sync_codex_wait_handles_sigterm_and_restores_handler(self):
        import subprocess
        code = """
import os,signal,sys
from agent_bridge_connect.executors.base import run_with_stdout_receipts
before=signal.getsignal(signal.SIGTERM)
def receipt(line):
    if line.strip()=='ready': os.kill(os.getpid(),signal.SIGTERM)
r=run_with_stdout_receipts([sys.executable,'-u','-c','import time;print("ready",flush=True);time.sleep(20)'],cwd='.',input='',timeout=30,on_line=receipt,interrupt_on_sigterm=True)
assert r.agentbc_interrupted and r.returncode != 0
assert signal.getsignal(signal.SIGTERM)==before
print('interrupted-without-worker-death')
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("interrupted-without-worker-death", result.stdout)

    def setUp(self):
        self.board = Path(tempfile.mkdtemp())
        self.config = {"workspace_root": str(self.board)}
        from agent_bridge_connect.service import TaskService

        self.service = TaskService(self.board, config=self.config)
        self.task = self.service.create_task("Integration task", "shell", [{"id": 1, "description": "printf hello"}], customer_dir=False)

    def tearDown(self):
        shutil.rmtree(self.board)

    @mock.patch("agent_bridge_connect.executors.codex.subprocess.run")
    def test_codex_allows_non_git_workspaces(self, run):
        from agent_bridge_connect.executors.codex import CodexExecutor

        codex_binary = self.board / "codex"
        codex_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        run.return_value = mock.Mock(returncode=0, stdout='{"type":"agent_message","text":"done"}\n', stderr="")
        executor = CodexExecutor(command=str(codex_binary), transport="cli")
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "write hello.txt"}], "workspace": {"root": str(self.board)}})
        command = run.call_args.args[0]
        self.assertTrue(result.ok)
        self.assertIn("--skip-git-repo-check", command)
        self.assertLess(command.index("--json"), len(command) - 1)

    @mock.patch("agent_bridge_connect.executors.codex.subprocess.run")
    def test_codex_limits_customer_project_writes_to_project_and_record(self, run):
        from agent_bridge_connect.executors.codex import CodexExecutor

        codex_binary = self.board / "codex"
        codex_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        project = self.board.parent / "customer-project"
        project.mkdir(exist_ok=True)
        report_root = self.board.parent / "agentbc-workspace" / "agentbc_tasks" / "report" / "2026-07-10" / "ABCD"
        report_root.mkdir(parents=True, exist_ok=True)
        agentbc_root = self.board.parent / "agentbc-workspace"
        task = self.service.create_task("Codex customer project", "codex", [{"id": 1, "description": "write progress"}], customer_dir=True, customer_path=project)
        packet = {
            "task_id": task.id,
            "title": task.title,
            "steps": [{"id": 1, "description": "write progress"}],
            "workspace": {
                **task.workspace,
                "agentbc_root": str(agentbc_root),
                "report_root": str(report_root),
                "task_file": str(report_root / f"{task.id}-task.md"),
                "report_file": str(report_root / f"{task.id}-report.md"),
            },
            "task_board": {"root": str(self.board)},
        }
        run.return_value = mock.Mock(returncode=0, stdout='{"type":"agent_message","text":"done"}\n', stderr="")
        result = CodexExecutor(command=str(codex_binary), transport="cli").start(packet)
        self.assertTrue(result.ok)
        command = run.call_args.args[0]
        add_dirs = [command[index + 1] for (index, value) in enumerate(command) if value == "--add-dir"]
        self.assertIn(str(project.resolve()), add_dirs)
        self.assertIn(str(self.board.resolve()), add_dirs)
        self.assertNotIn(str(agentbc_root.resolve()), add_dirs)
        self.assertNotIn(str(report_root.resolve()), add_dirs)
        self.assertLess(command.index("--add-dir"), len(command) - 1)

    def test_claude_limits_customer_project_writes_to_artifact_only(self):
        from agent_bridge_connect.executors.claude import _claude_writable_roots

        project = self.board.parent / "claude-customer-project"
        project.mkdir(exist_ok=True)
        agentbc_root = self.board.parent / "claude-agentbc-workspace"
        report_root = agentbc_root / "tasks" / "report" / "2026-07-15" / "ABCD"
        packet = {
            "workspace": {"root": str(project), "project_root": str(project), "artifact_root": str(project), "agentbc_root": str(agentbc_root), "report_root": str(report_root)},
            "task_board": {"root": str(self.board)},
        }
        roots = [str(path) for path in _claude_writable_roots(packet, project)]
        self.assertIn(str(project.resolve()), roots)
        self.assertNotIn(str(self.board.resolve()), roots)
        self.assertNotIn(str(agentbc_root.resolve()), roots)
        self.assertNotIn(str(report_root.resolve()), roots)

    def test_codex_falls_back_from_stale_configured_path_to_discovery(self):
        from agent_bridge_connect.executors.codex import CodexExecutor

        replacement = self.board / "chatgpt-codex"
        replacement.write_text("#!/bin/sh\n", encoding="utf-8")
        stale = "/Applications/Codex.app/Contents/Resources/codex"
        with mock.patch("agent_bridge_connect.executors.codex.find_binary", return_value={"found": True, "path": str(replacement), "source": "chatgpt_desktop", "searched_paths": [stale, str(replacement)]}) as discover:
            executor = CodexExecutor(command=stale)
        self.assertEqual(executor.agent_bin, replacement)
        self.assertEqual(executor.get_extensions()["executor"]["codex"]["agent_bin_source"], "chatgpt_desktop")
        self.assertEqual(discover.call_args.kwargs["extra_paths"], [stale])

    def test_executor_registry_passes_codex_command_to_runtime(self):
        from agent_bridge_connect.executor_registry import get_executor

        codex_binary = self.board / "custom-codex-runtime"
        codex_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        executor = get_executor("codex", {"command": str(codex_binary)})
        self.assertEqual(executor.agent_bin, codex_binary.resolve())

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_zero_exit_initialization_failure_is_not_success(self, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(returncode=0, stdout="Failed to initialize agent: Operation not permitted", stderr="")
        executor = HermesExecutor(command=sys.executable, transport="direct")
        executor._version = "Hermes 1.0"
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}})
        self.assertTrue(result.ok)
        polled = executor.poll(result.run_id)
        self.assertEqual(polled.status, "failed")
        self.assertEqual(polled.result["failure"]["kind"], "completion_marker_missing")

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_profile_log_denial_is_classified_as_parent_sandbox(self, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(returncode=0, stdout="Failed to initialize agent: [Errno 1] Operation not permitted:\n'/Users/test/.hermes/profiles/pm/logs/agent.log'", stderr="")
        executor = HermesExecutor(command=sys.executable, transport="direct")
        executor._version = "Hermes 1.0"
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        failure = executor.poll(result.run_id).result["failure"]
        self.assertEqual(failure["kind"], "parent_sandbox_write_denied")
        self.assertEqual(failure["layer"], "executor")
        self.assertTrue(failure["retryable"])
        self.assertIn("approve", failure["action"].lower())

    @mock.patch("agent_bridge_connect.executors.hermes.RunnerClient")
    def test_hermes_runner_transport_submits_and_polls(self, runner_client):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        client = runner_client.return_value
        client.health.return_value = {"ok": True, "status": "ready", "executors": ["hermes"]}
        client.submit.side_effect = lambda *args, **kwargs: {"ok": True, "run_id": kwargs["executor_run_id"], "pid": 4321, "status": "running"}
        client.status.return_value = {
            "ok": True,
            "run_id": "runner-hermes-test",
            "pid": 4321,
            "cwd": str(self.board),
            "status": "completed",
            "returncode": 0,
            "stdout": f"RUNNER_OK\nAGENTBC_FINAL_CALLBACK: {json.dumps(completed_callback(self.task, summary='runner ok'), separators=(',', ':'))}\n",
            "stderr": "",
            "output_truncated": False,
        }
        executor = HermesExecutor(transport="runner")
        executor.agent_bin = Path(sys.executable)
        executor._version = "Hermes test"
        started = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        polled = executor.poll(started.run_id)
        self.assertTrue(started.ok)
        self.assertEqual(started.run_id, client.submit.call_args.kwargs["executor_run_id"])
        self.assertEqual(polled.status, "completed")
        self.assertEqual(polled.result["transport"], "runner")
        self.assertEqual(polled.result["extensions"]["executor.hermes"]["last_run"]["returncode"], 0)
        command = client.submit.call_args.args[1]
        self.assertEqual(command[:2], [sys.executable, "chat"])

    @mock.patch("agent_bridge_connect.executors.hermes.RunnerClient")
    def test_hermes_runner_status_error_is_transient_not_recovery(self, runner_client):
        from agent_bridge_connect.executors.hermes import HermesExecutor
        from agent_bridge_connect.runner import RunnerError

        client = runner_client.return_value
        client.health.return_value = {"ok": True, "status": "ready", "executors": ["hermes"]}
        client.submit.side_effect = lambda *args, **kwargs: {"ok": True, "run_id": kwargs["executor_run_id"], "pid": 4321, "status": "running"}
        client.status.side_effect = RunnerError("unknown runner run: runner-hermes-transient")
        executor = HermesExecutor(transport="runner")
        executor.agent_bin = Path(sys.executable)
        started = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        polled = executor.poll(started.run_id)
        self.assertTrue(started.ok)
        self.assertEqual(started.run_id, client.submit.call_args.kwargs["executor_run_id"])
        self.assertEqual(polled.status, "running")
        self.assertEqual(polled.progress["runner_status"], "transient_unavailable")
        self.assertEqual(polled.result["failure"]["kind"], "runner_status_transient")

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    @mock.patch("agent_bridge_connect.executors.hermes.RunnerClient")
    def test_hermes_runner_probe_never_executes_local_binary(self, runner_client, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        runner_client.return_value.health.return_value = {"ok": True, "status": "ready", "executors": ["hermes"]}
        executor = HermesExecutor(command=sys.executable, transport="runner")
        result = executor.probe()
        self.assertTrue(result.ok)
        self.assertEqual(result.details["transport"], "runner")
        run.assert_not_called()

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    @mock.patch("agent_bridge_connect.executors.hermes.RunnerClient")
    def test_hermes_runner_probe_fails_closed_without_direct_fallback(self, runner_client, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor
        from agent_bridge_connect.runner import RunnerError

        runner_client.return_value.health.side_effect = RunnerError("not running")
        executor = HermesExecutor(command=sys.executable, transport="runner")
        result = executor.probe()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["failure_kind"], "runner_unavailable")
        run.assert_not_called()

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_success_can_quote_old_failure_without_becoming_failed(self, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(
            returncode=0,
            stdout=f"Initializing agent...\ncreated artifacts/output.txt\n- old report: Failed to initialize agent: Operation not permitted\nTask completed successfully\nSession: 20260619_test\nAGENTBC_FINAL_CALLBACK: {json.dumps(completed_callback(self.task, summary='quoted old failure safely'), separators=(',', ':'))}\n",
            stderr="",
        )
        executor = HermesExecutor(command=sys.executable, transport="direct")
        executor._version = "Hermes 1.0"
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        self.assertTrue(result.ok)
        self.assertEqual(executor.poll(result.run_id).status, "completed")

    def test_executor_prompts_reserve_report_for_agentbc_core(self):
        from agent_bridge_connect.executors.codex import _build_prompt as codex_prompt
        from agent_bridge_connect.executors.claude import _build_prompt as claude_prompt
        from agent_bridge_connect.executors.hermes import _build_prompt as hermes_prompt

        packet = {
            "task_id": self.task.id,
            "title": self.task.title,
            "steps": [{"id": 1, "description": "write artifact"}],
            "workspace": {"root": str(self.board), "task_file": str(self.board / "TASK.md"), "report_file": str(self.board / "REPORT.md"), "artifacts_dir": str(self.board / "artifacts")},
        }
        for prompt in (codex_prompt(packet), hermes_prompt(packet), claude_prompt(packet)):
            self.assertNotIn("Report target:", prompt)
            self.assertIn("Do not write or replace REPORT.md", prompt)
            self.assertIn("AGENTBC_FINAL_CALLBACK", prompt)
            self.assertIn("A zero CLI exit without a valid marker fails the task", prompt)

    def test_executor_prompts_support_legacy_action_steps(self):
        from agent_bridge_connect.executors.codex import _build_prompt as codex_prompt
        from agent_bridge_connect.executors.claude import _build_prompt as claude_prompt
        from agent_bridge_connect.executors.hermes import _build_prompt as hermes_prompt

        packet = {"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "action": "legacy action requirement"}], "workspace": {"root": str(self.board)}}
        for prompt in (codex_prompt(packet), hermes_prompt(packet), claude_prompt(packet)):
            self.assertIn("1. legacy action requirement", prompt)

    @mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_claude_default_command_is_safe_print_mode(self, run):
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        run.return_value = mock.Mock(returncode=0, stdout=f"AGENTBC_FINAL_CALLBACK: {json.dumps(completed_callback(self.task, summary='ok'), separators=(',', ':'))}\n", stderr="")
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        result = executor.start(
            {
                "task_id": self.task.id,
                "title": self.task.title,
                "steps": [{"id": 1, "description": "review"}],
                "workspace": {"root": str(self.board), "report_file": str(self.board / "report_001.md"), "artifacts_dir": str(self.board / "artifacts")},
                "task_board": {"root": str(self.board)},
            }
        )
        command = run.call_args.args[0]
        self.assertTrue(result.ok)
        self.assertEqual(command[:2], [sys.executable, "-p"])
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(command[command.index("--output-format") + 1], "text")
        self.assertNotIn("--add-dir", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Write,Edit,Bash,BashOutput")
        self.assertEqual(command[command.index("--disallowedTools") + 1], "TaskCreate,TaskUpdate,TodoWrite")
        self.assertNotIn("bypassPermissions", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertIn("AGENTBC_FINAL_CALLBACK", run.call_args.kwargs["input"])
        self.assertIn("A zero CLI exit without a valid marker fails the task", run.call_args.kwargs["input"])
        self.assertIn("use BashOutput repeatedly until it exits", run.call_args.kwargs["input"])
        self.assertNotIn("AGENTBC_FINAL_CALLBACK", command)

    @mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_claude_normal_exit_without_final_callback_fails(self, run):
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        packet = {"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}}
        run.return_value = mock.Mock(returncode=0, stdout="done without callback\n", stderr="")
        started = executor.start(packet)
        polled = executor.poll(started.run_id)
        self.assertEqual(polled.status, "failed")
        self.assertIsNone(polled.result["agent_callback"])
        self.assertEqual(polled.result["failure"]["kind"], "completion_marker_missing")

    @mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_claude_json_output_can_extract_callback(self, run):
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        callback_line = "AGENTBC_FINAL_CALLBACK: " + json.dumps(completed_callback(self.task, summary="json ok"), separators=(",", ":"))
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps({"type": "result", "subtype": "success", "result": f"summary\n{callback_line}\n"}), stderr="")
        executor = ClaudeExecutor(command=sys.executable, transport="direct", output_format="json")
        started = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        polled = executor.poll(started.run_id)
        self.assertEqual(polled.status, "completed")
        self.assertEqual(polled.result["agent_callback"]["summary"], "json ok")

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_probe_inherits_profile_and_delegates_auth(self, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(returncode=0, stdout="Hermes 1.0", stderr="")
        executor = HermesExecutor(command=sys.executable, transport="direct")
        result = executor.probe()
        self.assertTrue(result.ok)
        self.assertEqual(result.details["profile_mode"], "inherit")
        self.assertIsNone(result.details["profile"])
        self.assertEqual(result.details["auth_owner"], "hermes_cli")

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_default_command_is_quiet_without_bypassing_approvals(self, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(returncode=0, stdout="done", stderr="")
        executor = HermesExecutor(command=sys.executable, transport="direct")
        result = executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        command = next((call.args[0] for call in run.call_args_list if "chat" in call.args[0]))
        self.assertTrue(result.ok)
        self.assertEqual(command[:2], [sys.executable, "chat"])
        self.assertIn("-Q", command)
        self.assertNotIn("-p", command)
        self.assertNotIn("-z", command)
        self.assertNotIn("--oneshot", command)

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_explicit_runtime_overrides_use_documented_flags(self, run):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = mock.Mock(returncode=0, stdout="done", stderr="")
        executor = HermesExecutor(profile="pm", provider="nous", model="test-model", transport="direct")
        executor.agent_bin = Path(sys.executable)
        executor.start({"task_id": self.task.id, "title": self.task.title, "steps": [{"id": 1, "description": "review"}], "workspace": {"root": str(self.board)}, "task_board": {"root": str(self.board)}})
        command = next((call.args[0] for call in run.call_args_list if "chat" in call.args[0]))
        self.assertEqual(command[:3], [sys.executable, "-p", "pm"])
        self.assertEqual(command[command.index("--provider") + 1], "nous")
        self.assertEqual(command[command.index("--model") + 1], "test-model")

    def test_executor_registry_rejects_unknown_platform_config(self):
        from agent_bridge_connect.executor_registry import get_executor

        with self.assertRaisesRegex(ValueError, "Unsupported hermes executor config"):
            get_executor("hermes", {"profile_path": "/tmp/unsafe"})

    def test_executor_registry_applies_hermes_runtime_config(self):
        from agent_bridge_connect.executor_registry import get_executor

        executor = get_executor("hermes", {"type": "hermes", "command": sys.executable, "runtime_source": "npm_global", "profile": "pm", "provider": "nous", "quiet": False})
        self.assertEqual(executor.agent_bin, Path(sys.executable))
        self.assertEqual(executor.profile, "pm")
        self.assertEqual(executor.provider, "nous")
        self.assertFalse(executor.quiet)

    def test_executor_registry_applies_claude_runtime_config(self):
        from agent_bridge_connect.executor_registry import get_executor

        executor = get_executor(
            "claude",
            {
                "type": "claude",
                "command": sys.executable,
                "permission_mode": "acceptEdits",
                "safe_mode": True,
                "output_format": "text",
                "max_budget_usd": 0.5,
                "allowed_tools": ["Read", "Write"],
                "runtime_source": "test",
            },
        )
        self.assertEqual(executor.agent_bin, Path(sys.executable))
        self.assertEqual(executor.permission_mode, "acceptEdits")
        self.assertTrue(executor.safe_mode)
        self.assertEqual(executor.output_format, "text")
        self.assertEqual(executor.tools, ["Read", "Write"])
        self.assertEqual(executor.auto_approve_tools, [])

    def test_executor_registry_rejects_claude_bypass_permissions(self):
        from agent_bridge_connect.executor_registry import get_executor

        with self.assertRaisesRegex(ValueError, "bypassPermissions"):
            get_executor("claude", {"permission_mode": "bypassPermissions"})

    def test_worker_preserves_adapter_recovery_without_error_name_heuristics(self):
        from agent_bridge_connect.adapters import PollResult
        from agent_bridge_connect.cli import command_worker_run
        from agent_bridge_connect.executors.mock import MockExecutor
        task = self.service.create_task("Interrupted worker", "mock", [{"id": 1, "description": "wait"}], customer_dir=False)
        executor = MockExecutor()
        result = PollResult(status="needs_recovery", progress={}, result={"failure": {
            "kind": "executor_turn_interrupted", "message": "interrupted", "retryable": True}})
        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor), mock.patch.object(executor, "poll", return_value=result):
            with mock.patch("agent_bridge_connect.cli._notify_terminal"):
                code = command_worker_run(mock.Mock(root=self.board, executor="mock", once=True, interval=.01, config=None))
        current = self.service.get_task(task.id)
        self.assertEqual(code, 1)
        self.assertEqual(current.status, "needs_recovery")
        self.assertNotIn("agentbc.final_callback", current.extensions)

    def test_worker_passes_hermes_config_to_executor_registry(self):
        from agent_bridge_connect.cli import command_worker_run
        from agent_bridge_connect.executors.mock import MockExecutor

        config_path = self.board / "config.toml"
        config_path.write_text('[executors.hermes]\nprofile = "pm"\nquiet = true\n', encoding="utf-8")
        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=MockExecutor()) as registry:
            code = command_worker_run(mock.Mock(root=self.board, executor="hermes", once=True, interval=0.01, config=config_path))
        self.assertEqual(code, 0)
        registry.assert_called_once_with("hermes", {"profile": "pm", "quiet": True})

    def test_worker_passes_claude_config_to_executor_registry(self):
        from agent_bridge_connect.cli import command_worker_run
        from agent_bridge_connect.executors.mock import MockExecutor

        config_path = self.board / "config.toml"
        config_path.write_text('[executors.claude]\npermission_mode = "acceptEdits"\nsafe_mode = true\noutput_format = "text"\nmax_budget_usd = 0.25\n', encoding="utf-8")
        with mock.patch("agent_bridge_connect.cli.get_executor", return_value=MockExecutor()) as registry:
            code = command_worker_run(mock.Mock(root=self.board, executor="claude", once=True, interval=0.01, config=config_path))
        self.assertEqual(code, 0)
        registry.assert_called_once_with("claude", {"permission_mode": "acceptEdits", "safe_mode": True, "output_format": "text", "max_budget_usd": 0.25})

    @mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_worker_hermes_layered_contract_without_real_agent(self, run):
        from agent_bridge_connect.adapters import ProbeResult
        from agent_bridge_connect.cli import command_worker_run
        from agent_bridge_connect.executors.hermes import HermesExecutor

        workspace = self.board / "hermes-workspace"
        workspace.mkdir()
        task = self.service.create_task("Hermes layered contract", "hermes", [{"id": 1, "description": "produce artifact"}], customer_dir=True, customer_path=workspace)
        run.return_value = mock.Mock(
            returncode=0,
            stdout=f"Initializing agent...\n- historical text: Failed to initialize agent: old failure\nTask completed successfully\nSession: isolated_test\nAGENTBC_FINAL_CALLBACK: {json.dumps(completed_callback(task, summary='layered contract complete'), separators=(',', ':'))}\n",
            stderr="session_id: 20260810_010203_phase3\n",
        )
        executor = HermesExecutor(command=sys.executable, transport="direct")
        executor._version = "Hermes test"
        with mock.patch.object(executor, "probe", return_value=ProbeResult(ok=True, message="ready")):
            with mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor):
                code = command_worker_run(mock.Mock(root=self.board, executor="hermes", once=True, interval=0.01, config=None))
        completed = self.service.get_task(task.id)
        self.assertEqual(code, 0)
        self.assertEqual(completed.status, "completed")
        self.assertTrue((self.board / task.id.split("-")[0] / task.id.split("-")[1] / "run_lease.json").exists())
        self.assertFalse((workspace / "tasks" / task.id.split("-")[0] / task.id.split("-")[1] / "run_lease.json").exists())
        report = Path(completed.workspace["report_file"]).read_text(encoding="utf-8")
        self.assertTrue(report.startswith("# Report:"))
        self.assertNotIn("AgentBC Execution Report", report)


class OpenCodeDispatchIntegrationTests(unittest.TestCase):
    """Real CLI -> isolated Runner IPC -> worker -> registry -> fake CLI."""

    def setUp(self):
        import os
        from agent_bridge_connect.config import toml_dumps
        from agent_bridge_connect.runner import RunnerState, RunnerService
        import threading

        self.temp = tempfile.TemporaryDirectory(prefix="opencode-routing-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "工程 with spaces"
        self.project.mkdir()
        self.board = self.root / "record"
        self.capture = self.root / "capture.json"
        self.fake = self.root / "opencode"
        self.fake.write_text(f"#!{sys.executable}\n" + '''import json, os, re, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    if os.environ.get("OPENCODE_TEST_REMOVE_ON_PROBE"): Path(sys.argv[0]).unlink()
    print("opencode fake-105003"); sys.exit(0)
if sys.argv[1:3] == ["debug", "agent"]:
    print(json.dumps({"permission": []})); sys.exit(0)
args = sys.argv[1:]
prompt = args[-1]
if "agentbc task progress" in prompt:
    print("progress_session_receipt_invalid", file=sys.stderr); sys.exit(8)
task_id = re.search(r"Task ID: ([A-Z0-9]+-[0-9]+)", prompt).group(1)
Path(os.environ["OPENCODE_TEST_CAPTURE"]).write_text(json.dumps({
    "argv": args, "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
    "worker": os.environ.get("AGENTBC_RUNNER_WORKER_ID"), "task_id": task_id,
    "permission": os.environ.get("OPENCODE_PERMISSION"),
}))
if os.environ.get("OPENCODE_TEST_FAIL"):
    print("controlled execution failure", file=sys.stderr); sys.exit(7)
Path("routing-artifact.txt").write_text(task_id + "\\n", encoding="utf-8")
callback = {"version": 1, "task_id": task_id, "final_state": "completed",
            "summary": "fake executed", "step_results": [{"id": 1, "status": "done"}]}
print(json.dumps({"type": "text", "sessionID": "fake-" + task_id,
                  "part": {"text": "AGENTBC_FINAL_CALLBACK: " + json.dumps(callback)}}))
print(json.dumps({"type": "step_finish", "sessionID": "fake-" + task_id, "part": {"reason": "stop"}}))
''')
        self.fake.chmod(0o755)
        # Only UI notification is replaced in the child; task/worker/adapter
        # dispatch, process execution, callback and lease all run unchanged.
        hook = self.root / "hooks"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(
            "from agent_bridge_connect.notifiers.dialog import DialogNotifier\n"
            "from agent_bridge_connect.adapters import DeliveryResult\n"
            "DialogNotifier.send = lambda self, notice: DeliveryResult(True, 'test UI')\n"
        )
        self.config = {"workspace_root": str(self.root), "board_root": str(self.board),
                       "permissions": {"mode": "inherit"},
                       "executors": {"opencode": {"command": str(self.fake), "model": "fake/model", "timeout_s": 10}}}
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(toml_dumps(self.config))
        env = {"AGENTBC_CONFIG_PATH": str(self.config_path), "OPENCODE_TEST_CAPTURE": str(self.capture),
               "AGENTBC_OPENCODE_BIN": str(self.fake), "AGENTBC_RUNNER_SPOOL": str(self.root / "spool"),
               "PYTHONPATH": os.pathsep.join([str(hook), str(Path(__file__).resolve().parents[2] / "src")]),
               "AGENTBC_DESKTOP_BOOTSTRAPPED": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The Runner can win terminal delivery before the worker. Isolate its
        # UI too: a real dialog holds the delivery lock until human dismissal.
        # All task/callback/lease/worker-exit assertions remain real and unchanged.
        from agent_bridge_connect.adapters import DeliveryResult
        parent_dialog = mock.patch("agent_bridge_connect.notifiers.dialog.DialogNotifier.send",
                                   return_value=DeliveryResult(True, "test UI"))
        parent_dialog.start()
        self.addCleanup(parent_dialog.stop)
        self.state = RunnerState(self.root / "state", [self.root], {"opencode": self.fake}, {"opencode": "config"})
        self.waiters = []
        original_wait = self.state._wait_for_process
        def tracked_wait(*args):
            self.waiters.append(threading.current_thread())
            return original_wait(*args)
        self.state._wait_for_process = tracked_wait
        self.runner = RunnerService(self.root / "spool", self.root / "spool/token", self.state, interval_s=0.01)
        self.thread = threading.Thread(target=self.runner.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_runner)
        from agent_bridge_connect.service import TaskService
        self.tasks = TaskService(self.board, config=self.config)
        self.steps = self.root / "steps.yaml"
        self.steps.write_text('steps:\n  - id: 1\n    description: "write routing-artifact.txt 中文"\n')

    def _stop_runner(self):
        self.runner.shutdown()
        self.thread.join(timeout=3)
        for run_id, record in list(self.state.runs.items()):
            if record.get("status") in {"running", "accepted"}:
                self.state.cancel(run_id)
        # A terminal process status precedes report/session finalization in the
        # watcher. Join that owner before deleting the test's task directory.
        for waiter in self.waiters:
            waiter.join(timeout=10)
            self.assertFalse(waiter.is_alive(), "Runner finalization did not finish")
        self.assertFalse(self.thread.is_alive(), "Runner service did not stop")

    def _create(self, *extra):
        import contextlib
        import io
        from agent_bridge_connect.cli import main
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["task", "create", "--root", str(self.board), "--title", "Routing 中文",
                         "--assignee", "opencode", "--steps", str(self.steps),
                         "--customer-path", str(self.project), "--source-platform", "test", *extra])
        self.assertEqual(code, 0, output.getvalue())
        return self.tasks.list_tasks(assignee="opencode")[0]

    def _wait(self, task_id):
        import time
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = self.tasks.get_task(task_id)
            worker = (task.extensions.get("agentbc.execution") or {}).get("worker_run_id")
            if worker:
                record = self.state.status(worker)
                if record.get("status") in {"completed", "failed", "cancelled"}:
                    return self.tasks.get_task(task_id), record
            time.sleep(0.05)
        self.fail(f"Worker did not terminate: {self.tasks.get_task(task_id).to_dict()}")

    def test_create_dispatch_real_worker_preserves_identity_and_inputs(self):
        from agent_bridge_connect.run_lease import load_lease
        task = self._create("--dispatch")
        terminal, worker = self._wait(task.id)
        self.assertEqual(terminal.status, "completed", worker)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["task_id"], task.id)
        self.assertEqual(capture["worker"], worker["run_id"])
        self.assertEqual(capture["cwd"], str(self.project))
        self.assertEqual(capture["pwd"], str(self.project))
        args = capture["argv"]
        self.assertEqual(args[args.index("--dir") + 1], str(self.project))
        self.assertEqual(args[args.index("--model") + 1], "fake/model")
        self.assertIn("Routing 中文", args[-1])
        self.assertIn("write routing-artifact.txt 中文", args[-1])
        self.assertIsNone(capture["permission"])
        self.assertEqual((self.project / "routing-artifact.txt").read_text(), task.id + "\n")
        execution = terminal.extensions["agentbc.execution"]
        self.assertEqual(execution["executor_run_id"], worker["run_id"])
        self.assertEqual(load_lease(task.id, self.board).state, "closed")
        self.assertTrue(terminal.extensions.get("agentbc.final_callback"))
        self.assertNotIn("agentbc.session", terminal.extensions)
        report = Path(terminal.workspace["report_file"]).read_text(encoding="utf-8")
        self.assertIn(task.id, report)
        self.assertIn("Flow contract satisfied: `yes`", report)
        self.assertIn("1. [done] write routing-artifact.txt", report)
        self.assertTrue(all(step["status"] == "done" for step in terminal.steps))

    def test_stale_worker_identity_cannot_execute(self):
        import contextlib
        import io
        import os
        from agent_bridge_connect.cli import main
        task = self._create()
        self.tasks.update_execution_metadata(task.id, {"worker_run_id": "stale-worker"})
        with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_WORKER_ID": "other-worker", "AGENTBC_RUNNER_TASK_ID": task.id}), mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=mock.Mock(ok=True, message="test UI")), contextlib.redirect_stdout(io.StringIO()):
            code = main(["worker", "run", "--root", str(self.board), "--executor", "opencode", "--task-id", task.id,
                         "--runner-authorize", "--once"])
        self.assertEqual(code, 1)
        self.assertIn("opencode_run_id_conflict", json.dumps(self.tasks.get_task(task.id).to_dict()))
        self.assertFalse(self.capture.exists())

    def test_full_dispatch_uses_native_overlay_without_changing_task_policy(self):
        task = self._create("--dispatch", "--permission-mode", "full")
        terminal, worker = self._wait(task.id)
        self.assertEqual(terminal.status, "completed", worker)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["argv"][capture["argv"].index("--agent") + 1], "build")
        self.assertIsInstance(json.loads(capture["permission"]), dict)
        self.assertEqual(terminal.extensions["agentbc.permission"]["effective_mode"], "full")

    def test_changed_command_rejected_before_task_creation(self):
        import os
        from agent_bridge_connect.runner import RunnerError
        other = self.root / "other-opencode"
        other.write_text(self.fake.read_text())
        other.chmod(0o755)
        with mock.patch.dict(os.environ, {"AGENTBC_OPENCODE_BIN": str(other)}):
            with self.assertRaisesRegex(RunnerError, "runner_config_stale"):
                self.state.create_and_dispatch({"assignee": "opencode", "board_root": str(self.board)})
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_separate_dispatch_and_failure_preserve_task_binding(self):
        import contextlib
        import io
        import os
        from agent_bridge_connect.cli import main
        task = self._create()
        self.assertTrue(self.tasks.preflight(task.id).ok)
        with mock.patch.dict(os.environ, {"OPENCODE_TEST_FAIL": "1"}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["task", "dispatch", task.id, "--root", str(self.board)]), 0, output.getvalue())
            terminal, worker = self._wait(task.id)
        self.assertNotEqual(terminal.status, "completed")
        self.assertEqual(json.loads(self.capture.read_text())["task_id"], task.id)
        self.assertFalse((self.project / "routing-artifact.txt").exists())
        self.assertIn("opencode_exit_nonzero", json.dumps(terminal.to_dict()))

    def test_absent_optional_config_uses_discovery_and_native_model(self):
        from agent_bridge_connect.config import toml_dumps
        config = dict(self.config)
        config.pop("executors")
        self.config_path.write_text(toml_dumps(config))
        task = self._create("--dispatch")
        terminal, worker = self._wait(task.id)
        self.assertEqual(terminal.status, "completed", worker)
        self.assertNotIn("--model", json.loads(self.capture.read_text())["argv"])

    def test_process_start_failure_is_reported_without_completion(self):
        import os
        with mock.patch.dict(os.environ, {"OPENCODE_TEST_REMOVE_ON_PROBE": "1"}):
            task = self._create("--dispatch")
            terminal, worker = self._wait(task.id)
        self.assertNotEqual(terminal.status, "completed")
        self.assertIn("opencode_spawn_failed", json.dumps(terminal.to_dict()))
        self.assertFalse(self.capture.exists())

    def test_unsupported_actions_fail_before_mutation(self):
        from agent_bridge_connect.protocol import ABCError
        task = self._create()
        before = self.tasks.store.read_task(task.id)
        actions = [lambda: self.tasks.resume_task(task.id), lambda: self.tasks.retry_step(task.id, 1),
                   lambda: self.tasks.retry_failed_task(task.id), lambda: self.tasks.requeue_task(task.id),
                   lambda: self.tasks.respond_to_input(task.id, "missing", response_type="approve"),
                   lambda: self.tasks.handoff_task(task.id, "claude"),
                   lambda: self.tasks.handoff_task(task.id, "opencode"),
                   lambda: self.tasks.reassign_task(task.id, "claude")]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ABCError) as raised:
                action()
            self.assertEqual(raised.exception.code, "opencode_action_unsupported")
            self.assertEqual(self.tasks.store.read_task(task.id), before)
        self.assertEqual(self.tasks.retry_preflight(task.id)["allowed_next_actions"], [])
        self.assertFalse(self.capture.exists())

    def test_safe_is_rejected_and_missing_binary_is_diagnosable(self):
        from agent_bridge_connect.executor_registry import get_executor
        task = self._create("--permission-mode", "safe")
        self.assertIn("opencode_permission_mode_unsupported", " ".join(self.tasks.preflight(task.id).errors))
        with mock.patch("agent_bridge_connect.executors.opencode.find_binary", return_value={"found": False}):
            probe = get_executor("opencode", {}).probe()
        self.assertFalse(probe.ok)
        self.assertIn("unavailable", probe.message)
