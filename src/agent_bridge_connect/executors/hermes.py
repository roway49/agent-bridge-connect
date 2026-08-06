from __future__ import annotations

import json
import re
import shlex
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
    StartResult,
)
from agent_bridge_connect.execution_contract import (
    FINAL_CALLBACK_PREFIX,
    CallbackValidation,
    ExecutorTerminalResult,
    extract_callback_validation_from_output,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.media import task_image_paths
from agent_bridge_connect.protocol import resumed_input_prompt_lines, task_step_text

from .base import CLIExecutorBase
from ..path_provider import find_binary
from ..runner import RunnerClient, RunnerError

SAFETY_TIMEOUT_S = 24 * 60 * 60


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
        self.quiet = quiet
        if transport not in {"auto", "direct", "runner"}:
            raise ValueError(f"Unsupported Hermes transport: {transport}")
        self.transport = transport
        configured_command = _optional_text(command)
        self.agent_bin = (
            Path(configured_command).expanduser()
            if configured_command
            else _find_hermes_binary()
        )
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
                details={"candidates": [str(path) for path in self.COMMON_PATHS]},
            )

        runner_health, runner_error = self._probe_runner()
        if runner_health is not None:
            return ProbeResult(
                ok=True,
                message="Hermes available through AgentBC Runner",
                details={
                    "agent_bin": str(self.agent_bin),
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
                details={"agent_bin": str(self.agent_bin)},
            )

        version = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0:
            self._version = version
        return ProbeResult(
            ok=completed.returncode == 0,
            message=version or f"hermes exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
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

        if self._should_use_runner():
            return self._start_with_runner(task_packet, root)
        if self.transport == "runner":
            return StartResult(ok=False, run_id="", message="AgentBC Runner unavailable")

        run_id = f"hermes-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "hermes")
        prompt = _build_prompt(task_packet)
        command = self._build_command(prompt, images=images)

        try:
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
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"steps_total": len(steps)},
                result={
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
                },
            )
            return StartResult(ok=True, run_id=run_id, message="hermes execution needs recovery")
        except OSError as exc:
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
        )
        status = terminal.status
        self._store_run(
            run_id,
            root,
            completed.returncode,
            iteration=iteration,
        )
        self._runs[run_id] = PollResult(
            status=status,
            progress={"steps_total": len(steps), "returncode": completed.returncode},
            result={
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
                "extensions": self.get_extensions(),
            },
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
        )
        status = "cancelled" if remote_status == "cancelled" else terminal.status
        self._store_run(
            run_id,
            Path(str(remote.get("cwd") or ".")),
            returncode,
            "runner",
            iteration=iteration,
        )
        result = PollResult(
            status=status,
            progress={
                "returncode": returncode,
                "runner_status": remote_status,
                "output_truncated": bool(remote.get("output_truncated")),
            },
            result={
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
                "transport": "runner",
                "extensions": self.get_extensions(),
            },
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
        command = self._build_command(prompt, images=task_image_paths(task_packet))
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

    def _build_command(self, prompt: str, images: list[Path] | None = None) -> list[str]:
        if self.agent_bin is None:
            raise RuntimeError("hermes unavailable")
        command = [str(self.agent_bin)]
        if self.profile:
            command.extend(["-p", self.profile])
        command.append("chat")
        if images:
            command.extend(["--image", str(images[0])])
        if self.quiet:
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
            "capability_level": self.capabilities().level,
            "last_run_id": self._last_run_id,
            "profile_mode": "explicit" if self.profile else "inherit",
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "auth_owner": "hermes_cli",
            "transport": self.transport,
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


def _find_hermes_binary() -> Path | None:
    discovery = find_binary(
        "hermes",
        extra_paths=[str(path) for path in HermesExecutor.COMMON_PATHS],
    )
    if discovery["found"]:
        return Path(discovery["path"])
    return None


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _workspace_root(task_packet: dict[str, Any]) -> Path | None:
    workspace = task_packet.get("workspace") or {}
    if not isinstance(workspace, dict):
        return None
    root = workspace.get("root")
    if not root:
        return None
    return Path(str(root)).expanduser().resolve()


def _build_prompt(task_packet: dict[str, Any]) -> str:
    title = str(task_packet.get("title") or task_packet.get("task_id") or "Untitled task")
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    task_board = task_packet.get("task_board") if isinstance(task_packet.get("task_board"), dict) else {}
    board_root = str(task_board.get("root") or "")
    task_id = str(task_packet.get("task_id") or "")
    image_inputs = task_image_paths(task_packet)
    lineage = {}
    if isinstance(task_packet.get("extensions"), dict):
        lineage = (task_packet["extensions"].get("agentbc.lineage") or {}) if isinstance(task_packet["extensions"].get("agentbc.lineage"), dict) else {}
    progress_command = (
        f"agentbc task progress {shlex.quote(task_id)} --root {shlex.quote(board_root)} "
        '--summary "describe current progress"'
    )
    lines = [
        "You are executing a structured AgentBC task.",
        "",
        f"Task: {title}",
        f"Project root: {workspace.get('project_root') or workspace.get('root', '')}",
        f"Artifact root: {workspace.get('artifact_root') or workspace.get('artifacts_dir', '')}",
        f"Report directory: {workspace.get('report_root') or workspace.get('output_dir', '')}",
        f"Task brief: {workspace.get('task_file', '')}",
        f"Report: {workspace.get('report_file', '')}",
        "",
        "Steps:",
    ]
    if image_inputs:
        lines.extend(
            [
                "",
                "An image input is attached through the native Hermes CLI image interface:",
                f"- {image_inputs[0]}",
                "Inspect that image as a task input. Do not copy it merely to make it accessible.",
            ]
        )
    resume_context = resumed_input_prompt_lines(task_packet)
    if resume_context:
        lines.extend(["", *resume_context, ""])
    for index, step in enumerate(task_packet.get("steps") or [], 1):
        lines.append(f"{index}. {task_step_text(step)} [status: {step.get('status', 'pending')}]")
        lines.extend(
            [
                "",
                "Write user deliverables only under the Artifact root named above. Never write deliverables directly in the AgentBC workspace root, report directory, or record directory.",
                "If customer_dir is true, edit the existing project in place and do not copy it into the AgentBC workspace.",
                "If any path is rejected as outside allowed roots, stop and report the configuration problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection.",
                "If this task continues an existing deliverable, modify the existing baseline instead of creating a sibling project directory.",
                "For image generation or image editing work, use the native image_generate capability and save the final bitmap deliverables under the Artifact root; do not return only prose or preview links.",
                "AgentBC Core owns the execution report. Do not write or replace REPORT.md.",
                "Return a concise execution summary and mention any files changed.",
                "For long-running work, refresh AgentBC progress at least every few minutes:",
                progress_command,
            ]
        )
    if lineage:
        lines.extend(
            [
                "",
                f"Iteration chain root: {lineage.get('chain_root_task_id', '')}",
                f"Base task: {lineage.get('base_task_id', '')}",
                f"Task code: {lineage.get('task_code', workspace.get('task_code', ''))}",
                f"Iteration: {lineage.get('iteration_index', workspace.get('iteration', ''))}",
                f"Base artifact root: {lineage.get('base_artifacts_dir', workspace.get('artifacts_dir', ''))}",
            ]
        )
    step_results = ",".join(
        f'{{"id":{step.get("id", index)},"status":"done"}}'
        for index, step in enumerate(task_packet.get("steps") or [], 1)
    )
    lines.extend(
        [
            "",
            "Your final response must end with exactly one single-line terminal marker and no text after it:",
            (
                f'{FINAL_CALLBACK_PREFIX} {{"version":1,"task_id":{json.dumps(task_id)},'
                f'"final_state":"completed","summary":"concise summary",'
                f'"step_results":[{step_results}]}}'
            ),
            "Use final_state input_required only with at least one declared step status blocked; plain permission or approval prose is not a valid stop.",
            "A zero CLI exit without a valid marker fails the task. completed means flow execution ended, not user acceptance or quality approval.",
        ]
    )
    return "\n".join(lines)


def _extract_final_response(stdout: str, task_packet: dict[str, Any]) -> str:
    """Return the actual Hermes assistant response from raw CLI output.

    Hermes single-query mode prefixes the output with ``Query: <prompt>`` in
    its human-facing path, and models may repeat the task prompt verbatim
    ahead of their real answer. The prompt embeds the example final marker, so
    validating the raw stdout would count that echoed example plus the real
    marker as a duplicate and fail. Strip the known leading Query/task-prompt
    echo so marker validation inspects the actual final response only; a
    genuinely duplicated marker inside that response still fails as
    ``completion_marker_duplicate``.
    """
    output = (stdout or "").strip()
    if not output:
        return output
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
) -> ExecutorTerminalResult:
    """Route the Hermes terminal with iteration-budget classification.

    A valid completed/input_required marker routes to its declared state
    normally and a retryable transport/runtime failure keeps ``needs_recovery``.
    Budget exhaustion without a valid callback is classified as
    ``iteration_budget_exhausted`` instead of the generic missing/invalid
    marker failure, preserving zero-exit strictness.
    """
    terminal = route_executor_terminal(
        validation,
        returncode,
        executor_name="hermes",
        stderr=stderr,
        runtime_failure=failure,
    )
    if (
        iteration.get("iteration_exhausted")
        and terminal.callback is None
        and not (terminal.failure or {}).get("retryable")
    ):
        used = iteration.get("iteration_used")
        limit = iteration.get("iteration_limit")
        ratio = f"{used}/{limit}" if used is not None and limit is not None else "unknown"
        return ExecutorTerminalResult(
            status="failed",
            callback=None,
            failure={
                "kind": "iteration_budget_exhausted",
                "layer": "flow_contract",
                "message": (
                    f"Hermes iteration budget exhausted ({ratio}) before a "
                    "valid final marker was emitted"
                ),
                "retryable": False,
            },
        )
    return terminal


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
