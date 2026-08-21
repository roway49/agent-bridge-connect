from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_bridge_connect.adapters import (
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    SessionCleanupCapability,
    SessionCleanupRequest,
    SessionCleanupResult,
    StartResult,
)
from agent_bridge_connect.execution_contract import (
    detect_retryable_transport_failure,
    extract_callback_validation_from_events,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.control import (
    ApprovalControlPlane,
    ControlPlaneError,
    StdioJsonRpcTransport,
    TransportClosed,
    approval_response_payload,
)
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.media import task_image_paths
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import PromptPlatformExtras, build_prompt_contract
from agent_bridge_connect.runner import RunnerClient, RunnerError
from agent_bridge_connect.session import SessionRecoveryRequired

from .base import CLIExecutorBase
from ..path_provider import find_binary

SAFETY_TIMEOUT_S = 24 * 60 * 60
SESSION_EXTENSION_KEY = "agentbc.session"
CODEX_CLEANUP_UNSUPPORTED_CODE = "codex_session_delete_unavailable"
CODEX_SESSION_DELETE_FAILED_CODE = "codex_session_delete_failed"
CODEX_SESSION_DELETE_INVALID_ID_CODE = "codex_session_delete_invalid_session_id"
_CODEX_FROZEN_HELP_FIXTURE = "codex_0.146.0_help.txt"
_CODEX_FROZEN_VERSION = "0.146.0"
_CODEX_CLEANUP_TIMEOUT_S = 60
_CODEX_SESSION_ABSENT_RE = re.compile(
    r"(?im)^(?:session|saved session).*(?:not found|does not exist)"
)


class CodexExecutor(CLIExecutorBase):
    """L2 Codex CLI adapter using blocking JSONL execution."""

    def __init__(
        self,
        timeout_s: int = SAFETY_TIMEOUT_S,
        command: str | None = None,
        *,
        transport: str | Any = "auto",
        transport_factory: Any | None = None,
        approval_timeout_s: float = 300.0,
    ) -> None:
        super().__init__()
        self.timeout_s = timeout_s
        self.transport_mode = transport
        self.transport_factory = transport_factory
        self.approval_timeout_s = max(float(approval_timeout_s), 0.1)
        self._discovery = _discover_codex_binary(command)
        resolved = str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}
        self._app_runs: dict[str, dict[str, Any]] = {}
        self._app_server_capability: dict[str, Any] | None = None
        # Test seam: an injected pre-verified App Server capability report
        # (same shape as :func:`codex_app_server_contract`) replaces the
        # subprocess schema probe.  Production always runs the real probe.
        self._app_server_capability_override: dict[str, Any] | None = None

    def probe(self) -> ProbeResult:
        if self.agent_bin is None:
            return ProbeResult(
                ok=False,
                message="codex unavailable",
                details={
                    "agent_bin": "",
                    "agent_bin_source": self._discovery.get("source") or "not_found",
                    "searched_paths": self._discovery.get("searched_paths") or [],
                    "manual_override": self._discovery.get("manual_override") or "",
                },
            )
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "--version"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult(
                ok=False,
                message=f"codex unavailable: {exc}",
                details={
                    "agent_bin": str(self.agent_bin),
                    "agent_bin_source": self._discovery.get("source") or "unknown",
                },
            )

        version = (completed.stdout or completed.stderr).strip()
        return ProbeResult(
            ok=completed.returncode == 0,
            message=version or f"codex exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
                "agent_bin_source": self._discovery.get("source") or "unknown",
                "returncode": completed.returncode,
                "version": version,
            },
        )

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            structured_output=True,
            streaming_events=True,
            resume=True,
            cancel=False,
            input_required=self._uses_app_server_transport(),
            model_selection=True,
            multimodal=True,
            image_input=True,
            image_generation=True,
            image_editing=True,
            parallelism=1,
            level=ExecutorLevel.L2,
        )

    def session_cleanup_capability(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupCapability:
        """Probe the discovered CLI help without reading any saved session data."""
        if request.retain is True:
            return SessionCleanupCapability("not_applicable", "retain")
        if self.agent_bin is None:
            return _codex_cleanup_unsupported()
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "delete", "--help"],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _codex_cleanup_unsupported()
        if completed.returncode != 0:
            return _codex_cleanup_unsupported()
        return _codex_session_cleanup_capability(
            f"{completed.stdout or ''}\n{completed.stderr or ''}"
        )

    def cleanup_session(self, request: SessionCleanupRequest) -> SessionCleanupResult:
        """Delete one exact official UUID through ``codex delete --force``."""
        if request.retain is True:
            return SessionCleanupResult("retained", "not_applicable", "retain")
        request_error = _codex_cleanup_request_error(request)
        if request_error:
            return SessionCleanupResult(
                "failed",
                "supported",
                "official_session_delete",
                request_error,
                False,
            )
        capability = self.session_cleanup_capability(request)
        if capability.capability != "supported":
            return SessionCleanupResult(
                "unsupported",
                "unsupported",
                "none",
                capability.error_code or CODEX_CLEANUP_UNSUPPORTED_CODE,
                False,
            )
        assert self.agent_bin is not None
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "delete", "--force", request.session_id],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=_CODEX_CLEANUP_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return SessionCleanupResult(
                "failed",
                "supported",
                "official_session_delete",
                CODEX_SESSION_DELETE_FAILED_CODE,
                True,
            )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode == 0 or _CODEX_SESSION_ABSENT_RE.search(output):
            return SessionCleanupResult(
                "succeeded",
                "supported",
                "official_session_delete",
            )
        return SessionCleanupResult(
            "failed",
            "supported",
            "official_session_delete",
            CODEX_SESSION_DELETE_FAILED_CODE,
            False,
        )

    def start(self, task_packet: dict) -> StartResult:
        steps = task_packet.get("steps") or []
        if not steps:
            return StartResult(ok=False, run_id="", message="no steps")
        if self.agent_bin is None:
            return StartResult(ok=False, run_id="", message="codex unavailable")

        workspace = task_packet.get("workspace") or {}
        root = Path(workspace.get("root", ".")).expanduser().resolve()
        if not root.is_dir():
            return StartResult(ok=False, run_id="", message=f"workspace not found: {root}")

        if self._uses_app_server_transport(task_packet):
            return self._start_app_server(task_packet, root)

        run_id = f"codex-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "codex")
        prompt = _build_prompt(task_packet)
        try:
            permission = resolve_effective_permission(
                task_packet,
                "codex",
                run_id,
                trusted_runner_managed=(
                    task_packet.get("runner_authorization_required") is True
                ),
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"{exc.code}: {exc}")
        try:
            assert_executor_permission_supported(
                "codex", permission["effective_mode"], self.agent_bin
            )
            resumed, _ = _codex_resume_context(task_packet)
            command, prompt_input = self._build_command(
                task_packet,
                prompt,
                root,
                permission,
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=str(exc))

        try:
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command(
                    "codex",
                    command,
                    root,
                    task_packet,
                    executor_run_id=run_id,
                )
            self._heartbeat_run(run_id)
            completed = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                input=prompt_input,
                check=False,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            events = _parse_jsonl(exc.stdout or "")
            self._store_metadata(run_id, root, events, returncode=None)
            self._mark_run_stale(run_id)
            timeout_result: dict[str, Any] = {
                "events": events,
                "reason": f"codex safety runtime exceeded after {self.timeout_s}s",
                "timeout_is_failure": False,
                "failure": {
                    "kind": "executor_timeout",
                    "layer": "executor",
                    "message": f"codex safety runtime exceeded after {self.timeout_s}s",
                    "retryable": True,
                },
                "extensions": self.get_extensions(),
            }
            execution_session = _execution_session_receipt(events, resumed=resumed)
            if execution_session is not None:
                timeout_result["execution_session"] = execution_session
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"events_seen": len(events)},
                result=timeout_result,
            )
            return StartResult(ok=True, run_id=run_id, message="codex execution needs recovery")
        except (OSError, RunnerError) as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"failed to start codex: {exc}")

        self._heartbeat_run(run_id)
        events = _parse_jsonl(completed.stdout)
        summary = _extract_summary(events)
        validation = extract_callback_validation_from_events(
            events,
            task_packet,
            run_id,
        )
        terminal = route_executor_terminal(
            validation,
            completed.returncode,
            executor_name="codex",
            stderr=completed.stderr,
            runtime_failure=detect_retryable_transport_failure(completed.stdout, completed.stderr),
        )
        status = terminal.status
        result = {
            "events": events,
            "summary": summary,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "agent_callback": terminal.callback,
            "marker_valid": validation.valid,
            "marker_seen": validation.marker_seen,
            "failure": terminal.failure,
        }
        execution_session = _execution_session_receipt(events, resumed=resumed)
        if execution_session is not None:
            result["execution_session"] = execution_session
        self._store_metadata(run_id, root, events, returncode=completed.returncode)
        result["extensions"] = self.get_extensions()
        self._runs[run_id] = PollResult(
            status=status,
            progress={"events_seen": len(events)},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"codex execution {status}")

    def _uses_app_server_transport(self, task_packet: dict[str, Any] | None = None) -> bool:
        if not isinstance(self.transport_mode, str):
            # Injected fake transport objects in tests are always App Server.
            return True
        from agent_bridge_connect.codex_app_server import (
            CODEX_APP_SERVER_TRANSPORT_ALIASES,
        )

        transport = self.transport_mode.strip().lower()
        if transport in {"cli", "direct"}:
            return False
        from agent_bridge_connect.permission_grants import permission_grant_from_extensions

        extensions = (task_packet or {}).get("extensions")
        grant = permission_grant_from_extensions(
            extensions if isinstance(extensions, dict) else {}
        )
        if grant is not None and grant["state"]["status"] != "revoked":
            # A compatibility full continuation is an explicit CLI run even
            # when the task's inherited/safe base normally uses App Server.
            return False
        permission = permission_record_from_extensions(
            extensions,
            allow_legacy=True,
        )
        # Full remains the explicit non-interactive CLI path.  Inherit and safe
        # use App Server when transport is auto/app-server so runtime approval
        # is driven by an authenticated native request, not by a mode-name gate.
        if permission["effective_mode"] == "full":
            return False
        return transport == "auto" or transport in CODEX_APP_SERVER_TRANSPORT_ALIASES

    def _freeze_app_server_capability(self, permission: dict[str, Any]) -> dict[str, Any]:
        """Verify App Server for a runtime-approvable inherit/safe run.

        Inherit supplies no sandbox or approval override; App Server only adds
        the structured transport needed to identify a real blocked action and
        resume the same official session. Full remains the CLI path.
        """
        from agent_bridge_connect.codex_app_server import (
            CODEX_APP_SERVER_TRANSPORT,
            assert_codex_app_server_capability,
        )

        mode = str(permission.get("effective_mode") or "").strip().lower()
        if mode not in {"inherit", "safe"}:
            raise ABCError(
                "permission_capability_unsupported",
                (
                    "Codex App Server single-action chain requires a runtime-"
                    "approvable inherit or safe base; "
                    f"got {mode or 'inherit'}."
                ),
                {
                    "executor": "codex",
                    "permission_mode": mode,
                    "transport": CODEX_APP_SERVER_TRANSPORT,
                },
            )
        if self._app_server_capability is None:
            if self._app_server_capability_override is not None:
                override = dict(self._app_server_capability_override)
                if override.get("ok") is not True:
                    raise ABCError(
                        "permission_capability_unsupported",
                        str(override.get("reason") or "App Server capability override failed"),
                        {
                            "executor": "codex",
                            "permission_mode": mode,
                            "transport": CODEX_APP_SERVER_TRANSPORT,
                            "reason": override.get("reason"),
                        },
                    )
                self._app_server_capability = override
            else:
                self._app_server_capability = assert_codex_app_server_capability(
                    self.agent_bin, transport=CODEX_APP_SERVER_TRANSPORT
                )
        return dict(self._app_server_capability)

    def _build_app_server_command(self) -> list[str]:
        if self.agent_bin is None:
            raise RuntimeError("codex unavailable")
        return [str(self.agent_bin), "app-server", "--stdio"]

    def _start_app_server(self, task_packet: dict[str, Any], root: Path) -> StartResult:
        run_id = f"codex-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "codex")
        try:
            permission = resolve_effective_permission(
                task_packet,
                "codex",
                run_id,
                trusted_runner_managed=(
                    task_packet.get("runner_authorization_required") is True
                ),
            )
            assert_executor_permission_supported(
                "codex", permission["effective_mode"], self.agent_bin
            )
            # Capability gate is transport- and receipt-based. Inherit keeps
            # native permission settings, safe supplies the conservative
            # workspace policy, and full keeps the CLI fallback.
            self._freeze_app_server_capability(permission)
            resumed, explicit_session_id = _codex_resume_context(task_packet)
            command = self._build_app_server_command()
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command(
                    "codex",
                    command,
                    root,
                    task_packet,
                    executor_run_id=run_id,
                )
            plane = self._control_plane_for_run(
                task_packet,
                run_id,
                expected_session_id=explicit_session_id if resumed else None,
            )
        except (ABCError, RunnerError, ControlPlaneError, SessionRecoveryRequired) as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"codex App Server unavailable: {exc}")

        record: dict[str, Any] = {
            "run_id": run_id,
            "task_packet": dict(task_packet),
            "root": root,
            "permission": permission,
            "resumed": resumed,
            "explicit_session_id": explicit_session_id,
            "plane": plane,
            "status": "starting",
            "events": [],
            "result": {},
            "next_rpc_id": 1,
            "ready": threading.Event(),
            "started_at": time.time(),
            "transport": None,
        }
        self._app_runs[run_id] = record
        worker = threading.Thread(
            target=self._run_app_server,
            args=(run_id,),
            name=f"agentbc-codex-app-server-{run_id}",
            daemon=True,
        )
        record["thread"] = worker
        worker.start()
        record["ready"].wait(timeout=min(max(self.timeout_s, 0.1), 10.0))
        return StartResult(ok=True, run_id=run_id, message="codex App Server run started")

    def poll(self, run_id: str) -> PollResult:
        app_run = self._app_runs.get(run_id)
        if app_run is None:
            return super().poll(run_id)
        return PollResult(
            status=str(app_run.get("status") or "running"),
            progress={
                "events_seen": len(app_run.get("events") or []),
                "control_root": str(app_run["plane"].root),
            },
            result=dict(app_run.get("result") or {}),
        )

    def _make_app_server_transport(
        self,
        run_id: str,
        task_packet: dict[str, Any],
        root: Path,
        command: list[str],
    ) -> Any:
        if not isinstance(self.transport_mode, str) and hasattr(self.transport_mode, "send"):
            return self.transport_mode
        factory = self.transport_factory
        if factory is not None:
            attempts = (
                lambda: factory(run_id=run_id, task_packet=task_packet, cwd=root, command=command),
                lambda: factory(run_id, task_packet, root),
                lambda: factory(),
            )
            last_error: Exception | None = None
            for attempt in attempts:
                try:
                    return attempt()
                except TypeError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
        return StdioJsonRpcTransport(str(self.agent_bin), cwd=root, command=command)

    @staticmethod
    def _transport_start(transport: Any) -> None:
        starter = getattr(transport, "start", None)
        if callable(starter):
            starter()

    @staticmethod
    def _transport_send(transport: Any, message: dict[str, Any]) -> None:
        sender = getattr(transport, "send", None)
        if not callable(sender):
            raise TransportClosed("Codex App Server fake transport has no send method")
        sender(message)

    @staticmethod
    def _transport_recv(transport: Any) -> dict[str, Any]:
        receiver = getattr(transport, "recv", None) or getattr(transport, "receive", None)
        if not callable(receiver):
            raise TransportClosed("Codex App Server fake transport has no recv method")
        message = receiver()
        if not isinstance(message, dict):
            raise TransportClosed("Codex App Server transport returned a non-object")
        return message

    @staticmethod
    def _transport_close(transport: Any) -> None:
        closer = getattr(transport, "close", None)
        if callable(closer):
            closer()

    @staticmethod
    def _transport_is_alive(transport: Any) -> bool:
        process = getattr(transport, "process", None)
        if process is not None and callable(getattr(process, "poll", None)):
            return process.poll() is None
        checker = getattr(transport, "is_alive", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        value = getattr(transport, "alive", None)
        return bool(value) if isinstance(value, bool) else True

    def _app_rpc(self, record: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> int:
        request_id = int(record["next_rpc_id"])
        record["next_rpc_id"] = request_id + 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._transport_send(record["transport"], message)
        return request_id

    def _app_notification(self, record: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._transport_send(record["transport"], message)

    def _app_event(self, record: dict[str, Any], message: dict[str, Any]) -> None:
        method = str(message.get("method") or "rpc_response")
        payload = message.get("params") if isinstance(message.get("params"), dict) else message.get("result")
        record["events"].append(
            {
                "event_type": method,
                "source": "codex_app_server",
                "sequence": len(record["events"]) + 1,
                "payload": payload if isinstance(payload, dict) else message,
            }
        )

    def _app_server_permission_params(
        self,
        task_packet: dict[str, Any],
        root: Path,
        permission: dict[str, Any],
    ) -> dict[str, Any]:
        mode = str(permission.get("effective_mode") or "inherit").strip().lower()
        params: dict[str, Any] = {"cwd": str(root)}
        if mode == "safe":
            params.update(
                {
                    "sandbox": "workspace-write",
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                }
            )
        elif mode == "full":
            params.update({"sandbox": "danger-full-access", "approvalPolicy": "never"})

        # Task 1 may expose an explicit v2 mapping.  Accept only the narrow
        # App Server fields; the legacy effective mode remains the fallback.
        extensions = task_packet.get("extensions") if isinstance(task_packet.get("extensions"), dict) else {}
        for key in ("agentbc.permission.v2", "agentbc.permissions.v2", "agentbc.permission_mapping"):
            mapping = extensions.get(key)
            if isinstance(mapping, dict):
                codex_mapping = mapping.get("codex") if isinstance(mapping.get("codex"), dict) else mapping
                if isinstance(codex_mapping, dict):
                    for field in ("sandbox", "approvalPolicy", "approvalsReviewer"):
                        value = codex_mapping.get(field)
                        if isinstance(value, str) and value.strip():
                            params[field] = value.strip()
        return params

    @staticmethod
    def _thread_id_from_message(message: dict[str, Any]) -> str:
        result = message.get("result") if isinstance(message.get("result"), dict) else {}
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        candidates = [thread.get("id"), result.get("threadId")]
        values = [str(value).strip() for value in candidates if isinstance(value, str) and value.strip()]
        return values[0] if len(set(values)) == 1 else (values[0] if len(values) == 1 else "")

    def _app_wait_response(self, record: dict[str, Any], request_id: int) -> dict[str, Any]:
        while True:
            message = self._transport_recv(record["transport"])
            method = str(message.get("method") or "")
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            }:
                self._handle_app_approval(record, message)
                continue
            if message.get("id") == request_id:
                if isinstance(message.get("error"), dict):
                    raise ControlPlaneError(
                        "codex_app_server_rpc_error",
                        "Codex App Server rejected an AgentBC control request.",
                        {"method": str(message.get("method") or "unknown")},
                    )
                return message
            self._app_event(record, message)
            if method == "turn/completed":
                record["completion"] = message

    def _app_wait_turn_completed(self, record: dict[str, Any]) -> dict[str, Any]:
        if isinstance(record.get("completion"), dict):
            return record["completion"]
        while True:
            message = self._transport_recv(record["transport"])
            method = str(message.get("method") or "")
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            }:
                self._handle_app_approval(record, message)
                continue
            self._app_event(record, message)
            if method == "turn/completed":
                return message

    def _handle_app_approval(self, record: dict[str, Any], message: dict[str, Any]) -> None:
        plane: ApprovalControlPlane = record["plane"]
        event = plane.request_approval(message)
        request_id = str(event.get("request_id") or "")
        record["events"].append(
            {
                "event_type": "approval_requested",
                "source": "agentbc.control",
                "sequence": len(record["events"]) + 1,
                "payload": event,
            }
        )
        approval = {
            "type": "permission",
            "request_id": request_id,
            "request_fingerprint": str(event.get("request_fingerprint") or ""),
            "kind": str(event.get("operation") or "permission"),
            "operation": str(event.get("operation") or "permission"),
            "summary": str(event.get("summary") or ""),
            "scope": "single_action",
            "session_id": str(event.get("session_id") or ""),
        }
        record["status"] = "input_required"
        record["result"] = {
            "events": list(record["events"]),
            "execution_session": record.get("execution_session"),
            "approval_request": approval,
            "extensions": self.get_extensions(),
        }
        self._runs[record["run_id"]] = PollResult(
            status="input_required",
            progress={"events_seen": len(record["events"])},
            result=dict(record["result"]),
        )
        self._suspend_run(record["run_id"])
        monitor_stop = threading.Event()

        def monitor_transport() -> None:
            while not monitor_stop.wait(0.05):
                if self._transport_is_alive(record["transport"]):
                    continue
                try:
                    plane.invalidate_request(
                        request_id,
                        "Codex App Server transport died while approval was pending",
                        evidence={"phase": "approval_wait"},
                    )
                except Exception:
                    pass
                return

        monitor = threading.Thread(
            target=monitor_transport,
            name=f"agentbc-codex-approval-watch-{record['run_id']}",
            daemon=True,
        )
        monitor.start()
        try:
            response = plane.wait_for_decision(request_id, self.approval_timeout_s)
            response_payload = response.get("response_payload")
            if not isinstance(response_payload, dict):
                pending = plane.status().get("pending_request")
                if not isinstance(pending, dict):
                    raise ControlPlaneError("approval_response_missing", "Approval response payload is unavailable.")
                response_payload = approval_response_payload(pending, response.get("decision"))
            self._resume_run(record["run_id"])
            record["status"] = "running"
            record.setdefault("approval_history", []).append(
                {"request_id": request_id, "decision": str(response.get("decision") or "")}
            )
            rpc_response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": response_payload,
            }
            self._transport_send(record["transport"], rpc_response)
        except (ControlPlaneError, SessionRecoveryRequired):
            raise
        finally:
            monitor_stop.set()
            monitor.join(timeout=0.2)

    def _run_app_server(self, run_id: str) -> None:
        record = self._app_runs[run_id]
        plane: ApprovalControlPlane = record["plane"]
        transport: Any = None
        try:
            command = self._build_app_server_command()
            transport = self._make_app_server_transport(run_id, record["task_packet"], record["root"], command)
            record["transport"] = transport
            self._transport_start(transport)
            initialize_id = self._app_rpc(
                record,
                "initialize",
                {
                    "clientInfo": {"name": "agentbc", "version": "1.0.3A"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._app_wait_response(record, initialize_id)
            self._app_notification(record, "initialized")

            common_params = self._app_server_permission_params(
                record["task_packet"], record["root"], record["permission"]
            )
            if record["resumed"]:
                thread_id = str(record["explicit_session_id"] or "").strip()
                if not thread_id:
                    raise SessionRecoveryRequired(
                        "missing_executor_session_id",
                        "Explicit resume requires a task session ID.",
                    )
                thread_params = {"threadId": thread_id, **common_params}
                thread_method = "thread/resume"
            else:
                thread_params = dict(common_params)
                thread_method = "thread/start"
            thread_rpc_id = self._app_rpc(record, thread_method, thread_params)
            thread_response = self._app_wait_response(record, thread_rpc_id)
            official_thread_id = self._thread_id_from_message(thread_response)
            if not official_thread_id:
                raise SessionRecoveryRequired(
                    "session_receipt_missing",
                    "Codex App Server thread response did not contain an official thread ID.",
                )
            if record["resumed"] and official_thread_id != record["explicit_session_id"]:
                raise SessionRecoveryRequired(
                    "session_receipt_run_mismatch",
                    "Codex App Server resume returned a different official thread ID.",
                    {"expected_session_id": record["explicit_session_id"], "actual_session_id": official_thread_id},
                )
            receipt = {
                "version": 1,
                "executor": "codex",
                "session_id": official_thread_id,
                "resumed": bool(record["resumed"]),
                "persistence": "persistent",
                "source": "jsonl_thread_started",
            }
            session_event = plane.record_session_started(receipt)
            record["execution_session"] = receipt
            record["session_id"] = official_thread_id
            record["events"].append(
                {
                    "event_type": "session_started",
                    "source": "agentbc.control",
                    "sequence": len(record["events"]) + 1,
                    "payload": session_event,
                }
            )
            # This is the atomic gate: only now may the user prompt enter the
            # App Server turn input.
            plane.gate.require_before_turn(official_thread_id)
            record["ready"].set()
            prompt = _build_prompt(
                record["task_packet"],
                native_single_action=True,
            )
            inputs: list[dict[str, Any]] = [
                {"type": "localImage", "path": str(image)}
                for image in task_image_paths(record["task_packet"])
            ]
            inputs.append({"type": "text", "text": prompt})
            turn_id = self._app_rpc(
                record,
                "turn/start",
                {"threadId": official_thread_id, "input": inputs},
            )
            turn_response = self._app_wait_response(record, turn_id)
            turn_result = turn_response.get("result") if isinstance(turn_response.get("result"), dict) else {}
            turn = turn_result.get("turn") if isinstance(turn_result.get("turn"), dict) else {}
            record["turn_id"] = str(turn.get("id") or "")
            completed_message = self._app_wait_turn_completed(record)
            completed_params = completed_message.get("params") if isinstance(completed_message.get("params"), dict) else {}
            completed_turn = completed_params.get("turn") if isinstance(completed_params.get("turn"), dict) else {}
            turn_status = str(completed_turn.get("status") or "completed")
            plane.record_turn_completed(turn_id=str(completed_turn.get("id") or record.get("turn_id") or ""), status=turn_status)
            agent_events = _app_server_agent_message_events(record["events"])
            validation = extract_callback_validation_from_events(
                agent_events,
                record["task_packet"],
                run_id,
            )
            terminal = route_executor_terminal(
                validation,
                0,
                executor_name="codex",
            )
            result = {
                "events": list(record["events"]),
                "summary": _extract_summary(agent_events),
                "returncode": 0,
                "execution_session": receipt,
                "agent_callback": terminal.callback,
                "marker_valid": validation.valid,
                "marker_seen": validation.marker_seen,
                "failure": terminal.failure,
                "extensions": self.get_extensions(),
                "control_events": plane.events(),
            }
            record["result"] = result
            record["status"] = (
                terminal.status
                if turn_status in {"completed", "succeeded", "success"}
                else "failed"
            )
            self._runs[run_id] = PollResult(
                status=record["status"],
                progress={"events_seen": len(record["events"])},
                result=result,
            )
            self._store_metadata(run_id, record["root"], record["events"], returncode=0)
        except (
            TransportClosed,
            TimeoutError,
            EOFError,
            OSError,
            RuntimeError,
            ControlPlaneError,
            SessionRecoveryRequired,
            ABCError,
            RunnerError,
        ) as exc:
            record["ready"].set()
            pending = plane.status().get("pending_request")
            pending_id = ""
            if isinstance(pending, dict) and pending.get("status") == "pending":
                pending_id = str(pending.get("request_id") or "")
            try:
                plane.record_transport_failed(
                    str(exc) or "Codex App Server transport failed",
                    request_id=pending_id,
                    evidence={"phase": str(record.get("status") or "starting")},
                )
            except Exception:
                pass
            receipt = record.get("execution_session")
            result: dict[str, Any] = {
                "events": list(record["events"]),
                "execution_session": receipt,
                "failure": {
                    "kind": "codex_app_server_transport_failed",
                    "layer": "executor",
                    "message": str(exc),
                    "retryable": True,
                },
                "extensions": self.get_extensions(),
                "control_events": plane.events(),
            }
            record["result"] = result
            record["status"] = "needs_recovery"
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"events_seen": len(record["events"])},
                result=result,
            )
        finally:
            record["ready"].set()
            if transport is not None:
                self._transport_close(transport)
            self._close_run_lease(run_id)

    def _build_command(
        self,
        task_packet: dict[str, Any],
        prompt: str,
        root: Path,
        permission: dict[str, str] | None = None,
    ) -> tuple[list[str], str | None]:
        if self.agent_bin is None:
            raise RuntimeError("codex unavailable")
        selected = permission or permission_record_from_extensions(task_packet.get("extensions"))
        resumed, session_id = _codex_resume_context(task_packet)
        command = [str(self.agent_bin), "exec", "--json"]
        command.extend(permission_flags("codex", selected["effective_mode"]))
        command.append("--skip-git-repo-check")
        if selected["effective_mode"] == "safe":
            for writable_root in _codex_writable_roots(task_packet, root):
                command.extend(["--add-dir", str(writable_root)])
        if resumed:
            command.extend(["resume", session_id])
        images = task_image_paths(task_packet)
        prompt_input: str | None = None
        if images:
            command.append("--image")
            command.extend(str(image) for image in images)
            command.append("-")
            prompt_input = prompt
        else:
            command.append(prompt)
        return command, prompt_input

    def get_extensions(self) -> dict:
        """Return metadata suitable for storage at extensions.executor.codex."""
        metadata: dict[str, Any] = {
            "agent_bin": str(self.agent_bin) if self.agent_bin is not None else "",
            "agent_bin_source": self._discovery.get("source") or "not_found",
            "capability_level": self.capabilities().level,
            "last_run_id": self._last_run_id,
        }
        if self._last_run_id is not None:
            metadata["last_run"] = self._run_metadata[self._last_run_id]
        if self._app_server_capability is not None:
            metadata["app_server_capability"] = {
                "transport": str(self._app_server_capability.get("transport") or ""),
                "ok": bool(self._app_server_capability.get("ok")),
                "version": self._app_server_capability.get("version"),
                "version_parsed": self._app_server_capability.get("version_parsed"),
                "protocol_version": self._app_server_capability.get("protocol_version"),
                "evidence": list(self._app_server_capability.get("evidence") or []),
            }
        return {"executor": {"codex": metadata}}

    def _store_metadata(
        self,
        run_id: str,
        workspace: Path,
        events: list[dict[str, Any]],
        returncode: int | None,
    ) -> None:
        self._last_run_id = run_id
        self._run_metadata[run_id] = {
            "run_id": run_id,
            "workspace": str(workspace),
            "permission": permission_record_from_extensions(
                self._task_packets.get(run_id, {}).get("extensions")
            ),
            "writable_roots": [
                str(path) for path in _codex_writable_roots(self._task_packets.get(run_id, {}), workspace)
            ],
            "events_seen": len(events),
            "returncode": returncode,
        }
        app_run = self._app_runs.get(run_id)
        if app_run is not None:
            self._run_metadata[run_id]["transport"] = (
                str(self._app_server_capability.get("transport") or "app-server")
                if self._app_server_capability is not None
                else "app-server"
            )
            self._run_metadata[run_id]["session_id"] = str(
                app_run.get("session_id") or ""
            )
            self._run_metadata[run_id]["approval_events"] = sum(
                1
                for event in (app_run.get("events") or [])
                if event.get("event_type") == "approval_requested"
            )


def _discover_codex_binary(command: str | None) -> dict[str, Any]:
    configured = command.strip() if isinstance(command, str) else ""
    return find_binary("codex", extra_paths=[configured] if configured else None)


_FUZZY_SELECTOR_MARKERS = ("--last", "picker")
_GLOBAL_PURGE_MARKERS = ("prune", "purge", "delete old", "delete all")
_CODEX_DELETE_USAGE_RE = re.compile(
    r"^usage:\s+codex\s+delete\b",
    re.IGNORECASE | re.MULTILINE,
)


def _frozen_help_fixture_text(fixture_name: str) -> str:
    """Read a frozen CLI help fixture from the source checkout.

    The frozen fixture is the version-pinned evidence the cleanup capability
    probe is based on. Returns an empty string when the fixture cannot be
    resolved so callers fail closed; the fixture path itself is never exposed
    in any result.
    """
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "tests" / "fixtures" / "executor_runtime" / fixture_name
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return ""


def _codex_has_exact_session_delete_entry(help_text: str) -> bool:
    """Return True only for an official delete entry accepting an exact session ID.

    Codex may also accept names interactively, but ``--force`` explicitly
    requires SESSION to be a UUID. AgentBC validates the official UUID before
    invoking that noninteractive form, making the deletion exact.
    """
    if not help_text or _CODEX_DELETE_USAGE_RE.search(help_text) is None:
        return False
    lowered = help_text.lower()
    if any(marker in lowered for marker in _FUZZY_SELECTOR_MARKERS):
        return False
    if any(marker in lowered for marker in _GLOBAL_PURGE_MARKERS):
        return False
    force_uuid = (
        "session id (uuid) or session name" in lowered
        and "--force" in lowered
        and "session must be a uuid" in lowered
    )
    exact_positional = re.search(
        r"^\s+session_id\b.*session id to delete",
        help_text,
        re.IGNORECASE | re.MULTILINE,
    ) is not None
    return force_uuid or exact_positional


def _codex_session_cleanup_capability(help_text: str) -> SessionCleanupCapability:
    """Derive the Codex cleanup capability from frozen help fixture text."""
    if _codex_has_exact_session_delete_entry(help_text):
        return SessionCleanupCapability(
            capability="supported",
            strategy="official_session_delete",
            error_code="",
        )
    return SessionCleanupCapability(
        capability="unsupported",
        strategy="none",
        error_code=CODEX_CLEANUP_UNSUPPORTED_CODE,
    )


def _codex_cleanup_unsupported() -> SessionCleanupCapability:
    return SessionCleanupCapability(
        capability="unsupported",
        strategy="none",
        error_code=CODEX_CLEANUP_UNSUPPORTED_CODE,
    )


def _codex_cleanup_request_error(request: SessionCleanupRequest) -> str:
    if str(request.executor or "").strip().lower() != "codex":
        return "codex_cleanup_executor_mismatch"
    if request.retain is not False or request.project_mode != "none":
        return "codex_cleanup_mode_invalid"
    if request.strategy != "official_session_delete":
        return "codex_cleanup_strategy_mismatch"
    session_id = str(request.session_id or "").strip()
    try:
        parsed = uuid.UUID(session_id)
    except (AttributeError, ValueError):
        return CODEX_SESSION_DELETE_INVALID_ID_CODE
    if str(parsed) != session_id.lower():
        return CODEX_SESSION_DELETE_INVALID_ID_CODE
    return ""


def _codex_writable_roots(task_packet: dict[str, Any], workspace_root: Path) -> list[Path]:
    """Return only task deliverable and compact runtime-state write roots."""
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    task_board = task_packet.get("task_board") if isinstance(task_packet.get("task_board"), dict) else {}
    candidates: list[str | Path | None] = [
        workspace_root,
        workspace.get("project_root"),
        workspace.get("root"),
        workspace.get("artifact_root"),
        workspace.get("artifacts_dir"),
        task_board.get("root"),
    ]

    roots: list[Path] = []
    seen: set[str] = set()
    for value in candidates:
        if not value:
            continue
        path = Path(str(value)).expanduser().resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _build_prompt(
    task_packet: dict[str, Any],
    *,
    native_single_action: bool = False,
) -> str:
    """Build the Codex prompt: shared contract plus Codex platform notes."""
    extra_rules: tuple[str, ...] = ()
    if native_single_action:
        extra_rules = (
            "If an exact action explicitly declared by a task step is blocked by the native sandbox, "
            "retry that identical command exactly once with the same cwd through Codex's native "
            "sandbox_permissions=require_escalated single-action request. This does not change the "
            "task permission mode and is not a full fallback. Never use it for progress updates, "
            "diagnostics, an alternate command or path, persistent/session-wide access, or any "
            "undeclared action; if the native request cannot be emitted, stop and report the blocker.",
        )
    return build_prompt_contract(
        task_packet,
        PromptPlatformExtras(
            opening="You are executing a structured task.",
            image_note="Image inputs are attached through the native Codex CLI image interface:",
            image_inputs=tuple(str(image) for image in task_image_paths(task_packet)),
            image_rule=(
                "For image generation or image editing work, use the native image-generation "
                "capability and save the final bitmap deliverables under the Artifact root; do not "
                "return only prose or preview links."
            ),
            summary_line="After completing all steps, write a summary of what you did.",
            extra_rules=extra_rules,
        ),
    )


def _app_server_agent_message_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only completed App Server agent messages for terminal parsing.

    App Server also emits the user prompt as an item.  The prompt contains the
    example final marker, so feeding every item into the generic callback
    extractor creates a false duplicate.  ``item/completed`` is frozen in the
    capability contract and supplies the authoritative full agent text.
    """
    selected: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("event_type") or "") != "item/completed":
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").replace("_", "").lower()
        text = item.get("text")
        if item_type != "agentmessage" or not isinstance(text, str) or not text.strip():
            continue
        selected.append(
            {
                "event_type": "agent_message",
                "source": "codex_app_server",
                "sequence": len(selected) + 1,
                "payload": {
                    "type": "agent_message",
                    "text": text,
                },
            }
        )
    return selected


def _parse_jsonl(output: str | bytes) -> list[dict[str, Any]]:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for sequence, line in enumerate(output.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"type": "unparsed_output", "text": line}
        if not isinstance(payload, dict):
            payload = {"type": "codex_output", "value": payload}
        events.append(
            {
                "event_type": str(payload.get("type") or "codex_event"),
                "source": "codex",
                "sequence": sequence,
                "payload": payload,
            }
        )
    return events


def _codex_resume_context(task_packet: dict[str, Any]) -> tuple[bool, str]:
    """Return the explicit resume decision frozen into the task session snapshot."""
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict) or SESSION_EXTENSION_KEY not in extensions:
        return False, ""
    session = extensions.get(SESSION_EXTENSION_KEY)
    if not isinstance(session, dict):
        raise ABCError("invalid_executor_session", "agentbc.session must be an object")
    if str(session.get("executor") or "").strip().lower() != "codex":
        raise ABCError(
            "invalid_executor_session",
            "agentbc.session.executor must be codex",
        )
    run_ids = session.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in run_ids)
        or len(run_ids) != len(set(run_ids))
    ):
        raise ABCError(
            "invalid_executor_session",
            "agentbc.session.run_ids must contain unique non-empty strings",
        )
    if not run_ids:
        return False, ""
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ABCError(
            "missing_executor_session_id",
            "Codex resume requires an explicit task session ID",
        )
    return True, session_id.strip()


def _extract_codex_session_id(events: list[dict[str, Any]]) -> str:
    """Extract a session ID only from one well-formed ``thread.started`` event."""
    receipts: list[str] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "thread.started":
            continue
        thread_id = payload.get("thread_id")
        if (
            isinstance(thread_id, str)
            and thread_id
            and thread_id == thread_id.strip()
            and not any(character.isspace() for character in thread_id)
        ):
            receipts.append(thread_id)
        else:
            return ""
    return receipts[0] if len(receipts) == 1 else ""


def _execution_session_receipt(
    events: list[dict[str, Any]],
    *,
    resumed: bool,
) -> dict[str, Any] | None:
    session_id = _extract_codex_session_id(events)
    if not session_id:
        return None
    return {
        "version": 1,
        "executor": "codex",
        "session_id": session_id,
        "resumed": resumed,
        "persistence": "persistent",
        "source": "jsonl_thread_started",
    }


def _extract_summary(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        payload = event.get("payload") or {}
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return strip_callback_line(str(item.get("text") or ""))
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            return strip_callback_line(str(payload.get("text") or ""))
    return ""
