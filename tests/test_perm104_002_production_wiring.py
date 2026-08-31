"""PERM-104-002 production runtime wiring (GGQN-002).

Drives the REAL transport session path — ``run_controlled`` through a fake
official SDK client with the executor's actual hook feed and the transport's
``capture_tool_event`` — and proves the durable ``agentbc.permission_runtime``
record reaches ``verified`` only through an exact structured PostToolUse
success identity from the same official SDK session and run:

* safe single-action approve/deny: the exact approved ``can_use_tool``
  identity and its matching PostToolUse verify; a deny never verifies;
* explicit full and inherited full (bypassPermissions, no approval request):
  the successful target tool event verifies under the DECLARED run
  authorization without inventing an approval;
* temporary full: the consumed grant declares the run anchor; the
  session-scoped setMode update lands before the prompt in the same live
  session and the successful tool event verifies;
* wrong, missing, duplicate, failed, unrelated, and cross-run tool events
  fail closed and never verify;
* transport death (stream dead after query) never verifies;
* temporary-full revocation is durable through the TaskService store —
  reload after terminal/timeout/crash shows the exact grant revoked.

The tests never call the runtime verifier with a fabricated tool_use_id as
their only proof: every verifying case drives the transport session driver
and the hook-event capture path end to end.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.claude_sdk_hooks import (
    bind_hook_log_session,
    build_sdk_hooks,
)
from agent_bridge_connect.claude_sdk_transport import (
    ClaudeSDKControlTransport,
    ClaudeSDKTransportError,
)
from agent_bridge_connect.control import ApprovalControlPlane
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
    consume_permission_grant,
    permission_grant_from_extensions,
)
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.permission_runtime import (
    PERMISSION_RUNTIME_EXTENSION_KEY,
    activate_permission_runtime_record,
    authorize_permission_runtime_record,
    build_permission_runtime_record,
    load_block_ledger,
    permission_runtime_from_extensions,
)
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.session import control_root_for_task

RUN_ID = "claude-GGQN-002-wire"
SESSION_ID = "22222222-2222-4222-8222-222222222222"


# ── fake official SDK client ────────────────────────────────────────────────


class _FakeSDKClient:
    """Official-surface fake: connect/query/receive_response/disconnect plus
    the ``set_permission_mode`` control request, recording the call order."""

    def __init__(self, options: object = None) -> None:
        self.options = options
        self.messages: list[object] = []
        self.mode_calls: list[str] = []
        self.queries: list[str] = []
        self.stream_dead = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def set_permission_mode(self, mode: str) -> None:
        self.mode_calls.append(mode)

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        if self.stream_dead:
            return
            yield None
        for message in self.messages:
            yield message
        return

    def __aenter__(self) -> "_FakeSDKClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _result_message(is_error: bool = False) -> object:
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success" if not is_error else "error_during_execution",
        duration_ms=10,
        duration_api_ms=0,
        is_error=is_error,
        num_turns=1,
        session_id=SESSION_ID,
        result="done",
    )


class _Harness:
    """One TaskService-backed task plus its executor and transport."""

    def __init__(
        self,
        root: Path,
        *,
        permission: dict | None = None,
        grant: dict | None = None,
        with_runtime_record: bool = False,
    ) -> None:
        self.root = root
        self.board = root / "record"
        self.project = root / "customer"
        self.project.mkdir(parents=True, exist_ok=True)
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(root), "permission_mode": "safe"},
        )
        task = self.service.create_task(
            "sdk production wiring",
            "claude",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        self.task_id = task.id
        self.service.start_task_run(task.id, "claude")
        self.service.record_executor_run_started(task.id, RUN_ID)
        model = self.service.get_task(task.id)
        extensions = dict(model.extensions or {})
        extensions["agentbc.permission"] = dict(
            permission if permission is not None else build_permission_record(explicit_mode="safe")
        )
        if grant is not None:
            extensions[PERMISSION_GRANT_EXTENSION_KEY] = grant
        if with_runtime_record:
            record = build_permission_runtime_record(
                task_id=task.id,
                chain_head_id=task.id,
                executor="claude",
                executor_run_id=RUN_ID,
                session_id="",
                permission_source=(
                    "one_shot_permission_grant"
                    if grant is not None
                    else "explicit_task"
                ),
                path_plan_digest="sha256:" + "0" * 64,
                host_profile_digest="sha256:" + "1" * 64,
            )
            record["binding"]["session_id"] = ""
            authorized = authorize_permission_runtime_record(
                record, decision="approve", request_id="runner-dispatch-" + RUN_ID
            )
            extensions[PERMISSION_RUNTIME_EXTENSION_KEY] = activate_permission_runtime_record(
                authorized, host_profile_digest="sha256:" + "1" * 64
            )
        model.extensions = extensions
        self.service.store.write_task(model.id, model.to_dict())
        self.control_root = control_root_for_task(task.id, board_root=self.board)
        self.executor = ClaudeExecutor(command="claude", transport="direct")
        self.packet = {
            "task_id": task.id,
            "steps": [{"id": 1, "description": "finish"}],
            "workspace": {
                "root": str(self.project),
                "project_root": str(self.project),
            },
            "task_board": {"root": str(self.board)},
            "extensions": extensions,
        }

    def durable_revoker(self):
        def _revoke(grant: dict, code: str) -> None:
            outcome = self.service.revoke_permission_grant_for_target_run(
                self.task_id,
                code,
                target_run_id=RUN_ID,
                expected_grant=grant,
            )
            if not isinstance(outcome, dict):
                raise ClaudeSDKTransportError(
                    "claude_sdk_grant_revoke_failed",
                    "durable revocation failed",
                )

        return _revoke

    def verifier(self, transport: ClaudeSDKControlTransport):
        return self.executor._sdk_runtime_verifier(
            self.packet, RUN_ID, {"session_id": SESSION_ID}, transport=transport
        )


def _plane(root: Path, task_id: str = "GGQW-002", *, with_receipt: bool = True) -> ApprovalControlPlane:
    plane = ApprovalControlPlane(
        root / ".agentbc-control" / task_id,
        task_id=task_id,
        executor_run_id=RUN_ID,
        session_id=SESSION_ID,
        executor="claude",
    )
    # The executor persists the official session receipt before the turn
    # (production path); the control plane refuses approvals without it.
    if with_receipt:
        plane.record_session_started(
            {
                "version": 1,
                "executor": "claude",
                "session_id": SESSION_ID,
                "resumed": False,
                "persistence": "persistent",
                "source": "preallocated",
            }
        )
    return plane


def _run_session(
    transport: ClaudeSDKControlTransport,
    client: _FakeSDKClient,
    *,
    verify: object = None,
) -> dict:
    """Drive the full production session path: run_controlled (which owns
    the terminal-state durable revocation) over the fake official client."""

    async def scenario() -> dict:
        captured = await transport._run_session_coroutine(
            object(), "prompt", verify, None  # type: ignore[arg-type]
        )
        # run_controlled's finally-block (terminal-state revocation) runs in
        # production even on success; mirror that here.
        transport._revoke_consumed_grant("claude_run_terminal")
        return captured

    with mock.patch("claude_agent_sdk.ClaudeSDKClient", lambda options: client):
        return asyncio.run(scenario())


def _hooked_transport(harness: _Harness, transport: ClaudeSDKControlTransport):
    """Attach the executor's real hook feed, wired to the transport sink."""
    hooks = build_sdk_hooks(harness.control_root, event_sink=transport)
    # The executor binds the hook log to the official session before the
    # prompt (production path in _build_sdk_options_for_task).
    assert bind_hook_log_session(harness.control_root, SESSION_ID)
    return hooks


