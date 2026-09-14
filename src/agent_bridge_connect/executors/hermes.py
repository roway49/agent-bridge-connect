from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_bridge_connect.adapters import (
    AdapterResult,
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    SessionCleanupCapability,
    SessionCleanupRequest,
    SessionCleanupResult,
    StartResult,
)
from agent_bridge_connect.approval import compute_request_fingerprint
from agent_bridge_connect.control import ApprovalControlPlane, ControlPlaneError
from agent_bridge_connect.execution_contract import (
    CallbackValidation,
    ExecutorTerminalResult,
    build_resource_exhaustion,
    extract_callback_validation_from_output,
    resource_snapshot_limit,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.execution_policy import extract_hermes_session_id
from agent_bridge_connect.hermes_acp import (
    HermesAcpError,
    HermesAcpElevationRequired,
    HermesAcpPermissionRequest,
    HermesAcpTransport,
    approval_outcome_for_decision,
    build_approval_message,
    permission_summary,
)
from agent_bridge_connect.media import task_image_paths
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.permission_elevation import task_elevation_protocol_enabled
from agent_bridge_connect.permission_registry import (
    HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
    TRANSPORT_HERMES_ACP,
    build_permission_audit_payload,
    executor_permission_mapping,
    probe_hermes_acp,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import PromptPlatformExtras, build_prompt_contract
from agent_bridge_connect.session import SessionRecoveryRequired

from .base import CLIExecutorBase
from ..path_provider import find_binary
from ..runner import RunnerClient, RunnerError

SAFETY_TIMEOUT_S = 24 * 60 * 60
HERMES_CLEANUP_UNSUPPORTED_CODE = "hermes_session_delete_unavailable"
HERMES_SESSION_DELETE_FAILED_CODE = "hermes_session_delete_failed"
HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE = "hermes_session_delete_missing_session_id"
HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE = "hermes_session_delete_invalid_session_id"
_HERMES_FROZEN_HELP_FIXTURE = "matrix/hermes/0.17.0/help.txt"
_HERMES_FROZEN_VERSION = "0.17.0"
_HERMES_CLEANUP_TIMEOUT_S = 60
# PERM-104-001: ``hermes sessions delete <session_id>`` takes the exact official
# session identifier, and Hermes issues TWO documented identifier shapes.  Both
# are legitimately bound receipts, so both are accepted and nothing else:
#   * ACP ``session/new`` / ``session/load`` -> a UUID
#     (``acp_adapter/session.py``: ``str(uuid.uuid4())``), which is the shape the
#     Hermes ACP stderr receipt binds.  TJBS-001 bound
#     ``18a3e156-6aae-4286-b504-4276f90fc5b2`` and then cleanup rejected that
#     exact receipt with ``hermes_session_delete_invalid_session_id`` because
#     the old check accepted only the CLI token form.
#   * the Hermes CLI chat session token form (``YYYYMMDD_HHMMSS_<hex>``).
# Anything that is not one of those exact identifiers - free-form names, fuzzy
# "id or name" selectors, option-looking tokens - stays rejected.
_HERMES_ACP_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HERMES_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-fA-F]{6,32}$")
_HERMES_SESSION_ABSENT_RE = re.compile(
    r"(?im)^session.*(?:not found|does not exist)"
)
_HERMES_INITIALIZING_LINE_RE = re.compile(
    r"(?m)^[ \t]*Initializing agent\.\.\.[ \t]*\r?$"
)
# ACP ``stopReason`` values that mean the turn ran to a normal completion.
_ACP_COMPLETED_STOP_REASONS = frozenset({"end_turn", "success", "completed"})
# ACP run statuses that describe a live run (RunLease heartbeat eligible).
_ACP_RUNNING_STATUSES = frozenset({"starting", "prompting", "finalizing", "running"})
# PERM-104-001 heartbeat interval for an in-flight ACP turn.  It stays well
# under the RunLease staleness window (120s) so a silent multi-minute model or
# tool interval can never be mistaken for a dead worker.
_HERMES_ACP_HEARTBEAT_INTERVAL_S = 30.0


class _RunLeaseHeartbeat:
    """Keep one RunLease healthy while a Hermes ACP turn is in flight.

    The ACP worker thread blocks in ``transport.prompt`` for the whole turn and
    ``poll()`` may not be called for minutes, so a plain daemon timer beats at a
    fixed interval until the turn ends.  ``beat()`` is invoked once per received
    ACP frame and records a heartbeat at most once per interval, so a fast
    streaming turn never turns into a heartbeat write storm.  It is a liveness
    signal only: it never changes run state, never retries and never completes
    anything.
    """

    def __init__(self, executor: "HermesExecutor", run_id: str) -> None:
        self._executor = executor
        self._run_id = str(run_id)
        self._interval_s = _HERMES_ACP_HEARTBEAT_INTERVAL_S
        self._next_beat_at = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._next_beat_at = 0.0
        self._thread = threading.Thread(
            target=self._loop,
            name=f"agentbc-hermes-acp-heartbeat-{self._run_id}",
            daemon=True,
        )
        self._thread.start()

    def beat(self) -> None:
        """Record one liveness beat if the interval has elapsed."""
        now = time.monotonic()
        if now < self._next_beat_at:
            return
        self._next_beat_at = now + self._interval_s
        try:
            self._executor._heartbeat_run(self._run_id)
        except Exception:  # noqa: BLE001 - heartbeat is never fatal
            pass

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            self.beat()


class HermesExecutor(CLIExecutorBase):
    """L2 executor using the Hermes CLI in headless chat mode."""

    COMMON_PATHS = (
        Path.home() / ".local" / "bin" / "hermes",
        Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
    )

    def __init__(
        self,
        timeout_s: int = SAFETY_TIMEOUT_S,
        profile: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        quiet: bool = True,
        command: str | None = None,
        transport: str = "auto",
        runner_spool: str | None = None,
        runner_token: str | None = None,
        approval_timeout_s: float = 300.0,
    ) -> None:
        super().__init__()
        self.timeout_s = timeout_s
        self.profile = _optional_text(profile)
        self.provider = _optional_text(provider)
        self.model = _optional_text(model)
        self.max_turns = _validate_max_turns(max_turns)
        self.quiet = quiet
        if transport not in {"auto", "direct", "runner", "acp"}:
            raise ValueError(f"Unsupported Hermes transport: {transport}")
        self.transport = transport
        self.approval_timeout_s = max(float(approval_timeout_s), 0.1)
        configured_command = _optional_text(command)
        self._discovery = _discover_hermes_binary(configured_command)
        resolved = configured_command or str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._version = ""
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}
        self._runner_client = RunnerClient(
            spool_root=runner_spool,
            token_path=runner_token,
        )
        self._runner_runs: set[str] = set()
        self._runner_closed: set[str] = set()
        self._runner_poll_errors: dict[str, int] = {}
        self._acp_probe: dict[str, Any] | None = None
        self._acp_runs: dict[str, dict[str, Any]] = {}
        # PERM-104-003: the durable v3 task-elevation latch.  Once a run has
        # published ``PollResult(status="input_required")`` from a durably
        # waiting ``agentbc.input``, that exact result is the only thing this
        # adapter may ever report for the run.  Nothing else - a later
        # ``stopReason``, empty final text, return code, callback parsing,
        # transport close, a duplicate poll or thread-finalizer cleanup - may
        # overwrite it.
        self._elevation_latch: dict[str, PollResult] = {}
        # Test seam: an injected fake ACP transport (same interface as
        # HermesAcpTransport) replaces the spawned ``hermes acp`` subprocess.
        self._acp_transport_override: Any = None

    def _latch_elevation_result(self, run_id: str, poll_result: PollResult) -> None:
        """Latch one run as suspended-for-elevation, exactly once.

        The first call stores the published result and marks the ACP record as
        elevation-latched.  Every later call is an idempotent no-op, so a
        duplicate permission event, a duplicate poll or a late terminal result
        can never replace the authoritative ``input_required`` publication.
        """
        record = self._acp_runs.get(run_id)
        if isinstance(record, dict) and record.get("elevation_latched") is True:
            return
        self._elevation_latch[str(run_id)] = poll_result
        if isinstance(record, dict):
            record["elevation_latched"] = True

    def _latched_result(self, run_id: str) -> PollResult | None:
        """Return the latched task-elevation result for one run, if any."""
        return self._elevation_latch.get(str(run_id))

    def _set_acp_run_status(
        self,
        run_id: str,
        status: str,
        *,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        """Publish one ACP run status, refusing to overwrite the elevation latch.

        All ACP terminal publication goes through here so a latched
        ``input_required`` survives assistant ``stopReason`` handling, callback
        validation, transport close, thread finalization and duplicate polls.
        """
        run_id = str(run_id)
        latched = self._elevation_latch.get(run_id)
        if latched is not None:
            return
        if record is not None and record.get("elevation_latched") is True:
            return
        if progress is None and result is None:
            if record is not None:
                record["status"] = status
            return
        poll_result = PollResult(
            status=status,
            progress=progress if progress is not None else {"events_seen": 0},
            result=result if result is not None else {},
        )
        self._runs[run_id] = poll_result
        if record is not None:
            record["status"] = status
            if result is not None:
                record["result"] = dict(result)

    def probe(self) -> ProbeResult:
        if self.agent_bin is None:
            return ProbeResult(
                ok=False,
                message="hermes unavailable",
                details={
                    "agent_bin": "",
                    "agent_bin_source": self._discovery.get("source") or "not_found",
                    "candidates": [str(path) for path in self.COMMON_PATHS],
                    "searched_paths": self._discovery.get("searched_paths") or [],
                    "manual_override": self._discovery.get("manual_override") or "",
                },
            )

        runner_health, runner_error = self._probe_runner()
        if runner_health is not None:
            return ProbeResult(
                ok=True,
                message="Hermes available through AgentBC Runner",
                details={
                    "agent_bin": str(self.agent_bin),
                    "agent_bin_source": self._discovery.get("source") or "unknown",
                    "profile_mode": "explicit" if self.profile else "inherit",
                    "profile": self.profile,
                    "auth_owner": "hermes_cli",
                    "transport": "runner",
                    "runner": runner_health,
                },
            )
        if self.transport == "runner":
            return ProbeResult(
                ok=False,
                message=f"AgentBC Runner unavailable: {runner_error}",
                details={
                    "agent_bin": str(self.agent_bin),
                    "agent_bin_source": self._discovery.get("source") or "unknown",
                    "transport": "runner",
                    "failure_kind": "runner_unavailable",
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
                message=f"hermes unavailable: {exc}",
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
            message=version or f"hermes exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
                "agent_bin_source": self._discovery.get("source") or "unknown",
                "returncode": completed.returncode,
                "version": version,
                "profile_mode": "explicit" if self.profile else "inherit",
                "profile": self.profile,
                "auth_owner": "hermes_cli",
                "transport": "direct",
                "runner": None,
            },
        )

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            structured_output=True,
            streaming_events=False,
            resume=True,
            cancel=self.transport in {"runner", "acp"},
            input_required=False,
            model_selection=True,
            multimodal=True,
            image_input=True,
            image_generation=True,
            image_editing=True,
            max_input_images=1,
            parallelism=1,
            level=ExecutorLevel.L2,
        )

    def acp_capability(self) -> dict[str, Any]:
        """Frozen Hermes ACP capability probe (PERM-103-002).

        Uses only the official ``hermes acp --check`` / ``hermes acp
        --version`` CLI surface and caches the result per executor instance.
        It never scans Hermes private session databases or logs, never reads
        user configuration, and never modifies the global environment.
        """
        if self._acp_probe is None:
            self._acp_probe = probe_hermes_acp(self.agent_bin)
        return self._acp_probe

    def session_cleanup_capability(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupCapability:
        """Probe only the discovered CLI's exact delete help entry."""
        if request.retain is True:
            return SessionCleanupCapability("not_applicable", "retain")
        if self.agent_bin is None:
            return _hermes_cleanup_unsupported()
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "sessions", "delete", "--help"],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _hermes_cleanup_unsupported()
        if completed.returncode != 0:
            return _hermes_cleanup_unsupported()
        return _hermes_session_cleanup_capability(
            f"{completed.stdout or ''}\n{completed.stderr or ''}"
        )

    def cleanup_session(self, request: SessionCleanupRequest) -> SessionCleanupResult:
        """Delete exactly one session through the official CLI entry.

        The canonical shell-less argv is frozen from the help fixture:
        ``hermes sessions delete <session_id> --yes``. The capability probe
        runs first and fails closed, so no deletion subprocess is ever spawned
        unless the frozen fixture and the discovered CLI version both qualify.
        The session ID is validated as a plain token so it can never inject
        additional flags. Raw CLI output, argv and paths are never included in
        the result.
        """
        if request.retain is True:
            return SessionCleanupResult("retained", "not_applicable", "retain")
        request_error = _hermes_cleanup_request_error(request)
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
                state="unsupported",
                capability=capability.capability,
                strategy=capability.strategy,
                error_code=capability.error_code,
                retryable=False,
            )
        if self.agent_bin is None:
            return SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy="official_session_delete",
                error_code=HERMES_CLEANUP_UNSUPPORTED_CODE,
                retryable=False,
            )
        session_id = request.session_id.strip()
        command = [
            str(self.agent_bin),
            "sessions",
            "delete",
            session_id,
            "--yes",
        ]
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=_HERMES_CLEANUP_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return SessionCleanupResult(
                state="failed",
                capability="supported",
                strategy="official_session_delete",
                error_code=HERMES_SESSION_DELETE_FAILED_CODE,
                retryable=True,
            )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode == 0 or _HERMES_SESSION_ABSENT_RE.search(output):
            return SessionCleanupResult(
                state="succeeded",
                capability="supported",
                strategy="official_session_delete",
                error_code="",
                retryable=False,
            )
        return SessionCleanupResult(
            state="failed",
            capability="supported",
            strategy="official_session_delete",
            error_code=HERMES_SESSION_DELETE_FAILED_CODE,
            retryable=False,
        )

    def start(self, task_packet: dict[str, Any]) -> StartResult:
        steps = task_packet.get("steps") or []
        if not steps:
            return StartResult(ok=False, run_id="", message="no steps")
        if self.agent_bin is None:
            return StartResult(ok=False, run_id="", message="hermes unavailable")

        root = _workspace_root(task_packet)
        if root is not None and not root.is_dir():
            return StartResult(ok=False, run_id="", message=f"workspace not found: {root}")
        images = task_image_paths(task_packet)
        if len(images) > 1:
            return StartResult(ok=False, run_id="", message="Hermes CLI accepts one image input per task iteration")
        run_id = (
            str(task_packet.get("_agentbc_executor_run_id") or "").strip()
            if task_packet.get("runner_authorization_required") is True
            else ""
        ) or f"hermes-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        try:
            permission = resolve_effective_permission(
                task_packet,
                "hermes",
                run_id,
                trusted_runner_managed=(
                    task_packet.get("runner_authorization_required") is True
                ),
            )
        except ABCError as exc:
            return StartResult(ok=False, run_id="", message=f"{exc.code}: {exc}")
        if permission["effective_mode"] != "full":
            try:
                assert_executor_permission_supported(
                    "hermes", permission["effective_mode"], self.agent_bin
                )
            except ABCError as exc:
                return StartResult(ok=False, run_id="", message=f"{exc.code}: {exc}")

        frozen_transport = _hermes_transport_from_permission(permission)
        # ``direct`` and ``runner`` are retained as explicit legacy/test
        # transports.  Production setup selects ACP; there the frozen task
        # mode performs the full/non-full split below.
        if self.transport in {"direct", "runner"}:
            frozen_transport = "direct"
        # Hermes deliberately has two production runtime modes.  Native ACP
        # remains the interactive permission channel for inherit/safe.  Full
        # runs through the headless chat CLI with --yolo, because ACP edit
        # approvals are a separate subsystem and cannot be bypassed by the
        # generic HERMES_YOLO_MODE command policy.
        if frozen_transport == TRANSPORT_HERMES_ACP:
            if self._acp_transport_override is None:
                capability = self.acp_capability()
                if not capability.get("ok"):
                    return StartResult(
                        ok=False,
                        run_id="",
                        message=(
                            "permission_transport_unsupported: Hermes ACP protocol "
                            f"probe failed: {capability.get('reason') or 'unavailable'}"
                        ),
                    )
            return self._start_with_acp(task_packet, root, run_id, permission)

        if (
            self._should_use_runner()
            and task_packet.get("runner_authorization_required") is not True
        ):
            return self._start_with_runner(task_packet, root, run_id, permission)
        if (
            self.transport == "runner"
            and task_packet.get("runner_authorization_required") is not True
        ):
            return StartResult(ok=False, run_id="", message="AgentBC Runner unavailable")

        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "hermes")
        prompt = _build_prompt(task_packet)
        try:
            command = self._build_command(
                prompt,
                images=images,
                permission=permission,
                task_packet=task_packet,
            )
        except ValueError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"invalid Hermes task policy: {exc}")

        try:
            if task_packet.get("runner_authorization_required") is True:
                self._runner_client.authorize_command(
                    "hermes",
                    command,
                    root or Path.cwd(),
                    task_packet,
                    executor_run_id=run_id,
                )
            self._heartbeat_run(run_id)
            completed = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            self._store_run(run_id, root, None)
            self._mark_run_stale(run_id)
            result: dict[str, Any] = {
                "stdout": _coerce_output(stdout),
                "stderr": _coerce_output(stderr),
                "reason": f"hermes safety runtime exceeded after {self.timeout_s}s",
                "timeout_is_failure": False,
                "failure": {
                    "kind": "executor_timeout",
                    "layer": "executor",
                    "message": f"hermes safety runtime exceeded after {self.timeout_s}s",
                    "retryable": True,
                },
                "extensions": self.get_extensions(),
            }
            receipt = _execution_session_receipt(_coerce_output(stderr), task_packet)
            if receipt is not None:
                result["execution_session"] = receipt
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"steps_total": len(steps)},
                result=result,
            )
            return StartResult(ok=True, run_id=run_id, message="hermes execution needs recovery")
        except (OSError, RunnerError) as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"failed to start hermes: {exc}")

        self._heartbeat_run(run_id)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        failure = _runtime_failure_details(stdout, stderr)
        final_response = _extract_final_response(stdout, task_packet)
        iteration = _iteration_budget_diagnostics(stdout, stderr)
        summary = _extract_summary(final_response)
        validation = extract_callback_validation_from_output(
            final_response,
            task_packet,
            run_id,
        )
        terminal = _route_hermes_terminal(
            validation,
            completed.returncode,
            stderr=stderr,
            failure=failure,
            iteration=iteration,
            task_packet=task_packet,
        )
        status = terminal.status
        self._store_run(
            run_id,
            root,
            completed.returncode,
            iteration=iteration,
        )
        result: dict[str, Any] = {
            "stdout": stdout,
            "final_text": final_response,
            "stderr": stderr,
            "returncode": completed.returncode,
            "summary": summary,
            "parsed": _parse_output(final_response),
            "failure": terminal.failure,
            "agent_callback": terminal.callback,
            "marker_valid": validation.valid,
            "marker_seen": validation.marker_seen,
            "iteration": iteration,
            "resource_exhaustion": terminal.resource_exhaustion,
            "extensions": self.get_extensions(),
        }
        receipt = _execution_session_receipt(stderr, task_packet)
        if receipt is not None:
            result["execution_session"] = receipt
        self._runs[run_id] = PollResult(
            status=status,
            progress={"steps_total": len(steps), "returncode": completed.returncode},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"hermes execution {status}")

    def poll(self, run_id: str) -> PollResult:
        # PERM-104-003: the durable task-elevation latch is authoritative.  A
        # duplicate poll, a late assistant stopReason or a thread-finalizer
        # cleanup can never overwrite the published ``input_required`` result.
        latched = self._latched_result(run_id)
        if latched is not None:
            return latched
        acp_run = self._acp_runs.get(run_id)
        if acp_run is not None:
            # PERM-104-001: an in-flight ACP turn is live progress.  Heartbeat
            # on every poll so the RunLease cannot go stale while a healthy
            # Hermes turn keeps working past the old receive interval.
            if str(acp_run.get("status") or "") in _ACP_RUNNING_STATUSES:
                self._heartbeat_run(run_id)
            return PollResult(
                status=str(acp_run.get("status") or "running"),
                progress={
                    "events_seen": len(acp_run.get("events") or []),
                    "control_root": str(acp_run["plane"].root),
                },
                result=dict(acp_run.get("result") or {}),
            )
        if run_id not in self._runner_runs:
            return super().poll(run_id)
        try:
            remote = self._runner_client.status(run_id)
        except RunnerError as exc:
            attempts = self._runner_poll_errors.get(run_id, 0) + 1
            self._runner_poll_errors[run_id] = attempts
            self._heartbeat_run(run_id)
            return PollResult(
                status="running",
                progress={
                    "runner_status": "transient_unavailable",
                    "runner_poll_errors": attempts,
                },
                result={
                    "transport": "runner",
                    "failure": {
                        "kind": "runner_status_transient",
                        "layer": "executor",
                        "message": str(exc),
                        "retryable": True,
                    }
                },
            )
        remote_status = str(remote.get("status") or "failed")
        self._runner_poll_errors.pop(run_id, None)
        if remote_status in {"running", "cancelling"}:
            self._heartbeat_run(run_id)
            return PollResult(
                status="running",
                progress={"pid": remote.get("pid"), "runner_status": remote_status},
                result={"transport": "runner"},
            )

        stdout = str(remote.get("stdout") or "")
        stderr = str(remote.get("stderr") or "")
        returncode = remote.get("returncode")
        failure = _runtime_failure_details(stdout, stderr)
        if remote_status == "cancelled":
            failure = {
                "kind": "runner_cancelled",
                "layer": "executor",
                "message": "Hermes execution was cancelled through AgentBC Runner.",
                "retryable": True,
            }
        task_packet = self._task_packets.get(run_id, {"task_id": "", "steps": [], "workspace": {}})
        final_response = _extract_final_response(stdout, task_packet)
        iteration = _iteration_budget_diagnostics(stdout, stderr)
        summary = _extract_summary(final_response)
        validation = extract_callback_validation_from_output(
            final_response,
            task_packet,
            run_id,
        )
        terminal = _route_hermes_terminal(
            validation,
            returncode if isinstance(returncode, int) else 1,
            stderr=stderr,
            failure=failure,
            iteration=iteration,
            task_packet=task_packet,
        )
        status = "cancelled" if remote_status == "cancelled" else terminal.status
        self._store_run(
            run_id,
            Path(str(remote.get("cwd") or ".")),
            returncode,
            "runner",
            iteration=iteration,
        )
        result_payload: dict[str, Any] = {
            "stdout": stdout,
            "final_text": final_response,
            "stderr": stderr,
            "returncode": returncode,
            "summary": summary,
            "parsed": _parse_output(final_response),
            "failure": failure if remote_status == "cancelled" else terminal.failure,
            "agent_callback": None if remote_status == "cancelled" else terminal.callback,
            "marker_valid": validation.valid,
            "marker_seen": validation.marker_seen,
            "iteration": iteration,
            "resource_exhaustion": None if remote_status == "cancelled" else terminal.resource_exhaustion,
            "transport": "runner",
            "extensions": self.get_extensions(),
        }
        receipt = _execution_session_receipt(stderr, task_packet)
        if receipt is not None:
            result_payload["execution_session"] = receipt
        result = PollResult(
            status=status,
            progress={
                "returncode": returncode,
                "runner_status": remote_status,
                "output_truncated": bool(remote.get("output_truncated")),
            },
            result=result_payload,
        )
        self._runs[run_id] = result
        if run_id not in self._runner_closed:
            self._heartbeat_run(run_id)
            self._close_run_lease(run_id)
            self._runner_closed.add(run_id)
        return result

    def cancel(self, run_id: str) -> AdapterResult:
        acp_run = self._acp_runs.get(run_id)
        if acp_run is not None:
            acp_run["cancelled"] = True
            transport = acp_run.get("transport")
            if transport is not None:
                session_id = str(acp_run.get("session_id") or "")
                if session_id:
                    try:
                        transport.cancel_session(session_id)
                    except HermesAcpError:
                        pass
                try:
                    transport.close()
                except Exception:
                    pass
            return AdapterResult(True, "hermes ACP session cancellation requested")
        if run_id not in self._runner_runs:
            return super().cancel(run_id)
        try:
            result = self._runner_client.cancel(run_id)
        except RunnerError as exc:
            return AdapterResult(False, str(exc))
        return AdapterResult(True, f"runner status: {result.get('status', 'unknown')}")

    def _start_with_runner(
        self,
        task_packet: dict[str, Any],
        root: Path | None,
        run_id: str,
        permission: dict[str, Any],
    ) -> StartResult:
        prompt = _build_prompt(task_packet)
        try:
            command = self._build_command(
                prompt,
                images=task_image_paths(task_packet),
                permission=permission,
                task_packet=task_packet,
            )
        except ValueError as exc:
            return StartResult(ok=False, run_id="", message=f"invalid Hermes task policy: {exc}")
        try:
            remote = self._runner_client.submit(
                "hermes",
                command,
                root or Path.cwd(),
                task=task_packet,
                executor_run_id=run_id,
            )
        except RunnerError as exc:
            return StartResult(ok=False, run_id="", message=f"Runner submit failed: {exc}")
        if str(remote.get("run_id") or "") != run_id:
            return StartResult(
                ok=False,
                run_id="",
                message="Runner submit failed: executor run ID mismatch",
            )
        self._runner_runs.add(run_id)
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(
            task_packet,
            run_id,
            "hermes",
            pid=int(remote.get("pid") or 0),
        )
        self._store_run(run_id, root, None, "runner")
        return StartResult(ok=True, run_id=run_id, message="hermes execution submitted to Runner")

    # ---- Hermes ACP session-first transport (PERM-103-003/004) -------------

    def _start_with_acp(
        self,
        task_packet: dict[str, Any],
        root: Path | None,
        run_id: str,
        permission: dict[str, Any],
    ) -> StartResult:
        """Start the fail-closed ACP session-first run on a worker thread.

        The worker owns one spawned ``hermes acp`` subprocess and the exact
        task-scoped control plane.  The official session receipt is persisted
        through the frozen ``SessionFirstGate`` before ``session/prompt`` is
        ever sent; every transport failure lands in ``needs_recovery``.
        """
        resumed, explicit_session_id = _task_resume_session(task_packet)
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "hermes")
        if task_elevation_protocol_enabled(
            task_packet.get("extensions")
            if isinstance(task_packet.get("extensions"), dict)
            else {}
        ):
            # ACP can emit a permission request before ``start`` returns to
            # the Runner worker.  Register and reload the run now so the v3
            # task-elevation block is bound to the exact persisted session
            # snapshot, and the CLI's later registration is idempotent.
            from agent_bridge_connect.service import TaskService

            task_id = str(task_packet.get("task_id") or "")
            board_root = (
                task_packet.get("task_board") or {}
            ).get("root") or root
            try:
                service = TaskService(
                    board_root,
                    config={"_runner_worker": True},
                )
                service.record_executor_run_started(task_id, run_id)
                persisted_task = service.get_task(task_id)
                refreshed_packet = dict(task_packet)
                refreshed_packet["extensions"] = dict(persisted_task.extensions or {})
                task_packet = refreshed_packet
                self._task_packets[run_id] = dict(task_packet)
            except (ABCError, OSError) as exc:
                self._task_packets.pop(run_id, None)
                self._close_run_lease(run_id)
                return StartResult(
                    ok=False,
                    run_id="",
                    message=f"executor_session_snapshot_refresh_failed: {exc}",
                )
        try:
            plane = self._control_plane_for_run(
                task_packet,
                run_id,
                expected_session_id=explicit_session_id if resumed else None,
            )
        except (ControlPlaneError, SessionRecoveryRequired) as exc:
            self._close_run_lease(run_id)
            return StartResult(
                ok=False,
                run_id="",
                message=f"hermes ACP control unavailable: {exc}",
            )
        command = [str(self.agent_bin), "acp"]
        if task_packet.get("runner_authorization_required") is True:
            try:
                self._runner_client.authorize_command(
                    "hermes",
                    command,
                    root or Path.cwd(),
                    task_packet,
                    executor_run_id=run_id,
                )
            except RunnerError as exc:
                self._close_run_lease(run_id)
                return StartResult(
                    ok=False,
                    run_id="",
                    message=f"Runner authorization failed: {exc}",
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
            "ready": threading.Event(),
            "started_at": time.time(),
            "transport": None,
            "cancelled": False,
        }
        self._acp_runs[run_id] = record
        self._store_run(run_id, root, None, "acp")
        worker = threading.Thread(
            target=self._run_acp_session,
            args=(run_id,),
            name=f"agentbc-hermes-acp-{run_id}",
            daemon=True,
        )
        record["thread"] = worker
        worker.start()
        record["ready"].wait(timeout=min(max(self.timeout_s, 0.1), 10.0))
        return StartResult(ok=True, run_id=run_id, message="hermes ACP session started")

    def _make_acp_transport(self, record: dict[str, Any]) -> Any:
        """Return the injected fake transport (tests) or ``None`` for the real one."""
        return self._acp_transport_override

    def _run_acp_session(self, run_id: str) -> None:
        record = self._acp_runs[run_id]
        plane: ApprovalControlPlane = record["plane"]
        transport: Any = None
        try:
            mapping = executor_permission_mapping(
                "hermes",
                record["permission"]["effective_mode"],
                transport=TRANSPORT_HERMES_ACP,
            )
            env = dict(os.environ)
            env.update(mapping.get("env") or {})
            transport = self._make_acp_transport(record)
            if transport is None:
                transport = HermesAcpTransport(
                    self.agent_bin,
                    cwd=record["root"] or Path.cwd(),
                    env=env,
                )
            record["transport"] = transport
            transport.start()
            transport.initialize()
            cwd = str(record["root"] or Path.cwd())
            if record["resumed"]:
                session_id = str(record["explicit_session_id"] or "").strip()
                if not session_id:
                    raise SessionRecoveryRequired(
                        "missing_executor_session_id",
                        "Explicit resume requires a task session ID.",
                    )
                session_id = transport.load_session(cwd, session_id)
            else:
                session_id = transport.new_session(cwd)
            # Session-first: the official task/run-bound receipt is persisted
            # (and the turn gate opened) before any prompt can enter the ACP
            # session.  The source token is the frozen Hermes receipt channel
            # value from the v1 contract; the ACP session id itself comes from
            # the official protocol, never from scanning Hermes state.
            receipt = {
                "version": 1,
                "executor": "hermes",
                "session_id": session_id,
                "resumed": bool(record["resumed"]),
                "persistence": "persistent",
                "source": "stderr_receipt",
            }
            session_event = plane.record_session_started(receipt)
            record["execution_session"] = receipt
            record["session_id"] = session_id
            if task_elevation_protocol_enabled(
                record["task_packet"].get("extensions")
                if isinstance(record["task_packet"].get("extensions"), dict)
                else {}
            ):
                # The v3 task-elevation receipt must bind to the official ACP
                # session in the real TaskStore before the first native
                # request can become authority evidence.  The run ID was
                # registered in ``_start_with_acp`` before session creation;
                # this call only persists the protocol-issued session snapshot.
                from agent_bridge_connect.service import TaskService

                board_root = (
                    record["task_packet"].get("task_board") or {}
                ).get("root") or record.get("root")
                TaskService(
                    board_root,
                    config={"_runner_worker": True},
                ).record_executor_session_started(
                    str(record["task_packet"].get("task_id") or ""),
                    run_id,
                    receipt,
                )
            record["events"].append(
                {
                    "event_type": "session_started",
                    "source": "agentbc.control",
                    "sequence": len(record["events"]) + 1,
                    "payload": session_event,
                }
            )
            plane.gate.require_before_turn(session_id)
            record["ready"].set()
            prompt = _build_prompt(record["task_packet"])
            blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in task_image_paths(record["task_packet"])[:1]:
                resolved = Path(image).expanduser().resolve()
                blocks.append(
                    {
                        "type": "resource_link",
                        "uri": resolved.as_uri(),
                        "name": resolved.name,
                    }
                )
            self._set_acp_run_status(
                run_id,
                "prompting",
                record=record,
            )
            # PERM-104-001: a healthy Hermes turn runs on a worker thread that
            # blocks in ``prompt`` for the whole turn, and ``poll()`` may not be
            # called for minutes.  The RunLease is therefore heartbeated on a
            # fixed interval for as long as the turn is actually running, so a
            # long model or tool interval is progress - never a stale lease.
            heartbeat = _RunLeaseHeartbeat(self, run_id)
            heartbeat.start()
            try:
                response = transport.prompt(
                    session_id,
                    blocks,
                    on_permission=lambda request: self._handle_acp_permission(
                        record, request
                    ),
                    timeout_s=float(self.timeout_s),
                    on_progress=heartbeat.beat,
                )
            finally:
                heartbeat.stop()
            self._set_acp_run_status(
                run_id,
                "finalizing",
                record=record,
            )
            stop_reason = str(
                response.get("stopReason") or response.get("stop_reason") or "end_turn"
            )
            stderr = transport.stderr_evidence()
            assistant_text = transport.message_text()
            turn_status = "completed" if stop_reason in _ACP_COMPLETED_STOP_REASONS else stop_reason
            plane.record_turn_completed(turn_id=session_id, status=turn_status)
            final_response = _extract_final_response(assistant_text, record["task_packet"])
            summary = _extract_summary(final_response)
            validation = extract_callback_validation_from_output(
                final_response,
                record["task_packet"],
                run_id,
            )
            terminal = _route_hermes_terminal(
                validation,
                0,
                stderr=stderr,
                failure=None,
                iteration=_iteration_budget_diagnostics(assistant_text, stderr),
                task_packet=record["task_packet"],
            )
            status = terminal.status
            result_payload: dict[str, Any] = {
                "stdout": assistant_text,
                "final_text": final_response,
                "stderr": stderr,
                "returncode": 0,
                "summary": summary,
                "parsed": _parse_output(final_response),
                "failure": terminal.failure,
                "agent_callback": terminal.callback,
                "marker_valid": validation.valid,
                "marker_seen": validation.marker_seen,
                "iteration": (
                    _iteration_budget_diagnostics(assistant_text, stderr)
                    if terminal.resource_exhaustion is None
                    else terminal.resource_exhaustion
                ),
                "stop_reason": stop_reason,
                "execution_session": receipt,
                "control_events": plane.events(),
                "extensions": self.get_extensions(),
            }
            self._set_acp_run_status(
                run_id,
                status,
                progress={
                    "steps_total": len(record["task_packet"].get("steps") or []),
                    "stop_reason": stop_reason,
                },
                result=result_payload,
                record=record,
            )
            self._close_run_lease(run_id)
        except HermesAcpElevationRequired as exc:
            # PERM-104-003: one atomic durable state transition.  The v3
            # task-elevation input is already durably waiting in TaskService
            # (verified by the handler before it raised).  Publication of
            # ``PollResult(status="input_required")`` with the exact approval
            # request and the official execution session is the single act that
            # ends this ACP turn: the native request is never answered, the
            # original ACP transport and its run lease close as intended, and
            # the run is latched so no later stopReason, empty final text,
            # return code, callback parse, transport close, duplicate poll or
            # thread finalizer can turn it into a terminal completion.
            record["ready"].set()
            approval_request = dict(exc.approval_request)
            approval_request.setdefault(
                "execution_session", record.get("execution_session")
            )
            approval_request.setdefault("control_events", plane.events())
            approval_request.setdefault("extensions", self.get_extensions())
            approval_request.setdefault("executor_run_id", run_id)
            elevation_result = PollResult(
                status="input_required",
                progress={
                    "events_seen": len(record["events"]),
                    "elevation_state": "suspended_for_elevation",
                },
                result=approval_request,
            )
            record["result"] = dict(approval_request)
            record["status"] = "input_required"
            # Latch first: once the latch exists every later publication path
            # is a no-op, including this run's own terminal publication.
            self._latch_elevation_result(run_id, elevation_result)
            self._set_acp_run_status(
                run_id,
                "input_required",
                progress=dict(elevation_result.progress or {}),
                result=dict(approval_request),
                record=record,
            )
            # Only the original ACP transport/run lease closes here.  The task
            # itself stays waiting, never failed and never cleaned up.
            self._close_run_lease(run_id)
        except (
            ControlPlaneError,
            SessionRecoveryRequired,
            HermesAcpError,
            TimeoutError,
            OSError,
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
                    str(exc) or "Hermes ACP transport failed",
                    request_id=pending_id,
                    evidence={"phase": str(record.get("status") or "starting")},
                )
            except Exception:
                pass
            if record.get("cancelled") is True:
                status = "cancelled"
                failure: dict[str, Any] = {
                    "kind": "hermes_acp_cancelled",
                    "layer": "executor",
                    "message": "Hermes ACP execution was cancelled.",
                    "retryable": True,
                }
            else:
                status = "needs_recovery"
                failure = {
                    "kind": "hermes_acp_transport_failed",
                    "layer": "executor",
                    "message": str(exc),
                    "retryable": True,
                    "timeout_is_failure": isinstance(exc, TimeoutError),
                    # Bounded, sanitized transport evidence (stable code +
                    # field-level detail) so a wire-shape mismatch can be
                    # diagnosed from the task record alone.  Raw frames, raw
                    # argv, tokens and session content never enter this map.
                    "code": getattr(exc, "code", ""),
                    "details": dict(getattr(exc, "details", {}) or {}),
                }
            result_payload = {
                "stderr": transport.stderr_evidence() if transport is not None else "",
                "failure": failure,
                "execution_session": record.get("execution_session"),
                "control_events": plane.events(),
                "extensions": self.get_extensions(),
            }
            self._set_acp_run_status(
                run_id,
                status,
                progress={"events_seen": len(record["events"])},
                result=result_payload,
                record=record,
            )
            self._close_run_lease(run_id)
        finally:
            if transport is not None:
                transport.close()

    def _handle_acp_permission(
        self,
        record: dict[str, Any],
        request: HermesAcpPermissionRequest,
    ) -> dict[str, Any]:
        """Bridge one decoded ACP permission request into the ControlPlane.

        ``request`` is already the normalized output of
        :func:`agent_bridge_connect.hermes_acp.decode_permission_request`: the
        transport validated the canonical wire shape, the official session and
        the offered option surface before this bridge runs.  Only the exact
        offered ``optionId`` outcomes are ever returned.  Duplicate/concurrent
        requests, requests bound to a different session, unsupported option
        lists, mismatched identities, and late responses all fail closed into
        the control plane's recovery state and abort the turn.

        A v3 task-elevation packet never reaches this inline path: the request
        is authority evidence only and is handed to
        :meth:`_handle_task_elevation_permission`, which persists the waiting
        input and ends the original ACP turn without answering the native
        request.
        """
        if task_elevation_protocol_enabled(
            record["task_packet"].get("extensions")
            if isinstance(record["task_packet"].get("extensions"), dict)
            else {}
        ):
            return self._handle_task_elevation_permission(record, request)
        plane: ApprovalControlPlane = record["plane"]
        run_id = str(record["run_id"])
        session_id = str(record.get("session_id") or "")
        request_id = request.request_id
        message = build_approval_message(
            request,
            task_id=str(record["task_packet"].get("task_id") or ""),
            executor_run_id=run_id,
        )
        try:
            event = plane.request_approval(message)
        except (ControlPlaneError, SessionRecoveryRequired) as exc:
            self._record_transport_failed(
                run_id,
                f"Hermes ACP approval rejected: {exc}",
                request_id=str(request_id),
            )
            raise HermesAcpError(
                "hermes_acp_approval_rejected",
                str(exc),
                {"code": getattr(exc, "code", "")},
            ) from exc
        approval: dict[str, Any] = {
            "type": "permission",
            "request_id": str(request_id),
            "kind": "permission",
            "scope": "single_action",
            "session_id": session_id,
        }
        # PERM-104-002 v2: the offered native choices ride on the poll result
        # so the CLI worker can persist them on the input request.  The
        # choices come from the CONTROL PLANE's normalized pending request,
        # because that is where the opaque handles are computed and bound.
        pending_after = plane.status().get("pending_request")
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
        record["events"].append(
            {
                "event_type": "approval_requested",
                "source": "agentbc.control",
                "sequence": len(record["events"]) + 1,
                "payload": event,
            }
        )
        record["result"] = {
            "events": list(record["events"]),
            "execution_session": record.get("execution_session"),
            "approval_request": approval,
            "extensions": self.get_extensions(),
        }
        # Result must be visible before the status flips so poll() can never
        # observe input_required without the approval_request payload.  This is
        # the historical v2 single-action surface: the run lease is suspended,
        # not closed, and the run is NOT latched as a task elevation.
        self._set_acp_run_status(
            run_id,
            "input_required",
            progress={"events_seen": len(record["events"])},
            result=dict(record["result"]),
            record=record,
        )
        self._suspend_run(run_id)
        try:
            response = plane.wait_for_decision(
                str(request_id),
                self.approval_timeout_s,
            )
        except ControlPlaneError as exc:
            self._record_transport_failed(
                run_id,
                f"Hermes ACP approval wait failed: {exc}",
                request_id=str(request_id),
            )
            raise HermesAcpError(
                "hermes_acp_approval_wait_failed",
                str(exc),
                {"code": exc.code},
            ) from exc
        decision = str(response.get("decision") or "")
        # PERM-104-002 v2: the response carries the exact selected native
        # choice; its original ACP optionId is returned verbatim.  The v1
        # fallback (allow_once/cancelled) only fires for decisions recorded
        # without a choice payload.
        outcome = approval_outcome_for_decision(
            response.get("choice") if isinstance(response.get("choice"), dict) else decision
        )
        self._resume_run(run_id)
        record["status"] = "running"
        record.setdefault("approval_history", []).append(
            {"request_id": str(request_id), "decision": decision}
        )
        return outcome

    def _handle_task_elevation_permission(
        self,
        record: dict[str, Any],
        request: HermesAcpPermissionRequest,
    ) -> dict[str, Any]:
        """Persist one native Hermes request as a contained-full task wait.

        The ACP request is authority evidence only.  No ``allow_once`` or
        native permission response is produced for a v3 task; approval is a
        separate Core decision which dispatches one Runner-owned full
        continuation after the original ACP worker has ended.

        ``request`` is the normalized output of
        :func:`agent_bridge_connect.hermes_acp.decode_permission_request`, so
        the canonical wire shape, the official session binding and the offered
        option surface have already been validated mechanically.  This method
        never re-reads the raw frame to classify anything.

        Publication is one atomic durable state transition: the waiting v3
        input is re-read from TaskService and only a durably ``waiting`` input
        raises the elevation signal that publishes
        ``PollResult(status="input_required")``.  If persistence did not leave
        exactly one waiting input the run fails closed instead of reporting a
        terminal completion.
        """
        from agent_bridge_connect.permission_elevation import PERMISSION_ELEVATION_MODE
        from agent_bridge_connect.service import TaskService

        task_packet = record["task_packet"]
        run_id = str(record["run_id"] or "")
        session_id = str(record.get("session_id") or "").strip()
        if session_id and request.session_id != session_id:
            raise HermesAcpError(
                "hermes_acp_permission_session_mismatch",
                "ACP permission request is bound to a different official session.",
                {
                    "expected_session_id": session_id[:80],
                    "actual_session_id": request.session_id[:80],
                },
            )
        request_id = str(request.request_id).strip()
        operation = str(request.tool_call.kind or request.tool_call.title or "permission").strip()
        tool_call_id = str(request.tool_call.tool_call_id or "").strip()
        native_event = "hermes_acp.session/request_permission"
        request_fingerprint = compute_request_fingerprint(
            executor="hermes",
            session_id=session_id,
            tool_name=operation,
            tool_input=dict(request.tool_call.raw_input or {}),
            extra={"method": "session/request_permission"},
        )
        action_fingerprint = compute_request_fingerprint(
            executor="hermes",
            session_id=session_id,
            tool_name=operation,
            tool_input={
                "toolCallId": tool_call_id,
                "kind": request.tool_call.kind,
                "title": request.tool_call.title,
            },
        )
        authority = {
            "executor": "hermes",
            "protocol": "hermes_acp",
            "protocol_version": 1,
            "method": "session/request_permission",
        }
        board_root = (
            task_packet.get("task_board") or {}
        ).get("root") or record.get("root")
        service = TaskService(
            board_root,
            config={"_runner_worker": True},
        )
        task_id = str(task_packet.get("task_id") or "")
        blocked = service.block_task_for_elevation(
            task_id,
            executor_run_id=run_id,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            executor="hermes",
            operation=operation,
            summary=permission_summary(request.tool_call.title),
            reason=permission_summary(request.tool_call.title),
            reason_detail="",
            execution_session=record.get("execution_session"),
            tool_name=operation,
            tool_use_id=tool_call_id,
            action_fingerprint=action_fingerprint,
            escalation_domain="hermes_acp",
            profile_digest="",
            control_path=TRANSPORT_HERMES_ACP,
            native_event=native_event,
            authority=authority,
            path_plan_digest="",
            containment_profile_digest="",
            full_preflight=None,
        )
        persisted = service.get_task(task_id)
        waiting_input = (persisted.extensions or {}).get("agentbc.input")
        if not isinstance(waiting_input, dict) or waiting_input.get("status") != "waiting":
            raise HermesAcpError(
                "hermes_acp_task_elevation_persistence_failed",
                "Hermes task elevation did not leave one waiting v3 input.",
            )
        approval_request = dict(waiting_input)
        approval_request["session_id"] = session_id
        approval_request["requested_permission"] = "full"
        approval_request["elevation_mode"] = PERMISSION_ELEVATION_MODE
        approval_request["native_event"] = native_event
        record["events"].append(
            {
                "event_type": "task_elevation_requested",
                "source": "agentbc.service",
                "sequence": len(record["events"]) + 1,
                "payload": {
                    **dict(blocked),
                    "request_id": request_id,
                    "request_fingerprint": request_fingerprint,
                    "native_event": native_event,
                },
            }
        )
        # The caller raises this marker through ``prompt`` so the fake/real ACP
        # transport cannot answer the original permission request.
        raise HermesAcpElevationRequired(approval_request)

    def _should_use_runner(self) -> bool:
        if self.transport == "direct":
            return False
        health, _ = self._probe_runner()
        return health is not None

    def _probe_runner(self) -> tuple[dict[str, Any] | None, str]:
        if self.transport == "direct":
            return None, "direct transport selected"
        try:
            health = self._runner_client.health()
        except RunnerError as exc:
            return None, str(exc)
        if "hermes" not in (health.get("executors") or []):
            return None, "Runner does not allow the Hermes executor"
        return health, ""

    def _build_command(
        self,
        prompt: str,
        images: list[Path] | None = None,
        permission: dict[str, str] | None = None,
        task_packet: dict[str, Any] | None = None,
    ) -> list[str]:
        if self.agent_bin is None:
            raise RuntimeError("hermes unavailable")
        command = [str(self.agent_bin)]
        if self.profile:
            command.extend(["-p", self.profile])
        command.append("chat")
        selected = permission or permission_record_from_extensions(None)
        command.extend(permission_flags("hermes", selected["effective_mode"]))
        max_turns = _task_max_turns(task_packet, self.max_turns)
        if max_turns is not None:
            command.extend(["--max-turns", str(max_turns)])
        resumed, session_id = _task_resume_session(task_packet)
        if resumed:
            command.extend(["--resume", session_id])
        if images:
            command.extend(["--image", str(images[0])])
        # Hermes ``-Q`` suppresses its native iteration-budget lifecycle line.
        # Keep quiet output for standalone/legacy calls, but use the official
        # one-shot surface for frozen AgentBC max-turn tasks so exhaustion is
        # mechanically observable instead of being guessed from model prose.
        if _task_has_hermes_turn_limit(task_packet):
            command.append("--oneshot")
        elif self.quiet or _task_has_session_policy(task_packet):
            command.append("-Q")
        if self.provider:
            command.extend(["--provider", self.provider])
        if self.model:
            command.extend(["--model", self.model])
        command.extend(["--source", "tool"])
        command.extend(["-q", prompt])
        return command

    def get_extensions(self) -> dict[str, Any]:
        """Return metadata suitable for extensions.executor.hermes."""
        if not self._version and self.agent_bin is not None:
            self.probe()
        active_transport = self.transport
        if self._last_run_id is not None:
            active_transport = str(
                self._run_metadata.get(self._last_run_id, {}).get("transport")
                or active_transport
            )
        metadata: dict[str, Any] = {
            "version": self._version,
            "runtime": "cli",
            "agent_bin": str(self.agent_bin) if self.agent_bin is not None else "",
            "agent_bin_source": self._discovery.get("source") or "not_found",
            "capability_level": self.capabilities().level,
            "last_run_id": self._last_run_id,
            "profile_mode": "explicit" if self.profile else "inherit",
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "max_turns": self.max_turns,
            "auth_owner": "hermes_cli",
            "transport": active_transport,
            "permission": (
                permission_record_from_extensions(
                    self._task_packets.get(self._last_run_id, {}).get("extensions")
                )
                if self._last_run_id is not None
                else None
            ),
        }
        task_elevation = task_elevation_protocol_enabled(
            self._task_packets.get(self._last_run_id, {}).get("extensions")
            if self._last_run_id is not None
            else {}
        )
        if task_elevation and isinstance(metadata.get("permission"), dict):
            permission_metadata = dict(metadata["permission"])
            raw_mapping = permission_metadata.get("mapping")
            if isinstance(raw_mapping, dict):
                mapping = {
                    key: dict(value) if isinstance(value, dict) else value
                    for key, value in raw_mapping.items()
                }
                hermes_mapping = mapping.get("hermes")
                if isinstance(hermes_mapping, dict):
                    hermes_mapping["decisions"] = ["approve_full", "deny"]
                permission_metadata["mapping"] = mapping
            metadata["permission"] = permission_metadata
        if self._last_run_id is not None:
            last_run = self._run_metadata[self._last_run_id]
            metadata["last_run"] = last_run
            if isinstance(last_run.get("iteration"), dict):
                metadata["iteration"] = last_run["iteration"]
        acp = self.acp_capability()
        acp_state = "unavailable"
        if acp.get("ok"):
            acp_state = "available"
        if active_transport == "acp":
            active_run = self._acp_runs.get(str(self._last_run_id or ""), {})
            acp_state = "bound" if active_run.get("session_id") else "starting"
        elif self._last_run_id is not None:
            acp_state = "not_active"
        metadata["acp"] = {
            "transport": TRANSPORT_HERMES_ACP,
            "capability_id": HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
            "check": {
                "ok": acp["ok"],
                "reason": acp.get("reason") or "",
                "version": acp.get("version"),
            },
            "request_permission": {
                # Historical hand-built packets retain the v2 compatibility
                # surface.  Normal TaskService packets use the v3 cutover:
                # the native event is authority only and the human decision
                # is the separate contained-full task elevation.
                "state": acp_state,
                "capability_id": HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
                "decisions": (
                    ["approve_full", "deny"]
                    if task_elevation
                    else ["allow_once", "deny"]
                ),
            },
        }
        permission = metadata.get("permission")
        if isinstance(permission, dict):
            mode = permission.get("effective_mode")
            if isinstance(mode, str):
                mapping = executor_permission_mapping("hermes", mode)
                if task_elevation:
                    mapping = dict(mapping)
                    mapping["decisions"] = ["approve_full", "deny"]
                metadata["permission_capability"] = mapping
                if mode == "full":
                    metadata["permission_audit"] = build_permission_audit_payload(
                        permission,
                        executor="hermes",
                    )
        return {
            "executor.hermes": metadata,
            "executor": {"hermes": metadata},
        }

    def _store_run(
        self,
        run_id: str,
        workspace: Path | None,
        returncode: int | None,
        transport: str = "direct",
        iteration: dict[str, Any] | None = None,
    ) -> None:
        self._last_run_id = run_id
        metadata: dict[str, Any] = {
            "run_id": run_id,
            "workspace": str(workspace) if workspace is not None else "",
            "returncode": returncode,
            "transport": transport,
        }
        if iteration is not None:
            metadata["iteration"] = iteration
        self._run_metadata[run_id] = metadata


