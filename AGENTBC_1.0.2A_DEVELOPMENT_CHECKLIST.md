# AgentBC 1.0.2A 需求开发清单

> 制定日期：2026-08-08  
> 最近状态快照：2026-08-11（测试策略冻结与 Phase 5 规划）
> 状态：开发进行中（Phase 0～Phase 4 代码已合入；真实点对点与全面测试延后，下一阶段为 Phase 5）
> 当前开发分支：`private/integration`  
> 固定 Agent 分支：`agent/codex`、`agent/claude`、`agent/hermes`  
> 初始开发基线：`private/integration@cfddccba246e6d057172f6716ab4318ade9a40ad`
> 当前集成基线：`private/integration@6f19505`（本地尚未 push）
> 对应公开稳定修订：`v1.0.1A3` / Python `1.0.1a3` / `5e74de65c9b49867ac7957969138db59e2208572`  
> 目标版本：`v1.0.2A` / Python `1.0.2a1`

## 0. 继续开发时的唯一状态入口

后续会话先读本节，再按需进入详细需求；不得仅凭历史聊天、Agent 自述或一次
`accepted` 判断完成状态。状态优先级固定为：当前 Git/测试事实 → AgentBC
`task status/report/RunLease` → 本清单快照。运行中任务结束后必须先验收并更新本节，
再安排下一 Phase。

状态标记：`✅ 已合入`、`🟡 运行中/部分完成`、`⬜ 未开始或未闭环`。

### 0.1 已完成并合入

| 工作项 | 状态 | 已具备能力 | 主要证据 |
| --- | --- | --- | --- |
| `DEL-001` | ✅ | 终态任务链 dry-run/confirm、安全 staging、task code 归还与 customer path 保护 | `48de7dc`、`tests/test_task_delete.py` |
| `REPORT-001` + `OBS-001` | ✅ | 按真实 run interval 累计 execution duration，当前 lease 使用权威派生视图 | `795e2db`、`tests/test_timing_view.py` |
| `PROMPT-001` | ✅ | 三 Executor 公共 Prompt contract 单一 builder、golden 与长度门禁 | `0b51af5`、`b3da287`、`fe48ba3` |
| Phase 0 | ✅ | 冻结资源、session、耗尽路由、Claude Project 和真实 CLI fixture 契约 | Phase 0 contract tests |
| Phase 1 / `CFG-001` 配置入口 | ✅ | 原子 TOML 事务、setup 保留式合并、Claude 默认 `$10`、Hermes 默认提取，以及三组配置命令 | `4e60e4f`、`7164bc2` |
| Phase 2 | ✅ | 任务级 resource/session 冻结快照、handoff/reassign 重建、PathPlan、Runner packet 一致性和公共视图 | `b38c8da`、`fea07b2`、`6de4cf1`、`75ffc23` |
| Phase 3 / `CFG-001` 端到端 | ✅ | Claude `--max-budget-usd`、Hermes `--max-turns`、三 Executor session receipt、明确 ID resume 和 Runner fail-closed 校验 | `438030f` |
| Phase 4 Tasks 1～3 | ✅ | Claude/Hermes 资源耗尽结构化识别、Core `input_required` 阻塞、approve 翻倍继续、deny 明确失败、Runner respond-and-dispatch | `2b02891`、`24be4dd`、`b7ba051` |
| Phase 4 Task 4 / `CFG-002` UX | ✅ | 资源决策固定两按钮、approve/deny 映射、fallback 命令、公共资源视图与三份文档 | `QECT-001`、`22c8d61`、`tests/test_phase4_resource_decision_ux.py` |

Phase 4 Tasks 1～3 的集成基线 `b7ba051` 全量 discovery 为 `765` 项通过；Task 4
分支验收新增 `17` 项 UX 测试通过。合并基线 `bd6d6a2` 的 Phase 4 定向 `56` 项和全量
discovery `782` 项全部通过，Ruff、compileall 与 `git diff --check` 通过。已生成并安装的
本地 `1.0.1a3` Phase 4 Task 4 构建已指向 `6f19505`，`build_source=local-phase4-task4`，
package-only smoke 与 CLI/Runner identity 验证通过；正式版本尚未提升为 `1.0.2a1`。

### 0.2 本轮任务与失败链路

| 工作项 | AgentBC 任务 | 当前事实 | 完成后动作 |
| --- | --- | --- | --- |
| Phase 4 Task 4 / `CFG-002` UX、公共视图与文档 | `QECT-001`（Hermes） | `completed`；冻结 `max_turns=150`、显式 `full` 权限，实际运行约 26 分 41 秒；合法 final callback、完成标记、关闭 RunLease 与官方 session receipt `20260811_004323_d3bd9b` 均已验收 | 成果已合入 integration；安装包真实资源耗尽 canary 进入集中全面测试批次，本阶段不单独执行 |

旧任务 `QDKN-001` 已因旧运行达到 `60/60` 且 final marker 无效而终态 `failed`；
`97FK-001` 因旧 Hermes quiet review 竞争丢失官方 receipt 而 `needs_recovery`。二者均不恢复、
不 handoff，也不是当前开发基线。`QECT-001` 是不继承其 lineage、session、resource 或
运行记录的全新根任务，并验证 thread-scoped output 修复后长任务 receipt 可稳定落盘。

### 0.3 尚未完成，禁止提前关闭

| 工作项 | 状态 | 剩余闭环 |
| --- | --- | --- |
| Phase 4 / `CFG-002` | 🟡 代码完成/待集中验证 | Tasks 1～4 已合入；真实 Claude/Hermes 耗尽、approve 翻倍同 session 继续、deny 明确 failed 的安装包 canary 延后到集中全面测试 |
| `SESSION-001` | 🟡 | session 快照、receipt、input wait 保留和同 ID resume 已完成；终态 cleanup/purge、capability 与 cleanup receipt 未实现 |
| `SAFE-001` | ⬜ | linked worktree Git 元数据预检、`commit_required`、控制端审查提交和恢复链路均未实现 |
| `SKILL-001` | ⬜ | canonical controller contract 的安装身份、协议版本和 template hash 握手未实现 |
| `DOC-002` | ⬜ | doctor 稳定 schema/退出码以及 Skill、session cleanup、safe Git、config/runtime 漂移诊断未闭环 |
| `DOCFIX-001` | ⬜ | help、Record README、Quick Start、双语 User Guide 与三类 Skill 的最终一致性收口未完成 |
| `REL-102` | ⬜ | 版本提升、Python 3.10/3.11/3.14、wheel/sdist、双机和三真实 Executor 发布 Gate 未执行 |

