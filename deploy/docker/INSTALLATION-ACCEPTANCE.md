# Mac Docker 安装线路：实现与验收记录

## 2026-09-30 当前结论：Mac Docker 内测通过，转向 Windows 适配

老板明确确认 Mac Docker 标记为内测通过。验收基线为已安装 r11，镜像 `sha256:2e0ed2e2b07e7ae84c4539bf68250dc0b99ba54d564802fc2161be0cbeb70d75`，开发包版本仍为 `1.0.4a1`。此结论取代此前“Mac 内测未通过”的历史状态，不等同于正式发布或三宿主全部通过。

- 安装引导、互斥提示、WebUI、审批和结束弹窗已获用户确认。
- Mac 与 Docker 实际重启后，Runner/WebUI healthy、审批桥 ready；T735-001（Codex）、JDQ9-001（Claude）、TZDS-001（Hermes）、8VB3-001（OpenCode）全部 completed、有效 callback、报告就绪、租约 closed，四份产物内容及 SHA256 一致。
- 三 Agent 人工审批来源为 user，临时会话清理 succeeded；四条结束通知均成功一次。OpenCode 清理不在本版支持范围。
- 安装态证据：`deploy/docker/REBOOT-ACCEPTANCE-20260930.json`。此前四层 2284 项和前端 18 项通过记录保留，本次状态更新未重新运行代码测试。
- 各 Dispatcher 客户端覆盖、最终发行安装/恢复证据汇总仍作为发行收口项保留，不再阻止 Mac 内测通过。Linux/Windows 不随 Mac 结论关闭。

下一开发主线：Windows 宿主 + Linux amd64 Docker。保持 macOS 主开发、同源业务核心与镜像构建，仅增加必要宿主适配。按 Docker/Compose/WSL 实际运行能力判定兼容，不以 Windows 必须最新版本为门槛。

顺序：WIN-01 环境与现有平台假设审计 → WIN-02 Windows 安装、身份互斥、CLI/skill/setup 与路径映射 → WIN-03 宿主审批/通知、登录恢复与 WebUI 生命周期 → WIN-04 原生 amd64 四 Executor 真机闭环、更新卸载及人工验收。首要核对 Windows 宿主、Docker context/daemon、Linux 容器模式、Compose、WSL、Python、磁盘及 amd64 Codex 工具沙箱；未验证能力保持待验。

继承安装二选一、干净重装、API/登录态自动探测且不改变原配置、固定端口、只读 WebUI、现有派发—审批—验收合同。覆盖 PowerShell、盘符、中文与空格路径、挂载边界和 Windows 私有凭据权限。当前 macOS launchctl/osascript 及宿主桥平台判断须隔离为平台实现，不能直接宣称 Windows 可运行。OpenCode 原生审批、pending 计时、运行记录/实时对话继续属于 1.06a。


2026-09-29。实施 INSTALL-01～06；本文件区分自动化证据与仍需人工确认的正式替换。未替换本机 Native 安装，未发布镜像。

## 正式入口和安装身份

- Native 与 Docker 使用当前用户固定位置 `~/.agentbc-install/installation.json` 及跨类型文件锁。停止服务、换 prefix 或移除 PATH 项不解除互斥。
- 检查默认安装目录、PATH 全部入口、Homebrew 共享入口、受管 skills／登录项及当前 Docker daemon 上的正式安装标签。只剩保留数据或镜像缓存不占安装资格。
- Docker 为 `~/.agentbc-docker` 中的独立宿主 venv 和单一 `~/.local/bin/agentbc` 入口；实例绑定 context、daemon、安装 ID、Compose 项目、镜像和挂载。
- 安装冲突返回非零退出；回滚／卸载不完整保留 `repair_required`／`uninstalling`，不能安装另一类型。
- `agentbc --version` 保留版本格式，`doctor --json` 和 setup 明示安装类型。Docker 的 `setup/doctor/update/uninstall/runner/webui` 在宿主管理，其余命令沿用已有容器 CLI 合同。

## 离线包构建

构建机需要 Node、Python、Docker 和公开依赖下载网络；安装机只需要 Python 3.10+、Docker／Compose，不需要源码、Node 或宿主 Executor SDK。API 调用仍需相应网络与配置。

