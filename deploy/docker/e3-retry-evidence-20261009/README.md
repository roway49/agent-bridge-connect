# E3 execution acceptance (PASS, 2026-10-10)

Latest acceptance: see ACCEPTANCE.json, EKPN-close-observation.jsonl, EKPN-after-close.json and PTBQ-final-live-audit.json. Entries below are chronological history; earlier pending/failure notes are preserved.

Scope: consecutive retry twice; human Deny -> failed -> retry -> close; exact old attempt session isolation, closed leases and no residual writers. Existing 86 retry/cleanup/revival baseline tests pass.

YP5J-001 received actual user approve_full (not Deny). It is not denial evidence. Explicit test close returned success; exact worker 7362 absent and no task-bound command processes remained, task record removed. Non-retry close cleanup evidence requires separate audit; no archive/delete success is claimed from record absence.

Next: fresh human denial canary; do not synthesize an approval decision. E3 remains open.

5HAB-001 also reports actual approve_full. Awaiting human clarification of the clicked button before treating this as user action or a UI mapping defect; no synthetic Deny or new task issued. Approval dialog/identity/broker regression was run separately. Source audit identified that retry archives optional session control files, but no explicit task/attempt/session cleanup snapshot is persisted when those files are absent; investigate under E3 without broad ledger restructuring.

## Attempt binding repair (source only)

Retry now persists cleanup-binding.json in the existing session control attempts/attempt-N archive even when optional active control files are absent. It binds exact task, attempt, revival, execution, primary and auxiliary session receipts without relabeling historical outcomes. File is private and fsynced before live projection rotation; write failure rolls back and forbids a new attempt. Existing archive collisions remain fail-closed. Tests verify exact evidence, failure rollback and unchanged attempt-0 after attempt-1 is archived. Installed candidate is not updated yet; real Deny/retry/close and consecutive retry acceptance remain outstanding.

5HAB-001 was explicitly cancelled through installed CLI after its approved wait. Closed lease, successful terminal delivery and official archive/delete + fresh CLI absence confirmed. Update to binding candidate b018cf5d0538d3f33e732d72790d499ec895bcd20ab92214796cda3318848a56 was rejected by require_idle: the only blocker was cancelled 5HAB-001, no open leases. Do not claim update success. Maintenance policy omitted cancelled/rejected; source fixed to accept these terminal states while still rejecting every open lease. Release regression executes the actual volume probe with terminal/active task and open-lease fixtures. New candidate rebuild and installed update still required.

Fixed candidate installed via its staged host updater with all normal installation/lease/identity gates intact; old installed updater retained old predicate, so two earlier attempts were rejected without replacing runtime. Successful image sha256:1945d8a62ba3eed11c8a36d5f515601ce7311c1fb571ec75c6d0a04bd685775b; bundle SHA256 88af3c7899ff30bdf627b3c03b2413c8ae52d8ddb425a4c29529161308d62de9. Runner ready. Next real canary reads authoritative attempt_index, deliberately fails attempts 0/1 and completes attempt 2; no permissions overridden.

PTBQ-001 attempt 0 reached failed with completion_marker_state_invalid: test wording mistakenly requested failed as final marker state, which protocol rejects. Artifact read reported real index 0. This is a genuine protocol failure, not a valid failure callback; frozen requirements are not rewritten. Official cleanup succeeded, archive/delete acknowledged and CLI absence verified at 2026-10-09T15:16:18.940519Z; lease closed, revival eligible. Proceeding to exercise retry from this actual failed state; no product protocol relaxation.

## Consecutive retry real acceptance PASS

PTBQ-001 attempts 0 -> retry -> 1 -> retry -> 2 completed. Initial failures were actual strict callback rejections (test-description mistake, not valid failed markers). Both failed attempts had closed leases and official cleanup success before retry. Archived cleanup bindings map distinct exact sessions; attempt-0 bytes/hash unchanged after second retry. Attempt-2 artifact contains 2 + newline and e3-success.txt exact AGENTBC_E3_SUCCESS + newline. Core and Runner completed, valid callback, terminal delivery succeeded, new session official cleanup succeeded with fresh CLI absent.

Still outstanding: genuine human Deny -> retry -> close, old writer isolation at retry-close, final affected regression and documentation closure. Do not mark E3 complete from this subset. Previous YP5J/5HAB observations both recorded approve_full; user clarification pending.

## Current completion audit

318 affected retry/revival/handoff/cleanup/auxiliary/session/completion/receipt/install tests passed in 13.281s; Ruff, whitespace, 75-file test structure and working-tree repository gates passed. This is targeted E3 regression, not E5 full release regression.

Proven: consecutive two retries to completed; exact persistent old-attempt cleanup bindings unchanged; archive-write failure rollback; installed cancelled-task maintenance handling; old and new session separation.
Missing: a genuine human Deny receipt followed by retry and active close, with exact writer stop and cleanup-before-purge evidence. YP5J and 5HAB both recorded approve_full and cannot satisfy that requirement. User clarification of actual button choice is pending; do not repeat denial tasks or synthesize decisions. E3 remains open.

V9G6-001: genuine human Deny confirmed (response source=user), failed with closed lease. Notification and official archive/delete cleanup succeeded with fresh CLI absence. Retry gate eligible; proceeding to approved retry then active close.

## Close receipt regression (source repair; installed retest pending)

V9G6-001 retry received human approval and was closed while active. The exact worker 9328 exited cooperatively with returncode 1; stdout reports Codex CLI interrupted. Runner metadata records cancel cleanup completed. Lease closed, task record preserved; official cleanup subsequently failed with codex_session_archive_failed (delete not requested). Evidence: V9G6-close-observations.json. No acknowledgement has been synthesized or historical task state edited.

