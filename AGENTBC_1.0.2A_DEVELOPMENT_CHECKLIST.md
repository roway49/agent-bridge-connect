# AgentBC 1.0.2A 需求开发清单

> 制定日期：2026-08-08  
> 最近状态快照：2026-08-14（Phase 0～Phase 8 开发与真实核心 canary 收口）
> 状态：**开发截止**；不再向 `1.0.2A` 回填新功能、常规缺陷或协议调整，剩余项只属于发布验收
> 当前开发分支：`private/integration`  
> 固定 Agent 分支：`agent/codex`、`agent/claude`、`agent/hermes`  
> 初始开发基线：`private/integration@cfddccba246e6d057172f6716ab4318ade9a40ad`
> Phase 7 代码冻结基线：`private/integration@cb350d786d6fb6e6c47e588f48d1bc903a30721d`
> 开发截止代码基线：`private/integration@b8af2f3a0a1f56814854e3f46056dd8ab9cf55d7`
> 手工候选包：Python `1.0.2a1`，wheel SHA-256 `af588a16e50dc435557bf2c14946ec005753f10380c3f5c59c2146b3728aeec4`
> 对应公开稳定修订：`v1.0.1A3` / Python `1.0.1a3` / `5e74de65c9b49867ac7957969138db59e2208572`  
> 目标版本：`v1.0.2A` / Python `1.0.2a1`

## 0. 开发截止后的唯一状态入口

后续会话先读本节，再按需进入详细需求；不得仅凭历史聊天、Agent 自述或一次
`accepted` 判断完成状态。状态优先级固定为：当前 Git/测试事实 → AgentBC
`task status/report/RunLease` → 本清单快照。运行中任务结束后必须先验收并更新本节，
再安排发布验收或 `1.0.3A` 工作。`1.0.2A` 代码只有发现数据破坏、安装/回退失效或安全
边界失守，并经用户明确批准重开时才允许修改；其他问题统一进入
`AGENTBC_1.0.3A_DEVELOPMENT_CHECKLIST.md`。

状态标记：`✅ 已合入`、`🟡 运行中/部分完成`、`⬜ 未开始或未闭环`。

### 0.1 已完成并合入

| 工作项 | 状态 | 已具备能力 | 主要证据 |
| --- | --- | --- | --- |
| `DEL-001` | ✅ | 终态任务链 dry-run/内置 y/N 确认、安全 staging、task code 归还与 customer path 保护 | `48de7dc`、`tests/test_task_delete.py` |
| `REPORT-001` + `OBS-001` | ✅ | 按真实 run interval 累计 execution duration，当前 lease 使用权威派生视图 | `795e2db`、`tests/test_timing_view.py` |
| `PROMPT-001` | ✅ | 三 Executor 公共 Prompt contract 单一 builder、golden 与长度门禁 | `0b51af5`、`b3da287`、`fe48ba3` |
| Phase 0 | ✅ | 冻结资源、session、耗尽路由、Claude Project 和真实 CLI fixture 契约 | Phase 0 contract tests |
| Phase 1 / `CFG-001` 配置入口 | ✅ | 原子 TOML 事务、setup 保留式合并、Claude 默认 `$10`、Hermes 默认提取，以及三组配置命令 | `4e60e4f`、`7164bc2` |
| Phase 2 | ✅ | 任务级 resource/session 冻结快照、handoff/reassign 重建、PathPlan、Runner packet 一致性和公共视图 | `b38c8da`、`fea07b2`、`6de4cf1`、`75ffc23` |
| Phase 3 / `CFG-001` 端到端 | ✅ | Claude `--max-budget-usd`、Hermes `--max-turns`、三 Executor session receipt、明确 ID resume 和 Runner fail-closed 校验 | `438030f` |
| Phase 4 Tasks 1～3 | ✅ | Claude/Hermes 资源耗尽结构化识别、Core `input_required` 阻塞、approve 翻倍继续、deny 明确失败、Runner respond-and-dispatch | `2b02891`、`24be4dd`、`b7ba051` |
| Phase 4 Task 4 / `CFG-002` UX | ✅ | 资源决策固定两按钮、approve/deny 映射、fallback 命令、公共资源视图与三份文档 | `QECT-001`、`22c8d61`、`tests/test_phase4_resource_decision_ux.py` |
| Phase 5 / `SESSION-001` cleanup 代码闭环 | ✅ | cleanup receipt/coordinator、Claude 官方 project purge、Codex/Hermes capability、公共 cleanup 诊断 | `19b3b2c`、Phase 5 定向与相邻回归 |
| Phase 6 Tasks 1～3 / `SAFE-001` 授权核心 | ✅ | 通用 grant v1、严格 permission input、Core 生命周期、Runner 原子消费与三 Executor 同 session continuation | `cf82e0b`、`ce74d6d`、`c1111c8`、`908df2c` |
| Phase 6 Task 4 / 权限公共视图 | ✅ | status/preflight/report 脱敏授权投影、通知一致性与持久化一致性门禁 | `0e5b924`、`9776ae5`、`tests/test_phase6_public_views.py` |
| Phase 7 Task 1 / `SKILL-001` | ✅ | canonical controller contract 单一来源、三平台 thin Skill、安装 manifest 与 template hash 握手 | `d21ec13`、`fe6b0ca`、`tests/test_skill_manifest.py` |
| Phase 7 Task 2 / `DOC-002` | ✅ | doctor v2 schema、0/1/2 退出码、text/JSON 同源、Skill/cleanup/config/Runner 漂移诊断 | `580b398`、`050f183`、`tests/test_doctor.py` |
| Phase 7 Task 3 / `DOCFIX-001` | ✅ | help、Record README、双语文档与三平台 Skill 一致性 | `801e7ea`、`2989410`、`tests/test_phase7_doc_consistency.py` |
| Phase 7 Task 4 / 集成门禁 | ✅ | 精确 SHA 上的 Phase 6/7 合同、源码全量、静态、clean-export provenance 与脱敏门禁 | `8XWZ-001`、`private/integration@2989410` |

