# WIN-03 Windows 审批、完成通知与桌面恢复

2026-10-02；状态：实现与隔离联验进行中，尚未关闭工作包。Mac 正式安装未替换，未提交。

## 实现

- `desktop_dialog.py` 适配现有审批载荷、期限及按钮回执；Mac 继续 AppleScript，Windows 调用独立 `windows_dialog` 进程。JSON 管道有长度及等待上限，无正文命令拼接、匿名管道 selectors 或 Windows pass_fds。
- Windows TaskDialogIndirect 使用 ComCtl32 v6 激活上下文、正确的结构对齐和回调生命周期。默认拒绝；v2 只返回原生 handle；v3 保留 View Details / Deny / Approve Full。详情不续时，关闭／到期沿用拒绝语义，显示不确定不冒充决定。
- 完成窗口仅 OK，30 秒关闭。实际显示后先持久化 ack_pending，再经原 Core 确认；确认丢失只补确认，显示中断进入人工处理。审批、通知分别单线程队列。
- `windows_desktop.py` 注册当前 SID 的 InteractiveToken、Limited、登录触发计划任务；IgnoreNew，不存密码，不自动启动 Docker Desktop。pythonw 与 CREATE_NO_WINDOW 避免控制台窗口。Job Object 回收桥的子进程。
- 就绪校验 installation/container/image/engine、实例键、SID、进程创建时间及交互会话。锁屏／不明确会话等待，Docker 不可用退避 2/4/8/16/30 秒，身份异常停止恢复。
- 正式安装 setup/维护接入同一注册接口；普通桥重启保留显式 WebUI close 标记。正式 Windows 安装资格仍由 WIN-02 验收，代码接线不能代替安装态实测。

## 当前真实证据

DPZE-001 没有重跑。原终态 delivery `6d024f60-97a9-4408-a251-dda14ef96c72` 的 ui_notification 已取得 Core `succeeded`，attempts=1；宿主未重复弹窗。独立技术探针在 SID 对应 Session 1 显示成功并于 30 秒超时关闭；老板点击与视觉确认尚待反馈。

DPZE-001 清理仍阻塞：旧探针镜像的 Codex 位于 `/opt/cli/bin`，不符合受管清理入口 `/opt/agentbc/bin/codex`。清理回执为 `container_session_scope_invalid`、不可自动重试；协调器拒绝强制重试，未手改回执，也未调用 Desktop 归档。尝试的软链接经二进制发现规范化仍不能满足入口合同，后续应使用真实受管 launcher 并走经过验证的精确清理修复接口，不放宽路径校验。

DYTE-001 为新的最小工具任务，仅在默认产物目录写读 `win03-marker.txt`，沿用 inherit 和 Windows 原有配置。第一次因验收配置误用 `agent_bin` 字段导致 worker 未启动；修正为现有 `command` 字段后通过正常 retry 派发。已收到真正的 Codex `item/commandExecution/requestApproval`，v3 请求、300 秒期限，已于 2026-10-01 16:21:05Z 按 300 秒期限自动拒绝（source=timeout），任务 failed、lease closed。没有收到人工按钮选择；未自动重试、未自动批准或扩大任务路径。

## 隔离入口

```powershell
$env:PATH = 'I:\docker\app\resources\bin;' + $env:PATH
$agentbc = 'D:\agentbc-tests\win-flow-20261001\host\bin\agentbc.cmd'
& $agentbc task status DYTE-001
& $agentbc task report DYTE-001
```

最终宿主 wheel 已部署， `& $agentbc doctor` 输出桌面桥状态；不输出通知正文或 token。有运行中的审批或通知时不替换宿主或容器。

预览手动注册／修复自身桥（不是正式安装命令）：

```powershell
& 'D:\agentbc-tests\win-flow-20261001\venv\Scripts\python.exe' -m agent_bridge_connect.windows_desktop register --profile 'D:\agentbc-tests\win-flow-20261001\host\docker-host.json'
```

入口仅操作已登记的隔离容器，不把同名容器当作原安装。停止／重建前必须确认无未结任务；不得同时启动保留的回退容器。CLI 显式模式继续可用，桌面投递失败不会静默回退。

## 验收状态

