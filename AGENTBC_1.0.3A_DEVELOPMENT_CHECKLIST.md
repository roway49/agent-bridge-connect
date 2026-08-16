# AgentBC 1.0.3A 需求开发清单

> 制定日期：2026-08-11  
> 最近整理：2026-08-16
> 状态：开发进行中；Phase 0～1 已完成，Phase 2 进行中
> 目标版本：`v1.0.3A`  
> 来源基线：`1.0.2A` 开发截止代码 `b8af2f3a0a1f56814854e3f46056dd8ab9cf55d7`
> 计划开发起点：`private/integration@fc2f3f19d18d1c23890ee02a4ee9600c36456a60`
> 当前集成快照：`private/integration@ef8dd4f817e506cbb0ef39ebb00b4836981f25ea`
> 前置条件：`1.0.2A` 最终发布身份与双机 Gate 完成；Phase 0 只读契约盘点可提前进行
> 架构依据：`AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md`

## 0. 产品目标

`1.0.3A` 以权限治理、可信进度和简化分发为主线：AgentBC 提供一个统一、可解释、可审计
的权限 registry，但不改变已经验证体验良好的权限默认和继承逻辑。首次 setup 继续默认
`inherit`，已有配置继续保留；未显式覆盖的新根任务读取配置默认值，handoff 继续继承来源
Task 的冻结权限，同 Task retry/recover/input resume 继续使用原快照。

本版同时补齐权限阻塞到 `input_required` 的控制平面闭环。权限审批必须成为结构化运行
事件；不能继续依赖 Agent 在最终自然语言中主动填写 marker，也不能因为 Executor 没有
TTY 而等待 60/120 秒后静默拒绝。

版本边界已冻结：权限映射与审计、权限审批弹窗及其 Adapter/Core/Runner 协议、资源耗尽时
权威 step progress、交互式 update 和 Homebrew 由 `1.0.3A` 承担，不再回填已截止的
`1.0.2A`。“在首次工具审批前交付 session receipt”的早期 handshake 仍属于本版。完整
Executor 协议 fixture matrix 与模块局部重构延期到 `1.0.4A`，本版只保留支撑新增合同所需
的定向 fixture 和 characterization tests。

## 1. 当前问题与责任边界

### 1.1 权限来源需要统一解释

当前 `explicit task > handoff source > AgentBC config > legacy safe` 是已经投入使用的既定
优先级，本版必须保持；问题不在优先级本身，而在公共视图无法完整解释配置默认、handoff
继承、Task override、冻结快照和最终 Executor argv。Hermes 的 `inherit` 与 `safe` 目前都
不追加权限参数，命令层无法证明二者语义不同；若用户原生配置改变，任务行为可能漂移。
修改 AgentBC 全局默认不会改变已创建任务或从旧任务继承的 handoff，界面必须明确这一
作用域，不能让用户误以为“全局 full 已作用于现有 Task”。

### 1.2 弹窗后半段统一，阻塞事件入口不统一

Codex、Claude、Hermes 在拿到合法 `AGENTBC_FINAL_CALLBACK(final_state=input_required)`
之后，共用同一个 Core/Worker/Notification 流程：校验 blocked step、持久化
`agentbc.input`、suspend RunLease、调用 `notify_input_required()`、弹出 approve/deny、choice
或 message 对话框。

差异发生在此流程之前：

| Executor | safe 运行入口 | session receipt | 权限受阻时当前表现 |
| --- | --- | --- | --- |
| Codex | `--sandbox workspace-write`，JSONL 事件流 | 唯一 `thread.started.thread_id` | CLI 仍能结束 turn 并输出结构化 marker；已知 linked-worktree Git 元数据阻塞另由 `SAFE-001` 预检治理 |
| Claude | `--safe-mode --permission-mode acceptEdits`，print/JSON 输出 | fresh 由 AgentBC 预分配 UUID | 常规编辑无需交互；需要输入时仍能输出最终 marker，预算耗尽已有系统路由 |
| Hermes | safe 当前不加参数，沿用 Hermes 内部审批 | 只在 `-Q` 正常结束时从 stderr 读取 | terminal tool 在无 TTY 后台等待并超时拒绝；AgentBC 收不到结构化 approval event，且 receipt 可能在结束路径丢失，因此无法进入共用弹窗入口 |

