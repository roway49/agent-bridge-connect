from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_homebrew_rc_e2e.py"
SPEC = importlib.util.spec_from_file_location("run_homebrew_rc_e2e", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class HomebrewRcDriverTests(unittest.TestCase):
    class FakeRunner:
        def __init__(
            self,
            *,
            doctor_ok: bool = True,
            doctor_output: str = "",
            dependency_ok: bool = True,
            dependency_installed: str = "3.14.3_1",
            dependency_current: str = "3.14.3_1",
        ) -> None:
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
                stdout = json.dumps(
                    {
                        "formulae": [
                            {
                                "versions": {"stable": self.dependency_current},
                                "installed": [{"version": self.dependency_installed}],
                            }
                        ]
                    }
                )
            elif command[-2:] == ["--versions", "agentbc"]:
                returncode = 1
            elif "x509" in command:
                stdout = "\n".join(
                    (
                        "X509v3 Subject Alternative Name",
                        "X509v3 Subject Key Identifier",
                        "X509v3 Authority Key Identifier",
                    )
                )
            return MODULE.CommandResult(command, returncode, stdout, "")

    def test_brew_environment_disables_all_automatic_mutation(self) -> None:
        environment = MODULE.brew_environment({"PATH": "/bin"})
        self.assertEqual(environment["HOMEBREW_NO_AUTO_UPDATE"], "1")
        self.assertEqual(environment["HOMEBREW_NO_INSTALL_CLEANUP"], "1")
        self.assertEqual(environment["HOMEBREW_NO_AUTOREMOVE"], "1")
        self.assertEqual(environment["HOMEBREW_NO_ANALYTICS"], "1")

    def test_command_plan_uses_force_uninstall_and_never_autoremove(self) -> None:
        commands = MODULE.command_plan("roway49/agentbc-rc", service=True)
        self.assertIn(["brew", "uninstall", "--force", "agentbc"], commands)
        self.assertIn(
            ["brew", "trust", "--formula", "roway49/agentbc-rc/agentbc"],
            commands,
        )
        self.assertIn(
            ["brew", "untrust", "--formula", "roway49/agentbc-rc/agentbc"],
            commands,
        )
        self.assertIn(["brew", "services", "start", "agentbc"], commands)
        self.assertIn(["brew", "services", "stop", "agentbc"], commands)
        self.assertFalse(any("autoremove" in command for command in commands))

    def test_formula_metadata_requires_ordered_versions_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            formula = Path(temporary) / "agentbc.rb"
            formula.write_text(
                'class Agentbc < Formula\n  version "1.0.3a1"\n'
                '  depends_on "python"\nend\n',
                encoding="utf-8",
            )
            self.assertEqual(MODULE.formula_version(formula), "1.0.3a1")
            self.assertEqual(MODULE.formula_dependencies(formula), ["python"])
            self.assertLess(MODULE.version_key("1.0.2a1"), MODULE.version_key("1.0.10a1"))

    def test_dependency_metadata_detects_current_and_outdated_versions(self) -> None:
        current = json.dumps(
            {
                "formulae": [
                    {
                        "versions": {"stable": "3.14.7"},
                        "installed": [{"version": "3.14.7"}],
                    }
                ]
            }
        )
        self.assertEqual(
            MODULE.homebrew_dependency_versions(current),
            (["3.14.7"], "3.14.7"),
        )
        with self.assertRaises(MODULE.RcError):
            MODULE.homebrew_dependency_versions("{}")

    def test_stable_hash_ignores_runlease_heartbeat_but_semantics_do_not_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "record" / "ABCD" / "001"
            record.mkdir(parents=True)
            stable = root / "config.toml"
            stable.write_text("value = 1\n", encoding="utf-8")
            lease = record / "run_lease.json"
            value = {
                "run_id": "codex-ABCD-001-1",
                "task_id": "ABCD-001",
                "executor_id": "codex",
                "pid": 42,
                "pgid": 42,
                "work_dir": "/tmp/project",
                "started_at": "2026-08-22T00:00:00Z",
                "last_heartbeat_at": "2026-08-22T00:00:01Z",
                "cleanup_strategy": "kill_pgid",
                "state": "active",
            }
            lease.write_text(json.dumps(value), encoding="utf-8")
            before_hash = MODULE.stable_tree_sha256(root)
            before_lease = MODULE.lease_semantics(root / "record")
            value["last_heartbeat_at"] = "2026-08-22T00:10:00Z"
            lease.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(MODULE.stable_tree_sha256(root), before_hash)
            self.assertEqual(MODULE.lease_semantics(root / "record"), before_lease)
            value["run_id"] = "changed"
            lease.write_text(json.dumps(value), encoding="utf-8")
            self.assertNotEqual(MODULE.lease_semantics(root / "record"), before_lease)

    def test_tls_gates_separate_ca_and_server_extensions(self) -> None:
        ca_complete = "\n".join(
            (
                "X509v3 Subject Key Identifier",
                "X509v3 Authority Key Identifier",
            )
        )
        server_complete = "\n".join(
            (
                "X509v3 Subject Alternative Name",
                "X509v3 Subject Key Identifier",
                "X509v3 Authority Key Identifier",
            )
        )
        self.assertTrue(MODULE.ca_extensions_present(ca_complete))
        self.assertFalse(MODULE.ca_extensions_present("X509v3 Subject Key Identifier"))
        self.assertTrue(MODULE.server_extensions_present(server_complete))
        self.assertFalse(MODULE.server_extensions_present(ca_complete))

    def test_real_run_has_explicit_environment_gate(self) -> None:
        self.assertEqual(MODULE.GATE_ENV, "AGENTBC_HOMEBREW_RC_RUN")
        self.assertNotEqual(os.environ.get(MODULE.GATE_ENV), "1")

    def test_doctor_gate_distinguishes_advisory_and_blocking_findings(self) -> None:
        advisory = "Warning: You have unlinked kegs in your Cellar."
        blocking = "Warning: Your Command Line Tools are too outdated."
        self.assertEqual(MODULE.brew_doctor_blockers(advisory), [])
        self.assertEqual(MODULE.brew_doctor_blockers(blocking), ["clt_too_outdated"])

    def test_posix_write_gate_uses_owner_mode_not_dynamic_sandbox_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writable = root / "writable"
            read_only = root / "read-only"
            writable.mkdir(mode=0o700)
            read_only.mkdir(mode=0o500)
            self.assertTrue(MODULE.path_has_posix_write_permission(writable))
            self.assertFalse(MODULE.path_has_posix_write_permission(read_only))

    def test_default_preservation_includes_config_skills_and_workspace(self) -> None:
        paths = [str(path) for path in MODULE.default_preserve_paths(Path("/tmp/home"))]
        self.assertIn("/tmp/home/.abc", paths)
        self.assertIn("/tmp/home/.codex/skills/agentbc", paths)
        self.assertIn("/tmp/home/.claude/skills/agentbc", paths)
        self.assertIn("/tmp/home/.hermes/skills/agentbc", paths)
        self.assertIn("/tmp/home/Documents/AgentBC/workspace", paths)

    def test_path_order_preserves_both_install_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local" / "agentbc"
            brew = root / "Cellar" / "agentbc" / "bin" / "agentbc"
            local.parent.mkdir(parents=True)
            brew.parent.mkdir(parents=True)
            local.write_text("", encoding="utf-8")
            brew.write_text("", encoding="utf-8")
            local.chmod(0o755)
            brew.chmod(0o755)
            MODULE._assert_path_order(
                str(local),
                brew,
                os.pathsep.join((str(local.parent), "/usr/bin")),
            )

    def test_preflight_is_read_only_and_reports_environment_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.rb"
            new = root / "new.rb"
            ca = root / "ca.pem"
            server = root / "server.pem"
            old.write_text(
                'version "1.0.2a1"\ndepends_on "python"\n',
                encoding="utf-8",
            )
            new.write_text(
                'version "1.0.3a1"\ndepends_on "python"\n',
                encoding="utf-8",
            )
            ca.write_text("test", encoding="utf-8")
            server.write_text("test", encoding="utf-8")
            ready = self.FakeRunner()
            result = MODULE._preflight(
                ready,
                brew=Path("/opt/homebrew/bin/brew"),
                old_formula=old,
                new_formula=new,
                feed_url="https://example.test/",
                ca_cert=ca,
                server_cert=server,
                home=root,
                service=False,
                min_free_gib=0,
            )
            self.assertTrue(result["ok"])
            self.assertFalse(
                any(command[1:2] in (["tap"], ["install"], ["upgrade"], ["uninstall"])
                    for command in ready.commands)
            )
            python_probe = next(command for command in ready.commands if "-c" in command)
            self.assertIn("ssl.create_default_context(cafile=cafile)", python_probe[-1])
            self.assertNotIn(str(ca), python_probe[-1])

            blocked = self.FakeRunner(
                doctor_ok=False,
                doctor_output="Warning: You have unlinked kegs in your Cellar.",
                dependency_ok=False,
            )
            result = MODULE._preflight(
                blocked,
                brew=Path("/opt/homebrew/bin/brew"),
                old_formula=old,
                new_formula=new,
                feed_url="https://example.test/",
                ca_cert=ca,
                server_cert=server,
                home=root,
                service=False,
                min_free_gib=0,
            )
            self.assertFalse(result["ok"])
            self.assertNotIn("brew_doctor", result["blockers"])
            self.assertEqual(result["brew_doctor_status"], "warnings")
            self.assertEqual(result["brew_doctor_blocking_findings"], [])
            self.assertIn("dependency_missing:python", result["blockers"])

            toolchain_blocked = self.FakeRunner(
                doctor_ok=False,
                doctor_output="Warning: Your Command Line Tools are too outdated.",
            )
            result = MODULE._preflight(
                toolchain_blocked,
                brew=Path("/opt/homebrew/bin/brew"),
                old_formula=old,
                new_formula=new,
                feed_url="https://example.test/",
                ca_cert=ca,
                server_cert=server,
                home=root,
                service=False,
                min_free_gib=0,
            )
            self.assertIn("brew_doctor:clt_too_outdated", result["blockers"])

            outdated = self.FakeRunner(
                dependency_installed="3.14.3_1",
                dependency_current="3.14.7",
            )
            result = MODULE._preflight(
                outdated,
                brew=Path("/opt/homebrew/bin/brew"),
                old_formula=old,
                new_formula=new,
                feed_url="https://example.test/",
                ca_cert=ca,
                server_cert=server,
                home=root,
                service=False,
                min_free_gib=0,
            )
            self.assertIn("dependency_outdated:python", result["blockers"])

    def test_preflight_rejects_incomplete_test_tls_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.rb"
            new = root / "new.rb"
            ca = root / "ca.pem"
            old.write_text('version "1.0.2a1"\n', encoding="utf-8")
            new.write_text('version "1.0.3a1"\n', encoding="utf-8")
            ca.write_text("test", encoding="utf-8")
            result = MODULE._preflight(
                self.FakeRunner(),
                brew=Path("/opt/homebrew/bin/brew"),
                old_formula=old,
                new_formula=new,
                feed_url="https://example.test/",
                ca_cert=ca,
                server_cert=None,
                home=root,
                service=False,
                min_free_gib=0,
            )
            self.assertFalse(result["ok"])
            self.assertIn("test_tls_material_incomplete", result["blockers"])


if __name__ == "__main__":
    unittest.main()
