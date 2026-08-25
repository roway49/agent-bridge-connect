from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .protocol import ABCError


PERMISSION_EXTENSION_KEY = "agentbc.permission"
CANONICAL_PERMISSION_MODES = ("inherit", "safe", "full")
DEFAULT_PERMISSION_MODE = "inherit"
LEGACY_PERMISSION_MODE = "safe"
PERMISSION_SCHEMA_VERSION = 2
PERMISSION_SNAPSHOT_SCOPE = "task"
PERMISSION_APPROVAL_ON_BLOCK = "on_block"
PERMISSION_APPROVAL_NONE = "none"

_FULL_PERMISSION_FLAGS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "claude": "--dangerously-skip-permissions",
    "hermes": "--yolo",
}

# Every option here is documented by the installed executor CLIs and can either
# change the permission mode directly or change the configuration/rules from
# which permissions are loaded.  Values marked sensitive are redacted from
# authorization errors because raw config/settings and one-shot prompts may
# contain credentials or customer data.
_PERMISSION_OPTIONS: dict[str, dict[str, tuple[str, bool, bool]]] = {
    "codex": {
        "-s": ("sandbox", True, False),
        "--sandbox": ("sandbox", True, False),
        "-a": ("approval_policy", True, False),
        "--ask-for-approval": ("approval_policy", True, False),
        "--dangerously-bypass-approvals-and-sandbox": (
            "bypass_approvals_and_sandbox",
            False,
            False,
        ),
        "--dangerously-bypass-hook-trust": ("bypass_hook_trust", False, False),
        "--ignore-user-config": ("ignore_user_config", False, False),
        "--ignore-rules": ("ignore_rules", False, False),
        "-c": ("raw_config_override", True, True),
        "--config": ("raw_config_override", True, True),
        "-p": ("config_profile", True, False),
        "--profile": ("config_profile", True, False),
    },
    "claude": {
        "--safe-mode": ("safe_mode", False, False),
        "--permission-mode": ("permission_mode", True, False),
        "--dangerously-skip-permissions": (
            "dangerously_skip_permissions",
            False,
            False,
        ),
        "--allow-dangerously-skip-permissions": (
            "allow_dangerously_skip_permissions",
            False,
            False,
        ),
        "--bare": ("bare_config", False, False),
        "--setting-sources": ("setting_sources", True, False),
        "--settings": ("raw_settings", True, True),
    },
    "hermes": {
        "--safe-mode": ("safe_mode", False, False),
        "--yolo": ("yolo", False, False),
        "--accept-hooks": ("accept_hooks", False, False),
        "--ignore-user-config": ("ignore_user_config", False, False),
        "--ignore-rules": ("ignore_rules", False, False),
        "-z": ("oneshot", True, True),
        "--oneshot": ("oneshot", True, True),
    },
}

_EXPECTED_PERMISSION_SEMANTICS: dict[str, dict[str, dict[str, str | bool]]] = {
    "codex": {
        "inherit": {},
        "safe": {"sandbox": "workspace-write"},
        "full": {"bypass_approvals_and_sandbox": True},
    },
    "claude": {
        "inherit": {},
        "safe": {"safe_mode": True, "permission_mode": "acceptEdits"},
        "full": {"dangerously_skip_permissions": True},
    },
    "hermes": {
        "inherit": {},
        "safe": {},
        "full": {"yolo": True},
    },
}

_CONFLICT_GROUPS = {
    "codex": ({"sandbox", "approval_policy", "bypass_approvals_and_sandbox"},),
    "claude": (
        {
            "permission_mode",
            "dangerously_skip_permissions",
            "allow_dangerously_skip_permissions",
        },
    ),
    "hermes": ({"yolo", "oneshot"},),
}


def normalize_permission_mode(value: Any, *, code: str = "invalid_permission_mode") -> str:
    mode = str(value or "").strip().lower()
    if mode not in CANONICAL_PERMISSION_MODES:
        raise ABCError(
            code,
            (
                f"Unknown permission mode: {value!r}. "
                f"Expected one of: {', '.join(CANONICAL_PERMISSION_MODES)}."
            ),
            {"permission_mode": value, "allowed": list(CANONICAL_PERMISSION_MODES)},
        )
    return mode


