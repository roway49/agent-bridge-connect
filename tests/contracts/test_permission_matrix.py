"""PERM-104-002 final deterministic three-executor regression matrix.

The fixtures model the durable Core boundary only.  They never invoke a real
executor prompt, approve a live request, or perform a destructive action.
"""

from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.approval import compute_request_fingerprint
from agent_bridge_connect.claude_elevation import stable_input_digest
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.execution_policy import SESSION_RECEIPT_SOURCES
from agent_bridge_connect.notifications import notify_input_required
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY, permission_flags
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService
import json as _permission_regression_json
import tempfile as _permission_regression_tempfile
import unittest as _permission_regression_unittest
from pathlib import Path as _permission_regression_Path
from unittest import mock as _permission_regression_mock
from agent_bridge_connect.adapters import (
    DeliveryResult as _permission_regression_DeliveryResult,
    ExecutorCapabilities as _permission_regression_ExecutorCapabilities,
    ExecutorLevel as _permission_regression_ExecutorLevel,
    ExecutorPort as _permission_regression_ExecutorPort,
    PollResult as _permission_regression_PollResult,
    ProbeResult as _permission_regression_ProbeResult,
    SessionCleanupResult as _permission_regression_SessionCleanupResult,
    StartResult as _permission_regression_StartResult,
)
from agent_bridge_connect.cli import command_worker_run as _permission_regression_command_worker_run
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY as _permission_regression_PERMISSION_EXTENSION_KEY
from agent_bridge_connect.record_management import MAX_TASK_RECORD_BYTES as _permission_regression_MAX_TASK_RECORD_BYTES, task_record_size as _permission_regression_task_record_size
from agent_bridge_connect.service import TaskService as _permission_regression_TaskService


