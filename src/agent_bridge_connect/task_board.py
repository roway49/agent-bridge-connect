from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import task_step_text

from .schema import validate_task
from .task import Task
from .task_id import allocate_task_code, format_task_id, split_task_ref


class TaskCreateError(Exception):
    """Raised when a task cannot be created from user input."""


class TaskNotFoundError(Exception):
    """Raised when a task cannot be found in the board."""


class TaskBoard:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.tasks_dir = self.root

    def list_all(self, status: str | None = None, assignee: str | None = None) -> list[Task]:
        tasks: list[Task] = []
        if not self.tasks_dir.exists():
            return tasks

        for task_file in self.tasks_dir.glob("*/*/task.json"):
            try:
                data = json.loads(task_file.read_text(encoding="utf-8"))
                task = Task.from_dict(data)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                continue
            if status is not None and status != "all" and task.status != status:
                continue
            if assignee is not None and task.assignee != assignee:
                continue
            tasks.append(task)

        return sorted(tasks, key=lambda task: task.id, reverse=True)


def get_task_status(task_id: str, board_root: str | Path) -> dict[str, Any]:
    """Return task status and step state from the on-disk task.json file."""
    board = TaskBoard(board_root)
    task_path = _task_path(board.tasks_dir, task_id)
    if not task_path.exists():
        raise TaskNotFoundError(f"task not found: {task_id}")

    data = json.loads(task_path.read_text(encoding="utf-8"))
    steps = [dict(step, status=step.get("status", "pending")) for step in data.get("steps", [])]
    return {
        "id": data.get("id", task_id),
        "title": data.get("title", ""),
        "status": data.get("status", ""),
        "assignee": data.get("assignee", ""),
        "steps": steps,
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "intervention": data.get("intervention", {"paused": False}),
        "errors": data.get("errors", []),
        "session_id": data.get("session_id"),
    }


def poll_task(task_id: str, board_root: str | Path) -> dict[str, Any]:
    """Re-read task.json and return the latest task status."""
    return get_task_status(task_id, board_root)


