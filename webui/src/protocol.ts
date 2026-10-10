export interface TextValue {
  state: "available" | "missing" | "unavailable";
  scope: "task" | "none";
  text: string | null;
  truncated: boolean;
}
export interface Task {
  duration?: { seconds: number | null; label: string; display: string };
  task_id: string;
  attempt_id: string | null;
  run_id: string | null;
  title: string | null;
  executor: string | null;
  status: string | null;
  updated_at: string | null;
  steps: {
    id: number | null;
    description: string | null;
    status: string | null;
  }[];
  waiting: {
    type: string | null;
    status: string | null;
    summary: string | null;
  } | null;
  error: { code: string | null; message: string | null } | null;
  callback_state: "received" | "missing" | "unknown";
  capabilities: {
    execution_scope: "basic" | "unknown";
    full_lifecycle: "unverified" | "unknown";
  };
}
export interface Snapshot {
  schema_version: 1;
  source_id: string;
  snapshot_revision: string;
  captured_at: string;
  consistency: "consistent";
  task: Task;
  text: { report: TextValue; execution: TextValue };
}
export interface Row {
  task_id: string;
  title: string | null;
  executor: string | null;
  status: string | null;
  updated_at: string | null;
  title_truncated: boolean;
  error_code: string | null;
}
export interface Page {
  schema_version: 1;
  source_id: string;
  catalogue_revision: string;
  items: Row[];
  next_cursor: string | null;
}
const obj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const nullable = (v: unknown) => v === null || typeof v === "string";
const strings = (v: Record<string, unknown>, keys: string[]) =>
  keys.every((k) => nullable(v[k]));
const id = (v: unknown) =>
  typeof v === "string" &&
  /^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4,}-\d{3}$/.test(v);
function base(v: unknown): v is Record<string, unknown> {
  return (
    obj(v) &&
    v.schema_version === 1 &&
    typeof v.source_id === "string" &&
    /^[a-f0-9]{32}$/.test(v.source_id)
  );
}
function text(v: unknown) {
  return (
    obj(v) &&
    ["available", "missing", "unavailable"].includes(String(v.state)) &&
    ["task", "none"].includes(String(v.scope)) &&
    nullable(v.text) &&
    typeof v.truncated === "boolean"
  );
}
export function snapshot(v: unknown): Snapshot {
  if (
    !base(v) ||
    v.consistency !== "consistent" ||
    typeof v.snapshot_revision !== "string" ||
    !/^[a-f0-9]{64}$/.test(v.snapshot_revision) ||
    typeof v.captured_at !== "string" ||
    !obj(v.task) ||
    !obj(v.text)
  )
    throw Error("protocol_invalid");
  const t = v.task;
  if (
    t.duration !== undefined &&
    (!obj(t.duration) ||
      !(
        t.duration.seconds === null ||
        (typeof t.duration.seconds === "number" &&
          Number.isFinite(t.duration.seconds) &&
          t.duration.seconds >= 0)
      ) ||
      typeof t.duration.label !== "string" ||
      typeof t.duration.display !== "string")
  )
    throw Error("protocol_invalid");
  if (
    !id(t.task_id) ||
    !strings(t, [
      "attempt_id",
      "run_id",
      "title",
      "executor",
      "status",
      "updated_at",
    ]) ||
    !Array.isArray(t.steps) ||
    !t.steps.every(
      (s) =>
        obj(s) &&
        (s.id === null || Number.isInteger(s.id)) &&
        strings(s, ["description", "status"]),
    ) ||
    !(
      t.waiting === null ||
      (obj(t.waiting) && strings(t.waiting, ["type", "status", "summary"]))
    ) ||
    !(
      t.error === null ||
      (obj(t.error) && strings(t.error, ["code", "message"]))
    ) ||
    !["received", "missing", "unknown"].includes(String(t.callback_state)) ||
    !obj(t.capabilities) ||
    !["basic", "unknown"].includes(String(t.capabilities.execution_scope)) ||
    !["unverified", "unknown"].includes(
      String(t.capabilities.full_lifecycle),
    ) ||
    !text(v.text.report) ||
    !text(v.text.execution)
  )
    throw Error("protocol_invalid");
  return v as unknown as Snapshot;
}
export function page(v: unknown): Page {
  if (
    !base(v) ||
    typeof v.catalogue_revision !== "string" ||
    !Array.isArray(v.items) ||
    v.items.length > 200 ||
    !nullable(v.next_cursor) ||
    !v.items.every(
      (r) =>
        obj(r) &&
        id(r.task_id) &&
        strings(r, [
          "title",
          "executor",
          "status",
          "updated_at",
          "error_code",
        ]) &&
        typeof r.title_truncated === "boolean",
    )
  )
    throw Error("protocol_invalid");
  return v as unknown as Page;
}