def _discover_hermes_binary(configured_command: str | None = None) -> dict[str, Any]:
    if configured_command:
        path = Path(configured_command).expanduser()
        return {
            "name": "hermes",
            "found": path.is_file(),
            "path": str(path),
            "source": "configured",
            "searched_paths": [str(path)],
            "manual_override": "AGENTBC_HERMES_BIN=/your/path/hermes",
        }
    return find_binary(
        "hermes",
        extra_paths=[str(path) for path in HermesExecutor.COMMON_PATHS],
    )


_HERMES_SESSIONS_DELETE_USAGE_RE = re.compile(
    r"^usage:\s+hermes\s+sessions\s+delete\b",
    re.IGNORECASE | re.MULTILINE,
)
_HERMES_DELETE_POSITIONAL_RE = re.compile(
    r"^\s+session_id\b",
    re.MULTILINE,
)
_HERMES_REJECTED_ENTRY_MARKERS = (
    "or session name",
    "takes precedence",
    "--last",
    "picker",
    "prune",
    "purge",
    "--continue",
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


def _hermes_has_exact_session_delete_entry(help_text: str) -> bool:
    """Return True only for the official ``sessions delete`` exact-ID entry.

    The qualifying entry is the documented ``hermes sessions delete [-h]
    [--yes] session_id`` form: a delete action with one positional exact
    session ID and a skip-confirmation flag. Resume/continue flags, recent
    session pickers, fuzzy "id or name" selectors and global prune/purge
    entries do not qualify.
    """
    if not help_text:
        return False
    if _HERMES_SESSIONS_DELETE_USAGE_RE.search(help_text) is None:
        return False
    if _HERMES_DELETE_POSITIONAL_RE.search(help_text) is None:
        return False
    lowered = help_text.lower()
    return not any(marker in lowered for marker in _HERMES_REJECTED_ENTRY_MARKERS)


def _hermes_session_cleanup_capability(help_text: str) -> SessionCleanupCapability:
    """Derive the Hermes cleanup capability from frozen help fixture text."""
    if _hermes_has_exact_session_delete_entry(help_text):
        return SessionCleanupCapability(
            capability="supported",
            strategy="official_session_delete",
            error_code="",
        )
    return _hermes_cleanup_unsupported()


def _hermes_cleanup_unsupported() -> SessionCleanupCapability:
    return SessionCleanupCapability(
        capability="unsupported",
        strategy="none",
        error_code=HERMES_CLEANUP_UNSUPPORTED_CODE,
    )


def _hermes_session_delete_identifier_error(session_id: str) -> str:
    """Return the stable failure code for an unusable delete identifier.

    The identifier must be the exact officially bound session identifier in one
    of the two documented Hermes shapes, and it must stay a single shell-less
    positional argv token (the delete contract is positional).
    """
    if not session_id:
        return HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE
    unsafe = (
        session_id.startswith("-")
        or len(session_id) > 128
        or re.search(r"[\s/\\\0]", session_id) is not None
    )
    if unsafe:
        return HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE
    documented = (
        _HERMES_ACP_SESSION_ID_RE.fullmatch(session_id) is not None
        or _HERMES_SESSION_ID_RE.fullmatch(session_id) is not None
    )
    if not documented:
        return HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE
    return ""


def _hermes_cleanup_request_error(request: SessionCleanupRequest) -> str:
    """Validate one delete request against the official ``sessions delete`` contract.

    PERM-104-001: the coordinator always passes the exact officially bound
    receipt identifier, so this only proves the identifier is a documented Hermes
    session-id shape.  A run executed over the Hermes ACP transport binds a UUID
    session id, and the previous CLI-token-only shape check rejected that exact
    receipt before any deletion could run.
    """
    if str(request.executor or "").strip().lower() != "hermes":
        return "hermes_cleanup_executor_mismatch"
    if request.retain is not False or request.project_mode != "none":
        return "hermes_cleanup_mode_invalid"
    if request.strategy != "official_session_delete":
        return "hermes_cleanup_strategy_mismatch"
    return _hermes_session_delete_identifier_error(str(request.session_id or "").strip())


def _version_number_matches(output: str, expected: str) -> bool:
    match = re.search(r"v?(\d+\.\d+\.\d+)", output)
    return match is not None and match.group(1) == expected


def _find_hermes_binary() -> Path | None:
    discovery = _discover_hermes_binary()
    if discovery["found"]:
        return Path(discovery["path"])
    return None


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _validate_max_turns(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max_turns must be a positive integer")
    return value


def _task_max_turns(
    task_packet: dict[str, Any] | None,
    configured_max_turns: int | None,
) -> int | None:
    """Prefer the frozen task limit while retaining standalone CLI compatibility."""
    if not isinstance(task_packet, dict):
        return configured_max_turns
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict) or "agentbc.resources" not in extensions:
        return configured_max_turns
    resources = extensions.get("agentbc.resources")
    if not isinstance(resources, dict):
        raise ValueError("agentbc.resources must be an object")
    if str(resources.get("executor") or "").strip().lower() != "hermes":
        raise ValueError("agentbc.resources.executor must be hermes")
    if resources.get("resource") != "max_turns":
        raise ValueError("agentbc.resources.resource must be max_turns")
    return _validate_max_turns(resources.get("current_limit"))


def _task_resume_session(
    task_packet: dict[str, Any] | None,
    *,
    resume_fact: bool | None = None,
    resume_session_id: str = "",
) -> tuple[bool, str]:
    if not isinstance(task_packet, dict):
        return False, ""
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict) or "agentbc.session" not in extensions:
        return False, ""
    session = extensions.get("agentbc.session")
    if not isinstance(session, dict):
        raise ValueError("agentbc.session must be an object")
    if str(session.get("executor") or "").strip().lower() != "hermes":
        raise ValueError("agentbc.session.executor must be hermes")
    frozen_fact = task_packet.get("_agentbc_resume_fact")
    if resume_fact is None and isinstance(frozen_fact, dict):
        frozen_value = frozen_fact.get("resumed")
        if type(frozen_value) is bool:
            resume_fact = frozen_value
            resume_session_id = str(
                frozen_fact.get("session_id") or ""
            ).strip()
    if resume_fact is not None:
        if not resume_fact:
            return False, ""
        session_id = str(resume_session_id or session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("agentbc.session.session_id is required for resume")
        return True, session_id
    run_ids = session.get("run_ids")
    if not isinstance(run_ids, list):
        raise ValueError("agentbc.session.run_ids must be a list")
    resumed = bool(run_ids)
    if not resumed:
        return False, ""
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("agentbc.session.session_id is required for resume")
    return True, session_id


def _task_has_session_policy(task_packet: dict[str, Any] | None) -> bool:
    if not isinstance(task_packet, dict):
        return False
    extensions = task_packet.get("extensions")
    return isinstance(extensions, dict) and isinstance(
        extensions.get("agentbc.session"), dict
    )


def _task_has_hermes_turn_limit(task_packet: dict[str, Any] | None) -> bool:
    """Return whether one task carries a frozen Hermes max-turn snapshot."""
    if not isinstance(task_packet, dict):
        return False
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict):
        return False
    resources = extensions.get("agentbc.resources")
    return (
        isinstance(resources, dict)
        and str(resources.get("executor") or "").strip().lower() == "hermes"
        and resources.get("resource") == "max_turns"
    )


