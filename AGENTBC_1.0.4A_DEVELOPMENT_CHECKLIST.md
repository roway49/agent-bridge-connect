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
| `PROTO-104-001` | P0 / 已完成（2026-08-27） | 上游 CLI version/help/argv/event 漂移只能靠临时补测试 | 三 Executor 完整版本化 fixture、capability matrix 和未知组合 fail-closed | 无 |
| `ARCH-104-001` | P1 / 按域执行 | Service、Runner、CLI、approval、notification 责任仍集中 | 每个功能项先完成对应窄模块机械拆分，公共 API/CLI/磁盘行为不变 | `PROTO-104-001` |
| `PERM-104-001` | P0 | native Deny 后 Agent 仍可用 Prompt/callback 请求 full 并启动第二 worker | 审批渠道和 fallback 完全由可信 transport event 与 Core policy 决定 | permission fixtures；对应 ARCH slice |
| `PERM-104-002` | P0 | 已批准动作仍被宿主 containment 拒绝时会重复弹窗、grant 和 continuation；显式 full 还可能进入不可再升级 recovery | 稳定 fingerprint + escalation domain；人工授予、临时申请和权限继承三条路径的 full 都必须真实生效且在声明范围内无阻塞；完成后执行 `PERM-104-002-R1` 详情回归 | `PERM-104-001` |
| `FLOW-104-002` | P0 | report/record 超限可跳过终态通知和 cleanup receipt | terminal、report、notification、cleanup 独立且可重放，通知不被报告失败吞掉 | terminal fixtures；对应 ARCH slice |
| `FLOW-104-001` | P1 | handoff 只能声明一个 step，自由文本多步骤直到 callback 才失败 | handoff 原生结构化 steps、dispatch 前预检、严格 callback 一致性 | schema fixtures；对应 ARCH slice |
| `FLOW-103-001` | P1 / 跨版转入 | 资源耗尽或系统终态覆盖 callback 时会把真实部分进度回退 | task/run/session scoped 单调 progress receipt；所有公共视图同源 | `FLOW-104-001` 的 declared steps |
| `SESSION-104-001` | P0 / 已完成（2026-08-27） | cleanup 成功只证明 CLI delete 返回成功，未证明 Codex CLI 与 Desktop 的恢复列表都彻底移除同一临时会话 | 以官方 session ID 做双入口、重启后和保留边界的真实验收；不能确认时 fail closed | `PROTO-104-001` Codex session fixtures；`FLOW-104-002` cleanup receipt |
| `FLOW-104-003` | P0 / 恢复闭环 | 当前终态 `failed` 既不能 `task retry`，也不能作为普通 handoff 源，失败后只能人工绕行 | status/report 给出机械可判定的 retry/handoff 动作；同任务重试与新 iteration 交接均保留审计和进度 | failure taxonomy；`FLOW-104-002` terminal receipts；`FLOW-104-001` steps |
| `INPUT-104-001` | P0 / 派发阻断 | 显式 custom path 时，位于项目根之外的 `--image`/输入文件被 `image input is outside task roots` 原子拒绝 | 项目根与只读附件根分离；Runner 受控导入外部文件且不扩大 Executor 项目权限 | PathPlan v2；atomic dispatch；input manifest |

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

- `PERM-104-002` 必须在 `PERM-104-001` 的 Core-owned approval decision 落地后实施；
- `PERM-104-002` 完成前不实施或单独验收详情按钮修复；先证明相同不可升级阻塞不会生成第二个 permission input；
- `PERM-104-002` 通过定向/全量测试后执行派生项 `PERM-104-002-R1`，只补齐/验证 Core 详情投影和
  `View Details` 回归，不改变审批资格与阻塞收敛语义；
- 两个主项共享 approval schema、notification projection 或 DialogNotifier 时，不并行写同一文件，
  由 integration 先冻结公共接口再派发。

### Wave 3：终态、Failed 恢复与 Codex 双入口清理

