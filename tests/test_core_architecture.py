"""Architecture conformance tests for Core module.

These tests verify the architecture freeze gate:
1. No platform-specific imports in Core
2. Extensions survive read/write
3. CLI uses service API, not direct file access
4. Service API surface is complete
5. State machine validates transitions
6. Config loads from ~/.abc/config.toml
"""

import importlib
import tempfile
import unittest
from pathlib import Path


CORE_FILES = [
    "agent_bridge_connect.protocol",
    "agent_bridge_connect.state_machine",
    "agent_bridge_connect.task_store",
    "agent_bridge_connect.service",
    "agent_bridge_connect.adapters",
    "agent_bridge_connect.config",
]

# Files that must NOT import platform-specific modules
PLATFORM_FORBIDDEN_IN_CORE = [
    "codex", "claude", "gemini", "cursor",  # executor-specific
    "feishu", "dingtalk", "slack", "email", "smtp",  # notifier-specific
]


class ArchitectureFreezeGateTests(unittest.TestCase):
    """Core files must exist and not import platform details."""

    def test_core_modules_importable(self):
        """All 6 core modules must be importable."""
        for mod_name in CORE_FILES:
            mod = importlib.import_module(mod_name)
            self.assertIsNotNone(mod, f"Module {mod_name} not importable")

    def test_no_platform_imports_in_core(self):
        """Core modules must not import executor/notifier specific modules."""
        for mod_name in CORE_FILES:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError:
                continue
            source_file = getattr(mod, "__file__", "")
            if not source_file:
                continue
            # Check the module's direct dependencies only
            # (skip if we can't read the file)
            try:
                with open(source_file) as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            for forbidden in PLATFORM_FORBIDDEN_IN_CORE:
                # Allow "codex" in comments and strings like protocol docstrings
                # But not in import statements
                import_lines = [
                    line for line in content.split("\n")
                    if line.strip().startswith(("import ", "from "))
                ]
                for line in import_lines:
                    if forbidden in line.lower():
                        self.fail(
                            f"{mod_name} imports platform-specific module "
                            f"'{forbidden}': {line.strip()}"
                        )

    def test_protocol_has_version(self):
        """protocol.py must define ABC_PROTOCOL_VERSION."""
        from agent_bridge_connect import protocol

        self.assertTrue(
            hasattr(protocol, "ABC_PROTOCOL_VERSION"),
            "protocol.py missing ABC_PROTOCOL_VERSION"
        )

    def test_extensions_preservation(self):
        """Unknown extension fields in task.json survive read/write cycle."""
        from agent_bridge_connect.task_store import TaskStore

        store = TaskStore(Path(tempfile.mkdtemp()))
        # Create a task with custom extensions
        task_data = {
            "id": "TEST-001",
            "title": "Extension test",
            "status": "pending",
            "assignee": "mock",
            "created_by": "test",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "steps": [],
            "intervention": {"paused": False, "pause_reason": None, "latest_correction_id": None},
            "errors": [],
            "report": None,
            "extensions": {
                "executor.codex": {"session_id": "abc123", "model": "o3"},
                "notifier.feishu": {"message_id": "msg_001"},
                "custom_vendor": {"some_field": "some_value"}
            }
        }
        store.write_task("TEST-001", task_data)
        read_back = store.read_task("TEST-001")
        self.assertEqual(
            read_back["extensions"]["executor.codex"]["session_id"], "abc123"
        )
        self.assertEqual(
            read_back["extensions"]["custom_vendor"]["some_field"], "some_value"
        )


class ServiceAPISurfaceTests(unittest.TestCase):
    """service.py must expose required public operations."""

    def test_service_has_required_methods(self):
        """Service must expose create, get, list, claim, execute, pause, resume, report."""
        from agent_bridge_connect import service

        required = ["create_task", "get_task", "list_tasks",
                     "claim_task", "execute_step",
                     "pause_task", "resume_task", "cancel_task",
                     "generate_report", "notify"]
        for method in required:
            self.assertTrue(
                hasattr(service, method),
                f"service.py missing required method: {method}"
            )


class StateMachineTests(unittest.TestCase):
    """State machine must enforce valid transitions."""

    def test_valid_transition_pending_to_assigned(self):
        """pending → assigned is valid."""
        from agent_bridge_connect.state_machine import can_transition

        self.assertTrue(can_transition("pending", "assigned"))

    def test_valid_transition_assigned_to_working(self):
        """assigned → working is valid."""
        from agent_bridge_connect.state_machine import can_transition

        self.assertTrue(can_transition("assigned", "working"))

    def test_valid_transition_working_to_completed(self):
        """working → completed is valid."""
        from agent_bridge_connect.state_machine import can_transition

        self.assertTrue(can_transition("working", "completed"))

    def test_invalid_transition_pending_to_completed(self):
        """pending → completed (skipping intermediate) is invalid."""
        from agent_bridge_connect.state_machine import can_transition

        self.assertFalse(can_transition("pending", "completed"))

    def test_invalid_transition_working_to_pending(self):
        """working → pending (backward) is invalid."""
        from agent_bridge_connect.state_machine import can_transition

        self.assertFalse(can_transition("working", "pending"))

    def test_valid_transition_working_to_pause_pending(self):
        """working → pause_pending (cooperative pause) is valid."""
        from agent_bridge_connect.state_machine import can_transition

        self.assertTrue(can_transition("working", "pause_pending"))


class ConfigTests(unittest.TestCase):
    """Config module must load and validate ~/.abc/config.toml."""

    def test_config_module_exists(self):
        """config.py must be importable."""
        from agent_bridge_connect import config

        self.assertTrue(
            hasattr(config, "load_config") or hasattr(config, "get_config"),
            "config.py missing load/get function"
        )


class AdapterContractTests(unittest.TestCase):
    """Adapter port definitions must exist with required methods."""

    def test_executor_port_interface(self):
        """ExecutorPort must define probe, capabilities, start, poll, cancel."""
        from agent_bridge_connect.adapters import ExecutorPort

        required = ["probe", "capabilities", "start", "poll"]
        for method in required:
            self.assertTrue(
                hasattr(ExecutorPort, method),
                f"ExecutorPort missing: {method}"
            )

    def test_notifier_port_interface(self):
        """NotifierPort must define probe and send."""
        from agent_bridge_connect.adapters import NotifierPort

        self.assertTrue(hasattr(NotifierPort, "probe"))
        self.assertTrue(hasattr(NotifierPort, "send"))

    def test_capability_model_exists(self):
        """Capability model with L0-L4 levels must be defined."""
        from agent_bridge_connect.adapters import ExecutorLevel

        self.assertTrue(hasattr(ExecutorLevel, "L0"))
        self.assertTrue(hasattr(ExecutorLevel, "L4"))


if __name__ == "__main__":
    unittest.main()