### 1.3 从 1.0.2A 截止转入的权威待办

| ID | 优先级 | 问题 | 1.0.2A 证据 | 本版处理边界 |
| --- | --- | --- | --- | --- |
| `PERM-103-001`～`005` | P0 | 权限来源、映射、审批事件、早期 session 与迁移不统一 | 三 Executor 一次性 full canary 已通过，但依赖 Agent marker | 建立统一 registry 与原生控制平面 |
| `PERM-103-006` | P0 | 权限弹窗 reason 过长 | `E4S2-001` 超长 reason 曾阻断弹窗 | 极简首屏，详情独立展开 |
| `PERM-103-007` | P0 | Claude 临时工程写权限仍依赖 Prompt 约束 | `KXNX-001` 曾把交付物写入临时 cwd | 文件级 capability 与 Artifact root 分权 |
| `FLOW-103-001` | P0 | 系统资源耗尽覆盖 callback 时低估已完成 step | `BTCN-001` 实际完成 2 个 step，终态报告显示 `0/4` | Runner/Core 权威 progress receipt 与单调合并 |
| `UPD-103-001` | P1 | Alpha 缺少低心智负担的更新入口 | 1.0.2A 依赖手工 bundle 替换 | `agentbc update` 自动检查并以 `y/N` 确认升级；不提供 rollback 命令 |
| `PKG-103-001` | P1 | Homebrew 尚无正式 formula/cask Gate | 1.0.2A 只有 wheel/sdist/local bundle | 可验证安装、升级、卸载与迁移 |
| `PROTO-104-001` | 延期至 1.0.4A | Executor argv/help/output fixture 更新仍分散 | 1.0.2A 多次因真实 CLI 输出漂移补丁修复 | 与局部重构一起建立完整版本化 fixture matrix；1.0.3A 只补定向 fixture |
| `ARCH-104-001` | 延期至 1.0.4A | Service/Runner/CLI/Setup 继续过度集中 | 1.0.2A 收口时共享文件修改风险高 | 在完整 characterization/fixture 保护下进行局部重构 |
| `FLOW-104-001` | 延期至 1.0.4A | handoff 固定只声明一个 step，但自由文本可包含多段 Step 编号 | `XCKX-002`、`76MG-002` 实现与测试完成后因 callback 返回未声明的 Step 2～4 被 fail closed | 为 handoff 增加结构化多 steps 合同、预检和跨 Executor callback 一致性测试 |

### 1.4 当前开发进度（2026-08-16）

| ID | 状态 | 已完成证据 / 剩余边界 |
| --- | --- | --- |
| `PERM-103-001` | 已完成 | `79b5706`：统一 permission registry、配置事务、默认 `inherit` 和公共视图已合入 |
| `PERM-103-002` | 已完成 | `79b5706`：三 Executor 映射与 fail-closed capability probe 已合入；Hermes ACP 实际控制链计入 `003/004` |
| `PERM-103-003` | 进行中 | `87a7dc1`、`330168f`、`ce5e4e9`：Core、Codex、Claude 精确动作审批已完成；Hermes `session/request_permission` 待 Task 6 接入 |
| `PERM-103-004` | 进行中 | Codex、Claude early receipt 已完成；Hermes 仍需在 prompt 前从 ACP `session/new` 取得并持久化官方 session ID |
| `PERM-103-005` | 已完成 | `330168f`、`bb58e50`：严格 cutover、维护模式、历史只读投影和 update preflight 已合入 |
| `PERM-103-006` | 下一项 | Task 7：极简权限弹窗、脱敏详情展开和绝对超时 |
| `PERM-103-007` | 排队 | 待 Phase 2/3 公共接口稳定后实施 Claude 文件级 capability |
| `FLOW-103-001` | 排队 | Task 6/7 合入后的首个串行任务：Runner/Core 权威 progress receipt |
| `UPD-103-001` | 部分完成 | 已有 cutover preflight；自动 check、`y/N` 升级事务和失败保留旧版本尚未实现 |
| `PKG-103-001` | 未开始 | 等待 update 事务稳定后建设 Homebrew Gate |
| `PROTO-104-001` / `ARCH-104-001` / `FLOW-104-001` | 已延期 | 保持 `1.0.4A` 边界，本版不实现 |

