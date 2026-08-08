"""Shared timing view for REPORT-001 / OBS-001.

Single source of truth for how the product renders task timing and the current
lease state. Status JSON, Task List, Report and Notification all consume this
view so they display the same numbers.

- ``wall_duration_s`` is the full lifecycle time from creation to completion/now.
- ``execution_duration_s`` is accumulated only from authoritative worker/Executor
  run intervals. Pending, paused, needs_recovery, ready-for-recovery and human
  recovery waiting never add to execution time.
- ``waiting_duration_s`` keeps the historical input-waiting accounting unchanged.
- ``lease_state`` is always derived from the authoritative ``run_lease.json``.
  Stale ``extensions.agentbc.execution`` lease snapshots are historical evidence
  only and cannot override the current view.
- Historical tasks that lack enough interval evidence return ``unknown`` or
  ``estimated`` values instead of manufacturing precise execution time from
  ``created_at``/``completed_at``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .run_lease import RunLeaseState, load_lease
from .task_store import TaskStore
from .terminal_states import TASK_TERMINAL_STATES

_EXECUTION_EXTENSION_KEY = "agentbc.execution"
_RUN_INTERVALS_KEY = "run_intervals"
_INPUT_KEY = "agentbc.input"
_INPUT_HISTORY_KEY = "agentbc.input_history"
_FINAL_CALLBACK_KEY = "agentbc.final_callback"

_STATUS_NORMALIZATION = {
    "assigned": "running",
    "working": "running",
    "pause_pending": "running",
    "paused": "running",
    "review_required": "input_required",
    "needs_review": "needs_recovery",
    "failed": "failed",
    "in_progress": "running",
}


def build_timing_view(
    task: Any,
    board_root: str | Path,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Build the shared timing view for a task.

    ``task`` may be a ``TaskModel`` or a task JSON mapping. ``now`` overrides the
    wall-clock reference so deterministic fake-clock tests can assert exact values.
    """
    data = _task_data(task)
    task_id = str(data.get("id") or data.get("task_id") or "")
    root = Path(board_root).expanduser().resolve()
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}

    reference = _end_reference(data, extensions, now)
    wall_duration_s = _wall_duration(data, reference)

    intervals = _collected_intervals(task_id, extensions, root, now)
    excluded = _excluded_periods(data, extensions, root, reference, now)
    execution_duration_s, adjusted_intervals = _execution_duration(intervals, excluded)
    last_run_duration_s = _last_run_duration(adjusted_intervals)
    waiting_duration_s = _waiting_duration(extensions, reference)
    evidence_quality = _evidence_quality(intervals)
    lease = load_lease(task_id, root) if task_id else None

    return {
        "task_id": task_id,
        "wall_duration_s": wall_duration_s,
        "execution_duration_s": execution_duration_s,
        "last_run_duration_s": last_run_duration_s,
        "waiting_duration_s": waiting_duration_s,
        "evidence_quality": evidence_quality,
        "execution_duration_known": execution_duration_s is not None,
        "run_count": len(intervals),
        "run_intervals": adjusted_intervals,
        "lease_state": lease.state if lease is not None else RunLeaseState.CLOSED,
    }


def _task_data(task: Any) -> dict[str, Any]:
    if hasattr(task, "to_dict"):
        return dict(task.to_dict())
    if isinstance(task, dict):
        return dict(task)
    return {}


def _collected_intervals(
    task_id: str,
    extensions: dict[str, Any],
    root: Path,
    now: str | None,
) -> list[dict[str, Any]]:
    """Ledger intervals plus the current on-disk lease, deduplicated by run id."""
    intervals = _ledger_intervals(extensions)
    lease = load_lease(task_id, root) if task_id else None
    if lease is not None and not any(
        str(item.get("run_id") or "") == lease.run_id for item in intervals
    ):
        current = _interval_from_lease(lease, now)
        if current is not None:
            intervals.append(current)
    intervals.sort(key=lambda item: str(item.get("started_at") or ""))
    return intervals


