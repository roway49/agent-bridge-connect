"""Task/run-scoped auxiliary executor session ledger (SESSION-103-003).

AgentBC's primary ``agentbc.session`` tracks exactly one executor session per
task.  Executors may derive additional official sessions (a child thread, a
``session/new`` prompt, a Claude preallocated project) that AgentBC must also
clean up.  This module owns the durable, versioned ledger of those auxiliary
sessions.

Every auxiliary session is created through a two-phase controlled path:

1. ``reserve_auxiliary_session`` records a pending reservation before
   ``thread/start``, ``session/new``, or Claude preallocation may run;
2. ``bind_auxiliary_receipt`` atomically freezes the exact official executor
   receipt before any child prompt or action may continue.

Entries freeze the owner task/run, parent executor/session, child executor,
official session ID/source, safe purpose label, the primary retain snapshot,
session state, Claude cleanup metadata, and a v1 cleanup receipt.  Idempotent
duplicates return the existing entry; task/run/parent/receipt conflicts fail
closed.  The ledger never scans an Executor private store and never infers a
session ID from recent runs, logs, ``--last``, or ``--continue``.
"""

from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .execution_policy import (
    CLEANUP_STATES,
    CLEANUP_VERIFICATION_SIDES,
    MAX_SESSION_CLEANUP_ATTEMPTS,
    PROJECT_MODES,
    RESOLVED_CLEANUP_STATES,
    SESSION_EXTENSION_KEY,
    SESSION_RECEIPT_SOURCES,
    TERMINAL_SESSION_CLEANUP_STATUSES,
    build_session_cleanup_receipt,
    _empty_cleanup_commands,
    _empty_cleanup_verification,
    normalize_cleanup_commands,
    normalize_cleanup_verification,
    read_session_cleanup_receipt,
    session_cleanup_view,
    upgrade_session_cleanup_receipt,
    validate_execution_session_receipt,
    validate_session_cleanup_receipt,
)
from .protocol import ABCError

AUXILIARY_LEDGER_VERSION = 1
AUXILIARY_EXTENSION_KEY = "agentbc.auxiliary_sessions"
AUXILIARY_SESSION_LIMIT = 16
AUXILIARY_ENTRY_VERSION = 1
CODEX_AUXILIARY_RECEIPT_MISSING_CODE = "codex_auxiliary_receipt_missing"
CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE = "codex_auxiliary_session_unregistered"
CODEX_COLLABORATION_SPAWN_PURPOSE = "collaboration_spawn"

AUXILIARY_SESSION_STATES = frozenset(
    {"reserved", "active", "input_required", "needs_recovery", "terminal"}
)
AUXILIARY_ENTRY_FIELDS = frozenset(
    {
        "version",
        "aux_id",
        "owner_task_id",
        "owner_run_id",
        "parent_executor",
        "parent_session_id",
        "executor",
        "session_id",
        "source",
        "purpose",
        "retain",
        "session_state",
        "project_mode",
        "project_path",
        "cleanup",
        "reserved_at",
        "bound_at",
        "created_at",
        "updated_at",
    }
)
_AUXILIARY_OPTIONAL_ENTRY_FIELDS = frozenset(
    {
        "collaboration_item_id",
        "parent_turn_id",
        "archive_acknowledged",
        "archive_checked_at",
    }
)
_AUX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EXECUTORS = frozenset({"claude", "codex", "hermes"})


def build_auxiliary_ledger() -> dict[str, Any]:
    """Build the empty versioned ledger."""
    return {"version": AUXILIARY_LEDGER_VERSION, "sessions": []}


def redact_session_ref(session_id: str) -> str:
    """Return a stable redacted session reference for public evidence.

    The same official session ID always produces the same redacted ref, but the
    raw ID is never recoverable from the public ref.
    """
    value = str(session_id or "").strip()
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sess_{digest}"


def read_auxiliary_ledger(extensions: Any) -> dict[str, Any]:
    """Read and strictly validate the ledger; fail closed on any defect."""
    value = extensions if isinstance(extensions, dict) else {}
    ledger = value.get(AUXILIARY_EXTENSION_KEY)
    if ledger is None:
        return build_auxiliary_ledger()
    errors = validate_auxiliary_ledger(ledger)
    if errors:
        _raise_ledger_error("auxiliary_ledger_invalid", "; ".join(errors), errors)
    return copy.deepcopy(ledger)


def validate_auxiliary_ledger(value: Any) -> list[str]:
    """Return strict schema errors for the whole ledger."""
    prefix = AUXILIARY_EXTENSION_KEY
    if not isinstance(value, dict):
        return [f"{prefix} must be an object"]
    errors: list[str] = []
    if value.get("version") != AUXILIARY_LEDGER_VERSION:
        errors.append(
            f"{prefix}.version must be {AUXILIARY_LEDGER_VERSION}"
        )
    sessions = value.get("sessions")
    if not isinstance(sessions, list):
        errors.append(f"{prefix}.sessions must be an array")
        return errors
    if len(sessions) > AUXILIARY_SESSION_LIMIT:
        errors.append(
            f"{prefix} exceeds the {AUXILIARY_SESSION_LIMIT}-entry session limit"
        )
    aux_ids: list[str] = []
    for index, entry in enumerate(sessions):
        entry_errors = validate_auxiliary_entry(entry)
        if entry_errors:
            errors.extend(f"{prefix}.sessions[{index}]: {item}" for item in entry_errors)
            continue
        aux_id = str(entry.get("aux_id") or "")
        if aux_id in aux_ids:
            errors.append(f"{prefix} contains a duplicate aux_id: {aux_id}")
        aux_ids.append(aux_id)
    return errors


