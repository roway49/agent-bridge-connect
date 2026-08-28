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

CLAUDE_STDIO_CONTROL_RESPONSE = "control_response"
CLAUDE_INIT_RECEIPT_KIND = "system/init"

# The official flag a real Claude binary must list in its own ``--help``
# before the MCP permission-prompt path may be declared supported.
CLAUDE_PERMISSION_PROMPT_TOOL_FLAG = "--permission-prompt-tool"

# Frozen Claude control-path capability matrix.
#
# E52M-003 review fix: every entry was removed.  The 2.1.226/2.1.233
# entries previously claimed both control paths from declared (never
# live-captured) fixtures.  Live probe evidence on the production host:
#   * installed Claude Code is 2.1.247 (no matrix entry);
#   * its official ``claude --help`` does not contain
#     ``--permission-prompt-tool``;
#   * no ``can_use_tool``/``control_response`` exchange has been captured
#     live for any version.
# Declaring capability without that evidence let the worker emit a
# self-authored broker shell command under an official flag - a protocol
# AgentBC invented, not the official one.  Adding an entry back requires:
#   1. a fixture captured from a real binary at exactly that version
#      (``captured_live: true``) whose help lists the flag, and
#   2. a live canary proving the ``can_use_tool``/``control_response``
#      exchange end to end.
_CLAUDE_CONTROL_MATRIX: dict[str, dict[str, Any]] = {}

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

    With the E52M-003 matrix every combination fails closed with
    ``permission_transport_unsupported``: no version has a proven control
    path, so the worker must never enter ``start_control`` and never emit
    a self-authored broker command under an official flag.
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
            "probe_supports_prompt_tool": prompt_tool_supported,
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
    "CLAUDE_PERMISSION_PROMPT_TOOL_FLAG",
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
    "probe_claude_permission_prompt_tool",
    "select_claude_control_path",
]