def _ledger_intervals(extensions: dict[str, Any]) -> list[dict[str, Any]]:
    execution = extensions.get(_EXECUTION_EXTENSION_KEY)
    if not isinstance(execution, dict):
        return []
    raw_intervals = execution.get(_RUN_INTERVALS_KEY)
    if not isinstance(raw_intervals, list):
        return []
    intervals: list[dict[str, Any]] = []
    for item in raw_intervals:
        if not isinstance(item, dict):
            continue
        started = _parse_timestamp(str(item.get("started_at") or ""))
        ended = _parse_timestamp(str(item.get("ended_at") or ""))
        if started is None or ended is None:
            continue
        duration = item.get("duration_s")
        try:
            duration = round(float(duration), 3)
        except (TypeError, ValueError):
            duration = round(max((ended - started).total_seconds(), 0.0), 3)
        intervals.append(
            {
                "run_id": str(item.get("run_id") or ""),
                "executor_id": str(item.get("executor_id") or ""),
                "started_at": str(item.get("started_at") or ""),
                "ended_at": str(item.get("ended_at") or ""),
                "duration_s": duration,
                "state": str(item.get("state") or RunLeaseState.CLOSED),
                "source": "recorded",
            }
        )
    return intervals


def _interval_from_lease(lease: Any, now: str | None) -> dict[str, Any] | None:
    started = _parse_timestamp(lease.started_at)
    if started is None:
        return None
    if lease.state == RunLeaseState.ACTIVE:
        end_raw = now or _utc_now()
        ended = _parse_timestamp(end_raw)
        state = RunLeaseState.ACTIVE
    else:
        end_raw = lease.last_heartbeat_at
        ended = _parse_timestamp(end_raw)
        state = lease.state
    if ended is None:
        return None
    return {
        "run_id": lease.run_id,
        "executor_id": lease.executor_id,
        "started_at": lease.started_at,
        "ended_at": end_raw,
        "duration_s": round(max((ended - started).total_seconds(), 0.0), 3),
        "state": state,
        "source": "current_lease",
    }


def _excluded_periods(
    data: dict[str, Any],
    extensions: dict[str, Any],
    root: Path,
    reference: str | None,
    now: str | None,
) -> list[tuple[datetime, datetime]]:
    """Non-execution periods that must never count toward execution duration."""
    periods: list[tuple[datetime, datetime]] = []
    end = _parse_timestamp(reference)
    for request in _input_requests(extensions):
        start = _parse_timestamp(str(request.get("created_at") or ""))
        stop_raw = request.get("responded_at") or reference
        stop = _parse_timestamp(str(stop_raw or "")) or end
        if start is not None and stop is not None and stop > start:
            periods.append((start, stop))
    periods.extend(_pause_periods(data, root, now))
    return periods


def _pause_periods(
    data: dict[str, Any],
    root: Path,
    now: str | None,
) -> list[tuple[datetime, datetime]]:
    """Pause spans derived from the interventions ledger."""
    task_id = str(data.get("id") or data.get("task_id") or "")
    try:
        interventions = TaskStore(root).read_interventions(task_id) if task_id else []
    except (OSError, ValueError):
        interventions = []
    sorted_items = sorted(
        (item for item in interventions if isinstance(item, dict)),
        key=lambda item: str(item.get("created_at") or ""),
    )
    periods: list[tuple[datetime, datetime]] = []
    pause_at: datetime | None = None
    for item in sorted_items:
        kind = str(item.get("type") or item.get("intervention_type") or "")
        ts = _parse_timestamp(str(item.get("created_at") or ""))
        if ts is None:
            continue
        if kind == "pause":
            pause_at = ts
        elif kind == "resume" and pause_at is not None:
            if ts > pause_at:
                periods.append((pause_at, ts))
            pause_at = None
    intervention = data.get("intervention")
    if pause_at is not None and isinstance(intervention, dict) and bool(intervention.get("paused")):
        end = _parse_timestamp(now or _utc_now())
        if end is not None and end > pause_at:
            periods.append((pause_at, end))
    return periods