def validate_auxiliary_entry(value: Any) -> list[str]:
    """Return strict schema errors for one auxiliary session entry."""
    prefix = f"{AUXILIARY_EXTENSION_KEY} entry"
    if not isinstance(value, dict):
        return [f"{prefix} must be an object"]
    fields = set(value)
    missing = sorted(AUXILIARY_ENTRY_FIELDS - fields)
    unknown = sorted(fields - AUXILIARY_ENTRY_FIELDS - _AUXILIARY_OPTIONAL_ENTRY_FIELDS)
    errors: list[str] = []
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    if unknown:
        errors.append(f"contains unsupported fields: {', '.join(unknown)}")
    if missing or unknown:
        return errors
    for field in _AUXILIARY_OPTIONAL_ENTRY_FIELDS:
        if field == "archive_acknowledged":
            if field in value and type(value[field]) is not bool:
                errors.append(f"{field} must be a boolean")
            continue
        if field in value and not isinstance(value[field], str):
            errors.append(f"{field} must be a string")
    if value.get("archive_acknowledged") is True and not str(
        value.get("archive_checked_at") or ""
    ).strip():
        errors.append("archive_checked_at is required after archive acknowledgement")
    if value.get("version") != AUXILIARY_ENTRY_VERSION:
        errors.append(f"version must be {AUXILIARY_ENTRY_VERSION}")
    aux_id = value.get("aux_id")
    if not isinstance(aux_id, str) or not _AUX_ID_RE.fullmatch(aux_id):
        errors.append("aux_id must be a 32-character hex identifier")
    for field in ("owner_task_id", "owner_run_id", "parent_session_id", "executor", "purpose"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field} must be non-empty")
    parent_executor = str(value.get("parent_executor") or "").strip().lower()
    if parent_executor not in _EXECUTORS:
        errors.append("parent_executor is unsupported")
    child_executor = str(value.get("executor") or "").strip().lower()
    if child_executor not in _EXECUTORS:
        errors.append("executor is unsupported")
    purpose = value.get("purpose")
    if not isinstance(purpose, str) or not _PURPOSE_RE.fullmatch(purpose):
        errors.append("purpose must be a stable lowercase label")
    if type(value.get("retain")) is not bool:
        errors.append("retain must be a boolean")
    session_state = str(value.get("session_state") or "").strip().lower()
    if session_state not in AUXILIARY_SESSION_STATES:
        errors.append("session_state is invalid")
    session_id = value.get("session_id")
    if not isinstance(session_id, str):
        errors.append("session_id must be a string")
    elif session_state != "reserved" and not session_id.strip():
        errors.append("session_id is required after the reserved state")
    source = value.get("source")
    if not isinstance(source, str):
        errors.append("source must be a string")
    elif session_state != "reserved" and not source.strip():
        errors.append("source is required after the reserved state")
    project_mode = str(value.get("project_mode") or "").strip().lower()
    if project_mode not in PROJECT_MODES:
        errors.append("project_mode is invalid")
    project_path = value.get("project_path")
    if not isinstance(project_path, str):
        errors.append("project_path must be a string")
    cleanup_errors = validate_session_cleanup_receipt(value.get("cleanup"), allow_legacy=True)
    if cleanup_errors:
        errors.extend(cleanup_errors)
    for field in ("reserved_at", "bound_at", "created_at", "updated_at"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str):
            errors.append(f"{field} must be a string")
        elif timestamp and not _valid_utc_timestamp(timestamp):
            errors.append(f"{field} must be an ISO-8601 timestamp with timezone")
    if errors:
        return errors
    if session_state == "reserved":
        if session_id or source or value["bound_at"]:
            errors.append("reserved entry must not contain a bound session")
        if value.get("cleanup")["state"] != "not_requested":
            errors.append("reserved entry must not have requested cleanup")
    return errors