class FinalPermissionMatrixTests(unittest.TestCase):
    EXECUTORS = ("codex", "claude", "hermes")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.board = self.root / "record"
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = TaskService(self.board, config={"workspace_root": str(self.root), "permission_mode": "safe"})

    def _new_task(self, executor: str, *, permission_mode: str = "safe"):
        return self.service.create_task(f"PERM-104-002 deterministic {executor}", executor, [{"id": 1, "description": "run the deterministic fixture"}], customer_dir=True, customer_path=self.project, permission_mode=permission_mode)

    def _running_fixture(self, executor: str):
        task = self._new_task(executor)
        self.service.start_task_run(task.id, executor)
        run_id = f"{executor}-perm104-final-{task.id}"
        self.service.record_executor_run_started(task.id, run_id)
        before_receipt = self.service.get_task(task.id)
        session_id = str(before_receipt.extensions["agentbc.session"].get("session_id") or "") or f"{executor}-official-session-{task.id}"
        self.service.record_executor_session_started(task.id, run_id, {"version": 1, "executor": executor, "session_id": session_id, "resumed": False, "persistence": "persistent", "source": SESSION_RECEIPT_SOURCES[executor]})
        current = self.service.get_task(task.id)
        self.assertEqual(current.extensions["agentbc.session"].get("session_id"), session_id)
        return (current, run_id, session_id)

    def _elevation_kwargs(self, executor: str, run_id: str, session_id: str, *, request_id: str = "") -> dict:
        operation = "native_fixture"
        tool_input = {"fixture": "no-op", "executor": executor}
        request_id = request_id or f"{executor}-perm104-request"
        request_fingerprint = compute_request_fingerprint(executor=executor, session_id=session_id, tool_name=operation, tool_input=tool_input, extra={"request_id": request_id})
        action_fingerprint = compute_request_fingerprint(executor=executor, session_id=session_id, tool_name=operation, tool_input=tool_input)
        protocol = {"codex": "codex_app_server", "claude": "claude_agent_sdk", "hermes": "hermes_acp"}[executor]
        native_event = {"codex": "codex_app_server.requestApproval", "claude": "claude_sdk_can_use_tool", "hermes": "hermes_acp.session/request_permission"}[executor]
        return {
            "executor_run_id": run_id,
            "session_id": session_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "executor": executor,
            "operation": operation,
            "summary": "deterministic native permission fixture",
            "blocked_step_id": 1,
            "tool_name": operation,
            "tool_use_id": f"{executor}-tool-1",
            "input_fingerprint": stable_input_digest(tool_input),
            "action_fingerprint": action_fingerprint,
            "control_path": "deterministic_fixture",
            "native_event": native_event,
            "authority": {"executor": executor, "protocol": protocol, "protocol_version": 1, "method": "requestApproval"},
        }

    def _waiting_fixture(self, executor: str):
        (task, run_id, session_id) = self._running_fixture(executor)
        kwargs = self._elevation_kwargs(executor, run_id, session_id)
        blocked = self.service.block_task_for_elevation(task.id, **kwargs)
        self.assertEqual(blocked["status"], "input_required")
        return (task, run_id, session_id, kwargs, blocked)

    def test_full_handoff_and_retry_keep_native_full_for_all_executors(self) -> None:
        expected_flags = {"codex": ["--dangerously-bypass-approvals-and-sandbox"], "claude": ["--dangerously-skip-permissions"], "hermes": ["--yolo"]}
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor):
                source = self._new_task(executor, permission_mode="full")
                source.status = "completed"
                source.steps[0]["status"] = "done"
                self.service.store.write_task(source.id, source.to_dict())
                handed = self.service.handoff_task(source.id, executor, "continue the verified full task")
                permission = handed.extensions[PERMISSION_EXTENSION_KEY]
                self.assertEqual(permission["effective_mode"], "full")
                self.assertEqual(permission["selection_source"], "inherited_task")
                self.assertEqual(resolve_effective_permission(handed.to_dict(), executor, f"{executor}-handoff")["effective_mode"], "full")
                self.assertEqual(permission_flags(executor, "full"), expected_flags[executor])
                retried = self._new_task(executor, permission_mode="full")
                retried.status = "running"
                retried.steps[0]["status"] = "failed"
                self.service.store.write_task(retried.id, retried.to_dict())
                self.service.retry_step(retried.id, 1)
                after_retry = self.service.get_task(retried.id)
                self.assertEqual(after_retry.extensions[PERMISSION_EXTENSION_KEY]["effective_mode"], "full")
                self.assertEqual(resolve_effective_permission(after_retry.to_dict(), executor, f"{executor}-retry")["effective_mode"], "full")

    def test_duplicate_and_out_of_order_native_events_reserve_one_input_and_dialog(self) -> None:
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor):
                (task, _run_id, _session_id, kwargs, first) = self._waiting_fixture(executor)
                duplicate = self.service.block_task_for_elevation(task.id, **kwargs)
                self.assertTrue(duplicate["idempotent"])
                self.assertEqual(duplicate["input_id"], first["input_id"])
                late_kwargs = dict(kwargs)
                late_kwargs["request_id"] = f"{executor}-late-request"
                with self.assertRaises(ABCError) as raised:
                    self.service.block_task_for_elevation(task.id, **late_kwargs)
                self.assertEqual(raised.exception.code, "approval_already_pending")
                with mock.patch("agent_bridge_connect.notifiers.dialog.DialogNotifier.send", return_value=DeliveryResult(True, "deterministic dialog", "dialog:fixture", {"action": "dismissed"})) as dialog_send:
                    notify_input_required(self.service, task.id)
                    notify_input_required(self.service, task.id)
                self.assertEqual(dialog_send.call_count, 1)
                persisted = self.service.get_task(task.id)
                elevation = persisted.extensions["agentbc.permission_elevation"]
                self.assertEqual(elevation["cardinality"]["permission_requests"], 1)
                self.assertEqual(elevation["cardinality"]["notifications"], 1)
                events = self.service.store.read_events(task.id)
                self.assertEqual(sum((event.get("event_type") == "task.permission_elevation_notification_reserved" for event in events)), 1)

    def test_deny_and_timeout_start_no_continuation_for_all_executors(self) -> None:
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor, outcome="deny"):
                (task, run_id, session_id, _kwargs, blocked) = self._waiting_fixture(executor)
                denied = self.service.respond_to_input(task.id, blocked["input_id"], response_type="deny")
                self.assertFalse(denied["dispatch_required"])
                self.assertTrue(denied["permission_denied"])
                after = self.service.get_task(task.id)
                elevation = after.extensions["agentbc.permission_elevation"]
                self.assertEqual(after.status, "failed")
                self.assertEqual(elevation["state"]["status"], "denied")
                self.assertEqual(elevation["decision"]["type"], "deny")
                self.assertEqual(elevation["cardinality"]["human_decisions"], 1)
                self.assertEqual(elevation["cardinality"]["full_continuations"], 0)
                self.assertEqual(after.extensions["agentbc.session"]["run_ids"], [run_id])

    def test_approve_resumes_one_same_session_full_continuation_for_all_executors(self) -> None:
        for executor in self.EXECUTORS:
            with self.subTest(executor=executor):
                (task, run_id, session_id, _kwargs, blocked) = self._waiting_fixture(executor)
                approved = self.service.respond_to_input(task.id, blocked["input_id"], response_type="approve_full")
                self.assertTrue(approved["dispatch_required"])
                self.assertTrue(approved["same_session"])
                continuation_run_id = f"{executor}-perm104-final-continuation-{task.id}"
                self.service.record_executor_run_started(task.id, continuation_run_id)
                activated = self.service.activate_task_elevation(task.id, executor_run_id=continuation_run_id, session_id=session_id)
                self.assertEqual(activated["elevation_state"], "active")
                after = self.service.get_task(task.id)
                elevation = after.extensions["agentbc.permission_elevation"]
                self.assertEqual(elevation["continuation"]["count"], 1)
                self.assertEqual(elevation["continuation"]["session_id"], session_id)
                self.assertEqual(resolve_effective_permission(after.to_dict(), executor, continuation_run_id)["effective_mode"], "full")
                self.assertEqual(after.extensions["agentbc.session"]["run_ids"], [run_id, continuation_run_id])
                replay_activation = self.service.activate_task_elevation(task.id, executor_run_id=continuation_run_id, session_id=session_id)
                self.assertEqual(replay_activation["elevation_state"], "active")
                replay_answer = self.service.respond_to_input(task.id, blocked["input_id"], response_type="approve_full")
                self.assertEqual(replay_answer["status"], "already_answered")
                replayed = self.service.get_task(task.id)
                self.assertEqual(replayed.extensions["agentbc.permission_elevation"]["cardinality"]["full_continuations"], 1)
                replay = self.service.respond_to_input(task.id, blocked["input_id"], response_type="deny")
                self.assertEqual(replay["status"], "already_answered")
            with self.subTest(executor=executor, outcome="timeout"):
                (task, run_id, _session_id, _kwargs, blocked) = self._waiting_fixture(executor)
                expired = self.service.expire_waiting_inputs(now="2099-01-01T00:00:00Z")
                self.assertEqual([item["task_id"] for item in expired], [task.id])
                self.assertEqual(self.service.expire_waiting_inputs(now="2099-01-01T00:00:00Z"), [])
                after = self.service.get_task(task.id)
                elevation = after.extensions["agentbc.permission_elevation"]
                self.assertEqual(after.status, "failed")
                self.assertEqual(elevation["state"]["status"], "denied")
                self.assertEqual(elevation["decision"]["source"], "timeout")
                self.assertEqual(elevation["cardinality"]["human_decisions"], 1)
                self.assertEqual(elevation["cardinality"]["full_continuations"], 0)
                self.assertEqual(after.extensions["agentbc.session"]["run_ids"], [run_id])