| 工作包 | 当前状态 |
|---|---|
| 03-A 桌面探针 | 原生显示／Session 1／30 秒关闭机器证据通过；人工点击、中文视觉、无黑窗待确认 |
| 03-B 完成通知与清理 | DPZE 真实通知成功；精确会话清理阻塞，未通过 |
| 03-C 人工审批 | 自动化覆盖按钮、超时、详情、陈旧请求、确认丢失；DYTE 真实超时拒绝已验证，人工批准继续执行已由 38K8-001 Hermes 实测通过；Codex 待验 |
| 03-D 恢复与安装 | 实现登录桥及维护接线；锁屏、注销、Docker 重启、WebUI 关闭后审批的人工矩阵待验 |

后续人工流程：使用新的明确授权测试任务（DYTE 已终态，不再操作其过期审批），核对 task status/report、最终 callback、closed lease 与完成窗口；再验证 30 秒自动关闭、连续任务、关终端、锁屏解锁、注销登录、Docker 重启、WebUI close 后审批仍可用。真实审批与通知应分别记录，看到探针不代表业务闭环。未验项目保持待验，不关闭 Windows 正式安装或四 Executor 矩阵。

## 自动化收口记录

Python 四层全量 2301 tests / 443.462s / OK；前端 18 项测试、类型检查、构建、Ruff、75 文件结构、working-tree 仓库边界及 diff 检查通过。沙箱内全量曾有 12 个本地端口绑定 PermissionError；改在允许本地服务的测试环境重新跑全部四层通过，没有削弱断言。最终计划任务用户名/SID 与枚举兼容修正又经定向测试及 Windows 实际注册验证。

最终隔离宿主 wheel SHA256：`6d682fe0b53e37da210d28cb73c83618438632fb62c3e57ed3b7c61c893776e8`。容器镜像没有因此变为完整发行镜像；DPZE 清理、人工批准继续执行与恢复矩阵仍是阻塞项。

最终实测：DYTE-001 的失败通知 ui_notification=succeeded、attempts=1；最终宿主升级后 doctor=ready、SessionId=1。DYTE 会话清理也受同一探针 launcher 路径问题阻塞。没有再次派发任务或延长审批期限。


## 2026-10-05：38K8-001 人工验收通过范围

老板反馈：“任务完成，弹窗和审批内容基本符合预期”。Windows Docker Hermes / DeepSeek（deepseek-v4-flash）本次最小闭环通过：派发 → 原生权限请求 → 人工批准 → 同会话继续 → 最终 callback / Core 报告 → 完成弹窗。

- 安装态 CLI 核验：`status=completed`，step 1 done，`has_final_callback=true`、`report_ready=true`、`run_lease_state=closed`。
- 真实 v3 edit 请求；用户于 `2026-10-05T13:40:38.752724Z` 选择 `approve_full`。请求、通知、人工决定及 full continuation 各一次；没有自动批准。基础模式仍为 inherit，本任务人工授权提权已验证。
- 官方绑定 Hermes session `c34118cc-df05-4671-8599-4ee3d1dc2e19`，resume_count=1。最终回调报告在指定产物目录完成 `hermes-test.txt` 写入及回读。
- delivery `1c20a321-6559-41ae-936c-fd131b917b11` 五阶段均 succeeded；完成 UI 通知 attempts=1。
- Core 会话清理回执 `succeeded`，strategy=`official_session_delete`，attempts=1，无错误。该回执的 phase 仍为 waiting_for_cleanup，细分 verification/commands 为 not_applicable；此处仅确认 Core 成功回执，不宣称额外完成底层物理删除审计。
- 镜像 `sha256:41c1bdd18c5d0642da63fa5c3616a5834be8551bc4e3254d8ffa6e6cc4662348`；仅使用 Windows 原有凭据，原配置保持不变。

证据：`deploy/docker/win03-evidence/38K8-001-status.json`、`38K8-001-report.txt` 及本次老板人工反馈。此次只收口 Hermes 的审批与完成通知最小流程；Codex 启动/旧任务清理问题、锁屏解锁、注销登录、Docker 重启、WebUI close 后审批及维护恢复矩阵仍待验，不关闭 WIN-03 全部工作包、Windows 正式安装或四 Executor 验收。此次仅记录验收，无代码修改、重新派发或替换 Mac 安装。

## 2026-10-05 Windows WebUI 接线

