# Windows formal installation Runner readiness repair

2026-10-08: original Runner was running and logged runner_ready, but the Windows runtime image had Config.Healthcheck=null. Managed Compose inherited no Runner healthcheck while ready() required healthy. Explicit CMD agentbc runner status healthcheck is now supplied by Compose; readiness assertions/timeouts unchanged.

Failure rollback previously used the parent's initial profile, missing the child's persisted host_workspace binding. It now loads the identity-validated saved profile before inspecting or cleaning resources.

Verified installed identity: 20429f45c7e04ec199de2ace87eaaaec; image sha256:4a3adda5dd6080bfded46b8bfacf52350df1dd940945fc430b3c07deac927de3. User's existing setup choices and imported profiles preserved; no model calls. Official setup --non-interactive exited 0, receipt installed, Runner and WebUI healthy, three managed skills installed; WebUI fixed port 35609. Windows desktop independent status ready, PID 28812, SessionId 1, matching installed Runner f9ee9a6dd2a90737685af869773dbbfe1d8d5ff3bc5a0bd953ecf82ed327067b. Historical workspace remains separate.

Tests: 49 installation tests and combined 103 installation/desktop tests passed; wheel frontend typecheck/build passed; diff check passed. This repair is not a fresh uninstall/reinstall acceptance and does not replace the new installed business-task matrix.

Open diagnostic issue: doctor holds the installation lock while inspecting services, causing the bridge to temporarily publish waiting_for_docker / Installation maintenance is active. Doctor can return 1 during this self-induced contention. Independent status after release is ready; do not represent doctor exit as passing. Needs follow-up diagnostic state handling; business requests were not exercised here.

Candidate: D:\agentbc-tests\win-formal-20261008-healthfix\output\agentbc-1.0.4a1-docker-windows-amd64.zip. Supersedes aclfix/final candidates. Profile/receipt recovery snapshots: C:\Users\developer\.agentbc-install\backups\health-repair-20261008.
