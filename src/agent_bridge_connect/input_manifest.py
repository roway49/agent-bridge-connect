from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .protocol import ABCError


INPUTS_EXTENSION_KEY = "agentbc.inputs"
INPUTS_VERSION = 1


def canonical_task_input_root(workspace: dict[str, Any]) -> Path:
    return (
        Path(str(workspace["agentbc_root"])).expanduser().resolve()
        / "tasks"
        / "inputs"
        / str(workspace["task_date"])
        / str(workspace["task_code"])
        / str(workspace.get("task_id") or _task_id_from_workspace(workspace))
    )


def prepare_task_inputs(
    *,
    images: Iterable[str | Path] | None,
    files: Iterable[str | Path] | None,
    workspace: dict[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    """Freeze explicit external inputs without participating in permission flow.

    Inputs already inside the selected project remain direct project inputs.
    Every other explicitly named regular file is copied into an AgentBC-owned,
    task-scoped directory.  There are intentionally no AgentBC count, size,
    extension, or aggregate-resource limits here.
    """

    project_root = Path(str(workspace["project_root"])).expanduser().resolve()
    input_root = canonical_task_input_root(workspace)
    staging_root = input_root.with_name(f".{input_root.name}.staging-{uuid.uuid4().hex}")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    staged_external = False
    try:
        for kind, values in (("image", images or ()), ("file", files or ())):
            for raw_value in values:
                source = _validate_source(raw_value)
                key = (kind, str(source))
                if key in seen:
                    continue
                seen.add(key)
                source_scope = "project" if _is_within(source, project_root) else "external_import"
                if source_scope == "project":
                    digest, size = _hash_stable_file(source)
                    materialized = source
                else:
                    staging_root.mkdir(parents=True, exist_ok=True)
                    digest, size, staged_path = _copy_stable_file(source, staging_root)
                    suffix = source.suffix
                    materialized = input_root / f"{digest}{suffix}"
                    final_staged = staging_root / materialized.name
                    if staged_path != final_staged:
                        if final_staged.exists():
                            staged_path.unlink()
                        else:
                            staged_path.rename(final_staged)
                    staged_external = True
                entries.append(
                    {
                        "input_id": f"input-{len(entries) + 1:03d}",
                        "kind": kind,
                        "display_name": source.name,
                        "source_scope": source_scope,
                        "media_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                        "size_bytes": size,
                        "sha256": digest,
                        "materialized_path": str(materialized),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        if staged_external:
            input_root.parent.mkdir(parents=True, exist_ok=True)
            if input_root.exists():
                raise ABCError("input_stage_failed", "task input root already exists")
            staging_root.rename(input_root)
            committed_root: Path | None = input_root
        else:
            committed_root = None
        extension = (
            {INPUTS_EXTENSION_KEY: {"version": INPUTS_VERSION, "entries": entries}}
            if entries
            else {}
        )
        return extension, committed_root
    except ABCError:
        shutil.rmtree(staging_root, ignore_errors=True)
        _remove_empty_input_parents(input_root)
        raise
    except OSError as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        _remove_empty_input_parents(input_root)
        raise ABCError("input_stage_failed", f"could not stage task input: {exc}") from exc


def task_input_paths(task_packet: dict[str, Any], *, kind: str | None = None) -> list[Path]:
    entries = _manifest_entries(task_packet)
    return [
        Path(str(entry["materialized_path"])).expanduser().resolve()
        for entry in entries
        if kind is None or entry.get("kind") == kind
    ]


def task_input_sources(task_packet: dict[str, Any]) -> tuple[list[Path], list[Path]]:
    entries = _manifest_entries(task_packet)
    if entries:
        return (
            task_input_paths(task_packet, kind="image"),
            task_input_paths(task_packet, kind="file"),
        )
    extensions = task_packet.get("extensions")
    media = extensions.get("agentbc.media") if isinstance(extensions, dict) else None
    legacy = media.get("images") if isinstance(media, dict) else None
    images = [Path(str(value)).expanduser().resolve() for value in legacy or [] if str(value).strip()]
    return images, []


def public_inputs_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != INPUTS_VERSION:
        return {}
    entries = value.get("entries")
    if not isinstance(entries, list):
        return {}
    public_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        public_entries.append(
            {
                key: entry.get(key)
                for key in (
                    "input_id",
                    "kind",
                    "display_name",
                    "source_scope",
                    "media_type",
                    "size_bytes",
                    "sha256",
                    "created_at",
                )
            }
        )
    return {"version": INPUTS_VERSION, "entries": public_entries}


def cleanup_task_input_root(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
        _remove_empty_input_parents(path)


def _manifest_entries(task_packet: dict[str, Any]) -> list[dict[str, Any]]:
    extensions = task_packet.get("extensions")
    value = extensions.get(INPUTS_EXTENSION_KEY) if isinstance(extensions, dict) else None
    if not isinstance(value, dict):
        return []
    if value.get("version") != INPUTS_VERSION or not isinstance(value.get("entries"), list):
        raise ABCError("input_manifest_invalid", "task input manifest is invalid")
    entries: list[dict[str, Any]] = []
    for entry in value["entries"]:
        if not isinstance(entry, dict):
            raise ABCError("input_manifest_invalid", "task input manifest entry is invalid")
        path = str(entry.get("materialized_path") or "").strip()
        if entry.get("kind") not in {"image", "file"} or not path:
            raise ABCError("input_manifest_invalid", "task input manifest entry is incomplete")
        materialized = Path(path).expanduser().resolve()
        if not materialized.is_file():
            raise ABCError("input_payload_missing", f"task input payload is missing: {entry.get('input_id')}")
        entries.append(entry)
    return entries


def _validate_source(raw_value: str | Path) -> Path:
    raw = Path(raw_value).expanduser()
    try:
        info = raw.lstat()
    except FileNotFoundError as exc:
        raise ABCError("input_source_missing", f"input source does not exist: {raw}") from exc
    except PermissionError as exc:
        raise ABCError("input_source_unreadable", f"input source is not readable: {raw}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ABCError("input_source_symlink", f"input source must not be a symlink: {raw}")
    if not stat.S_ISREG(info.st_mode):
        raise ABCError("input_source_not_regular", f"input source must be a regular file: {raw}")
    return raw.resolve()


def _hash_stable_file(path: Path) -> tuple[str, int]:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
    except PermissionError as exc:
        raise ABCError("input_source_unreadable", f"input source is not readable: {path}") from exc
    except OSError as exc:
        raise ABCError("input_stage_failed", f"could not read task input: {exc}") from exc
    if _file_identity(before) != _file_identity(after):
        raise ABCError("input_source_changed", f"input source changed while being read: {path}")
    return digest.hexdigest(), size


def _copy_stable_file(path: Path, staging_root: Path) -> tuple[str, int, Path]:
    temporary = staging_root / f"payload-{uuid.uuid4().hex}"
    try:
        with path.open("rb") as source, temporary.open("xb") as target:
            before = os.fstat(source.fileno())
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
            after = os.fstat(source.fileno())
    except PermissionError as exc:
        temporary.unlink(missing_ok=True)
        raise ABCError("input_source_unreadable", f"input source is not readable: {path}") from exc
    if _file_identity(before) != _file_identity(after):
        temporary.unlink(missing_ok=True)
        raise ABCError("input_source_changed", f"input source changed while being copied: {path}")
    return digest.hexdigest(), size, temporary


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _task_id_from_workspace(workspace: dict[str, Any]) -> str:
    return f"{workspace['task_code']}-{workspace['iteration']}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _remove_empty_input_parents(input_root: Path) -> None:
    # task-id/input payload -> task-code -> date. Never remove the shared
    # ``tasks/inputs`` root, and never recurse through non-empty directories.
    current = input_root.parent
    for _ in range(2):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
