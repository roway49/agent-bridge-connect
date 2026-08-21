from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .approval import (
    APPROVAL_EXTENSION_KEY,
    APPROVAL_SCOPE,
    approval_public_projection,
    build_approval_receipt,
    normalize_reason_summary,
    record_approval_decision,
    sanitize_reason_detail,
    validate_approval_receipt,
)
from .config import DEFAULT_BOARD_ROOT, get_executor_config, init_board
from .execution_contract import validate_callback_payload
from .execution_policy import (
    RESOURCE_EXTENSION_KEY,
    RESOURCE_KIND_BY_EXECUTOR,
    SESSION_EXTENSION_KEY,
    apply_resource_input_decision,
    attach_execution_policy,
    build_task_execution_policy,
    execution_policy_view,
    is_resource_decision_request,
    next_resource_limit,
    public_task_view,
    validate_execution_session_receipt,
    validate_execution_policy_extensions,
    validate_resource_snapshot,
    validate_session_snapshot,
)
from .migration import (
    assert_legacy_cutover_clear,
    assert_maintenance_command_allowed,
    legacy_permission_cutover_blocked,
    maintenance_mode_view,
)
from .executor_registry import get_executor
from .media import media_extension, normalize_image_inputs, task_image_paths
from .path_model import build_path_plan, validate_path_plan_workspace
from .permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
    revoke_permission_grant as revoke_grant_contract,
)
from .permission_failures import (
    PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
    PERMISSION_CHAIN_HEAD_AMBIGUOUS,
    PERMISSION_CHAIN_HEAD_STALE,
    PERMISSION_EXECUTOR_SESSION_MISMATCH,
    PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
    PERMISSION_INPUT_INVALID,
    PERMISSION_MODE_UNSUPPORTED,
    PERMISSION_REQUESTED_SCOPE_INVALID,
    PERMISSION_RESUME_SESSION_MISSING,
    PERMISSION_RUN_LEASE_INVALID,
    PERMISSION_RUN_LEASE_RUN_MISMATCH,
    PERMISSION_SESSION_RECEIPT_INVALID,
    PERMISSION_SESSION_RECEIPT_MISSING,
    PERMISSION_SESSION_SNAPSHOT_INVALID,
    PERMISSION_SESSION_STATE_STALE,
    PERMISSION_WAIT_COMPATIBILITY_CODE,
    PermissionWaitFailure,
    permission_wait_failure,
)
from .permission_modes import (
    PERMISSION_EXTENSION_KEY,
    assert_executor_permission_supported,
    build_permission_record,
    permission_record_from_extensions,
    permission_runtime_policy,
)
from .protocol import ABCError, PreflightResult, TaskModel, task_step_text
from .schema import validate_task
from .state_machine import validate_transition
from .task_id import format_task_id, is_task_like, split_task_ref, task_iteration
from .task_index import refresh_task_index
from .task_store import TaskStore
from .terminal_states import TASK_TERMINAL_STATES


RUNNING_TASK_STATUSES = {
    "running",
    "input_required",
    "assigned",
    "working",
    "pause_pending",
    "paused",
    "in_progress",
}
REPORTABLE_TASK_STATUSES = set(TASK_TERMINAL_STATES)
PUBLIC_TASK_STATUSES = {
    "pending",
    "running",
    "input_required",
    "completed",
    "failed",
    "cancelled",
    "rejected",
    "needs_recovery",
}
HANDOFF_SOURCE_STATUSES = {"completed"}
DELETE_ELIGIBLE_STATUSES = {"completed", "failed", "cancelled", "rejected"}
DEFAULT_INPUT_WAIT_SECONDS = 24 * 60 * 60
PERMISSION_DIALOG_TIMEOUT_RESPONSE = "agentbc_permission_dialog_timeout"
PERMISSION_DIALOG_CLOSED_RESPONSE = "agentbc_permission_dialog_closed"


@dataclass(frozen=True)
class ChainResolution:
    requested_task_id: str
    chain_root_task_id: str
    head_task_ids: list[str]
    current_head_task_id: str | None
    requested_is_head: bool
    members: list[dict[str, Any]]
    anomalies: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_task_id": self.requested_task_id,
            "chain_root_task_id": self.chain_root_task_id,
            "head_task_ids": self.head_task_ids,
            "current_head_task_id": self.current_head_task_id,
            "requested_is_head": self.requested_is_head,
            "members": self.members,
            "anomalies": self.anomalies,
        }


