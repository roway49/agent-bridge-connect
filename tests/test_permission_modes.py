from __future__ import annotations

import contextlib
import io
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.cli import build_parser
from agent_bridge_connect.permission_modes import (
    CANONICAL_PERMISSION_MODES,
    PERMISSION_EXTENSION_KEY,
    assert_executor_permission_supported,
    build_permission_record,
    configured_permission_mode,
    legacy_permission_record,
    permission_flags,
    validate_permission_command,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService, task_to_status


class PermissionContractTests(unittest.TestCase):
    def test_contract_has_exactly_three_modes_and_safe_legacy_fallback(self) -> None:
        self.assertEqual(CANONICAL_PERMISSION_MODES, ("inherit", "safe", "full"))
        self.assertEqual(configured_permission_mode({}), ("safe", "safe_default"))
        self.assertEqual(legacy_permission_record()["effective_mode"], "safe")

    def test_explicit_mode_overrides_configured_default(self) -> None:
        self.assertEqual(
            build_permission_record(explicit_mode="inherit", config={"permission_mode": "full"}),
            {
                "requested_mode": "inherit",
                "effective_mode": "inherit",
                "selection_source": "explicit_task",
            },
        )
        self.assertEqual(
            build_permission_record(config={"permission_mode": "full"})["selection_source"],
            "configured_default",
        )

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ABCError, "Unknown permission mode"):
            build_permission_record(explicit_mode="root")

    def test_cli_uses_one_consistent_task_option(self) -> None:
        parser = build_parser()
        create = parser.parse_args(
            [
                "task",
                "create",
                "--title",
                "x",
                "--assignee",
                "codex",
                "--steps",
                "/tmp/steps.yaml",
                "--permission-mode",
                "inherit",
            ]
        )
        handoff = parser.parse_args(
            ["task", "handoff", "ABCD-001", "--to", "claude", "--permission-mode", "full"]
        )
        setup = parser.parse_args(["setup", "--non-interactive", "--permission-mode", "safe"])
        self.assertEqual(create.permission_mode, "inherit")
        self.assertEqual(handoff.permission_mode, "full")
        self.assertEqual(setup.permission_mode, "safe")

    def test_setup_selection_explains_modes_and_never_defaults_to_full(self) -> None:
        from agent_bridge_connect.setup import _select_permission_mode

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            selected = _select_permission_mode({}, explicit_mode=None, interactive=False)
        self.assertEqual(selected, "safe")
        text = output.getvalue()
        self.assertIn("existing user/global", text)
        self.assertIn("maximum documented noninteractive access", text)
        self.assertIn("WARNING: full", text)

    def test_atomic_create_and_handoff_requests_carry_permission_mode(self) -> None:
        from agent_bridge_connect.runner import RunnerClient

        client = RunnerClient(spool_root="/tmp/agentbc-permission-test-spool")
        with mock.patch.object(client, "_request", side_effect=lambda payload: payload):
            created = client.create_and_dispatch(
                "title",
                "codex",
                [{"id": 1, "description": "run"}],
                "/tmp/board",
                None,
                customer_path="default path",
                permission_mode="inherit",
            )
            handed = client.handoff_and_dispatch(
                "ABCD-001",
                "hermes",
                "continue",
                "/tmp/board",
                None,
                permission_mode="full",
            )
        self.assertEqual(created["permission_mode"], "inherit")
        self.assertEqual(handed["permission_mode"], "full")


class TaskPermissionPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self, mode: str | None = None) -> TaskService:
        config = {"workspace_root": str(self.root)}
        if mode is not None:
            config["permission_mode"] = mode
        return TaskService(self.board, config=config)

    def _create(self, service: TaskService, mode: str | None = None):
        return service.create_task(
            "permission persistence",
            "codex",
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode=mode,
        )

    def test_task_persists_requested_effective_and_precedence(self) -> None:
        service = self._service("inherit")
        inherited_default = self._create(service)
        explicit = self._create(service, "full")
        self.assertEqual(
            inherited_default.extensions[PERMISSION_EXTENSION_KEY]["effective_mode"], "inherit"
        )
        self.assertEqual(
            explicit.extensions[PERMISSION_EXTENSION_KEY],
            {
                "requested_mode": "full",
                "effective_mode": "full",
                "selection_source": "explicit_task",
            },
        )

    def test_legacy_task_cannot_gain_full_from_new_config(self) -> None:
        service = self._service("safe")
        task = self._create(service)
        raw = task.to_dict()
        raw["extensions"].pop(PERMISSION_EXTENSION_KEY)
        service.store.write_task(task.id, raw)

        restarted = self._service("full")
        persisted = restarted.ensure_task_permission(task.id)
        self.assertEqual(
            persisted.extensions[PERMISSION_EXTENSION_KEY], legacy_permission_record()
        )

    def test_handoff_inherits_unless_explicitly_overridden(self) -> None:
        service = self._service("safe")
        source = self._create(service, "inherit")
        source.status = "completed"
        source.steps[0]["status"] = "done"
        service.store.write_task(source.id, source.to_dict())

        inherited = service.handoff_task(source.id, "claude", "continue")
        self.assertEqual(
            inherited.extensions[PERMISSION_EXTENSION_KEY]["effective_mode"], "inherit"
        )
        self.assertEqual(
            inherited.extensions[PERMISSION_EXTENSION_KEY]["selection_source"], "inherited_task"
        )
        inherited.status = "completed"
        inherited.steps[0]["status"] = "done"
        service.store.write_task(inherited.id, inherited.to_dict())
        overridden = service.handoff_task(inherited.id, "hermes", "continue", permission_mode="safe")
        self.assertEqual(
            overridden.extensions[PERMISSION_EXTENSION_KEY]["selection_source"], "explicit_task"
        )

    def test_retry_recovery_and_status_preserve_permission(self) -> None:
        service = self._service("safe")
        task = self._create(service, "inherit")
        expected = dict(task.extensions[PERMISSION_EXTENSION_KEY])
        task.status = "running"
        task.steps[0]["status"] = "failed"
        service.store.write_task(task.id, task.to_dict())
        service.retry_step(task.id, 1)
        self.assertEqual(
            service.get_task(task.id).extensions[PERMISSION_EXTENSION_KEY], expected
        )
        status = task_to_status(service.get_task(task.id))
        self.assertEqual(status["extensions"][PERMISSION_EXTENSION_KEY], expected)

        recovery = self._create(service, "inherit")
        recovery_expected = dict(recovery.extensions[PERMISSION_EXTENSION_KEY])
        self.assertTrue(
            service.mark_task_needs_recovery(
                recovery.id, "test_recovery", "recover", {"executor": "codex"}
            )
        )
        service.requeue_task(recovery.id)
        self.assertEqual(
            service.get_task(recovery.id).extensions[PERMISSION_EXTENSION_KEY],
            recovery_expected,
        )

    def test_input_required_response_preserves_permission(self) -> None:
        service = self._service("safe")
        task = service.create_task(
            "input permission preservation",
            "codex",
            [{"id": 1, "description": "blocked"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="inherit",
        )
        service.start_task_run(task.id, "codex")
        current = service.get_task(task.id)
        execution = current.extensions["agentbc.execution"]
        run_id = str(execution.get("executor_run_id") or "codex-input-test")
        callback = {
            "version": 1,
            "task_id": task.id,
            "final_state": "input_required",
            "summary": "need approval",
            "executor_run_id": run_id,
            "input": {
                "type": "permission",
                "requested_permission": "full",
                "reason": "The next continuation requires temporary full permission.",
            },
            "step_results": [{"id": 1, "status": "blocked"}],
        }
        service.update_execution_metadata(task.id, {"executor_run_id": run_id})
        self.assertTrue(
            service.finalize_task_from_executor_exit(
                task.id, executor_run_id=run_id, callback=callback
            )
        )
        waiting = service.get_task(task.id)
        expected = dict(waiting.extensions[PERMISSION_EXTENSION_KEY])
        request = waiting.extensions["agentbc.input"]
        service.respond_to_input(
            task.id,
            request["input_id"],
            response_type="approve",
            message="",
        )
        self.assertEqual(
            service.get_task(task.id).extensions[PERMISSION_EXTENSION_KEY], expected
        )


class ExecutorPermissionMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _record(mode: str) -> dict[str, str]:
        return build_permission_record(explicit_mode=mode)

    def test_all_three_executor_mappings(self) -> None:
        self.assertEqual(permission_flags("codex", "inherit"), [])
        self.assertEqual(permission_flags("codex", "safe"), ["--sandbox", "workspace-write"])
        self.assertEqual(
            permission_flags("codex", "full"),
            ["--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertEqual(permission_flags("claude", "inherit"), [])
        self.assertEqual(
            permission_flags("claude", "safe"),
            ["--safe-mode", "--permission-mode", "acceptEdits"],
        )
        self.assertEqual(permission_flags("claude", "full"), ["--dangerously-skip-permissions"])
        self.assertEqual(permission_flags("hermes", "inherit"), [])
        self.assertEqual(permission_flags("hermes", "safe"), [])
        self.assertEqual(permission_flags("hermes", "full"), ["--yolo"])

    def test_inherit_omits_permission_and_writable_root_overrides(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor
        from agent_bridge_connect.executors.codex import CodexExecutor
        from agent_bridge_connect.executors.hermes import HermesExecutor

        packet = {
            "extensions": {PERMISSION_EXTENSION_KEY: self._record("inherit")},
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
        }
        codex, _ = CodexExecutor(command=sys.executable)._build_command(
            packet, "prompt", self.root
        )
        claude = ClaudeExecutor(command=sys.executable)._build_command(
            "prompt", self.root, packet
        )
        hermes = HermesExecutor(command=sys.executable, transport="direct")._build_command(
            "prompt", permission=self._record("inherit")
        )
        combined = " ".join([*codex, *claude, *hermes])
        for forbidden in (
            "--sandbox",
            "--safe-mode",
            "--permission-mode",
            "--yolo",
            "--dangerously-skip-permissions",
            "--dangerously-bypass-approvals-and-sandbox",
            "--add-dir",
        ):
            self.assertNotIn(forbidden, combined)

    def test_safe_keeps_existing_codex_and_claude_behavior(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor
        from agent_bridge_connect.executors.codex import CodexExecutor

        packet = {
            "extensions": {PERMISSION_EXTENSION_KEY: self._record("safe")},
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
        }
        codex, _ = CodexExecutor(command=sys.executable)._build_command(
            packet, "prompt", self.root
        )
        claude = ClaudeExecutor(command=sys.executable)._build_command(
            "prompt", self.root, packet
        )
        self.assertIn("workspace-write", codex)
        self.assertIn("--add-dir", codex)
        self.assertIn("--safe-mode", claude)
        self.assertIn("acceptEdits", claude)

    def test_unsupported_full_capability_fails_closed(self) -> None:
        completed = mock.Mock(returncode=0, stdout="usage without full flag", stderr="")
        with mock.patch("agent_bridge_connect.permission_modes.subprocess.run", return_value=completed):
            with self.assertRaises(ABCError) as raised:
                assert_executor_permission_supported("codex", "full", sys.executable)
        self.assertEqual(raised.exception.code, "unsupported_permission_mode")


class CanonicalPermissionArgumentTests(unittest.TestCase):
    @staticmethod
    def _record(mode: str) -> dict[str, str]:
        return build_permission_record(explicit_mode=mode)

    def assertAccepted(self, executor: str, mode: str, command: list[str]) -> None:
        validate_permission_command(executor, command, self._record(mode))

    def assertRejected(self, executor: str, mode: str, command: list[str]) -> ABCError:
        with self.assertRaises(ABCError) as raised:
            validate_permission_command(executor, command, self._record(mode))
        self.assertEqual(raised.exception.code, "unsupported_permission_mode")
        return raised.exception

    def test_codex_short_long_equals_and_attached_sandbox_forms_are_canonical(self) -> None:
        base = ["codex", "exec", "--json"]
        for arguments in (
            ["--sandbox", "workspace-write"],
            ["-s", "workspace-write"],
            ["--sandbox=workspace-write"],
            ["-s=workspace-write"],
            ["-sworkspace-write"],
        ):
            with self.subTest(arguments=arguments):
                self.assertAccepted("codex", "safe", [*base, *arguments])

        # This is the original Runner bypass from the review finding.
        self.assertRejected(
            "codex", "inherit", [*base, "-s", "danger-full-access"]
        )
        self.assertRejected(
            "codex", "full", [*base, "--sandbox=danger-full-access"]
        )
        self.assertAccepted(
            "codex",
            "full",
            [*base, "--dangerously-bypass-approvals-and-sandbox"],
        )

    def test_codex_raw_duplicate_conflicting_and_config_overrides_fail_closed(self) -> None:
        base = ["codex", "exec", "--json"]
        for mode, arguments in (
            ("safe", ["--sandbox", "workspace-write", "-s=workspace-write"]),
            (
                "full",
                [
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--dangerously-bypass-approvals-and-sandbox",
                ],
            ),
            ("inherit", ["-a", "never"]),
            ("inherit", ["--ask-for-approval=never"]),
            ("inherit", ["--dangerously-bypass-hook-trust"]),
            ("inherit", ["--ignore-user-config"]),
            ("safe", ["--sandbox=workspace-write", "--ignore-rules"]),
            ("inherit", ["-p", "unsafe-profile"]),
            ("inherit", ["--profile=unsafe-profile"]),
        ):
            with self.subTest(mode=mode, arguments=arguments):
                self.assertRejected("codex", mode, [*base, *arguments])

        secret = 'approval_policy="never-with-secret-token"'
        error = self.assertRejected("codex", "inherit", [*base, "-c", secret])
        self.assertNotIn(secret, str(error.details))
        self.assertIn("<redacted>", str(error.details))

    def test_claude_long_equals_duplicate_and_conflicting_forms_fail_closed(self) -> None:
        base = ["claude", "-p"]
        self.assertAccepted(
            "claude",
            "safe",
            [*base, "--permission-mode=acceptEdits", "--safe-mode"],
        )
        self.assertAccepted(
            "claude", "full", [*base, "--dangerously-skip-permissions"]
        )
        for mode, arguments in (
            ("inherit", ["--permission-mode=bypassPermissions"]),
            ("inherit", ["--dangerously-skip-permissions"]),
            ("inherit", ["--allow-dangerously-skip-permissions"]),
            ("inherit", ["--safe-mode"]),
            ("inherit", ["--bare"]),
            ("inherit", ["--setting-sources=user"]),
            ("inherit", ["--settings", '{"permissions": {"allow": ["Bash"]}}']),
            ("full", ["--permission-mode", "bypassPermissions"]),
            ("full", ["--dangerously-skip-permissions=true"]),
            (
                "safe",
                [
                    "--safe-mode",
                    "--safe-mode",
                    "--permission-mode",
                    "acceptEdits",
                ],
            ),
            (
                "full",
                [
                    "--dangerously-skip-permissions",
                    "--permission-mode=bypassPermissions",
                ],
            ),
        ):
            with self.subTest(mode=mode, arguments=arguments):
                self.assertRejected("claude", mode, [*base, *arguments])

    def test_hermes_modes_reject_yolo_aliases_and_customization_bypasses(self) -> None:
        base = ["hermes", "chat", "-q", "prompt"]
        self.assertAccepted("hermes", "inherit", base)
        self.assertAccepted("hermes", "safe", base)
        self.assertAccepted("hermes", "full", [*base, "--yolo"])
        for mode, arguments in (
            ("inherit", ["--yolo"]),
            ("safe", ["--yolo"]),
            ("full", ["--yolo=true"]),
            ("full", ["--yolo", "--yolo"]),
            ("inherit", ["-z", "prompt"]),
            ("safe", ["-z=prompt"]),
            ("safe", ["-zprompt"]),
            ("inherit", ["--oneshot=prompt"]),
            ("inherit", ["--accept-hooks"]),
            ("inherit", ["--ignore-user-config"]),
            ("safe", ["--ignore-rules"]),
            ("safe", ["--safe-mode"]),
            ("full", ["--yolo", "--accept-hooks"]),
        ):
            with self.subTest(mode=mode, arguments=arguments):
                self.assertRejected("hermes", mode, [*base, *arguments])


class ClaudePermissionDiagnosticTests(unittest.TestCase):
    def test_probe_not_found_has_stable_path_diagnostics(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        missing = {
            "found": False,
            "path": "",
            "source": "not_found",
            "searched_paths": ["fixture/claude"],
            "manual_override": "AGENTBC_CLAUDE_BIN=/your/path/claude",
        }
        with mock.patch(
            "agent_bridge_connect.executors.claude._discover_claude_binary",
            return_value=missing,
        ):
            result = ClaudeExecutor().probe()

        self.assertFalse(result.ok)
        self.assertEqual(result.details["agent_bin"], "")
        self.assertEqual(result.details["agent_bin_source"], "not_found")
        self.assertEqual(result.details["searched_paths"], ["fixture/claude"])
        self.assertIn("AGENTBC_CLAUDE_BIN", result.details["manual_override"])

    def test_setup_probe_reports_full_support_as_explicit_task_scoped(self) -> None:
        from agent_bridge_connect.setup import probe_claude

        completed = mock.Mock(
            returncode=0,
            stdout="--permission-mode <mode> --dangerously-skip-permissions",
            stderr="",
        )
        with (
            mock.patch(
                "agent_bridge_connect.setup.discover_claude",
                return_value={"found": True, "path": sys.executable, "version": "test"},
            ),
            mock.patch("agent_bridge_connect.setup._run_command", return_value=completed),
        ):
            capabilities = probe_claude()
        self.assertTrue(capabilities["dangerous_permissions_supported"])
        self.assertEqual(
            capabilities["dangerous_permissions_policy"],
            "explicit_persisted_full_task_only",
        )
        self.assertNotIn("dangerous_permissions_blocked", capabilities)

    def test_executor_diagnostics_separate_config_capability_and_task_authority(self) -> None:
        from agent_bridge_connect.executors.claude import ClaudeExecutor

        executor = ClaudeExecutor(command=sys.executable, transport="direct")
        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="claude 2", stderr=""),
        ):
            probe = executor.probe()
        self.assertEqual(probe.details["agent_bin_source"], "configured")
        self.assertIsNone(probe.details["dangerous_permissions_allowed"])
        self.assertEqual(
            probe.details["dangerous_permissions_policy"],
            "explicit_persisted_full_task_only",
        )
        executor._last_run_id = "full-run"
        executor._task_packets["full-run"] = {
            "extensions": {
                PERMISSION_EXTENSION_KEY: build_permission_record(explicit_mode="full")
            }
        }
        executor._run_metadata["full-run"] = {"run_id": "full-run"}
        metadata = executor.get_extensions()["executor"]["claude"]
        self.assertEqual(metadata["task_permission"]["requested_mode"], "full")
        self.assertEqual(metadata["task_permission"]["effective_mode"], "full")
        self.assertEqual(metadata["task_permission"]["selection_source"], "explicit_task")
        self.assertTrue(metadata["dangerous_permissions_supported"])
        self.assertTrue(metadata["dangerous_permissions_allowed"])
        self.assertEqual(metadata["configured_permission_mode"], "acceptEdits")
        self.assertNotIn("permission_mode", metadata)


class RunnerPermissionAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_hermes = self.root / "hermes"
        self.fake_hermes.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
        self.fake_hermes.chmod(self.fake_hermes.stat().st_mode | stat.S_IXUSR)
        self.fake_codex = self.root / "codex"
        self.fake_codex.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
        self.fake_codex.chmod(self.fake_codex.stat().st_mode | stat.S_IXUSR)
        from agent_bridge_connect.runner import RunnerState

        self.state = RunnerState(
            self.root / "runner",
            [self.root],
            {"hermes": self.fake_hermes, "codex": self.fake_codex},
        )
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _packet(self, mode: str, executor: str = "hermes"):
        service = TaskService(self.board, config={"workspace_root": str(self.root)})
        task = service.create_task(
            "runner permission",
            executor,
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode=mode,
        )
        packet = task.to_dict()
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(self.board)}
        return service, task, packet

    def test_runner_rejects_original_codex_short_sandbox_bypass(self) -> None:
        from agent_bridge_connect.runner import RunnerError

        _service, _task, inherit = self._packet("inherit", "codex")
        command = [
            str(self.fake_codex),
            "exec",
            "--json",
            "-s",
            "danger-full-access",
            "prompt",
        ]
        with (
            mock.patch.object(self.state, "_spawn_process") as spawn,
            self.assertRaisesRegex(RunnerError, "do not match"),
        ):
            self.state.submit("codex", command, str(self.project), inherit)
        spawn.assert_not_called()

    def test_missing_mismatched_and_raw_dangerous_authorization_are_rejected(self) -> None:
        from agent_bridge_connect.runner import RunnerError

        command = [str(self.fake_hermes), "chat", "-q", "prompt"]
        with self.assertRaisesRegex(RunnerError, "missing persisted"):
            self.state.submit("hermes", command, str(self.project))
        _service, _task, safe = self._packet("safe")
        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.submit("hermes", [*command, "--yolo"], str(self.project), safe)
        injected = {**safe, "extensions": dict(safe["extensions"])}
        injected["extensions"][PERMISSION_EXTENSION_KEY] = self._packet("full")[2][
            "extensions"
        ][PERMISSION_EXTENSION_KEY]
        with self.assertRaisesRegex(RunnerError, "stale or command-injected"):
            self.state.submit("hermes", command, str(self.project), injected)

    def test_explicit_full_is_authorized_without_executing_command(self) -> None:
        _service, _task, full = self._packet("full")
        command = [
            str(self.fake_hermes),
            "chat",
            "--yolo",
            "-q",
            "--max-turns",
            "90",
            "prompt",
        ]
        spawned = {"ok": True, "run_id": "mock-full", "pid": 1, "status": "running"}
        with mock.patch.object(self.state, "_spawn_process", return_value=spawned) as run:
            result = self.state.submit("hermes", command, str(self.project), full)
        self.assertEqual(result["run_id"], "mock-full")
        run.assert_called_once()

    def test_full_does_not_bypass_task_scoped_cwd_check(self) -> None:
        from agent_bridge_connect.runner import RunnerError

        _service, _task, full = self._packet("full")
        command = [str(self.fake_hermes), "chat", "--yolo", "-q", "prompt"]
        with self.assertRaisesRegex(RunnerError, "outside allowed roots"):
            self.state.submit("hermes", command, self.root.anchor, full)

    def test_dispatch_records_redacted_permission_audit_and_report(self) -> None:
        from agent_bridge_connect.reports import generate_report, generate_report_md

        service, task, _packet = self._packet("safe")
        spawned = {"ok": True, "run_id": "worker", "pid": 2, "status": "running"}
        with mock.patch.object(self.state, "_spawn_process", return_value=spawned):
            self.state.dispatch_worker(task.id, "hermes", str(self.board), "", 0.2, False)
        events = service.store.read_events(task.id)
        audit = next(event for event in events if event.get("event_type") == "permission_audit")
        self.assertEqual(audit["effective_mode"], "safe")
        self.assertNotIn("command", audit)
        report = generate_report(task.id, self.board)
        markdown = generate_report_md(task.id, self.board)
        self.assertEqual(report["permission"]["requested_mode"], "safe")
        self.assertIn("Requested permission mode: `safe`", markdown)

    def test_permission_mode_does_not_change_final_marker_validation(self) -> None:
        from agent_bridge_connect.execution_contract import validate_callback_payload

        _service, task, _packet = self._packet("full")
        callback = {
            "version": 1,
            "task_id": task.id,
            "final_state": "completed",
            "summary": "done",
            "step_results": [{"id": 1, "status": "done"}],
        }
        validation = validate_callback_payload(callback, task.id, task.steps)
        self.assertTrue(validation.valid)


if __name__ == "__main__":
    unittest.main()
