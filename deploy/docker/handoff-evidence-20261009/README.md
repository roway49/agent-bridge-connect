# Structured handoff development acceptance

Current verdict: E1 accepted using 4E8V-003 closeout and the preceding Hermes-to-Claude handoff evidence. See the final acceptance section below. Earlier pending/failure sections are historical; E5 remains separate.

Completed-source handoff supports `--steps <yaml>`. IDs must be explicit consecutive integers starting at 1; descriptions must be non-empty strings. Context from `--message` is persisted separately and rendered in the brief/shared prompt, never converted into additional callback steps. The source chain, artifact root and permission inheritance remain unchanged.

Without --steps, the existing single-step contract remains supported. Failed/needs-recovery handoff rejects replacement steps with `handoff_recovery_steps_frozen`; its frozen steps and completed progress continue through the existing recovery implementation.

CLI validates before dispatch. Runner transports the parsed steps and Core revalidates them. Input cannot inject completed status, progress receipts or custom step record paths. YAML mapping duplicate keys are rejected. Docker host forwards --steps through its existing input snapshot path.

Tests cover parser/CLI, RunnerClient/RunnerState, Core persistence, source preservation, inherited permissions, brief/prompt and exact callback step IDs. This is development evidence; no production installation was replaced and no new real model canary was dispatched in this stage.

Next installed canary: use a new candidate, complete a source with one Executor, then handoff to a different Executor with two explicit steps (write a deterministic file; read/verify it), preserving its artifact root. Require both progress receipts, valid final callback, ready report and closed RunLease, with normal human approvals. Do not call the existing installed CLI and assume it contains this change.

## Reproducible usage

```yaml
steps:
  - id: 1
    description: Write the agreed marker file in the inherited artifact directory.
  - id: 2
    description: Read the marker file and verify its exact contents.
```

```sh
agentbc task handoff <completed-source-id> --to claude \
  --steps /absolute/path/handoff-steps.yaml \
  --message 'Continue in the inherited artifact directory.' --dispatch
```

Run this only with a candidate containing this implementation. The message provides context, not a third step. A failed source must use its original recovery steps without `--steps`.

## Final-source validation

- Focused handoff / collaboration / IPC / prompt / CLI: 201 tests passed (6.939s).
- Provenance / architecture / handoff / collaboration: 169 tests passed (5.134s).
- PyYAML path: 49 tests passed (1.464s); default environment covers the built-in parser.
- Frontend: 18 tests passed; TypeScript and wheel resource build passed.
- Ruff, working-tree repository boundary, test structure (75 files / 40282 lines), and diff whitespace checks passed.
- Initial full run found current skill fingerprint and lifecycle LOC gate failures. Current-template fingerprints were updated without changing historical releases; step parsing is now isolated in `task_steps.py`, retaining the service import surface. Budgets were not raised.
- A sandboxed rerun had 12 local socket permission errors; it is not a pass. Identical full regression with local networking permitted passed: 2337 tests in 507.709s, OK.
- Development wheel SHA256: `396c919e427a57b32fbd9dbc0db97ce8c75829d1d48b0042b37320d57b24059f`.
- Wheel location: `/private/tmp/agentbc-handoff-wheel/agentbc-1.0.4a1-py3-none-any.whl` (temporary development artifact).
- Isolated wheel installation exposes `--steps`; invalid input exits 1 before creating a task board. No model request was sent.

## Installed canary 2026-10-09

- Source `4E8V-001` (Hermes): completed, one authoritative progress receipt, valid callback, ready report, closed RunLease, terminal notification and session cleanup succeeded. A real human approval occurred; handoff must inherit the frozen `inherit` base, not that temporary grant.
- Source artifact byte check: `handoff-source.txt` equals `AGENTBC_STRUCTURED_HANDOFF_SOURCE_OK\n`; SHA256 `fc462eca0974c1259143df5df651a5599fd1aad90bfa720ba5894a4f22e687eb`.
- Mac candidate installed through offline update: image `sha256:a27cb179cbd6da951e563c8f7b9dba91e4134fc6538d5008e634195d85975bdb`; bundle archive SHA256 `18427bb667f1131fe0a4bd12bec3a550bb53f10443a747bbc2689ce654ab9748`; wheel hash as above.
- Update initially rejected old managed skills as modified and successfully rolled back. Verified all three skills against the restored old installation templates: all `current`. Backed up complete skill directories under `~/.agentbc-install/backups/handoff-skills-20261009`, preserved extra files, then migrated only verified managed files and reran offline update successfully. Generic same-version skill migration remains an installer defect to track; no permissive integrity rule was added.
- Updated installed CLI exposes `handoff --steps`; Runner ready; WebUI healthy on unchanged port 58013. Source state and artifact root preserved.
- Next dispatch: Claude, two explicitly declared steps, same artifact root, no permission override. Source steps and message context are not additional child steps. Acceptance remains pending its actual terminal evidence.

