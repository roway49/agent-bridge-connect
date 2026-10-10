"""PROTO-104-001 controlled capture/validation tool tests.

The capture tool is the only sanctioned writer into the fixture matrix.  These
tests freeze its contract: closed probe whitelist, mandatory redaction of
tokens/paths/prompts/environment dumps, deterministic hashes and redacted
diffs, and refusal to stage anything that still carries private material.
"""

from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import importlib.util
import os as _mcp_os
import shutil as _mcp_shutil
import tempfile as _mcp_tempfile
import unittest as _mcp_unittest
from pathlib import Path as _mcp_Path
from unittest import mock as _mcp_mock

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tests" / "fixtures" / "executor_runtime" / "tools" / "capture_protocol_fixture.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("capture_protocol_fixture", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class ProbeWhitelistTests(unittest.TestCase):
    def test_probe_list_is_closed_and_official_only(self) -> None:
        for surface, argv in tool.PROBE_COMMANDS.items():
            with self.subTest(surface=surface):
                joined = " ".join(argv)
                self.assertTrue(any((token in joined for token in ("--version", "--help", "--check", "generate-json-schema"))), f"{surface} is not a read-only official probe")
        delete_probes = {surface: argv for (surface, argv) in tool.PROBE_COMMANDS.items() if "delete" in " ".join(argv)}
        self.assertTrue(delete_probes)
        for surface, argv in delete_probes.items():
            with self.subTest(surface=surface):
                self.assertTrue(argv[-1] == "--help")
                self.assertNotIn("--yes", argv)
                self.assertNotIn("--force", argv)

    def test_unknown_surface_is_rejected_without_spawning(self) -> None:
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(SystemExit):
                tool.run_probe(executor="hermes", binary=Path("/bin/hermes"), surface="export-session-body", staging_dir=Path(tempfile.mkdtemp()), timeout=1)
            run.assert_not_called()

    def test_directory_surfaces_are_schema_generation_only(self) -> None:
        self.assertEqual(list(tool.DIRECTORY_SURFACES), ["app_server_schema"])


class RedactionTests(unittest.TestCase):
    def test_home_paths_are_normalized(self) -> None:
        home = Path.home()
        (text, applied) = tool.redact_text(f"installed at {home}/.hermes/hermes-agent")
        self.assertIn("home_path", applied)
        self.assertNotIn(str(home), text)
        self.assertIn("~/.hermes/hermes-agent", text)

    def test_tokens_bearer_and_jwt_are_redacted(self) -> None:
        samples = {"openai_key": "sk-" + "abcdefghij0123456789", "bearer_credential": "Bearer abcdefghijklmnop1234567890", "github_token": "ghp_" + "a" * 24, "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE", "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz"}
        for name, sample in samples.items():
            with self.subTest(rule=name):
                (redacted, applied) = tool.redact_text(sample)
                self.assertIn(name, applied)
                self.assertNotEqual(redacted, sample)

    def test_private_session_store_paths_are_redacted(self) -> None:
        (redacted, applied) = tool.redact_text("session at ~/.codex/sessions/abc.jsonl")
        self.assertIn("private_session_store", applied)
        self.assertIn(tool._REDACTED, redacted)

    def test_env_assignment_dump_lines_are_redacted(self) -> None:
        (redacted, applied) = tool.redact_text("HERMES_YOLO_MODE=1\nnormal line")
        self.assertIn("assignment_env_dump", applied)
        self.assertIn("<redacted>", redacted)

    def test_forbidden_json_keys_are_scrubbed(self) -> None:
        value = {"prompt": "customer secret plan", "nested": {"env": {"PATH": "/usr/bin"}}}
        (cleaned, applied) = tool.redact_structured(value)
        self.assertEqual(cleaned["prompt"], "<redacted>")
        self.assertEqual(cleaned["nested"]["env"], "<redacted>")
        self.assertTrue(applied)

    def test_assert_no_secrets_fails_closed(self) -> None:
        with self.assertRaises(SystemExit):
            tool.assert_no_secrets("token ghp_" + "a" * 30, origin="help.txt")
        tool.assert_no_secrets("usage: codex delete [OPTIONS] <SESSION>", origin="x")


class CaptureAndStagingTests(unittest.TestCase):
    def _fake_run(self, stdout: str, returncode: int = 0):
        completed = mock.Mock()
        completed.returncode = returncode
        completed.stdout = stdout
        completed.stderr = ""
        return completed

    def test_capture_records_redacted_probe_output(self) -> None:
        staging = Path(tempfile.mkdtemp())
        payload = f"Hermes Agent v0.20.1\nInstall directory: {Path.home()}/.hermes/hermes-agent\n"
        with mock.patch.object(tool.subprocess, "run", create=True, return_value=self._fake_run(payload)):
            snapshot = tool.capture(executor="hermes", binary=Path("/bin/hermes"), version="0.20.1", staging_dir=staging, surfaces=["version"])
        staged = json.loads((staging / "snapshot.json").read_text(encoding="utf-8"))
        record = staged["probes"][0]
        self.assertFalse(snapshot is None)
        self.assertEqual(record["redactions"], ["home_path"])
        self.assertNotIn(str(Path.home()), record["stdout"])

    def test_staged_snapshot_with_private_material_is_refused_on_import(self) -> None:
        staging = Path(tempfile.mkdtemp())
        (staging / "snapshot.json").write_text(json.dumps({"prompt": f"user prompt mentioning {Path.home()}/secret"}), encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            tool.load_staging_snapshot(staging)
        self.assertIn("had to be redacted", str(ctx.exception))

    def test_review_reports_hashes_and_redacted_baseline_diff(self) -> None:
        staging = Path(tempfile.mkdtemp())
        baseline = (MATRIX_VERSION_DIR / "delete_help.txt").read_text(encoding="utf-8")
        (staging / "snapshot.json").write_text(json.dumps({"executor": "codex", "version": "0.146.0", "probes": [{"surface": "delete_help", "ok": True, "stdout": baseline + "\nExtra official line", "stderr": "", "returncode": 0}]}), encoding="utf-8")
        report = tool.review(executor="codex", version=MATRIX_VERSION, staging_dir=staging)
        entry = report["files"]["delete_help.txt"]
        self.assertIn("sha256", entry)
        self.assertFalse(entry["unchanged"])
        self.assertGreater(entry["diff_lines"], 0)
        self.assertEqual(tool.sha256_file(MATRIX_VERSION_DIR / "delete_help.txt"), tool.sha256_bytes(baseline.encode("utf-8")))

    def test_manifest_hash_refresh_detects_drift(self) -> None:
        surfaces_before = json.loads(MATRIX_MANIFEST.read_text(encoding="utf-8"))["executors"]["codex"]["versions"][MATRIX_VERSION]["surfaces"]
        stale_entry = dict(surfaces_before)
        for metadata in stale_entry.values():
            metadata["sha256"] = "0" * 64
        problems = []
        for relative, metadata in stale_entry.items():
            path = MATRIX_VERSION_DIR / relative
            if path.is_file() and tool.sha256_file(path) != metadata["sha256"]:
                problems.append(relative)
        self.assertEqual(problems, sorted(surfaces_before))

    def test_verify_passes_for_the_current_matrix(self) -> None:
        self.assertTrue(tool.verify())


MATRIX_ROOT = REPO_ROOT / "tests" / "fixtures" / "executor_runtime" / "matrix"
MATRIX_MANIFEST = MATRIX_ROOT / "manifest.json"
MATRIX_VERSION = "0.146.0"
MATRIX_VERSION_DIR = MATRIX_ROOT / "codex" / MATRIX_VERSION


class PromotionGuardTests(unittest.TestCase):
    def test_promote_refuses_undeclared_versions_before_writing(self) -> None:
        manifest = json.loads(MATRIX_MANIFEST.read_text(encoding="utf-8"))
        absent_version = "99.0.0"
        self.assertNotIn(absent_version, manifest["executors"]["codex"]["versions"])
        staging = Path(tempfile.mkdtemp())
        (staging / "snapshot.json").write_text(json.dumps({"executor": "codex", "version": absent_version, "probes": [{"surface": "version", "ok": True, "stdout": "x", "stderr": "", "returncode": 0}]}), encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            tool.promote(executor="codex", version=absent_version, staging_dir=staging)
        self.assertIn("not declared in the manifest", str(ctx.exception))
        self.assertFalse((MATRIX_ROOT / "codex" / absent_version).exists())

    def test_known_executors_only(self) -> None:
        self.assertEqual(set(tool.KNOWN_EXECUTORS), {"codex", "claude", "hermes"})


_mcp_FIXTURES = _mcp_Path(__file__).parents[1] / "fixtures"
_mcp_STEPS_YAML = _mcp_FIXTURES / "sample_steps.yaml"


class _mcp_MCPToolExistenceTests(_mcp_unittest.TestCase):
    """MCP server must expose required tools."""

    def test_mcp_server_importable(self):
        """mcp_server.py must be importable."""
        from agent_bridge_connect import mcp_server

        self.assertIsNotNone(mcp_server)

    def test_mcp_tools_registered(self):
        """All 6 MCP tools must be registered."""
        from agent_bridge_connect.mcp_server import get_tools

        tools = get_tools()
        tool_names = [t["name"] for t in tools]
        required = ["abc_task_create", "abc_task_get", "abc_task_list", "abc_task_intervene", "abc_task_report", "abc_task_brief"]
        for name in required:
            self.assertIn(name, tool_names, f"Missing MCP tool: {name}")


class _mcp_MCPParityTests(_mcp_unittest.TestCase):
    """Gate D: MCP calls must produce same state and report as CLI calls."""

    def setUp(self):
        self.test_dir = _mcp_Path(_mcp_tempfile.mkdtemp())
        self.workspace = self.test_dir / "workspace"
        self.config_path = self.test_dir / "config.toml"
        self.config_path.write_text(f'workspace_root = "{self.workspace}"\n', encoding="utf-8")
        self.env_patch = _mcp_mock.patch.dict(_mcp_os.environ, {"AGENTBC_CONFIG_PATH": str(self.config_path)}, clear=False)
        self.env_patch.start()
        from agent_bridge_connect.task_board import init_board

        init_board(self.test_dir)

    def tearDown(self):
        self.env_patch.stop()
        _mcp_shutil.rmtree(self.test_dir)

    def test_create_parity(self):
        """MCP create_task produces same task as CLI create."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        cli_task = svc.create_task("CLI task", "mock", [{"id": 1, "description": "step 1"}], customer_dir=False)
        mcp_result = handle_tool_call("abc_task_create", {"title": "MCP task", "assignee": "mock", "steps": [{"id": 1, "description": "step 1"}], "customer_dir": False}, board_root=self.test_dir)
        self.assertTrue(mcp_result["ok"])
        mcp_task = svc.get_task(mcp_result["task_id"])
        self.assertEqual(cli_task.status, mcp_task.status)
        self.assertEqual(cli_task.assignee, mcp_task.assignee)
        self.assertEqual(len(cli_task.steps), len(mcp_task.steps))

    def test_status_parity(self):
        """MCP get_task returns same data as CLI status."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Status test", "mock", [{"id": 1, "description": "step 1"}], customer_dir=False)
        cli_task = svc.get_task(task.id)
        mcp_result = handle_tool_call("abc_task_get", {"task_id": task.id}, board_root=self.test_dir)
        self.assertTrue(mcp_result["ok"])
        self.assertEqual(mcp_result["task"]["id"], cli_task.id)
        self.assertEqual(mcp_result["task"]["status"], cli_task.status)

    def test_report_parity(self):
        """MCP report returns same data as CLI report."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService
        from tests.contract_helpers import finalize_completed

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Report test", "mock", [{"id": 1, "description": "step 1"}], customer_dir=False)
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        finalize_completed(svc, task.id)
        cli_report = svc.generate_report(task.id)
        mcp_result = handle_tool_call("abc_task_report", {"task_id": task.id}, board_root=self.test_dir)
        self.assertTrue(mcp_result["ok"])
        self.assertEqual(mcp_result["report"]["task_id"], cli_report["task_id"])
        self.assertEqual(mcp_result["report"]["status"], cli_report["status"])

    def test_intervene_parity(self):
        """MCP intervene produces same effect as CLI intervene."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Intervene test", "mock", [{"id": 1, "description": "step 1"}], customer_dir=False)
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        mcp_result = handle_tool_call("abc_task_intervene", {"task_id": task.id, "action": "pause", "reason": "MCP pause test"}, board_root=self.test_dir)
        self.assertTrue(mcp_result["ok"])
        task_after = svc.get_task(task.id)
        self.assertEqual(task_after.status, "running")
        self.assertTrue(task_after.intervention["paused"])

    def test_brief_parity(self):
        """MCP brief returns same data as CLI brief."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Brief test", "mock", [{"id": 1, "description": "step 1"}], customer_dir=False)
        from agent_bridge_connect.reports import generate_task_brief

        cli_brief = generate_task_brief(task.id, self.test_dir)
        mcp_result = handle_tool_call("abc_task_brief", {"task_id": task.id}, board_root=self.test_dir)
        self.assertTrue(mcp_result["ok"])
        self.assertEqual(mcp_result["brief"]["task_id"], cli_brief["task_id"])
        self.assertEqual(mcp_result["brief"]["status"], cli_brief["status"])

    def test_create_uses_configured_workspace_root(self):
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        result = handle_tool_call("abc_task_create", {"title": "Configured workspace task", "assignee": "mock", "steps": [{"id": 1, "description": "step 1"}], "customer_dir": False}, board_root=self.test_dir)
        self.assertTrue(result["ok"])
        task = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)}).get_task(result["task_id"])
        self.assertEqual(task.workspace["agentbc_root"], str(self.workspace.resolve()))
        self.assertEqual(task.workspace["root"], task.workspace["artifact_root"])


class _mcp_MCPCoreIsolationTests(_mcp_unittest.TestCase):
    """MCP must not introduce platform deps into Core."""

    def test_mcp_not_imported_by_core(self):
        """Core modules must not import mcp_server."""
        import importlib

        core_modules = ["agent_bridge_connect.protocol", "agent_bridge_connect.state_machine", "agent_bridge_connect.task_store", "agent_bridge_connect.service", "agent_bridge_connect.adapters", "agent_bridge_connect.config"]
        for mod_name in core_modules:
            mod = importlib.import_module(mod_name)
            source = getattr(mod, "__file__", "")
            if source:
                with open(source) as f:
                    content = f.read()
                self.assertNotIn("mcp_server", content.lower(), f"{mod_name} imports mcp_server")
