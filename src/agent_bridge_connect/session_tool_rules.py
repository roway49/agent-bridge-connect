"""PERM-104-002: trusted CLI session-scoped tool-use permission rules.

An explicit AgentBC CLI decision
(``agentbc task respond <task-id> --input <input-id> --approve-tool
<tool-matcher> --scope session``) may translate ONE trusted pending native
Claude permission input into the official SDK session-scoped
``PermissionUpdate(type="addRules", ...)`` allow rule on the same live
transport session.

Fail-closed authority: every field of the decision is validated against the
persisted ``agentbc.input`` request — task id, executor run, official session,
request id, ``tool_use_id``, request/action fingerprints, escalation domain
and host profile digest.  Missing, stale, answered, mismatched, wildcard-all,
unsupported-executor, callback/full-fallback, and other non-native requests
are rejected with stable error codes before anything is granted.

This module owns the decision authority, the narrow matcher grammar, and the
task-scoped receipt lifecycle (issue / idempotent replay / terminal
revocation / public projection).  Applying the rule to the live SDK client is
owned by :mod:`agent_bridge_connect.claude_sdk_transport`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SESSION_RULE_RECEIPT_VERSION = 1
SESSION_RULE_RECEIPT_EXTENSION_KEY = "agentbc.session_tool_rule"
SESSION_RULE_SCOPE = "session"
SESSION_RULE_SELECTION_SOURCE = "cli_native_approval"

# Stable fail-closed error codes.
SESSION_RULE_INPUT_MISSING = "session_rule_input_missing"
SESSION_RULE_INPUT_NOT_PERMISSION = "session_rule_input_not_permission"
SESSION_RULE_INPUT_NOT_NATIVE = "session_rule_input_not_native"
SESSION_RULE_INPUT_STALE = "session_rule_input_stale"
SESSION_RULE_INPUT_ANSWERED = "session_rule_input_answered"
SESSION_RULE_IDENTITY_MISMATCH = "session_rule_identity_mismatch"
SESSION_RULE_MATCHER_INVALID = "session_rule_matcher_invalid"
SESSION_RULE_MATCHER_WILDCARD = "session_rule_matcher_wildcard"
SESSION_RULE_EXECUTOR_UNSUPPORTED = "session_rule_executor_unsupported"
SESSION_RULE_ALREADY_ACTIVE = "session_rule_already_active"
SESSION_RULE_REPLAY_CONFLICT = "session_rule_replay_conflict"
SESSION_RULE_REVOKED = "session_rule_revoked"

# Only executors with a live SDK control transport carrying an official
# session-scoped PermissionUpdate path may receive a session rule.
SUPPORTED_SESSION_RULE_EXECUTORS = frozenset({"claude"})
# Only the official SDK control transport can apply the update on the same
# live session; callback/stderr/prose-derived paths never qualify.
SUPPORTED_SESSION_RULE_CONTROL_PATHS = frozenset({"sdk_control_transport"})
SUPPORTED_SESSION_RULE_NATIVE_EVENTS = frozenset({"claude_sdk_can_use_tool"})

_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# command() rule content: printable ASCII, no control characters, no
# unclosed wildcard-all.  ``Bash`` alone (no parens) allows every Bash
# invocation and is therefore rejected; only bounded tool matchers or
# ``Tool(command prefix)`` matchers are accepted.
_COMMAND_RULE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]{0,63}\([^()]{1,240}\)$"
)


class SessionRuleError(Exception):
    """Fail-closed session-rule rejection with a stable error code."""

    status = "needs_recovery"

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "session_rule_invalid")
        self.details = dict(details or {})
        super().__init__(message)


# ---------------------------------------------------------------------------
# Matcher grammar (narrow, fail-closed)
# ---------------------------------------------------------------------------


def normalize_tool_matcher(value: Any) -> dict[str, str]:
    """Validate and normalize one ``--approve-tool`` matcher.

    Accepted shapes (the narrowest supported allow rules):

    * ``Bash(command prefix*)`` — official command-prefix rule for one tool;
    * ``WebFetch(domain:example.com)`` — any official content rule whose
      bound tool name matches the wrapper.

    Rejected: bare tool names (an allow-all for that tool), ``*`` wildcards,
    empty or control-character content, ``*``/empty command bodies, and any
    matcher longer than the bounded grammar above.
    """
    text = str(value or "").strip()
    if not text:
        raise SessionRuleError(
            SESSION_RULE_MATCHER_INVALID,
            "A session tool rule requires an explicit tool matcher, such as "
            "Bash(echo probe*).",
        )
    if text == "*" or _TOOL_NAME_RE.fullmatch(text):
        raise SessionRuleError(
            SESSION_RULE_MATCHER_WILDCARD,
            "A session tool rule must be bounded: bare tool names and '*' "
            "wildcards are rejected. Use Tool(command prefix*) instead.",
            {"matcher": text[:120]},
        )
    match = _COMMAND_RULE_RE.fullmatch(text)
    if match is None:
        raise SessionRuleError(
            SESSION_RULE_MATCHER_INVALID,
            "The tool matcher does not match the supported "
            "Tool(command-content) grammar.",
            {"matcher": text[:120]},
        )
    tool_name = text[: text.index("(")].strip()
    content = text[text.index("(") + 1 : text.rindex(")")].strip()
    if not _TOOL_NAME_RE.fullmatch(tool_name):
        raise SessionRuleError(
            SESSION_RULE_MATCHER_INVALID,
            "The tool matcher names an unsupported tool.",
            {"matcher": text[:120]},
        )
    if not content or content.strip() == "*":
        raise SessionRuleError(
            SESSION_RULE_MATCHER_WILDCARD,
            "A session tool rule content must be a bounded command or "
            "domain prefix, not '*'.",
            {"matcher": text[:120]},
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in content):
        raise SessionRuleError(
            SESSION_RULE_MATCHER_INVALID,
            "The tool matcher content contains control characters.",
            {"matcher": text[:120]},
        )
    return {"tool_name": tool_name, "rule_content": content, "matcher": text}


def session_rule_binding_digest(request: dict[str, Any]) -> str:
    """Redacted digest of the exact native binding a rule was issued for."""
    payload = json.dumps(
        {
            "task_id": str(request.get("task_id") or ""),
            "executor_run_id": str(request.get("executor_run_id") or ""),
            "session_id": str(request.get("session_id") or ""),
            "request_id": str(request.get("request_id") or ""),
            "tool_use_id": str(request.get("tool_use_id") or ""),
            "request_fingerprint": str(request.get("request_fingerprint") or ""),
            "action_fingerprint": str(request.get("action_fingerprint") or ""),
            "matcher": str(request.get("matcher") or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Decision authority: the persisted agentbc.input request
# ---------------------------------------------------------------------------


def validate_session_rule_request(
    task_id: str,
    request: Any,
    *,
    executor: str,
    executor_run_id: str,
    session_id: str,
    matcher_value: Any,
) -> dict[str, Any]:
    """Validate one pending native permission input for a session rule.

    Returns the normalized binding (including the parsed matcher).  Every
    rejection is a :class:`SessionRuleError` with a stable fail-closed code;
    nothing is granted on any mismatch.
    """
    if not isinstance(request, dict):
        raise SessionRuleError(
            SESSION_RULE_INPUT_MISSING,
            "The task has no persisted input request to answer.",
        )
    if str(request.get("type") or "") != "permission":
        raise SessionRuleError(
            SESSION_RULE_INPUT_NOT_PERMISSION,
            "Session tool rules apply only to permission inputs.",
            {"input_type": str(request.get("type") or "")},
        )
    # The rule follows a RECORDED decision: only an answered request can
    # carry the approved decision this rule translates.  A waiting, expired,
    # cancelled or superseded request is stale for rule purposes.
    if str(request.get("status") or "") != "answered":
        raise SessionRuleError(
            SESSION_RULE_INPUT_STALE,
            "The input request has no recorded answer to translate.",
            {"input_status": str(request.get("status") or "")},
        )
    if str(request.get("scope") or "") != "single_action" or not str(
        request.get("request_id") or ""
    ).strip():
        # Compatibility/full-fallback inputs (callback-derived, dialog-only)
        # never carry the full native binding; they are rejected here.
        raise SessionRuleError(
            SESSION_RULE_INPUT_NOT_NATIVE,
            "Session tool rules require the native single_action approval "
            "request (claude_sdk_can_use_tool via sdk_control_transport).",
        )
    normalized_executor = str(executor or "").strip().lower()
    if normalized_executor not in SUPPORTED_SESSION_RULE_EXECUTORS:
        raise SessionRuleError(
            SESSION_RULE_EXECUTOR_UNSUPPORTED,
            "The task executor has no official session-scoped permission "
            "rule path.",
            {"executor": normalized_executor},
        )
    matcher = normalize_tool_matcher(matcher_value)
    expected = {
        "task_id": str(task_id or "").strip(),
        "executor_run_id": str(executor_run_id or "").strip(),
        "session_id": str(session_id or "").strip(),
        "request_id": str(request.get("request_id") or "").strip(),
        "tool_use_id": str(request.get("tool_use_id") or "").strip(),
        "request_fingerprint": str(request.get("request_fingerprint") or "").strip(),
        "action_fingerprint": str(request.get("action_fingerprint") or "").strip(),
    }
    missing = sorted(key for key, value in expected.items() if not value)
    if missing:
        raise SessionRuleError(
            SESSION_RULE_IDENTITY_MISMATCH,
            "The input request is missing required native binding fields.",
            {"missing": missing},
        )
    if str(request.get("control_path") or "") not in SUPPORTED_SESSION_RULE_CONTROL_PATHS:
        raise SessionRuleError(
            SESSION_RULE_INPUT_NOT_NATIVE,
            "The input request did not originate from the official SDK "
            "control transport.",
            {"control_path": str(request.get("control_path") or "")},
        )
    if str(request.get("native_event") or "") not in SUPPORTED_SESSION_RULE_NATIVE_EVENTS:
        raise SessionRuleError(
            SESSION_RULE_INPUT_NOT_NATIVE,
            "The input request did not originate from the official Claude "
            "SDK can_use_tool event.",
            {"native_event": str(request.get("native_event") or "")},
        )
    if not str(request.get("escalation_domain") or ""):
        raise SessionRuleError(
            SESSION_RULE_IDENTITY_MISMATCH,
            "The input request is missing its escalation domain binding.",
        )
    if not str(request.get("profile_digest") or "").startswith("sha256:"):
        raise SessionRuleError(
            SESSION_RULE_IDENTITY_MISMATCH,
            "The input request is missing its host profile digest binding.",
        )
    binding = dict(expected)
    binding["matcher"] = matcher["matcher"]
    binding["tool_name"] = matcher["tool_name"]
    binding["rule_content"] = matcher["rule_content"]
    binding["escalation_domain"] = str(request.get("escalation_domain") or "").strip()
    binding["profile_digest"] = str(request.get("profile_digest") or "").strip()
    return binding


# ---------------------------------------------------------------------------
# Task-scoped receipt: issue, replay, revoke, project
# ---------------------------------------------------------------------------


def build_session_rule_receipt(
    binding: dict[str, Any],
    *,
    input_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Create the durable task-scoped session-rule receipt (active)."""
    if not str(input_id or "").strip() or not str(created_at or "").strip():
        raise SessionRuleError(
            SESSION_RULE_IDENTITY_MISMATCH,
            "A session rule receipt requires the input id and timestamp.",
        )
    return {
        "version": SESSION_RULE_RECEIPT_VERSION,
        "selection_source": SESSION_RULE_SELECTION_SOURCE,
        "scope": SESSION_RULE_SCOPE,
        "state": {
            "status": "active",
            "revoked": False,
            "revocation_code": "",
            "revoked_at": "",
        },
        "matcher": {
            "tool_name": str(binding.get("tool_name") or ""),
            "rule_content": str(binding.get("rule_content") or ""),
            "display": str(binding.get("matcher") or ""),
        },
        "binding": {
            "input_id": str(input_id),
            "task_id": str(binding.get("task_id") or ""),
            "executor_run_id": str(binding.get("executor_run_id") or ""),
            "session_id": str(binding.get("session_id") or ""),
            "request_id": str(binding.get("request_id") or ""),
            "tool_use_id": str(binding.get("tool_use_id") or ""),
            "request_fingerprint": str(
                binding.get("request_fingerprint") or ""
            ),
            "action_fingerprint": str(
                binding.get("action_fingerprint") or ""
            ),
            "escalation_domain": str(
                binding.get("escalation_domain") or ""
            ),
            "profile_digest": str(binding.get("profile_digest") or ""),
            "binding_digest": session_rule_binding_digest(binding),
        },
        "audit": {
            "created_at": str(created_at),
            "updated_at": str(created_at),
        },
    }


