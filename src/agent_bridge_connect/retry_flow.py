"""FLOW-104-003 failed-task full retry lifecycle.

This module owns the narrow failed-current-head retry transaction. It keeps
the task identity and frozen policy snapshots stable while resetting execution
state and applying the exact report/artifact cleanup boundary. Reservation,
preflight, validation and public projection are owned exclusively by the
shared :mod:`agent_bridge_connect.revival` protocol.
"""

from __future__ import annotations

import copy
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .revival import (
    REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS,
    REVIVAL_EXTENSION_KEY,
    REVIVAL_INPUT_UNRESOLVED,
    REVIVAL_PATH_PLAN_INVALID,
    REVIVAL_REQUIREMENTS_UNREADABLE,
    REVIVAL_RESERVATION_CONFLICT,
    REVIVAL_RUN_LEASE_OPEN,
    REVIVAL_SESSION_CLEANUP_UNSTABLE,
    REVIVAL_SOURCE_NOT_CHAIN_HEAD,
    REVIVAL_SOURCE_STATUS_INVALID,
    REVIVAL_WORKER_ACTIVE,
    RevivalPreflight,
    build_revival_reservation,
    commit_revival_reservation,
    evaluate_revival_preflight,
    revival_digest,
    revival_facts_from_task,
    revival_path_plan_digest,
    revival_policy_digest,
    revival_public_view,
    revival_status_view,
    validate_revival_reservation,
)

from .execution_policy import RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY, build_session_snapshot
from .path_model import validate_path_plan_workspace
from .permission_elevation import PERMISSION_ELEVATION_EXTENSION_KEY
from .permission_modes import PERMISSION_EXTENSION_KEY
from .protocol import ABCError
from .run_lease import RunLeaseState, close_lease, load_lease
from .session import (
    RECOVERY_FILE,
    SESSION_RECEIPT_FILE,
    SESSION_STATE_FILE,
    control_root_for_task,
)
from .task_health import clear_task_progress
from .terminal_delivery import TERMINAL_DELIVERY_EXTENSION_KEY


_RETRY_WRITE_LOCK = threading.RLock()
REVIVAL_CLEANUP_FAILED = "revival_cleanup_failed"
REVIVAL_REPORT_PATH_INVALID = "revival_report_path_invalid"
REVIVAL_STEP_FLAG_UNSUPPORTED = "retry_step_not_supported"
_ACTIVE_WORKER_STATES = {"accepted", "dispatching", "starting", "running"}
_UNSTABLE_CLEANUP_STATES = {
    "pending",
    "requested",
    "running",
    "failed",
    "retry_wait",
    "waiting_for_cleanup",
    "waiting_for_desktop",
    "archiving",
    "deleting",
}
_STABLE_CLEANUP_STATES = {
    "",
    "not_requested",
    "succeeded",
    "retained",
    "unsupported",
    "legacy",
}
_EXECUTION_DYNAMIC_KEYS = {
    "worker_run_id",
    "worker_pid",
    "executor_run_id",
    "dispatch_status",
    "monitor_status",
    "monitor_message",
    "waiting_since",
    "requeued_at",
}
_STEP_RUNTIME_KEYS = {
    "result",
    "error",
    "artifacts",
    "verification",
    "tests",
    "checks",
    "changed_files",
    "started_at",
    "completed_at",
}
_SESSION_ACTIVE_STATE_FILES = (
    SESSION_RECEIPT_FILE,
    SESSION_STATE_FILE,
    RECOVERY_FILE,
    "permission_block_ledger.json",
    "claude_sdk_hooks.jsonl",
    "claude_sdk_hooks_session.json",
    "responses",
)


