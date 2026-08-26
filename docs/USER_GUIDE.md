# User Guide

[中文](USER_GUIDE_ZH.md) | English

Applies to the AgentBC **1.0.3A** release (tag `v1.0.3A2`, Python package
`1.0.3a2`).

## Command Surface

Use `agentbc <group> <command> --help` for the exact options installed by your
version.

- `agentbc setup`: discover executors and install local integrations.
- `agentbc doctor`: read-only installation and Runner health check.
- `agentbc uninstall`: remove AgentBC with separate managed-data choices.
- `agentbc update`: check the Alpha channel and route to the supported upgrade path.
- `agentbc init`: initialize the managed runtime record directory.
- `agentbc claude budget`: configure the Claude budget for future runs.
- `agentbc hermes max-turns`: configure the Hermes turn limit for future runs.
- `agentbc session retention`: inspect or change executor temporary-session retention.
- `agentbc record clean`: clean eligible runtime diagnostics.
- `agentbc task`: create, inspect, hand off, intervene, close, delete, and recover.
- `agentbc worker`: run task-board worker operations.
- `agentbc runner`: start, stop, inspect, sample, and show Runner work.

Setup starts Runner in the background. Use `runner start` for recovery,
`runner stop` for shutdown, and foreground `runner serve` only for debugging.

### Command Added In 1.0.3A

| Command | Purpose and boundary |
| --- | --- |
| `agentbc update` | Check the verified Alpha release manifest. It prompts only when a newer version exists and the running CLI is an AgentBC-managed installation. |
| `agentbc update --root <RECORD_ROOT> --config <CONFIG>` | Use an explicit runtime record root or config for a controlled environment. Normal installations should omit both options. |

The updater never treats Homebrew, pip/pipx, an external symlink, or a copied
binary as an AgentBC-managed in-place target. Package-manager installations are
routed to their package manager instead of being overwritten.

### Commands Added Or Changed In 1.0.2A

| Command | Purpose and boundary |
| --- | --- |
| `agentbc setup --show` | Read-only discovery and effective-setting view. It never refreshes files or starts a migration. |
| `agentbc claude budget <usd>` | Set a positive finite USD limit for future Claude task snapshots. Existing tasks keep their frozen value. |
| `agentbc hermes max-turns <turns>` | Set a positive integer turn limit for future Hermes task snapshots. Existing tasks keep their frozen value. |
| `agentbc session retention status` | Read the effective executor temporary-session retention setting. |
| `agentbc session retention enable` | Keep executor temporary sessions after terminal tasks. It never affects dispatcher conversations. |
| `agentbc session retention disable` | Request background official session cleanup after eligible terminal tasks; active, waiting, and recovery tasks retain their session. |
| `agentbc task respond <TASK-ID> --input <INPUT-ID> --approve` | Approve the current resource or permission decision and resume the same task/session under its validated one-time policy. |
| `agentbc task respond <TASK-ID> --input <INPUT-ID> --deny` | Deny the current resource or permission decision; the resulting stable failure reason depends on the input type. |
| `agentbc task delete <TASKCODE> --dry-run` | List AgentBC-owned records, reports, index entries, and default artifacts without prompting or writing. |
| `agentbc task delete <TASKCODE>` | Show the same deletion plan and require interactive `y/N`; customer projects are always preserved. |
| `agentbc doctor [--json]` | Run the read-only Doctor v2 checks. Exit codes are `0` healthy, `1` warning, and `2` unavailable. |

These commands modify configuration or task state only through AgentBC's
validated contracts. Do not substitute executor-native budget, permission,
session, or deletion flags in an AgentBC task command.

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

## Updating AgentBC

Check the current installation through the supported entry point:

```bash
agentbc doctor --json
agentbc update
```

For an AgentBC-managed Alpha installation, `agentbc update` verifies the GitHub
release index, release manifest, tag, wheel filename, and SHA-256 before it can
change the active installation. If the installed version is current, it exits
without prompting or writing. If a newer version is available, it shows the
bounded release summary and asks:

```text
Upgrade? [y/N]
```

