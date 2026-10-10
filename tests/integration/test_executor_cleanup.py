from __future__ import annotations
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.adapters import SessionCleanupRequest
from agent_bridge_connect.executors.codex import CODEX_CLEANUP_UNSUPPORTED_CODE, CODEX_SESSION_DELETE_INVALID_ID_CODE, CodexExecutor, _CODEX_FROZEN_VERSION, _codex_session_cleanup_capability
from agent_bridge_connect.executors.hermes import (
    HERMES_CLEANUP_UNSUPPORTED_CODE,
    HERMES_SESSION_DELETE_FAILED_CODE,
    HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE,
    HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE,
    HermesExecutor,
    _HERMES_FROZEN_VERSION,
    _hermes_session_cleanup_capability,
    _version_number_matches,
)
import copy as _claude_cleanup_copy
import os as _claude_cleanup_os
import subprocess as _claude_cleanup_subprocess
import tempfile as _claude_cleanup_tempfile
import unittest as _claude_cleanup_unittest
from pathlib import Path as _claude_cleanup_Path
from unittest import mock as _claude_cleanup_mock
from agent_bridge_connect.adapters import SessionCleanupRequest as _claude_cleanup_SessionCleanupRequest
from agent_bridge_connect.executors.claude import CLAUDE_PROJECT_PURGE_TIMEOUT_S as _claude_cleanup_CLAUDE_PROJECT_PURGE_TIMEOUT_S, ClaudeExecutor as _claude_cleanup_ClaudeExecutor
from agent_bridge_connect.path_model import build_path_plan as _claude_cleanup_build_path_plan

FIXTURES = Path(__file__).parents[1] / "fixtures" / "executor_runtime"
CODEX_FIXTURE = FIXTURES / "matrix" / "codex" / "0.146.0" / "delete_help.txt"
HERMES_FIXTURE = FIXTURES / "matrix" / "hermes" / "0.17.0" / "help.txt"
CODEX_SESSION_ID = "019fef10-2f46-7c40-90c8-6d6ebd3cc7d6"
HERMES_SESSION_ID = "20260811_004323_d3bd9b"
HERMES_VERSION_OUTPUT = "Hermes Agent v0.17.0 (2026.6.19) · upstream 2cdb30a4\n"


def _request(**overrides: object) -> SessionCleanupRequest:
    values: dict[str, object] = {"executor": "codex", "session_id": CODEX_SESSION_ID, "task_id": "F5AH-001", "strategy": "official_session_delete", "receipt_source": "jsonl_thread_started", "official_receipt_bound": True}
    values.update(overrides)
    return SessionCleanupRequest(**values)


class FrozenFixtureTests(unittest.TestCase):
    def test_codex_frozen_help_fixture_pins_exact_force_uuid_delete(self) -> None:
        text = CODEX_FIXTURE.read_text(encoding="utf-8")
        self.assertIn(f"codex-cli {_CODEX_FROZEN_VERSION}", text)
        self.assertIn("Usage: codex delete [OPTIONS] <SESSION>", text)
        self.assertIn("Session id (UUID) or session name", text)
        self.assertIn("UUIDs take precedence if it parses", text)
        self.assertIn("--force", text)
        self.assertIn("SESSION must be a UUID", text)
        self.assertNotIn("--last", text)
        self.assertNotIn("picker", text)
        self.assertNotIn("sessions delete", text)

    def test_hermes_frozen_help_fixture_pins_exact_delete_entry(self) -> None:
        text = HERMES_FIXTURE.read_text(encoding="utf-8")
        self.assertIn(f"Hermes Agent v{_HERMES_FROZEN_VERSION}", text)
        self.assertIn("usage: hermes sessions delete [-h] [--yes] session_id", text)
        self.assertIn("session_id  Session ID to delete", text)
        self.assertIn("--yes, -y   Skip confirmation", text)


