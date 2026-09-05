# AgentBC 1.0.4A 需求开发清单

> 制定日期：2026-08-26
> 状态：持续开发与回归中；`SESSION-104-001` 保持已通过，`PERM-104-001` 的 Claude 同会话 safe-to-full 自动化门禁已完成，`PERM-104-002` 仍开放
> 目标版本：AgentBC `1.0.4A` / Python `1.0.4a1`
> 来源基线：`private/integration@01f3ce1`
> 已发布基线：`v1.0.3A2@62757a4`；公开 Formula 收口 `public/main@87c4bca`
> 上版归档：`AGENTBC_1.0.3A_DEVELOPMENT_CHECKLIST.md`
> 架构依据：`AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md`

## 0. 版本目标

`1.0.4A` 不再扩展 AgentBC 的权限等级或支持平台，而是把 `1.0.3A` 已经可用、但仍依赖
Prompt、共享大模块或脆弱终态顺序的控制链变成 Core-owned、可机械验证、可重放的合同。

本版只承担十个权威开发项：

1. `PROTO-104-001`：三 Executor 版本化协议 fixture matrix；
2. `ARCH-104-001`：在 characterization 保护下进行局部机械拆分；
3. `PERM-104-001`：审批资格、Deny 和 fallback eligibility 机械判定；
4. `PERM-104-002`：阻塞来源域与不可升级动作收敛；
5. `FLOW-104-001`：handoff 结构化多 steps；
6. `FLOW-104-002`：终态通知、报告、record budget 与 cleanup 解耦；
7. `FLOW-103-001`：从 1.0.3A 转入的权威单调 progress receipt；
8. `SESSION-104-001`：Codex CLI/Desktop 双入口临时会话清理验收；
9. `FLOW-104-003`：Failed 任务的 retry 与 handoff 恢复闭环；
10. `INPUT-104-001`：custom path 与外部输入附件的安全双根合同。

原独立项 `UX-104-001` 降级为 `PERM-104-002-R1` 派生回归项：不单独占用开发 Wave，必须在
`PERM-104-002` 完成并证明不可升级阻塞已收敛后再执行弹窗详情回归。

Update、Homebrew、Claude/Hermes session teardown、auxiliary session cleanup、三 Executor native
approval 和 `inherit|safe|full` 不是本版重做项，只作为不可回归基线。Codex session teardown
不重写私有会话存储，只补齐 CLI/Desktop 双入口真实验收及由该验收暴露的窄修复。

## 1. 不可改变的 1.0.3A 基线

- setup 权限默认继续是 `inherit`；新根任务读取当前配置，handoff 默认继承来源快照，
  retry/recover/respond 继续使用任务冻结快照；
- `inherit` 不是独立于 `safe/full` 的第三套 Executor 行为。它先解析用户原生配置，再由实际
  transport 事件判断动作是否受阻；不能恢复“只有 safe 才能审批”的错误模型；
- 原生 `single_action` 优先，兼容 full 只在 Core capability/policy 明确允许时存在；
- 不从普通 stderr、Prompt、Agent 自述、退出码或聊天总结合成 permission receipt；
- AgentBC 不扫描 Executor 私有会话库猜测 session ID；主会话与派生会话必须来自官方 receipt；
- `agentbc update` 保持自动 check 和 `y/N`，Alpha 不新增用户主动 rollback 命令；
- Homebrew Formula 继续只声明通用 `python`，不得硬编码 Python 小版本或把 Xcode/CLT 变成
  AgentBC 依赖；
- customer project、dispatcher conversation、未登记 session 与已发布 tag/PyPI 文件不得删除、
  移动或覆盖；
- `completed` 仍只表示流程合同完成，不替代产物质量验收。

## 2. 权威待办与优先级

| ID | 优先级 | 问题 | 目标结果 | 主要依赖 |
| --- | --- | --- | --- | --- |
| `PROTO-104-001` | P0 / fixture matrix 已完成（2026-08-27）；production collaboration_spawn wiring 待回归（2026-08-28） | 上游 CLI version/help/argv/event 漂移只能靠临时补测试 | 三 Executor 完整版本化 fixture、capability matrix 和未知组合 fail-closed；生产派生会话接线需通过版本 fixture + live probe 双门 | 无 |
| `ARCH-104-001` | P1 / 按域执行 | Service、Runner、CLI、approval、notification 责任仍集中 | 每个功能项先完成对应窄模块机械拆分，公共 API/CLI/磁盘行为不变 | `PROTO-104-001` |
| `PERM-104-001` | P0 / 实现与自动化门禁完成（2026-09-05） | native Deny 后 Agent 仍可用 Prompt/callback 请求 full 并启动第二 worker | Claude safe/default 的首个结构化 `can_use_tool` 事件只产生一次同会话输入；Approve 原子返回原始 input + `setMode/bypassPermissions/session`，Deny 无 mode change；重复事件 fail closed | `RM7A-001` artifact evidence；`tests/test_perm104_001_claude_same_session_elevation.py`；`PERM-104-002` 的部署后 full canary 仍独立开放 |
| `PERM-104-002` | P0-Blocker / 仍开放（RAXT-001，2026-08-31） | Claude native request 曾在 session receipt 建立前失败，后续 callback/full popup/worker crash 不能证明 native approval 成功；同一不可升级阻塞还可能重复请求 | 已补 session-first、native single_action 权威绑定、callback fail-closed、Runner contained cleanup 与回归；仍需 deployed explicit/temporary/inherited full 真机 canary 和 `PERM-104-002-R1` 详情回归，未通过前不得关闭 | `PERM-104-001` |
| `FLOW-104-002` | P0 | report/record 超限可跳过终态通知和 cleanup receipt | terminal、report、notification、cleanup 独立且可重放，通知不被报告失败吞掉 | terminal fixtures；对应 ARCH slice |
| `FLOW-104-001` | P1 | handoff 只能声明一个 step，自由文本多步骤直到 callback 才失败 | handoff 原生结构化 steps、dispatch 前预检、严格 callback 一致性 | schema fixtures；对应 ARCH slice |
| `FLOW-103-001` | P1 / 跨版转入 | 资源耗尽或系统终态覆盖 callback 时会把真实部分进度回退 | task/run/session scoped 单调 progress receipt；所有公共视图同源 | `FLOW-104-001` 的 declared steps |
| `SESSION-104-001` | P0 / 已通过（2026-08-29） | 历史 cleanup 只证明 CLI delete；现已建立同一官方 session 的 archive→delete 命令闭环并完成 CLI/Desktop 后端真机验收 | `QV46-001` completed 与 `NMY4-001` failed 均对精确主会话完成 archive/delete acknowledgement，CLI、Desktop backend 及当前应用 active/archived 列表均 absent；Desktop 刷新延迟不作为门禁 | 已完成；原生派生子会话未实际触发转 `PROTO-105-001` P2 |
| `FLOW-104-003` | P0 / 恢复闭环 | 当前终态 `failed` 既不能 `task retry`，也不能作为普通 handoff 源，失败后只能人工绕行 | status/report 给出机械可判定的 retry/handoff 动作；同任务重试与新 iteration 交接均保留审计和进度 | failure taxonomy；`FLOW-104-002` terminal receipts；`FLOW-104-001` steps |
| `INPUT-104-001` | P0 / 派发阻断 | 显式 custom path 时，位于项目根之外的 `--image`/输入文件被 `image input is outside task roots` 原子拒绝 | 项目根与只读附件根分离；Runner 受控导入外部文件且不扩大 Executor 项目权限 | PathPlan v2；atomic dispatch；input manifest |

### 2.1 P1 待回归项（不新增权威开发项）

| ID | 优先级 | 现场基线 | 回归目标 | 主要依赖 |
| --- | --- | --- | --- | --- |
| `RESOURCE-104-001-R1` | P1 / 待回归 | `E52M-002` 的 Hermes run 使用完 `150/150` 次迭代后，Core 已持久化 `input_required(type=choice, kind=resource_limit)`、RunLease 已挂起且 CLI 可响应，但 Codex Desktop 没有显示“提高预算并继续 / 终止任务”弹窗 | 每个仍有效的 resource-limit input 都有且只有一个 Desktop 弹窗；Approve 将当前 Task 上限翻倍并恢复同一官方 session，Deny 单调终止；CLI 响应保持等价兜底，但不能替代 Desktop 真机验收 | `FLOW-104-002` notification delivery；`FLOW-103-001` progress receipt；DialogNotifier |
| `FLOW-104-003-R1` | P1 / 待回归 | `E52M-003` 中 Hermes 0.20.1 运行 `2h37m` 后以返回码 `0` 结束，但输出停留在代码 diff、未产生 `AGENTBC_FINAL_CALLBACK`；Runner 明确记录 `output_truncated=false`、`marker_seen=false`，且没有可识别的迭代耗尽 receipt | 进程成功退出与任务合同完成继续严格分离；Hermes 必须提供结构化 terminal reason、实际/上限 turns 和最终响应边界。确属资源耗尽时生成唯一可恢复 input；仍有 pending step 却正常退出时给出稳定的 incomplete-exit 分类、保留部分进度并允许受审计 retry/handoff；不得伪造 callback | `FLOW-104-003` failed recovery；`FLOW-103-001` progress receipt；Hermes ACP/CLI terminal receipt |

