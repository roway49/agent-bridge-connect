# Security Policy

AgentBC is a Public Alpha. Run it only on projects with recoverable
version-control history and review executor changes before accepting them.

## Supported Version

Security fixes currently target the latest `1.0.1A2` source and Alpha bundle.
Older development snapshots are not supported.

## Report A Vulnerability

Use the repository's private GitHub Security Advisory form:

`https://github.com/roway49/agent-bridge-connect/security/advisories/new`

Include the AgentBC version, macOS and Python versions, executor and version,
reproduction steps, impact, and the smallest safe evidence set. Do not include
credentials, private prompts, customer source, full task reports, or personal
filesystem paths. Use a public issue only for non-sensitive reliability bugs.

## Local Trust Boundaries

- AgentBC launches already-installed local executor CLIs with the user's account.
- AgentBC does not authenticate or sandbox the model provider.
- Runner validates task identity, path plan, local request token, and run lease.
- Managed tasks use a task-scoped artifact root rather than the workspace root.
- Customer project paths are never uninstall or automatic-cleanup targets.
- Reports and runtime logs may contain task content; protect the AgentBC
  workspace with normal user-account permissions.

AgentBC reduces accidental path and lifecycle mistakes but is not a container,
virtual machine, or substitute for executor-native permission controls.
