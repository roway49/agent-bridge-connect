"""Phase 5 Task 5 public cleanup projection and doctor diagnostics."""

from __future__ import annotations
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from agent_bridge_connect.cli import _print_execution_policy
from agent_bridge_connect.doctor import build_session_cleanup_diagnostics, collect_session_cleanup_diagnostics, render_doctor_text
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY, execution_policy_view, session_cleanup_view
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, task_to_status
import contextlib as _task_public_view_contextlib
import io as _task_public_view_io
import json as _task_public_view_json
import tempfile as _task_public_view_tempfile
import unittest as _task_public_view_unittest
from pathlib import Path as _task_public_view_Path
from unittest import mock as _task_public_view_mock
from agent_bridge_connect.cli import _print_atomic_dispatch as _task_public_view__print_atomic_dispatch, _print_task_status as _task_public_view__print_task_status
from agent_bridge_connect.execution_policy import SESSION_EXTENSION_KEY as _task_public_view_SESSION_EXTENSION_KEY
from agent_bridge_connect.reports import generate_report as _task_public_view_generate_report, generate_report_md as _task_public_view_generate_report_md, generate_task_brief as _task_public_view_generate_task_brief
from agent_bridge_connect.runner import RunnerState as _task_public_view_RunnerState
from agent_bridge_connect.protocol import PreflightResult as _task_public_view_PreflightResult
from agent_bridge_connect.service import TaskService as _task_public_view_TaskService, task_to_status as _task_public_view_task_to_status

T0 = "2026-08-11T00:00:00Z"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _receipt(state: str, *, capability: str = "supported", attempts: int = 1, error_code: str = "", retryable: bool = False, last_attempt_at: str = T0) -> dict:
    strategy = "official_session_delete"
    completed_at = ""
    next_attempt_at = ""
    if state == "retained":
        capability = "not_applicable"
        strategy = "retain"
        attempts = 0
        completed_at = T0
    elif state == "succeeded":
        completed_at = T0
    elif state == "unsupported":
        capability = "unsupported"
        strategy = "none"
        completed_at = T0
        error_code = error_code or "session_cleanup_unsupported"
    elif state == "failed":
        error_code = error_code or "session_cleanup_failed"
        next_attempt_at = "2026-08-11T00:10:00Z" if retryable else ""
    return {
        "version": 1,
        "capability": capability,
        "strategy": strategy,
        "state": state,
        "attempts": attempts,
        "requested_at": T0 if attempts else "",
        "last_attempt_at": last_attempt_at if attempts else "",
        "next_attempt_at": next_attempt_at,
        "completed_at": completed_at,
        "error_code": error_code,
        "retryable": retryable,
    }


def _doctor_task(task_id: str, executor: str, receipt: dict) -> dict:
    return {"id": task_id, "extensions": {SESSION_EXTENSION_KEY: {"executor": executor, "session_id": "private-session-id", "project_path": "/private/customer/session", "raw_output": "secret executor output", "native_argv": ["delete", "--force"], "cleanup": receipt}}}


class PublicCleanupProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(self.board, config={"workspace_root": str(self.root / "workspace"), "sessions": {"retain_executor_sessions": False}})
        self.task = self.service.create_task("Public cleanup projection", "hermes", [{"id": 1, "description": "compare public cleanup views"}], customer_dir=False)

    def _store_receipt(self, receipt: dict) -> None:
        raw = self.service.store.read_task(self.task.id)
        raw["extensions"][SESSION_EXTENSION_KEY]["cleanup"] = copy.deepcopy(receipt)
        self.service.store.write_task(self.task.id, raw)

    def test_status_preflight_and_report_share_exact_safe_projection(self) -> None:
        receipt = _receipt("failed", error_code="session_cleanup_failed", retryable=True)
        self._store_receipt(receipt)
        status = task_to_status(self.service.get_task(self.task.id))
        preflight = self.service.preflight(self.task.id)
        report = generate_report(self.task.id, self.board)
        expected = {"capability": "supported", "state": "failed", "attempts": 1, "error_code": "session_cleanup_failed", "retryable": True}
        self.assertEqual(status["execution_policy"]["session"]["cleanup"], expected)
        self.assertEqual(preflight.execution_policy["session"]["cleanup"], expected)
        self.assertEqual(report["execution_policy"]["session"]["cleanup"], expected)
        self.assertEqual(status["extensions"][SESSION_EXTENSION_KEY]["cleanup"], expected)
        encoded = json.dumps(status) + json.dumps(report)
        for internal in ("official_session_delete", "requested_at", "last_attempt_at", "next_attempt_at"):
            self.assertNotIn(internal, encoded)

    def test_legacy_receipt_is_read_only_and_gets_safe_defaults(self) -> None:
        legacy = {"state": "not_requested", "attempts": 0}
        before = copy.deepcopy(legacy)
        self._store_receipt(legacy)
        status = task_to_status(self.service.get_task(self.task.id))
        cleanup = status["execution_policy"]["session"]["cleanup"]
        preflight = self.service.preflight(self.task.id)
        report = generate_report(self.task.id, self.board)
        self.assertEqual(legacy, before)
        self.assertEqual(cleanup, session_cleanup_view(legacy))
        self.assertEqual(preflight.execution_policy["session"]["cleanup"], cleanup)
        self.assertEqual(report["execution_policy"]["session"]["cleanup"], cleanup)
        self.assertEqual(cleanup["capability"], "unknown")
        self.assertEqual(cleanup["error_code"], "")
        self.assertFalse(cleanup["retryable"])
        stored = self.service.store.read_task(self.task.id)
        self.assertEqual(stored["extensions"][SESSION_EXTENSION_KEY]["cleanup"], before)

    def test_invalid_sensitive_receipt_fields_fall_back_without_leaking(self) -> None:
        secret = "private-native-output-secret"
        invalid = _receipt("unsupported")
        invalid.update({"native_argv": ["delete", "--force"], "raw_output": secret, "executor_database_path": "/private/executor/sessions.db"})
        self._store_receipt(invalid)
        status = task_to_status(self.service.get_task(self.task.id))
        report = generate_report(self.task.id, self.board)
        rendered = json.dumps(status) + json.dumps(report)
        self.assertEqual(status["execution_policy"]["session"]["cleanup"], session_cleanup_view(invalid))
        for sensitive in (secret, "native_argv", "raw_output", "executor_database_path", "/private/executor/sessions.db", "--force"):
            self.assertNotIn(sensitive, rendered)

    def test_doctor_collector_reads_authoritative_task_receipt(self) -> None:
        self._store_receipt(_receipt("unsupported"))
        result = collect_session_cleanup_diagnostics(self.board, now="2026-08-11T00:10:00Z")
        self.assertEqual(result["warnings"], 1)
        self.assertEqual(result["diagnostics"][0]["task_id"], self.task.id)
        self.assertEqual(result["diagnostics"][0]["capability"], "unsupported")

    def test_text_status_and_markdown_report_render_only_public_cleanup_fields(self) -> None:
        self._store_receipt(_receipt("unsupported"))
        policy = execution_policy_view(self.service.get_task(self.task.id).extensions)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_execution_policy(policy)
        rendered = output.getvalue() + generate_report_md(self.task.id, self.board)
        self.assertIn("capability=unsupported", rendered)
        self.assertIn("Cleanup state: `unsupported`", rendered)
        self.assertNotIn("official_session_delete", rendered)
        self.assertNotIn("last_attempt_at", rendered)


class CleanupDoctorDiagnosticTests(unittest.TestCase):
    def test_unsupported_and_failed_are_structured_warnings(self) -> None:
        result = build_session_cleanup_diagnostics([_doctor_task("T-001", "codex", _receipt("unsupported")), _doctor_task("T-002", "hermes", _receipt("failed", error_code="session_cleanup_failed", retryable=True))], now="2026-08-11T00:10:00Z")
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["warnings"], 2)
        (unsupported, failed) = result["diagnostics"]
        self.assertIn("no official exact-session deletion capability", unsupported["message"])
        self.assertEqual(unsupported["capability"], "unsupported")
        self.assertEqual(failed["error_code"], "session_cleanup_failed")
        self.assertTrue(failed["retryable"])

    def test_pending_is_stale_only_after_five_minutes(self) -> None:
        task = _doctor_task("T-003", "claude", _receipt("pending"))
        boundary = build_session_cleanup_diagnostics([task], now="2026-08-11T00:05:00Z")
        stale = build_session_cleanup_diagnostics([task], now="2026-08-11T00:05:01Z")
        self.assertEqual(boundary["warnings"], 0)
        self.assertEqual(stale["warnings"], 1)
        self.assertIn("more than five minutes", stale["diagnostics"][0]["message"])

    def test_retained_and_succeeded_are_healthy_and_supported_is_authoritative(self) -> None:
        result = build_session_cleanup_diagnostics([_doctor_task("T-004", "claude", _receipt("retained")), _doctor_task("T-005", "codex", _receipt("succeeded")), _doctor_task("T-006", "hermes", _receipt("pending"))], now="2026-08-11T00:04:59Z")
        self.assertEqual(result["warnings"], 0)
        self.assertTrue(all((item["status"] == "healthy" for item in result["diagnostics"])))
        self.assertEqual(result["diagnostics"][1]["capability"], "supported")
        self.assertEqual(result["diagnostics"][2]["capability"], "supported")

    def test_text_uses_json_data_and_never_exposes_sensitive_session_inputs(self) -> None:
        result = build_session_cleanup_diagnostics([_doctor_task("T-007", "hermes", _receipt("failed"))], now="2026-08-11T00:10:00Z")
        report = {
            "schema_version": 1,
            "ok": True,
            "status": "warning",
            "package": {"version": "1", "commit_sha": None, "source_tree_sha256": None, "build_source": "test", "module_path": "module", "executable_path": "agentbc", "install_source": "test"},
            "config": {"path": "config", "exists": True, "workspace_root": "workspace"},
            "runner": {"status": "ready", "pid": 1, "python_executable": "python", "module_path": "module", "executors": ["hermes"]},
            "session_cleanup": result,
            "checks": [{"id": "session.cleanup", "status": "warning", "message": "One cleanup warning."}],
        }
        rendered = json.dumps(report) + render_doctor_text(report)
        for sensitive in ("private-session-id", "/private/customer/session", "secret executor output", "native_argv", "--force", "official_session_delete", "last_attempt_at"):
            self.assertNotIn(sensitive, rendered)


