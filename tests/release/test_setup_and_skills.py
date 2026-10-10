"""Phase 10c tests: setup PathProvider, Hermes Skill, and setup modes."""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import shutil as _hermes_setup_shutil
import sys as _hermes_setup_sys
import tempfile as _hermes_setup_tempfile
import unittest as _hermes_setup_unittest
from pathlib import Path as _hermes_setup_Path
from unittest import mock as _hermes_setup_mock


class PathProviderTests(unittest.TestCase):
    @mock.patch("agent_bridge_connect.setup._version_for", return_value="Hermes test")
    def test_discover_hermes_prefers_real_venv_over_path_wrapper(self, _version):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runtime = home / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
            wrapper = home / ".local" / "bin" / "hermes"
            runtime.parent.mkdir(parents=True)
            wrapper.parent.mkdir(parents=True)
            runtime.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home), "PATH": str(wrapper.parent)}, clear=False):
                from agent_bridge_connect.setup import discover_hermes

                result = discover_hermes()
            self.assertTrue(result["found"])
            self.assertEqual(Path(result["path"]), runtime.resolve())

    def test_find_binary_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "tools" / "opencode"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\necho opencode custom\n", encoding="utf-8")
            binary.chmod(493)
            with mock.patch.dict(os.environ, {"AGENTBC_OPENCODE_BIN": str(binary), "PATH": ""}, clear=False):
                from agent_bridge_connect.path_provider import find_binary

                result = find_binary("opencode")
            self.assertTrue(result["found"])
            self.assertEqual(Path(result["path"]).resolve(), binary.resolve())
            self.assertEqual(result["source"], "env_override")

    def test_find_binary_common_npm_global_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            binary = home / ".npm-global" / "bin" / "opencode"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\necho opencode 1.2.3\n", encoding="utf-8")
            binary.chmod(493)
            with mock.patch.dict(os.environ, {"HOME": str(home), "PATH": ""}, clear=False):
                from agent_bridge_connect.path_provider import find_binary

                result = find_binary("opencode")
            self.assertTrue(result["found"])
            self.assertEqual(Path(result["path"]).resolve(), binary.resolve())
            self.assertEqual(result["source"], "npm")

    def test_find_binary_missing_reports_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp, "PATH": ""}, clear=False):
                from agent_bridge_connect.path_provider import find_binary

                result = find_binary("opencode")
            self.assertFalse(result["found"])
            self.assertIn("AGENTBC_OPENCODE_BIN=/your/path/opencode", result["manual_override"])
            self.assertTrue(result["searched_paths"])


class HermesSkillTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        shared = mock.patch("agent_bridge_connect.installation.shared_entries", return_value=())
        shared.start()
        self.addCleanup(shared.stop)
        self.skill_path = self.test_dir / "skills" / "agentbc" / "SKILL.md"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_install_hermes_skill_is_idempotent(self):
        from agent_bridge_connect.setup import install_hermes_skill

        first = install_hermes_skill(self.skill_path, interactive=False)
        second = install_hermes_skill(self.skill_path, interactive=False)
        self.assertTrue(first["installed"])
        self.assertTrue(first["changed"])
        self.assertTrue(second["installed"])
        self.assertFalse(second["changed"])
        text = self.skill_path.read_text(encoding="utf-8")
        contract_path = self.skill_path.parent / "references" / "controller-contract.md"
        contract = contract_path.read_text(encoding="utf-8")
        self.assertIn("agentbc runner status", text)
        self.assertIn("target_executor=hermes", text)
        self.assertIn("--dispatch", text)
        self.assertNotIn("--workspace /absolute/project/path", contract)
        self.assertIn('--customer-path "default path"', text)
        self.assertIn("--customer-path", contract)
        self.assertIn("steps[].description", contract)
        reference = self.skill_path.parent / "references" / "agentbc-steps-yaml.md"
        self.assertTrue(reference.is_file())
        self.assertTrue(contract_path.is_file())
        self.assertIn("Do not use `action`", reference.read_text(encoding="utf-8"))
        self.assertIn("默认 `target_executor=hermes`", text)
        self.assertIn("禁止根据任务类型", text)
        self.assertIn("stale_handoff_source", contract)
        self.assertIn("handoff_required", contract)
        self.assertIn("--branch", contract)
        self.assertTrue((self.skill_path.parent / ".agentbc-skill.json").is_file())

    def test_alpha_installs_hermes_skill_and_command_to_all_profiles(self):
        home = self.test_dir / "profile-home"
        (home / ".hermes" / "profiles" / "pm").mkdir(parents=True)
        (home / ".hermes" / "profiles" / "finance").mkdir(parents=True)
        (home / ".hermes" / "profiles" / ".archive").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home), "PATH": ""}, clear=True):
            from agent_bridge_connect.setup import install_hermes_skill

            result = install_hermes_skill(interactive=False)
        expected = {home / ".hermes" / "skills" / "agentbc" / "SKILL.md", home / ".hermes" / "profiles" / "pm" / "skills" / "agentbc" / "SKILL.md", home / ".hermes" / "profiles" / "finance" / "skills" / "agentbc" / "SKILL.md"}
        self.assertEqual({Path(path) for path in result["paths"]}, expected)
        self.assertTrue(all((path.is_file() for path in expected)))
        self.assertFalse((home / ".hermes" / "profiles" / ".archive" / "skills" / "agentbc" / "SKILL.md").exists())
        self.assertEqual(result["command"], "/agentbc")
        self.assertEqual(result["profile_scope"], "all")

    def test_codex_skill_does_not_invent_a_workspace(self):
        skill_path = Path(__file__).resolve().parents[2] / "src" / "agent_bridge_connect" / "skills" / "codex_skill.md"
        entry = skill_path.read_text(encoding="utf-8")
        text = " ".join((entry + (skill_path.parent / "references" / "controller-contract.md").read_text(encoding="utf-8")).split())
        self.assertIn("references/controller-contract.md", entry)
        self.assertNotIn("--workspace /absolute/project/path", text)
        self.assertIn('--customer-path "default path"', text)
        self.assertIn("Never invent a project path", text)
        self.assertIn("Runner rejects a path", text)
        self.assertIn("Do not copy a project or file", text)
        self.assertIn("A `pending` task is queued, not current", text)
        self.assertIn("After atomic dispatch", text)
        self.assertIn("do not request elevated", text)
        self.assertIn("task dispatch <task-id>", text)
        self.assertIn("current chain head", text)
        self.assertIn("stale_handoff_source", text)
        self.assertIn("fuzzy references", text)
        self.assertIn("task_code", text)
        self.assertIn("explicit task ID", text)
        self.assertIn("new task directory", text)
        self.assertIn("instead of handoff", text)
        self.assertIn("depends on, reviews, or modifies an existing task", text)
        self.assertIn("handoff_required", text)
        self.assertIn("project/artifact root", text)
        self.assertIn("before reading artifacts", text)
        self.assertIn("obsolete `--workspace` or `--output-dir`", text)
        self.assertIn("--branch", text)

    def test_public_codex_skill_matches_packaged_template(self):
        from agent_bridge_connect.setup import install_codex_skill

        repository_root = Path(__file__).resolve().parents[2]
        public_root = self.test_dir / "codex" / "skills" / "agentbc"
        result = install_codex_skill(public_root, interactive=False)
        public_skill = public_root / "SKILL.md"
        packaged_skill = repository_root / "src" / "agent_bridge_connect" / "skills" / "codex_skill.md"
        self.assertTrue(result["installed"])
        self.assertEqual(public_skill.read_text(encoding="utf-8"), packaged_skill.read_text(encoding="utf-8"))

    def test_claude_skill_documents_safe_l1_dispatch_rules(self):
        skill_path = Path(__file__).resolve().parents[2] / "src" / "agent_bridge_connect" / "skills" / "claude_skill.md"
        entry = skill_path.read_text(encoding="utf-8")
        text = " ".join((entry + (skill_path.parent / "references" / "controller-contract.md").read_text(encoding="utf-8")).split())
        self.assertTrue(entry.startswith("---\nname: agentbc\n"))
        self.assertIn("description:", entry.split("---", 2)[1])
        self.assertIn("agentbc task status", text)
        self.assertIn("bypassPermissions", text)
        self.assertIn("new root task", text)
        self.assertIn("stale_handoff_source", text)
        self.assertIn("depends on, reviews, or modifies an existing task", text)
        self.assertIn("handoff_required", text)
        self.assertIn("Claude is the controller, not the executor", text)
        self.assertIn("task handoff <confirmed-task-id> --to <target-executor>", text)
        self.assertIn("Do not edit files, generate artifacts, or complete that dispatched work inline", text)
        self.assertIn("top-level `steps:` list", text)
        self.assertIn("Do not use `.txt`", text)
        self.assertIn("cat > /tmp/agentbc-steps.yaml", text)
        self.assertIn("do not inspect AgentBC source code or CLI help", text)

    def test_install_claude_skill_is_idempotent(self):
        from agent_bridge_connect.setup import install_claude_skill

        destination = self.test_dir / "claude" / "skills" / "agentbc" / "SKILL.md"
        first = install_claude_skill(destination, interactive=False)
        second = install_claude_skill(destination, interactive=False)
        self.assertTrue(first["installed"])
        self.assertTrue(first["changed"])
        self.assertEqual(second["status"], "already_installed")
        text = destination.read_text(encoding="utf-8")
        self.assertIn("Claude is the controller, not the executor", text)
        self.assertIn("references/controller-contract.md", text)
        self.assertTrue((destination.parent / ".agentbc-skill.json").is_file())

    def test_uninstall_hermes_skill_removes_file(self):
        from agent_bridge_connect.setup import install_hermes_skill, uninstall_hermes_skill

        install_hermes_skill(self.skill_path, interactive=False)
        result = uninstall_hermes_skill(self.skill_path, interactive=False)
        self.assertTrue(result["removed"])
        self.assertFalse(self.skill_path.exists())

    def test_interactive_skill_install_requires_real_confirmation(self):
        from agent_bridge_connect.setup import install_hermes_skill

        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=EOFError):
                result = install_hermes_skill(self.skill_path, interactive=True)
        self.assertEqual(result["status"], "skipped_no_confirmation")
        self.assertFalse(self.skill_path.exists())


