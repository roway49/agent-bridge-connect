# AgentBC Shared Controller Contract

This is the canonical controller contract shared by Codex, Claude Code, and Hermes. Platform
`SKILL.md` files contain only entrypoint and platform-specific deltas. Follow both this contract and
the platform delta; where prose is duplicated elsewhere, this file is authoritative.

## Source Of Truth

- Use the installed `agentbc` CLI.
- Treat task status, task reports, RunLease health, logs, and artifacts as the source of truth, not a chat recap or an `accepted` transport response.
- Restore a fresh controller session from the exact task report.
- Do not mark a task failed merely because it ran for a long time; inspect RunLease and recovery state.
- Never silently run a requested executor inside the controller sandbox as a fallback.

## Runner Gate

Before dispatching Hermes, run `agentbc runner status` and require a healthy `hermes` executor.
`agentbc setup` normally starts the background Runner. If status explicitly reports a missing Runner
or token, run `agentbc runner start` once and check again. Do not invent another startup command or
use foreground `runner serve`. If startup fails, report `runner_unavailable` and the returned log path.
Path authorization belongs to Runner; controllers must not pre-approve or work around paths.

## Target Executor

Resolve the target executor before writing steps or issuing create/handoff:

| User target | Executor ID |
| --- | --- |
| Hermes | `hermes` |
| Claude or Claude Code | `claude` |
| Codex or ChatGPT Codex | `codex` |

An explicit user choice has priority. Do not reinterpret one executor as another model, select an
executor from task type, or create a mixed identity. If the user requests multiple executors, create
the separate tasks or chain operations they requested. Mechanically verify that the user target,
resolved executor, and final `--assignee` or `--to` value match.

## Permission Modes

AgentBC accepts exactly `inherit`, `safe`, and `full` task permission selectors:

- `inherit` is a selection strategy, not a third runtime access level. It adds no AgentBC permission, sandbox, safe-mode, or yolo override and lets the executor's existing policy resolve at runtime, including its native safe, full, or single-action behavior.
- `safe` is the explicit conservative task base and preserves established executor approval behavior.
- `full` is the explicit audited non-escalatable task base for the installed executor's strongest documented noninteractive access. Warn before selecting it.

First-time setup defaults to `inherit`; an existing configured default is preserved. Legacy tasks
that have no persisted permission extension still fail closed to `safe`.

Use only `--permission-mode <inherit|safe|full>` on `task create` or `task handoff`, and only when
the user chose a task override. Otherwise omit it so a new task uses the configured default and a
handoff inherits its source task. Never pass raw executor permission flags or config/profile/hook
overrides. Legacy tasks fall back to `safe`. Runner canonicalizes long, short, and equals forms and
fails closed on alternate, duplicate, conflicting, or bypass arguments.

Approval eligibility is decided by a trusted runtime permission-block event, not by matching the
task selector to `safe`. An `inherit` or `safe` task may therefore stop with `input_required` when
its executor transport reports a genuinely blocked declared action. Do not synthesize a permission
request merely from the configured selector, ordinary stderr, or executor exit status. A native
single-action transport binds the decision to the exact task, run, session, request, and fingerprint
and resumes that same live session without changing the frozen task base.

Where the current compatibility path explicitly requests the bounded `full` fallback, the input has
`input.type=permission`, `requested_permission=full`, and exactly one blocked declared step. The user
approves or denies through the existing dialog or
`agentbc task respond <task-id> --input <input-id> --approve|--deny`; a one-time `full` grant is
issued for the next same-session continuation only. It is consumed when that run is authorized;
an unused grant is revoked on terminal, recovery, reassignment, or other invalidating paths. A
permission dialog has exactly Approve and Deny, collects no text, defaults to Deny, and automatically
denies on timeout or close. A concrete `full` task base does not ask because no broader AgentBC
permission exists; plain message/choice text and native executor flags never escalate permissions.
AgentBC never treats approval prose as a valid completion marker.

## Steps Contract

Write a temporary YAML file before create. Every requirement is a non-empty `steps[].description`
with concrete behavior, parameters, boundaries, and acceptance evidence. Do not use `action` for new
tasks or rely on a top-level description. Read the sibling one-level reference
`references/agentbc-steps-yaml.md` for the exact format.

## New Task Path Plan

Every new task supplies exactly one `--customer-path`:

- No user path: pass `--customer-path "default path"`.
- Explicit file or directory: pass that exact absolute path. Runner converts an existing file to its parent project directory.
- Existing image with no separate project path: use the exact image path for both `--customer-path` and `--image`.

Never invent a project path from the title, current directory, or guessed project name. Do not use
obsolete `--workspace` or `--output-dir`, and do not decide `--customer-dir` manually. Do not copy a
project or file into AgentBC workspace to bypass a Runner rejection. When Runner rejects a path, stop
and report the exact path error.

User deliverables belong only under the task's project/artifact root. Task/report Markdown belongs in
`workspace/tasks/report`, and compact runtime state belongs in `workspace/record`; neither is a user
deliverable directory. Image generation/editing tasks must require final bitmap files under the
artifact root, not prose or a remote preview alone.

Canonical new-task shape:

