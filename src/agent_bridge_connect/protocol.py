from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any


ABC_PROTOCOL_VERSION = "1.0"
COMPAT_VERSIONS = {
    "core_protocol": "1.0",
    "adapter_api": "1.0",
    "config_schema": "1.0",
    "integration_manifest": "1.0",
}

STATES = [
    "pending",
    "running",
    "needs_recovery",
    "assigned",
    "working",
    "input_required",
    "pause_pending",
    "paused",
    "review_required",
    "completed",
    "failed",
    "cancelled",
    "rejected",
]

TRANSITIONS = {
    "pending": ["running", "cancelled", "needs_recovery", "assigned"],
    "running": ["running", "completed", "input_required", "cancelled", "needs_recovery", "pause_pending"],
    "needs_recovery": ["running", "cancelled"],
    "assigned": ["running", "cancelled", "needs_recovery", "working"],
    "working": ["working", "running", "completed", "input_required", "pause_pending", "cancelled", "needs_recovery"],
    "input_required": ["running", "completed", "cancelled", "needs_recovery", "working"],
    "pause_pending": ["paused", "cancelled", "needs_recovery"],
    "paused": ["running", "cancelled", "needs_recovery", "assigned"],
    "review_required": ["completed", "cancelled", "needs_recovery"],
    "completed": [],
    "failed": ["running"],
    "cancelled": [],
    "rejected": [],
}

class ABCError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class ExtensibleModel:
    extensions: dict[str, Any] = field(default_factory=dict)
    _extra: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        known = {item.name for item in fields(cls) if item.init}
        model = cls(**{key: value for key, value in data.items() if key in known})
        model._extra = {key: value for key, value in data.items() if key not in known}
        return model

    def to_dict(self) -> dict[str, Any]:
        data = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.init
        }
        data.update(self._extra)
        return data


@dataclass
class StepModel(ExtensibleModel):
    id: int = 0
    description: str = ""
    status: str = "pending"
    record: str = ""
    result: dict[str, Any] | None = None


@dataclass
class EventModel(ExtensibleModel):
    event_type: str = ""
    task_id: str = ""
    created_at: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterventionModel(ExtensibleModel):
    intervention_type: str = ""
    task_id: str = ""
    created_at: str = ""
    message: str | None = None
    step_id: int | None = None


@dataclass
class ReportModel(ExtensibleModel):
    task_id: str = ""
    status: str = ""
    generated_at: str = ""
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorModel(ExtensibleModel):
    code: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResultModel(ExtensibleModel):
    ok: bool = False
    value: Any = None
    error: ErrorModel | dict[str, Any] | None = None


@dataclass
class PreflightResult:
    ok: bool
    errors: list[str]


@dataclass
class TaskModel(ExtensibleModel):
    id: str = ""
    title: str = ""
    status: str = "pending"
    assignee: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    created_by: str = "user"
    created_at: str = ""
    updated_at: str = ""
    intervention: dict[str, Any] = field(
        default_factory=lambda: {
            "paused": False,
            "pause_reason": None,
            "latest_correction_id": None,
        }
    )
    errors: list[dict[str, Any]] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    session_id: str | None = None


def task_step_text(step: Any) -> str:
    """Return the canonical human-readable text for a task step."""
    if not isinstance(step, dict):
        return str(step).strip()
    for key in ("description", "action", "command"):
        value = step.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def resumed_input_prompt_lines(task_packet: dict[str, Any]) -> list[str]:
    """Return durable request/response context for a resumed executor turn."""
    extensions = task_packet.get("extensions") if isinstance(task_packet.get("extensions"), dict) else {}
    request = extensions.get("agentbc.input") if isinstance(extensions.get("agentbc.input"), dict) else {}
    response = request.get("response") if isinstance(request.get("response"), dict) else {}
    if request.get("status") != "answered" or not response:
        return []
    return [
        "Resume context:",
        f"- Prior input request ({request.get('type', 'message')}, step {request.get('blocked_step_id', '')}): {request.get('summary', '')}",
        f"- User response ({response.get('type', 'message')}): {response.get('summary', '')}",
        "- Keep completed-step evidence intact and continue only pending steps.",
    ]