def _execution_session_receipt(
    stderr: str,
    task_packet: dict[str, Any],
) -> dict[str, Any] | None:
    session_id = extract_hermes_session_id(stderr)
    if session_id is None:
        return None
    resumed, _ = _task_resume_session(task_packet)
    return {
        "version": 1,
        "executor": "hermes",
        "session_id": session_id,
        "resumed": resumed,
        "persistence": "persistent",
        "source": "stderr_receipt",
    }


def _hermes_transport_from_permission(permission: dict[str, Any]) -> str:
    """Select Hermes' runtime mode from the task's frozen permission mode.

    The mode, not a historical transport projection, is authoritative.  This
    lets active full snapshots created before the split use the corrected
    zero-prompt CLI path while inherit/safe continue to use native ACP.
    """

    mode = str(permission.get("effective_mode") or "inherit").strip().lower()
    return "direct" if mode == "full" else TRANSPORT_HERMES_ACP


def _workspace_root(task_packet: dict[str, Any]) -> Path | None:
    workspace = task_packet.get("workspace") or {}
    if not isinstance(workspace, dict):
        return None
    root = workspace.get("root")
    if not root:
        return None
    return Path(str(root)).expanduser().resolve()


def _build_prompt(task_packet: dict[str, Any]) -> str:
    """Build the Hermes prompt: shared contract plus Hermes transport notes."""
    return build_prompt_contract(
        task_packet,
        PromptPlatformExtras(
            opening="You are executing a structured AgentBC task.",
            image_note="An image input is attached through the native Hermes CLI image interface:",
            image_inputs=tuple(str(image) for image in task_image_paths(task_packet)[:1]),
            image_rule=(
                "For image generation or image editing work, use the native image_generate "
                "capability and save the final bitmap deliverables under the Artifact root; do not "
                "return only prose or preview links."
            ),
            summary_line="Return a concise execution summary and mention any files changed.",
        ),
    )


