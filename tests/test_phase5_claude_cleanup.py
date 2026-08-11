from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.adapters import SessionCleanupRequest
from agent_bridge_connect.executors.claude import (
    CLAUDE_PROJECT_PURGE_TIMEOUT_S,
    ClaudeExecutor,
)
from agent_bridge_connect.path_model import build_path_plan


HELP_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "executor_runtime"
    / "claude_2.1.226_help.txt"
)
SESSION_ID = "12345678-1234-5678-9234-567812345678"


class FakeClaudeCLI:
    def __init__(self, help_text: str) -> None:
        self.help_completed = subprocess.CompletedProcess(
            [], 0, stdout=help_text, stderr=""
        )
        self.purge_completed = subprocess.CompletedProcess(
            [], 0, stdout="Purged project state", stderr=""
        )
        self.purge_exception: Exception | None = None
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((list(command), dict(kwargs)))
        if command[-1] == "--help":
            return self.help_completed
        if self.purge_exception is not None:
            raise self.purge_exception
        return self.purge_completed


class ClaudeManagedCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agentbc_root = self.root / "workspace"
        self.plan = build_path_plan(
            customer_dir=False,
            customer_path=None,
            task_code="C5NP",
            iteration=1,
            config={"workspace_root": str(self.agentbc_root)},
            task_date="2026-08-11",
        )
        self.workspace = self.plan.to_workspace()
        self.project = self.plan.executor_project_root
        self.project.mkdir(parents=True)
        self.help_text = HELP_FIXTURE.read_text(encoding="utf-8")
        self.fake = FakeClaudeCLI(self.help_text)
        self.executor = ClaudeExecutor(
            command=str(self.root / "fake-claude"),
            transport="direct",
        )

    def _request(self, **overrides: object) -> SessionCleanupRequest:
        values: dict[str, object] = {
            "executor": "claude",
            "session_id": SESSION_ID,
            "task_id": "C5NP-001",
            "retain": False,
            "project_mode": "ephemeral",
            "strategy": "claude_project_purge",
            "project_path": str(self.project),
            "workspace": self.workspace,
        }
        values.update(overrides)
        return SessionCleanupRequest(**values)

    def test_frozen_help_fixture_enables_exact_cleanup_capability(self) -> None:
        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.run",
            side_effect=self.fake,
        ):
            capability = self.executor.session_cleanup_capability(self._request())

        self.assertEqual(capability.capability, "supported")
        self.assertEqual(capability.strategy, "claude_project_purge")
        command, kwargs = self.fake.calls[0]
        self.assertEqual(
            command,
            [str(self.executor.agent_bin), "project", "purge", "--help"],
        )
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], CLAUDE_PROJECT_PURGE_TIMEOUT_S)

    def test_incomplete_or_nonzero_help_is_unsupported(self) -> None:
        cases = (
            subprocess.CompletedProcess([], 0, stdout="Usage: claude", stderr=""),
            subprocess.CompletedProcess([], 2, stdout=self.help_text, stderr="bad help"),
        )
        for completed in cases:
            with self.subTest(returncode=completed.returncode):
                fake = FakeClaudeCLI(self.help_text)
                fake.help_completed = completed
                with mock.patch(
                    "agent_bridge_connect.executors.claude.subprocess.run",
                    side_effect=fake,
                ):
                    capability = self.executor.session_cleanup_capability(
                        self._request()
                    )
                self.assertEqual(capability.capability, "unsupported")
                self.assertEqual(
                    capability.error_code,
                    "claude_project_purge_unsupported",
                )

    def test_retain_or_native_never_probes_or_purges(self) -> None:
        requests = (
            self._request(retain=True),
            self._request(project_mode="native"),
        )
        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.run"
        ) as run:
            for request in requests:
                with self.subTest(
                    retain=request.retain,
                    project_mode=request.project_mode,
                ):
                    capability = self.executor.session_cleanup_capability(request)
                    result = self.executor.cleanup_session(request)
                    self.assertEqual(capability.capability, "not_applicable")
                    self.assertEqual(result.state, "retained")
                    self.assertEqual(result.capability, "not_applicable")
                    self.assertEqual(result.strategy, "retain")
        run.assert_not_called()
        self.assertTrue(self.project.is_dir())

    def test_invalid_session_binding_or_project_mode_never_probes(self) -> None:
        requests = (
            self._request(session_id="not-a-claude-session"),
            self._request(project_mode="none"),
        )
        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.run"
        ) as run:
            for request in requests:
                with self.subTest(
                    session_id=request.session_id,
                    project_mode=request.project_mode,
                ):
                    result = self.executor.cleanup_session(request)
                    self.assertEqual(result.state, "unsupported")
        run.assert_not_called()

    def test_ephemeral_cleanup_uses_canonical_argv_and_rmdir_order(self) -> None:
        real_rmdir = os.rmdir
        with (
            mock.patch(
                "agent_bridge_connect.executors.claude.subprocess.run",
                side_effect=self.fake,
            ),
            mock.patch(
                "agent_bridge_connect.executors.claude.os.rmdir",
                wraps=real_rmdir,
            ) as rmdir,
        ):
            result = self.executor.cleanup_session(self._request())

        self.assertEqual(result.state, "succeeded")
        purge_command, purge_kwargs = self.fake.calls[1]
        self.assertEqual(
            purge_command,
            [
                str(self.executor.agent_bin),
                "project",
                "purge",
                "--yes",
                str(self.project),
            ],
        )
        self.assertNotIn("--all", purge_command)
        self.assertIs(purge_kwargs["shell"], False)
        self.assertEqual(purge_kwargs["timeout"], 30)
        self.assertEqual(
            [item.args[0] for item in rmdir.call_args_list],
            [self.project, self.project.parent, self.project.parent.parent],
        )
        self.assertFalse(self.project.parent.parent.exists())

    def test_purge_timeout_and_nonzero_exit_stop_before_rmdir(self) -> None:
        cases = (
            (
                subprocess.TimeoutExpired(cmd=["fake-claude"], timeout=30),
                None,
                "claude_project_purge_timeout",
                True,
            ),
            (
                None,
                subprocess.CompletedProcess([], 7, stdout="", stderr="purge failed"),
                "claude_project_purge_failed",
                False,
            ),
        )
        for exception, completed, error_code, retryable in cases:
            with self.subTest(error_code=error_code):
                fake = FakeClaudeCLI(self.help_text)
                fake.purge_exception = exception
                if completed is not None:
                    fake.purge_completed = completed
                with (
                    mock.patch(
                        "agent_bridge_connect.executors.claude.subprocess.run",
                        side_effect=fake,
                    ),
                    mock.patch(
                        "agent_bridge_connect.executors.claude.os.rmdir"
                    ) as rmdir,
                ):
                    result = self.executor.cleanup_session(self._request())
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.retryable, retryable)
                rmdir.assert_not_called()

    def test_explicit_project_absence_supports_idempotent_repeat(self) -> None:
        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.run",
            side_effect=self.fake,
        ):
            first = self.executor.cleanup_session(self._request())
            self.fake.purge_completed = subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="No Claude Code project state found for project: exact path",
            )
            second = self.executor.cleanup_session(self._request())

        self.assertEqual(first.state, "succeeded")
        self.assertEqual(second.state, "succeeded")
        purge_calls = [call for call in self.fake.calls if "--yes" in call[0]]
        self.assertEqual(len(purge_calls), 2)

    def test_symlink_path_is_rejected_without_purge(self) -> None:
        self.project.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.project.symlink_to(outside, target_is_directory=True)

        with mock.patch(
            "agent_bridge_connect.executors.claude.subprocess.run",
            side_effect=self.fake,
        ):
            result = self.executor.cleanup_session(self._request())

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "cleanup_path_symlink")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertTrue(outside.is_dir())

    def test_tampered_project_path_and_wrong_task_id_are_rejected(self) -> None:
        tampered_workspace = copy.deepcopy(self.workspace)
        tampered_path = str(self.root / "outside" / "claude")
        tampered_workspace["executor_project_root"] = tampered_path
        requests = (
            (
                self._request(project_path=str(self.root / "outside" / "claude")),
                "cleanup_project_mismatch",
            ),
            (
                self._request(
                    project_path=tampered_path,
                    workspace=tampered_workspace,
                ),
                "cleanup_project_mismatch",
            ),
            (
                self._request(project_path=f"{self.project}/"),
                "cleanup_project_mismatch",
            ),
            (self._request(task_id="C5NP-002"), "cleanup_task_mismatch"),
        )
        for request, expected in requests:
            with self.subTest(expected=expected):
                fake = FakeClaudeCLI(self.help_text)
                with mock.patch(
                    "agent_bridge_connect.executors.claude.subprocess.run",
                    side_effect=fake,
                ):
                    result = self.executor.cleanup_session(request)
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.error_code, expected)
                self.assertEqual(len(fake.calls), 1)

    def test_nonempty_directory_stops_layered_cleanup(self) -> None:
        unexpected = self.project / "unexpected.txt"
        unexpected.write_text("preserve", encoding="utf-8")
        real_rmdir = os.rmdir
        with (
            mock.patch(
                "agent_bridge_connect.executors.claude.subprocess.run",
                side_effect=self.fake,
            ),
            mock.patch(
                "agent_bridge_connect.executors.claude.os.rmdir",
                wraps=real_rmdir,
            ) as rmdir,
        ):
            result = self.executor.cleanup_session(self._request())

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "claude_cleanup_directory_not_empty")
        self.assertEqual(rmdir.call_count, 1)
        self.assertEqual(unexpected.read_text(encoding="utf-8"), "preserve")

    def test_path_plan_is_revalidated_before_every_rmdir(self) -> None:
        workspace = copy.deepcopy(self.workspace)
        request = self._request(workspace=workspace)
        real_rmdir = os.rmdir
        rmdir_calls: list[Path] = []

        def drift_after_first_rmdir(path: Path) -> None:
            rmdir_calls.append(path)
            real_rmdir(path)
            workspace["iteration"] = "002"

        with (
            mock.patch(
                "agent_bridge_connect.executors.claude.subprocess.run",
                side_effect=self.fake,
            ),
            mock.patch(
                "agent_bridge_connect.executors.claude.os.rmdir",
                side_effect=drift_after_first_rmdir,
            ),
        ):
            result = self.executor.cleanup_session(request)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "cleanup_task_mismatch")
        self.assertEqual(rmdir_calls, [self.project])
        self.assertTrue(self.project.parent.is_dir())

    def test_cleanup_mode_is_revalidated_before_every_rmdir(self) -> None:
        request = self._request()
        real_rmdir = os.rmdir
        rmdir_calls: list[Path] = []

        def change_mode_after_first_rmdir(path: Path) -> None:
            rmdir_calls.append(path)
            real_rmdir(path)
            object.__setattr__(request, "project_mode", "native")

        with (
            mock.patch(
                "agent_bridge_connect.executors.claude.subprocess.run",
                side_effect=self.fake,
            ),
            mock.patch(
                "agent_bridge_connect.executors.claude.os.rmdir",
                side_effect=change_mode_after_first_rmdir,
            ),
        ):
            result = self.executor.cleanup_session(request)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "claude_cleanup_mode_invalid")
        self.assertEqual(rmdir_calls, [self.project])
        self.assertTrue(self.project.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
