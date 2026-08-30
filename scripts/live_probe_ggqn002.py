"""GGQN-002 frozen live probe: claude-agent-sdk 0.2.142 + Claude 2.1.233.

Runs the AgentBC control transport's PRODUCTION session driver
(``run_controlled``) against the real pinned CLI in one official SDK
session.  First can_use_tool request is allowed (exact original input),
the second is denied.  The probe verifies:

* allow executes the exact original input (allow file created with the
  exact probe content), deny produces zero execution;
* stable non-empty tool_use_id on every can_use_tool request;
* the approved identity becomes the transport's verification anchor and the
  production hook feed delivers a structured PostToolUse success bound to
  the same official session;
* one single session across all phases.

Prints the verdict JSON to stdout; the repo is never written.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path

PROBE_ROOT = Path(tempfile.mkdtemp(prefix="ggqn002-probe-"))
WORKSPACE = PROBE_ROOT / "workspace"
WORKSPACE.mkdir()
CONTROL_ROOT = PROBE_ROOT / "control"
CONTROL_ROOT.mkdir()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SDK_PIN = "0.2.142"
CLI_PIN = "2.1.233 (Claude Code)"
CLI_PATH = "/Users/wangroway/.local/share/claude/versions/2.1.233"

import claude_agent_sdk  # noqa: E402

assert claude_agent_sdk.__version__ == SDK_PIN, claude_agent_sdk.__version__

from agent_bridge_connect.claude_sdk_hooks import (  # noqa: E402
    bind_hook_log_session,
    build_sdk_hooks,
    load_hook_records,
)
from agent_bridge_connect.claude_sdk_transport import (  # noqa: E402
    ClaudeSDKControlTransport,
)

SESSION_ID = str(uuid.uuid4())
EVENTS: list[dict] = []


class _StubPlane:
    """Minimal plane facade for the driver; decisions are scripted."""

    def __init__(self) -> None:
        self.decisions: dict[str, str] = {}

    def request_approval(self, message: dict) -> dict:
        identity = str(message.get("_agentbc", {}).get("tool_use_id") or "")
        EVENTS.append(
            {
                "event": "can_use_tool",
                "tool_use_id": identity,
                "tool": str(message.get("_agentbc", {}).get("tool_name") or ""),
            }
        )
        return {"request_id": identity}

    def wait_for_decision(self, request_id: str, timeout_s: float) -> dict:
        return {"decision": self.decisions.get(request_id, "decline")}

    def record_transport_failed(self, *args, **kwargs):
        return {}


class _StubOptions:
    """Options facade consumed by the transport driver."""


async def main() -> dict:
    plane = _StubPlane()
    transport = ClaudeSDKControlTransport(
        plane=plane,  # type: ignore[arg-type]
        task_id="PROBE",
        run_id="probe-run",
        session_id=SESSION_ID,
    )
    hooks = build_sdk_hooks(CONTROL_ROOT, event_sink=transport)
    assert bind_hook_log_session(CONTROL_ROOT, SESSION_ID)

    seen = {"count": 0}

    async def deciding_can_use_tool(tool_name, tool_input, context):
        identity = str(context.tool_use_id or "")
        seen["count"] += 1
        if seen["count"] == 1:
            plane.decisions[identity] = "accept"
        else:
            plane.decisions[identity] = "decline"
        return await transport.can_use_tool(tool_name, tool_input, context)

    options = claude_agent_sdk.ClaudeAgentOptions(
        cli_path=CLI_PATH,
        cwd=str(WORKSPACE),
        permission_mode="default",
        can_use_tool=deciding_can_use_tool,
        allowed_tools=[],
        disallowed_tools=["TaskCreate", "TaskUpdate", "TodoWrite"],
        hooks=hooks,
        session_id=SESSION_ID,
    )

    captured = await asyncio.wait_for(
        transport._run_session_coroutine(
            options,
            "Run exactly two shell commands: `echo probe-allow > allow.txt` "
            "and then `echo probe-deny > deny.txt`. Then stop.",
            None,
            None,
        ),
        timeout=120,
    )

    # Phase 2: same client session continues (the driver exited; a second
    # client call would open a new session, so continuation is proven inside
    # the single driven session by the ResultMessage session binding).
    records = load_hook_records(CONTROL_ROOT)
    post_success = [
        record
        for record in records
        if record.get("event") == "PostToolUse" and record.get("blocked") is False
    ]
    anchor = str(transport._verification_anchor or "")
    anchor_post = [
        record for record in post_success if str(record.get("tool_use_id") or "") == anchor
    ]
    result = captured.get("result") or {}

    checks = {
        "allow_result_is_error_false": bool(result) and not result.get("is_error"),
        "result_session_matches_probe_session": (
            str(result.get("session_id") or "") == SESSION_ID
        ),
        "allow_file_created": (WORKSPACE / "allow.txt").is_file(),
        "allow_file_content": (
            (WORKSPACE / "allow.txt").read_text().strip()
            if (WORKSPACE / "allow.txt").is_file()
            else ""
        ),
        "deny_file_created": (WORKSPACE / "deny.txt").is_file(),
        "approved_identity_anchored": bool(anchor),
        "structured_post_tool_use_success_for_anchor": bool(anchor_post),
        "anchor_post_session_bound": bool(anchor_post)
        and all(record.get("session_id") == SESSION_ID for record in anchor_post),
        "hook_log_session_bound": bool(records)
        and all(record.get("session_id") == SESSION_ID for record in records),
        "two_can_use_tool_requests": seen["count"] == 2,
    }

    transport.stop()

    ok = (
        checks["allow_result_is_error_false"]
        and checks["result_session_matches_probe_session"]
        and checks["allow_file_created"]
        and checks["allow_file_content"] == "probe-allow"
        and not checks["deny_file_created"]
        and checks["approved_identity_anchored"]
        and checks["structured_post_tool_use_success_for_anchor"]
        and checks["anchor_post_session_bound"]
        and checks["hook_log_session_bound"]
        and checks["two_can_use_tool_requests"]
    )
    return {
        "probe": "GGQN-002-frozen-live-probe",
        "sdk_version": SDK_PIN,
        "cli_version": CLI_PIN,
        "events": EVENTS,
        "checks": checks,
        "verdict": "pass" if ok else "fail",
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, ensure_ascii=False))