def configured_permission_mode(config: dict[str, Any] | None) -> tuple[str, str]:
    """Dual-read the unified global permission setting.

    The unified ``permissions.mode`` key wins; the legacy top-level
    ``permission_mode`` key is read as the migration fallback.  First-time
    and existing defaults stay ``inherit``.
    """
    loaded = config if isinstance(config, dict) else {}
    permissions_table = loaded.get("permissions")
    unified = (
        permissions_table.get("mode")
        if isinstance(permissions_table, dict)
        else None
    )
    if unified is not None:
        return normalize_permission_mode(unified), "configured_default"
    if "permission_mode" not in loaded:
        return DEFAULT_PERMISSION_MODE, "inherit_default"
    return normalize_permission_mode(loaded.get("permission_mode")), "configured_default"


def build_permission_record(
    *,
    explicit_mode: str | None = None,
    config: dict[str, Any] | None = None,
    inherited: dict[str, Any] | None = None,
    scope: str = PERMISSION_SNAPSHOT_SCOPE,
) -> dict[str, Any]:
    """Build an ``agentbc.permission`` v2 snapshot.

    Resolution priority is preserved: explicit task override > handoff
    source snapshot > unified config > (legacy safe for historical tasks
    without a record, handled by :func:`permission_record_from_extensions`).
    The v2 record exposes ``configured_mode``, ``inherited_mode``,
    ``task_override``, ``effective_mode``, ``selection_source``, ``mapping``
    and ``scope``; ``permission_args`` only ever records permission
    arguments (empty at snapshot time; the executor attaches permission-only
    args at run time).
    """
    if inherited is not None:
        inherited_record = validate_permission_record(inherited)
        inherited_mode = inherited_record["effective_mode"]
    else:
        inherited_mode = None
    if explicit_mode is not None:
        mode = normalize_permission_mode(explicit_mode)
        source = "explicit_task"
        task_override = mode
    elif inherited_mode is not None:
        mode = inherited_mode
        source = "inherited_task"
        task_override = None
    else:
        mode, source = configured_permission_mode(config)
        task_override = None
    configured_mode, _ = configured_permission_mode(config)
    from .permission_registry import permission_mapping_view

    selection_strategy = "explicit" if task_override in {"safe", "full"} else "inherit"
    resolved_base_mode = mode if mode in {"safe", "full"} else "native"
    resolution_state = "frozen" if resolved_base_mode != "native" else "runtime"
    approval_policy = (
        PERMISSION_APPROVAL_NONE
        if resolved_base_mode == "full"
        else PERMISSION_APPROVAL_ON_BLOCK
    )

    return {
        "version": PERMISSION_SCHEMA_VERSION,
        "configured_mode": configured_mode,
        "inherited_mode": inherited_mode,
        "task_override": task_override,
        "requested_mode": mode,
        "effective_mode": mode,
        "selection_source": source,
        # ``inherit`` is a selection strategy, not a third access level.
        # The legacy requested/effective fields remain for v2 compatibility;
        # runtime approval decisions use the orthogonal fields below.
        "selection_strategy": selection_strategy,
        "resolved_base_mode": resolved_base_mode,
        "resolution_state": resolution_state,
        "approval_policy": approval_policy,
        "scope": str(scope or PERMISSION_SNAPSHOT_SCOPE),
        "mapping": permission_mapping_view(mode),
        "permission_args": [],
    }


def legacy_permission_record() -> dict[str, str]:
    return {
        "requested_mode": LEGACY_PERMISSION_MODE,
        "effective_mode": LEGACY_PERMISSION_MODE,
        "selection_source": "legacy_task",
    }


