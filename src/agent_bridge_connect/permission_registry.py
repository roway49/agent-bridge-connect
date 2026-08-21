"""Unified AgentBC permission registry (PERM-103-001 / PERM-103-002).

This narrow module is the single source of truth for:

* the ``agentbc.permission`` v2 record contract (``configured_mode``,
  ``inherited_mode``, ``task_override``, ``effective_mode``,
  ``selection_source``, ``mapping``, ``scope``),
* the unified global permission setting (``permissions.mode``) and its
  legacy top-level ``permission_mode`` dual-read,
* the executor capability mapping (Codex / Claude / Hermes x inherit / safe
  / full) and the capability probes, and
* the frozen Hermes ACP capability (``transport=hermes-acp`` with the
  session/request_permission capability bound by the Task 6 narrow ACP
  transport; only the exact ``allow_once`` / ``deny`` outcomes are exposed).

Setup, CLI, executors and tests route every permission decision through this
module and :mod:`permission_modes`; no other module keeps its own permission
condition branches.

Contract invariants (fail closed):

* resolution priority: task override > handoff source snapshot > unified
  config > legacy safe (historical tasks without a record);
* ``inherit`` never appends permission overrides;
* ``permission_args`` only ever contains permission arguments - never a
  prompt, the full argv, tokens, or raw executor output;
* unknown or inexpressible capabilities raise
  ``permission_capability_unsupported`` instead of approximating ``safe`` to
  ``inherit`` or ``full``;
* ``full`` is auditable: the audit payload records the mode, its source, the
  exact capability mapping, permission-only arguments and subprocess-scoped
  environment, and nothing else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .permission_modes import (
    DEFAULT_PERMISSION_MODE,
    LEGACY_PERMISSION_MODE,
    assert_executor_permission_supported,
    configured_permission_mode,
    normalize_permission_mode,
    permission_flags,
    validate_permission_record,
)
from .protocol import ABCError
from .codex_app_server import (
    CODEX_APP_SERVER_CLIENT_METHODS,
    CODEX_APP_SERVER_NOTIFICATIONS,
    CODEX_APP_SERVER_REQUEST_METHODS,
)

PERMISSION_SCHEMA_VERSION = 2

GLOBAL_PERMISSION_SETTING = "permissions.mode"
LEGACY_PERMISSION_SETTING = "permission_mode"

SUPPORTED_EXECUTORS = ("codex", "claude", "hermes")

TRANSPORT_HERMES_ACP = "hermes-acp"
TRANSPORT_CLI = "cli"
TRANSPORT_CODEX_APP_SERVER = "app-server"

# Canonical Codex transport values accepted by the registry and Runner.
# ``app-server`` is the only value that enables the same-process
# single-action approval chain; everything else (including the legacy
# aliases) is rejected so the frozen contract can be verified.
CODEX_TRANSPORT_VALUES = frozenset({TRANSPORT_CLI, TRANSPORT_CODEX_APP_SERVER})

# Reserved Hermes ACP capability IDs.  Task 6 binds the session and
# request_permission capabilities through the narrow ``hermes_acp`` transport
# (``transport=hermes-acp``); AgentBC freezes the capability surface here and
# exposes only the exact allow_once/deny outcomes.
HERMES_ACP_CHECK_CAPABILITY_ID = "hermes.acp.check"
HERMES_ACP_SESSION_CAPABILITY_ID = "hermes.acp.session"
HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID = "hermes.acp.session.request_permission"
HERMES_ACP_FULL_YOLO_ENV_CAPABILITY_ID = "hermes.acp.full.yolo_env"

# The only native permission outcomes AgentBC may expose on the Hermes ACP
# transport.  ``allow_once`` authorizes exactly one action; ``deny`` is the
# cancelled outcome.  allow_session/allow_always/deny_always are never issued.
HERMES_ACP_ALLOWED_DECISIONS = ("allow_once", "deny")

# Subprocess-scoped Hermes full-mode override.  May only be applied to the
# spawned Hermes ACP subprocess environment, never to the user's global
# environment.
HERMES_YOLO_ENV = {"HERMES_YOLO_MODE": "1"}

# Canonical capability mapping.  ``args`` are permission-only arguments for
# the declared transport (never the full argv); ``env`` is subprocess-scoped;
# ``decisions`` restricts the approval surface (Hermes ACP safe mode);
# ``overrides_native`` is True only for full mode.
_EXECUTOR_CAPABILITY_MAPPING: dict[str, dict[str, dict[str, Any]]] = {
    "codex": {
        "inherit": {
            "transport": TRANSPORT_CODEX_APP_SERVER,
            "capability_id": "codex.inherit",
            "args": [],
            "env": {},
            "decisions": None,
            "overrides_native": False,
        },
        "safe": {
            "transport": TRANSPORT_CODEX_APP_SERVER,
            "capability_id": "codex.sandbox_workspace_write",
            "args": ["--sandbox", "workspace-write"],
            "env": {},
            "decisions": None,
            "overrides_native": False,
        },
        "full": {
            "transport": TRANSPORT_CLI,
            "capability_id": "codex.bypass_approvals_and_sandbox",
            "args": ["--dangerously-bypass-approvals-and-sandbox"],
            "env": {},
            "decisions": None,
            "overrides_native": True,
        },
    },
    "claude": {
        "inherit": {
            "transport": TRANSPORT_CLI,
            "capability_id": "claude.inherit",
            "args": [],
            "env": {},
            "decisions": None,
            "overrides_native": False,
        },
        "safe": {
            "transport": TRANSPORT_CLI,
            "capability_id": "claude.safe_mode_accept_edits",
            "args": ["--safe-mode", "--permission-mode", "acceptEdits"],
            "env": {},
            "decisions": None,
            "overrides_native": False,
        },
        "full": {
            "transport": TRANSPORT_CLI,
            "capability_id": "claude.dangerously_skip_permissions",
            "args": ["--dangerously-skip-permissions"],
            "env": {},
            "decisions": None,
            "overrides_native": True,
        },
    },
    "hermes": {
        "inherit": {
            "transport": TRANSPORT_HERMES_ACP,
            "capability_id": "hermes.acp.inherit",
            "args": [],
            "env": {},
            "decisions": None,
            "overrides_native": False,
        },
        "safe": {
            "transport": TRANSPORT_HERMES_ACP,
            "capability_id": HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
            "args": [],
            "env": {},
            # Hermes safe maps only to restricted, non-interactive approval
            # decisions on the ACP transport.  It never impersonates safe
            # with --safe-mode or --accept-hooks.
            "decisions": ["allow_once", "deny"],
            "overrides_native": False,
        },
        "full": {
            "transport": TRANSPORT_HERMES_ACP,
            "capability_id": HERMES_ACP_FULL_YOLO_ENV_CAPABILITY_ID,
            "args": [],
            "env": dict(HERMES_YOLO_ENV),
            "decisions": None,
            "overrides_native": True,
        },
    },
}

# Direct-transport fallback args (what the frozen CLI transport uses today).
RESOLUTION_PRIORITY = ("task_override", "handoff_snapshot", "configured", "legacy_safe")


def _capability_mode(mode: Any) -> str:
    """Normalize a mode for capability probing or re-raise as unsupported."""
    try:
        return normalize_permission_mode(mode)
    except ABCError as exc:
        raise ABCError(
            "permission_capability_unsupported",
            exc.message,
            {"permission_mode": mode, "allowed": exc.details.get("allowed")},
        ) from exc


def executor_permission_mapping(
    executor: str,
    mode: str,
    *,
    transport: str | None = None,
) -> dict[str, Any]:
    """Return the canonical capability mapping for one executor x mode pair.

    Raises ``permission_capability_unsupported`` for unknown executors,
    unknown modes, or transports that cannot express the pair.  The result
    only contains permission arguments and subprocess-scoped environment;
    it never contains prompts, the full argv, tokens, or raw output.
    """
    selected = _capability_mode(mode)
    if executor not in SUPPORTED_EXECUTORS:
        raise ABCError(
            "permission_capability_unsupported",
            f"Executor {executor!r} has no AgentBC permission mapping.",
            {"executor": executor, "permission_mode": selected},
        )
    entry = dict(_EXECUTOR_CAPABILITY_MAPPING[executor][selected])
    if transport is not None:
        if executor == "hermes":
            if transport not in {TRANSPORT_HERMES_ACP, "direct"}:
                raise ABCError(
                    "permission_capability_unsupported",
                    f"Transport {transport!r} cannot express Hermes permission mode {selected}.",
                    {"executor": executor, "permission_mode": selected, "transport": transport},
                )
            entry["transport"] = transport
        elif executor == "codex":
            if transport in CODEX_TRANSPORT_VALUES:
                entry["transport"] = transport
            elif transport in {"direct"}:
                entry["transport"] = TRANSPORT_CLI
            else:
                raise ABCError(
                    "permission_capability_unsupported",
                    f"Transport {transport!r} cannot express Codex permission mode {selected}.",
                    {"executor": executor, "permission_mode": selected, "transport": transport},
                )
        else:
            if transport not in {TRANSPORT_CLI, "direct"}:
                raise ABCError(
                    "permission_capability_unsupported",
                    f"Transport {transport!r} cannot express {executor} permission mode {selected}.",
                    {"executor": executor, "permission_mode": selected, "transport": transport},
                )
            entry["transport"] = TRANSPORT_CLI
    entry["executor"] = executor
    entry["mode"] = selected
    entry["scope"] = "executor_subprocess"
    # The frozen direct-transport argv (what Runner validates today) is
    # reported separately from the reserved transport args.
    entry["direct_args"] = list(permission_flags(executor, selected))
    return entry


def permission_mapping_view(mode: str) -> dict[str, dict[str, Any]]:
    """Return the executor-agnostic mapping view for one effective mode."""
    return {
        executor: executor_permission_mapping(executor, mode)
        for executor in SUPPORTED_EXECUTORS
    }


def permission_args_for(executor: str, mode: str, *, transport: str | None = None) -> list[str]:
    """Return permission-only arguments for an executor x mode pair.

    The result never includes the prompt, the full argv, tokens, or raw
    output - only the arguments that express the permission mode itself.
    """
    entry = executor_permission_mapping(executor, mode, transport=transport)
    return list(entry["args"])


def probe_hermes_acp(executable: str | Path | None, timeout: int = 10) -> dict[str, Any]:
    """Freeze the Hermes ACP capability probe (PERM-103-002).

    Uses only the official ``hermes acp --check`` and ``hermes acp --version``
    CLI surface.  It never scans Hermes private session databases or logs,
    never reads user configuration, and never modifies the global
    environment.  Output evidence is bounded to the first line and truncated,
    so raw CLI output is never persisted wholesale.
    """
    result: dict[str, Any] = {
        "ok": False,
        "transport": TRANSPORT_HERMES_ACP,
        "capability_id": HERMES_ACP_CHECK_CAPABILITY_ID,
        "reason": "",
        "returncode": None,
        "check_summary": "",
        "version": None,
    }
    if executable is None:
        result["reason"] = "executable_not_found"
        return result
    command = [str(Path(executable).expanduser()), "acp", "--check"]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["reason"] = f"acp check unavailable: {exc}"
        return result
    output = (completed.stdout or completed.stderr or "").strip()
    summary = _first_line(output, limit=120)
    result["returncode"] = completed.returncode
    result["check_summary"] = summary
    if completed.returncode != 0 or not output:
        result["reason"] = "acp check failed"
        return result
    version_command = [str(Path(executable).expanduser()), "acp", "--version"]
    try:
        versioned = subprocess.run(
            version_command,
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        versioned = None
    if versioned is not None and versioned.returncode == 0:
        version = (versioned.stdout or versioned.stderr or "").strip()
        result["version"] = version or None
    result["ok"] = True
    return result


def probe_executor_capability(
    executor: str,
    mode: str,
    executable: str | Path | None,
    *,
    transport: str | None = None,
) -> dict[str, Any]:
    """Probe whether one executor can exactly express one permission mode.

    Returns a capability report on success.  Unknown executors/modes and
    inexpressible capabilities raise ``permission_capability_unsupported``;
    ``safe`` is never approximated to ``inherit`` or ``full``.
    """
    entry = executor_permission_mapping(executor, mode, transport=transport)
    selected = entry["mode"]
    if selected == "inherit" and not (
        executor == "codex" and entry["transport"] == TRANSPORT_CODEX_APP_SERVER
    ):
        # Non-Codex inherit transports add no AgentBC override and require no
        # permission capability probe.
        return {
            "executor": executor,
            "mode": selected,
            "transport": entry["transport"],
            "supported": True,
            "capability_id": entry["capability_id"],
            "evidence": ["no_overrides"],
            "details": {},
        }
    if executor == "hermes" and entry["transport"] == TRANSPORT_HERMES_ACP:
        probe = probe_hermes_acp(executable)
        if not probe["ok"]:
            raise ABCError(
                "permission_capability_unsupported",
                (
                    f"Hermes ACP unavailable; cannot express permission mode "
                    f"{selected}: {probe['reason']}."
                ),
                {
                    "executor": executor,
                    "permission_mode": selected,
                    "transport": TRANSPORT_HERMES_ACP,
                    "capability_id": entry["capability_id"],
                    "reason": probe["reason"],
                    "returncode": probe["returncode"],
                },
            )
        return {
            "executor": executor,
            "mode": selected,
            "transport": TRANSPORT_HERMES_ACP,
            "supported": True,
            "capability_id": entry["capability_id"],
            "evidence": ["acp_check_ok"],
            "details": {
                "check_summary": probe["check_summary"],
                "version": probe["version"],
                "session": {
                    "transport": TRANSPORT_HERMES_ACP,
                    "capability_id": HERMES_ACP_SESSION_CAPABILITY_ID,
                },
                "session_request_permission": {
                    # Bound by the Task 6 ACP transport at session init: the
                    # exact session-level capability bridges into the frozen
                    # approval receipt and ControlPlane with only the
                    # allow_once/deny outcome surface.
                    "state": "bound",
                    "capability_id": HERMES_ACP_REQUEST_PERMISSION_CAPABILITY_ID,
                    "decisions": list(HERMES_ACP_ALLOWED_DECISIONS),
                },
            },
        }
    if executor == "codex" and entry["transport"] == TRANSPORT_CODEX_APP_SERVER:
        from .codex_app_server import (
            CODEX_APP_SERVER_TRANSPORT,
            assert_codex_app_server_capability,
        )

        try:
            probe = assert_codex_app_server_capability(
                executable, transport=CODEX_APP_SERVER_TRANSPORT
            )
        except ABCError as exc:
            raise ABCError(
                "permission_capability_unsupported",
                exc.message,
                {
                    **dict(exc.details or {}),
                    "executor": executor,
                    "permission_mode": selected,
                    "transport": TRANSPORT_CODEX_APP_SERVER,
                    "capability_id": entry["capability_id"],
                },
            ) from exc
        return {
            "executor": executor,
            "mode": selected,
            "transport": TRANSPORT_CODEX_APP_SERVER,
            "supported": True,
            "capability_id": entry["capability_id"],
            "evidence": list(probe["evidence"]),
            "details": {
                "version": probe["version"],
                "version_parsed": probe["version_parsed"],
                "protocol_version": probe["protocol_version"],
                "schema_summary": probe["schema_summary"],
                "client_methods": sorted(CODEX_APP_SERVER_CLIENT_METHODS),
                "request_methods": sorted(CODEX_APP_SERVER_REQUEST_METHODS),
                "notifications": sorted(CODEX_APP_SERVER_NOTIFICATIONS),
                "decisions": ["accept", "decline"],
                "scope": "single_action",
            },
        }
    try:
        assert_executor_permission_supported(executor, selected, executable)
    except ABCError as exc:
        raise ABCError(
            "permission_capability_unsupported",
            f"{executor} cannot express permission mode {selected}: {exc.message}",
            {
                **dict(exc.details or {}),
                "executor": executor,
                "permission_mode": selected,
                "transport": entry["transport"],
            },
        ) from exc
    return {
        "executor": executor,
        "mode": selected,
        "transport": entry["transport"],
        "supported": True,
        "capability_id": entry["capability_id"],
        "evidence": ["cli_help_verified"],
        "details": {},
    }


def build_permission_audit_payload(
    record: dict[str, Any],
    *,
    executor: str,
    transport: str | None = None,
) -> dict[str, Any]:
    """Build the auditable full-mode payload for one executor run.

    The payload records only the mode, its source, the capability mapping,
    permission-only arguments and the subprocess-scoped environment.  It
    never contains the prompt, the full argv, tokens, or raw executor output.
    """
    validated = validate_permission_record(record)
    entry = executor_permission_mapping(executor, validated["effective_mode"], transport=transport)
    return {
        "version": PERMISSION_SCHEMA_VERSION,
        "executor": executor,
        "transport": entry["transport"],
        "mode": validated["effective_mode"],
        "selection_source": validated["selection_source"],
        "capability_id": entry["capability_id"],
        "scope": entry["scope"],
        "permission_args": list(entry["args"]),
        "env": dict(entry["env"]),
        "decisions": list(entry["decisions"]) if entry["decisions"] else None,
    }


def permissions_setting_path(config: dict[str, Any] | None) -> str | None:
    """Return which config key currently holds the global permission setting."""
    loaded = config if isinstance(config, dict) else {}
    permissions = loaded.get("permissions")
    if isinstance(permissions, dict) and "mode" in permissions:
        return GLOBAL_PERMISSION_SETTING
    if LEGACY_PERMISSION_SETTING in loaded:
        return LEGACY_PERMISSION_SETTING
    return None


def permissions_status_payload(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the machine-readable view for ``agentbc permissions status``."""
    loaded = config if isinstance(config, dict) else {}
    mode, source = configured_permission_mode(loaded)
    setting_path = permissions_setting_path(loaded)
    return {
        "ok": True,
        "command": "permissions status",
        "setting": GLOBAL_PERMISSION_SETTING,
        "setting_path": setting_path,
        "configured_mode": mode,
        "configured_source": source,
        "default_mode": DEFAULT_PERMISSION_MODE,
        "legacy_safe_default": LEGACY_PERMISSION_MODE,
        "effective_mode_for_new_tasks": mode,
        "priority": list(RESOLUTION_PRIORITY),
        "scope": "future_tasks",
        "mapping": permission_mapping_view(mode),
        "legacy_setting_present": setting_path == LEGACY_PERMISSION_SETTING,
        "notes": [
            "Affects newly dispatched root tasks and handoff iterations only; "
            "active, input_required, needs_recovery tasks and same-task resume "
            "keep their frozen permission snapshot."
        ],
    }


def _first_line(text: str, *, limit: int) -> str:
    line = text.splitlines()[0] if text else ""
    if len(line) > limit:
        return f"{line[:limit]}..."
    return line
