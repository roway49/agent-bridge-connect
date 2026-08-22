# AgentBC 开发手册

> 当前公开版本：AgentBC `1.0.2A` / Python `1.0.2a1`
> 当前开发方向：`1.0.3A` 统一权限治理与结构化审批
> 私有开发入口：`/Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/integration`
> 公开仓库：<https://github.com/roway49/agent-bridge-connect>
> 文档属性：私有长期维护基线；生成公开候选时必须排除本文件

## 0. 手册定位

本手册只记录长期有效的架构、合同、开发护栏和版本核心变更，不保存逐日开发日程、
Phase/Wave、AgentBC 任务 ID、临时提交 SHA、单次测试数量或故障流水。

需要历史细节时按以下顺序查阅：

- 已发布用户变化：`CHANGELOG.md`；
- 当前版本需求与验收证据：对应版本的 `*_DEVELOPMENT_CHECKLIST.md`；
- 用户命令和行为：`docs/USER_GUIDE.md`、`docs/USER_GUIDE_ZH.md`；
- 真实实现：`src/agent_bridge_connect/` 与当前测试；
- 发布步骤：`docs/RELEASE_PROCESS_ZH.md`。

开始工作前先确认真实状态：

```bash
cd /Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/integration
git status --short --branch
git log -3 --oneline
agentbc runner status
agentbc task status
```

聊天总结、`accepted`、Task List 颜色和历史测试数字都不能替代当前 Git、Runner、Task、
Report、RunLease、产物与测试证据。

## 1. 产品定位与边界

AgentBC 是一个 local-first 多 Agent 任务控制平面。它统一协调 Codex、Claude Code 和
Hermes，将跨 Agent 工作转化为可派发、可追踪、可暂停、可恢复、可验收的结构化任务。

AgentBC 负责：

- 任务、步骤、链路和路径规划；
- Runner 外部执行、权限映射和命令校验；
- Executor session、资源上限和恢复策略；
- Task Brief、Report、Record、Index、通知与 Doctor；
- setup、Skill 安装、升级、卸载和发布身份。

AgentBC 不负责：

- 判断模型产物质量是否满足用户预期；
- 自动批准危险权限或工具动作；
- 删除 customer project 或 dispatcher conversation；
- 保证任意上游 CLI 版本永久兼容；
- 将一次成功 smoke 等同于完整发布健康。

`completed` 只表示当前 run 满足执行与流程合同，不表示产物已经通过质量验收。

## 2. 不可偏离的设计原则

### 2.1 单一事实来源

事实优先级固定为：

1. `task.json` 的任务状态和冻结策略；
2. `run_lease.json` 与 Runner/worker/Executor 进程证据；
3. 步骤、事件、错误和 response receipt；
4. Report 与实际产物证据；
5. final callback；
6. Task List、Skill 文案和聊天回复。

低优先级信息不得覆盖高优先级事实。Report、Index、Notification、Doctor 和 GUI 都是
派生视图，不能反向修改任务状态。

### 2.2 职责单向

```text
Controller / User
  -> Skill / CLI
  -> Runner
  -> Worker
  -> Executor Adapter
  -> Codex / Claude / Hermes CLI

Worker -> TaskService -> TaskStore
                    -> Report / Index / Notification
```

- Skill 只指导正确调用，不是安全边界；
- CLI 解析参数和呈现结果，不拥有领域规则；
- Runner 管理 IPC、进程、命令和路径授权，不成为第二套 TaskService；
- TaskService 决定生命周期、链路、终态和干预；
- TaskStore 负责原子持久化，不推断业务状态；
- Adapter 翻译上游 CLI 并解释结构化结果，不直接写任务记录；
- Core 生成 Brief、Report、Index 和通知，Agent 不拥有这些文件。

### 2.3 路径先规划再执行

所有任务先生成并冻结 Path Plan。路径只由 `path_model.py` 推导：

- 用户未指定工程时使用托管 artifact 目录；
- 用户指定目录或文件时归一化为 customer project；
- Runner 从磁盘上的权威 Path Plan 计算 task-scoped writable roots；
- Adapter 只消费已验证路径，不自行推导；
- 禁止为规避权限把用户工程复制进 AgentBC workspace。