Codex 控制面遗留任务 `HZQR-001` 因旧运行缺失官方 session receipt 于 2026-08-16 明确取消，
未伪造 completed callback；实现提交 `87a7dc1` 已在 integration 独立复验。收尾时另发现并合入
`3aa105c` 的 task-scoped control root fail-closed 修复，10 项定向测试通过；合入前 integration
全量基线为 1125 项通过。

## 2. P0：统一 Agent 权限设置（`PERM-103-001`）

- 新增统一用户入口：

  ```text
  agentbc permissions status
  agentbc permissions set inherit
  agentbc permissions set safe
  agentbc permissions set full
  ```

- setup 使用同一设置来源，显示“影响后续新派发任务”，不得暗示会修改 active、
  `input_required`、`needs_recovery` 或同 Task resume；
- `inherit|safe|full` 继续作为三个一等公开值；首次 setup 默认 `inherit`，已有配置值保留，
  升级不得把历史 `inherit` 静默改成 `safe` 或 `full`；
- 没有显式 task override 的新根任务从配置默认值生成权限快照；没有显式 override 的 handoff
  继续继承来源 Task 快照；retry、recover、input response 和同 Task resume 继续使用当前
  Task 已冻结快照；
- 显式 task override 只影响该新 Task，必须在 create/dispatch 输出、preflight、status、
  report 中同时显示 `configured_mode`、`inherited_mode`、`task_override`、`effective_mode`、
  `mapping` 和 `scope`，不得只显示模糊的 `selection_source`；
- 配置默认、handoff 来源、task override 与最终 Executor argv 必须通过同一个 permission
  registry 解释和映射，Setup、Service、Adapter、Runner 不得各自维护一份条件分支。

## 3. P0：Executor 权限能力映射（`PERM-103-002`）

目标映射必须由真实 CLI capability probe 固定，并由 Runner 对任务快照 fail closed 校验：

| 模式 | Codex | Claude | Hermes |
| --- | --- | --- | --- |
| inherit | 不追加 AgentBC 权限覆盖，保留原生用户/全局设置 | 不追加 AgentBC 权限覆盖，保留原生用户/全局设置 | 不追加 AgentBC 权限覆盖，保留原生用户/全局设置 |
| safe | workspace-write + 明确非交互审批语义 | safe-mode + `acceptEdits` | 必须新增受限、非交互、可审计的 safe approval 能力；未探测到时拒绝后台派发并给出可行动原因 |
| full | strongest documented bypass flag | `--dangerously-skip-permissions` | `--yolo` |

- `inherit` 是默认且稳定的透传模式；必须显示其作用域和最终 argv 证据，但不得把 Executor
  原生权限重新命名成 AgentBC `safe` 或 `full`；
- `safe` 不得读取 Executor 原生危险全局配置后静默升级；
- AgentBC 管理的 `full` 必须由配置默认、来源 Task 快照或显式 task override 产生，记录
  审计，不允许由 prompt、普通 input response、handoff 文案或原生配置注入；
- Executor 无法精确表达目标模式时返回 `permission_capability_unsupported`，不得把
  `safe` 近似成 `inherit`，也不得把 `safe` 近似成 `full`；
- Hermes `--safe-mode`、`--accept-hooks` 不能冒充 AgentBC safe，`--yolo` 只能映射 full。

## 4. P0：结构化审批与弹窗闭环（`PERM-103-003`）

新增与 Executor 无关的 approval event/receipt，建议最小字段：

```json
{
  "version": 1,
  "executor": "hermes",
  "session_id": "official-executor-session-id",
  "request_id": "stable-approval-id",
  "kind": "permission",
  "operation": "terminal",
  "summary": "Run the requested test command",
  "scope": "single_action"
}
```

- Adapter 捕获 approval event 后立即停止当前执行等待，Core 系统生成合法
  `input_required(type=permission)`，不要求 Agent 再输出 final marker；
