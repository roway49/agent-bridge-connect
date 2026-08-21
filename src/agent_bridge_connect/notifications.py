from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from .approval import (
    APPROVAL_EXTENSION_KEY,
    APPROVAL_SCOPE,
    validate_approval_receipt,
)
from .protocol import ABCError
from .execution_policy import execution_policy_view
from .notifiers.dialog import DialogNotifier
from .notifiers.file import FileNotifier
from .reports import redact_secrets
from .terminal_states import TASK_TERMINAL_STATES, terminal_status_label
from .timing_view import build_timing_view

RESOURCE_DECISION_APPROVE_LABEL = "提高预算并继续"
RESOURCE_DECISION_DENY_LABEL = "终止任务"
RESOURCE_DECISION_KIND = "resource_limit"
RESOURCE_DECISION_PROTOCOL = "approve_deny"
PERMISSION_DIALOG_TIMEOUT_RESPONSE = "agentbc_permission_dialog_timeout"
PERMISSION_DIALOG_CLOSED_RESPONSE = "agentbc_permission_dialog_closed"


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
    decision_source = str(dialog_result.details.get("decision_source") or "")
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
            "dialog_decision_source": decision_source,
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
                (
                    PERMISSION_DIALOG_TIMEOUT_RESPONSE
                    if payload.get("input_type") == "permission"
                    and action == "deny"
                    and decision_source == "timeout"
                    else PERMISSION_DIALOG_CLOSED_RESPONSE
                    if payload.get("input_type") == "permission"
                    and action == "deny"
                    and decision_source == "dialog_closed"
                    else ""
                    if payload.get("input_type") == "permission"
                    else str(dialog_result.details.get("message") or "")
                ),
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
        # Single-action approval responses must resolve to the same native
        # request that created the wait; a response recorded against a different
        # request id fails closed and is never treated as a successful answer.
        if (
            payload.get("input_type") == "permission"
            and payload.get("approval_request_id")
            and not response_error
        ):
            response_request_id = str(response_result.get("request_id") or "")
            if response_request_id and response_request_id != payload["approval_request_id"]:
                response_error = (
                    "approval_request_mismatch: "
                    "response resolved to a different native request"
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
    input_kind = str(request.get("kind") or "").strip()
    response_protocol = str(request.get("response_protocol") or "").strip()
    is_resource_decision = (
        input_type == "choice"
        and input_kind == RESOURCE_DECISION_KIND
        and response_protocol == RESOURCE_DECISION_PROTOCOL
    )
    input_options = (
        [str(option).strip() for option in request.get("options", []) if str(option).strip()]
        if input_type == "choice" and isinstance(request.get("options"), list)
        else []
    )
    if is_resource_decision:
        input_options = [RESOURCE_DECISION_APPROVE_LABEL, RESOURCE_DECISION_DENY_LABEL]
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
    if input_type == "permission" or is_resource_decision:
        command = (
            f"agentbc task respond {task_id} --input {input_id} --approve"
            f" (or --deny)"
        )
    else:
        command = f"agentbc task respond {task_id} --input {input_id} --message \"<response>\""
    workspace = task.workspace or {}
    blocked_step = request.get("blocked_step_id", "")
    summary = compact_notification_text(
        str(request.get("reason_summary") or request.get("summary") or ""),
        240,
    )
    if input_type == "choice":
        reason = compact_notification_text(str(request.get("reason") or summary), 240)
        if is_resource_decision and not option_descriptions:
            option_descriptions = [
                "Approve: double this task's resource limit and continue the same session.",
                "Deny: terminate the task with a failed terminal state.",
            ]
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
        is_single_action_approval = (
            input_type == "permission"
            and request.get("scope") == "single_action"
        )
        if input_type == "permission" and is_single_action_approval:
            operation = compact_notification_text(
                str(request.get("operation") or ""), 120
            )
            if operation:
                body_lines.append(f"Requested operation: {operation}")
            body_lines.extend(
                [
                    "Approve authorizes only this exact single action in the current session.",
                    "Deny returns to the same session and the agent handles the rejection.",
                    "This approval never changes the task permission mode.",
                    "Choose Approve or Deny below.",
                ]
            )
        elif input_type == "permission" and request.get("requested_permission"):
            body_lines.extend(
                [
                    "Requested access:",
                    compact_notification_text(str(request.get("requested_permission") or ""), 180),
                    "Approve grants the corresponding Executor its complete full permission for exactly the next continuation of this same task/session.",
                    "The technical scope is not limited to Git or the blocked command.",
                    "The grant is single-use.",
                    "Deny terminates the task as failed.",
                    "Choose Approve or Deny below.",
                ]
            )
        else:
            body_lines.append("Enter your response below to resume this same task.")
    body = "\n".join(body_lines)
    permission_grant = execution_policy_view(task.extensions).get("permission_grant")
    is_single_action_approval = (
        input_type == "permission"
        and request.get("scope") == APPROVAL_SCOPE
        and bool(str(request.get("request_id") or "").strip())
    )
    # The bounded detail is read from the durable approval receipt, never from
    # the input request, so report/status projections of ``agentbc.input`` keep
    # exposing only the short summary by default.
    reason_detail = ""
    if is_single_action_approval:
        receipt_value = (task.extensions or {}).get(APPROVAL_EXTENSION_KEY)
        if isinstance(receipt_value, dict):
            try:
                reason_detail = str(
                    validate_approval_receipt(receipt_value).get("reason_detail") or ""
                )
            except ABCError:
                reason_detail = ""
    # Sanitized bounded identity facts are carried explicitly on permission
    # payloads so the macOS permission decision view can render them
    # deterministically without querying private executor state.  Task ID, the
    # bounded task title and the Executor label are all safe public facts; the
    # scope distinguishes a single-action approval from the legacy full
    # fallback continuation.  Non-permission inputs keep the generic title.
    if input_type == "permission":
        identity_task_id = compact_notification_text(task_id, 48)
        identity_title = compact_notification_text(str(task.title or ""), 96)
        executor_label = str(getattr(task, "assignee", "") or "").strip() or "unknown"
        identity_blocked_step = compact_notification_text(
            str(request.get("blocked_step_id") or ""), 24
        )
        if is_single_action_approval:
            identity_scope = APPROVAL_SCOPE
        elif str(request.get("requested_permission") or "").strip().lower() == "full":
            identity_scope = "full"
        else:
            identity_scope = "unknown"
        dialog_title = f"AgentBC · {executor_label} · {identity_task_id}"
    else:
        identity_task_id = ""
        identity_title = ""
        executor_label = ""
        identity_blocked_step = ""
        identity_scope = ""
        dialog_title = "Agent-Bridge-Connect"
    return {
        "task_id": task_id,
        "event_type": "task.input_required",
        "title": dialog_title,
        "level": "input",
        "message": body,
        "report_path": str(workspace.get("report_file") or ""),
        "respond_command": command,
        "deadline_at": str(request.get("deadline_at") or ""),
        "input_id": input_id,
        "input_type": input_type,
        "input_kind": input_kind,
        "response_protocol": response_protocol,
        "input_reason": str(request.get("reason") or ""),
        "reason_summary": str(request.get("reason_summary") or request.get("summary") or ""),
        "reason_detail": reason_detail,
        "input_options": input_options,
        "input_option_descriptions": option_descriptions,
        "permission_grant": permission_grant,
        # Single-action approval binding: the notification is tied to exactly one
        # native request so a dialog can only Approve/Deny the bound request.
        "approval_request_id": (
            str(request.get("request_id") or "") if is_single_action_approval else ""
        ),
        "approval_request_fingerprint": (
            str(request.get("request_fingerprint") or "")
            if is_single_action_approval
            else ""
        ),
        "approval_executor_run_id": (
            str(request.get("executor_run_id") or "")
            if is_single_action_approval
            else ""
        ),
        "approval_scope": (
            str(request.get("scope") or "") if is_single_action_approval else ""
        ),
        # Sanitized bounded identity facts rendered deterministically by the
        # macOS decision view.  These are safe public facts (never private paths,
        # raw argv, tokens, or session content) and keep the generic-only title
        # replaced by a compact ``AgentBC · <Executor> · <Task ID>`` title.
        "dialog_title": dialog_title,
        "identity_task_id": identity_task_id,
        "identity_task_title": identity_title,
        "identity_executor": executor_label,
        "identity_blocked_step": identity_blocked_step,
        "identity_scope": identity_scope,
    }


def build_notification_payload(
    service: Any,
    task_id: str,
    event_type: str,
    level: str,
    message: str,
) -> dict[str, Any]:
    task = service.get_task(task_id)
    workspace = task.workspace or {}
    extensions = task.extensions or {}
    provenance = extensions.get("agentbc.provenance") if isinstance(extensions.get("agentbc.provenance"), dict) else {}
    source_platform = str(provenance.get("source_platform") or "").strip()
    dispatcher = source_platform if source_platform and source_platform != "unknown" else str(task.created_by or "unknown")
    executor = str(task.assignee or "unknown")
    report_path = str(workspace.get("report_file") or "")
    status = terminal_status_label(task.status) or str(task.status or level or "unknown")
    timing = build_timing_view(task, service.board_root)
    duration_text = format_duration_seconds(timing.get("wall_duration_s"))
    body = "\n".join(
        [
            f"Task: {task_id} {status}",
            f"Title: {compact_notification_text(task.title, 96)}",
            f"Dispatcher/Executor: {dispatcher} -> {executor}",
            f"Duration: {duration_text}",
            f"Report: {report_path}",
        ]
    )
    payload = {
        "task_id": task_id,
        "event_type": event_type,
        "title": "Agent-Bridge-Connect",
        "level": level,
        "message": body,
        "report_path": report_path,
    }
    payload.update(
        {
            key: _notification_float(timing.get(key))
            for key in ("wall_duration_s", "execution_duration_s", "waiting_duration_s", "last_run_duration_s")
        }
    )
    payload["execution_evidence"] = str(timing.get("evidence_quality") or "unknown")
    return payload


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
    return format_duration_seconds(seconds)


def format_duration_seconds(value: Any) -> str:
    try:
        seconds = max(int(round(float(value))), 0)
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _notification_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