`RESOURCE-104-001-R1` 的固定验收合同：

- 弹窗只投影持久化的 `agentbc.input`，展示 Task、Executor、已用/当前/下一上限、阻塞原因和两个固定
  决策按钮；不得从 Agent 文本或 stderr 临时合成；
- Approve 必须绑定原 `input_id`，保持 Task ID、官方 session ID、冻结权限与已完成进度，将当前上限按
  已冻结 multiplier 提高后只启动一个 continuation；Deny 不启动 continuation，并记录资源耗尽终态；
- Desktop 未运行、Notifier 投递失败或应用重启时必须保存可行动的 notification receipt；恢复投递后最多
  重放一次，已由 CLI 响应或过期的 input 不得再次弹窗；
- 回归覆盖首次耗尽、第二次耗尽、Runner 重启、Desktop 重启、重复/乱序投递、Approve、Deny、超时及
  CLI/Desktop 竞争响应；公共 status/report/notification 必须同源；
- CLI `agentbc task respond ... --approve|--deny` 是弹窗缺失时的正式恢复入口，但仅证明控制面可恢复，
  不得据此把 Desktop 弹窗回归标记为通过。

`FLOW-104-003-R1` 的固定验收合同：

- 返回码 `0` 只证明 Executor 进程正常结束；只有合法且唯一的 `AGENTBC_FINAL_CALLBACK` 才能声明任务
  flow completed，Core 不得从 diff、自然语言总结或退出码补写 callback；
- Hermes terminal receipt 必须绑定 Task、run、官方 session 与冻结资源快照，并明确区分 completed、
  max-turn/context exhaustion、user stop、transport failure 和 incomplete normal exit；未知原因 fail closed；
- 资源耗尽分类不得只依赖人类可读 stderr 正则；fixture 与 live probe 必须覆盖当前支持版本，协议未知时保存
  原始脱敏 reason 并进入可恢复诊断，而不是静默退化为不可重试的 `completion_marker_missing`；
- 回归覆盖合法 callback、缺 callback 的返回码 0、非零退出、输出截断、最大 turns、上下文耗尽、Runner
  重启，以及同 Executor retry/跨 Executor handoff；每条路径验证唯一 RunLease、部分进度单调和通知幂等。

### 2.2 P2 待优化项（视进度决定是否并入 1.0.5A）

| ID | 优先级 | 现场基线 | 优化目标 | 发布关系 |
| --- | --- | --- | --- | --- |
| `PROTO-105-001` | P2 / 待优化 | Codex 0.150.1 schema、fixture 与 live probe 均声明 `collabAgentToolCall`/`spawnAgent`，且 AgentBC 已显式选择 collaboration 与 Ultra；但 `C5FN-001`、`NMY4-001` 仍由父模型直接输出 `CHILD_SESSION_CANARY_OK`，没有官方 `spawnAgent` lifecycle、receiver thread ID 或 auxiliary ledger | 找到官方、可验证的原生协作工具启用合同；只有收到真实 `item/started`→`item/completed`、官方 receiver thread receipt 并完成父子 cleanup 后才算通过。禁止把模型文字或模拟子代理当作成功 | 不重新打开 `SESSION-104-001`；根据 1.0.5A 开发容量决定是否并入，未并入时继续 fail closed |

`PROTO-105-001` 不属于 `1.0.4A` 发布门禁。当前主会话 completed/failed cleanup 已通过；由于上述 canary
没有实际创建派生会话，它既不能证明派生清理通过，也不能否定已登记 auxiliary receipt 的现有清理实现。

## 3. 开发顺序与并行边界

### Gate 0：冻结基线

- integration、三个 agent 分支、CLI、Runner、三平台 Skill identity 一致且工作树干净；
- `agentbc doctor --json` blocker 为 0，无 active/input_required/needs_recovery 历史任务阻塞开发；
- 固定支持的 Codex、Claude、Hermes 版本范围及真实 probe 输出；
- 运行 `1.0.3A` 权限、session、Update/Homebrew、发布与全量测试，保存基线结果；
- 用受控临时任务复现并冻结三项现场基线：Codex CLI/Desktop 清理可见性、无详情按钮的权限弹窗、
  Failed 后 retry/handoff 的当前拒绝码；
- 冻结 `custom path + external image` 的当前 `atomic_dispatch_error`，证明拒绝前没有创建 task、workspace、
  worker 或 RunLease；
- `PROTO-104-001` 完成前，不修改 Executor argv、event parser、approval/session capability gate。

### Wave 1：协议面与机械拆分

1. 完成 `PROTO-104-001`；
2. 完成 `INPUT-104-001` 的 PathPlan/input manifest schema 与原子导入边界；
3. 按功能域分别建立 characterization tests；
4. 每次只拆一个责任模块，并以独立提交完成对应 `ARCH-104-001` slice；
5. 机械拆分提交不得同时改变 schema、状态机、权限语义、CLI 文案或通知行为。

建议拆分顺序：

- approval decision service；
- terminal delivery coordinator；
- handoff/declared-step contract builder；
- progress receipt projector；
- Doctor collectors 与 Runner IPC handlers；
- update service 只在上述主线完成且测试证明有必要时拆分。

### Wave 2：权限机械判定与不可升级阻塞收敛

- 当前开发资源优先投入 Claude `PERM-104-002` 审批循环；在显式 full、临时 full、继承 full 三条真机
  canary 收敛前，不继续扩展 Codex 原生派生会话能力；
- `PERM-104-002` 必须在 `PERM-104-001` 的 Core-owned approval decision 落地后实施；
- `PERM-104-002` 完成前不实施或单独验收详情按钮修复；先证明相同不可升级阻塞不会生成第二个 permission input；
- `PERM-104-002` 通过定向/全量测试后执行派生项 `PERM-104-002-R1`，只补齐/验证 Core 详情投影和
  `View Details` 回归，不改变审批资格与阻塞收敛语义；
- 两个主项共享 approval schema、notification projection 或 DialogNotifier 时，不并行写同一文件，
  由 integration 先冻结公共接口再派发。

### Wave 3：终态、Failed 恢复与 Codex 双入口清理

- `FLOW-104-002` 先建立独立 terminal/report/notification/cleanup receipt，作为失败恢复的审计基础；
- `FLOW-104-003` 随后开放 Failed retry/handoff，禁止用清空失败记录或复制任务伪装恢复；
- `SESSION-104-001` 已完成 CLI/Desktop 后端真机矩阵并通过；后续仅执行不改变发布结论的防回归；
- 三项必须共同覆盖“旧 session 已清理后 retry 不得恢复已删除 session”与“handoff 不删除派发端对话”。

### Wave 4：结构化流程与权威进度

- 先完成 `FLOW-104-001` 的 declared steps schema、旧任务双读和 dispatch preflight；
- 再完成 `FLOW-103-001`，progress receipt 只能引用已持久化 declared step ID；
- 两项不得放宽 callback 的未知/重复/缺失 step fail-closed 校验。

### Wave 5：集成与发布候选

- 运行三 Executor permission Approve/Deny/blocked、handoff multi-step、资源耗尽和 terminal failure
  真实 canary；
- 执行 `RESOURCE-104-001-R1` 的 Desktop/CLI 双入口资源耗尽回归，保存弹窗、notification receipt、
  同 session continuation、资源上限变化与去重证据；
- 运行 Update、Homebrew、session teardown/auxiliary cleanup 全套回归；
- 构建 `1.0.4a1` 候选并完成 macOS bundle、PyPI dist、Homebrew Formula/bottle 与双机验证；
- 所有 Gate 完成前不创建公开 tag、GitHub Release 或 PyPI 文件。

### 建议节奏（自 2026-08-27 起）

| 时间窗 | 主目标 | 退出条件 |
| --- | --- | --- |
| 8 月 27 日—8 月 28 日 | Gate 0、三项体验问题复现、基线证据归档 | 干净基线、稳定拒绝码/截图/receipt、无遗留测试会话 |
| 8 月 31 日—9 月 4 日 | Wave 1：协议 fixtures、外部输入双根合同与首批机械拆分 | `PROTO-104-001`、`INPUT-104-001` 完成，后续功能修改有 characterization 保护 |
| 9 月 7 日—9 月 11 日 | Wave 2：`PERM-104-001`、`PERM-104-002`，随后执行 `PERM-104-002-R1` | 不可升级 blocked 先收敛，再通过 Approve/Deny/Details 回归 canary |
| 9 月 14 日—9 月 18 日 | Wave 3：`FLOW-104-002`、`FLOW-104-003`、`SESSION-104-001` | Failed retry/handoff 与 Codex 双入口清理矩阵通过 |
| 9 月 21 日—9 月 25 日 | Wave 4：`FLOW-104-001`、`FLOW-103-001` | multi-step handoff 与单调 progress 全链路通过 |
| 9 月 28 日—10 月 2 日 | Wave 5：全量回归、双机 RC、发布材料 | `1.0.4a1` RC 可复验，进入用户 go/no-go |

