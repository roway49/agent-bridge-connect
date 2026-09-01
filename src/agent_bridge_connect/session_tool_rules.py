"""PERM-104-002 1.04A: session tool rules are REMOVED (tombstone module).

The legacy CLI matcher grammar (``--approve-tool <matcher> --scope session``)
and its rule APIs/lifecycle were retired by the executor-native choice broker
(``agentbc.approval`` v2 / ``task respond --permission-option <handle>``).

This module keeps ONLY:

* the stable tombstone error code ``legacy_session_tool_rule_removed``;
* the historical receipt extension key and an audit-only public projection,
  so status/report views of terminal tasks that already carry a receipt stay
  readable without rewriting history.

No new receipt can ever be issued, applied, revoked, or re-derived; every
former entry point fails closed.  Historical receipts are never converted
into v2 choices or any other permission.
"""

from __future__ import annotations

from typing import Any

# Stable tombstone code surfaced to CLI/scripts that still try the old path.
LEGACY_SESSION_TOOL_RULE_REMOVED = "legacy_session_tool_rule_removed"

# Kept for dual-read of historical task records (audit-only).
SESSION_RULE_RECEIPT_EXTENSION_KEY = "agentbc.session_tool_rule"

# Historical selection source value (read-only evidence of the removed path).
SESSION_RULE_SELECTION_SOURCE = "cli_native_approval"


def session_rule_receipt_public_projection(value: Any) -> dict[str, Any] | None:
    """Audit-only public projection of a HISTORICAL receipt.

    Exposed: matcher display, scope, state, redacted binding digests and
    timestamps.  Never exposed: raw session/request/tool_use identifiers or
    executor internals.  The projection is read-only evidence; the receipt it
    describes can no longer grant anything.
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
    return {
        "version": value.get("version"),
        "retired": True,
        "retirement_code": LEGACY_SESSION_TOOL_RULE_REMOVED,
        "selection_source": str(value.get("selection_source") or ""),
        "scope": str(value.get("scope") or ""),
        "state": str(state.get("status") or ""),
        "revoked": state.get("revoked") is True,
        "revocation_code": str(state.get("revocation_code") or ""),
        "matcher": str(matcher.get("display") or ""),
        "matcher_kind": str(matcher.get("kind") or ""),
        "binding_digest": _short_digest(binding.get("binding_digest")),
        "profile_digest": _short_digest(binding.get("profile_digest")),
        "created_at": str(audit.get("created_at") or ""),
        "updated_at": str(audit.get("updated_at") or ""),
        "revoked_at": str(state.get("revoked_at") or ""),
    }


# Public alias kept for the status/report projections.
session_rule_public_projection = session_rule_receipt_public_projection


__all__ = [
    "LEGACY_SESSION_TOOL_RULE_REMOVED",
    "SESSION_RULE_RECEIPT_EXTENSION_KEY",
    "SESSION_RULE_SELECTION_SOURCE",
    "session_rule_public_projection",
    "session_rule_receipt_public_projection",
]
