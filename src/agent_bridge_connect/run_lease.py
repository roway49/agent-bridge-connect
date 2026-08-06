from __future__ import annotations

import json
import os
import signal
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import ABCError
from .task_health import clear_task_progress
from .task_store import TaskStore


class RunLeaseState:
    ACTIVE = "active"
    SUSPENDED = "suspended"
    STALE = "stale"
    ORPHANED = "orphaned"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class RunLease:
    run_id: str
    task_id: str
    executor_id: str
    pid: int
    pgid: int
    work_dir: str
    started_at: str
    last_heartbeat_at: str
    cleanup_strategy: str
    state: str


@dataclass
class _TaskHealthView:
    id: str
    workspace: dict[str, Any]


_LEASE_PATHS: dict[str, Path] = {}
_WORKER_FINALIZE_GRACE_S = 30


def create_lease(task_id: str, executor_id: str, pid: int, work_dir: str) -> RunLease:
    """Create an active in-memory lease for an executor run."""
    now = _utc_now()
    pgid = _process_group(pid)
    cleanup_strategy = (
        "kill_pgid"
        if pid > 0 and pgid > 0 and pgid != _current_process_group()
        else "none"
    )
    return RunLease(
        run_id=f"{executor_id}-{task_id}-{uuid.uuid4().hex[:8]}",
        task_id=task_id,
        executor_id=executor_id,
        pid=pid,
        pgid=pgid,
        work_dir=str(Path(work_dir).expanduser().resolve()),
        started_at=now,
        last_heartbeat_at=now,
        cleanup_strategy=cleanup_strategy,
        state=RunLeaseState.ACTIVE,
    )


def heartbeat(lease: RunLease) -> None:
    """Update last_heartbeat_at to now."""
    if lease.state in {RunLeaseState.SUSPENDED, RunLeaseState.CLOSING, RunLeaseState.CLOSED}:
        return
    lease.last_heartbeat_at = _utc_now()
    lease.state = RunLeaseState.ACTIVE
    _persist_registered(lease)


def is_stale(lease: RunLease, heartbeat_timeout_s: int = 120) -> bool:
    """Return True if heartbeat expired but not confirmed dead."""
    if lease.state in {RunLeaseState.SUSPENDED, RunLeaseState.CLOSING, RunLeaseState.CLOSED}:
        return False
    return heartbeat_age_s(lease) > max(heartbeat_timeout_s, 0)


def is_orphaned(lease: RunLease, recovery_timeout_s: int = 600) -> bool:
    """Return True if stale for longer than recovery window."""
    if lease.state in {RunLeaseState.SUSPENDED, RunLeaseState.CLOSING, RunLeaseState.CLOSED}:
        return False
    return heartbeat_age_s(lease) > max(recovery_timeout_s, 0)


def reconcile_task(task_id: str, board_root: Path) -> str:
    """Lazily reconcile run health without treating timeout as failure."""
    root = Path(board_root).expanduser().resolve()
    lease = load_lease(task_id, root)
    if lease is None:
        return RunLeaseState.CLOSED
    if lease.state in {RunLeaseState.CLOSING, RunLeaseState.CLOSED}:
        return lease.state
    if lease.state == RunLeaseState.SUSPENDED:
        return lease.state

    store = TaskStore(root)
    task = store.read_task(task_id)
    status = str(task.get("status", ""))

    process_lost = _process_lost(lease)
    if process_lost and _worker_process_alive(task):
        lease.state = (
            RunLeaseState.ACTIVE
            if heartbeat_age_s(lease) <= _WORKER_FINALIZE_GRACE_S
            else RunLeaseState.STALE
        )
        save_lease(lease, root)
        return lease.state

    if lease.state == RunLeaseState.ORPHANED or process_lost:
        lease.state = RunLeaseState.ORPHANED
        if status in {"pending", "running", "assigned", "working", "input_required", "needs_review", "pause_pending", "paused"}:
            task["status"] = "failed"
            _set_recovery_metadata(task, lease, "failed")
            _set_internal_status(task, "failed")
            extensions = task.get("extensions") if isinstance(task.get("extensions"), dict) else {}
            callback = extensions.pop("agentbc.final_callback", None)
            if isinstance(callback, dict):
                extensions["agentbc.superseded_final_callback"] = {
                    **callback,
                    "superseded_by": "failed",
                    "superseded_reason": "Executor exit could not be confirmed",
                    "superseded_at": _utc_now(),
                }
            task["extensions"] = extensions
            store.write_task(task_id, task)
            store.append_event(
                task_id,
                {
                    "event_type": "task.failed",
                    "task_id": task_id,
                    "created_at": _utc_now(),
                    "error": {"code": "executor_exit_unconfirmed", "message": "Executor exit could not be confirmed"},
                },
            )
            clear_task_progress(_TaskHealthView(task_id, task.get("workspace") or {}), remove_log=True)
            try:
                from .reports import write_report_files

                write_report_files(task_id, root)
            except (ABCError, OSError, PermissionError):
                pass
        save_lease(lease, root)
        return RunLeaseState.ORPHANED

    if lease.state == RunLeaseState.STALE or is_stale(lease):
        lease.state = RunLeaseState.STALE
        save_lease(lease, root)
        return RunLeaseState.STALE

    if lease.state != RunLeaseState.ACTIVE:
        lease.state = RunLeaseState.ACTIVE
        save_lease(lease, root)
    return RunLeaseState.ACTIVE


