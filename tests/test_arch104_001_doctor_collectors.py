"""ARCH-104-001 Slice C: focused Doctor collector and compatibility tests.

Covers the doctor.py / doctor_collectors.py responsibility split:

* collectors are the single read-only owners (config, package, Runner,
  storage, skills, executors, permission runtime, cleanup, blockers);
* doctor.py keeps ordering, aggregation, overall status, and rendering;
* compatibility aliases: every name importable from
  ``agent_bridge_connect.doctor`` before the split still resolves to the
  same object (the concrete owner implementation, not a wrapper);
* collectors perform zero writes / zero network access;
* deterministic collector ordering and the stable schema contract.

Healthy / warning / unavailable / partial-exception paths are exercised
through the public ``build_doctor_report`` entry point.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import agent_bridge_connect.doctor as doctor_module
import agent_bridge_connect.doctor_collectors as collectors_module
from agent_bridge_connect import __version__
from agent_bridge_connect.doctor import (
    SCHEMA_VERSION,
    build_doctor_report,
    build_session_cleanup_diagnostics,
    collect_session_cleanup_diagnostics,
    detect_install_source,
    render_doctor_text,
)
from agent_bridge_connect.runner import RunnerError
from agent_bridge_connect.setup import _current_skill_files
from tests.test_doctor import (
    T0,
    FakeDistribution,
    _cleanup_task,
    _healthy_executor_probe,
    _install_current_skill,
    _receipt,
    _write_board_task,
)

_ALIAS_NAMES = (
    "BUILD_INFO_SCHEMA_VERSION",
    "_BLOCKER_TASK_STATUSES",
    "_EXECUTOR_PLATFORMS",
    "_apply_storage_severity",
    "_auxiliary_cleanup_diagnostics",
    "_claude_sdk_capability_projection",
    "_cli_executable_path",
    "_collect_blockers",
    "_collect_claude_sdk_capability",
    "_collect_config",
    "_collect_executor_entry",
    "_collect_executors",
    "_collect_package",
    "_collect_permission_runtime",
    "_collect_runner",
    "_collect_skill_entry",
    "_collect_skills",
    "_collect_storage",
    "_default_auth",
    "_default_candidate_marker_paths",
    "_default_capability",
    "_default_executor_probe",
    "_default_runner_spool",
    "_default_skill_current_files",
    "_default_skill_roots",
    "_doctor_board_root",
    "_find_source_checkout",
    "_git_commit_sha",
    "_installed_distribution",
    "_one_auxiliary_diagnostic",
    "_package_module_path",
    "_parse_timestamp",
    "_path_permissions",
    "_pending_is_stale",
    "_public_executor_probe",
    "_public_identity_path",
    "_read_build_info",
    "_read_direct_url",
    "_read_task_records",
    "_resolved_path",
    "_runner_storage_permissions",
    "_safe_label",
    "_source_tree_sha256",
    "_spool_status",
    "_token_file_metadata",
    "_unverified_storage_permissions",
    "_write_capable",
)


class DoctorCollectorCompatibilityTests(unittest.TestCase):
    """Public names on doctor.py remain importable and alias the owner."""

    def test_public_imports_resolve_to_owner_implementations(self) -> None:
        for name in ("build_session_cleanup_diagnostics", "detect_install_source"):
            self.assertIs(getattr(doctor_module, name), getattr(collectors_module, name))
        for name in _ALIAS_NAMES:
            self.assertIs(
                getattr(doctor_module, name),
                getattr(collectors_module, name),
                f"doctor.{name} is not the concrete owner implementation",
            )
        self.assertIs(
            doctor_module.collect_session_cleanup_diagnostics,
            collectors_module.collect_session_cleanup_diagnostics,
        )

    def test_doctor_module_defines_no_collector_wrappers(self) -> None:
        # The compatibility layer must stay alias-only: no wrapper function
        # may shadow an owner implementation in the doctor module namespace.
        import types

        for name in _ALIAS_NAMES:
            value = getattr(doctor_module, name, None)
            if isinstance(value, types.FunctionType):
                self.assertIs(
                    value,
                    getattr(collectors_module, name),
                    f"doctor.{name} defines its own body instead of aliasing",
                )

    def test_two_responsibility_layers(self) -> None:
        # Aggregation/render stay in doctor.py; every collector lives in the
        # read-only owner module.
        self.assertEqual(
            doctor_module.build_doctor_report.__module__,
            "agent_bridge_connect.doctor",
        )
        self.assertEqual(
            doctor_module.render_doctor_text.__module__,
            "agent_bridge_connect.doctor",
        )
        self.assertEqual(
            collectors_module._collect_runner.__module__,
            "agent_bridge_connect.doctor_collectors",
        )
        self.assertEqual(
            collectors_module._collect_permission_runtime.__module__,
            "agent_bridge_connect.doctor_collectors",
        )

    def test_schema_constants_are_unchanged(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(
            doctor_module.EXIT_CODE_BY_STATUS,
            {"healthy": 0, "warning": 1, "unavailable": 2},
        )
        self.assertEqual(doctor_module.BUILD_INFO_SCHEMA_VERSION, 1)
        self.assertEqual(
            doctor_module._EXECUTOR_PLATFORMS, ("codex", "claude", "hermes")
        )


class DoctorCollectorBehaviorTests(unittest.TestCase):
    """Collector-level behavior: healthy, warning, unavailable, exceptions."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module = self.root / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "tasks" / "report").mkdir(parents=True)
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
            root.mkdir()
            self.skill_roots[platform] = root
            files = _current_skill_files(platform)
            self.skill_current_files[platform] = files
            _install_current_skill(root, platform, files)

    def python(self) -> str:
        return str(self.root / "python")

    def _base(self, **overrides) -> dict:
        self.config.write_text(
            f"workspace_root = {json.dumps(str(self.workspace))}\n"
            "[executors.codex]\n"
            "command = '/usr/bin/env codex'\n",
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

    @staticmethod
    def _check(report: dict, check_id: str) -> dict:
        return next(
            check for check in report["checks"] if check["id"] == check_id
        )

    # -- healthy / warning / unavailable / partial exception ---------------

    def test_healthy_path_through_collectors(self) -> None:
        report = self._base()

        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["runner"]["identity"], "match")
        for check_id in (
            "package.install_source",
            "config.load",
            "runner.availability",
            "runner.identity",
            "storage.workspace",
            "skills.codex",
            "executors.codex",
            "permission.runtime",
            "blockers.active",
        ):
            self.assertEqual(self._check(report, check_id)["status"], "healthy")

    def test_warning_path_keeps_exit_one(self) -> None:
        empty = self.root / "empty-skill"
        empty.mkdir()
        report = self._base(
            skill_roots={
                "codex": empty,
                "claude": self.skill_roots["claude"],
                "hermes": self.skill_roots["hermes"],
            },
        )

        self.assertEqual(report["skills"]["codex"]["status"], "warning")
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["exit_code"], 1)

    def test_unavailable_runner_is_reported_not_raised(self) -> None:
        def broken_health() -> dict:
            raise RunnerError("runner offline")

        report = self._base(runner_health=broken_health)

        self.assertEqual(report["runner"]["status"], "unavailable")
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["exit_code"], 2)
        self.assertNotIn("offline", render_doctor_text(report))

    def test_collector_exception_is_contained_by_safe_collect(self) -> None:
        def exploding_blockers(board_root, *, cleanup):  # noqa: ANN001
            raise RuntimeError("blocker collector exploded")

        report = self._base()
        # Direct containment proof at the collector boundary.
        section, checks = doctor_module._safe_collect(
            "blockers",
            lambda: exploding_blockers(self.record, cleanup={}),
        )
        self.assertEqual(section["status"], "warning")
        self.assertIn("contained", section["reason"])
        self.assertEqual(checks[0]["id"], "blockers.collector")
        # The full report path is unaffected by the injected failure shape.
        self.assertEqual(report["status"], "healthy")

    # -- Runner identity drift ---------------------------------------------

    def test_runner_identity_drift_is_unavailable(self) -> None:
        report = self._base(
            runner_health=lambda: {
                "ok": True,
                "status": "ready",
                "pid": 2,
                "python_executable": str(self.root / "other-python"),
                "module_path": str(self.module),
                "executors": ["codex"],
            },
        )

        identity = self._check(report, "runner.identity")
        self.assertEqual(identity["status"], "unavailable")
        self.assertIn("Python interpreter", identity["message"])
        self.assertEqual(report["exit_code"], 2)

    # -- storage -----------------------------------------------------------

    def test_runner_storage_authoritative_when_identity_matches(self) -> None:
        def runner_storage(paths: list[str]) -> dict:
            return {
                "ok": True,
                "status": "ready",
                "paths": [
                    {
                        "path": path,
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                    for path in paths
                ],
            }

        report = self._base(runner_storage=runner_storage)

        for name in ("workspace", "report", "record"):
            self.assertEqual(report["storage"][name]["writable"], True)

    def test_runner_storage_unverified_when_identity_drifts(self) -> None:
        def runner_storage(paths: list[str]) -> dict:
            return {
                "ok": True,
                "status": "ready",
                "paths": [
                    {
                        "path": path,
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                    for path in paths
                ],
            }

        report = self._base(
            runner_storage=runner_storage,
            runner_health=lambda: {
                "ok": True,
                "status": "ready",
                "pid": 2,
                "python_executable": str(self.root / "other-python"),
                "module_path": str(self.module),
                "executors": ["codex"],
            },
        )

        self.assertEqual(report["runner"]["identity"], "drift")
        self.assertEqual(report["storage"]["status"], "unavailable")
        self.assertIn(
            "Runner could not verify storage access.",
            report["storage"]["workspace"]["reason"],
        )

    def test_invalid_runner_storage_probe_fails_closed(self) -> None:
        secret = "storage-probe-secret"

        def invalid_probe(paths: list[str]) -> dict:
            return {"ok": False, "error": secret, "paths": paths}

        report = self._base(runner_storage=invalid_probe)

        self.assertEqual(report["storage"]["status"], "unavailable")
        self.assertNotIn(secret, json.dumps(report) + render_doctor_text(report))

    # -- skills ------------------------------------------------------------

    def test_skill_entries_are_ordered_and_classified(self) -> None:
        report = self._base()

        self.assertEqual(
            list(report["skills"]),
            ["codex", "claude", "hermes", "status", "warnings"],
        )
        for platform in ("codex", "claude", "hermes"):
            entry = report["skills"][platform]
            self.assertEqual(entry["classification"], "current")
            self.assertEqual(entry["status"], "healthy")
            self.assertEqual(entry["package_version"], __version__)

    # -- executors -----------------------------------------------------------

    def test_executor_projection_redacts_unknown_labels(self) -> None:
        secret = "executor-extra-secret"

        def probe(platform: str) -> dict:
            data = _healthy_executor_probe(platform)
            if platform == "codex":
                data["source"] = "raw-private-source"
                data["raw_output"] = secret
            return data

        report = self._base(executor_probe=probe)

        entry = report["executors"]["codex"]
        self.assertEqual(entry["source"], "unavailable")
        rendered = json.dumps(report) + render_doctor_text(report)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("raw-private-source", rendered)

    # -- permission runtime --------------------------------------------------

    def test_permission_runtime_projection_stays_redacted(self) -> None:
        report = self._base()

        projection = report["permission_runtime"]["claude_sdk_capability"]
        self.assertEqual(projection["selection"], "protocol_capability")
        self.assertEqual(projection["supported"], False)
        self.assertNotIn("command", projection)
        self.assertIn(
            report["permission_runtime"]["seatbelt_available"],
            {True, False},
        )
        self.assertEqual(
            report["permission_runtime"]["stable_block_codes"],
            sorted(report["permission_runtime"]["stable_block_codes"]),
        )

    # -- cleanup diagnostics ---------------------------------------------------

    def test_cleanup_collectors_are_consistent(self) -> None:
        tasks = [_cleanup_task("CLEAN-9", "codex", _receipt("succeeded"))]

        from_tasks = build_session_cleanup_diagnostics(tasks, now=T0)
        self.assertEqual(from_tasks["status"], "healthy")
        self.assertEqual(from_tasks["warnings"], 0)
        self.assertEqual(from_tasks["diagnostics"][0]["task_id"], "CLEAN-9")

        # The board-root variant reads the same shape from the record tree.
        _write_board_task(
            self.record,
            "BOARD-1",
            status="completed",
            extensions={},
        )
        from_board = collect_session_cleanup_diagnostics(self.record, now=T0)
        self.assertEqual(from_board["status"], "healthy")

    # -- blockers --------------------------------------------------------

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

        report = self._base()

        items = report["blockers"]["items"]
        self.assertEqual(
            [item["task_id"] for item in items], ["A-INPUT", "Z-INPUT"]
        )
        self.assertEqual(report["blockers"]["count"], 2)
        self.assertEqual(report["blockers"]["status"], "warning")
        self.assertNotIn("input-x", json.dumps(report))

    # -- deterministic ordering --------------------------------------------

    def test_checks_are_sorted_by_id_and_platforms_deterministic(self) -> None:
        report = self._base()

        ids = [check["id"] for check in report["checks"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            list(report["executors"]),
            ["codex", "claude", "hermes", "status", "warnings"],
        )
        self.assertEqual(
            list(report["skills"]),
            ["codex", "claude", "hermes", "status", "warnings"],
        )
        # Collector ordering is fixed by the assembler, independent of input.
        self.assertEqual(
            list(report),
            [
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
                "permission_runtime",
                "checks",
            ],
        )

    # -- detect_install_source stays public and behavioral -----------------

    def test_detect_install_source_precedence_unchanged(self) -> None:
        marker = self.root / ".agentbc-candidate"
        marker.write_text("candidate\n", encoding="utf-8")

        self.assertEqual(
            detect_install_source(
                self.module,
                distribution=FakeDistribution(
                    {"url": "file:///checkout", "dir_info": {"editable": True}}
                ),
                candidate_marker_paths=[marker],
            ),
            "candidate",
        )
        # An explicitly provided source checkout wins over the file scan.
        self.assertEqual(
            detect_install_source(
                self.module,
                distribution=None,
                candidate_marker_paths=[],
                source_checkout=self.root,
            ),
            "source_checkout",
        )
        # No distribution, no markers, no checkout ancestry: unknown.
        plain_dir = self.root / "plain"
        plain_dir.mkdir()
        plain_module = plain_dir / "agent_bridge_connect" / "__init__.py"
        plain_module.parent.mkdir(parents=True)
        plain_module.write_text("", encoding="utf-8")
        self.assertEqual(
            detect_install_source(
                plain_module,
                distribution=None,
                candidate_marker_paths=[],
            ),
            "unknown",
        )


class DoctorCollectorZeroWriteTests(unittest.TestCase):
    """Hard gate: collectors never write, never mutate, never network."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module = self.root / "agent_bridge_connect" / "__init__.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "tasks" / "report").mkdir(parents=True)
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
            files = _current_skill_files(platform)
            self.skill_current_files[platform] = files
            _install_current_skill(root, platform, files)

    def _snapshot(self) -> dict[str, tuple[int, int, str]]:
        state: dict[str, tuple[int, int, str]] = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            if path.is_file():
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state[relative] = (stat.st_size, stat.st_mtime_ns, digest)
            elif path.is_dir():
                state[relative] = (-1, int(path.stat().st_mtime_ns), "dir")
        return state

    @contextmanager
    def _forbid_writes_and_network(self, counter: dict):
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):  # noqa: ANN001
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                counter["writes"] += 1
                raise AssertionError(f"write attempted during doctor: {file}")
            return real_open(file, mode, *args, **kwargs)

        def guard(name: str):
            def _raise(*args, **kwargs):  # noqa: ANN001
                counter["writes"] += 1
                raise AssertionError(f"mutating call during doctor: {name}")

            return _raise

        def guarded_socket(*args, **kwargs):  # noqa: ANN001
            counter["network"] += 1
            raise AssertionError("network access attempted during doctor")

        with (
            mock.patch("builtins.open", side_effect=guarded_open),
            mock.patch("os.replace", side_effect=guard("os.replace")),
            mock.patch("os.rename", side_effect=guard("os.rename")),
            mock.patch("os.remove", side_effect=guard("os.remove")),
            mock.patch("os.unlink", side_effect=guard("os.unlink")),
            mock.patch("os.mkdir", side_effect=guard("os.mkdir")),
            mock.patch("os.makedirs", side_effect=guard("os.makedirs")),
            mock.patch("os.chmod", side_effect=guard("os.chmod")),
            mock.patch("shutil.rmtree", side_effect=guard("shutil.rmtree")),
            mock.patch("pathlib.Path.mkdir", side_effect=guard("Path.mkdir")),
            mock.patch(
                "pathlib.Path.write_text", side_effect=guard("Path.write_text")
            ),
            mock.patch(
                "pathlib.Path.write_bytes",
                side_effect=guard("Path.write_bytes"),
            ),
            mock.patch("socket.socket", side_effect=guarded_socket),
        ):
            yield

    def test_full_report_performs_zero_writes_and_zero_network(self) -> None:
        counter = {"writes": 0, "network": 0}
        before = self._snapshot()

        with self._forbid_writes_and_network(counter):
            report = build_doctor_report(
                config_path=self.config,
                runner_health=lambda: {
                    "ok": True,
                    "status": "ready",
                    "pid": 1,
                    "python_executable": str(self.root / "python"),
                    "module_path": str(self.module),
                    "executors": ["codex"],
                },
                module_path=self.module,
                executable_path=self.root / "agentbc",
                python_executable=self.root / "python",
                distribution=FakeDistribution(),
                candidate_marker_paths=[],
                build_info_path=self.build_info,
                board_root=self.record,
                skill_roots=self.skill_roots,
                skill_current_files=self.skill_current_files,
                executor_probe=_healthy_executor_probe,
            )
            rendered = render_doctor_text(report)
            self.assertTrue(rendered)
            self.assertEqual(report["status"], "healthy")

        after = self._snapshot()
        self.assertEqual(counter, {"writes": 0, "network": 0})
        self.assertEqual(before, after)

    def test_runner_unavailable_path_performs_zero_writes(self) -> None:
        counter = {"writes": 0, "network": 0}
        before = self._snapshot()

        def broken_health() -> dict:
            raise RunnerError("offline for zero-write proof")

        with self._forbid_writes_and_network(counter):
            report = build_doctor_report(
                config_path=self.config,
                runner_health=broken_health,
                module_path=self.module,
                executable_path=self.root / "agentbc",
                python_executable=self.root / "python",
                distribution=FakeDistribution(),
                candidate_marker_paths=[],
                build_info_path=self.build_info,
                board_root=self.record,
                skill_roots=self.skill_roots,
                skill_current_files=self.skill_current_files,
                executor_probe=_healthy_executor_probe,
            )

        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(counter["writes"], 0)
        self.assertEqual(counter["network"], 0)
        self.assertEqual(before, self._snapshot())

    def test_cleanup_collectors_perform_zero_writes(self) -> None:
        counter = {"writes": 0, "network": 0}
        before = self._snapshot()
        tasks = [
            _cleanup_task("CLEAN-A", "codex", _receipt("failed", retryable=True)),
            _cleanup_task("CLEAN-B", "hermes", _receipt("retained")),
        ]

        with self._forbid_writes_and_network(counter):
            built = build_session_cleanup_diagnostics(tasks, now=T0)
            collected = collect_session_cleanup_diagnostics(self.record, now=T0)

        self.assertEqual(built["status"], "warning")
        self.assertEqual(collected["status"], "healthy")
        self.assertEqual(counter["writes"], 0)
        self.assertEqual(counter["network"], 0)
        self.assertEqual(before, self._snapshot())


if __name__ == "__main__":
    unittest.main()
