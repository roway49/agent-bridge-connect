"""FLOW-104-003: mechanical handoff recovery for a failed chain head.

A ``failed``/``needs_recovery`` task that is the exact head of its chain is a
valid handoff source.  Recovery is mechanical: this module imports the source
task brief and canonical failure report verbatim, derives the locked and
remaining steps from the authoritative task record (never from dispatcher
prose), and produces the deterministic recovery record that TaskService, the
task brief and the shared Executor prompt contract project.

Design rules that this module owns and must not weaken:

- The source task stays terminal.  Its task brief, failure report, artifacts,
  events, terminal receipt and cleanup evidence are never rewritten by a
  handoff; the failure evidence is imported, not cleared.
- Step states come from the task record.  ``done``/``completed`` source steps
  become locked ``inherited_done`` steps that must never execute again and may
  only be reported as ``done`` in the final callback.  Every other source step
  (``failed``, ``blocked``, ``pending``, unknown) resets to ``pending``.
- When the report step statuses disagree with the task record the handoff
  continues from the task record and the disagreement is persisted as
  ``source_report_step_mismatch`` instead of being resolved by prose.
- A missing failure report is regenerated canonically from the authoritative
  task state and terminal receipt.  Unreadable requirements, an inconsistent
  lineage and an invalid PathPlan fail closed with the shared stable revival
  error code :data:`HANDOFF_RECOVERY_REVIVAL_ERROR`.
- Exactly one recovery iteration is created per source, even when the request
  is duplicated or replayed concurrently.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .execution_contract import INHERITED_DONE_STATUS
from .execution_policy import RESOURCE_EXTENSION_KEY
from .path_model import validate_path_plan_workspace
from .protocol import ABCError, TaskModel

HANDOFF_RECOVERY_EXTENSION_KEY = "agentbc.handoff_recovery"
HANDOFF_RECOVERY_VERSION = 1
HANDOFF_RECOVERY_SOURCE_STATUSES = frozenset({"failed", "needs_recovery"})
HANDOFF_RECOVERY_REVIVAL_ERROR = "handoff_revival_invalid"
SOURCE_REPORT_STEP_MISMATCH = "source_report_step_mismatch"
HANDOFF_RECOVERY_LOCK_NAME = ".handoff-recovery.lock"
HANDOFF_RECOVERY_LOCK_WAIT_S = 5.0
HANDOFF_RECOVERY_LOCK_RETRY_S = 0.01
HANDOFF_RECOVERY_LOCK_STALE_S = 30.0
HANDOFF_RECOVERY_SNAPSHOT_DIR = "handoff"
HANDOFF_RECOVERY_SOURCE_BRIEF_NAME = "source-task.md"
HANDOFF_RECOVERY_SOURCE_REPORT_NAME = "source-report.md"
TERMINAL_VERIFICATION_STEP_STATUS = "pending"
# A source step is already complete when the task record says so directly or
# when it was itself imported as locked inherited work by an earlier recovery
# iteration; a chained re-failure must never unlock inherited work.
INHERITED_DONE_DONE_STEP_STATUSES = frozenset({"done", "completed", INHERITED_DONE_STATUS})

_ADDITIVE_MESSAGE_LIMIT = 240

_REPORT_STEP_LINE_RE = re.compile(r"^(\d+)\.\s+\[([a-z_]+)\]\s", re.MULTILINE)
_REPORT_STEPS_HEADING = "## Steps"


def is_handoff_recovery_source(status: str) -> bool:
    """Return True when ``status`` makes a task a failed-handoff source."""
    return str(status or "").strip().lower() in HANDOFF_RECOVERY_SOURCE_STATUSES


def revival_error(reason: str, message: str, details: dict[str, Any] | None = None) -> ABCError:
    """Return the shared stable revival failure for a non-revivable source.

    Every mechanical preflight failure of a failed-handoff source (unreadable
    requirements, inconsistent lineage, invalid PathPlan, empty requirements)
    raises this single stable code so callers, status projections and tests can
    branch on one contract instead of per-cause prose.
    """
    payload = {"reason": str(reason or "handoff_revival_invalid")}
    if details:
        payload.update(details)
    return ABCError(HANDOFF_RECOVERY_REVIVAL_ERROR, message, payload)


@dataclass(frozen=True)
class RecoverySource:
    """Immutable mechanical snapshot of a failed handoff source."""

    task_id: str
    status: str
    assignee: str
    failure_code: str
    task_brief_path: str
    task_brief_sha256: str
    task_brief_bytes: int
    report_path: str
    report_sha256: str
    report_bytes: int
    report_regenerated: bool
    report_step_statuses: dict[int, str] = field(default_factory=dict)
    step_mismatches: tuple[dict[str, Any], ...] = ()
    source_steps: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "assignee": self.assignee,
            "failure_code": self.failure_code,
            "task_brief_path": self.task_brief_path,
            "task_brief_sha256": self.task_brief_sha256,
            "task_brief_bytes": self.task_brief_bytes,
            "report_path": self.report_path,
            "report_sha256": self.report_sha256,
            "report_bytes": self.report_bytes,
            "report_regenerated": self.report_regenerated,
            "report_step_statuses": {
                str(step_id): status
                for step_id, status in sorted(self.report_step_statuses.items())
            },
            "step_mismatches": [dict(item) for item in self.step_mismatches],
        }


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path, reason: str, label: str) -> bytes:
    try:
        return path.expanduser().read_bytes()
    except OSError as exc:
        raise revival_error(
            reason,
            f"{label} is unreadable: {path}",
            {"path": str(path), "error": str(exc)},
        ) from exc


def _report_steps_section(text: str) -> str:
    """Return the canonical ``## Steps`` markdown section of a report."""
    start = text.find(_REPORT_STEPS_HEADING)
    if start < 0:
        return ""
    start = text.find("\n", start)
    start = start + 1 if start >= 0 else len(text)
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def parse_report_step_statuses(report_text: str) -> dict[int, str]:
    """Parse ``<n>. [status] description`` lines from the canonical report."""
    statuses: dict[int, str] = {}
    for match in _REPORT_STEP_LINE_RE.finditer(_report_steps_section(report_text)):
        step_id = int(match.group(1))
        if step_id not in statuses:
            statuses[step_id] = match.group(2)
    return statuses