class FailedTaskRetryFlow:
    """Perform one serialized retry reservation and cleanup transaction."""

    def __init__(self, service: Any):
        self.service = service

    def preflight(self, task_id: str) -> dict[str, Any]:
        try:
            task = self.service.get_task(task_id)
        except ABCError as exc:
            return {
                "ok": False,
                "task_id": str(task_id),
                "errors": [{"code": exc.code, "message": str(exc)}],
                "allowed_next_actions": [],
                "recommended_action": "",
            }

        errors: list[dict[str, Any]] = []
        if str(task.status) != "failed":
            errors.append(
                _error(
                    REVIVAL_SOURCE_STATUS_INVALID,
                    f"Task {task.id} is {task.status}; failed task full retry requires failed.",
                    allowed_statuses=["failed"],
                )
            )
        try:
            chain = self.service.resolve_chain(task.id)
        except ABCError as exc:
            chain = None
            errors.append(_error("revival_invalid_lineage", str(exc)))
        if chain is not None:
            if chain.anomalies:
                errors.append(
                    _error(
                        "revival_invalid_lineage",
                        "Task lineage is inconsistent; repair the chain before retry.",
                        anomalies=chain.anomalies,
                    )
                )
            elif len(chain.head_task_ids) != 1:
                errors.append(
                    _error(
                        REVIVAL_SOURCE_NOT_CHAIN_HEAD,
                        "Retry requires exactly one current chain head.",
                        head_task_ids=chain.head_task_ids,
                    )
                )
            elif not chain.requested_is_head:
                errors.append(
                    _error(
                        REVIVAL_SOURCE_NOT_CHAIN_HEAD,
                        f"Task {task.id} is not the current chain head; use {chain.current_head_task_id}.",
                        current_head_task_id=chain.current_head_task_id,
                    )
                )

        if self.service.store.is_leased(task.id):
            errors.append(
                _error(
                    REVIVAL_RUN_LEASE_OPEN,
                    f"Task {task.id} has an active task lease; stop it before retry.",
                )
            )

        run_lease = load_lease(task.id, self.service.board_root)
        if run_lease is not None and run_lease.state != RunLeaseState.CLOSED:
            errors.append(
                _error(
                    REVIVAL_RUN_LEASE_OPEN,
                    f"Task {task.id} RunLease is {run_lease.state}; it must be closed before retry.",
                    run_lease_state=run_lease.state,
                )
            )

        extensions = dict(task.extensions or {})
        execution = dict(extensions.get("agentbc.execution") or {})
        if _active_worker_projection(execution):
            errors.append(
                _error(
                    REVIVAL_WORKER_ACTIVE,
                    f"Task {task.id} still has an active worker or dispatch projection.",
                )
            )
        request = extensions.get("agentbc.input")
        if isinstance(request, dict) and str(request.get("status") or "") == "waiting":
            errors.append(
                _error(
                    REVIVAL_INPUT_UNRESOLVED,
                    f"Task {task.id} has unresolved input {request.get('input_id', '')}; answer or invalidate it first.",
                    input_id=request.get("input_id", ""),
                )
            )

        session = extensions.get(SESSION_EXTENSION_KEY)
        cleanup_state = ""
        if isinstance(session, dict):
            cleanup = session.get("cleanup")
            cleanup_state = str(cleanup.get("state") or "") if isinstance(cleanup, dict) else ""
            if cleanup_state in _UNSTABLE_CLEANUP_STATES or cleanup_state not in _STABLE_CLEANUP_STATES:
                errors.append(
                    _error(
                        REVIVAL_SESSION_CLEANUP_UNSTABLE,
                        f"Task {task.id} session cleanup is not stable ({cleanup_state or 'invalid'}).",
                        cleanup_state=cleanup_state or "invalid",
                    )
                )
            session_state = str(session.get("session_state") or "").strip().lower()
            if session_state in {"active", "input_required", "resuming", "needs_recovery"}:
                errors.append(
                    _error(
                        REVIVAL_SESSION_CLEANUP_UNSTABLE,
                        f"Task {task.id} executor session is still {session_state}.",
                        session_state=session_state,
                    )
                )

        workspace = dict(task.workspace or {})
        try:
            validate_path_plan_workspace(workspace)
        except ABCError as exc:
            errors.append(_error(REVIVAL_PATH_PLAN_INVALID, str(exc)))

        requirements_digest = ""
        report_digest = ""
        try:
            requirements_path, report_path = _authoritative_report_paths(task)
        except ABCError as exc:
            requirements_path = report_path = None
            errors.append(_error(exc.code, str(exc), **(exc.details or {})))
        if requirements_path is not None:
            try:
                requirements_digest = revival_digest(requirements_path.read_bytes())
            except OSError as exc:
                errors.append(
                    _error(
                        REVIVAL_REQUIREMENTS_UNREADABLE,
                        f"Task brief is not readable: {requirements_path}",
                        error=str(exc),
                    )
                )
        if report_path is not None and report_path.exists():
            try:
                if report_path.is_symlink() or not report_path.is_file():
                    raise OSError("report is not a regular file")
                report_digest = revival_digest(report_path.read_bytes())
            except OSError as exc:
                errors.append(
                    _error(
                        "revival_report_unreadable",
                        f"Failure report is not readable: {report_path}",
                        error=str(exc),
                    )
                )
        elif report_path is not None:
            # A failed task can legitimately lack a report after an earlier
            # terminal-delivery failure; cleanup remains idempotent.
            report_digest = ""

        existing = extensions.get(REVIVAL_EXTENSION_KEY)
        if isinstance(existing, dict) and existing.get("state") == "reserved":
            errors.append(
                _error(
                    REVIVAL_RESERVATION_CONFLICT,
                    f"Task {task.id} already has an in-progress retry reservation.",
                    revival_id=existing.get("revival_id", ""),
                )
            )

        source_attempt_index = _source_attempt_index(task)
        attempt_index = source_attempt_index + 1
        cleanup_scope = REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS
        path_digest = revival_path_plan_digest(workspace)
        policy_digest = revival_policy_digest(extensions)
        warnings: list[str] = []
        if not report_digest:
            warnings.append("source_report_absent")
        worker_active = _active_worker_projection(execution)
        facts = revival_facts_from_task(
            task.to_dict(),
            is_chain_head=(
                chain is not None
                and not chain.anomalies
                and len(chain.head_task_ids) == 1
                and chain.requested_is_head
            ),
            lease_state=run_lease.state if run_lease is not None else "missing",
            worker_active=worker_active,
            dispatch_active=False,
            requirements_readable=bool(requirements_digest),
            lineage_valid=(chain is not None and not chain.anomalies),
            path_plan_valid=not any(
                item["code"] == REVIVAL_PATH_PLAN_INVALID for item in errors
            ),
            report_state=("readable" if report_digest else "absent"),
            warnings=warnings,
        )
        raw_cleanup = (
            session.get("cleanup")
            if isinstance(session, dict)
            and isinstance(session.get("cleanup"), dict)
            else {}
        )
        raw_cleanup_state = str(raw_cleanup.get("state") or "").lower()
        if raw_cleanup_state in {"retained", "succeeded", "unsupported"}:
            facts["session_cleanup_state"] = raw_cleanup_state
        shared_preflight = evaluate_revival_preflight(
            facts,
            requested_operation="retry",
        )
        existing_codes = {str(item.get("code") or "") for item in errors}
        for code in shared_preflight.error_codes:
            if code not in existing_codes:
                errors.append(
                    _error(
                        code,
                        f"Shared revival preflight rejected retry: {code}",
                    )
                )
                existing_codes.add(code)
        warnings = list(shared_preflight.warnings)
        return {
            "ok": not errors,
            "version": 1,
            "operation": "retry",
            "task_id": task.id,
            "source_task_id": task.id,
            "target_task_id": task.id,
            "source_attempt_index": source_attempt_index,
            "target_attempt_index": attempt_index,
            "errors": errors,
            "allowed_next_actions": (
                [] if errors else list(shared_preflight.allowed_next_actions)
            ),
            "recommended_action": (
                "" if errors else shared_preflight.recommended_action
            ),
            "attempt_index": attempt_index,
            "cleanup_scope": cleanup_scope,
            "customer_artifacts_preserved": bool(workspace.get("customer_dir")),
            "path_plan_digest": path_digest,
            "policy_digest": policy_digest,
            "requirements_digest": requirements_digest,
            "report_digest": report_digest,
            "warnings": warnings,
            "run_lease_state": run_lease.state if run_lease is not None else "closed",
            "cleanup_state": cleanup_state or "not_requested",
        }

    def execute(self, task_id: str) -> Any:
        # Avoid creating a lock directory for an invalid task reference.
        self.service.get_task(task_id)
        with _retry_transaction_lock(self.service, task_id):
            task = self.service.get_task(task_id)
            preflight = self.preflight(task.id)
            if not preflight["ok"]:
                first = preflight["errors"][0]
                raise ABCError(
                    str(first["code"]),
                    str(first["message"]),
                    {**preflight, "preflight": preflight},
                )

            source_snapshot = copy.deepcopy(task.to_dict())
            source_attempt = _source_attempt_index(task)
            target_attempt = int(preflight["attempt_index"])
            record = build_revival_reservation(
                operation="retry",
                source_task_id=task.id,
                source_attempt_id=f"attempt-{source_attempt}",
                target_attempt_id=f"attempt-{target_attempt}",
                steps=task.steps,
                path_plan_digest=str(preflight["path_plan_digest"]),
                policy_digest=str(preflight["policy_digest"]),
                source_requirements_digest=str(preflight["requirements_digest"]),
                source_report_digest=str(preflight["report_digest"]),
            )
            record["warnings"] = list(preflight["warnings"])
            record_errors = validate_revival_reservation(record)
            if record_errors:
                raise ABCError(
                    "revival_reservation_invalid",
                    "; ".join(record_errors),
                    {"record_errors": record_errors},
                )
            reserved_task = copy.deepcopy(task)
            reserved_extensions = dict(reserved_task.extensions or {})
            reserved_extensions[REVIVAL_EXTENSION_KEY] = record
            reserved_task.extensions = reserved_extensions
            reserved_task.updated_at = _utc_now()
            self.service.store.write_task(task.id, _without_none(reserved_task.to_dict()))

            cleanup = _RetryCleanupTransaction(
                task,
                record,
                board_root=self.service.board_root,
                source_attempt_index=source_attempt,
            )
            try:
                cleanup.perform()
                committed = _prepare_retry_task(
                    reserved_task,
                    record,
                    self.service,
                    target_attempt=target_attempt,
                )
                self.service.store.write_task(task.id, _without_none(committed.to_dict()))
                cleanup.commit()
            except Exception as exc:
                rollback_error: Exception | None = None
                try:
                    cleanup.rollback()
                except Exception as rollback_exc:  # noqa: BLE001 - preserve source state
                    rollback_error = rollback_exc
                try:
                    self.service.store.write_task(task.id, source_snapshot)
                except Exception as restore_exc:  # noqa: BLE001 - surface the stronger failure
                    rollback_error = rollback_error or restore_exc
                if isinstance(exc, ABCError):
                    raise
                raise ABCError(
                    REVIVAL_CLEANUP_FAILED,
                    f"Failed-task retry cleanup failed: {exc}",
                    {
                        "task_id": task.id,
                        "revival_id": record.get("revival_id"),
                        "cleanup_scope": record.get("cleanup_scope"),
                        "rollback_complete": rollback_error is None,
                        "rollback_error": str(rollback_error) if rollback_error else "",
                    },
                ) from exc

            self.service.store.append_event(
                task.id,
                {
                    "event_type": "task.retry",
                    "task_id": task.id,
                    "created_at": committed.updated_at,
                    "attempt_index": target_attempt,
                    "cleanup_scope": record.get("cleanup_scope"),
                    "revival_id": record.get("revival_id"),
                },
            )
            try:
                self.service._append_intervention(
                    task.id,
                    "retry",
                    committed.updated_at,
                    attempt_index=target_attempt,
                    cleanup_scope=record.get("cleanup_scope"),
                    revival_id=record.get("revival_id"),
                )
            except AttributeError:
                pass
            self.service._refresh_task_index()
            return self.service.get_task(task.id)


