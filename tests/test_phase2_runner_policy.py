from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_policy import (
    RESOURCE_EXTENSION_KEY,
    SESSION_EXTENSION_KEY,
    build_resource_snapshot,
    build_session_snapshot,
)
from agent_bridge_connect.runner import (
    PHASE2_AUDIT_EVENT_TYPE,
    PHASE2_EXECUTION_EXTENSION_KEY,
    PHASE2_EXECUTOR_PROJECT_ROOT_KEY,
    PHASE2_LEGACY_BACKFILLED_AT_KEY,
    PHASE2_LEGACY_BACKFILLED_KEY,
    PHASE2_LEGACY_UNRECORDED_KEY,
    RunnerError,
    RunnerState,
)
from agent_bridge_connect.task_store import TaskStore


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
        _write_script(self.fake_hermes)
        _write_script(self.fake_claude)
        self.board = self.root / "board"
        self.state = RunnerState(
            self.root / "state",
            [self.root],
            {"hermes": self.fake_hermes, "claude": self.fake_claude},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self):
        from agent_bridge_connect.service import TaskService

        return TaskService(self.board, config={"workspace_root": str(self.root)})

    def _create_task(self, executor: str) -> str:
        service = self._service()
        task = service.create_task(
            "Phase 2 policy test",
            executor,
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=self.root,
            permission_mode="safe",
        )
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

    def _attach_snapshots(self, task_id: str, resources: dict, session: dict) -> None:
        # Written directly so corrupt snapshots can be injected for fail-closed tests.
        task = self._read_task(task_id)
        extensions = dict(task.get("extensions") or {})
        extensions[RESOURCE_EXTENSION_KEY] = dict(resources)
        extensions[SESSION_EXTENSION_KEY] = dict(session)
        task["extensions"] = extensions
        TaskStore(self.board).write_task(task_id, task)

    def _make_native(self, task_id: str, executor: str) -> None:
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(self.root / "exec-project"))
        if executor == "claude":
            resources = build_resource_snapshot("claude", 10.0, source="config")
            session = build_session_snapshot(
                "claude",
                retain=False,
                session_state="pending",
                project_path=str(self.root / "claude-project"),
            )
        else:
            resources = build_resource_snapshot("hermes", 90, source="config")
            session = build_session_snapshot("hermes", retain=False, session_state="pending")
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
        return [
            event
            for event in self._audit_events(task_id)
            if event.get("event_type") == PHASE2_AUDIT_EVENT_TYPE
        ]

    def _dispatch(self, task_id: str, executor: str, resuming: bool = False) -> tuple[dict, mock.MagicMock]:
        fake_run = {"ok": True, "run_id": f"run-{task_id}", "pid": 4242, "status": "running"}
        with mock.patch.object(RunnerState, "_spawn_process", return_value=fake_run) as spawn:
            result = self.state.dispatch_worker(
                task_id, executor, str(self.board), "", 0.5, False, resuming=resuming
            )
        return result, spawn

    def _authorize(self, executor: str, command: list[str], packet: dict) -> dict:
        return self.state.authorize_command(executor, command, str(self.root), packet)

    def _hermes_command(self, *extra: str) -> list[str]:
        return [str(self.fake_hermes), "chat", "-q", *extra, "hello"]

    def _claude_command(self, *extra: str) -> list[str]:
        return [
            str(self.fake_claude),
            "-p",
            "--safe-mode",
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "text",
            *extra,
            "hello",
        ]

    # --- legacy backfill ---

    def test_legacy_claude_dispatch_backfills_canonical_snapshots_once(self) -> None:
        task_id = self._create_task("claude")
        task = self._read_task(task_id)
        self.assertNotIn(RESOURCE_EXTENSION_KEY, task.get("extensions") or {})

        result, spawn = self._dispatch(task_id, "claude")
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
        expected_prefix = (
            f"{workspace['agentbc_root']}/tasks/artifacts/{workspace['task_date']}"
            f"/{workspace['task_code']}/{task_id}-"
        )
        self.assertTrue(session["project_path"].startswith(expected_prefix), session["project_path"])
        uuid_tail = session["project_path"][len(expected_prefix):]
        self.assertRegex(
            uuid_tail,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        execution = extensions[PHASE2_EXECUTION_EXTENSION_KEY]
        self.assertTrue(execution[PHASE2_LEGACY_BACKFILLED_KEY])
        first_backfilled_at = execution[PHASE2_LEGACY_BACKFILLED_AT_KEY]
        self.assertEqual(len(self._policy_audits(task_id)), 1)
        self.assertEqual(self._policy_audits(task_id)[0]["outcome"], "backfilled")

        # The Runner does not inject executor resource flags into the worker command.
        worker_command = spawn.call_args.args[1]
        for flag in ("--max-budget-usd", "--max-turns", "--session-id", "--resume"):
            self.assertNotIn(flag, worker_command)

        # Re-dispatch backfills only once: snapshots and the backfill receipt stay stable.
        self._dispatch(task_id, "claude")
        refreshed_again = self._read_task(task_id)
        self.assertEqual(
            refreshed_again["extensions"][RESOURCE_EXTENSION_KEY],
            resources,
        )
        self.assertEqual(
            refreshed_again["extensions"][SESSION_EXTENSION_KEY],
            session,
        )
        self.assertEqual(
            refreshed_again["extensions"][PHASE2_EXECUTION_EXTENSION_KEY][PHASE2_LEGACY_BACKFILLED_AT_KEY],
            first_backfilled_at,
        )
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

    def test_legacy_backfill_never_reads_user_configuration(self) -> None:
        task_id = self._create_task("claude")
        hostile_config = {
            "workspace_root": str(self.root),
            "executors": {
                "claude": {"max_budget_usd": 25.0},
                "hermes": {"max_turns": 200},
            },
            "sessions": {"retain_executor_sessions": True},
        }
        with mock.patch("agent_bridge_connect.config.load_config", return_value=hostile_config):
            self._dispatch(task_id, "claude")
        resources = self._read_task(task_id)["extensions"][RESOURCE_EXTENSION_KEY]
        self.assertEqual(resources["configured_limit"], 10.0)
        self.assertEqual(resources["source"], "legacy_default_10")

    def test_resuming_legacy_task_is_backfilled_on_redispatch(self) -> None:
        task_id = self._create_task("hermes")
        execution = dict((self._read_task(task_id).get("extensions") or {}).get(PHASE2_EXECUTION_EXTENSION_KEY) or {})
        execution["internal_status"] = "resuming"
        self._write_task(
            task_id,
            {"status": "running", "extensions": {**self._read_task(task_id)["extensions"], PHASE2_EXECUTION_EXTENSION_KEY: execution}},
        )
        result, _spawn = self._dispatch(task_id, "hermes", resuming=True)
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

        # A second attempt must not add snapshots or duplicate the marker write.
        marked_at = task["updated_at"]
        with self.assertRaisesRegex(RunnerError, "not pending"):
            self._dispatch(task_id, "claude")
        task_again = self._read_task(task_id)
        self.assertNotIn(RESOURCE_EXTENSION_KEY, task_again.get("extensions") or {})
        self.assertEqual(task_again["updated_at"], marked_at)

    # --- native Phase 2 tasks ---

    def test_native_task_missing_snapshots_fails_closed_without_legacy_fix(self) -> None:
        task_id = self._create_task("claude")
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(self.root / "exec-project"))
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
        result, _spawn = self._dispatch(task_id, "claude")
        self.assertEqual(result["dispatch_status"], "accepted")

    def test_native_task_executor_mismatch_fails_closed(self) -> None:
        task_id = self._create_task("claude")
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(self.root / "exec-project"))
        self._attach_snapshots(
            task_id,
            build_resource_snapshot("hermes", 90, source="config"),
            build_session_snapshot("hermes", retain=False, session_state="pending"),
        )
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._dispatch(task_id, "claude")
        audits = self._policy_audits(task_id)
        self.assertEqual(audits[0]["outcome"], "fail")

    def test_native_task_corrupt_snapshot_fails_closed(self) -> None:
        task_id = self._create_task("hermes")
        self._make_native(task_id, "hermes")
        resources = dict(self._read_task(task_id)["extensions"][RESOURCE_EXTENSION_KEY])
        resources["version"] = 99
        self._attach_snapshots(
            task_id,
            resources,
            self._read_task(task_id)["extensions"][SESSION_EXTENSION_KEY],
        )
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._dispatch(task_id, "hermes")

    # --- authorize_command packet-vs-disk consistency ---

    def test_authorize_consistent_native_packet_passes(self) -> None:
        task_id = self._create_task("claude")
        self._make_native(task_id, "claude")
        packet = self._packet(task_id)
        command = self._claude_command("--max-budget-usd", "10", "--session-id", "abc", "--resume", "abc")
        result = self._authorize("claude", command, packet)
        self.assertTrue(result["authorized"])

        hermes_id = self._create_task("hermes")
        self._make_native(hermes_id, "hermes")
        result = self._authorize("hermes", self._hermes_command("--max-turns", "90"), self._packet(hermes_id))
        self.assertTrue(result["authorized"])

    def test_authorize_does_not_validate_or_inject_resource_flags(self) -> None:
        task_id = self._create_task("hermes")
        self._make_native(task_id, "hermes")
        packet = self._packet(task_id)
        result = self._authorize("hermes", self._hermes_command(), packet)
        self.assertTrue(result["authorized"])
        # Phase 2 never injects executor resource flags into the command.
        result = self._authorize("hermes", self._hermes_command("--max-turns", "90"), packet)
        self.assertTrue(result["authorized"])
        self.assertEqual(self._policy_audits(task_id), [])

    def test_authorize_backfilled_legacy_packet_passes(self) -> None:
        task_id = self._create_task("hermes")
        self._dispatch(task_id, "hermes")
        result = self._authorize("hermes", self._hermes_command(), self._packet(task_id))
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
        packet["extensions"][RESOURCE_EXTENSION_KEY] = build_resource_snapshot(
            "hermes", 90, source="config"
        )
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
        # The disk record advanced after the packet was built (stale authorization).
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
        self._inject_workspace_key(task_id, PHASE2_EXECUTOR_PROJECT_ROOT_KEY, str(self.root / "exec-project"))
        packet = self._packet(task_id)
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._authorize("claude", self._claude_command(), packet)
        # authorize never auto-fixes legacy-style snapshots into a native task.
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
        self._attach_snapshots(
            task_id,
            resources,
            self._read_task(task_id)["extensions"][SESSION_EXTENSION_KEY],
        )
        with self.assertRaisesRegex(RunnerError, "invalid_execution_policy"):
            self._authorize("hermes", self._hermes_command(), self._packet(task_id))

    # --- audit sanitization ---

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


if __name__ == "__main__":
    unittest.main()
