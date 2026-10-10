# Read-only Web snapshot v1

This contract is the data foundation for a minimal WebUI and future desktop host.
The provider itself is not a task controller or complete lifecycle API.
The HTTP host and browser presentation are documented in [WEBUI.md](WEBUI.md).
The executable API is `SnapshotProvider(board_root, report_root=...).snapshot(task_id)`
in `agent_bridge_connect.web_snapshot`. The wire schema is
[web-snapshot-v1.schema.json](protocol/web-snapshot-v1.schema.json).

## Reads and identity

The caller supplies trusted, existing board/report roots at startup. Requests
supply only an exact uppercase task iteration ID, not a path, chain shorthand or
latest-task alias. Missing roots are errors and are never initialized. The
provider uses descriptor-relative POSIX reads, pins root device/inode identity,
and rejects symlink traversal, special files and oversized records. Native hosts
without the required safe file APIs are unsupported; Linux containers and macOS
are the intended first targets.

Each server-lifetime provider creates an opaque `source_id`. A successful response
has `schema_version=1`, `consistency=consistent`, UTC `captured_at`, `task`, `text`
and `snapshot_revision`. The revision is SHA256 over sorted-key compact UTF-8 JSON
of schema_version/source_id/consistency/task/text (not captured_at or the revision
itself). A new provider gets a new source_id and clients must discard old caches.

The provider reads task bytes once, captures the bound report, then rechecks report
and task. File identity, size, mtime, ctime and content digest must agree. One retry
is allowed; continued changes raise `snapshot_changed`. This is optimistic
verification, not a multi-file transaction: writes after the final check are a
later snapshot. No TaskService, CLI, Runner action, reconcile, cleanup, report
generation or route registration is invoked. RunLease and interventions are bounded descriptor-relative read inputs for the
optional task.duration projection, reused from the shared timing view without its
live storage reads. Their versions/results are rechecked; no runtime writes occur.
Input/route files remain unread; waiting comes from the same captured Task record. The Core's pure `public_task_view` is reused before applying
a stricter field allowlist.

## Field-to-text mapping

| DTO | Persisted source | Display contract |
| --- | --- | --- |
| task.task_id | task.id, equal to requested ID | Exact text |
| title / executor / status / updated_at | title / assignee / status / updated_at | Preserve raw strings after secret redaction; null means not provided |
| attempt_id | agentbc.execution.attempt_index, explicit integer >=0 | `attempt-N`; absent/invalid stays null; never infer attempt-0 |
| run_id | execution.executor_run_id, otherwise worker_run_id | Persisted identity only, null if absent |
| steps[].id/description/status | declared steps; legacy action is description fallback | No inferred completion or progress percentage; absent status stays null |
| waiting | input.type/status/summary | Display type and current request status; answered/expired is not an active wait |
| error | last errors[].code/message | No diagnostics/details/raw executor result |
| callback_state | final_callback task/run match plus marker_valid=true | received; missing if absent/null; malformed/mismatched is unknown; does not imply completed |
| capabilities | known integration boundary | OpenCode basic/unverified; others unknown here, not live readiness |
| text.report | fixed task report in trusted report root | Plain text, scope=task; never claims current-run binding |
| text.execution | no approved public text source yet | unavailable, scope=none, null text |

Status labels for the presentation layer: pending=待派发,
running=执行中, input_required=等待输入, needs_recovery=待恢复,
completed=已完成, failed=失败, cancelled=已取消, rejected=已拒绝.
Legacy raw statuses remain visible: assigned=已分配, working/in_progress=执行中,
pause_pending=暂停处理中, paused=已暂停, review_required=待审核.
Other nonempty values display `未知状态（原码）`; null displays `未提供`.
Step labels: pending=待执行, running/in_progress=执行中, done=已完成,
blocked=受阻, failed=失败, inherited_done=继承完成; unknown stays explicit.
The raw status code is retained alongside its label; no summary interpretation.

Text states are distinct: missing=暂无, unavailable=不可用,
available with empty string=an existing empty file. `truncated=true` must show a
truncation notice. Preserve Unicode, spaces and newlines. Use DOM textContent
and pre-wrap, never interpret HTML or Markdown. Invalid timestamps display
`未提供（时间格式无效）` and keep the raw value available for diagnosis;
valid timestamps may be localized only with an explicit timezone label.
Text matching compares the **same source/task/attempt/revision** with this mapping.
The protocol field tests are not browser text-rendering acceptance.

## Report and secret boundaries

Report paths must be exactly `<trusted-report-root>/<YYYY-MM-DD>/<task-code>/<task-id>-report.md`,
and equal the captured Task's report_file. Paths outside that root, malformed paths,
symlinks, unreadable/non-UTF8 or oversized reports return unavailable. No source-path field is
returned to the browser; task/report prose may still contain user-authored project
paths and remains private local content. Reads never generate missing reports. These are existing
task-level documents; even a stable filename is not proof that the report belongs
to the current execution attempt. Do not use report prose to override Task state.

Task fields are explicitly allowlisted: no config, argv, environment, private
session, approval choice handle, grant, fingerprint, raw result or arbitrary
extensions. Free text covers the existing report secret patterns (password assignments/words
and API key tokens) plus credential assignments and Bearer values, using bounded
key matching to avoid quadratic scans of long text. This is defense in depth, not a promise
to recognize arbitrary secrets embedded in user prose. Raw logs/trace stay closed.

Bounds: task record 2 MiB; report input 1 MiB; report output 64 KiB UTF-8 after
redaction (truncate only at a codepoint boundary); final JSON 256 KiB. An oversized
response errors rather than silently dropping declared steps. Unknown/malformed
record fields never expose raw exceptions or filesystem paths.

## Errors and transport decisions

Exceptions are ABCError with a path-free code/message and retryable boolean.
`retryable` is true only for snapshot_changed. HTTP mapping:

| Code | HTTP |
| --- | --- |
| snapshot_task_id_invalid | 400 |
| snapshot_task_not_found | 404 |
| snapshot_record_invalid | 422 |
| snapshot_file_too_large / snapshot_response_too_large | 413 |
| snapshot_changed / snapshot_root_unavailable / snapshot_source_changed / snapshot_file_unavailable / snapshot_platform_unsupported | 503 |

A 503 consistency failure must not return a partial success snapshot. Source
replacement requires a new provider/source_id. Report-only access failures remain
an explicit unavailable text field and do not falsify the Task's state.

The gateway provides same-origin GET `/api/v1/tasks/{id}/snapshot` and a bounded
list endpoint (default50/max200, explicit cursor); see [WEBUI.md](WEBUI.md).
It never shells out to task status or uses TaskService.list_tasks.

The initial authentication decision is an ephemeral, cryptographically random
256-bit per-server Web token, separate from Runner credentials. The launch URL
passes it in its fragment; the page removes the fragment immediately and retains
the token only in memory, sending it as Authorization Bearer. Never put it in query
strings, access logs or localStorage. A full page reload needs the launch link again;
background polling does not. API requests require token validation, an allowed
Host, and same Origin when supplied; no permissive CORS. The server defaults to
loopback, and a container's published host port must also bind loopback. Static
assets receive a restrictive same-origin CSP. Authentication and HTTP are implemented by the separate gateway.

Future App hosts replace the transport boundary and reuse DTO/mapping semantics;
none of the above gives a browser permission to dispatch, approve, retry or cancel.

The compact UI hides diagnostic identity/capability fields and report prose while
retaining them in the DTO. task.duration is an additive object with nullable seconds,
label and display; see WEBUI.md for the Task List timing policy and local document route.
