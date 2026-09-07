# FLOW-104-002 — Independent Terminal Delivery and Cleanup (Evidence)

Task: `YBNW-001` · Branch: `agent/claude` · Base: `82db1da` (fast-forward from `0f59fc5`)

This document records the implementation and the reproduced/verified evidence for
FLOW-104-002. It is a user deliverable in the project root; AgentBC Core owns
the execution report.

---

## 1. Baseline (Step 1)

| Fact | Value |
| --- | --- |
| Worktree at start | clean, `agent/claude` @ `0f59fc50156bbe72c5f4f6083697519a1e38a34f` |
| `private/integration` tip | `82db1daf6fea3cb0fabb3fbc56190b7f95f5cfe0` |
| Behind / ahead | 4 behind / 0 ahead |
| `git merge-base --is-ancestor HEAD private/integration` | `0` (fast-forward safe) |
| Command used | `git merge --ff-only private/integration` |
| Result | `Updating 0f59fc5..82db1da` → `82db1da`, tree clean |

No reset, rebase, branch switch, push or `private/integration` mutation was
performed.

### 1.1 Reproduced coupling (before the change)

`TaskService.finalize_task_from_agent` (`src/agent_bridge_connect/service.py`)
ran one `try` block around report generation and then **two** paths that rewrote
a valid terminal task to `failed`:

```python
try:
    write_report_files(task_id, self.board_root, refresh_index=not self._runner_worker)
except (ABCError, OSError, PermissionError) as exc:
    if Path(report_file).expanduser().is_file():
        ...
        return True
    self.mark_task_failed(task_id, "report_contract_missing",
        f"Agent callback received but report contract failed: {exc}", ...)   # ← (A)
    return True
if not Path(report_file).expanduser().exists():
    self.mark_task_failed(task_id, "report_contract_missing",
        "Agent callback received but report file is missing after report generation",
        ...)                                                                  # ← (B)
    return True
```

`write_report_files` (`src/agent_bridge_connect/reports.py`) serially coupled
three independent side effects: Markdown write → `enforce_task_record_budget`
(50 KiB compaction) → `refresh_task_index`.

Session-cleanup eligibility was additionally gated by two delivery facts
(`src/agent_bridge_connect/execution_policy.py`,
`src/agent_bridge_connect/auxiliary_sessions.py`):

```python
if report_written is not True:
    blockers.append("report_not_written")
if notification_recorded is not True:
    blockers.append("notification_not_recorded")
```

Consequence: a report permission failure or a notification failure left a
terminal task whose executor session was **never** cleaned up, and the task
itself was demoted from `completed` to `failed`.

---

## 2. `agentbc.terminal_delivery` v1 (Step 2)

New module `src/agent_bridge_connect/terminal_delivery.py`.

| Field | Value |
| --- | --- |
| Extension key | `agentbc.terminal_delivery` |
| Receipt version | `1` |
| `delivery_id` | one stable UUID per terminal outcome |
| Frozen facts | `terminal_state`, `terminal_event`, `committed_at` |
| Stages | `report`, `record`, `index`, `file_notification`, `ui_notification` |
| Stage states | `pending`, `in_progress`, `retry_wait`, `succeeded`, `not_applicable` |
| Stage fields | `attempts`, `next_attempt_at`, `last_error_code`, `updated_at` |
| Attempt bound | `TERMINAL_DELIVERY_MAX_ATTEMPTS = 3` |
| Backoff | immediate → `60s` → capped `300s` |
| Event / log | `terminal.delivery` in `delivery.jsonl` (bounded by `append_bounded_jsonl`) |
| Lock | `.delivery.lock` per task directory |

**Created in the same authoritative task write.** `finalize_task_from_agent`
sets `task.extensions[TERMINAL_DELIVERY_EXTENSION_KEY] =
build_terminal_delivery_receipt(...)` immediately before the single
`self.store.write_task(...)` that records the business terminal state, and
`_mark_task_failed_model` does the same for `failed`.

