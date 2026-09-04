"""Narrow Hermes ACP session-first transport (PERM-103-003 / PERM-103-004).

This module is the Task 6 Hermes control path: it speaks the official Agent
Client Protocol (ACP) over stdio to a spawned ``hermes acp`` process through
the configured executable, and it bridges ``session/request_permission`` into
the executor-neutral approval receipt and ControlPlane **without changing
those public interfaces**.

Contract invariants (fail closed):

* Session-first ordering: the official ``session/new`` (fresh) or explicit
  ``session/load`` (resume) must complete and be persisted as the
  task/run-bound official session receipt **before** ``session/prompt`` is
  ever sent.  The transport itself never persists anything - it returns the
  official session ID so the adapter can persist the receipt through the
  frozen ``SessionFirstGate`` before calling :meth:`HermesAcpTransport.prompt`.
* No private scanning: the transport never reads Hermes databases, logs, or
  process tables, never uses ``--last`` / ``--continue`` / ``--accept-hooks``
  / ``--yolo`` flags, and never overrides global configuration.  Full mode is
  expressed exclusively through the registry-frozen subprocess-scoped
  ``HERMES_YOLO_MODE`` environment on the spawned ACP subprocess.
* Strict framing: every stdin/stdout frame must be a JSON object; malformed,
  duplicate, or out-of-order frames fail closed before any unsafe execution.
* Strict versioning: ``initialize`` must return exactly the supported
  protocol version (``1``); anything else is ``hermes_acp_unsupported_version``.
* Canonical ACP wire boundary (PERM-104-002): every inbound
  ``session/request_permission`` frame is validated **mechanically** against
  the canonical ACP camelCase field names (``params.sessionId``,
  ``params.toolCall.toolCallId``, ``params.options[].optionId/kind/name``)
  exactly once, then converted into one typed
  :class:`HermesAcpPermissionRequest`.  All later code consumes that
  normalized object; it never re-probes the raw dictionaries.  AgentBC
  internal names (``session_id``, ``tool_call_id``, ``native_option_id``)
  exist only *after* normalization and are never accepted as an alternative
  raw wire protocol: a frame that mixes canonical and snake_case fields,
  omits a canonical field, or repeats an optionId fails closed.  No fuzzy
  field matching and no executor-version branching exists.
* Every native option the ACP agent offers is persisted verbatim with its
  exact ``optionId``, ``kind`` and ``name`` - ``allow_once``,
  ``allow_session``, ``allow_always``, ``deny``, ``deny_always`` and any
  other id alike.  AgentBC never infers a permission category, never
  synthesizes a matcher, never mints a grant and never turns a choice into
  full mode.  A closed, documented option-id -> 1.04A dialog-role table is
  the only thing that decides whether a choice is actionable; options
  outside that table (``allow_always``, ``deny_always``, unknown ids) stay
  visible in the audit receipt and stay non-actionable.
* Exact response path: the selected original ``optionId`` is returned
  verbatim on the same JSON-RPC request in the canonical ACP
  ``SelectedPermissionOutcome`` shape.  View Details, Approve and Back are
  navigation only and send nothing.  Duplicate/concurrent requests, requests
  for a different official session, mismatched identities, missing choices
  and late responses fail closed.
* Transport death (broken pipe, process exit, EOF, timeout) surfaces as an
  explicit failure so the adapter can enter ``needs_recovery``; nothing is
  retried silently.

The module is deliberately dependency-free (AgentBC has no runtime
dependencies) and mirrors the official ``agent-client-protocol`` wire shapes
with camelCase field aliases.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Official ACP protocol version supported by this transport (the installed
# ``agent-client-protocol`` SDK pins ``PROTOCOL_VERSION = 1``).
HERMES_ACP_PROTOCOL_VERSION = 1
HERMES_ACP_CLIENT_NAME = "agentbc"
HERMES_ACP_CLIENT_VERSION = "1.0.4A"

# ---------------------------------------------------------------------------
# Canonical ACP wire names (PERM-104-002).
#
# These are the ONLY raw field names the wire boundary accepts for a
# ``session/request_permission`` request.  They were taken verbatim from the
# installed Hermes ACP adapter's pinned ``agent-client-protocol`` schema
# (``RequestPermissionRequest`` / ``ToolCallUpdate`` / ``PermissionOption``),
# which the agent serialises with ``model_dump(by_alias=True)``.  AgentBC's
# own snake_case names exist only *after* normalization and are never
# accepted as an alternative raw protocol.
# ---------------------------------------------------------------------------
ACP_FIELD_SESSION_ID = "sessionId"
ACP_FIELD_TOOL_CALL = "toolCall"
ACP_FIELD_TOOL_CALL_ID = "toolCallId"
ACP_FIELD_OPTIONS = "options"
ACP_FIELD_OPTION_ID = "optionId"
ACP_FIELD_OPTION_KIND = "kind"
ACP_FIELD_OPTION_NAME = "name"
ACP_FIELD_META = "_meta"

# Canonical ``session/update`` notification fields (PERM-104-001).  The pinned
# ``agent-client-protocol`` schema serialises ``SessionNotification`` with
# ``sessionId`` + ``update`` and ``AgentMessageChunk`` with the
# ``sessionUpdate`` discriminator plus one single ``content`` content block.
ACP_FIELD_UPDATE = "update"
ACP_FIELD_SESSION_UPDATE = "sessionUpdate"
ACP_FIELD_CONTENT = "content"
ACP_FIELD_TEXT = "text"
ACP_FIELD_STOP_REASON = "stopReason"
_HERMES_ACP_AGENT_MESSAGE_UPDATES = frozenset({"agent_message_chunk", "agent_message"})
_HERMES_ACP_MESSAGE_WIRE_FIELDS = frozenset(
    {
        ACP_FIELD_META,
        ACP_FIELD_SESSION_UPDATE,
        ACP_FIELD_CONTENT,
    }
)

# ``ToolCallUpdate`` field set on the wire.  ``content`` and ``rawInput`` are
# carried opaquely: only their existence is validated, never their shape.
_TOOL_CALL_WIRE_FIELDS = frozenset(
    {
        ACP_FIELD_META,
        "content",
        "kind",
        "locations",
        "rawInput",
        "rawOutput",
        "status",
        "title",
        ACP_FIELD_TOOL_CALL_ID,
    }
)
_PERMISSION_PARAMS_WIRE_FIELDS = frozenset(
    {
        ACP_FIELD_META,
        ACP_FIELD_OPTIONS,
        ACP_FIELD_SESSION_ID,
        ACP_FIELD_TOOL_CALL,
    }
)
_PERMISSION_OPTION_WIRE_FIELDS = frozenset(
    {
        ACP_FIELD_META,
        ACP_FIELD_OPTION_ID,
        ACP_FIELD_OPTION_KIND,
        ACP_FIELD_OPTION_NAME,
    }
)

# AgentBC-internal names for the canonical wire fields.  These are produced
# only *after* decoding; on a raw frame they indicate a mixed/non-canonical
# structure and are rejected instead of being probed as a fallback.
_INTERNAL_ALIASES = {
    ACP_FIELD_SESSION_ID: "session_id",
    ACP_FIELD_TOOL_CALL: "tool_call",
    ACP_FIELD_TOOL_CALL_ID: "tool_call_id",
    ACP_FIELD_OPTION_ID: "option_id",
}
_INTERNAL_ALIAS_NAMES = frozenset(set(_INTERNAL_ALIASES.values()) | {"id"})

# Official Hermes ACP permission option ids, frozen from the installed Hermes
# ACP adapter's ``_build_permission_options``.  This table decides ONLY which
# 1.04A dialog button an offered choice is bound to.  It is not a capability
# gate and not a version gate: every offered option is persisted verbatim, and
# an option id outside this table is preserved with role ``other`` (visible in
# the audit receipt, non-actionable in the 1.04A dialog).
HERMES_ACP_ALLOW_ONCE_OPTION_ID = "allow_once"
HERMES_ACP_ALLOW_SESSION_OPTION_ID = "allow_session"
HERMES_ACP_ALLOW_ALWAYS_OPTION_ID = "allow_always"
HERMES_ACP_DENY_OPTION_ID = "deny"
HERMES_ACP_DENY_ALWAYS_OPTION_ID = "deny_always"
HERMES_ACP_NATIVE_OPTION_IDS = (
    HERMES_ACP_ALLOW_ONCE_OPTION_ID,
    HERMES_ACP_ALLOW_SESSION_OPTION_ID,
    HERMES_ACP_ALLOW_ALWAYS_OPTION_ID,
    HERMES_ACP_DENY_OPTION_ID,
    HERMES_ACP_DENY_ALWAYS_OPTION_ID,
)

# 1.04A dialog roles.  Exactly one offered choice may carry each actionable
# role, so a button can never be ambiguous.
_HERMES_ACP_DIALOG_ROLES = {
    HERMES_ACP_ALLOW_ONCE_OPTION_ID: "once",
    HERMES_ACP_ALLOW_SESSION_OPTION_ID: "session",
    HERMES_ACP_DENY_OPTION_ID: "deny",
}
# Canonical ACP ``PermissionOptionKind`` fallback, used only for an optionId
# outside the Hermes table.  Persistent kinds never become actionable.
_HERMES_ACP_KIND_ROLES = {
    "allow_once": "once",
    "reject_once": "deny",
}
HERMES_ACP_ROLE_OTHER = "other"

# The 1.04A dialog never exposes a persistent-scope choice: allow_always and
# deny_always stay in the audit receipt but are not actionable.
HERMES_ACP_NON_ACTIONABLE_OPTION_IDS = frozenset(
    {
        HERMES_ACP_ALLOW_ALWAYS_OPTION_ID,
        HERMES_ACP_DENY_ALWAYS_OPTION_ID,
    }
)

# Legacy one-shot fallback mapping targets (v1 decisions without a native
# choice payload).  ``allow_once`` is the exact official option id Hermes
# offers for a one-shot approval; ``cancelled`` is the ACP ``DeniedOutcome``
# discriminator and is NOT one of Hermes' offered options.
HERMES_ACP_ALLOW_ONCE_OPTION = HERMES_ACP_ALLOW_ONCE_OPTION_ID
HERMES_ACP_DENIED_OUTCOME = "cancelled"
HERMES_ACP_SELECTED_OUTCOME = "selected"

# Frozen capability id (registry) bound by this transport.
HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID = "hermes.acp.session.request_permission"

# Default per-RPC bounds.  The whole prompt turn is bounded by the adapter's
# safety runtime instead of this value.
HERMES_ACP_RPC_TIMEOUT_S = 30.0
_HERMES_ACP_START_TIMEOUT_S = 60.0

# PERM-104-001: a healthy Hermes turn is NOT bounded by the per-RPC handshake
# timeout.  A single model call or tool call can stay silent on the ACP stream
# far longer than 30s (TJBS-001 run ``hermes-TJBS-001-46b34d3d`` timed out
# after a 45.1s model call and a tool execution that emitted no frame).  The
# transport therefore separates three independent bounds:
#
# * ``rpc_timeout_s``     - one handshake RPC (initialize/new/load) and the
#                           per-``select`` poll slice inside every receive loop.
# * ``receive_timeout_s`` - the longest fully silent interval AgentBC tolerates
#                           from a *live* ACP process while a turn runs.  This
#                           is an idle guard, never a turn deadline: expiring
#                           it is a truthful ``hermes_acp_receive_idle_timeout``
#                           failure, not a silent retry and not a fake success.
# * the overall prompt deadline passed by the adapter (the safety runtime).
#
# Nothing here converts a hung transport into success: an idle expiry, an EOF
# and a process exit stay three distinct, classified failures.
HERMES_ACP_RECEIVE_TIMEOUT_S = 900.0

# Bounded assistant-text budget for one turn.  The budget is large enough for a
# full Hermes answer (TJBS-001's real answer plus its marker was ~1.3 KB) and,
# when it is ever exceeded, the head AND the tail are both preserved so the
# terminal marker line can never be truncated away again.
HERMES_ACP_MESSAGE_MAX_BYTES = 1_048_576
HERMES_ACP_MESSAGE_TAIL_BYTES = 65_536

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_TOOL_CALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_SUMMARY_LIMIT = 240
_STDERR_KEEP_BYTES = 8192
_HERMES_ACP_POLL_SLICE_S = 0.05
_HERMES_ACP_READ_CHUNK_BYTES = 1_048_576
_HERMES_ACP_INBOUND_MAX_BYTES = 16_777_216

_JSONRPC = "2.0"


class HermesAcpError(RuntimeError):
    """Fail-closed transport error with a stable code and bounded evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "hermes_acp_error")
        self.details = dict(details or {})
        super().__init__(message)

    def __str__(self) -> str:
        # The stable code prefixes the human message so failure evidence and
        # audit trails always carry the exact fail-closed reason.
        return f"{self.code}: {super().__str__()}"


