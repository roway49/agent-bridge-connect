"""Task dataclass with serialization and status transition support."""

from dataclasses import dataclass, field
from typing import Any

STATUS_ORDER = ["pending", "assigned", "in_progress", "review", "done"]

_KNOWN_TASK_KEYS = frozenset({"id", "title", "status", "assignee", "steps"})


@dataclass
class Task:
    """A task in the agent-bridge-connect lifecycle."""

    id: str
    title: str
    status: str
    assignee: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    _extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        """Create a Task from a dictionary (e.g. parsed JSON)."""
        task = cls(
            id=data["id"],
            title=data["title"],
            status=data["status"],
            assignee=data["assignee"],
            steps=data.get("steps", []),
        )
        # Preserve every field not consumed by the dataclass for lossless round-trip.
        task._extra = {k: v for k, v in data.items() if k not in _KNOWN_TASK_KEYS}
        return task

    def to_dict(self) -> dict[str, Any]:
        """Serialize the Task back to a dictionary."""
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "assignee": self.assignee,
            "steps": self.steps,
        }
        result.update(self._extra)
        return result

    def can_transition_to(self, target_status: str) -> bool:
        """Return True if transitioning to *target_status* is allowed.

        The linear lifecycle is *pending → assigned → in_progress → review → done*.
        - Forward transitions along the chain are always permitted (you may skip a stage).
        - *blocked* and *failed* are reachable from any status.
        - *done* cannot be reached from *pending* or *assigned* — the task must have
          passed through *in_progress* at minimum.
        """
        if target_status == self.status:
            return True

        # blocked / failed are always reachable
        if target_status in ("blocked", "failed"):
            return True

        if self.status not in STATUS_ORDER or target_status not in STATUS_ORDER:
            return False

        current_idx = STATUS_ORDER.index(self.status)
        target_idx = STATUS_ORDER.index(target_status)

        # Only forward transitions are allowed
        if target_idx <= current_idx:
            return False

        # done requires at least in_progress as a prerequisite
        if target_status == "done" and current_idx < STATUS_ORDER.index("in_progress"):
            return False

        return True
