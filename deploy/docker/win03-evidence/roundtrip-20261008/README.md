
### 2026-10-08 Windows 正式安装完整往返实机通过

- 按老板授权，直接在 Windows 台式机通过公开安装器/CLI 完成首次安装、完整交互 setup、重复 setup、重复安装拒绝、WebUI close/start、真实卸载、第二次干净安装；全过程退出码与落盘身份均验证，不再要求老板逐项试错。
- 修复旧工作区引导：非空目录提供明确的归档重建/另选目录/取消；默认另选，明确归档前检查未结任务。两次归档均逐文件核对 SHA256 和修改时间；旧 preview 历史继续保留。新工作区为空，宿主原始 AI 配置内容不变。
- 修复 Windows .cmd 自删除后尾部报错：维护尾调用在删除前离开批处理上下文；实机卸载退出 0，无 batch-file 错误，另测真实失败退出 7 保持不变。卸载核对自有容器/网络/桌面任务/入口移除、端口释放、备份和原数据一致，默认删除导入凭据卷且保留镜像。
- doctor 不再持维护锁造成桌面桥误报等待；最终及重复检查均 exit 0，Runner/WebUI healthy、桌面桥 ready。WebUI 鉴权读取 HTTP 200，close 后不立即复活且 Runner 不受影响。
- 当前保留第二次安装供人工验收：安装 ID `4f8b600f1f0a4dcf8fc72564aa659e9d`，端口 `32353`，`agentbc task list` exit 0 且为空。往返脚本中的无 ID `task status` exit 1 是参数不完整，未作为业务验收；独立 final-audit 验证了正确的 task list 入口。
- 唯一推荐候选：Windows `D:\agentbc-tests\win-formal-20261008-roundtrip\output\agentbc-1.0.4a1-docker-windows-amd64.zip`，SHA256 `4da282d0c71abd63a4c6a91c860eb68b45a35dde6a24fd460e1b3a18892ddb1f`，镜像 `sha256:4acf4c483e90be7ea7dd31324f4dfaab5ff1f6c05b2ec26c5006c7ef54a6ffce`。替代此前 final/healthfix/readiness 候选；包版本仍 1.0.4a1，是本地 1.05 开发候选。
- 回归：Python 四层全量 2327 tests / 268.681s / OK；安装/桌面定向 106 项、前端 18 项、类型检查/构建、Ruff 0.15.22、75 文件/40107 行结构门禁、仓库边界及 diff 检查通过。
- 证据：integration `deploy/docker/win03-evidence/roundtrip-20261008/`；Windows 原始脱敏阶段日志位于候选父目录 `cycle-evidence`。本轮不发送模型请求，不替代该候选上的人工业务/重启验收或同类型更新验收；OpenCode Windows 仍为 1.06a。未提交，不改动 Mac 正式安装。
