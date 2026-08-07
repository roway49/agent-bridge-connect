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
from agent_bridge_connect.protocol import ABCError, resumed_input_prompt_lines, task_step_text
from agent_bridge_connect.runner import RunnerClient, RunnerError

from .base import CLIExecutorBase
from ..path_provider import find_binary

SAFETY_TIMEOUT_S = 24 * 60 * 60


class CodexExecutor(CLIExecutorBase):
    """L2 Codex CLI adapter using blocking JSONL execution."""

    def __init__(self, timeout_s: int = SAFETY_TIMEOUT_S, command: str | None = None) -> None:
        super().__init__()
        self.timeout_s = timeout_s
        self._discovery = _discover_codex_binary(command)
        resolved = str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}

    def probe(self) -> ProbeResult:
        if self.agent_bin is None:
            return ProbeResult(
                ok=False,
                message="codex unavailable",
                details={
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
                details={"agent_bin": str(self.agent_bin)},
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
            input_required=False,
            model_selection=True,
            multimodal=True,
            image_input=True,
            image_generation=True,
            image_editing=True,
            parallelism=1,
            level=ExecutorLevel.L2,
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

        run_id = f"codex-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "codex")
        prompt = _build_prompt(task_packet)
        permission = permission_record_from_extensions(task_packet.get("extensions"))
        try:
            assert_executor_permission_supported(
                "codex", permission["effective_mode"], self.agent_bin
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=str(exc))
        command, prompt_input = self._build_command(
            task_packet,
            prompt,
            root,
            permission,
        )

        try:
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command("codex", command, root, task_packet)
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
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"events_seen": len(events)},
                result={
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
                },
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
        self._store_metadata(run_id, root, events, returncode=completed.returncode)
        result["extensions"] = self.get_extensions()
        self._runs[run_id] = PollResult(
            status=status,
            progress={"events_seen": len(events)},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"codex execution {status}")

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
        command = [str(self.agent_bin), "exec", "--json"]
        command.extend(permission_flags("codex", selected["effective_mode"]))
        command.append("--skip-git-repo-check")
        if selected["effective_mode"] == "safe":
            for writable_root in _codex_writable_roots(task_packet, root):
                command.extend(["--add-dir", str(writable_root)])
        images = task_image_paths(task_packet)
        prompt_input: str | None = None
        if images:
            command.append("-")
            command.append("--image")
            command.extend(str(image) for image in images)
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


def _discover_codex_binary(command: str | None) -> dict[str, Any]:
    configured = command.strip() if isinstance(command, str) else ""
    return find_binary("codex", extra_paths=[configured] if configured else None)


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
        "You are executing a structured task.",
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
                "Image inputs are attached through the native Codex CLI image interface:",
                *[f"- {image}" for image in image_inputs],
                "Inspect those images as task inputs. Do not copy them merely to make them accessible.",
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
                "For image generation or image editing work, use the native image-generation capability and save the final bitmap deliverables under the Artifact root; do not return only prose or preview links.",
                "AgentBC Core owns the execution report. Do not write or replace REPORT.md.",
                "After completing all steps, write a summary of what you did.",
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
            'For a two-option user decision, include "input":{"type":"choice","options":["Option A","Option B"]}; the two distinct option labels must each be 48 characters or fewer. Use type message for free text and type permission only for approve/deny.',
            "A zero CLI exit without a valid marker fails the task. completed means flow execution ended, not user acceptance or quality approval.",
        ]
    )
    return "\n".join(lines)


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


def _extract_summary(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        payload = event.get("payload") or {}
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return strip_callback_line(str(item.get("text") or ""))
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            return strip_callback_line(str(payload.get("text") or ""))
    return ""
