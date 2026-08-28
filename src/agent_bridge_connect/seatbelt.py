"""Runner-owned macOS Seatbelt containment (PERM-104-002).

The Runner validates the task's real paths under its lock, generates a
task-scoped Seatbelt profile and launches the worker inside it.  A linked
worktree gets exactly four narrow Git metadata allowances (per-worktree git
dir, common objects, the current branch's exact ref/lock/reflog) and explicit
denials for everything else Git-owned: other refs, other worktrees, common
config, hooks, packed-refs and the whole main repository.

Rules:

* a normal (non-linked) repository adds no capability at all;
* detached HEAD, bare repositories, submodule gitdirs, topology drift and
  any non-current ref fail closed with
  ``linked_worktree_capability_invalid``;
* when an expansion is required but ``sandbox-exec`` is unavailable, the
  Runner returns ``host_containment_unliftable`` before starting the worker:
  no dialog, no grant consumption;
* Git metadata is never handed to an executor's inner sandbox
  (``--add-dir``/allowWrite); the inner sandbox may only write within the
  outer task roots;
* ``full`` is effective only inside the frozen PathPlan - the profile is
  built exclusively from the frozen task workspace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .permission_runtime import (
    HOST_CONTAINMENT_UNLIFTABLE,
    LINKED_WORKTREE_CAPABILITY_INVALID,
)
from .protocol import ABCError

SEATBELT_EXECUTABLE = "sandbox-exec"

_SAFE_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,191}$")
_GIT_TIMEOUT_S = 10.0


def seatbelt_available() -> bool:
    """Return whether the macOS sandbox-exec binary is reachable."""
    return sys.platform == "darwin" and shutil.which(SEATBELT_EXECUTABLE) is not None


def preflight_host_containment(*, require_expansion: bool) -> None:
    """Fail closed before start when containment expansion is impossible.

    This is a pre-start check: it never pops a dialog and never consumes a
    grant.  A later identical request therefore converges to
    ``permission_escalation_ineffective`` through the block ledger.
    """
    if require_expansion and not seatbelt_available():
        raise ABCError(
            HOST_CONTAINMENT_UNLIFTABLE,
            "sandbox-exec is unavailable; host containment cannot be expanded for this task.",
            {"platform": sys.platform, "executable": SEATBELT_EXECUTABLE},
        )


def _run_git(
    git: str,
    args: list[str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git, *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except OSError as exc:
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            f"git plumbing is unavailable: {exc}",
            {"git": git},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "git plumbing timed out while validating the linked worktree.",
            {"git": git},
        ) from exc


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinked components in Git metadata paths (topology drift)."""
    current = Path(os.path.abspath(str(path)))
    components: list[Path] = []
    while True:
        components.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for component in components:
        if component.exists() and os.path.islink(str(component)):
            raise ABCError(
                LINKED_WORKTREE_CAPABILITY_INVALID,
                "Linked-worktree Git metadata must not traverse symlinks.",
                {"path": str(component)},
            )


