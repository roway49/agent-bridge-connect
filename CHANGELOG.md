# Changelog

## 1.0.3A - 2026-08-26

> Development and release validation are complete for AgentBC `1.0.3A`.

### Added

- Unified permission registry and capability mapping for Codex, Claude, and
  Hermes while preserving `inherit` as the default behavior.
- Structured single-action approval receipts, bounded permission details, and
  official early-session handshakes across the three supported executors.
- Production-gated Codex app-server approval transport for the verified
  `0.146.0` and `0.147.0` protocol surfaces, with the existing one-time full
  continuation retained as a compatibility fallback.
- Stable permission/session failure taxonomy carried inside the compatible
  `permission_resume_session_unavailable` envelope.
- Verified Alpha-channel updates through `agentbc update`, including release
  manifest, tag, wheel digest, installation identity, and legacy-cutover gates.
- A public Homebrew Formula and universal bottle for both Apple Silicon and
  Intel macOS, with package-managed upgrades kept separate from self-update.
- Receipt-driven executor-session teardown and a task/run-scoped auxiliary
  session ledger with exact official cleanup, bounded retries, redacted public
  views, and stable Doctor/report diagnostics.

### Changed

- Permission dialogs use a minimal summary with separately bounded, redacted
  details; close and timeout continue to deny.
- Legacy permission cutover and supported-update preflight remain fail closed
  when old-channel tasks require recovery.
- `agentbc update` checks before prompting. Current versions and `n`, Enter, or
  EOF responses perform no local mutation; only explicit `y` or `yes` starts a
  verified managed-install transaction.
- Homebrew-owned installations return `brew upgrade agentbc` without prompting,
  writing a cutover stamp, or replacing files outside Homebrew.
- Alpha release tags and Python package versions use one validated serial
  mapping across Update, release manifests, and Homebrew Formula generation.

### Fixed

- Managed updates stage the target wheel before switching and verify the new
  CLI, managed Skills, and Runner as one identity after cutover.
- Failed managed updates restore the exact previous CLI link, managed Skill
  bytes and manifests, remove newly introduced managed paths, and restart the
  previous Runner; incomplete recovery fails closed.
- Update ownership checks reject unmanaged, pip/pipx-style, and external CLI
  links instead of overwriting them in place.
- Successful, failed, denied, timed-out, and transport-lost terminal paths
  schedule idempotent cleanup of official executor sessions when retention is
  disabled; registered auxiliary sessions follow the same task/run boundary.

### Validation

- The final source gate passed 153 focused release/session tests and 1386 full
  tests, plus Ruff, compileall, shell and Ruby syntax, `git diff --check`,
  wheel/sdist build, Twine validation, manifest hashes, and isolated install.
- Apple Silicon and Intel hosts passed Homebrew install, upgrade, uninstall,
  PATH coexistence, service lifecycle, and package-manager Update routing.
- Managed Update passed latest/no/EOF zero-write routes, integrity failures,
  successful bootstrap-to-final upgrade, identity cutover, and automatic
  restoration after injected Runner startup failure.
- Real Codex terminal canaries verified both failure-path and successful-path
  official session cleanup with retention disabled, one cleanup attempt, a
  closed RunLease, and healthy Doctor diagnostics.

## 1.0.2A - 2026-08-14

> Development is closed for Python package `1.0.2a1`. Publication remains
> gated on the immutable `v1.0.2A` tag, release-matrix validation, and final
> artifact checks.

### Added

- Persistent Claude budget and Hermes turn-limit configuration through
  `agentbc claude budget <usd>` and `agentbc hermes max-turns <turns>`.
- Executor temporary-session retention controls through
  `agentbc session retention status|enable|disable`, with task-level frozen
  session policy and background terminal cleanup.
- Same-session continuation for Claude, Hermes, and Codex using exact official
  executor session IDs; ambiguous recent-session recovery is rejected.
- Resource-exhaustion decisions that let users approve a task-local doubled
  budget/turn limit or deny continuation with a stable terminal reason.
- One-time `safe` to `full` permission continuation through the existing
  approve/deny input flow. The base task permission remains unchanged, and the
  grant cannot leak into retry, recovery, handoff, reassignment, or a new task.
- Doctor v2 diagnostics with one text/JSON data source and fixed exit codes:
  `0` healthy, `1` warning, and `2` unavailable.
- Versioned Skill manifests and a shared controller contract used by Codex,
  Claude, and Hermes installations.

### Changed

- First-time setup now defaults task permission to `inherit`, Claude budget to
  `$10`, Hermes turns to the discovered Hermes value (or `90`), and executor
  session retention to `false`.
- Resource and session settings are frozen into each task. Updating global
  configuration affects future executor runs only.
- Permission dialogs have exactly Approve and Deny. Close or timeout denies the
  request; free text cannot grant permission.
- `agentbc task delete <TASKCODE>` now shows AgentBC-owned records, reports,
  index entries, and default artifacts before an internal `y/N` confirmation.
  `--dry-run` remains read-only, and customer projects are never deleted.
- `agentbc record clean` removes eligible terminal runtime diagnostics only;
  authoritative task state and reports are always preserved.

### Fixed

- Doctor storage checks now use a scoped Runner-side probe, avoiding false
  `unavailable` results when the controller itself runs in a safe sandbox.
- Hermes native `Reached maximum iterations` output now takes precedence over
  an ordinary model choice callback, so the resource approve/deny flow opens
  reliably and resumed runs receive the doubled `--max-turns` value.
- Permission reasons are bounded before persistence so an oversized model
  explanation cannot suppress the approval dialog.
- Cancellation now closes waiting inputs, RunLeases, and executor-session state
  before terminal cleanup eligibility is evaluated.
- Claude managed-project cleanup preserves normal artifacts while continuing
  to fail closed on unexpected files, path drift, symlinks, or unsafe removal.

### Validation

- Development cutoff: `1007` source tests and `172` final affected regressions
  passed with Ruff, compileall, and `git diff --check`.
- Release candidate: `1010` source tests pass on Python 3.10, 3.11, and 3.14.
  The Intel MacBook gate also passes Ruff, compileall, shell syntax, Twine,
  wheel/sdist manifest validation, clean installation, upgrade from `1.0.1a3`,
  rollback to `1.0.1a3`, and package smoke.
- Real Codex, Claude, and Hermes canaries verified one-time permission
  continuation, exact-session resume, and terminal cleanup with retention off.
- Real Codex, Claude, and Hermes canaries verified `retain=true` terminal
  receipts: official session IDs remained recorded, cleanup resolved as
  `retained` with zero attempts, and Doctor remained healthy.
- Real Claude and Hermes canaries verified repeated resource exhaustion,
  task-local doubling, same-session continuation, and explicit user denial.

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