def failed_retry_preflight(service: Any, task_id: str) -> dict[str, Any]:
    return FailedTaskRetryFlow(service).preflight(task_id)


def retry_failed_task(service: Any, task_id: str) -> Any:
    return FailedTaskRetryFlow(service).execute(task_id)


def _prepare_retry_task(
    task: Any,
    record: dict[str, Any],
    service: Any,
    *,
    target_attempt: int,
) -> Any:
    now = _utc_now()
    task.status = "pending"
    task.updated_at = now
    task.report = None
    intervention = dict(task.intervention or {})
    intervention.pop("paused", None)
    intervention.pop("pause_reason", None)
    task.intervention = intervention
    task.steps = [_reset_step(step, index) for index, step in enumerate(task.steps, 1)]
    extensions = dict(task.extensions or {})

    current_input = extensions.pop("agentbc.input", None)
    history = [item for item in extensions.get("agentbc.input_history", []) if isinstance(item, dict)]
    if isinstance(current_input, dict):
        revoked = dict(current_input)
        revoked.update(
            {
                "status": "revoked",
                "revoked_at": now,
                "revoked_reason": "failed_task_retry",
            }
        )
        history.append(revoked)
    if history:
        extensions["agentbc.input_history"] = history[-16:]

    # Only the live runtime projections are removed.  The append-only error,
    # event, intervention and historical input ledgers remain untouched.
    extensions.pop("agentbc.completion_intent", None)
    extensions.pop("agentbc.final_callback", None)
    extensions.pop("agentbc.permission_runtime", None)
    extensions.pop(PERMISSION_ELEVATION_EXTENSION_KEY, None)
    extensions.pop(TERMINAL_DELIVERY_EXTENSION_KEY, None)
    execution = dict(extensions.get("agentbc.execution") or {})
    for key in _EXECUTION_DYNAMIC_KEYS:
        execution.pop(key, None)
    execution.update(
        {
            "internal_status": "pending",
            "lease_state": RunLeaseState.CLOSED,
            "attempt_index": int(target_attempt),
            "attempt_started_at": now,
        }
    )
    extensions["agentbc.execution"] = execution

    session = extensions.get(SESSION_EXTENSION_KEY)
    if isinstance(session, dict):
        new_session_id = (
            str(uuid.uuid4()) if str(task.assignee).lower() == "claude" else ""
        )
        extensions[SESSION_EXTENSION_KEY] = build_session_snapshot(
            task.assignee,
            retain=bool(session.get("retain")),
            session_id=new_session_id,
            project_mode=session.get("project_mode"),
            project_path=str(session.get("project_path") or ""),
            session_state="pending",
            created_at=now,
        )

    record = commit_revival_reservation(
        record,
        target_attempt_id=f"attempt-{target_attempt}",
        now=now,
    )
    extensions[REVIVAL_EXTENSION_KEY] = record
    task.extensions = extensions
    service.revoke_permission_grant(task.id, "task_retry", model=task)
    _close_old_lease(task.id, service)
    _clear_retry_progress(task)
    return task


