# 快速开始

中文 | [English](QUICK_START.md)

本文只保留 Public Alpha 的基础部署流程。任务、Runner、恢复和卸载命令统一见
[用户指南](USER_GUIDE_ZH.md)。

## 1. 检查环境要求

- Apple Silicon 或 Intel 芯片的 macOS；
- Python 3.10 或更高版本；
- 至少安装并登录一个执行器：Codex、Claude Code 或 Hermes。

## 2. 通过 PyPI 安装

从 [AgentBC PyPI 项目](https://pypi.org/project/agentbc/)安装已发布的 Alpha
包，然后执行 setup；它会识别本地执行器、安装 AgentBC 集成并启动 Runner：

```bash
python3 -m pip install agentbc==1.0.1a2
agentbc setup
```

固定版本号可以确保 Alpha 部署结果可复现。如需使用带完整校验文件的 GitHub
压缩包，请继续阅读下一节。

## 3. 通过已校验的 GitHub Release 安装

打开 [AgentBC 1.0.1A2 Release](https://github.com/roway49/agent-bridge-connect/releases/tag/v1.0.1A2)
查看发布说明和资产。推荐使用一行安装命令：它会下载发布校验文件、验证压缩包、
在隔离环境中安装 AgentBC、执行 setup，并运行纯包 smoke test：

```bash
curl -fsSL \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A2/install-agentbc-alpha.sh \
  | sh -s -- \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A2
```

如需手动安装，请从同一个 Release 下载 Alpha 压缩包及其 `.sha256` 文件，随后
依次校验压缩包和包内文件：

```bash
shasum -a 256 -c agentbc-v1.0.1A2-macos-local-alpha.tar.gz.sha256
mkdir -p "$HOME/AgentBC-Alpha"
tar -xzf agentbc-v1.0.1A2-macos-local-alpha.tar.gz -C "$HOME/AgentBC-Alpha"
cd "$HOME/AgentBC-Alpha/agentbc-v1.0.1A2-macos-local-alpha"
shasum -a 256 -c SHA256SUMS
```

任意一条校验命令失败时都不要继续安装。

### 安装手动下载的压缩包

```bash
./install_local_alpha.sh ./agentbc-1.0.1a2-py3-none-any.whl
```

安装脚本会创建隔离环境、识别本地执行器、安装 AgentBC 集成、执行 setup，
并启动 Runner。

## 4. 刷新终端与 Agent 会话

无论采用哪种安装方式，都请新开一个终端和新的 Agent 会话，让 shell 与各客户端
重新加载命令和 Skill。如果命令尚未加入 `PATH`：

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
