# Changelog

## 1.0.1A - Public Alpha

- Local-first task coordination for Codex, Claude Code, and Hermes through one
  authenticated Runner gateway.
- Four-character task codes with explicit chain iterations and context-free
  status, report, and artifact lookup.
- Atomic task creation and dispatch with preserved controller and executor
  identity.
- Customer-project and isolated managed-artifact path modes with task-scoped
  write roots.
- Cross-agent handoff for continuing work on an existing task chain.
- Native image input for Codex and Hermes, including image-generation and
  image-editing deliverables.
- Concurrent task visibility through the automatically managed Task List,
  low-frequency health classification, and compact macOS notifications.
- Executor-lifecycle terminal states covering completed execution, recovery,
  and unconfirmed termination.
- Readable task briefs and reports separated from bounded runtime records and a
  compact global task index.
- Explicit task close, recovery, reassignment, record cleanup, Runner
  lifecycle, setup, and uninstall commands.
- Automatic integration installation for Codex, Claude Code, and every
  detected Hermes profile.
- MIT-licensed source distribution and checksum-verified macOS Alpha bundle.
