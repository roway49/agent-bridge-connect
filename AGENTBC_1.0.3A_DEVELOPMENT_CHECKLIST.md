# AgentBC 1.0.3A 需求开发清单

> 制定日期：2026-08-11  
> 最近整理：2026-08-23
> 状态：权限、update 与 Homebrew 代码已收口；Update 缺陷修复已合入，Homebrew RC 驱动已加固但双机环境 Gate 尚未通过，Session P1 继续冻结
> 目标版本：`v1.0.3A`  
> 来源基线：`1.0.2A` 开发截止代码 `b8af2f3a0a1f56814854e3f46056dd8ab9cf55d7`
> 计划开发起点：`private/integration@fc2f3f19d18d1c23890ee02a4ee9600c36456a60`
> `PERM-103-007` 实现快照：`private/integration@0cfb492`
> `UPD-103-001` 实现快照：`private/integration@f7cbfbb`
> `PKG-103-001` RC 驱动快照：`private/integration@bf3c1e6`
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

版本边界已冻结：权限映射与审计、权限审批弹窗及其 Adapter/Core/Runner 协议、交互式
update 和 Homebrew 由 `1.0.3A` 承担，不再回填已截止的
`1.0.2A`。“在首次工具审批前交付 session receipt”的早期 handshake 仍属于本版。完整
Executor 协议 fixture matrix、模块局部重构、资源耗尽权威 progress receipt 和审批机械判定
延期到 `1.0.4A`，本版只保留支撑已交付合同所需的定向 fixture 和 characterization tests。

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
| `PERM-103-008` | P1，条件性补全 | permission/session 合同失败被折叠为同一错误，难以区分 mode、receipt、run、lease、chain 与 resume 原因 | `R8KP-004` 的 `inherit` mode gate 与后续缺失 session resume 使用了相近的 session unavailable 表述 | 拆分稳定错误类型、详情与回归断言；不改变现有权限语义 |
| `PERM-103-009` | P1，条件性补全 | Codex single-action 控制代码尚未成为生产默认路径，CLI transport 仍会回退到 continuation/full 模型 | `R8KP-004` 在 linked worktree commit 阻塞后无法形成可恢复的原生单动作审批 | 评估并打通 Codex app-server single-action 生产链；未完成时继续使用 1.0.3A 一次性 full 兜底 |
| `FLOW-103-001` | 延期至 1.0.4A | 系统资源耗尽覆盖 callback 时低估已完成 step | `BTCN-001` 实际完成 2 个 step，终态报告显示 `0/4` | 与结构化多 steps 和局部重构一并实现 Runner/Core 权威 progress receipt；1.0.3A 不修改状态机 |
| `UPD-103-001` | P1 | Alpha 缺少低心智负担的更新入口 | 1.0.2A 依赖手工 bundle 替换 | `agentbc update` 自动检查并以 `y/N` 确认升级；不提供 rollback 命令 |
| `PKG-103-001` | P1 | Homebrew 尚无正式 formula/cask Gate | 1.0.2A 只有 wheel/sdist/local bundle | 可验证安装、升级、卸载与迁移 |
| `SESSION-103-002` | P1，待开发 | E2E canary teardown 只结束进程和删除临时目录，没有关闭测试创建的官方 Executor 会话 | `WT3X-001` 的 raw Codex app-server canary 调用 `thread/start` 后仅 `terminate()` 并删除 `/tmp`，相关 Codex 对话继续保留 | 为真实 E2E helper 建立 receipt 驱动的 `try/finally` teardown；成功、拒绝、超时和进程异常均精确清理本次创建的会话 |
| `SESSION-103-003` | P1，待开发 | AgentBC 只跟踪主 Executor 的单一 `agentbc.session`，由 Executor 派生的子 Executor 对话不在 cleanup ledger | `WT3X-001` 的 Claude 主会话已按 `retain=false` 清理，但其派生的 Codex threads 没有被登记或清理 | 增加 task/run scoped auxiliary session receipt 与 cleanup 状态；任务收尾覆盖主会话及全部已登记派生会话，禁止扫描私有会话库猜测 ID |
| `PROTO-104-001` | 延期至 1.0.4A | Executor argv/help/output fixture 更新仍分散 | 1.0.2A 多次因真实 CLI 输出漂移补丁修复 | 与局部重构一起建立完整版本化 fixture matrix；1.0.3A 只补定向 fixture |
| `ARCH-104-001` | 延期至 1.0.4A | Service/Runner/CLI/Setup 继续过度集中 | 1.0.2A 收口时共享文件修改风险高 | 在完整 characterization/fixture 保护下进行局部重构 |
| `FLOW-104-001` | 延期至 1.0.4A | handoff 固定只声明一个 step，但自由文本可包含多段 Step 编号 | `XCKX-002`、`76MG-002` 实现与测试完成后因 callback 返回未声明的 Step 2～4 被 fail closed | 为 handoff 增加结构化多 steps 合同、预检和跨 Executor callback 一致性测试 |
| `PERM-104-001` | 延期至 1.0.4A | native single-action Deny 后，Agent 仍可能依据通用 Prompt 请求兼容 full fallback，审批渠道选择依赖语言模型遵循互相冲突的文字规则 | `YYN5-001` 用户依次选择 Approve、Deny、Approve；前两次 native request 在同 run/session 中正确记录，但 Deny 后 Agent 又输出 full fallback，触发第二 worker 才完成 | 审批资格、通知类型、Deny 后状态和 fallback eligibility 全部由 Core 根据可信 transport event 与持久化控制面历史机械判定；Prompt/Agent callback 不得成为审批判断依据 |