**Bounded and leak-free.** The receipt carries no raw commands, prompts,
notification bodies, secrets or private paths. Stage failure reasons reduce to
stable lowercase codes (`report_write_failed`, `report_missing`,
`record_budget_exceeded`, `record_compaction_failed`, `index_refresh_failed`,
`file_notification_failed`, `ui_notification_failed`,
`delivery_stage_interrupted`, `delivery_stage_uncertain`,
`delivery_not_applicable`). A raw exception string is never stored.

**Survives 50 KiB compaction.** `_compact_terminal_extensions` in
`src/agent_bridge_connect/record_management.py` now copies
`agentbc.terminal_delivery` verbatim alongside the other bounded v1 policy
receipts, so compaction can never drop the delivery plan.

**Cleanup authority unchanged.** The delivery receipt holds no cleanup state.
`agentbc.session.cleanup` (primary) and the auxiliary cleanup receipts remain
the sole cleanup authority; the coordinator only reads
`agentbc.session.session_state` to refuse `needs_recovery` sessions.

---

## 3. Decoupled terminal side effects (Step 3)

### 3.1 Removed completed → failed rewrites

Both `(A)` and `(B)` above are gone. `finalize_task_from_agent` now always
returns `True` for a validated terminal callback and runs the three local side
effects through `_run_terminal_delivery_stages`; every failure is recorded on
the receipt instead of mutating the task status.

`flow_contract_satisfied` still requires callback + steps + report (see §5), so
an unavailable report is visible without demoting the task.

### 3.2 Independently catchable operations

`src/agent_bridge_connect/reports.py` now exposes:

| Function | Stage | Failure surface |
| --- | --- | --- |
| `write_report_markdown` | `report` | `PermissionError`/`OSError`/`ABCError` |
| `compact_task_record` | `record` | `ABCError("record_budget_exceeded")` |
| `refresh_board_index` | `index` | `OSError`/`ABCError` |

`write_report_files` remains as the historical composed entry point (same
signature, same raise-on-first-failure contract) and is now built from those
three operations.

### 3.3 Runner is the production owner

* `TaskService._terminal_delivery_executors` only owns the three idempotent
  local projections. The notification handlers are deliberately omitted, so a
  missing handler leaves those stages `pending` **without consuming an
  attempt**.
* `RunnerState.terminal_delivery` (new `terminal_delivery` IPC op) delivers
  immediately for one exact task; `RunnerClient.deliver_terminal` hands a task
  to the Runner right after finalization (`cli._handoff_terminal_delivery`).
* `Runner.maintain_terminal_delivery` replays only stages that are not
  confirmed and whose backoff has elapsed; it is wired into the same 60 s
  `serve_once` maintenance window as `maintain_waiting_inputs` and
  `maintain_session_cleanup`, and runs **before** cleanup so a notification can
  still reach the user before the session is deleted.
* `TerminalDeliveryCoordinator.maintain_board` skips tasks whose receipt is
  already fully confirmed without taking the per-task lock, so a fully
  delivered board costs one cheap read per task.
* `reconcile_interrupted_stages` converts a persisted `in_progress`
  reservation: notification stages become `retry_wait` with
  `delivery_uncertain` evidence at the capped 300 s backoff; the pure
  report/record/index stages return to `pending` and retry immediately.
* Confirmed stages (`succeeded` / `not_applicable`) are immutable — a replay
  cannot rewrite them or inflate their attempt count.
* `input_required` tasks are never processed and `needs_recovery` sessions are
  never mutated (`terminal_delivery_eligible` + `_eligibility_blockers`).

---

## 4. Cleanup gates removed (Step 4)

| Symbol | Change |
| --- | --- |
| `execution_policy.session_cleanup_blockers` | `report_written` / `notification_recorded` parameters and blockers removed |
| `execution_policy.transition_session_cleanup` | parameters removed |
| `auxiliary_sessions.auxiliary_cleanup_blockers` | parameters and blockers removed |
| `auxiliary_sessions.transition_auxiliary_cleanup` | parameters removed |
| `session_cleanup.SessionCleanupCoordinator._gates` / `_transition` | no longer compute report/notification evidence |
| `session_cleanup.SessionCleanupCoordinator._auxiliary_gates` | same |
| `session_cleanup.SessionCleanupCoordinator._report_written` / `_notification_recorded` / `_report_path` | deleted |

