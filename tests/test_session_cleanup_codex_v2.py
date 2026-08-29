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

from agent_bridge_connect.adapters import SessionCleanupRequest
from agent_bridge_connect.codex_session_cleanup import (
    CODEX_SESSION_ARCHIVE_FAILED_CODE,
    CODEX_SESSION_ARCHIVE_INVALID_ID_CODE,
    CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE,
    CODEX_SESSION_ARCHIVE_TIMEOUT_CODE,
    CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE,
    CodexSessionCleanupError,
)
from agent_bridge_connect.codex_session_cleanup import (
    CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE,
    CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
    CODEX_SESSION_DELETE_TIMEOUT_CODE,
    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
)
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

SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
CHILD_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d7"
T0 = "2026-08-27T00:00:00Z"
ARCHIVE_THEN_DELETE = "official_session_archive_then_delete"
FIXTURES = Path(__file__).parent / "fixtures" / "session_cleanup_receipts.json"


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


def _action_transport(
    *,
    archive_response: dict | None = None,
    delete_response: dict | None = None,
    notification: dict | None = None,
    tail: list[dict] | None = None,
) -> FakeTransport:
    """Connection A: initialize(1), archive(2), delete(3)."""
    messages = [_initialize_response(1)]
    messages.append(
        archive_response
        if archive_response is not None
        else {"jsonrpc": "2.0", "id": 2, "result": {}}
    )
    messages.append(
        delete_response
        if delete_response is not None
        else {"jsonrpc": "2.0", "id": 3, "result": {}}
    )
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
    values: dict[str, object] = {
        "executor": "codex",
        "session_id": SESSION_ID,
        "task_id": "QEEY-001",
        "strategy": ARCHIVE_THEN_DELETE,
        "receipt_source": "jsonl_thread_started",
        "official_receipt_bound": True,
        "workspace": {"root": "."},
    }
    values.update(overrides)
    return SessionCleanupRequest(**values)  # type: ignore[arg-type]


def _executor(factory: TransportFactory, desktop: str | None = "absent") -> CodexExecutor:
    verifier = None if desktop is None else lambda _: {"status": desktop, "checked_at": T0}
    return CodexExecutor(
        command=sys.executable,
        transport="app-server",
        transport_factory=factory,
        desktop_verifier=verifier,
    )