### 2.4 用户数据优先保护

- customer project 永不由 close、delete、record clean、session cleanup 或 uninstall 删除；
- 自动删除只作用于 Path Plan 能证明属于 AgentBC 的记录、报告和托管目录；
- 删除前必须验证 canonical containment、symlink、Task ID 和目录所有权；
- 禁止按目录名猜测所有权，禁止对不明目录递归删除；
- linked worktree 不扩大外部 `.git` 或 `git_common_dir` writable roots。

### 2.5 原子、有界、幂等

- 配置、task JSON、请求和响应使用临时文件、fsync 与原子替换；
- 跨进程配置写入在 lock 内重新读取并只合并目标字段；
- 单迭代 Record 保持有界，长输出和用户产物不塞入 `task.json`；
- response、cleanup、终态写入和重试必须可幂等重放；
- prompt、raw command/output、token、secret 和 Executor 私有数据库路径不得进入公共投影。

## 3. 身份、目录与所有权

### 3.1 任务身份

```text
TASKCODE       一条任务链，例如 4XMC
TASKCODE-NNN   精确迭代，例如 4XMC-001
```

- handoff 复用 TASKCODE 并增加 iteration；
- 当前 chain head 是默认可继续对象；
- 根任务安全删除后 task code 才能重新进入可用池；
- ID 解析与分配只允许通过 `task_id.py`。

### 3.2 稳定目录

```text
~/Documents/AgentBC/workspace/
├── tasks/
│   ├── artifacts/YYYY-MM-DD/TASKCODE/
│   └── report/YYYY-MM-DD/TASKCODE/
└── record/TASKCODE/NNN/
    ├── task.json
    ├── events.jsonl
    ├── interventions.jsonl
    ├── lease.json
    ├── run_lease.json
    └── TASKCODE-NNN-run.log

~/.abc/config.toml
~/.abc/runner/runs/
/tmp/agentbc-runner-v2-<uid>/
```

托管模式的 project/artifact 位于 AgentBC workspace；customer-path 模式的用户产物写入用户
工程，Brief、Report 和内部 Record 始终由 AgentBC 管理。同一链路跨天 handoff 继续使用
根任务创建日，避免目录分裂。

### 3.3 数据所有权

| 数据 | 所有者 | 用途 | 清理边界 |
| --- | --- | --- | --- |
| Task Brief | Core | Executor 输入和用户核对 | 活跃及验收期保留 |
| Report | Core | 终态、步骤、路径和错误摘要 | `record clean` 永不删除 |
| Record | Core | 权威状态与有界运行证据 | 只清理 eligible 终态诊断 |
| Artifact | 用户/Executor | 任务产物 | 仅删除 AgentBC 托管产物 |
| Customer project | 用户 | 用户工程 | 永不自动删除 |
| Executor session | 上游 Executor | 同会话恢复 | 只走官方定向清理能力 |

## 4. 任务生命周期

### 4.1 状态与健康

必须区分任务状态、RunLease 状态和健康颜色。

| 状态 | 含义 |
| --- | --- |
| `pending` / `running` | 等待或正在执行 |
| `input_required` | 可恢复等待，不是终态 |
| `completed` | 执行与严格流程合同成立 |
| `needs_recovery` | 启动、恢复或进程证据不足 |
| `failed` | 已执行但无法确认合法完成，或用户明确拒绝继续 |

`terminal_states.py` 是公开终态集合的唯一入口。健康颜色只是最近进度与 lease 的展示，
不能推动任务状态，也不能代替进程证据。

### 4.2 创建、派发与完成

创建与派发的固定链路：

1. CLI/Skill 提供 title、assignee、steps、customer path 和权限选择；
2. Runner 验证配置、Executor、路径与 packet；
3. TaskService 创建任务、Path Plan、Brief 和冻结扩展；
4. Runner 启动 worker，worker 启动 Adapter/Executor；
5. Worker 持久化 run/session/lease 证据；
6. Executor 退出后 Core 同时核对退出结果和严格 final marker；
7. TaskService 原子写入终态、Report、Index、通知和 cleanup eligibility。

