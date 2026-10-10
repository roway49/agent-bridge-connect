# Windows Docker setup 人工操作说明

> 2026-10-10 状态索引：Mac Docker 已内测通过；Windows Codex/Claude/Hermes 已技术性通过；Linux 实际宿主与 Windows OpenCode 推迟至 1.06a。本文早期切片、待实现表和旧实例信息保留为历史，不代表当前状态；现行范围见[开发状态](../../docs/DEVELOPMENT_STATUS.md)与[验收矩阵](acceptance-matrix.csv)。E5 尚待收口。

2026-10-05。本机 Windows 验收入口已支持完整交互 setup；安装类型继续标记 `docker-preview`。此交付用于现有验收环境的配置部署，不等于全新离线安装、升级和卸载已经验收，也未解除正式 Windows 安装包保护。

## 一、启动 setup

保持 Docker Desktop 运行，并在 Windows 本机登录桌面。打开 PowerShell：

```powershell
& "$env:USERPROFILE\.local\bin\agentbc.cmd" setup
```

也可以双击 Documents\AgentBC\setup.cmd。所有 setup 提示为英文。完成后需要再次调整配置时，重跑同一命令即可，不需要重新安装。若存在未结任务，setup 会拒绝重建服务；先通过正常任务命令处理。

## 二、各项怎么选

| 提示/阶段 | 本轮建议 |
|---|---|
| Workspace / Reports / Task files | 确认显示 Documents\AgentBC\workspace 下的目录 |
| Permission mode | 回车保留当前 inherit；不要为跳过审批改为 full |
| Review Codex CLI connection settings? | 保留已有连接。Codex 的独立执行缺陷仍另行处理 |
| Review Hermes Agent connection settings? | Y，使用已有 Windows Docker Hermes 连接 |
| Replace existing Docker settings with detected host settings? | 回车/N，保留已验证的 DeepSeek 配置；仅需重新导入原生配置时选 Y |
| Hermes max turns | 回车保留当前值 |
| Retain executor temporary sessions? | 按需要选择，默认保留当前配置 |
| Task list style: 1) Terminal 2) WebUI | 2 |
| Fixed WebUI port ... Assign a new port? | N，当前端口 50757 |
| Keep WebUI running in the background? | Y；选 N 时可以用 webui start 手动启动 |
| Install ... AgentBC Skill? | Y；view 可先查看内容。修改过的 skill 不会被强制覆盖 |

当前镜像包含 Codex、Claude、Hermes；OpenCode 会明确提示 runtime 未包含，不能把探测到宿主 CLI 当作容器 Executor 已就绪。Skill 是 Dispatcher 的派发入口，不等于对应 Executor 已通过运行验收。

已有连接默认不被覆盖，不修改原始 Windows 配置，不自动改变模型，不在 setup 发起模型请求。用户选择重新导入后，才复制支持的设置和凭据，并列出不能迁移的宿主集成。

## 三、默认工程目录

此台机器为 `C:\Users\developer\Documents\AgentBC\workspace`。部署工具使用 Windows Documents 已知目录，支持重定向后的实际位置。

- `record`：任务内部记录，不要手工编辑。
- `tasks\report`：任务说明及报告。
- `tasks\artifacts`：默认任务产物。

整个工作区映射到容器 `/data/workspace`；WebUI 使用同一目录的只读挂载。新任务仍使用 `--customer-path "default path"`，由 Core 分配各任务目录，Windows 路径不直接当作容器路径。

现有 72 个文件已复制并通过内容 SHA256 清单校验。原 Docker data 卷保留作回退；不要同时启动旧容器。宿主程序及凭据仍在原受管位置，不放进任务目录。此 preview 的 setup 不扩大工程挂载范围；其他项目目录需后续明确部署，不要改 Compose 来绕过路径校验。

## 四、完成后验证

```powershell
$abc = "$env:USERPROFILE\.local\bin\agentbc.cmd"
& $abc setup --show
& $abc runner status
& $abc doctor
& $abc task list
& $abc task status 38K8-001
& $abc webui start
```

预期：setup complete；Runner 和桌面桥 ready；38K8-001 仍 completed；WebUI 打开并显示历史任务。请使用 start 输出的带认证链接，而非只输入裸地址。

```powershell
& $abc webui close
& $abc webui status
& $abc webui start
```

预期端口不变，close 不取消任务或关闭审批桥。N 不启动常驻页面；Y 开启后台页面。登录及 Docker 恢复已接线，但锁屏/注销/Docker 重启人工矩阵仍需单独验证。

`agentbc` 的目录已登记到用户 PATH；旧终端未刷新时继续使用上面的完整路径即可，不需要修改执行策略或以管理员运行。

## 五、失败与恢复

setup 在写入前收集选择并校验配置、容器/daemon/Compose 归属、skill 完整性及任务是否结束；与派发互斥。失败会尝试恢复配置、Compose、skill 及原 WebUI 运行状态。私有备份位于宿主安装的 `host\setup-backup-*`，其中可能包含凭据，不要上传或分享。