class CodexCapabilityProbeTests(unittest.TestCase):
    def test_frozen_fixture_probe_is_supported_by_force_uuid_contract(self) -> None:
        capability = _codex_session_cleanup_capability(CODEX_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "official_session_delete")
        self.assertEqual(capability.error_code, "")

    def test_misleading_same_name_delete_with_fuzzy_selector_is_rejected(self) -> None:
        help_text = "codex-cli 0.146.0\nPermanently delete a saved session by id or session name\nUsage: codex delete [OPTIONS] <SESSION>\nArguments:\n  <SESSION>\n          Session id (UUID) or session name. UUIDs take precedence if it parses\n"
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, CODEX_CLEANUP_UNSUPPORTED_CODE)

    def test_resume_last_and_picker_entries_are_rejected(self) -> None:
        help_text = "codex-cli 0.146.0\nUsage: codex resume [OPTIONS] [SESSION_ID] [PROMPT]\nOptions:\n      --last\n          Continue the most recent session without showing the picker\n"
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_archive_entry_is_not_a_delete_capability(self) -> None:
        help_text = "codex-cli 0.146.0\nUsage: codex archive [OPTIONS] <SESSION>\nArguments:\n  <SESSION>\n          Session id (UUID) or session name\n"
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_global_purge_delete_entry_is_rejected(self) -> None:
        help_text = "codex-cli 0.146.0\nPermanently delete all sessions\nUsage: codex delete [OPTIONS] <ALL>\nArguments:\n  <ALL>\n          Delete all sessions\n"
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_hypothetical_exact_session_delete_entry_is_supported(self) -> None:
        help_text = "codex-cli 0.146.0\nusage: codex delete [OPTIONS] session_id\npositional arguments:\n  session_id  Session ID to delete\noptions:\n  --yes, -y   Skip confirmation\n"
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "official_session_delete")
        self.assertEqual(capability.error_code, "")

    def test_empty_help_fails_closed(self) -> None:
        capability = _codex_session_cleanup_capability("")
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, CODEX_CLEANUP_UNSUPPORTED_CODE)


class HermesCapabilityProbeTests(unittest.TestCase):
    def test_frozen_fixture_probe_is_supported_with_official_strategy(self) -> None:
        capability = _hermes_session_cleanup_capability(HERMES_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "official_session_delete")
        self.assertEqual(capability.error_code, "")

    def test_resume_and_continue_flags_do_not_qualify(self) -> None:
        help_text = "Hermes Agent v0.17.0 (2026.6.19)\n  --resume SESSION_ID, -r SESSION_ID\n                        Resume a previous session by ID (shown on exit)\n  --continue [SESSION_NAME]\n                        Continue the most recent session with that name\n"
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)

    def test_fuzzy_selector_delete_entry_is_rejected(self) -> None:
        help_text = "usage: hermes sessions delete [-h] session_id\npositional arguments:\n  session_id  Session id (UUID) or session name\n"
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_global_prune_entry_is_rejected(self) -> None:
        help_text = "usage: hermes sessions prune [-h] [--yes]\nDelete old sessions\n"
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_delete_entry_without_exact_positional_id_is_rejected(self) -> None:
        help_text = "usage: hermes sessions delete [-h] [--yes] [--all]\noptions:\n  --all   Delete all sessions\n"
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_empty_help_fails_closed(self) -> None:
        capability = _hermes_session_cleanup_capability("")
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)

    def test_version_number_matches_frozen_semver_only(self) -> None:
        self.assertTrue(_version_number_matches(HERMES_VERSION_OUTPUT, "0.17.0"))
        self.assertTrue(_version_number_matches("codex-cli 0.146.0", "0.146.0"))
        self.assertFalse(_version_number_matches("Python 3.14.3", "0.17.0"))
        self.assertFalse(_version_number_matches("", "0.17.0"))


class CodexExecutorCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = CodexExecutor(command=sys.executable, transport="cli")
        self.request = _request()

    @staticmethod
    def _help_process(text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout=text if text is not None else CODEX_FIXTURE.read_text(encoding="utf-8"), stderr="")

    @staticmethod
    def _delete_process(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)

    def test_capability_and_cleanup_use_exact_force_uuid_argv(self) -> None:
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run", side_effect=[self._help_process()]) as run:
            capability = self.executor.session_cleanup_capability(self.request)
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "codex_desktop_archive_route_unavailable")
        self.assertEqual(run.call_count, 1)

    def test_cleanup_success_is_idempotent_for_absent_session(self) -> None:
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run"):
            first = self.executor.cleanup_session(self.request)
            second = self.executor.cleanup_session(self.request)
        self.assertEqual(first.state, "failed")
        self.assertEqual(second.state, "failed")
        self.assertEqual(first.error_code, "codex_desktop_archive_route_unavailable")
        self.assertEqual(second.error_code, "codex_desktop_archive_route_unavailable")

    def test_invalid_uuid_fails_before_any_subprocess(self) -> None:
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run") as run:
            result = self.executor.cleanup_session(_request(session_id="session name"))
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, CODEX_SESSION_DELETE_INVALID_ID_CODE)
        run.assert_not_called()

    def test_unsupported_help_never_spawns_delete(self) -> None:
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run") as run:
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "codex_desktop_archive_route_unavailable")
        run.assert_not_called()

    def test_delete_transport_failure_is_retryable(self) -> None:
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run"):
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "codex_desktop_archive_route_unavailable")
        self.assertTrue(result.retryable)

    def test_cleanup_result_never_leaks_sensitive_inputs(self) -> None:
        request = _request(project_path="/Users/example/.codex/private", workspace={"agentbc_root": "/private/root"})
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run"):
            result = self.executor.cleanup_session(request)
        rendered = repr(result)
        self.assertNotIn(CODEX_SESSION_ID, rendered)
        self.assertNotIn("/Users/example", rendered)
        self.assertNotIn("/private/root", rendered)
        self.assertNotIn("--force", rendered)
        self.assertIn("official_session_archive_then_delete", rendered)


class HermesExecutorCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = HermesExecutor(command=sys.executable, transport="direct")
        self.request = _request(executor="hermes", session_id=HERMES_SESSION_ID)

    @staticmethod
    def _help_process(text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout=text if text is not None else HERMES_FIXTURE.read_text(encoding="utf-8"), stderr="")

    @staticmethod
    def _delete_process(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)

    def test_capability_supported_from_discovered_exact_help(self) -> None:
        with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", return_value=self._help_process()) as run:
            capability = self.executor.session_cleanup_capability(self.request)
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "official_session_delete")
        self.assertEqual(run.call_args.args[0], [str(self.executor.agent_bin), "sessions", "delete", "--help"])

    def test_capability_fails_closed_on_unqualified_help_or_probe_error(self) -> None:
        for outcome in (self._help_process("usage: hermes sessions list"), OSError("missing")):
            with self.subTest(outcome=type(outcome).__name__):
                patch_kwargs = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
                with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", **patch_kwargs):
                    capability = self.executor.session_cleanup_capability(self.request)
                self.assertEqual(capability.capability, "unsupported")
                self.assertEqual(capability.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)

    def test_cleanup_session_invokes_exact_official_argv(self) -> None:
        with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", side_effect=[self._help_process(), self._delete_process(0)]) as run:
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(run.call_args_list[1].args[0], [str(self.executor.agent_bin), "sessions", "delete", HERMES_SESSION_ID, "--yes"])
        self.assertIs(run.call_args_list[1].kwargs["shell"], False)

    def test_cleanup_session_failure_maps_to_failed_receipt(self) -> None:
        with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", side_effect=[self._help_process(), self._delete_process(1)]):
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, HERMES_SESSION_DELETE_FAILED_CODE)
        self.assertFalse(result.retryable)

    def test_cleanup_session_rejects_missing_or_invalid_session_id_without_probe(self) -> None:
        cases = (
            ("", HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE),
            ("--yes", HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE),
            ("20260811 bad", HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE),
            ("delete --all", HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE),
            ("session-name", HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE),
        )
        for session_id, expected in cases:
            with self.subTest(session_id=session_id):
                with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run") as run:
                    result = self.executor.cleanup_session(_request(executor="hermes", session_id=session_id))
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, expected)
                run.assert_not_called()

    def test_unqualified_help_never_spawns_deletion(self) -> None:
        with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", return_value=self._help_process("usage: hermes sessions list")) as run:
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "unsupported")
        self.assertEqual(result.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)
        run.assert_called_once()

    def test_repeated_cleanup_returns_identical_results(self) -> None:
        with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", side_effect=[self._help_process(), self._delete_process(0), self._help_process(), self._delete_process(1, "Session does not exist")]):
            first = self.executor.cleanup_session(self.request)
            second = self.executor.cleanup_session(self.request)
        self.assertEqual(first, second)

    def test_cleanup_result_never_leaks_argv_output_or_paths(self) -> None:
        request = _request(executor="hermes", session_id=HERMES_SESSION_ID, project_path="/Users/example/.hermes/private", workspace={"agentbc_root": "/private/root"})
        with mock.patch("agent_bridge_connect.executors.hermes.subprocess.run", side_effect=[self._help_process(), self._delete_process(0)]):
            result = self.executor.cleanup_session(request)
        rendered = repr(result)
        self.assertNotIn(HERMES_SESSION_ID, rendered)
        self.assertNotIn("/Users/example", rendered)
        self.assertNotIn("/private/root", rendered)
        self.assertNotIn("--yes", rendered)
        self.assertNotIn(sys.executable, rendered)
        self.assertIn("official_session_delete", rendered)


