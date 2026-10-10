# Windows formal installation: configuration repair and redispatch

## Verified failures

- WA6H-001: Codex initialize succeeded but thread/start returned configuration file missing. The Windows config referenced `cc-switch-model-catalog.json`; setup copied config/auth but omitted this explicit dependency. No model turn or official session was established.
- C4NW-001: Hermes used DeepSeek credentials with `anthropic/claude-opus-4.6`. The provider rejected that model. The failure report's missing final callback is the consequence, not the root cause.
- CTQ9-001: Claude reached a live SDK approval, but no decision reached Core before the five-minute deadline. Host bridge recorded only `Command failed (1): docker exec`; the original precise failing operation is unavailable. Rebuilding the notification from an in-memory copy succeeds. Do not claim that --approve-full caused this failure: Core explicitly normalizes it for live Claude approval.

## Changes

Codex imports now copy explicitly referenced model catalog JSON from the selected Codex home (including profile references), validate file size/type and reject linked/outside paths, and rewrite the imported reference to a stable container path. Host configuration remains unchanged. Missing/invalid dependencies fail the import before any writes.

Docker approval errors now report operation phase and structural error identifiers without echoing arbitrary stderr, notification text, command arguments or credentials. Unknown original Claude failure remains subject to the new real approval test.

The instance repair privately backs up the two existing container configurations, imports only Codex's missing catalog/reference, and sets Hermes to DeepSeek/deepseek-flash. It does not change Windows original credentials or the configured task permission mode. Maintenance verifies installation ownership and idle leases.

## Validation

- Focused configuration/desktop suite: 112 tests passed.
- Frontend: 18 tests passed; wheel build ran TypeScript and Vite successfully.
- Ruff, repository boundary and test structure checks passed.
- Full Python regression: 2333 tests passed in 200.298 seconds.
- Official installed update completed successfully; installation ID and fixed port 32487 preserved. Runner and WebUI healthy; desktop approval bridge ready.
- Codex initialize and ephemeral thread/start both succeeded after repair; no model turn was sent by the probe.
- Hermes container configuration now explicitly selects deepseek / deepseek-flash.
- Original status/report/log evidence (nine files) saved under the private Windows installation backup directory `pre-config-retry-20261009`.
- Official retry accepted WA6H-001 (`runner-worker-e1c2fd57f580`) and C4NW-001 (`runner-worker-c8af22b03992`) without permission overrides. Runtime completion remains unverified.
- CTQ9-001 retry was rejected with `revival_worker_active`. Public status still shows failed, closed lease and succeeded cleanup; an active worker/dispatch projection blocks revival. No bypass, direct state modification or replacement Claude task was performed. Claude remains blocked.

OpenCode Windows remains deferred to 1.06a. No Mac installation update or Git commit.

## Installed candidate

- Bundle: `D:\agentbc-tests\win-formal-20261009-config\output\agentbc-1.0.4a1-docker-windows-amd64.zip`
- SHA256: `6953051d75b7f5cfe0bf00597c3ce9dc9110b4dc4134ceaa0e6c58f6f26cfeb8`
- Image: `sha256:6dd46dd741c7ed0e55653a448242a6d547bfde55a4ca5ba4332a8d30bc438a08`
- Local development candidate; package version remains 1.0.4a1.

## Follow-up acceptance

WA6H-001 and C4NW-001 are completed with valid final callbacks, authoritative step 1 progress, ready Core reports, closed RunLeases, and succeeded terminal notifications/cleanup. Host artifact bytes exactly match `WIN_FORMAL_CODEX_OK\n` (20 bytes) and `WIN_FORMAL_HERMES_OK\n` (21 bytes). Prior errors remain historical; the latest reports have no current failure.

Codex session `01a11c65-7188-7d53-a961-8c609227b660` has official App Server archive and delete acknowledgements and CLI absence verification. Its approval report records one human decision and one full continuation. Hermes session `8d230edd-4c88-4291-a0b8-137a126fbd7c` has succeeded official-session-delete cleanup. No Desktop archive action was used.

CTQ9-001 current status now projects eligible=true with retry/handoff allowed; redispatch is subject to the official CLI's fresh preflight. Stale execution projection fields are not evidence of a live process when the authoritative RunLease is closed.

## Claude retry acceptance

CTQ9-001 retry was accepted as `runner-worker-a362da3b9afb` and completed on run `claude-CTQ9-001-d6db60f3`. Official session `9f1b7164-4a3d-45a5-8efd-3de1bf45f07b` remained the same live SDK session through approval. The bound request `approval-e5822a4f495f4745a4b82514ac10864a` records one human decision (`approve_full`, source=user) at 2026-10-08T16:49:10.015105Z, before its deadline. Step 1 progress followed at 16:49:16.333584Z and task finalization at 16:49:19.052926Z.

Acceptance passed: valid final callback, completed step 1/1, report ready, closed authoritative RunLease, all five terminal delivery stages succeeded, and `claude_project_purge` cleanup succeeded. Actual Windows smoke.txt is exactly `WIN_FORMAL_CLAUDE_OK\n` (21 bytes), SHA256 `6db76577f8027d8aedd5ea4d8b6304846318400781ebbb4f14fed251ce82868a`.

The report's old approval_request_expired entry belongs to the previous attempt; current summary reports no failure. Full-continuation count 0 reflects live SDK continuation, not a newly dispatched worker. Historical raw execution projections still contain active fields; authoritative current RunLease/timing is closed. This closes the three-executor minimal-task acceptance for this installed Windows candidate, not the entire Windows installation/reboot matrix or OpenCode 1.06a scope.