def reap_orphaned(board_root: Path) -> list[dict]:
    """Scan run leases and return orphaned runs for an explicit user decision."""
    root = Path(board_root).expanduser().resolve()
    orphaned: list[dict[str, Any]] = []
    for lease_path in sorted(root.glob("*/*/run_lease.json")):
        try:
            lease_data = json.loads(lease_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        task_id = str(lease_data.get("task_id") or f"{lease_path.parent.parent.name}-{lease_path.parent.name}")
        try:
            state = reconcile_task(task_id, root)
            lease = load_lease(task_id, root)
        except (ABCError, OSError, ValueError, json.JSONDecodeError):
            continue
        if state == RunLeaseState.ORPHANED and lease is not None:
            orphaned.append(
                {
                    "task_id": task_id,
                    "run_id": lease.run_id,
                    "executor_id": lease.executor_id,
                    "state": lease.state,
                    "heartbeat_age_s": round(heartbeat_age_s(lease), 3),
                    "recommendation": recovery_recommendation(lease.state),
                }
            )
    return orphaned


def cleanup_lease(lease: RunLease) -> None:
    """Idempotently clean only the process group managed by AgentBC."""
    if lease.state == RunLeaseState.CLOSED:
        return
    lease.state = RunLeaseState.CLOSING
    _persist_registered(lease)
    if (
        lease.cleanup_strategy == "kill_pgid"
        and lease.pgid > 0
        and lease.pgid != _current_process_group()
    ):
        try:
            os.killpg(lease.pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    lease.state = RunLeaseState.CLOSED
    _persist_registered(lease)


def save_lease(lease: RunLease, board_root: Path) -> Path:
    root = Path(board_root).expanduser().resolve()
    path = _lease_path(lease.task_id, root)
    _atomic_write_json(path, asdict(lease))
    _LEASE_PATHS[lease.run_id] = path
    return path


def load_lease(task_id: str, board_root: Path) -> RunLease | None:
    path = _lease_path(task_id, Path(board_root).expanduser().resolve())
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    lease = RunLease(**data)
    _LEASE_PATHS[lease.run_id] = path
    return lease


def close_lease(lease: RunLease, board_root: Path | None = None) -> None:
    lease.state = RunLeaseState.CLOSING
    if board_root is not None:
        save_lease(lease, board_root)
    cleanup_lease(lease)
    if board_root is not None:
        save_lease(lease, board_root)


def suspend_lease(
    task_id: str,
    board_root: Path,
    *,
    executor_run_id: str,
    executor_id: str,
    work_dir: str,
) -> RunLease:
    """Persist a non-stale logical wait after an executor has yielded for input."""
    root = Path(board_root).expanduser().resolve()
    lease = load_lease(task_id, root)
    if lease is None:
        lease = create_lease(task_id, executor_id, 0, work_dir)
    lease.run_id = executor_run_id or lease.run_id
    lease.executor_id = executor_id or lease.executor_id
    lease.pid = 0
    lease.pgid = 0
    lease.cleanup_strategy = "none"
    lease.state = RunLeaseState.SUSPENDED
    save_lease(lease, root)
    return lease


def recover_task(task_id: str, board_root: Path, from_snapshot: bool = False) -> dict[str, Any]:
    """Prepare a task for explicit recovery; optionally restore its latest snapshot."""
    root = Path(board_root).expanduser().resolve()
    store = TaskStore(root)
    if from_snapshot:
        store.restore_snapshot(task_id)

    if not from_snapshot:
        try:
            reconcile_task(task_id, root)
        except ABCError:
            pass

    task = store.read_task(task_id)
    lease = load_lease(task_id, root)
    if lease is None:
        raise ABCError("run_lease_not_found", f"No run lease found for task: {task_id}")

    previous_status = str(task.get("status", ""))
    if from_snapshot:
        close_lease(lease, root)
        task = store.read_task(task_id)
        recovery_status = "snapshot_restored"
    else:
        if previous_status != "needs_recovery":
            raise ABCError(
                "invalid_recovery_state",
                f"Cannot recover task in state: {previous_status}",
                {"task_id": task_id, "status": previous_status, "run_lease_state": lease.state},
            )
        close_lease(lease, root)
        task = store.read_task(task_id)
        task["status"] = "needs_recovery"
        _set_recovery_metadata(task, lease, "ready_for_retry")
        _set_internal_status(task, "needs_recovery")
        store.write_task(task_id, task)
        store.append_event(
            task_id,
            {
                "event_type": "task.recovery_ready",
                "task_id": task_id,
                "created_at": _utc_now(),
                "recovery_status": "ready_for_retry",
                "message": "Stale run lease closed; dispatch the task to retry execution.",
            },
        )
        recovery_status = "ready_for_retry"

    return {
        "task_id": task_id,
        "previous_status": previous_status,
        "status": task.get("status", ""),
        "run_lease_state": lease.state,
        "recovery_status": recovery_status,
        "from_snapshot": from_snapshot,
    }


def heartbeat_age_s(lease: RunLease) -> float:
    heartbeat_at = _parse_timestamp(lease.last_heartbeat_at)
    if heartbeat_at is None:
        return float("inf")
    return max((_now() - heartbeat_at).total_seconds(), 0.0)


def recovery_recommendation(state: str) -> str:
    if state == RunLeaseState.ACTIVE:
        return "Continue waiting while heartbeat remains healthy."
    if state == RunLeaseState.SUSPENDED:
        return "Waiting for the requested user response; stale detection is paused."
    if state == RunLeaseState.STALE:
        return "Review executor state; continue waiting, recover, cancel, or reassign."
    if state == RunLeaseState.ORPHANED:
        return "Run agentbc task recover <id> or recover from a snapshot."
    if state == RunLeaseState.CLOSING:
        return "Retry cleanup if the managed process remains."
    return "No recovery action is required."


def _set_recovery_metadata(task: dict[str, Any], lease: RunLease, recovery_status: str) -> None:
    extensions = task.setdefault("extensions", {})
    recommendation = recovery_recommendation(lease.state)
    if recovery_status == "ready_for_retry":
        recommendation = f"Run agentbc task dispatch {lease.task_id} to retry execution."
    extensions["run_lease"] = {
        "run_id": lease.run_id,
        "state": lease.state,
        "recovery_status": recovery_status,
        "last_heartbeat_at": lease.last_heartbeat_at,
        "heartbeat_age_s": round(heartbeat_age_s(lease), 3),
        "recommendation": recommendation,
    }


def _set_internal_status(task: dict[str, Any], status: str) -> None:
    extensions = task.setdefault("extensions", {})
    execution = extensions.setdefault("agentbc.execution", {})
    execution["internal_status"] = status
    task["updated_at"] = _utc_now()


def _lease_path(task_id: str, board_root: Path) -> Path:
    store = TaskStore(board_root)
    return store.task_dir(task_id) / "run_lease.json"


def _persist_registered(lease: RunLease) -> None:
    path = _LEASE_PATHS.get(lease.run_id)
    if path is not None:
        _atomic_write_json(path, asdict(lease))


def _process_group(pid: int) -> int:
    if pid <= 0:
        return 0
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return 0


def _process_lost(lease: RunLease) -> bool:
    if lease.pid <= 0:
        return False
    return not _pid_alive(lease.pid)


def _worker_process_alive(task: dict[str, Any]) -> bool:
    extensions = task.get("extensions") if isinstance(task.get("extensions"), dict) else {}
    execution = extensions.get("agentbc.execution") if isinstance(extensions.get("agentbc.execution"), dict) else {}
    try:
        worker_pid = int(execution.get("worker_pid") or 0)
    except (TypeError, ValueError):
        return False
    return _pid_alive(worker_pid)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _current_process_group() -> int:
    try:
        return os.getpgrp()
    except OSError:
        return 0


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now() -> str:
    return _now().isoformat().replace("+00:00", "Z")
