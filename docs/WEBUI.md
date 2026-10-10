# Minimal read-only WebUI

## Installed candidates

For current 1.0.5A candidates, run `agentbc setup` to save the monitoring preference and fixed port, then use `agentbc webui start`, `agentbc webui close`, or `agentbc webui status`. First setup defaults to WebUI and background Y; repeated setup preserves choices. Start prints the authenticated link; status does not expose its token. Docker installations use their managed WebUI container, including on Windows. Port conflicts fail without silently changing the saved port. Closing the page/service does not cancel tasks. See [current acceptance and deferred work](DEVELOPMENT_STATUS.md).

The foreground `serve` commands below are for development/container hosting, not the normal installed lifecycle.

The development host displays existing task records and reports using the
[snapshot v1 contract](WEB_SNAPSHOT.md). It does not dispatch, approve, retry,
reconcile, generate reports or control the Runner.

## Run from a source checkout

```sh
npm --prefix webui ci --ignore-scripts
npm --prefix webui run build
PYTHONPATH=src python -m agent_bridge_connect.cli webui serve --assets webui/dist \
  --root /path/to/existing/record --report-root /path/to/existing/report
```

Build with a Node version supported by the pinned Vite dependency (Node 22.12+
within Node 22 is supported). Python hosts require the descriptor-relative safe
file APIs described in the snapshot contract. Packaged assets are loaded from the installed Python package, independent of
working directory. Source development explicitly passes `--assets webui/dist`.
Native Windows hosting is not supported; Linux containers are the Windows path.

`--config`, `--port` (default 0, allocated by the OS), and `--assets` (trusted local
build directory) are optional. Missing roots are never created. The foreground
server listens only on 127.0.0.1. Open the private launch URL printed by the server.
Ctrl+C stops the foreground host only. All webui commands bypass Desktop relay bootstrap.

The random per-process 256-bit token is delivered in the URL fragment, removed
immediately by the page and held only in memory. API calls use Bearer auth. Reload
with the original launch link; refreshing inside the UI keeps authentication.
No localStorage, cookies, Runner credentials, permissive CORS or request logging
are used. Host and supplied Origin must exactly match the server. Do not share the
launch link. Static files come from a startup allowlist and have a restrictive CSP.

## Read APIs

- `GET /api/v1/tasks?limit=50&cursor=...&group=active`: limit 1–200; omit cursor for a new catalogue.
- `GET /api/v1/tasks/{exact-task-id}/snapshot`: snapshot v1 with optional task.duration.
- `GET /api/v1/tasks/{exact-task-id}/report`: authenticated local report document, plain text with existing secret redaction; root/task binding and read bounds are unchanged. No file-path argument or raw log access.

The [list schema](protocol/web-task-list-v1.schema.json) freezes the wire shape.
The list response has `schema_version: 1`, opaque `source_id`, a 32-hex
`catalogue_revision`, `items`, and nullable `next_cursor`. Each item has task_id,
nullable title/executor/status/updated_at/error_code and boolean title_truncated.
Titles are limited to 256 Unicode characters; other metadata to 128. Invalid task
records remain visible as ID/error rows. Missing task files are omitted. No reports
are read to build the list. At most 5,000 task IDs and 256 KiB per response are allowed.

The UI has three server-filtered categories, applied before pagination:
active (including pending, excluding needs_recovery/needs_review), completed, and
failed/recovery (also cancelled/rejected/unknown/unreadable with raw status visible).
The API retains group=all as its compatibility default; cursors cannot cross groups.
Order within all: active tasks, unknown/unreadable tasks, then terminal tasks. Within each
group, updated_at descends (invalid dates last), then task ID ascends. A catalogue
freezes the projected rows, with at most four catalogues cached for 60 seconds.
Pagination is stable within it; it is not a transaction across all task records.
Cursors are signed, bound to page size and server lifetime. Bad cursors/limits
return 400; evicted or expired cursors return 409. UI expiry restarts page one.

401 means authentication required, 403 means disallowed Host/Origin, 404 means
missing route/task, 405 rejects write methods, 413 means a bound was exceeded,
422 means invalid task record; snapshot consistency/root failures return 503.
Failures have `{error: {code, retryable}}`; only snapshot_changed is retryable.
No raw exception or filesystem path is included in error messages.