def _extract_final_response(stdout: str, task_packet: dict[str, Any]) -> str:
    """Return the actual Hermes assistant response from raw CLI output.

    Hermes single-query mode may print warnings, a terminal-wrapped
    ``Query: <prompt>`` echo, and an ``Initializing agent...`` boundary before
    the actual response. The prompt embeds the example final marker, so
    validating the raw stdout would count that echoed example plus the real
    marker as a duplicate and fail. Prefer the explicit initialization
    boundary when present, then retain the older Query/task-prompt fallback
    for output variants without it. A genuinely duplicated marker inside the
    actual response still fails as ``completion_marker_duplicate``.
    """
    output = (stdout or "").strip()
    if not output:
        return output
    initialization = _HERMES_INITIALIZING_LINE_RE.search(output)
    if initialization is not None:
        return output[initialization.end():].lstrip()
    prompt = _build_prompt(task_packet)
    if output.startswith("Query:"):
        candidate = output[len("Query:"):].lstrip()
        if prompt and candidate.startswith(prompt):
            candidate = candidate[len(prompt):]
        output = candidate
    elif prompt and output.startswith(prompt):
        output = output[len(prompt):]
    return output.lstrip()


_ITERATION_MAX_REASON_RE = re.compile(r"max_iterations_reached\((\d+)/(\d+)\)")
_ITERATION_BUDGET_MSG_RE = re.compile(
    r"iteration budget exhausted[^\d]{0,40}?(\d+)/(\d+)",
    re.IGNORECASE,
)
_ITERATION_REACHED_MAX_RE = re.compile(
    r"reached\s+maximum\s+iterations\s*\((\d+)\)",
    re.IGNORECASE,
)