def validate_linked_worktree(
    project_root: str | Path,
    *,
    expected_ref: str | None = None,
    git: str = "git",
) -> dict[str, Any] | None:
    """Validate one frozen PathPlan project root against its real Git layout.

    Returns ``None`` for a plain directory or a normal (main) repository -
    those add no containment capability.  Returns the pinned metadata for a
    linked worktree, or raises a stable ``linked_worktree_capability_invalid``
    for bare/submodule/detached layouts, topology drift, or a branch that no
    longer matches the frozen plan.
    """
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Linked-worktree validation requires an existing project root.",
            {"project_root": str(root)},
        )
    git_dir_probe = _run_git(git, ["-C", str(root), "rev-parse", "--absolute-git-dir"], root)
    if git_dir_probe.returncode != 0 or not git_dir_probe.stdout.strip():
        # Plain directory or an unreadable layout: no capability is added.
        return None
    git_dir = Path(git_dir_probe.stdout.strip()).resolve()
    common_probe = _run_git(
        git,
        ["-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        root,
    )
    if common_probe.returncode != 0 or not common_probe.stdout.strip():
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "git common dir is not resolvable for the task project.",
            {"project_root": str(root)},
        )
    common_dir = Path(common_probe.stdout.strip()).resolve()

    bare_probe = _run_git(git, ["-C", str(root), "rev-parse", "--is-bare-repository"], root)
    if bare_probe.returncode == 0 and bare_probe.stdout.strip() == "true":
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Bare repositories cannot be task containment roots.",
            {"project_root": str(root)},
        )
    super_probe = _run_git(
        git,
        ["-C", str(root), "rev-parse", "--show-superproject-working-tree"],
        root,
    )
    if super_probe.returncode == 0 and super_probe.stdout.strip():
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Submodule gitdirs cannot be task containment roots.",
            {"project_root": str(root)},
        )

    if git_dir == common_dir:
        # Normal (main) worktree: no extra capability.
        return None
    if not _is_within(git_dir, common_dir / "worktrees"):
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Linked-worktree git dir drifted from the git common dir topology.",
            {"project_root": str(root)},
        )

    symbolic = _run_git(git, ["-C", str(root), "symbolic-ref", "-q", "HEAD"], root)
    if symbolic.returncode != 0 or not symbolic.stdout.strip():
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Detached HEAD cannot be pinned by a task-scoped containment profile.",
            {"project_root": str(root)},
        )
    ref = symbolic.stdout.strip()
    if not _SAFE_REF_RE.fullmatch(ref):
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Current symbolic ref is not a safe branch ref.",
            {"project_root": str(root), "symbolic_ref": ref},
        )
    if expected_ref is not None and ref != expected_ref:
        raise ABCError(
            LINKED_WORKTREE_CAPABILITY_INVALID,
            "Current symbolic ref no longer matches the frozen task plan.",
            {"expected_ref": expected_ref, "symbolic_ref": ref},
        )

    branch = ref.rsplit("/", 1)[-1]
    ref_path = common_dir / ref
    lock_path = Path(str(ref_path) + ".lock")
    reflog_path = common_dir / "logs" / ref
    for metadata_path in (git_dir, common_dir, ref_path, reflog_path):
        _reject_symlink_components(metadata_path)

    return {
        "kind": "linked",
        "git_dir": git_dir,
        "common_dir": common_dir,
        "symbolic_ref": ref,
        "branch": branch,
        "ref_path": ref_path,
        "lock_path": lock_path,
        "reflog_path": reflog_path,
        "objects_dir": common_dir / "objects",
    }


def linked_worktree_git_metadata_dirs(capability: dict[str, Any] | None) -> list[str]:
    """Return the Git metadata directories that must never enter an inner
    sandbox and that the outer profile allows only narrowly."""
    if not isinstance(capability, dict):
        return []
    return [
        str(capability["git_dir"]),
        str(capability["common_dir"]),
    ]


def canonical_task_roots(workspace: dict[str, Any] | None) -> list[Path]:
    """Realpath-canonicalize every frozen PathPlan root (no raw writes).

    The Seatbelt profile pins real paths, so aliased paths (for example
    ``/tmp`` vs ``/private/tmp``) cannot bypass containment.
    """
    values = workspace if isinstance(workspace, dict) else {}
    roots: list[Path] = []
    for key in (
        "agentbc_root",
        "project_root",
        "root",
        "artifact_root",
        "artifacts_dir",
        "report_root",
        "output_dir",
    ):
        text = str(values.get(key) or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve())
    customer_path = str(values.get("customer_path") or "").strip()
    if customer_path:
        roots.append(Path(customer_path).expanduser().resolve())
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        text = str(root)
        if text not in seen:
            seen.add(text)
            unique.append(root)
    return unique


