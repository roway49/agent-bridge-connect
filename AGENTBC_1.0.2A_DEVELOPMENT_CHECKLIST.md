# AgentBC 1.0.2A 需求开发清单

> 制定日期：2026-08-08  
> 状态：开发启动  
> 当前开发分支：`private/integration`  
> 固定 Agent 分支：`agent/codex`、`agent/claude`、`agent/hermes`  
> 开发基线：`private/integration@cfddccba246e6d057172f6716ab4318ade9a40ad`  
> 对应公开稳定修订：`v1.0.1A3` / Python `1.0.1a3` / `5e74de65c9b49867ac7957969138db59e2208572`  
> 目标版本：`v1.0.2A` / Python `1.0.2a1`

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
- `service.py`、`runner.py`、`cli.py`、`setup.py` 的大规模结构拆分。

允许的重构只限于支撑本清单功能的局部公共模块，并且机械迁移与语义变化必须分提交。

## 2. 当前源码盘点

| 项目 | 当前状态 | 1.0.2A 处理口径 |
| --- | --- | --- |
| `task delete` | 未实现；现有 `task close` 只删除当前非终态 head | 新增整条终态历史链删除命令 |
| `doctor [--json]` | 已有基础实现和 A3 构建身份 | 增量补齐 Skill 漂移、配置/runtime 漂移、稳定 schema/退出码和 blocker |
| 执行会话保留 | 未实现 | 作为本版主功能实现安全策略与能力回执 |
| Claude 预算 | Executor/setup 存在隐藏默认值 `1.0` | 改为用户可见、可验证、可独立配置 |
| 执行时长 | 已区分 input waiting，但仍以 wall-waiting 近似 execution | 改为真实 run interval 累计，排除恢复等待等非运行区间 |
| Prompt 公共契约 | 三个 Adapter 各自构造，公共规则重复 | 做行为不变的公共 builder 和长度门禁 |
| Skill 身份 | package template 是 canonical source，但无安装 hash 握手 | 写入版本/协议/template hash，doctor 检查漂移 |
| build identity | 已在 A3 修复并通过发布链验证 | 作为回归门禁，不重复开发 |
| execution lease 快照 | 原始 extension 可能滞后 | 统一当前视图来源，避免 status/report 展示旧 lease 事实 |

## 3. 需求总表

| ID | 优先级 | 需求 | 复杂度 | 预计工作量 | 主要责任模块 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| `DEL-001` | P0 | 安全删除终态历史链并归还 task code | 中高 | 4～5 人日 | Service、Store、ID、CLI、Index | A3 基线 |
| `SESSION-001` | P0 | 执行 Agent 临时会话保留策略 | 高 | 5～8 人日 | Config、Setup、CLI、Adapter、Worker/Service、Doctor | cleanup contract |
| `SKILL-001` | P0 | Controller contract 单一来源与 Skill hash 握手 | 中高 | 4～5 人日 | Skill template、Setup、Doctor | doctor 基础实现 |
| `DOC-002` | P0 | 完成只诊断 doctor 契约 | 中 | 3～4 人日 | Doctor、Registry、Runner query、CLI | SKILL-001、SESSION-001 receipt |
| `REPORT-001` | P1 | 修正恢复任务累计执行时长 | 中 | 2～3 人日 | RunLease、Service timing、Report、Task List、Notification | A3 input waiting 基线 |
| `CFG-001` | P1 | Claude 单次任务预算用户配置 | 低至中 | 2～3 人日 | Config、Setup、CLI、Claude Adapter、Preflight | doctor 配置视图 |
| `PROMPT-001` | P1 | 三 Executor 公共 Prompt 契约去重 | 中 | 2～3 人日 | 新公共 builder、Codex/Claude/Hermes Adapter | characterization tests |
| `OBS-001` | P1 | 当前 execution lease 状态单一派生视图 | 低至中 | 1～2 人日 | RunLease query、Status、Report | REPORT-001 |
| `DOCFIX-001` | P2 | 修正文档/help 漂移 | 低 | 0.5～1 人日 | Record README、CLI help、双语文档 | DEL-001 |
| `REL-102` | Gate | 1.0.2A 版本、双机、真实 Executor 与发布验收 | 中 | 2～3 人日 | Build/CI/docs/release | 全部需求 |

