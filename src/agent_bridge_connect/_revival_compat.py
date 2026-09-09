"""Temporary FLOW-104-003 revival protocol compatibility surface.

The shared ``agentbc.revival`` implementation is owned by the protocol task.
This small adapter keeps the Codex retry flow usable until that module is
integrated.  It intentionally contains only deterministic data-contract
helpers; filesystem and task lifecycle work belongs in :mod:`retry_flow`.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any


REVIVAL_EXTENSION_KEY = "agentbc.revival"
REVIVAL_VERSION = 1
REVIVAL_OPERATIONS = frozenset({"retry", "handoff"})
REVIVAL_RESERVATION_STATES = frozenset(
    {"reserved", "cleaning", "committed", "rolled_back"}
)
REVIVAL_CLEANUP_SCOPES = frozenset(
    {"managed_artifacts", "report_only_customer_path_preserved"}
)

# Stable public error names used by the retry/handoff family.  The aliases are
# deliberately protocol-shaped so the adapter can be replaced without changing
# callers when the shared Hermes-owned module lands.
REVIVAL_SOURCE_NOT_FAILED = "revival_source_not_failed"
REVIVAL_SOURCE_NOT_HEAD = "revival_source_not_head"
REVIVAL_CHAIN_HEAD_AMBIGUOUS = "revival_chain_head_ambiguous"
REVIVAL_ACTIVE_LEASE = "revival_active_lease"
REVIVAL_ACTIVE_WORKER = "revival_active_worker"
REVIVAL_ACTIVE_INPUT = "revival_input_pending"
REVIVAL_CLEANUP_UNSTABLE = "revival_cleanup_unstable"
REVIVAL_RESERVATION_CONFLICT = "revival_reservation_conflict"
REVIVAL_PATH_PLAN_INVALID = "revival_path_plan_invalid"
REVIVAL_REQUIREMENTS_UNREADABLE = "revival_requirements_unreadable"
REVIVAL_REPORT_PATH_INVALID = "revival_report_path_invalid"
REVIVAL_CLEANUP_FAILED = "revival_cleanup_failed"
REVIVAL_STEP_FLAG_UNSUPPORTED = "retry_step_not_supported"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(payload)


def build_revival_record(
    *,
    operation: str,
    reservation_id: str,
    source_task_id: str,
    source_attempt_index: int,
    target_task_id: str,
    target_attempt_index: int,
    path_plan_digest: str,
    policy_digest: str,
    cleanup_scope: str,
    requirements_digest: str,
    report_digest: str,
    inherited_done: list[int] | None = None,
    resumed_step_ids: list[int] | None = None,
    warnings: list[str] | None = None,
    state: str = "reserved",
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    source_attempt = int(source_attempt_index)
    target_attempt = int(target_attempt_index)
    record = {
        "version": REVIVAL_VERSION,
        "operation": str(operation),
        "state": str(state),
        "reservation_id": str(reservation_id),
        "source_task_id": str(source_task_id),
        "target_task_id": str(target_task_id),
        "source_attempt_index": source_attempt,
        "target_attempt_index": target_attempt,
        "attempt_index": target_attempt,
        "path_plan_digest": str(path_plan_digest),
        "policy_digest": str(policy_digest),
        "cleanup_scope": str(cleanup_scope),
        "requirements_digest": str(requirements_digest),
        "report_digest": str(report_digest),
        "inherited_done": sorted({int(item) for item in (inherited_done or [])}),
        "resumed_step_ids": sorted({int(item) for item in (resumed_step_ids or [])}),
        "warnings": [str(item) for item in (warnings or [])],
        "allowed_next_actions": [],
        "recommended_action": "",
        "created_at": now,
        "updated_at": now,
        "history": copy.deepcopy(list(history or []))[-8:],
    }
    return record


def validate_revival_record(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return [f"{REVIVAL_EXTENSION_KEY} must be an object"]
    errors: list[str] = []
    if value.get("version") != REVIVAL_VERSION:
        errors.append(f"{REVIVAL_EXTENSION_KEY}.version must be {REVIVAL_VERSION}")
    if value.get("operation") not in REVIVAL_OPERATIONS:
        errors.append(f"{REVIVAL_EXTENSION_KEY}.operation is invalid")
    if value.get("state") not in REVIVAL_RESERVATION_STATES | {"available"}:
        errors.append(f"{REVIVAL_EXTENSION_KEY}.state is invalid")
    for field in (
        "reservation_id",
        "source_task_id",
        "target_task_id",
        "path_plan_digest",
        "policy_digest",
        "cleanup_scope",
        "requirements_digest",
        "report_digest",
        "created_at",
        "updated_at",
    ):
        if not isinstance(value.get(field), str) or not value.get(field):
            errors.append(f"{REVIVAL_EXTENSION_KEY}.{field} must be non-empty")
    if value.get("cleanup_scope") not in REVIVAL_CLEANUP_SCOPES:
        errors.append(f"{REVIVAL_EXTENSION_KEY}.cleanup_scope is invalid")
    for field in (
        "source_attempt_index",
        "target_attempt_index",
        "attempt_index",
    ):
        if type(value.get(field)) is not int or value.get(field) < 0:
            errors.append(f"{REVIVAL_EXTENSION_KEY}.{field} must be a non-negative integer")
    for field in ("inherited_done", "resumed_step_ids", "warnings", "history"):
        if not isinstance(value.get(field), list):
            errors.append(f"{REVIVAL_EXTENSION_KEY}.{field} must be a list")
    return errors


def public_revival_projection(value: Any) -> dict[str, Any] | None:
    """Return the bounded status/report projection without reservation secrets."""
    if not isinstance(value, dict):
        return None
    projection: dict[str, Any] = {}
    for field in (
        "version",
        "operation",
        "state",
        "source_task_id",
        "target_task_id",
        "source_attempt_index",
        "target_attempt_index",
        "attempt_index",
        "path_plan_digest",
        "policy_digest",
        "cleanup_scope",
        "requirements_digest",
        "report_digest",
        "inherited_done",
        "resumed_step_ids",
        "warnings",
        "allowed_next_actions",
        "recommended_action",
        "created_at",
        "updated_at",
    ):
        if field in value:
            projection[field] = copy.deepcopy(value[field])
    return projection


def failed_revival_projection(task: Any) -> dict[str, Any]:
    """Project mechanical choices for a failed task without changing state."""
    extensions = getattr(task, "extensions", None) or {}
    existing = extensions.get(REVIVAL_EXTENSION_KEY)
    projection = public_revival_projection(existing) or {
        "version": REVIVAL_VERSION,
        "operation": "retry",
        "state": "available",
        "source_task_id": str(getattr(task, "id", "")),
        "target_task_id": str(getattr(task, "id", "")),
        "source_attempt_index": 0,
        "target_attempt_index": 1,
        "attempt_index": 0,
        "path_plan_digest": "",
        "policy_digest": "",
        "cleanup_scope": "",
        "requirements_digest": "",
        "report_digest": "",
        "inherited_done": [],
        "resumed_step_ids": [
            int(step.get("id", index))
            for index, step in enumerate(getattr(task, "steps", []) or [], 1)
            if isinstance(step, dict)
        ],
        "warnings": [],
        "created_at": "",
        "updated_at": "",
    }
    if str(getattr(task, "status", "")) == "failed":
        projection["allowed_next_actions"] = ["retry", "handoff"]
        projection["recommended_action"] = "retry"
    else:
        projection["allowed_next_actions"] = []
        projection["recommended_action"] = ""
    return projection
