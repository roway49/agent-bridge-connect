# PERM-104-002-R3 实施说明

## 状态

- `RAXT-001` 保持失败历史：没有最终 callback、没有本地提交，不能改写为完成。
- `CMF2-001` 是基于同一工作树的续作尝试；它在旧安装包中复现 contained worker 仍写全局索引，并因终态无法写回而卡死。2026-08-31 已由 controller 明确取消，RunLease 关闭、临时 full grant 撤销、官方 Codex Executor 会话完成 archive 后 delete。
- 本文件记录 controller 直接接管后的源码结果。`PERM-104-002` 仍为开放的 P0-Blocker，部署后三来源 full canary 与 `PERM-104-002-R1` 尚未执行。
- `SESSION-104-001` 不受本轮修改影响，保持既有通过结论。

## 失败前状态机

1. Runner 启动 contained worker，但安装包内的 worker `TaskService.start_task_run()` 仍刷新 board 全局 `task_index.jsonl`/`TASK_INDEX.md`。
2. Seatbelt 只允许 task-scoped record/report/control/temp 与项目路径，因此全局 index 写入返回 `Operation not permitted`。
3. Codex App Server 已提供原生 `requestApproval`，但 Prompt 仍同时告诉模型用 `requested_permission: full` 表示访问阻塞，终态路由也未声明原生审批权威。
4. 模型 callback 由兼容路径创建 full input；Approve 后签发一次性 `safe -> full` grant，新的 contained worker 再次在全局 index 写入处失败。
5. worker 无法持久化终态，Core 留下 `running/input_required` 与 suspended RunLease；RAXT 的 7700 个 App Server 事件又使 task record 超过 10 KiB。

## 修复后状态机

1. Runner 启动 worker 时注入 `_runner_worker=True`；worker 可写自己的 task record/event/progress/control/temp/report，但 `TaskService` 初始化、claim/start、terminal report、通知与任何 `_refresh_task_index()` 均不触碰 board 全局索引。
2. Runner 在 containment 外负责 worker dispatch/exit 后的全局 index refresh、authoritative recovery、grant/runtime receipt 阻断、stale input 失效和 worker 引用清理。
3. Claude Runner-managed 任务统一进入已探针确认的 `claude-agent-sdk==0.2.142` 控制路径。官方 preallocated/resumed session receipt 必须在 prompt/options/hooks/query 之前通过 `record_session_started()` 的 task/run/session/resumed/source 校验。
4. 只有 SDK `can_use_tool` 或 Codex App Server `requestApproval` 的结构化事件能创建原生 `single_action`；Approve 在相同进程/会话返回原 input，Deny 零执行。callback、stderr、退出码、普通 access 文本与 `requested_permission: full` 不具授权能力。
5. 原生 transport 的模型 callback 若请求 permission/full，路由为 `needs_recovery/native_permission_callback_ignored`，不会创建 input、grant、worker 或 continuation。相同 fingerprint/domain/profile 在 Approve 后再次阻塞收敛为 `permission_escalation_ineffective`。
6. 终态高容量 `control_events` 只投影总数、最多八种事件类型、首事件、最后一次 approval 与最后事件；完整流仍保留在 task-scoped control storage。若记录仍接近上限，只进一步压缩冗余 event/intervention/run-log 尾部，不删权威 task/session/permission/callback 状态。

## 变更范围

- Claude SDK 与权限权威：`claude_sdk_transport.py`、`control.py`、`execution_contract.py`、`executors/claude.py`、`prompt_contract.py`。
- Codex 自举修复：`executors/codex.py` 将 App Server 原生审批设为唯一权限权威，并移除 native prompt 中的 full callback 矛盾。
- Runner/worker 生命周期：`cli.py`、`runner.py`、`service.py`、`run_lease.py`、`notifications.py`。
- 记录、报告与清单：`record_management.py`、`reports.py`、`AGENTBC_1.0.4A_DEVELOPMENT_CHECKLIST.md`。
- 回归：`tests/test_perm104_002_r3.py`。

## 验证证据（controller 接管）

- 定向测试：53 项通过，覆盖 R3、production routing、SDK transport、record compaction 与既有终态增长回归。
- 完整 unittest：1673 项；当前 Codex 外层嵌套沙箱中 1669 通过，4 项因 `/usr/bin/sandbox-exec` 返回 `sandbox_apply: Operation not permitted` 失败。
- 将完全相同的 4 项用例放到受支持的非嵌套宿主路径重跑：4/4 通过；未关闭、绕过或放宽产品 Seatbelt。
- 离线封包：`uv build --offline` 成功生成 `agentbc-1.0.3a2.tar.gz` 与 `agentbc-1.0.3a2-py3-none-any.whl`；wheel metadata 为 `Name: agentbc`、`Version: 1.0.3a2`、`Requires-Python: >=3.10`、Claude extra 精确依赖 `claude-agent-sdk==0.2.142`。
- 仍需在最终提交前执行全量 Ruff、compileall、`git diff --check`、commit 与 clean-tree 检查。

## 部署后真机门禁

1. Claude `inherit` 原生单动作：只出现一个带 request/fingerprint/details 的审批；Approve 后相同 SDK session 完成，Deny 零执行。
2. explicit full、可信临时 full、inherited full：分别完成同类项目写入和 linked-worktree commit；不出现二次 full 请求，runtime receipt 最终达到 structured PostToolUse `verified`。
3. 否定边界：重复 fingerprint/domain/profile、transport death、timeout、Runner crash/restart、其他 worktree/ref 与未声明父目录全部 fail closed，零重复 input/grant/worker/continuation。
4. `PERM-104-002-R1`：在权限闭环通过后回归 View Details、Approve、Deny；不得用 UI 成功替代权限能力生效证据。

## 风险与未完成项

- 当前源码尚未合入 `private/integration`，本机已安装 Runner 仍是旧构建；在重新封包替换并重启 Runner 前，AgentBC 自派发代码任务仍可能复现全局 index 写入失败。
- SDK live allow/deny 探针已有 2.1.233 + SDK 0.2.142 历史证据，但本次 controller 接管没有把它等同于部署后的 AgentBC 端到端 canary。
- `PERM-104-002` 在三来源 full、原生 Approve/Deny 及 R1 详情回归全部通过前不得关闭。
