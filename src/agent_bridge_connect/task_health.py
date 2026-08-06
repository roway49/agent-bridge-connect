from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .terminal_states import TASK_TERMINAL_STATES


ACTIVE_STATUSES = {"running", "assigned", "working", "input_required", "pause_pending", "paused", "in_progress"}
TERMINAL_STATUSES = TASK_TERMINAL_STATES
DEFAULT_STALE_AFTER_S = 300
DEFAULT_LONG_STALE_AFTER_S = 600
DEFAULT_STARTUP_GRACE_S = 10
DASHBOARD_PROTOCOL_VERSION = 2
MAX_PROGRESS_MESSAGE_CHARS = 512
MAX_RUN_LOG_BYTES = 768


def task_run_temp_path(task: Any) -> Path:
    workspace = getattr(task, "workspace", None) or {}
    runtime_root = Path(
        str(workspace.get("internal_task_dir") or workspace.get("report_root") or workspace.get("output_dir") or "")
    ).expanduser()
    return runtime_root / f".{task.id}.run.temp"


def task_run_log_path(task: Any) -> Path:
    workspace = getattr(task, "workspace", None) or {}
    runtime_root = Path(
        str(workspace.get("internal_task_dir") or workspace.get("report_root") or workspace.get("output_dir") or "")
    ).expanduser()
    return runtime_root / f"{task.id}-run.log"


def write_task_progress(
    task: Any,
    *,
    state: str = "running",
    message: str = "",
    source: str = "agent",
) -> dict[str, Any]:
    now = utc_now()
    temp_path = task_run_temp_path(task)
    log_path = task_run_log_path(task)
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task.id,
        "state": state,
        "updated_at": now,
        "message": _compact_progress_message(str(message or state)),
        "source": str(source or "agent"),
        "run_log": str(log_path),
    }
    temporary = temp_path.with_name(f".{temp_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(temp_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now}\t{payload['source']}\t{payload['state']}\t{payload['message']}\n")
    if log_path.stat().st_size > MAX_RUN_LOG_BYTES:
        tail = log_path.read_bytes()[-MAX_RUN_LOG_BYTES:]
        log_path.write_text(tail.decode("utf-8", errors="ignore"), encoding="utf-8")
    return payload


def _compact_progress_message(value: str) -> str:
    text = " ".join(value.split())
    if len(text) <= MAX_PROGRESS_MESSAGE_CHARS:
        return text
    return text[: MAX_PROGRESS_MESSAGE_CHARS - 3].rstrip() + "..."


def clear_task_progress(task: Any, *, remove_log: bool = False) -> None:
    task_run_temp_path(task).unlink(missing_ok=True)
    if remove_log:
        task_run_log_path(task).unlink(missing_ok=True)