def _iteration_budget_diagnostics(stdout: str, stderr: str) -> dict[str, Any]:
    """Detect the documented Hermes iteration-budget exhaustion forms.

    Hermes reports exhaustion as the turn-exit reason ``max_iterations_reached
    (N/M)``, the ``budget_exhausted`` reason, a human ``Iteration budget
    exhausted (N/M)`` status line, or ``Reached maximum iterations (N)``.
    Returns a diagnostics dict with ``iteration_exhausted``,
    ``iteration_used``, ``iteration_limit`` and ``iteration_source``. No
    credentials or conversation content are included.
    """
    combined = f"{stdout}\n{stderr}"
    reason = _ITERATION_MAX_REASON_RE.search(combined)
    if reason:
        return {
            "iteration_exhausted": True,
            "iteration_used": int(reason.group(1)),
            "iteration_limit": int(reason.group(2)),
            "iteration_source": "max_iterations_reached",
        }
    if re.search(r"\bbudget_exhausted\b", combined):
        return {
            "iteration_exhausted": True,
            "iteration_used": None,
            "iteration_limit": None,
            "iteration_source": "budget_exhausted",
        }
    message = _ITERATION_BUDGET_MSG_RE.search(combined)
    if message:
        return {
            "iteration_exhausted": True,
            "iteration_used": int(message.group(1)),
            "iteration_limit": int(message.group(2)),
            "iteration_source": "iteration_budget_message",
        }
    reached = _ITERATION_REACHED_MAX_RE.search(combined)
    if reached:
        limit = int(reached.group(1))
        return {
            "iteration_exhausted": True,
            "iteration_used": limit,
            "iteration_limit": limit,
            "iteration_source": "reached_maximum_iterations",
        }
    return {
        "iteration_exhausted": False,
        "iteration_used": None,
        "iteration_limit": None,
        "iteration_source": "none",
    }


