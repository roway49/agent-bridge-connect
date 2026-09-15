"""Executor-neutral approval protocol and native choice mapping.

This module is the protocol boundary for request and event normalization. It
contains no durable task state and delegates receipt schema, validation, and
projection work to agent_bridge_connect.approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .approval import (
    APPROVAL_V3_ELEVATION_MODE,
    APPROVAL_V3_SCOPE,
    compute_request_fingerprint,
)
from .session import utc_now


def _bounded_text(value: Any, limit: int = 240) -> str:
    text = "" if value is None else str(value).strip()
    return text[:limit]


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            if isinstance(key, str):
                result[key[:80]] = _bounded_json(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:240]
    return str(value)[:240]

STABLE_EVENTS = frozenset(
    {"session_started", "approval_requested", "turn_completed", "transport_failed"}
)
CONTROL_VERSION = 1
APPROVAL_DECISIONS = frozenset({"accept", "decline"})
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
}
APPROVAL_V2_ERROR_CHOICE_REQUIRED = "native_permission_choice_required"
CODEX_SCHEMA_SESSION_DECISIONS = frozenset({"acceptForSession"})
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

class ControlPlaneError(RuntimeError):
    status = "needs_recovery"

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "control_plane_error")
        self.details = dict(details or {})
        super().__init__(message)

@dataclass(frozen=True)
class ControlEvent:
    event_type: str
    task_id: str
    executor: str
    executor_run_id: str
    session_id: str
    request_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        if self.event_type not in STABLE_EVENTS:
            raise ValueError(f"unsupported stable control event: {self.event_type}")
        value: dict[str, Any] = {
            "version": CONTROL_VERSION,
            "event_type": self.event_type,
            "task_id": self.task_id,
            "executor": self.executor,
            "executor_run_id": self.executor_run_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
        }
        if self.request_id:
            value["request_id"] = self.request_id
        value.update(_bounded_json(self.details))
        return value

@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    request_fingerprint: str
    rpc_id: Any
    task_id: str
    executor_run_id: str
    session_id: str
    kind: str
    operation: str
    summary: str
    scope: str = "single_action"
    thread_id: str = ""
    turn_id: str = ""
    item_id: str = ""
    requested_permissions: dict[str, Any] = field(default_factory=dict)
    tool_name: str = ""
    tool_use_id: str = ""
    input_fingerprint: str = ""
    action_fingerprint: str = ""
    escalation_domain: str = ""
    profile_digest: str = ""
    control_path: str = ""
    # PERM-104-002 v2: executor-native choice broker fields.
    approval_version: int = 1
    offered_choices: tuple[dict[str, Any], ...] = ()
    native_event: str = ""
    authority: dict[str, Any] = field(default_factory=dict)
    elevation_mode: str = ""
    path_plan_digest: str = ""
    containment_profile_digest: str = ""
    preflight: dict[str, Any] = field(default_factory=dict)
    # Claude safe-to-full uses the same v3 envelope for the public input, but
    # its response is an atomic SDK PermissionResult rather than a task
    # continuation.  Keep the marker explicit so old task-elevation records
    # remain readable without ever entering this path accidentally.
    native_live_elevation: bool = False
    native_elevation_protocol: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "task_id": self.task_id,
            "executor_run_id": self.executor_run_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "operation": self.operation,
            "summary": self.summary,
            "scope": (
                APPROVAL_V3_SCOPE
                if self.approval_version == 3
                else "single_action"
            ),
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
        }
        if self.operation == "permissions":
            value["requested_permissions"] = _bounded_json(self.requested_permissions)
        for key, item in (
            ("tool_name", self.tool_name),
            ("tool_use_id", self.tool_use_id),
            ("input_fingerprint", self.input_fingerprint),
            ("action_fingerprint", self.action_fingerprint),
            ("escalation_domain", self.escalation_domain),
            ("profile_digest", self.profile_digest),
            ("control_path", self.control_path),
            ("native_event", self.native_event),
        ):
            if item:
                value[key] = _bounded_text(item, 512)
        if self.approval_version == 2:
            value["approval_version"] = 2
            value["offered_choices"] = [
                dict(choice) for choice in self.offered_choices
            ]
            # The authority block rides on the pending request so the v2
            # respond path can dispatch the executor-native payload shape
            # (e.g. Hermes ACP outcome vs Codex decision).
            value["authority"] = _bounded_json(dict(self.authority or {}))
        elif self.approval_version == 3:
            value.update(
                {
                    "approval_version": 3,
                    "elevation_mode": self.elevation_mode
                    or APPROVAL_V3_ELEVATION_MODE,
                    "path_plan_digest": self.path_plan_digest,
                    "containment_profile_digest": self.containment_profile_digest,
                    "authority": _bounded_json(dict(self.authority or {})),
                    "preflight": {
                        "status": str(self.preflight.get("status") or "passed"),
                        "mode": str(
                            self.preflight.get("mode")
                            or APPROVAL_V3_ELEVATION_MODE
                        ),
                    },
                }
            )
            if self.native_live_elevation:
                value["native_live_elevation"] = True
                value["native_elevation_protocol"] = (
                    self.native_elevation_protocol
                    or "claude.can_use_tool.setMode"
                )
        return value

def normalize_approval_request(
    message: dict[str, Any],
    *,
    task_id: str,
    executor_run_id: str,
    session_id: str,
    executor: str = "codex",
) -> ApprovalRequest:
    """Normalize one official App Server approval request without raw content."""
    if not isinstance(message, dict):
        raise ControlPlaneError("approval_request_invalid", "Approval request is not an object.")
    method = str(message.get("method") or "")
    operation = APPROVAL_METHODS.get(method)
    if operation is None:
        raise ControlPlaneError(
            "approval_method_unsupported",
            "The executor sent an unsupported approval method.",
            {"method": method},
        )
    if "id" not in message or message.get("id") is None:
        raise ControlPlaneError("approval_request_id_missing", "Approval request has no JSON-RPC request ID.")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    request_id = _bounded_text(message.get("id"), 160)
    thread_id = _bounded_text(params.get("threadId") or params.get("thread_id"), 512)
    if not thread_id:
        raise ControlPlaneError("approval_session_missing", "Approval request has no official thread ID.")
    turn_id = _bounded_text(params.get("turnId") or params.get("turn_id"), 512)
    item_id = _bounded_text(params.get("itemId") or params.get("item_id"), 512)
    if operation == "command":
        default_summary = "Command execution approval requested"
    elif operation == "file_change":
        default_summary = "File change approval requested"
    else:
        default_summary = "Permission profile approval requested"
    # The App Server reason/message may embed argv, absolute paths or other
    # tool input.  Raw params participate only in the request fingerprint;
    # durable/public state gets an operation-level summary.
    summary = default_summary
    requested = params.get("permissions") if operation == "permissions" else {}
    if not isinstance(requested, dict):
        requested = {}
    agentbc = message.get("_agentbc") if isinstance(message.get("_agentbc"), dict) else {}
    native_request_fingerprint = _bounded_text(
        agentbc.get("request_fingerprint"), 160
    )
    native_tool_name = _bounded_text(agentbc.get("tool_name"), 120)
    native_tool_use_id = _bounded_text(agentbc.get("tool_use_id"), 512)
    native_input_fingerprint = _bounded_text(
        agentbc.get("input_fingerprint"), 160
    )
    native_action_fingerprint = _bounded_text(
        agentbc.get("action_fingerprint"), 160
    )
    native_domain = _bounded_text(agentbc.get("escalation_domain"), 120)
    native_profile = _bounded_text(agentbc.get("host_profile_digest"), 160)
    native_control_path = _bounded_text(agentbc.get("control_path"), 160)
    # PERM-104 Plan D: a trusted structured native block may request one
    # native-full elevation.  It carries no choices; all human decisions
    # are represented by the single top-level Approve Full / Deny dialog.
    requested_scope = str(message.get("scope") or "").strip()
    requested_mode = str(message.get("elevation_mode") or "").strip()
    is_v3 = (
        message.get("approval_version") == 3
        or requested_scope == APPROVAL_V3_SCOPE
        or requested_mode == APPROVAL_V3_ELEVATION_MODE
    )
    authority: dict[str, Any] = (
        dict(message.get("authority"))
        if isinstance(message.get("authority"), dict)
        else {}
    )
    if is_v3:
        if requested_scope != APPROVAL_V3_SCOPE:
            raise ControlPlaneError(
                "approval_scope_invalid",
                "A v3 elevation request must use the task_elevation scope.",
            )
        if requested_mode != APPROVAL_V3_ELEVATION_MODE:
            raise ControlPlaneError(
                "approval_elevation_mode_invalid",
                "A v3 elevation request must use full mode.",
            )
        authority_executor = str(authority.get("executor") or "").strip().lower()
        if authority_executor != str(executor or "").strip().lower():
            raise ControlPlaneError(
                "approval_authority_invalid",
                "A v3 elevation authority must match the active executor.",
            )
        protocol = str(authority.get("protocol") or "").strip()
        method = str(authority.get("method") or "").strip()
        protocol_version = authority.get("protocol_version")
        if (
            not protocol
            or not method
            or isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
        ):
            raise ControlPlaneError(
                "approval_authority_invalid",
                "A v3 elevation request requires mechanical authority facts.",
            )
        native_event = _bounded_text(
            message.get("native_event") or agentbc.get("native_event"), 512
        )
        if not native_event:
            raise ControlPlaneError(
                "approval_authority_invalid",
                "A v3 elevation request requires the trusted native event shape.",
            )
        path_digest = _bounded_text(
            message.get("path_plan_digest") or agentbc.get("path_plan_digest"),
            160,
        )
        profile_digest = _bounded_text(
            message.get("containment_profile_digest")
            or message.get("host_profile_digest")
            or agentbc.get("containment_profile_digest")
            or agentbc.get("host_profile_digest"),
            160,
        )
        native_live_elevation = message.get("native_live_elevation") is True
        native_elevation_protocol = _bounded_text(
            message.get("native_elevation_protocol"), 160
        )
        if native_live_elevation:
            if str(executor or "").strip().lower() != "claude":
                raise ControlPlaneError(
                    "approval_authority_invalid",
                    "Live same-session elevation is only defined for Claude.",
                )
            if native_elevation_protocol != "claude.can_use_tool.setMode":
                raise ControlPlaneError(
                    "approval_authority_invalid",
                    "Claude live elevation requires the setMode protocol shape.",
                )
            if authority.get("method") != "sdk.can_use_tool":
                raise ControlPlaneError(
                    "approval_authority_invalid",
                    "Claude live elevation requires the native can_use_tool method.",
                )
            if authority.get("update") != {
                "type": "setMode",
                "mode": "bypassPermissions",
                "destination": "session",
            }:
                raise ControlPlaneError(
                    "approval_authority_invalid",
                    "Claude live elevation requires the exact session setMode update.",
                )
        approval_version = 3
        requested_scope = APPROVAL_V3_SCOPE
        requested_mode = APPROVAL_V3_ELEVATION_MODE
        offered_choices: tuple[dict[str, Any], ...] = ()
        native_tool_use_id = _bounded_text(
            agentbc.get("tool_use_id") or item_id,
            512,
        )
        return ApprovalRequest(
            request_id=request_id,
            request_fingerprint=(
                native_request_fingerprint
                if native_request_fingerprint.startswith("fp-")
                else compute_request_fingerprint(
                    executor=str(executor or "codex"),
                    session_id=str(session_id),
                    tool_name=operation,
                    tool_input=params,
                    extra={"method": method},
                )
            ),
            rpc_id=message.get("id"),
            task_id=str(task_id),
            executor_run_id=str(executor_run_id),
            session_id=str(session_id),
            kind="permission",
            operation=operation,
            summary=summary or default_summary,
            scope=APPROVAL_V3_SCOPE,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            tool_name=native_tool_name,
            tool_use_id=native_tool_use_id,
            input_fingerprint=native_input_fingerprint,
            action_fingerprint=native_action_fingerprint,
            escalation_domain=native_domain,
            profile_digest=profile_digest,
            control_path=native_control_path,
            approval_version=3,
            native_event=native_event,
            authority=authority,
            elevation_mode=APPROVAL_V3_ELEVATION_MODE,
            path_plan_digest=path_digest,
            containment_profile_digest=profile_digest,
            preflight={"status": "passed", "mode": APPROVAL_V3_ELEVATION_MODE},
            native_live_elevation=native_live_elevation,
            native_elevation_protocol=native_elevation_protocol,
        )

    # PERM-104-002 v2: executor-supplied native choice set.  The authority
    # block and offered choices are validated structurally here; semantic
    # validation (schema-supported shapes per executor) happens in the
    # choice builders and the v2 respond path.
    approval_version = 1
    offered_choices: tuple[dict[str, Any], ...] = ()
    raw_offered = message.get("offered_choices")
    if message.get("approval_version") == 2 or isinstance(raw_offered, list):
        authority = (
            message.get("authority")
            if isinstance(message.get("authority"), dict)
            else {}
        )
        if not isinstance(raw_offered, list) or not raw_offered:
            raise ControlPlaneError(
                "approval_choices_missing",
                "A v2 approval request requires the executor's offered choice list.",
            )
        if str(authority.get("executor") or "").strip().lower() != "claude" and not authority:
            # The authority block is required on every v2 request.
            raise ControlPlaneError(
                "approval_authority_invalid",
                "A v2 approval request requires its authority block.",
            )
        approval_version = 2
        normalized: list[dict[str, Any]] = []
        for index, choice in enumerate(raw_offered):
            if not isinstance(choice, dict):
                raise ControlPlaneError(
                    "approval_choices_invalid",
                    "Each offered choice must be an object.",
                )
            native_option_id = _bounded_text(choice.get("native_option_id"), 160)
            if not native_option_id:
                raise ControlPlaneError(
                    "approval_choices_invalid",
                    "Each offered choice requires a native option id.",
                )
            kind = _bounded_text(choice.get("kind"), 40)
            if kind not in {"once", "session", "deny", "other"}:
                raise ControlPlaneError(
                    "approval_choices_invalid",
                    f"Offered choice kind is unsupported: {kind}",
                )
            label = _bounded_text(choice.get("label"), 120)
            entry: dict[str, Any] = {
                "native_option_id": native_option_id,
                "kind": kind,
                "label": label,
                "selectable": choice.get("selectable", True) is not False,
            }
            # PERM-104-002 v2: opaque handles are computed by the control
            # plane from the exact request id + offered shape, so a handle is
            # only ever valid for this exact request and choice.
            from .approval import compute_offered_choice_digest, build_choice_handle

            digest = compute_offered_choice_digest(entry)
            entry["offered_digest"] = digest
            entry["handle"] = build_choice_handle(str(request_id), index, digest)
            normalized.append(entry)
        offered_choices = tuple(normalized)
    return ApprovalRequest(
        request_id=request_id,
        request_fingerprint=(
            native_request_fingerprint
            if native_request_fingerprint.startswith("fp-")
            else compute_request_fingerprint(
                executor=str(executor or "codex"),
                session_id=str(session_id),
                tool_name=operation,
                tool_input=params,
                extra={"method": method},
            )
        ),
        rpc_id=message.get("id"),
        task_id=str(task_id),
        executor_run_id=str(executor_run_id),
        session_id=str(session_id),
        kind="permission",
        operation=operation,
        summary=summary or default_summary,
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=item_id,
        requested_permissions=_bounded_json(requested),
        tool_name=native_tool_name,
        tool_use_id=native_tool_use_id,
        action_fingerprint=native_action_fingerprint,
        escalation_domain=native_domain,
        profile_digest=native_profile,
        control_path=native_control_path,
        approval_version=approval_version,
        offered_choices=offered_choices,
        authority=authority,
    )

def normalize_decision(decision: Any) -> str:
    value = str(decision or "").strip().lower()
    if value not in APPROVAL_DECISIONS:
        raise ControlPlaneError(
            "approval_decision_invalid",
            "Approval accepts only the single-action decisions accept or decline.",
            {"allowed": sorted(APPROVAL_DECISIONS)},
        )
    return value

def codex_offered_choices(
    operation: str,
    *,
    session_decisions_supported: bool,
) -> tuple[dict[str, Any], ...]:
    """Return the exact choices the Codex App Server schema supports.

    ``accept`` (this turn), ``decline``, and — only where the captured schema
    contract proves session scope is supported — ``acceptForSession``.
    Execpolicy/network-policy amendments and ``cancel`` are never offered
    (non-selectable in 1.04A).  The permissions method returns the exact
    turn/session response shapes instead of inferred categories.
    """
    if operation in {"command", "file_change"}:
        choices: list[dict[str, Any]] = [
            {"native_option_id": "accept", "kind": "once", "label": "Approve once"},
            {"native_option_id": "decline", "kind": "deny", "label": "Deny"},
        ]
        if session_decisions_supported:
            choices.insert(
                1,
                {
                    "native_option_id": "acceptForSession",
                    "kind": "session",
                    "label": "Approve for this session",
                },
            )
        return tuple(choices)
    if operation == "permissions":
        return (
            {
                "native_option_id": "accept_turn",
                "kind": "once",
                "label": "Approve for this turn",
            },
            {
                "native_option_id": "accept_session",
                "kind": "session",
                "label": "Approve for this session",
            },
            {
                "native_option_id": "decline",
                "kind": "deny",
                "label": "Deny",
            },
        )
    raise ControlPlaneError(
        "approval_operation_invalid",
        "Codex v2 choices are not defined for this operation.",
        {"operation": operation},
    )

def claude_offered_choices(
    *,
    session_bundle_supported: bool,
) -> tuple[dict[str, Any], ...]:
    """Return the exact choices the Claude SDK can_use_tool contract supports.

    Deny via PermissionResultDeny, once via PermissionResultAllow carrying the
    original input with no updated_permissions.  Session is offered only when
    the callback's current suggestions form a fully valid destination=session
    bundle (validated by the adapter); persistent and bypass modes are never
    selectable choices.
    """
    choices: list[dict[str, Any]] = [
        {"native_option_id": "deny", "kind": "deny", "label": "Deny"},
        {"native_option_id": "allow_once", "kind": "once", "label": "Approve once"},
    ]
    if session_bundle_supported:
        choices.insert(
            2,
            {
                "native_option_id": "allow_session",
                "kind": "session",
                "label": "Approve for this session",
            },
        )
    return tuple(choices)

def hermes_offered_choices(
    options: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return the exact offered ACP session/request_permission options.

    Every original optionId is preserved verbatim and order/label independent;
    options the ACP agent marks non-selectable stay non-selectable.  Unknown
    option shapes fail closed upstream (validate_permission_request).
    """
    choices: list[dict[str, Any]] = []
    for option in options:
        choices.append(
            {
                "native_option_id": str(option.get("native_option_id") or ""),
                "kind": str(option.get("kind") or "other"),
                "label": str(option.get("label") or ""),
                "selectable": bool(option.get("selectable", True)),
            }
        )
    return tuple(choices)

