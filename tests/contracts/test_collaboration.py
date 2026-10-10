"""SESSION-104-001 strict Codex cleanup and collaboration-spawn contracts."""

from __future__ import annotations
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.auxiliary_sessions import AUXILIARY_EXTENSION_KEY, CODEX_AUXILIARY_RECEIPT_MISSING_CODE, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE, handle_codex_collaboration_item_completed, handle_codex_collaboration_item_started, read_auxiliary_ledger, reconcile_codex_descendants
from agent_bridge_connect.codex_app_server import assert_codex_collaboration_spawn_capability, codex_collaboration_spawn_contract, codex_collaboration_spawn_fixture_contract
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY, SESSION_RECEIPT_SOURCES, build_session_snapshot
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService
import contextlib as _conversation_trace_contextlib
import io as _conversation_trace_io
import os as _conversation_trace_os
import stat as _conversation_trace_stat
import tempfile as _conversation_trace_tempfile
import unittest as _conversation_trace_unittest
from pathlib import Path as _conversation_trace_Path
from unittest import mock as _conversation_trace_mock

PARENT_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
CHILD_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d7"
T0 = "2026-08-28T00:00:00Z"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "executor_runtime" / "matrix" / "codex"


def _parent_extensions(*, official: bool = True) -> dict:
    session = build_session_snapshot("codex", retain=False, session_id=PARENT_SESSION_ID, session_state="active", run_ids=["run-104"], created_at=T0)
    session["official_receipt_bound"] = official
    session["receipt_source"] = SESSION_RECEIPT_SOURCES["codex"] if official else ""
    return {SESSION_EXTENSION_KEY: session}


def _item(*, item_id: str = "item-104", receiver: str = "") -> dict:
    item = {"id": item_id, "type": "collabAgentToolCall", "tool": "spawnAgent"}
    if receiver:
        item["receiverThreadId"] = receiver
    return item


