"""PERM-104-001 deterministic regression tests for Hermes ACP long runs.

TJBS-001 (executor run ``hermes-TJBS-001-46b34d3d`` then
``hermes-TJBS-001-1eb6432e``) proved two independent Hermes ACP defects:

* a healthy turn was killed as ``hermes_acp_transport_failed`` because the
  transport bounded every receive by the 30s per-RPC handshake timeout while a
  real model call stayed silent for 45.1s;
* the recovered run then returned ``0`` with ``completion_marker_missing``
  because the ``session/update`` collector probed
  ``params.sessionUpdate[].message[].content[]``, a shape no real ACP agent
  emits, so the executor's terminal answer - including its
  ``AGENTBC_FINAL_CALLBACK`` - was never captured.

These tests freeze the repaired contract with real stdio subprocesses (no
Hermes runtime, no network, no private executor state) plus the executor-level
RunLease and session-cleanup guarantees.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.execution_policy import build_session_snapshot
from agent_bridge_connect.executors import hermes as hermes_executor_module
from agent_bridge_connect.executors.hermes import (
    HermesExecutor,
    _RunLeaseHeartbeat,
    _hermes_session_delete_identifier_error,
)
from agent_bridge_connect.hermes_acp import (
    HERMES_ACP_MESSAGE_TAIL_BYTES as _HERMES_ACP_MESSAGE_TAIL_BYTES,
    HERMES_ACP_RECEIVE_TIMEOUT_S,
    HermesAcpError,
    HermesAcpTimeout,
    HermesAcpTransport,
)
from agent_bridge_connect.permission_modes import build_permission_record
from agent_bridge_connect.run_lease import load_lease
from agent_bridge_connect.adapters import SessionCleanupRequest

FAKE_SERVER = (
    Path(__file__).parent / "fixtures" / "executor_runtime" / "hermes_acp_fake_server.py"
)
SESSION_ID = "acp-session-fake-1"
OTHER_SESSION_ID = "acp-session-other-1"
TASK_ID = "59QH-001"


def _marker_line() -> str:
    return "AGENTBC_FINAL_CALLBACK: " + json.dumps(
        {
            "version": 1,
            "task_id": TASK_ID,
            "final_state": "completed",
            "summary": "fake hermes turn completed",
            "step_results": [
                {"id": 1, "status": "done"},
                {"id": 2, "status": "done"},
                {"id": 3, "status": "done"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class _AcpBoardBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / "record"
        self.board.mkdir()

    def _transport(self, mode: str, *, silence_s: float = 0.0, **kwargs) -> HermesAcpTransport:
        options = {"rpc_timeout_s": 5.0, "receive_timeout_s": 5.0}
        options.update(kwargs)
        transport = HermesAcpTransport(
            sys.executable,
            cwd=self.root,
            command=[sys.executable, str(FAKE_SERVER), mode, str(self.root / "server.jsonl")]
            + ([str(silence_s)] if silence_s else []),
            **options,
        )
        transport.start()
        self.addCleanup(transport.close)
        return transport

    def _run_turn(self, mode: str, *, silence_s: float = 0.0, **kwargs):
        transport = self._transport(mode, silence_s=silence_s, **kwargs)
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        outcome: dict = {}
        try:
            response = transport.prompt(
                session_id,
                [{"type": "text", "text": "run the task"}],
                on_permission=lambda request: {"outcome": {"outcome": "cancelled"}},
                timeout_s=20.0,
            )
            outcome["response"] = response
            outcome["message_text"] = transport.message_text()
            outcome["session_id"] = session_id
        except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
            outcome["error"] = exc
        return outcome


class HermesAcpFrameAssemblyTests(_AcpBoardBase):
    """Complete frames are decoded whole, never truncated."""

    def test_split_agent_message_chunks_deliver_the_terminal_marker(self) -> None:
        outcome = self._run_turn("split_frames")
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertEqual(outcome["response"]["stopReason"], "end_turn")
        text = outcome["message_text"]
        self.assertIn("Step1 wrote the artifact.", text)
        self.assertIn("Step2 byte-verified it.", text)
        self.assertIn("Step3 finishing unattended.", text)
        self.assertEqual(text.count("AGENTBC_FINAL_CALLBACK:"), 1)
        self.assertIn(_marker_line(), text)

    def test_marker_split_mid_line_across_two_writes_is_still_complete(self) -> None:
        """A partial frame must never be decoded as a truncated frame."""
        outcome = self._run_turn("truncated_marker")
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertEqual(outcome["message_text"].count("AGENTBC_FINAL_CALLBACK:"), 1)
        self.assertIn(_marker_line(), outcome["message_text"])

    def test_unrelated_session_updates_never_reach_the_terminal_answer(self) -> None:
        outcome = self._run_turn("unrelated_session_update")
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertNotIn("stale answer from another session", outcome["message_text"])
        self.assertEqual(outcome["message_text"].count("AGENTBC_FINAL_CALLBACK:"), 1)

    def test_turn_budget_preserves_the_tail_of_the_terminal_answer(self) -> None:
        transport = self._transport("happy")
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        transport._collecting_session_id = session_id
        # The marker is the LAST line of the terminal answer, so a budget that
        # drops the tail silently discards the executor's completion.
        transport.message_max_bytes = 4096
        head = "x" * 8000
        transport._append_message_text(head)
        transport._append_message_text("\nsummary\n" + _marker_line())
        self.assertTrue(transport.message_truncated())
        text = transport.message_text()
        # The whole turn no longer fits, but the terminal marker survives and
        # the truncation is reported instead of being silent.
        self.assertLessEqual(
            len(text.encode("utf-8")),
            transport.message_max_bytes + _HERMES_ACP_MESSAGE_TAIL_BYTES + 1024,
        )
        self.assertIn(_marker_line(), text)

    def test_receive_defaults_keep_a_healthy_turn_unbounded_by_the_rpc_timeout(self) -> None:
        self.assertGreater(HERMES_ACP_RECEIVE_TIMEOUT_S, 60.0)


class HermesAcpLongIntervalTests(_AcpBoardBase):
    """A silent model/tool interval inside a healthy turn is not a failure."""

    def test_turn_survives_a_silent_interval_longer_than_the_rpc_timeout(self) -> None:
        # rpc_timeout_s=0.3 is the exact bound that killed TJBS-001 run 1; the
        # fake stays silent for 1.2s in the middle of the turn.
        outcome = self._run_turn("long_interval", silence_s=1.2, rpc_timeout_s=0.3)
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertIn("starting the long tool call", outcome["message_text"])
        self.assertIn("tool call finished", outcome["message_text"])
        self.assertIn(_marker_line(), outcome["message_text"])
        self.assertEqual(outcome["response"]["stopReason"], "end_turn")

    def test_the_old_per_frame_bound_would_have_failed_the_same_turn(self) -> None:
        """Guard the regression: the receive window is the only long bound."""
        transport = self._transport("long_interval", silence_s=1.2, rpc_timeout_s=0.3)
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        with self.assertRaises(HermesAcpTimeout):
            transport.prompt(
                session_id,
                [{"type": "text", "text": "run the task"}],
                on_permission=lambda request: {"outcome": {"outcome": "cancelled"}},
                timeout_s=20.0,
                receive_timeout_s=0.2,
            )


class HermesAcpTransportTruthTests(_AcpBoardBase):
    """Timeout, EOF and process exit stay three distinct truthful failures."""

    def test_hung_transport_is_an_idle_timeout_and_never_a_completion(self) -> None:
        outcome = self._run_turn("hang", rpc_timeout_s=5.0, receive_timeout_s=0.5)
        error = outcome.get("error")
        self.assertIsInstance(error, HermesAcpTimeout)
        self.assertEqual(error.code, "hermes_acp_receive_idle_timeout")
        self.assertEqual(error.details["timeout_s"], 0.5)
        # A timeout is a real failure: the adapter must keep timeout_is_failure.
        self.assertIsInstance(error, TimeoutError)

    def test_closed_stdout_while_alive_is_eof(self) -> None:
        outcome = self._run_turn("close_stdout")
        error = outcome.get("error")
        self.assertIsInstance(error, HermesAcpError)
        self.assertEqual(error.code, "hermes_acp_transport_eof")

    def test_closed_stdout_after_exit_is_reported_as_a_process_exit(self) -> None:
        outcome = self._run_turn("eof_mid_prompt")
        error = outcome.get("error")
        self.assertIsInstance(error, HermesAcpError)
        self.assertEqual(error.code, "hermes_acp_transport_exited")

    def test_process_exit_carries_the_exit_code(self) -> None:
        outcome = self._run_turn("exit_mid_prompt")
        error = outcome.get("error")
        self.assertIsInstance(error, HermesAcpError)
        self.assertEqual(error.code, "hermes_acp_transport_exited")
        self.assertEqual(error.details["exit_code"], 0)

    def test_overall_turn_deadline_is_reported_as_the_prompt_timeout(self) -> None:
        transport = self._transport("hang", rpc_timeout_s=5.0, receive_timeout_s=5.0)
        transport.initialize()
        session_id = transport.new_session(str(self.root))
        with self.assertRaises(HermesAcpTimeout) as ctx:
            transport.prompt(
                session_id,
                [{"type": "text", "text": "run the task"}],
                on_permission=lambda request: {"outcome": {"outcome": "cancelled"}},
                timeout_s=0.4,
            )
        self.assertEqual(ctx.exception.code, "hermes_acp_prompt_timeout")


class HermesAcpTerminalResultTests(_AcpBoardBase):
    """The executor's actual terminal answer is preserved and bridged exactly."""

    def _packet(self, *, resumed: bool = False) -> dict:
        session = build_session_snapshot(
            "hermes",
            retain=False,
            session_id=SESSION_ID if resumed else "",
            session_state="active" if resumed else "pending",
            run_ids=["prior-run"] if resumed else [],
        )
        return {
            "task_id": TASK_ID,
            "assignee": "hermes",
            "title": "hermes acp long run",
            "steps": [
                {"id": index, "description": f"declared step {index}"} for index in (1, 2, 3)
            ],
            "workspace": {"root": str(self.root), "project_root": str(self.root)},
            "task_board": {"root": str(self.board)},
            "extensions": {
                "agentbc.permission": build_permission_record(explicit_mode="safe"),
                "agentbc.session": session,
            },
        }

    def _executor(self, mode: str, *, silence_s: float = 0.0) -> HermesExecutor:
        executor = HermesExecutor(
            command=sys.executable,
            transport="acp",
            approval_timeout_s=2.0,
        )
        executor._acp_transport_override = HermesAcpTransport(
            sys.executable,
            cwd=self.root,
            command=[sys.executable, str(FAKE_SERVER), mode, str(self.root / "server.jsonl")]
            + ([str(silence_s)] if silence_s else []),
            rpc_timeout_s=5.0,
        )
        return executor

    def _wait_status(self, executor, run_id, statuses, timeout_s: float = 30.0) -> str:
        deadline = time.monotonic() + timeout_s
        status = ""
        while time.monotonic() < deadline:
            status = str(executor.poll(run_id).status)
            if status in statuses:
                return status
            time.sleep(0.02)
        return status

    def test_completed_turn_yields_exactly_one_valid_callback_for_all_steps(self) -> None:
        executor = self._executor("split_frames")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "completed", result.result.get("failure"))
        payload = result.result
        self.assertTrue(payload["marker_seen"])
        self.assertTrue(payload["marker_valid"])
        callback = payload["agent_callback"]
        self.assertEqual(callback["final_state"], "completed")
        self.assertEqual(callback["task_id"], TASK_ID)
        self.assertEqual(
            [step["id"] for step in callback["step_results"]], [1, 2, 3]
        )
        # The executor's real terminal answer is preserved, not synthesized.
        self.assertIn("Step2 byte-verified it.", payload["stdout"])
        self.assertEqual(payload["returncode"], 0)
        self.assertEqual(payload["failure"], None)

    def test_missing_marker_fails_closed_even_with_a_zero_exit_code(self) -> None:
        executor = self._executor("no_marker")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        # A turn that produced a normal ``end_turn`` result but no marker is
        # never a completed run.
        self.assertEqual(status, "failed")
        payload = result.result
        self.assertEqual(payload["failure"]["kind"], "incomplete_normal_exit")
        self.assertFalse(payload["marker_seen"])
        self.assertIsNone(payload["agent_callback"])
        self.assertEqual(payload["returncode"], 0)
        terminal = payload["terminal_receipt"]
        self.assertEqual(terminal["task_id"], TASK_ID)
        self.assertEqual(terminal["session_id"], SESSION_ID)
        self.assertEqual(terminal["session_source"], "acp_session")
        self.assertEqual(terminal["reason"], "incomplete_normal_exit")

    def test_duplicate_marker_is_rejected(self) -> None:
        executor = self._executor("duplicate_marker")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "failed")
        self.assertEqual(result.result["failure"]["kind"], "completion_marker_duplicate")
        self.assertIsNone(result.result["agent_callback"])

    def test_transport_death_keeps_needs_recovery_on_the_same_official_session(self) -> None:
        executor = self._executor("eof_mid_prompt")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet())
            status = self._wait_status(executor, started.run_id, {"needs_recovery", "failed", "completed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "needs_recovery")
        self.assertTrue(result.result["failure"]["retryable"])
        receipt = result.result["execution_session"]
        self.assertEqual(receipt["session_id"], SESSION_ID)
        self.assertEqual(receipt["persistence"], "persistent")

    def test_retry_on_the_same_official_session_completes(self) -> None:
        executor = self._executor("resume_direct")
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_suspend_run"),
            mock.patch.object(executor, "_resume_run"),
            mock.patch.object(executor, "_close_run_lease"),
        ):
            started = executor.start(self._packet(resumed=True))
            status = self._wait_status(executor, started.run_id, {"completed", "needs_recovery", "failed"})
            result = executor.poll(started.run_id)
        self.assertEqual(status, "completed", result.result.get("failure"))
        payload = result.result
        self.assertTrue(payload["marker_valid"])
        self.assertEqual(payload["execution_session"]["session_id"], SESSION_ID)
        self.assertTrue(payload["execution_session"]["resumed"])

    def test_poll_keeps_the_run_lease_healthy_while_the_turn_is_in_flight(self) -> None:
        executor = self._executor("hang")
        executor._acp_transport_override = None
        packet = self._packet()
        lease = executor._start_run_lease(packet, "acp-run-1", "hermes")
        self.addCleanup(executor._close_run_lease, "acp-run-1")
        executor._acp_runs["acp-run-1"] = {
            "status": "prompting",
            "events": [],
            "plane": mock.Mock(root=str(self.board)),
            "result": {},
        }
        stale_at = lease.last_heartbeat_at
        time.sleep(0.02)
        executor.poll("acp-run-1")
        refreshed = load_lease(lease.task_id, self.board)
        self.assertEqual(refreshed.run_id, "acp-run-1")
        self.assertGreaterEqual(refreshed.last_heartbeat_at, stale_at)

    def test_heartbeat_beat_is_rate_limited_to_one_write_per_interval(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="acp")
        lease = executor._start_run_lease(
            {
                "task_id": TASK_ID,
                "workspace": {"root": str(self.root)},
                "task_board": {"root": str(self.board)},
            },
            "acp-run-2",
            "hermes",
        )
        self.addCleanup(executor._close_run_lease, "acp-run-2")
        with mock.patch.object(
            hermes_executor_module,
            "_HERMES_ACP_HEARTBEAT_INTERVAL_S",
            0.01,
        ):
            heartbeat = _RunLeaseHeartbeat(executor, "acp-run-2")
            first = lease.last_heartbeat_at
            heartbeat.beat()
            second = load_lease(lease.task_id, self.board).last_heartbeat_at
            heartbeat.beat()
            third = load_lease(lease.task_id, self.board).last_heartbeat_at
        self.assertGreaterEqual(second, first)
        # A second beat inside the same interval must not rewrite the lease.
        self.assertEqual(second, third)
        heartbeat.stop()


_DELETE_HELP = (
    "usage: hermes sessions delete [-h] [--yes] session_id\n"
    "\n"
    "positional arguments:\n"
    "  session_id  session to delete\n"
    "  --yes       skip confirmation\n"
)


class HermesSessionDeleteContractTests(unittest.TestCase):
    """``hermes sessions delete`` receives the exact bound identifier only."""

    def _request(self, session_id: str) -> SessionCleanupRequest:
        return SessionCleanupRequest(
            executor="hermes",
            session_id=session_id,
            task_id=TASK_ID,
            retain=False,
            project_mode="none",
            strategy="official_session_delete",
            project_path="",
            workspace={},
            receipt_source="stderr_receipt",
            official_receipt_bound=True,
        )

    def test_acp_receipt_identifier_is_accepted(self) -> None:
        # The exact identifier TJBS-001 bound and then failed to delete.
        self.assertEqual(
            _hermes_session_delete_identifier_error(
                "18a3e156-6aae-4286-b504-4276f90fc5b2"
            ),
            "",
        )

    def test_cli_token_identifier_is_accepted(self) -> None:
        self.assertEqual(
            _hermes_session_delete_identifier_error("20260811_004323_d3bd9b"), ""
        )

    def test_missing_invalid_and_injection_identifiers_are_rejected(self) -> None:
        cases = {
            "": "hermes_session_delete_missing_session_id",
            "--yes": "hermes_session_delete_invalid_session_id",
            "-": "hermes_session_delete_invalid_session_id",
            "20260811 bad": "hermes_session_delete_invalid_session_id",
            "delete --all": "hermes_session_delete_invalid_session_id",
            "session-name": "hermes_session_delete_invalid_session_id",
            "../../etc": "hermes_session_delete_invalid_session_id",
            "18a3e156-6aae-4286-b504-4276f90fc5b": "hermes_session_delete_invalid_session_id",
            "x" * 129: "hermes_session_delete_invalid_session_id",
        }
        for session_id, expected in cases.items():
            with self.subTest(session_id=session_id):
                self.assertEqual(
                    _hermes_session_delete_identifier_error(session_id), expected
                )

    def test_cleanup_spawns_exactly_the_bound_identifier(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="acp")
        executor.agent_bin = Path("/tmp/hermes-unused")
        bound = "18a3e156-6aae-4286-b504-4276f90fc5b2"
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run"
        ) as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout=_DELETE_HELP, stderr=""),
                mock.Mock(returncode=0, stdout="Deleted session.\n", stderr=""),
            ]
            result = executor.cleanup_session(self._request(bound))
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["/tmp/hermes-unused", "sessions", "delete", bound, "--yes"],
        )

    def test_cleanup_never_targets_a_dispatcher_or_unrelated_session(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="acp")
        executor.agent_bin = Path("/tmp/hermes-unused")
        bound = "18a3e156-6aae-4286-b504-4276f90fc5b2"
        unrelated = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run"
        ) as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout=_DELETE_HELP, stderr=""),
                mock.Mock(returncode=0, stdout="Deleted session.\n", stderr=""),
            ]
            executor.cleanup_session(self._request(bound))
        argv = run.call_args_list[1].args[0]
        self.assertEqual(argv.count(bound), 1)
        self.assertNotIn(unrelated, argv)
        self.assertNotIn("", argv)
        self.assertNotIn("--all", argv)
        self.assertNotIn("--yes", argv[:-1])

    def test_repeat_cleanup_is_identity_stable(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="acp")
        executor.agent_bin = Path("/tmp/hermes-unused")
        bound = "18a3e156-6aae-4286-b504-4276f90fc5b2"
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run"
        ) as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout=_DELETE_HELP, stderr=""),
                mock.Mock(returncode=0, stdout="Deleted session.\n", stderr=""),
                mock.Mock(returncode=0, stdout=_DELETE_HELP, stderr=""),
                mock.Mock(returncode=0, stdout="Deleted session.\n", stderr=""),
            ]
            first = executor.cleanup_session(self._request(bound))
            second = executor.cleanup_session(self._request(bound))
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.error_code, second.error_code)
        self.assertEqual(first.strategy, second.strategy)
        # Same request -> the same delete argv (help probes are calls 0 and 2).
        self.assertEqual(
            run.call_args_list[1].args[0], run.call_args_list[3].args[0]
        )

    def test_absent_session_is_reported_as_already_absent(self) -> None:
        executor = HermesExecutor(command=sys.executable, transport="acp")
        executor.agent_bin = Path("/tmp/hermes-unused")
        bound = "18a3e156-6aae-4286-b504-4276f90fc5b2"
        with mock.patch(
            "agent_bridge_connect.executors.hermes.subprocess.run"
        ) as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout=_DELETE_HELP, stderr=""),
                mock.Mock(
                    returncode=1,
                    stdout=f"Session '{bound}' not found.\n",
                    stderr="",
                ),
            ]
            result = executor.cleanup_session(self._request(bound))
        self.assertEqual(result.state, "succeeded")


if __name__ == "__main__":
    unittest.main()