class _permission_regression__FailedPermissionExecutor(_permission_regression_ExecutorPort):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.task_packet: dict | None = None

    def probe(self) -> _permission_regression_ProbeResult:
        return _permission_regression_ProbeResult(ok=True, message="ready")

    def capabilities(self) -> _permission_regression_ExecutorCapabilities:
        return _permission_regression_ExecutorCapabilities(level=_permission_regression_ExecutorLevel.L2, resume=True)

    def start(self, task_packet: dict) -> _permission_regression_StartResult:
        self.task_packet = task_packet
        return _permission_regression_StartResult(ok=True, run_id=f"codex-{task_packet['task_id']}-canary")

    def poll(self, run_id: str) -> _permission_regression_PollResult:
        event = {"event_type": "item.completed", "payload": {"type": "command_execution", "command": "private command " + "x" * 1200, "aggregated_output": "private output " + "y" * 1200}}
        return _permission_regression_PollResult(
            status="failed",
            progress={"events_seen": 15},
            result={
                "events": [event for _ in range(15)],
                "returncode": 0,
                "failure": {"kind": "completion_marker_permission_step_invalid", "layer": "flow_contract", "message": "permission input must identify exactly one blocked declared step", "retryable": False},
                "execution_session": {"version": 1, "executor": "codex", "session_id": self.session_id, "resumed": False, "persistence": "persistent", "source": "jsonl_thread_started"},
            },
        )