def approval_response_payload_v2(
    request: ApprovalRequest | dict[str, Any],
    *,
    choice_kind: str,
    native_option_id: str,
    decision: str,
) -> dict[str, Any]:
    """Build the executor-native response payload for one selected choice.

    Codex returns only exact schema-supported requestApproval decisions or
    permissions turn/session responses.  Claude's PermissionResult objects are
    resolved on the SDK adapter; the control plane carries the abstract
    behavior contract.  Hermes returns the selected original optionId.
    """
    selected = normalize_decision(decision)
    if isinstance(request, ApprovalRequest):
        operation = request.operation
        authority = request.authority or {}
    else:
        operation = str(request.get("operation") or "")
        authority = request.get("authority")
        authority = authority if isinstance(authority, dict) else {}
    authority_method = str(authority.get("method") or "")
    authority_executor = str(authority.get("executor") or "").strip().lower()
    # Dispatch on the executor authority, never on the bridged operation:
    # Claude and Hermes requests ride Codex-shaped methods on the control
    # plane but MUST return their own payload shapes.
    if authority_method == "session/request_permission":
        # Hermes ACP: the selected original optionId verbatim.
        return {"outcome": {"optionId": native_option_id}}
    if authority_executor == "claude":
        # Claude SDK: the abstract allow/deny contract.  The concrete
        # PermissionResult objects are resolved on the SDK adapter.
        return {"behavior": "allow" if selected == "accept" else "deny"}
    if operation in {"command", "file_change"}:
        if selected == "decline":
            return {"decision": "decline"}
        if native_option_id == "acceptForSession":
            if native_option_id not in CODEX_SCHEMA_SESSION_DECISIONS:
                raise ControlPlaneError(
                    "approval_choice_unsupported",
                    "The session decision is not supported by the captured schema.",
                    {"native_option_id": native_option_id},
                )
            return {"decision": "acceptForSession"}
        if native_option_id == "accept":
            return {"decision": "accept"}
        raise ControlPlaneError(
            "approval_choice_unsupported",
            "The selected native decision is not schema-supported.",
            {"native_option_id": native_option_id},
        )
    if operation == "permissions":
        requested = (
            request.requested_permissions
            if isinstance(request, ApprovalRequest)
            else request.get("requested_permissions")
        )
        scope = "session" if choice_kind == "session" else "turn"
        if selected != "accept" and choice_kind != "deny":
            raise ControlPlaneError(
                "approval_choice_unsupported",
                "Permissions responses support only turn/session accept or deny.",
                {"choice_kind": choice_kind},
            )
        if choice_kind == "deny":
            return {"decision": "decline"}
        return {
            "permissions": _bounded_json(requested)
            if isinstance(requested, dict)
            else {},
            "scope": scope,
            "strictAutoReview": False,
        }
    raise ControlPlaneError(
        "approval_operation_invalid",
        "Approval operation is not supported.",
        {"operation": operation},
    )

