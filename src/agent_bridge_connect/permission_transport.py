"""Executor-native permission protocol capability checks (PERM-104-002).

The worker selects its permission control path from the executor-native
protocol surface, never from agent self-reports, stderr, natural-language
summaries, version tables or exit codes.  A ``compatibility-full`` upgrade may only originate from a
supported Adapter's structured permission-block event; this module supplies
the transport half of that contract:

* Claude CLI and SDK versions are diagnostics, never admission keys;
* a compatible official SDK protocol surface admits the native
  ``can_use_tool`` transport for official releases and compatible forks;
* missing or incompatible protocol members fail at the mechanical protocol
  handshake, never because a version is absent from a support list;
* the inner (executor-owned) sandbox contract is frozen here so the outer
  Seatbelt containment and the inner sandbox can never disagree about
  writable roots or Git metadata.

Only structured, sanitized facts are exposed: control-path identifiers,
stable capability booleans and the frozen sandbox key names.  Raw argv,
prompts, tokens and private paths never enter this module's projections.

The legacy MCP prompt-tool path remains disabled.  This module does not infer
permission categories or trust text output: only the SDK's structured
``can_use_tool`` callback and exact native result types remain authoritative.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

from .protocol import ABCError

PERMISSION_TRANSPORT_UNSUPPORTED = "permission_transport_unsupported"
PERMISSION_PROTOCOL_UNAVAILABLE = "permission_protocol_unavailable"
PERMISSION_PROTOCOL_SHAPE_UNSUPPORTED = "permission_protocol_shape_unsupported"
PERMISSION_PROTOCOL_HANDSHAKE_FAILED = "permission_protocol_handshake_failed"
PERMISSION_TRANSPORT_LOST = "permission_transport_lost"
LINKED_WORKTREE_CAPABILITY_INVALID = "linked_worktree_capability_invalid"

CONTROL_PATH_MCP_PERMISSION_TOOL = "mcp_permission_tool"
CONTROL_PATH_STDIO_CAN_USE_TOOL = "stdio_can_use_tool"
CONTROL_PATH_SDK_TRANSPORT = "sdk_control_transport"

# The exact SDK tuple proven by the isolated live probe.  Anything else
# (missing package, other SDK version, other platform, PATH-discovered CLI)
# fails closed with a stable code and redacted diagnostics.
CLAUDE_SDK_PACKAGE = "claude-agent-sdk"
CLAUDE_SDK_PINNED_VERSION = "0.2.142"
CLAUDE_SDK_PLATFORM = "macOS arm64"

SDK_CLAUDE_DEPENDENCY_MISSING = "claude_sdk_dependency_missing"
SDK_CLAUDE_DEPENDENCY_MISMATCH = PERMISSION_PROTOCOL_SHAPE_UNSUPPORTED
SDK_CLAUDE_PLATFORM_UNSUPPORTED = "claude_sdk_platform_unsupported"
SDK_CLAUDE_CLI_PATH_UNVERIFIED = "claude_sdk_cli_path_unverified"

CLAUDE_STDIO_CONTROL_RESPONSE = "control_response"
CLAUDE_INIT_RECEIPT_KIND = "system/init"

# The official flag a real Claude binary must list in its own ``--help``
# before the MCP permission-prompt path may be declared supported.
CLAUDE_PERMISSION_PROMPT_TOOL_FLAG = "--permission-prompt-tool"

# Version tables are intentionally empty: versions are recorded for diagnostics
# only.  Admission is determined by ``claude_sdk_protocol_capability``.
KNOWN_CLAUDE_VERSIONS = frozenset()

# Fixture gates every declared surface must satisfy before production
# trusts it.  Kept as data so tests and the capture tool share one truth.
_CLAUDE_FIXTURE_GATES: dict[str, Any] = {
    "captured_live_required": True,
    "help_flag_required": CLAUDE_PERMISSION_PROMPT_TOOL_FLAG,
    "stdio_exchange_required": (CLAUDE_STDIO_CONTROL_RESPONSE, CLAUDE_INIT_RECEIPT_KIND),
}

# Frozen inner-sandbox contract.  AgentBC never rewrites these keys and never
# widens them; the keys stay exactly as Claude documents them.
_CLAUDE_INNER_SANDBOX_CONTRACT: dict[str, Any] = {
    "sandbox_keys": ("sandbox.enabled", "sandbox.failIfUnavailable"),
    "edit_deny": True,
    "git_metadata_in_add_dir": False,
    "allow_write_within_outer_roots": True,
}

_CLAUDE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_claude_version(value: str | None) -> str | None:
    """Extract the canonical ``x.y.z`` version from a version probe line.

    Official probes report lines such as ``2.1.226 (Claude Code)``; the
    matrix is keyed by the parsed triple only.
    """
    text = str(value or "").strip()
    match = _CLAUDE_VERSION_RE.search(text)
    if match is None:
        return None
    return ".".join(match.groups())


def claude_control_path_capability(version: str | None) -> dict[str, Any]:
    """Return version-independent native protocol capability metadata."""
    normalized = str(version or "").strip()
    return {
        "executor": "claude",
        "version": normalized,
        "version_is_diagnostic": True,
        "control_paths": [CONTROL_PATH_SDK_TRANSPORT],
        "mcp_permission_tool": False,
        "stdio_can_use_tool": True,
        "sdk_control_transport": True,
        "sdk_package": CLAUDE_SDK_PACKAGE,
        "sdk_version": "runtime",
        "sdk_platform": "runtime",
        "control_response": CLAUDE_STDIO_CONTROL_RESPONSE,
        "init_receipt": CLAUDE_INIT_RECEIPT_KIND,
        "same_process_approve_deny": True,
        "transport_death_invalidation": True,
    }


def probe_claude_permission_prompt_tool(help_text: str | None) -> bool:
    """Return whether an official ``--help`` capture lists the prompt-tool flag.

    This is the single live-probe gate for the MCP path.  It inspects only
    captured help text; it never runs the binary itself so the matrix stays
    a pure function over evidence.
    """
    return CLAUDE_PERMISSION_PROMPT_TOOL_FLAG in str(help_text or "")


def select_claude_control_path(
    version: str | None,
    prompt_tool_supported: bool | None,
) -> str:
    """Select the worker control path from the capability matrix.

    Only the official Claude Agent SDK transport is ever selected.  The MCP
    permission-prompt path stays unproven for every version, and the raw
    stdio ``control_response`` wire (an SDK implementation detail AgentBC
    must not re-implement) is never selected directly.  The selected SDK path
    is admitted by the protocol handshake performed before worker startup.
    """
    claude_control_path_capability(version)
    return CONTROL_PATH_SDK_TRANSPORT


def current_platform() -> str:
    """Return the redacted AgentBC platform label for SDK gate checks.

    ``platform.system()`` reports ``Darwin`` on macOS while every AgentBC
    matrix fixture and the doctor projection use the human label
    ``macOS``; the mapping stays explicit so the gate can never silently
    admit a non-macOS host.
    """
    import platform

    system = platform.system()
    machine = platform.machine()
    label = {"Darwin": "macOS"}.get(system, system)
    return f"{label} {machine}"


def claude_sdk_protocol_capability() -> dict[str, Any]:
    """Mechanically verify the SDK permission protocol surface.

    This deliberately ignores package and CLI version numbers.  Compatible
    upgrades and forks are admitted when they expose the same protocol
    members; incompatible shapes fail before a worker starts.
    """
    try:
        import claude_agent_sdk as _sdk
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
            PermissionUpdate,
        )
        from claude_agent_sdk.types import ToolPermissionContext
    except Exception as exc:  # pragma: no cover - import failure path
        raise ABCError(
            SDK_CLAUDE_DEPENDENCY_MISSING,
            "The Claude SDK permission protocol is unavailable.",
            {
                "executor": "claude",
                "transport": CONTROL_PATH_SDK_TRANSPORT,
                "reason": "import_failed",
                "error_type": type(exc).__name__,
            },
        ) from exc

    required_options = {
        "cli_path",
        "cwd",
        "permission_mode",
        "can_use_tool",
        "hooks",
    }
    try:
        option_params = set(inspect.signature(ClaudeAgentOptions).parameters)
        client_params = set(inspect.signature(ClaudeSDKClient).parameters)
        allow_params = set(inspect.signature(PermissionResultAllow).parameters)
        deny_params = set(inspect.signature(PermissionResultDeny).parameters)
        update_params = set(inspect.signature(PermissionUpdate).parameters)
        context_fields = set(getattr(ToolPermissionContext, "__annotations__", {}))
    except Exception as exc:
        raise ABCError(
            PERMISSION_PROTOCOL_HANDSHAKE_FAILED,
            "The Claude SDK protocol surface could not be inspected.",
            {
                "executor": "claude",
                "transport": CONTROL_PATH_SDK_TRANSPORT,
                "error_type": type(exc).__name__,
            },
        ) from exc

    missing: list[str] = []
    missing.extend(f"ClaudeAgentOptions.{name}" for name in sorted(required_options - option_params))
    if "options" not in client_params:
        missing.append("ClaudeSDKClient.options")
    for name in sorted({"updated_input", "updated_permissions"} - allow_params):
        missing.append(f"PermissionResultAllow.{name}")
    for name in sorted({"message", "interrupt"} - deny_params):
        missing.append(f"PermissionResultDeny.{name}")
    for name in sorted({"type", "destination"} - update_params):
        missing.append(f"PermissionUpdate.{name}")
    for name in sorted({"tool_use_id", "suggestions"} - context_fields):
        missing.append(f"ToolPermissionContext.{name}")
    if missing:
        raise ABCError(
            PERMISSION_PROTOCOL_SHAPE_UNSUPPORTED,
            "The Claude SDK permission protocol shape is incompatible.",
            {
                "executor": "claude",
                "transport": CONTROL_PATH_SDK_TRANSPORT,
                "missing_members": missing,
            },
        )

    return {
        "available": True,
        "protocol": "sdk.can_use_tool",
        "sdk_version": str(getattr(_sdk, "__version__", "") or "unknown"),
        "required_members": sorted(required_options),
    }


def assert_claude_sdk_environment(cli_path: str | Path | None) -> dict[str, str]:
    """Require a compatible SDK protocol and an explicit executable path."""
    protocol = claude_sdk_protocol_capability()
    resolved = str(cli_path or "").strip()
    if (
        not resolved
        or not resolved.startswith("/")
        or not Path(resolved).is_absolute()
        or not Path(resolved).is_file()
    ):
        raise ABCError(
            SDK_CLAUDE_CLI_PATH_UNVERIFIED,
            "The SDK transport requires the configured absolute Claude CLI path; "
            "PATH discovery is never trusted.",
            {"executor": "claude", "transport": CONTROL_PATH_SDK_TRANSPORT},
        )
    return {
        "sdk_version": str(protocol["sdk_version"]),
        "protocol": str(protocol["protocol"]),
        "platform": current_platform(),
        "cli_path": resolved,
    }


def claude_inner_sandbox_contract() -> dict[str, Any]:
    """Return the frozen inner-sandbox invariants (defensive copy)."""
    return {
        "sandbox_keys": tuple(_CLAUDE_INNER_SANDBOX_CONTRACT["sandbox_keys"]),
        "edit_deny": bool(_CLAUDE_INNER_SANDBOX_CONTRACT["edit_deny"]),
        "git_metadata_in_add_dir": bool(
            _CLAUDE_INNER_SANDBOX_CONTRACT["git_metadata_in_add_dir"]
        ),
        "allow_write_within_outer_roots": bool(
            _CLAUDE_INNER_SANDBOX_CONTRACT["allow_write_within_outer_roots"]
        ),
    }


def assert_git_metadata_not_in_add_dir(
    additional_dirs: list[str] | None,
    git_metadata_dirs: list[str] | None,
) -> None:
    """Fail closed when inner-sandbox ``--add-dir`` values include Git metadata.

    Git metadata (per-worktree git dir, common objects/refs/reflogs) belongs
    exclusively to the Runner-owned outer Seatbelt containment; it must never
    be handed to an executor's inner sandbox.
    """
    additions = [str(path) for path in (additional_dirs or [])]
    metadata = [str(path) for path in (git_metadata_dirs or [])]
    for addition in additions:
        for meta in metadata:
            if addition == meta or addition.startswith(meta.rstrip("/") + "/"):
                raise ABCError(
                    LINKED_WORKTREE_CAPABILITY_INVALID,
                    "Inner sandbox --add-dir must not include Git metadata directories.",
                    {"additional_dir": addition, "git_metadata_dir": meta},
                )


def assert_inner_sandbox_within_outer(
    inner_allow_write: list[str] | None,
    outer_writable_roots: list[str] | None,
) -> None:
    """Fail closed when an inner sandbox writes outside the outer containment."""
    outer = [str(Path(path).expanduser().resolve()) for path in (outer_writable_roots or [])]
    for raw in inner_allow_write or []:
        inner = str(Path(raw).expanduser().resolve())
        if not any(
            inner == candidate or inner.startswith(candidate.rstrip("/") + "/")
            for candidate in outer
        ):
            raise ABCError(
                LINKED_WORKTREE_CAPABILITY_INVALID,
                "Inner sandbox allowWrite must stay within the outer task roots.",
                {"inner_allow_write": inner},
            )


__all__ = [
    "CLAUDE_INIT_RECEIPT_KIND",
    "CLAUDE_PERMISSION_PROMPT_TOOL_FLAG",
    "CLAUDE_SDK_PACKAGE",
    "CLAUDE_SDK_PINNED_VERSION",
    "CLAUDE_SDK_PLATFORM",
    "CLAUDE_STDIO_CONTROL_RESPONSE",
    "CONTROL_PATH_MCP_PERMISSION_TOOL",
    "CONTROL_PATH_SDK_TRANSPORT",
    "CONTROL_PATH_STDIO_CAN_USE_TOOL",
    "KNOWN_CLAUDE_VERSIONS",
    "LINKED_WORKTREE_CAPABILITY_INVALID",
    "PERMISSION_TRANSPORT_UNSUPPORTED",
    "SDK_CLAUDE_CLI_PATH_UNVERIFIED",
    "SDK_CLAUDE_DEPENDENCY_MISMATCH",
    "SDK_CLAUDE_DEPENDENCY_MISSING",
    "SDK_CLAUDE_PLATFORM_UNSUPPORTED",
    "assert_claude_sdk_environment",
    "claude_sdk_protocol_capability",
    "assert_git_metadata_not_in_add_dir",
    "assert_inner_sandbox_within_outer",
    "claude_control_path_capability",
    "claude_inner_sandbox_contract",
    "current_platform",
    "parse_claude_version",
    "probe_claude_permission_prompt_tool",
    "select_claude_control_path",
    "PERMISSION_PROTOCOL_UNAVAILABLE",
    "PERMISSION_PROTOCOL_SHAPE_UNSUPPORTED",
    "PERMISSION_PROTOCOL_HANDSHAKE_FAILED",
    "PERMISSION_TRANSPORT_LOST",
]