class _permission_regression__UnsupportedCleanupExecutor:
    def cleanup_session(self, request) -> _permission_regression_SessionCleanupResult:
        return _permission_regression_SessionCleanupResult(state="unsupported", capability="unsupported", strategy="none", error_code="codex_session_delete_unsupported", retryable=False)


class _permission_regression_PermissionCanaryRegressionTests(_permission_regression_unittest.TestCase):
    def test_large_permission_failure_preserves_policy_notifies_and_unblocks_cleanup(self) -> None:
        with _permission_regression_tempfile.TemporaryDirectory() as temporary:
            root = _permission_regression_Path(temporary)
            board = root / "record"
            project = root / "project"
            project.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(f'''workspace_root = "{root / "workspace"}"\npermission_mode = "inherit"\n[sessions]\nretain_executor_sessions = false\n''', encoding="utf-8")
            service = _permission_regression_TaskService(board, config={"workspace_root": str(root / "workspace"), "permission_mode": "inherit", "sessions": {"retain_executor_sessions": False}})
            task = service.create_task("permission failure canary", "codex", [{"id": 1, "description": "write the canary"}, {"id": 2, "description": "verify the canary"}], customer_dir=True, customer_path=project)
            session_id = "019ffa32-55d5-79e3-8448-06f3a8ea3dfa"
            executor = _permission_regression__FailedPermissionExecutor(session_id)
            with _permission_regression_mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor), _permission_regression_mock.patch("agent_bridge_connect.notifications.DialogNotifier.send", return_value=_permission_regression_DeliveryResult(True, "shown")):
                code = _permission_regression_command_worker_run(_permission_regression_mock.Mock(root=board, executor="codex", once=True, interval=0.01, config=config_path, detach=False, task_id=task.id, runner_authorize=False))
            failed = service.get_task(task.id)
            self.assertEqual(code, 1)
            self.assertIsNotNone(executor.task_packet)
            self.assertEqual(executor.task_packet["assignee"], "codex")
            self.assertEqual(failed.status, "failed")
            persisted_permission = failed.extensions[_permission_regression_PERMISSION_EXTENSION_KEY]
            self.assertEqual(persisted_permission["version"], 2)
            self.assertEqual(persisted_permission["requested_mode"], "inherit")
            self.assertEqual(persisted_permission["effective_mode"], "inherit")
            self.assertEqual(persisted_permission["selection_source"], "configured_default")
            self.assertEqual(persisted_permission["scope"], "task")
            self.assertEqual(persisted_permission["permission_args"], [])
            self.assertIn("hermes", persisted_permission["mapping"])
            self.assertLessEqual(_permission_regression_task_record_size(service.store.task_dir(task.id)), _permission_regression_MAX_TASK_RECORD_BYTES)
            self.assertTrue(_permission_regression_Path(failed.workspace["report_file"]).is_file())
            events = service.store.read_events(task.id)
            self.assertTrue(any((event.get("event_type") == "notification_delivery" and event.get("notification_event") == "task.failed" for event in events)))
            persisted = _permission_regression_json.loads((service.store.task_dir(task.id) / "task.json").read_text(encoding="utf-8"))
            serialized = _permission_regression_json.dumps(persisted, ensure_ascii=False)
            self.assertNotIn("private command", serialized)
            self.assertNotIn("private output", serialized)
            from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator
            from agent_bridge_connect.run_lease import close_lease, create_lease

            lease = create_lease(task.id, "codex", 0, str(project))
            close_lease(lease, board)
            result = SessionCleanupCoordinator(board, executor_port=_permission_regression__UnsupportedCleanupExecutor()).request_cleanup(task.id)
            self.assertEqual(result["status"], "waiting_for_desktop", result)
            self.assertEqual(result["receipt"]["state"], "pending", result)
            self.assertEqual(service.get_task(task.id).extensions["agentbc.session"]["cleanup"]["state"], "pending")


