# 分步修复 02：宿主 CLI、skill 与审批自动发现

2026-09-28。Mac 隔离安装预览；不替换本机正式安装，不代表 Docker 父项验收。

## 已实现

- `agent_bridge_connect.docker_host install` 在显式指定的空 prefix 创建 `bin/agentbc` 和 0600 实例配置；绑定确切 container ID。容器重建后拒绝继续指向同名替代实例，需显式重新绑定。
- 同时在显式 skill-home 安装 Codex、Claude、Hermes 的完整 skill 包与 Docker 使用约定，复用原有清单与完整性校验；原版完整 skill 可迁移，自定义修改不覆盖。
- 宿主 CLI 以 argv 转发到容器，保留标准输入、TTY 和退出码。显式设置支持命令的容器 root/config，避免 CLI 默认记录目录与配置不同源。
- steps/image/input-file 以容器用户权限快照到独立 UUID 目录，中文和空格路径可用；客户项目仅映射已存在的可写 bind mount，不隐式新增挂载或更换工作目录。
- 宿主命令自动启动 macOS 审批监听，不再要求逐个指定任务或保持终端。监听器直接从绑定记录库发现待审批任务，最多四个活动弹窗，不排队堆积过期请求。
- 每容器锁防重复监听，启动有界就绪检查；未就绪不发送任务命令。答复前再次校验原请求，批准只接受 native dialog 的 user 来源。诊断日志位于 profile 同目录的 `approval-bridge.log`，权限 0600。
- 不启动原生 Runner，不导入 Executor 凭据，不新增 WebUI 写接口。当前入口拒绝 setup/update/uninstall/WebUI 和 Runner 生命周期命令，交由后续安装与容器服务阶段接通；不能回退到宿主原生命令。

## 隔离安装复现

先将本工作区构建的 wheel 安装到独立 Python 3.10+ venv；无需 Claude SDK、宿主 Executor 或源码目录。然后使用该 venv 的 Python：

```sh
python -m agent_bridge_connect.docker_host install \
  --container agentbc-mac-flow-runner-1 \
  --prefix '/path/to/empty/docker-host' \
  --skill-home '/path/to/isolated/test-home'
'/path/to/empty/docker-host/bin/agentbc' runner status
'/path/to/empty/docker-host/bin/agentbc' task list --no-watch
```

将 prefix/bin 放在 PATH 前可使用常规 `agentbc`。本轮没有更改用户 PATH、正式 CLI、正式 skill 或原生 Runner。
首次不建议填写真实 home；先完成阶段 03/04 和最终替换验收。

输入快照保留在 `/data/workspace/host-inputs/<uuid>` 以支持后续引用，当前未实现自动清理。不要在任务运行或后续 handoff 仍需输入时删除。profile 不含 token/API key。

## 本轮实际证据

- 临时 venv：`/private/tmp/agentbc-host-step2/运行 venv`；入口：`/private/tmp/agentbc-host-step2/隔离安装/bin/agentbc`；skill-home：`/private/tmp/agentbc-host-step2/测试用户`。
- 从 `/private/tmp`（非源码 cwd）运行安装后的 wheel；Runner 返回四 Executor ready。
- `7J7A-001` / Claude：宿主 create --dispatch → 自动发现 native SDK 审批 → 正式 `source=user` 批准 → step 1 authoritative progress → completed、valid callback、report ready、closed lease、errors=[]。
- 标记：`/data/workspace/tasks/artifacts/2026-09-28/7J7A/host-cli-marker.txt`，27 字节，精确 `AGENTBC_DOCKER_HOST_CLI_OK\n`，SHA256 `45a3333d94cf3064652c37ff62b6cde6477d392de424c5ada4d3ee8235813818`。
- 业务镜像仍为 step 01 的 `sha256:1c1f67ecba1a60a5765a9fb13a07b9348036598cbd8913ede2b4d1d97fd07ee9`；本轮新增逻辑运行在宿主 wheel。

