"""Targeted tests for the Claude stream/control permission-prompt broker."""

from __future__ import annotations
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from agent_bridge_connect.executors.claude import PERMISSION_PROMPT_TOOL_FLAG, ClaudeExecutor, ClaudePermissionPromptBroker, _parse_stream_json_line
from agent_bridge_connect.permission_runtime import PERMISSION_TRANSPORT_UNSUPPORTED
from agent_bridge_connect.protocol import ABCError
import json as _claude_executor_json
import subprocess as _claude_executor_subprocess
import sys as _claude_executor_sys
import tempfile as _claude_executor_tempfile
import unittest as _claude_executor_unittest
import uuid as _claude_executor_uuid
from pathlib import Path as _claude_executor_Path
from unittest import mock as _claude_executor_mock
from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX as _claude_executor_FINAL_CALLBACK_PREFIX
from agent_bridge_connect.executors.claude import ClaudeExecutor as _claude_executor_ClaudeExecutor
import json as _claude_path_json
import subprocess as _claude_path_subprocess
import sys as _claude_path_sys
import tempfile as _claude_path_tempfile
import unittest as _claude_path_unittest
import uuid as _claude_path_uuid
from pathlib import Path as _claude_path_Path
from unittest import mock as _claude_path_mock
from agent_bridge_connect.claude_path_capability import (
    assert_claude_path_capability_command as _claude_path_assert_claude_path_capability_command,
    assert_claude_path_capability_supported as _claude_path_assert_claude_path_capability_supported,
    claude_ephemeral_path_capability as _claude_path_claude_ephemeral_path_capability,
)
from agent_bridge_connect.executors.claude import ClaudeExecutor as _claude_path_ClaudeExecutor
from agent_bridge_connect.path_model import build_path_plan as _claude_path_build_path_plan
from agent_bridge_connect.permission_modes import build_permission_record as _claude_path_build_permission_record, validate_permission_command as _claude_path_validate_permission_command
from agent_bridge_connect.permission_registry import probe_executor_capability as _claude_path_probe_executor_capability
from agent_bridge_connect.protocol import ABCError as _claude_path_ABCError

RUN_ID = "claude-ABCD-001-run1"


def _can_use_tool_event(**overrides: str) -> dict:
    payload = {"type": "can_use_tool", "session_id": "019feed0-0000-7000-8000-0000000000aa", "tool_name": "Bash", "tool_input": {"command": "ls"}, "user_prompt": "list files"}
    payload.update(overrides)
    return payload


class ClaudePermissionBrokerTests(unittest.TestCase):
    def test_allow_only_returns_allow(self) -> None:
        broker = ClaudePermissionPromptBroker(session_id="019feed0-0000-7000-8000-0000000000aa", decision_callback=lambda request: {"permission": "allow"})
        line = '{"type":"can_use_tool","tool_name":"Bash"}'
        response = broker.handle_request_line(line)
        self.assertEqual(response, '{"permission": "allow"}')

    def test_deny_only_returns_deny(self) -> None:
        broker = ClaudePermissionPromptBroker(session_id="019feed0-0000-7000-8000-0000000000aa", decision_callback=lambda request: {"permission": "deny"})
        response = broker.handle_request_line('{"tool_name":"Bash"}')
        self.assertEqual(response, '{"permission": "deny"}')

    def test_never_applies_updated_permissions(self) -> None:
        broker = ClaudePermissionPromptBroker(session_id="019feed0-0000-7000-8000-0000000000aa", decision_callback=lambda request: {"permission": "allow", "updated_permissions": ["Write"]})
        response = broker.handle_request_line('{"tool_name":"Write"}')
        self.assertEqual(response, '{"permission": "allow"}')
        self.assertNotIn("updated_permissions", response)

    def test_invalid_json_is_denied(self) -> None:
        broker = ClaudePermissionPromptBroker(session_id="019feed0-0000-7000-8000-0000000000aa", decision_callback=lambda request: {"permission": "allow"})
        response = broker.handle_request_line("not-json")
        self.assertTrue(response.startswith('{"permission": "deny"'))
        self.assertNotIn("allow", response)

    def test_init_receipt_verification(self) -> None:
        broker = ClaudePermissionPromptBroker(session_id="019feed0-0000-7000-8000-0000000000aa", decision_callback=lambda request: {"permission": "deny"})
        self.assertTrue(broker._verify_init_receipt({"type": "system", "subtype": "init", "session_id": "019feed0-0000-7000-8000-0000000000aa"}))
        self.assertFalse(broker._verify_init_receipt({"type": "system", "subtype": "init", "session_id": "other"}))

    def test_stream_line_parser(self) -> None:
        parsed = _parse_stream_json_line('{"type":"init"}')
        self.assertEqual(parsed, {"type": "init"})
        self.assertIsNone(_parse_stream_json_line("garbage"))
        self.assertIsNone(_parse_stream_json_line(""))


class ClaudeControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "customer"
        self.project.mkdir()
        self.claude_project = self.root / "workspace" / "tasks" / "artifacts" / "2026-08-10" / "ABCD" / "ABCD-001" / "claude"
        self.session_id = str(uuid.uuid4())

    def _packet(self) -> dict:
        return {
            "task_id": "ABCD-001",
            "title": "control path",
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {
                "customer_dir": True,
                "customer_path": str(self.project),
                "root": str(self.project),
                "project_root": str(self.project),
                "default_path": str(self.project),
                "executor_project_root": str(self.claude_project),
                "agentbc_root": str(self.root / "workspace"),
                "artifact_root": str(self.project),
                "report_root": str(self.root / "workspace" / "tasks" / "report"),
                "task_file": str(self.root / "workspace" / "tasks" / "task.md"),
                "report_file": str(self.root / "workspace" / "tasks" / "report.md"),
                "task_code": "ABCD",
                "iteration": "001",
                "task_date": "2026-08-10",
            },
            "task_board": {"root": str(self.root / "board")},
            "extensions": {
                "agentbc.resources": {"version": 1, "executor": "claude", "resource": "max_budget_usd", "current_limit": 2.5},
                "agentbc.session": {"version": 1, "executor": "claude", "retain": False, "session_id": self.session_id, "session_state": "pending", "project_mode": "ephemeral", "project_path": str(self.claude_project), "run_ids": []},
                "agentbc.permission": {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "configured_default"},
            },
        }

    @mock.patch("agent_bridge_connect.executors.claude.ClaudeExecutor.supports_permission_prompt_tool")
    def test_control_command_never_fabricates_a_prompt_tool_broker(self, supports: mock.Mock) -> None:
        supports.return_value = True
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        broker = ClaudePermissionPromptBroker(session_id=self.session_id, decision_callback=lambda request: {"permission": "deny"})
        with self.assertRaises(ABCError) as raised:
            executor._build_control_command("prompt", self.project, self._packet(), {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "x"}, broker)
        self.assertEqual(raised.exception.code, PERMISSION_TRANSPORT_UNSUPPORTED)

    @mock.patch("agent_bridge_connect.executors.claude.ClaudeExecutor.supports_permission_prompt_tool")
    def test_control_command_without_flag_uses_local_broker(self, supports: mock.Mock) -> None:
        supports.return_value = False
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        broker = ClaudePermissionPromptBroker(session_id=self.session_id, decision_callback=lambda request: {"permission": "deny"})
        command = executor._build_control_command("prompt", self.project, self._packet(), {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "x"}, broker)
        self.assertEqual(command[command.index("--session-id") + 1], self.session_id)
        self.assertNotIn(PERMISSION_PROMPT_TOOL_FLAG, command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self.buffer: list[str] = []

    def write(self, data: str) -> int:
        if self.process.broken_pipe and '"permission"' in data:
            raise BrokenPipeError("broken pipe")
        self.buffer.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self._lines = list(process.stdout_lines)
        self._index = 0

    def __iter__(self) -> "_FakeStdout":
        return self

    def __next__(self) -> str:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line if line.endswith("\n") else line + "\n"
        raise StopIteration

    def close(self) -> None:
        pass


class _FakeStderr:
    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = list(lines or [])
        self._index = 0

    def __iter__(self) -> "_FakeStderr":
        return self

    def __next__(self) -> str:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line if line.endswith("\n") else line + "\n"
        raise StopIteration

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], *, die_after_decision: bool = False, broken_pipe: bool = False, returncode: int = 0) -> None:
        self.stdout_lines = list(stdout_lines)
        self.die_after_decision = die_after_decision
        self.broken_pipe = broken_pipe
        self._returncode = returncode
        self.decisions = 0
        self.returncode: int | None = None
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.stderr = _FakeStderr()

    def poll(self) -> int | None:
        if self.die_after_decision and self.decisions >= 1:
            return self._returncode or 1
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._returncode
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class ClaudeBrokerTransportDeathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.session_id = "019feed0-0000-7000-8000-0000000000aa"
        self.init_line = f'{{"type":"system","subtype":"init","session_id":"{self.session_id}"}}'
        self.can_use_tool_line = '{"type":"can_use_tool","tool_name":"Bash","tool_input":{"command":"ls"}}'

    def _run(self, process: _FakeProcess, *, decision: dict) -> dict:

        def decision_callback(request: dict) -> dict:
            process.decisions += 1
            return decision

        broker = ClaudePermissionPromptBroker(session_id=self.session_id, decision_callback=decision_callback, transport_death_callback=lambda request_id: self.deaths.append(request_id))
        with mock.patch("agent_bridge_connect.executors.claude.subprocess.Popen", return_value=process):
            return broker.run_controlled(command=["claude", "-p"], cwd=self.root, timeout_s=10)

    def test_transport_death_invalidates_request(self) -> None:
        self.deaths: list[str] = []
        process = _FakeProcess([self.init_line, self.can_use_tool_line], die_after_decision=True, returncode=1)
        result = self._run(process, decision={"permission": "allow", "request_id": "approval-x"})
        self.assertTrue(result["transport_death_while_approval"])
        self.assertEqual(result["aborted_request_id"], "approval-x")
        self.assertEqual(self.deaths, ["approval-x"])
        self.assertTrue(result["init_verified"])
        self.assertEqual(process.returncode, 1)
        self.assertNotIn('"permission": "allow"', "".join(process.stdin.buffer))

    def test_transport_survives_and_response_is_delivered(self) -> None:
        self.deaths: list[str] = []
        process = _FakeProcess([self.init_line, self.can_use_tool_line], die_after_decision=False, returncode=0)
        result = self._run(process, decision={"permission": "allow", "request_id": "approval-y"})
        self.assertFalse(result["transport_death_while_approval"])
        self.assertEqual(result["aborted_request_id"], "")
        self.assertEqual(self.deaths, [])
        self.assertEqual(result["returncode"], 0)
        self.assertTrue('"permission": "allow"' in "".join(process.stdin.buffer))

    def test_broken_pipe_invalidates_request(self) -> None:
        self.deaths: list[str] = []
        process = _FakeProcess([self.init_line, self.can_use_tool_line], die_after_decision=False, broken_pipe=True, returncode=1)
        result = self._run(process, decision={"permission": "allow", "request_id": "approval-z"})
        self.assertTrue(result["transport_death_while_approval"])
        self.assertEqual(result["aborted_request_id"], "approval-z")
        self.assertEqual(self.deaths, ["approval-z"])


