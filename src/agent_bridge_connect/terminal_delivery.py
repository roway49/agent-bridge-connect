"""Independent terminal delivery (FLOW-104-002).

This module owns the bounded durable ``agentbc.terminal_delivery`` receipt that
is created in the *same authoritative task write* that records a business
task-end state (``completed`` / ``failed`` / ``cancelled`` / ``rejected`` /
``needs_recovery``).

Why it exists
-------------

Before FLOW-104-002 the terminal side effects were serially coupled inside
:meth:`TaskService.finalize_task_from_agent`: report generation, record
compaction and index refresh ran as one operation, and a missing report rewrote
a confirmed ``completed`` task as ``failed``.  Session cleanup additionally
required ``report_written`` *and* ``notification_recorded`` evidence, so a
report/notifications failure silently blocked executor-session cleanup forever.

The receipt makes every terminal side effect an independently catchable stage:

- ``report``            - canonical Markdown projection for the user
- ``record``            - 50 KiB terminal record compaction / budget
- ``index``             - board ``TASK_INDEX.md`` / ``task_index.jsonl``
- ``file_notification`` - board ``notifications.jsonl`` side channel
- ``ui_notification``   - the terminal dialog / OS notification

Design rules
------------

1. One stable ``delivery_id`` per terminal outcome; terminal state, event and
   commit time are frozen when the receipt is created.
2. Stages carry ``pending`` / ``in_progress`` / ``retry_wait`` / ``succeeded`` /
   ``not_applicable`` plus bounded ``attempts``, ``next_attempt_at``,
   ``last_error_code`` and ``updated_at``.
3. The receipt never carries raw commands, prompts, notification bodies,
   secrets or private paths.  Error reasons reduce to stable lowercase codes.
4. The receipt survives 50 KiB terminal-record compaction (see
   :func:`record_management._compact_terminal_extensions`).
5. ``agentbc.session.cleanup`` and the auxiliary cleanup receipts stay the sole
   cleanup authority; this receipt never duplicates their state.
6. Only Runner replays incomplete stages. ``input_required`` is interactive and
   never enters this pipeline; ``needs_recovery`` is a task-end outcome and does.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .protocol import ABCError

TERMINAL_DELIVERY_EXTENSION_KEY = "agentbc.terminal_delivery"
TERMINAL_DELIVERY_RECEIPT_VERSION = 1

#: Business terminal states that create a delivery receipt.
TERMINAL_DELIVERY_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "rejected", "needs_recovery"}
)

#: Terminal events that may anchor a receipt.
TERMINAL_DELIVERY_EVENTS = frozenset(
    {
        "task.finalized",
        "task.failed",
        "task.cancelled",
        "task.rejected",
        "task.recovery_required",
    }
)

#: The notification event bound to each business terminal state.  The receipt is
#: the single authority for what a terminal notification says, so Runner
#: maintenance can replay an unconfirmed stage without the original caller still
#: being alive.
TERMINAL_DELIVERY_NOTIFICATION_EVENTS = {
    "completed": "task.finalized",
    "cancelled": "task.finalized",
    "failed": "task.failed",
    "rejected": "task.rejected",
    "needs_recovery": "task.recovery_required",
}

#: The notification level bound to each business terminal state.
TERMINAL_DELIVERY_NOTIFICATION_LEVELS = {
    "completed": "done",
    "cancelled": "info",
    "failed": "error",
    "rejected": "info",
    "needs_recovery": "warning",
}

#: Independent, individually catchable terminal side effects.
TERMINAL_DELIVERY_STAGES = (
    "report",
    "record",
    "index",
    "file_notification",
    "ui_notification",
)

#: Notification stages are the only noninteractive stages whose delivery result
#: may be genuinely unknown after an interrupted process.
TERMINAL_DELIVERY_NOTIFICATION_STAGES = frozenset(
    {"file_notification", "ui_notification"}
)

TERMINAL_DELIVERY_STATES = frozenset(
    {"pending", "in_progress", "retry_wait", "succeeded", "not_applicable"}
)

#: Resolved stages are confirmed and must never be repeated.
RESOLVED_TERMINAL_DELIVERY_STAGES = frozenset({"succeeded", "not_applicable"})

#: Backoff schedule: immediate, then earliest 60s, then capped at 300s.
TERMINAL_DELIVERY_BACKOFF_S = (60, 300)
TERMINAL_DELIVERY_MAX_BACKOFF_S = 300
TERMINAL_DELIVERY_MAX_ATTEMPTS = 3

#: Stable, bounded stage failure codes.  Raw adapter messages never land here.
TERMINAL_DELIVERY_ERROR_CODES = frozenset(
    {
        "report_write_failed",
        "report_permission_denied",
        "report_missing",
        "record_budget_exceeded",
        "record_compaction_failed",
        "index_refresh_failed",
        "file_notification_failed",
        "ui_notification_failed",
        "delivery_stage_interrupted",
        "delivery_stage_uncertain",
        "delivery_not_applicable",
    }
)

DELIVERY_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

DELIVERY_EVENT_TYPE = "terminal.delivery"
DELIVERY_EVENTS_FILE = "delivery.jsonl"
DELIVERY_LOCK_NAME = ".delivery.lock"

DELIVERY_HEALTH_STATES = frozenset(
    {"pending", "in_progress", "retry_wait", "succeeded", "degraded", "not_applicable"}
)

_STAGE_FIELDS = ("state", "attempts", "next_attempt_at", "last_error_code", "updated_at")


class StageOutcome:
    """One bounded stage execution result.

    ``ok`` confirms the stage.  ``error_code`` must be a stable lowercase code;
    a raw exception string is never accepted.  ``delivery_uncertain`` marks a
    noninteractive notification whose result could not be observed.  ``deferred``
    marks a stage this process does not own (Runner owns terminal delivery), and
    leaves the stage ``pending`` without consuming an attempt.
    """

    __slots__ = ("ok", "error_code", "delivery_uncertain", "not_applicable", "deferred")

    def __init__(
        self,
        ok: bool,
        *,
        error_code: str = "",
        delivery_uncertain: bool = False,
        not_applicable: bool = False,
        deferred: bool = False,
    ) -> None:
        self.ok = bool(ok)
        # A successful stage carries no error code: it must never be reported
        # as if something had gone wrong.
        self.error_code = "" if self.ok else _sanitize_error_code(error_code)
        self.delivery_uncertain = bool(delivery_uncertain)
        self.not_applicable = bool(not_applicable)
        self.deferred = bool(deferred)


StageExecutor = Callable[[], StageOutcome]
StageExecutors = dict[str, StageExecutor]


# --------------------------------------------------------------------- helpers
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _add_delay(value: str, delay_s: int) -> str:
    return (_parse_utc(value) + timedelta(seconds=delay_s)).isoformat().replace(
        "+00:00", "Z"
    )


def _is_iso_utc(value: Any) -> bool:
    try:
        parsed = _parse_utc(str(value))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _sanitize_error_code(value: Any, fallback: str = "delivery_stage_interrupted") -> str:
    """Reduce an adapter reason to a stable bounded code; never echo raw text."""
    text = str(value or "").strip()
    if text in TERMINAL_DELIVERY_ERROR_CODES and DELIVERY_ERROR_CODE_RE.fullmatch(text):
        return text
    if text and DELIVERY_ERROR_CODE_RE.fullmatch(text) and "failed" in text:
        # Adapter-supplied stable code (e.g. ``index_refresh_failed``).
        return text
    return fallback


def _sanitize_stage(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in TERMINAL_DELIVERY_STAGES else ""


def _sanitize_state(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in TERMINAL_DELIVERY_STATES else ""


def _sanitize_terminal_state(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in TERMINAL_DELIVERY_STATUSES else ""


def _sanitize_event(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in TERMINAL_DELIVERY_EVENTS else ""


def _sanitize_bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))


def _sanitize_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    return text if text and _is_iso_utc(text) else ""


# --------------------------------------------------------------- receipt shape
def _new_stage(state: str = "pending") -> dict[str, Any]:
    return {
        "state": state,
        "attempts": 0,
        "next_attempt_at": "",
        "last_error_code": "",
        "updated_at": "",
    }


def build_terminal_delivery_receipt(
    task_id: str,
    *,
    terminal_state: str,
    terminal_event: str,
    committed_at: str | None = None,
    delivery_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one bounded v1 receipt with every stage ``pending``.

    ``terminal_state`` / ``terminal_event`` / ``committed_at`` are frozen: they
    describe the authoritative task write that created this receipt and are
    never rewritten by a later delivery attempt.
    """
    import uuid

    state = _sanitize_terminal_state(terminal_state)
    if not state:
        raise ABCError(
            "terminal_delivery_invalid_state",
            f"Unsupported terminal delivery state: {terminal_state}",
        )
    event = _sanitize_event(terminal_event)
    committed = _sanitize_timestamp(committed_at) or _utc_now()
    now = _sanitize_timestamp(created_at) or committed
    return {
        "version": TERMINAL_DELIVERY_RECEIPT_VERSION,
        "delivery_id": str(delivery_id or uuid.uuid4()),
        "task_id": str(task_id or "").strip(),
        "terminal_state": state,
        "terminal_event": event,
        "committed_at": committed,
        "stages": {stage: _new_stage() for stage in TERMINAL_DELIVERY_STAGES},
        "created_at": now,
        "updated_at": now,
    }


