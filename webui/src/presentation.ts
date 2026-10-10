import type { Snapshot, TextValue } from "./protocol.ts";
const statuses: Record<string, string> = {
  pending: "待派发",
  running: "执行中",
  input_required: "等待输入",
  needs_recovery: "待恢复",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  rejected: "已拒绝",
  assigned: "已分配",
  working: "执行中",
  in_progress: "执行中",
  pause_pending: "暂停处理中",
  paused: "已暂停",
  review_required: "待审核",
};
const steps: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  in_progress: "执行中",
  done: "已完成",
  blocked: "受阻",
  failed: "失败",
  inherited_done: "继承完成",
};
export const value = (v: string | null) => (v === null ? "未提供" : v);
export function status(v: string | null, step = false) {
  if (v === null) return "未提供";
  const label = (step ? steps : statuses)[v];
  return label ? `${label}（${v}）` : `未知状态（${v}）`;
}
export function timestamp(v: string | null) {
  return v === null
    ? "未提供"
    : !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(v) ||
        Number.isNaN(Date.parse(v))
      ? "未提供（时间格式无效）"
      : v;
}
export function text(v: TextValue) {
  return v.state === "available"
    ? (v.text ?? "不可用")
    : v.state === "missing"
      ? "暂无"
      : "不可用";
}
export interface Field {
  key: string;
  label: string;
  text: string;
}
export function fields(s: Snapshot): Field[] {
  const t = s.task;
  const result: Field[] = [
    { key: "task_id", label: "任务", text: t.task_id },
    { key: "title", label: "标题", text: value(t.title) },
    { key: "executor", label: "执行器", text: value(t.executor) },
    { key: "status", label: "状态", text: status(t.status) },
    {
      key: "duration",
      label: t.duration?.label ?? "任务历时",
      text:
        t.duration?.display === "unknown"
          ? "未提供"
          : (t.duration?.display ?? "未提供"),
    },
    { key: "updated_at", label: "更新时间", text: timestamp(t.updated_at) },
  ];
  if (
    t.waiting &&
    !["answered", "expired", "cancelled"].includes(t.waiting.status ?? "")
  )
    result.push({
      key: "waiting",
      label: "等待输入",
      text: value(t.waiting.summary),
    });
  if (t.error)
    result.push({
      key: "error",
      label: "最近错误",
      text: `${value(t.error.code)}\n${value(t.error.message)}`,
    });
  return result;
}
