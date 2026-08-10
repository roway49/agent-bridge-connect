from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.execution_policy import (
    RESOURCE_EXTENSION_KEY,
    SESSION_EXTENSION_KEY,
)
from agent_bridge_connect.record_management import (
    MAX_TASK_RECORD_BYTES,
    enforce_task_record_budget,
    task_record_size,
)
from agent_bridge_connect.service import TaskService


class Phase2RecordCompactionTests(unittest.TestCase):
    def test_terminal_compaction_preserves_complete_policy_under_ten_kib(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            board = root / "record"
            service = TaskService(
                board,
                config={
                    "workspace_root": str(root / "workspace"),
                    "executors": {"claude": {"max_budget_usd": 17.5}},
                    "sessions": {"retain_executor_sessions": False},
                },
            )
            task = service.create_task(
                "Policy compaction " + ("title " * 300),
                "claude",
                [{"id": 1, "description": "large " * 500}],
                customer_dir=False,
            )
            raw = service.store.read_task(task.id)
            resources = raw["extensions"][RESOURCE_EXTENSION_KEY]
            session = raw["extensions"][SESSION_EXTENSION_KEY]
            raw["status"] = "completed"
            raw["extensions"]["agentbc.test.large"] = {"payload": "x" * 20000}
            service.store.write_task(task.id, raw)

            task_dir = service.store.task_dir(task.id)
            size = enforce_task_record_budget(task_dir)
            compact = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))

            self.assertLessEqual(size, MAX_TASK_RECORD_BYTES)
            self.assertLessEqual(task_record_size(task_dir), MAX_TASK_RECORD_BYTES)
            self.assertEqual(compact["extensions"][RESOURCE_EXTENSION_KEY], resources)
            self.assertEqual(compact["extensions"][SESSION_EXTENSION_KEY], session)
            self.assertNotIn("agentbc.test.large", compact["extensions"])


if __name__ == "__main__":
    unittest.main()
