# Mac Docker 人工验收流程（2026-09-28，待四 Executor 自动验收完成）

## 当前可验证状态

- 实例：`agentbc-mac-flow-runner-1`，当前运行的是 `agentbc:mac-four-dev` 四运行时本地验收镜像。
- OpenCode：`CNRR-001` 已完成；callback 有效、报告存在、RunLease closed。
- 产物：`/data/workspace/tasks/artifacts/2026-09-28/CNRR/docker-flow-marker.txt`。
- 精确内容：`AGENTBC_MAC_DOCKER_FLOW_OK` 加换行，27 字节。
- SHA256：`f629994b8f6006f12b13e36e4f0a198724ab44b938bebb8b97910e405042356d`。
- 磁盘已释放至约 57 GiB，Docker 已恢复，四运行时镜像构建成功，Runner ready。
- 正式任务：Codex `5VYE-001`、Claude `RP52-001`、Hermes `RQ5N-001`；三项原任务均因缺少宿主入口超时；修复后 Codex `5VYE-002` 已闭环，Hermes `RQ5N-002` 审批回传成功但会话回执错误，Claude 旧任务恢复受阻。尚不能宣告四项通过。
- 已知问题：容器终态桌面通知 `ui_notification_failed` 重试；业务 callback/report/lease 已完成。已增加显式 headless stdout 终态通知通道，审批仍等待人工；CNRR-001 已补齐 5/5 交付阶段，delivery_health=succeeded。此处代表容器日志通知成功，不代表宿主桌面弹窗。

`Dockerfile.mac-runtime` 是使用既有已验证 Linux 依赖缓存的本机验收装配稿，不是可发布的可复现镜像。`runtime_launcher.py` 和 Compose 已通过四 CLI 注册及 Runner 健康检查，业务闭环仍逐项验收。

## 现在可执行：验收 OpenCode

```sh
docker exec agentbc-mac-flow-runner-1 agentbc task status CNRR-001 --root /data/workspace/record --json
docker exec agentbc-mac-flow-runner-1 agentbc task report CNRR-001 --root /data/workspace/record
docker exec agentbc-mac-flow-runner-1 python -c 'from pathlib import Path; import hashlib; b=Path("/data/workspace/tasks/artifacts/2026-09-28/CNRR/docker-flow-marker.txt").read_bytes(); assert b==b"AGENTBC_MAC_DOCKER_FLOW_OK\n"; print(len(b),hashlib.sha256(b).hexdigest())'
```

通过标准：`current_task.status=completed`；每个声明步骤 done；
`extensions.agentbc.final_callback.marker_valid=true`，task_id 对应本任务；
`report_ready=true`；`run_lease_state=closed`；文件精确匹配。
出现 accepted、进程退出 0 或只有产物，均不能替代这组证据。

## 四运行时人工验收

1. `docker exec agentbc-mac-flow-runner-1 agentbc runner status` 应 healthy/ready 且 executors 包含 codex、claude、hermes、opencode；检查安装路径和镜像 ID 与最终验收报告一致。
2. 每个 Executor 的认证、provider、model 使用已确认的隔离配置；API 用户不用统一登录。保留 inherit，不能因失败改 full。凭据不写入步骤、命令参数或报告。
3. 为每个 Executor 分别创建单步新任务，要求只在其 Artifact root 写一个 `manual-marker.txt`，内容为对应 `MAC_DOCKER_<EXECUTOR>_OK` 加换行，再读取核验并输出正式最终回调。每个步骤 YAML 必须先准备并传入容器；只派发一次，保存准确 task ID，不循环重试。
4. 创建命令模板（替换 EXECUTOR 以及步骤文件路径）：

```sh
docker exec agentbc-mac-flow-runner-1 agentbc task create \
  --title 'Mac Docker 人工闭环验收' --assignee EXECUTOR \
  --steps /tmp/manual-EXECUTOR.yaml --source-platform codex \
  --customer-path 'default path' --dispatch \
  --root /data/workspace/record --config /home/agentbc/.abc/config.toml
```

5. 用准确 task ID 查看状态、日志和报告：