Phase 4 Tasks 1～3 的集成基线 `b7ba051` 全量 discovery 为 `765` 项通过；Task 4
分支验收新增 `17` 项 UX 测试通过。合并基线 `bd6d6a2` 的 Phase 4 定向 `56` 项和全量
discovery `782` 项全部通过，Ruff、compileall 与 `git diff --check` 通过。已生成并安装的
本地 `1.0.1a3` Phase 4 Task 4 构建已指向 `6f19505`，`build_source=local-phase4-task4`，
package-only smoke 与 CLI/Runner identity 验证通过；正式版本尚未提升为 `1.0.2a1`。

### 0.2 本轮任务与失败链路

| 工作项 | AgentBC 任务 | 当前事实 | 完成后动作 |
| --- | --- | --- | --- |
| Phase 4 Task 4 / `CFG-002` UX、公共视图与文档 | `QECT-001`（Hermes） | `completed`；冻结 `max_turns=150`、显式 `full` 权限，实际运行约 26 分 41 秒；合法 final callback、完成标记、关闭 RunLease 与官方 session receipt `20260811_004323_d3bd9b` 均已验收 | 成果已合入 integration；后续 Claude `P5F7-001` 与 Hermes `BTCN-001` 已完成安装包资源耗尽 canary |
| Phase 5 Task 5 / 公共视图、doctor、文档 | `F6CS-001`（Codex） | 以 `270d671` 为任务基线收口；Phase 5 全部成果已合入 integration `19b3b2c` | 三 Executor retain=false cleanup 与 retain=true 保留 P2P 均已完成 |
| Phase 6 Task 1 / grant 与 strict permission input | `TN3R-002`（Codex） | `completed`；source/target run binding 修正后验收通过，成果已合入 `cf82e0b` | 只保留回归，不再修改冻结的 grant v1 schema |
| Phase 6 Task 2 / Core permission 生命周期 | `P4DH-001`（Claude） | `completed`；4/4 合法 callback、RunLease closed；当前 result receipt 门禁与 recovery revoke fail-closed 已验收并合入 `ce74d6d` | Task 4 公共投影与通知已合入，后续真实 canary 已完成 |
| Phase 6 Task 3 / Runner 与三 Adapter | `PMDQ-002`（Codex） | `completed`；1/1 合法 callback、RunLease closed；unmanaged full 阻断、原子消费和同 session resume 已验收并合入 `c1111c8`，integration 为 `908df2c` | Task 4 公共投影与通知已合入，后续真实 canary 已完成 |
| Phase 6 旧 Task 1 / Git topology 与 `commit_required` 前置合同 | `SWJF-001`（Codex） | Executor 任务已完成，但架构复核后判定 Git 专属状态机和公共 CLI 性价比不足；成果未提交、未合入，Codex worktree 已回退至干净 `19b3b2c` | 任务报告只保留历史证据；不得把该 diff 或其 `--git-write`、Git snapshot、`commit_required` 方案重新带回主线 |
| Phase 7 Task 3 / `DOCFIX-001` help、Record README 与双语/Skill 一致性 | `GE58-001`（Hermes） | 以 `050f183` 为任务基线完成；成果由 `801e7ea` 经 `2989410` 合入 `private/integration` | 只保留一致性回归 |
| Phase 7 Task 4 / 最终集成门禁 | `8XWZ-001`（Codex） | 在精确源 SHA `2989410d72357a07a687fe518047e63c72c990da` 上完成自动化、静态、clean-export provenance、Skill fake install 与公开输出脱敏验证 | Phase 7 代码完成、接口冻结；核心真实 canary 后续已完成，`REL-102` 只保留发布矩阵 |

旧任务 `QDKN-001` 已因旧运行达到 `60/60` 且 final marker 无效而终态 `failed`；
`97FK-001` 因旧 Hermes quiet review 竞争丢失官方 receipt 而 `needs_recovery`。二者均不恢复、
不 handoff，也不是当前开发基线。`QECT-001` 是不继承其 lineage、session、resource 或
运行记录的全新根任务，并验证 thread-scoped output 修复后长任务 receipt 可稳定落盘。

### 0.3 开发截止后的发布门禁

| 工作项 | 状态 | 剩余闭环 |
| --- | --- | --- |
| Phase 4 / `CFG-002` | ✅ 开发与真实 canary 完成 | Claude `P5F7-001` 验证 `0.05→0.1→0.2`、同 session 两次恢复及 deny；Hermes `BTCN-001` 验证 `10→20`、同 session 恢复、再次耗尽及 deny 明确 failed |
| `SESSION-001` | ✅ retain/cleanup P2P 完成 | session 快照、receipt、同 ID resume、终态 cleanup/purge、capability、公共视图与 doctor warning 已完成；retain=false 由 Codex `4PK9-001`、Hermes `C2KS-001`、Claude `FXCQ-001` 验证，retain=true 由 Codex `XHNJ-001`、Claude `2Y86-001`、Hermes `J7WQ-001` 验证 |
| `SAFE-001` | ✅ 1.0.2A 窄合同完成 | Codex `4PK9-001`、Hermes `C2KS-001`、Claude `FXCQ-001` 均已验证 safe→弹窗 Approve→同 session 单次 full→终态 cleanup；细粒度权限与结构化原生审批属于 1.0.3A |
| `REL-102` | 🟡 双架构/三 Python 门禁通过 | `1.0.2a1` 版本、wheel/sdist、隔离安装、只读 setup、shell smoke、三 Executor retain=true，以及 Python 3.10/3.11/3.14 全量均已通过；只剩从公开 main 生成最终不可变资产 Gate |

禁止回归记录：`commit_required`、`--git-write`、`--commit-sha`、`agentbc.git` 及 Phase 6 旧
Task 1（`SWJF-001`）移除的 Git 专属公共命令不得以任何形式（CLI、help、Skill、文档或新状态）
重新出现；`SAFE-001` 的一次性 full 授权只复用既有 permission input 与 respond 命令。

