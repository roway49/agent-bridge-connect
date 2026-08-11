from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import SessionCleanupRequest
from agent_bridge_connect.executors.codex import (
    CODEX_CLEANUP_UNSUPPORTED_CODE,
    CodexExecutor,
    _CODEX_FROZEN_VERSION,
    _codex_session_cleanup_capability,
)
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

FIXTURES = Path(__file__).parent / "fixtures" / "executor_runtime"
CODEX_FIXTURE = FIXTURES / "codex_0.146.0_help.txt"
HERMES_FIXTURE = FIXTURES / "hermes_0.17.0_help.txt"
OFFICIAL_SESSION_ID = "20260811_004323_d3bd9b"
HERMES_VERSION_OUTPUT = (
    "Hermes Agent v0.17.0 (2026.6.19) \u00b7 upstream 2cdb30a4\n"
)


def _request(**overrides: object) -> SessionCleanupRequest:
    values: dict[str, object] = {
        "executor": "codex",
        "session_id": OFFICIAL_SESSION_ID,
        "task_id": "F5AH-001",
        "strategy": "official_session_delete",
    }
    values.update(overrides)
    return SessionCleanupRequest(**values)  # type: ignore[arg-type]


class FrozenFixtureTests(unittest.TestCase):
    def test_codex_frozen_help_fixture_pins_version_and_fuzzy_delete_only(self) -> None:
        text = CODEX_FIXTURE.read_text(encoding="utf-8")
        self.assertIn(f"codex-cli {_CODEX_FROZEN_VERSION}", text)
        self.assertIn("Usage: codex delete [OPTIONS] <SESSION>", text)
        self.assertIn("Session id (UUID) or session name", text)
        self.assertIn("UUIDs take precedence if it parses", text)
        self.assertIn("--last", text)
        self.assertIn("picker", text)
        self.assertNotIn("sessions delete", text)

    def test_hermes_frozen_help_fixture_pins_exact_delete_entry(self) -> None:
        text = HERMES_FIXTURE.read_text(encoding="utf-8")
        self.assertIn(f"Hermes Agent v{_HERMES_FROZEN_VERSION}", text)
        self.assertIn("usage: hermes sessions delete [-h] [--yes] session_id", text)
        self.assertIn("session_id  Session ID to delete", text)
        self.assertIn("--yes, -y   Skip confirmation", text)


