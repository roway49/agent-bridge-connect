"""PERM-104-002 isolated live probe v2 (Slice A, repeat run).

Same contract as probe.py, plus: the official session id is read from
ResultMessage.session_id (SDK-parsed wire field) and must be identical and
non-empty across all three phases in one client/session.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
)

CLI_PATH = "/Users/wangroway/.local/share/claude/versions/2.1.233"

evidence: dict = {
    "probe": "PERM-104-002-sliceA-repeat",
    "sdk_version": getattr(claude_agent_sdk, "__version__", None) or "0.2.142",
    "sdk_package": "claude-agent-sdk==0.2.142",
    "cli_path": CLI_PATH,
    "cli_version": None,
    "checks": {},
    "events": [],
}


def record(check: str, ok: bool, detail: dict) -> None:
    evidence["checks"][check] = {"ok": ok, **detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {check}: {json.dumps(detail, sort_keys=True)}", flush=True)


def log(event: str, **fields) -> None:
    entry = {"event": event, **fields}
    evidence["events"].append(entry)
    print(f"[EVT] {json.dumps(entry, sort_keys=True)}", flush=True)


async def main() -> int:
    proc = await asyncio.create_subprocess_exec(
        CLI_PATH, "--version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    cli_version = out.decode().strip()
    evidence["cli_version"] = cli_version
    record("cli_version", proc.returncode == 0 and "2.1.233" in cli_version, {"version": cli_version})

    workdir = Path(tempfile.mkdtemp(prefix="perm104-probe2-"))
    allow_file = workdir / "allow.txt"
    deny_file = workdir / "deny.txt"

    decisions: dict = {"seen": [], "results": []}
    session_ids: list[str] = []
    state = {"phase": "allow"}

    async def can_use_tool(tool_name: str, input_data: dict, context):
        tool_use_id = str(getattr(context, "tool_use_id", "") or "")
        log("can_use_tool", tool=tool_name, tool_use_id=tool_use_id, input_keys=sorted(input_data.keys()))
        decisions["seen"].append({"tool": tool_name, "tool_use_id": tool_use_id, "input": input_data})
        if state["phase"] == "allow":
            state["phase"] = "deny"
            decisions["results"].append({"tool_use_id": tool_use_id, "decision": "allow", "original_input": dict(input_data)})
            return PermissionResultAllow(updated_input=input_data)
        decisions["results"].append({"tool_use_id": tool_use_id, "decision": "deny"})
        return PermissionResultDeny(message="AgentBC probe deny")

    options = ClaudeAgentOptions(
        cli_path=CLI_PATH,
        cwd=str(workdir),
        permission_mode="default",
        can_use_tool=can_use_tool,
        allowed_tools=[],
    )

    async with ClaudeSDKClient(options) as client:
        await client.query(
            "Use the Bash tool to run exactly: echo probe-allow > allow.txt\n"
            "Then reply with the single word DONE."
        )
        async for message in client.receive_response():
            if type(message).__name__ == "ResultMessage":
                sid = str(getattr(message, "session_id", "") or "")
                session_ids.append(sid)
                log("result_allow_phase", session_id=sid, is_error=bool(getattr(message, "is_error", None)))

        allow_existed = allow_file.exists()
        deny_absent_before = not deny_file.exists()

        state["phase"] = "deny"
        await client.query(
            "Use the Bash tool to run exactly: echo probe-deny > deny.txt\n"
            "The tool call may be refused; if so, just reply with the single word REFUSED. "
            "Do not retry. Do not use any other tool."
        )
        async for message in client.receive_response():
            if type(message).__name__ == "ResultMessage":
                sid = str(getattr(message, "session_id", "") or "")
                session_ids.append(sid)
                log("result_deny_phase", session_id=sid)

        await client.query("Reply with the single word ALIVE and nothing else.")
        tail = ""
        async for message in client.receive_response():
            if type(message).__name__ == "ResultMessage":
                sid = str(getattr(message, "session_id", "") or "")
                session_ids.append(sid)
                text = getattr(message, "result", None)
                if isinstance(text, str):
                    tail = text

    deny_absent_after = not deny_file.exists()
    seen = decisions["seen"]
    results = decisions["results"]
    stable_ids = (
        len(seen) >= 2
        and all(s["tool_use_id"] for s in seen)
        and seen[0]["tool_use_id"] != seen[1]["tool_use_id"]
    )
    same_session = (
        len(session_ids) == 3
        and all(session_ids)
        and len(set(session_ids)) == 1
    )

    record("can_use_tool_fired_with_stable_tool_use_id", stable_ids, {
        "requests": [
            {"tool": s["tool"], "tool_use_id": s["tool_use_id"], "input": s["input"]} for s in seen
        ],
    })
    allow_result = next((r for r in results if r["decision"] == "allow"), None)
    allow_input = next(
        (s["input"] for s in seen if allow_result and s["tool_use_id"] == allow_result["tool_use_id"]),
        None,
    )
    record("allow_with_original_input", allow_existed and allow_result is not None and allow_result["original_input"] == allow_input, {
        "allow_file_created": allow_existed,
        "allow_file_content": allow_file.read_text().strip() if allow_existed else None,
        "returned_updated_input_matches_original": bool(allow_result and allow_result["original_input"] == allow_input),
    })
    record("deny_zero_execution", deny_absent_before and deny_absent_after, {"deny_file_created": not deny_absent_after})
    record("same_client_session_continued", same_session and "ALIVE" in tail, {
        "session_ids": session_ids,
        "single_session_all_phases": same_session,
        "tail_reply": tail,
    })

    ok = all(c["ok"] for c in evidence["checks"].values())
    evidence["verdict"] = "pass" if ok else "fail"
    out_path = workdir / "probe-evidence.json"
    out_path.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    print(f"EVIDENCE={out_path}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
