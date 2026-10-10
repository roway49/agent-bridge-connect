# Windows Docker 正式安装候选：安装与卸载验收

状态：开发候选；2026-10-08 已通过 Windows 实机安装→setup→卸载→重装往返验证，2026-10-09 正式安装候选的 Codex/Claude/Hermes 业务闭环已技术性通过；此前跨重启恢复证据单独保留。不同镜像上的结果不能代替最终 E5 候选回归或完整更新矩阵。Windows OpenCode 闭环归属 1.06a。

## 安装前

使用普通用户 PowerShell；需要现有 Python 3.10+、Docker Desktop Linux 容器和 Compose。无需宿主 Node。先解压完整离线 ZIP，核对发布时提供的 ZIP SHA256，进入解压目录。不得把旧 preview 的配置目录或任务记录复制成新安装目录。

```powershell
Get-FileHash .\agentbc-*-docker-windows-amd64.zip -Algorithm SHA256
cd <解压后的包目录>
py -3 .\install_docker.py
```

旧 preview/Native 仍在时，此安装必须明确失败，且旧版 CLI、任务和 WebUI 仍可用。默认 Python 入口不修改 PowerShell 执行策略。若没有 py launcher，请使用 Python 3.10+ 解释器完整路径运行相同文件。包内也保留 .ps1 入口，供已允许本地脚本的环境使用。无需管理员权限。

## 从当前 Windows preview 切换

先用现有 `agentbc task list` 确认无未结任务；不要删除记录来绕过检查。以下操作会停止旧 preview 服务并撤销其 CLI/skills，需人工明确确认。它不会自动继续安装新版本。

```powershell
py -3 .\recover_docker.py uninstall-preview --profile 'D:\agentbc-tests\win-flow-20261001\host\docker-host.json'
```

工具先校验容器、Docker daemon、入口和备份，然后保留旧工作区为同级 `workspace-preview-backup-<时间>`，配置及 skill 备份位于用户 `.agentbc-install\backups`。保留 Docker、镜像缓存及旧凭据卷，不影响其他 Docker 项目。失败时保留现场，使用同一恢复命令继续；身份或备份不一致时必须先检查原因。

随后独立执行：

```powershell
py -3 .\install_docker.py
```

## setup 指引

1. 工作区默认 `Documents\AgentBC\workspace`，可以输入自选绝对路径。首次安装使用空目录；若目录已有数据，setup 提供明确选项：1) 将旧目录改名留档并创建新工作区；2) 选择其他目录；3) 取消。默认选 2，不自动移动文件。选择 1 前会检查未结任务，旧历史只供查阅，不能恢复执行。
2. 探测本机 AI 配置并逐项确认复制；沿用 Windows 已有凭据，不需要复制 Mac 凭据。导入不代表连接测试通过。
3. 选择权限、任务监控方式和固定端口。首次 WebUI 默认开启后台；关闭窗口不取消任务。
4. 确认安装所需宿主 skills。配置、CLI、skills 和容器属于同一安装身份。
5. 打开新终端执行下列命令；如果 PATH 未包含入口目录，可使用 `$env:USERPROFILE\.local\bin\agentbc.cmd` 的完整路径。

```powershell
agentbc doctor
agentbc runner status
agentbc task list
agentbc webui start
agentbc webui status
agentbc webui close
```

保留 `doctor` 中的安装 ID、镜像 ID、版本及包校验值。分享日志时不要包含 WebUI 私有链接 token 或凭据。

## 人工验收

- 在旧安装存在时安装失败，原服务不受影响。
- 按上述顺序卸载 preview、干净安装；默认工作区、英文 setup、配置导入、skills 和 PATH 符合预期。
- 从真实 Dispatcher 分别派发 Codex、Claude、Hermes 最小任务；核对人工审批、执行产物、最终 callback、Core 报告、完成通知、租约关闭与官方会话清理。
- WebUI 分类、报告链接、固定端口、关闭/启动及断线恢复正常。
- 关闭终端、重启 Docker、注销/登录后核对单实例恢复。显式 close 不应在当前登录期间立刻复活。
- 有未结任务时卸载必须拒绝；任务正常结束后再卸载。

## 卸载与恢复

```powershell
agentbc uninstall
```

默认保留任务和报告，输出备份位置；只移除本安装的容器、登录任务、CLI 和受管 skills。原始宿主 AI 配置不改动。需要保留 Docker 导入的凭据备份时，先查看 `agentbc uninstall --help` 的明确选项。

CLI 损坏时，从原候选包使用：

```powershell
py -3 .\recover_docker.py status
py -3 .\recover_docker.py uninstall
```

再次安装时，在 setup 明确选择“保留旧目录为归档并创建新工作区”，或选择另一个空目录；也可以先人工改名留档。仅剩任务备份或镜像缓存不构成安装冲突；部分卸载和身份不明仍须恢复处理。跨类型不提供强制覆盖。

正式安装/卸载及重启验收完成前，不标记“Windows 可完整替代 Native”；同类型更新与 Linux 安装须分别验收。

仅在旧探针仍存在时处理：早期 `AgentBC-WIN03-Probe-20261001` 为无触发器、未运行的 UI 探针，正式安装前应在任务计划程序导出备份后删除该精确任务。不要删除当前运行的 `AgentBC-Desktop-preview-*`；它由 preview 卸载器管理。探针清理脚本被执行策略阻止，本轮没有降低策略或自动绕过执行。
