"""PERM-104-002: Claude SDK control transport tests.

Covers the fail-closed contract of :class:`ClaudeSDKControlTransport`:
options building (probed tuple only, no PATH fallback), allow with original
input, deny, tool_use_id binding, duplicate/concurrent refusal, transport
death invalidation, restart/replay refusal, the three ``full`` sources'
SDK mode mapping, the hooks feed, and the ``tools``/``auto_approve_tools``
config split with legacy dual-read.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.claude_sdk_hooks import (
    append_hook_record,
    has_structured_post_tool_use_success,
    sanitize_hook_input,
)
from agent_bridge_connect.claude_sdk_transport import (
    SDK_PERMISSION_MODE_BY_FLAG,
    ClaudeSDKControlTransport,
    ClaudeSDKTransportError,
    build_sdk_options,
)
from agent_bridge_connect.control import ApprovalControlPlane
from agent_bridge_connect.session import control_root_for_task
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.service import TaskService

RUN_ID = "claude-GGQN-001-run1"


def _kind_handle(pending: dict, kind: str) -> str:
    """Resolve the opaque handle of the first offered choice with this kind."""
    return next(
        choice["handle"]
        for choice in pending.get("offered_choices") or []
        if choice.get("kind") == kind
    )


def _plane(tmp: str, *, task_id: str = "SDKT-001", run_id: str = "claude-sdk-1",
           session_id: str = "") -> ApprovalControlPlane:
    return ApprovalControlPlane(
        Path(tmp) / ".agentbc-control" / task_id,
        task_id=task_id,
        executor_run_id=run_id,
        session_id=session_id,
        executor="claude",
    )


class _FakeContext:
    def __init__(self, tool_use_id: str) -> None:
        self.tool_use_id = tool_use_id
        self.suggestions: list = []
        self.agent_id: str | None = None
        self.blocked_path: str | None = None
        self.decision_reason: str | None = None


def _run(coro):
    return asyncio.run(coro)


class ClaudeSdkOptionsTests(unittest.TestCase):
    """build_sdk_options binds only the configured absolute CLI path."""

    def _sdk(self) -> types.ModuleType:
        try:
            import claude_agent_sdk as sdk
        except Exception:
            self.skipTest("claude-agent-sdk not installed")
        return sdk

    def test_options_bind_cli_path_and_callback(self) -> None:
        sdk = self._sdk()

        async def cb(tool_name, input_data, context):  # pragma: no cover
            return None

        options = build_sdk_options(
            cli_path="/opt/claude-2.1.233",
            cwd="/tmp",
            can_use_tool=cb,
        )
        self.assertIsInstance(options, sdk.ClaudeAgentOptions)
        self.assertEqual(str(options.cli_path), "/opt/claude-2.1.233")
        self.assertIs(options.can_use_tool, cb)
        self.assertEqual(options.permission_mode, "default")
        self.assertNotIn("claude", str(options.cli_path).split("/")[1])

    def test_disallowed_tools_defaults_are_frozen(self) -> None:
        self._sdk()
        options = build_sdk_options(
            cli_path="/opt/claude", cwd="/tmp", can_use_tool=None
        )
        self.assertEqual(
            options.disallowed_tools, ["TaskCreate", "TaskUpdate", "TodoWrite"]
        )

    def test_tools_visibility_does_not_shadow_permission_callback(self) -> None:
        self._sdk()
        options = build_sdk_options(
            cli_path="/opt/claude",
            cwd="/tmp",
            can_use_tool=None,
            tools=["Read", "Write", "Edit", "Bash"],
            allowed_tools=[],
        )
        self.assertEqual(options.tools, ["Read", "Write", "Edit", "Bash"])
        self.assertEqual(options.allowed_tools, [])

    def test_full_flag_maps_to_bypass_permissions(self) -> None:
        self.assertEqual(
            SDK_PERMISSION_MODE_BY_FLAG.get("--dangerously-skip-permissions"),
            "bypassPermissions",
        )


class ClaudeSdkTransportDecisionTests(unittest.TestCase):
    """can_use_tool bridges into the frozen ControlPlane."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session_id = str(uuid.uuid4())
        self.plane = _plane(
            self._tmp.name,
            session_id=self.session_id,
        )
        self.plane.record_session_started(
            {
                "version": 1,
                "executor": "claude",
                "session_id": self.session_id,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            }
        )

    def _transport(self) -> ClaudeSDKControlTransport:
        transport = ClaudeSDKControlTransport(
            plane=self.plane,
            task_id="SDKT-001",
            run_id="claude-sdk-1",
            session_id=self.session_id,
        )
        self.addCleanup(transport.stop)
        return transport

    def test_missing_tool_use_id_denies_without_control_plane_write(self) -> None:
        transport = self._transport()

        class _NoId:
            tool_use_id = None

        async def scenario():
            return await transport.can_use_tool("Bash", {"command": "ls"}, _NoId())

        result = _run(scenario())
        self.assertEqual(getattr(result, "behavior", ""), "deny")
        self.assertIsNone(self.plane.status().get("pending_request"))

    def test_allow_returns_original_input_after_decision(self) -> None:
        transport = self._transport()
        tool_input = {"command": "echo hi", "description": "greet"}

        def decide_later() -> None:
            state = self.plane._state()
            pending = state.get("pending_request") or {}
            deadline = __import__("time").time() + 5
            while not pending and __import__("time").time() < deadline:
                threading.Event().wait(0.02)
                state = self.plane._state()
                pending = state.get("pending_request") or {}
            self.plane.respond_approval(
                "SDKT-001",
                "claude-sdk-1",
                self.session_id,
                str(pending.get("request_id") or ""),
                "accept",
                choice_handle=_kind_handle(pending, "once"),
            )

        thread = threading.Thread(target=decide_later, daemon=True)
        thread.start()
        result = _run(
            transport.can_use_tool(
                "Bash", tool_input, _FakeContext("call_allow_1")
            )
        )
        thread.join(timeout=5)
        self.assertEqual(getattr(result, "behavior", ""), "allow")
        self.assertEqual(getattr(result, "updated_input", None), tool_input)

    def test_deny_returns_deny_result(self) -> None:
        transport = self._transport()

        def decide_later() -> None:
            state = self.plane._state()
            pending = state.get("pending_request") or {}
            deadline = __import__("time").time() + 5
            while not pending and __import__("time").time() < deadline:
                threading.Event().wait(0.02)
                state = self.plane._state()
                pending = state.get("pending_request") or {}
            self.plane.respond_approval(
                "SDKT-001",
                "claude-sdk-1",
                self.session_id,
                str(pending.get("request_id") or ""),
                "decline",
                choice_handle=_kind_handle(pending, "deny"),
            )

        thread = threading.Thread(target=decide_later, daemon=True)
        thread.start()
        result = _run(
            transport.can_use_tool(
                "Bash", {"command": "rm -rf /"}, _FakeContext("call_deny_1")
            )
        )
        thread.join(timeout=5)
        self.assertEqual(getattr(result, "behavior", ""), "deny")

    def test_duplicate_tool_use_id_is_refused(self) -> None:
        transport = self._transport()
        context = _FakeContext("call_dup_1")

        def decide_deny() -> None:
            state = self.plane._state()
            pending = state.get("pending_request") or {}
            deadline = __import__("time").time() + 5
            while not pending and __import__("time").time() < deadline:
                threading.Event().wait(0.02)
                state = self.plane._state()
                pending = state.get("pending_request") or {}
            self.plane.respond_approval(
                "SDKT-001",
                "claude-sdk-1",
                self.session_id,
                str(pending.get("request_id") or ""),
                "decline",
                choice_handle=_kind_handle(pending, "deny"),
            )

        thread = threading.Thread(target=decide_deny, daemon=True)
        thread.start()
        first = _run(transport.can_use_tool("Bash", {"command": "a"}, context))
        thread.join(timeout=5)
        self.assertEqual(getattr(first, "behavior", ""), "deny")
        # A second request reusing the same native tool_use_id is refused
        # without consulting the control plane again.
        second = _run(transport.can_use_tool("Bash", {"command": "b"}, context))
        self.assertEqual(getattr(second, "behavior", ""), "deny")

    def test_transport_death_invalidates_pending_request(self) -> None:
        transport = self._transport()
        outcome: dict = {}

        def die_while_pending() -> None:
            deadline = __import__("time").time() + 5
            state = self.plane._state()
            while __import__("time").time() < deadline:
                state = self.plane._state()
                pending = state.get("pending_request") or {}
                if pending.get("status") == "pending":
                    transport.record_transport_death("worker crashed")
                    outcome["invalidated"] = True
                    return
                threading.Event().wait(0.02)
            outcome["invalidated"] = False

        thread = threading.Thread(target=die_while_pending, daemon=True)
        thread.start()
        with self.assertRaises(ClaudeSDKTransportError) as raised:
            _run(
                transport.can_use_tool(
                    "Bash", {"command": "x"}, _FakeContext("call_death_1")
                )
            )
        thread.join(timeout=5)
        self.assertTrue(outcome.get("invalidated"))
        # The control plane invalidated the pending request; the wait fails
        # closed either with the transport timeout or the recovery state.
        self.assertIn(
            raised.exception.code,
            {"claude_sdk_approval_timeout", "approval_control_needs_recovery"},
        )
        state = self.plane._state()
        self.assertEqual(
            (state.get("pending_request") or {}).get("status"), "invalidated"
        )

    def test_second_client_refusal(self) -> None:
        transport = self._transport()
        transport.connect_client(object())
        with self.assertRaises(ClaudeSDKTransportError) as raised:
            transport.connect_client(object())
        self.assertEqual(raised.exception.code, "claude_sdk_client_duplicate")


