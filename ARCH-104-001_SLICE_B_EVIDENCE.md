# ARCH-104-001 Slice B — Runner IPC mechanical refactor evidence

Task: `ZG3N-001` (ARCH-104-001 Slice B Runner IPC mechanical refactor)
Branch: `agent/claude`
Baseline: merge commit `35d9555f11bd9537aa133b6f97358dfe94738e58`, tree
`7fc6bff8fed451766294cfc401d9440ab2a60de3` — byte-identical to
`private/integration@cd4e62cd10a3d48e6369f329f1c648775043cc9d`.

## 1. Scope implemented

| Module | Owns |
| --- | --- |
| `runner_contract.py` (new) | `RunnerError`, IPC size constants (`MAX_REQUEST_BYTES`, `MAX_OUTPUT_BYTES`), the IPC channel pattern, `RUNNER_IDENTITY_REFRESH_INTERVAL_S`, the default Runner path contracts, and the `runner.pid` endpoint identity primitives |
| `runner_ipc.py` (new) | `RunnerClient`, `RunnerService`, request authentication/expiry/serialization, response handling, and the single `_dispatch_request` routing chain |
| `runner.py` (kept) | `RunnerState`, process authorization/spawn/reap, task dispatch, maintenance, `create_runner_service`, and the compatibility facade |

`runner_ipc` types the Runner state through the internal `RunnerOperations`
`typing.Protocol`, so it never imports `RunnerState` and no cycle exists.

Production flow is unchanged: `RunnerClient -> _dispatch_request -> one
RunnerState handler`.

`runner.pid` is published by the service and probed by the process layer, so
`_read_runner_pid` / `_pid_is_alive` live in `runner_contract.py` to keep exactly
one definition of that endpoint contract without introducing a cycle.

## 2. What did not change

Verified mechanically, T0 vs T2, by re-running the same capture harness against
the baseline tree and the working tree:

* **All 23 operation request/response fixtures: identical.** Including the exact
  error text for every operation and for an unknown operation
  (`unknown runner operation: <op>`).
* **All module constants: identical** (`MAX_REQUEST_BYTES`, `MAX_OUTPUT_BYTES`,
  `MAX_MANAGED_FILE_BYTES`, `TERMINAL_STATES`, `RUNNER_IDENTITY_REFRESH_INTERVAL_S`,
  `LEGACY_RUNNER_LAUNCH_AGENT_LABEL`).
* **Every public signature: identical**, with exactly one intended exception —
  `RunnerService.__init__` annotates its state parameter as `RunnerOperations`
  instead of `RunnerState`. Parameter names, order, defaults and return
  annotations are unchanged.
* **Health identity**: `_dispatch_request` still reports
  `Path(__file__).with_name("__init__.py")`, which resolves to the same
  `agent_bridge_connect/__init__.py` from either module.
* **No behaviour change**: the moved bodies are byte-identical extractions. Only
  the two `RunnerState` type annotations became `RunnerOperations`.

Baseline conditions preserved and documented rather than "fixed", because Slice B
is a mechanical move and adds no fallbacks:

* `RunnerState._atomic_dispatch_task` returns no `ok` key, so
  `RunnerClient.create_and_dispatch` over live IPC raises
  `runner request failed` **after** completing the full round trip. This is a
  pre-existing baseline condition, identical at T0 and T2 (180/180 rounds on each
  side). It is out of Slice B scope and was not touched.
* An exception outside the service's declared containment set
  (`ABCError, RunnerError, OSError, ValueError, json.JSONDecodeError`) escapes
  `serve_once` instead of becoming an error response. Also unchanged.

## 3. T0 vs T2 metrics

| Metric | T0 (baseline) | T2 (Slice B) | Gate / target | Result |
| --- | --- | --- | --- | --- |
| `runner.py` LOC | 4717 | **3902** | ≤ 3900 expected | −815 (−17.3%); 2 lines over the advisory target |
| Touched Runner-domain LOC (`runner.py`) | 4717 | 3902 | ≥ 5% down | **−17.3% PASS** |
| `runner_contract.py` LOC | — | 71 | new | — |
| `runner_ipc.py` LOC | — | 900 | new | — |
| Combined three-module LOC | 4717 | 4873 | informational | +3.3% (imports + protocol declarations) |
| Functions > 100 lines (all three modules) | 10 | 10 | no regression | unchanged (9 in `runner.py`, 1 in `runner_ipc.py`) |
| Functions > 200 lines | 3 | 3 | no regression | unchanged (all three stay in `RunnerState`) |
| Request routers | 1 | **1** | exactly 1 | **PASS** |
| Handlers per operation | 1 | **1** | exactly 1 | **PASS** (asserted per op) |
| Cross-module hops on the dispatch path | 0 | 1 | informational | +1 static boundary (`runner_ipc` → `runner`); **0 added dynamic frames** |
| Compatibility facade LOC in `runner.py` | 0 | 20 | informational | re-export block only, no wrapper |
| Runtime forwarding wrappers | 0 | **0** | none allowed | **PASS** |