def _route_hermes_terminal(
    validation: CallbackValidation,
    returncode: int,
    *,
    stderr: str,
    failure: dict[str, Any] | None,
    iteration: dict[str, Any],
    task_packet: dict[str, Any] | None = None,
) -> ExecutorTerminalResult:
    """Route the Hermes terminal with iteration-budget classification.

    A valid completed marker and a strict permission wait keep their declared
    meaning.  A native Hermes max-iteration receipt overrides an agent-authored
    ordinary/choice wait so a summary generated at the limit cannot bypass the
    resource decision state machine.  Retryable transport/runtime failures
    still keep ``needs_recovery``.  Confirmed exhaustion is classified through
    the shared resource contract, and a receipt limit that conflicts with the
    task snapshot fails closed to ``needs_recovery``.
    """
    exhaustion = _hermes_resource_exhaustion(iteration, task_packet)
    routed_validation = validation
    callback = validation.callback if validation.valid else None
    input_details = callback.get("input") if isinstance(callback, dict) else None
    input_type = (
        str(input_details.get("type") or "").strip().lower()
        if isinstance(input_details, dict)
        else ""
    )
    if (
        isinstance(exhaustion, dict)
        and exhaustion.get("detected") is True
        and isinstance(callback, dict)
        and callback.get("final_state") == "input_required"
        and input_type != "permission"
    ):
        routed_validation = CallbackValidation(
            marker_seen=validation.marker_seen,
            valid=False,
            callback=None,
            code="hermes_resource_exhaustion_authoritative",
            message="Hermes reported native max-turn exhaustion",
        )
    return route_executor_terminal(
        routed_validation,
        returncode,
        executor_name="hermes",
        stderr=stderr,
        runtime_failure=failure,
        resource_exhaustion=exhaustion,
    )


