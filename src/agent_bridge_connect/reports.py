from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import task_step_text
from .run_lease import (
    RunLeaseState,
    heartbeat_age_s,
    load_lease,
    reconcile_task,
    recovery_recommendation,
)
from .task_id import split_task_ref, task_sequence
from .task_store import TaskStore
from .terminal_states import TASK_TERMINAL_STATES


_OPENAI_KEY_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}", re.IGNORECASE)
_PASSWORD_ASSIGNMENT_RE = re.compile(
    r"[A-Za-z0-9_.-]*(?:password|passwd|pwd)(?:_hash)?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_PASSWORD_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:password|passwd|pwd)(?:_hash)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_TERMINAL_EVENTS = {"completed", "done", "failed", "cancelled", "rejected", "task.finalized", "task.recovery_required", "task.failed"}


def redact_secrets(value: Any) -> Any:
    """Return a JSON-compatible copy with common secret patterns removed."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = _redact_text(str(key))
            if _PASSWORD_WORD_RE.search(str(key)):
                redacted[clean_key] = "[REDACTED]"
            else:
                redacted[clean_key] = redact_secrets(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def generate_report(task_id: str, board_root: Path) -> dict[str, Any]:
    """Build a redacted report from task state and append-only ledgers."""
    root = Path(board_root).expanduser().resolve()
    try:
        reconcile_task(task_id, root)
    except PermissionError:
        pass
    store = TaskStore(root)
    task = store.read_task(task_id)
    public_status = _normalize_status(str(task.get("status", "")))
    events = store.read_events(task_id)
    interventions = store.read_interventions(task_id)
    steps = _load_steps(task, store.task_dir(task_id))
    created_at = _created_at(task, events)
    completed_at = _completed_at(task, events)
    workspace = task.get("workspace") or {}
    artifacts = _dedupe_values(
        [
            *_collect_named_values(steps, "artifacts"),
            *_collect_workspace_artifacts(workspace),
        ]
    )
    errors = list(task.get("errors") or [])
    errors.extend(_collect_named_values(steps, "error", include_scalars=True))
    lease = load_lease(task_id, root)
    lease_state = lease.state if lease is not None else RunLeaseState.CLOSED
    heartbeat_age = round(heartbeat_age_s(lease), 3) if lease is not None else None
    extensions = task.get("extensions") or {}
    final_callback = extensions.get("agentbc.final_callback") or {}
    report_ready = bool(workspace.get("report_file")) and Path(str(workspace.get("report_file"))).expanduser().exists()
    chain = _chain_snapshot(task_id, root, task)
    completed_step_count = sum(
        1 for step in steps if step.get("status") in {"done", "completed"}
    )
    failed_steps = [step.get("id") for step in steps if step.get("status") == "failed"]
    blocked_steps = [step.get("id") for step in steps if step.get("status") == "blocked"]
    latest_error = errors[-1] if errors and isinstance(errors[-1], dict) else {}
    marker_valid = bool(final_callback) and final_callback.get("marker_valid") is True
    flow_contract_satisfied = (
        public_status == "completed"
        and marker_valid
        and final_callback.get("final_state") == "completed"
        and completed_step_count == len(steps)
        and report_ready
    )

    report = {
        "task_id": task.get("id", task_id),
        "title": task.get("title", ""),
        "status": public_status,
        "assignee": task.get("assignee", ""),
        "created_at": created_at,
        "completed_at": completed_at,
        "steps": steps,
        "timeline": events,
        "artifacts": artifacts,
        "interventions": interventions,
        "errors": errors,
        "workspace": workspace,
        "session_id": task.get("session_id"),
        "provenance": extensions.get("agentbc.provenance") or {},
        "lineage": extensions.get("agentbc.lineage") or {},
        "media": extensions.get("agentbc.media") or {},
        "chain": chain,
        "final_callback": final_callback,
        "has_final_callback": bool(final_callback),
        "marker_valid": marker_valid,
        "report_ready": report_ready,
        "flow_contract_satisfied": flow_contract_satisfied,
        "failure_code": str(latest_error.get("code") or ""),
        "failed_steps": failed_steps,
        "blocked_steps": blocked_steps,
        "duration_s": _duration_seconds(created_at, completed_at, steps),
        "run_lease_state": lease_state,
        "time_since_last_heartbeat_s": heartbeat_age,
        "recovery_recommendation": (
            f"Recovery required. Fix the latest system error, then run agentbc task dispatch {task_id}."
            if public_status == "needs_recovery"
            else recovery_recommendation(lease_state)
        ),
        "generated_at": _utc_now(),
        "summary": {
            "steps_total": len(steps),
            "steps_done": completed_step_count,
            "artifacts_total": len(artifacts),
            "interventions_total": len(interventions),
            "errors_total": len(errors),
        },
    }
    return redact_secrets(report)


def generate_report_md(task_id: str, board_root: Path) -> str:
    """Return a readable, redacted markdown report."""
    return _render_report_md(generate_report(task_id, board_root))


def generate_task_brief(task_id: str, board_root: Path) -> dict[str, Any]:
    """Generate a context-free review and intervention brief for a task."""
    report = generate_report(task_id, board_root)
    changed_files = _collect_brief_values(
        report.get("steps") or [],
        {"changed_files", "artifacts"},
    )
    verification = _collect_brief_values(
        report.get("steps") or [],
        {"verification", "tests", "checks"},
    )
    risks = [_format_error(error) for error in (report.get("errors") or [])]
    if report.get("status") not in TASK_TERMINAL_STATES:
        risks.append(f"Task is not terminal; current status is {report.get('status', 'unknown')}.")

    brief = {
        "task_id": report["task_id"],
        "title": report["title"],
        "status": report["status"],
        "objective": f"Complete and verify task: {report['title']}",
        "evidence": {
            "summary": report["summary"],
            "steps": report["steps"],
            "timeline": report["timeline"],
            "artifacts": report["artifacts"],
            "interventions": report["interventions"],
            "workspace": report.get("workspace") or {},
        },
        "changed_files": changed_files,
        "verification": verification,
        "risks": risks,
        "run_lease_state": report["run_lease_state"],
        "time_since_last_heartbeat_s": report["time_since_last_heartbeat_s"],
        "recovery_recommendation": report["recovery_recommendation"],
        "available_actions": _available_actions(report["task_id"], report["status"]),
    }
    return redact_secrets(brief)


def write_report_files(task_id: str, board_root: Path) -> tuple[dict[str, Any], str]:
    """Write the single human-readable task record and enforce its size budget."""
    root = Path(board_root).expanduser().resolve()
    store = TaskStore(root)
    task_dir = store.task_dir(task_id)
    report = generate_report(task_id, root)
    workspace = report.get("workspace") or {}
    report_file = workspace.get("report_file")
    if isinstance(report_file, str) and report_file:
        report["report_ready"] = True
        report["flow_contract_satisfied"] = (
            report.get("status") == "completed"
            and report.get("marker_valid") is True
            and (report.get("final_callback") or {}).get("final_state") == "completed"
            and (report.get("summary") or {}).get("steps_done")
            == (report.get("summary") or {}).get("steps_total")
        )
    markdown = _render_report_md(report)
    if isinstance(report_file, str) and report_file:
        user_report = Path(report_file).expanduser()
        try:
            user_report.parent.mkdir(parents=True, exist_ok=True)
            user_report.write_text(markdown, encoding="utf-8")
        except PermissionError:
            from .runner import RunnerClient

            RunnerClient().write_report(user_report, markdown)
    from .record_management import enforce_task_record_budget

    enforce_task_record_budget(task_dir, report_file)
    from .task_index import refresh_task_index

    refresh_task_index(root)
    return report, markdown


def _chain_snapshot(task_id: str, root: Path, task: dict[str, Any]) -> dict[str, Any]:
    lineage = _lineage_from_task(task)
    chain_root_task_id = str(lineage.get("chain_root_task_id") or task_id)
    try:
        tasks = TaskStore(root).list_tasks()
    except Exception:  # noqa: BLE001 - report generation should remain best-effort
        tasks = [task]
    members = [
        item
        for item in tasks
        if str(_lineage_from_task(item).get("chain_root_task_id") or item.get("id", "")) == chain_root_task_id
    ]
    parent_task_id = lineage.get("parent_task_id")
    parent = next(
        (
            item
            for item in members
            if isinstance(parent_task_id, str) and item.get("id") == parent_task_id
        ),
        None,
    )
    child_ids = {
        str(parent_id)
        for item in members
        for parent_id in [_lineage_from_task(item).get("parent_task_id")]
        if isinstance(parent_id, str) and parent_id
    }
    head_task_ids = sorted(
        [str(item.get("id", "")) for item in members if str(item.get("id", "")) not in child_ids],
        key=_task_id_sort_value,
        reverse=True,
    )
    return {
        "requested_task_id": task_id,
        "chain_root_task_id": chain_root_task_id,
        "head_task_ids": head_task_ids,
        "current_head_task_id": head_task_ids[0] if len(head_task_ids) == 1 else None,
        "requested_is_head": task_id in head_task_ids,
        "parent_report_file": ((parent or {}).get("workspace") or {}).get("report_file", ""),
    }


def _lineage_from_task(task: dict[str, Any]) -> dict[str, Any]:
    extensions = task.get("extensions") or {}
    lineage = extensions.get("agentbc.lineage") or {}
    if not isinstance(lineage, dict):
        lineage = {}
    workspace = task.get("workspace") or {}
    task_code = lineage.get("task_code") or workspace.get("task_code")
    iteration = workspace.get("iteration")
    if not task_code or not iteration:
        try:
            parsed_code, parsed_iteration = split_task_ref(str(task.get("id", "")))
        except ValueError:
            parsed_code, parsed_iteration = "", None
        task_code = task_code or parsed_code
        iteration = iteration or parsed_iteration
    return {
        "parent_task_id": lineage.get("parent_task_id"),
        "base_task_id": lineage.get("base_task_id") or task.get("id", ""),
        "chain_root_task_id": lineage.get("chain_root_task_id") or task.get("id", ""),
        "iteration_index": lineage.get("iteration_index", 1),
        "task_code": task_code,
        "iteration": iteration,
        "task_date": lineage.get("task_date") or workspace.get("task_date"),
        "branch_mode": lineage.get("branch_mode", "linear"),
        "chain_id": lineage.get("chain_id")
        or workspace.get("chain_id")
        or task_code
        or Path(str(workspace.get("output_dir", ""))).name,
        "chain_token": lineage.get("chain_token")
        or workspace.get("chain_token")
        or task_code,
        "chain_dir": lineage.get("chain_dir")
        or workspace.get("chain_dir")
        or task_code
        or Path(str(workspace.get("output_dir", ""))).name,
        "chain_task_id": lineage.get("chain_task_id")
        or workspace.get("chain_task_id")
        or iteration,
        "chain_output_dir": lineage.get("chain_output_dir")
        or workspace.get("chain_output_dir")
        or workspace.get("report_root")
        or workspace.get("output_dir"),
    }


def _task_id_sort_value(task_id: str) -> tuple[int, str]:
    sequence = task_sequence(task_id)
    if sequence is not None:
        return (sequence, task_id)
    try:
        return (int(task_id.removeprefix("T-")), task_id)
    except ValueError:
        return (-1, task_id)


def _load_steps(task: dict[str, Any], task_dir: Path) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(task.get("steps") or [], 1):
        step = dict(item) if isinstance(item, dict) else {"description": str(item)}
        step["description"] = task_step_text(step)
        step.setdefault("id", index)
        step.setdefault("status", "pending")
        record = step.get("record")
        if isinstance(record, str) and record:
            try:
                ledger = json.loads((task_dir / record).read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                ledger = None
            if isinstance(ledger, dict) and ledger:
                step.update(ledger)
        steps.append(step)
    return steps


def _collect_named_values(
    value: Any,
    name: str,
    include_scalars: bool = False,
) -> list[Any]:
    collected: list[Any] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key == name:
                    if isinstance(nested, list):
                        collected.extend(nested)
                    elif include_scalars and nested not in (None, ""):
                        collected.append(nested)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return _dedupe_values(collected)


def _collect_workspace_artifacts(workspace: dict[str, Any]) -> list[str]:
    if workspace.get("customer_dir") is True:
        return []
    artifacts_dir = workspace.get("artifacts_dir") if isinstance(workspace, dict) else None
    if not isinstance(artifacts_dir, str) or not artifacts_dir:
        return []
    root = Path(artifacts_dir).expanduser()
    if not root.is_dir():
        return []
    values: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                values.append(str(path.relative_to(root)))
            except ValueError:
                values.append(str(path))
    return values


def _dedupe_values(values: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for item in values:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def _collect_brief_values(value: Any, names: set[str]) -> list[Any]:
    values: list[Any] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in names:
                    values.extend(nested if isinstance(nested, list) else [nested])
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    unique: list[Any] = []
    seen: set[str] = set()
    for item in values:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def _available_actions(task_id: str, status: str) -> list[str]:
    inspect_actions = [
        f"agentbc task status {task_id} --json",
        f"agentbc task report {task_id} --format json",
    ]
    if status == "pending":
        return ["agentbc worker run --executor <assignee> --once", *inspect_actions]
    if status == "running":
        return [
            f"agentbc task pause {task_id} --reason <reason>",
            f"agentbc task correct {task_id} --step <id> --message <message>",
            *inspect_actions,
        ]
    if status == "input_required":
        return [
            f"agentbc task status {task_id} --json",
            f"agentbc task retry {task_id} --step <id>",
            *inspect_actions,
        ]
    if status == "needs_recovery":
        return [
            f"agentbc task dispatch {task_id}",
            f"agentbc task recover {task_id} --from-snapshot",
            *inspect_actions,
        ]
    return inspect_actions


def _completed_at(task: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    value = task.get("completed_at")
    if isinstance(value, str) and value:
        return value
    for event in reversed(events):
        if event.get("event_type") in _TERMINAL_EVENTS:
            timestamp = event.get("created_at") or event.get("completed_at")
            if isinstance(timestamp, str) and timestamp:
                return timestamp
    if _normalize_status(str(task.get("status", ""))) in TASK_TERMINAL_STATES:
        updated_at = task.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            return updated_at
    return None


def _created_at(task: dict[str, Any], events: list[dict[str, Any]]) -> str:
    value = task.get("created_at")
    if isinstance(value, str) and value:
        return value
    for event in events:
        timestamp = event.get("created_at")
        if isinstance(timestamp, str) and timestamp:
            return timestamp
    return ""


def _duration_seconds(created_at: Any, completed_at: Any, steps: list[dict[str, Any]]) -> float:
    start = _parse_timestamp(created_at)
    end = _parse_timestamp(completed_at)
    if start is not None and end is not None:
        return round(max((end - start).total_seconds(), 0.0), 3)
    durations = _collect_named_values(steps, "duration_s", include_scalars=True)
    return round(
        sum(float(value) for value in durations if isinstance(value, (int, float))),
        3,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _render_report_md(report: dict[str, Any]) -> str:
    created_at = str(report.get("created_at") or "")
    completed_at = str(report.get("completed_at") or "")
    lines = [
        f"# Report: {report.get('title', '')}",
        "",
        "## Summary",
        f"- Task: `{report.get('task_id', '')}`",
        f"- Status: `{report.get('status', '')}`",
        f"- Agent declared state: `{(report.get('final_callback') or {}).get('final_state', 'missing')}`",
        f"- Marker valid: `{'yes' if report.get('marker_valid') else 'no'}`",
        f"- Completed steps: `{(report.get('summary') or {}).get('steps_done', 0)}/{(report.get('summary') or {}).get('steps_total', 0)}`",
        f"- Flow contract satisfied: `{'yes' if report.get('flow_contract_satisfied') else 'no'}`",
        f"- Failure code: `{report.get('failure_code') or 'none'}`",
        f"- Failed steps: `{_format_step_ids(report.get('failed_steps'))}`",
        f"- Blocked steps: `{_format_step_ids(report.get('blocked_steps'))}`",
        f"- Assignee: `{report.get('assignee', '')}`",
        f"- Created: `{_format_report_timestamp(created_at)}`",
        f"- Completed: `{_format_report_timestamp(completed_at) if completed_at else 'not completed'}`",
        f"- Duration: `{_format_duration(report.get('duration_s'))}`",
        f"- Run lease: `{report.get('run_lease_state', 'closed')}`",
        f"- Last heartbeat age: `{_format_heartbeat_age(report.get('time_since_last_heartbeat_s'))}`",
        f"- Recovery: {report.get('recovery_recommendation', '')}",
        "",
        "## Path Plan",
    ]
    workspace = report.get("workspace") or {}
    lineage = report.get("lineage") or {}
    if workspace:
        lines.extend(
            [
                f"- Task code: `{workspace.get('task_code') or lineage.get('task_code') or ''}`",
                f"- Iteration: `{workspace.get('iteration') or lineage.get('iteration_index') or ''}`",
                f"- Task date: `{workspace.get('task_date') or lineage.get('task_date') or ''}`",
                f"- Customer directory: `{workspace.get('customer_dir')}`",
                f"- Customer path: `{workspace.get('customer_path') or 'none'}`",
                f"- Project root: `{workspace.get('project_root') or workspace.get('root', '')}`",
                f"- Artifact root: `{workspace.get('artifact_root') or workspace.get('artifacts_dir', '')}`",
                f"- Report directory: `{workspace.get('report_root') or workspace.get('output_dir', '')}`",
                f"- Runtime record: `{workspace.get('internal_task_dir', '')}`",
                f"- Task brief: `{workspace.get('task_file', '')}`",
                f"- Report: `{workspace.get('report_file', '')}`",
            ]
        )
    else:
        lines.append("- None")

    image_inputs = (report.get("media") or {}).get("images") or []
    if image_inputs:
        lines.extend(["", "## Image Inputs", *[f"- `{image}`" for image in image_inputs]])

    if lineage:
        chain = report.get("chain") or {}
        lines.extend(
            [
                f"- Parent task: `{lineage.get('parent_task_id') or 'none'}`",
                f"- Previous report: `{chain.get('parent_report_file') or 'none'}`",
                f"- Current task: `{report.get('task_id', '')}`",
                f"- Chain root task: `{lineage.get('chain_root_task_id', report.get('task_id', ''))}`",
                f"- Base task: `{lineage.get('base_task_id', report.get('task_id', ''))}`",
                f"- Current chain heads: `{', '.join(chain.get('head_task_ids') or []) or report.get('task_id', '')}`",
                f"- Is chain head: `{'yes' if chain.get('requested_is_head', True) else 'no'}`",
                f"- Branch mode: `{lineage.get('branch_mode', 'linear')}`",
                f"- Base workspace: `{lineage.get('base_workspace_root', workspace.get('root', ''))}`",
                f"- Base artifacts: `{lineage.get('base_artifacts_dir', workspace.get('artifacts_dir', ''))}`",
                f"- Iteration: `{lineage.get('iteration_index', 1)}`",
            ]
        )

    lines.extend(
        [
            "",
            "## Steps",
        ]
    )
    steps = report.get("steps") or []
    if steps:
        for index, step in enumerate(steps, 1):
            lines.append(
                f"{index}. [{step.get('status', 'pending')}] "
                f"{task_step_text(step)}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Artifacts"])
    artifacts = report.get("artifacts") or []
    lines.extend(f"- `{artifact}`" for artifact in artifacts)
    if not artifacts:
        lines.append("- None")

    lines.extend(["", "## Timeline"])
    timeline = report.get("timeline") or []
    for event in timeline:
        event_time = _format_report_timestamp(str(event.get("created_at") or ""))
        lines.append(
            f"- `{event_time}` "
            f"{event.get('event_type', 'event')}"
        )
    if not timeline:
        lines.append("- None")

    provenance = report.get("provenance") or {}
    platform = provenance.get("source_platform") or report.get("assignee") or "unavailable"
    session_id = report.get("session_id") or "unavailable"
    lines.extend(
        [
            "",
            "## Dispatcher Traceability",
            f"- Dispatcher platform: `{platform}`",
            f"- Dispatcher conversation ID: `{session_id}`",
        ]
    )

    interventions = report.get("interventions") or []
    if interventions:
        lines.extend(["", "## Intervention Log"])
        for intervention in interventions:
            lines.append(
                f"- `{intervention.get('created_at', '')}` "
                f"{intervention.get('type') or intervention.get('intervention_type', 'event')}"
            )

    errors = report.get("errors") or []
    if errors:
        lines.extend(["", "## Errors"])
        lines.extend(f"- {_format_error(error)}" for error in errors)

    lines.extend(
        [
            "",
            "## Duration",
            f"- Duration: `{_format_duration(report.get('duration_s'))}`",
            "",
        ]
    )
    return _redact_text("\n".join(lines))


def _format_step_ids(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value)


def _format_report_timestamp(value: str) -> str:
    if not value:
        return "unknown"
    parsed = _parse_timestamp(value)
    if parsed is None:
        return value
    local = parsed.astimezone()
    zone = local.tzname() or "local"
    return f"{local:%Y-%m-%d %H:%M:%S} {zone}"


def _format_duration(value: Any) -> str:
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


def _format_heartbeat_age(value: Any) -> str:
    if value is None:
        return "none"
    return _format_duration(value)


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_error(value: Any) -> str:
    if not isinstance(value, dict):
        return _format_value(value)
    code = str(value.get("code") or "error")
    message = str(value.get("message") or "")
    details = value.get("details") if isinstance(value.get("details"), dict) else {}
    result = details.get("result") if isinstance(details.get("result"), dict) else {}
    context: list[str] = []
    if details.get("executor"):
        context.append(f"executor={details['executor']}")
    returncode = result.get("returncode")
    if returncode is None:
        progress = details.get("progress")
        if isinstance(progress, dict):
            returncode = progress.get("returncode")
    if returncode is not None:
        context.append(f"returncode={returncode}")
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        context.append(f"stderr={_bounded_text(stderr.strip(), 300)}")
    suffix = f" ({', '.join(context)})" if context else ""
    return f"`{code}`: {message or 'No error message provided.'}{suffix}"


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)] + "..."


def _redact_text(value: str) -> str:
    value = _PASSWORD_ASSIGNMENT_RE.sub("[REDACTED]", value)
    value = _OPENAI_KEY_RE.sub("[REDACTED]", value)
    return _PASSWORD_WORD_RE.sub("[REDACTED]", value)


def _normalize_status(status: str) -> str:
    mapping = {
        "assigned": "running",
        "working": "running",
        "pause_pending": "running",
        "paused": "running",
        "review_required": "input_required",
        "needs_review": "needs_recovery",
        "failed": "failed",
        "in_progress": "running",
    }
    return mapping.get(status, status or "needs_recovery")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
