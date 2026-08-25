from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_CLAUDE_MAX_BUDGET_USD,
    DEFAULT_HERMES_MAX_TURNS,
    apply_permissions_setting,
    configured_claude_budget,
    configured_hermes_max_turns,
    configured_session_retention,
    load_config,
    resolve_config_path,
    update_config_atomic,
    validate_config,
)
from .path_provider import find_binary
from .permission_modes import (
    DEFAULT_PERMISSION_MODE,
    configured_permission_mode,
    normalize_permission_mode,
)
from .protocol import ABCError
from .skill_packages import (
    build_skill_manifest,
    classify_skill_package,
    remove_managed_skill_package,
    replace_managed_skill_package,
)


_OWNED_ALIAS_MARKER = "# AgentBC-owned abc shim"
_COMMAND_TIMEOUT_S = 10
_HERMES_CONFIG_TIMEOUT_S = 5
_HERMES_CONFIG_MAX_BYTES = 1024 * 1024
_PROTECTED_EXECUTOR_SETTINGS = {
    "claude": frozenset({"max_budget_usd"}),
    "hermes": frozenset({"max_turns"}),
}
_SKILL_TEMPLATE_FALLBACK = """# AgentBC Skill

当用户要求执行、审查或管理任务时，使用 agentbc CLI：

## 派发任务
agentbc task create --title "任务描述" --assignee hermes --steps /tmp/agentbc-steps.yaml --customer-path "default path" --dispatch

## 查看进度
agentbc K7Q9

## 生成报告
agentbc task report K7Q9-001

## 人工干预
agentbc task pause K7Q9-001 --reason "..."
agentbc task resume K7Q9-001

## 验收任务
agentbc task status K7Q9-001 --json
"""


class RuntimeCapability:
    FRONTEND_ONLY = "frontend_only"
    BACKGROUND_SINGLE = "background_single"
    BACKGROUND_MULTI_CANDIDATE = "background_multi_candidate"
    BACKGROUND_MULTI_VERIFIED = "background_multi_verified"
    SILENT_UNATTENDED = "silent_unattended"


_AGENT_DEFINITIONS = [
    {
        "name": "codex",
        "binary": "codex",
        "display": "Codex CLI",
        "capability_level": "L2",
        "supported_executor": True,
        "skill": "codex",
    },
    {
        "name": "hermes",
        "binary": "hermes",
        "display": "Hermes Agent",
        "capability_level": "L2",
        "supported_executor": True,
        "skill": "hermes",
    },
    {
        "name": "claude",
        "binary": "claude",
        "display": "Claude Code",
        "capability_level": "L1",
        "supported_executor": True,
        "skill": "claude",
    },
    {
        "name": "opencode",
        "binary": "opencode",
        "display": "OpenCode",
        "capability_level": "L0",
        "supported_executor": False,
    },
    {
        "name": "cursor",
        "binary": "cursor",
        "display": "Cursor",
        "capability_level": "frontend",
        "supported_executor": False,
    },
    {
        "name": "gemini",
        "binary": "gemini",
        "display": "Gemini CLI",
        "capability_level": "L1",
        "supported_executor": False,
    },
]


def discover_command(
    name: str,
    *,
    env_var: str | None = None,
    extra_candidates: list[Path] | None = None,
) -> dict[str, Any]:
    """Find a command using the shared PathProvider search strategy."""
    extra_paths = [str(path) for path in extra_candidates or []]
    if env_var and os.environ.get(env_var) and env_var != _env_var_for(name):
        extra_paths.insert(0, os.environ[env_var])
    result = find_binary(name, extra_paths=extra_paths)
    if env_var and os.environ.get(env_var):
        env_path = Path(os.environ[env_var]).expanduser()
        if result["found"] and _same_path(Path(result["path"]), env_path):
            result = {**result, "source": "env_override", "env_var": env_var}
    return result


def discover_opencode() -> dict[str, Any]:
    """Find OpenCode even when installed outside the user's default PATH."""
    discovery = discover_command("opencode", env_var="AGENTBC_OPENCODE_BIN")
    version = _version_for(discovery)
    return {**discovery, "version": version}


def discover_codex() -> dict[str, Any]:
    """Find the Codex binary and report its version without changing its state."""
    discovery = discover_command(
        "codex",
        env_var="AGENTBC_CODEX_BIN",
        extra_candidates=[Path.home() / ".local" / "bin" / "codex"],
    )
    version = _version_for(discovery)
    return {**discovery, "version": version}


