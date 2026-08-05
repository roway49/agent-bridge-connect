from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ConversationOriginTests(unittest.TestCase):
    def test_explicit_session_id_overrides_matching_environment_id(self):
        from agent_bridge_connect.cli import _origin_context

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "environment-thread"},
            clear=True,
        ):
            context = _origin_context("explicit-thread", "codex")

        self.assertEqual(context, ("explicit-thread", "codex"))

    def test_explicit_platform_uses_only_its_matching_environment_id(self):
        from agent_bridge_connect.cli import _origin_context

        cases = {
            "codex": "CODEX_THREAD_ID",
            "claude": "CLAUDE_SESSION_ID",
            "hermes": "HERMES_SESSION_ID",
            "opencode": "OPENCODE_SESSION_ID",
        }
        for platform, variable in cases.items():
            with self.subTest(platform=platform), mock.patch.dict(
                os.environ,
                {
                    variable: f"{platform}-thread",
                    "UNRELATED_SESSION_ID": "wrong-thread",
                },
                clear=True,
            ):
                context = _origin_context(None, platform)

            self.assertEqual(context, (f"{platform}-thread", platform))

    def test_detected_platform_isolates_mismatched_environment_id(self):
        from agent_bridge_connect.cli import _origin_context

        with mock.patch.dict(
            os.environ,
            {
                "CODEX_SHELL": "1",
                "CLAUDE_SESSION_ID": "wrong-platform-thread",
            },
            clear=True,
        ):
            context = _origin_context(None, None)

        self.assertEqual(context, (None, "codex"))

    def test_missing_matching_environment_id_is_unavailable(self):
        from agent_bridge_connect.cli import _origin_context

        with mock.patch.dict(
            os.environ,
            {"CLAUDE_SESSION_ID": "wrong-platform-thread"},
            clear=True,
        ):
            context = _origin_context(None, "hermes")

        self.assertEqual(context, (None, "hermes"))


class ConversationTraceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.board = self.root / "record"
        self.workspace = self.root / "workspace"
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            f'workspace_root = "{self.workspace}"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _service(self):
        from agent_bridge_connect.service import TaskService

        return TaskService(
            self.board,
            config={"workspace_root": str(self.workspace)},
        )

    def _completed_source(
        self,
        *,
        session_id: str = "source-conversation",
        source_platform: str = "claude",
    ):
        service = self._service()
        source = service.create_task(
            "Source task",
            "hermes",
            [{"id": 1, "description": "finish source"}],
            session_id=session_id,
            source_platform=source_platform,
            customer_dir=False,
        )
        data = service.store.read_task(source.id)
        data["status"] = "completed"
        data["steps"][0]["status"] = "done"
        service.store.write_task(source.id, data)
        return service, service.get_task(source.id)

    def test_create_cli_captures_matching_environment_conversation(self):
        from agent_bridge_connect.cli import build_parser, command_task_create

        steps = self.root / "steps.yaml"
        steps.write_text("steps:\n- description: create traced task\n", encoding="utf-8")
        args = build_parser().parse_args(
            [
                "task",
                "create",
                "--root",
                str(self.board),
                "--title",
                "Traced create",
                "--assignee",
                "hermes",
                "--steps",
                str(steps),
                "--source-platform",
                "codex",
                "--customer-path",
                "default path",
                "--config",
                str(self.config_path),
            ]
        )

        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "create-conversation",
                "CLAUDE_SESSION_ID": "wrong-conversation",
            },
            clear=True,
        ), contextlib.redirect_stdout(io.StringIO()):
            result = command_task_create(args)

        task = self._service().list_tasks()[0]
        requirements = Path(task.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertEqual(task.session_id, "create-conversation")
        self.assertEqual(
            task.extensions["agentbc.provenance"]["source_platform"],
            "codex",
        )
        self.assertIn("Dispatcher platform: `codex`", requirements)
        self.assertIn(
            "Dispatcher conversation ID: `create-conversation`",
            requirements,
        )

    def test_non_atomic_handoff_owns_new_id_without_mutating_source_trace(self):
        from agent_bridge_connect.cli import build_parser, command_task_intervention

        service, source = self._completed_source()
        args = build_parser().parse_args(
            [
                "task",
                "handoff",
                source.id,
                "--root",
                str(self.board),
                "--to",
                "codex",
                "--message",
                "continue",
                "--session-id",
                "handoff-conversation",
                "--source-platform",
                "codex",
                "--config",
                str(self.config_path),
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = command_task_intervention(args)

        source_after = service.get_task(source.id)
        followup = next(task for task in service.list_tasks() if task.id != source.id)
        requirements = Path(followup.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertEqual(source_after.session_id, "source-conversation")
        self.assertEqual(
            source_after.extensions["agentbc.provenance"]["source_platform"],
            "claude",
        )
        self.assertEqual(followup.session_id, "handoff-conversation")
        self.assertEqual(
            followup.extensions["agentbc.provenance"]["source_platform"],
            "codex",
        )
        self.assertIn(
            "Dispatcher conversation ID: `handoff-conversation`",
            requirements,
        )

    def test_handoff_without_current_id_does_not_inherit_source_id(self):
        service, source = self._completed_source()

        followup = service.handoff_task(
            source.id,
            "codex",
            "continue without an available dispatcher id",
            source_platform="codex",
        )

        requirements = Path(followup.workspace["task_file"]).read_text(encoding="utf-8")
        self.assertIsNone(followup.session_id)
        self.assertEqual(service.get_task(source.id).session_id, "source-conversation")
        self.assertIn("Dispatcher conversation ID: `unavailable`", requirements)

    def test_atomic_handoff_cli_passes_detected_conversation_to_runner_client(self):
        from agent_bridge_connect.cli import build_parser, command_task_intervention

        args = build_parser().parse_args(
            [
                "task",
                "handoff",
                "TRACE-001",
                "--root",
                str(self.board),
                "--to",
                "hermes",
                "--source-platform",
                "codex",
                "--dispatch",
            ]
        )
        dispatched = {
            "task_id": "TRACE-002",
            "assignee": "hermes",
            "run_id": "runner-worker-trace",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "atomic-cli-conversation"},
            clear=True,
        ), mock.patch(
            "agent_bridge_connect.runner.RunnerClient.handoff_and_dispatch",
            return_value=dispatched,
        ) as handoff, contextlib.redirect_stdout(io.StringIO()):
            result = command_task_intervention(args)

        self.assertEqual(result, 0)
        self.assertEqual(
            handoff.call_args.kwargs["session_id"],
            "atomic-cli-conversation",
        )
        self.assertEqual(handoff.call_args.kwargs["source_platform"], "codex")

    def test_runner_client_request_includes_handoff_conversation(self):
        from agent_bridge_connect.runner import RunnerClient

        client = RunnerClient(self.root / "spool", self.root / "token")
        with mock.patch.object(
            client,
            "_request",
            return_value={"task_id": "TRACE-002"},
        ) as request:
            client.handoff_and_dispatch(
                "TRACE-001",
                "hermes",
                "continue",
                self.board,
                None,
                source_platform="codex",
                session_id="runner-client-conversation",
            )

        payload = request.call_args.args[0]
        self.assertEqual(payload["session_id"], "runner-client-conversation")
        self.assertEqual(payload["source_platform"], "codex")

    def test_runner_request_stores_atomic_handoff_conversation(self):
        from agent_bridge_connect.runner import RunnerState
        from agent_bridge_connect.service import TaskService

        executable = self.root / "hermes"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        state = RunnerState(
            self.root / "runner-state",
            [self.root],
            {"hermes": executable},
        )
        service = TaskService(
            self.board,
            config={"workspace_root": str(self.root)},
        )
        source = service.create_task(
            "Atomic source",
            "hermes",
            [{"id": 1, "description": "finish source"}],
            session_id="source-atomic-conversation",
            source_platform="claude",
            customer_dir=True,
            customer_path=self.root,
        )
        source_data = service.store.read_task(source.id)
        source_data["status"] = "completed"
        source_data["steps"][0]["status"] = "done"
        service.store.write_task(source.id, source_data)
        dispatched = {
            "run_id": "runner-worker-atomic-trace",
            "pid": 1234,
            "status": "running",
            "task_id": f"{source.workspace['task_code']}-002",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }

        with mock.patch.object(
            state,
            "dispatch_worker",
            return_value=dispatched,
        ):
            result = state.handoff_and_dispatch(
                {
                    "source_task_id": source.id,
                    "target_assignee": "hermes",
                    "message": "continue atomically",
                    "session_id": "runner-request-conversation",
                    "source_platform": "codex",
                    "board_root": str(self.board),
                    "interval_s": 2,
                    "monitor": False,
                }
            )

        followup = TaskService(self.board).get_task(result["task_id"])
        source_after = TaskService(self.board).get_task(source.id)
        self.assertEqual(followup.session_id, "runner-request-conversation")
        self.assertEqual(
            followup.extensions["agentbc.provenance"]["source_platform"],
            "codex",
        )
        self.assertEqual(source_after.session_id, "source-atomic-conversation")


if __name__ == "__main__":
    unittest.main()