def validate_permission_record(record: Any) -> dict[str, Any]:
    """Validate an ``agentbc.permission`` v1 (legacy) or v2 record.

    v1 records (no ``version``) round-trip unchanged so historical tasks
    keep running on their persisted snapshot.  v2 records additionally
    expose ``configured_mode``, ``inherited_mode``, ``task_override``,
    ``scope``, ``mapping`` and ``permission_args``; malformed or unknown
    versions fail closed.
    """
    if not isinstance(record, dict):
        raise ABCError(
            "invalid_permission_mode",
            "Task permission extension must be an object.",
        )
    version = record.get("version")
    if version is not None:
        try:
            version_number = int(version)
        except (TypeError, ValueError):
            raise ABCError(
                "unsupported_permission_mode",
                f"Unsupported agentbc.permission version: {version!r}.",
                {"version": version},
            )
        if version_number != PERMISSION_SCHEMA_VERSION:
            raise ABCError(
                "unsupported_permission_mode",
                f"Unsupported agentbc.permission version: {version!r}.",
                {"version": version},
            )
    requested = normalize_permission_mode(record.get("requested_mode") or record.get("effective_mode"))
    effective = normalize_permission_mode(record.get("effective_mode"))
    if requested != effective:
        raise ABCError(
            "unsupported_permission_mode",
            "AgentBC never silently downgrades permission modes; requested and effective modes differ.",
            {"requested_mode": requested, "effective_mode": effective},
        )
    source = str(record.get("selection_source") or "").strip()
    if not source:
        raise ABCError(
            "invalid_permission_mode",
            "Task permission extension is missing selection_source.",
        )
    if version is None:
        return {
            "requested_mode": requested,
            "effective_mode": effective,
            "selection_source": source,
        }
    configured = normalize_permission_mode(record.get("configured_mode") or effective)
    inherited_raw = record.get("inherited_mode")
    inherited_mode = (
        normalize_permission_mode(inherited_raw) if inherited_raw is not None else None
    )
    override_raw = record.get("task_override")
    task_override = (
        normalize_permission_mode(override_raw) if override_raw is not None else None
    )
    scope_raw = record.get("scope")
    if scope_raw is None:
        scope = PERMISSION_SNAPSHOT_SCOPE
    else:
        scope = str(scope_raw).strip()
        if not scope:
            raise ABCError(
                "invalid_permission_mode",
                "v2 permission extension is missing scope.",
            )
    mapping = record.get("mapping")
    if mapping is not None and not isinstance(mapping, dict):
        raise ABCError(
            "invalid_permission_mode",
            "v2 permission extension mapping must be an object or null.",
        )
    args = record.get("permission_args")
    if args is not None and (
        not isinstance(args, list) or any(not isinstance(item, str) for item in args)
    ):
        raise ABCError(
            "invalid_permission_mode",
            "v2 permission extension permission_args must be a list of strings.",
        )
    selection_strategy = str(record.get("selection_strategy") or "").strip().lower()
    if not selection_strategy:
        selection_strategy = (
            "explicit" if task_override in {"safe", "full"} else "inherit"
        )
    if selection_strategy not in {"inherit", "explicit"}:
        raise ABCError(
            "invalid_permission_mode",
            "v2 permission selection_strategy must be inherit or explicit.",
        )
    resolved_base_mode = str(record.get("resolved_base_mode") or "").strip().lower()
    if not resolved_base_mode:
        resolved_base_mode = effective if effective in {"safe", "full"} else "native"
    if resolved_base_mode not in {"native", "safe", "full"}:
        raise ABCError(
            "invalid_permission_mode",
            "v2 resolved_base_mode must be native, safe, or full.",
        )
    resolution_state = str(record.get("resolution_state") or "").strip().lower()
    if not resolution_state:
        resolution_state = "runtime" if resolved_base_mode == "native" else "frozen"
    if resolution_state not in {"runtime", "frozen"}:
        raise ABCError(
            "invalid_permission_mode",
            "v2 resolution_state must be runtime or frozen.",
        )
    approval_policy = str(record.get("approval_policy") or "").strip().lower()
    if not approval_policy:
        approval_policy = (
            PERMISSION_APPROVAL_NONE
            if resolved_base_mode == "full"
            else PERMISSION_APPROVAL_ON_BLOCK
        )
    if approval_policy not in {PERMISSION_APPROVAL_ON_BLOCK, PERMISSION_APPROVAL_NONE}:
        raise ABCError(
            "invalid_permission_mode",
            "v2 approval_policy must be on_block or none.",
        )
    expected_base_mode = effective if effective in {"safe", "full"} else "native"
    expected_resolution_state = (
        "runtime" if expected_base_mode == "native" else "frozen"
    )
    expected_approval_policy = (
        PERMISSION_APPROVAL_NONE
        if expected_base_mode == "full"
        else PERMISSION_APPROVAL_ON_BLOCK
    )
    if (
        resolved_base_mode != expected_base_mode
        or resolution_state != expected_resolution_state
        or approval_policy != expected_approval_policy
    ):
        raise ABCError(
            "invalid_permission_mode",
            "v2 runtime permission policy is inconsistent with its frozen selection snapshot.",
            {
                "effective_mode": effective,
                "resolved_base_mode": resolved_base_mode,
                "resolution_state": resolution_state,
                "approval_policy": approval_policy,
            },
        )
    return {
        "version": PERMISSION_SCHEMA_VERSION,
        "configured_mode": configured,
        "inherited_mode": inherited_mode,
        "task_override": task_override,
        "requested_mode": requested,
        "effective_mode": effective,
        "selection_source": source,
        "selection_strategy": selection_strategy,
        "resolved_base_mode": resolved_base_mode,
        "resolution_state": resolution_state,
        "approval_policy": approval_policy,
        "scope": scope,
        "mapping": mapping,
        "permission_args": list(args) if args else [],
    }