### 1.4 当前开发进度（2026-08-22）

| ID | 状态 | 已完成证据 / 剩余边界 |
| --- | --- | --- |
| `PERM-103-001` | 已完成 | `79b5706`：统一 permission registry、配置事务、默认 `inherit` 和公共视图已合入 |
| `PERM-103-002` | 已完成 | `79b5706`：三 Executor 映射与 fail-closed capability probe 已合入；Hermes ACP 实际控制链计入 `003/004` |
| `PERM-103-003` | 已完成 | `87a7dc1`、`330168f`、`ce5e4e9`、`791e3b8`、`a64f5a5`、`2b1bcbd`、`247c45b`：Core 与三 Executor 精确动作审批均已接入；Codex app-server production registry、Runner gate、官方 early receipt 与同进程单动作响应已合入 |
| `PERM-103-004` | 已完成 | Codex、Claude early receipt 已完成；`791e3b8`、`a64f5a5` 已合入 Hermes ACP `session/new` prompt 前官方 session receipt、显式 resume 与 fail-closed 校验；合入后 120 项 Hermes/registry/lifecycle 定向测试通过 |
| `PERM-103-005` | 已完成 | `330168f`、`bb58e50`：严格 cutover、维护模式、历史只读投影和 update preflight 已合入 |
| `PERM-103-006` | 已完成 | `ce4f366`～`c09fa18`、`1dbe97e`、`a383bcf`：极简权限弹窗、脱敏详情、绝对超时与普通说明语义保持已合入；74 项重点回归、1168 项全量和隔离 wheel smoke 通过 |
| `PERM-103-007` | 已完成 | `0cfb492`：Claude 2.1.216～2.1.x 版本/平台/help gate、任务级 `claude.ephemeral_project_isolation.v1`、Artifact-only `--add-dir`、内建 Edit deny、OS sandbox `allowWrite/denyWrite`、禁用 unsandboxed retry 与 Runner 精确 argv/settings 重建校验已合入；本机 2.1.233 probe、1268 项全量、Ruff、compileall 与 diff check 通过 |
| `PERM-103-008` / `PERM-103-009` | 已完成 | `a993e94`、`4ef12eb`：permission/session 兼容总错误下新增 15 类稳定原因与脱敏诊断；`2b1bcbd`、`247c45b`：Codex app-server single-action 生产链已合入；既有一次性 full continuation 继续作为兼容兜底 |
| `FLOW-103-001` | 已延期 | 与 `FLOW-104-001` 的结构化多 steps、`PROTO-104-001` fixtures 和 `ARCH-104-001` 局部重构一起进入 1.0.4A |
| `UPD-103-001` | 修复已合入；等待真实 RC 复验 | `2656cef` 修复跨版本受管 Skill 识别，`b285a35` 补齐 CLI/Runner/Skill 事务回滚与 identity 校验，`eeb9c00` 增加隔离两版本 RC 驱动；合入后 Update/Homebrew 联合定向测试 88 项通过。真实成功升级与故障包恢复尚未按新实现重跑，不得标记 RC Gate 完成 |
| `PKG-103-001` | RC 驱动已完成；双机环境 Gate 阻断 | `bf3c1e6` 增加默认只读、双重执行门禁的 install/upgrade/uninstall/service 驱动，冻结 Formula/Tap/service/trust/PATH/用户数据/RunLease 状态，禁用自动 update/cleanup/autoremove，并分别校验 CA SKI/AKI 与服务端 SAN/SKI/AKI。2026-08-23 Mac mini 只读复验确认 HTTPS curl/Python 与证书门禁通过，但被 `brew doctor` 不健康和缺失 `python@3.13` 阻断；未执行 Tap/Cellar 写入。Intel Xcode/CLT 阻断仍未解除 |
| `SESSION-103-002` | P1 待开发；等待 Update/Homebrew Gate | 修复 E2E helper 的 teardown 完整性；必须使用创建时捕获的官方 session receipt 精确删除，不得以结束进程、删除 canary root 或退出码代替 cleanup 成功 |
| `SESSION-103-003` | P1 待开发；等待 Update/Homebrew Gate | 补齐派生 Executor 对话的登记、终态清理、失败重试、report/doctor blocker 与脱敏 cleanup receipt；完成前不再运行会产生持久化子对话的真实权限 E2E |
| `REL-103-CANDIDATE` | 候选包与双机安装 smoke 已完成 | `89dc0b0` 已形成 `1.0.3a1` 内部候选；Mac mini 110 项权限定向与 1229 项全量、Intel MacBook 140 项权限定向与 1229 项全量通过；隔离 wheel smoke `F47F-001`、MacBook 安装态 `X977-001`、Mac mini 安装态 `CQBA-001` 通过，双机 CLI/Runner/三平台 Skill identity 一致；尚未关闭真实三 Executor 权限审批 canary 与最终发布 Gate |
| `FLOW-103-001` / `PROTO-104-001` / `ARCH-104-001` / `FLOW-104-001` / `PERM-104-001` | 已延期 | 保持 `1.0.4A` 边界，本版不实现；1.0.3A 不再扩大功能范围，只执行 update/Homebrew 与三 Executor 的 RC 验收 |

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

