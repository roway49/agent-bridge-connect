---
name: agentbc
description: Use AgentBC for agent-to-agent dispatch, status, intervention, recovery, reports, and context-free acceptance.
---

# AgentBC

Use the installed `agentbc` CLI. Do not use the optional `abc` alias unless setup confirms it is AgentBC-owned.

## Operating Rules

- Treat AgentBC task state and reports as the source of truth, not chat messages.
- Restore context from the task report in a fresh session.
- Before dispatching Hermes, run `agentbc runner status` and require a healthy `hermes` executor. `agentbc setup` starts the Runner automatically; if status explicitly reports a missing Runner or token, run `agentbc runner start` once and check again. Do not invent another startup command or use foreground `runner serve`. Report the returned log path when startup fails. Do not decide path permission yourself; path authorization is Runner-owned.
- If Runner is unavailable, report `runner_unavailable` and stop. Never silently run Hermes directly inside the current agent sandbox.
- Do not mark a task failed only because it exceeded a wall-clock duration; inspect its RunLease and recovery state.

## Execution Permission Modes

AgentBC has exactly three task permission modes: `inherit`, `safe`, and `full`.
`inherit` adds no AgentBC permission, approval, sandbox, safe-mode, or yolo override and uses the executor's existing user/global settings. `safe` is the conservative default and preserves the established executor behavior. `full` is an explicit, audited task authorization for the installed executor's strongest documented noninteractive access; warn the user before selecting it.

Use the single `--permission-mode <inherit|safe|full>` option on `task create` or `task handoff` only when the user chose a task override. Otherwise omit it so a handoff inherits its source task and a new task uses the configured default. Never pass raw executor permission flags. Configure the noninteractive default with `agentbc setup --non-interactive --permission-mode safe` (or `inherit`/`full` after an explicit choice). Legacy tasks remain `safe`.

Runner canonicalizes documented long, short, and equals forms before authorization. Alternate native spellings such as Codex `-s danger-full-access`, duplicate/conflicting flags, raw config/settings/profile overrides, hook-trust bypass, and Hermes `--accept-hooks`/`--oneshot` never substitute for the persisted mapping and fail closed.

## Dispatch Hermes

Write a temporary steps YAML file, then atomically create and dispatch the task. Before every new task, set exactly one path field:

- If the user did not supply a project directory/path, pass `--customer-path "default path"`.
- If the user supplied any explicit file or directory path, pass that exact absolute path with `--customer-path`. An existing file path is valid; Runner converts it to its parent project directory.
- If the user supplied an image, pass its exact absolute path with repeatable `--image`. The image path also counts as an explicit customer path; use it as `--customer-path` when no separate project path was supplied. Never copy the image into AgentBC workspace.
- Do not pre-judge path permission, copy the project into AgentBC workspace, or switch paths yourself. Runner derives the internal `customer_dir` value and authorizes or rejects the path.

Shortest no-user-project command:

```bash
agentbc task create \
  --title "task description" \
  --assignee hermes \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform codex \
  --customer-path "default path" \
  --dispatch \
  --config ~/.abc/config.toml
```

Native image-input dispatch:

```bash
agentbc task create \
  --title "analyze or edit the supplied image" \
  --assignee codex \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform codex \
  --customer-path /absolute/path/input.png \
  --image /absolute/path/input.png \
  --dispatch \
  --config ~/.abc/config.toml
```

Codex accepts repeated `--image` inputs. Hermes currently accepts one image per task iteration. For generation or editing, require final bitmap files under the task artifact root; a prose-only answer or remote preview URL is not a finished deliverable.

Explicit user-project command:

```bash
agentbc task create \
  --title "task description" \
  --assignee hermes \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform codex \
  --customer-path /absolute/user/project \
  --dispatch \
  --config ~/.abc/config.toml
```

Each YAML step must use a non-empty `steps[].description` containing concrete,
independently executable requirements and acceptance evidence. Do not use
`action` for new tasks or rely on a top-level YAML `description`; AgentBC only
treats the step list as the task requirements contract.

