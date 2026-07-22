# AgentBC Steps YAML Format

## Canonical Field

Every task requirement must be a non-empty `steps[].description`. Each description should be
specific enough for another agent to execute and verify without relying on the source chat.

```yaml
steps:
  - id: 1
    description: "Implement the requested behavior, including concrete parameters and constraints"
  - id: 2
    description: "Validate the deliverable and record reproducible evidence"
```

Do not use `action` for new tasks and do not rely on a top-level `description`. AgentBC Core
accepts legacy `action` steps for compatibility, but canonicalizes them to `description`.

Quoted single-line descriptions are the shortest and least ambiguous dispatch path. AgentBC also
accepts simple block scalar descriptions such as `description: |` when multi-line requirements are
clearer.

## Path Plan Requirement

Every new `agentbc task create` command must include one of these forms:

```bash
--customer-path "default path"
```

or:

```bash
--customer-path /absolute/user/project
```

Use `"default path"` only when the user did not supply a project path. If the user supplied a
project path, pass that exact path as `--customer-path`. Runner derives the internal
`customer_dir` value and owns path authorization. User deliverables go to the project/artifact
root. Readable task/report files go to `workspace/tasks/report`; compact runtime
state goes to the managed `workspace/record` directory.

## Image Inputs

Attach an existing image through the executor's native interface:

```bash
--customer-path /absolute/path/input.png --image /absolute/path/input.png
```

Repeat `--image` for Codex multi-image tasks. Hermes currently accepts one image per task
iteration. Handoff inherits image references unless the new handoff command supplies replacement
`--image` values. Image generation or editing steps must require final bitmap files under the task
artifact root.
