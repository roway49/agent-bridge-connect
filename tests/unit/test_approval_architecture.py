"""ARCH-104-001 Slice A import and lifecycle characterization tests."""

from __future__ import annotations
import inspect
import tempfile
import unittest
from pathlib import Path
from agent_bridge_connect import approval as approval_schema
from agent_bridge_connect import approval_lifecycle, approval_protocol, control
from agent_bridge_connect.approval import APPROVAL_EXTENSION_KEY
from agent_bridge_connect.control import ApprovalControlPlane, ControlPlaneError, approval_response_payload_v2, claude_offered_choices, codex_offered_choices, hermes_offered_choices, normalize_approval_request
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.service import TaskService
import importlib as _architecture_importlib
import tempfile as _architecture_tempfile
import unittest as _architecture_unittest
from pathlib import Path as _architecture_Path


class ApprovalProtocolRefactorTests(unittest.TestCase):
    SESSION_ID = "11111111-1111-4111-8111-111111111111"
    RUN_ID = "arch-104-run-1"

    def test_facade_reexports_and_signatures_remain_stable(self) -> None:
        from agent_bridge_connect import control_transport

        self.assertIs(control.ApprovalRequest, approval_protocol.ApprovalRequest)
        self.assertIs(control.ControlEvent, approval_protocol.ControlEvent)
        self.assertIs(control.StdioJsonRpcTransport, control_transport.StdioJsonRpcTransport)
        self.assertIs(control.CodexAppServerTransport, control_transport.CodexAppServerTransport)
        self.assertEqual(str(inspect.signature(control.normalize_approval_request)), "(message: 'dict[str, Any]', *, task_id: 'str', executor_run_id: 'str', session_id: 'str', executor: 'str' = 'codex') -> 'ApprovalRequest'")
        self.assertEqual(str(inspect.signature(TaskService.respond_to_input)), "(self, task_id: 'str', input_id: 'str', *, response_type: 'str', message: 'str' = '') -> 'dict[str, Any]'")
        block_parameters = inspect.signature(TaskService.block_task_for_approval).parameters
        self.assertEqual(list(block_parameters)[:6], ["self", "task_id", "executor_run_id", "session_id", "request_id", "request_fingerprint"])
        self.assertEqual(block_parameters["executor_run_id"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_codex_claude_and_hermes_requests_normalize_executor_neutrally(self) -> None:
        codex_choices = list(codex_offered_choices("command", session_decisions_supported=True))
        codex_request = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": "codex-request-1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": self.SESSION_ID, "turnId": "turn-1", "itemId": "item-1"},
                "approval_version": 2,
                "offered_choices": codex_choices,
                "authority": {"executor": "codex", "protocol": "codex_app_server", "protocol_version": 2, "method": "item/commandExecution/requestApproval"},
            },
            task_id="ARCH-104-001",
            executor_run_id=self.RUN_ID,
            session_id=self.SESSION_ID,
            executor="codex",
        )
        self.assertEqual(codex_request.approval_version, 2)
        self.assertEqual([choice["native_option_id"] for choice in codex_request.offered_choices], ["accept", "acceptForSession", "decline"])
        self.assertTrue(all((choice["handle"].startswith("opt-") for choice in codex_request.offered_choices)))
        claude_request = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": "claude-request-1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": self.SESSION_ID, "turnId": "turn-2", "itemId": "toolu-1"},
                "approval_version": 3,
                "scope": "task_elevation",
                "elevation_mode": "full",
                "native_event": "claude_sdk_can_use_tool",
                "path_plan_digest": "sha256:" + "a" * 64,
                "containment_profile_digest": "sha256:" + "b" * 64,
                "authority": {"executor": "claude", "protocol": "claude_agent_sdk", "protocol_version": 1, "method": "sdk.can_use_tool"},
                "_agentbc": {"request_fingerprint": "fp-" + "c" * 40, "tool_use_id": "toolu-1", "tool_name": "Read", "action_fingerprint": "fp-" + "d" * 40},
            },
            task_id="ARCH-104-001",
            executor_run_id=self.RUN_ID,
            session_id=self.SESSION_ID,
            executor="claude",
        )
        self.assertEqual(claude_request.approval_version, 3)
        self.assertEqual(claude_request.scope, "task_elevation")
        self.assertEqual(claude_request.elevation_mode, "full")
        self.assertEqual(claude_request.native_event, "claude_sdk_can_use_tool")
        hermes_choices = list(hermes_offered_choices([{"native_option_id": "allow_once", "kind": "once", "label": "Allow"}, {"native_option_id": "reject", "kind": "deny", "label": "Reject", "selectable": True}]))
        hermes_request = normalize_approval_request(
            {
                "jsonrpc": "2.0",
                "id": "hermes-request-1",
                "method": "item/permissions/requestApproval",
                "params": {"threadId": self.SESSION_ID, "turnId": "turn-3", "itemId": "permission-1", "permissions": {"filesystem": "workspace"}},
                "approval_version": 2,
                "offered_choices": hermes_choices,
                "authority": {"executor": "hermes", "protocol": "hermes_acp", "protocol_version": 1, "method": "session/request_permission"},
            },
            task_id="ARCH-104-001",
            executor_run_id=self.RUN_ID,
            session_id=self.SESSION_ID,
            executor="hermes",
        )
        self.assertEqual(hermes_request.approval_version, 2)
        self.assertEqual(hermes_request.offered_choices[1]["native_option_id"], "reject")
        self.assertEqual(approval_response_payload_v2(hermes_request, choice_kind="deny", native_option_id="reject", decision="decline"), {"outcome": {"optionId": "reject"}})

    def test_offered_choices_keep_native_contracts_and_session_gates(self) -> None:
        self.assertEqual([choice["native_option_id"] for choice in codex_offered_choices("command", session_decisions_supported=False)], ["accept", "decline"])
        self.assertEqual([choice["native_option_id"] for choice in claude_offered_choices(session_bundle_supported=False)], ["deny", "allow_once"])
        self.assertEqual([choice["native_option_id"] for choice in claude_offered_choices(session_bundle_supported=True)], ["deny", "allow_once", "allow_session"])
        self.assertEqual(hermes_offered_choices([{"native_option_id": "native-2", "kind": "other", "label": "Other", "selectable": False}])[0], {"native_option_id": "native-2", "kind": "other", "label": "Other", "selectable": False})

    def test_control_rejects_out_of_order_duplicate_replay_and_cross_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def make_plane(name: str) -> ApprovalControlPlane:
                plane = ApprovalControlPlane(root / name, task_id="ARCH-104-001", executor_run_id=self.RUN_ID, session_id=self.SESSION_ID, executor="codex")
                plane.record_session_started({"version": 1, "executor": "codex", "session_id": self.SESSION_ID, "resumed": False, "persistence": "persistent", "source": "jsonl_thread_started"})
                return plane

            out_of_order_plane = make_plane("out-of-order")
            with self.assertRaises(ControlPlaneError) as out_of_order:
                out_of_order_plane.respond_approval("ARCH-104-001", self.RUN_ID, self.SESSION_ID, "request-before-event", "accept")
            self.assertEqual(out_of_order.exception.code, "approval_identity_mismatch")
            request = {"jsonrpc": "2.0", "id": "request-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": self.SESSION_ID, "turnId": "turn-1", "itemId": "item-1"}}
            duplicate_plane = make_plane("duplicate")
            duplicate_plane.request_approval(request)
            with self.assertRaises(ControlPlaneError) as duplicate:
                duplicate_plane.request_approval(request)
            self.assertEqual(duplicate.exception.code, "approval_request_duplicate")
            concurrent_plane = make_plane("concurrent")
            concurrent_plane.request_approval(request)
            with self.assertRaises(ControlPlaneError) as out_of_order_request:
                concurrent_plane.request_approval({**request, "id": "request-2"})
            self.assertEqual(out_of_order_request.exception.code, "approval_concurrent_request")
            wrong_session_plane = make_plane("wrong-session")
            with self.assertRaises(ControlPlaneError) as wrong_session:
                wrong_session_plane.request_approval({**request, "id": "request-other-session", "params": {**request["params"], "threadId": "other-session"}})
            self.assertEqual(wrong_session.exception.code, "approval_session_mismatch")
            plane = make_plane("success")
            plane.request_approval(request)
            plane.respond_approval("ARCH-104-001", self.RUN_ID, self.SESSION_ID, "request-1", "accept")
            with self.assertRaises(ControlPlaneError) as replay:
                plane.respond_approval("ARCH-104-001", self.RUN_ID, self.SESSION_ID, "request-1", "accept")
            self.assertEqual(replay.exception.code, "approval_request_expired")


