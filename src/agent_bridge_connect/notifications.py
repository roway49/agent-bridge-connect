from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from .notifiers.dialog import DialogNotifier
from .notifiers.file import FileNotifier
from .reports import redact_secrets
from .terminal_states import TASK_TERMINAL_STATES, terminal_status_label


def notify_terminal(
    service: Any,
    task_id: str,
    event_type: str,
    level: str,
    message: str,
) -> None:
    payload = build_notification_payload(service, task_id, event_type, level, message)
    file_result = FileNotifier(service.board_root / "notifications.jsonl").send(payload)
    delay_s = 0
    # Every terminal result must reach the user. Concurrency changes only the
    # delivery timing; suppressing a completed dialog loses it permanently.
    delay_s = notification_delay_seconds(service, task_id)
    if delay_s > 0:
        time.sleep(delay_s)
    dialog_result = DialogNotifier().send(payload)
    service.store.append_event(
        task_id,
        {
            "event_type": "notification_delivery",
            "task_id": task_id,
            "notification_event": event_type,
            "file_ok": file_result.ok,
            "dialog_ok": dialog_result.ok,
            "dialog_message": dialog_result.message,
            "dialog_delay_s": delay_s,
            "created_at": utc_now(),
        },
    )


InputResponder = Callable[[str, str, str], dict[str, Any]]


def notify_input_required(
    service: Any,
    task_id: str,
    *,
    responder: InputResponder | None = None,
) -> dict[str, Any]:
    """Immediately deliver an actionable, explicitly nonterminal input notice."""
    payload = build_input_required_notification(service, task_id)
    file_result = FileNotifier(service.board_root / "notifications.jsonl").send(payload)
    dialog_result = DialogNotifier().send(payload)
    action = str(dialog_result.details.get("action") or "dismissed")
    service.store.append_event(
        task_id,
        {
            "event_type": "notification_delivery",
            "task_id": task_id,
            "notification_event": "task.input_required",
            "terminal": False,
            "file_ok": file_result.ok,
            "dialog_ok": dialog_result.ok,
            "dialog_message": dialog_result.message,
            "dialog_action": action,
            "dialog_delay_s": 0,
            "created_at": utc_now(),
        },
    )
    response_result: dict[str, Any] = {}
    response_error = ""
    if responder is not None and action in {"message", "approve", "deny"}:
        try:
            response_result = responder(
                str(payload["input_id"]),
                action,
                str(dialog_result.details.get("message") or ""),
            )
        except Exception as exc:
            response_error = compact_notification_text(str(redact_secrets(str(exc))), 240)
            DialogNotifier().send(
                {
                    "task_id": task_id,
                    "event_type": "task.input_response_failed",
                    "title": "Agent-Bridge-Connect",
                    "level": "warning",
                    "message": (
                        f"Task: {task_id} remains waiting for input\n"
                        f"Response failed: {response_error}\n"
                        f"Fallback: {payload['respond_command']}"
                    ),
                    "report_path": str(payload.get("report_path") or ""),
                }
            )
        service.store.append_event(
            task_id,
            {
                "event_type": "task.input_dialog_response",
                "task_id": task_id,
                "input_id": str(payload["input_id"]),
                "response_type": action,
                "response_status": str(response_result.get("status") or ""),
                "response_task_id": str(response_result.get("task_id") or ""),
                "same_task": bool(response_result.get("same_task", False)),
                "response_error": response_error,
                "created_at": utc_now(),
            },
        )
    return {
        "dialog_action": action,
        "response": response_result,
        "response_error": response_error,
    }