节奏按 Gate 退出，不按日期强行推进。任一 P0 真机 canary 未通过时，后续 Wave 可以继续做不冲突的
fixture/文档工作，但不得进入公开 RC。

## 4. 详细实现合同

### 4.1 `PROTO-104-001`：版本化 Executor fixture matrix

每个受支持 Executor 至少冻结：

- `--version`、`--help` 和相关子命令 help；
- AgentBC 生成的安全 argv、cwd、writable roots 和环境变量；
- session start/early receipt、resume、terminal 与 cleanup/delete 事件；
- native permission request/response、Deny、transport lost 和 unsupported capability；
- 资源耗尽、partial progress、invalid callback 与无 callback；
- 脱敏规则：fixture 不得含 token、raw private session path、用户 prompt 或敏感 argv。

验收要求：

- fixture 目录按 Executor、协议版本和能力分层，不用单一“latest”覆盖历史；
- parser/capability gate 的参数化测试覆盖当前支持版本、边界版本和未知未来版本；
- 未知字段可按协议兼容规则忽略，未知关键事件、缺失 early receipt 或能力组合必须 fail closed；
- fixture 更新必须附真实 probe 命令、脱敏 diff 和为何仍兼容的说明；
- fake Executor 只验证 Core 合同，不能替代三条真实 transport canary。

2026-08-27 收尾证据：`agent/claude@b45b133` 建立 Codex `0.146.0/0.147.0/0.150.1`、
Claude `2.1.226/2.1.233`、Hermes `0.17.0/0.20.1` 的版本化 matrix、manifest、capture/redaction
工具和参数化 fail-closed 测试；`private/integration@3d1fa1a` 完成初次集成，`9fce6b6` 根据真实
SESSION canary 补入独立 `desktop_visibility=thread/list` capability，并保持 `0.150.1` 为 candidate，
没有扩大 production version gate。原 AgentBC 任务 `67WY-001`、`574Z-001` 的失败只代表权限/回调
链未闭环，代码成果已由 integration 重新验证和提交，不再作为本项完成状态来源。

### 4.2 `ARCH-104-001`：局部重构

- 采用“characterization → 机械移动 → import compatibility → 功能修改”的四步法；
- 保留现有公共 import、CLI、配置键、task/report/record 路径和历史任务 reader；
- 新模块必须只有一个权威职责，禁止复制 Service/Runner 逻辑形成第二状态源；
- schema/protocol 变化使用独立提交；新任务写新字段时历史任务必须有明确双读和默认值；
- 每个 slice 都要证明生产代码净复杂度下降或责任边界收敛，不能只增加 wrapper；
- update、permission、notification、progress 的重构不得共享一个“大爆炸”提交。

### 4.3 `PERM-104-001`：审批机械判定

Core 只依据受支持 Adapter 的可信结构化 permission-block event 创建审批：

- 原子绑定 `task_id`、chain head、executor run、官方 session、request ID、fingerprint、operation、
  blocked declared step、capability 和 RunLease；
- UI/CLI 只投影持久化 `agentbc.approval` 与 `input_required`，不重新解释 Prompt；
- Approve/Deny 必须响应原 request，并验证 task/run/session/fingerprint 与过期状态；
- native Deny 是同一 request 的单调终态：动作零执行、无第二 worker、无自动 full fallback；
- compatibility full eligibility 只在 transport 缺少受支持 native single-action、任务冻结策略允许，
  且 Core capability matrix 明确命中时生成；
- Agent callback 中的 `requested_permission=full` 只能作为不可信诊断输入，不能直接弹窗或发 grant。

回归矩阵至少包含 Approve→Deny→Approve、重复/乱序响应、跨 task/run/session、过期 request、
native Deny 后伪造 full callback、transport lost、Runner 重启和 UI/CLI 双入口。

2026-09-05 `PERM-104-001 / RM7A-001` Claude same-session safe-to-full 收口：

- Claude safe/default 任务从 SDK `permission_mode=default` 开始；首个可信 `can_use_tool` 事件绑定
  task、run、官方 session、request/tool-use ID、request/action fingerprint 与精确 input digest，并在
  Core 持久化脱敏 `safe/default → elevation_pending → set_mode_response_ready →
  bypassPermissions_active` 收据；只有同一 tool-use 的结构化 `PostToolUse` 成功才能进入 active。Runner 在 SDK
  callback 可能抢先到达前先登记该 run，避免 receipt/run 竞态；PathPlan 与宿主 containment digest
  保持冻结。
- 唯一 Approve 返回官方 `PermissionResultAllow(updated_input=原始 input,
  updated_permissions=[PermissionUpdate(type="setMode", mode="bypassPermissions",
  destination="session")])`，同一个控制响应同时放行原动作并切换同一 live session；Deny 只返回
  `PermissionResultDeny`。两条路径均不创建 matcher、category、session-rule、grant、worker、CLI
  continuation 或新 session；显式 full 仍直接映射 `bypassPermissions` 并保持非交互启动。
- DialogNotifier/notification 服务通过 durable cardinality 只允许一个 input、一个 dialog、一个决策；
  setMode 响应准备后再次收到 `can_use_tool` 只记录一次 `claude_full_mode_ineffective` 并 fail closed，
  不重新弹窗或授权。能力准入检查 SDK protocol shape 与可序列化 setMode，不检查 Claude 版本表，
  因而同形兼容 fork 可被接纳，缺失 shape、transport 丢失或 setMode 构造失败均停止。
- 自动化覆盖安装 SDK 的 exact wire serializer、Deny/no-update、重复/并发回放、同 lease/session、
  one-dialog delivery、异构 Read/Write/Edit/Bash fake stream、shape-only admission、rejected setMode
  与 transport loss；可复验证据见 artifact root 的 `PERM-104-001_RM7A-001_EVIDENCE.md`。

### 4.4 `PERM-104-002`：阻塞来源域、不可升级动作与 full 闭环

- 为动作保存脱敏稳定 fingerprint，不保存 raw argv、token 或私有绝对路径；
- 来源域至少区分 Executor policy、AgentBC permission policy、Runner PathPlan 和宿主 OS containment；
- 每次审批后记录精确执行结果和来源域是否变化；
- 同一 task/run/session/action 获批后再次命中相同 fingerprint 与不可升级来源域时，直接 blocked；
- blocked 收敛不得创建新 permission input、grant、worker、continuation 或重复通知；
- 只有可信 transport 证明来源域变化，或用户明确发起不同动作，才能创建新 request；
- linked-worktree 共用 Git store 只作为测试样例，不新增穷举 Git/path 预检或控制器提交阶段。
- 对 Task 已声明、PathPlan 合法且属于预期执行边界的动作，`full` 必须同时落实 Executor 原生映射、
  Runner 精确授权根和受管 record/linked-worktree Git metadata 能力；不能只改变 Executor flags，
  却让同一动作继续被未变化的宿主 containment 拒绝；
- `full` 来源必须保留并可审计：用户创建/派发时人工显式授予、`inherit|safe` 合法阻塞后的临时申请
  Approve、handoff/retry/continuation 的冻结权限继承；三条路径进入执行前必须归一到同一权威 runtime
  capability receipt，禁止静默降级、只改文案或只改 Task selector；
- 已经生效的 concrete `full` 不得再次请求 `full`，不得产生 `permission_mode_unsupported`、
  `permission_resume_session_unavailable`、重复弹窗、重复 grant 或同 fingerprint continuation。

统一验收标识：人工授予、临时申请、权限继承均能让 `full` 权限正确生效，声明范围内的目标动作必须
实际执行成功，不得仍有权限、Runner PathPlan 或宿主 containment 阻塞。三条路径分别以同一组
linked-worktree 本地提交、受管 progress receipt 和普通项目写入 canary 验证，要求动作成功、唯一
RunLease/continuation、无第二次弹窗且 status/report/receipt 同源。声明范围之外的动作仍按 PathPlan
fail closed，不得借 full 扩大到任意用户或系统路径。

对于本来就不可升级、且不属于声明授权边界的动作，最多出现一次审批并稳定 blocked；真正可升级的
Executor 拒绝必须在同 session、同 request Approve 后精确执行。

派生回归项 `PERM-104-002-R1` 在上述验收完成后执行：

- 负向 fixture 固定 `HTE7-001` Step 3 的第九次 compatibility-full 请求：前八次 grant 均无法改变
  Git store 的宿主 containment，第九次 notification 的 `reason_detail=""`，导致 `View Details` 缺失；
- 正向 canary 固定 `KQGF-001` Codex native `single_action`：Core detail 存在、`View Details` 可见，
  Approve 只执行绑定的精确动作；
- Core 必须从持久化 approval identity 生成脱敏、有界、只读详情；按钮是否出现不得依赖 Executor
  可选自然语言 detail，Executor detail 只能作为补充；
- 主决策仍只有 Approve/Deny，默认 Deny；`View Details`/`Back` 不响应 request、不重置 deadline、
  不发 grant、不启动 continuation；
- 回归覆盖 compatibility full、native single_action、legacy receipt、空/超长/含控制字符 detail、
  Details→Back→Approve/Deny、关闭/超时和 Runner 重启重放；
