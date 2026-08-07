from pathlib import Path
import os
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SUPPORT = ROOT / "tests" / "fixtures" / "archive_support"


class LocalAlphaBundleTests(unittest.TestCase):
    def test_bundle_scripts_are_executable_and_valid_posix_shell(self) -> None:
        for name in (
            "build_local_alpha_bundle.sh",
            "install_alpha_from_url.sh",
            "install_local_alpha.sh",
            "run_local_alpha_smoke.sh",
            "serve_local_alpha.sh",
            "uninstall_fallback.sh",
        ):
            script = ROOT / "scripts" / name
            self.assertTrue(script.is_file(), name)
            self.assertTrue(os.access(script, os.X_OK), name)
            subprocess.run(["sh", "-n", str(script)], check=True)

    def test_local_install_runs_setup_and_supports_pipe_safe_interaction(self) -> None:
        installer = (ROOT / "scripts" / "install_local_alpha.sh").read_text(encoding="utf-8")
        self.assertIn('RUN_SETUP=${AGENTBC_RUN_SETUP:-1}', installer)
        self.assertIn('SETUP_NON_INTERACTIVE=${AGENTBC_SETUP_NONINTERACTIVE:-0}', installer)
        self.assertIn('"$TARGET" setup </dev/tty', installer)
        self.assertIn('"$TARGET" setup --non-interactive', installer)
        self.assertNotIn('echo "next: agentbc setup"', installer)

    def test_cross_machine_guide_uses_extracted_bundle_directory(self) -> None:
        guide = (ARCHIVE_SUPPORT / "docs" / "MACOS_ALPHA_TEST.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("agentbc-1.0.0a1-macos-local-alpha", guide)
        self.assertIn("macos-local-alpha.tar.gz.sha256", guide)
        self.assertIn("shasum -a 256 -c SHA256SUMS", guide)

    def test_url_installer_binds_manifest_to_pinned_checksum(self) -> None:
        installer = (ROOT / "scripts" / "install_alpha_from_url.sh").read_text(encoding="utf-8")
        self.assertIn("AGENTBC_EXPECTED_SHA256", installer)
        self.assertIn("AGENTBC_PRODUCT_VERSION", installer)
        self.assertIn('VERSION=${BASE_URL##*/}', installer)
        self.assertIn('[ -f "$SCRIPT_DIR/build_provenance.py" ]', installer)
        self.assertIn('shasum -a 256 -c "$ARCHIVE.sha256"', installer)
        self.assertIn('echo "install: completed (setup included)"', installer)
        self.assertNotIn('echo "next: agentbc setup"', installer)

    def test_bundle_builder_publishes_curl_installer(self) -> None:
        builder = (ROOT / "scripts" / "build_local_alpha_bundle.sh").read_text(encoding="utf-8")
        self.assertIn("install-agentbc-alpha.sh", builder)
        self.assertIn("install_alpha_from_url.sh", builder)

    def test_local_server_rebuilds_before_serving_by_default(self) -> None:
        server = (ROOT / "scripts" / "serve_local_alpha.sh").read_text(encoding="utf-8")
        self.assertIn("AGENTBC_ALPHA_SKIP_BUILD", server)
        self.assertIn("AGENTBC_PRODUCT_VERSION=$VERSION", server)
        self.assertIn('"$SCRIPT_DIR/build_local_alpha_bundle.sh" "$DIST_ROOT"', server)

    def test_bundle_publishes_product_and_fallback_uninstall_paths(self) -> None:
        builder = (ROOT / "scripts" / "build_local_alpha_bundle.sh").read_text(encoding="utf-8")
        server = (ROOT / "scripts" / "serve_local_alpha.sh").read_text(encoding="utf-8")
        self.assertIn('cp "$SCRIPT_DIR/uninstall_fallback.sh"', builder)
        self.assertIn("uninstall-agentbc-alpha.sh", server)
        self.assertIn("agentbc uninstall", server)

    def test_fallback_uninstaller_works_without_agentbc_cli(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = home / "managed-workspace"
            board = home / "managed-board"
            install_root = home / ".agentbc-alpha"
            bin_dir = home / ".local" / "bin"
            customer = home / "customer-project"
            for path in (
                workspace / "tasks" / "artifacts",
                workspace / "tasks" / "report",
                board,
                install_root,
                bin_dir,
                home / ".abc",
                home / ".hermes" / "skills" / "agentbc",
                home / ".hermes" / "profiles" / "pm" / "skills" / "agentbc",
                home / ".claude" / "skills" / "agentbc",
                home / ".codex" / "skills" / "agentbc",
                customer,
            ):
                path.mkdir(parents=True, exist_ok=True)
            (home / ".abc" / "config.toml").write_text(
                f'board_root = "{board}"\nworkspace_root = "{workspace}"\n',
                encoding="utf-8",
            )
            (workspace / "tasks" / "report" / "record.md").write_text("report", encoding="utf-8")
            (workspace / "tasks" / "artifacts" / "artifact.txt").write_text("artifact", encoding="utf-8")
            (board / "task.json").write_text("{}", encoding="utf-8")
            (bin_dir / "abc").write_text("# AgentBC-owned abc shim\n", encoding="utf-8")
            (customer / "keep.txt").write_text("keep", encoding="utf-8")

            result = subprocess.run(
                [
                    "sh",
                    str(ROOT / "scripts" / "uninstall_fallback.sh"),
                    "--remove-records",
                    "--keep-artifacts",
                ],
                check=False,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "AGENTBC_ALPHA_HOME": str(install_root),
                    "AGENTBC_BIN_DIR": str(bin_dir),
                    "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
                },
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(board.exists())
            self.assertTrue((workspace / "tasks" / "artifacts" / "artifact.txt").is_file())
            self.assertFalse(install_root.exists())
            self.assertFalse((home / ".abc").exists())
            self.assertFalse((home / ".hermes" / "profiles" / "pm" / "skills" / "agentbc").exists())
            self.assertTrue((customer / "keep.txt").is_file())

    def test_fallback_uninstaller_prompts_for_both_data_choices(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = home / "Documents" / "AgentBC" / "workspace"
            board = workspace / "record"
            reports = workspace / "tasks" / "report"
            artifacts = workspace / "tasks" / "artifacts"
            install_root = home / ".agentbc-alpha"
            for path in (board, reports, artifacts, install_root, home / ".abc"):
                path.mkdir(parents=True, exist_ok=True)
            (home / ".abc" / "config.toml").write_text(
                f'board_root = "{board}"\nworkspace_root = "{workspace}"\n',
                encoding="utf-8",
            )
            (reports / "report.md").write_text("report", encoding="utf-8")
            (artifacts / "artifact.txt").write_text("artifact", encoding="utf-8")

            result = subprocess.run(
                ["sh", str(ROOT / "scripts" / "uninstall_fallback.sh")],
                input="y\nn\n",
                check=False,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "AGENTBC_ALPHA_HOME": str(install_root),
                    "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
                },
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Remove AgentBC runtime records at", result.stderr)
            self.assertIn("and reports at", result.stderr)
            self.assertIn("Remove AgentBC default workspace artifacts at", result.stderr)
            self.assertFalse(board.exists())
            self.assertTrue((artifacts / "artifact.txt").is_file())

    def test_fallback_uninstaller_removes_complete_default_root_when_both_choices_are_yes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = home / "Documents" / "AgentBC" / "workspace"
            board = workspace / "record"
            artifacts = workspace / "tasks" / "artifacts"
            reports = workspace / "tasks" / "report"
            legacy = workspace / "agentbc_tasks"
            for path in (board, artifacts, reports, legacy, home / ".agentbc-alpha", home / ".abc"):
                path.mkdir(parents=True, exist_ok=True)
            (home / ".abc" / "config.toml").write_text(
                f'board_root = "{board}"\nworkspace_root = "{workspace}/"\n',
                encoding="utf-8",
            )
            (reports / "report.md").write_text("report", encoding="utf-8")
            (artifacts / "artifact.txt").write_text("artifact", encoding="utf-8")
            (legacy / "old.txt").write_text("old", encoding="utf-8")

            result = subprocess.run(
                [
                    "sh",
                    str(ROOT / "scripts" / "uninstall_fallback.sh"),
                    "--remove-records",
                    "--remove-artifacts",
                ],
                check=False,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "AGENTBC_ALPHA_HOME": str(home / ".agentbc-alpha"),
                    "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
                },
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((home / "Documents" / "AgentBC").exists())


if __name__ == "__main__":
    unittest.main()
