from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from agent_bridge_connect.execution_policy import (
    RESOURCE_EXTENSION_KEY,
    SESSION_EXTENSION_KEY,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService


class Phase2TaskPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.customer = self.root / "customer"
        self.customer.mkdir()

    def _service(self, **config: object) -> TaskService:
        return TaskService(
            self.board,
            config={"workspace_root": str(self.root / "workspace"), **config},
        )

    def _create(self, service: TaskService, executor: str):
        return service.create_task(
            "Phase 2 policy",
            executor,
            [{"id": 1, "description": "verify the frozen policy"}],
            customer_dir=True,
            customer_path=self.customer,
        )

    def test_custom_claude_policy_preassigns_uuid_and_native_project(self) -> None:
        service = self._service(
            executors={"claude": {"max_budget_usd": 12.5}},
            sessions={"retain_executor_sessions": True},
        )
        task = self._create(service, "claude")
        resources = task.extensions[RESOURCE_EXTENSION_KEY]
        session = task.extensions[SESSION_EXTENSION_KEY]

        self.assertEqual(resources["current_limit"], 12.5)
        self.assertEqual(resources["source"], "configured")
        self.assertTrue(session["retain"])
        self.assertEqual(session["project_mode"], "native")
        self.assertEqual(session["project_path"], str(self.customer.resolve()))
        self.assertEqual(str(uuid.UUID(session["session_id"])), session["session_id"])

    def test_default_hermes_and_codex_policies_are_frozen(self) -> None:
        hermes = self._create(self._service(), "hermes")
        resources = hermes.extensions[RESOURCE_EXTENSION_KEY]
        session = hermes.extensions[SESSION_EXTENSION_KEY]
        self.assertEqual(resources["current_limit"], 90)
        self.assertEqual(resources["source"], "hermes_default_90")
        self.assertFalse(session["retain"])
        self.assertEqual(session["session_id"], "")
        self.assertEqual(session["session_state"], "pending")

        codex = self._create(self._service(), "codex")
        self.assertNotIn(RESOURCE_EXTENSION_KEY, codex.extensions)
        self.assertEqual(codex.extensions[SESSION_EXTENSION_KEY]["session_id"], "")
        self.assertEqual(codex.extensions[SESSION_EXTENSION_KEY]["project_mode"], "none")

    def test_default_claude_uses_canonical_ephemeral_path(self) -> None:
        task = self._create(self._service(), "claude")
        resources = task.extensions[RESOURCE_EXTENSION_KEY]
        session = task.extensions[SESSION_EXTENSION_KEY]
        expected = (
            self.root
            / "workspace"
            / "tasks"
            / "artifacts"
            / task.workspace["task_date"]
            / task.workspace["task_code"]
            / task.id
            / "claude"
        ).resolve()
        self.assertEqual(resources["current_limit"], 10.0)
        self.assertEqual(resources["source"], "claude_default_10")
        self.assertFalse(session["retain"])
        self.assertEqual(session["project_mode"], "ephemeral")
        self.assertEqual(Path(session["project_path"]), expected)
        self.assertFalse(expected.exists())

    def test_existing_executor_project_root_wins_for_task2_compatibility(self) -> None:
        service = self._service()
        task = self._create(service, "codex")
        workspace = dict(task.workspace)
        workspace["executor_project_root"] = str(self.root / "task2" / task.id / "claude")
        from agent_bridge_connect.execution_policy import build_task_execution_policy

        _, session = build_task_execution_policy("claude", service.config, workspace)
        self.assertIsNotNone(session)
        self.assertEqual(session["project_path"], workspace["executor_project_root"])

    def test_present_invalid_config_fields_fail_closed(self) -> None:
        invalid = (
            {"executors": {"claude": {"max_budget_usd": True}}},
            {"executors": {"hermes": {"max_turns": 2.5}}},
            {"executors": {"claude": "invalid-table"}},
            {"sessions": {"retain_executor_sessions": "yes"}},
        )
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaises(ABCError) as raised:
                    self._create(self._service(**config), "claude")
                self.assertEqual(raised.exception.code, "config_invalid")

    def test_resume_retry_recover_and_redispatch_style_requeue_do_not_drift(self) -> None:
        config = {
            "executors": {"hermes": {"max_turns": 40}},
            "sessions": {"retain_executor_sessions": False},
        }
        service = self._service(**config)
        task = self._create(service, "hermes")
        frozen_resources = dict(task.extensions[RESOURCE_EXTENSION_KEY])
        frozen_session = dict(task.extensions[SESSION_EXTENSION_KEY])
        service.config["executors"]["hermes"]["max_turns"] = 99
        service.config["sessions"]["retain_executor_sessions"] = True

        raw = service.store.read_task(task.id)
        raw["status"] = "running"
        raw["intervention"]["paused"] = True
        raw["steps"][0]["status"] = "failed"
        service.store.write_task(task.id, raw)
        service.resume_task(task.id)
        service.retry_step(task.id, 1)

        raw = service.store.read_task(task.id)
        raw["status"] = "needs_recovery"
        service.store.write_task(task.id, raw)
        service.requeue_task(task.id)
        after = service.get_task(task.id)
        self.assertEqual(after.extensions[RESOURCE_EXTENSION_KEY], frozen_resources)
        self.assertEqual(after.extensions[SESSION_EXTENSION_KEY], frozen_session)

    def test_handoff_reads_current_config_for_the_new_target(self) -> None:
        service = self._service(
            executors={"claude": {"max_budget_usd": 15.0}},
            sessions={"retain_executor_sessions": False},
        )
        source = self._create(service, "claude")
        raw = service.store.read_task(source.id)
        raw["status"] = "completed"
        service.store.write_task(source.id, raw)
        service.config["executors"]["hermes"] = {"max_turns": 55}
        service.config["sessions"]["retain_executor_sessions"] = True

        handoff = service.handoff_task(source.id, "hermes")
        self.assertEqual(
            handoff.extensions[RESOURCE_EXTENSION_KEY]["current_limit"],
            55,
        )
        self.assertTrue(handoff.extensions[SESSION_EXTENSION_KEY]["retain"])
        self.assertEqual(handoff.extensions[SESSION_EXTENSION_KEY]["session_id"], "")

    def test_reassign_rebuilds_policy_and_records_only_public_views(self) -> None:
        service = self._service(
            executors={
                "claude": {"max_budget_usd": 11.0},
                "hermes": {"max_turns": 66},
            }
        )
        task = self._create(service, "claude")
        raw = service.store.read_task(task.id)
        raw["status"] = "needs_recovery"
        service.store.write_task(task.id, raw)

        service.reassign_task(task.id, "hermes")
        reassigned = service.get_task(task.id)
        self.assertNotIn(
            "project_path",
            service.store.read_events(task.id)[-1]["execution_policy_before"]["session"],
        )
        self.assertEqual(
            reassigned.extensions[RESOURCE_EXTENSION_KEY]["current_limit"],
            66,
        )
        self.assertEqual(reassigned.extensions[SESSION_EXTENSION_KEY]["executor"], "hermes")


if __name__ == "__main__":
    unittest.main()
