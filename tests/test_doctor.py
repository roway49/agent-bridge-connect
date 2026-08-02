"""Tests for the read-only AgentBC doctor contract."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect import __version__
from agent_bridge_connect.cli import main
from agent_bridge_connect.doctor import (
    build_doctor_report,
    detect_install_source,
    render_doctor_text,
)
from agent_bridge_connect.runner import RunnerError, RunnerState, _dispatch_request


class FakeDistribution:
    def __init__(self, direct_url: dict | None = None) -> None:
        self.direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json" or self.direct_url is None:
            return None
        return json.dumps(self.direct_url)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.module = self.root / "installed" / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.executable = self.root / "venv" / "bin" / "agentbc"
        self.python = self.root / "venv" / "bin" / "python"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runner_health(
        self,
        *,
        python_executable: Path | None = None,
        module_path: Path | None = None,
    ) -> dict:
        return {
            "ok": True,
            "status": "ready",
            "pid": 1234,
            "python_executable": str(python_executable or self.python),
            "module_path": str(module_path or self.module),
            "executors": ["hermes", "codex"],
        }

    def _report(self, **overrides) -> dict:
        arguments = {
            "config_path": self.config,
            "runner_health": self._runner_health,
            "module_path": self.module,
            "executable_path": self.executable,
            "python_executable": self.python,
            "distribution": FakeDistribution(),
            "candidate_marker_paths": [],
            "build_info_path": self.build_info,
        }
        arguments.update(overrides)
        return build_doctor_report(**arguments)

    def test_source_checkout_install_source(self) -> None:
        checkout = self.root / "checkout"
        module = checkout / "src" / "agent_bridge_connect" / "__init__.py"
        module.parent.mkdir(parents=True)
        module.write_text("", encoding="utf-8")
        (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (checkout / ".git").mkdir()

        result = detect_install_source(
            module,
            distribution=None,
            candidate_marker_paths=[],
        )

        self.assertEqual(result, "source_checkout")

    def test_candidate_marker_has_highest_priority(self) -> None:
        marker = self.root / ".agentbc-candidate"
        marker.write_text("candidate\n", encoding="utf-8")
        distribution = FakeDistribution(
            {"url": "file:///checkout", "dir_info": {"editable": True}}
        )

        result = detect_install_source(
            self.module,
            distribution=distribution,
            candidate_marker_paths=[marker],
        )

        self.assertEqual(result, "candidate")

    def test_pypi_like_distribution(self) -> None:
        result = detect_install_source(
            self.module,
            distribution=FakeDistribution(),
            candidate_marker_paths=[],
        )

        self.assertEqual(result, "pypi")

    def test_editable_and_direct_url_distributions(self) -> None:
        cases = (
            (
                {"url": "file:///checkout", "dir_info": {"editable": True}},
                "editable",
            ),
            (
                {"url": "https://example.invalid/agentbc.whl", "archive_info": {}},
                "direct_url",
            ),
        )
        for direct_url, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    detect_install_source(
                        self.module,
                        distribution=FakeDistribution(direct_url),
                        candidate_marker_paths=[],
                    ),
                    expected,
                )

    def test_missing_build_info_falls_back_safely_for_source(self) -> None:
        checkout = self.root / "source"
        module = checkout / "src" / "agent_bridge_connect" / "__init__.py"
        module.parent.mkdir(parents=True)
        module.write_text("", encoding="utf-8")
        (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (checkout / ".git").mkdir()

        report = self._report(
            module_path=module,
            distribution=None,
            build_info_path=module.with_name("_build_info.json"),
            runner_health=lambda: self._runner_health(module_path=module),
        )

        identity_check = self._check(report, "package.build_identity")
        self.assertEqual(report["package"]["build_source"], "source_checkout")
        self.assertEqual(report["package"]["install_source"], "source_checkout")
        self.assertEqual(identity_check["status"], "warning")
        self.assertTrue(report["ok"])

    def test_unavailable_runner_is_warning_and_exit_zero(self) -> None:
        secret = "runner-secret-that-must-not-leak"

        def unavailable() -> dict:
            raise RunnerError(f"runner token unavailable: {secret}")

        report = self._report(runner_health=unavailable)

        self.assertEqual(report["runner"]["status"], "unavailable")
        self.assertEqual(
            self._check(report, "runner.availability")["status"], "warning"
        )
        self.assertTrue(report["ok"])
        self.assertNotIn(secret, json.dumps(report))

    def test_interpreter_and_module_drift_are_errors(self) -> None:
        cases = (
            (self.root / "other-python", self.module, "Python interpreter"),
            (self.python, self.root / "other-module.py", "AgentBC module"),
        )
        for python_path, module_path, label in cases:
            with self.subTest(label=label):
                report = self._report(
                    runner_health=lambda python_path=python_path, module_path=module_path: (
                        self._runner_health(
                            python_executable=python_path,
                            module_path=module_path,
                        )
                    )
                )
                identity = self._check(report, "runner.identity")
                self.assertEqual(identity["status"], "error")
                self.assertIn(label, identity["message"])
                self.assertFalse(report["ok"])
                self.assertEqual(report["status"], "error")

    def test_json_contract_has_stable_shape(self) -> None:
        report = self._report()

        self.assertEqual(
            set(report),
            {"schema_version", "ok", "status", "package", "config", "runner", "checks"},
        )
        self.assertEqual(
            set(report["package"]),
            {
                "version",
                "commit_sha",
                "source_tree_sha256",
                "build_source",
                "module_path",
                "executable_path",
                "install_source",
            },
        )
        self.assertEqual(set(report["config"]), {"path", "exists", "workspace_root"})
        self.assertEqual(
            set(report["runner"]),
            {"status", "pid", "python_executable", "module_path", "executors"},
        )
        self.assertTrue(report["checks"])
        for check in report["checks"]:
            self.assertEqual(set(check), {"id", "status", "message"})

    def test_text_and_json_commands_use_identical_data(self) -> None:
        report = self._report()
        with mock.patch(
            "agent_bridge_connect.doctor.build_doctor_report",
            return_value=report,
        ):
            json_output = io.StringIO()
            with contextlib.redirect_stdout(json_output):
                json_exit = main(["doctor", "--json"])
            text_output = io.StringIO()
            with contextlib.redirect_stdout(text_output):
                text_exit = main(["doctor"])

        self.assertEqual(json.loads(json_output.getvalue()), report)
        self.assertEqual(
            text_output.getvalue().rstrip("\n"), render_doctor_text(report)
        )
        self.assertEqual(json_exit, 0)
        self.assertEqual(text_exit, 0)

    def test_command_exit_codes_follow_healthy_warning_error_contract(self) -> None:
        reports = (
            (self._report(), 0),
            (
                self._report(
                    runner_health=lambda: (_ for _ in ()).throw(RunnerError("offline"))
                ),
                0,
            ),
            (
                self._report(
                    runner_health=lambda: self._runner_health(
                        python_executable=self.root / "drifted-python"
                    )
                ),
                1,
            ),
        )
        for report, expected_exit in reports:
            with (
                self.subTest(status=report["status"]),
                mock.patch(
                    "agent_bridge_connect.doctor.build_doctor_report",
                    return_value=report,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(["doctor", "--json"]), expected_exit)

    def test_secret_values_are_never_rendered(self) -> None:
        secret = "super-secret-direct-url-credential"
        self.config.write_text(
            f"workspace_root = {json.dumps(str(self.workspace))}\n"
            f"api_token = {json.dumps(secret)}\n",
            encoding="utf-8",
        )
        distribution = FakeDistribution(
            {"url": f"https://user:{secret}@example.invalid/agentbc.whl"}
        )

        report = self._report(distribution=distribution)
        rendered = json.dumps(report) + render_doctor_text(report)

        self.assertEqual(report["package"]["install_source"], "direct_url")
        self.assertNotIn(secret, rendered)

    def test_invalid_config_is_blocking_without_echoing_contents(self) -> None:
        secret = "invalid-config-secret"
        self.config.write_text(
            f"api_token = {secret!r}\ninvalid line\n", encoding="utf-8"
        )

        report = self._report()

        self.assertEqual(self._check(report, "config.load")["status"], "error")
        self.assertFalse(report["ok"])
        self.assertNotIn(secret, json.dumps(report))

    @staticmethod
    def _check(report: dict, check_id: str) -> dict:
        return next(check for check in report["checks"] if check["id"] == check_id)


class RunnerHealthIdentityTests(unittest.TestCase):
    def test_health_reports_actual_python_and_agentbc_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = RunnerState(
                Path(temporary),
                [Path(temporary)],
                {"codex": Path("/bin/echo")},
            )

            health = _dispatch_request(state, {"op": "health"})

        self.assertTrue(Path(health["python_executable"]).is_absolute())
        self.assertEqual(Path(health["module_path"]).name, "__init__.py")
        self.assertIn("agent_bridge_connect", Path(health["module_path"]).parts)


if __name__ == "__main__":
    unittest.main()
