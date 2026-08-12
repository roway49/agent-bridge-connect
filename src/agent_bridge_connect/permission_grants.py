"""Executor-neutral one-shot permission grant contract.

The v1 envelope is deliberately internal.  It records only durable binding,
scope, state, and timestamp data needed to authorize one future executor run;
it never stores the permission prompt, command line, executor output, secrets,
private paths, or session content.
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .protocol import ABCError


PERMISSION_GRANT_EXTENSION_KEY = "agentbc.permission_grant"
PERMISSION_GRANT_VERSION = 1
PERMISSION_GRANT_SCOPE = "next_executor_run"
PERMISSION_GRANT_MAX_USES = 1
PERMISSION_GRANT_STATES = frozenset({"issued", "consumed", "revoked"})

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_REVOCATION_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:password|passwd|token|api[_-]?key|secret|authorization)\s*[:=]"
)
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


def build_permission_grant(
    *,
    executor: str,
    task_id: str,
    input_id: str,
    session_id: str,
    source_run_id: str,
    issued_at: str | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """Build one issued ``safe -> full`` grant for the next executor run."""
    envelope: dict[str, Any] = {
        "version": PERMISSION_GRANT_VERSION,
        "grant_id": grant_id or f"grant-{uuid.uuid4().hex}",
        "transition": {"from": "safe", "to": "full"},
        "binding": {
            "executor": executor,
            "task_id": task_id,
            "input_id": input_id,
            "session_id": session_id,
            "source_run_id": source_run_id,
            "target_run_id": "",
        },
        "scope": {
            "kind": PERMISSION_GRANT_SCOPE,
            "max_uses": PERMISSION_GRANT_MAX_USES,
        },
        "state": {"status": "issued", "uses": 0},
        "audit": {
            "source": "permission_input",
            "issued_at": issued_at or _utc_now(),
            "consumed_at": "",
            "revoked_at": "",
            "revocation_code": "",
        },
    }
    return validate_permission_grant(envelope)


def validate_permission_grant(
    value: Any,
    *,
    executor: str | None = None,
    task_id: str | None = None,
    input_id: str | None = None,
    session_id: str | None = None,
    source_run_id: str | None = None,
    target_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate v1 fail closed and return a lossless defensive copy.

    Unknown additive fields are retained at every object level.  Fields or
    values that could persist sensitive execution/session material are rejected
    even when they are otherwise unknown extensions.
    """
    if not isinstance(value, dict):
        _invalid("permission_grant_invalid", "Permission grant must be an object")
    grant = copy.deepcopy(value)
    version = grant.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        _invalid("permission_grant_version_invalid", "Permission grant version must be an integer")
    if version != PERMISSION_GRANT_VERSION:
        _invalid(
            "permission_grant_version_unsupported",
            f"Unsupported permission grant version: {version}",
        )
    _reject_sensitive_additions(grant)

    _require_identifier(grant.get("grant_id"), "grant_id")
    transition = _require_object(grant, "transition")
    if transition.get("from") != "safe" or transition.get("to") != "full":
        _invalid(
            "permission_grant_transition_invalid",
            "Permission grant v1 supports only safe to full",
        )

    binding = _require_object(grant, "binding")
    for field in ("executor", "task_id", "input_id", "session_id", "source_run_id"):
        _require_identifier(binding.get(field), f"binding.{field}")
    bound_target_run_id = binding.get("target_run_id")
    if (
        not isinstance(bound_target_run_id, str)
        or bound_target_run_id != bound_target_run_id.strip()
    ):
        _invalid(
            "permission_grant_binding_invalid",
            "Permission grant binding.target_run_id must be an empty or opaque identifier",
        )
    if bound_target_run_id:
        _require_identifier(bound_target_run_id, "binding.target_run_id")

    expected_bindings = {
        "executor": executor,
        "task_id": task_id,
        "input_id": input_id,
        "session_id": session_id,
        "source_run_id": source_run_id,
    }
    for field, expected in expected_bindings.items():
        if expected is not None and binding.get(field) != expected:
            _invalid(
                "permission_grant_binding_mismatch",
                f"Permission grant binding.{field} does not match the expected value",
            )

    scope = _require_object(grant, "scope")
    if scope.get("kind") != PERMISSION_GRANT_SCOPE:
        _invalid(
            "permission_grant_scope_invalid",
            f"Permission grant scope must be {PERMISSION_GRANT_SCOPE}",
        )
    max_uses = scope.get("max_uses")
    if isinstance(max_uses, bool) or max_uses != PERMISSION_GRANT_MAX_USES:
        _invalid(
            "permission_grant_scope_invalid",
            f"Permission grant max_uses must be {PERMISSION_GRANT_MAX_USES}",
        )

    state = _require_object(grant, "state")
    status = state.get("status")
    if status not in PERMISSION_GRANT_STATES:
        _invalid("permission_grant_state_invalid", f"Invalid permission grant state: {status}")
    uses = state.get("uses")
    if isinstance(uses, bool) or not isinstance(uses, int) or uses not in {0, 1}:
        _invalid("permission_grant_state_invalid", "Permission grant uses must be 0 or 1")

    audit = _require_object(grant, "audit")
    if audit.get("source") != "permission_input":
        _invalid(
            "permission_grant_audit_invalid",
            "Permission grant audit.source must be permission_input",
        )
    issued_at = _require_timestamp(audit.get("issued_at"), "audit.issued_at")
    consumed_at = _optional_timestamp(audit.get("consumed_at"), "audit.consumed_at")
    revoked_at = _optional_timestamp(audit.get("revoked_at"), "audit.revoked_at")
    revocation_code = audit.get("revocation_code")
    if not isinstance(revocation_code, str):
        _invalid(
            "permission_grant_audit_invalid",
            "Permission grant audit.revocation_code must be a string",
        )

    if status == "issued":
        if (
            uses != 0
            or bound_target_run_id
            or consumed_at
            or revoked_at
            or revocation_code
        ):
            _invalid("permission_grant_state_invalid", "Issued permission grant has inconsistent state")
    elif status == "consumed":
        if (
            uses != 1
            or not bound_target_run_id
            or consumed_at is None
            or revoked_at
            or revocation_code
        ):
            _invalid("permission_grant_state_invalid", "Consumed permission grant has inconsistent state")
    else:
        if revoked_at is None or not _REVOCATION_CODE_RE.fullmatch(revocation_code):
            _invalid("permission_grant_state_invalid", "Revoked permission grant requires a stable revocation code")
        if uses == 0 and (bound_target_run_id or consumed_at is not None):
            _invalid("permission_grant_state_invalid", "Unused revoked permission grant has consumed data")
        if uses == 1 and (not bound_target_run_id or consumed_at is None):
            _invalid("permission_grant_state_invalid", "Consumed revoked permission grant is missing run data")

    if target_run_id is not None and uses == 1 and bound_target_run_id != target_run_id:
        _invalid(
            "permission_grant_binding_mismatch",
            "Permission grant binding.target_run_id does not match the expected value",
        )

    for field, stamp in (("consumed_at", consumed_at), ("revoked_at", revoked_at)):
        if stamp is not None and stamp < issued_at:
            _invalid(
                "permission_grant_audit_invalid",
                f"Permission grant audit.{field} predates issuance",
            )
    if consumed_at is not None and revoked_at is not None and revoked_at < consumed_at:
        _invalid(
            "permission_grant_audit_invalid",
            "Permission grant revocation predates consumption",
        )
    return grant


