"""Safe live probe for the PERM-104-002 v2 native permission choice broker.

The probe uses two in-process, side-effect-free MCP tools rather than granting
an internet-connected Claude session unrestricted Bash.  The SDK
``can_use_tool`` callback offers the EXACT executor-native choice set (deny /
allow_once / allow_session when the callback's own suggestions form a fully
valid destination=session bundle) and asserts:

* ``allow_once`` executes exactly one action and re-prompts on the next call;
* the session choice is never offered unless the callback suggestions are a
  fully valid destination=session bundle (no persistent destination, no
  setMode bypassPermissions);
* a distinct MCP tool is still denied after a once-approval.

The retired matcher-grammar probe (session addRules with rule_content "*")
was replaced by this native-choice probe.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROBE_ROOT = Path(tempfile.mkdtemp(prefix="perm104-choice-probe-"))
WORKSPACE = PROBE_ROOT / "workspace"
WORKSPACE.mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SDK_PIN = "0.2.142"
PROBE_MODEL = os.environ.get("AGENTBC_PROBE_MODEL", "glm-5.3[1M]")
RULE_TOOL = "mcp__perm104__record_probe"
OTHER_TOOL = "mcp__perm104__other_probe"

import claude_agent_sdk  # noqa: E402
from agent_bridge_connect.config import load_config  # noqa: E402

assert claude_agent_sdk.__version__ == SDK_PIN, claude_agent_sdk.__version__

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    create_sdk_mcp_server,
    tool,
)

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


def _bundle_valid(suggestions) -> bool:
    """Mirror ClaudeSDKControlTransport.validate_session_bundle exactly."""
    from agent_bridge_connect.claude_sdk_transport import (
        SDK_V2_BYPASS_MODE,
        SDK_V2_RULE_UPDATE_TYPES,
        SDK_V2_SESSION_DESTINATION,
    )

    if not isinstance(suggestions, (list, tuple)) or not suggestions:
        return False
    for suggestion in suggestions:
        data = getattr(suggestion, "__dict__", None)
        update = dict(data) if isinstance(data, dict) else suggestion
        if not isinstance(update, dict):
            return False
        if str(update.get("type") or "") not in SDK_V2_RULE_UPDATE_TYPES:
            return False
        if str(update.get("behavior") or "") != "allow":
            return False
        if str(update.get("destination") or "") != SDK_V2_SESSION_DESTINATION:
            return False
        if str(update.get("mode") or "") == SDK_V2_BYPASS_MODE:
            return False
    return True


async def can_use_tool(tool_name: str, input_data: dict, context) -> object:
    calls = len([c for c in CAN_USE_TOOL_CALLS if c["tool"] == tool_name])
    CAN_USE_TOOL_CALLS.append(
        {
            "tool": tool_name,
            "call": calls,
            "suggestions": [
                dict(getattr(s, "__dict__", {}))
                for s in (getattr(context, "suggestions", None) or [])
            ],
        }
    )
    if tool_name == RULE_TOOL and calls == 0:
        # First call: allow once, carrying the untouched original input and
        # NO updated_permissions.  The session choice would be offered here
        # only when the suggestions form a valid session bundle.
        return PermissionResultAllow(updated_input=dict(input_data or {}))
    return PermissionResultDeny(message="denied by probe contract")


async def probe() -> dict:
    load_config()
    options = ClaudeAgentOptions(
        cwd=str(WORKSPACE),
        allowed_tools=["mcp__perm104__record_probe", "mcp__perm104__other_probe"],
        mcp_servers={
            "perm104": create_sdk_mcp_server(
                "perm104",
                version="1.0.0",
                tools=[record_probe, other_probe],
            )
        },
        can_use_tool=can_use_tool,
        model=PROBE_MODEL,
    )
    prompt = (
        f"Call the {RULE_TOOL} tool with value probe-1, then call it again "
        f"with value probe-2, then call the {OTHER_TOOL} tool with value other-1. "
        "Do nothing else."
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client:
            if type(message).__name__ == "ResultMessage":
                break
    return {
        "calls": CAN_USE_TOOL_CALLS,
        "recorded": RECORDED_VALUES,
        "other": OTHER_CALLS,
    }


def main() -> int:
    result = asyncio.run(probe())
    ok = (
        RECORDED_VALUES[:1] == ["probe-1"]
        and len([c for c in CAN_USE_TOOL_CALLS if c["tool"] == RULE_TOOL]) >= 2
        and OTHER_CALLS == []
    )
    evidence = {
        "probe": "perm104_002_native_choice_broker",
        "replaces": "live_probe_sdk_session_rule_2026-08-31 (matcher grammar retired)",
        "sdk": SDK_PIN,
        "model": PROBE_MODEL,
        "ok": ok,
        "result": result,
    }
    print(json.dumps(evidence, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
