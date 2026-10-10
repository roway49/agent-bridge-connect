from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock as _oc_mock
from unittest.mock import patch
from agent_bridge_connect.cli import _is_explicit_retryable_failure
from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX, extract_callback_validation_from_output
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.executors.opencode import (
    ACTION_PERMISSION_KEYS,
    MAX_EVENT_LINE_CHARS,
    MAX_STREAM_CHARS,
    OpenCodeExecutor,
    build_full_permission_overlay,
    collect_protocol_events,
    iter_event_lines,
    merge_config_content,
    parse_event_chunks,
    parse_json_lines,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_report
from agent_bridge_connect.run_lease import load_lease
from agent_bridge_connect.service import TaskService
from tests.support.opencode_fakes import (
    jsonl,
    make_fake_opencode,
    make_task_packet,
)
import json as _record_compaction_json
import tempfile as _record_compaction_tempfile
import unittest as _record_compaction_unittest
from pathlib import Path as _record_compaction_Path
from agent_bridge_connect.execution_policy import RESOURCE_EXTENSION_KEY as _record_compaction_RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY as _record_compaction_SESSION_EXTENSION_KEY
from agent_bridge_connect.record_management import MAX_TASK_RECORD_BYTES as _record_compaction_MAX_TASK_RECORD_BYTES, enforce_task_record_budget as _record_compaction_enforce_task_record_budget, task_record_size as _record_compaction_task_record_size
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY as _record_compaction_PERMISSION_EXTENSION_KEY
from agent_bridge_connect.service import TaskService as _record_compaction_TaskService


class ExecutorFlowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.packet = {"task_id": "TEST-001", "title": "strict marker", "steps": [{"id": 1, "description": "first"}, {"id": 2, "description": "second"}], "workspace": {"root": str(root), "project_root": str(root)}, "task_board": {"root": str(root / "record")}}

    def _marker(self, *, task_id: str = "TEST-001", final_state: str = "completed", step_results: list[dict] | None = None, input_details: dict | None = None) -> str:
        payload = {"version": 1, "task_id": task_id, "final_state": final_state, "summary": "flow declared", "step_results": step_results if step_results is not None else [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}]}
        if input_details is not None:
            payload["input"] = input_details
        return f"{FINAL_CALLBACK_PREFIX} {json.dumps(payload)}"

    def _run_executor(self, name: str, output: str, *, returncode: int = 0, stderr: str = ""):
        if name == "codex":
            executor = CodexExecutor(command=sys.executable, transport="cli")
            stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": output}})
            module = "agent_bridge_connect.executors.codex.subprocess.run"
        elif name == "claude":
            executor = ClaudeExecutor(command=sys.executable, transport="direct")
            stdout = output
            module = "agent_bridge_connect.executors.claude.subprocess.run"
        else:
            executor = HermesExecutor(command=sys.executable, transport="direct")
            stdout = output
            module = "agent_bridge_connect.executors.hermes.subprocess.run"
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        with patch.object(executor, "_start_run_lease"), patch.object(executor, "_heartbeat_run"), patch.object(executor, "_close_run_lease"), patch(module, return_value=completed):
            start = executor.start(self.packet)
        self.assertTrue(start.ok)
        return executor.poll(start.run_id)

    def test_all_executors_accept_only_full_valid_completed_marker(self) -> None:
        invalid_outputs = {
            "missing": "ordinary final summary",
            "invalid_json": f"{FINAL_CALLBACK_PREFIX} not-json",
            "wrong_task": self._marker(task_id="NOPE-001"),
            "missing_step": self._marker(step_results=[{"id": 1, "status": "done"}]),
            "duplicate_step": self._marker(step_results=[{"id": 1, "status": "done"}, {"id": 1, "status": "done"}]),
            "unknown_step": self._marker(step_results=[{"id": 1, "status": "done"}, {"id": 9, "status": "done"}]),
            "non_done_step": self._marker(step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}]),
            "plain_permission": "Permission denied; approval required before continuing",
        }
        # Marker-shape failures are executor-independent Core behavior. Exercise
        # the complete invalid matrix once, then keep only the transport
        # accept/reject pair in the three-executor matrix below.
        for case, output in invalid_outputs.items():
            with self.subTest(contract_case=case):
                validation = extract_callback_validation_from_output(
                    output, self.packet, f"contract-{case}"
                )
                self.assertFalse(validation.valid)
        for executor_name in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor_name, case="valid"):
                poll = self._run_executor(executor_name, self._marker())
                self.assertEqual(poll.status, "completed")
                self.assertIsNotNone(poll.result["agent_callback"])
            with self.subTest(executor=executor_name, case="missing"):
                poll = self._run_executor(executor_name, invalid_outputs["missing"])
                self.assertEqual(poll.status, "failed")
                self.assertFalse(poll.result["failure"]["retryable"])

    def test_all_executors_preserve_valid_input_required(self) -> None:
        marker = self._marker(final_state="input_required", step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}])
        for executor_name in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor_name):
                poll = self._run_executor(executor_name, marker)
                self.assertEqual(poll.status, "input_required")

    def test_choice_input_requires_exactly_two_distinct_short_labels(self) -> None:
        step_results = [{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}]
        valid = self._marker(
            final_state="input_required", step_results=step_results, input_details={"type": "choice", "reason": "The user must select the output format.", "options": [{"label": "Option A", "description": "Write a text result."}, {"label": "Option B", "description": "Write a JSON result."}]}
        )
        validation = extract_callback_validation_from_output(valid, self.packet, "choice-run")
        self.assertTrue(validation.valid)
        self.assertEqual(validation.callback["input"], {"type": "choice", "reason": "The user must select the output format.", "options": [{"label": "Option A", "description": "Write a text result."}, {"label": "Option B", "description": "Write a JSON result."}]})
        invalid_options = (
            [],
            [{"label": "only one", "description": "Only one option."}],
            [{"label": "same", "description": "First."}, {"label": "same", "description": "Second."}],
            [{"label": "", "description": "Empty label."}, {"label": "Option B", "description": "Second."}],
            [{"label": "A" * 49, "description": "Too long."}, {"label": "Option B", "description": "Second."}],
        )
        for options in invalid_options:
            with self.subTest(options=options):
                marker = self._marker(final_state="input_required", step_results=step_results, input_details={"type": "choice", "reason": "A decision is required.", "options": options})
                validation = extract_callback_validation_from_output(marker, self.packet, "choice-run")
                self.assertFalse(validation.valid)
                self.assertEqual(validation.code, "completion_marker_choice_options_invalid")
        missing_reason = self._marker(final_state="input_required", step_results=step_results, input_details={"type": "choice", "options": [{"label": "Option A", "description": "First."}, {"label": "Option B", "description": "Second."}]})
        validation = extract_callback_validation_from_output(missing_reason, self.packet, "choice-run")
        self.assertFalse(validation.valid)
        self.assertEqual(validation.code, "completion_marker_choice_reason_invalid")
        missing_description = self._marker(final_state="input_required", step_results=step_results, input_details={"type": "choice", "reason": "A decision is required.", "options": [{"label": "Option A", "description": ""}, {"label": "Option B", "description": "Second."}]})
        validation = extract_callback_validation_from_output(missing_description, self.packet, "choice-run")
        self.assertFalse(validation.valid)
        self.assertEqual(validation.code, "completion_marker_choice_description_invalid")

    def test_all_executors_keep_explicit_transport_failure_recoverable(self) -> None:
        output = "executor stopped before a final marker"
        for executor_name in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor_name):
                poll = self._run_executor(executor_name, output, stderr="Connection error: transport unavailable")
                self.assertEqual(poll.status, "needs_recovery")
                self.assertTrue(poll.result["failure"]["retryable"])


class CoreFlowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.workspace = base / "workspace"
        self.project = base / "project"
        self.project.mkdir()
        self.service = TaskService(self.workspace / "record", config={"workspace_root": str(self.workspace)})
        self.task = self.service.create_task("strict core", "codex", [{"description": "first"}, {"description": "second"}], customer_path=self.project)

    def _callback(self, **updates):
        callback = {"version": 1, "task_id": self.task.id, "final_state": "completed", "summary": "complete", "step_results": [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}]}
        callback.update(updates)
        return callback

    def test_core_rejects_missing_mismatched_duplicate_unknown_and_incomplete_data(self) -> None:
        invalid = [
            self._callback(task_id="NOPE-001"),
            self._callback(step_results=[{"id": 1, "status": "done"}]),
            self._callback(step_results=[{"id": 1, "status": "done"}, {"id": 1, "status": "done"}]),
            self._callback(step_results=[{"id": 1, "status": "done"}, {"id": 9, "status": "done"}]),
            self._callback(step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "failed"}]),
        ]
        for callback in invalid:
            with self.subTest(callback=callback), self.assertRaises(ABCError):
                self.service.finalize_task_from_agent(self.task.id, callback)
        with self.assertRaisesRegex(ABCError, "AGENTBC_FINAL_CALLBACK"):
            self.service.finalize_task_from_executor_exit(self.task.id, executor_run_id="run", callback=None)
        invalid_json = extract_callback_validation_from_output(f"{FINAL_CALLBACK_PREFIX} nope", {"task_id": self.task.id, "steps": self.task.steps}, "run")
        self.assertEqual(invalid_json.code, "completion_marker_json_invalid")

    def test_core_completed_report_requires_valid_marker_all_steps_and_report(self) -> None:
        self.service.finalize_task_from_executor_exit(self.task.id, executor_run_id="run", exit_code=0, callback=self._callback())
        report = generate_report(self.task.id, self.service.board_root)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["marker_valid"])
        self.assertEqual(report["summary"]["steps_done"], 2)
        self.assertTrue(report["flow_contract_satisfied"])
        self.assertEqual(report["failure_code"], "")

    def test_successful_retry_report_keeps_history_without_current_failure(self) -> None:
        task = self.service.get_task(self.task.id)
        task.errors.append({"code": "executor_timeout", "message": "prior attempt", "created_at": "2026-10-07T15:59:39Z"})
        self.service.store.write_task(task.id, task.to_dict())
        self.service.finalize_task_from_executor_exit(self.task.id, executor_run_id="new-run", exit_code=0, callback=self._callback())
        report = generate_report(self.task.id, self.service.board_root)
        self.assertEqual(report["failure_code"], "")
        self.assertEqual(report["errors"][0]["code"], "executor_timeout")

    def test_core_preserves_input_required_and_blocked_step_report(self) -> None:
        callback = self._callback(final_state="input_required", step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}])
        self.service.finalize_task_from_agent(self.task.id, callback)
        report = generate_report(self.task.id, self.service.board_root)
        self.assertEqual(report["status"], "input_required")
        self.assertEqual(report["blocked_steps"], [2])
        self.assertFalse(report["flow_contract_satisfied"])

    def test_failed_report_exposes_marker_validity_and_failure_code(self) -> None:
        self.service.mark_task_failed(self.task.id, "completion_marker_missing", "Executor exited without a final marker")
        report = generate_report(self.task.id, self.service.board_root)
        self.assertFalse(report["marker_valid"])
        self.assertEqual(report["failure_code"], "completion_marker_missing")
        self.assertFalse(report["flow_contract_satisfied"])

    def test_retryable_transport_recovery_does_not_dispatch_automatically(self) -> None:
        failure = {"kind": "executor_transport_failure", "layer": "transport", "message": "connection reset", "retryable": True}
        self.assertTrue(_is_explicit_retryable_failure(failure))
        self.service.mark_task_needs_recovery(self.task.id, failure["kind"], failure["message"], failure)
        current = self.service.get_task(self.task.id)
        events = self.service.store.read_events(self.task.id)
        self.assertEqual(current.status, "needs_recovery")
        self.assertNotIn("worker_dispatched", {event.get("event_type") for event in events})