class _RetryCleanupTransaction:
    def __init__(
        self,
        task: Any,
        record: dict[str, Any],
        *,
        board_root: str | Path,
        source_attempt_index: int,
    ):
        self.task = task
        self.record = record
        self.board_root = Path(board_root).expanduser().resolve()
        self.source_attempt_index = max(int(source_attempt_index), 0)
        self.report_path: Path | None = None
        self.report_quarantine: Path | None = None
        self.artifact_root: Path | None = None
        self.artifact_quarantine: Path | None = None
        self.artifact_existed = False
        self.artifact_created = False
        self.session_archive_root: Path | None = None
        self.session_state_moves: list[tuple[Path, Path]] = []

    def perform(self) -> None:
        self._archive_active_session_state()
        _, self.report_path = _authoritative_report_paths(self.task)
        if self.report_path.exists():
            if self.report_path.is_symlink() or not self.report_path.is_file():
                raise ABCError(
                    REVIVAL_REPORT_PATH_INVALID,
                    f"Failure report is not a regular file: {self.report_path}",
                )
            self.report_quarantine = self.report_path.parent / (
                f".agentbc-revival-report-{self.record['revival_id']}"
            )
            if self.report_quarantine.exists():
                raise ABCError(
                    REVIVAL_RESERVATION_CONFLICT,
                    f"Retry report quarantine already exists: {self.report_quarantine}",
                )
            os.replace(self.report_path, self.report_quarantine)

        if (
            self.record["cleanup_scope"]
            == REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS
            and not bool((self.task.workspace or {}).get("customer_dir"))
        ):
            workspace = self.task.workspace or {}
            self.artifact_root = _managed_artifact_root(workspace)
            if self.artifact_root.exists():
                if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
                    raise ABCError(
                        REVIVAL_PATH_PLAN_INVALID,
                        f"Managed artifact root is not a regular directory: {self.artifact_root}",
                    )
                self.artifact_existed = True
                self.artifact_quarantine = self.artifact_root.parent / (
                    f".agentbc-revival-artifacts-{self.record['revival_id']}"
                )
                if self.artifact_quarantine.exists():
                    raise ABCError(
                        REVIVAL_RESERVATION_CONFLICT,
                        f"Retry artifact quarantine already exists: {self.artifact_quarantine}",
                    )
                os.replace(self.artifact_root, self.artifact_quarantine)
            self.artifact_root.mkdir(parents=True, exist_ok=False)
            self.artifact_created = True

    def commit(self) -> None:
        if self.artifact_quarantine is not None and self.artifact_quarantine.exists():
            shutil.rmtree(self.artifact_quarantine)
        if self.report_quarantine is not None:
            self.report_quarantine.unlink(missing_ok=True)
        self._remove_empty_parents()

    def rollback(self) -> None:
        if self.artifact_root is not None and self.artifact_created:
            if self.artifact_root.is_symlink() or self.artifact_root.is_file():
                self.artifact_root.unlink(missing_ok=True)
            elif self.artifact_root.is_dir():
                shutil.rmtree(self.artifact_root)
        if self.artifact_quarantine is not None and self.artifact_quarantine.exists():
            os.replace(self.artifact_quarantine, self.artifact_root)
        if self.report_quarantine is not None and self.report_quarantine.exists():
            os.replace(self.report_quarantine, self.report_path)
        for source, archived in reversed(self.session_state_moves):
            if archived.exists():
                os.replace(archived, source)
        if self.session_archive_root is not None:
            try:
                self.session_archive_root.rmdir()
                self.session_archive_root.parent.rmdir()
            except OSError:
                pass
        self._remove_empty_parents()

    def _archive_active_session_state(self) -> None:
        control_root = control_root_for_task(
            self.task.id,
            board_root=self.board_root,
        )
        active = [
            control_root / name
            for name in _SESSION_ACTIVE_STATE_FILES
            if (control_root / name).exists()
        ]
        if not active:
            return
        archive_root = (
            control_root
            / "attempts"
            / f"attempt-{self.source_attempt_index}"
        )
        if archive_root.exists():
            raise ABCError(
                REVIVAL_RESERVATION_CONFLICT,
                f"Retry session archive already exists for attempt-{self.source_attempt_index}.",
            )
        archive_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.session_archive_root = archive_root
        try:
            for source in active:
                archived = archive_root / source.name
                os.replace(source, archived)
                self.session_state_moves.append((source, archived))
        except Exception:
            for source, archived in reversed(self.session_state_moves):
                if archived.exists():
                    os.replace(archived, source)
            self.session_state_moves.clear()
            try:
                archive_root.rmdir()
                archive_root.parent.rmdir()
            except OSError:
                pass
            self.session_archive_root = None
            raise

    def _remove_empty_parents(self) -> None:
        for path in (self.report_path.parent if self.report_path else None, self.artifact_root.parent if self.artifact_root else None):
            if path is None:
                continue
            quarantine_parent = path / ".agentbc-revival"
            try:
                quarantine_parent.rmdir()
            except OSError:
                pass


