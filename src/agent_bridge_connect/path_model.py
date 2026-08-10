from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import resolve_workspace_root
from .protocol import ABCError
from .task_id import format_task_id, normalize_task_code


DEFAULT_CUSTOMER_PATH = "default path"
EXECUTOR_PROJECT_ROOT_LEAF = "claude"


def canonical_executor_project_root(
    agentbc_root: str | Path,
    task_date: str,
    task_code: str,
    task_id: str,
) -> Path:
    """Return the canonical task-scoped Claude project root.

    The path is strictly
    ``<agentbc_root>/tasks/artifacts/<task_date>/<task_code>/<task_id>/claude``.
    The full TASK-ID (``<TASKCODE>-<iteration>``) isolates every iteration and
    agent branch so they never share the handoff-chain task-code directory.
    The final ``claude`` leaf namespaces the executor project.
    """
    return (
        Path(agentbc_root).expanduser().resolve()
        / "tasks"
        / "artifacts"
        / str(task_date).strip()
        / str(task_code).strip()
        / str(task_id).strip()
        / EXECUTOR_PROJECT_ROOT_LEAF
    )


def is_default_customer_path(value: str | Path | None) -> bool:
    return not str(value or "").strip() or str(value).strip().lower() == DEFAULT_CUSTOMER_PATH


def derive_customer_path_plan(
    customer_path: str | Path | None,
    customer_dir: bool | None = None,
) -> tuple[bool, str | None]:
    if is_default_customer_path(customer_path):
        if customer_dir is True:
            raise ABCError("path_plan_missing", "customer_path is required when customer_dir=true")
        return False, None
    resolved = Path(str(customer_path).strip()).expanduser().resolve()
    # Agents pass the path the user actually named. When that path is an
    # existing file, Runner owns the mechanical conversion to a writable
    # project root instead of asking the dispatching agent to reason about it.
    if resolved.is_file():
        resolved = resolved.parent
    return True, str(resolved)


@dataclass(frozen=True)
class PathPlan:
    customer_dir: bool
    customer_path: str
    project_root: Path
    default_path: Path
    agentbc_root: Path
    artifact_root: Path
    report_root: Path
    task_file: Path
    report_file: Path
    task_code: str
    iteration: str
    task_id: str
    task_date: str
    executor_project_root: Path

    def to_workspace(self) -> dict[str, Any]:
        return {
            "customer_dir": self.customer_dir,
            "customer_path": self.customer_path,
            "project_root": str(self.project_root),
            "default_path": str(self.default_path),
            "executor_project_root": str(self.executor_project_root),
            "agentbc_root": str(self.agentbc_root),
            "root": str(self.project_root),
            "artifact_root": str(self.artifact_root),
            "artifacts_dir": str(self.artifact_root),
            "report_root": str(self.report_root),
            "task_file": str(self.task_file),
            "report_file": str(self.report_file),
            "output_dir": str(self.report_root),
            "task_code": self.task_code,
            "iteration": self.iteration,
            "task_date": self.task_date,
            "chain_id": self.task_code,
            "chain_token": self.task_code,
            "chain_dir": self.task_code,
            "chain_task_id": self.iteration,
            "chain_output_dir": str(self.report_root),
        }


def build_path_plan(
    *,
    customer_dir: bool | None,
    customer_path: str | Path | None,
    task_code: str,
    iteration: int | str,
    config: dict[str, Any] | None = None,
    task_date: str | None = None,
    record_root: str | Path | None = None,
) -> PathPlan:
    customer_dir, customer_path = derive_customer_path_plan(customer_path, customer_dir)
    code = normalize_task_code(task_code)
    iter_text = f"{int(iteration):03d}"
    date_text = str(task_date or datetime.now().strftime("%Y-%m-%d"))
    agentbc_root = resolve_workspace_root(config)
    if customer_dir:
        if customer_path is None or not str(customer_path).strip():
            raise ABCError("path_plan_missing", "customer_path is required when customer_dir=true")
        project_root = Path(customer_path).expanduser().resolve()
        customer_text = str(project_root)
        artifact_root = project_root
    else:
        customer_text = ""
        artifact_root = agentbc_root / "tasks" / "artifacts" / date_text / code
        # Core-owned reports and runtime records share the AgentBC workspace.
        # A managed executor must therefore work from its own artifact root,
        # never from the workspace root itself.
        project_root = artifact_root
    report_root = agentbc_root / "tasks" / "report" / date_text / code
    task_id = format_task_id(code, iter_text)
    executor_project_root = canonical_executor_project_root(
        agentbc_root, date_text, code, task_id
    )
    task_file = report_root / f"{task_id}-task.md"
    report_file = report_root / f"{task_id}-report.md"
    return PathPlan(
        customer_dir=bool(customer_dir),
        customer_path=customer_text,
        project_root=project_root,
        default_path=project_root,
        executor_project_root=executor_project_root,
        agentbc_root=agentbc_root,
        artifact_root=artifact_root,
        report_root=report_root,
        task_file=task_file,
        report_file=report_file,
        task_code=code,
        iteration=iter_text,
        task_id=task_id,
        task_date=date_text,
    )


