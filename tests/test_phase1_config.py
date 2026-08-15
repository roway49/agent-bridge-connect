from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect import config as config_module
from agent_bridge_connect.cli import main
from agent_bridge_connect.config import (
    DEFAULT_CLAUDE_MAX_BUDGET_USD,
    DEFAULT_HERMES_MAX_TURNS,
    configured_session_retention,
    load_config,
    update_config_atomic,
    validate_config,
    write_config_atomic,
)
from agent_bridge_connect.executor_registry import get_executor
from agent_bridge_connect.setup import (
    _extract_hermes_max_turns,
    _merge_executor_config,
    _select_claude_budget,
    _select_hermes_max_turns,
    _select_session_retention,
    resolve_hermes_default_max_turns,
)


def _concurrent_update(path: str, key: str, value: int, barrier: object) -> None:
    barrier.wait()

    def mutate(config: dict) -> None:
        config.setdefault("concurrent", {})[key] = value

    update_config_atomic(mutate, path)


class ConfigTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "nested" / "config.toml"

    def test_resource_validation_rejects_unsafe_values(self) -> None:
        invalid = (
            {"executors": {"claude": {"max_budget_usd": True}}},
            {"executors": {"claude": {"max_budget_usd": float("nan")}}},
            {"executors": {"claude": {"max_budget_usd": 0}}},
            {"executors": {"hermes": {"max_turns": True}}},
            {"executors": {"hermes": {"max_turns": 2.5}}},
            {"sessions": {"retain_executor_sessions": "yes"}},
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                self.assertTrue(validate_config(candidate))

    def test_atomic_round_trip_preserves_unknown_values_and_permissions(self) -> None:
        original = {
            "workspace_root": "/tmp/workspace",
            "unknown": {"spaced key": "kept", "values": [1, 2, 3]},
            "executors": {"claude": {"type": "claude", "max_budget_usd": 25.0}},
        }
        self.assertTrue(write_config_atomic(original, self.path))
        self.assertEqual(load_config(self.path), original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_python310_compat_parser_accepts_toml_literal_strings(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            "[executors.codex]\ncommand = '/usr/bin/env codex'\n",
            encoding="utf-8",
        )
        with mock.patch.object(config_module, "tomllib", None):
            loaded = load_config(self.path)
        self.assertEqual(
            loaded,
            {"executors": {"codex": {"command": "/usr/bin/env codex"}}},
        )

    def test_idempotent_update_does_not_replace_file(self) -> None:
        write_config_atomic({"sessions": {"retain_executor_sessions": False}}, self.path)
        inode = self.path.stat().st_ino
        _, changed = update_config_atomic(lambda config: None, self.path)
        self.assertFalse(changed)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_invalid_existing_config_is_not_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        original = "[executors.claude\nmax_budget_usd = 10\n"
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaises(ValueError):
            update_config_atomic(lambda config: config.update({"new": True}), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_replace_failure_preserves_original_and_cleans_temporary_file(self) -> None:
        write_config_atomic({"value": 1}, self.path)
        with mock.patch.object(config_module.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                update_config_atomic(lambda config: config.update({"value": 2}), self.path)
        self.assertEqual(load_config(self.path), {"value": 1})
        self.assertEqual(list(self.path.parent.glob(".config.toml.*.tmp")), [])

    def test_fsync_failure_preserves_original_and_cleans_temporary_file(self) -> None:
        write_config_atomic({"value": 1}, self.path)
        with mock.patch.object(config_module.os, "fsync", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                update_config_atomic(lambda config: config.update({"value": 2}), self.path)
        self.assertEqual(load_config(self.path), {"value": 1})
        self.assertEqual(list(self.path.parent.glob(".config.toml.*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "Phase 1 locking is POSIX-only")
    def test_two_processes_do_not_lose_independent_updates(self) -> None:
        write_config_atomic({"seed": True}, self.path)
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        first = context.Process(
            target=_concurrent_update,
            args=(str(self.path), "first", 1, barrier),
        )
        second = context.Process(
            target=_concurrent_update,
            args=(str(self.path), "second", 2, barrier),
        )
        first.start()
        second.start()
        first.join(5)
        second.join(5)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        self.assertEqual(load_config(self.path)["concurrent"], {"first": 1, "second": 2})


class HermesDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.yaml = self.root / "config.yaml"

    def _completed(self) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["hermes", "config", "path"],
            0,
            f"{self.yaml}\n",
            "",
        )

    def test_nested_agent_value_wins_and_unrelated_nested_value_is_ignored(self) -> None:
        text = "goals:\n  max_turns: 20\nmax_turns: 45\nagent:\n  model: test\n  max_turns: 60\n"
        self.assertEqual(_extract_hermes_max_turns(text), (60, 45))
        self.yaml.write_text(text, encoding="utf-8")
        with mock.patch("agent_bridge_connect.setup.subprocess.run", return_value=self._completed()):
            self.assertEqual(
                resolve_hermes_default_max_turns("hermes"),
                (60, "hermes_agent_config"),
            )

    def test_legacy_value_is_supported(self) -> None:
        self.yaml.write_text("max_turns: 72\n", encoding="utf-8")
        with mock.patch("agent_bridge_connect.setup.subprocess.run", return_value=self._completed()):
            self.assertEqual(
                resolve_hermes_default_max_turns("hermes"),
                (72, "hermes_legacy_config"),
            )

    def test_invalid_complex_or_unavailable_values_fall_back(self) -> None:
        invalid_values = (
            "goals:\n  max_turns: 20\n",
            "agent:\n  max_turns: '60'\n",
            "agent:\n  nested:\n    max_turns: 60\n",
            "agent:\n  max_turns: &turns 60\n",
            "agent:\n  max_turns: -1\n",
        )
        for text in invalid_values:
            with self.subTest(text=text):
                self.yaml.write_text(text, encoding="utf-8")
                with mock.patch(
                    "agent_bridge_connect.setup.subprocess.run", return_value=self._completed()
                ):
                    self.assertEqual(
                        resolve_hermes_default_max_turns("hermes"),
                        (DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"),
                    )
        with mock.patch(
            "agent_bridge_connect.setup.subprocess.run",
            side_effect=subprocess.TimeoutExpired("hermes", 5),
        ):
            self.assertEqual(
                resolve_hermes_default_max_turns("hermes"),
                (DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"),
            )

    def test_oversized_config_falls_back_without_reading(self) -> None:
        self.yaml.write_bytes(b"x" * (1024 * 1024 + 1))
        with mock.patch("agent_bridge_connect.setup.subprocess.run", return_value=self._completed()):
            self.assertEqual(
                resolve_hermes_default_max_turns("hermes"),
                (DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"),
            )


class SetupResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / ".abc" / "config.toml"

    def _agent(self, name: str) -> dict:
        return {
            "name": name,
            "display": name.title(),
            "found": True,
            "supported_executor": True,
            "path": f"/usr/bin/{name}",
            "binary": name,
            "version": "test",
            "source": "test",
            "capability_level": "L2" if name == "hermes" else "L1",
            "capabilities": {},
            "skill": {"installed": True, "up_to_date": True},
        }

    def _run_noninteractive_setup(self) -> dict:
        from agent_bridge_connect import setup

        agents = [self._agent("hermes"), self._agent("claude")]
        skill_result = {"installed": True, "changed": False, "status": "already_installed"}
        environment = {
            "HOME": str(self.root),
            "AGENTBC_CONFIG_PATH": str(self.config_path),
        }
        with mock.patch.dict(os.environ, environment, clear=False), \
             mock.patch.object(setup, "scan_all_agents", side_effect=[agents, agents]), \
             mock.patch.object(setup, "_print_scan_report"), \
             mock.patch.object(setup, "install_hermes_skill", return_value=skill_result), \
             mock.patch.object(setup, "install_claude_skill", return_value=skill_result), \
             mock.patch.object(setup, "_configure_alias", return_value={"status": "skipped"}), \
             mock.patch.object(setup, "discover_codex", return_value={"found": False}), \
             mock.patch.object(setup, "probe_codex", return_value={}):
            with contextlib.redirect_stdout(io.StringIO()):
                return setup.run_setup(interactive=False)

    def test_executor_refresh_preserves_user_resource_values(self) -> None:
        claude = _merge_executor_config(
            "claude",
            {"type": "claude", "max_budget_usd": 25.0, "unknown": "keep"},
            {"type": "claude", "command": "new", "max_budget_usd": 10.0},
        )
        hermes = _merge_executor_config(
            "hermes",
            {"type": "hermes", "max_turns": 77, "unknown": "keep"},
            {"type": "hermes", "command": "new"},
        )
        self.assertEqual(claude["max_budget_usd"], 25.0)
        self.assertEqual(hermes["max_turns"], 77)
        self.assertEqual(claude["unknown"], "keep")
        self.assertEqual(hermes["unknown"], "keep")
        self.assertEqual(claude["command"], "new")

    def test_interactive_budget_choices_keep_default_and_custom(self) -> None:
        existing = {"executors": {"claude": {"max_budget_usd": 1.0}}}
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(_select_claude_budget(existing, interactive=True), (1.0, False, "configured"))
        with mock.patch("builtins.input", return_value="1"):
            self.assertEqual(
                _select_claude_budget(existing, interactive=True),
                (DEFAULT_CLAUDE_MAX_BUDGET_USD, True, "claude_default_10"),
            )
        with mock.patch("builtins.input", side_effect=["2", "bad", "12.5"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(_select_claude_budget({}, interactive=True), (12.5, True, "custom"))

    def test_existing_hermes_value_does_not_query_native_config(self) -> None:
        existing = {"executors": {"hermes": {"max_turns": 77}}}
        with mock.patch("agent_bridge_connect.setup.resolve_hermes_default_max_turns") as resolve:
            self.assertEqual(
                _select_hermes_max_turns(existing, command="hermes", interactive=False),
                (77, False, "configured"),
            )
        resolve.assert_not_called()

    def test_hermes_max_turns_is_injected_into_runtime_executor(self) -> None:
        executor = get_executor(
            "hermes",
            {"type": "hermes", "command": "/usr/bin/false", "max_turns": 60},
        )
        self.assertEqual(executor.max_turns, 60)

    def test_noninteractive_setup_writes_missing_defaults(self) -> None:
        with mock.patch(
            "agent_bridge_connect.setup.resolve_hermes_default_max_turns",
            return_value=(60, "hermes_agent_config"),
        ):
            result = self._run_noninteractive_setup()
        config = load_config(self.config_path)
        self.assertTrue(result["config_written"])
        self.assertEqual(config["permissions"]["mode"], "inherit")
        self.assertNotIn("permission_mode", config)
        self.assertEqual(config["executors"]["claude"]["max_budget_usd"], 10.0)
        self.assertEqual(config["executors"]["hermes"]["max_turns"], 60)
        self.assertFalse(config["sessions"]["retain_executor_sessions"])

    def test_first_interactive_retention_defaults_to_no(self) -> None:
        with mock.patch("builtins.input", return_value="") as prompt:
            selected = _select_session_retention({}, interactive=True)
        self.assertEqual(selected, (False, True, "session_default_false"))
        self.assertIn("[y/N]", prompt.call_args.args[0])

    def test_existing_interactive_retention_keeps_current_value(self) -> None:
        config = {"sessions": {"retain_executor_sessions": True}}
        with mock.patch("builtins.input", return_value="") as prompt:
            selected = _select_session_retention(config, interactive=True)
        self.assertEqual(selected, (True, False, "configured"))
        self.assertIn("Enter=enabled", prompt.call_args.args[0])

    def test_noninteractive_setup_preserves_existing_resources_and_unknown_keys(self) -> None:
        write_config_atomic(
            {
                "workspace_root": str(self.root / "workspace"),
                "unknown": {"keep": "yes"},
                "executors": {
                    "claude": {"type": "claude", "command": "old", "max_budget_usd": 25.0},
                    "hermes": {"type": "hermes", "command": "old", "max_turns": 77},
                },
                "sessions": {"retain_executor_sessions": True},
            },
            self.config_path,
        )
        with mock.patch(
            "agent_bridge_connect.setup.resolve_hermes_default_max_turns"
        ) as resolve:
            self._run_noninteractive_setup()
        resolve.assert_not_called()
        config = load_config(self.config_path)
        self.assertEqual(config["executors"]["claude"]["max_budget_usd"], 25.0)
        self.assertEqual(config["executors"]["hermes"]["max_turns"], 77)
        self.assertTrue(config["sessions"]["retain_executor_sessions"])
        self.assertEqual(config["unknown"]["keep"], "yes")


class ConfigCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.toml"
        self.env = mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(self.path)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, arguments: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def test_budget_and_turn_commands_change_only_the_target_setting(self) -> None:
        write_config_atomic(
            {
                "unknown": {"keep": True},
                "executors": {
                    "claude": {"type": "claude", "command": "claude", "max_budget_usd": 1.0},
                    "hermes": {"type": "hermes", "command": "hermes", "max_turns": 60},
                },
            },
            self.path,
        )
        code, budget = self._run(["claude", "budget", "25"])
        self.assertEqual(code, 0)
        self.assertEqual(budget["previous"], 1.0)
        self.assertTrue(budget["changed"])
        code, turns = self._run(["hermes", "max-turns", "120"])
        self.assertEqual(code, 0)
        self.assertEqual(turns["previous"], 60)
        config = load_config(self.path)
        self.assertEqual(config["executors"]["claude"]["max_budget_usd"], 25.0)
        self.assertEqual(config["executors"]["hermes"]["max_turns"], 120)
        self.assertTrue(config["unknown"]["keep"])
        _, unchanged = self._run(["claude", "budget", "25"])
        self.assertFalse(unchanged["changed"])

    def test_unconfigured_executor_returns_machine_readable_error(self) -> None:
        write_config_atomic({"executors": {}}, self.path)
        code, payload = self._run(["claude", "budget", "10"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "not_configured")

    def test_retention_status_is_read_only_and_enable_disable_are_idempotent(self) -> None:
        code, status_payload = self._run(["session", "retention", "status"])
        self.assertEqual(code, 0)
        self.assertFalse(status_payload["value"])
        self.assertFalse(self.path.exists())
        code, enabled = self._run(["session", "retention", "enable"])
        self.assertEqual(code, 0)
        self.assertTrue(enabled["changed"])
        self.assertEqual(configured_session_retention(load_config(self.path)), (True, "configured"))
        _, unchanged = self._run(["session", "retention", "enable"])
        self.assertFalse(unchanged["changed"])
        _, disabled = self._run(["session", "retention", "disable"])
        self.assertFalse(disabled["value"])
        self.assertEqual(disabled["dispatcher_conversations"], "never_deleted_by_agentbc")

    def test_invalid_cli_values_exit_two_before_writing(self) -> None:
        for arguments in (["claude", "budget", "nan"], ["hermes", "max-turns", "2.5"]):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        main(arguments)
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.path.exists())

    def test_invalid_config_is_reported_without_overwrite(self) -> None:
        original = "[sessions]\nretain_executor_sessions = \"yes\"\n"
        self.path.write_text(original, encoding="utf-8")
        code, payload = self._run(["session", "retention", "enable"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "config_invalid")
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