### 0.4 后续固定顺序

1. Phase 5：完成 `SESSION-001` 终态 cleanup/purge、capability/receipt；
2. Phase 6：完成 `SAFE-001` linked-worktree 控制端提交闭环；
3. Phase 7：完成 `SKILL-001`、`DOC-002`、`DOCFIX-001`；
4. Phase 5～7 核心功能基本完成后，统一执行源码全面回归、安装包验证、真实 Executor
   点对点 canary、Python/双机矩阵和安全/恢复测试；
5. Phase 8：根据集中测试结果修复阻断项，再执行 `REL-102` 版本与发布 Gate。

除非本清单被显式更新，后续 Agent 不得跳过 Phase 4 验收，不得把 session resume
等同于 session cleanup 完成，也不得因本地新包可运行而宣称 `1.0.2A` 已发布。

### 0.5 当前测试策略冻结（2026-08-11）

为了避免功能尚未收口时反复消耗真实 Agent 预算、会话和安装环境，Phase 5～7 开发期间
暂停逐功能的真实点对点测试，也不把每个小任务都升级为完整发布回归。当前阶段只执行：

1. 本任务新增契约的定向自动化测试；
2. 与改动模块直接相邻的受影响回归；
3. Ruff、compileall、`git diff --check`，以及必要的 Shell 语法检查；
4. 对删除、purge、配置写入和路径操作只使用临时目录、fixture、fake CLI 或 mock，
   禁止触碰真实用户 session、Claude Project、customer project 或 dispatcher conversation；
5. 只有修改共享状态机、Runner 授权、终态 finalize 等高风险公共路径，或出现难以定位的
   回归时，集成控制端才额外运行扩大回归；全量 discovery 不再作为每个子任务的默认门禁。

本阶段明确暂缓：Claude/Hermes 真实资源耗尽 approve/deny、真实 session purge/delete、
Codex safe 普通 clone/linked-worktree 双 canary、三 Executor 端到端串联、Python 版本矩阵、
双机、clean install/upgrade/rollback 和完整发布包测试。暂停测试不等于验收通过；对应项目
继续保持“代码完成/待集中验证”或“部分完成”。

当 Phase 5～7 代码路径均已合入、无已知 P0 实现缺口且接口/文档基本冻结后，集中测试按
以下顺序执行：源码全量回归 → 构建 provenance 与 wheel/sdist → clean install/upgrade →
CFG-002 资源耗尽点对点 → SESSION-001 retain/cleanup 点对点 → SAFE-001 linked-worktree →
doctor/Skill/help 一致性 → Python/双机矩阵 → 失败注入、恢复、回滚与数据安全复核。集中测试
重点不是单纯“命令退出 0”，而是核对 task/status/report/RunLease/receipt/session ID、同
Task resume、清理边界、用户数据保护、secret redaction、CLI/Runner identity 与最终 SHA
是否一致。

## 1. 文档地位与范围

本清单把《AgentBC Alpha 至正式版开发手册》中分散在版本目标、技术债、测试门禁和
已知转入项里的 `1.0.2A` 工作收敛为一份可执行清单。开发边界和架构护栏仍以
`AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md` 为准；旧状态报告只提供需求细节，不得扩大
本版范围。

`1.0.2A` 的产品目标是：补齐任务历史治理、运行环境自诊断、执行会话治理和几项已经
影响真实使用的透明度问题，为 `1.0.3A` 的安装升级工作以及 `1.0.5A` 的
OpenCode/Docker 亮点版本建立稳定运维基线。

本版明确不做：

- 顶层 `agentbc update`、Homebrew；
- OpenCode、Docker、原生 Windows/Linux Runner；
- completion/liveness/schema v2；
- GUI、Webhook、Email 等通知扩展；
- 跨机派发、链式自动派发；
- `service.py`、`runner.py`、`cli.py`、`setup.py` 的大规模结构拆分；
- 统一 Agent 权限设置、三 Executor 权限 registry、结构化 approval event，以及权限受阻
  自动进入桌面 `input_required` 弹窗；这些协议级改造全部冻结到 `1.0.3A`，不得继续给
  `1.0.2A` 增加需求负担。

`1.0.2A` 剩余开发期间采用人工过渡规则：每次创建新的根任务或 handoff 前，控制端必须先
向用户确认目标 Agent 完成本任务实际需要 `safe` 还是 `full`，再在派发命令中显式传入
`--permission-mode <safe|full>`；不得依赖 AgentBC 配置、来源任务或 Executor 原生配置的
隐式继承。`full` 必须明确提示风险并取得本次派发授权。同 Task 的 retry/recover/resume
继续使用已冻结权限，不通过普通 input 文本改权。该人工确认只约束派发流程，不在
`1.0.2A` 新增权限弹窗或权限协议实现。

允许的重构只限于支撑本清单功能的局部公共模块，并且机械迁移与语义变化必须分提交。

## 2. 当前源码盘点

| 项目 | 当前状态 | 1.0.2A 处理口径 |
| --- | --- | --- |
| `task delete` | 已完成并合入 | 保持整条终态历史链安全删除的回归门禁 |
| `doctor [--json]` | 只有基础实现和 A3 构建身份；完整契约未闭环 | 增量补齐 Skill 漂移、配置/runtime 漂移、稳定 schema/退出码和 blocker |
| 执行会话保留 | 快照、receipt、等待保留与同 ID resume 已完成；终态清理未实现 | 只继续实现 cleanup capability/receipt 与 Claude purge/目录安全清理 |
| Claude 预算 / Hermes 迭代 | 配置、任务快照、Adapter argv、Runner 校验和耗尽决策 UX 已完成 | 真实 approve/deny canary 延后到集中全面测试 |
| Codex safe 与 linked worktree | `workspace-write` 可修改工作树，但共享 Git 元数据位于 customer root 外，`git add/commit` 会失败；普通 input 响应不能改变沙箱授权 | 派发前识别并给出可接管的安全提交路径，禁止静默扩大权限 |
| 执行时长 | 已完成真实 run interval 累计和权威 lease 当前视图 | 保持 status/report/notification 同源回归 |
| Prompt 公共契约 | 已完成共享 builder、golden 和长度门禁 | 保持 v1 行为不变回归 |
| Skill 身份 | package template 是 canonical source，但无安装 hash 握手 | 写入版本/协议/template hash，doctor 检查漂移 |
| build identity | 已在 A3 修复并通过发布链验证 | 作为回归门禁，不重复开发 |
| execution lease 快照 | 已统一为权威当前视图，原始 extension 只作历史证据 | 保持 status/report 不被旧快照覆盖的回归 |