class ClaudeExecutorSdkSemanticsTests(unittest.TestCase):
    """Executor-level frozen semantics: three full sources and config split."""

    def _executor(self, **kwargs) -> ClaudeExecutor:
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "claude"
            fake.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o100)
            return ClaudeExecutor(command=str(fake), transport="direct", **kwargs)

    def test_legacy_allowed_tools_dual_reads_as_tools_only(self) -> None:
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            executor = self._executor(allowed_tools=["Read", "Write"])
        self.assertTrue(
            any(issubclass(w.category, DeprecationWarning) for w in caught)
        )
        self.assertEqual(executor.tools, ["Read", "Write"])
        self.assertEqual(executor.auto_approve_tools, [])

    def test_tools_and_auto_approve_tools_split(self) -> None:
        executor = self._executor(
            tools=["Read", "Bash"], auto_approve_tools=["Read"]
        )
        self.assertEqual(executor.tools, ["Read", "Bash"])
        self.assertEqual(executor.auto_approve_tools, ["Read"])

    def test_default_config_never_auto_approves(self) -> None:
        executor = self._executor()
        self.assertEqual(executor.auto_approve_tools, [])
        self.assertTrue(executor.tools)

    def test_executor_registry_accepts_split_keys(self) -> None:
        from agent_bridge_connect.executor_registry import get_executor

        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "claude"
            fake.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | 0o100)
            executor = get_executor(
                "claude",
                {
                    "type": "claude",
                    "command": str(fake),
                    "tools": ["Read"],
                    "auto_approve_tools": [],
                    "runtime_source": "test",
                },
            )
        self.assertEqual(executor.tools, ["Read"])
        self.assertEqual(executor.auto_approve_tools, [])

    def test_start_control_fails_closed_without_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            fake = Path(temporary) / "claude"
            fake.write_text(
                "#!/bin/sh\nprintf '2.1.233 (Claude Code)'\n", encoding="utf-8"
            )
            fake.chmod(fake.stat().st_mode | 0o100)
            executor = ClaudeExecutor(command=str(fake), transport="direct")
            executor._version = "2.1.233 (Claude Code)"
            packet = {
                "task_id": "SDKT-002",
                "steps": [{"id": 1, "description": "one"}],
                "workspace": {"project_root": str(workspace), "root": str(workspace)},
                "task_board": {"root": str(workspace)},
                "extensions": {},
                "runner_authorization_required": True,
            }
            if "claude_agent_sdk" in sys.modules:
                with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
                    result = executor.start_control(packet)
            else:
                # Deterministic regardless of import order: force the
                # dependency-missing condition exactly like the gate would
                # see on a core-light install.
                with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
                    result = executor.start_control(packet)
            self.assertFalse(result.ok)
            self.assertTrue(
                result.message.startswith("claude_sdk_")
                or "permission_transport_unsupported" in result.message,
                result.message,
            )


