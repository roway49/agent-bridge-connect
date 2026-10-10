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
from agent_bridge_connect.cli import _decorate_task_status, _task_list_timer
from agent_bridge_connect.notifications import build_notification_payload
from agent_bridge_connect.reports import generate_report
from agent_bridge_connect.run_lease import RunLeaseState, create_lease, save_lease
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.task_store import TaskStore
from agent_bridge_connect.timing_view import build_timing_view
import json as _report_json
import shutil as _report_shutil
import tempfile as _report_tempfile
import unittest as _report_unittest
from pathlib import Path as _report_Path
from unittest import mock as _report_mock
from tests.contract_helpers import finalize_completed as _report_finalize_completed


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
        self.service = TaskService(self.board, config={"workspace_root": str(self.base / "workspace")})
        self.task = self.service.create_task("timing view", "shell", [{"id": 1, "description": "work"}], customer_path=self.project)
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

    def _write_lease(self, run_id: str, started_at: str, ended_at: str, state: str = RunLeaseState.CLOSED) -> None:
        lease = create_lease(self.task_id, "shell", 0, str(self.project))
        lease.run_id = run_id
        lease.started_at = started_at
        lease.last_heartbeat_at = ended_at
        lease.state = state
        save_lease(lease, self.board)

    def _interval(self, run_id: str, start_hour: int, end_hour: int) -> dict:
        return {"run_id": run_id, "executor_id": "shell", "started_at": _hour(start_hour), "ended_at": _hour(end_hour), "duration_s": float(end_hour - start_hour) * 60 * 60, "state": RunLeaseState.CLOSED}

    def _view(self, now: str | None = None) -> dict:
        return build_timing_view(self._task_dict(), self.board, now=now)

    def test_first_run_failure_recovery_second_run_completion(self) -> None:
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(4))
        self._set_ledger([self._interval("run-fail", 0, 1), self._interval("run-final", 3, 4)])
        self._write_lease("run-final", _hour(3), _hour(4))
        view = self._view()
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 0)
        self.assertEqual(view["evidence_quality"], "authoritative")
        self.assertEqual(view["run_count"], 2)

    def test_long_recovery_wait_does_not_count_as_execution(self) -> None:
        self._write_task(status="needs_recovery", created_at=_hour(0), updated_at=_hour(20))
        self._set_ledger([self._interval("run-fail", 0, 1)])
        self._write_lease("run-fail", _hour(0), _hour(1))
        view = self._view()
        self.assertEqual(view["wall_duration_s"], 20 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 0)

    def test_input_required_waiting_is_excluded_and_separate(self) -> None:
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(4))
        extensions = dict(self._task_dict().get("extensions") or {})
        extensions["agentbc.input"] = {"input_id": "input-1", "created_at": _hour(1), "responded_at": _hour(3), "status": "answered", "summary": "approve"}
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._set_ledger([self._interval("run-before-input", 0, 1), self._interval("run-after-input", 3, 4)])
        self._write_lease("run-after-input", _hour(3), _hour(4))
        view = self._view()
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)

    def test_pause_span_is_excluded_from_execution(self) -> None:
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(4))
        self._set_ledger([self._interval("run-one", 0, 4)])
        self._write_lease("run-one", _hour(0), _hour(4))
        self.store.append_intervention(self.task_id, {"type": "pause", "task_id": self.task_id, "created_at": _hour(1)})
        self.store.append_intervention(self.task_id, {"type": "resume", "task_id": self.task_id, "created_at": _hour(3)})
        view = self._view()
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["waiting_duration_s"], 0)

    def test_multiple_failed_runs_accumulate(self) -> None:
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(4))
        self._set_ledger([self._interval("run-fail-1", 0, 1), self._interval("run-fail-2", 1, 2), self._interval("run-ok", 3, 4)])
        self._write_lease("run-ok", _hour(3), _hour(4))
        view = self._view()
        self.assertEqual(view["execution_duration_s"], 3 * 60 * 60)
        self.assertEqual(view["last_run_duration_s"], 60 * 60)
        self.assertEqual(view["run_count"], 3)

    def test_retry_attempt_timing_excludes_prior_attempt_from_task_list(self) -> None:
        self._write_task(status="running", created_at=_hour(0), updated_at=_hour(4))
        data = self._task_dict()
        extensions = dict(data.get("extensions") or {})
        execution = dict(extensions.get("agentbc.execution") or {})
        execution.update({"attempt_index": 1, "attempt_started_at": _hour(3), "run_intervals": [{**self._interval("run-original", 0, 1), "attempt_index": 0}, {**self._interval("run-retry", 3, 4), "attempt_index": 1}]})
        extensions["agentbc.execution"] = execution
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        view = self._view(now=_hour(4))
        self.assertEqual(view["wall_duration_s"], 4 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["attempt_index"], 1)
        self.assertEqual(view["attempt_wall_duration_s"], 60 * 60)
        self.assertEqual(view["attempt_execution_duration_s"], 60 * 60)
        self.assertEqual(view["attempt_run_count"], 1)
        self.assertEqual(_task_list_timer({"status": "running", "timing": view}), "1h00m00s")

    def test_historical_task_without_intervals_returns_unknown(self) -> None:
        self._write_task(status="completed", created_at="2025-12-01T00:00:00Z", updated_at="2025-12-02T00:00:00Z")
        self._task_dict()["extensions"].pop("agentbc.execution", None)
        self.store.write_task(self.task_id, self._task_dict())
        view = self._view()
        self.assertIsNone(view["execution_duration_s"])
        self.assertIsNone(view["last_run_duration_s"])
        self.assertEqual(view["evidence_quality"], "unknown")
        self.assertEqual(view["run_count"], 0)
        self.assertEqual(view["wall_duration_s"], 24 * 60 * 60)

    def test_stale_lease_snapshot_cannot_override_current_lease(self) -> None:
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

    def test_closed_lease_replaces_same_run_active_ledger_interval(self) -> None:
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(2))
        active = self._interval("run-final", 0, 1)
        active["state"] = RunLeaseState.ACTIVE
        self._set_ledger([active])
        self._write_lease("run-final", _hour(0), _hour(2), state=RunLeaseState.CLOSED)
        view = self._view()
        self.assertEqual(view["lease_state"], RunLeaseState.CLOSED)
        self.assertEqual(view["run_count"], 1)
        self.assertEqual(view["run_intervals"][0]["state"], RunLeaseState.CLOSED)
        self.assertEqual(view["execution_duration_s"], 2 * 60 * 60)
        self.assertEqual(view["evidence_quality"], "authoritative")

    def test_missing_run_lease_reads_as_closed(self) -> None:
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
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(4))
        self._set_ledger([self._interval("run-before-input", 0, 1), self._interval("run-after-input", 3, 4)])
        extensions = dict(self._task_dict().get("extensions") or {})
        extensions["agentbc.input"] = {"input_id": "input-1", "created_at": _hour(1), "responded_at": _hour(3), "status": "answered", "summary": "approve"}
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._write_lease("run-after-input", _hour(3), _hour(4))
        model = self.service.get_task(self.task_id)
        report_timing = generate_report(self.task_id, self.board)["timing"]
        status_timing = _decorate_task_status(model, self.board)["timing"]
        list_timing = self.service.task_summary(self.task_id)["timing"]
        notification = build_notification_payload(self.service, self.task_id, "task.finalized", "done", "done")
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
        self.service.finalize_task_from_agent(self.task_id, {"version": 1, "task_id": self.task_id, "final_state": "completed", "summary": "done", "finished_at": _hour(2), "step_results": [{"id": 1, "status": "done"}]})
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
        self._write_task(status="completed", created_at=_hour(0), updated_at=_hour(6))
        extensions = dict(self._task_dict().get("extensions") or {})
        extensions["agentbc.input_history"] = [{"input_id": "input-1", "created_at": _hour(1), "responded_at": _hour(2), "status": "answered"}, {"input_id": "input-2", "created_at": _hour(3), "responded_at": _hour(4), "status": "answered"}]
        extensions["agentbc.input"] = {"input_id": "input-3", "created_at": _hour(5), "responded_at": _hour(6), "status": "answered"}
        data = self._task_dict()
        data["extensions"] = extensions
        self.store.write_task(self.task_id, data)
        self._set_ledger([self._interval("run", 0, 6)])
        view = self._view()
        self.assertEqual(view["waiting_duration_s"], 3 * 60 * 60)
        self.assertEqual(view["execution_duration_s"], 3 * 60 * 60)
        self.assertEqual(view["wall_duration_s"], 6 * 60 * 60)


