# Mac-first Docker vertical slice — 2026-09-28

> 2026-10-10 状态索引：Mac Docker 已内测通过；Windows Codex/Claude/Hermes 已技术性通过；Linux 实际宿主与 Windows OpenCode 推迟至 1.06a。本文早期切片、待实现表和旧实例信息保留为历史，不代表当前状态；现行范围见[开发状态](../../docs/DEVELOPMENT_STATUS.md)与[验收矩阵](acceptance-matrix.csv)。E5 尚待收口。

Current priority: macOS arm64 workflow before native Linux/Windows validation.
Other-host acceptance remains pending, not removed. OpenCode is the first slice;
Codex/Claude/Hermes and WebUI integration follow on the same image/Compose design.

Delivered Dockerfile, main Compose, installed wheel with built WebUI assets,
SHA256-verified OpenCode 1.18.31, non-root foreground Runner, isolated config/data/
runtime volumes and tmpfs control channel. Runner health reports ready and
allowlists OpenCode. Wheel resources exist without a source checkout. Nine
OpenCode dispatch integration tests, Ruff, working-tree root boundary and diff
checks pass. Full release regression remains a later-stage gate.

Local development image: agentbc:mac-opencode-dev
Image ID: sha256:fdaaf79b68d864f91d82c29236e08be655a1f071303b0449839e3858b2ac3b13
Compose project: agentbc-mac-flow
Only a new test project is host-mounted. Selected existing OpenCode API/model
configuration is copied into this instance, not image layers; values are withheld.
General profile migration, arbitrary native helpers and key-rotation validation
remain unimplemented. This is not task02 all-executor/multiarch closure.

The user authorized one real inherit-mode OpenCode task writing a marker and
returning a report. Acceptance must inspect terminal callback, Core report,
closed RunLease and exact artifact contents; readiness is not workflow success.
Task identity and outcome will be appended after formal dispatch/acceptance.

The first uncached build was interrupted during a slow public binary download;
the completed build reused the already-downloaded archive through BuildKit's
runtime-cache named context and verified the same pinned SHA256. Cache is optional:

```sh
docker build --platform linux/arm64 --target opencode \
  --build-context runtime-cache=/absolute/path/to/public-opencode-archive-directory \
  -f deploy/docker/Dockerfile -t agentbc:mac-opencode-dev .
```

No production install, host Runner, host authentication or global Docker settings
were replaced. No image was published and no commit was created.

## 2026-09-28 follow-up

CNRR-001 completed with valid final callback, report and closed RunLease. Exact
27-byte marker verified; SHA256 f629994b8f6006f12b13e36e4f0a198724ab44b938bebb8b97910e405042356d.
Headless UI notification is still retrying and remains a defect.

Four-runtime local image assembly was attempted but export failed with Docker
snapshot/content-store input/output errors. Host free space was about 278 MiB.
Further builds stopped; no general Docker prune, volume deletion or host cleanup
was attempted. Three other executors remain unverified in real business tasks.
See MAC-MANUAL-ACCEPTANCE.md for current verification and the pending full procedure.