def _fire_hook(hooks: dict, event: str, tool_use_id: str, tool_name: str = "Bash"):
    """Deliver one official-shaped hook wire dict through the SDK hook feed.

    Safe to call from inside a running event loop (the async session driver
    scenario) or from plain test code.
    """
    payload = {
        "hook_event_name": event,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": {"command": "echo hi"},
    }
    if event == "PostToolUse":
        payload["tool_response"] = "ok"

    async def fire() -> None:
        for matcher in hooks.get(event) or []:
            for hook in matcher.hooks:
                await hook(payload, tool_use_id, {"signal": None})

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(fire())
        return

    task = loop.create_task(fire())
    # Best-effort inline completion when a loop is already running: the
    # awaiting test coroutine drives it on the next await point.
    _PENDING_HOOK_TASKS.append(task)


_PENDING_HOOK_TASKS: list = []


async def _drain_hook(hooks: dict, event: str, tool_use_id: str, tool_name: str = "Bash"):
    """Fire one hook event and await it inline (for async test scenarios)."""
    payload = {
        "hook_event_name": event,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": {"command": "echo hi"},
    }
    if event == "PostToolUse":
        payload["tool_response"] = "ok"
    for matcher in hooks.get(event) or []:
        for hook in matcher.hooks:
            await hook(payload, tool_use_id, {"signal": None})


