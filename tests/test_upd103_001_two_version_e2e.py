"""Unit tests for the isolated two-version Update RC E2E driver (UPD-103-001).

These tests cover the driver's pure logic: isolation guards, command
construction, evidence schema, hash comparison, redaction, fault selection,
fault-contract evaluation, and cleanup targeting.  They never run the slow
real two-version update; that run is opt-in behind ``AGENTBC_E2E_RUN_REAL=1``
(see ``scripts/run_update_rc_e2e.py`` module documentation for the exact
invocation).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "run_update_rc_e2e.py"
)
_SOURCE_PATH = Path(__file__).resolve().parent.parent / "src"


def _load_driver():
    spec = importlib.util.spec_from_file_location("run_update_rc_e2e", _DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def _isolated_root() -> Path:
    return Path(tempfile.mkdtemp(prefix=driver.TEMP_PREFIX))


def _make_plan(root: Path, scenario: str = "post_identity") -> driver.Plan:
    return driver.Plan.derive(
        root=root,
        scenario=scenario,
        old_version=driver.DEFAULT_OLD_VERSION,
        new_version=driver.DEFAULT_NEW_VERSION,
        old_src=_SOURCE_PATH,
        new_src=_SOURCE_PATH,
        python=Path("/opt/homebrew/bin/python3.12"),
    )


def _make_sample_evidence(scenario: str = "post_identity") -> dict:
    """Build a schema-complete evidence document with a passing fault contract."""
    skills = {}
    for platform in driver.PLATFORMS:
        skills[platform] = {
            "root": f"<root>/{platform}",
            "manifest_sha256_before": "a" * 64,
            "manifest_sha256_after": "a" * 64,
            "template_sha256_before": "b" * 64,
            "template_sha256_after": "b" * 64,
            "files_sha256_before": "c" * 64,
            "files_sha256_after": "c" * 64,
        }
    return {
        "schema_version": driver.SCHEMA_VERSION,
        "task_id": driver.TASK_ID,
        "scenario": scenario,
        "source": {
            "old": {"version": "1.0.2a1", "commit_sha": "d" * 40, "source_tree_sha256": "e" * 64},
            "new": {"version": "1.0.3a1", "commit_sha": "d" * 40, "source_tree_sha256": "e" * 64},
            "commit_sha": "d" * 40,
            "source_tree_sha256": "e" * 64,
        },
        "artifacts": {
            key: {"path": f"<root>/{key}.whl", "sha256": "f" * 64, "size": 123}
            for key in driver._ARTIFACT_KEYS
        },
        "commands": [{"argv": ["agentbc", "doctor", "--json"], "exit_code": 0}],
        "cli_link": {
            "target": "<root>/bin/agentbc",
            "readlink_before": "<root>/install/venv/bin/agentbc",
            "readlink_after": "<root>/install/venv/bin/agentbc",
            "restored": True,
        },
        "skills": skills,
        "runner": {
            "identity_before": "match",
            "identity_after": "match",
            "status_before": "ready",
            "status_after": "ready",
            "pid_before": 100,
            "pid_after": 100,
            "spool": "<root>/spool",
            "single_runner": True,
        },
        "stable_data": {
            "config_sha256_before": "c" * 64,
            "config_sha256_after": "c" * 64,
            "workspace_sha256_before": "d" * 64,
            "workspace_sha256_after": "d" * 64,
            "board_sha256_before": "e" * 64,
            "board_sha256_after": "e" * 64,
            "workspace_data_sha256_before": "d" * 64,
            "workspace_data_sha256_after": "d" * 64,
            "board_data_sha256_before": "e" * 64,
            "board_data_sha256_after": "e" * 64,
        },
        "outcome": {
            "expected": scenario,
            "actual": "update_error",
            "update_exit_code": 2,
            "known_pre_fix_failure": False,
            "reason": "update_error: Update failed; previous CLI, skills, and Runner were restored",
            "rollback_complete": True,
        },
        "diagnosis": {
            "new_package_version": "1.0.3a1",
            "diagnosed": False,
            "mismatched": [],
        },
        "contract": {},
    }


class ModuleContractTests(unittest.TestCase):
    def test_module_documents_real_run_gate_invocation(self) -> None:
        docstring = driver.__doc__ or ""
        self.assertIn("AGENTBC_E2E_RUN_REAL=1", docstring)
        self.assertIn("scripts/run_update_rc_e2e.py", docstring)

    def test_required_constants_are_explicit(self) -> None:
        self.assertEqual(driver.DEFAULT_OLD_VERSION, "1.0.2a1")
        self.assertEqual(driver.DEFAULT_NEW_VERSION, "1.0.3a1")
        self.assertEqual(set(driver.SCENARIOS), {"success", "setup_refresh", "runner_start", "post_identity"})
        self.assertEqual(set(driver.PLATFORMS), {"codex", "claude", "hermes"})


class IsolationGuardTests(unittest.TestCase):
    def test_isolation_guards_reject_real_roots(self) -> None:
        home = Path.home()
        for real in (
            home,
            home / "Documents" / "AgentBC" / "workspace",
            home / ".agentbc-alpha",
            home / ".local" / "bin",
            home / "Library" / "LaunchAgents",
        ):
            with self.subTest(path=str(real)):
                if not real.exists():
                    real.mkdir(parents=True, exist_ok=True)
                with self.assertRaises(driver.IsolationError):
                    driver.assert_temporary_root(real)

    def test_isolation_guard_accepts_fresh_temp_root(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        driver.assert_temporary_root(root)

    def test_isolation_guard_rejects_non_temp_root(self) -> None:
        scratch = Path(tempfile.mkdtemp()) / "not-our-prefix"
        scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scratch, True)
        with self.assertRaises(driver.IsolationError):
            driver.assert_temporary_root(scratch)

    def test_isolated_paths_must_stay_under_root(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "bin").mkdir()
        driver.assert_isolated_paths(root, (root / "bin", root / "config" / "config.toml"))
        with self.assertRaises(driver.IsolationError):
            driver.assert_isolated_paths(root, (Path.home() / "Documents",))

    def test_plan_refuses_missing_source_or_wheel(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        plan = driver.Plan.derive(root=root, scenario="success", old_version="1.0.2a1", new_version="1.0.3a1")
        errors = driver.validate_plan(plan)
        self.assertTrue(any("old" in error and "src" in error for error in errors))
        self.assertTrue(any("new" in error and "src" in error for error in errors))

    def test_version_ordering_is_required(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        plan = driver.Plan.derive(
            root=root,
            scenario="success",
            old_version="1.0.3a1",
            new_version="1.0.2a1",
            old_src=_SOURCE_PATH,
            new_src=_SOURCE_PATH,
        )
        errors = driver.validate_plan(plan)
        self.assertTrue(any("sort strictly after" in error for error in errors))


class CommandConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _isolated_root()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.plan = _make_plan(self.root)

    def test_update_command_uses_only_isolated_roots(self) -> None:
        argv = driver.build_update_argv(self.plan)
        self.assertEqual(argv[0], str(self.plan.bin_dir / "agentbc"))
        self.assertIn("--root", argv)
        self.assertIn(str(self.plan.board_root), argv)
        self.assertIn("--config", argv)
        self.assertIn(str(self.plan.config_path), argv)
        joined = " ".join(argv)
        self.assertNotIn("Documents/AgentBC/workspace", joined)
        self.assertNotIn(str(Path.home()), joined)

    def test_runner_stop_targets_isolated_spool_only(self) -> None:
        argv = driver.build_runner_stop_argv(self.plan)
        self.assertIn("--spool", argv)
        self.assertIn(str(self.plan.spool_root), argv)
        joined = " ".join(argv)
        self.assertNotIn("agentbc-runner-v2", joined)

    def test_env_never_uses_default_user_locations(self) -> None:
        env = driver.build_env(self.plan)
        self.assertEqual(env["HOME"], str(self.plan.home))
        self.assertEqual(env["AGENTBC_ALPHA_HOME"], str(self.plan.install_root))
        self.assertEqual(env["AGENTBC_BIN_DIR"], str(self.plan.bin_dir))
        self.assertEqual(env["AGENTBC_RUNNER_SPOOL"], str(self.plan.spool_root))
        for key in ("AGENTBC_CODEX_BIN", "AGENTBC_CLAUDE_BIN", "AGENTBC_HERMES_BIN"):
            self.assertEqual(Path(env[key]).parent, self.plan.bin_dir)

    def test_generated_tls_ca_is_valid_for_server_auth(self) -> None:
        cert_dir = self.root / "certs"
        ca_cert, server_cert, _server_key = driver.make_tls(cert_dir)
        ca_text = driver.subprocess.run(
            ["openssl", "x509", "-in", str(ca_cert), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        server_text = driver.subprocess.run(
            ["openssl", "x509", "-in", str(server_cert), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("CA:TRUE", ca_text)
        self.assertIn("TLS Web Server Authentication", server_text)
        self.assertIn("Authority Key Identifier", server_text)
        self.assertIn("IP Address:127.0.0.1", server_text)


class HashAndEvidenceTests(unittest.TestCase):
    def test_hash_comparison_is_case_insensitive_and_strict(self) -> None:
        self.assertTrue(driver.hash_equal("A" * 64, "a" * 64))
        self.assertTrue(driver.hash_equal("a" * 64, "a" * 64))
        self.assertFalse(driver.hash_equal("a" * 64, "b" * 64))
        self.assertFalse(driver.hash_equal("a" * 63, "a" * 63))
        self.assertFalse(driver.hash_equal("not-a-digest", "a" * 64))

    def test_dir_sha256_is_deterministic_and_content_sensitive(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "a").mkdir()
        (root / "a" / "file.txt").write_text("hello", encoding="utf-8")
        first = driver.dir_sha256(root)
        self.assertEqual(first, driver.dir_sha256(root))
        (root / "a" / "file.txt").write_text("changed", encoding="utf-8")
        self.assertNotEqual(first, driver.dir_sha256(root))

    def test_version_key_orders_alpha_versions(self) -> None:
        self.assertLess(driver.version_key("1.0.2a1"), driver.version_key("1.0.3a1"))
        with self.assertRaises(ValueError):
            driver.version_key("not-a-version")

    def test_dir_sha256_excluding_skips_subtrees(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "record").mkdir(parents=True)
        (root / "record" / "task.json").write_text("{}", encoding="utf-8")
        (root / "record" / ".agentbc-cutover-ready").write_text("stamp", encoding="utf-8")
        (root / "note.txt").write_text("hello", encoding="utf-8")
        with_stamp = driver.dir_sha256_excluding(root, ())
        without_board = driver.dir_sha256_excluding(root, ("record",))
        self.assertNotEqual(with_stamp, without_board)
        # Removing the stamp must not change the board-data digest.
        (root / "record" / ".agentbc-cutover-ready").write_text("other", encoding="utf-8")
        self.assertEqual(
            without_board,
            driver.dir_sha256_excluding(root, ("record",)),
        )
        (root / "note.txt").write_text("changed", encoding="utf-8")
        self.assertNotEqual(
            without_board,
            driver.dir_sha256_excluding(root, ("record",)),
        )

    def test_evidence_schema_accepts_complete_document(self) -> None:
        evidence = _make_sample_evidence()
        self.assertEqual(driver.validate_evidence(evidence), [])

    def test_evidence_schema_rejects_missing_keys(self) -> None:
        evidence = _make_sample_evidence()
        evidence.pop("outcome")
        evidence["skills"]["codex"].pop("files_sha256_after")
        errors = driver.validate_evidence(evidence)
        self.assertTrue(any("outcome" in error for error in errors))
        self.assertTrue(any("skills.codex.files_sha256_after" in error for error in errors))

    def test_evidence_schema_rejects_bad_digest(self) -> None:
        evidence = _make_sample_evidence()
        evidence["artifacts"]["new_wheel"]["sha256"] = "not-hex"
        errors = driver.validate_evidence(evidence)
        self.assertTrue(any("sha256" in error for error in errors))


class RedactionTests(unittest.TestCase):
    def test_redaction_replaces_root_home_and_secrets(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        home = root / "home"
        secret = "abc-token-value"
        text = (
            f"path={root}/config/config.toml home={home} "
            f"token={secret} nested={root}/spool/token"
        )
        redacted = driver.redact_text(text, root=root, home=home, secrets=[secret])
        self.assertNotIn(str(root), redacted)
        self.assertNotIn(str(home), redacted)
        self.assertNotIn(secret, redacted)
        self.assertIn("<TMPROOT>", redacted)
        self.assertIn("<REDACTED>", redacted)


class FaultSelectionTests(unittest.TestCase):
    def test_fault_env_mapping(self) -> None:
        self.assertEqual(driver.fault_env_for("success"), {})
        self.assertEqual(driver.fault_env_for("post_identity"), {})
        self.assertEqual(
            driver.fault_env_for("setup_refresh"),
            {driver.FAULT_ENV: "setup_refresh"},
        )
        self.assertEqual(
            driver.fault_env_for("runner_start"),
            {driver.FAULT_ENV: "runner_start"},
        )

    def test_fault_command_mapping(self) -> None:
        self.assertEqual(driver.fault_command_for("setup_refresh"), "setup")
        self.assertEqual(driver.fault_command_for("runner_start"), "runner")
        self.assertEqual(driver.fault_command_for("success"), "")
        self.assertEqual(driver.fault_command_for("post_identity"), "")

    def test_fault_patch_source_is_deterministic(self) -> None:
        self.assertEqual(driver.fault_patch_source(), driver.fault_patch_source())
        self.assertIn("AGENTBC_E2E_FAULT", driver.fault_patch_source())
        self.assertIn("return 3", driver.fault_patch_source())


class ContractEvaluationTests(unittest.TestCase):
    def test_fault_contract_passes_when_restored(self) -> None:
        evidence = _make_sample_evidence("post_identity")
        verdict = driver.evaluate_outcome("post_identity", evidence)
        self.assertTrue(verdict["ok"])
        self.assertIn("cli_link_restored", verdict["passes"])
        self.assertIn("skills_restored", verdict["passes"])
        self.assertIn("no_second_runner", verdict["passes"])
        self.assertFalse(verdict["known_pre_fix_failure"])

    def test_fault_contract_detects_unrestored_skills(self) -> None:
        evidence = _make_sample_evidence("setup_refresh")
        evidence["skills"]["codex"]["files_sha256_after"] = "9" * 64
        verdict = driver.evaluate_outcome("setup_refresh", evidence)
        self.assertIn("skills_restored", verdict["failures"])
        self.assertTrue(verdict["known_pre_fix_failure"])

    def test_fault_contract_detects_second_runner(self) -> None:
        evidence = _make_sample_evidence("runner_start")
        evidence["runner"]["single_runner"] = False
        verdict = driver.evaluate_outcome("runner_start", evidence)
        self.assertIn("no_second_runner", verdict["failures"])

    def test_success_contract_reports_known_pre_fix_failure(self) -> None:
        evidence = _make_sample_evidence("success")
        evidence["outcome"]["actual"] = "update_error"
        evidence["outcome"]["update_exit_code"] = 2
        evidence["outcome"]["known_pre_fix_failure"] = True
        evidence["outcome"]["reason"] = "update_skill_identity_mismatch: Updated managed skills are not version-matched"
        evidence["cli_link"]["restored"] = True
        evidence["cli_link"]["readlink_after"] = evidence["cli_link"]["readlink_before"]
        verdict = driver.evaluate_outcome("success", evidence)
        self.assertIn("update_succeeded", verdict["failures"])
        self.assertTrue(verdict["known_pre_fix_failure"])

    def test_diagnosis_marks_known_pre_fix_failure_in_evidence(self) -> None:
        diagnosis = {
            "new_package_version": "1.0.3a1",
            "diagnosed": True,
            "mismatched": [
                {"platform": p, "classification": "modified", "up_to_date": False, "package_version": "1.0.2a1"}
                for p in driver.PLATFORMS
            ],
        }
        self.assertTrue(driver.known_pre_fix_failure(diagnosis, "update_error", "post_identity"))
        self.assertTrue(driver.known_pre_fix_failure(diagnosis, "update_error", "success"))
        self.assertFalse(driver.known_pre_fix_failure(diagnosis, "update_error", "setup_refresh"))
        self.assertFalse(driver.known_pre_fix_failure(diagnosis, "updated", "post_identity"))
        self.assertFalse(driver.known_pre_fix_failure({"diagnosed": False}, "update_error", "post_identity"))


class CleanupAndRunnerTests(unittest.TestCase):
    def test_runner_pids_filter_only_isolated_spool(self) -> None:
        spool = Path("/tmp/agentbc-update-rc-e2e-abc/spool")
        lines = [
            "100 /usr/bin/python -m agent_bridge_connect.cli runner serve --spool /tmp/agentbc-update-rc-e2e-abc/spool",
            "101 /usr/bin/python -m agent_bridge_connect.cli runner serve --spool /tmp/agentbc-runner-v2-501",
            "102 /usr/bin/python -m something_else --spool /tmp/agentbc-update-rc-e2e-abc/spool",
            "not-a-pid /usr/bin/foo",
        ]
        pids = driver.runner_pids_for_spool(lines, spool)
        self.assertEqual(pids, [100])

    def test_cleanup_stop_command_is_isolated(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        plan = _make_plan(root)
        argv = driver.build_runner_stop_argv(plan)
        self.assertNotIn("agentbc-runner-v2", " ".join(argv))
        self.assertIn(str(plan.spool_root), argv)


class RealRunGateTests(unittest.TestCase):
    def test_real_run_requires_environment_gate(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        plan = _make_plan(root)
        previous = os.environ.pop(driver.GATE_ENV, None)
        try:
            with self.assertRaises(driver.GateNotEnabled):
                driver.real_run(plan)
        finally:
            if previous is not None:
                os.environ[driver.GATE_ENV] = previous

    def test_cli_exits_3_without_gate(self) -> None:
        previous = os.environ.pop(driver.GATE_ENV, None)
        try:
            exit_code = driver.main(
                [
                    "--old-src",
                    str(_SOURCE_PATH),
                    "--new-src",
                    str(_SOURCE_PATH),
                    "--scenario",
                    "success",
                ]
            )
            self.assertEqual(exit_code, 3)
        finally:
            if previous is not None:
                os.environ[driver.GATE_ENV] = previous

    def test_plan_mode_exits_zero(self) -> None:
        exit_code = driver.main(
            [
                "--plan",
                "--old-src",
                str(_SOURCE_PATH),
                "--new-src",
                str(_SOURCE_PATH),
                "--scenario",
                "success",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_wheel_build_info_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "sample.whl"
            info = {"schema_version": 1, "package_version": "1.0.2a1", "commit_sha": "c" * 40}
            with driver.zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("agent_bridge_connect/_build_info.json", json.dumps(info))
            self.assertEqual(driver.read_wheel_build_info(wheel), info)
            wheel.unlink()
            self.assertIsNone(driver.read_wheel_build_info(wheel))


if __name__ == "__main__":
    unittest.main()
