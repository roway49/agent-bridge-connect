"""PERM-104-002 live-compatible probe: official session-scoped PermissionUpdate.

Runs the installed claude-agent-sdk (pinned 0.2.142) + the local Claude CLI in
one official SDK session and answers the first ``can_use_tool`` Bash request
with an ``allow`` that carries the official session-scoped rule update
``PermissionUpdate(type="addRules", rules=[PermissionRuleValue("Bash", "echo probe-rule-allow*")], behavior="allow", destination="session")``.

The probe then asks the SAME live session to run the matching command and a
nonmatching command.  Verdict ``pass`` requires:

* the updatedPermissions allow path is accepted by the CLI (no control error);
* the matching command executes inside the same session WITHOUT a second
  ``can_use_tool`` request (the session rule took effect);
* the nonmatching command still triggers ``can_use_tool`` (narrow rule) and is
  denied with zero execution;
* the official settings files are never written (session scope only).

Prints the verdict JSON to stdout; the repo is never written.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

PROBE_ROOT = Path(tempfile.mkdtemp(prefix="perm104-rule-probe-"))
WORKSPACE = PROBE_ROOT / "workspace"
WORKSPACE.mkdir()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SDK_PIN = "0.2.142"
CLI_PATH = "/Users/wangroway/.local/share/claude/versions/2.1.247"
# The host's Anthropic-compatible proxy rejects thinking-disabled requests for
# glm-5.3-flash; the non-flash glm-5.3 tuple is the probe's pinned live model.
PROBE_MODEL = os.environ.get("AGENTBC_PROBE_MODEL", "glm-5.3[1M]")

import claude_agent_sdk  # noqa: E402

assert claude_agent_sdk.__version__ == SDK_PIN, claude_agent_sdk.__version__

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
)
from claude_agent_sdk.types import PermissionRuleValue  # noqa: E402

MATCH_COMMAND = "echo probe-rule-allow"
MATCH_FILE = "matched.txt"
NONMATCH_FILE = "nonmatched.txt"

CAN_USE_TOOL_CALLS: list[dict] = []


async def main() -> dict:
    async def deciding_can_use_tool(tool_name, tool_input, context):
        identity = str(getattr(context, "tool_use_id", "") or "")
        command = str((tool_input or {}).get("command") or "")
        CAN_USE_TOOL_CALLS.append(
            {
                "tool": str(tool_name or ""),
                "command": command[:200],
                "tool_use_id": identity,
            }
        )
        if command.startswith(MATCH_COMMAND):
            return PermissionResultAllow(
                updated_input=dict(tool_input or {}),
                updated_permissions=[
                    PermissionUpdate(
                        type="addRules",
                        rules=[
                            PermissionRuleValue(
                                tool_name="Bash",
                                rule_content="echo probe-rule-allow*",
                            )
                        ],
                        behavior="allow",
                        destination="session",
                    )
                ],
            )
        return PermissionResultDeny(message="probe: nonmatching command denied")

    options = ClaudeAgentOptions(
        cli_path=CLI_PATH,
        cwd=str(WORKSPACE),
        permission_mode="default",
        can_use_tool=deciding_can_use_tool,
        allowed_tools=[],
        disallowed_tools=["TaskCreate", "TaskUpdate", "TodoWrite"],
        model=PROBE_MODEL,
        session_id=str(uuid.uuid4()),
    )
    client = ClaudeSDKClient(options)
    await client.connect()
    session_ids: set[str] = set()
    result_tail = ""
    try:
        await client.query(
            f"Run exactly this shell command once: {MATCH_COMMAND} > {MATCH_FILE}. "
            f"Then run exactly: echo probe-other > {NONMATCH_FILE}. "
            "If a command is denied, continue to the next one anyway. Then reply DONE."
        )
        async for message in client.receive_response():
            if type(message).__name__ == "ResultMessage":
                sid = str(getattr(message, "session_id", "") or "")
                if sid:
                    session_ids.add(sid)
                result_tail = str(getattr(message, "result", "") or "")[:200]
    finally:
        await client.disconnect()

    matched = WORKSPACE / MATCH_FILE
    nonmatched = WORKSPACE / NONMATCH_FILE
    match_calls = [
        call for call in CAN_USE_TOOL_CALLS if call["command"].startswith(MATCH_COMMAND)
    ]
    nonmatch_calls = [
        call for call in CAN_USE_TOOL_CALLS if not call["command"].startswith(MATCH_COMMAND)
    ]
    settings_untouched = not any(
        path.exists()
        for path in (
            WORKSPACE / ".claude" / "settings.local.json",
            WORKSPACE / ".claude" / "settings.json",
            WORKSPACE / ".claude.json",
        )
    )
    checks = {
        "single_session": len(session_ids) == 1,
        "matched_command_executed": matched.is_file()
        and matched.read_text().strip() == "probe-rule-allow",
        "nonmatching_command_not_executed": not nonmatched.is_file(),
        "nonmatching_command_re_prompted": len(nonmatch_calls) >= 1,
        "matching_command_no_second_prompt": len(match_calls) == 1,
        "settings_files_untouched": settings_untouched,
    }
    ok = all(checks.values())
    return {
        "probe": "PERM-104-002-session-rule-live-probe",
        "sdk_version": SDK_PIN,
        "cli_path_pin": CLI_PATH.rsplit("/", 1)[-1],
        "probe_model": PROBE_MODEL,
        "can_use_tool_calls": CAN_USE_TOOL_CALLS,
        "session_ids": sorted(session_ids),
        "result_tail": result_tail,
        "checks": checks,
        "verdict": "pass" if ok else "fail",
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, ensure_ascii=False))