class TaskService:
    def __init__(self, board_root: str | Path = DEFAULT_BOARD_ROOT, config: dict[str, Any] | None = None):
        self.board_root = Path(board_root).expanduser().resolve()
        self.config = config or {}
        init_board(self.board_root)
        self.store = TaskStore(self.board_root)

    def create_task(
        self,
        title: str,
        assignee: str,
        steps: list[dict[str, Any]],
        session_id: str | None = None,
        source_platform: str | None = None,
        customer_dir: bool | None = None,
        customer_path: str | Path | None = None,
        workspace_root: str | Path | None = None,
        output_dir: str | Path | None = None,
        artifacts_dir: str | Path | None = None,
        lineage: dict[str, Any] | None = None,
        images: list[str | Path] | None = None,
        permission_mode: str | None = None,
        inherited_permission: dict[str, Any] | None = None,
    ) -> TaskModel:
        assert_maintenance_command_allowed(self, "create")
        title = title.strip()
        assignee = _normalize_executor_ref(
            assignee,
            field="assignee",
            empty_code="task_create_error",
            empty_message="assignee is required",
        )
        if not title:
            raise ABCError("task_create_error", "title is required")
        if not steps:
            raise ABCError("task_create_error", "steps must define at least one step")
        if workspace_root is not None or output_dir is not None or artifacts_dir is not None:
            raise ABCError(
                "path_model_v2_required",
                "Use customer_dir/customer_path instead of workspace_root/output_dir/artifacts_dir",
            )

        now = _utc_now()
        task_steps = [_normalize_step(step, index) for index, step in enumerate(steps, 1)]
        if lineage is None:
            self._reject_managed_artifact_new_root(
                customer_path,
                target_assignee=assignee,
                source_platform=source_platform,
            )
        lineage_data = dict(lineage or {})
        task_code = str(lineage_data.get("task_code") or self.store.allocate_task_code())
        iteration_index = int(lineage_data.get("iteration_index") or 1)
        while (self.store.tasks_dir / task_code / f"{iteration_index:03d}").exists():
            iteration_index += 1
        if lineage is not None:
            lineage_data["iteration_index"] = iteration_index
        task_date = str(lineage_data.get("task_date") or datetime.now().strftime("%Y-%m-%d"))
        path_config = self.config
        if lineage_data.get("agentbc_root"):
            path_config = {**self.config, "workspace_root": lineage_data["agentbc_root"]}
        path_plan = build_path_plan(
            customer_dir=customer_dir,
            customer_path=customer_path,
            task_code=task_code,
            iteration=iteration_index,
            config=path_config,
            task_date=task_date,
            record_root=self.board_root,
        )
        task_id = path_plan.task_id
        workspace = path_plan.to_workspace()
        workspace["internal_task_dir"] = str(self.store.tasks_dir / workspace["task_code"] / workspace["iteration"])
        normalized_images = normalize_image_inputs(
            images,
            allowed_roots=(workspace.get("agentbc_root"), workspace.get("project_root")),
        )
        task_lineage = _build_lineage(task_id, workspace, lineage_data if lineage is not None else None)
        permission = build_permission_record(
            explicit_mode=permission_mode,
            config=self.config,
            inherited=inherited_permission,
        )
        resources, executor_session = build_task_execution_policy(
            assignee,
            self.config,
            workspace,
            created_at=now,
        )
        extensions = attach_execution_policy(
            {
                "agentbc.provenance": {
                    "source_platform": source_platform or "unknown",
                    "conversation_id": session_id,
                },
                "agentbc.lineage": task_lineage,
                "agentbc.execution": {"internal_status": "pending"},
                PERMISSION_EXTENSION_KEY: permission,
                **media_extension(normalized_images),
            },
            resources=resources,
            session=executor_session,
        )
        task = TaskModel(
            id=task_id,
            title=title,
            status="pending",
            assignee=assignee,
            steps=task_steps,
            created_at=now,
            updated_at=now,
            workspace=workspace,
            session_id=session_id,
            created_by=source_platform or "user",
            extensions=extensions,
        )
        task_dir = self.store.tasks_dir / workspace["task_code"] / workspace["iteration"]
        (task_dir / "steps").mkdir(parents=True, exist_ok=False)
        try:
            if not workspace.get("customer_dir"):
                Path(workspace["artifacts_dir"]).mkdir(parents=True, exist_ok=True)
            Path(workspace["task_file"]).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ABCError(
                "task_create_error",
                f"workspace root is not writable: {workspace['root']}",
                {"workspace_root": workspace["root"], "error": str(exc)},
            ) from exc
        for index in range(1, len(task_steps) + 1):
            (task_dir / "steps" / f"{index:02d}.json").write_text("{}\n", encoding="utf-8")
        self.store.write_task(task_id, _without_none(task.to_dict()))
        try:
            _write_task_requirements(task, Path(workspace["task_file"]))
        except OSError as exc:
            raise ABCError(
                "task_create_error",
                f"workspace root is not writable: {workspace['root']}",
                {"workspace_root": workspace["root"], "error": str(exc)},
            ) from exc
        self.store.append_event(task_id, {"event_type": "created", "task_id": task_id, "created_at": now})
        try:
            from .record_management import assert_task_record_budget

            assert_task_record_budget(task_dir)
        except ABCError:
            shutil.rmtree(task_dir, ignore_errors=True)
            if not workspace.get("customer_dir"):
                try:
                    Path(workspace["artifacts_dir"]).rmdir()
                except OSError:
                    pass
            raise
        self._refresh_task_index()
        return task

    def get_task(self, task_id: str) -> TaskModel:
        return TaskModel.from_dict(self.store.read_task(task_id))

    def ensure_task_permission(self, task_id: str) -> TaskModel:
        """Validate and persist the conservative fallback for legacy task records."""
        task = self.get_task(task_id)
        permission = permission_record_from_extensions(task.extensions)
        if PERMISSION_EXTENSION_KEY not in (task.extensions or {}):
            task.extensions = dict(task.extensions or {})
            task.extensions[PERMISSION_EXTENSION_KEY] = permission
            task.updated_at = _utc_now()
            self.store.write_task(task.id, _without_none(task.to_dict()))
            task_file = str((task.workspace or {}).get("task_file") or "").strip()
            if task_file:
                _write_task_requirements(task, Path(task_file).expanduser())
            self._refresh_task_index()
        return task

    def _reject_managed_artifact_new_root(
        self,
        customer_path: str | Path | None,
        *,
        target_assignee: str,
        source_platform: str | None,
    ) -> None:
        raw_path = str(customer_path or "").strip()
        if not raw_path or raw_path.lower() == "default path":
            return
        requested = Path(raw_path).expanduser().resolve()
        if requested.is_file():
            requested = requested.parent
        owners: list[TaskModel] = []
        for candidate in self.list_tasks():
            workspace = candidate.workspace or {}
            if bool(workspace.get("customer_dir")):
                continue
            artifact_value = workspace.get("artifact_root") or workspace.get("artifacts_dir")
            if not artifact_value:
                continue
            artifact_root = Path(str(artifact_value)).expanduser().resolve()
            if _path_is_within(requested, artifact_root):
                owners.append(candidate)
        if not owners:
            return
        owner = max(
            owners,
            key=lambda item: (
                task_iteration(item.id) or 0,
                str(item.updated_at or ""),
            ),
        )
        try:
            chain = self.resolve_chain(owner.id)
            source_id = chain.current_head_task_id or owner.id
        except ABCError:
            source_id = owner.id
        platform_flag = f" --source-platform {source_platform}" if source_platform else ""
        suggested = (
            f"agentbc task handoff {source_id} --to {target_assignee}"
            f"{platform_flag} --dispatch"
        )
        raise ABCError(
            "handoff_required",
            (
                f"customer_path belongs to managed artifacts for AgentBC task {source_id}; "
                "continue that chain with task handoff instead of creating a new task code"
            ),
            {
                "requested_customer_path": str(requested),
                "source_task_id": source_id,
                "source_status": _normalize_status(owner.status),
                "suggested_command": suggested,
            },
        )

    def list_tasks(self, status: str | None = None, assignee: str | None = None) -> list[TaskModel]:
        tasks = [
            TaskModel.from_dict(data)
            for data in self.store.list_tasks(status=status, assignee=assignee)
        ]
        refreshed = self._refresh_active_tasks(tasks)
        return sorted(refreshed, key=_task_sort_key)

    def list_task_summaries(
        self,
        status: str | None = None,
        assignee: str | None = None,
        current_only: bool = False,
        all_iterations: bool = False,
    ) -> list[dict[str, Any]]:
        tasks = self.list_tasks(assignee=assignee)
        tasks = tasks if all_iterations else _chain_heads(tasks)
        if status is not None and status != "all":
            tasks = [task for task in tasks if task.status == status]
        if current_only:
            tasks = [task for task in tasks if _is_running_status(task.status)]
        return [_task_summary(task, self.board_root) for task in tasks]

    def task_summary(self, task_id: str) -> dict[str, Any]:
        return _task_summary(self.get_task(task_id), self.board_root)

    def resolve_chain(self, task_id: str) -> ChainResolution:
        requested = self.get_task(task_id)
        requested_lineage = _lineage_for(requested)
        task_code = _task_code_for(requested)
        chain_root_task_id = str(requested_lineage.get("chain_root_task_id") or format_task_id(task_code, 1))
        tasks = [
            task
            for task in self.list_tasks()
            if _task_code_for(task) == task_code
        ]
        task_ids = {task.id for task in tasks}
        child_ids = {
            str(parent_id)
            for task in tasks
            for parent_id in [_lineage_for(task).get("parent_task_id")]
            if isinstance(parent_id, str) and parent_id
        }
        head_task_ids = sorted(
            [task.id for task in tasks if task.id not in child_ids],
            key=_task_id_sort_value,
            reverse=True,
        )
        anomalies: list[str] = []
        if requested.id not in task_ids:
            anomalies.append(f"requested task {requested.id} is missing from chain members")
        for task in tasks:
            lineage = _lineage_for(task)
            parent_id = lineage.get("parent_task_id")
            if isinstance(parent_id, str) and parent_id and parent_id not in task_ids:
                anomalies.append(f"task {task.id} references missing parent {parent_id}")
            root_id = str(lineage.get("chain_root_task_id") or format_task_id(task_code, 1))
            if root_id != chain_root_task_id:
                anomalies.append(f"task {task.id} has inconsistent chain root {root_id}")
        current_head_task_id = head_task_ids[0] if len(head_task_ids) == 1 else None
        return ChainResolution(
            requested_task_id=requested.id,
            chain_root_task_id=chain_root_task_id,
            head_task_ids=head_task_ids,
            current_head_task_id=current_head_task_id,
            requested_is_head=requested.id in head_task_ids,
            members=[_task_summary(task, self.board_root) for task in tasks],
            anomalies=anomalies,
        )

    def resolve_task(self, task_id: str | None = None) -> dict[str, Any]:
        tasks = self.list_tasks()
        running_tasks = [task for task in tasks if _is_running_status(task.status)]
        active_candidates = [_task_summary(task, self.board_root) for task in running_tasks]
        if task_id:
            task = self.get_task(task_id)
            return {
                "resolved_task_id": task.id,
                "resolution_mode": "explicit_id",
                "active_candidates": active_candidates,
                "has_active_task": bool(running_tasks),
                "message": f"Resolved explicit task id: {task.id}",
                "current_task": self._task_status_with_chain(task),
            }
        if len(running_tasks) == 1:
            task = running_tasks[0]
            return {
                "resolved_task_id": task.id,
                "resolution_mode": "single_active",
                "active_candidates": active_candidates,
                "has_active_task": True,
                "message": "Resolution: current active task",
                "current_task": self._task_status_with_chain(task),
            }
        if len(running_tasks) > 1:
            return {
                "resolved_task_id": None,
                "resolution_mode": "ambiguous",
                "active_candidates": active_candidates,
                "has_active_task": True,
                "message": "Ambiguous current task. Multiple active tasks match; pass an explicit task id.",
                "current_task": None,
            }
        if not tasks:
            raise ABCError("task_not_found", "No tasks found in the task board")
        reportable_tasks = [task for task in tasks if _is_reportable_status(task.status)]
        pending_count = sum(1 for task in tasks if _normalize_status(task.status) == "pending")
        if not reportable_tasks:
            noun = "task" if pending_count == 1 else "tasks"
            return {
                "resolved_task_id": None,
                "resolution_mode": "no_reportable_task",
                "active_candidates": [],
                "has_active_task": False,
                "pending_count": pending_count,
                "message": (
                    "No task is currently running and no finished task is available. "
                    f"{pending_count} pending {noun}; use task list or pass an explicit task id."
                ),
                "current_task": None,
            }
        latest_updated = reportable_tasks[0].updated_at
        latest_candidates = [task for task in reportable_tasks if task.updated_at == latest_updated]
        if len(latest_candidates) > 1:
            return {
                "resolved_task_id": None,
                "resolution_mode": "ambiguous",
                "active_candidates": [_task_summary(task, self.board_root) for task in latest_candidates],
                "has_active_task": False,
                "message": "Ambiguous current task. Multiple recently updated tasks match; pass an explicit task id.",
                "current_task": None,
            }
        task = reportable_tasks[0]
        return {
            "resolved_task_id": task.id,
            "resolution_mode": "latest_updated",
            "active_candidates": [],
            "has_active_task": False,
            "message": "Resolution: latest reportable task\nNo task is currently running.",
            "current_task": self._task_status_with_chain(task),
        }

    def claim_task(self, task_id: str, executor_id: str) -> str:
        return self.start_task_run(task_id, executor_id)

    def start_task_run(self, task_id: str, executor_id: str) -> str:
        from .task_health import write_task_progress

        assert_maintenance_command_allowed(self, "dispatch")
        task = self.get_task(task_id)
        validate_path_plan_workspace(task.workspace or {})
        execution = dict((task.extensions or {}).get("agentbc.execution") or {})
        is_resuming = (
            _normalize_status(task.status) == "running"
            and execution.get("internal_status") == "resuming"
        )
        if _normalize_status(task.status) not in {"pending", "needs_recovery"} and not is_resuming:
            raise ABCError("invalid_transition", f"Cannot start task in state: {task.status}")
        source_status = (
            "running"
            if is_resuming
            else "pending" if _normalize_status(task.status) == "pending" else "needs_recovery"
        )
        validate_transition(source_status, "running")
        lease = self.store.acquire_lease(task_id, executor_id)
        if lease is None:
            raise ABCError("task_leased", f"Task is already leased: {task_id}", {"task_id": task_id})
        task.status = "running"
        task.assignee = executor_id
        task.updated_at = _utc_now()
        task.extensions = _merge_execution(
            task.extensions,
            {"internal_status": "running", "lease_state": "active"},
        )
        self.store.write_task(task_id, _without_none(task.to_dict()))
        write_task_progress(task, state="running", message="task started", source="runner")
        self.store.append_event(
            task_id,
            {
                "event_type": "task.started",
                "task_id": task_id,
                "executor_id": executor_id,
                "created_at": task.updated_at,
            },
        )
        self._refresh_task_index()
        return lease

    def record_executor_run_started(self, task_id: str, run_id: str) -> dict[str, Any]:
        """Append one executor run and freeze whether it is a session resume."""
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        session = extensions.get(SESSION_EXTENSION_KEY)
        errors = validate_session_snapshot(session, executor=task.assignee)
        if errors:
            raise ABCError("executor_session_invalid", "; ".join(errors), {"errors": errors})
        session = dict(session)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ABCError("executor_run_id_invalid", "Executor run ID is required")
        run_ids = list(session.get("run_ids") or [])
        if normalized_run_id in run_ids:
            return {
                "run_id": normalized_run_id,
                "resumed": len(run_ids) > 1,
                "session_state": session.get("session_state"),
            }
        resumed = bool(run_ids)
        if resumed and not str(session.get("session_id") or "").strip():
            raise ABCError(
                "executor_session_resume_unavailable",
                "A prior executor run exists but no authoritative session ID is available",
            )
        run_ids.append(normalized_run_id)
        session["run_ids"] = run_ids
        if resumed:
            session["resume_count"] = int(session.get("resume_count") or 0) + 1
        if str(session.get("session_id") or "").strip():
            session["session_state"] = "active"
        errors = validate_session_snapshot(session, executor=task.assignee)
        if errors:
            raise ABCError("executor_session_invalid", "; ".join(errors), {"errors": errors})
        extensions[SESSION_EXTENSION_KEY] = session
        task.extensions = extensions
        task.updated_at = _utc_now()
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "executor.session_run_started",
                "task_id": task.id,
                "executor": task.assignee,
                "run_id": normalized_run_id,
                "resumed": resumed,
                "created_at": task.updated_at,
            },
        )
        return {
            "run_id": normalized_run_id,
            "resumed": resumed,
            "session_state": session.get("session_state"),
        }

    def validate_executor_session_result(
        self,
        task_id: str,
        run_id: str,
        receipt: Any,
    ) -> dict[str, Any]:
        """Validate an adapter receipt against the durable task session without writing."""
        task = self.get_task(task_id)
        return self._validated_executor_session(task, run_id, receipt)

    def execute_step(self, task_id: str, step_id: int, result: dict[str, Any]) -> None:
        task = self.get_task(task_id)
        public_status = _normalize_status(task.status)
        if public_status not in {"running", "input_required"}:
            raise ABCError("invalid_transition", f"Cannot update step in state: {task.status}")
        task.steps = [_update_step(step, step_id, result) for step in task.steps]
        task.updated_at = _utc_now()
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(task_id, {"event_type": "step_executed", "task_id": task_id, "step_id": step_id, "created_at": task.updated_at, "result": result})

    def complete_task(self, task_id: str) -> None:
        raise ABCError(
            "completion_marker_required",
            "Task completion requires a valid AGENTBC_FINAL_CALLBACK from the executor",
        )

    def fail_task(
        self,
        task_id: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.mark_task_needs_recovery(task_id, code, message, details)

    def record_agent_callback(
        self,
        task_id: str,
        callback: dict[str, Any],
    ) -> bool:
        """Store an agent declaration without treating it as process termination."""
        task = self.get_task(task_id)
        task_id = task.id
        state = str(callback.get("final_state") or "").strip()
        summary = str(callback.get("summary") or "").strip()
        if state not in {"completed", "needs_recovery", "input_required", "cancelled"}:
            raise ABCError("invalid_agent_callback", f"Unsupported callback state: {state}")
        if not summary:
            raise ABCError("invalid_agent_callback", "Agent callback summary is required")
        now = _utc_now()
        if _normalize_status(task.status) in TASK_TERMINAL_STATES:
            self.store.append_event(
                task_id,
                {
                    "event_type": "task.late_agent_callback",
                    "task_id": task_id,
                    "created_at": now,
                    "declared_state": state,
                },
            )
            return False
        from .record_management import compact_diagnostic_details

        intent = {
            "task_id": task_id,
            "declared_state": state,
            "summary": str(compact_diagnostic_details(summary)),
            "report_file": str(callback.get("report_file") or ""),
            "artifacts_dir": str(callback.get("artifacts_dir") or ""),
            "executor_run_id": str(callback.get("executor_run_id") or ""),
            "received_at": now,
        }
        task.extensions = dict(task.extensions or {})
        task.extensions["agentbc.completion_intent"] = intent
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(
            task_id,
            {
                "event_type": "task.agent_callback_recorded",
                "task_id": task_id,
                "created_at": now,
                "declared_state": state,
            },
        )
        return True

    def finalize_task_from_executor_exit(
        self,
        task_id: str,
        *,
        executor_run_id: str,
        summary: str = "",
        exit_code: int = 0,
        callback: dict[str, Any] | None = None,
        execution_session: dict[str, Any] | None = None,
    ) -> bool:
        """Finalize an executor only from its valid structured flow declaration."""
        task = self.get_task(task_id)
        if not isinstance(callback, dict):
            raise ABCError(
                "completion_marker_missing",
                "Executor exited without a valid AGENTBC_FINAL_CALLBACK",
            )
        declared = dict(callback)
        declared["executor_run_id"] = executor_run_id
        declared["exit_code"] = int(exit_code)
        declared["source"] = "executor_final_marker"
        declared["outcome"] = "flow_declared"
        # Core owns managed report/artifact paths. Treat executor-supplied paths
        # as advisory and pin them back to the task workspace before validation.
        workspace = task.workspace or {}
        if workspace.get("report_file"):
            declared["report_file"] = str(workspace["report_file"])
        if workspace.get("artifacts_dir"):
            declared["artifacts_dir"] = str(workspace["artifacts_dir"])
        return self.finalize_task_from_agent(
            task.id,
            declared,
            execution_session=execution_session,
        )

    def finalize_task_from_agent(
        self,
        task_id: str,
        callback: dict[str, Any],
        *,
        execution_session: dict[str, Any] | None = None,
    ) -> bool:
        from .reports import write_report_files
        from .task_health import clear_task_progress

        task = self.get_task(task_id)
        task_id = task.id
        if _has_close_intent(task):
            return False
        raw_input_details = (
            dict(callback.get("input"))
            if isinstance(callback, dict) and isinstance(callback.get("input"), dict)
            else None
        )
        validation = validate_callback_payload(callback, task_id, task.steps)
        if not validation.valid or validation.callback is None:
            permission_failure = self._permission_wait_failure_for_callback_validation(
                callback,
                validation.code,
            )
            if permission_failure is not None:
                executor_run_id = (
                    str(callback.get("executor_run_id") or "")
                    if isinstance(callback, dict)
                    else ""
                )
                return self._fail_closed_permission_wait(
                    task,
                    executor_run_id,
                    failure=permission_failure,
                )
            raise ABCError(validation.code or "completion_marker_invalid", validation.message)
        callback = validation.callback
        final_state = str(callback["final_state"])
        workspace = task.workspace or {}
        validate_path_plan_workspace(workspace)
        default_report = str(workspace.get("report_file") or (self.store.task_dir(task_id) / f"{task_id}-report.md"))
        default_artifacts = str(workspace.get("artifacts_dir") or "")
        report_file = str(callback.get("report_file") or default_report)
        artifacts_dir = str(callback.get("artifacts_dir") or default_artifacts)
        from .record_management import compact_diagnostic_details

        summary = str(compact_diagnostic_details(str(callback.get("summary") or ""))).strip()
        if not summary:
            raise ABCError("invalid_agent_callback", "Agent callback summary is required")
        if final_state == "input_required":
            validated_input_details = callback.get("input")
            input_type = (
                str(validated_input_details.get("type") or "").strip().lower()
                if isinstance(validated_input_details, dict)
                else ""
            )
            if input_type == "permission":
                if execution_session is None:
                    return self._fail_closed_permission_wait(
                        task,
                        str(callback.get("executor_run_id") or ""),
                        failure=permission_wait_failure(PERMISSION_SESSION_RECEIPT_MISSING),
                    )
                try:
                    self._apply_executor_session_result(
                        task,
                        str(callback.get("executor_run_id") or ""),
                        execution_session,
                        "input_required",
                    )
                except ABCError as exc:
                    return self._fail_closed_permission_wait(
                        task,
                        str(callback.get("executor_run_id") or ""),
                        failure=self._permission_wait_failure_for_session_error(
                            task,
                            str(callback.get("executor_run_id") or ""),
                            exc,
                        ),
                    )
            elif execution_session is not None:
                self._apply_executor_session_result(
                    task,
                    str(callback.get("executor_run_id") or ""),
                    execution_session,
                    "input_required",
                )
            return self._suspend_task_for_input(
                task,
                callback,
                summary,
                raw_input_details=raw_input_details,
            )
        if not report_file:
            raise ABCError("invalid_agent_callback", "Agent callback report_file is required")
        existing_callback = (task.extensions or {}).get("agentbc.final_callback")
        if task.status in REPORTABLE_TASK_STATUSES and isinstance(existing_callback, dict):
            return False
        if _normalize_status(task.status) == "cancelled":
            now = _utc_now()
            task.extensions = dict(task.extensions or {})
            task.extensions["agentbc.late_callback_after_cancel"] = {
                "task_id": task_id,
                "final_state": final_state,
                "report_file": report_file,
                "artifacts_dir": artifacts_dir,
                "summary": summary,
                "executor_run_id": str(callback.get("executor_run_id") or ""),
                "received_at": now,
            }
            self.store.write_task(task_id, _without_none(task.to_dict()))
            self.store.append_event(
                task_id,
                {
                    "event_type": "task.late_callback_after_cancel",
                    "task_id": task_id,
                    "created_at": now,
                    "final_state": final_state,
                    "summary": summary,
                },
            )
            self._refresh_task_index()
            return False
        if _normalize_status(task.status) == "needs_recovery" and final_state != "needs_recovery":
            now = _utc_now()
            task.extensions = dict(task.extensions or {})
            task.extensions["agentbc.late_callback_after_recovery"] = {
                "task_id": task_id,
                "final_state": final_state,
                "report_file": report_file,
                "artifacts_dir": artifacts_dir,
                "summary": summary,
                "executor_run_id": str(callback.get("executor_run_id") or ""),
                "received_at": now,
            }
            self.store.write_task(task_id, _without_none(task.to_dict()))
            self.store.append_event(
                task_id,
                {
                    "event_type": "task.late_callback_after_recovery",
                    "task_id": task_id,
                    "created_at": now,
                    "final_state": final_state,
                    "summary": summary,
                },
            )
            self._refresh_task_index()
            return False
        expected_report = str(workspace.get("report_file") or "")
        expected_artifacts = str(workspace.get("artifacts_dir") or "")
        if expected_report and Path(report_file).expanduser().resolve() != Path(expected_report).expanduser().resolve():
            raise ABCError("invalid_agent_callback", "Agent callback report_file does not match task workspace")
        if expected_artifacts and artifacts_dir and Path(artifacts_dir).expanduser().resolve() != Path(expected_artifacts).expanduser().resolve():
            raise ABCError("invalid_agent_callback", "Agent callback artifacts_dir does not match task workspace")
        task.workspace = dict(workspace)
        task.workspace.setdefault("report_file", report_file)
        if artifacts_dir:
            task.workspace.setdefault("artifacts_dir", artifacts_dir)

        if execution_session is not None:
            session_state = "needs_recovery" if final_state == "needs_recovery" else "terminal"
            self._apply_executor_session_result(
                task,
                str(callback.get("executor_run_id") or ""),
                execution_session,
                session_state,
            )

        task.status = final_state
        task.updated_at = str(callback.get("finished_at") or _utc_now())
        task.steps = _finalize_steps(task.steps, callback["step_results"])
        task.extensions = dict(task.extensions or {})
        task.extensions = self._record_run_interval(task_id, task.extensions)
        task.extensions.pop("agentbc.completion_intent", None)
        task.extensions["agentbc.final_callback"] = {
            "version": callback["version"],
            "task_id": task_id,
            "final_state": final_state,
            "report_file": report_file,
            "artifacts_dir": artifacts_dir,
            "summary": summary,
            "executor_run_id": str(callback.get("executor_run_id") or ""),
            "finished_at": task.updated_at,
            "source": str(callback.get("source") or "agent_callback"),
            "outcome": str(callback.get("outcome") or "unverified"),
            "exit_code": callback.get("exit_code"),
            "step_results": callback["step_results"],
            "marker_valid": True,
            "completed_step_count": sum(
                1 for item in callback["step_results"] if item.get("status") == "done"
            ),
        }
        task.extensions = _merge_execution(task.extensions, {"internal_status": final_state})
        self.revoke_permission_grant(task.id, "task_terminal", model=task)
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        clear_task_progress(task)
        self.store.append_event(
            task_id,
            {
                "event_type": "task.finalized",
                "task_id": task_id,
                "created_at": task.updated_at,
                "final_state": final_state,
                "summary": summary,
            },
        )
        try:
            write_report_files(task_id, self.board_root)
        except (ABCError, OSError, PermissionError) as exc:
            # Report generation writes the canonical Markdown before compacting
            # Record state and refreshing indexes. A later bookkeeping failure
            # must not rewrite a confirmed normal executor exit as task failure.
            if Path(report_file).expanduser().is_file():
                self._cleanup_empty_managed_artifacts(task_id)
                try:
                    self._refresh_task_index()
                except (OSError, PermissionError):
                    pass
                return True
            self.mark_task_failed(
                task_id,
                "report_contract_missing",
                f"Agent callback received but report contract failed: {exc}",
                {"callback": task.extensions.get("agentbc.final_callback") or {}},
            )
            return True
        if not Path(report_file).expanduser().exists():
            self.mark_task_failed(
                task_id,
                "report_contract_missing",
                "Agent callback received but report file is missing after report generation",
                {"callback": task.extensions.get("agentbc.final_callback") or {}},
            )
            return True
        self._cleanup_empty_managed_artifacts(task_id)
        self._refresh_task_index()
        return True

    def revoke_permission_grant(
        self,
        task_id: str,
        code: str,
        *,
        model: TaskModel | None = None,
    ) -> bool:
        """Revoke any issued or consumed one-shot grant with a stable reason.

        Core-owned helper the Runner can call when a resume dispatch or start
        fails.  Tasks without an ``agentbc.permission_grant`` extension remain
        untouched (idempotent), and a grant already revoked for the same reason
        is returned unchanged.
        """
        current = model if model is not None else self.get_task(task_id)
        extensions = dict(current.extensions or {})
        if PERMISSION_GRANT_EXTENSION_KEY not in extensions:
            return False
        try:
            revoked = revoke_grant_contract(
                extensions[PERMISSION_GRANT_EXTENSION_KEY],
                _stable_revocation_code(code),
            )
        except ABCError:
            return False
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = revoked
        current.extensions = extensions
        if model is None:
            current.updated_at = _utc_now()
            self.store.write_task(task_id, _without_none(current.to_dict()))
        return True

    def revoke_permission_grant_for_recovery(self, task_id: str) -> None:
        """Fail-closed revocation used by explicit task recovery.

        Raises ``ABCError`` when a live grant cannot be durably revoked so the
        caller must not mark the task ready for retry/recover. Tasks without a
        grant extension and grants already revoked for any lifecycle reason are
        safe no-ops. ``OSError`` from the durable task write propagates.
        """
        current = self.get_task(task_id)
        extensions = dict(current.extensions or {})
        if PERMISSION_GRANT_EXTENSION_KEY not in extensions:
            return
        try:
            revoked = revoke_grant_contract(
                extensions[PERMISSION_GRANT_EXTENSION_KEY],
                _stable_revocation_code("task_recover"),
            )
        except ABCError as exc:
            if exc.code == "permission_grant_replay":
                return
            raise ABCError(
                "permission_grant_revocation_failed",
                f"Cannot revoke permission grant for recovery: {exc.code}",
            ) from exc
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = revoked
        current.extensions = extensions
        current.updated_at = _utc_now()
        self.store.write_task(task_id, _without_none(current.to_dict()))

    def _approval_receipt_for_response(
        self,
        task: TaskModel,
        extensions: dict[str, Any],
        request: dict[str, Any],
        input_id: str,
    ) -> dict[str, Any]:
        """Return the durable approval receipt bound to a responding input."""
        if APPROVAL_EXTENSION_KEY not in extensions:
            raise ABCError(
                "permission_input_invalid",
                "Approval input is missing the persisted agentbc.approval receipt",
            )
        session_id = str(
            (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
        )
        if not session_id:
            raise ABCError(
                "permission_input_invalid",
                "Approval input is missing the authoritative executor session",
            )
        return validate_approval_receipt(
            extensions[APPROVAL_EXTENSION_KEY],
            executor=task.assignee,
            task_id=task.id,
            session_id=session_id,
            request_id=str(request.get("request_id") or ""),
        )

    def _permission_wait_failure_for_callback_validation(
        self,
        callback: Any,
        validation_code: str,
    ) -> PermissionWaitFailure | None:
        """Project permission-specific callback validation into the recovery taxonomy."""
        if not isinstance(callback, dict):
            return None
        if str(callback.get("final_state") or "").strip().lower() != "input_required":
            return None
        input_details = callback.get("input")
        if not isinstance(input_details, dict):
            return None
        if str(input_details.get("type") or "").strip().lower() != "permission":
            return None

        if validation_code in {
            "completion_marker_input_step_missing",
            "completion_marker_permission_step_invalid",
        }:
            return permission_wait_failure(PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID)
        if validation_code == "completion_marker_permission_request_invalid":
            return permission_wait_failure(PERMISSION_REQUESTED_SCOPE_INVALID)
        if validation_code in {
            "completion_marker_permission_reason_invalid",
            "completion_marker_permission_native_flags_invalid",
            "completion_marker_steps_invalid",
            "completion_marker_step_duplicate",
            "completion_marker_step_unknown",
            "completion_marker_step_status_invalid",
        }:
            return permission_wait_failure(PERMISSION_INPUT_INVALID)
        return None

    def _permission_wait_failure_for_session_error(
        self,
        task: TaskModel,
        executor_run_id: str,
        error: ABCError,
    ) -> PermissionWaitFailure:
        """Map adapter/session validation errors without persisting their raw text."""
        error_details = error.details if isinstance(error.details, dict) else {}
        raw_errors = error_details.get("errors")
        validation_errors = (
            [str(item) for item in raw_errors if isinstance(item, str)]
            if isinstance(raw_errors, list)
            else []
        )
        if any("executor does not match" in item for item in validation_errors):
            return permission_wait_failure(
                PERMISSION_EXECUTOR_SESSION_MISMATCH,
                executor=task.assignee,
            )
        if error.code == "executor_session_receipt_invalid":
            return permission_wait_failure(
                PERMISSION_SESSION_RECEIPT_INVALID,
                receipt_state="invalid",
                executor=task.assignee,
            )
        if error.code in {"executor_session_run_mismatch", "executor_session_resume_mismatch"}:
            return permission_wait_failure(
                PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
                executor=task.assignee,
                run_id_present=bool(str(executor_run_id or "").strip()),
            )
        if error.code == "executor_session_id_mismatch":
            return permission_wait_failure(
                PERMISSION_EXECUTOR_SESSION_MISMATCH,
                executor=task.assignee,
                session_id_present=True,
            )
        if error.code == "executor_session_invalid":
            session = (task.extensions or {}).get(SESSION_EXTENSION_KEY)
            if not isinstance(session, dict):
                return permission_wait_failure(PERMISSION_RESUME_SESSION_MISSING)
            session_id_present = bool(str(session.get("session_id") or "").strip())
            if not session_id_present:
                return permission_wait_failure(
                    PERMISSION_RESUME_SESSION_MISSING,
                    session_id_present=False,
                )
            return permission_wait_failure(PERMISSION_SESSION_SNAPSHOT_INVALID)
        # The only remaining session failure at this gate is a malformed or
        # otherwise unusable receipt.  The receipt is deliberately not echoed.
        return permission_wait_failure(
            PERMISSION_SESSION_RECEIPT_INVALID,
            receipt_state="invalid",
            executor=task.assignee,
        )

    def _permission_wait_contract_failure(
        self,
        task: TaskModel,
        callback: dict[str, Any],
        executor_run_id: str,
        blocked_results: list[dict[str, Any]],
    ) -> PermissionWaitFailure | None:
        """Return one stable reason when a permission wait cannot be persisted.

        The wait is executor-neutral for codex, claude and hermes.  A permission
        request may only be persisted after a trusted runtime block. ``inherit``
        is a selection strategy and remains approval-capable; a concrete full
        base is the only non-escalatable state. The executor must ask for full,
        exactly one declared step must be blocked, the task must be the unique
        current chain head, the RunLease must match, and the authoritative
        session snapshot must bind the latest run.
        """
        from .run_lease import RunLeaseState, load_lease

        try:
            permission = permission_record_from_extensions(task.extensions)
        except ABCError as error:
            if error.code == "unsupported_permission_mode":
                raw_permission = (task.extensions or {}).get(PERMISSION_EXTENSION_KEY)
                if isinstance(raw_permission, dict):
                    raw_version = raw_permission.get("version")
                    requested = str(raw_permission.get("requested_mode") or "").strip().lower()
                    effective = str(raw_permission.get("effective_mode") or "").strip().lower()
                    if (
                        raw_version is not None
                        and raw_version != 2
                    ) or (
                        requested
                        and effective
                        and requested != effective
                    ):
                        return permission_wait_failure(
                            PERMISSION_INPUT_INVALID,
                            field="permission",
                        )
                effective = (
                    str(raw_permission.get("effective_mode") or "").strip().lower()
                    if isinstance(raw_permission, dict)
                    else ""
                )
                return permission_wait_failure(
                    PERMISSION_MODE_UNSUPPORTED,
                    effective_mode=effective,
                )
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="permission")
        except (TypeError, ValueError):
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="permission")
        if not isinstance(permission, dict):
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="permission")
        runtime_policy = permission_runtime_policy(permission)
        if runtime_policy["approval_on_block"] is not True:
            return permission_wait_failure(
                PERMISSION_MODE_UNSUPPORTED,
                effective_mode=str(permission.get("effective_mode") or ""),
            )

        raw_input = callback.get("input") if isinstance(callback, dict) else None
        if not isinstance(raw_input, dict):
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="input")
        if str(raw_input.get("type") or "").strip().lower() != "permission":
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="input_type")
        requested_permission = raw_input.get("requested_permission")
        if (
            not isinstance(requested_permission, str)
            or requested_permission.strip().lower() != "full"
        ):
            return permission_wait_failure(PERMISSION_REQUESTED_SCOPE_INVALID)
        reason = raw_input.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="reason")
        if any(
            field in raw_input
            for field in ("argv", "command", "executor_flags", "flags", "native_executor_flags")
        ):
            return permission_wait_failure(PERMISSION_INPUT_INVALID, field="native_flags")
        if len(blocked_results) != 1 or _safe_blocked_step_id(blocked_results) is None:
            return permission_wait_failure(
                PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
                blocked_step_count=len(blocked_results),
            )

        try:
            chain = self.resolve_chain(task.id)
        except ABCError:
            return permission_wait_failure(
                PERMISSION_CHAIN_HEAD_AMBIGUOUS,
                chain_state="unresolvable",
            )
        if chain.anomalies or len(chain.head_task_ids) != 1:
            return permission_wait_failure(
                PERMISSION_CHAIN_HEAD_AMBIGUOUS,
                chain_state="ambiguous",
                head_count=len(chain.head_task_ids),
            )
        if not chain.requested_is_head:
            return permission_wait_failure(
                PERMISSION_CHAIN_HEAD_STALE,
                chain_state="stale",
            )

        normalized_run_id = str(executor_run_id or "").strip()
        try:
            lease = load_lease(task.id, self.board_root)
            lease_state = (
                str(getattr(lease, "state", "") or "").strip().lower()
                if lease is not None
                else ""
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return permission_wait_failure(
                PERMISSION_RUN_LEASE_INVALID,
                lease_state="invalid",
            )
        if lease is None or lease_state not in {
            RunLeaseState.SUSPENDED,
            RunLeaseState.CLOSED,
        }:
            return permission_wait_failure(
                PERMISSION_RUN_LEASE_INVALID,
                lease_state=lease_state or "missing",
            )
        if (
            str(getattr(lease, "task_id", "") or "") != task.id
            or str(getattr(lease, "executor_id", "") or "").strip().lower()
            != str(task.assignee or "").strip().lower()
        ):
            return permission_wait_failure(
                PERMISSION_RUN_LEASE_INVALID,
                lease_state=lease_state,
                executor=task.assignee,
            )
        if str(getattr(lease, "run_id", "") or "").strip() != normalized_run_id:
            return permission_wait_failure(
                PERMISSION_RUN_LEASE_RUN_MISMATCH,
                run_id_present=bool(normalized_run_id),
            )

        session = (task.extensions or {}).get(SESSION_EXTENSION_KEY)
        if not isinstance(session, dict):
            return permission_wait_failure(PERMISSION_RESUME_SESSION_MISSING)
        session_state = str(session.get("session_state") or "").strip().lower()
        if session_state not in {"pending", "active", "input_required", "needs_recovery", "terminal"}:
            return permission_wait_failure(PERMISSION_SESSION_SNAPSHOT_INVALID)
        if session_state != "input_required":
            return permission_wait_failure(
                PERMISSION_SESSION_STATE_STALE,
                session_state=session_state,
            )
        session_id_present = bool(str(session.get("session_id") or "").strip())
        if not session_id_present:
            return permission_wait_failure(
                PERMISSION_RESUME_SESSION_MISSING,
                session_id_present=False,
            )
        session_errors = validate_session_snapshot(session, executor=task.assignee)
        if session_errors:
            return permission_wait_failure(PERMISSION_SESSION_SNAPSHOT_INVALID)
        run_ids = session.get("run_ids")
        if not isinstance(run_ids, list) or not run_ids:
            return permission_wait_failure(
                PERMISSION_RESUME_SESSION_MISSING,
                run_id_present=False,
            )
        if run_ids[-1] != normalized_run_id:
            return permission_wait_failure(
                PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH,
                run_id_present=True,
            )
        return None

    def _fail_closed_permission_wait(
        self,
        task: TaskModel,
        executor_run_id: str,
        *,
        failure: PermissionWaitFailure,
        blocked_step_id: int | None = None,
    ) -> bool:
        """Convert an unblockable permission wait into a recoverable terminal."""
        current = self.get_task(task.id)
        if _normalize_status(current.status) == "needs_recovery":
            latest_error = (current.errors or [])[-1] if current.errors else {}
            latest_details = (
                latest_error.get("details")
                if isinstance(latest_error, dict)
                and isinstance(latest_error.get("details"), dict)
                else {}
            )
            if (
                isinstance(latest_error, dict)
                and latest_error.get("code") == PERMISSION_WAIT_COMPATIBILITY_CODE
                and latest_details.get("reason_code") == failure.reason_code
            ):
                return False
        merged_details = failure.to_details(
            executor_run_id=executor_run_id,
            blocked_step_id=blocked_step_id,
        )
        return self.mark_task_needs_recovery(
            task.id,
            PERMISSION_WAIT_COMPATIBILITY_CODE,
            (
                "Permission wait cannot be created safely; task requires recovery "
                f"(reason: {failure.reason_code})"
            ),
            merged_details,
            executor_run_id=executor_run_id,
        )

    def _suspend_task_for_input(
        self,
        task: TaskModel,
        callback: dict[str, Any],
        summary: str,
        *,
        raw_input_details: dict[str, Any] | None = None,
    ) -> bool:
        """Persist one actionable wait without creating terminal artifacts."""
        from .reports import redact_secrets
        from .run_lease import suspend_lease
        from .task_health import clear_task_progress

        task_id = task.id
        extensions = dict(task.extensions or {})
        previous = extensions.get("agentbc.input")
        history = list(extensions.get("agentbc.input_history") or [])
        if isinstance(previous, dict):
            history.append(previous)

        blocked_results = [
            item
            for item in callback.get("step_results") or []
            if isinstance(item, dict) and item.get("status") == "blocked"
        ]
        blocked_step_id = _safe_blocked_step_id(blocked_results)
        validated_input_details = callback.get("input")
        input_details = (
            validated_input_details
            if isinstance(validated_input_details, dict)
            else {}
        )
        input_type = str(input_details.get("type") or "message").strip().lower() or "message"
        input_choices = (
            [dict(option) for option in input_details.get("options", []) if isinstance(option, dict)]
            if input_type == "choice" and isinstance(input_details.get("options"), list)
            else []
        )
        input_reason = str(input_details.get("reason") or "").strip()
        requested_permission = (
            str(input_details.get("requested_permission") or "").strip()
            if input_type == "permission"
            else ""
        )
        created_at = str(callback.get("finished_at") or _utc_now())
        deadline_at = (
            _parse_timestamp(created_at) + timedelta(seconds=DEFAULT_INPUT_WAIT_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        executor_run_id = str(callback.get("executor_run_id") or "")
        if input_type == "permission":
            failure = self._permission_wait_contract_failure(
                task,
                callback,
                executor_run_id,
                blocked_results,
            )
            if failure is not None:
                return self._fail_closed_permission_wait(
                    task,
                    executor_run_id,
                    failure=failure,
                    blocked_step_id=blocked_step_id,
                )
        if blocked_step_id is None:
            raise ABCError(
                "completion_marker_input_step_missing",
                "Input marker must identify one valid blocked step",
            )
        self.revoke_permission_grant(task.id, "input_superseded", model=task)
        extensions = dict(task.extensions or {})
        is_full_fallback_permission = input_type == "permission" and not (
            input_details.get("scope") == APPROVAL_SCOPE
            and bool(str(input_details.get("request_id") or "").strip())
        )
        fallback_reason_summary = ""
        fallback_summary_truncated = False
        fallback_reason_detail = ""
        if is_full_fallback_permission:
            from .approval import normalize_reason_summary_details

            # The validated callback keeps the compatibility reason bounded to
            # 240 characters.  A detail may only come from this current
            # structured callback, never from input history, logs or executor
            # session storage.  Sanitization is fail-closed for private paths,
            # argv, raw output and credential-bearing material.
            raw_reason = str(
                (raw_input_details or {}).get("reason") or input_reason
            ).strip()
            raw_detail_marker = (raw_input_details or {}).get("reason_detail")
            if raw_detail_marker is not None:
                fallback_reason_detail = sanitize_reason_detail(raw_detail_marker)
            elif raw_reason and not (
                len(raw_reason) == 240 and raw_reason.endswith("…")
            ):
                fallback_reason_detail = sanitize_reason_detail(raw_reason)

            summary_source = input_reason or raw_reason
            if summary_source and sanitize_reason_detail(summary_source):
                (
                    fallback_reason_summary,
                    fallback_summary_truncated,
                ) = normalize_reason_summary_details(
                    summary_source,
                    executor=task.assignee,
                    operation="full permission",
                )
            else:
                from .approval import core_bounded_summary_details

                (
                    fallback_reason_summary,
                    fallback_summary_truncated,
                ) = core_bounded_summary_details(
                    executor=task.assignee,
                    operation="full permission",
                )
        request: dict[str, Any] = {
            "input_id": f"input-{uuid.uuid4().hex}",
            "executor_run_id": executor_run_id,
            "blocked_step_id": blocked_step_id,
            "type": input_type,
            "summary": (
                fallback_reason_summary
                if is_full_fallback_permission
                else str(redact_secrets(summary))
            ),
            "created_at": created_at,
            "deadline_at": deadline_at,
            "status": "waiting",
        }
        if requested_permission:
            request["requested_permission"] = str(redact_secrets(requested_permission))
        if input_reason:
            request["reason"] = (
                fallback_reason_summary
                if is_full_fallback_permission
                else str(redact_secrets(input_reason))
            )
        if is_full_fallback_permission:
            request["reason_summary"] = fallback_reason_summary
            request["summary_truncated"] = fallback_summary_truncated
            if fallback_reason_detail:
                request["reason_detail"] = fallback_reason_detail
        if input_choices:
            request["options"] = [
                str(redact_secrets(str(option.get("label") or "").strip()))
                for option in input_choices
            ]
            request["option_descriptions"] = [
                str(redact_secrets(str(option.get("description") or "").strip()))
                for option in input_choices
            ]
        input_kind = str(input_details.get("kind") or "").strip()
        if input_kind:
            request["kind"] = str(redact_secrets(input_kind))
        response_protocol = str(input_details.get("response_protocol") or "").strip()
        if response_protocol:
            request["response_protocol"] = str(redact_secrets(response_protocol))

        task.status = "input_required"
        task.updated_at = created_at
        task.steps = _finalize_steps(task.steps, callback["step_results"])
        extensions = self._record_run_interval(task_id, extensions)
        extensions.pop("agentbc.completion_intent", None)
        extensions.pop("agentbc.final_callback", None)
        extensions["agentbc.input"] = request
        if history:
            extensions["agentbc.input_history"] = history
        task.extensions = _merge_execution(
            extensions,
            {
                "internal_status": "waiting",
                "lease_state": "suspended",
                "waiting_since": created_at,
            },
        )
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        suspend_lease(
            task_id,
            self.board_root,
            executor_run_id=executor_run_id,
            executor_id=task.assignee,
            work_dir=str((task.workspace or {}).get("project_root") or (task.workspace or {}).get("root") or self.board_root),
        )
        clear_task_progress(task)
        self.store.append_event(
            task_id,
            {
                "event_type": "task.input_required",
                "task_id": task_id,
                "created_at": created_at,
                "input_id": request["input_id"],
                "blocked_step_id": blocked_step_id,
                "input_type": input_type,
                "deadline_at": deadline_at,
            },
        )
        self._refresh_task_index()
        return True

    def block_task_for_resource(
        self,
        task_id: str,
        run_id: str,
        resource_exhaustion: dict[str, Any],
        *,
        execution_session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Block the first incomplete step for a confirmed resource exhaustion.

        Core entry for the ``CFG-002`` approve/deny flow. The task/run, the
        ``agentbc.resources`` snapshot, the execution session and the chain head
        are validated, done steps are preserved, and the first incomplete step
        becomes ``blocked``. ``exhaustion_count`` is atomically incremented and
        an ``agentbc.input`` request carrying ``kind=resource_limit``,
        ``response_protocol=approve_deny`` and the current/next limits is
        persisted. The executor session moves to ``input_required``, the store
        lease is released and the RunLease is suspended.

        Fail-closed: a missing blockable step, a damaged exhaustion receipt, an
        inconsistent snapshot/session or a stale chain head transition the task
        to ``needs_recovery`` instead of creating a bogus wait.
        """
        from .reports import redact_secrets
        from .run_lease import suspend_lease
        from .task_health import clear_task_progress

        task = self.get_task(task_id)
        task_id = task.id
        normalized_run_id = str(run_id or "").strip()
        failure_reason = _validate_resource_block_receipt(
            task,
            normalized_run_id,
            resource_exhaustion,
            execution_session,
        )
        if failure_reason is not None:
            return self._fail_closed_resource_block(
                task,
                failure_reason,
                normalized_run_id,
                execution_session,
            )

        chain = self.resolve_chain(task_id)
        if not chain.requested_is_head or len(chain.head_task_ids) != 1:
            return self._fail_closed_resource_block(
                task,
                "resource_block_stale_chain",
                normalized_run_id,
                execution_session,
                details={"reason": "task is not the current chain head"},
            )

        blocked_step_id = _first_incomplete_step_id(task.steps)
        if blocked_step_id is None:
            return self._fail_closed_resource_block(
                task,
                "resource_block_no_step",
                normalized_run_id,
                execution_session,
                details={"reason": "no incomplete step exists to block"},
            )

        extensions = dict(task.extensions or {})
        resources = dict(extensions[RESOURCE_EXTENSION_KEY])
        current_limit = resources["current_limit"]
        try:
            next_limit = next_resource_limit(resources, executor=task.assignee)
        except ABCError:
            return self._fail_closed_resource_block(
                task,
                "resource_block_next_limit_invalid",
                normalized_run_id,
                execution_session,
            )

        if execution_session is not None:
            self._apply_executor_session_result(
                task,
                normalized_run_id,
                execution_session,
                "input_required",
            )
        else:
            self._set_known_executor_session_state(task, "input_required")

        extensions = dict(task.extensions or {})
        resources = dict(extensions[RESOURCE_EXTENSION_KEY])
        now = _utc_now()
        deadline_at = (
            _parse_timestamp(now) + timedelta(seconds=DEFAULT_INPUT_WAIT_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        used = resource_exhaustion.get("used")
        observed_limit = resource_exhaustion.get("limit")
        limit = observed_limit if isinstance(observed_limit, (int, float)) else current_limit
        reason = _resource_block_reason(
            resource_exhaustion.get("executor") or task.assignee,
            used,
            limit,
        )
        request: dict[str, Any] = {
            "input_id": f"input-{uuid.uuid4().hex}",
            "executor_run_id": normalized_run_id,
            "blocked_step_id": blocked_step_id,
            "type": "choice",
            "kind": "resource_limit",
            "response_protocol": "approve_deny",
            "executor": task.assignee,
            "resource": resources["resource"],
            "current_limit": current_limit,
            "next_limit": next_limit,
            "used": used,
            "source": str(resource_exhaustion.get("source") or ""),
            "summary": "任务执行达到资源上限，等待用户决定是否提高资源上限并继续",
            "reason": str(redact_secrets(reason)),
            "options": ["提高预算并继续", "终止任务"],
            "option_descriptions": [
                f"将资源上限从 {current_limit} 提高至 {next_limit} 并继续同一会话",
                "终止任务，失败原因记录为资源耗尽且用户终止",
            ],
            "created_at": now,
            "deadline_at": deadline_at,
            "status": "waiting",
        }

        previous = extensions.get("agentbc.input")
        history = list(extensions.get("agentbc.input_history") or [])
        if isinstance(previous, dict):
            history.append(previous)

        task.status = "input_required"
        task.updated_at = now
        task.steps = [
            _resource_block_step(step, blocked_step_id)
            for step in task.steps
        ]
        resources["exhaustion_count"] = int(resources.get("exhaustion_count") or 0) + 1
        extensions[RESOURCE_EXTENSION_KEY] = resources
        extensions = self._record_run_interval(task_id, extensions)
        extensions.pop("agentbc.completion_intent", None)
        extensions.pop("agentbc.final_callback", None)
        extensions["agentbc.input"] = request
        if history:
            extensions["agentbc.input_history"] = history
        task.extensions = _merge_execution(
            extensions,
            {
                "internal_status": "waiting",
                "lease_state": "suspended",
                "waiting_since": now,
            },
        )
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        suspend_lease(
            task_id,
            self.board_root,
            executor_run_id=normalized_run_id,
            executor_id=task.assignee,
            work_dir=str(
                (task.workspace or {}).get("project_root")
                or (task.workspace or {}).get("root")
                or self.board_root
            ),
        )
        clear_task_progress(task)
        self.store.append_event(
            task_id,
            {
                "event_type": "task.resource_limit_blocked",
                "task_id": task_id,
                "created_at": now,
                "run_id": normalized_run_id,
                "input_id": request["input_id"],
                "blocked_step_id": blocked_step_id,
                "resource": resources["resource"],
                "current_limit": current_limit,
                "next_limit": next_limit,
                "exhaustion_count": resources["exhaustion_count"],
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task_id,
            "status": "input_required",
            "input_id": request["input_id"],
            "blocked_step_id": blocked_step_id,
            "current_limit": current_limit,
            "next_limit": next_limit,
            "exhaustion_count": resources["exhaustion_count"],
        }

    def block_task_for_approval(
        self,
        task_id: str,
        *,
        executor_run_id: str,
        session_id: str,
        request_id: str,
        request_fingerprint: str,
        executor: str,
        operation: str,
        summary: str = "",
        reason: str = "",
        reason_detail: str = "",
        blocked_step_id: int | None = None,
    ) -> dict[str, Any]:
        """Block the first incomplete step for one structured native approval request.

        Core entry for the ``PERM-103-003`` approval flow.  The caller supplies
        the executor-neutral ``agentbc.approval`` v1 binding facts; Core builds a
        bounded summary, persists the approval receipt under
        ``agentbc.approval``, and creates an ``input_required(type=permission)``
        request that only Approve / Deny can answer through
        :func:`notify_input_required`.  The optional ``reason`` is normalized to
        a Core single-line ``reason_summary`` (at most 120 characters) and the
        optional ``reason_detail`` is persisted only after redaction,
        control-character removal and a 2000-character bound.  No safe-to-full
        grant is issued and the task ``effective_mode`` is never changed.
        """
        from .approval import (
            APPROVAL_SCOPE,
            compute_request_fingerprint,
            core_bounded_summary,
            new_request_id,
        )
        from .reports import redact_secrets
        from .run_lease import suspend_lease
        from .task_health import clear_task_progress

        task = self.get_task(task_id)
        task_id = task.id
        chain = self.resolve_chain(task_id)
        if not chain.requested_is_head or len(chain.head_task_ids) != 1:
            raise ABCError(
                "approval_stale_chain",
                "Approval request must target the current unique chain head",
                chain.to_dict(),
            )
        normalized_executor = str(executor or "").strip().lower()
        if normalized_executor != str(task.assignee or "").strip().lower():
            raise ABCError(
                "approval_executor_mismatch",
                "Approval request executor does not match the task assignee",
            )
        normalized_run_id = str(executor_run_id or "").strip()
        if not normalized_run_id:
            raise ABCError(
                "approval_run_missing",
                "Approval request requires the authoritative executor run id",
            )
        session = (task.extensions or {}).get(SESSION_EXTENSION_KEY)
        session_errors = validate_session_snapshot(session, executor=task.assignee)
        if session_errors:
            raise ABCError("approval_session_invalid", "; ".join(session_errors), {"errors": session_errors})
        official_session_id = str(session.get("session_id") or "").strip()
        if not official_session_id or official_session_id != str(session_id or "").strip():
            raise ABCError(
                "approval_session_mismatch",
                "Approval request does not match the official executor session",
            )
        run_ids = list(session.get("run_ids") or [])
        if not run_ids or run_ids[-1] != normalized_run_id:
            raise ABCError(
                "approval_run_mismatch",
                "Approval request does not match the latest executor run",
            )

        clean_request_id = str(request_id or "").strip()
        if not clean_request_id:
            clean_request_id = new_request_id()
        clean_fingerprint = str(request_fingerprint or "").strip()
        if not clean_fingerprint:
            clean_fingerprint = compute_request_fingerprint(
                executor=normalized_executor,
                session_id=official_session_id,
                tool_name=operation,
            )
        clean_operation = str(operation or "").strip()
        if not clean_operation:
            raise ABCError("approval_operation_invalid", "Approval request requires an operation")
        clean_summary = str(summary or "").strip()
        if not clean_summary:
            clean_summary = core_bounded_summary(
                executor=normalized_executor,
                operation=clean_operation,
            )
        clean_reason_summary = normalize_reason_summary(
            reason,
            executor=normalized_executor,
            operation=clean_operation,
        )
        clean_reason_detail = sanitize_reason_detail(reason_detail)

        receipt = build_approval_receipt(
            task_id=task_id,
            executor_run_id=normalized_run_id,
            executor=normalized_executor,
            session_id=official_session_id,
            request_id=clean_request_id,
            request_fingerprint=clean_fingerprint,
            kind="permission",
            operation=clean_operation,
            summary=str(redact_secrets(clean_summary)),
            reason_summary=clean_reason_summary,
            reason_detail=clean_reason_detail,
            scope=APPROVAL_SCOPE,
        )

        step_id = blocked_step_id or _first_incomplete_step_id(task.steps)
        if step_id is None:
            raise ABCError(
                "approval_no_step",
                "Approval request cannot be created: no incomplete step exists",
            )

        now = _utc_now()
        deadline_at = (
            _parse_timestamp(now) + timedelta(seconds=DEFAULT_INPUT_WAIT_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        request: dict[str, Any] = {
            "input_id": f"input-{uuid.uuid4().hex}",
            "executor_run_id": normalized_run_id,
            "blocked_step_id": step_id,
            "type": "permission",
            "scope": APPROVAL_SCOPE,
            "request_id": clean_request_id,
            "request_fingerprint": clean_fingerprint,
            "operation": clean_operation,
            "summary": receipt["summary"],
            "reason_summary": clean_reason_summary,
            "summary_truncated": bool(receipt.get("summary_truncated", False)),
            "created_at": now,
            "deadline_at": deadline_at,
            "status": "waiting",
        }

        extensions = dict(task.extensions or {})
        previous = extensions.get("agentbc.input")
        history = list(extensions.get("agentbc.input_history") or [])
        if isinstance(previous, dict):
            history.append(previous)

        extensions[APPROVAL_EXTENSION_KEY] = receipt
        extensions = self._record_run_interval(task_id, extensions)
        extensions.pop("agentbc.completion_intent", None)
        extensions.pop("agentbc.final_callback", None)
        extensions["agentbc.input"] = request
        if history:
            extensions["agentbc.input_history"] = history
        task.extensions = _merge_execution(
            extensions,
            {
                "internal_status": "waiting",
                "lease_state": "suspended",
                "waiting_since": now,
            },
        )
        task.status = "input_required"
        task.updated_at = now
        task.steps = [
            _resource_block_step(step, step_id)
            for step in task.steps
        ]
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        suspend_lease(
            task_id,
            self.board_root,
            executor_run_id=normalized_run_id,
            executor_id=task.assignee,
            work_dir=str(
                (task.workspace or {}).get("project_root")
                or (task.workspace or {}).get("root")
                or self.board_root
            ),
        )
        clear_task_progress(task)
        self.store.append_event(
            task_id,
            {
                "event_type": "task.approval_required",
                "task_id": task_id,
                "created_at": now,
                "input_id": request["input_id"],
                "request_id": clean_request_id,
                "blocked_step_id": step_id,
                "scope": APPROVAL_SCOPE,
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task_id,
            "status": "input_required",
            "input_id": request["input_id"],
            "request_id": clean_request_id,
            "request_fingerprint": clean_fingerprint,
            "scope": APPROVAL_SCOPE,
            "blocked_step_id": step_id,
        }

    def cutover_preflight(self) -> dict[str, Any]:
        """Return the strict cutover gate for a supported update/preflight.

        Any old-channel ``pending`` / ``running`` / ``input_required`` /
        ``needs_recovery`` task, unconsumed one-shot grant, or legacy permission
        marker blocks the cutover with ``legacy_permission_cutover_blocked``.
        """
        return legacy_permission_cutover_blocked(self)

    def assert_legacy_cutover_clear(self) -> dict[str, Any]:
        """Raise ``legacy_permission_cutover_blocked`` when the gate is closed."""
        return assert_legacy_cutover_clear(self)

    def enter_maintenance_mode(self, reason: str = "") -> dict[str, Any]:
        """Enter cutover maintenance mode (manual bypass install)."""
        from .migration import enter_maintenance_mode as _enter_marker

        return _enter_marker(self, reason=reason)

    def exit_maintenance_mode(self) -> dict[str, Any]:
        from .migration import exit_maintenance_mode as _exit_marker

        return _exit_marker(self)

    def maintenance_mode(self) -> dict[str, Any]:
        return maintenance_mode_view(self)

    def _fail_closed_resource_block(
        self,
        task: TaskModel,
        code: str,
        run_id: str,
        execution_session: dict[str, Any] | None,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Convert an unblockable resource wait into a recoverable terminal."""
        safe_session = None
        if execution_session is not None:
            try:
                self._validated_executor_session(task, run_id, execution_session)
                safe_session = execution_session
            except ABCError:
                safe_session = None
        changed = self.mark_task_needs_recovery(
            task.id,
            code,
            "Resource exhaustion wait cannot be created safely; task requires recovery",
            details,
            executor_run_id=run_id,
            execution_session=safe_session,
        )
        return {
            "ok": False,
            "task_id": task.id,
            "status": "needs_recovery",
            "code": code,
            "message": "Resource exhaustion wait cannot be created safely; task requires recovery",
            "changed": changed,
        }

    def respond_to_input(
        self,
        task_id: str,
        input_id: str,
        *,
        response_type: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Atomically record a redacted answer and prepare the same task for resume."""
        from .reports import redact_secrets
        from .run_lease import RunLeaseState, load_lease
        from .task_health import write_task_progress

        assert_maintenance_command_allowed(self, "respond")
        task = self.get_task(task_id)
        request = (task.extensions or {}).get("agentbc.input")
        if not isinstance(request, dict):
            raise ABCError("input_not_pending", f"Task {task.id} has no input request")
        current_input_id = str(request.get("input_id") or "")
        if input_id != current_input_id:
            raise ABCError(
                "stale_input",
                f"Input {input_id} is not current for task {task.id}",
                {"task_id": task.id, "input_id": input_id, "current_input_id": current_input_id},
            )
        if str(request.get("status") or "") == "answered":
            return {
                "ok": True,
                "task_id": task.id,
                "input_id": current_input_id,
                "status": "already_answered",
                "dispatch_required": False,
            }
        if _normalize_status(task.status) != "input_required" or request.get("status") != "waiting":
            raise ABCError("input_not_pending", f"Input {input_id} is not waiting")
        chain = self.resolve_chain(task.id)
        if not chain.requested_is_head or len(chain.head_task_ids) != 1:
            raise ABCError("stale_input", f"Task {task.id} is not the current chain head", chain.to_dict())
        now = _utc_now()
        if _parse_timestamp(str(request.get("deadline_at") or "")) <= _parse_timestamp(now):
            raise ABCError("input_expired", f"Input {input_id} reached its response deadline")
        lease = load_lease(task.id, self.board_root)
        if lease is not None and lease.state not in {RunLeaseState.SUSPENDED, RunLeaseState.CLOSED}:
            raise ABCError(
                "executor_active",
                f"Task {task.id} still has an active executor",
                {"task_id": task.id, "run_id": lease.run_id, "run_lease_state": lease.state},
            )
        response_type = str(response_type or "").strip()
        if response_type not in {"message", "approve", "deny"}:
            raise ABCError("invalid_input_response", f"Unsupported response type: {response_type}")
        clean_message = str(redact_secrets(message)).strip() if response_type == "message" else response_type
        if response_type == "message" and not clean_message:
            raise ABCError("invalid_input_response", "--message requires non-empty text")

        is_permission_request = request.get("type") == "permission"
        if is_permission_request and response_type not in {"approve", "deny"}:
            raise ABCError(
                "invalid_input_response",
                "Permission requests only accept approve or deny",
            )

        is_resource_decision = is_resource_decision_request(request)
        updated_resources: dict[str, Any] | None = None
        if is_resource_decision:
            blocked_step_id = request.get("blocked_step_id")
            if not any(
                step.get("id") == blocked_step_id and step.get("status") == "blocked"
                for step in task.steps
            ):
                raise ABCError(
                    "resource_decision_invalid",
                    "Resource input does not identify the current blocked step",
                )
            updated_resources = apply_resource_input_decision(
                (task.extensions or {}).get(RESOURCE_EXTENSION_KEY),
                request,
                response_type,
                executor=task.assignee,
            )

        answered = dict(request)
        answered["status"] = "answered"
        answered["responded_at"] = now
        permission_denial_source = (
            "timeout"
            if is_permission_request
            and response_type == "deny"
            and message == PERMISSION_DIALOG_TIMEOUT_RESPONSE
            else "dialog_closed"
            if is_permission_request
            and response_type == "deny"
            and message == PERMISSION_DIALOG_CLOSED_RESPONSE
            else "user"
        )
        answered["response"] = {
            "type": response_type,
            "summary": clean_message,
            **(
                {"source": permission_denial_source}
                if is_permission_request and response_type == "deny"
                else {}
            ),
        }
        extensions = dict(task.extensions or {})
        extensions["agentbc.input"] = answered
        if updated_resources is not None:
            extensions[RESOURCE_EXTENSION_KEY] = updated_resources

        is_approval_request = (
            is_permission_request
            and request.get("scope") == APPROVAL_SCOPE
            and bool(str(request.get("request_id") or "").strip())
        )
        if is_approval_request:
            receipt = self._approval_receipt_for_response(
                task,
                extensions,
                request,
                current_input_id,
            )
            approval_source = permission_denial_source if response_type == "deny" else "user"
            updated_receipt = record_approval_decision(
                receipt,
                response_type,
                source=approval_source,
                decided_at=now,
                executor=task.assignee,
                task_id=task.id,
                session_id=str(
                    (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
                ),
                request_id=str(request.get("request_id") or ""),
            )
            extensions[APPROVAL_EXTENSION_KEY] = updated_receipt
            blocked_step_id = request.get("blocked_step_id")
            if not any(
                step.get("id") == blocked_step_id and step.get("status") == "blocked"
                for step in task.steps
            ):
                raise ABCError(
                    "permission_input_invalid",
                    "Approval input does not identify the current blocked step",
                )
            task.steps = [
                {**step, "status": "pending"}
                if step.get("id") == blocked_step_id and step.get("status") == "blocked"
                else dict(step)
                for step in task.steps
            ]
            task.status = "running"
            task.updated_at = now
            task.extensions = _merge_execution(
                extensions,
                {
                    "internal_status": "resuming",
                    "lease_state": "suspended",
                    "resuming_at": now,
                },
            )
            self.store.write_task(task.id, _without_none(task.to_dict()))
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.input_answered",
                    "task_id": task.id,
                    "input_id": current_input_id,
                    "response_type": response_type,
                    "response_source": approval_source,
                    "approval_scope": APPROVAL_SCOPE,
                    "created_at": now,
                },
            )
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.approval_decided",
                    "task_id": task.id,
                    "request_id": str(request.get("request_id") or ""),
                    "decision": response_type,
                    "decision_source": approval_source,
                    "scope": APPROVAL_SCOPE,
                    "created_at": now,
                },
            )
            write_task_progress(
                task,
                state="resuming",
                message="approval decision recorded; resuming the same task session",
                source="runner",
            )
            self._refresh_task_index()
            return {
                "ok": True,
                "task_id": task.id,
                "input_id": current_input_id,
                "request_id": str(request.get("request_id") or ""),
                "status": "resuming",
                "dispatch_required": True,
                "approval_decision": response_type,
                "approval_source": approval_source,
                "same_session": True,
            }

        if is_resource_decision and response_type == "deny":
            failure_code = {
                "claude": "budget_exhausted_user_terminated",
                "hermes": "iteration_exhausted_user_terminated",
            }.get(task.assignee)
            if failure_code is None:
                raise ABCError(
                    "resource_decision_invalid",
                    f"Resource decisions are unsupported for executor: {task.assignee}",
                )
            failure_message = (
                "User terminated the task after the executor resource limit was exhausted"
            )
            task.extensions = extensions
            task.updated_at = now
            self._mark_task_failed_model(
                task,
                failure_code,
                failure_message,
                {
                    "failure": {
                        "kind": failure_code,
                        "layer": "resource_limit",
                        "message": failure_message,
                        "retryable": False,
                    },
                    "input_id": current_input_id,
                    "executor": task.assignee,
                    "resource": updated_resources["resource"],
                    "current_limit": updated_resources["current_limit"],
                },
            )
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.input_answered",
                    "task_id": task.id,
                    "input_id": current_input_id,
                    "response_type": response_type,
                    "created_at": now,
                },
            )
            return {
                "ok": True,
                "task_id": task.id,
                "input_id": current_input_id,
                "status": "failed",
                "dispatch_required": False,
                "resource_terminated": True,
                "failure": {
                    "kind": failure_code,
                    "layer": "resource_limit",
                    "message": failure_message,
                    "retryable": False,
                },
            }

        if is_permission_request and response_type == "deny":
            timed_out = permission_denial_source == "timeout"
            failure_message = (
                "Permission request timed out and was automatically denied"
                if timed_out
                else "User denied the requested full permission"
            )
            failure_code = (
                "permission_denied_by_timeout"
                if timed_out
                else "permission_denied_by_user"
            )
            task.extensions = extensions
            task.updated_at = now
            self._mark_task_failed_model(
                task,
                failure_code,
                failure_message,
                {
                    "failure": {
                        "kind": failure_code,
                        "layer": "permission",
                        "message": failure_message,
                        "retryable": False,
                    },
                    "input_id": current_input_id,
                    "executor": task.assignee,
                    "requested_permission": request.get("requested_permission", ""),
                },
            )
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.input_answered",
                    "task_id": task.id,
                    "input_id": current_input_id,
                    "response_type": response_type,
                    "response_source": permission_denial_source,
                    "created_at": now,
                },
            )
            return {
                "ok": True,
                "task_id": task.id,
                "input_id": current_input_id,
                "status": "failed",
                "dispatch_required": False,
                "permission_denied": True,
                "failure": {
                    "kind": failure_code,
                    "layer": "permission",
                    "message": failure_message,
                    "retryable": False,
                },
            }

        if is_resource_decision:
            blocked_step_id = request.get("blocked_step_id")
            task.steps = [
                {**step, "status": "pending"}
                if step.get("id") == blocked_step_id and step.get("status") == "blocked"
                else dict(step)
                for step in task.steps
            ]
            session = extensions.get(SESSION_EXTENSION_KEY)
            if isinstance(session, dict) and str(session.get("session_id") or "").strip():
                updated_session = dict(session)
                updated_session["session_state"] = "active"
                session_errors = validate_session_snapshot(
                    updated_session,
                    executor=task.assignee,
                )
                if session_errors:
                    raise ABCError(
                        "resource_decision_invalid",
                        "; ".join(session_errors),
                        {"errors": session_errors},
                    )
                extensions[SESSION_EXTENSION_KEY] = updated_session
        elif is_permission_request:
            blocked_step_id = request.get("blocked_step_id")
            if not any(
                step.get("id") == blocked_step_id and step.get("status") == "blocked"
                for step in task.steps
            ):
                raise ABCError(
                    "permission_input_invalid",
                    "Permission input does not identify the current blocked step",
                )
            task.steps = [
                {**step, "status": "pending"}
                if step.get("id") == blocked_step_id and step.get("status") == "blocked"
                else dict(step)
                for step in task.steps
            ]
            session = extensions.get(SESSION_EXTENSION_KEY)
            session_id = (
                str(session.get("session_id") or "").strip()
                if isinstance(session, dict)
                else ""
            )
            if not session_id:
                raise ABCError(
                    "permission_input_invalid",
                    "Permission input is missing the authoritative executor session",
                )
            base_permission = permission_record_from_extensions(extensions)
            runtime_policy = permission_runtime_policy(base_permission)
            if runtime_policy["approval_on_block"] is not True:
                raise ABCError(
                    "permission_input_invalid",
                    "Permission input cannot escalate an already-full permission base",
                )
            extensions[PERMISSION_GRANT_EXTENSION_KEY] = build_permission_grant(
                executor=task.assignee,
                task_id=task.id,
                input_id=current_input_id,
                session_id=session_id,
                source_run_id=str(request.get("executor_run_id") or ""),
                base_mode=str(runtime_policy["base_mode"]),
                issued_at=now,
            )
        else:
            task.steps = [
                {**step, "status": "pending"} if step.get("status") == "blocked" else dict(step)
                for step in task.steps
            ]
        task.status = "running"
        task.updated_at = now
        task.extensions = _merge_execution(
            extensions,
            {
                "internal_status": "resuming",
                "lease_state": "suspended",
                "resuming_at": now,
            },
        )
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "task.input_answered",
                "task_id": task.id,
                "input_id": current_input_id,
                "response_type": response_type,
                "created_at": now,
            },
        )
        if is_resource_decision:
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.resource_limit_increased",
                    "task_id": task.id,
                    "input_id": current_input_id,
                    "executor": task.assignee,
                    "resource": updated_resources["resource"],
                    "previous_limit": request.get("current_limit"),
                    "current_limit": updated_resources["current_limit"],
                    "exhaustion_count": updated_resources["exhaustion_count"],
                    "created_at": now,
                },
            )
        write_task_progress(task, state="resuming", message="user response received; resuming task", source="runner")
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task.id,
            "input_id": current_input_id,
            "status": "resuming",
            "dispatch_required": True,
        }

    def expire_waiting_inputs(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Runner maintenance hook for durable 24-hour input deadlines."""
        current_at = _parse_timestamp(now or _utc_now())
        expired: list[dict[str, Any]] = []
        for task in self.list_tasks(status="input_required"):
            request = (task.extensions or {}).get("agentbc.input")
            if not isinstance(request, dict) or request.get("status") != "waiting":
                continue
            deadline_at = _parse_timestamp(str(request.get("deadline_at") or ""))
            if deadline_at > current_at:
                continue
            if request.get("type") == "permission":
                expired_at = now or _utc_now()
                answered_request = dict(request)
                answered_request["status"] = "answered"
                answered_request["responded_at"] = expired_at
                answered_request["response"] = {
                    "type": "deny",
                    "summary": "deny",
                    "source": "timeout",
                }
                task.extensions = dict(task.extensions or {})
                task.extensions["agentbc.input"] = answered_request
                task.updated_at = expired_at
                is_approval_request = (
                    request.get("scope") == APPROVAL_SCOPE
                    and bool(str(request.get("request_id") or "").strip())
                )
                if is_approval_request:
                    # Approval-based timeout auto-denies on the same native
                    # request, records the decision source, and never issues a
                    # safe-to-full grant.  The task is moved to needs_recovery so
                    # the official session is never silently lost.
                    extensions = dict(task.extensions)
                    try:
                        receipt = self._approval_receipt_for_response(
                            task,
                            extensions,
                            request,
                            str(request.get("input_id") or ""),
                        )
                    except ABCError:
                        receipt = None
                    if receipt is not None:
                        updated_receipt = record_approval_decision(
                            receipt,
                            "deny",
                            source="timeout",
                            decided_at=expired_at,
                            executor=task.assignee,
                            task_id=task.id,
                            session_id=str(
                                (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
                            ),
                            request_id=str(request.get("request_id") or ""),
                        )
                        extensions[APPROVAL_EXTENSION_KEY] = updated_receipt
                        task.extensions = extensions
                        self.store.write_task(task.id, _without_none(task.to_dict()))
                    changed = self.mark_task_needs_recovery(
                        task.id,
                        "approval_denied_by_timeout",
                        "Approval request timed out and was automatically denied",
                        {
                            "input_id": request.get("input_id", ""),
                            "request_id": request.get("request_id", ""),
                            "executor": task.assignee,
                            "response_source": "timeout",
                        },
                    )
                    if changed:
                        expired.append(
                            {
                                "task_id": task.id,
                                "input_id": request.get("input_id", ""),
                            }
                        )
                    continue
                failure_code = "permission_denied_by_timeout"
                failure_message = "Permission request timed out and was automatically denied"
                self._mark_task_failed_model(
                    task,
                    failure_code,
                    failure_message,
                    {
                        "failure": {
                            "kind": failure_code,
                            "layer": "permission",
                            "message": failure_message,
                            "retryable": False,
                        },
                        "input_id": request.get("input_id", ""),
                        "executor": task.assignee,
                        "requested_permission": request.get("requested_permission", ""),
                        "response_source": "timeout",
                    },
                )
                self.store.append_event(
                    task.id,
                    {
                        "event_type": "task.input_answered",
                        "task_id": task.id,
                        "input_id": request.get("input_id", ""),
                        "response_type": "deny",
                        "response_source": "timeout",
                        "created_at": expired_at,
                    },
                )
                expired.append(
                    {
                        "task_id": task.id,
                        "input_id": request.get("input_id", ""),
                    }
                )
                continue
            expired_request = dict(request)
            expired_request["status"] = "expired"
            expired_request["expired_at"] = now or _utc_now()
            task.extensions = dict(task.extensions or {})
            task.extensions["agentbc.input"] = expired_request
            self.store.write_task(task.id, _without_none(task.to_dict()))
            self.revoke_permission_grant(task.id, "input_expired")
            changed = self.mark_task_needs_recovery(
                task.id,
                "input_deadline_expired",
                f"Input {request.get('input_id', '')} was not answered before {request.get('deadline_at', '')}",
                {
                    "input_id": request.get("input_id", ""),
                    "blocked_step_id": request.get("blocked_step_id"),
                    "deadline_at": request.get("deadline_at", ""),
                },
            )
            if changed:
                expired.append(
                    {
                        "task_id": task.id,
                        "input_id": request.get("input_id", ""),
                    }
                )
        return expired

    def mark_task_failed(
        self,
        task_id: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        executor_run_id: str = "",
        execution_session: dict[str, Any] | None = None,
    ) -> bool:
        """Mark a started task whose executor termination could not be confirmed."""
        task = self.get_task(task_id)
        return self._mark_task_failed_model(
            task,
            code,
            message,
            details,
            executor_run_id=executor_run_id,
            execution_session=execution_session,
        )

    def _mark_task_failed_model(
        self,
        task: TaskModel,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        executor_run_id: str = "",
        execution_session: dict[str, Any] | None = None,
    ) -> bool:
        """Fail a caller-validated task model with one atomic task-state write."""
        from .task_health import clear_task_progress

        task_id = task.id
        if _has_close_intent(task):
            return False
        if _normalize_status(task.status) == "failed":
            return False
        now = _utc_now()
        from .record_management import compact_diagnostic_details

        compact_message = str(compact_diagnostic_details(message))
        compact_details = compact_diagnostic_details(details or {})
        task.status = "failed"
        task.updated_at = now
        task.errors = list(task.errors or [])
        task.errors.append(
            {
                "code": code,
                "message": compact_message,
                "details": compact_details,
                "created_at": now,
            }
        )
        if execution_session is not None:
            self._apply_executor_session_result(
                task,
                executor_run_id,
                execution_session,
                "terminal",
            )
        else:
            self._set_known_executor_session_state(task, "terminal")
        _supersede_final_callback(task, "failed", compact_message)
        execution_updates = {"internal_status": "failed"}
        from .run_lease import RunLeaseState, close_lease, load_lease

        run_lease = load_lease(task_id, self.board_root)
        if run_lease is not None and run_lease.state == RunLeaseState.SUSPENDED:
            close_lease(run_lease, self.board_root)
            execution_updates["lease_state"] = RunLeaseState.CLOSED
        task.extensions = self._record_run_interval(task_id, dict(task.extensions or {}))
        task.extensions = _merge_execution(task.extensions, execution_updates)
        self.revoke_permission_grant(task.id, code, model=task)
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        clear_task_progress(task)
        self.store.append_event(
            task_id,
            {
                "event_type": "task.failed",
                "task_id": task_id,
                "created_at": now,
                "error": {"code": code, "message": compact_message},
            },
        )
        self._refresh_task_index()
        self._sync_terminal_report(task_id)
        return True

    def mark_task_needs_recovery(
        self,
        task_id: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        executor_run_id: str = "",
        execution_session: dict[str, Any] | None = None,
    ) -> bool:
        from .task_health import clear_task_progress

        task = self.get_task(task_id)
        task_id = task.id
        if _has_close_intent(task):
            return False
        normalized = _normalize_status(task.status)
        if normalized == "cancelled":
            now = _utc_now()
            self.store.append_event(
                task_id,
                {
                    "event_type": "task.late_recovery_after_cancel",
                    "task_id": task_id,
                    "created_at": now,
                    "error": {"code": code, "message": message, "details": details or {}},
                },
            )
            return False
        now = _utc_now()
        task.status = "needs_recovery"
        task.updated_at = now
        task.errors = list(task.errors or [])
        from .record_management import compact_diagnostic_details

        compact_message = str(compact_diagnostic_details(message))
        compact_details = compact_diagnostic_details(details or {})
        task.errors.append(
            {
                "code": code,
                "message": compact_message,
                "details": compact_details,
                "created_at": now,
            }
        )
        if execution_session is not None:
            self._apply_executor_session_result(
                task,
                executor_run_id,
                execution_session,
                "needs_recovery",
            )
        else:
            self._set_known_executor_session_state(task, "needs_recovery")
        _supersede_final_callback(task, "needs_recovery", compact_message)
        execution_updates = {"internal_status": "needs_recovery"}
        from .run_lease import RunLeaseState, close_lease, load_lease

        run_lease = load_lease(task_id, self.board_root)
        if run_lease is not None and run_lease.state == RunLeaseState.SUSPENDED:
            close_lease(run_lease, self.board_root)
            execution_updates["lease_state"] = RunLeaseState.CLOSED
        task.extensions = self._record_run_interval(task_id, dict(task.extensions or {}))
        task.extensions = _merge_execution(task.extensions, execution_updates)
        self.revoke_permission_grant(task.id, code, model=task)
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        clear_task_progress(task)
        self.store.append_event(
            task_id,
            {
                "event_type": "task.recovery_required",
                "task_id": task_id,
                "created_at": now,
                "error": {"code": code, "message": compact_message},
            },
        )
        self._refresh_task_index()
        self._sync_terminal_report(task_id)
        return True

    def requeue_task(self, task_id: str) -> TaskModel:
        from .run_lease import RunLeaseState, load_lease
        from .task_health import cleanup_task_report_records

        task = self.get_task(task_id)
        if _normalize_status(task.status) not in {"needs_recovery", "failed", "cancelled"}:
            raise ABCError("invalid_transition", f"Cannot requeue task in state: {task.status}")
        lease = load_lease(task_id, self.board_root)
        if lease is not None and lease.state != RunLeaseState.CLOSED:
            raise ABCError("task_leased", f"Cannot requeue task with active run lease: {task_id}")
        now = _utc_now()
        cleanup_task_report_records(task)
        task.status = "pending"
        task.updated_at = now
        task.extensions = _merge_execution(
            task.extensions,
            {"internal_status": "pending", "requeued_at": now},
        )
        self.revoke_permission_grant(task.id, "task_retry", model=task)
        if not bool((task.workspace or {}).get("customer_dir")):
            Path(str(task.workspace["artifact_root"])).expanduser().mkdir(parents=True, exist_ok=True)
        Path(task.workspace["task_file"]).parent.mkdir(parents=True, exist_ok=True)
        _write_task_requirements(task, Path(task.workspace["task_file"]))
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(
            task_id,
            {"event_type": "task.requeued", "task_id": task_id, "created_at": now},
        )
        self._refresh_task_index()
        return task

    def update_execution_metadata(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        task = self.get_task(task_id)
        task.extensions = _merge_execution(task.extensions, updates)
        task.updated_at = _utc_now()
        self.store.write_task(task_id, _without_none(task.to_dict()))
        return dict((task.extensions.get("agentbc.execution") or {}))

    def pause_task(self, task_id: str, reason: str | None = None) -> None:
        task = self.get_task(task_id)
        if _normalize_status(task.status) != "running":
            raise ABCError("invalid_intervention", f"Cannot pause task in state: {task.status}")
        task.intervention = dict(task.intervention or {})
        task.intervention.update({"paused": True, "pause_reason": reason})
        task.updated_at = _utc_now()
        task.extensions = _merge_execution(task.extensions, {"internal_status": "paused"})
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(task_id, {"event_type": "paused", "task_id": task_id, "created_at": task.updated_at})
        self._append_intervention(task_id, "pause", task.updated_at, message=reason)

    def resume_task(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if not (task.intervention or {}).get("paused"):
            raise ABCError("invalid_intervention", f"Cannot resume task in state: {task.status}")
        task.intervention = dict(task.intervention or {})
        task.intervention.update({"paused": False, "pause_reason": None})
        task.updated_at = _utc_now()
        task.extensions = _merge_execution(task.extensions, {"internal_status": "running"})
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(task_id, {"event_type": "resumed", "task_id": task_id, "created_at": task.updated_at})
        self._append_intervention(task_id, "resume", task.updated_at)

    def cancel_task(self, task_id: str) -> None:
        from .run_lease import RunLeaseState, close_lease, load_lease
        from .task_health import cleanup_cancelled_task_files, clear_task_progress

        task = self.get_task(task_id)
        task_id = task.id
        if task.status in {"completed", "cancelled", "rejected"}:
            raise ABCError("invalid_intervention", f"Cannot cancel terminal task: {task.status}")
        validate_transition(task.status, "cancelled")
        task.status = "cancelled"
        task.updated_at = _utc_now()
        extensions = self._record_run_interval(task_id, dict(task.extensions or {}))
        request = extensions.get("agentbc.input")
        if isinstance(request, dict) and request.get("status") == "waiting":
            cancelled_request = dict(request)
            cancelled_request["status"] = "cancelled"
            cancelled_request["cancelled_at"] = task.updated_at
            extensions["agentbc.input"] = cancelled_request
        task.extensions = extensions
        self._set_known_executor_session_state(task, "terminal")
        execution_updates = {
            "internal_status": "cancelled",
            "lease_state": RunLeaseState.CLOSED,
        }
        lease = load_lease(task_id, self.board_root)
        if lease is not None and lease.state != RunLeaseState.CLOSED:
            close_lease(lease, self.board_root)
        task.extensions = _merge_execution(task.extensions, execution_updates)
        self.revoke_permission_grant(task.id, "task_cancelled", model=task)
        self._release_lease(task_id)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        cleanup_cancelled_task_files(task)
        clear_task_progress(task, remove_log=True)
        self.store.append_event(task_id, {"event_type": "cancelled", "task_id": task_id, "created_at": task.updated_at})
        self._append_intervention(task_id, "cancel", task.updated_at)
        self._refresh_task_index()
        self._sync_terminal_report(task_id)

    def plan_task_delete(self, task_code: str) -> dict[str, Any]:
        """Build a zero-write ownership plan for deleting one terminal chain."""
        try:
            normalized_code, requested_iteration = split_task_ref(task_code)
        except ValueError as exc:
            raise ABCError(
                "task_delete_requires_chain_code",
                f"task delete requires a task code, not an iteration id: {task_code}",
            ) from exc
        if requested_iteration is not None:
            raise ABCError(
                "task_delete_requires_chain_code",
                f"task delete requires a task code, not an iteration id: {task_code}",
                {"task_code": normalized_code, "iteration": requested_iteration},
            )

        pending = self.store.pending_chain_deletion(normalized_code)
        if pending is not None:
            plan = pending.get("plan") if isinstance(pending.get("plan"), dict) else {}
            return {
                **plan,
                "status": "pending_delete",
                "deletion_id": pending.get("deletion_id"),
                "transaction_state": pending.get("state"),
            }

        tasks = sorted(
            (task for task in self.list_tasks() if _task_code_for(task) == normalized_code),
            key=lambda task: task_iteration(task.id) or 0,
        )
        if not tasks:
            receipt = self.store.latest_deletion_receipt(normalized_code)
            if receipt is not None:
                return {
                    "status": "already_deleted",
                    "task_code": normalized_code,
                    "task_ids": list(receipt.get("task_ids") or []),
                    "delete_objects": [],
                    "preserve_objects": [],
                    "targets": [],
                    "receipt": receipt,
                }
            raise ABCError(
                "task_not_found",
                f"Task chain not found: {normalized_code}",
                {"task_code": normalized_code},
            )

        ineligible = [
            {"task_id": task.id, "status": _normalize_status(task.status)}
            for task in tasks
            if _normalize_status(task.status) not in DELETE_ELIGIBLE_STATUSES
        ]
        if ineligible:
            raise ABCError(
                "task_delete_ineligible",
                f"Task chain {normalized_code} is not deletion-eligible",
                {
                    "task_code": normalized_code,
                    "blocked_iterations": ineligible,
                    "allowed_statuses": sorted(DELETE_ELIGIBLE_STATUSES),
                },
            )

        ownership = _task_chain_delete_ownership(self.board_root, normalized_code, tasks)
        generation_text = "|".join(f"{task.id}:{task.created_at}" for task in tasks)
        return {
            "status": "ready",
            "task_code": normalized_code,
            "task_ids": [task.id for task in tasks],
            "generation": hashlib.sha256(generation_text.encode("utf-8")).hexdigest(),
            "delete_objects": ownership["delete_objects"],
            "preserve_objects": ownership["preserve_objects"],
            "targets": ownership["targets"],
        }

    def delete_task_chain(
        self,
        task_code: str,
        *,
        dry_run: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Delete a terminal chain with a durable reservation and commit boundary."""
        if dry_run == confirmed:
            raise ABCError(
                "task_delete_confirmation_required",
                "Choose exactly one deletion mode: dry-run or confirmed",
            )
        plan = self.plan_task_delete(task_code)
        if plan["status"] == "already_deleted":
            if confirmed:
                deletion_id = str((plan.get("receipt") or {}).get("deletion_id") or "")
                if deletion_id:
                    try:
                        purge_complete = self.store.purge_committed_deletion(deletion_id)
                    except ABCError as exc:
                        if exc.code != "task_delete_not_found":
                            raise
                        purge_complete = True
                    return {**plan, "purge_complete": purge_complete}
            return plan
        if dry_run:
            return {**plan, "status": "dry_run"}

        if plan["status"] == "pending_delete":
            deletion_id = str(plan.get("deletion_id") or "")
        else:
            reservation = self.store.reserve_chain_deletion(plan)
            deletion_id = str(reservation["deletion_id"])
        self.store.stage_chain_deletion(deletion_id)
        try:
            self._refresh_task_index()
        except Exception as exc:
            rollback_complete = self.store.rollback_chain_deletion(deletion_id)
            if rollback_complete:
                try:
                    self._refresh_task_index()
                except Exception:
                    pass
            raise ABCError(
                "task_delete_index_error",
                f"Could not commit task index deletion: {exc}",
                {"rollback_complete": rollback_complete},
            ) from exc
        receipt = self.store.finalize_chain_deletion(deletion_id)
        purge_complete = self.store.purge_committed_deletion(deletion_id)
        return {
            **plan,
            "status": "deleted",
            "deletion_id": deletion_id,
            "receipt": receipt,
            "released_task_code": True,
            "purge_complete": purge_complete,
        }

    def plan_task_close(self, task_ref: str) -> dict[str, Any]:
        """Validate close against the current active chain head without changing state."""
        try:
            task_code, requested_iteration = split_task_ref(task_ref)
        except ValueError as exc:
            raise ABCError("task_not_found", f"Task not found: {task_ref}") from exc
        tasks = sorted(
            (task for task in self.list_tasks() if _task_code_for(task) == task_code),
            key=lambda task: task_iteration(task.id),
        )
        if not tasks:
            raise ABCError("task_not_found", f"Task not found: {task_ref}", {"task_id": task_ref})
        head = tasks[-1]
        if requested_iteration is not None and head.id != format_task_id(task_code, requested_iteration):
            raise ABCError(
                "invalid_intervention",
                f"task close only supports the current chain head: {head.id}",
                {"requested_task_id": task_ref, "current_head_task_id": head.id},
            )
        status = _normalize_status(head.status)
        terminal_statuses = set(TASK_TERMINAL_STATES) | {"cancelled", "rejected"}
        if status in terminal_statuses:
            raise ABCError(
                "invalid_intervention",
                f"Cannot close terminal task: {head.id} ({status})",
                {"task_id": head.id, "status": status},
            )
        if status not in RUNNING_TASK_STATUSES | {"pending"}:
            raise ABCError(
                "invalid_intervention",
                f"task close only supports a queued or active task: {head.id} ({status})",
                {"task_id": head.id, "status": status},
            )
        return {
            "task_code": task_code,
            "task_id": head.id,
            "task": head,
            "iteration": str(head.workspace.get("iteration") or f"{task_iteration(head.id):03d}"),
            "chain_length": len(tasks),
            "is_chain_iteration": len(tasks) > 1,
            "prior_task_ids": [task.id for task in tasks[:-1]],
            "record_dir": str(head.workspace.get("internal_task_dir") or self.store.task_dir(head.id)),
            "task_file": str(head.workspace.get("task_file") or ""),
            "report_file": str(head.workspace.get("report_file") or ""),
            "artifact_root": str(head.workspace.get("artifact_root") or head.workspace.get("artifacts_dir") or ""),
        }

    def close_active_task(self, task_ref: str, *, confirmed: bool = False) -> dict[str, Any]:
        """Remove the active head; only a root iteration releases its task code."""
        reservation = self.reserve_task_close(task_ref, confirmed=confirmed)
        return self.commit_task_close(reservation["task_id"], reservation["close_token"])

    def reserve_task_close(self, task_ref: str, *, confirmed: bool = False) -> dict[str, Any]:
        """Reserve an active task close before Runner cancellation can race terminal state."""
        plan = self.plan_task_close(task_ref)
        if plan["is_chain_iteration"] and not confirmed:
            raise ABCError(
                "close_confirmation_required",
                f"Closing {plan['task_id']} requires explicit artifact-risk confirmation",
                {"task_id": plan["task_id"], "artifact_root": plan["artifact_root"]},
            )
        task = plan["task"]
        token = uuid.uuid4().hex
        task.extensions = dict(task.extensions or {})
        task.extensions["agentbc.close_intent"] = {
            "token": token,
            "requested_at": _utc_now(),
            "task_id": task.id,
            "task_code": plan["task_code"],
            "is_chain_iteration": bool(plan["is_chain_iteration"]),
        }
        self.store.write_task(task.id, _without_none(task.to_dict()))
        return {**plan, "task": task, "close_token": token}

    def abort_task_close(self, task_id: str, token: str) -> bool:
        task = self.get_task(task_id)
        intent = (task.extensions or {}).get("agentbc.close_intent")
        if not isinstance(intent, dict) or str(intent.get("token") or "") != token:
            return False
        task.extensions = dict(task.extensions or {})
        task.extensions.pop("agentbc.close_intent", None)
        self.store.write_task(task.id, _without_none(task.to_dict()))
        return True

    def commit_task_close(self, task_id: str, token: str) -> dict[str, Any]:
        """Commit a reserved close even if process cancellation has raced task state."""
        from .task_health import cleanup_cancelled_task_files, cleanup_task_report_records

        task = self.get_task(task_id)
        intent = (task.extensions or {}).get("agentbc.close_intent")
        if not isinstance(intent, dict) or str(intent.get("token") or "") != token:
            raise ABCError("invalid_intervention", f"Task close reservation expired: {task.id}")
        task_code = str(intent.get("task_code") or _task_code_for(task))
        is_chain_iteration = bool(intent.get("is_chain_iteration"))
        workspace = task.workspace or {}
        result = {
            "task_code": task_code,
            "task_id": task.id,
            "iteration": str(workspace.get("iteration") or f"{task_iteration(task.id):03d}"),
            "is_chain_iteration": is_chain_iteration,
            "record_dir": str(workspace.get("internal_task_dir") or self.store.task_dir(task.id)),
            "task_file": str(workspace.get("task_file") or ""),
            "report_file": str(workspace.get("report_file") or ""),
            "artifact_root": str(workspace.get("artifact_root") or workspace.get("artifacts_dir") or ""),
        }
        self._release_lease(task.id)
        if is_chain_iteration:
            cleanup_task_report_records(task)
            deleted = self.store.delete_iteration(task.id)
        else:
            cleanup_cancelled_task_files(task)
            deleted = self.store.delete_chain(task_code)
        if not deleted:
            raise ABCError("task_not_found", f"Task not found: {task.id}", {"task_id": task.id})
        self._refresh_task_index()
        result["released_task_code"] = not is_chain_iteration
        return result

    def _sync_terminal_report(self, task_id: str) -> None:
        try:
            from .reports import write_report_files

            write_report_files(task_id, self.board_root)
        except (ABCError, OSError, PermissionError):
            pass
        self._cleanup_empty_managed_artifacts(task_id)

    def _cleanup_empty_managed_artifacts(self, task_id: str) -> None:
        try:
            from .task_health import cleanup_empty_managed_task_artifacts

            cleanup_empty_managed_task_artifacts(self.get_task(task_id))
        except (ABCError, OSError, PermissionError):
            pass

    def correct_step(self, task_id: str, step_id: int, message: str) -> None:
        task = self.get_task(task_id)
        _require_step(task, step_id)
        if not message.strip():
            raise ABCError("invalid_intervention", "Correction message is required")
        now = _utc_now()
        correction_id = f"I-{len(self.store.read_interventions(task_id)) + 1:03d}"
        task.intervention = dict(task.intervention or {})
        task.intervention["latest_correction_id"] = correction_id
        task.updated_at = now
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self._append_intervention(task_id, "correct", now, intervention_type="correction", step_id=step_id, message=message, correction_id=correction_id)

    def retry_step(self, task_id: str, step_id: int) -> None:
        task = self.get_task(task_id)
        if _normalize_status(task.status) not in {"running", "input_required", "needs_recovery"}:
            raise ABCError("invalid_intervention", f"Cannot retry step in state: {task.status}")
        _require_step(task, step_id)
        task.steps = [_retry_step(step, step_id) for step in task.steps]
        task.updated_at = _utc_now()
        self.revoke_permission_grant(task.id, "task_retry", model=task)
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(task_id, {"event_type": "step_retry", "task_id": task_id, "step_id": step_id, "created_at": task.updated_at})
        self._append_intervention(task_id, "retry", task.updated_at, step_id=step_id)

    def reassign_task(self, task_id: str, new_executor: str) -> None:
        task = self.get_task(task_id)
        new_executor = _normalize_executor_ref(
            new_executor,
            field="new executor",
            empty_code="invalid_intervention",
            empty_message="New executor is required",
        )
        if _normalize_status(task.status) == "running" and not (task.intervention or {}).get("paused"):
            raise ABCError("task_leased", f"Cannot reassign working task: {task_id}")
        if _normalize_status(task.status) not in {"running", "needs_recovery", "cancelled"}:
            raise ABCError("invalid_intervention", f"Cannot reassign task in state: {task.status}")
        before_policy = execution_policy_view(task.extensions)
        resources, executor_session = build_task_execution_policy(
            new_executor,
            self.config,
            task.workspace or {},
        )
        policy_extensions = dict(task.extensions or {})
        policy_extensions.pop(RESOURCE_EXTENSION_KEY, None)
        policy_extensions.pop(SESSION_EXTENSION_KEY, None)
        task.extensions = attach_execution_policy(
            policy_extensions,
            resources=resources,
            session=executor_session,
        )
        after_policy = execution_policy_view(task.extensions)
        self.revoke_permission_grant(task.id, "task_reassign", model=task)
        self._release_lease(task_id)
        task.assignee = new_executor
        task.status = "pending"
        task.updated_at = _utc_now()
        task.intervention = dict(task.intervention or {})
        task.intervention.update({"paused": False, "pause_reason": None})
        task.extensions = _merge_execution(task.extensions, {"internal_status": "pending"})
        self.store.write_task(task_id, _without_none(task.to_dict()))
        self.store.append_event(
            task_id,
            {
                "event_type": "reassigned",
                "task_id": task_id,
                "assignee": new_executor,
                "created_at": task.updated_at,
                "execution_policy_before": before_policy,
                "execution_policy_after": after_policy,
            },
        )
        self._append_intervention(
            task_id,
            "reassign",
            task.updated_at,
            new_executor=new_executor,
            execution_policy_before=before_policy,
            execution_policy_after=after_policy,
        )
        self._refresh_task_index()

    def handoff_task(
        self,
        source_task_id: str,
        target_assignee: str,
        message: str | None = None,
        branch: bool = False,
        source_platform: str | None = None,
        images: list[str | Path] | None = None,
        session_id: str | None = None,
        permission_mode: str | None = None,
    ) -> TaskModel:
        source = self.get_task(source_task_id)
        target_assignee = _normalize_executor_ref(
            target_assignee,
            field="target assignee",
            empty_code="handoff_error",
            empty_message="target assignee is required",
        )
        normalized_source_status = _normalize_status(source.status)
        if normalized_source_status == "input_required":
            request = (source.extensions or {}).get("agentbc.input")
            raise ABCError(
                "input_pending",
                f"Task {source.id} is waiting for input; respond before handoff.",
                {
                    "source_task_id": source.id,
                    "input_id": request.get("input_id", "") if isinstance(request, dict) else "",
                },
            )
        if normalized_source_status not in HANDOFF_SOURCE_STATUSES:
            raise ABCError(
                "handoff_source_not_ready",
                f"Task {source.id} is {normalized_source_status}; handoff requires completed.",
                {
                    "source_task_id": source.id,
                    "status": normalized_source_status,
                    "allowed_statuses": sorted(HANDOFF_SOURCE_STATUSES),
                },
            )
        chain = self.resolve_chain(source.id)
        if chain.anomalies:
            raise ABCError(
                "invalid_lineage",
                f"Task chain for {source.id} has inconsistent lineage; resolve it before handoff.",
                chain.to_dict(),
            )
        if not branch:
            if len(chain.head_task_ids) > 1:
                raise ABCError(
                    "ambiguous_chain_head",
                    (
                        f"Task {source.id} belongs to a chain with multiple heads; "
                        "pass an explicit head task id, or use --branch intentionally."
                    ),
                    chain.to_dict(),
                )
            if not chain.requested_is_head:
                suggested = (
                    f"agentbc task handoff {chain.current_head_task_id} --to {target_assignee}"
                    if chain.current_head_task_id
                    else ""
                )
                raise ABCError(
                    "stale_handoff_source",
                    (
                        f"Task {source.id} is not the current chain head. "
                        f"Use {chain.current_head_task_id} instead."
                    ),
                    {**chain.to_dict(), "suggested_command": suggested},
                )
        workspace = source.workspace or {}
        source_permission = permission_record_from_extensions(source.extensions)
        validate_path_plan_workspace(workspace)
        report_file = workspace.get("report_file") or str(self.store.task_dir(source.id) / f"{source.id}-report.md")
        task_file = workspace.get("task_file") or report_file
        task_record_path = Path(str(task_file)).expanduser()
        report_record_path = Path(str(report_file)).expanduser()
        if report_record_path.exists():
            if task_record_path != report_record_path and task_record_path.exists():
                source_context = (
                    f"Read the previous AgentBC task record at {task_file} "
                    f"and report record at {report_file}."
                )
            else:
                source_context = f"Read the previous AgentBC task/report record at {report_file}."
        else:
            source_context = (
                f"The compact record was cleaned; restore the preserved task state with "
                f"`agentbc task report {source.id}`."
            )
        description = (
            f"Continue from AgentBC task {source.id} in task code {workspace.get('task_code')}. "
            f"{source_context} Then perform this handoff request: "
            f"{message or 'review current state and complete the next required action.'}"
        )
        handoff = self.create_task(
            title=f"Handoff from {source.id}: {source.title}",
            assignee=target_assignee,
            steps=[{"id": 1, "description": description}],
            session_id=session_id,
            source_platform=source_platform,
            customer_dir=bool(workspace.get("customer_dir")),
            customer_path=workspace.get("customer_path") or None,
            lineage=_next_lineage(source, workspace, branch=branch),
            images=images if images is not None else task_image_paths(source.to_dict()),
            permission_mode=permission_mode,
            inherited_permission=source_permission if permission_mode is None else None,
        )
        now = _utc_now()
        self.store.append_event(
            source.id,
            {
                "event_type": "handoff_created",
                "task_id": source.id,
                "created_at": now,
                "target_task_id": handoff.id,
                "target_assignee": target_assignee,
            },
        )
        self._append_intervention(
            source.id,
            "handoff",
            now,
            new_executor=target_assignee,
            target_task_id=handoff.id,
            message=message,
        )
        return handoff

    def preflight(self, task_id: str) -> PreflightResult:
        try:
            raw_task = self.store.read_task(task_id)
            task = TaskModel.from_dict(raw_task)
        except (ABCError, TypeError, ValueError) as exc:
            return PreflightResult(ok=False, errors=[f"schema invalid: {exc}"])

        errors = validate_task(raw_task)
        if self.store.is_leased(task_id):
            errors.append("task has an active lease")
        if not task.steps:
            errors.append("task has no steps")
        try:
            validate_path_plan_workspace(task.workspace or {})
        except ABCError as exc:
            errors.append(str(exc))
        errors.extend(validate_execution_policy_extensions(task.extensions or {}))
        if _normalize_status(task.status) in TASK_TERMINAL_STATES:
            errors.append(f"task is terminal: {task.status}")
        try:
            permission = permission_record_from_extensions(task.extensions)
            executor = get_executor(
                task.assignee,
                get_executor_config(self.config, task.assignee),
            )
            assert_executor_permission_supported(
                task.assignee,
                permission["effective_mode"],
                getattr(executor, "agent_bin", None),
            )
        except ABCError as exc:
            errors.append(f"{exc.code}: {exc}")
        except (TypeError, ValueError):
            errors.append(f"assignee does not exist: {task.assignee}")
        return PreflightResult(
            ok=not errors,
            errors=errors,
            execution_policy=execution_policy_view(task.extensions),
        )

    def _supports_immediate_pause(self, assignee: str) -> bool:
        try:
            return get_executor(assignee).capabilities().level >= 3
        except (TypeError, ValueError):
            return False

    def _refresh_active_tasks(self, tasks: list[TaskModel]) -> list[TaskModel]:
        from .run_lease import reconcile_task

        refreshed: list[TaskModel] = []
        for task in tasks:
            if _is_running_status(task.status):
                try:
                    reconcile_task(task.id, self.board_root)
                    task = self.get_task(task.id)
                except (ABCError, PermissionError):
                    pass
            refreshed.append(task)
        return refreshed

    def _validated_executor_session(
        self,
        task: TaskModel,
        run_id: str,
        receipt: Any,
    ) -> dict[str, Any]:
        errors = validate_execution_session_receipt(receipt, executor=task.assignee)
        if errors:
            raise ABCError(
                "executor_session_receipt_invalid",
                "; ".join(errors),
                {"errors": errors},
            )
        session = (task.extensions or {}).get(SESSION_EXTENSION_KEY)
        snapshot_errors = validate_session_snapshot(session, executor=task.assignee)
        if snapshot_errors:
            raise ABCError(
                "executor_session_invalid",
                "; ".join(snapshot_errors),
                {"errors": snapshot_errors},
            )
        normalized_run_id = str(run_id or "").strip()
        run_ids = list(session.get("run_ids") or [])
        if not normalized_run_id or normalized_run_id not in run_ids:
            raise ABCError(
                "executor_session_run_mismatch",
                "Executor session receipt does not belong to the recorded run",
            )
        expected_resumed = run_ids.index(normalized_run_id) > 0
        if receipt.get("resumed") is not expected_resumed:
            raise ABCError(
                "executor_session_resume_mismatch",
                "Executor session receipt resume mode does not match task history",
            )
        existing_id = str(session.get("session_id") or "").strip()
        received_id = str(receipt.get("session_id") or "").strip()
        if existing_id and received_id != existing_id:
            raise ABCError(
                "executor_session_id_mismatch",
                "Executor session receipt does not match the authoritative task session",
            )
        return dict(receipt)

    def _apply_executor_session_result(
        self,
        task: TaskModel,
        run_id: str,
        receipt: Any,
        session_state: str,
    ) -> None:
        validated = self._validated_executor_session(task, run_id, receipt)
        extensions = dict(task.extensions or {})
        session = dict(extensions[SESSION_EXTENSION_KEY])
        if not str(session.get("session_id") or "").strip():
            session["session_id"] = validated["session_id"]
        session["session_state"] = session_state
        errors = validate_session_snapshot(session, executor=task.assignee)
        if errors:
            raise ABCError("executor_session_invalid", "; ".join(errors), {"errors": errors})
        extensions[SESSION_EXTENSION_KEY] = session
        task.extensions = extensions

    def _set_known_executor_session_state(
        self,
        task: TaskModel,
        session_state: str,
    ) -> None:
        """Advance a known session after a non-receipt failure without inventing an ID."""
        extensions = dict(task.extensions or {})
        session = extensions.get(SESSION_EXTENSION_KEY)
        if not isinstance(session, dict) or not str(session.get("session_id") or "").strip():
            return
        updated = dict(session)
        updated["session_state"] = session_state
        errors = validate_session_snapshot(updated, executor=task.assignee)
        if errors:
            # Recovery must remain fail closed even when the persisted session
            # snapshot is already damaged.  Do not invent or partially rewrite
            # a session id/state; the task error is the public evidence.
            return
        extensions[SESSION_EXTENSION_KEY] = updated
        task.extensions = extensions

    def _release_lease(self, task_id: str) -> None:
        lease_path = self.store._task_dir(task_id) / "lease.json"
        lease = self.store._read_optional_json(lease_path)
        if lease and lease.get("lease_token"):
            self.store.release_lease(task_id, str(lease["lease_token"]))

    def _record_run_interval(
        self,
        task_id: str,
        extensions: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Append the current authoritative RunLease interval to the execution ledger.

        REPORT-001: execution duration accumulates from these RunLease intervals
        instead of ``created_at``/``completed_at`` wall time minus input waiting.
        Idempotent per run id so a lifecycle transition never double counts a run.
        """
        from .run_lease import RunLeaseState, load_lease

        merged = dict(extensions or {})
        lease = load_lease(task_id, self.board_root)
        if lease is None:
            return merged
        ledger = _execution_ledger(merged)
        if any(str(item.get("run_id") or "") == lease.run_id for item in ledger):
            return merged
        started_raw = lease.started_at
        if not isinstance(started_raw, str) or not started_raw:
            return merged
        started = _parse_timestamp(started_raw)
        if started == datetime.min.replace(tzinfo=timezone.utc):
            return merged
        if lease.state == RunLeaseState.ACTIVE:
            end_raw = _utc_now()
            state = RunLeaseState.ACTIVE
        else:
            end_raw = lease.last_heartbeat_at
            state = lease.state
        if not isinstance(end_raw, str) or not end_raw:
            return merged
        ended = _parse_timestamp(end_raw)
        if ended == datetime.min.replace(tzinfo=timezone.utc):
            return merged
        ledger.append(
            {
                "run_id": lease.run_id,
                "executor_id": lease.executor_id,
                "started_at": lease.started_at,
                "ended_at": end_raw,
                "duration_s": round(max((ended - started).total_seconds(), 0.0), 3),
                "state": state,
            }
        )
        return _merge_execution(merged, {"run_intervals": ledger[:8]})

    def _append_intervention(
        self,
        task_id: str,
        kind: str,
        created_at: str,
        intervention_type: str | None = None,
        **details: Any,
    ) -> None:
        self.store.append_intervention(
            task_id,
            {
                "type": kind,
                "intervention_type": intervention_type or kind,
                "task_id": task_id,
                "created_at": created_at,
                **details,
            },
        )

    def generate_report(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        report = {
            "task_id": task.id,
            "status": task.status,
            "generated_at": _utc_now(),
            "steps_total": len(task.steps),
            "steps_done": sum(1 for step in task.steps if step.get("status") in {"done", "completed"}),
            "events": len(self.store.read_events(task_id)),
            "interventions": len(self.store.read_interventions(task_id)),
        }
        task.report = report
        task.updated_at = report["generated_at"]
        self.store.write_task(task_id, _without_none(task.to_dict()))
        return report

    def notify(self, task_id: str, event_type: str) -> None:
        task = self.get_task(task_id)
        self.store.append_event(task_id, {"event_type": event_type, "task_id": task.id, "created_at": _utc_now()})

    def request_session_cleanup(self, task_id: str, *, now: str | None = None) -> dict:
        """Request one authoritative post-terminal session cleanup pass.

        The coordinator re-reads the task/session from disk, re-validates every
        eligibility gate (terminal task, closed RunLease, written report,
        recorded terminal notification, terminal session with an exact session
        ID) under a per-task lock, and only then transitions the cleanup receipt.
        Retained sessions are marked ``retained``; everything else is dispatched
        to the Executor cleanup port with an atomic receipt/event write-back.
        """
        from .session_cleanup import SessionCleanupCoordinator

        return SessionCleanupCoordinator(self.board_root).request_cleanup(task_id, now=now)

    def _refresh_task_index(self) -> None:
        refresh_task_index(self.board_root)

    def _task_status_with_chain(self, task: TaskModel) -> dict[str, Any]:
        status = task_to_status(task)
        from .timing_view import build_timing_view

        timing = build_timing_view(task, self.board_root)
        status["timing"] = timing
        status["run_lease_state"] = timing["lease_state"]
        try:
            chain = self.resolve_chain(task.id)
        except ABCError:
            return status
        status["chain_root_task_id"] = chain.chain_root_task_id
        status["head_task_ids"] = chain.head_task_ids
        status["is_chain_head"] = chain.requested_is_head
        status["chain_anomalies"] = chain.anomalies
        lineage = _lineage_for(task)
        status["parent_task_id"] = lineage.get("parent_task_id")
        status["base_task_id"] = lineage.get("base_task_id") or task.id
        status["iteration_index"] = lineage.get("iteration_index", 1)
        status["branch_mode"] = lineage.get("branch_mode", "linear")
        status["task_code"] = lineage.get("task_code") or (task.workspace or {}).get("task_code")
        status["iteration"] = (task.workspace or {}).get("iteration") or f"{int(status['iteration_index']):03d}"
        status["task_date"] = lineage.get("task_date") or (task.workspace or {}).get("task_date")
        status["chain_id"] = lineage.get("chain_id")
        status["chain_token"] = lineage.get("chain_token")
        status["chain_dir"] = lineage.get("chain_dir")
        status["chain_task_id"] = lineage.get("chain_task_id")
        status["chain_output_dir"] = lineage.get("chain_output_dir")
        return status


def create_task(
    title: str,
    assignee: str,
    steps: list[dict[str, Any]],
    session_id: str | None = None,
    source_platform: str | None = None,
    customer_dir: bool | None = None,
    customer_path: str | Path | None = None,
    board_root: str | Path = DEFAULT_BOARD_ROOT,
    workspace_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    artifacts_dir: str | Path | None = None,
    lineage: dict[str, Any] | None = None,
    images: list[str | Path] | None = None,
    permission_mode: str | None = None,
) -> TaskModel:
    return TaskService(board_root).create_task(
        title,
        assignee,
        steps,
        session_id=session_id,
        source_platform=source_platform,
        customer_dir=customer_dir,
        customer_path=customer_path,
        workspace_root=workspace_root,
        output_dir=output_dir,
        artifacts_dir=artifacts_dir,
        lineage=lineage,
        images=images,
        permission_mode=permission_mode,
    )


def get_task(task_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> TaskModel:
    return TaskService(board_root).get_task(task_id)


def list_tasks(
    status: str | None = None,
    assignee: str | None = None,
    board_root: str | Path = DEFAULT_BOARD_ROOT,
) -> list[TaskModel]:
    return TaskService(board_root).list_tasks(status=status, assignee=assignee)


def claim_task(task_id: str, executor_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> str:
    return TaskService(board_root).claim_task(task_id, executor_id)


def execute_step(task_id: str, step_id: int, result: dict[str, Any], board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).execute_step(task_id, step_id, result)


def complete_task(task_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).complete_task(task_id)


def fail_task(
    task_id: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    board_root: str | Path = DEFAULT_BOARD_ROOT,
) -> None:
    TaskService(board_root).fail_task(task_id, code, message, details)


def pause_task(task_id: str, reason: str | None = None, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).pause_task(task_id, reason)


def resume_task(task_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).resume_task(task_id)


def cancel_task(task_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).cancel_task(task_id)


def correct_step(task_id: str, step_id: int, message: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).correct_step(task_id, step_id, message)


def retry_step(task_id: str, step_id: int, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).retry_step(task_id, step_id)


def reassign_task(task_id: str, new_executor: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).reassign_task(task_id, new_executor)


def handoff_task(
    source_task_id: str,
    target_assignee: str,
    message: str | None = None,
    board_root: str | Path = DEFAULT_BOARD_ROOT,
    branch: bool = False,
    source_platform: str | None = None,
    images: list[str | Path] | None = None,
    session_id: str | None = None,
    permission_mode: str | None = None,
) -> TaskModel:
    return TaskService(board_root).handoff_task(
        source_task_id,
        target_assignee,
        message,
        branch=branch,
        session_id=session_id,
        source_platform=source_platform,
        images=images,
        permission_mode=permission_mode,
    )


def preflight(task_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> PreflightResult:
    return TaskService(board_root).preflight(task_id)


def generate_report(task_id: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> dict[str, Any]:
    return TaskService(board_root).generate_report(task_id)


def notify(task_id: str, event_type: str, board_root: str | Path = DEFAULT_BOARD_ROOT) -> None:
    TaskService(board_root).notify(task_id, event_type)


def load_steps(path: str | Path) -> list[dict[str, Any]]:
    steps_file = Path(path).expanduser()
    if not steps_file.exists():
        raise ABCError("task_create_error", f"steps file not found: {steps_file}")
    text = steps_file.read_text(encoding="utf-8")
    return _load_steps_text(text)


def task_to_status(task: TaskModel) -> dict[str, Any]:
    from .task_health import task_health

    data = task.to_dict()
    raw_status = str(data.get("status", "pending"))
    data["status"] = _normalize_status(raw_status)
    data["steps"] = [dict(step, status=step.get("status", "pending")) for step in data.get("steps", [])]
    extensions = dict(data.get("extensions") or {})
    extensions.setdefault(
        PERMISSION_EXTENSION_KEY,
        permission_record_from_extensions(extensions),
    )
    # Public status exposes only the sanitized approval projection: the bounded
    # summary, decision and timestamps.  Binding identifiers and the optional
    # bounded detail are internal and never projected.
    approval_value = extensions.get(APPROVAL_EXTENSION_KEY)
    if approval_value is not None:
        try:
            extensions[APPROVAL_EXTENSION_KEY] = approval_public_projection(
                approval_value
            )
        except ABCError:
            extensions.pop(APPROVAL_EXTENSION_KEY, None)
    data["extensions"] = extensions
    if raw_status != data["status"]:
        extensions = dict(data.get("extensions") or {})
        data["extensions"] = _merge_execution(extensions, {"internal_status": raw_status})
    data["health"] = task_health(task)
    return public_task_view(data)


def task_resolution(
    task_id: str | None = None,
    board_root: str | Path = DEFAULT_BOARD_ROOT,
) -> dict[str, Any]:
    return TaskService(board_root).resolve_task(task_id)


def chain_resolution(
    task_id: str,
    board_root: str | Path = DEFAULT_BOARD_ROOT,
) -> dict[str, Any]:
    return TaskService(board_root).resolve_chain(task_id).to_dict()


def _normalize_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ABCError("task_create_error", "each step must be a mapping")
    normalized = dict(step)
    description = task_step_text(normalized)
    if not description:
        raise ABCError(
            "task_create_error",
            f"step {index} must define a non-empty description or action",
        )
    normalized.setdefault("id", index)
    normalized["description"] = description
    normalized.setdefault("record", f"steps/{index:02d}.json")
    return normalized


def _validate_resource_block_receipt(
    task: TaskModel,
    run_id: str,
    resource_exhaustion: Any,
    execution_session: Any,
) -> str | None:
    """Return a fail-closed code when a resource wait cannot be created safely."""
    if not isinstance(resource_exhaustion, dict) or resource_exhaustion.get("detected") is not True:
        return "resource_block_invalid_receipt"
    executor = str(resource_exhaustion.get("executor") or "").strip().lower()
    if executor != str(task.assignee or "").strip().lower():
        return "resource_block_executor_mismatch"
    resource = str(resource_exhaustion.get("resource") or "").strip()
    if resource != RESOURCE_KIND_BY_EXECUTOR.get(executor):
        return "resource_block_resource_mismatch"
    if not str(run_id or "").strip():
        return "resource_block_run_mismatch"
    extensions = task.extensions or {}
    resources = extensions.get(RESOURCE_EXTENSION_KEY)
    snapshot_errors = validate_resource_snapshot(resources, executor=executor)
    if snapshot_errors:
        return "resource_block_snapshot_invalid"
    if resources.get("resource") != resource:
        return "resource_block_snapshot_invalid"
    source = str(resource_exhaustion.get("source") or "").strip()
    allowed_sources = {
        "claude": {"structured_error_max_budget_usd", "text_exceeded_usd_budget"},
        "hermes": {
            "max_iterations_reached",
            "budget_exhausted",
            "iteration_budget_message",
            "reached_maximum_iterations",
        },
    }
    if source not in allowed_sources.get(executor, set()):
        return "resource_block_invalid_receipt"
    limit = resource_exhaustion.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, (int, float))
    ):
        return "resource_block_invalid_receipt"
    if resource_exhaustion.get("limit_matches_snapshot") is False:
        return "resource_block_snapshot_invalid"
    session = extensions.get(SESSION_EXTENSION_KEY)
    session_errors = validate_session_snapshot(session, executor=executor)
    if session_errors:
        return "resource_block_session_invalid"
    run_ids = list(session.get("run_ids") or [])
    if run_id not in run_ids:
        return "resource_block_run_mismatch"
    if execution_session is not None:
        receipt_errors = validate_execution_session_receipt(execution_session, executor=executor)
        if receipt_errors:
            return "resource_block_receipt_invalid"
        received_id = str(execution_session.get("session_id") or "").strip()
        existing_id = str(session.get("session_id") or "").strip()
        if existing_id and received_id and received_id != existing_id:
            return "resource_block_receipt_invalid"
    return None


def _first_incomplete_step_id(steps: list[dict[str, Any]]) -> int | None:
    for step in steps:
        if str(step.get("status") or "pending") != "done":
            step_id = step.get("id")
            if isinstance(step_id, int):
                return step_id
    return None


def _safe_blocked_step_id(blocked_results: list[dict[str, Any]]) -> int | None:
    if len(blocked_results) != 1:
        return None
    step_id = blocked_results[0].get("id")
    if isinstance(step_id, bool) or not isinstance(step_id, int):
        return None
    return step_id


def _resource_block_step(
    step: dict[str, Any],
    blocked_step_id: int | None,
) -> dict[str, Any]:
    """Mark the first incomplete step blocked; keep done steps and pending status."""
    updated = dict(step)
    if str(updated.get("status") or "pending") == "done":
        return updated
    if updated.get("id") == blocked_step_id:
        updated["status"] = "blocked"
    else:
        updated["status"] = str(updated.get("status") or "pending")
    return updated


def _resource_block_reason(executor: str, used: Any, limit: Any) -> str:
    label = "Hermes 迭代" if str(executor).strip().lower() == "hermes" else "Claude 预算"
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
        return f"{label}已使用 {used} / 上限 {limit}"
    if isinstance(limit, (int, float)):
        return f"{label}上限 {limit} 已耗尽"
    return f"{label}已耗尽"


def _update_step(step: dict[str, Any], step_id: int, result: dict[str, Any]) -> dict[str, Any]:
    updated = dict(step)
    if updated.get("id") == step_id:
        updated["status"] = result.get("status", "done")
        updated["result"] = result
    return updated


def _retry_step(step: dict[str, Any], step_id: int) -> dict[str, Any]:
    updated = dict(step)
    if updated.get("id") == step_id:
        updated["status"] = "pending"
        updated.pop("result", None)
    return updated


def _require_step(task: TaskModel, step_id: int) -> None:
    if not any(step.get("id") == step_id for step in task.steps):
        raise ABCError("step_not_found", f"Step not found: {step_id}", {"task_id": task.id, "step_id": step_id})


def _validate_path(*states: str) -> None:
    for from_state, to_state in zip(states, states[1:]):
        validate_transition(from_state, to_state)


def _load_steps_text(text: str) -> list[dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        yaml = None
    if yaml is not None:
        data = yaml.safe_load(text)
        yaml_steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(yaml_steps, list):
            raise ABCError("task_create_error", "steps file must contain a steps list")
        return [_normalize_step(step, index) for index, step in enumerate(yaml_steps, 1)]

    steps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped == "steps:":
            index += 1
            continue
        if stripped.startswith("- "):
            if current is not None:
                steps.append(current)
            current = {}
            remainder = stripped[2:].strip()
            if remainder:
                if _is_block_scalar_key(remainder):
                    key = remainder.split(":", 1)[0].strip()
                    block, index = _collect_block_scalar(lines, index + 1, _indent(raw_line))
                    current[key] = block
                    continue
                _parse_step_key_value(current, remainder)
            index += 1
            continue
        if current is not None:
            if _is_block_scalar_key(stripped):
                key = stripped.split(":", 1)[0].strip()
                block, index = _collect_block_scalar(lines, index + 1, _indent(raw_line))
                current[key] = block
                continue
            _parse_step_key_value(current, stripped)
        index += 1
    if current is not None:
        steps.append(current)
    if not steps:
        raise ABCError("task_create_error", "steps file must contain a steps list")
    return steps


def _parse_step_key_value(step: dict[str, Any], text: str) -> None:
    if ":" not in text:
        return
    key, value = text.split(":", 1)
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    elif value.isdigit():
        step[key.strip()] = int(value)
        return
    step[key.strip()] = value


def _is_block_scalar_key(text: str) -> bool:
    if ":" not in text:
        return False
    _, value = text.split(":", 1)
    return value.strip() in {"|", "|-", "|+", ">", ">-", ">+"}


def _collect_block_scalar(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    collected: list[str] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        current_indent = _indent(raw)
        if stripped.startswith("- ") and current_indent <= parent_indent:
            break
        if stripped and current_indent <= parent_indent:
            break
        collected.append(stripped)
        index += 1
    return "\n".join(collected).strip(), index


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _normalize_executor_ref(
    value: str,
    *,
    field: str,
    empty_code: str,
    empty_message: str,
) -> str:
    executor = value.strip()
    if not executor:
        raise ABCError(empty_code, empty_message)
    if executor.lower() in {"codex", "hermes", "claude", "mock", "shell"}:
        return executor
    if is_task_like(executor):
        raise ABCError(
            "invalid_executor",
            f"{field} expects an executor name, not a task id: {executor}",
            {
                "field": field,
                "value": executor,
                "hint": "Use --to <executor> such as hermes, codex, or claude. Do not use --to <task_id> to link two existing tasks.",
            },
        )
    return executor


def _write_task_requirements(task: TaskModel, path: Path) -> None:
    workspace = task.workspace or {}
    provenance = task.extensions.get("agentbc.provenance") or {}
    lineage = task.extensions.get("agentbc.lineage") or {}
    images = task_image_paths(task.to_dict())
    permission = permission_record_from_extensions(task.extensions)
    policy = execution_policy_view(task.extensions)
    resources = policy.get("resources") or {}
    executor_session = policy.get("session") or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Task Requirements: {task.title}",
        "",
        f"- Task ID: `{task.id}`",
        f"- Task code: `{workspace.get('task_code', '')}`",
        f"- Iteration: `{workspace.get('iteration', lineage.get('iteration_index', 1))}`",
        f"- Assignee: `{task.assignee}`",
        f"- Status snapshot: `{task.status}` (non-authoritative; use `agentbc task status {task.id}` or the report for current state)",
        f"- Dispatcher platform: `{provenance.get('source_platform', task.created_by)}`",
        f"- Dispatcher conversation ID: `{task.session_id or 'unavailable'}`",
        f"- Requested permission mode: `{permission['requested_mode']}`",
        f"- Effective permission mode: `{permission['effective_mode']}`",
        f"- Permission selection source: `{permission['selection_source']}`",
        f"- Resource policy: `{resources.get('resource') or 'none'}`",
        f"- Effective resource limit: `{resources.get('limit') if resources else 'none'}`",
        f"- Resource policy source: `{resources.get('source') or 'none'}`",
        f"- Resource policy frozen: `{'yes' if resources.get('frozen') else 'no'}`",
        f"- Retain executor session: `{'yes' if executor_session.get('retain') else 'no'}`",
        f"- Executor session ID: `{executor_session.get('session_id') or 'pending'}`",
        f"- Executor session state: `{executor_session.get('session_state') or 'unavailable'}`",
        f"- Executor project mode: `{executor_session.get('project_mode') or 'unavailable'}`",
        f"- Customer directory: `{workspace.get('customer_dir', '')}`",
        f"- Customer path: `{workspace.get('customer_path', '')}`",
        f"- Project root: `{workspace.get('project_root', workspace.get('root', ''))}`",
        f"- Artifact root: `{workspace.get('artifact_root', workspace.get('artifacts_dir', ''))}`",
        f"- Report directory: `{workspace.get('report_root', '')}`",
        f"- Runtime record: `{workspace.get('internal_task_dir', '')}`",
        f"- Current task: `{task.id}`",
        f"- Report: `{workspace.get('report_file', '')}`",
        f"- Chain root task: `{lineage.get('chain_root_task_id', task.id)}`",
        f"- Base task: `{lineage.get('base_task_id', task.id)}`",
        f"- Base workspace: `{lineage.get('base_workspace_root', workspace.get('root', ''))}`",
        f"- Base artifacts: `{lineage.get('base_artifacts_dir', workspace.get('artifacts_dir', ''))}`",
        f"- Iteration: `{lineage.get('iteration_index', 1)}`",
    ]
    if images:
        lines.extend(["", "## Image Inputs", *[f"- `{image}`" for image in images]])
    lines.extend(["", "## Requirements"])
    for index, step in enumerate(task.steps, 1):
        lines.append(f"{index}. {task_step_text(step)}")
    lines.extend(
        [
            "",
            "## Output Contract",
            "- User deliverables go to the project/artifact root named above.",
            "- Task/report Markdown stays in the AgentBC report directory; compact runtime state stays in workspace/record.",
            "- Do not copy the user project into AgentBC workspace unless the task explicitly asks for that copied directory.",
            "- Keep source-code edits inside the project root.",
            "- Do not write or replace REPORT.md; AgentBC Core generates the execution report.",
            "- Use the AgentBC report, or `agentbc task report`, as the handoff source for another agent or a new session.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _validate_failure_path(status: str) -> None:
    if _normalize_status(status) not in {"pending", "running", "input_required", "needs_recovery"}:
        raise ABCError("invalid_transition", f"Cannot enter recovery from state: {status}")


def _without_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.min.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _stable_revocation_code(code: str) -> str:
    """Sanitize a lifecycle reason into a stable non-sensitive revocation code."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", str(code or "").strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "permission_revoked"
    if not cleaned[0].isalpha():
        cleaned = f"r_{cleaned}"
    return cleaned[:64]


def _is_running_status(status: str) -> bool:
    return _normalize_status(status) == "running" or status in RUNNING_TASK_STATUSES


def _is_reportable_status(status: str) -> bool:
    return _normalize_status(status) in REPORTABLE_TASK_STATUSES


def _task_sort_key(task: TaskModel) -> tuple[int, float, str]:
    updated = int(_parse_timestamp(task.updated_at).strftime("%Y%m%d%H%M%S%f"))
    if _is_running_status(task.status):
        priority = 0
    elif _is_reportable_status(task.status):
        priority = 1
    else:
        priority = 2
    return (priority, -updated, task.id)


def _task_code_for(task: TaskModel) -> str:
    workspace = task.workspace or {}
    lineage = (task.extensions or {}).get("agentbc.lineage") or {}
    if isinstance(lineage, dict) and isinstance(lineage.get("task_code"), str) and lineage.get("task_code"):
        return str(lineage["task_code"]).upper()
    if isinstance(workspace.get("task_code"), str) and workspace.get("task_code"):
        return str(workspace["task_code"]).upper()
    try:
        return split_task_ref(task.id)[0]
    except ValueError:
        return task.id


def _task_chain_delete_ownership(
    board_root: Path,
    task_code: str,
    tasks: list[TaskModel],
) -> dict[str, list[dict[str, Any]]]:
    """Prove every delete path from canonical task metadata, never from names alone."""
    delete_objects: list[dict[str, Any]] = []
    preserve_objects: list[dict[str, Any]] = []
    targets_by_path: dict[str, dict[str, str]] = {}
    customer_paths: set[str] = set()

    record_root = board_root.expanduser().resolve()
    record_chain = _require_owned_delete_path(
        record_root / task_code,
        record_root / task_code,
        record_root,
        "record chain",
    )
    for task in tasks:
        workspace = task.workspace or {}
        validate_path_plan_workspace(workspace)
        if _task_code_for(task) != task_code:
            raise ABCError("task_delete_ownership_error", f"Task code mismatch for {task.id}")
        iteration = f"{int(task_iteration(task.id) or 0):03d}"
        expected_record = record_chain / iteration
        _require_owned_delete_path(
            workspace.get("internal_task_dir"),
            expected_record,
            record_root,
            f"record for {task.id}",
        )
        delete_objects.append(
            {"kind": "record", "task_id": task.id, "path": str(expected_record), "exists": expected_record.exists()}
        )

        agentbc_root = Path(str(workspace.get("agentbc_root") or "")).expanduser().resolve()
        task_date = str(workspace.get("task_date") or "")
        report_base = agentbc_root / "tasks" / "report"
        expected_report_root = report_base / task_date / task_code
        report_root = _require_owned_delete_path(
            workspace.get("report_root"),
            expected_report_root,
            report_base,
            f"report root for {task.id}",
        )
        expected_task_file = report_root / f"{task.id}-task.md"
        expected_report_file = report_root / f"{task.id}-report.md"
        _require_exact_path(workspace.get("task_file"), expected_task_file, f"task brief for {task.id}")
        _require_exact_path(workspace.get("report_file"), expected_report_file, f"report for {task.id}")
        for kind, path in (("task_brief", expected_task_file), ("report", expected_report_file)):
            delete_objects.append(
                {"kind": kind, "task_id": task.id, "path": str(path), "exists": path.exists()}
            )
        if report_root.exists():
            targets_by_path.setdefault(
                str(report_root),
                {"kind": "reports", "path": str(report_root), "allowed_root": str(report_base.resolve())},
            )

        delete_objects.append(
            {"kind": "index_entry", "task_id": task.id, "path": f"task_index:{task.id}", "exists": True}
        )
        if bool(workspace.get("customer_dir")):
            customer_path = str(Path(str(workspace.get("project_root") or "")).expanduser().resolve())
            if customer_path and customer_path not in customer_paths:
                customer_paths.add(customer_path)
                preserve_objects.append(
                    {"kind": "customer_project", "path": customer_path, "reason": "customer-owned"}
                )
            continue

        artifact_base = agentbc_root / "tasks" / "artifacts"
        expected_artifact_root = artifact_base / task_date / task_code
        artifact_root = _require_owned_delete_path(
            workspace.get("artifact_root") or workspace.get("artifacts_dir"),
            expected_artifact_root,
            artifact_base,
            f"managed artifact root for {task.id}",
        )
        delete_objects.append(
            {"kind": "managed_artifact", "task_id": task.id, "path": str(artifact_root), "exists": artifact_root.exists()}
        )
        if artifact_root.exists():
            targets_by_path.setdefault(
                str(artifact_root),
                {"kind": "artifacts", "path": str(artifact_root), "allowed_root": str(artifact_base.resolve())},
            )

    delete_objects.append(
        {"kind": "task_code_claim", "task_code": task_code, "path": str(record_chain), "exists": record_chain.exists()}
    )
    targets_by_path[str(record_chain)] = {
        "kind": "records",
        "path": str(record_chain),
        "allowed_root": str(record_root),
    }
    return {
        "delete_objects": delete_objects,
        "preserve_objects": preserve_objects,
        "targets": list(targets_by_path.values()),
    }


def _require_owned_delete_path(
    value: Any,
    expected: Path,
    allowed_root: Path,
    label: str,
) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ABCError("task_delete_ownership_error", f"Missing {label} path")
    raw = Path(text).expanduser()
    if not raw.is_absolute():
        raise ABCError("task_delete_ownership_error", f"{label} path is not absolute: {raw}")
    if raw.is_symlink():
        raise ABCError("task_delete_ownership_error", f"Refusing symlink {label}: {raw}")
    resolved = raw.resolve()
    expected_resolved = expected.expanduser().resolve()
    allowed_resolved = allowed_root.expanduser().resolve()
    if resolved != expected_resolved or resolved == allowed_resolved or not _path_is_within(resolved, allowed_resolved):
        raise ABCError(
            "task_delete_ownership_error",
            f"{label} is not the canonical AgentBC-owned path: {raw}",
            {"expected": str(expected_resolved), "actual": str(resolved)},
        )
    return resolved


def _require_exact_path(value: Any, expected: Path, label: str) -> None:
    text = str(value or "").strip()
    if not text:
        raise ABCError("task_delete_ownership_error", f"Missing {label} path")
    raw = Path(text).expanduser()
    if not raw.is_absolute() or raw.resolve() != expected.resolve():
        raise ABCError(
            "task_delete_ownership_error",
            f"{label} is not the canonical AgentBC-owned path: {raw}",
        )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _agentbc_root_from_workspace(workspace: dict[str, Any]) -> str | None:
    explicit = str(workspace.get("agentbc_root") or "").strip()
    if explicit:
        return explicit
    for field in ("report_root", "report_file", "task_file"):
        value = str(workspace.get(field) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        parts = path.parts
        if "tasks" not in parts:
            continue
        index = parts.index("tasks")
        if index <= 0:
            continue
        return str(Path(*parts[:index]))
    return str(workspace.get("default_path") or "").strip() or None


def _iteration_for(task: TaskModel) -> int:
    workspace = task.workspace or {}
    lineage = (task.extensions or {}).get("agentbc.lineage") or {}
    value = workspace.get("iteration") or (lineage.get("iteration_index") if isinstance(lineage, dict) else None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return task_iteration(task.id) or 0


def _chain_heads(tasks: list[TaskModel]) -> list[TaskModel]:
    heads: dict[str, TaskModel] = {}
    for task in tasks:
        code = _task_code_for(task)
        current = heads.get(code)
        if current is None or _iteration_for(task) > _iteration_for(current):
            heads[code] = task
    return sorted(heads.values(), key=_task_sort_key)


def _task_summary(task: TaskModel, board_root: str | Path | None = None) -> dict[str, Any]:
    from .task_health import task_health

    workspace = task.workspace or {}
    lineage = _lineage_for(task)
    provenance = (task.extensions or {}).get("agentbc.provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    dispatcher = str(provenance.get("source_platform") or task.created_by or "user")
    health = task_health(task)
    is_active = _is_running_status(task.status) or health.get("state") == "starting"
    summary = {
        "task_id": task.id,
        "task_code": workspace.get("task_code") or lineage.get("task_code") or _task_code_for(task),
        "iteration": workspace.get("iteration") or lineage.get("iteration_index") or "",
        "title": task.title,
        "status": _normalize_status(task.status),
        "assignee": task.assignee,
        "created_by": task.created_by,
        "dispatcher": dispatcher if dispatcher != "unknown" else str(task.created_by or "user"),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "chain_root_task_id": lineage.get("chain_root_task_id") or task.id,
        "chain_id": lineage.get("chain_id") or workspace.get("chain_id", ""),
        "chain_token": lineage.get("chain_token") or workspace.get("chain_token", ""),
        "chain_dir": lineage.get("chain_dir") or workspace.get("chain_dir", ""),
        "chain_task_id": lineage.get("chain_task_id") or workspace.get("chain_task_id", ""),
        "chain_output_dir": lineage.get("chain_output_dir") or workspace.get("output_dir", ""),
        "base_task_id": lineage.get("base_task_id") or task.id,
        "artifacts_dir": workspace.get("artifacts_dir", ""),
        "project_root": workspace.get("project_root", workspace.get("root", "")),
        "report_file": workspace.get("report_file", ""),
        "is_active": is_active,
        "health": health,
        "health_state": health.get("state", ""),
        "health_color": health.get("color", "gray"),
    }
    if board_root is not None:
        from .timing_view import build_timing_view

        summary["timing"] = build_timing_view(task, board_root)
        summary["lease_state"] = summary["timing"]["lease_state"]
    return summary


def _lineage_for(task: TaskModel) -> dict[str, Any]:
    lineage = (task.extensions or {}).get("agentbc.lineage")
    if not isinstance(lineage, dict):
        lineage = {}
    return {
        "parent_task_id": lineage.get("parent_task_id"),
        "base_task_id": lineage.get("base_task_id") or task.id,
        "chain_root_task_id": lineage.get("chain_root_task_id") or task.id,
        "iteration_index": lineage.get("iteration_index", 1),
        "task_code": lineage.get("task_code") or (task.workspace or {}).get("task_code") or _task_code_for(task),
        "task_date": lineage.get("task_date") or (task.workspace or {}).get("task_date"),
        "agentbc_root": lineage.get("agentbc_root")
        or (task.workspace or {}).get("agentbc_root")
        or _agentbc_root_from_workspace(task.workspace or {}),
        "base_workspace_root": lineage.get("base_workspace_root") or (task.workspace or {}).get("root"),
        "base_artifacts_dir": lineage.get("base_artifacts_dir") or (task.workspace or {}).get("artifacts_dir"),
        "branch_mode": lineage.get("branch_mode", "linear"),
        "chain_id": lineage.get("chain_id") or (task.workspace or {}).get("chain_id"),
        "chain_token": lineage.get("chain_token") or (task.workspace or {}).get("chain_token"),
        "chain_dir": lineage.get("chain_dir") or (task.workspace or {}).get("chain_dir") or Path((task.workspace or {}).get("output_dir", "")).name,
        "chain_task_id": lineage.get("chain_task_id") or (task.workspace or {}).get("chain_task_id"),
        "chain_output_dir": lineage.get("chain_output_dir") or (task.workspace or {}).get("chain_output_dir") or (task.workspace or {}).get("output_dir"),
    }


def _task_id_sort_value(task_id: str) -> tuple[int, str]:
    sequence = task_iteration(task_id)
    if sequence is not None:
        return (sequence, task_id)
    try:
        return (int(task_id.removeprefix("T-")), task_id)
    except ValueError:
        return (-1, task_id)


def _build_lineage(
    task_id: str,
    workspace: dict[str, Any],
    lineage: dict[str, Any] | None,
) -> dict[str, Any]:
    if lineage is not None:
        current = dict(lineage)
    else:
        current = {}
    current.setdefault("parent_task_id", None)
    current.setdefault("base_task_id", task_id)
    current.setdefault("chain_root_task_id", format_task_id(str(workspace.get("task_code")), 1))
    current.setdefault("iteration_index", 1)
    current.setdefault("task_code", workspace.get("task_code"))
    current.setdefault("task_date", workspace.get("task_date"))
    if not current.get("agentbc_root"):
        current["agentbc_root"] = workspace.get("agentbc_root") or _agentbc_root_from_workspace(workspace)
    current.setdefault("base_workspace_root", workspace.get("root"))
    current.setdefault("base_artifacts_dir", workspace.get("artifacts_dir"))
    current.setdefault("branch_mode", "linear")
    current.setdefault("chain_id", workspace.get("chain_id") or workspace.get("chain_dir"))
    current.setdefault("chain_token", workspace.get("chain_token"))
    current.setdefault("chain_dir", workspace.get("chain_dir") or Path(str(workspace.get("output_dir", ""))).name)
    current.setdefault("chain_task_id", workspace.get("chain_task_id"))
    current.setdefault("chain_output_dir", workspace.get("chain_output_dir") or workspace.get("output_dir"))
    return current


def _next_lineage(source: TaskModel, workspace: dict[str, Any], branch: bool = False) -> dict[str, Any]:
    current = _lineage_for(source)
    base_task_id = current.get("base_task_id") or source.id
    chain_root_task_id = current.get("chain_root_task_id") or source.id
    iteration_index = int(current.get("iteration_index") or 1) + 1
    return {
        "parent_task_id": source.id,
        "base_task_id": base_task_id,
        "chain_root_task_id": chain_root_task_id,
        "iteration_index": iteration_index,
        "task_code": current.get("task_code") or workspace.get("task_code"),
        "task_date": current.get("task_date") or workspace.get("task_date"),
        "agentbc_root": current.get("agentbc_root") or workspace.get("default_path"),
        "base_workspace_root": current.get("base_workspace_root") or workspace.get("root"),
        "base_artifacts_dir": current.get("base_artifacts_dir") or workspace.get("artifacts_dir"),
        "branch_mode": "explicit_branch" if branch else "linear",
        "chain_id": current.get("chain_id") or workspace.get("chain_id") or workspace.get("chain_dir"),
        "chain_token": current.get("chain_token") or workspace.get("chain_token"),
        "chain_dir": current.get("chain_dir") or workspace.get("chain_dir") or Path(str(workspace.get("output_dir", ""))).name,
        "chain_task_id": f"{iteration_index:03d}",
        "chain_output_dir": current.get("chain_output_dir") or workspace.get("chain_output_dir") or workspace.get("output_dir"),
    }


def _normalize_status(status: str) -> str:
    mapping = {
        "assigned": "running",
        "working": "running",
        "pause_pending": "running",
        "paused": "running",
        "review_required": "input_required",
        "needs_review": "needs_recovery",
        "failed": "failed",
        "in_progress": "running",
    }
    return mapping.get(status, status if status in PUBLIC_TASK_STATUSES else "needs_recovery")


def _has_close_intent(task: TaskModel) -> bool:
    return isinstance((task.extensions or {}).get("agentbc.close_intent"), dict)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _supersede_final_callback(task: TaskModel, state: str, reason: str) -> None:
    extensions = dict(task.extensions or {})
    callback = extensions.pop("agentbc.final_callback", None)
    if isinstance(callback, dict):
        extensions["agentbc.superseded_final_callback"] = {
            **callback,
            "superseded_by": state,
            "superseded_reason": reason,
            "superseded_at": _utc_now(),
        }
    task.extensions = extensions


def _merge_execution(extensions: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(extensions or {})
    execution = dict(merged.get("agentbc.execution") or {})
    execution.update({key: value for key, value in updates.items() if value is not None})
    merged["agentbc.execution"] = execution
    return merged


def _execution_ledger(extensions: dict[str, Any]) -> list[dict[str, Any]]:
    execution = extensions.get("agentbc.execution")
    if not isinstance(execution, dict):
        return []
    intervals = execution.get("run_intervals")
    if not isinstance(intervals, list):
        return []
    return [item for item in intervals if isinstance(item, dict)]


def _finalize_steps(
    steps: list[dict[str, Any]],
    step_results: Any,
) -> list[dict[str, Any]]:
    results_by_id: dict[int, dict[str, Any]] = {}
    if isinstance(step_results, list):
        for item in step_results:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                results_by_id[item["id"]] = item
    finalized: list[dict[str, Any]] = []
    for index, step in enumerate(steps, 1):
        step_id = step.get("id", index)
        result = results_by_id.get(step_id)
        if result is None:
            finalized.append(dict(step))
            continue
        finalized.append(
            _update_step(
                step,
                step_id,
                {"status": result["status"], "executor_result": result},
            )
        )
    return finalized
