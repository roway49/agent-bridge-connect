"""SESSION-103-003 auxiliary session ledger and terminal cleanup tests.

Covers the two-phase reserve/bind path, conflict/idempotency rules, immutable
retain, primary-first then auxiliary cleanup ordering, continued cleanup after
primary failure, report/Doctor acceptance blockers, public redaction, and the
dispatcher exclusion invariant.  Uses fake ExecutorPorts and a temporary board;
never invokes a real Executor.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from agent_bridge_connect.adapters import SessionCleanupResult
from agent_bridge_connect.auxiliary_sessions import (
    AUXILIARY_EXTENSION_KEY,
    auxiliary_ledger_view,
    bind_auxiliary_receipt,
    build_auxiliary_ledger,
    mark_auxiliary_terminal,
    read_auxiliary_ledger,
    redact_session_ref,
    reserve_auxiliary_session,
    validate_auxiliary_ledger,
)
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.run_lease import create_lease, save_lease
from agent_bridge_connect.service import TaskService

T0 = "2026-08-25T00:00:00Z"


def _add_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _official_receipt(executor: str, session_id: str, source: str) -> dict:
    return {
        "version": 1,
        "executor": executor,
        "session_id": session_id,
        "resumed": False,
        "persistence": "persistent",
        "source": source,
    }


class FakeCleanupExecutor:
    """Fake ExecutorPort cleanup_session returning a configurable result."""

    def __init__(self, result: SessionCleanupResult | None = None) -> None:
        self.result = result or SessionCleanupResult(
            state="failed",
            capability="supported",
            strategy="official_session_delete",
            error_code="session_delete_busy",
            retryable=True,
        )
        self.calls: list = []
        self.raise_error: BaseException | None = None

    def cleanup_session(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


class AuxiliaryLedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )
        self.task = self.service.create_task(
            "auxiliary ledger",
            "codex",
            [{"id": 1, "description": "run"}],
            customer_dir=False,
        )

    # ------------------------------------------------------------- two-phase
    def test_bind_without_reservation_fails_closed(self) -> None:
        extensions = {
            AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger(),
        }
        with self.assertRaises(ABCError) as ctx:
            bind_auxiliary_receipt(
                extensions,
                aux_id="0" * 32,
                receipt=_official_receipt("hermes", "SESS", "stderr_receipt"),
            )
        self.assertEqual(ctx.exception.code, "auxiliary_receipt_missing")

    def test_reserve_then_bind_freezes_official_receipt(self) -> None:
        extensions = {
            AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger(),
        }
        extensions, reserved = reserve_auxiliary_session(
            extensions,
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        self.assertEqual(reserved["session_state"], "reserved")
        self.assertEqual(reserved["session_id"], "")
        self.assertEqual(reserved["cleanup"]["state"], "not_requested")

        extensions, bound = bind_auxiliary_receipt(
            extensions,
            aux_id=reserved["aux_id"],
            receipt=_official_receipt("hermes", "CHILD-1", "stderr_receipt"),
        )
        self.assertEqual(bound["session_state"], "active")
        self.assertEqual(bound["session_id"], "CHILD-1")
        self.assertEqual(bound["source"], "stderr_receipt")
        self.assertTrue(bound["bound_at"])
        # Bound entry is durable and re-readable.
        ledger = read_auxiliary_ledger(extensions)
        self.assertEqual(ledger["sessions"][0]["session_id"], "CHILD-1")

    def test_reserve_requires_owner_run_and_parent(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        base = dict(
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        for overrides in (
            {"owner_run_id": ""},
            {"parent_session_id": ""},
            {"parent_executor": "unknown"},
            {"executor": "unknown"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ABCError):
                    reserve_auxiliary_session(extensions, **{**base, **overrides})

    # ------------------------------------------------------- idempotency
    def test_idempotent_duplicate_reservation_returns_existing(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        common = dict(
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        extensions, first = reserve_auxiliary_session(extensions, **common)
        extensions, second = reserve_auxiliary_session(extensions, **common)
        self.assertEqual(first["aux_id"], second["aux_id"])
        self.assertEqual(len(read_auxiliary_ledger(extensions)["sessions"]), 1)

    def test_conflicting_frozen_fields_fail_closed(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        common = dict(
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        extensions, _ = reserve_auxiliary_session(extensions, **common)
        with self.assertRaises(ABCError) as ctx:
            reserve_auxiliary_session(extensions, **{**common, "retain": True})
        self.assertEqual(ctx.exception.code, "auxiliary_reservation_conflict")

    def test_duplicate_reservation_with_mismatched_owner_task_fails_closed(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        common = dict(
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        extensions, first = reserve_auxiliary_session(
            extensions, owner_task_id=self.task.id, **common
        )
        with self.assertRaises(ABCError) as ctx:
            reserve_auxiliary_session(extensions, owner_task_id="OTHER-TASK", **common)
        self.assertEqual(ctx.exception.code, "auxiliary_reservation_conflict")
        self.assertIn("owner_task_id", ctx.exception.details["conflicts"])
        # The ledger is unchanged: only the original reservation exists.
        self.assertEqual(len(read_auxiliary_ledger(extensions)["sessions"]), 1)
        self.assertEqual(read_auxiliary_ledger(extensions)["sessions"][0]["aux_id"], first["aux_id"])

    def test_receipt_conflict_fails_closed_and_same_rebind_is_idempotent(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        extensions, reserved = reserve_auxiliary_session(
            extensions,
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        extensions, bound = bind_auxiliary_receipt(
            extensions,
            aux_id=reserved["aux_id"],
            receipt=_official_receipt("hermes", "CHILD-1", "stderr_receipt"),
        )
        # Same exact session rebind is idempotent.
        extensions, again = bind_auxiliary_receipt(
            extensions,
            aux_id=reserved["aux_id"],
            receipt=_official_receipt("hermes", "CHILD-1", "stderr_receipt"),
        )
        self.assertEqual(again["aux_id"], bound["aux_id"])
        # A different session fails closed.
        with self.assertRaises(ABCError) as ctx:
            bind_auxiliary_receipt(
                extensions,
                aux_id=reserved["aux_id"],
                receipt=_official_receipt("hermes", "CHILD-2", "stderr_receipt"),
            )
        self.assertEqual(ctx.exception.code, "auxiliary_receipt_conflict")

    def test_receipt_executor_mismatch_fails_closed(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        extensions, reserved = reserve_auxiliary_session(
            extensions,
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=False,
            created_at=T0,
        )
        with self.assertRaises(ABCError):
            bind_auxiliary_receipt(
                extensions,
                aux_id=reserved["aux_id"],
                receipt=_official_receipt("codex", "CHILD-1", "jsonl_thread_started"),
            )

    # -------------------------------------------------------------- retain
    def test_retain_is_copied_from_primary_and_immutable(self) -> None:
        # Primary snapshot retain=true -> reservation must use the same value.
        raw = self.service.store.read_task(self.task.id)
        raw["extensions"][SESSION_EXTENSION_KEY]["retain"] = True
        self.service.store.write_task(self.task.id, raw)
        extensions = dict(raw["extensions"])

        extensions, entry = reserve_auxiliary_session(
            extensions,
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
            retain=True,
            created_at=T0,
        )
        self.assertTrue(entry["retain"])
        # Requesting retain=false against a retain=true primary fails closed.
        with self.assertRaises(ABCError) as ctx:
            reserve_auxiliary_session(
                extensions,
                owner_task_id=self.task.id,
                owner_run_id="run-2",
                parent_executor="codex",
                parent_session_id="PARENT",
                executor="hermes",
                purpose="child_worker",
                retain=False,
                created_at=T0,
            )
        self.assertEqual(ctx.exception.code, "auxiliary_reservation_conflict")

    # -------------------------------------------------------------- ledger
    def test_invalid_ledger_fails_closed(self) -> None:
        for ledger in (
            "nope",
            {"version": 99, "sessions": []},
            {"version": 1, "sessions": [{"not": "an entry"}]},
            {"version": 1, "sessions": [None]},
        ):
            with self.subTest(ledger=ledger):
                self.assertTrue(validate_auxiliary_ledger(ledger))
                with self.assertRaises(ABCError):
                    read_auxiliary_ledger({AUXILIARY_EXTENSION_KEY: ledger})

    def test_public_redaction_never_leaks_ids_paths_or_sources(self) -> None:
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        extensions, reserved = reserve_auxiliary_session(
            extensions,
            owner_task_id=self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="SECRET-PARENT",
            executor="claude",
            purpose="child_worker",
            retain=False,
            project_mode="ephemeral",
            project_path="/private/customer/project",
            created_at=T0,
        )
        extensions, bound = bind_auxiliary_receipt(
            extensions,
            aux_id=reserved["aux_id"],
            receipt=_official_receipt("claude", "SECRET-CHILD", "preallocated"),
        )
        view = auxiliary_ledger_view(extensions[AUXILIARY_EXTENSION_KEY])
        serialized = json.dumps(view)
        for sensitive in (
            "SECRET-CHILD",
            "SECRET-PARENT",
            "preallocated",
            "/private/customer/project",
            "project_path",
        ):
            self.assertNotIn(sensitive, serialized)
        self.assertTrue(view[0]["ref"].startswith("sess_"))
        self.assertEqual(view[0]["executor"], "claude")
        self.assertEqual(view[0]["purpose"], "child_worker")
        self.assertFalse(view[0]["retain"])

    def test_redact_ref_is_stable(self) -> None:
        self.assertEqual(
            redact_session_ref("abc-session"),
            redact_session_ref("abc-session"),
        )
        self.assertNotEqual(
            redact_session_ref("abc-session"),
            redact_session_ref("def-session"),
        )

    def test_dispatcher_conversation_never_enters_ledger(self) -> None:
        # The dispatcher conversation ID is only a provenance field; reserving
        # always requires an explicit parent session created by the task itself.
        extensions = {AUXILIARY_EXTENSION_KEY: build_auxiliary_ledger()}
        with self.assertRaises(ABCError):
            reserve_auxiliary_session(
                extensions,
                owner_task_id=self.task.id,
                owner_run_id="run-1",
                parent_executor="codex",
                parent_session_id="",
                executor="hermes",
                purpose="child_worker",
                retain=False,
                created_at=T0,
            )
        ledger = read_auxiliary_ledger(extensions)
        self.assertEqual(ledger["sessions"], [])

    def test_task_service_ledger_api_roundtrip(self) -> None:
        entry = self.service.reserve_auxiliary_session(
            self.task.id,
            owner_run_id="run-1",
            parent_executor="codex",
            parent_session_id="PARENT",
            executor="hermes",
            purpose="child_worker",
        )
        bound = self.service.bind_auxiliary_session(
            self.task.id,
            aux_id=entry["aux_id"],
            receipt=_official_receipt("hermes", "CHILD-1", "stderr_receipt"),
        )
        self.assertEqual(bound["session_id"], "CHILD-1")
        terminal = self.service.mark_auxiliary_session_terminal(
            self.task.id,
            aux_id=entry["aux_id"],
        )
        self.assertEqual(terminal["session_state"], "terminal")
        view = self.service.auxiliary_session_ledger(self.task.id)
        self.assertEqual(len(view["sessions"]), 1)
        self.assertNotIn("CHILD-1", json.dumps(view))


def _terminal_task(
    service: TaskService,
    *,
    executor: str = "codex",
    session_id: str = "PRIMARY-SESS",
    retain: bool = False,
    status: str = "completed",
) -> str:
    task = service.create_task(
        "cleanup coordinator",
        executor,
        [{"id": 1, "description": "run"}],
        customer_dir=False,
    )
    raw = service.store.read_task(task.id)
    raw["status"] = status
    raw["updated_at"] = T0
    session = raw["extensions"][SESSION_EXTENSION_KEY]
    session["session_state"] = "terminal"
    session["session_id"] = session_id
    session["retain"] = retain
    if executor == "claude":
        session["project_mode"] = "native" if retain else "ephemeral"
        session["project_path"] = str(raw["workspace"].get("project_root") or service.board_root)
    raw["extensions"][SESSION_EXTENSION_KEY] = session
    raw["extensions"]["agentbc.final_callback"] = {
        "version": 1,
        "task_id": task.id,
        "final_state": "completed",
        "summary": "done",
    }
    report_file = Path(str(raw["workspace"]["report_file"]))
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# terminal report\n", encoding="utf-8")
    service.store.write_task(task.id, raw)
    service.store.append_event(
        task.id,
        {
            "event_type": "notification_delivery",
            "task_id": task.id,
            "notification_event": "task.completed",
            "created_at": T0,
        },
    )
    lease = create_lease(task.id, executor, os.getpid(), str(service.board_root))
    lease.state = "closed"
    save_lease(lease, service.board_root)
    return task.id


def _add_auxiliary(
    service: TaskService,
    task_id: str,
    entries: list[dict],
) -> None:
    raw = service.store.read_task(task_id)
    extensions = dict(raw["extensions"])
    extensions[AUXILIARY_EXTENSION_KEY] = build_auxiliary_ledger()
    for spec in entries:
        extensions, reserved = reserve_auxiliary_session(
            extensions,
            owner_task_id=task_id,
            owner_run_id=spec.get("owner_run_id", "run-1"),
            parent_executor=spec["parent_executor"],
            parent_session_id=spec["parent_session_id"],
            executor=spec["executor"],
            purpose=spec.get("purpose", "child_worker"),
            retain=spec.get("retain", False),
            project_mode=spec.get("project_mode", "none"),
            project_path=spec.get("project_path", ""),
            created_at=spec.get("created_at", T0),
        )
        if spec.get("session_id"):
            default_source = {
                "codex": "jsonl_thread_started",
                "claude": "preallocated",
                "hermes": "stderr_receipt",
            }.get(spec["executor"], "stderr_receipt")
            extensions, bound = bind_auxiliary_receipt(
                extensions,
                aux_id=reserved["aux_id"],
                receipt=_official_receipt(
                    spec["executor"], spec["session_id"], spec.get("source", default_source)
                ),
            )
            if spec.get("terminal", True):
                extensions, _ = mark_auxiliary_terminal(extensions, aux_id=bound["aux_id"])
    raw["extensions"] = extensions
    service.store.write_task(task_id, raw)


class CoordinatorAuxiliaryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    def _coordinator(self, executor):
        from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator

        return SessionCleanupCoordinator(self.board, executor_port=executor)

    def test_auxiliary_cleanup_primary_first_then_deepest_newest(self) -> None:
        task_id = _terminal_task(self.service)
        # child_a (depth 1) and child_b (depth 2, parented on child_a).  child_b
        # must be processed first (deepest), then child_a (newest within depth).
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                },
                {
                    "executor": "claude",
                    "parent_executor": "hermes",
                    "parent_session_id": "CHILD-A",
                    "session_id": "CHILD-B",
                    "project_mode": "ephemeral",
                    "project_path": "/tmp/project",
                },
            ],
        )
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
            )
        )
        result = self._coordinator(executor).request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["aggregate"]["state"], "resolved")
        self.assertEqual(result["aggregate"]["total"], 2)
        # Primary first, then auxiliary deepest/newest: child_b before child_a.
        self.assertEqual(
            [item["executor"] for item in result["auxiliary"]],
            ["claude", "hermes"],
        )
        self.assertEqual(len(executor.calls), 3)
        self.assertEqual(executor.calls[0].session_id, "PRIMARY-SESS")
        self.assertEqual(executor.calls[1].session_id, "CHILD-B")
        self.assertEqual(executor.calls[2].session_id, "CHILD-A")

    def test_auxiliary_attempts_continue_after_primary_failure(self) -> None:
        task_id = _terminal_task(self.service)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                }
            ],
        )
        # Primary and auxiliary both fail (retryable); the coordinator still
        # attempts the auxiliary after the primary fails.
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy="official_session_delete",
                error_code="session_delete_busy",
                retryable=True,
            )
        )
        result = self._coordinator(executor).request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["receipt"]["state"], "failed")
        self.assertEqual(result["aggregate"]["state"], "blocked")
        # 1 primary + 1 auxiliary call.
        self.assertEqual(len(executor.calls), 2)

    def test_primary_success_never_implies_aggregate_success(self) -> None:
        task_id = _terminal_task(self.service)
        # One bound+terminal auxiliary that succeeds, one pending reservation
        # (retain=false, no receipt) that must stay a blocker.
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-GOOD",
                },
                {
                    "executor": "codex",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "",
                    "terminal": False,
                },
            ],
        )
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
            )
        )
        result = self._coordinator(executor).request_cleanup(task_id, now=T0)
        self.assertEqual(result["receipt"]["state"], "succeeded")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["aggregate"]["state"], "blocked")
        self.assertEqual(result["aggregate"]["unresolved"], 1)
        pending = next(item for item in result["auxiliary"] if item["executor"] == "codex")
        self.assertEqual(pending["status"], "skipped")
        self.assertIn("auxiliary_session_pending_reservation", pending["blockers"])

    def test_retain_true_is_immutable_and_never_executes(self) -> None:
        task_id = _terminal_task(self.service, retain=True)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                    "retain": True,
                }
            ],
        )
        executor = FakeCleanupExecutor()
        result = self._coordinator(executor).request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "retained")
        aux = result["auxiliary"][0]
        self.assertEqual(aux["status"], "retained")
        self.assertEqual(aux["receipt"]["capability"], "not_applicable")
        self.assertEqual(aux["receipt"]["strategy"], "retain")
        # No executor call for the retained auxiliary.
        self.assertEqual([call.session_id for call in executor.calls], [])

    def test_stale_pending_auxiliary_is_recovered_and_backs_off(self) -> None:
        task_id = _terminal_task(self.service)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                }
            ],
        )
        # Force a crashed pending receipt on the auxiliary entry.
        raw = self.service.store.read_task(task_id)
        ledger = read_auxiliary_ledger(raw["extensions"])
        ledger["sessions"][0]["cleanup"] = {
            "version": 1,
            "capability": "supported",
            "strategy": "official_session_delete",
            "state": "pending",
            "attempts": 1,
            "requested_at": T0,
            "last_attempt_at": T0,
            "next_attempt_at": "",
            "completed_at": "",
            "error_code": "",
            "retryable": False,
        }
        raw["extensions"][AUXILIARY_EXTENSION_KEY] = ledger
        self.service.store.write_task(task_id, raw)

        executor = FakeCleanupExecutor()
        result = self._coordinator(executor).request_cleanup(task_id, now=T0)
        self.assertEqual(result["auxiliary"][0]["status"], "recovered")
        self.assertEqual(result["auxiliary"][0]["receipt"]["state"], "failed")
        self.assertEqual(
            result["auxiliary"][0]["receipt"]["error_code"],
            "session_cleanup_interrupted",
        )
        self.assertTrue(result["auxiliary"][0]["receipt"]["retryable"])
        self.assertEqual(
            result["auxiliary"][0]["receipt"]["next_attempt_at"],
            _add_seconds(T0, 60),
        )
        # The crashed pending must not trigger a duplicate purge in the same pass.
        self.assertEqual([call.session_id for call in executor.calls], ["PRIMARY-SESS"])

    def test_repeated_auxiliary_cleanup_is_idempotent(self) -> None:
        task_id = _terminal_task(self.service)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                }
            ],
        )
        executor = FakeCleanupExecutor(
            SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
            )
        )
        coordinator = self._coordinator(executor)
        first = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(first["aggregate"]["state"], "resolved")
        calls_after_first = len(executor.calls)
        # Exactly one call per session (primary + one auxiliary).
        self.assertEqual(calls_after_first, 2)
        second = coordinator.request_cleanup(task_id, now=T0)
        self.assertEqual(second["status"], "resolved")
        # The replay pass is idempotent: no additional Executor calls.
        self.assertEqual(len(executor.calls), calls_after_first)

    def test_invalid_ledger_blocks_cleanup(self) -> None:
        task_id = _terminal_task(self.service)
        raw = self.service.store.read_task(task_id)
        raw["extensions"][AUXILIARY_EXTENSION_KEY] = {"version": 99, "sessions": []}
        self.service.store.write_task(task_id, raw)
        executor = FakeCleanupExecutor()
        result = self._coordinator(executor).request_cleanup(task_id, now=T0)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("auxiliary_ledger_invalid", result["auxiliary"][0]["blockers"])

    def test_auxiliary_adapter_exception_is_retryable_failure_not_crash(self) -> None:
        task_id = _terminal_task(self.service)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                }
            ],
        )

        class _BoomExecutor:
            def cleanup_session(self, request):
                raise RuntimeError("transport loss during delete")

        result = self._coordinator(_BoomExecutor()).request_cleanup(task_id, now=T0)
        # A transport loss during the auxiliary delete must produce a stable
        # retryable failed receipt and a blocked aggregate, never a crash.
        self.assertEqual(result["status"], "blocked")
        aux = result["auxiliary"][0]
        self.assertEqual(aux["status"], "failed")
        self.assertEqual(aux["receipt"]["state"], "failed")
        self.assertEqual(aux["receipt"]["error_code"], "session_cleanup_failed")
        self.assertTrue(aux["receipt"]["retryable"])
        self.assertEqual(result["aggregate"]["state"], "blocked")


class AuxiliaryDoctorReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )

    def test_doctor_warns_for_pending_reservation_and_cleanup_failure(self) -> None:
        from agent_bridge_connect.doctor import build_session_cleanup_diagnostics

        task_id = _terminal_task(self.service)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "codex",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "",
                    "terminal": False,
                },
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-FAIL",
                },
            ],
        )
        raw = self.service.store.read_task(task_id)
        ledger = read_auxiliary_ledger(raw["extensions"])
        for entry in ledger["sessions"]:
            if entry["executor"] == "hermes":
                entry["cleanup"] = {
                    "version": 1,
                    "capability": "supported",
                    "strategy": "official_session_delete",
                    "state": "failed",
                    "attempts": 1,
                    "requested_at": T0,
                    "last_attempt_at": T0,
                    "next_attempt_at": _add_seconds(T0, 60),
                    "completed_at": "",
                    "error_code": "session_delete_busy",
                    "retryable": True,
                }
        raw["extensions"][AUXILIARY_EXTENSION_KEY] = ledger
        self.service.store.write_task(task_id, raw)

        diagnostics = build_session_cleanup_diagnostics(
            [self.service.store.read_task(task_id)],
            now=_add_seconds(T0, 60),
        )
        self.assertGreaterEqual(diagnostics["warnings"], 2)
        messages = " ".join(item["message"] for item in diagnostics["diagnostics"])
        self.assertIn("no official session receipt", messages)
        self.assertIn("bounded retry path", messages)

    def test_doctor_flags_invalid_ledger(self) -> None:
        from agent_bridge_connect.doctor import build_session_cleanup_diagnostics

        task_id = _terminal_task(self.service)
        raw = self.service.store.read_task(task_id)
        raw["extensions"][AUXILIARY_EXTENSION_KEY] = {"version": 99, "sessions": []}
        self.service.store.write_task(task_id, raw)
        diagnostics = build_session_cleanup_diagnostics(
            [self.service.store.read_task(task_id)],
            now=T0,
        )
        self.assertGreaterEqual(diagnostics["warnings"], 1)
        self.assertIn(
            "ledger is invalid",
            " ".join(item["message"] for item in diagnostics["diagnostics"]),
        )

    def test_doctor_ignores_auxiliary_while_task_is_active(self) -> None:
        from agent_bridge_connect.doctor import build_session_cleanup_diagnostics

        # Task still running: the auxiliary session is in use and must not be
        # flagged as an acceptance failure.
        task_id = _terminal_task(self.service, status="running")
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "PRIMARY-SESS",
                    "session_id": "CHILD-A",
                }
            ],
        )
        diagnostics = build_session_cleanup_diagnostics(
            [self.service.store.read_task(task_id)],
            now=T0,
        )
        self.assertEqual(diagnostics["warnings"], 0)

    def test_report_and_public_view_render_only_redacted_auxiliary_fields(self) -> None:
        from agent_bridge_connect.reports import generate_report_md

        task_id = _terminal_task(self.service)
        _add_auxiliary(
            self.service,
            task_id,
            [
                {
                    "executor": "hermes",
                    "parent_executor": "codex",
                    "parent_session_id": "AUX-PARENT-SECRET",
                    "session_id": "SECRET-AUX-SESSION",
                }
            ],
        )
        rendered = generate_report_md(task_id, self.board)
        self.assertIn("Auxiliary Executor Sessions", rendered)
        self.assertIn("sess_", rendered)
        for sensitive in (
            "SECRET-AUX-SESSION",
            "AUX-PARENT-SECRET",
            "stderr_receipt",
        ):
            self.assertNotIn(sensitive, rendered)


if __name__ == "__main__":
    unittest.main()
