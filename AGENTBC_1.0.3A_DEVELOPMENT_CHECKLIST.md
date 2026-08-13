# AgentBC 1.0.3A 需求开发清单

> 制定日期：2026-08-11  
> 状态：需求冻结中，尚未开始实现  
> 目标版本：`v1.0.3A`  
> 前置条件：`1.0.2A` 发布 Gate 完成  
> 架构依据：`AGENTBC_ALPHA_DEVELOPMENT_HANDBOOK.md`

## 0. 产品目标

`1.0.3A` 在既定 update/Homebrew、协议 fixtures 和模块机械拆分之外，新增 P0
权限治理主线：AgentBC 提供一个统一、可解释、可审计的 Agent 权限设置，并把它确定性
映射到后续新派发的 Codex、Claude、Hermes 任务。用户不再需要同时理解 AgentBC 全局
默认、handoff 来源权限、Executor 原生全局配置和各 CLI 私有参数。

本版同时补齐权限阻塞到 `input_required` 的控制平面闭环。权限审批必须成为结构化运行
事件；不能继续依赖 Agent 在最终自然语言中主动填写 marker，也不能因为 Executor 没有
TTY 而等待 60/120 秒后静默拒绝。

版本边界已冻结：统一权限继承、权限审批弹窗及其 Adapter/Core/Runner 协议全部由
`1.0.3A` 承担，不再回填 `1.0.2A`。`1.0.2A` 只修复已承诺的 Hermes 最终 session receipt
稳定性；“在首次工具审批前交付 session receipt”的早期 handshake 仍属于本版。

## 1. 当前问题与责任边界

### 1.1 权限来源过多

当前 `explicit task > handoff source > AgentBC config > legacy safe` 的优先级会让新根任务、
handoff、retry/resume 和 Executor 原生配置呈现不同结果。Hermes 的 `inherit` 与 `safe`
目前都不追加权限参数，命令层无法证明二者语义不同；若用户原生配置改变，任务行为可能
漂移。另一方面，修改 AgentBC 全局默认不会改变已创建任务或从旧任务继承的 handoff，
用户容易误以为“全局 full 已生效”。

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

## 2. P0：统一 Agent 权限设置（`PERM-103-001`）

- 新增统一用户入口：

  ```text
  agentbc permissions status
  agentbc permissions set safe
  agentbc permissions set full
  ```

- setup 使用同一设置来源，显示“影响后续新派发任务”，不得暗示会修改 active、
  `input_required`、`needs_recovery` 或同 Task resume；
- 用户主流程只暴露 `safe` / `full` 两个清晰模式。现有 `inherit` 作为 legacy/高级兼容值
  双读，不再作为 setup 默认选项；升级不得把历史 `inherit` 静默改成 `full`；
- 每次创建新的根任务或 handoff iteration 时，从当时的统一全局设置生成权限快照；
  retry、recover、input response 和同 Task resume 继续使用该 Task 已冻结快照；
- 显式 task override 只影响该新 Task，必须在 create/dispatch 输出、preflight、status、
  report 中同时显示 `configured_mode`、`task_override`、`effective_mode`、`mapping` 和
  `scope`，不得只显示模糊的 `selection_source`；
- 全局设置、task override 与最终 Executor argv 必须通过同一个 permission registry 映射，
  Setup、Service、Adapter、Runner 不得各自维护一份条件分支。

## 3. P0：Executor 权限能力映射（`PERM-103-002`）

目标映射必须由真实 CLI capability probe 固定，并由 Runner 对任务快照 fail closed 校验：

| 模式 | Codex | Claude | Hermes |
| --- | --- | --- | --- |
| safe | workspace-write + 明确非交互审批语义 | safe-mode + `acceptEdits` | 必须新增受限、非交互、可审计的 safe approval 能力；未探测到时拒绝后台派发并给出可行动原因 |
| full | strongest documented bypass flag | `--dangerously-skip-permissions` | `--yolo` |

- `safe` 不得读取 Executor 原生危险全局配置后静默升级；
- `full` 必须由统一设置或显式 task override 产生，记录审计，不允许由 prompt、普通
  input response、handoff 文案或原生配置注入；
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
- Later、关闭和超时保持 waiting，不得伪造 deny，也不得丢失 session；
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
- handoff 行为变更必须有协议版本：新协议按“创建时读取统一全局设置”，旧协议继续读取
  来源任务快照，避免升级后重解释历史任务；
- User Guide、三 Executor skills、setup/help/doctor/status/report 同步统一术语。

## 7. 实施阶段

1. **Phase 0：契约冻结**——permission registry、配置/任务快照、approval event、早期
   session receipt、legacy 双读与预期失败测试；
2. **Phase 1：统一设置入口**——配置事务、setup、`agentbc permissions`、公共视图和迁移；
3. **Phase 2：三 Executor 映射**——capability probe、canonical argv、Runner 校验与 Hermes
   safe unsupported 的显式预检；
4. **Phase 3：审批控制平面**——Adapter approval event、Core input wait、弹窗响应、精确动作
   授权和同 session resume；
5. **Phase 4：真实 canary 与发布**——三 Executor safe/full、权限阻塞、approve/deny/Later、
   direct/Runner receipt、handoff/retry/resume 和旧任务兼容。

### 7.1 `1.0.3A` 开始前的人工过渡规则

- 每次创建新的根任务或 handoff iteration 前，控制端先向用户确认目标 Agent 完成任务
  所需权限是 `safe` 还是 `full`；
- 派发时显式传入 `--permission-mode safe|full`，不得依赖当前多来源继承结果；
- `full` 只在明确提示风险并取得本次派发授权后使用；不能把 Hermes `--yolo` 当作默认；
- retry/recover/resume 保持任务已冻结权限，不借普通 input 改权；
- 该规则是操作门禁，不要求 `1.0.2A` 新增命令、弹窗或协议字段；统一设置与自动弹窗在
  `1.0.3A` Phase 1～3 替代人工流程。

## 8. 验收门禁

- 改一次统一全局权限后，后续 Codex/Claude/Hermes 新任务均展示并执行正确映射；
- 同 Task resume 不因全局设置变化而漂移，新 handoff iteration 按新协议读取当前统一设置；
- Hermes safe 无可用 headless approval 能力时在派发前明确拒绝，不再运行二十分钟后超时；
- 三 Executor 权限受阻均进入同一 `input_required` 弹窗，任务保留官方 session ID；
- approve 只执行精确授权动作，deny 有明确结果，Later/关闭/超时保持等待；
- 缺失 receipt、伪造 permission argv、native dangerous config、safe→full 注入全部 fail closed；
- 单测、Runner 集成、真实 CLI canary、Ruff、compileall、`git diff --check` 和发布身份检查通过。

## 9. 明确不做

- 不让 Agent 自行决定或填写最终权限；
- 不通过扫描私有 session 数据补 receipt；
- 不把 Hermes `--yolo` 当作 safe 的临时修复；
- 不允许普通 input 文本修改权限快照；
- 不在 `1.0.2A` 临时回填这一协议级改造。
