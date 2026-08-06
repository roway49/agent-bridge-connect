---
name: agentbc
description: "Use AgentBC to dispatch, monitor, recover, report, and hand off work between Claude Code, Codex, and Hermes"
---

# AgentBC Claude Controller Skill

Use AgentBC when the user asks Claude to dispatch, continue, monitor, recover, or report agent work.

## Hard Rules

- Start task lookup with `agentbc task status`; do not infer the active task from directory names, titles, or report history.
- If the user says "latest task", "current task", "previous task", "continue that task", "继续最新任务", "当前任务", or similar fuzzy references, show the `agentbc task status` summary and ask the user to confirm the exact `task_id` before dispatching or editing.
- If the user asks to "hand off", "dispatch", "交给 Codex/Hermes/Claude", "让 Codex/Hermes/Claude 执行/验收/接手", or otherwise names another executor, Claude is the controller, not the executor. Do not edit files, generate artifacts, or complete the requested work inline. Use AgentBC CLI create/handoff with `--dispatch`.
- If `agentbc task status --json` returns `resolution_mode=ambiguous`, use `agentbc task list --current` or a compact `agentbc task list`; do not traverse old reports to guess.
- `agentbc task handoff <id> --to <agent>` may continue only from the current chain head. If AgentBC returns `stale_handoff_source`, use the suggested head task after user confirmation; do not create a replacement task.
- Any request that depends on, reviews, or modifies deliverables from an existing AgentBC task is a handoff, even if the user does not say "handoff". Resolve and confirm the exact current chain head before dispatch. Never use `task create` with an existing managed artifact directory as `--customer-path`; Core returns `handoff_required` for that misuse.
- Use `--branch` only when the user explicitly requests an intentional branch.
- Never pass Claude `bypassPermissions`, `--dangerously-skip-permissions`, or `--allow-dangerously-skip-permissions` as raw dispatch flags. AgentBC may translate an explicitly persisted `--permission-mode full` task authorization after Runner validation.

## Execution Permission Modes

AgentBC supports exactly `inherit`, `safe`, and `full`. `inherit` leaves the executor's user/global permission settings untouched. `safe` is the conservative default and preserves Claude `--safe-mode` plus `acceptEdits`. `full` grants the installed executor's maximum documented noninteractive access and must be explicitly chosen and audited. Use only `--permission-mode <inherit|safe|full>` on `task create` and `task handoff`; never inject executor-native flags. A handoff inherits its source mode unless this option is present. Legacy tasks remain `safe`. The default can be set noninteractively with `agentbc setup --non-interactive --permission-mode safe` without making `full` implicit.

Runner compares canonical permission semantics across long and equals forms and rejects duplicate, conflicting, raw settings/config, safe-mode, or alternate bypass arguments. Claude diagnostics distinguish installed full capability from the requested/effective/source values of the current persisted task; static executor defaults are not task authorization.

## Dispatch Flow

1. Run `agentbc runner status` before dispatching. The Runner must be `ready` and list the target executor. `agentbc setup` starts it automatically; if status explicitly reports a missing Runner or token, run `agentbc runner start` once and check status again. Do not invent another startup command or use foreground `runner serve`. If startup fails, report the returned `log` path. Do not decide path permission yourself; path authorization is Runner-owned.
2. Determine the target executor from the user's words. Use `codex` for "Codex", `hermes` for "Hermes", and `claude` only when the user explicitly wants Claude to execute it.
3. For a new task, provide only `--customer-path`: use `"default path"` only when the user supplied no file or directory path. Otherwise pass the exact absolute user path. Existing file paths are valid and Runner converts them to the parent project directory. Do not set `--customer-dir`; Runner derives it.
3a. For image input, add the exact absolute image path with repeatable `--image`. If no separate project path exists, use that image path as `--customer-path`. Codex accepts multiple images; Hermes currently accepts one per iteration. Never copy an image into AgentBC workspace to dispatch it.
4. For a new task, write a YAML steps file with a top-level `steps:` list and at least one `description`. Do not use `.txt`, top-level `description`, or `action` for new tasks.
5. For a new task, use `agentbc task create --assignee <target-executor> --steps <steps-yaml> --source-platform claude --customer-path <path-or-default-path> --dispatch`. Pass `--session-id` only when the current Claude dispatcher exposes a trusted conversation ID (for example the `CLAUDE_SESSION_ID` environment variable when it is present and trusted, or an ID the user explicitly provides). Omit `--session-id` when no trusted ID is available; never guess it from processes, paths, history, or a previous task.
6. For continuation, use `agentbc task handoff <confirmed-task-id> --to <target-executor> --source-platform claude --dispatch`. A handoff records the current dispatcher conversation, not the source task conversation. Pass `--session-id` only when the current Claude dispatcher exposes a trusted conversation ID; otherwise omit it.
7. If Bash is denied by Claude Code auto mode, do not wait for a separate approval notification and do not inspect AgentBC source code or CLI help. Report that Claude Code's permission classifier blocked the dispatch command, then show the exact command for the user to run or ask the user to retry in a permission mode that allows `agentbc`.
8. If Runner returns a path rejection, stop and report the Runner path problem. Do not copy the project or file into the AgentBC workspace, add `--customer-dir`, change `--customer-path`, or create a duplicate task to bypass the rejection.
9. When AgentBC returns `accepted`, report the created task id, assignee, report path, and project/artifact root if shown, then stop. Do not wait for completion or keep reasoning about the task.
10. If the user asks for a new task directory, create a new root task instead of handoff. A handoff intentionally reuses the task code path plan and project/artifact root while creating the next iteration.
11. Track progress with `agentbc task status` and `agentbc task report <task_id>`.
12. For `--customer-path "default path"`, write deliverables only to the task-scoped managed artifact directory. Never write deliverables directly in `~/Documents/AgentBC/workspace`; `tasks/report` and `record` are Core-owned.