## Presentation and verification

The page polls every two seconds, keeps the selected task, aborts superseded reads,
and displays fetch failures as potentially stale content. There is at most one
active list read and one detail read. Switching tasks clears the old detail. Empty,
unknown, missing and unavailable values are distinct; all prose is rendered as
textContent with preserved whitespace. Report text is task-scoped; execution text
is unavailable. OpenCode basic execution never implies full lifecycle certification.

`webui/src/protocol.ts`, `client.ts`, `controller.ts`, `presentation.ts`, and
`main.ts` separate wire validation, transport, request state, pure field mapping,
and browser view. A future App can replace the transport while retaining the DTO,
controller and mapping. Stage 03 covers substitution, disconnection and reordering.

```sh
npm --prefix webui test
npm --prefix webui run build
PYTHONPATH=src python -m unittest tests.integration.test_task_recovery_runtime.WebGatewayTests
```

The HTTP tests require permission to bind a localhost socket. Browser acceptance
compares actual DOM fields/steps to the same captured snapshot revision. This stage
does not claim three-host Docker acceptance or a packaged no-Node installation.

## Compact task detail

The page displays task ID/title/executor/status/duration/update time and steps;
waiting and error fields appear only when relevant. Attempt, run ID, callback,
capability diagnostics, raw execution text and report body are hidden from this
minimal view; the DTO retains its identity/diagnostic fields. Reports appear as
a local document link, fetched with Bearer authentication and opened separately
as plain text (download fallback when popups are blocked). No token is in its URL.

Duration uses the shared Task List timing view with safely captured RunLease and
interventions inputs: first attempt shows wall lifecycle time (任务历时); retries
show current-attempt execution time (本次执行时长). Unlike the Task List status
cell, the detail retains the duration for terminal/input states and displays status
separately. Missing evidence remains unknown. It never substitutes created_at
for retry execution time or equates wall time with pure execution time.

## Portable controller and stability (GUI-105-003-03)

`SnapshotTransport` exposes `list(cursor, signal, group)`, `detail(id, signal)` and
`report(id, signal)`. Groups are active/completed/failed. Reports return plain text;
only the browser view owns Blob URLs, new windows and download fallback. Alternate
hosts implement this interface and preserve `ReadError` HTTP status/code semantics.
The HTTP implementation checks response byte bounds before parsing and keeps
credentials confined to its instance. No new HTTP endpoints or wire version are
introduced by this stage.

`TaskController(transport, {clock, authenticated})` has start/dispose, subscription,
selection, category/page navigation, refresh and report-open operations. Its state
is consumer-read-only; consumers render it and send actions instead of mutating it.
The injected clock supplies set/clear only. The controller imports no DOM, storage,
filesystem or browser-window APIs. Deterministic memory transports exist only in
tests, never as a production UI mode.

Each list/detail channel holds one valid request and one next-refresh timer.
Completion schedules the next read after 2 seconds. Requests time out at 5 seconds;
failures back off 2/4/8/16/30 seconds, with manual refresh immediate and successful
reads resetting the interval. Abort plus request identity/global source epoch
rejects late results even if a transport ignores cancellation. Disposal settles
cancelled requests, clears timers and listeners, and cannot be restarted; mount a
new instance. Browsers restored from back/forward cache reload and require a launch
link rather than leave a disposed controller masquerading as live.

401/403 stops all reads and actions until a new authenticated mount. Network/503
failures preserve only the selected task's last successful detail and mark it stale.
404 clears detail. Protocol/413/422 errors are explicit and preserved content is
marked stale. List and detail errors are independent. A source change invalidates
all requests/content/cursors and loads the current category's first page. Each new
snapshot replaces the old task/attempt/run as a whole; revisions are opaque hashes,
not ordered timestamps.

A task moving to another status category stays selected with a visible notice.
Page changes and expired-cursor recovery also keep its detail; manual category
changes clear selection and select the new first row. Expiry recovery retains a
notice until navigation. Report operations deduplicate while pending, are bound to
the clicked task and source epoch, and cannot send old errors to a newly selected
task. Existing task-scoped report semantics remain unchanged.

