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
# PERM-104-002: the v2 envelope adds the executor-native choice broker.  v1
# receipts remain valid and are dual-read; new native permission requests are
# always persisted as v2.
APPROVAL_V2_VERSION = 2
# PERM-104-001: v3 is the task-scoped elevation decision.  v1/v2 remain
# dual-read history only; no v3 receipt contains executor-native choice
# lists or a session/once grant decision.
APPROVAL_V3_VERSION = 3
APPROVAL_SCOPE = "single_action"
# A v3 receipt is deliberately not a single-action approval.  It is the one
# task-scoped decision which authorizes the contained-full continuation.
APPROVAL_V3_SCOPE = "task_elevation"
APPROVAL_V3_ELEVATION_MODE = "contained_full"
APPROVAL_KIND = "permission"
APPROVAL_STATES = frozenset({"pending", "answered"})
APPROVAL_DECISION_TYPES = frozenset({"approve", "deny"})
APPROVAL_V3_DECISION_TYPES = frozenset({"approve_full", "deny"})
APPROVAL_DECISION_SOURCES = frozenset(
    {"user", "timeout", "dialog_closed", "close", "stale", "crash", "fail_closed"}
)
# v2 choice kinds.  ``other`` covers native options AgentBC may echo back but
# never synthesize (audit-only in 1.04A; persistent/always choices).
APPROVAL_V2_CHOICE_KINDS = frozenset({"once", "session", "deny", "other"})
APPROVAL_V2_BROKER_AUTHORITY_EXECUTORS = frozenset({"codex", "claude", "hermes"})
APPROVAL_V2_MAX_CHOICES = 12
APPROVAL_V2_LABEL_LIMIT = 120
APPROVAL_V2_HANDLE_PREFIX = "opt-"
APPROVAL_V2_ERROR_MISSING_HANDLE = "native_permission_choice_required"
APPROVAL_SUMMARY_LIMIT = 120
APPROVAL_REASON_SUMMARY_LIMIT = 120
APPROVAL_REASON_DETAIL_LIMIT = 2000
SUMMARY_ELLIPSIS = "…"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_NATIVE_EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
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
_SECRET_LABEL_PATTERN = (
    r"(?:access(?:\s+|[-_])token|api\s*[-_]?\s*key|bearer|password|passwd|"
    r"token|secret|credential|authorization)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9]){_SECRET_LABEL_PATTERN}(?![A-Za-z0-9])\s*[:=]"
)
_SECRET_VALUE_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9]){_SECRET_LABEL_PATTERN}(?![A-Za-z0-9])"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
# A space-separated sensitive label is safe only when its immediate next word
# is an explicitly allowlisted ordinary explanation word.  This is deliberate
# grammar, not credential entropy/length/character heuristics: ``token is`` is
# prose, while ``token huntertwo`` is sensitive regardless of its shape.
_SECRET_SPACE_ALLOWED_WORDS = frozenset(
    {
        "also",
        "and",
        "are",
        "as",
        "at",
        "auth",
        "authn",
        "authentication",
        "be",
        "been",
        "by",
        "can",
        "could",
        "configuration",
        "configurations",
        "credential",
        "credentials",
        "endpoint",
        "file",
        "for",
        "from",
        "header",
        "in",
        "is",
        "may",
        "might",
        "management",
        "must",
        "name",
        "needed",
        "not",
        "of",
        "on",
        "only",
        "or",
        "path",
        "pairs",
        "pair",
        "policy",
        "provider",
        "required",
        "rotation",
        "scheme",
        "service",
        "settings",
        "should",
        "store",
        "stored",
        "the",
        "to",
        "token",
        "tokens",
        "type",
        "use",
        "used",
        "using",
        "value",
        "was",
        "were",
        "will",
        "without",
        "with",
        "would",
    }
)
_SECRET_SPACE_ALLOWED_WORD_PATTERN = "|".join(
    re.escape(word)
    for word in sorted(_SECRET_SPACE_ALLOWED_WORDS, key=lambda item: (-len(item), item))
)
_SECRET_SPACE_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9]){_SECRET_LABEL_PATTERN}(?![A-Za-z0-9])\s+"
    rf"(?!(?:{_SECRET_SPACE_ALLOWED_WORD_PATTERN})\b)[^\s,;]+"
)
_SECRET_PROSE_LABEL_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9]){_SECRET_LABEL_PATTERN}(?![A-Za-z0-9])"
    rf"(?=\s+(?:{_SECRET_SPACE_ALLOWED_WORD_PATTERN})\b)"
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
        "summary": "",
        "scope": scope,
        "created_at": created_at or _utc_now(),
        "state": {"status": "pending"},
        "decision": {
            "type": "",
            "source": "",
            "decided_at": "",
        },
    }
    clean_summary, summary_was_truncated = _bounded_text_with_truncation(
        summary,
        APPROVAL_SUMMARY_LIMIT,
    )
    envelope["summary"] = clean_summary
    clean_reason_summary, reason_summary_was_truncated = normalize_reason_summary_details(
        reason_summary,
        executor=executor,
        operation=operation,
    )
    clean_reason_detail = sanitize_reason_detail(reason_detail)
    envelope["summary_truncated"] = bool(
        summary_was_truncated or reason_summary_was_truncated
    )
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
    if version == APPROVAL_V3_VERSION:
        return validate_approval_receipt_v3(
            receipt,
            executor=executor,
            task_id=task_id,
            session_id=session_id,
            request_id=request_id,
            executor_run_id=executor_run_id,
            request_fingerprint=request_fingerprint,
        )
    if isinstance(version, bool) or version not in {APPROVAL_VERSION, APPROVAL_V2_VERSION}:
        _invalid(
            "approval_version_unsupported",
            f"Unsupported approval receipt version: {version}",
        )
    if version == APPROVAL_V2_VERSION and not isinstance(receipt.get("choices"), list):
        _invalid(
            "approval_version_unsupported",
            "A v2 approval receipt requires the offered choice list",
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
    summary_truncated = receipt.get("summary_truncated")
    if summary_truncated is not None and not isinstance(summary_truncated, bool):
        _invalid(
            "approval_summary_truncated_invalid",
            "Approval receipt summary_truncated must be a boolean",
        )

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
    if receipt.get("version") == APPROVAL_V3_VERSION:
        return record_approval_decision_v3(
            receipt,
            decision,
            source=source,
            decided_at=decided_at,
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
    if receipt.get("version") == APPROVAL_V3_VERSION:
        return approval_public_projection_v3(receipt)
    decision = receipt["decision"]
    state = receipt["state"]
    projection: dict[str, Any] = {
        "version": APPROVAL_VERSION,
        "scope": APPROVAL_SCOPE,
        "kind": receipt["kind"],
        "executor": receipt["executor"],
        "operation": receipt["operation"],
        "summary": receipt["summary"],
        "summary_truncated": bool(receipt.get("summary_truncated", False)),
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
    return core_bounded_summary_details(
        executor=executor,
        operation=operation,
        scope=scope,
        kind=kind,
    )[0]


def _core_summary_text(
    *,
    executor: str,
    operation: str,
    kind: str,
) -> str:
    name = str(executor or "").strip().lower() or "executor"
    op = str(operation or "").strip()
    if not op:
        op = "an action"
    if kind == APPROVAL_KIND:
        return f"{name} needs one-time permission for: {op}"
    return f"{name} needs one-time approval for: {op}"


def _redact_reason_text(value: str) -> str:
    """Redact credentials while preserving explicitly allowlisted prose labels."""
    from .reports import redact_secrets

    text = _SECRET_SPACE_RE.sub("[REDACTED]", value)
    text = _SECRET_VALUE_RE.sub("[REDACTED]", text)
    parts: list[str] = []
    offset = 0
    for match in _SECRET_PROSE_LABEL_RE.finditer(text):
        parts.append(str(redact_secrets(text[offset : match.start()]) or ""))
        parts.append(match.group(0))
        offset = match.end()
    parts.append(str(redact_secrets(text[offset:]) or ""))
    return "".join(parts)


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
    return normalize_reason_summary_details(
        value,
        executor=executor,
        operation=operation,
    )[0]


def normalize_reason_summary_details(
    value: Any,
    *,
    executor: str = "",
    operation: str = "",
) -> tuple[str, bool]:
    """Return the Core-compacted reason summary and its truncation state."""
    raw = str(value or "")
    text = _redact_reason_text(raw)
    text = _remove_control_characters(text)
    text = " ".join(text.split()).strip()
    if not text:
        fallback = core_bounded_summary_details(executor=executor, operation=operation)
        return fallback
    return _bounded_text_with_truncation(text, APPROVAL_REASON_SUMMARY_LIMIT)


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
    raw = str(value or "")
    if _detail_contains_forbidden(raw):
        return ""
    text = _redact_reason_text(raw)
    text = _remove_control_characters(text)
    text = " ".join(text.split()).strip()
    if _detail_contains_forbidden(text):
        return ""
    if len(text) > APPROVAL_REASON_DETAIL_LIMIT:
        text = text[:APPROVAL_REASON_DETAIL_LIMIT].rstrip()
    return text


def approval_receipt_pending(value: Any) -> bool:
    """Return whether the receipt is waiting for a user decision."""
    receipt = validate_approval_receipt_any_version(value)
    return receipt["state"]["status"] == "pending"


# ---------------------------------------------------------------------------
# agentbc.approval v2: executor-native choice broker (PERM-104-002)
# ---------------------------------------------------------------------------
#
# v2 replaces inferred permission categories (approve/deny flattened over an
# executor's native surface) with the exact choices the executor itself
# offered.  Core never invents a choice: every offered choice carries the
# executor's native option identity plus a Core-computed digest of the exact
# offered shape, and the user selects one opaque handle bound to this exact
# request.


def build_choice_handle(request_id: str, index: int, offered_digest: str) -> str:
    """Return one opaque choice handle bound to this exact request+choice."""
    payload = json_module_dumps(
        {
            "request_id": str(request_id or ""),
            "index": int(index),
            "offered_digest": str(offered_digest or ""),
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{APPROVAL_V2_HANDLE_PREFIX}{digest[:16]}"


def compute_offered_choice_digest(choice: dict[str, Any]) -> str:
    """Digest the exact offered native choice shape (labels + native ids)."""
    payload = json_module_dumps(
        {
            "native_option_id": str(choice.get("native_option_id") or ""),
            "kind": str(choice.get("kind") or ""),
            "label": str(choice.get("label") or ""),
            "selectable": bool(choice.get("selectable", True)),
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_offered_choices(
    raw_choices: list[dict[str, Any]],
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    """Normalize the executor's offered choices into v2 receipt entries.

    Every entry carries: handle (opaque), native_option_id (exact), kind
    (once/session/deny/other), label (redacted, bounded), selectable flag and
    the digest of the exact offered shape.  Handles are derived from the
    request id + choice digest so a handle is only ever valid for the exact
    request and choice it was offered with.
    """
    if not isinstance(raw_choices, list) or not raw_choices:
        _invalid(
            "approval_choices_missing",
            "A v2 approval receipt requires at least one offered native choice",
        )
    if len(raw_choices) > APPROVAL_V2_MAX_CHOICES:
        _invalid(
            "approval_choices_invalid",
            f"A v2 approval receipt carries at most {APPROVAL_V2_MAX_CHOICES} choices",
        )
    from .reports import redact_secrets

    normalized: list[dict[str, Any]] = []
    seen_handles: set[str] = set()
    for index, raw in enumerate(raw_choices):
        if not isinstance(raw, dict):
            _invalid(
                "approval_choices_invalid",
                "Each offered choice must be an object",
            )
        native_option_id = str(raw.get("native_option_id") or "").strip()
        if not native_option_id or not _IDENTIFIER_RE.fullmatch(native_option_id):
            _invalid(
                "approval_choices_invalid",
                "Each offered choice requires a native option identifier",
            )
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in APPROVAL_V2_CHOICE_KINDS:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice kind is unsupported: {kind}",
            )
        label_raw = str(raw.get("label") or "").strip()
        label = " ".join(str(redact_secrets(label_raw) or "").split())
        if any(ord(char) < 32 or ord(char) == 127 for char in label):
            _invalid(
                "approval_choices_invalid",
                "Offered choice labels must not contain control characters",
            )
        label, _ = _bounded_text_with_truncation(label, APPROVAL_V2_LABEL_LIMIT)
        selectable = raw.get("selectable", True)
        if not isinstance(selectable, bool):
            _invalid(
                "approval_choices_invalid",
                "Offered choice selectable must be a boolean",
            )
        entry: dict[str, Any] = {
            "handle": "",
            "native_option_id": native_option_id,
            "kind": kind,
            "label": label,
            "selectable": selectable,
        }
        entry["offered_digest"] = compute_offered_choice_digest(entry)
        entry["handle"] = build_choice_handle(request_id, index, entry["offered_digest"])
        if entry["handle"] in seen_handles:
            _invalid(
                "approval_choices_invalid",
                "Offered choices produced duplicate handles",
            )
        seen_handles.add(entry["handle"])
        normalized.append(entry)
    return normalized


def build_approval_receipt_v2(
    *,
    task_id: str,
    executor_run_id: str,
    executor: str,
    session_id: str,
    request_id: str,
    request_fingerprint: str,
    operation: str,
    summary: str = "",
    reason_summary: str = "",
    reason_detail: str = "",
    authority_protocol: str,
    authority_protocol_version: int,
    authority_method: str,
    broker_request_id: str,
    provider_request_id: str = "",
    native_item_id: str = "",
    offered_choices: list[dict[str, Any]],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one pending v2 receipt binding the executor-native choice set."""
    clean_executor = str(executor or "").strip().lower()
    if clean_executor not in APPROVAL_V2_BROKER_AUTHORITY_EXECUTORS:
        _invalid(
            "approval_executor_invalid",
            f"v2 approval receipts require a broker-capable executor: {clean_executor}",
        )
    choices = build_offered_choices(
        list(offered_choices or []),
        request_id=str(request_id or ""),
    )
    envelope: dict[str, Any] = {
        "version": APPROVAL_V2_VERSION,
        "task_id": task_id,
        "executor_run_id": executor_run_id,
        "executor": clean_executor,
        "session_id": session_id,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
        "kind": APPROVAL_KIND,
        "operation": operation,
        "summary": "",
        "scope": APPROVAL_SCOPE,
        "authority": {
            "executor": clean_executor,
            "protocol": str(authority_protocol or "").strip(),
            "protocol_version": authority_protocol_version,
            "method": str(authority_method or "").strip(),
        },
        "broker_request_id": str(broker_request_id or "").strip(),
        "choices": choices,
        "selection": {},
        "created_at": created_at or _utc_now(),
        "state": {"status": "pending"},
        "decision": {"type": "", "source": "", "decided_at": ""},
    }
    if str(provider_request_id or "").strip():
        envelope["provider_request_id"] = str(provider_request_id).strip()
    if str(native_item_id or "").strip():
        envelope["native_item_id"] = str(native_item_id).strip()
    clean_summary, summary_was_truncated = _bounded_text_with_truncation(
        summary,
        APPROVAL_SUMMARY_LIMIT,
    )
    if not clean_summary:
        clean_summary, summary_was_truncated = core_bounded_summary_details(
            executor=clean_executor,
            operation=operation,
        )
    envelope["summary"] = clean_summary
    clean_reason_summary, reason_summary_was_truncated = normalize_reason_summary_details(
        reason_summary,
        executor=clean_executor,
        operation=operation,
    )
    clean_reason_detail = sanitize_reason_detail(reason_detail)
    envelope["summary_truncated"] = bool(
        summary_was_truncated or reason_summary_was_truncated
    )
    if clean_reason_summary:
        envelope["reason_summary"] = clean_reason_summary
    if clean_reason_detail:
        envelope["reason_detail"] = clean_reason_detail
    return validate_approval_receipt_v2(envelope)


def _validate_v2_shared(value: dict[str, Any]) -> None:
    """Validate the v2-specific fields (authority, choices, selection)."""
    authority_value = value.get("authority")
    if not isinstance(authority_value, dict):
        _invalid("approval_authority_invalid", "v2 receipts require an authority object")
    assert isinstance(authority_value, dict)
    authority_dict: dict[str, Any] = {
        str(key): item for key, item in authority_value.items()
    }
    if str(authority_dict.get("executor") or "").strip().lower() != str(
        value.get("executor") or ""
    ).strip().lower():
        _invalid(
            "approval_authority_invalid",
            "authority.executor must match the receipt executor",
        )
    protocol = str(authority_dict.get("protocol") or "").strip()
    if not protocol or not _IDENTIFIER_RE.fullmatch(protocol):
        _invalid("approval_authority_invalid", "authority.protocol is required")
    method = str(authority_dict.get("method") or "").strip()
    if not method or not _OPERATION_RE.fullmatch(method):
        _invalid("approval_authority_invalid", "authority.method is required")
    version_value = authority_dict.get("protocol_version")
    if isinstance(version_value, bool) or not isinstance(version_value, int):
        _invalid(
            "approval_authority_invalid",
            "authority.protocol_version must be an integer",
        )
    broker_request_id = str(value.get("broker_request_id") or "").strip()
    if not broker_request_id or not _IDENTIFIER_RE.fullmatch(broker_request_id):
        _invalid("approval_invalid", "v2 receipts require a broker_request_id")
    provider_request_id = str(value.get("provider_request_id") or "").strip()
    if provider_request_id and not _IDENTIFIER_RE.fullmatch(provider_request_id):
        _invalid("approval_invalid", "provider_request_id must be an opaque identifier")
    native_item_id = str(value.get("native_item_id") or "").strip()
    if native_item_id and not _IDENTIFIER_RE.fullmatch(native_item_id):
        _invalid("approval_invalid", "native_item_id must be an opaque identifier")
    offered = value.get("choices")
    raw_choices = [choice for choice in offered if isinstance(choice, dict)] if isinstance(offered, list) else []
    choices = build_offered_choices(
        [
            {
                "native_option_id": choice.get("native_option_id"),
                "kind": choice.get("kind"),
                "label": choice.get("label"),
                "selectable": choice.get("selectable", True),
            }
            for choice in raw_choices
        ],
        request_id=str(value.get("request_id") or ""),
    )
    if not isinstance(offered, list) or len(offered) != len(choices):
        _invalid("approval_choices_invalid", "Offered choices are missing or malformed")
    for index, (stored, expected) in enumerate(zip(offered, choices)):
        if not isinstance(stored, dict):
            _invalid("approval_choices_invalid", "Each offered choice must be an object")
        if str(stored.get("handle") or "") != expected["handle"]:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice handle at index {index} is not bound to this request",
            )
        if str(stored.get("offered_digest") or "") != expected["offered_digest"]:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice digest at index {index} does not match its shape",
            )
        if str(stored.get("native_option_id") or "") != expected["native_option_id"]:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice native id at index {index} is malformed",
            )
        if str(stored.get("kind") or "") != expected["kind"]:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice kind at index {index} is unsupported",
            )
        if str(stored.get("label") or "") != expected["label"]:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice label at index {index} is malformed",
            )
        if stored.get("selectable", True) is not expected["selectable"]:
            _invalid(
                "approval_choices_invalid",
                f"Offered choice selectable at index {index} is malformed",
            )
    selection = value.get("selection")
    if not isinstance(selection, dict):
        _invalid("approval_selection_invalid", "v2 receipts require a selection object")
    state = value.get("state")
    status = str(state.get("status") or "") if isinstance(state, dict) else ""
    selection_handle = str(selection.get("handle") or "").strip()
    selection_source = str(selection.get("source") or "").strip()
    selection_at = str(selection.get("at") or "").strip()
    if status == "pending":
        if selection_handle or selection_source or selection_at:
            _invalid(
                "approval_selection_invalid",
                "A pending v2 receipt must not carry a selection",
            )
        return
    if not selection_handle or not selection_source or not selection_at:
        _invalid(
            "approval_selection_invalid",
            "An answered v2 receipt requires the selection handle, source and time",
        )
    matched = [c for c in choices if str(c.get("handle")) == selection_handle]
    if not matched:
        _invalid(
            "approval_handle_mismatch",
            "The recorded selection handle was not offered by this request",
        )
    choice = matched[0]
    if not choice.get("selectable", True):
        _invalid(
            "approval_choice_not_selectable",
            "The selected choice was offered as non-selectable",
        )
    if str(selection.get("native_option_id") or "") != str(choice.get("native_option_id")):
        _invalid(
            "approval_handle_mismatch",
            "The recorded selection native option does not match the handle",
        )
    if str(selection.get("kind") or "") != str(choice.get("kind")):
        _invalid(
            "approval_handle_mismatch",
            "The recorded selection kind does not match the handle",
        )
    if str(selection.get("offered_digest") or "") != str(choice.get("offered_digest")):
        _invalid(
            "approval_handle_mismatch",
            "The recorded selection digest does not match the offered choice",
        )
    if selection_source not in APPROVAL_DECISION_SOURCES:
        _invalid(
            "approval_decision_invalid",
            f"Invalid approval decision source: {selection_source}",
        )
    _require_timestamp(selection_at, "selection.at")


def validate_approval_receipt_v2(value: Any) -> dict[str, Any]:
    """Validate the strict v2 schema fail closed and return a defensive copy."""
    receipt = validate_approval_receipt(value)
    if receipt.get("version") != APPROVAL_V2_VERSION:
        _invalid(
            "approval_version_unsupported",
            f"Expected a v2 approval receipt, got version {receipt.get('version')}",
        )
    _validate_v2_shared(receipt)
    # Validate the decision block exactly like v1 answered receipts.
    state = receipt["state"]
    decision = receipt["decision"]
    decided_type = str(decision.get("type") or "").strip()
    decided_source = str(decision.get("source") or "").strip()
    decided_at = str(decision.get("decided_at") or "").strip()
    if state["status"] == "answered":
        if decided_type not in APPROVAL_DECISION_TYPES:
            _invalid(
                "approval_decision_invalid",
                f"Invalid approval decision: {decided_type}",
            )
        if decided_source not in APPROVAL_DECISION_SOURCES:
            _invalid(
                "approval_decision_invalid",
                f"Invalid approval decision source: {decided_source}",
            )
        decided_timestamp = _require_timestamp(
            decision.get("decided_at"), "decision.decided_at"
        )
        created_at = _require_timestamp(receipt.get("created_at"), "created_at")
        if decided_timestamp < created_at:
            _invalid(
                "approval_decision_invalid",
                "Approval decision predates receipt creation",
            )
        selection_at = _require_timestamp(
            receipt["selection"].get("at"), "selection.at"
        )
        if selection_at < created_at:
            _invalid(
                "approval_selection_invalid",
                "Selection predates receipt creation",
            )
    else:
        if decided_type or decided_source or decided_at:
            _invalid(
                "approval_decision_invalid",
                "Pending approval receipt must not carry a decision",
            )
    return receipt


# ---------------------------------------------------------------------------
# PERM-104-001: task-scoped single-elevation approval (v3)
# ---------------------------------------------------------------------------

_V3_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_V3_CARDINALITY_FIELDS = (
    "permission_requests",
    "notifications",
    "human_decisions",
    "elevation_receipts",
    "full_continuations",
)
_V3_LEGACY_FIELDS = frozenset(
    {
        "choices",
        "offered_choices",
        "selection",
        "permission_option",
        "session_rule",
        "grant",
        "permission_grant",
    }
)


def build_approval_receipt_v3(
    *,
    task_id: str,
    executor_run_id: str,
    executor: str,
    session_id: str,
    request_id: str,
    request_fingerprint: str,
    operation: str,
    path_plan_digest: str,
    containment_profile_digest: str = "",
    profile_digest: str = "",
    summary: str = "",
    reason_summary: str = "",
    reason_detail: str = "",
    authority: dict[str, Any] | None = None,
    authority_protocol: str = "",
    authority_protocol_version: int = 0,
    authority_method: str = "",
    native_event: str = "",
    tool_call_id: str = "",
    action_fingerprint: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a pending v3 task-elevation receipt.

    v3 deliberately accepts no native choice set.  The only human decisions
    are ``approve_full`` and ``deny``; the later contained continuation is
    recorded separately after the authoritative Runner receipt is verified.
    """
    clean_executor = str(executor or "").strip().lower()
    authority_value = dict(authority or {})
    protocol = str(
        authority_value.get("protocol") or authority_protocol or "agentbc.native"
    ).strip()
    method = str(
        authority_value.get("method") or authority_method or "requestApproval"
    ).strip()
    protocol_version = authority_value.get(
        "protocol_version", authority_protocol_version
    )
    clean_profile_digest = str(
        containment_profile_digest or profile_digest or ""
    ).strip()
    envelope: dict[str, Any] = {
        "version": APPROVAL_V3_VERSION,
        "task_id": task_id,
        "executor_run_id": executor_run_id,
        "executor": clean_executor,
        "session_id": session_id,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
        "kind": APPROVAL_KIND,
        "operation": operation,
        "scope": APPROVAL_V3_SCOPE,
        "elevation_mode": APPROVAL_V3_ELEVATION_MODE,
        "summary": "",
        "path_plan_digest": path_plan_digest,
        "containment_profile_digest": clean_profile_digest,
        "authority": {
            "executor": clean_executor,
            "protocol": protocol,
            "protocol_version": protocol_version,
            "method": method,
        },
        "provenance": {
            "native_event": str(
                native_event or "native_structured_permission_event"
            ).strip(),
            "tool_call_id": str(tool_call_id or request_id).strip(),
            "action_fingerprint": str(action_fingerprint or "").strip(),
        },
        "containment": {
            "mode": APPROVAL_V3_ELEVATION_MODE,
            "profile_digest": clean_profile_digest,
            "path_plan_digest": path_plan_digest,
        },
        "cardinality": {
            "permission_requests": 1,
            "notifications": 0,
            "human_decisions": 0,
            "elevation_receipts": 1,
            "full_continuations": 0,
        },
        "continuation": {
            "count": 0,
            "executor_run_id": "",
            "session_id": "",
        },
        "created_at": created_at or _utc_now(),
        "state": {"status": "pending"},
        "decision": {"type": "", "source": "", "decided_at": ""},
    }
    clean_summary, summary_was_truncated = _bounded_text_with_truncation(
        summary,
        APPROVAL_SUMMARY_LIMIT,
    )
    if not clean_summary:
        clean_summary, summary_was_truncated = core_bounded_summary_details(
            executor=clean_executor,
            operation=operation,
            scope=APPROVAL_V3_SCOPE,
        )
    envelope["summary"] = clean_summary
    clean_reason_summary, reason_summary_was_truncated = normalize_reason_summary_details(
        reason_summary,
        executor=clean_executor,
        operation=operation,
    )
    clean_reason_detail = sanitize_reason_detail(reason_detail)
    envelope["summary_truncated"] = bool(
        summary_was_truncated or reason_summary_was_truncated
    )
    if clean_reason_summary:
        envelope["reason_summary"] = clean_reason_summary
    if clean_reason_detail:
        envelope["reason_detail"] = clean_reason_detail
    return validate_approval_receipt_v3(envelope)


def validate_approval_receipt_v3(
    value: Any,
    *,
    executor: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    executor_run_id: str | None = None,
    request_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Validate the durable v3 task-elevation receipt fail closed."""
    if not isinstance(value, dict):
        _invalid("approval_invalid", "Approval receipt must be an object")
    receipt = copy.deepcopy(value)
    if receipt.get("version") != APPROVAL_V3_VERSION:
        _invalid(
            "approval_version_unsupported",
            f"Expected a v3 approval receipt, got version {receipt.get('version')}",
        )
    _reject_v3_sensitive_additions(receipt)
    if receipt.get("kind") != APPROVAL_KIND:
        _invalid("approval_kind_invalid", f"Approval kind must be {APPROVAL_KIND}")
    if receipt.get("scope") != APPROVAL_V3_SCOPE:
        _invalid(
            "approval_scope_invalid",
            f"Approval v3 scope must be {APPROVAL_V3_SCOPE}",
        )
    if receipt.get("elevation_mode") != APPROVAL_V3_ELEVATION_MODE:
        _invalid(
            "approval_elevation_mode_invalid",
            "Approval v3 must request contained_full elevation",
        )
    for field in (
        "task_id",
        "executor_run_id",
        "request_id",
        "request_fingerprint",
    ):
        _require_identifier(receipt.get(field), field)
    executor_name = str(receipt.get("executor") or "").strip().lower()
    _require_identifier(executor_name, "executor")
    if executor is not None and executor_name != str(executor).strip().lower():
        _invalid("approval_executor_mismatch", "Approval v3 executor mismatch")
    _require_identifier(receipt.get("session_id"), "session_id")
    expected_values = {
        "task_id": task_id,
        "executor_run_id": executor_run_id,
        "session_id": session_id,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
    }
    for field, expected in expected_values.items():
        if expected is not None and str(receipt.get(field) or "") != str(expected).strip():
            _invalid("approval_binding_mismatch", f"Approval v3 {field} mismatch")
    _require_operation(receipt.get("operation"))
    _require_summary(receipt.get("summary"))
    _require_reason_summary(receipt.get("reason_summary"))
    _require_reason_detail(receipt.get("reason_detail"))
    if not _V3_DIGEST_RE.fullmatch(str(receipt.get("path_plan_digest") or "")):
        _invalid("approval_scope_invalid", "Approval v3 requires a PathPlan digest")
    profile_digest = str(receipt.get("containment_profile_digest") or "")
    if not _V3_DIGEST_RE.fullmatch(profile_digest):
        _invalid(
            "approval_scope_invalid",
            "Approval v3 requires a containment profile digest",
        )
    authority = receipt.get("authority")
    if not isinstance(authority, dict):
        _invalid("approval_authority_invalid", "Approval v3 requires an authority object")
    if str(authority.get("executor") or "").strip().lower() != executor_name:
        _invalid("approval_authority_invalid", "authority.executor must match executor")
    protocol = str(authority.get("protocol") or "").strip()
    method = str(authority.get("method") or "").strip()
    if not protocol or not _IDENTIFIER_RE.fullmatch(protocol):
        _invalid("approval_authority_invalid", "authority.protocol is required")
    if not method or not _OPERATION_RE.fullmatch(method):
        _invalid("approval_authority_invalid", "authority.method is required")
    protocol_version = authority.get("protocol_version")
    if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
        _invalid("approval_authority_invalid", "authority.protocol_version must be an integer")
    provenance = receipt.get("provenance")
    if not isinstance(provenance, dict):
        _invalid("approval_provenance_invalid", "Approval v3 requires native provenance")
    _require_native_event(
        str(provenance.get("native_event") or ""),
        "provenance.native_event",
    )
    _require_identifier(
        str(provenance.get("tool_call_id") or ""),
        "provenance.tool_call_id",
    )
    action_fingerprint_value = str(provenance.get("action_fingerprint") or "")
    if action_fingerprint_value and not _IDENTIFIER_RE.fullmatch(action_fingerprint_value):
        _invalid("approval_provenance_invalid", "provenance.action_fingerprint is invalid")
    containment = receipt.get("containment")
    if not isinstance(containment, dict):
        _invalid("approval_containment_invalid", "Approval v3 requires containment facts")
    if containment.get("mode") != APPROVAL_V3_ELEVATION_MODE:
        _invalid("approval_containment_invalid", "Approval v3 containment mode is invalid")
    if containment.get("profile_digest") != profile_digest:
        _invalid("approval_containment_invalid", "Approval v3 profile digest is not bound")
    if containment.get("path_plan_digest") != receipt.get("path_plan_digest"):
        _invalid("approval_containment_invalid", "Approval v3 PathPlan digest is not bound")
    cardinality = receipt.get("cardinality")
    if not isinstance(cardinality, dict):
        _invalid("approval_cardinality_invalid", "Approval v3 requires cardinality facts")
    for field in _V3_CARDINALITY_FIELDS:
        count = cardinality.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > 1:
            _invalid("approval_cardinality_invalid", f"Approval v3 cardinality.{field} must be 0 or 1")
    if cardinality.get("permission_requests") != 1 or cardinality.get("elevation_receipts") != 1:
        _invalid("approval_cardinality_invalid", "Approval v3 must reserve exactly one request and receipt")
    continuation = receipt.get("continuation")
    if not isinstance(continuation, dict):
        _invalid("approval_continuation_invalid", "Approval v3 requires continuation facts")
    continuation_count = continuation.get("count")
    if isinstance(continuation_count, bool) or continuation_count not in {0, 1}:
        _invalid("approval_continuation_invalid", "Approval v3 continuation count must be 0 or 1")
    for field in ("executor_run_id", "session_id"):
        text = str(continuation.get(field) or "").strip()
        if text:
            _require_identifier(text, f"continuation.{field}")
    if continuation_count == 0 and (
        str(continuation.get("executor_run_id") or "").strip()
        or str(continuation.get("session_id") or "").strip()
    ):
        _invalid("approval_continuation_invalid", "Pending v3 continuation cannot bind a run")
    if cardinality.get("full_continuations") != continuation_count:
        _invalid("approval_cardinality_invalid", "Approval v3 continuation cardinality is inconsistent")
    state = receipt.get("state")
    if not isinstance(state, dict) or state.get("status") not in APPROVAL_STATES:
        _invalid("approval_state_invalid", "Approval v3 state must be pending or answered")
    decision = receipt.get("decision")
    if not isinstance(decision, dict):
        _invalid("approval_decision_invalid", "Approval v3 requires a decision object")
    for field in ("type", "source", "decided_at"):
        if not isinstance(decision.get(field), str):
            _invalid("approval_decision_invalid", f"Approval v3 decision.{field} must be a string")
    decision_type = str(decision.get("type") or "").strip().lower()
    decision_source = str(decision.get("source") or "").strip().lower()
    decision_at = str(decision.get("decided_at") or "").strip()
    created_at = _require_timestamp(receipt.get("created_at"), "created_at")
    if state["status"] == "pending":
        if decision_type or decision_source or decision_at:
            _invalid("approval_decision_invalid", "Pending v3 receipt must not carry a decision")
        if continuation_count != 0:
            _invalid("approval_continuation_invalid", "Pending v3 receipt cannot continue")
    else:
        if decision_type not in APPROVAL_V3_DECISION_TYPES:
            _invalid("approval_decision_invalid", f"Invalid v3 approval decision: {decision_type}")
        if decision_source not in APPROVAL_DECISION_SOURCES:
            _invalid("approval_decision_invalid", f"Invalid approval decision source: {decision_source}")
        if _require_timestamp(decision.get("decided_at"), "decision.decided_at") < created_at:
            _invalid("approval_decision_invalid", "Approval v3 decision predates receipt creation")
        if decision_type == "deny" and continuation_count != 0:
            _invalid("approval_continuation_invalid", "Denied v3 approval cannot continue")
    return receipt


def record_approval_decision_v3(
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
    """Record exactly one ``approve_full`` or ``deny`` decision."""
    receipt = validate_approval_receipt_v3(
        value,
        executor=executor,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        executor_run_id=executor_run_id,
        request_fingerprint=request_fingerprint,
    )
    decision_type = str(decision or "").strip().lower()
    decision_source = str(source or "").strip().lower()
    if decision_type not in APPROVAL_V3_DECISION_TYPES:
        _invalid("approval_decision_invalid", f"Invalid v3 approval decision: {decision}")
    if decision_source not in APPROVAL_DECISION_SOURCES:
        _invalid("approval_decision_invalid", f"Invalid approval decision source: {source}")
    state = receipt["state"]
    existing = receipt["decision"]
    if state["status"] == "answered":
        if existing.get("type") == decision_type and existing.get("source") == decision_source:
            return receipt
        _invalid("approval_replay", "Approval v3 receipt was already answered differently")
    stamp = decided_at or _utc_now()
    receipt["state"]["status"] = "answered"
    receipt["decision"] = {
        "type": decision_type,
        "source": decision_source,
        "decided_at": stamp,
    }
    receipt["cardinality"]["human_decisions"] = 1
    return validate_approval_receipt_v3(receipt)


def record_approval_full_continuation(
    value: Any,
    *,
    executor_run_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Record one authoritative contained-full continuation, idempotently."""
    receipt = validate_approval_receipt_v3(value)
    if receipt["decision"]["type"] != "approve_full":
        _invalid("approval_continuation_invalid", "Only approve_full may continue")
    run_value = _require_identifier(executor_run_id, "continuation.executor_run_id")
    session_value = _require_identifier(session_id, "continuation.session_id")
    continuation = receipt["continuation"]
    if continuation["count"] == 1:
        if continuation.get("executor_run_id") == run_value and continuation.get("session_id") == session_value:
            return receipt
        _invalid("approval_replay", "Approval v3 continuation was already recorded differently")
    continuation.update(
        {"count": 1, "executor_run_id": run_value, "session_id": session_value}
    )
    receipt["cardinality"]["full_continuations"] = 1
    return validate_approval_receipt_v3(receipt)


def record_approval_notification(value: Any) -> dict[str, Any]:
    """Reserve the single user notification/dialog on a v3 receipt."""
    receipt = validate_approval_receipt_v3(value)
    if receipt["cardinality"]["notifications"] == 1:
        return receipt
    receipt["cardinality"]["notifications"] = 1
    return validate_approval_receipt_v3(receipt)


def approval_public_projection_v3(value: Any) -> dict[str, Any]:
    """Public v3 view with only safe mode/source/state facts and digests."""
    receipt = validate_approval_receipt_v3(value)
    projection: dict[str, Any] = {
        "version": APPROVAL_V3_VERSION,
        "scope": APPROVAL_V3_SCOPE,
        "kind": receipt["kind"],
        "executor": receipt["executor"],
        "operation": receipt["operation"],
        "elevation_mode": APPROVAL_V3_ELEVATION_MODE,
        "summary": receipt["summary"],
        "summary_truncated": bool(receipt.get("summary_truncated", False)),
        "state": receipt["state"]["status"],
        "source": "task_elevation",
        "path_plan_digest": receipt["path_plan_digest"],
        "containment_profile_digest": receipt["containment_profile_digest"],
        "created_at": receipt["created_at"],
        "cardinality": dict(receipt["cardinality"]),
    }
    if receipt.get("reason_summary"):
        projection["reason_summary"] = receipt["reason_summary"]
    if receipt["state"]["status"] == "answered":
        projection.update(
            {
                "decision": receipt["decision"]["type"],
                "decision_source": receipt["decision"]["source"],
                "decided_at": receipt["decision"]["decided_at"],
            }
        )
    if receipt["continuation"]["count"]:
        projection["full_continuations"] = 1
    return projection


def validate_approval_receipt_any_version(value: Any) -> dict[str, Any]:
    """Dual-read: validate v1, v2, or v3 receipts (fail closed otherwise)."""
    if isinstance(value, dict) and value.get("version") == APPROVAL_V2_VERSION:
        return validate_approval_receipt_v2(value)
    if isinstance(value, dict) and value.get("version") == APPROVAL_V3_VERSION:
        return validate_approval_receipt_v3(value)
    return validate_approval_receipt(value)


def record_approval_selection(
    value: Any,
    handle: str,
    *,
    source: str,
    selected_at: str | None = None,
    decided_type: str,
    decided_source: str | None = None,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Record one native choice selection on a v2 receipt (idempotent replay).

    Re-recording the exact same handle and source returns the receipt
    unchanged; a different handle or a different decision is a conflicting
    replay and is rejected fail closed.
    """
    receipt = validate_approval_receipt_v2(value)
    clean_handle = str(handle or "").strip()
    clean_source = str(source or "").strip().lower()
    if clean_source not in APPROVAL_DECISION_SOURCES:
        _invalid(
            "approval_decision_invalid",
            f"Invalid approval decision source: {source}",
        )
    state = receipt["state"]
    selection = receipt["selection"]
    if state["status"] == "answered":
        existing_type = str(receipt["decision"].get("type") or "")
        if (
            str(selection.get("handle") or "") == clean_handle
            and str(selection.get("source") or "") == clean_source
            and existing_type == str(decided_type or "").strip().lower()
        ):
            return receipt
        _invalid(
            "approval_replay",
            "Approval receipt was already answered with a different choice",
        )
    matched = [c for c in receipt["choices"] if str(c.get("handle")) == clean_handle]
    if not matched:
        _invalid(
            "approval_handle_mismatch",
            "The selection handle was not offered by this request",
        )
    choice = matched[0]
    if not choice.get("selectable", True):
        _invalid(
            "approval_choice_not_selectable",
            "The selected choice was offered as non-selectable",
        )
    stamp = selected_at or _utc_now()
    receipt["selection"] = {
        "handle": clean_handle,
        "native_option_id": str(choice.get("native_option_id") or ""),
        "kind": str(choice.get("kind") or ""),
        "offered_digest": str(choice.get("offered_digest") or ""),
        "source": clean_source,
        "at": stamp,
    }
    receipt["state"]["status"] = "answered"
    clean_type = str(decided_type or "").strip().lower()
    if clean_type not in APPROVAL_DECISION_TYPES:
        _invalid(
            "approval_decision_invalid",
            f"Invalid approval decision: {decided_type}",
        )
    receipt["decision"]["type"] = clean_type
    receipt["decision"]["source"] = str(decided_source or clean_source)
    receipt["decision"]["decided_at"] = decided_at or stamp
    return validate_approval_receipt_v2(receipt)


def approval_choice_for_handle(value: Any, handle: str) -> dict[str, Any] | None:
    """Return the offered choice bound to ``handle`` on this exact receipt."""
    receipt = validate_approval_receipt_any_version(value)
    if receipt.get("version") != APPROVAL_V2_VERSION:
        return None
    clean = str(handle or "").strip()
    for choice in receipt.get("choices", []):
        if str(choice.get("handle") or "") == clean:
            return dict(choice)
    return None


def approval_public_projection_v2(value: Any) -> dict[str, Any]:
    """Public sanitized v2 view: labels/handles only, never raw payloads."""
    receipt = validate_approval_receipt_any_version(value)
    if receipt.get("version") == APPROVAL_V3_VERSION:
        return approval_public_projection_v3(receipt)
    if receipt.get("version") != APPROVAL_V2_VERSION:
        return approval_public_projection(receipt)
    decision = receipt["decision"]
    state = receipt["state"]
    projection: dict[str, Any] = {
        "version": APPROVAL_V2_VERSION,
        "scope": APPROVAL_SCOPE,
        "kind": receipt["kind"],
        "executor": receipt["executor"],
        "operation": receipt["operation"],
        "summary": receipt["summary"],
        "summary_truncated": bool(receipt.get("summary_truncated", False)),
        "state": state["status"],
        "created_at": receipt["created_at"],
        "authority": {
            "protocol": str(receipt["authority"].get("protocol") or ""),
            "protocol_version": receipt["authority"].get("protocol_version"),
            "method": str(receipt["authority"].get("method") or ""),
        },
        "choices": [
            {
                "handle": choice.get("handle"),
                "kind": choice.get("kind"),
                "label": choice.get("label"),
                "selectable": choice.get("selectable", True),
            }
            for choice in receipt.get("choices", [])
        ],
    }
    if receipt.get("reason_summary"):
        projection["reason_summary"] = receipt["reason_summary"]
    if state["status"] == "answered":
        selection = receipt.get("selection") or {}
        projection["decision"] = decision["type"]
        projection["decision_source"] = decision["source"]
        projection["decided_at"] = decision["decided_at"]
        projection["selection"] = {
            "handle": str(selection.get("handle") or ""),
            "kind": str(selection.get("kind") or ""),
            "source": str(selection.get("source") or ""),
            "at": str(selection.get("at") or ""),
        }
    return projection


def json_module_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


def _require_native_event(value: Any, field: str) -> str:
    """Validate an opaque protocol event name without treating it as a path.

    Native protocol methods such as Codex App Server
    ``item/commandExecution/requestApproval`` legitimately contain slashes.
    They are receipt data only and are never used as filesystem identifiers.
    """
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _NATIVE_EVENT_RE.fullmatch(value)
    ):
        _invalid(
            "approval_invalid",
            f"Approval receipt {field} must be a non-empty native event identifier",
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
    return _SECRET_SPACE_RE.search(value) is not None


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
        ):
            _invalid(
                "approval_sensitive_field",
                f"Approval receipt cannot persist sensitive content at: {'.'.join(key_path)}",
            )


def _reject_v3_sensitive_additions(
    value: Any,
    *,
    key_path: tuple[str, ...] = (),
) -> None:
    """Reject raw execution material while allowing digest-only v3 scope."""
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in {re.sub(r"[^a-z0-9]", "", field) for field in _V3_LEGACY_FIELDS}:
                _invalid(
                    "approval_legacy_field_rejected",
                    f"Approval v3 cannot carry legacy field: {'.'.join((*key_path, key))}",
                )
            digest_field = normalized in {
                "pathplandigest",
                "containmentprofiledigest",
                "profiledigest",
            }
            if any(part in normalized for part in _FORBIDDEN_FIELD_PARTS) or (
                "path" in normalized and not digest_field
            ):
                _invalid(
                    "approval_sensitive_field",
                    f"Approval v3 cannot persist sensitive field: {'.'.join((*key_path, key))}",
                )
            _reject_v3_sensitive_additions(item, key_path=(*key_path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_v3_sensitive_additions(item, key_path=(*key_path, str(index)))
    elif isinstance(value, str):
        clean = value.strip()
        if (
            clean.startswith(("/", "~/"))
            or _SECRET_ASSIGNMENT_RE.search(clean)
            or _SECRET_SPACE_RE.search(clean)
        ):
            _invalid(
                "approval_sensitive_field",
                f"Approval v3 cannot persist sensitive content at: {'.'.join(key_path)}",
            )


def _remove_control_characters(value: str) -> str:
    # Replace control characters with a normal space so adjacent words never get
    # glued together; callers collapse runs of whitespace afterwards.
    return "".join(" " if _is_control_character(char) else char for char in value)


def _is_control_character(char: str) -> bool:
    code = ord(char)
    return code < 32 or code == 127


def _bound_text(value: str, limit: int) -> str:
    return _bounded_text_with_truncation(value, limit)[0]


def _bounded_text_with_truncation(value: Any, limit: int) -> tuple[str, bool]:
    """Normalize and bound one user-facing summary with explicit metadata."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    if limit == 1:
        return SUMMARY_ELLIPSIS, True
    return text[: limit - 1].rstrip() + SUMMARY_ELLIPSIS, True


def core_bounded_summary_details(
    *,
    executor: str,
    operation: str,
    scope: str = APPROVAL_SCOPE,
    kind: str = APPROVAL_KIND,
) -> tuple[str, bool]:
    """Return the Core summary plus whether its bound removed content."""
    text = _core_summary_text(
        executor=executor,
        operation=operation,
        kind=kind,
    )
    return _bounded_text_with_truncation(text, APPROVAL_SUMMARY_LIMIT)


def _invalid(code: str, message: str) -> None:
    raise ABCError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
