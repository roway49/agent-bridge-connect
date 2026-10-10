"""Controlled OpenCode fakes for the adapter tests.

These helpers create a real, isolated ``/bin/sh`` executable that imitates the
frozen OpenCode CLI surface (``--version``, ``debug agent build`` and
``run``) without contacting a provider or a model.  Every behavior is driven
by environment variables and every invocation records the exact argv, cwd,
PWD and permission environment the adapter produced, so the tests assert on
observed process facts instead of mocking ``subprocess``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Iterable

#: Posix shell fake.  Placeholders are environment variables set by a test.
FAKE_SCRIPT = r'''#!/bin/sh
capture="${OPENCODE_FAKE_CAPTURE:-}"
if [ -n "$capture" ]; then
  : > "$capture"
  printf 'PWD=%s\n' "${PWD-}" >> "$capture"
  printf 'CWD=%s\n' "$(pwd)" >> "$capture"
  printf 'CFG=%s\n' "${OPENCODE_CONFIG_CONTENT-}" >> "$capture"
  printf 'PERM=%s\n' "${OPENCODE_PERMISSION-}" >> "$capture"
  for arg in "$@"; do
    printf 'ARG=%s\n' "$(printf '%s' "$arg" | base64)" >> "$capture"
  done
fi
if [ "$1" = "--version" ]; then
  printf '%s\n' "${OPENCODE_FAKE_VERSION-1.18.31}"
  exit 0
fi
if [ "$1" = "debug" ]; then
  if [ -n "${OPENCODE_FAKE_RULES-}" ]; then
    printf '%s\n' "$OPENCODE_FAKE_RULES"
  fi
  exit "${OPENCODE_FAKE_DEBUG_RC-0}"
fi
if [ -n "${OPENCODE_FAKE_SLEEP-}" ]; then
  sleep "$OPENCODE_FAKE_SLEEP"
fi
if [ -n "${OPENCODE_FAKE_DERIVE_FILE-}" ]; then
  ( while :; do printf 'tick\n' >> "$OPENCODE_FAKE_DERIVE_FILE"; sleep 0.05; done ) &
  printf '%s\n' "$!" > "${OPENCODE_FAKE_DERIVE_FILE}.pid"
fi
if [ -n "${OPENCODE_FAKE_STDOUT_FILE-}" ]; then
  cat "$OPENCODE_FAKE_STDOUT_FILE"
elif [ -n "${OPENCODE_FAKE_STDOUT-}" ]; then
  printf '%s' "$OPENCODE_FAKE_STDOUT"
fi
if [ -n "${OPENCODE_FAKE_STDERR_FILE-}" ]; then
  cat "$OPENCODE_FAKE_STDERR_FILE" >&2
elif [ -n "${OPENCODE_FAKE_STDERR-}" ]; then
  printf '%s' "$OPENCODE_FAKE_STDERR" >&2
fi
if [ -n "${OPENCODE_FAKE_OUTPUT_READY-}" ]; then
  touch "$OPENCODE_FAKE_OUTPUT_READY"
fi
if [ -n "${OPENCODE_FAKE_SLEEP_AFTER-}" ]; then
  sleep "$OPENCODE_FAKE_SLEEP_AFTER"
fi
exit "${OPENCODE_FAKE_EXIT-0}"
'''


def make_fake_opencode(directory: Path, name: str = "opencode") -> Path:
    """Create an executable fake OpenCode binary inside ``directory``."""
    path = directory / name
    path.write_text(FAKE_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path


def jsonl(events: Iterable[dict[str, Any]]) -> str:
    """Serialize events exactly as the CLI JSON transport would."""
    return "".join(
        json.dumps(event, ensure_ascii=False) + "\n" for event in events
    )


def write_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> Path:
    path.write_text(jsonl(events), encoding="utf-8")
    return path


def text_event(
    text: str, *, session_id: str = "ses-main", event_type: str = "text"
) -> dict[str, Any]:
    return {
        "type": event_type,
        "sessionID": session_id,
        "part": {"type": event_type, "text": text},
    }


def step_finish_event(
    reason: str, *, session_id: str = "ses-main"
) -> dict[str, Any]:
    return {
        "type": "step_finish",
        "sessionID": session_id,
        "part": {"type": "step_finish", "reason": reason},
    }


def tool_use_event(
    *,
    session_id: str = "ses-main",
    tool: str = "bash",
    status: str = "completed",
    error: Any = None,
    child_session_id: str = "",
) -> dict[str, Any]:
    metadata = {"sessionId": child_session_id} if child_session_id else {}
    state: dict[str, Any] = {"status": status, "metadata": metadata}
    if error is not None:
        state["error"] = error
    return {
        "type": "tool_use",
        "sessionID": session_id,
        "part": {
            "type": "tool_use",
            "tool": tool,
            "callID": "call-1",
            "state": state,
        },
    }


def make_task_packet(
    root: Path,
    *,
    mode: str = "inherit",
    task_id: str = "FAKE-001",
    title: str = "Fake OpenCode task",
    steps: list[dict[str, Any]] | None = None,
    board: Path | None = None,
    extensions: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal but contract-valid AgentBC task packet."""
    from agent_bridge_connect.permission_modes import build_permission_record

    declared = steps or [
        {"id": 1, "description": "执行最小 OpenCode 动作", "status": "pending"}
    ]
    merged_extensions: dict[str, Any] = {
        "agentbc.permission": build_permission_record(
            explicit_mode=mode, config={}
        )
    }
    if extensions:
        merged_extensions.update(extensions)
    packet: dict[str, Any] = {
        "task_id": task_id,
        "title": title,
        "assignee": "opencode",
        "steps": declared,
        "workspace": {
            "root": str(root),
            "project_root": str(root),
            "artifact_root": str(root),
            "artifacts_dir": str(root),
            "report_root": str(root / "report"),
            "task_file": str(root / "brief.md"),
            "report_file": str(root / "report.md"),
        },
        "task_board": {"root": str(board or root)},
        "extensions": merged_extensions,
    }
    if extra:
        packet.update(extra)
    return packet


def parse_capture(path: Path) -> dict[str, Any]:
    """Parse one captured invocation into argv, cwd, PWD and raw config."""
    captured: dict[str, Any] = {
        "args": [],
        "PWD": "",
        "CWD": "",
        "CFG": None,
        "PERM": None,
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key == "ARG":
            decoded = base64.b64decode(value.encode("ascii")).decode(
                "utf-8", "replace"
            )
            captured["args"].append(decoded)
        elif key in ("PWD", "CWD"):
            captured[key] = value
        elif key == "CFG":
            captured["CFG"] = value if value != "" else None
        elif key == "PERM":
            captured["PERM"] = value if value != "" else None
    return captured