def reserve_auxiliary_session(
    extensions: Any,
    *,
    owner_task_id: str,
    owner_run_id: str,
    parent_executor: str,
    parent_session_id: str,
    executor: str,
    purpose: str,
    retain: bool,
    project_mode: str = "none",
    project_path: str = "",
    collaboration_item_id: str = "",
    parent_turn_id: str = "",
    created_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Phase one: durably reserve an auxiliary session before creation.

    Returns ``(extensions, entry)``.  An idempotent duplicate reservation
    returns the existing entry unchanged; a conflict on frozen fields fails
    closed.
    """
    extensions = dict(extensions or {})
    ledger = read_auxiliary_ledger(extensions)
    now = created_at or _utc_now()
    if not _valid_utc_timestamp(now):
        raise ABCError("auxiliary_ledger_invalid", "created_at must be an ISO-8601 timestamp")

    owner_task_id = str(owner_task_id or "").strip()
    owner_run_id = str(owner_run_id or "").strip()
    parent_session_id = str(parent_session_id or "").strip()
    child_executor = str(executor or "").strip().lower()
    purpose_label = str(purpose or "").strip().lower()
    parent_executor = str(parent_executor or "").strip().lower()
    collaboration_item_id = str(collaboration_item_id or "").strip()
    parent_turn_id = str(parent_turn_id or "").strip()
    if not owner_task_id or not owner_run_id:
        raise ABCError(
            "auxiliary_reservation_invalid",
            "owner_task_id and owner_run_id are required for an auxiliary reservation",
        )
    if child_executor not in _EXECUTORS:
        raise ABCError(
            "auxiliary_reservation_invalid",
            f"unsupported auxiliary executor: {child_executor}",
        )
    if parent_executor not in _EXECUTORS:
        raise ABCError(
            "auxiliary_reservation_invalid",
            f"unsupported parent executor: {parent_executor}",
        )
    if not parent_session_id:
        raise ABCError(
            "auxiliary_reservation_invalid",
            "parent_session_id is required for an auxiliary reservation",
        )
    if not _PURPOSE_RE.fullmatch(purpose_label):
        raise ABCError(
            "auxiliary_reservation_invalid",
            "purpose must be a stable lowercase label",
        )
    primary = extensions.get(SESSION_EXTENSION_KEY)
    if (
        isinstance(primary, dict)
        and type(primary.get("retain")) is bool
        and retain is not primary.get("retain")
    ):
        raise ABCError(
            "auxiliary_reservation_conflict",
            "auxiliary retain must be copied from the primary session snapshot",
            {"primary_retain": primary.get("retain"), "requested_retain": retain},
        )
    entry = _match_existing_reservation(
        ledger,
        owner_run_id=owner_run_id,
        parent_session_id=parent_session_id,
        executor=child_executor,
        purpose=purpose_label,
        collaboration_item_id=collaboration_item_id,
    )
    if entry is not None:
        _assert_frozen_match(
            entry,
            owner_task_id=owner_task_id,
            parent_executor=parent_executor,
            retain=retain,
            project_mode=project_mode,
            project_path=project_path,
            collaboration_item_id=collaboration_item_id,
            parent_turn_id=parent_turn_id,
        )
        return extensions, copy.deepcopy(entry)
    if len(ledger["sessions"]) >= AUXILIARY_SESSION_LIMIT:
        raise ABCError(
            "auxiliary_ledger_full",
            f"auxiliary session ledger exceeds {AUXILIARY_SESSION_LIMIT} entries",
        )
    entry = {
        "version": AUXILIARY_ENTRY_VERSION,
        "aux_id": uuid.uuid4().hex,
        "owner_task_id": owner_task_id,
        "owner_run_id": owner_run_id,
        "parent_executor": parent_executor,
        "parent_session_id": parent_session_id,
        "executor": child_executor,
        "session_id": "",
        "source": "",
        "purpose": purpose_label,
        "retain": bool(retain),
        "session_state": "reserved",
        "project_mode": str(project_mode or "").strip().lower(),
        "project_path": str(project_path or "").strip(),
        "cleanup": build_session_cleanup_receipt(),
        "reserved_at": now,
        "bound_at": "",
        "created_at": now,
        "updated_at": now,
    }
    if collaboration_item_id:
        entry["collaboration_item_id"] = collaboration_item_id
    if parent_turn_id:
        entry["parent_turn_id"] = parent_turn_id
    entry_errors = validate_auxiliary_entry(entry)
    if entry_errors:
        _raise_ledger_error("auxiliary_ledger_invalid", "; ".join(entry_errors), entry_errors)
    ledger["sessions"].append(copy.deepcopy(entry))
    extensions[AUXILIARY_EXTENSION_KEY] = ledger
    return extensions, copy.deepcopy(entry)


def bind_auxiliary_receipt(
    extensions: Any,
    *,
    aux_id: str,
    receipt: Any,
    expected_session_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Phase two: atomically freeze the official executor receipt.

    Returns ``(extensions, entry)``.  An idempotent re-bind of the same exact
    session returns the existing bound entry; a different session, task, run,
    or parent fails closed.
    """
    extensions = dict(extensions or {})
    ledger = read_auxiliary_ledger(extensions)
    aux_id = str(aux_id or "").strip()
    entry = next(
        (item for item in ledger["sessions"] if str(item.get("aux_id") or "") == aux_id),
        None,
    )
    if entry is None:
        raise ABCError(
            "auxiliary_receipt_missing",
            "Cannot bind an official receipt to an unknown auxiliary reservation.",
            {"aux_id": aux_id},
        )
    entry = copy.deepcopy(entry)
    executor = str(entry.get("executor") or "").strip().lower()
    errors = validate_execution_session_receipt(receipt, executor=executor)
    if errors:
        raise ABCError(
            "auxiliary_receipt_invalid",
            "; ".join(errors),
            {"errors": errors},
        )
    official_session_id = str(receipt.get("session_id") or "").strip()
    expected = str(expected_session_id or "").strip()
    if expected and official_session_id != expected:
        raise ABCError(
            "auxiliary_receipt_session_mismatch",
            "Official auxiliary receipt does not match the explicit session ID.",
            {
                "expected_session_id": expected,
                "actual_session_id": official_session_id,
            },
        )
    existing_id = str(entry.get("session_id") or "").strip()
    if existing_id:
        if existing_id == official_session_id:
            official_source = str(receipt.get("source") or "").strip()
            if official_source and official_source != str(entry.get("source") or "").strip():
                raise ABCError(
                    "auxiliary_receipt_conflict",
                    "Rebinding the same session with a different official source is a conflict.",
                    {
                        "existing_source": entry.get("source"),
                        "actual_source": official_source,
                    },
                )
            return extensions, copy.deepcopy(entry)
        raise ABCError(
            "auxiliary_receipt_conflict",
            "A different official session is already bound to this auxiliary reservation.",
            {"existing_session_id": existing_id, "actual_session_id": official_session_id},
        )
    if entry.get("session_state") != "reserved":
        raise ABCError(
            "auxiliary_receipt_conflict",
            "Auxiliary reservation is no longer in the reserved state.",
            {"session_state": entry.get("session_state")},
        )
    now = _utc_now()
    entry["session_id"] = official_session_id
    entry["source"] = str(receipt.get("source") or "").strip()
    entry["session_state"] = "active"
    entry["bound_at"] = now
    entry["updated_at"] = now
    entry_errors = validate_auxiliary_entry(entry)
    if entry_errors:
        _raise_ledger_error("auxiliary_ledger_invalid", "; ".join(entry_errors), entry_errors)
    for index, item in enumerate(ledger["sessions"]):
        if str(item.get("aux_id") or "") == aux_id:
            ledger["sessions"][index] = copy.deepcopy(entry)
            break
    extensions[AUXILIARY_EXTENSION_KEY] = ledger
    return extensions, copy.deepcopy(entry)


def mark_auxiliary_terminal(
    extensions: Any,
    *,
    aux_id: str,
    occurred_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark a bound auxiliary session terminal; reserved entries fail closed."""
    extensions = dict(extensions or {})
    ledger = read_auxiliary_ledger(extensions)
    aux_id = str(aux_id or "").strip()
    entry = next(
        (item for item in ledger["sessions"] if str(item.get("aux_id") or "") == aux_id),
        None,
    )
    if entry is None:
        raise ABCError(
            "auxiliary_receipt_missing",
            "Cannot finalize an unknown auxiliary reservation.",
            {"aux_id": aux_id},
        )
    if not str(entry.get("session_id") or "").strip():
        raise ABCError(
            "auxiliary_session_not_bound",
            "An auxiliary reservation must bind an official receipt before terminal.",
            {"aux_id": aux_id},
        )
    entry = copy.deepcopy(entry)
    if entry.get("session_state") == "terminal":
        return extensions, entry
    if entry.get("session_state") not in {"active", "input_required", "needs_recovery"}:
        raise ABCError(
            "auxiliary_session_conflict",
            "Only a bound auxiliary session may be marked terminal.",
            {"session_state": entry.get("session_state")},
        )
    entry["session_state"] = "terminal"
    entry["updated_at"] = occurred_at or _utc_now()
    for index, item in enumerate(ledger["sessions"]):
        if str(item.get("aux_id") or "") == aux_id:
            ledger["sessions"][index] = copy.deepcopy(entry)
            break
    extensions[AUXILIARY_EXTENSION_KEY] = ledger
    return extensions, copy.deepcopy(entry)


def reserve_codex_collaboration_session(
    extensions: Any,
    *,
    owner_task_id: str,
    owner_run_id: str,
    parent_session_id: str,
    item_id: str,
    parent_turn_id: str = "",
    created_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reserve one Codex collaboration child before any child thread exists."""
    item_id = str(item_id or "").strip()
    if not item_id:
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration item did not contain an official item ID.",
        )
    primary = (extensions or {}).get(SESSION_EXTENSION_KEY) if isinstance(extensions, dict) else None
    if not isinstance(primary, dict) or str(primary.get("executor") or "").strip().lower() != "codex":
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration child has no authoritative parent session.",
        )
    primary_session_id = str(primary.get("session_id") or "").strip()
    if not primary_session_id or primary_session_id != str(parent_session_id or "").strip():
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration child parent session does not match the task receipt.",
        )
    if primary.get("official_receipt_bound") is not True:
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration child has no official parent receipt binding.",
        )
    if str(primary.get("receipt_source") or "").strip() != SESSION_RECEIPT_SOURCES["codex"]:
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration child parent receipt is not an official thread receipt.",
        )
    run_ids = primary.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or str(owner_run_id or "").strip() not in run_ids
    ):
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration child is not bound to the current parent run.",
        )
    if str(primary.get("session_state") or "").strip().lower() not in {
        "active",
        "input_required",
        "needs_recovery",
    }:
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration child parent session is not active.",
        )
    return reserve_auxiliary_session(
        extensions,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        parent_executor="codex",
        parent_session_id=primary_session_id,
        executor="codex",
        purpose=CODEX_COLLABORATION_SPAWN_PURPOSE,
        retain=bool(primary.get("retain")),
        collaboration_item_id=item_id,
        parent_turn_id=str(parent_turn_id or "").strip(),
        created_at=created_at,
    )