### 2.1 分阶段实施记录

- `2026-08-10 / Phase 0`：已冻结资源、session、耗尽路由与 Claude Project 的 v1
  契约和真实 CLI fixture；当时缺口以可执行 `expectedFailure` 固定，Phase 1～4
  实现后已逐项转为正常测试，当前全量基线不保留 Phase 4 `expectedFailure`。
- `2026-08-10 / Phase 1`：已完成配置事务层、setup 保留式合并、Claude `$10`
  新默认、Hermes `agent.max_turns -> legacy max_turns -> 90` 提取，以及
  `claude budget`、`hermes max-turns`、`session retention` 三组配置命令。
- `2026-08-10 / Phase 2`：已完成新任务 `agentbc.resources` / `agentbc.session` 快照、
  handoff/reassign 重建、同 Task ID 冻结、Claude UUID、canonical
  `<TASK-ID>/claude` PathPlan、legacy 固定默认补齐、Runner packet 与磁盘快照一致性校验、
  统一公开视图及终态 compaction 保留。内部路径不进入公共
  workspace/session/artifact，Codex 只要求 session 快照而不虚构资源上限。
- `2026-08-10 / Phase 3`：已完成 Claude `--max-budget-usd`、Hermes `--max-turns` 的
  任务快照注入，Claude/Hermes/Codex 官方 session receipt、同 Task 显式 ID resume，及
  Runner command/snapshot/cwd fail-closed 校验。`CFG-001` 端到端完成；`SESSION-001`
  只剩终态 cleanup/purge 与能力回执，`CFG-002` 仍保持打开。
- `2026-08-10 / Phase 4 Tasks 1～3`：已完成资源耗尽的 Adapter 结构化识别、Core
  `input_required`、任务级翻倍/终止决策和 Runner 原子响应派发，并合入集成基线
  `b7ba051`；全量 discovery `765` 项通过，不再保留 Phase 4 `expectedFailure`；
  Ruff、compileall、Shell 语法和 `git diff --check` 通过。
- `2026-08-11 / Phase 4 Task 4（CFG-002 UX 切片）`：已完成资源耗尽 choice 的弹窗
  按钮映射（`kind=resource_limit` + `response_protocol=approve_deny` 下
  「提高预算并继续」→ `approve`、「终止任务」→ `deny`，Later/关闭/超时保持
  waiting）、fallback `--approve/--deny` 命令、公共 execution policy 视图新增
  `configured_limit` / `exhaustion_count` / `last_decision`、status/preflight/report
  一致展示，以及 1.0.2A 清单、开发手册、中文用户指南三份文档同步。`SESSION-001`
  终态 cleanup/purge 与能力回执仍保持打开；本切片不实现 purge/delete。Hermes
  `QECT-001` 以 `max_turns=150`、`full` 权限完成约 26 分 41 秒实跑并保存官方 session
  receipt `20260811_004323_d3bd9b`；成果由 `22c8d61` 经 `bd6d6a2` 合入。
- Phase 4 合并最终证据：定向 `56` 项、全量 discovery `782` 项通过，Ruff、compileall
  与 `git diff --check` 通过，不保留 Phase 4 `expectedFailure`。

## 3. 需求总表

| ID | 状态 | 优先级 | 需求 | 主要责任模块 | 依赖/剩余边界 |
| --- | --- | --- | --- | --- | --- |
| `DEL-001` | ✅ 已合入 | P0 | 安全删除终态历史链并归还 task code | Service、Store、ID、CLI、Index | 只保留回归 |
| `SESSION-001` | 🟡 部分完成 | P0 | 执行 Agent 临时会话保留策略 | Config、Setup、CLI、Adapter、Worker/Service、Doctor | 仅剩终态 cleanup/purge、capability/receipt |
| `SKILL-001` | ⬜ 未开始 | P0 | Controller contract 单一来源与 Skill hash 握手 | Skill template、Setup、Doctor | doctor 基础实现 |
| `DOC-002` | ⬜ 未闭环 | P0 | 完成只诊断 doctor 契约 | Doctor、Registry、Runner query、CLI | SKILL-001、SESSION-001 receipt、SAFE-001 |
| `REPORT-001` | ✅ 已合入 | P1 | 修正恢复任务累计执行时长 | RunLease、Service timing、Report、Task List、Notification | 只保留回归 |
| `CFG-001` | ✅ 已合入 | P0 | Claude 预算与 Hermes 迭代上限配置及执行注入 | Config、Setup、CLI、Claude/Hermes Adapter、Preflight | doctor 最终视图待 DOC-002 |
| `CFG-002` | 🟡 代码已合入/待集中验证 | P0 | 预算/迭代耗尽决策：弹窗翻倍继续或终止 | Adapter、Worker/Core、Notifications、Service respond | 安装包真实 canary 按 0.5 节延后 |
| `SAFE-001` | ⬜ 未开始 | P0 | Codex safe 在 linked worktree 下的 Git 写入预检与可接管提交 | Path Plan、Codex Adapter、Runner、Preflight、Input、Notification、Doctor | 权限三档、SESSION-001 resume 已具备 |
| `PROMPT-001` | ✅ 已合入 | P1 | 三 Executor 公共 Prompt 契约去重 | 公共 builder、Codex/Claude/Hermes Adapter | 只保留回归 |
| `OBS-001` | ✅ 已合入 | P1 | 当前 execution lease 状态单一派生视图 | RunLease query、Status、Report | 与 REPORT-001 同步完成 |
| `DOCFIX-001` | ⬜ 未闭环 | P2 | 修正文档/help 漂移 | Record README、CLI help、双语文档、Skills | Phase 4/5/6 字段稳定后收口 |
| `REL-102` | ⬜ 未开始 | Gate | 1.0.2A 版本、双机、真实 Executor 与发布验收 | Build/CI/docs/release | 全部需求 |

