"""Day 8 tests: AgentBC CLI + Codex executor + Task Brief."""

import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from agent_bridge_connect.adapters import ExecutorLevel
from agent_bridge_connect.executors.opencode import DEFAULT_TIMEOUT_S, OpenCodeExecutor
from agent_bridge_connect.protocol import ABCError
from tests.support.opencode_fakes import (
    jsonl,
    make_fake_opencode,
    make_task_packet,
    parse_capture,
    step_finish_event,
    text_event,
    tool_use_event,
    write_jsonl,
)
from io import StringIO as _cli_StringIO
from pathlib import Path as _cli_Path
import contextlib as _cli_contextlib
import tempfile as _cli_tempfile
import unittest as _cli_unittest
from unittest import mock as _cli_mock
from agent_bridge_connect.cli import _expand_shorthand as _cli__expand_shorthand, build_parser as _cli_build_parser, main as _cli_main
import contextlib as _setup_cli_contextlib
import io as _setup_cli_io
import os as _setup_cli_os
import shutil as _setup_cli_shutil
import tempfile as _setup_cli_tempfile
import unittest as _setup_cli_unittest
from pathlib import Path as _setup_cli_Path
from unittest import mock as _setup_cli_mock

FIXTURES = Path(__file__).parents[1] / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"
_SRC_ROOT = str(Path(__file__).resolve().parents[2] / "src")
_CLI_ENV = {"PYTHONPATH": _SRC_ROOT}


def _cli_run(*args: str):
    """Run the CLI in-process-equivalent: python3 -m with the src tree."""
    import os
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "agent_bridge_connect.cli", *args], capture_output=True, text=True, env=env)


class AgentBCCLITests(unittest.TestCase):
    """Test agentbc CLI entry point and subcommands."""

    def test_agentbc_task_create_list_status(self):
        """Full CLI loop: create → list → status."""
        test_dir = Path(tempfile.mkdtemp())
        workspace = test_dir / "workspace"
        workspace.mkdir()
        config = test_dir / "config.toml"
        config.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
        try:
            _cli_run("init", "--root", str(test_dir))
            r = _cli_run("task", "create", "--title", "CLI test", "--assignee", "mock", "--steps", str(STEPS_YAML), "--root", str(test_dir), "--customer-dir", "true", "--customer-path", str(workspace), "--config", str(config))
            self.assertEqual(r.returncode, 0, f"create failed: {r.stderr}")
            match = re.search("[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001", r.stdout)
            self.assertIsNotNone(match)
            task_id = match.group(0)
            r = _cli_run("task", "list", "--root", str(test_dir))
            self.assertEqual(r.returncode, 0, f"list failed: {r.stderr}")
            self.assertIn(task_id.split("-")[0], r.stdout)
            r = _cli_run("task", "status", task_id, "--root", str(test_dir))
            self.assertEqual(r.returncode, 0, f"status failed: {r.stderr}")
            self.assertIn("pending", r.stdout.lower())
        finally:
            shutil.rmtree(test_dir)


class CodexExecutorTests(unittest.TestCase):
    """Test CodexExecutor adapter."""

    def test_codex_probe(self):
        """CodexExecutor.probe() should check codex is available."""
        from agent_bridge_connect.executors.codex import CodexExecutor

        missing = {"found": False, "path": "", "source": "not_found", "searched_paths": ["fixture/codex"], "manual_override": "AGENTBC_CODEX_BIN=/your/path/codex"}
        with mock.patch("agent_bridge_connect.executors.codex.find_binary", return_value=missing):
            executor = CodexExecutor()
        result = executor.probe()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["agent_bin"], "")
        self.assertEqual(result.details["agent_bin_source"], "not_found")
        self.assertEqual(result.details["searched_paths"], ["fixture/codex"])

    def test_codex_capabilities(self):
        """CodexExecutor should declare L1+ capabilities."""
        from agent_bridge_connect.executors.codex import CodexExecutor
        from agent_bridge_connect.adapters import ExecutorLevel

        executor = CodexExecutor()
        caps = executor.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.streaming_events)
        self.assertGreaterEqual(caps.level, ExecutorLevel.L1)


