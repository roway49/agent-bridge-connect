"""Schema validation for task.json and steps/N.json records."""

from typing import Any

VALID_TASK_STATUSES = frozenset({
    "pending", "assigned", "in_progress", "review", "done", "failed", "blocked",
})

TASK_REQUIRED_FIELDS = ["id", "title", "status", "assignee", "steps"]

STEP_REQUIRED_FIELDS = ["task_id", "step_id", "worker", "status"]


def validate_task(data: dict[str, Any]) -> list[str]:
    """Validate a task dict. Returns a (possibly empty) list of error strings."""
    errors: list[str] = []

    for field in TASK_REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if "status" in data and data["status"] not in VALID_TASK_STATUSES:
        errors.append(f"Invalid status: {data['status']}")

    if "steps" in data:
        steps = data["steps"]
        if not isinstance(steps, list) or len(steps) == 0:
            errors.append("steps must be a non-empty list")

    return errors


def validate_step(data: dict[str, Any]) -> list[str]:
    """Validate a step record dict. Returns a (possibly empty) list of error strings."""
    errors: list[str] = []

    for field in STEP_REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if data.get("status") == "done":
        if "finished_at" not in data:
            errors.append("Missing required field for done step: finished_at")
        if "duration_s" not in data:
            errors.append("Missing required field for done step: duration_s")

    return errors
