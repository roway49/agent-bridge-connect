"""Resolve the permission mode for one concrete executor run.

The durable grant envelope lives in :mod:`permission_grants`.  This module is
the only place outside Core that interprets a task packet as permission for a
specific run.  Adapters and Runner therefore agree on the same fail-closed
binding rules without teaching each executor about grant fields.

PERM-103-005: the one-shot fallback does not rewrite the frozen task snapshot.
For an inherited executor policy, ``native`` records the runtime-resolved base;
``inherit`` remains only the selection strategy. Native single-action receipts
are preferred, while this bounded grant preserves the 1.0.3 full fallback.
"""

from __future__ import annotations

from typing import Any

from .permission_grants import permission_grant_from_extensions
from .permission_modes import (
    PERMISSION_EXTENSION_KEY,
    permission_record_from_extensions,
    permission_runtime_policy,
)
from .protocol import ABCError


INPUT_EXTENSION_KEY = "agentbc.input"
SESSION_EXTENSION_KEY = "agentbc.session"


def resolve_effective_permission(
    task: dict[str, Any],
    executor: str,
    executor_run_id: str,
    *,
    trusted_runner_managed: bool = False,
) -> dict[str, Any]:
    """Return the base permission or one matching one-shot ``full`` upgrade.

    A revoked grant is inert, and a consumed grant is inert outside Runner's
    one locked authorization call. An issued grant is active only for an
    approved permission response tied to the current task, executor,
    authoritative session, source run, and the caller's already allocated
    target run. Malformed/unknown grant versions fail through the frozen schema
    validator rather than falling back to the base mode.
    """
    if not isinstance(task, dict):
        raise ABCError(
            "permission_authorization_invalid",
            "Effective permission resolution requires a task packet.",
        )
    extensions = task.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    base = permission_record_from_extensions(extensions, allow_legacy=True)
    grant = permission_grant_from_extensions(extensions)
    if grant is None or grant["state"]["status"] == "revoked":
        return dict(base)

    if not trusted_runner_managed:
        if grant["state"]["status"] == "consumed":
            return dict(base)
        raise ABCError(
            "permission_grant_runner_context_required",
            "An issued permission grant requires explicit trusted Runner-managed context.",
        )

    grant = validate_temporary_permission_context(
        task,
        executor,
        executor_run_id,
        expected_status=str(grant["state"]["status"]),
    )
    return {
        "requested_mode": "full",
        "effective_mode": "full",
        "selection_source": "one_shot_permission_grant",
        "base_mode": str(grant.get("transition", {}).get("from") or ""),
        "temporary": True,
        "executor_run_id": str(executor_run_id).strip(),
        "grant_status": grant["state"]["status"],
    }


def validate_temporary_permission_context(
    task: dict[str, Any],
    executor: str,
    executor_run_id: str,
    *,
    expected_status: str,
) -> dict[str, Any]:
    """Validate one authoritative issued/consumed temporary-grant context.

    This does not authorize an Adapter or resolve argv permission. Runner uses
    it under its lock before consuming an issued grant, then calls the resolver
    with the consumed authoritative snapshot. Adapters may call the resolver
    only after their worker packet explicitly requires Runner authorization.
    """
    if not isinstance(task, dict):
        raise ABCError(
            "permission_authorization_invalid",
            "Temporary permission validation requires a task packet.",
        )
    extensions = task.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    base = permission_record_from_extensions(extensions, allow_legacy=True)
    grant = permission_grant_from_extensions(extensions)
    if grant is None:
        raise ABCError(
            "permission_grant_context_invalid",
            "Temporary permission validation requires a permission grant.",
        )
    if grant["state"]["status"] != expected_status:
        raise ABCError(
            "permission_grant_replay",
            "Permission grant state changed before Runner authorization.",
        )

    if PERMISSION_EXTENSION_KEY not in extensions:
        raise ABCError(
            "permission_grant_base_mismatch",
            "A one-shot permission grant requires an explicit persisted base permission.",
        )
    grant_base_mode = str(grant.get("transition", {}).get("from") or "").strip().lower()
    runtime_base_mode = str(permission_runtime_policy(base)["base_mode"])
    if runtime_base_mode not in {"native", "safe"} or runtime_base_mode != grant_base_mode:
        raise ABCError(
            "permission_grant_base_mismatch",
            "A one-shot permission grant must match an approval-capable persisted base.",
        )
    target_run_id = str(executor_run_id or "").strip()
    if not target_run_id:
        raise ABCError(
            "permission_grant_target_missing",
            "An issued permission grant requires a preallocated executor run ID.",
        )

    task_id = str(task.get("task_id") or task.get("id") or "").strip()
    input_request = extensions.get(INPUT_EXTENSION_KEY)
    session = extensions.get(SESSION_EXTENSION_KEY)
    if not isinstance(input_request, dict) or not isinstance(session, dict):
        raise ABCError(
            "permission_grant_context_invalid",
            "An issued permission grant requires authoritative input and session state.",
        )
    input_id = str(input_request.get("input_id") or "").strip()
    source_run_id = str(input_request.get("executor_run_id") or "").strip()
    session_id = str(session.get("session_id") or "").strip()
    response = input_request.get("response")
    response_type = (
        str(response.get("type") or "").strip().lower()
        if isinstance(response, dict)
        else ""
    )
    run_ids = session.get("run_ids")
    execution = extensions.get("agentbc.execution")
    task_status = task.get("status")
    packet_assignee = str(task.get("assignee") or "").strip().lower()
    context_valid = (
        bool(task_id)
        and task_status in {None, "running"}
        and isinstance(execution, dict)
        and execution.get("internal_status") in {"resuming", "running"}
        and bool(str(execution.get("resuming_at") or "").strip())
        and (
            not packet_assignee
            or str(executor or "").strip().lower() == packet_assignee
        )
        and input_request.get("type") == "permission"
        and input_request.get("status") == "answered"
        and input_request.get("requested_permission") == "full"
        and response_type == "approve"
        and str(session.get("executor") or "").strip().lower()
        == str(executor or "").strip().lower()
        and bool(session_id)
        and isinstance(run_ids, list)
        and bool(run_ids)
        and run_ids[-1] == source_run_id
    )
    if not context_valid:
        raise ABCError(
            "permission_grant_context_invalid",
            "Issued permission grant input/session context is not authoritative for continuation.",
        )

    validated = permission_grant_from_extensions(
        extensions,
        executor=str(executor or "").strip().lower(),
        task_id=task_id,
        input_id=input_id,
        session_id=session_id,
        source_run_id=source_run_id,
        target_run_id=target_run_id if expected_status == "consumed" else None,
    )
    if validated is None:
        raise ABCError(
            "permission_grant_context_invalid",
            "Temporary permission validation requires a permission grant.",
        )
    binding = validated["binding"]
    if binding.get("target_run_id") not in {"", target_run_id}:
        raise ABCError(
            "permission_grant_binding_mismatch",
            "Permission grant target run does not match the allocated executor run.",
        )
    return validated


def is_temporary_permission(record: dict[str, Any]) -> bool:
    """Return whether a resolver result requires Runner-side grant consumption."""
    return record.get("temporary") is True
