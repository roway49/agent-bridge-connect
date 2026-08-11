from __future__ import annotations

import json
import re
import subprocess
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
from agent_bridge_connect.execution_contract import (
    CallbackValidation,
    ExecutorTerminalResult,
    build_resource_exhaustion,
    extract_callback_validation_from_output,
    resource_snapshot_limit,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.execution_policy import extract_hermes_session_id
from agent_bridge_connect.media import task_image_paths
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import PromptPlatformExtras, build_prompt_contract

from .base import CLIExecutorBase
from ..path_provider import find_binary
from ..runner import RunnerClient, RunnerError

SAFETY_TIMEOUT_S = 24 * 60 * 60
HERMES_CLEANUP_UNSUPPORTED_CODE = "hermes_session_delete_unavailable"
HERMES_SESSION_DELETE_FAILED_CODE = "hermes_session_delete_failed"
HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE = "hermes_session_delete_missing_session_id"
HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE = "hermes_session_delete_invalid_session_id"
_HERMES_FROZEN_HELP_FIXTURE = "hermes_0.17.0_help.txt"
_HERMES_FROZEN_VERSION = "0.17.0"
_HERMES_CLEANUP_TIMEOUT_S = 60
_HERMES_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-fA-F]{6,32}$")
_HERMES_SESSION_ABSENT_RE = re.compile(
    r"(?im)^session.*(?:not found|does not exist)"
)
_HERMES_INITIALIZING_LINE_RE = re.compile(
    r"(?m)^[ \t]*Initializing agent\.\.\.[ \t]*\r?$"
)


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
    ) -> None:
        super().__init__()
        self.timeout_s = timeout_s
        self.profile = _optional_text(profile)
        self.provider = _optional_text(provider)
        self.model = _optional_text(model)
        self.max_turns = _validate_max_turns(max_turns)
        self.quiet = quiet
        if transport not in {"auto", "direct", "runner"}:
            raise ValueError(f"Unsupported Hermes transport: {transport}")
        self.transport = transport
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
            cancel=self.transport != "direct",
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
        permission = permission_record_from_extensions(task_packet.get("extensions"))
        try:
            assert_executor_permission_supported(
                "hermes", permission["effective_mode"], self.agent_bin
            )
        except ABCError as exc:
            return StartResult(ok=False, run_id="", message=f"{exc.code}: {exc}")

        if self._should_use_runner():
            return self._start_with_runner(task_packet, root)
        if self.transport == "runner":
            return StartResult(ok=False, run_id="", message="AgentBC Runner unavailable")

        run_id = f"hermes-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
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
                self._runner_client.authorize_command("hermes", command, root or Path.cwd(), task_packet)
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
    ) -> StartResult:
        prompt = _build_prompt(task_packet)
        permission = permission_record_from_extensions(task_packet.get("extensions"))
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
            remote = self._runner_client.submit("hermes", command, root or Path.cwd(), task=task_packet)
        except RunnerError as exc:
            return StartResult(ok=False, run_id="", message=f"Runner submit failed: {exc}")
        run_id = str(remote["run_id"])
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
        # AgentBC task runs require Hermes' machine-readable single-query path:
        # it emits the authoritative ``session_id:`` receipt on stderr.
        if self.quiet or _task_has_session_policy(task_packet):
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
            "transport": self.transport,
            "permission": (
                permission_record_from_extensions(
                    self._task_packets.get(self._last_run_id, {}).get("extensions")
                )
                if self._last_run_id is not None
                else None
            ),
        }
        if self._last_run_id is not None:
            last_run = self._run_metadata[self._last_run_id]
            metadata["last_run"] = last_run
            if isinstance(last_run.get("iteration"), dict):
                metadata["iteration"] = last_run["iteration"]
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


def _hermes_cleanup_request_error(request: SessionCleanupRequest) -> str:
    if str(request.executor or "").strip().lower() != "hermes":
        return "hermes_cleanup_executor_mismatch"
    if request.retain is not False or request.project_mode != "none":
        return "hermes_cleanup_mode_invalid"
    if request.strategy != "official_session_delete":
        return "hermes_cleanup_strategy_mismatch"
    session_id = str(request.session_id or "").strip()
    if not session_id:
        return HERMES_SESSION_DELETE_MISSING_SESSION_ID_CODE
    if _HERMES_SESSION_ID_RE.fullmatch(session_id) is None:
        return HERMES_SESSION_DELETE_INVALID_SESSION_ID_CODE
    return ""


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

    A valid completed/input_required marker routes to its declared state
    normally and a retryable transport/runtime failure keeps ``needs_recovery``.
    Confirmed budget exhaustion without a valid callback is classified through
    the shared resource-exhaustion contract: it becomes a system
    ``input_required`` wait with ``failure.kind=resource_limit_exhausted``, and
    a receipt limit that conflicts with the task snapshot fails closed to
    ``needs_recovery``.
    """
    return route_executor_terminal(
        validation,
        returncode,
        executor_name="hermes",
        stderr=stderr,
        runtime_failure=failure,
        resource_exhaustion=_hermes_resource_exhaustion(iteration, task_packet),
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