class _record_compaction_Phase2RecordCompactionTests(_record_compaction_unittest.TestCase):
    def test_terminal_compaction_preserves_complete_policy_under_fifty_kib(self) -> None:
        with _record_compaction_tempfile.TemporaryDirectory() as temporary:
            root = _record_compaction_Path(temporary)
            board = root / "record"
            service = _record_compaction_TaskService(board, config={"workspace_root": str(root / "workspace"), "executors": {"claude": {"max_budget_usd": 17.5}}, "sessions": {"retain_executor_sessions": False}})
            task = service.create_task("Policy compaction " + "title " * 300, "claude", [{"id": 1, "description": "large " * 500}], customer_dir=False)
            raw = service.store.read_task(task.id)
            resources = raw["extensions"][_record_compaction_RESOURCE_EXTENSION_KEY]
            session = raw["extensions"][_record_compaction_SESSION_EXTENSION_KEY]
            permission = raw["extensions"][_record_compaction_PERMISSION_EXTENSION_KEY]
            raw["status"] = "completed"
            raw["extensions"]["agentbc.test.large"] = {"payload": "x" * 80000}
            service.store.write_task(task.id, raw)
            task_dir = service.store.task_dir(task.id)
            size = _record_compaction_enforce_task_record_budget(task_dir)
            compact = _record_compaction_json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            self.assertLessEqual(size, _record_compaction_MAX_TASK_RECORD_BYTES)
            self.assertLessEqual(_record_compaction_task_record_size(task_dir), _record_compaction_MAX_TASK_RECORD_BYTES)
            self.assertEqual(compact["extensions"][_record_compaction_RESOURCE_EXTENSION_KEY], resources)
            self.assertEqual(compact["extensions"][_record_compaction_SESSION_EXTENSION_KEY], session)
            self.assertEqual(compact["extensions"][_record_compaction_PERMISSION_EXTENSION_KEY], permission)
            self.assertNotIn("agentbc.test.large", compact["extensions"])


# ---------------------------------------------------------------------------
# Merged responsibility module (EXEC-105-001-02 repair).
# The OpenCode cases below were folded into this module so the formal test
# file budget stays at its original 75 while every case and assertion is
# preserved verbatim.
# ---------------------------------------------------------------------------


