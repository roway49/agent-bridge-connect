"""Session-first control state for executor adapters.

This module deliberately owns only task-scoped control state.  It never
searches an executor's private storage and it never infers a session ID from
recent runs, logs, ``--last`` or ``--continue``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .execution_policy import validate_execution_session_receipt


SESSION_CONTROL_VERSION = 1
SESSION_RECEIPT_FILE = "session_receipt.json"
SESSION_STATE_FILE = "state.json"
RECOVERY_FILE = "recovery.json"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")


class SessionRecoveryRequired(RuntimeError):
    """A missing, duplicate, or mismatched receipt requires explicit recovery."""

    status = "needs_recovery"

    def __init__(
        self,
        code: str,
        message: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "session_recovery_required")
        self.evidence = dict(evidence or {})
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: str | Path, value: dict[str, Any]) -> Path:
    """Atomically write one small control document with restrictive mode."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_json(path: str | Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_identifier(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a safe non-empty identifier")
    return normalized


def control_root_for_task(
    task_id: str,
    *,
    board_root: str | Path | None = None,
    explicit_root: str | Path | None = None,
) -> Path:
    """Resolve the one exact task-scoped control directory.

    ``explicit_root`` is for tests and adapters that already received the
    controller-selected directory.  No fallback directory search is allowed.
    """
    safe_task_id = _safe_identifier(task_id, field="task_id")
    if explicit_root is not None:
        root = Path(explicit_root).expanduser().resolve()
        if root.name != safe_task_id or root.parent.name != ".agentbc-control":
            raise ValueError("explicit control root is not task scoped")
        return root
    if board_root is None:
        raise ValueError("board_root or explicit_root is required")
    board = Path(board_root).expanduser().resolve()
    return board / ".agentbc-control" / safe_task_id