def normalize_terminal_delivery_stages(value: Any) -> dict[str, dict[str, Any]]:
    """Return a bounded, validated stage map; unknown entries are dropped."""
    source = value if isinstance(value, dict) else {}
    stages: dict[str, dict[str, Any]] = {}
    for stage in TERMINAL_DELIVERY_STAGES:
        raw = source.get(stage)
        raw = raw if isinstance(raw, dict) else {}
        state = _sanitize_state(raw.get("state")) or "pending"
        stages[stage] = {
            "state": state,
            "attempts": _sanitize_bounded_int(
                raw.get("attempts"), minimum=0, maximum=TERMINAL_DELIVERY_MAX_ATTEMPTS
            ),
            "next_attempt_at": _sanitize_timestamp(raw.get("next_attempt_at")),
            "last_error_code": _sanitize_error_code(raw.get("last_error_code"), "")
            if str(raw.get("last_error_code") or "").strip()
            else "",
            "updated_at": _sanitize_timestamp(raw.get("updated_at")),
        }
    return stages


def read_terminal_delivery_receipt(value: Any) -> dict[str, Any]:
    """Return the bounded canonical receipt, failing closed on malformed input."""
    if not isinstance(value, dict):
        raise ABCError(
            "terminal_delivery_receipt_invalid",
            "Terminal delivery receipt must be an object",
        )
    version = value.get("version")
    if version != TERMINAL_DELIVERY_RECEIPT_VERSION:
        raise ABCError(
            "terminal_delivery_receipt_unsupported",
            f"Unsupported terminal delivery receipt version: {version}",
            {"version": version},
        )
    delivery_id = str(value.get("delivery_id") or "").strip()
    if not delivery_id:
        raise ABCError(
            "terminal_delivery_receipt_invalid",
            "Terminal delivery receipt requires a stable delivery_id",
        )
    terminal_state = _sanitize_terminal_state(value.get("terminal_state"))
    if not terminal_state:
        raise ABCError(
            "terminal_delivery_receipt_invalid",
            "Terminal delivery receipt requires a business terminal state",
        )
    stages = normalize_terminal_delivery_stages(value.get("stages"))
    now = _sanitize_timestamp(value.get("updated_at")) or _sanitize_timestamp(
        value.get("created_at")
    )
    return {
        "version": TERMINAL_DELIVERY_RECEIPT_VERSION,
        "delivery_id": delivery_id,
        "task_id": str(value.get("task_id") or "").strip(),
        "terminal_state": terminal_state,
        "terminal_event": _sanitize_event(value.get("terminal_event")),
        "committed_at": _sanitize_timestamp(value.get("committed_at")),
        "stages": stages,
        "created_at": _sanitize_timestamp(value.get("created_at")) or now,
        "updated_at": now or _utc_now(),
    }


