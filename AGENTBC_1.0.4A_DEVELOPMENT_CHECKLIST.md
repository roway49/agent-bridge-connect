# AgentBC 1.0.4A 需求开发清单

> 制定日期：2026-08-26
> 状态：需求冻结与开发规划阶段，尚未开始实现
> 目标版本：AgentBC `1.0.4A` / Python `1.0.4a1`
> 来源基线：`private/integration@01f3ce1`
> 已发布基线：`v1.0.3A2@62757a4`；公开 Formula 收口 `public/main@87c4bca`
> 上版归档：`AGENTBC_1.0.3A_DEVELOPMENT_CHECKLIST.md`
> 架构依据：`AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md`

## 0. 版本目标

`1.0.4A` 不再扩展 AgentBC 的权限等级或支持平台，而是把 `1.0.3A` 已经可用、但仍依赖
Prompt、共享大模块或脆弱终态顺序的控制链变成 Core-owned、可机械验证、可重放的合同。

本版只承担七个权威开发项：

1. `PROTO-104-001`：三 Executor 版本化协议 fixture matrix；
2. `ARCH-104-001`：在 characterization 保护下进行局部机械拆分；
3. `PERM-104-001`：审批资格、Deny 和 fallback eligibility 机械判定；
4. `PERM-104-002`：阻塞来源域与不可升级动作收敛；
5. `FLOW-104-001`：handoff 结构化多 steps；
6. `FLOW-104-002`：终态通知、报告、record budget 与 cleanup 解耦；
7. `FLOW-103-001`：从 1.0.3A 转入的权威单调 progress receipt。

Update、Homebrew、session teardown、auxiliary session cleanup、三 Executor native approval 和
`inherit|safe|full` 不是本版重做项，只作为不可回归基线。

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
| `PROTO-104-001` | P0 / 首项 | 上游 CLI version/help/argv/event 漂移只能靠临时补测试 | 三 Executor 完整版本化 fixture、capability matrix 和未知组合 fail-closed | 无 |
| `ARCH-104-001` | P1 / 按域执行 | Service、Runner、CLI、approval、notification 责任仍集中 | 每个功能项先完成对应窄模块机械拆分，公共 API/CLI/磁盘行为不变 | `PROTO-104-001` |
| `PERM-104-001` | P0 | native Deny 后 Agent 仍可用 Prompt/callback 请求 full 并启动第二 worker | 审批渠道和 fallback 完全由可信 transport event 与 Core policy 决定 | permission fixtures；对应 ARCH slice |
| `PERM-104-002` | P0 | 已批准动作仍被宿主 containment 拒绝时会重复弹窗、grant 和 continuation | 稳定 fingerprint + escalation domain；不可升级阻塞最多一次审批并直接 blocked | `PERM-104-001` |
| `FLOW-104-002` | P0 | report/record 超限可跳过终态通知和 cleanup receipt | terminal、report、notification、cleanup 独立且可重放，通知不被报告失败吞掉 | terminal fixtures；对应 ARCH slice |
| `FLOW-104-001` | P1 | handoff 只能声明一个 step，自由文本多步骤直到 callback 才失败 | handoff 原生结构化 steps、dispatch 前预检、严格 callback 一致性 | schema fixtures；对应 ARCH slice |
| `FLOW-103-001` | P1 / 跨版转入 | 资源耗尽或系统终态覆盖 callback 时会把真实部分进度回退 | task/run/session scoped 单调 progress receipt；所有公共视图同源 | `FLOW-104-001` 的 declared steps |

## 3. 开发顺序与并行边界

### Gate 0：冻结基线

- integration、三个 agent 分支、CLI、Runner、三平台 Skill identity 一致且工作树干净；
- `agentbc doctor --json` blocker 为 0，无 active/input_required/needs_recovery 历史任务阻塞开发；
- 固定支持的 Codex、Claude、Hermes 版本范围及真实 probe 输出；
- 运行 `1.0.3A` 权限、session、Update/Homebrew、发布与全量测试，保存基线结果；
- `PROTO-104-001` 完成前，不修改 Executor argv、event parser、approval/session capability gate。

### Wave 1：协议面与机械拆分

1. 完成 `PROTO-104-001`；
2. 按功能域分别建立 characterization tests；
3. 每次只拆一个责任模块，并以独立提交完成对应 `ARCH-104-001` slice；
4. 机械拆分提交不得同时改变 schema、状态机、权限语义、CLI 文案或通知行为。

建议拆分顺序：

- approval decision service；
- terminal delivery coordinator；
- handoff/declared-step contract builder；
- progress receipt projector；
- Doctor collectors 与 Runner IPC handlers；
- update service 只在上述主线完成且测试证明有必要时拆分。

### Wave 2：P0 控制面修复

- `PERM-104-001` 与 `FLOW-104-002` 可在各自机械拆分完成后并行；
- `PERM-104-002` 必须在 `PERM-104-001` 的 Core-owned approval decision 落地后实施；
- 三项共享 `service.py`、`runner.py`、record schema 或 notification projection 时，不并行写同一文件，
  由 integration 先冻结公共接口再派发。