- 若 `PERM-104-002` 已把相同不可升级动作收敛为 blocked，则不得为了测试详情而制造第二次权限请求；
  使用首个合法请求或 fixture 验证详情投影。

`PERM-104-002-R1` 不是独立发布项；它的通过证据归档到 `PERM-104-002`，失败则重新打开
`PERM-104-002`，不得绕过阻塞收敛单独发布 UI 修复。

以下现场证据并入 `PERM-104-002`，不再作为独立 `R2` 发布项：

- 复现基线固定为 `PROTO-104-001` / `67WY-001` / Claude Step 6：Task 的
  `requested_mode=full`、`effective_mode=full`、`selection_source=explicit_task` 且
  `approval_policy=none`，Executor run `claude-67WY-001-43319d05` 仍触发 permission block；
- 当前错误链为 `permission_mode_unsupported` → `permission_resume_session_unavailable` →
  `needs_recovery`，诊断为 `resolved permission base does not allow escalation`；原 RunLease 已关闭，
  原 worktree 改动仍保留且没有合法 final callback；
- `PROTO-104-001` / `574Z-001` 使用 `inherit` 后成功弹窗并至少三次恢复同一 Claude session，但批准没有
  改变 linked-worktree Git metadata 的宿主 containment，重复产生 permission input，最终 Step 4 blocked、
  Step 5 pending 且没有本地提交；这是临时申请 full 未真实生效和相同来源域未收敛的同一缺陷；
- 修复后必须用 `67WY-001` 的显式 full 基线和 `574Z-001` 的 inherit→临时 full 基线共同回归，并新增
  full 权限继承 canary；三者都要完成同类声明动作，不得生成上述 recovery code、重复审批或 blocker；
- 回归覆盖 Claude/Codex/Hermes、Runner/宿主 containment、restart/recover/retry/handoff，并保留原
  task/run/session/request/fingerprint 审计链。

2026-08-28 实施证据（`E52M-001`，agent/hermes 本地提交，未 push）：

- 新增 `agentbc.permission_runtime` v1 权威 runtime capability receipt：统一
  `explicit_task` / `one_shot_permission_grant` / `inherited_task` 三种 full 来源，绑定
  task/chain head/executor/run/官方 session、PathPlan digest 与 host profile digest；固定五级
  escalation 层级 `executor_policy` → `agentbc_policy` → `runner_pathplan` → `host_containment`
  → `linked_worktree_metadata`，生命周期 `prepared → authorized → activated → verified | blocked`；
  grant 只在 authorized 后消费，同一 host profile 内 activated，结构化动作成功后 verified；
- 七个稳定错误码落地：`permission_runtime_capability_unavailable`、
  `permission_transport_unsupported`、`permission_escalation_ineffective`、
  `permission_action_already_blocked`、`linked_worktree_capability_invalid`、
  `host_containment_unliftable`、`permission_block_evidence_unavailable`；
- 可信 block ledger 持久化 fingerprint/domain/profile digest/decision/execution result/domain
  变化；Approve 后相同 task/session/action/fingerprint/domain/profile 再现时收敛为
  `permission_escalation_ineffective`（needs_recovery、step blocked、零新增
  input/grant/worker/continuation/deadline/通知）；concrete full 直接收敛，不再请求 full 或产生
  `permission_mode_unsupported` / `permission_resume_session_unavailable`；
- macOS Runner 在锁内 realpath 校验 frozen PathPlan 根与 linked-worktree
  root/per-worktree git dir/git common dir/当前 symbolic ref/reflog，生成 Runner-owned
  task-scoped Seatbelt profile 并在其中启动 worker；linked-worktree 只允许 per-worktree git dir、
  common objects 与当前 branch 精确 ref/lock/reflog，禁止其他 refs/worktrees/common
  config/hooks/packed-refs 与整个主仓库；普通仓库不加能力；detached/bare/submodule、拓扑漂移与
  其他 ref 稳定 blocked；sandbox-exec 不可用时启动前返回 `host_containment_unliftable`，不弹窗、
  不消费 grant；Git metadata 不进入 Executor 内层 sandbox（--add-dir/allowWrite 同 outer 根），
  Claude 内层保持 sandbox.enabled/failIfUnavailable 与 Edit deny；
- Claude 历史上曾按 fixture 接入 MCP permission-tool / stdio can_use_tool+control_response
  版本能力矩阵；该准入路径已于 2026-09-01 清退，fixture 仅保留为历史回归证据，不再决定
  生产支持范围。worker 现在按原生协议结构选择 control path，并保留同进程 Approve/Deny 与
  transport-death 失效语义；
- 2026-09-01 `A7XB-001` 回归证明上述“未知版本 fail-closed”属于错误准入策略：配置从
  Claude `2.1.233` 更新至接口兼容的 `2.1.247` 后，任务在进程启动前被版本表秒拒绝，原生
  `can_use_tool` 根本没有机会产生。现已清退 CLI/SDK 版本白名单与 `--version` 准入；版本、
  发行方和绝对路径只作为诊断/身份记录。生产准入改为机械检查 SDK 协议成员
  （`ClaudeSDKClient.options`、`ClaudeAgentOptions.can_use_tool/cli_path/cwd/permission_mode/hooks`、
  `ToolPermissionContext.tool_use_id/suggestions`、Allow/Deny/PermissionUpdate 精确返回形状）；
  协议结构兼容的官方升级与 fork 默认支持，只有真实接口缺失、握手失败或运行中 transport
  丢失才分别返回 `permission_protocol_shape_unsupported`、
  `permission_protocol_handshake_failed`、`permission_transport_lost`。原生结构化事件仍是唯一
  审批权威，文本、stderr、callback 与退出码仍不得创建审批；
- Hermes canary：Agent callback / stderr / 退出码只作为诊断（`record_agent_callback` 仅持久化
  completion intent），不能创建 input/grant；本任务真机执行中两次 python3/pip 探测被 Hermes
  终端审批门以超时 fail-closed 拒绝，未产生任何 grant/input/continuation，证明 Hermes 不存在
  Claude 的审批循环；获批 continuation 后已补跑全部验证：定向 68 项与全量 1529 项 unittest
  通过，Ruff（src + 新测试文件）、compileall、`uv build` wheel/sdist（含三个新模块）与
  `git diff --check` 全部通过；
- 2026-08-30 官方 SDK transport 实施证据（`QJXH-001`，agent/hermes 本地提交
  `2d493cc` → `1aba319` → `50b4d4e` → 第四笔，未 push）：
  - 生产路径只剩官方 `claude_agent_sdk==0.2.142` `can_use_tool` transport；raw
    `ClaudePermissionPromptBroker`、shell `--permission-prompt-tool` 与 CLI resume 均
    不是生产路径；`can_use_tool` 在 worker event-loop 上 await ControlPlane 往返，
    同一 ClaudeSDKClient 进程/会话跨审批保持存活，并发第二个请求 fail closed；
  - transport 记录 in-flight `tool_use_id` 并把它交给 `record_transport_failed`，
    transport 死亡时精确失效该 pending request；已接受过的 native identity 全程去重，
    重复 `tool_use_id` 不产生第二个 permission input；审批 identity 绑定
    task/run/session/`tool_use_id`/fingerprint，escalation domain 与 host profile
    digest 顶层进入 ControlPlane，可信 approve 后相同 domain 再次阻塞收敛为
    `permission_escalation_ineffective`（零新增 input/grant/worker/continuation）；
  - 临时 full：consume 的一次性 grant 绑定到 transport，run 终态/崩溃/handoff/
    reassign 时精确撤销一次；explicit 与 inherited full 经冻结 flag→mode 映射以
    `bypassPermissions` 启动，safe/inherit 保持 SDK default 让 `can_use_tool` 继续触发；
  - hooks feed（PreToolUse/PostToolUse/PostToolUseFailure）只持久化脱敏结构化记录，
    verify 仅接受结构化 PostToolUse success；配置拆分 `tools`（可见性）与
    `auto_approve_tools`（显式预批准），legacy `allowed_tools` 仅警告式双读为 `tools`，
    setup/registry 同步；doctor `permission.claude_sdk` 投影保持脱敏；
  - 修复 SDK 环境门平台标签（`Darwin`→`macOS`）使被探针 macOS arm64 tuple 通过自身
    gate；live probe 于 2026-08-30 在隔离 venv 重跑通过（allow 原样执行、deny 零执行、
    同一 session 三阶段存活；证据 `probe_evidence_rerun_2026-08-30.json`）；
  - 定向 unittest：transport 19、sdk_transport 17、packaging 5、permission_runtime+
    seatbelt 75、phase10c/d 176、permission_modes/canary 32 全部通过；Ruff（src+tests）、
    compileall、`uv build` wheel/sdist、wheel metadata（`Requires-Dist:
    claude-agent-sdk==0.2.142; extra == "claude"`）与 `git diff --check` 全部通过；
  - 剩余门禁：deployed 三来源 canary（explicit/temporary/inherited 真机 full 生效）、
    `PERM-104-002-R1` 详情回归在合并后执行；P0 在 canary 通过前不宣布关闭；