原始工作量估算只保留为历史规划，不再用于推断当前剩余进度。剩余工作以 0.3 和 0.4
为准；任何真实 Executor 能力缺失应按 `unsupported` 交付，不得用危险文件扫描缩短排期。

## 4. 详细需求与验收

### 4.1 `DEL-001`：`agentbc task delete`

固定接口：

```text
agentbc task delete <TASKCODE> --dry-run
agentbc task delete <TASKCODE> --confirm
```

要求：

- 只接受任务链 code，不接受单次 iteration ID；
- 只有整条链均为 `completed/failed/cancelled/rejected` 才允许删除；
- `pending/running/input_required/needs_recovery` 任一存在时整链拒绝；
- `--dry-run` 只输出所有将删除和保留的对象，零磁盘写入；
- 删除范围只包括 AgentBC 拥有的 task/report/record/index entry 和 managed artifact；
- customer project 永不删除，即使路径名称与 managed artifact 相似；
- 删除需要 reservation/commit 或等价事务边界，异常中断不得产生半删除后释放 ID；
- 成功后写有界 deletion receipt，再释放 task code；receipt 不占用 task code；
- 重复执行必须返回稳定的 already-deleted/not-found 结果，不误删后续复用同 code 的任务。

验收：managed/customer 两种路径、单/多 iteration、活动链拒绝、dry-run、确认、模拟中断、
重复调用、ID 归还、index 重建和 containment 攻击全部通过。

### 4.2 `SESSION-001`：执行 Agent 临时会话保留

固定配置和命令：

```text
sessions.retain_executor_sessions = false

agentbc session retention status
agentbc session retention enable
agentbc session retention disable
```

Phase 1 已实现上述配置键和三命令的原子、幂等持久化，并明确输出只影响后续
Executor run、永不删除 dispatcher conversation。Phase 2～3 已完成 `input_required`
强制保留、session ID receipt 和同会话 resume；当前唯一未闭环部分是终态
cleanup/purge、cleanup capability 与 receipt。

Phase 2～3 子项状态（不代表 `SESSION-001` 整项完成）：

- [x] 新 Claude/Hermes/Codex 任务冻结 `agentbc.session`；Claude 预分配 UUID，
  Hermes/Codex pending receipt 为空；
- [x] handoff/reassign 新建目标策略，同 Task ID resume/retry/recover/re-dispatch 不漂移；
- [x] 公共 `execution_policy` 隐藏 `project_path`/`executor_project_root`，临时 Claude
  Project 不计入 artifact；终态 compaction 完整保留 session receipt；
- [x] PathPlan 原生 `executor_project_root`、canonical containment 与已有父级 symlink
  escape 校验完成；legacy Claude 使用相同 `<TASK-ID>/claude` 路径且 UUID 单独保存；
- [x] Claude/Hermes/Codex 记录官方 session receipt；同 Task 后续 run 使用明确 ID
  恢复同一会话，`input_required` 状态保存 ID/run history/resume count；
- [x] Runner 拒绝缺失、重复、篡改或模糊 session 参数，禁止 Claude 不持久化、Hermes
  `--continue` 和 Codex `--last`；
- [ ] 终态 cleanup/purge 和 cleanup capability/receipt 尚未接线。

要求：

- 默认 `false`（默认清理）：任务以终态（completed/failed/cancelled）结束时移除
  该任务的 Executor 临时会话；只有显式 enable 后终态保留；
- enable 后任务结束保留会话；disable 恢复默认清理；enable/disable 原子更新配置，
  仅对后续新 run 生效；
- `input_required` 是保留例外：无论全局开关，等待用户响应期间该任务临时会话
  必须保留，并在任务扩展 `agentbc.session` 记录 `session_id`/executor/run；
  respond resume 派发时通过官方 `--resume/--continue` 继续同一会话，禁止新建
  会话重建上下文；
- `needs_recovery/active/stale` 不清理；
- 交互 setup 默认值 `false`（默认清理），已有用户值必须保留并显示；
- 所有用户文案明确区分 dispatcher conversation 与 executor temporary session；
- AgentBC 永不删除派发源对话；
- Adapter 明确报告 session ID、是否持久化、是否支持官方安全清理及清理结果；
- 只在 terminal、RunLease closed、最终 task/report 落盘、通知入队后请求清理；
- 只使用官方 CLI/API 的不持久化或删除能力；禁止猜路径、扫描最新会话或递归删除目录；
- Claude 保留模式直接使用任务已解析的用户工程目录，不创建临时 Claude Project，
  终态不得对该用户工程执行 `claude project purge`；
- Claude 默认清理模式不新建顶层 runtime 根；临时 Claude Project 复用 AgentBC
  `tasks/artifacts/YYYY-MM-DD/<TASKCODE>/` 下的任务内部目录，并以完整
  `<TASK-ID>` 隔离 iteration，不得直接共用整条 handoff 链的 `<TASKCODE>` 目录；
- Claude 清理必须是后台、幂等且可重试的单一流程：根据任务记录的精确路径
  执行 `claude project purge --yes <project-path>`，再移除 AgentBC 拥有的 Claude
  子目录，最后只在 task/chain artifact 目录为空时删除空目录；
- 临时 Claude Project 只是内部执行锚点，不计入用户产物、不出现在正常
  status/report/artifact 列表中，也不增加独立的用户清理入口；正常体验必须与
  Hermes/Codex 运行时一致；
- Claude 清理路径必须由 Path Plan 生成并做 containment/symlink 校验；
  意外非空目录不得当作空壳递归删除，避免误删任务产物；
- unsupported/failed 只生成 receipt 和 doctor warning，不改变原任务终态。