def permission_grant_from_extensions(
    extensions: dict[str, Any] | None,
    *,
    executor: str | None = None,
    task_id: str | None = None,
    input_id: str | None = None,
    session_id: str | None = None,
    source_run_id: str | None = None,
    target_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Read and validate the optional internal grant extension."""
    values = extensions if isinstance(extensions, dict) else {}
    if PERMISSION_GRANT_EXTENSION_KEY not in values:
        return None
    return validate_permission_grant(
        values[PERMISSION_GRANT_EXTENSION_KEY],
        executor=executor,
        task_id=task_id,
        input_id=input_id,
        session_id=session_id,
        source_run_id=source_run_id,
        target_run_id=target_run_id,
    )


def consume_permission_grant(
    value: Any,
    target_run_id: str,
    *,
    consumed_at: str | None = None,
    executor: str | None = None,
    task_id: str | None = None,
    input_id: str | None = None,
    session_id: str | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    """Consume an issued grant once; same-run retries are idempotent."""
    grant = validate_permission_grant(
        value,
        executor=executor,
        task_id=task_id,
        input_id=input_id,
        session_id=session_id,
        source_run_id=source_run_id,
    )
    _require_identifier(target_run_id, "target_run_id")
    status = grant["state"]["status"]
    bound_run_id = grant["binding"].get("target_run_id")
    if status == "consumed" and bound_run_id == target_run_id:
        return grant
    if status != "issued":
        _invalid(
            "permission_grant_replay",
            "Permission grant is not issued for this executor run",
        )
    grant["binding"]["target_run_id"] = target_run_id
    grant["state"]["status"] = "consumed"
    grant["state"]["uses"] = 1
    grant["audit"]["consumed_at"] = consumed_at or _utc_now()
    return validate_permission_grant(
        grant,
        executor=executor,
        task_id=task_id,
        input_id=input_id,
        session_id=session_id,
        source_run_id=source_run_id,
        target_run_id=target_run_id,
    )


def revoke_permission_grant(
    value: Any,
    revocation_code: str,
    *,
    revoked_at: str | None = None,
) -> dict[str, Any]:
    """Revoke an issued or consumed grant using a non-sensitive reason code."""
    grant = validate_permission_grant(value)
    clean_code = str(revocation_code or "").strip().lower()
    if not _REVOCATION_CODE_RE.fullmatch(clean_code):
        _invalid(
            "permission_grant_revocation_invalid",
            "Permission grant revocation requires a stable lowercase reason code",
        )
    if grant["state"]["status"] == "revoked":
        if grant["audit"]["revocation_code"] == clean_code:
            return grant
        _invalid(
            "permission_grant_replay",
            "Permission grant was already revoked for a different reason",
        )
    grant["state"]["status"] = "revoked"
    grant["audit"]["revoked_at"] = revoked_at or _utc_now()
    grant["audit"]["revocation_code"] = clean_code
    return validate_permission_grant(grant)


def permission_grant_public_projection(value: Any) -> dict[str, Any]:
    """Return the single sanitized view allowed outside internal Core logic."""
    grant = validate_permission_grant(value)
    state = grant["state"]
    audit = grant["audit"]
    return {
        "version": PERMISSION_GRANT_VERSION,
        "temporary": True,
        "active": state["status"] == "issued",
        "source": "permission_input",
        "from_mode": "safe",
        "to_mode": "full",
        "scope": PERMISSION_GRANT_SCOPE,
        "max_uses": PERMISSION_GRANT_MAX_USES,
        "state": state["status"],
        "uses": state["uses"],
        "issued_at": audit["issued_at"],
        "consumed_at": audit["consumed_at"],
        "revoked_at": audit["revoked_at"],
    }


def _require_object(parent: dict[str, Any], field: str) -> dict[str, Any]:
    value = parent.get(field)
    if not isinstance(value, dict):
        _invalid("permission_grant_invalid", f"Permission grant {field} must be an object")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _IDENTIFIER_RE.fullmatch(value)
    ):
        _invalid(
            "permission_grant_binding_invalid",
            f"Permission grant {field} must be a non-empty opaque identifier",
        )
    return value


def _require_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _invalid("permission_grant_audit_invalid", f"Permission grant {field} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _invalid("permission_grant_audit_invalid", f"Permission grant {field} is invalid")
    if parsed.tzinfo is None:
        _invalid("permission_grant_audit_invalid", f"Permission grant {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _require_timestamp(value, field)


def _reject_sensitive_additions(value: Any, *, key_path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in _FORBIDDEN_FIELD_PARTS) or "path" in normalized:
                _invalid(
                    "permission_grant_sensitive_field",
                    f"Permission grant cannot persist sensitive field: {'.'.join((*key_path, key))}",
                )
            _reject_sensitive_additions(item, key_path=(*key_path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_additions(item, key_path=(*key_path, str(index)))
    elif isinstance(value, str):
        clean = value.strip()
        if clean.startswith(("/", "~/")) or _SECRET_ASSIGNMENT_RE.search(clean):
            _invalid(
                "permission_grant_sensitive_field",
                f"Permission grant cannot persist sensitive content at: {'.'.join(key_path)}",
            )


def _invalid(code: str, message: str) -> None:
    raise ABCError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
