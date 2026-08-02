from __future__ import annotations

import subprocess
import time
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
from agent_bridge_connect.execution_contract import build_agent_callback

from .base import CLIExecutorBase


class ShellExecutor(CLIExecutorBase):
    """L0 shell executor: runs shell commands as steps."""

    def __init__(self) -> None:
        super().__init__()

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, message="shell ready", details={})

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            structured_output=True,
            streaming_events=False,
            resume=False,
            cancel=False,
            input_required=False,
            model_selection=False,
            multimodal=False,
            parallelism=1,
            level=ExecutorLevel.L0,
        )

    def start(self, task_packet: dict) -> StartResult:
        steps = task_packet.get("steps", [])
        if not steps:
            return StartResult(ok=False, run_id=None, message="no steps")

        workspace = task_packet.get("workspace", {})
        root = Path(workspace.get("root", ".")).expanduser().resolve()
        if not root.is_dir():
            return StartResult(ok=False, run_id=None, message=f"workspace not found: {root}")

        run_id = f"shell-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._start_run_lease(task_packet, run_id, "shell")
        results: list[dict[str, Any]] = []
        status = "completed"

        for step in steps:
            self._heartbeat_run(run_id)
            command = step.get("command") or step.get("description")
            if not command:
                results.append(
                    {
                        "id": step.get("id"),
                        "command": "",
                        "returncode": None,
                        "stdout": "",
                        "stderr": "missing command",
                        "duration_s": 0.0,
                    }
                )
                status = "failed"
                break

            started = time.monotonic()
            try:
                completed = subprocess.run(
                    str(command),
                    cwd=root,
                    shell=True,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            except OSError as exc:
                results.append(
                    {
                        "id": step.get("id"),
                        "command": str(command),
                        "returncode": None,
                        "stdout": "",
                        "stderr": str(exc),
                        "duration_s": time.monotonic() - started,
                    }
                )
                status = "failed"
                break
            self._heartbeat_run(run_id)
            results.append(
                {
                    "id": step.get("id"),
                    "command": str(command),
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "duration_s": time.monotonic() - started,
                }
            )
            if completed.returncode != 0:
                status = "failed"
                break

        self._runs[run_id] = PollResult(
            status="completed" if status == "completed" else "needs_recovery",
            progress={"steps_done": len(results), "steps_total": len(steps)},
            result={
                "steps": results,
                "summary": (
                    "shell executor completed all steps"
                    if status == "completed"
                    else "shell executor encountered a command failure"
                ),
                "agent_callback": (
                    build_agent_callback(
                        task_packet,
                        "completed",
                        "shell executor completed all steps",
                        run_id,
                        callback={
                            "version": 1,
                            "task_id": str(task_packet.get("task_id") or ""),
                            "final_state": "completed",
                            "summary": "shell executor completed all steps",
                            "step_results": [
                                {"id": step.get("id", index), "status": "done"}
                                for index, step in enumerate(steps, 1)
                            ],
                        },
                    )
                    if status == "completed"
                    else None
                ),
                "failure": (
                    None
                    if status == "completed"
                    else {
                        "kind": "shell_command_failed",
                        "layer": "executor",
                        "message": results[-1]["stderr"] if results else "shell command failed",
                        "retryable": False,
                    }
                ),
            },
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message="started")