class Phase5DocumentationTests(unittest.TestCase):
    @staticmethod
    def _read(relative: str) -> str:
        return (PROJECT_ROOT / relative).read_text(encoding="utf-8")

    def test_guides_describe_background_executor_only_cleanup(self) -> None:
        english = self._read("docs/USER_GUIDE.md")
        chinese = self._read("docs/USER_GUIDE_ZH.md")
        for phrase in ("background and user-transparent", "temporary sessions", "never deletes the dispatcher conversation", "never requires users to manage a separate runtime directory"):
            self.assertIn(phrase, english)
        for phrase in ("后台无感", "Executor 创建的临时会话", "永远不会删除", "不会要求用户管理单独的 runtime 目录"):
            self.assertIn(phrase, chinese)

    def test_all_controller_skills_preserve_cleanup_boundary(self) -> None:
        for relative in ("src/agent_bridge_connect/skills/codex_skill.md", "src/agent_bridge_connect/skills/claude_skill.md", "src/agent_bridge_connect/skills/hermes_skill.md"):
            with self.subTest(relative=relative):
                skill = self._read(relative)
                self.assertIn("Executor", skill)
                self.assertTrue("dispatcher" in skill.lower() or "派发者" in skill)
                self.assertIn("runtime", skill)
                self.assertIn("cleanup", skill.lower())


