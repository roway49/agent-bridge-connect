# Mac Docker 宿主审批桥验收（2026-09-28）

## 根因与修复

Linux 容器不具备 macOS GUI 会话。此前 headless 通道只接受终态 stdout 通知，
权限输入返回未投递，因而有正式审批记录却没有宿主弹窗。

新增 `agent_bridge_connect.docker_approval_bridge`，显式绑定一个正在运行的容器 ID
和用户指定的任务集合。在容器内调用现有 `build_input_required_notification`，
在宿主复用 `DialogNotifier`，人工决定由容器内正式 `agentbc task respond` 接收。
不修改 WebUI，不挂载 Docker socket，不自动提权，不改宿主安装。

桥采用每容器进程锁、每请求去重和并发弹窗；回传前再次核对请求身份与截止时间。
同名容器重建不会被自动跟随。未显示成功、未知动作、请求替换或过期均不提交批准。
首次真实验证发现 `approve_full` 动作未被映射，已修正并重新要求人工选择，未复用丢失决定。

启动命令见 [MAC-MANUAL-ACCEPTANCE.md](MAC-MANUAL-ACCEPTANCE.md)。
此入口当前为 macOS 开发态前台桥；setup 自动常驻、安装态包装及其他宿主入口尚未交付。

## 真实验证

- 容器：`agentbc-mac-flow-runner-1`。
- 镜像：`sha256:60b49f4b9bf554c6aeec22b4127d19d972a603538749918c941e0436abf05383`。
- Codex `5VYE-002`：宿主弹窗 → 人工批准 → 正式 CLI 接受 → completed。
  正式 callback marker_valid=true，step 1 done，report_ready=true，RunLease closed。
- Hermes `RQ5N-002`：宿主弹窗 → 人工批准 → 正式 CLI 接受 → 标记文件正确；
  后续 `executor_session_receipt_invalid`（execution_session must be an object），
  最终 needs_recovery，lease closed，不能算业务闭环通过。
- 两项文件精确内容均为 `AGENTBC_MAC_DOCKER_FLOW_OK\n`，27 字节，SHA256
  `f629994b8f6006f12b13e36e4f0a198724ab44b938bebb8b97910e405042356d`。
- Claude `RP52-001`：先前超时 failed，仍有 waiting input，正式恢复门禁
  `revival_input_unresolved`。未直接改记录或绕过门禁。

## 自动化验证与边界

`tests.unit.test_approval_dialog_identity` 与 `tests.integration.test_approval_dialog_flow`：
83 tests OK。覆盖 approve/approve_full、deny、未显示、过期、请求替换、未知 v2 handle、
绑定官方 CLI 和原有弹窗详情/关闭/超时行为。Ruff、diff whitespace、仓库边界、75 文件测试结构检查通过。

此前正常本机权限完整回归为 2216 项，3 failures / 4 errors，集中在 Hermes ACP 的
审批过期/恢复竞态与清理；本次局部通过不替代该全量失败。没有发布全量通过结论。
真实拒绝/超时与多宿主人工验收未完成，不能由模拟测试替代。


## 2026-09-28 拒绝来源修复

新增正式 CLI 参数 `task respond ... --deny --denial-source timeout|dialog_closed`，
默认 user；来源参数不能与批准混用。宿主桥传递 DialogNotifier 的拒绝来源，
Runner 与 Core 复用原有超时/关闭拒绝语义，Claude live 通道也保留该来源。
超过绝对截止时间或请求已变更仍丢弃迟到决定，由维护流程结清，不重新授权。
旧 RX6H-001 的 user 记录保留为历史缺陷证据，不根据推测改写审计记录。
