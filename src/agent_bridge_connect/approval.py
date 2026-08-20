"""Executor-neutral structured approval receipt contract (``agentbc.approval`` v1).

The v1 envelope is the durable artifact Core persists for one native permission
request.  It binds the request to the task, the executor run, the official
executor session, and a stable request fingerprint, and it records the exact
single-action scope and Core-generated bounded summary.  It never stores the
permission prompt, the native tool input, command line, executor output,
secrets, private paths, or session content.

Approval decisions are recorded on the same receipt that created the wait, so
Approve / Deny / close / timeout always reply to the same native request and
keep an auditable ``decision.source``.  Unlike the legacy one-shot grant
envelope (:mod:`permission_grants`), an approval receipt never issues a
``safe -> full`` upgrade and never changes the task ``effective_mode``.
"""

from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .protocol import ABCError


APPROVAL_EXTENSION_KEY = "agentbc.approval"
APPROVAL_VERSION = 1
APPROVAL_SCOPE = "single_action"
APPROVAL_KIND = "permission"
APPROVAL_STATES = frozenset({"pending", "answered"})
APPROVAL_DECISION_TYPES = frozenset({"approve", "deny"})
APPROVAL_DECISION_SOURCES = frozenset(
    {"user", "timeout", "dialog_closed", "close", "stale", "crash", "fail_closed"}
)
APPROVAL_SUMMARY_LIMIT = 120
APPROVAL_REASON_SUMMARY_LIMIT = 120
APPROVAL_REASON_DETAIL_LIMIT = 2000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
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
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:password|passwd|token|api[_-]?key|secret|authorization)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
# A credential label immediately followed by whitespace then an opaque-looking
# value (e.g. ``token abc123``, ``Bearer abc123``, ``api key hunter2``) in any
# case.  These space-separated forms are not caught by the assignment regexes,
# so without a dedicated check ``sanitize_reason_detail`` would persist the
# real value verbatim.  A value is treated as credential-shaped when it is
# quoted, contains a digit, or contains a non-alphanumeric character (dash,
# underscore, dot, slash, ...).  Ordinary prose such as ``token is required``,
# ``access token for the api``, ``tokenization`` or ``api key rotation`` never
# matches: the ``(?<![A-Za-z0-9])``/``(?![A-Za-z0-9])`` boundaries exclude label
# substrings of larger unrelated words and the ``(?!...)`` lookahead excludes
# common English continuations.
_SECRET_SPACE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:access\s+token|api\s*[-_]?\s*key|bearer|password|passwd|"
    r"token|secret|credential|authorization)"
    r"(?![A-Za-z0-9])\s+(?!the\b|of\b|is\b|are\b|was\b|were\b|be\b|been\b|"
    r"for\b|to\b|in\b|on\b|at\b|by\b|from\b|with\b|without\b|and\b|or\b|"
    r"as\b|not\b|only\b|also\b|will\b|would\b|can\b|could\b|should\b|"
    r"must\b|may\b|might\b|using\b|used\b|use\b|required\b|needed\b|"
    r"header\b|value\b|policy\b|name\b|file\b|path\b|settings\b|rotation\b|"
    r"management\b|store\b|stored\b|pair\b|pairs\b|endpoint\b|service\b|"
    r"configuration\b|provider\b)"
    r"(?:\"[^\"]*\"|'[^']*'|"
    r"(?=[^\s,;]*(?:\d|[^A-Za-z0-9\s,;]))[^\s,;]+|"
    r"(?=[^\s,;]{16,})[^\s,;]+)"
)
# ``Bearer`` is an HTTP auth scheme whose payload is always a credential, so a
# ``Bearer`` value is treated as sensitive even when it is a short
# alphanumeric-only opaque string (e.g. a JWT header fragment).  Known prose
# continuations such as ``Bearer token`` / ``Bearer auth`` stay allowed.
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])bearer(?![A-Za-z0-9])\s+"
    r"(?!token\b|tokens\b|auth\b|authn\b|authentication\b|scheme\b|"
    r"credentials\b|credential\b|header\b|type\b|the\b|of\b|is\b|for\b|"
    r"to\b|in\b|on\b|at\b|by\b|with\b|without\b|and\b|or\b|as\b|not\b|"
    r"using\b|used\b|use\b|required\b|needed\b|value\b|policy\b|name\b|"
    r"file\b|path\b)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
