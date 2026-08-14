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
from agent_bridge_connect.doctor import (
    build_session_cleanup_diagnostics,
    collect_session_cleanup_diagnostics,
    render_doctor_text,
)
from agent_bridge_connect.execution_policy import (
    SESSION_EXTENSION_KEY,
    execution_policy_view,
    session_cleanup_view,
)
from agent_bridge_connect.reports import generate_report, generate_report_md
from agent_bridge_connect.service import TaskService, task_to_status


T0 = "2026-08-11T00:00:00Z"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _receipt(
    state: str,
    *,
    capability: str = "supported",
    attempts: int = 1,
    error_code: str = "",
    retryable: bool = False,
    last_attempt_at: str = T0,
) -> dict:
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
    return {
        "id": task_id,
        "extensions": {
            SESSION_EXTENSION_KEY: {
                "executor": executor,
                "session_id": "private-session-id",
                "project_path": "/private/customer/session",
                "raw_output": "secret executor output",
                "native_argv": ["delete", "--force"],
                "cleanup": receipt,
            }
        },
    }


class PublicCleanupProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board = self.root / "record"
        self.service = TaskService(
            self.board,
            config={
                "workspace_root": str(self.root / "workspace"),
                "sessions": {"retain_executor_sessions": False},
            },
        )
        self.task = self.service.create_task(
            "Public cleanup projection",
            "hermes",
            [{"id": 1, "description": "compare public cleanup views"}],
            customer_dir=False,
        )

    def _store_receipt(self, receipt: dict) -> None:
        raw = self.service.store.read_task(self.task.id)
        raw["extensions"][SESSION_EXTENSION_KEY]["cleanup"] = copy.deepcopy(receipt)
        self.service.store.write_task(self.task.id, raw)

    def test_status_preflight_and_report_share_exact_safe_projection(self) -> None:
        receipt = _receipt(
            "failed",
            error_code="session_cleanup_failed",
            retryable=True,
        )
        self._store_receipt(receipt)

        status = task_to_status(self.service.get_task(self.task.id))
        preflight = self.service.preflight(self.task.id)
        report = generate_report(self.task.id, self.board)
        expected = {
            "capability": "supported",
            "state": "failed",
            "attempts": 1,
            "error_code": "session_cleanup_failed",
            "retryable": True,
        }

        self.assertEqual(status["execution_policy"]["session"]["cleanup"], expected)
        self.assertEqual(preflight.execution_policy["session"]["cleanup"], expected)
        self.assertEqual(report["execution_policy"]["session"]["cleanup"], expected)
        self.assertEqual(
            status["extensions"][SESSION_EXTENSION_KEY]["cleanup"], expected
        )
        encoded = json.dumps(status) + json.dumps(report)
        for internal in (
            "official_session_delete",
            "requested_at",
            "last_attempt_at",
            "next_attempt_at",
        ):
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
        invalid.update(
            {
                "native_argv": ["delete", "--force"],
                "raw_output": secret,
                "executor_database_path": "/private/executor/sessions.db",
            }
        )
        self._store_receipt(invalid)

        status = task_to_status(self.service.get_task(self.task.id))
        report = generate_report(self.task.id, self.board)
        rendered = json.dumps(status) + json.dumps(report)

        self.assertEqual(
            status["execution_policy"]["session"]["cleanup"],
            session_cleanup_view(invalid),
        )
        for sensitive in (
            secret,
            "native_argv",
            "raw_output",
            "executor_database_path",
            "/private/executor/sessions.db",
            "--force",
        ):
            self.assertNotIn(sensitive, rendered)

    def test_doctor_collector_reads_authoritative_task_receipt(self) -> None:
        self._store_receipt(_receipt("unsupported"))

        result = collect_session_cleanup_diagnostics(
            self.board,
            now="2026-08-11T00:10:00Z",
        )

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
        result = build_session_cleanup_diagnostics(
            [
                _doctor_task("T-001", "codex", _receipt("unsupported")),
                _doctor_task(
                    "T-002",
                    "hermes",
                    _receipt(
                        "failed",
                        error_code="session_cleanup_failed",
                        retryable=True,
                    ),
                ),
            ],
            now="2026-08-11T00:10:00Z",
        )

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["warnings"], 2)
        unsupported, failed = result["diagnostics"]
        self.assertIn("no official exact-session deletion capability", unsupported["message"])
        self.assertEqual(unsupported["capability"], "unsupported")
        self.assertEqual(failed["error_code"], "session_cleanup_failed")
        self.assertTrue(failed["retryable"])

    def test_pending_is_stale_only_after_five_minutes(self) -> None:
        task = _doctor_task("T-003", "claude", _receipt("pending"))
        boundary = build_session_cleanup_diagnostics(
            [task], now="2026-08-11T00:05:00Z"
        )
        stale = build_session_cleanup_diagnostics(
            [task], now="2026-08-11T00:05:01Z"
        )

        self.assertEqual(boundary["warnings"], 0)
        self.assertEqual(stale["warnings"], 1)
        self.assertIn("more than five minutes", stale["diagnostics"][0]["message"])

    def test_retained_and_succeeded_are_healthy_and_supported_is_authoritative(self) -> None:
        result = build_session_cleanup_diagnostics(
            [
                _doctor_task("T-004", "claude", _receipt("retained")),
                _doctor_task("T-005", "codex", _receipt("succeeded")),
                _doctor_task("T-006", "hermes", _receipt("pending")),
            ],
            now="2026-08-11T00:04:59Z",
        )

        self.assertEqual(result["warnings"], 0)
        self.assertTrue(all(item["status"] == "healthy" for item in result["diagnostics"]))
        self.assertEqual(result["diagnostics"][1]["capability"], "supported")
        self.assertEqual(result["diagnostics"][2]["capability"], "supported")

    def test_text_uses_json_data_and_never_exposes_sensitive_session_inputs(self) -> None:
        result = build_session_cleanup_diagnostics(
            [_doctor_task("T-007", "hermes", _receipt("failed"))],
            now="2026-08-11T00:10:00Z",
        )
        report = {
            "schema_version": 1,
            "ok": True,
            "status": "warning",
            "package": {
                "version": "1",
                "commit_sha": None,
                "source_tree_sha256": None,
                "build_source": "test",
                "module_path": "module",
                "executable_path": "agentbc",
                "install_source": "test",
            },
            "config": {"path": "config", "exists": True, "workspace_root": "workspace"},
            "runner": {
                "status": "ready",
                "pid": 1,
                "python_executable": "python",
                "module_path": "module",
                "executors": ["hermes"],
            },
            "session_cleanup": result,
            "checks": [
                {
                    "id": "session.cleanup",
                    "status": "warning",
                    "message": "One cleanup warning.",
                }
            ],
        }
        rendered = json.dumps(report) + render_doctor_text(report)

        for sensitive in (
            "private-session-id",
            "/private/customer/session",
            "secret executor output",
            "native_argv",
            "--force",
            "official_session_delete",
            "last_attempt_at",
        ):
            self.assertNotIn(sensitive, rendered)


