from __future__ import annotations

from typing import Any

from .execution_contract import AGENT_FINAL_STATES
from .notifications import notify_terminal
from .protocol import ABCError
from .reports import write_report_files


AGENT_COMPLETION_STATES = frozenset({*AGENT_FINAL_STATES, "needs_recovery"})


def apply_agent_completion(
    service: Any,
    task_id: str,
    *,
    state: str,
    summary: str,
    report_file: str | None = None,
    artifacts_dir: str | None = None,
    executor_run_id: str | None = None,
    step_results: Any = None,
    recovery_code: str = "agent_reported_recovery",
    notify: bool = True,
) -> dict[str, Any]:
    final_state = str(state or "").strip()
    clean_summary = str(summary or "").strip()
    if final_state not in AGENT_COMPLETION_STATES:
        raise ABCError("invalid_agent_callback", f"Unsupported completion state: {final_state}")
    if not clean_summary:
        raise ABCError("invalid_agent_callback", "Agent completion summary is required")

    if final_state == "needs_recovery":
        finalized = service.mark_task_needs_recovery(
            task_id,
            recovery_code,
            clean_summary,
            {"source": "agent_callback", "executor_run_id": str(executor_run_id or "")},
        )
        if finalized:
            write_report_files(task_id, service.board_root)
        event_type = "task.recovery_required"
        level = "warning"
    else:
        callback: dict[str, Any] = {
            "task_id": task_id,
            "final_state": final_state,
            "summary": clean_summary,
        }
        if report_file:
            callback["report_file"] = str(report_file)
        if artifacts_dir:
            callback["artifacts_dir"] = str(artifacts_dir)
        if executor_run_id:
            callback["executor_run_id"] = str(executor_run_id)
        if step_results is not None:
            callback["step_results"] = step_results
        finalized = service.finalize_task_from_agent(task_id, callback)
        event_type = "task.finalized"
        level = "done" if final_state == "completed" else "info"

    task = service.get_task(task_id)
    if notify and finalized:
        notify_terminal(service, task_id, event_type, level, clean_summary)
    return {
        "ok": True,
        "task_id": task.id,
        "status": task.status,
        "event_type": event_type,
        "notified": bool(notify and finalized),
        "report_file": (task.workspace or {}).get("report_file", ""),
    }
