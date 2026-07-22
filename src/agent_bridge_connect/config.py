from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier
    tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG_PATH = Path.home() / ".abc" / "config.toml"
DEFAULT_WORKSPACE_ROOT = Path("~") / "Documents" / "AgentBC" / "workspace"
DEFAULT_BOARD_ROOT = DEFAULT_WORKSPACE_ROOT / "record"


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
    for section in ("executors", "notifiers"):
        if section in config and not isinstance(config[section], dict):
            errors.append(f"{section} must be a table")
    board_root = config.get("board_root")
    if board_root is not None and not isinstance(board_root, str):
        errors.append("board_root must be a string")
    workspace_root = config.get("workspace_root")
    if workspace_root is not None and not isinstance(workspace_root, str):
        errors.append("workspace_root must be a string")
    return errors


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
            for part in line[1:-1].split("."):
                current = current.setdefault(part.strip(), {})
            continue
        if "=" not in line:
            raise ValueError(f"Invalid TOML line: {raw_line}")
        key, raw_value = line.split("=", 1)
        current[key.strip()] = _parse_toml_value(raw_value.strip())
    return result


def _parse_toml_value(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        try:
            return int(value)
        except ValueError:
            return value