_report_FIXTURES = _report_Path(__file__).parents[1] / "fixtures"
_report_STEPS_YAML = _report_FIXTURES / "sample_steps.yaml"


class _report_ReportGenerationTests(_report_unittest.TestCase):
    """Test report.json and report.md generation."""

    def setUp(self):
        self.test_dir = _report_Path(_report_tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.board)
        self.task = create_task("Report test", "mock", _report_STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        _report_shutil.rmtree(self.test_dir)

    def _complete_task(self):
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done", "artifacts": ["src/user.py"], "diff": "+10/-0"})
        _report_finalize_completed(svc, self.task_id)
        return svc

    def test_report_json_structure(self):
        """report.json must contain required fields."""
        from agent_bridge_connect.reports import generate_report

        self._complete_task()
        report = generate_report(self.task_id, self.board)
        self.assertIn("task_id", report)
        self.assertIn("title", report)
        self.assertIn("status", report)
        self.assertIn("steps", report)
        self.assertIn("timeline", report)
        self.assertIn("artifacts", report)
        self.assertIn("created_at", report)
        self.assertIn("completed_at", report)

    def test_report_includes_steps(self):
        """Report must list all steps with their status."""
        from agent_bridge_connect.reports import generate_report

        self._complete_task()
        report = generate_report(self.task_id, self.board)
        self.assertEqual(len(report["steps"]), 3)
        step1 = report["steps"][0]
        self.assertEqual(step1["status"], "done")

    def test_report_includes_session_id(self):
        """Report must include session_id when present."""
        from agent_bridge_connect.task_board import create_task
        from agent_bridge_connect.reports import generate_report

        task = create_task("Session test", "mock", _report_STEPS_YAML, self.board, session_id="test-sess-001")
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(task.id, "mock")
        svc.execute_step(task.id, 1, {"status": "done"})
        _report_finalize_completed(svc, task.id)
        report = generate_report(task.id, self.board)
        self.assertEqual(report.get("session_id"), "test-sess-001")

    def test_report_markdown(self):
        """report.md must be a readable markdown string."""
        from agent_bridge_connect.reports import generate_report_md

        self._complete_task()
        md = generate_report_md(self.task_id, self.board)
        self.assertIsInstance(md, str)
        self.assertIn("# Report", md)
        self.assertIn("Report test", md)
        self.assertIn("mock", md)
        self.assertRegex(md, "- Created: `\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2} .+`")
        self.assertRegex(md, "- Completed: `\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2} .+`")
        self.assertIn("- Duration: `", md)

    def test_redaction_preserves_task_ids_and_paths_with_pwd_token(self):
        from agent_bridge_connect.reports import redact_secrets

        report = redact_secrets({"task_id": "T-001-PWDN", "workspace": {"report_file": "/tmp/report_T-001-PWDN.md", "task_file": "/tmp/task_T-001-PWDN.md"}, "step": "Create password_hash field with password=secret"})
        self.assertEqual(report["task_id"], "T-001-PWDN")
        self.assertEqual(report["workspace"]["report_file"], "/tmp/report_T-001-PWDN.md")
        self.assertIn("[REDACTED]", report["step"])
        self.assertNotIn("password", report["step"].lower())

    def test_report_includes_artifacts(self):
        """Report must list artifacts from step results."""
        from agent_bridge_connect.reports import generate_report

        self._complete_task()
        report = generate_report(self.task_id, self.board)
        self.assertIsInstance(report["artifacts"], list)

    def test_report_includes_interventions(self):
        """Report must include interventions when present."""
        from agent_bridge_connect.reports import generate_report

        _service = self._complete_task()
        report = generate_report(self.task_id, self.board)
        self.assertIn("interventions", report)

    def test_report_for_failed_task(self):
        """Report for failed task must include error info."""
        from agent_bridge_connect.service import TaskService
        from agent_bridge_connect.reports import generate_report

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})
        report = generate_report(self.task_id, self.board)
        self.assertEqual(report["task_id"], self.task_id)

    def test_failed_report_summarizes_error_without_dumping_raw_stdout(self):
        from agent_bridge_connect.reports import generate_report_md
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        raw_marker = "RAW_EXECUTOR_OUTPUT_" * 200
        svc.fail_task(self.task_id, "executor_failed", "executor returned status failed", {"executor": "hermes", "result": {"stdout": raw_marker, "stderr": "permission denied"}})
        markdown = generate_report_md(self.task_id, self.board)
        self.assertIn("executor_failed", markdown)
        self.assertIn("executor returned status failed", markdown)
        self.assertNotIn(raw_marker, markdown)

    def test_conversation_id_preserves_non_secret_desk_prefix(self):
        from agent_bridge_connect.reports import generate_report_md
        from agent_bridge_connect.service import TaskService

        task = TaskService(self.board, config={"workspace_root": str(self.board)}).create_task("Desk trace", "hermes", [{"id": 1, "description": "trace"}], session_id="desk-1780381467569-c5abba4d-e733-472b-af0f-85a03881c27c", source_platform="hermes", customer_dir=True, customer_path=self.board)
        report = generate_report_md(task.id, self.board)
        self.assertIn("desk-1780381467569-c5abba4d-e733-472b-af0f-85a03881c27c", report)
        self.assertNotIn("de[REDACTED]", report)