The view updates keyed fields/list entries without rebuilding unchanged nodes;
normal polling preserves scroll and text nodes. Test coverage uses Node's existing
test runner with controllable promises and a virtual clock, plus a small real
browser acceptance pass and a ten-minute continuous polling observation. The
pending-timer policy and live execution/conversation features remain 1.0.6A work;
packaging and Docker delivery remain stage 04.

To validate a host replacement, supply the same DTO fixtures through another
`SnapshotTransport`, pass a fake clock, subscribe to state, and compare the pure
presentation output. Do not expose the test transport or a credential override
in the production URL. For browser acceptance, first record immutable fixture
hashes/timestamps, then exercise pagination and network faults in a separate tab
from the continuous-polling tab. Record final snapshots/metrics without tokens.


## Setup, fixed port and native background service

Run `agentbc setup`: `Task list style: 1) Terminal  2) WebUI` (new-install default: 2).
Setup prompts remain English. Choosing WebUI shows its loopback address, fixed
port, actual service state and the command to open an authenticated launch link. This saves
a preference and usage hints; `agentbc task list` keeps its terminal behavior.
Choosing WebUI allocates a currently free loopback port once and saves it. Existing
ports are retained unless reallocation is selected. A saved port is not reserved:
a conflicting listener causes start to fail; AgentBC never silently changes ports
or stops that listener.

The `[webui]` table contains `preferred_view = "task_list"` or `"webui"`, optional
integer `port` (1024–65535), and boolean `autostart` (enabled by accepting the interactive Y default).
Noninteractive setup leaves a new configuration disabled until interactive setup
explicitly applies it; EOF does not enable the service. Noninteractive
setup preserves these values and never launches/registers WebUI. Repeated interactive
setup preserves the style and port defaults; the background-service question is
`Keep WebUI running in the background? [Y/n]` and Enter selects Y. Choosing N or switching back to Task List removes
the owned login item and stops its service while retaining its port.

- `agentbc webui start` (or `agentbc webui`): start in background, wait for readiness,
  open the private browser link and print it. `--no-open` suppresses browser launch.
- `agentbc webui close`: stop only this configuration's WebUI. Repeated close is safe.
- `agentbc webui status`: show running/configured port, preference, login registration
  and stale/stopped state without disclosing tokens.
- All accept `--config /path/to/config.toml`. Run setup first if no fixed port exists.
- `agentbc webui serve`: foreground development/container host; root/report-root,
  assets and port overrides belong here, not to the managed background commands.

On macOS, answering y registers a user LaunchAgent and starts the service. Login
loads it without opening a browser. There is no KeepAlive restart after close:
manual start or the next login restores it. Setup N permanently disables login
startup. Native Linux/Windows autostart is not provided; use the Docker integration.

A private per-user/configuration runtime directory contains a 0600 instance receipt
and Unix control socket. The browser token is distinct from the control secret;
neither is saved in config or ordinary logs. Stop authenticates the exact instance,
never a stored PID alone. Stale or unreachable ownership fails closed. Services for
other configurations and the Runner are independent. Update suspends the owned
service before cutover and restores its previous running/login state; uninstall
removes its registration. An unsuccessful operation reports failure, not readiness.

## Build and container delivery

The PEP 517 backend performs `npm ci --ignore-scripts` and the TypeScript/Vite build
before wheel/sdist creation. Both source distribution inputs and installed static
resources are included. Missing/incomplete assets fail the build. Node is a build
dependency only; installed wheels serve without Node or a source checkout.

The optional `deploy/webui/compose.yaml` merges into the Docker mainline. It requires
an existing mainline image digest, read-only data roots and saved host port. It is
not a standalone Docker implementation or a claim of three-host acceptance. See
its README for startup, private link retrieval and the manual matrix.

`serve --listen-host 0.0.0.0 --public-origin http://127.0.0.1:HOST_PORT` is restricted
to an explicit loopback public origin; Compose publishes only that loopback port.
`--launch-file PATH` stores the private link in a checked 0600 file instead of stdout.
It may replace only a single-link, regular file owned by the current user with private
permissions. No executor credential/configuration mounts are needed by WebUI.

WebUI is the primary monitoring interface. Terminal Task List remains a compatibility
option during a gradual retirement; this change does not remove or redirect its CLI
or alter dispatch, approvals, callbacks or task acceptance. Existing explicit style
choices are preserved; new interactive setups default to WebUI.
