"""SESSION-104-001 focused suite: official archive-then-delete Codex cleanup.

Covers the exact archive-before-delete order, zero delete calls after an
archive failure, advisory notifications, transport loss/timeout, partial
retry/restart evidence, already-archived confirmation, delete failure,
v1-v4 reads/projections/redaction, backend present/unavailable as
non-gating diagnostics, and unknown version fail-closed.
"""

from __future__ import annotations
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.adapters import SessionCleanupRequest, SessionCleanupResult
from agent_bridge_connect.codex_session_cleanup import CODEX_SESSION_ARCHIVE_FAILED_CODE, CODEX_SESSION_ARCHIVE_INVALID_ID_CODE, CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE, CODEX_SESSION_ARCHIVE_TIMEOUT_CODE, CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE, CodexSessionCleanupError
from agent_bridge_connect.codex_session_cleanup import CodexSessionCleanupClient, CODEX_SESSION_DELETE_STILL_PRESENT_CODE, CODEX_SESSION_DELETE_TIMEOUT_CODE, CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE
from agent_bridge_connect.execution_policy import (
    SESSION_CLEANUP_RECEIPT_VERSION,
    build_session_cleanup_receipt,
    build_session_snapshot,
    normalize_cleanup_commands,
    read_session_cleanup_receipt,
    session_cleanup_blockers,
    session_cleanup_view,
    transition_session_cleanup,
    validate_session_cleanup_receipt,
)
from agent_bridge_connect.executors.codex import CodexExecutor
import socket as _desktop_archive_socket
import sys as _desktop_archive_sys
import unittest as _desktop_archive_unittest
from pathlib import Path as _desktop_archive_Path
from agent_bridge_connect.adapters import SessionCleanupRequest as _desktop_archive_SessionCleanupRequest
from agent_bridge_connect.codex_desktop_archive import (
    AcknowledgedCodexDesktopArchiveBroker as _desktop_archive_AcknowledgedCodexDesktopArchiveBroker,
    CODEX_DESKTOP_ARCHIVE_REJECTED as _desktop_archive_CODEX_DESKTOP_ARCHIVE_REJECTED,
    CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE as _desktop_archive_CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE,
    CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST as _desktop_archive_CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST,
    CODEX_DESKTOP_ARCHIVE_UNSUPPORTED as _desktop_archive_CODEX_DESKTOP_ARCHIVE_UNSUPPORTED,
    CodexDesktopArchiveBroker as _desktop_archive_CodexDesktopArchiveBroker,
    CodexDesktopRouteContext as _desktop_archive_CodexDesktopRouteContext,
    read_desktop_route_context as _desktop_archive_read_desktop_route_context,
)
from agent_bridge_connect.execution_policy import (
    SESSION_CLEANUP_RECEIPT_VERSION as _desktop_archive_SESSION_CLEANUP_RECEIPT_VERSION,
    build_session_cleanup_receipt as _desktop_archive_build_session_cleanup_receipt,
    normalize_cleanup_commands as _desktop_archive_normalize_cleanup_commands,
    session_cleanup_view as _desktop_archive_session_cleanup_view,
    validate_session_cleanup_receipt as _desktop_archive_validate_session_cleanup_receipt,
)
from agent_bridge_connect.executors.codex import CodexExecutor as _desktop_archive_CodexExecutor

SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
CHILD_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d7"
T0 = "2026-08-27T00:00:00Z"
ARCHIVE_THEN_DELETE = "official_session_archive_then_delete"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "session_cleanup_receipts.json"


def _v5_commands(desktop_archive: str = "not_requested", delete: str = "not_requested", app_server_archive: str = "not_requested") -> dict[str, dict[str, str]]:

    def command(status: str) -> dict[str, str]:
        return {"status": status, "checked_at": T0 if status != "not_requested" else "", "request_digest": "", "route_digest": "", "app_instance_digest": ""}

    return {"desktop_archive": command(desktop_archive), "app_server_archive": command(app_server_archive), "delete": command(delete)}


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
        response = {"jsonrpc": "2.0", "id": 5, "error": read_error}
    elif read_result is not None:
        response = {"jsonrpc": "2.0", "id": 5, "result": read_result}
    return FakeTransport([_initialize_response(4), response])


def _action_transport(*, archive_response: dict | None = None, delete_response: dict | None = None, notification: dict | None = None, tail: list[dict] | None = None) -> FakeTransport:
    """Connection A: initialize(1), archive(2), delete(3)."""
    messages = [_initialize_response(1)]
    messages.append(archive_response if archive_response is not None else {"jsonrpc": "2.0", "id": 2, "result": {}})
    messages.append(delete_response if delete_response is not None else {"jsonrpc": "2.0", "id": 3, "result": {}})
    if notification is not None:
        messages.append(notification)
    messages.extend(tail or [])
    return FakeTransport(messages)


def _list_transport(*, data: list[dict] | None = None, error: dict | None = None) -> FakeTransport:
    response: dict = {"jsonrpc": "2.0", "id": 7, "result": {"data": data or [], "nextCursor": None}}
    if error is not None:
        response = {"jsonrpc": "2.0", "id": 7, "error": error}
    archived = {"jsonrpc": "2.0", "id": 8, "result": {"data": [], "nextCursor": None}}
    return FakeTransport([_initialize_response(6), response, archived])


def _request(**overrides: object) -> SessionCleanupRequest:
    values: dict[str, object] = {"executor": "codex", "session_id": SESSION_ID, "task_id": "T2A9-001", "strategy": ARCHIVE_THEN_DELETE, "receipt_source": "jsonl_thread_started", "official_receipt_bound": True, "workspace": {"root": "."}}
    values.update(overrides)
    return SessionCleanupRequest(**values)


def _executor(factory: TransportFactory, desktop: str | None = "absent") -> CodexExecutor:
    """Exercise the legacy App Server client without bypassing the new gate.

    The production ``CodexExecutor`` now requires a live Desktop broker.  The
    historical tests in this module intentionally remain focused on the
    unchanged App Server delete/read/list protocol, so they use the client
    directly through this small test-only adapter.  New Desktop-gate coverage
    lives in ``test_session104_desktop_archive.py``.
    """
    return _LegacyCleanupHarness(factory, desktop)