当前 completion contract 要求唯一且合法的 `AGENTBC_FINAL_CALLBACK`，Task ID 和步骤必须与
当前 run 匹配。退出码 0、自然语言“完成”或 callback 单独出现都不足以证明 completed。

### 4.3 Input Required 与 Respond

`input_required` 保留当前 Task ID、blocked step、input ID、冻结策略和官方 session receipt。
响应命令互斥：

```bash
agentbc task respond TASK-ID --input INPUT-ID --message "..."
agentbc task respond TASK-ID --input INPUT-ID --approve
agentbc task respond TASK-ID --input INPUT-ID --deny
```

Core 必须验证 current chain head、current input、deadline、RunLease 与无活跃 Executor。
重复响应幂等，旧 input 返回 stale，不得重复派发。恢复继续使用同 Task、同 worktree 和同一
官方 Executor session；无法证明 session 时 fail closed 为 `needs_recovery`。

权限确认只提供 Approve/Deny；关闭或超时按拒绝处理。普通 message、choice 文本或 prompt
内容不能创建权限授权。

### 4.4 权限合同

公开基础权限值为 `inherit|safe|full`，冻结在任务扩展中。优先级为显式 task option、
handoff 源任务、配置默认值和 legacy 回退；未知值、静默降级或 packet/disk 不一致必须拒绝。

- `inherit`：不覆盖上游 Executor 的原生权限设置；
- `safe`：使用 AgentBC 验证过的保守非交互映射；
- `full`：使用安装版 CLI 明确支持的最强非交互映射，并写审计证据。

1.0.2A 支持一次性 `safe -> full` continuation：只有合法 permission input、官方 session
receipt 和用户 Approve 才能为同一 Task、同一 Executor session 的下一次 run 发放一次性
授权。授权在进程启动前消费，基础权限快照不变，不得泄漏到 retry、recover、handoff、
reassign 或新任务。

不得引入 Git 专属任务状态、`commit_required`、`--git-write` 或 `--commit-sha`。需要完整
权限的动作统一走现有 permission input；更细粒度的动作授权属于后续权限 registry。

### 4.5 资源与会话

Claude 和 Hermes 的资源配置入口：

```bash
agentbc claude budget <usd>
agentbc hermes max-turns <turns>
agentbc session retention status|enable|disable
```

- setup 默认 Claude `$10`；
- Hermes 默认读取 `hermes config path` 对应配置，无法取得时回退 `90`；
- session retention 默认关闭；
- 配置更新只影响后续新任务；任务创建时冻结资源和 session 策略；
- retry/recover/resume 不重新读取全局配置；
- 资源耗尽进入 approve/deny，Approve 只为当前任务将上限翻倍并恢复同一 session，Deny 以
  明确原因 failed；
- `input_required` 无论 retention 设置如何都保留当前 session。

资源决策类型固定为 `approve_deny`，用户按钮为“提高预算并继续”和“终止任务”；公共资源
投影保留 `configured_limit`、`exhaustion_count` 和 `last_decision`，CLI fallback 使用
`--approve` / `--deny`。这些字段是稳定行为合同，不是单次开发阶段记录。

终态 session cleanup 只在 RunLease 关闭、Report 就绪和通知记录完成后运行。它只管理
Executor 临时会话，永不删除 dispatcher conversation。清理必须使用官方、精确 session ID
对应的 CLI/API；不支持或失败只更新有界 receipt 和 Doctor warning，不改变任务终态。

Claude 临时 project 使用任务托管 artifact 路径，不另建用户可见 runtime 根。只有
retain=false 且 Path Plan 证明为 ephemeral project 时才允许官方 project purge，再逐层
`rmdir` 已验证的空目录；用户工程永不 purge，禁止递归删除。

### 4.6 Handoff、Recover、Delete 与 Clean