_claude_cleanup_HELP_FIXTURE = _claude_cleanup_Path(__file__).parents[1] / "fixtures" / "executor_runtime" / "matrix" / "claude" / "2.1.226" / "help.txt"
_claude_cleanup_SESSION_ID = "12345678-1234-5678-9234-567812345678"


class _claude_cleanup_FakeClaudeCLI:
    def __init__(self, help_text: str) -> None:
        self.help_completed = _claude_cleanup_subprocess.CompletedProcess([], 0, stdout=help_text, stderr="")
        self.purge_completed = _claude_cleanup_subprocess.CompletedProcess([], 0, stdout="Purged project state", stderr="")
        self.purge_exception: Exception | None = None
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((list(command), dict(kwargs)))
        if command[-1] == "--help":
            return self.help_completed
        if self.purge_exception is not None:
            raise self.purge_exception
        return self.purge_completed


class _claude_cleanup_ClaudeManagedCleanupTests(_claude_cleanup_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _claude_cleanup_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _claude_cleanup_Path(self.temporary.name)
        self.agentbc_root = self.root / "workspace"
        self.plan = _claude_cleanup_build_path_plan(customer_dir=False, customer_path=None, task_code="C5NP", iteration=1, config={"workspace_root": str(self.agentbc_root)}, task_date="2026-08-11")
        self.workspace = self.plan.to_workspace()
        self.project = self.plan.executor_project_root
        self.project.mkdir(parents=True)
        self.help_text = _claude_cleanup_HELP_FIXTURE.read_text(encoding="utf-8")
        self.fake = _claude_cleanup_FakeClaudeCLI(self.help_text)
        self.executor = _claude_cleanup_ClaudeExecutor(command=str(self.root / "fake-claude"), transport="direct")

    def _request(self, **overrides: object) -> _claude_cleanup_SessionCleanupRequest:
        values: dict[str, object] = {"executor": "claude", "session_id": _claude_cleanup_SESSION_ID, "task_id": "C5NP-001", "retain": False, "project_mode": "ephemeral", "strategy": "claude_project_purge", "project_path": str(self.project), "workspace": self.workspace}
        values.update(overrides)
        return _claude_cleanup_SessionCleanupRequest(**values)

    def test_frozen_help_fixture_enables_exact_cleanup_capability(self) -> None:
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake):
            capability = self.executor.session_cleanup_capability(self._request())
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "claude_project_purge")
        (command, kwargs) = self.fake.calls[0]
        self.assertEqual(command, [str(self.executor.agent_bin), "project", "purge", "--help"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], _claude_cleanup_CLAUDE_PROJECT_PURGE_TIMEOUT_S)

    def test_incomplete_or_nonzero_help_is_unsupported(self) -> None:
        cases = (_claude_cleanup_subprocess.CompletedProcess([], 0, stdout="Usage: claude", stderr=""), _claude_cleanup_subprocess.CompletedProcess([], 2, stdout=self.help_text, stderr="bad help"))
        for completed in cases:
            with self.subTest(returncode=completed.returncode):
                fake = _claude_cleanup_FakeClaudeCLI(self.help_text)
                fake.help_completed = completed
                with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=fake):
                    capability = self.executor.session_cleanup_capability(self._request())
                self.assertEqual(capability.capability, "unsupported")
                self.assertEqual(capability.error_code, "claude_project_purge_unsupported")

    def test_retain_or_native_never_probes_or_purges(self) -> None:
        requests = (self._request(retain=True), self._request(project_mode="native"))
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run") as run:
            for request in requests:
                with self.subTest(retain=request.retain, project_mode=request.project_mode):
                    capability = self.executor.session_cleanup_capability(request)
                    result = self.executor.cleanup_session(request)
                    self.assertEqual(capability.capability, "not_applicable")
                    self.assertEqual(result.state, "retained")
                    self.assertEqual(result.capability, "not_applicable")
                    self.assertEqual(result.strategy, "retain")
        run.assert_not_called()
        self.assertTrue(self.project.is_dir())

    def test_invalid_session_binding_or_project_mode_never_probes(self) -> None:
        requests = (self._request(session_id="not-a-claude-session"), self._request(project_mode="none"))
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run") as run:
            for request in requests:
                with self.subTest(session_id=request.session_id, project_mode=request.project_mode):
                    result = self.executor.cleanup_session(request)
                    self.assertEqual(result.state, "failed")
                    self.assertFalse(result.retryable)
        run.assert_not_called()

    def test_ephemeral_cleanup_uses_canonical_argv_and_rmdir_order(self) -> None:
        real_rmdir = _claude_cleanup_os.rmdir
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake), _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.os.rmdir", wraps=real_rmdir) as rmdir:
            result = self.executor.cleanup_session(self._request())
        self.assertEqual(result.state, "succeeded")
        (purge_command, purge_kwargs) = self.fake.calls[1]
        self.assertEqual(purge_command, [str(self.executor.agent_bin), "project", "purge", "--yes", str(self.project)])
        self.assertNotIn("--all", purge_command)
        self.assertIs(purge_kwargs["shell"], False)
        self.assertEqual(purge_kwargs["timeout"], 30)
        self.assertEqual([item.args[0] for item in rmdir.call_args_list], [self.project, self.project.parent, self.project.parent.parent])
        self.assertFalse(self.project.parent.parent.exists())

    def test_managed_artifacts_survive_successful_session_cleanup(self) -> None:
        deliverable = self.plan.artifact_root / "result.txt"
        deliverable.write_text("keep", encoding="utf-8")
        real_rmdir = _claude_cleanup_os.rmdir
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake), _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.os.rmdir", wraps=real_rmdir) as rmdir:
            result = self.executor.cleanup_session(self._request())
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([item.args[0] for item in rmdir.call_args_list], [self.project, self.project.parent, self.project.parent.parent])
        self.assertFalse(self.project.parent.exists())
        self.assertEqual(deliverable.read_text(encoding="utf-8"), "keep")

    def test_purge_timeout_and_nonzero_exit_stop_before_rmdir(self) -> None:
        cases = ((_claude_cleanup_subprocess.TimeoutExpired(cmd=["fake-claude"], timeout=30), None, "claude_project_purge_timeout", True), (None, _claude_cleanup_subprocess.CompletedProcess([], 7, stdout="", stderr="purge failed"), "claude_project_purge_failed", False))
        for exception, completed, error_code, retryable in cases:
            with self.subTest(error_code=error_code):
                fake = _claude_cleanup_FakeClaudeCLI(self.help_text)
                fake.purge_exception = exception
                if completed is not None:
                    fake.purge_completed = completed
                with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=fake), _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.os.rmdir") as rmdir:
                    result = self.executor.cleanup_session(self._request())
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.retryable, retryable)
                rmdir.assert_not_called()

    def test_explicit_project_absence_supports_idempotent_repeat(self) -> None:
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake):
            first = self.executor.cleanup_session(self._request())
            self.fake.purge_completed = _claude_cleanup_subprocess.CompletedProcess([], 1, stdout="", stderr="No Claude Code project state found for project: exact path")
            second = self.executor.cleanup_session(self._request())
        self.assertEqual(first.state, "succeeded")
        self.assertEqual(second.state, "succeeded")
        purge_calls = [call for call in self.fake.calls if "--yes" in call[0]]
        self.assertEqual(len(purge_calls), 2)

    def test_symlink_path_is_rejected_without_purge(self) -> None:
        self.project.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.project.symlink_to(outside, target_is_directory=True)
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake):
            result = self.executor.cleanup_session(self._request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "cleanup_path_symlink")
        self.assertEqual(len(self.fake.calls), 0)
        self.assertTrue(outside.is_dir())

    def test_tampered_project_path_and_wrong_task_id_are_rejected(self) -> None:
        tampered_workspace = _claude_cleanup_copy.deepcopy(self.workspace)
        tampered_path = str(self.root / "outside" / "claude")
        tampered_workspace["executor_project_root"] = tampered_path
        requests = (
            (self._request(project_path=str(self.root / "outside" / "claude")), "cleanup_project_mismatch"),
            (self._request(project_path=tampered_path, workspace=tampered_workspace), "cleanup_project_mismatch"),
            (self._request(project_path=f"{self.project}/"), "cleanup_project_mismatch"),
            (self._request(task_id="C5NP-002"), "cleanup_task_mismatch"),
        )
        for request, expected in requests:
            with self.subTest(expected=expected):
                fake = _claude_cleanup_FakeClaudeCLI(self.help_text)
                with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=fake):
                    result = self.executor.cleanup_session(request)
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, expected)
                self.assertEqual(len(fake.calls), 0)

    def test_nonempty_directory_stops_layered_cleanup(self) -> None:
        unexpected = self.project / "unexpected.txt"
        unexpected.write_text("preserve", encoding="utf-8")
        real_rmdir = _claude_cleanup_os.rmdir
        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake), _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.os.rmdir", wraps=real_rmdir) as rmdir:
            result = self.executor.cleanup_session(self._request())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "claude_cleanup_directory_not_empty")
        self.assertEqual(rmdir.call_count, 1)
        self.assertEqual(unexpected.read_text(encoding="utf-8"), "preserve")

    def test_path_plan_is_revalidated_before_every_rmdir(self) -> None:
        workspace = _claude_cleanup_copy.deepcopy(self.workspace)
        request = self._request(workspace=workspace)
        real_rmdir = _claude_cleanup_os.rmdir
        rmdir_calls: list[_claude_cleanup_Path] = []

        def drift_after_first_rmdir(path: _claude_cleanup_Path) -> None:
            rmdir_calls.append(path)
            real_rmdir(path)
            workspace["iteration"] = "002"

        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake), _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.os.rmdir", side_effect=drift_after_first_rmdir):
            result = self.executor.cleanup_session(request)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "cleanup_task_mismatch")
        self.assertEqual(rmdir_calls, [self.project])
        self.assertTrue(self.project.parent.is_dir())

    def test_cleanup_mode_is_revalidated_before_every_rmdir(self) -> None:
        request = self._request()
        real_rmdir = _claude_cleanup_os.rmdir
        rmdir_calls: list[_claude_cleanup_Path] = []

        def change_mode_after_first_rmdir(path: _claude_cleanup_Path) -> None:
            rmdir_calls.append(path)
            real_rmdir(path)
            object.__setattr__(request, "project_mode", "native")

        with _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.subprocess.run", side_effect=self.fake), _claude_cleanup_mock.patch("agent_bridge_connect.executors.claude.os.rmdir", side_effect=change_mode_after_first_rmdir):
            result = self.executor.cleanup_session(request)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "claude_cleanup_mode_invalid")
        self.assertEqual(rmdir_calls, [self.project])
        self.assertTrue(self.project.parent.is_dir())