class HermesAcpUnsupported(HermesAcpError):
    """The installed Hermes ACP surface cannot express this contract."""


class HermesAcpTimeout(HermesAcpError, TimeoutError):
    """A Hermes ACP wait expired.

    Subclasses both :class:`HermesAcpError` (stable code + bounded evidence)
    and :class:`TimeoutError` so the adapter keeps reporting
    ``timeout_is_failure=True`` - a timeout is a real failure and never a
    silent retry or a synthetic completion.
    """


def _bounded_text(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    text = "" if value is None else str(value)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
    if len(cleaned) > limit:
        return cleaned[:limit]
    return cleaned


def _session_identifier(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or _SESSION_ID_RE.fullmatch(normalized) is None:
        raise HermesAcpError(
            "hermes_acp_invalid_identifier",
            f"ACP {field} is not a safe non-empty identifier.",
            {field: _bounded_text(value, 80)},
        )
    return normalized


def _tool_call_identifier(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or _TOOL_CALL_ID_RE.fullmatch(normalized) is None:
        raise HermesAcpError(
            "hermes_acp_invalid_tool_call_id",
            "ACP permission tool call ID is not a safe identifier.",
            {"tool_call_id": _bounded_text(value, 80)},
        )
    return normalized


def validate_initialize_result(result: Any) -> int:
    """Validate the ``initialize`` response, fail closed on version drift."""
    if not isinstance(result, dict):
        raise HermesAcpUnsupported(
            "hermes_acp_unsupported_version",
            "Hermes ACP initialize response is not an object.",
            {"result_kind": type(result).__name__},
        )
    version = result.get("protocolVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        raise HermesAcpUnsupported(
            "hermes_acp_unsupported_version",
            "Hermes ACP initialize response has no numeric protocol version.",
            {"protocol_version": _bounded_text(version, 40)},
        )
    if version != HERMES_ACP_PROTOCOL_VERSION:
        raise HermesAcpUnsupported(
            "hermes_acp_unsupported_version",
            f"Hermes ACP protocol version {version} is unsupported.",
            {
                "protocol_version": version,
                "supported_protocol_version": HERMES_ACP_PROTOCOL_VERSION,
            },
        )
    return version


def validate_session_id(value: Any) -> str:
    """Validate an official ACP session id, fail closed."""
    return _session_identifier(value, field="session_id")


# ---------------------------------------------------------------------------
# ACP wire-boundary decoder (PERM-104-002).
#
# One explicit, mechanical decoder converts a raw ``session/request_permission``
# JSON-RPC frame into :class:`HermesAcpPermissionRequest`.  Nothing else in
# AgentBC is allowed to probe the raw permission dictionaries.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HermesAcpPermissionOption:
    """One native ACP permission option, preserved exactly as offered.

    ``native_option_id``/``native_kind``/``native_name`` are the agent's own
    wire values, verbatim and order independent.  ``role`` is AgentBC's
    internal 1.04A dialog binding (``once``/``session``/``deny``/``other``)
    derived mechanically from a closed option-id table - never from a guessed
    permission category.  ``selectable`` is False for every non-actionable
    role, so persistent choices stay visible in the audit receipt and stay
    out of the dialog.
    """

    native_option_id: str
    native_kind: str
    native_name: str
    role: str
    label: str

    @property
    def selectable(self) -> bool:
        """Return whether the 1.04A dialog may offer this choice."""
        return self.role != HERMES_ACP_ROLE_OTHER

    def as_choice(self) -> dict[str, Any]:
        """Return the executor-neutral v2 choice entry for this option."""
        return {
            "native_option_id": self.native_option_id,
            "native_kind": self.native_kind,
            "kind": self.role,
            "label": self.label,
            "selectable": self.selectable,
        }


@dataclass(frozen=True)
class HermesAcpToolCall:
    """The tool-call metadata attached to one permission request."""

    tool_call_id: str
    kind: str
    status: str
    title: str
    content: tuple[dict[str, Any], ...]
    raw_input: Any

    @property
    def item_id(self) -> str:
        """Alias used by the bridged Codex-shaped approval message."""
        return self.tool_call_id


@dataclass(frozen=True)
class HermesAcpPermissionRequest:
    """The single normalized form of one ACP permission request.

    Every later consumer (approval bridge, control plane, dialog projection,
    response path) reads this object.  It carries the original JSON-RPC
    request id, the exact official session id, the tool-call identity and
    metadata, and the exact offered options in offer order.
    """

    request_id: Any
    session_id: str
    tool_call: HermesAcpToolCall
    options: tuple[HermesAcpPermissionOption, ...]
    summary: str
    frame: dict[str, Any]

    @property
    def tool_call_id(self) -> str:
        """Return the safe ACP tool-call id (``perm-check-<n>`` for Hermes)."""
        return self.tool_call.tool_call_id

    def offered_option_ids(self) -> tuple[str, ...]:
        """Return every offered optionId verbatim, in offer order."""
        return tuple(option.native_option_id for option in self.options)

    def selectable_options(self) -> tuple[HermesAcpPermissionOption, ...]:
        """Return the options the 1.04A dialog may act on."""
        return tuple(option for option in self.options if option.selectable)

    def option_by_id(self, option_id: Any) -> HermesAcpPermissionOption | None:
        """Return the offered option with this exact optionId, else None."""
        clean = str(option_id or "").strip()
        for option in self.options:
            if option.native_option_id == clean:
                return option
        return None

    def option_by_role(self, role: Any) -> HermesAcpPermissionOption | None:
        """Return the single offered option bound to this dialog role."""
        clean = str(role or "").strip()
        if not clean or clean == HERMES_ACP_ROLE_OTHER:
            return None
        matched = [option for option in self.options if option.role == clean]
        if len(matched) != 1:
            # Zero or ambiguous: fail closed rather than guessing a button.
            return None
        return matched[0]

    def offered_choices(self) -> list[dict[str, Any]]:
        """Return every offered option as an executor-neutral v2 choice."""
        return [option.as_choice() for option in self.options]


def _option_role(option_id: str, native_kind: str = "") -> str:
    """Bind one offered option to its 1.04A dialog role mechanically.

    Two closed tables and nothing else - never a guessed permission category:

    1. the official Hermes option ids.  This is the primary binding because
       Hermes marks its session-scoped choice ``allow_session`` with the
       ``allow_always`` kind (the ACP enum has no session kind), so keying on
       ``kind`` alone would hide the native session choice from the dialog.
    2. the canonical ACP ``PermissionOptionKind`` enum, used only when the
       ``optionId`` is unknown.  Only the two one-shot kinds map to an
       actionable role; ``allow_always``/``reject_always`` stay non-actionable,
       consistent with the invariant that the dialog never exposes a
       persistent-scope choice.

    Everything unknown stays visible in the audit receipt and non-actionable.
    """
    role = _HERMES_ACP_DIALOG_ROLES.get(option_id)
    if role is not None:
        return role
    kind_role = _HERMES_ACP_KIND_ROLES.get(native_kind)
    if kind_role is not None:
        return kind_role
    # Unknown / persistent ids stay visible and non-actionable.
    return HERMES_ACP_ROLE_OTHER


def _option_identifier(value: Any, *, context: str) -> str:
    """Validate one ACP ``optionId``; it must be a safe non-empty identifier.

    The optionId is echoed back verbatim, so it is never rewritten, trimmed to
    a prefix or fuzzy-matched to a known id.
    """
    normalized = str(value or "").strip()
    if not normalized or _TOOL_CALL_ID_RE.fullmatch(normalized) is None:
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "ACP permission option optionId is not a safe identifier.",
            {"context": context, "option_id": _bounded_text(value, 80)},
        )
    return normalized


def _reject_mixed_field(obj: dict[str, Any], *, canonical: str, context: str) -> None:
    """Fail closed when a canonical field coexists with a legacy alias.

    AgentBC's internal snake_case names are produced only *after* decoding.
    Their presence on a raw frame means the frame was not produced by the
    canonical ACP encoder, so guessing which field wins is not an option.
    """
    internal = _INTERNAL_ALIASES.get(canonical)
    if not internal:
        return
    for key in obj:
        if key == canonical or key == ACP_FIELD_META:
            continue
        if key == internal or key in _INTERNAL_ALIAS_NAMES:
            raise HermesAcpError(
                "hermes_acp_mixed_wire_fields",
                "ACP permission frame mixes canonical and non-canonical field names.",
                {"context": context, "canonical_field": canonical, "offending_field": str(key)},
            )


def _reject_unknown_fields(obj: dict[str, Any], *, allowed: frozenset[str], context: str) -> None:
    unknown = [str(key) for key in obj if key not in allowed]
    if unknown:
        raise HermesAcpError(
            "hermes_acp_unknown_wire_field",
            "ACP permission frame carries a field outside the canonical schema.",
            {"context": context, "unknown_fields": unknown[:8]},
        )


def _normalize_permission_option(
    option: Any,
    *,
    index: int,
) -> HermesAcpPermissionOption:
    """Validate and normalize one ``params.options[]`` entry."""
    context = f"options[{index}]"
    if not isinstance(option, dict):
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "Each ACP permission option must be an object.",
            {"context": context},
        )
    _reject_mixed_field(
        option,
        canonical=ACP_FIELD_OPTION_ID,
        context=context,
    )
    _reject_unknown_fields(
        option,
        allowed=_PERMISSION_OPTION_WIRE_FIELDS,
        context=context,
    )
    if ACP_FIELD_OPTION_ID not in option:
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "Each ACP permission option requires optionId.",
            {"context": context},
        )
    option_id = _option_identifier(option.get(ACP_FIELD_OPTION_ID), context=context)
    if ACP_FIELD_OPTION_KIND not in option:
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "Each ACP permission option requires kind.",
            {"context": context, "option_id": option_id},
        )
    if ACP_FIELD_OPTION_NAME not in option:
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "Each ACP permission option requires name.",
            {"context": context, "option_id": option_id},
        )
    native_kind = str(option.get(ACP_FIELD_OPTION_KIND) or "").strip()
    if not native_kind:
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "Each ACP permission option requires a non-empty kind.",
            {"context": context, "option_id": option_id},
        )
    raw_name = option.get(ACP_FIELD_OPTION_NAME)
    if raw_name is not None and not isinstance(raw_name, str):
        raise HermesAcpError(
            "hermes_acp_permission_options_invalid",
            "ACP permission option name must be a string.",
            {"context": context, "option_id": option_id},
        )
    native_name = _bounded_text(raw_name, 120)
    return HermesAcpPermissionOption(
        native_option_id=option_id,
        native_kind=native_kind,
        native_name=native_name,
        role=_option_role(option_id, native_kind),
        label=native_name or option_id,
    )


