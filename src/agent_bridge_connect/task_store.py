from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import ABCError
from .task_id import allocate_task_code, split_task_ref


class TaskStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.tasks_dir = self.root
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def write_task(self, task_id: str, data: dict[str, Any]) -> None:
        self._atomic_write_json(self._task_path(task_id), data)

    def read_task(self, task_id: str) -> dict[str, Any]:
        path = self._task_path(task_id)
        if not path.exists():
            raise ABCError("task_not_found", f"Task not found: {task_id}", {"task_id": task_id})
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ABCError("invalid_task", f"Task is not a JSON object: {task_id}")
        return data

    def list_tasks(self, status: str | None = None, assignee: str | None = None) -> list[dict[str, Any]]:
        tasks = []
        for path in self.tasks_dir.glob("*/*/task.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if status is not None and status != "all" and data.get("status") != status:
                continue
            if assignee is not None and data.get("assignee") != assignee:
                continue
            tasks.append(data)
        return sorted(tasks, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def task_exists(self, task_id: str) -> bool:
        return self._task_path(task_id).exists()

    def append_event(self, task_id: str, event: dict[str, Any]) -> None:
        self._append_jsonl(self._task_dir(task_id) / "events.jsonl", event)

    def append_intervention(self, task_id: str, record: dict[str, Any]) -> None:
        self._append_jsonl(self._task_dir(task_id) / "interventions.jsonl", record)

    def read_events(self, task_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._task_dir(task_id) / "events.jsonl")

    def read_interventions(self, task_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._task_dir(task_id) / "interventions.jsonl")

    def task_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id)

    def acquire_lease(self, task_id: str, executor_id: str, ttl_s: int = 3600) -> str | None:
        if not self.task_exists(task_id):
            raise ABCError("task_not_found", f"Task not found: {task_id}", {"task_id": task_id})
        lease_path = self._task_dir(task_id) / "lease.json"
        existing = self._read_optional_json(lease_path)
        now = time.time()
        if existing and float(existing.get("expires_at_epoch", 0)) > now:
            return None
        token = uuid.uuid4().hex
        self._atomic_write_json(
            lease_path,
            {
                "task_id": task_id,
                "executor_id": executor_id,
                "lease_token": token,
                "acquired_at": _utc_now(),
                "expires_at_epoch": now + max(ttl_s, 1),
            },
        )
        return token

    def release_lease(self, task_id: str, lease_token: str) -> bool:
        lease_path = self._task_dir(task_id) / "lease.json"
        existing = self._read_optional_json(lease_path)
        if not existing or existing.get("lease_token") != lease_token:
            return False
        lease_path.unlink(missing_ok=True)
        return True

    def is_leased(self, task_id: str) -> bool:
        lease_path = self._task_dir(task_id) / "lease.json"
        existing = self._read_optional_json(lease_path)
        if not existing:
            return False
        if float(existing.get("expires_at_epoch", 0)) <= time.time():
            lease_path.unlink(missing_ok=True)
            return False
        return True

    def snapshot(self, task_id: str) -> None:
        source = self._task_path(task_id)
        if not source.exists():
            raise ABCError("task_not_found", f"Task not found: {task_id}", {"task_id": task_id})
        snapshots = self._task_dir(task_id) / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        for previous in snapshots.iterdir():
            if previous.is_file():
                previous.unlink()
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + ".json.gz"
        with gzip.open(snapshots / name, "wb", compresslevel=9) as handle:
            handle.write(source.read_bytes())

    def restore_snapshot(self, task_id: str) -> None:
        snapshot_root = self._task_dir(task_id) / "snapshots"
        snapshots = sorted(snapshot_root.glob("*.json.gz"))
        legacy_snapshots = sorted(snapshot_root.glob("*.json"))
        snapshots.extend(legacy_snapshots)
        if not snapshots:
            raise ABCError("snapshot_not_found", f"No snapshot found for task: {task_id}")
        selected = snapshots[-1]
        if selected.suffix == ".gz":
            with gzip.open(selected, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
        else:
            data = json.loads(selected.read_text(encoding="utf-8"))
        self.write_task(task_id, data)

    def allocate_task_code(self) -> str:
        codes: set[str] = set()
        for task_dir in self.tasks_dir.iterdir() if self.tasks_dir.exists() else []:
            if task_dir.is_dir():
                try:
                    code, iteration = split_task_ref(task_dir.name)
                except ValueError:
                    continue
                if iteration is None:
                    codes.add(code)
        try:
            return allocate_task_code(codes)
        except RuntimeError as exc:
            raise ABCError("task_create_error", str(exc)) from exc

    def allocate_chain_token(self) -> str:
        return self.allocate_task_code()

    def delete_chain(self, task_ref: str) -> bool:
        code, _ = split_task_ref(task_ref)
        chain_dir = self.tasks_dir / code
        if not chain_dir.is_dir():
            return False
        shutil.rmtree(chain_dir)
        return True

    def delete_iteration(self, task_id: str) -> bool:
        code, iteration = split_task_ref(task_id)
        if iteration is None:
            raise ABCError("invalid_task_id", f"Exact task iteration required: {task_id}")
        task_dir = self.tasks_dir / code / iteration
        if not task_dir.is_dir():
            return False
        shutil.rmtree(task_dir)
        return True

    def _task_dir(self, task_id: str) -> Path:
        code, iteration = split_task_ref(task_id)
        if iteration is None:
            iteration = self._head_iteration(code)
        return self.tasks_dir / code / iteration

    def _task_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task.json"

    def _head_iteration(self, task_code: str) -> str:
        chain_dir = self.tasks_dir / task_code
        iterations = sorted(path.name for path in chain_dir.iterdir() if path.is_dir() and path.name.isdigit())
        if not iterations:
            raise ABCError("task_not_found", f"Task not found: {task_code}", {"task_id": task_code})
        return iterations[-1]

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
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

    def _append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        from .record_management import append_bounded_jsonl

        append_bounded_jsonl(path, data)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _read_optional_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
