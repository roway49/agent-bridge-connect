---
name: agentbc
description: "AgentBC task orchestration: dispatch, inspect, intervene, and hand off tasks across Hermes, Claude, and Codex executors"
---

# AgentBC Hermes 入口

执行前必须完整阅读以下两个一级引用：

- [共享 Controller 契约](references/controller-contract.md)
- [Steps YAML 格式](references/agentbc-steps-yaml.md)

共享契约是唯一权威规则；本文件只保留 Hermes 平台差异。

## Hermes 平台差异

- 每个 `task create` 与 `task handoff` 命令都必须使用 `--source-platform hermes`。
- 用户未指定执行者时默认 `target_executor=hermes`；若用户明确指定 Claude 或 Codex，必须逐字映射为 `claude` 或 `codex`，禁止根据任务类型改派。
- 只有当前 Hermes 控制端暴露可信会话 ID（用户明确提供或可信 `HERMES_SESSION_ID`）时才传 `--session-id`。否则省略，报告显示 `unavailable`；禁止从进程、路径、历史或旧任务猜测。
- Hermes CLI 每轮当前只接收一个 `--image`；Codex 支持重复传入。
- 派发到 Hermes 前，必须按共享契约先检查 `agentbc runner status`。
- Executor 临时会话 cleanup 与派发者 runtime 相互独立，绝不删除控制端会话。

规范命令形状：

```bash
agentbc task create --title "任务描述" --assignee <target-executor> \
  --steps /tmp/agentbc-steps.yaml --source-platform hermes \
  --customer-path "default path" --dispatch --config ~/.abc/config.toml

agentbc task handoff <confirmed-task-id> --to <target-executor> \
  --message "延续任务要求" --source-platform hermes --dispatch
```

派发返回 `accepted` 后立即报告准确任务 ID 并结束当前回复，不等待 executor 完成。
