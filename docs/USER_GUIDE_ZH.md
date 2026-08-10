# 用户指南

中文 | [English](USER_GUIDE.md)

## 命令结构

使用 `agentbc <group> <command> --help` 查看当前安装版本的准确选项。

- `agentbc setup`：发现执行器并安装本地集成。
- `agentbc uninstall`：卸载 AgentBC，并分别选择是否删除托管数据。
- `agentbc init`：初始化托管运行记录目录。
- `agentbc claude budget`：设置后续 Claude run 的预算。
- `agentbc hermes max-turns`：设置后续 Hermes run 的迭代上限。
- `agentbc session retention`：查看或修改执行器临时会话保留策略。
- `agentbc record clean`：清理符合条件的运行时诊断记录。
- `agentbc task`：创建、检查、handoff、干预、关闭、删除和恢复任务。
- `agentbc worker`：执行 task board worker 操作。
- `agentbc runner`：启动、停止、检查、采样和显示 Runner 工作。

setup 会在后台启动 Runner。恢复时使用 `runner start`，停止时使用
`runner stop`；前台 `runner serve` 仅用于调试。

## 执行器

AgentBC Public Alpha 支持 Codex、Claude Code 和 Hermes。setup 会检查已配置
可执行文件、当前 `PATH`、用户安装位置以及受支持的编辑器扩展运行时：

```bash
agentbc setup
agentbc setup --show
agentbc runner status
```

Agent 集成会明确传递派发者身份：

```bash
agentbc task create ... --source-platform codex --dispatch
agentbc task handoff 4XMC --to hermes --source-platform claude --dispatch
```

直接从终端调用时可省略该字段，此时派发者记录为 `cli`。setup 后请重启 Agent
客户端，使其重新加载已安装的 Skill。执行器模型选择暂不属于稳定的 Alpha 契约。

## 执行资源与临时会话保留

使用以下命令设置后续 Executor run 的默认值：

```bash
agentbc claude budget 50
agentbc hermes max-turns 150
agentbc session retention status
agentbc session retention enable
agentbc session retention disable
```

Claude 参数必须是大于零的有限 USD 金额；Hermes 参数必须是正整数。Claude 和
Hermes 必须先由 `agentbc setup` 配置，否则对应命令以退出码 2 返回
`not_configured`。retention 命令可独立使用。成功时输出稳定 JSON，包括旧值、新值、
是否实际改写配置以及 `"scope": "future_executor_runs"`；重复设置相同值是幂等操作，
不会重写配置文件。

交互式 setup 提供“使用默认 / 自定义”，已有值时按 Enter 保留；非交互 setup 同样
保留已有值。首次缺失时使用：

- Claude 预算：`$10`；
- Hermes turns：先读取 `hermes config path` 返回文件中的 `agent.max_turns`，再尝试
  兼容的顶层 `max_turns`，最后回退 `90`；
- 执行器临时会话保留：`false`。

设置保存在 AgentBC 配置中：

```toml
[executors.claude]
max_budget_usd = 50.0

[executors.hermes]
max_turns = 150

[sessions]
retain_executor_sessions = true
```

Phase 2 任务契约状态：每个新的 Claude/Hermes 任务会把有效资源上限冻结到
`agentbc.resources`；每个新的 Claude/Hermes/Codex 任务会把 retention 与执行器会话元数据
冻结到 `agentbc.session`。配置缺失时使用 `$10`、`90` 和 retention `false`；字段存在但
非法时 fail closed。handoff 按目标执行器创建新快照，reassign 重建快照；同一 Task ID 的
resume、retry、recover 和再次 dispatch 都保留原快照。

create/dispatch accepted、preflight、status、report 与 Task Brief 统一使用无内部路径的
`execution_policy` 视图：展示有效上限、来源、冻结状态（Codex resources 为 `null`），以及
retain、执行器 session ID/state 和 project mode。执行器内部 project path 只保留在 task
packet，不作为 artifact 展示。Hermes `--max-turns`、同会话 resume、终态 cleanup/purge 和
资源耗尽处理仍属于后续运行时阶段；存在冻结快照不代表这些行为已经执行。

该策略只管理 Executor 创建的临时会话。AgentBC 永远不会删除创建或 handoff 任务的
dispatcher conversation。修改全局设置不会改变 active、`input_required` 或 recovery
任务的既有语义。

## Create 与 Handoff

独立任务使用 create：

```bash
agentbc task create \
  --title "Add CSV export" \
  --assignee codex \
  --steps ./steps.yaml \
  --customer-path /path/to/project \
  --dispatch
```

当工作依赖、审查或修改已有 AgentBC 产物时，必须使用 handoff。若将已有托管
产物目录重新传给 create，Core 会返回 `handoff_required` 并给出当前 head 的
建议命令。

## 图片任务

Codex 支持多张原生图片输入；Hermes 当前每轮任务支持一张。若用户没有另行指定
工程路径，图片本身的路径同时作为 `--customer-path`：

