# WIN-01 Windows Docker 环境与平台假设审计

日期：2026-10-01。范围：环境及源码审计、无凭据原生 amd64 沙箱验证；不安装 AgentBC，不派发模型任务，不改变 Mac 正式安装。

## 环境

台式机 win-desktop：Windows 11 Pro 23H2，Docker Desktop 4.93.0 / Engine 29.8.1 / Compose 5.5.1，WSL 3.0.1，Linux x86_64 引擎可达。程序 I:\docker\app，数据 I:\docker\wsl。Python 3.11 与 3.13 可用；python3.exe 为 WindowsApps 别名，安装器应实际执行版本探测，不凭文件存在判断。I 盘本轮可用约 328 GiB。

不要求升级 Windows。Docker Hub 直连超时、SSH 无 Windows 凭据会话是已知环境限制；可通过经校验的离线镜像继续开发。禁止据此修改用户登录配置或引入未知镜像源。

## 源码缺口与归属

| 缺口 | 已确认位置 | 改造及验收 |
|---|---|---|
| 包身份和运行架构硬编码 | docker_install_bundle.py、docker_install_update.py、docker_install_runtime.py | WIN-02：windows-amd64 / linux/amd64；安装、更新、回退三入口统一校验身份，禁止只去掉平台检查 |
| 镜像装配固定 arm64 | Dockerfile.distribution、build_docker_bundle.py、runtime_launcher.py | WIN-02：运行时路径与 TARGETARCH 解耦；选择 amd64 资产及依赖锁，核对镜像/SDK/二进制架构 |
| 宿主导入即失败 | installation.py、docker_approval_bridge.py、docker_install_supervisor.py 顶层 fcntl | WIN-02/03：宿主锁接口提供 Windows 实现；容器内 Linux Runner 的 fcntl 保留。Windows Python 实测 fcntl 不存在 |
| 用户私有文件校验不可移植 | docker_install.py、docker_host.py 的 getuid/st_uid/chmod/O_NOFOLLOW | WIN-02：按 SID/DACL 验证权限与所有权，校验 reparse point/junction；不能用 chmod 假定 Windows 凭据安全 |
| 安装入口为 Unix | install_docker.sh、docker_install.py、installation.py | WIN-02：PowerShell 安装/恢复、venv Scripts/python.exe、受管 cmd 入口、用户 PATH；无需开发者模式/symlink权限。互斥覆盖 cmd/exe/历史入口 |
| 宿主/容器路径混用 | docker_host.map_project、docker_install_runtime.refresh_binding | WIN-02：WindowsPath 与 PurePosixPath 分开；核验 inspect 中宿主路径的规范化形式；盘符大小写、中文空格、junction逃逸、未挂载拒绝 |
| CLI 路由带入业务模块 | docker_host.route_arguments 导入 cli.build_parser | WIN-02：审计完整传递依赖；薄宿主不应因解析参数加载 Linux Runner/Executor 运行依赖 |
| 后台就绪握手不可移植 | docker_host.start_bridge 的 pass_fds/select(pipe) | WIN-03：Windows 可用且有界的进程/握手实现；重复启动只能一桥，退出不遗留伪就绪 |
| 结束通知的管道等待 | docker_terminal_bridge selectors.DefaultSelector 注册 subprocess stdout | WIN-03：Windows socket-only select 不适用匿名管道，改为跨平台有界读取；保留大小上限、超时与去重回执 |
| 登录恢复为 LaunchAgent | docker_install_supervisor.py | WIN-03：当前用户交互会话启动/恢复；可用任务计划程序，但不能将 UI 放进无交互会话，亦不自动开启 Docker Desktop |
| 审批与通知为 AppleScript | docker_approval_bridge.py、docker_terminal_bridge.py | WIN-03：Windows 原生对话实现；确认/拒绝/超时、精确绑定、ack_pending 与重启未知态语义不变。SSH 只发起业务，弹窗须到用户桌面 |
| 安装和升级文件占用 | docker_install_update.py、docker_install_maintenance.py | WIN-02：Windows 运行中 exe/venv 无法按 Unix 方式替换；由包外维护入口执行切换与恢复，失败保留待修复登记 |