1. 使用主线 Dockerfile 构建 `opencode` 基础镜像（Python／Node／OpenCode 均按现有锁定输入）。
2. `python scripts/prepare_docker_runtime.py <新目录> [--download-cache <下载缓存>]` 准备校验过的 Codex／Claude 二进制和固定 Hermes 源码。
3. `docker build --platform linux/arm64 -f deploy/docker/Dockerfile.distribution --build-context runtime-inputs=<目录> -t <运行时镜像> .` 从哈希依赖锁安装分离的 SDK／Hermes 环境；不复制宿主开发 venv。
4. 构建包含前端资源的 wheel，然后执行 `python scripts/build_docker_bundle.py --image <运行时镜像> --wheel <wheel> --output <新目录>`。
5. 构建器将同一 wheel 写入宿主包和容器，绑定 wheel SHA256，并输出完整镜像归档、支持脚本、构建身份、文件校验表及外层归档 SHA256。无 token 或 Executor 凭据入包。

当前版本字符串仍沿用开发基线 `1.0.4a1`；候选身份以完整包 SHA256、镜像 ID 与 source_build 为准，不能据此冒充正式 1.05a 发布。

## 用户操作

先校验外层 `.tar.gz.sha256`，再解压并进入目录：

```sh
sh install_docker.sh
# 明确解释器时：PYTHON_BIN=/absolute/python3 sh install_docker.sh
agentbc doctor --json
agentbc webui start
agentbc webui close
agentbc update --bundle /absolute/extracted-candidate
agentbc uninstall --keep-records --keep-artifacts
```

setup 全英文，首次默认 WebUI、后台 Y；非交互首次不启动 WebUI。固定端口冲突不自动换号。`close` 不改变常驻偏好，本次会话不立即重开；登录或 Docker 恢复后按偏好启动，不主动打开 Docker Desktop 或浏览器。

API 用户可明确选择原 CLI 的 profile HOME 或每个 Executor 的 API 环境 JSON。环境 JSON 只允许列出的该 Executor API／模型／代理键，不执行 shell 文件，不复制整个 HOME，不把多个 Executor 的凭据注入 Runner 或 WebUI。原生 hooks、plugins、MCP 或未支持的设置会明确拒绝，不静默丢弃后继续。单独环境 JSON 不代替原 CLI 必需的 provider/model 配置；两者可分别导入。

卸载默认保留数据和镜像。若需保留容器内运行凭据，显式传入 `--keep-credentials`；否则清理自有 profiles/runtime 卷中的副本，原始宿主 CLI 凭据不动。备份目录仅当前用户可访问。修改过的 skill 先备份再清理；失败时不解除登记。

跨类型只走“卸载 → 重新安装”。旧 Native 可先使用匹配离线包的 `sh recover_docker.sh uninstall-native` 做备份和卸载：对经核验的 Homebrew AgentBC 使用其包管理器，不手动删除 Cellar；其他非受管安装要求先使用其所属工具卸载。恢复工具不会在卸载后自动安装 Docker。

CLI 损坏时使用 `sh recover_docker.sh status` 或 `sh recover_docker.sh uninstall`。回退、卸载保留的记录供本地查阅，新安装不导入旧任务执行状态。

## 已有自动化证据

- 真实公共安装预检识别 `/opt/homebrew/Cellar/agentbc/...` 并拒绝 Docker 安装，本机 Native 未改变。
- 正向安装使用临时 HOME、独立 Compose 标签和测试注入的安装清单；公共安装器没有跳过互斥开关。此证据不等同于本机正式卸载重装。
- 隔离离线安装、非交互 setup、doctor、固定端口 WebUI、重复 start/close、空闲 Runner stop/start 已通过。
- 同类型离线更新通过；故障镜像无法启动时，已实测恢复旧镜像、绑定和 installed 状态。
- 发现并修复启动入口误写 `__main__`、空数据卷缺报告目录、LaunchAgent 重复注册竞态及回退后旧 PID 被新容器复用的问题。
- Docker 新 Runner 使用持有至进程退出的内核锁；只有取得独占锁后清除旧 namespace 的 PID 文件。重复 Runner 不能取得锁。Native Runner 合同不变。
- 隔离卸载移除自有容器、登录项、入口和登记，保留数据卷与经读取核对的备份；随后在同一临时 HOME 完成 Native 安装／卸载。
- 首轮完整四层回归 2258 项通过；后续新增修复以最终回归记录为准。

