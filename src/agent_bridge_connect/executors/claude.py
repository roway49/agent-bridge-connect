from __future__ import annotations

import json
import math
import re
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
    CallbackValidation,
    build_resource_exhaustion,
    detect_retryable_transport_failure,
    extract_callback_validation_from_output,
    resource_snapshot_limit,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.path_model import validate_path_plan_workspace
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import PromptPlatformExtras, build_prompt_contract
from agent_bridge_connect.runner import RunnerClient, RunnerError

from .base import CLIExecutorBase
from ..path_provider import find_binary

SAFETY_TIMEOUT_S = 24 * 60 * 60

_CLAUDE_BUDGET_ERROR_RE = re.compile(r"Exceeded\s+USD\s+budget")
_CLAUDE_BUDGET_AMOUNT_RE = re.compile(
    r"Exceeded\s+USD\s+budget\s*\(\s*\$?\s*(?P<amount>\d+(?:\.\d+)?)"
)


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
        permission = permission_record_from_extensions(task_packet.get("extensions"))
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
                    "claude", command, execution_root, task_packet
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
    structured_limit = _claude_structured_budget_limit(parsed_output)
    if structured_limit is not None:
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
    error_output = f"{stderr}\n{stdout}"
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


def _claude_structured_budget_limit(parsed_output: Any) -> int | float | None:
    """Recursively find the ``error_max_budget_usd`` subtype and its limit."""
    if isinstance(parsed_output, dict):
        if (
            str(parsed_output.get("subtype") or "").strip() == "error_max_budget_usd"
            or str(parsed_output.get("type") or "").strip() == "error_max_budget_usd"
        ):
            amount = _claude_budget_amount_from_text(
                str(parsed_output.get("message") or "")
            )
            if amount is not None:
                return amount
            for field in ("budget", "max_budget_usd", "limit", "amount"):
                value = parsed_output.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
        for value in parsed_output.values():
            found = _claude_structured_budget_limit(value)
            if found is not None:
                return found
    elif isinstance(parsed_output, list):
        for item in parsed_output:
            found = _claude_structured_budget_limit(item)
            if found is not None:
                return found
    return None


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