def validate_terminal_delivery_receipt(value: Any) -> list[str]:
    """Return the ordered reasons a stored receipt cannot be trusted."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["terminal_delivery_receipt_invalid"]
    try:
        read_terminal_delivery_receipt(value)
    except ABCError as exc:
        errors.append(str(exc.code or "terminal_delivery_receipt_invalid"))
        return errors
    stages = normalize_terminal_delivery_stages(value.get("stages"))
    for stage in TERMINAL_DELIVERY_STAGES:
        entry = stages[stage]
        if entry["state"] in {"retry_wait", "pending"} and entry["attempts"] <= 0:
            errors.append(f"{stage}_stage_unattempted")
        if entry["state"] == "retry_wait" and entry["next_attempt_at"] and not _is_iso_utc(
            entry["next_attempt_at"]
        ):
            errors.append(f"{stage}_stage_missing_backoff")
    return errors


def read_or_rebuild_terminal_delivery_receipt(value: Any) -> dict[str, Any]:
    """Read a stored receipt, or rebuild an empty v1 receipt for legacy records.

    Legacy terminal records predate this receipt.  Rebuilding keeps the public
    projection total without ever replaying a historical UI dialog: every stage
    is reported ``not_applicable`` until real evidence says otherwise (see
    :func:`import_legacy_terminal_delivery`).
    """
    try:
        return read_terminal_delivery_receipt(value)
    except ABCError:
        return {
            "version": TERMINAL_DELIVERY_RECEIPT_VERSION,
            "delivery_id": "",
            "task_id": "",
            "terminal_state": "",
            "terminal_event": "",
            "committed_at": "",
            "stages": {
                stage: _new_stage("not_applicable") for stage in TERMINAL_DELIVERY_STAGES
            },
            "created_at": "",
            "updated_at": "",
        }


# ------------------------------------------------------------- stage machinery
def _stage(receipt: dict[str, Any], stage: str) -> dict[str, Any]:
    stages = receipt.setdefault("stages", {})
    entry = stages.get(stage)
    if not isinstance(entry, dict):
        entry = _new_stage()
        stages[stage] = entry
    return entry


def is_stage_resolved(receipt: dict[str, Any], stage: str) -> bool:
    """Confirmed stages must never repeat."""
    return str(_stage(receipt, stage).get("state")) in RESOLVED_TERMINAL_DELIVERY_STAGES


def pending_terminal_delivery_stages(receipt: dict[str, Any]) -> list[str]:
    """Return the stages still needing a delivery attempt, in stable order."""
    stages = normalize_terminal_delivery_stages(receipt.get("stages"))
    return [
        stage
        for stage in TERMINAL_DELIVERY_STAGES
        if stages[stage]["state"] not in RESOLVED_TERMINAL_DELIVERY_STAGES
    ]


def due_terminal_delivery_stages(
    receipt: dict[str, Any], *, now: str | None = None
) -> list[str]:
    """Return unresolved stages whose backoff has elapsed at ``now``."""
    moment = _sanitize_timestamp(now) or _utc_now()
    stages = normalize_terminal_delivery_stages(receipt.get("stages"))
    due: list[str] = []
    for stage in pending_terminal_delivery_stages(receipt):
        entry = stages[stage]
        next_at = entry["next_attempt_at"]
        if entry["state"] == "retry_wait" and next_at:
            if _parse_utc(moment) < _parse_utc(next_at):
                continue
        due.append(stage)
    return due


def next_terminal_delivery_backoff_s(attempts: int) -> int:
    """Return the delay before the next attempt.

    ``attempts`` counts the attempts already consumed.  The first retry is
    immediate, the second is earliest 60s later, and every later retry is capped
    at 300s.
    """
    if attempts <= 1:
        return 0
    if attempts == 2:
        return min(TERMINAL_DELIVERY_BACKOFF_S[0], TERMINAL_DELIVERY_MAX_BACKOFF_S)
    return TERMINAL_DELIVERY_MAX_BACKOFF_S


def transition_terminal_delivery_stage(
    receipt: dict[str, Any],
    stage: str,
    target_state: str,
    *,
    occurred_at: str | None = None,
    error_code: str = "",
    delivery_uncertain: bool = False,
    not_applicable: bool = False,
) -> dict[str, Any]:
    """Apply one pure, fail-closed stage transition.

    The returned receipt is detached from the input.  No filesystem, Executor,
    project or dispatcher conversation is touched by this state machine.
    """
    stage_name = _sanitize_stage(stage)
    if not stage_name:
        raise ABCError(
            "terminal_delivery_invalid_stage", f"Unknown delivery stage: {stage}"
        )
    if type(target_state) is not str or target_state not in TERMINAL_DELIVERY_STATES:
        raise ABCError(
            "terminal_delivery_invalid_state",
            f"Invalid terminal delivery stage state: {target_state}",
        )
    updated = copy.deepcopy(read_terminal_delivery_receipt(receipt))
    now = _sanitize_timestamp(occurred_at) or _utc_now()
    entry = _stage(updated, stage_name)
    current_state = str(entry.get("state"))
    if current_state in RESOLVED_TERMINAL_DELIVERY_STAGES:
        # Confirmed stages are immutable: replaying must never rewrite them.
        return updated
    attempts = int(entry.get("attempts") or 0)
    if target_state == "in_progress":
        if attempts >= TERMINAL_DELIVERY_MAX_ATTEMPTS:
            raise ABCError(
                "terminal_delivery_attempt_limit",
                f"Terminal delivery stage {stage_name} reached the attempt limit",
                {"stage": stage_name, "attempts": attempts},
            )
        entry["attempts"] = attempts + 1
        entry["state"] = "in_progress"
        entry["next_attempt_at"] = ""
        entry["last_error_code"] = ""
    elif target_state == "succeeded":
        entry["state"] = "succeeded"
        entry["next_attempt_at"] = ""
        entry["last_error_code"] = ""
    elif target_state == "not_applicable":
        entry["state"] = "not_applicable"
        entry["next_attempt_at"] = ""
        entry["last_error_code"] = ""
        if not_applicable or delivery_uncertain:
            entry["last_error_code"] = "delivery_not_applicable"
    elif target_state == "retry_wait":
        code = _sanitize_error_code(
            error_code,
            "delivery_stage_uncertain" if delivery_uncertain else "delivery_stage_interrupted",
        )
        entry["state"] = "retry_wait"
        entry["last_error_code"] = code
        if delivery_uncertain:
            entry["attempts"] = max(attempts, 1)
            entry["next_attempt_at"] = _add_delay(now, TERMINAL_DELIVERY_MAX_BACKOFF_S)
        else:
            # A zero delay means "due immediately"; an empty next_attempt_at is
            # the canonical representation of an immediate retry.
            delay_s = next_terminal_delivery_backoff_s(attempts)
            entry["next_attempt_at"] = _add_delay(now, delay_s) if delay_s > 0 else ""
    elif target_state == "pending":
        entry["state"] = "pending"
        entry["next_attempt_at"] = ""
    entry["updated_at"] = now
    updated["updated_at"] = now
    return updated


def mark_stage_not_applicable(
    receipt: dict[str, Any], stage: str, *, occurred_at: str | None = None
) -> dict[str, Any]:
    """Record a historically non-applicable stage without a delivery attempt."""
    return transition_terminal_delivery_stage(
        receipt,
        stage,
        "not_applicable",
        occurred_at=occurred_at,
        not_applicable=True,
    )


# ---------------------------------------------------------------- projections
def terminal_notification_request(
    terminal_state: str, terminal_event: str = ""
) -> tuple[str, str]:
    """Return the bounded ``(event_type, level)`` a terminal notification uses.

    The frozen ``terminal_event`` recorded on the receipt wins when it is a known
    terminal event; otherwise the event is derived from the business terminal
    state.  The level is always derived from the state so a replayed stage is
    indistinguishable from the immediate delivery it replaces.
    """
    state = _sanitize_terminal_state(terminal_state)
    event = _sanitize_event(terminal_event) or TERMINAL_DELIVERY_NOTIFICATION_EVENTS.get(
        state, "task.finalized"
    )
    return event, TERMINAL_DELIVERY_NOTIFICATION_LEVELS.get(state, "info")


def terminal_delivery_view(value: Any) -> dict[str, Any]:
    """Return the public, path-free receipt projection used by status/report.

    Scheduling internals (``next_attempt_at``) never appear in a public
    projection: they are durable receipt fields, exactly like the
    ``agentbc.session.cleanup`` receipt whose ``next_attempt_at`` is also
    status/report-internal.
    """
    receipt = read_or_rebuild_terminal_delivery_receipt(value)
    stages = {
        stage: {
            "state": entry["state"],
            "attempts": entry["attempts"],
            "last_error_code": entry["last_error_code"],
        }
        for stage, entry in receipt["stages"].items()
    }
    return {
        "version": receipt["version"],
        "delivery_id": receipt["delivery_id"],
        "terminal_state": receipt["terminal_state"],
        "terminal_event": receipt["terminal_event"],
        "committed_at": receipt["committed_at"],
        "stages": stages,
        "updated_at": receipt["updated_at"],
    }


def delivery_health_view(value: Any) -> dict[str, Any]:
    """Return the aggregate delivery health projection for status/report."""
    receipt = read_or_rebuild_terminal_delivery_receipt(value)
    stages = receipt["stages"]
    resolved = [
        stage
        for stage in TERMINAL_DELIVERY_STAGES
        if stages[stage]["state"] in RESOLVED_TERMINAL_DELIVERY_STAGES
    ]
    waiting = [stage for stage in TERMINAL_DELIVERY_STAGES if stages[stage]["state"] == "retry_wait"]
    active = [stage for stage in TERMINAL_DELIVERY_STAGES if stages[stage]["state"] == "in_progress"]
    outstanding = pending_terminal_delivery_stages(receipt)
    if not outstanding:
        state = "not_applicable" if all(
            stages[stage]["state"] == "not_applicable"
            for stage in TERMINAL_DELIVERY_STAGES
        ) else "succeeded"
    elif active:
        state = "in_progress"
    elif waiting:
        state = "retry_wait"
    else:
        state = "pending"
    attempted = sum(int(stages[stage]["attempts"]) for stage in TERMINAL_DELIVERY_STAGES)
    errors = sorted(
        {
            stages[stage]["last_error_code"]
            for stage in TERMINAL_DELIVERY_STAGES
            if stages[stage]["last_error_code"]
        }
    )
    return {
        "state": state,
        "delivery_id": receipt["delivery_id"],
        "stages_total": len(TERMINAL_DELIVERY_STAGES),
        "stages_succeeded": len(resolved),
        "stages_outstanding": len(outstanding),
        "stages_retry_wait": len(waiting),
        "stages_in_progress": len(active),
        "outstanding_stages": outstanding,
        "attempts_total": attempted,
        "error_codes": errors,
        "healthy": not outstanding,
        "updated_at": receipt["updated_at"],
    }


# ------------------------------------------------------- legacy record import
def import_legacy_terminal_delivery(
    receipt: dict[str, Any] | None,
    *,
    task: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Satisfy legacy stages from existing durable evidence; never replay dialogs.

    A legacy terminal record has no ``agentbc.terminal_delivery`` receipt.  The
    historical evidence that *already* exists on disk is enough to mark the
    matching stages satisfied:

    - an existing report file satisfies ``report``
    - a terminal ``notification_delivery`` event satisfies ``file_notification``
      *and* ``ui_notification``

    Otherwise historical UI delivery is ``not_applicable``: a historical dialog
    is never re-shown.  Nothing here writes to disk.
    """
    from pathlib import Path

    base = read_or_rebuild_terminal_delivery_receipt(receipt)
    if base["delivery_id"]:
        # A real receipt already exists: never rewrite confirmed stages.
        return base
    task = task if isinstance(task, dict) else {}
    extensions = task.get("extensions") if isinstance(task.get("extensions"), dict) else {}
    workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    terminal_state = _sanitize_terminal_state(task.get("status"))
    committed_at = _sanitize_timestamp(task.get("updated_at"))
    rebuilt = build_terminal_delivery_receipt(
        str(task.get("id") or base["task_id"] or ""),
        terminal_state=terminal_state or "failed",
        terminal_event=str((extensions or {}).get("agentbc.final_callback", {}).get("final_state") or "task.finalized"),
        committed_at=committed_at or None,
        delivery_id=None,
    )
    if terminal_state:
        rebuilt["terminal_state"] = terminal_state

    report_file = str(workspace.get("report_file") or "").strip()
    report_exists = bool(report_file) and Path(report_file).expanduser().is_file()
    terminal_notification = False
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") != "notification_delivery":
            continue
        if event.get("terminal") is False:
            continue
        if event.get("notification_event") == "task.input_required":
            continue
        terminal_notification = True
        break

    stages = rebuilt["stages"]
    for stage in TERMINAL_DELIVERY_STAGES:
        if stage == "report":
            if report_exists:
                stages[stage] = _confirmed(stage, "succeeded")
            else:
                stages[stage] = _confirmed(stage, "not_applicable")
        elif stage in TERMINAL_DELIVERY_NOTIFICATION_STAGES:
            stages[stage] = _confirmed(stage, "succeeded" if terminal_notification else "not_applicable")
        else:
            # Record compaction and index refresh are idempotent, Runner-owned
            # recomputations: leave them pending so maintenance can close them.
            stages[stage] = _new_stage("pending")
    return rebuilt