@contextmanager
def _retry_transaction_lock(service: Any, task_id: str) -> Iterator[None]:
    """Serialize retry reservations in-process and across CLI processes."""
    task_dir = service.store.task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_path = task_dir / ".retry-flow.lock"
    with _RETRY_WRITE_LOCK:
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()


def _authoritative_report_paths(task: Any) -> tuple[Path, Path]:
    workspace = task.workspace or {}
    report_root_text = str(workspace.get("report_root") or "").strip()
    task_file_text = str(workspace.get("task_file") or "").strip()
    report_file_text = str(workspace.get("report_file") or "").strip()
    if not report_root_text or not task_file_text or not report_file_text:
        raise ABCError(
            REVIVAL_PATH_PLAN_INVALID,
            "Retry requires report_root, task_file, and report_file in the authoritative PathPlan.",
        )
    report_root = Path(report_root_text).expanduser().resolve()
    task_file = Path(task_file_text).expanduser()
    report_file = Path(report_file_text).expanduser()
    expected_report = report_root / f"{task.id}-report.md"
    expected_task = report_root / f"{task.id}-task.md"
    if task_file.resolve() != expected_task or report_file.resolve() != expected_report:
        raise ABCError(
            REVIVAL_REPORT_PATH_INVALID,
            "Retry report paths do not match the authoritative task PathPlan.",
            {"expected_task_file": str(expected_task), "expected_report_file": str(expected_report)},
        )
    if task_file.resolve() == report_file.resolve():
        raise ABCError(
            REVIVAL_REQUIREMENTS_UNREADABLE,
            "Retry cannot preserve a task brief when task_file and report_file are the same file.",
        )
    if report_file.exists():
        try:
            same_file = os.path.samefile(task_file, report_file)
        except OSError:
            same_file = False
        if same_file:
            raise ABCError(
                REVIVAL_REPORT_PATH_INVALID,
                "Retry cannot remove a failure report that shares the task brief inode.",
            )
    if not task_file.is_file():
        raise ABCError(
            REVIVAL_REQUIREMENTS_UNREADABLE,
            f"Task brief is missing or unreadable: {task_file}",
        )
    return task_file, report_file


