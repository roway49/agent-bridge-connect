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
    canonical_task_files,
    canonical_task_roots,
    cleanup_seatbelt_profiles,
    cleanup_stale_seatbelt_profiles,
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

    def test_cleanup_removes_stale_profiles_after_crash(self) -> None:
        # Runner crash/restart: stale task-scoped profiles must not survive.
        directory = Path(tempfile.mkdtemp())
        stale: list[Path] = []
        for _ in range(3):
            _, profile_path = launch_with_seatbelt(
                ["python", "-m", "worker"],
                directory,
                "(version 1)\n",
                directory,
            )
            stale.append(profile_path)
        # Unrelated files stay untouched by the sweep.
        unrelated = directory / "unrelated.txt"
        unrelated.write_text("keep me", encoding="utf-8")
        removed = cleanup_seatbelt_profiles(directory)
        self.assertEqual(removed, 3)
        for profile in stale:
            self.assertFalse(profile.exists())
        self.assertTrue(unrelated.exists())
        # Sweeping an empty/missing directory is a no-op.
        self.assertEqual(cleanup_seatbelt_profiles(directory), 0)
        self.assertEqual(cleanup_seatbelt_profiles(directory / "missing"), 0)


class StaleProfileSweepTests(unittest.TestCase):
    """E52M-003 review fix: the stale sweep must run BEFORE the new profile
    is created and must never delete an active profile - including the
    profile of the launch in progress and of concurrently running workers."""

    def setUp(self) -> None:
        if not seatbelt_available():
            self.skipTest("sandbox-exec unavailable")

    def test_sweep_before_launch_preserves_active_profiles(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: cleanup_seatbelt_profiles(directory))
        # Simulate a concurrently active worker's profile.
        _, active_profile = launch_with_seatbelt(
            ["true"], directory, "(version 1)\n", directory
        )
        removed = cleanup_stale_seatbelt_profiles(
            directory, keep=[str(active_profile)]
        )
        self.assertEqual(removed, [])
        self.assertTrue(active_profile.exists())

    def test_sweep_removes_only_stale_and_keeps_named(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: cleanup_seatbelt_profiles(directory))
        _, stale = launch_with_seatbelt(
            ["true"], directory, "(version 1)\n", directory
        )
        _, keep_me = launch_with_seatbelt(
            ["true"], directory, "(version 1)\n", directory
        )
        removed = cleanup_stale_seatbelt_profiles(
            directory, keep=[str(keep_me)]
        )
        self.assertEqual([Path(item).name for item in removed], [stale.name])
        self.assertFalse(stale.exists())
        self.assertTrue(keep_me.exists())

    def test_launch_then_sweep_never_deletes_the_new_profile(self) -> None:
        # The E52M-003 bug shape: create -> sweep-all deleted the profile
        # the launch had just written, so sandbox-exec could not start.
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: cleanup_seatbelt_profiles(directory))
        profile_text = build_seatbelt_profile(
            writable_roots=[str(directory)],
        )
        _, new_profile = launch_with_seatbelt(
            ["true"], directory, profile_text, directory
        )
        # Correct order: sweep first (nothing stale), keep-list includes the
        # new profile once registered.
        removed_before = cleanup_stale_seatbelt_profiles(
            directory, keep=[str(new_profile)]
        )
        self.assertEqual(removed_before, [])
        self.assertTrue(new_profile.exists())

    def _real_contained_run(self, profile_text: str, script: str, cwd: Path):
        wrapped, profile_path = launch_with_seatbelt(
            ["/bin/sh", "-c", script], cwd, profile_text, cwd
        )
        process = subprocess.Popen(
            wrapped,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=30)
        return process.returncode, stdout, stderr, profile_path

    def test_real_popen_plain_repo_containment_allows_task_root_denies_oob(self) -> None:
        # Fix 2 + Fix 6: a plain repository's concrete full worker enters the
        # task-scoped profile; writes inside the task root succeed and an
        # out-of-bounds write into /private/tmp fails.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            task_root = base / "task"
            task_root.mkdir()
            profile_text = build_seatbelt_profile(
                writable_roots=[str(task_root), str(base / "state")],
            )
            # Inside the task root: allowed.
            code, stdout, stderr, profile = self._real_contained_run(
                profile_text,
                "printf contained > in-task.txt && cat in-task.txt",
                task_root,
            )
            self.addCleanup(lambda: profile.unlink(missing_ok=True))
            self.assertEqual(code, 0, stderr)
            self.assertIn("contained", stdout)
            # Out-of-bounds write into the world-staging tree: denied.
            code, _stdout, stderr, profile = self._real_contained_run(
                profile_text,
                "printf oops > /private/tmp/agentbc-oob-$$ && exit 0",
                task_root,
            )
            self.addCleanup(lambda: profile.unlink(missing_ok=True))
            self.assertNotEqual(code, 0)
            self.assertIn("operation not permitted", stderr.lower())

    def test_real_popen_allows_exact_report_file_but_denies_sibling_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            task_root = base / "task"
            reports = base / "reports" / "E52M"
            task_root.mkdir()
            reports.mkdir(parents=True)
            current = reports / "E52M-003-report.md"
            sibling = reports / "E52M-002-report.md"
            profile_text = build_seatbelt_profile(
                writable_roots=[task_root],
                writable_files=[current],
            )
            code, _stdout, stderr, profile = self._real_contained_run(
                profile_text,
                f"printf current > {current}",
                task_root,
            )
            self.addCleanup(lambda: profile.unlink(missing_ok=True))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(current.read_text(encoding="utf-8"), "current")
            code, _stdout, stderr, profile = self._real_contained_run(
                profile_text,
                f"printf sibling > {sibling}",
                task_root,
            )
            self.addCleanup(lambda: profile.unlink(missing_ok=True))
            self.assertNotEqual(code, 0)
            self.assertFalse(sibling.exists())

    def test_real_popen_linked_worktree_git_commit_and_oob_denied(self) -> None:
        # Fix 5 joint constraint: inside the profile a real git commit works
        # (ref/lock/reflog/objects allow), but writing outside the pinned
        # metadata fails closed.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            main = base / "main-repo"
            main.mkdir()
            env_extra = "GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null "
            subprocess.run(
                ["git", "init", "-q", str(main)],
                check=True,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["git", "-C", str(main), "config", "user.email", "t@t"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["git", "-C", str(main), "config", "user.name", "t"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            (main / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(main), "add", "seed.txt"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["git", "-C", str(main), "commit", "-qm", "seed"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            branch = "agent/hermes"
            subprocess.run(
                ["git", "-C", str(main), "worktree", "add", "-b", branch,
                 str(base / "wt"), "HEAD"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            from agent_bridge_connect.seatbelt import validate_linked_worktree

            capability = validate_linked_worktree(base / "wt")
            self.assertIsNotNone(capability)
            assert capability is not None
            state = base / "state"
            state.mkdir()
            task_temp = base / "task-temp"
            task_temp.mkdir()
            profile_text = build_seatbelt_profile(
                writable_roots=[str(base / "wt"), str(state), str(task_temp)],
                linked_worktree=capability,
            )
            # A real commit inside the linked worktree succeeds.
            script = (
                env_extra
                + "printf change > seed.txt && "
                "git add seed.txt && git commit -qm canary && git log --oneline -1"
            )
            code, stdout, stderr, profile = self._real_contained_run(
                profile_text, script, base / "wt"
            )
            self.addCleanup(lambda: profile.unlink(missing_ok=True))
            self.assertEqual(code, 0, stderr)
            self.assertIn("canary", stdout)
            # Writing another branch's ref inside the same profile fails.
            oob_ref = (
                f"mkdir -p {capability['common_dir']}/refs/heads/other && "
                f"printf x > {capability['common_dir']}/refs/heads/other/x"
            )
            code, _stdout, stderr, profile = self._real_contained_run(
                profile_text, oob_ref, base / "wt"
            )
            self.addCleanup(lambda: profile.unlink(missing_ok=True))
            self.assertNotEqual(code, 0)

    def test_task_temp_root_is_task_scoped(self) -> None:
        from agent_bridge_connect.seatbelt import task_temp_root

        with tempfile.TemporaryDirectory() as temporary:
            record_root = Path(temporary).resolve() / "record"
            one = task_temp_root(record_root, "E52M-003")
            two = task_temp_root(record_root, "E52M-004")
            self.assertEqual(one, record_root / "temp" / "E52M-003")
            self.assertEqual(two, record_root / "temp" / "E52M-004")
            self.assertNotEqual(one, two)
            # Profile allows the task's own temp but not the sibling's.
            one.mkdir(parents=True)
            profile_text = build_seatbelt_profile(
                writable_roots=[str(one)],
            )
            self.assertIn(f'(allow file-write* (subpath "{one}"))', profile_text)
            self.assertNotIn(str(two), profile_text)
            # No broad allow of the world-staging trees (only the task's own
            # temp dir may appear, which on this host may sit under /var).
            self.assertNotIn('(allow file-write* (subpath "/private/tmp"))', profile_text)
            self.assertNotIn('(allow file-write* (subpath "/var/folders"))', profile_text)


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
                "task_code": "E52M",
                "iteration": "002",
                "task_date": "2026-08-28",
                "task_id": "E52M-002",
                "report_file": str(
                    base
                    / "workspace"
                    / "tasks"
                    / "report"
                    / "2026-08-28"
                    / "E52M"
                    / "E52M-002-report.md"
                ),
            }
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(project), texts)
            self.assertIn(str(project / "artifacts"), texts)
            # The whole AgentBC workspace is never a writable root: only the
            # exact task record directory is; the shared task-code report
            # directory is not.
            self.assertNotIn(str(base / "workspace"), texts)
            self.assertIn(
                str(base / "workspace" / "record" / "E52M" / "002"), texts
            )
            self.assertNotIn(
                str(
                    base
                    / "workspace"
                    / "tasks"
                    / "report"
                    / "2026-08-28"
                    / "E52M"
                ),
                texts,
            )
            files = {str(path) for path in canonical_task_files(workspace)}
            self.assertEqual(
                files,
                {
                    str(
                        base
                        / "workspace"
                        / "tasks"
                        / "report"
                        / "2026-08-28"
                        / "E52M"
                        / "E52M-002-report.md"
                    )
                },
            )
            self.assertEqual(len(roots), len(texts))

    def test_report_file_is_literal_not_shared_report_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            report_file = base / "reports" / "E52M" / "E52M-003-report.md"
            profile = build_seatbelt_profile(
                writable_roots=[base / "record" / "E52M" / "003"],
                writable_files=[report_file],
            )
            self.assertIn(
                f'(allow file-write* (literal "{report_file}"))', profile
            )
            self.assertNotIn(
                f'(allow file-write* (subpath "{report_file.parent}"))', profile
            )

    def test_internal_task_dir_overrides_record_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            internal = base / "record" / "E52M" / "003"
            internal.mkdir(parents=True)
            workspace = {
                "project_root": str(base / "project"),
                "agentbc_root": str(base / "workspace"),
                "internal_task_dir": str(internal),
                "task_code": "E52M",
                "iteration": "003",
            }
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(internal), texts)
            self.assertEqual(
                texts - {str(internal)},
                {str(base / "project")},
            )

    def test_control_and_temp_dirs_enter_containment_when_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            control = base / "board" / ".agentbc-control" / "E52M-002"
            control.mkdir(parents=True)
            temp = base / "record" / "E52M" / "002" / "temp"
            temp.mkdir(parents=True)
            workspace = {
                "project_root": str(base / "project"),
                "control_dir": str(control),
                "temp_dir": str(temp),
            }
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(control), texts)
            self.assertIn(str(temp), texts)

    def test_empty_workspace_yields_no_roots(self) -> None:
        self.assertEqual(canonical_task_roots(None), [])
        self.assertEqual(canonical_task_roots({}), [])


if __name__ == "__main__":
    unittest.main()