Always pass `--source-platform codex`; Agent UI subprocesses do not consistently export `CODEX_THREAD_ID`.
Pass `--session-id` only when the current Codex dispatcher exposes a trusted conversation ID, such as an ID the user explicitly provides or a `CODEX_THREAD_ID` verified set in this session. Omit `--session-id` when no trusted ID is available; never guess one from running processes, paths, shell history, or a previous task's report. A handoff records the current dispatcher conversation, not the source task conversation.
After atomic dispatch returns `accepted`, report the task ID and return control to the user immediately.
Do not wait for completion. Runner does not open a live-log Terminal by default; use `agentbc runner show` only when the user asks to inspect live execution. The background worker sends a compact desktop dialog when the final report is ready.

Runner IPC uses the AgentBC gateway under `/tmp`; invoke the command normally. Do not request elevated shell or GUI permissions just to submit a task or open the Runner-managed monitor.

Never invent a project path from the task title, the current directory, or a guessed project name. `--workspace`, `--output-dir`, and manual `--customer-dir` decisions are obsolete for task create/handoff; use only `--customer-path`.

Use `agentbc task handoff` whenever the requested work depends on, reviews, or modifies deliverables from an existing AgentBC task, even when the user does not say the word "handoff". An instruction such as "modify the previous output" is continuation work, not a new root task. Resolve the exact current chain head first; fuzzy references still require user confirmation. Handoff inherits the existing task code, task date, path plan, and project/artifact root while creating the next iteration.

`agentbc task close` accepts the current queued or active chain head. A queued task may be closed when its worker never starts; terminal tasks remain ineligible.

Never pass an existing AgentBC managed artifact directory as `--customer-path` to `task create`. Core rejects that path with `handoff_required`; use the suggested current-head handoff command instead.

If the user asks for a "new task directory", "new version directory", "new chain", "copy the existing artifacts into a new task", or similar wording, do not use `handoff`. Create a new root task with `agentbc task create`, a new task code, and an explicit `--customer-path` value. The task requirements must name the source `task_id`, source `report_file`, and source artifact/project root, then explain where the new deliverables should be written.

For a new-root request, do not rewrite it as any `agentbc task handoff`, especially not a Codex handoff unless the user explicitly asked for Codex.

If Runner rejects a project path, stop and report the Runner path error. Do not retry by changing `--customer-path`, adding `--customer-dir`, creating a duplicate task, copying the project/file into the AgentBC workspace, or moving deliverables to another directory.

For `--customer-path "default path"`, the task-scoped managed artifact directory is the only deliverable root. Never place deliverables directly in `~/Documents/AgentBC/workspace`; `tasks/report` and `record` are Core-owned.

## Status And Report

```bash
agentbc task status
agentbc <task-id>
agentbc task status <task-id> --json
agentbc task report <task-id>
agentbc task preflight <task-id>
agentbc task logs <task-id>
agentbc task logs <task-id> --follow
```

Always start task lookup with `agentbc task status`. Only when AgentBC reports an ambiguous current task may you use `agentbc task list` to ask the user for confirmation. Do not iterate through historical reports just to infer which task is current.

When the user says "latest task", "current task", "previous task", "continue that task", or similar fuzzy language, first run `agentbc task status` and show the resolved task summary (`task_code`, exact iteration id, title, status, project root, report) to the user for confirmation. This is a hard stop: do not read artifacts, edit files, create tasks, hand off, or dispatch until the user either confirms that summary or provides an explicit task ID. If the user already gave an explicit task code or exact task ID, verify it with `agentbc task status <task-id>` and proceed unless AgentBC reports stale, ambiguous, or not-ready chain state.

A `pending` task is queued, not current. Never treat it as the current task unless the user explicitly supplies its task ID; inspect queued tasks only when the user asks for them.

To submit an already-created pending task without creating a duplicate, use `agentbc task dispatch <task-id> --config ~/.abc/config.toml`.

AgentBC stores compact runtime state under `~/Documents/AgentBC/workspace/record`.
Readable task/report files use `workspace/tasks/report`; default managed
deliverables use `workspace/tasks/artifacts`. Do not write user deliverables to
`record`. Its generated `README.md` explains the directory, and
`agentbc record clean` removes terminal-task runtime diagnostics while
preserving the core index and task status. Do not clean a task that is still
waiting for input or recovery.

