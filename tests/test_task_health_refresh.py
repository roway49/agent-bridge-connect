from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_bridge_connect.cli import main
from agent_bridge_connect.protocol import TaskModel
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.task_health import (
    DEFAULT_LONG_STALE_AFTER_S,
    DEFAULT_STALE_AFTER_S,
    task_health,
    task_run_temp_path,
    write_task_progress,
)


class TaskHealthProgressWindowTests(unittest.TestCase):
    """Yellow means no progress arrived within the observation window, not task failure."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.board_root = Path(self.temporary_directory.name) / "record"
        self.service = TaskService(self.board_root)
        self.task = self._store_running_task()

    def _store_running_task(self) -> TaskModel:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        task = TaskModel(
            id="Y2GR-001",
            title="Verify yellow-to-green health refresh",
            status="running",
            assignee="codex",
            steps=[{"id": 1, "description": "verify progress health", "status": "pending"}],
            created_by="codex",
            created_at=now,
            updated_at=now,
            workspace={
                "internal_task_dir": str(self.board_root / "Y2GR" / "001"),
                "task_code": "Y2GR",
                "iteration": "001",
                "root": self.temporary_directory.name,
                "project_root": self.temporary_directory.name,
                "artifacts_dir": self.temporary_directory.name,
            },
            extensions={
                "agentbc.lineage": {
                    "task_code": "Y2GR",
                    "iteration_index": 1,
                    "chain_root_task_id": "Y2GR-001",
                }
            },
        )
        self.service.store.write_task(task.id, task.to_dict())
        return task

    def _set_progress_age(self, age_seconds: int) -> None:
        progress_path = task_run_temp_path(self.task)
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(
            json.dumps(
                {
                    "task_id": self.task.id,
                    "state": "running",
                    "updated_at": (
                        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
                    ).isoformat().replace("+00:00", "Z"),
                    "message": "waiting for the next progress update",
                    "source": "agent",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_health_thresholds_keep_the_progress_observation_window(self) -> None:
        self.assertEqual(DEFAULT_STALE_AFTER_S, 300)
        self.assertEqual(DEFAULT_LONG_STALE_AFTER_S, 600)
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

        for age_seconds, expected_state, expected_color in (
            (300, "responsive", "green"),
            (301, "unresponsive", "yellow"),
            (600, "unresponsive", "yellow"),
            (601, "long_unresponsive", "orange"),
        ):
            with self.subTest(age_seconds=age_seconds):
                progress_path = task_run_temp_path(self.task)
                progress_path.parent.mkdir(parents=True, exist_ok=True)
                progress_path.write_text(
                    json.dumps(
                        {
                            "task_id": self.task.id,
                            "state": "running",
                            "updated_at": (now - timedelta(seconds=age_seconds))
                            .isoformat()
                            .replace("+00:00", "Z"),
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with patch("agent_bridge_connect.task_health.time.time", return_value=now.timestamp()):
                    health = task_health(self.task)
                self.assertEqual(health["state"], expected_state)
                self.assertEqual(health["color"], expected_color)

    def test_direct_health_turns_green_after_progress_arrives(self) -> None:
        self._set_progress_age(301)
        stale_health = task_health(self.task)
        self.assertEqual(stale_health["state"], "unresponsive")
        self.assertEqual(stale_health["color"], "yellow")

        write_task_progress(
            self.task,
            state="running",
            message="progress received inside the observation window",
            source="agent",
        )

        refreshed_health = task_health(self.task)
        self.assertEqual(refreshed_health["state"], "responsive")
        self.assertEqual(refreshed_health["color"], "green")
        self.assertLessEqual(refreshed_health["last_progress_age_s"], DEFAULT_STALE_AFTER_S)

    def test_cli_progress_refreshes_task_service_status_and_list_display(self) -> None:
        self._set_progress_age(301)
        stale_summary = self.service.list_task_summaries(all_iterations=True)[0]
        self.assertEqual(stale_summary["health_state"], "unresponsive")
        self.assertEqual(stale_summary["health_color"], "yellow")

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "task",
                    "progress",
                    self.task.id,
                    "--root",
                    str(self.board_root),
                    "--summary",
                    "normal CLI progress update",
                ]
            )
        self.assertEqual(exit_code, 0, output.getvalue())

        refreshed_service = TaskService(self.board_root)
        refreshed_status = refreshed_service.resolve_task(self.task.id)["current_task"]
        self.assertEqual(refreshed_status["health"]["state"], "responsive")
        self.assertEqual(refreshed_status["health"]["color"], "green")
        refreshed_summary = refreshed_service.list_task_summaries(all_iterations=True)[0]
        self.assertEqual(refreshed_summary["health_state"], "responsive")
        self.assertEqual(refreshed_summary["health_color"], "green")


if __name__ == "__main__":
    unittest.main()