`runner.py` lands 2 lines over the 3900 advisory target. Those 2 lines are the
comment documenting the deliberate `from x import y as y` re-export idiom;
without it a future reader is likely to "clean up" the redundant aliases and
silently break `agent_bridge_connect.runner`'s public imports. The facade itself
is 20 lines.

### Latency (ms)

Absolute timings on this machine drift with background agent load, so the two
trees were measured **interleaved inside one run** — baseline, then working tree,
repeated — and compared by median. This removes load drift from the comparison.

| Metric | Baseline | Slice B | Ratio | Gate (≤ 1.05) |
| --- | --- | --- | --- | --- |
| Cold Runner health IPC (first call) | 30.565 | 30.250 | 0.990 | **PASS** |
| Warm Runner health IPC P50 | 29.841 | 29.626 | 0.993 | **PASS** |
| Warm Runner health IPC P95 | 31.881 | 32.089 | 1.007 | **PASS** |
| Mock create-to-accepted P50 | 164.723 | 167.936 | 1.020 | **PASS** |
| Mock create-to-accepted P95 | 188.966 | 190.195 | 1.007 | **PASS** |

Medians over 5 interleaved rounds x 300 warm health samples / 60 create samples
per side. A second independent 3-round run reproduced the result
(ratios 0.884 / 0.997 / 1.000 / 1.000 / 1.035, all PASS).

**Hard gate: latency ≤ 105% of baseline — PASS on every metric.**

The advisory target of a 5% health-IPC P95 *improvement* is **not met** (1.007).
A mechanical module split removes no work from the request path, so no speedup is
available without the caching or extra-hop changes Slice B forbids; the honest
result is parity.

## 4. Verification performed

* `ruff check src` — clean.
* `python -m compileall -q src` — clean.
* `git diff --check` — clean.
* `python -m build` — `agentbc-1.0.3a2.tar.gz` and
  `agentbc-1.0.3a2-py3-none-any.whl` built; the wheel contains `runner.py`,
  `runner_contract.py` and `runner_ipc.py`.
* `tests/test_core_architecture.py` — 15 tests, OK.
* Focused Runner/policy/spool/doctor set (204 tests) — OK.
* New `tests/test_runner_ipc_architecture.py` — 46 tests, OK.
* Zero circular imports, verified in fresh interpreters:
  `import agent_bridge_connect.runner_ipc` does **not** load
  `agent_bridge_connect.runner`, and `runner_contract` loads neither layer.

### Complete suite, baseline vs Slice B

The full suite was run against a byte-identical extraction of the merge baseline
(`git archive 35d9555`, `runner.py` checksum-verified) and against the working
tree, and the failing-test **ID sets** were diffed:

| | Baseline (`35d9555`) | Slice B |
| --- | --- | --- |
| Tests run | 2032 | 2078 (+46 new) |
| Failures | 3 | 3 |
| Errors | 37 | 36 |
| Skipped | 23 | 23 |
| **New failures/errors** | — | **0** |

The Slice B failure set is a strict subset of the baseline set. The single
difference is `test_release_provenance.NoPublishBehaviourTests.test_build_info_not_permanently_committed`,
which errors on the baseline only because the baseline was run from a
`git archive` extraction with no repository; the test shells out to
`git ls-files` in the repo root. It is an artifact of how the baseline was
materialised, not a behaviour change.

Every one of the 39 pre-existing defects is environmental and unrelated to the
Runner:

* 36 errors: `No module named 'claude_agent_sdk'` — the optional `claude` extra
  (`claude-agent-sdk==0.2.142`) is not installed here.
* 2 further errors from the same cause surfacing as
  `The Claude SDK permission protocol is unavailable.`
* 1 failure: `test_phase10d.test_cli_detects_codex_thread_origin` — no Codex
  Desktop in this environment.
* 1 failure: `test_day2_smoke` — smoke-test transcript formatting.

### New architecture test module

`tests/test_runner_ipc_architecture.py` covers: public import identity and the
absence of forwarding wrappers, the frozen 23-operation routing contract with
exactly one handler per operation, single-router enforcement across the package,
token authentication, missing/foreign token, expiry, request size limit, output
limit, invalid and contained channel names, handler error propagation (RunnerError
and ABCError unchanged, undeclared exceptions escaping), service restart and
singleton release, and live round trips for status, cancel, create, respond,
handoff, terminal delivery and the Desktop archive route.

Existing Runner tests were **not** modified; they continue to pass unchanged.

## 5. Files changed

```
src/agent_bridge_connect/runner.py         | +23  -838
src/agent_bridge_connect/runner_contract.py| new   71 LOC
src/agent_bridge_connect/runner_ipc.py     | new  900 LOC
tests/test_runner_ipc_architecture.py      | new  46 tests
```

No files outside the Runner domain were touched. `service.py`, `control.py`,
`approval.py`, `doctor.py`, `cli.py`, executors, cleanup modules, checklists,
scripts and release files are unmodified.
