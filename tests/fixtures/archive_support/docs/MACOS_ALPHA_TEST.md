# macOS Local Alpha Deployment Test

This is the acceptance procedure for a Mac that has never installed AgentBC.
Record every deviation before changing the package or public documentation.

## 1. Prerequisites

Verify:

```bash
uname -m
sw_vers
python3 --version
```

Required:

- macOS on Apple Silicon or Intel;
- Python 3.10 or newer;
- one authenticated executor: Codex, Claude Code, or Hermes;
- the `agentbc-1.0.0a1-macos-local-alpha.tar.gz` bundle.

## 2. Unpack

### Local curl simulation

On the build Mac, from the AgentBC checkout:

```bash
./scripts/serve_local_alpha.sh 8765
```

The server prints a checksum-pinned one-line command. Run that exact command on
the test MacBook while both Macs are on the same network. It downloads the
archive and checksum, verifies both against the pinned SHA-256 value, installs
the wheel, runs interactive setup, starts Runner, and runs the package-only
smoke test. Setup reads confirmations from the terminal even though the install
script itself is piped through `curl`. A checksum mismatch stops installation
before extraction.

The manual transfer path remains available below.

Place the archive and its `.sha256` file in the same directory, then verify the
archive before extracting it:

```bash
shasum -a 256 -c agentbc-1.0.0a1-macos-local-alpha.tar.gz.sha256
mkdir -p "$HOME/AgentBC-Alpha-Test"
tar -xzf agentbc-1.0.0a1-macos-local-alpha.tar.gz \
  -C "$HOME/AgentBC-Alpha-Test"
cd "$HOME/AgentBC-Alpha-Test/agentbc-1.0.0a1-macos-local-alpha"
shasum -a 256 -c SHA256SUMS
```

## 3. Install Without Source Checkout

```bash
./install_local_alpha.sh ./agent_bridge_connect-1.0.0a1-py3-none-any.whl
export PATH="$HOME/.local/bin:$PATH"
command -v agentbc
agentbc --help
```

Expected: `agentbc` resolves from `$HOME/.local/bin` and help contains only
`setup`, `init`, `task`, `worker`, and `runner` top-level commands.
The installer invokes `agentbc setup` before returning, including executor and
skill prompts and background Runner startup. No second setup command is needed.

## 4. Package-Only Smoke

```bash
./run_local_alpha_smoke.sh
```

Expected:

- a temporary board and project are created;
- a shell task reaches `completed`;
- `agentbc-smoke.txt` and a report exist;
- the script prints the evidence directory and exits zero.

This step does not require Codex, Claude Code, Hermes, or Runner.

## 5. Discover Executors

```bash
agentbc setup --show
```

During the setup started by the installer:

- enable only executors that are installed and authenticated;
- confirm the separate Hermes and Claude AgentBC skill prompts when those agents
  are detected;
- decline the optional `abc` alias unless it is specifically being tested.

Verify both skill locations when both agents are installed:

```bash
test -f "$HOME/.hermes/skills/agentbc/SKILL.md"
test -f "$HOME/.claude/skills/agentbc/SKILL.md"
agentbc setup --show
```

Expected: `setup --show` reports both skills as `installed`. Start fresh Hermes
and Claude sessions before checking discovery; sessions opened before setup may
retain a stale skill catalog.

For this Alpha, Hermes setup intentionally installs the AgentBC skill package
to the global Hermes home and every detected `~/.hermes/profiles/<name>` root.
Hermes derives the `/agentbc` command from each installed skill. A future setup
version will let users choose all, current, or selected profiles.

## Reset To A Clean Machine State

Use the product lifecycle command on the test MacBook:

```bash
agentbc uninstall
```

The command separately asks whether to remove managed runtime records/reports and whether
to remove default-workspace artifacts. Both choices default to **No**. It always
stops an AgentBC-owned Runner when detected and removes the Alpha venv, owned
command links, config, temporary download/smoke evidence, Hermes skill packages
from every profile, and the Claude/Codex AgentBC skills. It never traverses or
deletes customer project paths.

For automated disposable-machine testing, pass both decisions explicitly:

```bash
agentbc uninstall --remove-records --remove-artifacts
```

If the CLI executable, Alpha venv, or Python package is damaged, download and
run the independent POSIX shell fallback. It presents the same two
preserve-by-default data decisions and does not invoke `agentbc`:

```bash
curl -fsSL http://BUILD_MAC_IP:8765/uninstall-agentbc-alpha.sh \
  -o /tmp/uninstall-agentbc-alpha.sh
sh /tmp/uninstall-agentbc-alpha.sh
```

For an automated disposable-machine reset:

```bash
sh /tmp/uninstall-agentbc-alpha.sh --remove-records --remove-artifacts
```

Verify the generated config:

```bash
sed -n '1,220p' "$HOME/.abc/config.toml"
```

The config must not contain paths copied from the build Mac.

## 6. Start Runner

`agentbc setup` starts the Runner in the background. Verify it directly, or
restart it without opening a dedicated Terminal:

```bash
agentbc runner start
```

Keep it open. In a second Terminal:

```bash
agentbc runner status
```

Expected: `status` is `ready`, the selected executor appears in `executors`,
and all executable paths belong to the test Mac.

## 7. Real User-Project Task

```bash
mkdir -p "$HOME/AgentBC-Alpha-Project"
printf '# AgentBC Alpha Project\n' > "$HOME/AgentBC-Alpha-Project/README.md"
```

Create `real-task.yaml`:

```yaml
steps:
  - description: Add alpha-result.txt with the text AgentBC cross-machine test passed. Verify the file and report completion through AgentBC.
```

Dispatch with one enabled executor:

```bash
agentbc task create \
  --title "Cross-machine Alpha task" \
  --assignee codex \
  --steps ./real-task.yaml \
  --customer-path "$HOME/AgentBC-Alpha-Project" \
  --dispatch \
  --config "$HOME/.abc/config.toml"
```

Use the returned task code in the remaining commands:

```bash
agentbc task status TASKCODE
agentbc task report TASKCODE
test -f "$HOME/AgentBC-Alpha-Project/alpha-result.txt"
```

Acceptance:

- task reaches `completed` after the executor CLI exits normally, with or without an agent callback;
- task/report Markdown exists under `~/Documents/AgentBC/workspace/tasks/report`;
- compact runtime state exists under `~/Documents/AgentBC/workspace/record`;
- deliverable exists in the customer project;
- no AgentBC report or record directory is created inside the customer project;
- one compact completion notification appears.

## 8. Task List And Cancellation

Dispatch a second task that runs long enough to observe:

```bash
agentbc task list
```

Verify task ID color, route, timer and title. Then close the task:

```bash
agentbc task close TASKCODE
```

For a chain head later than `001`, inspect the printed preservation and artifact
risk summary, then repeat with `--confirm`. AgentBC does not keep an artifact
rollback backup.

Acceptance:

- the task disappears from the current cohort;
- no late completed notification appears;
- the cancelled task's transient Record files are removed;
- customer-project files are not deleted.

## 9. Restart Check

Stop the foreground Runner with `Ctrl+C`, start it again, and verify:

```bash
agentbc runner status
agentbc task status TASKCODE
```

No task may be silently redispatched by Runner restart.

## 10. Evidence To Return

Return:

- macOS version, architecture and Python version;
- installed executor names and versions;
- `agentbc setup --show` output;
- Runner status output;
- package-only smoke evidence directory;
- real task ID and report;
- screenshots of task list and completion notification;
- every error, permission prompt, unexpected path, or manual workaround.

Do not publish the repository until this checklist passes without source-code
edits on the test Mac.
