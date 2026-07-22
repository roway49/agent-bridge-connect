---
name: agentbc
description: "AgentBC task orchestration: dispatch, inspect, intervene, and hand off tasks across Hermes, Claude, and Codex executors"
---

# AgentBC Skill

使用 AgentBC 派发、查看、干预或验收任务时，以任务板和任务报告为唯一事实源，不依赖当前聊天历史。

## 执行前检查

Hermes 任务必须通过 AgentBC Runner 执行：

```bash
agentbc runner status
```

`agentbc setup` 会自动启动后台 Runner。若 status 明确返回 Runner 未启动或 token
不存在，只允许执行一次 `agentbc runner start` 后重新检查 status；不要建议不存在的命令，
不要使用前台 `runner serve` 代替产品启动流程。启动失败时报告返回的 `log` 路径。

步骤文件统一使用非空的 `steps[].description`，每一项都应包含可独立执行和验收的具体要求：

```yaml
steps:
  - id: 1
    description: "实现明确的功能要求、参数和边界条件"
  - id: 2
    description: "验证产物并记录可复现的测试证据"
```

不要使用 `action` 代替 `description`。AgentBC Core 仍兼容历史 `action` 文件，但新任务必须
使用规范字段。不要依赖 steps 文件中的顶层 `description`；任务要求必须逐项写入
`steps[].description`，禁止提交空白或只有泛化标题的步骤。

若 Runner 不可用或健康信息中没有 `hermes` executor，停止派发并明确报告
`runner_unavailable`。禁止静默降级为当前 Agent 沙盒内直接执行 Hermes CLI。
不要判断用户工程路径是否可用；路径权限是 Runner 底层防呆逻辑，派发 agent 只负责传入
`--customer-path`。

## 派发任务

### 执行者路由

在编写 steps 或执行任何 `task create` / `task handoff` 命令前，先从用户当前请求中解析
`target_executor`。三个 executor 是互斥的独立运行时，不是模型别名：

| 用户指定 | `target_executor` | CLI 字段 |
| --- | --- | --- |
| Hermes | `hermes` | `--assignee hermes` / `--to hermes` |
| Claude、Claude Code | `claude` | `--assignee claude` / `--to claude` |
| Codex、ChatGPT Codex | `codex` | `--assignee codex` / `--to codex` |

- 用户明确指定执行者时，该选择拥有最高优先级，必须逐字映射到上表中的 executor ID。
- 用户没有指定执行者时，默认 `target_executor=hermes`，因为当前控制会话是 Hermes。
- `--source-platform hermes` 只表示派发者，不能用来推断或覆盖执行者。
- 禁止把 Claude 理解为 Codex 使用的模型，禁止输出或创建 `Codex (Claude)`、
  `Claude via Codex` 等混合身份。
- 禁止根据任务类型、是否包含图片、模型能力、命令示例、历史 assignee 或上一轮执行者改变
  用户明确指定的目标。
- 用户同时指定多个执行者时，按用户要求分别创建任务或链路；不要任选一个，也不要把多个名字
  合并成一个 executor。

执行命令前必须做一次机械自检：用户点名的执行者、`target_executor` 和最终
`--assignee` / `--to` 三者必须完全一致。若不一致，先修正命令，禁止派发。

显式目标的最小参数形状：

```bash
# 用户指定 Claude
--assignee claude --source-platform hermes

# 用户指定 Codex
--assignee codex --source-platform hermes

# 用户未指定执行者，或明确指定 Hermes
--assignee hermes --source-platform hermes
```

先将步骤写入临时 YAML 文件，再原子创建并派发任务。每次新建任务前只填写一个路径字段：

- 用户没有给出任何文件或目录路径：`--customer-path "default path"`。
- 用户明确给出了文件或目录路径：必须把原始绝对路径作为 `--customer-path`。现有文件路径是合法输入，由 Runner 自动归一到其父目录。
- 用户给出了图片：使用可重复的 `--image` 传入原始绝对路径。若没有单独工程路径，该图片路径本身就是 `--customer-path`；禁止复制图片到 AgentBC workspace。
- 不要提前判断路径许可，不要复制工程到 AgentBC workspace。Runner 会根据
  `customer_path` 派生内部 `customer_dir` 并决定放行或拒绝。