- 进入等待前必须已有官方 executor session receipt，RunLease 转 `SUSPENDED`，并记录
  request/session/run 的绑定关系；
- `notify_input_required()` 继续作为唯一弹窗入口。Approve 只授权该 receipt 中的精确动作
  或明确的一次性作用域；Deny 返回同一 session 并让 Agent 处理拒绝结果；
- Approve 不得把 safe 永久改成 full。确需升级任务权限时，必须使用独立、明确标注风险的
  task permission override 流程并重新校验 Runner argv；
- 权限弹窗只提供 Approve / Deny 两种决策，不提供 Later 或文本输入；关闭和超时自动按
  Deny 处理，同时保留可审计的 decision source，不得静默丢失 session；
- Hermes 不再在无 TTY 后台自行等待 60/120 秒；Codex/Claude 也接入同一事件模型，避免
  继续依赖“模型通常会正确输出 marker”的偶然性。

## 5. P0：早期 session handshake（`PERM-103-004`）

- Codex、Claude、Hermes 都必须在可能发起工具审批之前交付官方 session receipt；
- Runner 只传输和校验 receipt，不扫描 Executor 数据库、日志、进程或“最近会话”；
- Hermes 需要上游机器可读 receipt 通道（事件、专用 FD/文件或启动握手），不得继续只在
  `-Q` 最终 cleanup 前后输出一次 stderr 行；
- receipt 缺失、重复、session/run 不匹配时进入 `needs_recovery`，禁止猜测 ID；
- approval 后只允许显式 ID resume，禁止 `--last`、`--continue` 等模糊恢复。

### 5.1 极简权限弹窗与完整原因展开（`PERM-103-006`）

- 权限弹窗首屏只显示由 Core 生成的最短单行摘要，例如“Hermes 需要一次性完整权限继续
  当前步骤”；默认界面不得直接铺开 Agent 生成的长 reason、完整命令、路径或原始输出；
- 保留 Approve / Deny 作为仅有的两种决策动作；新增“查看完整原因”交互按钮只负责展开或
  收起详情，不是第三种响应，不得发放授权、改变 input 状态、重置倒计时或绕过超时策略；
- approval event 分离 `reason_summary` 与 `reason_detail`。摘要由 Core 基于结构化
  Executor/operation/scope 生成；详情在持久化前统一脱敏、去控制字符并设置独立长度上限，
  禁止包含 token、secret、私有数据库路径、session 内容、raw output 或未经处理的 argv；
- `1.0.2A` 的兼容 marker 仍只持久化最多 240 字符的截断 reason，不承诺保留被截断尾部；
  `1.0.3A` 的完整详情必须来自新的结构化 approval event，不能从历史日志、Executor 私有
  session 存储或被截断的 v1 marker 反向恢复；
- text/JSON notification、状态页和报告默认只使用短摘要；只有本机显式展开动作读取脱敏后的
  bounded detail，公共 projection 不新增 raw reason 字段。

### 5.2 Claude 临时工程文件级最小写权限（`PERM-103-007`）

- 不再把“不要向临时 cwd 写交付物”仅作为 Prompt 约束；先对受支持的 Claude 版本执行
  fresh、resume、input wait、资源耗尽和正常终态探查，冻结临时工程真正必需的相对文件、
  目录、类型、创建时机和访问模式 fixture；探查只使用隔离目录和测试 session，不扫描用户
  原生 Claude 工程或私有 session 数据库；
- permission registry 为每个已探测版本登记临时工程写入 capability。Runner 只允许匹配
  fixture 的必要路径写入，其他位于 `<TASK-ID>/claude` 下的新建、覆盖、重命名和删除默认
  fail closed；未知 Claude 版本或未识别必要路径不得回退为整个临时目录可写；
- Artifact root / customer project 作为独立 deliverable capability 授权。无 customer path 时
  Core 自动创建 managed Artifact root，Claude 对产物的读写必须落在该根；临时工程与产物
  根即使物理上同属 task artifact tree，也不得共享同一宽泛写权限；
- 优先采用 OS/Executor 能实际阻止写入的路径策略；事后 diff/扫描只作为审计和 recovery
  证据，不能冒充权限阻断。无法表达文件级 allowlist 的平台必须明确 capability unsupported，
  不得继续仅靠模型遵守提示词；