class ClaudeSdkRuntimeVerifierTests(unittest.TestCase):
    """PERM-104-002 correction (GGQN-001): the executor's runtime verifier
    transitions the durable agentbc.permission_runtime record to verified
    ONLY on structured PostToolUse success for the exact approved
    tool_use_id plus a structured non-error ResultMessage.  PreToolUse
    records, failure records, and callback prose never verify."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "customer"
        self.project.mkdir()
        self.session_id = str(uuid.uuid4())
        self.executor = ClaudeExecutor(command=sys.executable, transport="direct")

    def _started_task(self) -> str:
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        task = service.create_task(
            "sdk runtime verify",
            "claude",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        service.start_task_run(task.id, "claude")
        service.record_executor_run_started(task.id, RUN_ID)
        return task.id

    def _packet(self, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {"root": str(self.project), "project_root": str(self.project)},
            "task_board": {"root": str(self.board)},
            "extensions": {},
        }

    def _record(self) -> dict:
        from agent_bridge_connect.permission_runtime import (
            build_permission_runtime_record,
        )

        return build_permission_runtime_record(
            task_id="GGQN-001",
            chain_head_id="GGQN-001",
            executor="claude",
            executor_run_id=RUN_ID,
            session_id=self.session_id,
            permission_source="one_shot_permission_grant",
            path_plan_digest="sha256:" + "0" * 64,
            host_profile_digest="sha256:" + "1" * 64,
        )

    def _activated_task(self, task_id: str) -> None:
        from agent_bridge_connect.permission_runtime import (
            PERMISSION_RUNTIME_EXTENSION_KEY,
            activate_permission_runtime_record,
            authorize_permission_runtime_record,
        )

        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        current = service.get_task(task_id)
        record = self._record()
        record["binding"]["task_id"] = task_id
        record["binding"]["executor_run_id"] = RUN_ID
        authorized = authorize_permission_runtime_record(
            record, decision="approve", request_id="runner-dispatch-" + RUN_ID
        )
        activated = activate_permission_runtime_record(
            authorized, host_profile_digest="sha256:" + "1" * 64
        )
        current.extensions = dict(current.extensions or {})
        current.extensions[PERMISSION_RUNTIME_EXTENSION_KEY] = activated
        service.store.write_task(current.id, current.to_dict())
    def _captured(self) -> dict:
        return {
            "stdout": "done",
            "stderr": "",
            "returncode": 0,
            "init_verified": True,
            "session_id": self.session_id,
            "result": {"is_error": False, "num_turns": 1, "session_id": self.session_id, "result": "done"},
        }

    def test_verified_only_after_structured_post_tool_use_success(self) -> None:
        from agent_bridge_connect.claude_sdk_hooks import (
            append_hook_record,
            bind_hook_log_session,
        )
        from agent_bridge_connect.permission_runtime import (
            permission_runtime_from_extensions,
        )

        task_id = self._started_task()
        self._activated_task(task_id)
        verifier = self.executor._sdk_runtime_verifier(
            self._packet(task_id), RUN_ID, {"session_id": self.session_id}
        )
        control_root = control_root_for_task(task_id, board_root=self.board)
        # GGQN-002: the hook log is bound to the official session before the
        # prompt; an unbound log never verifies a session-bound run.
        bind_hook_log_session(control_root, self.session_id)
        # Without any hook record the run cannot verify.
        first = verifier("call-verify-1", self._captured())
        self.assertFalse(first["verified"])
        self.assertEqual(
            first["reason"], "claude_sdk_post_tool_use_success_missing"
        )
        # A PreToolUse record is not success evidence.
        append_hook_record(
            control_root,
            {"event": "PreToolUse", "tool_use_id": "call-verify-1", "tool_name": "Bash"},
        )
        self.assertFalse(verifier("call-verify-1", self._captured())["verified"])
        # A failure record is not success evidence.
        append_hook_record(
            control_root,
            {"event": "PostToolUseFailure", "tool_use_id": "call-verify-1", "tool_name": "Bash"},
            extra={"blocked": True},
        )
        self.assertFalse(verifier("call-verify-1", self._captured())["verified"])
        # The structured PostToolUse success for the exact tool_use_id
        # verifies the activated record and persists the transition.
        append_hook_record(
            control_root,
            {"event": "PostToolUse", "tool_use_id": "call-verify-1", "tool_name": "Bash"},
            extra={"blocked": False},
        )
        outcome = verifier("call-verify-1", self._captured())
        self.assertTrue(outcome["verified"], outcome)
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(task_id).extensions, task_id=task_id, executor_run_id=RUN_ID
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["state"]["status"], "verified")

    def test_unbound_or_foreign_hook_log_never_verifies(self) -> None:
        """GGQN-002: verification is fail-closed to the bound session log."""
        from agent_bridge_connect.claude_sdk_hooks import (
            append_hook_record,
            bind_hook_log_session,
        )

        task_id = self._started_task()
        self._activated_task(task_id)
        verifier = self.executor._sdk_runtime_verifier(
            self._packet(task_id), RUN_ID, {"session_id": self.session_id}
        )
        control_root = control_root_for_task(task_id, board_root=self.board)
        append_hook_record(
            control_root,
            {"event": "PostToolUse", "tool_use_id": "call-xrun-1", "tool_name": "Bash"},
            extra={"blocked": False},
        )
        # Unbound log: the PostToolUse record exists but its provenance was
        # never pinned to the official session — never verifies.
        outcome = verifier("call-xrun-1", self._captured())
        self.assertFalse(outcome["verified"])
        self.assertEqual(outcome["reason"], "claude_sdk_post_tool_use_success_missing")
        # A log bound to a DIFFERENT session is a cross-run replay — the
        # stale record cannot verify this session's run either.
        bind_hook_log_session(control_root, str(uuid.uuid4()))
        outcome = verifier("call-xrun-1", self._captured())
        self.assertFalse(outcome["verified"])

    def test_missing_or_errored_result_never_verifies(self) -> None:
        task_id = self._started_task()
        self._activated_task(task_id)
        verifier = self.executor._sdk_runtime_verifier(
            self._packet(task_id), RUN_ID, {"session_id": self.session_id}
        )
        errored = self._captured()
        errored["result"]["is_error"] = True
        outcome = verifier("call-verify-2", errored)
        self.assertFalse(outcome["verified"])
        self.assertEqual(outcome["reason"], "claude_run_result_not_structured_success")
        # No approval identity: nothing to verify.
        outcome = verifier("", self._captured())
        self.assertFalse(outcome["verified"])

    def test_record_not_activated_never_verifies(self) -> None:
        from agent_bridge_connect.claude_sdk_hooks import bind_hook_log_session
        from agent_bridge_connect.permission_runtime import (
            PERMISSION_RUNTIME_EXTENSION_KEY,
            build_permission_runtime_record,
        )

        task_id = self._started_task()
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        current = service.get_task(task_id)
        record = build_permission_runtime_record(
            task_id=task_id,
            chain_head_id=task_id,
            executor="claude",
            executor_run_id=RUN_ID,
            session_id=self.session_id,
            permission_source="explicit_task",
            path_plan_digest="sha256:" + "0" * 64,
            host_profile_digest="sha256:" + "1" * 64,
        )
        current.extensions = dict(current.extensions or {})
        current.extensions[PERMISSION_RUNTIME_EXTENSION_KEY] = record
        service.store.write_task(current.id, current.to_dict())
        # Structured success evidence IS present: the record's own state
        # (prepared, not activated) must be what blocks verification.
        control_root_3 = control_root_for_task(task_id, board_root=self.board)
        bind_hook_log_session(control_root_3, self.session_id)
        append_hook_record(
            control_root_3,
            {"event": "PostToolUse", "tool_use_id": "call-verify-3", "tool_name": "Bash"},
            extra={"blocked": False},
            session_id=self.session_id,
        )
        verifier = self.executor._sdk_runtime_verifier(
            self._packet(task_id), RUN_ID, {"session_id": self.session_id}
        )
        outcome = verifier("call-verify-3", self._captured())
        self.assertFalse(outcome["verified"])
        self.assertEqual(outcome["reason"], "permission_runtime_state_invalid")


class ClaudeControlCommandTests(unittest.TestCase):
    """Hook feed records structured PostToolUse success evidence only."""

    def test_sanitize_rejects_unknown_events(self) -> None:
        class _Input:
            hook_event_name = "Stop"
            tool_use_id = "x"
            tool_name = "Bash"

        with self.assertRaises(ValueError):
            sanitize_hook_input(_Input())

    def test_post_tool_use_success_is_the_only_verify_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(
                has_structured_post_tool_use_success(temporary, tool_use_id="t1")
            )
            append_hook_record(
                temporary,
                {"event": "PostToolUseFailure", "tool_use_id": "t1", "tool_name": "Bash"},
                extra={"blocked": True},
            )
            self.assertFalse(
                has_structured_post_tool_use_success(temporary, tool_use_id="t1")
            )
            append_hook_record(
                temporary,
                {"event": "PostToolUse", "tool_use_id": "t1", "tool_name": "Bash"},
                extra={"blocked": False},
            )
            self.assertTrue(
                has_structured_post_tool_use_success(temporary, tool_use_id="t1")
            )
            self.assertFalse(
                has_structured_post_tool_use_success(temporary, tool_use_id="other")
            )

    def test_sdk_hooks_build_maps_three_events(self) -> None:
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception:
            self.skipTest("claude-agent-sdk not installed")
        from agent_bridge_connect.claude_sdk_hooks import build_sdk_hooks

        with tempfile.TemporaryDirectory() as temporary:
            hooks = build_sdk_hooks(temporary)
            self.assertEqual(
                sorted(hooks), ["PostToolUse", "PostToolUseFailure", "PreToolUse"]
            )


if __name__ == "__main__":
    unittest.main()