AgentBC keeps compact runtime state under `~/Documents/AgentBC/workspace/record`.
Readable task/report files use `workspace/tasks/report`; default managed
deliverables use `workspace/tasks/artifacts`. User deliverables never belong in
`record`. Its generated `README.md` explains the files. `agentbc record clean`
removes runtime diagnostics only for finished tasks while preserving the core
index and `task.json` state. Do not clean tasks that still require input or
recovery.

`agentbc task close` only accepts the current queued or active chain head. For a later
chain iteration, run it once and let AgentBC ask for `y/n` confirmation in the
same process. Do not restate the warning or ask the user to rerun a second
command. Reserve `--confirm` for explicitly approved non-interactive automation.

## Shortest New-Task Recipe

Use this exact shape for new tasks. Replace title, executor, and the single description.

```bash
cat > /tmp/agentbc-steps.yaml <<'YAML'
steps:
  - description: "Concrete task requirements, expected artifacts, and verification evidence."
YAML

agentbc task create \
  --title "Short task title" \
  --assignee codex \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform claude \
  --customer-path "default path" \
  --dispatch \
  --config ~/.abc/config.toml
```

If the user explicitly supplied `/absolute/user/project`, replace the default path value with:

```bash
  --customer-path /absolute/user/project \
```

The steps file contract is only the YAML `steps[].description` list above.

For a native image task, add this command shape:

```bash
  --customer-path /absolute/path/input.png \
  --image /absolute/path/input.png \
```

Image generation and editing requirements must name final bitmap files under the task artifact root as acceptance evidence.

## Dispatcher Traceability

Reports and task briefs label the controller that created or handed off the task:
`Dispatcher platform` and `Dispatcher conversation ID`. These labels describe the
current dispatcher conversation, not the source task conversation and not the
executor's temporary session.

- Always pass the correct `--source-platform` value for the current controller: `claude` for Claude
  Code, `codex` for Codex, `hermes` for Hermes.
- Pass `--session-id` only when the current Claude dispatcher exposes a trusted conversation ID, such
  as the `CLAUDE_SESSION_ID` environment variable when it is present and trusted, or an ID the user
  explicitly provides. Omit `--session-id` when no trusted ID is available; the report then shows
  `unavailable`.
- Never guess a conversation ID from processes, paths, history, or a previous task. A handoff records
  the current dispatcher conversation, not the source task conversation.
- Dispatcher traceability is separate from executor temporary sessions. AgentBC does not delete the
  dispatcher conversation, and it does not retain, budget, or clean executor temporary sessions here.

## Final Callback Contract

AgentBC Core owns task finalization. A Claude executor run may attach a concise
summary callback before its final response, but executor exit remains the
authoritative completion signal:

For long-running work, refresh progress at least every few minutes:

```bash
agentbc task progress <task-id> --root <board-root> --summary "short progress update"
```

```bash
agentbc task callback <task-id> --root <board-root> --state completed --summary "short completion summary"
```

AgentBC derives terminal state from the Claude CLI lifecycle. The callback above
is optional summary metadata and must not trigger a permission request. A normal
CLI exit means execution completed; it does not mean the user accepted the
result.
