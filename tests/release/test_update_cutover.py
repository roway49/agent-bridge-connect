"""Task-5 (PERM-103-005) supported-update preflight and maintenance tests.

The new-task / new-runtime tests here never issue or consume permission
grants and never detect permission markers outside the cutover gate: the
supported ``agentbc update`` preflight either returns
``legacy_permission_cutover_blocked`` with per-task evidence or, only when
the old version is explicitly cleared, ``cutover-ready``.  A manual
wheel/bundle bypass enters maintenance mode that permits doctor/status/
report and explicit termination only and blocks create/dispatch until the
board is cleared.
"""

from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.cli import main
from agent_bridge_connect.migration import assert_maintenance_command_allowed, is_maintenance_mode, maintenance_mode_view
from agent_bridge_connect.permission_grants import build_permission_grant
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.update import CUTOVER_READY_FILE, CUTOVER_READY_STATE, cutover_ready_stamp, manual_bypass_install, update_preflight
import json as _setup_update_json
import os as _setup_update_os
import shutil as _setup_update_shutil
import tempfile as _setup_update_tempfile
import unittest as _setup_update_unittest
from pathlib import Path as _setup_update_Path
from unittest import mock as _setup_update_mock
from agent_bridge_connect import __version__ as _setup_update___version__
from agent_bridge_connect.doctor import build_doctor_report as _setup_update_build_doctor_report
from agent_bridge_connect.skill_packages import build_skill_manifest as _setup_update_build_skill_manifest, serialize_skill_manifest as _setup_update_serialize_skill_manifest

SESSION_ID = "019feed0-0000-7000-8000-0000000000bb"