验收：默认清理、enable/disable 保留、input_required 期间会话保留与
resume 同会话继续、三命令幂等、三 Executor capability、恢复路径、handoff、
Claude 保留模式无临时目录、默认模式 purge 后无空壳、链路 iteration/分支隔离、
非空目录保护、用户界面无 runtime 暴露、失败回执、secret redaction 与 dispatcher
conversation 不受影响。

### 4.3 `SKILL-001` + `DOC-002`：Skill 握手与 doctor 完整契约

`doctor` 保持只读，不增加 `--fix`。第一版固定退出码：

```text
0 = healthy
1 = warning
2 = unavailable
```

要求：

- 保留现有 package version、commit/build SHA、安装来源、模块路径和 Runner Python；
- 增加 doctor schema version，JSON key 稳定且不输出 token/secret；
- 报告 Runner PID/token/spool/config、workspace/report/record 权限和活动任务 blocker；
- Executor discovery 必须从最终配置反推，显示 configured/resolved/source/version/probe/auth/capability；
- canonical controller contract 只维护一份，Codex/Claude/Hermes Skill 只保留平台差异；
- 安装副本写入 package version、completion/protocol version 和 template hash；
- doctor 比较 package、Runner 与全部已安装 Skill，给出明确 update/restart 指令；
- SESSION-001 unsupported/cleanup failed 必须作为对应 Executor warning；
- 单项 unavailable 不得让 doctor 自身崩溃。

验收：text/JSON 同源、三退出码、旧/缺失/被修改 Skill、stale Runner、配置指向旧二进制、
路径不可写、部分 Executor 缺失、无 secret 和 clean install Gate 全部通过。

### 4.4 `CFG-001`：Claude 预算与 Hermes 迭代上限配置

setup 提供两项执行资源配置：Claude 单任务预算 `max_budget_usd` 与 Hermes
单任务迭代上限 `max_turns`。交互逐项询问「自定义 / 使用默认」：

Phase 1 已完成 config/setup/CLI 入口和用户值保护；Phase 2 已贯通任务快照和
preflight/status/report 公共视图；Phase 3 已完成 Claude/Hermes Adapter 参数注入与
Runner fail-closed 参数校验。`CFG-001` 已端到端完成，不再是 config-only 状态。

- 自定义：用户输入。Claude 输入 USD 金额；Hermes 输入迭代轮数（正整数）；
- 使用默认：Claude 为 `$10`；Hermes 从 `~/.hermes/config.yaml` 读取
  `agent.max_turns` 作为默认（读取失败回退 Hermes CLI 默认 `90`）；
- 已有用户值必须保留并显示，不覆盖。

固定更改命令（随时可改，原子写 TOML 且保留其他配置，下一次 run 生效）：

```text
agentbc claude budget <usd>
agentbc hermes max-turns <turns>
```

要求：

- non-interactive 支持 `--claude-max-budget-usd <value>` 与
  `--hermes-max-turns <value>`，未提供时保留旧值；
- 交互输入只接受大于 0 的有限金额/轮数；Claude 不提供无限预算；
- 修改预算/迭代上限不自动重启 Runner、不重试历史任务；
- create/dispatch accepted、preflight 和 status JSON 显示本次 effective
  budget/turns；
- task 记录只保存金额/轮数，不保存凭据或 Executor 私有配置；
- setup 的 executor refresh 不得覆盖用户已配置值（修复当前 Claude budget
  被重置为 1.0 的缺陷）。

Phase 2～3 子项状态：

- [x] create/handoff/reassign 持久化任务级 `agentbc.resources`，默认/自定义/非法配置、
  冻结与公共 effective/source/frozen 视图已覆盖；
- [x] 同 Task ID resume/retry/recover/re-dispatch 保持原资源快照；
- [x] Runner 在派发和授权时校验原生/legacy 快照结构及 Worker packet 与磁盘权威记录一致；
- [x] Phase 3 已完成 Claude/Hermes Adapter 参数注入与 Runner argument/snapshot 核对；
  `CFG-001` 端到端完成，后续资源翻倍只属于 `CFG-002`。

验收：首次 setup、升级 setup、自定义/默认两分支、空值、非法数、NaN/Inf、
两命令幂等、配置保真、setup 后用户值保留和真实任务预算/迭代可见性通过。

### 4.5 `CFG-002`：预算/迭代耗尽可能决策（弹窗翻倍继续或终止）

Claude 预算耗尽（`Exceeded USD budget`）或 Hermes 迭代耗尽
（`max_iterations_reached(N/M)` 等）不再直接进入 failed，而是转为
`input_required`（`type=choice`）等待用户决策：

- Adapter 在无有效 final callback 且优先级允许时识别锚定资源耗尽信号，输出结构化
  `resource_exhaustion` / `failure.kind=resource_limit_exhausted`；Claude 优先结构化
  `error_max_budget_usd`，文本 fallback 只接受 CLI error 位置的精确形式；Hermes 只接受
  既定锚定信号，缺少数字上限时使用任务冻结快照，禁止从普通 prompt/output 回声误判；
- Core 校验 task/run、resource 快照与 execution session receipt 后进入
  `input_required`：保留 done step，阻塞第一个未完成 step，reason 只显示资源使用/上限，
  不含密钥、正文或内部路径；
- 弹窗两按钮：「提高预算并继续」/「终止任务」，附逐项说明；
- `approve`（提高并继续）：执行资源按任务级翻倍（Claude `max_budget_usd`
  ×2、Hermes `max_turns` ×2），仅本次任务生效，不写入全局配置；配合
  SESSION-001 通过 `--resume` 继续同一会话，翻倍值记录在任务扩展供
  preflight/status 展示；
- `deny`（终止任务）：终态为 `failed`，failure 携带明确原因
  （`budget_exhausted_user_terminated` / `iteration_exhausted_user_terminated`），
  `retryable=false`；
- 用户 24 小时未响应沿用 input_required 到期语义（转 `needs_recovery`）。

Phase 4 当前状态（对照集成基线 `b7ba051`，Tasks 1～3 已合入）：

- [x] Tasks 1～2：Adapter 耗尽识别、终态优先级、Core 资源阻塞入口；
- [x] Task 3：approve 按任务快照翻倍并恢复同 session，deny 以明确原因 failed，
  Runner 原子 respond-and-dispatch；
