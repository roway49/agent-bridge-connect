# Windows Hermes preparation — 2026-10-05

Status: Hermes / DeepSeek approval-to-completion minimal flow accepted on 2026-10-05. Preparation history below is retained; latest acceptance is at the end.

- Windows native Hermes: `%LOCALAPPDATA%\hermes`, version 0.21.5. Standard `.hermes` files are absent; no HERMES_HOME override is set. Native source files remain unchanged.
- `config.yaml`: model default `anthropic/claude-opus-4.6`, provider `auto`, terminal backend `local`, no Hooks/MCP.
- `.env`: DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL present and nonempty. Values were not logged. Auth provider list is empty. Do not silently select a different model.
- Prepared image: `agentbc-win-hermes:20261005`, Linux amd64, image identity `sha256:41c1bdd18c5d0642da63fa5c3616a5834be8551bc4e3254d8ffa6e6cc4662348`.
- Hermes 0.20.6 / Python 3.11.15 / OpenAI SDK 2.24.0. Locked dependencies installed with hashes and pip check. Public source reused from managed runtime image `sha256:408027d39e1668501d7dfce4e56c172cc45caf62fabc4858a8919947797566ea`, `/probe/hermes-source`; uv.lock SHA256 verified against repository lock. No Mac credentials or host virtual environment copied.
- Direct archive download was slow; Windows partial clone had missing historical blobs. Neither was used as unverified build input.
- No business task dispatched, no model call, no production container replacement, no credentials imported yet. Existing Windows Codex preview binding remains intact.

Next: obtain the exact model selection from the owner; privately import Windows configuration into the dedicated Hermes profile, preserve original files, verify Runner allowlists Hermes, then dispatch the already-requested minimal test via installed host CLI. Do not count CLI startup as task acceptance.

Windows build log: `D:\agentbc-tests\win-flow-20261001\hermes-build-20261005.log`.

## Activated after owner selected DeepSeek

The container copy now uses provider `deepseek`, model `deepseek-v4-flash`, and the existing Windows DEEPSEEK_BASE_URL/API key. Original Windows config/.env hashes remain unchanged. Only config.yaml and .env were imported, privately, into the dedicated Hermes profile volume.

- Current container: `17c2816803462baec5df357888f78ddef8e80e8b26a01c283be3f449f0772815`.
- Previous container retained stopped: `agentbc-win-flow-20261001-pre-hermes-20261005`; never start alongside its replacement because data volumes are shared.
- Hermes command: `/opt/agentbc/bin/hermes`.
- Installed host CLI runner status: ready, allowlist includes codex and hermes.
- Desktop bridge: ready, Windows SessionId 1, bound to the replacement container.
- Hermes/ACP imports and CLI help passed with network disabled before importing credentials. Model task completion remains separately unverified.


## 2026-10-05：38K8-001 人工验收通过范围

老板反馈：“任务完成，弹窗和审批内容基本符合预期”。Windows Docker Hermes / DeepSeek（deepseek-v4-flash）本次最小闭环通过：派发 → 原生权限请求 → 人工批准 → 同会话继续 → 最终 callback / Core 报告 → 完成弹窗。

- 安装态 CLI 核验：`status=completed`，step 1 done，`has_final_callback=true`、`report_ready=true`、`run_lease_state=closed`。
- 真实 v3 edit 请求；用户于 `2026-10-05T13:40:38.752724Z` 选择 `approve_full`。请求、通知、人工决定及 full continuation 各一次；没有自动批准。基础模式仍为 inherit，本任务人工授权提权已验证。
- 官方绑定 Hermes session `c34118cc-df05-4671-8599-4ee3d1dc2e19`，resume_count=1。最终回调报告在指定产物目录完成 `hermes-test.txt` 写入及回读。
- delivery `1c20a321-6559-41ae-936c-fd131b917b11` 五阶段均 succeeded；完成 UI 通知 attempts=1。
- Core 会话清理回执 `succeeded`，strategy=`official_session_delete`，attempts=1，无错误。该回执的 phase 仍为 waiting_for_cleanup，细分 verification/commands 为 not_applicable；此处仅确认 Core 成功回执，不宣称额外完成底层物理删除审计。
- 镜像 `sha256:41c1bdd18c5d0642da63fa5c3616a5834be8551bc4e3254d8ffa6e6cc4662348`；仅使用 Windows 原有凭据，原配置保持不变。

证据：`deploy/docker/win03-evidence/38K8-001-status.json`、`38K8-001-report.txt` 及本次老板人工反馈。此次只收口 Hermes 的审批与完成通知最小流程；Codex 启动/旧任务清理问题、锁屏解锁、注销登录、Docker 重启、WebUI close 后审批及维护恢复矩阵仍待验，不关闭 WIN-03 全部工作包、Windows 正式安装或四 Executor 验收。此次仅记录验收，无代码修改、重新派发或替换 Mac 安装。
