from __future__ import annotations

import copy
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    permission_grant_public_projection,
)
from .protocol import ABCError

EXECUTION_POLICY_VERSION = 1
EXECUTION_SESSION_RECEIPT_VERSION = 1
SESSION_CLEANUP_RECEIPT_VERSION = 4
LEGACY_SESSION_CLEANUP_RECEIPT_VERSION = 1
SESSION_CLEANUP_RECEIPT_VERSION_V2 = 2
SESSION_CLEANUP_RECEIPT_VERSION_V3 = 3
RESOURCE_EXTENSION_KEY = "agentbc.resources"
SESSION_EXTENSION_KEY = "agentbc.session"
RESOURCE_MULTIPLIER = 2
MAX_SESSION_CLEANUP_ATTEMPTS = 3

RESOURCE_KIND_BY_EXECUTOR = {
    "claude": "max_budget_usd",
    "hermes": "max_turns",
}
PROJECT_MODES = frozenset({"native", "ephemeral", "none"})
SESSION_STATES = frozenset(
    {"pending", "active", "input_required", "needs_recovery", "terminal"}
)
CLEANUP_STATES = frozenset(
    {"not_requested", "retained", "pending", "succeeded", "unsupported", "failed"}
)
CLEANUP_CAPABILITIES = frozenset(
    {"unknown", "supported", "unsupported", "not_applicable"}
)
CLEANUP_STRATEGIES = frozenset(
    {
        "none",
        "retain",
        "claude_project_purge",
        "official_session_delete",
        "official_session_archive_then_delete",
    }
)
# SESSION-104-001 bounded command-evidence statuses.  ``acknowledged`` means
# the exact RPC response arrived; ``confirmed`` means an official response
# proved the state; ``unverified`` covers evidence lost to transport death.
CLEANUP_COMMAND_STATUSES = frozenset(
    {
        "not_requested",
        "acknowledged",
        "confirmed",
        "failed",
        "unverified",
        "not_applicable",
    }
)
CLEANUP_COMMAND_ENTRY_FIELDS = frozenset({"status", "checked_at"})
RESOLVED_CLEANUP_STATES = frozenset({"retained", "succeeded", "unsupported"})
CLEANUP_VERIFICATION_SIDES_V2 = frozenset({"cli", "desktop"})
CLEANUP_VERIFICATION_SIDES = frozenset(
    {"cli", "desktop_backend", "desktop_live"}
)
CLEANUP_VERIFICATION_STATUSES = frozenset(
    {"unknown", "absent", "present", "unavailable", "unverified", "not_applicable"}
)
CLEANUP_RECEIPT_FIELDS_V1 = frozenset(
    {
        "version",
        "capability",
        "strategy",
        "state",
        "attempts",
        "requested_at",
        "last_attempt_at",
        "next_attempt_at",
        "completed_at",
        "error_code",
        "retryable",
    }
)
CLEANUP_RECEIPT_FIELDS_V2 = CLEANUP_RECEIPT_FIELDS_V1 | {"verification"}
CLEANUP_RECEIPT_FIELDS_V3 = CLEANUP_RECEIPT_FIELDS_V2
CLEANUP_RECEIPT_FIELDS = CLEANUP_RECEIPT_FIELDS_V2 | {"commands"}
RESOURCE_DECISIONS = frozenset({"", "increase", "terminate"})
TERMINAL_SESSION_CLEANUP_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "rejected"}
)

_HERMES_SESSION_RECEIPT_RE = re.compile(
    r"(?m)^[ \t]*session_id:[ \t]*([^\s]+)[ \t]*$"
)
_CLEANUP_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

SESSION_RECEIPT_SOURCES = {
    "claude": "preallocated",
    "hermes": "stderr_receipt",
    "codex": "jsonl_thread_started",
}