- handoff 从 completed 的 current chain head 创建下一 iteration，保留 lineage、路径和必要
  上下文；权限默认继承源任务；
- recover 只在有明确恢复证据时重新派发同一任务，不创建新 iteration；
- `task delete` 先展示将删除的 AgentBC 记录、报告和默认 artifact，再要求 `y/N`；
- customer project 不在 delete 范围；
- `record clean` 只清理 eligible 终态运行诊断，永不删除 Report；
- cancel、terminal、reassign 和再次 input_required 必须撤销尚未消费的一次性授权。

## 5. Runner 与 Executor

### 5.1 Runner 安全边界

Runner 解决 Controller 沙箱无法直接访问用户 CLI、profile 和 customer path 的问题。它不是
通用 shell server。

- IPC 请求带随机 ID、token 和过期时间，通过原子文件邮箱发布；
- 同一身份只允许一个有效 Runner；
- 只执行 Registry 登记的 Executor 和规范 argv，使用 `Popen(argv)`，不使用 shell；
- 每次 authorize/submit 都重新读取磁盘权威任务；
- cwd 和 writable roots 必须来自 stable roots 或冻结 Path Plan；
- 原始用户 flags、重复/冲突权限参数、篡改 session/resource 参数全部 fail closed；
- Runner 维护 worker PID 与 executor PID，close/cancel 必须处理两层；
- Doctor 的 workspace/report/record 写权限以同身份 Runner 的受限探测为准，不能用 Controller
  safe 沙箱中的 `os.access()` 代替。

### 5.2 Adapter 合同

| Executor | safe | full | session 关键点 |
| --- | --- | --- | --- |
| Codex | workspace-write sandbox | 官方 bypass approvals/sandbox | 只接受唯一 `thread.started.thread_id`，resume 不用 `--last` |
| Claude | safe-mode + acceptEdits | 官方 skip permissions | fresh 使用预分配 session ID，resume 使用精确 ID |
| Hermes | 正常审批语义，不启用故障排查 safe-mode | `--yolo` | 只接受官方 stderr receipt，禁止猜测或 `--continue` |

具体 argv 必须由当前安装版 CLI help/capability probe 验证。缺少正式能力时返回 unsupported，
不能猜测别名、降级权限或静默新建 session。

新增 Executor 必须同时更新 Adapter、Registry、discovery/setup、Runner allowlist、Skill、
用户文档、fake 测试和真实 CLI canary。平台差异留在 Adapter，公共生命周期留在 Core。

## 6. Report、通知与 Doctor

### 6.1 公共投影

status、preflight、report、notification 和 doctor 应复用同一组脱敏 projection：

- 任务状态、步骤和稳定错误码；
- 基础权限及临时授权的 from/to、scope、state；
- 资源 effective limit、source、frozen；
- session retain/state、resume count 和 cleanup capability/state；
- execution、waiting 和 wall duration；
- package、Runner、Skill 与 Executor identity。

公共输出不得暴露 grant/session/run ID、内部 project path、raw diff、完整命令、help/output、
token、secret 或 Executor 私有存储路径。

### 6.2 通知

- 终态通知只陈述已经持久化的状态和 Report；
- input notification 是非终态即时事件，不触发终态 cohort 退出；
- permission 弹窗只有 Approve/Deny，短文案展示必要风险，完整原因通过额外交互展开；
- 投递失败只影响通知 receipt，不改变任务终态；
- dispatcher 和 executor 身份必须分开显示。

### 6.3 Doctor v2

Doctor text 与 JSON 只消费同一诊断对象：

- `0 / healthy`：核心执行链可用；
- `1 / warning`：单个 Executor 缺失、Skill 漂移、cleanup unsupported/failed/stale 等非核心问题；
- `2 / unavailable`：配置损坏、Runner 身份漂移、Record 根不可读、必要存储失效或 package
  identity 无法验证。

单项 probe 失败必须结构化降级，不能让 Doctor 崩溃。诊断输出只给稳定状态和修复建议，
不打印 raw help、session 内容、凭据或私有数据库。

## 7. 配置、Setup 与 Skill