- matcher 必须使用 canonical containment、拒绝 symlink/hardlink/path traversal/case alias，
  并由 Runner 对 task snapshot、Claude 版本和真实 cwd 再校验；不得授权 AgentBC record、
  report、其他 Task、用户配置或 Claude 私有全局目录；
- cleanup 只 purge 精确绑定的 Claude project；临时工程出现非必要文件时停止删除并进入
  `needs_recovery`，正常 Artifact root 非空必须保留且不算 cleanup failure。

## 6. 兼容与迁移（`PERM-103-005`）

- 历史任务继续按持久化 `agentbc.permission` 运行，不做原地提权或批量改写；
- 旧顶层 `permission_mode` 双读迁移到统一 permission 配置事务，首次实际修改时再规范化；
- 老任务缺失权限仍按 legacy safe fail closed；
- 保持既有 handoff 语义：未显式覆盖时继续读取来源任务快照，不因升级或全局默认变化重解释
  历史任务；新根任务才在未显式覆盖时读取当前配置默认；
- User Guide、三 Executor skills、setup/help/doctor/status/report 同步统一术语。

## 7. P0：资源耗尽与终态进度权威化（`FLOW-103-001`）

`BTCN-001` 证明原生 Hermes 耗尽必须高于 Agent 普通/choice callback，否则会绕过资源状态
机；同时也证明“整体丢弃 callback”会把真实完成进度降为 `0/4`。本版不能通过恢复 choice
优先级解决，而应增加 Executor 无关的 progress receipt：

- progress receipt 至少绑定 `task_id`、`executor_run_id`、官方 `session_id`、step ID、
  单调状态、证据来源和序号；由 Runner/Core 补齐已知身份，Agent 不重抄内部路径；
- `done` 只能由运行期间已经落盘且校验通过的 receipt 单调推进；资源耗尽、permission wait、
  transport recovery 和 terminal callback 只合并，不得把 `done` 回退为 pending/blocked；
- receipt 不能仅由自然语言、summary、choice label 或耗尽后的 final callback 推断；缺失证据
  时保持旧状态，不伪造完成；
- 当前 run 的 terminal callback 仍负责最终 flow 状态；progress receipt 只保存已完成事实，
  不能单独把任务标记 completed；
- status/report/notification 使用同一公共 projection，显示“已确认进度”和证据质量，不公开
  raw output、prompt、session 内容或私有路径；
- Codex、Claude、Hermes 和 fake Executor 覆盖耗尽前部分完成、两次耗尽、权限等待、恢复、
  重放、乱序、重复 receipt、旧任务无 receipt 和任务/session 漂移。

## 8. P1：更新、分发与延期边界

### 8.1 交互式自更新（`UPD-103-001`）

- 用户只需运行 `agentbc update`；命令自动读取签名或哈希可验证的版本清单并检查更新；
- 已是最新版本时显示 current/latest/channel 后零写入退出；检测到新版本时显示当前版本、
  目标版本、来源和摘要，然后询问 `Upgrade? [y/N]`，只有明确 `y`/`yes` 才执行升级；
- Alpha 不提供公开 `check|apply|rollback` 子命令，也不提供用户主动回滚命令；升级失败必须
  保持或恢复本次操作前仍可启动的版本，不能把内部失败恢复暴露为公共 rollback 功能；
- 升级前验证当前 package/Runner identity，更新 CLI、Runner 和三平台 Skill 后再原子切换；
  保留配置、任务、报告和 customer project，不运行 setup 覆盖用户选择；Runner 必须在
  新旧 identity 间明确停止/启动，不允许混装；
- update、doctor 和安装器共用 build identity、版本和修复建议，不新增第二套版本判断。

### 8.2 Homebrew（`PKG-103-001`）

- 产出可审阅 formula/cask，固定 Python、wheel/sdist SHA-256、Runner service 和卸载边界；
- 覆盖 clean install、upgrade、uninstall、PATH 冲突和已有 PyPI/local-alpha 迁移；
- 不覆盖用户配置、record、report、artifact 或 Executor Skill 修改；受管 Skill 漂移先报告，
  不静默覆盖。