# Fail-closed markers for unprocessed executor material that must never be
# persisted inside ``reason_detail``: private/database paths, argv/command
# lines, raw output, and secret flags.  These are deliberately conservative.
_DB_FILE_RE = re.compile(
    r"(?i)(?:^|[\s('\"])[A-Za-z0-9_.-]*\.(?:db|db3|sqlite|sqlite3|sqlite2|sqlite-wal|sqlite-shm)"
    r"(?:$|[\s)'\"])"
)
_PRIVATE_HOME_RE = re.compile(r"(?i)(?:^|[\s('\"])/Users/[A-Za-z0-9_.-]+(?:[/\s]|$)")
_HOME_TILDE_RE = re.compile(r"(?i)(?:^|[\s('\"])~/")
_PRIVATE_SYSTEM_DIR_RE = re.compile(r"(?i)(?:/private/|/Users/|/home/|/etc/|/var/|/root/)")
_HIDDEN_CONFIG_DIR_RE = re.compile(
    r"(?i)(?:^|[/\s('\"])\.(?:hermes|claude|codex|config|aws|ssh|gnupg|azure|gradle|npm)"
    r"(?:[/\s.'\"]|$)"
)
_ARGV_MARKER_RE = re.compile(
    r"(?i)(?:^|\s)(?:argv|args?|cmd|command[- ]?line|shell|exec|spawn|bash|zsh|fish|sh\s+-[a-z]*c)"
    r"\s*[:=(]"
)
_RAW_OUTPUT_MARKER_RE = re.compile(
    r"(?i)(?:^|\s)(?:stdout|stderr|raw[- ]?output|output|result)\s*[:=]"
)
_SECRET_FLAG_RE = re.compile(
    r"(?i)(?:^|\s)(?:--|/)(?:token|secret|password|passwd|api[-_]?key|authorization|credential)\b"
)
_DETAIL_FORBIDDEN_MATCHERS = (
    _DB_FILE_RE,
    _PRIVATE_HOME_RE,
    _HOME_TILDE_RE,
    _PRIVATE_SYSTEM_DIR_RE,
    _HIDDEN_CONFIG_DIR_RE,
    _ARGV_MARKER_RE,
    _RAW_OUTPUT_MARKER_RE,
    _SECRET_FLAG_RE,
    _SECRET_SPACE_RE,
    _BEARER_CREDENTIAL_RE,
)


def build_approval_receipt(
    *,
    task_id: str,
    executor_run_id: str,
    executor: str,
    session_id: str,
    request_id: str,
    request_fingerprint: str,
    kind: str = APPROVAL_KIND,
    operation: str = "",
    summary: str = "",
    reason_summary: str = "",
    reason_detail: str = "",
    created_at: str | None = None,
    scope: str = APPROVAL_SCOPE,
) -> dict[str, Any]:
    """Build one pending v1 approval receipt, fail closed.

    The optional ``reason_summary`` is Core-normalized to a single redacted line
    of at most :data:`APPROVAL_REASON_SUMMARY_LIMIT` characters, and
    ``reason_detail`` is persisted only after secret redaction,
    control-character removal and a :data:`APPROVAL_REASON_DETAIL_LIMIT`
    character bound.  Receipts without either field remain valid (legacy).
    """
    envelope: dict[str, Any] = {
        "version": APPROVAL_VERSION,
        "task_id": task_id,
        "executor_run_id": executor_run_id,
        "executor": executor,
        "session_id": session_id,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
        "kind": kind,
        "operation": operation,
        "summary": summary,
        "scope": scope,
        "created_at": created_at or _utc_now(),
        "state": {"status": "pending"},
        "decision": {
            "type": "",
            "source": "",
            "decided_at": "",
        },
    }
    clean_reason_summary = normalize_reason_summary(
        reason_summary,
        executor=executor,
        operation=operation,
    )
    clean_reason_detail = sanitize_reason_detail(reason_detail)
    if clean_reason_summary:
        envelope["reason_summary"] = clean_reason_summary
    if clean_reason_detail:
        envelope["reason_detail"] = clean_reason_detail
    return validate_approval_receipt(envelope)