老板已确认 CLI 任务查看符合预期。原 preview Runner 无 Compose 项目标签，WebUI 无绑定；现已在确认无未结任务后迁入 `agentbc-win-flow-20261001` Compose 项目。保留原数据/home/Hermes profile 外部卷，旧容器停止保留为 `agentbc-win-flow-20261001-pre-webui-20261005`，禁止并行启动。

- Windows Compose 文件：`D:\agentbc-tests\win-flow-20261001\compose.webui-20261005.json`。
- Runner：`01d974a508eb60fd315e25a55027e40d36da804c81449b163d8293663c40e426`；WebUI：`33b4b453ad650ede8aeb701b3d8e1555fd6b1877913f28470b2f1e749ecabfdf`。
- 固定地址 `http://127.0.0.1:50757/`；仅回环发布。WebUI 使用同一镜像、只读任务卷、只读 rootfs、独立 tmpfs，无 Executor profile 挂载；明确 Host/Origin 与 token 鉴权。
- 使用安装态 CLI 完成 status → close → status(stopped) → start --no-open → status(running)，端口未变化；认证 snapshot 接口成功读取 38K8-001。未记录私有启动链接。
- Runner ready，桌面桥新绑定 ready / Session 1。当前服务 restart=no；Docker 重启/登录自动恢复未在此次验收，不能据此关闭恢复矩阵。
- 此配置尚无 Docker healthcheck，status health=unknown；已单独验证 HTTP 就绪及认证读取，不将 unknown 宣称 healthy。页面视觉、分类和报告打开仍待老板人工验收。
- Windows PowerShell 使用现有完整 CLI 路径运行 `webui start` 获取当前认证链接；裸地址不替代认证启动链接。Mac 安装未修改。

老板后续确认“webui正常运行”：当前 Windows WebUI 页面人工验证通过，CLI 任务查看亦已确认。此反馈不代替 Docker 重启、锁屏、注销登录及完整安装态验收。

后续 setup 工作见 WINDOWS-SETUP-GUIDE.md。共用引导新增流程/目录/项目挂载说明和使用命令，补充 Windows LocalAppData Hermes 配置探测。36 项安装身份、Docker 入口及 WebUI 绑定定向测试通过，diff 检查通过；当前 venv 无 Ruff，未完成该检查。未部署到 Windows preview，未解除正式安装保护；宿主默认工程目录仍待确认。

## 2026-10-06 Windows Codex / Claude 同批人工验收

老板确认“两项任务完成，用户面符合预期”。安装态 CLI 与宿主产物交叉核验：RQP6-001（Codex）及 6DZG-001（Claude）均 completed，step 1 done、有效最终 callback、Core report_ready、RunLease closed。两项终态 delivery 五阶段均 succeeded，ui_notification attempts=1。两项均有真实 v3 approve_full、decision_source=user 回执，不以模型摘要代替审批证据。

产物逐字节符合要求：Codex 23 bytes，SHA256 7664ce299d85721b052d1fef83a000a769c0e209cdb6808120be46c235c3a576；Claude 24 bytes，SHA256 a44afe14291b213bfa29028743c6f3f5e6ce6dacf4f36b6f1c0e21e373e35431。批准时间（北京时间）分别为 2026-10-06 01:44:59 与 01:43:44；容器记录目录仍按其 UTC 日期为 2026-10-05，未修改历史路径。

通过范围：两个 Executor 本次派发—人工审批—执行—callback/报告—完成弹窗最小流程，结合老板用户面确认记为通过。Claude 官方清理回执 succeeded / claude_project_purge / attempts=1；Codex 当前清理 not_requested / attempts=0 / capability unknown，未完成，因此不关闭会话清理或完整 Windows 发布验收。未重试业务、未替代容器清理调用 Desktop 归档。

另记录报告质量待优化：Claude final callback 摘要称“no approval required”，与 Core SDK can_use_tool 人工批准回执矛盾；以权威回执为准。业务产物及完成证据不受影响，该摘要不能作为免审批证明。

证据：deploy/docker/win03-evidence/RQP6-001-acceptance-status.json、6DZG-001-acceptance-status.json、对应 acceptance-report.txt 及 pair-artifacts-20261006.json。仍未覆盖全部 Windows 安装/更新/卸载、重启恢复和 OpenCode 矩阵。