def build_resource_snapshot(
    executor: str,
    limit: float,
    *,
    source: str = "config",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the durable per-task resource-policy snapshot."""
    normalized_executor = str(executor or "").strip().lower()
    resource = RESOURCE_KIND_BY_EXECUTOR.get(normalized_executor, "")
    normalized_limit = _normalize_resource_limit(normalized_executor, limit)
    snapshot: dict[str, Any] = {
        "version": EXECUTION_POLICY_VERSION,
        "executor": normalized_executor,
        "resource": resource,
        "configured_limit": normalized_limit,
        "current_limit": normalized_limit,
        "multiplier": RESOURCE_MULTIPLIER,
        "exhaustion_count": 0,
        "last_decision": "",
        "source": str(source or "").strip(),
        "created_at": created_at or _utc_now(),
    }
    _raise_policy_errors(validate_resource_snapshot(snapshot), RESOURCE_EXTENSION_KEY)
    return snapshot


def validate_resource_snapshot(
    value: Any,
    *,
    executor: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{RESOURCE_EXTENSION_KEY} must be an object"]
    if value.get("version") != EXECUTION_POLICY_VERSION:
        errors.append(
            f"{RESOURCE_EXTENSION_KEY}.version must be {EXECUTION_POLICY_VERSION}"
        )
    actual_executor = str(value.get("executor") or "").strip().lower()
    if actual_executor not in RESOURCE_KIND_BY_EXECUTOR:
        errors.append(f"{RESOURCE_EXTENSION_KEY}.executor is unsupported: {actual_executor}")
    if executor is not None and actual_executor != str(executor).strip().lower():
        errors.append(f"{RESOURCE_EXTENSION_KEY}.executor does not match {executor}")
    expected_resource = RESOURCE_KIND_BY_EXECUTOR.get(actual_executor)
    if value.get("resource") != expected_resource:
        errors.append(
            f"{RESOURCE_EXTENSION_KEY}.resource must be {expected_resource or 'executor-specific'}"
        )
    for field in ("configured_limit", "current_limit"):
        if not _valid_resource_limit(actual_executor, value.get(field)):
            errors.append(f"{RESOURCE_EXTENSION_KEY}.{field} is invalid")
    if value.get("multiplier") != RESOURCE_MULTIPLIER:
        errors.append(
            f"{RESOURCE_EXTENSION_KEY}.multiplier must be {RESOURCE_MULTIPLIER}"
        )
    exhaustion_count = value.get("exhaustion_count")
    if (
        isinstance(exhaustion_count, bool)
        or not isinstance(exhaustion_count, int)
        or exhaustion_count < 0
    ):
        errors.append(f"{RESOURCE_EXTENSION_KEY}.exhaustion_count must be a non-negative integer")
    if str(value.get("last_decision") or "") not in RESOURCE_DECISIONS:
        errors.append(f"{RESOURCE_EXTENSION_KEY}.last_decision is invalid")
    for field in ("source", "created_at"):
        if not isinstance(value.get(field), str) or not str(value.get(field)).strip():
            errors.append(f"{RESOURCE_EXTENSION_KEY}.{field} must be non-empty")
    return errors


def apply_resource_input_decision(
    value: Any,
    request: Any,
    response_type: str,
    *,
    executor: str,
) -> dict[str, Any]:
    """Validate and apply one task-scoped resource approve/deny decision."""
    errors = validate_resource_snapshot(value, executor=executor)
    if errors:
        raise ABCError("resource_decision_invalid", "; ".join(errors), {"errors": errors})
    if not is_resource_decision_request(request):
        raise ABCError(
            "resource_decision_invalid",
            (
                "Resource-limit input must be a choice with kind=resource_limit "
                "and response_protocol=approve_deny"
            ),
        )
    decision = str(response_type or "").strip()
    if decision not in {"approve", "deny"}:
        raise ABCError(
            "invalid_input_response",
            "Resource-limit input requires --approve or --deny",
        )

    resource = dict(value)
    current_limit = resource["current_limit"]
    request_current_limit = request.get("current_limit")
    if isinstance(request_current_limit, bool) or request_current_limit != current_limit:
        raise ABCError(
            "resource_decision_stale",
            "Resource input current_limit no longer matches the task snapshot",
        )
    request_executor = request.get("executor")
    if request_executor not in (None, "") and request_executor != resource["executor"]:
        raise ABCError(
            "resource_decision_stale",
            "Resource input executor no longer matches the task snapshot",
        )
    request_resource = request.get("resource")
    if request_resource not in (None, "") and request_resource != resource["resource"]:
        raise ABCError(
            "resource_decision_stale",
            "Resource input kind no longer matches the task snapshot",
        )

    if decision == "approve":
        next_limit = next_resource_limit(resource, executor=executor)
        request_next_limit = request.get("next_limit")
        if isinstance(request_next_limit, bool) or request_next_limit != next_limit:
            raise ABCError(
                "resource_decision_stale",
                "Resource input next_limit does not match the exact task multiplier",
            )
        resource["current_limit"] = next_limit
        resource["last_decision"] = "increase"
    else:
        resource["last_decision"] = "terminate"
    errors = validate_resource_snapshot(resource, executor=executor)
    if errors:
        raise ABCError("resource_decision_invalid", "; ".join(errors), {"errors": errors})
    return resource


def is_resource_decision_request(value: Any) -> bool:
    """Recognize only the complete resource-decision discriminator tuple."""
    return (
        isinstance(value, dict)
        and str(value.get("type") or "").strip().lower() == "choice"
        and value.get("kind") == "resource_limit"
        and value.get("response_protocol") == "approve_deny"
    )


def next_resource_limit(value: Any, *, executor: str) -> int | float:
    """Return the exact validated next task limit without mutating the snapshot."""
    errors = validate_resource_snapshot(value, executor=executor)
    if errors:
        raise ABCError("resource_decision_invalid", "; ".join(errors), {"errors": errors})
    current_limit = value["current_limit"]
    multiplier = value["multiplier"]
    return _normalize_resource_limit(executor, current_limit * multiplier)


def build_session_snapshot(
    executor: str,
    *,
    retain: bool,
    session_id: str = "",
    project_mode: str | None = None,
    project_path: str = "",
    session_state: str = "pending",
    run_ids: list[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the durable executor-session snapshot without touching Executor storage."""
    normalized_executor = str(executor or "").strip().lower()
    if project_mode is None:
        normalized_mode = "native" if normalized_executor == "claude" and retain else (
            "ephemeral" if normalized_executor == "claude" else "none"
        )
    else:
        normalized_mode = str(project_mode).strip().lower()
    snapshot: dict[str, Any] = {
        "version": EXECUTION_POLICY_VERSION,
        "executor": normalized_executor,
        "retain": retain,
        "session_id": str(session_id or "").strip(),
        "session_state": str(session_state or "").strip().lower(),
        "project_mode": normalized_mode,
        "project_path": str(project_path or "").strip(),
        "run_ids": [str(item).strip() for item in (run_ids or []) if str(item).strip()],
        "run_resume_facts": {},
        "resume_count": 0,
        "cleanup": build_session_cleanup_receipt(),
        "created_at": created_at or _utc_now(),
    }
    _raise_policy_errors(validate_session_snapshot(snapshot), SESSION_EXTENSION_KEY)
    return snapshot


def _empty_cleanup_verification(
    status: str = "unknown",
    *,
    checked_at: str = "",
) -> dict[str, dict[str, str]]:
    return {
        "cli": {"status": status, "checked_at": checked_at},
        "desktop_backend": {"status": status, "checked_at": checked_at},
        "desktop_live": {"status": status, "checked_at": checked_at},
    }


def _empty_cleanup_verification_v2(
    status: str = "unknown",
    *,
    checked_at: str = "",
) -> dict[str, dict[str, str]]:
    return {
        "cli": {"status": status, "checked_at": checked_at},
        "desktop": {"status": status, "checked_at": checked_at},
    }


def _empty_cleanup_commands(
    status: str = "not_requested",
    *,
    checked_at: str = "",
) -> dict[str, dict[str, str]]:
    """Build the bounded v4 per-command evidence object."""
    return {
        "archive": {"status": status, "checked_at": checked_at},
        "delete": {"status": status, "checked_at": checked_at},
    }


def _valid_cleanup_commands(value: Any) -> bool:
    """Validate the persisted bounded v4 ``commands`` object."""
    if not isinstance(value, dict) or set(value) != {"archive", "delete"}:
        return False
    for command in ("archive", "delete"):
        item = value.get(command)
        if not isinstance(item, dict) or set(item) != CLEANUP_COMMAND_ENTRY_FIELDS:
            return False
        status = item.get("status")
        if type(status) is not str or status not in CLEANUP_COMMAND_STATUSES:
            return False
        checked_at = item.get("checked_at")
        if type(checked_at) is not str or (
            checked_at and not _valid_utc_timestamp(checked_at)
        ):
            return False
    return True


def _valid_cleanup_verification(value: Any, *, v2: bool) -> bool:
    """Validate a persisted bounded verification object without projecting it."""
    sides = CLEANUP_VERIFICATION_SIDES_V2 if v2 else CLEANUP_VERIFICATION_SIDES
    if not isinstance(value, dict) or set(value) != sides:
        return False
    for side in sides:
        item = value.get(side)
        if not isinstance(item, dict) or set(item) != {"status", "checked_at"}:
            return False
        if (
            type(item.get("status")) is not str
            or item["status"] not in CLEANUP_VERIFICATION_STATUSES
        ):
            return False
        checked_at = item.get("checked_at")
        if type(checked_at) is not str or (
            checked_at and not _valid_utc_timestamp(checked_at)
        ):
            return False
    return True


def cleanup_verification_public_view(value: Any) -> dict[str, dict[str, str]]:
    """Project v3 verification with the v2-compatible aggregate ``desktop`` side."""
    checked = normalize_cleanup_verification(value)
    live = checked["desktop_live"]
    backend = checked["desktop_backend"]
    if live["status"] in {"absent", "present", "unavailable", "unverified"}:
        aggregate = live
    else:
        aggregate = backend
    return {
        "cli": dict(checked["cli"]),
        "desktop_backend": dict(backend),
        "desktop_live": dict(live),
        "desktop": dict(aggregate),
    }


def normalize_cleanup_verification(value: Any) -> dict[str, dict[str, str]]:
    """Normalize v2 or v3 bounded verification into the v3 internal tuple."""
    if not isinstance(value, dict):
        return _empty_cleanup_verification()
    if set(value) == CLEANUP_VERIFICATION_SIDES_V2:
        value = {
            "cli": value.get("cli"),
            "desktop_backend": value.get("desktop"),
            "desktop_live": {"status": "unverified", "checked_at": ""},
        }
    elif set(value) != CLEANUP_VERIFICATION_SIDES:
        return _empty_cleanup_verification()
    result: dict[str, dict[str, str]] = {}
    for side in ("cli", "desktop_backend", "desktop_live"):
        item = value.get(side)
        if not isinstance(item, dict) or set(item) != {"status", "checked_at"}:
            return _empty_cleanup_verification()
        status = item.get("status")
        checked_at = item.get("checked_at")
        if type(status) is not str or status not in CLEANUP_VERIFICATION_STATUSES:
            return _empty_cleanup_verification()
        if type(checked_at) is not str or (
            checked_at and not _valid_utc_timestamp(checked_at)
        ):
            return _empty_cleanup_verification()
        result[side] = {"status": status, "checked_at": checked_at}
    return result


def normalize_cleanup_commands(value: Any) -> dict[str, dict[str, str]]:
    """Normalize a bounded v4 commands object; unknown shapes fail closed."""
    if not isinstance(value, dict) or set(value) != {"archive", "delete"}:
        return _empty_cleanup_commands()
    result: dict[str, dict[str, str]] = {}
    for command in ("archive", "delete"):
        item = value.get(command)
        if not isinstance(item, dict) or set(item) != CLEANUP_COMMAND_ENTRY_FIELDS:
            return _empty_cleanup_commands()
        status = item.get("status")
        checked_at = item.get("checked_at")
        if type(status) is not str or status not in CLEANUP_COMMAND_STATUSES:
            return _empty_cleanup_commands()
        if type(checked_at) is not str or (
            checked_at and not _valid_utc_timestamp(checked_at)
        ):
            return _empty_cleanup_commands()
        result[command] = {"status": status, "checked_at": checked_at}
    return result


def _upgrade_cleanup_receipt(value: Any) -> dict[str, Any]:
    """Upgrade a valid historical receipt to the v4 transition representation."""
    if set(value) == {"state", "attempts"}:
        receipt = build_session_cleanup_receipt()
        receipt["state"] = value["state"]
        receipt["attempts"] = value["attempts"]
        return receipt
    version = value.get("version")
    if version == SESSION_CLEANUP_RECEIPT_VERSION:
        return copy.deepcopy(value)
    receipt = build_session_cleanup_receipt()
    for field in CLEANUP_RECEIPT_FIELDS_V1 - {"version"}:
        receipt[field] = copy.deepcopy(value[field])
    if version == SESSION_CLEANUP_RECEIPT_VERSION_V2:
        legacy_verification = value.get("verification")
        normalized = normalize_cleanup_verification(legacy_verification)
        receipt["verification"] = normalized
    else:
        receipt["verification"] = _empty_cleanup_verification("unverified")
    return receipt


def build_session_cleanup_receipt() -> dict[str, Any]:
    """Build the safe, inert v4 receipt used before cleanup is requested."""
    return {
        "version": SESSION_CLEANUP_RECEIPT_VERSION,
        "capability": "unknown",
        "strategy": "none",
        "state": "not_requested",
        "attempts": 0,
        "requested_at": "",
        "last_attempt_at": "",
        "next_attempt_at": "",
        "completed_at": "",
        "error_code": "",
        "retryable": False,
        "verification": _empty_cleanup_verification(),
        "commands": _empty_cleanup_commands(),
    }


def read_session_cleanup_receipt(value: Any) -> dict[str, Any]:
    """Read v4 and project v3/v2/v1/minimal receipts into the v4 view."""
    errors = validate_session_cleanup_receipt(value, allow_legacy=True)
    if errors:
        _raise_policy_errors(errors, f"{SESSION_EXTENSION_KEY}.cleanup")
    return _upgrade_cleanup_receipt(value)


def upgrade_session_cleanup_receipt(value: Any) -> dict[str, Any]:
    """Return a v3 transition copy for a valid v1/v2 or minimal receipt."""
    errors = validate_session_cleanup_receipt(value, allow_legacy=True)
    if errors:
        _raise_policy_errors(errors, f"{SESSION_EXTENSION_KEY}.cleanup")
    return _upgrade_cleanup_receipt(value)


def session_cleanup_view(value: Any) -> dict[str, Any]:
    """Return the safe status/report/doctor cleanup projection."""
    try:
        receipt = read_session_cleanup_receipt(value)
    except ABCError:
        receipt = build_session_cleanup_receipt()
    original_version = value.get("version") if isinstance(value, dict) else None
    base = {
        "capability": receipt["capability"],
        "state": receipt["state"],
        "attempts": receipt["attempts"],
        "error_code": receipt["error_code"],
        "retryable": receipt["retryable"],
    }
    if original_version == LEGACY_SESSION_CLEANUP_RECEIPT_VERSION and receipt["state"] == "succeeded":
        checked_at = str(receipt.get("completed_at") or receipt.get("last_attempt_at") or "")
        return {
            **base,
            "version": LEGACY_SESSION_CLEANUP_RECEIPT_VERSION,
            "state": "legacy",
            "error_code": "legacy_cleanup_unverified",
            "retryable": False,
            "verification": cleanup_verification_public_view(
                _empty_cleanup_verification("unverified", checked_at=checked_at)
            ),
        }
    if original_version == LEGACY_SESSION_CLEANUP_RECEIPT_VERSION:
        return base
    if original_version == SESSION_CLEANUP_RECEIPT_VERSION:
        # v4 projects the full command evidence surface.
        return {
            **base,
            "version": SESSION_CLEANUP_RECEIPT_VERSION,
            "strategy": receipt["strategy"],
            "verification": cleanup_verification_public_view(receipt.get("verification")),
            "commands": {
                command: dict((receipt.get("commands") or {}).get(command) or {})
                for command in ("archive", "delete")
            },
        }
    # v2/v3 history keeps its own shape: no commands evidence ever existed
    # for those versions, and history is never rewritten.
    return {
        **base,
        "version": original_version,
        "strategy": receipt["strategy"],
        "verification": cleanup_verification_public_view(receipt.get("verification")),
    }


def _validate_cleanup_fields(
    value: dict[str, Any],
    *,
    fields: set[str],
    prefix: str,
    version: int,
) -> list[str]:
    if version == LEGACY_SESSION_CLEANUP_RECEIPT_VERSION:
        expected = CLEANUP_RECEIPT_FIELDS_V1
    elif version == SESSION_CLEANUP_RECEIPT_VERSION_V2:
        expected = CLEANUP_RECEIPT_FIELDS_V2
    elif version == SESSION_CLEANUP_RECEIPT_VERSION_V3:
        expected = CLEANUP_RECEIPT_FIELDS_V3
    else:
        expected = CLEANUP_RECEIPT_FIELDS
    errors: list[str] = []
    missing = sorted(expected - fields)
    unknown = sorted(fields - expected)
    if missing:
        errors.append(f"{prefix} missing fields: {', '.join(missing)}")
    if unknown:
        errors.append(f"{prefix} contains unsupported fields: {', '.join(unknown)}")
    if missing or unknown:
        return errors

    expected_version = version
    if type(value.get("version")) is not int or value.get("version") != expected_version:
        errors.append(f"{prefix}.version must be {expected_version}")
    capability = value.get("capability")
    if type(capability) is not str or capability not in CLEANUP_CAPABILITIES:
        errors.append(f"{prefix}.capability is invalid")
    strategy = value.get("strategy")
    if type(strategy) is not str or strategy not in CLEANUP_STRATEGIES:
        errors.append(f"{prefix}.strategy is invalid")
    state = value.get("state")
    if type(state) is not str or state not in CLEANUP_STATES:
        errors.append(f"{prefix}.state is invalid")
    attempts = value.get("attempts")
    if type(attempts) is not int or attempts < 0:
        errors.append(f"{prefix}.attempts must be a non-negative integer")
    for field in ("requested_at", "last_attempt_at", "next_attempt_at", "completed_at"):
        timestamp = value.get(field)
        if type(timestamp) is not str:
            errors.append(f"{prefix}.{field} must be a string")
        elif timestamp and not _valid_utc_timestamp(timestamp):
            errors.append(f"{prefix}.{field} must be an ISO-8601 timestamp with timezone")
    error_code = value.get("error_code")
    if type(error_code) is not str:
        errors.append(f"{prefix}.error_code must be a string")
    elif error_code and not _CLEANUP_ERROR_CODE_RE.fullmatch(error_code):
        errors.append(f"{prefix}.error_code must be a stable lowercase code")
    if type(value.get("retryable")) is not bool:
        errors.append(f"{prefix}.retryable must be a boolean")
    if version == SESSION_CLEANUP_RECEIPT_VERSION_V2:
        verification = value.get("verification")
        if not _valid_cleanup_verification(verification, v2=True):
            errors.append(f"{prefix}.verification is invalid")
    elif version in {
        SESSION_CLEANUP_RECEIPT_VERSION_V3,
        SESSION_CLEANUP_RECEIPT_VERSION,
    }:
        verification = value.get("verification")
        if not _valid_cleanup_verification(verification, v2=False):
            errors.append(f"{prefix}.verification is invalid")
    if version == SESSION_CLEANUP_RECEIPT_VERSION:
        commands = value.get("commands")
        if not _valid_cleanup_commands(commands):
            errors.append(f"{prefix}.commands is invalid")
    if errors:
        return errors

    if state == "not_requested":
        if attempts != 0 or any(
            value[field]
            for field in (
                "requested_at",
                "last_attempt_at",
                "next_attempt_at",
                "completed_at",
                "error_code",
            )
        ):
            errors.append(f"{prefix}.not_requested receipt must be inert")
        if capability != "unknown" or strategy != "none" or value["retryable"]:
            errors.append(f"{prefix}.not_requested receipt uses unsafe metadata")
        if version == SESSION_CLEANUP_RECEIPT_VERSION and value["verification"] != _empty_cleanup_verification():
            errors.append(f"{prefix}.not_requested receipt uses unsafe verification metadata")
        if version == SESSION_CLEANUP_RECEIPT_VERSION_V2 and value["verification"] != _empty_cleanup_verification_v2():
            errors.append(f"{prefix}.not_requested receipt uses unsafe verification metadata")
        if version == SESSION_CLEANUP_RECEIPT_VERSION and value["commands"] != _empty_cleanup_commands():
            errors.append(f"{prefix}.not_requested receipt uses unsafe command metadata")
    elif state == "retained":
        if capability != "not_applicable" or strategy != "retain":
            errors.append(f"{prefix}.retained receipt requires retain semantics")
        if not value["completed_at"] or value["retryable"] or error_code:
            errors.append(f"{prefix}.retained receipt must be resolved")
    elif state == "pending":
        if capability not in {"unknown", "supported"} or strategy == "retain":
            errors.append(f"{prefix}.pending receipt has incompatible capability metadata")
        if attempts < 1 or not value["requested_at"] or not value["last_attempt_at"]:
            errors.append(f"{prefix}.pending receipt requires request and attempt metadata")
        if value["completed_at"] or value["retryable"] or error_code:
            errors.append(f"{prefix}.pending receipt must remain unresolved")
    elif state == "succeeded":
        if capability != "supported" or strategy in {"none", "retain"}:
            errors.append(f"{prefix}.succeeded receipt requires a supported delete strategy")
        if not value["completed_at"] or value["retryable"] or error_code:
            errors.append(f"{prefix}.succeeded receipt must be resolved")
        if version == SESSION_CLEANUP_RECEIPT_VERSION_V2:
            verification = value["verification"]
            statuses = {verification["cli"]["status"], verification["desktop"]["status"]}
            if statuses != {"absent"} and statuses != {"not_applicable"}:
                errors.append(f"{prefix}.succeeded receipt requires both verification sides absent")
            elif statuses == {"absent"} and any(
                not verification[side]["checked_at"] for side in ("cli", "desktop")
            ):
                errors.append(f"{prefix}.succeeded receipt requires verification timestamps")
        elif version in {
            SESSION_CLEANUP_RECEIPT_VERSION_V3,
            SESSION_CLEANUP_RECEIPT_VERSION,
        }:
            verification = value["verification"]
            statuses = {
                verification["cli"]["status"],
                verification["desktop_backend"]["status"],
                verification["desktop_live"]["status"],
            }
            if version == SESSION_CLEANUP_RECEIPT_VERSION:
                # v4 gate: under the archive-then-delete strategy the two
                # command acknowledgements are the real success proof; the
                # three verification sides are non-gating diagnostics and
                # desktop_live is not_applicable.  Other strategies keep the
                # historical sides and carry not_applicable commands.
                commands = value["commands"]
                command_statuses = {
                    commands["archive"]["status"],
                    commands["delete"]["status"],
                }
                if strategy == "official_session_archive_then_delete":
                    if command_statuses - {"acknowledged", "confirmed"}:
                        errors.append(
                            f"{prefix}.succeeded receipt requires both archive and delete commands acknowledged or confirmed"
                        )
                    if any(
                        not commands[command]["checked_at"]
                        for command in ("archive", "delete")
                    ):
                        errors.append(
                            f"{prefix}.succeeded receipt requires command acknowledgement timestamps"
                        )
                elif command_statuses - {"acknowledged", "confirmed", "not_applicable"}:
                    errors.append(
                        f"{prefix}.succeeded receipt requires command evidence or not_applicable"
                    )
                # The backend/read/list sides are non-gating diagnostics under
                # v4: every bounded status (including the accepted Desktop
                # refresh delay) is recordable without blocking the receipt.
            elif statuses != {"absent"} and statuses != {"not_applicable"}:
                errors.append(f"{prefix}.succeeded receipt requires all verification sides absent")
            elif statuses == {"absent"} and any(
                not verification[side]["checked_at"]
                for side in ("cli", "desktop_backend", "desktop_live")
            ):
                errors.append(f"{prefix}.succeeded receipt requires verification timestamps")
    elif state == "unsupported":
        if capability != "unsupported" or strategy != "none":
            errors.append(f"{prefix}.unsupported receipt requires unsupported capability")
        if not value["completed_at"] or value["retryable"] or not error_code:
            errors.append(f"{prefix}.unsupported receipt must contain a stable reason")
    elif state == "failed":
        if capability not in {"unknown", "supported"} or strategy == "retain":
            errors.append(f"{prefix}.failed receipt has incompatible capability metadata")
        if attempts < 1 or not value["requested_at"] or not value["last_attempt_at"]:
            errors.append(f"{prefix}.failed receipt requires attempt metadata")
        if value["completed_at"] or not error_code:
            errors.append(f"{prefix}.failed receipt must remain unresolved with a reason")
        if value["retryable"] != bool(value["next_attempt_at"]):
            errors.append(f"{prefix}.failed retry metadata is inconsistent")
    return errors


def validate_session_cleanup_receipt(
    value: Any,
    *,
    allow_legacy: bool = False,
) -> list[str]:
    """Validate v4 receipts while accepting exact v1/v2/v3 compatibility forms."""
    prefix = f"{SESSION_EXTENSION_KEY}.cleanup"
    if not isinstance(value, dict):
        return [f"{prefix} must be an object"]
    fields = set(value)
    if allow_legacy and fields == {"state", "attempts"}:
        errors: list[str] = []
        if type(value.get("state")) is not str or value.get("state") not in CLEANUP_STATES:
            errors.append(f"{prefix}.state is invalid")
        attempts = value.get("attempts")
        if type(attempts) is not int or attempts < 0:
            errors.append(f"{prefix}.attempts must be a non-negative integer")
        return errors
    version = value.get("version")
    if version not in {
        LEGACY_SESSION_CLEANUP_RECEIPT_VERSION,
        SESSION_CLEANUP_RECEIPT_VERSION_V2,
        SESSION_CLEANUP_RECEIPT_VERSION_V3,
        SESSION_CLEANUP_RECEIPT_VERSION,
    }:
        # Unknown versions fail closed; they are only re-labeled to surface
        # a bounded field/version error, never accepted.
        version = SESSION_CLEANUP_RECEIPT_VERSION
    return _validate_cleanup_fields(value, fields=fields, prefix=prefix, version=version)


def validate_session_snapshot(
    value: Any,
    *,
    executor: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{SESSION_EXTENSION_KEY} must be an object"]
    if value.get("version") != EXECUTION_POLICY_VERSION:
        errors.append(
            f"{SESSION_EXTENSION_KEY}.version must be {EXECUTION_POLICY_VERSION}"
        )
    actual_executor = str(value.get("executor") or "").strip().lower()
    if actual_executor not in {"claude", "hermes", "codex"}:
        errors.append(f"{SESSION_EXTENSION_KEY}.executor is unsupported: {actual_executor}")
    if executor is not None and actual_executor != str(executor).strip().lower():
        errors.append(f"{SESSION_EXTENSION_KEY}.executor does not match {executor}")
    if not isinstance(value.get("retain"), bool):
        errors.append(f"{SESSION_EXTENSION_KEY}.retain must be a boolean")
    session_state = str(value.get("session_state") or "").strip().lower()
    if session_state not in SESSION_STATES:
        errors.append(f"{SESSION_EXTENSION_KEY}.session_state is invalid")
    session_id = value.get("session_id")
    if not isinstance(session_id, str):
        errors.append(f"{SESSION_EXTENSION_KEY}.session_id must be a string")
    elif session_state != "pending" and not session_id.strip():
        errors.append(
            f"{SESSION_EXTENSION_KEY}.session_id is required after the pending state"
        )
    if "official_receipt_bound" in value and type(value.get("official_receipt_bound")) is not bool:
        errors.append(f"{SESSION_EXTENSION_KEY}.official_receipt_bound must be a boolean")
    if "receipt_source" in value and not isinstance(value.get("receipt_source"), str):
        errors.append(f"{SESSION_EXTENSION_KEY}.receipt_source must be a string")
    if "archive_acknowledged" in value and type(value.get("archive_acknowledged")) is not bool:
        errors.append(f"{SESSION_EXTENSION_KEY}.archive_acknowledged must be a boolean")
    if "archive_checked_at" in value and not isinstance(value.get("archive_checked_at"), str):
        errors.append(f"{SESSION_EXTENSION_KEY}.archive_checked_at must be a string")
    if value.get("archive_acknowledged") is True and not str(
        value.get("archive_checked_at") or ""
    ).strip():
        errors.append(
            f"{SESSION_EXTENSION_KEY}.archive_checked_at is required after archive acknowledgement"
        )
    if (
        actual_executor == "codex"
        and value.get("official_receipt_bound") is True
        and value.get("receipt_source") != SESSION_RECEIPT_SOURCES["codex"]
    ):
        errors.append(
            f"{SESSION_EXTENSION_KEY}.receipt_source must be {SESSION_RECEIPT_SOURCES['codex']}"
        )
    project_mode = str(value.get("project_mode") or "").strip().lower()
    if project_mode not in PROJECT_MODES:
        errors.append(f"{SESSION_EXTENSION_KEY}.project_mode is invalid")
    project_path = value.get("project_path")
    if not isinstance(project_path, str):
        errors.append(f"{SESSION_EXTENSION_KEY}.project_path must be a string")
        project_path = ""
    if actual_executor == "claude":
        expected_mode = "native" if value.get("retain") is True else "ephemeral"
        if project_mode != expected_mode:
            errors.append(
                f"{SESSION_EXTENSION_KEY}.project_mode must be {expected_mode} for Claude"
            )
        if not project_path or not Path(project_path).expanduser().is_absolute():
            errors.append(f"{SESSION_EXTENSION_KEY}.project_path must be absolute for Claude")
    elif project_mode != "none" or project_path:
        errors.append(
            f"{SESSION_EXTENSION_KEY} project fields are only supported for Claude"
        )
    run_ids = value.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in run_ids)
        or len(run_ids) != len(set(run_ids))
    ):
        errors.append(f"{SESSION_EXTENSION_KEY}.run_ids must contain unique non-empty strings")
    run_resume_facts = value.get("run_resume_facts")
    if run_resume_facts is not None:
        if not isinstance(run_resume_facts, dict):
            errors.append(f"{SESSION_EXTENSION_KEY}.run_resume_facts must be an object")
        elif any(
            not isinstance(run_id, str)
            or not run_id.strip()
            or type(resumed) is not bool
            or not isinstance(run_ids, list)
            or run_id not in run_ids
            for run_id, resumed in run_resume_facts.items()
        ):
            errors.append(
                f"{SESSION_EXTENSION_KEY}.run_resume_facts must map recorded run IDs to booleans"
            )
    resume_count = value.get("resume_count")
    if (
        isinstance(resume_count, bool)
        or not isinstance(resume_count, int)
        or resume_count < 0
    ):
        errors.append(f"{SESSION_EXTENSION_KEY}.resume_count must be a non-negative integer")
    cleanup = value.get("cleanup")
    errors.extend(validate_session_cleanup_receipt(cleanup, allow_legacy=True))
    if not isinstance(value.get("created_at"), str) or not str(value.get("created_at")).strip():
        errors.append(f"{SESSION_EXTENSION_KEY}.created_at must be non-empty")
    return errors