class DispatcherTraceabilitySkillTests(unittest.TestCase):
    """TRACE-001: packaged skills must document dispatcher traceability."""

    SKILLS = {"codex": "codex_skill.md", "claude": "claude_skill.md", "hermes": "hermes_skill.md"}
    EN = {"codex", "claude"}

    def _skill_text(self, platform: str) -> str:
        path = Path(__file__).resolve().parents[2] / "src" / "agent_bridge_connect" / "skills" / self.SKILLS[platform]
        return path.read_text(encoding="utf-8")

    def _contract_text(self) -> str:
        path = Path(__file__).resolve().parents[2] / "src" / "agent_bridge_connect" / "skills" / "references" / "controller-contract.md"
        return path.read_text(encoding="utf-8")

    def test_each_packaged_skill_passes_its_correct_source_platform_on_create(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                self.assertIn(f"--source-platform {platform}", text)
                self.assertIn("--customer-path", text)
                self.assertIn("--dispatch", text)

    def test_each_packaged_skill_passes_its_correct_source_platform_on_handoff(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                self.assertIn("task handoff", text)
                self.assertIn(f"--source-platform {platform}", text)

    def test_each_packaged_skill_documents_session_id_omission_rule(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                self.assertIn("--session-id", text)
                self.assertIn("unavailable", text)
                if platform in self.EN:
                    self.assertIn("trusted", text)
                else:
                    self.assertIn("可信", text)

    def test_each_packaged_skill_forbids_fabricated_session_ids(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                if platform in self.EN:
                    self.assertIn("Never infer", text)
                else:
                    self.assertIn("禁止", text)

    def test_each_packaged_skill_says_handoff_records_current_dispatcher_conversation(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = " ".join(self._contract_text().split())
                self.assertIn("current controller", text)
                self.assertIn("prior task", text)

    def test_each_packaged_skill_keeps_dispatcher_trace_separate_from_executor_sessions(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._contract_text()
                self.assertIn("Dispatcher sessions and executor temporary sessions are", text)
                self.assertIn("independent", text)

    def test_packaged_skills_have_no_hardcoded_trusted_id_examples(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                self.assertNotIn("--session-id 019f", text)
                self.assertNotIn('--session-id "019f', text)
                self.assertNotIn("--session-id '019f", text)


class IdempotentCodexSkillTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        shared = mock.patch("agent_bridge_connect.installation.shared_entries", return_value=())
        shared.start()
        self.addCleanup(shared.stop)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_install_codex_skill_is_idempotent(self):
        from agent_bridge_connect.setup import install_codex_skill

        root = self.test_dir / "codex" / "skills" / "agentbc"
        first = install_codex_skill(root, interactive=False)
        second = install_codex_skill(root, interactive=False)
        self.assertTrue(first["installed"])
        self.assertTrue(first["changed"])
        self.assertEqual(second["status"], "already_installed")
        skill = root / "SKILL.md"
        self.assertTrue(skill.is_file())
        text = skill.read_text(encoding="utf-8")
        self.assertIn("--source-platform codex", text)
        self.assertIn("--session-id", text)
        self.assertIn("references/controller-contract.md", text)
        self.assertTrue((root / "agents" / "openai.yaml").is_file())
        self.assertTrue((root / "references" / "controller-contract.md").is_file())
        self.assertTrue((root / ".agentbc-skill.json").is_file())


class SetupModeTests(unittest.TestCase):
    def test_hermes_setup_uses_native_acp_permission_transport(self):
        from agent_bridge_connect.setup import _executor_config_for

        config = _executor_config_for({"name": "hermes", "path": str(self.test_dir / "bin" / "hermes"), "binary": "hermes", "source": "test", "capability_level": "L2", "version": "test"})
        self.assertEqual(config["transport"], "acp")
        self.assertFalse(config["quiet"])

    def test_claude_executor_config_defaults_to_safe_l1_runner(self):
        from agent_bridge_connect.setup import _executor_config_for

        config = _executor_config_for({"name": "claude", "path": str(self.test_dir / "bin" / "claude"), "binary": "claude", "source": "test", "capability_level": "L1", "version": "2.1.186"})
        self.assertEqual(config["transport"], "runner")
        self.assertTrue(config["safe_mode"])
        self.assertEqual(config["permission_mode"], "acceptEdits")
        self.assertEqual(config["output_format"], "text")
        self.assertEqual(config["tools"], ["Read", "Write", "Edit", "Bash"])
        self.assertEqual(config["auto_approve_tools"], [])
        self.assertNotIn("allowed_tools", config)

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        shared = mock.patch("agent_bridge_connect.installation.shared_entries", return_value=())
        shared.start()
        self.addCleanup(shared.stop)
        self.home = self.test_dir / "home"
        self.home.mkdir()
        self.config_path = self.test_dir / "config.toml"
        self.skill_path = self.test_dir / "hermes" / "agentbc" / "SKILL.md"
        self.claude_skill_path = self.test_dir / "claude" / "agentbc" / "SKILL.md"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def env(self):
        return {"HOME": str(self.home), "PATH": "", "AGENTBC_CONFIG_PATH": str(self.config_path), "AGENTBC_HERMES_SKILL_PATH": str(self.skill_path), "AGENTBC_CLAUDE_SKILL_PATH": str(self.claude_skill_path)}

    def _seed_uninstall_tree(self):
        workspace = self.home / "Documents" / "AgentBC" / "workspace"
        board = workspace / "record"
        report_root = workspace / "tasks" / "report"
        artifact_root = workspace / "tasks" / "artifacts"
        install_root = self.home / ".agentbc-alpha"
        bin_dir = self.home / ".local" / "bin"
        customer = self.home / "customer-project"
        for path in (
            artifact_root,
            report_root,
            board,
            install_root / "venv" / "bin",
            bin_dir,
            self.home / ".hermes" / "skills" / "agentbc",
            self.home / ".hermes" / "profiles" / "pm" / "skills" / "agentbc",
            self.home / ".claude" / "skills" / "agentbc",
            self.home / ".codex" / "skills" / "agentbc",
            customer,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(f'board_root = "{board}"\nworkspace_root = "{workspace}"\n', encoding="utf-8")
        target = install_root / "venv" / "bin" / "agentbc"
        target.write_text("binary", encoding="utf-8")
        (bin_dir / "agentbc").symlink_to(target)
        (bin_dir / "abc").write_text("# AgentBC-owned abc shim\n", encoding="utf-8")
        (report_root / "record.md").write_text("report", encoding="utf-8")
        (artifact_root / "artifact.txt").write_text("artifact", encoding="utf-8")
        (board / "task.json").write_text("{}", encoding="utf-8")
        (customer / "keep.txt").write_text("keep", encoding="utf-8")
        return (workspace, board, install_root, customer)

    def test_uninstall_prompts_for_records_and_artifacts_independently(self):
        (workspace, board, install_root, customer) = self._seed_uninstall_tree()
        env = {"HOME": str(self.home), "PATH": "", "AGENTBC_CONFIG_PATH": str(self.config_path), "AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1", "HERMES_HOME": str(self.home / ".hermes")}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("builtins.input", side_effect=["y", "n"]):
                from agent_bridge_connect.setup import run_uninstall

                result = run_uninstall(interactive=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["remove_records"])
        self.assertFalse(result["remove_artifacts"])
        self.assertFalse(board.exists())
        self.assertTrue((workspace / "tasks" / "artifacts" / "artifact.txt").is_file())
        self.assertFalse(install_root.exists())
        self.assertFalse(self.config_path.exists())
        self.assertFalse((self.home / ".local" / "bin" / "agentbc").exists())
        self.assertFalse((self.home / ".local" / "bin" / "abc").exists())
        self.assertFalse((self.home / ".hermes" / "skills" / "agentbc").exists())
        self.assertFalse((self.home / ".hermes" / "profiles" / "pm" / "skills" / "agentbc").exists())
        self.assertFalse((self.home / ".claude" / "skills" / "agentbc").exists())
        self.assertFalse((self.home / ".codex" / "skills" / "agentbc").exists())
        self.assertTrue((customer / "keep.txt").is_file())

    def test_uninstall_can_keep_records_and_remove_default_artifacts(self):
        (workspace, board, install_root, customer) = self._seed_uninstall_tree()
        env = {"HOME": str(self.home), "PATH": "", "AGENTBC_CONFIG_PATH": str(self.config_path), "AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_uninstall

            result = run_uninstall(interactive=False, remove_records=False, remove_artifacts=True)
        self.assertFalse(result["remove_records"])
        self.assertTrue(result["remove_artifacts"])
        self.assertTrue((board / "task.json").is_file())
        self.assertTrue((workspace / "tasks" / "report" / "record.md").is_file())
        self.assertFalse((workspace / "tasks" / "artifacts").exists())
        self.assertFalse(install_root.exists())
        self.assertTrue((customer / "keep.txt").is_file())

    def test_noninteractive_uninstall_requires_both_data_choices(self):
        (workspace, board, install_root, customer) = self._seed_uninstall_tree()
        env = {"HOME": str(self.home), "PATH": "", "AGENTBC_CONFIG_PATH": str(self.config_path), "AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_uninstall

            with self.assertRaisesRegex(ValueError, "requires an explicit records choice"):
                run_uninstall(interactive=False, remove_records=True, remove_artifacts=None)
        self.assertTrue(board.exists())
        self.assertTrue((workspace / "tasks" / "artifacts" / "artifact.txt").is_file())
        self.assertTrue(install_root.exists())
        self.assertTrue((customer / "keep.txt").is_file())

    def test_uninstall_removes_complete_default_managed_root_when_both_choices_are_yes(self):
        (workspace, board, install_root, customer) = self._seed_uninstall_tree()
        legacy = workspace / "agentbc_tasks" / "legacy.txt"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("old alpha residue", encoding="utf-8")
        env = {"HOME": str(self.home), "PATH": "", "AGENTBC_CONFIG_PATH": str(self.config_path), "AGENTBC_ALPHA_HOME": str(install_root), "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"), "AGENTBC_UNINSTALL_SKIP_RUNNER": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_uninstall

            result = run_uninstall(interactive=False, remove_records=True, remove_artifacts=True)
        self.assertTrue(result["ok"])
        self.assertFalse((self.home / "Documents" / "AgentBC").exists())
        self.assertTrue((customer / "keep.txt").is_file())

    def test_scan_all_agents_lists_known_agents(self):
        with mock.patch.dict(os.environ, self.env(), clear=False):
            from agent_bridge_connect.setup import scan_all_agents

            agents = scan_all_agents()
        names = {agent["name"] for agent in agents}
        self.assertGreaterEqual(names, {"codex", "hermes", "claude", "opencode", "cursor", "gemini"})

    def test_setup_help_exposes_setup_modes(self):
        from agent_bridge_connect.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                main(["setup", "--help"])
        self.assertEqual(caught.exception.code, 0)
        text = output.getvalue()
        self.assertIn("--show", text)
        self.assertIn("--update", text)
        self.assertIn("--clean", text)

    def test_run_setup_noninteractive_writes_codex_config_only(self):
        codex = self.home / ".local" / "bin" / "codex"
        codex.parent.mkdir(parents=True)
        codex.write_text("#!/bin/sh\necho codex test\n", encoding="utf-8")
        codex.chmod(493)
        env = {**self.env(), "AGENTBC_CODEX_BIN": str(codex)}
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_setup

            with contextlib.redirect_stdout(io.StringIO()):
                result = run_setup(interactive=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["enabled"], ["codex"])
        self.assertEqual(result["workspace_root"], str((self.home / "Documents" / "AgentBC" / "workspace").resolve()))
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn("workspace_root", text)
        self.assertIn("[executors.codex]", text)
        self.assertFalse(self.skill_path.exists())

    def test_hermes_skill_path_uses_hermes_home_contract(self):
        path_env = {"HOME": str(self.home), "PATH": ""}
        with mock.patch.dict(os.environ, path_env, clear=True):
            from agent_bridge_connect.setup import _hermes_skill_path

            self.assertEqual(_hermes_skill_path(), self.home / ".hermes" / "skills" / "agentbc" / "SKILL.md")
        custom_home = self.test_dir / "custom-hermes-home"
        with mock.patch.dict(os.environ, {**path_env, "HERMES_HOME": str(custom_home)}, clear=True):
            self.assertEqual(_hermes_skill_path(), custom_home / "skills" / "agentbc" / "SKILL.md")

    def test_run_setup_installs_both_detected_skills_and_refreshes_state(self):
        agents_before = [
            {"name": "hermes", "display": "Hermes Agent", "found": True, "supported_executor": True, "path": str(self.test_dir / "bin" / "hermes"), "binary": "hermes", "version": "test", "source": "test", "capability_level": "L2", "capabilities": {}, "skill": {"installed": False}},
            {"name": "claude", "display": "Claude Code", "found": True, "supported_executor": True, "path": str(self.test_dir / "bin" / "claude"), "binary": "claude", "version": "test", "source": "test", "capability_level": "L1", "capabilities": {}, "skill": {"installed": False}},
        ]
        agents_after = [{**agent, "enabled": True, "skill": {"installed": True, "up_to_date": True}} for agent in agents_before]
        hermes_result = {"installed": True, "changed": True, "status": "installed", "path": "/h/SKILL.md"}
        claude_result = {"installed": True, "changed": True, "status": "installed", "path": "/c/SKILL.md"}
        with mock.patch.dict(os.environ, self.env(), clear=False):
            from agent_bridge_connect import setup

            with (
                mock.patch.object(setup, "scan_all_agents", side_effect=[agents_before, agents_after]),
                mock.patch.object(setup, "_print_scan_report"),
                mock.patch.object(setup, "_confirm", return_value=True),
                mock.patch.object(setup, "install_hermes_skill", return_value=hermes_result) as install_hermes,
                mock.patch.object(setup, "install_claude_skill", return_value=claude_result) as install_claude,
                mock.patch.object(setup, "_configure_alias", return_value={"status": "skipped"}),
                mock.patch.object(setup, "_select_claude_budget", return_value=(10.0, True, "test_default")),
                mock.patch.object(setup, "_select_hermes_max_turns", return_value=(60, True, "test_default")),
                mock.patch.object(setup, "_select_session_retention", return_value=(False, True, "test_default")),
                mock.patch("agent_bridge_connect.web_service.select_settings", return_value={"preferred_view":"task_list","autostart":False}),
                mock.patch.object(setup, "discover_codex", return_value={"found": False}),
                mock.patch.object(setup, "probe_codex", return_value={}),
            ):
                result = setup.run_setup(interactive=True, permission_mode="safe")
        install_hermes.assert_called_once_with(interactive=True)
        install_claude.assert_called_once_with(interactive=True)
        self.assertEqual(result["skills"]["hermes"], hermes_result)
        self.assertEqual(result["skills"]["claude"], claude_result)
        self.assertTrue(all((agent["skill"]["installed"] for agent in result["agents"])))

    def test_run_setup_interactively_installs_both_skill_files(self):
        fake_bin = self.test_dir / "bin"
        fake_bin.mkdir()
        hermes = fake_bin / "hermes"
        claude = fake_bin / "claude"
        hermes.write_text("#!/bin/sh\necho 'Hermes Agent test'\n", encoding="utf-8")
        claude.write_text("#!/bin/sh\nif [ \"${1:-}\" = '--help' ]; then\n  echo '--print --safe-mode text json stream-json acceptEdits'\nelse\n  echo 'Claude Code test'\nfi\n", encoding="utf-8")
        hermes.chmod(493)
        claude.chmod(493)
        env = {**self.env(), "AGENTBC_HERMES_BIN": str(hermes), "AGENTBC_CLAUDE_BIN": str(claude)}
        with mock.patch.dict(os.environ, env, clear=True):
            from agent_bridge_connect import setup

            with mock.patch("agent_bridge_connect.web_service.select_settings", return_value={"preferred_view":"task_list","autostart":False}), contextlib.redirect_stdout(io.StringIO()), mock.patch("builtins.input", side_effect=["safe", "y", "y", "1", "y", "1", "n", *["y"] * 10]), mock.patch.object(setup, "_configure_alias", return_value={"status": "skipped"}):
                result = setup.run_setup(interactive=True)
        self.assertTrue(self.skill_path.is_file())
        self.assertTrue(self.claude_skill_path.is_file())
        self.assertEqual(result["skills"]["hermes"]["status"], "installed")
        self.assertEqual(result["skills"]["claude"]["status"], "installed")
        states = {agent["name"]: agent.get("skill") for agent in result["agents"]}
        self.assertTrue(states["hermes"]["installed"])
        self.assertTrue(states["claude"]["installed"])

    def test_run_show_does_not_write_config_or_skill(self):
        with mock.patch.dict(os.environ, self.env(), clear=False):
            from agent_bridge_connect.setup import run_show

            with contextlib.redirect_stdout(io.StringIO()):
                result = run_show()
        self.assertTrue(result["ok"])
        self.assertEqual(result["workspace_root"], str((self.home / "Documents" / "AgentBC" / "workspace").resolve()))
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.skill_path.exists())
        claude = next((agent for agent in result["agents"] if agent["name"] == "claude"))
        self.assertIn("skill", claude)
        self.assertFalse(claude["skill"]["installed"])


_hermes_setup_FIXTURES = _hermes_setup_Path(__file__).parents[1] / "fixtures"
_hermes_setup_STEPS_YAML = _hermes_setup_FIXTURES / "sample_steps.yaml"


class _hermes_setup_HermesExecutorTests(_hermes_setup_unittest.TestCase):
    """Test HermesExecutor adapter."""

    @_hermes_setup_mock.patch("agent_bridge_connect.executors.hermes.subprocess.run")
    def test_hermes_probe(self, run):
        """HermesExecutor.probe() should check hermes is available."""
        from agent_bridge_connect.executors.hermes import HermesExecutor

        run.return_value = _hermes_setup_mock.Mock(returncode=0, stdout="Hermes test", stderr="")
        executor = HermesExecutor(command=_hermes_setup_sys.executable, transport="direct")
        result = executor.probe()
        self.assertTrue(result.ok)
        self.assertIn("agent_bin", result.details)
        self.assertEqual(result.details["agent_bin"], _hermes_setup_sys.executable)
        self.assertEqual(result.details["agent_bin_source"], "configured")
        self.assertEqual(result.details["profile_mode"], "inherit")
        self.assertEqual(result.details["auth_owner"], "hermes_cli")

    def test_hermes_probe_not_found_has_stable_path_diagnostics(self):
        from agent_bridge_connect.executors.hermes import HermesExecutor

        missing = {"found": False, "path": "", "source": "not_found", "searched_paths": ["fixture/hermes"], "manual_override": "AGENTBC_HERMES_BIN=/your/path/hermes"}
        with _hermes_setup_mock.patch("agent_bridge_connect.executors.hermes._discover_hermes_binary", return_value=missing):
            result = HermesExecutor().probe()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["agent_bin"], "")
        self.assertEqual(result.details["agent_bin_source"], "not_found")
        self.assertEqual(result.details["searched_paths"], ["fixture/hermes"])
        self.assertIn("AGENTBC_HERMES_BIN", result.details["manual_override"])

    def test_hermes_capabilities(self):
        """HermesExecutor should declare L2 capabilities."""
        from agent_bridge_connect.executors.hermes import HermesExecutor
        from agent_bridge_connect.adapters import ExecutorLevel

        executor = HermesExecutor()
        caps = executor.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.model_selection)
        self.assertTrue(caps.multimodal)
        self.assertGreaterEqual(caps.level, ExecutorLevel.L2)

    def test_hermes_extensions(self):
        """HermesExecutor should store metadata under extensions.executor.hermes."""
        from agent_bridge_connect.executors.hermes import HermesExecutor

        executor = HermesExecutor(command=_hermes_setup_sys.executable)
        executor._version = "Hermes test"
        details = executor.get_extensions()["executor"]["hermes"]
        self.assertEqual(details["agent_bin"], _hermes_setup_sys.executable)
        self.assertEqual(details["agent_bin_source"], "configured")


class _hermes_setup_VSCodeCapabilityTests(_hermes_setup_unittest.TestCase):
    """Test VSCode/Cursor/Windsurf runtime detection."""

    def test_runtime_capability_grades(self):
        """Should define all 5 capability grades."""
        from agent_bridge_connect.setup import RuntimeCapability

        self.assertEqual(RuntimeCapability.FRONTEND_ONLY, "frontend_only")
        self.assertEqual(RuntimeCapability.BACKGROUND_SINGLE, "background_single")
        self.assertEqual(RuntimeCapability.BACKGROUND_MULTI_CANDIDATE, "background_multi_candidate")
        self.assertEqual(RuntimeCapability.BACKGROUND_MULTI_VERIFIED, "background_multi_verified")
        self.assertEqual(RuntimeCapability.SILENT_UNATTENDED, "silent_unattended")

    def test_setup_outputs_capability_matrix(self):
        """Setup should return a capability matrix with all fields."""
        from agent_bridge_connect.setup import probe_codex

        with _hermes_setup_mock.patch("agent_bridge_connect.setup.discover_codex", return_value={"found": False, "version": ""}):
            result = probe_codex()
        self.assertIn("has_json_output", result)
        self.assertIn("has_sandbox", result)


class _hermes_setup_GateFTests(_hermes_setup_unittest.TestCase):
    """Gate F: HermesExecutor completes a real task."""

    def setUp(self):
        self.test_dir = _hermes_setup_Path(_hermes_setup_tempfile.mkdtemp())
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.test_dir)
        self.task = create_task("Gate F test", "hermes", _hermes_setup_STEPS_YAML, self.test_dir)
        self.task_id = self.task.id

    def tearDown(self):
        _hermes_setup_shutil.rmtree(self.test_dir)

    def test_gate_f_lifecycle(self):
        """HermesExecutor: claim → execute → complete → report."""
        from agent_bridge_connect.service import TaskService
        from tests.contract_helpers import finalize_completed

        svc = TaskService(self.test_dir)
        svc.claim_task(self.task_id, "hermes")
        svc.execute_step(self.task_id, 1, {"status": "done", "result": "reviewed"})
        finalize_completed(svc, self.task_id)
        task = svc.get_task(self.task_id)
        self.assertEqual(task.status, "completed")
        report = svc.generate_report(self.task_id)
        self.assertIsNotNone(report)


class DockerHostEntryTests(unittest.TestCase):
    def test_cli_delivery_forwards_without_grant_or_desktop_success(self):
        from agent_bridge_connect.docker_host import run
        with mock.patch('agent_bridge_connect.docker_host.load_profile', return_value={'container_id': 'exact', 'approval_delivery': 'cli'}), mock.patch('agent_bridge_connect.docker_host.inspect_container', return_value={'Mounts': []}), mock.patch('agent_bridge_connect.docker_host.route_arguments', return_value=['task', 'status', 'TEST-001']), mock.patch('agent_bridge_connect.docker_host.start_bridge') as bridge, mock.patch('agent_bridge_connect.docker_host.subprocess.run', return_value=mock.Mock(returncode=0)) as execute:
            self.assertEqual(run(Path('/private/profile.json'), ['task', 'status', 'TEST-001']), 0)
            bridge.assert_not_called()
            self.assertEqual(execute.call_args.args[0][-4:], ['agentbc', 'task', 'status', 'TEST-001'])

    def test_invalid_delivery_cannot_silently_disable_desktop(self):
        from agent_bridge_connect.docker_host import install
        with self.assertRaises(ValueError):
            install(Path('/unused'), Path('/unused-home'), 'unused', approval_delivery='disabled')

    def test_maps_only_existing_writable_project_mount(self):
        from agent_bridge_connect.docker_host import map_project
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / '工程 with spaces'
            project.mkdir()
            mounts = [{'Type': 'bind', 'RW': True, 'Source': str(project), 'Destination': '/project'}]
            self.assertEqual(map_project(str(project), mounts), '/project')
            self.assertEqual(map_project('default path', []), 'default path')
            with self.assertRaises(ValueError):
                map_project(str(root), mounts)
            mounts[0]['RW'] = False
            with self.assertRaises(ValueError):
                map_project(str(project), mounts)

    def test_routing_preserves_argument_boundaries_and_maps_both_forms(self):
        from agent_bridge_connect.docker_host import route_arguments
        with tempfile.TemporaryDirectory() as tmp:
            steps = Path(tmp) / '中文 steps.yaml'
            steps.write_text('steps: []')
            stage = mock.Mock(return_value='/data/input.yaml')
            result = route_arguments(['task', 'create', '--title', 'literal $(bad); string',
                                      f'--steps={steps}', '--customer-path', 'default path'], [], stage)
            self.assertEqual(result, ['task', 'create', '--title', 'literal $(bad); string',
                                      '--steps', '/data/input.yaml', '--customer-path', 'default path',
                                      '--root', '/data/workspace/record', '--config', '/home/agentbc/.abc/config.toml'])
            stage.assert_called_once_with(steps.resolve(), '--steps')

    def test_native_lifecycle_and_host_config_never_fall_back(self):
        from agent_bridge_connect.docker_host import route_arguments
        for argv in (['setup'], ['uninstall'], ['update'], ['webui'], ['runner', 'start'],
                     ['task', 'list', '--config', '/host/config.toml'], ['task', 'list', '--root=/host']):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                route_arguments(argv, [], mock.Mock())

    def test_install_is_isolated_and_preserves_conflicting_entry(self):
        from agent_bridge_connect.docker_host import install
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = {'Id': 'a' * 64, 'Name': '/test'}
            with mock.patch('agent_bridge_connect.docker_host.inspect_container', return_value=info):
                result = install(root / 'prefix', root / 'home', 'test')
                self.assertEqual(len(result['skills']), 3)
                self.assertEqual((root / 'prefix/docker-host.json').stat().st_mode & 0o777, 0o600)
                entry = Path(result['entry'])
                previous = entry.read_bytes()
                with self.assertRaises(ValueError):
                    install(root / 'prefix', root / 'home', 'test')
                self.assertEqual(entry.read_bytes(), previous)

    def test_modified_skill_aborts_before_creating_cli(self):
        from agent_bridge_connect.docker_host import install
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / 'home/.codex/skills/agentbc/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('user custom instructions')
            with mock.patch('agent_bridge_connect.docker_host.inspect_container', return_value={'Id': 'a' * 64, 'Name': '/test'}):
                with self.assertRaises(ValueError):
                    install(root / 'prefix', root / 'home', 'test')
            self.assertFalse((root / 'prefix').exists())
            self.assertEqual(skill.read_text(), 'user custom instructions')

    def test_profile_rejects_replacement_and_world_readable_file(self):
        import json
        from agent_bridge_connect.docker_host import load_profile
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / 'profile.json'
            profile.write_text(json.dumps({'schema': 1, 'container_id': 'original'}))
            profile.chmod(0o644)
            with self.assertRaises(ValueError):
                load_profile(profile)
            profile.chmod(0o600)
            with mock.patch('agent_bridge_connect.docker_host.inspect_container', return_value={'Id': 'replacement'}):
                with self.assertRaises(ValueError):
                    load_profile(profile)

    def test_intact_native_skill_can_be_migrated_to_docker_binding(self):
        from agent_bridge_connect.docker_host import install
        from agent_bridge_connect.setup import install_codex_skill
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'home/.codex/skills/agentbc'
            install_codex_skill(target, interactive=False)
            with mock.patch('agent_bridge_connect.docker_host.inspect_container', return_value={'Id': 'a' * 64, 'Name': '/test'}):
                install(root / 'prefix', root / 'home', 'test')
            self.assertIn('Docker host binding', (target / 'SKILL.md').read_text())


class DockerWebUIBindingTests(unittest.TestCase):
    def fixtures(self):
        runner = {'Id': 'runner-id', 'Config': {'Labels': {'com.docker.compose.project': 'test'}},
                  'Mounts': [{'Destination': '/data/workspace', 'Source': '/volumes/data', 'RW': True}]}
        webui = {'Id': 'webui-id', 'Config': {'Labels': {'com.docker.compose.project': 'test', 'com.docker.compose.service': 'webui'}},
                 'Mounts': [{'Destination': '/data/workspace', 'Source': '/volumes/data', 'RW': False}],
                 'HostConfig': {'ReadonlyRootfs': True, 'PortBindings': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18080'}]}},
                 'State': {'Running': False}}
        return runner, webui

    def test_binding_requires_same_readonly_data_and_loopback_port(self):
        import copy
        from agent_bridge_connect.docker_webui import binding
        runner, webui = self.fixtures()
        self.assertEqual(binding(runner, webui)['origin'], 'http://127.0.0.1:18080')
        variants = []
        other = copy.deepcopy(webui)
        other['Mounts'][0]['RW'] = True
        variants.append(other)
        other = copy.deepcopy(webui)
        other['Mounts'][0]['Source'] = '/other/data'
        variants.append(other)
        other = copy.deepcopy(webui)
        other['Mounts'].append({'Destination': '/profiles', 'Source': '/secrets'})
        variants.append(other)
        other = copy.deepcopy(webui)
        other['HostConfig']['PortBindings']['8080/tcp'][0]['HostIp'] = '0.0.0.0'
        variants.append(other)
        other = copy.deepcopy(webui)
        other['Config']['Labels']['com.docker.compose.project'] = 'other'
        variants.append(other)
        for other in variants:
            with self.assertRaises(ValueError):
                binding(runner, other)

    def test_close_is_idempotent_and_only_targets_bound_webui(self):
        from agent_bridge_connect.docker_webui import command
        _, info = self.fixtures()
        saved = {'container_id': 'webui-id', 'port': 18080, 'origin': 'http://127.0.0.1:18080'}
        with mock.patch('agent_bridge_connect.docker_webui.verify', return_value=(saved, info)), mock.patch('agent_bridge_connect.docker_webui.execute') as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(command({}, ['close']), 0)
            execute.assert_not_called()
            info['State']['Running'] = True
            self.assertEqual(command({}, ['close']), 0)
            execute.assert_called_once_with(['docker', 'stop', '--time', '5', 'webui-id'])

    def test_status_does_not_read_or_print_token(self):
        from agent_bridge_connect.docker_webui import command
        _, info = self.fixtures()
        saved = {'container_id': 'webui-id', 'port': 18080, 'origin': 'http://127.0.0.1:18080'}
        output = io.StringIO()
        with mock.patch('agent_bridge_connect.docker_webui.verify', return_value=(saved, info)), mock.patch('agent_bridge_connect.docker_webui.launch_url') as launch, contextlib.redirect_stdout(output):
            self.assertEqual(command({}, ['status']), 0)
            launch.assert_not_called()
        self.assertNotIn('token', output.getvalue())
        self.assertIn('18080', output.getvalue())

    def test_windows_reserved_port_fails_without_start_or_port_change(self):
        import json
        from agent_bridge_connect import docker_webui as web
        _, info = self.fixtures()
        info['State']['Health'] = {'Status': 'starting'}
        saved = {'container_id': 'webui-id', 'port': 50757, 'origin': 'http://127.0.0.1:50757'}
        with mock.patch.object(web.sys, 'platform', 'win32'), mock.patch.object(web.socket, 'socket') as sock, mock.patch.object(web, 'verify', return_value=(saved, info)), mock.patch.object(web, 'execute') as execute:
            sock.return_value.__enter__.return_value.bind.side_effect = PermissionError(13, 'denied')
            with self.assertRaisesRegex(RuntimeError, '50757.*reserved'):
                web.command({}, ['start'])
            execute.assert_not_called()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                web.command({}, ['status'])
            value = json.loads(output.getvalue())
            self.assertEqual(value['health'], 'stopped')
            self.assertIn('agentbc setup', value['error'])
            self.assertEqual(saved['port'], 50757)

    def test_windows_port_selection_skips_unavailable_ports(self):
        from agent_bridge_connect import docker_install_setup as setup
        with mock.patch.object(setup.sys, 'platform', 'win32'), mock.patch.object(setup.random, 'SystemRandom') as random, mock.patch.object(setup.socket, 'socket') as sock:
            random.return_value.sample.return_value = [18001, 18002]
            sock.return_value.__enter__.return_value.bind.side_effect = [PermissionError(13, 'reserved'), None]
            self.assertEqual(setup.free_port(), 18002)
            random.return_value.sample.assert_called_with(range(18000, 48000), 256)
            sock.return_value.__enter__.return_value.bind.side_effect = PermissionError(13, 'denied')
            with self.assertRaisesRegex(setup.InstallationError, 'No available fixed WebUI port'):
                setup.free_port()

    def test_launch_receipt_rejects_foreign_origin(self):
        from agent_bridge_connect.docker_webui import launch_url
        with mock.patch('agent_bridge_connect.docker_webui.execute', return_value='http://evil.invalid/#token=' + 'x' * 43):
            with self.assertRaises(ValueError):
                launch_url({'container_id': 'webui-id', 'origin': 'http://127.0.0.1:18080'})

    def test_webui_route_never_starts_approval_bridge_or_native_runner(self):
        from agent_bridge_connect.docker_host import run
        with mock.patch('agent_bridge_connect.docker_host.load_profile', return_value={}), mock.patch('agent_bridge_connect.docker_webui.command', return_value=0) as command, mock.patch('agent_bridge_connect.docker_host.start_bridge') as bridge:
            self.assertEqual(run(Path('/unused'), ['webui', 'close']), 0)
            command.assert_called_once_with({}, ['close'])
            bridge.assert_not_called()


    def test_webui_profile_remains_manageable_when_runner_is_stopped(self):
        import json
        from agent_bridge_connect.docker_host import load_profile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            path.write_text(json.dumps({'schema': 1, 'container_id': 'runner-id'}))
            path.chmod(0o600)
            with mock.patch('agent_bridge_connect.docker_host.execute', return_value=json.dumps([{'Id': 'runner-id', 'State': {'Running': False}}])):
                self.assertEqual(load_profile(path, require_running=False)['container_id'], 'runner-id')
