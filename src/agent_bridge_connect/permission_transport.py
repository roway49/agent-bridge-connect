"""Executor permission control-path capability matrix (PERM-104-002).

The worker selects its permission control path from the versioned fixture
matrix, never from agent self-reports, stderr, natural-language summaries or
exit codes.  A ``compatibility-full`` upgrade may only originate from a
supported Adapter's structured permission-block event; this module supplies
the transport half of that contract:

* Claude fixtures declare whether a version drives the native MCP
  permission-prompt tool (``mcp_permission_tool``) or the stdio stream-json
  ``can_use_tool``/``control_response`` control channel
  (``stdio_can_use_tool``);
* unknown version/transport combinations fail closed with the stable code
  ``permission_transport_unsupported``;
* the inner (executor-owned) sandbox contract is frozen here so the outer
  Seatbelt containment and the inner sandbox can never disagree about
  writable roots or Git metadata.

Only structured, sanitized facts are exposed: control-path identifiers,
stable capability booleans and the frozen sandbox key names.  Raw argv,
prompts, tokens and private paths never enter this module's projections.
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

CLAUDE_STDIO_CONTROL_RESPONSE = "control_response"
CLAUDE_INIT_RECEIPT_KIND = "system/init"

# Frozen Claude control-path capability matrix.  Sources:
# tests/fixtures/executor_runtime/matrix/claude/<version>/permission_control.json
# (PROTO-104-001 fixture surfaces).  Adding a version requires a captured
# fixture plus a live canary, exactly like the protocol fixture matrix.
_CLAUDE_CONTROL_MATRIX: dict[str, dict[str, Any]] = {
    "2.1.226": {
        "mcp_permission_tool": {
            "supported": True,
            "flag": "--permission-prompt-tool",
            "same_process_approve_deny": True,
        },
        "stdio_can_use_tool": {
            "supported": True,
            "control_response": CLAUDE_STDIO_CONTROL_RESPONSE,
            "init_receipt": CLAUDE_INIT_RECEIPT_KIND,
            "same_process_approve_deny": True,
            "transport_death_invalidation": True,
        },
    },
    "2.1.233": {
        "mcp_permission_tool": {
            "supported": True,
            "flag": "--permission-prompt-tool",
            "same_process_approve_deny": True,
        },
        "stdio_can_use_tool": {
            "supported": True,
            "control_response": CLAUDE_STDIO_CONTROL_RESPONSE,
            "init_receipt": CLAUDE_INIT_RECEIPT_KIND,
            "same_process_approve_deny": True,
            "transport_death_invalidation": True,
        },
    },
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
    path from help text, stderr or exit codes alone.
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
            f"Claude version {normalized!r} has no frozen permission control path.",
            {"executor": "claude", "version": normalized, "transport": "unknown"},
        )
    return {
        "executor": "claude",
        "version": normalized,
        "control_paths": sorted(entry),
        "mcp_permission_tool": bool(entry["mcp_permission_tool"]["supported"]),
        "stdio_can_use_tool": bool(entry["stdio_can_use_tool"]["supported"]),
        "control_response": CLAUDE_STDIO_CONTROL_RESPONSE,
        "init_receipt": CLAUDE_INIT_RECEIPT_KIND,
        "same_process_approve_deny": bool(
            entry["stdio_can_use_tool"]["same_process_approve_deny"]
        ),
        "transport_death_invalidation": bool(
            entry["stdio_can_use_tool"]["transport_death_invalidation"]
        ),
    }


def select_claude_control_path(
    version: str | None,
    prompt_tool_supported: bool | None,
) -> str:
    """Select the worker control path from the capability matrix.

    The native MCP permission-prompt tool wins when the matrix declares it
    and the probe agrees; otherwise the stdio ``can_use_tool`` control
    channel is selected.  Any combination the matrix cannot prove fails
    closed with ``permission_transport_unsupported``.
    """
    capability = claude_control_path_capability(version)
    if capability["mcp_permission_tool"] and prompt_tool_supported is not False:
        return CONTROL_PATH_MCP_PERMISSION_TOOL
    if capability["stdio_can_use_tool"]:
        return CONTROL_PATH_STDIO_CAN_USE_TOOL
    raise ABCError(
        PERMISSION_TRANSPORT_UNSUPPORTED,
        f"Claude version {capability['version']!r} exposes no supported permission control path.",
        {
            "executor": "claude",
            "version": capability["version"],
            "control_paths": capability["control_paths"],
        },
    )


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
    "CLAUDE_STDIO_CONTROL_RESPONSE",
    "CONTROL_PATH_MCP_PERMISSION_TOOL",
    "CONTROL_PATH_STDIO_CAN_USE_TOOL",
    "KNOWN_CLAUDE_VERSIONS",
    "LINKED_WORKTREE_CAPABILITY_INVALID",
    "PERMISSION_TRANSPORT_UNSUPPORTED",
    "assert_git_metadata_not_in_add_dir",
    "assert_inner_sandbox_within_outer",
    "claude_control_path_capability",
    "claude_inner_sandbox_contract",
    "parse_claude_version",
    "select_claude_control_path",
]
