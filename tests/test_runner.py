from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.contract_helpers import completed_callback


class RunnerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_hermes = self.root / "hermes"
        self.fake_hermes.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"sleep\" ]; then sleep 30; fi\n"
            "printf 'RUNNER_OK'\n",
            encoding="utf-8",
        )
        self.fake_hermes.chmod(self.fake_hermes.stat().st_mode | stat.S_IXUSR)
        self.fake_claude = self.root / "claude"
        self.fake_claude.write_text(
            "#!/bin/sh\n"
            "printf 'CLAUDE_OK'\n",
            encoding="utf-8",
        )
        self.fake_claude.chmod(self.fake_claude.stat().st_mode | stat.S_IXUSR)
        from agent_bridge_connect.runner import RunnerState

        self.state = RunnerState(
            self.root / "state",
            [self.root],
            {"hermes": self.fake_hermes, "claude": self.fake_claude},
        )

    def tearDown(self):
        self.temp.cleanup()

    def _authorized_task(self, executor="hermes", mode="safe", workspace=None):
        from agent_bridge_connect.service import TaskService

        board = self.root / "permission-board"
        project = Path(workspace or self.root)
        config = {"workspace_root": str(self.root)}
        if executor == "claude":
            config["sessions"] = {"retain_executor_sessions": True}
        service = TaskService(board, config=config)
        task = service.create_task(
            "Runner permission authorization",
            executor,
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=project,
            permission_mode=mode,
        )
        packet = task.to_dict()
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(board)}
        return packet

    def test_submit_and_poll_to_completion(self):
        task = self._authorized_task()
        result = self.state.submit(
            "hermes",
            [str(self.fake_hermes), "chat", "-q", "--max-turns", "90", "hello"],
            str(self.root),
            task=task,
        )
        terminal = self._wait_terminal(result["run_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["returncode"], 0)
        self.assertEqual(terminal["stdout"], "RUNNER_OK")

    def test_rejects_unsafe_flags_and_cwd(self):
        from agent_bridge_connect.runner import RunnerError

        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.submit(
                "hermes",
                [str(self.fake_hermes), "chat", "-q", "hello", "--yolo"],
                str(self.root),
                task=self._authorized_task(),
            )
        with self.assertRaisesRegex(RunnerError, "outside allowed roots"):
            self.state.submit(
                "hermes",
                [str(self.fake_hermes), "chat", "-q", "hello"],
                self.root.anchor,
                task=self._authorized_task(),
            )

    def test_claude_command_requires_safe_print_mode(self):
        from agent_bridge_connect.runner import RunnerError

        task = self._authorized_task("claude")
        session_id = task["extensions"]["agentbc.session"]["session_id"]
        result = self.state.submit(
            "claude",
            [
                str(self.fake_claude),
                "-p",
                "--safe-mode",
                "--permission-mode",
                "acceptEdits",
                "--output-format",
                "text",
                "--max-budget-usd",
                "10.0",
                "--session-id",
                session_id,
                "hello",
            ],
            str(self.root),
            task=task,
        )
        terminal = self._wait_terminal(result["run_id"])
        self.assertEqual(terminal["stdout"], "CLAUDE_OK")

        with self.assertRaisesRegex(RunnerError, "requires one of"):
            self.state.submit(
                "claude",
                [str(self.fake_claude), "--safe-mode", "hello"],
                str(self.root),
                task=self._authorized_task("claude"),
            )
        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.submit(
                "claude",
                [str(self.fake_claude), "-p", "hello"],
                str(self.root),
                task=self._authorized_task("claude"),
            )

    def test_claude_runner_rejects_dangerous_permissions(self):
        from agent_bridge_connect.runner import RunnerError

        base = [
            str(self.fake_claude),
            "-p",
            "--safe-mode",
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "text",
            "hello",
        ]
        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.submit(
                "claude",
                [*base, "--dangerously-skip-permissions"],
                str(self.root),
                task=self._authorized_task("claude"),
            )
        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.submit(
                "claude",
                [
                    str(self.fake_claude),
                    "-p",
                    "--safe-mode",
                    "--permission-mode",
                    "bypassPermissions",
                    "hello",
                ],
                str(self.root),
                task=self._authorized_task("claude"),
            )

    def test_cancel_terminates_process_group(self):
        command = [
            str(self.fake_hermes),
            "sleep",
            "chat",
            "-q",
            "--max-turns",
            "90",
            "hello",
        ]
        result = self.state.submit(
            "hermes", command, str(self.root), task=self._authorized_task()
        )
        cancelled = self.state.cancel(result["run_id"])
        self.assertIn(cancelled["status"], {"cancelling", "cancelled"})
        terminal = self._wait_terminal(result["run_id"])
        self.assertEqual(terminal["status"], "cancelled")

    def test_token_is_owner_only(self):
        from agent_bridge_connect.runner import _load_or_create_token

        token_path = self.root / "token"
        token = _load_or_create_token(token_path)
        self.assertTrue(token)
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_custom_spool_derives_its_own_token_path(self):
        from agent_bridge_connect.runner import RunnerClient

        client = RunnerClient(spool_root=self.root / "custom-spool")

        self.assertEqual(client.token_path, self.root / "custom-spool" / "token")

    def test_runner_service_rejects_second_instance_for_same_spool(self):
        from agent_bridge_connect.runner import RunnerError, RunnerService

        spool = self.root / "spool-singleton"
        first = RunnerService(spool, spool / "token", self.state, interval_s=0.01)
        try:
            with self.assertRaisesRegex(RunnerError, "runner already running"):
                RunnerService(spool, spool / "token", self.state, interval_s=0.01)
        finally:
            first.shutdown()
        second = RunnerService(spool, spool / "token", self.state, interval_s=0.01)
        second.shutdown()

    def test_managed_report_write_is_atomic_and_restricted(self):
        from agent_bridge_connect.runner import RunnerError

        report = self.root / "tasks" / "report" / "2026-07-14" / "ABCD" / "ABCD-001-report.md"
        result = self.state.write_report(str(report), "# Report\n")
        self.assertEqual(result["path"], str(report.resolve()))
        self.assertEqual(report.read_text(encoding="utf-8"), "# Report\n")
        with self.assertRaisesRegex(RunnerError, "only allow TASKCODE-001-report.md"):
            self.state.write_report(str(report.with_name("arbitrary.txt")), "blocked")
        with self.assertRaisesRegex(RunnerError, "outside allowed roots"):
            self.state.write_report(
                str(Path(self.root.anchor) / "tasks" / "report" / "2026-07-14" / "ABCD" / "ABCD-001-report.md"),
                "blocked",
            )

    def test_dispatch_worker_is_task_scoped_and_records_run(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        workspace = self.root / "project"
        workspace.mkdir()
        service = TaskService(board, config={"workspace_root": str(self.root)})
        task = service.create_task(
            "Worker dispatch",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=workspace,
        )
        task_dir = service.store.task_dir(task.id)
        spawned = {
            "ok": True,
            "run_id": "runner-worker-test",
            "pid": 1234,
            "status": "running",
        }
        with mock.patch.object(self.state, "_spawn_process", return_value=spawned) as start:
            with mock.patch.object(
                self.state,
                "_open_task_monitor",
                return_value={"status": "opened"},
            ) as monitor:
                result = self.state.dispatch_worker(task.id, "hermes", str(board), "", 0.2, True)
        self.assertEqual(result["dispatch_status"], "accepted")
        self.assertEqual(result["monitor_status"], "opened")
        monitor.assert_called_once_with(task.id, board.resolve())
        command = start.call_args.args[1]
        self.assertIn("--task-id", command)
        self.assertEqual(command[command.index("--task-id") + 1], task.id)
        task = __import__("json").loads((task_dir / "task.json").read_text(encoding="utf-8"))
        execution = task["extensions"]["agentbc.execution"]
        self.assertEqual(execution["worker_run_id"], "runner-worker-test")

    def test_dispatch_worker_allows_task_scoped_customer_path(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        with tempfile.TemporaryDirectory() as customer_temp:
            workspace = Path(customer_temp) / "customer-project"
            workspace.mkdir()
            service = TaskService(board, config={"workspace_root": str(self.root)})
            task = service.create_task(
                "External customer project",
                "hermes",
                [{"id": 1, "description": "run in customer project"}],
                customer_dir=True,
                customer_path=workspace,
            )
            self.assertFalse(workspace.resolve().is_relative_to(self.root.resolve()))
            spawned = {
                "ok": True,
                "run_id": "runner-worker-external",
                "pid": 1234,
                "status": "running",
            }
            with mock.patch.object(self.state, "_spawn_process", return_value=spawned) as start:
                result = self.state.dispatch_worker(task.id, "hermes", str(board), "", 0.2, False)

        self.assertEqual(result["dispatch_status"], "accepted")
        self.assertEqual(start.call_args.args[2], workspace.resolve())

    def test_submit_accepts_task_scoped_customer_path_when_task_plan_present(self):
        from agent_bridge_connect.runner import RunnerError
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        with tempfile.TemporaryDirectory() as customer_temp:
            workspace = Path(customer_temp) / "customer-project"
            workspace.mkdir()
            task = TaskService(board, config={"workspace_root": str(self.root)}).create_task(
                "External Hermes submit",
                "hermes",
                [{"id": 1, "description": "run in customer project"}],
                customer_dir=True,
                customer_path=workspace,
            )
            command = [
                str(self.fake_hermes),
                "chat",
                "-q",
                "--max-turns",
                "90",
                "hello",
            ]
            with self.assertRaisesRegex(RunnerError, "missing persisted"):
                self.state.submit("hermes", command, str(workspace))

            packet = task.to_dict()
            packet["task_id"] = task.id
            packet["task_board"] = {"root": str(board)}
            result = self.state.submit("hermes", command, str(workspace), task=packet)
            terminal = self._wait_terminal(result["run_id"])

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["stdout"], "RUNNER_OK")

    def test_process_sample_summarizes_runner_process_pressure(self):
        class Completed:
            returncode = 0
            stdout = (
                " 123 1 2.5 1.0 2048 S 00:01 python -m agent_bridge_connect.cli runner serve\n"
                " 456 1 4.0 2.0 4096 S 00:02 /Applications/Codex.app/Contents/MacOS/Codex\n"
            )
            stderr = ""

        with mock.patch("agent_bridge_connect.runner.subprocess.run", return_value=Completed()) as run:
            sample = self.state.process_sample(["agentbc", "codex"])

        run.assert_called_once()
        self.assertEqual(sample["source"], "runner_ps")
        self.assertEqual(sample["count"], 2)
        self.assertEqual(sample["groups"]["agentbc_runner"]["rss_mb_sum"], 2.0)
        self.assertEqual(sample["groups"]["codex_gui"]["rss_mb_sum"], 4.0)

    def test_open_task_monitor_uses_module_cli_command(self):
        with mock.patch("agent_bridge_connect.runner.subprocess.Popen") as popen:
            result = self.state._open_task_monitor("T-001", self.root / "abc-tasks")
        self.assertEqual(result["status"], "opened")
        args = popen.call_args.args[0]
        self.assertIn("/usr/bin/osascript", args)
        self.assertIn("-e", args)
        command = args[-1]
        self.assertIn("-m agent_bridge_connect.cli task logs T-001", command)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_open_task_list_dashboard_pins_task_without_self_close_prompt(self):
        with mock.patch("agent_bridge_connect.runner.subprocess.Popen") as popen:
            result = self.state._open_task_list_dashboard(self.root / "abc-tasks", task_id="ABCD-001")
        self.assertEqual(result["status"], "opened")
        args = popen.call_args.args[0]
        command = args[-1]
        self.assertIn("task list --root", command)
        self.assertIn("--watch-task-id ABCD-001", command)
        self.assertNotIn("--current --watch", command)
        self.assertIn("__agentbc_window_id=", command)
        self.assertIn("sleep 0.8", command)
        self.assertIn("/usr/bin/nohup /bin/sh -c", command)
        self.assertIn("AGENTBC_WINDOW_ID", command)
        self.assertIn("close window id $AGENTBC_WINDOW_ID", command)
        self.assertIn("disown", command)
        self.assertTrue(command.endswith("exit $__agentbc_status"))

    def test_runner_appends_dispatches_to_active_dashboard_cohort(self):
        from agent_bridge_connect.task_health import (
            dashboard_task_ids,
            mark_dashboard_closed,
        )

        board = self.root / "abc-tasks"
        self.state.enable_task_dashboard = True
        try:
            with mock.patch.object(
                self.state,
                "_open_task_list_dashboard",
                return_value={"status": "opened"},
            ) as opened:
                first = self.state._ensure_task_list_dashboard(board, task_id="ABCD-001")
                second = self.state._ensure_task_list_dashboard(board, task_id="EFGH-001")

            self.assertEqual(first["status"], "opened")
            self.assertEqual(second["status"], "refreshed")
            opened.assert_called_once_with(board, task_id="ABCD-001")
            self.assertEqual(dashboard_task_ids(board), ["ABCD-001", "EFGH-001"])
        finally:
            mark_dashboard_closed(board)

    def test_runner_releases_dashboard_reservation_when_open_fails(self):
        from agent_bridge_connect.task_health import dashboard_is_active

        board = self.root / "abc-tasks"
        self.state.enable_task_dashboard = True
        with mock.patch.object(
            self.state,
            "_open_task_list_dashboard",
            return_value={"status": "failed", "message": "osascript unavailable"},
        ):
            result = self.state._ensure_task_list_dashboard(board, task_id="ABCD-001")

        self.assertEqual(result["status"], "failed")
        self.assertFalse(dashboard_is_active(board))

    def test_runner_replaces_active_dashboard_with_old_protocol(self):
        board = self.root / "abc-tasks"
        self.state.enable_task_dashboard = True
        with (
            mock.patch("agent_bridge_connect.task_health.dashboard_is_active", return_value=True),
            mock.patch("agent_bridge_connect.task_health.dashboard_protocol_matches", return_value=False),
            mock.patch("agent_bridge_connect.task_health.stop_dashboard_process") as stop,
            mock.patch("agent_bridge_connect.task_health.register_dashboard_task") as register,
            mock.patch("agent_bridge_connect.task_health.request_dashboard_refresh"),
            mock.patch.object(
                self.state,
                "_open_task_list_dashboard",
                return_value={"status": "opened"},
            ) as opened,
        ):
            result = self.state._ensure_task_list_dashboard(board, task_id="ABCD-001")

        self.assertEqual(result["status"], "opened")
        stop.assert_called_once_with(board)
        register.assert_called_once_with(board, "ABCD-001", reset=True)
        opened.assert_called_once_with(board, task_id="ABCD-001")

    def test_runner_atomically_creates_and_dispatches_task(self):
        board = self.root / "abc-tasks"
        workspace = self.root / "workspace"
        workspace.mkdir()
        config_path = self.root / "config.toml"
        config_path.write_text(f'workspace_root = "{self.root}"\n', encoding="utf-8")
        dispatched = {
            "run_id": "runner-worker-atomic",
            "pid": 1234,
            "status": "running",
            "task_id": "T-001",
            "dispatch_status": "accepted",
            "monitor_status": "opened",
        }
        request = {
            "title": "Atomic task",
            "assignee": "hermes",
            "steps": [{"id": 1, "description": "run"}],
            "board_root": str(board),
            "customer_dir": True,
            "customer_path": str(workspace),
            "interval_s": 2,
            "monitor": True,
        }
        with mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False):
            with mock.patch.object(self.state, "dispatch_worker", return_value=dispatched) as dispatch:
                result = self.state.create_and_dispatch(request)

        self.assertRegex(result["task_id"], r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertEqual(result["assignee"], "hermes")
        task_code, iteration = result["task_id"].split("-")
        self.assertTrue((board / task_code / iteration / "task.json").exists())
        self.assertEqual(dispatch.call_args.args[0], result["task_id"])

    def test_runner_create_accepts_task_scoped_customer_path(self):
        board = self.root / "abc-tasks"
        config_path = self.root / "config.toml"
        config_path.write_text(f'workspace_root = "{self.root}"\n', encoding="utf-8")
        dispatched = {
            "run_id": "runner-worker-external-create",
            "pid": 1234,
            "status": "running",
            "task_id": "ABCD-001",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }
        with tempfile.TemporaryDirectory() as customer_temp:
            workspace = Path(customer_temp) / "customer-project"
            workspace.mkdir()
            request = {
                "title": "External atomic task",
                "assignee": "hermes",
                "steps": [{"id": 1, "description": "run"}],
                "board_root": str(board),
                "customer_dir": True,
                "customer_path": str(workspace),
                "interval_s": 2,
                "monitor": False,
            }
            with mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False):
                with mock.patch.object(self.state, "dispatch_worker", return_value=dispatched):
                    result = self.state.create_and_dispatch(request)

        self.assertRegex(result["task_id"], r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertEqual(result["workspace"]["project_root"], str(workspace.resolve()))

    def test_runner_create_normalizes_existing_customer_file_to_parent(self):
        board = self.root / "abc-tasks"
        config_path = self.root / "config.toml"
        config_path.write_text(f'workspace_root = "{self.root}"\n', encoding="utf-8")
        dispatched = {
            "run_id": "runner-worker-customer-file",
            "pid": 1234,
            "status": "running",
            "task_id": "ABCD-001",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }
        with tempfile.TemporaryDirectory() as customer_temp:
            customer = Path(customer_temp) / "documents"
            customer.mkdir()
            document = customer / "analysis.md"
            document.write_text("# Existing analysis\n", encoding="utf-8")
            request = {
                "title": "Update customer document",
                "assignee": "hermes",
                "steps": [{"id": 1, "description": f"Update {document}"}],
                "board_root": str(board),
                "customer_path": str(document),
                "interval_s": 2,
                "monitor": False,
            }
            with mock.patch.dict(os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False):
                with mock.patch.object(self.state, "dispatch_worker", return_value=dispatched):
                    result = self.state.create_and_dispatch(request)

            self.assertTrue(result["workspace"]["customer_dir"])
            self.assertEqual(result["workspace"]["project_root"], str(customer.resolve()))
            self.assertEqual(result["workspace"]["artifact_root"], str(customer.resolve()))
            managed = self.root / "tasks" / "artifacts" / result["workspace"]["task_date"] / result["workspace"]["task_code"]
            self.assertFalse(managed.exists())

    def test_runner_dispatches_existing_task_by_id(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        workspace = self.root / "workspace"
        workspace.mkdir()
        task = TaskService(board, config={"workspace_root": str(self.root)}).create_task(
            "Existing",
            "hermes",
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=workspace,
        )
        dispatched = {
            "run_id": "runner-worker-existing",
            "dispatch_status": "accepted",
            "monitor_status": "opened",
        }
        request = {"task_id": task.id, "board_root": str(board), "monitor": True}
        with mock.patch.object(self.state, "dispatch_worker", return_value=dispatched) as dispatch:
            result = self.state.dispatch_task(request)

        self.assertEqual(result["run_id"], "runner-worker-existing")
        self.assertEqual(dispatch.call_args.args[0:2], (task.id, "hermes"))

    def test_runner_agent_callback_is_staged_until_executor_exit(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        workspace = self.root / "workspace"
        workspace.mkdir()
        service = TaskService(board, config={"workspace_root": str(self.root)})
        task = service.create_task(
            "Callback task",
            "hermes",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=workspace,
        )
        service.start_task_run(task.id, "hermes")

        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=mock.Mock(ok=True, message="shown"),
        ):
            callback = completed_callback(task, summary="agent reported completion")
            result = self.state.agent_callback(
                {
                    "board_root": str(board),
                    "state": "completed",
                    **callback,
                }
            )

        self.assertEqual(result["task_id"], task.id)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["event_type"], "task.agent_callback_recorded")
        self.assertFalse(result["notified"])
        reloaded = TaskService(board).get_task(task.id)
        self.assertEqual(reloaded.status, "running")
        self.assertEqual(
            reloaded.extensions["agentbc.completion_intent"]["summary"],
            "agent reported completion",
        )
        events = TaskService(board).store.read_events(task.id)
        self.assertNotIn("notification_delivery", [event.get("event_type") for event in events])
        finalized = TaskService(board).finalize_task_from_executor_exit(
            task.id,
            executor_run_id="hermes-run-1",
            exit_code=0,
            callback=callback,
        )
        self.assertTrue(finalized)
        completed = TaskService(board).get_task(task.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            completed.extensions["agentbc.final_callback"]["summary"],
            "agent reported completion",
        )

    def test_executor_exit_ignores_agent_callback_path_overrides(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        workspace = self.root / "workspace"
        workspace.mkdir()
        service = TaskService(board, config={"workspace_root": str(self.root)})
        task = service.create_task(
            "Callback with stale paths",
            "hermes",
            [{"id": 1, "description": "finish"}],
            customer_dir=True,
            customer_path=workspace,
        )
        service.start_task_run(task.id, "hermes")

        finalized = service.finalize_task_from_executor_exit(
            task.id,
            executor_run_id="hermes-run-2",
            exit_code=0,
            callback=completed_callback(
                task,
                summary="Hermes finished",
                report_file="/tmp/stale-report.md",
                artifacts_dir="/tmp/stale-artifacts",
            ),
        )

        self.assertTrue(finalized)
        completed = service.get_task(task.id)
        self.assertEqual(completed.status, "completed")
        final_callback = completed.extensions["agentbc.final_callback"]
        self.assertEqual(final_callback["report_file"], task.workspace["report_file"])
        self.assertEqual(final_callback["artifacts_dir"], task.workspace["artifacts_dir"])

    def test_runner_recovery_callback_is_advisory_until_executor_exit(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        workspace = self.root / "workspace"
        workspace.mkdir()
        service = TaskService(board, config={"workspace_root": str(self.root)})
        task = service.create_task(
            "Callback recovery",
            "hermes",
            [{"id": 1, "description": "recover"}],
            customer_dir=True,
            customer_path=workspace,
        )
        service.start_task_run(task.id, "hermes")

        with mock.patch(
            "agent_bridge_connect.notifications.DialogNotifier.send",
            return_value=mock.Mock(ok=True, message="shown"),
        ):
            result = self.state.agent_callback(
                {
                    "task_id": task.id,
                    "board_root": str(board),
                    "state": "needs_recovery",
                    "summary": "agent reported blocker",
                    "recovery_code": "agent_blocked",
                }
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["event_type"], "task.agent_callback_recorded")
        reloaded = TaskService(board).get_task(task.id)
        self.assertEqual(reloaded.status, "running")
        self.assertEqual(
            reloaded.extensions["agentbc.completion_intent"]["declared_state"],
            "needs_recovery",
        )
        self.assertEqual(reloaded.errors, [])

    def test_runner_atomically_handoffs_and_dispatches_task(self):
        from agent_bridge_connect.service import TaskService

        board = self.root / "abc-tasks"
        workspace = self.root / "workspace"
        workspace.mkdir()
        service = TaskService(board, config={"workspace_root": str(self.root)})
        source = service.create_task(
            "Source",
            "hermes",
            [{"id": 1, "description": "source"}],
            customer_dir=True,
            customer_path=workspace,
        )
        service.claim_task(source.id, "hermes")
        service.execute_step(source.id, 1, {"status": "done"})
        from tests.contract_helpers import finalize_completed

        finalize_completed(service, source.id)
        source_report = Path(source.workspace["report_file"])
        source_report.chmod(0o400)
        dispatched = {
            "run_id": "runner-worker-handoff",
            "pid": 1234,
            "status": "running",
            "task_id": f"{source.workspace['task_code']}-002",
            "dispatch_status": "accepted",
            "monitor_status": "opened",
        }
        request = {
            "source_task_id": source.id,
            "target_assignee": "hermes",
            "message": "continue",
            "source_platform": "codex",
            "board_root": str(board),
            "interval_s": 2,
            "monitor": True,
        }
        try:
            with mock.patch.object(self.state, "dispatch_worker", return_value=dispatched) as dispatch:
                result = self.state.handoff_and_dispatch(request)
        finally:
            source_report.chmod(0o600)

        self.assertEqual(result["task_id"], f"{source.workspace['task_code']}-002")
        task_code, iteration = result["task_id"].split("-")
        self.assertTrue((board / task_code / iteration / "task.json").exists())
        self.assertEqual(dispatch.call_args.args[0], result["task_id"])
        followup = TaskService(board).get_task(result["task_id"])
        self.assertEqual(
            followup.extensions["agentbc.provenance"]["source_platform"],
            "codex",
        )

    def test_business_error_response_does_not_stop_runner_service(self):
        from agent_bridge_connect.runner import RunnerClient, RunnerError, RunnerService
        from agent_bridge_connect.service import TaskService

        board = self.root / "business-error-board"
        workspace = self.root / "business-error-workspace"
        workspace.mkdir()
        task_service = TaskService(board, config={"workspace_root": str(self.root)})
        source = task_service.create_task(
            "Failed source",
            "hermes",
            [{"id": 1, "description": "source"}],
            customer_dir=True,
            customer_path=workspace,
        )
        task_service.start_task_run(source.id, "hermes")
        task_service.mark_task_failed(source.id, "test_failure", "cannot hand off")

        spool = self.root / "business-error-spool"
        token = spool / "token"
        runner_service = RunnerService(spool, token, self.state, interval_s=0.01)
        thread = threading.Thread(target=runner_service.serve_forever, daemon=True)
        thread.start()
        try:
            client = RunnerClient(spool, token, timeout_s=2)
            with self.assertRaisesRegex(RunnerError, "handoff requires completed"):
                client.handoff_and_dispatch(
                    source.id,
                    "hermes",
                    "continue",
                    board,
                    None,
                    source_platform="codex",
                )
            self.assertEqual(client.health()["status"], "ready")
            self.assertTrue(thread.is_alive())
        finally:
            runner_service.shutdown()
            thread.join(timeout=2)

    def test_file_spool_client_service_round_trip(self):
        from agent_bridge_connect.runner import RunnerClient, RunnerService

        spool = self.root / "spool"
        token = spool / "token"
        service = RunnerService(spool, token, self.state, interval_s=0.01)
        thread = threading.Thread(
            target=service.serve_forever,
            daemon=True,
        )
        thread.start()
        try:
            client = RunnerClient(spool, token, timeout_s=2)
            health = client.health()
            self.assertEqual(health["status"], "ready")
            self.assertNotIn("allowed_roots", health)
            self.assertEqual(health["path_policy"]["agent_input"], "customer_path")
            storage = client.storage_status(
                [self.root, self.root / "tasks" / "report"]
            )
            self.assertEqual(storage["status"], "ready")
            self.assertEqual(len(storage["paths"]), 2)
            self.assertTrue(all(item["writable"] for item in storage["paths"]))
            submitted = client.submit(
                "hermes",
                [
                    str(self.fake_hermes),
                    "chat",
                    "-q",
                    "--max-turns",
                    "90",
                    "hello",
                ],
                self.root,
                task=self._authorized_task(),
            )
            terminal = self._wait_client_terminal(client, submitted["run_id"])
            self.assertEqual(terminal["stdout"], "RUNNER_OK")
            report = self.root / "tasks" / "report" / "2026-07-14" / "ABCD" / "ABCD-001-report.md"
            written = client.write_report(report, "# Through Runner\n")
            self.assertEqual(written["path"], str(report.resolve()))
            self.assertEqual(report.read_text(encoding="utf-8"), "# Through Runner\n")
        finally:
            service.shutdown()
            thread.join(timeout=2)

    def test_cli_exposes_runner_commands(self):
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        serve = parser.parse_args(
            ["runner", "serve", "--config", str(self.root / "config.toml"), "--allow-root", str(self.root)]
        )
        start = parser.parse_args(["runner", "start", "--config", str(self.root / "config.toml")])
        stop = parser.parse_args(["runner", "stop"])
        status = parser.parse_args(["runner", "status"])
        process_sample = parser.parse_args(["runner", "process-sample", "--pattern", "agentbc"])
        cancel = parser.parse_args(["runner", "cancel", "runner-hermes-test"])
        callback = parser.parse_args(
            ["task", "callback", "4XMC-001", "--state", "completed", "--summary", "done"]
        )
        self.assertEqual(serve.runner_command, "serve")
        self.assertEqual(serve.config, self.root / "config.toml")
        self.assertEqual(start.runner_command, "start")
        self.assertEqual(start.config, self.root / "config.toml")
        self.assertEqual(stop.runner_command, "stop")
        self.assertEqual(status.runner_command, "status")
        self.assertEqual(process_sample.runner_command, "process-sample")
        self.assertEqual(process_sample.pattern, ["agentbc"])
        self.assertEqual(cancel.run_id, "runner-hermes-test")
        self.assertEqual(callback.task_command, "callback")
        self.assertEqual(callback.state, "completed")
        self.assertEqual(callback.summary, "done")

    def test_background_runner_start_builds_detached_module_command(self):
        from agent_bridge_connect.runner import RunnerClient, RunnerError, start_runner_background

        process = mock.Mock(pid=4321, returncode=None)
        process.poll.return_value = None
        config = self.root / "config.toml"
        with mock.patch.object(
            RunnerClient,
            "health",
            side_effect=[RunnerError("missing token"), {"ok": True, "status": "ready", "pid": 4321}],
        ), mock.patch("agent_bridge_connect.runner.subprocess.Popen", return_value=process) as popen:
            result = start_runner_background(
                config_path=config,
                spool_root=self.root / "spool",
                state_root=self.root / "runner-state",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "started")
        command = popen.call_args.args[0]
        self.assertIn("agent_bridge_connect.cli", command)
        self.assertIn("serve", command)
        self.assertIn(str(config), command)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(result["log"], str(self.root / "runner-state" / "runner.log"))

    def test_background_runner_stop_rejects_unrelated_pid(self):
        from agent_bridge_connect.runner import RunnerClient, stop_runner_background

        pid_path = self.root / "spool" / "runner.pid"
        pid_path.parent.mkdir()
        pid_path.write_text("4321\n", encoding="utf-8")
        with mock.patch("agent_bridge_connect.runner._pid_is_alive", return_value=True), \
             mock.patch.object(
                 RunnerClient,
                 "health",
                 return_value={"ok": True, "status": "ready", "pid": 9999},
             ), \
             mock.patch("agent_bridge_connect.runner.os.kill") as kill:
            result = stop_runner_background(spool_root=pid_path.parent)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "pid_not_agentbc_runner")
        kill.assert_not_called()

    def test_runner_serve_refuses_when_existing_runner_is_ready(self):
        from agent_bridge_connect.cli import command_runner

        args = mock.Mock(
            runner_command="serve",
            spool=None,
            token=None,
            config=None,
            allow_root=[],
            state_root=None,
            hermes_command=None,
            codex_command=None,
        )
        with mock.patch(
            "agent_bridge_connect.cli._probe_existing_runner",
            return_value={"ok": True, "status": "ready", "pid": 12345},
        ):
            with mock.patch("agent_bridge_connect.runner.create_runner_service") as create:
                code = command_runner(args)

        self.assertEqual(code, 1)
        create.assert_not_called()

    def test_runner_roots_include_configured_workspace_without_cwd(self):
        from agent_bridge_connect.config import resolve_runner_allowed_roots

        workspace = self.root / "managed-workspace"
        extra = self.root / "explicit-projects"
        with mock.patch("pathlib.Path.cwd", return_value=self.root / "unrelated-cwd"):
            roots = resolve_runner_allowed_roots(
                {"workspace_root": str(workspace)},
                [extra, workspace],
            )

        self.assertEqual(roots, [workspace.resolve(), extra.resolve()])
        self.assertNotIn((self.root / "unrelated-cwd").resolve(), roots)

    def test_runner_prefers_configured_executor_command_over_path_discovery(self):
        from agent_bridge_connect.runner import create_runner_service

        configured = self.root / "configured-hermes"
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        service = create_runner_service(
            spool_root=self.root / "config-spool",
            token_path=self.root / "config-spool" / "token",
            state_root=self.root / "config-state",
            allowed_roots=[self.root],
            config={"executors": {"hermes": {"command": str(configured)}}},
        )

        self.assertEqual(service.runner_state.allowed_executables["hermes"], configured.resolve())
        self.assertEqual(service.runner_state.executable_sources["hermes"], "config")

    def test_runner_allows_configured_claude_executor(self):
        from agent_bridge_connect.runner import create_runner_service

        configured = self.root / "configured-claude"
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        service = create_runner_service(
            spool_root=self.root / "claude-spool",
            token_path=self.root / "claude-spool" / "token",
            state_root=self.root / "claude-state",
            allowed_roots=[self.root],
            config={"executors": {"claude": {"command": str(configured)}}},
        )

        self.assertEqual(service.runner_state.allowed_executables["claude"], configured.resolve())
        self.assertEqual(service.runner_state.executable_sources["claude"], "config")

    def test_runner_falls_back_when_configured_codex_path_is_missing(self):
        from agent_bridge_connect.runner import create_runner_service

        replacement = self.root / "chatgpt-codex"
        replacement.write_text("#!/bin/sh\n", encoding="utf-8")
        stale = self.root / "missing-codex"

        def discover(name, extra_paths=None):
            if name == "codex":
                self.assertEqual(extra_paths, [str(stale)])
                return {"found": True, "path": str(replacement), "source": "chatgpt_desktop"}
            return {"found": False}

        with mock.patch("agent_bridge_connect.runner.find_binary", side_effect=discover):
            service = create_runner_service(
                spool_root=self.root / "fallback-spool",
                token_path=self.root / "fallback-spool" / "token",
                state_root=self.root / "fallback-state",
                allowed_roots=[self.root],
                config={"executors": {"codex": {"command": str(stale)}}},
            )

        self.assertEqual(service.runner_state.allowed_executables["codex"], replacement.resolve())
        self.assertEqual(service.runner_state.executable_sources["codex"], "chatgpt_desktop")

    def test_runner_rejects_stale_executor_config_before_task_creation(self):
        configured = self.root / "different-hermes"
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        self.state.executable_sources["hermes"] = "config"
        board = self.root / "stale-board"
        workspace = self.root / "stale-workspace"
        workspace.mkdir()
        request = {
            "title": "Must not be created",
            "assignee": "hermes",
            "steps": [{"id": 1, "description": "run"}],
            "board_root": str(board),
            "workspace_root": str(workspace),
        }
        with mock.patch(
            "agent_bridge_connect.config.load_config",
            return_value={"executors": {"hermes": {"command": str(configured)}}},
        ):
            from agent_bridge_connect.runner import RunnerError

            with self.assertRaisesRegex(RunnerError, "runner_config_stale"):
                self.state.create_and_dispatch(request)

        self.assertFalse((board / "tasks").exists())

    def _wait_terminal(self, run_id: str):
        for _ in range(300):
            status = self.state.status(run_id)
            if status["status"] in {"completed", "failed", "cancelled"}:
                return status
            time.sleep(0.01)
        self.fail(f"runner did not finish: {run_id}")

    def _wait_client_terminal(self, client, run_id: str):
        for _ in range(100):
            status = client.status(run_id)
            if status["status"] in {"completed", "failed", "cancelled"}:
                return status
            time.sleep(0.01)
        self.fail(f"runner client did not finish: {run_id}")


if __name__ == "__main__":
    unittest.main()
