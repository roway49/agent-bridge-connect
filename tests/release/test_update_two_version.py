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
import hashlib as _update_flow_hashlib
import json as _update_flow_json
import os as _update_flow_os
import subprocess as _update_flow_subprocess
import tempfile as _update_flow_tempfile
import unittest as _update_flow_unittest
from pathlib import Path as _update_flow_Path
from unittest import mock as _update_flow_mock
from agent_bridge_connect.protocol import ABCError as _update_flow_ABCError
from agent_bridge_connect.service import TaskService as _update_flow_TaskService
from agent_bridge_connect import update as _update_flow_update_module
from agent_bridge_connect.update import _tag_to_package_version as _update_flow__tag_to_package_version, check_for_update as _update_flow_check_for_update, install_verified_release as _update_flow_install_verified_release, run_update_flow as _update_flow_run_update_flow

_DRIVER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_update_rc_e2e.py"
_SOURCE_PATH = Path(__file__).resolve().parents[2] / "src"


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
    return driver.Plan.derive(root=root, scenario=scenario, old_version=driver.DEFAULT_OLD_VERSION, new_version=driver.DEFAULT_NEW_VERSION, old_src=_SOURCE_PATH, new_src=_SOURCE_PATH, python=Path("/opt/homebrew/bin/python3.12"))