- `PERM-104-002-R1` 已接入本项验收：首块审批与合法 approval identity 的脱敏只读
  Details/View Details 投影回归在 `PERM-104-002` 收敛验收后执行，不单独占用 Wave。

2026-08-31 `F8MJ-001` 会话级批量审批（session-scoped tool rule）实施证据（agent/hermes 本地提交，未 push）：

- ZF5R-001 固定失败回归基线：Claude 官方 session `95260a61-51b4-484b-a231-f8af12d5a862`
  （run `claude-ZF5R-001-03745cf3`）在单动作审批闭环下对同 session 顺序产生 17 次独立 Bash
  `can_use_tool` 请求；`call_d4307bd5d90e4640b9f3a3d7` 经 CLI 单动作 approve 且其结构化
  PostToolUse 成功落盘（blocked=false），随后同一 session 又产生新的独立 Bash 请求
  `call_79d1691b0af6467080776263` 并因响应事务竞态 stale 失败——证明一次 approve 只豁免一次
  动作，同 session 后续同类动作仍需逐条弹窗（这正是本项要修复的批量审批缺口）。
- 新增 `agentbc task respond <task-id> --input <input-id> --approve-tool <tool-matcher>
  --scope session`：CLI 新 flag 与 `--message/--approve/--deny` 互斥且 `--approve-tool` 必须
  恰好搭配 `--scope session`；新模块 `agent_bridge_connect/session_tool_rules.py` 承载权威
  校验（input.type=permission、native_event=claude_sdk_can_use_tool、
  control_path=sdk_control_transport、status=answered、scope=single_action、
  task/executor_run/官方 session/request_id/tool_use_id/request/action fingerprint、
  escalation_domain、profile digest 全绑定），缺失/stale/mismatch/wildcard-all/不支持
  executor/兼容 full fallback/非 native 请求均以稳定错误码 fail closed
  （`session_rule_input_missing|stale|not_permission|not_native`、
  `session_rule_identity_mismatch`、`session_rule_matcher_invalid|wildcard`、
  `session_rule_executor_unsupported`、`session_rule_already_active|replay_conflict`）；
  matcher 语法只收窄 `Tool(command-prefix*)` / `Tool(content)`，裸工具名、`*`、`Tool(*)`
  一律拒绝；既有 `--approve`/`--deny` 单动作行为零改动。
- Runner 新 op `respond_session_rule` 与 `respond_task` 的 `tool_matcher/session_scope`
  扩展：session rule 只在控制面 native accept 事务提交后的同一次响应内签发，签发失败
  统一 `session_rule_issue_failed` 恢复，不启动第二个 worker。
- SDK 面板：先以 live-compatible probe（`scripts/live_probe_perm104_session_rule.py`）确认
  安装版 `claude-agent-sdk==0.2.142` + Claude CLI `2.1.247` 的官方 type shape
  `PermissionUpdate(type="addRules", rules=[PermissionRuleValue(toolName, ruleContent)],
  behavior="allow", destination="session")` 经 `PermissionResultAllow.updated_permissions`
  在同一 live session 生效：匹配命令零二次审批直接执行、非匹配命令仍触发 `can_use_tool`
  且 deny 零执行、`.claude/settings*.json` 零写入；probe 证据冻结于
  `tests/fixtures/executor_runtime/matrix/claude/live_probe_sdk_session_rule_2026-08-31/`。
  Transport 仅在该形状与冻结契约一致时（漂移返回
  `claude_sdk_session_rule_contract_invalid`）把 rule 附着到当前 live transport
  （`attach_session_rule`，仅内存、随 transport 死亡失效），且只对工具名匹配的已批准
  allow 结果附加该 update；不启动/恢复第二 Claude 进程、不注入 `--allowedTools` 启动参数、
  不持久化任何 user/project settings、不转换为 full/bypassPermissions。
- 任务级 rule receipt 与生命周期：`agentbc.session_tool_rule` v1 receipt 记录 matcher、
  session scope、`selection_source=cli_native_approval`、脱敏 binding digest、时间戳与
  active/revoked 状态；同 input 重放幂等（零新增 rule/事件）、冲突重放
  `session_rule_replay_conflict`、同 session 已有 active rule 时
  `session_rule_already_active`；completed/failed/needs_recovery/cancelled/retry/reassign/
  handoff 终态路径统一 `revoke_session_tool_rule`（terminal 标记永不改写 Claude 配置文件）；
  status 公共视图只投影脱敏 receipt（`session_rule_public_projection`），binding 标识符
  不外泄。
- PostToolUse 对账（ZF5R-001 缺陷修复）：新增 `reconcile_block_success`，在 runtime
  verification 选中 anchor 的结构化 PostToolUse 成功事件后，把该批准动作在 block ledger
  的精确条目推进为 `execution_result="succeeded"`；此后同 fingerprint/domain/profile 的
  动作不再被错误收敛为 `permission_escalation_ineffective`（blocked=false 记录为已验证
  执行），ledger 对账幂等且 deny 条目永不 reconcile。
- 回归：新增 `tests/test_perm104_002_session_tool_rules.py` 56 项（CLI 解析/互斥、pending
  input 权威、matcher 校验、SDK 序列化对齐 live probe、单 session 复用、非匹配工具不继承、
  duplicate/replay 幂等、终态撤销矩阵、Runner reload 幂等、PostToolUse 对账收敛消失）；
  live probe verdict=pass（6/6 checks）。

2026-08-31 controller 收尾修订（`F8MJ-001` correction `I-001`）：

- `--approve-tool Bash --scope session` 明确定义为当前可信 native Bash 阻塞所授权的
  session-scoped 工具类型规则；CLI 的裸工具名与全局 matcher `*` 不同。CLI 输入 `*`、
  `Tool(*)`、跨工具 matcher 继续 fail closed；仅在已绑定当前阻塞工具后，Adapter 按 Claude
  官方等价语义把裸 `Tool` 编码为 SDK `PermissionRuleValue(tool_name=Tool,
  rule_content="*")`。receipt 新增 `matcher_kind=tool_type|command_pattern`，仍绑定
  task/run/官方 session/request/tool_use/fingerprint/profile，并在终态撤销。
- 修复生产接线竞态：旧实现先写单动作 accept 唤醒 SDK，再签发 receipt，且
  `attach_session_rule()` 只在测试中调用，真实 live transport 无法取得 rule。修订后 Runner
  先验证并签发 receipt，再把完整绑定的 rule 原子写入同一 control response；SDK callback
  对 task/run/session/request/tool_use/tool-name 全量复核后，才返回携带官方
  `PermissionUpdate(addRules, destination=session)` 的 allow。
- live probe 不再硬编码 Claude `2.1.247`，默认解析 Runner 配置中的
  `executors.claude.command`，只允许以 `AGENTBC_PROBE_CLAUDE_BIN` 显式覆盖；证据必须记录
  精确绝对路径和 `--version`。controller 先证明 `rule_content=null` 在生产 Runner
  `2.1.233` 中不会抑制第二次同类回调，随后依据官方 `Tool` 等价于 `Tool(*)` 的规则语义改为
  `rule_content="*"`。为避免给联网 probe 放行全部 Bash，最终实机证据使用两个无副作用的
  in-process MCP 工具：同一官方 session 中 matching 工具连续执行两次但只有第一次进入
  `can_use_tool`，distinct 工具重新进入 callback 且 deny 零执行，settings 零写入，6/6
  checks pass；证据冻结于 `probe_evidence_tool_type_2.1.233_2026-08-31.json`。重新封包部署后
  仍需以真实 AgentBC task 验证 Desktop 弹窗到 CLI `--approve-tool Bash --scope session` 的
  端到端交互，方可关闭 P0。

2026-08-31 PERM-104-002 9ZEV-001（native permission passthrough 与 legacy matcher 退役）：

- `agentbc.approval` v2 choice broker 落地（`approval.py`）：receipt 绑定
  authority（executor/protocol/protocol_version/method）、broker/provider request ID、
  native item ID、fingerprint、每个 offered choice 的 opaque handle（`opt-*`，由
  request id + offered digest 派生，仅对该 exact request 有效）、selection
  （handle/native_option_id/kind/source/at）。v1 receipt 双读保持 valid；v2 拒绝
  flattened `--approve`/`--deny`（`native_permission_choice_required`）。identical
  replay 幂等、conflict/cross-identity/unknown handle 全部 fail closed。Raw payload
  只存保护态任务控制；公共视图仅暴露 sanitized labels + opaque handles。
- CLI 新增 `task respond --permission-option <handle>`（与 message/approve/deny 互斥）；
  Runner `respond_task` 映射 handle → control plane v2 响应；同 worker/同 live
  session 返回，不创建 grant/worker/continuation/mode change。`full` 保持 task-start-only，
  永不出现在 choice popup。