def permission_runtime_policy(record: Any) -> dict[str, str | bool]:
    """Project a task snapshot into the runtime approval decision contract.

    ``inherit`` is deliberately treated as a source strategy.  A trusted
    native permission-blocked event may therefore request approval while the
    inherited base is resolved at runtime.  Only a concrete full base disables
    escalation because no broader AgentBC permission exists.
    """
    permission = validate_permission_record(record)
    effective = str(permission.get("effective_mode") or "inherit")
    base_mode = str(
        permission.get("resolved_base_mode")
        or (effective if effective in {"safe", "full"} else "native")
    )
    policy = str(permission.get("approval_policy") or PERMISSION_APPROVAL_ON_BLOCK)
    return {
        "selection_strategy": str(permission.get("selection_strategy") or "inherit"),
        "base_mode": base_mode,
        "resolution_state": str(permission.get("resolution_state") or "runtime"),
        "approval_policy": policy,
        "approval_on_block": policy == PERMISSION_APPROVAL_ON_BLOCK and base_mode != "full",
    }


def permission_record_from_extensions(
    extensions: dict[str, Any] | None,
    *,
    allow_legacy: bool = True,
) -> dict[str, str]:
    values = extensions if isinstance(extensions, dict) else {}
    record = values.get(PERMISSION_EXTENSION_KEY)
    if record is None and allow_legacy:
        return legacy_permission_record()
    return validate_permission_record(record)


def permission_flags(executor: str, mode: str) -> list[str]:
    selected = normalize_permission_mode(mode)
    if executor == "codex":
        if selected == "safe":
            return ["--sandbox", "workspace-write"]
        if selected == "full":
            return [_FULL_PERMISSION_FLAGS[executor]]
        return []
    if executor == "claude":
        if selected == "safe":
            return ["--safe-mode", "--permission-mode", "acceptEdits"]
        if selected == "full":
            return [_FULL_PERMISSION_FLAGS[executor]]
        return []
    if executor == "hermes":
        # Hermes' documented conservative behavior is its normal approval path:
        # safe deliberately adds no --yolo and does not use troubleshooting
        # --safe-mode, which would also disable user tools, rules, and plugins.
        if selected == "full":
            return [_FULL_PERMISSION_FLAGS[executor]]
        return []
    if selected == "full":
        raise ABCError(
            "unsupported_permission_mode",
            f"Executor {executor!r} has no documented AgentBC full-mode mapping.",
            {"executor": executor, "permission_mode": selected},
        )
    return []


def assert_executor_permission_supported(
    executor: str,
    mode: str,
    executable: str | Path | None,
) -> None:
    selected = normalize_permission_mode(mode)
    permission_flags(executor, selected)
    if selected != "full":
        return
    required = _FULL_PERMISSION_FLAGS.get(executor)
    if required is None or executable is None:
        raise ABCError(
            "unsupported_permission_mode",
            f"Executor {executor!r} cannot represent permission mode full.",
            {"executor": executor, "permission_mode": selected},
        )
    command = [str(Path(executable).expanduser())]
    if executor == "codex":
        command.extend(["exec", "--help"])
    elif executor == "hermes":
        command.extend(["chat", "--help"])
    else:
        command.append("--help")
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ABCError(
            "unsupported_permission_mode",
            f"Could not verify {executor} full-mode capability: {exc}",
            {"executor": executor, "permission_mode": selected},
        ) from exc
    help_text = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or required not in help_text:
        raise ABCError(
            "unsupported_permission_mode",
            (
                f"Installed {executor} cannot represent AgentBC permission mode full; "
                f"documented flag {required} was not found."
            ),
            {
                "executor": executor,
                "permission_mode": selected,
                "required_flag": required,
                "returncode": completed.returncode,
            },
        )


