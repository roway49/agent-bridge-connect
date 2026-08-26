from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import ABCError


MAX_TASK_RECORD_BYTES = 10 * 1024
MAX_EVENT_LOG_BYTES = 1536
MAX_DIAGNOSTIC_TEXT = 512

RECORD_README = """# AgentBC Record Directory

This directory stores AgentBC task state and runtime records only.

- `TASK_INDEX.md`: compact, human-readable task lookup index.
- `task_index.jsonl`: machine-readable task lookup index.
- `<TASKCODE>/<NNN>/task.json`: authoritative state for one chain iteration.
- `<TASKCODE>/<NNN>/events.jsonl`: bounded lifecycle events.
- `<TASKCODE>/<NNN>/interventions.jsonl`: bounded user intervention history.
- `<TASKCODE>/<NNN>/run_lease.json`, progress temp files, and run logs: runtime diagnostics.

Task briefs, reports, and default-workspace deliverables are not stored here:

- `../tasks/report/YYYY-MM-DD/<TASKCODE>/<TASKID>-task.md`
- `../tasks/report/YYYY-MM-DD/<TASKCODE>/<TASKID>-report.md`
- `../tasks/artifacts/YYYY-MM-DD/<TASKCODE>/`

Deliverables for a user-selected customer path remain in that customer project.

Run `agentbc record clean` to remove eligible terminal-task runtime diagnostics
only. `task.json`, `TASK_INDEX.md`, and `task_index.jsonl` are always preserved,
and reports are never deleted by record cleanup.

`agentbc task close <TASKCODE>` closes the current queued (pending) or active
chain head; terminal and stale iterations are rejected. Closing an active `001`
root removes its reports, AgentBC-managed artifacts, runtime record, and task-code
claim. Closing an active later iteration requires `--confirm`; only that iteration's
task/report files and runtime record are removed. Earlier iterations, indexes, the
task-code claim, and shared artifacts are preserved. Artifacts may already contain
changes from the removed iteration. AgentBC keeps no rollback backup and does not
restore artifact versions. Customer-project files are never deleted by `task close`.

Do not edit task state files manually. Use the `agentbc task` commands instead.
"""


def ensure_record_root(root: str | Path) -> Path:
    record_root = Path(root).expanduser().resolve()
    record_root.mkdir(parents=True, exist_ok=True)
    readme = record_root / "README.md"
    if not readme.exists() or readme.read_text(encoding="utf-8") != RECORD_README:
        readme.write_text(RECORD_README, encoding="utf-8")
    return record_root


def compact_diagnostic_details(value: Any) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name == "events" and isinstance(item, list):
                compact["events_seen"] = len(item)
                event_types: list[str] = []
                for event in item:
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("event_type") or "").strip()
                    if event_type and event_type not in event_types:
                        event_types.append(event_type)
                    if len(event_types) >= 8:
                        break
                if event_types:
                    compact["event_types"] = event_types
                continue
            if name in {"command", "aggregated_output", "prompt", "raw_output"}:
                compact[f"{name}_bytes"] = len(str(item or "").encode("utf-8"))
                continue
            if name in {"stdout", "stderr"}:
                text = str(item or "")
                compact[f"{name}_bytes"] = len(text.encode("utf-8"))
                if text:
                    compact[f"{name}_tail"] = _compact_text(text[-MAX_DIAGNOSTIC_TEXT:])
                continue
            compact[name] = compact_diagnostic_details(item)
        return compact
    if isinstance(value, list):
        return [compact_diagnostic_details(item) for item in value[:8]]
    if isinstance(value, str):
        return _compact_text(value)
    return value


def append_bounded_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(compact_diagnostic_details(data), ensure_ascii=False, separators=(",", ":")) + "\n"
    existing = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    lines = [*existing, line]
    while len("".join(lines).encode("utf-8")) > MAX_EVENT_LOG_BYTES and len(lines) > 2:
        lines.pop(1)
    if len("".join(lines).encode("utf-8")) > MAX_EVENT_LOG_BYTES:
        lines = lines[-1:]
    path.write_text("".join(lines), encoding="utf-8")


def enforce_task_record_budget(task_dir: str | Path, record_file: str | Path | None = None) -> int:
    directory = Path(task_dir).expanduser().resolve()
    canonical = Path(record_file).expanduser().resolve() if record_file else None
    for legacy in (directory / "report.json", directory / "report.md", directory / "chain.json"):
        if canonical is None or legacy != canonical:
            legacy.unlink(missing_ok=True)
    shutil.rmtree(directory / "steps", ignore_errors=True)
    _trim_jsonl(directory / "events.jsonl", MAX_EVENT_LOG_BYTES)
    _trim_jsonl(directory / "interventions.jsonl", 768)
    for run_log in directory.glob("*-run.log"):
        _trim_text_tail(run_log, 768)
    total = task_record_size(directory)
    if total <= MAX_TASK_RECORD_BYTES:
        return total
    _compact_terminal_task_json(directory / "task.json")
    total = task_record_size(directory)
    if total <= MAX_TASK_RECORD_BYTES:
        return total
    if canonical is not None and canonical.is_file() and canonical.parent == directory:
        other_bytes = total - canonical.stat().st_size
        allowance = max(MAX_TASK_RECORD_BYTES - other_bytes, 0)
        _truncate_utf8_file(canonical, allowance)
        total = task_record_size(directory)
    if total > MAX_TASK_RECORD_BYTES:
        raise ABCError(
            "record_budget_exceeded",
            f"Task record exceeds {MAX_TASK_RECORD_BYTES} bytes: {total}",
            {"task_dir": str(directory), "bytes": total, "limit": MAX_TASK_RECORD_BYTES},
        )
    return total