class _LegacyCleanupHarness:
    def __init__(self, factory: TransportFactory, desktop: str | None) -> None:
        self.client = CodexSessionCleanupClient(sys.executable, cwd=Path.cwd(), transport_factory=factory)
        self.desktop = desktop

    def cleanup_session(self, request: SessionCleanupRequest) -> SessionCleanupResult:
        try:
            observation = self.client.delete_and_verify(request.session_id, archive_acknowledged=request.archive_acknowledged, archive_checked_at=request.archive_checked_at)
        except CodexSessionCleanupError as exc:
            checked_at = exc.cli_checked_at or T0
            commands = exc.commands or {"archive": {"status": "unverified", "checked_at": checked_at}, "delete": {"status": "not_requested", "checked_at": checked_at}}
            return SessionCleanupResult(
                "failed",
                "supported",
                request.strategy,
                exc.code,
                exc.retryable,
                verification={"cli": {"status": exc.cli_status, "checked_at": checked_at}, "desktop_backend": {"status": "unavailable", "checked_at": checked_at}, "desktop_live": {"status": "unavailable", "checked_at": checked_at}},
                commands=commands,
            )
        verification = observation.verification()
        try:
            backend = self.client.verify_desktop_absence(request.session_id)
        except (CodexSessionCleanupError, OSError, RuntimeError):
            backend = {"status": "unavailable", "checked_at": T0}
        live_status = "not_applicable" if observation.cli_status == "absent" else self.desktop if self.desktop in {"absent", "present"} else "unavailable"
        verification.update({"desktop_backend": backend, "desktop_live": {"status": live_status, "checked_at": T0}})
        return SessionCleanupResult("succeeded" if observation.cli_status == "absent" else "failed", "supported", request.strategy, "" if observation.cli_status == "absent" else CODEX_SESSION_DELETE_STILL_PRESENT_CODE, False, verification=verification, commands=observation.commands())