## 2026-10-06 范围调整：Windows OpenCode 闭环延期至 1.06

老板明确要求：Windows Codex 会话清理继续修复；OpenCode 的 Windows 闭环移至 **1.06**（跟踪项 `EXEC-106-WINDOWS-OPENCODE`）。包含 Windows Docker OpenCode 运行时、配置继承与派发—执行—最终 callback—Core 报告—RunLease 关闭的真实验收。

本条覆盖此前“Windows 四 Executor 全部通过才关闭 1.05a Windows”的范围要求：1.05a Windows Executor 验收范围为 Codex、Claude、Hermes；OpenCode 格登记延期，不能标通过。Windows 正式安装、三 Executor 同候选、桌面恢复、WebUI、更新/卸载及会话清理门禁仍有效。Mac 已通过的 OpenCode 证据保留；Linux 范围不变。OpenCode 原生审批仍为独立的 1.06a 待办，不因本次延期自动实现。

## 2026-10-06 Windows Codex 会话清理修复与真机验收

已部署修复镜像 `sha256:c766be667762b124b3e0bd892a9a139b0c7f2efce4a1e483f594214a9866fd9a`，当前 preview Runner / WebUI 共用此镜像。以真实非软链接 `/opt/agentbc/bin/codex` 启动原 `/opt/cli/bin/codex`，保持原 Windows 导入凭据与隔离 HOME，不迁移会话库、不放宽路径校验。维护前验证安装归属、无未结任务，切换后重绑桌面桥并恢复 WebUI。

协调器允许已修复受管入口的 `container_session_scope_invalid` 在原清理预算内重新尝试；仍核验 terminal、closed lease、报告/通知、retain=false、官方绑定。原执行连接已有可信归档确认时复用该回执，继续官方 delete 与新连接 read；对应旧 archive_failed 仅在官方归档回执和时间存在时可恢复。没有伪造 Desktop 确认或直接删除私有会话文件。

- RQP6-001：cleanup succeeded，attempts=2，官方 archive/delete acknowledged，新连接 cli absent。
- DYTE-001：cleanup succeeded，attempts=2，同上；业务状态仍 failed，不重跑业务。
- DPZE-001：cleanup succeeded，attempts=3，复用 2026-10-01 原执行连接归档确认，官方 delete acknowledged，新连接 cli absent。
- 三项 Desktop 状态均 not_applicable，清理 phase=delete_acknowledged。已有任务终态、callback、报告、closed lease 与通知 delivery 保持不变；未触碰 Dispatcher，未触发模型调用。

构建和验收证据：`deploy/docker/win03-evidence/codex-cleanup-20261006/`。仅关闭这三项 Windows Codex 清理阻塞，不自动关闭 Windows 正式安装、重启恢复或最终同候选业务矩阵。Mac 正式安装未替换，未提交。

最终源码四层全量 2309 tests / 123.509s / OK；清理定向79项、前端18项、类型检查、构建、测试结构（75文件/39812行）、仓库边界及diff通过。修复后重复读取验证成功回执不再重试，原Codex产物SHA256不变，Runner/桌面桥ready，WebUI healthy、固定50757。CI固定 Ruff 0.15.22 全仓 src/tests/scripts 检查通过。

## 2026-10-07 Windows / Docker 重启联验：恢复未通过

用户确认两者已重启。初始 installed doctor 显示 desktop degraded/stale_heartbeat，Runner stopped，WebUI running=false；installed runner start 同样被 stopped 前置检查拒绝。源码 preview 登录恢复仅启动 WebUI，没有启动 Runner；WebUI 启动失败会使桌面桥退出，具体本轮错误为固定端口50757被 Windows TCP excluded range 50753–50852包含，Docker bind access denied。

在验证 preview Compose/容器/镜像归属后手动 Compose start runner，重新启动受管桌面桥，ready / Session1。WebUI 保持失败，未自动换端口、未删除系统端口保留或停止其他进程。本次后续业务测试仅验证手动恢复后的能力，不能关闭自动恢复验收。待修复：preview Runner 生命周期入口、WebUI 恢复失败与审批桥隔离、Windows固定端口分配/冲突引导。

## 2026-10-07 重启后手动恢复：三 Executor 业务验收通过

