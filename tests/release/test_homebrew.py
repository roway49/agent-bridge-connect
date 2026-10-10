from __future__ import annotations
import importlib.util
import unittest
from pathlib import Path
import importlib.util as _parallel_monitor_importlib
import json as _parallel_monitor_json
import unittest as _parallel_monitor_unittest
from pathlib import Path as _parallel_monitor_Path
from unittest import mock as _parallel_monitor_mock
import importlib.util as _homebrew_rc_importlib
import json as _homebrew_rc_json
import os as _homebrew_rc_os
import sys as _homebrew_rc_sys
import tempfile as _homebrew_rc_tempfile
import unittest as _homebrew_rc_unittest
from pathlib import Path as _homebrew_rc_Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "generate_homebrew_formula.py"
SPEC = importlib.util.spec_from_file_location("generate_homebrew_formula", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class HomebrewFormulaTests(unittest.TestCase):
    def test_formula_is_manifest_pinned_and_has_service_and_smoke(self) -> None:
        manifest = {"tag": "v1.0.3A", "package_version": "1.0.3a1", "artifacts": [{"filename": "agentbc-1.0.3a1.tar.gz", "sha256": "a" * 64}, {"filename": "agentbc-1.0.3a1-py3-none-any.whl", "sha256": "b" * 64}]}
        formula = MODULE.generate_formula(manifest, "https://example.test/v1.0.3A")
        self.assertIn('url "https://example.test/v1.0.3A/agentbc-1.0.3a1.tar.gz"', formula)
        self.assertIn(f'''sha256 "{"a" * 64}"''', formula)
        self.assertIn('version "1.0.3a1"', formula)
        self.assertIn('depends_on "python"', formula)
        self.assertNotIn('depends_on "python@', formula)
        self.assertIn("virtualenv_install_with_resources", formula)
        self.assertIn("service do", formula)
        self.assertIn('"runner", "serve"', formula)
        self.assertIn("agentbc setup", formula)
        self.assertIn("brew services start agentbc", formula)

    def test_invalid_or_unpinned_manifest_is_rejected(self) -> None:
        cases = (
            {"tag": "v1.0.3A", "package_version": "1.0.3a1", "artifacts": []},
            {"tag": "v1.0.4A", "package_version": "1.0.3a1", "artifacts": [{"filename": "agentbc-1.0.3a1.tar.gz", "sha256": "a" * 64}]},
            {"tag": "v1.0.3A", "package_version": "1.0.3a1", "artifacts": [{"filename": "agentbc.tar.gz", "sha256": "bad"}]},
        )
        for manifest in cases:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                MODULE.generate_formula(manifest, "https://example.test")
        valid = {"tag": "v1.0.3A", "package_version": "1.0.3a1", "artifacts": [{"filename": "agentbc-1.0.3a1.tar.gz", "sha256": "a" * 64}]}
        with self.assertRaises(ValueError):
            MODULE.generate_formula(valid, "http://example.test")

    def test_formula_accepts_alpha_serial_and_rejects_mismatch(self) -> None:
        manifest = {"tag": "v1.0.3A2", "package_version": "1.0.3a2", "artifacts": [{"filename": "agentbc-1.0.3a2.tar.gz", "sha256": "a" * 64}]}
        formula = MODULE.generate_formula(manifest, "https://example.test/v1.0.3A2")
        self.assertIn('version "1.0.3a2"', formula)
        self.assertIn("agentbc-1.0.3a2.tar.gz", formula)
        mismatch = {**manifest, "package_version": "1.0.3a1"}
        with self.assertRaises(ValueError):
            MODULE.generate_formula(mismatch, "https://example.test/v1.0.3A2")

    def test_release_workflows_generate_and_publish_formula(self) -> None:
        root = Path(__file__).parents[2]
        publish = (root / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
        release_check = (root / ".github" / "workflows" / "release-check.yml").read_text(encoding="utf-8")
        for workflow in (publish, release_check):
            self.assertIn("scripts/generate_homebrew_formula.py", workflow)
            self.assertIn("ruby -c dist/agentbc.rb", workflow)
        self.assertIn("dist/agentbc.rb", publish)


def _parallel_monitor__load_monitor_module():
    path = _parallel_monitor_Path(__file__).resolve().parents[1] / "fixtures" / "archive_support" / "scripts" / "agentbc_parallel_monitor.py"
    spec = _parallel_monitor_importlib.spec_from_file_location("agentbc_parallel_monitor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load agentbc_parallel_monitor.py")
    module = _parallel_monitor_importlib.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _parallel_monitor_ParallelMonitorTests(_parallel_monitor_unittest.TestCase):
    def test_process_group_summary_splits_gui_and_workers(self) -> None:
        monitor = _parallel_monitor__load_monitor_module()
        rows = [
            {"command": "python -m agent_bridge_connect.cli worker run --executor codex", "pcpu": 10.0, "rss_kb": 100000, "group": monitor.classify_process("python -m agent_bridge_connect.cli worker run --executor codex")},
            {"command": "/Applications/Codex.app/Contents/MacOS/Codex", "pcpu": 2.0, "rss_kb": 500000, "group": monitor.classify_process("/Applications/Codex.app/Contents/MacOS/Codex")},
            {"command": "/Users/tester/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main dashboard", "pcpu": 1.0, "rss_kb": 900000, "group": monitor.classify_process("/Users/tester/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main dashboard")},
        ]
        groups = monitor.summarize_process_groups(rows)
        self.assertEqual(groups["agentbc_worker"]["rss_mb_sum"], 97.7)
        self.assertEqual(groups["codex_gui"]["rss_mb_sum"], 488.3)
        self.assertEqual(groups["hermes_gui"]["rss_mb_sum"], 878.9)

    def test_sample_processes_prefers_runner_process_sample(self) -> None:
        monitor = _parallel_monitor__load_monitor_module()
        runner_payload = {"ok": True, "source": "runner_ps", "count": 1, "cpu_sum": 3.0, "rss_kb_sum": 2048, "rss_mb_sum": 2.0, "groups": {"agentbc_runner": {"count": 1, "cpu_sum": 3.0, "rss_kb_sum": 2048, "rss_mb_sum": 2.0}}, "top": [], "ps_ok": True, "ps_error": ""}
        with _parallel_monitor_mock.patch.object(monitor, "run_command", return_value={"ok": True, "returncode": 0, "stdout": _parallel_monitor_json.dumps(runner_payload), "stderr": "", "elapsed_s": 0.01}) as run:
            sample = monitor.sample_processes(("agentbc",))
        run.assert_called_once_with(["agentbc", "runner", "process-sample", "--pattern", "agentbc"], timeout=7.0)
        self.assertEqual(sample["source"], "runner_ps")
        self.assertEqual(sample["rss_mb_sum"], 2.0)


_homebrew_rc_SCRIPT = _homebrew_rc_Path(__file__).parents[2] / "scripts" / "run_homebrew_rc_e2e.py"
_homebrew_rc_SPEC = _homebrew_rc_importlib.spec_from_file_location("run_homebrew_rc_e2e", _homebrew_rc_SCRIPT)
_homebrew_rc_MODULE = _homebrew_rc_importlib.module_from_spec(_homebrew_rc_SPEC)
assert _homebrew_rc_SPEC and _homebrew_rc_SPEC.loader
_homebrew_rc_sys.modules[_homebrew_rc_SPEC.name] = _homebrew_rc_MODULE
_homebrew_rc_SPEC.loader.exec_module(_homebrew_rc_MODULE)


class _homebrew_rc_HomebrewRcDriverTests(_homebrew_rc_unittest.TestCase):
    class FakeRunner:
        def __init__(self, *, doctor_ok: bool = True, doctor_output: str = "", dependency_ok: bool = True, dependency_installed: str = "3.14.3_1", dependency_current: str = "3.14.3_1") -> None:
            self.environment = {}
            self.commands: list[list[str]] = []
            self.doctor_ok = doctor_ok
            self.doctor_output = doctor_output
            self.dependency_ok = dependency_ok
            self.dependency_installed = dependency_installed
            self.dependency_current = dependency_current

        def run(self, argv, **_kwargs):
            command = [str(part) for part in argv]
            self.commands.append(command)
            stdout = ""
            returncode = 0
            if command[-1:] == ["--version"]:
                stdout = "Homebrew 5.0\n"
            elif command[-1:] == ["config"]:
                stdout = "HOMEBREW_PREFIX: /tmp\n"
            elif command[-1:] == ["doctor"]:
                returncode = 0 if self.doctor_ok else 1
                stdout = self.doctor_output
            elif command[-1:] == ["--prefix"]:
                stdout = "/tmp\n"
            elif command[-1:] in (["--cellar"], ["--cache"], ["--repository"]):
                stdout = "/tmp\n"
            elif command[-2:] == ["/usr/bin/uname", "-m"]:
                stdout = "arm64\n"
            elif command[-2:] == ["--versions", "python"]:
                if self.dependency_ok:
                    stdout = f"python@3.14 {self.dependency_installed}\n"
                else:
                    returncode = 1
            elif command[-3:] == ["info", "--json=v2", "python"]:
                stdout = _homebrew_rc_json.dumps({"formulae": [{"versions": {"stable": self.dependency_current}, "installed": [{"version": self.dependency_installed}]}]})
            elif command[-2:] == ["--versions", "agentbc"]:
                returncode = 1
            elif "x509" in command:
                stdout = "\n".join(("X509v3 Subject Alternative Name", "X509v3 Subject Key Identifier", "X509v3 Authority Key Identifier"))
            return _homebrew_rc_MODULE.CommandResult(command, returncode, stdout, "")

    def test_brew_environment_disables_all_automatic_mutation(self) -> None:
        environment = _homebrew_rc_MODULE.brew_environment({"PATH": "/bin"})
        self.assertEqual(environment["HOMEBREW_NO_AUTO_UPDATE"], "1")
        self.assertEqual(environment["HOMEBREW_NO_INSTALL_CLEANUP"], "1")
        self.assertEqual(environment["HOMEBREW_NO_AUTOREMOVE"], "1")
        self.assertEqual(environment["HOMEBREW_NO_ANALYTICS"], "1")

    def test_command_plan_uses_force_uninstall_and_never_autoremove(self) -> None:
        commands = _homebrew_rc_MODULE.command_plan("roway49/agentbc-rc", service=True)
        self.assertIn(["brew", "uninstall", "--force", "agentbc"], commands)
        self.assertIn(["brew", "trust", "--formula", "roway49/agentbc-rc/agentbc"], commands)
        self.assertIn(["brew", "untrust", "--formula", "roway49/agentbc-rc/agentbc"], commands)
        self.assertIn(["brew", "services", "start", "agentbc"], commands)
        self.assertIn(["brew", "services", "stop", "agentbc"], commands)
        self.assertFalse(any(("autoremove" in command for command in commands)))

    def test_formula_metadata_requires_ordered_versions_and_dependencies(self) -> None:
        with _homebrew_rc_tempfile.TemporaryDirectory() as temporary:
            formula = _homebrew_rc_Path(temporary) / "agentbc.rb"
            formula.write_text('class Agentbc < Formula\n  version "1.0.3a1"\n  depends_on "python"\nend\n', encoding="utf-8")
            self.assertEqual(_homebrew_rc_MODULE.formula_version(formula), "1.0.3a1")
            self.assertEqual(_homebrew_rc_MODULE.formula_dependencies(formula), ["python"])
            self.assertLess(_homebrew_rc_MODULE.version_key("1.0.2a1"), _homebrew_rc_MODULE.version_key("1.0.10a1"))

    def test_dependency_metadata_detects_current_and_outdated_versions(self) -> None:
        current = _homebrew_rc_json.dumps({"formulae": [{"versions": {"stable": "3.14.7"}, "installed": [{"version": "3.14.7"}]}]})
        self.assertEqual(_homebrew_rc_MODULE.homebrew_dependency_versions(current), (["3.14.7"], "3.14.7"))
        with self.assertRaises(_homebrew_rc_MODULE.RcError):
            _homebrew_rc_MODULE.homebrew_dependency_versions("{}")

    def test_stable_hash_ignores_runlease_heartbeat_but_semantics_do_not_drift(self) -> None:
        with _homebrew_rc_tempfile.TemporaryDirectory() as temporary:
            root = _homebrew_rc_Path(temporary)
            record = root / "record" / "ABCD" / "001"
            record.mkdir(parents=True)
            stable = root / "config.toml"
            stable.write_text("value = 1\n", encoding="utf-8")
            lease = record / "run_lease.json"
            value = {"run_id": "codex-ABCD-001-1", "task_id": "ABCD-001", "executor_id": "codex", "pid": 42, "pgid": 42, "work_dir": "/tmp/project", "started_at": "2026-08-22T00:00:00Z", "last_heartbeat_at": "2026-08-22T00:00:01Z", "cleanup_strategy": "kill_pgid", "state": "active"}
            lease.write_text(_homebrew_rc_json.dumps(value), encoding="utf-8")
            before_hash = _homebrew_rc_MODULE.stable_tree_sha256(root)
            before_lease = _homebrew_rc_MODULE.lease_semantics(root / "record")
            value["last_heartbeat_at"] = "2026-08-22T00:10:00Z"
            lease.write_text(_homebrew_rc_json.dumps(value), encoding="utf-8")
            self.assertEqual(_homebrew_rc_MODULE.stable_tree_sha256(root), before_hash)
            self.assertEqual(_homebrew_rc_MODULE.lease_semantics(root / "record"), before_lease)
            value["run_id"] = "changed"
            lease.write_text(_homebrew_rc_json.dumps(value), encoding="utf-8")
            self.assertNotEqual(_homebrew_rc_MODULE.lease_semantics(root / "record"), before_lease)

    def test_tls_gates_separate_ca_and_server_extensions(self) -> None:
        ca_complete = "\n".join(("X509v3 Subject Key Identifier", "X509v3 Authority Key Identifier"))
        server_complete = "\n".join(("X509v3 Subject Alternative Name", "X509v3 Subject Key Identifier", "X509v3 Authority Key Identifier"))
        self.assertTrue(_homebrew_rc_MODULE.ca_extensions_present(ca_complete))
        self.assertFalse(_homebrew_rc_MODULE.ca_extensions_present("X509v3 Subject Key Identifier"))
        self.assertTrue(_homebrew_rc_MODULE.server_extensions_present(server_complete))
        self.assertFalse(_homebrew_rc_MODULE.server_extensions_present(ca_complete))

    def test_real_run_has_explicit_environment_gate(self) -> None:
        self.assertEqual(_homebrew_rc_MODULE.GATE_ENV, "AGENTBC_HOMEBREW_RC_RUN")
        self.assertNotEqual(_homebrew_rc_os.environ.get(_homebrew_rc_MODULE.GATE_ENV), "1")

    def test_doctor_gate_distinguishes_advisory_and_blocking_findings(self) -> None:
        advisory = "Warning: You have unlinked kegs in your Cellar."
        blocking = "Warning: Your Command Line Tools are too outdated."
        self.assertEqual(_homebrew_rc_MODULE.brew_doctor_blockers(advisory), [])
        self.assertEqual(_homebrew_rc_MODULE.brew_doctor_blockers(blocking), ["clt_too_outdated"])

    def test_posix_write_gate_uses_owner_mode_not_dynamic_sandbox_access(self) -> None:
        with _homebrew_rc_tempfile.TemporaryDirectory() as temporary:
            root = _homebrew_rc_Path(temporary)
            writable = root / "writable"
            read_only = root / "read-only"
            writable.mkdir(mode=448)
            read_only.mkdir(mode=320)
            self.assertTrue(_homebrew_rc_MODULE.path_has_posix_write_permission(writable))
            self.assertFalse(_homebrew_rc_MODULE.path_has_posix_write_permission(read_only))

    def test_default_preservation_includes_config_skills_and_workspace(self) -> None:
        paths = [str(path) for path in _homebrew_rc_MODULE.default_preserve_paths(_homebrew_rc_Path("/tmp/home"))]
        self.assertIn("/tmp/home/.abc", paths)
        self.assertIn("/tmp/home/.codex/skills/agentbc", paths)
        self.assertIn("/tmp/home/.claude/skills/agentbc", paths)
        self.assertIn("/tmp/home/.hermes/skills/agentbc", paths)
        self.assertIn("/tmp/home/Documents/AgentBC/workspace", paths)

    def test_path_order_preserves_both_install_sources(self) -> None:
        with _homebrew_rc_tempfile.TemporaryDirectory() as temporary:
            root = _homebrew_rc_Path(temporary)
            local = root / "local" / "agentbc"
            brew = root / "Cellar" / "agentbc" / "bin" / "agentbc"
            local.parent.mkdir(parents=True)
            brew.parent.mkdir(parents=True)
            local.write_text("", encoding="utf-8")
            brew.write_text("", encoding="utf-8")
            local.chmod(493)
            brew.chmod(493)
            _homebrew_rc_MODULE._assert_path_order(str(local), brew, _homebrew_rc_os.pathsep.join((str(local.parent), "/usr/bin")))

    def test_preflight_is_read_only_and_reports_environment_blockers(self) -> None:
        with _homebrew_rc_tempfile.TemporaryDirectory() as temporary:
            root = _homebrew_rc_Path(temporary)
            old = root / "old.rb"
            new = root / "new.rb"
            ca = root / "ca.pem"
            server = root / "server.pem"
            old.write_text('version "1.0.2a1"\ndepends_on "python"\n', encoding="utf-8")
            new.write_text('version "1.0.3a1"\ndepends_on "python"\n', encoding="utf-8")
            ca.write_text("test", encoding="utf-8")
            server.write_text("test", encoding="utf-8")
            ready = self.FakeRunner()
            result = _homebrew_rc_MODULE._preflight(ready, brew=_homebrew_rc_Path("/opt/homebrew/bin/brew"), old_formula=old, new_formula=new, feed_url="https://example.test/", ca_cert=ca, server_cert=server, home=root, service=False, min_free_gib=0)
            self.assertTrue(result["ok"])
            self.assertFalse(any((command[1:2] in (["tap"], ["install"], ["upgrade"], ["uninstall"]) for command in ready.commands)))
            python_probe = next((command for command in ready.commands if "-c" in command))
            self.assertIn("ssl.create_default_context(cafile=cafile)", python_probe[-1])
            self.assertNotIn(str(ca), python_probe[-1])
            blocked = self.FakeRunner(doctor_ok=False, doctor_output="Warning: You have unlinked kegs in your Cellar.", dependency_ok=False)
            result = _homebrew_rc_MODULE._preflight(blocked, brew=_homebrew_rc_Path("/opt/homebrew/bin/brew"), old_formula=old, new_formula=new, feed_url="https://example.test/", ca_cert=ca, server_cert=server, home=root, service=False, min_free_gib=0)
            self.assertFalse(result["ok"])
            self.assertNotIn("brew_doctor", result["blockers"])
            self.assertEqual(result["brew_doctor_status"], "warnings")
            self.assertEqual(result["brew_doctor_blocking_findings"], [])
            self.assertIn("dependency_missing:python", result["blockers"])
            toolchain_blocked = self.FakeRunner(doctor_ok=False, doctor_output="Warning: Your Command Line Tools are too outdated.")
            result = _homebrew_rc_MODULE._preflight(toolchain_blocked, brew=_homebrew_rc_Path("/opt/homebrew/bin/brew"), old_formula=old, new_formula=new, feed_url="https://example.test/", ca_cert=ca, server_cert=server, home=root, service=False, min_free_gib=0)
            self.assertIn("brew_doctor:clt_too_outdated", result["blockers"])
            outdated = self.FakeRunner(dependency_installed="3.14.3_1", dependency_current="3.14.7")
            result = _homebrew_rc_MODULE._preflight(outdated, brew=_homebrew_rc_Path("/opt/homebrew/bin/brew"), old_formula=old, new_formula=new, feed_url="https://example.test/", ca_cert=ca, server_cert=server, home=root, service=False, min_free_gib=0)
            self.assertIn("dependency_outdated:python", result["blockers"])

    def test_preflight_rejects_incomplete_test_tls_pair(self) -> None:
        with _homebrew_rc_tempfile.TemporaryDirectory() as temporary:
            root = _homebrew_rc_Path(temporary)
            old = root / "old.rb"
            new = root / "new.rb"
            ca = root / "ca.pem"
            old.write_text('version "1.0.2a1"\n', encoding="utf-8")
            new.write_text('version "1.0.3a1"\n', encoding="utf-8")
            ca.write_text("test", encoding="utf-8")
            result = _homebrew_rc_MODULE._preflight(self.FakeRunner(), brew=_homebrew_rc_Path("/opt/homebrew/bin/brew"), old_formula=old, new_formula=new, feed_url="https://example.test/", ca_cert=ca, server_cert=None, home=root, service=False, min_free_gib=0)
            self.assertFalse(result["ok"])
            self.assertIn("test_tls_material_incomplete", result["blockers"])
