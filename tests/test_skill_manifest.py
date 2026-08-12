"""Phase 7 canonical Skill package manifest and classifier regressions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect import __version__
from agent_bridge_connect.skill_packages import (
    LEGACY_SKILL_FINGERPRINTS,
    aggregate_template_sha256,
    build_skill_manifest,
    classify_skill_package,
    serialize_skill_manifest,
    sha256_bytes,
)


class SkillManifestTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.test_dir)

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
        from agent_bridge_connect.setup import _current_skill_files

        return classify_skill_package(
            root,
            platform=platform,
            package_version=__version__,
            current_files=_current_skill_files(platform),
        )

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def test_every_platform_installs_schema_v1_current_package(self):
        expected_files = {
            "codex": {
                "SKILL.md",
                "agents/openai.yaml",
                "references/agentbc-steps-yaml.md",
                "references/controller-contract.md",
            },
            "claude": {
                "SKILL.md",
                "references/agentbc-steps-yaml.md",
                "references/controller-contract.md",
            },
            "hermes": {
                "SKILL.md",
                "references/agentbc-steps-yaml.md",
                "references/controller-contract.md",
            },
        }
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                root = self.test_dir / platform
                result = self._install(platform, root)
                manifest = json.loads((root / ".agentbc-skill.json").read_text(encoding="utf-8"))

                self.assertEqual(result["classification"], "current")
                self.assertEqual(manifest["schema_version"], 1)
                self.assertEqual(manifest["platform"], platform)
                self.assertEqual(manifest["package_version"], __version__)
                self.assertEqual(manifest["protocol_version"], "1.0")
                self.assertEqual(manifest["completion_version"], 1)
                self.assertEqual(set(manifest["files"]), expected_files[platform])
                self.assertNotIn(".agentbc-skill.json", manifest["files"])
                self.assertEqual(list(manifest["files"]), sorted(manifest["files"]))
                for relative_path, digest in manifest["files"].items():
                    self.assertEqual(digest, sha256_bytes((root / relative_path).read_bytes()))
                content = {path: (root / path).read_bytes() for path in manifest["files"]}
                self.assertEqual(manifest["template_sha256"], aggregate_template_sha256(content))
                self.assertEqual(self._state(platform, root)["classification"], "current")

    def test_aggregate_hash_uses_sorted_paths_nuls_and_exact_bytes(self):
        files = {"z/file": b"z\n", "a": b"a\x00b", "middle": b""}
        manual = hashlib.sha256()
        for path in sorted(files):
            manual.update(path.encode("utf-8"))
            manual.update(b"\0")
            manual.update(files[path])
            manual.update(b"\0")
        manifest = build_skill_manifest("codex", "test", files)

        self.assertEqual(manifest["template_sha256"], manual.hexdigest())
        self.assertEqual(list(manifest["files"]), sorted(files))
        encoded = serialize_skill_manifest(manifest)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded, serialize_skill_manifest(manifest))

    def test_legacy_33d3d08_fingerprints_are_frozen(self):
        self.assertEqual(
            LEGACY_SKILL_FINGERPRINTS["codex"]["template_sha256"],
            "ef0f073ef911e5fb0f8092fdda57c204db5dc24e565d9b8367d35830974afea1",
        )
        self.assertEqual(
            LEGACY_SKILL_FINGERPRINTS["claude"]["template_sha256"],
            "aaaa1fb45b9ef6a9c91b4435a0a22c5aedbc5beb89413f7c45e0f2b310ccc36d",
        )
        self.assertEqual(
            LEGACY_SKILL_FINGERPRINTS["hermes"]["template_sha256"],
            "381b9926e7a26cfba2b45c03b3797bd0683de3a022ac2848c3de8954c972668a",
        )

    def test_recognized_legacy_is_upgraded_noninteractively_for_every_platform(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                root = self.test_dir / f"legacy-{platform}"
                legacy_paths = LEGACY_SKILL_FINGERPRINTS[platform]["files"]
                old_files = {
                    path: f"legacy {platform} {path}\n".encode("utf-8")
                    for path in legacy_paths
                }
                for relative_path, content in old_files.items():
                    path = root / relative_path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                fingerprint = {
                    "template_sha256": aggregate_template_sha256(old_files),
                    "files": {path: sha256_bytes(content) for path, content in old_files.items()},
                }
                with mock.patch.dict(LEGACY_SKILL_FINGERPRINTS, {platform: fingerprint}):
                    self.assertEqual(self._state(platform, root)["classification"], "legacy")
                    result = self._install(platform, root)

                self.assertEqual(result["previous_classification"], "legacy")
                self.assertEqual(self._state(platform, root)["classification"], "current")

    def test_missing_partial_and_modified_classifications_cover_every_platform(self):
        from agent_bridge_connect.setup import _current_skill_files

        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                root = self.test_dir / f"states-{platform}"
                self.assertEqual(self._state(platform, root)["classification"], "missing")
                root.mkdir(parents=True)
                (root / "SKILL.md").write_bytes(_current_skill_files(platform)["SKILL.md"])
                self.assertEqual(self._state(platform, root)["classification"], "partial")
                (root / "SKILL.md").write_bytes(b"unknown managed content\n")
                self.assertEqual(self._state(platform, root)["classification"], "modified")

    def test_missing_partial_modified_and_unrelated_files(self):
        from agent_bridge_connect.setup import _current_skill_files, install_codex_skill

        root = self.test_dir / "classifications"
        root.mkdir(parents=True)
        unrelated = root / "notes.txt"
        unrelated.write_text("keep me", encoding="utf-8")
        self.assertEqual(self._state("codex", root)["classification"], "missing")

        files = _current_skill_files("codex")
        (root / "SKILL.md").write_bytes(files["SKILL.md"])
        self.assertEqual(self._state("codex", root)["classification"], "partial")
        repaired = install_codex_skill(root, interactive=False)
        self.assertEqual(repaired["previous_classification"], "partial")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

        (root / "SKILL.md").write_text("user customization\n", encoding="utf-8")
        self.assertEqual(self._state("codex", root)["classification"], "modified")
        refused = install_codex_skill(root, interactive=False)
        self.assertEqual(refused["status"], "modified_requires_confirmation")
        self.assertEqual((root / "SKILL.md").read_text(encoding="utf-8"), "user customization\n")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

    def test_invalid_and_future_manifests_are_modified(self):
        root = self.test_dir / "invalid"
        self._install("claude", root)
        manifest_path = root / ".agentbc-skill.json"
        valid = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = (b"not-json\n", json.dumps({**valid, "schema_version": 2}).encode("utf-8"))
        for content in cases:
            with self.subTest(content=content[:12]):
                manifest_path.write_bytes(content)
                self.assertEqual(self._state("claude", root)["classification"], "modified")

    def test_per_file_and_aggregate_manifest_mismatch_are_modified(self):
        root = self.test_dir / "hash-mismatch"
        self._install("hermes", root)
        manifest_path = root / ".agentbc-skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["template_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self._state("hermes", root)["classification"], "modified")

        self._install("hermes", root, force=True)
        (root / "references" / "controller-contract.md").write_bytes(b"changed\n")
        self.assertEqual(self._state("hermes", root)["classification"], "modified")

    def test_force_and_explicit_confirmation_replace_modified_managed_files(self):
        from agent_bridge_connect.setup import install_claude_skill

        root = self.test_dir / "confirmation"
        destination = root / "SKILL.md"
        install_claude_skill(destination, interactive=False)
        destination.write_text("custom\n", encoding="utf-8")
        with mock.patch("builtins.input", return_value="y"):
            confirmed = install_claude_skill(destination, interactive=True)
        self.assertEqual(confirmed["previous_classification"], "modified")

        destination.write_text("custom again\n", encoding="utf-8")
        forced = install_claude_skill(destination, interactive=False, force=True)
        self.assertEqual(forced["previous_classification"], "modified")
        self.assertEqual(self._state("claude", root)["classification"], "current")

    def test_uninstall_removes_only_managed_files(self):
        from agent_bridge_connect.setup import install_codex_skill, uninstall_codex_skill

        root = self.test_dir / "uninstall"
        install_codex_skill(root, interactive=False)
        unrelated = root / "references" / "personal-notes.md"
        unrelated.write_text("preserve", encoding="utf-8")
        result = uninstall_codex_skill(root)

        self.assertTrue(result["removed"])
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")
        self.assertFalse((root / "SKILL.md").exists())
        self.assertFalse((root / ".agentbc-skill.json").exists())

    def test_forged_manifest_cannot_claim_unrelated_file_for_update_or_uninstall(self):
        from agent_bridge_connect.setup import install_codex_skill, uninstall_codex_skill

        root = self.test_dir / "forged-manifest"
        install_codex_skill(root, interactive=False)
        unrelated = root / "notes.md"
        unrelated.write_text("user-owned\n", encoding="utf-8")
        forged = build_skill_manifest(
            "codex",
            __version__,
            {"notes.md": unrelated.read_bytes()},
        )
        manifest_path = root / ".agentbc-skill.json"
        manifest_path.write_bytes(serialize_skill_manifest(forged))

        state = self._state("codex", root)
        self.assertEqual(state["classification"], "modified")
        self.assertNotIn("notes.md", state["managed_paths"])
        install_codex_skill(root, interactive=False, force=True)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user-owned\n")

        manifest_path.write_bytes(serialize_skill_manifest(forged))
        uninstall_codex_skill(root)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user-owned\n")

    def test_non_regular_managed_target_fails_before_update_or_uninstall(self):
        from agent_bridge_connect.setup import install_codex_skill, uninstall_codex_skill

        update_root = self.test_dir / "directory-target-update"
        skill_directory = update_root / "SKILL.md"
        skill_directory.mkdir(parents=True)
        sentinel = skill_directory / "user.txt"
        sentinel.write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(OSError, "not a regular file"):
            install_codex_skill(update_root, interactive=False, force=True)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse((update_root / ".agentbc-skill.json").exists())

        uninstall_root = self.test_dir / "directory-target-uninstall"
        install_codex_skill(uninstall_root, interactive=False)
        (uninstall_root / "SKILL.md").unlink()
        replacement = uninstall_root / "SKILL.md"
        replacement.mkdir()
        uninstall_sentinel = replacement / "user.txt"
        uninstall_sentinel.write_text("preserve\n", encoding="utf-8")
        before = self._snapshot(uninstall_root)

        with self.assertRaisesRegex(OSError, "not a regular file"):
            uninstall_codex_skill(uninstall_root)
        self.assertEqual(self._snapshot(uninstall_root), before)
        self.assertTrue((uninstall_root / ".agentbc-skill.json").is_file())

    def test_symlinked_managed_parent_fails_before_force_update(self):
        from agent_bridge_connect.setup import install_codex_skill

        root = self.test_dir / "symlink-parent"
        outside = self.test_dir / "outside"
        outside.mkdir()
        root.mkdir()
        (root / "references").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(OSError, "parent is a symlink"):
            install_codex_skill(root, interactive=False, force=True)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((root / ".agentbc-skill.json").exists())

    def test_interrupted_managed_update_rolls_back_all_files(self):
        from agent_bridge_connect import skill_packages
        from agent_bridge_connect.setup import install_codex_skill

        root = self.test_dir / "rollback"
        install_codex_skill(root, interactive=False)
        (root / "SKILL.md").write_text("custom\n", encoding="utf-8")
        (root / "personal.txt").write_text("unrelated\n", encoding="utf-8")
        before = self._snapshot(root)
        original = skill_packages._install_staged_file
        calls = 0

        def fail_second(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted update")
            original(source, destination)

        with mock.patch.object(skill_packages, "_install_staged_file", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "interrupted"):
                install_codex_skill(root, interactive=False, force=True)

        self.assertEqual(self._snapshot(root), before)
        self.assertFalse(any(root.parent.glob(f".{root.name}.agentbc-*-*")))

    def test_setup_show_is_read_only_for_existing_packages(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "home"
        roots = {
            "AGENTBC_CODEX_SKILL_PATH": home / ".codex" / "skills" / "agentbc",
            "AGENTBC_CLAUDE_SKILL_PATH": home / ".claude" / "skills" / "agentbc" / "SKILL.md",
            "AGENTBC_HERMES_SKILL_PATH": home / ".hermes" / "skills" / "agentbc" / "SKILL.md",
        }
        self._install("codex", roots["AGENTBC_CODEX_SKILL_PATH"])
        self._install("claude", roots["AGENTBC_CLAUDE_SKILL_PATH"].parent)
        self._install("hermes", roots["AGENTBC_HERMES_SKILL_PATH"].parent)
        before = self._snapshot(home)
        env = {
            "HOME": str(home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(home / "config.toml"),
            **{key: str(value) for key, value in roots.items()},
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "probe_codex", return_value={}
        ), mock.patch.object(
            setup,
            "discover_codex",
            return_value={
                "found": False,
                "path": "",
                "version": "",
                "source": "not_found",
                "searched_paths": [],
                "manual_override": "",
            },
        ):
            setup.run_show()
        self.assertEqual(self._snapshot(home), before)
        self.assertFalse((home / "config.toml").exists())

    def test_noninteractive_setup_refuses_modified_skill(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "setup-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "SKILL.md").write_text("custom setup content\n", encoding="utf-8")
        config = home / "config.toml"
        agent = {
            "name": "codex",
            "display": "Codex CLI",
            "found": True,
            "supported_executor": True,
            "path": "/fake/codex",
            "binary": "codex",
            "version": "test",
            "source": "test",
            "capability_level": "L2",
            "capabilities": {},
            "skill": {"installed": True, "up_to_date": False},
        }
        env = {
            "HOME": str(home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(config),
            "AGENTBC_CODEX_SKILL_PATH": str(root),
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", side_effect=[[agent], [agent]]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "discover_codex", return_value={"found": True}
        ), mock.patch.object(setup, "probe_codex", return_value={}):
            result = setup.run_setup(interactive=False)

        self.assertEqual(result["skills"]["codex"]["status"], "modified_requires_confirmation")
        self.assertEqual((root / "SKILL.md").read_text(encoding="utf-8"), "custom setup content\n")

    def test_noninteractive_update_repairs_partial_and_refuses_modified(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "update-home"
        root = home / ".codex" / "skills" / "agentbc"
        self._install("codex", root)
        (root / "references" / "controller-contract.md").unlink()
        agent = {
            "name": "codex",
            "display": "Codex CLI",
            "found": True,
            "supported_executor": True,
            "path": "/fake/codex",
            "binary": "codex",
            "version": "test",
            "source": "test",
            "capability_level": "L2",
            "capabilities": {},
            "skill": {
                "installed": True,
                "up_to_date": False,
                "path": str(root / "SKILL.md"),
            },
        }
        env = {
            "HOME": str(home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(home / "config.toml"),
            "AGENTBC_CODEX_SKILL_PATH": str(root),
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            setup, "scan_all_agents", return_value=[agent]
        ), mock.patch.object(setup, "_print_scan_report"), mock.patch.object(
            setup, "_print_selectable_items"
        ):
            repaired = setup.run_update(interactive=False)
            (root / "SKILL.md").write_text("custom update content\n", encoding="utf-8")
            refused = setup.run_update(interactive=False)

        self.assertEqual(repaired["actions"][0]["previous_classification"], "partial")
        self.assertEqual(refused["actions"][0]["status"], "modified_requires_confirmation")
        self.assertEqual((root / "SKILL.md").read_text(encoding="utf-8"), "custom update content\n")

    def test_templates_are_concise_and_package_data_covers_references(self):
        repository_root = Path(__file__).resolve().parents[1]
        skills_root = repository_root / "src" / "agent_bridge_connect" / "skills"
        for skill in ("codex_skill.md", "claude_skill.md", "hermes_skill.md"):
            self.assertLess(len((skills_root / skill).read_text(encoding="utf-8").splitlines()), 500)
        package_config = (repository_root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"skills/references/*.md"', package_config)
        self.assertTrue((skills_root / "references" / "controller-contract.md").is_file())
        openai = (skills_root / "codex_openai.yaml").read_text(encoding="utf-8")
        self.assertIn("shared controller contract", openai)


if __name__ == "__main__":
    unittest.main()
