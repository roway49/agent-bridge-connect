from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService
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
        with mock.patch(
            "agent_bridge_connect.update._local_install_strategy",
            return_value={"method": "homebrew", "reason": "Homebrew owns the CLI"},
        ):
            result = run_update_flow(
                self.service,
                checker=_available,
                input_fn=lambda _prompt: self.fail("must not prompt"),
                output_fn=lambda _line: None,
            )
        self.assertEqual(result["state"], "homebrew_update_required")
        self.assertEqual(result["upgrade_command"], "brew upgrade agentbc")
        self.assertEqual(
            sorted(str(path.relative_to(self.board)) for path in self.board.rglob("*")),
            before,
        )

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

    def test_post_switch_failure_restores_old_cli_and_refreshes_old_skills(self) -> None:
        install_root = Path(self.temporary.name) / "install"
        bin_dir = Path(self.temporary.name) / "bin"
        old_cli = install_root / "venv" / "bin" / "agentbc"
        old_cli.parent.mkdir(parents=True)
        old_cli.write_text("old", encoding="utf-8")
        bin_dir.mkdir()
        target = bin_dir / "agentbc"
        target.symlink_to(old_cli)
        wheel_bytes = b"verified-wheel"
        release = {
            **_available(),
            "wheel": {
                **_available()["wheel"],
                "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
            },
        }

        def completed(args, **_kwargs):
            return mock.Mock(returncode=0, stdout="agentbc 1.0.4a1\n", args=args)

        environment = {
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(bin_dir),
        }
        with (
            mock.patch.dict("os.environ", environment),
            mock.patch("agent_bridge_connect.update._require_current_install_identity"),
            mock.patch("agent_bridge_connect.update._fetch_bytes", return_value=wheel_bytes),
            mock.patch("agent_bridge_connect.update.subprocess.run", side_effect=completed) as run,
            mock.patch(
                "agent_bridge_connect.update._require_post_update_identity",
                side_effect=ABCError("update_identity_mismatch", "simulated"),
            ),
            mock.patch(
                "agent_bridge_connect.runner.stop_runner_background",
                return_value={"ok": True},
            ),
            self.assertRaises(ABCError) as raised,
        ):
            install_verified_release(release)

        self.assertEqual(raised.exception.code, "update_install_failed")
        self.assertEqual(target.resolve(), old_cli.resolve())
        commands = [call.args[0] for call in run.call_args_list]
        setup_commands = [command for command in commands if "setup" in command]
        self.assertEqual(len(setup_commands), 2)
        self.assertTrue(all("--update" in command for command in setup_commands))


if __name__ == "__main__":
    unittest.main()
