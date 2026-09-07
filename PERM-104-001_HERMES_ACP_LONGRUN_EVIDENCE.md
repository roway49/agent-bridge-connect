# PERM-104-001 Hermes ACP long-run finalization and cleanup repair

Date: 2026-09-04
Task: `59QH-001` (AgentBC task `TJBS-001` is the failing baseline)
Branch: `agent/claude` (local commits, not pushed)
Base: `private/integration@8aea853`
Scope: `src/agent_bridge_connect/hermes_acp.py`, `src/agent_bridge_connect/executors/hermes.py`,
`tests/fixtures/executor_runtime/hermes_acp_fake_server.py`,
`tests/fixtures/executor_runtime/matrix/hermes/*/session_delete.json` (+ `manifest.json`),
`tests/test_hermes_acp_transport.py`, `tests/test_perm104_002_v2_broker.py`,
`tests/test_perm104_001_hermes_longrun.py` (new, 26 tests)

## 1. Failure baseline (TJBS-001, authoritative record)

AgentBC task `TJBS-001` — "PERM-104-001 Hermes full unattended completion canary" — failed
twice on the same official session and is the authoritative failure baseline.

| Run | Worker run | Symptom | Recorded evidence |
| --- | --- | --- | --- |
| 1 | `runner-worker-ae183f87a328` / `hermes-TJBS-001-46b34d3d` | Turn killed while Hermes was still working | `errors[0].code = hermes_acp_transport_failed`, message "Hermes ACP receive timed out without a complete frame.", `timeout_is_failure=true`, `retryable=true`. `stderr_tail` shows `API call #2 ... latency=45.1s` plus a tool execution after it, i.e. the agent was healthy and silent for > 30s. |
| 2 | `runner-worker-8e5f981a3d98` / `hermes-TJBS-001-1eb6432e` | Turn finished, task failed anyway | `errors[1].code = completion_marker_missing`, "Executor exited without AGENTBC_FINAL_CALLBACK", `returncode=0`, `marker_seen=false`, `marker_valid=false`, `stop_reason=end_turn`. `TJBS-001-run.log` records all three steps done. |
| cleanup | — | Official session receipt bound but never deleted | `agentbc.session.session_id = 18a3e156-6aae-4286-b504-4276f90fc5b2`, `cleanup.state = failed`, `cleanup.error_code = hermes_session_delete_invalid_session_id`, `retryable=false`, `commands.delete = not_requested`. |

The RunLease for run 2 closed, and the compatibility event `task.agent_callback_recorded`
(15:32:48) plus a chat summary and a real 23-byte artifact `hermes-full-canary.txt`
(`sha256 f1436659c663f717f31466e9f1f86291f01336e75e15d5622bf3cc7bac93da58`) all existed —
none of them is a substitute for the marker, and Core correctly refused to finalize.

## 2. Proven root causes

### RC1 — the terminal answer was never captured (`completion_marker_missing`, rc = 0)

`HermesAcpTransport._collect_message_chunks` probed
`params["sessionUpdate"] → update["type"] → update["message"]["role"] → message["content"][]`.

The pinned `agent-client-protocol` schema that the installed Hermes ACP adapter serialises
(`acp/schema.py`, `SessionNotification` / `ContentChunk`, `model_dump(by_alias=True)`) puts the
notification on the wire as:

```json
{"sessionId": "...", "update": {"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "..."}}}
```

`PromptResponse` carries **only** `stopReason` (+ usage), so the accumulated
`message_text()` was empty for every real turn, `_extract_final_response` had nothing to
validate, and a perfectly completed run failed as `completion_marker_missing`. The executor's
actual terminal answer — including its `AGENTBC_FINAL_CALLBACK` — was dropped on the floor.

### RC2 — a healthy turn was bounded by the per-RPC handshake timeout (`hermes_acp_transport_failed`)

`prompt()` called `_recv_frame(min(remaining, self.rpc_timeout_s))` with
`HERMES_ACP_RPC_TIMEOUT_S = 30.0`. A single model call or tool call can stay silent far longer
(45.1s in run 1), so a healthy turn was killed as a transport failure. The old loop also read
through a line-buffered `TextIOWrapper.readline()` after `select.select()`: `select` reports the
*fd* readable for a partial line, so `readline()` could block past the deadline — the timeout was
not even reliable — and `errors="strict"` let a `UnicodeDecodeError` escape the classified error
set and kill the worker thread without any control-plane record.

