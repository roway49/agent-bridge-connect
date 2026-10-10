# Windows Docker 最小收发闭环：通过

2026-10-01，win-desktop。限定：隔离 Codex 文字任务；不是 Windows 正式安装、四 Executor 或桌面通知验收。

## 真实结果

- Task：DPZE-001，Executor：Codex 0.153.4，权限 inherit，未覆盖原生权限。
- Windows 宿主已安装的隔离 agentbc.cmd → Windows Docker Linux amd64 Runner → Codex App Server → 模型实际回执 → Core callback/报告 → closed lease。
- 官方绑定会话：01a0f81a-ada6-74e0-8456-a780adfcfc13。
- 模型实际输出 WINDOWS_DOCKER_MIN_FLOW_OK，工具调用 0；有效 AGENTBC_FINAL_CALLBACK，1/1 步完成，exit_code=0，状态 completed。
- Core 报告已生成；report、record、index、file_notification 均 succeeded；RunLease closed。墙钟 38.574 秒。执行时长仍显示 unknown，不用墙钟冒充执行时长。
- 本机 Windows 的 Codex config/auth 私有复制到 Windows Docker 专属卷；原文件校验未变。未复制 Mac 凭据。排除不可移植的 MCP、plugins、notify，不改变权限模式或模型/provider。

## 尚未通过的部分

- ui_notification=pending：本轮显式 CLI 审批模式，没有 Windows 桌面桥；没有伪造弹窗成功。
- session cleanup=not_requested，phase=waiting_for_cleanup；有官方 session 绑定和 archive acknowledgement，但不能宣称删除清理完成，更不能归档其他 Desktop 会话。
- 无工具文字任务没有触发审批；工具执行、Windows 项目 bind mount、真实人工审批、WebUI、更新卸载、其他三个 Executor 仍待验。
- 正式 Windows 安装入口的未验收保护未移除；本次为显式隔离 preview，不修改系统 PATH 或用户现有 skills。Mac 正式安装未替换。

## 环境与入口

- 隔离目录：D:\agentbc-tests\win-flow-20261001。
- 宿主入口：D:\agentbc-tests\win-flow-20261001\host\bin\agentbc.cmd。
- 容器：agentbc-win-flow-20261001，ID 0fe4c5fbc963170b86a1313727b4d4ea02cc4a1314ef1635f7ac623a4d4f4f33。
- 镜像：sha256:0a432837e7d9dd58ba00d78c8291ad5a1e0051e098bdf2c098bf7c4345479f0c；Linux amd64，UID/GID 1000，cap_drop ALL，no-new-privileges 和已验证 Codex seccomp。
- 独立命名卷保存工作记录及容器内用户配置；没有宿主项目挂载、Docker socket 或 privileged。
- 容器及记录保留供后续联验，未配置登录恢复。

Windows PowerShell 查看：

```powershell
$env:PATH = 'I:\docker\app\resources\bin;' + $env:PATH
& 'D:\agentbc-tests\win-flow-20261001\host\bin\agentbc.cmd' task status DPZE-001 --json
& 'D:\agentbc-tests\win-flow-20261001\host\bin\agentbc.cmd' task report DPZE-001
```

## 本轮代码及验证

隔离 docker_host preview 增加显式 --approval-delivery cli，默认 desktop 不变；Windows 尚无桌面支持时要求显式选择，不静默退化。CLI 模式仍经过原 Core 权限及 respond 合同，不自动批准。preview 支持 Windows cmd/私有 profile 和独立 skill-home，不代替正式安装互斥与维护流程。

前端构建及 wheel 生成通过；Python 四层 2290 tests / 128.137s / OK，Ruff、75 文件结构、working-tree 仓库边界、diff 检查通过。未提交。

第一次派发在配置阶段因 preview fixture 写 terminal 而非 task_list 被拒绝；确认任务板为空后修正，只执行了一条真实模型任务。历史拒绝不算模型测试通过。

证据在 win-flow-evidence/：acceptance.json、DPZE-001-status.json、DPZE-001-report.md、DPZE-001-output.json、runtime.json、python-full.log。wheel SHA256 见 acceptance.json。