```bash
agentbc task create \
  --title "分析并修改这张设计图" \
  --assignee codex \
  --steps ./steps.yaml \
  --customer-path /absolute/path/design.png \
  --image /absolute/path/design.png \
  --dispatch
```

Codex 多图任务可重复传入 `--image`。handoff 默认继承上一轮图片引用；显式提供
新的 `--image` 时替换为新输入。生图和改图使用执行器自身的原生能力，最终位图
必须落在任务 artifact root；模型服务与鉴权仍由执行器自身负责。

## 状态与报告

```bash
agentbc task status 4XMC
agentbc task report 4XMC
agentbc task logs 4XMC
```

- `completed`：执行正常启动并结束，不代表质量通过验收。
- `needs_recovery`：执行未能正常启动或继续。
- `failed`：执行已启动，但未能确认执行器正常退出。

Agent callback 是可选元数据，Runner 观察到的执行器退出才是正常完成依据。

每份报告与 task brief 都包含 `Dispatcher Traceability`（派发者溯源）小节，带两个标签：
`Dispatcher platform`（派发平台）和 `Dispatcher conversation ID`（派发会话 ID）。它们描述
创建或 handoff 任务的控制端会话，而不是执行器的临时会话。`Dispatcher platform` 是派发平台，
例如 `codex`、`claude` 或 `hermes`。`Dispatcher conversation ID` 在派发者提供可信会话 ID 时
显示该 ID，否则显示 `unavailable`。AgentBC 记录的是 handoff 当前派发者会话，而不是源任务
会话，并且绝不从进程、路径、历史记录或上一个任务中猜测会话 ID。派发者溯源与执行器临时会话
相互独立，AgentBC 不会删除派发者会话。

## Task List 与健康状态

```bash
agentbc task list
agentbc runner show
```

Task List 跟踪当前派发批次。计时器仅表示显示界面仍在刷新，不会轮询执行器状态。

- 绿色：近期存在进度证据；
- 黄色：Runner 正常，但至少五分钟没有进度；
- 橙色：Runner 正常，但至少十分钟没有进度；
- 红色：需要恢复或已终态失败；
- 灰色：已排队，等待启动。

AgentBC 只观察未响应任务，不会自动取消，因为强制终止可能让用户工程处于部分
修改状态。

## 任务干预

```bash
agentbc task pause 4XMC
agentbc task resume 4XMC
agentbc task close 4XMC
agentbc task delete 4XMC --dry-run
agentbc task delete 4XMC --confirm
agentbc task recover 4XMC
```

close 只针对当前排队中或活跃的 head。关闭根任务会释放任务码并删除 AgentBC 自有文件；
关闭后续 chain 迭代会保留历史，并提示工程改动无法回滚。用户工程文件永远不会
被 AgentBC 删除。

delete 只接受任务码，不接受 iteration ID。整条链的每次迭代都必须处于
`completed`、`failed`、`cancelled` 或 `rejected`；存在排队中、活跃、等待输入或
等待恢复的迭代时会拒绝整条链。`--dry-run` 零写入，并列出将删除与保留的对象；
`--confirm` 只删除 AgentBC 自有 record、report、index entry 和 managed artifact，
成功后释放任务码。用户工程始终保留。

## Record 与进程压力

```bash
agentbc record clean --dry-run
agentbc record clean
agentbc runner process-sample
```

record clean 会保留全局索引、权威状态、可读报告和产物。请根据执行器负载与机器
性能选择并发数量。

## 卸载

```bash
agentbc uninstall
```

卸载流程会分别询问是否清理任务记录与报告、是否清理默认工作区产物。用户工程
路径不会成为卸载目标。CLI 损坏时，可使用 Release 包中的独立脚本
`uninstall-agentbc-alpha.sh`。

## 故障排查

### 找不到命令

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v agentbc
```

### Runner 不可用

```bash
agentbc runner start
agentbc runner status
```

如果仍然超时，请检查 `~/.abc/runner/runner.log`。

### 未发现执行器或 Skill

先验证执行器自身 CLI 和登录状态，再重新运行 setup，并启动新的 Agent 会话。
Hermes Skill 不一定以 slash command 的形式展示。

### customer path 被拒绝

直接传递用户指定的文件或目录。不要读取 Runner allowed roots，也不要将工程复制
到 AgentBC workspace。如果 Core 返回 `handoff_required`，请继续已有任务链。

### 任务变为黄色或橙色

检查日志和执行器界面，排查网络、配额、权限或长时间推理。进度恢复后健康状态会
重新变绿。

### 已生成产物但任务状态为 failed

`failed` 描述的是退出契约，不表示一定没有产物。请检查报告、日志和产物，再决定
恢复任务还是 handoff 给其他执行器。

数据归属与内部边界详见 [README 架构章节](../README_ZH.md#架构)。