def _codex_collaboration_entry(
    extensions: Any,
    *,
    owner_task_id: str,
    owner_run_id: str,
    parent_session_id: str,
    item_id: str,
) -> dict[str, Any]:
    ledger = read_auxiliary_ledger(extensions)
    for entry in ledger["sessions"]:
        if (
            str(entry.get("owner_task_id") or "") == str(owner_task_id or "")
            and str(entry.get("owner_run_id") or "") == str(owner_run_id or "")
            and str(entry.get("parent_session_id") or "") == str(parent_session_id or "")
            and str(entry.get("executor") or "").strip().lower() == "codex"
            and str(entry.get("purpose") or "") == CODEX_COLLABORATION_SPAWN_PURPOSE
            and str(entry.get("collaboration_item_id") or "") == str(item_id or "")
        ):
            return copy.deepcopy(entry)
    raise ABCError(
        CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
        "Codex collaboration completion has no matching reservation.",
    )


def bind_codex_collaboration_receipt(
    extensions: Any,
    *,
    aux_id: str,
    receiver_thread_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind only the official receiverThreadId to a reserved child."""
    receiver_thread_id = str(receiver_thread_id or "").strip()
    if not receiver_thread_id:
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration completion did not contain receiverThreadId.",
        )
    ledger = read_auxiliary_ledger(extensions)
    entry = next(
        (item for item in ledger["sessions"] if str(item.get("aux_id") or "") == str(aux_id or "")),
        None,
    )
    if entry is None or (
        str(entry.get("executor") or "").strip().lower() != "codex"
        or str(entry.get("purpose") or "") != CODEX_COLLABORATION_SPAWN_PURPOSE
    ):
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Cannot bind an official receiver to an unknown Codex reservation.",
        )
    existing_id = str(entry.get("session_id") or "").strip()
    if existing_id and existing_id != receiver_thread_id:
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration receiverThreadId conflicts with the bound receipt.",
        )
    receipt = {
        "version": 1,
        "executor": "codex",
        "session_id": receiver_thread_id,
        "resumed": False,
        "persistence": "persistent",
        "source": SESSION_RECEIPT_SOURCES["codex"],
    }
    try:
        return bind_auxiliary_receipt(
            extensions,
            aux_id=aux_id,
            receipt=receipt,
            expected_session_id=receiver_thread_id,
        )
    except ABCError as exc:
        if exc.code in {
            "auxiliary_receipt_session_mismatch",
            "auxiliary_receipt_conflict",
            "auxiliary_receipt_invalid",
        }:
            raise ABCError(
                CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
                "Codex collaboration receiverThreadId could not be bound.",
            ) from exc
        raise


def mark_codex_collaboration_terminal(
    extensions: Any,
    *,
    aux_id: str,
    occurred_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write the child terminal state only after its official receipt is bound."""
    try:
        return mark_auxiliary_terminal(
            extensions,
            aux_id=aux_id,
            occurred_at=occurred_at,
        )
    except ABCError as exc:
        if exc.code in {"auxiliary_receipt_missing", "auxiliary_session_not_bound"}:
            raise ABCError(
                CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
                "Codex collaboration child is not bound to an official receipt.",
            ) from exc
        raise


def _collaboration_item_id(item: Any) -> str:
    return str(item.get("id") or "").strip() if isinstance(item, dict) else ""


def _collaboration_receiver_thread_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    singular = str(item.get("receiverThreadId") or "").strip()
    plural = item.get("receiverThreadIds")
    if singular:
        return singular if not plural or plural == [singular] else ""
    if isinstance(plural, list) and len(plural) == 1 and isinstance(plural[0], str):
        return plural[0].strip()
    return ""


def handle_codex_collaboration_item_started(
    extensions: Any,
    *,
    owner_task_id: str,
    owner_run_id: str,
    parent_session_id: str,
    parent_turn_id: str = "",
    item: Any,
    occurred_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Handle item/started for spawnAgent without trusting model text."""
    if not isinstance(item, dict) or str(item.get("type") or "") != "collabAgentToolCall":
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration item type is not an official collabAgentToolCall.",
        )
    if str(item.get("tool") or "") != "spawnAgent":
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration item is not an official spawnAgent call.",
        )
    item_id = _collaboration_item_id(item)
    return reserve_codex_collaboration_session(
        extensions,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        parent_session_id=parent_session_id,
        item_id=item_id,
        parent_turn_id=parent_turn_id,
        created_at=occurred_at,
    )


def handle_codex_collaboration_item_completed(
    extensions: Any,
    *,
    owner_task_id: str,
    owner_run_id: str,
    parent_session_id: str,
    item: Any,
    occurred_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind receiverThreadId and then write the child terminal ledger state."""
    if not isinstance(item, dict) or str(item.get("type") or "") != "collabAgentToolCall":
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration item type is not an official collabAgentToolCall.",
        )
    if str(item.get("tool") or "") != "spawnAgent":
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Codex collaboration item is not an official spawnAgent call.",
        )
    item_id = _collaboration_item_id(item)
    if not item_id:
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration completion did not contain an official item ID.",
        )
    receiver_thread_id = _collaboration_receiver_thread_id(item)
    if not receiver_thread_id:
        raise ABCError(
            CODEX_AUXILIARY_RECEIPT_MISSING_CODE,
            "Codex collaboration completion did not contain one official receiverThreadId.",
        )
    entry = _codex_collaboration_entry(
        extensions,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        parent_session_id=parent_session_id,
        item_id=item_id,
    )
    updated, bound = bind_codex_collaboration_receipt(
        extensions,
        aux_id=str(entry["aux_id"]),
        receiver_thread_id=receiver_thread_id,
    )
    return mark_codex_collaboration_terminal(
        updated,
        aux_id=str(bound["aux_id"]),
        occurred_at=occurred_at,
    )


