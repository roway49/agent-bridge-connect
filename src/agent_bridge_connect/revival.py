"""Shared FLOW-104-003 revival protocol: ``agentbc.revival`` v1.

This module owns the Core-defined revival contract for failed and
``needs_recovery`` tasks.  It is a
focused protocol module: deterministic data, validation, serialization,
redacted projection, mechanical preflight facts and idempotent replay
helpers.  It deliberately does **not** execute retry filesystem cleanup and
does **not** create handoff tasks; those belong to the sibling CLI/service
implementations that consume these primitives.

Fixed meanings (authoritative for every consumer)
-------------------------------------------------

``retry``
    Keeps the Task ID.  Deletes the failed attempt's failure report, resets
    every step, and clears only AgentBC-managed default artifacts.  It never
    deletes custom-path (``customer_dir=true``) contents: the only retry
    cleanup scope is ``managed_default_artifacts``.

``handoff``
    Preserves all source evidence, creates a new iteration with a new Task ID,
    mechanically imports the prior requirements/task record and the failure
    report (by digest), locks completed steps as ``inherited_done`` and
    resumes the remainder.

The authoritative task record wins: when the stored report's step statuses
differ from the task record, :func:`revival_step_bindings` keeps the task
record's binding and emits the ``source_report_step_mismatch`` warning
instead of an error.  A differing report never blocks a mechanically valid
handoff.

Mechanical preflight
--------------------

:func:`evaluate_revival_preflight` is the common revivable-current-head gate.
It requires status ``failed`` or ``needs_recovery``, the exact current chain head, a closed
RunLease, no active worker/dispatch, no unresolved input, stable session
cleanup, readable requirements, valid lineage and PathPlan, and at most one
open retry/handoff reservation.  ``allowed_next_actions`` and
``recommended_action`` are mechanical status/report data: the failure
taxonomy only orders the recommendation inside the mechanically allowed set
and can never permanently suppress a mechanically valid user choice.

The record is bounded and path-free.  Only stable identifiers, SHA-256
digests (``sha256:<64 hex>``), enumerated states and bounded step-id lists
are stored, so the same record is safe for status/report projection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .protocol import ABCError

REVIVAL_EXTENSION_KEY = "agentbc.revival"
REVIVAL_PROTOCOL_VERSION = 1

REVIVAL_OPERATION_RETRY = "retry"
REVIVAL_OPERATION_HANDOFF = "handoff"
#: Canonical display/serialization order for operations.
REVIVAL_OPERATIONS = (REVIVAL_OPERATION_RETRY, REVIVAL_OPERATION_HANDOFF)
_REVIVAL_OPERATION_SET = frozenset(REVIVAL_OPERATIONS)
#: Both terminal failure states use the same mechanical retry/handoff contract.
REVIVAL_SOURCE_STATUSES = frozenset({"failed", "needs_recovery"})

REVIVAL_STATE_RESERVED = "reserved"
REVIVAL_STATE_COMMITTED = "committed"
REVIVAL_STATE_RELEASED = "released"
REVIVAL_RESERVATION_STATES = frozenset(
    {REVIVAL_STATE_RESERVED, REVIVAL_STATE_COMMITTED, REVIVAL_STATE_RELEASED}
)
#: Only an open reservation blocks a new revival of the same source task.
REVIVAL_BLOCKING_RESERVATION_STATES = frozenset({REVIVAL_STATE_RESERVED})

REVIVAL_CLEANUP_SCOPE_NONE = "none"
REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS = "managed_default_artifacts"
REVIVAL_CLEANUP_SCOPES = frozenset(
    {REVIVAL_CLEANUP_SCOPE_NONE, REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS}
)
#: Retry may clear only AgentBC-managed default artifacts; custom-path
#: (customer_dir) contents are never eligible for revival cleanup.
REVIVAL_RETRY_CLEANUP_SCOPE = REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS
REVIVAL_HANDOFF_CLEANUP_SCOPE = REVIVAL_CLEANUP_SCOPE_NONE
_REVIVAL_OPERATION_CLEANUP_SCOPE = {
    REVIVAL_OPERATION_RETRY: REVIVAL_RETRY_CLEANUP_SCOPE,
    REVIVAL_OPERATION_HANDOFF: REVIVAL_HANDOFF_CLEANUP_SCOPE,
}

#: Session-cleanup states that count as stable for the revival preflight.
#: Mirrors the resolved cleanup lifecycle states (``RESOLVED_CLEANUP_STATES``);
#: ``retained`` sessions are intentionally kept and therefore resolved, not
#: unstable.  Cleanup states are an independent receipt from terminal state
#: (FLOW-104-002), so a failed task's cleanup may still legitimately be in
#: flight; only a resolved receipt proves revival is safe to reserve.
REVIVAL_STABLE_CLEANUP_STATES = frozenset({"retained", "succeeded", "unsupported"})

# ------------------------------------------------------------ error codes ---
REVIVAL_OPERATION_INVALID = "revival_operation_invalid"
REVIVAL_SOURCE_STATUS_INVALID = "revival_source_status_invalid"
REVIVAL_SOURCE_NOT_CHAIN_HEAD = "revival_source_not_chain_head"
REVIVAL_RUN_LEASE_OPEN = "revival_run_lease_open"
REVIVAL_WORKER_ACTIVE = "revival_worker_active"
REVIVAL_DISPATCH_ACTIVE = "revival_dispatch_active"
REVIVAL_INPUT_UNRESOLVED = "revival_input_unresolved"
REVIVAL_SESSION_CLEANUP_UNSTABLE = "revival_session_cleanup_unstable"
REVIVAL_REQUIREMENTS_UNREADABLE = "revival_requirements_unreadable"
REVIVAL_LINEAGE_INVALID = "revival_lineage_invalid"
REVIVAL_PATH_PLAN_INVALID = "revival_path_plan_invalid"
REVIVAL_RESERVATION_CONFLICT = "revival_reservation_conflict"
REVIVAL_RESERVATION_INVALID = "revival_reservation_invalid"

#: Stable revival rejection codes.  CLI/service consumers must surface these
#: verbatim; they are the contract's whole failure vocabulary.
REVIVAL_ERROR_CODES = frozenset(
    {
        REVIVAL_OPERATION_INVALID,
        REVIVAL_SOURCE_STATUS_INVALID,
        REVIVAL_SOURCE_NOT_CHAIN_HEAD,
        REVIVAL_RUN_LEASE_OPEN,
        REVIVAL_WORKER_ACTIVE,
        REVIVAL_DISPATCH_ACTIVE,
        REVIVAL_INPUT_UNRESOLVED,
        REVIVAL_SESSION_CLEANUP_UNSTABLE,
        REVIVAL_REQUIREMENTS_UNREADABLE,
        REVIVAL_LINEAGE_INVALID,
        REVIVAL_PATH_PLAN_INVALID,
        REVIVAL_RESERVATION_CONFLICT,
        REVIVAL_RESERVATION_INVALID,
    }
)

# ---------------------------------------------------------- warning codes ---
REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH = "source_report_step_mismatch"
REVIVAL_WARNING_SOURCE_REPORT_ABSENT = "source_report_absent"
REVIVAL_WARNING_SOURCE_REPORT_UNREADABLE = "source_report_unreadable"
REVIVAL_WARNING_REVIVAL_REPLAYED = "revival_replayed"

#: Stable, non-blocking revival warnings.  The authoritative task record
#: wins over the stored report; report problems degrade to warnings.
REVIVAL_WARNING_CODES = frozenset(
    {
        REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH,
        REVIVAL_WARNING_SOURCE_REPORT_ABSENT,
        REVIVAL_WARNING_SOURCE_REPORT_UNREADABLE,
        REVIVAL_WARNING_REVIVAL_REPLAYED,
    }
)

REVIVAL_RECORD_FIELDS = (
    "version",
    "revival_id",
    "operation",
    "state",
    "created_at",
    "updated_at",
    "source_task_id",
    "source_attempt_id",
    "target_task_id",
    "target_attempt_id",
    "source_report_digest",
    "source_requirements_digest",
    "path_plan_digest",
    "policy_digest",
    "cleanup_scope",
    "inherited_done_step_ids",
    "resumed_step_ids",
    "warnings",
)

_REVIVAL_ID_RE = re.compile(r"^REV-[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_STEP_ID = 1000
_MAX_WARNINGS = 8
_MAX_STEP_BINDINGS = 1000
_REPORT_STATES = frozenset({"readable", "absent", "unreadable"})
_DONE_STEP_STATUSES = frozenset({"done", "completed"})
_LEASE_CLOSED_STATES = frozenset({"closed", "missing"})

#: Failure-kind markers that mechanically recommend retry.  Mirrors the
#: explicit retryable markers used by the worker terminal routing; the
#: taxonomy only orders ``recommended_action`` and never gates eligibility.
_REVIVAL_RETRY_MARKERS = (
    "transport",
    "infrastructure",
    "connection",
    "timeout",
    "runner_status",
    "runner_unavailable",
    "api_",
    "interrupted",
    "incomplete",
    "transient",
)


@dataclass(frozen=True)
class RevivalPreflight:
    """Deterministic result of the common failed-current-head revival gate."""

    ok: bool
    error_codes: tuple[str, ...]
    allowed_next_actions: tuple[str, ...]
    recommended_action: str
    warnings: tuple[str, ...]
    replay_revival_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_codes": list(self.error_codes),
            "allowed_next_actions": list(self.allowed_next_actions),
            "recommended_action": self.recommended_action,
            "warnings": list(self.warnings),
            "replay_revival_id": self.replay_revival_id,
        }


# ------------------------------------------------------------------ helpers -
def _utc_now_text(now: str | None = None) -> str:
    if now is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(now).strip()
    if not _is_iso_utc(text):
        raise ABCError(
            REVIVAL_RESERVATION_INVALID,
            "revival timestamp must be an ISO-8601 UTC instant",
        )
    return text


def _is_iso_utc(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _normalize_step_bindings(
    steps: Any,
) -> list[tuple[int, str]]:
    normalized: list[tuple[int, str]] = []
    for step in steps or ():
        if isinstance(step, dict):
            raw_id = step.get("id")
            status = str(step.get("status") or "pending").strip().lower()
        elif isinstance(step, (tuple, list)) and len(step) == 2:
            raw_id, status = step
            status = str(status or "pending").strip().lower()
        else:
            continue
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            continue
        if not 1 <= raw_id <= _MAX_STEP_ID:
            continue
        normalized.append((raw_id, status or "pending"))
    normalized.sort(key=lambda item: item[0])
    return normalized


def _normalize_step_ids(step_ids: Any) -> list[int]:
    if not isinstance(step_ids, list):
        return []
    ids: list[int] = []
    for value in step_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            return []
        if not 1 <= value <= _MAX_STEP_ID:
            return []
        ids.append(value)
    if ids != sorted(set(ids)):
        return []
    return ids


# ------------------------------------------------------------------ digests -
def revival_digest(payload: str | bytes) -> str:
    """Return the stable ``sha256:<hex>`` digest of the exact payload bytes."""

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def revival_path_plan_digest(workspace: Any) -> str:
    """Bind the revival to the source task's authoritative PathPlan."""

    return revival_digest(_canonical_json(workspace if isinstance(workspace, dict) else {}))