DKRV-001（Codex）、WMTT-001（Claude）、YS8W-001（Hermes）均 completed；唯一 declared step done，合法最终 callback、Core report_ready、RunLease closed。三份宿主标记产物逐字节符合要求（23/24/24 bytes），三项真实 v3 approve_full 的 decision_source 均为 user；结束通知均 succeeded / attempts=1。三项 Core cleanup 均 succeeded / attempts=1；Codex 另有官方 archive/delete acknowledged、新连接 cli absent（phase=delete_acknowledged），验证新任务自动清理修复有效。

本次通过范围为当前修复镜像上、重启后手动恢复服务的三 Executor 最小闭环。Runner/桌面桥 ready；WebUI 仍 stopped，固定50757被Windows保留端口范围覆盖。自动恢复、runner start停止状态拦截和WebUI失败导致桥退出仍待修复；不能关闭重启恢复工作包。另发现诊断不足：WebUI stopped却health=starting/error空，doctor只报桌面ready、exit0，未汇总WebUI故障。

Claude callback再次声称“No approval required”，与实际人工批准回执矛盾，保留摘要质量缺陷；Claude/Hermes cleanup主状态succeeded但phase仍waiting_for_cleanup，保留投影一致性待办，不将其误判为业务未完成。

证据：deploy/docker/win03-evidence/reboot-results-20261007.json、三项 *-reboot-status.json 和 *-reboot-report.txt。本次未改代码、未重跑业务、未人工代替清理。

## 2026-10-07 Windows WebUI 固定端口修复通过

原固定端口50757位于Windows重启后的TCP排除范围50753–50852，Docker bind access denied。经老板授权修复，显式重配置为 **36124**，同步Compose端口/public-origin、宿主登记及容器配置；非自动换端口。未修改Windows排除范围、防火墙、Docker系统配置或其他进程。

Windows setup新分配端口从18000–47999中进行有界候选探测，跳过权限拒绝和占用端口，优先避开默认动态范围；这不是长期占用承诺，仍可能受后续系统/程序变化影响。start保持固定端口，Windows冲突提示端口号、保留/访问拒绝原因及setup重新分配入口；停止服务状态不再误显示health=starting。现有合法端口不会被普通start或重复setup自动更换。

Windows已安装宿主wheel SHA256 a9494984fb054026c08fda3e03037aa6f9b4e928df4b526d7ea10d7ba2241607。Runner容器和镜像保持不变；WebUI重建并更新绑定，healthy、固定36124，桌面桥ready。真实页面、列表、DKRV-001详情和报告均HTTP200；重复start、重复close、再次start通过。使用`agentbc webui start`打开带鉴权链接，裸地址http://127.0.0.1:36124/不是免鉴权入口。

定向100项、四层全量2311 tests / 174.241s / OK；前端18项、类型检查/构建、CI固定Ruff0.15.22、75文件39842行结构门禁、仓库边界及diff通过。证据：deploy/docker/win03-evidence/webui-port-repair-20261007.json。

本轮仅关闭当前WebUI端口启动故障与停止状态误报；Windows登录自动恢复、停止Runner的启动入口、WebUI恢复失败与审批桥隔离、doctor完整健康汇总仍待收口。新端口跨重启人工验证仍待完成，不能据本次启停宣称重启恢复通过。未提交、未替换Mac安装。

## 2026-10-07 Windows 自动恢复修复

修复 preview 登录恢复仅启动WebUI而漏启动Runner的问题。桥按受管身份和Compose摘要核验，在单实例维护锁内恢复自有服务；首次/新登录或检测到Docker不可用后恢复时清除本会话关闭标记，普通桥重启保留runner.closed/webui.closed。服务意外停止时每30秒检查恢复；Docker未就绪/运行传输暂不可用保持等待及2/4/8/16/30秒退避，不退出桥。身份不匹配仍停止，不接管同名实例。

WebUI启动错误独立保存在desktop.webui_error，每30秒重试，审批桥继续运行；doctor在桥ready但WebUI恢复异常时退出码1。Preview宿主runner start/stop/status使用已验证Compose管理容器，停止状态不再被load_profile前置检查阻挡；stop仍检查未结任务/租约，与setup/派发共享维护锁。