def approval_response_payload(request: ApprovalRequest | dict[str, Any], decision: Any) -> dict[str, Any]:
    """Build the schema-compatible one-turn response; never a session grant."""
    selected = normalize_decision(decision)
    live_elevation = (
        request.native_live_elevation
        if isinstance(request, ApprovalRequest)
        else request.get("native_live_elevation") is True
    )
    if live_elevation:
        # This is a redacted description of the native SDK result.  The live
        # callback constructs the actual PermissionResult object with the
        # untouched input; the control plane never persists that input.
        if selected != "accept":
            return {"behavior": "deny"}
        return {
            "behavior": "allow",
            "updatedPermissions": [
                {
                    "type": "setMode",
                    "mode": "bypassPermissions",
                    "destination": "session",
                }
            ],
            "updatedInput": "original_blocked_input",
        }
    operation = request.operation if isinstance(request, ApprovalRequest) else str(request.get("operation") or "")
    if operation in {"command", "file_change"}:
        return {"decision": selected}
    if operation == "permissions":
        requested = request.requested_permissions if isinstance(request, ApprovalRequest) else request.get("requested_permissions")
        return {
            "permissions": _bounded_json(requested) if selected == "accept" and isinstance(requested, dict) else {},
            "scope": "turn",
            "strictAutoReview": False,
        }
    raise ControlPlaneError("approval_operation_invalid", "Approval operation is not supported.")

def _session_rule_response_payload(
    pending: dict[str, Any],
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Retired: session tool rules were removed in PERM-104-002 1.04A.

    Kept only as a fail-closed tombstone: any attempt to carry a rule on a
    response is rejected instead of validated.
    """
    if value is None:
        return None
    raise ControlPlaneError(
        "session_rule_response_invalid",
        "Session tool rules were retired; the legacy matcher grammar is no "
        "longer accepted on any response path.",
    )

__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_METHODS",
    "APPROVAL_V2_ERROR_CHOICE_REQUIRED",
    "ApprovalRequest",
    "CODEX_SCHEMA_SESSION_DECISIONS",
    "CONTROL_VERSION",
    "ControlEvent",
    "ControlPlaneError",
    "STABLE_EVENTS",
    "approval_response_payload",
    "approval_response_payload_v2",
    "claude_offered_choices",
    "codex_offered_choices",
    "hermes_offered_choices",
    "normalize_approval_request",
    "normalize_decision",
]
