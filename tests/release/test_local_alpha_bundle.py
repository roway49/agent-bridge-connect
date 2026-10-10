from pathlib import Path
import os
import subprocess
import unittest
import importlib.util as _repository_boundary_importlib
import unittest as _repository_boundary_unittest
from pathlib import Path as _repository_boundary_Path

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_SUPPORT = ROOT / "tests" / "fixtures" / "archive_support"


class LocalAlphaBundleTests(unittest.TestCase):
    def test_bundle_scripts_are_executable_and_valid_posix_shell(self) -> None:
        for name in ("build_local_alpha_bundle.sh", "install_alpha_from_url.sh", "install_local_alpha.sh", "run_local_alpha_smoke.sh", "serve_local_alpha.sh", "uninstall_fallback.sh"):
            script = ROOT / "scripts" / name
            self.assertTrue(script.is_file(), name)
            self.assertTrue(os.access(script, os.X_OK), name)
            subprocess.run(["sh", "-n", str(script)], check=True)

    def test_local_install_runs_setup_and_supports_pipe_safe_interaction(self) -> None:
        installer = (ROOT / "scripts" / "install_local_alpha.sh").read_text(encoding="utf-8")
        self.assertIn("RUN_SETUP=${AGENTBC_RUN_SETUP:-1}", installer)
        self.assertIn("SETUP_NON_INTERACTIVE=${AGENTBC_SETUP_NONINTERACTIVE:-0}", installer)
        self.assertIn('"$TARGET" setup </dev/tty', installer)
        self.assertIn('"$TARGET" setup --non-interactive', installer)
        self.assertNotIn('echo "next: agentbc setup"', installer)

    def test_cross_machine_guide_uses_extracted_bundle_directory(self) -> None:
        guide = (ARCHIVE_SUPPORT / "docs" / "MACOS_ALPHA_TEST.md").read_text(encoding="utf-8")
        self.assertIn("agentbc-1.0.0a1-macos-local-alpha", guide)
        self.assertIn("macos-local-alpha.tar.gz.sha256", guide)
        self.assertIn("shasum -a 256 -c SHA256SUMS", guide)

    def test_url_installer_binds_manifest_to_pinned_checksum(self) -> None:
        installer = (ROOT / "scripts" / "install_alpha_from_url.sh").read_text(encoding="utf-8")
        self.assertIn("AGENTBC_EXPECTED_SHA256", installer)
        self.assertIn("AGENTBC_PRODUCT_VERSION", installer)
        self.assertIn("VERSION=${BASE_URL##*/}", installer)
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
            (home / ".abc" / "config.toml").write_text(f'board_root = "{board}"\nworkspace_root = "{workspace}"\n', encoding="utf-8")
            (workspace / "tasks" / "report" / "record.md").write_text("report", encoding="utf-8")
            (workspace / "tasks" / "artifacts" / "artifact.txt").write_text("artifact", encoding="utf-8")
            (board / "task.json").write_text("{}", encoding="utf-8")
            (bin_dir / "abc").write_text("# AgentBC-owned abc shim\n", encoding="utf-8")
            (customer / "keep.txt").write_text("keep", encoding="utf-8")
            result = subprocess.run(
                ["sh", str(ROOT / "scripts" / "uninstall_fallback.sh"), "--remove-records", "--keep-artifacts"],
                check=False,
                env={**os.environ, "HOME": str(home), "AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(bin_dir), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1", "HERMES_HOME": str(home / ".hermes")},
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
            (home / ".abc" / "config.toml").write_text(f'board_root = "{board}"\nworkspace_root = "{workspace}"\n', encoding="utf-8")
            (reports / "report.md").write_text("report", encoding="utf-8")
            (artifacts / "artifact.txt").write_text("artifact", encoding="utf-8")
            result = subprocess.run(["sh", str(ROOT / "scripts" / "uninstall_fallback.sh")], input="y\nn\n", check=False, env={**os.environ, "HOME": str(home), "AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1"}, capture_output=True, text=True)
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
            (home / ".abc" / "config.toml").write_text(f'board_root = "{board}"\nworkspace_root = "{workspace}/"\n', encoding="utf-8")
            (reports / "report.md").write_text("report", encoding="utf-8")
            (artifacts / "artifact.txt").write_text("artifact", encoding="utf-8")
            (legacy / "old.txt").write_text("old", encoding="utf-8")
            result = subprocess.run(
                ["sh", str(ROOT / "scripts" / "uninstall_fallback.sh"), "--remove-records", "--remove-artifacts"], check=False, env={**os.environ, "HOME": str(home), "AGENTBC_ALPHA_HOME": str(home / ".agentbc-alpha"), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1"}, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((home / "Documents" / "AgentBC").exists())


_repository_boundary_ROOT = _repository_boundary_Path(__file__).resolve().parents[2]
_repository_boundary_SCRIPT = _repository_boundary_ROOT / "scripts" / "check_repository_boundary.py"
_repository_boundary_SPEC = _repository_boundary_importlib.spec_from_file_location("check_repository_boundary", _repository_boundary_SCRIPT)
assert _repository_boundary_SPEC is not None and _repository_boundary_SPEC.loader is not None
_repository_boundary_BOUNDARY = _repository_boundary_importlib.module_from_spec(_repository_boundary_SPEC)
_repository_boundary_SPEC.loader.exec_module(_repository_boundary_BOUNDARY)


class _repository_boundary_RepositoryBoundaryTests(_repository_boundary_unittest.TestCase):
    def test_manifest_matches_working_tree_root_files(self) -> None:
        self.assertEqual(_repository_boundary_BOUNDARY.expected_root_files(), _repository_boundary_BOUNDARY.working_tree_root_files())

    def test_manifest_matches_index_root_files(self) -> None:
        self.assertEqual(_repository_boundary_BOUNDARY.expected_root_files(), _repository_boundary_BOUNDARY.index_root_files())

    def test_validator_rejects_added_and_deleted_root_files(self) -> None:
        expected = _repository_boundary_BOUNDARY.expected_root_files()
        self.assertEqual(0, _repository_boundary_BOUNDARY.validate(expected, source="test"))
        self.assertEqual(1, _repository_boundary_BOUNDARY.validate(expected | {"PRIVATE_NOTES.md"}, source="test"))
        self.assertEqual(1, _repository_boundary_BOUNDARY.validate(expected - {"README.md"}, source="test"))


class InstallationIdentityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest import mock
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.environment = mock.patch.dict(os.environ, {'HOME': str(self.home), 'PATH': '',
            'AGENTBC_ALPHA_HOME': str(self.home / '.agentbc-alpha'), 'AGENTBC_BIN_DIR': str(self.home / '.local/bin')}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.shared_paths = mock.patch('agent_bridge_connect.installation.shared_entries', return_value=())
        self.shared_paths.start()
        self.addCleanup(self.shared_paths.stop)

    def test_both_directions_reject_before_mutation_even_stopped(self):
        from agent_bridge_connect.installation import transaction, preflight, InstallationError, state_root
        for existing, candidate in (('native', 'docker'), ('docker', 'native')):
            with transaction(existing, self.home / existing, version='test'):
                pass
            before = (state_root() / 'installation.json').read_bytes()
            with self.assertRaisesRegex(InstallationError, 'Uninstall'):
                preflight(candidate, self.home / candidate)
            self.assertEqual((state_root() / 'installation.json').read_bytes(), before)
            (state_root() / 'installation.json').unlink()

    def test_dispatch_shared_barrier_allows_bridge_but_blocks_maintenance(self):
        from unittest import mock
        from agent_bridge_connect import docker_install
        from agent_bridge_connect.installation import installation_lock, InstallationError
        profile = {'installation_id': 'owned', 'root': str(self.home)}
        seen = []
        def routed(path, argv):
            # This is the desktop readiness check that previously deadlocked.
            with installation_lock(shared=True):
                seen.append(path)
                self.assertTrue(path.is_file())
            with self.assertRaises(InstallationError):
                with installation_lock():
                    self.fail('maintenance overlapped dispatch')
            return 0
        with mock.patch.object(docker_install, 'load', return_value=profile), \
             mock.patch.object(docker_install, 'read_receipt', return_value={'installation_id': 'owned', 'state': 'installed'}), \
             mock.patch.object(docker_install, 'verify_engine'), mock.patch.object(docker_install, 'verify_binding'), \
             mock.patch('agent_bridge_connect.docker_host.run', side_effect=routed):
            for _ in range(2):
                self.assertEqual(docker_install.run(self.home / 'docker-host.json', ['task', 'create']), 0)
            self.assertNotEqual(seen[0], seen[1])
            self.assertTrue(all(not p.exists() for p in seen))
            with installation_lock():
                with self.assertRaises(InstallationError):
                    with installation_lock(shared=True):
                        self.fail('dispatch entered during maintenance')

    def test_legacy_native_outside_path_blocks_docker(self):
        from agent_bridge_connect.installation import preflight, InstallationError
        entry = self.home / '.agentbc-alpha/venv/bin/agentbc'
        entry.parent.mkdir(parents=True)
        entry.write_text('from agent_bridge_connect.cli import main')
        with self.assertRaisesRegex(InstallationError, 'Conflicting'):
            preflight('docker', self.home / '.agentbc-docker')
        self.assertFalse((self.home / '.agentbc-install').exists())

    def test_same_type_different_prefix_and_concurrent_lock_rejected(self):
        from agent_bridge_connect.installation import transaction, preflight, InstallationError, installation_lock
        with transaction('native', self.home / 'native', version='test'):
            with self.assertRaisesRegex(InstallationError, 'Another installation'):
                with installation_lock():
                    self.fail('second lock acquired')
        with self.assertRaisesRegex(InstallationError, 'existing installation root'):
            preflight('native', self.home / 'another')

    def test_unregistered_partial_runtime_without_entry_blocks_install(self):
        import shutil
        from agent_bridge_connect.installation import preflight, InstallationError
        for folder in ('.agentbc-alpha', '.agentbc-docker'):
            root = self.home / folder
            (root / 'venv').mkdir(parents=True)
            for kind in ('native', 'docker'):
                with self.assertRaisesRegex(InstallationError, 'Partial AgentBC'):
                    preflight(kind, self.home / 'new')
            shutil.rmtree(root)

    def test_failure_remains_repair_required_and_blocks_both_types(self):
        from agent_bridge_connect.installation import transaction, preflight, InstallationError, read_receipt
        with self.assertRaises(RuntimeError):
            with transaction('docker', self.home / 'docker', version='test'):
                raise RuntimeError('interrupted')
        self.assertEqual(read_receipt()['state'], 'repair_required')
        for kind in ('native', 'docker'):
            with self.assertRaises(InstallationError):
                preflight(kind, self.home / kind)

    def test_interrupted_update_rejects_unbound_or_corrupt_backup_before_control(self):
        import json
        from unittest import mock
        from agent_bridge_connect.docker_install_update import rollback_update
        from agent_bridge_connect.installation import InstallationError
        root = self.home / '.agentbc-docker'
        saved = self.home / '.agentbc-install/backups/update'
        saved.mkdir(parents=True)
        (saved / 'data.tar').write_bytes(b'corrupted')
        (saved / 'checksums.json').write_text(json.dumps({'data.tar': '0' * 64}))
        receipt = {'state': 'installing', 'root': str(root), 'installation_id': 'one',
                   'update': {'old': {'root': str(root), 'installation_id': 'one'},
                              'states': {}, 'backup': str(saved), 'candidate_image': 'sha256:new'}}
        with mock.patch('agent_bridge_connect.docker_install_update.compose') as control:
            with self.assertRaisesRegex(InstallationError, 'checksum'):
                rollback_update(receipt)
            receipt['update']['old']['installation_id'] = 'other'
            with self.assertRaisesRegex(InstallationError, 'identity'):
                rollback_update(receipt)
            control.assert_not_called()

    def test_restore_archive_requires_explicit_dot_prefix_before_stripping(self):
        import tarfile
        from agent_bridge_connect.docker_install_update import validate_restore_archive
        from agent_bridge_connect.installation import InstallationError
        archive = self.home / 'backup.tar'
        for name, valid in (('.', True), ('./record/file', True), ('record/file', False)):
            with tarfile.open(archive, 'w') as writer:
                writer.addfile(tarfile.TarInfo(name))
            if valid:
                validate_restore_archive(archive)
            else:
                with self.assertRaisesRegex(InstallationError, 'prefix'):
                    validate_restore_archive(archive)

    def test_native_uninstall_legacy_terminal_and_explicit_blocker_details(self):
        import json
        from agent_bridge_connect.installation_native import assert_idle_board
        from agent_bridge_connect.installation import InstallationError
        root = self.home / 'board'
        task = root / 'OLD1/001'
        task.mkdir(parents=True)
        (task / 'task.json').write_text(json.dumps({'id': 'OLD1-001', 'status': 'cancelled'}))
        (task / 'run_lease.json').write_text(json.dumps({'task_id': 'OLD1-001', 'state': 'closed'}))
        before = (task / 'task.json').read_bytes()
        assert_idle_board(root)
        self.assertEqual((task / 'task.json').read_bytes(), before)
        (task / 'run_lease.json').write_text(json.dumps({'task_id': 'OLD1-001', 'state': 'active'}))
        with self.assertRaisesRegex(InstallationError, 'OLD1-001 .*lease=active'):
            assert_idle_board(root)
        (task / 'task.json').write_text(json.dumps({'id': 'OLD1-001', 'status': 'pending'}))
        with self.assertRaisesRegex(InstallationError, 'OLD1-001 .*pending'):
            assert_idle_board(root)

    def test_webui_config_validation_does_not_import_native_service(self):
        import sys
        from unittest import mock
        from agent_bridge_connect.config import validate_config
        from agent_bridge_connect.docker_install_setup import selections
        with mock.patch.dict(sys.modules, {'agent_bridge_connect.web_service': None}):
            self.assertEqual(validate_config({'webui': {'preferred_view': 'webui', 'port': 50757, 'autostart': False}}), [])
            self.assertTrue(validate_config({'webui': {'port': 1}}))
            with mock.patch('builtins.input', return_value='1'):
                settings = selections({'settings': {'port': 50757}}, True)
            self.assertEqual(settings['preferred_view'], 'task_list')
            self.assertEqual(validate_config({'webui': settings}), [])

    def test_docker_feedback_separates_host_runtime_connection_and_skill(self):
        import io
        from contextlib import redirect_stdout
        from agent_bridge_connect.docker_setup_guidance import print_scan, print_summary
        profile = {'setup_binding': {'executors': ['codex', 'hermes']},
                   'executor_imports': {'codex': 'old status', 'hermes': 'old task completed'}}
        agents = [{'name': 'codex', 'found': False}, {'name': 'hermes', 'found': True},
                  {'name': 'claude', 'found': True}]
        with redirect_stdout(io.StringIO()) as output:
            print_scan(agents, profile, ['codex', 'hermes'])
            print_summary(profile)
        text = output.getvalue()
        self.assertNotIn('detected but not enabled', text)
        self.assertNotIn('old task completed', text)
        self.assertIn('Not detected', text)
        self.assertIn('Saved settings', text)
        self.assertIn('Runtime not included in this image', text)
        self.assertIn('Existing Docker settings kept', text)
        self.assertIn('do not install AI runtimes', text)

    def test_preview_existing_connection_keeps_model_and_skips_missing_runtime(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_bridge_connect.docker_setup_native import configure
        agents = [{'name': 'hermes', 'display': 'Hermes', 'found': True},
                  {'name': 'claude', 'display': 'Claude', 'found': True}]
        config = {'executors': {'hermes': {'model': 'deepseek-v4-flash'}}}
        with mock.patch('agent_bridge_connect.setup.scan_all_agents', return_value=agents), \
             mock.patch('agent_bridge_connect.setup._print_scan_report'), \
             mock.patch('agent_bridge_connect.docker_setup_native.portable_files', return_value=([('.hermes/config.yaml', b'model: other')], [])), \
             mock.patch('builtins.input', return_value=''), redirect_stdout(io.StringIO()):
            selected, imports = configure({'executor_imports': {'hermes': 'linked'}}, config,
                                           interactive=True, available=['hermes'])
        self.assertEqual(imports, {})
        self.assertEqual(selected['executors']['hermes']['model'], 'deepseek-v4-flash')
        self.assertNotIn('claude', selected['executors'])

    def test_preview_verify_rejects_compose_tampering_before_docker_control(self):
        from unittest import mock
        from agent_bridge_connect.docker_preview_setup import verify
        p = self.home / 'compose.json'
        p.write_text('{}')
        profile = {'setup_binding': {'engine': {'id': 'expected'}, 'compose': str(p), 'compose_sha256': 'wrong'}}
        with mock.patch('agent_bridge_connect.docker_install_runtime.engine_identity', return_value={'id': 'expected'}), \
             mock.patch('agent_bridge_connect.docker_preview_setup.validate_private'), \
             mock.patch('agent_bridge_connect.docker_preview_setup.execute') as docker:
            with self.assertRaisesRegex(ValueError, 'Compose configuration changed'):
                verify(profile)
            docker.assert_not_called()

    def test_preview_workspace_copy_preserves_files_and_rejects_existing_target(self):
        import runpy
        import io
        from contextlib import redirect_stdout
        module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/prepare_windows_preview_setup.py'))
        source, target = self.home / 'source', self.home / 'target'
        (source / '中文 path').mkdir(parents=True)
        target.mkdir()
        (source / '中文 path/report.md').write_text('report evidence')
        code = module['COPY'].replace("Path('/source')", 'Path(' + repr(str(source)) + ')').replace("Path('/target')", 'Path(' + repr(str(target)) + ')')
        with redirect_stdout(io.StringIO()):
            exec(code, {})
        self.assertEqual((target / '中文 path/report.md').read_text(), 'report evidence')
        with self.assertRaisesRegex(AssertionError, 'Destination must be empty'):
            exec(code, {})
        self.assertEqual((source / '中文 path/report.md').read_text(), 'report evidence')

    def test_windows_hermes_detects_localappdata_and_preserves_explicit_home(self):
        from unittest import mock
        from agent_bridge_connect.setup import _active_hermes_home
        from agent_bridge_connect.docker_setup_native import portable_files
        local = self.home / 'LocalAppData'
        active = local / 'hermes'
        active.mkdir(parents=True)
        (active / 'config.yaml').write_text('model: chosen\n')
        (active / '.env').write_text('DEEPSEEK_API_KEY=test-only\n')
        with mock.patch('sys.platform', 'win32'), mock.patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
            self.assertEqual(_active_hermes_home(), active)
            files, _ = portable_files('hermes')
            self.assertEqual(dict(files)['.hermes/.env'], b'DEEPSEEK_API_KEY=test-only\n')
            with mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home / 'explicit')}):
                self.assertEqual(_active_hermes_home(), self.home / 'explicit')
            traditional = self.home / '.hermes'
            traditional.mkdir()
            (traditional / 'config.yaml').write_text('model: original\n')
            self.assertEqual(_active_hermes_home(), traditional)

    def test_portable_profiles_keep_auth_model_permissions_and_original_files(self):
        import json
        from agent_bridge_connect.config import load_config
        from agent_bridge_connect.docker_setup_native import portable_files
        codex = self.home / '.codex'
        codex.mkdir()
        original = ('model="chosen"\napproval_policy="on-request"\nsandbox_mode="workspace-write"\n'
                    '[model_providers.custom]\nbase_url="https://provider.invalid"\n'
                    '[mcp_servers.host]\ncommand="/host/tool"\n')
        (codex / 'config.toml').write_text(original)
        (codex / 'auth.json').write_text('{"OPENAI_API_KEY":"private-test"}')
        files, excluded = portable_files('codex')
        converted = self.home / 'converted.toml'
        converted.write_bytes(dict(files)['.codex/config.toml'])
        config = load_config(converted)
        self.assertEqual(config['approval_policy'], 'on-request')
        self.assertEqual(config['sandbox_mode'], 'workspace-write')
        self.assertEqual(config['model'], 'chosen')
        self.assertEqual(config['model_providers']['custom']['base_url'], 'https://provider.invalid')
        self.assertNotIn('mcp_servers', config)
        self.assertIn('mcp_servers', excluded)
        self.assertEqual((codex / 'config.toml').read_text(), original)
        self.assertEqual(dict(files)['.codex/auth.json'], b'{"OPENAI_API_KEY":"private-test"}')
        claude = self.home / '.claude'
        claude.mkdir()
        settings = {'env': {'ANTHROPIC_API_KEY': 'private-test'}, 'model': 'chosen',
                    'permissions': {'defaultMode': 'default', 'deny': ['Bash(rm:*)']},
                    'hooks': {'host': []}, 'enabledPlugins': {'host': True}}
        (claude / 'settings.json').write_text(json.dumps(settings))
        files, excluded = portable_files('claude')
        result = json.loads(dict(files)['.claude/settings.json'])
        self.assertEqual(result['permissions'], settings['permissions'])
        self.assertEqual(result['env'], settings['env'])
        self.assertNotIn('hooks', result)
        self.assertEqual(json.loads((claude / 'settings.json').read_text()), settings)

    def test_codex_catalog_import_is_portable_and_keeps_host_unchanged(self):
        from agent_bridge_connect.docker_setup_native import portable_files
        from agent_bridge_connect.config import load_config
        folder = self.home / '.codex'
        folder.mkdir()
        original = 'model="chosen"\nmodel_catalog_json="catalog.json"\n'
        (folder / 'config.toml').write_text(original)
        (folder / 'catalog.json').write_text('{"models": []}')
        files = dict(portable_files('codex')[0])
        converted = self.home / 'converted.toml'
        converted.write_bytes(files['.codex/config.toml'])
        target = load_config(converted)['model_catalog_json']
        self.assertTrue(target.startswith('/profiles/codex/.codex/agentbc-model-catalog-'))
        self.assertEqual(files[target.removeprefix('/profiles/codex/')], b'{"models": []}')
        self.assertEqual((folder / 'config.toml').read_text(), original)
        (folder / 'catalog.json').unlink()
        with self.assertRaisesRegex(Exception, 'model catalog is missing'):
            portable_files('codex')
        (folder / 'catalog.json').write_text('not json')
        with self.assertRaisesRegex(Exception, 'model catalog is missing'):
            portable_files('codex')
        (folder / 'config.toml').write_text('model_catalog_json="../outside.json"')
        (self.home / 'outside.json').write_text('{"models": []}')
        with self.assertRaisesRegex(Exception, 'model catalog is missing'):
            portable_files('codex')

    def test_native_questions_link_detected_settings_without_profile_path_prompts(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_bridge_connect.docker_setup_native import configure
        folder = self.home / '.claude'
        folder.mkdir()
        (folder / 'settings.json').write_text('{"env":{"ANTHROPIC_API_KEY":"secret-test"},"hooks":{}}')
        agents = [{'name': 'claude', 'display': 'Claude Code', 'found': True}]
        prompts = []
        def answer(prompt):
            prompts.append(prompt)
            return ''
        with mock.patch('agent_bridge_connect.setup.scan_all_agents', return_value=agents), \
             mock.patch('agent_bridge_connect.setup._print_scan_report'), \
             mock.patch('builtins.input', side_effect=answer), redirect_stdout(io.StringIO()) as output:
            config, imports = configure({}, {}, interactive=True)
        self.assertIn('claude', imports)
        self.assertEqual(config['permissions']['mode'], 'inherit')
        self.assertEqual(config['executors']['claude']['max_budget_usd'], 10)
        self.assertFalse(config['sessions']['retain_executor_sessions'])
        self.assertIn('Link detected Claude Code settings to Docker?', prompts[1])
        self.assertNotIn('profile', ' '.join(prompts).lower())
        self.assertNotIn('secret-test', output.getvalue())
        self.assertIn('hooks', output.getvalue())
        with mock.patch('agent_bridge_connect.setup.scan_all_agents', return_value=agents), \
             mock.patch('agent_bridge_connect.setup._print_scan_report'), \
             mock.patch('builtins.input', side_effect=AssertionError('must not prompt')), \
             redirect_stdout(io.StringIO()):
            preserved, imports = configure({}, config, interactive=False)
        self.assertEqual(preserved, config)
        self.assertEqual(imports, {})

    def test_data_only_does_not_block_and_symlink_receipt_does(self):
        from agent_bridge_connect.installation import preflight, read_receipt, InstallationError, state_root
        (self.home / 'Documents/AgentBC/workspace/record').mkdir(parents=True)
        self.assertIsNone(preflight('docker', self.home / 'docker'))
        state_root().mkdir(mode=0o700)
        target = self.home / 'external'
        target.write_text('{}')
        (state_root() / 'installation.json').symlink_to(target)
        with self.assertRaises(InstallationError):
            read_receipt()

    def test_docker_settings_defaults_and_explicit_background_no(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_setup import selections
        with mock.patch('agent_bridge_connect.docker_install_setup.free_port', return_value=12345):
            result = selections({}, False)
            self.assertEqual(result, {'preferred_view': 'webui', 'autostart': False, 'port': 12345})
            with mock.patch('builtins.input', side_effect=['', 'n']):
                self.assertFalse(selections({}, True)['autostart'])
            with mock.patch('builtins.input', side_effect=['', '']):
                self.assertTrue(selections({}, True)['autostart'])

    def test_compose_isolated_webui_and_explicit_project_paths(self):
        from agent_bridge_connect.docker_install_runtime import compose_spec
        profile = {'installation_id': 'test', 'root': str(self.home), 'image_id': 'sha256:test',
                   'projects': [str(self.home / '工程 with spaces')], 'settings': {'port': 12345}}
        spec = compose_spec(profile)
        runner = spec['services']['runner']
        web = spec['services']['webui']
        self.assertEqual(web['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual([m['source'] for m in web['volumes']], ['data'])
        self.assertTrue(web['volumes'][0]['read_only'])
        self.assertEqual(runner['volumes'][-1]['source'], profile['projects'][0])
        self.assertEqual(web['restart'], 'no')
        self.assertNotIn('environment', web)

    def test_unregistered_docker_and_unavailable_discovery_fail_closed(self):
        from unittest import mock
        from agent_bridge_connect.installation import preflight, InstallationError
        with mock.patch('agent_bridge_connect.installation.discover_managed_docker', return_value=['owned-container']):
            with self.assertRaisesRegex(InstallationError, 'Managed Docker'):
                preflight('native', self.home / 'native')
        with mock.patch('agent_bridge_connect.installation.discover_managed_docker', side_effect=InstallationError('Cannot complete Docker installation discovery')):
            with self.assertRaisesRegex(InstallationError, 'Cannot complete'):
                preflight('docker', self.home / 'docker')

    def test_bundle_rejects_hash_drift_and_unlisted_files(self):
        import hashlib
        import json
        from agent_bridge_connect.docker_install_bundle import verify_bundle
        from agent_bridge_connect.installation import InstallationError
        root = self.home / 'bundle'
        root.mkdir()
        names = ['image.tar', 'seccomp.json', 'install_docker.sh', 'recover_docker.sh', 'agentbc.whl']
        for name in names:
            (root / name).write_bytes(b'fixture')
        manifest = {'schema': 1, 'type': 'docker', 'platform': 'macos-arm64', 'data_schema': 1,
                    'image_id': 'sha256:fixture', 'wheel': 'agentbc.whl',
                    'files': {name: hashlib.sha256(b'fixture').hexdigest() for name in names}}
        (root / 'bundle.json').write_text(json.dumps(manifest))
        self.assertEqual(verify_bundle(root)['type'], 'docker')
        (root / 'image.tar').write_bytes(b'corrupted')
        with self.assertRaisesRegex(InstallationError, 'checksum'):
            verify_bundle(root)
        (root / 'image.tar').write_bytes(b'fixture')
        (root / 'extra').write_bytes(b'not inventoried')
        with self.assertRaisesRegex(InstallationError, 'inventory'):
            verify_bundle(root)

    def test_windows_workspace_is_explicit_empty_and_shared_readonly_with_webui(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_paths import initialize_workspace
        from agent_bridge_connect.docker_install_runtime import compose_spec
        from agent_bridge_connect.installation import InstallationError
        folder = self.home / 'Documents/AgentBC/workspace'
        profile = {'platform': 'windows-amd64', 'installation_id': 'test', 'root': str(self.home), 'image_id': 'sha256:test', 'settings': {'port': 32100}}
        with mock.patch('agent_bridge_connect.docker_preview_setup.documents_workspace', return_value=folder):
            initialize_workspace(profile, interactive=False)
            self.assertEqual(profile['host_workspace'], str(folder))
            spec = compose_spec(profile)
            self.assertEqual(spec['services']['runner']['healthcheck']['test'], ['CMD', 'agentbc', 'runner', 'status'])
            web = spec['services']['webui']['volumes'][0]
            self.assertEqual(web['type'], 'bind')
            self.assertTrue(web['read_only'])
            self.assertEqual(web['source'], str(folder))
            (folder / 'keep.txt').write_text('history')
            with self.assertRaisesRegex(InstallationError, 'empty'):
                initialize_workspace({'platform': 'windows-amd64'}, interactive=False)
            self.assertEqual((folder / 'keep.txt').read_text(), 'history')

    def test_windows_maintenance_entry_uses_base_python_outside_venv(self):
        import json
        import sys
        from unittest import mock
        from agent_bridge_connect.docker_install_paths import windows_entry
        root = self.home / '.agentbc-docker'
        (root / 'bundle').mkdir(parents=True)
        (root / 'bundle/bundle.json').write_text(json.dumps({'wheel': 'agentbc.whl'}))
        with mock.patch.object(sys, '_base_executable', str(self.home / 'Python/python.exe')):
            entry = windows_entry({'root': str(root)}, root / 'docker-host.json')
        self.assertIn('"uninstall" goto maintenance', entry)
        self.assertIn(str(self.home / 'Python/python.exe'), entry.split(':maintenance')[1])
        self.assertNotIn(str(root / 'venv'), entry.split(':maintenance')[1])
        self.assertIn('-- %*', entry)
        self.assertIn(':maintenance\n(goto) 2>nul & ', entry)
        self.assertNotIn('exit /b', entry.split(':maintenance\n')[1])

    def test_workspace_reinstall_archives_only_after_explicit_selection(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_paths import initialize_workspace
        from agent_bridge_connect.installation import InstallationError
        folder = self.home / 'Documents/AgentBC/workspace'
        folder.mkdir(parents=True)
        (folder / 'history.txt').write_bytes(b'preserved history')
        with mock.patch('agent_bridge_connect.docker_preview_setup.documents_workspace', return_value=folder), mock.patch('builtins.input', side_effect=['', '3']):
            with self.assertRaisesRegex(InstallationError, 'cancelled'):
                initialize_workspace({'platform': 'windows-amd64'}, interactive=True)
        self.assertEqual((folder / 'history.txt').read_bytes(), b'preserved history')
        with mock.patch('agent_bridge_connect.docker_preview_setup.documents_workspace', return_value=folder), mock.patch('builtins.input', side_effect=['', '1']):
            initialize_workspace({'platform': 'windows-amd64'}, interactive=True)
        archives = list(folder.parent.glob('workspace-backup-*'))
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / 'history.txt').read_bytes(), b'preserved history')
        self.assertEqual(list(folder.iterdir()), [])

    def test_workspace_archive_refuses_active_task(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_paths import initialize_workspace
        from agent_bridge_connect.installation import InstallationError
        folder = self.home / 'workspace'
        task = folder / 'record/day/TEST-001/task.json'
        task.parent.mkdir(parents=True)
        task.write_text('{"id":"TEST-001","status":"running"}')
        with mock.patch('agent_bridge_connect.docker_preview_setup.documents_workspace', return_value=folder), mock.patch('builtins.input', side_effect=['', '1']):
            with self.assertRaisesRegex(InstallationError, 'Unfinished'):
                initialize_workspace({'platform': 'windows-amd64'}, interactive=True)
        self.assertTrue(task.exists())

    def test_install_rollback_uses_setup_persisted_workspace_binding(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_bundle import rollback_install
        root = self.home / 'managed'
        root.mkdir()
        (root / 'docker-host.json').write_text('{}')
        saved = {'installation_id': 'owned', 'host_workspace': str(self.home / 'workspace')}
        with mock.patch('agent_bridge_connect.docker_install.load', return_value=saved), mock.patch('agent_bridge_connect.docker_install_runtime.service_ids', return_value={'runner': {}}), mock.patch('agent_bridge_connect.docker_install_runtime.require_idle', side_effect=RuntimeError('stop before mutation')) as idle:
            with self.assertRaisesRegex(RuntimeError, 'stop before mutation'):
                rollback_install({'installation_id': 'owned'}, root, {'installation_id': 'owned'})
            idle.assert_called_once_with(saved)

    def test_maintenance_accepts_cancelled_but_rejects_open_leases(self):
        import contextlib
        import io
        from unittest import mock
        from agent_bridge_connect.docker_install_runtime import require_idle
        from agent_bridge_connect.installation import InstallationError
        root = self.home / 'maintenance-record'
        root.mkdir()
        def read_volume(profile, code, readonly):
            self.assertTrue(readonly)
            code = code.replace('/data/workspace/record', str(root))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(code, {})
            return output.getvalue()
        with mock.patch('agent_bridge_connect.docker_install_runtime.volume_python', side_effect=read_volume), mock.patch('agent_bridge_connect.task_store.TaskStore.list_tasks') as tasks:
            for status in ('cancelled', 'rejected', 'completed', 'failed', 'needs_recovery'):
                tasks.return_value = [{'id': 'TEST-001', 'status': status}]
                require_idle({})
            tasks.return_value = [{'id': 'TEST-001', 'status': 'running'}]
            with self.assertRaises(InstallationError):
                require_idle({})
            tasks.return_value = [{'id': 'TEST-001', 'status': 'cancelled'}]
            (root / 'run_lease.json').write_text('{"state":"active"}')
            with self.assertRaises(InstallationError):
                require_idle({})

    def test_runner_readiness_requires_real_health_and_reports_startup_failures(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_runtime import ready
        from agent_bridge_connect.installation import InstallationError
        cases = [
            ({'Running': True, 'Status': 'running'}, 'no active healthcheck'),
            ({'Running': False, 'Status': 'exited', 'ExitCode': 2}, 'exit code 2'),
        ]
        for state, message in cases:
            with self.subTest(state=state), mock.patch('agent_bridge_connect.docker_install_runtime.service_ids', return_value={'runner': {'State': state}}):
                with self.assertRaisesRegex(InstallationError, message):
                    ready({})
        starting = {'runner': {'State': {'Running': True, 'Health': {'Status': 'starting'}}}}
        healthy = {'runner': {'State': {'Running': True, 'Health': {'Status': 'healthy'}}}}
        with mock.patch('agent_bridge_connect.docker_install_runtime.service_ids', side_effect=[starting, healthy]), mock.patch('agent_bridge_connect.docker_install_runtime.time.sleep'):
            ready({})
        with mock.patch('agent_bridge_connect.docker_install_runtime.service_ids', return_value=starting), mock.patch('agent_bridge_connect.docker_install_runtime.time.monotonic', side_effect=[0, 1, 46]), mock.patch('agent_bridge_connect.docker_install_runtime.time.sleep'):
            with self.assertRaisesRegex(InstallationError, 'last state: starting'):
                ready({})

    def test_skill_destinations_are_frozen_across_home_changes(self):
        from agent_bridge_connect.docker_install_paths import skill_destinations
        expected = {name: str(self.home / 'original' / name) for name in ('codex', 'claude', 'hermes')}
        self.assertEqual(skill_destinations({'skill_paths': expected}), {name: Path(path) for name, path in expected.items()})

    def test_preview_retirement_preserves_history_and_removes_only_owned_entries(self):
        from unittest import mock
        from agent_bridge_connect.docker_preview_retire import retire
        from agent_bridge_connect.installation import private_json
        root = self.home / 'preview/host'
        (root / 'bin').mkdir(parents=True)
        entry = root / 'bin/agentbc.cmd'
        entry.write_text('agent_bridge_connect.docker_host preview')
        public = self.home / '.local/bin/agentbc.cmd'
        public.parent.mkdir(parents=True)
        public.write_bytes(entry.read_bytes())
        workspace = self.home / 'Documents/AgentBC/workspace'
        (workspace / 'record').mkdir(parents=True)
        (workspace / 'old-report.txt').write_text('keep history')
        profile = {'schema': 1, 'host_workspace': str(workspace), 'setup_binding': {'engine': {'context': 'test'}, 'containers': {}}}
        path = root / 'docker-host.json'
        private_json(path, profile)
        with mock.patch('agent_bridge_connect.docker_preview_setup.verify', return_value=(None, {'name': 'owned'})), mock.patch('agent_bridge_connect.windows_desktop.desktop_engine', return_value={'context': 'test'}), mock.patch('agent_bridge_connect.windows_desktop.register'), mock.patch('agent_bridge_connect.docker_approval_bridge.execute', return_value=''):
            result = retire(path, confirmed=True)
        self.assertFalse(public.exists())
        self.assertFalse(root.exists())
        self.assertEqual((Path(result['history']) / 'old-report.txt').read_text(), 'keep history')
        self.assertTrue((Path(result['backup']) / 'host/docker-host.json').exists())

    def test_preview_backup_refuses_changed_or_linked_content(self):
        from agent_bridge_connect.docker_preview_retire import checked_copy
        from agent_bridge_connect.installation import InstallationError
        source, target = self.home / 'source', self.home / 'backup'
        source.mkdir()
        (source / 'settings').write_text('original')
        checked_copy(source, target)
        (source / 'settings').write_text('changed')
        with self.assertRaisesRegex(InstallationError, 'Backup verification'):
            checked_copy(source, target)
        self.assertEqual((source / 'settings').read_text(), 'changed')
        (source / 'link').symlink_to(target / 'settings')
        with self.assertRaisesRegex(ValueError, 'reparse'):
            checked_copy(source, self.home / 'other')

    def test_generated_entry_uses_importable_module_in_main_context(self):
        import json
        from unittest import mock
        import agent_bridge_connect.docker_install as module
        root = self.home / '.agentbc-docker'
        root.mkdir()
        profile = {'root': str(root), 'version': '1.0.4a1'}
        path = root / 'docker-host.json'
        path.write_text(json.dumps(profile))
        from agent_bridge_connect.setup import _current_skill_files
        from agent_bridge_connect.skill_packages import build_skill_manifest, replace_managed_skill_package
        legacy = _current_skill_files('codex')
        legacy['SKILL.md'] += b'\n## Docker host binding\n\nUse the installed `agentbc` CLI. It manages the registered Docker installation. Never call the container CLI directly or start a native Runner. Use host setup for project mounts and webui start/close/status for monitoring.\n'
        skill = self.home / '.codex/skills/agentbc'
        replace_managed_skill_package(skill, platform='codex', files=legacy,
            manifest=build_skill_manifest('codex', profile['version'], legacy))
        with mock.patch.object(module, '__name__', '__main__'):
            module.entry_and_skills(profile, path)
        entry = (root / 'bin/agentbc').read_text()
        self.assertIn('agent_bridge_connect.docker_install', entry)
        self.assertNotIn('-m __main__', entry)
        self.assertIn('container_session_archive_then_delete', (skill / 'SKILL.md').read_text())
        with (skill / 'SKILL.md').open('a') as stream:
            stream.write('user edit')
        with self.assertRaisesRegex(module.InstallationError, 'Locally modified'):
            module.entry_and_skills(profile, path)
        self.assertEqual((self.home / '.local/bin/agentbc').resolve(), (root / 'bin/agentbc').resolve())

    def test_initial_setup_creates_readonly_ui_roots_before_service_readiness(self):
        from unittest import mock
        from agent_bridge_connect.docker_install_setup import setup
        profile = {'settings': {}, 'projects': [], 'container_id': 'runner', 'engine': {'context': 'test'},
                   'root': str(self.home), 'installation_id': 'test'}
        events = []
        with mock.patch('agent_bridge_connect.docker_install_setup.selections', return_value={'preferred_view':'webui','port':12345,'autostart':False}), \
             mock.patch('agent_bridge_connect.docker_install.entry_and_skills'), \
             mock.patch('agent_bridge_connect.docker_install_setup.write_compose'), \
             mock.patch('agent_bridge_connect.docker_install_setup.compose') as compose_call, \
             mock.patch('agent_bridge_connect.docker_install_setup.refresh_binding'), \
             mock.patch('agent_bridge_connect.docker_install_setup.import_config'), \
             mock.patch('agent_bridge_connect.docker_install_setup.docker', side_effect=lambda *a, **k: events.append(a)), \
             mock.patch('agent_bridge_connect.docker_install_setup.ready', side_effect=lambda *a: events.append(('ready',))), \
             mock.patch('agent_bridge_connect.docker_install_supervisor.register'):
            setup(profile, self.home / 'profile.json', interactive=False, initial=True)
        self.assertIn('/data/workspace/tasks/report', events[0])
        self.assertEqual(events[-1], ('ready',))
        compose_call.assert_any_call(profile, 'restart', 'runner')

    def test_foreign_compose_container_blocks_service_control(self):
        import json
        from unittest import mock
        from agent_bridge_connect.docker_install_runtime import compose
        from agent_bridge_connect.installation import InstallationError
        profile = {'project': 'owned', 'installation_id': 'owner', 'engine': {'context': 'test'}}
        with mock.patch('agent_bridge_connect.docker_install_runtime.verify_engine'), \
             mock.patch('agent_bridge_connect.docker_install_runtime.docker', side_effect=['foreign', json.dumps([{'Config': {'Labels': {'io.agentbc.installation':'someone-else'}}}])]) as engine:
            with self.assertRaisesRegex(InstallationError, 'foreign'):
                compose(profile, 'stop')
        self.assertFalse(any(call.args[:1] == ('compose',) for call in engine.call_args_list))

    def test_managed_runner_lock_rejects_duplicate_but_recovers_stale_pid(self):
        from unittest import mock
        from agent_bridge_connect.docker_runner_entry import claim_runtime, main
        with mock.patch('sys.platform', 'linux'), mock.patch('sys.argv', ['entry', 'runner', 'serve']), mock.patch.dict(os.environ, {}, clear=True), mock.patch('agent_bridge_connect.docker_runner_entry.claim_runtime'), mock.patch('os.execv') as execute:
            main()
            self.assertEqual(os.environ['AGENTBC_SESSION_STORE'], 'isolated_container')
            execute.assert_called_once_with('/usr/local/bin/agentbc', ['agentbc', 'runner', 'serve'])
        state, spool = self.home / 'state', self.home / 'spool'
        state.mkdir()
        spool.mkdir()
        (state / 'runner.pid').write_text(str(os.getpid()))
        fd = claim_runtime(state, spool)
        try:
            self.assertFalse((state / 'runner.pid').exists())
            (state / 'runner.pid').write_text('active')
            with self.assertRaises(BlockingIOError):
                claim_runtime(state, spool)
            self.assertEqual((state / 'runner.pid').read_text(), 'active')
        finally:
            os.close(fd)
        fd = claim_runtime(state, spool)
        os.close(fd)
        self.assertFalse((state / 'runner.pid').exists())

    def test_uninstall_resume_keeps_identity_until_all_owned_resources_removed(self):
        import json
        from unittest import mock
        from agent_bridge_connect.installation import private_json, state_root, InstallationError
        from agent_bridge_connect.docker_install_maintenance import uninstall
        root = self.home / '.agentbc-docker'
        root.mkdir()
        backup = state_root() / 'backups/saved'
        backup.mkdir(parents=True, mode=0o700)
        receipt = {'schema':1,'type':'docker','state':'uninstalling','installation_id':'owned','root':str(root),'version':'test',
                   'uninstall':{'backup':str(backup),'remove_records':False,'remove_artifacts':False,'keep_credentials':False}}
        private_json(state_root() / 'installation.json', receipt)
        profile = {'root':str(root),'installation_id':'owned','project':'owned','engine':{'context':'test'}}
        with mock.patch('agent_bridge_connect.docker_install_maintenance.assert_project_owned'), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.require_idle'), \
             mock.patch('agent_bridge_connect.docker_install_supervisor.register'), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.service_ids', return_value={}), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.docker', side_effect=InstallationError('engine lost')):
            with self.assertRaisesRegex(InstallationError, 'engine lost'):
                uninstall(profile)
        self.assertEqual(json.loads((state_root()/'installation.json').read_text())['state'],'uninstalling')
        self.assertTrue(root.exists())
        with mock.patch('agent_bridge_connect.docker_install_maintenance.assert_project_owned'), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.require_idle'), \
             mock.patch('agent_bridge_connect.docker_install_supervisor.register'), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.service_ids', return_value={}), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.volume_python'), \
             mock.patch('agent_bridge_connect.docker_install_maintenance.docker', return_value=''):
            self.assertTrue(uninstall(profile)['ok'])
        self.assertFalse(root.exists())
        self.assertFalse((state_root()/'installation.json').exists())
        self.assertTrue(backup.exists())

    def test_api_environment_import_is_explicit_scoped_and_preserves_values(self):
        import json
        from agent_bridge_connect.docker_profiles import read_environment
        path = self.home / 'env.json'
        secret = 'synthetic-only-$with#special=characters'
        path.write_text(json.dumps({'ANTHROPIC_API_KEY': secret, 'ANTHROPIC_BASE_URL':'https://example.invalid/api'}))
        self.assertEqual(read_environment('claude', path)['ANTHROPIC_API_KEY'], secret)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            read_environment('codex', path)
        path.write_text(json.dumps({'LD_PRELOAD':'/untrusted/library.so'}))
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            read_environment('claude', path)


class HostPlatformPrimitiveTests(unittest.TestCase):
    def test_bundle_platform_rejects_unsupported_host_and_maps_architecture(self):
        from unittest.mock import patch
        from agent_bridge_connect.host_platform import bundle_platform, image_architecture
        with patch('platform.system', return_value='Windows'), patch('platform.machine', return_value='AMD64'):
            self.assertEqual(bundle_platform(), 'windows-amd64')
        with patch('platform.system', return_value='Linux'), patch('platform.machine', return_value='x86_64'):
            with self.assertRaises(ValueError):
                bundle_platform()
        self.assertEqual(image_architecture('windows-amd64'), 'amd64')
        self.assertEqual(image_architecture('macos-arm64'), 'arm64')
        with self.assertRaises(ValueError):
            image_architecture('windows-arm64')

    def test_host_lock_excludes_another_process_and_releases_on_close(self):
        import tempfile
        import sys
        from agent_bridge_connect.host_platform import try_lock
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'lock'
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            code = ('import os,sys; from agent_bridge_connect.host_platform import try_lock; '
                    'fd=os.open(sys.argv[1],os.O_RDWR); try_lock(fd); os.close(fd)')
            try:
                try_lock(fd)
                blocked = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn(b'BlockingIOError', blocked.stderr)
            finally:
                os.close(fd)
            released = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_shared_host_lock_allows_reader_and_excludes_writer_process(self):
        import tempfile
        import sys
        from agent_bridge_connect.host_platform import try_lock
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'lock'
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            code = ('import os,sys; from agent_bridge_connect.host_platform import try_lock; '
                    'fd=os.open(sys.argv[1],os.O_RDWR); try_lock(fd,shared=sys.argv[2]=="read"); os.close(fd)')
            try:
                try_lock(fd, shared=True)
                reader = subprocess.run([sys.executable, '-c', code, str(path), 'read'], capture_output=True)
                self.assertEqual(reader.returncode, 0, reader.stderr)
                writer = subprocess.run([sys.executable, '-c', code, str(path), 'write'], capture_output=True)
                self.assertNotEqual(writer.returncode, 0)
                self.assertIn(b'BlockingIOError', writer.stderr)
            finally:
                os.close(fd)
            writer = subprocess.run([sys.executable, '-c', code, str(path), 'write'], capture_output=True)
            self.assertEqual(writer.returncode, 0, writer.stderr)

    def test_platform_mismatch_update_rejected_before_docker_mutation(self):
        from unittest.mock import patch
        from agent_bridge_connect.docker_install_update import update
        from agent_bridge_connect.installation import InstallationError
        with patch('agent_bridge_connect.docker_install_update.verify_bundle', return_value={'platform': 'windows-amd64'}), \
             patch('agent_bridge_connect.docker_install_update.docker') as docker:
            with self.assertRaisesRegex(InstallationError, 'platform'):
                update({'data_schema': 1}, Path('/unused/profile'), Path('/unused/bundle'))
            docker.assert_not_called()

    def test_compose_architecture_comes_from_installation_identity(self):
        from agent_bridge_connect.docker_install_runtime import compose_spec
        profile = {'root': '/tmp/owned', 'installation_id': 'test', 'image_id': 'sha256:abc',
                   'projects': [], 'settings': {'port': 58013}, 'platform': 'windows-amd64', 'project': 'owned'}
        spec = compose_spec(profile)
        self.assertEqual(spec['services']['runner']['platform'], 'linux/amd64')
        self.assertEqual(spec['services']['webui']['platform'], 'linux/amd64')
        profile.pop('platform')
        self.assertEqual(compose_spec(profile)['services']['runner']['platform'], 'linux/arm64')