## 收口门槛

最终固定输入运行时构建、最新代码全量回归、同一最终候选四 Executor 真实 callback／报告／closed lease，以及真实 Dispatcher skill 和 Mac 人工安装流程均需要独立记录。未完成项不得写成通过。Linux／Windows 原生宿主格继续待验，OpenCode 原生审批继续归 1.06a。

## 2026-09-29 候选与剩余人工门槛

当前候选目录：`dist/docker-install-candidate-r3/agentbc-1.0.4a1-docker-macos-arm64`。

- 外层归档 SHA256：`6138a4b2b5012e63cca0a228cb8ec3af85adf9c85f79000a2df4e66850f6e125`。
- 镜像身份：`sha256:fefbb089a87144599274cf1d329218ec7c08a53ca0583438fd9eb1f6892f8474`。
- 运行时由锁定二进制、Hermes 源码及哈希依赖锁重新装配；宿主与容器安装同一 wheel。
- 更新在容器切换后注入进程退出 77，使用包内 `sh recover_docker.sh rollback` 恢复旧镜像、配置、数据、skills 和 installed 登记已通过。恢复前校验事务归属及备份校验和；恢复失败保持阻塞状态。
- 正向 fixture 的 HOME 和 Docker 项目隔离；原生冲突发现只在测试进程注入为空，生产安装器不提供绕过开关。未在本机正式卸载 Native。

前一轮安装候选 `sha256:ad22f87b420659cec71510364e4e38646fb548c0bd4cfd2985dea6fba6727bb0` 的真实任务：

| Executor | Task | 结果 | 结论 |
|---|---|---|---|
| Codex | TW8C-001 | failed；权限弹窗无人工批准 | 需要老板在场重新验收，不自动 retry 或改 full |
| Claude | V5CV-001 | failed；权限弹窗无人工批准 | 同上 |
| Hermes | DM4G-001 | failed；权限弹窗无人工批准 | 同上 |
| OpenCode | PRY7-001 | completed，marker-valid callback、报告、产物、closed lease | 仅该镜像本格通过；不能替代最终镜像四格 |

四任务租约均已关闭。任务状态与报告 SHA256 见同目录 `INSTALLATION-EVIDENCE-20260929.json`。报告及证据不含启动 token 或模型凭据。

真实 WebUI 列表、详情、报告、重复 start/close、固定端口及关闭 WebUI 后 Runner 保持正常均通过。三任务等待审批时，Runner stop、setup 重建和 uninstall 均拒绝且登记/绑定不变。源码新增无登记残留运行目录拦截、setup 导入后 Runner 配置重载，以及维护重启保留当前 stop/close 选择。

最终候选仍需要：同镜像四 Executor 正向闭环、真实 Dispatcher skill、登录/Docker 重启恢复、Mac 人工卸载重装。OpenCode 原生审批、Linux/Windows 人工格仍按原范围保留。**不得标记 Mac Docker 可完整替代 Native。**

最新候选安装态追加通过：离线更新至 r3；重复 Runner 内核锁拒绝；KILL 异常退出后重新启动；外部重建 ID 后 doctor 拒绝并经显式 setup 验证重新绑定；WebUI 读取/关闭/重开前后全部静态记录、报告与配置 SHA256 及 mtime 不变；临时工作目录、PATH 不含 Node 时安装 CLI 的 `--version` 可用。

最终自动化收口：Python 四层全量 **2263 tests / 577.158s / OK**（正常环境；全局 headless 曾影响弹窗断言，该次未计通过）；前端 **18/18**、类型检查和构建通过；Ruff、working-tree 仓库边界、index 公共发布边界、测试结构及 diff 检查通过。测试文件仍为 75 个，未提高预算。全量中的 OpenCode 相关用例未复现此前退出等待超时，未放宽断言。