### RC3 — temporary-session cleanup rejected the officially bound identifier

`_HERMES_SESSION_ID_RE = ^\d{8}_\d{6}_[0-9a-fA-F]{6,32}$` accepted only the Hermes *CLI chat*
session token. A Hermes **ACP** session id is a UUID (`acp_adapter/session.py`:
`session_id = str(uuid.uuid4())`), and that is exactly what the ACP stderr receipt binds. So
`_hermes_cleanup_request_error` returned `hermes_session_delete_invalid_session_id` before any
deletion subprocess could run — the receipt was bound and then refused by AgentBC itself, and
`hermes sessions delete <session_id> --yes` (whose positional argument accepts the exact id
verbatim) was never executed.

### RC4 — the response path sent an outcome the agent cannot parse

`approval_outcome_for_decision` returned `{"outcome": {"optionId": ...}}`. The ACP
`RequestPermissionResponse.outcome` is `AllowedOutcome | DeniedOutcome`, i.e.
`{"outcome": {"outcome": "selected", "optionId": ...}}` or
`{"outcome": {"outcome": "cancelled"}}`. A response without the `selected` discriminator is not
parseable by the agent and is silently read as a denial.

## 3. Fix

### 3.1 Canonical ACP wire boundary (completes the reviewed in-progress work)

The pre-existing uncommitted `src/agent_bridge_connect/hermes_acp.py` change was a
partially-applied canonical-wire refactor for `session/request_permission`. It was **validated
field-by-field against the installed `agent-client-protocol` schema** and kept:

* `_PERMISSION_PARAMS_WIRE_FIELDS = {_meta, options, sessionId, toolCall}` == `RequestPermissionRequest`
* `_TOOL_CALL_WIRE_FIELDS = {_meta, content, kind, locations, rawInput, rawOutput, status, title, toolCallId}` == `ToolCallUpdate`
* `_PERMISSION_OPTION_WIRE_FIELDS = {_meta, optionId, kind, name}` == `PermissionOption`
* typed `HermesAcpPermissionRequest` / `HermesAcpToolCall` / `HermesAcpPermissionOption`, one
  mechanical decoder (`decode_permission_request`), mixed snake_case/alias frames rejected, no
  fuzzy matching, no executor-version branching.

Incorrect assumptions and defects in that working tree were rejected/repaired rather than
reverted wholesale:

* unreachable dead code after `return HermesAcpPermissionRequest(...)` in
  `decode_permission_request` (left over from the rename) removed;
* `executors/hermes.py` migrated from the removed `validate_permission_request` /
  old `build_approval_message(frame, ..., session_id=...)` signature to the normalized object, so
  the Hermes executor is importable again (it was not importable at all before the repair);
* the dialog-role table stays keyed on the exact Hermes `optionId` (so the native
  `allow_session` choice, which Hermes marks `allow_always` because the ACP enum has no session
  kind, is not hidden), with an added closed fallback over the canonical
  `PermissionOptionKind` enum (`allow_once` → once, `reject_once` → deny) used **only** for an
  unknown `optionId`; `allow_always` / `reject_always` / unknown stay non-actionable;
* fake fixtures and the two affected test modules updated to the canonical shapes they must
  exercise (`toolCall.toolCallId`, `SelectedPermissionOutcome`).

### 3.2 `session/update` terminal-answer capture (RC1)

`_collect_message_chunks` now reads the canonical notification:
`params.sessionId` + `params.update.sessionUpdate` + one single `params.update.content`
content block. Collection is turn-scoped (`_collecting_session_id` is set only while a prompt is
in flight, so `session/load` history replay and any unrelated session can never leak into the
answer the marker is extracted from) and bounded by
`HERMES_ACP_MESSAGE_MAX_BYTES = 1 MiB` with head+tail preservation: the budget evicts the
**oldest** text and never the newest, because the terminal marker is the last line of the
terminal answer. Truncation is reported through `message_truncated()` instead of being silent.

### 3.3 Long-run receive path (RC2)

Three independent, individually truthful bounds replace the single per-frame bound:

