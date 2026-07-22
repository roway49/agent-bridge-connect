# Quick Start

[中文](QUICK_START_ZH.md) | English

This page contains only the Public Alpha deployment flow. Task, Runner,
recovery, and uninstall commands are documented in the
[User Guide](USER_GUIDE.md).

## 1. Check Requirements

- macOS on Apple Silicon or Intel;
- Python 3.10 or newer;
- at least one installed and authenticated executor: Codex, Claude Code, or Hermes.

## 2. Download And Verify

Download the Alpha archive and its `.sha256` file from the same release:

```bash
shasum -a 256 -c agentbc-1.0.1A-macos-local-alpha.tar.gz.sha256
mkdir -p "$HOME/AgentBC-Alpha"
tar -xzf agentbc-1.0.1A-macos-local-alpha.tar.gz -C "$HOME/AgentBC-Alpha"
cd "$HOME/AgentBC-Alpha/agentbc-1.0.1A-macos-local-alpha"
shasum -a 256 -c SHA256SUMS
```

Do not install the bundle if either checksum command fails.

## 3. Install

```bash
./install_local_alpha.sh ./agent_bridge_connect-1.0.1a1-py3-none-any.whl
```

The installer creates an isolated environment, detects local executors,
installs their AgentBC integrations, runs setup, and starts Runner.

## 4. Refresh The Shell

Open a new terminal and new agent sessions so the shell and each client reload
their command and skill catalogs. If the command is not yet on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 5. Verify Deployment

```bash
command -v agentbc
agentbc --version
agentbc setup --show
agentbc runner status
```

All four checks should succeed before dispatching work. The release bundle also
contains a package-only smoke test that does not launch an agent:

```bash
./run_local_alpha_smoke.sh
```

Continue with the [User Guide](USER_GUIDE.md) to create, inspect, hand off,
recover, and close tasks.
