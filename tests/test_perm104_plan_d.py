"""PERM-104-002 Plan D production-contract regressions.

These tests exercise the narrow Plan D seams rather than reintroducing the
retired AgentBC containment/runtime capability chain:

* explicit and inherited full use each executor's native strongest CLI flag;
* only a trusted structured native event can create one v3 elevation wait;
* approval, denial, timeout, replay and restart preserve durable cardinality;
* handoff/retry keep an inherited full snapshot; and
* protocol-compatible executable paths are not version/help probed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.permission_elevation import permission_elevation_from_extensions
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    build_permission_record,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.runner import RunnerState
from agent_bridge_connect.service import (
    PERMISSION_DIALOG_TIMEOUT_RESPONSE,
    TaskService,
)
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY
from tests.contract_helpers import finalize_completed


class PlanDRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "board"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace"), "permission_mode": "safe"},
        )

    def _fake_executors(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for name in ("codex", "claude", "hermes"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
            result[name] = path
        return result

    def _task_run(self, executor: str, *, permission_mode: str = "safe") -> tuple[str, str, str]:
        project = self.root / f"project-{executor}-{len(list(self.root.glob('project-*')))}"
        project.mkdir()
        task = self.service.create_task(
            f"Plan D {executor}",
            executor,
            [{"id": 1, "description": "one"}],
            customer_dir=True,
            customer_path=project,
            permission_mode=permission_mode,
        )
        # Codex and Hermes receive their official session ID from their native
        # transports.  This unit fixture supplies that already-issued fact so
        # the Service lifecycle can be tested without fabricating an event.
        model = self.service.get_task(task.id)
        session = dict((model.extensions or {}).get(SESSION_EXTENSION_KEY) or {})
        if not str(session.get("session_id") or "").strip():
            session["session_id"] = f"{executor}-official-session"
            model.extensions = dict(model.extensions or {})
            model.extensions[SESSION_EXTENSION_KEY] = session
            self.service.store.write_task(model.id, model.to_dict())
        self.service.start_task_run(task.id, executor)
        run_id = f"{executor}-run-1"
        self.service.record_executor_run_started(task.id, run_id)
        current = self.service.get_task(task.id)
        session_id = str(
            ((current.extensions or {}).get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
        )
        return task.id, run_id, session_id

    def _native_block(self, task_id: str, run_id: str, session_id: str, *, request_id: str = "native-req-1") -> dict:
        return self.service.block_task_for_elevation(
            task_id,
            executor_run_id=run_id,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=f"native-fingerprint-{request_id}",
            executor=self.service.get_task(task_id).assignee,
            operation="write-project",
            summary="native structured permission event",
            authority={
                "executor": self.service.get_task(task_id).assignee,
                "protocol": "executor.native",
                "protocol_version": 7,
                "method": "requestApproval",
            },
            native_event="executor.native.permission_request",
            input_fingerprint=f"input-{request_id}",
            tool_name="Write",
            tool_use_id=f"tool-{request_id}",
        )

    def test_three_executors_explicit_and_inherited_full_use_native_flags(self) -> None:
        binaries = self._fake_executors()
        expected = {
            "codex": "--dangerously-bypass-approvals-and-sandbox",
            "claude": "--dangerously-skip-permissions",
            "hermes": "--yolo",
        }
        forbidden = {
            "--sandbox",
            "workspace-write",
            "--settings",
            "--add-dir",
            "--tools",
            "--allowedTools",
            "--disallowedTools",
            "sandbox-exec",
        }
        full = build_permission_record(explicit_mode="full")
        inherited = build_permission_record(inherited=full)
        self.assertEqual(inherited["selection_source"], "inherited_task")
        for executor, binary in binaries.items():
            for permission in (full, inherited):
                packet = {
                    "task_id": "P3FK-001",
                    "steps": [{"id": 1, "description": "one"}],
                    "workspace": {"root": str(self.root), "project_root": str(self.root)},
                    "extensions": {"agentbc.permission": dict(permission)},
                }
                if executor == "codex":
                    command, _ = CodexExecutor(command=str(binary), transport="direct")._build_command(
                        packet, "prompt", self.root, permission
                    )
                elif executor == "claude":
                    command = ClaudeExecutor(
                        command=str(binary),
                        transport="direct",
                        tools=["Bash"],
                        auto_approve_tools=["Bash"],
                    )._build_command("prompt", self.root, packet, permission)
                else:
                    command = HermesExecutor(
                        command=str(binary), transport="direct", profile="configured"
                    )._build_command("prompt", permission=permission, task_packet=packet)
                self.assertIn(expected[executor], command)
                self.assertTrue(forbidden.isdisjoint(command), (executor, command))

    def test_full_runner_authorization_has_no_second_permission_layer(self) -> None:
        binaries = self._fake_executors()
        runner = RunnerState(
            self.root / "runner-state",
            [self.root],
            binaries,
        )
        full = build_permission_record(explicit_mode="full")
        # The command and cwd are intentionally outside the configured roots;
        # full authorization stops at exact executor identity and native mode.
        outside = self.root / "not-a-task-root"
        for executor, binary in binaries.items():
            with self.subTest(executor=executor):
                runner._validate_request(
                    executor,
                    [str(binary), "native-command"],
                    outside,
                    permission=full,
                )
                self.assertEqual(
                    resolve_effective_permission(
                        {"extensions": {"agentbc.permission": full}}, executor, "run-1"
                    )["effective_mode"],
                    "full",
                )

    def test_only_trusted_native_event_creates_one_v3_wait_and_replays_after_restart(self) -> None:
        task_id, run_id, session_id = self._task_run("hermes")
        with self.assertRaises(ABCError) as raised:
            self.service.block_task_for_elevation(
                task_id,
                executor_run_id=run_id,
                session_id=session_id,
                request_id="untrusted-req",
                request_fingerprint="untrusted-fingerprint",
                executor="hermes",
                operation="write-project",
                authority={
                    "executor": "hermes",
                    "protocol": "executor.native",
                    "protocol_version": 7,
                    "method": "requestApproval",
                },
            )
        self.assertEqual(raised.exception.code, "permission_block_evidence_unavailable")

        first = self._native_block(task_id, run_id, session_id)
        duplicate = self._native_block(task_id, run_id, session_id)
        self.assertEqual(duplicate["input_id"], first["input_id"])
        self.assertTrue(duplicate["idempotent"])
        _, first_notice = self.service.reserve_task_elevation_notification(task_id)
        _, second_notice = self.service.reserve_task_elevation_notification(task_id)
        self.assertTrue(first_notice)
        self.assertFalse(second_notice)

        task = self.service.get_task(task_id)
        self.assertEqual(
            (task.extensions or {})["agentbc.permission_elevation"]["cardinality"]["notifications"],
            1,
        )
        answered = self.service.respond_to_input(
            task_id, first["input_id"], response_type="approve_full"
        )
        self.assertTrue(answered["dispatch_required"])
        restarted = TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace"), "permission_mode": "safe"},
        )
        replayed_answer = restarted.respond_to_input(
            task_id, first["input_id"], response_type="approve_full"
        )
        self.assertTrue(replayed_answer["idempotent"])
        self.assertTrue(replayed_answer["dispatch_required"])
        restarted.activate_task_elevation(
            task_id,
            executor_run_id="hermes-continuation-1",
            session_id=session_id,
        )
        after_activation = restarted.respond_to_input(
            task_id, first["input_id"], response_type="approve_full"
        )
        self.assertTrue(after_activation["idempotent"])
        self.assertFalse(after_activation["dispatch_required"])
        late_duplicate = restarted.block_task_for_elevation(
            task_id,
            executor_run_id=run_id,
            session_id=session_id,
            request_id="native-req-1",
            request_fingerprint="native-fingerprint-native-req-1",
            executor="hermes",
            operation="write-project",
            authority={
                "executor": "hermes",
                "protocol": "executor.native",
                "protocol_version": 7,
                "method": "requestApproval",
            },
            native_event="executor.native.permission_request",
        )
        self.assertTrue(late_duplicate["idempotent"])
        elevation = permission_elevation_from_extensions(
            restarted.get_task(task_id).extensions
        )
        assert elevation is not None
        self.assertEqual(elevation["continuation"]["count"], 1)
        self.assertNotIn("agentbc.permission_grant", restarted.get_task(task_id).extensions or {})

    def test_deny_and_timeout_create_no_full_worker_or_continuation(self) -> None:
        for response, message in (("deny", ""), ("deny", PERMISSION_DIALOG_TIMEOUT_RESPONSE)):
            with self.subTest(message=message or "deny"):
                task_id, run_id, session_id = self._task_run("codex")
                request = self._native_block(
                    task_id,
                    run_id,
                    session_id,
                    request_id=f"deny-{len(self.service.list_tasks())}",
                )
                result = self.service.respond_to_input(
                    task_id,
                    request["input_id"],
                    response_type=response,
                    message=message,
                )
                self.assertFalse(result["dispatch_required"])
                task = self.service.get_task(task_id)
                elevation = permission_elevation_from_extensions(task.extensions)
                assert elevation is not None
                self.assertEqual(elevation["state"]["status"], "denied")
                self.assertEqual(elevation["continuation"]["count"], 0)
                execution = (task.extensions or {}).get("agentbc.execution") or {}
                self.assertNotIn("worker_run_id", execution)

    def test_handoff_and_retry_preserve_inherited_full_selection(self) -> None:
        source = self.service.create_task(
            "Plan D handoff source",
            "codex",
            [{"id": 1, "description": "one"}],
            customer_dir=True,
            customer_path=self.root / "handoff-project",
            permission_mode="full",
        )
        self.service.start_task_run(source.id, "codex")
        self.assertTrue(finalize_completed(self.service, source.id))
        target = self.service.handoff_task(source.id, "hermes", message="continue")
        target_permission = (target.extensions or {})["agentbc.permission"]
        self.assertEqual(target_permission["effective_mode"], "full")
        self.assertEqual(target_permission["selection_source"], "inherited_task")

        self.service.start_task_run(target.id, "hermes")
        self.service.fail_task(target.id, "transport_lost", "native transport ended")
        self.service.retry_step(target.id, 1)
        retried = self.service.get_task(target.id)
        retried_permission = (retried.extensions or {})["agentbc.permission"]
        self.assertEqual(retried_permission["effective_mode"], "full")
        self.assertEqual(retried_permission["selection_source"], "inherited_task")

    def test_protocol_compatible_versions_are_not_probed_or_allowlisted(self) -> None:
        with mock.patch(
            "agent_bridge_connect.permission_modes.permission_flags",
            wraps=lambda executor, mode: [],
        ) as flags:
            assert_executor_permission_supported(
                "hermes", "full", self.root / "fork-without-version-string"
            )
            flags.assert_called_once_with("hermes", "full")


if __name__ == "__main__":
    unittest.main()
