# Mac Docker 本机替换验收与进度校对

> 2026-10-10 状态索引：Mac Docker 已内测通过；Windows Codex/Claude/Hermes 已技术性通过；Linux 实际宿主与 Windows OpenCode 推迟至 1.06a。本文早期切片、待实现表和旧实例信息保留为历史，不代表当前状态；现行范围见[开发状态](../../docs/DEVELOPMENT_STATUS.md)与[验收矩阵](acceptance-matrix.csv)。E5 尚待收口。

> 最新安装约束（2026-09-28）：Native 与 Docker 正式安装强制二选一；跨类型必须先卸载再重新安装，不能原地接管。详细规则及待实现门槛见 [INSTALLATION-CONTRACT.md](INSTALLATION-CONTRACT.md)。下文“替换/迁移”均受此约束，现有隔离入口不等于正式安装能力。

校对日期：2026-09-28。老板确认目标：Docker 版完整替换本机已有 AgentBC CLI 和 skill，正常收发任务及 WebUI 监控。

## 验收对象

“替换”指日常继续使用宿主 `agentbc` 命令及各 Dispatcher 的 AgentBC skill，实际业务由 Docker 内唯一 Runner/Executor 实例承担。宿主可以保留已安装的薄命令入口、skill 文件和自动管理的审批桥；不应要求保留原生 AgentBC Runner/业务 Python 环境，也不能依赖源码仓库、开发 venv、临时运行时缓存或逐条手写 docker exec。
宿主 Dispatcher 应用继续存在，Docker 不替代 Codex/Claude/Hermes 客户端本身。

当前结论：**未达到本机替换标准**。仅部分容器业务切片通过；不得卸载或覆盖现有生产安装作为开发前置条件。

## 当前证据（本轮读取正式 task status）

| 项目 | 进度 | 缺口/证据 |
|---|---|---|
| 运行镜像与 Runner | 部分完成 | Mac arm64 四运行时开发镜像、Runner healthy；镜像仍由本机缓存装配，非完整可重建交付包 |
| OpenCode 最小闭环 | 通过此切片 | CNRR-001 completed、callback/report/closed lease，原单运行时镜像证据；仍需最终统一候选镜像回归 |
| Codex 最小闭环及人工审批 | 通过此切片 | 5VYE-002 completed、callback/report/closed lease，宿主弹窗批准已回传 |
| Claude 闭环 | 阻塞 | RP52-001 approval_request_expired；失败后 input 未结清，revival_input_unresolved |
| Hermes 闭环 | 阻塞 | RQ5N-002 needs_recovery；批准回传及文件正确，executor_session_receipt_invalid，未形成最终 callback |
| 宿主 CLI 安装/路由 | 未交付 | 仍用 docker exec；setup/install 未接通 Docker 后端路由与路径映射 |
| 各 Dispatcher skill | 未验收 | 现有 skill 仍要求宿主 installed agentbc；未完成 Docker 安装选择、同步、真实 Dispatcher 发起验收 |
| 宿主审批体验 | 部分完成 | 显式前台桥可用；依赖源码/venv、手工指定 Task ID，未自动启动/发现请求/恢复连接 |
| WebUI | 有独立片段，未接入运行实例 | 主 Compose 无 WebUI/端口；旧片段读宿主 bind，而当前任务在 named volume，需统一数据来源与路径 |
| 配置继承与迁移 | 部分完成 | 已验证选定配置，尚无安装级导入/验证/回滚；API/OAuth 不能被统一登录要求替代 |
| 重启/升级/回退 | 未通过 | 已发现异常退出 PID 重号误判；迁移、重建和升级恢复未完成 |
| 测试门禁 | 未通过 | 上次完整 2216 项：3 failures、4 errors（Hermes ACP）；本轮审批83项通过不能替代全量 |

当前实例：agentbc-mac-flow-runner-1；无宿主发布端口。
四运行时开发镜像 ID：sha256:60b49f4b9bf554c6aeec22b4127d19d972a603538749918c941e0436abf05383。
旧清单“磁盘阻塞/三项尚未派发”已经过时：磁盘已恢复、镜像构建及真实派发已完成，但存在上述业务与产品入口缺口。

## 本机替换通过门槛（全部必须满足）

