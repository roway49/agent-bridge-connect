"""Task-scoped Claude project and deliverable path capability.

PERM-103-007 keeps Claude's internal ephemeral project separate from the
task's deliverable root.  The generated CLI settings are intentionally small
and deterministic so Runner can reconstruct and authorize them from the
persisted PathPlan instead of trusting packet argv or model instructions.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

from .path_model import validate_path_plan_workspace
from .protocol import ABCError


CLAUDE_PATH_CAPABILITY_VERSION = 1
CLAUDE_PATH_CAPABILITY_MIN_VERSION = (2, 1, 216)
CLAUDE_PATH_CAPABILITY_MAX_VERSION = (2, 2, 0)
_CLAUDE_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_CLAUDE_PATH_REQUIRED_FLAGS = ("--add-dir", "--settings")


def assert_claude_path_capability_supported(executable: str | Path) -> dict[str, Any]:
    """Probe the installed CLI before relying on its sandbox path contract."""
    system = platform.system()
    if system not in {"Darwin", "Linux"}:
        raise ABCError(
            "claude_path_capability_unsupported",
            "Claude filesystem sandboxing requires macOS, Linux, or WSL2",
            {"platform": system or "unknown"},
        )
    command = str(Path(executable).expanduser())
    try:
        version_result = subprocess.run(
            [command, "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        help_result = subprocess.run(
            [command, "--help"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ABCError(
            "claude_path_capability_unsupported",
            "Claude path capability probe failed",
        ) from exc
    version_text = f"{version_result.stdout}\n{version_result.stderr}".strip()
    matched = _CLAUDE_VERSION_RE.search(version_text)
    parsed = tuple(int(part) for part in matched.groups()) if matched else None
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    missing_flags = [flag for flag in _CLAUDE_PATH_REQUIRED_FLAGS if flag not in help_text]
    if (
        version_result.returncode != 0
        or help_result.returncode != 0
        or parsed is None
        or not (
            CLAUDE_PATH_CAPABILITY_MIN_VERSION
            <= parsed
            < CLAUDE_PATH_CAPABILITY_MAX_VERSION
        )
        or missing_flags
    ):
        raise ABCError(
            "claude_path_capability_unsupported",
            "Installed Claude cannot express the frozen ephemeral path capability",
            {
                "version": version_text.splitlines()[0][:80] if version_text else "",
                "supported_min": ".".join(map(str, CLAUDE_PATH_CAPABILITY_MIN_VERSION)),
                "supported_before": ".".join(map(str, CLAUDE_PATH_CAPABILITY_MAX_VERSION)),
                "missing_flags": missing_flags,
            },
        )
    return {
        "supported": True,
        "capability_id": "claude.ephemeral_project_isolation.v1",
        "version": ".".join(map(str, parsed)),
        "platform": system,
        "evidence": ["version_gate", "cli_help", "sandbox_fail_closed"],
    }


def claude_ephemeral_path_capability(
    task_packet: dict[str, Any],
    *,
    execution_root: str | Path,
) -> dict[str, Any] | None:
    """Build the exact ephemeral-project isolation capability for one task.

    Native/retained Claude projects do not use this capability.  Ephemeral
    projects must match the frozen PathPlan and receive one separate
    deliverable root.  No AgentBC record/report directory is writable.
    """
    extensions = (
        task_packet.get("extensions")
        if isinstance(task_packet.get("extensions"), dict)
        else {}
    )
    session = extensions.get("agentbc.session")
    if session is None:
        return None
    if not isinstance(session, dict):
        raise ABCError(
            "claude_path_capability_invalid",
            "agentbc.session must be an object",
        )
    if str(session.get("executor") or "").strip().lower() != "claude":
        raise ABCError(
            "claude_path_capability_invalid",
            "Claude path capability requires a Claude session snapshot",
        )
    if session.get("project_mode") == "native":
        return None
    if session.get("project_mode") != "ephemeral" or session.get("retain") is not False:
        raise ABCError(
            "claude_path_capability_invalid",
            "Claude managed sessions require an ephemeral non-retained project",
        )

    workspace = (
        task_packet.get("workspace")
        if isinstance(task_packet.get("workspace"), dict)
        else {}
    )
    validate_path_plan_workspace(workspace)
    project_text = str(workspace.get("executor_project_root") or "").strip()
    artifact_text = str(workspace.get("artifact_root") or "").strip()
    if not project_text or not artifact_text:
        raise ABCError(
            "claude_path_capability_invalid",
            "Claude PathPlan requires executor_project_root and artifact_root",
        )

    project_root = _canonical_absolute(project_text, "executor_project_root")
    artifact_root = _canonical_absolute(artifact_text, "artifact_root")
    actual_execution_root = _canonical_absolute(execution_root, "execution_root")
    session_project = _canonical_absolute(
        str(session.get("project_path") or ""),
        "agentbc.session.project_path",
    )
    if actual_execution_root != project_root or session_project != project_root:
        raise ABCError(
            "claude_path_capability_mismatch",
            "Claude execution root does not match the frozen ephemeral project",
        )
    if artifact_root == project_root:
        raise ABCError(
            "claude_path_capability_mismatch",
            "Claude Artifact root must be separate from its ephemeral project",
        )

    settings = {
        "permissions": {
            # Edit rules cover Claude's built-in Write/Edit file tools.  The
            # absolute // syntax is Claude's permission-rule syntax.
            "deny": [f"Edit(//{str(project_root).lstrip('/')}/**)"]
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            # Preserve AgentBC's native approval flow; sandboxing is a path
            # boundary and must not silently auto-approve Bash actions.
            "autoAllowBashIfSandboxed": False,
            "filesystem": {
                "allowWrite": [str(artifact_root)],
                "denyWrite": [str(project_root)],
            },
        },
    }
    settings_json = json.dumps(
        settings,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "version": CLAUDE_PATH_CAPABILITY_VERSION,
        "capability_id": "claude.ephemeral_project_isolation.v1",
        "project_root": str(project_root),
        "artifact_root": str(artifact_root),
        "additional_dirs": [str(artifact_root)],
        "settings": settings,
        "settings_json": settings_json,
    }


def claude_path_capability_args(capability: dict[str, Any] | None) -> list[str]:
    if capability is None:
        return []
    args = ["--settings", str(capability["settings_json"])]
    for root in capability["additional_dirs"]:
        args.extend(["--add-dir", str(root)])
    return args


def assert_claude_path_capability_command(
    command: list[str],
    task_packet: dict[str, Any],
    *,
    execution_root: str | Path,
) -> dict[str, Any] | None:
    """Fail closed unless argv exactly matches the persisted PathPlan."""
    if any(
        token.startswith("--settings=") or token.startswith("--add-dir=")
        for token in command
    ):
        raise ABCError(
            "claude_path_capability_mismatch",
            "Claude path capability requires canonical separated arguments",
        )
    capability = claude_ephemeral_path_capability(
        task_packet,
        execution_root=execution_root,
    )
    settings_values = _flag_values(command, "--settings")
    add_dir_values = _flag_values(command, "--add-dir")
    expected_settings = [] if capability is None else [capability["settings_json"]]
    expected_dirs = [] if capability is None else list(capability["additional_dirs"])
    if settings_values != expected_settings or add_dir_values != expected_dirs:
        raise ABCError(
            "claude_path_capability_mismatch",
            "Claude path arguments do not match the frozen task capability",
            {
                "settings_count": len(settings_values),
                "add_dir_count": len(add_dir_values),
                "expected_settings_count": len(expected_settings),
                "expected_add_dir_count": len(expected_dirs),
            },
        )
    return capability


def _canonical_absolute(value: str | Path, field: str) -> Path:
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raise ABCError(
            "claude_path_capability_invalid",
            f"{field} must be absolute",
        )
    return raw.resolve()


def _flag_values(command: list[str], flag: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        if token == flag:
            if index + 1 >= len(command):
                values.append("")
            else:
                index += 1
                values.append(command[index])
        elif token.startswith(f"{flag}="):
            values.append(token.split("=", 1)[1])
        index += 1
    return values