- Exact adapters：Codex 只返回 schema 支持的 `accept`/`acceptForSession`/`decline` 或
  permissions turn/session 响应（原 ID；amendments/cancel 不可选）；Claude 只信任 SDK
  `can_use_tool`：deny → `PermissionResultDeny`、once → `PermissionResultAllow`
  （原始 input、无 updated_permissions）、session → 仅当 callback suggestions 是完整
  allow-rule bundle（无 persistent destination、无 setMode bypassPermissions）时，将其未决
  destination 机械绑定为 session 并原样保留规则内容，绝不生成 matcher；Hermes 保留 ACP request/session/tool-call/options 并
  回传选中的原 optionId（order/label 无关），移除 allow_once-only 限制。
- 动态弹窗（macOS/CLI 同一 Runner API）：一级固定 View Details / Deny / Approve，
  Approve 只进入二级 Back / Once / This Session；只有 Deny、Once、This Session 携带
  exact native handle 并回传，View Details、Approve、Back 仅导航、零回执。一个绝对
  deadline 覆盖两级交互；无 allow default；close/timeout 恰好发送一次 exact denial。
- Legacy matcher 退役：`session_tool_rules.py` 收缩为 tombstone
  （`legacy_session_tool_rule_removed`），matcher 语法/rule API/lifecycle/Runner rule
  routing/Claude synthesized rules/Hermes one-shot 常量/Codex session-decision 禁止全部移除；
  `--approve-tool/--scope session` 隐藏为一次发布的 nonfunctional tombstone；
  `legacy_permission_cutover_blocked` 扩展覆盖 active v1 waits、session rules、
  approve-tool waits、grants、compatibility-full continuations（列 task IDs/reasons）；
  terminal history 只读、v1/v2 双读、只写 v2。
- 测试：新增 `tests/test_perm104_002_v2_broker.py` 29 项（v1/v2 读写与脱敏、handle
  binding/replay/cross-identity 拒绝、Codex exact once/session/deny + amendment 拒绝、
  Claude bundle mixed/persistent/bypass/unknown 拒绝、Hermes exact optionId、
  migration gate 扩展、tombstone）；codex control/production 47 项更新为 v2 handle
  响应语义；matcher probe 替换为 native-choice probe
  （`scripts/live_probe_perm104_session_rule.py` 重写）。

2026-08-31 `RAXT-001` 实施与证据边界（`agent/codex` 本地提交，未 push）：

- 保留 `WDAB-001` 的关闭记录，不改写历史因果顺序：官方 session
  `75706277-a80f-42e6-a6f5-f82dded33364` 只发出一次 Bash；在任何 native request identity
  建立前先得到 `session_receipt_missing`，之后才出现
  `input_required/requested_permission=full` callback，且在 Approve 前关闭。因此 WDAB 证明的是
  native request 在 popup 前创建失败；后来的 full popup 与 worker crash 是下游现象，不是成功证据。
- RAXT-001 已将 Claude SDK wiring 改为 receipt-before-query/can_use_tool：官方预分配或同 session
  resume receipt 通过 `record_session_started` 且 task/run/session/resumed/source 校验成功后，才绑定
  hooks、创建 transport、构造 prompt/options 并进入 SDK query；receipt/control/transport 初始化失败
  统一走结构化 `needs_recovery`，不创建 input 或 full grant。
- native `can_use_tool` 只产生一次绑定 task/run/session/tool_use_id/request_id/action fingerprint/
  escalation domain/profile digest 的 `single_action`；Approve 原样返回同 session input，Deny/timeout/
  transport death 使精确 request 失效；model callback、requested_permission、stderr、退出码和普通
  access error 只作诊断，不能创建 permission input、grant、worker 或 continuation。相同 fingerprint/
  domain/profile 在批准后收敛到 `permission_escalation_ineffective`。
- contained worker 只写 task-scoped record/event/progress/control/temp/report；Runner 负责
  `task_index.jsonl`/`TASK_INDEX.md` refresh。spawn/start/activation failure 会回收进程、清理 profile、
  标记 recovery、阻断 runtime receipt、撤销 grant、失效 native input 并清除 worker/run 引用。
- `CMF2-001` 使用尚未替换的旧安装包复现了自举缺口：contained worker 在 claim/start 阶段写全局
  index，被 Seatbelt 拒绝后模型又生成兼容 full callback，Approve 只产生未消费 grant，重启 worker
  仍在同一点失败。controller 已取消该任务并直接补齐 Codex App Server native-authoritative prompt/
  terminal routing，以及 `control_events` 的终态有界投影；不得把 CMF2 popup 或 grant 视为权限成功。
- 本轮专项回归见 artifact root 的 `RAXT-001_IMPLEMENTATION_NOTES.md` 与
  `tests/test_perm104_002_r3.py`。本地单元回归不等于发布门禁：仍需支持宿主路径的真实 Seatbelt
  probe、SDK live blocking/Approve/Deny/timeout/death 重启矩阵，以及三来源 full 生效 canary。
  `SESSION-104-001` 本节不修改，仍按原有 `SESSION-104-001_CANARY_EVIDENCE.md` 结论处理；
  `PERM-104-002` 继续保持开放。

### 4.5 `FLOW-104-001`：handoff 结构化多 steps

- `agentbc task handoff` 接受与根任务一致的结构化 `steps[].description` 输入；
- handoff 继续继承 chain、PathPlan、artifact root、权限/资源/session 冻结策略；
- 自由文本 message 只描述上下文，不隐式创建 Step 2+；message 与 steps 的优先级必须唯一；
- dispatch 前校验空 steps、重复/缺失 ID、非连续编号和文本中的歧义正式编号；
- task packet、Prompt 和 callback 示例只能从持久化 declared steps 生成；
- callback 出现未知、重复、缺失或非完成 step 时继续 fail closed，不猜测映射；
- 旧 handoff 无结构化 steps 时继续按单 step reader 运行，不批量迁移历史 task.json。

测试覆盖单 step、多 steps、嵌套编号、correct/retry/recover、跨 Executor handoff、旧任务双读及
callback invalid。

### 4.6 `FLOW-103-001`：权威单调 progress receipt

- receipt 至少绑定 task、executor run、官方 session、declared step ID、单调状态、序号与证据来源；
- `done` 只能由运行期间已落盘并校验的 receipt 推进；乱序、重复或重放不能回退；
- permission wait、资源耗尽、transport recovery 和 terminal callback 只合并现有进度；
- receipt 不从自然语言 summary、choice label、耗尽后的 callback 或退出码推断；
- progress receipt 不能单独把任务标记 completed，terminal authority 保持不变；
- status/report/notification 使用同一 projection，并只显示脱敏证据质量；
- 旧任务没有 receipt 时保持旧语义，不伪造历史完成进度。

测试覆盖部分完成后资源耗尽、两次耗尽、permission wait、recover/replay、乱序/重复、session 漂移、
旧任务以及 Codex/Claude/Hermes/fake Executor。

### 4.7 `FLOW-104-002`：终态投递与 record budget

终态流程拆为独立、幂等、可重放阶段 receipt：

1. terminal state committed；
2. report generated/compacted；
3. public index refreshed；
4. file notification delivered；
5. UI notification delivered；
6. main/auxiliary session cleanup requested/completed。

约束：

- report、index 或 record compact 失败不得阻止至少一次 terminal notification；
- notification 失败不得回写、覆盖或伪造任务业务终态；
- Runner 重启只重放缺失阶段，不重复已经确认的同一 terminal event；
- input history、run intervals、错误和通知详情进入 task record 前必须有条数与字节上限；
- 超限时保留首条、末条、总数和稳定摘要，完整诊断进入有界事件文件；
- cleanup 失败形成独立 blocker/receipt，不把已经 failed/completed 的任务改回 active；
- callback invalid、report 不可写、record 超限、DialogNotifier 失败和并发终态均必须产生可行动证据。

### 4.8 `SESSION-104-001`：Codex CLI/Desktop 双入口临时会话清理

> 2026-08-29 最终状态：本项已通过 `1.0.4A` 发布门禁。`QV46-001` 与 `NMY4-001`
> 均对绑定任务的精确官方主会话取得 archive、delete acknowledgement，并在 CLI、Desktop
> backend 及当前应用 active/archived 列表确认 absent；完整证据见
> `SESSION-104-001_CANARY_EVIDENCE.md`。以下未完成结论仅保留为历史过程记录。原生派生子会话
> 未实际触发的问题已降级为 `PROTO-105-001` P2 候选，不重新打开本项。
>
> 2026-08-28 `DEWX-001` continuation status：本项仍为 P0 未完成。`3XAZ-001` 在 Step 1 因
> `permission_denied_by_user` 失败，完成 `0/5` 且没有合法 final callback；`WSR8-001` 是独立
> primary canary，历史 report 的 CLI/Desktop 状态为 unknown；`8XF5-001` 因
> `completion_marker_missing` 失败，不能把 `SESSION_CHILD_OK` 当作原生派生会话证据；`HHWC-001`
> 因 `executor_exit_nonzero` 失败。当前 0.147.0 的 frozen fixture 缺少
> `collabAgentToolCall`、`spawnAgent`、`receiverThreadId`，所以 production collaboration_spawn
> wiring 保持 disabled。App Server backend `thread/list` 缺失只能证明 backend absent；当前没有受支持
> Desktop 实时读取/重启后复验通道，必须记录 `codex_desktop_verification_unavailable`，不得将本项关闭。
>
> 2026-08-29 `QEEY-001` 修订：release gate 改为「官方 archive → delete 命令闭环」。
> `thread/archive` RPC 必须先发送并取得绑定同一 UUID 的 RPC acknowledgement，之后才允许发送
> `thread/delete`；archive 未确认时 delete 调用次数必须为零。`thread/archived` 与
> `thread/deleted` 仅作 advisory。新连接 `thread/read` 与分页 active/archived 全 source-kind
> `thread/list` 观察保留为非门控 diagnostics；当前 Codex Desktop 刷新延迟被接受且不阻塞成功；
> `desktop_live` 在 archive-then-delete 策略下记为 `not_applicable`。私有库扫描、GUI 自动化、
> 强制刷新/重启、dispatcher 清理与无关会话清理依旧禁止。排序依据：`SQKX-001` 真实 canary 证明
> 先 delete 后 archive 会返回 target-not-found（详见 `SESSION-104-001_CANARY_EVIDENCE.md`）；
> 该历史 canary 只解释排序，不证明新实现。