class _Harness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})

    def close(self) -> None:
        self.temp.cleanup()

    def create(self, status: str = "pending") -> str:
        task = self.service.create_task("task5 update task", "claude", [{"id": 1, "description": "one step"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        if status != "pending":
            self.set_status(task.id, status)
        return task.id

    def set_status(self, task_id: str, status: str) -> None:
        task = self.service.get_task(task_id)
        task.status = status
        self.service.store.write_task(task_id, task.to_dict())

    def inject_issued_grant(self, task_id: str) -> None:
        grant = build_permission_grant(executor="claude", task_id=task_id, input_id="input-g", session_id=SESSION_ID, source_run_id="claude-run-1")
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.permission_grant"] = grant
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())

    def inject_permission_marker(self, task_id: str) -> None:
        task = self.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions["agentbc.input"] = {"input_id": "input-m", "type": "permission", "requested_permission": "full", "status": "waiting", "summary": "legacy marker"}
        task.extensions = extensions
        self.service.store.write_task(task_id, task.to_dict())


class UpdatePreflightTests(unittest.TestCase):
    def test_clear_board_produces_cutover_ready(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        result = update_preflight(harness.service)
        self.assertEqual(result["state"], CUTOVER_READY_STATE)
        self.assertTrue(result["cutover_ready"])
        self.assertEqual(result["code"], "")
        self.assertEqual(result["blockers"], [])
        self.assertFalse(result["maintenance_active"])
        stamp = result["stamp"]
        self.assertTrue(stamp["cutover_ready"])
        self.assertIn("installed_version", stamp)
        self.assertEqual(cutover_ready_stamp(harness.service)["state"], CUTOVER_READY_STATE)
        self.assertTrue((harness.board / CUTOVER_READY_FILE).is_file())

    def test_pending_task_blocks_with_task_evidence(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="pending")
        result = update_preflight(harness.service)
        self.assertEqual(result["state"], "legacy_permission_cutover_blocked")
        self.assertEqual(result["code"], "legacy_permission_cutover_blocked")
        self.assertFalse(result["cutover_ready"])
        self.assertEqual(result["blockers"], [{"task_id": task_id, "status": "pending", "reasons": ["old_channel_status:pending"]}])

    def test_every_legacy_nonterminal_status_blocks(self) -> None:
        for status in ("pending", "running", "input_required", "needs_recovery"):
            with self.subTest(status=status):
                harness = _Harness()
                self.addCleanup(harness.close)
                harness.create(status=status)
                result = update_preflight(harness.service)
                self.assertFalse(result["cutover_ready"])
                self.assertEqual(result["code"], "legacy_permission_cutover_blocked")
                self.assertEqual(result["blockers"][0]["reasons"], [f"old_channel_status:{status}"])

    def test_unconsumed_grant_and_marker_are_task_evidence(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        harness.inject_permission_marker(task_id)
        result = update_preflight(harness.service)
        self.assertFalse(result["cutover_ready"])
        reasons = result["blockers"][0]["reasons"]
        self.assertIn("unconsumed_permission_grant", reasons)
        self.assertIn("legacy_permission_marker", reasons)

    def test_only_explicitly_cleared_old_version_is_cutover_ready(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        self.assertFalse(update_preflight(harness.service)["cutover_ready"])
        task = harness.service.get_task(task_id)
        extensions = dict(task.extensions or {})
        extensions.pop("agentbc.permission_grant", None)
        extensions.pop("agentbc.input", None)
        task.extensions = extensions
        task.status = "completed"
        harness.service.store.write_task(task_id, task.to_dict())
        result = update_preflight(harness.service)
        self.assertEqual(result["state"], CUTOVER_READY_STATE)
        self.assertTrue(result["cutover_ready"])

    def test_terminal_history_is_not_rewritten_by_preflight(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="running")
        harness.inject_issued_grant(task_id)
        harness.set_status(task_id, "failed")
        extensions_before = harness.service.get_task(task_id).extensions
        update_preflight(harness.service)
        extensions_after = harness.service.get_task(task_id).extensions
        self.assertEqual(extensions_after, extensions_before)


class ManualBypassMaintenanceTests(unittest.TestCase):
    def test_bypass_enters_maintenance_that_blocks_create_and_dispatch(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        result = manual_bypass_install(harness.service)
        self.assertEqual(result["state"], "legacy_permission_cutover_maintenance")
        self.assertTrue(result["maintenance"]["active"])
        self.assertTrue(is_maintenance_mode(harness.service))
        for command in ("doctor", "status", "report", "cancel", "close", "delete"):
            assert_maintenance_command_allowed(harness.service, command)
        with self.assertRaises(ABCError) as create_error:
            harness.service.create_task("blocked", "claude", [{"id": 1, "description": "step"}], customer_dir=True, customer_path=harness.project)
        self.assertEqual(create_error.exception.code, "legacy_permission_cutover_maintenance")
        with self.assertRaises(ABCError) as dispatch_error:
            assert_maintenance_command_allowed(harness.service, "dispatch")
        self.assertEqual(dispatch_error.exception.code, "legacy_permission_cutover_maintenance")

    def test_supported_update_after_clearing_exits_maintenance(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        task_id = harness.create(status="pending")
        manual_bypass_install(harness.service)
        self.assertTrue(is_maintenance_mode(harness.service))
        harness.set_status(task_id, "cancelled")
        result = update_preflight(harness.service)
        self.assertTrue(result["cutover_ready"])
        self.assertFalse(is_maintenance_mode(harness.service))
        self.assertFalse(maintenance_mode_view(harness.service)["active"])

    def test_blocked_supported_update_keeps_maintenance(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="running")
        manual_bypass_install(harness.service)
        result = update_preflight(harness.service)
        self.assertFalse(result["cutover_ready"])
        self.assertTrue(result["maintenance_active"])
        self.assertTrue(is_maintenance_mode(harness.service))


class UpdateCliTests(unittest.TestCase):
    @staticmethod
    def _available() -> dict:
        from tests.support.updates import available_release

        return available_release()

    def test_cli_update_blocked_returns_code_and_evidence(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="pending")
        with mock.patch("agent_bridge_connect.update._local_install_strategy", return_value={"method": "managed"}), mock.patch("agent_bridge_connect.update.check_for_update", side_effect=self._available), mock.patch("builtins.input", return_value="y"):
            code = main(["update", "--root", str(harness.board)])
        self.assertEqual(code, 1)
        self.assertFalse((harness.board / CUTOVER_READY_FILE).is_file())

    def test_cli_homebrew_route_never_constructs_task_service(self) -> None:
        with mock.patch("agent_bridge_connect.update._local_install_strategy", return_value={"method": "homebrew", "reason": "Homebrew owns the CLI"}), mock.patch("agent_bridge_connect.cli._task_service", side_effect=AssertionError("Homebrew route must not touch the board")) as task_service:
            code = main(["update"])
        self.assertEqual(code, 0)
        task_service.assert_not_called()

    def test_cli_update_cutover_ready(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        with (
            mock.patch("agent_bridge_connect.update._local_install_strategy", return_value={"method": "managed"}),
            mock.patch("agent_bridge_connect.update.check_for_update", side_effect=self._available),
            mock.patch("agent_bridge_connect.update.install_verified_release", return_value={"version": "1.0.4a1"}),
            mock.patch("builtins.input", return_value="y"),
        ):
            code = main(["update", "--root", str(harness.board)])
        self.assertEqual(code, 0)
        self.assertTrue((harness.board / CUTOVER_READY_FILE).is_file())
        stamp = cutover_ready_stamp(harness.service) or {}
        self.assertEqual(stamp.get("state"), CUTOVER_READY_STATE)

    def test_cli_bypass_enters_maintenance_and_blocks_create(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        code = main(["update", "--root", str(harness.board), "--bypass"])
        self.assertEqual(code, 0)
        self.assertTrue(is_maintenance_mode(harness.service))
        steps = harness.root / "steps.yaml"
        steps.write_text("steps:\n  - id: 1\n    description: step\n", encoding="utf-8")
        create_code = main(["task", "create", "--root", str(harness.board), "--title", "blocked", "--assignee", "claude", "--steps", str(steps), "--customer-path", str(harness.project)])
        self.assertEqual(create_code, 1)

    def test_cli_update_blocked_output_is_stable_json(self) -> None:
        harness = _Harness()
        self.addCleanup(harness.close)
        harness.create(status="needs_recovery")
        with mock.patch("agent_bridge_connect.update._local_install_strategy", return_value={"method": "managed"}), mock.patch("agent_bridge_connect.update.check_for_update", side_effect=self._available), mock.patch("builtins.input", return_value="y"):
            code = main(["update", "--root", str(harness.board)])
        self.assertEqual(code, 1)
        stamp = cutover_ready_stamp(harness.service)
        self.assertIsNone(stamp)


def _setup_update__frozen_102a1_files(platform: str) -> dict[str, bytes]:
    """Reconstruct the genuine 1.0.2a1 skill files (hash-verified).

    PERM-104-002 T2A3-001: the current package is newer and its controller
    contract changed after 1.0.2a1. Those contract bytes are preserved as a fixture;
    byte-identical files come from the current package.
    """
    from agent_bridge_connect.setup import _current_skill_files
    from agent_bridge_connect.skill_packages import MANAGED_SKILL_FINGERPRINTS, sha256_bytes

    fingerprint = MANAGED_SKILL_FINGERPRINTS["1.0.2a1"][platform]
    frozen_override = {
        "SKILL.md": _setup_update_Path(__file__).parents[1] / "fixtures" / "skill" / "1.0.2a1" / "codex-SKILL.md" if platform == "codex" else _setup_update_Path(""),
        "references/controller-contract.md": _setup_update_Path(__file__).parents[1] / "fixtures" / "skill" / "1.0.2a1" / "controller-contract.md",
    }
    current = _current_skill_files(platform)
    files: dict[str, bytes] = {}
    for relative_path, digest in fingerprint["files"].items():
        override = frozen_override.get(relative_path)
        if override is not None and override.is_file():
            candidate = override.read_bytes()
        else:
            candidate = current[relative_path]
        if sha256_bytes(candidate) != digest:
            raise AssertionError(f"frozen 1.0.2a1 bytes unavailable for {platform}:{relative_path}")
        files[relative_path] = candidate
    return files


class _setup_update__FakeDistribution:
    def read_text(self, filename: str) -> str | None:
        return None if filename != "direct_url.json" else None


def _setup_update__healthy_executor_probe(platform: str) -> dict:
    return {"resolved": True, "source": "path", "version": f"{platform}-9.9.9", "probe": "ok", "auth": {"key_env": "AGENTBC_TEST_KEY", "configured": True, "present": False}, "capability": {"level": 3, "structured_output": True, "resume": True, "cancel": False, "input_required": True}}


class _setup_update_SetupUpdateTruthfulnessTests(_setup_update_unittest.TestCase):
    def setUp(self):
        self.test_dir = _setup_update_Path(_setup_update_tempfile.mkdtemp())
        self.addCleanup(_setup_update_shutil.rmtree, self.test_dir)

    def _install(self, platform: str, root: _setup_update_Path, **kwargs):
        from agent_bridge_connect.setup import install_claude_skill, install_codex_skill, install_hermes_skill

        if platform == "codex":
            return install_codex_skill(root, interactive=False, **kwargs)
        destination = root / "SKILL.md"
        if platform == "claude":
            return install_claude_skill(destination, interactive=False, **kwargs)
        return install_hermes_skill(destination, interactive=False, all_profiles=False, **kwargs)

    def _state(self, platform: str, root: _setup_update_Path):
        from agent_bridge_connect.setup import _classify_installed_skill

        return _classify_installed_skill(root, platform)

    def _agent(self, name: str, skill_path: _setup_update_Path) -> dict:
        return {"name": name, "display": name, "found": True, "supported_executor": True, "path": f"/fake/{name}", "binary": name, "version": "test", "source": "test", "capability_level": "L2", "capabilities": {}, "skill": {"installed": True, "up_to_date": False, "path": str(skill_path)}}

    def _env(self, home: _setup_update_Path, config: _setup_update_Path, **roots) -> dict:
        env = {"HOME": str(home), "PATH": "", "AGENTBC_CONFIG_PATH": str(config)}
        env.update({key: str(value) for (key, value) in roots.items()})
        return env

    def test_noninteractive_update_modified_skill_returns_ok_false(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "update-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "SKILL.md").write_text("custom update content\n", encoding="utf-8")
        config = home / "config.toml"
        env = self._env(home, config, AGENTBC_CODEX_SKILL_PATH=root)
        with (
            _setup_update_mock.patch.dict(_setup_update_os.environ, env, clear=True),
            _setup_update_mock.patch.object(setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]),
            _setup_update_mock.patch.object(setup, "_print_scan_report"),
            _setup_update_mock.patch.object(setup, "_print_selectable_items"),
        ):
            result = setup.run_update(interactive=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actions"][0]["status"], "modified_requires_confirmation")
        self.assertEqual((root / "SKILL.md").read_text(encoding="utf-8"), "custom update content\n")

    def test_cli_setup_update_noninteractive_exits_nonzero_when_skill_modified(self):
        from agent_bridge_connect import setup
        from agent_bridge_connect.cli import main

        home = self.test_dir / "cli-update-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "SKILL.md").write_text("custom update content\n", encoding="utf-8")
        config = home / "config.toml"
        env = self._env(home, config, AGENTBC_CODEX_SKILL_PATH=root)
        with (
            _setup_update_mock.patch.dict(_setup_update_os.environ, env, clear=True),
            _setup_update_mock.patch.object(setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]),
            _setup_update_mock.patch.object(setup, "_print_scan_report"),
            _setup_update_mock.patch.object(setup, "_print_selectable_items"),
        ):
            code = main(["setup", "--update", "--non-interactive"])
        self.assertEqual(code, 1)
        self.assertEqual((root / "SKILL.md").read_text(encoding="utf-8"), "custom update content\n")

    def test_noninteractive_update_install_failure_returns_ok_false(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "fail-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "references" / "controller-contract.md").unlink()
        config = home / "config.toml"
        env = self._env(home, config, AGENTBC_CODEX_SKILL_PATH=root)
        with (
            _setup_update_mock.patch.dict(_setup_update_os.environ, env, clear=True),
            _setup_update_mock.patch.object(setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]),
            _setup_update_mock.patch.object(setup, "_print_scan_report"),
            _setup_update_mock.patch.object(setup, "_print_selectable_items"),
            _setup_update_mock.patch.object(setup, "_write_current_skill_package", side_effect=OSError("simulated install failure")),
        ):
            result = setup.run_update(interactive=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actions"][0]["status"], "install_failed")
        self.assertIn("simulated install failure", result["actions"][0]["error"])

    def test_noninteractive_update_not_current_after_action_returns_ok_false(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "stale-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "references" / "controller-contract.md").unlink()

        def write_stale(root: _setup_update_Path, platform: str, state: dict) -> None:
            (root / "SKILL.md").write_bytes(b"stale managed content\n")

        config = home / "config.toml"
        env = self._env(home, config, AGENTBC_CODEX_SKILL_PATH=root)
        with (
            _setup_update_mock.patch.dict(_setup_update_os.environ, env, clear=True),
            _setup_update_mock.patch.object(setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]),
            _setup_update_mock.patch.object(setup, "_print_scan_report"),
            _setup_update_mock.patch.object(setup, "_print_selectable_items"),
            _setup_update_mock.patch.object(setup, "_write_current_skill_package", side_effect=write_stale),
        ):
            result = setup.run_update(interactive=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actions"][0]["status"], "installed")
        self.assertEqual(self._state("codex", root)["classification"], "modified")

    def test_noninteractive_update_repairs_partial_and_returns_ok_true(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "repair-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "references" / "controller-contract.md").unlink()
        config = home / "config.toml"
        env = self._env(home, config, AGENTBC_CODEX_SKILL_PATH=root)
        with (
            _setup_update_mock.patch.dict(_setup_update_os.environ, env, clear=True),
            _setup_update_mock.patch.object(setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]),
            _setup_update_mock.patch.object(setup, "_print_scan_report"),
            _setup_update_mock.patch.object(setup, "_print_selectable_items"),
        ):
            result = setup.run_update(interactive=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["actions"][0]["previous_classification"], "partial")
        self.assertEqual(self._state("codex", root)["classification"], "current")

    def test_noninteractive_hermes_modified_profile_blocks_all_profiles(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "hermes-home"
        base = home / ".hermes"
        profile = base / "profiles" / "dev"
        base_skill = base / "skills" / "agentbc"
        profile_skill = profile / "skills" / "agentbc"
        current_files = _setup_update__frozen_102a1_files("hermes")
        for relative_path, content in current_files.items():
            target = base_skill / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = _setup_update_build_skill_manifest("hermes", "1.0.2a1", current_files)
        (base_skill / ".agentbc-skill.json").write_bytes(_setup_update_serialize_skill_manifest(manifest))
        profile_skill.mkdir(parents=True)
        (profile_skill / "SKILL.md").write_text("user customization\n", encoding="utf-8")
        env = {"HOME": str(home), "PATH": "", "HERMES_HOME": str(base), "AGENTBC_CONFIG_PATH": str(home / "config.toml")}
        with _setup_update_mock.patch.dict(_setup_update_os.environ, env, clear=True):
            result = setup.install_hermes_skill(interactive=False, all_profiles=True)
        self.assertEqual(result["status"], "modified_requires_confirmation")
        self.assertEqual(result["classifications"][str(base_skill / "SKILL.md")], "managed_outdated")
        self.assertEqual(result["classifications"][str(profile_skill / "SKILL.md")], "modified")
        self.assertEqual((base_skill / "SKILL.md").read_bytes(), current_files["SKILL.md"])
        self.assertEqual((profile_skill / "SKILL.md").read_text(encoding="utf-8"), "user customization\n")
        self.assertTrue((base_skill / ".agentbc-skill.json").is_file())


class _setup_update_DoctorManagedOutdatedTests(_setup_update_unittest.TestCase):
    def setUp(self):
        self.temporary = _setup_update_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _setup_update_Path(self.temporary.name)
        self.module = self.root / "installed" / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.executable = self.root / "venv" / "bin" / "agentbc"
        self.python = self.root / "venv" / "bin" / "python"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.record = self.root / "record"
        self.record.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(f"workspace_root = {_setup_update_json.dumps(str(self.workspace))}\n", encoding="utf-8")
        self.build_info = self.module.with_name("_build_info.json")
        self.build_info.write_text(_setup_update_json.dumps({"schema_version": 1, "package_version": _setup_update___version__, "commit_sha": "a" * 40, "source_tree_sha256": "b" * 64, "build_source": "release", "built_at_utc": "2026-08-02T00:00:00Z"}), encoding="utf-8")

    def _runner_health(self) -> dict:
        return {"ok": True, "status": "ready", "pid": 1234, "python_executable": str(self.python), "module_path": str(self.module), "executors": ["hermes", "codex"]}

    def _report_for(self, skill_roots: dict[str, str | _setup_update_Path], current_files: dict[str, dict[str, bytes]]) -> dict:
        return _setup_update_build_doctor_report(
            config_path=self.config,
            runner_health=self._runner_health,
            module_path=self.module,
            executable_path=self.executable,
            python_executable=self.python,
            distribution=_setup_update__FakeDistribution(),
            candidate_marker_paths=[],
            build_info_path=self.build_info,
            board_root=self.record,
            skill_roots=skill_roots,
            skill_current_files=current_files,
            executor_probe=_setup_update__healthy_executor_probe,
        )

    def test_doctor_reports_managed_outdated_as_intact_older_package(self):
        from agent_bridge_connect.setup import _current_skill_files

        current_files = _setup_update__frozen_102a1_files("codex")
        root = self.root / "skills-codex"
        root.mkdir()
        for relative_path, content in current_files.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = _setup_update_build_skill_manifest("codex", "1.0.2a1", current_files)
        (root / ".agentbc-skill.json").write_bytes(_setup_update_serialize_skill_manifest(manifest))
        skill_roots: dict[str, str | _setup_update_Path] = {"codex": root, "claude": self.root / "missing-claude", "hermes": self.root / "missing-hermes"}
        current_files_by_platform = {"codex": current_files, "claude": _current_skill_files("claude"), "hermes": _current_skill_files("hermes")}
        report = self._report_for(skill_roots, current_files_by_platform)
        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "managed_outdated")
        self.assertFalse(entry["up_to_date"])
        self.assertTrue(entry["installed"])
        self.assertEqual(entry["package_version"], "1.0.2a1")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("intact managed package", entry["reason"])
        self.assertIn("agentbc setup --update", entry["remediation"])
        self.assertEqual(report["skills"]["claude"]["classification"], "missing")
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["exit_code"], 1)