### 7.1 配置事务

`config.py` 是 TOML 读写唯一入口：

- 同目录常驻 lock + POSIX `flock`；
- lock 内重新读取并 read-modify-write；
- 唯一临时文件、`0600`、flush/fsync、`os.replace` 和父目录同步；
- 无变化不重写；非法旧配置 fail closed；未知 section/key 保留。

setup refresh 只更新 AgentBC 拥有的发现/运行字段，不覆盖用户预算、turns、retention 和未知
配置。首次 setup 默认权限为 `inherit`、retention 为 `false`。

### 7.2 Skill 单一来源

Canonical Skill 位于：

```text
src/agent_bridge_connect/skills/
├── references/controller-contract.md
├── codex_skill.md
├── codex_openai.yaml
├── claude_skill.md
└── hermes_skill.md
```

三平台 Skill 只保存平台差异，共享 controller contract 为唯一命令语义来源。安装包写入
`.agentbc-skill.json`，记录 schema、平台、package/protocol/completion 版本和受管文件 hash。
setup/doctor 区分 current、legacy、missing、modified、partial；更新不得静默覆盖用户修改。

修改 Skill 必须同时验证 package template、renderer、manifest/hash、fake install 和真实新
会话。只手改用户目录或只改模板都不算完成。

## 8. 模块责任索引

| 模块 | 唯一职责 |
| --- | --- |
| `protocol.py` / `state_machine.py` / `terminal_states.py` | 模型、转换和终态集合 |
| `task_id.py` / `task_store.py` | ID、原子持久化与 claim lease |
| `service.py` | 生命周期、链路、干预与终态领域规则 |
| `path_model.py` | Path Plan 唯一推导与验证 |
| `execution_contract.py` / `prompt_contract.py` | final marker 与共享 prompt 合同 |
| `execution_policy.py` | 资源、session、cleanup 和公共执行策略投影 |
| `permission_modes.py` / `permission_grants.py` | 权限模式与一次性授权 |
| `run_lease.py` / `session_cleanup.py` | 进程证据与终态 session cleanup |
| `runner.py` | IPC、授权、进程、派发和 maintenance |
| `executor_registry.py` / `executors/*` | Adapter 构造、argv 与上游结果解析 |
| `reports.py` / `task_index.py` | Brief、Report 与索引派生视图 |
| `record_management.py` | Record 预算、README 和诊断清理 |
| `notifications.py` / `notifiers/*` | 结构化通知与投递 |
| `doctor.py` | Doctor v2 collectors、projection 与退出码 |
| `config.py` / `setup.py` / `skill_packages.py` | 配置、发现、安装、更新和 Skill 握手 |
| `cli.py` | 参数、命令路由、展示和 worker 入口 |

`task.py`、`task_board.py`、`task_completion.py` 和未正式接线的 `mcp_server.py` 属于 legacy 或
dormant 范围，新功能不得继续依赖。删除前仍需查外部导入、补 characterization 并给出迁移。

## 9. 修改功能的固定方法

### 9.1 状态或终态

同步检查 protocol、terminal set、Service、health、RunLease、Report、Notification、record
clean、close/cancel/recover/handoff、CLI 和三 Adapter。无法同步完整时不要新增状态。

### 9.2 路径或删除

只在 `path_model.py` 定义不变量；Service 固化，Runner 复核，Adapter 消费。customer/default、
文件路径、跨日 handoff、symlink、越界、非空目录和重复执行都必须测试。

### 9.3 权限、资源或 session

变更必须贯穿 config -> task snapshot -> Adapter argv -> Runner validation -> receipt ->
status/report/doctor。只改 setup 默认值、prompt 或 Adapter 参数都不构成端到端完成。

### 9.4 Report、Record 或通知

先找唯一 projection 和事实来源；Report 不接受 Agent 写入，Record 不保存大输出，通知不推断
状态。任何新增公共字段都要做 text/JSON 同源、脱敏和旧任务兼容测试。

### 9.5 重构或协议迁移

固定顺序：

