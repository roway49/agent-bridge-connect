"""Safe live probe for Claude SDK session-scoped tool-type approval.

The probe uses two in-process, side-effect-free MCP tools rather than granting
an internet-connected Claude session unrestricted Bash. It approves the first
call to ``mcp__perm104__record_probe`` with the official session update whose
rule content is ``*``. Pass requires a second call to the same tool to execute
without another callback, while a distinct MCP tool is still denied.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PROBE_ROOT = Path(tempfile.mkdtemp(prefix="perm104-rule-probe-"))
WORKSPACE = PROBE_ROOT / "workspace"
WORKSPACE.mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SDK_PIN = "0.2.142"
PROBE_MODEL = os.environ.get("AGENTBC_PROBE_MODEL", "glm-5.3[1M]")
RULE_TOOL = "mcp__perm104__record_probe"
OTHER_TOOL = "mcp__perm104__other_probe"

import claude_agent_sdk  # noqa: E402
from agent_bridge_connect.config import get_executor_config, load_config  # noqa: E402

assert claude_agent_sdk.__version__ == SDK_PIN, claude_agent_sdk.__version__

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import PermissionRuleValue  # noqa: E402

CAN_USE_TOOL_CALLS: list[dict] = []
RECORDED_VALUES: list[str] = []
OTHER_CALLS: list[str] = []


@tool("record_probe", "Record one harmless protocol-probe value", {"value": str})
async def record_probe(args: dict) -> dict:
    value = str(args.get("value") or "")
    RECORDED_VALUES.append(value)
    return {"content": [{"type": "text", "text": f"recorded:{value}"}]}


@tool("other_probe", "A distinct harmless protocol-probe tool", {"value": str})
async def other_probe(args: dict) -> dict:
    value = str(args.get("value") or "")
    OTHER_CALLS.append(value)
    return {"content": [{"type": "text", "text": f"other:{value}"}]}


def resolve_probe_cli_path() -> str:
    """Use the exact production Runner binary unless explicitly overridden."""
    override = str(os.environ.get("AGENTBC_PROBE_CLAUDE_BIN") or "").strip()
    configured = str(
        get_executor_config(load_config(), "claude").get("command") or ""
    ).strip()
    selected = override or configured
    if not selected or not Path(selected).expanduser().is_file():
        raise RuntimeError(
            "The probe requires the exact configured Claude Runner binary; "
            "set executors.claude.command or AGENTBC_PROBE_CLAUDE_BIN."
        )
    return str(Path(selected).expanduser().resolve())


async def main() -> dict:
    cli_path = resolve_probe_cli_path()

    async def deciding_can_use_tool(tool_name, tool_input, context):
        name = str(tool_name or "")
        CAN_USE_TOOL_CALLS.append(
            {
                "tool": name,
                "tool_use_id": str(getattr(context, "tool_use_id", "") or ""),
            }
        )
        if name == RULE_TOOL:
            return PermissionResultAllow(
                updated_input=dict(tool_input or {}),
                updated_permissions=[
                    PermissionUpdate(
                        type="addRules",
                        rules=[
                            PermissionRuleValue(
                                tool_name=RULE_TOOL,
                                rule_content="*",
                            )
                        ],
                        behavior="allow",
                        destination="session",
                    )
                ],
            )
        return PermissionResultDeny(message="probe: distinct tool denied")

    server = create_sdk_mcp_server("perm104", tools=[record_probe, other_probe])
    options = ClaudeAgentOptions(
        cli_path=cli_path,
        cwd=str(WORKSPACE),
        permission_mode="default",
        can_use_tool=deciding_can_use_tool,
        allowed_tools=[],
        tools=[RULE_TOOL, OTHER_TOOL],
        mcp_servers={"perm104": server},
        strict_mcp_config=True,
        model=PROBE_MODEL,
        session_id=str(uuid.uuid4()),
    )
    client = ClaudeSDKClient(options)
    await client.connect()
    session_ids: set[str] = set()
    result_tail = ""
    try:
        await client.query(
            "Call mcp__perm104__record_probe exactly twice in sequence: first "
            "with value 'first', then with value 'second'. Then call "
            "mcp__perm104__other_probe once with value 'other'. If that final "
            "call is denied, continue and reply DONE. Do not use other tools."
        )
        async for message in client.receive_response():
            if type(message).__name__ == "ResultMessage":
                sid = str(getattr(message, "session_id", "") or "")
                if sid:
                    session_ids.add(sid)
                result_tail = str(getattr(message, "result", "") or "")[:200]
    finally:
        await client.disconnect()

    matching_callbacks = [
        call for call in CAN_USE_TOOL_CALLS if call["tool"] == RULE_TOOL
    ]
    other_callbacks = [
        call for call in CAN_USE_TOOL_CALLS if call["tool"] == OTHER_TOOL
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
        "two_matching_calls_executed": RECORDED_VALUES == ["first", "second"],
        "tool_type_rule_prevented_second_prompt": len(matching_callbacks) == 1,
        "distinct_tool_re_prompted": len(other_callbacks) >= 1,
        "distinct_tool_not_executed": OTHER_CALLS == [],
        "settings_files_untouched": settings_untouched,
    }
    return {
        "probe": "PERM-104-002-session-rule-safe-live-probe",
        "sdk_version": SDK_PIN,
        "cli_path": cli_path,
        "cli_version": subprocess.run(
            [cli_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "probe_model": PROBE_MODEL,
        "rule": {"tool_name": RULE_TOOL, "rule_content": "*"},
        "can_use_tool_calls": CAN_USE_TOOL_CALLS,
        "recorded_values": RECORDED_VALUES,
        "session_ids": sorted(session_ids),
        "result_tail": result_tail,
        "checks": checks,
        "verdict": "pass" if all(checks.values()) else "fail",
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, ensure_ascii=False))