def build_input_required_notification(service: Any, task_id: str) -> dict[str, Any]:
    task = service.get_task(task_id)
    request = (task.extensions or {}).get("agentbc.input")
    if not isinstance(request, dict) or request.get("status") != "waiting":
        raise ValueError(f"Task {task_id} has no waiting input request")
    input_id = str(request.get("input_id") or "")
    if not input_id:
        raise ValueError(f"Task {task_id} input request has no response ID")
    input_type = str(request.get("type") or "message").strip().lower()
    input_options = (
        [str(option).strip() for option in request.get("options", []) if str(option).strip()]
        if input_type == "choice" and isinstance(request.get("options"), list)
        else []
    )
    option_descriptions = (
        [
            compact_notification_text(str(description).strip(), 160)
            for description in request.get("option_descriptions", [])
            if str(description).strip()
        ]
        if input_type == "choice" and isinstance(request.get("option_descriptions"), list)
        else []
    )
    if len(option_descriptions) != len(input_options):
        option_descriptions = []
    if input_type == "permission":
        command = (
            f"agentbc task respond {task_id} --input {input_id} --approve"
            f" (or --deny)"
        )
    else:
        command = f"agentbc task respond {task_id} --input {input_id} --message \"<response>\""
    workspace = task.workspace or {}
    blocked_step = request.get("blocked_step_id", "")
    summary = compact_notification_text(str(request.get("summary") or ""), 240)
    if input_type == "choice":
        reason = compact_notification_text(str(request.get("reason") or summary), 240)
        body_lines = [
            f"Task: {task_id} needs your decision",
            f"Blocked step: {blocked_step}",
            "Why this is blocked:",
            reason,
            "Choices:",
        ]
        if option_descriptions:
            body_lines.extend(
                f"• {label} — {description}"
                for label, description in zip(input_options, option_descriptions)
            )
        else:
            body_lines.extend(f"• {label}" for label in input_options)
        body_lines.append("Choose one below to resume this same task.")
    else:
        body_lines = [
            f"Task: {task_id} needs your input",
            f"Blocked step: {blocked_step}",
            "Why this is blocked:",
            summary,
        ]
        if input_type == "permission" and request.get("requested_permission"):
            body_lines.extend(
                [
                    "Requested access:",
                    compact_notification_text(str(request.get("requested_permission") or ""), 180),
                    "Choose Approve or Deny below.",
                ]
            )
        else:
            body_lines.append("Enter your response below to resume this same task.")
    body = "\n".join(body_lines)
    return {
        "task_id": task_id,
        "event_type": "task.input_required",
        "title": "Agent-Bridge-Connect",
        "level": "input",
        "message": body,
        "report_path": str(workspace.get("report_file") or ""),
        "respond_command": command,
        "deadline_at": str(request.get("deadline_at") or ""),
        "input_id": input_id,
        "input_type": input_type,
        "input_reason": str(request.get("reason") or ""),
        "input_options": input_options,
        "input_option_descriptions": option_descriptions,
    }


def build_notification_payload(
    service: Any,
    task_id: str,
    event_type: str,
    level: str,
    message: str,
) -> dict[str, str]:
    task = service.get_task(task_id)
    workspace = task.workspace or {}
    extensions = task.extensions or {}
    provenance = extensions.get("agentbc.provenance") if isinstance(extensions.get("agentbc.provenance"), dict) else {}
    source_platform = str(provenance.get("source_platform") or "").strip()
    dispatcher = source_platform if source_platform and source_platform != "unknown" else str(task.created_by or "unknown")
    executor = str(task.assignee or "unknown")
    report_path = str(workspace.get("report_file") or "")
    status = terminal_status_label(task.status) or str(task.status or level or "unknown")
    body = "\n".join(
        [
            f"Task: {task_id} {status}",
            f"Title: {compact_notification_text(task.title, 96)}",
            f"Dispatcher/Executor: {dispatcher} -> {executor}",
            f"Duration: {format_elapsed(task.created_at, task.updated_at)}",
            f"Report: {report_path}",
        ]
    )
    return {
        "task_id": task_id,
        "event_type": event_type,
        "title": "Agent-Bridge-Connect",
        "level": level,
        "message": body,
        "report_path": report_path,
    }


def should_show_dialog_notification(service: Any, task_id: str, level: str) -> bool:
    """Retained for CLI compatibility; terminal dialogs are never dropped."""
    return True


def notification_delay_seconds(service: Any, task_id: str, threshold_s: int = 10) -> int:
    current = service.get_task(task_id)
    current_at = task_terminal_timestamp(current)
    if current_at is None:
        return 0
    recent_at: datetime | None = None
    for task in service.list_tasks():
        if task.id == task_id:
            continue
        if task.status not in TASK_TERMINAL_STATES:
            continue
        completed_at = task_terminal_timestamp(task)
        if completed_at is None or completed_at > current_at:
            continue
        if recent_at is None or completed_at > recent_at:
            recent_at = completed_at
    if recent_at is None:
        return 0
    gap_s = (current_at - recent_at).total_seconds()
    return threshold_s if gap_s <= threshold_s else 0


def task_terminal_timestamp(task: Any) -> datetime | None:
    extensions = getattr(task, "extensions", None) or {}
    callback = extensions.get("agentbc.final_callback")
    if isinstance(callback, dict):
        finished_at = parse_timestamp(str(callback.get("finished_at") or ""))
        if finished_at is not None:
            return finished_at
    return parse_timestamp(str(getattr(task, "updated_at", "") or ""))


def compact_notification_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def format_elapsed(start: str, end: str) -> str:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    if start_dt is None or end_dt is None:
        return "unknown"
    seconds = max(int(round((end_dt - start_dt).total_seconds())), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