Phase 8 手测修复记录（2026-08-13）：Hermes `E4S2-001` 已确定性触发原生危险命令阻塞并
输出官方 session receipt，但 Agent 生成的 permission reason 为 374 字符，超过 v1 的 240
字符限制，Core 因 `completion_marker_permission_reason_invalid` 将任务直接终结为 failed，
导致用户看不到权限弹窗。当前版改为仅对非空 permission reason 做最多 240 字符的安全截断，
其余 requested permission、唯一 blocked step、session/run 绑定和 native flag 注入继续严格
fail closed；公共 prompt 同时明示 240 字符上限。修正版 `C2KS-001` 已完成两次 run、同一
Hermes session、`resume_count=1`、一次性 full grant 消费、证明文件验证及 session cleanup。
极简首屏和“查看完整原因”交互按钮已转入 `1.0.3A / PERM-103-006`，不回填本版 UI 合同。

Claude canary `KXNX-001` 补充暴露两项边界：`acceptEdits` 会直接允许低风险 Bash 写入，且
retention=false 时进程 cwd 是 `<TASK-ID>/claude` 临时工程，相对交付路径会污染临时工程并
使安全 cleanup 因目录非空停止。当前版采用最小兼容修复：Claude 公共 Prompt 明确所有交付
和产生交付物的命令必须使用所展示的绝对 Project/Artifact root；cleanup 在清空精确 Claude
project 与 task root 后，允许最外层 managed Artifact/chain root 因正常产物或其他 iteration
非空而成功保留。临时 project 或 task root 非空仍 fail closed，不递归删除、不自动搬运。
不依赖 Prompt 的文件级最小写权限已登记为 `1.0.3A / PERM-103-007`。

Claude 修正版真实 canary `FXCQ-001` 使用 `customer-path="default path"` 完成两次 run、
`resume_count=1`、一次性 `safe -> full` 授权消费、绝对 Artifact Root 写入和字节校验；终态
`claude_project_purge` 首次成功，`<TASK-ID>/claude` 与空 task root 均已移除，外层 Artifact
root 因保留正式产物而正确存在。本轮同时冻结三项 setup/CLI 体验修正：首次 setup 权限默认
为 `inherit`，旧任务缺少权限快照仍回落 `safe`；首次 retention 明确以 `[y/N]` 默认不保留；
`task delete <TASKCODE>` 改为先展示将删除的 record、brief/report、index 与默认 Artifact，
再内置 `y/N` 确认，移除多余的公开 `--confirm`，`--dry-run` 继续零写入且不提示。

2026-08-14 集中测试又确认两个实现缺陷。第一，Doctor 原先在 CLI/Controller 进程内用
`os.access()` 判断 workspace/report/record 可写性，Codex safe 沙箱会因此误报
`unavailable`，即使沙箱外 Runner 已能正常派发和写报告。修复后 Doctor 通过受限的只读
Runner IPC 探测这三个精确路径，Runner 只接受自身 allowed roots 内的最多八个路径，不返回
allowed roots 或原始异常；Runner 不可用、身份漂移、旧协议或回执畸形时仍 fail closed。
Doctor v2 的公共字段和 `0/1/2` 退出码合同保持不变。

第二，Hermes `2WXY-001` 在 `max_turns=10` 时确实输出官方
`Reached maximum iterations (10)`，但紧随其后的 Agent 普通 `input_required(type=choice)`
抢占了终态优先级，导致 Core 没有创建 `resource_limit` approve/deny input，任务快照也未
翻倍。修复后 Hermes 原生迭代耗尽对普通/choice wait 具有权威优先级；合法 completed、严格
permission wait 与 retryable transport recovery 仍保持原优先级。Claude `P5F7-001` 已验证
`0.05 -> 0.1 -> 0.2`、同一 session 两次 resume、第三次耗尽 deny 后以
`budget_exhausted_user_terminated` failed，Hermes 必须在新候选包上重新执行同等 canary。

Hermes 修正版真实 canary `BTCN-001` 使用候选代码 `b8af2f3`，冻结
`max_turns=10`。首次运行以规范 argv `--max-turns 10` 命中原生耗尽，Approve 后任务快照
翻倍到 `20`，第二次运行以 `--max-turns 20 --resume 20260814_004550_6f27c1` 恢复同一
官方 session；再次耗尽时用户选择终止，任务以
`iteration_exhausted_user_terminated`、`retryable=false` 结束。两次 RunLease 均关闭，
`resume_count=1`，14 个 checkpoint 及 SHA-256 依赖链真实存在，Hermes 官方定向 session
cleanup 首次成功。该证据关闭 `CFG-002` 的 Hermes 真实 canary。

`BTCN-001` 同时暴露一个不阻断本版发布的公共进度低估：原生资源耗尽覆盖 Agent 生成的
普通/choice callback 时，Core 正确采用系统资源决策，但 callback 中已经完成的 step
progress 也被整体丢弃，终态报告因此显示 `0/4`，低于实际完成度。不得在 1.0.2A 重新引入
Agent choice 抢占资源信号；该问题转入 `1.0.3A / FLOW-103-001`，由 Runner/Core 权威的
结构化 progress receipt 解决，不从自然语言或被覆盖 callback 猜测进度。

### 0.4 截止后固定顺序

1. 冻结 `b8af2f3` 为 1.0.2A 开发截止代码；后续文档收口提交不得改变该运行代码；
2. 只执行 `REL-102` 剩余发布验收：从公开 main 生成最终不可变资产并校验发布身份；发布
   验收失败不自动等于重开开发；
3. 新功能、常规缺陷、协议优化和 `BTCN-001` 进度低估统一进入 1.0.3A；
4. 正式发布必须从唯一最终发布提交生成不可变版本、manifest、wheel/sdist 与 checksum，
   不覆盖既有同名公开资产。

开发截止不等于公开发布。后续 Agent 不得因本地新包和核心 canary 通过就宣称
`1.0.2A` 已发布，也不得以发布门禁未执行为理由继续向本版增加常规代码。

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

`908df2c` 时点的源码全量盘点为 `923` 项、`10` 个已知失败：`7` 个 Hermes Runner 旧 fixture
未回显预分配 `executor_run_id`，`2` 个 Codex/Claude Skill 文案一致性失败，以及本地被忽略的
构建产物 `_build_info.json` 导致 `1` 个 provenance 环境失败。前三类分别由 Phase 6 Task 4、
Phase 7 Task 1 和干净导出树门禁解决；`050f183` 时点源码全量 unittest discovery 为 `972` 项
全部通过，不再保留已知失败。不得通过放宽 Runner ID 校验或提交 build artifact 修复。