- 只清理由 AgentBC Executor 创建并从官方 early receipt 取得精确 ID 的临时会话；dispatcher
  conversation、用户会话、未登记会话和模糊名称匹配永不进入清理候选；
- 测试必须把同一官方 session ID 在 Codex CLI `resume` 入口与 Codex Desktop 恢复列表中的可见性
  关联起来；若上游不提供可验证关联，记录 `unsupported`/blocker，禁止扫描或改写私有数据库；
- 发布门控是官方 archive→delete 命令闭环：同一官方 App Server 连接上先 `thread/archive` 并等待
  其 RPC acknowledgement（`thread/archived` 通知只作 advisory），确认后才发送 `thread/delete`；
  archive 超时、传输中断、RPC 错误或 target-not-found 都必须以稳定 archive 错误码 fail closed，
  且 delete 调用次数为零；`thread/delete` 的 RPC acknowledgement 同样强制，`thread/deleted`
  通知仍为 advisory；
- 新连接 `thread/read` 缺失与覆盖全部 source kind、分页和归档分区的 `thread/list` 缺失保留为
  非门控 diagnostics：它们不阻塞成功，也不作为成功条件；Desktop 当前刷新延迟被接受；
- cleanup receipt v4（向后兼容）新增有界 `commands.archive` 与 `commands.delete`
  status/checked_at 条目，状态只允许 `not_requested`、`acknowledged`、`confirmed`、`failed`、
  `unverified`、`not_applicable`；Codex cleanup 只有两条命令均 `acknowledged`/`confirmed` 才能
  `succeeded`；部分命令证据（尤其已确认的 archive）随 receipt 与 cleanup 事件持久化，重试与
  Runner 重启不得丢失；已登记派生会话按同一规则清理，并保持 primary-first 与最深/最新顺序；
- 显式 `cli/direct` fallback 只保留 `official_session_delete` 策略名，永不冒用
  `official_session_archive_then_delete`；CLI exit 0 仍只是动作证据；
- 覆盖 completed、failed、cancelled、permission Deny/timeout、transport lost、Runner/Desktop 重启，
  并验证同一 receipt 重放幂等；
- 每个用例同时创建一条非 AgentBC 控制会话作为保留哨兵，证明清理没有扩大到 dispatcher 或用户会话；
- status/report/doctor 显示 cleanup capability、strategy、attempt、命令确认状态、验证诊断与稳定
  error code，不泄露私有会话路径、原始 prompt 或用户会话清单。

完成证据必须包含官方 ID 绑定、cleanup receipt v4 命令闭环（archive 与 delete 均 acknowledged/
confirmed）、持久化的部分命令证据与保留哨兵；命令闭环缺失任一 acknowledgement 不得写 `succeeded`。
Desktop 恢复列表的最终肉眼确认是延迟性诊断，不阻塞发布门控。

历史实现证据（2026-08-27）：`agent/codex@d18697f` 完成 cleanup receipt v2、官方 UUID 绑定、
App Server 删除/新连接 read 验证、status/report/doctor 同源投影与 fail-closed 错误；
`private/integration@8aa60a6` 完成初次集成，`9fce6b6` 补齐生产 Desktop `thread/list` 全 source kind、
分页和 archived/non-archived 验证。该段只描述历史实现基线，不构成本次双入口真机验收。

`DEWX-001` continuation 的可验证结果（历史基线）：cleanup receipt 曾升为向后兼容 v3，分别持久化
`cli`、`desktop_backend`、`desktop_live`，公共投影保留 `desktop` 聚合字段；`transport=auto` 的有官方
receipt 路径统一使用 App Server，只有显式 `cli/direct` 允许 CLI action fallback，CLI exit 0 不产生
cleanup success。真实 0.147.0 单父 timeout canary 取得官方 thread ID；App Server delete 返回成功，
新连接 `thread/read` 返回缺失，active（3 页）与 archived（1 页）的全 source-kind `thread/list` 均不含
该 ID。CLI `delete --help` 清理前后均为 exit 0，但仅作 action capability evidence。该历史 canary 只
证明当时的 delete-only 链路，不构成 archive-then-delete 门控的验收。

`QEEY-001` 实施结果（2026-08-29）：receipt 升为向后兼容 v4；`codex.session_cleanup` 能力组扩展为
`thread/archive`、`thread/delete`、`thread/read` 与 advisory `thread/archived`、`thread/deleted`；
新增策略 `official_session_archive_then_delete` 与 archive 稳定错误码
（`codex_session_archive_failed`、`codex_session_archive_invalid_session_id`、
`codex_session_archive_target_missing`、`codex_session_archive_timeout`、
`codex_session_archive_transport_lost`）；App Server 清理路径先 archive 后 delete，零确认即零删除；
status/report/doctor 投影命令证据。

### 4.9 `FLOW-104-003`：Failed 任务 retry 与 handoff

- 为 failed terminal receipt 增加稳定 failure taxonomy 与 `allowed_next_actions` 投影，至少区分可重试的
  transport/临时环境失败、需要 recovery 的 session/lease 失败，以及必须 correction/handoff 的合同失败；
- `agentbc task retry <id> --step <n>` 可作用于 current chain head 的 failed 任务，但只重置目标 failed/
  blocked step；已完成 step、progress receipt、旧 terminal/report/error/attempt 审计不可删除或回退；
- retry 保留任务冻结的权限/资源策略，撤销旧 grant 并关闭旧 RunLease。原 session 已成功清理、失效或
  不可安全恢复时必须创建新官方 session，不得恢复已删除 session 或猜测私有 ID；
- failed current chain head 可作为 handoff 源创建新 iteration；新任务继承 PathPlan/artifact lineage，
  明确引用失败 report、未完成 steps、已有进度和恢复目标，但不把源任务改写成 completed；
- retry/handoff 前做原子 preflight：拒绝 stale/non-head、活动 lease、未决 permission input、重复 dispatch、
  不可读 report 和不一致 lineage；并发请求至多创建一个新 run 或一个新 iteration；
- status/report/CLI 错误必须给出可执行的下一步和稳定拒绝码；`retryable=false` 时不能盲目 retry，
  handoff 也不能绕过 PathPlan、权限或 callback 合同；
- 覆盖同 Executor retry、跨 Executor handoff、retry 后再失败、handoff 后 stale source、清理前后 retry、
  partial progress、多 step、Runner 重启和旧任务双读。

验收以新 run/iteration 的 RunLease、报告、严格 callback、产物与无重复 worker 为准；CLI 返回 accepted
或简单把 step 状态改回 pending 不算恢复成功。

### 4.10 `INPUT-104-001`：custom path 与外部输入附件

- `--customer-path` 继续唯一决定项目/产物根；显式 `--image` 或后续通用 `--input-file` 可以来自项目根
  之外，但只作为用户明确选择的只读附件，绝不自动变成额外 writable root；
- Runner 在原子 create/dispatch 内独立校验每个外部输入：必须是存在、可读、允许类型和大小的普通文件，
  拒绝目录、设备、socket、symlink/realpath 漂移、路径替换、超限数量与 dispatch 中途内容变化；
- 校验后由 Runner 导入到 AgentBC-owned、task-scoped、content-addressed 的不可变 input root，记录原始
  basename、媒体类型、字节数、SHA-256、来源类别和导入时间；源文件永不移动、覆盖或删除；
- Executor packet 只获得项目根和导入后的只读附件路径，不获得外部父目录权限；公开 status/report 默认
  不暴露原始绝对路径，诊断视图只显示脱敏来源与 hash 摘要；
- 任一附件失败时 create/dispatch 全部回滚，不创建 task/index/workspace/worker/RunLease，也不遗留部分导入；
- Codex 支持重复 `--image`，Hermes 保持当前单图限制；handoff 默认继承冻结的 input manifest，替换附件
  时生成新 iteration manifest，不回读可能已变化的源文件；
- customer path 内文件保持现有直读语义；外部输入导入是 Runner 官方能力，不能由 controller 预复制文件
  或临时扩大 containment 来模拟。

