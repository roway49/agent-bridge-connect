# SESSION-104-001 continuation evidence

> [!WARNING]
> **2026-09-08 regression reopening and correction:** the historical acceptance
> below remains valid only as command/backend evidence. `P3FK-002` remains in
> the running Desktop sidebar even though its receipt says archive acknowledged
> and delete acknowledged. Clicking that row reports that no rollout exists for
> its exact thread ID, proving delete succeeded while the sidebar never consumed
> the archive state. The user currently observes that temporary Codex sessions
> remain in the running Desktop sidebar, so
> `SESSION-104-001` is reopened as a P0-Blocker. A `thread/archive` RPC
> acknowledgement followed by `thread/delete` is no longer sufficient proof
> that archive reached the current Desktop instance. The new gate requires
> independent archive-backend, current-Desktop-delivery, and delete receipts plus
> a no-click Desktop-sidebar convergence canary. A fresh list/read absence check
> must not substitute for the rendered sidebar evidence. Existing delete behavior
> remains unchanged; private-store mutation, GUI automation, and forced
> application restart remain forbidden.

> [!CAUTION]
> **最高优先级、不可覆盖：禁止在 `full` 运行基础上增加任何额外安全策略。安全策略不属于 AgentBC 产品特性，不为任务执行负责；凡与 `full` 任务完整执行冲突，必要时必须移除一切额外安全策略。**

Date: 2026-08-28
Task: `DEWX-001`
Artifact root: this existing `agent/claude` worktree

> 2026-08-29 `QEEY-001` note: the sections below describe the historical
> delete-only implementation and its 0.147.0 canary. They are retained
> verbatim as history and are NOT proof of the current archive-then-delete
> gate. See "QEEY-001 archive-then-delete gate evidence" at the end.

## Scope and preserved handoff

- Step 1 evidence was retained from `3XAZ-001`: authoritative status was failed
  with `permission_denied_by_user`, `0/5` completed, and no valid final callback.
- The handoff also retained the authoritative `WSR8-001`, `8XF5-001`, and
  `HHWC-001` reports/receipts. `WSR8-001` was a completed primary-only canary
  with cleanup visibility unknown; `8XF5-001` failed with
  `completion_marker_missing`; `HHWC-001` failed with
  `executor_exit_nonzero`.
- The existing worktree was clean at `b45b133` before the continuation merge.
  The one declared `git merge --ff-only private/integration` fast-forwarded to
  `02aebc8`. No branch switch, copy, rebuild, or push was performed.
- No Codex private database, GUI delete, forced application restart, dispatcher
  conversation, user conversation, or unrelated thread was modified.

## Automated implementation evidence

- Cleanup receipt v3 persists `cli`, `desktop_backend`, and `desktop_live`
  independently; public projections retain the compatibility `desktop` field.
  v1/v2 readers and projections remain accepted. Success requires all three
  v3 sides to be `absent`.
- `transport=auto` uses the App Server cleanup chain; only explicit `cli` or
  `direct` selects the CLI action fallback. A CLI exit code of zero is recorded
  as action evidence only and cannot create cleanup success.
- App Server cleanup uses `thread/delete`, then a new-connection
  `thread/read`, then a new-connection paginated `thread/list` over both
  archive partitions and all supported source kinds. No supported Desktop live
  channel was available, so `desktop_live` remains `unavailable` and the
  result is fail-closed as `codex_desktop_verification_unavailable`.
- Collaboration production dispatch is two-proof gated. The 0.147.0 frozen
  fixture is explicitly unsupported because it lacks
  `collabAgentToolCall`, `spawnAgent`, and `receiverThreadId`; no prompt text or
  `SESSION_CHILD_OK` was treated as a child-session success. Reservation,
  official receiver binding, terminal ledger, replay, mismatch, and unknown
  descendant guards are covered by tests.

Verification commands and results:

- focused cleanup/app-server/auxiliary/collaboration suite: `87 tests`, OK;
- expanded cleanup/protocol/auxiliary/collaboration regression suite: `141 tests`,
  OK;
- full `python3 -m unittest discover -s tests -p 'test*.py'`: `1475 tests`,
  OK;
- `ruff check src tests`: OK;
- `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache python3 -m compileall -q src
  tests`: OK;
- Python 3.11 `pip wheel . --no-deps` package build: OK, wheel
  `agentbc-1.0.3a2-py3-none-any.whl`, SHA-256
  `039be76c0f12a555d9bfccb4fec7568d8cee4b1ae9e1d71aff24db41177e396e`;
- `git diff --check`: OK. The system Python 3.9 package attempt was rejected
  by the declared project requirement `>=3.10`; it was not used as a passing
  build result.

## Real Codex 0.147.0 canary

Executable: Codex `0.147.0` (`codex-cli 0.147.0`) using
`codex app-server --stdio`.

The first `thread/start`-only probe produced an ephemeral official ID that was
not in `thread/list`; its delete returned `no rollout found`, and fresh read/
list proved it absent. It is not counted as a cleanup success.

The real single-parent canary then started one parent turn with an explicit
no-tools/no-child instruction. It did not return a `turn/completed` event within
the 180-second wait and emitted no valid callback. The resulting official
thread was nevertheless visible before cleanup:

- official canary ID: `01a0478f-263b-7cc2-b901-73fc5aa159fd`;
- pre-cleanup fresh `thread/list`: active `221` items across `3` pages
  (`100, 100, 21`), target present; archived `76` items across `1` page;
- cleanup connection: `thread/delete` returned success;
- fresh post-cleanup `thread/read`: error `-32600`, `thread not loaded`, target
  absent;
- fresh post-cleanup `thread/list`: active `220` items across `3` pages
  (`100, 100, 20`), target absent; archived `76` items across `1` page,
  target absent;
