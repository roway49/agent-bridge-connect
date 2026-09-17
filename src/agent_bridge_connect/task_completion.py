from __future__ import annotations

from typing import Any

from .execution_contract import AGENT_FINAL_STATES, FINAL_CALLBACK_VERSION
from .notifications import notify_input_required, notify_terminal
from .protocol import ABCError


AGENT_COMPLETION_STATES = frozenset({*AGENT_FINAL_STATES, "needs_recovery"})


def _deliver_terminal_side_effects(
    service: Any,
    task_id: str,
    *,
    event_type: str,
    level: str,
    message: str,
) -> bool:
    """Route the terminal side effects through the durable stage receipt.

    FLOW-104-002: the receipt is the production authority for the report, record,
    index, file-notification and UI-notification stages of a task-end
    outcome.  Delivering them here records each stage on the receipt, so Runner
    maintenance replays only the unconfirmed stages instead of repeating a
    terminal notification the user has already seen.

    ``input_required`` is never routed here: the actionable nonterminal input
    notice stays owned by :func:`notify_input_required`.  A task without a
    receipt (a legacy record) returns ``False`` and keeps the historical direct
    behaviour.
    """
    from .terminal_delivery_coordinator import deliver_terminal_outcome

    outcome = deliver_terminal_outcome(
        service,
        task_id,
        event_type=event_type,
        level=level,
        message=message,
    )
    return bool(outcome.get("routed"))


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
            # FLOW-104-002: ``mark_task_needs_recovery`` already ran the report,
            # record and index stages through the durable stage split.  Re-running
            # the composed report entry point here duplicated them outside the
            # receipt.
            service.run_terminal_side_effects(task_id)
        event_type = "task.recovery_required"
        level = "warning"
    else:
        callback: dict[str, Any] = {
            "version": FINAL_CALLBACK_VERSION,
            "task_id": task_id,
            "final_state": final_state,
            "summary": clean_summary,
            "step_results": step_results,
        }
        if report_file:
            callback["report_file"] = str(report_file)
        if artifacts_dir:
            callback["artifacts_dir"] = str(artifacts_dir)
        if executor_run_id:
            callback["executor_run_id"] = str(executor_run_id)
        finalized = service.finalize_task_from_agent(task_id, callback)
        if final_state == "input_required":
            event_type = "task.input_required"
            level = "input"
        else:
            event_type = "task.finalized"
            level = "done" if final_state == "completed" else "info"

    task = service.get_task(task_id)
    notified = False
    if notify and finalized:
        if final_state == "input_required":
            # FLOW-104-002 never touches the approval flow: an ``input_required``
            # notice is not a terminal notification and keeps its own path.
            notify_input_required(service, task_id)
            notified = True
        elif _deliver_terminal_side_effects(
            service,
            task_id,
            event_type=event_type,
            level=level,
            message=clean_summary,
        ):
            notified = True
        else:
            notify_terminal(service, task_id, event_type, level, clean_summary)
            notified = True
    return {
        "ok": True,
        "task_id": task.id,
        "status": task.status,
        "event_type": event_type,
        "notified": notified,
        "report_file": (task.workspace or {}).get("report_file", ""),
    }