如出现异常，保留错误文字，不要删除 workspace 或手工改任务回执。正式安装/更新/卸载仍按 Windows 安装主线推进；这个配置入口不代表整套发行安装已通过。

## 2026-10-06 引导反馈修正

Docker setup 不再显示 Native 的 enabled executors / detected but not enabled、长搜索路径和能力参数。新表分别列出宿主 CLI、Docker runtime、Connection；宿主未检测到 CLI 不等于容器无法使用已保存设置。已有连接使用 Review connection settings，重新导入才展示被排除的宿主集成。完成摘要只报告当前配置操作，不把历史任务成功作为本次连接测试。Skill 安装明确说明 AI runtime unchanged；未包含的运行时统一显示 unavailable as a Docker executor。

Windows 已在 setup 互斥锁保护下更新宿主组件，无配置/服务变更。26 项安装与引导定向测试、构建、Ruff、测试结构及 diff 检查通过。宿主 wheel SHA256：887c4379017beca3465832020eadf0b0254c90ea0e8e9b7abeed3d408a5d761b。


## 2026-10-06 Claude 运行时接入

Claude CLI 2.1.247 与 claude-agent-sdk 0.2.142 已加入 Windows Linux amd64 镜像，按仓库锁校验公开资产和依赖。独立 Claude 配置卷只挂载 Runner；从 Windows 原 settings.json / .claude.json 复制可迁移配置，保留 API provider/model/权限，排除 host project settings 和 mcpServers，源文件 hash 不变。setup 现显示 Claude runtime Included / Saved settings；OpenCode 仍未包含。

Runner 实际 SDK Python 启动参数与 SDK 导入已验证，允许 claude/codex/hermes；桌面桥 ready、WebUI 与旧任务报告可读。没有发送模型请求，尚不构成 Claude 真实审批/执行/callback 验收。镜像身份和机器证据见 win03-evidence/windows-claude-20261006.json。原配置、Compose 及镜像保留作回退，Mac 未修改。

## 2026-10-06 范围调整：Windows OpenCode 闭环延期至 1.06

老板明确要求：Windows Codex 会话清理继续修复；OpenCode 的 Windows 闭环移至 **1.06**（跟踪项 `EXEC-106-WINDOWS-OPENCODE`）。包含 Windows Docker OpenCode 运行时、配置继承与派发—执行—最终 callback—Core 报告—RunLease 关闭的真实验收。

本条覆盖此前“Windows 四 Executor 全部通过才关闭 1.05a Windows”的范围要求：1.05a Windows Executor 验收范围为 Codex、Claude、Hermes；OpenCode 格登记延期，不能标通过。Windows 正式安装、三 Executor 同候选、桌面恢复、WebUI、更新/卸载及会话清理门禁仍有效。Mac 已通过的 OpenCode 证据保留；Linux 范围不变。OpenCode 原生审批仍为独立的 1.06a 待办，不因本次延期自动实现。

## 2026-10-07 Windows WebUI 固定端口修复通过

原固定端口50757位于Windows重启后的TCP排除范围50753–50852，Docker bind access denied。经老板授权修复，显式重配置为 **36124**，同步Compose端口/public-origin、宿主登记及容器配置；非自动换端口。未修改Windows排除范围、防火墙、Docker系统配置或其他进程。

Windows setup新分配端口从18000–47999中进行有界候选探测，跳过权限拒绝和占用端口，优先避开默认动态范围；这不是长期占用承诺，仍可能受后续系统/程序变化影响。start保持固定端口，Windows冲突提示端口号、保留/访问拒绝原因及setup重新分配入口；停止服务状态不再误显示health=starting。现有合法端口不会被普通start或重复setup自动更换。

Windows已安装宿主wheel SHA256 a9494984fb054026c08fda3e03037aa6f9b4e928df4b526d7ea10d7ba2241607。Runner容器和镜像保持不变；WebUI重建并更新绑定，healthy、固定36124，桌面桥ready。真实页面、列表、DKRV-001详情和报告均HTTP200；重复start、重复close、再次start通过。使用`agentbc webui start`打开带鉴权链接，裸地址http://127.0.0.1:36124/不是免鉴权入口。

定向100项、四层全量2311 tests / 174.241s / OK；前端18项、类型检查/构建、CI固定Ruff0.15.22、75文件39842行结构门禁、仓库边界及diff通过。证据：deploy/docker/win03-evidence/webui-port-repair-20261007.json。

本轮仅关闭当前WebUI端口启动故障与停止状态误报；Windows登录自动恢复、停止Runner的启动入口、WebUI恢复失败与审批桥隔离、doctor完整健康汇总仍待收口。新端口跨重启人工验证仍待完成，不能据本次启停宣称重启恢复通过。未提交、未替换Mac安装。