def reconcile_codex_descendants(
    extensions: Any,
    *,
    owner_task_id: str,
    owner_run_id: str,
    parent_session_id: str,
    threads: Any,
) -> list[str]:
    """Reconcile only official ancestorThreadId descendants; never delete here."""
    registered = {
        str(entry.get("session_id") or "").strip()
        for entry in read_auxiliary_ledger(extensions)["sessions"]
        if str(entry.get("owner_task_id") or "") == str(owner_task_id or "")
        and str(entry.get("owner_run_id") or "") == str(owner_run_id or "")
        and str(entry.get("parent_session_id") or "") == str(parent_session_id or "")
        and str(entry.get("executor") or "").strip().lower() == "codex"
        and str(entry.get("purpose") or "") == CODEX_COLLABORATION_SPAWN_PURPOSE
        and str(entry.get("session_id") or "").strip()
    }
    descendants: set[str] = set()
    if isinstance(threads, list):
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            if str(thread.get("ancestorThreadId") or "").strip() != str(parent_session_id or "").strip():
                continue
            thread_id = str(thread.get("id") or thread.get("threadId") or "").strip()
            if thread_id:
                descendants.add(thread_id)
    unknown = descendants - registered
    if unknown:
        raise ABCError(
            CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE,
            "Official Codex descendant is not registered in the current task/run ledger.",
            {"unknown_count": len(unknown)},
        )
    return sorted(descendants)


