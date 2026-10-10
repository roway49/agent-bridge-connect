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
from agent_bridge_connect.permission_runtime import HOST_CONTAINMENT_UNLIFTABLE, LINKED_WORKTREE_CAPABILITY_INVALID
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
from agent_bridge_connect.permission_transport import assert_git_metadata_not_in_add_dir, assert_inner_sandbox_within_outer
import sys as _native_full_sys
import tempfile as _native_full_tempfile
import unittest as _native_full_unittest
from pathlib import Path as _native_full_Path
from unittest import mock as _native_full_mock
from agent_bridge_connect.effective_permissions import resolve_effective_permission as _native_full_resolve_effective_permission
from agent_bridge_connect.executors.claude import ClaudeExecutor as _native_full_ClaudeExecutor, _claude_control_required as _native_full__claude_control_required
from agent_bridge_connect.executors.codex import CodexExecutor as _native_full_CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor as _native_full_HermesExecutor
from agent_bridge_connect.permission_elevation import build_permission_elevation as _native_full_build_permission_elevation, record_permission_elevation_decision as _native_full_record_permission_elevation_decision
from agent_bridge_connect.permission_modes import build_permission_record as _native_full_build_permission_record


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
        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=self._run_git):
            capability = validate_linked_worktree(self.project)
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertEqual(capability["kind"], "linked")
        self.assertEqual(capability["symbolic_ref"], "refs/heads/agent/hermes")
        self.assertEqual(capability["ref_path"], self.common / "refs" / "heads" / "agent" / "hermes")
        self.assertEqual(capability["reflog_path"], self.common / "logs" / "refs" / "heads" / "agent" / "hermes")
        self.assertEqual(capability["objects_dir"], self.common / "objects")
        self.assertEqual(linked_worktree_git_metadata_dirs(capability), [str(self.git_dir), str(self.common)])

    def test_expected_ref_mismatch_is_branch_drift(self) -> None:
        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=self._run_git):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project, expected_ref="refs/heads/main")
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_detached_head_fails_closed(self) -> None:

        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "HEAD":
                return _git_fail()
            return self._run_git(command, **kwargs)

        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_bare_repository_fails_closed(self) -> None:

        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "--is-bare-repository":
                return _git_ok("true\n")
            return self._run_git(command, **kwargs)

        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_submodule_gitdir_fails_closed(self) -> None:

        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "--show-superproject-working-tree":
                return _git_ok(str(self.root / "super") + "\n")
            return self._run_git(command, **kwargs)

        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_topology_drift_fails_closed(self) -> None:

        def run_git(command: list[str], **kwargs: object):
            if command[-1] == "--absolute-git-dir":
                return _git_ok(str(self.root / "elsewhere" / ".git") + "\n")
            return self._run_git(command, **kwargs)

        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=run_git):
            with self.assertRaises(ABCError) as raised:
                validate_linked_worktree(self.project)
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_plain_directory_and_main_repo_add_no_capability(self) -> None:
        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", return_value=_git_fail()):
            self.assertIsNone(validate_linked_worktree(self.project))
        main = self.root / "main-repo"
        main.mkdir()
        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=lambda command, **kwargs: _git_ok(str(main / ".git") + "\n") if command[-1] in ("--absolute-git-dir", "--git-common-dir") else self._run_git(command, **kwargs)):
            self.assertIsNone(validate_linked_worktree(main))

    def test_symlinked_git_metadata_fails_closed(self) -> None:
        refs_target = self.root / "refs-elsewhere"
        refs_target.mkdir()
        refs_link = self.common / "refs"
        try:
            refs_link.symlink_to(refs_target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.addCleanup(lambda: refs_link.unlink(missing_ok=True))
        with mock.patch("agent_bridge_connect.seatbelt.subprocess.run", side_effect=self._run_git):
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
        profile = build_seatbelt_profile(writable_roots=[project, artifacts])
        self.assertTrue(profile.startswith("(version 1)\n(deny default)\n"))
        self.assertIn(f'(allow file-write* (subpath "{project}"))', profile)
        self.assertIn(f'(allow file-write* (subpath "{artifacts}"))', profile)
        self.assertIn("(allow file-read*)", profile)
        self.assertNotIn("refs/heads", profile)

    def test_linked_worktree_profile_pins_only_current_branch(self) -> None:
        profile = build_seatbelt_profile(writable_roots=["/tmp/task-project"], linked_worktree=self._linked())
        self.assertIn('(allow file-write* (subpath "/repo/.git/worktrees/agent"))', profile)
        self.assertIn('(allow file-write* (subpath "/repo/.git/objects"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/refs"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/worktrees"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/config"))', profile)
        self.assertIn('(deny file-write* (subpath "/repo/.git/hooks"))', profile)
        self.assertIn('(deny file-write* (literal "/repo/.git/packed-refs"))', profile)
        self.assertIn('(allow file-write* (literal "/repo/.git/refs/heads/agent/hermes"))', profile)
        self.assertIn('(allow file-write* (literal "/repo/.git/refs/heads/agent/hermes.lock"))', profile)
        self.assertIn('(allow file-write* (literal "/repo/.git/logs/refs/heads/agent/hermes"))', profile)
        self.assertLess(profile.index('(deny file-write* (subpath "/repo/.git/refs"))'), profile.index('(allow file-write* (literal "/repo/.git/refs/heads/agent/hermes"))'))

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
        assert_inner_sandbox_within_outer(["/tmp/task-project/out"], ["/tmp/task-project"])
        with self.assertRaises(ABCError) as raised:
            assert_inner_sandbox_within_outer(["/tmp/elsewhere"], ["/tmp/task-project"])
        self.assertEqual(raised.exception.code, LINKED_WORKTREE_CAPABILITY_INVALID)

    def test_launch_wraps_command_with_0600_profile(self) -> None:
        directory = Path(tempfile.mkdtemp())
        (wrapped, profile_path) = launch_with_seatbelt(["python", "-m", "worker"], directory, "(version 1)\n(deny default)\n", directory)
        self.assertEqual(wrapped, [SEATBELT_EXECUTABLE, "-f", str(profile_path), "python", "-m", "worker"])
        self.assertTrue(profile_path.exists())
        self.assertEqual(profile_path.read_text(encoding="utf-8"), "(version 1)\n(deny default)\n")
        self.assertEqual(oct(profile_path.stat().st_mode & 511), "0o600")

    def test_cleanup_removes_stale_profiles_after_crash(self) -> None:
        directory = Path(tempfile.mkdtemp())
        stale: list[Path] = []
        for _ in range(3):
            (_, profile_path) = launch_with_seatbelt(["python", "-m", "worker"], directory, "(version 1)\n", directory)
            stale.append(profile_path)
        unrelated = directory / "unrelated.txt"
        unrelated.write_text("keep me", encoding="utf-8")
        removed = cleanup_seatbelt_profiles(directory)
        self.assertEqual(removed, 3)
        for profile in stale:
            self.assertFalse(profile.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(cleanup_seatbelt_profiles(directory), 0)
        self.assertEqual(cleanup_seatbelt_profiles(directory / "missing"), 0)


class StaleProfileSweepTests(unittest.TestCase):
    """T2A6-003 review fix: the stale sweep must run BEFORE the new profile
    is created and must never delete an active profile - including the
    profile of the launch in progress and of concurrently running workers."""

    def setUp(self) -> None:
        if not seatbelt_available():
            self.skipTest("sandbox-exec unavailable")

    def test_sweep_before_launch_preserves_active_profiles(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: cleanup_seatbelt_profiles(directory))
        (_, active_profile) = launch_with_seatbelt(["true"], directory, "(version 1)\n", directory)
        removed = cleanup_stale_seatbelt_profiles(directory, keep=[str(active_profile)])
        self.assertEqual(removed, [])
        self.assertTrue(active_profile.exists())

    def test_sweep_removes_only_stale_and_keeps_named(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: cleanup_seatbelt_profiles(directory))
        (_, stale) = launch_with_seatbelt(["true"], directory, "(version 1)\n", directory)
        (_, keep_me) = launch_with_seatbelt(["true"], directory, "(version 1)\n", directory)
        removed = cleanup_stale_seatbelt_profiles(directory, keep=[str(keep_me)])
        self.assertEqual([Path(item).name for item in removed], [stale.name])
        self.assertFalse(stale.exists())
        self.assertTrue(keep_me.exists())

    def test_launch_then_sweep_never_deletes_the_new_profile(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: cleanup_seatbelt_profiles(directory))
        profile_text = build_seatbelt_profile(writable_roots=[str(directory)])
        (_, new_profile) = launch_with_seatbelt(["true"], directory, profile_text, directory)
        removed_before = cleanup_stale_seatbelt_profiles(directory, keep=[str(new_profile)])
        self.assertEqual(removed_before, [])
        self.assertTrue(new_profile.exists())

    def _real_contained_run(self, profile_text: str, script: str, cwd: Path):
        (wrapped, profile_path) = launch_with_seatbelt(["/bin/sh", "-c", script], cwd, profile_text, cwd)
        process = subprocess.Popen(wrapped, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (stdout, stderr) = process.communicate(timeout=30)
        return (process.returncode, stdout, stderr, profile_path)

    def test_task_temp_root_is_task_scoped(self) -> None:
        from agent_bridge_connect.seatbelt import task_temp_root

        with tempfile.TemporaryDirectory() as temporary:
            record_root = Path(temporary).resolve() / "record"
            one = task_temp_root(record_root, "T2A6-003")
            two = task_temp_root(record_root, "T2A6-004")
            self.assertEqual(one, record_root / "temp" / "T2A6-003")
            self.assertEqual(two, record_root / "temp" / "T2A6-004")
            self.assertNotEqual(one, two)
            one.mkdir(parents=True)
            profile_text = build_seatbelt_profile(writable_roots=[str(one)])
            self.assertIn(f'(allow file-write* (subpath "{one}"))', profile_text)
            self.assertNotIn(str(two), profile_text)
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
                "task_code": "T2A6",
                "iteration": "002",
                "task_date": "2026-08-28",
                "task_id": "T2A6-002",
                "report_file": str(base / "workspace" / "tasks" / "report" / "2026-08-28" / "T2A6" / "T2A6-002-report.md"),
            }
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(project), texts)
            self.assertIn(str(project / "artifacts"), texts)
            self.assertNotIn(str(base / "workspace"), texts)
            self.assertIn(str(base / "workspace" / "record" / "T2A6" / "002"), texts)
            self.assertNotIn(str(base / "workspace" / "tasks" / "report" / "2026-08-28" / "T2A6"), texts)
            files = {str(path) for path in canonical_task_files(workspace)}
            self.assertEqual(files, {str(base / "workspace" / "tasks" / "report" / "2026-08-28" / "T2A6" / "T2A6-002-report.md")})
            self.assertEqual(len(roots), len(texts))

    def test_report_file_is_literal_not_shared_report_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            report_file = base / "reports" / "T2A6" / "T2A6-003-report.md"
            profile = build_seatbelt_profile(writable_roots=[base / "record" / "T2A6" / "003"], writable_files=[report_file])
            self.assertIn(f'(allow file-write* (literal "{report_file}"))', profile)
            self.assertNotIn(f'(allow file-write* (subpath "{report_file.parent}"))', profile)

    def test_internal_task_dir_overrides_record_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            internal = base / "record" / "T2A6" / "003"
            internal.mkdir(parents=True)
            workspace = {"project_root": str(base / "project"), "agentbc_root": str(base / "workspace"), "internal_task_dir": str(internal), "task_code": "T2A6", "iteration": "003"}
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(internal), texts)
            self.assertEqual(texts - {str(internal)}, {str(base / "project")})

    def test_control_and_temp_dirs_enter_containment_when_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            control = base / "board" / ".agentbc-control" / "T2A6-002"
            control.mkdir(parents=True)
            temp = base / "record" / "T2A6" / "002" / "temp"
            temp.mkdir(parents=True)
            workspace = {"project_root": str(base / "project"), "control_dir": str(control), "temp_dir": str(temp)}
            roots = canonical_task_roots(workspace)
            texts = {str(root) for root in roots}
            self.assertIn(str(control), texts)
            self.assertIn(str(temp), texts)

    def test_empty_workspace_yields_no_roots(self) -> None:
        self.assertEqual(canonical_task_roots(None), [])
        self.assertEqual(canonical_task_roots({}), [])