class CollaborationCapabilityTests(unittest.TestCase):
    def test_subagent_activity_binds_real_uuid_and_distinct_completion_item(self):
        from agent_bridge_connect.auxiliary_sessions import handle_codex_subagent_activity
        args = dict(owner_task_id="TASK-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID)
        start = {"type": "subAgentActivity", "kind": "started", "id": "call-start",
                 "agentThreadId": CHILD_SESSION_ID, "agentPath": "/root/child"}
        end = {**start, "kind": "completed", "id": "completion-other-id"}
        with self.assertRaises(ABCError):
            handle_codex_subagent_activity(_parent_extensions(), item=end, **args)
        for invalid in ("/root/child", PARENT_SESSION_ID):
            with self.assertRaises(ABCError):
                handle_codex_subagent_activity(_parent_extensions(), item={**start, "agentThreadId": invalid}, **args)
        ext, entry = handle_codex_subagent_activity(_parent_extensions(), item=start, **args)
        self.assertEqual(entry["session_state"], "active")
        self.assertEqual(entry["session_id"], CHILD_SESSION_ID)
        with self.assertRaises(ABCError):
            handle_codex_subagent_activity(ext, item=end, **{**args, "owner_run_id": "stale-run"})
        ext, entry = handle_codex_subagent_activity(ext, item=end, **args)
        self.assertEqual(entry["session_state"], "terminal")
        same, _ = handle_codex_subagent_activity(ext, item=end, **args)
        self.assertEqual(same, ext)

    def test_promoted_fixture_is_packaged_for_production(self) -> None:
        result = codex_collaboration_spawn_fixture_contract("0.150.1")
        self.assertTrue(result["ok"])
        self.assertIn("protocol_fixtures", result["schema_path"])

    def test_task_contract_explicitly_freezes_collaboration_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = TaskService(root / "record", config={"workspace_root": str(root / "workspace")})
            task = service.create_task("Codex collaboration contract", "codex", [{"id": 1, "description": "spawn exactly once"}], customer_dir=False, collaboration_spawn=True)
            self.assertEqual(task.extensions["agentbc.codex.collaboration_spawn"], {"version": 1, "enabled": True})
            self.assertTrue(CodexExecutor._collaboration_spawn_requested(task.to_dict()))

    def test_0147_fixture_is_explicitly_unsupported(self) -> None:
        result = codex_collaboration_spawn_fixture_contract("0.147.0", fixture_root=FIXTURES)
        self.assertFalse(result["ok"])
        self.assertEqual(result["version"], "0.147.0")
        self.assertEqual(set(result["missing"]), {"collabAgentToolCall", "spawnAgent", "receiverThreadId"})

    def test_promoted_fixture_requires_matching_live_proof(self) -> None:
        candidate = codex_collaboration_spawn_fixture_contract("0.150.1")
        self.assertTrue(candidate["ok"])
        schema = json.loads((FIXTURES / "0.147.0" / "app_server_schema.json").read_text(encoding="utf-8"))
        live = codex_collaboration_spawn_contract("/tmp/fake-codex", version_output="codex-cli 0.147.0", schema_bundle=schema)
        self.assertFalse(live["ok"])
        self.assertIn("collabAgentToolCall", live["reason"])

    def test_executor_accepts_live_protocol_without_matching_fixture(self) -> None:
        live = {"ok": True, "version": "codex-cli 0.147.0", "version_parsed": [0, 147, 0]}
        fixture = {"ok": False, "reason": "collaboration fixture is missing"}
        executor = CodexExecutor(command=sys.executable, transport="app-server")
        with mock.patch("agent_bridge_connect.codex_app_server.codex_collaboration_spawn_contract", return_value=live), mock.patch("agent_bridge_connect.codex_app_server.codex_collaboration_spawn_fixture_contract", return_value=fixture):
            result = executor.collaboration_spawn_capability()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["version"], "0.147.0")
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["verification_source"], "live_schema")
        self.assertFalse(result["fixture"]["ok"])

    def test_assertion_accepts_newer_live_protocol_without_fixture(self) -> None:
        live = {"ok": True, "version": "codex-cli 0.153.4", "version_parsed": [0, 153, 4]}
        fixture = {"ok": False, "reason": "collaboration fixture is unavailable"}
        with mock.patch("agent_bridge_connect.codex_app_server.codex_collaboration_spawn_contract", return_value=live), mock.patch("agent_bridge_connect.codex_app_server.codex_collaboration_spawn_fixture_contract", return_value=fixture):
            result = assert_codex_collaboration_spawn_capability("/tmp/fake-codex", transport="app-server")
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
                "extensions": {"agentbc.permission": build_permission_record(explicit_mode="safe"), SESSION_EXTENSION_KEY: build_session_snapshot("codex", retain=False, session_state="pending", created_at=T0)},
            }
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            executor._app_server_capability_override = {"ok": True, "transport": "app-server", "version": "codex-cli 0.147.0", "version_parsed": (0, 147, 0), "schema_missing": [], "evidence": ["fixture"]}
            executor._collaboration_spawn_capability = {"enabled": False, "reason": "Codex 0.147.0 collaboration_spawn is unsupported", "fixture": {"ok": False}, "live": {"ok": False}}
            factory = mock.Mock()
            executor.transport_factory = factory
            with mock.patch.object(executor, "_start_run_lease"), mock.patch.object(executor, "_close_run_lease"):
                started = executor.start(packet)
            self.assertFalse(started.ok)
            self.assertIn("codex_collaboration_spawn_unsupported", started.message)
            factory.assert_not_called()
            self.assertEqual(executor._app_runs, {})