Phase 7 Task 3（`DOCFIX-001`）新增 `tests/test_phase7_doc_consistency.py` 一致性门禁：共享
命令表与 CLI help 一致、record clean 只清理 eligible 终态运行时诊断且报告永不删除、
Record README 的 queued/pending head close 语义、退休命令
（`--git-write`/`--commit-sha`/`commit_required`/`agentbc.git`）缺席、中英文文档与三平台
Skill 对齐，均在不弱化既有契约的前提下验证。

Phase 7 Task 4（`8XWZ-001`）在精确源 SHA
`2989410d72357a07a687fe518047e63c72c990da` 上完成最终集成门禁：使用满足
`requires-python >=3.10` 的 Python 3.11.15 运行 Phase 6/7 与相邻合同矩阵 `292` 项，以及
源码全量 unittest discovery `986` 项，均为 `failures=0`、`errors=0`、`skips=0`；Ruff
0.15.13 `--no-cache`、compileall、`git diff --check` 通过；干净 `git archive` 中无
`_build_info.json`，provenance `47` 项通过；三平台 Skill fake install/manifest `18` 项通过，
Doctor/cleanup/permission 公共投影脱敏与退休 Git 公共面缺席门禁通过。该证据只关闭 Phase 7
代码与接口门禁，不代表 Python 版本矩阵、真实 Executor/P2P、安装包、双机或发布 Gate 通过。

开发截止基线 `b8af2f3` 的最终源码 discovery 为 `1007` 项全部通过；Doctor/Runner、Hermes
iteration、资源决策、Phase 7 文档等最终相关回归 `172` 项通过，Ruff、compileall 与
`git diff --check` 通过。手工候选 wheel 的 `_build_info.json` 精确指向 `b8af2f3`；安装并
重启 Runner 后，Codex safe 沙箱内 `agentbc doctor --json` 返回 `healthy/0`，Runner identity
match，workspace/report/record 三项均由 Runner 权威探测为 writable。该证据固定开发截止
代码和本机手工候选，不替代 Python 3.10/3.14、MacBook 或最终不可变发布资产门禁。

当 Phase 5～7 代码路径均已合入、无已知 P0 实现缺口且接口/文档基本冻结后，集中测试按
以下顺序执行：源码全量回归 → 构建 provenance 与 wheel/sdist → clean install/upgrade →
CFG-002 资源耗尽点对点 → SESSION-001 retain/cleanup 点对点 → SAFE-001 linked-worktree →
doctor/Skill/help 一致性 → Python/双机矩阵 → 失败注入、恢复、回滚与数据安全复核。集中测试
重点不是单纯“命令退出 0”，而是核对 task/status/report/RunLease/receipt/session ID、同
Task resume、清理边界、用户数据保护、secret redaction、CLI/Runner identity 与最终 SHA
是否一致。

集中测试新增权限持久化一致性门禁：使用同一候选包的 CLI 与 Runner，分别创建三 Executor
的显式 `safe`、显式 `full` 和配置默认任务；从 create/dispatch 返回、task brief、磁盘权威
task snapshot、`permission_audit`、Runner 实际授权、终态 status/report 逐层核对
`requested_mode`、`effective_mode` 与 `selection_source`。显式 `full / explicit_task` 在任何
Worker、finalize、report 或旧数据兼容路径中都不得降级为 `safe / legacy_task`；同时反向验证
旧任务缺少 permission extension 时才允许稳定回落 legacy safe。CLI/Runner build identity 或
协议版本不一致时必须先阻断 canary，不得把混装结果当成权限实现结论。

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
- 统一 Agent 权限设置、三 Executor 权限 registry、细粒度 capability、授权时效和多次 scope
  仍冻结到 `1.0.3A`。`1.0.2A` 只实现三 Executor 共用的窄场景：基础权限为 `safe` 时，
  复用已经存在的 `input_required(type=permission)`、approve/deny UI 和同 session resume，
  为下一次 continuation 发放一次性既有 `full` 授权。不得新增 Git 专属任务状态、权限类别
  或公共响应命令；`inherit` 不参与临时提升。
- 内部授权使用版本化、非 Git/Executor 专属的可扩展 envelope；v1 只解释通用绑定、
  `safe -> full`、下一次 run 和一次消费，保留未知附加字段，未知未来版本 fail closed。
  Service、Runner 和 Adapter 不得分别写死 Codex/Claude/Hermes 字段合同；1.0.3A 可在不改写
  v1 基础 `agentbc.permission` 的前提下增加 registry、granular target 和新 scope。

`1.0.2A` 开发阶段采用过以下人工过渡规则（现只保留为历史操作证据）：每次创建新的根任务或 handoff 前，控制端必须先
向用户确认目标 Agent 完成本任务实际需要 `safe` 还是 `full`，再在派发命令中显式传入
`--permission-mode <safe|full>`；不得依赖 AgentBC 配置、来源任务或 Executor 原生配置的
隐式继承。`full` 必须明确提示风险并取得本次派发授权。除上述一次性 permission
approve 外，同 Task 的 retry/recover/resume 继续使用已冻结权限，普通 message、choice、
prompt 文本或原生参数都不能改权。一次性授权消费或中断后必须恢复原 `safe`，不得被
retry、recover、handoff 或新任务继承。

允许的重构只限于支撑本清单功能的局部公共模块，并且机械迁移与语义变化必须分提交。

## 2. 开发截止源码盘点

