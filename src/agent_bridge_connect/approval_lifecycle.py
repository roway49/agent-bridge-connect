"""TaskService-owned approval and permission-response lifecycle.

The mixin owns lifecycle transitions while its host owns the task store,
leases, chain resolution, progress, and terminal recovery operations. The host
contract is a Protocol rather than a service import, so this module has no
service/control cycle. Receipt schema and projection authority remains
exclusively agent_bridge_connect.approval.
"""

from __future__ import annotations

import functools
import importlib
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .approval import (
    APPROVAL_EXTENSION_KEY, APPROVAL_SCOPE, APPROVAL_V3_SCOPE,
    build_approval_receipt, build_approval_receipt_v2,
    build_approval_receipt_v3, normalize_reason_summary,
    record_approval_decision,
    record_approval_full_continuation, record_approval_notification,
    sanitize_reason_detail, validate_approval_receipt,
)
from .permission_elevation import (
    PERMISSION_ELEVATION_EXTENSION_KEY, PERMISSION_ELEVATION_MODE,
    activate_permission_elevation, block_permission_elevation,
    build_permission_elevation, permission_elevation_from_extensions,
    record_permission_elevation_decision, record_permission_elevation_notification,
    verify_permission_elevation,
)
from .permission_failures import (
    PERMISSION_BLOCKED_STEP_CARDINALITY_INVALID,
    PERMISSION_CHAIN_HEAD_AMBIGUOUS, PERMISSION_CHAIN_HEAD_STALE,
    PERMISSION_EXECUTOR_SESSION_MISMATCH,
    PERMISSION_EXECUTOR_SESSION_RUN_MISMATCH, PERMISSION_INPUT_INVALID,
    PERMISSION_MODE_UNSUPPORTED, PERMISSION_REQUESTED_SCOPE_INVALID,
    PERMISSION_RESUME_SESSION_MISSING, PERMISSION_RUN_LEASE_INVALID,
    PERMISSION_RUN_LEASE_RUN_MISMATCH, PERMISSION_SESSION_RECEIPT_INVALID,
    PERMISSION_SESSION_STATE_STALE, PERMISSION_SESSION_SNAPSHOT_INVALID,
    PERMISSION_WAIT_COMPATIBILITY_CODE, PermissionWaitFailure,
    permission_wait_failure,
)
from .permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
    revoke_permission_grant as revoke_grant_contract,
)
from .permission_modes import (
    PERMISSION_EXTENSION_KEY, permission_record_from_extensions,
    permission_runtime_policy,
)
from .execution_policy import SESSION_EXTENSION_KEY, validate_session_snapshot
from .migration import assert_maintenance_command_allowed
from .protocol import ABCError, TaskModel
from .terminal_states import TASK_TERMINAL_STATES

_claude_elevation_contract = importlib.import_module(
    "." + "claude_" + "elevation", __package__
)
CLAUDE_ELEVATION_ACTIVE = _claude_elevation_contract.CLAUDE_ELEVATION_ACTIVE
CLAUDE_ELEVATION_DENIED = _claude_elevation_contract.CLAUDE_ELEVATION_DENIED
CLAUDE_ELEVATION_EXTENSION_KEY = (
    _claude_elevation_contract.CLAUDE_ELEVATION_EXTENSION_KEY
)
CLAUDE_ELEVATION_PENDING = _claude_elevation_contract.CLAUDE_ELEVATION_PENDING
build_claude_elevation_receipt = (
    _claude_elevation_contract.build_claude_elevation_receipt
)
claude_elevation_from_extensions = (
    _claude_elevation_contract.claude_elevation_from_extensions
)
claude_elevation_public_projection = (
    _claude_elevation_contract.claude_elevation_public_projection
)
record_claude_elevation_dialog = _claude_elevation_contract.record_claude_elevation_dialog
stable_claude_input_digest = _claude_elevation_contract.stable_input_digest
transition_claude_elevation = _claude_elevation_contract.transition_claude_elevation
validate_claude_elevation_receipt = (
    _claude_elevation_contract.validate_claude_elevation_receipt
)

RUNNING_TASK_STATUSES = {
    "running", "input_required", "assigned", "working",
    "pause_pending", "paused", "in_progress",
}
REPORTABLE_TASK_STATUSES = set(TASK_TERMINAL_STATES)
PUBLIC_TASK_STATUSES = {
    "pending", "running", "input_required", "completed",
    "failed", "cancelled", "rejected", "needs_recovery",
}
DEFAULT_INPUT_WAIT_SECONDS = 24 * 60 * 60
PERMISSION_DIALOG_TIMEOUT_RESPONSE = "agentbc_permission_dialog_timeout"
PERMISSION_DIALOG_CLOSED_RESPONSE = "agentbc_permission_dialog_closed"

_TASK_ELEVATION_WRITE_LOCK = threading.RLock()

def _serialize_task_elevation_write(function: Any) -> Any:
    """Serialize in-process approval writes across TaskService instances."""
    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _TASK_ELEVATION_WRITE_LOCK:
            return function(*args, **kwargs)
    return wrapped

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

