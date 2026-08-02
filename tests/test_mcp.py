"""Phase 10b tests: MCP tools + Gate D (CLI/MCP parity)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FIXTURES = Path(__file__).parent / "fixtures"
STEPS_YAML = FIXTURES / "sample_steps.yaml"


class MCPToolExistenceTests(unittest.TestCase):
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
        required = ["abc_task_create", "abc_task_get", "abc_task_list",
                     "abc_task_intervene", "abc_task_report", "abc_task_brief"]
        for name in required:
            self.assertIn(name, tool_names, f"Missing MCP tool: {name}")


class MCPParityTests(unittest.TestCase):
    """Gate D: MCP calls must produce same state and report as CLI calls."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.workspace = self.test_dir / "workspace"
        self.config_path = self.test_dir / "config.toml"
        self.config_path.write_text(
            f'workspace_root = "{self.workspace}"\n',
            encoding="utf-8",
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {"AGENTBC_CONFIG_PATH": str(self.config_path)},
            clear=False,
        )
        self.env_patch.start()
        from agent_bridge_connect.task_board import init_board
        init_board(self.test_dir)

    def tearDown(self):
        self.env_patch.stop()
        shutil.rmtree(self.test_dir)

    def test_create_parity(self):
        """MCP create_task produces same task as CLI create."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})

        # CLI path
        cli_task = svc.create_task("CLI task", "mock",
                                    [{"id": 1, "description": "step 1"}],
                                    customer_dir=False)

        # MCP path
        mcp_result = handle_tool_call("abc_task_create", {
            "title": "MCP task",
            "assignee": "mock",
            "steps": [{"id": 1, "description": "step 1"}],
            "customer_dir": False,
        }, board_root=self.test_dir)

        self.assertTrue(mcp_result["ok"])
        mcp_task = svc.get_task(mcp_result["task_id"])

        # Both should have same structure
        self.assertEqual(cli_task.status, mcp_task.status)
        self.assertEqual(cli_task.assignee, mcp_task.assignee)
        self.assertEqual(len(cli_task.steps), len(mcp_task.steps))

    def test_status_parity(self):
        """MCP get_task returns same data as CLI status."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Status test", "mock",
                                [{"id": 1, "description": "step 1"}],
                                customer_dir=False)

        # CLI path
        cli_task = svc.get_task(task.id)

        # MCP path
        mcp_result = handle_tool_call("abc_task_get", {
            "task_id": task.id
        }, board_root=self.test_dir)

        self.assertTrue(mcp_result["ok"])
        self.assertEqual(mcp_result["task"]["id"], cli_task.id)
        self.assertEqual(mcp_result["task"]["status"], cli_task.status)

    def test_report_parity(self):
        """MCP report returns same data as CLI report."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Report test", "mock",
                                [{"id": 1, "description": "step 1"}],
                                customer_dir=False)
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        svc.complete_task(task.id)

        # CLI path
        cli_report = svc.generate_report(task.id)

        # MCP path
        mcp_result = handle_tool_call("abc_task_report", {
            "task_id": task.id
        }, board_root=self.test_dir)

        self.assertTrue(mcp_result["ok"])
        self.assertEqual(mcp_result["report"]["task_id"], cli_report["task_id"])
        self.assertEqual(mcp_result["report"]["status"], cli_report["status"])

    def test_intervene_parity(self):
        """MCP intervene produces same effect as CLI intervene."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Intervene test", "mock",
                                [{"id": 1, "description": "step 1"}],
                                customer_dir=False)
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})

        # MCP pause
        mcp_result = handle_tool_call("abc_task_intervene", {
            "task_id": task.id,
            "action": "pause",
            "reason": "MCP pause test"
        }, board_root=self.test_dir)

        self.assertTrue(mcp_result["ok"])
        task_after = svc.get_task(task.id)
        self.assertEqual(task_after.status, "running")
        self.assertTrue(task_after.intervention["paused"])

    def test_brief_parity(self):
        """MCP brief returns same data as CLI brief."""
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)})
        task = svc.create_task("Brief test", "mock",
                                [{"id": 1, "description": "step 1"}],
                                customer_dir=False)

        # CLI path
        from agent_bridge_connect.reports import generate_task_brief
        cli_brief = generate_task_brief(task.id, self.test_dir)

        # MCP path
        mcp_result = handle_tool_call("abc_task_brief", {
            "task_id": task.id
        }, board_root=self.test_dir)

        self.assertTrue(mcp_result["ok"])
        self.assertEqual(mcp_result["brief"]["task_id"], cli_brief["task_id"])
        self.assertEqual(mcp_result["brief"]["status"], cli_brief["status"])

    def test_create_uses_configured_workspace_root(self):
        from agent_bridge_connect.mcp_server import handle_tool_call
        from agent_bridge_connect.service import TaskService

        result = handle_tool_call("abc_task_create", {
            "title": "Configured workspace task",
            "assignee": "mock",
            "steps": [{"id": 1, "description": "step 1"}],
            "customer_dir": False,
        }, board_root=self.test_dir)

        self.assertTrue(result["ok"])
        task = TaskService(self.test_dir, config={"workspace_root": str(self.workspace)}).get_task(result["task_id"])
        self.assertEqual(task.workspace["agentbc_root"], str(self.workspace.resolve()))
        self.assertEqual(task.workspace["root"], task.workspace["artifact_root"])


class MCPCoreIsolationTests(unittest.TestCase):
    """MCP must not introduce platform deps into Core."""

    def test_mcp_not_imported_by_core(self):
        """Core modules must not import mcp_server."""
        import importlib
        core_modules = [
            "agent_bridge_connect.protocol",
            "agent_bridge_connect.state_machine",
            "agent_bridge_connect.task_store",
            "agent_bridge_connect.service",
            "agent_bridge_connect.adapters",
            "agent_bridge_connect.config",
        ]
        for mod_name in core_modules:
            mod = importlib.import_module(mod_name)
            source = getattr(mod, "__file__", "")
            if source:
                with open(source) as f:
                    content = f.read()
                self.assertNotIn("mcp_server", content.lower(),
                    f"{mod_name} imports mcp_server")


if __name__ == "__main__":
    unittest.main()