- [x] Task 4（本切片）：通知/dialog 两按钮、fallback 命令、公共视图字段和三份文档；
- [ ] 集中全面测试中的安装包真实 canary：Claude 与 Hermes 各覆盖耗尽→approve→同
  session 继续，以及耗尽→deny→明确 failed；验证前不得关闭 `CFG-002`，但不阻塞
  Phase 5～7 继续开发。

Task 4 已落地子项（弹窗/视图/文档切片）：

- [x] `kind=resource_limit` + `response_protocol=approve_deny` 的 choice 弹窗两按钮
  「提高预算并继续」/「终止任务」分别映射 `approve` / `deny`；Later、关闭弹窗与
  超时保持 `waiting`（dismissed，不推进任务状态）；fallback 命令展示
  `--approve` / `--deny`；普通 choice 仍以消息选项提交（`--message`），语义不变；
- [x] 公共 execution policy 视图在兼容 `limit`（当前生效上限）字段基础上新增
  `configured_limit`、`exhaustion_count`、`last_decision`；status/preflight/report
  一致展示，不暴露 raw output、secret 或内部 Claude project path；
- [x] 1.0.2A 清单、开发手册、中文用户指南三份文档同步 Phase 4/CFG-002；
  `SESSION-001` 终态 cleanup/purge 保持打开，本切片不实现 purge/delete。

`SESSION-001` 终态 cleanup/purge 与能力回执仍保持打开；`CFG-002` 只剩集中全面测试中的
安装包真实 canary 未验收。

验收：claude 预算耗尽、hermes 迭代耗尽、翻倍继续成功、终止 failed 带原因、
到期转恢复、弹窗文案、无密钥泄漏与 status/report 一致展示通过。

### 4.6 `SAFE-001`：Codex safe linked-worktree Git 写入与可接管提交

问题边界：Codex `safe` 固定使用 `--sandbox workspace-write`。普通 clone 的 Git 元数据位于
project root 内时可以正常提交；linked worktree 的 `.git` 是指向主仓
`.git/worktrees/<name>` 的指针，共享 object/ref 元数据位于 customer root 外，因此源码编辑和
测试可以成功，但 `git add/commit` 会在 `index.lock`、objects 或 refs 写入阶段被沙箱拒绝。
用户在普通 message 输入里回复“允许执行”只会恢复任务，不会改变已持久化的 `safe`
权限，当前实现会重复进入同一不可执行步骤。

要求：

- create/dispatch/preflight 解析并记录 checkout 类型、`git_dir`、`git_common_dir`、当前
  branch/HEAD，以及 Git 元数据是否位于本次可写根内；解析必须使用 Git 官方查询结果并做
  realpath/containment 校验，不接受任务文本声称的路径；
- 当任务要求 Git 写入，而 Codex safe 检测到 linked worktree 的元数据位于可写根外时，
  必须在首次提交前进入可行动的 `input_required`，稳定原因码为
  `codex_safe_git_metadata_blocked`；status/report/notification 明确显示已完成的源码/测试、
  被阻塞的 Git 动作和下一步，不得把普通 message 响应描述成“提权”；
- 默认恢复路径是“由控制端审查并提交”：Executor 保留工作树变更和测试证据，输出结构化
  `commit_required` 清单；控制端在同一固定 agent 分支审查并提交后，以 commit SHA 响应；
  Runner 恢复同一 Task ID/会话，Executor 只验证 HEAD、clean tree 和原步骤证据后完成；
- `safe` 不得把整个主仓 `git_common_dir` 加入 writable roots，不得写 `main`、其他 agent
  分支或其他 worktree 的 refs，不得自动切换为 `full`；若未来实现 Runner Git proxy，只允许
  对精确当前 worktree/branch 执行 allowlist 中的 `status/diff/add/commit`，并审计文件清单、
  commit message、基准 HEAD 和最终 SHA；
- 用户若明确选择终止或改用 `full`，本次 safe run 必须先以可解释状态结束；`full` 只能通过
  已有显式、持久化、可审计的任务授权重新派发，不能由自由文本响应隐式升级；
- response 前后复核 task/input ID、current chain head、branch/HEAD 和 dirty file set；发生
  stale response、HEAD 变化、越界路径、symlink escape 或 controller commit 不匹配时 fail
  closed，不重复派发同一必失败的提交尝试。

验收：普通 clone safe 编辑并提交、linked worktree safe 编辑后进入 `commit_required`、控制端
提交并恢复完成、用户终止、重复/错误/stale input、HEAD/branch 竞态、symlink/realpath/共同
Git 目录篡改、禁止写 main/其他 refs、通知原因与下一步、status/report/doctor 一致展示全部通过。

### 4.7 `REPORT-001` + `OBS-001`：真实执行时长与 lease 当前视图

要求：

- `wall_duration` 表示从创建到当前/终态的生命周期；
- `execution_duration` 只累计每次真实 worker/Executor run interval；
- `waiting_duration` 单列 input waiting；恢复等待、pending、paused、recovery-ready 不计入 execution；
- 保存累计执行、最近一次执行及证据来源；历史证据不足显示 `unknown/estimated`；
- 不再用 `completed_at-created_at` 冒充执行时长；
- Task List、status JSON、Report、Notification 和未来 GUI 共用一个 timing view；
- 当前 lease 状态从权威 RunLease 派生，原始 extension 快照只作为历史证据，不覆盖当前视图。

验收：首次失败、长时间等待、recover、再次执行、input waiting、pause、完成的 fake-clock
测试分别断言 wall/execution/waiting/last-run；历史 task 缺字段时不崩溃、不伪精确。

### 4.8 `PROMPT-001`：公共 Prompt 契约去重

要求：

- 先建立三个 Adapter 当前 prompt 的 golden/characterization tests；
- 新建共享 prompt contract builder，公共身份、路径、报告所有权、progress、strict marker、
  resumed-input 规则只生成一次；
- Adapter 只追加 argv、权限、图片、Hermes transport 等平台差异；
- 不改变当前 v1 strict marker、permission、Path Plan、input_required 或 report 所有权；
- 10 步 task 的公共 prompt 目标不超过约 3,000 字符，并阻止按 step 重复公共规则；
- 机械迁移和文本/协议语义变化分提交，本版不引入 v2 sidecar。