| Bound | Default | Meaning on expiry |
| --- | --- | --- |
| `rpc_timeout_s` | 30s | one handshake RPC (`initialize`/`session/new`/`session/load`) → `hermes_acp_rpc_timeout` |
| `receive_timeout_s` | 900s (`HERMES_ACP_RECEIVE_TIMEOUT_S`) | longest silent interval tolerated from a **live** agent inside a turn → `hermes_acp_receive_idle_timeout` |
| `prompt(timeout_s)` | adapter safety runtime (24h) | whole-turn deadline → `hermes_acp_prompt_timeout` |

Framing is byte-accurate: stdout/stderr are binary, frames are assembled in an inbound buffer
until a complete newline-terminated frame exists, then decoded once with an explicit UTF-8 step.
Partial frames are never truncated and never read through a blocking `readline`, and the inbound
buffer is bounded (`hermes_acp_frame_oversized`). EOF, process exit and idle expiry stay three
distinct classified failures:

| Stable code | Exception | Truth |
| --- | --- | --- |
| `hermes_acp_transport_eof` | `HermesAcpError` | the agent closed stdout and is still alive |
| `hermes_acp_transport_exited` | `HermesAcpError` (+ `exit_code`) | the process exited (also reported when EOF and the exit race) |
| `hermes_acp_receive_idle_timeout` | `HermesAcpTimeout` (both `HermesAcpError` and `TimeoutError`) | no complete frame inside the receive window |
| `hermes_acp_prompt_timeout` | `HermesAcpTimeout` | the overall safety runtime expired |

`HermesAcpTimeout` inherits both `HermesAcpError` and `TimeoutError`, so
`timeout_is_failure=true` stays truthful and a timeout is never silently retried and never turned
into success. `on_progress` fires once per received frame for liveness only.

### 3.4 RunLease liveness during a long turn

The ACP worker thread blocks in `prompt()` for the whole turn and `poll()` may not be called for
minutes, so a `_RunLeaseHeartbeat` daemon beats every
`_HERMES_ACP_HEARTBEAT_INTERVAL_S = 30s` (well under the 120s staleness window) while the turn
runs, `prompt(on_progress=...)` beats per frame (rate-limited to one write per interval), and
`poll()` beats for live ACP statuses (`starting`/`prompting`/`finalizing`/`running`). The
heartbeat is a liveness signal only — it never changes run state, retries or completes anything.
The failure payload now also carries the sanitized stable `code` + bounded `details` of the
`HermesAcpError`, so a wire mismatch is diagnosable from the task record alone.

### 3.5 Temporary-session cleanup (RC3)

`_hermes_session_delete_identifier_error` accepts the **two documented Hermes identifier shapes**
and nothing else — the ACP UUID receipt and the CLI chat token — and rejects empty ids, leading
dashes (option injection), whitespace/path separators and ids above 128 characters with the
stable code `hermes_session_delete_invalid_session_id` (`hermes_session_delete_missing_session_id`
when empty). The argv stays exactly `[hermes, "sessions", "delete", <bound id>, "--yes"]`, so
dispatcher sessions, unrelated sessions, fuzzy "id or name" selectors and `--all`-style tokens
can never be targeted. The frozen fixtures
(`matrix/hermes/0.17.0/session_delete.json`, `matrix/hermes/0.20.1/session_delete.json`) and the
matrix `manifest.json` hashes were refreshed through the sanctioned capture tool.

## 4. Stable failure codes

| Code | Layer | Meaning | Retryable |
| --- | --- | --- | --- |
| `hermes_acp_transport_failed` | executor | any classified ACP transport failure (carries `code`, `details`, `timeout_is_failure`) | yes |
| `hermes_acp_receive_idle_timeout` | executor | silent interval exceeded the receive window | yes (`timeout_is_failure=true`) |
| `hermes_acp_prompt_timeout` | executor | whole-turn safety runtime exceeded | yes (`timeout_is_failure=true`) |
| `hermes_acp_transport_eof` | executor | stdout closed while the process is alive | yes |
| `hermes_acp_transport_exited` | executor | process exited (`exit_code` recorded) | yes |
| `hermes_acp_frame_oversized` | executor | inbound frame above the bounded size | yes |
| `hermes_acp_rpc_timeout` | executor | one handshake RPC timed out | yes |
| `completion_marker_missing` | flow contract | terminal answer contained no `AGENTBC_FINAL_CALLBACK` — return code 0 never substitutes | no |
| `completion_marker_duplicate` | flow contract | more than one marker line | no |
| `hermes_session_delete_invalid_session_id` | session cleanup | identifier is not a documented Hermes session id, empty, option-like or oversized | no |
| `hermes_session_delete_missing_session_id` | session cleanup | no identifier bound | no |
| `hermes_session_delete_failed` | session cleanup | official delete entry failed | no |