def issue_session_rule_receipt(
    extensions: dict[str, Any],
    binding: dict[str, Any],
    *,
    input_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Return the receipt to persist for this response (idempotent replay).

    A second ``--approve-tool`` response for the same input never creates a
    second rule: an identical replay returns the existing receipt unchanged,
    a conflicting replay (different matcher or binding for the same input) is
    rejected.  A different active rule for the same session also fails
    closed — one session carries at most one CLI-issued rule.
    """
    existing = extensions.get(SESSION_RULE_RECEIPT_EXTENSION_KEY)
    receipt = build_session_rule_receipt(binding, input_id=input_id, created_at=created_at)
    if isinstance(existing, dict):
        existing_state = existing.get("state") or {}
        if existing_state.get("revoked") is not True:
            existing_binding = existing.get("binding") or {}
            if str(existing_binding.get("input_id") or "") == str(input_id):
                if existing.get("matcher", {}) != receipt["matcher"] or str(
                    existing_binding.get("binding_digest") or ""
                ) != str(receipt["binding"]["binding_digest"]):
                    raise SessionRuleError(
                        SESSION_RULE_REPLAY_CONFLICT,
                        "The input was already answered with a different "
                        "session rule binding.",
                    )
                return existing
            if existing_state.get("status") == "active":
                raise SessionRuleError(
                    SESSION_RULE_ALREADY_ACTIVE,
                    "The session already carries an active session tool "
                    "rule; revoke it before issuing another.",
                    {"active_input_id": str(existing_binding.get("input_id") or "")},
                )
    return receipt


def revoke_session_rule_receipt(
    value: Any,
    code: str,
    *,
    revoked_at: str | None = None,
) -> dict[str, Any] | None:
    """Mark the receipt terminal (idempotent); ``None`` when no receipt.

    The in-memory rule on the transport dies with the same signal that ends
    the run; the receipt records the durable terminal state so retry,
    handoff, restart and recovery can never inherit the grant.
    """
    if not isinstance(value, dict):
        return None
    state = value.get("state") if isinstance(value.get("state"), dict) else {}
    if state.get("revoked") is True:
        return value
    stamp = str(revoked_at or "").strip()
    value["state"] = {
        "status": "revoked",
        "revoked": True,
        "revocation_code": str(code or "session_rule_revoked"),
        "revoked_at": stamp,
    }
    audit = value.get("audit") if isinstance(value.get("audit"), dict) else {}
    audit["updated_at"] = stamp
    value["audit"] = audit
    return value


def session_rule_receipt_active(value: Any) -> bool:
    """Return whether a receipt currently authorizes the session rule."""
    if not isinstance(value, dict):
        return False
    state = value.get("state") if isinstance(value.get("state"), dict) else {}
    return state.get("status") == "active" and state.get("revoked") is not True


def session_rule_receipt_for_session(value: Any, session_id: str) -> bool:
    """Return whether an active receipt exists for the exact session."""
    if not session_rule_receipt_active(value):
        return False
    binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
    return bool(str(binding.get("session_id") or "")) and str(
        binding.get("session_id")
    ) == str(session_id or "").strip()


def session_rule_receipt_public_projection(value: Any) -> dict[str, Any] | None:
    """Return the sanitized public view allowed in status/report.

    Exposed: matcher, session scope, selection source, redacted binding
    digests, timestamps, active/revoked state, and stable error codes.
    Never exposed: raw session/request/tool_use identifiers, fingerprints
    beyond their digests, or executor internals.
    """

    def _short_digest(value_text: Any) -> str:
        text = str(value_text or "")
        if text.startswith("sha256:") and len(text) > len("sha256:") + 8:
            return f"sha256:{text[len('sha256:') : len('sha256:') + 12]}…"
        return ""

    if not isinstance(value, dict):
        return None
    matcher = value.get("matcher") if isinstance(value.get("matcher"), dict) else {}
    binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
    state = value.get("state") if isinstance(value.get("state"), dict) else {}
    audit = value.get("audit") if isinstance(value.get("audit"), dict) else {}
    projection: dict[str, Any] = {
        "version": value.get("version"),
        "selection_source": str(value.get("selection_source") or ""),
        "scope": str(value.get("scope") or ""),
        "state": str(state.get("status") or ""),
        "revoked": state.get("revoked") is True,
        "revocation_code": str(state.get("revocation_code") or ""),
        "matcher": str(matcher.get("display") or ""),
        "binding_digest": _short_digest(binding.get("binding_digest")),
        "profile_digest": _short_digest(binding.get("profile_digest")),
        "created_at": str(audit.get("created_at") or ""),
        "updated_at": str(audit.get("updated_at") or ""),
        "revoked_at": str(state.get("revoked_at") or ""),
    }
    return projection


# Public alias used by the status/report projections.
session_rule_public_projection = session_rule_receipt_public_projection


__all__ = [
    "SESSION_RULE_ALREADY_ACTIVE",
    "SESSION_RULE_EXECUTOR_UNSUPPORTED",
    "SESSION_RULE_IDENTITY_MISMATCH",
    "SESSION_RULE_INPUT_ANSWERED",
    "SESSION_RULE_INPUT_MISSING",
    "SESSION_RULE_INPUT_NOT_NATIVE",
    "SESSION_RULE_INPUT_NOT_PERMISSION",
    "SESSION_RULE_INPUT_STALE",
    "SESSION_RULE_MATCHER_INVALID",
    "SESSION_RULE_MATCHER_WILDCARD",
    "SESSION_RULE_RECEIPT_EXTENSION_KEY",
    "SESSION_RULE_RECEIPT_VERSION",
    "SESSION_RULE_REPLAY_CONFLICT",
    "SESSION_RULE_REVOKED",
    "SESSION_RULE_SCOPE",
    "SESSION_RULE_SELECTION_SOURCE",
    "SUPPORTED_SESSION_RULE_CONTROL_PATHS",
    "SUPPORTED_SESSION_RULE_EXECUTORS",
    "SUPPORTED_SESSION_RULE_NATIVE_EVENTS",
    "SessionRuleError",
    "build_session_rule_receipt",
    "issue_session_rule_receipt",
    "normalize_tool_matcher",
    "revoke_session_rule_receipt",
    "session_rule_binding_digest",
    "session_rule_public_projection",
    "session_rule_receipt_active",
    "session_rule_receipt_for_session",
    "session_rule_receipt_public_projection",
    "validate_session_rule_request",
]
