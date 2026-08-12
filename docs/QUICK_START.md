# Quick Start

[中文](QUICK_START_ZH.md) | English

This page contains only the Public Alpha deployment flow. Task, Runner,
recovery, and uninstall commands are documented in the
[User Guide](USER_GUIDE.md).

## 1. Check Requirements

- macOS on Apple Silicon or Intel;
- Python 3.10 or newer;
- at least one installed and authenticated executor: Codex, Claude Code, or Hermes.

## 2. Install From PyPI

Install the published Alpha package from the
[AgentBC PyPI project](https://pypi.org/project/agentbc/), then run setup to
discover local executors, install their AgentBC integrations, and start Runner:

```bash
python3 -m pip install agentbc==1.0.1a3
agentbc setup
```

The explicit version pin keeps Alpha deployments reproducible. To use the
checksummed GitHub bundle instead, continue with the next section.

## 3. Install From A Verified GitHub Release

Open the [AgentBC 1.0.1A3 release](https://github.com/roway49/agent-bridge-connect/releases/tag/v1.0.1A3)
to review the release notes and assets. The recommended one-command installer
downloads the release checksum manifest, verifies the bundle, installs
AgentBC in an isolated environment, runs setup, and performs a package smoke
test:

```bash
curl -fsSL \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A3/install-agentbc-alpha.sh \
  | sh -s -- \
  https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.1A3
```

For a manual installation, download the Alpha archive and its `.sha256` file
from the same release, then verify both the archive and its contents:

```bash
shasum -a 256 -c agentbc-v1.0.1A3-macos-local-alpha.tar.gz.sha256
mkdir -p "$HOME/AgentBC-Alpha"
tar -xzf agentbc-v1.0.1A3-macos-local-alpha.tar.gz -C "$HOME/AgentBC-Alpha"
cd "$HOME/AgentBC-Alpha/agentbc-v1.0.1A3-macos-local-alpha"
shasum -a 256 -c SHA256SUMS
```

Do not install the bundle if either checksum command fails.

### Install A Manually Downloaded Bundle

```bash
./install_local_alpha.sh ./agentbc-1.0.1a3-py3-none-any.whl
```

The installer creates an isolated environment, detects local executors,
installs their AgentBC integrations, runs setup, and starts Runner.

## 4. Refresh The Shell And Agent Sessions

Open a new terminal and new agent sessions after either installation method so
the shell and each client reload their command and skill catalogs. If the
command is not yet on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 5. Verify Deployment

```bash
command -v agentbc
agentbc --version
agentbc setup --show
agentbc runner status
agentbc doctor
```

All five checks should succeed before dispatching work; `doctor` exits `0` when healthy, `1` on
warnings (for example installed-Skill drift), and `2` when the installation is unavailable. The
release bundle also contains a package-only smoke test that does not launch an agent:

```bash
./run_local_alpha_smoke.sh
```

## 6. Command Surface At A Glance

The [User Guide](USER_GUIDE.md) is the full command and behavior contract. These are the fixed
commands it covers:

- `agentbc claude budget <usd>` / `agentbc hermes max-turns <turns>`: resource defaults for
  future executor runs; each task freezes its effective values at dispatch.
- `agentbc session retention status|enable|disable`: executor temporary-session retention.
  Cleanup is background and user-transparent and never deletes dispatcher conversations.
- `agentbc record clean`: removes only eligible terminal-task runtime diagnostics; `task.json`,
  indexes, and reports are always preserved — reports are never deleted.
- `agentbc task close <TASKCODE>`: closes the current queued (pending) or active chain head;
  terminal and stale iterations are rejected.
- `agentbc doctor`: read-only health check with the fixed exit-code contract `0` healthy /
  `1` warning / `2` unavailable.
- Permission modes `inherit` / `safe` / `full` via `--permission-mode`; a `safe` task may stop
  with an approve/deny `permission` input to request a one-time `full` continuation.

Continue with the [User Guide](USER_GUIDE.md) to create, inspect, hand off,
recover, and close tasks.