class ArchiveThenDeleteOrderTests(unittest.TestCase):
    """The exact official-session command sequence and its evidence."""

    def test_recovery_archive_only_never_deletes(self):
        transport = _action_transport()
        client = CodexSessionCleanupClient("codex", cwd=Path.cwd(), transport=transport)
        receipt = client.archive_only(SESSION_ID)
        self.assertTrue(receipt["archive_acknowledged"])
        self.assertTrue(receipt["archive_checked_at"])
        self.assertEqual([m["method"] for m in transport.sent], ["initialize", "initialized", "thread/archive"])
        self.assertEqual(transport.sent[-1]["params"], {"threadId": SESSION_ID})
        self.assertTrue(transport.closed)

    def test_recovery_archive_failure_never_deletes_or_acknowledges(self):
        from agent_bridge_connect.codex_session_cleanup import CodexSessionCleanupError
        transport = _action_transport(archive_response={"jsonrpc": "2.0", "id": 2,
            "error": {"code": -32600, "message": "active writer"}})
        client = CodexSessionCleanupClient("codex", cwd=Path.cwd(), transport=transport)
        with self.assertRaises(CodexSessionCleanupError):
            client.archive_only(SESSION_ID)
        self.assertNotIn("thread/delete", [m["method"] for m in transport.sent])
        self.assertTrue(transport.closed)

    def test_archive_is_acked_before_delete_is_sent(self) -> None:
        first = _action_transport(notification={"jsonrpc": "2.0", "method": "thread/deleted", "params": {"threadId": SESSION_ID}})
        second = _read_transport(read_result={"thread": None})
        third = _list_transport()
        result = _executor(TransportFactory(first, second, third)).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([item["method"] for item in first.sent], ["initialize", "initialized", "thread/archive", "thread/delete"])
        self.assertEqual(first.sent[2]["params"], {"threadId": SESSION_ID})
        self.assertEqual(first.sent[3]["params"], {"threadId": SESSION_ID})
        self.assertEqual([item["method"] for item in second.sent], ["initialize", "initialized", "thread/read"])
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "acknowledged")
        self.assertTrue(result.commands["archive"]["checked_at"])
        self.assertTrue(result.commands["delete"]["checked_at"])
        self.assertEqual(result.verification["desktop_live"]["status"], "not_applicable")

    def test_executor_prearchive_receipt_skips_non_idempotent_archive(self) -> None:
        first = FakeTransport([_initialize_response(1), {"jsonrpc": "2.0", "id": 2, "result": {}}])
        second = FakeTransport([_initialize_response(3), {"jsonrpc": "2.0", "id": 4, "result": {"thread": None}}])
        third = FakeTransport([_initialize_response(5), {"jsonrpc": "2.0", "id": 6, "result": {"data": [], "nextCursor": None}}, {"jsonrpc": "2.0", "id": 7, "result": {"data": [], "nextCursor": None}}])
        result = _executor(TransportFactory(first, second, third)).cleanup_session(_request(archive_acknowledged=True, archive_checked_at=T0))
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([item["method"] for item in first.sent], ["initialize", "initialized", "thread/delete"])
        self.assertEqual(result.commands["archive"]["checked_at"], T0)
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "acknowledged")

    def test_prearchived_delete_target_missing_is_idempotent_after_absence_proof(self) -> None:
        first = FakeTransport([_initialize_response(1), {"jsonrpc": "2.0", "id": 2, "error": {"code": "thread_not_found", "message": f"thread not found: {SESSION_ID}"}}])
        second = FakeTransport([_initialize_response(3), {"jsonrpc": "2.0", "id": 4, "error": {"code": "thread_not_found", "message": "thread not found"}}])
        third = FakeTransport([_initialize_response(5), {"jsonrpc": "2.0", "id": 6, "result": {"data": [], "nextCursor": None}}, {"jsonrpc": "2.0", "id": 7, "result": {"data": [], "nextCursor": None}}])
        result = _executor(TransportFactory(first, second, third)).cleanup_session(_request(archive_acknowledged=True, archive_checked_at=T0))
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "confirmed")

    def test_capability_reports_the_archive_then_delete_strategy(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="auto")
        capability = executor.session_cleanup_capability(_request())
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, ARCHIVE_THEN_DELETE)

    def test_archived_notification_is_advisory_only(self) -> None:
        first = _action_transport(notification={"jsonrpc": "2.0", "method": "thread/archived", "params": {"threadId": SESSION_ID}})
        second = _read_transport(read_result={"thread": None})
        result = _executor(TransportFactory(first, second, _list_transport())).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([item["method"] for item in first.sent], ["initialize", "initialized", "thread/archive", "thread/delete"])

    def test_already_archived_acknowledgement_continues_to_delete(self) -> None:
        first = _action_transport(archive_response={"jsonrpc": "2.0", "id": 2, "result": {"alreadyArchived": True}})
        second = _read_transport(read_result={"thread": None})
        result = _executor(TransportFactory(first, second, _list_transport())).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")

    def test_zero_delete_calls_after_archive_rpc_error(self) -> None:
        first = _action_transport(archive_response={"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "archive rejected"}})
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_FAILED_CODE)
        methods = [item.get("method") for item in first.sent]
        self.assertNotIn("thread/delete", methods)
        self.assertEqual(result.commands["archive"]["status"], "failed")
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_archive_target_missing_fails_closed_before_delete(self) -> None:
        first = _action_transport(archive_response={"jsonrpc": "2.0", "id": 2, "error": {"code": -32600, "message": f"thread not found: {SESSION_ID}"}})
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE)
        self.assertNotIn("thread/delete", [item.get("method") for item in first.sent])
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_archive_invalid_session_id_is_scoped_to_archive(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="auto")
        with mock.patch.object(executor, "_cleanup_session_app_server") as app_server:
            result = executor.cleanup_session(_request(session_id="not-a-uuid"))
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_INVALID_ID_CODE)
        self.assertEqual(result.strategy, ARCHIVE_THEN_DELETE)
        app_server.assert_not_called()

    def test_archive_transport_loss_sends_zero_delete_calls(self) -> None:
        first = FakeTransport([_initialize_response(1), TransportClosedForTest()])
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE)
        self.assertTrue(result.retryable)
        self.assertNotIn("thread/delete", [item.get("method") for item in first.sent])
        self.assertEqual(result.commands["archive"]["status"], "unverified")
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_archive_timeout_sends_zero_delete_calls(self) -> None:
        first = FakeTransport([_initialize_response(1), TimeoutError("timeout")])
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_TIMEOUT_CODE)
        self.assertTrue(result.retryable)
        self.assertNotIn("thread/delete", [item.get("method") for item in first.sent])

    def test_delete_timeout_has_stable_delete_code(self) -> None:
        first = FakeTransport([_initialize_response(1), {"jsonrpc": "2.0", "id": 2, "result": {}}, TimeoutError("timeout")])
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_DELETE_TIMEOUT_CODE)
        self.assertTrue(result.retryable)
        self.assertIn("thread/archive", [item.get("method") for item in first.sent])
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_delete_transport_loss_has_stable_delete_code(self) -> None:
        first = FakeTransport([_initialize_response(1), {"jsonrpc": "2.0", "id": 2, "result": {}}, TransportClosedForTest()])
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE)
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")

    def test_post_delete_read_still_present_fails_closed(self) -> None:
        first = _action_transport()
        second = _read_transport(read_result={"thread": {"id": SESSION_ID}})
        result = _executor(TransportFactory(first, second)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_DELETE_STILL_PRESENT_CODE)
        self.assertEqual(result.verification["cli"]["status"], "present")

    def test_read_not_found_is_the_idempotent_absent_result(self) -> None:
        first = _action_transport()
        second = _read_transport(read_error={"code": "thread_not_found", "message": "thread not found"})
        result = _executor(TransportFactory(first, second, _list_transport())).cleanup_session(_request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")

    def test_desktop_diagnostics_are_non_gating_for_success(self) -> None:
        """Command acknowledgements decide; backend/live stay diagnostic.

        The current Codex Desktop refresh delay is accepted and non-blocking:
        a stale Desktop live entry or an unavailable list channel must not
        turn an acknowledged archive+delete pair into a failure.
        """
        for desktop, extra in (("present", []), (None, [_list_transport(error={"code": "unsupported", "message": "unsupported"})])):
            with self.subTest(desktop=desktop):
                first = _action_transport()
                second = _read_transport(read_result={"thread": None})
                if not extra:
                    extra = [_list_transport()]
                result = _executor(TransportFactory(first, second, *extra), desktop=desktop).cleanup_session(_request())
                self.assertEqual(result.state, "succeeded")
                self.assertEqual(result.commands["archive"]["status"], "acknowledged")
                self.assertEqual(result.commands["delete"]["status"], "acknowledged")
                self.assertEqual(result.verification["desktop_live"]["status"], "not_applicable")


class TransportClosedForTest(RuntimeError):
    pass


class CodexCleanupContractTests(unittest.TestCase):
    def test_cleanup_auto_uses_app_server_and_only_explicit_cli_direct_fallback(self) -> None:
        auto = CodexExecutor(command=sys.executable, transport="auto")
        self.assertTrue(auto._uses_cleanup_app_server(_request()))
        for mode in ("cli", "direct"):
            with self.subTest(mode=mode):
                executor = CodexExecutor(command=sys.executable, transport=mode)
                self.assertFalse(executor._uses_cleanup_app_server(_request()))
        self.assertTrue(CodexExecutor(command=sys.executable, transport="full")._uses_cleanup_app_server(_request()))

    def test_cli_fallback_cannot_claim_the_archive_strategy(self) -> None:
        help_text = "codex-cli 0.146.0\nUsage: codex delete [OPTIONS] <SESSION>\nSession id (UUID) or session name\n--force\nSESSION must be a UUID\n"
        for transport in ("cli", "direct"):
            with self.subTest(transport=transport):
                executor = CodexExecutor(command=sys.executable, transport=transport)
                with mock.patch("agent_bridge_connect.executors.codex.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0, stdout=help_text, stderr=""), subprocess.CompletedProcess([], 0, stdout="", stderr="")]):
                    result = executor.cleanup_session(_request())
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, "codex_desktop_archive_route_unavailable")
                self.assertEqual(result.strategy, ARCHIVE_THEN_DELETE)

    def test_v4_receipt_contains_bounded_commands(self) -> None:
        receipt = build_session_cleanup_receipt()
        self.assertEqual(receipt["version"], SESSION_CLEANUP_RECEIPT_VERSION)
        self.assertEqual(receipt["commands"], _v5_commands())
        self.assertEqual(validate_session_cleanup_receipt(receipt), [])
        receipt["commands"]["desktop_archive"]["status"] = "raw_state"
        self.assertTrue(validate_session_cleanup_receipt(receipt))

    def test_v4_succeeded_requires_both_command_acknowledgements(self) -> None:
        receipt = build_session_cleanup_receipt()
        receipt["version"] = 4
        receipt.update({"capability": "supported", "strategy": ARCHIVE_THEN_DELETE, "state": "succeeded", "attempts": 1, "requested_at": T0, "last_attempt_at": T0, "completed_at": T0})
        receipt["commands"] = {"archive": {"status": "acknowledged", "checked_at": T0}, "delete": {"status": "acknowledged", "checked_at": T0}}
        self.assertEqual(validate_session_cleanup_receipt(receipt), [])
        for command in ("archive", "delete"):
            broken = copy.deepcopy(receipt)
            broken["commands"][command]["status"] = "unverified"
            self.assertTrue(validate_session_cleanup_receipt(broken))

    def test_v2_receipt_projects_backend_and_unverified_live(self) -> None:
        v2 = {
            "version": 2,
            "capability": "unknown",
            "strategy": "none",
            "state": "not_requested",
            "attempts": 0,
            "requested_at": "",
            "last_attempt_at": "",
            "next_attempt_at": "",
            "completed_at": "",
            "error_code": "",
            "retryable": False,
            "verification": {"cli": {"status": "unknown", "checked_at": ""}, "desktop": {"status": "unknown", "checked_at": ""}},
        }
        self.assertEqual(validate_session_cleanup_receipt(v2), [])
        projected = session_cleanup_view(v2)
        self.assertEqual(projected["version"], 2)
        verification = projected["verification"]
        self.assertEqual(verification["desktop_backend"]["status"], "unknown")
        self.assertEqual(verification["desktop_live"]["status"], "unverified")
        self.assertEqual(verification["desktop"]["status"], "unverified")
        self.assertNotIn("commands", projected)

    def test_v3_receipt_projects_with_history_and_no_commands(self) -> None:
        fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
        v3 = fixture["legacy_v3"]
        self.assertEqual(validate_session_cleanup_receipt(v3), [])
        projected = session_cleanup_view(v3)
        self.assertEqual(projected["version"], 3)
        self.assertEqual(projected["strategy"], "official_session_delete")
        self.assertEqual(projected["error_code"], "codex_desktop_verification_unavailable")
        self.assertNotIn("commands", projected)

    def test_v1_success_is_publicly_legacy_and_unverified(self) -> None:
        v1 = {"version": 1, "capability": "supported", "strategy": "official_session_delete", "state": "succeeded", "attempts": 1, "requested_at": T0, "last_attempt_at": T0, "next_attempt_at": "", "completed_at": T0, "error_code": "", "retryable": False}
        view = session_cleanup_view(v1)
        self.assertEqual(view["state"], "legacy")
        self.assertEqual(view["error_code"], "legacy_cleanup_unverified")
        self.assertEqual(view["verification"]["cli"]["status"], "unverified")

    def test_unknown_receipt_version_fails_closed(self) -> None:
        future = build_session_cleanup_receipt()
        future["version"] = 99
        self.assertTrue(validate_session_cleanup_receipt(future))
        with self.assertRaises(Exception):
            read_session_cleanup_receipt(future)

    def test_codex_cleanup_requires_official_receipt_binding_but_retain_does_not(self) -> None:
        session = build_session_snapshot("codex", retain=False, session_id=SESSION_ID, session_state="terminal", created_at=T0)
        blockers = session_cleanup_blockers(task_status="completed", lease_state="closed", session=session, task_end_dialog_delivered=True)
        self.assertIn("session_receipt_unbound", blockers)
        retained = build_session_snapshot("codex", retain=True, session_id=SESSION_ID, session_state="terminal", created_at=T0)
        retained_blockers = session_cleanup_blockers(task_status="completed", lease_state="closed", session=retained, task_end_dialog_delivered=True)
        self.assertNotIn("session_receipt_unbound", retained_blockers)

    def test_unbound_cleanup_request_fails_before_transport(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="auto")
        with mock.patch.object(executor, "_cleanup_session_app_server") as cleanup:
            result = executor.cleanup_session(_request(official_receipt_bound=False, receipt_source=""))
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "codex_cleanup_receipt_unbound")
        cleanup.assert_not_called()


