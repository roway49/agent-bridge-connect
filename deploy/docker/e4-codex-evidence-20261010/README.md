# E4 Codex single parent-child — PASSED (KS4C-001)


Current acceptance: see ACCEPTANCE.json, KS4C-live-parent-child.json, KS4C-final-parent-child.json and KS4C-report.md. Real parent and child completed; child archive/delete acknowledgement and fresh absence precede parent archive/delete and fresh absence. Terminal delivery succeeded and lease closed. 127 relevant tests passed. E5 release-wide regression remains separate. Historical sections below retain their original failure conclusions.

Pinned installed Codex CLI 0.153.4; multi_agent enabled; live schema probe passes but is not execution proof. Task 7R42-001 failed with completion_marker_state_invalid because the parent emitted unsupported needs_recovery final marker. Its summary claims one native child /root/e4_codex_child completed with CHILD_E4_CODEX_OK, but reports path-only tool identity and wait_agent without a target parameter. This summary is not official receiver binding evidence. No auxiliary ledger was persisted. Therefore child lifecycle and child cleanup remain unverified; parent completion and E4 are not accepted.

Next diagnostic must inspect the actual official runtime tool/event shape and reconcile it with the adapter's collabAgentToolCall/spawnAgent/receiver expectation. Do not weaken callback validation, invent a receiver ID, infer a UUID from a path, or archive unrelated sessions. No automatic retry was performed. Retain this frozen failure as evidence.

## CYKG diagnostic result and source repair

CYKG-001 completed with no Core errors. Official thread/read captured parent 01a124be-2155-7691-8a53-d42db0c37a08 and native subAgentActivity started/completed entries naming child 01a124be-434d-7bc1-90cd-d2b94b7a0bc0, path /root/e4_codex. Separate official child thread/read verifies parentThreadId and source.subAgent.thread_spawn.parent_thread_id, depth=1, and exact final text CHILD_E4_CODEX_OK. The current adapter only recognized collabAgentToolCall/spawnAgent; it ignored this new official item variant. Parent completion alone is NOT E4 acceptance because installed code has not registered the child's cleanup ledger.

Source repair accepts subAgentActivity started/completed with real UUID, binds the actual start item and child UUID to the authoritative parent/run, matches distinct completion-item IDs by the bound child UUID, rejects path-as-UUID, wrong owner and completion without start. Existing auxiliary reservation/receipt/terminal gates are reused; no model text or synthetic child is accepted. 59 collaboration/auxiliary tests passed (2.194s), Ruff passed. Not installed yet; live notification shape and official parent/child cleanup must still be verified after installation. Current diagnostic child remains an exact known unregistered session; do not fabricate historical ledger or delete it directly.

## Installed-candidate preparation

Official Codex 0.153.4 schema subAgentActivity fragment saved in tests/fixtures/executor_runtime/matrix/codex/0.153.4/subagent_activity_schema.json. Adapter integration test passes official item/completed envelopes with both native activity kinds through _app_event and verifies persisted child binding/terminal state. 126 collaboration/auxiliary/cleanup/executor tests passed in 14.920s; Ruff, diff, test structure and repository boundary passed. Candidate image sha256:a59d8b0a08ec43b14b349dd0d349aff69fef5eb07f036d6e4e713b92884b9ecb; bundle SHA256 5d8ab487d783da75ab75db51c76f7797893642533843f2edd541b5f650d73cb4. Formal installed update in progress; real live event/cleanup acceptance remains required.

## Y8SZ installed live binding verified (cleanup pending)

Candidate a59d8b0a08ec43b14b349dd0d349aff69fef5eb07f036d6e4e713b92884b9ecb installed; Y8SZ-001 dispatched. Native started/completed subAgentActivity notifications now automatically persist exactly one auxiliary entry. Official parent UUID 01a124c6-6c61-7b83-b649-599f7294b80f; child UUID 01a124c6-8765-73b2-937e-621c009c41a3, source depth=1 and exact parent ID. Official child thread/read shows completed turn and exact CHILD_E4_CODEX_OK. Ledger terminal state uses original run codex-Y8SZ-001-1b34bcd2 and actual start item call_7ab922100a9649d490551ddf2b6e4bf9. Parent approval continuation remains same parent session; no duplicate child. Snapshot Y8SZ-live-parent-child.json saved before cleanup. Parent step 1 done, step 2 observation window still running at the recorded check. No final acceptance yet.

## Y8SZ terminal: execution PASS, child cleanup FAILED

Parent completed both steps with valid callback, report/record/index/file notification/UI notification succeeded and worker exited. Primary cleanup succeeded (archive/delete acknowledged, fresh CLI absent at 2026-10-10T07:49:40.629367Z). Child cleanup then failed codex_session_archive_failed; delete not requested. Y8SZ-final-task.json preserves exact evidence. Subsequent official child thread/read returned -32600 'thread not loaded' after primary deletion. Earlier official reads proved non-ephemeral persistent parent and child paths. Possible parent cleanup cascading/child archive ordering requires investigation; do not infer a child delete acknowledgement from absence or label E4 passed. Y8SZ-child-after-parent-cleanup.json captures the read response. No manual child archive/delete or fabricated acknowledgement performed.

## Descendant-first repair candidate

Installed Codex 0.153.4 ThreadDeleteParams schema exposes only threadId; no preserve-descendants switch. This does not alone prove cascade semantics. The observed loss of the previously readable child after primary cleanup warrants a guarded descendant-first transaction for container Codex families. Each descendant must obtain its own successful cleanup receipt before ancestor deletion; failed, retained, unsupported or unbound descendants cannot authorize parent deletion. Native/mixed legacy ordering is unchanged. Shared terminal, notification and lease gates are checked before any child call; parent state is reloaded after child persistence. Existing retained-family handling is preserved.

127 collaboration/auxiliary/cleanup/executor tests passed (14.996s), including child-before-parent, child failure preserving parent, closed-lease and recovery gates, retention and repeated cleanup. Ruff, diff, test structure and repository boundary passed. Real installed acceptance remains outstanding; historical Y8SZ failure is not rewritten.

Descendant-first candidate installed successfully through the formal host update command. Image sha256:1d3ea5b15b19677b47a9535e084b5ac8c10f69da409f74af79518b894758d7b0; bundle SHA256 ecb33eb7cfaff341d932bcb42b52b4ebb22b53b05b50a20ed3570f4fed29bf65. Awaiting a fresh independent live canary with unchanged single-child requirements.
