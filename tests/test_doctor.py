"""Tests for the read-only AgentBC doctor v2 contract."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect import __version__
from agent_bridge_connect.cli import main
from agent_bridge_connect.doctor import (
    SCHEMA_VERSION,
    build_doctor_report,
    detect_install_source,
    render_doctor_text,
)
from agent_bridge_connect.runner import RunnerError, RunnerState, _dispatch_request
from agent_bridge_connect.skill_packages import (
    LEGACY_SKILL_FINGERPRINTS,
    build_skill_manifest,
    serialize_skill_manifest,
    sha256_bytes,
)

T0 = "2026-08-11T00:00:00Z"
_TOP_LEVEL_KEYS = {
    "schema_version",
    "status",
    "exit_code",
    "package",
    "config",
    "runner",
    "storage",
    "skills",
    "executors",
    "session_cleanup",
    "blockers",
    "checks",
}


class FakeDistribution:
    def __init__(self, direct_url: dict | None = None) -> None:
        self.direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json" or self.direct_url is None:
            return None
        return json.dumps(self.direct_url)


def _receipt(
    state: str,
    *,
    capability: str = "supported",
    attempts: int = 1,
    error_code: str = "",
    retryable: bool = False,
    last_attempt_at: str = T0,
) -> dict:
    strategy = "official_session_delete"
    completed_at = ""
    next_attempt_at = ""
    if state == "retained":
        capability = "not_applicable"
        strategy = "retain"
        attempts = 0
        completed_at = T0
    elif state == "succeeded":
        completed_at = T0
    elif state == "unsupported":
        capability = "unsupported"
        strategy = "none"
        completed_at = T0
        error_code = error_code or "session_cleanup_unsupported"
    elif state == "failed":
        error_code = error_code or "session_cleanup_failed"
        next_attempt_at = "2026-08-11T00:10:00Z" if retryable else ""
    return {
        "version": 1,
        "capability": capability,
        "strategy": strategy,
        "state": state,
        "attempts": attempts,
        "requested_at": T0 if attempts else "",
        "last_attempt_at": last_attempt_at if attempts else "",
        "next_attempt_at": next_attempt_at,
        "completed_at": completed_at,
        "error_code": error_code,
        "retryable": retryable,
    }


def _cleanup_task(task_id: str, executor: str, receipt: dict) -> dict:
    from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY

    return {
        "id": task_id,
        "extensions": {
            SESSION_EXTENSION_KEY: {
                "executor": executor,
                "session_id": "private-session-id",
                "project_path": "/private/customer/session",
                "raw_output": "secret executor output",
                "native_argv": ["delete", "--force"],
                "cleanup": receipt,
            }
        },
    }


def _install_current_skill(root: Path, platform: str, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = build_skill_manifest(platform, __version__, files)
    (root / ".agentbc-skill.json").write_bytes(serialize_skill_manifest(manifest))


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


def _healthy_runner_health() -> dict:
    return {
        "ok": True,
        "status": "ready",
        "pid": 1234,
        "python_executable": "/opt/abc/bin/python",
        "module_path": "/opt/abc/agent_bridge_connect/__init__.py",
        "executors": ["hermes", "codex"],
    }


def _write_board_task(
    board_root: Path,
    task_id: str,
    *,
    status: str,
    assignee: str = "codex",
    extensions: dict | None = None,
) -> None:
    task_dir = board_root / "chain" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": task_id,
        "status": status,
        "assignee": assignee,
        "updated_at": "2026-08-11T00:00:00Z",
        "extensions": extensions or {},
    }
    (task_dir / "task.json").write_text(json.dumps(data), encoding="utf-8")


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.report_root = self.workspace / "tasks" / "report"
        self.report_root.mkdir(parents=True)
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
        self.skill_roots: dict[str, Path] = {}
        self.skill_current_files: dict[str, dict[str, bytes]] = {}
        for platform in ("codex", "claude", "hermes"):
            root = self.root / f"skills-{platform}"
            root.mkdir(parents=True)
            self.skill_roots[platform] = root
            self.skill_current_files[platform] = self._current_skill_files(platform)
            _install_current_skill(root, platform, self.skill_current_files[platform])

    @staticmethod
    def _current_skill_files(platform: str) -> dict[str, bytes]:
        from agent_bridge_connect.setup import _current_skill_files

        return _current_skill_files(platform)

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
            "board_root": self.record,
            "skill_roots": self.skill_roots,
            "skill_current_files": self.skill_current_files,
            "executor_probe": _healthy_executor_probe,
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
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["exit_code"], 1)

    def test_healthy_baseline_contract(self) -> None:
        report = self._report()

        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["exit_code"], 0)
        for check in report["checks"]:
            self.assertIn(check["status"], {"healthy", "warning", "unavailable"})

    def test_unavailable_runner_is_core_unavailable_and_exit_two(self) -> None:
        secret = "runner-secret-that-must-not-leak"

        def unavailable() -> dict:
            raise RunnerError(f"runner token unavailable: {secret}")

        report = self._report(runner_health=unavailable)

        self.assertEqual(report["runner"]["status"], "unavailable")
        self.assertEqual(
            self._check(report, "runner.availability")["status"], "unavailable"
        )
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["exit_code"], 2)
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, render_doctor_text(report))

    def test_interpreter_and_module_drift_are_core_unavailable(self) -> None:
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
                self.assertEqual(identity["status"], "unavailable")
                self.assertIn(label, identity["message"])
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["exit_code"], 2)

    def test_json_contract_has_stable_shape(self) -> None:
        report = self._report()

        self.assertEqual(set(report), _TOP_LEVEL_KEYS)
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
                "status",
                "reason",
                "remediation",
            },
        )
        self.assertEqual(
            set(report["config"]),
            {
                "path",
                "exists",
                "workspace_root",
                "board_root",
                "status",
                "reason",
                "remediation",
            },
        )
        self.assertEqual(
            set(report["runner"]),
            {
                "status",
                "pid",
                "python_executable",
                "module_path",
                "executors",
                "identity",
                "token_file",
                "spool",
                "reason",
                "remediation",
            },
        )
        self.assertEqual(
            set(report["storage"]),
            {"workspace", "report", "record", "status", "reason", "remediation"},
        )
        for name in ("workspace", "report", "record"):
            self.assertEqual(
                set(report["storage"][name]),
                {
                    "path",
                    "exists",
                    "is_dir",
                    "readable",
                    "writable",
                    "status",
                    "reason",
                    "remediation",
                },
            )
        self.assertEqual(
            set(report["skills"]),
            {"codex", "claude", "hermes", "status", "warnings"},
        )
        for platform in ("codex", "claude", "hermes"):
            self.assertEqual(
                set(report["skills"][platform]),
                {
                    "platform",
                    "root",
                    "classification",
                    "installed",
                    "up_to_date",
                    "package_version",
                    "protocol_version",
                    "completion_version",
                    "template_sha256",
                    "status",
                    "reason",
                    "remediation",
                },
            )
        self.assertEqual(
            set(report["executors"]),
            {"codex", "claude", "hermes", "status", "warnings"},
        )
        for platform in ("codex", "claude", "hermes"):
            self.assertEqual(
                set(report["executors"][platform]),
                {
                    "platform",
                    "configured",
                    "command",
                    "resolved",
                    "source",
                    "version",
                    "probe",
                    "auth",
                    "capability",
                    "status",
                    "reason",
                    "remediation",
                },
            )
        self.assertEqual(
            set(report["session_cleanup"]),
            {"status", "warnings", "diagnostics"},
        )
        self.assertEqual(
            set(report["blockers"]),
            {"status", "count", "items"},
        )
        self.assertTrue(report["checks"])
        for check in report["checks"]:
            self.assertEqual(set(check), {"id", "status", "message"})
            self.assertIn(check["status"], {"healthy", "warning", "unavailable"})

    def test_checks_are_stable_sorted_by_id(self) -> None:
        report = self._report()
        ids = [check["id"] for check in report["checks"]]

        self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            list(report["skills"]),
            ["codex", "claude", "hermes", "status", "warnings"],
        )
        self.assertEqual(
            list(report["executors"]),
            ["codex", "claude", "hermes", "status", "warnings"],
        )

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

    def test_cleanup_warning_text_and_json_commands_use_identical_data(self) -> None:
        report = self._report(
            cleanup_tasks=[
                _cleanup_task("CLEAN-001", "codex", _receipt("unsupported"))
            ],
            now="2026-08-11T00:10:00Z",
        )
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
        self.assertEqual(text_output.getvalue().rstrip(), render_doctor_text(report))
        self.assertEqual(report["session_cleanup"]["warnings"], 1)
        self.assertEqual(report["status"], "warning")
        self.assertEqual(json_exit, 1)
        self.assertEqual(text_exit, 1)

    def test_command_exit_codes_follow_healthy_warning_unavailable_contract(self) -> None:
        warning_report = self._report(
            skill_roots={"codex": self.root / "empty-skill"},
            skill_current_files={
                platform: self.skill_current_files[platform]
                for platform in ("codex", "claude", "hermes")
            },
        )
        unavailable_report = self._report(
            runner_health=lambda: (_ for _ in ()).throw(RunnerError("offline"))
        )
        reports = (
            (self._report(), 0),
            (warning_report, 1),
            (unavailable_report, 2),
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

    def test_invalid_config_is_core_unavailable_without_echoing_contents(self) -> None:
        secret = "invalid-config-secret"
        self.config.write_text(
            f"api_token = {secret!r}\ninvalid line\n", encoding="utf-8"
        )

        report = self._report()

        self.assertEqual(self._check(report, "config.load")["status"], "unavailable")
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["exit_code"], 2)
        self.assertNotIn(secret, json.dumps(report))

    def test_malformed_build_identity_is_core_unavailable(self) -> None:
        self.build_info.write_text("{not json", encoding="utf-8")

        report = self._report()

        self.assertEqual(
            self._check(report, "package.build_identity")["status"], "unavailable"
        )
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["exit_code"], 2)

    def test_remediation_strings_are_stable(self) -> None:
        empty = self.root / "empty-skill"
        empty.mkdir()
        report = self._report(
            skill_roots={
                "codex": empty,
                "claude": self.skill_roots["claude"],
                "hermes": self.skill_roots["hermes"],
            },
            skill_current_files=self.skill_current_files,
        )

        skill = report["skills"]["codex"]
        self.assertEqual(skill["classification"], "missing")
        self.assertIn("agentbc setup --update", skill["remediation"])
        unavailable = self._report(
            runner_health=lambda: (_ for _ in ()).throw(RunnerError("offline"))
        )
        self.assertIn(
            "agentbc runner start", unavailable["runner"]["remediation"]
        )

    @staticmethod
    def _check(report: dict, check_id: str) -> dict:
        return next(check for check in report["checks"] if check["id"] == check_id)


class DoctorCollectorIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module = self.root / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.report_root = self.workspace / "tasks" / "report"
        self.report_root.mkdir(parents=True)
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
        self.skill_roots = {}
        self.skill_current_files = {}
        for platform in ("codex", "claude", "hermes"):
            root = self.root / f"skills-{platform}"
            root.mkdir()
            self.skill_roots[platform] = root
            files = DoctorTests._current_skill_files(platform)
            self.skill_current_files[platform] = files
            _install_current_skill(root, platform, files)

    def _base(self, **overrides) -> dict:
        self.config.write_text(
            f"workspace_root = {json.dumps(str(self.workspace))}\n"
            "[executors.codex]\n"
            "command = '/usr/bin/env codex'\n"
            "[executors.claude]\n"
            "command = '/opt/claude-code/bin/claude'\n"
            "[executors.hermes]\n"
            "command = '/opt/hermes/bin/hermes'\n",
            encoding="utf-8",
        )
        arguments = {
            "config_path": self.config,
            "runner_health": lambda: {
                "ok": True,
                "status": "ready",
                "pid": 1,
                "python_executable": self.python(),
                "module_path": str(self.module),
                "executors": ["codex"],
            },
            "module_path": self.module,
            "executable_path": self.root / "agentbc",
            "python_executable": self.python(),
            "distribution": FakeDistribution(),
            "candidate_marker_paths": [],
            "build_info_path": self.build_info,
            "board_root": self.record,
            "skill_roots": self.skill_roots,
            "skill_current_files": self.skill_current_files,
            "executor_probe": _healthy_executor_probe,
        }
        arguments.update(overrides)
        return build_doctor_report(**arguments)

    def python(self) -> str:
        return str(self.root / "python")

    def test_collector_exception_isolation_does_not_crash_doctor(self) -> None:
        secret = "probe-secret-not-leaked"

        def broken_probe(platform: str) -> dict:
            raise RuntimeError(f"{platform} probe exploded: {secret}")

        def broken_health() -> dict:
            raise ValueError(f"health exploded: {secret}")

        report = self._base(
            executor_probe=broken_probe,
            runner_health=broken_health,
        )

        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(
            self._check(report, "runner.availability")["status"], "unavailable"
        )
        for platform in ("codex", "claude", "hermes"):
            entry = report["executors"][platform]
            self.assertEqual(entry["status"], "warning")
            self.assertEqual(entry["source"], "unavailable")
            self.assertEqual(entry["probe"], "unavailable")
        rendered = json.dumps(report) + render_doctor_text(report)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("exploded", rendered)

    def test_missing_and_unreadable_record_root(self) -> None:
        missing = self.root / "missing-record"
        missing_report = self._base(board_root=missing)
        self.assertEqual(missing_report["storage"]["record"]["exists"], False)
        self.assertEqual(
            self._check(missing_report, "storage.record")["status"], "warning"
        )

        unreadable = self.root / "unreadable-record"
        unreadable.mkdir()
        unreadable.chmod(0o000)
        try:
            unreadable_report = self._base(board_root=unreadable)
        finally:
            unreadable.chmod(0o700)
        self.assertEqual(unreadable_report["storage"]["record"]["readable"], False)
        self.assertEqual(
            self._check(unreadable_report, "storage.record")["status"], "unavailable"
        )
        self.assertEqual(unreadable_report["status"], "unavailable")
        self.assertEqual(unreadable_report["exit_code"], 2)

    def test_workspace_report_record_write_permission_failures_are_unavailable(
        self,
    ) -> None:
        real_access = os.access

        def deny_write(path: str, mode: int) -> bool:
            if mode == os.W_OK:
                target = Path(path).expanduser().resolve()
                if target == self.workspace.resolve():
                    return False
            return real_access(path, mode)

        with mock.patch("agent_bridge_connect.doctor.os.access", side_effect=deny_write):
            report = self._base()

        self.assertEqual(report["storage"]["workspace"]["writable"], False)
        self.assertEqual(
            self._check(report, "storage.workspace")["status"], "unavailable"
        )
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["exit_code"], 2)

    def test_runner_token_metadata_and_spool_are_reported_without_content(self) -> None:
        spool = self.root / "spool"
        token = spool / "token"
        token.parent.mkdir(parents=True)
        token_value = "super-secret-runner-token-value"
        token.write_text(token_value, encoding="utf-8")
        (spool / "requests").mkdir()
        (spool / "runner.pid").write_text("9999", encoding="utf-8")

        report = self._base(
            runner_spool_root=spool,
            runner_token_path=token,
        )

        token_file = report["runner"]["token_file"]
        self.assertEqual(token_file["exists"], True)
        self.assertEqual(token_file["is_file"], True)
        self.assertEqual(token_file["readable"], True)
        self.assertEqual(token_file["bytes"], len(token_value))
        spool_status = report["runner"]["spool"]
        self.assertEqual(spool_status["requests_exists"], True)
        self.assertEqual(spool_status["pid_file_exists"], True)
        rendered = json.dumps(report) + render_doctor_text(report)
        self.assertNotIn(token_value, rendered)

    @staticmethod
    def _check(report: dict, check_id: str) -> dict:
        return next(check for check in report["checks"] if check["id"] == check_id)


class DoctorSkillMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module = self.root / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.report_root = self.workspace / "tasks" / "report"
        self.report_root.mkdir(parents=True)
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
        self.files = {
            platform: DoctorTests._current_skill_files(platform)
            for platform in ("codex", "claude", "hermes")
        }

    def _report_for(self, codex_root: Path) -> dict:
        return build_doctor_report(
            config_path=self.config,
            runner_health=lambda: {
                "ok": True,
                "status": "ready",
                "pid": 1,
                "python_executable": str(self.module.parent / "python"),
                "module_path": str(self.module),
                "executors": ["codex"],
            },
            module_path=self.module,
            executable_path=self.root / "agentbc",
            python_executable=self.module.parent / "python",
            distribution=FakeDistribution(),
            candidate_marker_paths=[],
            build_info_path=self.build_info,
            board_root=self.record,
            skill_roots={
                "codex": codex_root,
                "claude": self._current_root("claude"),
                "hermes": self._current_root("hermes"),
            },
            skill_current_files=self.files,
            executor_probe=_healthy_executor_probe,
        )

    def _current_root(self, platform: str) -> Path:
        root = self.root / f"current-{platform}"
        if not (root / ".agentbc-skill.json").exists():
            _install_current_skill(root, platform, self.files[platform])
        return root

    def test_current_skill_is_healthy(self) -> None:
        root = self.root / "current-codex"
        _install_current_skill(root, "codex", self.files["codex"])

        report = self._report_for(root)

        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "current")
        self.assertEqual(entry["status"], "healthy")
        self.assertEqual(entry["package_version"], __version__)
        self.assertEqual(entry["protocol_version"], "1.0")
        self.assertEqual(entry["completion_version"], 1)
        self.assertTrue(entry["template_sha256"])
        self.assertEqual(report["status"], "healthy")

    def test_missing_skill_is_warning_with_update_remediation(self) -> None:
        root = self.root / "missing-codex"
        root.mkdir()

        report = self._report_for(root)

        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "missing")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("agentbc setup --update", entry["remediation"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["exit_code"], 1)

    def test_modified_skill_is_warning_without_touching_files(self) -> None:
        root = self.root / "modified-codex"
        _install_current_skill(root, "codex", self.files["codex"])
        marker = root / "SKILL.md"
        original = marker.read_bytes()
        marker.write_bytes(original + b"\n# user edit\n")

        report = self._report_for(root)

        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "modified")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("agentbc setup --update", entry["remediation"])
        self.assertEqual(marker.read_bytes(), original + b"\n# user edit\n")
        self.assertNotIn(b"user edit", (root / ".agentbc-skill.json").read_bytes())

    def test_partial_skill_is_warning(self) -> None:
        root = self.root / "partial-codex"
        _install_current_skill(root, "codex", self.files["codex"])
        (root / "references" / "agentbc-steps-yaml.md").unlink()

        report = self._report_for(root)

        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "partial")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("agentbc setup --update", entry["remediation"])

    def test_legacy_skill_is_warning(self) -> None:
        root = self.root / "legacy-codex"
        legacy_files = {"SKILL.md": b"legacy skill body", "agents/openai.yaml": b"legacy: true"}
        legacy = {
            "template_sha256": "0" * 64,
            "files": {path: sha256_bytes(content) for path, content in legacy_files.items()},
        }
        for path, content in legacy_files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        patched_fingerprints = {
            **LEGACY_SKILL_FINGERPRINTS,
            "codex": legacy,
        }
        with mock.patch(
            "agent_bridge_connect.skill_packages.LEGACY_SKILL_FINGERPRINTS",
            patched_fingerprints,
        ):
            report = self._report_for(root)

        entry = report["skills"]["codex"]
        self.assertEqual(entry["classification"], "legacy")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("agentbc setup --update", entry["remediation"])
        self.assertNotIn("legacy skill body", json.dumps(report))
        self.assertNotIn("legacy skill body", render_doctor_text(report))


class DoctorExecutorMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module = self.root / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.report_root = self.workspace / "tasks" / "report"
        self.report_root.mkdir(parents=True)
        self.record = self.root / "record"
        self.record.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(
            f"workspace_root = {json.dumps(str(self.workspace))}\n"
            "[executors.codex]\n"
            "command = '/usr/bin/env codex'\n"
            "[executors.claude]\n"
            "command = '/opt/claude-code/bin/claude'\n"
            "[executors.hermes]\n"
            "command = '/opt/hermes/bin/hermes'\n",
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
        self.skill_roots = {}
        self.skill_current_files = {}
        for platform in ("codex", "claude", "hermes"):
            root = self.root / f"skills-{platform}"
            root.mkdir()
            self.skill_roots[platform] = root
            files = DoctorTests._current_skill_files(platform)
            self.skill_current_files[platform] = files
            _install_current_skill(root, platform, files)

    def _report(self, probes: dict) -> dict:
        return build_doctor_report(
            config_path=self.config,
            runner_health=lambda: {
                "ok": True,
                "status": "ready",
                "pid": 1,
                "python_executable": str(self.module.parent / "python"),
                "module_path": str(self.module),
                "executors": ["codex", "claude", "hermes"],
            },
            module_path=self.module,
            executable_path=self.root / "agentbc",
            python_executable=self.module.parent / "python",
            distribution=FakeDistribution(),
            candidate_marker_paths=[],
            build_info_path=self.build_info,
            board_root=self.record,
            skill_roots=self.skill_roots,
            skill_current_files=self.skill_current_files,
            executor_probe=lambda platform: probes[platform],
        )

    def test_configured_healthy_executor_reports_stable_fake_outcomes(self) -> None:
        probes = {platform: _healthy_executor_probe(platform) for platform in ("codex", "claude", "hermes")}
        probes["codex"] = {
            "resolved": True,
            "source": "path",
            "version": "codex-0.146.0",
            "probe": "ok",
            "auth": {"key_env": "OPENAI_API_KEY", "configured": True, "present": False},
            "capability": {
                "level": 2,
                "structured_output": True,
                "resume": True,
                "cancel": False,
                "input_required": False,
            },
        }
        report = self._report(probes)

        entry = report["executors"]["codex"]
        self.assertEqual(entry["configured"], True)
        self.assertEqual(entry["command"], "/usr/bin/env codex")
        self.assertEqual(entry["resolved"], True)
        self.assertEqual(entry["source"], "path")
        self.assertEqual(entry["version"], "codex-0.146.0")
        self.assertEqual(entry["probe"], "ok")
        self.assertEqual(entry["auth"]["key_env"], "OPENAI_API_KEY")
        self.assertEqual(entry["auth"]["configured"], True)
        self.assertEqual(entry["capability"]["level"], 2)
        self.assertEqual(entry["status"], "healthy")
        self.assertEqual(report["status"], "healthy")

    def test_executor_probe_uses_strict_public_projection_and_redacts_extras(
        self,
    ) -> None:
        secret = "executor-probe-secret-that-must-not-leak"
        probes = {
            platform: _healthy_executor_probe(platform)
            for platform in ("codex", "claude", "hermes")
        }
        probes["codex"].update(
            {
                "raw_output": secret,
                "private_database_path": f"/private/{secret}/sessions.db",
                "native_argv": ["codex", "--token", secret],
            }
        )
        probes["codex"]["auth"].update({"token": secret, "raw_output": secret})
        probes["codex"]["capability"].update(
            {"raw_help": secret, "session_content": secret}
        )

        report = self._report(probes)
        entry = report["executors"]["codex"]
        rendered = json.dumps(report) + render_doctor_text(report)

        self.assertEqual(
            set(entry["auth"]), {"key_env", "configured", "present"}
        )
        self.assertEqual(
            set(entry["capability"]),
            {"level", "structured_output", "resume", "cancel", "input_required"},
        )
        self.assertNotIn(secret, rendered)

    def test_executor_probe_rejects_non_scalar_version_and_unknown_labels(
        self,
    ) -> None:
        probes = {
            platform: _healthy_executor_probe(platform)
            for platform in ("codex", "claude", "hermes")
        }
        probes["codex"].update(
            {
                "source": "raw-private-source",
                "version": {"raw_output": "private-version-output"},
                "probe": "raw-private-state",
            }
        )

        report = self._report(probes)
        entry = report["executors"]["codex"]

        self.assertEqual(entry["source"], "unavailable")
        self.assertIsNone(entry["version"])
        self.assertEqual(entry["probe"], "unavailable")
        self.assertEqual(entry["status"], "warning")
        rendered = json.dumps(report)
        self.assertNotIn("raw-private-source", rendered)
        self.assertNotIn("private-version-output", rendered)
        self.assertNotIn("raw-private-state", rendered)

    def test_configured_missing_command_is_warning_not_crash(self) -> None:
        probes = {platform: _healthy_executor_probe(platform) for platform in ("codex", "claude", "hermes")}
        probes["codex"] = {
            "resolved": False,
            "source": "not_found",
            "version": None,
            "probe": "skipped",
            "auth": {"key_env": "OPENAI_API_KEY", "configured": True, "present": False},
            "capability": {
                "level": 0,
                "structured_output": False,
                "resume": False,
                "cancel": False,
                "input_required": False,
            },
        }
        report = self._report(probes)

        entry = report["executors"]["codex"]
        self.assertEqual(entry["configured"], True)
        self.assertEqual(entry["resolved"], False)
        self.assertEqual(entry["probe"], "skipped")
        self.assertEqual(entry["status"], "warning")
        self.assertIn("agentbc setup", entry["remediation"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["exit_code"], 1)

    def test_configured_probe_failure_is_warning(self) -> None:
        probes = {platform: _healthy_executor_probe(platform) for platform in ("codex", "claude", "hermes")}
        probes["codex"] = {
            "resolved": True,
            "source": "path",
            "version": None,
            "probe": "failed",
            "auth": {"key_env": "", "configured": False, "present": False},
            "capability": {
                "level": 0,
                "structured_output": False,
                "resume": False,
                "cancel": False,
                "input_required": False,
            },
        }
        report = self._report(probes)

        entry = report["executors"]["codex"]
        self.assertEqual(entry["status"], "warning")
        self.assertIn("probe failed", entry["reason"])
        self.assertEqual(report["status"], "warning")

    def test_unconfigured_executor_is_healthy(self) -> None:
        config = self.root / "config.toml"
        config.write_text(
            f"workspace_root = {json.dumps(str(self.workspace))}\n",
            encoding="utf-8",
        )
        report = build_doctor_report(
            config_path=config,
            runner_health=lambda: {
                "ok": True,
                "status": "ready",
                "pid": 1,
                "python_executable": str(self.module.parent / "python"),
                "module_path": str(self.module),
                "executors": [],
            },
            module_path=self.module,
            executable_path=self.root / "agentbc",
            python_executable=self.module.parent / "python",
            distribution=FakeDistribution(),
            candidate_marker_paths=[],
            build_info_path=self.build_info,
            board_root=self.record,
            skill_roots=self.skill_roots,
            skill_current_files=self.skill_current_files,
            executor_probe=_healthy_executor_probe,
        )

        for platform in ("codex", "claude", "hermes"):
            entry = report["executors"][platform]
            self.assertEqual(entry["configured"], False)
            self.assertEqual(entry["status"], "healthy")
            self.assertIn("not configured", entry["reason"])


class DoctorBlockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module = self.root / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.report_root = self.workspace / "tasks" / "report"
        self.report_root.mkdir(parents=True)
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
        self.skill_roots = {}
        self.skill_current_files = {}
        for platform in ("codex", "claude", "hermes"):
            root = self.root / f"skills-{platform}"
            root.mkdir()
            self.skill_roots[platform] = root
            files = DoctorTests._current_skill_files(platform)
            self.skill_current_files[platform] = files
            _install_current_skill(root, platform, files)

    def _report(self, **overrides) -> dict:
        arguments = {
            "config_path": self.config,
            "runner_health": lambda: {
                "ok": True,
                "status": "ready",
                "pid": 1,
                "python_executable": str(self.module.parent / "python"),
                "module_path": str(self.module),
                "executors": ["codex"],
            },
            "module_path": self.module,
            "executable_path": self.root / "agentbc",
            "python_executable": self.module.parent / "python",
            "distribution": FakeDistribution(),
            "candidate_marker_paths": [],
            "build_info_path": self.build_info,
            "board_root": self.record,
            "skill_roots": self.skill_roots,
            "skill_current_files": self.skill_current_files,
            "executor_probe": _healthy_executor_probe,
        }
        arguments.update(overrides)
        return build_doctor_report(**arguments)

    def test_active_input_resource_permission_and_needs_recovery_blockers(self) -> None:
        from agent_bridge_connect.permission_grants import (
            PERMISSION_GRANT_EXTENSION_KEY,
            build_permission_grant,
        )

        _write_board_task(
            self.record,
            "INPUT-001",
            status="input_required",
            assignee="claude",
            extensions={
                "agentbc.input": {
                    "input_id": "input-1",
                    "type": "choice",
                    "kind": "user_input",
                    "status": "waiting",
                    "summary": "sensitive prompt summary",
                    "reason": "sensitive reason",
                }
            },
        )
        _write_board_task(
            self.record,
            "RES-001",
            status="input_required",
            assignee="claude",
            extensions={
                "agentbc.input": {
                    "input_id": "input-2",
                    "type": "choice",
                    "kind": "resource_limit",
                    "response_protocol": "approve_deny",
                    "executor": "claude",
                    "resource": "max_budget_usd",
                    "current_limit": 10.0,
                    "next_limit": 20.0,
                    "status": "waiting",
                }
            },
        )
        grant = build_permission_grant(
            executor="codex",
            task_id="PERM-001",
            input_id="input-3",
            session_id="sess-3",
            source_run_id="run-3",
        )
        _write_board_task(
            self.record,
            "PERM-001",
            status="running",
            extensions={PERMISSION_GRANT_EXTENSION_KEY: grant},
        )
        _write_board_task(
            self.record,
            "RECV-001",
            status="needs_recovery",
            assignee="hermes",
        )

        report = self._report()

        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["blockers"]["count"], 4)
        kinds = {(item["type"], item["task_id"]) for item in report["blockers"]["items"]}
        self.assertEqual(
            kinds,
            {
                ("input", "INPUT-001"),
                ("resource", "RES-001"),
                ("permission", "PERM-001"),
                ("needs_recovery", "RECV-001"),
            },
        )
        rendered = json.dumps(report) + render_doctor_text(report)
        self.assertNotIn("sensitive prompt summary", rendered)
        self.assertNotIn("sensitive reason", rendered)
        self.assertNotIn("sess-3", rendered)
        self.assertNotIn("run-3", rendered)
        self.assertNotIn("input-3", rendered)

    def test_cleanup_unsupported_failed_and_stale_pending_are_blockers(self) -> None:
        report = self._report(
            cleanup_tasks=[
                _cleanup_task("CLEAN-001", "codex", _receipt("unsupported")),
                _cleanup_task(
                    "CLEAN-002",
                    "hermes",
                    _receipt(
                        "failed",
                        error_code="session_cleanup_failed",
                        retryable=True,
                    ),
                ),
                _cleanup_task("CLEAN-003", "claude", _receipt("pending")),
            ],
            now="2026-08-11T00:10:00Z",
        )

        self.assertEqual(report["status"], "warning")
        cleanup_types = [
            item["type"]
            for item in report["blockers"]["items"]
            if item["type"] == "cleanup"
        ]
        self.assertEqual(len(cleanup_types), 3)
        states = {
            item["state"]
            for item in report["blockers"]["items"]
            if item["type"] == "cleanup"
        }
        self.assertEqual(states, {"unsupported", "failed", "pending"})
        rendered = json.dumps(report) + render_doctor_text(report)
        self.assertNotIn("secret executor output", rendered)
        self.assertNotIn("/private/customer/session", rendered)
        self.assertNotIn("private-session-id", rendered)
        self.assertNotIn("--force", rendered)

    def test_retained_and_succeeded_cleanup_are_healthy(self) -> None:
        report = self._report(
            cleanup_tasks=[
                _cleanup_task("CLEAN-004", "claude", _receipt("retained")),
                _cleanup_task("CLEAN-005", "codex", _receipt("succeeded")),
            ],
            now="2026-08-11T00:04:59Z",
        )

        self.assertEqual(report["session_cleanup"]["warnings"], 0)
        self.assertEqual(report["blockers"]["count"], 0)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["exit_code"], 0)

    def test_blocker_items_are_sorted_and_sanitized(self) -> None:
        input_extension = {
            "input_id": "input-x",
            "type": "choice",
            "kind": "user_input",
            "status": "waiting",
        }
        _write_board_task(
            self.record,
            "Z-INPUT",
            status="input_required",
            extensions={"agentbc.input": input_extension},
        )
        _write_board_task(
            self.record,
            "A-INPUT",
            status="input_required",
            extensions={"agentbc.input": input_extension},
        )

        report = self._report()

        items = report["blockers"]["items"]
        self.assertEqual([item["task_id"] for item in items], ["A-INPUT", "Z-INPUT"])
        for item in items:
            self.assertEqual(set(item), {"task_id", "type", "executor", "kind", "state"})


class RunnerHealthIdentityTests(unittest.TestCase):
    def test_health_reports_actual_python_and_agentbc_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = RunnerState(
                Path(temporary),
                [Path(temporary)],
                {"codex": Path(sys.executable)},
            )

            health = _dispatch_request(state, {"op": "health"})

        self.assertTrue(Path(health["python_executable"]).is_absolute())
        self.assertEqual(Path(health["module_path"]).name, "__init__.py")
        self.assertIn("agent_bridge_connect", Path(health["module_path"]).parts)


if __name__ == "__main__":
    unittest.main()