已部署宿主wheel SHA256 9196112d3c4d21436208f5d21c6d0517b02bd5e98e62f7880236b84ef8dfcc1e；Runner镜像/容器绑定和固定端口36124不变。

真机受控验证通过：显式runner stop/webui close后重启桌面桥，两服务继续停止；installed runner start可从停止启动；仅停止自有容器模拟意外服务丢失后，桥自动恢复Runner和WebUI。Runner/桌面桥ready、WebUI healthy、旧三任务completed/closed/cleanup succeeded保持。

本轮没有重启整台Windows或Docker Desktop，不将受控容器停启等同于真实系统重启。完整Windows登录触发、Docker daemon重启与固定端口跨重启仍需老板再次重启后的实测确认。Windows正式安装与Linux验收仍独立待办。

最终验证：定向154项、四层全量2313 tests / 317.138s / OK；前端18项、类型检查/构建、CI固定Ruff、75文件39888行结构门禁、仓库边界及diff通过。额外真机WebUI端口故障注入通过：桥同一PID保持ready，doctor警告；释放端口后自动恢复。证据：win03-evidence/recovery-acceptance-20261007.json、webui-isolation-20261007.json。

## 2026-10-07 真实重启自动恢复验证通过

老板按要求重启Windows/Docker并登录后，本轮只读检查，未执行runner start、webui start、ensure或Compose启动。Runner ready（三Executor allowlist）、WebUI running/healthy且固定36124、桌面桥ready无webui_error。登录LUID由555496变为478573、桥进程PID变为11096，安装及镜像绑定仍匹配当前修复候选。

真实HTTP页面、任务列表、DKRV-001详情及报告均200。DKRV-001/WMTT-001/YS8W-001保持completed、closed lease、cleanup succeeded且attempts=1；结束通知仍attempts=1，三份产物字节/hash保持。当前Windows preview的本次真实重启恢复、固定端口及历史数据保留验收通过。

证据：deploy/docker/win03-evidence/actual-reboot-acceptance-20261007.json。此结论不替代重启后新任务人工审批/通知实测，也不关闭完整Windows正式安装、更新卸载或独立Docker-daemon故障矩阵。本次无代码修改、无新派发、无模型调用、未替换Mac。

## 2026-10-07 当前遗留项总表（以最新验收为准）

已通过，不再列作现存阻塞：Mac Docker内测；Windows Codex/Claude/Hermes最小派发、人工审批、callback/报告、通知和清理；Windows setup用户面；固定36124 WebUI及真实重启后自动恢复/历史数据保留。Windows仍为docker-preview，不等于正式安装发行通过。

| 类别 | 遗留项 | 收口要求 |
|---|---|---|
| 1.05a 发布主线 | Windows正式离线安装候选 | 固定源码/镜像/宿主wheel身份，干净环境安装、Native/Docker双向互斥、CLI/skill/setup、中文空格路径及权限验收；不能以preview替代 |
| 1.05a 发布主线 | Windows更新/卸载/失败回退 | 未结业务阻止维护、外部维护入口、文件占用、部分失败修复、备份/数据保留及跨类型卸载重装 |
| 1.05a 待验收 | 最新宿主恢复修复后的新任务 | 真实重启后新任务人工审批/通知/清理；旧三任务保留已验证，不重复列为未通过 |
| 1.05a 待验收 | Windows剩余恢复负向矩阵 | 锁屏/解锁、会话断开、独立Docker daemon不可用/恢复、安装维护恢复；已有普通桥重启/显式关闭保持/容器意外停止恢复/WebUI端口失败隔离证据可复用 |
| 1.05a 待验收 | Linux实际宿主 | 四Executor及WebUI、安装/路径/启停/数据保留；Mac或Windows成功不能替代 |
| 1.05a 发布收口 | 同源双架构候选和全矩阵 | 可复建构建、再分发资格、真实Dispatcher客户端skill覆盖、最终候选受影响非回归和完整发行门禁；现有成功证据按版本绑定汇总 |
| 已知质量缺陷 | 审批摘要与权威回执矛盾 | Claude反复写No approval required，实际有user批准；不得用模型摘要替代审批事实 |
| 已知质量缺陷 | cleanup投影一致性 | Claude/Hermes主状态succeeded但phase仍waiting_for_cleanup；不误判业务失败，不伪造额外删除证据 |
| 待核验 | doctor完整健康汇总 | WebUI恢复异常警告已修复；仍需核验独立故障、预期关闭/禁用与全部服务健康一致性 |
| P1原定范围 | 七项1.04遗留 | FLOW-105-001结构化handoff、FLOW-105-002受控中断恢复、SESSION-105-001旧attempt清理、GUI-105-001终态监控关闭、GUI-105-002活动retry可见入口、GUI-105-004原生route恢复、PROTO-105-001官方子代理；先逐项核验已有实现及证据，不把未勾选当作未实现，不自动延期 |
| P2原定范围 | TEST-105-001持续减负 | 可比基线/耗时、去重收益及剩余优化记录；最新2313测试通过不等于优化目标达成 |
| 文档收口 | 消除当前状态歧义 | acceptance-matrix.csv除Windows OpenCode延期外仍为旧pending模板，需补真实证据并区分preview通过与最终发行通过；历史章节保留，以本表及最新验收覆盖旧状态 |