def build_seatbelt_profile(
    *,
    writable_roots: list[str | Path],
    linked_worktree: dict[str, Any] | None = None,
) -> str:
    """Build the task-scoped SBPL profile from frozen roots only.

    The profile denies everything by default, allows reads, and allows
    writes only inside the frozen task roots plus the pinned linked-worktree
    Git metadata.  Later rules win in sandbox-exec, so the narrow ref allows
    follow the broad refs/worktrees denials.
    """
    roots = [str(Path(root).expanduser().resolve()) for root in writable_roots or []]
    if not roots:
        raise ABCError(
            HOST_CONTAINMENT_UNLIFTABLE,
            "A Seatbelt profile requires at least one writable task root.",
        )
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow file-read*)",
    ]
    # Executor-owned state (Claude/Codex/Hermes config and history) lives
    # under the user's home; without it the executor cannot run at all.  The
    # Git metadata denies below are evaluated after this allow, so they still
    # pin the repository boundary exactly.
    home = str(Path.home().expanduser().resolve())
    lines.append(f'(allow file-write* (subpath "{_escape_sbpl(home)}"))')
    linked = isinstance(linked_worktree, dict)
    if linked:
        # sandbox-exec applies the last matching rule, so every broad deny is
        # emitted before the narrow allows that must survive it.
        main_repo_root = str(Path(linked_worktree["common_dir"]).parent)
        lines.append(f'(deny file-write* (subpath "{_escape_sbpl(main_repo_root)}"))')
        common_dir = str(linked_worktree["common_dir"])
        for denied in ("refs", "worktrees", "config", "hooks"):
            lines.append(
                f'(deny file-write* (subpath "{_escape_sbpl(str(Path(common_dir) / denied))}"))'
            )
        packed_refs = str(Path(common_dir) / "packed-refs")
        lines.append(f'(deny file-write* (literal "{_escape_sbpl(packed_refs)}"))')
    for root in roots:
        lines.append(f'(allow file-write* (subpath "{_escape_sbpl(root)}"))')
    if linked:
        git_dir = str(linked_worktree["git_dir"])
        ref_path = str(linked_worktree["ref_path"])
        lock_path = str(linked_worktree["lock_path"])
        reflog_path = str(linked_worktree["reflog_path"])
        objects_dir = str(linked_worktree["objects_dir"])
        # The per-worktree git dir is fully writable (index, HEAD, per-worktree
        # refs like refs/worktree/<name>); common objects are append-only in
        # practice and reads are already allowed.
        lines.append(f'(allow file-write* (subpath "{_escape_sbpl(git_dir)}"))')
        lines.append(f'(allow file-write* (subpath "{_escape_sbpl(objects_dir)}"))')
        # Narrow allows for the current branch only (exact ref, lock, reflog).
        lines.append(f'(allow file-write* (literal "{_escape_sbpl(ref_path)}"))')
        lines.append(f'(allow file-write* (literal "{_escape_sbpl(lock_path)}"))')
        lines.append(f'(allow file-write* (literal "{_escape_sbpl(reflog_path)}"))')
    return "\n".join(lines) + "\n"


def _escape_sbpl(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def launch_with_seatbelt(
    command: list[str],
    cwd: str | Path,
    profile_text: str,
    profile_dir: str | Path,
) -> tuple[list[str], Path]:
    """Persist the Runner-owned profile (0600) and wrap the worker command."""
    directory = Path(profile_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_path = directory / f"task-{uuid.uuid4().hex[:12]}.sb"
    with profile_path.open("w", encoding="utf-8") as handle:
        handle.write(profile_text)
    os.chmod(profile_path, 0o600)
    wrapped = [SEATBELT_EXECUTABLE, "-f", str(profile_path), *command]
    return wrapped, profile_path


__all__ = [
    "SEATBELT_EXECUTABLE",
    "build_seatbelt_profile",
    "canonical_task_roots",
    "launch_with_seatbelt",
    "linked_worktree_git_metadata_dirs",
    "preflight_host_containment",
    "seatbelt_available",
    "validate_linked_worktree",
]