def validate_permission_command(
    executor: str,
    command: list[str],
    record: dict[str, Any],
    *,
    authorized_claude_settings: str | None = None,
    authorized_claude_add_dir: bool = False,
) -> None:
    permission = validate_permission_record(record)
    mode = permission["effective_mode"]
    expected = permission_flags(executor, mode)
    expected_semantics = _EXPECTED_PERMISSION_SEMANTICS.get(executor, {}).get(mode)
    if expected_semantics is None:
        raise ABCError(
            "unsupported_permission_mode",
            f"Executor {executor!r} has no permission authorization schema.",
            {"executor": executor, "permission_mode": mode},
        )
    analysis = _permission_analysis(
        executor,
        command,
        authorized_claude_settings=authorized_claude_settings,
    )
    actual_semantics = analysis["semantics"]
    violations = [
        name
        for name in ("malformed", "duplicates", "conflicts")
        if analysis[name]
    ]
    if violations or actual_semantics != expected_semantics:
        raise ABCError(
            "unsupported_permission_mode",
            (
                f"Generated {executor} permission arguments do not match the persisted "
                f"{mode} authorization."
            ),
            {
                "executor": executor,
                "requested_mode": permission["requested_mode"],
                "effective_mode": mode,
                "selection_source": permission["selection_source"],
                "expected_permission_arguments": expected,
                "expected_permission_semantics": expected_semantics,
                "actual_permission_arguments": analysis["arguments"],
                "actual_permission_semantics": analysis["redacted_semantics"],
                "permission_argument_violations": violations,
                "duplicate_permission_arguments": analysis["duplicates"],
                "conflicting_permission_arguments": analysis["conflicts"],
                "malformed_permission_arguments": analysis["malformed"],
            },
        )
    if (
        not authorized_claude_add_dir
        and mode != "safe"
        and executor in {"codex", "claude"}
        and _command_has_option(command, "--add-dir")
    ):
        raise ABCError(
            "unsupported_permission_mode",
            f"{executor} {mode} mode must not inject --add-dir permission overrides.",
            {"executor": executor, "permission_mode": mode},
        )


def _permission_analysis(
    executor: str,
    command: list[str],
    *,
    authorized_claude_settings: str | None = None,
) -> dict[str, Any]:
    known = _PERMISSION_OPTIONS.get(executor, {})
    semantics: dict[str, str | bool] = {}
    redacted_semantics: dict[str, str | bool] = {}
    arguments: list[str] = []
    duplicates: list[str] = []
    malformed: list[str] = []
    index = 0
    while index < len(command):
        item = command[index]
        if item == "--":
            break
        matched = _match_permission_option(item, known)
        if matched is None:
            index += 1
            continue
        alias, inline_value, used_inline = matched
        semantic, takes_value, sensitive = known[alias]
        value: str | bool = True
        if takes_value:
            if used_inline:
                value = inline_value
            elif index + 1 < len(command):
                index += 1
                value = command[index]
            else:
                value = ""
            if value == "":
                malformed.append(alias)
        elif used_inline:
            malformed.append(alias)
        if (
            executor == "claude"
            and alias == "--settings"
            and authorized_claude_settings is not None
            and value == authorized_claude_settings
        ):
            index += 1
            continue
        display_value = "<redacted>" if sensitive and takes_value else value
        arguments.append(alias if not takes_value else f"{alias}={display_value}")
        if semantic in semantics:
            duplicates.append(semantic)
        else:
            semantics[semantic] = value
            redacted_semantics[semantic] = display_value
        index += 1
    conflicts: list[list[str]] = []
    for group in _CONFLICT_GROUPS.get(executor, ()):
        present = sorted(group & semantics.keys())
        if len(present) > 1:
            conflicts.append(present)
    return {
        "semantics": semantics,
        "redacted_semantics": redacted_semantics,
        "arguments": arguments,
        "duplicates": sorted(set(duplicates)),
        "conflicts": conflicts,
        "malformed": malformed,
    }


def _match_permission_option(
    item: str,
    known: dict[str, tuple[str, bool, bool]],
) -> tuple[str, str, bool] | None:
    if item in known:
        return item, "", False
    name, separator, inline_value = item.partition("=")
    if separator and name in known:
        return name, inline_value, True
    # Short value options also accept their attached form in common CLI
    # parsers (for example -sdanger-full-access). Treat it as permission
    # syntax even when a particular installed version would reject it.
    for alias, (_semantic, takes_value, _sensitive) in known.items():
        if (
            takes_value
            and alias.startswith("-")
            and not alias.startswith("--")
            and item.startswith(alias)
            and len(item) > len(alias)
        ):
            inline_value = item[len(alias) :]
            if inline_value.startswith("="):
                inline_value = inline_value[1:]
            return alias, inline_value, True
    return None


def _command_has_option(command: list[str], option: str) -> bool:
    for item in command:
        if item == "--":
            return False
        if item == option or item.startswith(f"{option}="):
            return True
    return False
