# WIN-02 Windows 宿主适配进度

2026-10-01。状态：进行中，基础适配已有验证；尚未交付可安装 Windows 候选。Mac 正式安装未替换。

## 已实现与验证

- 平台身份区分 macos-arm64 / windows-amd64，Compose、运行时路径、构建资产选择和更新镜像校验按架构处理；拒绝跨平台更新。
- 宿主文件锁隔离 Unix fcntl / Windows msvcrt；Windows 私有文件按当前 SID、受保护 DACL 验证，拒绝 reparse point。容器内 Linux 锁不变。
- Windows 台式机直接验证跨进程锁冲突及释放、私有 ACL、添加 Everyone 读取后的拒绝、中文路径原子 JSON 写入和 Desktop 挂载路径映射。
- 提供 PowerShell 安装与恢复入口、Python 多版本实际探测、venv Scripts 路径和受管 cmd 入口代码。PowerShell 语法及缺包失败行为已实测；未执行完整安装。
- Windows 业务参数解析改由容器读取，避免薄宿主仅为解析参数导入 Linux 业务模块。
- 不在此次基础验证中导入凭据或发起模型请求；未改 Windows 用户或机器执行策略。

## 当前未完成项

1. WIN-02-B：完整 amd64 离线运行时及安装事务。Hermes 锁定源码归档下载返回 HTTP 429，尚无匹配哈希的本地归档；不得用重新打包源码替代锁定归档并声称校验通过。
2. 安装态 setup / skills / CLI 集成与完整 Windows 私有目录链验证；中文路径下 cmd 调用仍需实测。
3. WIN-02-C：真实 Windows 项目 bind mount 与沙箱、输入快照、报告路径；更新/卸载须从受管 venv 外执行，尚未完成文件占用及失败回退验证。
4. WIN-03 桌面桥和恢复接口尚未实现；不能以现有 macOS 后台管理代码宣布 Windows 安装成功。

Windows 正式安装入口目前在事务修改前明确拒绝未验收候选。此保护是临时状态，不算 WIN-02 功能完成；完成集成与验证后才能移除。没有生成或发布可供老板替换安装的 Windows 包。

## 回归证据

- 最终 Python 四层全量：2288 tests，203.254 秒，OK。日志：win02-evidence/python-full.log。
- 安装相关定向回归 141 项通过；最终全量包含后续接线修改。
- Ruff、75 文件测试结构、working-tree 仓库根边界、git diff --check 通过；测试文件预算未提高。
- 前端未改，本轮未重新执行浏览器验收。Windows 真机基础验证不等于安装态业务闭环。

下一段顺序：恢复可验证的运行时输入 → 完成外部维护入口与 setup 接线 → 隔离安装/更新/卸载 → 联合 WIN-03 验收。四 Executor、真实审批、callback、报告、租约和重启验收仍在 WIN-04。

## 后续：2026-10-01 最小业务流已验证

DPZE-001 在 Windows 原生 amd64 Docker 中通过 Codex 文字收发、有效 callback、Core 报告、closed lease；仅使用 Windows 自有配置。隔离 preview 的宿主 cmd 和显式 CLI 审批接线已实测，2290 项全量回归通过。详情：WINDOWS-MINIMAL-FLOW.md。此结论不关闭上文正式安装/维护、项目挂载或 WIN-03 桌面桥待办；UI 通知仍 pending，会话清理未完成。

## 2026-10-05：可人工执行的 Windows preview setup

老板选定 Documents\AgentBC\workspace。新增受验证 Compose preview 的 setup/show/non-interactive 入口，复用 Native AI 探测、权限/预算/会话及 skill 确认；已有连接默认保留，Windows Hermes 自动探测 LocalAppData，未包含运行时明确跳过。WebUI 固定端口、背景偏好及 close 标记接入桌面桥；配置校验不再导入 Unix WebUI 服务模块。目录迁移先复制校验，原卷保留，未结任务阻止维护；独立安装 receipt/资格保护没有移除。

人工步骤见 WINDOWS-SETUP-GUIDE.md。此项是现有 preview 可完成 setup，不关闭正式离线安装、更新/卸载、四 Executor 或重启恢复验收。


## 2026-10-05 Windows 人工 setup 入口交付

