# Installation readiness contract — 2026-10-08

Recommended fresh-install candidate: D:\agentbc-tests\win-formal-20261008-readiness\output\agentbc-1.0.4a1-docker-windows-amd64.zip
ZIP SHA256: e5a23c4cc65accb86ef1b6121c2e9d08f06247a8a2173648201b080264d4b635

Managed Compose declares Runner's own CMD agentbc runner status probe, independent of the runtime base image. Readiness still requires actual Docker healthy status. Missing healthcheck and exited process now report explicit startup failures; timeout remains bounded and reports its last state. Persisted workspace binding is used during failed-install rollback.

104 installation/desktop regression tests passed, including starting->healthy, missing probe, process exit, timeout, explicit Compose probe, and persisted rollback workspace. Frontend typecheck/build, Ruff 0.15.22, structure (75 files/40074 lines), boundary and diff checks passed.

Real Windows isolation: fresh workspace/config/profiles volumes with no credentials or model calls; Runner healthy, WebUI healthy, force-recreated Runner with new ID healthy. Production container IDs unchanged. Test project's services/volumes removed. See acceptance.json and bundle.json.

This verifies the installation service-creation/readiness path, not a new full host uninstall/reinstall or business-task acceptance. Existing formal installation retains the prior healthfix build, which already includes the identical Runner healthcheck. New readiness package additionally improves error diagnostics. Old final/aclfix packages must not be used for new installs. Doctor lock-contention display issue remains a separate diagnostic item.