def init_board(root: str | Path) -> None:
    from .record_management import ensure_record_root

    board = TaskBoard(root)
    ensure_record_root(board.root)
    agents_yaml = board.root / "agents.yaml"
    if not agents_yaml.exists():
        agents_yaml.write_text(
            "\n".join(
                [
                    "agents:",
                    "  codex:",
                    "    type: codex",
                    "    command: codex exec --json {task_path}",
                    "    capabilities: [coding, testing, debugging]",
                    "    maturity: experimental",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def create_task(
    title: str,
    assignee: str,
    steps_path: str | Path,
    board_root: str | Path,
    session_id: str | None = None,
) -> Task:
    title = title.strip()
    assignee = assignee.strip()
    steps_file = Path(steps_path).expanduser()

    if not title:
        raise TaskCreateError("title is required")
    if not assignee:
        raise TaskCreateError("assignee is required")
    if not steps_file.exists():
        raise TaskCreateError(f"steps file not found: {steps_file}")

    init_board(board_root)
    board = TaskBoard(board_root)
    task_id = _next_task_id(board.tasks_dir)
    steps = _load_steps(steps_file)
    if not steps:
        raise TaskCreateError("steps file must define at least one step")

    task_steps = [
        {
            "id": index,
            "description": step.get("description", ""),
            "record": f"steps/{index:02d}.json",
        }
        for index, step in enumerate(steps, 1)
    ]
    code, iteration = split_task_ref(task_id)
    iteration = iteration or "001"
    task_date = datetime.now().strftime("%Y-%m-%d")
    agentbc_root = board.root.parent
    artifact_root = agentbc_root / "tasks" / "artifacts" / task_date / code
    project_root = artifact_root
    task_dir = board.tasks_dir / code / iteration
    report_root = agentbc_root / "tasks" / "report" / task_date / code
    task_file = report_root / f"{task_id}-task.md"
    report_file = report_root / f"{task_id}-report.md"
    task = Task(
        id=task_id,
        title=title,
        status="pending",
        assignee=assignee,
        steps=task_steps,
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    task._extra.update(
        {
            "created_at": now,
            "updated_at": now,
            "workspace": {
                "customer_dir": False,
                "customer_path": "",
                "project_root": str(project_root),
                "default_path": str(project_root),
                "agentbc_root": str(agentbc_root),
                "root": str(project_root),
                "artifact_root": str(artifact_root),
                "artifacts_dir": str(artifact_root),
                "report_root": str(report_root),
                "task_file": str(task_file),
                "report_file": str(report_file),
                "output_dir": str(report_root),
                "task_code": code,
                "iteration": iteration,
                "task_date": task_date,
                "chain_id": code,
                "chain_token": code,
                "chain_dir": code,
                "chain_task_id": iteration,
                "chain_output_dir": str(report_root),
                "internal_task_dir": str(task_dir),
            },
            "extensions": {
                "agentbc.lineage": {
                    "parent_task_id": None,
                    "base_task_id": task_id,
                    "chain_root_task_id": task_id,
                    "iteration_index": int(iteration),
                    "task_code": code,
                    "task_date": task_date,
                    "agentbc_root": str(agentbc_root),
                    "base_workspace_root": str(project_root),
                    "base_artifacts_dir": str(artifact_root),
                    "branch_mode": "linear",
                    "chain_id": code,
                    "chain_token": code,
                    "chain_dir": code,
                    "chain_task_id": iteration,
                    "chain_output_dir": str(report_root),
                },
                "agentbc.execution": {"internal_status": "pending"},
            },
        }
    )
    if session_id is not None:
        task._extra["session_id"] = session_id

    data = task.to_dict()
    errors = validate_task(data)
    if errors:
        raise TaskCreateError("; ".join(errors))

    steps_dir = task_dir / "steps"
    steps_dir.mkdir(parents=True, exist_ok=False)
    artifact_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)
    for index in range(1, len(task_steps) + 1):
        (steps_dir / f"{index:02d}.json").write_text("{}\n", encoding="utf-8")
    (task_dir / "task.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return task


def _next_task_id(tasks_dir: Path) -> str:
    codes = [path.name for path in tasks_dir.iterdir() if path.is_dir()] if tasks_dir.exists() else []
    return format_task_id(allocate_task_code(codes), 1)


def _task_path(tasks_dir: Path, task_id: str) -> Path:
    try:
        code, iteration = split_task_ref(task_id)
    except ValueError as exc:
        raise TaskNotFoundError(f"task not found: {task_id}") from exc
    if iteration is None:
        chain_dir = tasks_dir / code
        iterations = sorted(path.name for path in chain_dir.iterdir() if path.is_dir() and path.name.isdigit())
        if not iterations:
            raise TaskNotFoundError(f"task not found: {task_id}")
        iteration = iterations[-1]
    return tasks_dir / code / iteration / "task.json"


def _load_steps(path: Path) -> list[dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        return _load_steps_without_yaml(path)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        raise TaskCreateError("steps file must contain a steps list")
    return [_normalize_step(step) for step in steps]


def _load_steps_without_yaml(path: Path) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped == "steps:":
            index += 1
            continue
        if stripped.startswith("- "):
            if current is not None:
                steps.append(current)
            current = {}
            remainder = stripped[2:].strip()
            if remainder:
                if _is_block_scalar_key(remainder):
                    key = remainder.split(":", 1)[0].strip()
                    block, index = _collect_block_scalar(lines, index + 1, _indent(raw_line))
                    current[key] = block
                    continue
                _parse_step_key_value(current, remainder)
            index += 1
            continue
        if current is not None:
            if _is_block_scalar_key(stripped):
                key = stripped.split(":", 1)[0].strip()
                block, index = _collect_block_scalar(lines, index + 1, _indent(raw_line))
                current[key] = block
                continue
            _parse_step_key_value(current, stripped)
        index += 1

    if current is not None:
        steps.append(current)
    return [_normalize_step(step) for step in steps]


def _parse_step_key_value(step: dict[str, Any], text: str) -> None:
    if ":" not in text:
        return
    key, value = text.split(":", 1)
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    elif value.isdigit():
        step[key.strip()] = int(value)
        return
    step[key.strip()] = value


def _is_block_scalar_key(text: str) -> bool:
    if ":" not in text:
        return False
    _, value = text.split(":", 1)
    return value.strip() in {"|", "|-", "|+", ">", ">-", ">+"}


def _collect_block_scalar(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    collected: list[str] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        current_indent = _indent(raw)
        if stripped.startswith("- ") and current_indent <= parent_indent:
            break
        if stripped and current_indent <= parent_indent:
            break
        collected.append(stripped)
        index += 1
    return "\n".join(collected).strip(), index


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _normalize_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise TaskCreateError("each step must be a mapping")
    normalized = dict(step)
    description = task_step_text(normalized)
    if not description:
        raise TaskCreateError("each step must define a non-empty description or action")
    normalized["description"] = description
    return normalized
