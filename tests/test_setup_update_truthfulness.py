"""Setup --update truthfulness and managed Skill upgrade regression tests.

Covers the non-interactive contract: a selected Skill that is blocked
(modified_requires_confirmation), fails to install, or is still not current
after the action makes the update result ok=false and the CLI exit nonzero,
while interactive Decline semantics are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect import __version__
from agent_bridge_connect.doctor import build_doctor_report
from agent_bridge_connect.skill_packages import (
    build_skill_manifest,
    serialize_skill_manifest,
)


class _FakeDistribution:
    def read_text(self, filename: str) -> str | None:
        return None if filename != "direct_url.json" else None


def _healthy_executor_probe(platform: str) -> dict:
    return {
        "resolved": True,
        "source": "path",
        "version": f"{platform}-9.9.9",
        "probe": "ok",
        "auth": {"key_env": "AGENTBC_TEST_KEY", "configured": True, "present": False},
        "capability": {
            "level": 3,
            "structured_output": True,
            "resume": True,
            "cancel": False,
            "input_required": True,
        },
    }


class SetupUpdateTruthfulnessTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.test_dir)

    def _install(self, platform: str, root: Path, **kwargs):
        from agent_bridge_connect.setup import (
            install_claude_skill,
            install_codex_skill,
            install_hermes_skill,
        )

        if platform == "codex":
            return install_codex_skill(root, interactive=False, **kwargs)
        destination = root / "SKILL.md"
        if platform == "claude":
            return install_claude_skill(destination, interactive=False, **kwargs)
        return install_hermes_skill(
            destination,
            interactive=False,
            all_profiles=False,
            **kwargs,
        )

    def _state(self, platform: str, root: Path):
        from agent_bridge_connect.setup import _classify_installed_skill

        return _classify_installed_skill(root, platform)

    def _agent(self, name: str, skill_path: Path) -> dict:
        return {
            "name": name,
            "display": name,
            "found": True,
            "supported_executor": True,
            "path": f"/fake/{name}",
            "binary": name,
            "version": "test",
            "source": "test",
            "capability_level": "L2",
            "capabilities": {},
            "skill": {
                "installed": True,
                "up_to_date": False,
                "path": str(skill_path),
            },
        }

    def _env(self, home: Path, config: Path, **roots) -> dict:
        env = {
            "HOME": str(home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(config),
        }
        env.update({key: str(value) for key, value in roots.items()})
        return env

    def test_noninteractive_update_modified_skill_returns_ok_false(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "update-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "SKILL.md").write_text("custom update content\n", encoding="utf-8")
        config = home / "config.toml"
        env = self._env(
            home,
            config,
            AGENTBC_CODEX_SKILL_PATH=root,
        )
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "_print_selectable_items"
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
        env = self._env(
            home,
            config,
            AGENTBC_CODEX_SKILL_PATH=root,
        )
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "_print_selectable_items"
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
        env = self._env(
            home,
            config,
            AGENTBC_CODEX_SKILL_PATH=root,
        )
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "_print_selectable_items"
        ), mock.patch.object(
            setup,
            "_write_current_skill_package",
            side_effect=OSError("simulated install failure"),
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

        def write_stale(root: Path, platform: str, state: dict) -> None:
            (root / "SKILL.md").write_bytes(b"stale managed content\n")

        config = home / "config.toml"
        env = self._env(
            home,
            config,
            AGENTBC_CODEX_SKILL_PATH=root,
        )
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "_print_selectable_items"
        ), mock.patch.object(setup, "_write_current_skill_package", side_effect=write_stale):
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
        env = self._env(
            home,
            config,
            AGENTBC_CODEX_SKILL_PATH=root,
        )
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", return_value=[self._agent("codex", root / "SKILL.md")]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "_print_selectable_items"
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

        # Base profile carries an intact, genuine 1.0.2a1 managed package;
        # the dev profile carries a user-modified Skill.
        current_files = setup._current_skill_files("hermes")
        for relative_path, content in current_files.items():
            target = base_skill / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = build_skill_manifest("hermes", "1.0.2a1", current_files)
        (base_skill / ".agentbc-skill.json").write_bytes(
            serialize_skill_manifest(manifest)
        )
        profile_skill.mkdir(parents=True)
        (profile_skill / "SKILL.md").write_text("user customization\n", encoding="utf-8")

        env = {
            "HOME": str(home),
            "PATH": "",
            "HERMES_HOME": str(base),
            "AGENTBC_CONFIG_PATH": str(home / "config.toml"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = setup.install_hermes_skill(interactive=False, all_profiles=True)

        self.assertEqual(result["status"], "modified_requires_confirmation")
        self.assertEqual(
            result["classifications"][str(base_skill / "SKILL.md")],
            "managed_outdated",
        )
        self.assertEqual(
            result["classifications"][str(profile_skill / "SKILL.md")],
            "modified",
        )
        # All-or-nothing prevalidation: the intact older profile was not written.
        self.assertEqual((base_skill / "SKILL.md").read_bytes(), current_files["SKILL.md"])
        self.assertEqual((profile_skill / "SKILL.md").read_text(encoding="utf-8"), "user customization\n")
        self.assertTrue((base_skill / ".agentbc-skill.json").is_file())


class DoctorManagedOutdatedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
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
        self.config.write_text(
            f"workspace_root = {json.dumps(str(self.workspace))}\n",
            encoding="utf-8",
        )
        self.build_info = self.module.with_name("_build_info.json")
        self.build_info.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package_version": __version__,
                    "commit_sha": "a" * 40,
                    "source_tree_sha256": "b" * 64,
                    "build_source": "release",
                    "built_at_utc": "2026-08-02T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    def _runner_health(self) -> dict:
        return {
            "ok": True,
            "status": "ready",
            "pid": 1234,
            "python_executable": str(self.python),
            "module_path": str(self.module),
            "executors": ["hermes", "codex"],
        }

    def _report_for(
        self,
        skill_roots: dict[str, str | Path],
        current_files: dict[str, dict[str, bytes]],
    ) -> dict:
        return build_doctor_report(
            config_path=self.config,
            runner_health=self._runner_health,
            module_path=self.module,
            executable_path=self.executable,
            python_executable=self.python,
            distribution=_FakeDistribution(),
            candidate_marker_paths=[],
            build_info_path=self.build_info,
            board_root=self.record,
            skill_roots=skill_roots,
            skill_current_files=current_files,
            executor_probe=_healthy_executor_probe,
        )

    def test_doctor_reports_managed_outdated_as_intact_older_package(self):
        from agent_bridge_connect.setup import _current_skill_files

        current_files = _current_skill_files("codex")
        root = self.root / "skills-codex"
        root.mkdir()
        for relative_path, content in current_files.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = build_skill_manifest("codex", "1.0.2a1", current_files)
        (root / ".agentbc-skill.json").write_bytes(serialize_skill_manifest(manifest))

        skill_roots: dict[str, str | Path] = {
            "codex": root,
            "claude": self.root / "missing-claude",
            "hermes": self.root / "missing-hermes",
        }
        current_files_by_platform = {
            "codex": current_files,
            "claude": _current_skill_files("claude"),
            "hermes": _current_skill_files("hermes"),
        }
        report = self._report_for(skill_roots, current_files_by_platform)

        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "managed_outdated")
        self.assertFalse(entry["up_to_date"])
        self.assertTrue(entry["installed"])
        self.assertEqual(entry["package_version"], "1.0.2a1")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("intact managed package", entry["reason"])
        self.assertIn("agentbc setup --update", entry["remediation"])
        # The current classification meaning is unchanged for other platforms.
        self.assertEqual(report["skills"]["claude"]["classification"], "missing")
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