class CollaborationLedgerTests(unittest.TestCase):
    def test_started_reserves_and_completed_binds_official_receiver(self) -> None:
        extensions = _parent_extensions()
        (extensions, reserved) = handle_codex_collaboration_item_started(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, parent_turn_id="turn-104", item=_item(), occurred_at=T0)
        self.assertEqual(reserved["session_state"], "reserved")
        self.assertEqual(reserved["purpose"], "collaboration_spawn")
        self.assertEqual(reserved["collaboration_item_id"], "item-104")
        self.assertEqual(reserved["session_id"], "")
        (extensions, terminal) = handle_codex_collaboration_item_completed(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(receiver=CHILD_SESSION_ID), occurred_at=T0)
        self.assertEqual(terminal["session_id"], CHILD_SESSION_ID)
        self.assertEqual(terminal["source"], SESSION_RECEIPT_SOURCES["codex"])
        self.assertEqual(terminal["session_state"], "terminal")
        self.assertEqual(validate_ledger(extensions), [])

    def test_missing_reservation_and_receiver_mismatch_fail_closed(self) -> None:
        with self.assertRaises(ABCError) as missing:
            handle_codex_collaboration_item_completed(_parent_extensions(), owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(receiver=CHILD_SESSION_ID))
        self.assertEqual(missing.exception.code, CODEX_AUXILIARY_RECEIPT_MISSING_CODE)
        (extensions, _) = handle_codex_collaboration_item_started(_parent_extensions(), owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(), occurred_at=T0)
        (extensions, _) = handle_codex_collaboration_item_completed(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(receiver=CHILD_SESSION_ID), occurred_at=T0)
        with self.assertRaises(ABCError) as mismatch:
            handle_codex_collaboration_item_completed(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(receiver="019fef10-2f46-7c40-90c8-6d6ebd3cc7d8"))
        self.assertEqual(mismatch.exception.code, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE)

    def test_unknown_descendant_is_reconciliation_only_and_never_deleted(self) -> None:
        (extensions, _) = handle_codex_collaboration_item_started(_parent_extensions(), owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(), occurred_at=T0)
        (extensions, _) = handle_codex_collaboration_item_completed(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(receiver=CHILD_SESSION_ID), occurred_at=T0)
        self.assertEqual(
            reconcile_codex_descendants(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, threads=[{"id": "dispatcher-thread", "ancestorThreadId": "other-parent"}, {"id": CHILD_SESSION_ID, "ancestorThreadId": PARENT_SESSION_ID}]),
            [CHILD_SESSION_ID],
        )

    def test_registered_child_is_archived_on_owning_transport_and_receipted(self) -> None:

        class ArchiveTransport:
            def __init__(self) -> None:
                self.sent: list[dict] = []
                self.responses: list[dict] = []

            def send(self, message: dict) -> None:
                self.sent.append(message)
                self.responses.append({"jsonrpc": "2.0", "id": message["id"], "result": {}})

            def recv(self) -> dict:
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "record"
            service = TaskService(board, config={"workspace_root": str(root / "workspace")})
            task = service.create_task("Codex child archive lifecycle", "codex", [{"id": 1, "description": "exercise child archive"}], customer_dir=False, collaboration_spawn=True)
            raw = service.store.read_task(task.id)
            raw["extensions"][SESSION_EXTENSION_KEY] = _parent_extensions()[SESSION_EXTENSION_KEY]
            (extensions, _) = handle_codex_collaboration_item_started(raw["extensions"], owner_task_id=task.id, owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, parent_turn_id="turn-104", item=_item(), occurred_at=T0)
            (extensions, _) = handle_codex_collaboration_item_completed(extensions, owner_task_id=task.id, owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item(receiver=CHILD_SESSION_ID), occurred_at=T0)
            raw["extensions"] = extensions
            service.store.write_task(task.id, raw)
            transport = ArchiveTransport()
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            record = {"task_packet": {"task_id": task.id, "task_board": {"root": str(board)}, "extensions": copy.deepcopy(extensions)}, "run_id": "run-104", "session_id": PARENT_SESSION_ID, "transport": transport, "next_rpc_id": 1, "events": []}
            executor._archive_registered_auxiliary_sessions(record)
            self.assertEqual([message["method"] for message in transport.sent], ["thread/archive"])
            self.assertEqual(transport.sent[0]["params"], {"threadId": CHILD_SESSION_ID})
            persisted = service.store.read_task(task.id)
            entry = read_auxiliary_ledger(persisted["extensions"])["sessions"][0]
            self.assertTrue(entry["archive_acknowledged"])
            self.assertTrue(entry["archive_checked_at"])
        with self.assertRaises(ABCError) as unknown:
            reconcile_codex_descendants(extensions, owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, threads=[{"id": "unknown-child", "ancestorThreadId": PARENT_SESSION_ID}])
        self.assertEqual(unknown.exception.code, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE)

    def test_parent_receipt_is_required_before_reservation(self) -> None:
        with self.assertRaises(ABCError) as raised:
            handle_codex_collaboration_item_started(_parent_extensions(official=False), owner_task_id="SESSION-104-001", owner_run_id="run-104", parent_session_id=PARENT_SESSION_ID, item=_item())
        self.assertEqual(raised.exception.code, CODEX_AUXILIARY_RECEIPT_MISSING_CODE)

    def test_current_parent_run_is_required_before_reservation(self) -> None:
        with self.assertRaises(ABCError) as raised:
            handle_codex_collaboration_item_started(_parent_extensions(), owner_task_id="SESSION-104-001", owner_run_id="run-not-current", parent_session_id=PARENT_SESSION_ID, item=_item())
        self.assertEqual(raised.exception.code, CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE)