Cleanup eligibility is now exactly: business-terminal task (`completed`,
`failed`, `cancelled`, `rejected`, `needs_recovery`-free), closed RunLease,
terminal session state, `retain=false`, valid exact official session receipt
(including the Codex UUID binding), and an unresolved cleanup receipt. The
accepted Codex archive-acknowledgement-before-delete-acknowledgement ordering is
untouched (`session_cleanup._strict_codex_success_result`). Cleanup failure
stays an independent receipt and never changes task status.

### 4.1 Public projections

* `execution_policy_view(extensions)` now returns `terminal_delivery` and
  `delivery_health`.
* `public_extensions_view` projects `agentbc.terminal_delivery` through
  `terminal_delivery_view` (never echoes it verbatim).
* `generate_report` adds `terminal_delivery` and `delivery_health`, and the
  Markdown summary adds `Terminal delivery` and `Terminal delivery
  outstanding` lines.
* `next_attempt_at` is **excluded** from public projections, matching the
  existing invariant enforced by `tests/test_phase5_public_cleanup_views.py`
  for the session-cleanup receipt.
* `flow_contract_satisfied` stays `false` until the final callback, all steps
  and the report contract are all satisfied.

### 4.2 Legacy import without replaying dialogs

`import_legacy_terminal_delivery` rebuilds a receipt for a historical terminal
record from evidence that already exists on disk:

| Existing evidence | Satisfied stage |
| --- | --- |
| report file exists (`workspace.report_file`) | `report` |
| terminal `notification_delivery` event | `file_notification`, `ui_notification` |
| none of the above | `file_notification` / `ui_notification` = `not_applicable` |

`record` and `index` stay `pending` for Runner. Nothing writes to disk and no
historical dialog is re-shown.

---

## 5. Fault injection and approval isolation (Step 5)

See §7 for the exact commands and results. In short:

`tests/test_flow104_002_fault_injection.py` — **14 tests, all passing.**
`tests/test_flow104_002_terminal_delivery.py` — **35 tests, all passing.**

Every injected fault asserts the three invariants: (1) business terminal state,
final callback and step results unchanged; (2) independent stages continue and
are recorded on the receipt; (3) eligible cleanup still runs with no report or
notification evidence.

| Fault injected | Observed behaviour |
| --- | --- |
| report permission failure (`PermissionError`) | status stays `completed`; `record`/`index` succeed; `report` = `retry_wait`, `report_write_failed`; cleanup runs (`report_not_written` absent) |
| record beyond 50 KiB (≈ 300 KiB of step text) | record compaction succeeds; receipt keeps the same `delivery_id` and exact stages; status unchanged; notifications confirmed independently |
| index failure (`OSError`) | `index` = `retry_wait`, `index_refresh_failed`; `report`/`record` succeed; a later pass retries only `index`; snapshot byte-identical |
| file notifier failure | `file_notification` = `retry_wait`, `file_notification_failed`; `ui_notification` independent; cleanup runs (`notification_not_recorded` absent) |
| UI notifier failure | `ui_notification` = `retry_wait`, `ui_notification_failed`; `file_notification` succeeds; cleanup runs |
| concurrent terminal callbacks | two coordinators race; one `delivery_id`; confirmed stage attempt counts do not inflate; snapshot unchanged |
| Runner death between stage reservation and result | persisted `in_progress` reconciles to `retry_wait` + `delivery_stage_uncertain` + `next_attempt_at = T+300s`; snapshot unchanged |
| Runner restart | new coordinator replays exactly the two outstanding notification stages; `report`/`record`/`index` confirmed stages are never repeated; final health `succeeded` |
| task `input_required` + maintenance | no result, no receipt invented; `agentbc.input`, approval/elevation receipt, `deadline_at`, `dialog_count`, permission mode, Executor session, `worker_count`, `continuation_count` all unchanged; `respond_to_input` and `respond_to_live_claude_elevation` never called (`mock.assert_not_called`) |
| `needs_recovery` session + maintenance | skipped, snapshot unchanged |
| terminal delivery for a sibling while a task waits for input | waiting task's input/elevation/session snapshot unchanged |