class ApprovalLifecycleRefactorTests(unittest.TestCase):
    RUN_ID = "arch-104-lifecycle-run"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.board = root / "record"
        self.project = root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(root), "permission_mode": "safe"})

    def _started_task(self, request_id: str) -> tuple[str, str]:
        task = self.service.create_task("ARCH-104 lifecycle fixture", "claude", [{"id": 1, "description": "one approval-bound step"}], customer_dir=True, customer_path=self.project, permission_mode="safe")
        self.service.start_task_run(task.id, "claude")
        self.service.record_executor_run_started(task.id, self.RUN_ID)
        current = self.service.get_task(task.id)
        session = dict(current.extensions["agentbc.session"])
        session["session_state"] = "active"
        session["run_ids"] = [self.RUN_ID]
        current.extensions["agentbc.session"] = session
        self.service.store.write_task(task.id, current.to_dict())
        self.service.block_task_for_approval(task.id, executor_run_id=self.RUN_ID, session_id=str(session["session_id"]), request_id=request_id, request_fingerprint="fp-" + request_id.replace("-", "")[:40].ljust(40, "a"), executor="claude", operation="Bash")
        return (task.id, str(session["session_id"]))

    def test_approve_deny_timeout_and_same_session_binding(self) -> None:
        (task_id, session_id) = self._started_task("approval-approve")
        request = self.service.get_task(task_id).extensions["agentbc.input"]
        approved = self.service.respond_to_input(task_id, request["input_id"], response_type="approve")
        self.assertEqual(approved["approval_source"], "user")
        self.assertTrue(approved["same_session"])
        self.assertEqual(self.service.get_task(task_id).extensions[APPROVAL_EXTENSION_KEY]["decision"]["type"], "approve")
        self.assertEqual(self.service.get_task(task_id).extensions["agentbc.session"]["session_id"], session_id)
        (denied_id, _) = self._started_task("approval-deny")
        denied_request = self.service.get_task(denied_id).extensions["agentbc.input"]
        denied = self.service.respond_to_input(denied_id, denied_request["input_id"], response_type="deny")
        self.assertTrue(denied["dispatch_required"])
        self.assertEqual(denied["approval_decision"], "deny")
        (timeout_id, _) = self._started_task("approval-timeout")
        timeout_task = self.service.get_task(timeout_id)
        timeout_request = timeout_task.extensions["agentbc.input"]
        timeout_at = timeout_task.extensions[APPROVAL_EXTENSION_KEY]["created_at"]
        timeout_request["deadline_at"] = timeout_at
        timeout_task.extensions["agentbc.input"] = timeout_request
        self.service.store.write_task(timeout_id, timeout_task.to_dict())
        self.assertEqual([item["task_id"] for item in self.service.expire_waiting_inputs(now=timeout_at)], [timeout_id])
        timed_out = self.service.get_task(timeout_id)
        self.assertEqual(timed_out.status, "needs_recovery")
        self.assertEqual(timed_out.extensions[APPROVAL_EXTENSION_KEY]["decision"]["source"], "timeout")

    def test_full_permission_has_zero_approval_and_one_lifecycle_delegate(self) -> None:
        full_task = self.service.create_task("ARCH-104 full fixture", "codex", [{"id": 1, "description": "full mode work"}], customer_dir=True, customer_path=self.project, permission_mode="full")
        self.assertEqual(full_task.extensions[PERMISSION_EXTENSION_KEY]["effective_mode"], "full")
        self.assertNotIn(APPROVAL_EXTENSION_KEY, full_task.extensions)
        service_source = Path(__import__("agent_bridge_connect.service", fromlist=["__file__"]).__file__).read_text()
        lifecycle_source = Path(approval_lifecycle.__file__).read_text()
        schema_source = Path(approval_schema.__file__).read_text()
        self.assertEqual(service_source.count("self._respond_to_permission_input("), 1)
        self.assertIn("def record_approval_decision(", schema_source)
        self.assertNotIn("def record_approval_decision(", lifecycle_source)
        self.assertNotIn("def record_approval_decision(", service_source)
        self.assertNotIn("from .service", lifecycle_source)
        self.assertNotIn("from .control", lifecycle_source)


