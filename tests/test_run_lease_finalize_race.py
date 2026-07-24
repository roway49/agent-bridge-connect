"""Regression tests for Executor-exit and Worker-finalization ordering."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class RunLeaseFinalizeRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from agent_bridge_connect.service import TaskService

        self.temporary = tempfile.TemporaryDirectory()
        self.board = Path(self.temporary.name) / "record"
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(Path(self.temporary.name) / "workspace")},
        )
        self.task = self.service.create_task(
            "Worker finalization race",
            "shell",
            [{"id": 1, "description": "finish normally"}],
            customer_dir=False,
        )
        task = self.service.store.read_task(self.task.id)
        task["status"] = "working"
        task.setdefault("extensions", {}).setdefault("agentbc.execution", {})[
            "worker_pid"
        ] = os.getpid()
        self.service.store.write_task(self.task.id, task)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _lost_executor_lease(self, *, age_s: int = 0) -> None:
        from agent_bridge_connect.run_lease import create_lease, save_lease

        lease = create_lease(self.task.id, "shell", 99_999_999, str(self.board))
        lease.last_heartbeat_at = (
            datetime.now(timezone.utc) - timedelta(seconds=age_s)
        ).isoformat().replace("+00:00", "Z")
        save_lease(lease, self.board)

    def test_live_worker_gets_finalize_grace_after_executor_exit(self) -> None:
        from agent_bridge_connect.run_lease import reconcile_task

        self._lost_executor_lease()

        self.assertEqual(reconcile_task(self.task.id, self.board), "active")
        current = self.service.store.read_task(self.task.id)
        self.assertEqual(current["status"], "working")
        self.assertFalse(
            any(
                event.get("event_type") == "task.failed"
                for event in self.service.store.read_events(self.task.id)
            )
        )

    def test_live_worker_becomes_stale_not_failed_after_grace(self) -> None:
        from agent_bridge_connect.run_lease import reconcile_task

        self._lost_executor_lease(age_s=60)

        self.assertEqual(reconcile_task(self.task.id, self.board), "stale")
        current = self.service.store.read_task(self.task.id)
        self.assertEqual(current["status"], "working")


if __name__ == "__main__":
    unittest.main()