### Wave 3：结构化流程与权威进度

- 先完成 `FLOW-104-001` 的 declared steps schema、旧任务双读和 dispatch preflight；
- 再完成 `FLOW-103-001`，progress receipt 只能引用已持久化 declared step ID；
- 两项不得放宽 callback 的未知/重复/缺失 step fail-closed 校验。

### Wave 4：集成与发布候选

- 运行三 Executor permission Approve/Deny/blocked、handoff multi-step、资源耗尽和 terminal failure
  真实 canary；
- 运行 Update、Homebrew、session teardown/auxiliary cleanup 全套回归；
- 构建 `1.0.4a1` 候选并完成 macOS bundle、PyPI dist、Homebrew Formula/bottle 与双机验证；
- 所有 Gate 完成前不创建公开 tag、GitHub Release 或 PyPI 文件。

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

### 4.4 `PERM-104-002`：阻塞来源域与不可升级动作

- 为动作保存脱敏稳定 fingerprint，不保存 raw argv、token 或私有绝对路径；
- 来源域至少区分 Executor policy、AgentBC permission policy、Runner PathPlan 和宿主 OS containment；
- 每次审批后记录精确执行结果和来源域是否变化；
- 同一 task/run/session/action 获批后再次命中相同 fingerprint 与不可升级来源域时，直接 blocked；
- blocked 收敛不得创建新 permission input、grant、worker、continuation 或重复通知；
- 只有可信 transport 证明来源域变化，或用户明确发起不同动作，才能创建新 request；
- linked-worktree 共用 Git store 只作为测试样例，不新增穷举 Git/path 预检或控制器提交阶段。

验收要求不可升级动作最多出现一次审批；真正可升级的 Executor 拒绝仍能在同 session、同 request
Approve 后精确执行。

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

## 5. 文件所有权与派发建议

| 工作包 | 首要所有权 | 禁止越界 |
| --- | --- | --- |
| `PROTO-104-001` | `tests/fixtures/`、协议 probe/fixture tests | 不修改生产 argv/parser 语义 |
| `ARCH-104-001` | 每次仅一个目标模块及 import compatibility tests | 不夹带 schema、状态机、文案变化 |
| `PERM-104-001` | approval/permission decision、TaskService approval lifecycle | 不改 handoff、update、notification pipeline |
| `PERM-104-002` | permission failure taxonomy、fingerprint/domain projection | 不新增 Git/path 穷举预检 |
| `FLOW-104-001` | handoff CLI/schema/task packet/prompt contract | 不放宽 callback validator |
| `FLOW-103-001` | progress receipt/store/projection | 不改变 terminal completion authority |
| `FLOW-104-002` | terminal delivery、reports、record budget、notifications/cleanup receipt | 不改变任务质量含义或权限策略 |

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
- handoff 单 step 与 multi-step 分别跨至少两个不同 Executor；
- progress canary 包含部分 step 完成后资源/permission/transport 阻塞；
- terminal canary 注入 report 不可写、record 超限、UI notifier 失败和 Runner 重启；
- 不把弹窗出现、`accepted`、退出码 0 或聊天总结当通过证据。

## 7. Update、Homebrew 与发布回归

- local-alpha/PyPI managed update：latest、decline、digest mismatch、成功升级、Runner 启动失败恢复；
- Homebrew-owned `agentbc update` 继续只返回 `brew upgrade agentbc`，零写入 AgentBC board；
- Intel/Apple Silicon bottle install/upgrade、version/help、PATH 与 service identity；
- Homebrew 自身 Xcode/CLT doctor/test harness 问题继续作为环境诊断，不写入 AgentBC 依赖；
- session teardown/auxiliary cleanup 覆盖 success、Deny、timeout、transport lost、进程异常和重启；
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
- 不在本版新增 OpenCode、Cursor、原生 Windows/Linux Runner、GUI、Webhook/Email 或跨机派发；
- 不移除 protocol v1 reader，不做未经 characterization 保护的全模块重写；
- 不移动、覆盖或重建 `v1.0.3A2` 与 PyPI `1.0.3a2`。

## 9. 完成定义

`1.0.4A` 只有同时满足以下条件才能进入发布：

- 七个权威开发项均有实现、定向/全量测试、真实 E2E 和合入证据；
- Core 不再依据 Prompt/Agent 自述选择审批渠道；native Deny 零执行且不产生第二 worker；
- 不可升级阻塞最多一次审批并稳定 blocked；
- handoff multi-step 在 dispatch 前完成合同校验，callback 严格一致；
- progress receipt 单调且不被资源/permission/terminal 覆盖回退；
- report/record/notification/cleanup 任一阶段失败时其余阶段仍可独立完成或重放；
- Update、Homebrew、session cleanup 和 `1.0.3A` 权限行为无回归；
- integration 与三个 agent 分支干净，Runner identity match，Doctor blocker 为 0；
- release candidate 的 GitHub/PyPI/bundle/bottle/manifest SHA 与 tag commit 可复验；
- 用户完成最终 go/no-go，之后才创建不可变 `v1.0.4A` tag 和 PyPI `1.0.4a1`。
