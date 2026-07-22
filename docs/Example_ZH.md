# 演示示例

中文 | [English](Example.md)

本页通过真实操作录屏展示 AgentBC `1.0.1A` 的典型协作流程。这里重点呈现
实际使用体验；完整命令与行为说明请参阅[功能展示](FEATURE_SHOW_ZH.md)和
[用户指南](USER_GUIDE_ZH.md)。

## 1. 用自然语言并发派发任务

在一次对话中描述多个任务及其执行者，AgentBC 会为每项工作创建独立任务并交由
Runner 并发执行。派发完成后，控制 Agent 无需在当前会话中持续轮询或陪跑。

![通过 AgentBC 并发派发多个任务](assets/multidispatch.gif)

## 2. 并行启动多个同类 Executor

同一轮派发可以把多项独立工作交给多个相同类型的 Executor。每项任务拥有独立的
任务 ID、运行状态和报告，适合批量处理互不依赖的工作。

![从 Hermes 并行派发任务给多个 Codex Executor](assets/hermes2codex.gif)

## 3. 由一个 Agent 规划，另一个 Agent 执行

控制 Agent 可以先把需求整理为结构化任务，再明确交给另一个 Agent 执行。规划、
执行和结果报告被记录在同一套任务协议中，减少跨 Agent 转述造成的信息损失。

![在 Codex 中规划并派发 AgentBC 任务](assets/codex_plan.gif)

## 4. 只凭任务 ID 继续协作

新的 Agent 或新的会话不需要继承原聊天上下文。通过任务 ID 即可定位任务简报、
报告和产物，并使用 handoff 在同一任务链中继续下一轮工作。

![通过任务 ID 和 handoff 继续已有任务](assets/codex_handoff.gif)

## 5. 查看状态、取消任务并接收通知

Task List 集中展示当前批次的任务状态。用户可以随时关闭仍在运行或等待启动的
任务；任务进入完成或恢复状态时，AgentBC 会发送简洁的桌面通知。

![查看并关闭活跃的 AgentBC 任务](assets/task_cancel.gif)

## 6. 集中管理产物、报告和运行记录

默认工作区将托管产物、任务报告和紧凑运行记录分开保存。用户和 Agent 可以通过
任务 ID 快速定位结果，同时避免把 AgentBC 运行文件混入用户工程。

![查看 AgentBC 任务产物和报告](assets/artifacts.gif)

