from __future__ import annotations

from agent_bridge_connect.adapters import (
    ExecutorCapabilities,
    ExecutorLevel,
    ExecutorPort,
    PollResult,
    ProbeResult,
    StartResult,
)
from agent_bridge_connect.execution_contract import build_agent_callback


class MockExecutor(ExecutorPort):
    """L0 mock executor: always succeeds and runs instantly."""

    def __init__(self) -> None:
        self._packets: dict[str, dict] = {}

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, message="mock ready", details={})

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
        run_id = f"mock-{task_packet.get('task_id', 'unknown')}-{len(steps)}"
        self._packets[run_id] = dict(task_packet)
        return StartResult(ok=True, run_id=run_id, message="started")

    def poll(self, run_id: str) -> PollResult:
        packet = self._packets.get(run_id, {"task_id": "unknown", "workspace": {}})
        return PollResult(
            status="completed",
            progress={"steps_done": 999},
            result={
                "summary": "mock executor completed all steps",
                "agent_callback": build_agent_callback(
                    packet,
                    "completed",
                    "mock executor completed all steps",
                    run_id,
                    callback={
                        "version": 1,
                        "task_id": str(packet.get("task_id") or ""),
                        "final_state": "completed",
                        "summary": "mock executor completed all steps",
                        "step_results": [
                            {"id": step.get("id", index), "status": "done"}
                            for index, step in enumerate(packet.get("steps") or [], 1)
                        ],
                    },
                ),
            },
        )
