# AgentBC test suite

AgentBC keeps one `unittest` suite organized by current product responsibility:

- `unit/`: isolated core, task, permission, executor, Runner, and setup behavior.
- `contracts/`: public protocol, callback, projection, compatibility, and fixture contracts.
- `integration/`: Runner IPC, approvals, lifecycle, retry/handoff, cleanup, and transports.
- `release/`: repository boundary, provenance, packaging, update, Homebrew, and wheel checks.
- `fixtures/`: captured protocol samples and immutable test data, never a version allowlist.
- `support/`: shared test-only builders and stable repository paths (maximum four modules).

Run the complete suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

Run an individual layer with, for example:

```bash
PYTHONPATH=src python -m unittest discover -s tests/unit -t .
```

Python 3.10 and 3.14 CI run `unit` and `contracts`; Python 3.11 additionally runs
`integration` and `release`. The three required check names remain unchanged.

New tests belong to the narrowest current responsibility. Do not create Phase, Day,
ticket-number, or version-support modules. Shared executor behavior uses an executor
matrix; only native protocol differences get executor-specific tests. Compatibility
tests cover durable read/backfill/close behavior, not retired execution paths.

`scripts/check_test_structure.py` enforces the file and line budgets, zero permanent
skips, naming rules, `sys.executable` for Python subprocesses, bounded unit/contract
waits, and presence of all four discovery layers. Never satisfy the gate by weakening
assertions, deleting failing behavior, hiding tests outside `test_*.py`, or adding skips.