Source audit found mark_task_needs_recovery returns immediately for close intent and discards the interrupted executor's session receipt, including any official archive acknowledgement. Repair now persists an acknowledged receipt only for cancelled tasks after latest-run and existing authoritative session validation; task status, terminal delivery and close intent remain unchanged. Regression exercises exact receipt preservation plus rejection of stale run and wrong session. 49 run-lease lifecycle tests and Ruff passed; affected cleanup/retry/handoff regression logged in /private/tmp/e3-close-regression.log. Installation and fresh real close verification remain required; E3 is not complete. The failed historical archive alone does not prove its earlier RPC succeeded.

Installed close candidate image 63b25dccd302e8a9c195af78baa4d91dd5eb6749fcd4000a9b98b5cede19a0ae (bundle SHA256 701b5a13d00c513eb97a0660bebb46294a7472a2fcb32d72dcb9391ab6f3c9f6), Runner ready. 4S4V-001 received actual approve_full, completed step 1 and waited with worker 777 alive, ready marker exact and no done marker. close returned success and removed record. This is NOT retry-close cleanup acceptance: the canary had attempt_index=0 and therefore used existing non-retry direct close. No official cleanup success inferred from absence. Before-close evidence retained. Next canary must first genuinely fail then retry before active close. Source additionally preserves verified archive receipt while close intent exists but status has not yet changed to cancelled (CLI cancellation ordering); 49 lifecycle tests cover both orderings. Latest source repair is not yet installed.

Second close candidate installed: image sha256:0aac69fc2d9dfb9c071227c76c211e38446a47faed5461d602aa597a47b81ea6, bundle SHA256 d962c896afb4293893043b2bb052cea81bc61bd782c63e35454ea2a9eb7ae540. 202 affected tests passed (8.887s), Ruff and diff checks passed. FKTR-001 dispatched for actual Deny -> retry -> close; first observation is input_required, human_decisions=0, session 01a1215a-f969-7dc1-83e0-2965080c8dce. Waiting for a real human decision; no synthetic response or duplicate dispatch.

## FKTR retry-close: cleanup passed, residual writer defect found

FKTR attempt 0 received real Deny and official archive/delete + fresh absence. Attempt 1 received real approve_full; ready marker exact, step 1 done, step 2 waiting. close returned close_pending_cleanup. High-frequency read-only observation captured official archive acknowledgement while close intent existed, cancelled state, cleanup pending, cleanup succeeded with archive/delete acknowledgements and fresh CLI absence at 2026-10-09T15:54:51.969580Z, then record removal and worker 2367 absent. Exact old-attempt binding remained distinct. Evidence: FKTR-denied-attempt-0.json, FKTR-close-observation.jsonl, FKTR-after-close.json.

Final process audit found detached canary wait PID 2674 still alive. Thus E3 is NOT passed. Container has no ps; _snapshot_descendant_pids incorrectly treated missing ps as no children. Source now falls back to Linux /proc stat parent links; tests cover names containing parentheses, exact recursive descendants, vanished entries, absent/failed ps. 52 Runner tests and Ruff passed. This repair is not installed yet. The exact orphan PID was identified by start time 78897933 and FKTR artifact path, then SIGTERM sent through pidfd (no PID-reuse risk). This manual cleanup is not product stop-gate acceptance. Next: install fallback repair and repeat retry-close writer verification.

Proc fallback candidate built: image sha256:fe0f9441b5d7156fabbc91bf91a1f9758d27ce57ed1bfcb1f902af7e39be280e; bundle SHA256 0afdcb9feff5ba2471f538a2da87745a437cbd49c16b61294dc21d3600be5a60. 169 Runner/IPC/retry/lease tests passed in 14.072s, plus diff/test-structure/repository gates. Formal installed update in progress; final real retry-close process-tree verification still required.

Proc fallback candidate installed successfully, Runner ready. EKPN-001 dispatched (attempt 0), exact official session 01a12164-42f1-7591-acd4-8a51d209ca5d; initial status input_required with zero human decisions. Awaiting real Deny before retry. No synthetic decision or repeated task created.

## Final E3 acceptance — PASS (2026-10-10 local time)

PTBQ live re-audit confirms attempts 0/1/2, valid final callback and exact artifacts; both archived binding hashes remain unchanged. EKPN-001 real user Deny produced failed with closed lease and official cleanup, then retry received real approve_full. Before close, worker 1829 and descendants 1846/2038/2150/2151 were captured from /proc (no ps installed); marker exact, step 1 done, step 2 waiting. close returned pending cleanup. All five processes stopped before archive/delete completion and record purge; final whole-container task-path scan found no detached writers. The exact session 01a12166-1a1f-7dc1-b583-c1fe65d00579 had official archive acknowledgement, official delete acknowledgement and fresh CLI absence at 2026-10-09T16:03:54.840771Z. Closed lease and all terminal delivery stages succeeded. Only afterwards was the record absent. No Dispatcher archive, invented acknowledgement, task-state patch or permission override was used.

465 affected tests passed in 26.688s. Ruff, diff, test structure and repository boundaries passed; this is E3 targeted closure, not E5 full release regression. Installed image fe0f9441b5d7156fabbc91bf91a1f9758d27ce57ed1bfcb1f902af7e39be280e, bundle SHA256 0afdcb9feff5ba2471f538a2da87745a437cbd49c16b61294dc21d3600be5a60. Real child-agent capability remains E4; auxiliary isolation is covered by existing contract/integration regressions. Historical V9G6 cleanup failure is retained honestly and did not block subsequent retry/canary execution; no retrospective successful receipt is claimed.
