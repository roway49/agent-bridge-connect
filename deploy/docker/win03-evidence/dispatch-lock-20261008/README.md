# Windows formal-install dispatch lock repair

## Defect and fix

The formal CLI held the exclusive installation lock while `task create --dispatch` waited for desktop readiness. The Windows desktop loop needed that same exclusive lock, entered maintenance waiting, and could never satisfy the dispatch readiness check. No business task was created by the failed request.

Daily commands now hold a shared installation barrier for the full routed operation. Windows desktop validation/restoration uses that shared barrier; setup, uninstall, update and Runner/WebUI lifecycle commands retain exclusivity. Binding files have per-command UUID names to prevent concurrent invocations from overwriting/removing each other's transient profile. The receipt and container identity checks remain mandatory.

Windows uses `LockFileEx` on the same first byte as previous locks, with explicit pointer-sized Win32 fields and immediate failure. Real Windows child-process probes verified shared/shared success, shared/exclusive and exclusive/shared rejection, and lock release on close. The route regression reproduces desktop readiness while dispatch holds its barrier and proves maintenance cannot enter.

## Maintenance defects encountered during delivery

The first offline update failed and its rollback exposed GNU tar attempting to chmod/utime the Windows workspace mount root. The backup SHA256 values were checked before further recovery. Isolation probes showed `--no-overwrite-dir` still failed, and excluding `.` incorrectly omitted all files; neither was accepted. `--strip-components=1` restored every regular file byte-for-byte and avoided changing the mount root. Recovery now verifies that every member has backup()'s canonical `./` prefix before stripping it.

Desktop process identity now rejects a nonzero process exit time, including a terminated process whose kernel object is still held by another handle. A real Windows probe verified this. Formal desktop registration always selects the installation's `venv/Scripts/pythonw.exe`, not the outside interpreter executing maintenance.

The corrected official rollback restored the old installation successfully. Delivery uses the verified candidate wheel's normal update router because the previously installed maintenance code lacks these fixes. This preserves receipt, idle-task, binding, backup and exclusive-maintenance checks; it is not evidence that the old public update implementation succeeded.

## Evidence boundaries

- Focused installation/desktop suite: 110 tests passed.
- Windows shared-lock matrix and terminated-process probe passed.
- Isolated backup extraction: exit 0, all regular-file bytes matched.
- Initial restricted full regression failed only on 12 local socket-bind permission errors. Unrestricted intermediate full regression: 2329 tests / 392.222s / OK. Final full regression and installed candidate audit recorded separately.
- No model call or task approval is performed by these engineering probes. The three requested real Executor tasks remain a separate acceptance stage; dispatch acceptance alone is not closure.
- OpenCode Windows remains deferred to 1.06a. Mac installation is unchanged. No commit made.

## Final installed result

- Complete candidate: Windows `win-formal-20261008-dispatch-lock5/output/agentbc-1.0.4a1-docker-windows-amd64.zip`; SHA256 `0dad8e2887b283541b26844b6a1dac4559e018a29c3646d6949df0d6174d04d8`.
- Image: `sha256:b06a35dcfd204cecfd7c8e247d510599907c4fca9347cc748089c2465b86e4f0`.
- The second delivery attempt rolled back successfully but revealed that `copytree` recreated bundle with inherited Administrators ownership/DACL. Restoration now creates an owner-private destination before copying. The already-restored exact directory was tightened only after verifying receipt, image, all package hashes and its known inherited owner.
- Final update succeeded, preserving installation `abfb6ee8e1f94aa2b259c5c94ea93a6c`, settings, project mounts and port 32487. Candidate remains a local 1.05 development build with package metadata 1.0.4a1.
- Installed doctor: exit 0; desktop ready; Runner/WebUI healthy. Installed `task create --help` traversed the desktop-readiness gate and returned 0 in 5.91 seconds. Runner advertises Codex/Claude/Hermes and task board is empty before dispatch.
- Full four-layer regression: 2331 tests / 506.925s / OK; focused current installation/desktop checks: 110 passed; frontend 18 passed, build/type checks, Ruff and repository gates passed. Structure remains 75 files / 40190 lines.
- See `audit.json`, `update.log`, `bundle.sha256`. Three actual business-task dispatches follow this repair audit and must be accepted separately by their authoritative reports/callbacks/leases/notification/cleanup receipts.