验收：三个 Executor prompt snapshot、10 步长度、resumed turn、图片/权限差异通过；真实
Codex/Claude/Hermes canary 纳入 0.5 节集中全面测试。

### 4.9 `DOCFIX-001`：文档与 help 一致性

- `record clean --help` 明确只清理可清理的运行诊断，不删除 Report；
- Record README 明确 queued pending 可以 close；
- 增加 help/template/用户文档一致性测试；
- 同步中英文 User Guide、Quick Start 和三类 Skill 的新增命令。

## 5. 当前 Phase 计划与分工原则

### 5.1 已结束的开发批次

- 初始 Wave 1：`DEL-001`、`REPORT-001/OBS-001`、`PROMPT-001` 已合入；
- Phase 0～3：资源配置、任务冻结、PathPlan、Adapter argv、session receipt/resume 已完成；
- Phase 4 Tasks 1～3：资源耗尽识别、Core wait、approve/deny 与 Runner 响应派发已合入。

以上项目只做回归或缺陷修正，不得在后续 Phase 中被重新规划成“尚未实现”。

### 5.2 当前与后续 Phase

| Phase | 状态 | 目标 | 进入下一阶段的硬门禁 |
| --- | --- | --- | --- |
| Phase 4 | 🟡 代码完成/待集中验证 | `CFG-002` UX、公共视图、文档已合入 | 定向与受影响回归通过；真实 approve/deny canary 延后，不阻塞 Phase 5 |
| Phase 5 | ▶ 下一阶段 | `SESSION-001` 终态 cleanup/purge、capability/receipt、失败重试与 doctor warning 数据 | 临时目录/fake CLI 下 retain on/off、三 Executor capability、Claude 路径/非空目录保护、失败不改终态的定向契约通过 |
| Phase 6 | ⬜ | `SAFE-001` linked-worktree Git 元数据预检、`commit_required`、控制端审查提交与同 Task 恢复 | 自动化 fixture 覆盖普通 clone/linked worktree、分支/HEAD/路径竞态与禁止扩大共享 Git 权限；真实双 canary 延后 |
| Phase 7 | ⬜ | `SKILL-001`、`DOC-002`、`DOCFIX-001` | Skill hash/版本握手、doctor 0/1/2、text/JSON 同源、双语文档/help/Skill 一致 |
| 集中全面测试 | ⏸ 延后 | 源码全量、安装包、真实点对点、Python/双机、安全/恢复/回滚 | Phase 5～7 代码完成且接口基本冻结；所有打开 canary 逐项给出可追溯证据 |
| Phase 8 | ⬜ | `REL-102` 阻断修复与发布收口 | 集中测试阻断清零；wheel/sdist、clean install、三 Executor、双机、唯一 SHA |

### 5.3 Phase 5 开发任务规划

Phase 5 只完成 `SESSION-001` 终态清理代码闭环，不顺带实现 `SAFE-001`、统一权限或
1.0.3A approval 协议。按以下依赖顺序实施；任务 2 依赖任务 1，任务 3～4 依赖任务 2，
任务 5 在前四项合入后收口：

#### Task 1：cleanup contract、状态机与能力模型

- 在现有 `agentbc.session.cleanup.state/attempts` 和 `session_cleanup_blockers()` 基础上
  冻结 cleanup receipt v1；至少表达 `capability`、`strategy`、`state`、`attempts`、
  `requested_at`、`completed_at`、稳定 `error_code`，不得保存命令正文、用户 prompt、
  secret 或 Executor 私有数据库路径；
- eligibility 固定为：Task 属于 terminal 集合、RunLease closed、最终 report 已写入、
  终态 notification 已记录、`retain=false`、session state 为 terminal、官方 session ID
  存在且 cleanup 尚未解决；`input_required/needs_recovery/active/stale` 必须阻断；
- 定义 `not_requested -> pending -> succeeded|unsupported|failed|retained` 的幂等迁移，
  `failed` 可重试但不得无限热循环，已解决状态重复调用必须零副作用；
- 只新增纯 contract/fixture 测试，不在此任务接入真实删除动作。

#### Task 2：终态 cleanup coordinator 与原子 receipt

- 在 terminal finalize 的既有顺序之后触发：RunLease 关闭、最终报告落盘、终态通知入队
  均成功后，才在 task 锁内把 cleanup 从可执行状态切换到 `pending`；
- coordinator 读取磁盘权威 session snapshot，调用 Executor cleanup capability，并将结果
  原子写回同一 Task；进程中断后可根据 receipt 恢复或重试，不重复 purge；
- cleanup 的 `unsupported/failed` 只形成 receipt 与 warning，不修改原任务终态、final
  callback、report readiness 或已完成 step；
- retry 采用有界 attempts/backoff/maintenance 路径；不增加要求用户管理 runtime 的新入口。

#### Task 3：Claude 官方 purge 与受管目录无感清理

- 只允许 `retain=false + project_mode=ephemeral`；`retain=true + project_mode=native` 的
  用户工程固定为 `retained`，永不调用 purge，也不删除任何用户目录；
- 通过真实 help fixture/capability probe 固定无 shell 的规范 argv：
  `claude project purge --yes <project-path>`；只使用任务快照中的精确 session/project
  绑定，不按“最近项目”或模糊名称查找；
- purge 成功后按固定顺序尝试 `rmdir <TASK-ID>/claude`、空的 `<TASK-ID>`、空的 chain
  artifact 目录；每一步重做 ownership、canonical containment、symlink 和预期路径校验；
- 意外文件、用户产物、非空目录、路径漂移或 purge 失败时停止后续删除并写失败 receipt，
  禁止递归删除，禁止把清理失败改写为任务 failed；
- 当前阶段只使用 fake Claude CLI 和临时 artifact tree，不执行真实 Claude Project purge。

#### Task 4：Codex/Hermes capability 与 unsupported 闭环

- 从已发现的 CLI 和冻结 help fixture 探测官方、定向、可审计的 session 删除能力；只有
  能用精确官方 session ID 删除当前任务会话时才标记 supported；
