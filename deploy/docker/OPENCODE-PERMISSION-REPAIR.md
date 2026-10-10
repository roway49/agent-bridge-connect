# OpenCode inherit 模式多余文件读取修复

2026-09-28；对应宿主入口复验失败 `P9ZN-001`。本项仅解决最小执行流程误读 Core 文件，不新增 OpenCode 审批继续能力。

## 原因

- AgentBC 已将完整步骤嵌入执行协议，但同时列出 Core task brief/report 地址。OpenCode 在执行前重复读取项目目录外的 brief。
- 官方会话工具事件明确记录 `read` 访问 `/data/workspace/tasks/report/2026-09-28/P9ZN/P9ZN-001-task.md` 被原生权限拒绝；L0 无审批继续，最终缺少 callback。
- OpenCode 的外部目录权限独立于普通 read；官方默认 external_directory 为 ask。来源：<https://opencode.ai/docs/permissions/>（2026-09-28 核对）。
- 旧验收 steps 另有错误要求中途 CLI progress；这不属于 OpenCode L0 能力，本次 steps 使用最终回调协议。

## 修改

只在 OpenCode 的平台附加规则中明确：完整步骤已经传入；Core brief/report/record 是管理元数据，不是任务启动前必须读取的输入；直接执行嵌入步骤。真正的项目文件和显式必要输入照常读取；必要输入被拒绝时停止并报告，不能复制、更换工具绕过或扩大权限。

未改变工作目录、继承规则、原生配置、认证、挂载或沙箱；未添加 `--auto`、full 参数或权限覆盖。公共三 Agent prompt 与终态合同不变；仍要求真实有效回调。OpenCode 的 safe、审批继续、retry/handoff 仍不支持。

## 实际复验

- 镜像：`sha256:eeaa10f3847aaaac9e296229cd3a4b0ce8f3eb2f99377c9b3d7612bbf07e1775`。
- 原验收数据卷保留，容器更新后 Healthy，四 Executor ready；没有活动任务时才重建。
- 新宿主显式绑定入口：`/private/tmp/agentbc-opencode-fix/host/bin/agentbc`。旧入口仍绑定旧 container ID，不静默改绑。
- Task `ANDN-001`，run `runner-worker-a1e6e2037532`：requested/effective 均 inherit，source=configured_default；completed、Marker valid=yes、Flow contract=yes、report ready、RunLease closed、errors=[]。
- 产物 `/data/workspace/tasks/artifacts/2026-09-28/ANDN/opencode-inherit-marker.txt`，28 字节，精确 `AGENTBC_OPENCODE_INHERIT_OK\n`。
- SHA256 `6f5e30713063980a372a3f3b833366588d5c8ce3a83b2ad615df20437dd748c5`。
- 配置与认证的前后 SHA256 映射完全一致，包括 `/home/agentbc/.abc/config.toml`、OpenCode config 和 auth；未读取输出凭据内容。
- 正式报告本地证据：`/private/tmp/opencode-ANDN-001-report.md`；此前 RPGE/P9ZN 失败记录保留，不重写。

## 边界与后续

该复验证明自包含最小任务可在 inherit 下完成宿主派发→OpenCode执行→报告闭环，不证明任意外部文件可访问，也不意味着 OpenCode 已具有审批继续功能。用户必要输入确实不在原生允许范围时仍会阻塞；不能静默 full 或自动批准。

本次未替换正式安装，未提交或发布；下一步继续 WebUI 同源容器接线，最终同候选四 Executor、真实 Dispatcher、生命周期与三宿主验收仍分别记录。

## 最终门禁

- 针对性协议回归37项通过。
- 四层全量2240项、404.243秒、OK：`/private/tmp/opencode-permission-full.log`。
- 前端18项通过；Docker镜像构建包含前端类型检查及构建。
- Ruff、working-tree仓库边界、75文件测试结构、现有index公共发布边界及diff check通过；未提交或发布。
