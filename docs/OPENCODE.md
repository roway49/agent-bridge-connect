# OpenCode 基础执行接入（1.0.5A 开发中）

本页对应 EXEC-105-001 与 EXEC-105-002-A。开发工作树已接通正式 CLI → Runner → Worker →
Registry → OpenCode Adapter；已发布的 1.0.4A 安装不因此自动获得此能力。
受控 fake 证明机械路由；2026-09-23 已用 OpenCode 1.18.31 在 macOS 原生环境验证
真实多步任务、最终 callback 和 Core 报告。Mac Docker 基础闭环已完成内测（2026-09-30）；Windows OpenCode 和 Linux 实际宿主验收推迟至 1.06a。最终发行统一候选仍需 E5 回归。

## 能力边界

| 事实 | 说明 |
| --- | --- |
| `found` | discovery 找到可执行文件；不证明认证或模型可用 |
| `enabled` | 已写入 AgentBC executor 配置 |
| `supported_executor=true`、`basic_execution=true` | 支持新建任务的基础派发与执行，L0 |
| `full_lifecycle=false` | 未认证完整生命周期；不支持 resume、审批续行、retry、handoff、reassign 或原生 session 自动清理 |
| `capabilities.cancel=false` | 不提供 Adapter 原生取消能力；不能把进程退出当成原生 session 已清理 |

缺少 completion callback 或终止协议不完整时，产物存在也不等于 Task completed。
不支持的生命周期操作返回 `opencode_action_unsupported`，不会静默新建 session。
现有 Codex、Claude、Hermes 的审批与生命周期合同保持原状。

最小流程使用启动时选定的权限，执行中不切换。OpenCode 在最终响应中输出严格 v1 callback，
一次提交全部声明步骤的结果及证据摘要；Core 验证后更新步骤、生成任务报告。
本 transport 不要求调用 `agentbc task progress`：运行中只有 RunLease 心跳，
不提供逐步完成回执，也不伪造 `agentbc.session`。最终流中的原生 sessionID 仅作执行诊断，
不能当作首轮前绑定或自动清理授权。缺 callback、工具拒绝及异常退出仍按失败报告。
原生审批、同会话续行及提权属于后续 P2，不是本版最小流程的承诺。

## 配置与发现

先在 OpenCode 自身完成模型和认证配置，AgentBC 不保存或代填 provider 凭据。
使用此开发版本执行 `agentbc setup --show` 查看 discovery；`agentbc setup`
可以启用已发现的 OpenCode。可通过 `AGENTBC_OPENCODE_BIN=/absolute/path/opencode`
指定路径。Runner 与 Worker 必须使用同一配置和可执行文件；路径变更后重启 Runner。

可在既有 `~/.abc/config.toml` 中加入：

```toml
[executors.opencode]
command = "/absolute/path/opencode"
model = "provider/model"
timeout_s = 600
```

三项均可省略：command 走标准发现，model 使用 OpenCode 自身默认，timeout_s 默认600秒。
无可用命令时 preflight/Runner/Worker 返回缺失或不可用诊断；无模型/认证时保留原生失败，
不换用其他 Executor。仅支持 `cli-json` 固定 transport，不接受额外 transport 或权限配置键。

权限模式支持 `inherit` 和 `full`：

- `inherit` 不改变 OpenCode 原生权限，遇到需要交互的拒绝可能失败；当前不能接审批续行。
- `full` 仅在用户明确选择时使用。它固定 build agent，读取官方规则并建立子进程范围的权限覆盖。
- `safe` 返回 `opencode_permission_mode_unsupported`，不会自动转成 inherit/full。

setup 不为 OpenCode 改写全局权限默认值，也不自动替用户选择 full。

## 正式入口

写入步骤文件，例如 `/tmp/opencode-steps.yaml`：

```yaml
steps:
  - id: 1
    description: "创建 hello.txt，内容为 UTF-8 的 hello 加一个换行，并回读核对。"
```

确认 Runner 健康且包含 `opencode` 后派发：

```sh
agentbc runner status
agentbc task create --title "OpenCode 基础执行" --assignee opencode \
  --steps /tmp/opencode-steps.yaml --customer-path /absolute/project \
  --source-platform codex --dispatch
```

以上命令使用已配置的权限模式；仅在用户明确要求覆盖时添加 `--permission-mode inherit`
或 `--permission-mode full`。也可以先创建，再执行 `agentbc task preflight <TASK_ID>` 与
`agentbc task dispatch <TASK_ID>`。控制器来自 Claude/Hermes 时，source-platform 使用对应身份。

用 `agentbc task status <TASK_ID> --json`、`agentbc task logs <TASK_ID>` 和
`agentbc task report <TASK_ID>` 检查真实结果，并回读产物。accepted 只证明派发被接受。
Task/run、cwd、任务文本、模型配置和进程证据必须一致；此阶段不承诺原生 session 清理。

## 开发验证

`tests/integration/test_executor_runtime.py` 中的 OpenCode 路由测试使用临时 Runner IPC、
真实 Worker 子进程和受控 CLI fake，覆盖创建派发、单独派发、inherit/full、无可选配置、
启动/执行失败和不支持动作。仅替换通知 UI；不 mock Registry、Adapter、TaskService、
进程执行、callback 或 RunLease。所有测试记录均位于临时目录。