已批准移至1.06/1.06a：EXEC-106-WINDOWS-OPENCODE（Windows运行时与最小闭环）；EXEC-106-OPENCODE-APPROVAL（原生审批与续行）；GUI-106-001非运行计时；运行记录及实时对话。仅Windows OpenCode延期，Linux范围未变。

建议执行顺序：补重启后新任务及剩余恢复验收 → Windows正式安装/维护线路 → Linux → 同源最终候选及发行收口。两个已知质量缺陷可并行小批修复；七项P1逐项审计，任何延期需明确范围决定。

## 2026-10-08：full 路径进度回执与重试报告收尾

- 验收来源：G9B5-001 重试、Q8HC-001、RCPQ-001。三者 full 写入/回读、最终 callback、通知和清理已通过；Codex/Claude 中途 progress 缺少绑定，不算完整进度链路通过。
- 根因：Codex CLI 原先在阻塞 subprocess 完成后读取 thread.started；Claude 直接 CLI 原先未在执行前绑定预分配会话。不是通过放宽 Core progress 校验解决。
- 修复：受管 Codex 持续读取官方 JSONL thread.started，登记精确 run/session 和当前运行元数据；Claude 在授权完成后、启动 CLI 前绑定预分配会话。错绑仍拒绝，绑定失败终止执行。原生交互审批路径保持原有合同。
- 成功终态报告不再把历史错误作为当前 failure_code；历史 errors 保留用于审计，不修改历史业务记录。
- 自动化覆盖：进程尚未退出即消费回执、Codex/Claude full 适配器执行中真实 Core progress、错绑拒绝、stdout/stderr 排空、超时与绑定失败回收、成功任务保留历史错误但清空当前失败码。
- Windows 修复镜像与部署证据见 `win03-evidence/full-repair-20261008/`。新模型任务的进度回执验收单独记录，不以自动化或原模型任务结果冒充新镜像闭环。

最终回归：Python 四层全量 2318 项通过（183.833 秒）；前端测试、类型检查、构建、Ruff 0.15.22、测试结构、仓库边界及 diff 门禁通过。Windows 已部署候选；Mac 正式安装未替换，未提交 Git。

实际复验（2026-10-08）：Claude Q3K7-001 通过，Core 在终态前接受 progress sequence=1，产物精确匹配、最终 callback/通知/清理成功。Codex P2G5-001 已在执行期间绑定官方会话，但 300 秒后 executor_timeout，未产生产物或 progress/final callback，因此完整复验未通过；不能仅凭上次网络故障推定本次根因。Runner/WebUI/桌面桥健康；未自动重试。证据 acceptance.json 与两项 report.md。

2026-10-08 最终复验通过：Codex P2G5-001 attempt 1 completed，full 生效；产物 37 bytes 精确匹配，Core 在最终 callback 前接受 progress sequence=1，最终 marker 有效、RunLease closed、ui_notification succeeded，官方 archive/delete acknowledged 且 CLI absent。报告 Failure code=none，历史 timeout 保留。结合 Claude Q3K7-001，关闭本次 Codex/Claude full 进度回执与成功重试报告收尾项；不据此关闭 Windows 正式安装、剩余恢复矩阵或 1.06 OpenCode 项。证据 full-repair-20261008/retry-acceptance.json、P2G5-retry-report.md。