class _claude_executor_Phase3ClaudeExecutorTests(_claude_executor_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _claude_executor_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _claude_executor_Path(self.temporary.name).resolve()
        self.project = self.root / "customer"
        self.project.mkdir()
        self.claude_project = self.root / "workspace" / "tasks" / "artifacts" / "2026-08-10" / "ABCD" / "ABCD-001" / "claude"
        self.session_id = str(_claude_executor_uuid.uuid4())

    def _packet(self, *, retain: bool = False, run_ids: list[str] | None = None, budget: float = 2.5) -> dict:
        project_path = self.project if retain else self.claude_project
        return {
            "task_id": "ABCD-001",
            "title": "phase three Claude",
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {
                "customer_dir": True,
                "customer_path": str(self.project),
                "root": str(self.project),
                "project_root": str(self.project),
                "default_path": str(self.project),
                "executor_project_root": str(self.claude_project),
                "agentbc_root": str(self.root / "workspace"),
                "artifact_root": str(self.project),
                "report_root": str(self.root / "workspace" / "tasks" / "report"),
                "task_file": str(self.root / "workspace" / "tasks" / "task.md"),
                "report_file": str(self.root / "workspace" / "tasks" / "report.md"),
                "task_code": "ABCD",
                "iteration": "001",
                "task_date": "2026-08-10",
            },
            "task_board": {"root": str(self.root / "board")},
            "extensions": {
                "agentbc.resources": {"version": 1, "executor": "claude", "resource": "max_budget_usd", "current_limit": budget},
                "agentbc.session": {"version": 1, "executor": "claude", "retain": retain, "session_id": self.session_id, "session_state": "pending", "project_mode": "native" if retain else "ephemeral", "project_path": str(project_path), "run_ids": list(run_ids or [])},
            },
        }

    @staticmethod
    def _completed_output() -> str:
        callback = {"version": 1, "task_id": "ABCD-001", "final_state": "completed", "summary": "done", "step_results": [{"id": 1, "status": "done"}]}
        return f"{_claude_executor_FINAL_CALLBACK_PREFIX} {_claude_executor_json.dumps(callback)}\n"

    def test_capability_and_constructor_default_enable_persistent_sessions(self) -> None:
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct")
        self.assertTrue(executor.capabilities().resume)
        self.assertEqual(executor.max_budget_usd, 10.0)

    def test_fresh_command_uses_snapshot_budget_and_preallocated_session(self) -> None:
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct", max_budget_usd=99.0)
        command = executor._build_command("prompt", self.project, self._packet())
        self.assertNotIn("--no-session-persistence", command)
        self.assertNotIn("--resume", command)
        self.assertEqual(command[command.index("--session-id") + 1], self.session_id)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.5")
        self.assertIn("--safe-mode", command)
        self.assertIn("--add-dir", command)

    def test_resume_command_uses_the_same_session_id(self) -> None:
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct")
        command = executor._build_command("prompt", self.project, self._packet(run_ids=["claude-ABCD-001-first"]))
        self.assertNotIn("--session-id", command)
        self.assertEqual(command[command.index("--resume") + 1], self.session_id)

    @_claude_executor_mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_ephemeral_start_creates_only_path_plan_directory_and_emits_receipt(self, run: _claude_executor_mock.Mock) -> None:
        run.return_value = _claude_executor_subprocess.CompletedProcess([], 0, stdout=self._completed_output(), stderr="")
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct")
        with (
            _claude_executor_mock.patch("agent_bridge_connect.executors.claude.assert_claude_path_capability_supported", return_value={"supported": True}),
            _claude_executor_mock.patch.object(executor, "_start_run_lease"),
            _claude_executor_mock.patch.object(executor, "_heartbeat_run"),
            _claude_executor_mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
        self.assertTrue(started.ok)
        self.assertTrue(self.claude_project.is_dir())
        self.assertEqual(_claude_executor_Path(run.call_args.kwargs["cwd"]), self.claude_project)
        receipt = executor.poll(started.run_id).result["execution_session"]
        self.assertEqual(receipt, {"version": 1, "executor": "claude", "session_id": self.session_id, "resumed": False, "persistence": "persistent", "source": "preallocated"})

    @_claude_executor_mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_retained_start_uses_user_directory_without_creating_ephemeral_path(self, run: _claude_executor_mock.Mock) -> None:
        run.return_value = _claude_executor_subprocess.CompletedProcess([], 0, stdout=self._completed_output(), stderr="")
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct")
        with (
            _claude_executor_mock.patch("agent_bridge_connect.executors.claude.assert_claude_path_capability_supported", return_value={"supported": True}),
            _claude_executor_mock.patch.object(executor, "_start_run_lease"),
            _claude_executor_mock.patch.object(executor, "_heartbeat_run"),
            _claude_executor_mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet(retain=True))
        self.assertTrue(started.ok)
        self.assertEqual(_claude_executor_Path(run.call_args.kwargs["cwd"]), self.project)
        self.assertFalse(self.claude_project.exists())

    @_claude_executor_mock.patch("agent_bridge_connect.executors.claude.subprocess.run")
    def test_timeout_preserves_available_session_receipt(self, run: _claude_executor_mock.Mock) -> None:
        run.side_effect = _claude_executor_subprocess.TimeoutExpired(cmd=[_claude_executor_sys.executable], timeout=1, output="partial", stderr="timeout")
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct", timeout_s=1)
        with (
            _claude_executor_mock.patch("agent_bridge_connect.executors.claude.assert_claude_path_capability_supported", return_value={"supported": True}),
            _claude_executor_mock.patch.object(executor, "_start_run_lease"),
            _claude_executor_mock.patch.object(executor, "_heartbeat_run"),
            _claude_executor_mock.patch.object(executor, "_mark_run_stale"),
        ):
            started = executor.start(self._packet(run_ids=["claude-ABCD-001-first"]))
        polled = executor.poll(started.run_id)
        self.assertEqual(polled.status, "needs_recovery")
        self.assertEqual(polled.result["execution_session"]["session_id"], self.session_id)
        self.assertTrue(polled.result["execution_session"]["resumed"])

    def test_ephemeral_path_mismatch_fails_without_creating_either_directory(self) -> None:
        packet = self._packet()
        injected = self.root / "outside" / "claude"
        packet["extensions"]["agentbc.session"]["project_path"] = str(injected)
        executor = _claude_executor_ClaudeExecutor(command=_claude_executor_sys.executable, transport="direct")
        started = executor.start(packet)
        self.assertFalse(started.ok)
        self.assertIn("does not match", started.message)
        self.assertFalse(injected.exists())
        self.assertFalse(self.claude_project.exists())


class _claude_path_Perm103007ClaudePathCapabilityTests(_claude_path_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _claude_path_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _claude_path_Path(self.temporary.name).resolve()
        self.customer = self.root / "customer"
        self.customer.mkdir()
        self.plan = _claude_path_build_path_plan(customer_dir=True, customer_path=self.customer, task_code="PTHS", iteration=1, config={"workspace_root": str(self.root / "workspace")}, task_date="2026-08-22")
        self.packet = {
            "task_id": "PTHS-001",
            "title": "Claude path capability",
            "steps": [{"id": 1, "description": "write the deliverable"}],
            "workspace": self.plan.to_workspace(),
            "task_board": {"root": str(self.root / "private-board")},
            "extensions": {
                "agentbc.permission": _claude_path_build_permission_record(explicit_mode="safe"),
                "agentbc.session": {"version": 1, "executor": "claude", "retain": False, "session_id": str(_claude_path_uuid.uuid4()), "session_state": "pending", "project_mode": "ephemeral", "project_path": str(self.plan.executor_project_root), "run_ids": []},
            },
        }

    def _command(self, mode: str = "safe") -> list[str]:
        packet = dict(self.packet)
        packet["extensions"] = dict(self.packet["extensions"])
        record = _claude_path_build_permission_record(explicit_mode=mode)
        packet["extensions"]["agentbc.permission"] = record
        return _claude_path_ClaudeExecutor(command=_claude_path_sys.executable, transport="direct")._build_command("prompt", self.plan.project_root, packet, record)

    def test_only_non_full_modes_receive_the_legacy_path_boundary(self) -> None:
        expected_settings = None
        for mode in ("inherit", "safe", "full"):
            with self.subTest(mode=mode):
                command = self._command(mode)
                if mode == "full":
                    self.assertNotIn("--settings", command)
                    self.assertNotIn("--add-dir", command)
                    self.assertNotIn("--disallowedTools", command)
                    continue
                self.assertEqual(command.count("--settings"), 1)
                self.assertEqual(command.count("--add-dir"), 1)
                settings_text = command[command.index("--settings") + 1]
                settings = _claude_path_json.loads(settings_text)
                self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"], [str(self.plan.artifact_root), str(self.plan.executor_project_root)])
                self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"], [])
                self.assertFalse(settings["sandbox"]["autoAllowBashIfSandboxed"])
                self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
                self.assertTrue(settings["sandbox"]["failIfUnavailable"])
                self.assertEqual(command[command.index("--add-dir") + 1], str(self.plan.artifact_root))
                self.assertNotIn(str(self.root / "private-board"), settings_text)
                self.assertNotIn(str(self.plan.report_root), settings_text)
                if expected_settings is None:
                    expected_settings = settings_text
                self.assertEqual(settings_text, expected_settings)

    def test_runner_reconstructs_exact_settings_and_rejects_tampering(self) -> None:
        command = self._command("safe")
        capability = _claude_path_assert_claude_path_capability_command(command, self.packet, execution_root=self.plan.executor_project_root)
        self.assertIsNotNone(capability)
        _claude_path_validate_permission_command("claude", command, self.packet["extensions"]["agentbc.permission"], authorized_claude_settings=capability["settings_json"], authorized_claude_add_dir=True)
        tampered_settings = list(command)
        settings_index = tampered_settings.index("--settings") + 1
        tampered_settings[settings_index] = '{"sandbox":{"enabled":false}}'
        with self.assertRaises(_claude_path_ABCError) as raised:
            _claude_path_assert_claude_path_capability_command(tampered_settings, self.packet, execution_root=self.plan.executor_project_root)
        self.assertEqual(raised.exception.code, "claude_path_capability_mismatch")
        tampered_root = list(command)
        add_dir_index = tampered_root.index("--add-dir") + 1
        tampered_root[add_dir_index] = str(self.root)
        with self.assertRaises(_claude_path_ABCError):
            _claude_path_assert_claude_path_capability_command(tampered_root, self.packet, execution_root=self.plan.executor_project_root)
        inline = [token for (index, token) in enumerate(command) if index not in {command.index("--settings"), command.index("--settings") + 1}]
        inline.append(f"--settings={capability['settings_json']}")
        with self.assertRaises(_claude_path_ABCError):
            _claude_path_assert_claude_path_capability_command(inline, self.packet, execution_root=self.plan.executor_project_root)

    def test_pathplan_mismatch_and_shared_project_artifact_fail_closed(self) -> None:
        mismatch = dict(self.packet)
        mismatch["extensions"] = dict(self.packet["extensions"])
        mismatch["extensions"]["agentbc.session"] = dict(self.packet["extensions"]["agentbc.session"])
        mismatch["extensions"]["agentbc.session"]["project_path"] = str(self.root / "outside")
        with self.assertRaises(_claude_path_ABCError) as raised:
            _claude_path_claude_ephemeral_path_capability(mismatch, execution_root=self.plan.executor_project_root)
        self.assertEqual(raised.exception.code, "claude_path_capability_mismatch")
        shared = dict(self.packet)
        shared["workspace"] = dict(self.packet["workspace"])
        shared["workspace"]["artifact_root"] = str(self.plan.executor_project_root)
        with self.assertRaises(_claude_path_ABCError):
            _claude_path_claude_ephemeral_path_capability(shared, execution_root=self.plan.executor_project_root)

    def test_version_and_help_probe_is_bounded_and_fail_closed(self) -> None:
        supported = (_claude_path_subprocess.CompletedProcess([], 0, stdout="2.1.226 (Claude Code)", stderr=""), _claude_path_subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON", stderr=""))
        with _claude_path_mock.patch("platform.system", return_value="Darwin"), _claude_path_mock.patch("agent_bridge_connect.claude_path_capability.subprocess.run", side_effect=supported):
            result = _claude_path_assert_claude_path_capability_supported("/bin/claude")
        self.assertTrue(result["supported"])
        self.assertEqual(result["version"], "2.1.226")
        cases = (
            ("Darwin", _claude_path_subprocess.CompletedProcess([], 0, stdout="2.2.0", stderr=""), _claude_path_subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON", stderr="")),
            ("Darwin", _claude_path_subprocess.CompletedProcess([], 0, stdout="2.1.226", stderr=""), _claude_path_subprocess.CompletedProcess([], 0, stdout="--add-dir X", stderr="")),
            ("Windows", _claude_path_subprocess.CompletedProcess([], 0, stdout="2.1.226", stderr=""), _claude_path_subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON", stderr="")),
        )
        for system, version_result, help_result in cases:
            with self.subTest(system=system, version=version_result.stdout):
                with _claude_path_mock.patch("platform.system", return_value=system), _claude_path_mock.patch("agent_bridge_connect.claude_path_capability.subprocess.run", side_effect=(version_result, help_result)), self.assertRaises(_claude_path_ABCError) as raised:
                    _claude_path_assert_claude_path_capability_supported("/bin/claude")
                self.assertEqual(raised.exception.code, "claude_path_capability_unsupported")

    def test_permission_registry_reports_path_capability_for_inherit(self) -> None:
        completed = (
            _claude_path_subprocess.CompletedProcess([], 0, stdout="2.1.226 (Claude Code)", stderr=""),
            _claude_path_subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON\n--dangerously-skip-permissions\n--safe-mode\n--permission-mode MODE", stderr=""),
            _claude_path_subprocess.CompletedProcess([], 0, stdout="2.1.226 (Claude Code)", stderr=""),
            _claude_path_subprocess.CompletedProcess([], 0, stdout="--add-dir X\n--settings JSON\n--dangerously-skip-permissions\n--safe-mode\n--permission-mode MODE", stderr=""),
        )
        with _claude_path_mock.patch("platform.system", return_value="Darwin"), _claude_path_mock.patch("agent_bridge_connect.claude_path_capability.subprocess.run", side_effect=completed[:2]):
            report = _claude_path_probe_executor_capability("claude", "inherit", "/bin/claude")
        self.assertTrue(report["supported"])
        self.assertEqual(report["details"]["path_capability"]["capability_id"], "claude.ephemeral_project_isolation.v1")