## 实施顺序

1. WIN-02-A：平台边界、跨平台锁/私有文件、架构清单、薄 CLI 依赖隔离。
2. WIN-02-B：离线 Windows 候选与安装互斥，安装后英文 setup、基础配置及 skill 自动探测。
3. WIN-02-C：项目路径、输入快照、报告映射、更新卸载与失败回退。
4. WIN-03：交互桌面审批/通知、单实例后台恢复和 WebUI 管理。
5. WIN-04：同一 amd64 镜像四 Executor，真实 Dispatcher、人工审批、callback、报告、closed lease、通知、适用会话清理；重启与更新卸载验证。

估计：WIN-02 3–4 有效开发日，WIN-03 2–3 日，WIN-04 1–2 日及人工窗口，另留 1–2 日修复缓冲。估计基于源码审计，不是工期保证；网络与原生沙箱阻塞另计。

维持现有测试文件预算；新增反例放入对应安装、路径、桥接测试文件。Mac 回归必须保留，禁止改变现有三 Agent 派发—审批—验收合同。OpenCode 原生审批等 1.06a 项不扩入本次。

## 原生 amd64 探测

本轮审计完成，可进入 WIN-02。原生 amd64 的镜像内置 Codex 沙箱通过，宿主适配尚未实现。使用已有 probe_docker_codex_sandbox.py，官方 Codex 0.153.4、锁定 Python 镜像与精确 seccomp 配置；无模型、凭据或业务任务。禁止 privileged/unconfined/SYS_ADMIN/full 回退。

### 真机结果

- 官方 Codex 0.153.4，Python amd64 配置 ID `sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15`。
- 镜像内置 Codex：read-only/workspace-write 全断言通过；外部与符号链接写、受保护目录写、敏感读取、网络、mount/unshare 均拒绝；UID 1000、零 capabilities、NoNewPrivs=1、Seccomp=2。未复现 Mac amd64 模拟的 SeccompInstall EINVAL。
- 默认 Moby seccomp 负对照在 namespace 创建失败，符合预期。
- Windows 只读 bind 挂载 Codex 包的探针失败于 `/newroot/opt/cli` remount EPERM。内置镜像方式消除了这个失败，但不证明 Windows 任意项目 bind mount 均兼容；WIN-02-C 必须验证真实项目目录、只读挂载组合和中文/空格路径，失败时不能扩大权限绕过。
- 首次离线加载未保留 registry 引用，导致尝试联网并触发 SSH 凭据错误。核验源镜像 ID 和传输 SHA256 后，探针副本使用精确本地配置 ID；所有安全断言保持不变。离线包必须以镜像身份验收，不要求 registry RepoDigest 一定存在。
- 证据：`win01-evidence/win01-baked.json`、`win01-baked-baseline.json`、`win01-candidate.json`、`win01-baseline.json`、`win01-first-load-failure.json`；调整后的独立探针 `probe_baked.py` 仅将运行时来源改为镜像内置。
- 探针镜像 manifest list：`sha256:7d89d9209fad6ae5ec690134081a4138fea652936c427e1626d462a70f8ce4d1`；配置 ID：`sha256:0b6743b80561190da0393194a708c4fe8689073c75bada483028dd6a361cf50e`。

探针包 SHA256 `2263a9d2be3e0ec3567a23a712997c31cafa0691dba87414b9dc1133c415c7c0`；Python 离线镜像归档 SHA256 `0bbda6e5d958f7916882a0f6775f8fe1052b38e23f7f616587c005e778b2a84f`。保留 D:\agentbc-tests 下无凭据探针供后续复现；探针容器使用 --rm，无常驻测试服务。

结论边界：WIN-01 完成审计与原生离线沙箱验证，不等于四 Executor、模型网络、审批弹窗或 AgentBC Windows 安装通过。本轮未改变生产代码，未运行全量业务回归；文档 diff 检查通过。
