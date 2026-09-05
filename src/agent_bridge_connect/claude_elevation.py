"""Redacted lifecycle receipts for Claude's same-session elevation protocol.

The official Claude SDK keeps the blocked tool input in the live callback
future.  AgentBC must therefore persist only the identity and digest of that
input, never the input itself.  This module is deliberately small and
executor-specific: it is not a permission rule store and it cannot mint a
grant or a continuation.
"""

from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, NoReturn

from .protocol import ABCError


CLAUDE_ELEVATION_EXTENSION_KEY = "agentbc.claude_elevation"
CLAUDE_ELEVATION_RECEIPT_VERSION = 1
CLAUDE_ELEVATION_PROTOCOL = "claude.can_use_tool.setMode"
CLAUDE_ELEVATION_MODE_FROM = "default"
CLAUDE_ELEVATION_MODE_TO = "bypassPermissions"
CLAUDE_ELEVATION_DESTINATION = "session"

CLAUDE_ELEVATION_SAFE_DEFAULT = "safe/default"
CLAUDE_ELEVATION_PENDING = "elevation_pending"
CLAUDE_ELEVATION_RESPONSE_READY = "set_mode_response_ready"
CLAUDE_ELEVATION_ACTIVE = "bypassPermissions_active"
CLAUDE_ELEVATION_DENIED = "denied"
CLAUDE_ELEVATION_BLOCKED = "blocked"
CLAUDE_ELEVATION_STATES = frozenset(
    {
        CLAUDE_ELEVATION_SAFE_DEFAULT,
        CLAUDE_ELEVATION_PENDING,
        CLAUDE_ELEVATION_RESPONSE_READY,
        CLAUDE_ELEVATION_ACTIVE,
        CLAUDE_ELEVATION_DENIED,
        CLAUDE_ELEVATION_BLOCKED,
    }
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^fp-[0-9a-f]{40}$")


def stable_input_digest(input_data: Any) -> str:
    """Return a redacted digest of the exact callback input."""
    import json

    payload = json.dumps(
        input_data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_claude_elevation_receipt(
    *,
    task_id: str,
    executor_run_id: str,
    session_id: str,
    request_id: str,
    tool_use_id: str,
    request_fingerprint: str,
    input_fingerprint: str,
    action_fingerprint: str,
    operation: str,
    path_plan_digest: str,
    containment_profile_digest: str,
    native_event: str = "claude_sdk_can_use_tool",
    elevation_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the redacted receipt immediately before the pending transition."""
    now = created_at or _utc_now()
    value: dict[str, Any] = {
        "version": CLAUDE_ELEVATION_RECEIPT_VERSION,
        "elevation_id": elevation_id or f"claude-elev-{uuid.uuid4().hex}",
        "protocol": CLAUDE_ELEVATION_PROTOCOL,
        "mode": {
            "from": CLAUDE_ELEVATION_MODE_FROM,
            "to": CLAUDE_ELEVATION_MODE_TO,
            "destination": CLAUDE_ELEVATION_DESTINATION,
        },
        # The base receipt starts at the frozen safe/default state.  The first
        # native callback advances it to elevation_pending, preserving the
        # complete transition history in the durable projection.
        "state": {"status": CLAUDE_ELEVATION_SAFE_DEFAULT, "error_code": ""},
        "binding": {
            "task_id": str(task_id or "").strip(),
            "executor_run_id": str(executor_run_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "request_id": str(request_id or "").strip(),
            "tool_use_id": str(tool_use_id or "").strip(),
            "request_fingerprint": str(request_fingerprint or "").strip(),
            "input_fingerprint": str(input_fingerprint or "").strip(),
            "action_fingerprint": str(action_fingerprint or "").strip(),
            "operation": str(operation or "").strip(),
        },
        "containment": {
            "path_plan_digest": str(path_plan_digest or "").strip(),
            "profile_digest": str(containment_profile_digest or "").strip(),
            "policy": "runner_pathplan_contained",
        },
        "authority": {
            "executor": "claude",
            "event": str(native_event or "claude_sdk_can_use_tool").strip(),
            "method": "sdk.can_use_tool",
            "response": "PermissionResultAllow",
            "update": "PermissionUpdate.setMode",
        },
        "decision": {"type": "", "source": "", "at": ""},
        "transition_history": [],
        "cardinality": {
            "permission_requests": 1,
            "dialogs": 0,
            "human_decisions": 0,
            "set_mode_updates": 0,
            "protocol_anomalies": 0,
        },
        "created_at": now,
        "updated_at": now,
    }
    return validate_claude_elevation_receipt(value)


def transition_claude_elevation(
    value: Any,
    target_state: str,
    *,
    at: str | None = None,
    decision: str = "",
    source: str = "",
    error_code: str = "",
) -> dict[str, Any]:
    """Advance one receipt without changing its task/session binding."""
    receipt = validate_claude_elevation_receipt(value)
    target = str(target_state or "").strip()
    current = str(receipt["state"].get("status") or "")
    if target not in CLAUDE_ELEVATION_STATES:
        _invalid("claude_elevation_state_invalid", "Unknown Claude elevation state")
    if current == target:
        return receipt
    allowed = {
        CLAUDE_ELEVATION_PENDING: {CLAUDE_ELEVATION_SAFE_DEFAULT},
        CLAUDE_ELEVATION_RESPONSE_READY: {CLAUDE_ELEVATION_PENDING},
        CLAUDE_ELEVATION_ACTIVE: {CLAUDE_ELEVATION_RESPONSE_READY},
        CLAUDE_ELEVATION_DENIED: {CLAUDE_ELEVATION_PENDING},
        CLAUDE_ELEVATION_BLOCKED: {
            CLAUDE_ELEVATION_PENDING,
            CLAUDE_ELEVATION_RESPONSE_READY,
            CLAUDE_ELEVATION_ACTIVE,
            CLAUDE_ELEVATION_DENIED,
        },
    }
    if current != CLAUDE_ELEVATION_SAFE_DEFAULT and target == CLAUDE_ELEVATION_PENDING:
        _invalid("claude_elevation_replay", "Claude elevation request was already created")
    if current not in allowed.get(target, set()):
        _invalid(
            "claude_elevation_state_invalid",
            f"Cannot transition Claude elevation from {current} to {target}",
        )
    stamp = at or _utc_now()
    receipt["transition_history"].append(
        {"from": current, "to": target, "at": stamp}
    )
    receipt["state"] = {"status": target, "error_code": str(error_code or "").strip()}
    if decision:
        receipt["decision"] = {
            "type": str(decision).strip().lower(),
            "source": str(source or "").strip().lower(),
            "at": stamp,
        }
        receipt["cardinality"]["human_decisions"] = 1
    if target == CLAUDE_ELEVATION_RESPONSE_READY:
        receipt["cardinality"]["set_mode_updates"] = 1
    if target == CLAUDE_ELEVATION_BLOCKED:
        receipt["cardinality"]["protocol_anomalies"] = 1
    receipt["updated_at"] = stamp
    return validate_claude_elevation_receipt(receipt)


def record_claude_elevation_dialog(value: Any) -> dict[str, Any]:
    """Reserve the one AgentBC dialog for a pending receipt."""
    receipt = validate_claude_elevation_receipt(value)
    if receipt["cardinality"]["dialogs"] == 1:
        return receipt
    if receipt["state"]["status"] != CLAUDE_ELEVATION_PENDING:
        _invalid("claude_elevation_state_invalid", "Only a pending elevation may reserve a dialog")
    receipt["cardinality"]["dialogs"] = 1
    receipt["updated_at"] = _utc_now()
    return validate_claude_elevation_receipt(receipt)


def claude_elevation_from_extensions(
    extensions: dict[str, Any] | None,
    **binding: Any,
) -> dict[str, Any] | None:
    """Read and validate the optional task receipt without inferring one."""
    values = extensions if isinstance(extensions, dict) else {}
    value = values.get(CLAUDE_ELEVATION_EXTENSION_KEY)
    if value is None:
        return None
    return validate_claude_elevation_receipt(value, **binding)


def claude_elevation_public_projection(value: Any) -> dict[str, Any]:
    """Return only bounded state and digest facts for status/report views."""
    receipt = validate_claude_elevation_receipt(value)
    binding = receipt["binding"]
    return {
        "version": receipt["version"],
        "protocol": receipt["protocol"],
        "state": receipt["state"]["status"],
        "error_code": receipt["state"].get("error_code") or "",
        "task_id": binding["task_id"],
        "executor_run_id": binding["executor_run_id"],
        "session_id": binding["session_id"],
        "request_id": binding["request_id"],
        "tool_use_id": binding["tool_use_id"],
        "request_fingerprint": binding["request_fingerprint"],
        "input_fingerprint": binding["input_fingerprint"],
        "action_fingerprint": binding["action_fingerprint"],
        "path_plan_digest": receipt["containment"]["path_plan_digest"],
        "containment_profile_digest": receipt["containment"]["profile_digest"],
        "cardinality": dict(receipt["cardinality"]),
        "decision": dict(receipt["decision"]),
        "transition_history": [dict(item) for item in receipt["transition_history"]],
        "created_at": receipt["created_at"],
        "updated_at": receipt["updated_at"],
    }


def validate_claude_elevation_receipt(
    value: Any,
    *,
    task_id: str | None = None,
    executor_run_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    tool_use_id: str | None = None,
) -> dict[str, Any]:
    """Validate every persisted binding and reject raw-input additions."""
    if not isinstance(value, dict):
        _invalid("claude_elevation_invalid", "Claude elevation receipt must be an object")
    receipt = copy.deepcopy(value)
    if receipt.get("version") != CLAUDE_ELEVATION_RECEIPT_VERSION:
        _invalid("claude_elevation_version_unsupported", "Unsupported Claude elevation receipt version")
    if receipt.get("protocol") != CLAUDE_ELEVATION_PROTOCOL:
        _invalid("claude_elevation_protocol_invalid", "Claude elevation protocol is invalid")
    mode = receipt.get("mode")
    if not isinstance(mode, dict) or mode != {
        "from": CLAUDE_ELEVATION_MODE_FROM,
        "to": CLAUDE_ELEVATION_MODE_TO,
        "destination": CLAUDE_ELEVATION_DESTINATION,
    }:
        _invalid("claude_elevation_mode_invalid", "Claude elevation mode contract is invalid")
    state = receipt.get("state")
    if not isinstance(state, dict) or state.get("status") not in CLAUDE_ELEVATION_STATES:
        _invalid("claude_elevation_state_invalid", "Claude elevation state is invalid")
    _require_identifier(receipt.get("elevation_id"), "elevation_id")
    binding = _require_object(receipt, "binding")
    for field in (
        "task_id",
        "executor_run_id",
        "session_id",
        "request_id",
        "tool_use_id",
        "request_fingerprint",
        "input_fingerprint",
        "action_fingerprint",
        "operation",
    ):
        _require_identifier(binding.get(field), f"binding.{field}")
    for field, expected in {
        "task_id": task_id,
        "executor_run_id": executor_run_id,
        "session_id": session_id,
        "request_id": request_id,
        "tool_use_id": tool_use_id,
    }.items():
        if expected is not None and str(binding.get(field) or "") != str(expected).strip():
            _invalid("claude_elevation_binding_mismatch", f"binding.{field} does not match the expected value")
    if not _FINGERPRINT_RE.fullmatch(str(binding.get("request_fingerprint") or "")):
        _invalid(
            "claude_elevation_binding_invalid",
            "binding.request_fingerprint must be an AgentBC fingerprint",
        )
    if not _DIGEST_RE.fullmatch(str(binding.get("input_fingerprint") or "")):
        _invalid(
            "claude_elevation_binding_invalid",
            "binding.input_fingerprint must be a sha256 digest",
        )
    if not _FINGERPRINT_RE.fullmatch(str(binding.get("action_fingerprint") or "")):
        _invalid(
            "claude_elevation_binding_invalid",
            "binding.action_fingerprint must be an AgentBC fingerprint",
        )
    containment = _require_object(receipt, "containment")
    if containment.get("policy") != "runner_pathplan_contained":
        _invalid("claude_elevation_containment_invalid", "Claude elevation containment policy is invalid")
    for field in ("path_plan_digest", "profile_digest"):
        if not _DIGEST_RE.fullmatch(str(containment.get(field) or "")):
            _invalid("claude_elevation_containment_invalid", f"containment.{field} must be a sha256 digest")
    authority = _require_object(receipt, "authority")
    if authority.get("executor") != "claude" or authority.get("event") != "claude_sdk_can_use_tool":
        _invalid("claude_elevation_authority_invalid", "Claude elevation authority is invalid")
    if authority.get("method") != "sdk.can_use_tool" or authority.get("response") != "PermissionResultAllow":
        _invalid("claude_elevation_authority_invalid", "Claude elevation native response authority is invalid")
    if authority.get("update") != "PermissionUpdate.setMode":
        _invalid("claude_elevation_authority_invalid", "Claude elevation update authority is invalid")
    history = receipt.get("transition_history")
    if not isinstance(history, list) or len(history) > 4:
        _invalid("claude_elevation_transition_invalid", "Claude elevation transition history is invalid")
    previous = CLAUDE_ELEVATION_SAFE_DEFAULT
    for item in history:
        next_state = item.get("to") if isinstance(item, dict) else None
        allowed_next = {
            CLAUDE_ELEVATION_SAFE_DEFAULT: {CLAUDE_ELEVATION_PENDING},
            CLAUDE_ELEVATION_PENDING: {
                CLAUDE_ELEVATION_RESPONSE_READY,
                CLAUDE_ELEVATION_DENIED,
                CLAUDE_ELEVATION_BLOCKED,
            },
            CLAUDE_ELEVATION_RESPONSE_READY: {
                CLAUDE_ELEVATION_ACTIVE,
                CLAUDE_ELEVATION_BLOCKED,
            },
            CLAUDE_ELEVATION_ACTIVE: {CLAUDE_ELEVATION_BLOCKED},
            CLAUDE_ELEVATION_DENIED: {CLAUDE_ELEVATION_BLOCKED},
        }
        if (
            not isinstance(item, dict)
            or item.get("from") != previous
            or next_state not in allowed_next.get(previous, set())
        ):
            _invalid("claude_elevation_transition_invalid", "Claude elevation transition history is not monotonic")
        previous = str(item["to"])
    if history and previous != state.get("status"):
        _invalid("claude_elevation_transition_invalid", "Claude elevation state does not match transition history")
    if not history and state.get("status") != CLAUDE_ELEVATION_SAFE_DEFAULT:
        _invalid("claude_elevation_transition_invalid", "A non-base Claude elevation requires a transition")
    decision = _require_object(receipt, "decision")
    for field in ("type", "source", "at"):
        if not isinstance(decision.get(field), str):
            _invalid("claude_elevation_decision_invalid", f"decision.{field} must be a string")
    decision_type = str(decision.get("type") or "")
    if decision_type and decision_type not in {"approve", "deny"}:
        _invalid("claude_elevation_decision_invalid", "Claude elevation decision must be approve or deny")
    if state.get("status") in {
        CLAUDE_ELEVATION_RESPONSE_READY,
        CLAUDE_ELEVATION_ACTIVE,
    } and decision_type != "approve":
        _invalid("claude_elevation_state_invalid", "Claude elevation response/activation requires approve")
    if state.get("status") == CLAUDE_ELEVATION_DENIED and decision_type != "deny":
        _invalid("claude_elevation_state_invalid", "Denied Claude elevation requires deny")
    if decision_type and not _IDENTIFIER_RE.fullmatch(str(decision.get("source") or "")):
        _invalid("claude_elevation_decision_invalid", "Claude elevation decision source is invalid")
    cardinality = _require_object(receipt, "cardinality")
    for field in (
        "permission_requests",
        "dialogs",
        "human_decisions",
        "set_mode_updates",
        "protocol_anomalies",
    ):
        count = cardinality.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > 1:
            _invalid("claude_elevation_cardinality_invalid", f"cardinality.{field} must be 0 or 1")
    if cardinality.get("permission_requests") != 1:
        _invalid("claude_elevation_cardinality_invalid", "Exactly one Claude permission request is required")
    if cardinality.get("human_decisions") != (1 if decision_type else 0):
        _invalid("claude_elevation_cardinality_invalid", "Claude elevation decision cardinality is inconsistent")
    expected_set_mode_updates = (
        1
        if state.get("status") in {
            CLAUDE_ELEVATION_RESPONSE_READY,
            CLAUDE_ELEVATION_ACTIVE,
        }
        or (state.get("status") == CLAUDE_ELEVATION_BLOCKED and decision_type == "approve")
        else 0
    )
    if cardinality.get("set_mode_updates") != expected_set_mode_updates:
        _invalid("claude_elevation_cardinality_invalid", "Claude elevation setMode cardinality is inconsistent")
    if cardinality.get("protocol_anomalies") != (1 if state.get("status") == CLAUDE_ELEVATION_BLOCKED else 0):
        _invalid("claude_elevation_cardinality_invalid", "Claude elevation anomaly cardinality is inconsistent")
    _require_timestamp(receipt.get("created_at"), "created_at")
    _require_timestamp(receipt.get("updated_at"), "updated_at")
    return receipt


def _require_object(parent: dict[str, Any], field: str) -> dict[str, Any]:
    value = parent.get(field)
    if not isinstance(value, dict):
        _invalid("claude_elevation_invalid", f"Claude elevation {field} must be an object")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _IDENTIFIER_RE.fullmatch(value):
        _invalid("claude_elevation_binding_invalid", f"Claude elevation {field} must be an opaque identifier")
    return value


def _require_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _invalid("claude_elevation_audit_invalid", f"Claude elevation {field} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _invalid("claude_elevation_audit_invalid", f"Claude elevation {field} is invalid")
    if parsed.tzinfo is None:
        _invalid("claude_elevation_audit_invalid", f"Claude elevation {field} must include a timezone")


def _invalid(code: str, message: str) -> NoReturn:
    raise ABCError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CLAUDE_ELEVATION_ACTIVE",
    "CLAUDE_ELEVATION_BLOCKED",
    "CLAUDE_ELEVATION_DENIED",
    "CLAUDE_ELEVATION_DESTINATION",
    "CLAUDE_ELEVATION_EXTENSION_KEY",
    "CLAUDE_ELEVATION_MODE_FROM",
    "CLAUDE_ELEVATION_MODE_TO",
    "CLAUDE_ELEVATION_PENDING",
    "CLAUDE_ELEVATION_RESPONSE_READY",
    "CLAUDE_ELEVATION_PROTOCOL",
    "CLAUDE_ELEVATION_RECEIPT_VERSION",
    "CLAUDE_ELEVATION_SAFE_DEFAULT",
    "CLAUDE_ELEVATION_STATES",
    "build_claude_elevation_receipt",
    "claude_elevation_from_extensions",
    "claude_elevation_public_projection",
    "record_claude_elevation_dialog",
    "stable_input_digest",
    "transition_claude_elevation",
    "validate_claude_elevation_receipt",
]