最终包从源码目录之外、全新隔离 HOME 使用包内 wheel 完成干净安装，默认 WebUI、非交互后台关闭，四个 Executor 的 wrapper 配置正确；新数据卷无旧任务。前一隔离安装已经卸载，私有凭据与 retain=yes 会话通过显式保留选项保存在私有备份/卷中，未清理用户原始凭据。本机 Native 和原开发 Docker 服务保持原状。

## 2026-09-29：人工反馈与 setup 引导优化

老板确认现有安装流程、安装冲突提示和 WebUI 展示符合预期。这三项记为人工通过；不代替四 Executor 任务闭环、重启恢复及跨平台验收。

setup 保持全英文，增加本地配置自动发现和导入菜单：
- 已在本机使用 AI CLI：选择 Import detected settings，核对显示的来源，再确认复制。原配置不变，不发起模型请求。
- 配置在其他位置：Choose another user folder，选择包含提示中隐藏配置路径的用户目录。
- API 用户：优先导入原 CLI 已配置的设置；高级选项可选择现有 API 环境 JSON。界面列出支持的变量，提醒 provider/model 可能还需配置；不要将密钥直接粘贴到提示中。
- 尚未准备好或暂不使用：Skip for now。已有 Docker 配置保留，之后运行 agentbc setup 补配，无需重装。
- 不支持的 hooks/plugins/MCP 配置提供换目录或跳过入口，不要求删除原文件，也不直接中断整个向导。

导入后只显示“已导入／连接未测试”。复制配置不能证明登录有效或执行可用，需后续真实小任务验收。
重复 setup 默认保留已有导入记录；结束时解释固定地址、后台状态、私有启动链接和关闭命令。
配置路径或 API 密钥内容不会进入展示摘要。

本次验证：安装/setup/更新相关 139 项测试通过，测试结构与仓库边界门禁、diff 检查通过。旧测试已隔离 HOME、PATH 和共享入口，不再受本机 Docker 正式安装影响；未放宽安装互斥。此前四层全量记录属于上一候选，本次未重新声明全量通过。
本次修改尚未替换已安装程序，原 r3 包不包含上述引导。

## 2026-09-29：r5 Native setup 主流程对齐

r4 的逐 Agent profile/HOME/API JSON 菜单不再是正式安装入口。r5 复用 Native 的环境扫描、权限模式选择、Enable Agent、Claude budget、Hermes max-turns、session retention；随后配置 WebUI、安装受管 skill 并建立 Docker CLI 路由。skill 安装沿用 Y/n/view 确认与完整性检查，不覆盖用户修改。

启用 Agent 的确认同时授权复制检测到的基础配置与凭据，不再另问路径。支持默认目录及 CODEX_HOME、CLAUDE_CONFIG_DIR、HERMES_HOME、XDG_CONFIG_HOME/XDG_DATA_HOME；当前进程中对应 Agent 支持的 API 环境变量也进入其独立副本。未检测到配置时提示先配置原 CLI，再运行 setup，不伪造已连接状态。非交互 setup 不自动复制凭据。

Codex/Claude 配置使用结构化解析，排除明确列出的宿主 MCP/hooks/plugins、状态栏/notify 及宿主项目设置；在启用前列出排除项。保留基础认证、provider/model、全局权限设置，不修改宿主源文件。当前本机四 Agent 的纯读取转换已通过；没有发起模型调用。
标准引导不再询问项目或旧 AgentBC 配置路径。既有挂载保留，新增项目显式执行 agentbc setup --project "/absolute/project folder"。

这里对齐的是安装主流程；Docker 仍采用独立运行时、固定端口和受管服务，不能将宿主扩展视为自动兼容。Native 可选 abc alias 与 Hermes 多 profile skill 铺设尚未纳入 Docker 管理合同，当前仍使用正式 agentbc 入口和默认三平台 skill 目录。

验证：138 项安装/setup/更新测试通过，Ruff、测试结构、仓库边界及 diff 检查通过；测试文件仍为 75 个。此前全量记录不替代本轮验证。r5 未替换本机；模型调用及四 Executor 闭环待更新后的人工验收。

## 2026-09-29：P0 宿主审批投递修复候选

