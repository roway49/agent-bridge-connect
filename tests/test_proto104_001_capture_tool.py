"""PROTO-104-001 controlled capture/validation tool tests.

The capture tool is the only sanctioned writer into the fixture matrix.  These
tests freeze its contract: closed probe whitelist, mandatory redaction of
tokens/paths/prompts/environment dumps, deterministic hashes and redacted
diffs, and refusal to stage anything that still carries private material.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "executor_runtime"
    / "tools"
    / "capture_protocol_fixture.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "capture_protocol_fixture", TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class ProbeWhitelistTests(unittest.TestCase):
    def test_probe_list_is_closed_and_official_only(self) -> None:
        for surface, argv in tool.PROBE_COMMANDS.items():
            with self.subTest(surface=surface):
                joined = " ".join(argv)
                self.assertTrue(
                    any(
                        token in joined
                        for token in (
                            "--version",
                            "--help",
                            "--check",
                            "generate-json-schema",
                        )
                    ),
                    f"{surface} is not a read-only official probe",
                )
        # Session deletion is never probed here; it stays a cleanup-capability
        # concern validated by the frozen help fixtures.
        delete_probes = {
            surface: argv
            for surface, argv in tool.PROBE_COMMANDS.items()
            if "delete" in " ".join(argv)
        }
        self.assertTrue(delete_probes)
        for surface, argv in delete_probes.items():
            with self.subTest(surface=surface):
                # Only ever a help probe; the tool never deletes anything.
                self.assertTrue(argv[-1] == "--help")
                self.assertNotIn("--yes", argv)
                self.assertNotIn("--force", argv)

    def test_unknown_surface_is_rejected_without_spawning(self) -> None:
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(SystemExit):
                tool.run_probe(
                    executor="hermes",
                    binary=Path("/bin/hermes"),
                    surface="export-session-body",
                    staging_dir=Path(tempfile.mkdtemp()),
                    timeout=1,
                )
            run.assert_not_called()

    def test_directory_surfaces_are_schema_generation_only(self) -> None:
        self.assertEqual(list(tool.DIRECTORY_SURFACES), ["app_server_schema"])


class RedactionTests(unittest.TestCase):
    def test_home_paths_are_normalized(self) -> None:
        home = Path.home()
        text, applied = tool.redact_text(f"installed at {home}/.hermes/hermes-agent")
        self.assertIn("home_path", applied)
        self.assertNotIn(str(home), text)
        self.assertIn("~/.hermes/hermes-agent", text)

    def test_tokens_bearer_and_jwt_are_redacted(self) -> None:
        samples = {
            "openai_key": "sk-abcdefghij0123456789",
            "bearer_credential": "Bearer abcdefghijklmnop1234567890",
            "github_token": "ghp_" + "a" * 24,
            "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz",
        }
        for name, sample in samples.items():
            with self.subTest(rule=name):
                redacted, applied = tool.redact_text(sample)
                self.assertIn(name, applied)
                self.assertNotEqual(redacted, sample)

    def test_private_session_store_paths_are_redacted(self) -> None:
        redacted, applied = tool.redact_text("session at ~/.codex/sessions/abc.jsonl")
        self.assertIn("private_session_store", applied)
        self.assertIn(tool._REDACTED, redacted)

    def test_env_assignment_dump_lines_are_redacted(self) -> None:
        redacted, applied = tool.redact_text("HERMES_YOLO_MODE=1\nnormal line")
        self.assertIn("assignment_env_dump", applied)
        self.assertIn("<redacted>", redacted)

    def test_forbidden_json_keys_are_scrubbed(self) -> None:
        value = {"prompt": "customer secret plan", "nested": {"env": {"PATH": "/usr/bin"}}}
        cleaned, applied = tool.redact_structured(value)
        self.assertEqual(cleaned["prompt"], "<redacted>")
        self.assertEqual(cleaned["nested"]["env"], "<redacted>")
        self.assertTrue(applied)

    def test_assert_no_secrets_fails_closed(self) -> None:
        with self.assertRaises(SystemExit):
            tool.assert_no_secrets("token ghp_" + "a" * 30, origin="help.txt")
        # Plain official output passes.
        tool.assert_no_secrets("usage: codex delete [OPTIONS] <SESSION>", origin="x")


class CaptureAndStagingTests(unittest.TestCase):
    def _fake_run(self, stdout: str, returncode: int = 0):
        completed = mock.Mock()
        completed.returncode = returncode
        completed.stdout = stdout
        completed.stderr = ""
        return completed

    def test_capture_records_redacted_probe_output(self) -> None:
        staging = Path(tempfile.mkdtemp())
        payload = (
            f"Hermes Agent v0.20.1\nInstall directory: {Path.home()}/.hermes/hermes-agent\n"
        )
        with mock.patch.object(
            tool.subprocess,
            "run",
            create=True,
            return_value=self._fake_run(payload),
        ):
            snapshot = tool.capture(
                executor="hermes",
                binary=Path("/bin/hermes"),
                version="0.20.1",
                staging_dir=staging,
                surfaces=["version"],
            )
        staged = json.loads((staging / "snapshot.json").read_text(encoding="utf-8"))
        record = staged["probes"][0]
        self.assertFalse(snapshot is None)
        self.assertEqual(record["redactions"], ["home_path"])
        self.assertNotIn(str(Path.home()), record["stdout"])

    def test_staged_snapshot_with_private_material_is_refused_on_import(self) -> None:
        staging = Path(tempfile.mkdtemp())
        (staging / "snapshot.json").write_text(
            json.dumps({"prompt": f"user prompt mentioning {Path.home()}/secret"}),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            tool.load_staging_snapshot(staging)
        self.assertIn("had to be redacted", str(ctx.exception))

    def test_review_reports_hashes_and_redacted_baseline_diff(self) -> None:
        staging = Path(tempfile.mkdtemp())
        baseline = (MATRIX_VERSION_DIR / "delete_help.txt").read_text(encoding="utf-8")
        (staging / "snapshot.json").write_text(
            json.dumps(
                {
                    "executor": "codex",
                    "version": "0.146.0",
                    "probes": [
                        {
                            "surface": "delete_help",
                            "ok": True,
                            "stdout": baseline + "\nExtra official line",
                            "stderr": "",
                            "returncode": 0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = tool.review(
            executor="codex", version=MATRIX_VERSION, staging_dir=staging
        )
        entry = report["files"]["delete_help.txt"]
        self.assertIn("sha256", entry)
        self.assertFalse(entry["unchanged"])
        self.assertGreater(entry["diff_lines"], 0)
        # The baseline hash is unchanged by a review; only promote() writes.
        self.assertEqual(
            tool.sha256_file(MATRIX_VERSION_DIR / "delete_help.txt"),
            tool.sha256_bytes(baseline.encode("utf-8")),
        )

    def test_manifest_hash_refresh_detects_drift(self) -> None:
        surfaces_before = json.loads(MATRIX_MANIFEST.read_text(encoding="utf-8"))[
            "executors"
        ]["codex"]["versions"][MATRIX_VERSION]["surfaces"]
        stale_entry = dict(surfaces_before)
        for metadata in stale_entry.values():
            metadata["sha256"] = "0" * 64
        problems = []
        for relative, metadata in stale_entry.items():
            path = MATRIX_VERSION_DIR / relative
            if path.is_file() and tool.sha256_file(path) != metadata["sha256"]:
                problems.append(relative)
        self.assertEqual(problems, sorted(surfaces_before))

    def test_verify_passes_for_the_current_matrix(self) -> None:
        self.assertTrue(tool.verify())


MATRIX_ROOT = REPO_ROOT / "tests" / "fixtures" / "executor_runtime" / "matrix"
MATRIX_MANIFEST = MATRIX_ROOT / "manifest.json"
MATRIX_VERSION = "0.146.0"
MATRIX_VERSION_DIR = MATRIX_ROOT / "codex" / MATRIX_VERSION


class PromotionGuardTests(unittest.TestCase):
    def test_promote_refuses_undeclared_versions_before_writing(self) -> None:
        manifest = json.loads(MATRIX_MANIFEST.read_text(encoding="utf-8"))
        absent_version = "99.0.0"
        self.assertNotIn(absent_version, manifest["executors"]["codex"]["versions"])
        staging = Path(tempfile.mkdtemp())
        (staging / "snapshot.json").write_text(
            json.dumps(
                {
                    "executor": "codex",
                    "version": absent_version,
                    "probes": [
                        {"surface": "version", "ok": True, "stdout": "x", "stderr": "",
                         "returncode": 0}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            tool.promote(
                executor="codex", version=absent_version, staging_dir=staging
            )
        self.assertIn("not declared in the manifest", str(ctx.exception))
        self.assertFalse((MATRIX_ROOT / "codex" / absent_version).exists())

    def test_known_executors_only(self) -> None:
        self.assertEqual(set(tool.KNOWN_EXECUTORS), {"codex", "claude", "hermes"})


if __name__ == "__main__":
    unittest.main()
