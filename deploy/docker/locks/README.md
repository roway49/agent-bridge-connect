# Docker dependency locks

These are candidate build inputs for `PLATFORM-105-001-01`, not release certification.

Claude SDK: Python 3.11, `claude-agent-sdk==0.2.142`, resolved with uv 0.11.14
for aarch64-unknown-linux-gnu and x86_64-unknown-linux-gnu. Each requirements file
pins all 30 packages and includes artifact hashes. Install with `pip install
--require-hashes -r <file>`; use the matching Linux architecture. Do not re-resolve
at service startup. SDK wheels include a CLI, but AgentBC's explicitly selected
CLI must also pass the native protocol handshake; import/shape checks alone are
not an authenticated execution result.

Hermes: use upstream commit and uv.lock SHA256 in `../runtime-lock.json`.
Export with `uv export --frozen --no-dev --extra acp --no-emit-project`; do not
replace it with an unconstrained pip resolution. The build backend is separately
pinned by upstream pyproject.toml. An install result must include `pip check`
and a runtime import/CLI check, not merely a successful dependency export.

Keep AgentBC + Claude SDK and Hermes in separate virtual environments in the
same image. Their candidate locks have 13 shared package version differences;
installing both locks into one environment would overwrite frozen versions.
This is dependency isolation, not a second implementation or image family.

Refreshes require regenerated hashes, two-architecture install checks, and
native protocol regression. macOS amd64 emulation is only an installation probe;
it cannot close a native Linux or Windows acceptance cell.

The 2026-09-27 probes installed both Claude locks with hashes and passed `pip
check`, AgentBC protocol-shape validation and an unauthenticated native initialize
against CLI 2.1.247. Both Hermes installs passed `pip check` and AgentBC's ACP
probe. Upstream Hermes deliberately blocks ordinary wheel builds: retain its
complete pinned source and install editable, as its own Dockerfile does. Do not
set `HERMES_NIX_BUILD` to bypass that guard. `hermes-build.txt` freezes setuptools
83.0.0; install it with hashes before `pip install --no-build-isolation --no-deps
-e <source>`. The arm64 probe validated this exact backend-lock path; amd64 used
the same upstream-pinned backend through PEP 517 isolation.