def auxiliary_cleanup_blockers(
    entry: Any,
    *,
    task_status: str,
    lease_state: str,
) -> list[str]:
    """Return the ordered reasons one auxiliary session must not be cleaned.

    FLOW-104-002 removed ``report_written`` and ``notification_recorded`` as
    auxiliary cleanup gates: those are independent ``agentbc.terminal_delivery``
    stages and must never block an auxiliary session cleanup.
    """
    blockers: list[str] = []
    if str(task_status or "").strip().lower() not in TERMINAL_SESSION_CLEANUP_STATUSES:
        blockers.append("task_not_terminal")
    if str(lease_state or "").strip().lower() != "closed":
        blockers.append("run_lease_not_closed")
    entry_errors = validate_auxiliary_entry(entry)
    if entry_errors:
        blockers.append("auxiliary_ledger_invalid")
        return blockers
    if (
        str(entry.get("executor") or "").strip().lower() == "codex"
        and str(entry.get("source") or "") != SESSION_RECEIPT_SOURCES["codex"]
    ):
        blockers.append("auxiliary_session_receipt_unbound")
    if entry.get("retain") is True:
        blockers.append("retention_enabled")
    if str(entry.get("session_state") or "") != "terminal":
        blockers.append("auxiliary_session_not_terminal")
    if not str(entry.get("session_id") or "").strip():
        blockers.append("auxiliary_session_pending_reservation")
    cleanup = read_session_cleanup_receipt(entry.get("cleanup"))
    if cleanup["state"] in RESOLVED_CLEANUP_STATES:
        blockers.append("cleanup_already_resolved")
    return blockers


def transition_auxiliary_cleanup(
    entry: Any,
    target_state: str,
    *,
    task_status: str,
    lease_state: str,
    capability: str | None = None,
    strategy: str | None = None,
    error_code: str = "",
    retryable: bool = False,
    next_attempt_at: str = "",
    verification: Any | None = None,
    commands: Any | None = None,
    occurred_at: str | None = None,
    authoritative_archive_ack: bool = False,
) -> dict[str, Any]:
    """Apply one pure, fail-closed auxiliary cleanup receipt transition.

    The returned receipt is detached from ``entry``.  No Executor, filesystem,
    task record, project, or dispatcher conversation is touched.
    """
    if type(target_state) is not str or target_state not in CLEANUP_STATES:
        _raise_cleanup_transition(f"invalid target state: {target_state}")
    entry_errors = validate_auxiliary_entry(entry)
    if entry_errors:
        _raise_cleanup_transition("auxiliary session entry is invalid", entry_errors)

    receipt = read_session_cleanup_receipt(copy.deepcopy(entry["cleanup"]))
    current_state = receipt["state"]
    if current_state in RESOLVED_CLEANUP_STATES or current_state == target_state:
        return receipt
    receipt = upgrade_session_cleanup_receipt(receipt)

    now = occurred_at or _utc_now()
    if not _valid_utc_timestamp(now):
        _raise_cleanup_transition("occurred_at must be an ISO-8601 timestamp with timezone")

    blockers = auxiliary_cleanup_blockers(
        entry,
        task_status=task_status,
        lease_state=lease_state,
    )
    if target_state == "retained":
        if current_state != "not_requested" or entry.get("retain") is not True:
            _raise_cleanup_transition(f"illegal transition: {current_state} -> retained")
        blockers = [item for item in blockers if item != "retention_enabled"]
        if blockers:
            _raise_cleanup_transition("retained transition is blocked", blockers)
        updated = dict(receipt)
        updated.update(
            {
                "capability": "not_applicable",
                "strategy": "retain",
                "state": "retained",
                "completed_at": now,
                "error_code": "",
                "retryable": False,
                "next_attempt_at": "",
                "verification": _empty_cleanup_verification(),
            }
        )
        return _validated_cleanup_transition(updated)

    if target_state == "pending":
        if current_state not in {"not_requested", "failed"}:
            _raise_cleanup_transition(f"illegal transition: {current_state} -> pending")
        if blockers:
            _raise_cleanup_transition("cleanup request is blocked", blockers)
        if current_state == "failed":
            if authoritative_archive_ack:
                if str(entry.get("executor") or "").strip().lower() != "codex":
                    _raise_cleanup_transition(
                        "authoritative archive acknowledgement requires Codex"
                    )
            else:
                if receipt["retryable"] is not True:
                    _raise_cleanup_transition("failed cleanup is not retryable")
                if receipt["attempts"] >= MAX_SESSION_CLEANUP_ATTEMPTS:
                    _raise_cleanup_transition("cleanup attempt limit reached")
                due_at = receipt["next_attempt_at"]
                if not due_at or _parse_utc_timestamp(now) < _parse_utc_timestamp(due_at):
                    _raise_cleanup_transition("cleanup retry backoff has not elapsed")
        updated = dict(receipt)
        updated.update(
            {
                "capability": capability or receipt["capability"],
                "strategy": strategy or receipt["strategy"],
                "state": "pending",
                "attempts": receipt["attempts"] + 1,
                "requested_at": receipt["requested_at"] or now,
                "last_attempt_at": now,
                "next_attempt_at": "",
                "completed_at": "",
                "error_code": "",
                "retryable": False,
                "verification": _empty_cleanup_verification(),
                "commands": _empty_cleanup_commands(),
            }
        )
        return _validated_cleanup_transition(updated)

    if current_state != "pending" or target_state not in {
        "succeeded",
        "unsupported",
        "failed",
    }:
        _raise_cleanup_transition(f"illegal transition: {current_state} -> {target_state}")
    if blockers:
        _raise_cleanup_transition("cleanup result is blocked", blockers)

    updated = dict(receipt)
    updated["last_attempt_at"] = now
    # SESSION-104-001 v5 gate: the current Desktop archive acknowledgement and
    # the unchanged delete acknowledgement are required for auxiliary Codex
    # sessions; legacy App Server archive evidence is diagnostic only.
    executor_is_codex = str(entry.get("executor") or "").strip().lower() == "codex"
    is_archive_strategy = (
        (strategy or receipt["strategy"]) == "official_session_archive_then_delete"
    )
    normalized_commands = (
        normalize_cleanup_commands(commands) if commands is not None else receipt.get("commands")
    )
    if target_state == "succeeded":
        normalized_verification = (
            normalize_cleanup_verification(verification)
            if verification is not None
            else (
                _empty_cleanup_verification("not_applicable")
                if not executor_is_codex
                else receipt["verification"]
            )
        )
        if not executor_is_codex:
            # Non-Codex executors have no official archive/delete commands.
            normalized_commands = _empty_cleanup_commands("not_applicable", checked_at=now)
        elif not normalized_commands or all(
            (normalized_commands or {}).get(command, {}).get("status") == "not_requested"
            for command in ("desktop_archive", "app_server_archive", "delete")
        ):
            # A Codex adapter supplied no command evidence: fail closed to
            # not_applicable rather than leaving invalid not_requested proof.
            normalized_commands = _empty_cleanup_commands("not_applicable", checked_at=now)
            if is_archive_strategy:
                _raise_cleanup_transition(
                    "Codex auxiliary cleanup succeeded without Desktop archive and "
                    "delete command acknowledgements"
                )
        if executor_is_codex and is_archive_strategy:
            desktop_archive_status = str(
                (normalized_commands or {}).get("desktop_archive", {}).get("status") or ""
            )
            delete_status = str(
                (normalized_commands or {}).get("delete", {}).get("status") or ""
            )
            if desktop_archive_status not in {"acknowledged", "confirmed"} or delete_status not in {
                "acknowledged",
                "confirmed",
            }:
                _raise_cleanup_transition(
                    "Codex auxiliary cleanup succeeded without Desktop archive and "
                    "delete command acknowledgements"
                )
            normalized_verification = dict(normalized_verification)
            normalized_verification["desktop_live"] = {
                "status": "not_applicable",
                "checked_at": now,
            }
        elif (
            executor_is_codex
            and {
                normalized_verification[side]["status"]
                for side in CLEANUP_VERIFICATION_SIDES
            }
            != {"absent"}
        ):
            _raise_cleanup_transition(
                "Codex cleanup succeeded without all CLI, Desktop backend, and Desktop live absence verification"
            )
        updated.update(
            {
                "capability": capability or receipt["capability"],
                "strategy": strategy or receipt["strategy"],
                "state": "succeeded",
                "completed_at": now,
                "error_code": "",
                "retryable": False,
                "next_attempt_at": "",
                "verification": normalized_verification,
                "commands": normalized_commands,
            }
        )
    elif target_state == "unsupported":
        updated.update(
            {
                "capability": capability or "unsupported",
                "strategy": strategy or "none",
                "state": "unsupported",
                "completed_at": now,
                "error_code": error_code or "session_cleanup_unsupported",
                "retryable": False,
                "next_attempt_at": "",
                "verification": normalize_cleanup_verification(verification)
                if verification is not None
                else receipt["verification"],
                "commands": normalized_commands,
            }
        )
    else:
        updated.update(
            {
                "capability": capability or receipt["capability"],
                "strategy": strategy or receipt["strategy"],
                "state": "failed",
                "completed_at": "",
                "error_code": error_code,
                "retryable": retryable,
                "next_attempt_at": next_attempt_at,
                "verification": normalize_cleanup_verification(verification)
                if verification is not None
                else receipt["verification"],
                "commands": normalized_commands,
            }
        )
    return _validated_cleanup_transition(updated)


