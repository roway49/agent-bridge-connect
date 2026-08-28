"""PERM-104-002: Runner-owned Seatbelt containment tests.

Covers linked-worktree realpath validation (bare/submodule/detached/topology
drift/symlink/branch drift fail closed), the task-scoped SBPL profile
(writable roots, pinned Git metadata, main-repo deny, inner-sandbox
exclusions), the sandbox-exec launcher, and the host-containment preflight
that returns ``host_containment_unliftable`` without dialogs or grant
consumption.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.permission_runtime import (
    HOST_CONTAINMENT_UNLIFTABLE,
    LINKED_WORKTREE_CAPABILITY_INVALID,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.seatbelt import (
    SEATBELT_EXECUTABLE,
    build_seatbelt_profile,
    canonical_task_roots,
    launch_with_seatbelt,
    linked_worktree_git_metadata_dirs,
    preflight_host_containment,
    seatbelt_available,
    validate_linked_worktree,
)
from agent_bridge_connect.permission_transport import (
    assert_git_metadata_not_in_add_dir,
    assert_inner_sandbox_within_outer,
)


def _git_ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _git_fail() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: not a git repository")


class SeatbeltAvailabilityTests(unittest.TestCase):
    @mock.patch("agent_bridge_connect.seatbelt.sys.platform", "darwin")
    @mock.patch("agent_bridge_connect.seatbelt.shutil.which", return_value="/usr/bin/sandbox-exec")
    def test_available_on_darwin_with_binary(self, _which: mock.Mock) -> None:
        self.assertTrue(seatbelt_available())

    @mock.patch("agent_bridge_connect.seatbelt.sys.platform", "darwin")
    @mock.patch("agent_bridge_connect.seatbelt.shutil.which", return_value=None)
    def test_unavailable_without_binary(self, _which: mock.Mock) -> None:
        self.assertFalse(seatbelt_available())

    @mock.patch("agent_bridge_connect.seatbelt.sys.platform", "linux")
    def test_unavailable_off_darwin(self) -> None:
        self.assertFalse(seatbelt_available())

    @mock.patch("agent_bridge_connect.seatbelt.seatbelt_available", return_value=False)
    def test_preflight_fails_closed_with_stable_code(self, _available: mock.Mock) -> None:
        with self.assertRaises(ABCError) as raised:
            preflight_host_containment(require_expansion=True)
        self.assertEqual(raised.exception.code, HOST_CONTAINMENT_UNLIFTABLE)

    @mock.patch("agent_bridge_connect.seatbelt.seatbelt_available", return_value=True)
    def test_preflight_passes_when_expansion_possible(self, _available: mock.Mock) -> None:
        preflight_host_containment(require_expansion=True)

    def test_preflight_without_expansion_never_fails(self) -> None:
        preflight_host_containment(require_expansion=False)


class LinkedWorktreeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "worktrees" / "agent"
        self.project.mkdir(parents=True)
        self.common = self.root / "repo" / ".git"
        self.git_dir = self.common / "worktrees" / "agent"
        self.git_dir.mkdir(parents=True)

    def _run_git(self, command: list[str], **kwargs: object):
        if command[-1] == "--absolute-git-dir":
            return _git_ok(str(self.git_dir) + "\n")
        if command[-1] == "--git-common-dir":
            return _git_ok(str(self.common) + "\n")
        if command[-1] == "--is-bare-repository":
            return _git_ok("false\n")
        if command[-1] == "--show-superproject-working-tree":
            return _git_ok("\n")
        if command[-1] == "HEAD":
            return _git_ok("refs/heads/agent/hermes\n")
        return _git_fail()

    def test_linked_worktree_pins_exact_metadata(self) -> None:
        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run",
            side_effect=self._run_git,
        ):
            capability = validate_linked_worktree(self.project)
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability["kind"], "linked")
        self.assertEqual(capability["symbolic_ref"], "refs/heads/agent/hermes")
        self.assertEqual(
            capability["ref_path"], self.common / "refs" / "heads" / "agent" / "hermes"
        )
        self.assertEqual(
            capability["reflog_path"],
            self.common / "logs" / "refs" / "heads" / "agent" / "hermes",
        )
        self.assertEqual(
            capability["objects_dir"], self.common / "objects"
        )
        self.assertEqual(
            linked_worktree_git_metadata_dirs(capability),
            [str(self.git_dir), str(self.common)],
        )

    def test_expected_ref_mismatch_is_branch_drift(self) -> None:
        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run",
            side_effect=self._run_git,
        ):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(
                    self.project, expected_ref="refs/heads/main"
                )
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_detached_head_fails_closed(self) -> None:
        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "HEAD":
                return _git_fail()
            return self._run_git(command, **kwargs)

        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git
        ):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_bare_repository_fails_closed(self) -> None:
        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "--is-bare-repository":
                return _git_ok("true\n")
            return self._run_git(command, **kwargs)

        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git
        ):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_submodule_gitdir_fails_closed(self) -> None:
        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "--show-superproject-working-tree":
                return _git_ok(str(self.root / "super") + "\n")
            return self._run_git(command, **kwargs)

        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git
        ):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_topology_drift_fails_closed(self) -> None:
        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "--absolute-git-dir":
                return _git_ok(str(self.root / "elsewhere" / ".git") + "\n")
            return self._run_git(command, **kwargs)

        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git
        ):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_plain_directory_and_main_repo_add_no_capability(self) -> None:
        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run",
            return_value=_git_fail(),
        ):
            self.assertIsNone(validate_linked_worktree(self.project))
        main = self.root / "main-repo"
        main.mkdir()
        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run",
            side_effect=lambda command, **kwargs: _git_ok(
                str(main / ".git") + "\n"
            )
            if command[-1] in ("--absolute-git-dir", "--git-common-dir")
            else self._run_git(command, **kwargs),
        ):
            self.assertIsNone(validate_linked_worktree(main))

    def test_symlinked_git_metadata_fails_closed(self) -> None:
        # A symlink inside the git common dir (e.g. refs -> elsewhere) is
        # topology drift: the pinned ref paths must never traverse symlinks.
        refs_target = self.root / "refs-elsewhere"
        refs_target.mkdir()
        refs_link = self.common / "refs"
        try:
            refs_link.symlink_to(refs_target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.addCleanup(lambda: refs_link.unlink(missing_ok=True))
        with mock.patch(
            "agent_bridge_connect.seatbelt.subprocess.run",
            side_effect=self._run_git,
        ):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)


class SeatbeltProfileTests(unittest.TestCase):
    def _linked(self) -> dict:
        return {
            "kind": "linked",
            "git_dir": Path("/repo/.git/worktrees/agent"),
            "common_dir": Path("/repo/.git"),
            "symbolic_ref": "refs/heads/agent/hermes",
            "ref_path": Path("/repo/.git/refs/heads/agent/hermes"),
            "lock_path": Path("/repo/.git/refs/heads/agent/hermes.lock"),
            "reflog_path": Path("/repo/.git/logs/refs/heads/agent/hermes"),
            "objects_dir": Path("/repo/.git/objects"),
        }

    def test_profile_denies_default_and_pins_task_roots(self) -> None:
        project = str(Path("/tmp/task-project").resolve())
        artifacts = str(Path("/tmp/task-artifacts").resolve())
        profile = build_seatbelt_profile(
            writable_roots=[project, artifacts],
        )
        self.assertTrue(profile.startswith("(version 1)\n(deny default)\n"))
        self.assertIn(f'(allow file-write* (subpath "{project}"))', profile)
        self.assertIn(f'(allow file-write* (subpath "{artifacts}"))', profile)
        self.assertIn("(allow file-read*)", profile)
        self.assertNotIn("refs/heads", profile)

    def test_linked_worktree_profile_pins_only_current_branch(self) -> None:
        profile = build_seatbelt_profile(
            writable_roots=["/tmp/task-project"],
            linked_worktree=self._linked(),
        )
        self.assertIn(
            '(allow file-write* (subpath "/repo/.git/worktrees/agent"))', profile
        )
        self.assertIn(
            '(allow file-write* (subpath "/repo/.git/objects"))', profile
        )
        # The whole main repository working tree is denied.
        self.assertIn('(deny file-write* (subpath "/repo"))', profile)
        # Everything else Git-owned is denied.
        self.assertIn('(deny file-write* (subpath "/repo/.git/refs"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/worktrees"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/config"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/hooks"))', profile)
        self.assertIn(
            '(deny file-write* (literal "/repo/.git/packed-refs"))', profile
        )
        # Exact current-branch ref, lock and reflog are the only writes allowed.
        self.assertIn(
            '(allow file-write* (literal "/repo/.git/refs/heads/agent/hermes"))',
            profile,
        )
        self.assertIn(
            '(allow file-write* (literal "/repo/.git/refs/heads/agent/hermes.lock"))',
            profile,
        )
        self.assertIn(
            '(allow file-write* (literal "/repo/.git/logs/refs/heads/agent/hermes"))',
            profile,
        )
        # Other refs stay denied because the refs deny precedes the exact allow.
        self.assertLess(
            profile.index('(deny file-write* (subpath "/repo/.git/refs"))'),
            profile.index(
                '(allow file-write* (literal "/repo/.git/refs/heads/agent/hermes"))'
            ),
        )

    def test_profile_requires_writable_root(self) -> None:
        with self.assertRaises(ABCError) as raised:
            build_seatbelt_profile(writable_roots=[])
        self.assertEqual(raised.exception.code, HOST_CONTAINMENT_UNLIFTABLE)

    def test_git_metadata_never_enters_inner_sandbox(self) -> None:
        metadata = ["/repo/.git", "/repo/.git/worktrees/agent"]
        assert_git_metadata_not_in_add_dir(["/tmp/artifact"], metadata)
        with self.assertRaises(ABCError) as raised:
            assert_git_metadata_not_in_add_dir(["/repo/.git/worktrees/agent"], metadata)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_inner_sandbox_stays_within_outer_roots(self) -> None:
        assert_inner_sandbox_within_outer(
            ["/tmp/task-project/out"], ["/tmp/task-project"]
        )
        with self.assertRaises(ABCError) as raised:
            assert_inner_sandbox_within_outer(
                ["/tmp/elsewhere"], ["/tmp/task-project"]
            )
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_launch_wraps_command_with_0600_profile(self) -> None:
        directory = Path(tempfile.mkdtemp())
        wrapped, profile_path = launch_with_seatbelt(
            ["python", "-m", "worker"],
            directory,
            "(version 1)\n(deny default)\n",
            directory,
        )
        self.assertEqual(
            wrapped,
            [SEATBELT_EXECUTABLE, "-f", str(profile_path), "python", "-m", "worker"],
        )
        self.assertTrue(profile_path.exists())
        self.assertEqual(profile_path.read_text(encoding="utf-8"), "(version 1)\n(deny default)\n")
        self.assertEqual(oct(profile_path.stat().st_mode & 0o777), "0o600")


class CanonicalTaskRootsTests(unittest.TestCase):
    def test_roots_are_realpath_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "project"
            project.mkdir()
            workspace = {
                "customer_dir": True,
                "customer_path": str(project),
                "project_root": str(project),
                "artifact_root": str(project / "artifacts"),
                "report_root": str(base / "report"),
                "agentbc_root": str(base / "workspace"),
            }
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(project), texts)
            self.assertIn(str(project / "artifacts"), texts)
            self.assertIn(str(base / "workspace"), texts)
            self.assertEqual(len(roots), len(texts))

    def test_empty_workspace_yields_no_roots(self) -> None:
        self.assertEqual(canonical_task_roots(None), [])
        self.assertEqual(canonical_task_roots({}), [])


if __name__ == "__main__":
    unittest.main()