def validate_execution_session_receipt(
    value: Any,
    *,
    executor: str | None = None,
) -> list[str]:
    """Validate the adapter-to-worker receipt for one persisted executor session."""
    if not isinstance(value, dict):
        return ["execution_session must be an object"]
    errors: list[str] = []
    if value.get("version") != EXECUTION_SESSION_RECEIPT_VERSION:
        errors.append(
            f"execution_session.version must be {EXECUTION_SESSION_RECEIPT_VERSION}"
        )
    actual_executor = str(value.get("executor") or "").strip().lower()
    if actual_executor not in SESSION_RECEIPT_SOURCES:
        errors.append(f"execution_session.executor is unsupported: {actual_executor}")
    if executor is not None and actual_executor != str(executor).strip().lower():
        errors.append(f"execution_session.executor does not match {executor}")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        errors.append("execution_session.session_id must be a non-empty string")
    if not isinstance(value.get("resumed"), bool):
        errors.append("execution_session.resumed must be a boolean")
    if value.get("persistence") != "persistent":
        errors.append("execution_session.persistence must be persistent")
    expected_source = SESSION_RECEIPT_SOURCES.get(actual_executor)
    if value.get("source") != expected_source:
        errors.append(
            f"execution_session.source must be {expected_source or 'executor-specific'}"
        )
    return errors


