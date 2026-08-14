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


DELETION_TRANSACTIONS_DIR = ".agentbc-deletions"
DELETION_RECEIPTS_FILE = "deletion_receipts.jsonl"
MAX_DELETION_RECEIPT_BYTES = 8 * 1024


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
        codes.update(self.reserved_deletion_codes())
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

    def reserve_chain_deletion(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Persist a chain-delete intent before moving any owned directory."""
        code, iteration = split_task_ref(str(plan.get("task_code") or ""))
        if iteration is not None:
            raise ABCError("task_delete_requires_chain_code", "task delete requires a task code")
        existing = self.pending_chain_deletion(code)
        if existing is not None:
            if existing.get("generation") != plan.get("generation"):
                raise ABCError(
                    "task_delete_in_progress",
                    f"A different deletion transaction already reserves task code {code}",
                )
            return existing

        deletion_id = uuid.uuid4().hex
        transaction_dir = self._deletion_transaction_dir(deletion_id)
        transaction_dir.mkdir(parents=True, exist_ok=False)
        targets: list[dict[str, str]] = []
        try:
            for raw_target in plan.get("targets") or []:
                target = self._validated_delete_target(raw_target, deletion_id)
                if Path(target["staged_path"]).exists():
                    raise ABCError(
                        "task_delete_conflict",
                        f"Deletion staging path already exists: {target['staged_path']}",
                    )
                targets.append(target)
            manifest = {
                "version": 1,
                "deletion_id": deletion_id,
                "task_code": code,
                "generation": str(plan.get("generation") or ""),
                "state": "reserved",
                "reserved_at": _utc_now(),
                "plan": {**plan, "targets": targets},
                "targets": targets,
            }
            self._atomic_write_json(transaction_dir / "manifest.json", manifest)
            return manifest
        except Exception:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            self._remove_empty_deletion_dir(transaction_dir.parent)
            raise

    def pending_chain_deletion(self, task_code: str) -> dict[str, Any] | None:
        code, iteration = split_task_ref(task_code)
        if iteration is not None:
            return None
        matches = []
        for path in self._deletion_root().glob("*/manifest.json"):
            manifest = self._read_optional_json(path)
            if not manifest or manifest.get("state") == "committed":
                continue
            if str(manifest.get("task_code") or "").upper() == code:
                matches.append(manifest)
        if len(matches) > 1:
            raise ABCError(
                "task_delete_conflict",
                f"Multiple pending deletion transactions reserve task code {code}",
            )
        return matches[0] if matches else None

    def reserved_deletion_codes(self) -> set[str]:
        codes: set[str] = set()
        for path in self._deletion_root().glob("*/manifest.json"):
            manifest = self._read_optional_json(path)
            if not manifest or manifest.get("state") == "committed":
                continue
            try:
                code, iteration = split_task_ref(str(manifest.get("task_code") or ""))
            except ValueError:
                continue
            if iteration is None:
                codes.add(code)
        return codes

    def stage_chain_deletion(self, deletion_id: str) -> dict[str, Any]:
        """Atomically move each owned directory aside, rolling back ordinary failures."""
        manifest = self._read_deletion_manifest(deletion_id)
        if manifest.get("state") == "committed":
            return manifest
        staged: list[dict[str, str]] = []
        try:
            for target in manifest.get("targets") or []:
                validated = self._validated_persisted_delete_target(target, deletion_id)
                source = Path(validated["path"])
                destination = Path(validated["staged_path"])
                source_exists = source.exists() or source.is_symlink()
                destination_exists = destination.exists() or destination.is_symlink()
                if source_exists and destination_exists:
                    raise ABCError(
                        "task_delete_conflict",
                        f"Both deletion source and staging path exist: {source}",
                    )
                if destination_exists:
                    staged.append(validated)
                    continue
                if not source_exists:
                    raise ABCError("task_delete_interrupted", f"Deletion source disappeared: {source}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._stage_delete_target(source, destination)
                staged.append(validated)
                manifest["state"] = "staging"
                manifest["staged_count"] = len(staged)
                self._atomic_write_json(self._deletion_manifest_path(deletion_id), manifest)
        except Exception as exc:
            rollback_ok = self.rollback_chain_deletion(deletion_id)
            if isinstance(exc, ABCError):
                details = dict(exc.details or {})
                details["rollback_complete"] = rollback_ok
                raise ABCError(exc.code, exc.message, details) from exc
            raise ABCError(
                "task_delete_failed",
                f"Could not stage task deletion: {exc}",
                {"rollback_complete": rollback_ok},
            ) from exc
        manifest["state"] = "staged"
        manifest["staged_count"] = len(staged)
        self._atomic_write_json(self._deletion_manifest_path(deletion_id), manifest)
        return manifest

    def rollback_chain_deletion(self, deletion_id: str) -> bool:
        try:
            manifest = self._read_deletion_manifest(deletion_id)
        except ABCError:
            return False
        ok = True
        for target in reversed(manifest.get("targets") or []):
            try:
                validated = self._validated_persisted_delete_target(target, deletion_id)
                source = Path(validated["path"])
                destination = Path(validated["staged_path"])
                source_exists = source.exists() or source.is_symlink()
                destination_exists = destination.exists() or destination.is_symlink()
                if source_exists and destination_exists:
                    ok = False
                elif destination_exists:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
                    self._cleanup_staging_parents(destination)
                elif not source_exists:
                    ok = False
            except (ABCError, OSError):
                ok = False
        if ok:
            self._remove_deletion_transaction(deletion_id)
        else:
            manifest["state"] = "rollback_required"
            self._atomic_write_json(self._deletion_manifest_path(deletion_id), manifest)
        return ok

    def finalize_chain_deletion(self, deletion_id: str) -> dict[str, Any]:
        """Write the bounded receipt, then atomically release the pending code claim."""
        manifest = self._read_deletion_manifest(deletion_id)
        if manifest.get("state") == "committed":
            return dict(manifest.get("receipt") or {})
        if manifest.get("state") != "staged":
            raise ABCError("task_delete_interrupted", "Deletion transaction is not fully staged")
        for target in manifest.get("targets") or []:
            validated = self._validated_persisted_delete_target(target, deletion_id)
            source = Path(validated["path"])
            destination = Path(validated["staged_path"])
            if source.exists() or source.is_symlink() or not destination.exists():
                raise ABCError(
                    "task_delete_interrupted",
                    f"Deletion staging is incomplete for {source}",
                )
        plan = manifest.get("plan") if isinstance(manifest.get("plan"), dict) else {}
        receipt = next(
            (
                item
                for item in self._read_deletion_receipts()
                if str(item.get("deletion_id") or "") == deletion_id
            ),
            None,
        ) or {
            "version": 1,
            "deletion_id": deletion_id,
            "task_code": manifest.get("task_code"),
            "generation": manifest.get("generation"),
            "task_ids": list(plan.get("task_ids") or []),
            "deleted_at": _utc_now(),
            "deleted_count": len(plan.get("delete_objects") or []),
            "preserved_count": len(plan.get("preserve_objects") or []),
        }
        self._append_deletion_receipt(receipt)
        manifest["state"] = "committed"
        manifest["committed_at"] = receipt["deleted_at"]
        manifest["receipt"] = receipt
        self._atomic_write_json(self._deletion_manifest_path(deletion_id), manifest)
        return receipt

    def purge_committed_deletion(self, deletion_id: str) -> bool:
        manifest = self._read_deletion_manifest(deletion_id)
        if manifest.get("state") != "committed":
            return False
        try:
            targets = [
                self._validated_persisted_delete_target(target, deletion_id)
                for target in manifest.get("targets") or []
            ]
        except ABCError:
            return False
        ok = True
        for target in targets:
            destination = Path(str(target.get("staged_path") or ""))
            try:
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink(missing_ok=True)
                self._cleanup_staging_parents(destination)
            except OSError:
                ok = False
        if ok:
            self._remove_deletion_transaction(deletion_id)
        return ok

    def latest_deletion_receipt(self, task_code: str) -> dict[str, Any] | None:
        code, iteration = split_task_ref(task_code)
        if iteration is not None:
            return None
        matches = [
            receipt
            for receipt in self._read_deletion_receipts()
            if str(receipt.get("task_code") or "").upper() == code
        ]
        return matches[-1] if matches else None

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

    def _deletion_root(self) -> Path:
        return self.root / DELETION_TRANSACTIONS_DIR

    def _deletion_transaction_dir(self, deletion_id: str) -> Path:
        if not deletion_id or any(char not in "0123456789abcdef" for char in deletion_id.lower()):
            raise ABCError("task_delete_invalid_transaction", "Invalid deletion transaction id")
        return self._deletion_root() / deletion_id

    def _deletion_manifest_path(self, deletion_id: str) -> Path:
        return self._deletion_transaction_dir(deletion_id) / "manifest.json"

    def _read_deletion_manifest(self, deletion_id: str) -> dict[str, Any]:
        manifest = self._read_optional_json(self._deletion_manifest_path(deletion_id))
        if not manifest:
            raise ABCError("task_delete_not_found", f"Deletion transaction not found: {deletion_id}")
        return manifest

    def _validated_delete_target(self, target: dict[str, Any], deletion_id: str) -> dict[str, str]:
        if not isinstance(target, dict):
            raise ABCError("task_delete_ownership_error", "Deletion target metadata is invalid")
        kind = str(target.get("kind") or "").strip()
        source_text = str(target.get("path") or "").strip()
        allowed_root_text = str(target.get("allowed_root") or "").strip()
        if not kind or not source_text or not allowed_root_text:
            raise ABCError("task_delete_ownership_error", "Deletion target is missing ownership metadata")
        source = Path(source_text).expanduser()
        allowed_root = Path(allowed_root_text).expanduser()
        source = source.absolute()
        allowed_root = allowed_root.resolve()
        if source.is_symlink():
            raise ABCError("task_delete_ownership_error", f"Refusing symlink deletion target: {source}")
        resolved_source = source.resolve()
        if resolved_source == allowed_root or not _is_within(resolved_source, allowed_root):
            raise ABCError(
                "task_delete_ownership_error",
                f"Deletion target is outside its AgentBC-owned root: {source}",
            )
        staged = source.parent / DELETION_TRANSACTIONS_DIR / deletion_id / f"{kind}-{source.name}"
        resolved_staged = staged.resolve()
        if not _is_within(resolved_staged, allowed_root):
            raise ABCError(
                "task_delete_ownership_error",
                f"Deletion staging path is outside its AgentBC-owned root: {staged}",
            )
        return {
            "kind": kind,
            "path": str(source),
            "allowed_root": str(allowed_root),
            "staged_path": str(staged),
        }

    def _validated_persisted_delete_target(
        self,
        target: dict[str, Any],
        deletion_id: str,
    ) -> dict[str, str]:
        validated = self._validated_delete_target(target, deletion_id)
        if str(target.get("staged_path") or "") != validated["staged_path"]:
            raise ABCError("task_delete_ownership_error", "Deletion staging path was modified")
        return validated

    def _remove_deletion_transaction(self, deletion_id: str) -> None:
        transaction_dir = self._deletion_transaction_dir(deletion_id)
        shutil.rmtree(transaction_dir, ignore_errors=True)
        self._remove_empty_deletion_dir(transaction_dir.parent)

    @staticmethod
    def _remove_empty_deletion_dir(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass

    def _cleanup_staging_parents(self, staged_path: Path) -> None:
        self._remove_empty_deletion_dir(staged_path.parent)
        self._remove_empty_deletion_dir(staged_path.parent.parent)

    def _read_deletion_receipts(self) -> list[dict[str, Any]]:
        path = self.root / DELETION_RECEIPTS_FILE
        if not path.exists():
            return []
        receipts: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                receipts.append(value)
        return receipts

    @staticmethod
    def _stage_delete_target(source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def _append_deletion_receipt(self, receipt: dict[str, Any]) -> None:
        receipts = self._read_deletion_receipts()
        deletion_id = str(receipt.get("deletion_id") or "")
        if not any(str(item.get("deletion_id") or "") == deletion_id for item in receipts):
            receipts.append(receipt)
        lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in receipts]
        while len("".join(lines).encode("utf-8")) > MAX_DELETION_RECEIPT_BYTES and len(lines) > 1:
            lines.pop(0)
        self._atomic_write_text(self.root / DELETION_RECEIPTS_FILE, "".join(lines))

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._atomic_write_text(path, payload)

    def _atomic_write_text(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
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


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
