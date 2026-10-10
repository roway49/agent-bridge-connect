from __future__ import annotations
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
import io as _health_refresh_io
import json as _health_refresh_json
import tempfile as _health_refresh_tempfile
import unittest as _health_refresh_unittest
from contextlib import redirect_stdout as _health_refresh_redirect_stdout
from datetime import datetime as _health_refresh_datetime, timedelta as _health_refresh_timedelta, timezone as _health_refresh_timezone
from pathlib import Path as _health_refresh_Path
from unittest.mock import patch as _health_refresh_patch
from agent_bridge_connect.cli import main as _health_refresh_main
from agent_bridge_connect.protocol import TaskModel as _health_refresh_TaskModel
from agent_bridge_connect.service import TaskService as _health_refresh_TaskService
from agent_bridge_connect.task_health import (
    DEFAULT_LONG_STALE_AFTER_S as _health_refresh_DEFAULT_LONG_STALE_AFTER_S,
    DEFAULT_STALE_AFTER_S as _health_refresh_DEFAULT_STALE_AFTER_S,
    task_health as _health_refresh_task_health,
    task_run_temp_path as _health_refresh_task_run_temp_path,
    write_task_progress as _health_refresh_write_task_progress,
)


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
        return SimpleNamespace(id="ABCD-001", status=status, workspace={"report_root": str(self.report_root), "internal_task_dir": str(self.internal_task_dir)}, extensions={}, created_at=now, updated_at=now)

    def _touch_temp_age(self, task: SimpleNamespace, age_s: int) -> None:
        from agent_bridge_connect.task_health import task_run_temp_path

        path = task_run_temp_path(task)
        path.write_text("{}\n", encoding="utf-8")
        timestamp = time.time() - age_s
        os.utime(path, (timestamp, timestamp))

    def _write_lost_lease(self) -> None:
        (self.internal_task_dir / "run_lease.json").write_text(json.dumps({"state": "orphaned", "pid": 0}), encoding="utf-8")

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

        base = {"iteration": "001", "dispatcher": "codex", "assignee": "hermes", "created_at": "2026-07-09T00:00:00Z", "updated_at": "2026-07-09T00:01:00Z", "title": "Terminal task"}
        completed = _format_task_candidate({**base, "task_id": "DONE-001", "status": "completed", "health_color": "gray"}, color=True)
        failed = _format_task_candidate({**base, "task_id": "FAIL-001", "status": "needs_recovery", "health_color": "red"}, color=True)
        unconfirmed = _format_task_candidate({**base, "task_id": "LOST-001", "status": "failed", "health_color": "red"}, color=True)
        self.assertTrue(completed.startswith("DONE-001"))
        self.assertIn("\tcompleted\t", completed)
        self.assertTrue(failed.startswith("\x1b[31mFAIL-001\x1b[0m"))
        self.assertIn("\tneeds_recovery\t", failed)
        self.assertTrue(unconfirmed.startswith("\x1b[31mLOST-001\x1b[0m"))
        self.assertIn("\tfailed\t", unconfirmed)


class DashboardCohortTests(unittest.TestCase):
    def test_cohort_accumulates_tasks_and_is_cleared_on_close(self) -> None:
        from agent_bridge_connect.task_health import dashboard_task_ids, mark_dashboard_closed, register_dashboard_task

        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board"
            self.assertEqual(register_dashboard_task(board, "abcd-001", reset=True), ["ABCD-001"])
            self.assertEqual(register_dashboard_task(board, "efgh-001"), ["ABCD-001", "EFGH-001"])
            self.assertEqual(dashboard_task_ids(board), ["ABCD-001", "EFGH-001"])
            mark_dashboard_closed(board)
            self.assertEqual(dashboard_task_ids(board), [])

    def test_remove_task_preserves_empty_current_cohort(self) -> None:
        from agent_bridge_connect.task_health import dashboard_cohort_exists, dashboard_task_ids, register_dashboard_task, remove_dashboard_task

        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board"
            register_dashboard_task(board, "abcd-001", reset=True)
            self.assertEqual(remove_dashboard_task(board, "abcd-001"), [])
            self.assertTrue(dashboard_cohort_exists(board))
            self.assertEqual(dashboard_task_ids(board), [])

    def test_dashboard_state_records_current_protocol(self) -> None:
        from agent_bridge_connect.task_health import dashboard_paths, dashboard_protocol_matches, mark_dashboard_active, mark_dashboard_closed

        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board"
            mark_dashboard_active(board)
            self.assertTrue(dashboard_protocol_matches(board))
            state = json.loads(dashboard_paths(board)["state"].read_text(encoding="utf-8"))
            state.pop("protocol_version")
            dashboard_paths(board)["state"].write_text(json.dumps(state), encoding="utf-8")
            self.assertFalse(dashboard_protocol_matches(board))
            mark_dashboard_closed(board)