### 8.3 协议 fixtures（延期到 `1.0.4A / PROTO-104-001`）

- `1.0.3A` 不建设覆盖三 Executor 全部 version/help/argv/output/session/approval/resource 的完整
  fixture matrix；只为本版新增 permission/session/progress 合同补充最小定向 fixture；
- 完整版本化 matrix、未知版本组合的系统化 fail-closed probe 和 fixture 更新流程统一移入
  `1.0.4A`，与局部重构一并实施。

### 8.4 模块局部重构（延期到 `1.0.4A / ARCH-104-001`）

- `1.0.3A` 只允许为实现新合同新增窄模块，不安排 Service/Runner/CLI/Setup 的主动拆分；
- `1.0.4A` 在完整协议 fixture matrix 和 characterization tests 保护下进行局部重构，优先拆出
  permission registry、approval transport、Doctor collectors、Runner IPC handlers 和 update
  service，同时保留公共入口与 import compatibility；
- 重构提交不得混入状态机、schema、CLI 文案或权限语义变化。

### 8.5 handoff 结构化多 steps（延期到 `1.0.4A / FLOW-104-001`）

- `1.0.3A` 保持现有 handoff 单 step 合同，不修改 Core、CLI、prompt 或 callback 校验；handoff
  message 只是该唯一 step 的说明，控制端不得在其中使用会被误解为正式合同的 `Step 2+`
  编号，Executor callback 只能返回任务包实际声明的 step ID；
- `1.0.4A` 为 `agentbc task handoff` 增加结构化 steps 输入，复用根任务的规范
  `steps[].description` schema，同时继续继承 chain、PathPlan、artifact root、权限快照、资源和
  session 策略；自由文本 message 与结构化 steps 的组合及优先级必须唯一、可解释；
- create/dispatch preflight 必须校验 declared step IDs、重复/缺失编号及自由文本中的歧义编号，
  在启动 Executor 前给出稳定错误，不能等最终 callback 才发现合同不一致；
- Prompt 及 `AGENTBC_FINAL_CALLBACK` 示例只能从持久化 task packet 的 declared steps 生成，
  Claude、Hermes、Codex 与 fake Executor 均覆盖单 step、多 steps、嵌套编号、重试、恢复和
  handoff 链；
- 继续保留当前严格 fail-closed 校验：callback 出现未知、重复、缺失或非完成 step 时不得
  静默归并或猜测映射。

## 9. 实施阶段与依赖

1. **Phase 0：现状审计与契约冻结**——从 `fc2f3f1` 建基线；冻结 permission registry、
   approval event、early session、progress receipt、legacy 双读和本版最小定向 fixture；
2. **Phase 1：统一设置与 registry**——配置事务、setup、`agentbc permissions`、三 Executor
   capability mapping、公共视图与迁移；
3. **Phase 2：Runner/Adapter 控制平面**——早期 session handshake、approval transport、
   canonical argv、Runner fail-closed 校验；
4. **Phase 3：Core input 与 progress**——精确动作 Approve/Deny、关闭/超时自动 Deny、同 session resume、
   `FLOW-103-001` 单调进度合并、极简弹窗和报告投影；
5. **Phase 4：权限细分与 Claude 临时工程**——`PERM-103-007`、路径攻击矩阵、三 Executor
   safe/full 和旧任务兼容 canary；
6. **Phase 5：交互式 update 与 Homebrew**——自动 check、`y/N` 升级、失败保持旧版本、安装/
   升级/卸载/迁移；不实现 rollback 命令、完整 fixture matrix 或主动模块拆分；
7. **Phase 6：集成与发布**——安装升级、Python/双机、三 Executor、失败注入、发布身份。

### 9.1 `1.0.3A` 开始前的人工过渡规则

- 默认继续使用现有 `inherit` 逻辑；新根任务未指定 override 时使用配置默认，handoff 未指定
  override 时继承来源 Task，控制端不得为了“更明确”而强制改写为 `safe` 或 `full`；