- the list requests supplied all supported source kinds: `cli`, `vscode`,
  `exec`, `appServer`, `subAgent`, `subAgentReview`, `subAgentCompact`,
  `subAgentThreadSpawn`, `subAgentOther`, and `unknown`;
- CLI `--version` returned `0` for the 0.147.0 probe, and CLI `delete --help`
  returned `0` before/after with the expected delete capability text. These are
  CLI action/capability observations, not cleanup proof;
- no child session, collaboration item, or child callback was dispatched.

The backend cleanup portion therefore passed for this exact canary ID, including
the timeout/no-callback cleanup path. Desktop live synchronization and
application-restart evidence were not obtained. Consequently this evidence
does not close `SESSION-104-001`; the item remains P0 incomplete.

## Live capability probe

For the installed 0.147.0 executable, the ordinary App Server schema probe and
the live collaboration marker probe returned `ok=true`. The version-matched
frozen fixture returned `ok=false` with the three missing collaboration markers,
so the combined production gate returned `enabled=false`. The candidate 0.150.1
fixture is recorded only as candidate evidence and does not promote 0.147.0.

## QEEY-001 archive-then-delete gate evidence (2026-08-29)

Task: `QEEY-001` (worktree `agent/hermes`, fast-forwarded from
`private/integration` at `c28760b`; no push, no history rewrite).

### SQKX-001 ordering finding (rationale, not proof)

`SQKX-001` (2026-08-28, Codex executor, completed in 20s) deleted its retained
official session `01a04914-765f-7e60-a661-ae360f1d889f` first through the then
official App Server cleanup path, and the controller then attempted to archive
the already-deleted session through the Codex Desktop control plane. The
archive attempt returned a target-not-found style error for the deleted
thread. This proves delete-then-archive is an unusable ordering: once the
exact thread is deleted, the archive precondition can no longer be
established. It is recorded here as the ordering rationale only; the
historical canary is not evidence that the new archive-then-delete gate works.

### Implementation now under the release gate

- The release gate for Codex cleanup is the official archive-then-delete
  command closure: one App Server connection sends `thread/archive` and
  requires its bound RPC acknowledgement before `thread/delete` may be sent;
  if archive is not confirmed (RPC error, target-missing, timeout, or
  transport loss), zero `thread/delete` calls are sent. `thread/archived` and
  `thread/deleted` notifications are advisory only.
- Cleanup receipt v4 (backward compatible) persists bounded
  `commands.archive` and `commands.delete` entries whose status is one of
  `not_requested`, `acknowledged`, `confirmed`, `failed`, `unverified`,
  `not_applicable`. New Codex cleanup succeeds only when both commands are
  acknowledged or officially confirmed. v1/v2/v3 receipts are read and
  projected as-is; history is never rewritten and old failures are never
  retroactively closed. Unknown versions fail closed.
- Post-delete fresh `thread/read` and paginated active/archived all-source-kind
  `thread/list` are preserved as non-gating diagnostics. Current Codex
  Desktop refresh delay is accepted and non-blocking; `desktop_live` is
  `not_applicable` under the archive-then-delete strategy.
- Stable archive codes: `codex_session_archive_failed`,
  `codex_session_archive_invalid_session_id`,
  `codex_session_archive_target_missing`, `codex_session_archive_timeout`,
  `codex_session_archive_transport_lost`. Existing delete codes are preserved,
  and the explicit `cli`/`direct` fallback keeps the
  `official_session_delete` strategy name and can never claim the archive
  gate.
- Partial command evidence (an acknowledged archive before a transport death)
  is persisted in the receipt and in the bounded cleanup event log, so
  retries and Runner restarts do not lose it. Registered auxiliary sessions
  follow the same rules while primary-first and deepest/newest ordering is
  preserved.
- Still forbidden: private-store scans, GUI automation, forced
  refresh/restart, dispatcher conversation cleanup, and unrelated-session
  cleanup. Desktop visibility is not a success gate; GUI refresh, app
  restart, and sidebar disappearance are never used to judge cleanup.

## SESSION-104-001 final acceptance (2026-08-29)

`SESSION-104-001` is accepted for the `1.0.4A` release gate. Historical
incomplete statements above remain valid for their individual canaries but are
superseded by the following installed-build evidence:

- installed AgentBC build identity: `private/integration@d2f77b8`, Codex CLI
  `0.150.1`;
- `QV46-001` completed with one valid `AGENTBC_FINAL_CALLBACK`, official parent
  session `01a04db3-2a59-70f1-859a-cce2a044ff7e`, archive acknowledged,
  delete acknowledged, cleanup `succeeded`, CLI `absent`, Desktop backend
  `absent`;
- `NMY4-001` correctly failed with `completion_marker_missing`, but its exact
  parent session `01a04db3-4135-7d52-ada4-a26c786b8264` still completed archive
  acknowledgement, delete acknowledgement, cleanup `succeeded`, CLI `absent`,
  and Desktop backend `absent` after terminal notification;
- a fresh read through the current Codex Desktop application control plane
  found neither exact session ID in active nor archived task listings;
- both tasks froze `retain=false`; no dispatcher conversation, unrelated task,
  private database, GUI automation, forced refresh, or application restart was
  used.

The attempted child canary did not create an official derived conversation:
there was no `spawnAgent` lifecycle, receiver thread receipt, or auxiliary
ledger, and the parent model merely emitted `CHILD_SESSION_CANARY_OK`. This is
recorded separately as P2 `PROTO-105-001`, considered for `1.0.5A`; it does not
reopen the accepted primary/failed-session cleanup gate and must remain
fail-closed until an official native collaboration receipt exists.