def auxiliary_cleanup_strategy(entry: Any) -> str:
    """Return the official Codex archive-then-delete strategy for one entry.

    Codex cleanup is always the acknowledged archive gate followed by delete;
    Claude keeps its project purge; everything else uses the exact-session
    delete strategy.  Primary-first and deepest/newest auxiliary ordering are
    preserved by the coordinator.
    """
    if entry.get("retain") is True:
        return "retain"
    executor = str(entry.get("executor") or "").strip().lower()
    project_mode = str(entry.get("project_mode") or "").strip().lower()
    if executor == "claude" and project_mode == "ephemeral":
        return "claude_project_purge"
    if executor == "codex":
        return "official_session_archive_then_delete"
    return "official_session_delete"


def _validated_ledger_or_default(ledger: Any) -> dict[str, Any]:
    """Return a validated ledger dict or the empty default when invalid."""
    if not isinstance(ledger, dict) or validate_auxiliary_ledger(ledger):
        return build_auxiliary_ledger()
    return ledger


def auxiliary_ledger_view(ledger: Any) -> list[dict[str, Any]]:
    """Return the safe public projection with redacted refs and no paths."""
    validated = _validated_ledger_or_default(ledger)
    entries: list[dict[str, Any]] = []
    for entry in validated.get("sessions") or []:
        try:
            cleanup = session_cleanup_view(entry.get("cleanup"))
        except ABCError:
            cleanup = session_cleanup_view(build_session_cleanup_receipt())
        entries.append(
            {
                "aux_id": str(entry.get("aux_id") or ""),
                "ref": redact_session_ref(str(entry.get("session_id") or "")),
                "executor": str(entry.get("executor") or "").strip().lower(),
                "purpose": str(entry.get("purpose") or ""),
                "retain": bool(entry.get("retain")),
                "session_state": str(entry.get("session_state") or "").strip().lower(),
                "cleanup": cleanup,
            }
        )
    return entries


