from __future__ import annotations

import copy
import math
import json
import os
import re
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable

from .protocol import ABCError

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - Phase 1 is POSIX-only
    fcntl = None  # type: ignore[assignment]

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier
    tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG_PATH = Path.home() / ".abc" / "config.toml"
DEFAULT_WORKSPACE_ROOT = Path("~") / "Documents" / "AgentBC" / "workspace"
DEFAULT_BOARD_ROOT = DEFAULT_WORKSPACE_ROOT / "record"
DEFAULT_CLAUDE_MAX_BUDGET_USD = 10.0
DEFAULT_HERMES_MAX_TURNS = 90
DEFAULT_RETAIN_EXECUTOR_SESSIONS = False

_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = resolve_config_path(path)
    if not config_path.exists():
        return {}
    if tomllib is not None:
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    return _load_toml_compat(config_path.read_text(encoding="utf-8"))


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for section in ("executors", "notifiers", "sessions"):
        if section in config and not isinstance(config[section], dict):
            errors.append(f"{section} must be a table")
    board_root = config.get("board_root")
    if board_root is not None and not isinstance(board_root, str):
        errors.append("board_root must be a string")
    workspace_root = config.get("workspace_root")
    if workspace_root is not None and not isinstance(workspace_root, str):
        errors.append("workspace_root must be a string")
    if "permission_mode" in config:
        from .permission_modes import normalize_permission_mode

        try:
            normalize_permission_mode(config.get("permission_mode"))
        except ABCError as exc:  # validation returns errors instead of raising
            errors.append(str(exc))
    executors = config.get("executors")
    if isinstance(executors, dict):
        claude = executors.get("claude")
        if "claude" in executors and not isinstance(claude, dict):
            errors.append("executors.claude must be a table")
        if isinstance(claude, dict) and "max_budget_usd" in claude:
            budget = claude["max_budget_usd"]
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not math.isfinite(float(budget))
                or float(budget) <= 0
            ):
                errors.append("executors.claude.max_budget_usd must be a positive finite number")
        hermes = executors.get("hermes")
        if "hermes" in executors and not isinstance(hermes, dict):
            errors.append("executors.hermes must be a table")
        if isinstance(hermes, dict) and "max_turns" in hermes:
            max_turns = hermes["max_turns"]
            if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
                errors.append("executors.hermes.max_turns must be a positive integer")
    sessions = config.get("sessions")
    if isinstance(sessions, dict) and "retain_executor_sessions" in sessions:
        if not isinstance(sessions["retain_executor_sessions"], bool):
            errors.append("sessions.retain_executor_sessions must be a boolean")
    return errors


def configured_claude_budget(config: dict[str, Any] | None) -> tuple[float, str]:
    executor = get_executor_config(config or {}, "claude")
    value = executor.get("max_budget_usd")
    if _valid_claude_budget(value):
        return float(value), "configured"
    return DEFAULT_CLAUDE_MAX_BUDGET_USD, "claude_default_10"


def configured_hermes_max_turns(config: dict[str, Any] | None) -> tuple[int, str]:
    executor = get_executor_config(config or {}, "hermes")
    value = executor.get("max_turns")
    if _valid_hermes_max_turns(value):
        return int(value), "configured"
    return DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"


def configured_session_retention(config: dict[str, Any] | None) -> tuple[bool, str]:
    sessions = config.get("sessions") if isinstance(config, dict) else None
    if isinstance(sessions, dict) and isinstance(sessions.get("retain_executor_sessions"), bool):
        return sessions["retain_executor_sessions"], "configured"
    return DEFAULT_RETAIN_EXECUTOR_SESSIONS, "session_default_false"


def write_config_atomic(
    config: dict[str, Any],
    path: str | Path | None = None,
) -> bool:
    """Atomically replace one AgentBC config while holding the cross-process lock."""
    desired = copy.deepcopy(config)

    def replace(current: dict[str, Any]) -> dict[str, Any]:
        del current
        return desired

    _, changed = update_config_atomic(replace, path)
    return changed