def _kind_handle(pending: dict, kind: str) -> str:
    """Resolve the opaque handle of the first offered choice with this kind."""
    return next(
        choice["handle"]
        for choice in pending.get("offered_choices") or []
        if choice.get("kind") == kind
    )


async def _respond_when_pending(
    transport: ClaudeSDKControlTransport,
    harness: _Harness,
    decision: str,
) -> dict:
    """Wait for the real ControlPlane request and propagate response errors.

    GGQN-002 originally used detached daemon threads here.  An exception in
    ``respond_approval`` therefore printed a traceback while unittest still
    reported success.  Running the blocking response through ``to_thread``
    and awaiting the task makes every control-plane failure fail the test.
    PERM-104-002 v2: the response must select an explicit native choice
    handle; flattened approve/deny is rejected.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    plane = transport.plane
    while loop.time() < deadline:
        pending = (plane.status() or {}).get("pending_request") or {}
        if pending.get("status") == "pending":
            kind = "deny" if decision == "decline" else "once"
            return await asyncio.to_thread(
                plane.respond_approval,
                harness.task_id,
                RUN_ID,
                SESSION_ID,
                str(pending.get("request_id") or ""),
                decision,
                choice_handle=_kind_handle(pending, kind),
            )
        await asyncio.sleep(0.02)
    raise AssertionError("ControlPlane approval request did not become pending")


class ProductionSessionWiringTests(unittest.TestCase):
    """Real transport session path with a fake official SDK client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _transport(self, harness: _Harness) -> ClaudeSDKControlTransport:
        transport = ClaudeSDKControlTransport(
            plane=_plane(self.root, harness.task_id),
            task_id=harness.task_id,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            escalation_domain="executor_policy",
            host_profile_digest="sha256:" + "1" * 64,
        )
        self.addCleanup(transport.stop)
        return transport

    # ── safe single-action approve ────────────────────────────────────────

    def test_safe_approve_binds_exact_identity_and_verifies(self) -> None:
        harness = _Harness(self.root, with_runtime_record=True)
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        approved_id = f"call_{uuid.uuid4().hex[:16]}"

        # The can_use_tool decision lands through the frozen ControlPlane;
        # await the responder so its exceptions are part of the test result.
        async def scenario() -> dict:
            client = _FakeSDKClient()
            # The approved identity becomes the anchor BEFORE the session
            # drive: can_use_tool runs its ControlPlane wait, the approved
            # identity becomes the verification anchor, then the exact
            # PostToolUse success for THAT identity arrives through the real
            # hook feed during the driven session.
            approval = asyncio.create_task(
                transport.can_use_tool(
                    "Bash", {"command": "echo hi"},
                    mock.MagicMock(tool_use_id=approved_id),
                )
            )
            responder = asyncio.create_task(
                _respond_when_pending(transport, harness, "accept")
            )
            await asyncio.gather(approval, responder)
            await _drain_hook(hooks, "PreToolUse", approved_id)
            await _drain_hook(hooks, "PostToolUse", approved_id)
            client.messages.append(_result_message(is_error=False))
            await transport._drive_session(client, "prompt", None, None)
            return responder.result()

        response = asyncio.run(scenario())
        self.assertEqual(response["decision"], "accept")
        ledger = load_block_ledger(transport.plane.root)
        self.assertEqual(
            {entry["decision"] for entry in ledger["entries"].values()},
            {"approve"},
        )
        captured_harness = harness.verifier(transport)
        outcome = captured_harness(approved_id, {
            "result": {"is_error": False},
        })
        self.assertTrue(outcome["verified"], outcome)
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["state"]["status"], "verified")

    def test_safe_deny_never_verifies(self) -> None:
        harness = _Harness(self.root, with_runtime_record=True)
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        denied_id = f"call_{uuid.uuid4().hex[:16]}"

        async def scenario() -> dict:
            client = _FakeSDKClient()
            # The deny decision flows through the ControlPlane; the denied
            # call never executes, so NO PostToolUse success may exist for
            # the denied identity — only a PreToolUse trace.
            approval = asyncio.create_task(
                transport.can_use_tool(
                    "Bash", {"command": "echo hi"},
                    mock.MagicMock(tool_use_id=denied_id),
                )
            )
            responder = asyncio.create_task(
                _respond_when_pending(transport, harness, "decline")
            )
            await asyncio.gather(approval, responder)
            await _drain_hook(hooks, "PreToolUse", denied_id)
            client.messages.append(_result_message(is_error=False))
            await transport._drive_session(client, "prompt", None, None)
            return responder.result()

        response = asyncio.run(scenario())
        self.assertEqual(response["decision"], "decline")
        ledger = load_block_ledger(transport.plane.root)
        self.assertEqual(
            {entry["decision"] for entry in ledger["entries"].values()},
            {"deny"},
        )
        outcome = harness.verifier(transport)(denied_id, {"result": {"is_error": False}})
        self.assertFalse(outcome["verified"])
        self.assertEqual(
            outcome["reason"], "claude_sdk_post_tool_use_success_missing"
        )

    def test_response_file_is_last_commit_and_failure_rolls_back(self) -> None:
        """A failed response write exposes neither approval nor ledger state."""
        from agent_bridge_connect import control as control_module

        harness = _Harness(self.root, with_runtime_record=True)
        transport = self._transport(harness)
        tool_use_id = f"call_{uuid.uuid4().hex[:16]}"

        async def scenario() -> Path:
            approval = asyncio.create_task(
                transport.can_use_tool(
                    "Bash",
                    {"command": "echo hi"},
                    mock.MagicMock(tool_use_id=tool_use_id),
                )
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            pending: dict = {}
            while loop.time() < deadline:
                pending = (transport.plane.status() or {}).get("pending_request") or {}
                if pending.get("status") == "pending":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(pending.get("status"), "pending")
            response_path = transport.plane._response_path(
                str(pending.get("request_id") or "")
            )
            original_atomic_write = control_module.atomic_write_json

            def fail_response_write(path: object, value: dict) -> Path:
                if Path(path).resolve() == response_path.resolve():
                    raise OSError("simulated response commit failure")
                return original_atomic_write(path, value)

            with mock.patch.object(
                control_module, "atomic_write_json", side_effect=fail_response_write
            ):
                with self.assertRaises(OSError):
                    await asyncio.to_thread(
                        transport.plane.respond_approval,
                        harness.task_id,
                        RUN_ID,
                        SESSION_ID,
                        str(pending.get("request_id") or ""),
                        "accept",
                        choice_handle=_kind_handle(pending, "once"),
                    )
            rollback_evidence = {
                "response_exists": response_path.exists(),
                "pending_status": (transport.plane.status() or {})
                .get("pending_request", {})
                .get("status"),
                "ledger": load_block_ledger(transport.plane.root)["entries"],
            }
            # Release the still-blocked transport through the real plane so
            # asyncio does not leave its blocking wait_for_decision thread
            # alive after the rollback assertions were captured.
            await asyncio.to_thread(
                transport.plane.respond_approval,
                harness.task_id,
                RUN_ID,
                SESSION_ID,
                str(pending.get("request_id") or ""),
                "decline",
                choice_handle=_kind_handle(pending, "deny"),
            )
            await approval
            return rollback_evidence

        rollback = asyncio.run(scenario())
        self.assertFalse(rollback["response_exists"])
        self.assertEqual(rollback["pending_status"], "pending")
        self.assertEqual(rollback["ledger"], {})

    def test_distinct_actions_are_independent_and_identical_retry_converges(self) -> None:
        """Action identity includes tool input but excludes the per-call id."""
        harness = _Harness(self.root, with_runtime_record=True)
        transport = self._transport(harness)

        async def decide(command: str, tool_use_id: str, decision: str) -> object:
            approval = asyncio.create_task(
                transport.can_use_tool(
                    "Bash",
                    {"command": command},
                    mock.MagicMock(tool_use_id=tool_use_id),
                )
            )
            responder = asyncio.create_task(
                _respond_when_pending(transport, harness, decision)
            )
            await asyncio.gather(approval, responder)
            return approval.result()

        async def scenario() -> str:
            await decide("echo first", "call_distinct_1", "accept")
            # A different Bash input remains independently approvable.
            await decide("echo second", "call_distinct_2", "decline")
            # Retrying the first action under a fresh native call identity
            # converges without creating a third permission input.
            try:
                await transport.can_use_tool(
                    "Bash",
                    {"command": "echo first"},
                    mock.MagicMock(tool_use_id="call_distinct_3"),
                )
            except ClaudeSDKTransportError as exc:
                return exc.code
            return ""

        self.assertEqual(
            asyncio.run(scenario()), "permission_escalation_ineffective"
        )
        decisions = sorted(
            entry["decision"]
            for entry in load_block_ledger(transport.plane.root)["entries"].values()
        )
        self.assertEqual(decisions, ["approve", "deny"])

    # ── explicit full / inherited full ────────────────────────────────────

    def test_explicit_full_declared_run_verifies_without_approval(self) -> None:
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="full"),
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        tool_id = f"call_{uuid.uuid4().hex[:16]}"
        client = _FakeSDKClient()
        _fire_hook(hooks, "PreToolUse", tool_id)
        _fire_hook(hooks, "PostToolUse", tool_id)
        client.messages.append(_result_message(is_error=False))
        captured = _run_session(transport, client, verify=harness.verifier(transport))
        # The bypass run never had an approval request; the declared-run
        # anchor plus the successful target tool event verifies anyway.
        self.assertTrue(captured is not None)
        self.assertFalse(client.mode_calls, "no setMode for explicit full")
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertEqual(persisted["state"]["status"], "verified")

    def test_inherited_full_declared_run_verifies_without_approval(self) -> None:
        harness = _Harness(
            self.root,
            permission={
                "requested_mode": "full",
                "effective_mode": "full",
                "selection_source": "inherited_task",
                "base_mode": "full",
                "resolution_state": "frozen",
                "approval_policy": "none",
            },
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        tool_id = f"call_{uuid.uuid4().hex[:16]}"
        client = _FakeSDKClient()
        _fire_hook(hooks, "PostToolUse", tool_id)
        client.messages.append(_result_message(is_error=False))
        _run_session(transport, client, verify=harness.verifier(transport))
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertEqual(persisted["state"]["status"], "verified")

    def test_declared_run_without_post_tool_use_never_verifies(self) -> None:
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="full"),
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        client = _FakeSDKClient()
        client.messages.append(_result_message(is_error=False))
        _run_session(transport, client, verify=harness.verifier(transport))
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertEqual(persisted["state"]["status"], "activated")

    # ── fail-closed identities ────────────────────────────────────────────

    def test_wrong_and_unrelated_and_failure_events_never_verify(self) -> None:
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="full"),
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        client = _FakeSDKClient()
        # A PreToolUse and a PostToolUseFailure are never success evidence.
        _fire_hook(hooks, "PreToolUse", "call_pre_1")
        _fire_hook(hooks, "PostToolUseFailure", "call_fail_1")
        client.messages.append(_result_message(is_error=False))
        _run_session(transport, client, verify=harness.verifier(transport))
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertEqual(persisted["state"]["status"], "activated")

    def test_duplicate_post_tool_use_identity_never_verifies(self) -> None:
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="full"),
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        tool_id = f"call_{uuid.uuid4().hex[:16]}"
        client = _FakeSDKClient()
        # The same native identity observed twice in one run window is a
        # duplicate/replay: nothing may verify.
        _fire_hook(hooks, "PostToolUse", tool_id)
        _fire_hook(hooks, "PostToolUse", tool_id)
        client.messages.append(_result_message(is_error=False))
        _run_session(transport, client, verify=harness.verifier(transport))
        self.assertIsNone(
            transport.select_verification_event(session_id=SESSION_ID)
        )

    def test_cross_run_post_tool_use_never_verifies(self) -> None:
        """A foreign-session tool event can never verify this session."""
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="full"),
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        tool_id = f"call_{uuid.uuid4().hex[:16]}"
        client = _FakeSDKClient()
        _fire_hook(hooks, "PostToolUse", tool_id)
        client.messages.append(_result_message(is_error=False))
        # Overwrite the log binding with a DIFFERENT official session (a
        # cross-run replay of the hook log).  The verifying run then binds to
        # its own session and must refuse the foreign evidence.
        assert bind_hook_log_session(harness.control_root, str(uuid.uuid4()))
        outcome = harness.verifier(transport)(f"run:{RUN_ID}", {"result": {"is_error": False}})
        self.assertFalse(outcome["verified"])

    def test_transport_death_never_verifies(self) -> None:
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="full"),
            with_runtime_record=True,
        )
        transport = self._transport(harness)
        hooks = _hooked_transport(harness, transport)
        transport.declare_run_authorization(mode="declared_run")
        tool_id = f"call_{uuid.uuid4().hex[:16]}"
        client = _FakeSDKClient()
        client.stream_dead = True
        _fire_hook(hooks, "PostToolUse", tool_id)
        captured = _run_session(transport, client, verify=harness.verifier(transport))
        # A dead stream produces no structured ResultMessage: the captured
        # result is not structured success, so nothing may verify.
        self.assertTrue(captured["result"].get("is_error") or not captured["result"])
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertEqual(persisted["state"]["status"], "activated")

    # ── temporary full ────────────────────────────────────────────────────

    def test_temporary_full_session_scoped_setmode_and_durable_revoke(self) -> None:
        grant = build_permission_grant(
            executor="claude",
            task_id="GGQW-002",
            input_id="input-1",
            session_id=str(uuid.uuid4()),
            source_run_id="run-source",
        )
        harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="safe"),
            grant=grant,
            with_runtime_record=True,
        )
        # The Runner consumed the grant for this exact run (durable write)
        # before the transport takes over.
        model = harness.service.get_task(harness.task_id)
        extensions = dict(model.extensions or {})
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = consume_permission_grant(grant, RUN_ID)
        model.extensions = extensions
        harness.service.store.write_task(model.id, model.to_dict())
        transport = ClaudeSDKControlTransport(
            plane=_plane(self.root, harness.task_id),
            task_id=harness.task_id,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            grant_revoke_callback=harness.durable_revoker(),
        )
        self.addCleanup(transport.stop)
        hooks = _hooked_transport(harness, transport)
        transport.attach_consumed_grant(consume_permission_grant(grant, RUN_ID))
        tool_id = f"call_{uuid.uuid4().hex[:16]}"
        client = _FakeSDKClient()
        _fire_hook(hooks, "PostToolUse", tool_id)
        client.messages.append(_result_message(is_error=False))
        captured = _run_session(transport, client, verify=harness.verifier(transport))
        # The session-scoped bypass was applied inside the same live session,
        # BEFORE the prompt.
        self.assertEqual(client.mode_calls, ["bypassPermissions"])
        self.assertTrue(captured.get("session_mode_applied"))
        # The successful tool event verified the consumed-grant run.
        service = TaskService(
            harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        persisted = permission_runtime_from_extensions(
            service.get_task(harness.task_id).extensions,
            task_id=harness.task_id,
            executor_run_id=RUN_ID,
        )
        self.assertEqual(persisted["state"]["status"], "verified")
        # Durable revocation through the store: the exact grant is revoked.
        reloaded = permission_grant_from_extensions(
            service.get_task(harness.task_id).extensions
        )
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["state"]["status"], "revoked")
        self.assertEqual(reloaded["audit"]["revocation_code"], "claude_run_terminal")