def _latest_failure_code(task: TaskModel) -> str:
    errors = [item for item in (task.errors or []) if isinstance(item, dict)]
    return str(errors[-1].get("code") or "") if errors else ""


def read_recovery_source(
    task: TaskModel,
    *,
    board_root: str | Path,
    regenerate_report: Callable[[str, Path], None] | None = None,
) -> RecoverySource:
    """Import the source task brief and canonical failure report.

    The report is read from the authoritative PathPlan ``report_file``.  When
    it is absent Core regenerates it canonically from the task state and the
    terminal receipt instead of inventing recovery evidence.
    """
    workspace = task.workspace or {}
    validate_path_plan_workspace(workspace)
    task_brief_path = Path(str(workspace.get("task_file") or "")).expanduser()
    if not str(workspace.get("task_file") or "").strip():
        raise revival_error(
            "requirements_unreadable",
            f"Task {task.id} has no task brief in its PathPlan",
            {"task_id": task.id},
        )
    task_brief = _read_bytes(
        task_brief_path, "requirements_unreadable", f"Task brief for {task.id}"
    )

    report_path = Path(str(workspace.get("report_file") or "")).expanduser()
    report_regenerated = False
    if not str(workspace.get("report_file") or "").strip():
        raise revival_error(
            "path_plan_invalid",
            f"Task {task.id} has no canonical report path in its PathPlan",
            {"task_id": task.id},
        )
    if not report_path.is_file():
        report_regenerated = True
        regenerator = regenerate_report or _default_regenerate_report
        try:
            regenerator(task.id, Path(board_root).expanduser().resolve())
        except ABCError:
            raise
        except Exception as exc:  # noqa: BLE001 - regeneration must fail closed
            raise revival_error(
                "report_unwritable",
                f"Could not regenerate the canonical failure report for {task.id}: {exc}",
                {"task_id": task.id, "report_file": str(report_path)},
            ) from exc
        if not report_path.is_file():
            raise revival_error(
                "report_unreadable",
                f"Canonical failure report for {task.id} is unavailable: {report_path}",
                {"task_id": task.id, "report_file": str(report_path)},
            )
    report = _read_bytes(
        report_path, "report_unreadable", f"Failure report for {task.id}"
    )

    source_steps = tuple(
        dict(step) for step in (task.steps or []) if isinstance(step, dict)
    )
    if not source_steps:
        raise revival_error(
            "requirements_empty",
            f"Task {task.id} has no requirements to import",
            {"task_id": task.id},
        )
    statuses = parse_report_step_statuses(report.decode("utf-8", errors="replace"))
    mismatches = []
    for step in source_steps:
        step_id = step.get("id")
        record_status = str(step.get("status") or "pending")
        report_status = statuses.get(step_id) if isinstance(step_id, int) else None
        if report_status is None:
            mismatches.append(
                {
                    "step_id": step_id,
                    "task_status": record_status,
                    "report_status": "",
                    "reason": "missing_from_report",
                }
            )
        elif report_status != record_status:
            mismatches.append(
                {
                    "step_id": step_id,
                    "task_status": record_status,
                    "report_status": report_status,
                    "reason": "status_differs",
                }
            )
    return RecoverySource(
        task_id=task.id,
        status=str(task.status or ""),
        assignee=str(task.assignee or ""),
        failure_code=_latest_failure_code(task),
        task_brief_path=str(task_brief_path),
        task_brief_sha256=_sha256_hex(task_brief),
        task_brief_bytes=len(task_brief),
        report_path=str(report_path),
        report_sha256=_sha256_hex(report),
        report_bytes=len(report),
        report_regenerated=report_regenerated,
        report_step_statuses=statuses,
        step_mismatches=tuple(mismatches),
        source_steps=source_steps,
    )