def _managed_artifact_root(workspace: dict[str, Any]) -> Path:
    artifact_text = str(workspace.get("artifact_root") or workspace.get("artifacts_dir") or "")
    artifact_input = Path(artifact_text).expanduser()
    if artifact_input.is_symlink():
        raise ABCError(
            REVIVAL_PATH_PLAN_INVALID,
            "Managed artifact root must not be a symlink.",
        )
    artifact = artifact_input.resolve()
    agentbc_root = Path(str(workspace.get("agentbc_root") or "")).expanduser().resolve()
    managed = agentbc_root / "tasks" / "artifacts"
    try:
        relative = artifact.relative_to(managed)
    except ValueError as exc:
        raise ABCError(
            REVIVAL_PATH_PLAN_INVALID,
            "Managed artifact root is outside AgentBC workspace/tasks/artifacts.",
        ) from exc
    if artifact == managed or len(relative.parts) < 2:
        raise ABCError(
            REVIVAL_PATH_PLAN_INVALID,
            "Retry requires a task-scoped managed artifact root.",
        )
    return artifact


def _path_plan_projection(workspace: dict[str, Any]) -> dict[str, Any]:
    return {key: workspace.get(key) for key in sorted(workspace) if key not in {"internal_task_dir"}}


def _frozen_policy_projection(extensions: dict[str, Any]) -> dict[str, Any]:
    session = extensions.get(SESSION_EXTENSION_KEY)
    session_policy = {}
    if isinstance(session, dict):
        session_policy = {
            key: session.get(key)
            for key in ("executor", "retain", "project_mode", "project_path")
        }
    return {
        PERMISSION_EXTENSION_KEY: copy.deepcopy(extensions.get(PERMISSION_EXTENSION_KEY)),
        RESOURCE_EXTENSION_KEY: copy.deepcopy(extensions.get(RESOURCE_EXTENSION_KEY)),
        "agentbc.input_policy": copy.deepcopy(extensions.get("agentbc.input_policy")),
        SESSION_EXTENSION_KEY: session_policy,
    }