## 7. 延期至 1.0.4A：资源耗尽与终态进度权威化（`FLOW-103-001`）

`BTCN-001` 证明原生 Hermes 耗尽必须高于 Agent 普通/choice callback，否则会绕过资源状态
机；同时也证明“整体丢弃 callback”会把真实完成进度降为 `0/4`。该问题不在 1.0.3A
继续修改；以下合同冻结后整体转入 1.0.4A，并与结构化多 steps 和局部重构一并实施：

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

### 8.0 条件性权限补全（`PERM-103-008` / `PERM-103-009`）

- `PERM-103-008` 已在兼容总错误下拆分 15 类 permission/session 稳定原因与脱敏诊断，不修改
  permission mode、grant、resume 或审批决策语义；mode 不支持、receipt 缺失/不匹配、
  run/session 不匹配、RunLease 状态、stale chain head 和 resume session 缺失均有独立断言；
- `PERM-103-009` 已把 Codex app-server transport、首次审批前官方 session receipt、同进程
  request/response 与 Runner production registry 一起接入；支持的 Codex 协议面优先使用
  `single_action`，不再把已合入实现描述为实验性半链；
- 已验证的一次性 `safe -> full` continuation 继续作为不支持原生 single-action transport 时的
  兼容兜底。`full` grant 仍必须绑定当前 Task、官方 session 和下一次 run，一次消费，失败、
  recovery、reassign、handoff 或终态时撤销；不得因 fallback 可用而把 single-action canary
  判定为通过；