def discover_hermes() -> dict[str, Any]:
    """Prefer the real Hermes runtime over optional shell wrappers."""
    discovery = discover_command(
        "hermes",
        env_var="AGENTBC_HERMES_BIN",
        extra_candidates=[Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"],
    )
    version = _version_for(discovery)
    return {**discovery, "version": version}


def discover_claude() -> dict[str, Any]:
    """Find Claude Code without enabling it by default."""
    discovery = discover_command(
        "claude",
        env_var="AGENTBC_CLAUDE_BIN",
        extra_candidates=[
            Path.home() / ".local" / "bin" / "claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
        ],
    )
    version = _version_for(discovery)
    return {**discovery, "version": version}


def probe_claude() -> dict[str, Any]:
    discovery = discover_claude()
    if not discovery.get("found"):
        return {
            "has_print": False,
            "has_safe_mode": False,
            "output_formats": [],
            "permission_modes": [],
            "dangerous_permissions_supported": False,
            "dangerous_permissions_policy": "explicit_persisted_full_task_only",
            "capability_grade": RuntimeCapability.BACKGROUND_SINGLE,
            "status": "not_found",
        }
    help_text = _command_output(_run_command(Path(discovery["path"]), "--help"))
    lower_help = help_text.lower()
    full_supported = "--dangerously-skip-permissions" in lower_help
    return {
        "has_print": "-p, --print" in lower_help or "--print" in lower_help,
        "has_safe_mode": "--safe-mode" in lower_help,
        "output_formats": [
            value
            for value in ("text", "json", "stream-json")
            if value in lower_help
        ],
        "permission_modes": [
            value
            for value in (
                "acceptEdits",
                "auto",
                "bypassPermissions",
                "manual",
                "dontAsk",
                "plan",
            )
            if value.lower() in lower_help
        ],
        "dangerous_permissions_supported": full_supported,
        "dangerous_permissions_policy": "explicit_persisted_full_task_only",
        "default_permission_mode": "acceptEdits",
        "default_output_format": "text",
        "capability_grade": RuntimeCapability.BACKGROUND_SINGLE,
        "status": "experimental_l1",
    }


def detect_vscode_codex() -> dict[str, Any]:
    """Detect VSCode-family Codex extensions and any bundled runtime."""
    for extension_dir in _vscode_extension_candidates():
        runtime_path = _find_bundled_codex_runtime(extension_dir)
        package = _read_package_metadata(extension_dir)
        version = str(package.get("version") or _version_from_extension_name(extension_dir))
        return {
            "found": True,
            "path": str(extension_dir),
            "runtime_path": str(runtime_path) if runtime_path is not None else "",
            "version": version,
            "source": _editor_source(extension_dir),
        }
    return {
        "found": False,
        "path": "",
        "runtime_path": "",
        "version": "",
        "source": "",
    }


def probe_codex() -> dict[str, Any]:
    """Probe supported Codex CLI flags and authentication readiness."""
    discovery = discover_codex()
    vscode = detect_vscode_codex()
    if not discovery["found"]:
        runtime_path = vscode.get("runtime_path") or ""
        if runtime_path:
            return _probe_codex_path(
                Path(runtime_path),
                version=str(vscode.get("version") or ""),
                runtime_source="vscode_extension_bundled",
                vscode=vscode,
            )
        return {
            "has_json_output": False,
            "has_sandbox": False,
            "sandbox_modes": [],
            "has_model_selection": False,
            "has_workdir": False,
            "authentication_ready": False,
            "version": discovery["version"],
            "runtime_source": "vscode_extension" if vscode["found"] else "not_found",
            "runtime_path": "",
            "capability_grade": RuntimeCapability.FRONTEND_ONLY,
            "background": "limited",
            "concurrency": "unverified",
            "max_parallel": 0,
            "silent_mode": "limited",
            "limitations": _codex_limitations("vscode_extension" if vscode["found"] else "not_found"),
            "vscode_codex": vscode,
            "action_required": (
                "No callable Codex runtime found in detected VSCode-family extensions."
                if vscode["found"]
                else "No callable Codex runtime found. Re-run setup with AGENTBC_CODEX_BIN=/path/to/codex if it is installed outside common locations."
            ),
        }

    runtime_source = _codex_runtime_source(str(discovery.get("source") or ""))
    return _probe_codex_path(
        Path(discovery["path"]),
        version=str(discovery["version"] or vscode.get("version") or ""),
        runtime_source=runtime_source,
        vscode=vscode,
    )


def scan_all_agents() -> list[dict[str, Any]]:
    """Scan every known AgentBC integration through the shared PathProvider."""
    config = _load_config(_config_path())
    enabled = set((config.get("executors") or {}).keys())
    agents = [_scan_agent(definition, enabled) for definition in _AGENT_DEFINITIONS]
    return agents


def run_show() -> dict[str, Any]:
    """Scan and display AgentBC setup state without changing files."""
    config_path = _config_path()
    config = _load_config(config_path)
    agents = scan_all_agents()
    workspace_root = str(_effective_workspace_root(config))
    _print_scan_report(agents, workspace_root)
    permission_mode, permission_source = configured_permission_mode(config)
    _print_permission_mode_help(permission_mode)
    resources = _configured_resource_report(config)
    _print_resource_settings(resources)
    return {
        "ok": True,
        "mode": "show",
        "agents": agents,
        "config_path": str(config_path),
        "workspace_root": workspace_root,
        "permission_mode": permission_mode,
        "permission_source": permission_source,
        "permission_setting": "permissions.mode",
        "permission_scope": "future_tasks",
        "resources": resources,
    }


def run_setup(
    interactive: bool = True,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    """Scan agents, write AgentBC config, and optionally install integrations."""
    config_path = _config_path()
    config = _load_config(config_path)
    agents = scan_all_agents()
    workspace_root = str(_effective_workspace_root(config))
    _print_scan_report(agents, workspace_root)
    previous_permission, _ = configured_permission_mode(config)
    selected_permission = _select_permission_mode(
        config,
        explicit_mode=permission_mode,
        interactive=interactive,
    )
    permission_touched = _permission_setting_absent(config) or (
        selected_permission != previous_permission
    )
    enabled: list[str] = []
    skipped: list[str] = []
    executor_updates: dict[str, dict[str, Any]] = {}
    resource_updates: dict[str, tuple[Any, bool, str]] = {}

    for agent in agents:
        if not agent["found"] or not agent["supported_executor"]:
            skipped.append(agent["name"])
            continue
        if interactive:
            if not _confirm(
                f"Enable {agent['display']} as executor? [Y/n] ",
                default=True,
                eof_default=False,
            ):
                skipped.append(agent["name"])
                continue
        executor_updates[agent["name"]] = _executor_config_for(agent)
        if agent["name"] == "claude":
            resource_updates["claude"] = _select_claude_budget(config, interactive=interactive)
        elif agent["name"] == "hermes":
            resource_updates["hermes"] = _select_hermes_max_turns(
                config,
                command=agent.get("path") or agent.get("binary") or "hermes",
                interactive=interactive,
            )
        enabled.append(agent["name"])

    retention_value, retention_touched, retention_source = _select_session_retention(
        config,
        interactive=interactive,
    )

    def apply_setup(latest: dict[str, Any]) -> None:
        latest.setdefault("workspace_root", str(_default_workspace_root()))
        latest.setdefault("board_root", str(_effective_workspace_root(latest) / "record"))
        if permission_touched:
            apply_permissions_setting(latest, selected_permission)
        executors = latest.setdefault("executors", {})
        for name, desired in executor_updates.items():
            current = executors.get(name)
            if not isinstance(current, dict):
                current = {}
            executors[name] = _merge_executor_config(name, current, desired)
        claude_setting = resource_updates.get("claude")
        if claude_setting and claude_setting[1]:
            executors.setdefault("claude", {})["max_budget_usd"] = claude_setting[0]
        hermes_setting = resource_updates.get("hermes")
        if hermes_setting and hermes_setting[1]:
            executors.setdefault("hermes", {})["max_turns"] = hermes_setting[0]
        if retention_touched:
            latest.setdefault("sessions", {})["retain_executor_sessions"] = retention_value

    config, config_written = update_config_atomic(apply_setup, config_path)
    workspace_root = str(_effective_workspace_root(config))
    from .config import init_board

    init_board(config["board_root"])

    skill_results: dict[str, dict[str, Any]] = {}
    codex_agent = _agent_by_name(agents, "codex")
    if codex_agent and codex_agent["found"]:
        skill_results["codex"] = (
            install_codex_skill(interactive=True)
            if interactive
            else install_codex_skill(interactive=False)
        )
    else:
        skill_results["codex"] = {
            "installed": False,
            "status": "agent_not_detected",
            "path": str(_codex_skill_root() / "SKILL.md"),
        }

    hermes = _agent_by_name(agents, "hermes")
    if hermes and hermes["found"]:
        skill_results["hermes"] = (
            install_hermes_skill(interactive=True)
            if interactive
            else install_hermes_skill(interactive=False, all_profiles=True)
        )
    else:
        skill_results["hermes"] = {
            "installed": False,
            "status": "agent_not_detected",
            "path": str(_hermes_skill_path()),
        }

    claude = _agent_by_name(agents, "claude")
    if claude and claude["found"]:
        skill_results["claude"] = (
            install_claude_skill(interactive=True)
            if interactive
            else install_claude_skill(interactive=False)
        )
    else:
        skill_results["claude"] = {
            "installed": False,
            "status": "agent_not_detected",
            "path": str(_claude_skill_path()),
        }

    alias = _configure_alias(interactive)
    agents = scan_all_agents()
    codex = discover_codex()
    capabilities = probe_codex()
    return {
        "ok": True,
        "mode": "setup",
        "config_path": str(config_path),
        "config_written": config_written,
        "workspace_root": workspace_root,
        "permission_mode": selected_permission,
        "resources": {
            **_configured_resource_report(config),
            "retention_selection_source": retention_source,
        },
        "codex": codex,
        "capabilities": capabilities,
        "agents": agents,
        "enabled": enabled,
        "skipped": skipped,
        "skill": skill_results["hermes"],
        "skills": skill_results,
        "alias": alias,
        "action_required": capabilities.get("action_required"),
    }


def run_update(interactive: bool = True) -> dict[str, Any]:
    """Scan and selectively update config or the Hermes Skill."""
    config_path = _config_path()
    config = _load_config(config_path)
    agents = scan_all_agents()
    workspace_root = str(_effective_workspace_root(config))
    _print_scan_report(agents, workspace_root)
    items = _update_items(agents, config)
    _print_selectable_items("Updates", items)
    selected = (
        _select_items(items, interactive=True)
        if interactive
        else [item for item in items if item["action"].startswith("install_")]
    )
    if interactive:
        actions = [
            _apply_update_item(item, config, force=True)
            for item in selected
        ]
        truthful = True
    else:
        actions = []
        truthful = True
        for item in selected:
            try:
                action = _apply_update_item(item, config, force=False)
            except Exception as exc:  # noqa: BLE001 - report install failures truthfully
                action = {
                    "item": item["id"],
                    "status": "install_failed",
                    "config_changed": False,
                    "error": str(exc),
                }
            actions.append(action)
            if not _update_skill_action_succeeded(item, action):
                truthful = False
    if any(action.get("config_changed") for action in actions):
        selected_executors = [
            item for item in selected if item.get("action") == "update_executor"
        ]

        def apply_updates(latest: dict[str, Any]) -> None:
            latest.setdefault("workspace_root", str(_default_workspace_root()))
            latest.setdefault("board_root", str(_effective_workspace_root(latest) / "record"))
            executors = latest.setdefault("executors", {})
            for item in selected_executors:
                agent = item["agent"]
                current = executors.get(agent["name"])
                if not isinstance(current, dict):
                    current = {}
                executors[agent["name"]] = _merge_executor_config(
                    agent["name"], current, _executor_config_for(agent)
                )

        update_config_atomic(apply_updates, config_path)
    return {
        "ok": truthful,
        "mode": "update",
        "agents": agents,
        "workspace_root": workspace_root,
        "items": items,
        "selected": [item["id"] for item in selected],
        "actions": actions,
    }


def run_clean(interactive: bool = True) -> dict[str, Any]:
    """Scan and selectively remove AgentBC-owned setup artifacts."""
    config = _load_config(_config_path())
    agents = scan_all_agents()
    workspace_root = str(_effective_workspace_root(config))
    _print_scan_report(agents, workspace_root)
    items = _clean_items()
    _print_selectable_items("Cleanable", items)
    selected = _select_items(items, interactive=interactive)
    actions = [_apply_clean_item(item) for item in selected]
    return {
        "ok": True,
        "mode": "clean",
        "agents": agents,
        "workspace_root": workspace_root,
        "items": items,
        "selected": [item["id"] for item in selected],
        "actions": actions,
    }


def run_uninstall(
    *,
    interactive: bool = True,
    remove_records: bool | None = None,
    remove_artifacts: bool | None = None,
) -> dict[str, Any]:
    """Remove AgentBC-owned runtime files with independent managed-data choices."""
    config_path = _config_path()
    config = _load_config(config_path)
    board_root = Path(config.get("board_root") or (_effective_workspace_root(config) / "record")).expanduser().resolve()
    workspace_root = _effective_workspace_root(config)
    report_root = workspace_root / "tasks" / "report"
    artifact_root = workspace_root / "tasks" / "artifacts"

    if not interactive and (remove_records is None or remove_artifacts is None):
        raise ValueError(
            "non-interactive uninstall requires an explicit records choice "
            "(--remove-records or --keep-records) and artifacts choice "
            "(--remove-artifacts or --keep-artifacts)"
        )

    if remove_records is None:
        remove_records = _confirm_uninstall_choice(
            f"Remove AgentBC runtime records at {board_root} and reports at {report_root}? [y/N] "
        )
    if remove_artifacts is None:
        remove_artifacts = _confirm_uninstall_choice(
            f"Remove AgentBC default workspace artifacts at {artifact_root}? [y/N] "
        )

    removed: list[str] = []
    preserved: list[str] = []
    skip_runner = os.environ.get("AGENTBC_UNINSTALL_SKIP_RUNNER") == "1"
    _stop_owned_runner(removed)

    hermes_result = uninstall_hermes_skill(interactive=False, force=True, all_profiles=True)
    if hermes_result.get("removed"):
        removed.extend(str(Path(path).parent) for path in hermes_result.get("paths", []))
    for result in (uninstall_claude_skill(), uninstall_codex_skill()):
        if result.get("removed"):
            removed.append(str(Path(result["path"]).parent))

    install_root = Path(os.environ.get("AGENTBC_ALPHA_HOME", Path.home() / ".agentbc-alpha")).expanduser()
    bin_dir = Path(os.environ.get("AGENTBC_BIN_DIR", Path.home() / ".local" / "bin")).expanduser()
    agentbc_link = bin_dir / "agentbc"
    if agentbc_link.is_symlink():
        target = (agentbc_link.parent / os.readlink(agentbc_link)).resolve()
        if _is_relative_to(target, install_root.resolve()):
            _remove_owned_path(agentbc_link, removed)
        else:
            preserved.append(str(agentbc_link))
    alias = bin_dir / "abc"
    try:
        owned_alias = alias.is_file() and _OWNED_ALIAS_MARKER in alias.read_text(encoding="utf-8")
    except OSError:
        owned_alias = False
    if owned_alias:
        _remove_owned_path(alias, removed)

    launch_agent = Path.home() / "Library" / "LaunchAgents" / "com.agentbc.runner.plist"
    _remove_owned_path(launch_agent, removed)
    if not skip_runner:
        # Skipping the Runner stop also preserves its live spool, token, and pid.
        from .runner import default_runner_spool

        _remove_owned_path(default_runner_spool(), removed)

    if remove_records:
        _remove_owned_path(board_root, removed)
        _remove_owned_path(report_root, removed)
    else:
        preserved.extend(str(path) for path in (board_root, report_root) if path.exists())
    if remove_artifacts:
        _remove_owned_path(artifact_root, removed)
    elif artifact_root.exists():
        preserved.append(str(artifact_root))

    default_workspace_root = _default_workspace_root().resolve()
    if remove_records and remove_artifacts and workspace_root.resolve() == default_workspace_root:
        # Both managed-data choices authorize a complete reset of AgentBC's
        # dedicated default root, including residue from older Alpha layouts.
        _remove_owned_path(default_workspace_root.parent, removed)

    _remove_config(config_path, removed)
    _remove_owned_path(install_root, removed)
    _remove_alpha_temp_traces(removed)
    _prune_empty_managed_parents(workspace_root)
    return {
        "ok": True,
        "mode": "uninstall",
        "remove_records": remove_records,
        "remove_artifacts": remove_artifacts,
        "removed": removed,
        "preserved": preserved,
        "customer_paths_touched": False,
    }


def _stop_owned_runner(removed: list[str]) -> None:
    if os.environ.get("AGENTBC_UNINSTALL_SKIP_RUNNER") == "1":
        return
    from .runner import cleanup_legacy_runner_launch_agent, stop_runner_background

    legacy = cleanup_legacy_runner_launch_agent()
    if not legacy.get("ok"):
        raise RuntimeError(
            f"refusing uninstall while legacy Runner cleanup is unverified: "
            f"{legacy.get('status')} {legacy.get('error', '')}".strip()
        )
    if legacy.get("status") == "removed":
        removed.append(f"runner:launch-agent:{legacy.get('path')}")

    result = stop_runner_background()
    if not result.get("ok"):
        raise RuntimeError(
            f"refusing uninstall while Runner cleanup is unverified: "
            f"{result.get('status')} {result.get('error', '')}".strip()
        )
    for pid in result.get("pids", []):
        removed.append(f"runner:pid:{pid}")


def _remove_owned_path(path: Path, removed: list[str]) -> None:
    path = path.expanduser()
    if not (path.exists() or path.is_symlink()):
        return
    resolved = path.resolve(strict=False)
    if resolved in {Path("/"), Path.home().resolve()}:
        raise ValueError(f"refusing unsafe removal path: {path}")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    removed.append(str(path))


def _remove_config(config_path: Path, removed: list[str]) -> None:
    _remove_owned_path(config_path, removed)
    config_root = config_path.parent
    if config_root.name == ".abc" and config_root.exists():
        _remove_owned_path(config_root, removed)


def _remove_alpha_temp_traces(removed: list[str]) -> None:
    temp_root = Path(os.environ.get("TMPDIR", "/tmp")).expanduser()
    for pattern in ("agentbc-alpha-download.*", "agentbc-alpha-smoke.*"):
        for path in temp_root.glob(pattern):
            _remove_owned_path(path, removed)


def _prune_empty_managed_parents(workspace_root: Path) -> None:
    paths: list[Path] = []
    if workspace_root == _default_workspace_root():
        paths.extend((workspace_root, workspace_root.parent))
    for path in paths:
        try:
            path.rmdir()
        except OSError:
            pass


def _codex_skill_root() -> Path:
    override = os.environ.get("AGENTBC_CODEX_SKILL_PATH")
    if override:
        path = Path(override).expanduser()
        return path.parent if path.name == "SKILL.md" else path
    return Path.home() / ".codex" / "skills" / "agentbc"


def install_codex_skill(
    path: str | Path | None = None,
    *,
    interactive: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Install or refresh the complete Codex AgentBC skill package."""
    root = Path(path).expanduser() if path is not None else _codex_skill_root()
    if root.name == "SKILL.md":
        root = root.parent
    return _install_skill_package(
        root,
        platform="codex",
        display_name="Codex Skill",
        result_path=root / "SKILL.md",
        interactive=interactive,
        force=force,
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def install_hermes_skill(
    path: str | Path | None = None,
    *,
    interactive: bool = True,
    force: bool = False,
    all_profiles: bool = True,
) -> dict[str, Any]:
    """Install the Hermes AgentBC Skill and derived command across profiles."""
    destinations = _hermes_skill_destinations(path=path, all_profiles=all_profiles)
    states = {
        destination: _classify_installed_skill(destination.parent, "hermes")
        for destination in destinations
    }
    pending = [destination for destination, state in states.items() if state["classification"] != "current"]
    result_base = {
        "path": str(destinations[0]),
        "paths": [str(destination) for destination in destinations],
        "profile_scope": "all" if path is None and all_profiles else "single",
        "profile_count": max(len(destinations) - 1, 0),
        "command": "/agentbc",
        "classifications": {
            str(destination): state["classification"] for destination, state in states.items()
        },
        "expected_manifest": states[destinations[0]]["expected_manifest"],
    }
    if not pending:
        return {
            **result_base,
            "installed": all(state["installed"] for state in states.values()),
            "changed": False,
            "status": "already_installed",
            "classification": "current",
            "manifest": states[destinations[0]]["manifest"],
        }

    modified = [
        destination for destination in pending if states[destination]["classification"] == "modified"
    ]
    if modified and not interactive and not force:
        return {
            **result_base,
            "installed": all(state["installed"] for state in states.values()),
            "changed": False,
            "status": "modified_requires_confirmation",
            "classification": "modified",
            "manifest": states[modified[0]]["manifest"],
        }

    if interactive and not force:
        print("Hermes Skill and /agentbc command require writing:")
        for destination in pending:
            print(f"  {destination}")
        confirmation = _confirm_skill_package_install(
            "Install Hermes Skill in all detected profiles?",
            preview=_load_skill_template(),
            modified=bool(modified),
        )
        if confirmation != "confirmed":
            return {
                **result_base,
                "installed": False,
                "changed": False,
                "status": (
                    "skipped_no_confirmation"
                    if confirmation == "no_confirmation"
                    else "declined"
                ),
                "classification": "modified" if modified else states[pending[0]]["classification"],
                "manifest": states[pending[0]]["manifest"],
            }

    for destination in pending:
        _write_current_skill_package(destination.parent, "hermes", states[destination])
    return {
        **result_base,
        "installed": True,
        "changed": True,
        "status": "installed",
        "classification": "current",
        "previous_classification": (
            "modified" if modified else states[pending[0]]["classification"]
        ),
        "manifest": _expected_skill_manifest("hermes"),
    }


def install_claude_skill(
    path: str | Path | None = None,
    *,
    interactive: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Install or refresh the Claude Code AgentBC controller skill."""
    destination = Path(path).expanduser() if path is not None else _claude_skill_path()
    return _install_skill_package(
        destination.parent,
        platform="claude",
        display_name="Claude Skill",
        result_path=destination,
        interactive=interactive,
        force=force,
    )


def _install_skill_package(
    root: Path,
    *,
    platform: str,
    display_name: str,
    result_path: Path,
    interactive: bool,
    force: bool,
) -> dict[str, Any]:
    state = _classify_installed_skill(root, platform)
    result_base = {
        "path": str(result_path),
        "classification": state["classification"],
        "manifest": state["manifest"],
        "expected_manifest": state["expected_manifest"],
    }
    if state["classification"] == "current":
        return {
            **result_base,
            "installed": True,
            "changed": False,
            "status": "already_installed",
        }
    if state["classification"] == "modified" and not interactive and not force:
        return {
            **result_base,
            "installed": state["installed"],
            "changed": False,
            "status": "modified_requires_confirmation",
        }
    if interactive and not force:
        print(f"{display_name} requires writing: {root}")
        confirmation = _confirm_skill_package_install(
            f"Install {display_name}?",
            preview=_current_skill_files(platform)["SKILL.md"].decode("utf-8"),
            modified=state["classification"] == "modified",
        )
        if confirmation != "confirmed":
            return {
                **result_base,
                "installed": False,
                "changed": False,
                "status": (
                    "skipped_no_confirmation"
                    if confirmation == "no_confirmation"
                    else "declined"
                ),
            }
    _write_current_skill_package(root, platform, state)
    return {
        **result_base,
        "installed": True,
        "changed": True,
        "status": "installed",
        "classification": "current",
        "previous_classification": state["classification"],
        "manifest": state["expected_manifest"],
    }


def _confirm_skill_package_install(prompt: str, *, preview: str, modified: bool) -> str:
    suffix = " [y/N/view] " if modified else " [Y/n/view] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except EOFError:
        return "no_confirmation"
    if answer in {"view", "v"}:
        print(preview)
        confirmed = _confirm(
            "Confirm replacement? [y/N] " if modified else "Confirm install? [Y/n] ",
            default=not modified,
            eof_default=False,
        )
        return "confirmed" if confirmed else "declined"
    if answer in {"y", "yes"}:
        return "confirmed"
    if answer in {"n", "no", "q", "quit"}:
        return "declined"
    return "declined" if modified else "confirmed"


def _current_skill_files(platform: str) -> dict[str, bytes]:
    skill_loaders = {
        "codex": _load_codex_skill_template,
        "claude": _load_claude_skill_template,
        "hermes": _load_skill_template,
    }
    files = {
        "SKILL.md": skill_loaders[platform]().encode("utf-8"),
        "references/agentbc-steps-yaml.md": _load_skill_reference_template().encode("utf-8"),
        "references/controller-contract.md": _load_controller_contract_template().encode("utf-8"),
    }
    if platform == "codex":
        files["agents/openai.yaml"] = _load_codex_openai_template().encode("utf-8")
    return files


def _expected_skill_manifest(platform: str) -> dict[str, Any]:
    return build_skill_manifest(platform, __version__, _current_skill_files(platform))


def _classify_installed_skill(root: Path, platform: str) -> dict[str, Any]:
    return classify_skill_package(
        root,
        platform=platform,
        package_version=__version__,
        current_files=_current_skill_files(platform),
    )


def _write_current_skill_package(root: Path, platform: str, state: dict[str, Any]) -> None:
    replace_managed_skill_package(
        root,
        platform=platform,
        files=_current_skill_files(platform),
        manifest=state["expected_manifest"],
    )


def uninstall_hermes_skill(
    path: str | Path | None = None,
    *,
    interactive: bool = True,
    force: bool = False,
    all_profiles: bool = True,
) -> dict[str, Any]:
    """Remove AgentBC's Hermes skill package from selected profile roots."""
    destinations = _hermes_skill_destinations(path=path, all_profiles=all_profiles)
    existing = [destination for destination in destinations if destination.parent.exists()]
    if not existing:
        return {
            "removed": False,
            "changed": False,
            "status": "missing",
            "path": str(destinations[0]),
            "paths": [str(destination) for destination in destinations],
        }
    if interactive and not force:
        if not _confirm(
            f"Remove Hermes Skill from {len(existing)} profile root(s)? [y/N] ",
            default=False,
            eof_default=False,
        ):
            return {
                "removed": False,
                "changed": False,
                "status": "declined",
                "path": str(destinations[0]),
                "paths": [str(destination) for destination in destinations],
            }
    removals = [
        remove_managed_skill_package(
            destination.parent,
            platform="hermes",
            package_version=__version__,
            current_files=_current_skill_files("hermes"),
        )
        for destination in existing
    ]
    changed = any(result["changed"] for result in removals)
    return {
        "removed": changed,
        "changed": changed,
        "status": "removed" if changed else "missing",
        "path": str(destinations[0]),
        "paths": [str(destination) for destination in destinations],
        "classifications": [result["classification"] for result in removals],
    }


def _uninstall_single_skill(root: Path, platform: str) -> dict[str, Any]:
    result = remove_managed_skill_package(
        root,
        platform=platform,
        package_version=__version__,
        current_files=_current_skill_files(platform),
    )
    return {
        **result,
        "status": "removed" if result["removed"] else "missing",
        "path": str(root / "SKILL.md"),
    }


def uninstall_codex_skill(path: str | Path | None = None) -> dict[str, Any]:
    root = Path(path).expanduser() if path is not None else _codex_skill_root()
    if root.name == "SKILL.md":
        root = root.parent
    return _uninstall_single_skill(root, "codex")


def uninstall_claude_skill(path: str | Path | None = None) -> dict[str, Any]:
    destination = Path(path).expanduser() if path is not None else _claude_skill_path()
    root = destination.parent if destination.name == "SKILL.md" else destination
    return _uninstall_single_skill(root, "claude")


def generate_default_config() -> dict[str, Any]:
    """Generate a default AgentBC config using discovered Codex capabilities."""
    discovery = discover_codex()
    capabilities = probe_codex()
    return _default_config(discovery, capabilities)


def _scan_agent(definition: dict[str, Any], enabled: set[str]) -> dict[str, Any]:
    name = str(definition["name"])
    binary = str(definition["binary"])
    if name == "codex":
        discovery = discover_codex()
        capabilities = probe_codex()
    elif name == "hermes":
        discovery = discover_hermes()
        capabilities = {}
    elif name == "claude":
        discovery = discover_claude()
        capabilities = probe_claude()
    elif name == "opencode":
        discovery = discover_opencode()
        capabilities = {}
    else:
        discovery = discover_command(binary, env_var=_env_var_for(binary))
        discovery = {**discovery, "version": _version_for(discovery)}
        capabilities = {}

    agent = {
        **definition,
        "found": bool(discovery["found"]),
        "path": str(discovery.get("path") or ""),
        "version": str(discovery.get("version") or ""),
        "source": str(discovery.get("source") or "not_found"),
        "searched_paths": list(discovery.get("searched_paths") or []),
        "manual_override": str(discovery.get("manual_override") or ""),
        "enabled": name in enabled,
        "capabilities": capabilities,
    }
    if definition.get("skill") == "codex":
        agent["skill"] = _codex_skill_state()
    elif definition.get("skill") == "hermes":
        agent["skill"] = _hermes_skill_state()
    elif definition.get("skill") == "claude":
        agent["skill"] = _claude_skill_state()
    return agent


def _probe_codex_path(
    codex_path: Path,
    version: str,
    runtime_source: str,
    vscode: dict[str, Any],
) -> dict[str, Any]:
    main_help = _command_output(_run_command(codex_path, "--help"))
    exec_help = _command_output(_run_command(codex_path, "exec", "--help"))
    combined_help = f"{main_help}\n{exec_help}".lower()
    auth_result = _run_command(codex_path, "login", "status")
    auth_ready = auth_result is not None and auth_result.returncode == 0

    sandbox_modes = [
        mode
        for mode in ("read-only", "workspace-write", "danger-full-access")
        if mode in combined_help
    ]
    return {
        "has_json_output": "--json" in combined_help,
        "has_sandbox": "--sandbox" in combined_help,
        "sandbox_modes": sandbox_modes,
        "has_model_selection": "--model" in combined_help,
        "has_workdir": "--cd" in combined_help or "-c, --cd" in combined_help,
        "authentication_ready": auth_ready,
        "version": version,
        "runtime_source": runtime_source,
        "runtime_path": str(codex_path),
        "capability_grade": _capability_grade(runtime_source),
        "background": "available",
        "concurrency": "unverified",
        "max_parallel": 1,
        "silent_mode": "limited",
        "limitations": _codex_limitations(runtime_source),
        "vscode_codex": vscode,
        "action_required": None if auth_ready else "Run codex login to authenticate.",
    }


def _default_config(
    discovery: dict[str, Any],
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    sandbox_modes = list(capabilities.get("sandbox_modes") or [])
    sandbox = "workspace-write" if "workspace-write" in sandbox_modes else ""
    return {
        "board_root": str(_default_workspace_root() / "record"),
        "workspace_root": str(_default_workspace_root()),
        "permission_mode": DEFAULT_PERMISSION_MODE,
        "executors": {
            "codex": {
                "type": "codex",
                "command": capabilities.get("runtime_path") or discovery.get("path") or "codex",
                "api_key_env": "OPENAI_API_KEY",
                "json_output": bool(capabilities.get("has_json_output")),
                "sandbox": sandbox,
                "sandbox_modes": sandbox_modes,
                "model_selection": bool(capabilities.get("has_model_selection")),
                "workdir": bool(capabilities.get("has_workdir")),
                "runtime_source": capabilities.get("runtime_source", "not_found"),
                "background": capabilities.get("background", "limited"),
                "concurrency": capabilities.get("concurrency", "unverified"),
                "max_parallel": capabilities.get("max_parallel", 1),
                "silent_mode": capabilities.get("silent_mode", "limited"),
                "limitations": list(capabilities.get("limitations") or []),
            }
        },
    }


def _executor_config_for(agent: dict[str, Any]) -> dict[str, Any]:
    if agent["name"] == "codex":
        discovery = discover_codex()
        capabilities = probe_codex()
        return _default_config(discovery, capabilities)["executors"]["codex"]
    result = {
        "type": agent["name"],
        "command": agent["path"] or agent["binary"],
        "runtime_source": agent["source"],
        "capability_level": agent["capability_level"],
        "version": agent["version"],
    }
    if agent["name"] == "hermes":
        result["transport"] = "runner"
        result["quiet"] = False
    if agent["name"] == "claude":
        result["transport"] = "runner"
        result["safe_mode"] = True
        result["permission_mode"] = "acceptEdits"
        result["output_format"] = "text"
        result["max_budget_usd"] = DEFAULT_CLAUDE_MAX_BUDGET_USD
        result["allowed_tools"] = ["Read", "Write", "Edit", "Bash"]
    return result


def _merge_executor_config(
    name: str,
    current: dict[str, Any],
    desired: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(current)
    protected = _PROTECTED_EXECUTOR_SETTINGS.get(name, frozenset())
    for key, value in desired.items():
        if key not in protected:
            merged[key] = value
    return merged


def _load_config(path: Path) -> dict[str, Any]:
    config = load_config(path)
    errors = validate_config(config)
    if errors:
        raise ABCError("config_invalid", "; ".join(errors), {"errors": errors})
    return config


def resolve_hermes_default_max_turns(command: str) -> tuple[int, str]:
    """Read only Hermes' documented max-turns field without exposing its config."""
    try:
        completed = subprocess.run(
            [command, "config", "path"],
            capture_output=True,
            text=True,
            timeout=_HERMES_CONFIG_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) != 1:
        return DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"
    path = Path(lines[0]).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > _HERMES_CONFIG_MAX_BYTES:
            return DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"
    nested, legacy = _extract_hermes_max_turns(text)
    if nested is not None:
        return nested, "hermes_agent_config"
    if legacy is not None:
        return legacy, "hermes_legacy_config"
    return DEFAULT_HERMES_MAX_TURNS, "hermes_default_90"


def _extract_hermes_max_turns(text: str) -> tuple[int | None, int | None]:
    nested: int | None = None
    legacy: int | None = None
    in_agent = False
    agent_child_indent: int | None = None
    for raw_line in text.splitlines():
        content = raw_line.split("#", 1)[0].rstrip()
        if not content.strip() or "\t" in content[: len(content) - len(content.lstrip())]:
            continue
        indent = len(content) - len(content.lstrip(" "))
        stripped = content.strip()
        if indent == 0:
            in_agent = stripped == "agent:"
            agent_child_indent = None
            value = _plain_yaml_positive_int(stripped, "max_turns")
            if value is not None:
                legacy = value
            continue
        if in_agent:
            if agent_child_indent is None:
                agent_child_indent = indent
            value = _plain_yaml_positive_int(stripped, "max_turns")
            if value is not None and indent == agent_child_indent:
                nested = value
    return nested, legacy


def _plain_yaml_positive_int(line: str, key: str) -> int | None:
    prefix = f"{key}:"
    if not line.startswith(prefix):
        return None
    raw = line[len(prefix) :].strip()
    if not raw.isascii() or not raw.isdecimal():
        return None
    value = int(raw)
    return value if value > 0 else None


def _select_claude_budget(
    config: dict[str, Any],
    *,
    interactive: bool,
) -> tuple[float, bool, str]:
    current_table = config.get("executors", {}).get("claude", {})
    has_current = isinstance(current_table, dict) and "max_budget_usd" in current_table
    current, current_source = configured_claude_budget(config)
    if not interactive:
        return (current, not has_current, current_source)
    prompt = (
        f"Claude max budget current ${current:g} "
        f"[Enter=keep, 1=default ${DEFAULT_CLAUDE_MAX_BUDGET_USD:g}, 2=custom]: "
        if has_current
        else f"Claude max budget [1=default ${DEFAULT_CLAUDE_MAX_BUDGET_USD:g}, 2=custom] (1): "
    )
    answer = _prompt(prompt, default="").strip().lower()
    if has_current and not answer:
        return current, False, "configured"
    if not answer or answer in {"1", "default"}:
        return DEFAULT_CLAUDE_MAX_BUDGET_USD, True, "claude_default_10"
    if answer not in {"2", "custom"}:
        print("Choose 1 for default or 2 for custom.")
        return _select_claude_budget(config, interactive=interactive)
    while True:
        raw = _prompt("Claude max budget USD: ", default="").strip()
        if not raw:
            return (current, False, "configured") if has_current else (
                DEFAULT_CLAUDE_MAX_BUDGET_USD,
                True,
                "claude_default_10",
            )
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0 and value != float("inf") and value == value:
            return value, True, "custom"
        print("Enter a positive finite number.")


def _select_hermes_max_turns(
    config: dict[str, Any],
    *,
    command: str,
    interactive: bool,
) -> tuple[int, bool, str]:
    current_table = config.get("executors", {}).get("hermes", {})
    has_current = isinstance(current_table, dict) and "max_turns" in current_table
    current, current_source = configured_hermes_max_turns(config)
    if not interactive:
        if has_current:
            return current, False, current_source
        value, source = resolve_hermes_default_max_turns(command)
        return value, True, source
    prompt = (
        f"Hermes max turns current {current} [Enter=keep, 1=use Hermes default, 2=custom]: "
        if has_current
        else "Hermes max turns [1=use Hermes default, 2=custom] (1): "
    )
    answer = _prompt(prompt, default="").strip().lower()
    if has_current and not answer:
        return current, False, "configured"
    if not answer or answer in {"1", "default"}:
        value, source = resolve_hermes_default_max_turns(command)
        return value, True, source
    if answer not in {"2", "custom"}:
        print("Choose 1 for default or 2 for custom.")
        return _select_hermes_max_turns(config, command=command, interactive=interactive)
    while True:
        raw = _prompt("Hermes max turns: ", default="").strip()
        if not raw:
            if has_current:
                return current, False, "configured"
            value, source = resolve_hermes_default_max_turns(command)
            return value, True, source
        if raw.isascii() and raw.isdecimal() and int(raw) > 0:
            return int(raw), True, "custom"
        print("Enter a positive integer.")


def _select_session_retention(
    config: dict[str, Any],
    *,
    interactive: bool,
) -> tuple[bool, bool, str]:
    sessions = config.get("sessions")
    has_current = isinstance(sessions, dict) and "retain_executor_sessions" in sessions
    current, source = configured_session_retention(config)
    if not interactive:
        return current, not has_current, source
    if has_current:
        default_label = "enabled" if current else "disabled"
        prompt = f"Retain executor temporary sessions? [y/n, Enter={default_label}]: "
    else:
        prompt = "Retain executor temporary sessions? [y/N]: "
    answer = _prompt(prompt, default="").strip().lower()
    if not answer:
        return current, not has_current, source
    if answer in {"y", "yes", "enable", "enabled"}:
        return True, True, "interactive"
    if answer in {"n", "no", "disable", "disabled"}:
        return False, True, "interactive"
    print("Choose y or n.")
    return _select_session_retention(config, interactive=interactive)


def _configured_resource_report(config: dict[str, Any]) -> dict[str, Any]:
    claude, claude_source = configured_claude_budget(config)
    hermes, hermes_source = configured_hermes_max_turns(config)
    retention, retention_source = configured_session_retention(config)
    return {
        "claude_max_budget_usd": claude,
        "claude_source": claude_source,
        "hermes_max_turns": hermes,
        "hermes_source": hermes_source,
        "retain_executor_sessions": retention,
        "retention_source": retention_source,
    }


def _print_resource_settings(resources: dict[str, Any]) -> None:
    print()
    print("Executor resource settings:")
    print(
        f"  Claude max budget: ${resources['claude_max_budget_usd']:g} "
        f"({resources['claude_source']})"
    )
    print(
        f"  Hermes max turns: {resources['hermes_max_turns']} "
        f"({resources['hermes_source']})"
    )
    print(
        "  Retain executor sessions: "
        f"{str(resources['retain_executor_sessions']).lower()} "
        f"({resources['retention_source']})"
    )


def _update_items(agents: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    executors = config.get("executors") if isinstance(config.get("executors"), dict) else {}
    for agent in agents:
        if not agent["found"] or not agent["supported_executor"]:
            continue
        current = executors.get(agent["name"], {})
        desired = _executor_config_for(agent)
        merged = _merge_executor_config(
            agent["name"], current if isinstance(current, dict) else {}, desired
        )
        if not isinstance(current, dict) or current != merged:
            status = "enable" if not current else "refresh"
            items.append(
                {
                    "id": f"executor:{agent['name']}",
                    "label": f"{agent['name']} executor",
                    "status": status,
                    "action": "update_executor",
                    "agent": agent,
                }
            )
    codex = _agent_by_name(agents, "codex")
    if codex and codex.get("skill"):
        skill = codex["skill"]
        if not skill["installed"] or not skill["up_to_date"]:
            items.append(
                {
                    "id": "skill:codex",
                    "label": "Codex Skill",
                    "status": "install" if not skill["installed"] else "refresh",
                    "action": "install_codex_skill",
                    "path": skill["path"],
                }
            )
    hermes = _agent_by_name(agents, "hermes")
    if hermes and hermes.get("skill"):
        skill = hermes["skill"]
        if not skill["installed"] or not skill["up_to_date"]:
            items.append(
                {
                    "id": "skill:hermes",
                    "label": "hermes Skill + /agentbc command (all profiles)",
                    "status": "install" if not skill["installed"] else "refresh",
                    "action": "install_skill",
                    "path": skill["path"],
                }
            )
    claude = _agent_by_name(agents, "claude")
    if claude and claude.get("skill"):
        skill = claude["skill"]
        if not skill["installed"] or not skill["up_to_date"]:
            items.append(
                {
                    "id": "skill:claude",
                    "label": "claude Skill",
                    "status": "install" if not skill["installed"] else "refresh",
                    "action": "install_claude_skill",
                    "path": skill["path"],
                }
            )
    return items


def _clean_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    skill_paths = _hermes_skill_destinations(all_profiles=True)
    if any(skill_path.parent.exists() for skill_path in skill_paths):
        items.append(
            {
                "id": "skill:hermes",
                "label": "hermes Skill + /agentbc command (all profiles)",
                "path": str(skill_paths[0].parent),
                "action": "remove_skill",
            }
        )
    claude_root = _claude_skill_path().parent
    if claude_root.exists():
        items.append(
            {
                "id": "skill:claude",
                "label": "Claude Skill",
                "path": str(claude_root),
                "action": "remove_claude_skill",
            }
        )
    codex_root = _codex_skill_root()
    if codex_root.exists():
        items.append(
            {
                "id": "skill:codex",
                "label": "Codex Skill",
                "path": str(codex_root),
                "action": "remove_codex_skill",
            }
        )
    alias_path = Path.home() / ".local" / "bin" / "abc"
    if alias_path.exists():
        items.append(
            {
                "id": "alias:abc",
                "label": "abc alias",
                "path": str(alias_path),
                "action": "remove_alias",
                "owned": _is_owned_alias(alias_path),
            }
        )
    config_path = _config_path()
    if config_path.exists():
        items.append(
            {
                "id": "config",
                "label": "config file",
                "path": str(config_path),
                "action": "remove_config",
            }
        )
    board_root = _default_workspace_root() / "record"
    if board_root.exists():
        items.append(
            {
                "id": "board",
                "label": "task board",
                "path": str(board_root),
                "action": "remove_board",
            }
        )
    return items


def _apply_update_item(
    item: dict[str, Any],
    config: dict[str, Any],
    *,
    force: bool = True,
) -> dict[str, Any]:
    action = item["action"]
    if action == "update_executor":
        agent = item["agent"]
        config.setdefault("workspace_root", str(_default_workspace_root()))
        config.setdefault("board_root", str(_effective_workspace_root(config) / "record"))
        executors = config.setdefault("executors", {})
        current = executors.get(agent["name"])
        executors[agent["name"]] = _merge_executor_config(
            agent["name"], current if isinstance(current, dict) else {}, _executor_config_for(agent)
        )
        return {"item": item["id"], "status": "updated", "config_changed": True}
    if action == "install_skill":
        result = install_hermes_skill(interactive=False, force=force, all_profiles=True)
        return {"item": item["id"], "status": result["status"], "config_changed": False, **result}
    if action == "install_claude_skill":
        result = install_claude_skill(path=item["path"], interactive=False, force=force)
        return {"item": item["id"], "status": result["status"], "config_changed": False, **result}
    if action == "install_codex_skill":
        result = install_codex_skill(path=item["path"], interactive=False, force=force)
        return {"item": item["id"], "status": result["status"], "config_changed": False, **result}
    raise AssertionError(action)


def _update_skill_action_succeeded(item: dict[str, Any], action: dict[str, Any]) -> bool:
    """True only when a non-interactive Skill update is current after the action.

    A blocked modified Skill, an install failure, or any state that is still
    not current after the action makes the whole update untruthful.
    """
    status = action.get("status")
    if status == "modified_requires_confirmation":
        return False
    if status not in {"installed", "already_installed"}:
        return False
    action_name = item.get("action")
    if action_name == "install_skill":
        return _hermes_skill_state()["up_to_date"]
    if action_name == "install_claude_skill":
        root = Path(item["path"]).parent
        return _classify_installed_skill(root, "claude")["classification"] == "current"
    if action_name == "install_codex_skill":
        root = Path(item["path"]).parent
        return _classify_installed_skill(root, "codex")["classification"] == "current"
    return False


def _apply_clean_item(item: dict[str, Any]) -> dict[str, Any]:
    action = item["action"]
    if action == "remove_skill":
        result = uninstall_hermes_skill(interactive=False, force=True)
        return {"item": item["id"], **result}
    if action == "remove_claude_skill":
        return {"item": item["id"], **uninstall_claude_skill(item["path"])}
    if action == "remove_codex_skill":
        return {"item": item["id"], **uninstall_codex_skill(item["path"])}
    if action == "remove_alias":
        path = Path(item["path"])
        if not item.get("owned"):
            return {"item": item["id"], "removed": False, "status": "unowned_alias_preserved", "path": str(path)}
        path.unlink()
        return {"item": item["id"], "removed": True, "status": "removed", "path": str(path)}
    if action == "remove_config":
        path = Path(item["path"])
        path.unlink()
        return {"item": item["id"], "removed": True, "status": "removed", "path": str(path)}
    if action == "remove_board":
        path = Path(item["path"])
        shutil.rmtree(path)
        return {"item": item["id"], "removed": True, "status": "removed", "path": str(path)}
    raise AssertionError(action)


def _print_scan_report(agents: list[dict[str, Any]], workspace_root: str) -> None:
    print("AgentBC environment scan")
    print()
    print(f"{'Agent':<12} {'Status':<10} {'Version':<18} {'Capability':<10} Skill")
    print("-" * 70)
    for agent in agents:
        status = "installed" if agent["found"] else "missing"
        version = agent["version"] or "-"
        skill = "-"
        if agent.get("skill"):
            skill = "installed" if agent["skill"]["installed"] else "missing"
            if agent["skill"]["installed"] and not agent["skill"]["up_to_date"]:
                skill = "outdated"
        print(
            f"{agent['name']:<12} {status:<10} {version[:17]:<18} "
            f"{agent['capability_level']:<10} {skill}"
        )
    print()
    for agent in agents:
        if agent["found"]:
            continue
        print(f"{agent['display']} not detected.")
        print(f"  searched paths: {', '.join(agent['searched_paths'])}")
        print(f"  manual override: {agent['manual_override']}")
    claude = _agent_by_name(agents, "claude")
    if claude and claude["found"]:
        capabilities = claude.get("capabilities") or {}
        safe = "yes" if capabilities.get("has_safe_mode") else "unknown"
        print("Claude L1 safety probe:")
        print(
            "  "
            f"print={bool(capabilities.get('has_print'))} "
            f"safe_mode={safe} "
            f"default_permission={capabilities.get('default_permission_mode', 'acceptEdits')} "
            f"default_output={capabilities.get('default_output_format', 'text')} "
            "dangerous_permissions="
            + (
                "explicit-persisted-full-task-only"
                if capabilities.get("dangerous_permissions_supported")
                else "unsupported"
            )
        )
    print()
    enabled = [agent["name"] for agent in agents if agent["enabled"]]
    disabled = [agent["name"] for agent in agents if agent["found"] and not agent["enabled"]]
    print(f"enabled executors: {', '.join(enabled) if enabled else '-'}")
    print(f"detected but not enabled: {', '.join(disabled) if disabled else '-'}")
    print(f"workspace root: {workspace_root}")


def _permission_setting_absent(config: dict[str, Any]) -> bool:
    """True when neither the unified nor the legacy permission key exists."""
    permissions = config.get("permissions")
    return not (
        (isinstance(permissions, dict) and "mode" in permissions)
        or "permission_mode" in config
    )


def _print_permission_mode_help(selected: str) -> None:
    print()
    print("Execution permission modes:")
    print("  safe     keep AgentBC's conservative executor behavior")
    print("  full     grant the executor its maximum documented noninteractive access")
    print("  inherit  use the executor's existing user/global permission settings (legacy/advanced)")
    print("  WARNING: full is auditable per task and can bypass executor safety checks.")
    print(f"default permission mode: {selected}")
    print("The setting affects future dispatched tasks only; it never changes active,")
    print("input_required, needs_recovery tasks or the same-task resume path.")


def _select_permission_mode(
    config: dict[str, Any],
    *,
    explicit_mode: str | None,
    interactive: bool,
) -> str:
    current, _ = configured_permission_mode(config)
    _print_permission_mode_help(current)
    if explicit_mode is not None:
        return normalize_permission_mode(explicit_mode)
    if not interactive:
        return current
    while True:
        try:
            answer = input(
                f"Permission mode for future tasks [safe/full] (current: {current}; "
                f"'inherit' for legacy): "
            )
        except (EOFError, KeyboardInterrupt):
            return current
        value = answer.strip().lower()
        if not value:
            return current
        try:
            return normalize_permission_mode(value)
        except ABCError:
            print("Choose inherit, safe, or full.")


def _print_selectable_items(title: str, items: list[dict[str, Any]]) -> None:
    print()
    print(f"{title}:")
    if not items:
        print("  none")
        return
    for index, item in enumerate(items, 1):
        status = item.get("status") or item.get("path") or ""
        print(f"  {index}. {item['label']} {status}")


def _select_items(items: list[dict[str, Any]], *, interactive: bool) -> list[dict[str, Any]]:
    if not items:
        return []
    if not interactive:
        return []
    answer = _prompt("Select numbers, all, or q: ", default="q").strip().lower()
    if answer in {"q", "quit", ""}:
        return []
    if answer == "all":
        return items
    selected: list[dict[str, Any]] = []
    for part in answer.split(","):
        try:
            index = int(part.strip())
        except ValueError:
            continue
        if 1 <= index <= len(items):
            selected.append(items[index - 1])
    return selected


def _hermes_skill_state() -> dict[str, Any]:
    paths = _hermes_skill_destinations(all_profiles=True)
    states = [_classify_installed_skill(path.parent, "hermes") for path in paths]
    classifications = [state["classification"] for state in states]
    installed = all(state["installed"] for state in states)
    up_to_date = all(classification == "current" for classification in classifications)
    classification = _combined_skill_classification(classifications)
    return {
        "installed": installed,
        "path": str(paths[0]),
        "paths": [str(path) for path in paths],
        "profile_scope": "all",
        "command": "/agentbc",
        "up_to_date": up_to_date,
        "classification": classification,
        "classifications": classifications,
        "manifest": states[0]["manifest"],
        "expected_manifest": states[0]["expected_manifest"],
        "current_version": classification if classification != "missing" else "",
        "desired_version": "current",
    }


def _codex_skill_state() -> dict[str, Any]:
    root = _codex_skill_root()
    skill_path = root / "SKILL.md"
    state = _classify_installed_skill(root, "codex")
    return {
        "installed": state["installed"],
        "path": str(skill_path),
        "up_to_date": state["up_to_date"],
        "classification": state["classification"],
        "manifest": state["manifest"],
        "expected_manifest": state["expected_manifest"],
        "current_version": state["classification"] if state["installed"] else "",
        "desired_version": "current",
    }


def _claude_skill_state() -> dict[str, Any]:
    path = _claude_skill_path()
    state = _classify_installed_skill(path.parent, "claude")
    return {
        "installed": state["installed"],
        "path": str(path),
        "up_to_date": state["up_to_date"],
        "classification": state["classification"],
        "manifest": state["manifest"],
        "expected_manifest": state["expected_manifest"],
        "current_version": state["classification"] if state["installed"] else "",
        "desired_version": "current",
    }


def _combined_skill_classification(classifications: list[str]) -> str:
    if classifications and len(set(classifications)) == 1:
        return classifications[0]
    if "modified" in classifications:
        return "modified"
    if all(classification == "missing" for classification in classifications):
        return "missing"
    return "partial"


def _hermes_skill_path() -> Path:
    override = os.environ.get("AGENTBC_HERMES_SKILL_PATH")
    if override:
        return Path(override).expanduser()
    return _active_hermes_home() / "skills" / "agentbc" / "SKILL.md"


def _active_hermes_home() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    return Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"


def _hermes_base_home() -> Path:
    active = _active_hermes_home()
    if active.parent.name == "profiles":
        return active.parent.parent
    return active


def _hermes_skill_destinations(
    path: str | Path | None = None,
    *,
    all_profiles: bool = True,
) -> list[Path]:
    if path is not None:
        return [Path(path).expanduser()]
    override = os.environ.get("AGENTBC_HERMES_SKILL_PATH")
    if override:
        return [Path(override).expanduser()]
    active = _active_hermes_home()
    if not all_profiles:
        return [active / "skills" / "agentbc" / "SKILL.md"]
    base = _hermes_base_home()
    roots = [base]
    profiles_root = base / "profiles"
    if profiles_root.is_dir():
        roots.extend(
            profile
            for profile in sorted(profiles_root.iterdir(), key=lambda item: item.name.lower())
            if profile.is_dir() and not profile.name.startswith(".")
        )
    if active not in roots:
        roots.append(active)
    return [root / "skills" / "agentbc" / "SKILL.md" for root in roots]


def _claude_skill_path() -> Path:
    override = os.environ.get("AGENTBC_CLAUDE_SKILL_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "skills" / "agentbc" / "SKILL.md"


def _load_skill_template() -> str:
    template_path = Path(__file__).resolve().parent / "skills" / "hermes_skill.md"
    try:
        return template_path.read_text(encoding="utf-8").rstrip() + "\n"
    except OSError:
        return _SKILL_TEMPLATE_FALLBACK


def _load_claude_skill_template() -> str:
    template_path = Path(__file__).resolve().parent / "skills" / "claude_skill.md"
    return template_path.read_text(encoding="utf-8").rstrip() + "\n"


def _load_codex_skill_template() -> str:
    template_path = Path(__file__).resolve().parent / "skills" / "codex_skill.md"
    return template_path.read_text(encoding="utf-8").rstrip() + "\n"


def _load_codex_openai_template() -> str:
    template_path = Path(__file__).resolve().parent / "skills" / "codex_openai.yaml"
    return template_path.read_text(encoding="utf-8").rstrip() + "\n"


def _load_skill_reference_template() -> str:
    template_path = (
        Path(__file__).resolve().parent
        / "skills"
        / "references"
        / "agentbc-steps-yaml.md"
    )
    return template_path.read_text(encoding="utf-8").rstrip() + "\n"


def _load_controller_contract_template() -> str:
    template_path = (
        Path(__file__).resolve().parent
        / "skills"
        / "references"
        / "controller-contract.md"
    )
    return template_path.read_text(encoding="utf-8").rstrip() + "\n"


def _configure_alias(interactive: bool) -> dict[str, Any]:
    alias_path = Path.home() / ".local" / "bin" / "abc"
    locations = _find_command_locations("abc")
    if alias_path.exists():
        locations = _unique_paths([*locations, alias_path])
    unknown_locations = [
        location
        for location in locations
        if not _same_path(location, alias_path) or not _is_owned_alias(location)
    ]
    if unknown_locations:
        return {
            "installed": False,
            "path": str(alias_path),
            "status": "conflict",
            "conflicts": [str(location) for location in unknown_locations],
        }
    if not interactive:
        return {
            "installed": _is_owned_alias(alias_path),
            "path": str(alias_path),
            "status": "skipped_non_interactive",
            "conflicts": [],
        }

    if not _confirm(f"Install optional 'abc' alias at {alias_path}? [y/N] ", default=False, eof_default=False):
        return {
            "installed": _is_owned_alias(alias_path),
            "path": str(alias_path),
            "status": "declined",
            "conflicts": [],
        }

    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text(
        f"#!/bin/sh\n{_OWNED_ALIAS_MARKER}\nexec agentbc \"$@\"\n",
        encoding="utf-8",
    )
    alias_path.chmod(0o755)
    return {
        "installed": True,
        "path": str(alias_path),
        "status": "installed",
        "conflicts": [],
    }


def _find_command_locations(name: str) -> list[Path]:
    locations: list[Path] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory).expanduser() / name
        if candidate.exists():
            locations.append(candidate.resolve())
    return _unique_paths(locations)


def _version_for(discovery: dict[str, Any]) -> str:
    if not discovery.get("found"):
        return ""
    result = _run_command(Path(discovery["path"]), "--version")
    return (result.stdout or result.stderr).strip() if result is not None else ""


def _run_command(path: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [str(path), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _vscode_extension_candidates() -> list[Path]:
    bases = [
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".cursor" / "extensions",
        Path.home() / ".windsurf" / "extensions",
        Path.home() / "Library" / "Application Support" / "Code" / "User" / "extensions",
        Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "extensions",
        Path.home() / "Library" / "Application Support" / "Windsurf" / "User" / "extensions",
    ]
    prefixes = (
        "openai.chatgpt-",
        "openai.codex-",
        "anthropic.claude-code-",
        "codex-",
    )
    candidates: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir(), reverse=True):
            if child.is_dir() and child.name.startswith(prefixes):
                candidates.append(child)
    return candidates


def _find_bundled_codex_runtime(extension_dir: Path) -> Path | None:
    preferred = [
        extension_dir / "bin" / "macos-aarch64" / "codex",
        extension_dir / "bin" / "darwin-arm64" / "codex",
        extension_dir / "bin" / "darwin-x64" / "codex",
        extension_dir / "bin" / "codex",
        extension_dir / "codex",
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    try:
        for candidate in extension_dir.rglob("codex"):
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def _read_package_metadata(extension_dir: Path) -> dict[str, Any]:
    package_path = extension_dir / "package.json"
    try:
        data = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _version_from_extension_name(extension_dir: Path) -> str:
    parts = extension_dir.name.split("-")
    for index, part in enumerate(parts):
        if part and part[0].isdigit():
            return "-".join(parts[index:])
    return ""


def _editor_source(extension_dir: Path) -> str:
    text = str(extension_dir)
    if "/.cursor/" in text or "/Cursor/" in text:
        return "cursor"
    if "/.windsurf/" in text or "/Windsurf/" in text:
        return "windsurf"
    return "vscode"


def _codex_runtime_source(source: str) -> str:
    if source.endswith("_extension_bundled"):
        return "vscode_extension_bundled"
    return "standalone_cli"


def _capability_grade(runtime_source: str) -> str:
    if runtime_source == "vscode_extension_bundled":
        return RuntimeCapability.BACKGROUND_SINGLE
    if runtime_source == "standalone_cli":
        return RuntimeCapability.BACKGROUND_MULTI_CANDIDATE
    return RuntimeCapability.FRONTEND_ONLY


def _codex_limitations(runtime_source: str) -> list[str]:
    if runtime_source == "standalone_cli":
        return [
            "multi-run concurrency has not been verified",
            "some approvals may still require user interaction",
        ]
    if runtime_source == "vscode_extension_bundled":
        return [
            "runtime comes from VSCode extension bundle",
            "multi-run concurrency has not been verified",
            "some approvals may still require user interaction",
        ]
    if runtime_source == "vscode_extension":
        return [
            "VSCode-family extension detected without a callable bundled runtime",
        ]
    return ["no Codex runtime detected"]


def _command_output(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return ""
    return f"{result.stdout}\n{result.stderr}"


def _config_path() -> Path:
    return resolve_config_path()


def _default_workspace_root() -> Path:
    return (Path.home() / "Documents" / "AgentBC" / "workspace").resolve()


def _effective_workspace_root(config: dict[str, Any]) -> Path:
    value = config.get("workspace_root")
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return _default_workspace_root()


def _env_var_for(name: str) -> str:
    normalized = "".join(char if char.isalnum() else "_" for char in name).upper()
    return f"AGENTBC_{normalized}_BIN"


def _prompt(prompt: str, *, default: str = "") -> str:
    try:
        return input(prompt)
    except EOFError:
        return default


def _confirm(prompt: str, *, default: bool, eof_default: bool) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return eof_default
    if not answer.strip():
        return default
    return answer.strip().lower() in {"y", "yes"}


def _confirm_uninstall_choice(prompt: str) -> bool:
    """Require an explicit uninstall data choice from stdin or the controlling TTY."""
    try:
        answer = input(prompt)
    except EOFError:
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write(prompt)
                tty.flush()
                answer = tty.readline()
        except OSError as exc:
            raise RuntimeError(
                "uninstall data choice unavailable; run from an interactive terminal or "
                "pass explicit --remove/--keep flags"
            ) from exc
    return answer.strip().lower() in {"y", "yes"}


def _agent_by_name(agents: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((agent for agent in agents if agent["name"] == name), None)


def _is_owned_alias(path: Path) -> bool:
    try:
        return path.is_file() and _OWNED_ALIAS_MARKER in path.read_text(
            encoding="utf-8"
        ).splitlines()[:2]
    except OSError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.expanduser() == right.expanduser()


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = str(path.expanduser())
        if marker not in seen:
            seen.add(marker)
            unique.append(path.expanduser())
    return unique