class CollaborationEventPersistenceTests(unittest.TestCase):
    def test_non_spawn_collaboration_items_do_not_abort_or_create_receipts(self) -> None:
        executor = CodexExecutor(command=sys.executable, transport="app-server")
        record = {"collaboration_spawn": {"enabled": True}, "task_packet": {"extensions": {}}}
        for tool in ("listAgents", "wait", "sendInput"):
            executor._handle_collaboration_event(record, "item/completed", {"item": {"id": f"item-{tool}", "type": "collabAgentToolCall", "tool": tool}})
        self.assertNotIn(AUXILIARY_EXTENSION_KEY, record["task_packet"]["extensions"])

    def test_official_subagent_fixture_events_reach_persisted_ledger(self):
        fixture = json.loads((FIXTURES / "0.153.4/subagent_activity_schema.json").read_text())
        self.assertIn("agentThreadId", fixture["items"][0]["required"])
        with tempfile.TemporaryDirectory() as temporary:
            service = TaskService(Path(temporary) / "record", config={"workspace_root": temporary})
            task = service.create_task("native event", "codex", [{"id": 1, "description": "one child"}], customer_dir=False)
            raw = service.store.read_task(task.id)
            raw["extensions"].update(_parent_extensions())
            service.store.write_task(task.id, raw)
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            record = {"events": [], "collaboration_spawn": {"enabled": True}, "run_id": "run-104",
                      "session_id": PARENT_SESSION_ID, "task_packet": {"task_id": task.id,
                      "task_board": {"root": str(service.board_root)}, "extensions": {}}}
            for kind in ("started", "completed"):
                item = {"type": "subAgentActivity", "id": "item-" + kind, "kind": kind,
                        "agentThreadId": CHILD_SESSION_ID, "agentPath": "/root/child"}
                executor._app_event(record, {"method": "item/completed", "params": {
                    "threadId": PARENT_SESSION_ID, "turnId": "turn-104", "item": item}})
            ledger = read_auxiliary_ledger(service.store.read_task(task.id)["extensions"])
            self.assertEqual(len(ledger["sessions"]), 1)
            self.assertEqual(ledger["sessions"][0]["session_id"], CHILD_SESSION_ID)
            self.assertEqual(ledger["sessions"][0]["session_state"], "terminal")

    def test_lifecycle_events_persist_the_task_scoped_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "record"
            service = TaskService(board, config={"workspace_root": str(root / "workspace")})
            task = service.create_task("Codex collaboration lifecycle", "codex", [{"id": 1, "description": "exercise ledger"}], customer_dir=False)
            raw = service.store.read_task(task.id)
            raw["extensions"][SESSION_EXTENSION_KEY] = _parent_extensions()[SESSION_EXTENSION_KEY]
            service.store.write_task(task.id, raw)
            packet = {"task_id": task.id, "task_board": {"root": str(board)}, "extensions": copy.deepcopy(raw["extensions"])}
            packet["extensions"][SESSION_EXTENSION_KEY]["run_ids"] = ["older-run"]
            executor = CodexExecutor(command=sys.executable, transport="app-server")
            record = {"collaboration_spawn": {"enabled": True}, "task_packet": packet, "run_id": "run-104", "session_id": PARENT_SESSION_ID}
            executor._handle_collaboration_event(record, "item/started", {"turnId": "turn-104", "item": _item()})
            persisted = service.store.read_task(task.id)
            reserved = read_auxiliary_ledger(persisted["extensions"])["sessions"]
            self.assertEqual(len(reserved), 1)
            self.assertEqual(reserved[0]["session_state"], "reserved")
            executor._handle_collaboration_event(record, "item/completed", {"turnId": "turn-104", "item": _item(receiver=CHILD_SESSION_ID)})
            persisted = service.store.read_task(task.id)
            final_entry = read_auxiliary_ledger(persisted["extensions"])["sessions"][0]
            self.assertEqual(final_entry["session_id"], CHILD_SESSION_ID)
            self.assertEqual(final_entry["session_state"], "terminal")
            self.assertTrue((service.store.task_dir(task.id) / "events.jsonl").is_file())