def _compact_terminal_task_json(path: Path) -> None:
    if not path.is_file():
        return
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(task, dict) or str(task.get("status") or "") not in {
        "completed",
        "failed",
        "cancelled",
        "rejected",
    }:
        return

    compact: dict[str, Any] = {}
    for key in (
        "id",
        "title",
        "status",
        "assignee",
        "created_by",
        "created_at",
        "updated_at",
        "workspace",
        "session_id",
    ):
        value = task.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value

    compact["title"] = _compact_text(str(compact.get("title") or ""))
    compact["steps"] = [_compact_terminal_step(step) for step in (task.get("steps") or []) if isinstance(step, dict)]
    intervention = {
        str(key): compact_diagnostic_details(value)
        for key, value in (task.get("intervention") or {}).items()
        if value not in (None, "", False, [], {})
    }
    if intervention:
        compact["intervention"] = intervention
    errors = task.get("errors") if isinstance(task.get("errors"), list) else []
    if errors:
        compact["errors"] = [compact_diagnostic_details(item) for item in errors[-2:]]
    extensions = _compact_terminal_extensions(task.get("extensions"))
    if extensions:
        compact["extensions"] = extensions
    path.write_text(json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def _compact_terminal_step(step: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("id", "status", "record"):
        value = step.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    description = " ".join(str(step.get("description") or "").split())
    if description:
        compact["description"] = description[:240]
    result = step.get("result")
    if isinstance(result, dict):
        result_summary = {
            str(key): compact_diagnostic_details(value)
            for key, value in result.items()
            if key in {"status", "summary", "artifacts", "error"}
        }
        if result_summary:
            compact["result"] = result_summary
    return compact


def _compact_terminal_extensions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in (
        "agentbc.resources",
        "agentbc.session",
        "agentbc.permission",
        "agentbc.auxiliary_sessions",
    ):
        item = value.get(key)
        if item not in (None, "", [], {}):
            # These v1 policy receipts are already bounded and must remain exact.
            compact[key] = item
    for key in (
        "agentbc.provenance",
        "agentbc.lineage",
        "agentbc.execution",
        "agentbc.final_callback",
        "agentbc.superseded_final_callback",
        "agentbc.close_intent",
    ):
        item = value.get(key)
        if item not in (None, "", [], {}):
            compact[key] = compact_diagnostic_details(item)
    return compact


def assert_task_record_budget(task_dir: str | Path) -> int:
    directory = Path(task_dir).expanduser().resolve()
    total = task_record_size(directory)
    if total > MAX_TASK_RECORD_BYTES:
        raise ABCError(
            "record_budget_exceeded",
            f"Task definition exceeds the {MAX_TASK_RECORD_BYTES}-byte Record budget: {total}",
            {"task_dir": str(directory), "bytes": total, "limit": MAX_TASK_RECORD_BYTES},
        )
    return total


def clean_terminal_records(root: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    from .task_store import TaskStore

    record_root = ensure_record_root(root)
    store = TaskStore(record_root)
    # Keep actionable records. input_required and needs_recovery still carry the
    # evidence needed to resume or recover a task.
    terminal = {"completed", "cancelled", "failed", "rejected"}
    removed: list[str] = []
    cleaned_tasks: list[str] = []
    for task in store.list_tasks():
        if str(task.get("status") or "") not in terminal:
            continue
        task_id = str(task.get("id") or "")
        task_dir = store.task_dir(task_id)
        targets = [
            path
            for path in task_dir.iterdir()
            if path.name != "task.json"
        ]
        if not targets:
            continue
        cleaned_tasks.append(task_id)
        for path in targets:
            removed.append(str(path))
            if dry_run:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
    if not dry_run:
        from .task_index import refresh_task_index

        refresh_task_index(record_root)
    return {
        "ok": True,
        "record_root": str(record_root),
        "dry_run": dry_run,
        "tasks_cleaned": cleaned_tasks,
        "removed": removed,
        "preserved": [
            str(record_root / "README.md"),
            str(record_root / "TASK_INDEX.md"),
            str(record_root / "task_index.jsonl"),
            "*/*/task.json",
        ],
        "cleaned_at": _utc_now(),
    }


def task_record_size(task_dir: str | Path) -> int:
    directory = Path(task_dir)
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def _trim_jsonl(path: Path, limit: int) -> None:
    if not path.exists() or path.stat().st_size <= limit:
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    while len("".join(lines).encode("utf-8")) > limit and len(lines) > 2:
        lines.pop(1)
    path.write_text("".join(lines[-2:]), encoding="utf-8")


def _truncate_utf8_file(path: Path, limit: int) -> None:
    marker = "\n\n> Record truncated to satisfy the 10KB AgentBC record budget.\n"
    if limit <= 0:
        path.unlink(missing_ok=True)
        return
    data = path.read_bytes()
    if len(data) <= limit:
        return
    marker_bytes = marker.encode("utf-8")
    content_limit = max(limit - len(marker_bytes), 0)
    text = data[:content_limit].decode("utf-8", errors="ignore")
    path.write_text(text + (marker if content_limit else ""), encoding="utf-8")


def _trim_text_tail(path: Path, limit: int) -> None:
    if not path.exists() or path.stat().st_size <= limit:
        return
    data = path.read_bytes()[-limit:]
    path.write_text(data.decode("utf-8", errors="ignore"), encoding="utf-8")


def _compact_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_DIAGNOSTIC_TEXT:
        return text
    return text[: MAX_DIAGNOSTIC_TEXT - 3].rstrip() + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