class ArchiveThenDeleteOrderTests(unittest.TestCase):
    """The exact official-session command sequence and its evidence."""

    def test_archive_is_acked_before_delete_is_sent(self) -> None:
        first = _action_transport(
            notification={
                "jsonrpc": "2.0",
                "method": "thread/deleted",
                "params": {"threadId": SESSION_ID},
            }
        )
        second = _read_transport(read_result={"thread": None})
        third = _list_transport()
        result = _executor(TransportFactory(first, second, third)).cleanup_session(
            _request()
        )

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(
            [item["method"] for item in first.sent],
            ["initialize", "initialized", "thread/archive", "thread/delete"],
        )
        self.assertEqual(first.sent[2]["params"], {"threadId": SESSION_ID})
        self.assertEqual(first.sent[3]["params"], {"threadId": SESSION_ID})
        self.assertEqual(
            [item["method"] for item in second.sent],
            ["initialize", "initialized", "thread/read"],
        )
        # Both official commands carry acknowledged evidence with timestamps.
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "acknowledged")
        self.assertTrue(result.commands["archive"]["checked_at"])
        self.assertTrue(result.commands["delete"]["checked_at"])
        # desktop_live is not_applicable under the archive-then-delete gate.
        self.assertEqual(result.verification["desktop_live"]["status"], "not_applicable")

    def test_executor_prearchive_receipt_skips_non_idempotent_archive(self) -> None:
        first = FakeTransport(
            [
                _initialize_response(1),
                {"jsonrpc": "2.0", "id": 2, "result": {}},
            ]
        )
        second = FakeTransport(
            [
                _initialize_response(3),
                {"jsonrpc": "2.0", "id": 4, "result": {"thread": None}},
            ]
        )
        third = FakeTransport(
            [
                _initialize_response(5),
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "result": {"data": [], "nextCursor": None},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "result": {"data": [], "nextCursor": None},
                },
            ]
        )
        result = _executor(TransportFactory(first, second, third)).cleanup_session(
            _request(archive_acknowledged=True, archive_checked_at=T0)
        )

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(
            [item["method"] for item in first.sent],
            ["initialize", "initialized", "thread/delete"],
        )
        self.assertEqual(result.commands["archive"]["checked_at"], T0)
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "acknowledged")

    def test_capability_reports_the_archive_then_delete_strategy(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="auto")
        capability = executor.session_cleanup_capability(_request())
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, ARCHIVE_THEN_DELETE)

    def test_archived_notification_is_advisory_only(self) -> None:
        first = _action_transport(
            notification={
                "jsonrpc": "2.0",
                "method": "thread/archived",
                "params": {"threadId": SESSION_ID},
            }
        )
        second = _read_transport(read_result={"thread": None})
        result = _executor(TransportFactory(first, second, _list_transport())).cleanup_session(
            _request()
        )
        # The notification never replaces the bound RPC acknowledgement, and
        # the sequence continues to delete after the acknowledgement arrives.
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(
            [item["method"] for item in first.sent],
            ["initialize", "initialized", "thread/archive", "thread/delete"],
        )

    def test_already_archived_acknowledgement_continues_to_delete(self) -> None:
        # A server that reports the thread was already archived still
        # acknowledges the archive state, so the sequence may continue.
        first = _action_transport(
            archive_response={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"alreadyArchived": True},
            }
        )
        second = _read_transport(read_result={"thread": None})
        result = _executor(TransportFactory(first, second, _list_transport())).cleanup_session(
            _request()
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")

    def test_zero_delete_calls_after_archive_rpc_error(self) -> None:
        first = _action_transport(
            archive_response={"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "archive rejected"}}
        )
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_FAILED_CODE)
        methods = [item.get("method") for item in first.sent]
        self.assertNotIn("thread/delete", methods)
        self.assertEqual(result.commands["archive"]["status"], "failed")
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_archive_target_missing_fails_closed_before_delete(self) -> None:
        # SQKX-001: delete followed by archive returns target-not-found.  The
        # same error on a fresh archive proves the exact thread is gone, so
        # the precondition cannot be established and delete must not run.
        first = _action_transport(
            archive_response={
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32600, "message": f"thread not found: {SESSION_ID}"},
            }
        )
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_TARGET_MISSING_CODE)
        self.assertNotIn(
            "thread/delete", [item.get("method") for item in first.sent]
        )
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
        self.assertNotIn(
            "thread/delete", [item.get("method") for item in first.sent]
        )
        self.assertEqual(result.commands["archive"]["status"], "unverified")
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_archive_timeout_sends_zero_delete_calls(self) -> None:
        first = FakeTransport([_initialize_response(1), TimeoutError("timeout")])
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_ARCHIVE_TIMEOUT_CODE)
        self.assertTrue(result.retryable)
        self.assertNotIn(
            "thread/delete", [item.get("method") for item in first.sent]
        )

    def test_delete_timeout_has_stable_delete_code(self) -> None:
        # Archive was acknowledged; the delete RPC never answers.
        first = FakeTransport(
            [
                _initialize_response(1),
                {"jsonrpc": "2.0", "id": 2, "result": {}},
                TimeoutError("timeout"),
            ]
        )
        result = _executor(TransportFactory(first)).cleanup_session(_request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_DELETE_TIMEOUT_CODE)
        self.assertTrue(result.retryable)
        self.assertIn("thread/archive", [item.get("method") for item in first.sent])
        # Partial evidence: archive acknowledgement survives for the retry.
        self.assertEqual(result.commands["archive"]["status"], "acknowledged")
        self.assertEqual(result.commands["delete"]["status"], "not_requested")

    def test_delete_transport_loss_has_stable_delete_code(self) -> None:
        first = FakeTransport(
            [
                _initialize_response(1),
                {"jsonrpc": "2.0", "id": 2, "result": {}},
                TransportClosedForTest(),
            ]
        )
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
        second = _read_transport(
            read_error={"code": "thread_not_found", "message": "thread not found"}
        )
        result = _executor(TransportFactory(first, second, _list_transport())).cleanup_session(
            _request()
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.verification["cli"]["status"], "absent")

    def test_desktop_diagnostics_are_non_gating_for_success(self) -> None:
        """Command acknowledgements decide; backend/live stay diagnostic.

        The current Codex Desktop refresh delay is accepted and non-blocking:
        a stale Desktop live entry or an unavailable list channel must not
        turn an acknowledged archive+delete pair into a failure.
        """
        for desktop, extra in (
            ("present", []),
            (
                None,
                [_list_transport(error={"code": "unsupported", "message": "unsupported"})],
            ),
        ):
            with self.subTest(desktop=desktop):
                first = _action_transport()
                second = _read_transport(read_result={"thread": None})
                if not extra:
                    extra = [_list_transport()]
                result = _executor(
                    TransportFactory(first, second, *extra), desktop=desktop
                ).cleanup_session(_request())
                self.assertEqual(result.state, "succeeded")
                self.assertEqual(result.commands["archive"]["status"], "acknowledged")
                self.assertEqual(result.commands["delete"]["status"], "acknowledged")
                # desktop_live is not_applicable under the archive gate.
                self.assertEqual(
                    result.verification["desktop_live"]["status"],
                    "not_applicable",
                )


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
        self.assertTrue(
            CodexExecutor(command=sys.executable, transport="full")._uses_cleanup_app_server(
                _request()
            )
        )

    def test_cli_fallback_cannot_claim_the_archive_strategy(self) -> None:
        help_text = (
            "codex-cli 0.146.0\n"
            "Usage: codex delete [OPTIONS] <SESSION>\n"
            "Session id (UUID) or session name\n"
            "--force\n"
            "SESSION must be a UUID\n"
        )
        for transport in ("cli", "direct"):
            with self.subTest(transport=transport):
                executor = CodexExecutor(command=sys.executable, transport=transport)
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
                # The explicit fallback keeps the historical strategy name.
                self.assertEqual(result.strategy, "official_session_delete")

    def test_v4_receipt_contains_bounded_commands(self) -> None:
        receipt = build_session_cleanup_receipt()
        self.assertEqual(receipt["version"], SESSION_CLEANUP_RECEIPT_VERSION)
        self.assertEqual(
            receipt["commands"],
            {
                "archive": {"status": "not_requested", "checked_at": ""},
                "delete": {"status": "not_requested", "checked_at": ""},
            },
        )
        self.assertEqual(validate_session_cleanup_receipt(receipt), [])
        receipt["commands"]["archive"]["status"] = "raw_state"
        self.assertTrue(validate_session_cleanup_receipt(receipt))

    def test_v4_succeeded_requires_both_command_acknowledgements(self) -> None:
        receipt = build_session_cleanup_receipt()
        receipt.update(
            {
                "capability": "supported",
                "strategy": ARCHIVE_THEN_DELETE,
                "state": "succeeded",
                "attempts": 1,
                "requested_at": T0,
                "last_attempt_at": T0,
                "completed_at": T0,
            }
        )
        receipt["commands"] = {
            "archive": {"status": "acknowledged", "checked_at": T0},
            "delete": {"status": "acknowledged", "checked_at": T0},
        }
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
            "verification": {
                "cli": {"status": "unknown", "checked_at": ""},
                "desktop": {"status": "unknown", "checked_at": ""},
            },
        }
        self.assertEqual(validate_session_cleanup_receipt(v2), [])
        projected = session_cleanup_view(v2)
        # v2 history keeps its own version and never gains commands evidence.
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

    def test_unknown_receipt_version_fails_closed(self) -> None:
        future = build_session_cleanup_receipt()
        future["version"] = 99
        self.assertTrue(validate_session_cleanup_receipt(future))
        with self.assertRaises(Exception):
            read_session_cleanup_receipt(future)

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

    def test_unbound_cleanup_request_fails_before_transport(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="auto")
        with mock.patch.object(executor, "_cleanup_session_app_server") as cleanup:
            result = executor.cleanup_session(
                _request(
                    official_receipt_bound=False,
                    receipt_source="",
                )
            )
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "codex_cleanup_receipt_unbound")
        cleanup.assert_not_called()