## 失败证据与未通过项

- `RPGE-001` 暴露宿主入口未显式传 root/config；已修正。不迁移或伪造该历史任务证据。
- `P9ZN-001` root/config 修正后仍失败。官方 OpenCode session 的 read 工具尝试访问 `/data/workspace/tasks/report/2026-09-28/P9ZN/P9ZN-001-task.md`，返回 native permission rejected。终态为 completion_marker_missing，不能因退出码 0 视为通过。
- 首轮验证 steps 还错误要求 OpenCode 提交中途 CLI progress，与 L0 约定不符；后续 OpenCode 验收应只要求最终 step callback。修正文案不等于修复原生权限继续能力。未扩大为 full、未造审批回执、未对不支持 retry/handoff 的 OpenCode 执行恢复。
- 四 Executor 同一最终候选的宿主入口验收未完成。真实 Dispatcher 应用载入新 skill 后发起任务仍需人工联验。
- WebUI 同源接线、固定端口与服务命令、容器重建绑定迁移、自动登录恢复、安装升级回滚和正式替换仍待后续阶段。

## 测试

新增安装/路径/绑定/skill 保护与发现测试写入现有测试文件，保持 75 文件结构；最终全量结果见下方收口记录。构建包含前端类型检查和 Vite。首轮全量运行期间路由新增 root/config，对应旧断言失败，已同步并完整重跑，不用局部复测替代最终门禁。

### 全量中发现的 OpenCode 测试竞态

第二轮全量 2238 项出现 `test_derived_writer_is_reaped_with_its_process_group` 的 PID 文件缺失：0.5 秒超时可能先于 fake shell 的子进程就绪。测试现在在 Popen 返回给执行器前等待本测试的 PID 和写文件信号（有界 5 秒），之后仍使用原 0.5 秒执行超时；保留 cleanup_confirmed、closed lease 和进程不存在的断言。未修改生产超时或杀进程策略。五项进程生命周期定向测试通过，最终再次运行全量。

重复启动验证：从隔离安装目录同时调用两次 `agentbc runner status`，两次 returncode=0、ready=true，审批桥复用一个绑定实例。旧的指定任务监听器不会被当作全量自动监听器复用，避免漏审批。

安装态 wheel SHA256：`c8409aaf5b43b2c93cae807477edab8d46c8b961283d4768e3198e0697098383`。包版本仍为开发基线 `1.0.4a1`，以源码与校验和区分此次 1.05A 开发产物，不当作正式版本发布。

### 最终收口结果

- 全量四层：2239 tests / 233.394s / OK，日志 `/private/tmp/step2-verified-full.log`。包含最新路由、skill、审批锁与 OpenCode 就绪同步，不以局部复测替代。
- 前端：18 tests / 18 pass；wheel 构建包含 tsc --noEmit 与 Vite。
- Ruff（本轮模块与测试）、working-tree 仓库边界、public release boundary（现有 index）、75 文件测试结构及 diff check 通过。尚未提交，index 门禁不等于未跟踪文件的发布验收。
- 本阶段宿主入口/skill/自动发现核心路径已验证。由于 OpenCode 真实入口用例失败及最终四 Executor/真实 Dispatcher 矩阵未闭合，阶段 02 仅分段通过，不能正式替换。

## 2026-09-28 OpenCode 阻塞修复后续

`ANDN-001` 已在保持 inherit 和原配置/认证摘要不变的条件下，通过宿主入口完整执行标记文件写读→有效callback→报告→closed lease。修复的是协议误导的多余 Core brief 读取；OpenCode 原生审批继续仍不支持。详见 `OPENCODE-PERMISSION-REPAIR.md`。最终全量2240项、404.243秒、OK。本条取代本页“OpenCode最小入口用例仍失败”的当前状态，旧失败事实保留；最终同候选全Executor/Dispatcher/WebUI/生命周期矩阵仍待验。