def normalize_permission_options(options: Any) -> tuple[HermesAcpPermissionOption, ...]:
    """Normalize the canonical ``params.options`` array (fail closed).

    Every offered option is preserved verbatim with its exact ``optionId``,
    ``kind`` and ``name``; duplicate optionIds, missing canonical fields and
    non-canonical fields all fail closed.
    """
    if not isinstance(options, list) or not options:
        raise HermesAcpError(
            "hermes_acp_permission_options_unsupported",
            "ACP permission request offers no options.",
        )
    normalized: list[HermesAcpPermissionOption] = []
    seen: set[str] = set()
    for index, option in enumerate(options):
        entry = _normalize_permission_option(option, index=index)
        if entry.native_option_id in seen:
            raise HermesAcpError(
                "hermes_acp_permission_options_duplicate",
                "ACP permission request offered the same optionId twice.",
                {"option_id": entry.native_option_id},
            )
        seen.add(entry.native_option_id)
        normalized.append(entry)
    if not normalized:
        raise HermesAcpError(
            "hermes_acp_permission_options_unsupported",
            "ACP permission request offers no usable options.",
        )
    return tuple(normalized)


def _normalize_tool_call(tool_call: Any) -> HermesAcpToolCall:
    """Validate and normalize the canonical ``params.toolCall`` object.

    The tool-call identity lives at ``toolCall.toolCallId`` on the canonical
    ACP wire (``ToolCallUpdate.tool_call_id``, alias ``toolCallId``).  A
    ``toolCall.id`` field is not part of the schema and is rejected instead of
    being probed as a fallback.
    """
    if not isinstance(tool_call, dict):
        raise HermesAcpError(
            "hermes_acp_permission_tool_call_missing",
            "ACP permission request has no tool call details.",
        )
    _reject_mixed_field(
        tool_call,
        canonical=ACP_FIELD_TOOL_CALL_ID,
        context="toolCall",
    )
    _reject_unknown_fields(
        tool_call,
        allowed=_TOOL_CALL_WIRE_FIELDS,
        context="toolCall",
    )
    if ACP_FIELD_TOOL_CALL_ID not in tool_call:
        raise HermesAcpError(
            "hermes_acp_permission_tool_call_id_missing",
            "ACP permission tool call has no canonical toolCallId.",
            {"tool_call_fields": sorted(str(key) for key in tool_call)[:12]},
        )
    tool_call_id = _tool_call_identifier(tool_call.get(ACP_FIELD_TOOL_CALL_ID))
    title = tool_call.get("title")
    if title is not None and not isinstance(title, str):
        raise HermesAcpError(
            "hermes_acp_permission_tool_call_invalid",
            "ACP permission tool call title must be a string.",
            {"tool_call_id": tool_call_id},
        )
    kind = tool_call.get("kind")
    status = tool_call.get("status")
    content = tool_call.get("content")
    if content is not None and not isinstance(content, list):
        raise HermesAcpError(
            "hermes_acp_permission_tool_call_invalid",
            "ACP permission tool call content must be a list.",
            {"tool_call_id": tool_call_id},
        )
    return HermesAcpToolCall(
        tool_call_id=tool_call_id,
        kind=str(kind) if isinstance(kind, str) else "",
        status=str(status) if isinstance(status, str) else "",
        title=_bounded_text(title),
        content=tuple(dict(block) for block in content or [] if isinstance(block, dict)),
        raw_input=tool_call.get("rawInput"),
    )