def _hermes_resource_exhaustion(
    iteration: dict[str, Any] | None,
    task_packet: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the structured receipt from one of the four anchored Hermes forms."""
    if not (isinstance(iteration, dict) and iteration.get("iteration_exhausted")):
        return None
    snapshot_limit = (
        resource_snapshot_limit(task_packet, "hermes")
        if isinstance(task_packet, dict)
        else None
    )
    return build_resource_exhaustion(
        "hermes",
        "max_turns",
        used=iteration.get("iteration_used"),
        limit=iteration.get("iteration_limit"),
        source=iteration.get("iteration_source"),
        snapshot_limit=snapshot_limit,
    )


def _parse_output(output: str) -> Any:
    text = output.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _extract_summary(output: str) -> str:
    parsed = _parse_output(strip_callback_line(output))
    if isinstance(parsed, dict):
        for key in ("summary", "result", "message", "text"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
    return strip_callback_line(output)


def _coerce_output(output: str | bytes) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _contains_runtime_failure(stdout: str, stderr: str) -> bool:
    return _runtime_failure_details(stdout, stderr) is not None


def _runtime_failure_details(stdout: str, stderr: str) -> dict[str, Any] | None:
    failure_prefixes = (
        "failed to initialize agent:",
        "unhandled errors in a taskgroup",
        "permission denied",
        "operation not permitted",
    )
    combined = f"{stdout}\n{stderr}"
    for raw_line in combined.splitlines():
        line = raw_line.strip().lower()
        if (
            line.startswith("api call failed after")
            or "apiconnectionerror" in line
            or line.startswith("connection error")
            or line.startswith("rate limit exceeded")
            or line.startswith("quota exceeded")
        ):
            return {
                "kind": "hermes_api_transport_failure",
                "layer": "executor",
                "message": raw_line.strip(),
                "retryable": True,
            }
        if line.startswith(failure_prefixes):
            return _classify_runtime_failure(raw_line, combined)
        if line.startswith("error:") and any(
            marker in line for marker in failure_prefixes
        ):
            return _classify_runtime_failure(raw_line, combined)
    return None


def _classify_runtime_failure(raw_line: str, context: str = "") -> dict[str, Any]:
    line = f"{raw_line}\n{context}".strip().lower()
    if (
        "operation not permitted" in line
        and ("agent.log" in line or "/.hermes/" in line)
    ):
        return {
            "kind": "parent_sandbox_write_denied",
            "layer": "executor",
            "message": "Hermes cannot write its runtime log under the current parent sandbox.",
            "action": (
                "Run AgentBC from a terminal with access to Hermes home, "
                "or approve the parent runtime access request."
            ),
            "retryable": True,
        }
    return {
        "kind": "hermes_runtime_failure",
        "layer": "executor",
        "message": raw_line.strip(),
        "retryable": False,
    }


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = str(path.expanduser())
        if marker not in seen:
            seen.add(marker)
            unique.append(path.expanduser())
    return unique
