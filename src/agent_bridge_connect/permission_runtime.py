"""Authoritative runtime permission capability receipt (PERM-104-002).

``agentbc.permission_runtime`` v1 unifies the three legitimate ``full``
paths - explicit task base, one-shot permission grant, and inherited task
snapshot - into one Runner-owned, task-scoped capability record.  Only this
record may prove that a declared ``full`` base actually became effective for
one executor run; a selector value, a flag on a command line, or an agent
self-report is never enough.

Contract invariants (fail closed):

* the escalation domain hierarchy is fixed in this module and projected in
  that exact order: ``executor_policy``, ``agentbc_policy``,
  ``runner_pathplan``, ``host_containment``, ``linked_worktree_metadata``;
* lifecycle states are strictly ordered: ``prepared -> authorized ->
  activated -> verified``; any stable error code moves the record to
  ``blocked`` (a terminal state for that run);
* a grant is consumed only after the record is ``authorized``; the record
  becomes ``activated`` only after the executor enters the same host
  profile; it becomes ``verified`` only after the structured action
  succeeds;
* the block ledger persists each trusted block decision together with its
  execution result and domain-change facts.  When the same
  task/session/action/fingerprint/domain/profile reappears after an
  approved but ineffective escalation, the step converges to
  ``permission_escalation_ineffective`` with zero new permission inputs,
  grants, workers, continuations, deadlines or notifications;
* a concrete ``full`` base never asks for ``full`` again and never emits
  ``permission_mode_unsupported`` or ``permission_resume_session_unavailable``;
* status/report/doctor projections expose only mode, source, hierarchy
  status, stable error codes, timestamps and sanitized digests - never raw
  argv, tokens, private paths or binding identifiers.

The envelope deliberately stores no raw paths: the frozen PathPlan and the
host containment profile enter only as SHA-256 digests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from .protocol import ABCError

PERMISSION_RUNTIME_EXTENSION_KEY = "agentbc.permission_runtime"
PERMISSION_RUNTIME_VERSION = 1
PERMISSION_RUNTIME_MODE = "full"

PERMISSION_RUNTIME_STATES = frozenset(
    {"prepared", "authorized", "activated", "verified", "blocked"}
)
PERMISSION_RUNTIME_SOURCES = frozenset(
    {"explicit_task", "one_shot_permission_grant", "inherited_task"}
)
PERMISSION_RUNTIME_DOMAINS = (
    "executor_policy",
    "agentbc_policy",
    "runner_pathplan",
    "host_containment",
    "linked_worktree_metadata",
)
HIERARCHY_STATUSES = frozenset({"ok", "blocked", "not_applicable"})

# Stable PERM-104-002 error codes.
PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE = "permission_runtime_capability_unavailable"
PERMISSION_TRANSPORT_UNSUPPORTED = "permission_transport_unsupported"
PERMISSION_ESCALATION_INEFFECTIVE = "permission_escalation_ineffective"
PERMISSION_ACTION_ALREADY_BLOCKED = "permission_action_already_blocked"
LINKED_WORKTREE_CAPABILITY_INVALID = "linked_worktree_capability_invalid"
HOST_CONTAINMENT_UNLIFTABLE = "host_containment_unliftable"
PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE = "permission_block_evidence_unavailable"

PERMISSION_RUNTIME_BLOCK_CODES = frozenset(
    {
        PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE,
        PERMISSION_TRANSPORT_UNSUPPORTED,
        PERMISSION_ESCALATION_INEFFECTIVE,
        PERMISSION_ACTION_ALREADY_BLOCKED,
        LINKED_WORKTREE_CAPABILITY_INVALID,
        HOST_CONTAINMENT_UNLIFTABLE,
        PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
    }
)

# Legacy compatibility envelope: a blocked runtime never fabricates the old
# recovery chain; the compatibility code is only carried as a projection
# marker for historical readers.
PERMISSION_RUNTIME_COMPATIBILITY_CODE = "permission_resume_session_unavailable"
PERMISSION_RUNTIME_COMPATIBILITY_SUPPRESSED = "permission_mode_unsupported"

BLOCK_LEDGER_VERSION = 1
BLOCK_LEDGER_FILENAME = "permission_block_ledger.json"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,95}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^fp-[0-9a-f]{40,64}$")
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
        "config",
        "settings",
    }
)


def build_permission_runtime_record(
    *,
    task_id: str,
    chain_head_id: str,
    executor: str,
    executor_run_id: str,
    session_id: str,
    permission_source: str,
    path_plan_digest: str,
    host_profile_digest: str,
    action_fingerprint: str = "",
    operation: str = "",
    runtime_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one ``prepared`` runtime capability record, fail closed."""
    normalized_source = str(permission_source or "").strip()
    if normalized_source not in PERMISSION_RUNTIME_SOURCES:
        raise ABCError(
            PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE,
            "A full runtime capability requires an explicit, granted, or inherited permission source.",
            {"permission_source": normalized_source},
        )
    envelope: dict[str, Any] = {
        "version": PERMISSION_RUNTIME_VERSION,
        "runtime_id": runtime_id or f"rt-{uuid.uuid4().hex}",
        "mode": PERMISSION_RUNTIME_MODE,
        "state": {
            "status": "prepared",
            "block_code": "",
            "block_domain": "",
        },
        "binding": {
            "task_id": task_id,
            "chain_head_id": chain_head_id,
            "executor": executor,
            "executor_run_id": executor_run_id,
            "session_id": session_id,
            "permission_source": normalized_source,
            "request_id": "",
            "grant_id": "",
        },
        "scope": {
            "path_plan_digest": path_plan_digest,
            "host_profile_digest": host_profile_digest,
        },
        "hierarchy": {domain: "not_applicable" for domain in PERMISSION_RUNTIME_DOMAINS},
        "action": {
            "fingerprint": action_fingerprint,
            "operation": operation,
        },
        "audit": {
            "created_at": created_at or _utc_now(),
            "authorized_at": "",
            "activated_at": "",
            "verified_at": "",
            "blocked_at": "",
        },
    }
    return validate_permission_runtime_record(envelope)