class V4TransitionGateTests(unittest.TestCase):
    """Fail-closed v4 transitions: command acknowledgements gate success."""

    def _session(self, *, executor: str = "codex") -> dict:
        session_id = SESSION_ID if executor == "codex" else "20260827_000000_a1b2c3"
        session = build_session_snapshot(executor, retain=False, session_id=session_id, session_state="terminal", created_at=T0)
        if executor == "codex":
            session["official_receipt_bound"] = True
            session["receipt_source"] = "jsonl_thread_started"
        return session

    @staticmethod
    def _transition(session: dict, target: str, **kwargs: object) -> dict:
        defaults = {"task_status": "completed", "lease_state": "closed", "task_end_dialog_delivered": True, "occurred_at": T0}
        defaults.update(kwargs)
        receipt = transition_session_cleanup(session, target, **defaults)
        session["cleanup"] = receipt
        return receipt

    def test_codex_success_requires_both_acknowledged_commands(self) -> None:
        session = self._session()
        self._transition(session, "pending", capability="supported", strategy=ARCHIVE_THEN_DELETE)
        for commands, expect_error in (({**_v5_commands("acknowledged", "acknowledged")}, False), ({**_v5_commands("confirmed", "acknowledged")}, False), ({**_v5_commands("unverified", "acknowledged")}, True), ({**_v5_commands("acknowledged")}, True)):
            with self.subTest(archive=commands["desktop_archive"]["status"], delete=commands["delete"]["status"]):
                fresh = self._session()
                self._transition(fresh, "pending", capability="supported", strategy=ARCHIVE_THEN_DELETE)
                kwargs = {"capability": "supported", "strategy": ARCHIVE_THEN_DELETE, "commands": commands, "verification": {"cli": {"status": "absent", "checked_at": T0}, "desktop_backend": {"status": "unavailable", "checked_at": T0}, "desktop_live": {"status": "unknown", "checked_at": T0}}}
                if expect_error:
                    with self.assertRaises(Exception):
                        self._transition(fresh, "succeeded", **kwargs)
                else:
                    receipt = self._transition(fresh, "succeeded", **kwargs)
                    self.assertEqual(receipt["state"], "succeeded")
                    self.assertEqual(receipt["verification"]["desktop_live"]["status"], "not_applicable")
                    self.assertEqual(receipt["verification"]["desktop_backend"]["status"], "unavailable")

    def test_failed_transition_preserves_partial_command_evidence(self) -> None:
        session = self._session()
        self._transition(session, "pending", capability="supported", strategy=ARCHIVE_THEN_DELETE)
        partial = _v5_commands("unverified")
        failed = self._transition(session, "failed", capability="supported", strategy=ARCHIVE_THEN_DELETE, error_code=CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE, retryable=True, next_attempt_at="2026-08-27T00:01:00Z", commands=partial)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["commands"], partial)
        reread = read_session_cleanup_receipt(failed)
        self.assertEqual(reread["commands"], partial)

    def test_retry_backoff_reuses_stale_pending_receipt_commands(self) -> None:
        session = self._session()
        self._transition(session, "pending", capability="supported", strategy=ARCHIVE_THEN_DELETE)
        partial = {"archive": {"status": "failed", "checked_at": T0}, "delete": {"status": "not_requested", "checked_at": T0}}
        self._transition(session, "failed", capability="supported", strategy=ARCHIVE_THEN_DELETE, error_code=CODEX_SESSION_ARCHIVE_FAILED_CODE, retryable=True, next_attempt_at="2026-08-27T00:01:00Z", commands=partial)
        retry = self._transition(session, "pending", occurred_at="2026-08-27T00:02:00Z")
        self.assertEqual(retry["attempts"], 2)
        self.assertEqual(retry["strategy"], ARCHIVE_THEN_DELETE)

    def test_hermes_success_stamps_not_applicable_commands(self) -> None:
        session = self._session(executor="hermes")
        self._transition(session, "pending", capability="supported", strategy="official_session_delete")
        receipt = self._transition(session, "succeeded", capability="supported", strategy="official_session_delete")
        self.assertEqual(receipt["commands"]["desktop_archive"]["status"], "not_applicable")
        self.assertEqual(receipt["commands"]["delete"]["status"], "not_applicable")