```sh
docker exec agentbc-mac-flow-runner-1 agentbc task status TASK-ID --root /data/workspace/record --json
docker exec -it agentbc-mac-flow-runner-1 agentbc task logs TASK-ID --root /data/workspace/record --follow
docker exec agentbc-mac-flow-runner-1 agentbc task report TASK-ID --root /data/workspace/record
```

6. 若出现 input_required，读取准确请求内容和协议版本，由人工确认后使用正式 `task respond`。当前 v3 请求是任务级 full 提权，使用绑定 input ID 的 `--approve-full` 或 `--deny`；只有 v2 才使用 native choice handle；不伪造批准、不更换权限模式。四项基本闭环与“批准/拒绝/超时”权限专题分开记录。
7. 每项均按前述 callback、步骤、报告、closed lease、精确文件五项验收，额外记录镜像 ID、Task/run/revision、模型及文件 SHA256。不得用一项成功代替其他项。
8. 四项终态后再测试容器正常重建，确认已有记录/报告/产物保留。运行中强停、升级回滚、GUI 及 Linux/Windows 是独立待验范围，不在这次基本闭环结果中混算。

人工操作预计 15–25 分钟，模型等待及审批另计。正式 GUI 接入前用上述 CLI；宿主原生 WebUI 不自动读取容器卷。

## 环境恢复后的继续顺序

先检查磁盘余量与 Docker 存储健康，再重新构建/校验本机四运行时镜像。
如 Docker 仍 I/O error，不直接删除镜像库或 volumes。先保全本实例数据，再确定恢复方案。
完成三个未验 Executor 的真实闭环、处理 headless 通知、更新精确任务证据后，才发布四项可人工复核的通过结论。

## 异常重启发现

磁盘耗尽使旧容器异常退出，持久卷保留 runner.pid=7；新容器 PID 恰好也是 7，PID-only 单例检查误判已运行。已确认验收容器停止后，将其旧 PID 文件改名备份并恢复。自动异常重启仍属待修缺陷；不要在运行中删除锁，不要将本次人工恢复视为重启验收通过。

当前本地镜像 ID：`sha256:60b49f4b9bf554c6aeec22b4127d19d972a603538749918c941e0436abf05383`。

## macOS 宿主审批桥（修复入口）

Docker 不具备 macOS GUI 会话，不能在 Linux 容器内调用宿主 osascript。
容器默认仅投递终态日志；人工权限输入必须另有宿主入口。当前开发版提供显式前台桥，
复用 AgentBC 原生弹窗，将人工决定通过容器内的正式 `task respond` 回传。
WebUI 仍为只读，不注入凭据、不挂载 Docker socket。

在 Mac 登录桌面的终端、仓库根目录执行（替换为本次派发的准确任务 ID）：

```sh
PYTHONPATH=src .venv/bin/python -m agent_bridge_connect.docker_approval_bridge \
  --container agentbc-mac-flow-runner-1 \
  --task 5VYE-002 --task RQ5N-002
```

保持终端开启，看到 `Host approval bridge ready` 后等待标题带 `Docker · AgentBC` 的弹窗。
`View Details` 查看原请求及任务步骤；`Approve Full` 是任务级提权，`Deny` 拒绝。
关闭弹窗按现有拒绝逻辑处理；到期由正式审批生命周期处理，不自动批准。
回传成功显示 `decision accepted by container CLI`；仅显示弹窗不能视为审批成功。
用正式 task status/report 验证恢复、最终回调和闭合租约。

桥只监控显式列出的任务，固定绑定启动时的容器 ID，不跟随同名容器替换；
同一容器只允许一个桥进程。每个请求只显示一次，并在回传前重新核对 input、run、
session、fingerprint 和 deadline。过期或变更的请求不回传旧决定。
目前是 macOS 开发验收入口，尚未集成 setup/自动常驻，也未实现 Windows/Linux 原生弹窗。
旧任务 `5VYE-001`、`RP52-001`、`RQ5N-001` 均保留超时失败证据。
Claude `RP52-001` 仍有 `revival_input_unresolved`，不能绕过此门禁重置数据。

宿主弹窗修复及逐项证据见 [APPROVAL-BRIDGE.md](APPROVAL-BRIDGE.md)。