def validate_permission_runtime_record(
    value: Any,
    *,
    task_id: str | None = None,
    chain_head_id: str | None = None,
    executor: str | None = None,
    executor_run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Validate the v1 envelope fail closed and return a defensive copy."""
    if not isinstance(value, dict):
        _invalid("permission_runtime_invalid", "Permission runtime record must be an object")
    record = copy.deepcopy(value)
    version = record.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        _invalid(
            "permission_runtime_version_invalid",
            "Permission runtime version must be an integer",
        )
    if version != PERMISSION_RUNTIME_VERSION:
        _invalid(
            "permission_runtime_version_unsupported",
            f"Unsupported permission runtime version: {version}",
        )
    _reject_sensitive_additions(record)
    if record.get("mode") != PERMISSION_RUNTIME_MODE:
        _invalid(
            "permission_runtime_mode_invalid",
            "Permission runtime record mode must be full",
        )

    _require_identifier(record.get("runtime_id"), "runtime_id")
    state = _require_object(record, "state")
    status = str(state.get("status") or "").strip()
    if status not in PERMISSION_RUNTIME_STATES:
        _invalid(
            "permission_runtime_state_invalid",
            f"Invalid permission runtime state: {status}",
        )
    block_code = str(state.get("block_code") or "").strip()
    block_domain = str(state.get("block_domain") or "").strip()
    if status == "blocked":
        if block_code not in PERMISSION_RUNTIME_BLOCK_CODES:
            _invalid(
                "permission_runtime_state_invalid",
                "Blocked permission runtime requires a stable block code",
            )
        if block_domain not in PERMISSION_RUNTIME_DOMAINS:
            _invalid(
                "permission_runtime_state_invalid",
                "Blocked permission runtime requires a hierarchy domain",
            )
    else:
        if block_code or block_domain:
            _invalid(
                "permission_runtime_state_invalid",
                "Non-blocked permission runtime cannot carry a block code",
            )

    binding = _require_object(record, "binding")
    for field in (
        "task_id",
        "chain_head_id",
        "executor",
        "executor_run_id",
    ):
        _require_identifier(binding.get(field), f"binding.{field}")
    session_id_value = str(binding.get("session_id") or "").strip()
    if session_id_value:
        _require_identifier(session_id_value, "binding.session_id")
    # The official session may not exist yet when the Runner prepares and
    # activates the record (the executor establishes it during the run); the
    # authoritative session binding lives in the approval receipt, the
    # one-shot grant and the block ledger, which all require it.
    source = str(binding.get("permission_source") or "").strip()
    if source not in PERMISSION_RUNTIME_SOURCES:
        _invalid(
            "permission_runtime_source_invalid",
            f"Invalid permission runtime source: {source}",
        )
    expected = {
        "task_id": task_id,
        "chain_head_id": chain_head_id,
        "executor": executor,
        "executor_run_id": executor_run_id,
        "session_id": session_id,
    }
    for field, expected_value in expected.items():
        if expected_value is not None and binding.get(field) != expected_value:
            _invalid(
                "permission_runtime_binding_mismatch",
                f"Permission runtime binding.{field} does not match the expected value",
            )
    for optional in ("request_id", "grant_id"):
        value_text = str(binding.get(optional) or "").strip()
        if value_text and not _IDENTIFIER_RE.fullmatch(value_text):
            _invalid(
                "permission_runtime_binding_invalid",
                f"Permission runtime binding.{optional} must be an opaque identifier",
            )

    scope = _require_object(record, "scope")
    path_plan_digest = str(scope.get("path_plan_digest") or "").strip()
    host_profile_digest = str(scope.get("host_profile_digest") or "").strip()
    if not _DIGEST_RE.fullmatch(path_plan_digest) or not _DIGEST_RE.fullmatch(
        host_profile_digest
    ):
        _invalid(
            "permission_runtime_scope_invalid",
            "Permission runtime scope requires sha256 digests",
        )

    hierarchy = _require_object(record, "hierarchy")
    if set(hierarchy) != set(PERMISSION_RUNTIME_DOMAINS):
        _invalid(
            "permission_runtime_hierarchy_invalid",
            "Permission runtime hierarchy must contain exactly the fixed domains",
        )
    for domain in PERMISSION_RUNTIME_DOMAINS:
        domain_status = str(hierarchy.get(domain) or "").strip()
        if domain_status not in HIERARCHY_STATUSES:
            _invalid(
                "permission_runtime_hierarchy_invalid",
                f"Permission runtime hierarchy.{domain} has an invalid status",
            )

    action = _require_object(record, "action")
    fingerprint = str(action.get("fingerprint") or "").strip()
    if fingerprint and not _FINGERPRINT_RE.fullmatch(fingerprint):
        _invalid(
            "permission_runtime_action_invalid",
            "Permission runtime action fingerprint is invalid",
        )
    operation = str(action.get("operation") or "").strip()
    if operation and not _OPERATION_RE.fullmatch(operation):
        _invalid(
            "permission_runtime_action_invalid",
            "Permission runtime action operation is invalid",
        )

    audit = _require_object(record, "audit")
    created_at = _require_timestamp(audit.get("created_at"), "audit.created_at")
    stamps = {
        "authorized_at": _optional_timestamp(audit.get("authorized_at"), "audit.authorized_at"),
        "activated_at": _optional_timestamp(audit.get("activated_at"), "audit.activated_at"),
        "verified_at": _optional_timestamp(audit.get("verified_at"), "audit.verified_at"),
        "blocked_at": _optional_timestamp(audit.get("blocked_at"), "audit.blocked_at"),
    }
    expected_state: dict[str, Any] = {
        # value True = the stamp must be absent in this state.
        "prepared": {"authorized_at": True, "activated_at": True, "verified_at": True, "blocked_at": True},
        "authorized": {"authorized_at": False, "activated_at": True, "verified_at": True, "blocked_at": True},
        "activated": {"activated_at": False, "verified_at": True, "blocked_at": True},
        "verified": {"verified_at": False, "blocked_at": True},
        "blocked": {"blocked_at": False},
    }
    for stamp_field, must_be_absent in expected_state[status].items():
        if must_be_absent and stamps[stamp_field] is not None:
            _invalid(
                "permission_runtime_state_invalid",
                f"Permission runtime {status} state cannot carry audit.{stamp_field}",
            )
    if status == "authorized" and not binding.get("request_id"):
        _invalid(
            "permission_runtime_state_invalid",
            "Authorized permission runtime requires the bound request id",
        )
    for stamp_field, stamp in stamps.items():
        if stamp is not None and stamp < created_at:
            _invalid(
                "permission_runtime_audit_invalid",
                f"Permission runtime audit.{stamp_field} predates creation",
            )
    ordered = [
        stamps["authorized_at"],
        stamps["activated_at"],
        stamps["verified_at"],
        stamps["blocked_at"],
    ]
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier is not None and later is not None and later < earlier:
            _invalid(
                "permission_runtime_audit_invalid",
                "Permission runtime audit stamps are out of order",
            )
    return record


def permission_runtime_from_extensions(
    extensions: dict[str, Any] | None,
    *,
    task_id: str | None = None,
    chain_head_id: str | None = None,
    executor: str | None = None,
    executor_run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Read and validate the optional internal runtime extension."""
    values = extensions if isinstance(extensions, dict) else {}
    if PERMISSION_RUNTIME_EXTENSION_KEY not in values:
        return None
    return validate_permission_runtime_record(
        values[PERMISSION_RUNTIME_EXTENSION_KEY],
        task_id=task_id,
        chain_head_id=chain_head_id,
        executor=executor,
        executor_run_id=executor_run_id,
        session_id=session_id,
    )


def authorize_permission_runtime_record(
    value: Any,
    *,
    decision: str,
    request_id: str,
    grant_id: str = "",
    authorized_at: str | None = None,
) -> dict[str, Any]:
    """Move a prepared record to ``authorized`` for an approved decision.

    The one-shot grant is consumed only after this transition; a Deny or any
    non-approve decision never authorizes the runtime capability.
    """
    record = validate_permission_runtime_record(value)
    if record["state"]["status"] != "prepared":
        _invalid(
            "permission_runtime_state_invalid",
            "Only a prepared permission runtime can be authorized",
        )
    if str(decision or "").strip().lower() != "approve":
        _invalid(
            "permission_runtime_state_invalid",
            "Permission runtime authorization requires an approve decision",
        )
    _require_identifier(request_id, "request_id")
    record["state"]["status"] = "authorized"
    record["binding"]["request_id"] = request_id
    record["binding"]["grant_id"] = str(grant_id or "").strip()
    record["audit"]["authorized_at"] = authorized_at or _utc_now()
    return validate_permission_runtime_record(record)


def activate_permission_runtime_record(
    value: Any,
    *,
    host_profile_digest: str,
    activated_at: str | None = None,
) -> dict[str, Any]:
    """Move an authorized record to ``activated`` inside the same host profile.

    ``full`` becomes effective only after the executor enters the same
    containment profile that was digested at preparation time.
    """
    record = validate_permission_runtime_record(value)
    if record["state"]["status"] != "authorized":
        _invalid(
            "permission_runtime_state_invalid",
            "Only an authorized permission runtime can be activated",
        )
    if not _DIGEST_RE.fullmatch(str(host_profile_digest or "").strip()):
        _invalid(
            "permission_runtime_scope_invalid",
            "Activation requires the host profile sha256 digest",
        )
    if record["scope"]["host_profile_digest"] != host_profile_digest:
        raise ABCError(
            "permission_runtime_profile_mismatch",
            "Permission runtime cannot activate outside its prepared host profile.",
        )
    record["state"]["status"] = "activated"
    record["audit"]["activated_at"] = activated_at or _utc_now()
    return validate_permission_runtime_record(record)


def verify_permission_runtime_record(
    value: Any,
    *,
    session_id: str | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """Move an activated record to ``verified`` after structured success.

    PERM-104-002 review fix: verification is the production point where the
    record binds the executor's real official session id (from the validated
    session receipt).  A record without a session binding can never be
    verified, so ``full`` is only ever proven effective for a run that the
    executor itself confirmed.
    """
    record = validate_permission_runtime_record(value)
    if record["state"]["status"] != "activated":
        _invalid(
            "permission_runtime_state_invalid",
            "Only an activated permission runtime can be verified",
        )
    if session_id is not None:
        normalized = str(session_id or "").strip()
        _require_identifier(normalized, "binding.session_id")
        record["binding"]["session_id"] = normalized
    elif not str(record["binding"].get("session_id") or "").strip():
        _invalid(
            "permission_runtime_state_invalid",
            "Verified permission runtime requires the bound official session id",
        )
    record["state"]["status"] = "verified"
    record["audit"]["verified_at"] = verified_at or _utc_now()
    return validate_permission_runtime_record(record)


def block_permission_runtime_record(
    value: Any,
    *,
    code: str,
    domain: str,
    blocked_at: str | None = None,
) -> dict[str, Any]:
    """Move any non-terminal record to ``blocked`` with a stable code."""
    record = validate_permission_runtime_record(value)
    if record["state"]["status"] == "blocked":
        return record
    if record["state"]["status"] == "verified":
        _invalid(
            "permission_runtime_state_invalid",
            "A verified permission runtime cannot be blocked",
        )
    if code not in PERMISSION_RUNTIME_BLOCK_CODES:
        _invalid(
            "permission_runtime_state_invalid",
            f"Unknown stable block code: {code}",
        )
    if domain not in PERMISSION_RUNTIME_DOMAINS:
        _invalid(
            "permission_runtime_state_invalid",
            f"Unknown block domain: {domain}",
        )
    record["state"]["status"] = "blocked"
    record["state"]["block_code"] = code
    record["state"]["block_domain"] = domain
    record["audit"]["blocked_at"] = blocked_at or _utc_now()
    return validate_permission_runtime_record(record)


def runtime_source_for_permission(permission: dict[str, Any]) -> str | None:
    """Map a frozen permission record to one of the three ``full`` sources.

    Returns ``None`` when the resolved base is not concrete ``full`` (no
    runtime capability is required).  Any other full-looking source fails
    closed with ``permission_runtime_capability_unavailable``.
    """
    effective = str(permission.get("effective_mode") or "").strip().lower()
    if effective != PERMISSION_RUNTIME_MODE:
        return None
    source = str(permission.get("selection_source") or "").strip()
    if source in PERMISSION_RUNTIME_SOURCES:
        return source
    raise ABCError(
        PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE,
        "Concrete full requires an explicit, granted, or inherited permission source.",
        {"selection_source": source, "effective_mode": effective},
    )


def path_plan_digest(workspace: dict[str, Any] | None) -> str:
    """Return the sanitized SHA-256 digest of the frozen PathPlan structure.

    Only structural facts are digested; the raw absolute paths are never
    persisted by the runtime envelope.
    """
    values = workspace if isinstance(workspace, dict) else {}
    plan = {
        "customer_dir": bool(values.get("customer_dir")),
        "project_root": _canonical_text(values.get("project_root") or values.get("root")),
        "artifact_root": _canonical_text(
            values.get("artifact_root") or values.get("artifacts_dir")
        ),
        "report_root": _canonical_text(values.get("report_root") or values.get("output_dir")),
        "agentbc_root": _canonical_text(values.get("agentbc_root")),
        "executor_project_root": _canonical_text(values.get("executor_project_root")),
        "customer_path": _canonical_text(values.get("customer_path")),
    }
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def host_profile_digest(facts: dict[str, Any] | None = None) -> str:
    """Return the SHA-256 digest of the host containment profile.

    Defaults to stable platform facts; tests may pin explicit facts.  The
    digest never includes user names, private paths or tokens.
    """
    if facts is None:
        from .seatbelt import seatbelt_available

        facts = {
            "platform": sys.platform,
            "seatbelt_available": seatbelt_available(),
        }
    payload = json.dumps(
        {str(key): str(value) for key, value in (facts or {}).items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def action_fingerprint(*, executor: str, session_id: str, operation: str) -> str:
    """Content-derived stable fingerprint for one blocked action."""
    payload = json.dumps(
        {
            "executor": str(executor or "").strip().lower(),
            "session_id": str(session_id or "").strip(),
            "operation": str(operation or "").strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"fp-{digest[:40]}"


def block_fingerprint(
    *,
    task_id: str,
    session_id: str,
    action_fingerprint_value: str,
    domain: str,
    profile_digest: str,
) -> str:
    """Stable fingerprint for one trusted block decision."""
    payload = json.dumps(
        {
            "task_id": str(task_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "action_fingerprint": str(action_fingerprint_value or "").strip(),
            "domain": str(domain or "").strip(),
            "profile_digest": str(profile_digest or "").strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"fp-{digest[:40]}"


def classify_block_domain(evidence: dict[str, Any] | None) -> str:
    """Classify one trusted block event into the fixed hierarchy domain.

    Raises ``permission_block_evidence_unavailable`` when no supported
    source domain can be proven - stderr, natural language and exit codes
    are never accepted as evidence.
    """
    values = evidence if isinstance(evidence, dict) else {}
    for domain in PERMISSION_RUNTIME_DOMAINS:
        if values.get(domain) is True:
            return domain
    source = str(values.get("source") or "").strip().lower()
    if source in PERMISSION_RUNTIME_DOMAINS:
        return source
    raise ABCError(
        PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
        "Permission block evidence must identify one supported escalation domain.",
        {"evidence_sources": sorted(set(values) or set())},
    )


def permission_runtime_public_projection(value: Any) -> dict[str, Any]:
    """Return the single sanitized view allowed outside internal Core logic.

    Projects only mode, permission source, hierarchy statuses, stable error
    codes, timestamps and the two digests.  Binding identifiers and raw
    action material are never projected.
    """
    record = validate_permission_runtime_record(value)
    state = record["state"]
    projection: dict[str, Any] = {
        "version": PERMISSION_RUNTIME_VERSION,
        "mode": PERMISSION_RUNTIME_MODE,
        "permission_source": record["binding"]["permission_source"],
        "state": state["status"],
        "hierarchy": dict(record["hierarchy"]),
        "path_plan_digest": record["scope"]["path_plan_digest"],
        "host_profile_digest": record["scope"]["host_profile_digest"],
        "created_at": record["audit"]["created_at"],
    }
    if state["status"] == "blocked":
        projection["block_code"] = state["block_code"]
        projection["block_domain"] = state["block_domain"]
    for stamp in ("authorized_at", "activated_at", "verified_at", "blocked_at"):
        value_text = record["audit"].get(stamp)
        if value_text:
            projection[stamp] = value_text
    return projection


# --------------------------------------------------------------------------
# Block ledger: convergence of identical approved-but-ineffective escalations
# --------------------------------------------------------------------------


def block_ledger_path(control_root: str | Path) -> Path:
    """Return the exact ledger path inside one task control root."""
    return Path(control_root).expanduser().resolve() / BLOCK_LEDGER_FILENAME


def load_block_ledger(control_root: str | Path) -> dict[str, Any]:
    """Load the versioned ledger; a missing or empty file is an empty ledger."""
    path = block_ledger_path(control_root)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {"version": BLOCK_LEDGER_VERSION, "entries": {}}
    if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
        return {"version": BLOCK_LEDGER_VERSION, "entries": {}}
    entries = {
        str(key): entry
        for key, entry in value["entries"].items()
        if isinstance(entry, dict) and str(entry.get("decision") or "").strip()
    }
    return {"version": BLOCK_LEDGER_VERSION, "entries": entries}


def save_block_ledger(control_root: str | Path, ledger: dict[str, Any]) -> None:
    """Persist the ledger atomically with a 0600 mode."""
    path = block_ledger_path(control_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def remember_block_outcome(
    ledger: dict[str, Any],
    *,
    fingerprint: str,
    task_id: str,
    session_id: str,
    action_fingerprint_value: str,
    domain: str,
    profile_digest: str,
    decision: str,
    execution_result: str,
    domain_changed: bool,
    code: str = PERMISSION_ESCALATION_INEFFECTIVE,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Persist one trusted block decision and its execution result."""
    if not _FINGERPRINT_RE.fullmatch(str(fingerprint or "")):
        _invalid("permission_runtime_ledger_invalid", "Block ledger requires a stable fingerprint")
    if decision not in {"approve", "deny"}:
        _invalid("permission_runtime_ledger_invalid", "Block ledger decision must be approve or deny")
    if execution_result not in {"", "blocked", "succeeded"}:
        _invalid(
            "permission_runtime_ledger_invalid",
            "Block ledger execution result must be pending, blocked or succeeded",
        )
    if domain not in PERMISSION_RUNTIME_DOMAINS:
        _invalid("permission_runtime_ledger_invalid", "Block ledger domain is invalid")
    entries = ledger.setdefault("entries", {})
    entries[fingerprint] = {
        "task_id": str(task_id or "").strip(),
        "session_id": str(session_id or "").strip(),
        "action_fingerprint": str(action_fingerprint_value or "").strip(),
        "domain": domain,
        "profile_digest": str(profile_digest or "").strip(),
        "decision": decision,
        "execution_result": execution_result,
        "domain_changed": bool(domain_changed),
        "code": str(code or "").strip(),
        "updated_at": updated_at or _utc_now(),
    }
    ledger["version"] = BLOCK_LEDGER_VERSION
    return ledger


def replay_blocked_after_approval(
    ledger: dict[str, Any],
    *,
    fingerprint: str,
    task_id: str,
    session_id: str,
    action_fingerprint_value: str,
    domain: str,
    profile_digest: str,
) -> dict[str, Any] | None:
    """Return the prior outcome when the identical block reappears.

    Convergence requires: same task, session, action fingerprint, escalation
    domain and host profile; the prior decision was an approve; and the prior
    execution result was still ``blocked``.  When matched, the caller must
    converge to ``permission_escalation_ineffective`` with zero new
    permission inputs, grants, workers, continuations, deadlines or
    notifications.
    """
    entries = ledger.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    entry = entries.get(str(fingerprint or ""))
    if not isinstance(entry, dict):
        return None
    if (
        entry.get("task_id") == str(task_id or "").strip()
        and entry.get("session_id") == str(session_id or "").strip()
        and entry.get("action_fingerprint") == str(action_fingerprint_value or "").strip()
        and entry.get("domain") == domain
        and entry.get("profile_digest") == str(profile_digest or "").strip()
        and entry.get("decision") == "approve"
        and entry.get("execution_result") != "succeeded"
    ):
        return dict(entry)
    return None


def record_block_decision(
    control_root: str | Path,
    *,
    fingerprint: str,
    task_id: str,
    session_id: str,
    action_fingerprint_value: str,
    domain: str,
    profile_digest: str,
    decision: str,
) -> None:
    """Record one trusted approval decision in the block ledger (pending result)."""
    # The public control protocols use ``accept``/``decline`` while the
    # convergence ledger deliberately stores the executor-neutral
    # ``approve``/``deny`` vocabulary.  Normalize only at this boundary so
    # wire responses retain their official shape and ledger validation stays
    # strict everywhere else.
    normalized_decision = {
        "accept": "approve",
        "decline": "deny",
        "approve": "approve",
        "deny": "deny",
    }.get(str(decision or "").strip().lower(), "")
    if not normalized_decision:
        _invalid(
            "permission_runtime_ledger_invalid",
            "Block ledger decision must be accept, decline, approve or deny",
        )
    ledger = load_block_ledger(control_root)
    remember_block_outcome(
        ledger,
        fingerprint=fingerprint,
        task_id=task_id,
        session_id=session_id,
        action_fingerprint_value=action_fingerprint_value,
        domain=domain,
        profile_digest=profile_digest,
        decision=normalized_decision,
        execution_result="",
        domain_changed=False,
        code=PERMISSION_ESCALATION_INEFFECTIVE,
    )
    save_block_ledger(control_root, ledger)


def converge_approved_block(
    control_root: str | Path,
    *,
    task_id: str,
    session_id: str,
    executor: str,
    operation: str,
    domain: str,
    profile_digest: str,
    action_fingerprint_value: str = "",
) -> str | None:
    """Converge one approved-but-ineffective escalation to a stable code.

    Returns ``permission_escalation_ineffective`` when the identical
    task/session/action/domain/profile reappears after an approve whose
    execution did not succeed; returns ``None`` otherwise.  On convergence
    the ledger entry is updated and persisted; the caller must then fail the
    blocked step with zero new permission inputs, grants, workers,
    continuations, deadlines or notifications.
    """
    action_fp = str(action_fingerprint_value or "").strip() or action_fingerprint(
        executor=executor,
        session_id=session_id,
        operation=operation,
    )
    ledger = load_block_ledger(control_root)
    fp = block_fingerprint(
        task_id=task_id,
        session_id=session_id,
        action_fingerprint_value=action_fp,
        domain=domain,
        profile_digest=profile_digest,
    )
    replay = replay_blocked_after_approval(
        ledger,
        fingerprint=fp,
        task_id=task_id,
        session_id=session_id,
        action_fingerprint_value=action_fp,
        domain=domain,
        profile_digest=profile_digest,
    )
    if replay is None:
        return None
    remember_block_outcome(
        ledger,
        fingerprint=fp,
        task_id=task_id,
        session_id=session_id,
        action_fingerprint_value=action_fp,
        domain=domain,
        profile_digest=profile_digest,
        decision="approve",
        execution_result="blocked",
        domain_changed=False,
        code=PERMISSION_ESCALATION_INEFFECTIVE,
    )
    save_block_ledger(control_root, ledger)
    return PERMISSION_ESCALATION_INEFFECTIVE


def supersede_block(
    ledger: dict[str, Any],
    fingerprint: str,
    new_domain: str,
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Mark a ledger entry superseded when trusted transport proves the
    escalation domain changed; only then may a new request be created."""
    entries = ledger.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    entry = entries.get(str(fingerprint or ""))
    if not isinstance(entry, dict):
        return ledger
    entry["domain_changed"] = True
    entry["superseded_domain"] = new_domain
    entry["updated_at"] = updated_at or _utc_now()
    return ledger


def block_ledger_public_projection(ledger: dict[str, Any]) -> dict[str, Any]:
    """Bounded public projection: counts and latest facts only."""
    entries = ledger.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    latest = ""
    for entry in entries.values():
        stamp = str(entry.get("updated_at") or "")
        if stamp > latest:
            latest = stamp
    return {
        "version": BLOCK_LEDGER_VERSION,
        "entry_count": len(entries),
        "latest_updated_at": latest,
    }


def _require_object(parent: dict[str, Any], field: str) -> dict[str, Any]:
    value = parent.get(field)
    if not isinstance(value, dict):
        _invalid("permission_runtime_invalid", f"Permission runtime {field} must be an object")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _IDENTIFIER_RE.fullmatch(value)
    ):
        _invalid(
            "permission_runtime_binding_invalid",
            f"Permission runtime {field} must be a non-empty opaque identifier",
        )
    return value


def _require_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _invalid("permission_runtime_audit_invalid", f"Permission runtime {field} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _invalid("permission_runtime_audit_invalid", f"Permission runtime {field} is invalid")
        raise AssertionError("unreachable")  # pragma: no cover
    if parsed.tzinfo is None:
        _invalid(
            "permission_runtime_audit_invalid",
            f"Permission runtime {field} must include a timezone",
        )
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _require_timestamp(value, field)


def _canonical_text(value: Any) -> str:
    return str(value or "").strip()


def _reject_sensitive_additions(value: Any, *, key_path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in _FORBIDDEN_FIELD_PARTS) or (
                "path" in normalized
                and not normalized.endswith("digest")
                and normalized != "runnerpathplan"
            ):
                _invalid(
                    "permission_runtime_sensitive_field",
                    f"Permission runtime cannot persist sensitive field: {'.'.join((*key_path, key))}",
                )
            _reject_sensitive_additions(item, key_path=(*key_path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_additions(item, key_path=(*key_path, str(index)))
    elif isinstance(value, str):
        clean = value.strip()
        if clean.startswith(("/", "~/")) or _SECRET_ASSIGNMENT_RE.search(clean):
            _invalid(
                "permission_runtime_sensitive_field",
                f"Permission runtime cannot persist sensitive content at: {'.'.join(key_path)}",
            )


def _invalid(code: str, message: str) -> NoReturn:
    raise ABCError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "BLOCK_LEDGER_FILENAME",
    "BLOCK_LEDGER_VERSION",
    "HOST_CONTAINMENT_UNLIFTABLE",
    "HIERARCHY_STATUSES",
    "LINKED_WORKTREE_CAPABILITY_INVALID",
    "PERMISSION_ACTION_ALREADY_BLOCKED",
    "PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE",
    "PERMISSION_ESCALATION_INEFFECTIVE",
    "PERMISSION_RUNTIME_BLOCK_CODES",
    "PERMISSION_RUNTIME_CAPABILITY_UNAVAILABLE",
    "PERMISSION_RUNTIME_COMPATIBILITY_CODE",
    "PERMISSION_RUNTIME_COMPATIBILITY_SUPPRESSED",
    "PERMISSION_RUNTIME_DOMAINS",
    "PERMISSION_RUNTIME_EXTENSION_KEY",
    "PERMISSION_RUNTIME_MODE",
    "PERMISSION_RUNTIME_SOURCES",
    "PERMISSION_RUNTIME_STATES",
    "PERMISSION_RUNTIME_VERSION",
    "PERMISSION_TRANSPORT_UNSUPPORTED",
    "action_fingerprint",
    "activate_permission_runtime_record",
    "authorize_permission_runtime_record",
    "block_fingerprint",
    "block_ledger_path",
    "block_ledger_public_projection",
    "block_permission_runtime_record",
    "build_permission_runtime_record",
    "classify_block_domain",
    "converge_approved_block",
    "host_profile_digest",
    "load_block_ledger",
    "path_plan_digest",
    "permission_runtime_from_extensions",
    "permission_runtime_public_projection",
    "record_block_decision",
    "remember_block_outcome",
    "replay_blocked_after_approval",
    "runtime_source_for_permission",
    "save_block_ledger",
    "supersede_block",
    "validate_permission_runtime_record",
    "verify_permission_runtime_record",
]