def validate_path_plan_workspace(workspace: dict[str, Any]) -> None:
    if "customer_dir" not in workspace:
        raise ABCError("path_plan_missing", "task is missing customer_dir path-plan field")
    if not isinstance(workspace.get("customer_dir"), bool):
        raise ABCError("path_plan_missing", "customer_dir must be a boolean")
    if workspace.get("customer_dir") and not str(workspace.get("customer_path") or "").strip():
        raise ABCError("path_plan_missing", "customer_path is required when customer_dir=true")
    for field in ("project_root", "default_path", "agentbc_root", "artifact_root", "report_root", "task_file", "report_file", "task_code", "iteration", "task_date"):
        if not str(workspace.get(field) or "").strip():
            raise ABCError("path_plan_missing", f"task is missing {field} path-plan field")
    if not workspace.get("customer_dir"):
        project_root = Path(str(workspace["project_root"])).expanduser().resolve()
        artifact_root = Path(str(workspace["artifact_root"])).expanduser().resolve()
        managed_root = (
            Path(str(workspace["agentbc_root"])).expanduser().resolve()
            / "tasks"
            / "artifacts"
        )
        try:
            artifact_root.relative_to(managed_root)
        except ValueError as exc:
            raise ABCError(
                "path_plan_invalid",
                "managed task artifact_root must be inside AgentBC workspace/tasks/artifacts",
            ) from exc
        if project_root != artifact_root:
            raise ABCError(
                "path_plan_invalid",
                "managed task project_root must equal its task-scoped artifact_root",
            )
    # New plans built by build_path_plan() carry an internal executor project
    # root. Legacy board records (created before this phase) may omit it; when
    # present it must be the canonical task-scoped Claude project path.
    if str(workspace.get("executor_project_root") or "").strip():
        _validate_executor_project_root(workspace)


def _validate_executor_project_root(workspace: dict[str, Any]) -> None:
    """Validate the internal executor project root against the canonical form.

    The planned Claude project path is strictly
    ``<agentbc_root>/tasks/artifacts/<task_date>/<task_code>/<task_id>/claude``.
    This is a plan only: nothing is created and the path is not required to
    exist yet. It must match task_date/task_code/iteration exactly, stay inside
    the managed artifacts root after realpath resolution, and no existing
    ancestor may escape that root through a symlink.
    """
    agentbc_root = Path(str(workspace["agentbc_root"])).expanduser().resolve()
    task_date = str(workspace["task_date"]).strip()
    task_code = str(workspace["task_code"]).strip()
    iteration = str(workspace["iteration"]).strip()
    try:
        task_id = format_task_id(task_code, iteration)
    except ValueError as exc:
        raise ABCError(
            "path_plan_invalid",
            f"invalid task_code/iteration in path plan: {exc}",
        ) from exc
    managed_artifacts = agentbc_root / "tasks" / "artifacts"
    expected = canonical_executor_project_root(agentbc_root, task_date, task_code, task_id)

    raw = str(workspace.get("executor_project_root") or "").strip()
    actual = Path(raw).expanduser().resolve()

    try:
        actual.relative_to(managed_artifacts)
    except ValueError as exc:
        raise ABCError(
            "path_plan_invalid",
            "executor_project_root must be inside AgentBC workspace/tasks/artifacts",
        ) from exc

    if actual != expected:
        raise ABCError(
            "path_plan_invalid",
            "executor_project_root must be exactly "
            "<agentbc_root>/tasks/artifacts/<task_date>/<task_code>/<task_id>/claude",
        )

    _reject_existing_parent_symlink_escape(expected, managed_artifacts)


def _reject_existing_parent_symlink_escape(candidate: Path, managed_artifacts: Path) -> None:
    """Reject planned roots whose existing ancestors escape via symlink.

    The executor project root is never created here, so only ancestors that
    already exist can carry a symlink escape. Each existing ancestor must
    resolve back inside the managed artifacts root.
    """
    managed_resolved = managed_artifacts.resolve()
    current = managed_artifacts
    for part in candidate.relative_to(managed_artifacts).parts:
        current = current / part
        if not (os.path.islink(current) or current.exists()):
            continue
        resolved = current.resolve()
        try:
            resolved.relative_to(managed_resolved)
        except ValueError as exc:
            raise ABCError(
                "path_plan_invalid",
                f"executor_project_root parent escapes managed artifacts: {current}",
            ) from exc