### 5.1 Verification commands

```
.venv/bin/python -m unittest discover -s tests -t .        # full unittest suite
.venv/bin/python -m unittest tests.test_flow104_002_terminal_delivery
.venv/bin/python -m unittest tests.test_flow104_002_fault_injection
.venv/bin/python -m unittest tests.test_phase5_cleanup_coordinator \
    tests.test_phase5_cleanup_contract tests.test_phase5_public_cleanup_views \
    tests.test_session_cleanup_codex_v2 tests.test_execution_policy
.venv/bin/python -m unittest tests.test_perm104_002_permission_runtime \
    tests.test_perm104_002_production_routing tests.test_perm104_002_seatbelt \
    tests.test_perm104_001_claude_same_session_elevation \
    tests.test_perm104_plan_d tests.test_permission_modes \
    tests.test_phase6_permission_taxonomy tests.test_phase6_runner_adapter_authorization
.venv/bin/python -m compileall -q src/ tests/
ruff check src/ tests/
/usr/bin/python3 -m build --sdist --wheel --outdir <dist>
git diff --check
```

Exact results are recorded in §7.

---

## 6. Files changed

| Path | Change |
| --- | --- |
| `src/agent_bridge_connect/terminal_delivery.py` | **new** — v1 receipt, stage state machine, projections, legacy import |
| `src/agent_bridge_connect/terminal_delivery_coordinator.py` | **new** — Runner-owned delivery coordinator |
| `src/agent_bridge_connect/service.py` | receipt in the authoritative write; removed both `report_contract_missing` demotions; split/catchable stages; `_sync_terminal_report` keeps legacy path |
| `src/agent_bridge_connect/reports.py` | `write_report_markdown` / `compact_task_record` / `refresh_board_index`; public `terminal_delivery` + `delivery_health`; report Markdown lines |
| `src/agent_bridge_connect/execution_policy.py` | removed report/notification cleanup gates; added delivery projections |
| `src/agent_bridge_connect/auxiliary_sessions.py` | removed auxiliary report/notification gates |
| `src/agent_bridge_connect/session_cleanup.py` | coordinator no longer computes report/notification evidence |
| `src/agent_bridge_connect/record_management.py` | receipt survives terminal compaction |
| `src/agent_bridge_connect/runner.py` | `maintain_terminal_delivery`, `terminal_delivery` op, `deliver_terminal` client, maintenance wiring |
| `src/agent_bridge_connect/cli.py` | `_handoff_terminal_delivery` |
| `tests/test_flow104_002_terminal_delivery.py` | **new** — 35 tests |
| `tests/test_flow104_002_fault_injection.py` | **new** — 14 tests |
| `tests/test_execution_policy.py`, `tests/test_phase5_cleanup_contract.py`, `tests/test_session_cleanup_codex_v2.py`, `tests/test_phase5_cleanup_coordinator.py` | gate kwargs / gate-order expectations updated to the new semantics |

---

## 7. Result summary

All commands were run from the project root with the repository virtualenv
(`.venv`, Python 3.11.15).