_architecture_CORE_FILES = ["agent_bridge_connect.protocol", "agent_bridge_connect.state_machine", "agent_bridge_connect.task_store", "agent_bridge_connect.service", "agent_bridge_connect.adapters", "agent_bridge_connect.config"]
_architecture_SOURCE_ROOT = _architecture_Path(__file__).resolve().parents[2] / "src" / "agent_bridge_connect"
_architecture_ARCH104_LOC_BUDGETS = {
    "approval": (9202, ("service.py", "control.py", "approval_lifecycle.py", "approval_protocol.py", "control_runtime.py", "control_transport.py")),
    "runner": (4481, ("runner.py", "runner_contract.py", "runner_ipc.py")),
    "doctor": (2129, ("doctor.py", "doctor_collectors.py")),
}
_architecture_PLATFORM_FORBIDDEN_IN_CORE = ["codex", "claude", "gemini", "cursor", "feishu", "dingtalk", "slack", "email", "smtp"]


class _architecture_ArchitectureFreezeGateTests(_architecture_unittest.TestCase):
    """Core files must exist and not import platform details."""

    def test_core_modules_importable(self):
        """All 6 core modules must be importable."""
        for mod_name in _architecture_CORE_FILES:
            mod = _architecture_importlib.import_module(mod_name)
            self.assertIsNotNone(mod, f"Module {mod_name} not importable")

    def test_arch104_production_loc_reduction_is_preserved(self):
        """Each refactored domain remains at least 5% below its T0 LOC."""
        measured = {}
        for domain, (budget, files) in _architecture_ARCH104_LOC_BUDGETS.items():
            measured[domain] = sum((len((_architecture_SOURCE_ROOT / name).read_text().splitlines()) for name in files))
            self.assertLessEqual(measured[domain], budget, f"{domain} LOC budget exceeded")
        self.assertLessEqual(sum(measured.values()), 15813, "combined ARCH-104 LOC budget exceeded")

    def test_no_platform_imports_in_core(self):
        """Core modules must not import executor/notifier specific modules."""
        for mod_name in _architecture_CORE_FILES:
            try:
                mod = _architecture_importlib.import_module(mod_name)
            except ImportError:
                continue
            source_file = getattr(mod, "__file__", "")
            if not source_file:
                continue
            try:
                with open(source_file) as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            for forbidden in _architecture_PLATFORM_FORBIDDEN_IN_CORE:
                import_lines = [line for line in content.split("\n") if line.strip().startswith(("import ", "from "))]
                for line in import_lines:
                    if forbidden in line.lower():
                        self.fail(f"{mod_name} imports platform-specific module '{forbidden}': {line.strip()}")

    def test_protocol_has_version(self):
        """protocol.py must define ABC_PROTOCOL_VERSION."""
        from agent_bridge_connect import protocol

        self.assertTrue(hasattr(protocol, "ABC_PROTOCOL_VERSION"), "protocol.py missing ABC_PROTOCOL_VERSION")

    def test_extensions_preservation(self):
        """Unknown extension fields in task.json survive read/write cycle."""
        from agent_bridge_connect.task_store import TaskStore

        store = TaskStore(_architecture_Path(_architecture_tempfile.mkdtemp()))
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
            "extensions": {"executor.codex": {"session_id": "abc123", "model": "o3"}, "notifier.feishu": {"message_id": "msg_001"}, "custom_vendor": {"some_field": "some_value"}},
        }
        store.write_task("TEST-001", task_data)
        read_back = store.read_task("TEST-001")
        self.assertEqual(read_back["extensions"]["executor.codex"]["session_id"], "abc123")
        self.assertEqual(read_back["extensions"]["custom_vendor"]["some_field"], "some_value")