For long-running AgentBC work, refresh task progress at least every few minutes so `task list` can show whether the task is responsive:

```bash
agentbc task progress <task-id> --root <board-root> --summary "short progress update"
```

Successful AgentBC execution requires the final response to end with exactly one
single-line versioned flow marker. A zero CLI exit is not completion by itself:

```text
AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"4XMC-001","final_state":"completed","summary":"short completion summary","step_results":[{"id":1,"status":"done"}]}
```

Use the exact running task ID and list every declared task step exactly once as
`done` for `completed`. Missing/invalid JSON, mismatched IDs, or missing,
duplicate, unknown, or non-done steps fail the task. Use `input_required` only
with at least one declared step marked `blocked`; permission or approval prose
without that valid marker fails. Explicit retryable transport/infrastructure
failures may remain `needs_recovery` and are never auto-retried. Core validates
the flow declaration only, not Git state, tests, files, artifact quality, or user
acceptance.

The `agentbc task callback` command remains compatibility metadata and does not
replace the mandatory terminal marker.

## Dispatcher Traceability

Reports and task briefs label the controller that created or handed off the task:
`Dispatcher platform` and `Dispatcher conversation ID`. These labels describe the
current dispatcher conversation, not the source task conversation and not the
executor's temporary session.

- Always pass the correct `--source-platform` value for the current controller: `codex` for Codex,
  `claude` for Claude Code, `hermes` for Hermes.
- Pass `--session-id` only when the current dispatcher exposes a trusted conversation ID, such as the
  user-supplied `CODEX_THREAD_ID` when Codex verifies it, or an ID the user explicitly provides.
  Omit `--session-id` when no trusted ID is available; the report then shows `unavailable`.
- Never guess a conversation ID from processes, paths, history, or a previous task. A handoff records
  the current dispatcher conversation, not the source task conversation.
- Dispatcher traceability is separate from executor temporary sessions. AgentBC does not delete the
  dispatcher conversation, and it does not retain, budget, or clean executor temporary sessions here.

## Intervention

```bash
agentbc task pause <task-id> --reason "..."
agentbc task resume <task-id>
agentbc task cancel <task-id> --confirm
agentbc task close <active-task-id>
agentbc task correct <task-id> --step <step> --message "..."
agentbc task retry <task-id> --step <step>
agentbc task reassign <task-id> --to <agent>
agentbc task recover <task-id>
```

Check status before intervention. Do not create duplicate work while a task is running, leased, stale, or awaiting recovery.
`task close` only accepts the current active chain head. For a later chain
iteration, run the command once and let AgentBC ask for `y/n` confirmation in
the same process. Do not restate the warning or ask the user to rerun a second
command. Reserve `--confirm` for explicitly approved non-interactive automation.

## Cross-Agent Handoff And Acceptance

```bash
agentbc task handoff <task-id> --to <target-agent> --message "continue or verify the task" --source-platform codex --dispatch
```

Pass `--source-platform codex` on handoff too. Pass `--session-id` only when the
current Codex dispatcher exposes a trusted conversation ID; a handoff records the
current dispatcher conversation, not the source task conversation. Omit
`--session-id` when unavailable and never fabricate one.

`agentbc task handoff <task-id>` only continues from the current chain head. If
AgentBC reports `stale_handoff_source`, use the suggested current head task ID
from the error/details and retry the handoff from that task. Do not create a new
task, scan historical reports, or guess a different baseline.

If AgentBC reports `ambiguous_chain_head`, show the candidate heads to the user
and ask for confirmation instead of picking one. Use `--branch` only when the
user explicitly asks to create an intentional branch from a non-head task.

For acceptance in a new session:

1. Read status and report using the task ID.
2. Verify the objective, artifacts, tests, interventions, risks, and RunLease evidence.
3. State accepted, needs correction, blocked, or needs human review.
4. Record corrections or recovery through AgentBC.