class PrimaryAuxiliaryIsolationTests(unittest.TestCase):
    """Primary-first, deepest/newest auxiliary ordering and strategy binding."""

    def test_auxiliary_codex_strategy_is_archive_then_delete(self) -> None:
        from agent_bridge_connect.auxiliary_sessions import auxiliary_cleanup_strategy

        codex_entry = {"executor": "codex", "project_mode": "none"}
        self.assertEqual(auxiliary_cleanup_strategy(codex_entry), ARCHIVE_THEN_DELETE)
        claude_entry = {"executor": "claude", "project_mode": "ephemeral"}
        self.assertEqual(auxiliary_cleanup_strategy(claude_entry), "claude_project_purge")
        self.assertEqual(auxiliary_cleanup_strategy({"retain": True}), "retain")

    def test_auxiliary_receipt_views_redact_and_project_commands(self) -> None:
        from agent_bridge_connect.auxiliary_sessions import auxiliary_ledger_view

        receipt = build_session_cleanup_receipt()
        receipt.update({"capability": "supported", "strategy": ARCHIVE_THEN_DELETE, "state": "failed", "attempts": 1, "requested_at": T0, "last_attempt_at": T0, "error_code": "codex_session_delete_transport_lost", "retryable": True, "next_attempt_at": "2026-08-27T00:01:00Z"})
        receipt["commands"] = _v5_commands("acknowledged")
        entry = {
            "version": 1,
            "aux_id": "a" * 32,
            "owner_task_id": "T2A9-001",
            "owner_run_id": "run-1",
            "parent_executor": "codex",
            "parent_session_id": SESSION_ID,
            "executor": "codex",
            "session_id": CHILD_SESSION_ID,
            "source": "jsonl_thread_started",
            "purpose": "collaboration_spawn",
            "retain": False,
            "session_state": "terminal",
            "project_mode": "none",
            "project_path": "",
            "cleanup": receipt,
            "reserved_at": T0,
            "bound_at": T0,
            "created_at": T0,
            "updated_at": T0,
        }
        ledger = {"version": 1, "sessions": [entry]}
        view = auxiliary_ledger_view(ledger)
        self.assertEqual(len(view), 1)
        cleanup = view[0]["cleanup"]
        self.assertEqual(cleanup["commands"]["archive"]["status"], "acknowledged")
        self.assertEqual(cleanup["commands"]["delete"]["status"], "not_requested")
        self.assertNotIn("project_path", cleanup)
        self.assertNotIn(CHILD_SESSION_ID, str(view[0]))
        self.assertNotIn(SESSION_ID, str(view[0]))

    def test_primary_and_auxiliary_candidates_exclude_everything_else(self) -> None:
        session = build_session_snapshot("codex", retain=False, session_id=SESSION_ID, session_state="terminal", created_at=T0)
        session["official_receipt_bound"] = True
        session["receipt_source"] = "jsonl_thread_started"
        blockers = session_cleanup_blockers(task_status="completed", lease_state="closed", session=session, task_end_dialog_delivered=True)
        self.assertEqual(blockers, [])


