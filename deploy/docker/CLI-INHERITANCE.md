# Native CLI runtime inheritance contract

2026-09-27. Defined, not implemented or certified. Applies to Docker tasks 01,
02, 04 and 05; replaces any requirement that every executor must log in.

Default: inherit the user's selected native CLI configuration. Also allow an
independently configured container. API keys, custom gateways/endpoints, native
OAuth, supported cloud identities and supported unauthenticated local endpoints
are valid modes. Each executor/version must actually support the selected mode.
An absent OAuth login is not a failure for API users.

## Configuration provenance and resolution

Capture the selected executable/version, cwd, explicit profile/arguments,
relevant exported variables and native user/project/managed configuration layers.
Preserve that CLI's precedence rules. Do not invent a shared API-vs-OAuth priority,
change provider/model/base URL/billing, or silently fall back to another account.
A different locked Linux version must report unsupported source settings.

Use an instance-specific runtime profile: selected config sources, path mappings,
secret references and a configuration revision. Freeze the revision for a run;
explicit refresh affects new runs. A later export in another host shell does not
change a running container or Runner. Runtime inheritance is distinct from task
permission `inherit/full`, which keeps its existing contract.

Do not source shell startup files or execute aliases/wrappers to discover secrets.
Support explicit config paths, environment names/files and declarative parameters.
Unsupported native helpers, hooks, plugins, MCP servers or platform-specific
programs must be reported, not silently discarded or granted extra permissions.

## Delivery boundary

- Preserve native config structure and precedence; treat secret-bearing config
  files as secrets. Keep regular config read-only where possible; native refresh
  state uses a private writable executor volume.
- Assemble environment/config for the selected executor child. Do not inject all
  executor secrets into a shared Runner environment: identical variable names may
  mean different accounts/providers. WebUI never receives executor credentials.
- Show a redacted source/mapping summary during setup. Support predeclared mappings
  for noninteractive use without prompting for OAuth. Never mount the entire host
  home or capture the entire shell environment.
- Do not put secrets in image layers, command arguments, logs or task reports.
  Preserve values through Compose parsing, including dollars, hashes and equals.
- Host loopback is not container loopback. Resolve host-local API/proxy addresses
  with an explicit mapping and platform test; never silently switch to public API.
- Map CA/proxy/helper dependencies explicitly. Preserve native token refresh when
  used. API modes test key rotation; unauthenticated endpoints need no token test.

## Readiness and acceptance

Readiness depends on the selected mode's actual minimal call, not login status.
Distinguish missing config, unavailable credential, unreachable endpoint, missing
model/dependency and unsupported mode. Offer login only when the selected OAuth
path requires it. API failure should identify the relevant API configuration.
An empty test home proves no configuration was supplied, not that the user's CLI
cannot run. Third-party protocol compatibility requires testing, not inference.

Check the actual AgentBC entry path: Codex app-server, Claude SDK with explicit
CLI/settings sources, Hermes ACP with provider dependencies, OpenCode run with
native config merge. Current inherited `os.environ` is not a host-import mechanism
or executor-isolated secret mapping; implementation remains pending.

Task 01-B now verifies native runtime configuration and actual calls. Task 02 owns
profile/import/env boundaries, task 04 adapter semantics, task 05 setup/diagnostics.
The 12 host/executor acceptance cells remain; record redacted runtime mode,
provider/model and config revision. Add targeted API-only, custom endpoint,
API+OAuth precedence, OAuth refresh, no-auth endpoint where supported, missing
secret, loopback, helper/CA failure, per-executor isolation and reload tests. Use
synthetic secrets for contract tests; real provider calls still need live evidence.

## References

- [Codex configuration](https://developers.openai.com/codex/config-advanced/)
- [Claude settings](https://code.claude.com/docs/en/settings)
- [OpenCode configuration](https://opencode.ai/docs/config/)
- [Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration/)

Online docs describe the configuration surfaces; validate against pinned versions
before claiming support. No credentials were imported to define this contract.
