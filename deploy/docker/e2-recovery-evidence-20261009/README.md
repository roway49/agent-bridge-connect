# E2 interruption recovery development

Current verdict: E2 accepted using AETB-001 → AETB-002. See the final acceptance section and AETB-002-acceptance.json. Earlier pending/failure sections remain historical evidence; E5 remains separate.

Scope: exact worker interruption, needs_recovery + closed lease without fabricated callback or automatic continuation; preserve confirmed progress and official session for explicit recovery. No recovery state machine rewrite.

Implemented: late recovery signals cannot overwrite completed/failed/rejected trusted terminal records. Existing cancelled handling remains. Task status/watch/list no longer reconcile leases; Runner checks registered boards every 10 seconds through the existing lease recovery transaction. This separates reads from recovery ownership.

Regression: 346 tests passed (70.222s), covering lease lifecycle, Runner, recovery runtime, handoff, completion, CLI, IPC and architecture budgets. New cases prove read-only status/list, Runner-owned recovery, repeated reconciliation idempotency and terminal evidence preservation. Two old cancellation fixtures now enter recovery directly instead of rewriting failed into recovery. Ruff, test structure (75 files / 40355 lines), working-tree boundary, diff check and wheel/type build passed. This is not a final full-release test result.

Candidate image: sha256:b6e39b404370449db3a3af618627faf4e2a024690182b8d82eb53eb3e7a23fa9
Bundle archive SHA256: 37c6f966e9dc27939fe67a9e6dc47823db2a5ab79b0fb164182bfeebe86c94f6

Real canary pending: Codex completes step 1, then writes the ready marker and waits in step 2. Verify exact task/worker/run/session/PID identity before interrupting only that worker; never kill Runner or use task cancel as a substitute. Measure recovery within 30 seconds and absence of callback/automatic continuation; verify official session parking without deletion. Only explicit subsequent recovery may run the unfinished step. Do not claim E2 closed until the recovered task actually completes.

## GMYX-001 controlled interruption result

After human approval, verified continuation worker PID 1018, run `runner-worker-0919d8b09061`, executor run `codex-GMYX-001-06a08b62`, official session `01a120bf-c23f-7092-b369-e8f353d87688`; exact cmdline included worker run and GMYX-001. Baseline and ready files matched required bytes, authoritative step 1 receipt present. Sent SIGTERM only to PID 1018 after rechecking all identities.

Observed after 0.507 seconds: needs_recovery, lease closed, no final callback, step 1 receipt preserved. Subsequent official read still needs_recovery with no automatic continuation. Exit evidence is worker_process_exited / returncode -15. Official archive acknowledgement was NOT found; cleanup remains not_requested. Do not classify this as successful archive parking or close E2. No release file or recovery task has been created.

Follow-up implementation investigation: CLI installs the Codex cooperative SIGTERM handler only after executor.start returns. Investigate the approved continuation start path and whether a blocking start leaves the worker on the default signal handler. Also task logs currently resolves the earlier worker history instead of this interrupted continuation; official task error still binds the correct worker. Preserve this canary and original receipt; never synthesize archive evidence or use Desktop archive for this container session.

## Synchronous Codex continuation interruption repair

Root cause: approved elevation intentionally selects synchronous Codex CLI. The worker's App Server cancellation handler is registered after `executor.start()`, so it does not protect that synchronous wait. Preserve this transport/permission contract.

The receipt-draining subprocess helper now optionally handles SIGTERM during the synchronous wait (Codex only), signals its own isolated process group, bounds termination, preserves stdout receipts and restores the previous signal handler. The Codex adapter returns needs_recovery without a completion callback, and requests only official thread/archive for the exact receipt session after the CLI exits. An acknowledgement and timestamp are recorded only on successful RPC; archive errors leave no invented acknowledgement. The separate archive-only API never sends thread/delete.