| 项目 | 当前状态 | 1.0.2A 处理口径 |
| --- | --- | --- |
| `task delete` | 已完成并合入 | 保持整条终态历史链安全删除的回归门禁 |
| `doctor [--json]` | doctor v2、0/1/2、Runner 权威 storage probe、Skill/cleanup/config/runtime 诊断均已完成 | 只保留发布环境与身份回归 |
| 执行会话保留 | 快照、receipt、同 ID resume、终态 cleanup capability/coordinator，以及三 Executor retain=false/retain=true P2P 均已完成 | 只保留发布候选回归 |
| Claude 预算 / Hermes 迭代 | 配置、任务快照、Adapter argv、Runner 校验、耗尽决策 UX 与真实 approve/deny canary 已完成 | 只保留发布候选回归；进度低估转入 `FLOW-103-001` |
| 三 Executor safe 与 linked worktree | 一次性 `safe→full` grant、同 session resume、弹窗、脱敏投影与三 Executor canary 已完成；linked-worktree 不扩大外部 `.git` writable roots | 只保留 1.0.2A 窄合同；统一 registry、原生 approval 与细粒度权限转入 1.0.3A |
| 执行时长 | 已完成真实 run interval 累计和权威 lease 当前视图 | 保持 status/report/notification 同源回归 |
| Prompt 公共契约 | 已完成共享 builder、golden 和长度门禁 | 保持 v1 行为不变回归 |
| Skill 身份 | canonical controller contract、三平台 thin Skill、manifest/hash 握手与 doctor 漂移检测已完成 | 只保留安装与发布身份回归 |
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
  Runner command/snapshot/cwd fail-closed 校验。`CFG-001` 端到端完成；该 Phase 3
  时点的 `SESSION-001` 只剩终态 cleanup/purge 与能力回执，`CFG-002` 当时仍保持打开。
- `2026-08-10 / Phase 4 Tasks 1～3`：已完成资源耗尽的 Adapter 结构化识别、Core
  `input_required`、任务级翻倍/终止决策和 Runner 原子响应派发，并合入集成基线
  `b7ba051`；全量 discovery `765` 项通过，不再保留 Phase 4 `expectedFailure`；
  Ruff、compileall、Shell 语法和 `git diff --check` 通过。
- `2026-08-11 / Phase 4 Task 4（CFG-002 UX 切片）`：已完成资源耗尽 choice 的弹窗
  按钮映射（`kind=resource_limit` + `response_protocol=approve_deny` 下
  「提高预算并继续」→ `approve`、「终止任务」→ `deny`，Later/关闭/超时保持
  waiting）、fallback `--approve/--deny` 命令、公共 execution policy 视图新增
  `configured_limit` / `exhaustion_count` / `last_decision`、status/preflight/report
  一致展示，以及 1.0.2A 清单、开发手册、中文用户指南三份文档同步。该 Phase 4
  时点的 `SESSION-001` cleanup 仍保持打开；本切片不实现 purge/delete。Hermes
  `QECT-001` 以 `max_turns=150`、`full` 权限完成约 26 分 41 秒实跑并保存官方 session
  receipt `20260811_004323_d3bd9b`；成果由 `22c8d61` 经 `bd6d6a2` 合入。
- `2026-08-11 / Phase 5`：以集成基线 `270d671` 完成 cleanup contract/coordinator、Claude
  managed project purge、Codex exact UUID delete with `--force`、Hermes official sessions
  delete capability，以及 status/preflight/report 单一安全投影、doctor text/JSON 同源 warning
  与双语/Skill 文档。Phase 5 为代码完成/待集中验证；未执行真实 session deletion 或 P2P，
  `SESSION-001` 在后续真实 retain/cleanup P2P 通过前仍为部分完成。
- Phase 4 合并最终证据：定向 `56` 项、全量 discovery `782` 项通过，Ruff、compileall
  与 `git diff --check` 通过，不保留 Phase 4 `expectedFailure`。
- `2026-08-12 / Phase 6 Task 4`：完成通知与 status/preflight/report 脱敏授权投影、权限
  持久化一致性门禁，由 `0e5b924` 经 `9776ae5` 合入（`tests/test_phase6_public_views.py`）。
- `2026-08-12 / Phase 7 Task 1（SKILL-001）`：canonical controller contract 单一来源、三平台
  thin Skill 与安装 manifest/template hash 握手由 `d21ec13` 经 `fe6b0ca` 合入
  （`tests/test_skill_manifest.py`）。
- `2026-08-12 / Phase 7 Task 2（DOC-002）`：doctor v2（schema_version=2、text/JSON 同源、
  0/1/2 退出码、Skill/cleanup/config/Runner 漂移诊断）由 `580b398` 经 `050f183` 合入
  （`tests/test_doctor.py`）。
- `2026-08-12 / Phase 7 Task 3（DOCFIX-001）`：help 与 Record README 语义修正、README/
  Quick Start/User Guide 双语对齐、canonical contract 与三平台 thin Skill 同步及
  `tests/test_phase7_doc_consistency.py` 一致性门禁由任务 `GE58-001` 完成，成果由
  `801e7ea` 经 `2989410` 合入。
- `2026-08-12 / Phase 7 Task 4（最终集成门禁）`：任务 `8XWZ-001` 在精确源 SHA
  `2989410d72357a07a687fe518047e63c72c990da` 上通过 Phase 6/7 合同矩阵 `292` 项、全量
  discovery `986` 项、Ruff、compileall、`git diff --check`、clean-export provenance、
  Skill fake install 与公开输出脱敏验证；Phase 7 代码完成、接口冻结，但不关闭真实 canary、
  Python/双机、包验证或 `REL-102`。
- `2026-08-13 / 1.0.2A 手工测试候选`：版本统一为 Python `1.0.2a1`；Python 3.11
  源码全量 `986` 项、Ruff、compileall、wheel/sdist provenance 与 SHA256、隔离 wheel 安装、
  `setup --show` 只读路径和 shell task/report 闭环 smoke 通过。精确候选 commit 与 source hash
  由包内 `_build_info.json` 和随包 `release-manifest.json` 记录；真实三 Executor、资源耗尽、
  session retain/cleanup、Python 3.10/3.14 与双机门禁仍保持打开。

## 3. 需求总表