class CommandEvidenceRedactionTests(unittest.TestCase):
    def test_normalize_cleanup_commands_rejects_extra_fields(self) -> None:
        good = {"archive": {"status": "acknowledged", "checked_at": T0}, "delete": {"status": "not_requested", "checked_at": ""}}
        self.assertEqual(normalize_cleanup_commands(good), _v5_commands(app_server_archive="acknowledged"))
        polluted = copy.deepcopy(good)
        polluted["archive"]["threadId"] = SESSION_ID
        self.assertEqual(normalize_cleanup_commands(polluted), normalize_cleanup_commands(None))
        self.assertEqual(normalize_cleanup_commands(None), normalize_cleanup_commands(None))

    def test_codex_error_command_evidence_stays_bounded(self) -> None:
        error = CodexSessionCleanupError(CODEX_SESSION_ARCHIVE_TIMEOUT_CODE, retryable=True, commands={"archive": {"status": "unverified", "checked_at": T0}, "delete": {"status": "not_requested", "checked_at": T0}})
        self.assertEqual(error.commands["delete"]["status"], "not_requested")
        self.assertNotIn(SESSION_ID, str(error))


_desktop_archive_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
_desktop_archive_OTHER_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d7"
_desktop_archive_DISPATCHER_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d8"
_desktop_archive_T0 = "2026-09-08T00:00:00Z"


class _desktop_archive_FakeTransport:
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


class _desktop_archive_Factory:
    def __init__(self, *transports: _desktop_archive_FakeTransport) -> None:
        self.transports = list(transports)
        self.calls = 0

    def __call__(self, **_: object) -> _desktop_archive_FakeTransport:
        self.calls += 1
        return self.transports.pop(0)


def _desktop_archive__response(request_id: int, result: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result or {}}


def _desktop_archive__route_transport(*, response: dict | None = None, tools: list[str] | None = None, start_id: int = 1) -> _desktop_archive_FakeTransport:
    names = tools or ["set_thread_archived", "other"]
    return _desktop_archive_FakeTransport([_desktop_archive__response(start_id), _desktop_archive__response(start_id + 1, {"tools": [{"name": name} for name in names]}), response or _desktop_archive__response(start_id + 3)])


def _desktop_archive__context(dispatcher_thread_id: str = _desktop_archive_DISPATCHER_ID) -> _desktop_archive_CodexDesktopRouteContext:
    return _desktop_archive_CodexDesktopRouteContext(
        pipe_path="/tmp/codex-app-tools-test.pipe",
        dispatcher_thread_id=dispatcher_thread_id,
        mcp_runtime="/Applications/Test.app/Contents/Resources/cua_node/bin/node",
        mcp_resource="/Applications/Test.app/Contents/Resources/plugins/openai-bundled/plugins/codex-app-tools/server.mjs",
        host=_desktop_archive_socket.gethostname(),
    )


def _desktop_archive__archive_transport(*, response: dict | None = None, start_id: int = 3) -> _desktop_archive_FakeTransport:
    return _desktop_archive_FakeTransport([_desktop_archive__response(start_id), response or _desktop_archive__response(start_id + 1)])


def _desktop_archive__request(**overrides: object) -> _desktop_archive_SessionCleanupRequest:
    values: dict[str, object] = {
        "executor": "codex",
        "session_id": _desktop_archive_SESSION_ID,
        "task_id": "T2A5-001",
        "executor_run_id": "run-e299",
        "strategy": "official_session_archive_then_delete",
        "receipt_source": "jsonl_thread_started",
        "official_receipt_bound": True,
        "workspace": {"root": str(_desktop_archive_Path.cwd())},
    }
    values.update(overrides)
    return _desktop_archive_SessionCleanupRequest(**values)


