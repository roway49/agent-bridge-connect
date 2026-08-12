---
name: agentbc
description: "Use AgentBC to dispatch, monitor, recover, report, and hand off work between Claude Code, Codex, and Hermes"
---

# AgentBC Claude Entry Point

Before acting, read these two one-level references completely:

- [Shared controller contract](references/controller-contract.md)
- [Steps YAML format](references/agentbc-steps-yaml.md)

The shared controller contract is authoritative. This file contains only Claude-specific deltas.

## Claude Deltas

- When the user names another executor, Claude is the controller, not the executor. Do not edit files, generate artifacts, or complete that dispatched work inline.
- Use `--source-platform claude` on every `task create` and `task handoff` command.
- Pass `--session-id` only when this Claude controller exposes a trusted ID, such as a user-provided ID or trusted `CLAUDE_SESSION_ID`. Otherwise omit it; the report records `unavailable`. Never infer it from processes, paths, history, or an earlier task.
- Claude Code remains an L1 executor. `bypassPermissions` is only an executor detail selected by AgentBC for an explicitly persisted `full` task; never pass native permission flags in controller commands.
- Executor temporary-session cleanup is separate from the dispatcher runtime and never deletes the controller conversation.
- Configuration (`claude budget`, `hermes max-turns`, `session retention`), `doctor` exit codes 0/1/2, `record clean`, and queued-head `task close` semantics all follow the shared controller contract.
- Write the steps file as YAML with a top-level `steps:` list. Do not use `.txt`; a shell heredoc such as `cat > /tmp/agentbc-steps.yaml` is acceptable when the environment permits it.

Canonical command shapes:

```bash
agentbc task create --title "task description" --assignee <target-executor> \
  --steps /tmp/agentbc-steps.yaml --source-platform claude \
  --customer-path "default path" --dispatch --config ~/.abc/config.toml

agentbc task handoff <confirmed-task-id> --to <target-executor> \
  --message "continuation requirements" --source-platform claude --dispatch
```

Use persisted AgentBC configuration as the contract; do not inspect AgentBC source code or CLI help to invent unsupported behavior during dispatch.