def _execution_duration(
    intervals: list[dict[str, Any]],
    excluded: list[tuple[datetime, datetime]],
) -> tuple[float | None, list[dict[str, Any]]]:
    if not intervals:
        return None, []
    total = 0.0
    adjusted: list[dict[str, Any]] = []
    for interval in intervals:
        start = _parse_timestamp(str(interval.get("started_at") or ""))
        end = _parse_timestamp(str(interval.get("ended_at") or ""))
        if start is None or end is None:
            continue
        raw = max((end - start).total_seconds(), 0.0)
        if raw <= 0:
            continue
        excluded_s = _excluded_overlap(start, end, excluded)
        exec_s = round(max(raw - excluded_s, 0.0), 3)
        total += exec_s
        adjusted.append({**interval, "execution_duration_s": exec_s})
    if not adjusted:
        return None, []
    return round(total, 3), adjusted


def _last_run_duration(adjusted: list[dict[str, Any]]) -> float | None:
    if not adjusted:
        return None
    latest = max(
        adjusted,
        key=lambda item: str(item.get("started_at") or ""),
    )
    return latest.get("execution_duration_s")


def _waiting_duration(extensions: dict[str, Any], reference: str | None) -> float:
    end = _parse_timestamp(reference)
    if end is None:
        return 0.0
    total = 0.0
    seen: set[str] = set()
    for request in _input_requests(extensions):
        input_id = str(request.get("input_id") or "")
        if input_id and input_id in seen:
            continue
        if input_id:
            seen.add(input_id)
        start = _parse_timestamp(str(request.get("created_at") or ""))
        stop = _parse_timestamp(str(request.get("responded_at") or "")) or end
        if start is not None:
            total += max((min(stop, end) - start).total_seconds(), 0.0)
    return round(total, 3)


def _evidence_quality(intervals: list[dict[str, Any]]) -> str:
    if not intervals:
        return "unknown"
    if any(str(item.get("state") or "") != RunLeaseState.ACTIVE for item in intervals):
        return "authoritative"
    return "estimated"


def _input_requests(extensions: dict[str, Any]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    history = extensions.get(_INPUT_HISTORY_KEY)
    if isinstance(history, list):
        requests.extend(item for item in history if isinstance(item, dict))
    current = extensions.get(_INPUT_KEY)
    if isinstance(current, dict):
        requests.append(current)
    return requests


def _end_reference(
    data: dict[str, Any],
    extensions: dict[str, Any],
    now: str | None,
) -> str:
    value = data.get("completed_at")
    if isinstance(value, str) and value:
        return value
    callback = extensions.get(_FINAL_CALLBACK_KEY)
    if isinstance(callback, dict):
        finished = callback.get("finished_at")
        if isinstance(finished, str) and finished:
            return finished
    if _is_terminal_status(str(data.get("status") or "")):
        updated = data.get("updated_at")
        if isinstance(updated, str) and updated:
            return updated
    return now or _utc_now()


def _wall_duration(data: dict[str, Any], reference: str | None) -> float | None:
    start = _parse_timestamp(str(data.get("created_at") or ""))
    end = _parse_timestamp(str(reference or ""))
    if start is None or end is None:
        return None
    return round(max((end - start).total_seconds(), 0.0), 3)


def _is_terminal_status(raw_status: str) -> bool:
    return _normalize_status(raw_status) in TASK_TERMINAL_STATES


def _normalize_status(raw_status: str) -> str:
    return _STATUS_NORMALIZATION.get(raw_status, raw_status or "needs_recovery")


def _excluded_overlap(
    start: datetime,
    end: datetime,
    excluded: list[tuple[datetime, datetime]],
) -> float:
    total = 0.0
    for excluded_start, excluded_end in excluded:
        overlap = min(end, excluded_end) - max(start, excluded_start)
        if overlap.total_seconds() > 0:
            total += overlap.total_seconds()
    return min(total, max((end - start).total_seconds(), 0.0))


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