| ID | 状态 | 优先级 | 需求 | 主要责任模块 | 依赖/剩余边界 |
| --- | --- | --- | --- | --- | --- |
| `DEL-001` | ✅ 已合入 | P0 | 安全删除终态历史链并归还 task code | Service、Store、ID、CLI、Index | 只保留回归 |
| `SESSION-001` | ✅ 开发与 retain/cleanup P2P 完成 | P0 | 执行 Agent 临时会话保留策略 | Config、Setup、CLI、Adapter、Worker/Service、Doctor | retain=false 与 retain=true 三 Executor 均通过 |
| `SKILL-001` | ✅ 已合入 | P0 | Controller contract 单一来源与 Skill hash 握手 | Skill template、Setup、Doctor | 只保留回归；`d21ec13`/`fe6b0ca` |
| `DOC-002` | ✅ 已合入 | P0 | 完成只诊断 doctor 契约 | Doctor、Registry、Runner query、CLI | 只保留回归；`580b398`/`050f183` |
| `REPORT-001` | ✅ 已合入 | P1 | 修正恢复任务累计执行时长 | RunLease、Service timing、Report、Task List、Notification | 只保留回归 |
| `CFG-001` | ✅ 已合入 | P0 | Claude 预算与 Hermes 迭代上限配置及执行注入 | Config、Setup、CLI、Claude/Hermes Adapter、Preflight | doctor 最终视图待 DOC-002 |
| `CFG-002` | ✅ 开发与真实 canary 完成 | P0 | 预算/迭代耗尽决策：弹窗翻倍继续或终止 | Adapter、Worker/Core、Notifications、Service respond | Claude `P5F7-001`、Hermes `BTCN-001` |
| `SAFE-001` | ✅ 开发与真实 canary 完成 | P0 | 三 Executor safe 受阻后，经现有 permission input 明确批准，为下一次同官方 session continuation 一次性启用 full | Permission、三 Adapter、Runner、Input、Notification、Report | Codex `4PK9-001`、Hermes `C2KS-001`、Claude `FXCQ-001` 已通过；细粒度权限转入 1.0.3A |
| `PROMPT-001` | ✅ 已合入 | P1 | 三 Executor 公共 Prompt 契约去重 | 公共 builder、Codex/Claude/Hermes Adapter | 只保留回归 |
| `OBS-001` | ✅ 已合入 | P1 | 当前 execution lease 状态单一派生视图 | RunLease query、Status、Report | 与 REPORT-001 同步完成 |
| `DOCFIX-001` | ✅ 已合入 | P2 | 修正文档/help 漂移 | Record README、CLI help、双语文档、Skills | `GE58-001` 完成，由 `801e7ea` 经 `2989410` 合入；只保留一致性回归 |
| `REL-102` | 🟡 双架构/三 Python 门禁通过 | Gate | 1.0.2A 版本、双机、真实 Executor 与发布验收 | Build/CI/docs/release | Python 3.10/3.11/3.14、ARM64/x86_64、wheel/sdist、安装/升级/回退、shell smoke 与 retain=true 已通过；只剩公开 main 最终资产 |

原始工作量估算只保留为历史规划，不再用于推断当前剩余进度。剩余工作以 0.3 和 0.4
为准；任何真实 Executor 能力缺失应按 `unsupported` 交付，不得用危险文件扫描缩短排期。

## 4. 详细需求与验收

### 4.1 `DEL-001`：`agentbc task delete`

固定接口：

```text
agentbc task delete <TASKCODE> --dry-run
agentbc task delete <TASKCODE>
```

要求：

- 只接受任务链 code，不接受单次 iteration ID；
- 只有整条链均为 `completed/failed/cancelled/rejected` 才允许删除；
- `pending/running/input_required/needs_recovery` 任一存在时整链拒绝；
- `--dry-run` 只输出所有将删除和保留的对象，零磁盘写入；
- 普通 delete 必须先展示将删除的任务记录、任务说明/报告、索引项和默认 managed artifact，
  再以 `y/N` 询问；只有 `y/yes` 提交，Enter、`n`、EOF、Ctrl-C 全部取消且零写入；
- 不提供公开 `task delete --confirm`，避免用户先记忆额外危险开关再获得真正风险说明；
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
强制保留、session ID receipt 和同会话 resume；Phase 5 已完成终态 cleanup/purge、
cleanup capability/receipt、公共投影与 doctor warning 的代码接线。真实 retain/cleanup
P2P 门禁通过前，`SESSION-001` 仍保持部分完成。

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
- [x] 终态 cleanup/purge、cleanup capability/receipt、status/preflight/report 公共投影与
  doctor warning 已接线；真实 retain/cleanup P2P 延后到集中验证。

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

已合入状态：Phase 7 Task 1 `SKILL-001` 由 `d21ec13` 经 `fe6b0ca` 合入（canonical
controller contract 单一来源、三平台 thin Skill、安装 manifest 与 template hash 握手，
`tests/test_skill_manifest.py`）；Phase 7 Task 2 `DOC-002` 由 `580b398` 经 `050f183` 合入
（doctor `schema_version=2`、text/JSON 同源、0/1/2 退出码、Skill/cleanup/config/Runner 漂移
诊断，`tests/test_doctor.py`）。

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

- Adapter 在优先级允许时识别锚定资源耗尽信号，输出结构化
  `resource_exhaustion` / `failure.kind=resource_limit_exhausted`；Claude 优先结构化
  `error_max_budget_usd`，文本 fallback 只接受 CLI error 位置的精确形式；Hermes 只接受
  既定原生锚定信号，缺少数字上限时使用任务冻结快照，禁止从普通 prompt/output 回声误判；
  Hermes 原生耗尽覆盖同一输出中 Agent 生成的普通/choice input_required，避免总结回调
  绕过资源状态机，但不覆盖合法 completed、严格 permission wait 或 retryable transport failure；
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
- [x] Claude 安装包真实 canary `P5F7-001`：两轮耗尽→approve→同 session 继续，第三轮
  耗尽→deny→明确 failed；任务资源快照与 cleanup receipt 均已核对；
- [x] Hermes 安装包真实 canary `BTCN-001`：`10→20`、同 session resume、第二次耗尽
  deny→`iteration_exhausted_user_terminated`，RunLease closed、cleanup succeeded。

Task 4 已落地子项（弹窗/视图/文档切片）：

- [x] `kind=resource_limit` + `response_protocol=approve_deny` 的 choice 弹窗两按钮
  「提高预算并继续」/「终止任务」分别映射 `approve` / `deny`；Later、关闭弹窗与
  超时保持 `waiting`（dismissed，不推进任务状态）；fallback 命令展示
  `--approve` / `--deny`；普通 choice 仍以消息选项提交（`--message`），语义不变；