class TaskBriefTests(unittest.TestCase):
    """Test Task Brief generation."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.test_dir)
        self.task = create_task("Brief test", "mock", STEPS_YAML, self.test_dir)
        self.task_id = self.task.id

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_task_brief_structure(self):
        """Task Brief should contain required fields."""
        from agent_bridge_connect.reports import generate_task_brief

        brief = generate_task_brief(self.task_id, self.test_dir)
        required = ["task_id", "title", "status", "objective", "evidence", "changed_files", "verification", "risks", "available_actions"]
        for field in required:
            self.assertIn(field, brief, f"Missing field: {field}")

    def test_task_brief_has_objective(self):
        """Task Brief objective should come from task title."""
        from agent_bridge_connect.reports import generate_task_brief

        brief = generate_task_brief(self.task_id, self.test_dir)
        self.assertIn("Brief test", brief["objective"])

    def test_task_brief_has_actions(self):
        """Task Brief should list available actions based on current state."""
        from agent_bridge_connect.reports import generate_task_brief

        brief = generate_task_brief(self.task_id, self.test_dir)
        self.assertIsInstance(brief["available_actions"], list)
        self.assertTrue(len(brief["available_actions"]) > 0)


class GateATests(unittest.TestCase):
    """Gate A: Direct CLI can create, inspect, intervene, and accept."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board

        init_board(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_gate_a_full_lifecycle(self):
        """CLI: create → claim → execute → complete → report."""
        from agent_bridge_connect.service import TaskService
        from tests.contract_helpers import finalize_completed

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.test_dir)})
        task = svc.create_task("Gate A test", "mock", [{"id": 1, "description": "Write hello"}], customer_dir=False)
        self.assertEqual(task.status, "pending")
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        task = svc.get_task(task.id)
        self.assertEqual(task.status, "running")
        finalize_completed(svc, task.id)
        task = svc.get_task(task.id)
        self.assertEqual(task.status, "completed")
        report = svc.generate_report(task.id)
        self.assertIsNotNone(report)
        self.assertEqual(report["task_id"], task.id)


class _cli_CliTests(_cli_unittest.TestCase):
    def test_public_help_lists_only_v2_command_groups(self) -> None:
        help_text = _cli_build_parser().format_help()
        for command in ("setup", "doctor", "uninstall", "init", "claude", "hermes", "session", "task", "worker", "runner"):
            self.assertIn(command, help_text)
        for legacy_command in ("submit", "watch", "notify", "_shorthand"):
            self.assertNotIn(legacy_command, help_text)

    def test_task_code_shorthand_routes_to_status(self) -> None:
        self.assertEqual(_cli__expand_shorthand(["4XMC"]), ["task", "status", "4XMC"])
        self.assertEqual(_cli__expand_shorthand(["4XMC-001", "--json"]), ["task", "status", "4XMC-001", "--json"])

    def test_init_command(self) -> None:
        with _cli_tempfile.TemporaryDirectory() as tmp:
            out = _cli_StringIO()
            with _cli_contextlib.redirect_stdout(out):
                code = _cli_main(["init", "--root", str(_cli_Path(tmp) / "runtime")])
            self.assertEqual(code, 0)
            self.assertIn("initialized:", out.getvalue())

    def test_uninstall_command_routes_explicit_data_choices(self) -> None:
        with _cli_mock.patch("agent_bridge_connect.setup.run_uninstall") as uninstall:
            uninstall.return_value = {"ok": True, "mode": "uninstall"}
            code = _cli_main(["uninstall", "--remove-records", "--keep-artifacts"])
        self.assertEqual(code, 0)
        uninstall.assert_called_once_with(interactive=True, remove_records=True, remove_artifacts=False)

    def test_setup_starts_runner_after_writing_config(self) -> None:
        setup_result = {"ok": True, "mode": "setup", "config_path": "/tmp/agentbc-config.toml"}
        runner_result = {"ok": True, "status": "started", "pid": 123}
        with _cli_mock.patch("agent_bridge_connect.setup.run_setup", return_value=setup_result), _cli_mock.patch("agent_bridge_connect.runner.start_runner_background", return_value=runner_result) as start:
            out = _cli_StringIO()
            with _cli_contextlib.redirect_stdout(out):
                code = _cli_main(["setup", "--non-interactive"])
        self.assertEqual(code, 0)
        start.assert_called_once_with(config_path="/tmp/agentbc-config.toml")
        self.assertIn('"status": "started"', out.getvalue())

    def test_desktop_archive_ack_accepts_runner_status_success(self) -> None:
        result = {"ok": True, "task_id": "T2AH-001", "session_id": "01a081be-3e15-7f93-9b1c-2fb032b6c279", "status": "succeeded"}
        with _cli_mock.patch("agent_bridge_connect.runner.RunnerClient.acknowledge_desktop_archive", return_value=result):
            with _cli_contextlib.redirect_stdout(_cli_StringIO()):
                code = _cli_main(["session", "acknowledge-desktop-archive", "T2AH-001", "--session-id", "01a081be-3e15-7f93-9b1c-2fb032b6c279"])
        self.assertEqual(code, 0)

    def test_desktop_archive_ack_rejects_nonterminal_status(self) -> None:
        result = {"ok": True, "task_id": "T2AH-001", "session_id": "01a081be-3e15-7f93-9b1c-2fb032b6c279", "status": "waiting_for_desktop"}
        with _cli_mock.patch("agent_bridge_connect.runner.RunnerClient.acknowledge_desktop_archive", return_value=result):
            with _cli_contextlib.redirect_stdout(_cli_StringIO()):
                code = _cli_main(["session", "acknowledge-desktop-archive", "T2AH-001", "--session-id", "01a081be-3e15-7f93-9b1c-2fb032b6c279"])
        self.assertEqual(code, 1)