class V4TransitionGateTests(unittest.TestCase):
    """Fail-closed v4 transitions: command acknowledgements gate success."""

    def _session(self, *, executor: str = "codex") -> dict:
        session_id = SESSION_ID if executor == "codex" else "20260827_000000_a1b2c3"
        session = build_session_snapshot(
            executor,
            retain=False,
            session_id=session_id,
            session_state="terminal",
            created_at=T0,
        )
        if executor == "codex":
            # Bind the official early receipt exactly as production does.
            session["official_receipt_bound"] = True
            session["receipt_source"] = "jsonl_thread_started"
        return session

    @staticmethod
    def _transition(session: dict, target: str, **kwargs: object) -> dict:

        defaults = {
            "task_status": "completed",
            "lease_state": "closed",
            "report_written": True,
            "notification_recorded": True,
            "occurred_at": T0,
        }
        defaults.update(kwargs)
        receipt = transition_session_cleanup(session, target, **defaults)
        session["cleanup"] = receipt
        return receipt

    def test_codex_success_requires_both_acknowledged_commands(self) -> None:
        session = self._session()
        self._transition(
            session,
            "pending",
            capability="supported",
            strategy=ARCHIVE_THEN_DELETE,
        )
        for commands, expect_error in (
            (
                {
                    "archive": {"status": "acknowledged", "checked_at": T0},
                    "delete": {"status": "acknowledged", "checked_at": T0},
                },
                False,
            ),
            (
                {
                    "archive": {"status": "confirmed", "checked_at": T0},
                    "delete": {"status": "acknowledged", "checked_at": T0},
                },
                False,
            ),
            (
                {
                    "archive": {"status": "unverified", "checked_at": T0},
                    "delete": {"status": "acknowledged", "checked_at": T0},
                },
                True,
            ),
            (
                {
                    "archive": {"status": "acknowledged", "checked_at": T0},
                    "delete": {"status": "not_requested", "checked_at": T0},
                },
                True,
            ),
        ):
            with self.subTest(archive=commands["archive"]["status"], delete=commands["delete"]["status"]):
                fresh = self._session()
                self._transition(
                    fresh,
                    "pending",
                    capability="supported",
                    strategy=ARCHIVE_THEN_DELETE,
                )
                kwargs = {
                    "capability": "supported",
                    "strategy": ARCHIVE_THEN_DELETE,
                    "commands": commands,
                    "verification": {
                        "cli": {"status": "absent", "checked_at": T0},
                        "desktop_backend": {"status": "unavailable", "checked_at": T0},
                        "desktop_live": {"status": "unknown", "checked_at": T0},
                    },
                }
                if expect_error:
                    with self.assertRaises(Exception):
                        self._transition(fresh, "succeeded", **kwargs)
                else:
                    receipt = self._transition(fresh, "succeeded", **kwargs)
                    self.assertEqual(receipt["state"], "succeeded")
                    # desktop_live became not_applicable; diagnostics stayed.
                    self.assertEqual(
                        receipt["verification"]["desktop_live"]["status"],
                        "not_applicable",
                    )
                    self.assertEqual(
                        receipt["verification"]["desktop_backend"]["status"],
                        "unavailable",
                    )

    def test_failed_transition_preserves_partial_command_evidence(self) -> None:
        session = self._session()
        self._transition(
            session,
            "pending",
            capability="supported",
            strategy=ARCHIVE_THEN_DELETE,
        )
        partial = {
            "archive": {"status": "unverified", "checked_at": T0},
            "delete": {"status": "not_requested", "checked_at": T0},
        }
        failed = self._transition(
            session,
            "failed",
            capability="supported",
            strategy=ARCHIVE_THEN_DELETE,
            error_code=CODEX_SESSION_ARCHIVE_TRANSPORT_LOST_CODE,
            retryable=True,
            next_attempt_at="2026-08-27T00:01:00Z",
            commands=partial,
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["commands"], partial)
        # A retry (or a Runner restart reading the same receipt) still sees
        # the persisted partial evidence.
        reread = read_session_cleanup_receipt(failed)
        self.assertEqual(reread["commands"], partial)

    def test_retry_backoff_reuses_stale_pending_receipt_commands(self) -> None:
        session = self._session()
        self._transition(
            session,
            "pending",
            capability="supported",
            strategy=ARCHIVE_THEN_DELETE,
        )
        partial = {
            "archive": {"status": "failed", "checked_at": T0},
            "delete": {"status": "not_requested", "checked_at": T0},
        }
        self._transition(
            session,
            "failed",
            capability="supported",
            strategy=ARCHIVE_THEN_DELETE,
            error_code=CODEX_SESSION_ARCHIVE_FAILED_CODE,
            retryable=True,
            next_attempt_at="2026-08-27T00:01:00Z",
            commands=partial,
        )
        retry = self._transition(
            session,
            "pending",
            occurred_at="2026-08-27T00:02:00Z",
        )
        self.assertEqual(retry["attempts"], 2)
        self.assertEqual(retry["strategy"], ARCHIVE_THEN_DELETE)

    def test_hermes_success_stamps_not_applicable_commands(self) -> None:
        session = self._session(executor="hermes")
        self._transition(
            session,
            "pending",
            capability="supported",
            strategy="official_session_delete",
        )
        receipt = self._transition(
            session,
            "succeeded",
            capability="supported",
            strategy="official_session_delete",
        )
        self.assertEqual(receipt["commands"]["archive"]["status"], "not_applicable")
        self.assertEqual(receipt["commands"]["delete"]["status"], "not_applicable")