def revival_policy_digest(extensions: Any) -> str:
    """Bind the revival to the frozen permission/resource policy snapshot."""

    value = extensions if isinstance(extensions, dict) else {}
    try:
        from .execution_policy import execution_policy_view
        from .permission_modes import permission_record_from_extensions

        snapshot = {
            "execution_policy": execution_policy_view(value),
            "permission": permission_record_from_extensions(value),
        }
    except Exception:  # noqa: BLE001 - digest must stay total and deterministic
        snapshot = {"execution_policy": None, "permission": None}
    return revival_digest(_canonical_json(snapshot))


def revival_intent_fingerprint(
    operation: str,
    source_task_id: str,
    source_attempt_id: str = "",
) -> str:
    """Return the deterministic replay fingerprint of one revival intent."""

    return revival_digest(
        _canonical_json(
            {
                "operation": str(operation or ""),
                "source_task_id": str(source_task_id or ""),
                "source_attempt_id": str(source_attempt_id or ""),
            }
        )
    )


# ----------------------------------------------------------- step bindings --
def revival_step_bindings(
    task_steps: Any,
    report_steps: Any = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Compare task-record step statuses against the stored report's.

    The authoritative task record always wins.  A differing report status is
    surfaced as the stable ``source_report_step_mismatch`` warning; it never
    produces a validation error and never blocks a mechanically valid
    handoff.  Bindings carry only step ids and statuses - never step text.
    """

    bindings: list[dict[str, Any]] = []
    warnings: list[str] = []
    report_status_by_id: dict[int, str] = {}
    for step in report_steps or ():
        if not isinstance(step, dict):
            continue
        raw_id = step.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            continue
        report_status_by_id[raw_id] = str(step.get("status") or "").strip().lower()
    for step_id, task_status in _normalize_step_bindings(task_steps):
        report_status = str(report_status_by_id.get(step_id, ""))
        binding = {
            "step_id": step_id,
            "task_status": task_status,
            "report_status": report_status,
        }
        bindings.append(binding)
        if report_status and report_status != task_status:
            if REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH not in warnings:
                warnings.append(REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH)
    if len(bindings) > _MAX_STEP_BINDINGS:
        bindings = bindings[:_MAX_STEP_BINDINGS]
    return tuple(bindings), tuple(warnings)


# -------------------------------------------------------------- reservation -
def build_revival_reservation(
    *,
    operation: str,
    source_task_id: str,
    source_attempt_id: str = "",
    steps: Any = None,
    source_report_digest: str = "",
    source_requirements_digest: str = "",
    path_plan_digest: str = "",
    policy_digest: str = "",
    target_task_id: str = "",
    target_attempt_id: str = "",
    now: str | None = None,
    revival_id: str | None = None,
) -> dict[str, Any]:
    """Build one validated ``agentbc.revival`` v1 reservation record.

    Step bindings are derived mechanically from the authoritative task
    record: ``handoff`` locks done steps as ``inherited_done`` and resumes
    the remainder; ``retry`` resets every step and locks nothing.
    """

    if operation not in _REVIVAL_OPERATION_SET:
        raise ABCError(
            REVIVAL_OPERATION_INVALID,
            f"unknown revival operation: {operation}",
        )
    if operation == REVIVAL_OPERATION_RETRY and str(target_task_id or "").strip():
        raise ABCError(
            REVIVAL_OPERATION_INVALID,
            "retry keeps the Task ID; a target task binding is not allowed",
        )
    timestamp = _utc_now_text(now)
    normalized_steps = _normalize_step_bindings(steps)
    if operation == REVIVAL_OPERATION_HANDOFF:
        inherited = [step_id for step_id, status in normalized_steps if status in _DONE_STEP_STATUSES]
        resumed = [step_id for step_id, status in normalized_steps if status not in _DONE_STEP_STATUSES]
    else:
        inherited = []
        resumed = [step_id for step_id, _ in normalized_steps]
    record = {
        "version": REVIVAL_PROTOCOL_VERSION,
        "revival_id": str(revival_id or f"REV-{uuid.uuid4().hex}"),
        "operation": operation,
        "state": REVIVAL_STATE_RESERVED,
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_task_id": str(source_task_id or "").strip(),
        "source_attempt_id": str(source_attempt_id or "").strip(),
        "target_task_id": str(target_task_id or "").strip(),
        "target_attempt_id": str(target_attempt_id or "").strip(),
        "source_report_digest": str(source_report_digest or "").strip(),
        "source_requirements_digest": str(source_requirements_digest or "").strip(),
        "path_plan_digest": str(path_plan_digest or "").strip(),
        "policy_digest": str(policy_digest or "").strip(),
        "cleanup_scope": _REVIVAL_OPERATION_CLEANUP_SCOPE[operation],
        "inherited_done_step_ids": inherited,
        "resumed_step_ids": resumed,
        "warnings": [],
    }
    errors = validate_revival_reservation(record)
    if errors:
        raise ABCError(
            REVIVAL_RESERVATION_INVALID,
            "revival reservation is invalid",
            {"errors": errors},
        )
    return record


def validate_revival_reservation(record: Any) -> list[str]:
    """Return the ordered list of validation errors; empty means valid."""

    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record: must be an object"]
    unknown = sorted(set(record) - set(REVIVAL_RECORD_FIELDS))
    if unknown:
        errors.append("record: unknown fields " + ",".join(unknown))
        return errors

    def _flag(field: str, reason: str) -> None:
        errors.append(f"{field}: {reason}")

    if record.get("version") != REVIVAL_PROTOCOL_VERSION:
        _flag("version", "must be " + str(REVIVAL_PROTOCOL_VERSION))
    revival_id = str(record.get("revival_id") or "")
    if not _REVIVAL_ID_RE.fullmatch(revival_id):
        _flag("revival_id", "must match REV-<32 hex>")
    operation = record.get("operation")
    if operation not in _REVIVAL_OPERATION_SET:
        _flag("operation", "must be retry or handoff")
    state = record.get("state")
    if state not in REVIVAL_RESERVATION_STATES:
        _flag("state", "must be reserved, committed or released")
    for field in ("created_at", "updated_at"):
        if not _is_iso_utc(record.get(field)):
            _flag(field, "must be an ISO-8601 UTC instant")
    source_task_id = str(record.get("source_task_id") or "")
    if not _SAFE_ID_RE.fullmatch(source_task_id):
        _flag("source_task_id", "must be a non-empty bounded identifier")
    for field in ("source_attempt_id", "target_attempt_id"):
        value = str(record.get(field) or "")
        if value and not _SAFE_ID_RE.fullmatch(value):
            _flag(field, "must be a bounded identifier")
    target_task_id = str(record.get("target_task_id") or "")
    if target_task_id and not _SAFE_ID_RE.fullmatch(target_task_id):
        _flag("target_task_id", "must be a bounded identifier")
    for field in (
        "source_report_digest",
        "source_requirements_digest",
        "path_plan_digest",
        "policy_digest",
    ):
        value = str(record.get(field) or "")
        if value and not _DIGEST_RE.fullmatch(value):
            _flag(field, "must be empty or sha256:<64 hex>")
    cleanup_scope = record.get("cleanup_scope")
    if cleanup_scope not in REVIVAL_CLEANUP_SCOPES:
        _flag("cleanup_scope", "must be none or managed_default_artifacts")
    elif operation in _REVIVAL_OPERATION_SET:
        expected_scope = _REVIVAL_OPERATION_CLEANUP_SCOPE[operation]
        if cleanup_scope != expected_scope:
            _flag("cleanup_scope", f"must be {expected_scope} for {operation}")
    if operation == REVIVAL_OPERATION_RETRY:
        if target_task_id:
            _flag("target_task_id", "retry keeps the Task ID and has no target task")
        if record.get("inherited_done_step_ids") != []:
            _flag("inherited_done_step_ids", "retry resets every step and locks nothing")
    for field in ("inherited_done_step_ids", "resumed_step_ids"):
        if _normalize_step_ids(record.get(field)) is None or (
            record.get(field) is not None
            and not isinstance(record.get(field), list)
        ):
            _flag(field, "must be a sorted list of unique step ids")
        elif _normalize_step_ids(record.get(field)) != (record.get(field) or []):
            _flag(field, "must be a sorted list of unique step ids")
    if operation == REVIVAL_OPERATION_HANDOFF:
        inherited = record.get("inherited_done_step_ids") or []
        resumed = record.get("resumed_step_ids") or []
        if isinstance(inherited, list) and isinstance(resumed, list):
            overlap = sorted(set(inherited) & set(resumed))
            if overlap:
                _flag("resumed_step_ids", "overlap inherited_done_step_ids " + str(overlap))
    warnings = record.get("warnings")
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) or item not in REVIVAL_WARNING_CODES for item in warnings
    ):
        _flag("warnings", "must be a list of stable revival warning codes")
    elif len(warnings) != len(set(warnings)):
        _flag("warnings", "must not repeat a warning code")
    elif len(warnings) > _MAX_WARNINGS:
        _flag("warnings", f"must carry at most {_MAX_WARNINGS} entries")
    return errors


def revival_from_extensions(extensions: Any) -> dict[str, Any] | None:
    """Return the validated stored reservation, or ``None`` when absent.

    Backward compatible: tasks stored before this protocol have no
    ``agentbc.revival`` extension and project as ``None``.  A malformed
    stored record also projects as ``None`` here; the preflight fail-closes
    such records as ``revival_reservation_invalid`` so a second reservation
    can never be created over unreadable revival state.
    """

    if not isinstance(extensions, dict):
        return None
    raw = extensions.get(REVIVAL_EXTENSION_KEY)
    if not isinstance(raw, dict):
        return None
    if validate_revival_reservation(raw):
        return None
    return copy.deepcopy(raw)


def revival_to_extensions(record: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical extension payload for the reservation record."""

    return {REVIVAL_EXTENSION_KEY: copy.deepcopy(record)}


def open_revival_reservation(extensions: Any) -> dict[str, Any] | None:
    """Return the open (``reserved``) reservation for the task, if any."""

    record = revival_from_extensions(extensions)
    if record is not None and record.get("state") == REVIVAL_STATE_RESERVED:
        return record
    return None


def revival_replay_or_reserve(
    extensions: Any,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return ``(record, replayed)`` idempotently for one revival intent.

    When an open reservation with the same intent fingerprint already exists
    the stored record is returned unchanged (``replayed=True``); no second
    reservation is ever created for the same intent.
    """

    open_record = open_revival_reservation(extensions)
    if open_record is not None and isinstance(candidate, dict):
        same_intent = (
            open_record.get("operation") == candidate.get("operation")
            and open_record.get("source_task_id") == candidate.get("source_task_id")
            and open_record.get("source_attempt_id") == candidate.get("source_attempt_id")
        )
        if same_intent:
            return open_record, True
    if isinstance(candidate, dict):
        return copy.deepcopy(candidate), False
    raise ABCError(REVIVAL_RESERVATION_INVALID, "revival candidate must be an object")


def commit_revival_reservation(
    record: dict[str, Any],
    *,
    target_task_id: str = "",
    target_attempt_id: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    """Move an open reservation to ``committed`` and bind target facts."""

    if validate_revival_reservation(record):
        raise ABCError(REVIVAL_RESERVATION_INVALID, "revival reservation is invalid")
    if record.get("state") != REVIVAL_STATE_RESERVED:
        raise ABCError(
            REVIVAL_RESERVATION_INVALID,
            "only a reserved revival can be committed",
        )
    operation = record.get("operation")
    target = str(target_task_id or "").strip()
    if operation == REVIVAL_OPERATION_HANDOFF and not target:
        raise ABCError(
            REVIVAL_OPERATION_INVALID,
            "handoff commit requires the target task id",
        )
    if operation == REVIVAL_OPERATION_RETRY and target:
        raise ABCError(
            REVIVAL_OPERATION_INVALID,
            "retry keeps the Task ID; a target task binding is not allowed",
        )
    committed = copy.deepcopy(record)
    committed["state"] = REVIVAL_STATE_COMMITTED
    committed["updated_at"] = _utc_now_text(now)
    if target:
        committed["target_task_id"] = target
    if str(target_attempt_id or "").strip():
        committed["target_attempt_id"] = str(target_attempt_id).strip()
    errors = validate_revival_reservation(committed)
    if errors:
        raise ABCError(
            REVIVAL_RESERVATION_INVALID,
            "committed revival reservation is invalid",
            {"errors": errors},
        )
    return committed


def release_revival_reservation(
    record: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Release an open reservation; committed history is never rewound."""

    if validate_revival_reservation(record):
        raise ABCError(REVIVAL_RESERVATION_INVALID, "revival reservation is invalid")
    if record.get("state") != REVIVAL_STATE_RESERVED:
        return copy.deepcopy(record)
    released = copy.deepcopy(record)
    released["state"] = REVIVAL_STATE_RELEASED
    released["updated_at"] = _utc_now_text(now)
    return released


# ---------------------------------------------------------------- preflight -
def revival_facts_from_task(
    task: Any,
    *,
    is_chain_head: bool | None = None,
    lease_state: str | None = None,
    worker_active: bool | None = None,
    dispatch_active: bool | None = None,
    requirements_readable: bool | None = None,
    lineage_valid: bool | None = None,
    path_plan_valid: bool | None = None,
    report_state: str | None = None,
    warnings: Any = None,
) -> dict[str, Any]:
    """Derive the mechanical preflight facts from one authoritative task.

    Defaults fail closed: an unknown chain-head answer, an unknown lease
    state or an unreadable requirements record blocks revival until the
    caller (the sibling CLI/service integration) supplies the authoritative
    observation.  Only Core-owned task state is read; no executor output.
    """

    data = task if isinstance(task, dict) else {}
    extensions = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}

    failure_kind = ""
    failure_layer = ""
    errors = [item for item in (data.get("errors") or []) if isinstance(item, dict)]
    if errors:
        latest = errors[-1]
        failure_kind = str(latest.get("code") or "").strip()
        details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
        failure = details.get("failure") if isinstance(details.get("failure"), dict) else {}
        failure_kind = str(failure.get("kind") or failure_kind).strip()
        failure_layer = str(failure.get("layer") or "").strip().lower()

    input_request = extensions.get("agentbc.input")
    input_unresolved = isinstance(input_request, dict) and (
        str(input_request.get("status") or "").strip().lower() == "waiting"
    )

    cleanup_state = "not_requested"
    session = extensions.get("agentbc.session")
    if isinstance(session, dict):
        try:
            from .execution_policy import session_cleanup_view

            cleanup_state = str(
                session_cleanup_view(session.get("cleanup")).get("state") or "not_requested"
            )
        except Exception:  # noqa: BLE001 - projection must never break facts
            cleanup_state = "not_requested"

    if requirements_readable is None:
        requirements_readable = _requirements_readable(
            workspace if isinstance(workspace, dict) else {}
        )

    lineage = extensions.get("agentbc.lineage")
    if lineage_valid is None:
        lineage_valid = lineage is None or isinstance(lineage, dict)

    if path_plan_valid is None:
        path_plan_valid = False
        if workspace:
            try:
                from .path_model import validate_path_plan_workspace

                validate_path_plan_workspace(workspace)
                path_plan_valid = True
            except Exception:  # noqa: BLE001 - fail closed on any plan problem
                path_plan_valid = False

    if report_state is None:
        report_state = _report_state(workspace if isinstance(workspace, dict) else {})

    facts: dict[str, Any] = {
        "status": str(data.get("status") or "").strip().lower(),
        "is_chain_head": bool(is_chain_head) if is_chain_head is not None else False,
        "lease_state": str(lease_state or "").strip().lower(),
        "worker_active": bool(worker_active),
        "dispatch_active": bool(dispatch_active),
        "input_unresolved": input_unresolved,
        "session_cleanup_state": cleanup_state,
        "requirements_readable": bool(requirements_readable),
        "lineage_valid": bool(lineage_valid),
        "path_plan_valid": bool(path_plan_valid),
        "report_state": report_state if report_state in _REPORT_STATES else "absent",
        "failure_kind": failure_kind,
        "failure_layer": failure_layer,
        "source_task_id": str(data.get("id") or "").strip(),
        "source_attempt_id": _latest_attempt_id(extensions),
        "warnings": tuple(
            warning for warning in (warnings or ()) if warning in REVIVAL_WARNING_CODES
        ),
    }
    if isinstance(extensions, dict):
        facts["reservation_raw"] = extensions.get(REVIVAL_EXTENSION_KEY)
    return facts


def _requirements_readable(workspace: dict[str, Any]) -> bool:
    task_file = str(workspace.get("task_file") or "").strip()
    if not task_file:
        return False
    try:
        path = os.path.realpath(os.path.expanduser(task_file))
        return os.path.isfile(path) and os.access(path, os.R_OK)
    except OSError:
        return False


def _report_state(workspace: dict[str, Any]) -> str:
    report_file = str(workspace.get("report_file") or "").strip()
    if not report_file:
        return "absent"
    try:
        path = os.path.realpath(os.path.expanduser(report_file))
    except OSError:
        return "absent"
    if not os.path.exists(path):
        return "absent"
    return "readable" if (os.path.isfile(path) and os.access(path, os.R_OK)) else "unreadable"


def _latest_attempt_id(extensions: dict[str, Any]) -> str:
    execution = extensions.get("agentbc.execution")
    if isinstance(execution, dict):
        return str(execution.get("worker_run_id") or "").strip()
    return ""


def _taxonomy_recommendation(failure_kind: str, failure_layer: str) -> str:
    kind = str(failure_kind or "").strip().lower()
    layer = str(failure_layer or "").strip().lower()
    if (
        layer == "permission"
        or layer == "flow_contract"
        or "denied" in kind
        or "cancel" in kind
        or "user_terminated" in kind
        or "contract" in kind
        or "callback" in kind
    ):
        return REVIVAL_OPERATION_HANDOFF
    return REVIVAL_OPERATION_RETRY


def _clamp_recommendation(recommended: str, allowed: tuple[str, ...]) -> str:
    if recommended in allowed:
        return recommended
    return allowed[0] if allowed else ""


def evaluate_revival_preflight(
    facts: dict[str, Any],
    *,
    requested_operation: str | None = None,
) -> RevivalPreflight:
    """Evaluate the common revivable-current-head revival gate.

    Every gate maps to one stable revival error code.  ``allowed_next_actions``
    is empty unless all mechanical gates pass; the failure taxonomy only
    chooses ``recommended_action`` inside the allowed set.  A requested
    operation is never permanently suppressed by the taxonomy: a mechanically
    valid operation stays allowed even when the taxonomy would recommend the
    other one.
    """

    facts = facts if isinstance(facts, dict) else {}
    error_codes: list[str] = []
    warnings: list[str] = []

    status = str(facts.get("status") or "").strip().lower()
    if status not in REVIVAL_SOURCE_STATUSES:
        error_codes.append(REVIVAL_SOURCE_STATUS_INVALID)
    if facts.get("is_chain_head") is not True:
        error_codes.append(REVIVAL_SOURCE_NOT_CHAIN_HEAD)
    if requested_operation is not None and requested_operation not in _REVIVAL_OPERATION_SET:
        error_codes.append(REVIVAL_OPERATION_INVALID)

    lease_state = str(facts.get("lease_state") or "").strip().lower()
    if lease_state not in _LEASE_CLOSED_STATES:
        error_codes.append(REVIVAL_RUN_LEASE_OPEN)
    if facts.get("worker_active") is True:
        error_codes.append(REVIVAL_WORKER_ACTIVE)
    if facts.get("dispatch_active") is True:
        error_codes.append(REVIVAL_DISPATCH_ACTIVE)
    if facts.get("input_unresolved") is True:
        error_codes.append(REVIVAL_INPUT_UNRESOLVED)
    cleanup_state = str(facts.get("session_cleanup_state") or "not_requested").strip().lower()
    recovery_session_not_cleaned = (
        status == "needs_recovery" and cleanup_state == "not_requested"
    )
    if (
        cleanup_state not in REVIVAL_STABLE_CLEANUP_STATES
        and not recovery_session_not_cleaned
    ):
        error_codes.append(REVIVAL_SESSION_CLEANUP_UNSTABLE)
    if facts.get("requirements_readable") is not True:
        error_codes.append(REVIVAL_REQUIREMENTS_UNREADABLE)
    if facts.get("lineage_valid") is not True:
        error_codes.append(REVIVAL_LINEAGE_INVALID)
    if facts.get("path_plan_valid") is not True:
        error_codes.append(REVIVAL_PATH_PLAN_INVALID)

    report_state = str(facts.get("report_state") or "").strip().lower()
    if report_state == "absent":
        warnings.append(REVIVAL_WARNING_SOURCE_REPORT_ABSENT)
    elif report_state == "unreadable":
        warnings.append(REVIVAL_WARNING_SOURCE_REPORT_UNREADABLE)
    for warning in facts.get("warnings") or ():
        if warning in REVIVAL_WARNING_CODES and warning not in warnings:
            warnings.append(warning)

    replay_revival_id = ""
    raw_reservation = facts.get("reservation_raw")
    if raw_reservation is not None:
        # ``reservation_raw`` is the stored ``agentbc.revival`` record itself
        # (or an extensions dict carrying it).  Both shapes are accepted; a
        # record that fails validation fail-closes the preflight so no second
        # reservation can ever be created over unreadable revival state.
        raw_record = raw_reservation
        if isinstance(raw_reservation, dict) and REVIVAL_EXTENSION_KEY in raw_reservation:
            raw_record = raw_reservation.get(REVIVAL_EXTENSION_KEY)
        if not isinstance(raw_record, dict):
            error_codes.append(REVIVAL_RESERVATION_INVALID)
        else:
            stored = revival_from_extensions({REVIVAL_EXTENSION_KEY: raw_record})
            if stored is None:
                error_codes.append(REVIVAL_RESERVATION_INVALID)
            elif stored.get("state") in REVIVAL_BLOCKING_RESERVATION_STATES:
                same_intent = (
                    str(stored.get("operation") or "")
                    and str(stored.get("source_task_id") or "")
                    == str(facts.get("source_task_id") or "")
                    and str(stored.get("source_attempt_id") or "")
                    == str(facts.get("source_attempt_id") or "")
                )
                if requested_operation is not None:
                    stored_fingerprint = revival_intent_fingerprint(
                        str(stored.get("operation") or ""),
                        str(stored.get("source_task_id") or ""),
                        str(stored.get("source_attempt_id") or ""),
                    )
                    requested_fingerprint = revival_intent_fingerprint(
                        requested_operation,
                        str(facts.get("source_task_id") or ""),
                        str(facts.get("source_attempt_id") or ""),
                    )
                    same_intent = stored_fingerprint == requested_fingerprint
                if same_intent:
                    replay_revival_id = str(stored.get("revival_id") or "")
                    if REVIVAL_WARNING_REVIVAL_REPLAYED not in warnings:
                        warnings.append(REVIVAL_WARNING_REVIVAL_REPLAYED)
                else:
                    error_codes.append(REVIVAL_RESERVATION_CONFLICT)

    deduped_errors: list[str] = []
    for code in error_codes:
        if code not in deduped_errors:
            deduped_errors.append(code)
    deduped_warnings = warnings[:_MAX_WARNINGS]

    if deduped_errors:
        allowed: tuple[str, ...] = ()
    elif replay_revival_id and requested_operation is not None:
        allowed = (requested_operation,)
    else:
        allowed = REVIVAL_OPERATIONS

    recommended = ""
    if allowed:
        recommended = _clamp_recommendation(
            _taxonomy_recommendation(
                str(facts.get("failure_kind") or ""),
                str(facts.get("failure_layer") or ""),
            ),
            allowed,
        )

    return RevivalPreflight(
        ok=not deduped_errors,
        error_codes=tuple(deduped_errors),
        allowed_next_actions=allowed,
        recommended_action=recommended,
        warnings=tuple(deduped_warnings),
        replay_revival_id=replay_revival_id,
    )


# --------------------------------------------------------------- projection -
def revival_public_view(record: Any) -> dict[str, Any] | None:
    """Return the bounded, redacted public projection of the reservation.

    The v1 record already stores only stable identifiers, digests, enumerated
    states and step ids - never paths, prompts or raw report content - so the
    public view is the validated, field-fixed copy.  Absent or malformed
    records project as ``None`` (backward-compatible absence handling).
    """

    if not isinstance(record, dict):
        return None
    if validate_revival_reservation(record):
        return None
    return {field: copy.deepcopy(record.get(field)) for field in REVIVAL_RECORD_FIELDS}


def revival_status_view(
    preflight: RevivalPreflight,
    reservation: Any = None,
) -> dict[str, Any]:
    """Return the mechanical ``revival`` status/report projection.

    ``allowed_next_actions`` and ``recommended_action`` are mechanical data:
    consumers must present them as-is and must not let the failure taxonomy
    remove a mechanically valid user choice.
    """

    warnings = list(preflight.warnings)
    reservation_view: dict[str, Any] | None = None
    if isinstance(reservation, dict) and not validate_revival_reservation(reservation):
        reservation_view = {
            "revival_id": str(reservation.get("revival_id") or ""),
            "operation": str(reservation.get("operation") or ""),
            "state": str(reservation.get("state") or ""),
            "created_at": str(reservation.get("created_at") or ""),
        }
        if reservation_view["state"] == REVIVAL_STATE_RESERVED:
            for warning in reservation.get("warnings") or ():
                if warning in REVIVAL_WARNING_CODES and warning not in warnings:
                    warnings.append(warning)
    return {
        "version": REVIVAL_PROTOCOL_VERSION,
        "eligible": preflight.ok,
        "allowed_next_actions": list(preflight.allowed_next_actions),
        "recommended_action": preflight.recommended_action,
        "error_codes": list(preflight.error_codes),
        "warnings": warnings[:_MAX_WARNINGS],
        "reservation": reservation_view,
        "replay_revival_id": preflight.replay_revival_id,
    }


__all__ = [
    "REVIVAL_BLOCKING_RESERVATION_STATES",
    "REVIVAL_CLEANUP_SCOPES",
    "REVIVAL_CLEANUP_SCOPE_MANAGED_DEFAULT_ARTIFACTS",
    "REVIVAL_CLEANUP_SCOPE_NONE",
    "REVIVAL_ERROR_CODES",
    "REVIVAL_EXTENSION_KEY",
    "REVIVAL_HANDOFF_CLEANUP_SCOPE",
    "REVIVAL_OPERATIONS",
    "REVIVAL_OPERATION_HANDOFF",
    "REVIVAL_OPERATION_INVALID",
    "REVIVAL_OPERATION_RETRY",
    "REVIVAL_PROTOCOL_VERSION",
    "REVIVAL_RECORD_FIELDS",
    "REVIVAL_RESERVATION_INVALID",
    "REVIVAL_RESERVATION_STATES",
    "REVIVAL_RESERVATION_CONFLICT",
    "REVIVAL_RETRY_CLEANUP_SCOPE",
    "REVIVAL_STABLE_CLEANUP_STATES",
    "REVIVAL_SOURCE_STATUSES",
    "REVIVAL_WARNING_CODES",
    "REVIVAL_WARNING_REVIVAL_REPLAYED",
    "REVIVAL_WARNING_SOURCE_REPORT_ABSENT",
    "REVIVAL_WARNING_SOURCE_REPORT_STEP_MISMATCH",
    "REVIVAL_WARNING_SOURCE_REPORT_UNREADABLE",
    "RevivalPreflight",
    "build_revival_reservation",
    "commit_revival_reservation",
    "evaluate_revival_preflight",
    "open_revival_reservation",
    "release_revival_reservation",
    "revival_digest",
    "revival_from_extensions",
    "revival_intent_fingerprint",
    "revival_path_plan_digest",
    "revival_policy_digest",
    "revival_public_view",
    "revival_replay_or_reserve",
    "revival_status_view",
    "revival_step_bindings",
    "revival_to_extensions",
    "validate_revival_reservation",
]