class _setup_cli_SetupCommandTests(_setup_cli_unittest.TestCase):
    """Test agentbc setup command."""

    def test_codex_desktop_candidates_include_current_and_legacy_apps(self):
        from agent_bridge_connect.path_provider import _macos_candidates

        candidates = dict(((str(path), source) for (path, source) in _macos_candidates("codex")))
        self.assertEqual(candidates["/Applications/ChatGPT.app/Contents/Resources/codex"], "chatgpt_desktop")
        self.assertEqual(candidates["/Applications/Codex.app/Contents/Resources/codex"], "codex_desktop_legacy")

    def test_setup_discover_codex(self):
        """Setup should discover codex binary path and version."""
        from agent_bridge_connect.setup import discover_codex

        result = discover_codex()
        self.assertIsNotNone(result)
        self.assertIn("path", result)
        self.assertIn("version", result)

    def test_setup_probe_codex_capabilities(self):
        """Setup should probe Codex capabilities."""
        from agent_bridge_connect.setup import probe_codex

        result = probe_codex()
        self.assertIsNotNone(result)
        self.assertIn("has_json_output", result)
        self.assertIn("has_sandbox", result)

    def test_setup_creates_config(self):
        """Setup should create ~/.abc/config.toml with codex executor."""
        from agent_bridge_connect.setup import generate_default_config

        config = generate_default_config()
        self.assertIn("workspace_root", config)
        self.assertIn("executors", config)
        self.assertIn("codex", config["executors"])
        self.assertEqual(config["executors"]["codex"]["type"], "codex")

    def test_discovers_opencode_from_npm_global_home(self):
        """Setup should find npm-installed tools even when they are not on PATH."""
        with _setup_cli_tempfile.TemporaryDirectory() as tmp:
            fake_home = _setup_cli_Path(tmp)
            bin_dir = fake_home / ".npm-global" / "bin"
            bin_dir.mkdir(parents=True)
            opencode = bin_dir / "opencode"
            opencode.write_text("#!/bin/sh\necho opencode 1.2.3\n", encoding="utf-8")
            opencode.chmod(493)
            with _setup_cli_mock.patch.dict(_setup_cli_os.environ, {"HOME": str(fake_home), "PATH": ""}, clear=False):
                from agent_bridge_connect.setup import discover_opencode

                result = discover_opencode()
            self.assertTrue(result["found"])
            self.assertEqual(_setup_cli_Path(result["path"]).resolve(), opencode.resolve())
            self.assertEqual(result["source"], "npm")
            self.assertIn("1.2.3", result["version"])

    def test_command_env_override_for_nonstandard_paths(self):
        """Users should be able to point setup at a manually installed binary."""
        with _setup_cli_tempfile.TemporaryDirectory() as tmp:
            custom = _setup_cli_Path(tmp) / "tools" / "opencode"
            custom.parent.mkdir()
            custom.write_text("#!/bin/sh\necho opencode custom\n", encoding="utf-8")
            custom.chmod(493)
            with _setup_cli_mock.patch.dict(_setup_cli_os.environ, {"AGENTBC_OPENCODE_BIN": str(custom), "PATH": ""}, clear=False):
                from agent_bridge_connect.setup import discover_opencode

                result = discover_opencode()
            self.assertTrue(result["found"])
            self.assertEqual(_setup_cli_Path(result["path"]).resolve(), custom.resolve())
            self.assertEqual(result["source"], "env_override")


