from __future__ import annotations
import json
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from agent_bridge_connect.execution_policy import RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY, build_resource_snapshot, build_session_snapshot
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.runner import PHASE2_AUDIT_EVENT_TYPE, PHASE2_EXECUTION_EXTENSION_KEY, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, PHASE2_LEGACY_BACKFILLED_AT_KEY, PHASE2_LEGACY_BACKFILLED_KEY, PHASE2_LEGACY_UNRECORDED_KEY, RunnerError, RunnerState
from agent_bridge_connect.task_store import TaskStore
import copy as _runner_authorization_copy
import stat as _runner_authorization_stat
import subprocess as _runner_authorization_subprocess
import tempfile as _runner_authorization_tempfile
import unittest as _runner_authorization_unittest
from pathlib import Path as _runner_authorization_Path
from types import SimpleNamespace as _runner_authorization_SimpleNamespace
from unittest import mock as _runner_authorization_mock
from agent_bridge_connect.effective_permissions import resolve_effective_permission as _runner_authorization_resolve_effective_permission
from agent_bridge_connect.executors.claude import ClaudeExecutor as _runner_authorization_ClaudeExecutor
from agent_bridge_connect.executors.codex import CodexExecutor as _runner_authorization_CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor as _runner_authorization_HermesExecutor
from agent_bridge_connect.permission_grants import PERMISSION_GRANT_EXTENSION_KEY as _runner_authorization_PERMISSION_GRANT_EXTENSION_KEY, build_permission_grant as _runner_authorization_build_permission_grant
from agent_bridge_connect.permission_modes import permission_flags as _runner_authorization_permission_flags
from agent_bridge_connect.runner import CLAUDE_SDK_CONTROL_AUTHORIZATION as _runner_authorization_CLAUDE_SDK_CONTROL_AUTHORIZATION, RunnerClient as _runner_authorization_RunnerClient, RunnerError as _runner_authorization_RunnerError, RunnerState as _runner_authorization_RunnerState
from agent_bridge_connect.service import TaskService as _runner_authorization_TaskService


