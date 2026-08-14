# 用户指南

中文 | [English](USER_GUIDE.md)

适用于 AgentBC **1.0.2A**（Python 包 `1.0.2a1`）。

## 命令结构

使用 `agentbc <group> <command> --help` 查看当前安装版本的准确选项。

- `agentbc setup`：发现执行器并安装本地集成。
- `agentbc doctor`：只读的安装与 Runner 健康检查。
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

### 1.0.2A 新增或调整的命令

| 命令 | 用途与边界 |
| --- | --- |
| `agentbc setup --show` | 只读显示执行器发现和生效设置，不刷新文件，也不启动迁移。 |
| `agentbc claude budget <usd>` | 为后续 Claude 任务快照设置大于零的有限 USD 预算；已有任务继续使用冻结值。 |
| `agentbc hermes max-turns <turns>` | 为后续 Hermes 任务快照设置正整数迭代上限；已有任务继续使用冻结值。 |
| `agentbc session retention status` | 只读查看执行器临时会话保留设置。 |
| `agentbc session retention enable` | 终态后保留执行器临时会话；永远不影响 dispatcher conversation。 |
| `agentbc session retention disable` | 在符合条件的终态任务后请求后台官方会话清理；活跃、等待输入和恢复任务继续保留会话。 |
| `agentbc task respond <TASK-ID> --input <INPUT-ID> --approve` | 批准当前资源或权限决策，按经校验的一次性策略恢复同一任务和 session。 |
| `agentbc task respond <TASK-ID> --input <INPUT-ID> --deny` | 拒绝当前资源或权限决策；稳定失败原因由输入类型决定。 |
| `agentbc task delete <TASKCODE> --dry-run` | 列出 AgentBC 自有记录、报告、索引项和默认产物，不提示、不写入。 |
| `agentbc task delete <TASKCODE>` | 展示同一删除计划并要求交互式 `y/N`；用户工程始终保留。 |
| `agentbc doctor [--json]` | 执行只读 Doctor v2 检查；退出码固定为 `0` 健康、`1` 警告、`2` 不可用。 |

这些命令只通过 AgentBC 的校验合同修改配置或任务状态。不得在 AgentBC 任务命令中
自行替换为执行器原生的预算、权限、session 或删除参数。

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

- AgentBC 任务权限：`inherit`（保持执行器现有用户/全局设置）；
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

当前任务契约：每个新的 Claude/Hermes 任务会把有效资源上限冻结到
`agentbc.resources`；每个新的 Claude/Hermes/Codex 任务会把 retention 与执行器会话元数据
冻结到 `agentbc.session`。配置缺失时使用 `$10`、`90` 和 retention `false`；字段存在但
非法时 fail closed。handoff 按目标执行器创建新快照，reassign 重建快照；同一 Task ID 的
resume、retry、recover 和再次 dispatch 都保留原快照。

create/dispatch accepted、preflight、status、report 与 Task Brief 统一使用无内部路径的
`execution_policy` 视图：展示当前生效上限（`limit`）、配置上限（`configured_limit`）、
耗尽次数（`exhaustion_count`）、上次决策（`last_decision`）、来源、冻结状态（Codex
resources 为 `null`），以及 retain、执行器 session ID/state 和 project mode。执行器内部
project path 只保留在 task packet，不作为 artifact 展示。Claude 临时 Project 使用 canonical
`<TASK-ID>/claude` 受管路径。实际运行时 Claude 使用冻结的 `--max-budget-usd`，Hermes 使用
冻结的 `--max-turns`；Claude、Hermes、Codex 都会记录执行器官方 session ID。同一 Task 在
`input_required`、retry 或 recovery 后再次运行时，会通过明确 session ID 恢复原会话，不会
选择“最近一次会话”或新建会话重构上下文。

Runner 会同时校验 Worker packet、持久化快照、资源参数、session 参数及 Claude 执行目录；
缺失、重复、篡改或模糊恢复参数都会 fail closed。修改全局预算、迭代次数或 retention
只影响后续新任务，不改变现有 Task 的冻结值。

公共策略还包含有界 cleanup 投影，只展示 capability、state、attempts、稳定 error code
与 retryable。doctor 以任务中的权威 receipt 为准：unsupported、failed，以及超过五分钟的
pending 会告警；retained 与 succeeded 为健康状态。不会展示 Executor 私有路径、原生命令或
原始输出。

资源耗尽决策弹窗已随 Phase 4 提供：当任务以 `kind=resource_limit`（响应协议
`approve_deny`）的 choice 等待决策时，弹窗提供「提高预算并继续」（approve）与
「终止任务」（deny）两个按钮；「Later」、关闭弹窗或超时保持等待。终端兜底命令使用
`--approve` / `--deny`。终态 cleanup 在后台无感执行；`input_required` 和
`needs_recovery` 期间不会清理执行器会话，cleanup 失败或当前 Executor 不支持官方精确删除
也不会改写 Task/report 的原终态。

该策略只管理 Executor 创建的临时会话。AgentBC 永远不会删除创建或 handoff 任务的
dispatcher conversation，也不会要求用户管理单独的 runtime 目录。修改全局设置不会改变
active、`input_required` 或 recovery 任务的既有语义。

## 权限模式

每个任务都带三种规范权限模式之一：`inherit`、`safe` 或 `full`：

- `inherit` 不注入任何 AgentBC 权限、审批、sandbox 或 yolo 覆盖，保持执行器既有的用户/全局设置；
- `safe` 是保守的任务级覆盖，保持执行器既有审批行为；
- `full` 是显式、可审计的选择，使用已安装执行器文档中支持的最强非交互访问。