Only `y` or `yes` starts the update. `n`, Enter, EOF, or any other response
returns `update_declined` without modifying the CLI, Skills, Runner, config, or
task board. Before cutover, AgentBC also rejects unresolved legacy permission
records and invalid current CLI/Skill/Runner identity.

An approved update stages and verifies the target wheel, snapshots the existing
managed CLI and Skills, stops the old Runner, switches the CLI, refreshes Skills,
starts the target Runner, and requires all target identities to match. If any
post-switch gate fails, AgentBC restores the exact previous CLI link and managed
Skill files, removes newly introduced managed paths, and restarts the previous
Runner. `update_rollback_incomplete` requires inspection with
`agentbc doctor --json` before another update attempt.

For a Homebrew installation, `agentbc update` does not prompt or write update
state. It returns `homebrew_update_required` with the command:

```bash
brew update
brew upgrade agentbc
agentbc doctor --json
```

Unmanaged binaries, ordinary pip/pipx installations, non-symlink targets, and
links outside the AgentBC-managed Alpha root are refused with
`update_install_unsupported`; reinstall through the intended installation source
rather than replacing them in place.

## Executor Resources And Session Retention

Configure the defaults used for future executor runs:

```bash
agentbc claude budget 50
agentbc hermes max-turns 150
agentbc session retention status
agentbc session retention enable
agentbc session retention disable
```

The Claude value is a positive finite USD amount. Hermes accepts a positive
integer. Claude and Hermes must already be configured by `agentbc setup`; those
two commands otherwise return `not_configured` with exit code 2. Session
retention can be configured independently. Successful commands emit stable JSON
with the previous value, new value, whether the file changed, and
`"scope": "future_executor_runs"`. Repeating the current value is safe and does
not rewrite the config.

Interactive setup offers default or custom values and preserves an existing
value when Enter is pressed. Non-interactive setup also preserves existing
values. New settings use these defaults:

