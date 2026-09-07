from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
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
from agent_bridge_connect.codex_session_cleanup import (
    CODEX_DESKTOP_UI_STALE_CODE,
    CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE,
    CODEX_SESSION_ARCHIVE_INVALID_ID_CODE,
    CODEX_SESSION_DELETE_FAILED_CODE,
    CODEX_SESSION_DELETE_INVALID_ID_CODE,
    CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
    CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
    CodexSessionCleanupClient,
    CodexSessionCleanupError,
)
from agent_bridge_connect.control import (
    ApprovalControlPlane,
    ControlPlaneError,
    StdioJsonRpcTransport,
    TransportClosed,
    approval_response_payload,
)
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.execution_contract import (
    detect_retryable_transport_failure,
    extract_callback_validation_from_events,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.media import task_image_paths
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.permission_elevation import (
    permission_elevation_from_extensions,
    task_elevation_protocol_enabled,
)
from agent_bridge_connect.prompt_contract import (
    PromptPlatformExtras,
    build_prompt_contract,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.runner import RunnerClient, RunnerError
from agent_bridge_connect.session import SessionRecoveryRequired

from ..path_provider import find_binary
from .base import CLIExecutorBase

SAFETY_TIMEOUT_S = 24 * 60 * 60
SESSION_EXTENSION_KEY = "agentbc.session"
CODEX_CLEANUP_UNSUPPORTED_CODE = "codex_session_delete_unavailable"
_CODEX_FROZEN_HELP_FIXTURE = "matrix/codex/0.146.0/delete_help.txt"
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
        desktop_verifier: Any | None = None,
    ) -> None:
        super().__init__()
        self.timeout_s = timeout_s
        self.transport_mode = transport
        self.transport_factory = transport_factory
        self.desktop_verifier = desktop_verifier
        self.approval_timeout_s = max(float(approval_timeout_s), 0.1)
        self._discovery = _discover_codex_binary(command)
        resolved = str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}
        self._app_runs: dict[str, dict[str, Any]] = {}
        self._app_server_capability: dict[str, Any] | None = None
        self._collaboration_spawn_capability: dict[str, Any] | None = None
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
        """Return the narrow official cleanup capability without session-store reads."""
        if request.retain is True:
            return SessionCleanupCapability("not_applicable", "retain")
        if self.agent_bin is None:
            return _codex_cleanup_unsupported()
        if self._uses_cleanup_app_server(request):
            # SESSION-104-001: the official App Server sequence is always
            # archive first (acknowledged) then delete.
            return SessionCleanupCapability(
                "supported",
                "official_session_archive_then_delete",
            )
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
        """Archive-then-delete one exact official UUID through the official chain."""
        if request.retain is True:
            return SessionCleanupResult("retained", "not_applicable", "retain")
        request_error = _codex_cleanup_request_error(request)
        if request_error:
            return SessionCleanupResult(
                "failed",
                "supported",
                _codex_cleanup_result_strategy(request),
                request_error,
                False,
            )
        if self._uses_cleanup_app_server(request):
            return self._cleanup_session_app_server(request)
        return self._cleanup_session_cli(request)

    def _uses_cleanup_app_server(
        self, request: SessionCleanupRequest | None = None
    ) -> bool:
        """Use App Server for auto/official cleanup and only explicit CLI otherwise."""
        if not isinstance(self.transport_mode, str):
            return True
        transport = self.transport_mode.strip().lower()
        if transport in {"cli", "direct"}:
            return False
        # ``auto`` and every non-CLI transport spelling are App Server
        # selections for cleanup, even when a factory is not injected. A
        # caller must opt into cli/direct to use the legacy subprocess action.
        return True

    def _cleanup_session_app_server(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupResult:
        verification = _unknown_cleanup_verification()
        commands = _unknown_cleanup_commands()
        try:
            assert self.agent_bin is not None
            root = _cleanup_workspace_root(request)
            cleanup_client = CodexSessionCleanupClient(
                self.agent_bin,
                cwd=root,
                transport_factory=self.transport_factory,
                transport=(
                    self.transport_mode
                    if not isinstance(self.transport_mode, str)
                    else None
                ),
                timeout_s=_CODEX_CLEANUP_TIMEOUT_S,
            )
            observation = cleanup_client.delete_and_verify(
                request.session_id,
                archive_acknowledged=request.archive_acknowledged,
                archive_checked_at=request.archive_checked_at,
            )
            verification = observation.verification()
            commands = observation.commands()
        except CodexSessionCleanupError as exc:
            live = self._desktop_live_cleanup_verification(request)
            verification = {
                "cli": {
                    "status": exc.cli_status
                    if exc.cli_status in {"unknown", "absent", "present"}
                    else "unknown",
                    "checked_at": exc.cli_checked_at or _cleanup_now(),
                },
                "desktop_backend": {
                    "status": "unavailable",
                    "checked_at": _cleanup_now(),
                },
                "desktop_live": live,
            }
            return SessionCleanupResult(
                "failed",
                "supported",
                OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
                exc.code,
                exc.retryable,
                verification=verification,
                commands=exc.commands or _unknown_cleanup_commands(),
            )
        except (OSError, TransportClosed, RuntimeError):
            live = self._desktop_live_cleanup_verification(request)
            verification = {
                **_unknown_cleanup_verification(),
                "desktop_live": live,
            }
            return SessionCleanupResult(
                "failed",
                "supported",
                OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
                CODEX_SESSION_DELETE_TRANSPORT_LOST_CODE,
                True,
                verification=verification,
                commands=_unknown_cleanup_commands(),
            )

        try:
            desktop_backend = cleanup_client.verify_desktop_absence(request.session_id)
        except (CodexSessionCleanupError, OSError, RuntimeError, TransportClosed):
            desktop_backend = {"status": "unavailable", "checked_at": _cleanup_now()}
        desktop_live = self._desktop_live_cleanup_verification(request)
        verification["desktop_backend"] = desktop_backend
        verification["desktop_live"] = desktop_live
        cli_status = verification["cli"]["status"]
        backend_status = desktop_backend["status"]
        live_status = desktop_live["status"]
        # SESSION-104-001: under the archive-then-delete gate the two
        # acknowledged commands are the success proof.  The fresh read/list
        # observations stay as non-gating diagnostics, and the current Codex
        # Desktop refresh delay is accepted: desktop_live becomes
        # not_applicable and backend/live states never block the result.
        if commands.get("archive", {}).get("status") in {
            "acknowledged",
            "confirmed",
        } and (
            commands.get("delete", {}).get("status") in {"acknowledged", "confirmed"}
        ):
            verification["desktop_live"] = {
                "status": "not_applicable",
                "checked_at": commands.get("delete", {}).get("checked_at")
                or _cleanup_now(),
            }
            return SessionCleanupResult(
                "succeeded" if cli_status == "absent" else "failed",
                "supported",
                OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
                ""
                if cli_status == "absent"
                else CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
                False,
                verification=verification,
                commands=commands,
            )
        if backend_status == "absent" and live_status == "present":
            return SessionCleanupResult(
                "failed",
                "supported",
                OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
                CODEX_DESKTOP_UI_STALE_CODE,
                False,
                verification=verification,
                commands=commands,
            )
        if cli_status == "present" or backend_status == "present":
            return SessionCleanupResult(
                "failed",
                "supported",
                OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
                CODEX_SESSION_DELETE_STILL_PRESENT_CODE,
                False,
                verification=verification,
                commands=commands,
            )
        if (
            cli_status == "absent"
            and backend_status in {"absent", "unavailable", "unverified"}
            and live_status in {"unknown", "unavailable", "unverified"}
        ):
            return SessionCleanupResult(
                "failed",
                "supported",
                OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
                CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE,
                False,
                verification=verification,
                commands=commands,
            )
        return SessionCleanupResult(
            "failed",
            "supported",
            OFFICIAL_SESSION_ARCHIVE_THEN_DELETE,
            CODEX_SESSION_DELETE_STILL_PRESENT_CODE
            if cli_status == "present"
            else CODEX_SESSION_DELETE_FAILED_CODE,
            False,
            verification=verification,
            commands=commands,
        )

    def _desktop_live_cleanup_verification(
        self,
        request: SessionCleanupRequest,
    ) -> dict[str, str]:
        """Read the explicitly supplied live Desktop verifier only.

        The App Server ``thread/list`` response is a backend observation and is
        deliberately kept separate.  No private database or GUI automation is
        an acceptable substitute for a supported live Desktop channel.
        """
        checked_at = _cleanup_now()
        verifier = self.desktop_verifier
        if verifier is None:
            return {"status": "unavailable", "checked_at": checked_at}
        try:
            if callable(getattr(verifier, "verify_desktop_absence", None)):
                value = verifier.verify_desktop_absence(request.session_id)
            elif callable(getattr(verifier, "verify_absent", None)):
                value = verifier.verify_absent(session_id=request.session_id)
            elif callable(getattr(verifier, "verify_session", None)):
                value = verifier.verify_session(session_id=request.session_id)
            elif callable(verifier):
                value = verifier(request.session_id)
            else:
                value = None
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            status = value.get("status")
            timestamp = value.get("checked_at")
            if status in {"absent", "present"}:
                return {
                    "status": str(status),
                    "checked_at": str(timestamp)
                    if _is_cleanup_timestamp(timestamp)
                    else checked_at,
                }
        elif isinstance(value, str) and value.strip().lower() in {"absent", "present"}:
            return {"status": value.strip().lower(), "checked_at": checked_at}
        return {"status": "unavailable", "checked_at": checked_at}

    # Backward-compatible private seam name used by older integrations.  It
    # now means the supported live Desktop check and never invokes the App
    # Server backend list verifier.
    def _desktop_cleanup_verification(
        self,
        request: SessionCleanupRequest,
        protocol_client: CodexSessionCleanupClient | None = None,
    ) -> dict[str, str]:
        del protocol_client
        return self._desktop_live_cleanup_verification(request)

    def _cleanup_session_cli(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupResult:
        """Keep the legacy CLI action as a bounded fallback.

        A CLI exit code is action evidence only.  It can never produce a
        cleanup success because it supplies neither the fresh backend list nor
        the live Desktop verification required by the v3 receipt.
        """
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
        except subprocess.TimeoutExpired:
            return SessionCleanupResult(
                "failed",
                "supported",
                "official_session_delete",
                CODEX_SESSION_DELETE_FAILED_CODE,
                True,
            )
        except OSError:
            return SessionCleanupResult(
                "failed",
                "supported",
                "official_session_delete",
                CODEX_SESSION_DELETE_FAILED_CODE,
                True,
            )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        cli_status = "absent" if _CODEX_SESSION_ABSENT_RE.search(output) else "unknown"
        code = (
            CODEX_DESKTOP_VERIFICATION_UNAVAILABLE_CODE
            if completed.returncode == 0 or cli_status == "absent"
            else CODEX_SESSION_DELETE_FAILED_CODE
        )
        if completed.returncode == 0:
            # Deliberately do not treat the subprocess success as cleanup
            # success; no raw output crosses the adapter boundary.
            cli_status = "unknown"
        return SessionCleanupResult(
            "failed",
            "supported",
            "official_session_delete",
            code,
            False,
            verification={
                "cli": {"status": cli_status, "checked_at": _cleanup_now()},
                "desktop_backend": {
                    "status": "unavailable",
                    "checked_at": _cleanup_now(),
                },
                "desktop_live": self._desktop_live_cleanup_verification(request),
            },
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
            return StartResult(
                ok=False, run_id="", message=f"workspace not found: {root}"
            )

        if self._uses_app_server_transport(task_packet):
            return self._start_app_server(task_packet, root)

        run_id = (
            str(task_packet.get("_agentbc_executor_run_id") or "").strip()
            if task_packet.get("runner_authorization_required") is True
            else ""
        ) or f"codex-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
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
            if permission["effective_mode"] != "full":
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
            return StartResult(
                ok=True, run_id=run_id, message="codex execution needs recovery"
            )
        except (OSError, RunnerError) as exc:
            self._close_run_lease(run_id)
            return StartResult(
                ok=False, run_id="", message=f"failed to start codex: {exc}"
            )

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
            runtime_failure=detect_retryable_transport_failure(
                completed.stdout, completed.stderr
            ),
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

    def _uses_app_server_transport(
        self, task_packet: dict[str, Any] | None = None
    ) -> bool:
        if not isinstance(self.transport_mode, str):
            # Injected fake transport objects in tests are always App Server.
            return True
        from agent_bridge_connect.codex_app_server import (
            CODEX_APP_SERVER_TRANSPORT_ALIASES,
        )

        transport = self.transport_mode.strip().lower()
        if transport in {"cli", "direct"}:
            return False
        extensions = (task_packet or {}).get("extensions")
        extensions = extensions if isinstance(extensions, dict) else {}
        elevation = permission_elevation_from_extensions(extensions)
        session = (
            extensions.get(SESSION_EXTENSION_KEY)
            if isinstance(extensions, dict)
            else None
        )
        official_receipt = (
            isinstance(session, dict)
            and session.get("official_receipt_bound") is True
            and bool(str(session.get("session_id") or "").strip())
        )
        if elevation is not None and elevation["state"]["status"] in {
            "approved",
            "active",
            "verified",
        }:
            # Plan D: once elevated, use Codex's native strongest CLI mode.
            # Historical permission grants are deliberately ignored.
            return False
        permission = permission_record_from_extensions(
            extensions,
            allow_legacy=True,
        )
        # Once an official receipt exists, auto/app-server must use the same
        # App Server transport for every continuation, including full mode.
        # The fresh full task exception above avoids creating a resumable
        # session through a path that has no official receipt yet.
        if permission["effective_mode"] == "full" and not official_receipt:
            return False
        return transport == "auto" or transport in CODEX_APP_SERVER_TRANSPORT_ALIASES

    def _freeze_app_server_capability(
        self, permission: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify the App Server contract before a receipt-bound run."""
        from agent_bridge_connect.codex_app_server import (
            CODEX_APP_SERVER_TRANSPORT,
            assert_codex_app_server_capability,
        )

        mode = str(permission.get("effective_mode") or "").strip().lower()
        if mode not in {"inherit", "safe", "full"}:
            raise ABCError(
                "permission_capability_unsupported",
                (
                    "Codex App Server single-action chain requires a supported "
                    "permission base; "
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
                        str(
                            override.get("reason")
                            or "App Server capability override failed"
                        ),
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

    def collaboration_spawn_capability(self) -> dict[str, Any]:
        """Return the two-proof collaboration gate without enabling dispatch."""
        if self._collaboration_spawn_capability is not None:
            return dict(self._collaboration_spawn_capability)
        if self.agent_bin is None:
            result = {
                "enabled": False,
                "version": "",
                "reason": "codex executable unavailable",
                "fixture": {"ok": False, "reason": "codex executable unavailable"},
                "live": {"ok": False, "reason": "codex executable unavailable"},
            }
            self._collaboration_spawn_capability = result
            return dict(result)
        from agent_bridge_connect.codex_app_server import (
            codex_collaboration_spawn_contract,
            codex_collaboration_spawn_fixture_contract,
        )

        live = codex_collaboration_spawn_contract(self.agent_bin)
        parsed = live.get("version_parsed")
        version = (
            ".".join(str(part) for part in parsed)
            if isinstance(parsed, (tuple, list)) and len(parsed) == 3
            else ""
        )
        fixture = (
            codex_collaboration_spawn_fixture_contract(version)
            if version
            else {
                "ok": False,
                "reason": "Codex version is unavailable for fixture matching",
            }
        )
        enabled = bool(live.get("ok") is True and fixture.get("ok") is True)
        if enabled:
            reason = ""
        elif not live.get("ok"):
            reason = str(live.get("reason") or "collaboration live probe failed")
        else:
            reason = str(fixture.get("reason") or "collaboration fixture failed")
        result = {
            "enabled": enabled,
            "version": version,
            "reason": reason,
            "fixture": fixture,
            "live": live,
        }
        self._collaboration_spawn_capability = result
        return dict(result)

    # Stable alias for callers that use the capability-group terminology.
    def collaboration_spawn_capability_group(self) -> dict[str, Any]:
        return self.collaboration_spawn_capability()

    @staticmethod
    def _collaboration_spawn_requested(task_packet: dict[str, Any]) -> bool:
        extensions = (
            task_packet.get("extensions")
            if isinstance(task_packet.get("extensions"), dict)
            else {}
        )
        frozen = extensions.get("agentbc.codex.collaboration_spawn")
        return bool(
            task_packet.get("collaboration_spawn") is True
            or task_packet.get("enable_collaboration_spawn") is True
            or (isinstance(frozen, dict) and frozen.get("enabled") is True)
        )

    def _archive_registered_auxiliary_sessions(self, record: dict[str, Any]) -> None:
        """Archive exact registered Codex children on the owning connection."""
        from agent_bridge_connect.auxiliary_sessions import (
            AUXILIARY_EXTENSION_KEY,
            read_auxiliary_ledger,
            validate_auxiliary_ledger,
        )
        from agent_bridge_connect.task_store import TaskStore

        packet = record["task_packet"]
        extensions = copy.deepcopy(packet.get("extensions") or {})
        ledger = read_auxiliary_ledger(extensions)
        owner_task_id = str(packet.get("task_id") or packet.get("id") or "").strip()
        owner_run_id = str(record.get("run_id") or "").strip()
        parent_session_id = str(record.get("session_id") or "").strip()
        candidates = [
            entry
            for entry in ledger["sessions"]
            if str(entry.get("owner_task_id") or "") == owner_task_id
            and str(entry.get("owner_run_id") or "") == owner_run_id
            and str(entry.get("parent_session_id") or "") == parent_session_id
            and str(entry.get("executor") or "").strip().lower() == "codex"
            and entry.get("retain") is False
            and str(entry.get("session_state") or "") == "terminal"
            and str(entry.get("session_id") or "").strip()
        ]
        candidates.sort(
            key=lambda entry: (
                str(entry.get("updated_at") or ""),
                str(entry.get("aux_id") or ""),
            ),
            reverse=True,
        )
        for candidate in candidates:
            session_id = str(candidate["session_id"]).strip()
            archive_id = self._app_rpc(
                record,
                "thread/archive",
                {"threadId": session_id},
            )
            self._app_wait_response(record, archive_id)
            checked_at = _cleanup_now()
            for entry in ledger["sessions"]:
                if entry.get("aux_id") == candidate.get("aux_id"):
                    entry["archive_acknowledged"] = True
                    entry["archive_checked_at"] = checked_at
                    entry["updated_at"] = checked_at
                    break
        if not candidates:
            return
        errors = validate_auxiliary_ledger(ledger)
        if errors:
            raise ABCError(
                "codex_auxiliary_receipt_missing",
                "; ".join(errors),
            )
        extensions[AUXILIARY_EXTENSION_KEY] = ledger
        packet["extensions"] = extensions
        board = packet.get("task_board")
        board_root = board.get("root") if isinstance(board, dict) else ""
        if not board_root:
            raise ABCError(
                "codex_auxiliary_receipt_missing",
                "Codex collaboration archive has no authoritative task board.",
            )
        store = TaskStore(board_root)
        persisted = store.read_task(owner_task_id)
        persisted["extensions"] = extensions
        store.write_task(owner_task_id, persisted)

    def _build_app_server_command(self) -> list[str]:
        if self.agent_bin is None:
            raise RuntimeError("codex unavailable")
        return [str(self.agent_bin), "app-server", "--stdio"]

    def _start_app_server(self, task_packet: dict[str, Any], root: Path) -> StartResult:
        run_id = (
            str(task_packet.get("_agentbc_executor_run_id") or "").strip()
            if task_packet.get("runner_authorization_required") is True
            else ""
        ) or f"codex-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
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
            if permission["effective_mode"] != "full":
                assert_executor_permission_supported(
                    "codex", permission["effective_mode"], self.agent_bin
                )
            # Capability gate is transport- and receipt-based. Inherit keeps
            # native permission settings, safe supplies the conservative
            # workspace policy, and full keeps the CLI fallback.
            if permission["effective_mode"] != "full":
                self._freeze_app_server_capability(permission)
            collaboration_capability = {
                "enabled": False,
                "reason": "collaboration_spawn_not_requested",
            }
            if self._collaboration_spawn_requested(task_packet):
                collaboration_capability = self.collaboration_spawn_capability()
                if collaboration_capability.get("enabled") is not True:
                    raise ABCError(
                        "codex_collaboration_spawn_unsupported",
                        str(
                            collaboration_capability.get("reason")
                            or "Codex collaboration_spawn capability is not verified"
                        ),
                    )
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
        except (
            ABCError,
            RunnerError,
            ControlPlaneError,
            SessionRecoveryRequired,
        ) as exc:
            self._close_run_lease(run_id)
            code = f"{exc.code}: " if isinstance(exc, ABCError) else ""
            return StartResult(
                ok=False,
                run_id="",
                message=f"codex App Server unavailable: {code}{exc}",
            )

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
            "collaboration_spawn": collaboration_capability,
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
        return StartResult(
            ok=True, run_id=run_id, message="codex App Server run started"
        )

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
        if not isinstance(self.transport_mode, str) and hasattr(
            self.transport_mode, "send"
        ):
            return self.transport_mode
        factory = self.transport_factory
        if factory is not None:
            attempts = (
                lambda: factory(
                    run_id=run_id, task_packet=task_packet, cwd=root, command=command
                ),
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
        receiver = getattr(transport, "recv", None) or getattr(
            transport, "receive", None
        )
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
            except Exception:  # noqa: BLE001
                return False
        value = getattr(transport, "alive", None)
        return bool(value) if isinstance(value, bool) else True

    def _app_rpc(
        self, record: dict[str, Any], method: str, params: dict[str, Any] | None = None
    ) -> int:
        request_id = int(record["next_rpc_id"])
        record["next_rpc_id"] = request_id + 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._transport_send(record["transport"], message)
        return request_id

    def _app_notification(
        self, record: dict[str, Any], method: str, params: dict[str, Any] | None = None
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._transport_send(record["transport"], message)

    def _app_event(self, record: dict[str, Any], message: dict[str, Any]) -> None:
        method = str(message.get("method") or "rpc_response")
        payload = (
            message.get("params")
            if isinstance(message.get("params"), dict)
            else message.get("result")
        )
        record["events"].append(
            {
                "event_type": method,
                "source": "codex_app_server",
                "sequence": len(record["events"]) + 1,
                "payload": payload if isinstance(payload, dict) else message,
            }
        )
        if method in {"item/started", "item/completed"}:
            self._handle_collaboration_event(record, method, payload)

    def _handle_collaboration_event(
        self,
        record: dict[str, Any],
        method: str,
        payload: Any,
    ) -> None:
        """Persist verified Codex collaboration lifecycle events only."""
        capability = record.get("collaboration_spawn")
        if not isinstance(capability, dict) or capability.get("enabled") is not True:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("item"), dict):
            return
        item = payload["item"]
        if str(item.get("type") or "") != "collabAgentToolCall":
            return
        # Collaboration-mode turns also publish lifecycle items for wait,
        # listAgents, and the other coordination tools.  Only spawnAgent
        # creates a new session receipt; those sibling calls are not malformed
        # spawn events and must not abort the parent transport.
        if str(item.get("tool") or "") != "spawnAgent":
            return
        from agent_bridge_connect.auxiliary_sessions import (
            handle_codex_collaboration_item_completed,
            handle_codex_collaboration_item_started,
        )
        from agent_bridge_connect.task_store import TaskStore

        task_packet = record["task_packet"]
        task_id = str(task_packet.get("task_id") or task_packet.get("id") or "").strip()
        run_id = str(record.get("run_id") or "").strip()
        parent_session_id = str(record.get("session_id") or "").strip()
        if not task_id or not run_id or not parent_session_id:
            raise ABCError(
                "codex_auxiliary_receipt_missing",
                "Codex collaboration event has no bound parent task/session.",
            )
        extensions = dict(task_packet.get("extensions") or {})
        primary = dict(extensions.get(SESSION_EXTENSION_KEY) or {})
        primary.update(
            {
                "session_id": parent_session_id,
                "session_state": "active",
                "receipt_source": "jsonl_thread_started",
                "official_receipt_bound": True,
            }
        )
        extensions[SESSION_EXTENSION_KEY] = primary
        parent_turn_id = str(
            payload.get("turnId") or payload.get("turn_id") or ""
        ).strip()
        if method == "item/started":
            updated, entry = handle_codex_collaboration_item_started(
                extensions,
                owner_task_id=task_id,
                owner_run_id=run_id,
                parent_session_id=parent_session_id,
                parent_turn_id=parent_turn_id,
                item=item,
                occurred_at=_cleanup_now(),
            )
        else:
            updated, entry = handle_codex_collaboration_item_completed(
                extensions,
                owner_task_id=task_id,
                owner_run_id=run_id,
                parent_session_id=parent_session_id,
                item=item,
                occurred_at=_cleanup_now(),
            )
        task_packet["extensions"] = updated
        board = task_packet.get("task_board")
        board_root = board.get("root") if isinstance(board, dict) else ""
        if not board_root:
            raise ABCError(
                "codex_auxiliary_receipt_missing",
                "Codex collaboration event has no authoritative task board.",
            )
        store = TaskStore(board_root)
        persisted = store.read_task(task_id)
        persisted["extensions"] = updated
        store.write_task(task_id, persisted)
        store.append_event(
            task_id,
            {
                "event_type": "codex.collaboration_auxiliary_updated",
                "task_id": task_id,
                "run_id": run_id,
                "aux_id": str(entry.get("aux_id") or ""),
                "lifecycle": method,
                "session_state": str(entry.get("session_state") or ""),
                "created_at": _cleanup_now(),
            },
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
        extensions = (
            task_packet.get("extensions")
            if isinstance(task_packet.get("extensions"), dict)
            else {}
        )
        for key in (
            "agentbc.permission.v2",
            "agentbc.permissions.v2",
            "agentbc.permission_mapping",
        ):
            mapping = extensions.get(key)
            if isinstance(mapping, dict):
                codex_mapping = (
                    mapping.get("codex")
                    if isinstance(mapping.get("codex"), dict)
                    else mapping
                )
                if isinstance(codex_mapping, dict):
                    for field in ("sandbox", "approvalPolicy", "approvalsReviewer"):
                        value = codex_mapping.get(field)
                        if isinstance(value, str) and value.strip():
                            params[field] = value.strip()
        return params

    @staticmethod
    def _thread_id_from_message(message: dict[str, Any]) -> str:
        result = (
            message.get("result") if isinstance(message.get("result"), dict) else {}
        )
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        candidates = [thread.get("id"), result.get("threadId")]
        values = [
            str(value).strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        ]
        return (
            values[0]
            if len(set(values)) == 1
            else (values[0] if len(values) == 1 else "")
        )

    def _app_wait_response(
        self, record: dict[str, Any], request_id: int
    ) -> dict[str, Any]:
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

    def _handle_app_approval(
        self, record: dict[str, Any], message: dict[str, Any]
    ) -> None:
        plane: ApprovalControlPlane = record["plane"]
        # PERM-104-002 v2: enrich the native App Server request with the
        # exact schema-supported choice set before it reaches the control
        # plane.  The captured fixtures prove session-scope decisions are
        # schema-supported (acceptForSession on command/file_change;
        # turn/session permissions responses); amendments stay non-selectable
        # and never offered.
        from agent_bridge_connect.control import codex_offered_choices

        method = str(message.get("method") or "")
        operation = {
            "item/commandExecution/requestApproval": "command",
            "item/fileChange/requestApproval": "file_change",
            "item/permissions/requestApproval": "permissions",
        }.get(method, "")
        if operation:
            task_elevation = task_elevation_protocol_enabled(
                (record.get("task_packet") or {}).get("extensions")
                if isinstance(record.get("task_packet"), dict)
                else {}
            )
            authority = {
                "executor": "codex",
                "protocol": "codex_app_server",
                "protocol_version": 2,
                "method": method,
            }
            if task_elevation:
                identity = message.get("_agentbc") if isinstance(message.get("_agentbc"), dict) else {}
                message = {
                    **message,
                    "approval_version": 3,
                    "scope": "task_elevation",
                    "elevation_mode": "full",
                    "native_event": f"codex_app_server.{method}",
                    "path_plan_digest": "",
                    "containment_profile_digest": "",
                    "preflight": {"status": "retired", "mode": "full"},
                    "authority": authority,
                    "_agentbc": {
                        **identity,
                        "native_event": f"codex_app_server.{method}",
                        "path_plan_digest": "",
                        "containment_profile_digest": "",
                        "preflight": {"status": "retired", "mode": "full"},
                    },
                }
            else:
                message = {
                    **message,
                    "approval_version": 2,
                    "authority": authority,
                    "offered_choices": [
                        dict(choice)
                        for choice in codex_offered_choices(
                            operation,
                            session_decisions_supported=True,
                        )
                    ],
                }
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
        approval: dict[str, Any] = {
            "type": "permission",
            "request_id": request_id,
            "request_fingerprint": str(event.get("request_fingerprint") or ""),
            "kind": str(event.get("operation") or "permission"),
            "operation": str(event.get("operation") or "permission"),
            "summary": str(event.get("summary") or ""),
            "scope": str(event.get("scope") or "single_action"),
            "session_id": str(event.get("session_id") or ""),
        }
        pending_after = plane.status().get("pending_request")
        if (
            isinstance(pending_after, dict)
            and int(pending_after.get("approval_version") or 1) == 3
        ):
            approval.update(
                {
                    "approval_version": 3,
                    "elevation_mode": str(
                        pending_after.get("elevation_mode") or "full"
                    ),
                    "path_plan_digest": str(pending_after.get("path_plan_digest") or ""),
                    "containment_profile_digest": str(
                        pending_after.get("containment_profile_digest") or ""
                    ),
                    "preflight": dict(pending_after.get("preflight") or {}),
                    "native_event": str(pending_after.get("native_event") or ""),
                    "authority": dict(pending_after.get("authority") or {}),
                }
            )
        # PERM-104-002 v2: the offered native choices ride on the poll result
        # so the CLI worker can persist them on the input request.  The
        # choices come from the CONTROL PLANE's normalized pending request,
        # because that is where the opaque handles are computed and bound.
        if (
            isinstance(pending_after, dict)
            and str(pending_after.get("approval_version") or "") == "2"
            and isinstance(pending_after.get("offered_choices"), list)
        ):
            approval["approval_version"] = 2
            approval["authority"] = dict(pending_after.get("authority") or {})
            approval["offered_choices"] = [
                dict(choice)
                for choice in pending_after.get("offered_choices") or []
                if isinstance(choice, dict)
            ]
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
                except Exception:  # noqa: BLE001,S110
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
                    raise ControlPlaneError(
                        "approval_response_missing",
                        "Approval response payload is unavailable.",
                    )
                response_payload = approval_response_payload(
                    pending, response.get("decision")
                )
            # PERM-104-002 v2: the exact selected native choice decides the
            # response shape (accept / acceptForSession / decline on the
            # original id; permissions turn/session responses).  Amendments
            # are never selectable and never returned.
            self._resume_run(record["run_id"])
            record["status"] = "running"
            record.setdefault("approval_history", []).append(
                {
                    "request_id": request_id,
                    "decision": str(response.get("decision") or ""),
                }
            )
            rpc_response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": response_payload,
            }
            self._transport_send(record["transport"], rpc_response)
        except (ControlPlaneError, SessionRecoveryRequired):  # noqa: TRY203
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
            transport = self._make_app_server_transport(
                run_id, record["task_packet"], record["root"], command
            )
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
            collaboration_enabled = bool(
                isinstance(record.get("collaboration_spawn"), dict)
                and record["collaboration_spawn"].get("enabled") is True
            )
            if collaboration_enabled:
                # Codex 0.150.1 marks multiAgentMode as deprecated/ignored and
                # names Ultra effort as the supported activation path. Keep
                # both the explicit-request policy and activation task-scoped;
                # ordinary AgentBC tasks retain their configured effort.
                thread_params["multiAgentMode"] = "explicitRequestOnly"
                thread_params["effort"] = "ultra"
            thread_rpc_id = self._app_rpc(record, thread_method, thread_params)
            thread_response = self._app_wait_response(record, thread_rpc_id)
            official_thread_id = self._thread_id_from_message(thread_response)
            if not official_thread_id:
                raise SessionRecoveryRequired(
                    "session_receipt_missing",
                    "Codex App Server thread response did not contain an official thread ID.",
                )
            if (
                record["resumed"]
                and official_thread_id != record["explicit_session_id"]
            ):
                raise SessionRecoveryRequired(
                    "session_receipt_run_mismatch",
                    "Codex App Server resume returned a different official thread ID.",
                    {
                        "expected_session_id": record["explicit_session_id"],
                        "actual_session_id": official_thread_id,
                    },
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
            turn_params: dict[str, Any] = {
                "threadId": official_thread_id,
                "input": inputs,
            }
            if collaboration_enabled:
                turn_params["multiAgentMode"] = "explicitRequestOnly"
                turn_params["effort"] = "ultra"
            turn_id = self._app_rpc(record, "turn/start", turn_params)
            turn_response = self._app_wait_response(record, turn_id)
            turn_result = (
                turn_response.get("result")
                if isinstance(turn_response.get("result"), dict)
                else {}
            )
            turn = (
                turn_result.get("turn")
                if isinstance(turn_result.get("turn"), dict)
                else {}
            )
            record["turn_id"] = str(turn.get("id") or "")
            completed_message = self._app_wait_turn_completed(record)
            completed_params = (
                completed_message.get("params")
                if isinstance(completed_message.get("params"), dict)
                else {}
            )
            completed_turn = (
                completed_params.get("turn")
                if isinstance(completed_params.get("turn"), dict)
                else {}
            )
            turn_status = str(completed_turn.get("status") or "completed")
            plane.record_turn_completed(
                turn_id=str(completed_turn.get("id") or record.get("turn_id") or ""),
                status=turn_status,
            )
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
                native_approval_authoritative=True,
            )
            # Archive while this exact App Server connection still owns the
            # thread writer. A separate cleanup process is rejected by Codex
            # with "already has an active writer". Publishing terminal state
            # before this acknowledgement creates that race. The bounded
            # receipt lets cleanup skip the non-idempotent archive call and
            # retain the existing delete implementation.
            session_policy = (
                record["task_packet"].get("extensions", {}).get(SESSION_EXTENSION_KEY)
                if isinstance(record["task_packet"].get("extensions"), dict)
                else None
            )
            if (
                isinstance(session_policy, dict)
                and session_policy.get("retain") is False
            ):
                self._archive_registered_auxiliary_sessions(record)
                archive_id = self._app_rpc(
                    record,
                    "thread/archive",
                    {"threadId": official_thread_id},
                )
                self._app_wait_response(record, archive_id)
                receipt["archive_acknowledged"] = True
                receipt["archive_checked_at"] = _cleanup_now()
                self._transport_close(transport)
                transport = None
                record["transport"] = None
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
            except Exception:  # noqa: BLE001,S110
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
        selected = permission or permission_record_from_extensions(
            task_packet.get("extensions")
        )
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
        if self._collaboration_spawn_capability is not None:
            collaboration = self._collaboration_spawn_capability
            fixture = (
                collaboration.get("fixture")
                if isinstance(collaboration.get("fixture"), dict)
                else {}
            )
            live = (
                collaboration.get("live")
                if isinstance(collaboration.get("live"), dict)
                else {}
            )
            metadata["collaboration_spawn"] = {
                "enabled": bool(collaboration.get("enabled")),
                "version": str(collaboration.get("version") or ""),
                "reason": str(collaboration.get("reason") or ""),
                "fixture_ok": bool(fixture.get("ok")),
                "live_ok": bool(live.get("ok")),
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
                str(path)
                for path in _codex_writable_roots(
                    self._task_packets.get(run_id, {}), workspace
                )
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


def _cleanup_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_cleanup_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _unknown_cleanup_verification() -> dict[str, dict[str, str]]:
    return {
        "cli": {"status": "unknown", "checked_at": _cleanup_now()},
        "desktop_backend": {"status": "unavailable", "checked_at": _cleanup_now()},
        "desktop_live": {"status": "unavailable", "checked_at": _cleanup_now()},
    }


# SESSION-104-001 strategy name.  The App Server path is the only producer;
# the explicit cli/direct fallback keeps ``official_session_delete`` so the
# fallback can never claim the archive gate it does not perform.
OFFICIAL_SESSION_ARCHIVE_THEN_DELETE = "official_session_archive_then_delete"
_CODEX_CLEANUP_REQUEST_STRATEGIES = frozenset(
    {"official_session_delete", OFFICIAL_SESSION_ARCHIVE_THEN_DELETE}
)


def _unknown_cleanup_commands() -> dict[str, dict[str, str]]:
    """Bounded v4 command evidence when nothing can be proven (fail closed)."""
    return {
        "archive": {"status": "unverified", "checked_at": _cleanup_now()},
        "delete": {"status": "unverified", "checked_at": _cleanup_now()},
    }


def _cleanup_workspace_root(request: SessionCleanupRequest) -> Path:
    workspace = request.workspace if isinstance(request.workspace, dict) else {}
    candidate = str(workspace.get("root") or workspace.get("project_root") or ".")
    root = Path(candidate).expanduser().resolve()
    return root if root.is_dir() else Path.cwd()


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
    candidate = (
        here.parents[3] / "tests" / "fixtures" / "executor_runtime" / fixture_name
    )
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
    exact_positional = (
        re.search(
            r"^\s+session_id\b.*session id to delete",
            help_text,
            re.IGNORECASE | re.MULTILINE,
        )
        is not None
    )
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
    if request.strategy not in _CODEX_CLEANUP_REQUEST_STRATEGIES:
        return "codex_cleanup_strategy_mismatch"
    if (
        request.official_receipt_bound is not True
        or request.receipt_source != "jsonl_thread_started"
    ):
        return "codex_cleanup_receipt_unbound"
    session_id = str(request.session_id or "").strip()
    try:
        parsed = uuid.UUID(session_id)
    except (AttributeError, ValueError):
        return _codex_invalid_id_code(request.strategy)
    if str(parsed) != session_id.lower():
        return _codex_invalid_id_code(request.strategy)
    return ""


def _codex_invalid_id_code(strategy: str) -> str:
    """Return the strategy-scoped invalid-id code without widening the gate."""
    if strategy == "official_session_archive_then_delete":
        return CODEX_SESSION_ARCHIVE_INVALID_ID_CODE
    return CODEX_SESSION_DELETE_INVALID_ID_CODE


def _codex_cleanup_result_strategy(request: SessionCleanupRequest) -> str:
    """Mirror the caller's strategy so a legacy request keeps its own name.

    The new archive-then-delete strategy is only ever claimed by the App
    Server path that actually performs the archive gate; the explicit
    cli/direct fallback keeps reporting ``official_session_delete``.
    """
    if request.strategy == "official_session_archive_then_delete":
        return "official_session_archive_then_delete"
    return "official_session_delete"


def _codex_writable_roots(
    task_packet: dict[str, Any], workspace_root: Path
) -> list[Path]:
    """Return only task deliverable and compact runtime-state write roots."""
    workspace = (
        task_packet.get("workspace")
        if isinstance(task_packet.get("workspace"), dict)
        else {}
    )
    task_board = (
        task_packet.get("task_board")
        if isinstance(task_packet.get("task_board"), dict)
        else {}
    )
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
    native_permission_rule: str | None = None
    if native_single_action:
        extra_rules = (
            (
                "If an exact action explicitly declared by a task step is blocked by the native sandbox, "
                "retry that identical command exactly once with the same cwd through Codex's native "
                "sandbox_permissions=require_escalated single-action request. This does not change the "
                "task permission mode and is not a full fallback. Never use it for progress updates, "
                "diagnostics, an alternate command or path, persistent/session-wide access, or any "
                "undeclared action; if the native request cannot be emitted, stop and report the blocker."
            ),
        )
        native_permission_rule = (
            "For Codex App Server native permission events, never request full, mint a grant, or "
            "emit a permission input from the model; only the structured requestApproval event can "
            "block one exact single action. Native transport or containment failure requires "
            "needs_recovery and must never become a full request."
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
            native_permission_rule=native_permission_rule,
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