def decode_permission_request(
    frame: Any,
    *,
    session_id: str,
) -> HermesAcpPermissionRequest:
    """Decode one raw ``session/request_permission`` frame, fail closed.

    This is the ONLY place AgentBC reads the raw permission wire.  It
    validates the frame mechanically against the canonical ACP field names,
    requires the exact official session and the original JSON-RPC request id,
    and returns the typed normalized request every later consumer uses.
    """
    if not isinstance(frame, dict):
        raise HermesAcpError(
            "hermes_acp_malformed_frame",
            "ACP permission frame is not an object.",
        )
    if frame.get("method") != "session/request_permission":
        raise HermesAcpError(
            "hermes_acp_permission_method_invalid",
            "ACP permission frame uses an unsupported method.",
            {"method": _bounded_text(frame.get("method"), 80)},
        )
    if "id" not in frame or frame.get("id") is None:
        raise HermesAcpError(
            "hermes_acp_permission_id_missing",
            "ACP permission request has no JSON-RPC request ID.",
        )
    params = frame.get("params")
    if not isinstance(params, dict):
        raise HermesAcpError(
            "hermes_acp_permission_params_invalid",
            "ACP permission request has no params object.",
        )
    _reject_mixed_field(
        params,
        canonical=ACP_FIELD_SESSION_ID,
        context="params",
    )
    _reject_unknown_fields(
        params,
        allowed=_PERMISSION_PARAMS_WIRE_FIELDS,
        context="params",
    )
    if ACP_FIELD_SESSION_ID not in params:
        raise HermesAcpError(
            "hermes_acp_permission_params_invalid",
            "ACP permission request has no canonical sessionId.",
        )
    actual_session = _session_identifier(
        params.get(ACP_FIELD_SESSION_ID), field="session_id"
    )
    expected_session = _session_identifier(session_id, field="session_id")
    if actual_session != expected_session:
        raise HermesAcpError(
            "hermes_acp_permission_session_mismatch",
            "ACP permission request is bound to a different official session.",
            {
                "expected_session_id": _bounded_text(expected_session, 80),
                "actual_session_id": _bounded_text(actual_session, 80),
            },
        )
    if ACP_FIELD_TOOL_CALL not in params:
        raise HermesAcpError(
            "hermes_acp_permission_tool_call_missing",
            "ACP permission request has no tool call details.",
        )
    tool_call = _normalize_tool_call(params.get(ACP_FIELD_TOOL_CALL))
    if ACP_FIELD_OPTIONS not in params:
        raise HermesAcpError(
            "hermes_acp_permission_options_unsupported",
            "ACP permission request offers no options.",
        )
    options = normalize_permission_options(params.get(ACP_FIELD_OPTIONS))
    return HermesAcpPermissionRequest(
        request_id=frame["id"],
        session_id=actual_session,
        tool_call=tool_call,
        options=options,
        summary=permission_summary(tool_call.title),
        frame=frame,
    )


