import test from "node:test";
import assert from "node:assert/strict";
import { TaskController, selectionNotice } from "./controller.ts";
import { HttpTransport, ReadError } from "./client.ts";
import type { SnapshotTransport, Group } from "./client.ts";
import type { Snapshot, Page } from "./protocol.ts";
import { fields } from "./presentation.ts";
class Clock {
  now = 0;
  seq = 0;
  timers = new Map<number, { at: number; fn: () => void }>();
  set(fn: () => void, ms: number) {
    const id = ++this.seq;
    this.timers.set(id, { at: this.now + ms, fn });
    return id;
  }
  clear(id: unknown) {
    this.timers.delete(id as number);
  }
  async advance(ms: number) {
    const target = this.now + ms;
    for (;;) {
      const next = [...this.timers].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > target) break;
      this.now = next[1].at;
      this.timers.delete(next[0]);
      next[1].fn();
      await flush();
    }
    this.now = target;
    await flush();
  }
}
const flush = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};
function snap(
  id = "ABCD-001",
  source = "a".repeat(32),
  status = "running",
): Snapshot {
  return {
    schema_version: 1,
    source_id: source,
    snapshot_revision: "b".repeat(64),
    captured_at: "2026-09-22T00:00:00Z",
    consistency: "consistent",
    task: {
      task_id: id,
      title: "中文\n <script>literal</script>",
      executor: "codex",
      status,
      updated_at: null,
      attempt_id: null,
      run_id: null,
      steps: [],
      waiting: null,
      error: null,
      callback_state: "missing",
      capabilities: { execution_scope: "unknown", full_lifecycle: "unknown" },
    },
    text: {
      report: {
        state: "available",
        scope: "task",
        text: "not shown",
        truncated: false,
      },
      execution: {
        state: "unavailable",
        scope: "none",
        text: null,
        truncated: false,
      },
    },
  };
}
function page(
  ids = ["ABCD-001"],
  source = "a".repeat(32),
  next: string | null = null,
): Page {
  return {
    schema_version: 1,
    source_id: source,
    catalogue_revision: "c".repeat(32),
    next_cursor: next,
    items: ids.map((id) => ({
      task_id: id,
      title: id,
      executor: "codex",
      status: "running",
      updated_at: null,
      title_truncated: false,
      error_code: null,
    })),
  };
}
function deferred<T>() {
  let resolve!: (v: T) => void, reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
}
class MemoryTransport implements SnapshotTransport {
  calls = { list: 0, detail: 0, report: 0 };
  listFn = async (
    _cursor: string | null,
    _signal: AbortSignal,
    _group: Group,
  ) => page();
  detailFn = async (id: string, _signal: AbortSignal) => snap(id);
  reportFn = async (_id: string, _signal: AbortSignal) => "中文 report";
  list(c: string | null, s: AbortSignal, g: Group) {
    this.calls.list++;
    return this.listFn(c, s, g);
  }
  detail(id: string, s: AbortSignal) {
    this.calls.detail++;
    return this.detailFn(id, s);
  }
  report(id: string, s: AbortSignal) {
    this.calls.report++;
    return this.reportFn(id, s);
  }
}
async function setup(t = new MemoryTransport()) {
  const clock = new Clock(),
    c = new TaskController(t, { clock });
  c.start();
  await flush();
  return { c, t, clock };
}
test("HTTP and memory transports produce identical view state including cancellable plain report", async () => {
  const mem = await setup();
  const fetcher = (async (input, init) => {
    assert.equal(init?.credentials, "omit");
    assert.equal((init?.headers as any).Authorization, "Bearer private");
    const url = String(input);
    return new Response(
      url.endsWith("/report")
        ? "中文 report"
        : JSON.stringify(url.endsWith("/snapshot") ? snap() : page()),
    );
  }) as typeof fetch;
  const c = new TaskController(new HttpTransport("private", "", fetcher), {
    clock: new Clock(),
  });
  c.start();
  await flush();
  assert.deepEqual(fields(c.state.detail!), fields(mem.c.state.detail!));
  assert.deepEqual(await c.openReport(), await mem.c.openReport());
  c.dispose();
  mem.c.dispose();
});
test("A B A late responses ignoring abort never replace the last selection", async () => {
  const { c, t } = await setup();
  const pending: ReturnType<typeof deferred<Snapshot>>[] = [];
  t.detailFn = () => {
    const d = deferred<Snapshot>();
    pending.push(d);
    return d.promise;
  };
  c.select("ABCD-001");
  await flush();
  c.select("BCDE-001");
  await flush();
  c.select("ABCD-001");
  await flush();
  pending[2].resolve({ ...snap(), snapshot_revision: "f".repeat(64) });
  await flush();
  pending[1].resolve(snap("BCDE-001"));
  pending[0].resolve(snap());
  await flush();
  assert.equal(c.state.detail?.snapshot_revision, "f".repeat(64));
  c.dispose();
});
test("group change rejects old list and detail; empty group clears view", async () => {
  const { c, t } = await setup();
  const old = deferred<Page>();
  t.listFn = () => old.promise;
  c.refresh();
  await flush();
  t.listFn = async () => page([]);
  c.setGroup("completed");
  await flush();
  old.resolve(page(["BCDE-001"]));
  await flush();
  assert.equal(c.state.group, "completed");
  assert.equal(c.state.detail, null);
  assert.equal(c.state.page?.items.length, 0);
  c.dispose();
});
test("terminal transition and pagination preserve selected detail, expiration resets page with notice", async () => {
  const { c, t } = await setup();
  t.detailFn = async (id) => snap(id, "a".repeat(32), "completed");
  t.listFn = async (cursor) => {
    if (cursor) throw new ReadError("cursor_expired", 409);
    return page(["BCDE-001"], "a".repeat(32), "next");
  };
  c.refresh();
  await flush();
  assert.equal(c.state.selected, "ABCD-001");
  assert.match(selectionNotice(c.state)!, /已完成/);
  c.next();
  await flush();
  assert.equal(c.state.cursor, null);
  assert.match(c.state.notice!, /过期/);
  assert.equal(c.state.selected, "ABCD-001");
  c.dispose();
});
test("new source resets all flights and cursor while old source responses cannot return", async () => {
  const { c, t } = await setup();
  const old = deferred<Snapshot>();
  t.detailFn = () => old.promise;
  c.refresh();
  await flush();
  t.listFn = async () => page(["BCDE-001"], "d".repeat(32));
  t.detailFn = async (id) => snap(id, "d".repeat(32));
  c.first();
  await flush();
  old.resolve(snap());
  await flush();
  assert.equal(c.state.source, "d".repeat(32));
  assert.equal(c.state.selected, "BCDE-001");
  assert.equal(c.state.detail?.source_id, c.state.source);
  c.dispose();
});
test("timeouts and transient failures back off deterministically and manual refresh recovers", async () => {
  const { c, t, clock } = await setup();
  t.detailFn = () => new Promise(() => {});
  c.select("ABCD-001");
  await flush();
  await clock.advance(5000);
  assert.equal(c.state.detailError, "request_timeout");
  const n = t.calls.detail;
  await clock.advance(1999);
  assert.equal(t.calls.detail, n);
  await clock.advance(1);
  assert.equal(t.calls.detail, n + 1);
  t.detailFn = async () => {
    throw new ReadError("snapshot_changed", 503);
  };
  c.refresh();
  await flush();
  const next = t.calls.detail;
  await clock.advance(3999);
  assert.equal(t.calls.detail, next);
  await clock.advance(1);
  assert.equal(t.calls.detail, next + 1);
  t.detailFn = async (id) => snap(id);
  c.refresh();
  await flush();
  assert.equal(c.state.detailError, null);
  const recovered = t.calls.detail;
  await clock.advance(2000);
  assert.equal(t.calls.detail, recovered + 1);
  c.dispose();
  assert.equal(clock.timers.size, 0);
});
test("401 and 403 stop all reads, including manual actions", async () => {
  for (const code of [401, 403]) {
    const { c, t, clock } = await setup();
    t.detailFn = async () => {
      throw new ReadError(
        code === 401 ? "authentication_required" : "access_forbidden",
        code,
      );
    };
    c.refresh();
    await flush();
    const calls = { ...t.calls };
    await clock.advance(60000);
    c.refresh();
    c.select("BCDE-001");
    await flush();
    assert.deepEqual(t.calls, calls);
    assert.ok(c.state.authError);
    c.dispose();
  }
});
test("404 clears old detail; 422 413 and malformed responses mark preserved data stale", async () => {
  for (const code of [404, 422, 413, 0]) {
    const { c, t } = await setup();
    t.detailFn = async () => {
      throw new ReadError(
        code === 0 ? "protocol_invalid" : "snapshot_task_not_found",
        code,
      );
    };
    c.refresh();
    await flush();
    assert.ok(c.state.detailError);
    assert.equal(c.state.detail === null, code === 404);
    c.dispose();
  }
});
test("attempt/run replaces full snapshot; report deduplicates and old errors do not affect new task", async () => {
  const { c, t } = await setup();
  t.detailFn = async (id) => ({
    ...snap(id),
    task: {
      ...snap(id).task,
      attempt_id: "attempt-2",
      run_id: "new",
      steps: [],
    },
  });
  c.refresh();
  await flush();
  assert.equal(c.state.detail?.task.run_id, "new");
  const d = deferred<string>();
  t.reportFn = () => d.promise;
  const result = c.openReport();
  await flush();
  assert.equal(await c.openReport(), undefined);
  assert.equal(t.calls.report, 1);
  c.select("BCDE-001");
  await flush();
  d.reject(new Error("late failure"));
  assert.equal(await result, undefined);
  await flush();
  assert.equal(c.state.reportError, null);
  assert.equal(c.state.selected, "BCDE-001");
  c.dispose();
});
test("dispose and repeated start never duplicate polling or retain listeners", async () => {
  const { c, t, clock } = await setup();
  c.start();
  const calls = t.calls.list;
  await clock.advance(2000);
  assert.equal(t.calls.list, calls + 1);
  let events = 0;
  c.subscribe(() => events++);
  c.dispose();
  const last = events;
  await clock.advance(60000);
  c.start();
  await flush();
  assert.equal(events, last);
  assert.equal(clock.timers.size, 0);
});
test("HTTP bounds utf8 bytes, typed status, malformed json and wrong task identity", async () => {
  for (const [body, statusCode, expected] of [
    ["中文".repeat(100000), 200, "response_too_large"],
    ["bad", 200, "protocol_invalid"],
    [JSON.stringify(snap("BCDE-001")), 200, "protocol_invalid"],
    ["", 401, "authentication_required"],
    ["", 403, "access_forbidden"],
  ] as const) {
    const t = new HttpTransport(
      "t",
      "",
      (async () => new Response(body, { status: statusCode })) as typeof fetch,
    );
    await assert.rejects(
      t.detail("ABCD-001", new AbortController().signal),
      new RegExp(expected),
    );
  }
});
test("120 tasks paginate without changing selection and retain frozen catalogue", async () => {
  const t = new MemoryTransport();
  const ids = Array.from(
    { length: 120 },
    (_, i) => `ABCD-${String(i + 1).padStart(3, "0")}`,
  );
  t.listFn = async (cursor) => {
    const offset = Number(cursor ?? 0);
    return page(
      ids.slice(offset, offset + 50),
      "a".repeat(32),
      offset + 50 < 120 ? String(offset + 50) : null,
    );
  };
  const { c } = await setup(t);
  const seen = [...c.state.page!.items];
  c.next();
  await flush();
  seen.push(...c.state.page!.items);
  c.next();
  await flush();
  seen.push(...c.state.page!.items);
  assert.equal(new Set(seen.map((r) => r.task_id)).size, 120);
  assert.equal(c.state.selected, "ABCD-001");
  assert.match(selectionNotice(c.state)!, /不在本页/);
  c.dispose();
});
test("backoff caps at thirty seconds and ten-minute fake-clock run has bounded timers", async () => {
  const { c, t, clock } = await setup();
  const moments: number[] = [];
  t.detailFn = async () => {
    moments.push(clock.now);
    throw new ReadError("snapshot_changed", 503);
  };
  c.refresh();
  await flush();
  for (const delay of [2000, 4000, 8000, 16000, 30000, 30000])
    await clock.advance(delay);
  assert.deepEqual(moments, [0, 2000, 6000, 14000, 30000, 60000, 90000]);
  t.detailFn = async (id) => snap(id);
  c.refresh();
  await flush();
  await clock.advance(600000);
  assert.ok(clock.timers.size <= 2);
  assert.equal(c.state.detailError, null);
  c.dispose();
});
test("late fulfilled report is discarded after task switch; unmount settles ignored cancellation", async () => {
  const { c, t, clock } = await setup();
  const d = deferred<string>();
  t.reportFn = () => d.promise;
  const result = c.openReport();
  await flush();
  c.select("BCDE-001");
  await flush();
  d.resolve("old report");
  assert.equal(await result, undefined);
  const hanging = c.openReport();
  await flush();
  c.dispose();
  await hanging;
  assert.equal(clock.timers.size, 0);
});

test("fetch adapter preserves native global receiver and forwards cancellation", async () => {
  const abort = new AbortController();
  let receiver: unknown;
  let signal: unknown;
  const fetcher = async function (
    this: unknown,
    _url: unknown,
    init?: RequestInit,
  ) {
    receiver = this;
    signal = init?.signal;
    return new Response(JSON.stringify(page()));
  };
  await new HttpTransport("t", "", fetcher as typeof fetch).list(
    null,
    abort.signal,
    "active",
  );
  assert.equal(receiver, globalThis);
  assert.equal(signal, abort.signal);
});
