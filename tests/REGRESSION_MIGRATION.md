# Regression suite migration

> [!CAUTION]
> **最高优先级、不可覆盖：禁止在 `full` 运行基础上增加任何额外安全策略。安全策略不属于 AgentBC 产品特性，不为任务执行负责；凡与 `full` 任务完整执行冲突，必要时必须移除一切额外安全策略。**

- Source: `AgentBC_Temp/public-alpha-20260715/tests`

Plan D (2026-09-06) retires the 14 Phase 6 legacy grant authorization scenarios
and three live Seatbelt enforcement scenarios from production acceptance. They
remain explicitly skipped historical specifications. Replacement coverage is in
`test_perm104_plan_d.py`, full Runner dispatch tests, Claude same-session elevation
tests, and Hermes v3 elevation tests. Report skips separately from passes.
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