| # | Check | Command | Result |
| --- | --- | --- | --- |
| 1 | Full unittest suite (after) | `.venv/bin/python -m unittest discover -s tests -t .` | `Ran 1845 tests in 425.735s` — `FAILED (failures=3, errors=36, skipped=23)` |
| 2 | Full unittest suite (baseline `82db1da`) | same, from a detached worktree at `82db1da` | `Ran 1794 tests in 374.766s` — `FAILED (failures=3, errors=36, skipped=23)` |
| 3 | Failure-set diff | `comm` of the two `FAIL:`/`ERROR:` id lists | **0 new, 0 fixed** (39 identical pre-existing ids) |
| 4 | New FLOW-104-002 tests | `unittest tests.test_flow104_002_terminal_delivery` | `Ran 35 tests` — `OK` |
| 5 | New fault-injection tests | `unittest tests.test_flow104_002_fault_injection` | `Ran 14 tests` — `OK` |
| 6 | Focused FLOW/cleanup/report suites | `test_phase5_cleanup_contract`, `test_phase5_public_cleanup_views`, `test_phase5_claude_cleanup`, `test_phase5_codex_hermes_cleanup`, `test_session_cleanup_codex_v2`, `test_execution_policy`, `test_report`, `test_phase2_record_compaction`, `test_run_lease_finalize_race`, `test_strict_flow_completion` | `Ran 157 tests` — `OK` |
| 7 | Focused permission regression suites | `test_perm104_002_permission_runtime`, `test_perm104_002_seatbelt`, `test_perm104_plan_d`, `test_permission_modes`, `test_phase6_permission_taxonomy`, `test_phase6_runner_adapter_authorization`, `test_phase3_session_lifecycle`, `test_perm103_007_claude_paths` | `Ran 149 tests` — `OK` except the one pre-existing SDK-dependency gate failure listed below |
| 8 | Bytecode compilation | `.venv/bin/python -m compileall -q src/ tests/` | exit `0`, no output |
| 9 | Lint | `ruff check src/ tests/` | `All checks passed!` |
| 10 | Package build | `/usr/bin/python3 -m build --sdist --wheel --outdir <dist>` | `Successfully built agentbc-1.0.3a2.tar.gz and agentbc-1.0.3a2-py3-none-any.whl`; both new modules present in the wheel |
| 11 | Whitespace check | `git diff --check` | exit `0`, no output |
| 12 | Local commit | `git commit` on `agent/claude` | committed locally, not pushed, tree clean |

### 7.1 The 39 pre-existing failures (identical on `82db1da` and after)

* **36 `ERROR`** — every one raises
  `ModuleNotFoundError: No module named 'claude_agent_sdk'`.  The official
  Claude Agent SDK is an optional extra (`[project.optional-dependencies]
  claude = ["claude-agent-sdk==0.2.142"]`) and is not installed in this
  environment.  Affected modules: `test_perm104_001_claude_same_session_elevation`,
  `test_perm104_002_sdk_transport`, `test_perm104_002_production_wiring`,
  `test_perm104_002_production_routing`, `test_perm104_002_transport`.
* **`FAIL` `test_phase10d … test_cli_detects_codex_thread_origin`** —
  `AssertionError: None != 'thread-auto'`; depends on the local `codex` CLI
  session detection.  Reproduced identically on the pristine `82db1da` worktree.
* **`FAIL` `test_day2_smoke … test_init_create_list_and_disk_protocol`** —
  `AssertionError: 'claude' unexpectedly found in '…claude -> codex…'`; depends
  on the executors installed on this machine.  Reproduced identically on the
  pristine `82db1da` worktree.
* **`FAIL` `test_perm104_002_permission_runtime …
  test_start_control_does_not_reject_unknown_cli_version`** —
  `AssertionError: 'approval_control_invalid' not found in
  'claude_sdk_dependency_missing: …'`; same missing optional SDK.  Reproduced
  identically on the pristine `82db1da` worktree.

The comparison method: a detached read-only worktree at `82db1da`
(`git worktree add --detach /tmp/… 82db1da`, importing its own `src/` via
`PYTHONPATH`), the same full suite, then `comm` of the sorted `FAIL:`/`ERROR:`
id lists.  The worktree was removed afterwards; no branch was switched, reset,
rebased or pushed.

---

## 8. Notes and limits

* `TERMINAL_DELIVERY_MAX_ATTEMPTS = 3` bounds each stage; a stage that exhausts
  its attempts stays `retry_wait` with the capped backoff and
  `delivery_health.healthy = false`, so an undeliverable notification is
  observable rather than silently dropped or hot-looped.
* Notification delivery is owned by the Runner. If no Runner is running, the
  notification stages stay `pending` and are delivered by the next Runner
  maintenance pass; the business terminal state and the report/record/index
  stages are already durable.
* The delivery receipt is intentionally *not* cleaned up by
  `record clean`/`task close` bookkeeping beyond the existing terminal-record
  rules; `task.json` (and therefore the receipt) is always preserved.
