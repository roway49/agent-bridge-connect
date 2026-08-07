# User Guide

[中文](USER_GUIDE_ZH.md) | English

## Command Surface

Use `agentbc <group> <command> --help` for the exact options installed by your
version.

- `agentbc setup`: discover executors and install local integrations.
- `agentbc uninstall`: remove AgentBC with separate managed-data choices.
- `agentbc init`: initialize the managed runtime record directory.
- `agentbc record clean`: clean eligible runtime diagnostics.
- `agentbc task`: create, inspect, hand off, intervene, close, and recover.
- `agentbc worker`: run task-board worker operations.
- `agentbc runner`: start, stop, inspect, sample, and show Runner work.

Setup starts Runner in the background. Use `runner start` for recovery,
`runner stop` for shutdown, and foreground `runner serve` only for debugging.

## Executors

AgentBC Public Alpha supports Codex, Claude Code, and Hermes. Setup discovers
configured executables, the active `PATH`, user-owned install locations, and
supported editor-extension runtimes:

```bash
agentbc setup
agentbc setup --show
agentbc runner status
```

Agent integrations pass dispatcher identity explicitly:

```bash
agentbc task create ... --source-platform codex --dispatch
agentbc task handoff 4XMC --to hermes --source-platform claude --dispatch
```

Direct terminal use may omit the flag and is recorded as `cli`. Restart agent
clients after setup so they reload installed skills. Executor-specific model
selection is not part of the stable Alpha contract.

## Create Versus Handoff

Use create for independent work:

```bash
agentbc task create \
  --title "Add CSV export" \
  --assignee codex \
  --steps ./steps.yaml \
  --customer-path /path/to/project \
  --dispatch
```

Use handoff whenever work depends on, reviews, or modifies existing AgentBC
deliverables. Passing a managed artifact directory back to create is rejected
with `handoff_required` and a suggested current-head command.

## Image Tasks

Codex accepts multiple native image inputs; Hermes currently accepts one image
per task iteration. Use the image itself as `--customer-path` when no separate
project path was supplied:

```bash
agentbc task create \
  --title "Analyze and revise this design" \
  --assignee codex \
  --steps ./steps.yaml \
  --customer-path /absolute/path/design.png \
  --image /absolute/path/design.png \
  --dispatch
```

Repeat `--image` for Codex multi-image work. Handoff inherits the prior image
references unless replacement `--image` values are supplied. Image generation
and editing use the executor's native capability; final bitmap files belong in
the task artifact root. Provider authentication remains owned by the executor.

## Status And Reports

```bash
agentbc task status 4XMC
agentbc task report 4XMC
agentbc task logs 4XMC
```

- `completed`: a valid final marker declared every task step `done`; quality is not asserted.
- `needs_recovery`: an explicit retryable transport or infrastructure failure stopped execution.
- `failed`: the final marker or another non-retryable execution contract was missing or invalid.

Every successful Codex, Claude, or Hermes run must end its final response with
exactly one version-1 marker on a single line:

```text
AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"4XMC-001","final_state":"completed","summary":"Completed the task","step_results":[{"id":1,"status":"done"}]}
```

The task ID must match. Every declared task step must appear exactly once and be
`done` for `completed`. A valid `input_required` marker must name at least one
declared `blocked` step. Zero exit, invalid JSON, incomplete step data, or plain
permission/approval text does not imply success. Explicit retryable transport or
infrastructure failures may become `needs_recovery`, but AgentBC never dispatches
a retry automatically. Flow validation does not inspect Git, tests, files, or
artifact quality.

For a two-option decision, include
`"input":{"type":"choice","options":["Option A","Option B"]}` in the
`input_required` marker. AgentBC renders both labels as direct desktop buttons;
free-text requests continue to use `type: message`, and approve/deny requests use
`type: permission`. Input dialogs wait up to five minutes, and dismissal or timeout
keeps the same task waiting for `agentbc task respond`.

Reports show marker validity, completed-step count, failure code, failed/blocked
steps, and `Flow contract satisfied`. That field is `yes` only for a valid
completed marker with all task steps done after the report is generated.

Every report and task brief also carries a `Dispatcher Traceability` section with
two labels: `Dispatcher platform` and `Dispatcher conversation ID`. They describe
the controller conversation that created or handed off the task, not the executor's
temporary session. `Dispatcher platform` is the source platform such as `codex`,
`claude`, or `hermes`. `Dispatcher conversation ID` is the trusted conversation ID
from that dispatcher when one is available; it shows `unavailable` otherwise.
AgentBC records a handoff's current dispatcher conversation, never the source task
conversation, and never guesses a conversation ID from processes, paths, history,
or a previous task. Dispatcher traceability is separate from executor temporary
sessions, and AgentBC does not delete the dispatcher conversation.

## Task List And Health

```bash
agentbc task list
agentbc runner show
```

Task List tracks the current dispatch cohort. Its timer only proves that the
display is refreshing; it does not poll executor state.

- green: recent progress evidence;
- yellow: at least five minutes without progress while Runner is healthy;
- orange: at least ten minutes without progress while Runner is healthy;
- red: recovery condition or terminal failure;
- gray: queued and waiting to start.

Unresponsive tasks are observed, not automatically cancelled, because forced
termination may leave a user project partially modified.

## Intervention

```bash
agentbc task pause 4XMC
agentbc task resume 4XMC
agentbc task close 4XMC
agentbc task recover 4XMC
```

Close applies only to a queued or active current head. Root-task close releases its code
and removes AgentBC-owned files. Later chain iterations preserve prior history
and warn that project changes cannot be rolled back. Customer-project files are
never deleted.

## Records And Cache

```bash
agentbc record clean --dry-run
agentbc record clean
agentbc runner process-sample
```

Record cleaning preserves the global index, authoritative state, readable
reports, and deliverables. Choose concurrency according to the executor workload
and machine.

## Troubleshooting

### Command not found

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v agentbc
```

### Runner unavailable

```bash
agentbc runner start
agentbc runner status
```

Inspect `~/.abc/runner/runner.log` if status still times out.

### Executor or skill not discovered

Verify the executor's own CLI and authentication, rerun setup, then start a new
agent session. Hermes skills do not necessarily appear as slash commands.

### Customer path rejected

Pass the user-named file or directory directly. Do not inspect Runner allowed
roots or copy the project into AgentBC workspace. If Core returns
`handoff_required`, continue the existing chain.

### Task is yellow or orange

Check logs and executor UI for network, quota, permission, or long-reasoning
conditions. Health returns to green when progress resumes.

### Deliverable exists but task is failed

`failed` describes the exit contract, not whether a file was produced. Review
the report, logs, and deliverables before deciding whether to recover or hand
off the work.

See the [README architecture section](../README.md#architecture) for data
ownership and internal boundaries.