def validate_ledger(extensions: dict) -> list[str]:
    from agent_bridge_connect.auxiliary_sessions import validate_auxiliary_ledger

    return validate_auxiliary_ledger(extensions[AUXILIARY_EXTENSION_KEY])


class _conversation_trace_ConversationOriginTests(_conversation_trace_unittest.TestCase):
    def test_explicit_session_id_overrides_matching_environment_id(self):
        from agent_bridge_connect.cli import _origin_context

        with _conversation_trace_mock.patch.dict(_conversation_trace_os.environ, {"CODEX_THREAD_ID": "environment-thread"}, clear=True):
            context = _origin_context("explicit-thread", "codex")
        self.assertEqual(context, ("explicit-thread", "codex"))

    def test_explicit_platform_uses_only_its_matching_environment_id(self):
        from agent_bridge_connect.cli import _origin_context

        cases = {"codex": "CODEX_THREAD_ID", "claude": "CLAUDE_SESSION_ID", "hermes": "HERMES_SESSION_ID", "opencode": "OPENCODE_SESSION_ID"}
        for platform, variable in cases.items():
            with self.subTest(platform=platform), _conversation_trace_mock.patch.dict(_conversation_trace_os.environ, {variable: f"{platform}-thread", "UNRELATED_SESSION_ID": "wrong-thread"}, clear=True):
                context = _origin_context(None, platform)
            self.assertEqual(context, (f"{platform}-thread", platform))

    def test_detected_platform_isolates_mismatched_environment_id(self):
        from agent_bridge_connect.cli import _origin_context

        with _conversation_trace_mock.patch.dict(_conversation_trace_os.environ, {"CODEX_SHELL": "1", "CLAUDE_SESSION_ID": "wrong-platform-thread"}, clear=True):
            context = _origin_context(None, None)
        self.assertEqual(context, (None, "codex"))

    def test_missing_matching_environment_id_is_unavailable(self):
        from agent_bridge_connect.cli import _origin_context

        with _conversation_trace_mock.patch.dict(_conversation_trace_os.environ, {"CLAUDE_SESSION_ID": "wrong-platform-thread"}, clear=True):
            context = _origin_context(None, "hermes")
        self.assertEqual(context, (None, "hermes"))


