from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import (
    DeliveryResult,
    ExecutorCapabilities,
    ExecutorLevel,
    ExecutorPort,
    PollResult,
    ProbeResult,
    SessionCleanupResult,
    StartResult,
)
from agent_bridge_connect.cli import command_worker_run
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY
from agent_bridge_connect.record_management import MAX_TASK_RECORD_BYTES, task_record_size
from agent_bridge_connect.service import TaskService


class _FailedPermissionExecutor(ExecutorPort):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, message="ready")

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(level=ExecutorLevel.L2, resume=True)

    def start(self, task_packet: dict) -> StartResult:
        return StartResult(ok=True, run_id=f"codex-{task_packet['task_id']}-canary")

    def poll(self, run_id: str) -> PollResult:
        event = {
            "event_type": "item.completed",
            "payload": {
                "type": "command_execution",
                "command": "private command " + ("x" * 1200),
                "aggregated_output": "private output " + ("y" * 1200),
            },
        }
        return PollResult(
            status="failed",
            progress={"events_seen": 15},
            result={
                "events": [event for _ in range(15)],
                "returncode": 0,
                "failure": {
                    "kind": "completion_marker_permission_step_invalid",
                    "layer": "flow_contract",
                    "message": "permission input must identify exactly one blocked declared step",
                    "retryable": False,
                },
                "execution_session": {
                    "version": 1,
                    "executor": "codex",
                    "session_id": self.session_id,
                    "resumed": False,
                    "persistence": "persistent",
                    "source": "jsonl_thread_started",
                },
            },
        )


class _UnsupportedCleanupExecutor:
    def cleanup_session(self, request) -> SessionCleanupResult:
        return SessionCleanupResult(
            state="unsupported",
            capability="unsupported",
            strategy="none",
            error_code="codex_session_delete_unsupported",
            retryable=False,
        )


class PermissionCanaryRegressionTests(unittest.TestCase):
    def test_large_permission_failure_preserves_policy_notifies_and_unblocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "record"
            project = root / "project"
            project.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                f'workspace_root = "{root / "workspace"}"\n'
                'permission_mode = "inherit"\n'
                "[sessions]\nretain_executor_sessions = false\n",
                encoding="utf-8",
            )
            service = TaskService(
                board,
                config={
                    "workspace_root": str(root / "workspace"),
                    "permission_mode": "inherit",
                    "sessions": {"retain_executor_sessions": False},
                },
            )
            task = service.create_task(
                "permission failure canary",
                "codex",
                [
                    {"id": 1, "description": "write the canary"},
                    {"id": 2, "description": "verify the canary"},
                ],
                customer_dir=True,
                customer_path=project,
            )
            session_id = "019ffa32-55d5-79e3-8448-06f3a8ea3dfa"
            executor = _FailedPermissionExecutor(session_id)

            with (
                mock.patch("agent_bridge_connect.cli.get_executor", return_value=executor),
                mock.patch(
                    "agent_bridge_connect.notifications.DialogNotifier.send",
                    return_value=DeliveryResult(True, "shown"),
                ),
            ):
                code = command_worker_run(
                    mock.Mock(
                        root=board,
                        executor="codex",
                        once=True,
                        interval=0.01,
                        config=config_path,
                        detach=False,
                        task_id=task.id,
                        runner_authorize=False,
                    )
                )

            failed = service.get_task(task.id)
            self.assertEqual(code, 1)
            self.assertEqual(failed.status, "failed")
            persisted_permission = failed.extensions[PERMISSION_EXTENSION_KEY]
            self.assertEqual(persisted_permission["version"], 2)
            self.assertEqual(persisted_permission["requested_mode"], "inherit")
            self.assertEqual(persisted_permission["effective_mode"], "inherit")
            self.assertEqual(
                persisted_permission["selection_source"], "configured_default"
            )
            self.assertEqual(persisted_permission["scope"], "task")
            self.assertEqual(persisted_permission["permission_args"], [])
            self.assertIn("hermes", persisted_permission["mapping"])
            self.assertLessEqual(task_record_size(service.store.task_dir(task.id)), MAX_TASK_RECORD_BYTES)
            self.assertTrue(Path(failed.workspace["report_file"]).is_file())
            events = service.store.read_events(task.id)
            self.assertTrue(
                any(
                    event.get("event_type") == "notification_delivery"
                    and event.get("notification_event") == "task.failed"
                    for event in events
                )
            )
            persisted = json.loads(
                (service.store.task_dir(task.id) / "task.json").read_text(encoding="utf-8")
            )
            serialized = json.dumps(persisted, ensure_ascii=False)
            self.assertNotIn("private command", serialized)
            self.assertNotIn("private output", serialized)

            from agent_bridge_connect.session_cleanup import SessionCleanupCoordinator
            from agent_bridge_connect.run_lease import close_lease, create_lease

            lease = create_lease(task.id, "codex", 0, str(project))
            close_lease(lease, board)

            result = SessionCleanupCoordinator(
                board,
                executor_port=_UnsupportedCleanupExecutor(),
            ).request_cleanup(task.id)
            self.assertEqual(result["receipt"]["state"], "unsupported", result)
            self.assertEqual(
                service.get_task(task.id).extensions["agentbc.session"]["cleanup"]["state"],
                "unsupported",
            )


if __name__ == "__main__":
    unittest.main()
