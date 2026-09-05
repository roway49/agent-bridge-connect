"""Task-scoped contained-full permission elevation.

This module is the durable v1 capability which replaces the old one-shot
``agentbc.permission_grant`` write path for new approvals.  It intentionally
contains only binding facts and digests: the customer path, argv, sandbox
profile, executor output, and credentials never enter the envelope.

The state machine is monotonic::

    prepared -> approved -> active -> verified
             \-> denied
    any non-verified state -> blocked

The exact Task ID is the capability boundary.  Retry, recovery and
reassignment may reuse the record for that Task ID; a handoff creates a new
Task ID and therefore has no record to inherit.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from .protocol import ABCError


PERMISSION_ELEVATION_EXTENSION_KEY = "agentbc.permission_elevation"
PERMISSION_PROTOCOL_EXTENSION_KEY = "agentbc.permission_protocol"
PERMISSION_PROTOCOL_VERSION = 3
PERMISSION_PROTOCOL_SCOPE = "task_elevation"
PERMISSION_ELEVATION_VERSION = 1
PERMISSION_ELEVATION_MODE = "contained_full"
PERMISSION_ELEVATION_SOURCE = "task_elevation"
PERMISSION_ELEVATION_STATES = frozenset(
    {"prepared", "approved", "active", "verified", "denied", "blocked"}
)
PERMISSION_ELEVATION_DECISIONS = frozenset({"approve_full", "deny"})
PERMISSION_ELEVATION_BLOCK_CODES = frozenset(
    {
        "permission_escalation_ineffective",
        "permission_transport_lost",
        "permission_protocol_unavailable",
        "permission_preflight_failed",
        "permission_elevation_replay",
        "permission_elevation_binding_mismatch",
        "permission_elevation_ineffective",
        "host_containment_unliftable",
        "unknown_action_outcome",
    }
)
PERMISSION_ELEVATION_FILENAME = "permission_elevation.json"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_NATIVE_EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_RE = re.compile(r"^[^\x00-\x1f]{1,120}$")
_FORBIDDEN_FIELD_PARTS = frozenset(
    {
        "prompt",
        "command",
        "argv",
        "output",
        "stdout",
        "stderr",
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "database",
        "dbpath",
        "sessioncontent",
        "conversation",
        "message",
        "flags",
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:password|passwd|token|api[_-]?key|secret|authorization)\s*[:=]"
)


def build_permission_elevation(
    *,
    task_id: str,
    path_plan_digest: str,
    executor: str,
    executor_run_id: str,
    session_id: str,
    request_id: str,
    request_fingerprint: str,
    containment_profile_digest: str = "",
    profile_digest: str = "",
    operation: str = "",
    native_event: str = "",
    tool_call_id: str = "",
    action_fingerprint: str = "",
    authority: dict[str, Any] | None = None,
    source: str = PERMISSION_ELEVATION_SOURCE,
    elevation_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a pending task elevation bound to one request and PathPlan."""
    clean_profile = str(containment_profile_digest or profile_digest or "").strip()
    authority_value = dict(authority or {})
    clean_executor = str(executor or "").strip().lower()
    envelope: dict[str, Any] = {
        "version": PERMISSION_ELEVATION_VERSION,
        "elevation_id": elevation_id or f"elev-{uuid.uuid4().hex}",
        "mode": PERMISSION_ELEVATION_MODE,
        "source": str(source or PERMISSION_ELEVATION_SOURCE).strip(),
        "state": {"status": "prepared", "block_code": ""},
        "binding": {
            "task_id": task_id,
            "path_plan_digest": path_plan_digest,
            "executor": clean_executor,
            "executor_run_id": executor_run_id,
            "session_id": session_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
        },
        "provenance": {
            "native_event": str(native_event or "native_structured_permission_event").strip(),
            "tool_call_id": str(tool_call_id or request_id).strip(),
            "action_fingerprint": str(action_fingerprint or "").strip(),
        },
        "authority": {
            "executor": clean_executor,
            "protocol": str(
                authority_value.get("protocol") or "agentbc.native"
            ).strip(),
            "protocol_version": authority_value.get("protocol_version", 1),
            "method": str(
                authority_value.get("method") or "requestApproval"
            ).strip(),
        },
        "containment": {
            "mode": PERMISSION_ELEVATION_MODE,
            "path_plan_digest": path_plan_digest,
            "profile_digest": clean_profile,
            "policy": "runner_pathplan_contained",
        },
        "decision": {"type": "", "source": "", "at": ""},
        "continuation": {
            "count": 0,
            "executor_run_id": "",
            "session_id": "",
        },
        "cardinality": {
            "permission_requests": 1,
            "notifications": 0,
            "human_decisions": 0,
            "elevation_receipts": 1,
            "full_continuations": 0,
        },
        "operation": str(operation or "").strip(),
        "created_at": created_at or _utc_now(),
    }
    return validate_permission_elevation(envelope)