- 若 Codex/Hermes 当前版本无官方删除入口，稳定写入 `capability=unsupported`、
  `state=unsupported` 和原因码；不得扫描 `~/.codex`、`~/.hermes`、SQLite、日志、进程
  或“最近会话”来模拟支持；
- unsupported 必须是已解决 cleanup 状态，避免重复重试，同时由 doctor/status/report
  明确提示能力缺口，不泄露 session 内容。

#### Task 5：公共视图、doctor、文档与阶段集成

- status/preflight/report 只展示 cleanup capability/state/attempts/error code 与是否可重试，
  不展示 Claude internal project path、原生命令、raw output 或 secret；
- doctor 对 unsupported/failed/stale pending 给出稳定 warning 与下一步，对 retain=true 的
  retained 状态不误报警；text/JSON 同源；
- 同步中英文 User Guide、开发手册和三类 Skill 的用户语义：清理始终后台无感，只管理
  Executor 临时会话，永不删除 dispatcher conversation；
- 合并验收执行 Phase 5 定向测试、受影响 session/finalize/report/doctor 回归、Ruff、
  compileall 与 `git diff --check`。真实三 Executor cleanup 点对点和全量 discovery 按
  0.5 节延后到集中全面测试。

Phase 5 推荐文件所有权：Task 1 独占 `execution_policy.py` 与 cleanup schema/fixtures；
Task 2 独占 coordinator 及 `service.py`/terminal finalize 接线；Task 3 独占 Claude Adapter、
PathPlan 清理实现；Task 4 独占 Codex/Hermes Adapter 与 capability probe；Task 5 最后统一
修改 CLI/report/doctor/docs。共享文件不得并行交叉写，先合入依赖项再开始下游任务。

### 5.4 分工与合并护栏

- 三个固定 agent worktree 每轮开始前同步最新 `private/integration`；
- 同一 Phase 可并行的任务必须先冻结文件所有权；`service.py`、`runner.py`、`cli.py`、
  `setup.py` 不允许多个 Agent 同时做交叉语义修改；
- Agent 分支任务失败时先回滚到干净基线，再以新根任务重新派发；不得把失败现场当完成产物；
- integration 必须审阅 Agent diff，允许在合并提交中修正跨模块契约，再按 0.5 节运行
  本阶段定向与受影响回归；开发期不默认重复完整发布门禁；
- 任一 Phase 5 子任务不得因 cleanup schema 或 Claude purge 单项完成就提前关闭整个
  `SESSION-001`；Phase 6 SAFE 与 Phase 7 doctor 继续保持独立边界；
- 真实 Executor 能力不足用 `unsupported` capability/receipt 表达，不得扫描私有目录模拟支持。

## 6. 分支、提交与验收规则

- 三个固定 agent worktree 每轮开始前必须同步最新 `private/integration`；
- 每个 Agent 只在自己的固定分支工作，不新建一次性长期分支；
- 失败任务也必须把分支恢复到可合并或无变更的清洁状态；
- 审查通过后才合入 `private/integration`，随后三分支再次同步；
- 不在本开发机操作公开 `main`、tag、Release 或 PyPI；
- 功能实现、机械重构、协议语义变化和文档更新使用可辨认的独立提交；
- `accepted` 只证明派发；完成验收必须检查 status、report、RunLease、测试和 git diff；
- 涉及删除、会话清理和配置写入的测试只能使用临时根目录，不触碰真实用户数据。
- Phase 5～7 每个子任务按 0.5 节执行定向与受影响回归；不得自行启动真实 Executor
  点对点、真实 purge 或完整发布矩阵。扩大测试范围必须由 integration 控制端基于风险决定。

## 7. 版本级完成定义

`1.0.2A` 只有同时满足以下条件才允许进入候选：

- `DEL-001` 不触碰 customer project，异常中断不产生半删除或提前释放 ID；
- SESSION retain 默认关闭；终态只清理当前 run 的官方可确认执行会话，`input_required` 期间保留并恢复同一会话；
- doctor 能识别 package/Runner/Skill/Executor/config/session cleanup 漂移且 JSON schema 稳定；
- Claude 预算与 Hermes 迭代上限在 setup、配置、preflight/status 和真实执行中一致可见；
- 预算/迭代耗尽可能决策：弹窗翻倍继续或终止 failed 带明确原因，不再直接 failed；
- Codex safe 在普通 clone 可正常提交；linked worktree 在提交前给出明确阻塞原因和控制端审查提交路径，不扩大共享 Git 权限、不循环伪提权；
- Executor 临时会话默认终态清理，input_required 期间保留并 resume 同会话继续；
- 恢复等待不再计入 execution duration，全部用户界面使用同一 timing view；
- 三 Executor prompt 不重复公共规则，v1 完成协议和权限行为不变；
- 所有新增删除/配置/清理动作幂等、原子、可审计且不泄露 secret；
- A3 的严格终态、input_required、权限、路径、conversation trace 和发布身份无回归；
- 当前全量测试基线及新增测试全部通过，真实 Executor 与双机 Gate 通过；
- 工作树清洁，候选版本、提交、构建来源和 SHA256 可追溯。

## 8. 每个新会话/Phase 的启动检查

- [ ] 先读取第 0 节，确认当前 Phase、集成 SHA、运行中 AgentBC task ID 和未闭环边界；
- [ ] 运行 `git status --short --branch`，不得覆盖 integration 或固定 Agent 分支上的未知改动；
- [ ] 用 `agentbc task status` 读取当前任务；存在 active/input_required/needs_recovery 时先按
  status/report/RunLease 处理，不创建重复任务；
- [ ] 派发前确认 Runner ready、目标 Executor available、配置有效，并使用精确固定 worktree
  作为 `--customer-path`；
- [ ] 新任务 steps 明确文件所有权、验收测试和唯一合法 final marker；
- [ ] `accepted` 后记录 task ID；完成验收必须同时检查 callback、report、RunLease、测试和 diff；
- [ ] Agent 完成后先审阅再合入；合入后按 0.5 节跑本阶段门禁并更新第 0 节，不把聊天
  总结当长期状态；全量与真实点对点只在集中测试批次执行；
- [ ] 构建/安装包记录版本、commit SHA、source tree hash 和 SHA256；正式版本提升只在
  `REL-102` Gate 执行。