## 4E8V-002 acceptance: functional pass, closeout needs correction

- Hermes `4E8V-001` -> Claude `4E8V-002`, same artifact root and chain, `inherit` selected from `inherited_task`; context persisted separately, exactly declared IDs 1 and 2.
- Both authoritative progress receipts present, sequence 2. Final marker valid, executor exit code 0, step results exactly 1/2 done, task completed, report ready, lease closed.
- Read-back source SHA256 remains `fc462eca0974c1259143df5df651a5599fd1aad90bfa720ba5894a4f22e687eb`; new file SHA256 `aa185af223dfabd695ff7e8e86518e6f9cfcf8eac6d135d4d510f7d2c29adc9e`. Both exact expected UTF-8 ASCII bytes including final LF. Source preserved.
- Terminal delivery `7bee6402-d17b-4fc7-9d9f-525d5c03b291`: report/record/index/file_notification/ui_notification all succeeded. Claude session `eb8820bb-8d66-4ce2-88d7-648e77f17090`: terminal, retain=false, cleanup succeeded (`claude_project_purge`).
- **Closeout defect:** worker `runner-worker-c7384b1cf37d` is failed after valid business finalization. Log traceback: `_notify_terminal` -> `deliver_terminal_outcome` -> coordinator `_pass` -> `_task_lock` -> nonblocking `fcntl.flock`, `BlockingIOError: [Errno 11] Resource temporarily unavailable`. Durable delivery nevertheless succeeded. Do not retry completed business or discard the successful artifact/callback; fix closeout lock contention and validate with a fresh canary.
- Verdict: structured handoff functional behavior verified; E1 overall closure remains pending this execution closeout defect. This is not a clean Runner pass. No automatic approval or business retry was performed during acceptance.

## Terminal delivery lock repair

The headless container coordinator now treats a busy nonblocking delivery lock as `deferred` with `delivery_lock_busy` and no attempted stages. Only lock acquisition contention is handled; permission errors and errors inside delivery are not disguised as success. The durable receipt stays unchanged, and the existing owner/maintenance performs delivery. The wrapper keeps the event routed, preventing fallback duplicate notification.

Regression: 216 tests passed in 17.819s across terminal delivery, handoff recovery, cleanup coordinator, Runner and completion contracts. Added real flock contention at the worker notification entry, receipt immutability, successful delivery after release, no duplicate delivery, and unsuppressed permission errors. Ruff, diff check, test structure (75 files / 40321 lines), TypeScript and wheel build passed. The earlier 2337-test full run predates this repair and is not claimed as a full run of the repaired candidate. A fresh real task is still required to close the installed worker-exit acceptance; `4E8V-002` is not retried or rewritten.

Repair installed on Mac via successful managed update: image `sha256:704ae5889fbfb950a4b97a594202d2ff514fa0e0431f2d7c2c6cda3b818e0047`, wheel SHA256 `4989af97e488ee3ead15711002f9a955071688a5aad2974512a10f7e826c8648`, archive SHA256 `b258f5d467383a89ede7384fa3d4f6647affcadadd4086a2f38ef167dec8879a`. Runner ready and WebUI healthy at port 58013. This update had no skill conflict. Windows installation unchanged. Fresh real closeout acceptance remains pending.

## Final E1 acceptance: 4E8V-003 passed

On installed image `sha256:704ae5889fbfb950a4b97a594202d2ff514fa0e0431f2d7c2c6cda3b818e0047`, Claude `4E8V-003` completed with Runner completed, valid final marker (executor exit 0), exactly step results 1/2 done, authoritative progress sequence 2, report ready and RunLease closed. Parent is 4E8V-002, current chain head 4E8V-003; frozen permission remains inherit/inherited_task.

Delivery `b7e9f870-739b-424c-b256-fc7c0fb640f4`: all five stages succeeded with one attempt each. Session cleanup succeeded. No lock traceback in the observed worker log; only existing model-name diagnostic warnings remain.

All three files were independently read back byte-for-byte. Source and prior Claude file hashes remain unchanged. New `handoff-lock-retest.txt` contains `AGENTBC_HANDOFF_LOCK_RETEST_OK` plus LF, SHA256 `4960957a352e9c22ef6a57669f878693e512c0d7fb21210531a6c2fe18906450`.

Verdict: E1 structured handoff functional and real closeout acceptance passed, combining Hermes→Claude 4E8V-001/002 evidence and repaired-candidate 4E8V-003 closeout. This does not claim every race occurred in this run; real lock contention is covered by the regression test. E5 final full regression/release remains separate; generic same-version managed-skill upgrade classification remains recorded as an installation issue. No historical task was rewritten, and no Windows replacement was performed.