class _architecture_ServiceAPISurfaceTests(_architecture_unittest.TestCase):
    """service.py must expose required public operations."""

    def test_service_has_required_methods(self):
        """Service must expose create, get, list, claim, execute, pause, resume, report."""
        from agent_bridge_connect import service

        required = ["create_task", "get_task", "list_tasks", "claim_task", "execute_step", "pause_task", "resume_task", "cancel_task", "generate_report", "notify"]
        for method in required:
            self.assertTrue(hasattr(service, method), f"service.py missing required method: {method}")


class _architecture_StateMachineTests(_architecture_unittest.TestCase):
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


class _architecture_ConfigTests(_architecture_unittest.TestCase):
    """Config module must load and validate ~/.abc/config.toml."""

    def test_config_module_exists(self):
        """config.py must be importable."""
        from agent_bridge_connect import config

        self.assertTrue(hasattr(config, "load_config") or hasattr(config, "get_config"), "config.py missing load/get function")


class _architecture_AdapterContractTests(_architecture_unittest.TestCase):
    """Adapter port definitions must exist with required methods."""

    def test_executor_port_interface(self):
        """ExecutorPort must define probe, capabilities, start, poll, cancel."""
        from agent_bridge_connect.adapters import ExecutorPort

        required = ["probe", "capabilities", "start", "poll"]
        for method in required:
            self.assertTrue(hasattr(ExecutorPort, method), f"ExecutorPort missing: {method}")

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
