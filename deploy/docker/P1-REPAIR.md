# Docker P1 修复与验收（2026-09-29）

本轮范围：Codex 容器会话清理、任务日志读取。尚不代表四 Executor 同候选业务闭环或 Mac 完整替代验收通过。

## 行为

- Native Codex 继续使用原 Desktop 归档合同。受管 Docker Runner 使用独立策略 `container_session_archive_then_delete`，要求官方绑定、retain=false、已关闭租约及原有任务结束投递门禁。
- 先调用容器内官方 App Server archive，再调用 delete，最后通过新连接 read 验证不存在；任何一步失败都不记成功。Desktop 归档标记为 not_applicable，不生成 Desktop 确认。
- 容器模式由受管 Linux Runner 入口与 Compose 设置；入口设置兼容旧宿主更新器生成的 Compose。宿主普通 CLI 不启用该路径。
- 原有 waiting_for_desktop 的 pending 回执可在更新后的容器维护周期继续处理；retain、未绑定、活动租约等保护不变。受管 skill 说明容器清理由 Runner 处理，不再为此创建 Desktop 归档 heartbeat。
- 清除活动执行指针时保留日志 Worker ID；旧记录按同一 Task 的 worker_dispatched 事件只读回溯。Executor run ID 无法解析时尝试 Worker 日志。
- Runner 重启后可读取自身 runs 目录内终态日志，不收养历史 PID，不接受路径穿越、目录或日志符号链接；日志跟随切换 run 时重置偏移。

## 验证与曾遇问题

- 模拟协议验证 archive → delete → fresh read，归档失败不删除，未验证消失不成功；retain、未绑定和 Native 调用均不删除。遗留 pending 回执迁移通过。
- 隔离真实 Codex 官方接口验证：无外网、无凭据、无宿主挂载，创建专用测试会话，以本地不可达 provider 形成持久化记录后归档、删除、新连接确认不存在。无真实模型用量。只调用 thread/start 的空会话尚未落盘，早期探测因此失败，未伪造成功。
- 日志测试覆盖 Runner 重启、历史事件恢复、任务状态不变、路径穿越与符号链接隔离。
- 完整回归先暴露合同枚举未同步、错误文案兼容、架构预算超限，均已修复且未提高预算。安装测试隔离真实 PATH/共享入口，未放宽安装互斥。
- OpenCode 测试删除目录时仍有 Runner 收尾写入：测试现在等待自身 watcher 结束。callback-before-timeout 用例改为先确认假进程已输出，再计原有超时，避免把进程调度延迟误作协议失败；原有断言保留。
- 最终包及全量结果见 INSTALLATION-ACCEPTANCE.md 同日 P1 条目。

## 更新后人工复核

1. 使用本文最终候选执行同类型离线 update，不卸载现有 Docker，不绕过未结任务检查。
2. agentbc doctor：确认新镜像身份、Runner 与审批桥正常。
3. agentbc task status WPMK-001：核对业务终态保持 completed；清理回执应使用容器策略，并取得真实 archive/delete/absence 证据。不得用 Desktop 工具归档其他任务代替。
4. agentbc task logs WPMK-001：应能回溯该任务的 Worker 日志。日志已被实际删除时不承诺恢复丢失文件。
5. 无未结任务时重启 Runner，再读取同一任务日志并检查 WebUI。停止服务不取消任务。
6. 清理官方成功后再处理已暂停的旧 heartbeat；当前开发过程不伪造确认、不恢复业务任务。

正式安装替换、遗留真实会话清理与同候选四 Executor 全流程仍须逐项复核。Linux/Windows 和 1.06a 待办不因此关闭。


### 2026-09-29：老板授权本机替换与 P1 安装态验收（r9）

已直接替换本机正式 Docker 安装，保留原安装身份、配置、记录、报告及固定端口 58013。最终安装 r9，镜像 sha256:01d240b4f7b16449fd5a269e3b8460b9f323f3dcce81a94f21af994eb4f9031b；包 dist/docker-install-candidate-r9/agentbc-1.0.4a1-docker-macos-arm64.tar.gz，SHA256 8860f04ad89086d483ecbd823aec9aa2a3f518d9d34716e2a5bcd421d567d4d5。r7/r8 为中间候选。

安装态追加修复两项：r6 未改动的受管 skill 因模板变化被误判 modified，采用已知旧模板完整指纹匹配允许升级，用户实际修改仍拒绝；Runner 重启未登记配置任务目录，现仅在允许根内登记配置 board_root，后台维护无需新任务触发。第一次更新失败已成功回退 r6，之后 r8/r9 同类型更新成功，三平台宿主 skill 正常更新。

真实验收：WPMK-001 保持 completed、callback/report 已有、租约 closed；43YY-001 保持 failed、租约 closed。两者 exact Codex 绑定、retain=false 均经核对，cleanup 均 succeeded，官方 App Server archive/delete acknowledged，新连接 cli absent，Desktop not_applicable。未调用 Desktop 归档、未伪造 acknowledgement、未重试业务任务；两条旧暂停清理 heartbeat 已删除。

WPMK-001 历史 Worker 日志可读，正常 stop/start Runner 后仍可读；WebUI close/start 后仍使用 58013，静态页、列表、详情、报告 200，无鉴权 API 401，业务状态未改变。Runner、WebUI 恢复运行，审批桥 ready。最终源码四层 2278 tests / 99.877s / OK，升级定向 33 项及 Runner/架构定向 73 项通过，Ruff、测试结构、仓库边界及 diff 检查通过；预算未提高。

本轮为 P1 安装态验收，没有新增模型任务或模型用量，不代替四 Executor 在 r9 同镜像上的新任务闭环验收，也不关闭 Linux/Windows 人工项。
