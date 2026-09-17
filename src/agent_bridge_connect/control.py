# ruff: noqa: F822
"""Durable task-scoped control-plane compatibility facade.

Protocol normalization lives in approval_protocol; stdio process I/O lives in
control_transport; durable state lives in control_runtime. This module keeps
historical public imports and signatures stable.
"""

from __future__ import annotations

import sys
import types

from . import approval as _approval
from . import approval_protocol as _approval_protocol
from . import control_runtime as _control_runtime
from . import control_transport as _control_transport
from . import permission_runtime as _permission_runtime
from . import session as _session

_FACADE_EXPORTS = {
    _approval: "APPROVAL_V3_ELEVATION_MODE APPROVAL_V3_SCOPE compute_request_fingerprint",
    _approval_protocol: "APPROVAL_DECISIONS APPROVAL_METHODS APPROVAL_V2_ERROR_CHOICE_REQUIRED ApprovalRequest CODEX_SCHEMA_SESSION_DECISIONS CONTROL_VERSION ControlEvent ControlPlaneError STABLE_EVENTS approval_response_payload approval_response_payload_v2 claude_offered_choices codex_offered_choices hermes_offered_choices normalize_approval_request normalize_decision",
    _control_runtime: "ApprovalControlPlane RunnerControlPlane respond_approval",
    _control_transport: "CodexAppServerTransport StdioJsonRpcTransport TransportClosed",
    _permission_runtime: "PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE PERMISSION_RUNTIME_DOMAINS action_fingerprint block_fingerprint converge_approved_block load_block_ledger record_block_decision save_block_ledger",
    _session: "SessionFirstGate SessionRecoveryRequired atomic_write_json read_json utc_now",
}
for _module, _names in _FACADE_EXPORTS.items():
    globals().update({name: getattr(_module, name) for name in _names.split()})


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
