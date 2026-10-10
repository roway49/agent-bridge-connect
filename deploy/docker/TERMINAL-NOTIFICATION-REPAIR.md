# Docker 宿主结束通知


### 2026-09-30：Docker 完成／失败弹窗修复（r11 已安装）

根因：容器 stdout 通知曾返回成功，Core 将其记为 ui_notification=succeeded；宿主桥只处理审批，未处理任务结束通知。现容器日志不再作为 UI 成功证据，UI 阶段等待宿主握手；deferred 不占用重试预算。Native 通知行为保持原合同。

宿主桥新增独立串行结束通知队列，完成／失败／取消使用任务终态投递 ID 绑定。实际 macOS 信息弹窗返回成功后才确认 Core；仅 OK，不执行权限操作，不直接打开容器路径，提供 agentbc task report 命令。已显示但回写失败保存 ack_pending，只重试回写，不再显示；进程在 UI 中断且结果未知时记 manual_action_required；明确投递失败最多三次并退避。私有日志不保存通知正文或凭据；doctor 可见 terminal_notifications 状态。容器维护使用非阻塞任务投递锁，等待弹窗不阻塞审批和 IPC。

历史纠正限定显式 Task ID 且有精确 headless terminal notification written 事件。仅重置错误的 UI 投递阶段，不重跑业务，不改终态、回调、报告或租约；已存在真实宿主确认的事件拒绝再次纠正。文件通知重试产生的空 dialog_message 不掩盖真实 UI 事件。

安装态：本机已更新至 r11，镜像 sha256:2e0ed2e2b07e7ae84c4539bf68250dc0b99ba54d564802fc2161be0cbeb70d75。包 dist/docker-install-candidate-r11/agentbc-1.0.4a1-docker-macos-arm64.tar.gz，SHA256 f080970ce3a0b9146d4515c445e9d4675a21e204967344b1033ce767c6b1d7e2。仍为 1.0.4a1 开发身份。

四条真实漏通知已补发并取得系统侧成功回执：5FPM-001 failed（原因为拒绝提权），SZXS-001、F9H8-001、23XA-001 completed。四者租约 closed，后三者 callback=true；UI succeeded 且各一次尝试，业务状态不变。5FPM 的首次弹窗验证发生在 r10，另三条在 r11，不能混称四条都在同一版本首次送达。已向老板请求可见性反馈，尚不能代替老板肉眼确认。

自动化：四层全量 2284 tests / 115.983s / OK；通知定向 86 项通过；前端 18 项、类型检查、构建通过；Ruff、75 文件测试结构、仓库边界、公共发布 index 边界、diff 检查通过，未提高预算。覆盖 pending/ack 绑定、失败与取消、重复扫描、旧记录保护、确认回写丢失、重启不重复及失败重试上限。没有新增模型调用或重试旧任务。本修复不等于四 Executor 同镜像正向业务验收全部通过。
