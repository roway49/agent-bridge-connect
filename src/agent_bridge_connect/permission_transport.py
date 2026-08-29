"""Executor permission control-path capability matrix (PERM-104-002).

The worker selects its permission control path from the versioned fixture
matrix, never from agent self-reports, stderr, natural-language summaries or
exit codes.  A ``compatibility-full`` upgrade may only originate from a
supported Adapter's structured permission-block event; this module supplies
the transport half of that contract:

* a Claude fixture declares a control path only when BOTH gates hold:
  (a) the official ``--help`` of a real binary at that exact version lists
  ``--permission-prompt-tool`` (for the MCP path), and (b) a live
  ``can_use_tool``/``control_response`` exchange was captured against that
  binary (for the stdio path).  A characterized expectation is never
  evidence;
* unknown versions, unproven surfaces and unprobed binaries fail closed
  with the stable code ``permission_transport_unsupported``;
* the inner (executor-owned) sandbox contract is frozen here so the outer
  Seatbelt containment and the inner sandbox can never disagree about
  writable roots or Git metadata.

Only structured, sanitized facts are exposed: control-path identifiers,
stable capability booleans and the frozen sandbox key names.  Raw argv,
prompts, tokens and private paths never enter this module's projections.

E52M-003 review fix: the previous matrix declared both 2.1.226 and 2.1.233
as supporting ``mcp_permission_tool`` and ``stdio_can_use_tool`` although
the recorded fixtures were declared expectations (``captured_live: false``)
and the live probe of the installed binary (2.1.247) shows its official
``--help`` does NOT contain ``--permission-prompt-tool``.  The matrix now
carries no transport capability for any version: every combination fails
closed until a fixture captured from a real binary plus a live canary
proves otherwise.  Production therefore never selects a control path and
never launches a fabricated broker command under an official flag.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .protocol import ABCError

PERMISSION_TRANSPORT_UNSUPPORTED = "permission_transport_unsupported"
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
SDK_CLAUDE_DEPENDENCY_MISMATCH = "claude_sdk_dependency_mismatch"
SDK_CLAUDE_PLATFORM_UNSUPPORTED = "claude_sdk_platform_unsupported"
SDK_CLAUDE_CLI_PATH_UNVERIFIED = "claude_sdk_cli_path_unverified"

CLAUDE_STDIO_CONTROL_RESPONSE = "control_response"
CLAUDE_INIT_RECEIPT_KIND = "system/init"

# The official flag a real Claude binary must list in its own ``--help``
# before the MCP permission-prompt path may be declared supported.
CLAUDE_PERMISSION_PROMPT_TOOL_FLAG = "--permission-prompt-tool"

# Frozen Claude control-path capability matrix.
#
# E52M-003 review fix: the 2.1.226/2.1.233 entries previously claimed both
# control paths from declared (never live-captured) fixtures and were
# withdrawn.  The only re-admitted path is the OFFICIAL Claude Agent SDK
# ``can_use_tool`` transport, admitted solely from the isolated live probe of
# 2026-08-29 (PERM-104-002): claude-agent-sdk 0.2.142 bound to
# ``cli_path=/Users/wangroway/.local/share/claude/versions/2.1.233`` proved
#   * a stable non-empty ``context.tool_use_id`` per request;
#   * ``PermissionResultAllow(updated_input=original_input)`` executed the
#     exact original action in the same client/session;
#   * ``PermissionResultDeny`` produced zero execution;
#   * the same client/session kept working after both decisions.
# Evidence: tests/fixtures/executor_runtime/matrix/claude/live_probe_sdk_2026-08-29
# (probe script + redacted JSON evidence).  Adding any further entry requires
# the same class of live probe at the exact version tuple.  Production
# doctor/matrix support only this probed tuple; no PATH fallback and no
# unprobed binary is ever trusted.
_CLAUDE_CONTROL_MATRIX: dict[str, dict[str, Any]] = {
    "2.1.233": {
        "mcp_permission_tool": {"supported": False},
        "stdio_can_use_tool": {
            "supported": True,
            "same_process_approve_deny": True,
            "transport_death_invalidation": True,
        },
        "sdk_control_transport": {
            "supported": True,
            "sdk_package": "claude-agent-sdk",
            "sdk_version": "0.2.142",
            "platform": "macOS arm64",
            "cli_path_must_match_configured": True,
        },
    },
}

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

KNOWN_CLAUDE_VERSIONS = frozenset(_CLAUDE_CONTROL_MATRIX)

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
    """Return the frozen control-path capability for one Claude version.

    Unknown or unparseable versions fail closed with
    ``permission_transport_unsupported``; AgentBC never guesses a control
    path from help text, stderr or exit codes alone.  With the E52M-003
    matrix this fails closed for every version until a live-captured
    fixture proves a control path.
    """
    normalized = str(version or "").strip()
    if not normalized:
        raise ABCError(
            PERMISSION_TRANSPORT_UNSUPPORTED,
            "Claude control path requires an official version receipt.",
            {"executor": "claude", "transport": "unknown"},
        )
    entry = _CLAUDE_CONTROL_MATRIX.get(normalized)
    if entry is None:
        raise ABCError(
            PERMISSION_TRANSPORT_UNSUPPORTED,
            f"Claude version {normalized!r} has no proven permission control path.",
            {
                "executor": "claude",
                "version": normalized,
                "transport": "unknown",
                "reason": "no live-captured fixture proves a control path",
            },
        )
    sdk_entry = entry.get("sdk_control_transport") or {}
    return {
        "executor": "claude",
        "version": normalized,
        "control_paths": sorted(entry),
        "mcp_permission_tool": bool(entry["mcp_permission_tool"]["supported"]),
        "stdio_can_use_tool": bool(entry["stdio_can_use_tool"]["supported"]),
        "sdk_control_transport": bool(sdk_entry.get("supported")),
        "sdk_package": str(sdk_entry.get("sdk_package") or ""),
        "sdk_version": str(sdk_entry.get("sdk_version") or ""),
        "sdk_platform": str(sdk_entry.get("platform") or ""),
        "control_response": CLAUDE_STDIO_CONTROL_RESPONSE,
        "init_receipt": CLAUDE_INIT_RECEIPT_KIND,
        "same_process_approve_deny": bool(
            entry["stdio_can_use_tool"]["same_process_approve_deny"]
        ),
        "transport_death_invalidation": bool(
            entry["stdio_can_use_tool"]["transport_death_invalidation"]
        ),
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
    must not re-implement) is never selected directly.  Every unsupported
    combination fails closed with ``permission_transport_unsupported``.
    """
    capability = claude_control_path_capability(version)
    if capability["sdk_control_transport"]:
        return CONTROL_PATH_SDK_TRANSPORT
    raise ABCError(
        PERMISSION_TRANSPORT_UNSUPPORTED,
        f"Claude version {capability['version']!r} exposes no supported permission control path.",
        {
            "executor": "claude",
            "version": capability["version"],
            "control_paths": capability["control_paths"],
            "probe_supports_prompt_tool": prompt_tool_supported,
        },
    )


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