def _active_worker_projection(execution: dict[str, Any]) -> bool:
    worker_pid = execution.get("worker_pid")
    if isinstance(worker_pid, int) and worker_pid > 0 and _pid_alive(worker_pid):
        return True
    dispatch_status = str(execution.get("dispatch_status") or "").strip().lower()
    worker_run_id = str(execution.get("worker_run_id") or "").strip()
    if dispatch_status in _ACTIVE_WORKER_STATES and worker_run_id and not (
        isinstance(worker_pid, int) and worker_pid > 0
    ):
        return True
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _source_attempt_index(task: Any) -> int:
    extensions = getattr(task, "extensions", None) or {}
    execution = extensions.get("agentbc.execution")
    if not isinstance(execution, dict):
        return 0
    try:
        number = int(execution.get("attempt_index") or 0)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _step_id(step: Any, fallback: int) -> int:
    try:
        return int(step.get("id", fallback))
    except (AttributeError, TypeError, ValueError):
        return fallback


def _reset_step(step: Any, fallback: int) -> dict[str, Any]:
    reset = dict(step) if isinstance(step, dict) else {"id": fallback, "description": str(step)}
    reset["id"] = _step_id(reset, fallback)
    reset["status"] = "pending"
    for key in _STEP_RUNTIME_KEYS:
        reset.pop(key, None)
    return reset