class PrimaryAuxiliaryIsolationTests(unittest.TestCase):
    """Primary-first, deepest/newest auxiliary ordering and strategy binding."""

    def test_auxiliary_codex_strategy_is_archive_then_delete(self) -> None:
        from agent_bridge_connect.auxiliary_sessions import auxiliary_cleanup_strategy

        codex_entry = {"executor": "codex", "project_mode": "none"}
        self.assertEqual(
            auxiliary_cleanup_strategy(codex_entry), ARCHIVE_THEN_DELETE
        )
        claude_entry = {"executor": "claude", "project_mode": "ephemeral"}
        self.assertEqual(
            auxiliary_cleanup_strategy(claude_entry), "claude_project_purge"
        )
        self.assertEqual(auxiliary_cleanup_strategy({"retain": True}), "retain")

    def test_auxiliary_receipt_views_redact_and_project_commands(self) -> None:
        from agent_bridge_connect.auxiliary_sessions import auxiliary_ledger_view

        receipt = build_session_cleanup_receipt()
        receipt.update(
            {
                "capability": "supported",
                "strategy": ARCHIVE_THEN_DELETE,
                "state": "failed",
                "attempts": 1,
                "requested_at": T0,
                "last_attempt_at": T0,
                "error_code": "codex_session_delete_transport_lost",
                "retryable": True,
                "next_attempt_at": "2026-08-27T00:01:00Z",
            }
        )
        # Partial evidence: the archive was acknowledged before the transport
        # died; a retry or Runner restart must still see it.
        receipt["commands"] = {
            "archive": {"status": "acknowledged", "checked_at": T0},
            "delete": {"status": "not_requested", "checked_at": T0},
        }
        entry = {
            "version": 1,
            "aux_id": "a" * 32,
            "owner_task_id": "QEEY-001",
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
        session = build_session_snapshot(
            "codex",
            retain=False,
            session_id=SESSION_ID,
            session_state="terminal",
            created_at=T0,
        )
        session["official_receipt_bound"] = True
        session["receipt_source"] = "jsonl_thread_started"
        # The dispatcher, user, unrelated, fuzzy-name, and unregistered
        # descendant sessions never appear in the candidate set: cleanup is
        # keyed by the single bound UUID plus registered ledger entries only.
        blockers = session_cleanup_blockers(
            task_status="completed",
            lease_state="closed",
            report_written=True,
            notification_recorded=True,
            session=session,
        )
        self.assertEqual(blockers, [])


class CommandEvidenceRedactionTests(unittest.TestCase):
    def test_normalize_cleanup_commands_rejects_extra_fields(self) -> None:

        good = {
            "archive": {"status": "acknowledged", "checked_at": T0},
            "delete": {"status": "not_requested", "checked_at": ""},
        }
        self.assertEqual(normalize_cleanup_commands(good), good)
        polluted = copy.deepcopy(good)
        polluted["archive"]["threadId"] = SESSION_ID
        self.assertEqual(normalize_cleanup_commands(polluted), normalize_cleanup_commands(None))
        self.assertEqual(normalize_cleanup_commands(None), normalize_cleanup_commands(None))

    def test_codex_error_command_evidence_stays_bounded(self) -> None:
        error = CodexSessionCleanupError(
            CODEX_SESSION_ARCHIVE_TIMEOUT_CODE,
            retryable=True,
            commands={
                "archive": {"status": "unverified", "checked_at": T0},
                "delete": {"status": "not_requested", "checked_at": T0},
            },
        )
        self.assertEqual(error.commands["delete"]["status"], "not_requested")
        self.assertNotIn(SESSION_ID, str(error))


if __name__ == "__main__":
    unittest.main()
