# 快速开始

中文 | [English](QUICK_START.md)

本文只保留 Public Alpha 的基础部署流程。任务、Runner、恢复和卸载命令统一见
[用户指南](USER_GUIDE_ZH.md)。

## 1. 检查环境要求

- Apple Silicon 或 Intel 芯片的 macOS；
- Python 3.10 或更高版本；
- 至少安装并登录一个执行器：Codex、Claude Code 或 Hermes。

## 2. 下载并校验

打开 [AgentBC 1.0.1A Release](https://github.com/roway49/agent-bridge-connect/releases/tag/v1.0.1A)
查看发布说明和资产。推荐使用一行安装命令：它会下载发布校验文件、验证压缩包、
在隔离环境中安装 AgentBC、执行 setup，并运行纯包 smoke test：

```bash
curl -fsSL \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A/install-agentbc-alpha.sh \
  | sh -s -- \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A
```

如需手动安装，请从同一个 Release 下载 Alpha 压缩包及其 `.sha256` 文件，随后
依次校验压缩包和包内文件：

```bash
shasum -a 256 -c agentbc-1.0.1A-macos-local-alpha.tar.gz.sha256
mkdir -p "$HOME/AgentBC-Alpha"
tar -xzf agentbc-1.0.1A-macos-local-alpha.tar.gz -C "$HOME/AgentBC-Alpha"
cd "$HOME/AgentBC-Alpha/agentbc-1.0.1A-macos-local-alpha"
shasum -a 256 -c SHA256SUMS
```

任意一条校验命令失败时都不要继续安装。

## 3. 安装手动下载的压缩包

```bash
./install_local_alpha.sh ./agent_bridge_connect-1.0.1a1-py3-none-any.whl
```

安装脚本会创建隔离环境、识别本地执行器、安装 AgentBC 集成、执行 setup，
并启动 Runner。

## 4. 刷新终端与 Agent 会话

新开一个终端和新的 Agent 会话，让 shell 与各客户端重新加载命令和 Skill。
如果命令尚未加入 `PATH`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 5. 验证部署

```bash
command -v agentbc
agentbc --version
agentbc setup --show
agentbc runner status
```

以上四项都成功后再开始派发任务。Release 包还提供不启动 Agent 的纯包验证：

```bash
./run_local_alpha_smoke.sh
```

下一步请阅读[用户指南](USER_GUIDE_ZH.md)，了解任务创建、查询、handoff、
恢复与关闭流程。