Validation: 227 related tests passed (24.451s), including a real SIGTERM in an isolated test worker with handler restoration; archive-only acknowledgement and error/no-delete contracts; existing App Server, cleanup, lease and handoff regressions. Ruff, test structure (75 files / 40393 lines), diff and wheel/type build passed. This is not a fresh full-release run. GMYX-001 remains historical evidence; no stored task or archive receipt was rewritten. New real interruption + explicit recovery acceptance remains pending.

Repair candidate installed successfully through managed update: image sha256:f1d7bc02c7c4c918de1fd0de929fcc1868aaeb068555092fd6b84f98eb2503a3; archive SHA256 c0b420a78fa69ffece8c05aa6d7e55ad008c386fbb3f5892d2eb34d72be99fe6. No Windows deployment or task retry.

## D6CS-001 retest: official archive passed, worker classification failed

Verified exact task, ready/baseline bytes, continuation worker PID1110/run runner-worker-58434005c740, executor run codex-D6CS-001-7c52c0f4, session 01a120e4-630b-7ad0-a3f4-cc21d48ca775; sent SIGTERM only to that worker. Official archive acknowledgement persisted at 2026-10-09T13:41:19.862355Z. Step1 remained done and no callback was fabricated; lease closed.

However Core became failed rather than needs_recovery: worker ignored the adapter's explicit needs_recovery status and used a failure-name substring heuristic that excluded executor_turn_interrupted. Consequently the existing failed-terminal cleanup officially archived/deleted the session (cleanup succeeded, container_session_archive_then_delete). This cannot count as preserved-session recovery acceptance. No historical state/receipt was rewritten, and no recovery task was dispatched.

Source fix: honor explicit adapter needs_recovery before heuristic handling. Added actual worker entry regression for executor_turn_interrupted: preserves recovery state and no callback. 128 related tests passed in24.137s; Ruff/diff passed. This latest classification fix is NOT yet packaged/deployed; installed image remains f1d7bc02c7c4c918de1fd0de929fcc1868aaeb068555092fd6b84f98eb2503a3. E2 remains open, requiring new-candidate real interruption and explicit recovery completion.

Classification fix now installed through successful managed update. Image sha256:d1febd6e5d1b3b6d338c58a3f3126a9a85bc56ebd8a517797dbc4df58bee7b90; bundle archive SHA256 f528c6ca555ad56fd6b67f17c15bcfa431094fb03b7deb8da64b07e8b843be49. Next fresh Codex canary will validate needs_recovery + official archive preservation before explicit recovery. Prior canaries remain untouched.

## NPD7-001 classification retest

Installed image `sha256:d1febd6e5d1b3b6d338c58a3f3126a9a85bc56ebd8a517797dbc4df58bee7b90`. User approved the active retry attempt. Exact worker PID 2304 / `runner-worker-bc46033a3cbd` was interrupted after binding and ready-marker checks. In 0.507 seconds the task became `needs_recovery`, lease closed, step 1 remained authoritative, and no callback was produced. Official session `01a120f6-0311-7150-a50e-98dbcad0e082` archive was acknowledged.

Acceptance remains blocked: background terminal cleanup subsequently executed `container_session_archive_then_delete`, acknowledged deletion and verified CLI absence at 2026-10-09T14:01:01.997933Z despite needs_recovery. Classification fix passes; recovery-session preservation fails. No recovery handoff dispatched, no source outcome rewritten. See NPD7-001-interruption.json and NPD7-001-result.json.

## Recovery cleanup guard repair

Root cause: cleanup intentionally ignored task status after task-end dialog delivery; `task.recovery_required` therefore unlocked deletion. The coordinator now skips all primary/auxiliary cleanup for `needs_recovery`, including force/restart replay and already-resolved primary receipts. The shared policy exposes `recovery_session_preserved`; the destructive primary call rechecks its gates after rereading the task. Existing archive acknowledgements and cleanup receipts remain unchanged; no fake retained/succeeded receipt is introduced. Existing revival already permits needs_recovery + not_requested, so its gate is unchanged.

