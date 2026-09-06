# PERM-104-001 / RM7A-001 evidence

Date: 2026-09-05
Project/artifact root: `/Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/codex`
Branch: `agent/codex`

## 1. Starting state and safe reconciliation

- Starting `HEAD`: `d06519c3bc7c107aadde20badc366b7b8844b22b`.
- Starting `private/integration`: `ee2d3e8a999ed44bbd6dc4f44669ff043af55829`.
- Starting merge-base: `5dbc816687434d347ab6749567ebb1e05b8e6951`.
- Starting divergence (`HEAD...private/integration`): `1 4`.
- The original worktree contained 10 modified tracked files and one new
  untracked `src/agent_bridge_connect/permission_elevation.py`. The exact
  WIP was preserved in `90046942e4b70002117e2328459a4517ca2cd94b`.
- `private/integration` was merged with `--no-ff --no-commit`; its only
  conflict was `runner.py`. The resolution retained the task-scoped Runner
  IPC, PathPlan and host-containment behavior from the task WIP. The merge
  commit is `9ce2a25`.
- No public ref, remote ref, private executor storage, other worktree, or
  AgentBC report file was modified. AgentBC Core remains the report owner.

## 2. Native Claude behavior implemented

The live path is in `src/agent_bridge_connect/claude_sdk_transport.py`, with
durable receipt and Core routing in
`src/agent_bridge_connect/claude_elevation.py` and
`src/agent_bridge_connect/service.py`.

| Check | Evidence |
| --- | --- |
| Initial mode | Safe/inherit Claude task starts through SDK `permission_mode=default`; explicit full still maps directly to `bypassPermissions`. |
| Binding | First authoritative `can_use_tool` is bound to task, executor run, official session, request ID, tool-use ID, request/action fingerprints and redacted input digest. |
| Approve wire | One official `PermissionResultAllow` carries the unchanged original `updatedInput` plus exactly `PermissionUpdate(type="setMode", mode="bypassPermissions", destination="session")`. |
| Deny wire | One official `PermissionResultDeny`; no `updatedPermissions` and no mode change. |
| Same execution | The live worker, RunLease and official session remain unchanged; no worker, CLI continuation, new session, matcher, category, session-rule bundle or generated grant is created. |
| Lifecycle | Durable receipt records `safe/default -> elevation_pending -> set_mode_response_ready -> bypassPermissions_active`; active requires the exact approved tool-use's structured `PostToolUse` success. |
| Replay/failure | A repeated post-activation callback records one `claude_full_mode_ineffective` anomaly and denies without another popup or grant. Rejected setMode construction, transport loss, unsupported protocol shape and identity mismatch fail closed. |
| Containment | Runner PathPlan, preflight and host-containment/profile digests remain bound; the early executor-run registration closes the callback-vs-RunLease receipt race. |

## 3. Automated evidence

Focused command:

```text
PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache PYTHONPATH=src uv run python -m unittest \
  tests.test_perm104_001_claude_same_session_elevation \
  tests.test_perm104_002_sdk_transport \
  tests.test_perm104_002_production_wiring \
  tests.test_perm104_002_production_routing \
  tests.test_perm104_002_transport \
  tests.test_claude_control -q
```

Result: `Ran 97 tests in 1.508s` / `OK`.

The new focused module covers the exact SDK serializer, Deny/no-update,
single dialog/input/decision, concurrent and replayed events, same session
and active RunLease, no matcher/category/session-rule fallback, version-
independent protocol-shape admission, unsupported shape, rejected setMode,
transport loss, and a fake SDK/CLI stream completing heterogeneous Read,
Write/Edit and Bash actions after the first approval with no later callback.

Installed protocol dependencies inspected by that coverage:

- Claude Code: `2.1.247 (Claude Code)` at `/Users/wangroway/.local/bin/claude`.
- `claude-agent-sdk`: `0.2.142`.
- The official SDK serializer produced the exact `setMode/bypassPermissions/session`
  update and original-input wire shape; no version allowlist is used for
  capability admission.

Complete suite:

```text
PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache PYTHONPATH=src uv run python -m unittest discover -s tests -q
```

Result: `Ran 1740 tests in 78.820s` / `OK`.

Additional gates:

- `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache PYTHONPATH=src uv run ruff check src tests` — `All checks passed!`
- `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache PYTHONPATH=src uv run python -m compileall -q src tests` — passed.
- `uv build --offline` — built `dist/agentbc-1.0.3a2.tar.gz` and
  `dist/agentbc-1.0.3a2-py3-none-any.whl`.
- `git diff --check` — passed.

## 4. Local completion

- Implementation commit: `6bbca2c580f442bd768d3de14706eaf4c0f91b09`
  (`fix(perm104): atomically elevate Claude live session`).
- The evidence file is committed as the final local evidence commit after
  the implementation commit; no push was performed.
- The final handoff verifies `git status --short` is empty.
