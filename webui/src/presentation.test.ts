import test from "node:test";
import assert from "node:assert/strict";
import { fields, status, text, timestamp } from "./presentation.ts";
import { snapshot, page } from "./protocol.ts";
const example = () => ({
  schema_version: 1,
  source_id: "a".repeat(32),
  snapshot_revision: "b".repeat(64),
  captured_at: "2026-09-21T00:00:00Z",
  consistency: "consistent",
  task: {
    task_id: "ABCD-001",
    attempt_id: null,
    run_id: null,
    title: "中文\n  空格  <script>keep</script>",
    executor: "opencode",
    status: "failed",
    updated_at: null,
    steps: [{ id: 1, description: "中文\n第二行", status: "pending" }],
    waiting: null,
    error: { code: "completion_marker_missing", message: "missing callback" },
    callback_state: "missing",
    capabilities: { execution_scope: "basic", full_lifecycle: "unverified" },
  },
  text: {
    report: { state: "available", scope: "task", text: "", truncated: false },
    execution: {
      state: "unavailable",
      scope: "none",
      text: null,
      truncated: false,
    },
  },
});
test("same snapshot preserves text and failure semantics", () => {
  const s = snapshot(example()),
    copy = JSON.stringify(s);
  const map = Object.fromEntries(fields(s).map((f) => [f.key, f.text]));
  assert.equal(map.title, s.task.title);
  assert.equal(map.status, "失败（failed）");
  assert.equal(map.callback_state, undefined);
  assert.equal(map.report, undefined);
  assert.equal(map.execution, undefined);
  assert.equal(map.attempt_id, undefined);
  assert.equal(map.capabilities, undefined);
  assert.equal(map.duration, "未提供");
  assert.equal(JSON.stringify(s), copy);
});
test("unknown, null, dates and text availability are distinct", () => {
  assert.equal(status("future"), "未知状态（future）");
  assert.equal(status(null), "未提供");
  assert.equal(timestamp("invalid"), "未提供（时间格式无效）");
  assert.equal(timestamp("2026-09-21T00:00:00Z"), "2026-09-21T00:00:00Z");
  assert.equal(
    text({ state: "missing", scope: "none", text: null, truncated: false }),
    "暂无",
  );
});
test("runtime guards reject wrong version and malformed fields", () => {
  const s = example();
  assert.throws(() => snapshot({ ...s, schema_version: 2 }));
  assert.throws(() => snapshot({ ...s, task: { ...s.task, steps: [null] } }));
  assert.throws(() => snapshot({ ...s, text: { report: {}, execution: {} } }));
  assert.throws(() =>
    page({ schema_version: 1, source_id: "bad", items: [], next_cursor: null }),
  );
});
