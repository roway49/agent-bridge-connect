# Windows formal install candidate — 2026-10-08

Candidate ZIP (Windows host):
`D:\agentbc-tests\win-formal-20261008-final\output\agentbc-1.0.4a1-docker-windows-amd64.zip`

SHA256: `91b0567bd1bf1658b1eb3a4dbc226e3dae0272889459d75257f6634c1bd80fa7`

Image: `sha256:89fa3f8833289f218487942b1cf4b1f73d1be2b95cb505664e751e754cca2051`

This is a local development candidate with package metadata 1.0.4a1, not a published 1.05 release. Source identity, wheel checksum, runtime versions and inventory are in `bundle.json`. Build outputs before `-final` are superseded and must not be used for acceptance.

Passed on Windows:
- All bundle files verified against the manifest.
- Codex / Claude / Hermes `--version` in network-disabled fresh containers; no credentials or model requests.
- Python installer returns exit 2 with current preview still installed.
- Current public CLI and preview profile bytes unchanged.
- Temporary private directory/file SID and DACL checks.

Remaining manual gates:
- Export and remove the inactive `AgentBC-WIN03-Probe-20261001` in Task Scheduler. Its exact owner/action were inspected; the attempted cleanup `.ps1` was blocked by Windows execution policy, so no removal occurred. Policy was not changed.
- Confirm backup/retirement of preview, then clean formal setup, installed business tasks, desktop recovery, uninstall preservation and reinstall.
- Windows full install/update acceptance and Linux remain open; OpenCode Windows is deferred to 1.06a.

No current preview replacement/uninstall or Mac installation change was performed. See `../../WINDOWS-INSTALLATION.md` for manual instructions.

Final validation: Python four-layer full suite 2323 tests / 502.756s / OK; focused installer/desktop 102 tests; frontend 18 tests; typecheck/build, Ruff 0.15.22, 75-file/40041-line structure, repository boundary and diff check passed. Desktop dialog waits account for the longer full-suite wall time.
