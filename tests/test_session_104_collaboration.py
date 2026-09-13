"""SESSION-104-001 strict Codex cleanup and collaboration-spawn contracts."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.auxiliary_sessions import (
    AUXILIARY_EXTENSION_KEY,
    CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
    CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
    handle_codex_collaboration_item_completed,
    handle_codex_collaboration_item_started,
    read_auxiliary_ledger,
    reconcile_codex_descendants,
)
from agent_bridge_connect.codex_app_server import (
    assert_codex_collaboration_spawn_capability,
    codex_collaboration_spawn_contract,
    codex_collaboration_spawn_fixture_contract,
)
from agent_bridge_connect.execution_policy import (
    SESSION_EXTENSION_KEY,
    SESSION_RECEIPT_SOURCES,
    build_session_snapshot,
)
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService

PARENT_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
CHILD_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d7"
T0 = "2026-08-28T00:00:00Z"
FIXTURES = Path(__file__).parent / "fixtures" / "executor_runtime" / "matrix" / "codex"


def _parent_extensions(*, official: bool = True) -> dict:
    session = build_session_snapshot(
        "codex",
        retain=False,
        session_id=PARENT_SESSION_ID,
        session_state="active",
        run_ids=["run-104"],
        created_at=T0,
    )
    session["official_receipt_bound"] = official
    session["receipt_source"] = SESSION_RECEIPT_SOURCES["codex"] if official else ""
    return {SESSION_EXTENSION_KEY: session}


def _item(*, item_id: str = "item-104", receiver: str = "") -> dict:
    item = {
        "id": item_id,
        "type": "collabAgentToolCall",
        "tool": "spawnAgent",
    }
    if receiver:
        item["receiverThreadId"] = receiver
    return item


class CollaborationCapabilityTests(unittest.TestCase):
    def test_promoted_fixture_is_packaged_for_production(self) -> None:
        result = codex_collaboration_spawn_fixture_contract("0.150.1")
        self.assertTrue(result["ok"])
        self.assertIn("protocol_fixtures", result["schema_path"])

    def test_task_contract_explicitly_freezes_collaboration_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = TaskService(
                root / "record",
                config={"workspace_root": str(root / "workspace")},
            )
            task = service.create_task(
                "Codex collaboration contract",
                "codex",
                [{"id": 1, "description": "spawn exactly once"}],
                customer_dir=False,
                collaboration_spawn=True,
            )
            self.assertEqual(
                task.extensions["agentbc.codex.collaboration_spawn"],
                {"version": 1, "enabled": True},
            )
            self.assertTrue(CodexExecutor._collaboration_spawn_requested(task.to_dict()))

    def test_0147_fixture_is_explicitly_unsupported(self) -> None:
        result = codex_collaboration_spawn_fixture_contract("0.147.0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["version"], "0.147.0")
        self.assertEqual(
            set(result["missing"]),
            {"collabAgentToolCall", "spawnAgent", "receiverThreadId"},
        )

    def test_promoted_fixture_requires_matching_live_proof(self) -> None:
        candidate = codex_collaboration_spawn_fixture_contract("0.150.1")
        self.assertTrue(candidate["ok"])
        schema = json.loads(
            (FIXTURES / "0.147.0" / "app_server_schema.json").read_text(
                encoding="utf-8"
            )
        )
        live = codex_collaboration_spawn_contract(
            "/tmp/fake-codex",
            version_output="codex-cli 0.147.0",
            schema_bundle=schema,
        )
        self.assertFalse(live["ok"])
        self.assertIn("collabAgentToolCall", live["reason"])

    def test_executor_accepts_live_protocol_without_matching_fixture(self) -> None:
        live = {
            "ok": True,
            "version": "codex-cli 0.147.0",
            "version_parsed": [0, 147, 0],
        }
        fixture = {"ok": False, "reason": "collaboration fixture is missing"}
        executor = CodexExecutor(command=sys.executable, transport="app-server")
        with (
            mock.patch(
                "agent_bridge_connect.codex_app_server.codex_collaboration_spawn_contract",
                return_value=live,
            ),
            mock.patch(
                "agent_bridge_connect.codex_app_server.codex_collaboration_spawn_fixture_contract",
                return_value=fixture,
            ),
        ):
            result = executor.collaboration_spawn_capability()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["version"], "0.147.0")
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["verification_source"], "live_schema")
        self.assertFalse(result["fixture"]["ok"])

    def test_assertion_accepts_newer_live_protocol_without_fixture(self) -> None:
        live = {
            "ok": True,
            "version": "codex-cli 0.153.4",
            "version_parsed": [0, 153, 4],
        }
        fixture = {"ok": False, "reason": "collaboration fixture is unavailable"}
        with (
            mock.patch(
                "agent_bridge_connect.codex_app_server.codex_collaboration_spawn_contract",
                return_value=live,
            ),
            mock.patch(
                "agent_bridge_connect.codex_app_server.codex_collaboration_spawn_fixture_contract",
                return_value=fixture,
            ),
        ):
            result = assert_codex_collaboration_spawn_capability(
                "/tmp/fake-codex", transport="app-server"
            )

        self.assertTrue(result["enabled"])
        self.assertEqual(result["verification_source"], "live_schema")
        self.assertFalse(result["fixture"]["ok"])

    def test_unsupported_requested_spawn_fails_before_transport_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = {
                "task_id": "SESSION-104-001",
                "steps": [{"id": 1, "description": "gate"}],
                "workspace": {"root": str(root), "project_root": str(root)},
                "collaboration_spawn": True,
                "extensions": {
                    "agentbc.permission": build_permission_record(explicit_mode="safe"),
                    SESSION_EXTENSION_KEY: build_session_snapshot(
                        "codex",
                        retain=False,
                        session_state="pending",
                        created_at=T0,
                    ),
                },
            }
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            executor._app_server_capability_override = {
                "ok": True,
                "transport": "app-server",
                "version": "codex-cli 0.147.0",
                "version_parsed": (0, 147, 0),
                "schema_missing": [],
                "evidence": ["fixture"],
            }
            executor._collaboration_spawn_capability = {
                "enabled": False,
                "reason": "Codex 0.147.0 collaboration_spawn is unsupported",
                "fixture": {"ok": False},
                "live": {"ok": False},
            }
            factory = mock.Mock()
            executor.transport_factory = factory
            with (
                mock.patch.object(executor, "_start_run_lease"),
                mock.patch.object(executor, "_close_run_lease"),
            ):
                started = executor.start(packet)
            self.assertFalse(started.ok)
            self.assertIn("codex_collaboration_spawn_unsupported", started.message)
            factory.assert_not_called()
            self.assertEqual(executor._app_runs, {})


class CollaborationLedgerTests(unittest.TestCase):
    def test_started_reserves_and_completed_binds_official_receiver(self) -> None:
        extensions = _parent_extensions()
        extensions, reserved = handle_codex_collaboration_item_started(
            extensions,
            owner_task_id="SESSION-104-001",
            owner_run_id="run-104",
            parent_session_id=PARENT_SESSION_ID,
            parent_turn_id="turn-104",
            item=_item(),
            occurred_at=T0,
        )
        self.assertEqual(reserved["session_state"], "reserved")
        self.assertEqual(reserved["purpose"], "collaboration_spawn")
        self.assertEqual(reserved["collaboration_item_id"], "item-104")
        self.assertEqual(reserved["session_id"], "")

        extensions, terminal = handle_codex_collaboration_item_completed(
            extensions,
            owner_task_id="SESSION-104-001",
            owner_run_id="run-104",
            parent_session_id=PARENT_SESSION_ID,
            item=_item(receiver=CHILD_SESSION_ID),
            occurred_at=T0,
        )
        self.assertEqual(terminal["session_id"], CHILD_SESSION_ID)
        self.assertEqual(terminal["source"], SESSION_RECEIPT_SOURCES["codex"])
        self.assertEqual(terminal["session_state"], "terminal")
        self.assertEqual(validate_ledger(extensions), [])

    def test_missing_reservation_and_receiver_mismatch_fail_closed(self) -> None:
        with self.assertRaises(ABCError) as missing:
            handle_codex_collaboration_item_completed(
                _parent_extensions(),
                owner_task_id="SESSION-104-001",
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                item=_item(receiver=CHILD_SESSION_ID),
            )
        self.assertEqual(missing.exception.code, CODEX_AUXILIARY_RECEIPT_MISSING_CODE)

        extensions, _ = handle_codex_collaboration_item_started(
            _parent_extensions(),
            owner_task_id="SESSION-104-001",
            owner_run_id="run-104",
            parent_session_id=PARENT_SESSION_ID,
            item=_item(),
            occurred_at=T0,
        )
        extensions, _ = handle_codex_collaboration_item_completed(
            extensions,
            owner_task_id="SESSION-104-001",
            owner_run_id="run-104",
            parent_session_id=PARENT_SESSION_ID,
            item=_item(receiver=CHILD_SESSION_ID),
            occurred_at=T0,
        )
        with self.assertRaises(ABCError) as mismatch:
            handle_codex_collaboration_item_completed(
                extensions,
                owner_task_id="SESSION-104-001",
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                item=_item(receiver="019fef10-2f46-7c40-90c8-6d6ebd3cc7d8"),
            )
        # A second, different receiver must be an unregistered descendant, not
        # a rebind.
        self.assertEqual(mismatch.exception.code, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE)

    def test_unknown_descendant_is_reconciliation_only_and_never_deleted(self) -> None:
        extensions, _ = handle_codex_collaboration_item_started(
            _parent_extensions(),
            owner_task_id="SESSION-104-001",
            owner_run_id="run-104",
            parent_session_id=PARENT_SESSION_ID,
            item=_item(),
            occurred_at=T0,
        )
        extensions, _ = handle_codex_collaboration_item_completed(
            extensions,
            owner_task_id="SESSION-104-001",
            owner_run_id="run-104",
            parent_session_id=PARENT_SESSION_ID,
            item=_item(receiver=CHILD_SESSION_ID),
            occurred_at=T0,
        )
        self.assertEqual(
            reconcile_codex_descendants(
                extensions,
                owner_task_id="SESSION-104-001",
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                threads=[
                    {"id": "dispatcher-thread", "ancestorThreadId": "other-parent"},
                    {"id": CHILD_SESSION_ID, "ancestorThreadId": PARENT_SESSION_ID},
                ],
            ),
            [CHILD_SESSION_ID],
        )

    def test_registered_child_is_archived_on_owning_transport_and_receipted(self) -> None:
        class ArchiveTransport:
            def __init__(self) -> None:
                self.sent: list[dict] = []
                self.responses: list[dict] = []

            def send(self, message: dict) -> None:
                self.sent.append(message)
                self.responses.append(
                    {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                )

            def recv(self) -> dict:
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "record"
            service = TaskService(
                board,
                config={"workspace_root": str(root / "workspace")},
            )
            task = service.create_task(
                "Codex child archive lifecycle",
                "codex",
                [{"id": 1, "description": "exercise child archive"}],
                customer_dir=False,
                collaboration_spawn=True,
            )
            raw = service.store.read_task(task.id)
            raw["extensions"][SESSION_EXTENSION_KEY] = _parent_extensions()[
                SESSION_EXTENSION_KEY
            ]
            extensions, _ = handle_codex_collaboration_item_started(
                raw["extensions"],
                owner_task_id=task.id,
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                parent_turn_id="turn-104",
                item=_item(),
                occurred_at=T0,
            )
            extensions, _ = handle_codex_collaboration_item_completed(
                extensions,
                owner_task_id=task.id,
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                item=_item(receiver=CHILD_SESSION_ID),
                occurred_at=T0,
            )
            raw["extensions"] = extensions
            service.store.write_task(task.id, raw)
            transport = ArchiveTransport()
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            record = {
                "task_packet": {
                    "task_id": task.id,
                    "task_board": {"root": str(board)},
                    "extensions": copy.deepcopy(extensions),
                },
                "run_id": "run-104",
                "session_id": PARENT_SESSION_ID,
                "transport": transport,
                "next_rpc_id": 1,
                "events": [],
            }
            executor._archive_registered_auxiliary_sessions(record)

            self.assertEqual(
                [message["method"] for message in transport.sent],
                ["thread/archive"],
            )
            self.assertEqual(
                transport.sent[0]["params"],
                {"threadId": CHILD_SESSION_ID},
            )
            persisted = service.store.read_task(task.id)
            entry = read_auxiliary_ledger(persisted["extensions"])["sessions"][0]
            self.assertTrue(entry["archive_acknowledged"])
            self.assertTrue(entry["archive_checked_at"])
        with self.assertRaises(ABCError) as unknown:
            reconcile_codex_descendants(
                extensions,
                owner_task_id="SESSION-104-001",
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                threads=[
                    {"id": "unknown-child", "ancestorThreadId": PARENT_SESSION_ID}
                ],
            )
        self.assertEqual(unknown.exception.code, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE)

    def test_parent_receipt_is_required_before_reservation(self) -> None:
        with self.assertRaises(ABCError) as raised:
            handle_codex_collaboration_item_started(
                _parent_extensions(official=False),
                owner_task_id="SESSION-104-001",
                owner_run_id="run-104",
                parent_session_id=PARENT_SESSION_ID,
                item=_item(),
            )
        self.assertEqual(raised.exception.code, CODEX_AUXILIARY_RECEIPT_MISSING_CODE)

    def test_current_parent_run_is_required_before_reservation(self) -> None:
        with self.assertRaises(ABCError) as raised:
            handle_codex_collaboration_item_started(
                _parent_extensions(),
                owner_task_id="SESSION-104-001",
                owner_run_id="run-not-current",
                parent_session_id=PARENT_SESSION_ID,
                item=_item(),
            )
        self.assertEqual(raised.exception.code, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE)


class CollaborationEventPersistenceTests(unittest.TestCase):
    def test_non_spawn_collaboration_items_do_not_abort_or_create_receipts(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="app-server")
        record = {
            "collaboration_spawn": {"enabled": True},
            "task_packet": {"extensions": {}},
        }
        for tool in ("listAgents", "wait", "sendInput"):
            executor._handle_collaboration_event(
                record,
                "item/completed",
                {
                    "item": {
                        "id": f"item-{tool}",
                        "type": "collabAgentToolCall",
                        "tool": tool,
                    }
                },
            )
        self.assertNotIn(AUXILIARY_EXTENSION_KEY, record["task_packet"]["extensions"])

    def test_lifecycle_events_persist_the_task_scoped_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "record"
            service = TaskService(
                board,
                config={"workspace_root": str(root / "workspace")},
            )
            task = service.create_task(
                "Codex collaboration lifecycle",
                "codex",
                [{"id": 1, "description": "exercise ledger"}],
                customer_dir=False,
            )
            raw = service.store.read_task(task.id)
            raw["extensions"][SESSION_EXTENSION_KEY] = _parent_extensions()[SESSION_EXTENSION_KEY]
            service.store.write_task(task.id, raw)
            packet = {
                "task_id": task.id,
                "task_board": {"root": str(board)},
                "extensions": copy.deepcopy(raw["extensions"]),
            }
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            record = {
                "collaboration_spawn": {"enabled": True},
                "task_packet": packet,
                "run_id": "run-104",
                "session_id": PARENT_SESSION_ID,
            }
            executor._handle_collaboration_event(
                record,
                "item/started",
                {"turnId": "turn-104", "item": _item()},
            )
            persisted = service.store.read_task(task.id)
            reserved = read_auxiliary_ledger(persisted["extensions"])["sessions"]
            self.assertEqual(len(reserved), 1)
            self.assertEqual(reserved[0]["session_state"], "reserved")
            executor._handle_collaboration_event(
                record,
                "item/completed",
                {"turnId": "turn-104", "item": _item(receiver=CHILD_SESSION_ID)},
            )
            persisted = service.store.read_task(task.id)
            final_entry = read_auxiliary_ledger(persisted["extensions"])["sessions"][0]
            self.assertEqual(final_entry["session_id"], CHILD_SESSION_ID)
            self.assertEqual(final_entry["session_state"], "terminal")
            self.assertTrue((service.store.task_dir(task.id) / "events.jsonl").is_file())


def validate_ledger(extensions: dict) -> list[str]:
    from agent_bridge_connect.auxiliary_sessions import validate_auxiliary_ledger

    return validate_auxiliary_ledger(extensions[AUXILIARY_EXTENSION_KEY])


if __name__ == "__main__":
    unittest.main()
