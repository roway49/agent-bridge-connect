from __future__ import annotations

import errno
import json
import math
import os
import re
import shlex
import subprocess
import sys
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
from agent_bridge_connect.approval import (
    assert_no_pending_approval,
    compute_request_fingerprint,
    core_bounded_summary,
    new_request_id,
    validate_approval_receipt,
)
from agent_bridge_connect.execution_contract import (
    CallbackValidation,
    build_resource_exhaustion,
    detect_retryable_transport_failure,
    extract_callback_validation_from_output,
    resource_snapshot_limit,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.effective_permissions import (
    SESSION_EXTENSION_KEY,
    resolve_effective_permission,
)
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.path_model import (
    validate_managed_cleanup_paths,
    validate_path_plan_workspace,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import PromptPlatformExtras, build_prompt_contract
from agent_bridge_connect.runner import RunnerClient, RunnerError

from .base import CLIExecutorBase
from ..path_provider import find_binary

SAFETY_TIMEOUT_S = 24 * 60 * 60
CLAUDE_PROJECT_PURGE_TIMEOUT_S = 30

PERMISSION_PROMPT_TOOL_FLAG = "--permission-prompt-tool"

_CLAUDE_BUDGET_ERROR_RE = re.compile(
    r"(?m)^Error:\s+Exceeded\s+USD\s+budget(?:\s*\(|\s*$)"
)
_CLAUDE_BUDGET_AMOUNT_RE = re.compile(
    r"Exceeded\s+USD\s+budget\s*\(\s*\$?\s*(?P<amount>\d+(?:\.\d+)?)"
)
_CLAUDE_PROJECT_ABSENT_RE = re.compile(
    r"(?im)^(?:no claude code (?:project )?state found(?: for (?:project )?)?"
    r"|no project (?:state )?found(?: for (?:path )?)?"
    r"|project (?:state )?not found)"
    r"(?:[.: ].*)?$"
)
_STREAM_INIT_SUBTYPE = "init"


class ClaudeExecutor(CLIExecutorBase):
    """L1 Claude Code adapter using headless print mode and AgentBC callbacks."""

    COMMON_PATHS = (
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    )
    BANNED_PERMISSION_MODES = {"bypassPermissions"}

    def __init__(
        self,
        timeout_s: int = SAFETY_TIMEOUT_S,
        model: str | None = None,
        effort: str | None = "high",
        permission_mode: str = "acceptEdits",
        safe_mode: bool = True,
        output_format: str = "text",
        max_budget_usd: float | None = 10.0,
        allowed_tools: list[str] | tuple[str, ...] | str | None = None,
        command: str | None = None,
        transport: str = "runner",
        runner_spool: str | None = None,
        runner_token: str | None = None,
    ) -> None:
        super().__init__()
        if permission_mode in self.BANNED_PERMISSION_MODES:
            raise ValueError("Claude bypassPermissions is disabled for AgentBC L1")
        if output_format not in {"text", "json", "stream-json"}:
            raise ValueError(f"Unsupported Claude output_format: {output_format}")
        if transport not in {"runner", "direct", "auto"}:
            raise ValueError(f"Unsupported Claude transport: {transport}")
        self.timeout_s = timeout_s
        self.model = _optional_text(model)
        self.effort = _optional_text(effort)
        self.permission_mode = permission_mode
        self.safe_mode = bool(safe_mode)
        self.output_format = output_format
        self.max_budget_usd = max_budget_usd
        self.allowed_tools = _normalize_allowed_tools(allowed_tools)
        self.transport = transport
        self.runner_spool = runner_spool
        self.runner_token = runner_token
        configured_command = _optional_text(command)
        self._discovery = _discover_claude_binary(configured_command)
        resolved = configured_command or str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._version = ""
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}

    def probe(self) -> ProbeResult:
        if self.agent_bin is None:
            return ProbeResult(
                ok=False,
                message="claude unavailable",
                details={
                    "agent_bin": "",
                    "agent_bin_source": self._discovery.get("source") or "not_found",
                    "candidates": [str(path) for path in self.COMMON_PATHS],
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
                message=f"claude unavailable: {exc}",
                details={
                    "agent_bin": str(self.agent_bin),
                    "agent_bin_source": self._discovery.get("source") or "unknown",
                },
            )
        version = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0:
            self._version = version
        return ProbeResult(
            ok=completed.returncode == 0,
            message=version or f"claude exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
                "agent_bin_source": self._discovery.get("source") or "unknown",
                "returncode": completed.returncode,
                "version": version,
                "transport": self.transport,
                "configured_safe_mode": self.safe_mode,
                "configured_permission_mode": self.permission_mode,
                "output_format": self.output_format,
                "task_permission": None,
                "dangerous_permissions_supported": None,
                "dangerous_permissions_allowed": None,
                "dangerous_permissions_policy": "explicit_persisted_full_task_only",
                "capability_level": "L1",
            },
        )

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            structured_output=True,
            streaming_events=self.output_format == "stream-json",
            resume=True,
            cancel=False,
            input_required=False,
            model_selection=True,
            multimodal=True,
            parallelism=1,
            level=ExecutorLevel.L1,
        )

    def session_cleanup_capability(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupCapability:
        if request.retain is True or request.project_mode == "native":
            return SessionCleanupCapability(
                capability="not_applicable",
                strategy="retain",
            )
        request_error = _claude_cleanup_request_error(request)
        if request_error:
            return SessionCleanupCapability(
                capability="unsupported",
                strategy="none",
                error_code=request_error,
            )
        if self.agent_bin is None:
            return SessionCleanupCapability(
                capability="unsupported",
                strategy="none",
                error_code="claude_project_purge_unavailable",
            )
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "project", "purge", "--help"],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=CLAUDE_PROJECT_PURGE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return SessionCleanupCapability(
                capability="unsupported",
                strategy="none",
                error_code="claude_project_purge_help_timeout",
            )
        except OSError:
            return SessionCleanupCapability(
                capability="unsupported",
                strategy="none",
                error_code="claude_project_purge_unavailable",
            )
        help_text = "\n".join((completed.stdout or "", completed.stderr or ""))
        if completed.returncode != 0 or not _supports_claude_project_purge(help_text):
            return SessionCleanupCapability(
                capability="unsupported",
                strategy="none",
                error_code="claude_project_purge_unsupported",
            )
        return SessionCleanupCapability(
            capability="supported",
            strategy="claude_project_purge",
        )

    def cleanup_session(self, request: SessionCleanupRequest) -> SessionCleanupResult:
        if request.retain is True or request.project_mode == "native":
            return SessionCleanupResult(
                state="retained",
                capability="not_applicable",
                strategy="retain",
            )

        request_error = _claude_cleanup_request_error(request)
        if request_error:
            return _claude_cleanup_failed(request_error)
        path_error = _validate_claude_cleanup_paths(request)
        if path_error:
            return _claude_cleanup_failed(path_error)

        capability = self.session_cleanup_capability(request)
        if capability.capability != "supported":
            return SessionCleanupResult(
                state="unsupported",
                capability="unsupported",
                strategy="none",
                error_code=capability.error_code or "claude_project_purge_unsupported",
                retryable=False,
            )

        if self.agent_bin is None:  # Guard against mutation after the capability probe.
            return _claude_cleanup_failed("claude_project_purge_unavailable")
        purge_command = [
            str(self.agent_bin),
            "project",
            "purge",
            "--yes",
            request.project_path,
        ]
        try:
            completed = subprocess.run(
                purge_command,
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=CLAUDE_PROJECT_PURGE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return _claude_cleanup_failed(
                "claude_project_purge_timeout",
                retryable=True,
            )
        except OSError:
            return _claude_cleanup_failed(
                "claude_project_purge_unavailable",
                retryable=True,
            )

        purge_output = "\n".join((completed.stdout or "", completed.stderr or ""))
        if completed.returncode != 0 and not _claude_project_is_absent(purge_output):
            return _claude_cleanup_failed("claude_project_purge_failed")

        for field in ("executor_project_root", "task_root", "chain_root"):
            request_error = _claude_cleanup_request_error(request)
            if request_error:
                return _claude_cleanup_failed(request_error)
            try:
                paths = validate_managed_cleanup_paths(
                    request.workspace,
                    task_id=request.task_id,
                    project_path=request.project_path,
                )
            except ABCError as exc:
                return _claude_cleanup_failed(exc.code)
            except (OSError, TypeError, ValueError):
                return _claude_cleanup_failed("cleanup_path_invalid")
            try:
                os.rmdir(getattr(paths, field))
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    # The chain root is also the managed Artifact root when the
                    # user did not provide a customer path. Normal deliverables
                    # (and sibling task iterations) must survive session cleanup.
                    if field == "chain_root":
                        continue
                    return _claude_cleanup_failed(
                        "claude_cleanup_directory_not_empty"
                    )
                return _claude_cleanup_failed("claude_cleanup_rmdir_failed")

        return SessionCleanupResult(
            state="succeeded",
            capability="supported",
            strategy="claude_project_purge",
        )

    def start(self, task_packet: dict) -> StartResult:
        steps = task_packet.get("steps") or []
        if not steps:
            return StartResult(ok=False, run_id="", message="no steps")
        if self.agent_bin is None:
            return StartResult(ok=False, run_id="", message="claude unavailable")

        root = _workspace_root(task_packet)
        if root is None or not root.is_dir():
            return StartResult(ok=False, run_id="", message=f"workspace not found: {root}")
        try:
            execution_root = _claude_execution_root(task_packet, root)
            execution_session = _claude_execution_session(task_packet)
        except (OSError, ValueError) as exc:
            return StartResult(ok=False, run_id="", message=f"invalid claude session: {exc}")

        run_id = f"claude-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "claude")
        prompt = _build_prompt(task_packet)
        try:
            permission = resolve_effective_permission(
                task_packet,
                "claude",
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
                "claude", permission["effective_mode"], self.agent_bin
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=str(exc))
        try:
            command = self._build_command(prompt, root, task_packet, permission)
        except ValueError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"invalid claude policy: {exc}")

        try:
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command(
                    "claude",
                    command,
                    execution_root,
                    task_packet,
                    executor_run_id=run_id,
                )
            self._heartbeat_run(run_id)
            completed = subprocess.run(
                command,
                input=prompt,
                cwd=execution_root,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_output(exc.stdout or "")
            stderr = _coerce_output(exc.stderr or "")
            self._store_run(run_id, execution_root, None)
            self._mark_run_stale(run_id)
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"steps_total": len(steps)},
                result={
                    "stdout": stdout,
                    "stderr": stderr,
                    "reason": f"claude safety runtime exceeded after {self.timeout_s}s",
                    "timeout_is_failure": False,
                    "failure": {
                        "kind": "executor_timeout",
                        "layer": "executor",
                        "message": f"claude safety runtime exceeded after {self.timeout_s}s",
                        "retryable": True,
                    },
                    "extensions": self.get_extensions(),
                    **(
                        {"execution_session": execution_session}
                        if execution_session is not None
                        else {}
                    ),
                },
            )
            return StartResult(ok=True, run_id=run_id, message="claude execution needs recovery")
        except (OSError, RunnerError) as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"failed to start claude: {exc}")

        self._heartbeat_run(run_id)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        output_text, parsed_output = _extract_output_text(stdout, self.output_format)
        summary = _extract_summary(output_text)
        validation = extract_callback_validation_from_output(
            output_text,
            task_packet,
            run_id,
        )
        terminal = route_executor_terminal(
            validation,
            completed.returncode,
            executor_name="claude",
            stderr=stderr,
            runtime_failure=detect_retryable_transport_failure(output_text, stderr),
            resource_exhaustion=_claude_resource_exhaustion(
                stdout,
                stderr,
                parsed_output,
                task_packet,
                validation,
                completed.returncode,
            ),
        )
        status = terminal.status
        self._store_run(run_id, execution_root, completed.returncode)
        result = {
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "parsed_output": parsed_output,
            "returncode": completed.returncode,
            "agent_callback": terminal.callback,
            "marker_valid": validation.valid,
            "marker_seen": validation.marker_seen,
            "failure": terminal.failure,
            "resource_exhaustion": terminal.resource_exhaustion,
            "extensions": self.get_extensions(),
            **(
                {"execution_session": execution_session}
                if execution_session is not None
                else {}
            ),
        }
        self._runs[run_id] = PollResult(
            status=status,
            progress={"steps_total": len(steps), "callback_seen": terminal.callback is not None},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"claude execution {status}")

    def supports_permission_prompt_tool(self) -> bool:
        """Return whether the installed Claude CLI exposes the stdio prompt tool.

        The flag is version-dependent.  When absent, the executor falls back to
        an equivalent local MCP broker that captures ``can_use_tool`` events
        from the stream-json control channel.
        """
        if self.agent_bin is None:
            return False
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "--help"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return PERMISSION_PROMPT_TOOL_FLAG in f"{completed.stdout}\n{completed.stderr}"

    def start_control(self, task_packet: dict) -> StartResult:
        """Run the Claude stream/control path with structured approval capture.

        The control path pre-allocates ``--session-id`` and only sends the user
        message after the official init receipt has been verified.  Permission
        requests are captured as ``can_use_tool`` events and answered with
        allow/deny only; ``updated_permissions`` is never applied.  The new run
        chain does not rely on the ``AGENTBC_FINAL_CALLBACK`` marker nor on
        legacy safe-to-full grants.
        """
        steps = task_packet.get("steps") or []
        if not steps:
            return StartResult(ok=False, run_id="", message="no steps")
        if self.agent_bin is None:
            return StartResult(ok=False, run_id="", message="claude unavailable")

        root = _workspace_root(task_packet)
        if root is None or not root.is_dir():
            return StartResult(ok=False, run_id="", message=f"workspace not found: {root}")
        try:
            execution_root = _claude_execution_root(task_packet, root)
            execution_session = _claude_execution_session(task_packet)
        except (OSError, ValueError) as exc:
            return StartResult(ok=False, run_id="", message=f"invalid claude session: {exc}")

        run_id = f"claude-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "claude")
        prompt = _build_prompt(task_packet)
        try:
            permission = resolve_effective_permission(
                task_packet,
                "claude",
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
                "claude", permission["effective_mode"], self.agent_bin
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=str(exc))

        execution_session_id = (
            str(execution_session["session_id"]) if execution_session is not None else ""
        )
        broker = ClaudePermissionPromptBroker(
            session_id=execution_session_id,
            decision_callback=lambda request: self._approval_decision_callback(
                task_packet, run_id, execution_session, request
            ),
            transport_death_callback=lambda request_id: (
                self._invalidate_approval_after_transport_death(
                    task_packet,
                    run_id,
                    execution_session,
                    request_id,
                )
            ),
        )
        try:
            command = self._build_control_command(
                prompt,
                root,
                task_packet,
                permission,
                broker,
            )
        except ValueError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"invalid claude control policy: {exc}")

        captured: dict[str, Any] | None = None
        try:
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command(
                    "claude",
                    command,
                    execution_root,
                    task_packet,
                    executor_run_id=run_id,
                )
            self._heartbeat_run(run_id)
            captured = broker.run_controlled(
                command=command,
                cwd=execution_root,
                timeout_s=self.timeout_s,
                on_started=lambda: self._heartbeat_run(run_id),
            )
        except subprocess.TimeoutExpired:
            self._store_run(run_id, execution_root, None)
            self._mark_run_stale(run_id)
            result = self._timeout_poll_result(steps, run_id, execution_session)
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"steps_total": len(steps)},
                result=result,
            )
            return StartResult(ok=True, run_id=run_id, message="claude execution needs recovery")
        except ABCError as exc:
            self._store_run(run_id, execution_root, None)
            self._mark_run_stale(run_id)
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"steps_total": len(steps)},
                result={
                    "stdout": "",
                    "stderr": str(exc),
                    "summary": "",
                    "returncode": None,
                    "agent_callback": None,
                    "marker_valid": False,
                    "marker_seen": False,
                    "failure": {
                        "kind": exc.code,
                        "layer": "executor",
                        "message": str(exc),
                        "retryable": False,
                    },
                    "extensions": self.get_extensions(),
                    **(
                        {"execution_session": execution_session}
                        if execution_session is not None
                        else {}
                    ),
                },
            )
            return StartResult(ok=True, run_id=run_id, message="claude control init receipt failed")
        except (OSError, RunnerError) as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"failed to start claude: {exc}")

        assert captured is not None
        if captured.get("transport_death_while_approval") is True:
            # Transport died while a single-action approval was pending.  The
            # request is invalidated (crash denial recorded by the broker hook)
            # and the run must be recovered with a fresh request id.
            self._store_run(run_id, execution_root, int(captured.get("returncode") or 0))
            self._mark_run_stale(run_id)
            aborted_request_id = str(captured.get("aborted_request_id") or "")
            failure_message = (
                "Claude transport died while a single-action approval was pending; "
                "the request is invalidated and recovery requires a fresh request id"
            )
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"steps_total": len(steps)},
                result={
                    "stdout": str(captured.get("stdout") or ""),
                    "stderr": str(captured.get("stderr") or ""),
                    "summary": "",
                    "returncode": captured.get("returncode"),
                    "agent_callback": None,
                    "marker_valid": False,
                    "marker_seen": False,
                    "failure": {
                        "kind": "transport_death_while_approval_pending",
                        "layer": "executor",
                        "message": failure_message,
                        "retryable": True,
                    },
                    "aborted_request_id": aborted_request_id,
                    "extensions": self.get_extensions(),
                    **(
                        {"execution_session": execution_session}
                        if execution_session is not None
                        else {}
                    ),
                },
            )
            self._close_run_lease(run_id)
            return StartResult(
                ok=True,
                run_id=run_id,
                message="claude transport died while approval pending",
            )

        self._heartbeat_run(run_id)
        stdout = str(captured.get("stdout") or "")
        stderr = str(captured.get("stderr") or "")
        output_text, parsed_output = _extract_output_text(stdout, self.output_format)
        validation = extract_callback_validation_from_output(
            output_text,
            task_packet,
            run_id,
        )
        terminal = route_executor_terminal(
            validation,
            int(captured.get("returncode") or 0),
            executor_name="claude",
            stderr=stderr,
            runtime_failure=detect_retryable_transport_failure(output_text, stderr),
            resource_exhaustion=_claude_resource_exhaustion(
                stdout,
                stderr,
                parsed_output,
                task_packet,
                validation,
                int(captured.get("returncode") or 0),
            ),
        )
        status = terminal.status
        self._store_run(run_id, execution_root, int(captured.get("returncode") or 0))
        result = {
            "stdout": stdout,
            "stderr": stderr,
            "summary": _extract_summary(output_text),
            "parsed_output": parsed_output,
            "returncode": int(captured.get("returncode") or 0),
            "agent_callback": terminal.callback,
            "marker_valid": validation.valid,
            "marker_seen": validation.marker_seen,
            "failure": terminal.failure,
            "resource_exhaustion": terminal.resource_exhaustion,
            "init_verified": captured.get("init_verified") is True,
            "extensions": self.get_extensions(),
            **(
                {"execution_session": execution_session}
                if execution_session is not None
                else {}
            ),
        }
        self._runs[run_id] = PollResult(
            status=status,
            progress={"steps_total": len(steps), "callback_seen": terminal.callback is not None},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"claude execution {status}")

    def _approval_decision_callback(
        self,
        task_packet: dict[str, Any],
        run_id: str,
        execution_session: dict[str, Any] | None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Route one captured can_use_tool request through the Core approval flow.

        The callback persists an ``agentbc.approval`` v1 receipt and blocks for a
        user decision.  It only ever returns allow/deny and never applies
        ``updated_permissions``.  It is safe to call from the broker thread.

        Fail-closed guarantees: a concurrent second request while one approval is
        already waiting is refused without touching the pending receipt; the
        persisted receipt must bind ``task_id`` + ``executor_run_id`` +
        ``session_id`` + ``request_id`` + fingerprint; and a response that was
        recorded for a different native request is never returned as ``allow``.
        """
        from agent_bridge_connect.approval import APPROVAL_EXTENSION_KEY
        from agent_bridge_connect.notifications import notify_input_required
        from agent_bridge_connect.service import TaskService

        board_root = (
            task_packet.get("task_board") or {}
        ).get("root") or _workspace_root(task_packet)
        service = TaskService(board_root, config=getattr(self, "_config", None) or {})
        task_id = str(task_packet.get("task_id") or "")
        session_id = (
            str(execution_session["session_id"])
            if execution_session is not None
            else str(request.get("session_id") or "")
        )
        tool_name = str(request.get("tool_name") or request.get("tool") or "unknown").strip()
        operation = tool_name or "an action"
        request_id = str(request.get("request_id") or "").strip() or new_request_id()
        fingerprint = compute_request_fingerprint(
            executor="claude",
            session_id=session_id,
            tool_name=tool_name,
            tool_input=request.get("tool_input") or {},
        )
        summary = core_bounded_summary(executor="claude", operation=operation)

        # Concurrent second request fail-closed: only one single-action approval
        # may wait at a time, so one dialog can never authorize two actions.
        try:
            current = service.get_task(task_id)
        except ABCError as exc:
            return {"permission": "deny", "error": exc.code, "request_id": request_id}
        try:
            assert_no_pending_approval(
                current.extensions or {},
                task_status=current.status,
            )
        except ABCError as exc:
            return {"permission": "deny", "error": exc.code, "request_id": request_id}

        try:
            service.block_task_for_approval(
                task_id,
                executor_run_id=run_id,
                session_id=session_id,
                request_id=request_id,
                request_fingerprint=fingerprint,
                executor="claude",
                operation=operation,
                summary=summary,
            )
        except ABCError as exc:
            return {"permission": "deny", "error": exc.code, "request_id": request_id}
        try:
            persisted = service.get_task(task_id)
            validate_approval_receipt(
                (persisted.extensions or {}).get(APPROVAL_EXTENSION_KEY),
                executor="claude",
                task_id=task_id,
                session_id=session_id,
                request_id=request_id,
                executor_run_id=run_id,
                request_fingerprint=fingerprint,
            )
        except ABCError as exc:
            return {"permission": "deny", "error": exc.code, "request_id": request_id}

        def responder(input_id: str, action: str, message: str) -> dict[str, Any]:
            try:
                return service.respond_to_input(
                    task_id,
                    input_id,
                    response_type=action,
                    message=message,
                )
            except ABCError as exc:
                return {"ok": False, "error": exc.code, "status": "failed"}

        try:
            outcome = notify_input_required(service, task_id, responder=responder)
        except ABCError as exc:
            return {"permission": "deny", "error": exc.code, "request_id": request_id}
        response = outcome.get("response") or {}
        # Approve only the bound native request: a decision recorded against a
        # different request id is never reported back as ``allow``.
        response_request_id = str(response.get("request_id") or "").strip()
        if response_request_id and response_request_id != request_id:
            return {"permission": "deny", "error": "approval_request_mismatch", "request_id": request_id}
        decision = str(response.get("approval_decision") or "").strip().lower()
        if decision not in {"allow", "deny"}:
            action = str(outcome.get("dialog_action") or "").strip().lower()
            decision = "deny" if action in {"deny", "dismissed", "timeout"} else "allow"
        return {"permission": decision, "request_id": request_id}

    def _invalidate_approval_after_transport_death(
        self,
        task_packet: dict[str, Any],
        run_id: str,
        execution_session: dict[str, Any] | None,
        request_id: str,
    ) -> None:
        """Invalidate a single-action approval whose native transport died.

        When the Claude transport exits while a can_use_tool request is waiting
        on a user decision, the old request must not be reusable.  If the
        approval receipt is still pending (the user never answered, or the dialog
        failed), a fail-closed ``crash`` denial is recorded on the same receipt so
        the dead request is durably invalidated and recovery must mint a fresh
        request id.  The worker transitions the task to ``needs_recovery`` from
        the executor's poll result.
        """
        from agent_bridge_connect.approval import (
            APPROVAL_EXTENSION_KEY,
            record_approval_decision,
        )
        from agent_bridge_connect.service import TaskService

        board_root = (
            task_packet.get("task_board") or {}
        ).get("root") or _workspace_root(task_packet)
        service = TaskService(board_root, config=getattr(self, "_config", None) or {})
        task_id = str(task_packet.get("task_id") or "")
        try:
            current = service.get_task(task_id)
        except ABCError:
            return
        extensions = dict(current.extensions or {})
        receipt_value = extensions.get(APPROVAL_EXTENSION_KEY)
        if not isinstance(receipt_value, dict):
            return
        try:
            receipt = validate_approval_receipt(receipt_value)
        except ABCError:
            return
        if receipt["state"]["status"] != "pending":
            return
        session_id = str(
            (extensions.get(SESSION_EXTENSION_KEY) or {}).get("session_id") or ""
        )
        try:
            updated = record_approval_decision(
                receipt_value,
                "deny",
                source="crash",
                executor=current.assignee,
                task_id=current.id,
                session_id=session_id,
                request_id=str(receipt.get("request_id") or ""),
                executor_run_id=run_id,
                request_fingerprint=str(receipt.get("request_fingerprint") or ""),
            )
        except ABCError:
            return
        extensions[APPROVAL_EXTENSION_KEY] = updated
        current.extensions = extensions
        current.updated_at = _utc_now()
        service.store.write_task(current.id, current.to_dict())
        service.store.append_event(
            current.id,
            {
                "event_type": "task.approval_transport_death",
                "task_id": current.id,
                "request_id": str(receipt.get("request_id") or ""),
                "executor_run_id": run_id,
                "decision": "deny",
                "decision_source": "crash",
                "reason": "Claude transport died while a single-action approval was pending; the request is invalidated",
                "created_at": _utc_now(),
            },
        )

    def _build_control_command(
        self,
        prompt: str,
        workspace_root: Path,
        task_packet: dict[str, Any],
        permission: dict[str, str] | None,
        broker: "ClaudePermissionPromptBroker",
    ) -> list[str]:
        if self.agent_bin is None:
            raise RuntimeError("claude unavailable")
        selected = permission or permission_record_from_extensions(task_packet.get("extensions"))
        command = [str(self.agent_bin), "-p"]
        command.extend(permission_flags("claude", selected["effective_mode"]))
        execution_session = _claude_execution_session(task_packet)
        if execution_session is not None:
            session_flag = "--resume" if execution_session["resumed"] else "--session-id"
            command.extend([session_flag, execution_session["session_id"]])
        command.extend(["--output-format", "stream-json", "--verbose"])
        if self.supports_permission_prompt_tool():
            command.extend([PERMISSION_PROMPT_TOOL_FLAG, broker.broker_command()])
        if selected["effective_mode"] == "safe":
            for writable_root in _claude_writable_roots(task_packet, workspace_root):
                command.extend(["--add-dir", str(writable_root)])
        if self.model:
            command.extend(["--model", self.model])
        if self.effort:
            command.extend(["--effort", self.effort])
        max_budget_usd = _claude_max_budget_usd(task_packet, self.max_budget_usd)
        if max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(max_budget_usd)])
        if self.allowed_tools:
            tools_arg = _claude_tools_argument(self.allowed_tools)
            if tools_arg:
                command.extend(["--tools", tools_arg])
            command.extend(["--allowedTools", ",".join(self.allowed_tools)])
        command.extend(["--disallowedTools", "TaskCreate,TaskUpdate,TodoWrite"])
        return command

    def _timeout_poll_result(
        self,
        steps: list[dict[str, Any]],
        run_id: str,
        execution_session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stdout": "",
            "stderr": "",
            "summary": "",
            "returncode": None,
            "agent_callback": None,
            "marker_valid": False,
            "marker_seen": False,
            "failure": {
                "kind": "executor_timeout",
                "layer": "executor",
                "message": f"claude safety runtime exceeded after {self.timeout_s}s",
                "retryable": True,
            },
            "extensions": self.get_extensions(),
        }
        if execution_session is not None:
            result["execution_session"] = execution_session
        return result

    def get_extensions(self) -> dict:
        task_permission = None
        if self._last_run_id is not None:
            task_permission = permission_record_from_extensions(
                self._task_packets.get(self._last_run_id, {}).get("extensions")
            )
        metadata: dict[str, Any] = {
            "agent_bin": str(self.agent_bin) if self.agent_bin is not None else "",
            "agent_bin_source": self._discovery.get("source") or "not_found",
            "capability_level": self.capabilities().level,
            "last_run_id": self._last_run_id,
            "model": self.model,
            "effort": self.effort,
            "configured_permission_mode": self.permission_mode,
            "configured_safe_mode": self.safe_mode,
            "task_permission": task_permission,
            "output_format": self.output_format,
            "max_budget_usd": self.max_budget_usd,
            "transport": self.transport,
            "dangerous_permissions_supported": (
                True
                if task_permission is not None
                and task_permission["effective_mode"] == "full"
                else None
            ),
            "dangerous_permissions_allowed": (
                task_permission["effective_mode"] == "full"
                if task_permission is not None
                else None
            ),
            "dangerous_permissions_policy": "explicit_persisted_full_task_only",
            "resume": True,
        }
        if self._last_run_id is not None:
            metadata["last_run"] = self._run_metadata[self._last_run_id]
        return {"executor": {"claude": metadata}}

    def _build_command(
        self,
        prompt: str,
        workspace_root: Path,
        task_packet: dict[str, Any],
        permission: dict[str, str] | None = None,
    ) -> list[str]:
        if self.agent_bin is None:
            raise RuntimeError("claude unavailable")
        selected = permission or permission_record_from_extensions(task_packet.get("extensions"))
        command = [str(self.agent_bin), "-p"]
        command.extend(permission_flags("claude", selected["effective_mode"]))
        execution_session = _claude_execution_session(task_packet)
        if execution_session is not None:
            session_flag = "--resume" if execution_session["resumed"] else "--session-id"
            command.extend([session_flag, execution_session["session_id"]])
        command.extend(["--output-format", self.output_format])
        if self.output_format == "stream-json":
            command.append("--verbose")
        if selected["effective_mode"] == "safe":
            for writable_root in _claude_writable_roots(task_packet, workspace_root):
                command.extend(["--add-dir", str(writable_root)])
        if self.model:
            command.extend(["--model", self.model])
        if self.effort:
            command.extend(["--effort", self.effort])
        max_budget_usd = _claude_max_budget_usd(task_packet, self.max_budget_usd)
        if max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(max_budget_usd)])
        if self.allowed_tools:
            tools_arg = _claude_tools_argument(self.allowed_tools)
            if tools_arg:
                command.extend(["--tools", tools_arg])
            command.extend(["--allowedTools", ",".join(self.allowed_tools)])
        command.extend(["--disallowedTools", "TaskCreate,TaskUpdate,TodoWrite"])
        return command

    def _store_run(self, run_id: str, workspace: Path, returncode: int | None) -> None:
        self._last_run_id = run_id
        self._run_metadata[run_id] = {
            "run_id": run_id,
            "workspace": str(workspace),
            "returncode": returncode,
            "model": self.model,
            "permission": permission_record_from_extensions(
                self._task_packets.get(run_id, {}).get("extensions")
            ),
            "output_format": self.output_format,
            "transport": self.transport,
        }


class ClaudePermissionPromptBroker:
    """Local stdio/MCP-equivalent broker that captures ``can_use_tool``.

    The broker models the ``--permission-prompt-tool stdio`` contract: Claude
    invokes a tool with one JSON request per line on stdin and expects one JSON
    response per line on stdout.  This implementation intentionally only ever
    emits ``{"permission": "allow"}`` or ``{"permission": "deny"}``; it never
    applies ``updated_permissions`` and never grants a safe-to-full upgrade.

    The control run also verifies the official session init receipt before the
    user message is sent, so a fresh pre-allocated ``--session-id`` is bound to
    the running session before any permission request can be produced.
    """

    def __init__(
        self,
        *,
        session_id: str,
        decision_callback: Any,
        verbose: bool = False,
        transport_death_callback: Any = None,
    ) -> None:
        self.session_id = str(session_id or "").strip()
        self.decision_callback = decision_callback
        self.verbose = bool(verbose)
        self.transport_death_callback = transport_death_callback
        self._last_request_id = ""

    def broker_command(self) -> str:
        """Return a self-contained fail-closed stdio broker command.

        The real approval decision is made in-process by the local MCP-equivalent
        broker (:meth:`run_controlled`).  If a newer Claude CLI exposes
        ``--permission-prompt-tool``, this fallback still never grants access:
        it only ever replies ``{"permission": "deny"}`` so permissions cannot be
        widened through the stdio tool.
        """
        script = (
            "import sys,json\n"
            "for line in sys.stdin:\n"
            "    print(json.dumps({'permission':'deny'}))\n"
            "    sys.stdout.flush()\n"
        )
        return f"{sys.executable} -c {shlex.quote(script)}"

    def handle_request_line(self, line: str) -> str:
        """Handle one stdio JSON request line and return the response line."""
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"permission": "deny", "error": "invalid_json"})
        if not isinstance(request, dict):
            return json.dumps({"permission": "deny", "error": "invalid_request"})
        if self.verbose:
            tool_name = str(request.get("tool_name") or request.get("tool") or "unknown")
            print(f"can_use_tool capture: {tool_name}", file=sys.stderr)
        result = self.decision_callback(request)
        self._last_request_id = str((result or {}).get("request_id") or "").strip()
        permission = str((result or {}).get("permission") or "").strip().lower()
        if permission == "allow":
            return json.dumps({"permission": "allow"})
        return json.dumps({"permission": "deny"})

    def run_controlled(
        self,
        *,
        command: list[str],
        cwd: str | Path,
        timeout_s: int,
        on_started: Any = None,
    ) -> dict[str, Any]:
        """Spawn Claude, verify init receipt, then stream the prompt/events.

        Only sends the user message after the official ``init`` receipt matches
        the pre-allocated session id.  ``can_use_tool`` lines are answered with
        allow/deny through the decision callback.  Returns the captured
        ``{returncode, stdout, stderr, init_verified}`` facts for the executor.
        """
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if on_started is not None:
            on_started()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        init_verified = False
        user_message_sent = False
        transport_death_while_approval = False
        aborted_request_id = ""
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.rstrip("\n")
                    stdout_lines.append(line)
                    parsed = _parse_stream_json_line(line)
                    if (
                        not init_verified
                        and isinstance(parsed, dict)
                        and parsed.get("type") == "system"
                        and parsed.get("subtype") == _STREAM_INIT_SUBTYPE
                    ):
                        init_verified = self._verify_init_receipt(parsed)
                        if not init_verified:
                            raise ABCError(
                                "claude_init_receipt_mismatch",
                                "Claude init receipt does not match the pre-allocated session id",
                            )
                    if init_verified and not user_message_sent:
                        user_message_sent = True
                        if process.stdin is not None:
                            process.stdin.write("\n")
                            process.stdin.flush()
                    if isinstance(parsed, dict) and self._is_can_use_tool(parsed):
                        response = self.handle_request_line(json.dumps(parsed))
                        if process.poll() is not None:
                            # The transport died while the user decision was
                            # being made.  The native request is void: transport
                            # death invalidates the request and recovery requires
                            # a fresh request id.
                            transport_death_while_approval = True
                            aborted_request_id = self._last_request_id
                            self._notify_transport_death(aborted_request_id)
                            break
                        if process.stdin is not None:
                            try:
                                process.stdin.write(response + "\n")
                                process.stdin.flush()
                            except OSError:
                                transport_death_while_approval = True
                                aborted_request_id = self._last_request_id
                                self._notify_transport_death(aborted_request_id)
                                break
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            if process.stderr is not None:
                for line in process.stderr:
                    stderr_lines.append(line.rstrip("\n"))
                try:
                    process.stderr.close()
                except OSError:
                    pass
        return {
            "returncode": process.returncode,
            "stdout": "\n".join(stdout_lines),
            "stderr": "\n".join(stderr_lines),
            "init_verified": init_verified,
            "transport_death_while_approval": transport_death_while_approval,
            "aborted_request_id": aborted_request_id,
        }

    def _notify_transport_death(self, request_id: str) -> None:
        """Notify the executor that transport died with a pending approval."""
        if self.transport_death_callback is None:
            return
        try:
            self.transport_death_callback(request_id)
        except ABCError:
            pass

    def _verify_init_receipt(self, receipt: dict[str, Any]) -> bool:
        received = str(receipt.get("session_id") or "").strip()
        return bool(self.session_id) and received == self.session_id

    @staticmethod
    def _is_can_use_tool(parsed: dict[str, Any]) -> bool:
        event_type = str(parsed.get("type") or "").strip()
        if event_type == "can_use_tool":
            return True
        if str(parsed.get("subtype") or "").strip() == "can_use_tool":
            return True
        return (
            event_type == "tool_permission"
            or "can_use_tool" in str(parsed.get("event_type") or "")
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_stream_json_line(line: str) -> dict[str, Any] | None:
    if not line or not line.strip():
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _claude_cleanup_request_error(request: SessionCleanupRequest) -> str:
    if str(request.executor or "").strip().lower() != "claude":
        return "claude_cleanup_executor_mismatch"
    if request.retain is not False or request.project_mode != "ephemeral":
        return "claude_cleanup_mode_invalid"
    if request.strategy != "claude_project_purge":
        return "claude_cleanup_strategy_mismatch"
    session_id = str(request.session_id or "").strip()
    try:
        parsed_session_id = uuid.UUID(session_id)
    except (AttributeError, ValueError):
        return "claude_cleanup_session_invalid"
    if str(parsed_session_id) != session_id.lower():
        return "claude_cleanup_session_invalid"
    if not str(request.task_id or "").strip():
        return "cleanup_task_mismatch"
    if not str(request.project_path or "").strip():
        return "cleanup_project_mismatch"
    return ""


def _supports_claude_project_purge(help_text: str) -> bool:
    frozen_contract = (
        "Usage: claude project purge [options] [path]",
        "Delete all Claude Code state for a project",
        "--yes",
        "Skip confirmation prompt",
    )
    return all(item in help_text for item in frozen_contract)


def _validate_claude_cleanup_paths(request: SessionCleanupRequest) -> str:
    try:
        validate_managed_cleanup_paths(
            request.workspace,
            task_id=request.task_id,
            project_path=request.project_path,
        )
    except ABCError as exc:
        return exc.code
    except (OSError, TypeError, ValueError):
        return "cleanup_path_invalid"
    return ""


def _claude_project_is_absent(output: str) -> bool:
    return _CLAUDE_PROJECT_ABSENT_RE.search(output or "") is not None


def _claude_cleanup_failed(
    error_code: str,
    *,
    retryable: bool = False,
) -> SessionCleanupResult:
    return SessionCleanupResult(
        state="failed",
        capability="supported",
        strategy="claude_project_purge",
        error_code=error_code,
        retryable=retryable,
    )


def _claude_max_budget_usd(
    task_packet: dict[str, Any],
    configured_budget: float | None,
) -> int | float | None:
    extensions = (
        task_packet.get("extensions")
        if isinstance(task_packet.get("extensions"), dict)
        else {}
    )
    if "agentbc.resources" not in extensions:
        value: Any = configured_budget
    else:
        resource = extensions["agentbc.resources"]
        if not isinstance(resource, dict):
            raise ValueError("agentbc.resources must be an object")
        if str(resource.get("executor") or "").strip().lower() != "claude":
            raise ValueError("agentbc.resources.executor must be claude")
        if resource.get("resource") != "max_budget_usd":
            raise ValueError("agentbc.resources.resource must be max_budget_usd")
        value = resource.get("current_limit")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Claude max budget must be a number")
    if not math.isfinite(float(value)) or value <= 0:
        raise ValueError("Claude max budget must be finite and greater than zero")
    return float(value)


def _claude_execution_session(task_packet: dict[str, Any]) -> dict[str, Any] | None:
    extensions = (
        task_packet.get("extensions")
        if isinstance(task_packet.get("extensions"), dict)
        else {}
    )
    if "agentbc.session" not in extensions:
        return None
    session = extensions["agentbc.session"]
    if not isinstance(session, dict):
        raise ValueError("agentbc.session must be an object")
    if str(session.get("executor") or "").strip().lower() != "claude":
        raise ValueError("agentbc.session.executor must be claude")
    session_id = str(session.get("session_id") or "").strip()
    try:
        parsed_session_id = uuid.UUID(session_id)
    except (AttributeError, ValueError) as exc:
        raise ValueError("agentbc.session.session_id must be a UUID") from exc
    if str(parsed_session_id) != session_id.lower():
        raise ValueError("agentbc.session.session_id must use canonical UUID syntax")
    run_ids = session.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in run_ids)
        or len(run_ids) != len(set(run_ids))
    ):
        raise ValueError("agentbc.session.run_ids must contain unique non-empty strings")
    return {
        "version": 1,
        "executor": "claude",
        "session_id": session_id,
        "resumed": bool(run_ids),
        "persistence": "persistent",
        "source": "preallocated",
    }


def _claude_execution_root(task_packet: dict[str, Any], workspace_root: Path) -> Path:
    execution_session = _claude_execution_session(task_packet)
    if execution_session is None:
        return workspace_root
    extensions = task_packet["extensions"]
    session = extensions["agentbc.session"]
    retain = session.get("retain")
    if not isinstance(retain, bool):
        raise ValueError("agentbc.session.retain must be a boolean")
    project_path = str(session.get("project_path") or "").strip()
    if not project_path:
        raise ValueError("agentbc.session.project_path is required for Claude")
    project_root = Path(project_path).expanduser()
    if not project_root.is_absolute():
        raise ValueError("agentbc.session.project_path must be absolute")
    project_root = project_root.resolve()

    workspace = (
        task_packet.get("workspace")
        if isinstance(task_packet.get("workspace"), dict)
        else {}
    )
    if retain:
        if session.get("project_mode") != "native":
            raise ValueError("retained Claude sessions must use native project mode")
        frozen_user_root = Path(
            str(workspace.get("project_root") or workspace.get("root") or "")
        ).expanduser()
        if not frozen_user_root.is_absolute() or project_root != frozen_user_root.resolve():
            raise ValueError("retained Claude project path does not match the frozen workspace")
        if not project_root.is_dir():
            raise ValueError(f"retained Claude project path does not exist: {project_root}")
        return project_root

    if session.get("project_mode") != "ephemeral":
        raise ValueError("non-retained Claude sessions must use ephemeral project mode")
    try:
        validate_path_plan_workspace(workspace)
    except ABCError as exc:
        raise ValueError(f"invalid Claude PathPlan: {exc}") from exc
    planned_path = str(workspace.get("executor_project_root") or "").strip()
    if not planned_path:
        raise ValueError("workspace.executor_project_root is required for ephemeral Claude")
    planned_root = Path(planned_path).expanduser()
    if not planned_root.is_absolute() or project_root != planned_root.resolve():
        raise ValueError("Claude project path does not match workspace.executor_project_root")
    project_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not project_root.is_dir() or project_root.resolve() != planned_root.resolve():
        raise ValueError("Claude project path is not a safe directory")
    return project_root


def _build_prompt(task_packet: dict[str, Any]) -> str:
    """Build the Claude prompt: shared contract plus Claude Code rules."""
    return build_prompt_contract(
        task_packet,
        PromptPlatformExtras(
            opening="You are executing a structured AgentBC task with Claude Code.",
            task_id_line=True,
            summary_line="After completing all steps, print a concise summary.",
            extra_rules=(
                "Do not claim user acceptance. completed only means your agent turn is finished and ready for user review.",
                "Do not create Claude-internal tasks/todos. The AgentBC task record and report are the only execution ledger.",
                "Your process cwd may be an internal temporary Claude project, not the Project root. Never place user deliverables in cwd by relative path; use the exact absolute Project root or Artifact root printed above for every deliverable and for the working directory of commands that create deliverables.",
                "If the step asks another agent to execute or review work, use the AgentBC CLI handoff/dispatch command instead of doing that agent's work inline.",
                "Keep required long-running commands in the foreground with a tool timeout longer than the expected runtime.",
                "If Claude Code moves a command to the background, use BashOutput repeatedly until it exits. Never end this turn while a required background command is still running.",
            ),
        ),
    )


def _workspace_root(task_packet: dict[str, Any]) -> Path | None:
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    root = _optional_text(workspace.get("root"))
    return Path(root).expanduser().resolve() if root else None


def _claude_writable_roots(
    task_packet: dict[str, Any],
    workspace_root: Path,
) -> list[Path]:
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
        text = str(path)
        if text in seen:
            continue
        seen.add(text)
        roots.append(path)
    return roots


def _discover_claude_binary(configured_command: str | None = None) -> dict[str, Any]:
    if configured_command:
        path = Path(configured_command).expanduser()
        return {
            "name": "claude",
            "found": path.is_file(),
            "path": str(path),
            "source": "configured",
            "searched_paths": [str(path)],
            "manual_override": "AGENTBC_CLAUDE_BIN=/your/path/claude",
        }
    return find_binary(
        "claude",
        extra_paths=[str(path) for path in ClaudeExecutor.COMMON_PATHS],
    )


def _find_claude_binary() -> Path | None:
    result = _discover_claude_binary()
    if result.get("found"):
        return Path(str(result["path"])).expanduser()
    return None


def _normalize_allowed_tools(value: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if value is None:
        return ["Read", "Write", "Edit", "Bash", "BashOutput"]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _claude_tools_argument(tools: list[str]) -> str | None:
    builtins = {"Read", "Write", "Edit", "Bash", "BashOutput", "Glob", "Grep", "LS"}
    if all(tool in builtins for tool in tools):
        return ",".join(tools)
    return None


def _extract_output_text(stdout: str, output_format: str) -> tuple[str, Any]:
    if output_format == "json":
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, None
        if isinstance(parsed, dict):
            return str(parsed.get("result") or stdout), parsed
        return stdout, parsed
    if output_format == "stream-json":
        texts: list[str] = []
        parsed_lines: list[Any] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                texts.append(line)
                continue
            parsed_lines.append(payload)
            if isinstance(payload, dict):
                if isinstance(payload.get("result"), str):
                    texts.append(payload["result"])
                message = payload.get("message")
                if isinstance(message, dict):
                    for item in message.get("content") or []:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            texts.append(item["text"])
        return "\n".join(texts) if texts else stdout, parsed_lines
    return stdout, None


def _claude_resource_exhaustion(
    stdout: str,
    stderr: str,
    parsed_output: Any,
    task_packet: dict[str, Any],
    validation: CallbackValidation,
    returncode: int,
) -> dict[str, Any] | None:
    """Detect confirmed Claude budget exhaustion with structured precedence.

    Structured detection: the ``error_max_budget_usd`` subtype in Claude's JSON
    output is authoritative whenever present. Text fallback is intentionally
    narrow: it only accepts the exact ``Exceeded USD budget`` phrase when there
    is no valid callback, the CLI exited non-zero, and the phrase appears in the
    CLI error output. A valid callback or a retryable transport failure always
    wins the terminal-state priority regardless of these diagnostics.
    """
    structured_detected, structured_limit = _claude_structured_budget_receipt(
        parsed_output
    )
    if structured_detected:
        return build_resource_exhaustion(
            "claude",
            "max_budget_usd",
            used=None,
            limit=structured_limit,
            source="structured_error_max_budget_usd",
            snapshot_limit=resource_snapshot_limit(task_packet, "claude"),
        )
    if validation.valid or returncode == 0:
        return None
    error_output = str(stderr or "")
    if _CLAUDE_BUDGET_ERROR_RE.search(error_output) is None:
        candidate = str(stdout or "").lstrip()
        error_output = candidate if _CLAUDE_BUDGET_ERROR_RE.match(candidate) else ""
    if _CLAUDE_BUDGET_ERROR_RE.search(error_output) is None:
        return None
    amount = _claude_budget_amount_from_text(error_output)
    return build_resource_exhaustion(
        "claude",
        "max_budget_usd",
        used=None,
        limit=amount,
        source="text_exceeded_usd_budget",
        snapshot_limit=resource_snapshot_limit(task_packet, "claude"),
    )


def _claude_structured_budget_receipt(
    parsed_output: Any,
) -> tuple[bool, int | float | None]:
    """Recursively find the authoritative subtype and its optional limit."""
    if isinstance(parsed_output, dict):
        if (
            str(parsed_output.get("subtype") or "").strip() == "error_max_budget_usd"
            or str(parsed_output.get("type") or "").strip() == "error_max_budget_usd"
        ):
            amount = _claude_budget_amount_from_text(
                str(parsed_output.get("message") or "")
            )
            if amount is not None:
                return True, amount
            for field in ("budget", "max_budget_usd", "limit", "amount"):
                value = parsed_output.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return True, float(value)
            return True, None
        for value in parsed_output.values():
            detected, limit = _claude_structured_budget_receipt(value)
            if detected:
                return True, limit
    elif isinstance(parsed_output, list):
        for item in parsed_output:
            detected, limit = _claude_structured_budget_receipt(item)
            if detected:
                return True, limit
    return False, None


def _claude_budget_amount_from_text(text: str) -> int | float | None:
    match = _CLAUDE_BUDGET_AMOUNT_RE.search(str(text or ""))
    if match is None:
        return None
    try:
        return float(match.group("amount"))
    except (AttributeError, ValueError):
        return None


def _extract_summary(text: str) -> str:
    return strip_callback_line(text)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_output(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