class _setup_cli_NaturalLanguageTests(_setup_cli_unittest.TestCase):
    """Test natural-language task operations."""

    def setUp(self):
        self.test_dir = _setup_cli_Path(_setup_cli_tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.test_dir)
        self.task = create_task("NL test", "mock", _setup_cli_Path("tests/fixtures/sample_steps.yaml"), self.test_dir)
        self.task_id = self.task.id

    def tearDown(self):
        _setup_cli_shutil.rmtree(self.test_dir)

    def test_shorthand_create(self):
        """agentbc 'description' should create a task."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.test_dir)})
        task = svc.create_task("Write unit tests", "codex", [{"id": 1, "description": "Write unit tests for auth module"}], customer_dir=False)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.assignee, "codex")

    def test_shorthand_status(self):
        """agentbc <task-id> should show task status."""
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir)
        task = svc.get_task(self.task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.id, self.task_id)

    def test_list_alias_expands_to_task_list(self):
        from agent_bridge_connect.cli import main

        output = _setup_cli_io.StringIO()
        with _setup_cli_contextlib.redirect_stdout(output):
            code = main(["list", "--root", str(self.test_dir)])
        self.assertEqual(code, 0)
        self.assertIn(self.task_id.split("-")[0], output.getvalue())

    def test_single_word_typo_is_rejected_instead_of_creating_task(self):
        from agent_bridge_connect.cli import main
        from agent_bridge_connect.service import TaskService

        before = len(TaskService(self.test_dir).list_tasks())
        output = _setup_cli_io.StringIO()
        with _setup_cli_contextlib.redirect_stdout(output):
            code = main(["satus", "--root", str(self.test_dir)])
        after = len(TaskService(self.test_dir).list_tasks())
        self.assertEqual(code, 1)
        self.assertEqual(after, before)
        self.assertIn("unknown command", output.getvalue().lower())

    def test_context_free_review(self):
        """A new session should be able to review a task by ID only."""
        from agent_bridge_connect.service import TaskService
        from agent_bridge_connect.reports import generate_task_brief

        _service = TaskService(self.test_dir)
        brief = generate_task_brief(self.task_id, self.test_dir)
        self.assertIn("task_id", brief)
        self.assertIn("objective", brief)
        self.assertIn("status", brief)


# ---------------------------------------------------------------------------
# Merged responsibility module (EXEC-105-001-02 repair).
# The OpenCode cases below were folded into this module so the formal test
# file budget stays at its original 75 while every case and assertion is
# preserved verbatim.
# ---------------------------------------------------------------------------


def _marker(
    task_id: str,
    *,
    final_state: str = "completed",
    step_ids: tuple[int, ...] = (1,),
) -> str:
    step_results = [
        {"id": step_id, "status": "done"} for step_id in step_ids
    ]
    payload = {
        "version": 1,
        "task_id": task_id,
        "final_state": final_state,
        "summary": "fake run finished",
        "step_results": step_results,
    }
    return "AGENTBC_FINAL_CALLBACK: " + json.dumps(payload, ensure_ascii=False)


class _OpenCodeAdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="opencode-adapter-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = self.tmp / "工程 中文 目录"
        self.project.mkdir()
        self.board = self.tmp / "board"
        self.board.mkdir()
        self.fake = make_fake_opencode(self.tmp)
        self.capture = self.tmp / "capture.txt"
        self.stdout_file = self.tmp / "stdout.jsonl"

    def executor(self, **kwargs: object) -> OpenCodeExecutor:
        kwargs.setdefault("command", str(self.fake))
        return OpenCodeExecutor(**kwargs)  # type: ignore[arg-type]

    def packet(self, **kwargs: object) -> dict:
        kwargs.setdefault("board", self.board)
        return make_task_packet(self.project, **kwargs)  # type: ignore[arg-type]

    def run_adapter(
        self, packet: dict, env: dict[str, str], executor: OpenCodeExecutor
    ) -> object:
        merged = {"OPENCODE_FAKE_CAPTURE": str(self.capture)}
        merged.update(env)
        with mock.patch.dict(os.environ, merged, clear=False):
            return executor.start(packet)

    def captured(self) -> dict:
        return parse_capture(self.capture)

    def assert_arg_after(self, captured: dict, flag: str, value: str) -> None:
        args = captured["args"]
        self.assertIn(flag, args)
        index = args.index(flag)
        self.assertLess(index + 1, len(args))
        self.assertEqual(args[index + 1], value)


class OpenCodeCommandContractTests(_OpenCodeAdapterCase):
    def test_explicit_dir_cwd_and_pwd_share_one_absolute_project_path(self) -> None:
        other = self.tmp / "not-the-project"
        other.mkdir()
        result = self.run_adapter(
            self.packet(),
            {"PWD": str(other), "OPENCODE_FAKE_EXIT": "0"},
            self.executor(),
        )
        self.assertTrue(result.ok)
        project = str(self.project.resolve())
        captured = self.captured()
        self.assert_arg_after(captured, "--dir", project)
        self.assertEqual(captured["PWD"], project)
        self.assertEqual(captured["CWD"], project)

    def test_parent_pwd_is_never_mutated(self) -> None:
        other = self.tmp / "parent-pwd"
        other.mkdir()
        with mock.patch.dict(
            os.environ, {"PWD": str(other), "OPENCODE_FAKE_CAPTURE": str(self.capture)}
        ):
            self.executor().start(self.packet())
            self.assertEqual(os.environ["PWD"], str(other))

    def test_multiline_chinese_prompt_is_one_argv_element(self) -> None:
        packet = self.packet(title="中文 任务：实现最小适配器")
        result = self.run_adapter(packet, {}, self.executor())
        self.assertTrue(result.ok)
        args = self.captured()["args"]
        self.assertEqual(args.count("--"), 1)
        separator = args.index("--")
        self.assertEqual(separator + 1, len(args) - 1)
        prompt = args[separator + 1]
        self.assertIn("中文 任务", prompt)
        self.assertIn("\n", prompt)
        self.assertIn("AGENTBC_FINAL_CALLBACK", prompt)
        self.assertNotIn("agentbc task progress", prompt)
        self.assertIn("submits step results only in the final callback", prompt)
        self.assertIn("AgentBC Core generates the report", prompt)
        self.assertIn("cannot resume approvals or elevate permissions", prompt)
        self.assertNotIn("requested_permission full", prompt)

    def test_dash_leading_prompt_stays_after_the_separator(self) -> None:
        executor = self.executor()
        prompt = "- 第一行\n--not-a-flag\n- done"
        command = executor._build_command(self.project, prompt, "inherit")
        self.assertEqual(command[-1], prompt)
        separator = command.index("--")
        self.assertEqual(separator + 1, len(command) - 1)
        self.assertNotIn("--not-a-flag", command[:separator])

    def test_explicit_model_is_passed_and_default_is_not_guessed(self) -> None:
        with_model = self.executor(model="deepseek/deepseek-v4-flash")
        args = with_model._build_command(self.project, "p", "inherit")
        self.assert_arg_after({"args": args}, "--model", "deepseek/deepseek-v4-flash")
        without_model = self.executor()
        self.assertNotIn("--model", without_model._build_command(self.project, "p", "inherit"))

    def test_full_mode_fixes_the_build_agent_and_never_uses_auto(self) -> None:
        command = self.executor()._build_command(self.project, "p", "full")
        self.assert_arg_after({"args": command}, "--agent", "build")
        self.assertNotIn("--auto", command)

    def test_timeout_default_and_type_diagnostics(self) -> None:
        self.assertEqual(self.executor().timeout_s, float(DEFAULT_TIMEOUT_S))
        for bad in (0, -1, True, "600"):
            with self.subTest(bad=bad):
                with self.assertRaises(ABCError) as caught:
                    OpenCodeExecutor(command=str(self.fake), timeout_s=bad)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "opencode_argument_invalid")
        with self.assertRaises(ABCError) as caught:
            OpenCodeExecutor(command=str(self.fake), model=123)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "opencode_argument_invalid")


class OpenCodePermissionEnvironmentTests(_OpenCodeAdapterCase):
    def test_inherit_leaves_the_native_permission_environment_alone(self) -> None:
        existing = '{"theme": "dark"}'
        result = self.run_adapter(
            self.packet(mode="inherit"),
            {"OPENCODE_CONFIG_CONTENT": existing},
            self.executor(),
        )
        self.assertTrue(result.ok)
        captured = self.captured()
        self.assertEqual(captured["CFG"], existing)
        self.assertIsNone(captured["PERM"])

    def test_full_overrides_global_build_and_granular_denies(self) -> None:
        rules = [
            {"permission": "edit", "pattern": "*", "action": "deny"},
            {"permission": "bash", "pattern": "*", "action": "deny"},
            {"permission": "bash", "pattern": "printf *", "action": "deny"},
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
            {"permission": "read", "pattern": "*", "action": "allow"},
        ]
        existing = json.dumps(
            {
                "theme": "dark",
                "permission": {"edit": "deny"},
                "agent": {
                    "plan": {"mode": "readonly"},
                    "build": {"temperature": 1},
                },
            }
        )
        result = self.run_adapter(
            self.packet(mode="full"),
            {
                "OPENCODE_CONFIG_CONTENT": existing,
                "OPENCODE_FAKE_RULES": json.dumps({"permission": rules}),
            },
            self.executor(),
        )
        self.assertTrue(result.ok, result.message)
        captured = self.captured()
        args = captured["args"]
        self.assert_arg_after(captured, "--agent", "build")
        self.assertNotIn("--auto", args)
        config = json.loads(captured["CFG"])
        permissions = config["permission"]
        for permission, pattern in (
            ("edit", "*"),
            ("bash", "*"),
            ("bash", "printf *"),
            ("external_directory", "*"),
        ):
            self.assertEqual(permissions[permission][pattern], "allow")
        self.assertEqual(permissions["*"], "allow")
        for key in ("doom_loop", "question", "plan_enter", "plan_exit", "todoread", "todowrite"):
            self.assertEqual(permissions[key], "allow")
        self.assertEqual(config["theme"], "dark")
        self.assertEqual(config["agent"]["plan"], {"mode": "readonly"})
        self.assertEqual(config["agent"]["build"]["temperature"], 1)
        self.assertEqual(config["agent"]["build"]["permission"], permissions)
        self.assertEqual(json.loads(captured["PERM"]), permissions)

    def test_full_environment_is_process_scoped(self) -> None:
        existing = '{"theme": "dark"}'
        with mock.patch.dict(
            os.environ,
            {
                "OPENCODE_CONFIG_CONTENT": existing,
                "OPENCODE_FAKE_RULES": json.dumps({"permission": []}),
            },
        ):
            self.executor().start(self.packet(mode="full"))
            self.assertEqual(os.environ["OPENCODE_CONFIG_CONTENT"], existing)
            self.assertNotIn("OPENCODE_PERMISSION", os.environ)

    def test_invalid_config_content_reports_a_stable_diagnostic(self) -> None:
        for value in ("{not json", "[]", '"string"'):
            with self.subTest(value=value):
                result = self.run_adapter(
                    self.packet(mode="full"),
                    {
                        "OPENCODE_CONFIG_CONTENT": value,
                        "OPENCODE_FAKE_RULES": json.dumps({"permission": []}),
                    },
                    self.executor(),
                )
                self.assertFalse(result.ok)
                self.assertTrue(
                    result.message.startswith("opencode_config_invalid:"),
                    result.message,
                )

    def test_official_rule_discovery_failure_is_stable(self) -> None:
        result = self.run_adapter(
            self.packet(mode="full"),
            {"OPENCODE_FAKE_RULES": "not-json"},
            self.executor(),
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            result.message.startswith("opencode_full_rules_invalid:"),
            result.message,
        )
        rejected = self.run_adapter(
            self.packet(mode="full"),
            {"OPENCODE_FAKE_RULES": json.dumps({"permission": []}), "OPENCODE_FAKE_DEBUG_RC": "3"},
            self.executor(),
        )
        self.assertFalse(rejected.ok)
        self.assertTrue(
            rejected.message.startswith("opencode_full_rules_unavailable:"),
            rejected.message,
        )

    def test_safe_and_resume_are_refused_without_approximation(self) -> None:
        safe = self.run_adapter(self.packet(mode="safe"), {}, self.executor())
        self.assertFalse(safe.ok)
        self.assertTrue(
            safe.message.startswith("opencode_permission_mode_unsupported:")
        )
        resume_packet = self.packet()
        resume_packet["resume"] = True
        resume = self.run_adapter(resume_packet, {}, self.executor())
        self.assertFalse(resume.ok)
        self.assertTrue(
            resume.message.startswith("opencode_action_unsupported:")
        )


class OpenCodeResultContractTests(_OpenCodeAdapterCase):
    def _stdout_env(self, events: list[dict]) -> dict[str, str]:
        write_jsonl(self.stdout_file, events)
        return {"OPENCODE_FAKE_STDOUT_FILE": str(self.stdout_file)}

    def test_empty_output_fails_without_a_callback(self) -> None:
        executor = self.executor()
        result = executor.start(self.packet())
        self.assertTrue(result.ok)
        poll = executor.poll(result.run_id)
        self.assertEqual(poll.status, "failed")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(
            poll.result["failure"]["kind"], "completion_marker_missing"
        )
        self.assertFalse(poll.result["auto_rejected"])
        self.assertTrue(poll.result["summary"])

    def test_auto_rejected_tool_call_is_not_success(self) -> None:
        events = [
            {"type": "step_start", "sessionID": "ses-main"},
            tool_use_event(
                status="error", error={"message": "permission auto-rejecting"}
            ),
            step_finish_event("tool-calls"),
        ]
        executor = self.executor()
        result = self.run_adapter(self.packet(), self._stdout_env(events), executor)
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.status, "failed")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertTrue(poll.result["auto_rejected"])
        self.assertEqual(poll.result["tool_error_count"], 1)
        self.assertFalse(poll.result["stop_finish_seen"])
        self.assertEqual(poll.result["returncode"], 0)

    def test_valid_callback_completes_the_run(self) -> None:
        events = [
            {"type": "step_start", "sessionID": "ses-main"},
            text_event(_marker("FAKE-001")),
            step_finish_event("stop"),
        ]
        executor = self.executor()
        result = self.run_adapter(
            self.packet(), self._stdout_env(events), executor
        )
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.status, "completed")
        callback = poll.result["agent_callback"]
        self.assertIsNotNone(callback)
        self.assertEqual(callback["task_id"], "FAKE-001")
        self.assertEqual(callback["final_state"], "completed")

    def test_stdout_and_stderr_are_split_and_raw_text_is_not_projected(self) -> None:
        events = [text_event(_marker("FAKE-001")), step_finish_event("stop")]
        write_jsonl(self.stdout_file, events)
        executor = self.executor()
        result = self.run_adapter(
            self.packet(),
            {
                "OPENCODE_FAKE_STDOUT_FILE": str(self.stdout_file),
                "OPENCODE_FAKE_STDERR": "noise on stderr",
            },
            executor,
        )
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.status, "completed")
        self.assertGreater(poll.result["stderr_bytes"], 0)
        self.assertNotIn("noise on stderr", json.dumps(poll.result))

    def test_duplicate_callback_fails_closed(self) -> None:
        events = [
            text_event(_marker("FAKE-001") + "\n" + _marker("FAKE-001")),
            step_finish_event("stop"),
        ]
        executor = self.executor()
        result = self.run_adapter(
            self.packet(), self._stdout_env(events), executor
        )
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.status, "failed")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(
            poll.result["failure"]["kind"], "completion_marker_duplicate"
        )

    def test_main_session_is_bound_and_child_session_is_separate(self) -> None:
        events = [
            {"type": "step_start", "sessionID": "ses-main"},
            tool_use_event(
                status="completed", child_session_id="ses-child"
            ),
            text_event("working", session_id="ses-main"),
            step_finish_event("stop"),
        ]
        executor = self.executor()
        result = self.run_adapter(
            self.packet(), self._stdout_env(events), executor
        )
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.result["session_id"], "ses-main")
        self.assertEqual(poll.result["session_ids"], ["ses-main"])
        self.assertEqual(poll.result["child_session_count"], 1)
        self.assertNotIn("ses-child", poll.result["session_ids"])

    def test_timeout_reaps_its_own_group_and_reports_authoritative_exit(self) -> None:
        executor = self.executor(timeout_s=0.4)
        result = self.run_adapter(
            self.packet(), {"OPENCODE_FAKE_SLEEP": "3"}, executor
        )
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.status, "failed")
        self.assertTrue(poll.result["timed_out"])
        self.assertTrue(poll.result["cleanup_confirmed"])
        self.assertIsNotNone(poll.result["returncode"])
        self.assertLess(poll.result["duration_s"], 3.0)
        self.assertEqual(poll.result["failure"]["kind"], "opencode_timeout")

    def test_nonzero_exit_fails_without_a_callback(self) -> None:
        executor = self.executor()
        result = self.run_adapter(
            self.packet(), {"OPENCODE_FAKE_EXIT": "2"}, executor
        )
        poll = executor.poll(result.run_id)  # type: ignore[union-attr]
        self.assertEqual(poll.status, "failed")
        self.assertEqual(poll.result["returncode"], 2)
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(
            poll.result["failure"]["kind"], "opencode_exit_nonzero"
        )

    def test_unknown_run_and_run_lease_lifecycle(self) -> None:
        executor = self.executor()
        unknown = executor.poll("opencode-does-not-exist")
        self.assertEqual(unknown.status, "failed")
        self.assertIn("unknown run_id", unknown.result["error"])
        result = self.run_adapter(self.packet(), {}, executor)
        self.assertTrue(result.ok)
        run_id = result.run_id  # type: ignore[union-attr]
        self.assertIn(run_id, executor._runs)
        lease = executor._run_leases[run_id]
        self.assertEqual(lease.state, "closed")
        self.assertEqual(lease.executor_id, "opencode")
        self.assertEqual(lease.work_dir, str(self.project.resolve()))
        leases = list(self.board.rglob("run_lease.json"))
        self.assertEqual(len(leases), 1)
        self.assertIn(str(self.project.resolve()), leases[0].read_text("utf-8"))


class OpenCodeProbeAndCapabilityTests(_OpenCodeAdapterCase):
    def test_capabilities_are_l0_with_only_authenticated_flags(self) -> None:
        caps = self.executor().capabilities()
        self.assertEqual(caps.level, ExecutorLevel.L0)
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.model_selection)
        for flag in (
            "streaming_events",
            "resume",
            "cancel",
            "input_required",
            "multimodal",
            "image_input",
            "image_generation",
            "image_editing",
        ):
            with self.subTest(flag=flag):
                self.assertFalse(getattr(caps, flag))

    def test_probe_uses_version_only_and_never_runs_a_model(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "AGENTBC_OPENCODE_BIN": str(self.fake),
                "OPENCODE_FAKE_CAPTURE": str(self.capture),
            },
        ):
            probe = OpenCodeExecutor().probe()
        self.assertTrue(probe.ok)
        self.assertIn("1.18.31", probe.message)
        captured = self.captured()
        self.assertEqual(captured["args"], ["--version"])
        self.assertNotIn("run", captured["args"])

    def test_probe_reports_missing_binary_without_spawning(self) -> None:
        missing = {
            "found": False,
            "path": "",
            "source": "not_found",
            "searched_paths": ["fixture/opencode"],
            "manual_override": "AGENTBC_OPENCODE_BIN=/your/path/opencode",
        }
        with mock.patch(
            "agent_bridge_connect.executors.opencode.find_binary",
            return_value=missing,
        ):
            probe = OpenCodeExecutor().probe()
        self.assertFalse(probe.ok)
        self.assertEqual(probe.details["agent_bin_source"], "not_found")
        self.assertTrue(probe.message)


class OpenCodeFinalTurnAndRecoveryRepairTests(_OpenCodeAdapterCase):
    """R1a/R1b: the final turn owns its stop and its own recovery proof."""

    def _poll(self, events: list, tail: str = "") -> object:
        execution = {"stdout": jsonl(events) + tail, "stderr": "", "returncode": 0,
                     "cleanup_confirmed": True, "readers_ended": True}
        return self.executor()._build_poll_result(
            self.packet(), "repair-run", "inherit", execution)

    def test_history_stop_cannot_finish_a_later_unfinished_turn(self):
        poll = self._poll([step_finish_event("stop"),
                           {"type": "step_start", "sessionID": "ses-main"},
                           text_event(_marker("FAKE-001")),
                           step_finish_event("tool-calls")])
        self.assertFalse(poll.result["final_stop_seen"])
        self.assertNotEqual(poll.status, "completed")
        self.assertEqual(poll.result["failure"]["kind"],
                         "opencode_stop_boundary_missing")

    def test_corrupt_tail_after_valid_callback_is_not_completed(self):
        poll = self._poll([text_event(_marker("FAKE-001")),
                           step_finish_event("stop")], tail='{"type":')
        self.assertEqual(poll.result["parse_diagnostics"]["malformed_lines"], 1)
        self.assertNotEqual(poll.status, "completed")
        self.assertEqual(poll.result["failure"]["kind"], "opencode_stream_corrupt")

    def test_multi_turn_with_intermediate_tool_calls_completes(self):
        poll = self._poll([{"type": "step_start", "sessionID": "ses-main"},
                           tool_use_event(status="completed"),
                           step_finish_event("tool-calls"),
                           {"type": "step_start", "sessionID": "ses-main"},
                           text_event(_marker("FAKE-001")),
                           step_finish_event("stop")])
        self.assertTrue(poll.result["final_stop_seen"])
        self.assertEqual(poll.status, "completed")

    def test_only_matching_completed_tool_recovers_an_error(self):
        cases = (("running", "bash", 1), ("pending", "bash", 1), ("", "bash", 1),
                 ("completed", "read", 1), ("completed", "bash", 0))
        for status, tool, expected in cases:
            with self.subTest(status=status, tool=tool):
                poll = self._poll([
                    tool_use_event(status="error", error="permission denied", tool="bash"),
                    tool_use_event(status=status, tool=tool),
                    text_event(_marker("FAKE-001")),
                    step_finish_event("stop")])
                self.assertEqual(poll.result["unrecovered_tool_error_count"], expected)
                self.assertEqual(poll.status == "completed", expected == 0)

    def test_unknown_extra_event_after_stop_is_compatible(self):
        poll = self._poll([text_event(_marker("FAKE-001")),
                           step_finish_event("stop"),
                           {"type": "future.event", "sessionID": "ses-main", "extra": 1}])
        self.assertEqual(poll.status, "completed")


class OpenCodeProcessGroupRepairTests(_OpenCodeAdapterCase):
    """R3: a normal exit still verifies the owned process group and readers."""

    def _worker_fake(self, *, hold_pipe: bool, ignore_term: bool = False) -> Path:
        child_out = "None" if hold_pipe else "subprocess.DEVNULL"
        child_code = "import signal,time" + (
            ";signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else ""
        ) + ";time.sleep(30)"
        payload = jsonl([text_event(_marker("FAKE-001")), step_finish_event("stop")])
        fake = self.tmp / "fake-worker"
        fake.write_text(
            "#!" + sys.executable + "\n"
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', " + repr(child_code) +
            "], stdout=" + child_out + ", stderr=" + child_out + ")\n"
            "Path(" + repr(str(self.tmp / "worker.pid")) +
            ").write_text(str(child.pid))\n"
            "print(" + repr(payload) + ", end='', flush=True)\n",
            encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def _assert_reclaimed(self, fake: Path) -> None:
        executor = self.executor(command=str(fake), timeout_s=5)
        result = executor.start(self.packet())
        self.assertTrue(result.ok, result.message)
        poll = executor.poll(result.run_id)
        pid = int((self.tmp / "worker.pid").read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("owned child survived the bounded reclaim")
        self.assertEqual(poll.status, "completed")
        self.assertTrue(poll.result["cleanup_confirmed"])
        self.assertTrue(poll.result["readers_ended"])
        self.assertEqual(executor._run_leases[result.run_id].state, "closed")

    def test_live_child_is_reclaimed_for_both_stdio_modes(self):
        for hold_pipe in (False, True):
            with self.subTest(hold_pipe=hold_pipe):
                self._assert_reclaimed(self._worker_fake(hold_pipe=hold_pipe))

    def test_child_ignoring_term_is_killed(self):
        fake = self._worker_fake(hold_pipe=True, ignore_term=True)
        with mock.patch("agent_bridge_connect.executors.opencode.TERMINATE_GRACE_S", 0.05):
            self._assert_reclaimed(fake)

    def test_normal_run_without_children_closes_cleanly(self):
        write_jsonl(self.stdout_file, [text_event(_marker("FAKE-001")),
                                       step_finish_event("stop")])
        executor = self.executor()
        result = self.run_adapter(self.packet(),
                                  {"OPENCODE_FAKE_STDOUT_FILE": str(self.stdout_file)},
                                  executor)
        poll = executor.poll(result.run_id)
        self.assertEqual(poll.status, "completed")
        self.assertTrue(poll.result["cleanup_confirmed"])
        self.assertEqual(executor._run_leases[result.run_id].state, "closed")

    def test_unreclaimed_group_is_never_reported_as_clean(self):
        fake = self._worker_fake(hold_pipe=True)
        pidfile = self.tmp / "worker.pid"
        executor = self.executor(command=str(fake), timeout_s=5)
        try:
            with mock.patch.object(OpenCodeExecutor, "_reclaim_owned_group",
                                   return_value=False), mock.patch(
                    "agent_bridge_connect.executors.opencode.TERMINATE_GRACE_S", 0.05):
                result = executor.start(self.packet())
            poll = executor.poll(result.run_id)
            self.assertNotEqual(poll.status, "completed")
            self.assertFalse(poll.result["cleanup_confirmed"])
            self.assertFalse(poll.result["process_stopped"])
            self.assertFalse(poll.result["readers_ended"])
            self.assertEqual(executor._run_leases[result.run_id].state, "stale")
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass


if __name__ == "__main__":
    unittest.main()