- AgentBC task permission: `inherit` (preserve the executor's user/global settings);
- Claude budget: `$10`;
- Hermes turns: `agent.max_turns` from the path returned by
  `hermes config path`, then legacy top-level `max_turns`, then `90`;
- executor session retention: `false`.

They are stored in AgentBC config as:

```toml
[executors.claude]
max_budget_usd = 50.0

[executors.hermes]
max_turns = 150

[sessions]
retain_executor_sessions = true
```

Phase 2 task-contract status: every new Claude or Hermes task freezes its
effective resource limit in `agentbc.resources`, and every new Claude, Hermes,
or Codex task freezes retention and executor-session metadata in
`agentbc.session`. Missing settings use `$10`, `90`, and retention `false`;
present invalid settings fail closed. Handoff creates a new snapshot for its
target executor, reassign rebuilds the snapshot, and resume, retry, recover, or
re-dispatch of the same task keeps the original snapshot.

Accepted create/dispatch output, preflight, status, report, and task briefs use
one path-free `execution_policy` view. It shows the effective limit, source, and
frozen state (or `null` resources for Codex), plus retention, executor session
ID/state, and project mode. Executor-only project paths remain inside the task
packet and are not listed as artifacts. Ephemeral Claude projects use the
canonical managed path `<TASK-ID>/claude`; Runner validates legacy backfill and
the worker packet against the durable snapshot. The same public policy includes a
bounded cleanup projection containing only capability, state, attempts, stable
error code, and retryability. Doctor uses the authoritative receipt: unsupported,
failed, and cleanup pending for more than five minutes are warnings; retained and
succeeded are healthy.

Cleanup is background and user-transparent. It manages only temporary sessions
created by the Executor, never deletes the dispatcher conversation that created or
handed off a task, and never requires users to manage a separate runtime directory.
Active, `input_required`, and recovery tasks keep their session; cleanup failure or
an unsupported capability does not change the terminal task/report result.

## Permission Modes

Every task carries one of three canonical permission modes — `inherit`, `safe`, or `full`:

- `inherit` adds no AgentBC permission, approval, sandbox, or yolo override and keeps the executor's existing user/global settings;
- `safe` is the conservative task override and preserves established executor approval behavior;
- `full` is an explicit audited choice for the installed executor's strongest documented noninteractive access.

On first setup, Enter selects `inherit`. Existing configured values are preserved when Enter is
pressed. Tasks missing a permission snapshot from an older AgentBC version still fall back to
`safe`; changing the new-install default never broadens legacy tasks.

Pass `--permission-mode <inherit|safe|full>` on `task create` or `task handoff` only when the user
chose a task override; otherwise a new task uses the configured default and a handoff inherits its
source task. Never pass raw executor permission flags (`--yolo`, `--dangerously-skip-permissions`,
bypass flags, sandbox or config overrides) in AgentBC commands.

A `safe` task that reaches a step genuinely requiring `full` stops with `input_required` and a
`type: permission` input declaring `requested_permission=full` plus the blocked step. Approve or
deny through the dialog or the fallback:

```bash
agentbc task respond 4XMC-001 --input INPUT_ID --approve
agentbc task respond 4XMC-001 --input INPUT_ID --deny
```

Approving issues a one-time `full` grant for the next same-session continuation of that task only;
it is consumed or revoked afterward and is never inherited by retry, recover, reassign, handoff, or
a new task. Denying ends the task `failed` with the stable reason `permission_denied_by_user`.
The permission dialog has exactly Approve and Deny, no Later action or text field, defaults to
Deny, and automatically denies on timeout or close; timeout uses the stable reason
`permission_denied_by_timeout`. Plain message text or approval prose is not a permission grant and
never a completion marker.

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

For a two-option decision, include a concrete reason and describe the outcome of
both options: `"input":{"type":"choice","reason":"why the user must decide","options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B","description":"what B does or changes"}]}`.
AgentBC shows the reason and descriptions above two direct option buttons. The
deadline and CLI fallback command remain available in the task report but are not
shown in the desktop dialog. Free-text requests continue to use `type: message`,
approve/deny requests use `type: permission`, and input dialogs wait up to five
minutes. Dismissal or timeout keeps the same task waiting for `agentbc task respond`.

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
agentbc task delete 4XMC --dry-run
agentbc task delete 4XMC
agentbc task recover 4XMC
```

Close applies only to the current queued (pending) or active chain head. Terminal iterations
(completed, failed, cancelled, rejected) and stale non-head iterations are rejected. Root-task close
releases its code and removes AgentBC-owned files. Later chain iterations preserve prior history
and warn that project changes cannot be rolled back. Customer-project files are never deleted.

Delete accepts a task code, never an iteration ID. It requires every iteration in
the chain to be `completed`, `failed`, `cancelled`, or `rejected`; queued, active,
input-required, and recovery-required chains are rejected as a whole. `--dry-run`
makes no writes, never prompts, and lists both deleted and preserved objects. Plain
`task delete` first lists the task records, briefs/reports, task index entries, and
default AgentBC artifacts that will be removed, then asks `Continue? [y/N]`. Only an
explicit `y`/`yes` commits deletion. EOF, Ctrl-C, Enter, or `n` cancels without writes.
Customer projects are always preserved.

## Records And Cache

```bash
agentbc record clean --dry-run
agentbc record clean
agentbc runner process-sample
```

Record cleaning removes only eligible terminal-task runtime diagnostics (events,
interventions, run leases, and run logs of terminal tasks). It always preserves the global index,
authoritative `task.json` state, readable reports, and deliverables; reports are never deleted by
record cleanup. Choose concurrency according to the executor workload and machine.

## Doctor

```bash
agentbc doctor
agentbc doctor --json
```

`doctor` is a read-only health check of the installation: package/build identity, configuration,
Runner identity and spool, storage permissions, installed Skill manifests, executor discovery, and
session-cleanup receipts. The exit code contract is fixed: `0` = healthy, `1` = warning (for
example a Skill drift or a cleanup receipt in warning state), `2` = unavailable (for example a
missing Runner or config). `--json` emits the same structured diagnostics as the text view.

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