def _write_script(path: Path, body: str = "#!/bin/sh\nprintf 'OK'\n") -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Phase2RunnerPolicyTests(unittest.TestCase):
    """Runner legacy backfill and execution-policy validation (1.0.2A Phase 2)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_hermes = self.root / "hermes"
        self.fake_claude = self.root / "claude"
        self.fake_codex = self.root / "codex"
        _write_script(self.fake_hermes)
        _write_script(self.fake_claude)
        _write_script(self.fake_codex)
        self.board = self.root / "board"
        self.state = RunnerState(self.root / "state", [self.root], {"hermes": self.fake_hermes, "claude": self.fake_claude, "codex": self.fake_codex})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self):
        from agent_bridge_connect.service import TaskService

        return TaskService(self.board, config={"workspace_root": str(self.root)})

    def _create_task(self, executor: str) -> str:
        service = self._service()
        task = service.create_task("Phase 2 policy test", executor, [{"id": 1, "description": "run"}], customer_dir=True, customer_path=self.root, permission_mode="safe")
        raw = service.store.read_task(task.id)
        raw["workspace"].pop(PHASE2_EXECUTOR_PROJECT_ROOT_KEY, None)
        raw["extensions"].pop(RESOURCE_EXTENSION_KEY, None)
        raw["extensions"].pop(SESSION_EXTENSION_KEY, None)
        service.store.write_task(task.id, raw)
        return task.id

    def _read_task(self, task_id: str) -> dict:
        return TaskStore(self.board).read_task(task_id)

    def _write_task(self, task_id: str, updates: dict) -> None:
        task = self._read_task(task_id)
        task.update(updates)
        TaskStore(self.board).write_task(task_id, task)

    def _inject_workspace_key(self, task_id: str, key: str, value: object) -> None:
        task = self._read_task(task_id)
        workspace = dict(task.get("workspace") or {})
        workspace[key] = value
        task["workspace"] = workspace
        TaskStore(self.board).write_task(task_id, task)

    def _attach_snapshots(self, task_id: str, resources: dict | None, session: dict) -> None:
        task = self._read_task(task_id)
        extensions = dict(task.get("extensions") or {})
        if resources is None:
            extensions.pop(RESOURCE_EXTENSION_KEY, None)
        else:
            extensions[RESOURCE_EXTENSION_KEY] = dict(resources)
        extensions[SESSION_EXTENSION_KEY] = dict(session)
        task["extensions"] = extensions
        TaskStore(self.board).write_task(task_id, task)

    def _make_native(self, task_id: str, executor: str) -> None:
        workspace = self._read_task(task_id)["workspace"]
        executor_project_root = Path(workspace["agentbc_root"]) / "tasks" / "artifacts" / workspace["task_date"] / workspace["task_code"] / task_id / "claude"
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(executor_project_root))
        if executor == "claude":
            resources = build_resource_snapshot("claude", 10.0, source="config")
            session = build_session_snapshot("claude", retain=False, session_id=str(uuid.uuid4()), session_state="pending", project_path=str(executor_project_root))
        elif executor == "hermes":
            resources = build_resource_snapshot("hermes", 90, source="config")
            session = build_session_snapshot("hermes", retain=False, session_state="pending")
        else:
            resources = None
            session = build_session_snapshot("codex", retain=False, session_state="pending")
        self._attach_snapshots(task_id, resources, session)

    def _packet(self, task_id: str) -> dict:
        task = self._read_task(task_id)
        packet = dict(task)
        packet["task_id"] = task_id
        packet["task_board"] = {"root": str(self.board)}
        return packet

    def _audit_events(self, task_id: str) -> list[dict]:
        return TaskStore(self.board).read_events(task_id)

    def _policy_audits(self, task_id: str) -> list[dict]:
        return [event for event in self._audit_events(task_id) if event.get("event_type") == PHASE2_AUDIT_EVENT_TYPE]

    def _dispatch(self, task_id: str, executor: str, resuming: bool = False) -> tuple[dict, mock.MagicMock]:
        fake_run = {"ok": True, "run_id": f"run-{task_id}", "pid": 4242, "status": "running"}
        with mock.patch.object(RunnerState, "_spawn_process", return_value=fake_run) as spawn:
            result = self.state.dispatch_worker(task_id, executor, str(self.board), "", 0.5, False, resuming=resuming)
        return (result, spawn)

    def _authorize(self, executor: str, command: list[str], packet: dict) -> dict:
        return self.state.authorize_command(executor, command, str(self.root), packet)

    def _hermes_command(self, *extra: str) -> list[str]:
        return [str(self.fake_hermes), "chat", "-q", *extra, "hello"]

    def _claude_command(self, *extra: str) -> list[str]:
        return [str(self.fake_claude), "-p", "--safe-mode", "--permission-mode", "acceptEdits", "--output-format", "text", *extra, "hello"]

    def test_legacy_claude_dispatch_backfills_canonical_snapshots_once(self) -> None:
        task_id = self._create_task("claude")
        task = self._read_task(task_id)
        self.assertNotIn(RESOURCE_EXTENSION_KEY, task.get("extensions") or {})
        (result, spawn) = self._dispatch(task_id, "claude")
        self.assertEqual(result["dispatch_status"], "accepted")
        refreshed = self._read_task(task_id)
        extensions = refreshed["extensions"]
        resources = extensions[RESOURCE_EXTENSION_KEY]
        session = extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(resources["executor"], "claude")
        self.assertEqual(resources["resource"], "max_budget_usd")
        self.assertEqual(resources["configured_limit"], 10.0)
        self.assertEqual(resources["current_limit"], 10.0)
        self.assertEqual(resources["source"], "legacy_default_10")
        self.assertFalse(session["retain"])
        self.assertEqual(session["session_state"], "pending")
        self.assertEqual(session["project_mode"], "ephemeral")
        workspace = refreshed["workspace"]
        expected_path = Path(workspace["agentbc_root"]) / "tasks" / "artifacts" / workspace["task_date"] / workspace["task_code"] / task_id / "claude"
        self.assertEqual(Path(session["project_path"]), expected_path)
        self.assertEqual(str(uuid.UUID(session["session_id"])), session["session_id"])
        execution = extensions[PHASE2_EXECUTION_EXTENSION_KEY]
        self.assertTrue(execution[PHASE2_LEGACY_BACKFILLED_KEY])
        first_backfilled_at = execution[PHASE2_LEGACY_BACKFILLED_AT_KEY]
        self.assertEqual(len(self._policy_audits(task_id)), 1)
        self.assertEqual(self._policy_audits(task_id)[0]["outcome"], "backfilled")
        worker_command = spawn.call_args.args[1]
        for flag in ("--max-budget-usd", "--max-turns", "--session-id", "--resume"):
            self.assertNotIn(flag, worker_command)
        self._dispatch(task_id, "claude")
        refreshed_again = self._read_task(task_id)
        self.assertEqual(refreshed_again["extensions"][RESOURCE_EXTENSION_KEY], resources)
        self.assertEqual(refreshed_again["extensions"][SESSION_EXTENSION_KEY], session)
        self.assertEqual(refreshed_again["extensions"][PHASE2_EXECUTION_EXTENSION_KEY][PHASE2_LEGACY_BACKFILLED_AT_KEY], first_backfilled_at)
        self.assertEqual(len(self._policy_audits(task_id)), 1)

    def test_legacy_hermes_dispatch_backfills_turns_default(self) -> None:
        task_id = self._create_task("hermes")
        self._dispatch(task_id, "hermes")
        extensions = self._read_task(task_id)["extensions"]
        resources = extensions[RESOURCE_EXTENSION_KEY]
        session = extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(resources["resource"], "max_turns")
        self.assertEqual(resources["configured_limit"], 90)
        self.assertEqual(resources["source"], "legacy_default_90")
        self.assertFalse(session["retain"])
        self.assertEqual(session["project_mode"], "none")
        self.assertEqual(session["project_path"], "")

    def test_legacy_codex_dispatch_backfills_only_session(self) -> None:
        task_id = self._create_task("codex")
        self._dispatch(task_id, "codex")
        extensions = self._read_task(task_id)["extensions"]
        self.assertNotIn(RESOURCE_EXTENSION_KEY, extensions)
        session = extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(session["executor"], "codex")
        self.assertFalse(session["retain"])
        self.assertEqual(session["project_mode"], "none")

    def test_partial_legacy_backfill_preserves_existing_snapshot(self) -> None:
        task_id = self._create_task("hermes")
        task = self._read_task(task_id)
        preserved = build_resource_snapshot("hermes", 77, source="preserved_legacy_snapshot")
        task["extensions"][RESOURCE_EXTENSION_KEY] = preserved
        self._write_task(task_id, {"extensions": task["extensions"]})
        self._dispatch(task_id, "hermes")
        extensions = self._read_task(task_id)["extensions"]
        self.assertEqual(extensions[RESOURCE_EXTENSION_KEY], preserved)
        self.assertIn(SESSION_EXTENSION_KEY, extensions)

    def test_legacy_backfill_never_reads_user_configuration(self) -> None:
        task_id = self._create_task("claude")
        hostile_config = {"workspace_root": str(self.root), "executors": {"claude": {"max_budget_usd": 25.0}, "hermes": {"max_turns": 200}}, "sessions": {"retain_executor_sessions": True}}
        with mock.patch("agent_bridge_connect.config.load_config", return_value=hostile_config):
            self._dispatch(task_id, "claude")
        resources = self._read_task(task_id)["extensions"][RESOURCE_EXTENSION_KEY]
        self.assertEqual(resources["configured_limit"], 10.0)
        self.assertEqual(resources["source"], "legacy_default_10")

    def test_resuming_legacy_task_is_backfilled_on_redispatch(self) -> None:
        task_id = self._create_task("hermes")
        execution = dict((self._read_task(task_id).get("extensions") or {}).get(PHASE2_EXECUTION_EXTENSION_KEY) or {})
        execution["internal_status"] = "resuming"
        self._write_task(task_id, {"status": "running", "extensions": {**self._read_task(task_id)["extensions"], PHASE2_EXECUTION_EXTENSION_KEY: execution}})
        (result, _spawn) = self._dispatch(task_id, "hermes", resuming=True)
        self.assertEqual(result["dispatch_status"], "accepted")
        extensions = self._read_task(task_id)["extensions"]
        self.assertIn(RESOURCE_EXTENSION_KEY, extensions)
        self.assertIn(SESSION_EXTENSION_KEY, extensions)
        self.assertEqual(extensions[PHASE2_EXECUTION_EXTENSION_KEY]["internal_status"], "resuming")

    def test_terminal_legacy_task_is_not_rewritten_and_marked_unrecorded(self) -> None:
        task_id = self._create_task("claude")
        self._write_task(task_id, {"status": "completed"})
        with self.assertRaisesRegex(RunnerError, "not pending"):
            self._dispatch(task_id, "claude")
        task = self._read_task(task_id)
        extensions = task.get("extensions") or {}
        self.assertNotIn(RESOURCE_EXTENSION_KEY, extensions)
        self.assertNotIn(SESSION_EXTENSION_KEY, extensions)
        execution = extensions.get(PHASE2_EXECUTION_EXTENSION_KEY) or {}
        self.assertIs(execution.get(PHASE2_LEGACY_UNRECORDED_KEY), True)
        marked_at = task["updated_at"]
        with self.assertRaisesRegex(RunnerError, "not pending"):
            self._dispatch(task_id, "claude")
        task_again = self._read_task(task_id)
        self.assertNotIn(RESOURCE_EXTENSION_KEY, task_again.get("extensions") or {})
        self.assertEqual(task_again["updated_at"], marked_at)

    def test_native_task_missing_snapshots_fails_closed_without_legacy_fix(self) -> None:
        task_id = self._create_task("claude")
        workspace = self._read_task(task_id)["workspace"]
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(Path(workspace["agentbc_root"]) / "tasks" / "artifacts" / workspace["task_date"] / workspace["task_code"] / task_id / "claude"))
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._dispatch(task_id, "claude")
        extensions = self._read_task(task_id).get("extensions") or {}
        self.assertNotIn(RESOURCE_EXTENSION_KEY, extensions)
        self.assertNotIn(SESSION_EXTENSION_KEY, extensions)
        self.assertNotIn(PHASE2_LEGACY_BACKFILLED_KEY, extensions.get(PHASE2_EXECUTION_EXTENSION_KEY) or {})
        audits = self._policy_audits(task_id)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["outcome"], "fail")
        self.assertIn("invalid_execution_policy", audits[0]["reason"])

    def test_native_task_with_valid_snapshots_dispatches(self) -> None:
        task_id = self._create_task("claude")
        self._make_native(task_id, "claude")
        (result, _spawn) = self._dispatch(task_id, "claude")
        self.assertEqual(result["dispatch_status"], "accepted")

    def test_native_codex_requires_session_but_not_resources(self) -> None:
        task_id = self._create_task("codex")
        self._make_native(task_id, "codex")
        (result, _spawn) = self._dispatch(task_id, "codex")
        self.assertEqual(result["dispatch_status"], "accepted")
        self.assertNotIn(RESOURCE_EXTENSION_KEY, self._read_task(task_id)["extensions"])

    def test_native_task_executor_mismatch_fails_closed(self) -> None:
        task_id = self._create_task("claude")
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(self.root / "exec-project"))
        self._attach_snapshots(task_id, build_resource_snapshot("hermes", 90, source="config"), build_session_snapshot("hermes", retain=False, session_state="pending"))
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._dispatch(task_id, "claude")
        audits = self._policy_audits(task_id)
        self.assertEqual(audits[0]["outcome"], "fail")

    def test_native_task_corrupt_snapshot_fails_closed(self) -> None:
        task_id = self._create_task("hermes")
        self._make_native(task_id, "hermes")
        resources = dict(self._read_task(task_id)["extensions"][RESOURCE_EXTENSION_KEY])
        resources["version"] = 99
        self._attach_snapshots(task_id, resources, self._read_task(task_id)["extensions"][SESSION_EXTENSION_KEY])
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._dispatch(task_id, "hermes")

    def test_authorize_consistent_native_packet_passes(self) -> None:
        task_id = self._create_task("claude")
        self._make_native(task_id, "claude")
        packet = self._packet(task_id)
        Path(packet["extensions"][SESSION_EXTENSION_KEY]["project_path"]).mkdir(parents=True, exist_ok=True)
        command = ClaudeExecutor(command=str(self.fake_claude), transport="direct")._build_command("hello", Path(packet["workspace"]["root"]), packet)
        result = self.state.authorize_command("claude", command, packet["extensions"][SESSION_EXTENSION_KEY]["project_path"], packet)
        self.assertTrue(result["authorized"])
        hermes_id = self._create_task("hermes")
        self._make_native(hermes_id, "hermes")
        result = self._authorize("hermes", self._hermes_command("--max-turns", "90"), self._packet(hermes_id))
        self.assertTrue(result["authorized"])

    def test_authorize_requires_frozen_resource_flags_without_injecting_them(self) -> None:
        task_id = self._create_task("hermes")
        self._make_native(task_id, "hermes")
        packet = self._packet(task_id)
        with self.assertRaisesRegex(RunnerError, "runner_resource_argument_mismatch"):
            self._authorize("hermes", self._hermes_command(), packet)
        result = self._authorize("hermes", self._hermes_command("--max-turns", "90"), packet)
        self.assertTrue(result["authorized"])
        self.assertEqual(self._policy_audits(task_id)[0]["reason"], "runner_resource_argument_mismatch")

    def test_authorize_backfilled_legacy_packet_passes(self) -> None:
        task_id = self._create_task("hermes")
        self._dispatch(task_id, "hermes")
        result = self._authorize("hermes", self._hermes_command("--max-turns", "90"), self._packet(task_id))
        self.assertTrue(result["authorized"])

    def test_authorize_missing_packet_snapshot_fails_closed(self) -> None:
        task_id = self._create_task("claude")
        self._make_native(task_id, "claude")
        packet = self._packet(task_id)
        del packet["extensions"][RESOURCE_EXTENSION_KEY]
        with self.assertRaisesRegex(RunnerError, "execution_policy_mismatch: missing:agentbc.resources"):
            self._authorize("claude", self._claude_command(), packet)
        audits = self._policy_audits(task_id)
        self.assertEqual(audits[0]["outcome"], "fail")
        self.assertEqual(audits[0]["reason"], "missing:agentbc.resources")

    def test_authorize_injected_packet_snapshot_fails_closed(self) -> None:
        task_id = self._create_task("hermes")
        packet = self._packet(task_id)
        packet["extensions"][RESOURCE_EXTENSION_KEY] = build_resource_snapshot("hermes", 90, source="config")
        with self.assertRaisesRegex(RunnerError, "execution_policy_mismatch: injected:agentbc.resources"):
            self._authorize("hermes", self._hermes_command(), packet)
        audits = self._policy_audits(task_id)
        self.assertEqual(audits[0]["outcome"], "fail")
        self.assertEqual(audits[0]["reason"], "injected:agentbc.resources")

    def test_authorize_modified_packet_snapshot_fails_closed(self) -> None:
        task_id = self._create_task("claude")
        self._make_native(task_id, "claude")
        packet = self._packet(task_id)
        modified = dict(packet["extensions"][RESOURCE_EXTENSION_KEY])
        modified["configured_limit"] = 999.0
        packet["extensions"][RESOURCE_EXTENSION_KEY] = modified
        with self.assertRaisesRegex(RunnerError, "execution_policy_mismatch: modified:agentbc.resources"):
            self._authorize("claude", self._claude_command(), packet)
        audits = self._policy_audits(task_id)
        self.assertEqual(audits[0]["outcome"], "fail")
        self.assertEqual(audits[0]["reason"], "modified:agentbc.resources")

    def test_authorize_expired_packet_fails_closed(self) -> None:
        task_id = self._create_task("hermes")
        self._make_native(task_id, "hermes")
        packet = self._packet(task_id)
        extensions = dict(self._read_task(task_id)["extensions"])
        execution = dict(extensions[PHASE2_EXECUTION_EXTENSION_KEY])
        execution["internal_status"] = "running"
        extensions[PHASE2_EXECUTION_EXTENSION_KEY] = execution
        self._write_task(task_id, {"extensions": extensions})
        with self.assertRaisesRegex(RunnerError, "execution_policy_mismatch: expired:"):
            self._authorize("hermes", self._hermes_command(), packet)
        audits = self._policy_audits(task_id)
        self.assertEqual(audits[0]["outcome"], "fail")
        self.assertIn("expired:", audits[0]["reason"])

    def test_authorize_native_missing_disk_snapshots_fails_closed(self) -> None:
        task_id = self._create_task("claude")
        workspace = self._read_task(task_id)["workspace"]
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(Path(workspace["agentbc_root"]) / "tasks" / "artifacts" / workspace["task_date"] / workspace["task_code"] / task_id / "claude"))
        packet = self._packet(task_id)
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._authorize("claude", self._claude_command(), packet)
        extensions = self._read_task(task_id).get("extensions") or {}
        self.assertNotIn(RESOURCE_EXTENSION_KEY, extensions)
        self.assertNotIn(SESSION_EXTENSION_KEY, extensions)

    def test_authorize_legacy_task_missing_disk_snapshots_fails_closed(self) -> None:
        task_id = self._create_task("hermes")
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._authorize("hermes", self._hermes_command(), self._packet(task_id))
        audits = self._policy_audits(task_id)
        self.assertEqual(len(audits), 1)
        self.assertIn("invalid_execution_policy", audits[0]["reason"])

    def test_authorize_corrupt_disk_snapshot_fails_closed(self) -> None:
        task_id = self._create_task("hermes")
        self._make_native(task_id, "hermes")
        resources = dict(self._read_task(task_id)["extensions"][RESOURCE_EXTENSION_KEY])
        resources["version"] = 99
        self._attach_snapshots(task_id, resources, self._read_task(task_id)["extensions"][SESSION_EXTENSION_KEY])
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._authorize("hermes", self._hermes_command(), self._packet(task_id))

    def test_execution_policy_audit_never_leaks_paths_content_or_credentials(self) -> None:
        task_id = self._create_task("claude")
        self._make_native(task_id, "claude")
        packet = self._packet(task_id)
        modified = dict(packet["extensions"][RESOURCE_EXTENSION_KEY])
        modified["configured_limit"] = 999.0
        packet["extensions"][RESOURCE_EXTENSION_KEY] = modified
        with self.assertRaisesRegex(RunnerError, "execution_policy_mismatch"):
            self._authorize("claude", self._claude_command(), packet)
        audits = self._policy_audits(task_id)
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        self.assertEqual(audit["event_type"], PHASE2_AUDIT_EVENT_TYPE)
        self.assertEqual(audit["task_id"], task_id)
        self.assertEqual(audit["executor"], "claude")
        self.assertEqual(audit["outcome"], "fail")
        self.assertEqual(audit["reason"], "modified:agentbc.resources")
        self.assertIn("created_at", audit)
        serialized = json.dumps(audits, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("Phase 2 policy test", serialized)
        self.assertNotIn("hello", serialized)
        self.assertNotIn("token", serialized.lower())


class _runner_authorization_Phase6RunnerAdapterAuthorizationTests(_runner_authorization_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _runner_authorization_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _runner_authorization_Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.root / "record"
        self.binaries = {name: self.root / name for name in ("codex", "claude", "hermes")}
        full_flags = {"codex": "--dangerously-bypass-approvals-and-sandbox", "claude": "--dangerously-skip-permissions", "hermes": "--yolo"}
        for name, path in self.binaries.items():
            path.write_text(f"""#!/bin/sh\ncase "$*" in\n  *--help*) printf '%s\\n' '{full_flags[name]}'; exit 0;;\nesac\nexit 0\n""", encoding="utf-8")
            path.chmod(path.stat().st_mode | _runner_authorization_stat.S_IXUSR)
        self.state = _runner_authorization_RunnerState(self.root / "runner", [self.root], self.binaries)

    def _grant_packet(self, executor: str) -> tuple[_runner_authorization_TaskService, dict, str, str]:
        config = {"workspace_root": str(self.root / "workspace"), "executors": {"claude": {"max_budget_usd": 10.0}, "hermes": {"max_turns": 90}}, "sessions": {"retain_executor_sessions": True}}
        service = _runner_authorization_TaskService(self.board, config=config)
        task = service.create_task(f"Phase 6 {executor}", executor, [{"id": 1, "description": "continue with one-shot full"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        raw = service.store.read_task(task.id)
        source_run_id = f"{executor}-{task.id}-source"
        session_ids = {"codex": "019feed0-0000-7000-8000-000000000006", "claude": "019feed0-0000-7000-8000-000000000007", "hermes": "20260812_010203_phase6"}
        session_id = session_ids[executor]
        session = dict(raw["extensions"]["agentbc.session"])
        session.update({"session_id": session_id, "session_state": "active", "run_ids": [source_run_id]})
        input_id = f"input-{executor}-phase6"
        input_request = {"input_id": input_id, "executor_run_id": source_run_id, "blocked_step_id": 1, "type": "permission", "requested_permission": "full", "status": "answered", "response": {"type": "approve", "summary": "approve"}}
        grant = _runner_authorization_build_permission_grant(executor=executor, task_id=task.id, input_id=input_id, session_id=session_id, source_run_id=source_run_id)
        raw["status"] = "running"
        raw["extensions"]["agentbc.execution"]["internal_status"] = "resuming"
        raw["extensions"]["agentbc.execution"]["resuming_at"] = raw["updated_at"]
        raw["extensions"]["agentbc.session"] = session
        raw["extensions"]["agentbc.input"] = input_request
        raw["extensions"][_runner_authorization_PERMISSION_GRANT_EXTENSION_KEY] = grant
        service.store.write_task(task.id, raw)
        packet = _runner_authorization_copy.deepcopy(raw)
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(self.board)}
        packet["runner_authorization_required"] = True
        return (service, packet, source_run_id, session_id)

    def _command(self, executor: str, packet: dict, *, full: bool = True) -> list[str]:
        session = packet["extensions"]["agentbc.session"]
        mode = "full" if full else "safe"
        if executor == "codex":
            return [str(self.binaries[executor]), "exec", "--json", *_runner_authorization_permission_flags(executor, mode), "--skip-git-repo-check", "resume", session["session_id"], "prompt"]
        if executor == "claude":
            resources = packet["extensions"]["agentbc.resources"]
            return [str(self.binaries[executor]), "-p", *_runner_authorization_permission_flags(executor, mode), "--resume", session["session_id"], "--output-format", "text", "--max-budget-usd", str(float(resources["current_limit"])), "prompt"]
        resources = packet["extensions"]["agentbc.resources"]
        return [str(self.binaries[executor]), "chat", *_runner_authorization_permission_flags(executor, mode), "--max-turns", str(int(resources["current_limit"])), "--resume", session["session_id"], "-Q", "-q", "prompt"]

    def _cwd(self, executor: str, packet: dict) -> str:
        if executor == "claude":
            return packet["extensions"]["agentbc.session"]["project_path"]
        return str(self.project)

    def test_persisted_base_modes_resolve_without_runner_context(self) -> None:
        for mode in ("inherit", "safe", "full"):
            with self.subTest(mode=mode):
                resolved = _runner_authorization_resolve_effective_permission({"extensions": {"agentbc.permission": {"requested_mode": mode, "effective_mode": mode, "selection_source": "explicit_task"}}}, "codex", f"codex-base-{mode}")
                self.assertEqual(resolved["effective_mode"], mode)

    def _assert_unmanaged_adapter_rejected(self, executor_name: str) -> None:
        (service, packet, _source, _session) = self._grant_packet(executor_name)
        packet.pop("runner_authorization_required")
        executors = {"codex": _runner_authorization_CodexExecutor(command=str(self.binaries["codex"])), "claude": _runner_authorization_ClaudeExecutor(command=str(self.binaries["claude"]), transport="direct"), "hermes": _runner_authorization_HermesExecutor(command=str(self.binaries["hermes"]), transport="direct")}
        executor = executors[executor_name]
        with (
            _runner_authorization_mock.patch.object(executor, "_start_run_lease"),
            _runner_authorization_mock.patch.object(executor, "_close_run_lease"),
            _runner_authorization_mock.patch.object(executor, "_build_command") as build_command,
            _runner_authorization_mock.patch(f"agent_bridge_connect.executors.{executor_name}.subprocess.run") as spawn,
            _runner_authorization_mock.patch(f"agent_bridge_connect.executors.{executor_name}.RunnerClient.authorize_command") as authorize,
        ):
            result = executor.start(packet)
        self.assertFalse(result.ok)
        self.assertIn("permission_grant_runner_context_required", result.message)
        build_command.assert_not_called()
        spawn.assert_not_called()
        authorize.assert_not_called()
        grant = service.store.read_task(packet["task_id"])["extensions"][_runner_authorization_PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"]["target_run_id"], "")

    def test_runner_client_transports_executor_run_id_internally(self) -> None:
        client = _runner_authorization_RunnerClient(spool_root=self.root / "spool")
        with _runner_authorization_mock.patch.object(client, "_request", return_value={"ok": True}) as request:
            client.authorize_command("codex", ["codex", "exec", "--json"], self.project, {"task_id": "ABCD-001"}, executor_run_id="codex-ABCD-001-target")
            authorize_payload = request.call_args.args[0]
            self.assertEqual(authorize_payload["executor_run_id"], "codex-ABCD-001-target")
            client.submit("hermes", ["hermes", "chat", "-q", "prompt"], self.project, task={"task_id": "EFGH-001"}, executor_run_id="hermes-EFGH-001-target")
            submit_payload = request.call_args.args[0]
            self.assertEqual(submit_payload["executor_run_id"], "hermes-EFGH-001-target")
            client.authorize_transport("claude", _runner_authorization_CLAUDE_SDK_CONTROL_AUTHORIZATION, self.project, {"task_id": "SDKC-001"}, {"control_path": "sdk_control_transport"}, executor_run_id="claude-SDKC-001-target")
            transport_payload = request.call_args.args[0]
            self.assertEqual(transport_payload["op"], "authorize_transport")
            self.assertEqual(transport_payload["transport"], _runner_authorization_CLAUDE_SDK_CONTROL_AUTHORIZATION)
            self.assertEqual(transport_payload["executor_run_id"], "claude-SDKC-001-target")
            self.assertEqual(transport_payload["context"], {"control_path": "sdk_control_transport"})

    def test_runner_sdk_transport_rejects_context_drift(self) -> None:
        service = _runner_authorization_TaskService(self.root / "sdk-safe-board", config={"workspace_root": str(self.root / "sdk-safe-workspace"), "executors": {"claude": {"max_budget_usd": 10.0}}, "sessions": {"retain_executor_sessions": True}})
        task = service.create_task("SDK context drift", "claude", [{"id": 1, "description": "reject drift"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        packet = service.store.read_task(task.id)
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(service.board_root)}
        packet["runner_authorization_required"] = True
        session = packet["extensions"]["agentbc.session"]
        executor = _runner_authorization_ClaudeExecutor(command=str(self.binaries["claude"]))
        context = executor._build_sdk_authorization_context(
            packet, _runner_authorization_Path(session["project_path"]), session["session_id"], {"sdk_version": "0.2.142", "platform": "macOS arm64", "cli_path": str(self.binaries["claude"])}, _runner_authorization_resolve_effective_permission(packet, "claude", "claude-sdk-drift")
        )
        context["permission_mode"] = "bypassPermissions"
        version_probe = _runner_authorization_subprocess.CompletedProcess([], 0, stdout="2.1.233 (Claude Code)\n", stderr="")
        with (
            _runner_authorization_mock.patch("agent_bridge_connect.runner.assert_claude_sdk_environment", return_value={"sdk_version": "0.2.142", "platform": "macOS arm64", "cli_path": str(self.binaries["claude"])}),
            _runner_authorization_mock.patch("agent_bridge_connect.runner.subprocess.run", return_value=version_probe),
            self.assertRaisesRegex(_runner_authorization_RunnerError, "permission_transport_mismatch"),
        ):
            self.state.authorize_transport("claude", _runner_authorization_CLAUDE_SDK_CONTROL_AUTHORIZATION, session["project_path"], packet, context, "claude-sdk-drift")

    def test_runner_accepts_plan_d_claude_sdk_context_without_retired_paths(self) -> None:
        service = _runner_authorization_TaskService(self.root / "sdk-plan-d-board", config={"workspace_root": str(self.root / "sdk-plan-d-workspace"), "executors": {"claude": {"max_budget_usd": 10.0}}, "sessions": {"retain_executor_sessions": False}})
        task = service.create_task("SDK Plan D context", "claude", [{"id": 1, "description": "accept native SDK context"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        packet = service.store.read_task(task.id)
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(service.board_root)}
        packet["runner_authorization_required"] = True
        session = packet["extensions"]["agentbc.session"]
        executor = _runner_authorization_ClaudeExecutor(command=str(self.binaries["claude"]))
        sdk_facts = {"sdk_version": "0.2.142", "platform": "macOS arm64", "cli_path": str(self.binaries["claude"])}
        context = executor._build_sdk_authorization_context(packet, _runner_authorization_Path(session["project_path"]), session["session_id"], sdk_facts, _runner_authorization_resolve_effective_permission(packet, "claude", "claude-sdk-plan-d"))
        _runner_authorization_Path(session["project_path"]).mkdir(parents=True, exist_ok=True)
        self.state.allowed_roots.append(_runner_authorization_Path(session["project_path"]).resolve())
        with _runner_authorization_mock.patch("agent_bridge_connect.runner.assert_claude_sdk_environment", return_value=sdk_facts):
            result = self.state.authorize_transport("claude", _runner_authorization_CLAUDE_SDK_CONTROL_AUTHORIZATION, session["project_path"], packet, context, "claude-sdk-plan-d")
        self.assertEqual(result["effective_permission_mode"], "safe")
        self.assertEqual(context["settings_json"], "")
        self.assertEqual(context["additional_dirs"], [])

    def test_runner_treats_registered_first_hermes_acp_run_as_fresh(self) -> None:
        service = _runner_authorization_TaskService(self.root / "hermes-fresh-board", config={"workspace_root": str(self.root / "hermes-fresh-workspace"), "executors": {"hermes": {"max_turns": 90}}, "sessions": {"retain_executor_sessions": False}})
        task = service.create_task("Hermes fresh ACP", "hermes", [{"id": 1, "description": "start one fresh ACP session"}], customer_dir=True, customer_path=self.project, permission_mode="inherit")
        run_id = f"hermes-{task.id}-fresh"
        service.record_executor_run_started(task.id, run_id)
        persisted = service.store.read_task(task.id)
        self.state._validate_phase3_execution_command("hermes", [str(self.binaries["hermes"]), "acp"], self.project, persisted, run_id)
        self.assertFalse(persisted["extensions"]["agentbc.session"]["run_resume_facts"][run_id])

    def test_preconsume_dispatch_failure_calls_core_revoke_helper(self) -> None:
        (_service, packet, _source, _session) = self._grant_packet("codex")
        task = _runner_authorization_SimpleNamespace(id=packet["task_id"], assignee="codex", extensions=packet["extensions"], to_dict=lambda: packet)
        fake_service = _runner_authorization_mock.Mock()
        fake_service.respond_to_input.return_value = {"dispatch_required": True, "input_id": "input-codex-phase6"}
        fake_service.expire_waiting_inputs.return_value = []
        fake_service.get_task.return_value = task
        fake_service.board_root = self.board
        with (
            _runner_authorization_mock.patch("agent_bridge_connect.service.TaskService", return_value=fake_service),
            _runner_authorization_mock.patch.object(self.state, "dispatch_worker", side_effect=_runner_authorization_RunnerError("preconsume dispatch failed")),
            _runner_authorization_mock.patch("agent_bridge_connect.reports.write_report_files"),
            _runner_authorization_mock.patch("agent_bridge_connect.notifications.notify_terminal"),
            _runner_authorization_mock.patch.object(self.state, "_refresh_task_list_dashboard"),
            self.assertRaisesRegex(_runner_authorization_RunnerError, "preconsume dispatch failed"),
        ):
            self.state.respond_and_dispatch({"board_root": str(self.board), "task_id": packet["task_id"], "input_id": "input-codex-phase6", "response_type": "approve"})
        fake_service.revoke_permission_grant.assert_called_once_with(packet["task_id"], "dispatch_failed")
        fake_service.mark_task_needs_recovery.assert_called_once()