def update_config_atomic(
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Read, validate, mutate, and atomically write the latest config under a file lock."""
    config_path = resolve_config_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if fcntl is None:
        raise ABCError(
            "config_lock_unsupported",
            "AgentBC config updates require POSIX file locking",
        )
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existed = config_path.exists()
        current = load_config(config_path)
        _raise_config_errors(current)
        working = copy.deepcopy(current)
        replacement = mutator(working)
        updated = working if replacement is None else replacement
        if not isinstance(updated, dict):
            raise TypeError("config mutator must return a table or None")
        _raise_config_errors(updated)
        changed = not existed or updated != current
        if changed:
            _replace_config_file(config_path, toml_dumps(updated))
        return copy.deepcopy(updated), changed
    finally:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def toml_dumps(config: dict[str, Any]) -> str:
    """Serialize the supported AgentBC TOML model deterministically."""
    lines: list[str] = []

    def write_table(table: dict[str, Any], prefix: tuple[str, ...]) -> None:
        scalar_items = [
            (key, value) for key, value in table.items() if not isinstance(value, dict)
        ]
        child_tables = [
            (key, value) for key, value in table.items() if isinstance(value, dict)
        ]
        if prefix:
            lines.append(f"[{'.'.join(_toml_key(part) for part in prefix)}]")
        for key, value in scalar_items:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        if scalar_items and child_tables:
            lines.append("")
        for index, (key, value) in enumerate(child_tables):
            write_table(value, (*prefix, key))
            if index != len(child_tables) - 1:
                lines.append("")

    write_table(config, ())
    return "\n".join(lines).rstrip() + "\n"


def get_executor_config(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get("executors", {}).get(name, {})
    return dict(value) if isinstance(value, dict) else {}


def get_notifier_config(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get("notifiers", {}).get(name, {})
    return dict(value) if isinstance(value, dict) else {}


def resolve_secret(value_env: str) -> str | None:
    return os.environ.get(value_env)


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("AGENTBC_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def default_workspace_root() -> Path:
    return DEFAULT_WORKSPACE_ROOT.expanduser().resolve()


def resolve_workspace_root(config: dict[str, Any] | None = None) -> Path:
    configured = config.get("workspace_root") if isinstance(config, dict) else None
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    return default_workspace_root()


def resolve_runner_allowed_roots(
    config: dict[str, Any] | None = None,
    extra_roots: list[str | Path] | None = None,
) -> list[Path]:
    """Resolve stable Runner roots without depending on its launch directory."""
    roots = [resolve_workspace_root(config), *(extra_roots or [])]
    resolved: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        path = Path(root).expanduser().resolve()
        key = str(path)
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    return resolved


def init_board(root: str | Path = DEFAULT_BOARD_ROOT) -> Path:
    from .record_management import ensure_record_root

    board_root = ensure_record_root(root)
    agents = board_root / "agents.yaml"
    if not agents.exists():
        agents.write_text(
            "agents:\n"
            "  default:\n"
            "    type: local\n"
            "    capabilities: [coding, testing, debugging]\n"
            "    maturity: experimental\n",
            encoding="utf-8",
        )
    return board_root


def _load_toml_compat(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current = result
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = result
            for part in _split_toml_dotted_key(line[1:-1]):
                current = current.setdefault(_parse_toml_key(part), {})
            continue
        if "=" not in line:
            raise ValueError(f"Invalid TOML line: {raw_line}")
        key, raw_value = line.split("=", 1)
        current[_parse_toml_key(key)] = _parse_toml_value(raw_value.strip())
    return result


def _parse_toml_value(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        # TOML literal strings use single quotes and do not process escapes.
        # Python 3.10 uses this compatibility parser because tomllib is absent.
        return value[1:-1]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        try:
            return int(value)
        except ValueError:
            return value


def _parse_toml_key(value: str) -> str:
    token = value.strip()
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        return token[1:-1]
    if token.startswith('"') and token.endswith('"'):
        parsed = json.loads(token)
        if isinstance(parsed, str):
            return parsed
    return token


def _split_toml_dotted_key(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == "." and not quoted:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _valid_claude_budget(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _valid_hermes_max_turns(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _raise_config_errors(config: dict[str, Any]) -> None:
    errors = validate_config(config)
    if errors:
        raise ABCError(
            "config_invalid",
            "; ".join(errors),
            {"errors": errors},
        )


def _replace_config_file(path: Path, text: str) -> None:
    temporary_path: Path | None = None
    descriptor = -1
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _toml_key(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Unsupported TOML key: {value!r}")
    return value if _BARE_TOML_KEY.fullmatch(value) else json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"Unsupported non-finite TOML value: {value!r}")
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError(f"Unsupported TOML value: {value!r}")