class DurableGrantLifecycleTests(unittest.TestCase):
    """Reload through TaskService after terminal/timeout/crash: revoked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.grant = build_permission_grant(
            executor="claude",
            task_id="GGQW-002",
            input_id="input-1",
            session_id=str(uuid.uuid4()),
            source_run_id="run-source",
        )
        self.harness = _Harness(
            self.root,
            permission=build_permission_record(explicit_mode="safe"),
            grant=self.grant,
        )
        self.harness.service.store.write_task(
            self.harness.task_id, self.harness.service.get_task(self.harness.task_id).to_dict()
        )
        # The Runner consumed the grant for this exact run (durable write).
        model = self.harness.service.get_task(self.harness.task_id)
        extensions = dict(model.extensions or {})
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = consume_permission_grant(
            self.grant, RUN_ID
        )
        model.extensions = extensions
        self.harness.service.store.write_task(model.id, model.to_dict())

    def _persisted_state(self) -> str:
        service = TaskService(
            self.harness.board,
            config={"workspace_root": str(self.root), "permission_mode": "safe"},
        )
        reloaded = permission_grant_from_extensions(
            service.get_task(self.harness.task_id).extensions
        )
        self.assertIsNotNone(reloaded)
        return str(reloaded["state"]["status"])

    def _transport(self) -> ClaudeSDKControlTransport:
        transport = ClaudeSDKControlTransport(
            plane=_plane(self.root, self.harness.task_id),
            task_id=self.harness.task_id,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            grant_revoke_callback=self.harness.durable_revoker(),
        )
        self.addCleanup(transport.stop)
        transport.attach_consumed_grant(
            consume_permission_grant(self.grant, RUN_ID)
        )
        return transport

    def test_terminal_success_revokes_durably(self) -> None:
        transport = self._transport()
        client = _FakeSDKClient()
        client.messages.append(_result_message(is_error=False))
        _run_session(transport, client)
        self.assertEqual(self._persisted_state(), "revoked")

    def test_timeout_revokes_durably(self) -> None:
        transport = self._transport()

        def _fail(*args: object, **kwargs: object) -> dict:
            raise TimeoutError("run timeout")

        with mock.patch.object(transport, "_submit", side_effect=_fail):
            with self.assertRaises(TimeoutError):
                transport.run_controlled(options=object(), prompt="p", timeout_s=1.0)
        self.assertEqual(self._persisted_state(), "revoked")

    def test_crash_revokes_durably(self) -> None:
        transport = self._transport()

        def _fail(*args: object, **kwargs: object) -> dict:
            raise ClaudeSDKTransportError("claude_sdk_transport_dead", "dead")

        with mock.patch.object(transport, "_submit", side_effect=_fail):
            with self.assertRaises(ClaudeSDKTransportError):
                transport.run_controlled(options=object(), prompt="p", timeout_s=1.0)
        self.assertEqual(self._persisted_state(), "revoked")

    def test_wrong_grant_identity_fails_closed(self) -> None:
        """A revocation for a different run identity must not land."""
        # The durable callback re-checks the persisted binding (grant id AND
        # target-run binding); a transport holding a grant bound to a
        # different run must raise fail-closed instead of revoking the real
        # run's grant.
        broken = ClaudeSDKControlTransport(
            plane=_plane(self.root, self.harness.task_id, with_receipt=False),
            task_id=self.harness.task_id,
            run_id="claude-other-run",
            session_id=SESSION_ID,
            grant_revoke_callback=self.harness.durable_revoker(),
        )
        self.addCleanup(broken.stop)
        broken.attach_consumed_grant(consume_permission_grant(self.grant, "claude-other-run"))
        with self.assertRaises(ClaudeSDKTransportError) as raised:
            broken._revoke_consumed_grant("claude_run_terminal")
        self.assertEqual(raised.exception.code, "claude_sdk_grant_revoke_failed")
        # The persisted grant for the REAL run stays consumed (not revoked).
        self.assertEqual(self._persisted_state(), "consumed")

    def test_persistence_failure_raises_instead_of_silence(self) -> None:
        def _boom(grant: dict, code: str) -> None:
            raise OSError("disk on fire")

        broken = ClaudeSDKControlTransport(
            plane=_plane(self.root, self.harness.task_id, with_receipt=False),
            task_id=self.harness.task_id,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            grant_revoke_callback=_boom,
        )
        self.addCleanup(broken.stop)
        broken.attach_consumed_grant(consume_permission_grant(self.grant, RUN_ID))
        with self.assertRaises(OSError):
            broken._revoke_consumed_grant("claude_run_terminal")


if __name__ == "__main__":
    unittest.main()
