"""Targeted tests for the Claude stream/control permission-prompt broker."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.executors.claude import (
    PERMISSION_PROMPT_TOOL_FLAG,
    ClaudeExecutor,
    ClaudePermissionPromptBroker,
    _parse_stream_json_line,
)
from agent_bridge_connect.service import TaskService

RUN_ID = "claude-ABCD-001-run1"


def _can_use_tool_event(**overrides: str) -> dict:
    payload = {
        "type": "can_use_tool",
        "session_id": "019feed0-0000-7000-8000-0000000000aa",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "user_prompt": "list files",
    }
    payload.update(overrides)
    return payload


class ClaudePermissionBrokerTests(unittest.TestCase):
    def test_allow_only_returns_allow(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "allow"},
        )
        line = '{"type":"can_use_tool","tool_name":"Bash"}'
        response = broker.handle_request_line(line)
        self.assertEqual(response, '{"permission": "allow"}')

    def test_deny_only_returns_deny(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "deny"},
        )
        response = broker.handle_request_line('{"tool_name":"Bash"}')
        self.assertEqual(response, '{"permission": "deny"}')

    def test_never_applies_updated_permissions(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {
                "permission": "allow",
                "updated_permissions": ["Write"],
            },
        )
        response = broker.handle_request_line('{"tool_name":"Write"}')
        self.assertEqual(response, '{"permission": "allow"}')
        self.assertNotIn("updated_permissions", response)

    def test_invalid_json_is_denied(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "allow"},
        )
        response = broker.handle_request_line("not-json")
        self.assertTrue(response.startswith('{"permission": "deny"'))
        self.assertNotIn("allow", response)

    def test_init_receipt_verification(self) -> None:
        broker = ClaudePermissionPromptBroker(
            session_id="019feed0-0000-7000-8000-0000000000aa",
            decision_callback=lambda request: {"permission": "deny"},
        )
        self.assertTrue(
            broker._verify_init_receipt(
                {"type": "system", "subtype": "init", "session_id": "019feed0-0000-7000-8000-0000000000aa"}
            )
        )
        self.assertFalse(
            broker._verify_init_receipt(
                {"type": "system", "subtype": "init", "session_id": "other"}
            )
        )

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
        self.claude_project = (
            self.root
            / "workspace"
            / "tasks"
            / "artifacts"
            / "2026-08-10"
            / "ABCD"
            / "ABCD-001"
            / "claude"
        )
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
                "agentbc.resources": {
                    "version": 1,
                    "executor": "claude",
                    "resource": "max_budget_usd",
                    "current_limit": 2.5,
                },
                "agentbc.session": {
                    "version": 1,
                    "executor": "claude",
                    "retain": False,
                    "session_id": self.session_id,
                    "session_state": "pending",
                    "project_mode": "ephemeral",
                    "project_path": str(self.claude_project),
                    "run_ids": [],
                },
                "agentbc.permission": {
                    "requested_mode": "safe",
                    "effective_mode": "safe",
                    "selection_source": "configured_default",
                },
            },
        }

    @mock.patch("agent_bridge_connect.executors.claude.ClaudeExecutor.supports_permission_prompt_tool")
    def test_control_command_preallocates_session_and_adds_prompt_tool(
        self, supports: mock.Mock
    ) -> None:
        supports.return_value = True
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        broker = ClaudePermissionPromptBroker(
            session_id=self.session_id,
            decision_callback=lambda request: {"permission": "deny"},
        )
        command = executor._build_control_command(
            "prompt",
            self.project,
            self._packet(),
            {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "x"},
            broker,
        )
        self.assertEqual(command[command.index("--session-id") + 1], self.session_id)
        self.assertIn(PERMISSION_PROMPT_TOOL_FLAG, command)
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")

    @mock.patch("agent_bridge_connect.executors.claude.ClaudeExecutor.supports_permission_prompt_tool")
    def test_control_command_without_flag_uses_local_broker(self, supports: mock.Mock) -> None:
        supports.return_value = False
        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        broker = ClaudePermissionPromptBroker(
            session_id=self.session_id,
            decision_callback=lambda request: {"permission": "deny"},
        )
        command = executor._build_control_command(
            "prompt",
            self.project,
            self._packet(),
            {"requested_mode": "safe", "effective_mode": "safe", "selection_source": "x"},
            broker,
        )
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
    def __init__(
        self,
        stdout_lines: list[str],
        *,
        die_after_decision: bool = False,
        broken_pipe: bool = False,
        returncode: int = 0,
    ) -> None:
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
        self.init_line = (
            f'{{"type":"system","subtype":"init","session_id":"{self.session_id}"}}'
        )
        self.can_use_tool_line = (
            '{"type":"can_use_tool","tool_name":"Bash","tool_input":{"command":"ls"}}'
        )

    def _run(self, process: _FakeProcess, *, decision: dict) -> dict:
        def decision_callback(request: dict) -> dict:
            process.decisions += 1
            return decision

        broker = ClaudePermissionPromptBroker(
            session_id=self.session_id,
            decision_callback=decision_callback,
            transport_death_callback=lambda request_id: self.deaths.append(request_id),
        )
        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.Popen",
            return_value=process,
        ):
            return broker.run_controlled(
                command=["claude", "-p"],
                cwd=self.root,
                timeout_s=10,
            )

    def test_transport_death_invalidates_request(self) -> None:
        self.deaths: list[str] = []
        process = _FakeProcess(
            [self.init_line, self.can_use_tool_line],
            die_after_decision=True,
            returncode=1,
        )
        result = self._run(process, decision={"permission": "allow", "request_id": "approval-x"})
        self.assertTrue(result["transport_death_while_approval"])
        self.assertEqual(result["aborted_request_id"], "approval-x")
        self.assertEqual(self.deaths, ["approval-x"])
        self.assertTrue(result["init_verified"])
        self.assertEqual(process.returncode, 1)
        # The response was never written to the dead transport.
        self.assertNotIn('"permission": "allow"', "".join(process.stdin.buffer))

    def test_transport_survives_and_response_is_delivered(self) -> None:
        self.deaths: list[str] = []
        process = _FakeProcess(
            [self.init_line, self.can_use_tool_line],
            die_after_decision=False,
            returncode=0,
        )
        result = self._run(process, decision={"permission": "allow", "request_id": "approval-y"})
        self.assertFalse(result["transport_death_while_approval"])
        self.assertEqual(result["aborted_request_id"], "")
        self.assertEqual(self.deaths, [])
        self.assertEqual(result["returncode"], 0)
        self.assertTrue('"permission": "allow"' in "".join(process.stdin.buffer))

    def test_broken_pipe_invalidates_request(self) -> None:
        self.deaths: list[str] = []
        process = _FakeProcess(
            [self.init_line, self.can_use_tool_line],
            die_after_decision=False,
            broken_pipe=True,
            returncode=1,
        )
        result = self._run(process, decision={"permission": "allow", "request_id": "approval-z"})
        self.assertTrue(result["transport_death_while_approval"])
        self.assertEqual(result["aborted_request_id"], "approval-z")
        self.assertEqual(self.deaths, ["approval-z"])


class ClaudeApprovalCallbackFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "customer"
        self.project.mkdir()
        self.session_id = str(uuid.uuid4())
        self.executor = ClaudeExecutor(command=sys.executable, transport="direct")

    def _task_packet(self, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "title": "control approval",
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {
                "root": str(self.project),
                "project_root": str(self.project),
                "artifact_root": str(self.project),
                "report_file": str(self.root / "report.md"),
            },
            "task_board": {"root": str(self.board)},
            "extensions": {
                "agentbc.session": {
                    "version": 1,
                    "executor": "claude",
                    "retain": False,
                    "session_id": self.session_id,
                    "session_state": "active",
                    "project_mode": "ephemeral",
                    "project_path": str(self.project / "claude"),
                    "run_ids": [RUN_ID],
                },
                "agentbc.permission": {
                    "requested_mode": "safe",
                    "effective_mode": "safe",
                    "selection_source": "configured_default",
                },
            },
        }

    def _started_task(self) -> str:
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        task = service.create_task(
            "control approval",
            "claude",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        service.start_task_run(task.id, "claude")
        service.record_executor_run_started(task.id, RUN_ID)
        model = service.get_task(task.id)
        session = dict((model.extensions or {})["agentbc.session"])
        session["session_state"] = "active"
        session["run_ids"] = [RUN_ID]
        model.extensions = dict(model.extensions or {})
        model.extensions["agentbc.session"] = session
        service.store.write_task(model.id, model.to_dict())
        return model.id

    @mock.patch("agent_bridge_connect.notifications.notify_input_required")
    def test_callback_rejects_concurrent_second_request(self, notify: mock.Mock) -> None:
        task_id = self._started_task()
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        session_id = str(
            (service.get_task(task_id).extensions or {})["agentbc.session"]["session_id"]
        )
        service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-1",
            request_fingerprint="fp-" + "a" * 40,
            executor="claude",
            operation="Bash",
        )
        result = self.executor._approval_decision_callback(
            self._task_packet(task_id),
            RUN_ID,
            {"session_id": session_id},
            _can_use_tool_event(),
        )
        self.assertEqual(result["permission"], "deny")
        self.assertEqual(result["error"], "approval_already_pending")
        notify.assert_not_called()
        # The first request is untouched.
        current = service.get_task(task_id).extensions["agentbc.input"]
        self.assertEqual(current["request_id"], "approval-1")

    @mock.patch("agent_bridge_connect.notifications.notify_input_required")
    def test_callback_approves_only_the_bound_request(self, notify: mock.Mock) -> None:
        task_id = self._started_task()
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        session_id = str(
            (service.get_task(task_id).extensions or {})["agentbc.session"]["session_id"]
        )
        # First request is answered so the callback reaches a second request.
        first = service.block_task_for_approval(
            task_id,
            executor_run_id=RUN_ID,
            session_id=session_id,
            request_id="approval-bound",
            request_fingerprint="fp-" + "b" * 40,
            executor="claude",
            operation="Bash",
        )
        service.respond_to_input(
            task_id,
            first["input_id"],
            response_type="approve",
        )
        # The dialog for the second request records a decision against a
        # different native request: the callback must not report that as an
        # allow for the bound request.
        notify.return_value = {
            "dialog_action": "approve",
            "response": {
                "request_id": "approval-other",
                "approval_decision": "approve",
                "status": "resuming",
            },
        }
        result = self.executor._approval_decision_callback(
            self._task_packet(task_id),
            RUN_ID,
            {"session_id": session_id},
            _can_use_tool_event(),
        )
        self.assertEqual(result["permission"], "deny")
        self.assertEqual(result["error"], "approval_request_mismatch")
        self.assertTrue(result["request_id"].startswith("approval-"))


if __name__ == "__main__":
    unittest.main()