class CodexCapabilityProbeTests(unittest.TestCase):
    def test_frozen_fixture_probe_is_unsupported_with_stable_code(self) -> None:
        capability = _codex_session_cleanup_capability(
            CODEX_FIXTURE.read_text(encoding="utf-8")
        )
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.strategy, "none")
        self.assertEqual(capability.error_code, CODEX_CLEANUP_UNSUPPORTED_CODE)

    def test_misleading_same_name_delete_with_fuzzy_selector_is_rejected(self) -> None:
        # ``codex delete`` looks like a delete entry but accepts "id or session
        # name" and lets names take precedence over exact targeting.
        help_text = (
            "codex-cli 0.146.0\n"
            "Permanently delete a saved session by id or session name\n"
            "Usage: codex delete [OPTIONS] <SESSION>\n"
            "Arguments:\n"
            "  <SESSION>\n"
            "          Session id (UUID) or session name. UUIDs take precedence if it parses\n"
        )
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, CODEX_CLEANUP_UNSUPPORTED_CODE)

    def test_resume_last_and_picker_entries_are_rejected(self) -> None:
        help_text = (
            "codex-cli 0.146.0\n"
            "Usage: codex resume [OPTIONS] [SESSION_ID] [PROMPT]\n"
            "Options:\n"
            "      --last\n"
            "          Continue the most recent session without showing the picker\n"
        )
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_archive_entry_is_not_a_delete_capability(self) -> None:
        help_text = (
            "codex-cli 0.146.0\n"
            "Usage: codex archive [OPTIONS] <SESSION>\n"
            "Arguments:\n"
            "  <SESSION>\n"
            "          Session id (UUID) or session name\n"
        )
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_global_purge_delete_entry_is_rejected(self) -> None:
        help_text = (
            "codex-cli 0.146.0\n"
            "Permanently delete all sessions\n"
            "Usage: codex delete [OPTIONS] <ALL>\n"
            "Arguments:\n"
            "  <ALL>\n"
            "          Delete all sessions\n"
        )
        capability = _codex_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_hypothetical_exact_session_delete_entry_is_supported(self) -> None:
        # Judgment boundary: an official delete entry accepting only the exact
        # positional session id (with a skip-confirmation flag) qualifies.
        help_text = (
            "codex-cli 0.146.0\n"
            "usage: codex delete [OPTIONS] session_id\n"
            "positional arguments:\n"
            "  session_id  Session ID to delete\n"
            "options:\n"
            "  --yes, -y   Skip confirmation\n"
        )
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
        capability = _hermes_session_cleanup_capability(
            HERMES_FIXTURE.read_text(encoding="utf-8")
        )
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "official_session_delete")
        self.assertEqual(capability.error_code, "")

    def test_resume_and_continue_flags_do_not_qualify(self) -> None:
        help_text = (
            "Hermes Agent v0.17.0 (2026.6.19)\n"
            "  --resume SESSION_ID, -r SESSION_ID\n"
            "                        Resume a previous session by ID (shown on exit)\n"
            "  --continue [SESSION_NAME]\n"
            "                        Continue the most recent session with that name\n"
        )
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)

    def test_fuzzy_selector_delete_entry_is_rejected(self) -> None:
        help_text = (
            "usage: hermes sessions delete [-h] session_id\n"
            "positional arguments:\n"
            "  session_id  Session id (UUID) or session name\n"
        )
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_global_prune_entry_is_rejected(self) -> None:
        help_text = (
            "usage: hermes sessions prune [-h] [--yes]\n"
            "Delete old sessions\n"
        )
        capability = _hermes_session_cleanup_capability(help_text)
        self.assertEqual(capability.capability, "unsupported")

    def test_delete_entry_without_exact_positional_id_is_rejected(self) -> None:
        help_text = (
            "usage: hermes sessions delete [-h] [--yes] [--all]\n"
            "options:\n"
            "  --all   Delete all sessions\n"
        )
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
        self.executor = CodexExecutor(command=sys.executable)
        self.request = _request()

    def test_capability_and_cleanup_are_unsupported_without_any_subprocess(self) -> None:
        with mock.patch(
            "agent_bridge_connect.executors.codex.subprocess.run"
        ) as run:
            capability = self.executor.session_cleanup_capability(self.request)
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, CODEX_CLEANUP_UNSUPPORTED_CODE)
        self.assertEqual(result.state, "unsupported")
        self.assertEqual(result.capability, "unsupported")
        self.assertEqual(result.strategy, "none")
        self.assertEqual(result.error_code, CODEX_CLEANUP_UNSUPPORTED_CODE)
        self.assertFalse(result.retryable)
        self.assertEqual(result.next_attempt_at, "")
        run.assert_not_called()

    def test_cleanup_unsupported_is_idempotent_and_stable(self) -> None:
        with mock.patch("agent_bridge_connect.executors.codex.subprocess.run"):
            first = self.executor.cleanup_session(self.request)
            second = self.executor.cleanup_session(self.request)
        self.assertEqual(first, second)
        self.assertEqual(first, self.executor.cleanup_session(self.request))

    def test_cleanup_result_never_leaks_sensitive_inputs(self) -> None:
        request = _request(
            session_id=OFFICIAL_SESSION_ID,
            project_path="/Users/example/.codex/private",
            workspace={"agentbc_root": "/private/root"},
        )
        result = self.executor.cleanup_session(request)
        rendered = repr(result)
        self.assertNotIn(OFFICIAL_SESSION_ID, rendered)
        self.assertNotIn("/Users/example", rendered)
        self.assertNotIn("/private/root", rendered)
        self.assertNotIn("--yes", rendered)
        self.assertNotIn("codex delete", rendered)
        self.assertIn("codex_session_delete_unavailable", rendered)


class HermesExecutorCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = HermesExecutor(command=sys.executable, transport="direct")
        self.request = _request(executor="hermes")

    @staticmethod
    def _version_process(output: str = HERMES_VERSION_OUTPUT) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout=output)

    @staticmethod
    def _delete_process(returncode: int) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr="")

    def test_capability_supported_when_discovered_cli_is_frozen_version(self) -> None:
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            return_value=self._version_process(),
        ) as run:
            capability = self.executor.session_cleanup_capability(self.request)
        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "official_session_delete")
        self.assertEqual(capability.error_code, "")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [sys.executable, "--version"])

    def test_capability_fails_closed_on_version_mismatch(self) -> None:
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            return_value=self._version_process("Python 3.14.3\n"),
        ) as run:
            capability = self.executor.session_cleanup_capability(self.request)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)
        run.assert_called_once()

    def test_capability_fails_closed_when_fixture_unreadable(self) -> None:
        with (
            mock.patch(
                "agent_bridge_connect.executors.hermes._frozen_help_fixture_text",
                return_value="",
            ),
            mock.patch(
                "agent_bridge_connect.executors.hermes.subprocess.run"
            ) as run,
        ):
            capability = self.executor.session_cleanup_capability(self.request)
        self.assertEqual(capability.capability, "unsupported")
        self.assertEqual(capability.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)
        run.assert_not_called()

    def test_cleanup_session_invokes_exact_official_argv(self) -> None:
        side_effects = [self._version_process(), self._delete_process(0)]
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            side_effect=side_effects,
        ) as run:
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.capability, "supported")
        self.assertEqual(result.strategy, "official_session_delete")
        self.assertEqual(result.error_code, "")
        self.assertFalse(result.retryable)
        self.assertEqual(run.call_count, 2)
        delete_call = run.call_args_list[1]
        self.assertEqual(
            delete_call.args[0],
            [sys.executable, "sessions", "delete", OFFICIAL_SESSION_ID, "--yes"],
        )
        self.assertIsNone(delete_call.kwargs.get("shell"))
        self.assertEqual(delete_call.kwargs.get("timeout"), 60)

    def test_cleanup_session_failure_maps_to_failed_receipt(self) -> None:
        side_effects = [self._version_process(), self._delete_process(1)]
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            side_effect=side_effects,
        ):
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, HERMES_SESSION_DELETE_FAILED_CODE)
        self.assertFalse(result.retryable)
        self.assertEqual(result.next_attempt_at, "")

    def test_cleanup_session_rejects_missing_session_id(self) -> None:
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            return_value=self._version_process(),
        ) as run:
            result = self.executor.cleanup_session(_request(executor="hermes", session_id=""))
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE)
        self.assertFalse(result.retryable)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [sys.executable, "--version"])

    def test_cleanup_session_rejects_flag_injection_session_id(self) -> None:
        for session_id in ("--yes", "20260811 bad", "delete --all", "delete/all"):
            with self.subTest(session_id=session_id):
                with mock.patch(
                    "agent_bridge_connect.executors.hermes.subprocess.run",
                    return_value=self._version_process(),
                ) as run:
                    result = self.executor.cleanup_session(
                        _request(executor="hermes", session_id=session_id)
                    )
                self.assertEqual(result.state, "failed")
                self.assertEqual(
                    result.error_code, HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE
                )
                self.assertFalse(result.retryable)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], [sys.executable, "--version"])

    def test_unsupported_path_never_spawns_deletion_subprocess(self) -> None:
        # Version mismatch -> capability unsupported -> cleanup must not run
        # any command that could delete a session.
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            return_value=self._version_process("Python 3.14.3\n"),
        ) as run:
            result = self.executor.cleanup_session(self.request)
        self.assertEqual(result.state, "unsupported")
        self.assertEqual(result.error_code, HERMES_CLEANUP_UNSUPPORTED_CODE)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [sys.executable, "--version"])

    def test_repeated_cleanup_returns_identical_results(self) -> None:
        side_effects = [
            self._version_process(),
            self._delete_process(0),
            self._version_process(),
            self._delete_process(0),
        ]
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            side_effect=side_effects,
        ):
            first = self.executor.cleanup_session(self.request)
            second = self.executor.cleanup_session(self.request)
        self.assertEqual(first, second)

    def test_cleanup_result_never_leaks_argv_output_or_paths(self) -> None:
        request = _request(
            executor="hermes",
            session_id=OFFICIAL_SESSION_ID,
            project_path="/Users/example/.hermes/private",
            workspace={"agentbc_root": "/private/root"},
        )
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run",
            side_effect=[self._version_process(), self._delete_process(0)],
        ):
            result = self.executor.cleanup_session(request)
        rendered = repr(result)
        self.assertNotIn(OFFICIAL_SESSION_ID, rendered)
        self.assertNotIn("/Users/example", rendered)
        self.assertNotIn("/private/root", rendered)
        self.assertNotIn("--yes", rendered)
        self.assertNotIn(sys.executable, rendered)
        self.assertIn("official_session_delete", rendered)


if __name__ == "__main__":
    unittest.main()