def normalize_official_session_receipt(
    receipt: Any,
    *,
    executor: str,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    errors = validate_execution_session_receipt(receipt, executor=executor)
    if errors:
        raise SessionRecoveryRequired(
            "session_receipt_invalid",
            "; ".join(errors),
            {"errors": errors},
        )
    normalized = {
        "version": int(receipt["version"]),
        "executor": str(receipt["executor"]).strip().lower(),
        "session_id": str(receipt["session_id"]).strip(),
        "resumed": bool(receipt["resumed"]),
        "persistence": "persistent",
        "source": str(receipt["source"]),
    }
    if expected_session_id is not None:
        expected = str(expected_session_id).strip()
        if not expected or normalized["session_id"] != expected:
            raise SessionRecoveryRequired(
                "session_receipt_session_mismatch",
                "Official session receipt does not match the explicit session ID.",
                {"expected_session_id": expected, "actual_session_id": normalized["session_id"]},
            )
    return normalized


class SessionReceiptStore:
    """Durable receipt gate bound to one task, executor run, and session."""

    def __init__(
        self,
        root: str | Path,
        *,
        task_id: str,
        executor: str,
        executor_run_id: str,
        create: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.task_id = _safe_identifier(task_id, field="task_id")
        self.executor = str(executor or "").strip().lower()
        self.executor_run_id = _safe_identifier(executor_run_id, field="executor_run_id")
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.root / "receipts").mkdir(exist_ok=True, mode=0o700)
            (self.root / "responses").mkdir(exist_ok=True, mode=0o700)
        elif not self.root.is_dir():
            raise SessionRecoveryRequired(
                "session_control_unavailable",
                "Task-scoped session control state is unavailable.",
            )

    @property
    def receipt_path(self) -> Path:
        return self.root / "receipts" / f"{self.executor_run_id}.json"

    @property
    def current_receipt_path(self) -> Path:
        return self.root / SESSION_RECEIPT_FILE

    @property
    def state_path(self) -> Path:
        return self.root / SESSION_STATE_FILE

    @property
    def recovery_path(self) -> Path:
        return self.root / RECOVERY_FILE

    def _state(self) -> dict[str, Any]:
        return read_json(self.state_path) or {
            "version": SESSION_CONTROL_VERSION,
            "task_id": self.task_id,
            "executor": self.executor,
            "status": "pending",
        }

    def _save_state(self, value: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, value)

    def persist(
        self,
        receipt: Any,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist exactly one official receipt before the first turn."""
        normalized = normalize_official_session_receipt(
            receipt,
            executor=self.executor,
            expected_session_id=expected_session_id,
        )
        if self.receipt_path.exists():
            raise SessionRecoveryRequired(
                "session_receipt_duplicate",
                "An official session receipt already exists for this executor run.",
                {"executor_run_id": self.executor_run_id},
            )

        current = read_json(self.current_receipt_path)
        if current is not None:
            current_id = str(current.get("session_id") or "").strip()
            if current_id != normalized["session_id"]:
                raise SessionRecoveryRequired(
                    "session_receipt_mismatch",
                    "A task already has a different persisted official session ID.",
                    {"existing_session_id": current_id, "actual_session_id": normalized["session_id"]},
                )
            if not normalized["resumed"]:
                raise SessionRecoveryRequired(
                    "session_receipt_duplicate",
                    "A fresh turn cannot replace an existing task session.",
                    {"session_id": current_id},
                )

        record = {
            "version": SESSION_CONTROL_VERSION,
            "task_id": self.task_id,
            "executor": self.executor,
            "executor_run_id": self.executor_run_id,
            "persisted_at": utc_now(),
            "receipt": normalized,
            **normalized,
        }
        atomic_write_json(self.receipt_path, record)
        atomic_write_json(self.current_receipt_path, record)
        state = self._state()
        state.update(
            {
                "version": SESSION_CONTROL_VERSION,
                "task_id": self.task_id,
                "executor": self.executor,
                "executor_run_id": self.executor_run_id,
                "session_id": normalized["session_id"],
                "resumed": normalized["resumed"],
                "status": "session_started",
                "receipt_path": str(self.receipt_path),
                "updated_at": utc_now(),
            }
        )
        self._save_state(state)
        return normalized

    def load_for_run(self, *, session_id: str | None = None) -> dict[str, Any]:
        record = read_json(self.receipt_path)
        if record is None:
            raise SessionRecoveryRequired(
                "session_receipt_missing",
                "The official session receipt was not persisted before the turn.",
                {"executor_run_id": self.executor_run_id},
            )
        if record.get("task_id") != self.task_id or record.get("executor_run_id") != self.executor_run_id:
            raise SessionRecoveryRequired(
                "session_receipt_run_mismatch",
                "The official session receipt belongs to a different task or run.",
                {"executor_run_id": self.executor_run_id},
            )
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else record
        normalized = normalize_official_session_receipt(
            receipt,
            executor=self.executor,
            expected_session_id=session_id,
        )
        return normalized

    def assert_before_turn(self, session_id: str) -> dict[str, Any]:
        receipt = self.load_for_run(session_id=session_id)
        state = self._state()
        if state.get("status") == "needs_recovery":
            raise SessionRecoveryRequired(
                "session_control_needs_recovery",
                "Session control state is already marked needs_recovery.",
                {"state": state},
            )
        state.update(
            {
                "status": "turn_start_allowed",
                "turn_gate_opened_at": utc_now(),
                "session_id": receipt["session_id"],
                "executor_run_id": self.executor_run_id,
                "updated_at": utc_now(),
            }
        )
        self._save_state(state)
        return receipt

    def mark_recovery(
        self,
        code: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "version": SESSION_CONTROL_VERSION,
            "task_id": self.task_id,
            "executor": self.executor,
            "executor_run_id": self.executor_run_id,
            "code": str(code or "session_recovery_required"),
            "message": str(message or "Session control requires recovery."),
            "evidence": dict(evidence or {}),
            "created_at": utc_now(),
        }
        previous = read_json(self.recovery_path)
        history = previous.get("history") if isinstance(previous, dict) else []
        if not isinstance(history, list):
            history = []
        history.append(event)
        atomic_write_json(self.recovery_path, {"version": SESSION_CONTROL_VERSION, "history": history[-32:], "latest": event})
        state = self._state()
        state.update(
            {
                "status": "needs_recovery",
                "recovery_code": event["code"],
                "recovery_message": event["message"],
                "updated_at": utc_now(),
            }
        )
        self._save_state(state)
        return event


class SessionFirstGate:
    """Small adapter-facing facade for the receipt-before-turn invariant."""

    def __init__(
        self,
        root: str | Path,
        *,
        task_id: str,
        executor: str,
        executor_run_id: str,
        expected_session_id: str | None = None,
        create: bool = True,
    ) -> None:
        self.expected_session_id = (
            str(expected_session_id).strip() if expected_session_id is not None else None
        )
        self.store = SessionReceiptStore(
            root,
            task_id=task_id,
            executor=executor,
            executor_run_id=executor_run_id,
            create=create,
        )

    @property
    def root(self) -> Path:
        return self.store.root

    def persist_official_receipt(self, receipt: Any) -> dict[str, Any]:
        try:
            return self.store.persist(receipt, expected_session_id=self.expected_session_id)
        except SessionRecoveryRequired as exc:
            self.store.mark_recovery(exc.code, str(exc), evidence=exc.evidence)
            raise

    persist_receipt = persist_official_receipt

    def require_before_turn(self, session_id: str | None = None) -> dict[str, Any]:
        exact = str(session_id or self.expected_session_id or "").strip()
        if not exact:
            raise SessionRecoveryRequired(
                "session_id_required",
                "The first turn requires the exact persisted official session ID.",
            )
        if self.expected_session_id is not None and exact != self.expected_session_id:
            raise SessionRecoveryRequired(
                "session_receipt_session_mismatch",
                "Turn session ID does not match the explicit resume session ID.",
                {"expected_session_id": self.expected_session_id, "actual_session_id": exact},
            )
        return self.store.assert_before_turn(exact)

    before_turn = require_before_turn

    def mark_recovery(self, code: str, message: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.store.mark_recovery(code, message, evidence=evidence)


__all__ = [
    "RECOVERY_FILE",
    "SESSION_CONTROL_VERSION",
    "SessionFirstGate",
    "SessionReceiptStore",
    "SessionRecoveryRequired",
    "atomic_write_json",
    "control_root_for_task",
    "normalize_official_session_receipt",
    "read_json",
    "utc_now",
]
