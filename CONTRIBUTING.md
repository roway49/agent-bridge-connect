# Contributing

AgentBC welcomes focused bug fixes, documentation improvements, executor probes,
and reproducible compatibility reports during the Public Alpha.

## Development Setup

```bash
git clone https://github.com/roway49/agent-bridge-connect.git
cd agent-bridge-connect
uv tool install --force -e .
agentbc setup --show
```

Use a dedicated branch and a disposable project for executor tests. Do not use
customer data, credentials, or an unrecoverable working tree.

## Local Checks

Before opening a pull request:

```bash
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache \
  python3 -m compileall -q src
git diff --check
./scripts/build_local_alpha_bundle.sh
./dist/agentbc-v1.0.1A3-macos-local-alpha/run_local_alpha_smoke.sh
```

The package smoke test does not require Codex, Claude Code, or Hermes. Changes
to executor behavior should also include the exact executor version, discovery
path, command, status/report evidence, and a minimal reproduction.

## Pull Requests

Keep changes scoped. Describe:

- the user-visible problem;
- the behavior before and after the change;
- path, task-state, and cleanup implications;
- checks performed and any untested executor;
- migration or compatibility impact.

Changes must preserve task/report truth, customer-path safety, callback
idempotency, single-Runner ownership, and explicit recovery behavior.

Use the private reporting process in [SECURITY.md](SECURITY.md) for
vulnerabilities rather than a public issue.
