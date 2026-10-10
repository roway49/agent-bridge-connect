"""Deterministic tests for release provenance: tag/version mapping,
dynamic filenames, build identity, manifest hashes, and no-publish
behaviour.
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import hashlib as _skill_manifest_hashlib
import json as _skill_manifest_json
import os as _skill_manifest_os
import shutil as _skill_manifest_shutil
import tempfile as _skill_manifest_tempfile
import unittest as _skill_manifest_unittest
from pathlib import Path as _skill_manifest_Path
from unittest import mock as _skill_manifest_mock
from agent_bridge_connect import __version__ as _skill_manifest___version__
from agent_bridge_connect.skill_packages import (
    LEGACY_SKILL_FINGERPRINTS as _skill_manifest_LEGACY_SKILL_FINGERPRINTS,
    MANAGED_SKILL_FINGERPRINTS as _skill_manifest_MANAGED_SKILL_FINGERPRINTS,
    aggregate_template_sha256 as _skill_manifest_aggregate_template_sha256,
    build_skill_manifest as _skill_manifest_build_skill_manifest,
    classify_skill_package as _skill_manifest_classify_skill_package,
    serialize_skill_manifest as _skill_manifest_serialize_skill_manifest,
    sha256_bytes as _skill_manifest_sha256_bytes,
)

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
import build_provenance as bp  # noqa: E402


class TagVersionMappingTests(unittest.TestCase):
    """vX.Y.ZS[N]  ⇄  X.Y.ZsN  round-trip and edge cases."""

    def test_tag_to_python(self) -> None:
        self.assertEqual(bp.tag_to_python_version("v1.0.1A"), "1.0.1a1")
        self.assertEqual(bp.tag_to_python_version("v1.0.1A2"), "1.0.1a2")
        self.assertEqual(bp.tag_to_python_version("v1.0.1A3"), "1.0.1a3")
        self.assertEqual(bp.tag_to_python_version("v2.3.4B"), "2.3.4b1")
        self.assertEqual(bp.tag_to_python_version("v2.3.4B12"), "2.3.4b12")
        self.assertEqual(bp.tag_to_python_version("v0.0.0Z"), "0.0.0z1")
        self.assertEqual(bp.tag_to_python_version("v1.0.4A"), "1.0.4a1")

    def test_python_to_tag(self) -> None:
        self.assertEqual(bp.python_to_tag_version("1.0.1a1"), "v1.0.1A")
        self.assertEqual(bp.python_to_tag_version("1.0.1a2"), "v1.0.1A2")
        self.assertEqual(bp.python_to_tag_version("1.0.1a3"), "v1.0.1A3")
        self.assertEqual(bp.python_to_tag_version("2.3.4b1"), "v2.3.4B")
        self.assertEqual(bp.python_to_tag_version("2.3.4b12"), "v2.3.4B12")
        self.assertEqual(bp.python_to_tag_version("0.0.0z1"), "v0.0.0Z")
        self.assertEqual(bp.python_to_tag_version("1.0.4a1"), "v1.0.4A")

    def test_round_trip_tag_first(self) -> None:
        for tag in ("v1.0.1A", "v1.0.1A2", "v1.0.1A3", "v2.3.4B12", "v5.6.7C"):
            with self.subTest(tag=tag):
                self.assertEqual(bp.python_to_tag_version(bp.tag_to_python_version(tag)), tag)

    def test_round_trip_version_first(self) -> None:
        for ver in ("1.0.1a1", "1.0.1a2", "1.0.1a3", "2.3.4b12", "5.6.7c1"):
            with self.subTest(version=ver):
                self.assertEqual(bp.tag_to_python_version(bp.python_to_tag_version(ver)), ver)

    def test_tag_rejects_lowercase_suffix(self) -> None:
        with self.assertRaises(ValueError):
            bp.tag_to_python_version("v1.0.1a")

    def test_tag_rejects_missing_v(self) -> None:
        with self.assertRaises(ValueError):
            bp.tag_to_python_version("1.0.1A")

    def test_tag_rejects_invalid_serial(self) -> None:
        with self.assertRaises(ValueError):
            bp.tag_to_python_version("v1.0.1A0")
        with self.assertRaises(ValueError):
            bp.tag_to_python_version("v1.0.1A02")
        with self.assertRaises(ValueError):
            bp.tag_to_python_version("v1.0.1A2x")

    def test_version_rejects_no_pre_release(self) -> None:
        with self.assertRaises(ValueError):
            bp.python_to_tag_version("1.0.1")

    def test_version_rejects_invalid_serial(self) -> None:
        with self.assertRaises(ValueError):
            bp.python_to_tag_version("1.0.1a0")
        with self.assertRaises(ValueError):
            bp.python_to_tag_version("1.0.1a02")

    def test_mapping_is_deterministic(self) -> None:
        """Repeated calls give identical results."""
        for _ in range(100):
            self.assertEqual(bp.tag_to_python_version("v1.0.1A"), "1.0.1a1")
            self.assertEqual(bp.python_to_tag_version("1.0.1a1"), "v1.0.1A")
            self.assertEqual(bp.tag_to_python_version("v1.0.1A2"), "1.0.1a2")
            self.assertEqual(bp.python_to_tag_version("1.0.1a2"), "v1.0.1A2")


class PackageVersionTests(unittest.TestCase):
    def test_105a_release_candidate_identity(self) -> None:
        self.assertEqual(bp.get_package_version(_REPO), "1.0.5a1")
        self.assertEqual(bp.python_to_tag_version("1.0.5a1"), "v1.0.5A")
        pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "1.0.5a1"', pyproject)

    def test_get_package_version_matches_init(self) -> None:
        pv = bp.get_package_version(_REPO)
        init_path = _REPO / "src" / "agent_bridge_connect" / "__init__.py"
        m = re.search("__version__\\s*=\\s*[\"\\']([^\"\\']+)[\"\\']", init_path.read_text(encoding="utf-8"))
        assert m is not None
        self.assertEqual(pv, m.group(1))

    def test_version_is_valid_pre_release(self) -> None:
        pv = bp.get_package_version(_REPO)
        self.assertRegex(pv, "^\\d+\\.\\d+\\.\\d+[a-z][1-9]\\d*$", f"package version {pv!r} must be X.Y.ZsN")

    def test_print_package_version(self) -> None:
        result = subprocess.run([sys.executable, str(_REPO / "scripts" / "build_provenance.py"), "print-package-version", "--repo-root", str(_REPO)], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), bp.get_package_version(_REPO))

    def test_print_product_version_round_trips(self) -> None:
        result = subprocess.run([sys.executable, str(_REPO / "scripts" / "build_provenance.py"), "print-product-version", "--repo-root", str(_REPO)], capture_output=True, text=True, check=True)
        product_ver = result.stdout.strip()
        pv = bp.get_package_version(_REPO)
        self.assertEqual(bp.tag_to_python_version(product_ver), pv)
        self.assertEqual(bp.python_to_tag_version(pv), product_ver)


class ReleaseDocumentationTests(unittest.TestCase):
    """Package-facing release docs must follow the source version mapping."""

    def test_package_facing_docs_use_current_version_and_tag(self) -> None:
        package_version = bp.get_package_version(_REPO)
        product_tag = bp.python_to_tag_version(package_version)
        product_version = product_tag.removeprefix("v")
        paths = ("README.md", "README_ZH.md", "docs/QUICK_START.md", "docs/QUICK_START_ZH.md", "docs/USER_GUIDE.md", "docs/USER_GUIDE_ZH.md")
        for relative in paths:
            text = (_REPO / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(package_version, text)
                self.assertIn(product_version, text)

    def test_changelog_has_current_dated_release_section(self) -> None:
        package_version = bp.get_package_version(_REPO)
        product_version = bp.python_to_tag_version(package_version).removeprefix("v")
        changelog_version = re.sub("([A-Z])[1-9]\\d*$", "\\1", product_version)
        changelog = (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = next((line for line in changelog.splitlines() if line.startswith(f"## {changelog_version} - ")))
        release_date = heading.removeprefix(f"## {changelog_version} - ")
        self.assertRegex(release_date, "^\\d{4}-\\d{2}-\\d{2}$")
        self.assertNotEqual(release_date, "Unreleased")
        if product_version != changelog_version:
            self.assertNotIn(f"## {product_version} - ", changelog)

    def test_publish_and_release_check_pin_the_same_build_tools(self) -> None:
        publish = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        release_check = (_REPO / ".github" / "workflows" / "release-check.yml").read_text(encoding="utf-8")
        for requirement in ('"build==1.5.0"', '"twine==6.2.0"'):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, publish)
                self.assertIn(requirement, release_check)


class BuildIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_git_repo(self, version: str = "1.0.1a1") -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (self.repo / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

    def test_generate_build_info_fields(self) -> None:
        self._init_git_repo("1.0.1a1")
        info = bp.generate_build_info(self.repo, build_source="test")
        self.assertEqual(info["schema_version"], bp.SCHEMA_VERSION)
        self.assertEqual(info["package_version"], "1.0.1a1")
        self.assertEqual(info["build_source"], "test")
        self.assertRegex(info["built_at_utc"], "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$")
        self.assertEqual(len(info["commit_sha"]), 40)
        self.assertEqual(len(info["source_tree_sha256"]), 64)

    def test_required_fields_present(self) -> None:
        self._init_git_repo()
        required = {"schema_version", "package_version", "commit_sha", "source_tree_sha256", "build_source", "built_at_utc"}
        info = bp.generate_build_info(self.repo)
        self.assertEqual(set(info.keys()), required)

    def test_write_build_info_creates_file(self) -> None:
        self._init_git_repo()
        out = bp.write_build_info(self.repo, build_source="test")
        self.assertTrue(out.is_file())
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["build_source"], "test")

    def test_generated_build_info_matches_runtime_reader_contract(self) -> None:
        from agent_bridge_connect.doctor import _read_build_info

        self._init_git_repo()
        out = bp.write_build_info(self.repo, build_source="test")
        (data, state) = _read_build_info(out)
        self.assertEqual(state, "valid")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["schema_version"], 1)

    def test_source_tree_sha256_deterministic(self) -> None:
        """Same git tree always produces the same hash."""
        self._init_git_repo()
        h1 = bp.get_source_tree_sha256(self.repo)
        h2 = bp.get_source_tree_sha256(self.repo)
        self.assertEqual(h1, h2)
        (self.repo / "extra.txt").write_text("untracked\n", encoding="utf-8")
        h3 = bp.get_source_tree_sha256(self.repo)
        self.assertEqual(h1, h3, "untracked files must not affect tree hash")

    def test_source_tree_sha256_changes_after_edit(self) -> None:
        self._init_git_repo()
        h1 = bp.get_source_tree_sha256(self.repo)
        readme = self.repo / "README.md"
        readme.write_text("# changed\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "change"], cwd=self.repo, check=True, capture_output=True)
        h2 = bp.get_source_tree_sha256(self.repo)
        self.assertNotEqual(h1, h2)


class DynamicFilenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_discover_exactly_one_of_each(self) -> None:
        whl = self.dist / "agentbc-1.0.1a1-py3-none-any.whl"
        sdist = self.dist / "agentbc-1.0.1a1.tar.gz"
        whl.write_text("whl")
        sdist.write_text("sdist")
        (w, s) = bp.discover_dists(self.dist)
        self.assertEqual(w, whl)
        self.assertEqual(s, sdist)

    def test_rejects_zero_wheels(self) -> None:
        (self.dist / "agentbc-1.0.1a1.tar.gz").write_text("sdist")
        with self.assertRaises(ValueError) as ctx:
            bp.discover_dists(self.dist)
        self.assertIn("expected exactly 1 wheel", str(ctx.exception))

    def test_rejects_multiple_wheels(self) -> None:
        (self.dist / "agentbc-1.0.1a1-py3-none-any.whl").write_text("a")
        (self.dist / "agentbc-1.0.2a1-py3-none-any.whl").write_text("b")
        (self.dist / "agentbc-1.0.1a1.tar.gz").write_text("sdist")
        with self.assertRaises(ValueError) as ctx:
            bp.discover_dists(self.dist)
        self.assertIn("expected exactly 1 wheel", str(ctx.exception))

    def test_rejects_zero_sdists(self) -> None:
        (self.dist / "agentbc-1.0.1a1-py3-none-any.whl").write_text("whl")
        with self.assertRaises(ValueError) as ctx:
            bp.discover_dists(self.dist)
        self.assertIn("expected exactly 1 sdist", str(ctx.exception))

    def test_validate_dist_filenames_correct(self) -> None:
        pv = "1.0.1a1"
        (self.dist / f"agentbc-{pv}-py3-none-any.whl").write_text("w")
        (self.dist / f"agentbc-{pv}.tar.gz").write_text("s")
        result = bp.validate_dist_filenames(self.dist, pv)
        self.assertTrue(result["valid"], result.get("errors"))

    def test_validate_dist_filenames_mismatch(self) -> None:
        (self.dist / "agentbc-9.9.9a1-py3-none-any.whl").write_text("w")
        (self.dist / "agentbc-9.9.9a1.tar.gz").write_text("s")
        result = bp.validate_dist_filenames(self.dist, "1.0.1a1")
        self.assertFalse(result["valid"])

    def test_discover_dists_cli(self) -> None:
        pv = bp.get_package_version(_REPO)
        (self.dist / f"agentbc-{pv}-py3-none-any.whl").write_text("w")
        (self.dist / f"agentbc-{pv}.tar.gz").write_text("s")
        result = subprocess.run([sys.executable, str(_REPO / "scripts" / "build_provenance.py"), "discover-dists", "--dist-dir", str(self.dist)], capture_output=True, text=True, check=True)
        self.assertIn("wheel=", result.stdout)
        self.assertIn("sdist=", result.stdout)


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.dist = Path(self.tmp.name) / "dist"
        self.dist.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_git_repo(self, version: str = "1.0.1a1") -> str:
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@e.org"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (self.repo / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def _make_fake_dists(self, version: str = "1.0.1a1") -> tuple[Path, Path]:
        whl = self.dist / f"agentbc-{version}-py3-none-any.whl"
        sdist = self.dist / f"agentbc-{version}.tar.gz"
        whl.write_bytes(os.urandom(1024))
        sdist.write_bytes(os.urandom(2048))
        return (whl, sdist)

    def test_manifest_required_fields(self) -> None:
        self._init_git_repo()
        self._make_fake_dists()
        manifest = bp.generate_release_manifest(self.repo, self.dist, "v1.0.1A")
        required = {"schema_version", "tag", "package_version", "commit_sha", "source_tree_sha256", "artifacts"}
        self.assertEqual(set(manifest.keys()), required)
        self.assertIsInstance(manifest["artifacts"], list)
        self.assertEqual(len(manifest["artifacts"]), 2)

    def test_manifest_artifact_fields(self) -> None:
        self._init_git_repo()
        self._make_fake_dists()
        manifest = bp.generate_release_manifest(self.repo, self.dist)
        for art in manifest["artifacts"]:
            self.assertIn("filename", art)
            self.assertIn("size", art)
            self.assertIn("sha256", art)
            self.assertGreater(art["size"], 0)
            self.assertEqual(len(art["sha256"]), 64)

    def test_manifest_tag_auto_derived(self) -> None:
        self._init_git_repo("1.0.1a1")
        self._make_fake_dists("1.0.1a1")
        manifest = bp.generate_release_manifest(self.repo, self.dist)
        self.assertEqual(manifest["tag"], "v1.0.1A")

    def test_manifest_explicit_tag(self) -> None:
        self._init_git_repo()
        self._make_fake_dists()
        manifest = bp.generate_release_manifest(self.repo, self.dist, "v9.9.9Z")
        self.assertEqual(manifest["tag"], "v9.9.9Z")
        self.assertEqual(manifest["package_version"], "1.0.1a1")

    def test_manifest_artifact_sha256_matches_content(self) -> None:
        self._init_git_repo()
        (whl, sdist) = self._make_fake_dists()
        manifest = bp.generate_release_manifest(self.repo, self.dist)
        for art in manifest["artifacts"]:
            fp = self.dist / art["filename"]
            actual = hashlib.sha256(fp.read_bytes()).hexdigest()
            self.assertEqual(art["sha256"], actual, f"hash mismatch for {art['filename']}")

    def test_write_manifest_creates_file(self) -> None:
        self._init_git_repo()
        self._make_fake_dists()
        out = bp.write_release_manifest(self.repo, self.dist, "v1.0.1A")
        self.assertTrue(out.is_file())
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["tag"], "v1.0.1A")
        self.assertEqual(len(data["artifacts"]), 2)

    def test_manifest_schema_version_constant(self) -> None:
        self._init_git_repo()
        self._make_fake_dists()
        m1 = bp.generate_release_manifest(self.repo, self.dist)
        m2 = bp.generate_release_manifest(self.repo, self.dist)
        self.assertEqual(m1["schema_version"], m2["schema_version"])
        self.assertEqual(m1["schema_version"], bp.SCHEMA_VERSION)


class ProvenanceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_repo(self, version: str = "1.0.1a1", tag: str | None = None) -> str:
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@e.org"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (self.repo / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()
        if tag:
            subprocess.run(["git", "tag", tag], cwd=self.repo, check=True, capture_output=True)
        return sha

    def test_validate_no_tag_passes_on_clean_tree(self) -> None:
        self._init_repo()
        result = bp.validate_provenance(self.repo)
        self.assertTrue(result["valid"], result.get("errors"))

    def test_validate_clean_tree_with_tag(self) -> None:
        self._init_repo("1.0.1a1", "v1.0.1A")
        result = bp.validate_provenance(self.repo, "v1.0.1A")
        self.assertTrue(result["valid"], result.get("errors"))

    def test_validate_dirty_tree_fails(self) -> None:
        self._init_repo()
        (self.repo / "dirty.txt").write_text("oops\n", encoding="utf-8")
        result = bp.validate_provenance(self.repo)
        self.assertFalse(result["valid"])
        self.assertTrue(any(("not clean" in e for e in result["errors"])))

    def test_validate_tag_not_at_head_fails(self) -> None:
        self._init_repo("1.0.1a1")
        result = bp.validate_provenance(self.repo, "v1.0.1A")
        self.assertFalse(result["valid"])
        self.assertTrue(any(("not tagged" in e for e in result["errors"])))

    def test_validate_tag_version_mismatch_fails(self) -> None:
        self._init_repo("2.0.0a1", "v1.0.1A")
        result = bp.validate_provenance(self.repo, "v1.0.1A")
        self.assertFalse(result["valid"])
        self.assertTrue(any(("maps to version" in e for e in result["errors"])))

    def test_validate_invalid_tag_format_fails(self) -> None:
        self._init_repo()
        result = bp.validate_provenance(self.repo, "not-a-valid-tag")
        self.assertFalse(result["valid"])
        self.assertTrue(any(("does not match" in e for e in result["errors"])))


class NoPublishBehaviourTests(unittest.TestCase):
    """Verify that workflow_dispatch never publishes to PyPI.

    These tests check the *code-level* guards; CI-level enforcement is
    tested by the workflow YAML structure.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_repo(self) -> str:
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@e.org"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text('__version__ = "1.0.1a1"\n', encoding="utf-8")
        (self.repo / "README.md").write_text("# t\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def test_publish_job_requires_release_or_confirmed_tag_recovery(self) -> None:
        """Publish is limited to releases or explicit tagged recovery runs."""
        workflow = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        publish_guard = workflow.split("\n  publish:\n", 1)[1].split("\n    steps:\n", 1)[0]
        self.assertIn("github.event_name == 'release'", publish_guard)
        self.assertIn("github.event.release.prerelease == false", publish_guard)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish_guard)
        self.assertIn("inputs.publish", publish_guard)
        self.assertIn("inputs.release_tag != ''", publish_guard)

    def test_release_upload_is_repo_explicit_and_recoverable(self) -> None:
        workflow = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        self.assertIn('--repo "${{ github.repository }}"', workflow)
        self.assertIn("--clobber", workflow)

    def test_pypi_upload_excludes_release_manifest(self) -> None:
        """Only Python distributions are handed to the PyPI publisher."""
        workflow = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        self.assertIn("mkdir -p dist/pypi", workflow)
        self.assertIn("cp dist/*.whl dist/*.tar.gz dist/pypi/", workflow)
        self.assertIn("packages-dir: dist/pypi/", workflow)

    def test_build_source_distinguishes_events(self) -> None:
        """build_source is set from CI context to differentiate events."""
        workflow = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        self.assertIn("BUILD_SOURCE", workflow)
        self.assertIn("github-release", workflow)
        self.assertIn("github-manual", workflow)

    def test_tagged_recovery_checks_out_and_validates_existing_tag(self) -> None:
        """Manual publish recovery must build and validate the named tag."""
        workflow = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        self.assertIn("inputs.release_tag || github.ref", workflow)
        self.assertIn('validate --tag "$RELEASE_TAG"', workflow)
        self.assertIn("github-release-recovery", workflow)

    def test_workflow_dispatch_triggers_build(self) -> None:
        """workflow_dispatch is listed as a trigger."""
        workflow = (_REPO / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", workflow)

    def test_build_info_not_permanently_committed(self) -> None:
        """_build_info.json is gitignored and not tracked."""
        gitignore = (_REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("_build_info.json", gitignore)
        tracked = subprocess.run(["git", "ls-files", "--", "src/agent_bridge_connect/_build_info.json"], cwd=_REPO, capture_output=True, text=True, check=True)
        self.assertFalse(tracked.stdout.strip(), "_build_info.json must not be committed; it is a build artifact")


class _skill_manifest_SkillManifestTests(_skill_manifest_unittest.TestCase):
    def setUp(self):
        self.test_dir = _skill_manifest_Path(_skill_manifest_tempfile.mkdtemp())

    def tearDown(self):
        _skill_manifest_shutil.rmtree(self.test_dir)

    def _install(self, platform: str, root: _skill_manifest_Path, **kwargs):
        from agent_bridge_connect.setup import install_claude_skill, install_codex_skill, install_hermes_skill

        if platform == "codex":
            return install_codex_skill(root, interactive=False, **kwargs)
        destination = root / "SKILL.md"
        if platform == "claude":
            return install_claude_skill(destination, interactive=False, **kwargs)
        return install_hermes_skill(destination, interactive=False, all_profiles=False, **kwargs)

    def _state(self, platform: str, root: _skill_manifest_Path):
        from agent_bridge_connect.setup import _current_skill_files

        return _skill_manifest_classify_skill_package(root, platform=platform, package_version=_skill_manifest___version__, current_files=_current_skill_files(platform))

    def _snapshot(self, root: _skill_manifest_Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file() and (not path.is_symlink())}

    def test_every_platform_installs_schema_v1_current_package(self):
        expected_files = {
            "codex": {"SKILL.md", "agents/openai.yaml", "references/agentbc-steps-yaml.md", "references/controller-contract.md"},
            "claude": {"SKILL.md", "references/agentbc-steps-yaml.md", "references/controller-contract.md"},
            "hermes": {"SKILL.md", "references/agentbc-steps-yaml.md", "references/controller-contract.md"},
        }
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                root = self.test_dir / platform
                result = self._install(platform, root)
                manifest = _skill_manifest_json.loads((root / ".agentbc-skill.json").read_text(encoding="utf-8"))
                self.assertEqual(result["classification"], "current")
                self.assertEqual(manifest["schema_version"], 1)
                self.assertEqual(manifest["platform"], platform)
                self.assertEqual(manifest["package_version"], _skill_manifest___version__)
                self.assertEqual(manifest["protocol_version"], "1.0")
                self.assertEqual(manifest["completion_version"], 1)
                self.assertEqual(set(manifest["files"]), expected_files[platform])
                self.assertNotIn(".agentbc-skill.json", manifest["files"])
                self.assertEqual(list(manifest["files"]), sorted(manifest["files"]))
                for relative_path, digest in manifest["files"].items():
                    self.assertEqual(digest, _skill_manifest_sha256_bytes((root / relative_path).read_bytes()))
                content = {path: (root / path).read_bytes() for path in manifest["files"]}
                self.assertEqual(manifest["template_sha256"], _skill_manifest_aggregate_template_sha256(content))
                self.assertEqual(self._state(platform, root)["classification"], "current")

    def test_aggregate_hash_uses_sorted_paths_nuls_and_exact_bytes(self):
        files = {"z/file": b"z\n", "a": b"a\x00b", "middle": b""}
        manual = _skill_manifest_hashlib.sha256()
        for path in sorted(files):
            manual.update(path.encode("utf-8"))
            manual.update(b"\x00")
            manual.update(files[path])
            manual.update(b"\x00")
        manifest = _skill_manifest_build_skill_manifest("codex", "test", files)
        self.assertEqual(manifest["template_sha256"], manual.hexdigest())
        self.assertEqual(list(manifest["files"]), sorted(files))
        encoded = _skill_manifest_serialize_skill_manifest(manifest)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded, _skill_manifest_serialize_skill_manifest(manifest))

    def test_legacy_33d3d08_fingerprints_are_frozen(self):
        self.assertEqual(_skill_manifest_LEGACY_SKILL_FINGERPRINTS["codex"]["template_sha256"], "ef0f073ef911e5fb0f8092fdda57c204db5dc24e565d9b8367d35830974afea1")
        self.assertEqual(_skill_manifest_LEGACY_SKILL_FINGERPRINTS["claude"]["template_sha256"], "aaaa1fb45b9ef6a9c91b4435a0a22c5aedbc5beb89413f7c45e0f2b310ccc36d")
        self.assertEqual(_skill_manifest_LEGACY_SKILL_FINGERPRINTS["hermes"]["template_sha256"], "381b9926e7a26cfba2b45c03b3797bd0683de3a022ac2848c3de8954c972668a")

    def test_managed_102a1_fingerprints_are_frozen_and_version_consistent(self):
        fingerprints = _skill_manifest_MANAGED_SKILL_FINGERPRINTS["1.0.2a1"]
        self.assertEqual(set(fingerprints), {"codex", "claude", "hermes"})
        self.assertEqual(fingerprints["codex"]["template_sha256"], "bd6eebf95d63963eeafa4986705e010b92c41dde45878dbfb83ebb0bfc0c290e")
        self.assertEqual(fingerprints["claude"]["template_sha256"], "891b95db10659920d5c3b9f5e013ddf5803c476458ce75e53d4e58a9a6b953b8")
        self.assertEqual(fingerprints["hermes"]["template_sha256"], "bcfce4bfdc406d36670155256b48bd2ab1c32c83415b588a10da722f8bba26e1")
        self.assertEqual(fingerprints["codex"]["files"]["references/controller-contract.md"], "370af89b528469368c75f49b9986f004951397b240a1d9721d6598057665c38d")

    def test_managed_103a2_is_frozen_and_current_templates_have_separate_fingerprints(self):
        """The historical 1.0.3a2 managed-package fingerprint is frozen."""
        fingerprints = _skill_manifest_MANAGED_SKILL_FINGERPRINTS["1.0.3a2"]
        self.assertEqual(set(fingerprints), {"codex", "claude", "hermes"})
        self.assertEqual(fingerprints["codex"]["files"]["references/controller-contract.md"], "a8548e70a6c5d16d9f856e346918d5c3b10584c817f54a4b3cd83312edfac2fc")
        self.assertNotEqual(fingerprints["codex"]["files"]["SKILL.md"], _skill_manifest_MANAGED_SKILL_FINGERPRINTS["1.0.2a1"]["codex"]["files"]["SKILL.md"])
        self.assertEqual(fingerprints["codex"]["files"]["references/agentbc-steps-yaml.md"], _skill_manifest_MANAGED_SKILL_FINGERPRINTS["1.0.2a1"]["codex"]["files"]["references/agentbc-steps-yaml.md"])
        from agent_bridge_connect.setup import _current_skill_files, _expected_skill_manifest

        # Historical managed packages must retain their original fingerprints.
        # Structured handoff changes only the shared controller contract in the current
        # templates; pin the new bytes independently of the old release.
        historical_templates = {
            "codex": "7e8d35c9620de6c763ac1adaf63110524c59f9d59f1339447834616a00a73525",
            "claude": "c6a25936f73c73ed8df2478c051ad195b312e5d29d3fdcda00e3d40076fdd167",
            "hermes": "31214ff1aac79d0c78c9753db5c6b884ea0fe6245542c8f2e2a71f6e2029ab2a",
        }
        self.assertEqual({name: record["template_sha256"] for name, record in fingerprints.items()}, historical_templates)
        current_contract = "d1c9f4380293a23bdeb5286bc01389c1e4e40f155cce768ef2fbf8e8246e4bfd"
        current_templates = {
            "codex": "192a9f3ea205a9d322861e6c0498f1f148613739537b97785921dd158857541b",
            "claude": "02def9e39635ac73f39a7cfdf43b02f5b34b209c0ebaacbef84c953d30ec86bf",
            "hermes": "826d53e5188d0813af08c9a3b212fb7044908a22e626a88723bb02ccb6da3e44",
        }
        for platform in ("codex", "claude", "hermes"):
            current = _current_skill_files(platform)
            for path, digest in fingerprints[platform]["files"].items():
                expected_digest = current_contract if path == "references/controller-contract.md" else digest
                self.assertEqual(expected_digest, _skill_manifest_sha256_bytes(current[path]))
            expected = _expected_skill_manifest(platform)
            self.assertEqual(current_templates[platform], expected["template_sha256"])
            self.assertNotEqual(fingerprints[platform]["template_sha256"], expected["template_sha256"])
            self.assertEqual(set(fingerprints[platform]["files"]), set(expected["files"]))

    def _write_managed_package(self, platform: str, root: _skill_manifest_Path, files: dict[str, bytes], package_version: str) -> None:
        for relative_path, content in files.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = _skill_manifest_build_skill_manifest(platform, package_version, files)
        (root / ".agentbc-skill.json").write_bytes(_skill_manifest_serialize_skill_manifest(manifest))

    def _write_real_102a1_package(self, platform: str, root: _skill_manifest_Path) -> None:

        fingerprint = _skill_manifest_MANAGED_SKILL_FINGERPRINTS["1.0.2a1"][platform]
        from agent_bridge_connect.setup import _current_skill_files

        current = _current_skill_files(platform)
        frozen_override = {
            "SKILL.md": _skill_manifest_Path(__file__).parents[1] / "fixtures" / "skill" / "1.0.2a1" / "codex-SKILL.md" if platform == "codex" else _skill_manifest_Path(""),
            "references/controller-contract.md": _skill_manifest_Path(__file__).parents[1] / "fixtures" / "skill" / "1.0.2a1" / "controller-contract.md",
        }
        files: dict[str, bytes] = {}
        for relative_path, digest in fingerprint["files"].items():
            override = frozen_override.get(relative_path)
            if override is not None and override.is_file():
                candidate = override.read_bytes()
            else:
                candidate = current.get(relative_path)
                if candidate is None:
                    raise AssertionError(f"no current bytes for {platform}:{relative_path}")
            if _skill_manifest_sha256_bytes(candidate) != digest:
                raise AssertionError(f"frozen 1.0.2a1 bytes unavailable for {platform}:{relative_path}; the file changed and the preserved fixture is missing or stale")
            files[relative_path] = candidate
        self._write_managed_package(platform, root, files, "1.0.2a1")

    def test_managed_outdated_real_102a1_is_classified_and_refreshed_for_every_platform(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                root = self.test_dir / f"managed-outdated-{platform}"
                self._write_real_102a1_package(platform, root)
                unrelated = root / "notes.txt"
                unrelated.write_text("keep me", encoding="utf-8")
                state = self._state(platform, root)
                self.assertEqual(state["classification"], "managed_outdated")
                self.assertTrue(state["installed"])
                self.assertFalse(state["up_to_date"])
                self.assertEqual(state["manifest"]["package_version"], "1.0.2a1")
                result = self._install(platform, root)
                self.assertEqual(result["previous_classification"], "managed_outdated")
                self.assertEqual(result["classification"], "current")
                self.assertEqual(self._state(platform, root)["classification"], "current")
                self.assertEqual(self._state(platform, root)["manifest"]["package_version"], _skill_manifest___version__)
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

    def test_forged_unknown_and_edited_managed_manifests_remain_modified(self):
        from agent_bridge_connect.setup import _current_skill_files, install_codex_skill

        root = self.test_dir / "managed-forged"
        current_files = _current_skill_files("codex")
        self._write_managed_package("codex", root, current_files, "0.0.0-unknown")
        self.assertEqual(self._state("codex", root)["classification"], "modified")
        unrelated = root / "notes.md"
        unrelated.write_text("user-owned\n", encoding="utf-8")
        forged = _skill_manifest_build_skill_manifest("codex", "1.0.2a1", {"notes.md": unrelated.read_bytes()})
        manifest_path = root / ".agentbc-skill.json"
        manifest_path.write_bytes(_skill_manifest_serialize_skill_manifest(forged))
        state = self._state("codex", root)
        self.assertEqual(state["classification"], "modified")
        self.assertNotIn("notes.md", state["managed_paths"])
        install_codex_skill(root, interactive=False, force=True)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user-owned\n")
        self._write_real_102a1_package("codex", root)
        (root / "SKILL.md").write_bytes(b"user customization\n")
        self.assertEqual(self._state("codex", root)["classification"], "modified")
        refused = install_codex_skill(root, interactive=False)
        self.assertEqual(refused["status"], "modified_requires_confirmation")
        self.assertEqual((root / "SKILL.md").read_bytes(), b"user customization\n")
        (root / "SKILL.md").unlink()
        (root / "SKILL.md").mkdir()
        (root / "SKILL.md" / "user.txt").write_text("preserve\n", encoding="utf-8")
        self.assertEqual(self._state("codex", root)["classification"], "modified")
        self.assertEqual((root / "SKILL.md" / "user.txt").read_text(encoding="utf-8"), "preserve\n")

    def test_recognized_legacy_is_upgraded_noninteractively_for_every_platform(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                root = self.test_dir / f"legacy-{platform}"
                legacy_paths = _skill_manifest_LEGACY_SKILL_FINGERPRINTS[platform]["files"]
                old_files = {path: f"legacy {platform} {path}\n".encode("utf-8") for path in legacy_paths}
                for relative_path, content in old_files.items():
                    path = root / relative_path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                fingerprint = {"template_sha256": _skill_manifest_aggregate_template_sha256(old_files), "files": {path: _skill_manifest_sha256_bytes(content) for (path, content) in old_files.items()}}
                with _skill_manifest_mock.patch.dict(_skill_manifest_LEGACY_SKILL_FINGERPRINTS, {platform: fingerprint}):
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
        valid = _skill_manifest_json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = (b"not-json\n", _skill_manifest_json.dumps({**valid, "schema_version": 2}).encode("utf-8"))
        for content in cases:
            with self.subTest(content=content[:12]):
                manifest_path.write_bytes(content)
                self.assertEqual(self._state("claude", root)["classification"], "modified")

    def test_per_file_and_aggregate_manifest_mismatch_are_modified(self):
        root = self.test_dir / "hash-mismatch"
        self._install("hermes", root)
        manifest_path = root / ".agentbc-skill.json"
        manifest = _skill_manifest_json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["template_sha256"] = "0" * 64
        manifest_path.write_text(_skill_manifest_json.dumps(manifest), encoding="utf-8")
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
        with _skill_manifest_mock.patch("builtins.input", return_value="y"):
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
        forged = _skill_manifest_build_skill_manifest("codex", _skill_manifest___version__, {"notes.md": unrelated.read_bytes()})
        manifest_path = root / ".agentbc-skill.json"
        manifest_path.write_bytes(_skill_manifest_serialize_skill_manifest(forged))
        state = self._state("codex", root)
        self.assertEqual(state["classification"], "modified")
        self.assertNotIn("notes.md", state["managed_paths"])
        install_codex_skill(root, interactive=False, force=True)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user-owned\n")
        manifest_path.write_bytes(_skill_manifest_serialize_skill_manifest(forged))
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

        def fail_second(source: _skill_manifest_Path, destination: _skill_manifest_Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted update")
            original(source, destination)

        with _skill_manifest_mock.patch.object(skill_packages, "_install_staged_file", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "interrupted"):
                install_codex_skill(root, interactive=False, force=True)
        self.assertEqual(self._snapshot(root), before)
        self.assertFalse(any(root.parent.glob(f".{root.name}.agentbc-*-*")))

    def test_setup_show_is_read_only_for_existing_packages(self):
        from agent_bridge_connect import setup

        home = self.test_dir / "home"
        roots = {"AGENTBC_CODEX_SKILL_PATH": home / ".codex" / "skills" / "agentbc", "AGENTBC_CLAUDE_SKILL_PATH": home / ".claude" / "skills" / "agentbc" / "SKILL.md", "AGENTBC_HERMES_SKILL_PATH": home / ".hermes" / "skills" / "agentbc" / "SKILL.md"}
        self._install("codex", roots["AGENTBC_CODEX_SKILL_PATH"])
        self._install("claude", roots["AGENTBC_CLAUDE_SKILL_PATH"].parent)
        self._install("hermes", roots["AGENTBC_HERMES_SKILL_PATH"].parent)
        before = self._snapshot(home)
        env = {"HOME": str(home), "PATH": "", "AGENTBC_CONFIG_PATH": str(home / "config.toml"), **{key: str(value) for (key, value) in roots.items()}}
        with (
            _skill_manifest_mock.patch.dict(_skill_manifest_os.environ, env, clear=True),
            _skill_manifest_mock.patch.object(setup, "probe_codex", return_value={}),
            _skill_manifest_mock.patch.object(setup, "discover_codex", return_value={"found": False, "path": "", "version": "", "source": "not_found", "searched_paths": [], "manual_override": ""}),
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
        agent = {"name": "codex", "display": "Codex CLI", "found": True, "supported_executor": True, "path": "/fake/codex", "binary": "codex", "version": "test", "source": "test", "capability_level": "L2", "capabilities": {}, "skill": {"installed": True, "up_to_date": False}}
        env = {"HOME": str(home), "PATH": "", "AGENTBC_CONFIG_PATH": str(config), "AGENTBC_CODEX_SKILL_PATH": str(root)}
        with (
            _skill_manifest_mock.patch.dict(_skill_manifest_os.environ, env, clear=True),
            _skill_manifest_mock.patch.object(setup, "scan_all_agents", side_effect=[[agent], [agent]]),
            _skill_manifest_mock.patch.object(setup, "_print_scan_report"),
            _skill_manifest_mock.patch.object(setup, "discover_codex", return_value={"found": True}),
            _skill_manifest_mock.patch.object(setup, "probe_codex", return_value={}),
        ):
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
            "skill": {"installed": True, "up_to_date": False, "path": str(root / "SKILL.md")},
        }
        env = {"HOME": str(home), "PATH": "", "AGENTBC_CONFIG_PATH": str(home / "config.toml"), "AGENTBC_CODEX_SKILL_PATH": str(root)}
        with _skill_manifest_mock.patch.dict(_skill_manifest_os.environ, env, clear=True), _skill_manifest_mock.patch.object(setup, "scan_all_agents", return_value=[agent]), _skill_manifest_mock.patch.object(setup, "_print_scan_report"), _skill_manifest_mock.patch.object(setup, "_print_selectable_items"):
            repaired = setup.run_update(interactive=False)
            (root / "SKILL.md").write_text("custom update content\n", encoding="utf-8")
            refused = setup.run_update(interactive=False)
        self.assertEqual(repaired["actions"][0]["previous_classification"], "partial")
        self.assertEqual(refused["actions"][0]["status"], "modified_requires_confirmation")
        self.assertEqual((root / "SKILL.md").read_text(encoding="utf-8"), "custom update content\n")

    def test_templates_are_concise_and_package_data_covers_references(self):
        repository_root = _skill_manifest_Path(__file__).resolve().parents[2]
        skills_root = repository_root / "src" / "agent_bridge_connect" / "skills"
        for skill in ("codex_skill.md", "claude_skill.md", "hermes_skill.md"):
            self.assertLess(len((skills_root / skill).read_text(encoding="utf-8").splitlines()), 500)
        package_config = (repository_root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"skills/references/*.md"', package_config)
        self.assertTrue((skills_root / "references" / "controller-contract.md").is_file())
        openai = (skills_root / "codex_openai.yaml").read_text(encoding="utf-8")
        self.assertIn("shared controller contract", openai)
