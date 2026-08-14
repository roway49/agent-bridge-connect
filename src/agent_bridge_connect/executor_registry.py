from __future__ import annotations

import importlib

from .adapters import ExecutorPort


BUILTIN_EXECUTORS = {
    "mock": "agent_bridge_connect.executors.mock.MockExecutor",
    "shell": "agent_bridge_connect.executors.shell.ShellExecutor",
    "codex": "agent_bridge_connect.executors.codex.CodexExecutor",
    "hermes": "agent_bridge_connect.executors.hermes.HermesExecutor",
    "claude": "agent_bridge_connect.executors.claude.ClaudeExecutor",
}

BUILTIN_EXECUTOR_RUNTIME_KEYS = {
    "mock": frozenset(),
    "shell": frozenset(),
    "codex": frozenset({"timeout_s", "command"}),
    "hermes": frozenset(
        {
            "timeout_s",
            "profile",
            "provider",
            "model",
            "max_turns",
            "quiet",
            "command",
            "transport",
            "runner_spool",
            "runner_token",
        }
    ),
    "claude": frozenset(
        {
            "timeout_s",
            "model",
            "effort",
            "permission_mode",
            "safe_mode",
            "output_format",
            "max_budget_usd",
            "allowed_tools",
            "command",
            "transport",
            "runner_spool",
            "runner_token",
        }
    ),
}

BUILTIN_EXECUTOR_CONFIG_ONLY_KEYS = {
    "mock": frozenset(),
    "shell": frozenset(),
    "codex": frozenset(),
    "hermes": frozenset(),
    "claude": frozenset(),
}

EXECUTOR_METADATA_KEYS = frozenset(
    {
        "type",
        "command",
        "api_key_env",
        "json_output",
        "sandbox",
        "sandbox_modes",
        "model_selection",
        "workdir",
        "runtime_source",
        "background",
        "concurrency",
        "max_parallel",
        "silent_mode",
        "limitations",
        "capability_level",
        "version",
    }
)


def get_executor(name: str, config: dict | None = None) -> ExecutorPort:
    try:
        import_path = BUILTIN_EXECUTORS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown executor: {name}") from exc

    module_name, class_name = import_path.rsplit(".", 1)
    executor_class = getattr(importlib.import_module(module_name), class_name)
    raw_config = config or {}
    runtime_keys = BUILTIN_EXECUTOR_RUNTIME_KEYS[name]
    config_only_keys = BUILTIN_EXECUTOR_CONFIG_ONLY_KEYS[name]
    unknown_keys = sorted(
        set(raw_config) - runtime_keys - config_only_keys - EXECUTOR_METADATA_KEYS
    )
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise ValueError(f"Unsupported {name} executor config: {joined}")
    runtime_config = {key: raw_config[key] for key in runtime_keys if key in raw_config}
    executor = executor_class(**runtime_config)
    if not isinstance(executor, ExecutorPort):
        raise TypeError(f"Executor does not implement ExecutorPort: {name}")
    return executor