def validate_execution_policy_extensions(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["extensions must be an object"]
    errors: list[str] = []
    if RESOURCE_EXTENSION_KEY in value:
        errors.extend(validate_resource_snapshot(value[RESOURCE_EXTENSION_KEY]))
    if SESSION_EXTENSION_KEY in value:
        errors.extend(validate_session_snapshot(value[SESSION_EXTENSION_KEY]))
    return errors


def attach_execution_policy(
    extensions: dict[str, Any] | None,
    *,
    resources: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy with validated policy snapshots attached under canonical keys."""
    updated = dict(extensions or {})
    if resources is not None:
        _raise_policy_errors(validate_resource_snapshot(resources), RESOURCE_EXTENSION_KEY)
        updated[RESOURCE_EXTENSION_KEY] = dict(resources)
    if session is not None:
        _raise_policy_errors(validate_session_snapshot(session), SESSION_EXTENSION_KEY)
        updated[SESSION_EXTENSION_KEY] = dict(session)
    return updated


def build_task_execution_policy(
    executor: str,
    config: dict[str, Any] | None,
    workspace: dict[str, Any],
    *,
    created_at: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Freeze the v1 resource and session policy for one new task assignment."""
    normalized_executor = str(executor or "").strip().lower()
    if normalized_executor not in {"claude", "hermes", "codex"}:
        return None, None

    from .config import (
        configured_claude_budget,
        configured_hermes_max_turns,
        configured_session_retention,
        validate_config,
    )

    config_value = config if isinstance(config, dict) else {}
    config_errors = validate_config(config_value)
    if config_errors:
        raise ABCError(
            "config_invalid",
            "; ".join(config_errors),
            {"errors": config_errors},
        )

    resources: dict[str, Any] | None = None
    if normalized_executor == "claude":
        limit, source = configured_claude_budget(config_value)
        resources = build_resource_snapshot(
            normalized_executor,
            limit,
            source=source,
            created_at=created_at,
        )
    elif normalized_executor == "hermes":
        limit, source = configured_hermes_max_turns(config_value)
        resources = build_resource_snapshot(
            normalized_executor,
            limit,
            source=source,
            created_at=created_at,
        )

    retain, _retention_source = configured_session_retention(config_value)
    project_path = ""
    session_id = ""
    if normalized_executor == "claude":
        session_id = str(uuid.uuid4())
        if retain:
            project_path = str(workspace.get("project_root") or workspace.get("root") or "")
        else:
            project_path = str(
                workspace.get("executor_project_root")
                or _canonical_claude_project_path(workspace)
            )
    session = build_session_snapshot(
        normalized_executor,
        retain=retain,
        session_id=session_id,
        project_path=project_path,
        session_state="pending",
        created_at=created_at,
    )
    return resources, session


def permission_grant_view(value: Any) -> dict[str, Any] | None:
    """Return the sanitized one-shot grant projection, failing closed.

    Malformed, tampered or future-version grant envelopes project to ``None``
    so public interfaces never surface binding identifiers or sensitive fields
    and never crash the shared status/preflight/report view.
    """
    if value is None:
        return None
    try:
        return permission_grant_public_projection(value)
    except ABCError:
        return None


def execution_policy_view(extensions: Any) -> dict[str, Any]:
    """Return the stable, path-free policy projection used by public interfaces."""
    from .auxiliary_sessions import (
        AUXILIARY_EXTENSION_KEY,
        auxiliary_aggregate_view,
        auxiliary_ledger_view,
    )

    value = extensions if isinstance(extensions, dict) else {}
    resource = value.get(RESOURCE_EXTENSION_KEY)
    session = value.get(SESSION_EXTENSION_KEY)
    resource_view = None
    if isinstance(resource, dict):
        resource_view = {
            "resource": resource.get("resource"),
            "limit": resource.get("current_limit"),
            "configured_limit": resource.get("configured_limit"),
            "exhaustion_count": resource.get("exhaustion_count"),
            "last_decision": resource.get("last_decision"),
            "source": resource.get("source"),
            "frozen": True,
        }
    session_view = None
    if isinstance(session, dict):
        session_view = {
            "retain": session.get("retain"),
            "session_id": session.get("session_id"),
            "session_state": session.get("session_state"),
            "project_mode": session.get("project_mode"),
            "cleanup": session_cleanup_view(session.get("cleanup")),
        }
    return {
        "version": EXECUTION_POLICY_VERSION,
        "resources": resource_view,
        "session": session_view,
        "permission_grant": permission_grant_view(
            value.get(PERMISSION_GRANT_EXTENSION_KEY)
        ),
        "auxiliary_sessions": auxiliary_ledger_view(value.get(AUXILIARY_EXTENSION_KEY)),
        "auxiliary_aggregate": auxiliary_aggregate_view(value.get(AUXILIARY_EXTENSION_KEY)),
    }


def public_workspace_view(workspace: Any) -> dict[str, Any]:
    """Remove executor-only path-plan fields from a public workspace projection."""
    public = copy.deepcopy(workspace) if isinstance(workspace, dict) else {}
    public.pop("executor_project_root", None)
    return public


def public_extensions_view(extensions: Any) -> dict[str, Any]:
    """Remove internal session data while preserving the public extension record.

    The internal ``agentbc.permission_grant`` envelope is replaced by the same
    sanitized projection exposed through ``execution_policy_view``; malformed
    or unsupported envelopes are removed entirely (fail closed).  The auxiliary
    session ledger is replaced by its redacted public projection.
    """
    from .auxiliary_sessions import (
        AUXILIARY_EXTENSION_KEY,
        auxiliary_aggregate_view,
        auxiliary_ledger_view,
    )

    public = copy.deepcopy(extensions) if isinstance(extensions, dict) else {}
    session = public.get(SESSION_EXTENSION_KEY)
    if isinstance(session, dict):
        session.pop("project_path", None)
        session["cleanup"] = session_cleanup_view(session.get("cleanup"))
    if AUXILIARY_EXTENSION_KEY in public:
        public[AUXILIARY_EXTENSION_KEY] = {
            "sessions": auxiliary_ledger_view(public[AUXILIARY_EXTENSION_KEY]),
            "aggregate": auxiliary_aggregate_view(public[AUXILIARY_EXTENSION_KEY]),
        }
    input_request = public.get("agentbc.input")
    if isinstance(input_request, dict):
        # Full-fallback detail is a local dialog affordance, not a public task
        # projection.  The durable request remains available to Core's
        # notification builder.
        input_request.pop("reason_detail", None)
    input_history = public.get("agentbc.input_history")
    if isinstance(input_history, list):
        for historical_request in input_history:
            if isinstance(historical_request, dict):
                historical_request.pop("reason_detail", None)
    grant = public.get(PERMISSION_GRANT_EXTENSION_KEY)
    if grant is not None:
        projected = permission_grant_view(grant)
        if projected is None:
            public.pop(PERMISSION_GRANT_EXTENSION_KEY, None)
        else:
            public[PERMISSION_GRANT_EXTENSION_KEY] = projected
    return public


def public_task_view(task: dict[str, Any]) -> dict[str, Any]:
    """Return one public task projection without mutating the durable packet."""
    public = copy.deepcopy(task)
    extensions = task.get("extensions") if isinstance(task.get("extensions"), dict) else {}
    public["workspace"] = public_workspace_view(task.get("workspace"))
    public["extensions"] = public_extensions_view(extensions)
    public["execution_policy"] = execution_policy_view(extensions)
    return public


def extract_hermes_session_id(stderr: str) -> str | None:
    """Extract only Hermes' official single-query session receipt."""
    matches = _HERMES_SESSION_RECEIPT_RE.findall(str(stderr or ""))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def session_cleanup_blockers(
    *,
    task_status: str,
    lease_state: str,
    report_written: bool,
    notification_recorded: bool,
    session: Any,
) -> list[str]:
    """Return the ordered reasons post-terminal session cleanup must not run."""
    blockers: list[str] = []
    if str(task_status or "").strip().lower() not in TERMINAL_SESSION_CLEANUP_STATUSES:
        blockers.append("task_not_terminal")
    if str(lease_state or "").strip().lower() != "closed":
        blockers.append("run_lease_not_closed")
    if report_written is not True:
        blockers.append("report_not_written")
    if notification_recorded is not True:
        blockers.append("notification_not_recorded")
    session_errors = validate_session_snapshot(session)
    if session_errors:
        blockers.append("session_receipt_invalid")
        return blockers
    if session.get("retain") is True:
        blockers.append("retention_enabled")
    if str(session.get("session_state") or "") != "terminal":
        blockers.append("session_not_terminal")
    if not str(session.get("session_id") or "").strip():
        blockers.append("session_id_missing")
    if (
        str(session.get("executor") or "").strip().lower() == "codex"
        and session.get("retain") is not True
    ):
        if session.get("official_receipt_bound") is not True or session.get("receipt_source") != SESSION_RECEIPT_SOURCES["codex"]:
            blockers.append("session_receipt_unbound")
        try:
            codex_id = uuid.UUID(str(session.get("session_id") or "").strip())
        except (AttributeError, ValueError):
            blockers.append("session_id_invalid")
        else:
            if str(codex_id) != str(session.get("session_id") or "").strip().lower():
                blockers.append("session_id_invalid")
    cleanup = read_session_cleanup_receipt(session.get("cleanup"))
    if cleanup["state"] in RESOLVED_CLEANUP_STATES:
        blockers.append("cleanup_already_resolved")
    return blockers


def is_session_cleanup_eligible(**kwargs: Any) -> bool:
    return not session_cleanup_blockers(**kwargs)


def is_session_cleanup_resolved(value: Any) -> bool:
    """Return whether a strict or historical receipt is in an idempotent end state."""
    try:
        receipt = read_session_cleanup_receipt(value)
    except ABCError:
        return False
    return receipt["state"] in RESOLVED_CLEANUP_STATES


def transition_session_cleanup(
    session: Any,
    target_state: str,
    *,
    task_status: str,
    lease_state: str,
    report_written: bool,
    notification_recorded: bool,
    capability: str | None = None,
    strategy: str | None = None,
    error_code: str = "",
    retryable: bool = False,
    next_attempt_at: str = "",
    verification: Any | None = None,
    commands: Any | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Apply one pure, fail-closed cleanup receipt transition.

    The returned receipt is detached from ``session``. No Executor, filesystem, task
    record, project, or dispatcher conversation is touched by this state machine.
    """
    if type(target_state) is not str or target_state not in CLEANUP_STATES:
        _raise_cleanup_transition(f"invalid target state: {target_state}")
    session_errors = validate_session_snapshot(session)
    if session_errors:
        _raise_cleanup_transition("session receipt is invalid", session_errors)

    stored_receipt = copy.deepcopy(session["cleanup"])
    receipt = read_session_cleanup_receipt(stored_receipt)
    current_state = receipt["state"]
    if current_state in RESOLVED_CLEANUP_STATES or current_state == target_state:
        return stored_receipt
    receipt = _upgrade_cleanup_receipt(receipt)

    now = occurred_at or _utc_now()
    if not _valid_utc_timestamp(now):
        _raise_cleanup_transition("occurred_at must be an ISO-8601 timestamp with timezone")

    blockers = session_cleanup_blockers(
        task_status=task_status,
        lease_state=lease_state,
        report_written=report_written,
        notification_recorded=notification_recorded,
        session=session,
    )
    if target_state == "retained":
        if current_state != "not_requested" or session.get("retain") is not True:
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
    # SESSION-104-001 v4 gate: both official commands must be acknowledged
    # (or officially confirmed) before a Codex cleanup may succeed.  The
    # backend/read/list sides are diagnostics only and desktop_live is
    # not_applicable under the archive-then-delete strategy.
    executor_is_codex = str(session.get("executor") or "").strip().lower() == "codex"
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
            for command in ("archive", "delete")
        ):
            # A Codex adapter supplied no command evidence: fail closed to
            # not_applicable rather than leaving invalid not_requested proof.
            normalized_commands = _empty_cleanup_commands("not_applicable", checked_at=now)
            if is_archive_strategy:
                _raise_cleanup_transition(
                    "Codex cleanup succeeded without both official archive and "
                    "delete command acknowledgements"
                )
        if executor_is_codex and is_archive_strategy:
            archive_status = str(
                (normalized_commands or {}).get("archive", {}).get("status") or ""
            )
            delete_status = str(
                (normalized_commands or {}).get("delete", {}).get("status") or ""
            )
            if archive_status not in {"acknowledged", "confirmed"} or delete_status not in {
                "acknowledged",
                "confirmed",
            }:
                _raise_cleanup_transition(
                    "Codex cleanup succeeded without both official archive and "
                    "delete command acknowledgements"
                )
            normalized_verification = dict(normalized_verification)
            normalized_verification["desktop_live"] = {
                "status": "not_applicable",
                "checked_at": now,
            }
        elif executor_is_codex:
            statuses = {
                normalized_verification[side]["status"]
                for side in CLEANUP_VERIFICATION_SIDES
            }
            if statuses != {"absent"}:
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


def _normalize_resource_limit(executor: str, value: float) -> int | float:
    if not _valid_resource_limit(executor, value):
        raise ABCError(
            "invalid_execution_resource_limit",
            f"Invalid {RESOURCE_KIND_BY_EXECUTOR.get(executor) or 'resource'} limit for {executor}",
            {"executor": executor, "value": value},
        )
    if executor == "hermes":
        return int(value)
    return float(value)


def _valid_resource_limit(executor: str, value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if executor == "hermes":
        return isinstance(value, int) and value > 0
    if executor == "claude":
        return isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0
    return False


def _canonical_claude_project_path(workspace: dict[str, Any]) -> Path:
    agentbc_root = Path(str(workspace.get("agentbc_root") or "")).expanduser()
    task_code = str(workspace.get("task_code") or "").strip()
    iteration = str(workspace.get("iteration") or "").strip()
    task_date = str(workspace.get("task_date") or "").strip()
    if not agentbc_root.is_absolute() or not task_code or not iteration or not task_date:
        raise ABCError(
            "path_plan_missing",
            "Claude session policy requires the canonical task path-plan fields",
        )
    task_id = f"{task_code}-{int(iteration):03d}"
    return (
        agentbc_root
        / "tasks"
        / "artifacts"
        / task_date
        / task_code
        / task_id
        / "claude"
    ).resolve()


def _raise_policy_errors(errors: list[str], key: str) -> None:
    if errors:
        raise ABCError(
            "invalid_execution_policy",
            f"Invalid {key}: {'; '.join(errors)}",
            {"extension": key, "errors": errors},
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
    raise ABCError("invalid_session_cleanup_transition", message, details)


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