def _close_old_lease(task_id: str, service: Any) -> None:
    run_lease = load_lease(task_id, service.board_root)
    if run_lease is not None and run_lease.state != RunLeaseState.CLOSED:
        close_lease(run_lease, service.board_root)
    release = getattr(service, "_release_lease", None)
    if callable(release):
        release(task_id)


def _clear_retry_progress(task: Any) -> None:
    """Clear AgentBC runtime progress without touching a customer project."""
    workspace = task.workspace or {}
    if workspace.get("customer_dir"):
        runtime_text = str(workspace.get("internal_task_dir") or "").strip()
        project_text = str(workspace.get("project_root") or "").strip()
        if runtime_text and project_text:
            runtime = Path(runtime_text).expanduser().resolve()
            project = Path(project_text).expanduser().resolve()
            try:
                runtime.relative_to(project)
            except ValueError:
                pass
            else:
                return
    clear_task_progress(task, remove_log=True)


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _without_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def public_revival_projection(value: Any) -> dict[str, Any] | None:
    """Backward-compatible import name backed by the canonical v1 view."""

    return revival_public_view(value)


def failed_revival_projection(
    task: Any,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed canonical projection without inventing eligibility."""

    extensions = getattr(task, "extensions", None) or {}
    if isinstance(preflight, dict):
        errors = tuple(
            str(item.get("code") or "")
            for item in preflight.get("errors") or []
            if isinstance(item, dict) and item.get("code")
        )
        shared = RevivalPreflight(
            ok=not errors,
            error_codes=errors,
            allowed_next_actions=(
                tuple(preflight.get("allowed_next_actions") or ())
                if not errors
                else ()
            ),
            recommended_action=(
                str(preflight.get("recommended_action") or "")
                if not errors
                else ""
            ),
            warnings=tuple(preflight.get("warnings") or ()),
        )
    else:
        facts = revival_facts_from_task(
            task.to_dict() if hasattr(task, "to_dict") else {},
            is_chain_head=False,
            lease_state="",
            worker_active=False,
            dispatch_active=False,
        )
        shared = evaluate_revival_preflight(facts)
    return revival_status_view(shared, extensions.get(REVIVAL_EXTENSION_KEY))