1. **可安装与可回退**：从候选包/镜像安装，干净目录运行，不依赖源码或 Node；记录镜像/包身份。备份现有配置、skill、任务、报告，完成迁移校验；切换后只有一个负责业务的 Runner，旧服务可恢复且不丢记录。不得在有运行/等待审批任务时强行切换。
2. **CLI 与 skill 等价入口**：原 `agentbc` 命令用法和 exit code/JSON 契约保持；setup、doctor、task create/status/report/logs/respond/retry/handoff、配置及 webui 生命周期路由正确。每个已支持的 Dispatcher 从其实际客户端调用安装后的 skill 完成派发；不用手工指定容器或 task bridge。临时 steps、图片/附件、customer-path、中文/空格路径和报告打开均有稳定映射，不挂整颗宿主 HOME。
3. **四 Executor 完整收发**：同一最终候选镜像分别通过已支持的派发→接收→人工审批（如请求）→执行→最终 callback→Core 报告→租约关闭→终态通知；核对 Task/run/revision 与产物。覆盖现有三 Agent 的恢复/继续及会话保留行为；不额外要求 OpenCode 尚未承诺的 retry/handoff 能力。
4. **审批自动可达**：日常派发后无需额外命令，宿主自动可见任务和权限范围，批准/拒绝/关闭/超时准确回传；重启、重复投递、旧请求与并发任务不串线、不重复提权。无桌面场景明确提供正式 CLI 操作入口。
5. **WebUI 日常监控**：setup 保存固定端口和后台偏好；宿主 `agentbc webui start/close/status` 管理同一容器数据源，启动输出可访问私有链接。真实任务三分类、文字、时长、终态选中保留、报告链接、断线恢复通过；重启保留配置与数据，端口冲突明确报错。页面严格只读，关闭页面/服务不取消任务。pending计时修正和实时对话仍属1.06a，不偷偷扩入本期。
6. **稳定性与安装态门禁**：正常停止/重建、Docker重启、异常退出、升级失败回退可恢复；配置/凭据不入镜像、不静默更换 provider/model。四层回归、前端及仓库门禁全量通过；最后进行停止原生业务服务后的真实日常人工替换验收。

## 下一步顺序与原任务映射

- **04收口**：先修 Claude 超时状态与 Hermes session receipt；同一候选镜像四 Executor 重验，并定位全量 Hermes ACP 回归失败。
- **02/05交付入口**：可重建四运行时安装产物、Docker 后端薄CLI、setup配置继承/路径映射、skill安装同步、审批桥自动管理一起实现，避免把临时脚本当作最终交付。
- **05/GUI05监控**：将独立 WebUI 接到同一 named volume，固定端口/Host/Origin/token、私有链接与报告映射完成真实验收。
- **06生命周期**：修复 PID 陈旧状态，补备份迁移、重建、升级与回退。
- **Mac替换验收门**：上述六项全部通过才允许报告“可完整替换本机版本”。Linux/Windows及双架构仍由07和12+3矩阵独立验收，Mac通过不关闭平台父项。

不按代码行数或局部测试数量给总完成百分比；按以上门槛逐项验收。当前不执行正式安装替换。


## 最新校对：第一步缺陷修复通过

Claude RP52-003 与 Hermes RX6H-002 已完成真实闭环；Claude权威步骤进度上报也通过。
超时残留、UUID退出摘要、拒绝来源、Claude恢复session状态/心跳均已修复。
最后完整回归2229项、262.290秒、OK；前端18项通过。上文阻塞表保留为首次校对历史，
当前证据以 STEP-01-REPAIR.md 为准。下一步为CLI/skill与自动审批入口。
最终同镜像四格、安装态及本机完整替换门槛仍未通过，不据此关闭Docker父项。

## 2026-09-28 宿主入口阶段更新

宿主薄入口、隔离 skill 安装和自动审批发现已有可运行实现，详见 `STEP-02-HOST.md`。
Claude `7J7A-001` 从安装态宿主 CLI 完整闭环，并有 source=user 的自动发现审批证据。
OpenCode `P9ZN-001` 因原生工具权限拒绝失败；四 Executor 同候选入口矩阵未通过。
本轮仍未替换正式安装，WebUI 与生命周期继续待验；阶段 02 为分段验证，不能将表中“未交付”直接改为完整替换通过。

阶段 02 最终代码四层全量 2239 项、233.394 秒、OK；前端18项、类型检查、构建及门禁通过。OpenCode 派生进程测试增加明确就绪同步，保留原超时与退出断言。

OpenCode 后续修复：`ANDN-001` 在 inherit 下通过宿主入口完整闭环，Core管理元数据的多余读取已避免；配置及认证摘要未改变。镜像 `sha256:eeaa10f3847aaaac9e296229cd3a4b0ce8f3eb2f99377c9b3d7612bbf07e1775`，全量2240项通过。详情 `OPENCODE-PERMISSION-REPAIR.md`。继续 WebUI 同源接线；不据此关闭最终同候选四格与正式替换验收。

## Docker WebUI 接线进展

独立只读WebUI已接入Runner同一数据卷、固定loopback 18080端口，宿主显式绑定入口支持start/close/status。真实分类/文字/时长、报告独立打开、断线恢复及生命周期自动验证已有证据；202文件内容与mtime不变。详细记录见 `WEBUI-HOST-ACCEPTANCE.md`。Mac人工结论、Linux/Windows格、最终安装setup/升级回滚仍待验。