def task_health(
    task: Any,
    *,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    long_stale_after_s: int = DEFAULT_LONG_STALE_AFTER_S,
    startup_grace_s: int = DEFAULT_STARTUP_GRACE_S,
) -> dict[str, Any]:
    status = str(getattr(task, "status", "") or "")
    temp_path = task_run_temp_path(task)
    log_path = task_run_log_path(task)
    base: dict[str, Any] = {
        "state": "inactive",
        "color": "gray",
        "stale_after_s": stale_after_s,
        "long_stale_after_s": long_stale_after_s,
        "startup_grace_s": startup_grace_s,
        "temp_file": str(temp_path),
        "run_log": str(log_path),
        "last_progress_at": "",
        "last_progress_age_s": None,
        "run_lease_state": "",
        "run_lease_pid": None,
        "runner_alive": None,
        "message": "",
        "source": "",
    }
    if status == "needs_recovery":
        base.update({"state": "runner_recovery_required", "color": "red"})
        return base
    if status == "failed":
        base.update({"state": "failed", "color": "red"})
        return base
    if status == "input_required":
        request = dict(((getattr(task, "extensions", None) or {}).get("agentbc.input") or {}))
        base.update(
            {
                "state": "waiting_for_input",
                "color": "yellow",
                "run_lease_state": "suspended",
                "message": str(request.get("summary") or "waiting for user response"),
                "source": "input",
            }
        )
        return base
    if status in TERMINAL_STATUSES:
        base.update({"state": status, "color": "gray" if status == "completed" else "red"})
        return base
    execution = dict(((getattr(task, "extensions", None) or {}).get("agentbc.execution") or {}))
    if status == "pending":
        dispatched = bool(
            execution.get("dispatch_status") == "accepted" or execution.get("worker_run_id")
        )
        base.update(
            {
                "state": "pending",
                "color": "gray",
                "message": "worker accepted; waiting to start" if dispatched else "waiting in queue",
                "source": "runner" if dispatched else "task",
            }
        )
        return base
    if status not in ACTIVE_STATUSES:
        base.update({"state": status or "pending", "color": "gray"})
        return base

    now_ts = time.time()
    runner_lost = False
    lease = _read_run_lease(task)
    if lease:
        lease_state = str(lease.get("state") or "")
        pid = _safe_int(lease.get("pid"))
        runner_alive = _pid_alive(pid) if pid and pid > 0 else None
        lease_progress_at = _parse_datetime(str(lease.get("last_heartbeat_at") or ""))
        lease_age_s = max(int(now_ts - lease_progress_at.timestamp()), 0) if lease_progress_at else None
        runner_lost = lease_state in {"orphaned", "closing", "closed"} or runner_alive is False
        base.update(
            {
                "run_lease_state": lease_state,
                "run_lease_pid": pid,
                "runner_alive": runner_alive,
                "run_lease_age_s": lease_age_s,
            }
        )

    payload = _read_temp_payload(temp_path)
    progress_at = _progress_timestamp(temp_path, payload)
    if progress_at is None:
        start_at = _task_start_timestamp(task)
        start_age_s = max(int(now_ts - start_at.timestamp()), 0) if start_at else None
        if isinstance(start_age_s, int) and start_age_s <= max(startup_grace_s, 0):
            base.update(
                {
                    "state": "starting",
                    "color": "green",
                    "message": "waiting for run temp",
                    "last_progress_age_s": start_age_s,
                }
            )
            return base
        if runner_lost:
            base.update(
                {
                    "state": "runner_lost",
                    "color": "red",
                    "message": "runner process is not alive",
                    "last_progress_age_s": start_age_s,
                }
            )
            return base
        base.update({"state": "unresponsive", "color": "yellow", "message": "run temp missing"})
        return base
    age_s = max(int(now_ts - progress_at.timestamp()), 0)
    if age_s <= max(stale_after_s, 0):
        state = "responsive"
        color = "green"
    elif runner_lost:
        state = "runner_lost"
        color = "red"
    elif age_s > max(long_stale_after_s, stale_after_s, 0):
        state = "long_unresponsive"
        color = "orange"
    else:
        state = "unresponsive"
        color = "yellow"
    base.update(
        {
            "state": state,
            "color": color,
            "last_progress_at": progress_at.isoformat().replace("+00:00", "Z"),
            "last_progress_age_s": age_s,
            "message": str(payload.get("message") or "") if isinstance(payload, dict) else "",
            "source": str(payload.get("source") or "") if isinstance(payload, dict) else "",
        }
    )
    return base