def permission_summary(title: Any) -> str:
    """Return a sanitized, bounded one-line summary for one ACP tool call.

    The summary is derived only from the structured tool-call title; it is
    bounded and control-character-free and never includes raw session
    content, tokens, or secrets.
    """
    bounded = _bounded_text(title)
    return bounded or "Hermes terminal action"


def build_approval_message(
    request: HermesAcpPermissionRequest,
    *,
    task_id: str,
    executor_run_id: str,
) -> dict[str, Any]:
    """Translate one normalized ACP permission request into the plane shape.

    The executor-neutral :class:`ApprovalControlPlane` accepts the Codex
    App-Server approval message shape; the transport preserves that public
    interface and only translates the wire format.  ``threadId`` is the
    official ACP session id so the plane's session-first gate binds the
    request to the exact persisted receipt.  ``request`` must already be the
    output of :func:`decode_permission_request`: this function never touches
    the raw frame beyond reading its identity fields.  Every offered native
    option travels as the executor-native choice set with its exact
    ``optionId``, ``kind`` and ``name``.
    """
    return {
        "jsonrpc": _JSONRPC,
        "id": request.request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": request.session_id,
            "turnId": "",
            "itemId": request.tool_call_id,
            "reason": request.summary,
        },
        "_agentbc": {
            "task_id": str(task_id or "").strip(),
            "executor_run_id": str(executor_run_id or "").strip(),
        },
        "approval_version": 2,
        "authority": {
            "executor": "hermes",
            "protocol": "hermes_acp",
            "protocol_version": HERMES_ACP_PROTOCOL_VERSION,
            "method": "session/request_permission",
        },
        "offered_choices": request.offered_choices(),
    }


def selected_permission_outcome(option_id: Any) -> dict[str, Any]:
    """Return the canonical ACP outcome for one selected native option.

    The result is a valid ``RequestPermissionResponse``:
    ``{"outcome": {"outcome": "selected", "optionId": <exact>}}``.  The
    ``outcome: "selected"`` discriminator is required by the ACP schema
    (``AllowedOutcome``); returning only ``{"optionId": ...}`` is not
    parseable by the agent and is silently read as a denial.
    """
    clean = str(option_id or "").strip()
    if not clean:
        raise HermesAcpError(
            "hermes_acp_approval_decision_invalid",
            "A selected ACP permission outcome requires an optionId.",
        )
    return {"outcome": {"outcome": HERMES_ACP_SELECTED_OUTCOME, "optionId": clean}}


def cancelled_permission_outcome() -> dict[str, Any]:
    """Return the canonical ACP ``DeniedOutcome`` (no option selected)."""
    return {"outcome": {"outcome": HERMES_ACP_DENIED_OUTCOME}}


def approval_outcome_for_decision(decision: Any) -> dict[str, Any]:
    """Map one ControlPlane decision to the exact ACP permission outcome.

    A selected native choice returns the EXACT original optionId verbatim in
    the canonical ``SelectedPermissionOutcome`` shape.  The legacy one-shot
    mapping (accept -> the offered one-shot option / decline -> the ACP
    ``cancelled`` outcome) remains only as the fail-closed fallback for a
    decision recorded without a native choice payload - the v1 dual-read
    path.  Nothing here infers a permission category or mints a grant.
    """
    if isinstance(decision, dict):
        outcome = decision.get("outcome")
        if isinstance(outcome, dict):
            option_id = str(outcome.get(ACP_FIELD_OPTION_ID) or "").strip()
            if option_id:
                return selected_permission_outcome(option_id)
        option_id = str(decision.get("native_option_id") or "").strip()
        if option_id:
            return selected_permission_outcome(option_id)
        raise HermesAcpError(
            "hermes_acp_approval_decision_invalid",
            "Only the exact offered optionId outcomes may be returned to Hermes ACP.",
            {"decision": _bounded_text(decision, 40)},
        )
    selected = str(decision or "").strip().lower()
    if selected == "accept":
        return selected_permission_outcome(HERMES_ACP_ALLOW_ONCE_OPTION)
    if selected == "decline":
        return cancelled_permission_outcome()
    raise HermesAcpError(
        "hermes_acp_approval_decision_invalid",
        "Only the exact offered optionId outcomes may be returned to Hermes ACP.",
        {"decision": _bounded_text(decision, 40)},
    )


def frame_kind(frame: Any) -> str:
    """Classify one parsed frame, fail closed on malformed shapes.

    Returns ``response``, ``request``, or ``notification``.  A frame that is
    not a JSON object, or mixes response and request fields, is malformed.
    """
    if not isinstance(frame, dict):
        raise HermesAcpError(
            "hermes_acp_malformed_frame",
            "ACP frame is not a JSON object.",
            {"frame_kind": type(frame).__name__},
        )
    has_id = "id" in frame
    has_method = isinstance(frame.get("method"), str)
    has_result = "result" in frame
    has_error = isinstance(frame.get("error"), dict)
    if has_id and has_method and (has_result or has_error):
        # A frame that is simultaneously a request and a response is never
        # valid on the wire; fail closed instead of guessing intent.
        raise HermesAcpError(
            "hermes_acp_malformed_frame",
            "ACP frame mixes request and response fields.",
            {"keys": sorted(str(key) for key in frame.keys())[:16]},
        )
    if has_id and (has_result or has_error):
        return "response"
    if has_id and has_method:
        return "request"
    if not has_id and has_method:
        return "notification"
    raise HermesAcpError(
        "hermes_acp_malformed_frame",
        "ACP frame mixes or omits response/request fields.",
        {"keys": sorted(str(key) for key in frame.keys())[:16]},
    )


