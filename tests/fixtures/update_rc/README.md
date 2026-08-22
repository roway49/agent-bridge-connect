# UPD-103-001 Update RC E2E fixtures

This directory holds fixtures for the isolated two-version Update RC E2E
driver (`scripts/run_update_rc_e2e.py`) and its unit tests
(`tests/test_upd103_001_two_version_e2e.py`).

The driver builds deterministic 1.0.2a1 / 1.0.3a1 test packages and runs the
real `agentbc update` flow entirely inside a freshly created temporary
HOME / install / bin / config / workspace / spool hierarchy.  It never
discovers or writes the real user installation or Runner, and no test CA,
feed, wheel, fault package, or temporary version ever enters formal release
assets.

## Exact real-run invocation (opt-in)

The slow real two-version run is gated behind the explicit environment gate
`AGENTBC_E2E_RUN_REAL=1`.  Build-from-source example:

```sh
AGENTBC_E2E_RUN_REAL=1 python3.12 scripts/run_update_rc_e2e.py \
  --scenario post_identity \
  --old-src /path/to/agentbc-checkout \
  --new-src /path/to/agentbc-checkout \
  --old-version 1.0.2a1 \
  --new-version 1.0.3a1
```

Scenarios: `success`, `setup_refresh`, `runner_start`, `post_identity`.

Preview the same plan without executing anything:

```sh
python3.12 scripts/run_update_rc_e2e.py --plan \
  --old-src /path/to/agentbc-checkout \
  --new-src /path/to/agentbc-checkout
```

Pre-built wheels can be supplied with `--old-wheel` / `--new-wheel` instead
of source trees.  `--keep` preserves the isolation root for inspection (the
isolated Runner is still stopped).  `--out-evidence FILE.json` writes the
machine-readable evidence document.