def _default_regenerate_report(task_id: str, board_root: Path) -> None:
    from .reports import write_report_markdown

    write_report_markdown(task_id, board_root)


def source_status_of(step: dict[str, Any]) -> str:
    return str(step.get("status") or "pending")


def is_locked_inherited_step(step: dict[str, Any]) -> bool:
    return (
        str(step.get("status") or "") == INHERITED_DONE_STATUS
        or step.get("locked") is True
    )


def plan_recovery_steps(source: RecoverySource) -> list[dict[str, Any]]:
    """Map source steps onto locked inherited and executable remaining steps.

    ``done``/``completed`` source steps become locked ``inherited_done`` steps;
    every other source step resets to ``pending``.  When every source step is
    already complete but the source still terminated as a failure, exactly one
    terminal-verification closeout step is appended, because the failure can
    only have happened in the callback/transport/terminal-delivery path.
    """
    steps: list[dict[str, Any]] = []
    all_done = True
    for step in source.source_steps:
        origin_status = source_status_of(step)
        locked = origin_status in INHERITED_DONE_DONE_STEP_STATUSES
        if not locked:
            all_done = False
        planned: dict[str, Any] = {
            "id": step.get("id"),
            "description": str(step.get("description") or ""),
            "status": INHERITED_DONE_STATUS if locked else "pending",
            "origin_task_id": source.task_id,
            "origin_step_id": step.get("id"),
            "origin_status": origin_status,
        }
        if locked:
            planned["locked"] = True
        steps.append(planned)
    if (
        all_done
        and source.source_steps
        and not any(step.get("terminal_verification") is True for step in source.source_steps)
    ):
        next_id = max(
            (
                item.get("id")
                for item in steps
                if isinstance(item.get("id"), int)
            ),
            default=0,
        ) + 1
        steps.append(
            {
                "id": next_id,
                "description": TERMINAL_VERIFICATION_STEP_TEXT,
                "status": TERMINAL_VERIFICATION_STEP_STATUS,
                "origin_task_id": source.task_id,
                "origin_step_id": None,
                "origin_status": source.status,
                "terminal_verification": True,
            }
        )
    return steps


TERMINAL_VERIFICATION_STEP_TEXT = (
    "Terminal-verification closeout: every inherited step is already done, so do not "
    "re-execute inherited work. Verify the inherited deliverables and the imported "
    "failure report still exist under the artifact and report roots above, then emit "
    "exactly one valid AGENTBC_FINAL_CALLBACK that reports every step as done."
)


def locked_step_ids(steps: list[dict[str, Any]]) -> list[int]:
    return [
        int(step["id"])
        for step in steps
        if isinstance(step.get("id"), int) and is_locked_inherited_step(step)
    ]