class HermesAcpTransport:
    """Dependency-free ACP stdio client for one spawned ``hermes acp`` process.

    The transport owns one subprocess and one JSON-RPC stream.  It never
    scans Hermes state, never mutates global configuration, and applies only
    the caller-provided subprocess-scoped environment (the registry-frozen
    full-mode override).  All receive paths are bounded by explicit timeouts.
    """

    def __init__(
        self,
        executable: str | Path,
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
        rpc_timeout_s: float = HERMES_ACP_RPC_TIMEOUT_S,
        start_timeout_s: float = _HERMES_ACP_START_TIMEOUT_S,
        receive_timeout_s: float = HERMES_ACP_RECEIVE_TIMEOUT_S,
        message_max_bytes: int = HERMES_ACP_MESSAGE_MAX_BYTES,
        poll_slice_s: float = _HERMES_ACP_POLL_SLICE_S,
    ) -> None:
        self.executable = str(executable)
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.command = list(command or [self.executable, "acp"])
        self.env = dict(env or {})
        self.rpc_timeout_s = max(float(rpc_timeout_s), 0.1)
        self.start_timeout_s = max(float(start_timeout_s), 0.1)
        # Idle guard for a live process inside one turn.  It is NOT the turn
        # deadline: the adapter keeps owning that through ``prompt(timeout_s)``.
        self.receive_timeout_s = max(float(receive_timeout_s), 0.1)
        self.message_max_bytes = max(int(message_max_bytes), 1)
        self._poll_slice_s = max(float(poll_slice_s), 0.01)
        self.process: subprocess.Popen[bytes] | None = None
        self._send_lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        self._message_chunks: list[str] = []
        self._message_bytes = 0
        self._message_truncated = False
        self._inbound = bytearray()
        # The session whose ``session/update`` notifications are collected for
        # the current turn.  Unset outside a turn, so ``session/load`` history
        # replay and any unrelated session can never leak into the terminal
        # answer that the final callback is extracted from.
        self._collecting_session_id = ""
        self._closed = False

    # ---- process lifecycle -------------------------------------------------

    def start(self) -> None:
        """Spawn the ACP subprocess; raise :class:`HermesAcpError` on failure."""
        if self.process is not None:
            return
        try:
            # stdout/stderr stay binary: AgentBC frames are assembled at the
            # byte level so a frame written in several chunks is decoded whole
            # and a non-UTF-8 byte becomes a classified failure instead of an
            # escaping UnicodeDecodeError.
            process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise HermesAcpError(
                "hermes_acp_start_failed",
                f"Failed to start hermes acp: {exc}",
            ) from exc
        self.process = process
        self._stderr_tail = []
        self._inbound = bytearray()
        drain = threading.Thread(
            target=self._drain_stderr,
            name="agentbc-hermes-acp-stderr",
            daemon=True,
        )
        drain.start()

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                with self._stderr_lock:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 32:
                        del self._stderr_tail[:-32]
        except (OSError, ValueError):
            pass

    def stderr_evidence(self) -> str:
        """Return bounded stderr tail for failure evidence (never parsed for ids)."""
        with self._stderr_lock:
            joined = "".join(self._stderr_tail)
        if len(joined.encode("utf-8", errors="replace")) > _STDERR_KEEP_BYTES:
            return joined[-_STDERR_KEEP_BYTES:]
        return joined

    def _collect_message_chunks(self, frame: dict[str, Any]) -> None:
        """Accumulate bounded assistant text from one ``session/update`` frame.

        PERM-104-001: the canonical ACP notification is
        ``params.sessionId`` + ``params.update``, where ``update`` carries the
        ``sessionUpdate`` discriminator and a single ``content`` content block
        (``SessionNotification`` / ``AgentMessageChunk`` in the pinned
        ``agent-client-protocol`` schema).  The previous probe of
        ``params.sessionUpdate[].message[].content[]`` matched no real frame,
        so AgentBC collected an empty terminal answer and a perfectly completed
        Hermes turn failed as ``completion_marker_missing``.

        Collection is bounded, exact and session-scoped: only ``agent_message_chunk``
        text blocks of the currently prompted session are kept, ``session/load``
        history replay and unrelated sessions are ignored, and the head AND the
        tail of the turn text survive the byte budget so the final marker line
        can never be truncated away.
        """
        params = frame.get("params")
        if not isinstance(params, dict):
            return
        if not self._collecting_session_id:
            # Outside a turn nothing is collected, so a resume's history replay
            # can never be mistaken for this run's terminal answer.
            return
        session_id = params.get(ACP_FIELD_SESSION_ID)
        if not isinstance(session_id, str) or session_id != self._collecting_session_id:
            return
        update = params.get(ACP_FIELD_UPDATE)
        if not isinstance(update, dict):
            raise HermesAcpError(
                "hermes_acp_session_update_invalid",
                "Hermes ACP session/update carries no canonical update object.",
            )
        discriminator = update.get(ACP_FIELD_SESSION_UPDATE)
        if discriminator not in _HERMES_ACP_AGENT_MESSAGE_UPDATES:
            return
        if ACP_FIELD_META in update and not isinstance(update.get(ACP_FIELD_META), dict):
            raise HermesAcpError(
                "hermes_acp_session_update_invalid",
                "Hermes ACP session/update _meta must be an object.",
            )
        unknown = [
            str(key)
            for key in update
            if key not in _HERMES_ACP_MESSAGE_WIRE_FIELDS
        ]
        if unknown:
            raise HermesAcpError(
                "hermes_acp_session_update_invalid",
                "Hermes ACP agent message update carries a non-canonical field.",
                {"unknown_fields": unknown[:8]},
            )
        content = update.get(ACP_FIELD_CONTENT)
        if not isinstance(content, dict):
            raise HermesAcpError(
                "hermes_acp_session_update_invalid",
                "Hermes ACP agent message update has no content block.",
            )
        if content.get("type") != "text":
            return
        text = content.get(ACP_FIELD_TEXT)
        if not isinstance(text, str) or not text:
            return
        self._append_message_text(text)

    def _append_message_text(self, text: str) -> None:
        """Append one exact text block under the bounded turn budget.

        The budget is enforced by evicting the OLDEST text once the turn grows
        past it, never by dropping the newest text: the terminal marker is the
        last line of the terminal answer, so a policy that keeps the head and
        discards the tail would silently discard the executor's completion.
        """
        encoded = text.encode("utf-8", errors="replace")
        budget = self.message_max_bytes
        if len(encoded) > budget:
            # One chunk above the budget: keep its tail, which carries the
            # terminal marker.
            encoded = encoded[-min(HERMES_ACP_MESSAGE_TAIL_BYTES, budget):]
            text = encoded.decode("utf-8", errors="replace")
        while self._message_chunks and self._message_bytes + len(encoded) > budget:
            evicted = self._message_chunks.pop(0)
            self._message_bytes -= len(evicted.encode("utf-8", errors="replace"))
            self._message_truncated = True
        if self._message_bytes + len(encoded) > budget:
            # A single chunk larger than the whole budget (already tail-clipped
            # above) replaces the accumulated text.
            self._message_chunks = []
            self._message_bytes = 0
            self._message_truncated = True
        self._message_chunks.append(text)
        self._message_bytes += len(encoded)

    def message_text(self) -> str:
        """Return the bounded accumulated assistant text for the current turn."""
        return "".join(self._message_chunks)

    def message_truncated(self) -> bool:
        """Return whether the accumulated turn text hit the bounded budget."""
        return self._message_truncated

    def is_alive(self) -> bool:
        process = self.process
        if process is None:
            return False
        return process.poll() is None

    def close(self) -> None:
        """Terminate the ACP subprocess and close the stream, idempotently."""
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        self.process = None
        self._inbound = bytearray()
        self._collecting_session_id = ""

    # ---- framing -----------------------------------------------------------

    def _send(self, frame: dict[str, Any]) -> None:
        if self._closed or self.process is None or self.process.stdin is None:
            raise HermesAcpError(
                "hermes_acp_transport_closed",
                "Hermes ACP transport is not started or already closed.",
            )
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        wire = (encoded + "\n").encode("utf-8")
        with self._send_lock:
            try:
                self.process.stdin.write(wire)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise HermesAcpError(
                    "hermes_acp_broken_pipe",
                    "Hermes ACP stdin closed before the request completed.",
                ) from exc

    def _recv_frame(self, timeout_s: float) -> dict[str, Any]:
        """Return the next complete ACP frame, or raise a classified failure.

        The frame is assembled at the byte level so a frame that arrives in
        several writes (or slower than one ``select`` slice) is decoded whole:
        partial input is buffered, never truncated and never read through a
        blocking ``TextIOWrapper.readline`` that could ignore the deadline.
        """
        if self._closed or self.process is None or self.process.stdout is None:
            raise HermesAcpError(
                "hermes_acp_transport_closed",
                "Hermes ACP transport is not started or already closed.",
            )
        deadline = time.monotonic() + max(float(timeout_s), 0.0)
        while True:
            decoded = self._decode_buffered_frame()
            if decoded is not None:
                return decoded
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HermesAcpTimeout(
                    "hermes_acp_receive_idle_timeout",
                    "Hermes ACP receive timed out without a complete frame.",
                    {
                        "timeout_s": round(max(float(timeout_s), 0.0), 3),
                        "buffered_bytes": len(self._inbound),
                        "process_alive": self.is_alive(),
                    },
                )
            self._read_available(min(remaining, self._poll_slice_s))

    def _decode_buffered_frame(self) -> dict[str, Any] | None:
        """Decode one buffered frame, or return ``None`` if none is complete."""
        complete = self._pop_complete_frame()
        if complete is None:
            return None
        stripped = complete.strip()
        if not stripped:
            # A blank separator line is not a frame; keep draining the buffer.
            return self._decode_buffered_frame()
        try:
            text = stripped.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HermesAcpError(
                "hermes_acp_malformed_frame",
                "Hermes ACP emitted a frame that is not valid UTF-8.",
                {"reason": str(exc.reason)},
            ) from exc
        try:
            frame = json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HermesAcpError(
                "hermes_acp_malformed_frame",
                "Hermes ACP emitted a malformed JSON frame.",
                {"line": _bounded_text(text, 120)},
            ) from exc
        kind = frame_kind(frame)
        if kind == "notification" and frame.get("method") == "session/update":
            self._collect_message_chunks(frame)
        return frame

    def _pop_complete_frame(self) -> bytes | None:
        """Return one buffered newline-terminated frame, else ``None``."""
        index = self._inbound.find(b"\n")
        if index < 0:
            return None
        raw = self._inbound[:index]
        del self._inbound[: index + 1]
        return raw

    def _read_available(self, slice_s: float) -> None:
        """Block at most ``slice_s`` for more inbound bytes, then return.

        EOF, process exit and idle expiry stay distinct: EOF means the agent
        closed stdout, a process exit carries the exit code, and an idle slice
        just returns so the caller can re-check its own deadline.
        """
        process = self.process
        if process is None or process.stdout is None:
            raise HermesAcpError(
                "hermes_acp_transport_closed",
                "Hermes ACP transport is not started or already closed.",
            )
        fd = process.stdout.fileno()
        try:
            ready, _, _ = select.select([fd], [], [], max(float(slice_s), 0.0))
        except (OSError, ValueError) as exc:
            raise HermesAcpError(
                "hermes_acp_transport_closed",
                f"Hermes ACP stdout is unavailable: {exc}",
            ) from exc
        if not ready:
            if process.poll() is not None:
                raise HermesAcpError(
                    "hermes_acp_transport_exited",
                    "Hermes ACP process exited before the frame completed.",
                    {"exit_code": process.returncode},
                )
            return
        try:
            chunk = os.read(fd, _HERMES_ACP_READ_CHUNK_BYTES)
        except OSError as exc:
            raise HermesAcpError(
                "hermes_acp_broken_pipe",
                "Hermes ACP stdout could not be read.",
            ) from exc
        if not chunk:
            # EOF.  A process that exits also closes its stdout, so the exit is
            # the stronger truth when it is already reaped; EOF while still
            # alive is reported as its own failure.
            exit_code = process.poll()
            if exit_code is not None:
                raise HermesAcpError(
                    "hermes_acp_transport_exited",
                    "Hermes ACP process exited before the frame completed.",
                    {"exit_code": exit_code},
                )
            raise HermesAcpError(
                "hermes_acp_transport_eof",
                "Hermes ACP process closed stdout before the frame completed.",
            )
        self._inbound.extend(chunk)
        if len(self._inbound) > _HERMES_ACP_INBOUND_MAX_BYTES:
            raise HermesAcpError(
                "hermes_acp_frame_oversized",
                "Hermes ACP emitted a frame above the bounded inbound size.",
                {"inbound_bytes": len(self._inbound)},
            )

    # ---- RPC helpers -------------------------------------------------------

    def _request(self, method: str, params: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        request_id = f"agentbc-{method.replace('/', '-')}-{os.getpid()}-{time.monotonic_ns()}"
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + max(float(timeout_s), 0.1)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HermesAcpTimeout(
                    "hermes_acp_rpc_timeout",
                    f"Hermes ACP {method} timed out.",
                    {"method": method, "timeout_s": round(max(float(timeout_s), 0.1), 3)},
                )
            frame = self._recv_frame(remaining)
            kind = frame_kind(frame)
            if kind == "request":
                if frame.get("method") == "session/request_permission":
                    raise HermesAcpError(
                        "hermes_acp_permission_out_of_order",
                        "Hermes ACP requested permission before the session handshake completed.",
                    )
                self._respond_error(frame.get("id"), -32601, "Method not found")
                continue
            if kind == "notification":
                continue
            if frame.get("id") != request_id:
                raise HermesAcpError(
                    "hermes_acp_response_mismatch",
                    "Hermes ACP responded to an unknown request.",
                    {"expected_id": _bounded_text(request_id, 80)},
                )
            error = frame.get("error")
            if isinstance(error, dict):
                raise HermesAcpError(
                    "hermes_acp_rpc_error",
                    "Hermes ACP rejected the request.",
                    {
                        "method": method,
                        "code": error.get("code"),
                        "message": _bounded_text(error.get("message"), 160),
                    },
                )
            result = frame.get("result")
            if not isinstance(result, dict):
                raise HermesAcpError(
                    "hermes_acp_rpc_result_invalid",
                    f"Hermes ACP {method} returned a non-object result.",
                    {"method": method},
                )
            return result

    def _respond(self, request_id: Any, result: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": _JSONRPC, "id": request_id}
        if result is not None:
            frame["result"] = result
        else:
            frame["result"] = None
        self._send(frame)

    def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    # ---- protocol surface --------------------------------------------------

    def initialize(self, timeout_s: float | None = None) -> dict[str, Any]:
        """Run the ACP initialize handshake and return the validated result.

        Sends ``initialize``, validates the negotiated protocol version, then
        sends the ``notifications/initialized`` notification.
        """
        result = self._request(
            "initialize",
            {
                "protocolVersion": HERMES_ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": HERMES_ACP_CLIENT_NAME,
                    "version": HERMES_ACP_CLIENT_VERSION,
                },
            },
            timeout_s=timeout_s if timeout_s is not None else self.rpc_timeout_s,
        )
        validate_initialize_result(result)
        self._send({"jsonrpc": _JSONRPC, "method": "notifications/initialized"})
        return result

    def new_session(self, cwd: str, timeout_s: float | None = None) -> str:
        """Create a fresh official ACP session and return its session id."""
        result = self._request(
            "session/new",
            {"cwd": str(cwd), "mcpServers": []},
            timeout_s=timeout_s if timeout_s is not None else self.rpc_timeout_s,
        )
        session_id = validate_session_id(result.get("sessionId") or result.get("session_id"))
        return session_id

    def load_session(self, cwd: str, session_id: str, timeout_s: float | None = None) -> str:
        """Explicitly load one persisted official session (resume).

        The server returns an error or a null result when the session is
        unknown; both fail closed.  The official session id is the explicit
        id the caller requested - never a guessed or scanned id.
        """
        exact = validate_session_id(session_id)
        self._request(
            "session/load",
            {"cwd": str(cwd), "sessionId": exact, "mcpServers": []},
            timeout_s=timeout_s if timeout_s is not None else self.rpc_timeout_s,
        )
        return exact

    def prompt(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        on_permission: Callable[[dict[str, Any]], dict[str, Any]],
        timeout_s: float | None = None,
        receive_timeout_s: float | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Send one prompt turn and run the event loop until the response.

        ``on_permission`` receives each validated ``session/request_permission``
        request and must return the exact outcome dict; any exception it raises
        aborts the turn fail closed.  ``on_progress`` is invoked once per
        received frame so the adapter can keep its RunLease heartbeat alive.

        Bounds stay independent and truthful:

        * ``timeout_s`` is the whole-turn safety deadline owned by the adapter.
          Expiring it raises ``hermes_acp_prompt_timeout``.
        * ``receive_timeout_s`` (default :data:`HERMES_ACP_RECEIVE_TIMEOUT_S`)
          is only the longest silent interval tolerated from a *live* agent, so
          a multi-minute model or tool call inside a healthy turn continues
          past the old 30s per-frame bound.  Expiring it raises
          ``hermes_acp_receive_idle_timeout``.
        """
        exact = validate_session_id(session_id)
        request_id = f"agentbc-prompt-{os.getpid()}-{time.monotonic_ns()}"
        deadline = time.monotonic() + (
            max(float(timeout_s), 0.1) if timeout_s is not None else 86400.0 * 7
        )
        idle_window = (
            self.receive_timeout_s
            if receive_timeout_s is None
            else max(float(receive_timeout_s), 0.1)
        )
        self._message_chunks = []
        self._message_bytes = 0
        self._message_truncated = False
        self._collecting_session_id = exact
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "id": request_id,
                "method": "session/prompt",
                "params": {"sessionId": exact, "prompt": blocks},
            }
        )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HermesAcpTimeout(
                        "hermes_acp_prompt_timeout",
                        "Hermes ACP prompt turn exceeded the safety runtime.",
                        {
                            "timeout_s": round(
                                max(float(timeout_s) if timeout_s is not None else 0.0, 0.0),
                                3,
                            ),
                            "message_bytes": self._message_bytes,
                            "message_truncated": self._message_truncated,
                        },
                    )
                try:
                    frame = self._recv_frame(min(remaining, idle_window))
                except HermesAcpTimeout as exc:
                    if (
                        exc.code == "hermes_acp_receive_idle_timeout"
                        and time.monotonic() >= deadline
                    ):
                        # The overall safety deadline expired while waiting, so
                        # the turn deadline is the truthful cause.
                        raise HermesAcpTimeout(
                            "hermes_acp_prompt_timeout",
                            "Hermes ACP prompt turn exceeded the safety runtime.",
                            {
                                "timeout_s": round(
                                    max(
                                        float(timeout_s)
                                        if timeout_s is not None
                                        else 0.0,
                                        0.0,
                                    ),
                                    3,
                                ),
                                "message_bytes": self._message_bytes,
                                "message_truncated": self._message_truncated,
                            },
                        ) from exc
                    raise
                if on_progress is not None:
                    try:
                        on_progress()
                    except Exception:  # noqa: BLE001 - progress is never fatal
                        pass
                if on_progress is not None:
                    try:
                        on_progress()
                    except Exception:  # noqa: BLE001 - progress is never fatal
                        pass
                kind = frame_kind(frame)
                if kind == "request":
                    method = str(frame.get("method") or "")
                    if method == "session/request_permission":
                        # Decode once at the wire boundary so malformed,
                        # cross-session, mixed-field or duplicate-option
                        # requests fail closed before any decision can be
                        # returned.  The callback receives the normalized
                        # request, never the raw frame.
                        permission = decode_permission_request(frame, session_id=exact)
                        outcome = on_permission(permission)
                        self._respond(permission.request_id, outcome)
                        continue
                    if method in {"session/cancel", "session/close"}:
                        self._respond_error(
                            frame.get("id"),
                            -32601,
                            f"Unsupported request: {method}",
                        )
                        continue
                    self._respond_error(frame.get("id"), -32601, "Method not found")
                    continue
                if kind == "notification":
                    continue
                if frame.get("id") != request_id:
                    raise HermesAcpError(
                        "hermes_acp_response_mismatch",
                        "Hermes ACP responded to an unknown prompt request.",
                    )
                error = frame.get("error")
                if isinstance(error, dict):
                    raise HermesAcpError(
                        "hermes_acp_rpc_error",
                        "Hermes ACP rejected the prompt.",
                        {
                            "code": error.get("code"),
                            "message": _bounded_text(error.get("message"), 160),
                        },
                    )
                result = frame.get("result")
                if not isinstance(result, dict):
                    raise HermesAcpError(
                        "hermes_acp_rpc_result_invalid",
                        "Hermes ACP prompt returned a non-object result.",
                    )
                return result
        finally:
            self._collecting_session_id = ""

    def respond_permission(self, request_id: Any, outcome: dict[str, Any]) -> None:
        """Answer one permission request with the exact validated outcome."""
        self._respond(request_id, outcome)

    def cancel_session(self, session_id: str) -> None:
        """Send the official ``session/cancel`` notification for one session."""
        exact = validate_session_id(session_id)
        self._send(
            {
                "jsonrpc": _JSONRPC,
                "method": "session/cancel",
                "params": {"sessionId": exact},
            }
        )


__all__ = [
    "ACP_FIELD_CONTENT",
    "ACP_FIELD_META",
    "ACP_FIELD_OPTION_ID",
    "ACP_FIELD_OPTION_KIND",
    "ACP_FIELD_OPTION_NAME",
    "ACP_FIELD_OPTIONS",
    "ACP_FIELD_SESSION_ID",
    "ACP_FIELD_SESSION_UPDATE",
    "ACP_FIELD_TEXT",
    "ACP_FIELD_TOOL_CALL",
    "ACP_FIELD_TOOL_CALL_ID",
    "ACP_FIELD_UPDATE",
    "HERMES_ACP_ALLOW_ALWAYS_OPTION_ID",
    "HERMES_ACP_ALLOW_ONCE_OPTION",
    "HERMES_ACP_ALLOW_ONCE_OPTION_ID",
    "HERMES_ACP_ALLOW_SESSION_OPTION_ID",
    "HERMES_ACP_CLIENT_NAME",
    "HERMES_ACP_CLIENT_VERSION",
    "HERMES_ACP_DENIED_OUTCOME",
    "HERMES_ACP_DENY_ALWAYS_OPTION_ID",
    "HERMES_ACP_DENY_OPTION_ID",
    "HERMES_ACP_MESSAGE_MAX_BYTES",
    "HERMES_ACP_MESSAGE_TAIL_BYTES",
    "HERMES_ACP_NATIVE_OPTION_IDS",
    "HERMES_ACP_PROTOCOL_VERSION",
    "HERMES_ACP_RECEIVE_TIMEOUT_S",
    "HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID",
    "HERMES_ACP_RPC_TIMEOUT_S",
    "HermesAcpError",
    "HermesAcpPermissionOption",
    "HermesAcpPermissionRequest",
    "HermesAcpTimeout",
    "HermesAcpToolCall",
    "HermesAcpTransport",
    "HermesAcpUnsupported",
    "approval_outcome_for_decision",
    "build_approval_message",
    "cancelled_permission_outcome",
    "decode_permission_request",
    "frame_kind",
    "permission_summary",
    "selected_permission_outcome",
    "validate_initialize_result",
    "validate_session_id",
]
