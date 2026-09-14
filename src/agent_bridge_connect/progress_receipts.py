from __future__ import annotations

import copy
from typing import Any

from .protocol import ABCError


PROGRESS_EXTENSION_KEY = "agentbc.progress"
PROGRESS_RECEIPT_VERSION = 1
PROGRESS_STATUS_DONE = "done"


def validate_progress_record(
    value: Any,
    *,
    task_id: str | None = None,
    declared_step_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Validate and return one bounded authoritative progress record."""
    if not isinstance(value, dict):
        raise ABCError("progress_receipt_invalid", "Progress receipt must be an object")
    if value.get("version") != PROGRESS_RECEIPT_VERSION:
        raise ABCError(
            "progress_receipt_invalid",
            f"Progress receipt version must be {PROGRESS_RECEIPT_VERSION}",
        )
    record_task_id = str(value.get("task_id") or "").strip()
    if not record_task_id or (task_id is not None and record_task_id != task_id):
        raise ABCError("progress_receipt_task_mismatch", "Progress receipt task binding is invalid")
    attempt_index = value.get("attempt_index")
    latest_sequence = value.get("latest_sequence")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 0:
        raise ABCError("progress_receipt_invalid", "Progress attempt index must be non-negative")
    if isinstance(latest_sequence, bool) or not isinstance(latest_sequence, int) or latest_sequence < 0:
        raise ABCError("progress_receipt_invalid", "Progress sequence must be non-negative")
    receipts = value.get("receipts")
    if not isinstance(receipts, list):
        raise ABCError("progress_receipt_invalid", "Progress receipts must be a list")
    seen_steps: set[int] = set()
    seen_sequences: set[int] = set()
    highest_sequence = 0
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ABCError("progress_receipt_invalid", "Each progress receipt must be an object")
        step_id = receipt.get("step_id")
        sequence = receipt.get("sequence")
        binding = receipt.get("binding")
        if isinstance(step_id, bool) or not isinstance(step_id, int) or step_id <= 0:
            raise ABCError("progress_receipt_step_invalid", "Progress step ID must be a positive integer")
        if declared_step_ids is not None and step_id not in declared_step_ids:
            raise ABCError("progress_receipt_step_unknown", f"Unknown declared step ID: {step_id}")
        if step_id in seen_steps:
            raise ABCError("progress_receipt_duplicate_step", f"Duplicate progress receipt for step {step_id}")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ABCError("progress_receipt_sequence_invalid", "Progress sequence must be positive")
        if sequence in seen_sequences:
            raise ABCError("progress_receipt_sequence_invalid", "Progress sequences must be unique")
        if receipt.get("status") != PROGRESS_STATUS_DONE:
            raise ABCError("progress_receipt_status_invalid", "Only done progress is authoritative")
        if receipt.get("evidence_source") != "agent_cli":
            raise ABCError("progress_receipt_source_invalid", "Progress evidence source must be agent_cli")
        if not isinstance(receipt.get("recorded_at"), str) or not str(receipt.get("recorded_at") or "").strip():
            raise ABCError("progress_receipt_invalid", "Progress recorded_at is required")
        if not isinstance(binding, dict):
            raise ABCError("progress_receipt_binding_invalid", "Progress binding must be an object")
        for field in ("executor", "executor_run_id", "session_id"):
            if not isinstance(binding.get(field), str) or not str(binding.get(field) or "").strip():
                raise ABCError(
                    "progress_receipt_binding_invalid",
                    f"Progress binding {field} is required",
                )
        seen_steps.add(step_id)
        seen_sequences.add(sequence)
        highest_sequence = max(highest_sequence, sequence)
    if highest_sequence != latest_sequence:
        raise ABCError("progress_receipt_sequence_invalid", "Latest progress sequence is inconsistent")
    return copy.deepcopy(value)


def progress_public_projection(value: Any) -> dict[str, Any] | None:
    """Return one stable redacted projection without run or session identifiers."""
    if value is None:
        return None
    try:
        record = validate_progress_record(value)
    except ABCError:
        return None
    receipts = sorted(record["receipts"], key=lambda item: int(item["sequence"]))
    return {
        "version": PROGRESS_RECEIPT_VERSION,
        "attempt_index": record["attempt_index"],
        "latest_sequence": record["latest_sequence"],
        "confirmed_step_ids": [int(item["step_id"]) for item in receipts],
        "confirmed_count": len(receipts),
        "evidence_quality": "authoritative",
        "updated_at": str(record.get("updated_at") or ""),
    }
