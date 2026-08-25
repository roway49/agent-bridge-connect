from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService
from agent_bridge_connect import update as update_module
from agent_bridge_connect.update import (
    check_for_update,
    install_verified_release,
    run_update_flow,
)


def _available() -> dict:
    return {
        "state": "update_available",
        "code": "",
        "channel": "alpha",
        "current": "1.0.3a1",
        "latest": "1.0.4a1",
        "update_available": True,
        "source": "github_release_manifest",
        "release_url": "https://example.test/release",
        "summary": "fixes",
        "wheel": {
            "filename": "agentbc-1.0.4a1-py3-none-any.whl",
            "sha256": "a" * 64,
            "url": "https://example.test/wheel",
        },
    }


class UpdateResolutionTests(unittest.TestCase):
    def _responses(self, *, tag: str = "v1.0.4A", tamper_manifest: bool = False):
        package = "1.0.4a1" if tag == "v1.0.4A" else "1.0.3a1"
        wheel_name = f"agentbc-{package}-py3-none-any.whl"
        wheel_sha = "b" * 64
        manifest = {
            "schema_version": 1,
            "tag": tag,
            "package_version": package,
            "commit_sha": "c" * 40,
            "source_tree_sha256": "d" * 64,
            "artifacts": [{"filename": wheel_name, "size": 10, "sha256": wheel_sha}],
        }
        manifest_bytes = json.dumps(manifest).encode()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        release = [{
            "tag_name": tag,
            "draft": False,
            "html_url": "https://example.test/release",
            "body": "release notes",
            "assets": [
                {
                    "name": "release-manifest.json",
                    "browser_download_url": "https://example.test/manifest",
                    "digest": f"sha256:{'0' * 64 if tamper_manifest else manifest_digest}",
                },
                {
                    "name": wheel_name,
                    "browser_download_url": "https://example.test/wheel",
                    "digest": f"sha256:{wheel_sha}",
                },
            ],
        }]
        payloads = {
            "https://example.test/releases": json.dumps(release).encode(),
            "https://example.test/manifest": manifest_bytes,
        }
        return lambda url: payloads[url]

    def test_verified_manifest_reports_available_or_current(self) -> None:
        available = check_for_update(
            releases_url="https://example.test/releases",
            fetch_bytes=self._responses(),
        )
        self.assertTrue(available["update_available"])
        self.assertEqual(available["latest"], "1.0.4a1")
        current = check_for_update(
            releases_url="https://example.test/releases",
            fetch_bytes=self._responses(tag="v1.0.3A"),
        )
        self.assertFalse(current["update_available"])
        self.assertEqual(current["state"], "current")

    def test_manifest_digest_mismatch_fails_closed(self) -> None:
        with self.assertRaises(ABCError) as raised:
            check_for_update(
                releases_url="https://example.test/releases",
                fetch_bytes=self._responses(tamper_manifest=True),
            )
        self.assertEqual(raised.exception.code, "update_manifest_hash_mismatch")


class UpdateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.board = Path(self.temporary.name) / "record"
        self.service = TaskService(self.board, config={"workspace_root": self.temporary.name})

    def test_current_and_decline_are_zero_write(self) -> None:
        before = sorted(str(path.relative_to(self.board)) for path in self.board.rglob("*"))
        current = {**_available(), "state": "current", "latest": "1.0.3a1", "update_available": False}
        result = run_update_flow(
            self.service,
            checker=lambda: current,
            input_fn=lambda _prompt: self.fail("must not prompt"),
            output_fn=lambda _line: None,
        )
        self.assertEqual(result["state"], "current")
        self.assertEqual(
            sorted(str(path.relative_to(self.board)) for path in self.board.rglob("*")),
            before,
        )

        installer = mock.Mock()
        result = run_update_flow(
            self.service,
            checker=_available,
            input_fn=lambda _prompt: "n",
            output_fn=lambda _line: None,
            installer=installer,
        )
        self.assertEqual(result["state"], "update_declined")
        self.assertEqual(
            sorted(str(path.relative_to(self.board)) for path in self.board.rglob("*")),
            before,
        )
        installer.assert_not_called()

    def test_explicit_yes_runs_preflight_then_installer(self) -> None:
        installer = mock.Mock(return_value={"version": "1.0.4a1", "runner_refreshed": True})
        result = run_update_flow(
            self.service,
            checker=_available,
            input_fn=lambda _prompt: "yes",
            output_fn=lambda _line: None,
            installer=installer,
        )
        self.assertEqual(result["state"], "updated")
        self.assertTrue(result["preflight"]["cutover_ready"])
        installer.assert_called_once()

    def test_homebrew_route_does_not_prompt_or_write_cutover_stamp(self) -> None:
        before = sorted(str(path.relative_to(self.board)) for path in self.board.rglob("*"))
        checker = mock.Mock(side_effect=AssertionError("must not check release index"))
        with mock.patch(
            "agent_bridge_connect.update._local_install_strategy",
            return_value={"method": "homebrew", "reason": "Homebrew owns the CLI"},
        ):
            result = run_update_flow(
                self.service,
                checker=checker,
                input_fn=lambda _prompt: self.fail("must not prompt"),
                output_fn=lambda _line: None,
            )
        checker.assert_not_called()
        self.assertEqual(result["state"], "homebrew_update_required")
        self.assertEqual(result["upgrade_command"], "brew upgrade agentbc")
        self.assertEqual(result["source"], "homebrew")
        self.assertEqual(
            sorted(str(path.relative_to(self.board)) for path in self.board.rglob("*")),
            before,
        )

    def test_zero_write_routes_do_not_construct_task_service(self) -> None:
        service_factory = mock.Mock(side_effect=AssertionError("must remain lazy"))
        current = {**_available(), "state": "current", "latest": "1.0.3a1", "update_available": False}
        result = run_update_flow(
            None,
            service_factory=service_factory,
            checker=lambda: current,
            input_fn=lambda _prompt: self.fail("must not prompt"),
            output_fn=lambda _line: None,
            installer=mock.Mock(),
        )
        self.assertEqual(result["state"], "current")
        service_factory.assert_not_called()

        service_factory.reset_mock()
        with mock.patch(
            "agent_bridge_connect.update._local_install_strategy",
            return_value={"method": "homebrew", "reason": "Homebrew owns the CLI"},
        ):
            result = run_update_flow(
                None,
                service_factory=service_factory,
                checker=mock.Mock(side_effect=AssertionError("must not check")),
                input_fn=lambda _prompt: self.fail("must not prompt"),
                output_fn=lambda _line: None,
            )
        self.assertEqual(result["state"], "homebrew_update_required")
        service_factory.assert_not_called()

    def test_confirmed_update_constructs_task_service_once(self) -> None:
        service_factory = mock.Mock(return_value=self.service)
        installer = mock.Mock(return_value={"version": "1.0.4a1", "runner_refreshed": True})
        result = run_update_flow(
            None,
            service_factory=service_factory,
            checker=_available,
            input_fn=lambda _prompt: "yes",
            output_fn=lambda _line: None,
            installer=installer,
        )
        self.assertEqual(result["state"], "updated")
        service_factory.assert_called_once_with()

    def test_homebrew_runtime_python_is_detected_without_cli_argv_receipt(self) -> None:
        cellar_python = (
            Path(self.temporary.name)
            / "Cellar"
            / "agentbc"
            / "1.0.3a1"
            / "libexec"
            / "bin"
            / "python"
        )
        with (
            mock.patch.object(update_module.sys, "executable", str(cellar_python)),
            mock.patch.object(update_module, "_invoked_cli_path", return_value=None),
        ):
            strategy = update_module._local_install_strategy()
        self.assertEqual(strategy["method"], "homebrew")
        self.assertIn("Python environment", strategy["reason"])

    def test_installer_refuses_unmanaged_cli_and_hash_failure_is_pre_switch(self) -> None:
        install_root = Path(self.temporary.name) / "install"
        bin_dir = Path(self.temporary.name) / "bin"
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.write_text("user-owned", encoding="utf-8")
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "AGENTBC_ALPHA_HOME": str(install_root),
                    "AGENTBC_BIN_DIR": str(bin_dir),
                },
            ),
            self.assertRaises(ABCError) as raised,
        ):
            install_verified_release(_available())
        self.assertEqual(raised.exception.code, "update_install_unsupported")
        self.assertEqual(target.read_text(encoding="utf-8"), "user-owned")

        target.unlink()
        managed_cli = install_root / "venv" / "bin" / "agentbc"
        managed_cli.parent.mkdir(parents=True)
        managed_cli.write_text("managed", encoding="utf-8")
        target.symlink_to(managed_cli)
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "AGENTBC_ALPHA_HOME": str(install_root),
                    "AGENTBC_BIN_DIR": str(bin_dir),
                },
            ),
            mock.patch("agent_bridge_connect.update._require_current_install_identity"),
            mock.patch("agent_bridge_connect.update._fetch_bytes", return_value=b"wrong"),
            self.assertRaises(ABCError) as raised,
        ):
            install_verified_release(_available())
        self.assertEqual(raised.exception.code, "update_wheel_hash_mismatch")
        self.assertEqual(target.resolve(), managed_cli.resolve())

    def test_installer_refuses_homebrew_and_other_external_symlinks(self) -> None:
        install_root = Path(self.temporary.name) / "install"
        bin_dir = Path(self.temporary.name) / "bin"
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        cellar_cli = Path(self.temporary.name) / "Cellar" / "agentbc" / "1.0.3" / "bin" / "agentbc"
        cellar_cli.parent.mkdir(parents=True)
        cellar_cli.write_text("brew", encoding="utf-8")
        target.symlink_to(cellar_cli)
        environment = {
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(bin_dir),
        }
        with mock.patch.dict("os.environ", environment), self.assertRaises(ABCError) as raised:
            install_verified_release(_available())
        self.assertEqual(raised.exception.code, "update_homebrew_managed")
        self.assertEqual(target.resolve(), cellar_cli.resolve())

        target.unlink()
        external_cli = Path(self.temporary.name) / "pipx" / "bin" / "agentbc"
        external_cli.parent.mkdir(parents=True)
        external_cli.write_text("pipx", encoding="utf-8")
        target.symlink_to(external_cli)
        with mock.patch.dict("os.environ", environment), self.assertRaises(ABCError) as raised:
            install_verified_release(_available())
        self.assertEqual(raised.exception.code, "update_install_unsupported")
        self.assertEqual(target.resolve(), external_cli.resolve())

    def test_identity_guards_require_runner_match_and_current_skills(self) -> None:
        from agent_bridge_connect.update import (
            _require_current_install_identity,
            _require_post_update_identity,
        )

        current = {
            "package": {"version": "1.0.3a1", "status": "healthy"},
            "runner": {"status": "ready", "identity": "match"},
            "skills": {},
        }
        with mock.patch("agent_bridge_connect.update._doctor_report", return_value=current):
            _require_current_install_identity(Path("agentbc"), _available())

        drifted = {
            "package": {"version": "1.0.4a1", "status": "healthy"},
            "runner": {"status": "ready", "identity": "match"},
            "skills": {
                "codex": {"installed": True, "up_to_date": False},
                "claude": {"installed": False, "up_to_date": False},
                "hermes": {"installed": True, "up_to_date": True},
            },
        }
        with (
            mock.patch("agent_bridge_connect.update._doctor_report", return_value=drifted),
            self.assertRaises(ABCError) as raised,
        ):
            _require_post_update_identity(Path("agentbc"), "1.0.4a1")
        self.assertEqual(raised.exception.code, "update_skill_identity_mismatch")

    def _run_transaction_failure(self, failure: str) -> ABCError:
        """Exercise one post-stop failure and assert the transaction proof."""
        install_root = Path(self.temporary.name) / "install"
        bin_dir = Path(self.temporary.name) / "bin"
        old_cli = install_root / "venv" / "bin" / "agentbc"
        old_cli.parent.mkdir(parents=True)
        old_cli.write_text("old", encoding="utf-8")
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.symlink_to(old_cli)
        old_link = os.readlink(target)

        skill_base = Path(self.temporary.name) / "skills"
        codex_root = skill_base / "codex"
        claude_root = skill_base / "claude"
        hermes_home = skill_base / "hermes"
        hermes_roots = [
            hermes_home / "skills" / "agentbc",
            hermes_home / "profiles" / "alpha" / "skills" / "agentbc",
            hermes_home / "profiles" / "beta" / "skills" / "agentbc",
        ]
        skill_specs = [
            ("codex", codex_root),
            ("claude", claude_root),
            *(('hermes', root) for root in hermes_roots),
        ]
        from agent_bridge_connect.setup import _current_skill_files
        from agent_bridge_connect.skill_packages import (
            MANIFEST_NAME,
            build_skill_manifest,
            serialize_skill_manifest,
        )

        def write_package(
            root: Path,
            platform: str,
            version: str,
            *,
            target_version: bool,
            write_manifest: bool = True,
        ) -> None:
            root.mkdir(parents=True, exist_ok=True)
            base_files = _current_skill_files(platform)
            files = {
                relative_path: (
                    f"target:{platform}:{relative_path}".encode()
                    if target_version
                    else content
                )
                for relative_path, content in base_files.items()
            }
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
        original_managed: dict[tuple[Path, str], tuple[bytes, str]] = {}
        unrelated: dict[Path, bytes] = {}
        for _platform, root in skill_specs:
            for path in root.rglob("*"):
                if path.is_file() and path.name != "unrelated-user-file.txt":
                    content = path.read_bytes()
                    original_managed[(root, str(path.relative_to(root)))] = (
                        content,
                        hashlib.sha256(content).hexdigest(),
                    )
            unrelated[root] = (root / "unrelated-user-file.txt").read_bytes()

        wheel_bytes = b"verified-wheel"
        release = {
            **_available(),
            "wheel": {
                **_available()["wheel"],
                "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
            },
        }
        current_identity = {
            "package": {"version": "1.0.3a1", "status": "healthy"},
            "runner": {"status": "ready", "identity": "match"},
            "skills": {},
        }
        target_skill_query = {
            "codex": {
                "root": str(codex_root),
                "files": sorted(
                    set(_current_skill_files("codex"))
                    | {"new-managed.txt", MANIFEST_NAME}
                ),
            },
            "claude": {
                "root": str(claude_root),
                "files": sorted(
                    set(_current_skill_files("claude"))
                    | {"new-managed.txt", MANIFEST_NAME}
                ),
            },
            "hermes": [
                {
                    "root": str(root),
                    "files": sorted(
                        set(_current_skill_files("hermes"))
                        | {"new-managed.txt", MANIFEST_NAME}
                    ),
                }
                for _platform, root in skill_specs
                if _platform == "hermes"
            ],
        }
        calls: list[list[str]] = []
        events: list[str] = []
        post_identity_calls: list[tuple[str, str]] = []

        def write_target_skills(*, commit_manifest: bool = True) -> None:
            for platform, root in skill_specs:
                write_package(
                    root,
                    platform,
                    "1.0.4a1",
                    target_version=True,
                    write_manifest=commit_manifest,
                )

        def completed(args, **_kwargs):
            command = [str(part) for part in args]
            calls.append(command)
            if len(command) > 1 and command[1] == "-c":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps(target_skill_query),
                    args=args,
                )
            if "setup" in command:
                if failure == "setup":
                    raise subprocess.CalledProcessError(1, command)
                write_target_skills(commit_manifest=failure != "setup_partial")
                if failure == "setup_partial":
                    raise subprocess.CalledProcessError(1, command)
                return mock.Mock(returncode=0, stdout="", args=args)
            if command[-2:] == ["runner", "start"]:
                is_target = str(install_root / "versions") in command[0]
                events.append("target_runner_start" if is_target else "old_runner_start")
                if is_target and failure == "runner":
                    raise subprocess.CalledProcessError(1, command)
                return mock.Mock(returncode=0, stdout="", args=args)
            return mock.Mock(returncode=0, stdout="agentbc 1.0.4a1\n", args=args)

        def post_identity(cli: Path, version: str) -> None:
            post_identity_calls.append((str(cli), version))
            is_target = str(install_root / "versions") in str(cli)
            if is_target and failure in {"post_identity", "restore_identity"}:
                raise ABCError("update_identity_mismatch", "simulated target identity failure")
            if not is_target and failure == "restore_identity":
                raise ABCError("update_identity_mismatch", "simulated old identity failure")

        def stop_runner() -> dict[str, object]:
            events.append("runner_stop")
            return {"ok": True}

        environment = {
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(bin_dir),
            "AGENTBC_CODEX_SKILL_PATH": str(codex_root),
            "AGENTBC_CLAUDE_SKILL_PATH": str(claude_root / "SKILL.md"),
            "AGENTBC_HERMES_SKILL_PATH": "",
            "HERMES_HOME": str(hermes_home),
        }
        with (
            mock.patch.dict("os.environ", environment, clear=False),
            mock.patch(
                "agent_bridge_connect.update._require_current_install_identity",
                return_value=current_identity,
            ),
            mock.patch("agent_bridge_connect.update._fetch_bytes", return_value=wheel_bytes),
            mock.patch("agent_bridge_connect.update.subprocess.run", side_effect=completed),
            mock.patch("agent_bridge_connect.update._require_post_update_identity", side_effect=post_identity),
            mock.patch(
                "agent_bridge_connect.runner.stop_runner_background",
                side_effect=stop_runner,
            ),
            self.assertRaises(ABCError) as raised,
        ):
            install_verified_release(release)

        expected_code = "update_rollback_incomplete" if failure == "restore_identity" else "update_install_failed"
        self.assertEqual(raised.exception.code, expected_code)
        self.assertEqual(os.readlink(target), old_link)
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
            self.assertEqual(hashlib.sha256(restored).hexdigest(), digest)
        for root, content in unrelated.items():
            self.assertEqual((root / "unrelated-user-file.txt").read_bytes(), content)
            self.assertFalse((root / "new-managed.txt").exists())
        setup_commands = [command for command in calls if "setup" in command]
        self.assertEqual(len(setup_commands), 1)
        self.assertTrue("--update" in setup_commands[0])
        old_runner_commands = [
            command
            for command in calls
            if command[-2:] == ["runner", "start"] and str(install_root / "versions") not in command[0]
        ]
        self.assertEqual(len(old_runner_commands), 1)
        return raised.exception

    def test_installed_skill_drift_blocks_before_runner_stop_or_switch(self) -> None:
        install_root = Path(self.temporary.name) / "install"
        bin_dir = Path(self.temporary.name) / "bin"
        old_cli = install_root / "venv" / "bin" / "agentbc"
        old_cli.parent.mkdir(parents=True)
        old_cli.write_text("old", encoding="utf-8")
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.symlink_to(old_cli)
        before_link = os.readlink(target)
        report = {
            "package": {"version": "1.0.3a1", "status": "healthy"},
            "runner": {"status": "ready", "identity": "match"},
            "skills": {
                "codex": {"installed": True, "up_to_date": True},
                "hermes": {
                    "installed": True,
                    "up_to_date": True,
                    "profiles": [{"installed": True, "up_to_date": False}],
                },
            },
        }
        environment = {
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(bin_dir),
        }
        with (
            mock.patch.dict("os.environ", environment, clear=False),
            mock.patch("agent_bridge_connect.update._doctor_report", return_value=report),
            mock.patch("agent_bridge_connect.update._fetch_bytes") as fetch,
            mock.patch("agent_bridge_connect.runner.stop_runner_background") as stop,
            mock.patch("agent_bridge_connect.update.subprocess.run") as run,
            self.assertRaises(ABCError) as raised,
        ):
            install_verified_release(_available())
        self.assertEqual(raised.exception.code, "update_skill_identity_mismatch")
        stop.assert_not_called()
        fetch.assert_not_called()
        run.assert_not_called()
        self.assertEqual(os.readlink(target), before_link)

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


if __name__ == "__main__":
    unittest.main()
