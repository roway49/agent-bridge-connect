from io import StringIO
from pathlib import Path
import contextlib
import tempfile
import unittest
from unittest import mock

from agent_bridge_connect.cli import _expand_shorthand, build_parser, main


class CliTests(unittest.TestCase):
    def test_public_help_lists_only_v2_command_groups(self) -> None:
        help_text = build_parser().format_help()

        for command in (
            "setup",
            "doctor",
            "uninstall",
            "init",
            "claude",
            "hermes",
            "session",
            "task",
            "worker",
            "runner",
        ):
            self.assertIn(command, help_text)
        for legacy_command in (
            "submit",
            "watch",
            "notify",
            "_shorthand",
        ):
            self.assertNotIn(legacy_command, help_text)

    def test_task_code_shorthand_routes_to_status(self) -> None:
        self.assertEqual(_expand_shorthand(["4XMC"]), ["task", "status", "4XMC"])
        self.assertEqual(
            _expand_shorthand(["4XMC-001", "--json"]),
            ["task", "status", "4XMC-001", "--json"],
        )

    def test_init_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["init", "--root", str(Path(tmp) / "runtime")])

            self.assertEqual(code, 0)
            self.assertIn("initialized:", out.getvalue())

    def test_uninstall_command_routes_explicit_data_choices(self) -> None:
        with mock.patch("agent_bridge_connect.setup.run_uninstall") as uninstall:
            uninstall.return_value = {"ok": True, "mode": "uninstall"}
            code = main(["uninstall", "--remove-records", "--keep-artifacts"])

        self.assertEqual(code, 0)
        uninstall.assert_called_once_with(
            interactive=True,
            remove_records=True,
            remove_artifacts=False,
        )

    def test_setup_starts_runner_after_writing_config(self) -> None:
        setup_result = {
            "ok": True,
            "mode": "setup",
            "config_path": "/tmp/agentbc-config.toml",
        }
        runner_result = {"ok": True, "status": "started", "pid": 123}
        with mock.patch("agent_bridge_connect.setup.run_setup", return_value=setup_result), \
             mock.patch(
                 "agent_bridge_connect.runner.start_runner_background",
                 return_value=runner_result,
             ) as start:
            out = StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["setup", "--non-interactive"])

        self.assertEqual(code, 0)
        start.assert_called_once_with(config_path="/tmp/agentbc-config.toml")
        self.assertIn('"status": "started"', out.getvalue())

    def test_desktop_archive_ack_accepts_runner_status_success(self) -> None:
        result = {
            "ok": True,
            "task_id": "VE9X-001",
            "session_id": "01a081be-3e15-7f93-9b1c-2fb032b6c279",
            "status": "succeeded",
        }
        with mock.patch(
            "agent_bridge_connect.runner.RunnerClient.acknowledge_desktop_archive",
            return_value=result,
        ):
            with contextlib.redirect_stdout(StringIO()):
                code = main(
                    [
                        "session",
                        "acknowledge-desktop-archive",
                        "VE9X-001",
                        "--session-id",
                        "01a081be-3e15-7f93-9b1c-2fb032b6c279",
                    ]
                )

        self.assertEqual(code, 0)

    def test_desktop_archive_ack_rejects_nonterminal_status(self) -> None:
        result = {
            "ok": True,
            "task_id": "VE9X-001",
            "session_id": "01a081be-3e15-7f93-9b1c-2fb032b6c279",
            "status": "waiting_for_desktop",
        }
        with mock.patch(
            "agent_bridge_connect.runner.RunnerClient.acknowledge_desktop_archive",
            return_value=result,
        ):
            with contextlib.redirect_stdout(StringIO()):
                code = main(
                    [
                        "session",
                        "acknowledge-desktop-archive",
                        "VE9X-001",
                        "--session-id",
                        "01a081be-3e15-7f93-9b1c-2fb032b6c279",
                    ]
                )

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
