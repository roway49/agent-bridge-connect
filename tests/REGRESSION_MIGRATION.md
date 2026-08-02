# Regression suite migration

- Source: `AgentBC_Temp/public-alpha-20260715/tests`
- Restored: 20 archived `test_*.py` modules and 13 required fixture files.
- Preserved current test: `test_run_lease_finalize_race.py`.
- Excluded: `.DS_Store`, caches, virtual environments, and generated runtime output.
- Product code, packaging, workflows, scripts, and installed skills were not changed.

Current-contract assertion updates:

- `test_phase10c.py`: match the current Hermes skill wording for the default
  executor and executor-selection prohibition.
- `test_phase10d.py`: accept queued-head close planning for a pending task.
- `test_phase10d.py`: expect pending/gray/non-active health before dispatch,
  rather than the retired starting/green/active state.
- Root-level archived guide/monitor dependencies were moved under
  `tests/fixtures/archive_support/`; the public Codex skill check now installs
  the packaged template into a temporary directory before comparing content.

Validation requires the complete suite to discover at least 360 tests with
zero failures or errors, followed by compileall and `git diff --check`.