43YY-001 的旧日志只保留 RuntimeError，无法追溯当次 osascript 底层错误，不能将其归因于用户未及时审批。
修复包含：窗口调用前激活 GUI；窗口进程超时作为投递失败，不伪造用户超时决定；保存脱敏底层失败原因；同一有效请求最多三次投递，间隔 2/4 秒；仍按原 deadline 和完整请求身份校验。明确取得用户决定之后的异常不重新弹窗、不自动提交第二次决定。
重试计数保存到私有 approval-bridge-state.json，桥重启继承计数；重启前投递状态不明确时转人工处理，避免重复弹窗。投递失败发送无审批按钮的提示通知，并提供 task status 命令；doctor 展示 degraded/stale/unknown 和待处理请求。GUI 通知本身不可用时仍可查 doctor 和日志。
验证：239 项审批/安装相关测试通过，Ruff、测试结构、仓库边界及 diff 门禁通过。本机三秒独立诊断窗口成功返回 gave-up；未绑定任务、未向 Core 提交任何决定。没有重试、批准或取消业务任务。
P0 当前为“修复候选通过自动化，待正式安装后真实人工审批回传验收”；不得据此宣称 43YY-001 已恢复或四 Executor 闭环通过。


### 2026-09-29：P1 容器会话清理与日志修复（r7，待正式安装复核）

Codex Docker 会话改用独立 container_session_archive_then_delete 策略：保留官方绑定、retain、租约及结束投递门禁，必须取得官方 archive/delete 确认并通过新连接验证不存在；Desktop 标记 not_applicable，不伪造 Desktop 确认。旧 waiting_for_desktop 的 pending 回执可由容器维护继续处理。受管 skill 区分容器与 Native 清理；受管 Runner 入口设置隔离模式，兼容 r6 旧宿主更新器生成的 Compose。
任务日志保留历史 Worker ID，旧任务只读回溯同 Task 派发事件；Runner 重启可读取其自有目录的终态日志，不接管历史 PID，不接受路径穿越或符号链接。切换 run 重置日志偏移。

最终源码四层全量：2278 tests / 98.131s / OK；前端 18/18、类型检查、构建通过；Ruff、测试结构、working-tree 仓库边界、公共发布 index 边界、编译与 diff 检查通过。测试文件仍 75 个，未提高架构或测试预算。OpenCode 测试收尾竞态通过等待自身 Runner watcher 结束修复；callback-before-timeout 用例同步假进程输出后启动原超时，保留原断言。首次失败的回归记录未计通过。
最终候选镜像无网络、无凭据、无宿主挂载的真实 Codex 官方清理探测成功：archive acknowledged → delete acknowledged → fresh read absent。仅测试会话被清理，无模型用量。自动化测试覆盖旧 pending 回执迁移、retain/未绑定/Native 禁止删除、归档失败不删、日志路径边界与重启读取。

唯一交付候选：dist/docker-install-candidate-r7-final/agentbc-1.0.4a1-docker-macos-arm64.tar.gz
SHA256：ac172171a2a49a33db364c4a732555887a3b417aed81782f1699115be3055665
镜像：sha256:f23980b08fc6acbb87a5cf7220cf52ff7abdc5a93f95457314674c5e0d8144b8
包清单 7 个文件及外层归档校验通过。仍沿用开发版本 1.0.4a1，不冒充正式发布。r7/r7b 是中间构建，不用于本轮交付。

源码、候选包与开发验证已完成；未提交、未替换本机。WPMK-001 / 43YY-001 的正式安装清理与日志须更新后核对，两条暂停 heartbeat 未被恢复，未伪造归档确认。四 Executor 同候选闭环及 Mac 完整替代验收仍未关闭。详细方案与人工步骤：deploy/docker/P1-REPAIR.md。


### 2026-09-29：老板授权本机替换与 P1 安装态验收（r9）

已直接替换本机正式 Docker 安装，保留原安装身份、配置、记录、报告及固定端口 58013。最终安装 r9，镜像 sha256:01d240b4f7b16449fd5a269e3b8460b9f323f3dcce81a94f21af994eb4f9031b；包 dist/docker-install-candidate-r9/agentbc-1.0.4a1-docker-macos-arm64.tar.gz，SHA256 8860f04ad89086d483ecbd823aec9aa2a3f518d9d34716e2a5bcd421d567d4d5。r7/r8 为中间候选。

