# SESSION-104-001 baseline

Captured before source edits on 2026-09-08.

## Historical failure/control evidence

- `28KY-001` is the retain=true control case for official Executor session
  `01a08036-c2a8-7a12-a365-127baec881cb`. The supplied task baseline records
  that the current Codex Desktop `set_thread_archived` control plane made the
  session disappear immediately and that the session was then deleted
  successfully. Its AgentBC cleanup receipt correctly records retain behavior
  (`state=retained`, `commands.archive=not_requested`,
  `commands.delete=not_requested`) because those manual control-plane actions
  are outside automatic retain cleanup.
- `ED84-001` is the retain=false automatic-cleanup failure for official
  Executor session `01a08037-0ff7-7061-95ee-36f4627c2b98`. Its report and
  `record/ED84/001/cleanup.jsonl` record `codex_session_archive_failed`, with
  archive failed/pending and delete `not_requested`; no delete request was
  made. The supplied baseline records that the session remained visible.
- Read sources: the two historical task briefs and reports under
  `Documents/AgentBC/workspace/tasks/report/2026-09-08/{28KY,ED84}`, plus the
  corresponding `task.json`, `events.jsonl`, `delivery.jsonl` where present,
  and `cleanup.jsonl` records under `Documents/AgentBC/workspace/record`.
  No private Codex database, process list, temporary-directory socket scan,
  title match, dispatcher session, or unrelated session was inspected.

## Repository baseline

- Worktree: `/Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/codex`
- Branch: `agent/codex`
- Pre-edit status: clean (`git status --short --branch` reported only the
  branch line).
- HEAD: `5b752ea52510e64ab5313ea8cbd8a8d5a6c8a985`
- `private/integration`: `4fb5614d6569ee3b84730c87f96a6c2c353aa4b7`

## Implementation and automated evidence

- Added the in-memory `CodexDesktopArchiveBroker` route. It accepts only the
  current-process Desktop environment envelope, binds same-host dispatcher
  context, negotiates `tools/list`, requires `set_thread_archived`, redacts
  all public route evidence to bounded digests, and invalidates stale routes.
- Codex retain=false cleanup now requires the exact receipt-bound Executor UUID
  to receive Desktop archive acknowledgement before the unchanged App Server
  delete/read/list verification chain can issue one delete. Missing, rejected,
  timed-out, mismatched, dead, or restarted routes leave cleanup pending or
  retryable with zero delete calls. retain=true remains untouched.
- Cleanup receipts are backward-compatible v5. New records independently
  record `desktop_archive`, legacy/diagnostic `app_server_archive`, and
  `delete`; the public `archive` field projects Desktop acknowledgement only.
  v1-v4 input remains readable and v4 App Server acknowledgement is never
  inferred as Desktop acknowledgement. Status, report, doctor, Runner replay,
  and CLI status distinguish the Desktop waiting/acknowledgement/delete phases.
- Focused proof: `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest tests.test_session104_desktop_archive tests.test_session_cleanup_codex_v2 tests.test_auxiliary_sessions.CoordinatorAuxiliaryTestCase tests.test_e2e_session_supervisor -v` — 63 tests passed.
- Full proof: `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest discover -s tests -v` — 1870 tests passed, 17 skipped.
- Static/package proof: `ruff check src tests`; `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m compileall -q src tests`; `git diff --check`; `sh -n scripts/session104_desktop_archive_canary.sh`; executable canary `--help`; `uv build --wheel` — all passed, including `dist/agentbc-1.0.3a2-py3-none-any.whl`.
- `scripts/session104_desktop_archive_canary.sh` is the executable real-machine
  procedure for completed, failed, one-permission-continuation,
  registered parent/auxiliary, and Desktop-offline terminal tasks. It asks the
  user to reopen Desktop and run the final status compensation command; it
  never enumerates sessions or reads private Desktop storage.

## Acceptance boundary

Automated proof covers route registration/capability negotiation, exact-ID
archive gating, zero-delete-before-ack, one-delete-after-ack, retry/replay,
redaction, receipt compatibility, terminal delivery isolation, and package
integrity. Final acceptance still requires the user-visible Codex Desktop
sidebar confirmation that the exact canary Executor sessions disappear after
the acknowledged archive route; sidebar refresh is intentionally not used as
the command-level cleanup gate.
