"""Phase 10c tests: setup PathProvider, Hermes Skill, and setup modes."""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "PATH": str(wrapper.parent)},
                clear=False,
            ):
                from agent_bridge_connect.setup import discover_hermes

                result = discover_hermes()

            self.assertTrue(result["found"])
            self.assertEqual(Path(result["path"]), runtime.resolve())

    def test_find_binary_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "tools" / "opencode"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\necho opencode custom\n", encoding="utf-8")
            binary.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {"AGENTBC_OPENCODE_BIN": str(binary), "PATH": ""},
                clear=False,
            ):
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
            binary.chmod(0o755)

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
        self.assertIn("agentbc runner status", text)
        self.assertIn("--assignee hermes", text)
        self.assertIn("--dispatch", text)
        self.assertNotIn("--workspace /absolute/project/path", text)
        self.assertIn('--customer-path "default path"', text)
        self.assertIn("--customer-path", text)
        self.assertIn("steps[].description", text)
        reference = self.skill_path.parent / "references" / "agentbc-steps-yaml.md"
        self.assertTrue(reference.is_file())
        self.assertIn("Do not use `action`", reference.read_text(encoding="utf-8"))
        self.assertIn("禁止根据任务标题", text)
        self.assertIn("Runner 路径错误", text)
        self.assertIn("禁止通过修改", text)
        self.assertIn("pending", text)
        self.assertIn("不是当前任务", text)
        self.assertIn("--step 1", text)
        self.assertIn("当前 chain head", text)
        self.assertIn("stale_handoff_source", text)
        self.assertIn("最新任务", text)
        self.assertIn("task code", text)
        self.assertIn("project/artifact root", text)
        self.assertIn("用户确认", text)
        self.assertIn("默认 `target_executor=hermes`", text)
        self.assertIn("禁止根据任务类型", text)
        self.assertIn("新开任务目录", text)
        self.assertIn("禁止使用 `handoff`", text)
        self.assertIn("禁止读取产物文件", text)
        self.assertIn("路径计划", text)
        self.assertIn("不要把这种需求改写成", text)
        self.assertIn("既有 AgentBC 任务的产物", text)
        self.assertIn("handoff_required", text)
        self.assertIn("唯一产物根", text)
        self.assertIn("--branch", text)

    def test_alpha_installs_hermes_skill_and_command_to_all_profiles(self):
        home = self.test_dir / "profile-home"
        (home / ".hermes" / "profiles" / "pm").mkdir(parents=True)
        (home / ".hermes" / "profiles" / "finance").mkdir(parents=True)
        (home / ".hermes" / "profiles" / ".archive").mkdir(parents=True)

        with mock.patch.dict(os.environ, {"HOME": str(home), "PATH": ""}, clear=True):
            from agent_bridge_connect.setup import install_hermes_skill

            result = install_hermes_skill(interactive=False)

        expected = {
            home / ".hermes" / "skills" / "agentbc" / "SKILL.md",
            home / ".hermes" / "profiles" / "pm" / "skills" / "agentbc" / "SKILL.md",
            home / ".hermes" / "profiles" / "finance" / "skills" / "agentbc" / "SKILL.md",
        }
        self.assertEqual({Path(path) for path in result["paths"]}, expected)
        self.assertTrue(all(path.is_file() for path in expected))
        self.assertFalse(
            (home / ".hermes" / "profiles" / ".archive" / "skills" / "agentbc" / "SKILL.md").exists()
        )
        self.assertEqual(result["command"], "/agentbc")
        self.assertEqual(result["profile_scope"], "all")

    def test_codex_skill_does_not_invent_a_workspace(self):
        skill_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_bridge_connect"
            / "skills"
            / "codex_skill.md"
        )
        text = skill_path.read_text(encoding="utf-8")

        self.assertNotIn("--workspace /absolute/project/path", text)
        self.assertIn('--customer-path "default path"', text)
        self.assertIn("Never invent a project path", text)
        self.assertIn("Runner path error", text)
        self.assertIn("Do not retry by changing", text)
        self.assertIn("A `pending` task is queued, not current", text)
        self.assertIn("atomically create and dispatch", text)
        self.assertIn("Do not request elevated", text)
        self.assertIn("task dispatch <task-id>", text)
        self.assertIn("current chain head", text)
        self.assertIn("stale_handoff_source", text)
        self.assertIn("latest task", text)
        self.assertIn("task_code", text)
        self.assertIn("provides an explicit task ID", text)
        self.assertIn("new task directory", text)
        self.assertIn("do not use `handoff`", text)
        self.assertIn("depends on, reviews, or modifies deliverables", text)
        self.assertIn("handoff_required", text)
        self.assertIn("only deliverable root", text)
        self.assertIn("hard stop", text)
        self.assertIn("`--workspace`, `--output-dir`, and manual `--customer-dir` decisions are obsolete", text)
        self.assertIn("--branch", text)

    def test_public_codex_skill_matches_packaged_template(self):
        from agent_bridge_connect.setup import install_codex_skill

        repository_root = Path(__file__).resolve().parents[1]
        public_root = self.test_dir / "codex" / "skills" / "agentbc"
        result = install_codex_skill(public_root, interactive=False)
        public_skill = public_root / "SKILL.md"
        packaged_skill = (
            repository_root
            / "src"
            / "agent_bridge_connect"
            / "skills"
            / "codex_skill.md"
        )

        self.assertTrue(result["installed"])
        self.assertEqual(
            public_skill.read_text(encoding="utf-8"),
            packaged_skill.read_text(encoding="utf-8"),
        )

    def test_claude_skill_documents_safe_l1_dispatch_rules(self):
        skill_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_bridge_connect"
            / "skills"
            / "claude_skill.md"
        )
        text = skill_path.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("---\nname: agentbc\n"))
        self.assertIn("description:", text.split("---", 2)[1])
        self.assertIn("agentbc task status", text)
        self.assertIn("bypassPermissions", text)
        self.assertIn("当前任务", text)
        self.assertIn("new root task", text)
        self.assertIn("stale_handoff_source", text)
        self.assertIn("depends on, reviews, or modifies deliverables", text)
        self.assertIn("handoff_required", text)
        self.assertIn("Claude is the controller, not the executor", text)
        self.assertIn(
            "task handoff <confirmed-task-id> --to <target-executor> --source-platform claude --dispatch",
            text,
        )
        self.assertIn("Do not edit files, generate artifacts, or complete the requested work inline", text)
        self.assertIn("top-level `steps:` list", text)
        self.assertIn("Do not use `.txt`", text)
        self.assertIn("cat > /tmp/agentbc-steps.yaml", text)
        self.assertIn("Claude Code auto mode", text)
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
        self.assertIn("Shortest New-Task Recipe", text)

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

    SKILLS = {
        "codex": "codex_skill.md",
        "claude": "claude_skill.md",
        "hermes": "hermes_skill.md",
    }

    # English text appears in the Codex and Claude skills; the Hermes skill is Chinese.
    EN = {"codex", "claude"}

    def _skill_text(self, platform: str) -> str:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_bridge_connect"
            / "skills"
            / self.SKILLS[platform]
        )
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
                    self.assertIn("never guess", text)
                else:
                    self.assertIn("禁止", text)

    def test_each_packaged_skill_says_handoff_records_current_dispatcher_conversation(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                if platform in self.EN:
                    self.assertIn("current dispatcher conversation", text)
                    self.assertIn("not the source task conversation", text)
                else:
                    self.assertIn("当前派发者会话", text)
                    self.assertIn("而不是源任务会话", text)

    def test_each_packaged_skill_keeps_dispatcher_trace_separate_from_executor_sessions(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                if platform in self.EN:
                    self.assertIn("dispatcher conversation", text)
                    self.assertIn("does not delete", text)
                else:
                    self.assertIn("派发者会话", text)
                    self.assertIn("不会删除", text)

    def test_packaged_skills_have_no_hardcoded_trusted_id_examples(self):
        for platform in ("codex", "claude", "hermes"):
            with self.subTest(platform=platform):
                text = self._skill_text(platform)
                self.assertNotIn("--session-id 019f", text)
                self.assertNotIn("--session-id \"019f", text)
                self.assertNotIn("--session-id '019f", text)


class IdempotentCodexSkillTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

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
        self.assertIn("Dispatcher Traceability", text)
        self.assertTrue((root / "agents" / "openai.yaml").is_file())


class SetupModeTests(unittest.TestCase):
    def test_hermes_setup_enables_visible_runner_output(self):
        from agent_bridge_connect.setup import _executor_config_for

        config = _executor_config_for(
            {
                "name": "hermes",
                "path": str(self.test_dir / "bin" / "hermes"),
                "binary": "hermes",
                "source": "test",
                "capability_level": "L2",
                "version": "test",
            }
        )
        self.assertEqual(config["transport"], "runner")
        self.assertFalse(config["quiet"])

    def test_claude_executor_config_defaults_to_safe_l1_runner(self):
        from agent_bridge_connect.setup import _executor_config_for

        config = _executor_config_for(
            {
                "name": "claude",
                "path": str(self.test_dir / "bin" / "claude"),
                "binary": "claude",
                "source": "test",
                "capability_level": "L1",
                "version": "2.1.186",
            }
        )
        self.assertEqual(config["transport"], "runner")
        self.assertTrue(config["safe_mode"])
        self.assertEqual(config["permission_mode"], "acceptEdits")
        self.assertEqual(config["output_format"], "text")
        self.assertEqual(config["allowed_tools"], ["Read", "Write", "Edit", "Bash"])

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.home = self.test_dir / "home"
        self.home.mkdir()
        self.config_path = self.test_dir / "config.toml"
        self.skill_path = self.test_dir / "hermes" / "agentbc" / "SKILL.md"
        self.claude_skill_path = self.test_dir / "claude" / "agentbc" / "SKILL.md"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def env(self):
        return {
            "HOME": str(self.home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(self.config_path),
            "AGENTBC_HERMES_SKILL_PATH": str(self.skill_path),
            "AGENTBC_CLAUDE_SKILL_PATH": str(self.claude_skill_path),
        }

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
        self.config_path.write_text(
            f'board_root = "{board}"\nworkspace_root = "{workspace}"\n',
            encoding="utf-8",
        )
        target = install_root / "venv" / "bin" / "agentbc"
        target.write_text("binary", encoding="utf-8")
        (bin_dir / "agentbc").symlink_to(target)
        (bin_dir / "abc").write_text("# AgentBC-owned abc shim\n", encoding="utf-8")
        (report_root / "record.md").write_text("report", encoding="utf-8")
        (artifact_root / "artifact.txt").write_text("artifact", encoding="utf-8")
        (board / "task.json").write_text("{}", encoding="utf-8")
        (customer / "keep.txt").write_text("keep", encoding="utf-8")
        return workspace, board, install_root, customer

    def test_uninstall_prompts_for_records_and_artifacts_independently(self):
        workspace, board, install_root, customer = self._seed_uninstall_tree()
        env = {
            "HOME": str(self.home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(self.config_path),
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"),
            "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
        }
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
        workspace, board, install_root, customer = self._seed_uninstall_tree()
        env = {
            "HOME": str(self.home),
            "PATH": "",
            "AGENTBC_CONFIG_PATH": str(self.config_path),
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"),
            "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_uninstall

            result = run_uninstall(
                interactive=False,
                remove_records=False,
                remove_artifacts=True,
            )

        self.assertFalse(result["remove_records"])
        self.assertTrue(result["remove_artifacts"])
        self.assertTrue((board / "task.json").is_file())
        self.assertTrue((workspace / "tasks" / "report" / "record.md").is_file())
        self.assertFalse((workspace / "tasks" / "artifacts").exists())
        self.assertFalse(install_root.exists())
        self.assertTrue((customer / "keep.txt").is_file())

    def test_noninteractive_uninstall_requires_both_data_choices(self):
        workspace, board, install_root, customer = self._seed_uninstall_tree()
        env = {
            "HOME": str(self.home),
            "AGENTBC_CONFIG_PATH": str(self.config_path),
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"),
            "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_uninstall

            with self.assertRaisesRegex(ValueError, "requires an explicit records choice"):
                run_uninstall(
                    interactive=False,
                    remove_records=True,
                    remove_artifacts=None,
                )

        self.assertTrue(board.exists())
        self.assertTrue((workspace / "tasks" / "artifacts" / "artifact.txt").is_file())
        self.assertTrue(install_root.exists())
        self.assertTrue((customer / "keep.txt").is_file())

    def test_uninstall_removes_complete_default_managed_root_when_both_choices_are_yes(self):
        workspace, board, install_root, customer = self._seed_uninstall_tree()
        legacy = workspace / "agentbc_tasks" / "legacy.txt"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("old alpha residue", encoding="utf-8")
        env = {
            "HOME": str(self.home),
            "AGENTBC_CONFIG_PATH": str(self.config_path),
            "AGENTBC_ALPHA_HOME": str(install_root),
            "AGENTBC_BIN_DIR": str(self.home / ".local" / "bin"),
            "AGENTBC_UNINSTALL_SKIP_RUNNER": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from agent_bridge_connect.setup import run_uninstall

            result = run_uninstall(
                interactive=False,
                remove_records=True,
                remove_artifacts=True,
            )

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
        codex.chmod(0o755)
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

            self.assertEqual(
                _hermes_skill_path(),
                self.home / ".hermes" / "skills" / "agentbc" / "SKILL.md",
            )

        custom_home = self.test_dir / "custom-hermes-home"
        with mock.patch.dict(
            os.environ,
            {**path_env, "HERMES_HOME": str(custom_home)},
            clear=True,
        ):
            self.assertEqual(
                _hermes_skill_path(),
                custom_home / "skills" / "agentbc" / "SKILL.md",
            )

    def test_run_setup_installs_both_detected_skills_and_refreshes_state(self):
        agents_before = [
            {
                "name": "hermes",
                "display": "Hermes Agent",
                "found": True,
                "supported_executor": True,
                "path": str(self.test_dir / "bin" / "hermes"),
                "binary": "hermes",
                "version": "test",
                "source": "test",
                "capability_level": "L2",
                "capabilities": {},
                "skill": {"installed": False},
            },
            {
                "name": "claude",
                "display": "Claude Code",
                "found": True,
                "supported_executor": True,
                "path": str(self.test_dir / "bin" / "claude"),
                "binary": "claude",
                "version": "test",
                "source": "test",
                "capability_level": "L1",
                "capabilities": {},
                "skill": {"installed": False},
            },
        ]
        agents_after = [
            {**agent, "enabled": True, "skill": {"installed": True, "up_to_date": True}}
            for agent in agents_before
        ]
        hermes_result = {"installed": True, "changed": True, "status": "installed", "path": "/h/SKILL.md"}
        claude_result = {"installed": True, "changed": True, "status": "installed", "path": "/c/SKILL.md"}

        with mock.patch.dict(os.environ, self.env(), clear=False):
            from agent_bridge_connect import setup

            with mock.patch.object(setup, "scan_all_agents", side_effect=[agents_before, agents_after]), \
                 mock.patch.object(setup, "_print_scan_report"), \
                 mock.patch.object(setup, "_confirm", return_value=True), \
                 mock.patch.object(setup, "install_hermes_skill", return_value=hermes_result) as install_hermes, \
                 mock.patch.object(setup, "install_claude_skill", return_value=claude_result) as install_claude, \
                 mock.patch.object(setup, "_configure_alias", return_value={"status": "skipped"}), \
                 mock.patch.object(setup, "discover_codex", return_value={"found": False}), \
                 mock.patch.object(setup, "probe_codex", return_value={}):
                result = setup.run_setup(interactive=True, permission_mode="safe")

        install_hermes.assert_called_once_with(interactive=True)
        install_claude.assert_called_once_with(interactive=True)
        self.assertEqual(result["skills"]["hermes"], hermes_result)
        self.assertEqual(result["skills"]["claude"], claude_result)
        self.assertTrue(all(agent["skill"]["installed"] for agent in result["agents"]))

    def test_run_setup_interactively_installs_both_skill_files(self):
        fake_bin = self.test_dir / "bin"
        fake_bin.mkdir()
        hermes = fake_bin / "hermes"
        claude = fake_bin / "claude"
        hermes.write_text("#!/bin/sh\necho 'Hermes Agent test'\n", encoding="utf-8")
        claude.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = '--help' ]; then\n"
            "  echo '--print --safe-mode text json stream-json acceptEdits'\n"
            "else\n"
            "  echo 'Claude Code test'\n"
            "fi\n",
            encoding="utf-8",
        )
        hermes.chmod(0o755)
        claude.chmod(0o755)
        env = {
            **self.env(),
            "AGENTBC_HERMES_BIN": str(hermes),
            "AGENTBC_CLAUDE_BIN": str(claude),
        }

        with mock.patch.dict(os.environ, env, clear=True):
            from agent_bridge_connect import setup

            with contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch(
                     "builtins.input",
                     side_effect=["safe", "y", "y", "1", "y", "1", "n", *(["y"] * 10)],
                 ), \
                 mock.patch.object(setup, "_configure_alias", return_value={"status": "skipped"}):
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
        claude = next(agent for agent in result["agents"] if agent["name"] == "claude")
        self.assertIn("skill", claude)
        self.assertFalse(claude["skill"]["installed"])


if __name__ == "__main__":
    unittest.main()