安装态追加修复两项：r6 未改动的受管 skill 因模板变化被误判 modified，采用已知旧模板完整指纹匹配允许升级，用户实际修改仍拒绝；Runner 重启未登记配置任务目录，现仅在允许根内登记配置 board_root，后台维护无需新任务触发。第一次更新失败已成功回退 r6，之后 r8/r9 同类型更新成功，三平台宿主 skill 正常更新。

真实验收：WPMK-001 保持 completed、callback/report 已有、租约 closed；43YY-001 保持 failed、租约 closed。两者 exact Codex 绑定、retain=false 均经核对，cleanup 均 succeeded，官方 App Server archive/delete acknowledged，新连接 cli absent，Desktop not_applicable。未调用 Desktop 归档、未伪造 acknowledgement、未重试业务任务；两条旧暂停清理 heartbeat 已删除。

WPMK-001 历史 Worker 日志可读，正常 stop/start Runner 后仍可读；WebUI close/start 后仍使用 58013，静态页、列表、详情、报告 200，无鉴权 API 401，业务状态未改变。Runner、WebUI 恢复运行，审批桥 ready。最终源码四层 2278 tests / 99.877s / OK，升级定向 33 项及 Runner/架构定向 73 项通过，Ruff、测试结构、仓库边界及 diff 检查通过；预算未提高。

本轮为 P1 安装态验收，没有新增模型任务或模型用量，不代替四 Executor 在 r9 同镜像上的新任务闭环验收，也不关闭 Linux/Windows 人工项。


### 2026-09-30：Docker 完成／失败弹窗修复（r11 已安装）

根因：容器 stdout 通知曾返回成功，Core 将其记为 ui_notification=succeeded；宿主桥只处理审批，未处理任务结束通知。现容器日志不再作为 UI 成功证据，UI 阶段等待宿主握手；deferred 不占用重试预算。Native 通知行为保持原合同。

宿主桥新增独立串行结束通知队列，完成／失败／取消使用任务终态投递 ID 绑定。实际 macOS 信息弹窗返回成功后才确认 Core；仅 OK，不执行权限操作，不直接打开容器路径，提供 agentbc task report 命令。已显示但回写失败保存 ack_pending，只重试回写，不再显示；进程在 UI 中断且结果未知时记 manual_action_required；明确投递失败最多三次并退避。私有日志不保存通知正文或凭据；doctor 可见 terminal_notifications 状态。容器维护使用非阻塞任务投递锁，等待弹窗不阻塞审批和 IPC。

历史纠正限定显式 Task ID 且有精确 headless terminal notification written 事件。仅重置错误的 UI 投递阶段，不重跑业务，不改终态、回调、报告或租约；已存在真实宿主确认的事件拒绝再次纠正。文件通知重试产生的空 dialog_message 不掩盖真实 UI 事件。

安装态：本机已更新至 r11，镜像 sha256:2e0ed2e2b07e7ae84c4539bf68250dc0b99ba54d564802fc2161be0cbeb70d75。包 dist/docker-install-candidate-r11/agentbc-1.0.4a1-docker-macos-arm64.tar.gz，SHA256 f080970ce3a0b9146d4515c445e9d4675a21e204967344b1033ce767c6b1d7e2。仍为 1.0.4a1 开发身份。

四条真实漏通知已补发并取得系统侧成功回执：5FPM-001 failed（原因为拒绝提权），SZXS-001、F9H8-001、23XA-001 completed。四者租约 closed，后三者 callback=true；UI succeeded 且各一次尝试，业务状态不变。5FPM 的首次弹窗验证发生在 r10，另三条在 r11，不能混称四条都在同一版本首次送达。已向老板请求可见性反馈，尚不能代替老板肉眼确认。

自动化：四层全量 2284 tests / 115.983s / OK；通知定向 86 项通过；前端 18 项、类型检查、构建通过；Ruff、75 文件测试结构、仓库边界、公共发布 index 边界、diff 检查通过，未提高预算。覆盖 pending/ack 绑定、失败与取消、重复扫描、旧记录保护、确认回写丢失、重启不重复及失败重试上限。没有新增模型调用或重试旧任务。本修复不等于四 Executor 同镜像正向业务验收全部通过。