class _native_full_PlanDTests(_native_full_unittest.TestCase):
    def test_full_commands_have_native_flags_without_agentbc_restrictions(self):
        with _native_full_tempfile.TemporaryDirectory() as directory:
            root = _native_full_Path(directory)
            permission = _native_full_build_permission_record(explicit_mode="full")
            packet = {"workspace": {"root": directory}, "extensions": {"agentbc.permission": permission}}
            claude = _native_full_ClaudeExecutor(command=_native_full_sys.executable)
            with _native_full_mock.patch("agent_bridge_connect.executors.claude.claude_ephemeral_path_capability", side_effect=AssertionError("full must not construct sandbox capabilities")):
                commands = {
                    "claude": claude._build_command("prompt", root, packet, permission),
                    "codex": _native_full_CodexExecutor(command=_native_full_sys.executable)._build_command(packet, "prompt", root, permission)[0],
                    "hermes": _native_full_HermesExecutor(command=_native_full_sys.executable, transport="direct")._build_command("prompt", permission=permission, task_packet=packet),
                }
            flags = {"claude": "--dangerously-skip-permissions", "codex": "--dangerously-bypass-approvals-and-sandbox", "hermes": "--yolo"}
            for executor, command in commands.items():
                with self.subTest(executor=executor):
                    self.assertIn(flags[executor], command)
                    for forbidden in ("--settings", "--add-dir", "--tools", "--allowedTools", "--disallowedTools", "--sandbox", "sandbox-exec"):
                        self.assertNotIn(forbidden, command)

    def test_legacy_grants_are_inert_even_when_malformed(self):
        for executor in ("codex", "claude", "hermes"):
            for grant in ({"version": 999}, {"state": {"status": "issued"}}, None):
                with self.subTest(executor=executor, grant=grant):
                    packet = {"extensions": {"agentbc.permission": _native_full_build_permission_record(explicit_mode="safe"), "agentbc.permission_grant": grant}}
                    self.assertEqual(_native_full_resolve_effective_permission(packet, executor, "run-1")["effective_mode"], "safe")

    def test_approved_elevation_resolves_full_for_each_executor_without_runtime(self):
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                elevation = _native_full_build_permission_elevation(task_id="ABCD-001", path_plan_digest="", executor=executor, executor_run_id="run-1", session_id="session-1", request_id="request-1", request_fingerprint="sha256:" + "a" * 64, operation="tool", containment_profile_digest="")
                elevation = _native_full_record_permission_elevation_decision(elevation, "approve_full", source="human")
                packet = {"task_id": "ABCD-001", "runner_authorization_required": True, "extensions": {"agentbc.permission": _native_full_build_permission_record(explicit_mode="safe"), "agentbc.permission_elevation": elevation}}
                self.assertEqual(_native_full_resolve_effective_permission(packet, executor, "run-2")["effective_mode"], "full")
                self.assertNotIn("agentbc.permission_runtime", packet["extensions"])
                self.assertFalse(_native_full__claude_control_required(packet))
                self.assertFalse(_native_full_CodexExecutor(command=_native_full_sys.executable)._uses_app_server_transport(packet))