def auxiliary_aggregate_view(ledger: Any) -> dict[str, Any]:
    """Return a stable aggregate health summary for one auxiliary ledger."""
    if not isinstance(ledger, dict) or validate_auxiliary_ledger(ledger):
        return {"state": "blocked", "total": 0, "resolved": 0, "unresolved": 0}
    entries = ledger.get("sessions") or []
    total = len(entries)
    unresolved = 0
    for entry in entries:
        try:
            blockers = auxiliary_cleanup_blockers(
                entry,
                task_status="completed",
                lease_state="closed",
            )
            cleanup = read_session_cleanup_receipt(entry.get("cleanup"))
        except ABCError:
            unresolved += 1
            continue
        if entry.get("retain") is True:
            continue
        if str(entry.get("session_id") or "").strip() == "":
            unresolved += 1
            continue
        if cleanup["state"] in RESOLVED_CLEANUP_STATES:
            continue
        if blockers and "auxiliary_session_not_terminal" in blockers:
            # Still in use; not an acceptance failure while the task is active.
            continue
        unresolved += 1
    return {
        "state": "resolved" if unresolved == 0 else "blocked",
        "total": total,
        "resolved": total - unresolved,
        "unresolved": unresolved,
    }


def _match_existing_reservation(
    ledger: dict[str, Any],
    *,
    owner_run_id: str,
    parent_session_id: str,
    executor: str,
    purpose: str,
    collaboration_item_id: str = "",
) -> dict[str, Any] | None:
    for entry in ledger.get("sessions") or []:
        if (
            str(entry.get("owner_run_id") or "") == owner_run_id
            and str(entry.get("parent_session_id") or "") == parent_session_id
            and str(entry.get("executor") or "").strip().lower() == executor
            and str(entry.get("purpose") or "") == purpose
            and (
                str(entry.get("collaboration_item_id") or "")
                == str(collaboration_item_id or "")
            )
        ):
            return entry
    return None


def _assert_frozen_match(
    entry: dict[str, Any],
    *,
    owner_task_id: str,
    parent_executor: str,
    retain: bool,
    project_mode: str,
    project_path: str,
    collaboration_item_id: str = "",
    parent_turn_id: str = "",
) -> None:
    conflicts: list[str] = []
    if str(entry.get("owner_task_id") or "").strip() != str(owner_task_id or "").strip():
        conflicts.append("owner_task_id")
    if str(entry.get("parent_executor") or "").strip().lower() != str(parent_executor or "").strip().lower():
        conflicts.append("parent_executor")
    if entry.get("retain") is not retain:
        conflicts.append("retain")
    if str(entry.get("project_mode") or "").strip().lower() != str(project_mode or "").strip().lower():
        conflicts.append("project_mode")
    if str(entry.get("project_path") or "").strip() != str(project_path or "").strip():
        conflicts.append("project_path")
    if str(entry.get("collaboration_item_id") or "") != str(collaboration_item_id or ""):
        conflicts.append("collaboration_item_id")
    if str(entry.get("parent_turn_id") or "") != str(parent_turn_id or ""):
        conflicts.append("parent_turn_id")
    if conflicts:
        raise ABCError(
            "auxiliary_reservation_conflict",
            f"duplicate auxiliary reservation has conflicting frozen fields: {', '.join(conflicts)}",
            {"conflicts": conflicts},
        )


def _validated_cleanup_transition(value: dict[str, Any]) -> dict[str, Any]:
    errors = validate_session_cleanup_receipt(value)
    if errors:
        _raise_cleanup_transition("transition produced an invalid receipt", errors)
    return value


def _raise_cleanup_transition(
    message: str,
    blockers: list[str] | None = None,
) -> None:
    details = {"blockers": blockers} if blockers else None
    raise ABCError("invalid_auxiliary_session_cleanup_transition", message, details)


def _raise_ledger_error(code: str, message: str, errors: list[str]) -> None:
    raise ABCError(code, message, {"errors": errors})


def _parse_utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _valid_utc_timestamp(value: str) -> bool:
    try:
        parsed = _parse_utc_timestamp(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "AUXILIARY_ENTRY_VERSION",
    "AUXILIARY_EXTENSION_KEY",
    "AUXILIARY_LEDGER_VERSION",
    "AUXILIARY_SESSION_LIMIT",
    "AUXILIARY_SESSION_STATES",
    "CODEX_AUXILIARY_RECEIPT_MISSING_CODE",
    "CODEX_AUXILIARY_SESSION_UNREGISTERED_CODE",
    "CODEX_COLLABORATION_SPAWN_PURPOSE",
    "auxiliary_aggregate_view",
    "auxiliary_cleanup_blockers",
    "auxiliary_cleanup_strategy",
    "auxiliary_ledger_view",
    "bind_auxiliary_receipt",
    "bind_codex_collaboration_receipt",
    "build_auxiliary_ledger",
    "mark_auxiliary_terminal",
    "mark_codex_collaboration_terminal",
    "read_auxiliary_ledger",
    "redact_session_ref",
    "reserve_auxiliary_session",
    "reserve_codex_collaboration_session",
    "handle_codex_collaboration_item_completed",
    "handle_codex_collaboration_item_started",
    "reconcile_codex_descendants",
    "transition_auxiliary_cleanup",
    "validate_auxiliary_entry",
    "validate_auxiliary_ledger",
]