def remaining_step_ids(steps: list[dict[str, Any]]) -> list[int]:
    return [
        int(step["id"])
        for step in steps
        if isinstance(step.get("id"), int) and not is_locked_inherited_step(step)
    ]


def terminal_verification_step_id(steps: list[dict[str, Any]]) -> int | None:
    for step in steps:
        if step.get("terminal_verification") is True and isinstance(step.get("id"), int):
            return int(step["id"])
    return None


def _bounded_message(message: str | None) -> str:
    text = " ".join(str(message or "").split())
    if len(text) > _ADDITIVE_MESSAGE_LIMIT:
        return text[: _ADDITIVE_MESSAGE_LIMIT - 1].rstrip() + "…"
    return text


def build_recovery_record(
    source: RecoverySource,
    steps: list[dict[str, Any]],
    *,
    target_assignee: str,
    message: str | None,
    snapshot_paths: dict[str, str],
    created_at: str,
    permission_override: bool,
) -> dict[str, Any]:
    """Build the bounded, deterministic ``agentbc.handoff_recovery`` record."""
    return {
        "version": HANDOFF_RECOVERY_VERSION,
        "source_task_id": source.task_id,
        "source_status": source.status,
        "source_assignee": source.assignee,
        "target_assignee": target_assignee,
        "source_failure_code": source.failure_code,
        "source_task_brief": {
            "path": source.task_brief_path,
            "sha256": source.task_brief_sha256,
            "bytes": source.task_brief_bytes,
            "snapshot_path": snapshot_paths.get("task_brief", ""),
        },
        "source_report": {
            "path": source.report_path,
            "sha256": source.report_sha256,
            "bytes": source.report_bytes,
            "regenerated": source.report_regenerated,
            "snapshot_path": snapshot_paths.get("report", ""),
        },
        SOURCE_REPORT_STEP_MISMATCH: [dict(item) for item in source.step_mismatches],
        "locked_step_ids": locked_step_ids(steps),
        "remaining_step_ids": remaining_step_ids(steps),
        "terminal_verification_step_id": terminal_verification_step_id(steps),
        "additive_message": _bounded_message(message),
        "permission_override": bool(permission_override),
        "created_at": created_at,
    }


def write_source_snapshots(
    source: RecoverySource,
    *,
    report_root: str | Path,
    task_brief_bytes: bytes,
    report_bytes: bytes,
) -> dict[str, str]:
    """Persist the imported source brief/report next to the new task reports."""
    snapshot_dir = (
        Path(report_root).expanduser()
        / HANDOFF_RECOVERY_SNAPSHOT_DIR
        / source.task_id
    )
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    brief_snapshot = snapshot_dir / HANDOFF_RECOVERY_SOURCE_BRIEF_NAME
    report_snapshot = snapshot_dir / HANDOFF_RECOVERY_SOURCE_REPORT_NAME
    brief_snapshot.write_bytes(task_brief_bytes)
    report_snapshot.write_bytes(report_bytes)
    return {
        "task_brief": str(brief_snapshot),
        "report": str(report_snapshot),
    }


def read_source_bytes(source: RecoverySource) -> tuple[bytes, bytes]:
    """Re-read the imported brief and report so the snapshot is byte-exact."""
    brief = _read_bytes(
        Path(source.task_brief_path),
        "requirements_unreadable",
        f"Task brief for {source.task_id}",
    )
    report = _read_bytes(
        Path(source.report_path), "report_unreadable", f"Failure report for {source.task_id}"
    )
    return brief, report


def inherit_frozen_resources(
    source_extensions: dict[str, Any] | None,
    target_executor: str,
    *,
    created_at: str,
) -> dict[str, Any] | None:
    """Carry the source frozen resource snapshot to a same-executor iteration.

    A cross-executor handoff has no meaningful shared resource kind, so the
    target keeps the freshly built policy for its own executor.
    """
    resources = (source_extensions or {}).get(RESOURCE_EXTENSION_KEY)
    if not isinstance(resources, dict):
        return None
    if str(resources.get("executor") or "").strip().lower() != str(
        target_executor or ""
    ).strip().lower():
        return None
    inherited = dict(resources)
    inherited["exhaustion_count"] = 0
    inherited["last_decision"] = ""
    inherited["created_at"] = created_at
    return inherited