- 发布 go/no-go 仍要求默认 `inherit`/handoff 继承无漂移、定向与全量回归通过、隔离升级包和
  Runner identity 可复验，并完成下方三 Executor 真实审批 canary；
- 不新增 linked-worktree Git 预检或专用提交阶段。权限审批应覆盖真实运行中出现的动作阻塞，
  不通过穷举路径和命令场景扩大前置流程。

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
- Homebrew-owned Cellar/opt link 不进入 AgentBC 自更新事务；`agentbc update` 只提示
  `brew upgrade agentbc`，避免破坏 Homebrew receipt 与 service 所有权。

#### 8.2 RC Gate 复验记录（2026-08-22）

- `integration@52eb7fc` 的 75 项 Update/Homebrew 定向测试、1280 项全量、Ruff、compileall、
  Shell/Ruby 语法、Twine、manifest/hash、隔离 wheel smoke 均通过；测试资产没有进入正式发布；
- Update 真实 E2E 的只读/拒绝/校验失败场景通过，但成功升级和故障恢复均被同一 Skill
  migration 缺陷阻断：旧版受管 Skill 在新版本 `setup --update` 中被归类为 `modified`，因此不被
  刷新；post-update identity 正确 fail closed，但当前恢复声明不成立；
- Homebrew ARM64 install/upgrade/test/PATH/update guidance 通过，且 Mac mini 原 Runner 未停止；
  Intel 安装在写入 AgentBC Cellar 前被本机 Xcode/CLT 前置条件阻断，`brew services` 未测试；
- 本轮结论为 Phase 5 `blocked`，不是 RC accepted。先最小修复 Skill 跨版本刷新与失败回滚，更新
  Intel 构建工具后重跑双机 Gate；`SESSION-103-002/003` 与重新出包继续保持冻结。

#### 8.2 RC 驱动收尾（2026-08-23）

- Update 三项修复已按 Hermes → Codex → Claude 顺序合入 `private/integration@f7cbfbb`；联合
  Update/Homebrew 定向测试 88 项、Ruff、compile 与 `git diff --check` 通过，真实慢速 E2E 仍为
  显式 opt-in；
- `bf3c1e6` 提供 Homebrew RC 驱动：默认只读，真实执行同时要求 `--execute` 与
  `AGENTBC_HOMEBREW_RC_RUN=1`；仅对精确 RC Formula 使用临时 trust，不关闭 Tap Trust，结束后
  必须恢复 Formula、Tap、service、trust、PATH、用户数据与稳定 RunLease 快照；
- Mac mini 临时 HTTPS feed 的 curl 与 Python 双探测、CA/server 扩展校验均通过。预检在任何
  Tap/install 之前因 `brew doctor` 非零与缺失 `python@3.13` 正确停止；证据为
  `/private/tmp/agentbc-homebrew-preflight-20260823.json`。不得为通过 Gate 自动执行 `sudo chown`、
  更新/删除 CLT、信任无关 Tap 或安装依赖；主机环境由用户确认修复后再执行破坏性 RC；
- 当前结论仍为 Phase 5 `blocked`。Mac mini 与 Intel MacBook 的新驱动实跑、Update 真实成功/回滚
  复验全部通过后，才可解冻 `SESSION-103-002/003`。

### 8.2.1 E2E teardown 与派生会话清理（`SESSION-103-002` / `SESSION-103-003`）

- 开发顺序固定为：先完成 `UPD-103-001` 真实新旧包升级/故障注入和 `PKG-103-001` 双架构
  install/upgrade/uninstall/services/PATH/迁移 Gate，再补本节；两项 session P1 通过后才恢复
  会创建真实 Executor 对话的权限 E2E 与最终发布 Gate；
- `SESSION-103-002` 约束 AgentBC 自带 E2E/canary helper：每次创建 Executor 会话时立即捕获
  官方 receipt，使用 `try/finally` 在成功、Deny、timeout、transport lost、进程异常和测试中断
  路径执行精确 teardown，并等待官方删除结果；`process.terminate()`、删除 `/tmp` 或 customer
  root、命令退出 0 均不能作为会话已清理证据；