class Phase5DocumentationTests(unittest.TestCase):
    @staticmethod
    def _read(relative: str) -> str:
        return (PROJECT_ROOT / relative).read_text(encoding="utf-8")

    def test_guides_describe_background_executor_only_cleanup(self) -> None:
        english = self._read("docs/USER_GUIDE.md")
        chinese = self._read("docs/USER_GUIDE_ZH.md")

        for phrase in (
            "background and user-transparent",
            "temporary sessions",
            "never deletes the dispatcher conversation",
            "never requires users to manage a separate runtime directory",
        ):
            self.assertIn(phrase, english)
        for phrase in (
            "后台无感",
            "Executor 创建的临时会话",
            "永远不会删除",
            "不会要求用户管理单独的 runtime 目录",
        ):
            self.assertIn(phrase, chinese)

    def test_integration_docs_preserve_executor_session_cleanup_boundary(self) -> None:
        checklist = self._read("AGENTBC_1.0.2A_DEVELOPMENT_CHECKLIST.md")
        handbook = self._read("AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md")

        for document in (checklist, handbook):
            self.assertIn("SESSION-001", document)
            self.assertIn("retain", document)
            self.assertIn("cleanup", document)
            self.assertIn("dispatcher conversation", document)

    def test_all_controller_skills_preserve_cleanup_boundary(self) -> None:
        for relative in (
            "src/agent_bridge_connect/skills/codex_skill.md",
            "src/agent_bridge_connect/skills/claude_skill.md",
            "src/agent_bridge_connect/skills/hermes_skill.md",
        ):
            with self.subTest(relative=relative):
                skill = self._read(relative)
                self.assertIn("Executor", skill)
                self.assertTrue("dispatcher" in skill.lower() or "派发者" in skill)
                self.assertIn("runtime", skill)
                self.assertIn("cleanup", skill.lower())


if __name__ == "__main__":
    unittest.main()