def recovery_prompt_lines(record: dict[str, Any]) -> list[str]:
    """Render the deterministic handoff-recovery block of an Executor prompt."""
    brief = record.get("source_task_brief") or {}
    report = record.get("source_report") or {}
    locked = [str(item) for item in record.get("locked_step_ids") or []]
    remaining = [str(item) for item in record.get("remaining_step_ids") or []]
    closeout = record.get("terminal_verification_step_id")
    mismatches = record.get(SOURCE_REPORT_STEP_MISMATCH) or []
    lines = [
        "Handoff recovery:",
        f"- Source task: {record.get('source_task_id', '')} "
        f"(status: {record.get('source_status', '')}, "
        f"failure code: {record.get('source_failure_code') or 'none'})",
        f"- Imported source task brief: {brief.get('path', '')} "
        f"(sha256 {brief.get('sha256', '')})",
        f"- Imported source report: {report.get('path', '')} "
        f"(sha256 {report.get('sha256', '')})",
    ]
    if report.get("regenerated") is True:
        lines.append(
            "- The source failure report was missing and was regenerated canonically "
            "from the authoritative task state and terminal receipt."
        )
    lines.append(
        f"- Locked inherited steps (already done, never re-execute): "
        f"{', '.join(locked) if locked else 'none'}"
    )
    lines.append(
        f"- Remaining executable steps: {', '.join(remaining) if remaining else 'none'}"
    )
    lines.append(
        f"- Terminal-verification closeout step: "
        f"{closeout if isinstance(closeout, int) else 'none'}"
    )
    if mismatches:
        lines.append(
            "- Source report step mismatch (task state is authoritative): "
            + ", ".join(
                f"step {item.get('step_id')} report={item.get('report_status') or 'missing'} "
                f"task={item.get('task_status')}"
                for item in mismatches
            )
        )
    message = str(record.get("additive_message") or "")
    lines.append(
        f"- Additive handoff goal: {message if message else 'none'}"
    )
    lines.extend(
        [
            "- Never re-execute a locked inherited step; report it as done only.",
            "- The additive handoff goal may add an objective; it cannot change, remove "
            "or reorder the inherited requirements and cannot choose the resume step.",
        ]
    )
    return lines


def has_recovery_child(tasks: list[TaskModel], source_task_id: str) -> TaskModel | None:
    """Return the already-created recovery iteration for ``source_task_id``."""
    children = [
        task
        for task in tasks
        if str((task.extensions or {}).get("agentbc.lineage", {}).get("parent_task_id") or "")
        == source_task_id
    ]
    if not children:
        return None
    return sorted(children, key=lambda item: item.id)[-1]


class HandoffRecoveryLock:
    """Chain-scoped advisory lock that serialises recovery-iteration creation.

    The critical section only writes a handful of files, so a concurrent
    replay waits a bounded moment and then converges on the iteration the
    winner created instead of allocating a second one.  A lock left behind by
    a crashed process is broken once it is older than
    :data:`HANDOFF_RECOVERY_LOCK_STALE_S`; the duplicate re-check inside the
    critical section still guarantees at most one iteration.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def _break_stale_lock(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age > HANDOFF_RECOVERY_LOCK_STALE_S:
            try:
                self.path.unlink()
            except OSError:
                pass

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + HANDOFF_RECOVERY_LOCK_WAIT_S
        while True:
            self._break_stale_lock()
            try:
                self._fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ABCError(
                        "handoff_in_progress",
                        f"A handoff for chain {self.path.parent.name} is already in progress",
                        {"lock_file": str(self.path)},
                    ) from None
                time.sleep(HANDOFF_RECOVERY_LOCK_RETRY_S)
                continue
            os.write(self._fd, b"handoff-recovery\n")
            return

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "HandoffRecoveryLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def handoff_lock_path(board_root: str | Path, task_code: str) -> Path:
    return (
        Path(board_root).expanduser().resolve()
        / str(task_code or "").strip()
        / HANDOFF_RECOVERY_LOCK_NAME
    )


def rollback_created_iteration(task_dir: Path) -> None:
    """Remove a partially created iteration after a failed recovery attach."""
    shutil.rmtree(task_dir, ignore_errors=True)