- `SESSION-103-003` 为任务增加 task/run scoped auxiliary session ledger。每条记录至少绑定
  owner task、run、parent executor/session、child executor、官方 session ID、purpose、retain
  快照和 cleanup state/attempts/receipt；`thread/start` / `session/new` 成功后必须先原子登记，
  才能继续派生执行；
- 终态 cleanup coordinator 同时处理主 `agentbc.session` 与全部 auxiliary sessions。任一
  `retain=false` 派生会话未登记、缺失官方 receipt、清理失败或仍处于不确定状态时，report 和
  doctor 必须产生稳定 blocker，不得把主会话清理成功投影为任务 cleanup 完成；
- `input_required`、retry、recover 与同 session continuation 期间保留仍在使用的派生会话；
  只有进入符合条件的终态后清理。显式 `retain=true` 必须冻结到每条 receipt，不能由子进程或
  E2E helper 自行改变；
- 禁止扫描 Codex/Claude/Hermes 私有数据库、目录或“最近会话”推断 ID；只能清理由当前任务
  创建并通过官方 transport 捕获、已经登记的精确 receipt；公共 status/report/log 仅显示脱敏
  session 引用和 cleanup 结果；
- 回归覆盖 raw app-server canary、嵌套 Executor、成功、Approve/Deny、超时、崩溃、Runner
  重启、cleanup 重试和 retain=true。验收要求没有新增遗留对话、每条 cleanup receipt 可审计、
  重放删除幂等，且 dispatcher conversation 永远不进入清理集合。

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

### 8.6 审批通知机械判定（延期到 `1.0.4A / PERM-104-001`）

- `1.0.3A` 不继续优化该路径，保留现有 native single-action 与一次性 full compatibility
  fallback；`YYN5-001` 的 Git 产物、弹窗信息和三次用户选择均有有效证据，但因第二次 native
  Deny 后又进入 full fallback 并启动第二 worker，只能证明用户面符合预期，不能作为纯原生
  single-action 链通过证据；
- `1.0.4A` 不再让 Prompt、Agent 自述、普通 stderr、退出码或 callback 文案判断是否需要审批、
  选择 `single_action`/`full`、生成 fallback 或改变 Deny 后状态。Agent 只描述当前动作和执行
  结果，不拥有审批渠道选择权；
- Core 只接受受支持 Adapter 的可信结构化 permission-block event，并机械核对 task、executor
  run、官方 session、request ID、fingerprint、operation、blocked declared step、chain head 和
  RunLease 后生成 `agentbc.approval`、`input_required` 与通知投影；UI/CLI 仅响应该持久化状态，
  不从 Prompt 重新解释原因；
- native `decline` 必须成为同一 request/fingerprint 的单调终态：精确动作不得执行，也不得把
  同一阻塞自动转换为 full fallback。后续是否允许新动作或 compatibility fallback，必须由 Core
  预先持久化的 capability/policy matrix 明确允许，并产生新的绑定事实；
- full fallback eligibility 仅在目标 transport 没有受支持的 native single-action 能力、且任务
  冻结策略明确允许 compatibility path 时由 Core 生成。Agent 输出 `requested_permission=full`
  只能作为不可信请求输入，不能直接触发弹窗或 permission grant；
- 回归必须覆盖 Approve→Deny→Approve、重复/乱序/跨 request 响应、native Deny 后伪造 full
  callback、transport lost、session/run/fingerprint 漂移和 UI/CLI 双入口，要求通知次数与控制面
  决策一一对应、Deny 后零执行、无第二 worker、无权限模式漂移。

## 9. 实施阶段与依赖

1. **Phase 0：现状审计与契约冻结**——从 `fc2f3f1` 建基线；冻结 permission registry、
   approval event、early session、progress receipt、legacy 双读和本版最小定向 fixture；
2. **Phase 1：统一设置与 registry**——配置事务、setup、`agentbc permissions`、三 Executor
   capability mapping、公共视图与迁移；
3. **Phase 2：Runner/Adapter 控制平面**——早期 session handshake、approval transport、
   canonical argv、Runner fail-closed 校验；
