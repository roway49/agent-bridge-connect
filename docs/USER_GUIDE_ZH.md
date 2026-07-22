# 用户指南

中文 | [English](USER_GUIDE.md)

## 命令结构

使用 `agentbc <group> <command> --help` 查看当前安装版本的准确选项。

- `agentbc setup`：发现执行器并安装本地集成。
- `agentbc uninstall`：卸载 AgentBC，并分别选择是否删除托管数据。
- `agentbc init`：初始化托管运行记录目录。
- `agentbc record clean`：清理符合条件的运行时诊断记录。
- `agentbc task`：创建、检查、handoff、干预、关闭和恢复任务。
- `agentbc worker`：执行 task board worker 操作。
- `agentbc runner`：启动、停止、检查、采样和显示 Runner 工作。

setup 会在后台启动 Runner。恢复时使用 `runner start`，停止时使用
`runner stop`；前台 `runner serve` 仅用于调试。

## 执行器

AgentBC Public Alpha 支持 Codex、Claude Code 和 Hermes。setup 会检查已配置
可执行文件、当前 `PATH`、用户安装位置以及受支持的编辑器扩展运行时：

```bash
agentbc setup
agentbc setup --show
agentbc runner status
```

Agent 集成会明确传递派发者身份：

```bash
agentbc task create ... --source-platform codex --dispatch
agentbc task handoff 4XMC --to hermes --source-platform claude --dispatch
```

直接从终端调用时可省略该字段，此时派发者记录为 `cli`。setup 后请重启 Agent
客户端，使其重新加载已安装的 Skill。执行器模型选择暂不属于稳定的 Alpha 契约。

## Create 与 Handoff

独立任务使用 create：

```bash
agentbc task create \
  --title "Add CSV export" \
  --assignee codex \
  --steps ./steps.yaml \
  --customer-path /path/to/project \
  --dispatch
```

当工作依赖、审查或修改已有 AgentBC 产物时，必须使用 handoff。若将已有托管
产物目录重新传给 create，Core 会返回 `handoff_required` 并给出当前 head 的
建议命令。

## 图片任务

Codex 支持多张原生图片输入；Hermes 当前每轮任务支持一张。若用户没有另行指定
工程路径，图片本身的路径同时作为 `--customer-path`：

```bash
agentbc task create \
  --title "分析并修改这张设计图" \
  --assignee codex \
  --steps ./steps.yaml \
  --customer-path /absolute/path/design.png \
  --image /absolute/path/design.png \
  --dispatch
```

Codex 多图任务可重复传入 `--image`。handoff 默认继承上一轮图片引用；显式提供
新的 `--image` 时替换为新输入。生图和改图使用执行器自身的原生能力，最终位图
必须落在任务 artifact root；模型服务与鉴权仍由执行器自身负责。

## 状态与报告

```bash
agentbc task status 4XMC
agentbc task report 4XMC
agentbc task logs 4XMC
```

- `completed`：执行正常启动并结束，不代表质量通过验收。
- `needs_recovery`：执行未能正常启动或继续。
- `failed`：执行已启动，但未能确认执行器正常退出。

Agent callback 是可选元数据，Runner 观察到的执行器退出才是正常完成依据。

## Task List 与健康状态

```bash
agentbc task list
agentbc runner show
```

Task List 跟踪当前派发批次。计时器仅表示显示界面仍在刷新，不会轮询执行器状态。

- 绿色：近期存在进度证据；
- 黄色：Runner 正常，但至少五分钟没有进度；
- 橙色：Runner 正常，但至少十分钟没有进度；
- 红色：需要恢复或已终态失败；
- 灰色：已排队，等待启动。

AgentBC 只观察未响应任务，不会自动取消，因为强制终止可能让用户工程处于部分
修改状态。

## 任务干预

```bash
agentbc task pause 4XMC
agentbc task resume 4XMC
agentbc task close 4XMC
agentbc task recover 4XMC
```

close 只针对当前排队中或活跃的 head。关闭根任务会释放任务码并删除 AgentBC 自有文件；
关闭后续 chain 迭代会保留历史，并提示工程改动无法回滚。用户工程文件永远不会
被 AgentBC 删除。

## Record 与进程压力

```bash
agentbc record clean --dry-run
agentbc record clean
agentbc runner process-sample
```

record clean 会保留全局索引、权威状态、可读报告和产物。请根据执行器负载与机器
性能选择并发数量。

## 卸载

```bash
agentbc uninstall
```

卸载流程会分别询问是否清理任务记录与报告、是否清理默认工作区产物。用户工程
路径不会成为卸载目标。CLI 损坏时，可使用 Release 包中的独立脚本
`uninstall-agentbc-alpha.sh`。

## 故障排查

### 找不到命令

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v agentbc
```

### Runner 不可用

```bash
agentbc runner start
agentbc runner status
```

如果仍然超时，请检查 `~/.abc/runner/runner.log`。

### 未发现执行器或 Skill

先验证执行器自身 CLI 和登录状态，再重新运行 setup，并启动新的 Agent 会话。
Hermes Skill 不一定以 slash command 的形式展示。

### customer path 被拒绝

直接传递用户指定的文件或目录。不要读取 Runner allowed roots，也不要将工程复制
到 AgentBC workspace。如果 Core 返回 `handoff_required`，请继续已有任务链。

### 任务变为黄色或橙色

检查日志和执行器界面，排查网络、配额、权限或长时间推理。进度恢复后健康状态会
重新变绿。

### 已生成产物但任务状态为 failed

`failed` 描述的是退出契约，不表示一定没有产物。请检查报告、日志和产物，再决定
恢复任务还是 handoff 给其他执行器。

数据归属与内部边界详见 [README 架构章节](../README_ZH.md#架构)。
