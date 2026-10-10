# AgentBC Docker development candidates

Current status (2026-10-10): Mac arm64 internal acceptance passed; Windows amd64
Codex/Claude/Hermes technical acceptance passed. Physical Linux host and Windows
OpenCode acceptance are deferred to 1.06a. E5 final release regression is pending.
The release proposal uses `1.0.5a1`; historical candidates used `1.0.4a1`. Verify image and bundle SHA256.

Use the offline bundle installer, then the installed host CLI and skills:

```sh
# macOS, from the extracted bundle
sh install_docker.sh
agentbc setup
agentbc doctor
agentbc task list
agentbc webui start
```

Windows uses `py -3 .\install_docker.py`. Native and Docker formal installs are
mutually exclusive; switching requires uninstall then a clean installation.
Updates of the same type use `agentbc update --bundle <directory>`.

- [Current scope and remaining work](../../docs/DEVELOPMENT_STATUS.md)
- [Windows installation and recovery](WINDOWS-INSTALLATION.md)
- [Installation contract](INSTALLATION-CONTRACT.md)
- [Platform evidence matrix](acceptance-matrix.csv)
- [Mac restart acceptance](REBOOT-ACCEPTANCE-20260930.json)
- [E4 Codex parent/child acceptance](e4-codex-evidence-20261010/README.md)

## Historical first OpenCode development slice

The following retained procedure describes the early isolated probe, not the
current formal installer or production task entrypoint. Its pending-status claims
are historical; use the dated evidence and current scope above.


Current target: macOS arm64 Docker Desktop, one real OpenCode task through
AgentBC Runner. This is a development image, not the four-executor or multi-host
release. Codex sandbox evidence is separate in `seccomp/README.md`.

## Build and start

Use a new dedicated project directory. Compose owns separate config, task data
and OpenCode runtime volumes, scoped by project name. Do not bind your whole home.

```sh
export AGENTBC_PROJECT=/absolute/path/to/test-project
# Run from the repository root.
docker compose -p agentbc-mac-test -f deploy/docker/compose.yaml build
docker compose -p agentbc-mac-test -f deploy/docker/compose.yaml up -d
docker compose -p agentbc-mac-test -f deploy/docker/compose.yaml exec -T runner agentbc runner status
```

Build uses digest-pinned Python/Node, pinned wheel build tools, locked frontend
dependencies and the SHA256-verified OpenCode release. Frontend resources are
built by the normal wheel backend; missing resources fail the build. The installed
wheel runs without the source checkout. Dockerfile-specific allowlisting excludes
host credentials, private records and development caches from the build context.

Default container configuration is copied into the named config volume only on
first creation. It chooses permission `inherit`, never full. No setup login,
LaunchAgent or host Runner is started. Native OpenCode authentication/model
configuration must be explicitly supplied to this isolated instance; readiness
alone does not establish authentication. Credentials belong in runtime volumes,
never image layers, Compose interpolation or logs. General profile import and
other executors are later slices; this does not yet certify complete CLI config
inheritance.

Task CLI commands must use `--root /data/workspace/record` (the generic CLI's
built-in board default remains unchanged). A real test is accepted only with
terminal callback, report, closed lease and matching host artifact. Explicit
customer path `/project` selects the mounted test project; `default path` selects
the managed artifact directory in the data volume.

## Stop / retain data

```sh
docker compose -p agentbc-mac-test -f deploy/docker/compose.yaml down
```

This keeps volumes and the bound project. Do not add `-v` unless deliberately
removing that instance's data. Upgrade/active-task recovery remains task 06 work;
finish the test before stopping. No production install or host service is replaced.

## Public evidence exports

Personal host paths in historical evidence exports have been replaced with generic development paths. Private originals are retained separately. Artifact/image hashes refer to the original tested artifacts, not the redacted report bytes. Public history is prepared from the sanitized snapshot; raw private development commits are not publication inputs.