4. **Phase 3：Core input**——精确动作 Approve/Deny、关闭/超时自动 Deny、同 session resume、
   极简弹窗和报告投影；`FLOW-103-001` 不再属于本版 Phase 3；
5. **Phase 4：权限细分与 Claude 临时工程**——`PERM-103-007` 已收口；三 Executor 真实
   canary 继续作为发布验收，不再扩大权限代码范围；
6. **Phase 5：交互式 update 与 Homebrew**——Update 修复已合入 `f7cbfbb`，Homebrew RC 驱动
   已合入 `bf3c1e6`；88 项联合定向测试通过，但 2026-08-23 Mac mini 被 Homebrew ownership/
   Doctor 与预装依赖门禁阻断，Intel 仍被 Xcode/CLT 阻断，故双机真实 RC 尚未通过；不实现
   rollback 命令、完整 fixture matrix 或主动模块拆分；
7. **Phase 5.5：E2E/session P1 补完**——仅在 Phase 5 的 update 与 Homebrew RC Gate 全部通过后
   开始 `SESSION-103-002` / `SESSION-103-003`；实现 teardown 和派生会话 ledger/cleanup，完成
   定向、异常路径与零残留回归；
8. **Phase 6：集成与发布**——Phase 5.5 通过后再执行三 Executor 权限 E2E、Python/双机、
   失败注入与最终发布身份 Gate。

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
| 2026-08-29～09-06 | Phase 2 | 已提前完成 | 三 Executor early session 与 approval transport 已合入；Hermes unsupported 路径可行动且 fail closed |
| 2026-09-07～09-13 | Phase 3 | 已完成；progress 延期 | Approve/Deny、同 session resume 与极简弹窗已合入；`FLOW-103-001` 转入 1.0.4A |
| 2026-09-14～09-20 | Phase 4 | `PERM-103-007` 已完成 | Claude 文件级 capability 与 Runner fail-closed 校验通过；真实 canary 归发布验收 |
| 2026-09-21～09-24 | Phase 5 | 代码已完成；RC Gate blocked | 先修复跨版本 Skill 刷新/失败回滚；Intel 更新 Xcode/CLT 后重跑 install/upgrade/uninstall/services/PATH，ARM64 已通过的路径同时复验卸载自动清理边界 |
| Phase 5 Gate 后、Phase 6 前 | Phase 5.5 | P1 待开发 | 完成 `SESSION-103-002` E2E teardown 与 `SESSION-103-003` 派生会话 ledger/cleanup；异常路径、幂等删除、doctor blocker 与零新增遗留对话回归通过 |
| Phase 5.5 通过后 | Phase 6 | 候选包与双机安装 smoke 已提前完成 | 重新出候选包，执行真实三 Executor 权限 canary、故障注入、P0/P1 收口复验和最终发布身份 Gate |

目标发布窗口仍以 `2026-09-27` 为基准，但不得越过 Phase 5.5 零残留门禁；若 session P1、
Hermes early approval/session capability 或 Claude 文件级 capability 无法由当前上游 CLI
精确表达，本版按既定 fail-closed 合同交付，不以危险近似实现换取日期。

### 9.3 权限审批测试任务流程（`PERM-TEST-103`）

测试只使用当前候选包和独立 canary 根，不在 AgentBC 源码 worktree、用户真实项目、Executor
私有 session 目录或现有任务链内制造权限阻塞。每个真实 Executor canary 使用新根 Task、显式
`--permission-mode safe` 和独立 customer path；不得从旧 Task handoff，以免继承权限、session
或 recovery 状态污染结论。真实弹窗决策必须由验收人执行，控制器不得自动批准，也不得把
`accepted`、进程退出码或聊天总结当完成证据。