def _confirmed(stage: str, state: str) -> dict[str, Any]:
    entry = _new_stage(state)
    entry["updated_at"] = ""
    if state == "not_applicable":
        entry["last_error_code"] = ""
    return entry


# ------------------------------------------------------------ stage execution
def reconcile_interrupted_stages(
    receipt: dict[str, Any], *, now: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Downgrade a persisted ``in_progress`` reservation left by a dead process.

    An ``in_progress`` stage found on disk during Runner maintenance means the
    process died between the stage reservation and its recorded result.  For the
    noninteractive notification stages the real delivery may already have
    happened, so the stage is scheduled as ``retry_wait`` with
    ``delivery_uncertain`` evidence at the capped 300-second backoff.  The
    report/record/index stages are pure recomputations, so they return to
    ``pending`` and are retried immediately.

    Confirmed stages are never touched.
    """
    working = copy.deepcopy(read_terminal_delivery_receipt(receipt))
    occurred_at = _sanitize_timestamp(now) or _utc_now()
    reconciled: list[str] = []
    for stage in TERMINAL_DELIVERY_STAGES:
        entry = working["stages"][stage]
        if entry["state"] != "in_progress":
            continue
        if stage in TERMINAL_DELIVERY_NOTIFICATION_STAGES:
            working = transition_terminal_delivery_stage(
                working,
                stage,
                "retry_wait",
                occurred_at=occurred_at,
                error_code="delivery_stage_uncertain",
                delivery_uncertain=True,
            )
        else:
            working = transition_terminal_delivery_stage(
                working, stage, "pending", occurred_at=occurred_at
            )
        reconciled.append(stage)
    return working, reconciled


def run_delivery_stages(
    receipt: dict[str, Any],
    executors: StageExecutors,
    *,
    now: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Attempt every due stage once and return the updated receipt + results.

    Only stages that are not confirmed and whose backoff has elapsed are
    attempted.  Confirmed stages are never repeated.  Each stage is executed
    inside its own ``try``/``except`` boundary so one failure can never suppress
    another stage, the notification, or the terminal state.
    """
    working = copy.deepcopy(read_terminal_delivery_receipt(receipt))
    occurred_at = _sanitize_timestamp(now) or _utc_now()
    results: list[dict[str, Any]] = []
    for stage in TERMINAL_DELIVERY_STAGES:
        entry = working["stages"][stage]
        if entry["state"] in RESOLVED_TERMINAL_DELIVERY_STAGES:
            continue
        if entry["state"] == "retry_wait" and entry["next_attempt_at"]:
            if _parse_utc(occurred_at) < _parse_utc(entry["next_attempt_at"]):
                continue
        executor = executors.get(stage)
        if executor is None:
            continue
        if entry["state"] != "in_progress":
            try:
                working = transition_terminal_delivery_stage(
                    working, stage, "in_progress", occurred_at=occurred_at
                )
            except ABCError as exc:
                results.append(
                    {"stage": stage, "status": "skipped", "error_code": str(exc.code)}
                )
                continue
        try:
            outcome = executor()
        except Exception as exc:  # noqa: BLE001 - stage isolation is the contract
            outcome = StageOutcome(False, error_code=_stage_error_code(stage, exc))
        if isinstance(outcome, bool):
            outcome = StageOutcome(bool(outcome), error_code="" if outcome else _stage_error_code(stage))
        if getattr(outcome, "deferred", False):
            # This process does not own the stage; leave it pending for the
            # owner (Runner) without consuming an attempt or recording an error.
            results.append({"stage": stage, "status": "deferred", "error_code": ""})
            continue
        if outcome.ok:
            working = transition_terminal_delivery_stage(
                working, stage, "succeeded", occurred_at=occurred_at
            )
            results.append({"stage": stage, "status": "succeeded", "error_code": ""})
        elif outcome.not_applicable:
            working = transition_terminal_delivery_stage(
                working,
                stage,
                "not_applicable",
                occurred_at=occurred_at,
                not_applicable=True,
            )
            results.append({"stage": stage, "status": "not_applicable", "error_code": ""})
        elif outcome.delivery_uncertain and stage in TERMINAL_DELIVERY_NOTIFICATION_STAGES:
            working = transition_terminal_delivery_stage(
                working,
                stage,
                "retry_wait",
                occurred_at=occurred_at,
                error_code=outcome.error_code or "delivery_stage_uncertain",
                delivery_uncertain=True,
            )
            results.append(
                {
                    "stage": stage,
                    "status": "retry_wait",
                    "error_code": outcome.error_code or "delivery_stage_uncertain",
                    "delivery_uncertain": True,
                }
            )
        else:
            working = transition_terminal_delivery_stage(
                working,
                stage,
                "retry_wait",
                occurred_at=occurred_at,
                error_code=outcome.error_code or _stage_error_code(stage),
            )
            results.append(
                {
                    "stage": stage,
                    "status": "retry_wait",
                    "error_code": outcome.error_code or _stage_error_code(stage),
                }
            )
    return working, results


def _stage_error_code(stage: str, exc: BaseException | None = None) -> str:
    """Map a stage failure to a stable bounded code."""
    if isinstance(exc, ABCError):
        code = str(getattr(exc, "code", "") or "")
        mapped = {
            "record_budget_exceeded": "record_budget_exceeded",
        }
        if code in mapped:
            return mapped[code]
    mapping = {
        "report": "report_write_failed",
        "record": "record_compaction_failed",
        "index": "index_refresh_failed",
        "file_notification": "file_notification_failed",
        "ui_notification": "ui_notification_failed",
    }
    return mapping.get(stage, "delivery_stage_interrupted")


# --------------------------------------------------------------- eligibility
def terminal_delivery_eligible(task: dict[str, Any]) -> bool:
    """Return whether a task may be handled by the terminal delivery coordinator.

    ``input_required`` is never processed here. ``needs_recovery`` is eligible
    because its task-end dialog is now the same durable lifecycle signal as any
    other task-end dialog.
    """
    status = str((task or {}).get("status") or "").strip().lower()
    return status in TERMINAL_DELIVERY_STATUSES


def delivery_event_payload(
    receipt: dict[str, Any], results: list[dict[str, Any]], *, occurred_at: str
) -> dict[str, Any]:
    """Build the bounded ``terminal.delivery`` event line (no bodies/paths)."""
    health = delivery_health_view(receipt)
    return {
        "event_type": DELIVERY_EVENT_TYPE,
        "task_id": receipt.get("task_id") or "",
        "delivery_id": receipt.get("delivery_id") or "",
        "terminal_state": receipt.get("terminal_state") or "",
        "health": health["state"],
        "stages_succeeded": health["stages_succeeded"],
        "stages_outstanding": health["stages_outstanding"],
        "outstanding_stages": list(health["outstanding_stages"]),
        "attempts_total": health["attempts_total"],
        "error_codes": list(health["error_codes"]),
        "attempted": [
            {
                "stage": str(item.get("stage") or ""),
                "status": str(item.get("status") or ""),
                "error_code": str(item.get("error_code") or ""),
                "delivery_uncertain": bool(item.get("delivery_uncertain")),
            }
            for item in results
        ],
        "created_at": occurred_at,
    }


__all__ = [
    "DELIVERY_ERROR_CODE_RE",
    "DELIVERY_EVENT_TYPE",
    "DELIVERY_EVENTS_FILE",
    "DELIVERY_LOCK_NAME",
    "DELIVERY_HEALTH_STATES",
    "RESOLVED_TERMINAL_DELIVERY_STAGES",
    "StageExecutor",
    "StageExecutors",
    "StageOutcome",
    "TERMINAL_DELIVERY_BACKOFF_S",
    "TERMINAL_DELIVERY_ERROR_CODES",
    "TERMINAL_DELIVERY_EVENTS",
    "TERMINAL_DELIVERY_EXTENSION_KEY",
    "TERMINAL_DELIVERY_MAX_ATTEMPTS",
    "TERMINAL_DELIVERY_MAX_BACKOFF_S",
    "TERMINAL_DELIVERY_NOTIFICATION_EVENTS",
    "TERMINAL_DELIVERY_NOTIFICATION_LEVELS",
    "TERMINAL_DELIVERY_NOTIFICATION_STAGES",
    "TERMINAL_DELIVERY_RECEIPT_VERSION",
    "TERMINAL_DELIVERY_STAGES",
    "TERMINAL_DELIVERY_STATES",
    "TERMINAL_DELIVERY_STATUSES",
    "build_terminal_delivery_receipt",
    "delivery_event_payload",
    "delivery_health_view",
    "due_terminal_delivery_stages",
    "import_legacy_terminal_delivery",
    "is_stage_resolved",
    "mark_stage_not_applicable",
    "next_terminal_delivery_backoff_s",
    "normalize_terminal_delivery_stages",
    "pending_terminal_delivery_stages",
    "read_or_rebuild_terminal_delivery_receipt",
    "read_terminal_delivery_receipt",
    "reconcile_interrupted_stages",
    "run_delivery_stages",
    "terminal_delivery_eligible",
    "terminal_delivery_view",
    "terminal_notification_request",
    "transition_terminal_delivery_stage",
    "validate_terminal_delivery_receipt",
]
