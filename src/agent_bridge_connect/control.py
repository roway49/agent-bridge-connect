"""Durable task-scoped control-plane compatibility facade.

Protocol normalization lives in approval_protocol; stdio process I/O lives in
control_transport; durable state lives in control_runtime. This module keeps
historical public imports and signatures stable.
"""

from __future__ import annotations

import sys
import types

from . import control_runtime as _control_runtime
from .approval import (  # noqa: F401
    APPROVAL_V3_ELEVATION_MODE,
    APPROVAL_V3_SCOPE,
    compute_request_fingerprint,
)
from .approval_protocol import (
    APPROVAL_DECISIONS,
    APPROVAL_METHODS,
    APPROVAL_V2_ERROR_CHOICE_REQUIRED,
    ApprovalRequest,
    CODEX_SCHEMA_SESSION_DECISIONS,
    CONTROL_VERSION,  # noqa: F401
    ControlEvent,
    ControlPlaneError,
    STABLE_EVENTS,
    approval_response_payload,
    approval_response_payload_v2,
    claude_offered_choices,
    codex_offered_choices,
    hermes_offered_choices,
    normalize_approval_request,
    normalize_decision,
)
from .control_runtime import (
    ApprovalControlPlane,
    RunnerControlPlane,
    respond_approval,
)
from .control_transport import (
    CodexAppServerTransport,
    StdioJsonRpcTransport,
    TransportClosed,
)
from .permission_runtime import (  # noqa: F401
    PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
    PERMISSION_RUNTIME_DOMAINS,
    action_fingerprint,
    block_fingerprint,
    converge_approved_block,
    load_block_ledger,
    record_block_decision,
    save_block_ledger,
)
from .session import (  # noqa: F401
    SessionFirstGate,
    SessionRecoveryRequired,
    atomic_write_json,
    read_json,
    utc_now,
)


class _ControlFacadeModule(types.ModuleType):
    """Keep legacy monkeypatch points bound to the runtime globals."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name in {
            "SessionFirstGate",
            "SessionRecoveryRequired",
            "action_fingerprint",
            "atomic_write_json",
            "block_fingerprint",
            "converge_approved_block",
            "load_block_ledger",
            "read_json",
            "record_block_decision",
            "save_block_ledger",
            "utc_now",
        }:
            setattr(_control_runtime, name, value)


sys.modules[__name__].__class__ = _ControlFacadeModule

__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_METHODS",
    "APPROVAL_V2_ERROR_CHOICE_REQUIRED",
    "ApprovalControlPlane",
    "ApprovalRequest",
    "CODEX_SCHEMA_SESSION_DECISIONS",
    "CodexAppServerTransport",
    "ControlEvent",
    "ControlPlaneError",
    "RunnerControlPlane",
    "STABLE_EVENTS",
    "StdioJsonRpcTransport",
    "TransportClosed",
    "approval_response_payload",
    "approval_response_payload_v2",
    "claude_offered_choices",
    "codex_offered_choices",
    "hermes_offered_choices",
    "normalize_approval_request",
    "normalize_decision",
    "respond_approval",
]