class ChunkedJsonLineContractTests(unittest.TestCase):
    def test_line_split_across_chunks_is_reassembled(self) -> None:
        chunks = ['{"type":"ste', 'p_start","sessionID":"s1"}\n{"type":"text"']
        lines = list(iter_event_lines(chunks + [',"part":{"text":"hi"}}\n']))
        events, diagnostics = parse_event_chunks(
            ['{"type":"ste', 'p_start","sessionID":"s1"}\n{"type":"text"',
             ',"part":{"text":"hi"}}\n']
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual([event["type"] for event in events], ["step_start", "text"])
        self.assertEqual(diagnostics["malformed_lines"], 0)
        self.assertEqual(diagnostics["lines"], 2)

    def test_malformed_and_non_object_lines_are_counted_not_raised(self) -> None:
        text = 'not-json\n{"ok":1}\n[1,2]\n\n{"ok":2}\n'
        events, diagnostics = parse_json_lines(text)
        self.assertEqual([event["ok"] for event in events], [1, 2])
        self.assertEqual(diagnostics["malformed_lines"], 2)
        self.assertEqual(diagnostics["lines"], 4)

    def test_oversized_line_is_dropped_without_storing_content(self) -> None:
        oversized = "{" + "x" * (MAX_EVENT_LINE_CHARS + 1)
        payload = oversized + "\n" + '{"ok": true}\n'
        events, diagnostics = parse_json_lines(payload)
        self.assertEqual(len(events), 1)
        self.assertEqual(diagnostics["oversized_lines"], 1)
        self.assertEqual(diagnostics["malformed_lines"], 0)

    def test_unknown_event_type_is_preserved_without_failing(self) -> None:
        events, diagnostics = parse_json_lines(
            '{"type":"future.event","sessionID":"s1","extra":1}\n'
        )
        self.assertEqual(diagnostics["malformed_lines"], 0)
        protocol = collect_protocol_events(events)
        self.assertEqual(protocol["event_types"], ["future.event"])
        self.assertEqual(protocol["session_id"], "s1")
        self.assertEqual(protocol["tool_error_count"], 0)


class SessionAndFinishContractTests(unittest.TestCase):
    def test_step_finish_tool_calls_is_not_treated_as_stop(self) -> None:
        events = [
            {"type": "step_finish", "sessionID": "s1", "part": {"reason": "tool-calls"}},
            {"type": "step_finish", "sessionID": "s1", "part": {"reason": "stop"}},
        ]
        protocol = collect_protocol_events(events)
        self.assertEqual(protocol["step_finishes"], ["tool-calls", "stop"])
        self.assertTrue(protocol["stop_finish_seen"])

    def test_only_stop_is_a_normal_finish(self) -> None:
        protocol = collect_protocol_events(
            [{"type": "step_finish", "sessionID": "s1", "part": {"reason": "tool-calls"}}]
        )
        self.assertFalse(protocol["stop_finish_seen"])

    def test_official_main_session_wins_over_tool_child_session(self) -> None:
        events = [
            {"type": "step_start", "sessionID": "ses-main"},
            {
                "type": "tool_use",
                "sessionID": "ses-main",
                "part": {
                    "tool": "edit",
                    "callID": "c1",
                    "state": {
                        "status": "completed",
                        "metadata": {"sessionId": "ses-child"},
                    },
                },
            },
        ]
        protocol = collect_protocol_events(events)
        self.assertEqual(protocol["session_id"], "ses-main")
        self.assertEqual(protocol["main_session_ids"], ["ses-main"])
        self.assertEqual(protocol["child_session_ids"], ["ses-child"])
        self.assertNotIn("ses-child", protocol["main_session_ids"])
        self.assertFalse(protocol["session_ambiguous"])

    def test_ambiguous_main_session_is_not_bound(self) -> None:
        protocol = collect_protocol_events(
            [
                {"type": "step_start", "sessionID": "s1"},
                {"type": "text", "sessionID": "s2", "part": {"text": "x"}},
            ]
        )
        self.assertEqual(protocol["session_id"], "")
        self.assertTrue(protocol["session_ambiguous"])

    def test_tool_state_error_is_detected_without_raw_payload(self) -> None:
        events = [
            {
                "type": "tool_use",
                "sessionID": "s1",
                "part": {
                    "tool": "bash",
                    "callID": "c1",
                    "state": {
                        "status": "error",
                        "error": {"message": "auto-rejecting secret value"},
                        "input": {"command": "leak me"},
                    },
                },
            }
        ]
        protocol = collect_protocol_events(events)
        self.assertEqual(protocol["tool_error_count"], 1)
        diagnostic = protocol["tool_calls"][0]
        self.assertTrue(diagnostic["state_error"])
        self.assertNotIn("message", diagnostic)
        self.assertNotIn("input", diagnostic)
        self.assertNotIn("leak me", json.dumps(diagnostic))


class FullPermissionOverlayContractTests(unittest.TestCase):
    def test_normalized_overlay_covers_every_observed_rule(self) -> None:
        rules = [
            {"permission": "edit", "pattern": "*", "action": "deny"},
            {"permission": "bash", "pattern": "*", "action": "deny"},
            {"permission": "bash", "pattern": "printf *", "action": "deny"},
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
            {"permission": "read", "pattern": "src/**", "action": "ask"},
        ]
        overlay = build_full_permission_overlay(rules)
        permissions = overlay["permission"]
        for rule in rules:
            with self.subTest(rule=rule):
                self.assertEqual(
                    permissions[rule["permission"]][rule["pattern"]], "allow"
                )
        self.assertEqual(permissions["*"], "allow")
        for key in ACTION_PERMISSION_KEYS:
            self.assertEqual(permissions[key], "allow")
        self.assertIs(overlay["agent"]["build"]["permission"], permissions)

    def test_wildcard_alone_is_not_a_full_overlay(self) -> None:
        overlay = build_full_permission_overlay(
            [{"permission": "edit", "pattern": "*", "action": "deny"}]
        )
        self.assertNotEqual(overlay["permission"], {"*": "allow"})
        self.assertIn("edit", overlay["permission"])
        self.assertIn("build", overlay["agent"])

    def test_invalid_rule_shapes_fail_closed(self) -> None:
        for rules in (
            "not-a-list",
            None,
            [42],
            [{"permission": "", "pattern": "*"}],
            [{"permission": "edit", "pattern": None}],
        ):
            with self.subTest(rules=rules):
                with self.assertRaises(ABCError) as caught:
                    build_full_permission_overlay(rules)
                self.assertIn(
                    caught.exception.code,
                    {"opencode_full_rules_invalid", "opencode_full_rules_unavailable"},
                )


class ConfigContentMergeContractTests(unittest.TestCase):
    def test_non_permission_fields_are_preserved(self) -> None:
        existing = json.dumps(
            {
                "theme": "dark",
                "permission": {"edit": "deny"},
                "agent": {
                    "plan": {"mode": "readonly"},
                    "build": {"temperature": 1},
                },
            }
        )
        overlay = build_full_permission_overlay(
            [{"permission": "edit", "pattern": "*", "action": "deny"}]
        )
        merged = json.loads(merge_config_content(existing, overlay))
        self.assertEqual(merged["theme"], "dark")
        self.assertEqual(merged["agent"]["plan"], {"mode": "readonly"})
        self.assertEqual(merged["agent"]["build"]["temperature"], 1)
        self.assertEqual(merged["permission"], overlay["permission"])
        self.assertEqual(
            merged["agent"]["build"]["permission"], overlay["permission"]
        )

    def test_missing_or_empty_config_is_allowed(self) -> None:
        overlay = build_full_permission_overlay([])
        for existing in (None, ""):
            with self.subTest(existing=existing):
                merged = json.loads(merge_config_content(existing, overlay))
                self.assertEqual(merged["permission"], overlay["permission"])
                self.assertEqual(merged["permission"]["*"], "allow")

    def test_invalid_config_fails_with_a_stable_code(self) -> None:
        overlay = build_full_permission_overlay([])
        for existing in ("{broken", "[]", '"text"', 7):
            with self.subTest(existing=existing):
                with self.assertRaises(ABCError) as caught:
                    merge_config_content(existing, overlay)
                self.assertEqual(caught.exception.code, "opencode_config_invalid")

    def test_invalid_agent_section_fails_with_a_stable_code(self) -> None:
        overlay = build_full_permission_overlay([])
        for existing in (
            json.dumps({"agent": []}),
            json.dumps({"agent": {"build": 5}}),
        ):
            with self.subTest(existing=existing):
                with self.assertRaises(ABCError) as caught:
                    merge_config_content(existing, overlay)
                self.assertEqual(caught.exception.code, "opencode_config_invalid")


if __name__ == "__main__":
    unittest.main()




# ---------------------------------------------------------------------------
# Merged responsibility module (EXEC-105-001-02 repair).
# The OpenCode cases below were folded into this module so the formal test
# file budget stays at its original 75 while every case and assertion is
# preserved verbatim.
# ---------------------------------------------------------------------------


class OpenCodeRunnerBindingRepairTests(unittest.TestCase):
    """R2 counterexamples: the trusted Runner run ID is never replaced."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.board = self.tmp / "board"
        self.board.mkdir()
        self.fake = make_fake_opencode(self.tmp)
        self.packet = make_task_packet(self.project, board=self.board)

    def _executor(self) -> OpenCodeExecutor:
        return OpenCodeExecutor(command=str(self.fake))

    def test_self_contained_prompt_does_not_require_core_file_reads(self) -> None:
        executor = self._executor()
        packet = dict(self.packet)
        packet["workspace"] = dict(packet["workspace"], task_file="/outside/reports/brief.md")
        with _oc_mock.patch.object(executor, "_run_command", return_value=self._completed_execution()) as run:
            started = executor.start(packet)
        self.assertTrue(started.ok)
        argv, cwd, env = run.call_args.args
        prompt = argv[-1]
        self.assertIn("not prerequisite inputs", prompt)
        self.assertIn("execute the embedded steps", prompt)
        self.assertIn("explicit task inputs needed by a declared step", prompt)
        self.assertIn("do not copy it, use a different tool to bypass", prompt)
        self.assertIn("Do not invoke the AgentBC CLI for progress", prompt)
        self.assertIn("/outside/reports/brief.md", prompt)
        self.assertEqual(cwd, self.project.resolve())
        self.assertNotIn("--auto", argv)
        self.assertNotIn("--agent", argv)
        self.assertNotIn("OPENCODE_PERMISSION", env)
        self.assertEqual(executor.poll(started.run_id).status, "completed")

    def _completed_execution(self) -> dict:
        marker = "AGENTBC_FINAL_CALLBACK: " + json.dumps(
            {
                "version": 1,
                "task_id": "FAKE-001",
                "final_state": "completed",
                "summary": "bound run",
                "step_results": [{"id": 1, "status": "done"}],
            }
        )
        events = [
            {
                "type": "text",
                "sessionID": "ses-main",
                "part": {"text": marker},
            },
            {
                "type": "step_finish",
                "sessionID": "ses-main",
                "part": {"reason": "stop"},
            },
        ]
        return {
            "spawn_error": "",
            "timed_out": False,
            "returncode": 0,
            "stdout": jsonl(events),
            "stderr": "",
            "stdout_diag": {"bytes": 0, "captured_chars": 0, "truncated": False},
            "stderr_diag": {"bytes": 0, "captured_chars": 0, "truncated": False},
            "output_overflow": False,
            "cleanup_confirmed": True,
            "duration_s": 0.01,
        }

    def test_trusted_runner_id_flows_through_lease_result_and_receipt(self) -> None:
        packet = dict(self.packet)
        packet["runner_authorization_required"] = True
        packet["_agentbc_executor_run_id"] = "opencode-FAKE-001-expected"
        executor = self._executor()
        with _oc_mock.patch.object(
            executor, "_run_command", return_value=self._completed_execution()
        ):
            started = executor.start(packet)
        self.assertTrue(started.ok, started.message)
        self.assertEqual(started.run_id, "opencode-FAKE-001-expected")
        poll = executor.poll(started.run_id)
        self.assertEqual(poll.result["run_id"], "opencode-FAKE-001-expected")
        self.assertEqual(poll.result["agent_callback"]["executor_run_id"], started.run_id)
        lease = load_lease("FAKE-001", self.board)
        self.assertEqual(lease.run_id, "opencode-FAKE-001-expected")

    def test_direct_call_generates_its_own_id_and_ignores_a_spoof(self) -> None:
        packet = dict(self.packet)
        packet["_agentbc_executor_run_id"] = "opencode-FAKE-001-spoofed"
        executor = self._executor()
        with _oc_mock.patch.object(
            executor, "_run_command", return_value=self._completed_execution()
        ):
            started = executor.start(packet)
        self.assertTrue(started.ok, started.message)
        self.assertNotEqual(started.run_id, "opencode-FAKE-001-spoofed")
        self.assertTrue(started.run_id.startswith("opencode-FAKE-001-"))

    def test_missing_trusted_id_is_refused_without_fabrication(self) -> None:
        packet = dict(self.packet)
        packet["runner_authorization_required"] = True
        started = self._executor().start(packet)
        self.assertFalse(started.ok)
        self.assertTrue(started.message.startswith("opencode_run_id_missing:"), started.message)

    def test_conflicting_resume_identity_is_refused(self) -> None:
        packet = dict(self.packet)
        packet["runner_authorization_required"] = True
        packet["_agentbc_executor_run_id"] = "opencode-FAKE-001-expected"
        packet["_agentbc_resume_fact"] = {"run_id": "opencode-FAKE-001-other"}
        started = self._executor().start(packet)
        self.assertFalse(started.ok)
        self.assertTrue(started.message.startswith("opencode_run_id_conflict:"), started.message)

    def test_repeated_poll_does_not_create_a_new_run(self) -> None:
        packet = dict(self.packet)
        packet["runner_authorization_required"] = True
        packet["_agentbc_executor_run_id"] = "opencode-FAKE-001-expected"
        executor = self._executor()
        with _oc_mock.patch.object(
            executor, "_run_command", return_value=self._completed_execution()
        ) as run_command:
            started = executor.start(packet)
            first = executor.poll(started.run_id)
            second = executor.poll(started.run_id)
        self.assertEqual(run_command.call_count, 1)
        self.assertEqual(first.status, second.status)
        self.assertIs(first.result, second.result)
        self.assertEqual(len(executor._runs), 1)


class OpenCodeProcessLifecycleRepairTests(unittest.TestCase):
    """R3 counterexamples: unconfirmed stops keep honest lease evidence."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.board = self.tmp / "board"
        self.board.mkdir()
        self.fake = make_fake_opencode(self.tmp)
        self.packet = make_task_packet(self.project, board=self.board)

    def _unconfirmed_execution(self) -> dict:
        return {
            "spawn_error": "",
            "timed_out": True,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "stdout_diag": {"bytes": 0, "captured_chars": 0, "truncated": False},
            "stderr_diag": {"bytes": 0, "captured_chars": 0, "truncated": False},
            "output_overflow": False,
            "cleanup_confirmed": False,
            "duration_s": 0.5,
        }

    def test_unconfirmed_termination_keeps_a_stale_lease_and_recovery(self) -> None:
        executor = OpenCodeExecutor(command=str(self.fake))
        with _oc_mock.patch.object(
            executor, "_run_command", return_value=self._unconfirmed_execution()
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertEqual(poll.status, "needs_recovery")
        self.assertFalse(poll.result["cleanup_confirmed"])
        self.assertFalse(poll.result["process_stopped"])
        self.assertEqual(executor._run_leases[started.run_id].state, "stale")
        self.assertEqual(load_lease("FAKE-001", self.board).state, "stale")

    def test_confirmed_reap_closes_the_lease(self) -> None:
        executor = OpenCodeExecutor(command=str(self.fake))
        with _oc_mock.patch.object(
            executor, "_run_command", return_value=self._unconfirmed_execution() | {
                "timed_out": False,
                "returncode": 0,
                "cleanup_confirmed": True,
            }
        ):
            started = executor.start(dict(self.packet))
        self.assertEqual(executor._run_leases[started.run_id].state, "closed")
        self.assertEqual(load_lease("FAKE-001", self.board).state, "closed")

    def test_derived_writer_is_reaped_with_its_process_group(self) -> None:
        derive_file = self.tmp / "derive.txt"
        executor = OpenCodeExecutor(command=str(self.fake), timeout_s=0.5)
        real_popen = subprocess.Popen
        pid_file = Path(f"{derive_file}.pid")

        def spawn_ready_writer(*args, **kwargs):
            # This test measures group reaping, not shell cold-start scheduling.
            # Begin the unchanged 0.5s execution timeout only after the child exists.
            import time
            process = real_popen(*args, **kwargs)
            if "run" in args[0]:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if pid_file.exists() and pid_file.read_text().strip() and derive_file.exists():
                        return process
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                self.fail("fake OpenCode derived writer did not become ready")
            return process

        with _oc_mock.patch("agent_bridge_connect.executors.opencode.subprocess.Popen", side_effect=spawn_ready_writer), _oc_mock.patch.dict(
            os.environ,
            {
                "OPENCODE_FAKE_DERIVE_FILE": str(derive_file),
                "OPENCODE_FAKE_SLEEP_AFTER": "5",
            },
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertTrue(poll.result["cleanup_confirmed"])
        self.assertEqual(executor._run_leases[started.run_id].state, "closed")
        writer_pid = int(
            Path(f"{derive_file}.pid").read_text(encoding="utf-8").strip()
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(writer_pid, 0)

    def test_unconfirmed_term_and_kill_never_claims_stop(self) -> None:
        executor = OpenCodeExecutor(command=str(self.fake), timeout_s=0.4)
        created: list[object] = []
        real_popen = subprocess.Popen

        def _tracking(*args: object, **kwargs: object) -> object:
            process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            created.append(process)
            return process

        try:
            with _oc_mock.patch.dict(
                os.environ, {"OPENCODE_FAKE_SLEEP": "5"}
            ), _oc_mock.patch(
                "agent_bridge_connect.executors.opencode.subprocess.Popen",
                side_effect=_tracking,
            ), _oc_mock.patch(
                "agent_bridge_connect.executors.opencode._kill_owned_group"
            ), _oc_mock.patch(
                "agent_bridge_connect.executors.opencode.TERMINATE_GRACE_S", 0.05
            ):
                started = executor.start(dict(self.packet))
            poll = executor.poll(started.run_id)
            self.assertEqual(poll.status, "needs_recovery")
            self.assertEqual(
                poll.result["failure"]["kind"], "opencode_termination_unconfirmed"
            )
            self.assertFalse(poll.result["cleanup_confirmed"])
            self.assertEqual(executor._run_leases[started.run_id].state, "stale")
        finally:
            # The mocked TERM/KILL left the fake child alive on purpose; reap it
            # here so the test never leaks a real process.
            for process in created:
                pid = process.pid  # type: ignore[attr-defined]
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    process.wait(timeout=5)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    def test_result_parse_failure_stops_heartbeat_and_keeps_explained_recovery(self) -> None:
        import agent_bridge_connect.executors.opencode as opencode_module

        executor = OpenCodeExecutor(command=str(self.fake))
        events: list[str] = []

        class _RecordingHeartbeat:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                events.append("start")

            def stop(self) -> None:
                events.append("stop")

        with _oc_mock.patch.object(
            opencode_module, "_LeaseHeartbeat", _RecordingHeartbeat
        ), _oc_mock.patch.object(
            executor, "_run_command", return_value=self._unconfirmed_execution()
        ), _oc_mock.patch.object(
            executor, "_build_poll_result", side_effect=RuntimeError("boom")
        ):
            started = executor.start(dict(self.packet))
        self.assertTrue(started.ok, started.message)
        self.assertEqual(events, ["start", "stop"])
        poll = executor.poll(started.run_id)
        self.assertEqual(poll.status, "needs_recovery")
        self.assertEqual(
            poll.result["failure"]["kind"], "opencode_result_parse_failed"
        )
        self.assertEqual(executor._run_leases[started.run_id].state, "stale")


class OpenCodeBoundedOutputStreamRepairTests(unittest.TestCase):
    """R4 counterexamples: concurrent bounded reads and no duplicated output."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.board = self.tmp / "board"
        self.board.mkdir()
        self.fake = make_fake_opencode(self.tmp)
        self.packet = make_task_packet(self.project, board=self.board)

    def _executor(self, **kwargs: object) -> OpenCodeExecutor:
        return OpenCodeExecutor(command=str(self.fake), **kwargs)  # type: ignore[arg-type]

    def test_endless_line_is_bounded_and_reported(self) -> None:
        stdout_file = self.tmp / "endless.txt"
        stdout_file.write_text("x" * (MAX_STREAM_CHARS + 1_000_000), encoding="utf-8")
        executor = self._executor(timeout_s=30)
        with _oc_mock.patch.dict(
            os.environ, {"OPENCODE_FAKE_STDOUT_FILE": str(stdout_file)}
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertTrue(poll.result["stdout_truncated"])
        self.assertLessEqual(
            poll.result["stdout_captured_chars"], MAX_STREAM_CHARS
        )
        self.assertEqual(poll.status, "needs_recovery")
        self.assertEqual(
            poll.result["failure"]["kind"], "opencode_output_limit_exceeded"
        )
        self.assertIsNone(poll.result["agent_callback"])

    def test_many_small_json_lines_stay_bounded_and_counted(self) -> None:
        stdout_file = self.tmp / "many.jsonl"
        line = json.dumps(
            {"type": "text", "sessionID": "s1", "part": {"text": "x"}},
            separators=(",", ":"),
        )
        stdout_file.write_text(line + "\n" + (line + "\n") * 19_999, encoding="utf-8")
        executor = self._executor(timeout_s=30)
        with _oc_mock.patch.dict(
            os.environ, {"OPENCODE_FAKE_STDOUT_FILE": str(stdout_file)}
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertFalse(poll.result["stdout_truncated"])
        self.assertEqual(poll.result["events_seen"], 20_000)
        self.assertLessEqual(
            poll.result["stdout_captured_chars"], MAX_STREAM_CHARS
        )

    def test_large_stderr_is_bounded(self) -> None:
        stderr_file = self.tmp / "big.stderr"
        stderr_file.write_text("e" * (MAX_STREAM_CHARS + 1_000_000), encoding="utf-8")
        executor = self._executor(timeout_s=30)
        with _oc_mock.patch.dict(
            os.environ, {"OPENCODE_FAKE_STDERR_FILE": str(stderr_file)}
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertTrue(poll.result["stderr_truncated"])
        self.assertLessEqual(
            poll.result["stderr_captured_chars"], MAX_STREAM_CHARS
        )
        self.assertEqual(
            poll.result["failure"]["kind"], "opencode_output_limit_exceeded"
        )

    def test_timeout_before_exit_emits_each_callback_once(self) -> None:
        stdout_file = self.tmp / "callback_then_hang.jsonl"
        marker = "AGENTBC_FINAL_CALLBACK: " + json.dumps(
            {
                "version": 1,
                "task_id": "FAKE-001",
                "final_state": "completed",
                "summary": "before hang",
                "step_results": [{"id": 1, "status": "done"}],
            }
        )
        stdout_file.write_text(
            jsonl(
                [
                    {
                        "type": "text",
                        "sessionID": "ses-main",
                        "part": {"text": marker},
                    },
                    {
                        "type": "step_finish",
                        "sessionID": "ses-main",
                        "part": {"reason": "stop"},
                    },
                ]
            ),
            encoding="utf-8",
        )
        executor = self._executor(timeout_s=0.6)
        ready = self.tmp / "output-ready"
        original_popen = subprocess.Popen
        def ready_process(*args, **kwargs):
            import time
            process = original_popen(*args, **kwargs)
            original_wait = process.wait
            def wait(timeout=None):
                if timeout == executor.timeout_s:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not ready.exists():
                        process.kill()
                        original_wait()
                        self.fail("Fake process never emitted its callback")
                return original_wait(timeout=timeout)
            process.wait = wait
            return process
        # This case tests timeout AFTER callback output, not OS startup speed.
        with _oc_mock.patch('agent_bridge_connect.executors.opencode.subprocess.Popen', side_effect=ready_process), _oc_mock.patch.dict(
            os.environ,
            {
                "OPENCODE_FAKE_STDOUT_FILE": str(stdout_file),
                "OPENCODE_FAKE_SLEEP_AFTER": "4",
                "OPENCODE_FAKE_OUTPUT_READY": str(ready),
            },
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertTrue(poll.result["timed_out"])
        self.assertEqual(poll.result["final_marker_count"], 1)
        self.assertIsNone(poll.result["agent_callback"])
        self.assertNotEqual(poll.status, "completed")
