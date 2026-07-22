"""AgentBC MCP Server - exposes task operations as MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_bridge_connect.config import DEFAULT_BOARD_ROOT, load_config
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.reports import generate_task_brief
from agent_bridge_connect.service import TaskService


TOOLS: list[dict[str, Any]] = [
    {
        "name": "abc_task_create",
        "description": "Create a new task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "assignee": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "object"}},
                "session_id": {"type": "string"},
                "customer_path": {
                    "type": "string",
                    "description": 'Use "default path" when no user project path was supplied, otherwise pass the exact user project path.',
                },
            },
            "required": ["title", "assignee", "steps", "customer_path"],
        },
    },
    {
        "name": "abc_task_get",
        "description": "Get task status and details",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "abc_task_list",
        "description": "List tasks with optional filters",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "assignee": {"type": "string"},
            },
        },
    },
    {
        "name": "abc_task_intervene",
        "description": "Pause, resume, cancel, or reassign a task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "cancel", "reassign"],
                },
                "reason": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["task_id", "action"],
        },
    },
    {
        "name": "abc_task_report",
        "description": "Generate task report",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "abc_task_brief",
        "description": "Generate Task Brief for context-free review",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


def get_tools() -> list[dict[str, Any]]:
    """Return MCP tool definitions."""
    return [dict(tool) for tool in TOOLS]


def handle_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    board_root: str | Path | None = None,
) -> dict[str, Any]:
    """Dispatch an MCP tool call through the task service API."""
    svc = TaskService(board_root or DEFAULT_BOARD_ROOT, config=load_config())

    try:
        if tool_name == "abc_task_create":
            task = svc.create_task(
                arguments["title"],
                arguments["assignee"],
                arguments.get("steps", []),
                session_id=arguments.get("session_id"),
                customer_path=arguments.get("customer_path"),
            )
            return {"ok": True, "task_id": task.id, "status": task.status}

        if tool_name == "abc_task_get":
            task = svc.get_task(arguments["task_id"])
            return {"ok": True, "task": task.to_dict()}

        if tool_name == "abc_task_list":
            tasks = svc.list_tasks(
                status=arguments.get("status"),
                assignee=arguments.get("assignee"),
            )
            return {"ok": True, "tasks": [task.to_dict() for task in tasks]}

        if tool_name == "abc_task_intervene":
            return _handle_intervention(svc, arguments)

        if tool_name == "abc_task_report":
            report = svc.generate_report(arguments["task_id"])
            return {"ok": True, "report": report}

        if tool_name == "abc_task_brief":
            brief = generate_task_brief(arguments["task_id"], svc.board_root)
            return {"ok": True, "brief": brief}

    except ABCError as exc:
        return {"ok": False, "error": exc.message, "code": exc.code, "details": exc.details}
    except KeyError as exc:
        missing = str(exc).strip("'")
        return {"ok": False, "error": f"Missing argument: {missing}", "code": "missing_argument"}

    return {"ok": False, "error": f"Unknown tool: {tool_name}", "code": "unknown_tool"}


def _handle_intervention(svc: TaskService, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = arguments["task_id"]
    action = arguments["action"]

    if action == "pause":
        svc.pause_task(task_id, reason=arguments.get("reason"))
    elif action == "resume":
        svc.resume_task(task_id)
    elif action == "cancel":
        svc.cancel_task(task_id)
    elif action == "reassign":
        svc.reassign_task(task_id, arguments["target"])
    else:
        return {"ok": False, "error": f"Unknown intervention action: {action}", "code": "unknown_action"}

    return {"ok": True, "action": action, "task_id": task_id}