- [x] 公共 execution policy 视图在兼容 `limit`（当前生效上限）字段基础上新增
  `configured_limit`、`exhaustion_count`、`last_decision`；status/preflight/report
  一致展示，不暴露 raw output、secret 或内部 Claude project path；
- [x] 1.0.2A 清单、开发手册、中文用户指南三份文档同步 Phase 4/CFG-002；
  该 Phase 4 切片当时保持 `SESSION-001` cleanup 打开；Phase 5 后续已完成代码接线。

`SESSION-001` cleanup 代码与三 Executor retain=false P2P 已完成；2026-08-14 的
Codex `XHNJ-001`、Claude `2Y86-001`、Hermes `J7WQ-001` 又验证 retain=true：三项均
`completed`、`2/2`、合法 callback、Report ready、RunLease closed、官方 session receipt
存在，cleanup 为 `retained` 且 `attempts=0`。Claude 同时经历 `0.05→0.1`、两次 run 和
同 session resume；Doctor 为 `healthy/0`、零 blocker。验收后全局 retention 已恢复为
`false`。`CFG-002` 的 Claude/Hermes 安装包真实 canary 均已完成。

验收：claude 预算耗尽、hermes 迭代耗尽、翻倍继续成功、终止 failed 带原因、
到期转恢复、弹窗文案、无密钥泄漏与 status/report 一致展示通过。

### 4.6 `SAFE-001`：三 Executor safe 受阻后的单次 full continuation

问题边界：Codex `safe` 的 workspace-write 可能阻止 linked worktree 外部 Git 元数据写入；
Claude 的 acceptEdits 仍可能要求 Bash/tool approval；Hermes 的正常审批路径在非交互执行中
也可能阻塞。根因都是当前任务冻结为 `safe`，而已有 `input_required(type=permission)` 的
approve 只恢复任务，不会改变下一次 Executor run 的权限。解决方案复用现有任务状态、
permission 类型、三 Executor 既有 `full` 映射和 SESSION-001 同 session resume，不把某个
具体命令、Git 提交或 Executor 建模为新的权限分类。

要求：

- 不新增 `commit_required`、`agentbc.git`、`--git-write`、`--commit-sha`、Git proxy 或
  controller commit receipt；不要求用户理解 checkout 类型、branch/HEAD、dirty set 或
  Executor/Controller 提交分工；`SWJF-001` 的未合入实现已回退，不是后续开发基线；
- 任一 Executor safe 运行遇到确需 `full` 才能继续的步骤时，使用现有 strict final marker 进入
  `input_required`，`input.type=permission`、`requested_permission=full`，并标记实际 blocked
  step；普通 message/choice、自由文本“允许”、Executor 原生 flags 均不能触发提权；
- notification 继续使用现有 Approve/Deny 和 fallback
  `agentbc task respond <task-id> --input <input-id> --approve|--deny`，不新增响应命令。
  弹窗必须明确说明：批准将为对应 Executor 的下一次 continuation 临时启用完整 `full`，其技术权限
  范围不限于 Git；任务结束、中断或再次等待后自动撤销；
- 创建 permission wait 前必须存在当前 run 的官方 execution session receipt；Codex/Claude/
  Hermes 一律不得猜测或补写 session ID。Hermes 若在 receipt 产生前受阻，直接以稳定原因
  `permission_resume_session_unavailable` 进入 `needs_recovery`，不得发放 full 或新开 session；
- approve 后保持同一 Task ID、current chain head 和 `agentbc.session.session_id`，Runner
  原子发放一次性授权并恢复下一次 run。授权只绑定当前 task/input/executor/session，只能
  消费一次；adapter 使用对应 Executor 的现有 `full` argv，Runner 仍执行已有 permission command
  canonicalization 与 persisted authorization 校验；
- 基础任务权限仍为 `safe`。一次性授权在成功派发后视为已消费，并在 completed、failed、
  cancelled、再次 `input_required`、`needs_recovery` 或 resume dispatch 失败时恢复 `safe`。
  retry、recover、reassign、handoff、新任务和 input replay 均不得继承或重新使用；失败后若
  仍需 full，必须产生新的 permission input 并重新批准；
- deny 直接终结任务为带稳定原因 `permission_denied_by_user` 的 failed；expired、stale、
  task/input/session 不匹配、重复 approve、授权篡改或 Runner 无法验证 full argv 均 fail
  closed，不自动重派 full；
- 权限弹窗严格只有允许/拒绝两个动作，不提供 Later 或文本输入；默认拒绝，关闭或超时自动
  拒绝，超时稳定原因为 `permission_denied_by_timeout`。普通 message/choice 文本永远不得被
  解释为权限批准；
- 一次性授权必须留下脱敏审计：原权限、临时权限、input ID、目标 run、issued/consumed/
  revoked 状态和时间。不得保存 prompt、raw command/output、token、secret 或私有会话内容；
  status/report 只显示是否存在当前临时授权、来源和消费结果，不引入新的任务状态。

验收：三 Executor 原有 safe/full/inherit 派发不受影响；safe 受阻后可进入现有 permission
input；有官方 receipt 时 approve 后同 Task/同 session 的下一次 run 使用 full，Hermes 无
receipt 时严格进入 recovery；deny 明确 failed；grant 一次消费、超时、重复响应、resume
dispatch 失败、needs_recovery、retry、recover、reassign、handoff 和新任务均不能泄漏 full；
通知、status/report 与 permission audit 同源，且用户流程不出现新的任务状态或 Git 权限概念。

2026-08-13 手工 canary 修正：`Y6NX-001` 暴露 permission blocker 被误报为 message、权限弹窗
存在 Later/文本歧义，以及 cancel 后遗留 waiting input、suspended RunLease、input_required
session，令 cleanup 门禁长期停在 `not_requested`。修复要求已纳入自动化：权限 blocker 必须
使用 strict permission marker；取消必须原子关闭 input/RunLease/session、生成终态 Report 与
notification，再进入后台 cleanup。Claude ephemeral project 在官方 purge 前不得被通用取消清理
递归删除。后续 Codex `4PK9-001`、Hermes `C2KS-001`、Claude `FXCQ-001` 已完成新安装包
permission/cleanup canary，关闭 1.0.2A 的窄合同开发验收；极简原因展开和原生结构化审批
继续由 1.0.3A 承担。

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