def _merge_execution(extensions: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(extensions or {})
    execution = dict(merged.get("agentbc.execution") or {})
    execution.update({key: value for key, value in updates.items() if value is not None})
    merged["agentbc.execution"] = execution
    return merged

class ApprovalLifecycleHost(Protocol):
    """Host surface required by ApprovalLifecycleMixin."""

    board_root: Path
    store: Any

    def get_task(self, task_id: str) -> TaskModel: ...
    def resolve_chain(self, task_id: str) -> Any: ...
    def mark_task_needs_recovery(self, *args: Any, **kwargs: Any) -> bool: ...
    def _mark_task_failed_model(self, *args: Any, **kwargs: Any) -> bool: ...
    def _record_run_interval(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def _release_lease(self, task_id: str) -> None: ...
    def _refresh_task_index(self) -> None: ...
    def _sync_terminal_report(self, task_id: str) -> None: ...
    def _apply_executor_session_result(self, *args: Any, **kwargs: Any) -> None: ...


class ApprovalLifecycleMixin:
    """Approval and permission transitions mixed into a TaskService host."""

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

    def revoke_permission_grant_for_target_run(
        self,
        task_id: str,
        code: str,
        *,
        target_run_id: str,
        expected_grant: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Durably revoke a consumed grant bound to one executor run.

        GGQN-002: the transport-lifecycle revocation path.  Reloads the task
        through the TaskService store, verifies the persisted grant is the
        exact consumed grant backing ``target_run_id`` (same ``grant_id`` and
        ``binding.target_run_id`` when ``expected_grant`` is supplied),
        revokes it with a stable reason code, and writes the envelope back
        durably.  Returns the revoked envelope on success, or ``None`` when
        the persisted grant is missing, does not match the expected binding,
        is not the run's consumed grant, or fails validation — callers must
        treat ``None`` as a fail-closed revocation failure.  ``OSError`` from
        the durable write propagates to the caller.
        """
        current = self.get_task(task_id)
        extensions = dict(current.extensions or {})
        persisted = extensions.get(PERMISSION_GRANT_EXTENSION_KEY)
        if not isinstance(persisted, dict):
            return None
        if expected_grant is not None:
            # The caller mirrors the exact grant envelope the Runner consumed
            # for this run.  Both the grant id AND the target-run binding
            # must match the persisted envelope, so a transport holding a
            # grant bound to a different run can never revoke it (GGQN-002
            # fail-closed wrong-identity rejection).
            expected_id = str((expected_grant or {}).get("grant_id") or "")
            if expected_id and str(persisted.get("grant_id") or "") != expected_id:
                return None
            expected_binding = (expected_grant or {}).get("binding")
            expected_binding_map = (
                expected_binding if isinstance(expected_binding, dict) else {}
            )
            expected_target = str(expected_binding_map.get("target_run_id") or "").strip()
            persisted_binding = persisted.get("binding")
            persisted_binding_map = (
                persisted_binding if isinstance(persisted_binding, dict) else {}
            )
            persisted_target = str(persisted_binding_map.get("target_run_id") or "").strip()
            if expected_target and persisted_target != expected_target:
                return None
        binding = persisted.get("binding")
        binding_map = binding if isinstance(binding, dict) else {}
        if (
            str(binding_map.get("target_run_id") or "").strip()
            != str(target_run_id or "").strip()
        ):
            return None
        try:
            revoked = revoke_grant_contract(
                persisted,
                _stable_revocation_code(code),
            )
        except ABCError:
            return None
        extensions[PERMISSION_GRANT_EXTENSION_KEY] = revoked
        current.extensions = extensions
        current.updated_at = _utc_now()
        self.store.write_task(task_id, _without_none(current.to_dict()))
        return revoked

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

    def permission_choice_for_response(
        self,
        task_id: str,
        input_id: str,
    ) -> dict[str, Any] | None:
        """Return the exact recorded permission choice for one answered input.

        PERM-104-002 v2: the Runner maps this choice to the executor-native
        response payload.  ``None`` when the input was answered without a
        native choice (v1 dual-read) or does not exist.
        """
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        current_input = extensions.get("agentbc.input")
        history = list(extensions.get("agentbc.input_history") or [])
        candidates: list[dict[str, Any]] = []
        if isinstance(current_input, dict):
            candidates.append(current_input)
        candidates.extend(item for item in history if isinstance(item, dict))
        answered = next(
            (
                item
                for item in candidates
                if str(item.get("input_id") or "") == str(input_id)
            ),
            None,
        )
        if answered is None:
            return None
        response = answered.get("response")
        if not isinstance(response, dict):
            return None
        choice = response.get("permission_choice")
        if isinstance(choice, dict) and str(choice.get("handle") or "").strip():
            return dict(choice)
        return None

    def issue_session_tool_rule(
        self,
        task_id: str,
        input_id: str,
        *,
        tool_matcher: str,
    ) -> dict[str, Any]:
        """Retired tombstone (PERM-104-002 1.04A).

        The legacy matcher-grammar session rules were removed.  Historical
        receipts stay audit-only readable; no new rule can ever be issued.
        """
        raise ABCError(
            "legacy_session_tool_rule_removed",
            "Session tool rules were removed; respond with "
            "--permission-option <handle> instead",
        )

    def mark_session_tool_rule_applied(
        self,
        task_id: str,
        *,
        session_id: str,
        applied: bool,
        error_code: str = "",
    ) -> bool:
        """Retired tombstone (PERM-104-002 1.04A)."""
        raise ABCError(
            "legacy_session_tool_rule_removed",
            "Session tool rules were removed; nothing can be applied",
        )

    def revoke_session_tool_rule(self, task_id: str, code: str, *, model: TaskModel | None = None) -> bool:
        """Retired tombstone (PERM-104-002 1.04A).

        Historical receipts are preserved verbatim (read-only audit) and are
        never rewritten, revoked, or re-derived.
        """
        return False

    def block_permission_runtime_after_failure(
        self,
        task_id: str,
        *,
        code: str = "permission_runtime_capability_unavailable",
        domain: str = "host_containment",
    ) -> bool:
        """Move a live full-runtime receipt to ``blocked`` after worker loss."""
        from .permission_runtime import block_permission_runtime_record

        current = self.get_task(task_id)
        extensions = dict(current.extensions or {})
        runtime = extensions.get("agentbc.permission_runtime")
        if not isinstance(runtime, dict):
            return False
        try:
            blocked = block_permission_runtime_record(
                runtime,
                code=code,
                domain=domain,
            )
        except ABCError as exc:
            # A verified receipt is historical evidence and must not be
            # rewritten.  Any other malformed live receipt is already a
            # recovery condition, so leave the original evidence intact.
            if exc.code == "permission_runtime_state_invalid":
                return False
            raise
        extensions["agentbc.permission_runtime"] = blocked
        current.extensions = extensions
        current.updated_at = _utc_now()
        self.store.write_task(task_id, _without_none(current.to_dict()))
        self.store.append_event(
            task_id,
            {
                "event_type": "permission_runtime_blocked",
                "task_id": task_id,
                "code": code,
                "domain": domain,
                "created_at": current.updated_at,
                "source": "runner_fail_closed_recovery",
            },
        )
        self._refresh_task_index()
        return True

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

    @_serialize_task_elevation_write
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
        execution_session: dict[str, Any] | None = None,
        tool_name: str = "",
        tool_use_id: str = "",
        input_fingerprint: str = "",
        action_fingerprint: str = "",
        escalation_domain: str = "",
        profile_digest: str = "",
        control_path: str = "",
        native_event: str = "",
        offered_choices: list[dict[str, Any]] | None = None,
        authority: dict[str, Any] | None = None,
        approval_version: int | None = None,
        elevation_mode: str = "",
        path_plan_digest: str = "",
        containment_profile_digest: str = "",
        full_preflight: dict[str, Any] | None = None,
        native_live_elevation: bool = False,
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
        if execution_session is not None:
            self._apply_executor_session_result(
                task,
                executor_run_id,
                execution_session,
                "input_required",
            )
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

        # Plan D production cutover: every production adapter emits v3.  The
        # old branches below remain callable only for historical record and
        # protocol-fixture compatibility; they are not selected by a current
        # Codex, Claude, or Hermes task.
        is_task_elevation = (
            approval_version == 3
            or str(elevation_mode or "").strip().lower()
            in {PERMISSION_ELEVATION_MODE, "contained_full"}
        )
        if is_task_elevation:
            return self._block_task_for_elevation(
                task,
                executor_run_id=normalized_run_id,
                session_id=official_session_id,
                request_id=clean_request_id,
                request_fingerprint=clean_fingerprint,
                executor=normalized_executor,
                operation=clean_operation,
                summary=str(redact_secrets(clean_summary)),
                reason_summary=clean_reason_summary,
                reason_detail=clean_reason_detail,
                blocked_step_id=blocked_step_id,
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                input_fingerprint=input_fingerprint,
                action_fingerprint=action_fingerprint,
                escalation_domain=escalation_domain,
                profile_digest=profile_digest,
                control_path=control_path,
                native_event=native_event,
                authority=authority,
                path_plan_digest_value=path_plan_digest,
                containment_profile_digest_value=containment_profile_digest,
                full_preflight=full_preflight,
                native_live_elevation=native_live_elevation,
            )

        if offered_choices:
            native_authority = dict(authority or {})
            receipt = build_approval_receipt_v2(
                task_id=task_id,
                executor_run_id=normalized_run_id,
                executor=normalized_executor,
                session_id=official_session_id,
                request_id=clean_request_id,
                request_fingerprint=clean_fingerprint,
                operation=clean_operation,
                summary=str(redact_secrets(clean_summary)),
                reason_summary=clean_reason_summary,
                reason_detail=clean_reason_detail,
                authority_protocol=str(native_authority.get("protocol") or ""),
                authority_protocol_version=int(
                    native_authority.get("protocol_version") or 0
                ),
                authority_method=str(native_authority.get("method") or ""),
                broker_request_id=clean_request_id,
                native_item_id=str(tool_use_id or "").strip(),
                offered_choices=[dict(choice) for choice in offered_choices],
            )
        else:
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
        native_binding = {
            "tool_name": str(tool_name or "").strip(),
            "tool_use_id": str(tool_use_id or "").strip(),
            "action_fingerprint": str(action_fingerprint or "").strip(),
            "escalation_domain": str(escalation_domain or "").strip().lower(),
            "profile_digest": str(profile_digest or "").strip(),
            "control_path": str(control_path or "").strip(),
            "native_event": str(native_event or "").strip(),
        }
        for key, value in native_binding.items():
            if value:
                request[key] = value[:512]
        # PERM-104-002 v2: persist the executor-native choice set verbatim on
        # the input request so the dialog and CLI can offer exactly what the
        # executor offered, bound to this exact request.  Handles were
        # computed by the adapter against the native request id; the v2
        # request rejects flattened approve/deny.
        if offered_choices:
            request["approval_version"] = 2
            request["authority"] = dict(receipt["authority"])
            request["choices"] = [dict(choice) for choice in receipt["choices"]]

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

    def record_claude_elevation_transition(
        self,
        task_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one redacted Claude same-session transition receipt.

        This method never stores the blocked tool input and never changes the
        task permission snapshot.  It is called by the live SDK transport for
        the pending and active transitions, so the official session and worker
        remain the only execution identity.
        """
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        session = extensions.get(SESSION_EXTENSION_KEY)
        session_id = str(session.get("session_id") or "") if isinstance(session, dict) else ""
        if str(task.assignee or "").strip().lower() != "claude":
            raise ABCError(
                "claude_elevation_executor_mismatch",
                "Claude elevation receipts may only be attached to Claude tasks",
            )
        validated = validate_claude_elevation_receipt(
            receipt,
            task_id=task.id,
            executor_run_id=str(receipt.get("binding", {}).get("executor_run_id") or ""),
            session_id=session_id or None,
        )
        run_ids = list(session.get("run_ids") or []) if isinstance(session, dict) else []
        if validated["binding"]["executor_run_id"] not in run_ids:
            raise ABCError(
                "claude_elevation_run_mismatch",
                "Claude elevation receipt is not bound to a recorded task run",
            )
        existing = claude_elevation_from_extensions(extensions)
        if existing is not None:
            existing_binding = existing["binding"]
            incoming_binding = validated["binding"]
            for field in (
                "task_id",
                "executor_run_id",
                "session_id",
                "request_id",
                "tool_use_id",
                "request_fingerprint",
                "input_fingerprint",
                "action_fingerprint",
            ):
                if existing_binding.get(field) != incoming_binding.get(field):
                    raise ABCError(
                        "claude_elevation_binding_mismatch",
                        "Claude elevation transition changed its native identity",
                    )
            current = existing["state"]["status"]
            incoming = validated["state"]["status"]
            if current == incoming:
                validated = existing
            elif current != CLAUDE_ELEVATION_PENDING and incoming == CLAUDE_ELEVATION_PENDING:
                raise ABCError(
                    "claude_elevation_replay",
                    "Claude elevation pending transition was replayed",
                )
        extensions[CLAUDE_ELEVATION_EXTENSION_KEY] = validated
        execution = dict(extensions.get("agentbc.execution") or {})
        execution["claude_elevation_state"] = validated["state"]["status"]
        execution["claude_elevation_protocol"] = validated["protocol"]
        extensions["agentbc.execution"] = execution
        task.extensions = extensions
        task.updated_at = _utc_now()
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "task.claude_elevation_transition",
                "task_id": task.id,
                "executor_run_id": validated["binding"]["executor_run_id"],
                "session_id": validated["binding"]["session_id"],
                "request_id": validated["binding"]["request_id"],
                "state": validated["state"]["status"],
                "error_code": validated["state"].get("error_code") or "",
                "created_at": task.updated_at,
            },
        )
        self._refresh_task_index()
        return claude_elevation_public_projection(validated)

    def record_claude_elevation_notification(
        self,
        task_id: str,
    ) -> dict[str, Any] | None:
        """Reserve the one user dialog for the live Claude request."""
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        receipt = claude_elevation_from_extensions(extensions, task_id=task.id)
        if receipt is None:
            return None
        updated = record_claude_elevation_dialog(receipt)
        if updated != receipt:
            extensions[CLAUDE_ELEVATION_EXTENSION_KEY] = updated
            task.extensions = extensions
            task.updated_at = _utc_now()
            self.store.write_task(task.id, _without_none(task.to_dict()))
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.claude_elevation_dialog_reserved",
                    "task_id": task.id,
                    "request_id": updated["binding"]["request_id"],
                    "created_at": task.updated_at,
                },
            )
        return claude_elevation_public_projection(updated)

    def _block_claude_live_elevation(
        self,
        task: TaskModel,
        *,
        executor_run_id: str,
        session_id: str,
        request_id: str,
        request_fingerprint: str,
        executor: str,
        operation: str,
        summary: str,
        reason_summary: str,
        reason_detail: str,
        blocked_step_id: int | None,
        tool_name: str,
        tool_use_id: str,
        input_fingerprint: str,
        action_fingerprint: str,
        escalation_domain: str,
        profile_digest: str,
        control_path: str,
        native_event: str,
        authority: dict[str, Any] | None,
        path_plan_digest_value: str,
        containment_profile_digest_value: str,
        full_preflight: dict[str, Any] | None,
        native_live_elevation: bool = False,
    ) -> dict[str, Any]:
        """Expose one live callback wait without suspending its RunLease."""
        from .permission_runtime import host_profile_digest, path_plan_digest

        extensions = dict(task.extensions or {})
        step_id = blocked_step_id or _first_incomplete_step_id(task.steps)
        if step_id is None:
            raise ABCError(
                "approval_no_step",
                "Claude live elevation cannot be displayed without an incomplete step",
            )
        clean_plan = str(path_plan_digest_value or path_plan_digest(task.workspace or {}))
        clean_profile = str(
            containment_profile_digest_value or profile_digest or host_profile_digest()
        )
        native_authority = dict(authority or {})
        receipt_value = build_approval_receipt_v3(
            task_id=task.id,
            executor_run_id=executor_run_id,
            executor=executor,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            operation=operation,
            path_plan_digest=clean_plan,
            containment_profile_digest=clean_profile,
            summary=summary,
            reason_summary=reason_summary,
            reason_detail=reason_detail,
            authority=native_authority,
            native_event=native_event or "claude_sdk_can_use_tool",
            tool_call_id=tool_use_id or request_id,
            action_fingerprint=action_fingerprint,
        )
        existing_receipt = claude_elevation_from_extensions(extensions)
        if existing_receipt is None:
            existing_receipt = build_claude_elevation_receipt(
                task_id=task.id,
                executor_run_id=executor_run_id,
                session_id=session_id,
                request_id=request_id,
                tool_use_id=tool_use_id or request_id,
                request_fingerprint=request_fingerprint,
                input_fingerprint=stable_claude_input_digest(
                    {"request_fingerprint": request_fingerprint}
                ),
                action_fingerprint=action_fingerprint or request_fingerprint,
                operation=operation,
                path_plan_digest=clean_plan,
                containment_profile_digest=clean_profile,
                native_event=native_event or "claude_sdk_can_use_tool",
            )
            existing_receipt = transition_claude_elevation(
                existing_receipt,
                CLAUDE_ELEVATION_PENDING,
            )
        else:
            existing_binding = existing_receipt["binding"]
            expected_binding = {
                "task_id": task.id,
                "executor_run_id": executor_run_id,
                "session_id": session_id,
                "request_id": request_id,
                "tool_use_id": tool_use_id or request_id,
                "request_fingerprint": request_fingerprint,
                "action_fingerprint": action_fingerprint or request_fingerprint,
                "operation": operation,
            }
            same_request = all(
                existing_binding.get(field) == expected
                for field, expected in expected_binding.items()
            )
            if not same_request:
                raise ABCError(
                    "claude_elevation_binding_mismatch",
                    "A Claude live elevation request changed its native identity or input",
                )
            if existing_receipt["state"]["status"] == CLAUDE_ELEVATION_PENDING:
                previous_input = extensions.get("agentbc.input")
                if isinstance(previous_input, dict) and previous_input.get("native_live_elevation") is True:
                    return {
                        "ok": True,
                        "task_id": task.id,
                        "status": str(task.status or "input_required"),
                        "input_id": str(previous_input.get("input_id") or ""),
                        "request_id": request_id,
                        "request_fingerprint": request_fingerprint,
                        "scope": APPROVAL_V3_SCOPE,
                        "approval_version": 3,
                        "elevation_mode": PERMISSION_ELEVATION_MODE,
                        "native_live_elevation": True,
                        "blocked_step_id": previous_input.get("blocked_step_id"),
                        "same_session": True,
                        "dispatch_required": False,
                        "idempotent": True,
                    }
                raise ABCError(
                    "claude_elevation_input_missing",
                    "The pending Claude elevation receipt has no reusable input request",
                )
            elif existing_receipt["state"]["status"] != CLAUDE_ELEVATION_PENDING:
                # A duplicate/replayed native event is never a reason to show
                # a second dialog.  The live callback/control plane owns the
                # terminal decision; Core only exposes the already-bound wait.
                raise ABCError(
                    "claude_elevation_replay",
                    "A Claude live elevation request already reached a terminal state",
                )
        extensions[APPROVAL_EXTENSION_KEY] = receipt_value
        extensions[CLAUDE_ELEVATION_EXTENSION_KEY] = existing_receipt
        now = _utc_now()
        deadline_at = (
            _parse_timestamp(now) + timedelta(seconds=DEFAULT_INPUT_WAIT_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        request: dict[str, Any] = {
            "input_id": f"input-{uuid.uuid4().hex}",
            "executor_run_id": executor_run_id,
            "session_id": session_id,
            "blocked_step_id": step_id,
            "type": "permission",
            "scope": APPROVAL_V3_SCOPE,
            "approval_version": 3,
            "elevation_mode": PERMISSION_ELEVATION_MODE,
            "native_live_elevation": True,
            "native_elevation_protocol": "claude.can_use_tool.setMode",
            "requested_permission": "full",
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "input_fingerprint": existing_receipt["binding"]["input_fingerprint"],
            "operation": operation,
            "summary": receipt_value["summary"],
            "reason_summary": reason_summary,
            "summary_truncated": bool(receipt_value.get("summary_truncated", False)),
            "path_plan_digest": clean_plan,
            "containment_profile_digest": clean_profile,
            "authority": native_authority,
            "native_event": native_event or "claude_sdk_can_use_tool",
            "tool_name": str(tool_name or "").strip()[:512],
            "tool_use_id": str(tool_use_id or "").strip()[:512],
            "action_fingerprint": str(action_fingerprint or "").strip()[:512],
            "escalation_domain": str(escalation_domain or "").strip().lower()[:120],
            "control_path": str(control_path or "").strip()[:512],
            "preflight": dict(full_preflight or {"ok": True, "status": "passed", "mode": "contained_full"}),
            "created_at": now,
            "deadline_at": deadline_at,
            "status": "waiting",
        }
        if native_live_elevation:
            request["native_live_elevation"] = True
        # The task is visibly waiting, but its active RunLease and official SDK
        # session are intentionally left untouched.
        task.status = "input_required"
        task.updated_at = now
        extensions["agentbc.input"] = request
        task.extensions = _merge_execution(
            extensions,
            {
                "internal_status": "elevation_pending",
                "lease_state": "active",
                "waiting_since": now,
                "claude_elevation_state": CLAUDE_ELEVATION_PENDING,
            },
        )
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "task.claude_elevation_pending",
                "task_id": task.id,
                "executor_run_id": executor_run_id,
                "session_id": session_id,
                "request_id": request_id,
                "input_id": request["input_id"],
                "created_at": now,
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task.id,
            "status": "input_required",
            "input_id": request["input_id"],
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "scope": APPROVAL_V3_SCOPE,
            "approval_version": 3,
            "elevation_mode": PERMISSION_ELEVATION_MODE,
            "native_live_elevation": True,
            "blocked_step_id": step_id,
            "same_session": True,
            "dispatch_required": False,
        }

    def respond_to_live_claude_elevation(
        self,
        task_id: str,
        input_id: str,
        *,
        response_type: str,
    ) -> dict[str, Any]:
        """Answer the live dialog while retaining the current worker/lease."""
        from .run_lease import RunLeaseState, load_lease

        assert_maintenance_command_allowed(self, "respond")
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        request = extensions.get("agentbc.input")
        if not isinstance(request, dict) or request.get("native_live_elevation") is not True:
            raise ABCError("input_not_pending", f"Task {task.id} has no live Claude elevation input")
        if str(request.get("input_id") or "") != str(input_id or ""):
            raise ABCError("stale_input", f"Input {input_id} is not current for task {task.id}")
        if request.get("status") != "waiting":
            if request.get("status") == "answered":
                answered_decision = str(
                    request.get("approval_decision") or ""
                ).strip().lower()
                return {
                    "ok": True,
                    "task_id": task.id,
                    "input_id": input_id,
                    "status": "already_answered",
                    "dispatch_required": False,
                    "same_session": True,
                    "approval_decision": (
                        answered_decision
                        if answered_decision in {"approve", "deny"}
                        else ""
                    ),
                }
            raise ABCError("input_not_pending", f"Input {input_id} is not waiting")
        response_value = str(response_type or "").strip().lower()
        if response_value == "approve_full":
            response_value = "approve"
        if response_value not in {"approve", "deny"}:
            raise ABCError(
                "invalid_input_response",
                "Live Claude elevation accepts only approve or deny",
            )
        lease = load_lease(task.id, self.board_root)
        expected_run = str(request.get("executor_run_id") or "")
        if lease is None or lease.state != RunLeaseState.ACTIVE or lease.run_id != expected_run:
            raise ABCError(
                "executor_active",
                "The live Claude RunLease is missing or no longer active",
            )
        session = extensions.get(SESSION_EXTENSION_KEY)
        session_id = str(session.get("session_id") or "") if isinstance(session, dict) else ""
        request_session_id = str(request.get("session_id") or "").strip()
        if not session_id or request_session_id != session_id:
            raise ABCError(
                "approval_session_mismatch",
                "The live Claude input is not bound to the official executor session",
            )
        elevation = permission_elevation_from_extensions(
            extensions,
            task_id=task.id,
            executor_run_id=expected_run,
            session_id=session_id,
            request_id=str(request.get("request_id") or ""),
            request_fingerprint=str(request.get("request_fingerprint") or ""),
        )
        if elevation is None or elevation["state"]["status"] != "prepared":
            raise ABCError(
                "permission_elevation_state_invalid",
                "The live task elevation receipt is not pending",
            )
        now = _utc_now()
        deadline = _parse_timestamp(str(request.get("deadline_at") or now))
        if deadline <= _parse_timestamp(now):
            raise ABCError("input_expired", f"Input {input_id} reached its response deadline")
        approval_receipt = extensions.get(APPROVAL_EXTENSION_KEY)
        if isinstance(approval_receipt, dict):
            extensions[APPROVAL_EXTENSION_KEY] = record_approval_decision(
                approval_receipt,
                "approve_full" if response_value == "approve" else "deny",
                source="user",
                decided_at=now,
                executor=task.assignee,
                task_id=task.id,
                session_id=session_id,
                request_id=str(request.get("request_id") or ""),
                executor_run_id=expected_run,
                request_fingerprint=str(request.get("request_fingerprint") or ""),
            )
        extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = (
            record_permission_elevation_decision(
                elevation,
                "approve_full" if response_value == "approve" else "deny",
                source="user",
                decided_at=now,
                task_id=task.id,
                executor=task.assignee,
                executor_run_id=expected_run,
                session_id=session_id,
                request_id=str(request.get("request_id") or ""),
                request_fingerprint=str(request.get("request_fingerprint") or ""),
            )
        )
        answered = dict(request)
        answered.update(
            {
                "status": "answered",
                "responded_at": now,
                "response": {"type": response_value, "summary": response_value},
                "approval_decision": response_value,
            }
        )
        extensions["agentbc.input"] = answered
        task.status = "running"
        task.updated_at = now
        task.extensions = _merge_execution(
            extensions,
            {
                "internal_status": "running",
                "lease_state": "active",
                "permission_elevation_state": (
                    "approved" if response_value == "approve" else "denied"
                ),
            },
        )
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "task.claude_elevation_decision",
                "task_id": task.id,
                "input_id": input_id,
                "request_id": str(request.get("request_id") or ""),
                "decision": response_value,
                "executor_run_id": expected_run,
                "session_id": session_id,
                "created_at": now,
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task.id,
            "input_id": input_id,
            "request_id": str(request.get("request_id") or ""),
            "status": "running",
            "dispatch_required": False,
            "same_task": True,
            "same_session": True,
            "native_live_elevation": True,
            "approval_decision": response_value,
        }

    def _block_task_for_elevation(
        self,
        task: TaskModel,
        *,
        executor_run_id: str,
        session_id: str,
        request_id: str,
        request_fingerprint: str,
        executor: str,
        operation: str,
        summary: str,
        reason_summary: str,
        reason_detail: str,
        blocked_step_id: int | None,
        tool_name: str,
        tool_use_id: str,
        input_fingerprint: str,
        action_fingerprint: str,
        escalation_domain: str,
        profile_digest: str,
        control_path: str,
        native_event: str,
        authority: dict[str, Any] | None,
        path_plan_digest_value: str,
        containment_profile_digest_value: str,
        full_preflight: dict[str, Any] | None,
        native_live_elevation: bool = False,
    ) -> dict[str, Any]:
        """Create the single v3 native-full elevation wait."""
        from .reports import redact_secrets
        from .run_lease import suspend_lease
        from .task_health import clear_task_progress

        task_id = task.id
        extensions = dict(task.extensions or {})
        authority_value = dict(authority or {})
        authority_executor = str(authority_value.get("executor") or "").strip().lower()
        authority_protocol = str(authority_value.get("protocol") or "").strip()
        authority_method = str(authority_value.get("method") or "").strip()
        if (
            authority_executor != executor
            or not authority_protocol
            or not authority_method
            or not str(native_event or "").strip()
        ):
            raise ABCError(
                "permission_block_evidence_unavailable",
                "Task elevation requires the trusted structured native authority event",
            )

        # Plan D binds authority only to the trusted native block identity.
        # PathPlan, host containment, version probes and capability checks are
        # not permission systems and cannot prevent the one elevation.
        clean_plan_digest = ""
        clean_profile_digest = ""

        previous_input = extensions.get("agentbc.input")
        if isinstance(previous_input, dict) and previous_input.get("status") == "waiting":
            if (
                int(previous_input.get("approval_version") or 1) == 3
                and str(previous_input.get("request_id") or "") == request_id
            ):
                identity_fields = (
                    ("request_fingerprint", request_fingerprint),
                    ("input_fingerprint", input_fingerprint),
                    ("tool_name", tool_name),
                    ("tool_use_id", tool_use_id),
                    ("action_fingerprint", action_fingerprint),
                    ("control_path", control_path),
                    ("native_event", native_event),
                )
                mismatched = [
                    key
                    for key, incoming in identity_fields
                    if incoming
                    and str(previous_input.get(key) or "")
                    != str(incoming)
                ]
                if mismatched:
                    raise ABCError(
                        "permission_elevation_binding_mismatch",
                        "A replayed native elevation request changed its native identity or input",
                        {"fields": mismatched, "request_id": request_id},
                    )
                return {
                    "ok": True,
                    "task_id": task_id,
                    "status": "input_required",
                    "input_id": str(previous_input.get("input_id") or ""),
                    "request_id": request_id,
                    "request_fingerprint": request_fingerprint,
                    "scope": APPROVAL_V3_SCOPE,
                    "blocked_step_id": previous_input.get("blocked_step_id"),
                    "idempotent": True,
                }
            raise ABCError(
                "approval_already_pending",
                "A task elevation request is already waiting for this Task ID",
            )

        existing = permission_elevation_from_extensions(
            extensions,
            task_id=task_id,
            path_plan_digest=clean_plan_digest,
        )
        if existing is not None:
            existing_status = str(existing["state"].get("status") or "")
            if existing_status in {"approved", "active", "verified"}:
                try:
                    failed_elevation = block_permission_elevation(
                        existing,
                        code="permission_escalation_ineffective",
                    )
                    extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = failed_elevation
                    task.extensions = extensions
                    self.store.write_task(task_id, _without_none(task.to_dict()))
                except ABCError:
                    pass
                self.mark_task_needs_recovery(
                    task_id,
                    "permission_escalation_ineffective",
                    "The approved full elevation did not remove the native permission block",
                    {
                        "executor": executor,
                        "request_id": request_id,
                        "phase": "repeated_native_block",
                    },
                    executor_run_id=executor_run_id,
                )
                raise ABCError(
                    "permission_escalation_ineffective",
                    "A repeated permission block converged without another dialog or continuation",
                )
            raise ABCError(
                "permission_elevation_replay",
                "This Task ID already has a terminal task-elevation decision",
            )

        step_id = blocked_step_id or _first_incomplete_step_id(task.steps)
        if step_id is None:
            raise ABCError(
                "permission_preflight_failed",
                "Task elevation cannot be created because no incomplete step exists",
            )
        if not any(
            step.get("id") == step_id and step.get("status") not in {"done", "completed"}
            for step in task.steps
        ):
            raise ABCError(
                "permission_input_invalid",
                "Task elevation does not identify an incomplete step",
            )

        receipt = build_approval_receipt_v3(
            task_id=task_id,
            executor_run_id=executor_run_id,
            executor=executor,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            operation=operation,
            path_plan_digest=clean_plan_digest,
            containment_profile_digest=clean_profile_digest,
            summary=str(redact_secrets(summary)),
            reason_summary=reason_summary,
            reason_detail=reason_detail,
            authority=authority_value,
            native_event=native_event,
            tool_call_id=tool_use_id or request_id,
            action_fingerprint=action_fingerprint,
        )
        elevation = build_permission_elevation(
            task_id=task_id,
            path_plan_digest=clean_plan_digest,
            executor=executor,
            executor_run_id=executor_run_id,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            containment_profile_digest=clean_profile_digest,
            operation=operation,
            native_event=native_event,
            tool_call_id=tool_use_id or request_id,
            action_fingerprint=action_fingerprint,
            authority=authority_value,
        )
        now = _utc_now()
        deadline_at = (
            _parse_timestamp(now) + timedelta(seconds=DEFAULT_INPUT_WAIT_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        request: dict[str, Any] = {
            "input_id": f"input-{uuid.uuid4().hex}",
            "executor_run_id": executor_run_id,
            "blocked_step_id": step_id,
            "type": "permission",
            "scope": APPROVAL_V3_SCOPE,
            "approval_version": 3,
            "elevation_mode": PERMISSION_ELEVATION_MODE,
            "requested_permission": "full",
            "session_id": session_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "operation": operation,
            "summary": receipt["summary"],
            "reason_summary": reason_summary,
            "summary_truncated": bool(receipt.get("summary_truncated", False)),
            "path_plan_digest": clean_plan_digest,
            "containment_profile_digest": clean_profile_digest,
            "authority": authority_value,
            "native_event": str(native_event).strip()[:512],
            "tool_name": str(tool_name or "").strip()[:512],
            "tool_use_id": str(tool_use_id or "").strip()[:512],
            "action_fingerprint": str(action_fingerprint or "").strip()[:512],
            "escalation_domain": str(escalation_domain or "").strip().lower()[:120],
            "control_path": str(control_path or "").strip()[:512],
            "preflight": {
                "status": "retired",
                "mode": PERMISSION_ELEVATION_MODE,
            },
            "created_at": now,
            "deadline_at": deadline_at,
            "status": "waiting",
        }
        if str(input_fingerprint or "").strip():
            request["input_fingerprint"] = str(input_fingerprint).strip()[:160]
        if native_live_elevation:
            request["native_live_elevation"] = True
        history = list(extensions.get("agentbc.input_history") or [])
        if isinstance(previous_input, dict):
            history.append(previous_input)
        task.status = "input_required"
        task.updated_at = now
        task.steps = [_resource_block_step(step, step_id) for step in task.steps]
        extensions[APPROVAL_EXTENSION_KEY] = receipt
        extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = elevation
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
                "lease_state": "active" if native_live_elevation else "suspended",
                "waiting_since": now,
                "permission_elevation_mode": PERMISSION_ELEVATION_MODE,
                "permission_elevation_source": "task_elevation",
                "permission_elevation_state": "prepared",
            },
        )
        self.store.write_task(task_id, _without_none(task.to_dict()))
        if not native_live_elevation:
            self._release_lease(task_id)
            suspend_lease(
                task_id,
                self.board_root,
                executor_run_id=executor_run_id,
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
                "event_type": "task.permission_elevation_required",
                "task_id": task_id,
                "created_at": now,
                "input_id": request["input_id"],
                "request_id": request_id,
                "blocked_step_id": step_id,
                "scope": APPROVAL_V3_SCOPE,
                "mode": PERMISSION_ELEVATION_MODE,
                "source": "task_elevation",
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task_id,
            "status": "input_required",
            "input_id": request["input_id"],
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "scope": APPROVAL_V3_SCOPE,
            "approval_version": 3,
            "elevation_mode": PERMISSION_ELEVATION_MODE,
            "blocked_step_id": step_id,
            "preflight": "retired",
            "same_session": bool(native_live_elevation),
            "dispatch_required": not native_live_elevation,
        }

    def block_task_for_elevation(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        """Public explicit v3 entry point; native adapters use this contract."""
        return self.block_task_for_approval(
            task_id,
            approval_version=3,
            elevation_mode=PERMISSION_ELEVATION_MODE,
            **kwargs,
        )

    @_serialize_task_elevation_write
    def record_task_elevation_notification(self, task_id: str) -> dict[str, Any] | None:
        """Persist the single notification reservation for a v3 wait."""
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        value = permission_elevation_from_extensions(extensions, task_id=task.id)
        if value is None:
            return None
        updated = record_permission_elevation_notification(value)
        approval_value = extensions.get(APPROVAL_EXTENSION_KEY)
        updated_approval = approval_value
        if isinstance(approval_value, dict) and approval_value.get("version") == 3:
            updated_approval = record_approval_notification(approval_value)
        if updated != value or updated_approval != approval_value:
            extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = updated
            if updated_approval is not None:
                extensions[APPROVAL_EXTENSION_KEY] = updated_approval
            task.extensions = extensions
            task.updated_at = _utc_now()
            self.store.write_task(task.id, _without_none(task.to_dict()))
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.permission_elevation_notification_reserved",
                    "task_id": task.id,
                    "request_id": str(value["binding"].get("request_id") or ""),
                    "created_at": task.updated_at,
                },
            )
        return updated

    def reserve_task_elevation_notification(
        self,
        task_id: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Reserve the v3 notice and report whether this caller won it."""
        with _TASK_ELEVATION_WRITE_LOCK:
            task = self.get_task(task_id)
            extensions = dict(task.extensions or {})
            value = permission_elevation_from_extensions(extensions, task_id=task.id)
            if value is None:
                return None, False
            already_reserved = value["cardinality"].get("notifications") == 1
            updated = self.record_task_elevation_notification(task_id)
            return updated, not already_reserved

    @_serialize_task_elevation_write
    def activate_task_elevation(
        self,
        task_id: str,
        *,
        executor_run_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Activate one approved native-full elevation."""
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        elevation = permission_elevation_from_extensions(
            extensions,
            task_id=task.id,
        )
        if elevation is None:
            raise ABCError(
                "permission_elevation_missing",
                "Cannot activate a task elevation without its durable receipt",
            )
        if elevation["state"]["status"] == "approved":
            activated_elevation = activate_permission_elevation(
                elevation,
                executor_run_id=executor_run_id,
                session_id=session_id,
            )
        elif elevation["state"]["status"] in {"active", "verified"}:
            activated_elevation = elevation
        else:
            raise ABCError(
                "permission_elevation_state_invalid",
                "Only an approved task elevation can be activated",
            )
        approval_value = extensions.get(APPROVAL_EXTENSION_KEY)
        approval = validate_approval_receipt(approval_value)
        if approval.get("version") == 3 and approval["decision"].get("type") == "approve_full":
            approval = record_approval_full_continuation(
                approval,
                executor_run_id=executor_run_id,
                session_id=session_id,
            )
        extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = activated_elevation
        extensions[APPROVAL_EXTENSION_KEY] = approval
        task.extensions = _merge_execution(
            extensions,
            {
                "permission_elevation_mode": PERMISSION_ELEVATION_MODE,
                "permission_elevation_source": "task_elevation",
                "permission_elevation_state": activated_elevation["state"]["status"],
                "permission_runtime_state": "retired",
            },
        )
        task.updated_at = _utc_now()
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "task.permission_elevation_activated",
                "task_id": task.id,
                "executor": task.assignee,
                "executor_run_id": executor_run_id,
                "session_id": session_id,
                "elevation_id": str(elevation.get("elevation_id") or ""),
                "created_at": task.updated_at,
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task.id,
            "mode": PERMISSION_ELEVATION_MODE,
            "source": "task_elevation",
            "elevation_state": activated_elevation["state"]["status"],
            "runtime_state": "retired",
        }

    def verify_task_elevation(
        self,
        task_id: str,
        *,
        executor_run_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Verify the elevation from the official resumed executor session."""
        task = self.get_task(task_id)
        extensions = dict(task.extensions or {})
        elevation = permission_elevation_from_extensions(
            extensions,
            task_id=task.id,
        )
        if elevation is None:
            raise ABCError("permission_elevation_missing", "No task elevation receipt exists")
        verified_elevation = verify_permission_elevation(
            elevation,
            executor_run_id=executor_run_id,
            session_id=session_id,
        )
        extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = verified_elevation
        task.extensions = _merge_execution(
            extensions,
            {
                "permission_elevation_state": "verified",
                "permission_runtime_state": "retired",
            },
        )
        task.updated_at = _utc_now()
        self.store.write_task(task.id, _without_none(task.to_dict()))
        self.store.append_event(
            task.id,
            {
                "event_type": "task.permission_elevation_verified",
                "task_id": task.id,
                "executor_run_id": executor_run_id,
                "session_id": session_id,
                "created_at": task.updated_at,
            },
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task.id,
            "elevation_state": "verified",
            "runtime_state": "retired",
        }

    def _respond_to_permission_input(
        self,
        task: TaskModel,
        request: dict[str, Any],
        *,
        input_id: str,
        response_type: str,
        message: str,
        clean_message: str,
        now: str,
    ) -> dict[str, Any]:
        """Apply one permission answer and own every permission transition."""
        from .task_health import write_task_progress

        is_task_elevation_request = (
            int(request.get("approval_version") or 1) == 3
            and request.get("scope") == APPROVAL_V3_SCOPE
            and request.get("elevation_mode") == PERMISSION_ELEVATION_MODE
        )
        is_v2_permission = (
            int(request.get("approval_version") or 1) == 2
            and isinstance(request.get("choices"), list)
            and bool(request.get("choices"))
        )
        if response_type == "permission_option":
            if not is_v2_permission:
                raise ABCError(
                    "native_permission_choice_required",
                    "Only a v2 native permission request accepts an explicit "
                    "choice handle",
                )
            if not str(message or "").strip():
                raise ABCError(
                    "invalid_input_response",
                    "--permission-option requires the exact offered handle",
                )
        elif response_type not in (
            {"approve_full", "deny"}
            if is_task_elevation_request
            else {"approve", "deny"}
        ):
            raise ABCError(
                "invalid_input_response",
                "Task elevation requests only accept approve_full or deny"
                if is_task_elevation_request
                else "Permission requests only accept approve or deny",
            )
        if is_v2_permission and response_type in {"approve", "deny", "approve_full"}:
            raise ABCError(
                "native_permission_choice_required",
                "A v2 native permission request requires an explicit choice "
                "handle (--permission-option), not a flattened approve/deny",
            )

        selected_choice: dict[str, Any] | None = None
        if response_type == "permission_option":
            handle = str(message or "").strip()
            offered_choices = [
                choice
                for choice in request.get("choices", [])
                if isinstance(choice, dict)
            ]
            matched = [
                choice
                for choice in offered_choices
                if str(choice.get("handle") or "") == handle
            ]
            if not matched:
                raise ABCError(
                    "approval_handle_mismatch",
                    "The choice handle was not offered by this exact request",
                    {"input_id": str(request.get("input_id") or "")},
                )
            selected_choice = dict(matched[0])
            if selected_choice.get("selectable", True) is False:
                raise ABCError(
                    "approval_choice_not_selectable",
                    "The selected native choice was offered as non-selectable",
                )

        answered = dict(request)
        answered["status"] = "answered"
        answered["responded_at"] = now
        permission_denial_source = (
            "timeout"
            if response_type == "deny" and message == PERMISSION_DIALOG_TIMEOUT_RESPONSE
            else "dialog_closed"
            if response_type == "deny" and message == PERMISSION_DIALOG_CLOSED_RESPONSE
            else "user"
        )
        response_payload: dict[str, Any] = {
            "type": response_type,
            "summary": clean_message,
            **(
                {"source": permission_denial_source}
                if response_type == "deny"
                else {}
            ),
        }
        if response_type == "permission_option" and selected_choice is not None:
            response_payload["permission_choice"] = {
                "handle": str(selected_choice.get("handle") or ""),
                "native_option_id": str(selected_choice.get("native_option_id") or ""),
                "kind": str(selected_choice.get("kind") or ""),
            }
        answered["response"] = response_payload
        extensions = dict(task.extensions or {})
        extensions["agentbc.input"] = answered

        if is_task_elevation_request:
            elevation = permission_elevation_from_extensions(
                extensions,
                task_id=task.id,
                path_plan_digest=str(request.get("path_plan_digest") or ""),
                executor=task.assignee,
                executor_run_id=str(request.get("executor_run_id") or ""),
                session_id=str(
                    (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
                ),
                request_id=str(request.get("request_id") or ""),
                request_fingerprint=str(request.get("request_fingerprint") or ""),
            )
            if elevation is None:
                raise ABCError(
                    "permission_elevation_binding_mismatch",
                    "Task elevation input is missing its durable elevation receipt",
                )
            session_value = str(
                (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
            )
            approval_receipt = self._approval_receipt_for_response(
                task, extensions, request, input_id
            )
            elevation_source = (
                permission_denial_source if response_type == "deny" else "user"
            )
            extensions[APPROVAL_EXTENSION_KEY] = record_approval_decision(
                approval_receipt,
                response_type,
                source=elevation_source,
                decided_at=now,
                executor=task.assignee,
                task_id=task.id,
                session_id=session_value,
                request_id=str(request.get("request_id") or ""),
                executor_run_id=str(request.get("executor_run_id") or ""),
                request_fingerprint=str(request.get("request_fingerprint") or ""),
            )
            extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = (
                record_permission_elevation_decision(
                    elevation,
                    response_type,
                    source=elevation_source,
                    decided_at=now,
                    task_id=task.id,
                    path_plan_digest=str(request.get("path_plan_digest") or ""),
                    executor=task.assignee,
                    executor_run_id=str(request.get("executor_run_id") or ""),
                    session_id=session_value,
                    request_id=str(request.get("request_id") or ""),
                    request_fingerprint=str(request.get("request_fingerprint") or ""),
                )
            )
            if response_type == "deny":
                timed_out = permission_denial_source == "timeout"
                failure_code = (
                    "permission_denied_by_timeout"
                    if timed_out
                    else "permission_denied_by_user"
                )
                failure_message = (
                    "Task elevation timed out and was automatically denied"
                    if timed_out
                    else "User denied full task elevation"
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
                        "input_id": input_id,
                        "executor": task.assignee,
                        "permission_elevation_mode": PERMISSION_ELEVATION_MODE,
                    },
                    executor_run_id=str(request.get("executor_run_id") or ""),
                )
                self.store.append_event(
                    task.id,
                    {
                        "event_type": "task.permission_elevation_denied",
                        "task_id": task.id,
                        "input_id": input_id,
                        "request_id": str(request.get("request_id") or ""),
                        "response_source": elevation_source,
                        "created_at": now,
                    },
                )
                return {
                    "ok": True,
                    "task_id": task.id,
                    "input_id": input_id,
                    "request_id": str(request.get("request_id") or ""),
                    "status": "failed",
                    "dispatch_required": False,
                    "same_session": False,
                    "permission_denied": True,
                    "approval_decision": "deny",
                    "elevation_state": "denied",
                }

            blocked_step_id = request.get("blocked_step_id")
            if not any(
                step.get("id") == blocked_step_id and step.get("status") == "blocked"
                for step in task.steps
            ):
                raise ABCError(
                    "permission_input_invalid",
                    "Task elevation input does not identify the current blocked step",
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
                    "permission_elevation_mode": PERMISSION_ELEVATION_MODE,
                    "permission_elevation_source": "task_elevation",
                    "permission_elevation_state": "approved",
                },
            )
            self.store.write_task(task.id, _without_none(task.to_dict()))
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.permission_elevation_approved",
                    "task_id": task.id,
                    "input_id": input_id,
                    "request_id": str(request.get("request_id") or ""),
                    "response_source": elevation_source,
                    "created_at": now,
                },
            )
            write_task_progress(
                task,
                state="resuming",
                message="full elevation approved; resuming the same task session",
                source="runner",
            )
            self._refresh_task_index()
            return {
                "ok": True,
                "task_id": task.id,
                "input_id": input_id,
                "request_id": str(request.get("request_id") or ""),
                "status": "resuming",
                "dispatch_required": True,
                "approval_decision": "approve_full",
                "approval_source": elevation_source,
                "same_session": True,
                "elevation_state": "approved",
            }

        is_approval_request = (
            request.get("scope") == APPROVAL_SCOPE
            and bool(str(request.get("request_id") or "").strip())
        )
        if is_approval_request:
            receipt = self._approval_receipt_for_response(
                task, extensions, request, input_id
            )
            if response_type == "permission_option" and selected_choice is not None:
                approval_source = (
                    permission_denial_source
                    if str(selected_choice.get("kind") or "") == "deny"
                    and permission_denial_source != "user"
                    else "user"
                )
                decided_type = (
                    "deny"
                    if str(selected_choice.get("kind") or "") == "deny"
                    else "approve"
                )
                from .approval import record_approval_selection

                updated_receipt = record_approval_selection(
                    receipt,
                    str(selected_choice.get("handle") or ""),
                    source=approval_source,
                    decided_type=decided_type,
                )
            else:
                approval_source = (
                    permission_denial_source if response_type == "deny" else "user"
                )
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
                    "input_id": input_id,
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
                "input_id": input_id,
                "request_id": str(request.get("request_id") or ""),
                "status": "resuming",
                "dispatch_required": True,
                "approval_decision": response_type,
                "approval_source": approval_source,
                "same_session": True,
            }

        if response_type == "deny":
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
                    "input_id": input_id,
                    "executor": task.assignee,
                    "requested_permission": request.get("requested_permission", ""),
                },
            )
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.input_answered",
                    "task_id": task.id,
                    "input_id": input_id,
                    "response_type": response_type,
                    "response_source": permission_denial_source,
                    "created_at": now,
                },
            )
            return {
                "ok": True,
                "task_id": task.id,
                "input_id": input_id,
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
            input_id=input_id,
            session_id=session_id,
            source_run_id=str(request.get("executor_run_id") or ""),
            base_mode=str(runtime_policy["base_mode"]),
            issued_at=now,
        )
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
                "input_id": input_id,
                "response_type": response_type,
                "created_at": now,
            },
        )
        write_task_progress(
            task,
            state="resuming",
            message="user response received; resuming task",
            source="runner",
        )
        self._refresh_task_index()
        return {
            "ok": True,
            "task_id": task.id,
            "input_id": input_id,
            "status": "resuming",
            "dispatch_required": True,
        }

    def _expire_permission_input(
        self,
        task: TaskModel,
        request: dict[str, Any],
        expired_at: str,
    ) -> bool:
        """Auto-deny one expired permission input using the same lifecycle."""
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
        is_task_elevation_request = (
            request.get("scope") == APPROVAL_V3_SCOPE
            and int(request.get("approval_version") or 1) == 3
            and request.get("elevation_mode") == PERMISSION_ELEVATION_MODE
        )
        if is_task_elevation_request:
            extensions = dict(task.extensions or {})
            try:
                session_value = str(
                    (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
                )
                receipt = self._approval_receipt_for_response(
                    task,
                    extensions,
                    request,
                    str(request.get("input_id") or ""),
                )
                elevation = permission_elevation_from_extensions(
                    extensions,
                    task_id=task.id,
                    path_plan_digest=str(request.get("path_plan_digest") or ""),
                    executor=task.assignee,
                    executor_run_id=str(request.get("executor_run_id") or ""),
                    session_id=session_value,
                    request_id=str(request.get("request_id") or ""),
                    request_fingerprint=str(request.get("request_fingerprint") or ""),
                )
                if elevation is None:
                    raise ABCError(
                        "permission_elevation_binding_mismatch",
                        "Timed-out task elevation has no durable elevation receipt",
                    )
                extensions[APPROVAL_EXTENSION_KEY] = record_approval_decision(
                    receipt,
                    "deny",
                    source="timeout",
                    decided_at=expired_at,
                    executor=task.assignee,
                    task_id=task.id,
                    session_id=session_value,
                    request_id=str(request.get("request_id") or ""),
                    executor_run_id=str(request.get("executor_run_id") or ""),
                    request_fingerprint=str(request.get("request_fingerprint") or ""),
                )
                extensions[PERMISSION_ELEVATION_EXTENSION_KEY] = (
                    record_permission_elevation_decision(
                        elevation,
                        "deny",
                        source="timeout",
                        decided_at=expired_at,
                        task_id=task.id,
                        path_plan_digest=str(request.get("path_plan_digest") or ""),
                        executor=task.assignee,
                        executor_run_id=str(request.get("executor_run_id") or ""),
                        session_id=session_value,
                        request_id=str(request.get("request_id") or ""),
                        request_fingerprint=str(request.get("request_fingerprint") or ""),
                    )
                )
                task.extensions = extensions
                self.store.write_task(task.id, _without_none(task.to_dict()))
            except ABCError:
                # The timeout remains fail-closed even if a damaged receipt
                # prevents a second write; no replacement dialog is created.
                pass
            self._mark_task_failed_model(
                task,
                "permission_denied_by_timeout",
                "Task elevation timed out and was automatically denied",
                {
                    "failure": {
                        "kind": "permission_denied_by_timeout",
                        "layer": "permission",
                        "message": "Task elevation timed out and was automatically denied",
                        "retryable": False,
                    },
                    "input_id": request.get("input_id", ""),
                    "executor": task.assignee,
                    "permission_elevation_mode": PERMISSION_ELEVATION_MODE,
                },
            )
            self.store.append_event(
                task.id,
                {
                    "event_type": "task.permission_elevation_denied",
                    "task_id": task.id,
                    "input_id": request.get("input_id", ""),
                    "response_type": "deny",
                    "response_source": "timeout",
                    "created_at": expired_at,
                },
            )
            return True

        if is_approval_request:
            # Approval-based timeout auto-denies on the same native request,
            # then moves the task to needs_recovery so the official session is
            # never silently lost.
            extensions = dict(task.extensions or {})
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
                extensions[APPROVAL_EXTENSION_KEY] = record_approval_decision(
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
                task.extensions = extensions
                self.store.write_task(task.id, _without_none(task.to_dict()))
            return bool(
                self.mark_task_needs_recovery(
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
            )

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
        return True

__all__ = ["ApprovalLifecycleHost", "ApprovalLifecycleMixin"]
