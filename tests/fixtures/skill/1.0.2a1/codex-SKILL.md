---
name: agentbc
description: Use AgentBC for agent-to-agent dispatch, status, intervention, recovery, reports, and context-free acceptance.
---

# AgentBC Codex Entry Point

Before acting, read these two one-level references completely:

- [Shared controller contract](references/controller-contract.md)
- [Steps YAML format](references/agentbc-steps-yaml.md)

The shared controller contract is authoritative. This file contains only Codex-specific deltas.

## Codex Deltas

- Use `--source-platform codex` on every `task create` and `task handoff` command.
- Pass `--session-id` only when this Codex controller exposes a trusted conversation ID, such as a user-provided ID or `CODEX_THREAD_ID` verified in this session. Otherwise omit it; the report records `unavailable`. Never infer it from processes, paths, history, or an earlier task.
- Codex accepts repeated `--image` arguments. Hermes currently accepts one image per iteration.
- If the requested target executor is Hermes, verify `agentbc runner status` first as required by the shared contract.
- Executor temporary-session cleanup is separate from the dispatcher runtime and never deletes the controller conversation.
- Configuration (`claude budget`, `hermes max-turns`, `session retention`), `doctor` exit codes 0/1/2, `record clean`, and queued-head `task close` semantics all follow the shared controller contract.
- After a dispatched create/handoff returns `accepted`, report the exact task ID and return control immediately.

Canonical command shapes:

```bash
agentbc task create --title "task description" --assignee <target-executor> \
  --steps /tmp/agentbc-steps.yaml --source-platform codex \
  --customer-path "default path" --dispatch --config ~/.abc/config.toml

agentbc task handoff <confirmed-task-id> --to <target-executor> \
  --message "continuation requirements" --source-platform codex --dispatch
```

Do not use the optional `abc` alias unless setup confirms it is AgentBC-owned.