Phase 7 Task 3（`GE58-001`）已完成：CLI help 改为“只清理 eligible 终态任务运行时诊断，
报告永远不会被删除”；Record README 改为“当前排队中（pending）或活跃的 chain head 可
close，终态与过期迭代拒绝”；README/README_ZH、Quick Start/Quick Start ZH、User
Guide/User Guide ZH、开发手册、canonical controller contract 与三平台 thin Skill 统一到
同一命令与行为契约（claude budget、hermes max-turns、session retention、safe/full
permission input approve/deny、后台 executor-session cleanup、record clean、queued-head
close、doctor 0/1/2、永不删除 dispatcher conversation）；新增
`tests/test_phase7_doc_consistency.py` 一致性门禁，成果由 `801e7ea` 经 `2989410` 合入；
Task 4 在精确源 SHA `2989410d72357a07a687fe518047e63c72c990da` 上完成最终集成验证，
Phase 7 代码完成、接口冻结。

## 5. 当前 Phase 计划与分工原则

### 5.1 已结束的开发批次

- 初始 Wave 1：`DEL-001`、`REPORT-001/OBS-001`、`PROMPT-001` 已合入；
- Phase 0～3：资源配置、任务冻结、PathPlan、Adapter argv、session receipt/resume 已完成；
- Phase 4 Tasks 1～3：资源耗尽识别、Core wait、approve/deny 与 Runner 响应派发已合入。

以上项目只做回归或缺陷修正，不得在后续 Phase 中被重新规划成“尚未实现”。

### 5.2 当前与后续 Phase

| Phase | 状态 | 目标 | 进入下一阶段的硬门禁 |
| --- | --- | --- | --- |
| Phase 4 | ✅ 开发与真实 canary 完成 | `CFG-002` UX、公共视图、文档、Claude/Hermes approve/deny 同 session canary | `P5F7-001`、`BTCN-001` |
| Phase 5 | ✅ 开发与 retain/cleanup P2P 完成 | `SESSION-001` 终态 cleanup/purge、capability/receipt、失败重试、公共视图与 doctor warning 数据已接线 | retain=false 与 retain=true 三 Executor P2P 均通过 |
| Phase 6 | ✅ 开发与真实 canary 完成 | `SAFE-001` grant/Core/Runner/三 Adapter、Task 4 通知/公共投影及三 Executor canary 完成 | 只保留发布候选回归；1.0.3A 承担统一 registry 与细粒度权限 |
| Phase 7 | ✅ 代码完成、接口冻结 | `SKILL-001`、`DOC-002`、`DOCFIX-001` 已合入，Task 4 精确 SHA 集成门禁通过 | Skill hash/版本握手、doctor 0/1/2、text/JSON 同源、双语文档/help/Skill 一致 |
| 集中全面测试 | ✅ 双架构/三 Python 收口 | 源码全量、`1.0.2a1` 包、Doctor、三 Executor 权限/retain/cleanup、Claude/Hermes 资源链，以及 Python 3.10/3.11/3.14 均有证据 | 只剩公开 main 最终不可变资产属于发布门禁 |
| Phase 8 | ✅ 开发截止 | Doctor sandbox 与 Hermes 原生耗尽优先级修复合入 `b8af2f3` | 不再接收常规实现；只继续 `REL-102` 发布验收 |

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

### 5.4 Phase 6 修订后开发任务

Phase 6 不沿用 `SWJF-001` 的代码，也不实现 Git topology/commit 状态机。按以下依赖实施：

Tasks 1～3 已分别通过 `TN3R-002`、`P4DH-001`、`PMDQ-002` 完成并合入
`private/integration@908df2c`；后续只执行 Task 4 和阶段集成，不重开授权核心合同。

1. **Task 1：permission input 与可扩展的一次性授权合同**
   - 冻结 permission request/grant 的内部 schema、一次消费、失效和审计规则；
   - 扩展现有 strict permission input 校验，deny 明确 failed；
   - 三 Executor 使用同一 envelope；v1 只支持基础 safe、请求 full，不提前实现 1.0.3A registry。
2. **Task 2：Core 输入生命周期与 session receipt 门禁**
   - permission wait 必须绑定当前 run 的官方 receipt；Hermes receipt 缺失时进入 recovery；
   - approve 生成 issued grant，deny 明确 failed；terminal/recovery/reassign/dispatch failure 撤销；
   - retry/recover/handoff/new task 不继承授权，资源决策与 permission 决策严格分流。
3. **Task 3：Runner、三 Adapter 与同 session resume**
   - Adapter 通过公共 resolver 复用现有 full argv，不自行解释 grant 字段；
   - Runner 从持久化 issued grant 原子绑定/消费下一次 run，并拒绝注入、漂移和重放；
   - 所有终态、中断、再次等待和 dispatch failure 自动撤销，retry/recover/handoff 不继承。
4. **Task 4：通知、公共视图和文档**
   - 复用现有 Approve/Deny popup 和 respond 命令；
   - 明示 full 权限范围不限于 Git且只作用下一次 continuation；
   - status/report 展示脱敏授权来源、是否临时、消费/撤销结果，不新增任务状态。
5. **阶段集成与自动化门禁**
   - fake Executor 覆盖 approve/deny、same session、single-use、stale/replay、崩溃恢复和权限回落；
   - 普通 clone/linked worktree 只用临时 fixture；真实 Codex canary 按 0.5 节集中执行；
   - Ruff、compileall、`git diff --check` 与受影响 input/permission/session/Runner 回归通过。

Task 1 合入后 Tasks 2～3 可按文件所有权并行；Task 4 依赖二者全部合入并最后收口。任何子任务不得
恢复 `--git-write`、`--commit-sha`、`commit_required` 或把临时 full 持久化为 handoff 默认。

### 5.5 分工与合并护栏

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
- 三 Executor safe 受阻时复用现有 permission input；用户 approve 后仅下一次同官方 session
  continuation 临时使用 full，随后自动失效；Hermes 缺少官方 receipt 时进入 recovery；deny、
  超时、异常、retry/recover/handoff 均不得泄漏或重放授权；
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