```bash
agentbc task create \
  --title "task description" \
  --assignee <target-executor> \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform <controller-platform> \
  --customer-path "default path" \
  --dispatch \
  --config ~/.abc/config.toml
```

After atomic dispatch returns `accepted`, report the exact task ID and return control immediately.
Do not wait for completion. Runner does not open a live-log Terminal by default; use
`agentbc runner show` only when the user asks. Runner IPC uses the AgentBC gateway under `/tmp`; do
not request elevated shell or GUI permission merely to submit a task or open its monitor.

## Status And Exact Task Resolution

Use these public reads:

```bash
agentbc task status
agentbc task status <task-id> --json
agentbc task report <task-id>
agentbc task preflight <task-id>
agentbc task logs <task-id>
agentbc task logs <task-id> --follow
```

Always start lookup with `agentbc task status`. Use `task list` only when status reports ambiguous
active candidates. For fuzzy references such as latest/current/previous/continue, show the resolved
`task_code`, exact iteration ID, title, status, project root, and report, then obtain confirmation
before reading artifacts, editing, creating, handing off, or dispatching. An explicit task ID may be
verified and used directly unless status reports stale, ambiguous, or not-ready chain state.

A `pending` task is queued, not current, unless the user explicitly supplies its task ID. Dispatch an
already-created pending task with `agentbc task dispatch <task-id> --config ~/.abc/config.toml`; never
create a duplicate.

## Continuations, New Roots, And Close

Use `agentbc task handoff` whenever new work depends on, reviews, or modifies an existing task's
deliverables, even if the user did not say “handoff.” Resolve the exact current chain head first.
Handoff inherits task code, date, path plan, project/artifact root, and (unless replaced) image inputs.

```bash
agentbc task handoff <current-head-task-id> \
  --to <target-executor> \
  --message "continuation requirements" \
  --source-platform <controller-platform> \
  --dispatch
```

If Core reports `stale_handoff_source`, use its suggested current head. If it reports
`ambiguous_chain_head`, show the candidate heads and ask the user. Use `--branch` only for an explicit
request to branch from a non-head task.

If the user asks for a new task directory, new version directory, new chain, or copied baseline,
create a new root task instead of handoff. Its requirements must identify the source task ID, report,
artifact/project root, and new output location. Never pass an existing AgentBC-managed artifact
directory to `task create`; a `handoff_required` response must be followed through the current head.

`agentbc task close` accepts only the current queued (pending) or active chain head. Terminal
iterations and stale (non-head) iterations are rejected. Run an interactive close once and let
AgentBC obtain its own confirmation. Use `--confirm` only after explicit authorization.

`agentbc task delete <TASKCODE> --dry-run` is read-only and never prompts. Plain
`agentbc task delete <TASKCODE>` lists the AgentBC-owned task records, briefs/reports, index entries,
and default artifacts that will be removed, then asks `Continue? [y/N]`. Only explicit `y`/`yes`
deletes; Enter, `n`, EOF, or interrupt cancels without writes. Customer projects are always
preserved. There is no public `task delete --confirm` mode.

## Configuration And Health

Use the configured values for future executor runs; do not invent executor-native budget or turn
flags in task commands:

```bash
agentbc claude budget <usd>
agentbc hermes max-turns <turns>
agentbc session retention status
agentbc session retention enable
agentbc session retention disable
```

Executor temporary-session cleanup runs in the background after eligible terminal tasks. It manages
only temporary sessions created by the Executor, never the dispatcher conversation that created or
handed off the task.

`agentbc record clean` removes eligible terminal-task runtime diagnostics only; `task.json`, the
indexes, and all reports are preserved. Reports are never deleted by record cleanup.

Check installation and Runner health with `agentbc doctor` (add `--json` for the stable
machine-readable contract). The exit code contract is fixed: `0` = healthy, `1` = warning,
`2` = unavailable.

## Dispatcher Traceability

Always pass the platform-specific `--source-platform`. Pass `--session-id` only when the current
controller exposes a trusted conversation ID or the user explicitly supplies one. Otherwise omit it;
the report records `unavailable`. Never infer an ID from processes, paths, shell history, a prior
task, or an executor temporary session. Dispatcher sessions and executor temporary sessions are
independent; cleanup warnings do not change task/report terminal state. AgentBC never deletes the
dispatcher conversation.

## Progress, Completion, Intervention, And Acceptance

For long-running work, refresh progress at least every few minutes:

```bash
agentbc task progress <task-id> --root <board-root> --summary "short progress update"
```

An executor completing structured work must end its final response with exactly one valid
`AGENTBC_FINAL_CALLBACK` marker for the actual task and every declared step exactly once. A zero exit
or optional `agentbc task callback` is not a substitute for that flow marker and is not user quality
acceptance.

Intervene only after checking status:

```bash
agentbc task pause <task-id> --reason "..."
agentbc task resume <task-id>
agentbc task cancel <task-id> --confirm
agentbc task correct <task-id> --step <step> --message "..."
agentbc task retry <task-id> --step <step>
agentbc task reassign <task-id> --to <target-executor>
agentbc task recover <task-id>
```

For context-free acceptance, read status and report, verify objectives, artifacts, tests,
interventions, risks, and RunLease evidence, then state accepted, needs correction, blocked, or needs
human review. Record corrections and recovery through AgentBC.