class _task_public_view_Phase2PublicViewTests(_task_public_view_unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _task_public_view_tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = _task_public_view_Path(self.temporary.name)
        self.board = self.root / "record"
        self.config = {"workspace_root": str(self.root / "workspace"), "executors": {"claude": {"max_budget_usd": 12.0}}, "sessions": {"retain_executor_sessions": False}}
        self.service = _task_public_view_TaskService(self.board, config=self.config)

    def _task(self, executor: str = "claude"):
        return self.service.create_task("Public policy view", executor, [{"id": 1, "description": "render public views"}], customer_dir=False)

    def _inject_task2_path(self, task) -> tuple[str, str]:
        internal_root = str(self.root / "workspace" / "private-task2-root")
        session_path = str(self.root / "workspace" / "private-session-path")
        raw = self.service.store.read_task(task.id)
        raw["workspace"]["executor_project_root"] = internal_root
        raw["extensions"][_task_public_view_SESSION_EXTENSION_KEY]["project_path"] = session_path
        self.service.store.write_task(task.id, raw)
        return (internal_root, session_path)

    def test_status_json_text_and_preflight_share_path_free_policy(self) -> None:
        task = self._task()
        (internal_root, session_path) = self._inject_task2_path(task)
        stored = self.service.get_task(task.id)
        status = _task_public_view_task_to_status(stored)
        policy = status["execution_policy"]
        self.assertEqual(policy["resources"]["limit"], 12.0)
        self.assertEqual(policy["resources"]["source"], "configured")
        self.assertTrue(policy["resources"]["frozen"])
        self.assertEqual(policy["session"]["project_mode"], "ephemeral")
        self.assertNotIn("executor_project_root", status["workspace"])
        self.assertNotIn("project_path", status["extensions"][_task_public_view_SESSION_EXTENSION_KEY])
        self.assertNotIn(internal_root, _task_public_view_json.dumps(status))
        self.assertNotIn(session_path, _task_public_view_json.dumps(status))
        preflight = self.service.preflight(task.id)
        self.assertEqual(preflight.execution_policy, policy)
        output = _task_public_view_io.StringIO()
        with _task_public_view_contextlib.redirect_stdout(output):
            _task_public_view__print_task_status(status)
        rendered = output.getvalue()
        self.assertIn("Resources: max_budget_usd=12.0", rendered)
        self.assertIn("project_mode=ephemeral", rendered)
        self.assertNotIn(internal_root, rendered)
        self.assertNotIn(session_path, rendered)

    def test_preflight_result_extension_keeps_old_constructor_compatible(self) -> None:
        result = _task_public_view_PreflightResult(ok=True, errors=[])
        self.assertEqual(result.execution_policy, {})

    def test_report_json_markdown_and_review_brief_share_public_policy(self) -> None:
        task = self._task()
        (internal_root, session_path) = self._inject_task2_path(task)
        report = _task_public_view_generate_report(task.id, self.board)
        markdown = _task_public_view_generate_report_md(task.id, self.board)
        brief = _task_public_view_generate_task_brief(task.id, self.board)
        self.assertEqual(report["execution_policy"], brief["evidence"]["execution_policy"])
        self.assertEqual(report["execution_policy"]["resources"]["limit"], 12.0)
        self.assertNotIn("executor_project_root", report["workspace"])
        for rendered in (_task_public_view_json.dumps(report), _task_public_view_json.dumps(brief), markdown):
            self.assertNotIn(internal_root, rendered)
            self.assertNotIn(session_path, rendered)
        self.assertIn("## Execution Policy", markdown)
        self.assertIn("- Resource frozen: `yes`", markdown)

    def test_generated_task_requirements_show_policy_without_internal_path(self) -> None:
        task = self._task()
        text = _task_public_view_Path(task.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertIn("- Resource policy: `max_budget_usd`", text)
        self.assertIn("- Effective resource limit: `12.0`", text)
        self.assertIn("- Executor project mode: `ephemeral`", text)
        self.assertNotIn(task.extensions[_task_public_view_SESSION_EXTENSION_KEY]["project_path"], text)

    def test_atomic_accepted_view_is_sanitized_and_prints_policy(self) -> None:
        task = self._task()
        (internal_root, session_path) = self._inject_task2_path(task)
        task = self.service.get_task(task.id)
        state = object.__new__(_task_public_view_RunnerState)
        state.dispatch_worker = _task_public_view_mock.Mock(return_value={"run_id": "run-1", "dispatch_status": "accepted", "monitor_status": "disabled"})
        state._ensure_task_list_dashboard = _task_public_view_mock.Mock()
        result = state._atomic_dispatch_task(self.service, task, None, {})
        self.assertNotIn("executor_project_root", result["workspace"])
        self.assertEqual(result["execution_policy"]["resources"]["limit"], 12.0)
        encoded = _task_public_view_json.dumps(result)
        self.assertNotIn(internal_root, encoded)
        self.assertNotIn(session_path, encoded)
        output = _task_public_view_io.StringIO()
        with _task_public_view_contextlib.redirect_stdout(output):
            _task_public_view__print_atomic_dispatch(result)
        self.assertIn("Resources: max_budget_usd=12.0", output.getvalue())

    def test_codex_public_resources_are_null(self) -> None:
        task = self._task("codex")
        status = _task_public_view_task_to_status(task)
        self.assertIsNone(status["execution_policy"]["resources"])
        self.assertIsNotNone(status["execution_policy"]["session"])

    def test_ephemeral_claude_project_is_not_an_artifact(self) -> None:
        task = self._task()
        session_path = _task_public_view_Path(task.extensions[_task_public_view_SESSION_EXTENSION_KEY]["project_path"])
        session_path.mkdir(parents=True)
        (session_path / "receipt.json").write_text("{}", encoding="utf-8")
        artifact_root = _task_public_view_Path(task.workspace["artifact_root"])
        (artifact_root / "deliverable.txt").write_text("done", encoding="utf-8")
        report = _task_public_view_generate_report(task.id, self.board)
        self.assertIn("deliverable.txt", report["artifacts"])
        self.assertFalse(any(("receipt.json" in str(item) for item in report["artifacts"])))
        self.assertFalse(any((task.id in str(item) and "claude" in str(item) for item in report["artifacts"])))
