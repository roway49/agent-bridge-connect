from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from agent_bridge_connect.adapters import SessionCleanupRequest
from agent_bridge_connect.codex_session_cleanup import (
    CODEX_DESKTOP_UI_STALE_CODE,
    CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE,
    CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
    CODEX_SESSION_DELETE_TIMEOUT_CODE,
    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
)
from agent_bridge_connect.execution_policy import (
    build_session_cleanup_receipt,
    build_session_snapshot,
    session_cleanup_blockers,
    session_cleanup_view,
    validate_session_cleanup_receipt,
)
from agent_bridge_connect.executors.codex import CodexExecutor

SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
T0 = "2026-08-27T00:00:00Z"


class FakeTransport:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.sent: list[dict] = []
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def recv(self, timeout_s: float | None = None) -> dict:
        if not self.messages:
            raise TimeoutError("test timeout")
        message = self.messages.pop(0)
        if isinstance(message, BaseException):
            raise message
        return message

    def close(self) -> None:
        self.closed = True


class TransportFactory:
    def __init__(self, *transports: FakeTransport) -> None:
        self.transports = list(transports)

    def __call__(self, **_: object) -> FakeTransport:
        if not self.transports:
            raise AssertionError("a fresh cleanup connection was not supplied")
        return self.transports.pop(0)


def _initialize_response(request_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": {}}


def _read_transport(*, read_result: dict | None = None, read_error: dict | None = None) -> FakeTransport:
    response = _initialize_response(4)
    if read_error is not None:
        response = {"jsonrpc": "2.0", "id": 4, "error": read_error}
    elif read_result is not None:
        response = {"jsonrpc": "2.0", "id": 4, "result": read_result}
    return FakeTransport([_initialize_response(3), response])


def _delete_transport(*, notification: dict | None = None, tail: list[dict] | None = None) -> FakeTransport:
    messages = [_initialize_response(1), {"jsonrpc": "2.0", "id": 2, "result": {}}]
    if notification is not None:
        messages.append(notification)
    messages.extend(tail or [])
    return FakeTransport(messages)


def _list_transport(*, data: list[dict] | None = None, error: dict | None = None) -> FakeTransport:
    response: dict = {"jsonrpc": "2.0", "id": 6, "result": {"data": data or [], "nextCursor": None}}
    if error is not None:
        response = {"jsonrpc": "2.0", "id": 6, "error": error}
    archived = {"jsonrpc": "2.0", "id": 7, "result": {"data": [], "nextCursor": None}}
    return FakeTransport([_initialize_response(5), response, archived])


def _request(**overrides: object) -> SessionCleanupRequest:
    values: dict[str, object] = {
        "executor": "codex",
        "session_id": SESSION_ID,
        "task_id": "HHWC-001",
        "strategy": "official_session_delete",
        "receipt_source": "jsonl_thread_started",
        "official_receipt_bound": True,
        "workspace": {"root": "."},
    }
    values.update(overrides)
    return SessionCleanupRequest(**values)  # type: ignore[arg-type]


class CodexCleanupProtocolTests(unittest.TestCase):
    def _executor(self, factory: TransportFactory, desktop: str | None = "absent") -> CodexExecutor:
        verifier = None if desktop is None else lambda _: {"status": desktop, "checked_at": T0}
        return CodexExecutor(
            command=sys.executable,
            transport="app-server",
            transport_factory=factory,
            desktop_verifier=verifier,
        )

    def test_delete_rpc_notification_and_fresh_read_are_verified_for_success(self) -> None:
        first = _delete_transport(
            notification={
                "jsonrpc": "2.0",
                "method": "thread/deleted",
                "params": {"threadId": SESSION_ID},
            }
        )
        second = _read_transport(read_result={"thread": None})
        factory = TransportFactory(first, second)
        result = self._executor(factory).cleanup_session(_request())

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")
        self.assertEqual(result.verification["desktop"]["status"], "absent")
        self.assertEqual(
            [item["method"] for item in first.sent],
            ["initialize", "initialized", "thread/delete"],
        )
        self.assertEqual(first.sent[2]["params"], {"threadId": SESSION_ID})
        self.assertEqual(
            [item["method"] for item in second.sent],
            ["initialize", "initialized", "thread/read"],
        )
        self.assertEqual(second.sent[2]["params"], {"threadId": SESSION_ID})
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_read_not_found_is_the_idempotent_absent_result(self) -> None:
        first = _delete_transport(
            notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}}
        )
        second = _read_transport(read_error={"code": "thread_not_found", "message": "thread not found"})
        result = self._executor(TransportFactory(first, second)).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")

    def test_current_app_server_thread_not_loaded_is_absent(self) -> None:
        first = _delete_transport(
            notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}}
        )
        second = _read_transport(
            read_error={"code": -32600, "message": f"thread not loaded: {SESSION_ID}"}
        )
        result = self._executor(TransportFactory(first, second)).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")

    def test_missing_notification_uses_fresh_read_as_authoritative_proof(self) -> None:
        first = _delete_transport()
        second = _read_transport(read_error={"code": -32600, "message": f"thread not loaded: {SESSION_ID}"})
        result = self._executor(TransportFactory(first, second)).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")

    def test_transport_loss_and_timeout_have_stable_codes(self) -> None:
        for failure, expected in (
            (TransportClosedForTest(), CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE),
            (TimeoutError("timeout"), CODEX_SESSION_DELETE_TIMEOUT_CODE),
        ):
            with self.subTest(expected=expected):
                first = FakeTransport([_initialize_response(1), failure])
                result = self._executor(TransportFactory(first)).cleanup_session(_request())
                self.assertEqual(result.error_code, expected)

    def test_post_delete_read_still_present_fails_closed(self) -> None:
        first = _delete_transport(
            notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}}
        )
        second = _read_transport(read_result={"thread": {"id": SESSION_ID}})
        result = self._executor(TransportFactory(first, second)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_DELETE_STILL_PRESENT_CODE)
        self.assertEqual(result.verification["cli"]["status"], "present")

    def test_desktop_aggregation_is_strict(self) -> None:
        for desktop, expected, extra in (
            ("present", CODEX_DESKTOP_UI_STALE_CODE, []),
            (
                None,
                CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE,
                [_list_transport(error={"code": "unsupported", "message": "unsupported"})],
            ),
        ):
            with self.subTest(desktop=desktop):
                first = _delete_transport(
                    notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}}
                )
                second = _read_transport(read_result={"thread": None})
                result = self._executor(
                    TransportFactory(first, second, *extra), desktop=desktop
                ).cleanup_session(_request())
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, expected)

    def test_production_desktop_list_verifier_checks_all_sources_and_archives(self) -> None:
        first = _delete_transport(
            notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}}
        )
        second = _read_transport(read_result={"thread": None})
        third = _list_transport()
        result = self._executor(
            TransportFactory(first, second, third), desktop=None
        ).cleanup_session(_request())

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["desktop"]["status"], "absent")
        list_requests = [item for item in third.sent if item.get("method") == "thread/list"]
        self.assertEqual([item["params"]["archived"] for item in list_requests], [False, True])
        self.assertIn("exec", list_requests[0]["params"]["sourceKinds"])

    def test_production_desktop_list_verifier_detects_stale_entry(self) -> None:
        first = _delete_transport(
            notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}}
        )
        second = _read_transport(read_result={"thread": None})
        third = _list_transport(data=[{"id": SESSION_ID}])
        result = self._executor(
            TransportFactory(first, second, third), desktop=None
        ).cleanup_session(_request())

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_DESKTOP_UI_STALE_CODE)