没有用户工程路径时：

```bash
agentbc task create \
  --title "任务描述" \
  --assignee hermes \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform hermes \
  --customer-path "default path" \
  --dispatch \
  --config ~/.abc/config.toml
```

原生图片输入派发：

```bash
agentbc task create \
  --title "分析或编辑输入图片" \
  --assignee hermes \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform hermes \
  --customer-path /absolute/path/input.png \
  --image /absolute/path/input.png \
  --dispatch \
  --config ~/.abc/config.toml
```

Hermes CLI 当前每轮任务接收一张输入图片；Codex 可重复传入多张。生图或改图任务必须把最终位图文件保存到当前任务 artifact root，只有文字说明或远程预览链接不算完成产物。

用户明确给出工程路径时：

```bash
agentbc task create \
  --title "任务描述" \
  --assignee hermes \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform hermes \
  --customer-path /absolute/user/project \
  --dispatch \
  --config ~/.abc/config.toml
```

禁止根据任务标题、当前目录或猜测的项目名自行创建工程路径。`--workspace` 和 `--output-dir`
已废弃；新任务只传 `--customer-path`，不要让派发 agent 自行填写 `--customer-dir`。

执行者必须遵循上面的“执行者路由”规则。只有用户没有指定执行者时才使用
`--assignee hermes`；用户明确指定 Claude 或 Codex 时，必须分别使用 `--assignee claude`
或 `--assignee codex`。

只要新需求依赖、复核或修改既有 AgentBC 任务的产物，即使用户没有说出
`handoff` 也必须使用 `agentbc task handoff`。“在上一个产物基础上改”属于链路延续，
不是新 root task。先解析准确的 current chain head；模糊的“上一个”仍需用户确认。
Handoff 继承原有 task code、task date、路径计划和 project/artifact root。

`agentbc task close` 可关闭当前排队中或活跃的 chain head。worker 未能启动时，pending
任务也允许关闭；终态任务仍不可 close。

禁止把已有 AgentBC managed artifact 目录作为 `task create --customer-path`。Core 会返回
`handoff_required`，应使用其提供的 current-head handoff 命令。

如果用户要求“新开任务目录 / 新版任务目录 / 新 chain / 复制原产物到新目录 / 不在原目录改”，
禁止使用 `handoff`。必须创建新的 root task：先用 `agentbc task status` 确认源任务，再用
`agentbc task create --assignee hermes --customer-path <path-or-default-path> --dispatch` 创建新任务。步骤要求中必须写清楚源
`task_id`、源 `report_file`、源 artifact/project root，并明确新产物写入哪里。不要在确认前直接读取、编辑或移动源 artifacts。

新目录任务模板：

```bash
agentbc task create \
  --title "基于 <source-task-id> 复制产物并创建新版任务" \
  --assignee hermes \
  --steps /tmp/agentbc-steps.yaml \
  --source-platform hermes \
  --customer-path "default path" \
  --dispatch \
  --config ~/.abc/config.toml
```

不要把这种需求改写成任何 `agentbc task handoff`，尤其不要改派给 Codex。

若 Runner 返回路径拒绝，必须停止并报告 Runner 路径错误。禁止通过修改
`--customer-path`、添加 `--customer-dir`、创建重复任务、复制工程/文件到 AgentBC
workspace，或把产物迁移到其他目录来绕过拒绝。

使用 `--customer-path "default path"` 时，当前任务的 managed artifact 目录是唯一产物根。
禁止把产物直接写到 `~/Documents/AgentBC/workspace`；`tasks/report` 和 `record` 由 Core 管理。

返回 `accepted` 后立即将任务 ID 告知用户并结束当前回复，禁止等待执行完成。
Runner 默认不打开独立 Terminal；用户要求查看执行窗口时使用 `agentbc runner show`。后台 worker 会在报告生成后弹出简洁桌面通知。

Runner IPC 使用 `/tmp` 下的 AgentBC gateway。正常执行命令即可；仅为提交任务或打开由
Runner 管理的 monitor 时，禁止主动申请额外 shell 或 GUI 权限。

长时间执行时，Hermes 必须至少每几分钟刷新一次 AgentBC progress，让 `task list` 能显示任务是否仍有响应：

