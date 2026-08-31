"""Generic Runner/Adapter control-plane primitives.

The control plane is deliberately transport-neutral.  Adapters publish
stable events and wait for a single task-scoped decision; Runner only validates
the identity tuple and writes the decision atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

from .approval import compute_request_fingerprint
from .permission_runtime import (
    PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
    PERMISSION_RUNTIME_DOMAINS,
    action_fingerprint,
    block_fingerprint,
    converge_approved_block,
    load_block_ledger,
    record_block_decision,
    save_block_ledger,
)
from .session import (
    SessionFirstGate,
    SessionRecoveryRequired,
    atomic_write_json,
    read_json,
    utc_now,
)


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


class TransportClosed(RuntimeError):
    """The official stdio transport ended before the turn completed."""


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
    tool_use_id: str = ""
    action_fingerprint: str = ""
    escalation_domain: str = ""
    profile_digest: str = ""
    control_path: str = ""

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
            "scope": "single_action",
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
        }
        if self.operation == "permissions":
            value["requested_permissions"] = _bounded_json(self.requested_permissions)
        for key, item in (
            ("tool_use_id", self.tool_use_id),
            ("action_fingerprint", self.action_fingerprint),
            ("escalation_domain", self.escalation_domain),
            ("profile_digest", self.profile_digest),
            ("control_path", self.control_path),
        ):
            if item:
                value[key] = _bounded_text(item, 512)
        return value


def normalize_approval_request(
    message: dict[str, Any],
    *,
    task_id: str,
    executor_run_id: str,
    session_id: str,
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
    native_tool_use_id = _bounded_text(agentbc.get("tool_use_id"), 512)
    native_action_fingerprint = _bounded_text(
        agentbc.get("action_fingerprint"), 160
    )
    native_domain = _bounded_text(agentbc.get("escalation_domain"), 120)
    native_profile = _bounded_text(agentbc.get("host_profile_digest"), 160)
    native_control_path = _bounded_text(agentbc.get("control_path"), 160)
    return ApprovalRequest(
        request_id=request_id,
        request_fingerprint=(
            native_request_fingerprint
            if native_request_fingerprint.startswith("fp-")
            else compute_request_fingerprint(
                executor="codex",
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
        tool_use_id=native_tool_use_id,
        action_fingerprint=native_action_fingerprint,
        escalation_domain=native_domain,
        profile_digest=native_profile,
        control_path=native_control_path,
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


def approval_response_payload(request: ApprovalRequest | dict[str, Any], decision: Any) -> dict[str, Any]:
    """Build the schema-compatible one-turn response; never a session grant."""
    selected = normalize_decision(decision)
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


class _ControlFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "_ControlFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self.handle.close()


class ApprovalControlPlane:
    """Task-scoped approval state shared by an adapter and Runner."""

    def __init__(
        self,
        root: str | Path,
        *,
        task_id: str,
        executor_run_id: str,
        session_id: str = "",
        executor: str = "codex",
        expected_session_id: str | None = None,
        create: bool = True,
        approval_timeout_s: float = 300.0,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.task_id = str(task_id or "").strip()
        self.executor = str(executor or "").strip().lower()
        self.executor_run_id = str(executor_run_id or "").strip()
        self.session_id = str(session_id or "").strip()
        self.approval_timeout_s = max(float(approval_timeout_s), 0.1)
        self._thread_lock = threading.RLock()
        self._pending_wire: dict[str, dict[str, Any]] = {}
        self.gate = SessionFirstGate(
            self.root,
            task_id=self.task_id,
            executor=self.executor,
            executor_run_id=self.executor_run_id,
            expected_session_id=expected_session_id,
            create=create,
        )
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "control.lock"
        self.responses_root = self.root / "responses"
        if create:
            self.responses_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            with _ControlFileLock(self.lock_path):
                yield

    def _state(self) -> dict[str, Any]:
        return read_json(self.gate.store.state_path) or {
            "version": CONTROL_VERSION,
            "task_id": self.task_id,
            "executor": self.executor,
            "executor_run_id": self.executor_run_id,
            "session_id": self.session_id,
            "status": "pending",
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.gate.store.state_path, state)

    def _append_event(self, event: ControlEvent) -> dict[str, Any]:
        value = event.to_dict()
        self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.events_path.open("a", encoding="utf-8") as handle:
            os.chmod(self.events_path, 0o600)
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return value

    def record_session_started(
        self,
        receipt: dict[str, Any],
        *,
        expected_task_id: str | None = None,
        expected_executor_run_id: str | None = None,
        expected_session_id: str | None = None,
        expected_resumed: bool | None = None,
        expected_source: str | None = None,
    ) -> dict[str, Any]:
        """Persist the one official receipt bound to this control plane.

        Adapters may receive a preallocated receipt (Claude) or an official
        transport receipt (Codex/Hermes).  The receipt schema intentionally
        does not duplicate task/run metadata, so this method validates the
        optional metadata when present *and* validates the expected identity
        supplied by the adapter.  A mismatch is recorded as recovery before
        anything can bind hooks or submit a turn.
        """
        with self._locked():
            raw = receipt if isinstance(receipt, dict) else {}
            expected_task = str(expected_task_id or self.task_id).strip()
            expected_run = str(
                expected_executor_run_id or self.executor_run_id
            ).strip()
            raw_task = str(raw.get("task_id") or "").strip()
            raw_run = str(
                raw.get("executor_run_id") or raw.get("run_id") or ""
            ).strip()
            identity_errors: list[str] = []
            if raw_task and raw_task != expected_task:
                identity_errors.append("task_id does not match the control plane")
            if raw_run and raw_run != expected_run:
                identity_errors.append(
                    "executor_run_id does not match the control plane"
                )
            if expected_session_id is not None:
                actual_session = str(raw.get("session_id") or "").strip()
                if actual_session != str(expected_session_id).strip():
                    identity_errors.append(
                        "session_id does not match the expected execution session"
                    )
            if expected_resumed is not None and raw.get("resumed") is not expected_resumed:
                identity_errors.append("resumed does not match task session history")
            if expected_source is not None and str(raw.get("source") or "") != str(expected_source):
                identity_errors.append("source does not match the executor transport")
            if identity_errors:
                evidence = {
                    "expected_task_id": expected_task,
                    "expected_executor_run_id": expected_run,
                    "expected_session_id": str(expected_session_id or ""),
                    "expected_resumed": expected_resumed,
                    "expected_source": str(expected_source or ""),
                    "errors": identity_errors,
                }
                self._recovery(
                    "session_receipt_run_mismatch",
                    "; ".join(identity_errors),
                    evidence,
                )
                raise SessionRecoveryRequired(
                    "session_receipt_run_mismatch",
                    "; ".join(identity_errors),
                    evidence,
                )
            normalized = self.gate.persist_official_receipt(receipt)
            if self.session_id and normalized["session_id"] != self.session_id:
                evidence = {
                    "expected_session_id": self.session_id,
                    "actual_session_id": normalized["session_id"],
                    "executor_run_id": self.executor_run_id,
                }
                self._recovery(
                    "session_receipt_session_mismatch",
                    "Official session receipt does not match the control plane session.",
                    evidence,
                )
                raise SessionRecoveryRequired(
                    "session_receipt_session_mismatch",
                    "Official session receipt does not match the control plane session.",
                    evidence,
                )
            self.session_id = normalized["session_id"]
            return self._append_event(
                ControlEvent(
                    "session_started",
                    self.task_id,
                    self.executor,
                    self.executor_run_id,
                    self.session_id,
                    details={"resumed": normalized["resumed"], "source": normalized["source"]},
                )
            )

    session_started = record_session_started

    def _recovery(self, code: str, message: str, evidence: dict[str, Any] | None = None) -> None:
        self.gate.mark_recovery(code, message, evidence=evidence)

    def request_approval(self, message: dict[str, Any]) -> dict[str, Any]:
        with self._locked():
            state = self._state()
            try:
                exact_session = self.gate.store.load_for_run(session_id=self.session_id or None)["session_id"]
            except SessionRecoveryRequired as exc:
                self._recovery(exc.code, str(exc), evidence=exc.evidence)
                raise ControlPlaneError(exc.code, str(exc), exc.evidence) from exc
            request = normalize_approval_request(
                message,
                task_id=self.task_id,
                executor_run_id=self.executor_run_id,
                session_id=exact_session,
            )
            if self.executor == "claude":
                # Claude's SDK callback is the only permission authority for
                # this transport.  Require the adapter's complete structured
                # binding before creating a pending request; a callback,
                # stderr, exit code, or requested_permission value cannot
                # substitute for any of these fields.
                identity = (
                    message.get("_agentbc")
                    if isinstance(message.get("_agentbc"), dict)
                    else {}
                )
                expected_identity = {
                    "task_id": self.task_id,
                    "executor_run_id": self.executor_run_id,
                    "session_id": exact_session,
                    "request_id": request.request_id,
                    "tool_use_id": request.item_id,
                    "control_path": "sdk_control_transport",
                }
                actual_identity = {
                    "task_id": str(identity.get("task_id") or "").strip(),
                    "executor_run_id": str(
                        identity.get("executor_run_id") or ""
                    ).strip(),
                    "session_id": request.thread_id,
                    "request_id": str(identity.get("request_id") or "").strip(),
                    "tool_use_id": str(identity.get("tool_use_id") or "").strip(),
                    "control_path": str(
                        identity.get("control_path") or ""
                    ).strip(),
                }
                binding_errors = [
                    key
                    for key, expected_value in expected_identity.items()
                    if actual_identity.get(key) != expected_value
                ]
                request_fingerprint = str(
                    identity.get("request_fingerprint") or ""
                ).strip()
                action_fingerprint_value = str(
                    identity.get("action_fingerprint") or ""
                ).strip()
                domain = str(identity.get("escalation_domain") or "").strip().lower()
                profile_digest = str(
                    identity.get("host_profile_digest") or ""
                ).strip()
                top_level_domain = str(
                    message.get("escalation_domain") or ""
                ).strip().lower()
                top_level_profile = str(
                    message.get("host_profile_digest") or ""
                ).strip()
                if request_fingerprint != request.request_fingerprint:
                    binding_errors.append("request_fingerprint")
                if not request_fingerprint.startswith("fp-"):
                    binding_errors.append("request_fingerprint_missing")
                if not action_fingerprint_value.startswith("fp-"):
                    binding_errors.append("action_fingerprint")
                if domain not in PERMISSION_RUNTIME_DOMAINS:
                    binding_errors.append("escalation_domain")
                if top_level_domain != domain:
                    binding_errors.append("top_level_escalation_domain")
                if not (
                    profile_digest.startswith("sha256:")
                    and len(profile_digest) == len("sha256:") + 64
                    and all(character in "0123456789abcdef" for character in profile_digest[7:])
                ):
                    binding_errors.append("host_profile_digest")
                if top_level_profile != profile_digest:
                    binding_errors.append("top_level_host_profile_digest")
                if binding_errors:
                    evidence = {
                        "request_id": request.request_id,
                        "task_id": self.task_id,
                        "executor_run_id": self.executor_run_id,
                        "session_id": exact_session,
                        "binding_errors": sorted(set(binding_errors)),
                    }
                    self._recovery(
                        PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
                        "Claude native approval evidence is incomplete or mismatched.",
                        evidence,
                    )
                    raise ControlPlaneError(
                        PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
                        "Claude native approval evidence is incomplete or mismatched.",
                        evidence,
                    )
                request = replace(
                    request,
                    tool_use_id=actual_identity["tool_use_id"],
                    action_fingerprint=action_fingerprint_value,
                    escalation_domain=domain,
                    profile_digest=profile_digest,
                    control_path=actual_identity["control_path"],
                )
            if request.thread_id != exact_session:
                evidence = {"request_id": request.request_id, "expected_session_id": exact_session, "actual_session_id": request.thread_id}
                self._recovery("approval_session_mismatch", "Approval request thread does not match the official session.", evidence)
                raise ControlPlaneError("approval_session_mismatch", "Approval request thread does not match the official session.", evidence)
            state_identity = {
                "task_id": str(state.get("task_id") or ""),
                "executor_run_id": str(state.get("executor_run_id") or ""),
                "session_id": str(state.get("session_id") or ""),
            }
            expected_identity = {
                "task_id": self.task_id,
                "executor_run_id": self.executor_run_id,
                "session_id": exact_session,
            }
            if state_identity != expected_identity:
                self._recovery(
                    "approval_identity_mismatch",
                    "Persisted approval state does not match the active control plane.",
                    {"expected": expected_identity, "actual": state_identity, "request_id": request.request_id},
                )
                raise ControlPlaneError(
                    "approval_identity_mismatch",
                    "Persisted approval state does not match the active control plane.",
                    {"expected": expected_identity, "actual": state_identity, "request_id": request.request_id},
                )
            pending = state.get("pending_request")
            if isinstance(pending, dict):
                pending_identity = {
                    "task_id": str(pending.get("task_id") or ""),
                    "executor_run_id": str(pending.get("executor_run_id") or ""),
                    "session_id": str(pending.get("session_id") or ""),
                }
                if pending_identity != expected_identity:
                    message_text = "The pending approval belongs to a different task run or session."
                    evidence = {
                        "expected": expected_identity,
                        "actual": pending_identity,
                        "request_id": request.request_id,
                    }
                    self._recovery("approval_identity_mismatch", message_text, evidence)
                    raise ControlPlaneError("approval_identity_mismatch", message_text, evidence)
                if str(pending.get("request_id")) == request.request_id:
                    code = "approval_request_duplicate"
                    message_text = "The approval request ID was already seen for this task run."
                    evidence = {"pending_request_id": pending.get("request_id"), "request_id": request.request_id}
                    self._recovery(code, message_text, evidence)
                    raise ControlPlaneError(code, message_text, evidence)
            if isinstance(pending, dict) and pending.get("status") == "pending":
                code = "approval_concurrent_request"
                message_text = "A second approval request arrived while one action was pending."
                evidence = {"pending_request_id": pending.get("request_id"), "request_id": request.request_id}
                self._recovery(code, message_text, evidence)
                raise ControlPlaneError(code, message_text, evidence)
            # PERM-104-002 convergence: an identical approved-but-ineffective
            # escalation never creates a second permission input.  Only a
            # trusted structured escalation domain on the block event can
            # reach this point; stderr, natural language and exit codes are
            # diagnostics only.
            domain = str(message.get("escalation_domain") or "").strip().lower()
            profile_digest = str(message.get("host_profile_digest") or "").strip()
            agentbc_identity = (
                message.get("_agentbc")
                if isinstance(message.get("_agentbc"), dict)
                else {}
            )
            structured_action_fingerprint = str(
                agentbc_identity.get("action_fingerprint") or ""
            ).strip()
            if not structured_action_fingerprint.startswith("fp-"):
                structured_action_fingerprint = action_fingerprint(
                    executor=self.executor,
                    session_id=exact_session,
                    operation=str(request.operation or "").strip(),
                )
            if domain:
                if domain not in PERMISSION_RUNTIME_DOMAINS:
                    evidence = {"domain": domain, "request_id": request.request_id}
                    raise ControlPlaneError(
                        PERMISSION_BLOCK_EVIDENCE_UNAVAILABLE,
                        "Permission block evidence names an unsupported escalation domain.",
                        evidence,
                    )
                converged = converge_approved_block(
                    self.root,
                    task_id=self.task_id,
                    session_id=exact_session,
                    executor=self.executor,
                    operation=str(request.operation or "").strip(),
                    domain=domain,
                    profile_digest=profile_digest,
                    action_fingerprint_value=structured_action_fingerprint,
                )
                if converged is not None:
                    evidence = {
                        "request_id": request.request_id,
                        "domain": domain,
                        "code": converged,
                    }
                    self._recovery(
                        converged,
                        "The approved action is still blocked by the same escalation domain; escalation is ineffective.",
                        evidence,
                    )
                    raise ControlPlaneError(
                        converged,
                        "The approved action is still blocked by the same escalation domain; escalation is ineffective.",
                        evidence,
                    )
            if state.get("status") == "needs_recovery":
                raise ControlPlaneError("approval_control_needs_recovery", "Approval control state requires recovery.")
            pending = {
                **request.to_dict(),
                "rpc_id": request.rpc_id,
                "status": "pending",
                "created_at": utc_now(),
                "expires_at": time.time() + self.approval_timeout_s,
            }
            if domain:
                pending["escalation_domain"] = domain
                pending["action_fingerprint"] = structured_action_fingerprint
                pending["profile_digest"] = profile_digest
                pending["block_fingerprint"] = block_fingerprint(
                    task_id=self.task_id,
                    session_id=exact_session,
                    action_fingerprint_value=pending["action_fingerprint"],
                    domain=domain,
                    profile_digest=profile_digest,
                )
            state.update(
                {
                    "version": CONTROL_VERSION,
                    "task_id": self.task_id,
                    "executor": self.executor,
                    "executor_run_id": self.executor_run_id,
                    "session_id": exact_session,
                    "status": "approval_pending",
                    "pending_request": pending,
                    "updated_at": utc_now(),
                }
            )
            self._save_state(state)
            self._pending_wire[request.request_id] = dict(message)
            return self._append_event(
                ControlEvent(
                    "approval_requested",
                    self.task_id,
                    self.executor,
                    self.executor_run_id,
                    exact_session,
                    request_id=request.request_id,
                    details={
                        "kind": request.kind,
                        "operation": request.operation,
                        "scope": "single_action",
                        "request_fingerprint": request.request_fingerprint,
                        "summary": request.summary,
                        "turn_id": request.turn_id,
                        "item_id": request.item_id,
                    },
                )
            )

    def _response_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()
        return self.responses_root / f"{digest}.json"

    def _stale(self, state: dict[str, Any], code: str, message: str) -> None:
        pending = state.get("pending_request") if isinstance(state.get("pending_request"), dict) else {}
        evidence = {"request_id": pending.get("request_id"), "pending_status": pending.get("status")}
        self._recovery(code, message, evidence)

    def respond_approval(
        self,
        task_id: str,
        executor_run_id: str,
        session_id: str,
        request_id: str,
        decision: str,
    ) -> dict[str, Any]:
        selected = normalize_decision(decision)
        with self._locked():
            if str(task_id) != self.task_id or str(executor_run_id) != self.executor_run_id or str(session_id) != self.session_id:
                evidence = {
                    "expected": {"task_id": self.task_id, "executor_run_id": self.executor_run_id, "session_id": self.session_id},
                    "actual": {"task_id": str(task_id), "executor_run_id": str(executor_run_id), "session_id": str(session_id)},
                    "request_id": str(request_id),
                }
                self._recovery("approval_identity_mismatch", "Approval identity does not match the active control plane.", evidence)
                raise ControlPlaneError("approval_identity_mismatch", "Approval identity does not match the active control plane.", evidence)
            try:
                official_session = self.gate.store.load_for_run(session_id=self.session_id)["session_id"]
            except SessionRecoveryRequired as exc:
                self._recovery(exc.code, str(exc), evidence=exc.evidence)
                raise ControlPlaneError(exc.code, str(exc), exc.evidence) from exc
            state = self._state()
            expected_identity = {
                "task_id": self.task_id,
                "executor_run_id": self.executor_run_id,
                "session_id": official_session,
            }
            persisted_identity = {
                "task_id": str(state.get("task_id") or ""),
                "executor_run_id": str(state.get("executor_run_id") or ""),
                "session_id": str(state.get("session_id") or ""),
            }
            pending = state.get("pending_request")
            pending_identity = {
                "task_id": str(pending.get("task_id") or ""),
                "executor_run_id": str(pending.get("executor_run_id") or ""),
                "session_id": str(pending.get("session_id") or ""),
            } if isinstance(pending, dict) else {}
            if persisted_identity != expected_identity or pending_identity != expected_identity:
                evidence = {
                    "expected": expected_identity,
                    "state": persisted_identity,
                    "pending": pending_identity,
                    "request_id": str(request_id),
                }
                self._recovery(
                    "approval_identity_mismatch",
                    "Persisted approval state does not match the active control plane.",
                    evidence,
                )
                raise ControlPlaneError(
                    "approval_identity_mismatch",
                    "Persisted approval state does not match the active control plane.",
                    evidence,
                )
            if not isinstance(pending, dict) or str(pending.get("request_id")) != str(request_id):
                self._stale(state, "approval_request_stale", "Approval request is missing or no longer current.")
                raise ControlPlaneError("approval_request_stale", "Approval request is missing or no longer current.")
            if state.get("status") == "needs_recovery":
                raise ControlPlaneError(
                    "approval_control_needs_recovery",
                    "Approval control state requires explicit recovery before a decision.",
                )
            if pending.get("status") != "pending" or float(pending.get("expires_at") or 0) <= time.time():
                self._stale(state, "approval_request_expired", "Approval request has expired or was already invalidated.")
                raise ControlPlaneError("approval_request_expired", "Approval request has expired or was already invalidated.")
            path = self._response_path(str(request_id))
            if path.exists():
                self._stale(state, "approval_response_duplicate", "Approval response was already recorded.")
                raise ControlPlaneError("approval_response_duplicate", "Approval response was already recorded.")
            response_payload = approval_response_payload(pending, selected)
            response = {
                "version": CONTROL_VERSION,
                "task_id": self.task_id,
                "executor_run_id": self.executor_run_id,
                "session_id": self.session_id,
                "request_id": str(request_id),
                "decision": selected,
                "response_payload": response_payload,
                "responded_at": utc_now(),
            }
            # Commit the decision as one fail-closed control-plane
            # transaction.  The response file is the worker-visible commit
            # marker and is therefore written LAST: if ledger or state
            # persistence fails, the worker cannot observe an approval that
            # the controller reported as failed.  On a final response-write
            # failure, restore the pre-decision state and ledger before
            # propagating the error.
            domain = str(pending.get("escalation_domain") or "").strip()
            previous_state = json.loads(json.dumps(state))
            previous_ledger = load_block_ledger(self.root) if domain else None
            pending = dict(pending)
            pending.update({"status": "responded", "decision": selected, "responded_at": response["responded_at"]})
            state["pending_request"] = pending
            state["status"] = "approval_responded"
            state["updated_at"] = utc_now()
            try:
                # PERM-104-002: persist the trusted decision in the block
                # ledger so a later identical block converges instead of
                # re-asking. ``record_block_decision`` owns the protocol to
                # ledger decision mapping (accept/decline -> approve/deny).
                if domain:
                    record_block_decision(
                        self.root,
                        fingerprint=str(pending.get("block_fingerprint") or ""),
                        task_id=self.task_id,
                        session_id=self.session_id,
                        action_fingerprint_value=str(pending.get("action_fingerprint") or ""),
                        domain=domain,
                        profile_digest=str(pending.get("profile_digest") or ""),
                        decision=selected,
                    )
                self._save_state(state)
                atomic_write_json(path, response)
            except Exception as exc:
                # No response file was committed, so the worker remains
                # blocked.  Restore both durable projections to the exact
                # pre-decision snapshots.  Attempt both rollbacks even if one
                # fails; an incomplete rollback becomes an explicit recovery
                # state instead of hiding behind the original I/O error.
                rollback_errors: list[str] = []
                try:
                    self._save_state(previous_state)
                except Exception as rollback_exc:
                    rollback_errors.append(f"state:{rollback_exc}")
                if previous_ledger is not None:
                    try:
                        save_block_ledger(self.root, previous_ledger)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"ledger:{rollback_exc}")
                if rollback_errors:
                    raise ControlPlaneError(
                        "approval_decision_transaction_incomplete",
                        "Approval decision persistence failed and its rollback "
                        "was incomplete; explicit recovery is required.",
                        {
                            "request_id": str(request_id),
                            "rollback_errors": rollback_errors,
                        },
                    ) from exc
                raise
            return {"ok": True, **response}

    def wait_for_decision(self, request_id: str, timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + max(float(timeout_s if timeout_s is not None else self.approval_timeout_s), 0.1)
        path = self._response_path(str(request_id))
        while time.monotonic() < deadline:
            response = read_json(path)
            if isinstance(response, dict):
                return response
            state = self._state()
            pending = state.get("pending_request") if isinstance(state.get("pending_request"), dict) else {}
            if pending.get("request_id") == str(request_id) and pending.get("status") in {"invalidated", "expired"}:
                raise ControlPlaneError("approval_request_stale", "Approval request is no longer valid.")
            if pending.get("request_id") == str(request_id) and state.get("status") == "needs_recovery":
                raise ControlPlaneError(
                    "approval_control_needs_recovery",
                    "Approval control state requires explicit recovery before a decision.",
                )
            time.sleep(0.02)
        with self._locked():
            state = self._state()
            pending = state.get("pending_request") if isinstance(state.get("pending_request"), dict) else {}
            if pending.get("request_id") == str(request_id) and pending.get("status") == "pending":
                pending = dict(pending)
                pending["status"] = "expired"
                state["pending_request"] = pending
                state["status"] = "needs_recovery"
                state["updated_at"] = utc_now()
                self._save_state(state)
                self._recovery("approval_request_expired", "Approval request timed out without a decision.", {"request_id": str(request_id)})
        raise ControlPlaneError("approval_request_expired", "Approval request timed out without a decision.")

    def invalidate_request(self, request_id: str, reason: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._locked():
            state = self._state()
            pending = state.get("pending_request") if isinstance(state.get("pending_request"), dict) else {}
            if pending.get("request_id") != str(request_id) or pending.get("status") != "pending":
                return {"ok": True, "status": "already_closed", "request_id": str(request_id)}
            pending = dict(pending)
            pending["status"] = "invalidated"
            pending["invalidated_at"] = utc_now()
            state["pending_request"] = pending
            state["status"] = "needs_recovery"
            state["updated_at"] = utc_now()
            self._save_state(state)
            merged = {"request_id": str(request_id), **dict(evidence or {})}
            self._recovery("transport_failed", reason, merged)
            session_id = str(state.get("session_id") or self.session_id)
            return self._append_event(
                ControlEvent(
                    "transport_failed",
                    self.task_id,
                    self.executor,
                    self.executor_run_id,
                    session_id,
                    request_id=str(request_id),
                    details={"reason": _bounded_text(reason), "recovery": True},
                )
            )

    def record_transport_failed(self, reason: str, *, request_id: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if request_id:
            return self.invalidate_request(request_id, reason, evidence=evidence)
        with self._locked():
            state = self._state()
            state["status"] = "needs_recovery"
            state["updated_at"] = utc_now()
            self._save_state(state)
            self._recovery("transport_failed", reason, evidence)
            return self._append_event(
                ControlEvent(
                    "transport_failed",
                    self.task_id,
                    self.executor,
                    self.executor_run_id,
                    str(state.get("session_id") or self.session_id),
                    details={"reason": _bounded_text(reason), "recovery": True},
                )
            )

    def record_turn_completed(self, *, turn_id: str = "", status: str = "completed") -> dict[str, Any]:
        with self._locked():
            state = self._state()
            state["status"] = "turn_completed"
            state["turn_id"] = _bounded_text(turn_id, 512)
            state["turn_status"] = _bounded_text(status, 80)
            state["updated_at"] = utc_now()
            self._save_state(state)
            return self._append_event(
                ControlEvent(
                    "turn_completed",
                    self.task_id,
                    self.executor,
                    self.executor_run_id,
                    str(state.get("session_id") or self.session_id),
                    details={"turn_id": _bounded_text(turn_id, 512), "status": _bounded_text(status, 80)},
                )
            )

    def status(self) -> dict[str, Any]:
        state = self._state()
        pending = state.get("pending_request")
        if isinstance(pending, dict):
            state["pending_request"] = {
                key: value
                for key, value in pending.items()
                if key not in {"rpc_id"}
            }
        return state

    def events(self) -> list[dict[str, Any]]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        values: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                values.append(value)
        return values


RunnerControlPlane = ApprovalControlPlane


def respond_approval(
    task_id: str,
    executor_run_id: str,
    session_id: str,
    request_id: str,
    decision: str,
    *,
    root: str | Path,
    executor: str = "codex",
) -> dict[str, Any]:
    """IPC-facing convenience command used by Runner and Task 3/Core."""
    plane = ApprovalControlPlane(
        root,
        task_id=task_id,
        executor_run_id=executor_run_id,
        session_id=session_id,
        executor=executor,
        create=False,
    )
    return plane.respond_approval(task_id, executor_run_id, session_id, request_id, decision)


class StdioJsonRpcTransport:
    """Minimal JSON-RPC stdio transport for Codex App Server."""

    def __init__(
        self,
        executable: str | Path,
        *,
        cwd: str | Path,
        command: list[str] | None = None,
    ) -> None:
        self.executable = str(executable)
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.command = list(command or [self.executable, "app-server", "--stdio"])
        self.process: subprocess.Popen[str] | None = None
        self._send_lock = threading.Lock()

    def start(self) -> None:
        if self.process is not None:
            return
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise TransportClosed(f"failed to start Codex App Server: {exc}") from exc

    def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise TransportClosed("Codex App Server transport is not started")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._send_lock:
            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise TransportClosed("Codex App Server stdin closed") from exc

    def recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise TransportClosed("Codex App Server transport is not started")
        stream = self.process.stdout
        if timeout_s is not None:
            try:
                ready, _, _ = select.select([stream], [], [], max(float(timeout_s), 0.0))
            except (OSError, ValueError) as exc:
                raise TransportClosed("Codex App Server stdout is unavailable") from exc
            if not ready:
                raise TimeoutError("Codex App Server receive timed out")
        line = stream.readline()
        if not line:
            raise TransportClosed("Codex App Server transport closed")
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            raise TransportClosed("Codex App Server sent invalid JSON") from exc
        if not isinstance(value, dict):
            raise TransportClosed("Codex App Server sent a non-object message")
        return value

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except OSError:
                    pass


CodexAppServerTransport = StdioJsonRpcTransport


__all__ = [
    "APPROVAL_DECISIONS",
    "ApprovalControlPlane",
    "ApprovalRequest",
    "CodexAppServerTransport",
    "ControlEvent",
    "ControlPlaneError",
    "RunnerControlPlane",
    "STABLE_EVENTS",
    "StdioJsonRpcTransport",
    "TransportClosed",
    "approval_response_payload",
    "normalize_approval_request",
    "normalize_decision",
    "respond_approval",
]