## 5. Deterministic test evidence

`tests/test_perm104_001_hermes_longrun.py` (new, 26 tests, real stdio subprocesses against the
fake ACP server; no Hermes runtime, no network, no private executor state):

* **Frame assembly**: split `agent_message_chunk` chunks deliver the terminal marker; a marker
  split mid-line across two raw byte writes is still decoded whole; an `agent_message_chunk`
  bound to another session never reaches the terminal answer; the turn budget keeps the tail so
  the marker survives and truncation is reported.
* **Long intervals**: a turn that stays silent for 1.2s with `rpc_timeout_s=0.3` (the exact bound
  that killed TJBS-001 run 1) completes; the same turn with `receive_timeout_s=0.2` fails — the
  regression guard.
* **Truthful transport failures**: hung transport → `hermes_acp_receive_idle_timeout` (and still
  a `TimeoutError`); stdout closed while alive → `hermes_acp_transport_eof`; closed stdout after
  exit → `hermes_acp_transport_exited` with `exit_code=0`; overall deadline →
  `hermes_acp_prompt_timeout`.
* **Terminal result delivery / completion bridging**: `split_frames` yields `completed`,
  `marker_seen`/`marker_valid` true, one valid callback for the real task and all three declared
  steps, the executor's real text preserved in `stdout`, `returncode=0`; `no_marker` →
  `failed` / `completion_marker_missing` with `agent_callback=None` despite `returncode=0`;
  `duplicate_marker` → `completion_marker_duplicate`; transport death → `needs_recovery` with the
  official persistent receipt; retry on the same official session (`session/load` + receipt
  `resumed=true`) completes.
* **RunLease**: `poll()` refreshes the persisted lease for an in-flight ACP run; the heartbeat
  beat is rate-limited to one write per interval.
* **Session cleanup**: the exact TJBS-001 ACP receipt
  `18a3e156-6aae-4286-b504-4276f90fc5b2` and the CLI token are accepted; empty, `--yes`,
  `20260811 bad`, `delete --all`, `session-name`, `../../etc`, a truncated UUID and a 129-char id
  are rejected without spawning; the spawned argv is exactly the bound id; unrelated/dispatcher
  ids never appear; repeat cleanup is identity-stable; an absent session maps to already-absent.

Updated suites: `tests/test_hermes_acp_transport.py` (27 tests), `tests/test_perm104_002_v2_broker.py`
(29 tests). Local run (Python 3.11 worktree venv):

```
Ran 1756 tests in 76.1s  FAILED (failures=4, errors=27, skipped=6)
```

Identical counts and identical failing test ids as the unmodified `HEAD` baseline; the 27 errors
are the optional `claude` extra (`claude-agent-sdk==0.2.142`) missing from the worktree venv, two
failures are caused only by running *inside* Claude Code (`CLAUDECODE` in the environment changes
`_detected_source_platform`), and one (`test_issued_grant_prepares_outer_containment_before_worker_spawn`)
is a stale expectation against the intentional `containment = None` for concrete full mode — all
pre-existing and outside this repair. With the SDK present
(`/Users/wangroway/.agentbc-alpha/venv`, Python 3.14) the suite reports the same three
pre-existing failures and no new ones. `ruff check src tests` → clean;
`python -m compileall src tests` → 0; `git diff --check` → clean; `uv build` → wheel + sdist.

## 6. Live Hermes full-mode canary

Environment: real `hermes` CLI (`Hermes Agent v0.20.6`, ACP transport, provider `zai` /
`glm-5.3-flash`), `HERMES_YOLO_MODE=1` as the only full-mode override (registry-frozen,
subprocess-scoped), full permission mode `source=explicit_task`, `max_turns=300`.

### 6.1 Transport-level canary (worktree code, real `hermes acp`)

