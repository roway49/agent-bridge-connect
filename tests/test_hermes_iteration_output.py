from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bridge_connect.execution_contract import (
    FINAL_CALLBACK_PREFIX,
    extract_callback_validation_from_output,
)
from agent_bridge_connect.executors.hermes import (
    HermesExecutor,
    _build_prompt,
    _extract_final_response,
    _iteration_budget_diagnostics,
)


class HermesOutputExtractionTests(unittest.TestCase):
    """Regression coverage for Hermes final-response extraction and
    iteration-budget diagnostics."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.packet = {
            "task_id": "TEST-001",
            "title": "strict marker",
            "steps": [
                {"id": 1, "description": "first"},
                {"id": 2, "description": "second"},
            ],
            "workspace": {"root": str(root), "project_root": str(root)},
            "task_board": {"root": str(root / "record")},
        }

    def _marker(
        self,
        *,
        task_id: str = "TEST-001",
        final_state: str = "completed",
        step_results: list[dict] | None = None,
    ) -> str:
        payload = {
            "version": 1,
            "task_id": task_id,
            "final_state": final_state,
            "summary": "flow declared",
            "step_results": step_results
            if step_results is not None
            else [{"id": 1, "status": "done"}, {"id": 2, "status": "done"}],
        }
        return f"{FINAL_CALLBACK_PREFIX} {json.dumps(payload)}"

    def _run_direct(
        self,
        output: str,
        *,
        returncode: int = 0,
        stderr: str = "",
    ):
        executor = HermesExecutor(command=sys.executable, transport="direct")
        completed = subprocess.CompletedProcess([], returncode, stdout=output, stderr=stderr)
        with (
            patch.object(executor, "_start_run_lease"),
            patch.object(executor, "_heartbeat_run"),
            patch.object(executor, "_close_run_lease"),
            patch.object(executor._runner_client, "authorize_command", return_value={"ok": True}),
            patch(
                "agent_bridge_connect.executors.hermes.subprocess.run",
                return_value=completed,
            ),
        ):
            start = executor.start(self.packet)
        self.assertTrue(start.ok)
        return executor.poll(start.run_id)

    def _run_runner(
        self,
        output: str,
        *,
        returncode: int = 0,
        stderr: str = "",
        remote_status: str = "completed",
    ):
        executor = HermesExecutor(command=sys.executable, transport="runner")
        submit = {"run_id": "hermes-runner-1", "pid": 9999}
        remote = {
            "status": remote_status,
            "stdout": output,
            "stderr": stderr,
            "returncode": returncode,
            "cwd": str(Path(self.temporary.name)),
            "output_truncated": False,
        }
        with (
            patch.object(executor, "_start_run_lease"),
            patch.object(executor, "_heartbeat_run"),
            patch.object(executor, "_close_run_lease"),
            patch.object(
                executor._runner_client,
                "health",
                return_value={"executors": ["hermes"]},
            ),
            patch.object(executor._runner_client, "submit", return_value=submit),
            patch.object(executor._runner_client, "status", return_value=remote),
        ):
            start = executor.start(self.packet)
            self.assertTrue(start.ok)
            return executor.poll(start.run_id)

    # ---- final-response extraction -------------------------------------

    def test_prompt_echo_plus_one_real_marker_validates(self) -> None:
        prompt = _build_prompt(self.packet)
        output = f"{prompt}\n{self._marker()}"
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "completed")
        self.assertTrue(poll.result["marker_valid"])
        self.assertIsNotNone(poll.result["agent_callback"])

    def test_query_prefix_echo_plus_one_real_marker_validates(self) -> None:
        prompt = _build_prompt(self.packet)
        output = f"Query: {prompt}\n{self._marker()}"
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "completed")
        self.assertTrue(poll.result["marker_valid"])

    def test_two_markers_in_actual_response_fail_duplicate(self) -> None:
        output = f"{self._marker()}\n{self._marker()}"
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "failed")
        self.assertEqual(poll.result["failure"]["kind"], "completion_marker_duplicate")
        self.assertFalse(poll.result["failure"]["retryable"])

    def test_prompt_echo_plus_two_real_markers_still_fails_duplicate(self) -> None:
        prompt = _build_prompt(self.packet)
        output = f"{prompt}\n{self._marker()}\n{self._marker()}"
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "failed")
        self.assertEqual(poll.result["failure"]["kind"], "completion_marker_duplicate")

    def test_missing_and_malformed_markers_remain_strict(self) -> None:
        invalid_outputs = {
            "missing": "ordinary final summary",
            "invalid_json": f"{FINAL_CALLBACK_PREFIX} not-json",
            "mismatched_task": self._marker(task_id="NOPE-001"),
            "incomplete_steps": self._marker(
                step_results=[{"id": 1, "status": "done"}]
            ),
        }
        for case, output in invalid_outputs.items():
            with self.subTest(case=case):
                poll = self._run_direct(output)
                self.assertEqual(poll.status, "failed")
                self.assertFalse(poll.result["failure"]["retryable"])
                self.assertFalse(poll.result["marker_valid"])

    def test_extract_final_response_variants(self) -> None:
        prompt = _build_prompt(self.packet)
        marker = self._marker()
        live_cli_output = (
            "Warning: Unknown toolsets: messaging\n"
            "Query: Execute task TEST-001 and finish every declared step.\n"
            "Your final response must end with exactly one single-line terminal marker:\n"
            f"{FINAL_CALLBACK_PREFIX} {{\"version\":1,\"task_id\":\"TEST-001\"}}\n"
            "Initializing agent...\r\n"
            "Working through the requested files.\n"
            f"{marker}"
        )
        cases = [
            (f"{prompt}\n{marker}", marker),
            (f"Query: {prompt}\n{marker}", marker),
            (marker, marker),
            (f"Query: {marker}", marker),
            (live_cli_output, f"Working through the requested files.\n{marker}"),
            ("", ""),
        ]
        for output, expected in cases:
            with self.subTest(output=output[:48]):
                self.assertEqual(_extract_final_response(output, self.packet), expected)

    def test_live_cli_warning_and_wrapped_query_validates_after_initialization(self) -> None:
        echoed_marker = (
            f'{FINAL_CALLBACK_PREFIX} {{"version":1,"task_id":"TEST-001",'
            '"final_state":"completed"}}'
        )
        output = (
            "Warning: Unknown toolsets: messaging\n"
            "Query: Execute task TEST-001 and finish every declared step.\n"
            "The terminal wrapped this long prompt before its marker example:\n"
            f"{echoed_marker}\n"
            "Initializing agent...\n"
            "Completed the requested verification.\n"
            f"{self._marker()}"
        )
        raw_validation = extract_callback_validation_from_output(
            output,
            self.packet,
            "run",
        )
        self.assertFalse(raw_validation.valid)
        self.assertEqual(raw_validation.code, "completion_marker_duplicate")

        for transport, poll in (
            ("direct", self._run_direct(output)),
            ("runner", self._run_runner(output)),
        ):
            with self.subTest(transport=transport):
                self.assertEqual(poll.status, "completed")
                self.assertTrue(poll.result["marker_valid"])
                self.assertIsNone(poll.result["failure"])

    def test_duplicate_markers_after_initialization_still_fail(self) -> None:
        output = (
            "Warning: Unknown toolsets: messaging\n"
            "Query: wrapped prompt\n"
            "Initializing agent...\n"
            f"{self._marker()}\n"
            f"{self._marker()}"
        )
        for transport, poll in (
            ("direct", self._run_direct(output)),
            ("runner", self._run_runner(output)),
        ):
            with self.subTest(transport=transport):
                self.assertEqual(poll.status, "failed")
                self.assertEqual(
                    poll.result["failure"]["kind"],
                    "completion_marker_duplicate",
                )

    def test_direct_and_runner_parity(self) -> None:
        prompt = _build_prompt(self.packet)
        cases = [
            ("valid", self._marker(), "completed"),
            ("prompt_echo", f"{prompt}\n{self._marker()}", "completed"),
            ("duplicate", f"{self._marker()}\n{self._marker()}", "failed"),
            ("missing", "no marker", "failed"),
            ("budget_no_marker", "Iteration budget exhausted (60/60)\nno marker", "input_required"),
        ]
        for case, output, expected_status in cases:
            with self.subTest(case=case):
                direct = self._run_direct(output)
                runner = self._run_runner(output)
                self.assertEqual(direct.status, runner.status, case)
                self.assertEqual(direct.status, expected_status, case)
                self.assertEqual(
                    direct.result["marker_valid"],
                    runner.result["marker_valid"],
                    case,
                )
                self.assertEqual(direct.result["failure"], runner.result["failure"], case)
                direct_callback = direct.result["agent_callback"]
                runner_callback = runner.result["agent_callback"]
                self.assertEqual(
                    (direct_callback or {}).get("final_state"),
                    (runner_callback or {}).get("final_state"),
                    case,
                )
                self.assertEqual(
                    (direct_callback or {}).get("step_results"),
                    (runner_callback or {}).get("step_results"),
                    case,
                )
                self.assertEqual(direct.result["iteration"], runner.result["iteration"], case)

    # ---- iteration-budget diagnostics ------------------------------------

    def test_iteration_budget_diagnostics_60_over_60(self) -> None:
        cases = [
            (
                "turn_exit_reason=max_iterations_reached(60/60)",
                "max_iterations_reached",
                60,
                60,
            ),
            (
                "⚠️  Iteration budget exhausted (60/60) — asking model to summarise",
                "iteration_budget_message",
                60,
                60,
            ),
            (
                "⚠️  Reached maximum iterations (60). Requesting summary...",
                "reached_maximum_iterations",
                60,
                60,
            ),
        ]
        for output, source, used, limit in cases:
            with self.subTest(source=source):
                diag = _iteration_budget_diagnostics(output, "")
                self.assertTrue(diag["iteration_exhausted"])
                self.assertEqual(diag["iteration_used"], used)
                self.assertEqual(diag["iteration_limit"], limit)
                self.assertEqual(diag["iteration_source"], source)

    def test_iteration_budget_diagnostics_budget_exhausted_reason(self) -> None:
        diag = _iteration_budget_diagnostics("turn_exit_reason: budget_exhausted", "")
        self.assertTrue(diag["iteration_exhausted"])
        self.assertEqual(diag["iteration_source"], "budget_exhausted")
        self.assertIsNone(diag["iteration_used"])
        self.assertIsNone(diag["iteration_limit"])

    def test_iteration_budget_diagnostics_absent(self) -> None:
        diag = _iteration_budget_diagnostics("all done here", "")
        self.assertFalse(diag["iteration_exhausted"])
        self.assertEqual(diag["iteration_source"], "none")
        self.assertIsNone(diag["iteration_used"])

    def test_budget_exhaustion_without_marker_is_system_input_required(self) -> None:
        output = (
            "⚠️  Reached maximum iterations (60). Requesting summary...\n"
            "⚠️  Iteration budget exhausted (60/60) — asking model to summarise\n"
            "I ran out of iterations before finishing."
        )
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "input_required")
        self.assertEqual(poll.result["failure"]["kind"], "resource_limit_exhausted")
        self.assertFalse(poll.result["failure"]["retryable"])
        self.assertIsNone(poll.result["agent_callback"])
        exhaustion = poll.result["resource_exhaustion"]
        self.assertTrue(exhaustion["detected"])
        self.assertEqual(exhaustion["executor"], "hermes")
        self.assertEqual(exhaustion["resource"], "max_turns")
        self.assertEqual(exhaustion["used"], 60)
        self.assertEqual(exhaustion["limit"], 60)
        iteration = poll.result["iteration"]
        self.assertTrue(iteration["iteration_exhausted"])
        self.assertEqual(iteration["iteration_used"], 60)
        self.assertEqual(iteration["iteration_limit"], 60)
        extensions_iteration = poll.result["extensions"]["executor"]["hermes"].get("iteration")
        self.assertTrue(extensions_iteration["iteration_exhausted"])
        self.assertEqual(extensions_iteration["iteration_used"], 60)

    def test_budget_exhaustion_with_nonzero_exit_is_system_input_required(self) -> None:
        output = "⚠️  Iteration budget exhausted (60/60) — asking model to summarise\nno marker"
        poll = self._run_direct(output, returncode=1)
        self.assertEqual(poll.status, "input_required")
        self.assertEqual(poll.result["failure"]["kind"], "resource_limit_exhausted")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(poll.result["resource_exhaustion"]["limit"], 60)

    def test_valid_completed_routes_normally_despite_budget_exhaustion(self) -> None:
        output = (
            "⚠️  Iteration budget exhausted (60/60) — asking model to summarise\n"
            f"{self._marker()}"
        )
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "completed")
        self.assertIsNotNone(poll.result["agent_callback"])
        self.assertIsNone(poll.result["failure"])
        self.assertTrue(poll.result["iteration"]["iteration_exhausted"])

    def test_valid_input_required_routes_normally_despite_budget_exhaustion(self) -> None:
        marker = self._marker(
            final_state="input_required",
            step_results=[{"id": 1, "status": "done"}, {"id": 2, "status": "blocked"}],
        )
        output = f"⚠️  Iteration budget exhausted (60/60) — asking model to summarise\n{marker}"
        poll = self._run_direct(output)
        self.assertEqual(poll.status, "input_required")
        self.assertEqual(poll.result["agent_callback"]["final_state"], "input_required")
        self.assertTrue(poll.result["iteration"]["iteration_exhausted"])

    def test_transport_recovery_preserved_despite_budget_output(self) -> None:
        output = (
            "⚠️  Iteration budget exhausted (60/60) — asking model to summarise\n"
            "no final marker"
        )
        poll = self._run_direct(output, stderr="Connection error: transport unavailable")
        self.assertEqual(poll.status, "needs_recovery")
        self.assertTrue(poll.result["failure"]["retryable"])

    # ---- inherited command construction ------------------------------------

    def test_inherited_command_construction_has_no_turn_limit_flags(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="direct")
        prompt = _build_prompt(self.packet)
        command = executor._build_command(prompt)
        joined = " ".join(command)
        self.assertNotIn("--max-turns", joined)
        self.assertNotIn("--ignore-user-config", joined)
        self.assertEqual(command[0], sys.executable)
        self.assertIn("chat", command)
        self.assertIn("-Q", command)
        self.assertIn("--source", command)
        self.assertIn("tool", command)
        self.assertEqual(command[-2], "-q")
        self.assertEqual(command[-1], prompt)

    def test_inherited_command_construction_keeps_profile_flags(self) -> None:
        executor = HermesExecutor(
            command=sys.executable,
            transport="direct",
            profile="pm",
            provider="openai",
            model="gpt-5",
        )
        command = executor._build_command(_build_prompt(self.packet))
        joined = " ".join(command)
        self.assertNotIn("--max-turns", joined)
        self.assertNotIn("--ignore-user-config", joined)
        self.assertIn("-p", command)
        self.assertIn("pm", command)
        self.assertIn("--provider", command)
        self.assertIn("openai", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5", command)

    # ---- contract-level helpers ---------------------------------------------

    def test_marker_validation_uses_final_response_not_raw_stdout(self) -> None:
        prompt = _build_prompt(self.packet)
        real_marker = self._marker()
        output = f"{prompt}\n{real_marker}"
        validation = extract_callback_validation_from_output(
            _extract_final_response(output, self.packet),
            self.packet,
            "run",
        )
        self.assertTrue(validation.valid)
        self.assertEqual(validation.callback["final_state"], "completed")
        raw_validation = extract_callback_validation_from_output(
            output,
            self.packet,
            "run",
        )
        self.assertFalse(raw_validation.valid)
        self.assertEqual(raw_validation.code, "completion_marker_duplicate")


if __name__ == "__main__":
    unittest.main()
