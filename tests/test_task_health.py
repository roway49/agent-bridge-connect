from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


class TaskHealthTempCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.report_root = self.root / "report"
        self.internal_task_dir = self.root / "internal"
        self.report_root.mkdir()
        self.internal_task_dir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _task(self, *, status: str = "running") -> SimpleNamespace:
        now = "2026-07-09T00:00:00Z"
        return SimpleNamespace(
            id="ABCD-001",
            status=status,
            workspace={
                "report_root": str(self.report_root),
                "internal_task_dir": str(self.internal_task_dir),
            },
            extensions={},
            created_at=now,
            updated_at=now,
        )

    def _touch_temp_age(self, task: SimpleNamespace, age_s: int) -> None:
        from agent_bridge_connect.task_health import task_run_temp_path

        path = task_run_temp_path(task)
        path.write_text("{}\n", encoding="utf-8")
        timestamp = time.time() - age_s
        os.utime(path, (timestamp, timestamp))

    def _write_lost_lease(self) -> None:
        (self.internal_task_dir / "run_lease.json").write_text(
            json.dumps({"state": "orphaned", "pid": 0}),
            encoding="utf-8",
        )

    def test_temp_age_drives_green_yellow_orange(self) -> None:
        from agent_bridge_connect.task_health import task_health

        task = self._task()
        self._touch_temp_age(task, 100)
        self.assertEqual(task_health(task)["color"], "green")

        self._touch_temp_age(task, 350)
        self.assertEqual(task_health(task)["color"], "yellow")

        self._touch_temp_age(task, 650)
        self.assertEqual(task_health(task)["color"], "orange")

    def test_runner_lost_only_turns_red_after_temp_is_stale(self) -> None:
        from agent_bridge_connect.task_health import task_health

        task = self._task()
        self._write_lost_lease()

        self._touch_temp_age(task, 100)
        self.assertEqual(task_health(task)["color"], "green")

        self._touch_temp_age(task, 350)
        health = task_health(task)
        self.assertEqual(health["color"], "red")
        self.assertEqual(health["state"], "runner_lost")

    def test_missing_temp_gets_startup_grace_then_yellow(self) -> None:
        from agent_bridge_connect.task_health import task_health, utc_now

        task = self._task()
        task.updated_at = utc_now()
        self.assertEqual(task_health(task)["color"], "green")

        task.updated_at = "2026-07-09T00:00:00Z"
        self.assertEqual(task_health(task)["color"], "yellow")


class TaskListDisplayTests(unittest.TestCase):
    def test_task_list_summary_uses_colored_task_id_shape(self) -> None:
        from agent_bridge_connect.cli import _format_task_candidate

        line = _format_task_candidate(
            {
                "task_id": "ABCD-001",
                "iteration": "001",
                "dispatcher": "codex",
                "assignee": "hermes",
                "created_at": "2026-07-09T00:00:00Z",
                "updated_at": "2026-07-09T00:01:00Z",
                "status": "running",
                "title": "Short title",
                "health_color": "green",
                "project_root": "/tmp/project",
                "report_file": "/tmp/report.md",
            },
            color=False,
            timer_now="2026-07-09T00:02:00Z",
        )

        self.assertEqual(line, "ABCD-001\t001\tcodex -> hermes\t2m00s\tShort title")
        self.assertNotIn("/tmp/project", line)
        self.assertNotIn("/tmp/report.md", line)

    def test_completed_and_failed_tasks_use_terminal_labels(self) -> None:
        from agent_bridge_connect.cli import _format_task_candidate

        base = {
            "iteration": "001",
            "dispatcher": "codex",
            "assignee": "hermes",
            "created_at": "2026-07-09T00:00:00Z",
            "updated_at": "2026-07-09T00:01:00Z",
            "title": "Terminal task",
        }
        completed = _format_task_candidate(
            {**base, "task_id": "DONE-001", "status": "completed", "health_color": "gray"},
            color=True,
        )
        failed = _format_task_candidate(
            {**base, "task_id": "FAIL-001", "status": "needs_recovery", "health_color": "red"},
            color=True,
        )
        unconfirmed = _format_task_candidate(
            {**base, "task_id": "LOST-001", "status": "failed", "health_color": "red"},
            color=True,
        )

        self.assertTrue(completed.startswith("DONE-001"))
        self.assertIn("\tcompleted\t", completed)
        self.assertTrue(failed.startswith("\033[31mFAIL-001\033[0m"))
        self.assertIn("\tneeds_recovery\t", failed)
        self.assertTrue(unconfirmed.startswith("\033[31mLOST-001\033[0m"))
        self.assertIn("\tfailed\t", unconfirmed)


class DashboardCohortTests(unittest.TestCase):
    def test_cohort_accumulates_tasks_and_is_cleared_on_close(self) -> None:
        from agent_bridge_connect.task_health import (
            dashboard_task_ids,
            mark_dashboard_closed,
            register_dashboard_task,
        )

        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board"
            self.assertEqual(register_dashboard_task(board, "abcd-001", reset=True), ["ABCD-001"])
            self.assertEqual(register_dashboard_task(board, "efgh-001"), ["ABCD-001", "EFGH-001"])
            self.assertEqual(dashboard_task_ids(board), ["ABCD-001", "EFGH-001"])
            mark_dashboard_closed(board)
            self.assertEqual(dashboard_task_ids(board), [])

    def test_remove_task_preserves_empty_current_cohort(self) -> None:
        from agent_bridge_connect.task_health import (
            dashboard_cohort_exists,
            dashboard_task_ids,
            register_dashboard_task,
            remove_dashboard_task,
        )

        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board"
            register_dashboard_task(board, "abcd-001", reset=True)
            self.assertEqual(remove_dashboard_task(board, "abcd-001"), [])
            self.assertTrue(dashboard_cohort_exists(board))
            self.assertEqual(dashboard_task_ids(board), [])

    def test_dashboard_state_records_current_protocol(self) -> None:
        from agent_bridge_connect.task_health import (
            dashboard_paths,
            dashboard_protocol_matches,
            mark_dashboard_active,
            mark_dashboard_closed,
        )

        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board"
            mark_dashboard_active(board)
            self.assertTrue(dashboard_protocol_matches(board))
            state = json.loads(dashboard_paths(board)["state"].read_text(encoding="utf-8"))
            state.pop("protocol_version")
            dashboard_paths(board)["state"].write_text(json.dumps(state), encoding="utf-8")
            self.assertFalse(dashboard_protocol_matches(board))
            mark_dashboard_closed(board)


if __name__ == "__main__":
    unittest.main()
