"""Bounded read-only task catalogue; no lifecycle refresh or report reads."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import OrderedDict
from datetime import datetime
from threading import RLock

from .protocol import ABCError
from .task_id import split_task_ref
from .web_snapshot import (
    MAX_RECORD_BYTES,
    MAX_RESPONSE_BYTES,
    SnapshotProvider,
    _json,
    project_task,
)

_ACTIVE = {
    "pending",
    "running",
    "input_required",
    "needs_recovery",
    "assigned",
    "working",
    "in_progress",
    "pause_pending",
    "paused",
    "review_required",
    "needs_review",
}
_TERMINAL = {"completed", "failed", "cancelled", "rejected"}


def task_group(status):
    if status == "completed":
        return "completed"
    if status in _ACTIVE - {"needs_recovery", "needs_review"}:
        return "active"
    return "failed"


def _sort_key(row):
    state = row.get("status")
    group = 0 if state in _ACTIVE else 2 if state in _TERMINAL else 1
    try:
        date = datetime.fromisoformat(
            (row.get("updated_at") or "").replace("Z", "+00:00")
        )
        stamp = date.timestamp() if date.tzinfo is not None else float("-inf")
    except (ValueError, OverflowError, OSError):
        stamp = float("-inf")
    return group, -stamp, row["task_id"]


class TaskCatalogue:
    def __init__(self, provider: SnapshotProvider):
        self.provider = provider
        self._cache = OrderedDict()
        self._key = secrets.token_bytes(32)
        self._lock = RLock()

    def _scan(self):
        root = self.provider._board
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            fd = os.open(root.path, flags)
            try:
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino) != root.identity:
                    raise ABCError("snapshot_source_changed", "snapshot_source_changed")
                ids = []
                for code in os.listdir(fd):
                    try:
                        split_task_ref(code)
                        code_fd = os.open(code, flags, dir_fd=fd)
                    except (ValueError, OSError):
                        continue
                    try:
                        for iteration in os.listdir(code_fd):
                            task_id = f"{code}-{iteration}"
                            try:
                                normalized, number = split_task_ref(task_id)
                            except ValueError:
                                continue
                            if number is None or task_id != f"{normalized}-{number}":
                                continue
                            ids.append(task_id)
                            if len(ids) > 5000:
                                raise ABCError(
                                    "catalogue_too_large", "catalogue_too_large"
                                )
                    finally:
                        os.close(code_fd)
            finally:
                os.close(fd)
        except OSError:
            raise ABCError(
                "snapshot_root_unavailable", "snapshot_root_unavailable"
            ) from None
        rows = []
        for task_id in ids:
            code, iteration = task_id.split("-")
            row = {
                "task_id": task_id,
                "title": None,
                "executor": None,
                "status": None,
                "updated_at": None,
                "title_truncated": False,
                "error_code": None,
            }
            try:
                data, version = root.read(
                    (code, iteration, "task.json"), MAX_RECORD_BYTES
                )
                if data is None:
                    continue
                raw = json.loads(data)
                if not isinstance(raw, dict) or raw.get("id") != task_id:
                    raise ValueError
                view = project_task(raw)
                _, after = root.read((code, iteration, "task.json"), MAX_RECORD_BYTES)
                if version != after:
                    raise ABCError("snapshot_changed", "snapshot_changed")
                for field in ("title", "executor", "status", "updated_at"):
                    value = view[field]
                    if value is not None:
                        value.encode("utf-8")
                    if field != "title" and value is not None and len(value) > 128:
                        raise ValueError
                    row[field] = value
                if row["title"] is not None:
                    row["title_truncated"] = len(row["title"]) > 256
                    row["title"] = row["title"][:256]
            except ABCError as exc:
                if exc.code in {"snapshot_source_changed", "snapshot_root_unavailable"}:
                    raise
                row["error_code"] = (
                    exc.code
                    if exc.code.startswith("snapshot_")
                    else "snapshot_record_invalid"
                )
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                row["error_code"] = "snapshot_record_invalid"
            if row["error_code"]:
                # Invalid metadata must not retain partially projected, unbounded text.
                for field in ("title", "executor", "status", "updated_at"):
                    row[field] = None
                row["title_truncated"] = False
            rows.append(row)
        return sorted(rows, key=_sort_key)

    def _cursor(self, key, offset, limit):
        payload = f"{key}.{offset}.{limit}"
        return (
            payload
            + "."
            + hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        )

    def page(self, *, limit=50, cursor=None, group="all"):
        if group not in {"all", "active", "completed", "failed"}:
            raise ABCError("invalid_group", "invalid_group")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ABCError("invalid_limit", "invalid_limit")
        with self._lock:
            now = time.monotonic()
            for key in list(self._cache):
                if self._cache[key][0] <= now:
                    del self._cache[key]
            if cursor:
                try:
                    if len(cursor) > 256:
                        raise ValueError
                    key, offset, encoded_limit, signature = cursor.split(".")
                    offset, encoded_limit = int(offset), int(encoded_limit)
                    expected = self._cursor(key, offset, encoded_limit).rsplit(".", 1)[
                        1
                    ]
                    if (
                        not hmac.compare_digest(signature, expected)
                        or encoded_limit != limit
                        or offset < 0
                    ):
                        raise ValueError
                except (ValueError, AttributeError):
                    raise ABCError("invalid_cursor", "invalid_cursor") from None
                if key not in self._cache:
                    raise ABCError("cursor_expired", "cursor_expired")
                if self._cache[key][2] != group:
                    raise ABCError("invalid_cursor", "invalid_cursor")
                rows = self._cache[key][1]
            else:
                rows = self._scan()
                if group != "all":
                    rows = [row for row in rows if task_group(row["status"]) == group]
                key, offset = secrets.token_hex(16), 0
                self._cache[key] = (time.monotonic() + 60, rows, group)
                while len(self._cache) > 4:
                    self._cache.popitem(last=False)
            end = offset + limit
            result = {
                "schema_version": 1,
                "source_id": self.provider.source_id,
                "catalogue_revision": key,
                "items": rows[offset:end],
                "next_cursor": self._cursor(key, end, limit)
                if end < len(rows)
                else None,
            }
            if len(_json(result)) > MAX_RESPONSE_BYTES:
                raise ABCError(
                    "snapshot_response_too_large", "snapshot_response_too_large"
                )
            return result
