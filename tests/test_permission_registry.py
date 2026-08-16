"""Targeted tests for the unified permission registry (PERM-103-001/002).

Covers the ``agentbc.permission`` v2 record contract, the resolution
priority (task override > handoff snapshot > config > legacy safe), the
dual-read unified permission setting, the same-source setup/config
transaction, the three-executor x three-mode capability mapping and probes
(including ``permission_capability_unsupported``), and the frozen Hermes ACP
capability (``transport=hermes-acp``, ``session/request_permission``,
subprocess-scoped ``HERMES_YOLO_MODE=1``).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_bridge_connect.cli import main
from agent_bridge_connect.config import (
    apply_permissions_setting,
    load_config,
    set_permissions_mode_atomic,
)
from agent_bridge_connect.permission_modes import (
    PERMISSION_EXTENSION_KEY,
    build_permission_record,
    configured_permission_mode,
    permission_record_from_extensions,
    validate_permission_record,
)
from agent_bridge_connect.permission_registry import (
    GLOBAL_PERMISSION_SETTING,
    HERMES_ACP_CHECK_CAPABILITY_ID,
    HERMES_ACP_FULL_YOLO_ENV_CAPABILITY_ID,
    HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
    LEGACY_PERMISSION_SETTING,
    PERMISSION_SCHEMA_VERSION,
    RESOLUTION_PRIORITY,
    TRANSPORT_HERMES_ACP,
    build_permission_audit_payload,
    executor_permission_mapping,
    permission_args_for,
    permission_mapping_view,
    permissions_status_payload,
    probe_executor_capability,
    probe_hermes_acp,
)
from agent_bridge_connect.protocol import ABCError

FAKE_ACP_CHECK = mock.Mock(
    returncode=0,
    stdout="Hermes ACP check OK\n",
    stderr="",
)
FAKE_ACP_VERSION = mock.Mock(
    returncode=0,
    stdout="0.20.1\n",
    stderr="",
)


class PermissionRegistryV2RecordTests(unittest.TestCase):
    def test_v2_record_exposes_all_public_fields(self) -> None:
        record = build_permission_record(config={"permissions": {"mode": "safe"}})
        self.assertEqual(record["version"], PERMISSION_SCHEMA_VERSION)
        self.assertEqual(record["configured_mode"], "safe")
        self.assertIsNone(record["inherited_mode"])
        self.assertIsNone(record["task_override"])
        self.assertEqual(record["requested_mode"], "safe")
        self.assertEqual(record["effective_mode"], "safe")
        self.assertEqual(record["selection_source"], "configured_default")
        self.assertEqual(record["scope"], "task")
        self.assertEqual(record["permission_args"], [])
        self.assertEqual(set(record["mapping"]), {"codex", "claude", "hermes"})

    def test_priority_task_override_over_handoff_over_config(self) -> None:
        inherited = build_permission_record(explicit_mode="full")
        override = build_permission_record(
            explicit_mode="safe",
            config={"permissions": {"mode": "full"}},
            inherited=inherited,
        )
        self.assertEqual(override["effective_mode"], "safe")
        self.assertEqual(override["selection_source"], "explicit_task")
        self.assertEqual(override["task_override"], "safe")
        self.assertEqual(override["inherited_mode"], "full")
        self.assertEqual(override["configured_mode"], "full")

        inherited_only = build_permission_record(
            config={"permissions": {"mode": "inherit"}},
            inherited=inherited,
        )
        self.assertEqual(inherited_only["effective_mode"], "full")
        self.assertEqual(inherited_only["selection_source"], "inherited_task")
        self.assertEqual(inherited_only["inherited_mode"], "full")
        self.assertEqual(inherited_only["task_override"], None)
        self.assertEqual(inherited_only["configured_mode"], "inherit")

    def test_config_priority_and_legacy_safe_fallback(self) -> None:
        self.assertEqual(
            build_permission_record(config={"permissions": {"mode": "safe"}})[
                "effective_mode"
            ],
            "safe",
        )
        self.assertEqual(
            build_permission_record(config={})["selection_source"], "inherit_default"
        )
        self.assertEqual(
            build_permission_record(config={})["effective_mode"], "inherit"
        )
        # Historical tasks without a record fail closed to legacy safe.
        legacy = permission_record_from_extensions(None)
        self.assertEqual(legacy["effective_mode"], "safe")
        self.assertEqual(legacy["selection_source"], "legacy_task")
        self.assertEqual(
            permission_record_from_extensions({"unrelated": 1})["effective_mode"],
            "safe",
        )

    def test_handoff_snapshot_accepts_v1_records(self) -> None:
        v1_source = {
            "requested_mode": "safe",
            "effective_mode": "safe",
            "selection_source": "configured_default",
        }
        record = build_permission_record(config={}, inherited=v1_source)
        self.assertEqual(record["effective_mode"], "safe")
        self.assertEqual(record["selection_source"], "inherited_task")
        self.assertEqual(record["inherited_mode"], "safe")
        self.assertEqual(record["version"], PERMISSION_SCHEMA_VERSION)

    def test_validate_roundtrips_v1_and_v2(self) -> None:
        v1 = {
            "requested_mode": "safe",
            "effective_mode": "safe",
            "selection_source": "explicit_task",
        }
        self.assertEqual(validate_permission_record(v1), v1)
        v2 = build_permission_record(explicit_mode="full")
        self.assertEqual(validate_permission_record(v2), v2)

    def test_validate_rejects_unknown_version_and_malformed_v2(self) -> None:
        with self.assertRaises(ABCError) as raised:
            validate_permission_record(
                {
                    "version": 99,
                    "requested_mode": "safe",
                    "effective_mode": "safe",
                    "selection_source": "x",
                }
            )
        self.assertEqual(raised.exception.code, "unsupported_permission_mode")
        with self.assertRaises(ABCError) as raised:
            validate_permission_record(
                {
                    "version": 2,
                    "configured_mode": "safe",
                    "effective_mode": "safe",
                    "selection_source": "x",
                    "scope": "",
                }
            )
        self.assertEqual(raised.exception.code, "invalid_permission_mode")
        with self.assertRaises(ABCError) as raised:
            validate_permission_record(
                {
                    "version": 2,
                    "configured_mode": "safe",
                    "effective_mode": "safe",
                    "selection_source": "x",
                    "scope": "task",
                    "permission_args": ["--yolo", 3],
                }
            )
        self.assertEqual(raised.exception.code, "invalid_permission_mode")

    def test_configured_mode_dual_reads_unified_then_legacy(self) -> None:
        self.assertEqual(
            configured_permission_mode({"permissions": {"mode": "full"}}),
            ("full", "configured_default"),
        )
        self.assertEqual(
            configured_permission_mode({"permission_mode": "safe"}),
            ("safe", "configured_default"),
        )
        self.assertEqual(
            configured_permission_mode(
                {"permissions": {"mode": "inherit"}, "permission_mode": "full"}
            ),
            ("inherit", "configured_default"),
        )
        self.assertEqual(
            configured_permission_mode({}), ("inherit", "inherit_default")
        )

    def test_inherit_mapping_adds_no_overrides(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                entry = executor_permission_mapping(executor, "inherit")
                self.assertEqual(entry["args"], [])
                self.assertEqual(entry["direct_args"], [])
                self.assertEqual(entry["env"], {})
                self.assertIsNone(entry["decisions"])
                self.assertFalse(entry["overrides_native"])
                self.assertEqual(permission_args_for(executor, "inherit"), [])


class PermissionRegistryConfigTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        self.env_patch = mock.patch.dict(
            os.environ, {"AGENTBC_CONFIG_PATH": str(self.config_path)}
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temp.cleanup()

    def test_apply_permissions_setting_migrates_legacy_key(self) -> None:
        config: dict[str, Any] = {"permission_mode": "safe"}
        changed = apply_permissions_setting(config, "full")
        self.assertTrue(changed)
        self.assertEqual(config["permissions"]["mode"], "full")
        self.assertNotIn("permission_mode", config)

    def test_apply_permissions_setting_noop_when_unchanged(self) -> None:
        config = {"permissions": {"mode": "safe"}}
        changed = apply_permissions_setting(config, "safe")
        self.assertFalse(changed)
        self.assertEqual(config["permissions"]["mode"], "safe")

    def test_atomic_set_writes_unified_key_and_keeps_unknown_values(self) -> None:
        config_path = str(self.config_path)
        from agent_bridge_connect.config import write_config_atomic

        write_config_atomic({"unknown": {"keep": "yes"}, "permission_mode": "safe"}, config_path)
        updated, changed = set_permissions_mode_atomic("full", config_path)
        self.assertTrue(changed)
        self.assertEqual(updated["permissions"]["mode"], "full")
        self.assertNotIn("permission_mode", updated)
        self.assertEqual(updated["unknown"], {"keep": "yes"})
        persisted = load_config(config_path)
        self.assertEqual(persisted["permissions"]["mode"], "full")
        self.assertNotIn("permission_mode", persisted)

    def test_atomic_set_noop_reports_changed_false(self) -> None:
        from agent_bridge_connect.config import write_config_atomic

        write_config_atomic({"permissions": {"mode": "inherit"}}, str(self.config_path))
        _, changed = set_permissions_mode_atomic("inherit", str(self.config_path))
        self.assertFalse(changed)

    def test_validate_config_accepts_permissions_table(self) -> None:
        from agent_bridge_connect.config import validate_config

        self.assertEqual(validate_config({"permissions": {"mode": "safe"}}), [])
        errors = validate_config({"permissions": {"mode": "root"}})
        self.assertTrue(any("permission mode" in error for error in errors))
        self.assertIn("permissions must be a table", validate_config({"permissions": "x"}))


class PermissionRegistryCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        self.env_patch = mock.patch.dict(
            os.environ, {"AGENTBC_CONFIG_PATH": str(self.config_path)}
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temp.cleanup()

    def _run(self, *argv: str) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(argv))
        return code, json.loads(output.getvalue())

    def test_cli_permissions_status_defaults_to_inherit(self) -> None:
        code, payload = self._run("permissions", "status")
        self.assertEqual(code, 0)
        self.assertEqual(payload["configured_mode"], "inherit")
        self.assertEqual(payload["setting"], GLOBAL_PERMISSION_SETTING)
        self.assertIsNone(payload["setting_path"])
        self.assertEqual(payload["scope"], "future_tasks")
        self.assertEqual(payload["priority"], list(RESOLUTION_PRIORITY))
        self.assertEqual(set(payload["mapping"]), {"codex", "claude", "hermes"})
        self.assertEqual(payload["legacy_safe_default"], "safe")

    def test_cli_permissions_status_reflects_legacy_and_mapping(self) -> None:
        from agent_bridge_connect.config import write_config_atomic

        write_config_atomic({"permission_mode": "safe"}, str(self.config_path))
        code, payload = self._run("permissions", "status")
        self.assertEqual(code, 0)
        self.assertEqual(payload["configured_mode"], "safe")
        self.assertEqual(payload["setting_path"], LEGACY_PERMISSION_SETTING)
        self.assertTrue(payload["legacy_setting_present"])
        self.assertEqual(
            payload["mapping"]["hermes"]["capability_id"],
            HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
        )
        self.assertEqual(payload["mapping"]["hermes"]["decisions"], ["allow_once", "deny"])
        self.assertEqual(
            payload["mapping"]["codex"]["args"], ["--sandbox", "workspace-write"]
        )

    def test_cli_permissions_set_full_writes_and_migrates(self) -> None:
        from agent_bridge_connect.config import write_config_atomic

        write_config_atomic({"permission_mode": "safe"}, str(self.config_path))
        code, payload = self._run("permissions", "set", "full")
        self.assertEqual(code, 0)
        self.assertEqual(payload["value"], "full")
        self.assertEqual(payload["previous"], "safe")
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["scope"], "future_tasks")
        persisted = load_config(self.config_path)
        self.assertEqual(persisted["permissions"]["mode"], "full")
        self.assertNotIn("permission_mode", persisted)

    def test_cli_permissions_set_inherit_is_accepted(self) -> None:
        code, payload = self._run("permissions", "set", "inherit")
        self.assertEqual(code, 0)
        self.assertEqual(payload["value"], "inherit")
        self.assertTrue(payload["changed"])

    def test_cli_permissions_set_same_value_reports_unchanged(self) -> None:
        self._run("permissions", "set", "safe")
        code, payload = self._run("permissions", "set", "safe")
        self.assertEqual(code, 0)
        self.assertFalse(payload["changed"])
        self.assertEqual(payload["source"], "configured")

    def test_cli_permissions_unknown_mode_rejected_by_parser(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit):
                main(["permissions", "set", "root"])

    def test_setup_and_permissions_share_one_source(self) -> None:
        """setup --permission-mode and permissions set write the same key."""
        from agent_bridge_connect import setup

        code, payload = self._run("permissions", "set", "safe")
        self.assertEqual(code, 0)
        agents = [
            {
                "name": "hermes",
                "found": True,
                "supported_executor": True,
                "display": "Hermes",
                "path": "/bin/true",
                "binary": "/bin/true",
                "source": "configured",
                "version": "test",
                "capability_level": "L2",
                "capabilities": {},
                "skill": {"installed": True, "up_to_date": True},
            }
        ]
        skill_result = {"installed": True, "changed": False, "status": "already_installed"}
        with (
            mock.patch.dict(
                os.environ,
                {"AGENTBC_CONFIG_PATH": str(self.config_path)},
                clear=False,
            ),
            mock.patch.object(setup, "scan_all_agents", return_value=agents),
            mock.patch.object(setup, "_print_scan_report"),
            mock.patch.object(setup, "install_hermes_skill", return_value=skill_result),
            mock.patch.object(setup, "install_claude_skill", return_value=skill_result),
            mock.patch.object(setup, "_configure_alias", return_value={"status": "skipped"}),
            mock.patch.object(setup, "discover_codex", return_value={"found": False}),
            mock.patch.object(setup, "probe_codex", return_value={}),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                result = setup.run_setup(interactive=False, permission_mode="full")
        self.assertTrue(result["ok"])
        persisted = load_config(self.config_path)
        self.assertEqual(persisted["permissions"]["mode"], "full")
        self.assertNotIn("permission_mode", persisted)
        self.assertEqual(result["permission_mode"], "full")


class ExecutorCapabilityMappingTests(unittest.TestCase):
    def test_three_executors_x_three_modes_mapping_table(self) -> None:
        expectations = {
            ("codex", "inherit"): ([], "codex.inherit"),
            ("codex", "safe"): (["--sandbox", "workspace-write"], "codex.sandbox_workspace_write"),
            ("codex", "full"): (
                ["--dangerously-bypass-approvals-and-sandbox"],
                "codex.bypass_approvals_and_sandbox",
            ),
            ("claude", "inherit"): ([], "claude.inherit"),
            ("claude", "safe"): (
                ["--safe-mode", "--permission-mode", "acceptEdits"],
                "claude.safe_mode_accept_edits",
            ),
            ("claude", "full"): (
                ["--dangerously-skip-permissions"],
                "claude.dangerously_skip_permissions",
            ),
            ("hermes", "inherit"): ([], "hermes.acp.inherit"),
            ("hermes", "safe"): ([], HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID),
            ("hermes", "full"): ([], HERMES_ACP_FULL_YOLO_ENV_CAPABILITY_ID),
        }
        for (executor, mode), (args, capability_id) in expectations.items():
            with self.subTest(executor=executor, mode=mode):
                entry = executor_permission_mapping(executor, mode)
                self.assertEqual(entry["args"], args)
                self.assertEqual(entry["capability_id"], capability_id)
                self.assertEqual(entry["scope"], "executor_subprocess")
                if mode == "full":
                    self.assertTrue(entry["overrides_native"])
                else:
                    self.assertFalse(entry["overrides_native"])

    def test_hermes_full_env_is_subprocess_scoped_and_auditable(self) -> None:
        entry = executor_permission_mapping("hermes", "full")
        self.assertEqual(entry["transport"], TRANSPORT_HERMES_ACP)
        self.assertEqual(entry["env"], {"HERMES_YOLO_MODE": "1"})
        self.assertEqual(entry["args"], [])
        # The frozen direct transport keeps its documented flag.
        self.assertEqual(entry["direct_args"], ["--yolo"])
        from agent_bridge_connect.executors.hermes import hermes_acp_yolo_env

        self.assertEqual(hermes_acp_yolo_env(), {"HERMES_YOLO_MODE": "1"})

    def test_hermes_safe_never_impersonates_with_safe_mode_or_accept_hooks(self) -> None:
        entry = executor_permission_mapping("hermes", "safe")
        combined = json.dumps(entry)
        self.assertNotIn("--safe-mode", combined)
        self.assertNotIn("--accept-hooks", combined)
        self.assertEqual(entry["args"], [])
        self.assertEqual(entry["decisions"], ["allow_once", "deny"])
        self.assertEqual(entry["env"], {})

    def test_permission_args_never_contain_prompt_or_full_argv(self) -> None:
        # permission_args_for only returns the permission arguments; the
        # prompt, the full argv, tokens and raw output are never present.
        for executor, mode, expected in (
            ("codex", "safe", ["--sandbox", "workspace-write"]),
            ("claude", "full", ["--dangerously-skip-permissions"]),
            ("hermes", "full", []),
            ("hermes", "safe", []),
        ):
            with self.subTest(executor=executor, mode=mode):
                args = permission_args_for(executor, mode)
                self.assertEqual(args, expected)
                self.assertFalse(any("prompt" in arg for arg in args))

    def test_unknown_executor_and_mode_are_unsupported(self) -> None:
        with self.assertRaises(ABCError) as raised:
            executor_permission_mapping("opencode", "safe")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")
        with self.assertRaises(ABCError) as raised:
            executor_permission_mapping("codex", "root")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")
        with self.assertRaises(ABCError) as raised:
            probe_executor_capability("opencode", "safe", "/bin/true")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")
        with self.assertRaises(ABCError) as raised:
            probe_executor_capability("hermes", "root", "/bin/true")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")

    def test_unknown_transport_is_unsupported(self) -> None:
        with self.assertRaises(ABCError) as raised:
            executor_permission_mapping("hermes", "safe", transport="tcp")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")

    def test_permission_mapping_view_covers_all_executors(self) -> None:
        view = permission_mapping_view("full")
        self.assertEqual(set(view), {"codex", "claude", "hermes"})
        self.assertEqual(view["codex"]["mode"], "full")
        self.assertEqual(view["hermes"]["transport"], TRANSPORT_HERMES_ACP)


class HermesACPCapabilityProbeTests(unittest.TestCase):
    def test_acp_probe_success_binds_request_permission(self) -> None:
        with mock.patch(
            "agent_bridge_connect.permission_registry.subprocess.run",
            side_effect=[FAKE_ACP_CHECK, FAKE_ACP_VERSION],
        ) as run:
            report = probe_executor_capability(
                "hermes", "safe", "/usr/local/bin/hermes", transport=TRANSPORT_HERMES_ACP
            )
        self.assertTrue(report["supported"])
        self.assertEqual(report["transport"], TRANSPORT_HERMES_ACP)
        self.assertEqual(report["capability_id"], HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID)
        self.assertEqual(report["evidence"], ["acp_check_ok"])
        self.assertEqual(report["details"]["version"], "0.20.1")
        session = report["details"]["session_request_permission"]
        self.assertEqual(session["state"], "bound")
        self.assertEqual(session["decisions"], ["allow_once", "deny"])
        self.assertEqual(
            session["capability_id"], HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID
        )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ["/usr/local/bin/hermes", "acp", "--check"])
        self.assertEqual(commands[1], ["/usr/local/bin/hermes", "acp", "--version"])

    def test_acp_probe_failure_fails_closed_with_actionable_reason(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="dependencies missing")
        with mock.patch(
            "agent_bridge_connect.permission_registry.subprocess.run", return_value=failed
        ):
            with self.assertRaises(ABCError) as raised:
                probe_executor_capability("hermes", "safe", "/usr/local/bin/hermes")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")
        details = raised.exception.details
        self.assertEqual(details["permission_mode"], "safe")
        self.assertEqual(details["transport"], TRANSPORT_HERMES_ACP)
        # safe must never be approximated to inherit or full on failure.
        self.assertNotEqual(details["permission_mode"], "inherit")
        self.assertNotEqual(details["permission_mode"], "full")

    def test_acp_probe_missing_executable_fails_closed(self) -> None:
        with self.assertRaises(ABCError) as raised:
            probe_executor_capability("hermes", "full", None)
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")
        self.assertEqual(raised.exception.details["reason"], "executable_not_found")

    def test_probe_only_uses_official_acp_cli_never_scans_sessions(self) -> None:
        """The probe never touches Hermes private session storage or logs."""
        with mock.patch(
            "agent_bridge_connect.permission_registry.subprocess.run",
            side_effect=[FAKE_ACP_CHECK, FAKE_ACP_VERSION],
        ) as run:
            probe_hermes_acp("/usr/local/bin/hermes")
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:2], ["/usr/local/bin/hermes", "acp"])
            self.assertIn(command[2], {"--check", "--version"})

    def test_hermes_inherit_probe_requires_no_capability(self) -> None:
        report = probe_executor_capability("hermes", "inherit", None)
        self.assertTrue(report["supported"])
        self.assertEqual(report["evidence"], ["no_overrides"])

    def test_cli_transport_probe_fails_closed_without_documented_flag(self) -> None:
        completed = mock.Mock(returncode=0, stdout="usage without full flag", stderr="")
        with mock.patch(
            "agent_bridge_connect.permission_modes.subprocess.run", return_value=completed
        ):
            with self.assertRaises(ABCError) as raised:
                probe_executor_capability("codex", "full", "/usr/local/bin/codex")
        self.assertEqual(raised.exception.code, "permission_capability_unsupported")

    def test_cli_transport_probe_success(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="--dangerously-bypass-approvals-and-sandbox",
            stderr="",
        )
        with mock.patch(
            "agent_bridge_connect.permission_modes.subprocess.run", return_value=completed
        ):
            report = probe_executor_capability("codex", "full", "/usr/local/bin/codex")
        self.assertTrue(report["supported"])
        self.assertEqual(report["evidence"], ["cli_help_verified"])


class PermissionAuditPayloadTests(unittest.TestCase):
    def test_full_audit_records_only_permission_args_and_env(self) -> None:
        record = build_permission_record(explicit_mode="full")
        payload = build_permission_audit_payload(record, executor="hermes")
        self.assertEqual(payload["version"], PERMISSION_SCHEMA_VERSION)
        self.assertEqual(payload["executor"], "hermes")
        self.assertEqual(payload["transport"], TRANSPORT_HERMES_ACP)
        self.assertEqual(payload["mode"], "full")
        self.assertEqual(payload["selection_source"], "explicit_task")
        self.assertEqual(
            payload["capability_id"], HERMES_ACP_FULL_YOLO_ENV_CAPABILITY_ID
        )
        self.assertEqual(payload["permission_args"], [])
        self.assertEqual(payload["env"], {"HERMES_YOLO_MODE": "1"})
        self.assertIsNone(payload["decisions"])
        serialized = json.dumps(payload)
        for forbidden in ("prompt", "token", "argv", "output", "session"):
            self.assertNotIn(forbidden, serialized)

    def test_codex_full_audit_records_only_permission_args(self) -> None:
        record = build_permission_record(explicit_mode="full")
        payload = build_permission_audit_payload(record, executor="codex")
        self.assertEqual(
            payload["permission_args"],
            ["--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertEqual(payload["env"], {})
        serialized = json.dumps(payload)
        for forbidden in ("prompt", "token", "output"):
            self.assertNotIn(forbidden, serialized)


class HermesExecutorACPMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_hermes = self.root / "hermes"
        self.fake_hermes.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = acp ] && [ \"$2\" = --check ]; then echo 'Hermes ACP check OK'; exit 0; fi\n"
            "if [ \"$1\" = acp ] && [ \"$2\" = --version ]; then echo '0.20.1'; exit 0; fi\n"
            "echo ok\n",
            encoding="utf-8",
        )
        self.fake_hermes.chmod(self.fake_hermes.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_get_extensions_freezes_acp_transport_and_capability_id(self) -> None:
        from agent_bridge_connect.executors.hermes import HermesExecutor

        executor = HermesExecutor(command=str(self.fake_hermes), transport="direct")
        executor._version = "0.20.1"
        details = executor.get_extensions()["executor"]["hermes"]
        acp = details["acp"]
        self.assertEqual(acp["transport"], TRANSPORT_HERMES_ACP)
        self.assertEqual(acp["capability_id"], HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID)
        self.assertTrue(acp["check"]["ok"])
        self.assertEqual(acp["check"]["version"], "0.20.1")
        self.assertEqual(acp["request_permission"]["state"], "bound")
        self.assertEqual(acp["request_permission"]["decisions"], ["allow_once", "deny"])
        self.assertEqual(
            acp["request_permission"]["capability_id"],
            HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
        )
        # No run yet: no permission_capability metadata.
        self.assertNotIn("permission_capability", details)

    def test_get_extensions_records_full_mode_audit(self) -> None:
        from agent_bridge_connect.executors.hermes import HermesExecutor

        executor = HermesExecutor(command=str(self.fake_hermes), transport="direct")
        executor._last_run_id = "full-run"
        executor._task_packets["full-run"] = {
            "extensions": {PERMISSION_EXTENSION_KEY: build_permission_record(explicit_mode="full")}
        }
        executor._run_metadata["full-run"] = {"run_id": "full-run"}
        details = executor.get_extensions()["executor"]["hermes"]
        capability = details["permission_capability"]
        self.assertEqual(capability["transport"], TRANSPORT_HERMES_ACP)
        self.assertEqual(capability["capability_id"], HERMES_ACP_FULL_YOLO_ENV_CAPABILITY_ID)
        self.assertEqual(capability["env"], {"HERMES_YOLO_MODE": "1"})
        audit = details["permission_audit"]
        self.assertEqual(audit["mode"], "full")
        self.assertEqual(audit["permission_args"], [])
        self.assertEqual(audit["env"], {"HERMES_YOLO_MODE": "1"})
        # The frozen task record itself is never mutated by run metadata.
        self.assertEqual(
            executor._task_packets["full-run"]["extensions"][PERMISSION_EXTENSION_KEY][
                "permission_args"
            ],
            [],
        )

    def test_acp_capability_is_cached_per_instance(self) -> None:
        from agent_bridge_connect.executors.hermes import HermesExecutor

        executor = HermesExecutor(command=str(self.fake_hermes), transport="direct")
        first = executor.acp_capability()
        second = executor.acp_capability()
        self.assertIs(first, second)
        self.assertTrue(first["ok"])
        self.assertEqual(first["capability_id"], HERMES_ACP_CHECK_CAPABILITY_ID)


class PermissionsStatusPayloadTests(unittest.TestCase):
    def test_status_payload_mapping_follows_configured_mode(self) -> None:
        payload = permissions_status_payload({"permissions": {"mode": "full"}})
        self.assertEqual(payload["configured_mode"], "full")
        self.assertEqual(payload["setting_path"], GLOBAL_PERMISSION_SETTING)
        self.assertFalse(payload["legacy_setting_present"])
        self.assertEqual(
            payload["mapping"]["hermes"]["env"], {"HERMES_YOLO_MODE": "1"}
        )
        self.assertEqual(
            payload["mapping"]["claude"]["args"], ["--dangerously-skip-permissions"]
        )
        self.assertEqual(payload["effective_mode_for_new_tasks"], "full")

    def test_status_payload_flags_legacy_setting_for_migration(self) -> None:
        payload = permissions_status_payload({"permission_mode": "safe"})
        self.assertEqual(payload["setting_path"], LEGACY_PERMISSION_SETTING)
        self.assertTrue(payload["legacy_setting_present"])
        self.assertEqual(payload["configured_mode"], "safe")


if __name__ == "__main__":
    unittest.main()
