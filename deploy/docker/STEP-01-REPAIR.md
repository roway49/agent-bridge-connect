# 分步修复 01：Claude/Hermes 闭环

2026-09-28。第一步缺陷修复、真实复验及全量门禁通过；尚不切换正式安装。

## 修复范围

- Hermes 官方退出摘要同时接受旧时间戳 ID 和 ACP UUID，仍校验完整摘要与两个 ID 一致；不从模型散文或私有库补造回执。
- Claude 原生 SDK 超时先终态后维护时，仅对同 run 已关闭租约、已超时的 v3 waiting input 结清；原失败与错误保留，不自动恢复任务。
- 宿主桥、正式 CLI、Runner 和 Core 保留 user / timeout / dialog_closed 拒绝来源；`--denial-source` 不能修饰批准，旧审计记录不改写。
- Claude live 审批答复恢复官方 session 为 active，解决 progress_session_drift；SDK 活跃运行轮询维持租约心跳，终态不再续租。
- fake ACP 测试补齐自身 capability/version，避免无意探测用户的宿主 Runner 消耗测试审批期限；未放宽时间断言。
- 超时候选筛选归入现有 approval_timing 模块。原审批架构 LOC 预算不变，门禁通过。

## 实际验收

| Task | 结论 | 正式证据 |
|---|---|---|
| RP52-001 | 历史超时正确结清 | 仍 failed，input answered，revival eligible；未直接改任务JSON |
| RX6H-002 | Hermes完整最小闭环通过 | 人工批准后 completed，marker_valid=true，step done，报告存在，lease closed，精确标记文件通过 |
| RP52-002 | 最终闭环通过，但发现进度缺陷 | 文件与最终回调正确；progress_session_drift，因此追加修复后复验 |
| RP52-003 | Claude完整复验通过 | 人工批准后 completed；step 1 progress_receipt evidence_source=agent_cli、confirmed_count=1；有效回调、报告、closed lease、无错误 |

Hermes 产物：docker-flow-marker.txt，27字节，精确 `AGENTBC_MAC_DOCKER_FLOW_OK\n`，
SHA256 f629994b8f6006f12b13e36e4f0a198724ab44b938bebb8b97910e405042356d。
Claude 追加产物：approval-progress-marker.txt，27字节，精确 `AGENTBC_CLAUDE_PROGRESS_OK\n`，
SHA256 f670c305e41b38360effa8f71eab77d9fbb637fab8cfe762d041a4a20a440267。

当前最终修复镜像：sha256:1c1f67ecba1a60a5765a9fb13a07b9348036598cbd8913ede2b4d1d97fd07ee9。
Hermes通过记录来自前一修复镜像，最终追加镜像只补Claude状态/心跳；不能据此声称最终安装候选的四格同镜像回归已完成。

## 验证与交付边界

- 上一轮完整回归2227项、209.212秒、OK；新增Claude状态/心跳修复后完整重跑2229项、262.290秒、OK：`/private/tmp/step1-final-full.log`。
- 前端18项通过；镜像构建包含前端类型检查与构建；Ruff、diff、仓库边界、测试结构门禁通过。
- 旧全量的Hermes失败与审批架构预算失败均已有修复和通过证据，不以局部复测替代新全量。
- 各轮旧失败记录保留。宿主正式CLI/skill、Runner和安装未替换，未提交或发布镜像。
- 四Executor此前均有Docker最小闭环证据，但安装入口、自动审批服务、同源可交付镜像、WebUI接线、生命周期、最终统一候选验收仍未完成。
- 下一步按顺序进入CLI/skill与自动审批入口；本步骤通过不等于本机完整替换通过。
