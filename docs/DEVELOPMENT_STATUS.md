# 1.0.5A development status / 开发状态

Updated: 2026-10-10. This page describes the 1.0.5A release proposal, not a published release.
The proposed package version is `1.0.5a1`. Earlier acceptance candidates used
`1.0.4a1`; identify each by image ID, bundle SHA256 and build provenance. Public
1.0.4A installation instructions do not install these proposed changes.

## Delivery and acceptance

| Scope | Current result | Remaining boundary |
| --- | --- | --- |
| Mac arm64 Docker | Internal acceptance passed, including four Executor basic tasks and restart checks | Final release candidate regression remains E5 |
| Windows amd64 Docker | Technical acceptance passed for Codex, Claude and Hermes; install/setup/uninstall, desktop approvals, completion notices and WebUI verified | Windows OpenCode execution deferred to 1.06a; not a four-Executor certification |
| Linux amd64 | Virtual/container environment testing only | Physical Linux host acceptance deferred to 1.06a |
| E1 structured handoff | Accepted | Existing task lineage and frozen failed-task requirements remain authoritative |
| E2 interruption recovery | Accepted | Recovery requires explicit user action; no automatic task retry |
| E3 retry and cleanup | Accepted | Preserve historical failed receipts; never infer old-attempt cleanup from a new success |
| E4 native child | Accepted on Codex CLI 0.153.4: one parent, one native child, official binding, result and individual cleanup receipts | No multi-level or other-Executor claim |
| E5 release validation | Pending | Final code/package identity, affected three-Agent regression, four test layers, frontend and repository gates |

GUI follow-up work is deferred; its insertion version will be decided separately.
Pending duration and live execution/conversation details remain future work, with
1.06a as the recorded requirement target. OpenCode native approval is deferred to
1.06a. Experience polish and independent cleanup-ledger redesign are not added to
this version's remaining execution scope.

## Installation and everyday use

Use an offline Docker candidate matching the host architecture. Native and Docker
formal installations are mutually exclusive: uninstall the existing type before
installing the other. Same-type updates use `agentbc update --bundle <directory>`.
Do not erase task records or installation registration to bypass maintenance gates.

- macOS: `sh install_docker.sh` from the extracted candidate bundle.
- Windows: `py -3 .\install_docker.py` from the extracted candidate bundle.
- Requirements: working Python 3.10+, Docker and Compose. Installation uses bundled
  dependencies; model execution still needs network access and valid credentials.
- `agentbc setup` detects local tools/settings, lets the user link selected API or
  login configurations, configures permissions/monitoring, and installs skills.
  Importing settings does not prove a model connection. Source credentials remain unchanged.
- Windows default workspace: `Documents\AgentBC\workspace`. Project mounts are
  explicitly selected. A clean reinstall preserves old history separately instead
  of resuming old tasks automatically.
- WebUI defaults to the monitoring preference; setup saves a fixed port and asks
  `Keep WebUI running in the background? [Y/n]`. Repeated setup preserves choices.
- `agentbc webui start`, `close`, and `status` operate only the registered installation.
  Start prints an authenticated link. A bare host/port URL is not the private launch link.
  Closing WebUI does not cancel tasks. A port conflict does not silently select another port.
- Use the installed host `agentbc` and skills for business commands. Direct container
  CLI commands in old probe documents are historical development procedures.
- `agentbc approval max-time 300` sets the maximum approval wait to 300 seconds;
  a request's earlier absolute deadline still applies. Completion notices on Windows
  offer OK and close automatically after 30 seconds.

See [Docker entrypoint](../deploy/docker/README.md),
[Windows installation](../deploy/docker/WINDOWS-INSTALLATION.md),
[WebUI](WEBUI.md), and [OpenCode boundaries](OPENCODE.md).

## Evidence and historical records

The [platform matrix](../deploy/docker/acceptance-matrix.csv) records the candidate
actually tested for each row. Different image IDs are historical acceptance, not
proof that all platforms passed the latest Mac image. Detailed evidence resides in
`deploy/docker/`; historical failure and early planning records are retained.
Current status on this page and dated acceptance evidence take precedence over
older documents describing work as pending. No publication, merge or push is implied.
