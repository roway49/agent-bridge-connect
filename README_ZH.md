# Agent Bridge Connect

中文 | [English](README.md)

AgentBC 是一个本地优先的任务控制系统，用于协调本机 Agent 执行后台任务。
当前版本支持 Codex/ChatGPT、Claude Code 和 Hermes。它让不同 Agent CLI
共享统一的任务身份、Runner 网关、报告契约和恢复模型。

> Public Alpha：请在启用版本控制的开发项目中使用 AgentBC，并在接受改动前
> 审查 Agent 的输出。

当前版本：**1.0.1A**（Python 包版本为 `1.0.1a1`）。

- 仓库与版本发布：[GitHub](https://github.com/roway49/agent-bridge-connect)
- CLI：`agentbc`

## 为什么使用 AgentBC

- 通过统一 CLI 将任务派发给本机 Agent。
- 用 `4XMC-001 -> 4XMC-002` 这样的可见任务链持续迭代。
- 将产物直接写入用户工程，或写入隔离的托管工作区。
- 通过紧凑、自动管理的 Task List 观察并发任务。
- 将可读任务报告与有容量上限的运行时记录分离。
- 无需依赖聊天上下文即可关闭、恢复、改派或 handoff 任务。
- 接收简洁的 macOS 完成与恢复通知。
- 通过自然语言在任意 Agent 中完成任务收发，详情参阅
  [演示示例](docs/Example_ZH.md)。

![通过任务 ID 延续任务并 handoff](docs/assets/codex_handoff.gif)

## 如何创建任务

在任意 Agent 对话中调用 `/agentbc`，使用自然语言描述任务并指定执行者：

```text
/agentbc 让 Codex（或任意受支持的 Agent）写一个文档，总结 AgentBC 的功能和用途。
```

## 环境要求

- 当前桌面通知和 Task List 工作流需要 macOS；
- Python 3.10 或更高版本；
- 至少安装并登录一个执行器：Codex、Claude Code 或 Hermes。

## 部署并校验

一行命令即可完成下载、校验、安装和配置：
```bash
curl -fsSL \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A/install-agentbc-alpha.sh \
  | sh -s -- \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A
```

请先阅读[快速开始](docs/QUICK_START_ZH.md)，任务与 Runner 命令详见
[用户指南](docs/USER_GUIDE_ZH.md)。

## 架构

AgentBC 是一个本地控制平面。Agent 集成将结构化任务提交给单一的本地
Runner；Core 负责任务身份、状态、报告、记录和通知。

```mermaid
flowchart TD
    A[用户或控制 Agent] --> CLI[CLI 与已安装 Skill]
    CLI --> S[TaskService]
    S --> TS[TaskStore]
    CLI --> R[Runner 网关]
    R --> X[执行器适配器]
    X --> E[Codex、Claude Code 或 Hermes CLI]
    E --> P[用户工程或托管产物目录]
    R --> L[RunLease 与进度记录]
    S --> RP[Task Brief 与 Report]
    S --> I[全局任务索引]
    S --> N[Task List 与桌面通知]
```

### 组件边界

**CLI 与 Skills。** CLI 提供任务、Runner、setup、record、worker 和卸载操作。
安装后的 Skill 指导控制 Agent 选择 customer path、保留派发者身份，并在 create
与 handoff 之间做出选择。Skill 不能绕过 Core 校验，也不拥有任务事实。

**TaskService 与 TaskStore。** `TaskService` 负责状态流转、任务码分配、handoff
链路、关闭与恢复、报告终态化和索引刷新。`TaskStore` 负责紧凑运行时记录。
每次任务迭代都有容量上限，避免长时间任务无限增长元数据。

**Runner。** Runner 是正常派发的统一网关。它校验任务和路径计划，获取运行
租约，启动执行器，记录低频进度证据，并判断执行器退出状态。Runner 不判断
产物质量，质量验收仍由用户或审查者负责。

**执行器适配器。** 适配器将统一任务包转换为各执行器的参数和提示词。执行器
CLI 仍是独立进程。在能力允许时，Codex 和 Claude 会接收限定的可写目录；
Hermes 从指定工程或产物目录运行，并受其自身 CLI 能力约束。

**报告与记录。** 可读的 Task Brief、Report 与紧凑机器状态相互分离。报告描述
需求、链路、结果和产物位置；运行时记录保存精确状态和恢复证据。

## 任务与完成模型

四位 `TASKCODE` 标识一条任务链，数字后缀表示迭代次数：`4XMC-001` 和
`4XMC-002` 属于同一条链。命令可使用任务码解析当前 head，也可使用完整 ID
精确定位某次迭代。

Agent callback 仅作为可选元数据，正常情况下以执行器进程退出作为完成依据：

1. Runner 确认任务已启动。
2. 执行器 CLI 退出。
3. Runner 判断退出契约。
4. Core 写入终态并同步 Report。
5. Task List 与桌面通知展示相同状态。

- `completed`：执行已正常启动并结束，不代表产物质量已通过验收。
- `needs_recovery`：执行未能正常启动或继续。
- `failed`：执行已启动，但未能确认正常退出流程。

`accepted` 等派发响应不代表任务完成。任务状态、报告、产物和通知才是事实来源。

## 路径与数据模型

控制 Agent 只需提供明确的用户路径或字面值 `"default path"`，Runner 据此生成
路径计划。明确路径的产物直接写入用户工程；默认路径任务使用隔离的托管产物
目录。报告与运行时记录始终由 Core 管理。

```text
~/Documents/AgentBC/workspace/
|-- tasks/
|   |-- artifacts/YYYY-MM-DD/<TASKCODE>/
|   `-- report/YYYY-MM-DD/<TASKCODE>/
|       |-- <TASKCODE>-<NNN>-task.md
|       `-- <TASKCODE>-<NNN>-report.md
`-- record/
    |-- README.md
    |-- TASK_INDEX.md
    |-- task_index.jsonl
    `-- <TASKCODE>/<NNN>/
        |-- task.json
        |-- events.jsonl
        |-- interventions.jsonl
        |-- run_lease.json
        `-- 有容量限制的进度与运行日志
```

每次迭代的 record 上限为 10KB。`agentbc record clean` 会清理符合条件的终态
诊断记录，同时保留核心索引和状态。终态任务的空托管产物目录会自动删除；
用户工程永远不会成为自动清理或卸载目标。

## 本地安全模型

- Runner 只接受经过本地 token 认证的 spool 请求。
- 每套安装只允许一个 Runner 身份和稳定 PID；即使 spool 状态被替换，重复或
  孤儿 Runner 启动也会被拒绝。
- customer path 是明确任务输入，不会为了绕过权限而复制进托管工作区。
- 托管任务只获得当前任务的产物目录，而非整个 workspace 根目录。
- Report Markdown 由 Core 管理。
- 卸载和 task close 不会遍历用户工程路径。

AgentBC 不是容器沙箱。请结合版本控制、操作系统权限和执行器自身的审批机制
进行纵深防护。

## 文档

- [快速开始](docs/QUICK_START_ZH.md) / [Quick Start](docs/QUICK_START.md)
- [用户指南](docs/USER_GUIDE_ZH.md) / [User Guide](docs/USER_GUIDE.md)
- [功能展示](docs/FEATURE_SHOW_ZH.md) / [Feature Show](docs/FEATURE_SHOW.md)
- [演示示例](docs/Example_ZH.md) / [Examples](docs/Example.md)
- [后续功能预告](docs/PREVIEW_ZH.md) / [Feature Preview](docs/PREVIEW.md)

## 许可证

AgentBC 使用 [MIT License](LICENSE) 开源。
