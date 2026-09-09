"""SESSION-104-001 Desktop route and v5 cleanup gate coverage."""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

from agent_bridge_connect.adapters import SessionCleanupRequest
from agent_bridge_connect.codex_desktop_archive import (
    AcknowledgedCodexDesktopArchiveBroker,
    CODEX_DESKTOP_ARCHIVE_REJECTED,
    CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
    CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
    CODEX_DESKTOP_ARCHIVE_UNSUPPORTED,
    CodexDesktopArchiveBroker,
    CodexDesktopRouteContext,
    read_desktop_route_context,
)
from agent_bridge_connect.execution_policy import (
    SESSION_CLEANUP_RECEIPT_VERSION,
    build_session_cleanup_receipt,
    normalize_cleanup_commands,
    session_cleanup_view,
    validate_session_cleanup_receipt,
)
from agent_bridge_connect.executors.codex import CodexExecutor

SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
OTHER_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d7"
DISPATCHER_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d8"
T0 = "2026-09-08T00:00:00Z"


class FakeTransport:
    def __init__(self, messages: list) -> None:
        self.messages = list(messages)
        self.sent: list[dict] = []

    def start(self) -> None:
        return None

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def recv(self, timeout_s: float | None = None) -> dict:
        del timeout_s
        if not self.messages:
            raise TimeoutError("test timeout")
        message = self.messages.pop(0)
        if isinstance(message, BaseException):
            raise message
        return message

    def close(self) -> None:
        return None


class Factory:
    def __init__(self, *transports: FakeTransport) -> None:
        self.transports = list(transports)
        self.calls = 0

    def __call__(self, **_: object) -> FakeTransport:
        self.calls += 1
        return self.transports.pop(0)


def _response(request_id: int, result: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result or {}}


def _route_transport(
    *,
    response: dict | None = None,
    tools: list[str] | None = None,
    start_id: int = 1,
) -> FakeTransport:
    names = tools or ["set_thread_archived", "other"]
    return FakeTransport(
        [
            _response(start_id),
            _response(start_id + 1, {"tools": [{"name": name} for name in names]}),
            response or _response(start_id + 3),
        ]
    )


def _context(dispatcher_thread_id: str = DISPATCHER_ID) -> CodexDesktopRouteContext:
    return CodexDesktopRouteContext(
        pipe_path="/tmp/codex-app-tools-test.pipe",
        dispatcher_thread_id=dispatcher_thread_id,
        mcp_runtime="/Applications/Test.app/Contents/Resources/cua_node/bin/node",
        mcp_resource=(
            "/Applications/Test.app/Contents/Resources/plugins/"
            "openai-bundled/plugins/codex-app-tools/server.mjs"
        ),
        host=socket.gethostname(),
    )


def _archive_transport(*, response: dict | None = None, start_id: int = 3) -> FakeTransport:
    return FakeTransport([_response(start_id), response or _response(start_id + 1)])


def _request(**overrides: object) -> SessionCleanupRequest:
    values: dict[str, object] = {
        "executor": "codex",
        "session_id": SESSION_ID,
        "task_id": "E299-001",
        "executor_run_id": "run-e299",
        "strategy": "official_session_archive_then_delete",
        "receipt_source": "jsonl_thread_started",
        "official_receipt_bound": True,
        "workspace": {"root": str(Path.cwd())},
    }
    values.update(overrides)
    return SessionCleanupRequest(**values)  # type: ignore[arg-type]