def cleanup_task_report_records(task: Any) -> None:
    workspace = getattr(task, "workspace", None) or {}
    report_root = Path(str(workspace.get("report_root") or "")).expanduser()
    task_id = str(getattr(task, "id", "") or "")
    if not task_id or not report_root:
        return
    for key in ("task_file", "report_file"):
        value = workspace.get(key)
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if _is_within(path, report_root):
            path.unlink(missing_ok=True)
    clear_task_progress(task, remove_log=True)
    for path in report_root.glob(f"{task_id}-*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    try:
        report_root.rmdir()
    except OSError:
        pass


def cleanup_managed_task_artifacts(task: Any) -> bool:
    workspace = getattr(task, "workspace", None) or {}
    if bool(workspace.get("customer_dir")):
        return False
    artifact_value = workspace.get("artifact_root") or workspace.get("artifacts_dir")
    agentbc_value = workspace.get("agentbc_root")
    if not artifact_value or not agentbc_value:
        return False
    artifact_root = Path(str(artifact_value)).expanduser().resolve()
    managed_root = Path(str(agentbc_value)).expanduser().resolve() / "tasks" / "artifacts"
    if artifact_root == managed_root or not _is_within(artifact_root, managed_root):
        return False
    shutil.rmtree(artifact_root, ignore_errors=True)
    return True


def cleanup_empty_managed_task_artifacts(task: Any) -> bool:
    """Remove only an empty task-scoped managed artifact directory."""
    workspace = getattr(task, "workspace", None) or {}
    if bool(workspace.get("customer_dir")):
        return False
    artifact_value = workspace.get("artifact_root") or workspace.get("artifacts_dir")
    agentbc_value = workspace.get("agentbc_root")
    if not artifact_value or not agentbc_value:
        return False
    artifact_root = Path(str(artifact_value)).expanduser().resolve()
    managed_root = Path(str(agentbc_value)).expanduser().resolve() / "tasks" / "artifacts"
    if artifact_root == managed_root or not _is_within(artifact_root, managed_root):
        return False
    try:
        artifact_root.rmdir()
    except OSError:
        return False
    return True


def cleanup_cancelled_task_files(task: Any) -> None:
    cleanup_task_report_records(task)
    cleanup_managed_task_artifacts(task)


def dashboard_paths(board_root: str | Path) -> dict[str, Path]:
    board = Path(board_root).expanduser().resolve()
    digest = hashlib.sha1(str(board).encode("utf-8")).hexdigest()[:12]
    root = Path(tempfile.gettempdir()) / "agentbc-task-list-dashboard" / digest
    return {
        "root": root,
        "state": root / "state.json",
        "cohort": root / "cohort.json",
        "refresh": root / "refresh.tick",
        "board": board,
    }


def mark_dashboard_active(board_root: str | Path, *, pid: int | None = None) -> None:
    paths = dashboard_paths(board_root)
    paths["root"].mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": int(pid or os.getpid()),
        "board_root": str(paths["board"]),
        "protocol_version": DASHBOARD_PROTOCOL_VERSION,
        "updated_at": utc_now(),
    }
    temporary = paths["state"].with_name(f".{paths['state'].name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(paths["state"])


def mark_dashboard_closed(board_root: str | Path) -> None:
    paths = dashboard_paths(board_root)
    paths["state"].unlink(missing_ok=True)
    paths["cohort"].unlink(missing_ok=True)


def register_dashboard_task(
    board_root: str | Path,
    task_id: str,
    *,
    reset: bool = False,
) -> list[str]:
    paths = dashboard_paths(board_root)
    paths["root"].mkdir(parents=True, exist_ok=True)
    task_ids = [] if reset else dashboard_task_ids(board_root)
    clean_task_id = str(task_id or "").strip().upper()
    if clean_task_id and clean_task_id not in task_ids:
        task_ids.append(clean_task_id)
    payload = {
        "board_root": str(paths["board"]),
        "protocol_version": DASHBOARD_PROTOCOL_VERSION,
        "task_ids": task_ids,
        "updated_at": utc_now(),
    }
    if reset or not paths["cohort"].exists():
        payload["started_at"] = payload["updated_at"]
    else:
        try:
            previous = json.loads(paths["cohort"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        payload["started_at"] = str(previous.get("started_at") or payload["updated_at"])
    temporary = paths["cohort"].with_name(f".{paths['cohort'].name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(paths["cohort"])
    return task_ids


def dashboard_task_ids(board_root: str | Path) -> list[str]:
    path = dashboard_paths(board_root)["cohort"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    values = payload.get("task_ids") if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return []
    task_ids: list[str] = []
    for value in values:
        task_id = str(value or "").strip().upper()
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
    return task_ids


def dashboard_cohort_exists(board_root: str | Path) -> bool:
    return dashboard_paths(board_root)["cohort"].exists()


def remove_dashboard_task(board_root: str | Path, task_id: str) -> list[str]:
    paths = dashboard_paths(board_root)
    if not paths["cohort"].exists():
        return []
    task_ids = dashboard_task_ids(board_root)
    clean_task_id = str(task_id or "").strip().upper()
    task_ids = [value for value in task_ids if value != clean_task_id]
    try:
        previous = json.loads(paths["cohort"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    payload = {
        "board_root": str(paths["board"]),
        "protocol_version": DASHBOARD_PROTOCOL_VERSION,
        "task_ids": task_ids,
        "started_at": str(previous.get("started_at") or utc_now()),
        "updated_at": utc_now(),
    }
    temporary = paths["cohort"].with_name(f".{paths['cohort'].name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(paths["cohort"])
    return task_ids


def request_dashboard_refresh(board_root: str | Path) -> None:
    paths = dashboard_paths(board_root)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["refresh"].write_text(utc_now() + "\n", encoding="utf-8")


def dashboard_is_active(board_root: str | Path, *, max_age_s: int = 60) -> bool:
    paths = dashboard_paths(board_root)
    data = _read_dashboard_state(board_root)
    if not data or str(data.get("board_root") or "") != str(paths["board"]):
        return False
    pid = int(data.get("pid") or 0)
    if pid <= 0 or not _pid_alive(pid):
        return False
    try:
        age = time.time() - paths["state"].stat().st_mtime
    except OSError:
        return False
    return age <= max(max_age_s, 1)


def dashboard_protocol_matches(board_root: str | Path) -> bool:
    data = _read_dashboard_state(board_root)
    try:
        version = int(data.get("protocol_version") or 0)
    except (TypeError, ValueError):
        return False
    return version == DASHBOARD_PROTOCOL_VERSION


def stop_dashboard_process(board_root: str | Path, *, wait_s: float = 2.0) -> bool:
    paths = dashboard_paths(board_root)
    data = _read_dashboard_state(board_root)
    if not data or str(data.get("board_root") or "") != str(paths["board"]):
        mark_dashboard_closed(board_root)
        return False
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or pid == os.getpid() or not _pid_alive(pid):
        mark_dashboard_closed(board_root)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        mark_dashboard_closed(board_root)
        return False
    deadline = time.monotonic() + max(wait_s, 0.0)
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    mark_dashboard_closed(board_root)
    return True


def dashboard_refresh_mtime(board_root: str | Path) -> float:
    try:
        return dashboard_paths(board_root)["refresh"].stat().st_mtime
    except OSError:
        return 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_temp_payload(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_dashboard_state(board_root: str | Path) -> dict[str, Any]:
    path = dashboard_paths(board_root)["state"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_run_lease(task: Any) -> dict[str, Any]:
    workspace = getattr(task, "workspace", None) or {}
    internal_task_dir = workspace.get("internal_task_dir")
    if not internal_task_dir:
        return {}
    path = Path(str(internal_task_dir)).expanduser() / "run_lease.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _progress_timestamp(path: Path, payload: dict[str, Any]) -> datetime | None:
    raw = str(payload.get("updated_at") or "")
    if raw:
        parsed = _parse_datetime(raw)
        if parsed is not None:
            return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _task_start_timestamp(task: Any) -> datetime | None:
    for key in ("updated_at", "created_at"):
        raw = str(getattr(task, key, "") or "")
        if not raw:
            continue
        parsed = _parse_datetime(raw)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True