def build_permission_elevation_record(**kwargs: Any) -> dict[str, Any]:
    """Explicit record-named alias used by schema and migration callers."""
    return build_permission_elevation(**kwargs)


def validate_permission_elevation(
    value: Any,
    *,
    task_id: str | None = None,
    path_plan_digest: str | None = None,
    executor: str | None = None,
    executor_run_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    request_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Validate the v1 envelope and all exact binding facts."""
    if not isinstance(value, dict):
        _invalid("permission_elevation_invalid", "Permission elevation must be an object")
    record = copy.deepcopy(value)
    if record.get("version") != PERMISSION_ELEVATION_VERSION:
        _invalid(
            "permission_elevation_version_unsupported",
            f"Unsupported permission elevation version: {record.get('version')}",
        )
    _reject_sensitive_additions(record)
    _require_identifier(record.get("elevation_id"), "elevation_id")
    if record.get("mode") != PERMISSION_ELEVATION_MODE:
        _invalid("permission_elevation_mode_invalid", "Permission elevation mode must be contained_full")
    if record.get("source") != PERMISSION_ELEVATION_SOURCE:
        _invalid("permission_elevation_source_invalid", "Permission elevation source must be task_elevation")

    state = record.get("state")
    if not isinstance(state, dict) or state.get("status") not in PERMISSION_ELEVATION_STATES:
        _invalid("permission_elevation_state_invalid", "Permission elevation state is invalid")
    status = str(state["status"])
    block_code = str(state.get("block_code") or "")
    if status == "blocked":
        if block_code not in PERMISSION_ELEVATION_BLOCK_CODES:
            _invalid("permission_elevation_state_invalid", "Blocked elevation requires a stable error code")
    elif block_code:
        _invalid("permission_elevation_state_invalid", "Only blocked elevation may carry block_code")

    binding = _require_object(record, "binding")
    for field in (
        "task_id",
        "executor",
        "executor_run_id",
        "session_id",
        "request_id",
        "request_fingerprint",
    ):
        _require_identifier(binding.get(field), f"binding.{field}")
    for digest_field in ("path_plan_digest",):
        if not _DIGEST_RE.fullmatch(str(binding.get(digest_field) or "")):
            _invalid("permission_elevation_binding_invalid", f"binding.{digest_field} must be a sha256 digest")
    expected = {
        "task_id": task_id,
        "path_plan_digest": path_plan_digest,
        "executor": str(executor or "").strip().lower() if executor is not None else None,
        "executor_run_id": executor_run_id,
        "session_id": session_id,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
    }
    for field, expected_value in expected.items():
        if expected_value is not None and str(binding.get(field) or "") != str(expected_value).strip():
            _invalid("permission_elevation_binding_mismatch", f"binding.{field} does not match the expected value")

    provenance = _require_object(record, "provenance")
    _require_native_event(
        provenance.get("native_event"),
        "provenance.native_event",
    )
    _require_identifier(
        provenance.get("tool_call_id"),
        "provenance.tool_call_id",
    )
    action_fingerprint_value = str(provenance.get("action_fingerprint") or "")
    if action_fingerprint_value and not _IDENTIFIER_RE.fullmatch(action_fingerprint_value):
        _invalid("permission_elevation_provenance_invalid", "provenance.action_fingerprint is invalid")

    authority = _require_object(record, "authority")
    if str(authority.get("executor") or "").strip().lower() != str(binding.get("executor") or "").strip().lower():
        _invalid("permission_elevation_authority_invalid", "authority.executor must match binding.executor")
    if not _IDENTIFIER_RE.fullmatch(str(authority.get("protocol") or "")):
        _invalid("permission_elevation_authority_invalid", "authority.protocol is required")
    if not _OPERATION_RE.fullmatch(str(authority.get("method") or "")):
        _invalid("permission_elevation_authority_invalid", "authority.method is required")
    protocol_version = authority.get("protocol_version")
    if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
        _invalid("permission_elevation_authority_invalid", "authority.protocol_version must be an integer")

    containment = _require_object(record, "containment")
    if containment.get("mode") != PERMISSION_ELEVATION_MODE:
        _invalid("permission_elevation_containment_invalid", "containment.mode must be contained_full")
    if containment.get("policy") != "runner_pathplan_contained":
        _invalid("permission_elevation_containment_invalid", "containment policy must be Runner PathPlan containment")
    if containment.get("path_plan_digest") != binding.get("path_plan_digest"):
        _invalid("permission_elevation_containment_invalid", "containment PathPlan digest is not bound")
    profile_digest = str(containment.get("profile_digest") or "")
    if not _DIGEST_RE.fullmatch(profile_digest):
        _invalid("permission_elevation_containment_invalid", "containment.profile_digest must be a sha256 digest")

    operation = str(record.get("operation") or "")
    if operation and not _OPERATION_RE.fullmatch(operation):
        _invalid("permission_elevation_operation_invalid", "operation is invalid")
    decision = _require_object(record, "decision")
    for field in ("type", "source", "at"):
        if not isinstance(decision.get(field), str):
            _invalid("permission_elevation_decision_invalid", f"decision.{field} must be a string")
    decision_type = str(decision.get("type") or "").strip().lower()
    decision_source = str(decision.get("source") or "").strip().lower()
    decision_at = str(decision.get("at") or "").strip()
    if decision_type and decision_type not in PERMISSION_ELEVATION_DECISIONS:
        _invalid("permission_elevation_decision_invalid", f"Invalid elevation decision: {decision_type}")
    if decision_type and not decision_source:
        _invalid("permission_elevation_decision_invalid", "A decision requires its source")
    if decision_source and not _IDENTIFIER_RE.fullmatch(decision_source):
        _invalid("permission_elevation_decision_invalid", "Decision source is invalid")
    created_at = _require_timestamp(record.get("created_at"), "created_at")
    if decision_at:
        if _require_timestamp(decision_at, "decision.at") < created_at:
            _invalid("permission_elevation_decision_invalid", "Decision predates elevation creation")

    if status == "prepared" and (decision_type or decision_source or decision_at):
        _invalid("permission_elevation_state_invalid", "Prepared elevation cannot carry a decision")
    if status in {"approved", "active", "verified"} and decision_type != "approve_full":
        _invalid("permission_elevation_state_invalid", "Active elevation requires approve_full")
    if status == "denied" and decision_type != "deny":
        _invalid("permission_elevation_state_invalid", "Denied elevation requires deny")
    if status == "blocked" and not decision_type:
        _invalid("permission_elevation_state_invalid", "Blocked elevation must retain its decision history")

    continuation = _require_object(record, "continuation")
    count = continuation.get("count")
    if isinstance(count, bool) or count not in {0, 1}:
        _invalid("permission_elevation_continuation_invalid", "Continuation count must be 0 or 1")
    for field in ("executor_run_id", "session_id"):
        value_text = str(continuation.get(field) or "")
        if value_text:
            _require_identifier(value_text, f"continuation.{field}")
    if count == 0 and (
        str(continuation.get("executor_run_id") or "")
        or str(continuation.get("session_id") or "")
    ):
        _invalid("permission_elevation_continuation_invalid", "Zero continuations cannot bind a run")
    if status in {"active", "verified"} and count != 1:
        _invalid("permission_elevation_continuation_invalid", "Active elevation requires one full continuation")

    cardinality = _require_object(record, "cardinality")
    for field in (
        "permission_requests",
        "notifications",
        "human_decisions",
        "elevation_receipts",
        "full_continuations",
    ):
        value_int = cardinality.get(field)
        if isinstance(value_int, bool) or not isinstance(value_int, int) or value_int < 0 or value_int > 1:
            _invalid("permission_elevation_cardinality_invalid", f"cardinality.{field} must be 0 or 1")
    if cardinality.get("permission_requests") != 1 or cardinality.get("elevation_receipts") != 1:
        _invalid("permission_elevation_cardinality_invalid", "Exactly one request and elevation receipt are required")
    if cardinality.get("human_decisions") != (1 if decision_type else 0):
        _invalid("permission_elevation_cardinality_invalid", "Human-decision cardinality is inconsistent")
    if cardinality.get("full_continuations") != count:
        _invalid("permission_elevation_cardinality_invalid", "Continuation cardinality is inconsistent")
    return record


def validate_permission_elevation_record(value: Any, **kwargs: Any) -> dict[str, Any]:
    """Explicit record-named validator alias."""
    return validate_permission_elevation(value, **kwargs)


def permission_elevation_from_extensions(
    extensions: dict[str, Any] | None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Read the optional task elevation extension without inferring one."""
    values = extensions if isinstance(extensions, dict) else {}
    if PERMISSION_ELEVATION_EXTENSION_KEY not in values:
        return None
    return validate_permission_elevation(values[PERMISSION_ELEVATION_EXTENSION_KEY], **kwargs)


def task_elevation_protocol_enabled(extensions: dict[str, Any] | None) -> bool:
    """Return whether a newly-created task opted into the v3 elevation path.

    The marker is deliberately mechanical and contains no executor/version
    allowlist. Hand-built historical packets without it remain readable by
    the v1/v2 compatibility adapters, while normal TaskService-created tasks
    use the v3 cutover.
    """
    values = extensions if isinstance(extensions, dict) else {}
    marker = values.get(PERMISSION_PROTOCOL_EXTENSION_KEY)
    return (
        isinstance(marker, dict)
        and marker.get("version") == PERMISSION_PROTOCOL_VERSION
        and marker.get("scope") == PERMISSION_PROTOCOL_SCOPE
        and marker.get("mode") == PERMISSION_ELEVATION_MODE
    )


def full_capability_preflight(
    task_packet: dict[str, Any] | None,
    *,
    executor: str,
    executable: str | Path | None = None,
) -> dict[str, Any]:
    """Run the no-UI structural/full-capability gate for one native block.

    The result is intentionally a bounded receipt: only pass/fail, executor,
    mode, and frozen digests cross the adapter boundary. If an executable is
    supplied, its documented full capability is checked mechanically before a
    request can be shown to a user.
    """
    from .path_model import validate_path_plan_workspace
    from .permission_modes import assert_executor_permission_supported
    from .permission_runtime import host_profile_digest, path_plan_digest

    packet = task_packet if isinstance(task_packet, dict) else {}
    workspace = packet.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    try:
        validate_path_plan_workspace(workspace)
        if executable is not None:
            assert_executor_permission_supported(executor, "full", executable)
        return {
            "ok": True,
            "status": "passed",
            "mode": PERMISSION_ELEVATION_MODE,
            "executor": str(executor or "").strip().lower(),
            "path_plan_digest": path_plan_digest(workspace),
            "containment_profile_digest": host_profile_digest(),
        }
    except Exception as exc:  # noqa: BLE001 - preflight is a fail-closed boundary.
        return {
            "ok": False,
            "status": "failed",
            "mode": PERMISSION_ELEVATION_MODE,
            "executor": str(executor or "").strip().lower(),
            "code": str(getattr(exc, "code", "permission_preflight_failed")),
        }


def record_permission_elevation_decision(
    value: Any,
    decision: str,
    *,
    source: str,
    decided_at: str | None = None,
    **binding: Any,
) -> dict[str, Any]:
    """Persist exactly one human decision, with idempotent identical replay."""
    record = validate_permission_elevation(value, **binding)
    decision_type = str(decision or "").strip().lower()
    decision_source = str(source or "").strip().lower()
    if decision_type not in PERMISSION_ELEVATION_DECISIONS:
        _invalid("permission_elevation_decision_invalid", f"Invalid elevation decision: {decision}")
    if not _IDENTIFIER_RE.fullmatch(decision_source):
        _invalid("permission_elevation_decision_invalid", "Decision source must be an opaque identifier")
    current_type = str(record["decision"].get("type") or "")
    if current_type:
        if current_type == decision_type and record["decision"].get("source") == decision_source:
            return record
        _invalid("permission_elevation_replay", "Elevation was already answered differently")
    stamp = decided_at or _utc_now()
    record["decision"] = {"type": decision_type, "source": decision_source, "at": stamp}
    record["state"]["status"] = "approved" if decision_type == "approve_full" else "denied"
    record["cardinality"]["human_decisions"] = 1
    return validate_permission_elevation(record)


def authorize_permission_elevation(
    value: Any,
    *,
    source: str,
    authorized_at: str | None = None,
    **binding: Any,
) -> dict[str, Any]:
    """Authorize the task elevation using the v3 approve_full decision."""
    return record_permission_elevation_decision(
        value,
        "approve_full",
        source=source,
        decided_at=authorized_at,
        **binding,
    )


def record_permission_elevation_notification(value: Any) -> dict[str, Any]:
    """Record the one user notification/dialog reservation."""
    record = validate_permission_elevation(value)
    if record["cardinality"]["notifications"] == 1:
        return record
    record["cardinality"]["notifications"] = 1
    return validate_permission_elevation(record)


def record_permission_elevation_continuation(
    value: Any,
    *,
    executor_run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Bind one full continuation to the authoritative Runner receipt."""
    record = validate_permission_elevation(value)
    if record["decision"]["type"] != "approve_full":
        _invalid("permission_elevation_state_invalid", "Only approve_full may continue")
    run_value = _require_identifier(executor_run_id, "continuation.executor_run_id")
    session_value = _require_identifier(session_id, "continuation.session_id")
    continuation = record["continuation"]
    if continuation["count"] == 1:
        if continuation.get("executor_run_id") == run_value and continuation.get("session_id") == session_value:
            return record
        _invalid("permission_elevation_replay", "A different full continuation was already recorded")
    record["state"]["status"] = "active"
    continuation.update({"count": 1, "executor_run_id": run_value, "session_id": session_value})
    record["cardinality"]["full_continuations"] = 1
    return validate_permission_elevation(record)


def activate_permission_elevation(
    value: Any,
    *,
    executor_run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Activate only after the contained continuation is authoritatively bound."""
    record = record_permission_elevation_continuation(
        value,
        executor_run_id=executor_run_id,
        session_id=session_id,
    )
    return record


def verify_permission_elevation(
    value: Any,
    *,
    executor_run_id: str | None = None,
    session_id: str | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """Verify only from the same contained run/session receipt."""
    record = validate_permission_elevation(value)
    if record["state"]["status"] == "verified":
        if (
            executor_run_id is None
            or record["continuation"].get("executor_run_id") == str(executor_run_id).strip()
        ) and (
            session_id is None
            or record["continuation"].get("session_id") == str(session_id).strip()
        ):
            return record
        _invalid("permission_elevation_binding_mismatch", "Verified elevation receipt belongs to another run/session")
    if record["state"]["status"] != "active":
        _invalid("permission_elevation_state_invalid", "Only active elevation can be verified")
    if executor_run_id is not None and record["continuation"].get("executor_run_id") != str(executor_run_id).strip():
        _invalid("permission_elevation_binding_mismatch", "Verification run does not match continuation")
    if session_id is not None and record["continuation"].get("session_id") != str(session_id).strip():
        _invalid("permission_elevation_binding_mismatch", "Verification session does not match continuation")
    record["state"]["status"] = "verified"
    record["verified_at"] = verified_at or _utc_now()
    return validate_permission_elevation(record)


def block_permission_elevation(
    value: Any,
    *,
    code: str,
    blocked_at: str | None = None,
) -> dict[str, Any]:
    """Fail closed without replaying or creating another permission input."""
    record = validate_permission_elevation(value)
    if record["state"]["status"] == "blocked":
        return record
    if code not in PERMISSION_ELEVATION_BLOCK_CODES:
        _invalid("permission_elevation_state_invalid", f"Unknown elevation block code: {code}")
    if record["state"]["status"] == "verified":
        _invalid("permission_elevation_state_invalid", "A verified elevation cannot be blocked")
    record["state"].update({"status": "blocked", "block_code": code})
    record["blocked_at"] = blocked_at or _utc_now()
    return validate_permission_elevation(record)


def permission_elevation_public_projection(value: Any) -> dict[str, Any]:
    """Return safe mode/source/state facts for status and reports."""
    record = validate_permission_elevation(value)
    projection: dict[str, Any] = {
        "version": PERMISSION_ELEVATION_VERSION,
        "mode": PERMISSION_ELEVATION_MODE,
        "source": PERMISSION_ELEVATION_SOURCE,
        "state": record["state"]["status"],
        "path_plan_digest": record["binding"]["path_plan_digest"],
        "containment_profile_digest": record["containment"]["profile_digest"],
        "cardinality": dict(record["cardinality"]),
        "created_at": record["created_at"],
    }
    if record["decision"]["type"]:
        projection.update(
            {
                "decision": record["decision"]["type"],
                "decision_source": record["decision"]["source"],
                "decided_at": record["decision"]["at"],
            }
        )
    if record["state"].get("block_code"):
        projection["error_code"] = record["state"]["block_code"]
    return projection


class PermissionElevationStore:
    """Small atomic persistence helper for restart/replay tests and adapters."""

    def __init__(self, root: str | Path):
        candidate = Path(root).expanduser()
        self.path = candidate if candidate.suffix == ".json" else candidate / PERMISSION_ELEVATION_FILENAME

    def load(self, **binding: Any) -> dict[str, Any] | None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ABCError("permission_elevation_persistence_invalid", "Permission elevation persistence is unreadable") from exc
        return validate_permission_elevation(value, **binding)

    def save(self, value: Any) -> dict[str, Any]:
        record = validate_permission_elevation(value)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ABCError("permission_elevation_persistence_failed", "Permission elevation could not be persisted") from exc
        return record

    def update(self, value: Any) -> dict[str, Any]:
        return self.save(value)


def _require_object(parent: dict[str, Any], field: str) -> dict[str, Any]:
    value = parent.get(field)
    if not isinstance(value, dict):
        _invalid("permission_elevation_invalid", f"Permission elevation {field} must be an object")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _IDENTIFIER_RE.fullmatch(value):
        _invalid("permission_elevation_binding_invalid", f"Permission elevation {field} must be an opaque identifier")
    return value


def _require_native_event(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _NATIVE_EVENT_RE.fullmatch(value)
    ):
        _invalid(
            "permission_elevation_binding_invalid",
            f"Permission elevation {field} must be a native event identifier",
        )
    return value


def _require_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _invalid("permission_elevation_audit_invalid", f"Permission elevation {field} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _invalid("permission_elevation_audit_invalid", f"Permission elevation {field} is invalid")
    if parsed.tzinfo is None:
        _invalid("permission_elevation_audit_invalid", f"Permission elevation {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _reject_sensitive_additions(value: Any, *, key_path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in _FORBIDDEN_FIELD_PARTS) or (
                "path" in normalized
                and normalized not in {"pathplandigest", "profiledigest"}
            ):
                _invalid(
                    "permission_elevation_sensitive_field",
                    f"Permission elevation cannot persist sensitive field: {'.'.join((*key_path, key))}",
                )
            _reject_sensitive_additions(item, key_path=(*key_path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_additions(item, key_path=(*key_path, str(index)))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("/", "~/")) or _SECRET_ASSIGNMENT_RE.search(text):
            _invalid(
                "permission_elevation_sensitive_field",
                f"Permission elevation cannot persist sensitive content at: {'.'.join(key_path)}",
            )


def _invalid(code: str, message: str) -> NoReturn:
    raise ABCError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "PERMISSION_ELEVATION_BLOCK_CODES",
    "PERMISSION_ELEVATION_DECISIONS",
    "PERMISSION_ELEVATION_EXTENSION_KEY",
    "PERMISSION_ELEVATION_FILENAME",
    "PERMISSION_ELEVATION_MODE",
    "PERMISSION_ELEVATION_SOURCE",
    "PERMISSION_ELEVATION_STATES",
    "PERMISSION_ELEVATION_VERSION",
    "PERMISSION_PROTOCOL_EXTENSION_KEY",
    "PERMISSION_PROTOCOL_SCOPE",
    "PERMISSION_PROTOCOL_VERSION",
    "PermissionElevationStore",
    "activate_permission_elevation",
    "authorize_permission_elevation",
    "block_permission_elevation",
    "build_permission_elevation",
    "build_permission_elevation_record",
    "permission_elevation_from_extensions",
    "permission_elevation_public_projection",
    "full_capability_preflight",
    "task_elevation_protocol_enabled",
    "record_permission_elevation_continuation",
    "record_permission_elevation_decision",
    "record_permission_elevation_notification",
    "validate_permission_elevation",
    "validate_permission_elevation_record",
    "verify_permission_elevation",
]