class DesktopRouteTests(unittest.TestCase):
    def test_native_control_plane_ack_is_exactly_bound(self) -> None:
        broker = AcknowledgedCodexDesktopArchiveBroker(
            task_id="E299-001",
            executor_run_id="run-e299",
            session_id=SESSION_ID,
        )
        self.assertTrue(broker.route_available())
        self.assertTrue(broker.archive(_request()).acknowledged)
        self.assertEqual(
            broker.archive(_request(session_id=OTHER_ID)).error_code,
            CODEX_DESKTOP_ARCHIVE_REJECTED,
        )

    def test_environment_context_derives_official_resource_from_node(self) -> None:
        env = {
            "CODEX_APP_TOOLS_PIPE_PATH": "/tmp/app-tools.pipe",
            "CODEX_THREAD_ID": DISPATCHER_ID,
            "CODEX_MCP_NODE_PATH": (
                "/Applications/Test.app/Contents/Resources/cua_node/bin/node"
            ),
            "AGENTBC_DESKTOP_RELAY_SOCKET": "/tmp/agentbc-relay.sock",
            "AGENTBC_DESKTOP_RELAY_TOKEN": "relay-token",
        }
        context = read_desktop_route_context(env)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context.mcp_resource,
            "/Applications/Test.app/Contents/Resources/plugins/"
            "openai-bundled/plugins/codex-app-tools/server.mjs",
        )
        self.assertEqual(context.relay_socket, "/tmp/agentbc-relay.sock")
        self.assertEqual(context.relay_token, "relay-token")
        self.assertNotIn("app-tools.pipe", repr(context))
        self.assertIsNone(read_desktop_route_context({**env, "CODEX_THREAD_ID": "wrong"}))
        self.assertIsNone(
            read_desktop_route_context(
                {key: value for key, value in env.items() if key != "CODEX_MCP_NODE_PATH"}
            )
        )

    def test_registration_rejects_unrelated_facade_resource(self) -> None:
        invalid = CodexDesktopRouteContext(
            pipe_path="/tmp/codex-app-tools-test.pipe",
            dispatcher_thread_id=DISPATCHER_ID,
            mcp_runtime="/Applications/Test.app/Contents/Resources/cua_node/bin/node",
            mcp_resource="/tmp/server.mjs",
            host=socket.gethostname(),
        )
        broker = CodexDesktopArchiveBroker(transport_factory=Factory(_route_transport()))
        self.assertEqual(
            broker.register(invalid)["error_code"],
            CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
        )

    def test_registration_negotiates_tools_without_version_allowlist(self) -> None:
        factory = Factory(_route_transport())
        broker = CodexDesktopArchiveBroker(transport_factory=factory)
        registration = broker.register(_context())
        self.assertEqual(registration["capability"], "supported")
        self.assertTrue(registration["route_digest"].startswith("route_"))
        self.assertNotIn("/tmp", str(registration))

    def test_unsupported_and_wrong_thread_responses_fail_closed(self) -> None:
        unsupported = CodexDesktopArchiveBroker(
            transport_factory=Factory(_route_transport(tools=["other"]))
        )
        unsupported.register(_context())
        self.assertEqual(unsupported.archive(_request()).error_code, CODEX_DESKTOP_ARCHIVE_UNSUPPORTED)

        wrong = CodexDesktopArchiveBroker(
            transport_factory=Factory(
                _route_transport(),
                _archive_transport(response=_response(4, {"threadId": OTHER_ID})),
            )
        )
        wrong.register(_context())
        self.assertEqual(wrong.archive(_request()).error_code, CODEX_DESKTOP_ARCHIVE_REJECTED)

    def test_transport_death_keeps_route_retryable_and_duplicate_is_deduplicated(self) -> None:
        dead = CodexDesktopArchiveBroker(
            transport_factory=Factory(
                _route_transport(),
                FakeTransport([RuntimeError("dead")]),
                FakeTransport(
                    [
                        _response(4),
                        _response(5, {"tools": [{"name": "set_thread_archived"}]}),
                        _response(6),
                    ]
                ),
            )
        )
        dead.register(_context())
        self.assertEqual(dead.archive(_request()).error_code, CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST)
        self.assertTrue(dead.route_available())
        self.assertTrue(dead.archive(_request()).acknowledged)

        factory = Factory(_route_transport(), _archive_transport())
        broker = CodexDesktopArchiveBroker(transport_factory=factory)
        broker.register(_context())
        first = broker.archive(_request())
        self.assertEqual(first, broker.archive(_request()))
        self.assertTrue(first.acknowledged)
        self.assertEqual(factory.calls, 2)

    def test_registration_collision_is_retried_during_archive(self) -> None:
        retry_transport = FakeTransport(
            [
                _response(2),
                _response(3, {"tools": [{"name": "set_thread_archived"}]}),
                _response(4),
            ]
        )
        broker = CodexDesktopArchiveBroker(
            transport_factory=Factory(
                FakeTransport([RuntimeError("pipe busy")]),
                retry_transport,
            )
        )
        registration = broker.register(_context())
        self.assertEqual(registration["capability"], "unknown")
        self.assertTrue(registration["route_digest"].startswith("route_"))
        self.assertTrue(broker.route_available())
        self.assertTrue(broker.archive(_request()).acknowledged)
        self.assertEqual(broker.public_status()["capability"], "supported")

    def test_restarted_route_drops_old_cache_and_keeps_long_task_route_usable(self) -> None:
        factory = Factory(
            _route_transport(),
            _archive_transport(start_id=3),
            _route_transport(start_id=5),
            _archive_transport(start_id=7),
            _archive_transport(start_id=9),
        )
        broker = CodexDesktopArchiveBroker(transport_factory=factory)
        broker.register(_context())
        self.assertTrue(broker.archive(_request()).acknowledged)
        self.assertTrue(broker.route_available())
        # A new dispatcher identity represents a restarted Desktop. It must
        # negotiate again and must not reuse the old exact-request cache.
        restarted = broker.register(_context(DISPATCHER_ID[:-1] + "9"))
        self.assertEqual(restarted["capability"], "supported")
        self.assertTrue(broker.archive(_request()).acknowledged)
        self.assertTrue(
            broker.archive(_request(session_id=OTHER_ID)).acknowledged
        )
        self.assertEqual(factory.calls, 5)

    def test_archive_timeout_and_tool_rejection_are_bounded(self) -> None:
        timeout = CodexDesktopArchiveBroker(
            transport_factory=Factory(
                _route_transport(),
                _archive_transport(response=TimeoutError("timeout")),
            )
        )
        timeout.register(_context())
        self.assertEqual(
            timeout.archive(_request()).error_code,
            CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
        )
        rejected = CodexDesktopArchiveBroker(
            transport_factory=Factory(
                _route_transport(),
                _archive_transport(response=_response(4, {"isError": True})),
            )
        )
        rejected.register(_context())
        self.assertEqual(
            rejected.archive(_request()).error_code,
            CODEX_DESKTOP_ARCHIVE_REJECTED,
        )

    def test_unregistered_route_is_bounded_unavailable(self) -> None:
        self.assertEqual(
            CodexDesktopArchiveBroker().archive(_request()).error_code,
            CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
        )