| 测试任务 | 执行方式 | 核心动作 | 退出门禁 |
| --- | --- | --- | --- |
| `PERM-TEST-103-00` 身份与隔离预检 | 串行，只读 | 固定候选 commit、wheel/manifest hash、CLI/Runner/Skill identity、三 Executor capability、空 canary 根和无 active blocker | package/Runner/三 Skill 同版本同 provenance；Runner `ready`；若目标 Executor unsupported 或存在 blocker，停止真实 canary |
| `PERM-TEST-103-01` 自动合同回归 | 串行门禁 | 运行 registry、mode、approval receipt、dialog、lifecycle、taxonomy、Codex app-server、Claude control、Hermes ACP 与 legacy cutover 定向测试 | 全部通过；15 类稳定原因、request fingerprint、single-action scope、close/timeout Deny、replay/cross-task/cross-run fail-closed 均有断言；不得用真实 full 权限做故障注入 |
| `PERM-TEST-103-CODEX` | 与另两个真实 canary 并行 | Codex app-server 在同一 Task 内依次请求两个无害终端动作：首个 Approve 后创建 `approved.txt`；第二个必须生成新 request，Deny 后不得创建 `denied.txt` | 首次工具动作前已有官方 thread receipt；等待时 RunLease `SUSPENDED`；Approve 只响应同 request/fingerprint，按同 session 恢复且不改变 Task `safe` 快照；第二动作不能继承授权；最终 callback/report/lease/cleanup 完整，且 transport 证据为 production app-server，不以 full fallback 代替 |
| `PERM-TEST-103-CLAUDE` | 与另两个真实 canary 并行 | Claude permission-prompt tool 执行与 Codex 相同的 Approve→新 request→Deny 无害文件 canary | preallocated 官方 session receipt、同进程 request/response、单动作不泄漏、Deny 后产物不存在；临时工程无越界交付物；最终证据要求同上 |
| `PERM-TEST-103-HERMES` | 与另两个真实 canary 并行 | Hermes ACP `session/new` 后执行相同的 Approve→新 request→Deny 无害文件 canary | 首次 prompt/tool 前已有 ACP session receipt；无 TTY 超时等待；Approve/Deny 经同一 ACP session 返回；无 `--yolo` 注入；最终证据要求同上 |
| `PERM-TEST-103-02` 故障注入 | 三 canary 后串行 | 使用 fake/fixture transport 注入 receipt 缺失、session/run/request/fingerprint 不匹配、重复响应、transport death、stale lease/chain、close 与 timeout | 每项进入精确稳定错误或自动 Deny；grant 撤销，不能执行动作、猜 session、恢复到 `full` 或泄露 raw argv/session/token；status/report/doctor 使用同一脱敏 projection |
| `PERM-TEST-103-03` 兼容与继承 | 串行 | 验证默认 `inherit`、显式 `safe`、handoff 继承、同 Task retry/recover/respond 冻结；对不支持 single-action 的 fixture 验证一次性 full continuation 兼容兜底 | 新根读取当前默认，handoff/retry/recover 不漂移；fallback 只消费一次并绑定 Task/session/run，不能被普通文本、native dangerous config 或新 Task 复用 |
| `PERM-TEST-103-04` 双机安装态复验 | 可并行 | Mac mini 与 Intel MacBook 分别从同一 wheel 运行 package smoke、doctor、Runner refresh 和三 Skill identity 检查，再执行至少一个真实 Executor approval canary | 两机 build identity、hash、Runner 与 Skill 一致；配置/record/report/artifact/customer project 未变化；本轮已完成安装 smoke，仍需补真实 approval canary 后关闭 |
| `PERM-TEST-103-05` 最终人工验收 | 串行收口 | 逐项读取每个 Task 的 status/report/callback、RunLease、approval/session receipt、logs、artifact 与测试输出 | 三 Executor分别标记 accepted/needs correction/blocked；只有三条真实链、故障注入和兼容门禁全部通过才关闭权限工作流；不得自动合并、发布或升级用户权限 |

真实 canary 的每个 Executor Task 固定声明三个结构化 step：①触发第一个无害动作并等待
Approve；②确认 `approved.txt` 后触发第二个独立动作并等待 Deny，确认 `denied.txt` 不存在；
③输出脱敏验收摘要并以合法 callback 收口。控制器在每次 `input_required` 时先核对 task/run/
session/request/fingerprint、blocked step、RunLease 与弹窗内容，再把决策留给验收人。任何一步
出现 fallback、receipt 缺失、自动授权、跨 request 响应或权限快照变化，立即停止该 Executor
链并按精确错误证据修复，不继续用其他 Executor 的成功结果覆盖失败。

