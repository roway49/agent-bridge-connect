# SESSION-104-001 continuation evidence

Date: 2026-08-28
Task: `DEWX-001`
Artifact root: this existing `agent/claude` worktree

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