class _health_refresh_TaskHealthProgressWindowTests(_health_refresh_unittest.TestCase):
    """Yellow means no progress arrived within the observation window, not task failure."""

    def setUp(self) -> None:
        self.temporary_directory = _health_refresh_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.board_root = _health_refresh_Path(self.temporary_directory.name) / "record"
        self.service = _health_refresh_TaskService(self.board_root)
        self.task = self._store_running_task()

    def _store_running_task(self) -> _health_refresh_TaskModel:
        now = _health_refresh_datetime.now(_health_refresh_timezone.utc).isoformat().replace("+00:00", "Z")
        task = _health_refresh_TaskModel(
            id="Y2GR-001",
            title="Verify yellow-to-green health refresh",
            status="running",
            assignee="codex",
            steps=[{"id": 1, "description": "verify progress health", "status": "pending"}],
            created_by="codex",
            created_at=now,
            updated_at=now,
            workspace={"internal_task_dir": str(self.board_root / "Y2GR" / "001"), "task_code": "Y2GR", "iteration": "001", "root": self.temporary_directory.name, "project_root": self.temporary_directory.name, "artifacts_dir": self.temporary_directory.name},
            extensions={"agentbc.lineage": {"task_code": "Y2GR", "iteration_index": 1, "chain_root_task_id": "Y2GR-001"}},
        )
        self.service.store.write_task(task.id, task.to_dict())
        return task

    def _set_progress_age(self, age_seconds: int) -> None:
        progress_path = _health_refresh_task_run_temp_path(self.task)
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(
            _health_refresh_json.dumps({"task_id": self.task.id, "state": "running", "updated_at": (_health_refresh_datetime.now(_health_refresh_timezone.utc) - _health_refresh_timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z"), "message": "waiting for the next progress update", "source": "agent"})
            + "\n",
            encoding="utf-8",
        )

    def test_health_thresholds_keep_the_progress_observation_window(self) -> None:
        self.assertEqual(_health_refresh_DEFAULT_STALE_AFTER_S, 300)
        self.assertEqual(_health_refresh_DEFAULT_LONG_STALE_AFTER_S, 600)
        now = _health_refresh_datetime(2026, 8, 2, 12, 0, tzinfo=_health_refresh_timezone.utc)
        for age_seconds, expected_state, expected_color in ((300, "responsive", "green"), (301, "unresponsive", "yellow"), (600, "unresponsive", "yellow"), (601, "long_unresponsive", "orange")):
            with self.subTest(age_seconds=age_seconds):
                progress_path = _health_refresh_task_run_temp_path(self.task)
                progress_path.parent.mkdir(parents=True, exist_ok=True)
                progress_path.write_text(_health_refresh_json.dumps({"task_id": self.task.id, "state": "running", "updated_at": (now - _health_refresh_timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")}) + "\n", encoding="utf-8")
                with _health_refresh_patch("agent_bridge_connect.task_health.time.time", return_value=now.timestamp()):
                    health = _health_refresh_task_health(self.task)
                self.assertEqual(health["state"], expected_state)
                self.assertEqual(health["color"], expected_color)

    def test_direct_health_turns_green_after_progress_arrives(self) -> None:
        self._set_progress_age(301)
        stale_health = _health_refresh_task_health(self.task)
        self.assertEqual(stale_health["state"], "unresponsive")
        self.assertEqual(stale_health["color"], "yellow")
        _health_refresh_write_task_progress(self.task, state="running", message="progress received inside the observation window", source="agent")
        refreshed_health = _health_refresh_task_health(self.task)
        self.assertEqual(refreshed_health["state"], "responsive")
        self.assertEqual(refreshed_health["color"], "green")
        self.assertLessEqual(refreshed_health["last_progress_age_s"], _health_refresh_DEFAULT_STALE_AFTER_S)

    def test_cli_progress_refreshes_task_service_status_and_list_display(self) -> None:
        self._set_progress_age(301)
        stale_summary = self.service.list_task_summaries(all_iterations=True)[0]
        self.assertEqual(stale_summary["health_state"], "unresponsive")
        self.assertEqual(stale_summary["health_color"], "yellow")
        output = _health_refresh_io.StringIO()
        with _health_refresh_redirect_stdout(output):
            exit_code = _health_refresh_main(["task", "progress", self.task.id, "--root", str(self.board_root), "--summary", "normal CLI progress update"])
        self.assertEqual(exit_code, 0, output.getvalue())
        refreshed_service = _health_refresh_TaskService(self.board_root)
        refreshed_status = refreshed_service.resolve_task(self.task.id)["current_task"]
        self.assertEqual(refreshed_status["health"]["state"], "responsive")
        self.assertEqual(refreshed_status["health"]["color"], "green")
        refreshed_summary = refreshed_service.list_task_summaries(all_iterations=True)[0]
        self.assertEqual(refreshed_summary["health_state"], "responsive")
        self.assertEqual(refreshed_summary["health_color"], "green")
