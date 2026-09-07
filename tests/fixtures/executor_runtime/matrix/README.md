# Executor protocol fixture matrix (PROTO-104-001)

This directory is the **single authoritative copy** of every executor protocol
fixture.  It replaces the previously loose `tests/fixtures/executor_runtime`
files; do not recreate sibling copies outside `matrix/`.

## Layout

```
matrix/
  manifest.json                       # single source of truth: statuses, hashes, bounds
  <executor>/<version>/<surface>      # one directory per recorded version
  codex/shared/                       # surfaces shared across codex versions
```

*executor* ∈ {`codex`, `claude`, `hermes`} — *surface* names describe what they
freeze (`version`, `argv_contract`, `session_events`, `decisions`,
`resource_exhaustion`, `partial_progress`, `callback_failures`, plus the
executor-specific App Server / project-purge / ACP surfaces).

## Version status rules

| status            | meaning                                                            |
| ----------------- | ------------------------------------------------------------------ |
| `supported`       | inside the production bounds enforced by code                      |
| `candidate`       | real official-probe evidence captured, but **not** in production    |
| `pending_capture` | declared baseline with characterized expectation only              |

A candidate never widens a feature contract on its own. Codex App Server
support is version-agnostic: any release or fork is accepted when its official
generated schema contains the required protocol surface. Other executor
capabilities retain their documented policies.

Production bounds currently enforced by code:

| executor | bound | constant |
| --- | --- | --- |
| codex | CLI freeze `0.146.0`; App Server protocol-surface detection | `_CODEX_FROZEN_VERSION`, `CODEX_APP_SERVER_REQUIRED_PROTOCOL` |
| claude | path capability `[2.1.216, 2.2.0)` | `CLAUDE_PATH_CAPABILITY_MIN_VERSION` / `..._MAX_VERSION` |
| hermes | cleanup help freeze `0.17.0`; ACP protocol version `1` | `_HERMES_FROZEN_VERSION`, `HERMES_ACP_PROTOCOL_VERSION` |

## Capture workflow

Use only the controlled capture tool:

```bash
python3 tests/fixtures/executor_runtime/tools/capture_protocol_fixture.py capture \
  --executor hermes --binary "$HOME/.local/bin/hermes" --version 0.20.1 \
  --staging-dir /tmp/proto104-staging
python3 tests/fixtures/executor_runtime/tools/capture_protocol_fixture.py review \
  --executor hermes --version 0.20.1 --staging-dir /tmp/proto104-staging
python3 tests/fixtures/executor_runtime/tools/capture_protocol_fixture.py promote \
  --executor hermes --version 0.20.1 --staging-dir /tmp/proto104-staging
python3 tests/fixtures/executor_runtime/tools/capture_protocol_fixture.py verify
```

The tool probes only official read-only surfaces (`--version`, `--help`,
subcommand `--help`, `acp --check`, `acp --version`, the generated App Server
schema) and redacts home paths, private session paths, tokens, prompts and
environment dumps before anything reaches staging. Review prints the redacted
unified diff against the current authoritative copy plus SHA-256 hashes;
promote refreshes the manifest entry. Nothing may be written into this tree by
hand.

Contract tests live in:

* `tests/test_proto104_001_fixture_matrix.py` – manifest integrity, production
  bound agreement, candidate isolation, named Codex capability groups,
  fail-closed completeness, redaction invariants.
* `tests/test_proto104_001_protocol_surfaces.py` – fixture samples pushed
  through the real production parsers per executor/version.
* `tests/test_proto104_001_capture_tool.py` – capture tool whitelist,
  redaction and manifest-verification behavior.