class _report_DispatcherTraceabilityTests(_report_unittest.TestCase):
    """Dispatcher Traceability section rendering in report Markdown."""

    def setUp(self):
        self.test_dir = _report_Path(_report_tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board

        init_board(self.board)

    def tearDown(self):
        _report_shutil.rmtree(self.test_dir)

    def _service_task(self, session_id=None, source_platform=None, assignee="codex"):
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board, config={"workspace_root": str(self.board)})
        task = svc.create_task("Dispatcher trace task", assignee, [{"id": 1, "description": "trace"}], session_id=session_id, source_platform=source_platform, customer_dir=False)
        return (task, svc)

    def _report_md(self, task_id):
        from agent_bridge_connect.reports import generate_report_md

        return generate_report_md(task_id, self.board)

    def test_report_md_renders_stable_dispatcher_traceability_labels(self):
        (task, _) = self._service_task(session_id="thread-123", source_platform="codex")
        md = self._report_md(task.id)
        self.assertIn("## Dispatcher Traceability", md)
        self.assertIn("- Dispatcher platform: `codex`", md)
        self.assertIn("- Dispatcher conversation ID: `thread-123`", md)
        self.assertNotIn("Session Traceability", md)
        self.assertNotIn("- Source platform:", md)
        self.assertNotIn("- Conversation ID:", md)
        self.assertNotIn("hermes session search", md)

    def test_report_md_renders_unavailable_when_dispatcher_id_missing(self):
        (task, _) = self._service_task(session_id=None, source_platform="codex")
        md = self._report_md(task.id)
        self.assertIn("## Dispatcher Traceability", md)
        self.assertIn("- Dispatcher platform: `codex`", md)
        self.assertIn("- Dispatcher conversation ID: `unavailable`", md)

    def test_report_md_platform_falls_back_to_assignee_for_old_records(self):
        from agent_bridge_connect.task_board import create_task

        task = create_task("Old record", "claude", _report_STEPS_YAML, self.board)
        md = self._report_md(task.id)
        self.assertIn("## Dispatcher Traceability", md)
        self.assertIn("- Dispatcher platform: `claude`", md)
        self.assertIn("- Dispatcher conversation ID: `unavailable`", md)

    def test_report_md_platform_falls_back_when_provenance_lacks_platform(self):
        (task, svc) = self._service_task(session_id="conv-9", source_platform=None)
        data = svc.store.read_task(task.id)
        data["extensions"]["agentbc.provenance"] = {"conversation_id": "conv-9"}
        svc.store.write_task(task.id, data)
        md = self._report_md(task.id)
        self.assertIn("- Dispatcher platform: `codex`", md)
        self.assertIn("- Dispatcher conversation ID: `conv-9`", md)

    def test_report_md_renders_unavailable_for_old_record_without_any_trace(self):
        (task, svc) = self._service_task(session_id=None, source_platform=None)
        data = svc.store.read_task(task.id)
        data["extensions"]["agentbc.provenance"] = {}
        data["assignee"] = ""
        svc.store.write_task(task.id, data)
        md = self._report_md(task.id)
        self.assertIn("## Dispatcher Traceability", md)
        self.assertIn("- Dispatcher platform: `unavailable`", md)
        self.assertIn("- Dispatcher conversation ID: `unavailable`", md)

    def test_redaction_keeps_conversation_id_but_redacts_secrets(self):
        from agent_bridge_connect.reports import redact_secrets

        fake_key = "sk-" + "abcdefghijklmnopqrstuvwx"
        value = redact_secrets({"session_id": "019f92f8-9e92-7251-a792-8a6390e0d380", "provenance": {"source_platform": "codex", "conversation_id": "019f92f8-9e92-7251-a792-8a6390e0d380"}, "workspace": {"config": "password=secret", "token": fake_key}})
        self.assertEqual(value["session_id"], "019f92f8-9e92-7251-a792-8a6390e0d380")
        self.assertEqual(value["provenance"]["conversation_id"], "019f92f8-9e92-7251-a792-8a6390e0d380")
        self.assertNotIn("secret", value["workspace"]["config"].lower())
        self.assertNotIn(fake_key, value["workspace"]["token"])

    def test_report_md_redacts_secret_like_conversation_id(self):
        fake_key = "sk-" + "abcdefghijklmnopqrstuvwx"
        (task, _) = self._service_task(session_id=fake_key, source_platform="codex")
        md = self._report_md(task.id)
        self.assertIn("## Dispatcher Traceability", md)
        self.assertNotIn(fake_key, md)


