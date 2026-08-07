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

# Make the build_provenance module importable from scripts/.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "scripts"))

import build_provenance as bp  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# tag ↔ version mapping
# ═══════════════════════════════════════════════════════════════════════


class TagVersionMappingTests(unittest.TestCase):
    """vX.Y.ZS[N]  ⇄  X.Y.ZsN  round-trip and edge cases."""

    def test_tag_to_python(self) -> None:
        self.assertEqual(bp.tag_to_python_version("v1.0.1A"), "1.0.1a1")
        self.assertEqual(bp.tag_to_python_version("v1.0.1A2"), "1.0.1a2")
        self.assertEqual(bp.tag_to_python_version("v2.3.4B"), "2.3.4b1")
        self.assertEqual(bp.tag_to_python_version("v2.3.4B12"), "2.3.4b12")
        self.assertEqual(bp.tag_to_python_version("v0.0.0Z"), "0.0.0z1")

    def test_python_to_tag(self) -> None:
        self.assertEqual(bp.python_to_tag_version("1.0.1a1"), "v1.0.1A")
        self.assertEqual(bp.python_to_tag_version("1.0.1a2"), "v1.0.1A2")
        self.assertEqual(bp.python_to_tag_version("2.3.4b1"), "v2.3.4B")
        self.assertEqual(bp.python_to_tag_version("2.3.4b12"), "v2.3.4B12")
        self.assertEqual(bp.python_to_tag_version("0.0.0z1"), "v0.0.0Z")

    def test_round_trip_tag_first(self) -> None:
        for tag in ("v1.0.1A", "v1.0.1A2", "v2.3.4B12", "v5.6.7C"):
            with self.subTest(tag=tag):
                self.assertEqual(
                    bp.python_to_tag_version(bp.tag_to_python_version(tag)),
                    tag,
                )

    def test_round_trip_version_first(self) -> None:
        for ver in ("1.0.1a1", "1.0.1a2", "2.3.4b12", "5.6.7c1"):
            with self.subTest(version=ver):
                self.assertEqual(
                    bp.tag_to_python_version(bp.python_to_tag_version(ver)),
                    ver,
                )

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


# ═══════════════════════════════════════════════════════════════════════
# package-version discovery
# ═══════════════════════════════════════════════════════════════════════


class PackageVersionTests(unittest.TestCase):
    def test_get_package_version_matches_init(self) -> None:
        pv = bp.get_package_version(_REPO)
        init_path = _REPO / "src" / "agent_bridge_connect" / "__init__.py"
        m = re.search(
            r'__version__\s*=\s*["\']([^"\']+)["\']',
            init_path.read_text(encoding="utf-8"),
        )
        assert m is not None
        self.assertEqual(pv, m.group(1))

    def test_version_is_valid_pre_release(self) -> None:
        pv = bp.get_package_version(_REPO)
        self.assertRegex(pv, r"^\d+\.\d+\.\d+[a-z][1-9]\d*$",
                         f"package version {pv!r} must be X.Y.ZsN")

    def test_print_package_version(self) -> None:
        result = subprocess.run(
            [sys.executable, str(_REPO / "scripts" / "build_provenance.py"),
             "print-package-version", "--repo-root", str(_REPO)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), bp.get_package_version(_REPO))

    def test_print_product_version_round_trips(self) -> None:
        result = subprocess.run(
            [sys.executable, str(_REPO / "scripts" / "build_provenance.py"),
             "print-product-version", "--repo-root", str(_REPO)],
            capture_output=True, text=True, check=True,
        )
        product_ver = result.stdout.strip()
        pv = bp.get_package_version(_REPO)
        self.assertEqual(bp.tag_to_python_version(product_ver), pv)
        self.assertEqual(bp.python_to_tag_version(pv), product_ver)


# ═══════════════════════════════════════════════════════════════════════
# build identity (_build_info.json)
# ═══════════════════════════════════════════════════════════════════════


class BuildIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_git_repo(self, version: str = "1.0.1a1") -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.repo, check=True, capture_output=True,
        )
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
        (self.repo / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo,
                       check=True, capture_output=True)

    def test_generate_build_info_fields(self) -> None:
        self._init_git_repo("1.0.1a1")
        info = bp.generate_build_info(self.repo, build_source="test")
        self.assertEqual(info["schema_version"], bp.SCHEMA_VERSION)
        self.assertEqual(info["package_version"], "1.0.1a1")
        self.assertEqual(info["build_source"], "test")
        self.assertRegex(info["built_at_utc"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(len(info["commit_sha"]), 40)
        self.assertEqual(len(info["source_tree_sha256"]), 64)

    def test_required_fields_present(self) -> None:
        self._init_git_repo()
        required = {
            "schema_version", "package_version", "commit_sha",
            "source_tree_sha256", "build_source", "built_at_utc",
        }
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
        data, state = _read_build_info(out)
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
        # Same tree after clean worktree
        (self.repo / "extra.txt").write_text("untracked\n", encoding="utf-8")
        h3 = bp.get_source_tree_sha256(self.repo)
        self.assertEqual(h1, h3, "untracked files must not affect tree hash")

    def test_source_tree_sha256_changes_after_edit(self) -> None:
        self._init_git_repo()
        h1 = bp.get_source_tree_sha256(self.repo)
        readme = self.repo / "README.md"
        readme.write_text("# changed\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "change"], cwd=self.repo,
                       check=True, capture_output=True)
        h2 = bp.get_source_tree_sha256(self.repo)
        self.assertNotEqual(h1, h2)


# ═══════════════════════════════════════════════════════════════════════
# dynamic filename discovery
# ═══════════════════════════════════════════════════════════════════════


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
        w, s = bp.discover_dists(self.dist)
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
        result = subprocess.run(
            [sys.executable, str(_REPO / "scripts" / "build_provenance.py"),
             "discover-dists", "--dist-dir", str(self.dist)],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("wheel=", result.stdout)
        self.assertIn("sdist=", result.stdout)


# ═══════════════════════════════════════════════════════════════════════
# release-manifest generation & hash verification
# ═══════════════════════════════════════════════════════════════════════


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
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@e.org"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
        (self.repo / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo,
                       check=True, capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _make_fake_dists(self, version: str = "1.0.1a1") -> tuple[Path, Path]:
        whl = self.dist / f"agentbc-{version}-py3-none-any.whl"
        sdist = self.dist / f"agentbc-{version}.tar.gz"
        whl.write_bytes(os.urandom(1024))
        sdist.write_bytes(os.urandom(2048))
        return whl, sdist

    def test_manifest_required_fields(self) -> None:
        self._init_git_repo()
        self._make_fake_dists()
        manifest = bp.generate_release_manifest(self.repo, self.dist, "v1.0.1A")
        required = {
            "schema_version", "tag", "package_version", "commit_sha",
            "source_tree_sha256", "artifacts",
        }
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
        whl, sdist = self._make_fake_dists()
        manifest = bp.generate_release_manifest(self.repo, self.dist)
        for art in manifest["artifacts"]:
            fp = self.dist / art["filename"]
            actual = hashlib.sha256(fp.read_bytes()).hexdigest()
            self.assertEqual(art["sha256"], actual,
                             f"hash mismatch for {art['filename']}")

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


# ═══════════════════════════════════════════════════════════════════════
# provenance validation
# ═══════════════════════════════════════════════════════════════════════


class ProvenanceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_repo(self, version: str = "1.0.1a1", tag: str | None = None) -> str:
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@e.org"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
        (self.repo / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo,
                       check=True, capture_output=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if tag:
            subprocess.run(["git", "tag", tag], cwd=self.repo, check=True,
                           capture_output=True)
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
        self.assertTrue(any("not clean" in e for e in result["errors"]))

    def test_validate_tag_not_at_head_fails(self) -> None:
        self._init_repo("1.0.1a1")
        # Tag a different commit
        result = bp.validate_provenance(self.repo, "v1.0.1A")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("not tagged" in e for e in result["errors"])
        )

    def test_validate_tag_version_mismatch_fails(self) -> None:
        self._init_repo("2.0.0a1", "v1.0.1A")
        result = bp.validate_provenance(self.repo, "v1.0.1A")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("maps to version" in e for e in result["errors"])
        )

    def test_validate_invalid_tag_format_fails(self) -> None:
        self._init_repo()
        result = bp.validate_provenance(self.repo, "not-a-valid-tag")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("does not match" in e for e in result["errors"])
        )


# ═══════════════════════════════════════════════════════════════════════
# no-publish behaviour (manual / smoke)
# ═══════════════════════════════════════════════════════════════════════


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
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@e.org"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=self.repo, check=True, capture_output=True)
        pkg_dir = self.repo / "src" / "agent_bridge_connect"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(
            '__version__ = "1.0.1a1"\n', encoding="utf-8"
        )
        (self.repo / "README.md").write_text("# t\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo,
                       check=True, capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_publish_job_requires_release_or_confirmed_tag_recovery(self) -> None:
        """Publish is limited to releases or explicit tagged recovery runs."""
        workflow = (
            _REPO / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")
        publish_guard = workflow.split("\n  publish:\n", 1)[1].split(
            "\n    steps:\n", 1
        )[0]
        self.assertIn("github.event_name == 'release'", publish_guard)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish_guard)
        self.assertIn("inputs.publish", publish_guard)
        self.assertIn("inputs.release_tag != ''", publish_guard)

    def test_release_upload_is_repo_explicit_and_recoverable(self) -> None:
        workflow = (
            _REPO / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('--repo "${{ github.repository }}"', workflow)
        self.assertIn("--clobber", workflow)

    def test_build_source_distinguishes_events(self) -> None:
        """build_source is set from CI context to differentiate events."""
        workflow = (
            _REPO / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("BUILD_SOURCE", workflow)
        self.assertIn("github-release", workflow)
        self.assertIn("github-manual", workflow)

    def test_tagged_recovery_checks_out_and_validates_existing_tag(self) -> None:
        """Manual publish recovery must build and validate the named tag."""
        workflow = (
            _REPO / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("inputs.release_tag || github.ref", workflow)
        self.assertIn('validate --tag "$RELEASE_TAG"', workflow)
        self.assertIn("github-release-recovery", workflow)

    def test_workflow_dispatch_triggers_build(self) -> None:
        """workflow_dispatch is listed as a trigger."""
        workflow = (
            _REPO / ".github" / "workflows" / "publish-pypi.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", workflow)

    def test_build_info_not_permanently_committed(self) -> None:
        """_build_info.json is gitignored and not tracked."""
        gitignore = (_REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("_build_info.json", gitignore)
        # It should not exist in the committed tree.
        info_path = _REPO / "src" / "agent_bridge_connect" / "_build_info.json"
        self.assertFalse(
            info_path.exists(),
            "_build_info.json must not be committed; it is a build artifact",
        )


if __name__ == "__main__":
    unittest.main()