控制器为三个 Executor 分别生成一份 steps 文件，内容固定为以下合同；若目标版本没有产生
原生 approval event，Executor 必须停止并报告 `approval_not_triggered`，不得自行模拟
`input_required` 或改用 full：

```yaml
steps:
  - id: 1
    description: "通过当前 Executor 的原生审批 transport 请求一个 single_action 权限，只执行无害命令创建 approved.txt；等待验收人 Approve 后确认文件内容，不得请求或使用 task full 权限"
  - id: 2
    description: "请求第二个独立 single_action 权限以创建 denied.txt；该请求必须使用新的 request ID，等待验收人 Deny，并确认 denied.txt 不存在且第一项授权未泄漏"
  - id: 3
    description: "核对 Task 权限快照仍为 safe、session ID 未漂移、审批与 RunLease 证据完整，输出脱敏测试摘要并按声明的三个 step 生成唯一合法 final callback"
```

在创建任务前为 Codex、Claude、Hermes 分别建立空的绝对 canary root，并将下方占位符替换为
实际路径；三个根不得复用。控制器机械核对 `--assignee` 后才允许并行 dispatch：

```bash
agentbc task create --title "PERM-TEST-103 <executor> single-action canary" \
  --assignee <codex|claude|hermes> \
  --steps /tmp/perm-test-103-<executor>.yaml \
  --source-platform codex \
  --customer-path <absolute-isolated-canary-root> \
  --permission-mode safe \
  --dispatch \
  --config ~/.abc/config.toml
```

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
- `PERM-TEST-103` 的三条真实 Executor canary 必须分别证明首次审批前官方 receipt、
  `single_action` Approve、下一动作重新询问、Deny 不执行、同 session resume 与终态 cleanup；
  自动化 fixture、旧版 full canary 或任一 Executor 的通过不能替代另外两条真实链；
- `agentbc update` 自动 check；无更新零写入，有更新以 `y/N` 确认，升级成功后 CLI、Runner 与
  三平台 Skill identity 一致，失败时旧版本仍可启动；配置、record、report、artifact 和
  customer project 均保持；Homebrew 与 PyPI/local bundle 迁移有可复现证据；
- AgentBC 自带 E2E 和任务派生 Executor 会话均有 task/run scoped 官方 receipt；默认
  `retain=false` 时，成功、Deny、timeout、transport lost、进程异常与 Runner 重启后都能定向、
  幂等清理，report/doctor 不得把未登记或未清理的子会话误报为 cleanup 完成；
- 单测、Runner 集成、真实 CLI canary、Ruff、compileall、`git diff --check` 和发布身份检查通过。

## 11. 明确不做

- 不让 Agent 自行决定或填写最终权限；
- 不通过扫描私有 session 数据补 receipt；
- 不把 Hermes `--yolo` 当作 safe 的临时修复；
- 不允许普通 input 文本修改权限快照；
- 不从自然语言或被系统覆盖的 callback 猜测已完成 step；
- 不在 `1.0.3A` 实现 `FLOW-103-001`；资源耗尽权威 progress receipt 与结构化多 steps、协议
  fixtures 和局部重构一起进入 `1.0.4A`；
- Alpha 不提供 `agentbc update rollback` 或其他用户主动回滚命令；
- 不在 `1.0.3A` 建设完整 Executor fixture matrix 或主动拆分共享模块；二者随局部重构进入
  `1.0.4A`；
- 不在 `1.0.3A` 改造 handoff 单 step 合同；结构化多 steps、预检与 Executor 一致性测试进入
  `1.0.4A / FLOW-104-001`；
- 不在 `1.0.3A` 继续调整 native Deny 与 full fallback 衔接；审批通知机械判定、Deny 单调终态
  和 Core-owned fallback eligibility 进入 `1.0.4A / PERM-104-001`，不得以强化 Prompt 代替；
- 不用 update 静默覆盖用户 Skill 或配置；
- 不在 `1.0.2A` 临时回填这一协议级改造。
