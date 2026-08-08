"""REPORT-001 / OBS-001 fake-clock tests for the shared timing view.

Execution duration must be accumulated from authoritative RunLease run intervals
rather than wall time minus only input waiting. Waiting, paused, pending,
needs_recovery and recovery-ready periods never count as execution. The current
lease state always derives from ``run_lease.json``; stale extension snapshots are
historical evidence only. Historical tasks without interval fields return
``unknown`` instead of manufactured precision.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.cli import _decorate_task_status
from agent_bridge_connect.notifications import build_notification_payload
from agent_bridge_connect.reports import generate_report
from agent_bridge_connect.run_lease import (
    RunLeaseState,
    create_lease,
    save_lease,
)
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.task_store import TaskStore
from agent_bridge_connect.timing_view import build_timing_view


def _hour(value: int) -> str:
    return f"2026-01-01T{value:02d}:00:00Z"


class TimingViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.board = self.base / "record"
        self.project = self.base / "project"
        self.project.mkdir()
        self.service = TaskService(
            self.board,
            config={"workspace_root": str(self.base / "workspace")},
        )
        self.task = self.service.create_task(
            "timing view",
            "shell",
            [{"id": 1, "description": "work"}],
            customer_path=self.project,
        )
        self.task_id = self.task.id
        self.store = TaskStore(self.board)

    def _task_dict(self) -> dict:
        return self.store.read_task(self.task_id)

    def _write_task(self, **overrides) -> dict:
        data = self._task_dict()
        data.update(overrides)
        self.store.write_task(self.task_id, data)
        return data

    def _set_ledger(self, intervals: list[dict]) -> None:
        data = self._task_dict()
        extensions = dict(data.get("extensions") or {})
        execution = dict(extensions.get("agentbc.execution") or {})
        execution["run_intervals"] = intervals
        extensions["agentbc.execution"] = execution
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)

    def _write_lease(
        self,
        run_id: str,
        started_at: str,
        ended_at: str,
        state: str = RunLeaseState.CLOSED,
    ) -> None:
        lease = create_lease(self.task_id, "shell", 0, str(self.project))
        lease.run_id = run_id
        lease.started_at = started_at
        lease.last_heartbeat_at = ended_at
        lease.state = state
        save_lease(lease, self.board)

    def _interval(self, run_id: str, start_hour: int, end_hour: int) -> dict:
        return {
            "run_id": run_id,
            "executor_id": "shell",
            "started_at": _hour(start_hour),
            "ended_at": _hour(end_hour),
            "duration_s": float(end_hour - start_hour) * 60 * 60,
            "state": RunLeaseState.CLOSED,
        }

    def _view(self, now: str | None = None) -> dict:
        return build_timing_view(self._task_dict(), self.board, now=now)

    def test_first_run_failure_recovery_second_run_completion(self) -> None:
        self._write_task(
            status="completed",
            created_at=_hour(0),
            updated_at=_hour(4),
        )
        self._set_ledger(
            [
                self._interval("run-fail", 0, 1),
                self._interval("run-final", 3, 4),
            ]
        )
        self._write_lease("run-final", _hour(3), _hour(4))

        view = self._view()
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 0)
        self.assertEqual(view["evidence_quality"], "authoritative")
        self.assertEqual(view["run_count"], 2)

    def test_long_recovery_wait_does_not_count_as_execution(self) -> None:
        self._write_task(
            status="needs_recovery",
            created_at=_hour(0),
            updated_at=_hour(20),
        )
        self._set_ledger([self._interval("run-fail", 0, 1)])
        self._write_lease("run-fail", _hour(0), _hour(1))

        view = self._view()
        # Wall time spans 20h but only the 1h run is execution.
        self.assertEqual(view["wall_duration_s"], 20 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 0)

    def test_input_required_waiting_is_excluded_and_separate(self) -> None:
        self._write_task(
            status="completed",
            created_at=_hour(0),
            updated_at=_hour(4),
        )
        extensions = dict(self._task_dict().get("extensions") or {})
        extensions["agentbc.input"] = {
            "input_id": "input-1",
            "created_at": _hour(1),
            "responded_at": _hour(3),
            "status": "answered",
            "summary": "approve",
        }
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._set_ledger(
            [
                self._interval("run-before-input", 0, 1),
                self._interval("run-after-input", 3, 4),
            ]
        )
        self._write_lease("run-after-input", _hour(3), _hour(4))

        view = self._view()
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)

    def test_pause_span_is_excluded_from_execution(self) -> None:
        self._write_task(
            status="completed",
            created_at=_hour(0),
            updated_at=_hour(4),
        )
        # One continuous lease that spans the pause; the pause must be removed.
        self._set_ledger([self._interval("run-one", 0, 4)])
        self._write_lease("run-one", _hour(0), _hour(4))
        self.store.append_intervention(
            self.task_id,
            {"type": "pause", "task_id": self.task_id, "created_at": _hour(1)},
        )
        self.store.append_intervention(
            self.task_id,
            {"type": "resume", "task_id": self.task_id, "created_at": _hour(3)},
        )

        view = self._view()
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 0)

    def test_multiple_failed_runs_accumulate(self) -> None:
        self._write_task(
            status="completed",
            created_at=_hour(0),
            updated_at=_hour(4),
        )
        self._set_ledger(
            [
                self._interval("run-fail-1", 0, 1),
                self._interval("run-fail-2", 1, 2),
                self._interval("run-ok", 3, 4),
            ]
        )
        self._write_lease("run-ok", _hour(3), _hour(4))

        view = self._view()
        self.assertEqual(view["execution_duration_s"], 3 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)
        self.assertEqual(view["run_count"], 3)

    def test_historical_task_without_intervals_returns_unknown(self) -> None:
        # No run_intervals ledger, no run_lease.json: must not fabricate time.
        self._write_task(
            status="completed",
            created_at="2025-12-01T00:00:00Z",
            updated_at="2025-12-02T00:00:00Z",
        )
        self._task_dict()["extensions"].pop("agentbc.execution", None)
        self.store.write_task(self.task_id, self._task_dict())

        view = self._view()
        self.assertIsNone(view["execution_duration_s"])
        self.assertIsNone(view["last_run_duration_s"])
        self.assertEqual(view["evidence_quality"], "unknown")
        self.assertEqual(view["run_count"], 0)
        # Wall lifecycle time is still reported.
        self.assertEqual(view["wall_duration_s"], 24 * 60 * 60)

    def test_stale_lease_snapshot_cannot_override_current_lease(self) -> None:
        # Stale extension snapshot claims "suspended"; authoritative lease is ACTIVE.
        self._write_task(status="running", created_at=_hour(0), updated_at=_hour(1))
        extensions = dict(self._task_dict().get("extensions") or {})
        execution = dict(extensions.get("agentbc.execution") or {})
        execution["lease_state"] = "suspended"
        extensions["agentbc.execution"] = execution
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._write_lease("run-active", _hour(0), _hour(1), state=RunLeaseState.ACTIVE)

        view = self._view(now=_hour(2))
        self.assertEqual(view["lease_state"], RunLeaseState.ACTIVE)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["evidence_quality"], "estimated")

    def test_missing_run_lease_reads_as_closed(self) -> None:
        # Stale extension snapshot claims "active" but no run_lease.json exists.
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(1))
        extensions = dict(self._task_dict().get("extensions") or {})
        execution = dict(extensions.get("agentbc.execution") or {})
        execution["lease_state"] = "active"
        extensions["agentbc.execution"] = execution
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)

        view = self._view()
        self.assertEqual(view["lease_state"], RunLeaseState.CLOSED)
        self.assertIsNone(view["execution_duration_s"])

    def test_status_report_list_notification_share_the_same_values(self) -> None:
        self._write_task(
            status="completed",
            created_at=_hour(0),
            updated_at=_hour(4),
        )
        self._set_ledger(
            [
                self._interval("run-before-input", 0, 1),
                self._interval("run-after-input", 3, 4),
            ]
        )
        extensions = dict(self._task_dict().get("extensions") or {})
        extensions["agentbc.input"] = {
            "input_id": "input-1",
            "created_at": _hour(1),
            "responded_at": _hour(3),
            "status": "answered",
            "summary": "approve",
        }
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._write_lease("run-after-input", _hour(3), _hour(4))

        model = self.service.get_task(self.task_id)
        report_timing = generate_report(self.task_id, self.board)["timing"]
        status_timing = _decorate_task_status(model, self.board)["timing"]
        list_timing = self.service.task_summary(self.task_id)["timing"]
        notification = build_notification_payload(
            self.service, self.task_id, "task.finalized", "done", "done"
        )

        for source in (report_timing, status_timing, list_timing):
            self.assertEqual(source["execution_duration_s"], 2 * 60 * 60)
            self.assertEqual(source["waiting_duration_s"], 2 * 60 * 60)
            self.assertEqual(source["wall_duration_s"], 4 * 60 * 60)
            self.assertEqual(source["last_run_duration_s"], 60 * 60)
            self.assertEqual(source["evidence_quality"], "authoritative")
            self.assertEqual(source["lease_state"], RunLeaseState.CLOSED)
        self.assertEqual(notification["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(notification["waiting_duration_s"], 2 * 60 * 60)
        self.assertEqual(notification["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(notification["last_run_duration_s"], 60 * 60)
        self.assertEqual(notification["execution_evidence"], "authoritative")

    def test_service_records_run_interval_on_terminal_finalize(self) -> None:
        self._write_lease("run-recorded", _hour(0), _hour(2))
        self.service.finalize_task_from_agent(
            self.task_id,
            {
                "version": 1,
                "task_id": self.task_id,
                "final_state": "completed",
                "summary": "done",
                "finished_at": _hour(2),
                "step_results": [{"id": 1, "status": "done"}],
            },
        )
        data = self.store.read_task(self.task_id)
        execution = data["extensions"]["agentbc.execution"]
        self.assertEqual(execution["run_intervals"][0]["run_id"], "run-recorded")
        self.assertEqual(execution["run_intervals"][0]["duration_s"], 2 * 60 * 60)
        view = build_timing_view(data, self.board)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["evidence_quality"], "authoritative")

    def test_active_lease_is_estimated_until_closed(self) -> None:
        self._write_task(status="running", created_at=_hour(0), updated_at=_hour(1))
        self._write_lease("run-active", _hour(0), _hour(1), state=RunLeaseState.ACTIVE)

        view = self._view(now=_hour(2))
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["evidence_quality"], "estimated")
        self.assertEqual(view["lease_state"], RunLeaseState.ACTIVE)

    def test_input_waiting_duration_behavior_is_preserved(self) -> None:
        # Multiple input requests across the ledger still sum into waiting only.
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(6))
        extensions = dict(self._task_dict().get("extensions") or {})
        extensions["agentbc.input_history"] = [
            {
                "input_id": "input-1",
                "created_at": _hour(1),
                "responded_at": _hour(2),
                "status": "answered",
            },
            {
                "input_id": "input-2",
                "created_at": _hour(3),
                "responded_at": _hour(4),
                "status": "answered",
            },
        ]
        extensions["agentbc.input"] = {
            "input_id": "input-3",
            "created_at": _hour(5),
            "responded_at": _hour(6),
            "status": "answered",
        }
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._set_ledger([self._interval("run", 0, 6)])

        view = self._view()
        self.assertEqual(view["waiting_duration_s"], 3 * 60 * 60)
        # Execution excludes all three waiting windows.
        self.assertEqual(view["execution_duration_s"], 3 * 60 * 60)
        self.assertEqual(view["wall_duration_s"], 6 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