测试覆盖 custom path + 单/多外部图片、项目内外混合附件、同名不同 hash、symlink/TOCTOU、超限、
不可读文件、Runner 崩溃重放、handoff 继承/替换、脱敏以及失败零残留。真实 E2E 必须复现本次
`image input is outside task roots` 基线，并证明新合同下可以安全派发且项目外父目录仍不可访问。

## 5. 文件所有权与派发建议

| 工作包 | 首要所有权 | 禁止越界 |
| --- | --- | --- |
| `PROTO-104-001` | `tests/fixtures/`、协议 probe/fixture tests | 不修改生产 argv/parser 语义 |
| `ARCH-104-001` | 每次仅一个目标模块及 import compatibility tests | 不夹带 schema、状态机、文案变化 |
| `PERM-104-001` | approval/permission decision、TaskService approval lifecycle | 不改 handoff、update、notification pipeline |
| `PERM-104-002`（含 `R1`） | permission failure taxonomy、fingerprint/domain projection、三来源 full runtime capability 闭环；完成后 approval detail/DialogNotifier 回归 | 不新增 Git/path 穷举预检；不先做 UI 修复绕过阻塞与 full 生效闭环 |
| `FLOW-104-001` | handoff CLI/schema/task packet/prompt contract | 不放宽 callback validator |
| `FLOW-103-001` | progress receipt/store/projection | 不改变 terminal completion authority |
| `FLOW-104-002` | terminal delivery、reports、record budget、notifications/cleanup receipt | 不改变任务质量含义或权限策略 |
| `SESSION-104-001` | Codex cleanup adapter、session receipt、CLI/Desktop E2E fixtures | 不扫描/改写 Codex 私有会话库，不触碰 dispatcher conversation |
| `FLOW-104-003` | failure taxonomy、retry/handoff preflight、attempt/lineage projection | 不清空旧失败证据，不绕过 current-head/lease/PathPlan 合同 |
| `INPUT-104-001` | Runner PathPlan/input manifest、CLI attachment parsing、atomic staging tests | 不扩大 writable root，不由 controller 复制附件绕过授权 |

派发前必须为每个任务写明 owned files、公共接口、不可修改文件、基线 commit、定向测试和唯一 callback。
共享文件冲突时按上表依赖串行，不以“先合后修”处理并行冲突。

## 6. 测试门禁

### 每个工作包

- 新增失败复现先红后绿；
- affected unit、Service/Runner integration、fixture 参数化测试通过；
- Ruff、compileall、`git diff --check` 通过；
- 工作树干净、提交单一职责、无越界文件；
- public status/report/log 不泄露 token、raw argv、私有 session path 或未脱敏详情。

### 每个 Wave

- 全量 unittest 与 package build/Twine 通过；
- 三 Executor 当前支持版本 probe healthy；
- 历史 task fixture 双读、recovery/replay 和 record budget 回归通过；
- Runner/CLI/Skill build identity 匹配，Doctor blocker 为 0；
- 没有遗留 worker、RunLease、approval grant、测试会话或临时 customer artifact。

### 真实 E2E

- Codex、Claude、Hermes 分别完成 native Approve、Deny、不可升级 blocked 和 session cleanup；
- `PERM-104-002` 对人工显式 full、inherit/safe 临时申请 full、handoff/retry 权限继承 full 分别执行同类
  linked-worktree 提交与受管 progress 动作；三条路径必须实际成功、无第二弹窗、无阻塞和无权限降级；
- `PERM-104-002` 先证明不可升级动作最多一次审批；随后 `PERM-104-002-R1` 验证每条仍合法的可审批路径
  均有 `View Details`、内容脱敏、Back 不响应且总 deadline 不重置；
- Codex 临时会话在 CLI 与 Desktop 两个恢复入口清理前可定位、清理及重启后均不可恢复，同时保留哨兵存在；
- Failed current head 分别完成一次同 Executor retry 与一次跨 Executor handoff，并验证 stale/concurrent 请求拒绝；
- custom path 同时传入项目外单图/多图并成功原子派发，证明附件只读、父目录不可访问、失败无残留；
- handoff 单 step 与 multi-step 分别跨至少两个不同 Executor；
- progress canary 包含部分 step 完成后资源/permission/transport 阻塞；
- 资源耗尽 canary 必须触发 `RESOURCE-104-001-R1` 的 Desktop 弹窗；Approve 后同 Task、同 session、
  唯一 continuation 继续，CLI fallback 只作控制面兜底而不替代 UI 通过证据；
- Hermes terminal canary 必须触发 `FLOW-104-003-R1` 的正常完成、资源耗尽和 incomplete normal exit 三条
  路径，证明返回码 `0` 不替代 callback，且缺 marker 的部分进度可通过正式 retry/handoff 继续；
- terminal canary 注入 report 不可写、record 超限、UI notifier 失败和 Runner 重启；
- 不把弹窗出现、`accepted`、退出码 0 或聊天总结当通过证据。

## 7. Update、Homebrew 与发布回归

- local-alpha/PyPI managed update：latest、decline、digest mismatch、成功升级、Runner 启动失败恢复；
- Homebrew-owned `agentbc update` 继续只返回 `brew upgrade agentbc`，零写入 AgentBC board；
- Intel/Apple Silicon bottle install/upgrade、version/help、PATH 与 service identity；
- Homebrew 自身 Xcode/CLT doctor/test harness 问题继续作为环境诊断，不写入 AgentBC 依赖；
- session teardown/auxiliary cleanup 覆盖 success、Deny、timeout、transport lost、进程异常和重启；
- Codex session teardown 额外覆盖 CLI/Desktop 双入口、应用重启、索引刷新与非 AgentBC 会话保留；
- GitHub tag、Release、Actions、PyPI、manifest、bundle 与 bottle SHA 必须独立一致；
- 发布候选不得包含私有手册、开发清单、失败证据、临时 CA/feed/tap 或 fault package。

## 8. 明确不做

- 不改变默认 `inherit` 与现有 safe/full/solo 原生权限继承；
- 不以强化 Prompt 代替审批机械判定；
- 不从普通 stderr、退出码或 callback 文案合成可信 permission/progress/session receipt；
- 不新增 linked-worktree Git 预检、控制器提交阶段或扩大 git common dir；
- 不提供用户主动 update rollback 命令；
- 不硬编码 Homebrew Python 小版本，不声明 AgentBC Xcode 依赖；
- 不扫描或批量删除 Executor 私有会话；
- 不通过修改 Codex Desktop 私有数据库、缓存或索引来伪造双入口清理成功；
- 不把外部附件父目录加入 Executor writable roots，不接受模糊目录输入，不由 controller 预复制绕过 Runner；
- 不在本版新增 OpenCode、Cursor、原生 Windows/Linux Runner、GUI、Webhook/Email 或跨机派发；
- 不移除 protocol v1 reader，不做未经 characterization 保护的全模块重写；
- 不移动、覆盖或重建 `v1.0.3A2` 与 PyPI `1.0.3a2`。

## 9. 完成定义

`1.0.4A` 只有同时满足以下条件才能进入发布：

- 十个权威开发项均有实现、定向/全量测试、真实 E2E 和合入证据；
- Core 不再依据 Prompt/Agent 自述选择审批渠道；native Deny 零执行且不产生第二 worker；
- `PERM-104-002` 证明人工授予、临时申请与权限继承的 full 均通过权威 runtime capability receipt 真实
  生效，声明范围内动作无权限/PathPlan/宿主 containment 阻塞、无重复审批且无静默降级；
- 不可升级阻塞最多一次审批并稳定 blocked；之后 `PERM-104-002-R1` 证明所有仍合法的权限弹窗稳定
  提供脱敏只读详情，Details/Back 不改变审批状态或 deadline；
- Codex Executor 临时会话经 cleanup 后在 CLI/Desktop 双入口及重启后均不可恢复，dispatcher 与保留哨兵不受影响；
- Failed current head 的 retry/handoff 有稳定选择、原子 preflight、完整旧证据和唯一新 run/iteration；
- custom path 可安全组合项目外只读附件，input manifest 可复验、失败零残留且不扩大项目权限；
- handoff multi-step 在 dispatch 前完成合同校验，callback 严格一致；
- progress receipt 单调且不被资源/permission/terminal 覆盖回退；
- `RESOURCE-104-001-R1` 证明资源耗尽 input 在 Desktop 稳定显示、决策幂等、应用/Runner 重启可重放，
  且 CLI 响应与 UI 使用同一持久化 input；
- `FLOW-104-003-R1` 证明 Hermes 的 terminal reason 与 turns receipt 可机械判定，返回码 `0` 且缺失
  callback 时不会误报完成、丢失部分进度或陷入不可继续的 failed 终态；
- report/record/notification/cleanup 任一阶段失败时其余阶段仍可独立完成或重放；
- Update、Homebrew、session cleanup 和 `1.0.3A` 权限行为无回归；
- integration 与三个 agent 分支干净，Runner identity match，Doctor blocker 为 0；
- release candidate 的 GitHub/PyPI/bundle/bottle/manifest SHA 与 tag commit 可复验；
- 用户完成最终 go/no-go，之后才创建不可变 `v1.0.4A` tag 和 PyPI `1.0.4a1`。