class CodexCleanupContractTests(unittest.TestCase):
    def test_v2_receipt_contains_only_bounded_verification(self) -> None:
        receipt = build_session_cleanup_receipt()
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(validate_session_cleanup_receipt(receipt), [])
        receipt["verification"]["cli"]["status"] = "raw_rpc"
        self.assertTrue(validate_session_cleanup_receipt(receipt))

    def test_v1_success_is_publicly_legacy_and_unverified(self) -> None:
        v1 = {
            "version": 1,
            "capability": "supported",
            "strategy": "official_session_delete",
            "state": "succeeded",
            "attempts": 1,
            "requested_at": T0,
            "last_attempt_at": T0,
            "next_attempt_at": "",
            "completed_at": T0,
            "error_code": "",
            "retryable": False,
        }
        view = session_cleanup_view(v1)
        self.assertEqual(view["state"], "legacy")
        self.assertEqual(view["error_code"], "legacy_cleanup_unverified")
        self.assertEqual(view["verification"]["cli"]["status"], "unverified")

    def test_codex_cleanup_requires_official_receipt_binding_but_retain_does_not(self) -> None:
        session = build_session_snapshot(
            "codex",
            retain=False,
            session_id=SESSION_ID,
            session_state="terminal",
            created_at=T0,
        )
        blockers = session_cleanup_blockers(
            task_status="completed",
            lease_state="closed",
            report_written=True,
            notification_recorded=True,
            session=session,
        )
        self.assertIn("session_receipt_unbound", blockers)
        retained = build_session_snapshot(
            "codex",
            retain=True,
            session_id=SESSION_ID,
            session_state="terminal",
            created_at=T0,
        )
        retained_blockers = session_cleanup_blockers(
            task_status="completed",
            lease_state="closed",
            report_written=True,
            notification_recorded=True,
            session=retained,
        )
        self.assertNotIn("session_receipt_unbound", retained_blockers)

    def test_cli_exit_zero_is_not_v2_success(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="cli")
        help_text = (
            "codex-cli 0.146.0\n"
            "Usage: codex delete [OPTIONS] <SESSION>\n"
            "Session id (UUID) or session name\n"
            "--force\n"
            "SESSION must be a UUID\n"
        )
        with mock.patch(
            "agent_bridge_connect.executors.codex.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout=help_text, stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ],
        ):
            result = executor.cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE)


class TransportClosedForTest(RuntimeError):
    pass


if __name__ == "__main__":
    unittest.main()
