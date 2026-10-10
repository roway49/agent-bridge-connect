# Codex nested sandbox profile

Candidate for Codex 0.153.4. The local **Docker Desktop arm64** offline boundary
probe passes; native Linux/Windows amd64 acceptance remains pending. This fixes
namespace setup, not authentication, approvals or business-task completion.

## Use

Merge `../compose.codex-sandbox.yaml` after the Docker mainline Compose and set
`AGENTBC_CODEX_SECCOMP` to this directory's **absolute** `codex-bwrap.json` path.
The mainline Runner image/Compose is still task 02 work; the fragment is not a
standalone deployment. Inspect `docker compose config` after merging and require:

- Runner runs as a non-root UID/GID; capability drop is `ALL`.
- `no-new-privileges:true` and this exact seccomp file are both active.
- No privileged mode, added capabilities, host PID/network, Docker socket or
  complete host-home bind. Leave Docker's proc masks and host AppArmor intact.
- Only Runner receives this profile. WebUI keeps its existing default isolation.

Do not silently retry with unconfined, SYS_ADMIN, legacy sandbox or full mode if
qualification fails. Other kernel/AppArmor/runtime combinations must be tested
on that host. Explicit network-enabled Codex profiles are not qualified here;
the exact clone rule currently covers the network-disabled sandbox only.

## Reproduce without credentials or model calls

Use a host Python 3.10+ and the unpacked frozen **Linux** Codex package:

```sh
python3 scripts/probe_docker_codex_sandbox.py \
  --codex-package /absolute/path/to/linux-arm64/codex-package \
  --platform linux/arm64 --output /tmp/codex-sandbox-result.json
```

On native amd64 use the amd64 package and `--platform linux/amd64`. Exit 0 means
all assertions passed; container startup or child exit 0 alone is insufficient.
For a negative control, add `--seccomp-profile deploy/docker/seccomp/moby-default.json`:
this must fail with namespace EPERM on the currently measured host.

The probe uses only tmpfs synthetic fixtures, a read-only CLI package and an
isolated HOME. It creates no task, imports no credentials and contacts no model.
It checks read-only and explicitly defined workspace-write profiles: Chinese/space
path writes; denied external/symlink writes; explicitly read-only `.git`, `.codex`,
`.agents`; explicitly denied file reads; network separation against a reachable
outer-container loopback listener; denied mount/namespace operations by the tool;
UID 1000, zero effective capabilities, no-new-privileges and active seccomp.

## Why these rules

The pinned Moby baseline and Apache-2.0 license are vendored unchanged. Source
commit and file hashes are in `manifest.json`. It is not claimed to be identical
to the compiled default of every Docker daemon. `codex-bwrap.json` is that baseline
plus exactly `codex-bwrap-rules.json`; contract tests verify the composition.

Observed failures were namespace-bearing clone, mount, pivot_root, umount2 and
unshare. Added rules restrict clone/unshare and mount/umount flags to enumerated
values. `pivot_root` cannot be path-filtered by classic seccomp. Neither can
mount's string paths or filesystem names: the flag constraints are not path
isolation. Kernel namespace ownership and Codex's inner policy enforce the
actual file boundary. The outer profile applies to all Runner processes, expands
the available kernel syscall surface and must never be described as unchanged
Docker-default security or a formally minimal security proof.

Docker's masked proc mount still rejects fresh proc creation on the measured
host. Codex's built-in compatibility path reuses the container proc layout; the
boundary tests pass with the masks intact. We did not enable the legacy sandbox.
The diagnostic initially used obsolete `codex sandbox linux` syntax; after
namespace setup it failed trying to execute a command named `linux`. The fixed
version uses `codex sandbox -- COMMAND` or explicit `-P` profiles. That diagnostic
error did not invalidate the preceding syscall failures and is not hidden.

Results on 2026-09-27:

| Environment | Result |
| --- | --- |
| Docker Desktop macOS arm64, default baseline | Expected namespace EPERM failure |
| Same host, candidate rules | Both permission profiles and all boundary assertions pass |
| Same Mac, Linux amd64 emulation | Fails inner SeccompInstall EINVAL; not native amd64 evidence |
| Native Linux amd64 / Windows Docker amd64 | Pending |

Detailed local evidence is indexed by `../D0-01-evidence.json`. Do not promote
these smoke checks to the 12 executor business cells or three GUI acceptance cells.

Sources: [OpenAI sandbox documentation](https://learn.chatgpt.com/docs/sandboxing),
[pinned Moby baseline](https://github.com/moby/profiles/blob/85e237f1fe229a0c61c9c7d8e743fa780d3b97ca/seccomp/default.json),
[Codex permission implementation](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/config/permissions.rs).
