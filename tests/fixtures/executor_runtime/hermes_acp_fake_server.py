"""Deterministic fake Hermes ACP server for AgentBC Task 6 transport tests.

Speaks the Agent Client Protocol over stdio like ``hermes acp`` and is driven
by a fixed mode name, so tests are deterministic and need no real Hermes
runtime.  Every received client frame and every emitted server frame is
appended to a JSONL log file for assertions.

Usage::

    python hermes_acp_fake_server.py <mode> <log_path>

Modes:

* ``happy``: full handshake, session/new, then prompt; emits one
  ``session/request_permission`` request (options allow_once + deny) and
  waits for the client outcome before replying ``stopReason=end_turn``.
* ``happy_no_permission``: handshake and prompt without any permission
  request.
* ``resume``: like ``happy`` but expects ``session/load`` with the explicit
  session id before the prompt.
* ``load_unknown``: ``session/load`` is rejected with a JSON-RPC error.
* ``wrong_version``: ``initialize`` negotiates protocol version 2.
* ``malformed``: emits a non-JSON line before replying to ``initialize``.
* ``dup_response``: answers ``initialize`` twice.
* ``exit_mid_prompt``: never answers ``session/prompt`` and exits.
* ``unsupported_options``: the permission request offers only ``deny``.
* ``wrong_session_permission``: the permission request is bound to a
  different session id than the one that was created.

The fake never reads or writes any Hermes state; it only speaks the protocol.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PERMISSION_ID = 100
SESSION_ID = "acp-session-fake-1"
OTHER_SESSION_ID = "acp-session-other-1"


def _frame(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


def _send(frame: dict[str, Any], log: list[dict[str, Any]]) -> None:
    sys.stdout.write(_frame(frame))
    sys.stdout.flush()
    log.append({"direction": "server", "frame": frame})


def _log_line(path: str, entry: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _read_frame() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if line == "":
        return None
    stripped = line.strip()
    if not stripped:
        return _read_frame()
    return json.loads(stripped)


def _options(include_allow_once: bool) -> list[dict[str, str]]:
    options = [{"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"}]
    if not include_allow_once:
        options = []
    options.append({"optionId": "deny", "kind": "reject_once", "name": "Deny"})
    options.append({"optionId": "allow_session", "kind": "allow_always", "name": "Allow for session"})
    return options


def _permission_frame(session_id: str, include_allow_once: bool = True) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": PERMISSION_ID,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "id": "perm-check-1",
                "kind": "execute",
                "title": "run the approved command",
                "status": "pending",
                "content": [{"type": "text", "text": "$ hermes acp --check"}],
                "rawInput": {"command": "hermes acp --check", "description": "verify ACP"},
            },
            "options": _options(include_allow_once),
        },
    }


def _prompt_reply(request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}}


def main(mode: str, log_path: str) -> int:
    log: list[dict[str, Any]] = []
    request_id = 0

    def respond(result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            frame["error"] = error
        else:
            frame["result"] = result or {}
        _send(frame, log)

    while True:
        frame = _read_frame()
        if frame is None:
            return 0
        log.append({"direction": "client", "frame": frame})
        method = str(frame.get("method") or "")
        request_id = frame.get("id")
        if method == "initialize":
            if mode == "wrong_version":
                respond({"protocolVersion": 2, "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}})
                continue
            if mode == "malformed":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                log.append({"direction": "server", "frame": {"raw": "this is not json"}})
                respond({"protocolVersion": 1, "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}})
                continue
            if mode == "dup_response":
                respond({"protocolVersion": 1, "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}})
                respond({"protocolVersion": 1, "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}})
                continue
            respond({"protocolVersion": 1, "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}})
            continue
        if method == "notifications/initialized":
            continue
        if method == "session/new":
            respond({"sessionId": SESSION_ID})
            continue
        if method == "session/load":
            params = frame.get("params") or {}
            requested = str(params.get("sessionId") or "")
            if mode == "load_unknown" or requested != SESSION_ID:
                respond(error={"code": -32602, "message": "session not found"})
                continue
            respond({"models": None, "modes": None})
            continue
        if method == "session/prompt":
            if mode == "exit_mid_prompt":
                _log_line(log_path, {"event": "exit_mid_prompt"})
                return 0
            if mode in {"happy", "resume", "unsupported_options", "wrong_session_permission"}:
                _send(_permission_frame(
                    OTHER_SESSION_ID if mode == "wrong_session_permission" else SESSION_ID,
                    include_allow_once=mode != "unsupported_options",
                ), log)
                outcome = _read_frame()
                if outcome is None:
                    return 0
                log.append({"direction": "client", "frame": outcome})
                _log_line(log_path, {"event": "permission_outcome", "frame": outcome})
            _send(_prompt_reply(request_id), log)
            continue
        if method == "session/cancel":
            continue
        # Unknown method: reply with a proper JSON-RPC error like the real
        # agent-client-protocol router would.
        respond(error={"code": -32601, "message": "method not found"})
        continue


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: hermes_acp_fake_server.py <mode> <log_path>", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1], sys.argv[2]))
    except Exception as exc:  # pragma: no cover - failure evidence only
        print(f"fake acp server failed: {exc}", file=sys.stderr)
        sys.exit(1)
