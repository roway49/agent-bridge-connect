# Changelog

## 1.0.1A3 - 2026-08-08

- Removed host-installed Codex, Claude, and Hermes dependencies from the release-check test suite.
- Replaced non-portable executor test paths with isolated interpreter or temporary fixture paths.
- Standardized executor probe diagnostics with stable binary path and discovery-source fields.
- Preserved explicit macOS system and application candidates as platform adapters rather than user-local assumptions.

## 1.0.1A2 - 2026-08-08

> Stability revision published from the 1.0.1A cutoff. The original
> `v1.0.1A` tag, GitHub Release, and PyPI `1.0.1a1` artifacts remain immutable.

### Added

- A strict, versioned `AGENTBC_FINAL_CALLBACK` flow contract. A task reaches
  `completed` only when exactly one valid marker names the running task and
  declares every configured step exactly once as `done`; a zero exit code no
  longer implies completion by itself.
- A resumable `input_required` lifecycle with persisted input IDs, blocked-step
  evidence, typed message/permission/choice requests, suspended RunLease state,
  and same-task continuation after `agentbc task respond`.
- Audited `inherit`, `safe`, and `full` permission modes across task creation,
  handoff, setup, Runner authorization, reports, and all three supported
  executors. Dangerous native argument aliases and conflicting overrides fail
  closed.
- Dispatcher traceability in both task briefs and final reports. AgentBC records
  the dispatcher platform and a trusted current conversation ID when available;
  handoffs do not inherit the previous iteration's dispatcher conversation.
- Read-only `agentbc doctor` and `agentbc doctor --json` diagnostics for package,
  installation source, configuration, workspace, Runner availability, and
  CLI/Runner identity drift.
- Reproducible release-provenance checks, pinned Gate tooling, and regression
  coverage for clean builds, wheel/sdist metadata, checksums, and guarded
  publication.

### Changed

- `input_required` is now an active, recoverable waiting state instead of a
  terminal result. Completed step evidence is retained, the blocked step is
  reset on response, and user-waiting time is excluded from execution time.
- macOS input notifications now show the concrete reason and two described
  choices directly. Choosing `Later` or allowing the five-minute dialog to time
  out dismisses only the dialog and leaves the request waiting for later
  takeover; internal deadline and CLI response details are no longer shown in
  the choice dialog.
- Reports now expose flow-marker validity, completed-step count, failure code,
  failed or blocked steps, permission selection, dispatcher traceability,
  execution/waiting duration, and RunLease recovery guidance.
- Task List health remains yellow while a task is waiting for input or has not
  produced progress within the observation window, and returns to green on a
  later valid heartbeat.
- Runner finalization now treats executor callbacks as staged evidence until
  process exit and reconciles completion, cancellation, recovery, and late
  callback races before writing the terminal report.

### Fixed

- Packaged build identity now uses the same integer schema contract as
  `agentbc doctor`, so release provenance is recognized after installation.
- The standalone GitHub Release installer now resolves the product version
  from the release URL when the source-tree provenance helper is unavailable.
- Prevented a live worker from being marked failed during the short interval
  between executor exit and Core finalization.
- Isolated test and uninstall Runner spools so skip-runner and fallback cleanup
  cannot stop or remove the user's active Runner state.
- Classified Hermes iteration-budget exhaustion consistently and exposed
  bounded diagnostics without imposing an AgentBC-specific turn limit.
- Prevented Hermes prompt examples from being counted as real terminal markers.
  Validation now uses the assistant response after the CLI
  `Initializing agent...` boundary, including when warnings and terminal line
  wrapping precede it, while genuine duplicate markers in the response still
  fail.
- Kept legacy `input_required` records safe and actionable without generating
  blank response commands or converting a dismissed dialog into completion or
  failure.
- Preserved terminal task state when bounded record compaction, notification
  delivery, or late executor output occurs after finalization.

### Validation

- `542` automated tests pass in the current candidate, including strict flow,
  permission, input takeover, notification, RunLease race, Runner spool,
  release provenance, and Hermes real-output regression suites.
- The candidate passes Ruff, compileall, shell syntax, Twine, clean-wheel smoke,
  and the MacBook x86_64 Gate before installation on the ARM64 development
  machine.
- Real executor canaries verified Codex/Claude/Hermes dispatch, strict Hermes
  completion, and concurrent Claude/Hermes `input_required` suspension. User
  takeover and final deliverable quality remain separate acceptance steps.

## 1.0.1A - Public Alpha

- Local-first task coordination for Codex, Claude Code, and Hermes through one
  authenticated Runner gateway.
- Four-character task codes with explicit chain iterations and context-free
  status, report, and artifact lookup.
- Atomic task creation and dispatch with preserved controller and executor
  identity.
- Customer-project and isolated managed-artifact path modes with task-scoped
  write roots.
- Cross-agent handoff for continuing work on an existing task chain.
- Native image input for Codex and Hermes, including image-generation and
  image-editing deliverables.
- Concurrent task visibility through the automatically managed Task List,
  low-frequency health classification, and compact macOS notifications.
- Executor-lifecycle terminal states covering completed execution, recovery,
  and unconfirmed termination.
- Readable task briefs and reports separated from bounded runtime records and a
  compact global task index.
- Explicit task close, recovery, reassignment, record cleanup, Runner
  lifecycle, setup, and uninstall commands.
- Automatic integration installation for Codex, Claude Code, and every
  detected Hermes profile.
- MIT-licensed source distribution and checksum-verified macOS Alpha bundle.
