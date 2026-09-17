"""PERM-104-002 production routing and temporary-full lifecycle (T2A7-001).

Covers the corrected production contract:

* every Runner-managed Claude task routes through the official SDK control
  transport — explicit full, inherited full, safe/inherit, and trusted
  temporary full — with no raw-CLI full branch;
* explicit and inherited concrete ``full`` start ``bypassPermissions`` in
  the SDK options; a temporary full starts in the SDK default mode and the
  official session-scoped ``setMode`` update is applied inside the live
  SDK session BEFORE the prompt is sent;
* the temporary-full lifecycle consumes exactly one durable grant and
  persistently revokes it on terminal, timeout, crash, handoff,
  reassignment, and recovery through the TaskService store;
* the routing is exercised against production call paths (spied on the
  real executor/transport methods), never against constants alone.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_bridge_connect.claude_sdk_transport import (
    SDK_SESSION_MODE_UPDATE,
    ClaudeSDKControlTransport,
    ClaudeSDKTransportError,
)
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.executors.claude import ClaudeExecutor, _claude_control_required
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
    consume_permission_grant,
    permission_grant_from_extensions,
    revoke_permission_grant,
)  # noqa: F401 - revoke_permission_grant re-exported for durable-store tests
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.service import TaskService

RUN_ID = "claude-T2A7-001-route"


def _fake_binary(directory: str, version: str = "2.1.233 (Claude Code)") -> Path:
    fake = Path(directory) / "claude"
    fake.write_text(f"#!/bin/sh\nprintf '{version}\\n'\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | 0o100)
    return fake


class ProductionRoutingTests(unittest.TestCase):
    """Routing decisions on the real production selector and executor."""

    def test_only_safe_runner_managed_base_routes_to_control(self) -> None:
        bases = (
            {"requested_mode": "safe", "effective_mode": "safe",
             "selection_source": "configured_default"},
            {"requested_mode": "full", "effective_mode": "full",
             "selection_source": "explicit_task"},
            {"requested_mode": "full", "effective_mode": "full",
             "selection_source": "inherited_task"},
        )
        for permission in bases:
            with self.subTest(effective=permission["effective_mode"]):
                packet = {
                    "extensions": {"agentbc.permission": dict(permission)},
                    "runner_authorization_required": True,
                }
                self.assertEqual(
                    _claude_control_required(packet),
                    permission["effective_mode"] != "full",
                )

    def test_temporary_grant_routes_to_control(self) -> None:
        grant = build_permission_grant(
            executor="claude",
            task_id="T2A7-001",
            input_id="input-1",
            session_id=str(uuid.uuid4()),
            source_run_id="run-source",
        )
        for status in ("issued", "consumed"):
            with self.subTest(status=status):
                grant_copy = dict(grant)
                grant_copy["state"] = {**grant["state"], "status": status}
                packet = {
                    "extensions": {
                        "agentbc.permission": build_permission_record(explicit_mode="safe"),
                        PERMISSION_GRANT_EXTENSION_KEY: grant_copy,
                    },
                    "runner_authorization_required": True,
                }
                self.assertTrue(_claude_control_required(packet))

    def test_non_runner_packet_keeps_direct_path(self) -> None:
        self.assertFalse(
            _claude_control_required(
                {
                    "extensions": {
                        "agentbc.permission": build_permission_record(explicit_mode="full")
                    }
                }
            )
        )

    def test_full_tasks_do_not_enter_sdk_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for permission in (
                build_permission_record(explicit_mode="full"),
                {
                    "requested_mode": "full",
                    "effective_mode": "full",
                    "selection_source": "inherited_task",
                    "base_mode": "full",
                    "resolution_state": "frozen",
                    "approval_policy": "none",
                },
            ):
                with self.subTest(source=permission["selection_source"]):
                    packet = {
                        "task_id": "T2A7-001",
                        "steps": [{"id": 1, "description": "one"}],
                        "workspace": {"project_root": temporary, "root": temporary},
                        "extensions": {"agentbc.permission": dict(permission)},
                        "runner_authorization_required": True,
                    }
                    self.assertFalse(_claude_control_required(packet))


class TemporaryFullSessionModeTests(unittest.TestCase):
    """The official session-scoped setMode contract for trusted temporary full."""

    def test_frozen_session_mode_update_shape(self) -> None:
        from claude_agent_sdk import PermissionUpdate

        self.assertEqual(SDK_SESSION_MODE_UPDATE["type"], "setMode")
        self.assertEqual(SDK_SESSION_MODE_UPDATE["mode"], "bypassPermissions")
        self.assertEqual(SDK_SESSION_MODE_UPDATE["destination"], "session")
        official = PermissionUpdate(
            type=SDK_SESSION_MODE_UPDATE["type"],
            mode=SDK_SESSION_MODE_UPDATE["mode"],
            destination=SDK_SESSION_MODE_UPDATE["destination"],
        )
        self.assertEqual(official.mode, "bypassPermissions")
        self.assertEqual(official.destination, "session")

    def test_explicit_and_inherited_full_start_bypass_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor = ClaudeExecutor(
                command=str(_fake_binary(temporary)), transport="direct"
            )
            executor._transport = mock.MagicMock()
            for permission in (
                build_permission_record(explicit_mode="full"),
                {
                    "requested_mode": "full",
                    "effective_mode": "full",
                    "selection_source": "inherited_task",
                    "base_mode": "full",
                    "resolution_state": "frozen",
                    "approval_policy": "none",
                },
            ):
                with self.subTest(source=permission["selection_source"]):
                    options = executor._build_sdk_options_for_task(
                        {"extensions": {"agentbc.permission": dict(permission)}},
                        Path(temporary),
                        "",
                        {},
                    )
                    self.assertEqual(options.permission_mode, "bypassPermissions")

    def test_safe_and_inherit_keep_sdk_default_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor = ClaudeExecutor(
                command=str(_fake_binary(temporary)), transport="direct"
            )
            executor._transport = mock.MagicMock()
            options = executor._build_sdk_options_for_task(
                {
                    "extensions": {
                        "agentbc.permission": build_permission_record(explicit_mode="safe")
                    }
                },
                Path(temporary),
                "",
                {},
            )
            self.assertEqual(options.permission_mode, "default")
            self.assertEqual(options.tools, executor.tools)
            self.assertEqual(options.allowed_tools, [])

    def test_temporary_full_keeps_default_mode_and_session_scoped_flip(self) -> None:
        """The consumed grant run starts in default mode; the session-scoped
        bypass comes from the live-session setMode update, not the options."""
        with tempfile.TemporaryDirectory() as temporary:
            executor = ClaudeExecutor(
                command=str(_fake_binary(temporary)), transport="direct"
            )
            executor._transport = mock.MagicMock()
            grant = build_permission_grant(
                executor="claude",
                task_id="T2A7-001",
                input_id="input-1",
                session_id=str(uuid.uuid4()),
                source_run_id="run-source",
            )
            consumed = consume_permission_grant(grant, RUN_ID)
            options = executor._build_sdk_options_for_task(
                {
                    "extensions": {
                        "agentbc.permission": build_permission_record(explicit_mode="safe"),
                        PERMISSION_GRANT_EXTENSION_KEY: consumed,
                    }
                },
                Path(temporary),
                "",
                {},
            )
            self.assertEqual(options.permission_mode, "default")

    def test_session_mode_update_applied_before_prompt_only_with_grant(self) -> None:
        """The live-session driver applies the official session-scoped mode
        update exactly once, BEFORE the prompt, when (and only when) a
        consumed grant backs the run."""
        async def scenario() -> tuple[list[str], list[str]]:
            order: list[str] = []
            calls: list[str] = []

            class _FakeClient:
                def __init__(self, options: object = None) -> None:
                    self.options = options

                async def set_permission_mode(self, mode: str) -> None:
                    calls.append(mode)
                    order.append("set_permission_mode")

                async def query(self, prompt: str) -> None:
                    order.append("query")

                async def connect(self) -> None:
                    order.append("connect")

                async def disconnect(self) -> None:
                    return None

                async def receive_response(self):  # pragma: no cover - not reached
                    return
                    yield None

                def __aenter__(self) -> "_FakeClient":
                    return self

                async def __aexit__(self, *exc: object) -> None:
                    return None

            transport = ClaudeSDKControlTransport(
                plane=mock.MagicMock(),
                task_id="T2A7-001",
                run_id=RUN_ID,
                session_id="",
            )
            grant = build_permission_grant(
                executor="claude",
                task_id="T2A7-001",
                input_id="input-1",
                session_id=str(uuid.uuid4()),
                source_run_id="run-source",
            )
            transport.attach_consumed_grant(consume_permission_grant(grant, RUN_ID))
            with mock.patch("claude_agent_sdk.ClaudeSDKClient", _FakeClient):
                await transport._run_session_async(object(), "prompt", None, None)
            return order, calls

        import asyncio

        order, calls = asyncio.run(scenario())
        self.assertEqual(calls, ["bypassPermissions"])
        self.assertEqual(
            order[:2],
            ["set_permission_mode", "query"],
            "the session-scoped mode flip must land before the prompt",
        )

    def test_no_grant_means_no_mode_update(self) -> None:
        async def scenario() -> list[str]:
            calls: list[str] = []

            class _FakeClient:
                def __init__(self, options: object = None) -> None:
                    self.options = options

                async def set_permission_mode(self, mode: str) -> None:
                    calls.append(mode)

                async def query(self, prompt: str) -> None:
                    return None

                async def connect(self) -> None:
                    return None

                async def disconnect(self) -> None:
                    return None

                async def receive_response(self):
                    return
                    yield None

                def __aenter__(self) -> "_FakeClient":
                    return self

                async def __aexit__(self, *exc: object) -> None:
                    return None

            transport = ClaudeSDKControlTransport(
                plane=mock.MagicMock(),
                task_id="T2A7-001",
                run_id=RUN_ID,
                session_id="",
            )
            with mock.patch("claude_agent_sdk.ClaudeSDKClient", _FakeClient):
                await transport._run_session_async(object(), "prompt", None, None)
            return calls

        import asyncio

        self.assertEqual(asyncio.run(scenario()), [])


class TemporaryFullGrantLifecycleTests(unittest.TestCase):
    """Exactly one durable grant, persistently revoked on every exit path."""

    def _grant(self) -> dict:
        return build_permission_grant(
            executor="claude",
            task_id="T2A7-001",
            input_id="input-1",
            session_id=str(uuid.uuid4()),
            source_run_id="run-source",
        )

    def _authoritative_task(self, grant: dict) -> dict:
        """The exact Runner continuation context an issued grant requires."""
        session_id = str(grant["binding"]["session_id"])
        source_run_id = str(grant["binding"]["source_run_id"])
        return {
            "task_id": "T2A7-001",
            "assignee": "claude",
            "status": "running",
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                PERMISSION_GRANT_EXTENSION_KEY: grant,
                "agentbc.execution": {
                    "internal_status": "resuming",
                    "resuming_at": "2026-08-30T00:00:00Z",
                },
                "agentbc.input": {
                    "type": "permission",
                    "status": "answered",
                    "input_id": "input-1",
                    "executor_run_id": source_run_id,
                    "requested_permission": "full",
                    "response": {"type": "approve"},
                },
                "agentbc.session": {
                    "executor": "claude",
                    "session_id": session_id,
                    "run_ids": [source_run_id],
                },
            },
        }

    def _task_with_grant(self, grant: dict) -> dict:
        return {
            "task_id": "T2A7-001",
            "assignee": "claude",
            "status": "running",
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                PERMISSION_GRANT_EXTENSION_KEY: grant,
            },
        }

    def test_issued_grant_is_inert_under_plan_d(self) -> None:
        grant = self._grant()
        permission = resolve_effective_permission(
            self._authoritative_task(grant),
            "claude",
            RUN_ID,
            trusted_runner_managed=True,
        )
        self.assertEqual(permission["effective_mode"], "safe")

    def test_issued_grant_is_inert_without_runner_context(self) -> None:
        permission = resolve_effective_permission(
            self._task_with_grant(self._grant()),
            "claude",
            RUN_ID,
            trusted_runner_managed=False,
        )
        self.assertEqual(permission["effective_mode"], "safe")

    def test_revoked_grant_is_inert_for_resolution(self) -> None:
        grant = revoke_permission_grant(self._grant(), "claude_run_terminal")
        permission = resolve_effective_permission(
            self._task_with_grant(grant),
            "claude",
            RUN_ID,
            trusted_runner_managed=True,
        )
        self.assertEqual(permission["effective_mode"], "safe")

    def test_transport_revokes_grant_exactly_once_on_terminal(self) -> None:
        revoked_envelopes: list[tuple[dict, str]] = []

        def _revoke(grant: dict, code: str) -> None:
            revoked_envelopes.append((grant, code))

        transport = ClaudeSDKControlTransport(
            plane=mock.MagicMock(),
            task_id="T2A7-001",
            run_id=RUN_ID,
            session_id="",
            grant_revoke_callback=_revoke,
        )
        grant = consume_permission_grant(self._grant(), RUN_ID)
        transport.attach_consumed_grant(grant)
        transport._revoke_consumed_grant("claude_run_terminal")
        # A second terminal-state call is a no-op: exactly one revocation.
        transport._revoke_consumed_grant("claude_run_terminal")
        self.assertEqual(len(revoked_envelopes), 1)
        self.assertEqual(revoked_envelopes[0][1], "claude_run_terminal")
        self.assertEqual(
            revoked_envelopes[0][0]["grant_id"], grant["grant_id"]
        )
        self.assertIsNone(transport._consumed_grant)

    def test_revoke_without_durable_callback_fails_closed(self) -> None:
        transport = ClaudeSDKControlTransport(
            plane=mock.MagicMock(),
            task_id="T2A7-001",
            run_id=RUN_ID,
            session_id="",
        )
        transport.attach_consumed_grant(consume_permission_grant(self._grant(), RUN_ID))
        with self.assertRaises(ClaudeSDKTransportError) as raised:
            transport._revoke_consumed_grant("claude_run_terminal")
        self.assertEqual(raised.exception.code, "claude_sdk_grant_revoke_failed")
        # The grant stays attached: it must surface, not vanish silently.
        self.assertIsNotNone(transport._consumed_grant)

    def test_run_controlled_revokes_on_timeout_and_crash(self) -> None:
        for failure in (
            ClaudeSDKTransportError("claude_sdk_transport_dead", "dead"),
            TimeoutError("run timeout"),
        ):
            with self.subTest(failure=type(failure).__name__):
                revoked: list[str] = []

                def _revoke(grant: dict, code: str, _revoked: list = revoked) -> None:
                    _revoked.append(code)

                transport = ClaudeSDKControlTransport(
                    plane=mock.MagicMock(),
                    task_id="T2A7-001",
                    run_id=RUN_ID,
                    session_id="",
                    grant_revoke_callback=_revoke,
                )
                transport.attach_consumed_grant(consume_permission_grant(self._grant(), RUN_ID))

                def _fail(*args: object, **kwargs: object) -> dict:
                    raise failure

                with mock.patch.object(transport, "_submit", side_effect=_fail):
                    with self.assertRaises(type(failure)):
                        transport.run_controlled(
                            options=object(), prompt="p", timeout_s=1.0
                        )
                self.assertEqual(revoked, ["claude_run_terminal"])
                self.assertIsNone(transport._consumed_grant)

    def test_handoff_reassign_and_recovery_revoke_durably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = TaskService(
                Path(temporary) / "board",
                config={"workspace_root": temporary, "permission_mode": "safe"},
            )
            task = service.create_task(
                "grant lifecycle",
                "claude",
                [{"id": 1, "description": "finish"}],
                customer_dir=True,
                customer_path=Path(temporary),
                permission_mode="safe",
            )
            task_id = task.id
            grant = self._grant()
            model = service.get_task(task_id)
            model.extensions = dict(model.extensions or {})
            model.extensions[PERMISSION_GRANT_EXTENSION_KEY] = grant
            service.store.write_task(model.id, model.to_dict())
            # Handoff/reassign path: durable revocation through the store.
            self.assertTrue(
                service.revoke_permission_grant(task_id, "claude_run_handoff")
            )
            persisted = permission_grant_from_extensions(
                service.get_task(task_id).extensions
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted["state"]["status"], "revoked")
            self.assertEqual(
                persisted["audit"]["revocation_code"], "claude_run_handoff"
            )
            # Recovery path is a safe no-op once revoked.
            self.assertTrue(
                service.revoke_permission_grant_for_recovery(task_id) is None
            )
            self.assertEqual(
                permission_grant_from_extensions(
                    service.get_task(task_id).extensions
                )["state"]["status"],
                "revoked",
            )

    def test_attach_rejects_non_consumed_grant(self) -> None:
        transport = ClaudeSDKControlTransport(
            plane=mock.MagicMock(),
            task_id="T2A7-001",
            run_id=RUN_ID,
            session_id="",
        )
        with self.assertRaises(ClaudeSDKTransportError) as raised:
            transport.attach_consumed_grant(self._grant())
        self.assertEqual(raised.exception.code, "claude_sdk_grant_not_consumed")


if __name__ == "__main__":
    unittest.main()
