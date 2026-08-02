"""Helpers for constructing valid AgentBC terminal flow declarations in tests."""

from __future__ import annotations

from typing import Any


def completed_callback(task: Any, summary: str = "test flow completed", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "task_id": task.id,
        "final_state": "completed",
        "summary": summary,
        "step_results": [
            {"id": int(step["id"]), "status": "done"}
            for step in task.steps
        ],
    }
    payload.update(extra)
    return payload


def finalize_completed(service: Any, task_id: str, summary: str = "test flow completed") -> bool:
    task = service.get_task(task_id)
    return service.finalize_task_from_agent(
        task.id,
        completed_callback(task, summary=summary),
    )