建议功能开发净工作量约 `25～37` 人日；三个 Agent 并行且严格控制巨型文件冲突时，目标
日历周期约 3～4 周。任何真实 Executor 能力缺失应按 `unsupported` 交付，不得用危险
文件扫描缩短排期。

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
sessions.retain_executor_sessions = true

agentbc session retention status
agentbc session retention enable
agentbc session retention disable
```

要求：

- 交互 setup 默认保留；非交互 setup 保留旧值，无旧值时写 `true`；
- 所有用户文案明确区分 dispatcher conversation 与 executor temporary session；
- AgentBC 永不删除派发源对话；
- Adapter 明确报告 session ID、是否持久化、是否支持官方安全清理及清理结果；
- 只在 terminal、RunLease closed、最终 task/report 落盘、通知入队后请求清理；
- `input_required/needs_recovery/active/stale` 不清理；
- 只使用官方 CLI/API 的不持久化或删除能力；禁止猜路径、扫描最新会话或递归删除目录；
- unsupported/failed 只生成 receipt 和 doctor warning，不改变原任务终态；
- enable/disable 原子更新配置，仅对后续新 run 生效。

验收：setup Yes/No/EOF/non-interactive、三命令幂等、retain on/off、三 Executor capability、
恢复路径、handoff、失败回执、secret redaction 与 dispatcher conversation 不受影响。

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

### 4.4 `CFG-001`：Claude 单次任务预算

要求：

- setup 检测 Claude 后显示当前值与建议值 `$2.5`；已有用户值必须保留并显示；
- 交互输入只接受大于 0 的有限金额，空输入使用建议值；不提供无限预算；
- non-interactive 支持 `--claude-max-budget-usd <value>`，未提供时保留旧值；
- 提供精确配置查询和修改入口，原子写 TOML 且保留其他配置；
- 修改预算不自动重启 Runner、不重试历史任务；下一次 Claude run 生效；
- create/dispatch accepted、preflight 和 status JSON 显示本次 effective budget；
- task 记录只保存金额，不保存凭据或 Claude 私有配置。

验收：首次 setup、升级 setup、空值、非法数、NaN/Inf、命令幂等、配置保真和真实 Claude
任务预算可见性通过。

### 4.5 `REPORT-001` + `OBS-001`：真实执行时长与 lease 当前视图

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

### 4.6 `PROMPT-001`：公共 Prompt 契约去重

要求：

- 先建立三个 Adapter 当前 prompt 的 golden/characterization tests；
- 新建共享 prompt contract builder，公共身份、路径、报告所有权、progress、strict marker、
  resumed-input 规则只生成一次；
- Adapter 只追加 argv、权限、图片、Hermes transport 等平台差异；
- 不改变当前 v1 strict marker、permission、Path Plan、input_required 或 report 所有权；
- 10 步 task 的公共 prompt 目标不超过约 3,000 字符，并阻止按 step 重复公共规则；
- 机械迁移和文本/协议语义变化分提交，本版不引入 v2 sidecar。

验收：三个 Executor prompt snapshot、10 步长度、resumed turn、图片/权限差异和真实
Codex/Claude/Hermes canary 通过。

### 4.7 `DOCFIX-001`：文档与 help 一致性

- `record clean --help` 明确只清理可清理的运行诊断，不删除 Report；
- Record README 明确 queued pending 可以 close；
- 增加 help/template/用户文档一致性测试；
- 同步中英文 User Guide、Quick Start 和三类 Skill 的新增命令。

## 5. 开发波次与分工原则

### Wave 1：可并行、低冲突

| Agent 分支 | 首项任务 | 文件所有权重点 |
| --- | --- | --- |
| `agent/codex` | `DEL-001` | delete 的 Service/Store/ID/CLI 路径；不改 Executor prompt |
| `agent/claude` | `REPORT-001` + `OBS-001` | timing/RunLease/Report/View；非必要不改 `cli.py` |
| `agent/hermes` | `PROMPT-001` | prompt contract 与三 Adapter；不改 Service 生命周期 |

Wave 1 每个分支必须交付完整、可单独测试、可合并的清洁提交。禁止三个 Agent 同时在
`service.py/runner.py/cli.py/setup.py` 上做交叉语义修改；发现必须跨边界时先停在接口契约，
由 integration 审查后安排下一波。

### Wave 2：在 Wave 1 合入后推进

1. `SESSION-001`：先冻结 cleanup capability/receipt contract，再接 Setup、CLI 和终态 hook；
2. `CFG-001`：与 SESSION 的 setup/config 改动串行，避免 TOML 和交互流程冲突；
3. `SKILL-001`：抽取 controller contract 并生成 Skill 身份；
4. `DOC-002`：消费 Skill、session、budget、timing 的最终诊断字段；
5. `DOCFIX-001`：随相邻功能合入，但保持独立提交。

### Wave 3：版本收口

1. 全量单元/Service/Runner 回归；
2. Python 3.10/3.11/3.14 Gate；
3. wheel/sdist、Twine、clean-install smoke；
4. 三 Executor 真实任务和 retain on/off 能力验证；
5. MacBook 私有 Gate、唯一 commit 构建、开发机同 SHA canary；
6. 文档、版本、tag、Release、PyPI 三方身份与哈希一致。

## 6. 分支、提交与验收规则

- 三个固定 agent worktree 每轮开始前必须同步最新 `private/integration`；
- 每个 Agent 只在自己的固定分支工作，不新建一次性长期分支；
- 失败任务也必须把分支恢复到可合并或无变更的清洁状态；
- 审查通过后才合入 `private/integration`，随后三分支再次同步；
- 不在本开发机操作公开 `main`、tag、Release 或 PyPI；
- 功能实现、机械重构、协议语义变化和文档更新使用可辨认的独立提交；
- `accepted` 只证明派发；完成验收必须检查 status、report、RunLease、测试和 git diff；
- 涉及删除、会话清理和配置写入的测试只能使用临时根目录，不触碰真实用户数据。

## 7. 版本级完成定义

`1.0.2A` 只有同时满足以下条件才允许进入候选：

- `DEL-001` 不触碰 customer project，异常中断不产生半删除或提前释放 ID；
- SESSION retain 默认开启，关闭后也只处理当前 terminal run 的官方可确认执行会话；
- doctor 能识别 package/Runner/Skill/Executor/config/session cleanup 漂移且 JSON schema 稳定；
- Claude 预算在 setup、配置、preflight/status 和真实执行中一致可见；
- 恢复等待不再计入 execution duration，全部用户界面使用同一 timing view；
- 三 Executor prompt 不重复公共规则，v1 完成协议和权限行为不变；
- 所有新增删除/配置/清理动作幂等、原子、可审计且不泄露 secret；
- A3 的严格终态、input_required、权限、路径、conversation trace 和发布身份无回归；
- 当前全量测试基线及新增测试全部通过，真实 Executor 与双机 Gate 通过；
- 工作树清洁，候选版本、提交、构建来源和 SHA256 可追溯。

## 8. 首轮启动检查

- [ ] `private/integration` 已 fetch 并确认基线；
- [ ] `agent/codex`、`agent/claude`、`agent/hermes` 已同步本文档所在提交；
- [ ] 开发机 CLI、Skill、Runner 已升级到 `1.0.1a3` 同一构建；
- [ ] 无 active/input_required/needs_recovery 任务后再刷新 Runner；
- [ ] Wave 1 三任务 steps 明确文件边界、测试命令和 final marker；
- [ ] 三个任务使用各自固定 worktree 作为精确 `--customer-path`；
- [ ] 派发后记录 task ID，不把 `accepted` 当完成。