_DOCKER_ROOT = Path(__file__).resolve().parents[2]
_DOCKER_SPEC = importlib.util.spec_from_file_location('docker_sandbox_probe', _DOCKER_ROOT / 'scripts/probe_docker_codex_sandbox.py')
_DOCKER_PROBE = importlib.util.module_from_spec(_DOCKER_SPEC)
_DOCKER_SPEC.loader.exec_module(_DOCKER_PROBE)


class DockerSandboxContractTests(unittest.TestCase):
    def test_profile_is_only_declared_additions_to_frozen_baseline(self):
        folder = _DOCKER_ROOT / 'deploy/docker/seccomp'
        baseline = json.loads((folder / 'moby-default.json').read_text())
        additions = json.loads((folder / 'codex-bwrap-rules.json').read_text())
        baseline['syscalls'] += additions
        self.assertEqual(baseline, json.loads((folder / 'codex-bwrap.json').read_text()))
        self.assertEqual(baseline['defaultAction'], 'SCMP_ACT_ERRNO')
        for rule in additions:
            name, = rule['names']
            self.assertIn(name, ('clone', 'unshare', 'mount', 'umount2', 'pivot_root'))
            if name != 'pivot_root':
                self.assertEqual(len(rule['args']), 1)
                self.assertEqual(rule['args'][0]['op'], 'SCMP_CMP_EQ')
        for name, expected in [('clone', 0x78020011), ('unshare', 0x10000000), ('umount2', 2)]:
            values = [r['args'][0]['value'] for r in additions if r['names'] == [name]]
            self.assertEqual(values, [expected])

    def test_manifest_hashes(self):
        folder = _DOCKER_ROOT / 'deploy/docker/seccomp'
        manifest = json.loads((folder / 'manifest.json').read_text())
        for name, digest in manifest['sha256'].items():
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest)

    def test_validation_rejects_missing_or_failed_commands(self):
        self.assertTrue(_DOCKER_PROBE.validate({}))
        self.assertTrue(_DOCKER_PROBE.validate({'outside_unchanged': True, 'control_network_reachable': True,
                                       'cases': {'read-only': {'exit': 1}, 'workspace-write': {'exit': 0}}}))

    def test_validation_rejects_boundary_escape(self):
        checks = {k: {'errno': 30} for k in ('project', 'outside', 'symlink', 'git', 'codex', 'agents', 'secret_read', 'network')}
        checks.update(unshare_mount={'return': -1}, mount={'return': -1},
                      status={'Uid': '1000 1000 1000 1000', 'CapEff': '0000000000000000', 'NoNewPrivs': '1', 'Seccomp': '2'})
        result = {'outside_unchanged': True, 'control_network_reachable': True,
                  'cases': {m: {'exit': 0, 'checks': copy.deepcopy(checks)} for m in ('read-only', 'workspace-write')}}
        result['cases']['workspace-write']['checks']['project'] = 'allowed'
        self.assertEqual(_DOCKER_PROBE.validate(result), [])
        result['cases']['workspace-write']['checks']['symlink'] = 'allowed'
        self.assertTrue(_DOCKER_PROBE.validate(result))