- 只有用户明确要求当前 Task 覆盖时才传 `--permission-mode inherit|safe|full`；
- `full` 只在明确提示风险并取得本次派发授权后使用；不能把 Hermes `--yolo` 当作默认；
- retry/recover/resume 保持任务已冻结权限，不借普通 input 改权；
- 该规则是操作门禁，不要求 `1.0.2A` 新增命令、弹窗或协议字段；统一设置与自动弹窗在
  `1.0.3A` Phase 1～3 替代人工流程。

### 9.2 目标日程

| 日期 | 阶段 | 当前状态 | 退出门禁 |
| --- | --- | --- | --- |
| 2026-08-17～08-19 | Phase 0 | 已提前完成 | `fc2f3f1` 基线、权限/approval/session/progress 合同与最小定向 fixture 冻结 |
| 2026-08-20～08-28 | Phase 1 | 已提前完成 | `inherit|safe|full` registry、配置事务、既有继承逻辑、公共视图和迁移通过 |
| 2026-08-29～09-06 | Phase 2 | 进行中 | 三 Executor early session 与 approval transport；Hermes unsupported 路径可行动且 fail closed |
| 2026-09-07～09-13 | Phase 3 | 部分基础已完成 | Approve/Deny、同 session resume、极简弹窗与单调 progress receipt 通过 |
| 2026-09-14～09-20 | Phase 4 | 未开始 | Claude 文件级 capability、路径攻击矩阵、旧任务兼容与三 Executor canary 通过 |
| 2026-09-21～09-24 | Phase 5 | update preflight 已完成 | `agentbc update` 自动 check/`y/N` 升级及 Homebrew 安装、升级、卸载、迁移通过 |
| 2026-09-25～09-27 | Phase 6 | 未开始 | 全量、Python/双机、真实 Executor、失败注入与发布身份 Gate 通过 |

目标发布窗口为 `2026-09-27`；若 Hermes early approval/session capability 或 Claude 文件级
capability 无法由当前上游 CLI 精确表达，本版按既定 fail-closed 合同交付，不以危险近似
实现换取日期。

## 10. 验收门禁

- 首次 setup 继续默认 `inherit`，已有配置值升级后保持；修改配置默认后，后续新根任务展示并
  执行正确映射，已创建 Task 不变化；
- 同 Task resume 不因全局设置变化而漂移，未显式覆盖的新 handoff iteration 继续继承来源
  Task 快照；
- Hermes safe 无可用 headless approval 能力时在派发前明确拒绝，不再运行二十分钟后超时；
- 三 Executor 权限受阻均进入同一 `input_required` 弹窗，任务保留官方 session ID；
- approve 只执行精确授权动作，deny 有明确结果；权限弹窗无 Later/文本输入，关闭和超时
  自动 Deny 并记录来源；
- 缺失 receipt、伪造 permission argv、native dangerous config、safe→full 注入全部 fail closed；
- 资源/权限等待前已经确认的 step progress 不回退、不重复计数，旧任务无 receipt 时不伪造；
- `agentbc update` 自动 check；无更新零写入，有更新以 `y/N` 确认，升级成功后 CLI、Runner 与
  三平台 Skill identity 一致，失败时旧版本仍可启动；配置、record、report、artifact 和
  customer project 均保持；Homebrew 与 PyPI/local bundle 迁移有可复现证据；
- 单测、Runner 集成、真实 CLI canary、Ruff、compileall、`git diff --check` 和发布身份检查通过。

## 11. 明确不做

- 不让 Agent 自行决定或填写最终权限；
- 不通过扫描私有 session 数据补 receipt；
- 不把 Hermes `--yolo` 当作 safe 的临时修复；
- 不允许普通 input 文本修改权限快照；
- 不从自然语言或被系统覆盖的 callback 猜测已完成 step；
- Alpha 不提供 `agentbc update rollback` 或其他用户主动回滚命令；
- 不在 `1.0.3A` 建设完整 Executor fixture matrix 或主动拆分共享模块；二者随局部重构进入
  `1.0.4A`；
- 不在 `1.0.3A` 改造 handoff 单 step 合同；结构化多 steps、预检与 Executor 一致性测试进入
  `1.0.4A / FLOW-104-001`；
- 不用 update 静默覆盖用户 Skill 或配置；
- 不在 `1.0.2A` 临时回填这一协议级改造。