class _report_NotificationTests(_report_unittest.TestCase):
    """Test notification delivery on task events."""

    def setUp(self):
        self.test_dir = _report_Path(_report_tempfile.mkdtemp())
        self.board = self.test_dir / "abc-tasks"
        from agent_bridge_connect.task_board import init_board, create_task

        init_board(self.board)
        self.task = create_task("Notify test", "mock", _report_STEPS_YAML, self.board)
        self.task_id = self.task.id

    def tearDown(self):
        _report_shutil.rmtree(self.test_dir)

    def test_file_notifier_delivers(self):
        """File notifier must write to notifications.jsonl."""
        from agent_bridge_connect.notifiers.file import FileNotifier

        notifier = FileNotifier(self.board / "notifications.jsonl")
        result = notifier.send({"task_id": self.task_id, "event_type": "task.completed", "title": "Notify test", "level": "done", "message": "Task completed successfully"})
        self.assertTrue(result.ok)
        self.assertTrue((self.board / "notifications.jsonl").exists())

    def test_file_notifier_content(self):
        """File notifier must write valid JSON lines."""
        from agent_bridge_connect.notifiers.file import FileNotifier

        notifier = FileNotifier(self.board / "notifications.jsonl")
        notifier.send({"task_id": self.task_id, "event_type": "task.completed", "title": "Test", "level": "done", "message": "done"})
        content = (self.board / "notifications.jsonl").read_text().strip()
        line = _report_json.loads(content.split("\n")[-1])
        self.assertEqual(line["task_id"], self.task_id)

    def test_dialog_notifier_creates_notification_object(self):
        """Dialog notifier must accept a notification dict and return DeliveryResult."""
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with _report_mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = _report_mock.Mock(returncode=0, stdout="button returned:Open Report, gave up:false", stderr="")
            result = notifier.send({"task_id": self.task_id, "event_type": "task.completed", "title": "Dialog test", "level": "done", "message": "Task completed", "report_path": "/tmp/REPORT.md"})
        self.assertTrue(result.ok)
        self.assertEqual(notifier.timeout_s, 30)
        self.assertEqual(notifier.input_timeout_s, 300)
        self.assertEqual(run.call_args_list[0].args[0][2], "Agent-Bridge-Connect")
        self.assertEqual(run.call_args_list[0].args[0][3], "Task completed")

    def test_dialog_notifier_opens_report_when_requested(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with _report_mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.side_effect = [_report_mock.Mock(returncode=0, stdout="button returned:Open Report, gave up:false", stderr=""), _report_mock.Mock(returncode=0, stdout="", stderr="")]
            result = notifier.send({"task_id": self.task_id, "event_type": "task.completed", "title": "Dialog test", "level": "done", "message": "Task completed", "report_path": "/tmp/REPORT.md"})
        self.assertTrue(result.ok)
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/open", "/tmp/REPORT.md"])
        self.assertIn("Open Report", result.message)

    def test_dialog_notifier_collects_message_input(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with _report_mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = _report_mock.Mock(returncode=0, stdout="button returned:Submit\ngave up:false\ntext returned:选择方案 A", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "Choose an option", "input_type": "message"})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "message", "message": "选择方案 A"})
        self.assertIn('default answer ""', run.call_args.kwargs["input"])
        self.assertIn('buttons {"Later", "Submit"}', run.call_args.kwargs["input"])

    def test_dialog_notifier_renders_two_choice_buttons_for_five_minutes(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with _report_mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = _report_mock.Mock(returncode=0, stdout="button returned:Option B\ngave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "Choose an option", "input_type": "choice", "input_options": ["Option A", "Option B"]})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "message", "message": "Option B"})
        self.assertEqual(run.call_args.args[0], ["/usr/bin/osascript", "-", "Agent-Bridge-Connect", "Choose an option", "Option A", "Option B"])
        self.assertEqual(run.call_args.kwargs["timeout"], 305)
        self.assertIn('buttons {"Later", (item 3 of argv), (item 4 of argv)}', run.call_args.kwargs["input"])
        self.assertIn("giving up after 300", run.call_args.kwargs["input"])

    def test_dialog_notifier_collects_permission_decision(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with _report_mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = _report_mock.Mock(returncode=0, stdout="button returned:Deny\ngave up:false", stderr="")
            result = notifier.send({"event_type": "task.input_required", "message": "Allow access?", "input_type": "permission"})
        self.assertTrue(result.ok)
        self.assertEqual(result.details, {"action": "deny", "decision_source": "user"})
        self.assertIn('buttons {"View Details", "Deny", "Approve"}', run.call_args.kwargs["input"])
        self.assertIn('default button "Deny"', run.call_args.kwargs["input"])
        self.assertNotIn('"Later"', run.call_args.kwargs["input"])

    def test_dialog_notifier_reports_osascript_failure(self):
        from agent_bridge_connect.notifiers.dialog import DialogNotifier

        notifier = DialogNotifier()
        with _report_mock.patch("agent_bridge_connect.notifiers.dialog.subprocess.run") as run:
            run.return_value = _report_mock.Mock(returncode=1, stdout="", stderr="execution error")
            result = notifier.send({"task_id": self.task_id, "event_type": "task.completed", "title": "Dialog test", "level": "done", "message": "Task completed"})
        self.assertFalse(result.ok)
        self.assertIn("osascript exited 1", result.message)

    def test_secret_redaction(self):
        """Notification content must not contain secrets."""
        from agent_bridge_connect.reports import generate_report_md
        from agent_bridge_connect.service import TaskService

        svc = TaskService(self.board)
        svc.claim_task(self.task_id, "mock")
        svc.execute_step(self.task_id, 1, {"status": "done"})
        _report_finalize_completed(svc, self.task_id)
        md = generate_report_md(self.task_id, self.board)
        self.assertNotIn("sk-", md)
        self.assertNotIn("password", md.lower())