- `FLOW-104-002` 先建立独立 terminal/report/notification/cleanup receipt，作为失败恢复的审计基础；
- `FLOW-104-003` 随后开放 Failed retry/handoff，禁止用清空失败记录或复制任务伪装恢复；
- `SESSION-104-001` 在 terminal/cleanup receipt 稳定后跑 CLI/Desktop 真机矩阵并实施窄修复；
- 三项必须共同覆盖“旧 session 已清理后 retry 不得恢复已删除 session”与“handoff 不删除派发端对话”。

### Wave 4：结构化流程与权威进度

- 先完成 `FLOW-104-001` 的 declared steps schema、旧任务双读和 dispatch preflight；
- 再完成 `FLOW-103-001`，progress receipt 只能引用已持久化 declared step ID；
- 两项不得放宽 callback 的未知/重复/缺失 step fail-closed 校验。

### Wave 5：集成与发布候选

- 运行三 Executor permission Approve/Deny/blocked、handoff multi-step、资源耗尽和 terminal failure
  真实 canary；
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

- 只清理由 AgentBC Executor 创建并从官方 early receipt 取得精确 ID 的临时会话；dispatcher
  conversation、用户会话、未登记会话和模糊名称匹配永不进入清理候选；
- 测试必须把同一官方 session ID 在 Codex CLI `resume` 入口与 Codex Desktop 恢复列表中的可见性
  关联起来；若上游不提供可验证关联，记录 `unsupported`/blocker，禁止扫描或改写私有数据库；
- `thread/delete` RPC 成功只是动作证据，不是双端清理完成证据；`thread/deleted` 在受支持的真实 stdio
  canary 中可能不发出，只作为绑定同一 UUID 的辅助事件；权威完成必须由新连接 `thread/read` 返回不存在，
  并由覆盖全部 source kind、分页和归档分区的 `thread/list` 证明 Desktop 恢复列表无该精确 UUID；
- 覆盖 completed、failed、cancelled、permission Deny/timeout、transport lost、Runner/Desktop 重启，
  并验证同一 receipt 重放幂等；
- 每个用例同时创建一条非 AgentBC 控制会话作为保留哨兵，证明清理没有扩大到 dispatcher 或用户会话；
- status/report/doctor 显示 cleanup capability、strategy、attempt、双入口验证状态与稳定 error code，
  不泄露私有会话路径、原始 prompt 或用户会话清单。

完成证据必须包含官方 ID 绑定、cleanup receipt、CLI 与 Desktop 清理前后快照、重启后复验和保留哨兵；
缺少任一项不得写 `succeeded`。

2026-08-27 收尾证据：`agent/codex@d18697f` 完成 cleanup receipt v2、官方 UUID 绑定、App Server
删除/新连接 read 验证、status/report/doctor 同源投影与 fail-closed 错误；`private/integration@8aa60a6`
完成初次集成，`9fce6b6` 补齐生产 Desktop `thread/list` 全 source kind、分页和 archived/non-archived
验证。受支持 Codex `0.147.0` 真实 canary 取得唯一 early receipt，目标在清理前为 `present`，清理后
CLI 与 Desktop 均为 `absent`，dispatcher 保留哨兵前后均为 `present`。真实 `0.147.0/0.150.1`
stdio 均可能不发 `thread/deleted`，因此该事件降为辅助证据，RPC 成功、新连接 `thread/read` 缺失与
Desktop `thread/list` 缺失共同构成权威完成条件。全量回归 `1461 tests` 通过；原 `HHWC-001` 无最终
callback 的 failed 状态不再作为实现状态来源。

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
- report/record/notification/cleanup 任一阶段失败时其余阶段仍可独立完成或重放；
- Update、Homebrew、session cleanup 和 `1.0.3A` 权限行为无回归；
- integration 与三个 agent 分支干净，Runner identity match，Doctor blocker 为 0；
- release candidate 的 GitHub/PyPI/bundle/bottle/manifest SHA 与 tag commit 可复验；
- 用户完成最终 go/no-go，之后才创建不可变 `v1.0.4A` tag 和 PyPI `1.0.4a1`。