def _make_sample_evidence(scenario: str = "post_identity") -> dict:
    """Build a schema-complete evidence document with a passing fault contract."""
    skills = {}
    for platform in driver.PLATFORMS:
        skills[platform] = {"root": f"<root>/{platform}", "manifest_sha256_before": "a" * 64, "manifest_sha256_after": "a" * 64, "template_sha256_before": "b" * 64, "template_sha256_after": "b" * 64, "files_sha256_before": "c" * 64, "files_sha256_after": "c" * 64}
    return {
        "schema_version": driver.SCHEMA_VERSION,
        "task_id": driver.TASK_ID,
        "scenario": scenario,
        "source": {"old": {"version": "1.0.2a1", "commit_sha": "d" * 40, "source_tree_sha256": "e" * 64}, "new": {"version": "1.0.3a1", "commit_sha": "d" * 40, "source_tree_sha256": "e" * 64}, "commit_sha": "d" * 40, "source_tree_sha256": "e" * 64},
        "artifacts": {key: {"path": f"<root>/{key}.whl", "sha256": "f" * 64, "size": 123} for key in driver._ARTIFACT_KEYS},
        "commands": [{"argv": ["agentbc", "doctor", "--json"], "exit_code": 0}],
        "cli_link": {"target": "<root>/bin/agentbc", "readlink_before": "<root>/install/venv/bin/agentbc", "readlink_after": "<root>/install/venv/bin/agentbc", "restored": True},
        "skills": skills,
        "runner": {"identity_before": "match", "identity_after": "match", "status_before": "ready", "status_after": "ready", "pid_before": 100, "pid_after": 100, "spool": "<root>/spool", "single_runner": True},
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
        "outcome": {"expected": scenario, "actual": "update_error", "update_exit_code": 2, "known_pre_fix_failure": False, "reason": "update_error: Update failed; previous CLI, skills, and Runner were restored", "rollback_complete": True},
        "diagnosis": {"new_package_version": "1.0.3a1", "diagnosed": False, "mismatched": []},
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
        for real in (home, home / "Documents" / "AgentBC" / "workspace", home / ".agentbc-alpha", home / ".local" / "bin", home / "Library" / "LaunchAgents"):
            with self.subTest(path=str(real)):
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
        self.assertTrue(any(("old" in error and "src" in error for error in errors)))
        self.assertTrue(any(("new" in error and "src" in error for error in errors)))

    def test_version_ordering_is_required(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        plan = driver.Plan.derive(root=root, scenario="success", old_version="1.0.3a1", new_version="1.0.2a1", old_src=_SOURCE_PATH, new_src=_SOURCE_PATH)
        errors = driver.validate_plan(plan)
        self.assertTrue(any(("sort strictly after" in error for error in errors)))


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
        (ca_cert, server_cert, _server_key) = driver.make_tls(cert_dir)
        ca_text = driver.subprocess.run(["openssl", "x509", "-in", str(ca_cert), "-noout", "-text"], capture_output=True, text=True, check=True).stdout
        server_text = driver.subprocess.run(["openssl", "x509", "-in", str(server_cert), "-noout", "-text"], capture_output=True, text=True, check=True).stdout
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
        (root / "record" / ".agentbc-cutover-ready").write_text("other", encoding="utf-8")
        self.assertEqual(without_board, driver.dir_sha256_excluding(root, ("record",)))
        (root / "note.txt").write_text("changed", encoding="utf-8")
        self.assertNotEqual(without_board, driver.dir_sha256_excluding(root, ("record",)))

    def test_evidence_schema_accepts_complete_document(self) -> None:
        evidence = _make_sample_evidence()
        self.assertEqual(driver.validate_evidence(evidence), [])

    def test_evidence_schema_rejects_missing_keys(self) -> None:
        evidence = _make_sample_evidence()
        evidence.pop("outcome")
        evidence["skills"]["codex"].pop("files_sha256_after")
        errors = driver.validate_evidence(evidence)
        self.assertTrue(any(("outcome" in error for error in errors)))
        self.assertTrue(any(("skills.codex.files_sha256_after" in error for error in errors)))

    def test_evidence_schema_rejects_bad_digest(self) -> None:
        evidence = _make_sample_evidence()
        evidence["artifacts"]["new_wheel"]["sha256"] = "not-hex"
        errors = driver.validate_evidence(evidence)
        self.assertTrue(any(("sha256" in error for error in errors)))


class RedactionTests(unittest.TestCase):
    def test_redaction_replaces_root_home_and_secrets(self) -> None:
        root = _isolated_root()
        self.addCleanup(shutil.rmtree, root, True)
        home = root / "home"
        secret = "abc-token-value"
        text = f"path={root}/config/config.toml home={home} token={secret} nested={root}/spool/token"
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
        self.assertEqual(driver.fault_env_for("setup_refresh"), {driver.FAULT_ENV: "setup_refresh"})
        self.assertEqual(driver.fault_env_for("runner_start"), {driver.FAULT_ENV: "runner_start"})

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
        diagnosis = {"new_package_version": "1.0.3a1", "diagnosed": True, "mismatched": [{"platform": p, "classification": "modified", "up_to_date": False, "package_version": "1.0.2a1"} for p in driver.PLATFORMS]}
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
            exit_code = driver.main(["--old-src", str(_SOURCE_PATH), "--new-src", str(_SOURCE_PATH), "--scenario", "success"])
            self.assertEqual(exit_code, 3)
        finally:
            if previous is not None:
                os.environ[driver.GATE_ENV] = previous

    def test_plan_mode_exits_zero(self) -> None:
        exit_code = driver.main(["--plan", "--old-src", str(_SOURCE_PATH), "--new-src", str(_SOURCE_PATH), "--scenario", "success"])
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


def _update_flow__available() -> dict:
    from tests.support.updates import available_release

    return available_release()


class _update_flow_UpdateResolutionTests(_update_flow_unittest.TestCase):
    def _responses(self, *, tag: str = "v1.0.5A", tamper_manifest: bool = False):
        package = "1.0.5a1" if tag == "v1.0.5A" else "1.0.4a1"
        wheel_name = f"agentbc-{package}-py3-none-any.whl"
        wheel_sha = "b" * 64
        manifest = {"schema_version": 1, "tag": tag, "package_version": package, "commit_sha": "c" * 40, "source_tree_sha256": "d" * 64, "artifacts": [{"filename": wheel_name, "size": 10, "sha256": wheel_sha}]}
        manifest_bytes = _update_flow_json.dumps(manifest).encode()
        manifest_digest = _update_flow_hashlib.sha256(manifest_bytes).hexdigest()
        release = [
            {
                "tag_name": tag,
                "draft": False,
                "html_url": "https://example.test/release",
                "body": "release notes",
                "assets": [
                    {"name": "release-manifest.json", "browser_download_url": "https://example.test/manifest", "digest": f"sha256:{('0' * 64 if tamper_manifest else manifest_digest)}"},
                    {"name": wheel_name, "browser_download_url": "https://example.test/wheel", "digest": f"sha256:{wheel_sha}"},
                ],
            }
        ]
        payloads = {"https://example.test/releases": _update_flow_json.dumps(release).encode(), "https://example.test/manifest": manifest_bytes}
        return lambda url: payloads[url]

    def test_verified_manifest_reports_available_or_current(self) -> None:
        available = _update_flow_check_for_update(releases_url="https://example.test/releases", fetch_bytes=self._responses())
        self.assertTrue(available["update_available"])
        self.assertEqual(available["latest"], "1.0.5a1")
        current = _update_flow_check_for_update(releases_url="https://example.test/releases", fetch_bytes=self._responses(tag="v1.0.4A"))
        self.assertFalse(current["update_available"])
        self.assertEqual(current["state"], "current")

    def test_manifest_digest_mismatch_fails_closed(self) -> None:
        with self.assertRaises(_update_flow_ABCError) as raised:
            _update_flow_check_for_update(releases_url="https://example.test/releases", fetch_bytes=self._responses(tamper_manifest=True))
        self.assertEqual(raised.exception.code, "update_manifest_hash_mismatch")

    def test_alpha_serial_tag_mapping_is_stable_and_fail_closed(self) -> None:
        self.assertEqual(_update_flow__tag_to_package_version("v1.0.3A"), "1.0.3a1")
        self.assertEqual(_update_flow__tag_to_package_version("v1.0.3A2"), "1.0.3a2")
        self.assertEqual(_update_flow__tag_to_package_version("v1.0.3A12"), "1.0.3a12")
        for invalid in ("v1.0.3A0", "v1.0.3A02", "v1.0.3a2", "1.0.3A2"):
            with self.subTest(tag=invalid):
                self.assertIsNone(_update_flow__tag_to_package_version(invalid))


class _update_flow_UpdateFlowTests(_update_flow_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _update_flow_tempfile.TemporaryDirectory()
        self.install_home = _update_flow_mock.patch.dict("os.environ", {"HOME": self.temporary.name, "PATH": ""})
        self.install_home.start()
        shared = _update_flow_mock.patch("agent_bridge_connect.installation.shared_entries", return_value=())
        shared.start()
        self.addCleanup(shared.stop)
        self.addCleanup(self.install_home.stop)
        self.addCleanup(self.temporary.cleanup)
        self.board = _update_flow_Path(self.temporary.name) / "record"
        self.service = _update_flow_TaskService(self.board, config={"workspace_root": self.temporary.name})

    def test_current_and_decline_are_zero_write(self) -> None:
        before = sorted((str(path.relative_to(self.board)) for path in self.board.rglob("*")))
        current = {**_update_flow__available(), "state": "current", "latest": "1.0.3a1", "update_available": False}
        result = _update_flow_run_update_flow(self.service, checker=lambda: current, input_fn=lambda _prompt: self.fail("must not prompt"), output_fn=lambda _line: None)
        self.assertEqual(result["state"], "current")
        self.assertEqual(sorted((str(path.relative_to(self.board)) for path in self.board.rglob("*"))), before)
        installer = _update_flow_mock.Mock()
        result = _update_flow_run_update_flow(self.service, checker=_update_flow__available, input_fn=lambda _prompt: "n", output_fn=lambda _line: None, installer=installer)
        self.assertEqual(result["state"], "update_declined")
        self.assertEqual(sorted((str(path.relative_to(self.board)) for path in self.board.rglob("*"))), before)
        installer.assert_not_called()

    def test_explicit_yes_runs_preflight_then_installer(self) -> None:
        installer = _update_flow_mock.Mock(return_value={"version": "1.0.4a1", "runner_refreshed": True})
        result = _update_flow_run_update_flow(self.service, checker=_update_flow__available, input_fn=lambda _prompt: "yes", output_fn=lambda _line: None, installer=installer)
        self.assertEqual(result["state"], "updated")
        self.assertTrue(result["preflight"]["cutover_ready"])
        installer.assert_called_once()

    def test_homebrew_route_does_not_prompt_or_write_cutover_stamp(self) -> None:
        before = sorted((str(path.relative_to(self.board)) for path in self.board.rglob("*")))
        checker = _update_flow_mock.Mock(side_effect=AssertionError("must not check release index"))
        with _update_flow_mock.patch("agent_bridge_connect.update._local_install_strategy", return_value={"method": "homebrew", "reason": "Homebrew owns the CLI"}):
            result = _update_flow_run_update_flow(self.service, checker=checker, input_fn=lambda _prompt: self.fail("must not prompt"), output_fn=lambda _line: None)
        checker.assert_not_called()
        self.assertEqual(result["state"], "homebrew_update_required")
        self.assertEqual(result["upgrade_command"], "brew upgrade agentbc")
        self.assertEqual(result["source"], "homebrew")
        self.assertEqual(sorted((str(path.relative_to(self.board)) for path in self.board.rglob("*"))), before)

    def test_zero_write_routes_do_not_construct_task_service(self) -> None:
        service_factory = _update_flow_mock.Mock(side_effect=AssertionError("must remain lazy"))
        current = {**_update_flow__available(), "state": "current", "latest": "1.0.3a1", "update_available": False}
        result = _update_flow_run_update_flow(None, service_factory=service_factory, checker=lambda: current, input_fn=lambda _prompt: self.fail("must not prompt"), output_fn=lambda _line: None, installer=_update_flow_mock.Mock())
        self.assertEqual(result["state"], "current")
        service_factory.assert_not_called()
        service_factory.reset_mock()
        with _update_flow_mock.patch("agent_bridge_connect.update._local_install_strategy", return_value={"method": "homebrew", "reason": "Homebrew owns the CLI"}):
            result = _update_flow_run_update_flow(None, service_factory=service_factory, checker=_update_flow_mock.Mock(side_effect=AssertionError("must not check")), input_fn=lambda _prompt: self.fail("must not prompt"), output_fn=lambda _line: None)
        self.assertEqual(result["state"], "homebrew_update_required")
        service_factory.assert_not_called()

    def test_confirmed_update_constructs_task_service_once(self) -> None:
        service_factory = _update_flow_mock.Mock(return_value=self.service)
        installer = _update_flow_mock.Mock(return_value={"version": "1.0.4a1", "runner_refreshed": True})
        result = _update_flow_run_update_flow(None, service_factory=service_factory, checker=_update_flow__available, input_fn=lambda _prompt: "yes", output_fn=lambda _line: None, installer=installer)
        self.assertEqual(result["state"], "updated")
        service_factory.assert_called_once_with()

    def test_homebrew_runtime_python_is_detected_without_cli_argv_receipt(self) -> None:
        cellar_python = _update_flow_Path(self.temporary.name) / "Cellar" / "agentbc" / "1.0.3a1" / "libexec" / "bin" / "python"
        with _update_flow_mock.patch.object(_update_flow_update_module.sys, "executable", str(cellar_python)), _update_flow_mock.patch.object(_update_flow_update_module, "_invoked_cli_path", return_value=None):
            strategy = _update_flow_update_module._local_install_strategy()
        self.assertEqual(strategy["method"], "homebrew")
        self.assertIn("Python environment", strategy["reason"])

    def test_installer_refuses_unmanaged_cli_and_hash_failure_is_pre_switch(self) -> None:
        install_root = _update_flow_Path(self.temporary.name) / "install"
        bin_dir = _update_flow_Path(self.temporary.name) / "bin"
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.write_text("user-owned", encoding="utf-8")
        with _update_flow_mock.patch.dict("os.environ", {"AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(bin_dir)}), self.assertRaises(_update_flow_ABCError) as raised:
            _update_flow_install_verified_release(_update_flow__available())
        self.assertEqual(raised.exception.code, "update_install_unsupported")
        self.assertEqual(target.read_text(encoding="utf-8"), "user-owned")
        target.unlink()
        managed_cli = install_root / "venv" / "bin" / "agentbc"
        managed_cli.parent.mkdir(parents=True)
        managed_cli.write_text("managed", encoding="utf-8")
        target.symlink_to(managed_cli)
        with (
            _update_flow_mock.patch.dict("os.environ", {"AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(bin_dir)}),
            _update_flow_mock.patch("agent_bridge_connect.update._require_current_install_identity"),
            _update_flow_mock.patch("agent_bridge_connect.update._fetch_bytes", return_value=b"wrong"),
            self.assertRaises(_update_flow_ABCError) as raised,
        ):
            _update_flow_install_verified_release(_update_flow__available())
        self.assertEqual(raised.exception.code, "update_wheel_hash_mismatch")
        self.assertEqual(target.resolve(), managed_cli.resolve())

    def test_installer_refuses_homebrew_and_other_external_symlinks(self) -> None:
        install_root = _update_flow_Path(self.temporary.name) / "install"
        bin_dir = _update_flow_Path(self.temporary.name) / "bin"
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        cellar_cli = _update_flow_Path(self.temporary.name) / "Cellar" / "agentbc" / "1.0.3" / "bin" / "agentbc"
        cellar_cli.parent.mkdir(parents=True)
        cellar_cli.write_text("brew", encoding="utf-8")
        target.symlink_to(cellar_cli)
        environment = {"AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(bin_dir)}
        with _update_flow_mock.patch.dict("os.environ", environment), self.assertRaises(_update_flow_ABCError) as raised:
            _update_flow_install_verified_release(_update_flow__available())
        self.assertEqual(raised.exception.code, "update_homebrew_managed")
        self.assertEqual(target.resolve(), cellar_cli.resolve())
        target.unlink()
        external_cli = _update_flow_Path(self.temporary.name) / "pipx" / "bin" / "agentbc"
        external_cli.parent.mkdir(parents=True)
        external_cli.write_text("pipx", encoding="utf-8")
        target.symlink_to(external_cli)
        with _update_flow_mock.patch.dict("os.environ", environment), self.assertRaises(_update_flow_ABCError) as raised:
            _update_flow_install_verified_release(_update_flow__available())
        self.assertEqual(raised.exception.code, "update_install_unsupported")
        self.assertEqual(target.resolve(), external_cli.resolve())

    def test_identity_guards_require_runner_match_and_current_skills(self) -> None:
        from agent_bridge_connect.update import _require_current_install_identity, _require_post_update_identity

        current = {"package": {"version": "1.0.3a1", "status": "healthy"}, "runner": {"status": "ready", "identity": "match"}, "skills": {}}
        with _update_flow_mock.patch("agent_bridge_connect.update._doctor_report", return_value=current):
            _require_current_install_identity(_update_flow_Path("agentbc"), _update_flow__available())
        drifted = {"package": {"version": "1.0.4a1", "status": "healthy"}, "runner": {"status": "ready", "identity": "match"}, "skills": {"codex": {"installed": True, "up_to_date": False}, "claude": {"installed": False, "up_to_date": False}, "hermes": {"installed": True, "up_to_date": True}}}
        with _update_flow_mock.patch("agent_bridge_connect.update._doctor_report", return_value=drifted), self.assertRaises(_update_flow_ABCError) as raised:
            _require_post_update_identity(_update_flow_Path("agentbc"), "1.0.4a1")
        self.assertEqual(raised.exception.code, "update_skill_identity_mismatch")

    def _run_transaction_failure(self, failure: str) -> _update_flow_ABCError:
        """Exercise one post-stop failure and assert the transaction proof."""
        install_root = _update_flow_Path(self.temporary.name) / "install"
        bin_dir = _update_flow_Path(self.temporary.name) / "bin"
        old_cli = install_root / "venv" / "bin" / "agentbc"
        old_cli.parent.mkdir(parents=True)
        old_cli.write_text("old", encoding="utf-8")
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.symlink_to(old_cli)
        old_link = _update_flow_os.readlink(target)
        skill_base = _update_flow_Path(self.temporary.name) / "skills"
        codex_root = skill_base / "codex"
        claude_root = skill_base / "claude"
        hermes_home = skill_base / "hermes"
        hermes_roots = [hermes_home / "skills" / "agentbc", hermes_home / "profiles" / "alpha" / "skills" / "agentbc", hermes_home / "profiles" / "beta" / "skills" / "agentbc"]
        skill_specs = [("codex", codex_root), ("claude", claude_root), *(("hermes", root) for root in hermes_roots)]
        from agent_bridge_connect.setup import _current_skill_files
        from agent_bridge_connect.skill_packages import MANIFEST_NAME, build_skill_manifest, serialize_skill_manifest

        def write_package(root: _update_flow_Path, platform: str, version: str, *, target_version: bool, write_manifest: bool = True) -> None:
            root.mkdir(parents=True, exist_ok=True)
            base_files = _current_skill_files(platform)
            files = {relative_path: f"target:{platform}:{relative_path}".encode() if target_version else content for (relative_path, content) in base_files.items()}
            if target_version:
                files["new-managed.txt"] = b"introduced-by-target"
            manifest = build_skill_manifest(platform, version, files)
            for relative_path, content in files.items():
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            if write_manifest:
                (root / MANIFEST_NAME).write_bytes(serialize_skill_manifest(manifest))

        for platform, root in skill_specs:
            write_package(root, platform, "1.0.3a1", target_version=False)
            (root / "unrelated-user-file.txt").write_bytes(b"keep-me")
        original_managed: dict[tuple[_update_flow_Path, str], tuple[bytes, str]] = {}
        unrelated: dict[_update_flow_Path, bytes] = {}
        for _platform, root in skill_specs:
            for path in root.rglob("*"):
                if path.is_file() and path.name != "unrelated-user-file.txt":
                    content = path.read_bytes()
                    original_managed[root, str(path.relative_to(root))] = (content, _update_flow_hashlib.sha256(content).hexdigest())
            unrelated[root] = (root / "unrelated-user-file.txt").read_bytes()
        wheel_bytes = b"verified-wheel"
        release = {**_update_flow__available(), "wheel": {**_update_flow__available()["wheel"], "sha256": _update_flow_hashlib.sha256(wheel_bytes).hexdigest()}}
        current_identity = {"package": {"version": "1.0.3a1", "status": "healthy"}, "runner": {"status": "ready", "identity": "match"}, "skills": {}}
        target_skill_query = {
            "codex": {"root": str(codex_root), "files": sorted(set(_current_skill_files("codex")) | {"new-managed.txt", MANIFEST_NAME})},
            "claude": {"root": str(claude_root), "files": sorted(set(_current_skill_files("claude")) | {"new-managed.txt", MANIFEST_NAME})},
            "hermes": [{"root": str(root), "files": sorted(set(_current_skill_files("hermes")) | {"new-managed.txt", MANIFEST_NAME})} for (_platform, root) in skill_specs if _platform == "hermes"],
        }
        calls: list[list[str]] = []
        events: list[str] = []
        post_identity_calls: list[tuple[str, str]] = []

        def write_target_skills(*, commit_manifest: bool = True) -> None:
            for platform, root in skill_specs:
                write_package(root, platform, "1.0.4a1", target_version=True, write_manifest=commit_manifest)

        def completed(args, **_kwargs):
            command = [str(part) for part in args]
            calls.append(command)
            if len(command) > 1 and command[1] == "-c":
                return _update_flow_mock.Mock(returncode=0, stdout=_update_flow_json.dumps(target_skill_query), args=args)
            if "setup" in command:
                if failure == "setup":
                    raise _update_flow_subprocess.CalledProcessError(1, command)
                write_target_skills(commit_manifest=failure != "setup_partial")
                if failure == "setup_partial":
                    raise _update_flow_subprocess.CalledProcessError(1, command)
                return _update_flow_mock.Mock(returncode=0, stdout="", args=args)
            if command[-2:] == ["runner", "start"]:
                is_target = str(install_root / "versions") in command[0]
                events.append("target_runner_start" if is_target else "old_runner_start")
                if is_target and failure == "runner":
                    raise _update_flow_subprocess.CalledProcessError(1, command)
                return _update_flow_mock.Mock(returncode=0, stdout="", args=args)
            return _update_flow_mock.Mock(returncode=0, stdout="agentbc 1.0.4a1\n", args=args)

        def post_identity(cli: _update_flow_Path, version: str) -> None:
            post_identity_calls.append((str(cli), version))
            is_target = str(install_root / "versions") in str(cli)
            if is_target and failure in {"post_identity", "restore_identity"}:
                raise _update_flow_ABCError("update_identity_mismatch", "simulated target identity failure")
            if not is_target and failure == "restore_identity":
                raise _update_flow_ABCError("update_identity_mismatch", "simulated old identity failure")

        def stop_runner() -> dict[str, object]:
            events.append("runner_stop")
            return {"ok": True}

        environment = {"AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(bin_dir), "AGENTBC_CODEX_SKILL_PATH": str(codex_root), "AGENTBC_CLAUDE_SKILL_PATH": str(claude_root / "SKILL.md"), "AGENTBC_HERMES_SKILL_PATH": "", "HERMES_HOME": str(hermes_home)}
        with (
            _update_flow_mock.patch.dict("os.environ", environment, clear=False),
            _update_flow_mock.patch("agent_bridge_connect.update._require_current_install_identity", return_value=current_identity),
            _update_flow_mock.patch("agent_bridge_connect.update._fetch_bytes", return_value=wheel_bytes),
            _update_flow_mock.patch("agent_bridge_connect.update.subprocess.run", side_effect=completed),
            _update_flow_mock.patch("agent_bridge_connect.update._require_post_update_identity", side_effect=post_identity),
            _update_flow_mock.patch("agent_bridge_connect.runner.stop_runner_background", side_effect=stop_runner),
            self.assertRaises(_update_flow_ABCError) as raised,
        ):
            _update_flow_install_verified_release(release)
        expected_code = "update_rollback_incomplete" if failure == "restore_identity" else "update_install_failed"
        self.assertEqual(raised.exception.code, expected_code)
        self.assertEqual(_update_flow_os.readlink(target), old_link)
        self.assertEqual(target.resolve(), old_cli.resolve())
        self.assertEqual(events.count("runner_stop"), 2)
        self.assertEqual(events[-1], "old_runner_start")
        self.assertEqual(post_identity_calls[-1], (str(old_cli.resolve()), "1.0.3a1"))
        if failure == "restore_identity":
            self.assertNotIn("previous CLI, skills, and Runner were restored", raised.exception.message)
        else:
            self.assertIn("previous CLI, skills, and Runner were restored", raised.exception.message)
        for (root, relative_path), (content, digest) in original_managed.items():
            restored = (root / relative_path).read_bytes()
            self.assertEqual(restored, content)
            self.assertEqual(_update_flow_hashlib.sha256(restored).hexdigest(), digest)
        for root, content in unrelated.items():
            self.assertEqual((root / "unrelated-user-file.txt").read_bytes(), content)
            self.assertFalse((root / "new-managed.txt").exists())
        setup_commands = [command for command in calls if "setup" in command]
        self.assertEqual(len(setup_commands), 1)
        self.assertTrue("--update" in setup_commands[0])
        old_runner_commands = [command for command in calls if command[-2:] == ["runner", "start"] and str(install_root / "versions") not in command[0]]
        self.assertEqual(len(old_runner_commands), 1)
        return raised.exception

    def test_installed_skill_drift_blocks_before_runner_stop_or_switch(self) -> None:
        install_root = _update_flow_Path(self.temporary.name) / "install"
        bin_dir = _update_flow_Path(self.temporary.name) / "bin"
        old_cli = install_root / "venv" / "bin" / "agentbc"
        old_cli.parent.mkdir(parents=True)
        old_cli.write_text("old", encoding="utf-8")
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.symlink_to(old_cli)
        before_link = _update_flow_os.readlink(target)
        report = {"package": {"version": "1.0.3a1", "status": "healthy"}, "runner": {"status": "ready", "identity": "match"}, "skills": {"codex": {"installed": True, "up_to_date": True}, "hermes": {"installed": True, "up_to_date": True, "profiles": [{"installed": True, "up_to_date": False}]}}}
        environment = {"AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(bin_dir)}
        with (
            _update_flow_mock.patch.dict("os.environ", environment, clear=False),
            _update_flow_mock.patch("agent_bridge_connect.update._doctor_report", return_value=report),
            _update_flow_mock.patch("agent_bridge_connect.update._fetch_bytes") as fetch,
            _update_flow_mock.patch("agent_bridge_connect.runner.stop_runner_background") as stop,
            _update_flow_mock.patch("agent_bridge_connect.update.subprocess.run") as run,
            self.assertRaises(_update_flow_ABCError) as raised,
        ):
            _update_flow_install_verified_release(_update_flow__available())
        self.assertEqual(raised.exception.code, "update_skill_identity_mismatch")
        stop.assert_not_called()
        fetch.assert_not_called()
        run.assert_not_called()
        self.assertEqual(_update_flow_os.readlink(target), before_link)

    def test_setup_refresh_failure_restores_exact_transaction_snapshot(self) -> None:
        self._run_transaction_failure("setup")

    def test_partial_setup_failure_removes_unmanifested_target_managed_file(self) -> None:
        self._run_transaction_failure("setup_partial")

    def test_target_runner_start_failure_restores_exact_transaction_snapshot(self) -> None:
        self._run_transaction_failure("runner")

    def test_post_identity_failure_restores_exact_transaction_snapshot(self) -> None:
        self._run_transaction_failure("post_identity")

    def test_unverified_old_identity_returns_rollback_incomplete(self) -> None:
        self._run_transaction_failure("restore_identity")