首次 setup 按 Enter 选择 `inherit`；已有配置按 Enter 保留当前值。旧版本任务缺少权限快照时
仍按 `safe` 回落，新安装默认值变化不会放宽历史任务。

只有用户明确选择任务级覆盖时才在 `task create` 或 `task handoff` 上传递
`--permission-mode <inherit|safe|full>`；否则新任务使用配置默认值，handoff 继承源任务。
禁止在 AgentBC 命令中传递执行器原生权限参数（`--yolo`、`--dangerously-skip-permissions`、
bypass、sandbox 或配置覆盖）。

`safe` 任务遇到确实需要 `full` 的步骤时，会以 `input_required` 停止并声明
`type: permission` 输入：`requested_permission=full` 加 blocked step。通过弹窗或兜底命令批准或拒绝：

```bash
agentbc task respond 4XMC-001 --input INPUT_ID --approve
agentbc task respond 4XMC-001 --input INPUT_ID --deny
```

批准只为该任务下一次同 session continuation 发放一次性 `full` 授权；授权随后被消费或撤销，
绝不被 retry、recover、reassign、handoff 或新任务继承。拒绝以稳定原因
`permission_denied_by_user` 将任务终态置为 `failed`。普通消息文本或“允许”字样既不是授权，
也永远不能充当完成标记。权限弹窗严格只有“允许”和“拒绝”两个动作，不提供 Later 或文本框；
默认动作是拒绝，超时或关闭弹窗也会自动拒绝。超时拒绝记录稳定原因
`permission_denied_by_timeout`。

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

每次成功的 Codex、Claude 或 Hermes 运行，其最终回复都必须以单行恰好一个 version-1 标记结束：

```text
AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"4XMC-001","final_state":"completed","summary":"任务完成","step_results":[{"id":1,"status":"done"}]}
```

task ID 必须匹配；`completed` 要求每个声明的步骤恰好出现一次且为 `done`。合法
`input_required` 标记必须声明至少一个 `blocked` 步骤。零退出、无效 JSON、步骤数据不完整或
纯“允许/拒绝”文本都不等于成功。显式可重试的传输或基础设施失败可以进入 `needs_recovery`，
但 AgentBC 绝不自动重试。流程校验不检查 Git、测试、文件或产物质量。

两步决策需要给出具体原因并描述两个选项的后果：`"input":{"type":"choice","reason":"为什么需要用户决定","options":[{"label":"Option A","description":"选择 A 会做什么或改变什么"},{"label":"Option B","description":"选择 B 会做什么或改变什么"}]}`。
AgentBC 在桌面弹窗中显示原因和说明，配两个直接按钮。deadline 与 CLI 兜底命令保留在任务
报告和通知事件中，但不显示在桌面弹窗。自由文本继续使用 `type: message`，批准/拒绝类请求
使用 `type: permission`，输入弹窗最长等待五分钟；关闭或超时后同一任务继续等待
`agentbc task respond`。

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
agentbc task delete 4XMC
agentbc task recover 4XMC
```

close 只针对当前排队中（pending）或活跃的 chain head；终态迭代（completed、failed、
cancelled、rejected）与过期非 head 迭代都会被拒绝。关闭根任务会释放任务码并删除 AgentBC
自有文件；关闭后续 chain 迭代会保留历史，并提示工程改动无法回滚。用户工程文件永远不会
被 AgentBC 删除。

delete 只接受任务码，不接受 iteration ID。整条链的每次迭代都必须处于
`completed`、`failed`、`cancelled` 或 `rejected`；存在排队中、活跃、等待输入或
等待恢复的迭代时会拒绝整条链。`--dry-run` 零写入，并列出将删除与保留的对象；
普通 `task delete` 会先列出将删除的任务记录、任务说明/报告、索引项和默认 AgentBC Artifact，
随后询问 `Continue? [y/N]`；只有明确输入 `y`/`yes` 才执行。Enter、`n`、EOF 或 Ctrl-C
均取消且零写入。用户工程始终保留。

## 等待输入与决策

任务进入 `input_required` 后保持 open 并等待响应，桌面弹窗只展示任务、阻塞步骤、
原因与直接操作按钮。终端兜底命令：

```bash
agentbc task respond 4XMC-001 --input INPUT_ID --message "继续"
agentbc task respond 4XMC-001 --input INPUT_ID --approve
agentbc task respond 4XMC-001 --input INPUT_ID --deny
```

普通 choice 以 `--message` 提交所选选项；资源耗尽类决策（`kind=resource_limit`、
`response_protocol=approve_deny`）使用 `--approve`（「提高预算并继续」）或 `--deny`
（「终止任务」）。普通 choice 与资源决策中的「Later」、关闭弹窗或超时不提交响应，任务保持等待；
权限确认例外：它只有允许/拒绝，且超时或关闭自动按拒绝终结任务。

## Record 与进程压力

```bash
agentbc record clean --dry-run
agentbc record clean
agentbc runner process-sample
```

record clean 只删除符合条件的终态任务运行时诊断（终态任务的 events、interventions、
run lease 与 run log），始终保留全局索引、权威 `task.json` 状态、可读报告和产物；
record clean 永远不会删除报告。请根据执行器负载与机器性能选择并发数量。

## Doctor

```bash
agentbc doctor
agentbc doctor --json
```

`doctor` 是只读的安装健康检查：package/build 身份、配置、Runner 身份与 spool、存储权限、
已安装 Skill manifest、执行器发现和 session-cleanup receipt。退出码契约固定为：
`0` = healthy（健康）、`1` = warning（警告，例如 Skill 漂移或 cleanup receipt 处于警告态）、
`2` = unavailable（不可用，例如 Runner 或配置缺失）。`--json` 输出与文本视图同一来源的
结构化诊断。

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