class _desktop_archive_DesktopRouteTests(_desktop_archive_unittest.TestCase):
    def test_native_control_plane_ack_is_exactly_bound(self) -> None:
        broker = _desktop_archive_AcknowledgedCodexDesktopArchiveBroker(task_id="T2A5-001", executor_run_id="run-e299", session_id=_desktop_archive_SESSION_ID)
        self.assertTrue(broker.route_available())
        self.assertTrue(broker.archive(_desktop_archive__request()).acknowledged)
        self.assertEqual(broker.archive(_desktop_archive__request(session_id=_desktop_archive_OTHER_ID)).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_REJECTED)

    def test_native_ack_accepts_cleared_terminal_activity_pointer(self) -> None:
        broker = _desktop_archive_AcknowledgedCodexDesktopArchiveBroker(task_id="T2A5-001", executor_run_id="", session_id=_desktop_archive_SESSION_ID, binding_executor_run_id="run-e299")
        self.assertTrue(broker.route_available())
        self.assertTrue(broker.archive(_desktop_archive__request(executor_run_id="")).acknowledged)
        self.assertEqual(broker.archive(_desktop_archive__request(executor_run_id="other-run")).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_REJECTED)

    def test_environment_context_derives_official_resource_from_node(self) -> None:
        env = {
            "CODEX_APP_TOOLS_PIPE_PATH": "/tmp/app-tools.pipe",
            "CODEX_THREAD_ID": _desktop_archive_DISPATCHER_ID,
            "CODEX_MCP_NODE_PATH": "/Applications/Test.app/Contents/Resources/cua_node/bin/node",
            "AGENTBC_DESKTOP_RELAY_SOCKET": "/tmp/agentbc-relay.sock",
            "AGENTBC_DESKTOP_RELAY_TOKEN": "relay-token",
        }
        context = _desktop_archive_read_desktop_route_context(env)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.mcp_resource, "/Applications/Test.app/Contents/Resources/plugins/openai-bundled/plugins/codex-app-tools/server.mjs")
        self.assertEqual(context.relay_socket, "/tmp/agentbc-relay.sock")
        self.assertEqual(context.relay_token, "relay-token")
        self.assertNotIn("app-tools.pipe", repr(context))
        self.assertIsNone(_desktop_archive_read_desktop_route_context({**env, "CODEX_THREAD_ID": "wrong"}))
        self.assertIsNone(_desktop_archive_read_desktop_route_context({key: value for (key, value) in env.items() if key != "CODEX_MCP_NODE_PATH"}))

    def test_registration_rejects_unrelated_facade_resource(self) -> None:
        invalid = _desktop_archive_CodexDesktopRouteContext(pipe_path="/tmp/codex-app-tools-test.pipe", dispatcher_thread_id=_desktop_archive_DISPATCHER_ID, mcp_runtime="/Applications/Test.app/Contents/Resources/cua_node/bin/node", mcp_resource="/tmp/server.mjs", host=_desktop_archive_socket.gethostname())
        broker = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=_desktop_archive_Factory(_desktop_archive__route_transport()))
        self.assertEqual(broker.register(invalid)["error_code"], _desktop_archive_CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE)

    def test_registration_negotiates_tools_without_version_allowlist(self) -> None:
        factory = _desktop_archive_Factory(_desktop_archive__route_transport())
        broker = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=factory)
        registration = broker.register(_desktop_archive__context())
        self.assertEqual(registration["capability"], "supported")
        self.assertTrue(registration["route_digest"].startswith("route_"))
        self.assertNotIn("/tmp", str(registration))

    def test_unsupported_and_wrong_thread_responses_fail_closed(self) -> None:
        unsupported = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=_desktop_archive_Factory(_desktop_archive__route_transport(tools=["other"])))
        unsupported.register(_desktop_archive__context())
        self.assertEqual(unsupported.archive(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_UNSUPPORTED)
        wrong = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=_desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive__archive_transport(response=_desktop_archive__response(4, {"threadId": _desktop_archive_OTHER_ID}))))
        wrong.register(_desktop_archive__context())
        self.assertEqual(wrong.archive(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_REJECTED)

    def test_transport_death_keeps_route_retryable_and_duplicate_is_deduplicated(self) -> None:
        dead = _desktop_archive_CodexDesktopArchiveBroker(
            transport_factory=_desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive_FakeTransport([RuntimeError("dead")]), _desktop_archive_FakeTransport([_desktop_archive__response(4), _desktop_archive__response(5, {"tools": [{"name": "set_thread_archived"}]}), _desktop_archive__response(6)]))
        )
        dead.register(_desktop_archive__context())
        self.assertEqual(dead.archive(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST)
        self.assertTrue(dead.route_available())
        self.assertTrue(dead.archive(_desktop_archive__request()).acknowledged)
        factory = _desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive__archive_transport())
        broker = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=factory)
        broker.register(_desktop_archive__context())
        first = broker.archive(_desktop_archive__request())
        self.assertEqual(first, broker.archive(_desktop_archive__request()))
        self.assertTrue(first.acknowledged)
        self.assertEqual(factory.calls, 2)

    def test_registration_collision_is_retried_during_archive(self) -> None:
        retry_transport = _desktop_archive_FakeTransport([_desktop_archive__response(2), _desktop_archive__response(3, {"tools": [{"name": "set_thread_archived"}]}), _desktop_archive__response(4)])
        broker = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=_desktop_archive_Factory(_desktop_archive_FakeTransport([RuntimeError("pipe busy")]), retry_transport))
        registration = broker.register(_desktop_archive__context())
        self.assertEqual(registration["capability"], "unknown")
        self.assertTrue(registration["route_digest"].startswith("route_"))
        self.assertTrue(broker.route_available())
        self.assertTrue(broker.archive(_desktop_archive__request()).acknowledged)
        self.assertEqual(broker.public_status()["capability"], "supported")

    def test_restarted_route_drops_old_cache_and_keeps_long_task_route_usable(self) -> None:
        factory = _desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive__archive_transport(start_id=3), _desktop_archive__route_transport(start_id=5), _desktop_archive__archive_transport(start_id=7), _desktop_archive__archive_transport(start_id=9))
        broker = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=factory)
        broker.register(_desktop_archive__context())
        self.assertTrue(broker.archive(_desktop_archive__request()).acknowledged)
        self.assertTrue(broker.route_available())
        restarted = broker.register(_desktop_archive__context(_desktop_archive_DISPATCHER_ID[:-1] + "9"))
        self.assertEqual(restarted["capability"], "supported")
        self.assertTrue(broker.archive(_desktop_archive__request()).acknowledged)
        self.assertTrue(broker.archive(_desktop_archive__request(session_id=_desktop_archive_OTHER_ID)).acknowledged)
        self.assertEqual(factory.calls, 5)

    def test_archive_timeout_and_tool_rejection_are_bounded(self) -> None:
        timeout = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=_desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive__archive_transport(response=TimeoutError("timeout"))))
        timeout.register(_desktop_archive__context())
        self.assertEqual(timeout.archive(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_TRANSPORT_LOST)
        rejected = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=_desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive__archive_transport(response=_desktop_archive__response(4, {"isError": True}))))
        rejected.register(_desktop_archive__context())
        self.assertEqual(rejected.archive(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_REJECTED)

    def test_unregistered_route_is_bounded_unavailable(self) -> None:
        self.assertEqual(_desktop_archive_CodexDesktopArchiveBroker().archive(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE)


class _desktop_archive_DesktopGateTests(_desktop_archive_unittest.TestCase):
    def test_zero_delete_before_desktop_ack_and_one_delete_after_ack(self) -> None:
        desktop_factory = _desktop_archive_Factory(_desktop_archive__route_transport(), _desktop_archive__archive_transport())
        broker = _desktop_archive_CodexDesktopArchiveBroker(transport_factory=desktop_factory)
        broker.register(_desktop_archive__context())
        delete_first = _desktop_archive_FakeTransport([_desktop_archive__response(1), _desktop_archive__response(2)])
        read_second = _desktop_archive_FakeTransport([_desktop_archive__response(3), _desktop_archive__response(4, {"thread": None})])
        list_third = _desktop_archive_FakeTransport([_desktop_archive__response(5), _desktop_archive__response(6, {"data": [], "nextCursor": None}), _desktop_archive__response(7, {"data": [], "nextCursor": None})])
        app_factory = _desktop_archive_Factory(delete_first, read_second, list_third)
        executor = _desktop_archive_CodexExecutor(command=_desktop_archive_sys.executable, transport="app-server", transport_factory=app_factory, desktop_archive_broker=broker)
        result = executor.cleanup_session(_desktop_archive__request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([message.get("method") for message in delete_first.sent], ["initialize", "initialized", "thread/delete"])
        self.assertEqual(result.commands["desktop_archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["app_server_archive"]["status"], "not_requested")
        self.assertEqual(result.commands["delete"]["status"], "acknowledged")
        no_route = _desktop_archive_CodexExecutor(command=_desktop_archive_sys.executable, transport="app-server")
        self.assertEqual(no_route.cleanup_session(_desktop_archive__request()).error_code, _desktop_archive_CODEX_DESKTOP_ARCHIVE_ROUTE_UNAVAILABLE)

    def test_v5_receipt_redaction_and_v4_projection_never_infer_desktop(self) -> None:
        receipt = _desktop_archive_build_session_cleanup_receipt()
        self.assertEqual(_desktop_archive_SESSION_CLEANUP_RECEIPT_VERSION, 5)
        self.assertEqual(_desktop_archive_validate_session_cleanup_receipt(receipt), [])
        self.assertEqual(set(receipt["commands"]), {"desktop_archive", "app_server_archive", "delete"})
        self.assertTrue(all((set(item) == {"status", "checked_at", "request_digest", "route_digest", "app_instance_digest"} for item in receipt["commands"].values())))
        legacy = {"archive": {"status": "acknowledged", "checked_at": _desktop_archive_T0}, "delete": {"status": "acknowledged", "checked_at": _desktop_archive_T0}}
        normalized = _desktop_archive_normalize_cleanup_commands(legacy)
        self.assertEqual(normalized["app_server_archive"]["status"], "acknowledged")
        self.assertEqual(normalized["desktop_archive"]["status"], "not_requested")
        v4 = receipt | {
            "version": 4,
            "capability": "supported",
            "strategy": "official_session_archive_then_delete",
            "state": "succeeded",
            "attempts": 1,
            "requested_at": _desktop_archive_T0,
            "last_attempt_at": _desktop_archive_T0,
            "completed_at": _desktop_archive_T0,
            "commands": legacy,
            "verification": {"cli": {"status": "absent", "checked_at": _desktop_archive_T0}, "desktop_backend": {"status": "unavailable", "checked_at": _desktop_archive_T0}, "desktop_live": {"status": "unavailable", "checked_at": _desktop_archive_T0}},
        }
        view = _desktop_archive_session_cleanup_view(v4)
        self.assertEqual(view["version"], 4)
        self.assertEqual(view["commands"]["archive"]["status"], "acknowledged")
        self.assertEqual(view["verification"]["desktop_backend"]["status"], "unavailable")

class ContainerCleanupTests(unittest.TestCase):
    def port(self, factory):
        port = CodexExecutor(transport_factory=factory)
        port.agent_bin = Path('/opt/agentbc/bin/codex')
        port.desktop_archive_broker = mock.Mock()
        return port

    def test_container_uses_official_archive_delete_and_fresh_read_without_desktop(self):
        from agent_bridge_connect.container_session_cleanup import STRATEGY
        first = _action_transport()
        second = _read_transport(read_result={'thread': None})
        port = self.port(TransportFactory(first, second))
        with mock.patch('agent_bridge_connect.container_session_cleanup.enabled', return_value=True):
            result = port.cleanup_session(_request(strategy=STRATEGY))
        self.assertEqual(result.state, 'succeeded')
        self.assertEqual([m['method'] for m in first.sent], ['initialize', 'initialized', 'thread/archive', 'thread/delete'])
        self.assertEqual(second.sent[-1]['method'], 'thread/read')
        self.assertEqual(result.commands['desktop_archive']['status'], 'not_applicable')
        port.desktop_archive_broker.archive.assert_not_called()
        session = V4TransitionGateTests()._session()
        transition = V4TransitionGateTests._transition
        transition(session, 'pending', capability='supported', strategy=STRATEGY)
        receipt = transition(session, 'succeeded', capability='supported', strategy=STRATEGY,
                             verification=result.verification, commands=result.commands)
        self.assertFalse(validate_session_cleanup_receipt(receipt))
        broken = copy.deepcopy(receipt)
        broken['commands']['delete']['status'] = 'not_requested'
        self.assertTrue(validate_session_cleanup_receipt(broken))

    def test_container_reuses_bound_executor_archive_receipt(self):
        from agent_bridge_connect.container_session_cleanup import STRATEGY
        first = FakeTransport([_initialize_response(1), {'id': 2, 'result': {}}])
        second = FakeTransport([_initialize_response(3), {'id': 4, 'result': {'thread': None}}])
        port = self.port(TransportFactory(first, second))
        with mock.patch('agent_bridge_connect.container_session_cleanup.enabled', return_value=True):
            result = port.cleanup_session(_request(strategy=STRATEGY,
                archive_acknowledged=True, archive_checked_at='2026-10-01T15:35:20Z'))
        self.assertEqual(result.state, 'succeeded')
        self.assertEqual([m['method'] for m in first.sent], ['initialize', 'initialized', 'thread/delete'])
        self.assertEqual(result.commands['app_server_archive']['checked_at'], '2026-10-01T15:35:20Z')
        self.assertEqual(result.verification['cli']['status'], 'absent')
        port.desktop_archive_broker.archive.assert_not_called()

    def test_container_retain_unbound_and_native_scope_never_delete(self):
        from agent_bridge_connect.container_session_cleanup import STRATEGY
        for change in ({'retain': True}, {'official_receipt_bound': False}, {}):
            factory = mock.Mock()
            port = self.port(factory)
            with mock.patch('agent_bridge_connect.container_session_cleanup.enabled', return_value=bool(change)):
                result = port.cleanup_session(_request(strategy=STRATEGY, **change))
            self.assertNotEqual(result.state, 'succeeded')
            factory.assert_not_called()
            port.desktop_archive_broker.archive.assert_not_called()

    def test_archive_failure_never_deletes_and_absence_is_required(self):
        from agent_bridge_connect.container_session_cleanup import STRATEGY
        first = _action_transport(archive_response={'jsonrpc': '2.0', 'id': 2,
                                                   'error': {'code': -32603, 'message': 'archive failed'}})
        port = self.port(TransportFactory(first))
        with mock.patch('agent_bridge_connect.container_session_cleanup.enabled', return_value=True):
            result = port.cleanup_session(_request(strategy=STRATEGY))
        self.assertEqual(result.state, 'failed')
        self.assertNotIn('thread/delete', [m['method'] for m in first.sent])
        port = self.port(TransportFactory(_action_transport(), _read_transport(read_result={'thread': {'id': SESSION_ID}})))
        with mock.patch('agent_bridge_connect.container_session_cleanup.enabled', return_value=True):
            self.assertEqual(port.cleanup_session(_request(strategy=STRATEGY)).state, 'failed')