* official session `509c17d8-6580-4c10-af80-729d3ddf991a`
* turn completed in 39.2s, `stopReason=end_turn`, **no** `session/request_permission` request
* terminal answer captured: 656 bytes, `truncated=false`, exactly **one**
  `AGENTBC_FINAL_CALLBACK` with `final_state=completed` and 3/3 `step_results`
* artifact `hermes-full-canary.txt` = exactly `AGENTBC_HERMES_FULL_OK\n` (23 bytes,
  `sha256 f1436659c663f717f31466e9f1f86291f01336e75e15d5622bf3cc7bac93da58`)

### 6.2 AgentBC task canary `CG8D-001` (worktree worker, real AgentBC finalize path)

* status **completed**, `Final callback: yes`, 3/3 declared steps committed
* `agentbc.final_callback`: `source=executor_final_marker`, `outcome=flow_declared`,
  `exit_code=0`, `marker_valid=true`, `completed_step_count=3`, exactly one marker for the real
  task id `CG8D-001`
* artifacts byte-verified: `hermes-full-canary.txt` 23 bytes exact; verification log
  `hermes-full-canary-log.md` written by the executor into the artifact root
* wall 2m43s / execution 2m04s — a nontrivial multi-minute execution path with 7+ model calls
* RunLease `active` and heartbeated throughout (`last_heartbeat_at` advanced 09:23:25 →
  09:23:37 → … → 09:25:38), then `closed` on completion — never stale, never orphaned
* official session bound from the ACP receipt: `20727e3e-7017-4890-ac5c-37eb0ea03ea4`
* **exact-session cleanup (live)**: `hermes sessions delete 20727e3e-7017-4890-ac5c-37eb0ea03ea4 --yes`
  → `succeeded`, repeated → `succeeded` (already-absent mapping), `retryable=false`,
  `error_code=""`; no private executor database was read or modified
* retry/recovery on the same official session is additionally proven live by `A9D9-001`, whose
  second run resumed `c6982726-390b-4e47-97ee-cf1527e41071` (`resumed=true`) through
  `session/load`

### 6.3 Canary caveats (reported, not weakened)

1. **Deployed build lag.** The two AgentBC-level canaries dispatched through the installed
   `agentbc` Runner (`A9D9-001`) still run the installed 1.0.3a2 build, so they reproduced the
   old defects (`hermes_acp_transport_failed: hermes_acp_invalid_tool_call_id`, and
   `hermes_session_delete_invalid_session_id` for a bound ACP UUID receipt) rather than the fix.
   `CG8D-001` was therefore executed by the worktree worker
   (`python -m agent_bridge_connect.cli worker run`), and the live cleanup was executed by the
   worktree `HermesExecutor`. The repair must be repackaged/deployed before the installed Runner
   can pass the same canary.
2. **One Hermes-side approval request.** ~2 minutes into the `CG8D-001` turn Hermes emitted one
   `session/request_permission` for `item_id=edit-approval-1` (its edit-approval path) even under
   `HERMES_YOLO_MODE=1`. AgentBC bridged it fail-closed (single_action, exact authority and
   offered choices), the recorded decision `{"type":"approve","source":"user"}` came from the
   operator/service through the public respond path, and the turn then completed unattended.
   AgentBC added no dialog, no auto-answer, no retry and no synthetic completion. Whether Hermes
   should ask for edits under YOLO mode is Hermes-side behaviour and out of AgentBC's contract.
3. The pre-existing `test_issued_grant_prepares_outer_containment_before_worker_spawn` failure
   (stale `containment` expectation) is still open and is unrelated to this repair.

## 7. Acceptance

* Both TJBS-001 failures are reproduced and fixed: a silent multi-minute Hermes turn no longer
  times out, and the executor's real terminal answer reaches Core so a `returncode=0` run
  finalizes **only** through one valid `AGENTBC_FINAL_CALLBACK` for the real task and all
  declared steps.
* Timeout, EOF, process exit and oversized frames stay distinct, truthful, classified failures;
  nothing converts a hung transport into success.
* No executor-version allowlist, no fuzzy protocol matching, no repeated approval dialogs, no
  implicit retries and no synthetic completion were added; full mode remains the registry-frozen
  `HERMES_YOLO_MODE` subprocess env with no additional flags.
* The officially bound session identifier is the only identifier cleanup can delete, and the
  live exact-session delete now succeeds for the ACP UUID receipt that previously failed.