老板选定 Documents\AgentBC\workspace。本机解析为 C:\Users\developer\Documents\AgentBC\workspace；原任务卷 72 个文件复制并通过 SHA256 内容清单核验，原卷保留回退。宿主 CLI 已接通 setup / --show / --non-interactive，交互复用 Native 探测、权限、预算/会话和 skill 确认，保留已有连接优先；WebUI 固定 50757，只读挂载同一工作区。默认工程不再只能在 Docker 卷内查阅。

Windows 实测重复非交互 setup 不改变已有凭据；CLI 仍读取 38K8-001 completed、lease closed；WebUI snapshot/report 成功，未认证 API=401；重复 start/close 端口和绑定不变，审批桥 ready。主机 wheel SHA256：83f4db5b4e510ba5e3cd0e96f7254517db5c2c06678217b19fc2c2b6b5d6dcfb。

代码补齐 Windows 纯配置校验（避免导入 Unix fcntl）、Terminal 设置使用 task_list 合同、用户 Documents 默认目录、预览迁移/绑定、与派发互斥及失败恢复。最终 Python 四层 2306 tests / 128.269s / OK；前端 18 tests、类型检查和构建通过；Ruff、75 文件测试结构、working-tree 边界和 diff 检查通过。沙箱端口限制导致的 12 项错误经允许本地端口后全量重跑通过。

人工入口：Documents\AgentBC\setup.cmd，或 PowerShell 执行 & "$env:USERPROFILE\.local\bin\agentbc.cmd" setup。操作说明为 Documents\AgentBC\SETUP_GUIDE.md，仓库 deploy/docker/WINDOWS-SETUP-GUIDE.md。真实交互选择仍待老板操作。当前镜像仅 Codex/Hermes，其他 Executor 明确跳过；安装类型保留 docker-preview，此交付不关闭正式离线安装/更新/卸载资格或 Windows 四 Executor、重启恢复矩阵。Mac 安装未修改。

最终真机交互预演无编码异常，默认不重新导入配置、保持 inherit，三类 skill 安装计划正确生成。实际交互确认由老板执行。CLI 探测固定 UTF-8 解码，避免中文 Windows GBK readerthread 异常。


## 2026-10-06 Windows Claude 运行环境与配置

补齐 Claude CLI 2.1.247 / SDK 0.2.142，镜像 sha256:dab8c8aa90ddc5234fec70ebd994a30de3c5df24326bca417c89268ea2e3c530。使用仓库锁定公开 Linux amd64 二进制/SDK wheel，校验 SHA256；依赖安装采用 require-hashes 与 pip check。未复制 Mac 凭据或虚拟环境。

仅从 Windows 已有 settings.json / .claude.json 导入可迁移配置到独立私有卷，原文件 hash 不变，保留 provider/model/权限；排除宿主项目设置和 MCP。Runner ready 且 allowlist 包含 claude/codex/hermes；实际 Runner 使用 SDK 环境的进程启动参数及 SDK 导入验证通过。Windows 桌面桥 ready。setup 已反映 Claude 可用运行时与已保存配置，OpenCode 仍未包含。

此次只完成运行时与配置接入，没有模型调用，不关闭 Claude 真实任务审批、执行、callback、报告和清理矩阵。第一次切换因验收误把规范化 Python 路径当作 venv 身份而自动回退；修正为实际进程参数验收后成功，不降低运行时要求。证据：deploy/docker/win03-evidence/windows-claude-20261006.json；人工说明 WINDOWS-SETUP-GUIDE.md。

## 2026-10-06 范围调整：Windows OpenCode 闭环延期至 1.06

老板明确要求：Windows Codex 会话清理继续修复；OpenCode 的 Windows 闭环移至 **1.06**（跟踪项 `EXEC-106-WINDOWS-OPENCODE`）。包含 Windows Docker OpenCode 运行时、配置继承与派发—执行—最终 callback—Core 报告—RunLease 关闭的真实验收。

本条覆盖此前“Windows 四 Executor 全部通过才关闭 1.05a Windows”的范围要求：1.05a Windows Executor 验收范围为 Codex、Claude、Hermes；OpenCode 格登记延期，不能标通过。Windows 正式安装、三 Executor 同候选、桌面恢复、WebUI、更新/卸载及会话清理门禁仍有效。Mac 已通过的 OpenCode 证据保留；Linux 范围不变。OpenCode 原生审批仍为独立的 1.06a 待办，不因本次延期自动实现。
