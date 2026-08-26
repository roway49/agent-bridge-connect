"""SESSION-103-002 E2E/canary session supervisor tests.

Covers receipt-driven create/bind journaling before test actions, try/finally
plus SIGINT/SIGTERM/KeyboardInterrupt teardown, success/Deny/timeout/transport
loss/crash/Runner-restart paths, bounded retries, immutable retain, public
redaction, idempotent delete replay, dispatcher exclusion, and task-attached
auxiliary ledger integration.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.adapters import SessionCleanupResult
from agent_bridge_connect.auxiliary_sessions import (
    AUXILIARY_EXTENSION_KEY,
    read_auxiliary_ledger,
)
from agent_bridge_connect.e2e_session_supervisor import (
    E2ESessionSupervisor,
)
from agent_bridge_connect.service import TaskService

T0 = "2026-08-25T00:00:00Z"


class FakeCleanupPort:
    """Configurable official exact-session cleanup port."""

    def __init__(self, result: SessionCleanupResult | None = None) -> None:
        self.result = result or SessionCleanupResult(
            state="succeeded",
            capability="supported",
            strategy="official_session_delete",
        )
        self.calls: list = []
        self.raise_error: BaseException | None = None
        self.not_found_ids: set = set()

    def cleanup_session(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.raise_error is not None:
            raise self.raise_error
        if request.session_id in self.not_found_ids:
            return SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
                error_code="",
            )
        return self.result


def _creator(executor: str, session_id: str, source: str):
    def _create():
        return {
            "version": 1,
            "executor": executor,
            "session_id": session_id,
            "resumed": False,
            "persistence": "persistent",
            "source": source,
        }

    return _create


class E2ESessionSupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = self.root / "journal.jsonl"
        self.port = FakeCleanupPort()
        self.supervisor = E2ESessionSupervisor(
            task_id="E2E-001",
            run_id="run-1",
            retain=False,
            journal_path=self.journal,
            cleanup_port=self.port,
        )

    def _journal(self) -> list[dict]:
        return self.supervisor.journal.latest_entries()

    # ------------------------------------------------------------ success
    def test_success_path_journals_receipt_before_actions_and_tears_down(self) -> None:
        created_at_before = None
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-1", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
            # The official receipt is durably journaled before the handle is
            # usable by the test body.
            latest = self._journal()[-1]
            self.assertEqual(latest["session_id"], "SESS-1")
            self.assertEqual(latest["session_state"], "active")
            self.assertTrue(handle.ref.startswith("sess_"))
            handle.mark_terminal()
            created_at_before = len(self.port.calls)
        # try/finally teardown ran the official cleanup exactly once.
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-1")
        self.assertEqual(self.port.calls[0].executor, "hermes")
        latest = self._journal()[-1]
        self.assertEqual(latest["cleanup"]["state"], "succeeded")
        self.assertEqual(latest["cleanup"]["attempts"], 1)
        self.assertGreater(len(self._journal()), created_at_before)

    def test_deny_and_process_exception_still_reach_teardown(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.supervisor.guarded(install_signals=False):
                self.supervisor.open_session(
                    "hermes",
                    "canary_probe",
                    _creator("hermes", "SESS-DENY", "stderr_receipt"),
                    parent_executor="claude",
                    parent_session_id="PARENT-1",
                )
                raise RuntimeError("user denied the permission")
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-DENY")

    def test_creator_failure_never_binds_or_cleans_a_session(self) -> None:
        def _failing_creator():
            raise RuntimeError("transport lost during thread/start")

        with self.assertRaises(RuntimeError):
            with self.supervisor.guarded(install_signals=False):
                self.supervisor.open_session(
                    "hermes",
                    "canary_probe",
                    _failing_creator,
                    parent_executor="claude",
                    parent_session_id="PARENT-1",
                )
        # No official session was created, so no cleanup call is possible and
        # the durable reservation was never bound.
        self.assertEqual(self.port.calls, [])
        entries = self._journal()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_state"], "reserved")
        self.assertEqual(entries[0]["session_id"], "")

    def test_test_interruption_via_keyboard_interrupt_reaches_teardown(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with self.supervisor.guarded(install_signals=False):
                self.supervisor.open_session(
                    "hermes",
                    "canary_probe",
                    _creator("hermes", "SESS-INT", "stderr_receipt"),
                    parent_executor="claude",
                    parent_session_id="PARENT-1",
                )
                raise KeyboardInterrupt
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-INT")

    def test_sigterm_is_translated_to_interrupt_and_tears_down(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with self.supervisor.guarded(install_signals=True):
                self.supervisor.open_session(
                    "hermes",
                    "canary_probe",
                    _creator("hermes", "SESS-SIG", "stderr_receipt"),
                    parent_executor="claude",
                    parent_session_id="PARENT-1",
                )
                os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(self.port.calls[0].session_id, "SESS-SIG")

    def test_cleanup_exception_is_journaled_as_retryable_failure(self) -> None:
        self.port.raise_error = RuntimeError("transport loss during delete")
        with self.supervisor.guarded(install_signals=False):
            self.supervisor.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-BUSY", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
        latest = self._journal()[-1]
        self.assertEqual(latest["cleanup"]["state"], "failed")
        self.assertEqual(latest["cleanup"]["error_code"], "session_cleanup_failed")
        self.assertTrue(latest["cleanup"]["retryable"])
        # A restart replays only its own unresolved exact receipt.
        restarted = E2ESessionSupervisor(
            task_id="E2E-001",
            run_id="run-1",
            retain=False,
            journal_path=self.journal,
            cleanup_port=self.port,
        )
        self.port.raise_error = None
        self.port.result = SessionCleanupResult(
            state="succeeded",
            capability="supported",
            strategy="official_session_delete",
        )
        replayed = restarted.replay_unresolved()
        self.assertEqual(len(replayed), 1)
        self.assertEqual(replayed[0]["status"], "succeeded")
        self.assertEqual(len(self.port.calls), 2)

    # ------------------------------------------------------------- restart
    def test_restart_replays_only_own_unresolved_receipts_before_new_session(self) -> None:
        # First run leaves one unresolved session (cleanup failed).
        self.port.result = SessionCleanupResult(
            state="failed",
            capability="supported",
            strategy="official_session_delete",
            error_code="session_delete_busy",
            retryable=True,
        )
        with self.supervisor.guarded(install_signals=False):
            self.supervisor.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-UNRESOLVED", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
        self.assertEqual(self._journal()[-1]["cleanup"]["state"], "failed")

        # Restart: replay must clean the unresolved exact receipt before the
        # new session is created.
        restarted = E2ESessionSupervisor(
            task_id="E2E-001",
            run_id="run-1",
            retain=False,
            journal_path=self.journal,
            cleanup_port=self.port,
        )
        self.port.result = SessionCleanupResult(
            state="succeeded",
            capability="supported",
            strategy="official_session_delete",
        )
        replayed = restarted.replay_unresolved()
        self.assertEqual(len(replayed), 1)
        self.assertTrue(replayed[0]["ref"].startswith("sess_"))
        self.assertEqual(len(self.port.calls), 2)
        # New sessions may only be created after the replay finished.
        with restarted.guarded(install_signals=False):
            handle = restarted.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-NEW", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
            handle.mark_terminal()
        self.assertEqual(len(self.port.calls), 3)

    # -------------------------------------------------------------- retain
    def test_retain_true_is_immutable_and_skips_cleanup(self) -> None:
        retained = E2ESessionSupervisor(
            task_id="E2E-001",
            run_id="run-1",
            retain=True,
            journal_path=self.root / "retain.jsonl",
            cleanup_port=self.port,
        )
        with retained.guarded(install_signals=False):
            handle = retained.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-RETAIN", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
            self.assertTrue(handle.retain)
            handle.mark_terminal()
        # retain=true sessions are never sent to the cleanup adapter.
        self.assertEqual(self.port.calls, [])
        latest = retained.journal.latest_entries()[-1]
        self.assertEqual(latest["cleanup"]["state"], "retained")
        self.assertEqual(latest["cleanup"]["capability"], "not_applicable")

    # ------------------------------------------------------------ redaction
    def test_public_evidence_uses_only_redacted_refs_and_safe_fields(self) -> None:
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SECRET-SESSION-ID", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="SECRET-PARENT",
            )
            view = handle.public_view()
            handle.mark_terminal()
        serialized = json.dumps(view)
        for sensitive in ("SECRET-SESSION-ID", "SECRET-PARENT", "stderr_receipt"):
            self.assertNotIn(sensitive, serialized)
        self.assertTrue(view["ref"].startswith("sess_"))
        self.assertEqual(view["executor"], "hermes")
        self.assertEqual(view["purpose"], "canary_probe")
        self.assertFalse(view["retain"])

    # ------------------------------------------------- dispatcher exclusion
    def test_dispatcher_conversation_is_never_collected_or_deleted(self) -> None:
        # The supervisor only knows sessions it created via open_session; the
        # dispatcher conversation id is never a session the journal tracks.
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-OWN", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
            handle.mark_terminal()
        journal_ids = {entry.get("session_id") for entry in self._journal()}
        self.assertNotIn("dispatcher-conversation-id", journal_ids)
        self.assertEqual(journal_ids, {"SESS-OWN"})
        self.assertEqual(
            [call.session_id for call in self.port.calls],
            ["SESS-OWN"],
        )

    # ---------------------------------------------------- task integration
    def test_task_attached_run_mirrors_into_auxiliary_ledger(self) -> None:
        board = self.root / "record"
        service = TaskService(
            board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )
        task = service.create_task(
            "supervised canary",
            "codex",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )
        task_id = task.id
        attached = E2ESessionSupervisor(
            task_id=task_id,
            run_id="run-1",
            retain=False,
            journal_path=self.root / "attached.jsonl",
            board_root=board,
            cleanup_port=self.port,
        )
        with attached.guarded(install_signals=False):
            handle = attached.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-TASK", "stderr_receipt"),
                parent_executor="codex",
                parent_session_id="PRIMARY-SESS",
            )
            handle.mark_terminal()
        raw = service.store.read_task(task_id)
        ledger = read_auxiliary_ledger(raw["extensions"])
        self.assertEqual(len(ledger["sessions"]), 1)
        entry = ledger["sessions"][0]
        self.assertEqual(entry["session_id"], "SESS-TASK")
        self.assertEqual(entry["executor"], "hermes")
        self.assertEqual(entry["cleanup"]["state"], "succeeded")
        # The public projection is redacted even though the internal ledger is
        # the authoritative exact-session record.
        from agent_bridge_connect.auxiliary_sessions import auxiliary_ledger_view

        view = auxiliary_ledger_view(raw["extensions"][AUXILIARY_EXTENSION_KEY])
        serialized = json.dumps(view)
        self.assertNotIn("SESS-TASK", serialized)
        self.assertNotIn("PRIMARY-SESS", serialized)

    def test_idempotent_delete_replay_never_duplicates_cleanup(self) -> None:
        with self.supervisor.guarded(install_signals=False):
            handle = self.supervisor.open_session(
                "hermes",
                "canary_probe",
                _creator("hermes", "SESS-1", "stderr_receipt"),
                parent_executor="claude",
                parent_session_id="PARENT-1",
            )
            handle.mark_terminal()
        calls_after_first = len(self.port.calls)
        self.assertEqual(calls_after_first, 1)
        # Re-running teardown on the same journal is a no-op replay.
        self.supervisor.teardown()
        self.assertEqual(len(self.port.calls), calls_after_first)


if __name__ == "__main__":
    unittest.main()