class _conversation_trace_ConversationTraceFlowTests(_conversation_trace_unittest.TestCase):
    def setUp(self):
        self.temp = _conversation_trace_tempfile.TemporaryDirectory()
        self.root = _conversation_trace_Path(self.temp.name)
        self.board = self.root / "record"
        self.workspace = self.root / "workspace"
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(f'workspace_root = "{self.workspace}"\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _service(self):
        from agent_bridge_connect.service import TaskService

        return TaskService(self.board, config={"workspace_root": str(self.workspace)})

    def _completed_source(self, *, session_id: str = "source-conversation", source_platform: str = "claude"):
        service = self._service()
        source = service.create_task("Source task", "hermes", [{"id": 1, "description": "finish source"}], session_id=session_id, source_platform=source_platform, customer_dir=False)
        data = service.store.read_task(source.id)
        data["status"] = "completed"
        data["steps"][0]["status"] = "done"
        service.store.write_task(source.id, data)
        return (service, service.get_task(source.id))

    def test_create_cli_captures_matching_environment_conversation(self):
        from agent_bridge_connect.cli import build_parser, command_task_create

        steps = self.root / "steps.yaml"
        steps.write_text("steps:\n- description: create traced task\n", encoding="utf-8")
        args = build_parser().parse_args(["task", "create", "--root", str(self.board), "--title", "Traced create", "--assignee", "hermes", "--steps", str(steps), "--source-platform", "codex", "--customer-path", "default path", "--config", str(self.config_path)])
        with _conversation_trace_mock.patch.dict(_conversation_trace_os.environ, {"CODEX_THREAD_ID": "create-conversation", "CLAUDE_SESSION_ID": "wrong-conversation"}, clear=True), _conversation_trace_contextlib.redirect_stdout(_conversation_trace_io.StringIO()):
            result = command_task_create(args)
        task = self._service().list_tasks()[0]
        requirements = _conversation_trace_Path(task.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertEqual(task.session_id, "create-conversation")
        self.assertEqual(task.extensions["agentbc.provenance"]["source_platform"], "codex")
        self.assertIn("Dispatcher platform: `codex`", requirements)
        self.assertIn("Dispatcher conversation ID: `create-conversation`", requirements)

    def test_confirmed_handoff_owns_new_id_without_mutating_source_trace(self):
        from agent_bridge_connect.cli import build_parser, command_task_intervention

        (service, source) = self._completed_source()
        args = build_parser().parse_args(["task", "handoff", source.id, "--root", str(self.board), "--to", "codex", "--message", "continue", "--session-id", "handoff-conversation", "--source-platform", "codex", "--config", str(self.config_path)])

        def dispatch_handoff(**kwargs):
            followup = service.handoff_task(kwargs["source_task_id"], kwargs["target_assignee"], kwargs["message"], branch=kwargs["branch"], session_id=kwargs["session_id"], source_platform=kwargs["source_platform"], images=kwargs["images"], permission_mode=kwargs["permission_mode"])
            return {"task_id": followup.id, "assignee": followup.assignee, "workspace": followup.workspace, "run_id": "test-run", "dispatch_status": "accepted", "monitor_status": "opened"}

        with _conversation_trace_mock.patch("builtins.input", return_value="y"), _conversation_trace_mock.patch("agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch", side_effect=dispatch_handoff), _conversation_trace_contextlib.redirect_stdout(_conversation_trace_io.StringIO()):
            result = command_task_intervention(args)
        source_after = service.get_task(source.id)
        followup = next((task for task in service.list_tasks() if task.id != source.id))
        requirements = _conversation_trace_Path(followup.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertEqual(source_after.session_id, "source-conversation")
        self.assertEqual(source_after.extensions["agentbc.provenance"]["source_platform"], "claude")
        self.assertEqual(followup.session_id, "handoff-conversation")
        self.assertEqual(followup.extensions["agentbc.provenance"]["source_platform"], "codex")
        self.assertIn("Dispatcher conversation ID: `handoff-conversation`", requirements)

    def test_handoff_without_current_id_does_not_inherit_source_id(self):
        (service, source) = self._completed_source()
        followup = service.handoff_task(source.id, "codex", "continue without an available dispatcher id", source_platform="codex")
        requirements = _conversation_trace_Path(followup.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertIsNone(followup.session_id)
        self.assertEqual(service.get_task(source.id).session_id, "source-conversation")
        self.assertIn("Dispatcher conversation ID: `unavailable`", requirements)

    def test_atomic_handoff_cli_passes_detected_conversation_to_runner_client(self):
        from agent_bridge_connect.cli import build_parser, command_task_intervention

        args = build_parser().parse_args(["task", "handoff", "TRACE-001", "--root", str(self.board), "--to", "hermes", "--source-platform", "codex", "--dispatch"])
        dispatched = {"task_id": "TRACE-002", "assignee": "hermes", "run_id": "runner-worker-trace", "dispatch_status": "accepted", "monitor_status": "disabled"}
        with (
            _conversation_trace_mock.patch.dict(_conversation_trace_os.environ, {"CODEX_THREAD_ID": "atomic-cli-conversation"}, clear=True),
            _conversation_trace_mock.patch("agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch", return_value=dispatched) as handoff,
            _conversation_trace_contextlib.redirect_stdout(_conversation_trace_io.StringIO()),
        ):
            result = command_task_intervention(args)
        self.assertEqual(result, 0)
        self.assertEqual(handoff.call_args.kwargs["session_id"], "atomic-cli-conversation")
        self.assertEqual(handoff.call_args.kwargs["source_platform"], "codex")

    def test_runner_client_request_includes_handoff_conversation(self):
        from agent_bridge_connect.runner import RunnerClient

        client = RunnerClient(self.root / "spool", self.root / "token")
        with _conversation_trace_mock.patch.object(client, "_request", return_value={"task_id": "TRACE-002"}) as request:
            client.handoff_and_dispatch("TRACE-001", "hermes", "continue", self.board, None, source_platform="codex", session_id="runner-client-conversation", steps=[{"id": 1, "description": "bound step"}])
        payload = request.call_args.args[0]
        self.assertEqual(payload["steps"], [{"id": 1, "description": "bound step"}])
        self.assertEqual(payload["session_id"], "runner-client-conversation")
        self.assertEqual(payload["source_platform"], "codex")

    def test_runner_request_stores_atomic_handoff_conversation(self):
        from agent_bridge_connect.runner import RunnerState
        from agent_bridge_connect.service import TaskService

        executable = self.root / "hermes"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | _conversation_trace_stat.S_IXUSR)
        state = RunnerState(self.root / "runner-state", [self.root], {"hermes": executable})
        service = TaskService(self.board, config={"workspace_root": str(self.root)})
        source = service.create_task("Atomic source", "hermes", [{"id": 1, "description": "finish source"}], session_id="source-atomic-conversation", source_platform="claude", customer_dir=True, customer_path=self.root)
        source_data = service.store.read_task(source.id)
        source_data["status"] = "completed"
        source_data["steps"][0]["status"] = "done"
        service.store.write_task(source.id, source_data)
        dispatched = {"run_id": "runner-worker-atomic-trace", "pid": 1234, "status": "running", "task_id": f"{source.workspace['task_code']}-002", "dispatch_status": "accepted", "monitor_status": "disabled"}
        with _conversation_trace_mock.patch.object(state, "dispatch_worker", return_value=dispatched):
            result = state.handoff_and_dispatch({"source_task_id": source.id, "target_assignee": "hermes", "message": "continue atomically", "steps": [{"id": 1, "description": "first"}, {"id": 2, "description": "second"}], "session_id": "runner-request-conversation", "source_platform": "codex", "board_root": str(self.board), "interval_s": 2, "monitor": False})
        followup = TaskService(self.board).get_task(result["task_id"])
        source_after = TaskService(self.board).get_task(source.id)
        self.assertEqual([s["description"] for s in followup.steps], ["first", "second"])
        self.assertEqual(followup.session_id, "runner-request-conversation")
        self.assertEqual(followup.extensions["agentbc.provenance"]["source_platform"], "codex")
        self.assertEqual(source_after.session_id, "source-atomic-conversation")