```bash
agentbc task progress <task-id> --root <board-root> --summary "简短进度说明"
```

AgentBC 根据 Hermes CLI 的退出事件确定终态，不依赖 Hermes 主动通知。下面的
callback 仅用于可选的简短摘要，不得因此向用户申请额外权限：

```bash
agentbc task callback <task-id> --root <board-root> --state completed --summary "简短完成说明"
```

CLI 正常退出表示本轮执行已经结束，不表示任务成果成功或已通过用户验收。
质量问题写入执行摘要，由用户结合 report 和产物判断。

## 查看与报告

```bash
agentbc task status
agentbc 4XMC
agentbc task status 4XMC-001 --json
agentbc task report 4XMC-001
agentbc task logs 4XMC-001
agentbc task logs 4XMC-001 --follow
```

始终先用 `agentbc task status` 判断当前任务。只有当输出明确提示存在多个活动候选时，才允许使用 `agentbc task list` 让用户确认。禁止为了猜测“最新任务是谁”而逐个遍历历史任务报告。

当用户说“最新任务”“当前任务”“上一个任务”“继续刚才那个”或类似模糊指代时，必须先运行
`agentbc task status`，把解析出的 `task_code`、精确 iteration id、标题、状态、project root 和 report 展示给用户确认。
这是硬停止点：在用户确认前，禁止读取产物文件、编辑文件、handoff、dispatch 或创建新任务。
只有用户确认该摘要，或用户明确给出 task ID 后，才允许继续。
若用户已给出明确 task ID，先用 `agentbc task status <task-id>` 校验；除非 AgentBC 返回
stale、ambiguous 或 not-ready chain 状态，否则不需要额外确认。

`pending` 只表示排队等待，不是当前任务。除非用户明确提供 task ID，否则禁止把 pending
任务当作当前任务；只有用户主动查询待执行队列时才查看它们。

若需提交已经创建的 pending 任务，使用
`agentbc task dispatch <task-id> --config ~/.abc/config.toml`，禁止重复创建任务。

AgentBC 的紧凑运行状态位于 `~/Documents/AgentBC/workspace/record`；可读 task/report 位于
`workspace/tasks/report`，默认托管产物位于 `workspace/tasks/artifacts`。用户产物不得写入
`record`。目录内自动生成的 `README.md` 会说明文件用途。`agentbc record clean` 仅清理
已结束任务的运行诊断信息，保留核心索引和 `task.json` 状态；不要清理仍在等待输入或恢复的任务。

## 人工干预

```bash
agentbc task pause 4XMC-001 --reason "..."
agentbc task resume 4XMC-001
agentbc task close 4XMC
agentbc task retry 4XMC-001 --step 1
agentbc task reassign 4XMC-001 --to <target-agent>
```

`task close` 只允许当前 active chain head。后续迭代只运行一次命令，由 AgentBC
在同一进程内显示精简风险并询问 `y/n`；不要重复解释风险，也不要要求用户再次运行命令。
`--confirm` 仅用于已经得到明确授权的非交互自动化。

## 跨 Agent 验收

先读取 `agentbc task report 4XMC-001`，再按报告中的任务要求、产物路径、证据和风险验收。
需要另一 Agent 接手时使用：

```bash
agentbc task handoff 4XMC-001 --to <target-agent> --message "复核产物并完成验收" --source-platform hermes --dispatch
```

`--to` 必须遵循上面的“执行者路由”精确映射。用户指定 Claude 时只能使用
`--to claude`，指定 Codex 时只能使用 `--to codex`，指定 Hermes 时只能使用
`--to hermes`。如果用户没有要求另一 Agent 接手，不要仅凭任务内容自动创建跨 Agent handoff。

`agentbc task handoff <task-id>` 默认只能从当前 chain head 继续。若 AgentBC
返回 `stale_handoff_source`，必须使用错误详情中建议的 current head task ID
重新 handoff；禁止创建新任务、遍历历史 report 或猜测其他基线。

若 AgentBC 返回 `ambiguous_chain_head`，必须把候选 head 列给用户确认，不能自行选择。
只有用户明确要求创建分支时，才允许使用 `--branch` 从非 head 任务创建 intentional branch。