1. 写 characterization/golden；
2. 机械拆分并保持 API、CLI、磁盘和输出不变；
3. 单独引入 schema/protocol version；
4. 新任务写新格式，历史任务双读；
5. 跑单元、Service/Runner、package、真实 Executor 和双机门禁；
6. 失败时回滚当前阶段，不堆兼容特判。

机械拆分和协议语义变化不得混在同一提交。多人并行时先按文件和责任边界拆任务。

## 10. 开发与合并护栏

- 开发只在固定 agent worktree；integration 控制端审阅、提交、合并和同步分支；
- Executor 任务不得执行 git add/commit/push/merge/rebase，避免 linked-worktree `.git` 写入冲突；
- 失败分支先恢复干净基线，再用新根任务重新派发，不沿用污染现场；
- 验收必须查看 status、report、callback、RunLease、execution session receipt、diff 和测试；
- 不把 `accepted` 或 Agent 自述当完成；
- 不在有活跃任务时更新安装、替换 CLI/Skill 或重启 Runner；
- 私有手册与开发清单不得进入公开 main；
- 公开版本、tag 和 PyPI 文件不可覆盖或移动。

每个缺陷修复必须回答：根因属于哪一层、为何原边界未拦住、是否修在唯一责任模块、是否
删除重复逻辑、哪个回归能阻止复发。

## 11. 测试与发布门禁

### 11.1 开发期

```bash
PYTHONPATH=src python -m unittest <affected tests>
PYTHONPATH=src python -m ruff check src tests
PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache python -m compileall -q src
git diff --check
```

测试证据分层：

1. 纯合同与状态机；
2. Service/Store/Runner 事务和竞态；
3. fake Executor 与 fixture；
4. wheel/sdist、clean install、upgrade 与 failed-upgrade recovery；
5. 真实 Codex/Claude/Hermes canary。

mock 通过不能替代真实 CLI；真实任务成功也不能替代源码、包或发布矩阵。

### 11.2 发布期

发布必须分别通过：

- checkout 与公开树干净、内部文件排除；
- 支持的 Python 与目标架构矩阵；
- Ruff、compileall、shell syntax、Twine 与 manifest/checksum；
- clean install、upgrade、failed-upgrade recovery 和 CLI smoke；
- Runner/package/Skill identity 一致；
- 真实 Executor 关键路径；
- 不可变 tag、GitHub Release、Actions Trusted Publishing 和 PyPI 公网页面；
- GitHub/PyPI/manifest 资产 SHA-256 一致。

不要在手册记录某次测试总数；当前数量和失败项以实际 discovery、CI 与版本清单为准。

## 12. 常见故障定位

| 现象 | 首查 | 原则 |
| --- | --- | --- |
| accepted 后无执行 | Runner response、worker run、RunLease | accepted 不是 completed |
| Task List 变黄/橙 | progress temp、RunLease、input | 颜色不改变状态 |
| status 与进程矛盾 | task.json、RunLease、Runner identity | 重新派生公共视图 |
| customer path 被拒绝 | Path Plan、canonical root、Runner roots | 不复制工程规避权限 |
| safe 无法提交 linked worktree | `.git` containment、permission input | 不扩大 git common dir |
| permission 没弹窗 | strict marker、官方 session receipt、reason bound | 文本不能授权 |
| Hermes 没有 session ID | stderr receipt、transport、CLI 版本 | 不猜 ID、不新建 session |
| Claude 临时目录异常 | frozen project mode/path、Path Plan | 用户工程不 purge |
| 预算/turns 未生效 | config、task snapshot、argv、Runner audit | 更新只影响新任务 |
| cleanup 未执行 | terminal、lease、report、notification、capability | 失败不改任务终态 |
| Doctor 沙箱误报 | Runner storage probe 与 identity | 不信 Controller os.access |
| Skill 行为仍旧 | manifest/hash、setup update、Agent 会话 | 更新后重启对应会话 |
| Report 与真实进度不同 | callback、系统事件、progress receipt | 不从自然语言猜进度 |

## 13. 版本核心变更