def assert_claude_sdk_environment(cli_path: str | Path | None) -> dict[str, str]:
    """Fail closed unless the exact probed SDK tuple is installed and bound.

    Gates, in order (each with a stable code and redacted diagnostics):
      1. ``claude-agent-sdk`` importable at all
         (``claude_sdk_dependency_missing``);
      2. its ``__version__`` equals the probed pin
         (``claude_sdk_dependency_mismatch``);
      3. the host platform is the probed one
         (``claude_sdk_platform_unsupported``);
      4. an explicit absolute ``cli_path`` is supplied by configuration —
         never a PATH discovery (``claude_sdk_cli_path_unverified``).

    Returns the redacted facts {sdk_version, platform, cli_path} on success.
    """
    try:
        import claude_agent_sdk as _sdk  # noqa: F401
    except Exception as exc:  # pragma: no cover - import failure path
        raise ABCError(
            SDK_CLAUDE_DEPENDENCY_MISSING,
            "The official Claude Agent SDK is not installed; the SDK "
            "permission transport is unsupported.",
            {
                "executor": "claude",
                "transport": CONTROL_PATH_SDK_TRANSPORT,
                "reason": "import_failed",
                "error_type": type(exc).__name__,
            },
        ) from exc
    installed = str(getattr(_sdk, "__version__", "") or "").strip()
    if installed != CLAUDE_SDK_PINNED_VERSION:
        raise ABCError(
            SDK_CLAUDE_DEPENDENCY_MISMATCH,
            "The installed Claude Agent SDK does not match the probed pin.",
            {
                "executor": "claude",
                "transport": CONTROL_PATH_SDK_TRANSPORT,
                "expected": CLAUDE_SDK_PINNED_VERSION,
            },
        )
    host = current_platform()
    if host != CLAUDE_SDK_PLATFORM:
        raise ABCError(
            SDK_CLAUDE_PLATFORM_UNSUPPORTED,
            "The Claude SDK permission transport is probed on macOS arm64 only.",
            {"executor": "claude", "transport": CONTROL_PATH_SDK_TRANSPORT, "host": host},
        )
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
        "sdk_version": installed,
        "platform": host,
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
    "assert_git_metadata_not_in_add_dir",
    "assert_inner_sandbox_within_outer",
    "claude_control_path_capability",
    "claude_inner_sandbox_contract",
    "current_platform",
    "parse_claude_version",
    "probe_claude_permission_prompt_tool",
    "select_claude_control_path",
]
