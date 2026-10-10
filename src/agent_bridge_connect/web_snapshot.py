"""Read-only, bounded snapshots for browser and future desktop clients.

No TaskService/Runner, reconciliation, report generation, or runtime writes.
Roots are trusted startup configuration, never request parameters. This first
provider targets POSIX hosts/containers; unsupported safe file APIs fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from .execution_policy import public_task_view
from .protocol import ABCError
from .task_id import split_task_ref

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
# Extra defense for public free text; not a general-purpose DLP guarantee.
_CREDENTIAL = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:authorization|[A-Za-z0-9_.-]{0,128}(?:api[_-]?key|token|secret|password|passwd|pwd)(?:_hash)?)\s*[:=]\s*"
    r'(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;]+)'
)
_OPENAI_KEY = re.compile(r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}")
_PASSWORD_WORD = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd)(?:_hash)?(?![A-Za-z0-9])"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


class TextSnapshot(TypedDict):
    state: str
    scope: str
    text: str | None
    truncated: bool


class _TimingSnapshot(TypedDict, total=False):
    duration: dict[str, Any]


class TaskSnapshot(_TimingSnapshot):
    task_id: str
    attempt_id: str | None
    run_id: str | None
    title: str | None
    executor: str | None
    status: str | None
    updated_at: str | None
    steps: list[dict[str, Any]]
    waiting: dict[str, Any] | None
    error: dict[str, Any] | None
    callback_state: str
    capabilities: dict[str, str]


class Snapshot(TypedDict):
    schema_version: int
    source_id: str
    snapshot_revision: str
    captured_at: str
    consistency: str
    task: TaskSnapshot
    text: dict[str, TextSnapshot]


def _error(code: str) -> ABCError:
    # Do not include private filesystem paths or exception contents.
    return ABCError(code, code, {"retryable": code == "snapshot_changed"})


def _json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = _OPENAI_KEY.sub("[REDACTED]", value)
    value = _CREDENTIAL.sub("[REDACTED]", _BEARER.sub("[REDACTED]", value))
    return _PASSWORD_WORD.sub("[REDACTED]", value)


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _Root:
    def __init__(self, path: str | Path):
        try:
            self.path = Path(path).expanduser().resolve(strict=True)
            s = self.path.stat()
            if not stat.S_ISDIR(s.st_mode):
                raise OSError
            self.identity = (s.st_dev, s.st_ino)
        except (OSError, RuntimeError):
            raise _error("snapshot_root_unavailable") from None

    def read(self, parts: tuple[str, ...], limit: int) -> tuple[bytes | None, Any]:
        """Open below a pinned root by dirfd; refuse symlinks and special files."""
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise _error("snapshot_platform_unsupported")
        fds: list[int] = []
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(self.path, flags | os.O_DIRECTORY)
            fds.append(fd)
            s = os.fstat(fd)
            if (s.st_dev, s.st_ino) != self.identity:
                raise _error("snapshot_source_changed")
            for part in parts[:-1]:
                fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                fds.append(fd)
            fd = os.open(parts[-1], flags, dir_fd=fd)
            fds.append(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise _error("snapshot_file_unavailable")
            if before.st_size > limit:
                raise _error("snapshot_file_too_large")
            chunks = bytearray()
            while len(chunks) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > limit:
                raise _error("snapshot_file_too_large")
            after = os.fstat(fd)
            if _stamp(before) != _stamp(after):
                raise _error("snapshot_changed")
            data = bytes(chunks)
            return data, (_stamp(after), hashlib.sha256(data).hexdigest())
        except FileNotFoundError:
            if not fds:
                raise _error("snapshot_root_unavailable") from None
            return None, None
        except OSError:
            raise _error("snapshot_file_unavailable") from None
        finally:
            for fd in reversed(fds):
                os.close(fd)


def project_task(raw: dict[str, Any]) -> TaskSnapshot:
    """Project one captured record; do not read live state or infer completion."""
    # Reuse the pure Core public projection, then narrow to the Web DTO.
    view = public_task_view(raw)
    ext = _object(view.get("extensions"))
    original_ext = _object(raw.get("extensions"))
    execution = _object(ext.get("agentbc.execution"))
    attempt = execution.get("attempt_index")
    run = _text(execution.get("executor_run_id")) or _text(
        execution.get("worker_run_id")
    )
    callback = original_ext.get("agentbc.final_callback")
    if callback is None:
        callback_state = "missing"
    elif (
        isinstance(callback, dict)
        and callback.get("task_id") == raw.get("id")
        and callback.get("executor_run_id") == run
        and run
        and callback.get("marker_valid") is True
    ):
        callback_state = "received"
    else:
        callback_state = "unknown"
    request = _object(ext.get("agentbc.input"))
    errors = view.get("errors")
    failure = _object(errors[-1]) if isinstance(errors, list) and errors else {}
    steps = view.get("steps", [])
    if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
        raise _error("snapshot_record_invalid")
    executor = _text(view.get("assignee"))
    return {
        "task_id": raw["id"],
        "attempt_id": f"attempt-{attempt}"
        if type(attempt) is int and attempt >= 0
        else None,
        "run_id": run,
        "title": _text(view.get("title")),
        "executor": executor,
        "status": _text(view.get("status")),
        "updated_at": _text(view.get("updated_at")),
        "steps": [
            {
                "id": s.get("id") if type(s.get("id")) is int else None,
                "description": _text(
                    s.get("description") if "description" in s else s.get("action")
                ),
                "status": _text(s.get("status")),
            }
            for s in steps
        ],
        "waiting": (
            {k: _text(request.get(k)) for k in ("type", "status", "summary")}
            if request
            else None
        ),
        "error": (
            {k: _text(failure.get(k)) for k in ("code", "message")} if failure else None
        ),
        "callback_state": callback_state,
        # This is an integration boundary, NOT discovery or live health.
        "capabilities": {
            "execution_scope": "basic" if executor == "opencode" else "unknown",
            "full_lifecycle": "unverified" if executor == "opencode" else "unknown",
        },
    }


class SnapshotProvider:
    """One instance per server lifetime. Replacement creates a new source_id."""

    def __init__(
        self, board_root: str | Path, *, report_root: str | Path | None = None
    ):
        self._board = _Root(board_root)
        self._reports = _Root(report_root) if report_root is not None else None
        self.source_id = uuid.uuid4().hex

    def snapshot(self, task_id: str, *, full_report=False) -> Snapshot:
        try:
            code, iteration = split_task_ref(task_id)
            if iteration is None or task_id != f"{code}-{iteration}":
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise _error("snapshot_task_id_invalid") from None
        for _ in range(2):
            try:
                return self._capture(
                    task_id, (code, iteration, "task.json"), full_report=full_report
                )
            except ABCError as exc:
                if exc.code != "snapshot_changed":
                    raise
        raise _error("snapshot_changed")

    def report_document(self, task_id: str) -> bytes:
        # The same verified task/report binding, delivered only on explicit open.
        return self.snapshot(task_id, full_report=True)["text"]["report"][
            "text"
        ].encode("utf-8")

    def _timing(self, raw: dict, parts: tuple[str, ...], now: str):
        from .run_lease import RunLease
        from .timing_view import build_timing_view
        from .notifications import format_duration_seconds

        captured = []
        versions = []
        for filename in ("run_lease.json", "interventions.jsonl"):
            data, version = self._board.read((*parts[:2], filename), MAX_RECORD_BYTES)
            captured.append(data)
            versions.append(version)
        try:
            lease = RunLease(**json.loads(captured[0])) if captured[0] else None
            if lease is not None and lease.task_id != raw["id"]:
                raise ValueError
            interventions = [
                json.loads(line)
                for line in (captured[1] or b"").splitlines()
                if line.strip()
            ]
            timing = build_timing_view(
                raw, self._board.path, now=now, captured_inputs=(lease, interventions)
            )
            retry = timing["attempt_index"] > 0
            seconds = (
                timing["attempt_execution_duration_s"]
                if retry
                else timing["wall_duration_s"]
            )
            result = {
                "seconds": seconds,
                "label": "本次执行时长" if retry else "任务历时",
                "display": format_duration_seconds(seconds),
            }
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            result = {"seconds": None, "label": "任务历时", "display": "unknown"}
        for filename, version in zip(
            ("run_lease.json", "interventions.jsonl"), versions
        ):
            _, after = self._board.read((*parts[:2], filename), MAX_RECORD_BYTES)
            if version != after:
                raise _error("snapshot_changed")
        return result

    def _report(self, raw: dict, code: str, *, full=False) -> tuple[TextSnapshot, Any]:
        empty: TextSnapshot = {
            "state": "unavailable",
            "scope": "none",
            "text": None,
            "truncated": False,
        }
        if self._reports is None:
            return empty, None
        value = _object(raw.get("workspace")).get("report_file")
        if not isinstance(value, str) or not value:
            return {**empty, "state": "missing"}, None
        try:
            path = Path(value)
            # Lexical containment; dirfd opening below refuses every symlink.
            parts = path.relative_to(self._reports.path).parts
            if (
                len(parts) != 3
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0])
                or parts[1] != code
                or parts[2] != f"{raw['id']}-report.md"
            ):
                return empty, "untrusted_path"
            data, version = self._reports.read(parts, MAX_REPORT_BYTES)
            if data is None:
                return {**empty, "state": "missing"}, version
            clean = _text(data.decode("utf-8")) or ""
            encoded = clean.encode("utf-8")
            return {
                "state": "available",
                "scope": "task",
                "text": encoded[: MAX_REPORT_BYTES if full else MAX_TEXT_BYTES].decode(
                    "utf-8", errors="ignore"
                ),
                "truncated": len(encoded) > MAX_TEXT_BYTES,
            }, version
        except (ValueError, UnicodeError):
            return empty, "invalid_report"
        except ABCError as exc:
            if exc.code in {"snapshot_changed", "snapshot_source_changed"}:
                raise
            return empty, exc.code

    def _capture(
        self, task_id: str, parts: tuple[str, ...], *, full_report=False
    ) -> Snapshot:
        data, version = self._board.read(parts, MAX_RECORD_BYTES)
        if data is None:
            raise _error("snapshot_task_not_found")
        try:
            raw = json.loads(data)
            if not isinstance(raw, dict) or raw.get("id") != task_id:
                raise ValueError
            task = project_task(raw)
        except ABCError:
            raise _error("snapshot_record_invalid") from None
        except (ValueError, TypeError, KeyError, RecursionError, AttributeError):
            raise _error("snapshot_record_invalid") from None
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        task["duration"] = self._timing(raw, parts, now)
        report, report_version = self._report(raw, parts[0])
        _, after_report = self._report(raw, parts[0])
        if task["duration"] != self._timing(raw, parts, now):
            raise _error("snapshot_changed")
        _, after_task = self._board.read(parts, MAX_RECORD_BYTES)
        if version != after_task or report_version != after_report:
            raise _error("snapshot_changed")
        if full_report:
            report, full_version = self._report(raw, parts[0], full=True)
            _, full_task_version = self._board.read(parts, MAX_RECORD_BYTES)
            if full_version != report_version or full_task_version != version:
                raise _error("snapshot_changed")
            if report["state"] != "available":
                raise _error("snapshot_task_not_found")
            return {"text": {"report": report}}
        body = {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "consistency": "consistent",
            "task": task,
            "text": {
                "report": report,
                "execution": {
                    "state": "unavailable",
                    "scope": "none",
                    "text": None,
                    "truncated": False,
                },
            },
        }
        try:
            revision = hashlib.sha256(_json(body)).hexdigest()
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise _error("snapshot_record_invalid") from None
        result: Snapshot = {
            **body,
            "snapshot_revision": revision,
            "captured_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        if len(_json(result)) > MAX_RESPONSE_BYTES:
            raise _error("snapshot_response_too_large")
        return result
