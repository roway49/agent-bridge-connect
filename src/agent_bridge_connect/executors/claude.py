from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agent_bridge_connect.adapters import (
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    StartResult,
)
from agent_bridge_connect.execution_contract import (
    FINAL_CALLBACK_PREFIX,
    detect_retryable_transport_failure,
    extract_callback_validation_from_output,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.protocol import ABCError, resumed_input_prompt_lines, task_step_text
from agent_bridge_connect.runner import RunnerClient, RunnerError

from .base import CLIExecutorBase
from ..path_provider import find_binary

SAFETY_TIMEOUT_S = 24 * 60 * 60


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
        max_budget_usd: float | None = 1.0,
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
        self.agent_bin = Path(configured_command).expanduser() if configured_command else _find_claude_binary()
        self._version = ""
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}

    def probe(self) -> ProbeResult:
        if self.agent_bin is None:
            return ProbeResult(
                ok=False,
                message="claude unavailable",
                details={"candidates": [str(path) for path in self.COMMON_PATHS]},
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
                details={"agent_bin": str(self.agent_bin)},
            )
        version = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0:
            self._version = version
        return ProbeResult(
            ok=completed.returncode == 0,
            message=version or f"claude exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
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
            resume=False,
            cancel=False,
            input_required=False,
            model_selection=True,
            multimodal=True,
            parallelism=1,
            level=ExecutorLevel.L1,
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

        run_id = f"claude-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "claude")
        prompt = _build_prompt(task_packet)
        permission = permission_record_from_extensions(task_packet.get("extensions"))
        try:
            assert_executor_permission_supported(
                "claude", permission["effective_mode"], self.agent_bin
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=str(exc))
        command = self._build_command(prompt, root, task_packet, permission)

        try:
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command("claude", command, root, task_packet)
            self._heartbeat_run(run_id)
            completed = subprocess.run(
                command,
                input=prompt,
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_output(exc.stdout or "")
            stderr = _coerce_output(exc.stderr or "")
            self._store_run(run_id, root, None)
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
        )
        status = terminal.status
        self._store_run(run_id, root, completed.returncode)
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
            "extensions": self.get_extensions(),
        }
        self._runs[run_id] = PollResult(
            status=status,
            progress={"steps_total": len(steps), "callback_seen": terminal.callback is not None},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"claude execution {status}")

    def get_extensions(self) -> dict:
        task_permission = None
        if self._last_run_id is not None:
            task_permission = permission_record_from_extensions(
                self._task_packets.get(self._last_run_id, {}).get("extensions")
            )
        metadata: dict[str, Any] = {
            "agent_bin": str(self.agent_bin) if self.agent_bin is not None else "",
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
            "resume": False,
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
        command.append("--no-session-persistence")
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
        if self.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(self.max_budget_usd)])
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


def _build_prompt(task_packet: dict[str, Any]) -> str:
    title = str(task_packet.get("title") or task_packet.get("task_id") or "Untitled task")
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    task_board = task_packet.get("task_board") if isinstance(task_packet.get("task_board"), dict) else {}
    board_root = str(task_board.get("root") or "")
    task_id = str(task_packet.get("task_id") or "")
    lineage = {}
    if isinstance(task_packet.get("extensions"), dict):
        value = task_packet["extensions"].get("agentbc.lineage")
        lineage = value if isinstance(value, dict) else {}
    progress_command = (
        f"agentbc task progress {shlex.quote(task_id)} --root {shlex.quote(board_root)} "
        '--summary "describe current progress"'
    )
    lines = [
        "You are executing a structured AgentBC task with Claude Code.",
        "",
        f"Task ID: {task_packet.get('task_id', '')}",
        f"Task: {title}",
        f"Project root: {workspace.get('project_root') or workspace.get('root', '')}",
        f"Artifact root: {workspace.get('artifact_root') or workspace.get('artifacts_dir', '')}",
        f"Report directory: {workspace.get('report_root') or workspace.get('output_dir', '')}",
        f"Task brief: {workspace.get('task_file', '')}",
        f"Report: {workspace.get('report_file', '')}",
        "",
        "Rules:",
        "- Write user deliverables only under the Artifact root named above. Never write deliverables directly in the AgentBC workspace root, report directory, or record directory.",
        "- If customer_dir is true, edit the existing project in place and do not copy it into the AgentBC workspace.",
        "- If any path is rejected as outside allowed roots, stop and report the configuration problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection.",
        "- If this task continues an existing deliverable, modify the existing baseline instead of creating a sibling project directory.",
        "- AgentBC Core owns the execution report. Do not write or replace REPORT.md.",
        "- Do not claim user acceptance. completed only means your agent turn is finished and ready for user review.",
        "- Do not create Claude-internal tasks/todos. The AgentBC task record and report are the only execution ledger.",
        "- If the step asks another agent to execute or review work, use the AgentBC CLI handoff/dispatch command instead of doing that agent's work inline.",
        "- Keep required long-running commands in the foreground with a tool timeout longer than the expected runtime.",
        "- If Claude Code moves a command to the background, use BashOutput repeatedly until it exits. Never end this turn while a required background command is still running.",
        "",
        "Steps:",
    ]
    resume_context = resumed_input_prompt_lines(task_packet)
    if resume_context:
        lines.extend(["", *resume_context, ""])
    for index, step in enumerate(task_packet.get("steps") or [], 1):
        lines.append(f"{index}. {task_step_text(step)} [status: {step.get('status', 'pending')}]")
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
    lines.extend(
        [
            "",
            "After completing all steps, print a concise summary.",
            "For long-running work, refresh AgentBC progress at least every few minutes:",
            progress_command,
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
            'For a two-option user decision, include "input":{"type":"choice","reason":"why the user must decide","options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B","description":"what B does or changes"}]}; give a concrete reason and a concrete description for each option. Labels must be distinct and at most 48 characters; descriptions must be at most 160 characters. Use type message for free text and type permission only for approve/deny.',
            "A zero CLI exit without a valid marker fails the task. completed means flow execution ended, not user acceptance or quality approval.",
        ]
    )
    return "\n".join(lines)


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


def _find_claude_binary() -> Path | None:
    result = find_binary("claude", extra_paths=[str(path) for path in ClaudeExecutor.COMMON_PATHS])
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