class DesktopGateTests(unittest.TestCase):
    def test_zero_delete_before_desktop_ack_and_one_delete_after_ack(self) -> None:
        desktop_factory = Factory(_route_transport(), _archive_transport())
        broker = CodexDesktopArchiveBroker(transport_factory=desktop_factory)
        broker.register(_context())
        delete_first = FakeTransport([_response(1), _response(2)])
        read_second = FakeTransport([_response(3), _response(4, {"thread": None})])
        list_third = FakeTransport(
            [_response(5), _response(6, {"data": [], "nextCursor": None}), _response(7, {"data": [], "nextCursor": None})]
        )
        app_factory = Factory(delete_first, read_second, list_third)
        executor = CodexExecutor(
            command=sys.executable,
            transport="app-server",
            transport_factory=app_factory,
            desktop_archive_broker=broker,
        )
        result = executor.cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(
            [message.get("method") for message in delete_first.sent],
            ["initialize", "initialized", "thread/delete"],
        )
        self.assertEqual(result.commands["desktop_archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["app_server_archive"]["status"], "not_requested")
        self.assertEqual(result.commands["delete"]["status"], "acknowledged")

        no_route = CodexExecutor(command=sys.executable, transport="app-server")
        self.assertEqual(no_route.cleanup_session(_request()).error_code, CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE)

    def test_v5_receipt_redaction_and_v4_projection_never_infer_desktop(self) -> None:
        receipt = build_session_cleanup_receipt()
        self.assertEqual(SESSION_CLEANUP_RECEIPT_VERSION, 5)
        self.assertEqual(validate_session_cleanup_receipt(receipt), [])
        self.assertEqual(set(receipt["commands"]), {"desktop_archive", "app_server_archive", "delete"})
        self.assertTrue(all(set(item) == {"status", "checked_at", "request_digest", "route_digest", "app_instance_digest"} for item in receipt["commands"].values()))
        legacy = {
            "archive": {"status": "acknowledged", "checked_at": T0},
            "delete": {"status": "acknowledged", "checked_at": T0},
        }
        normalized = normalize_cleanup_commands(legacy)
        self.assertEqual(normalized["app_server_archive"]["status"], "acknowledged")
        self.assertEqual(normalized["desktop_archive"]["status"], "not_requested")
        v4 = receipt | {
            "version": 4,
            "capability": "supported",
            "strategy": "official_session_archive_then_delete",
            "state": "succeeded",
            "attempts": 1,
            "requested_at": T0,
            "last_attempt_at": T0,
            "completed_at": T0,
            "commands": legacy,
            "verification": {
                "cli": {"status": "absent", "checked_at": T0},
                "desktop_backend": {"status": "unavailable", "checked_at": T0},
                "desktop_live": {"status": "unavailable", "checked_at": T0},
            },
        }
        view = session_cleanup_view(v4)
        self.assertEqual(view["version"], 4)
        self.assertEqual(view["commands"]["archive"]["status"], "acknowledged")
        self.assertEqual(view["verification"]["desktop_backend"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