def validate_approval_receipt(
    value: Any,
    *,
    executor: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    executor_run_id: str | None = None,
    request_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Validate the strict v1 schema fail closed and return a defensive copy.

    Unknown additive fields are retained at every object level.  Fields or
    values that could persist sensitive execution/session material are rejected
    even when they are otherwise unknown extensions.

    The optional binding arguments harden single-action approval to exactly one
    native request: an expected ``executor_run_id`` and ``request_fingerprint``
    are checked against the receipt just like the task, official session and
    native request id.  A response that resolves to a different run or a
    different fingerprint is rejected fail closed.
    """
    if not isinstance(value, dict):
        _invalid("approval_invalid", "Approval receipt must be an object")
    receipt = copy.deepcopy(value)
    version = receipt.get("version")
    if isinstance(version, bool) or version != APPROVAL_VERSION:
        _invalid(
            "approval_version_unsupported",
            f"Unsupported approval receipt version: {version}",
        )
    _reject_sensitive_additions(receipt)

    if receipt.get("kind") != APPROVAL_KIND:
        _invalid("approval_kind_invalid", f"Approval kind must be {APPROVAL_KIND}")
    if receipt.get("scope") != APPROVAL_SCOPE:
        _invalid(
            "approval_scope_invalid",
            f"Approval scope must be {APPROVAL_SCOPE}",
        )
    _require_identifier(receipt.get("task_id"), "task_id")
    _require_identifier(receipt.get("executor_run_id"), "executor_run_id")
    _require_identifier(receipt.get("request_id"), "request_id")
    _require_identifier(receipt.get("request_fingerprint"), "request_fingerprint")
    session_id = _require_identifier(receipt.get("session_id"), "session_id")
    _require_operation(receipt.get("operation"))
    _require_summary(receipt.get("summary"))
    _require_reason_summary(receipt.get("reason_summary"))
    _require_reason_detail(receipt.get("reason_detail"))

    executor_name = str(receipt.get("executor") or "").strip().lower()
    if not executor_name:
        _invalid("approval_executor_invalid", "Approval receipt requires an executor")
    if executor is not None and executor_name != str(executor).strip().lower():
        _invalid(
            "approval_executor_mismatch",
            "Approval receipt executor does not match the expected executor",
        )
    expected_task_id = str(receipt.get("task_id") or "")
    if task_id is not None and expected_task_id != str(task_id).strip():
        _invalid(
            "approval_task_mismatch",
            "Approval receipt task_id does not match the expected task",
        )
    expected_session_id = str(receipt.get("session_id") or "")
    if session_id is not None and expected_session_id != str(session_id).strip():
        _invalid(
            "approval_session_mismatch",
            "Approval receipt session_id does not match the official session",
        )
    expected_request_id = str(receipt.get("request_id") or "")
    if request_id is not None and expected_request_id != str(request_id).strip():
        _invalid(
            "approval_request_mismatch",
            "Approval receipt request_id does not match the native request",
        )
    expected_executor_run_id = str(receipt.get("executor_run_id") or "")
    if (
        executor_run_id is not None
        and expected_executor_run_id != str(executor_run_id).strip()
    ):
        _invalid(
            "approval_run_mismatch",
            "Approval receipt executor_run_id does not match the authoritative run",
        )
    expected_fingerprint = str(receipt.get("request_fingerprint") or "")
    if (
        request_fingerprint is not None
        and expected_fingerprint != str(request_fingerprint).strip()
    ):
        _invalid(
            "approval_fingerprint_mismatch",
            "Approval receipt request_fingerprint does not match the native request",
        )

    state = _require_object(receipt, "state")
    if state.get("status") not in APPROVAL_STATES:
        _invalid("approval_state_invalid", f"Invalid approval state: {state.get('status')}")
    decision = _require_object(receipt, "decision")
    for field in ("type", "source", "decided_at"):
        if not isinstance(decision.get(field), str):
            _invalid("approval_decision_invalid", f"Approval decision.{field} must be a string")

    created_at = _require_timestamp(receipt.get("created_at"), "created_at")
    decided_type = str(decision.get("type") or "").strip()
    decided_source = str(decision.get("source") or "").strip()
    decided_at = str(decision.get("decided_at") or "").strip()
    if state["status"] == "pending":
        if decided_type or decided_source or decided_at:
            _invalid(
                "approval_decision_invalid",
                "Pending approval receipt must not carry a decision",
            )
    else:
        if decided_type not in APPROVAL_DECISION_TYPES:
            _invalid("approval_decision_invalid", f"Invalid approval decision: {decided_type}")
        if decided_source not in APPROVAL_DECISION_SOURCES:
            _invalid("approval_decision_invalid", f"Invalid approval decision source: {decided_source}")
        decided_timestamp = _require_timestamp(decision.get("decided_at"), "decision.decided_at")
        if decided_timestamp < created_at:
            _invalid(
                "approval_decision_invalid",
                "Approval decision predates receipt creation",
            )
    return receipt


def approval_receipt_from_extensions(
    extensions: dict[str, Any] | None,
    *,
    executor: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any] | None:
    """Read and validate the optional durable approval receipt extension."""
    values = extensions if isinstance(extensions, dict) else {}
    if APPROVAL_EXTENSION_KEY not in values:
        return None
    return validate_approval_receipt(
        values[APPROVAL_EXTENSION_KEY],
        executor=executor,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
    )


def record_approval_decision(
    value: Any,
    decision: str,
    *,
    source: str,
    decided_at: str | None = None,
    executor: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    executor_run_id: str | None = None,
    request_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Record one decision on the same native receipt (idempotent per source).

    Approve / Deny / close / timeout all record their auditable decision source
    on the receipt that created the wait.  Re-recording the exact same decision
    and source returns the receipt unchanged; a conflicting replay is rejected.

    The optional binding arguments are forwarded to :func:`validate_approval_receipt`
    so a decision that resolves to a different run, session, request, or
    fingerprint is rejected fail closed before anything is recorded.
    """
    receipt = validate_approval_receipt(
        value,
        executor=executor,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        executor_run_id=executor_run_id,
        request_fingerprint=request_fingerprint,
    )
    clean_decision = str(decision or "").strip().lower()
    if clean_decision not in APPROVAL_DECISION_TYPES:
        _invalid("approval_decision_invalid", f"Invalid approval decision: {decision}")
    clean_source = str(source or "").strip().lower()
    if clean_source not in APPROVAL_DECISION_SOURCES:
        _invalid("approval_decision_invalid", f"Invalid approval decision source: {source}")

    state = receipt["state"]
    existing = receipt["decision"]
    if state["status"] == "answered":
        if existing["type"] == clean_decision and existing["source"] == clean_source:
            return receipt
        _invalid(
            "approval_replay",
            "Approval receipt was already answered for a different decision",
        )
    state["status"] = "answered"
    existing["type"] = clean_decision
    existing["source"] = clean_source
    existing["decided_at"] = decided_at or _utc_now()
    return validate_approval_receipt(receipt)


def approval_public_projection(value: Any) -> dict[str, Any]:
    """Return the single sanitized view allowed outside internal Core logic.

    The projection mirrors the durable envelope's stable non-identifying facts:
    version, scope, kind, executor, operation, the Core-generated bounded
    summary, decision type/source, and timestamps.  Binding identifiers
    (task_id, executor_run_id, session_id, request_id, request_fingerprint)
    and any sensitive execution material are never projected.
    """
    receipt = validate_approval_receipt(value)
    decision = receipt["decision"]
    state = receipt["state"]
    projection: dict[str, Any] = {
        "version": APPROVAL_VERSION,
        "scope": APPROVAL_SCOPE,
        "kind": receipt["kind"],
        "executor": receipt["executor"],
        "operation": receipt["operation"],
        "summary": receipt["summary"],
        "state": state["status"],
        "created_at": receipt["created_at"],
    }
    if receipt.get("reason_summary"):
        projection["reason_summary"] = receipt["reason_summary"]
    if state["status"] == "answered":
        projection["decision"] = decision["type"]
        projection["decision_source"] = decision["source"]
        projection["decided_at"] = decision["decided_at"]
    return projection


def compute_request_fingerprint(
    *,
    executor: str,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Compute the stable fingerprint of one native can_use_tool request.

    The fingerprint is content-derived so the same tool call on the same
    official session maps to the same approval request.  Sensitive tool input
    values are included in the digest but never persisted.
    """
    payload: dict[str, Any] = {
        "executor": str(executor or "").strip().lower(),
        "session_id": str(session_id or "").strip(),
        "tool_name": str(tool_name or "").strip(),
        "tool_input": _normalize_fingerprint_input(tool_input or {}),
        "extra": _normalize_fingerprint_input(extra or {}),
    }
    digest = hashlib.sha256(
        _stable_json(payload).encode("utf-8")
    ).hexdigest()
    return f"fp-{digest[:40]}"


def core_bounded_summary(
    *,
    executor: str,
    operation: str,
    scope: str = APPROVAL_SCOPE,
    kind: str = APPROVAL_KIND,
) -> str:
    """Generate the Core-owned bounded one-line summary.

    The summary is derived only from structured Executor/operation/scope facts
    and is intentionally short.  It never includes the permission prompt, the
    native tool input, command line, raw output, or secrets.
    """
    name = str(executor or "").strip().lower() or "executor"
    op = str(operation or "").strip()
    if not op:
        op = "an action"
    if kind == APPROVAL_KIND:
        text = f"{name} needs one-time permission for: {op}"
    else:
        text = f"{name} needs one-time approval for: {op}"
    return _bound_text(text, APPROVAL_SUMMARY_LIMIT)


def normalize_reason_summary(
    value: Any,
    *,
    executor: str = "",
    operation: str = "",
) -> str:
    """Generate or normalize the single-line reason summary Core persists.

    The summary is redacted, stripped of control characters, collapsed to one
    line and bounded to :data:`APPROVAL_REASON_SUMMARY_LIMIT` characters.  When
    no usable reason is supplied, Core falls back to the structured
    Executor/operation summary so the minimal dialog view always has text.
    """
    from .reports import redact_secrets

    raw = str(value or "")
    # Whitespace-separated credentials (``token abc123``, ``Bearer abc123``)
    # must be redacted before the generic redactor can mask the label while
    # leaving the real value orphaned in the persisted summary.
    text = _SECRET_SPACE_RE.sub("[REDACTED]", raw)
    text = _BEARER_CREDENTIAL_RE.sub("[REDACTED]", text)
    text = str(redact_secrets(text) or "")
    text = _SECRET_VALUE_RE.sub("[REDACTED]", text)
    text = _remove_control_characters(text)
    text = " ".join(text.split()).strip()
    if not text:
        return core_bounded_summary(executor=executor, operation=operation)
    return _bound_text(text, APPROVAL_REASON_SUMMARY_LIMIT)


def sanitize_reason_detail(value: Any) -> str:
    """Return the bounded, redacted, control-character-free reason detail.

    Core persists the detail only after secret redaction, control-character
    removal and a :data:`APPROVAL_REASON_DETAIL_LIMIT` character bound.  The
    whole detail is dropped fail-closed when it contains private or database
    paths, unprocessed argv/command lines, raw output, or secret flags
    anywhere in the string -- not just at the very start.  The fail-closed
    markers are checked on the raw input before redaction can mask them, and
    again on the redacted result.  An empty result is omitted from the receipt
    so existing receipts without a detail remain valid.
    """
    from .reports import redact_secrets

    raw = str(value or "")
    if _detail_contains_forbidden(raw):
        return ""
    # Redact whitespace-separated credentials before the generic redactor can
    # mask the label while leaving the real value orphaned in the result.
    text = _SECRET_SPACE_RE.sub("[REDACTED]", raw)
    text = _BEARER_CREDENTIAL_RE.sub("[REDACTED]", text)
    text = str(redact_secrets(text) or "")
    text = _SECRET_VALUE_RE.sub("[REDACTED]", text)
    text = _remove_control_characters(text)
    text = " ".join(text.split()).strip()
    if _detail_contains_forbidden(text):
        return ""
    if len(text) > APPROVAL_REASON_DETAIL_LIMIT:
        text = text[:APPROVAL_REASON_DETAIL_LIMIT].rstrip()
    return text


def approval_receipt_pending(value: Any) -> bool:
    """Return whether the receipt is waiting for a user decision."""
    return validate_approval_receipt(value)["state"]["status"] == "pending"


def pending_approval_request(
    extensions: dict[str, Any] | None,
    *,
    task_status: str = "",
) -> dict[str, Any] | None:
    """Return the waiting single-action approval input, or ``None``.

    A single-action approval is only "pending" while the task is actually
    waiting for input and the persisted receipt is still ``pending``.  Stale
    receipts left behind by a crash, timeout, or transport death (the task is no
    longer ``input_required``, or the receipt is already answered) never count
    as pending, so an explicit recovery can request a fresh approval request id
    instead of being blocked by the dead request.
    """
    if task_status and str(task_status).strip().lower() != "input_required":
        return None
    values = extensions if isinstance(extensions, dict) else {}
    receipt_value = values.get(APPROVAL_EXTENSION_KEY)
    if not isinstance(receipt_value, dict):
        return None
    try:
        receipt = validate_approval_receipt(receipt_value)
    except ABCError:
        return None
    if receipt["state"]["status"] != "pending":
        return None
    request = values.get("agentbc.input")
    if (
        not isinstance(request, dict)
        or str(request.get("status") or "") != "waiting"
        or str(request.get("type") or "") != APPROVAL_KIND
        or str(request.get("scope") or "") != APPROVAL_SCOPE
    ):
        return None
    return request


def assert_no_pending_approval(
    extensions: dict[str, Any] | None,
    *,
    task_status: str = "",
) -> None:
    """Reject a concurrent second single-action approval fail closed.

    One pending receipt may drive exactly one native request and one dialog.  A
    second native permission request while the first is still waiting is refused
    with ``approval_already_pending`` so a single dialog can never authorize two
    different actions.
    """
    if pending_approval_request(extensions, task_status=task_status) is not None:
        _invalid(
            "approval_already_pending",
            "A single-action approval is already waiting for this task",
        )


def new_request_id() -> str:
    """Return a stable opaque approval request id."""
    return f"approval-{uuid.uuid4().hex}"


def _normalize_fingerprint_input(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_fingerprint_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_fingerprint_input(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value or "")


def _stable_json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_object(parent: dict[str, Any], field: str) -> dict[str, Any]:
    value = parent.get(field)
    if not isinstance(value, dict):
        _invalid("approval_invalid", f"Approval receipt {field} must be an object")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _IDENTIFIER_RE.fullmatch(value)
    ):
        _invalid(
            "approval_invalid",
            f"Approval receipt {field} must be a non-empty opaque identifier",
        )
    return value


def _require_operation(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid("approval_operation_invalid", "Approval receipt operation is required")
    if not _OPERATION_RE.fullmatch(value.strip()):
        _invalid(
            "approval_operation_invalid",
            "Approval receipt operation contains control characters",
        )
    return value.strip()


def _require_summary(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid("approval_summary_invalid", "Approval receipt summary is required")
    clean = value.strip()
    if len(clean) > APPROVAL_SUMMARY_LIMIT:
        _invalid(
            "approval_summary_invalid",
            f"Approval receipt summary must be at most {APPROVAL_SUMMARY_LIMIT} characters",
        )
    if any(ord(char) < 32 for char in clean):
        _invalid("approval_summary_invalid", "Approval receipt summary contains control characters")
    return clean


def _require_reason_summary(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _invalid(
            "approval_reason_summary_invalid",
            "Approval receipt reason_summary must be a non-empty string",
        )
    clean = value.strip()
    if len(clean) > APPROVAL_REASON_SUMMARY_LIMIT:
        _invalid(
            "approval_reason_summary_invalid",
            "Approval receipt reason_summary must be a single line of at most "
            f"{APPROVAL_REASON_SUMMARY_LIMIT} characters",
        )
    if any(ord(char) < 32 for char in clean):
        _invalid(
            "approval_reason_summary_invalid",
            "Approval receipt reason_summary must be a single line without control characters",
        )
    if _summary_contains_credential(clean):
        _invalid(
            "approval_sensitive_field",
            "Approval receipt reason_summary cannot persist credential content",
        )
    return clean


def _require_reason_detail(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _invalid(
            "approval_reason_detail_invalid",
            "Approval receipt reason_detail must be a non-empty string",
        )
    clean = value.strip()
    if len(clean) > APPROVAL_REASON_DETAIL_LIMIT:
        _invalid(
            "approval_reason_detail_invalid",
            "Approval receipt reason_detail must be at most "
            f"{APPROVAL_REASON_DETAIL_LIMIT} characters",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in clean):
        _invalid(
            "approval_reason_detail_invalid",
            "Approval receipt reason_detail contains control characters",
        )
    if _detail_contains_forbidden(clean):
        _invalid(
            "approval_sensitive_field",
            "Approval receipt reason_detail cannot persist sensitive content",
        )
    return clean


def _require_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _invalid("approval_invalid", f"Approval receipt {field} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _invalid("approval_invalid", f"Approval receipt {field} is invalid")
    if parsed.tzinfo is None:
        _invalid("approval_invalid", f"Approval receipt {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _detail_contains_forbidden(value: str) -> bool:
    """Return whether a detail string still contains fail-closed content.

    The checks mirror :data:`_DETAIL_FORBIDDEN_MATCHERS`: private/database
    paths and unprocessed argv/raw output anywhere in the string (not just at
    the start) invalidate the detail before it can be persisted.
    """
    return any(pattern.search(value) for pattern in _DETAIL_FORBIDDEN_MATCHERS)


def _summary_contains_credential(value: str) -> bool:
    """Return whether a reason summary still contains a real credential value.

    The single-line summary only admits the space-separated credential forms;
    private-path / argv / raw-output markers are detail-specific and would
    over-reject legitimate one-line summaries.
    """
    return _SECRET_SPACE_RE.search(value) is not None or _BEARER_CREDENTIAL_RE.search(
        value
    ) is not None


def _reject_sensitive_additions(value: Any, *, key_path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in _FORBIDDEN_FIELD_PARTS) or "path" in normalized:
                _invalid(
                    "approval_sensitive_field",
                    f"Approval receipt cannot persist sensitive field: {'.'.join((*key_path, key))}",
                )
            _reject_sensitive_additions(item, key_path=(*key_path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_additions(item, key_path=(*key_path, str(index)))
    elif isinstance(value, str):
        clean = value.strip()
        if (
            clean.startswith(("/", "~/"))
            or _SECRET_ASSIGNMENT_RE.search(clean)
            or _SECRET_SPACE_RE.search(clean)
            or _BEARER_CREDENTIAL_RE.search(clean)
        ):
            _invalid(
                "approval_sensitive_field",
                f"Approval receipt cannot persist sensitive content at: {'.'.join(key_path)}",
            )


def _remove_control_characters(value: str) -> str:
    # Replace control characters with a normal space so adjacent words never get
    # glued together; callers collapse runs of whitespace afterwards.
    return "".join(" " if _is_control_character(char) else char for char in value)


def _is_control_character(char: str) -> bool:
    code = ord(char)
    return code < 32 or code == 127


def _bound_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _invalid(code: str, message: str) -> None:
    raise ABCError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