本节只保留对当前架构仍有影响的核心变化，不记录开发过程。

### 1.0.1A：可信执行基线

- 严格 final callback、input_required/respond、RunLease 和终态竞态收口；
- `inherit|safe|full` 权限审计；
- dispatcher/executor 溯源、Doctor、构建身份和发布来源；
- Report、通知、Record 和 Task List 的事实边界明确化。

### 1.0.2A：资源、权限与执行会话治理

- Claude budget、Hermes max turns 和 session retention 配置；
- `SESSION-001` 完成三 Executor session receipt、retain/resume 和终态 cleanup 合同；
- 任务级资源/session 冻结、官方 session receipt 与精确 resume；
- 资源耗尽 approve 翻倍继续或 deny 失败；
- 三 Executor 一次性 `safe -> full` continuation；
- 终态 session cleanup、Claude project purge 安全边界和 cleanup receipt；
- Doctor v2、Runner storage probe、Skill canonical contract 与 manifest/hash；
- setup 默认权限 `inherit`、retention `false`；
- `task delete` 内置 `y/N` 确认并保护 customer project。

### 1.0.3A：剩余发布验收主线

- 权限设置、三 Executor approval/session、弹窗与 Claude 临时工程文件级能力已经收口；默认
  `inherit`、已有配置保留和 handoff 继承逻辑保持不变；
- `agentbc update` 的 manifest/hash 校验、`y/N`、受管 venv 原子切换、Runner/Skill identity 复验
  与内部失败恢复已实现；Alpha 不提供公开 rollback 命令；
- Homebrew Formula 生成与发布资产接线已实现；剩余真实 release 资产升级故障注入，以及 Apple
  Silicon/Intel 的安装、升级、卸载、services、PATH 和现有 PyPI/local bundle 迁移；
- 只补 RC 所需的可复现发布证据，不再扩大本版权限、progress、update 或 packaging 功能范围。

详细合同只在 `AGENTBC_1.0.3A_DEVELOPMENT_CHECKLIST.md` 维护，避免手册再次复制 Phase 计划。

### 1.0.4A：已确定延期项

- `FLOW-103-001`：资源耗尽与 final callback 的权威、单调 progress receipt；
- `PERM-104-001`：审批资格、通知类型、Deny 终态和 fallback eligibility 由 Core 机械判定；
- 建立三 Executor 完整、版本化的 version/help/argv/output/session/approval/resource fixture
  matrix 与未知能力组合 fail-closed probe（`PROTO-104-001`）；
- 在上述 fixtures 和 characterization tests 保护下进行局部重构，优先拆分 permission
  registry、approval transport、Doctor collectors、Runner IPC handlers 和 update service
  （`ARCH-104-001`）；
- 重构提交与状态机、schema、CLI 文案和权限语义变更严格分离。

### 中期方向

- OpenCode 正式 Executor 与公共 Executor contract；
- Docker amd64/arm64 profile，覆盖 macOS、Linux 和 Windows Docker Desktop；
- protocol v2 新任务写入与 v1 历史双读；
- GUI、通知中心、Webhook/Email 和签名安装包；
- GUI/通知始终复用 Core API，不成为第二状态源。

原生跨机派发、原生 Windows/Linux Runner、自动产物质量判断和删除 v1 reader 均不在当前
开发范围。

## 14. 开发前一分钟检查表

- 当前目录、分支和工作树是否正确且干净；
- Runner/package/Skill identity 是否一致；
- 是否有 active 或 input_required 任务；
- 需求的唯一责任模块和事实来源是否明确；
- 是否涉及状态、路径、权限、资源、session、删除或公共投影；
- 是否保留 unknown fields、旧任务读取和 fail-closed 行为；
- 是否会触碰 customer project、dispatcher conversation、外部 `.git` 或 secret；
- 是否已定义定向测试、相邻回归和真实 canary 边界；
- 是否把历史细节写回了版本清单/CHANGELOG，而不是继续膨胀本手册；
- 是否能用一句话解释回滚条件。

只要其中任何一项不清楚，先停止实现并补齐边界。