Validation: 110 cleanup/auxiliary/receipt/session tests and 147 retry/revival/handoff/completion tests passed (257 total); Ruff, diff whitespace and test-structure checks passed. This is source-level validation, not a new installed-image acceptance. NPD7-001 remains historical evidence of deletion; it was not rewritten. A fresh installed candidate and controlled interruption/handoff canary are still required to close E2.

Recovery guard candidate installed successfully via managed offline update; Runner ready. Image: `sha256:25f6a41bc3e7ce3b0ef84a79aaca56d9ef36278eca171f9d45920c411365335b`. Bundle checksum: `e7c683b343c9e71d44789b4eb87b4817d42f6d272673539ec4ca11f7997a54e8  agentbc-1.0.4a1-docker-macos-arm64.tar.gz`. New real canary pending; installation success does not close E2.

## AETB-001 installed preservation acceptance

Exact worker 798 / runner-worker-6ab1bd40627d interrupted after binding and ready-marker checks. In 0.501s: needs_recovery, closed lease, step 1 receipt preserved, no callback. Official archive acknowledged for 01a12103-203f-7213-bac1-20d3c78c728f. After all recovery delivery stages succeeded, cleanup remained not_requested with zero attempts. A fresh official App Server thread/read returned the exact same thread ID without error, proving the session still exists.

Preservation fix passes this real canary. Overall E2 remains blocked: installed status reports revival_session_cleanup_unstable. retry_flow.py separately requires stable completed cleanup, whereas revival.py permits needs_recovery/not_requested. No handoff, release marker, or task-state rewrite performed. Recovery eligibility must be reconciled before continuation acceptance.

## Archived recovery revival gate repair

The installed public projection merged a stricter retry-flow gate with revival policy. retry_flow now accepts a Codex needs_recovery session with valid snapshot, exact official receipt source/binding, archive acknowledgement and timezone-qualified acknowledgement timestamp, provided cleanup is not_requested. Existing lease, worker, input, auxiliary and chain gates remain intact. No retain/cleanup receipt is rewritten. Active, unacknowledged, malformed or in-progress cleanup states remain blocked.

Regression exercises public retry preflight followed by actual handoff creation, inherited_done progress and byte-equivalent source session metadata; negative cases cover missing archive acknowledgement, malformed timestamp, unbound receipt and active session. Related suite and static checks passed; see /private/tmp/e2-parked-tests.log. This repair is source-only, not installed yet. AETB-001 remains parked for subsequent installed continuation acceptance.

Revival gate candidate installed successfully: image sha256:f9dfb6119d3cf3dcb6993e12c3b92ac3e171ec9b68ac9a71d594dd5d2b50e69c; archive SHA256 2f833f31e02524a516d9694b09ab5d23480c812498762fc3047390e893285311. Runner ready. Installed status AETB-001 now eligible for retry/handoff with no errors, step 1 done and step 2 pending. Fresh official thread/read confirmed exact archived session still present. Controller verified baseline bytes and created the planned e2-release.txt marker before explicit handoff. Full recovery completion remains pending.

## E2 accepted: AETB-001 → AETB-002

AETB-002 Core/Runner completed; valid final callback, 2/2 steps, closed lease. Step 1 retains locked source origin AETB-001; step 2 has authoritative progress. Exact baseline and recovered marker bytes passed. Report, record, index, file notification and UI notification all succeeded. New session 01a12120-9639-7002-b5c3-a774d35850b2 has official archive/delete acknowledgements and fresh CLI absence at 2026-10-09T14:47:07.677645Z. Source task remains needs_recovery with original step 1 receipt, pending step 2 and archived session untouched.

E2 scoped controlled-interruption/handoff acceptance passes. This does not close E3 old-attempt cleanup or E5 final full regression/release. See AETB-002-acceptance.json and AETB-artifact-acceptance.json.
