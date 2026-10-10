import { HttpTransport } from "./client.ts";
import { TaskController, selectionNotice } from "./controller.ts";
import type { State } from "./controller.ts";
import { fields, status, value } from "./presentation.ts";
import "./style.css";
function el<K extends keyof HTMLElementTagNameMap>(tag: K, text = "") {
  const e = document.createElement(tag);
  e.textContent = text;
  return e;
}
function text(e: HTMLElement, s: string) {
  if (e.textContent !== s) e.textContent = s;
}
const root = document.querySelector<HTMLDivElement>("#app")!;
const header = el("header");
const refresh = el("button", "刷新");
header.append(el("h1", "AgentBC"), el("span", "只读任务"), refresh);
const layout = el("main"),
  sidebar = el("aside"),
  detail = el("section"),
  categories = el("nav");
categories.setAttribute("aria-label", "任务分类");
const listMessage = el("p"),
  list = el("div"),
  first = el("button", "首页"),
  next = el("button", "下一页"),
  detailMessage = el("p"),
  notice = el("p"),
  content = el("div");
for (const p of [listMessage, detailMessage, notice])
  p.setAttribute("role", "status");
sidebar.append(el("h2", "任务"), categories, listMessage, list, first, next);
detail.append(detailMessage, notice, content);
layout.append(sidebar, detail);
root.append(header, layout);
const token = new URLSearchParams(location.hash.slice(1)).get("token") ?? "";
history.replaceState(null, "", location.pathname);
const controller = new TaskController(new HttpTransport(token), {
  authenticated: !!token,
});
for (const [id, label] of [
  ["active", "进行中"],
  ["completed", "已完成"],
  ["failed", "失败 / 待恢复"],
] as const) {
  const b = el("button", label);
  b.dataset.group = id;
  b.onclick = () => controller.setGroup(id);
  categories.append(b);
}
const blocks = new Map<string, HTMLElement>();
const taskButtons = new Map<string, HTMLButtonElement>();
let visibleTask: string | null = null;
const urls = new Map<string, ReturnType<typeof setTimeout>>();
function authMessage(code: string) {
  return code === "access_forbidden"
    ? "访问受限，请核对启动地址并重新打开有效链接。"
    : "认证失效，请重新打开终端中的完整启动链接。";
}
function errorMessage(code: string) {
  return code === "snapshot_task_not_found"
    ? "任务不存在。"
    : `刷新失败／内容可能过期（${code}）`;
}
function render(s: State) {
  refresh.disabled = !!s.authError;
  first.disabled = !!s.authError || !s.cursor;
  next.disabled = !!s.authError || !s.page?.next_cursor;
  for (const b of categories.querySelectorAll("button")) {
    b.disabled = !!s.authError;
    b.setAttribute("aria-pressed", String(b.dataset.group === s.group));
  }
  text(
    listMessage,
    s.authError
      ? authMessage(s.authError)
      : s.listError
        ? errorMessage(s.listError)
        : s.page
          ? `本页 ${s.page.items.length} 项`
          : s.listBusy
            ? "正在读取任务"
            : "暂无任务",
  );
  text(notice, [s.notice, selectionNotice(s)].filter(Boolean).join(" "));
  const ids = new Set(s.page?.items.map((r) => r.task_id) ?? []);
  for (const [id, b] of taskButtons)
    if (!ids.has(id)) {
      b.remove();
      taskButtons.delete(id);
    }
  for (const [i, row] of (s.page?.items ?? []).entries()) {
    let b = taskButtons.get(row.task_id);
    if (!b) {
      b = el("button");
      b.className = "task";
      b.dataset.taskId = row.task_id;
      b.onclick = () => controller.select(row.task_id);
      taskButtons.set(row.task_id, b);
    }
    text(
      b,
      `${row.task_id}\n${value(row.title)}${row.title_truncated ? "［标题已截断］" : ""}\n${value(row.executor)} · ${status(row.status)}${row.error_code ? "\n不可用（" + row.error_code + "）" : ""}`,
    );
    b.disabled = !!s.authError;
    b.setAttribute("aria-pressed", String(s.selected === row.task_id));
    if (list.children[i] !== b) list.insertBefore(b, list.children[i] ?? null);
  }
  text(
    detailMessage,
    s.authError
      ? authMessage(s.authError) + (s.detail ? " 内容可能过期。" : "")
      : s.detailError
        ? errorMessage(s.detailError)
        : s.reportError
          ? "报告暂时不可用，请刷新重试。"
          : s.detail
            ? "只读快照 · " + s.detail.captured_at
            : s.detailBusy
              ? "正在读取 " + s.selected
              : "请选择任务",
  );
  const taskId = s.detail?.task.task_id ?? null;
  if (visibleTask !== taskId) {
    content.replaceChildren();
    blocks.clear();
    visibleTask = taskId;
  }
  if (!s.detail) {
    delete content.dataset.taskId;
    delete content.dataset.revision;
    return;
  }
  content.dataset.taskId = taskId!;
  content.dataset.revision = s.detail.snapshot_revision;
  const desired = new Set<string>();
  let position = 0;
  function place(b: HTMLElement) {
    if (content.children[position] !== b)
      content.insertBefore(b, content.children[position] ?? null);
    position++;
  }

  function block(key: string, label: string, body: string, step?: number) {
    desired.add(key);
    let b = blocks.get(key);
    if (!b) {
      b = el("div");
      b.className = step === undefined ? "field" : "step";
      b.append(el("h3"), el("pre"));
      blocks.set(key, b);
    }
    text(b.children[0] as HTMLElement, label);
    const pre = b.children[1] as HTMLElement;
    text(pre, body);
    pre.dataset.field = key;
    if (step !== undefined) pre.dataset.step = String(step);
    place(b);
  }
  for (const f of fields(s.detail)) block(f.key, f.label, f.text);
  if (!s.detail.task.steps.length) block("steps", "步骤", "暂无步骤");
  s.detail.task.steps.forEach((step, i) =>
    block(
      "step-" + i,
      `步骤 ${step.id ?? i + 1} · ${status(step.status, true)}`,
      value(step.description),
      i,
    ),
  );
  desired.add("report");
  let report = blocks.get("report");
  if (!report) {
    report = el("div");
    report.className = "field";
    report.append(el("h3", "任务报告"), el("span"));
    blocks.set("report", report);
  }
  const available = s.detail.text.report.state === "available";
  const current = report.children[1];
  if (available) {
    let link: HTMLAnchorElement;
    if (current.tagName !== "A") {
      link = el("a");
      current.replaceWith(link);
      link.onclick = openReport;
    } else link = current as HTMLAnchorElement;
    text(link, `${taskId}-report.md`);
    link.href = `/api/v1/tasks/${taskId}/report`;
    link.title = "打开本地报告文档";
    link.setAttribute("aria-disabled", String(s.reportBusy || !!s.authError));
  } else {
    let span = current;
    if (span.tagName !== "SPAN") {
      span = el("span");
      current.replaceWith(span);
    }
    text(
      span as HTMLElement,
      s.detail.text.report.state === "missing" ? "暂无报告" : "报告不可用",
    );
  }
  place(report);
  for (const [key, b] of blocks)
    if (!desired.has(key)) {
      b.remove();
      blocks.delete(key);
    }
}
async function openReport(event: MouseEvent) {
  event.preventDefault();
  if (controller.state.reportBusy || controller.state.authError) return;
  const viewer = window.open("about:blank", "_blank");
  if (viewer) viewer.opener = null;
  const result = await controller.openReport();
  if (!result) {
    viewer?.close();
    return;
  }
  const url = URL.createObjectURL(
    new Blob([result.text], { type: "text/plain;charset=utf-8" }),
  );
  if (viewer && !viewer.closed) viewer.location.href = url;
  else {
    const a = el("a");
    a.href = url;
    a.download = `${result.taskId}-report.md`;
    a.click();
  }
  urls.set(
    url,
    setTimeout(() => {
      URL.revokeObjectURL(url);
      urls.delete(url);
    }, 60000),
  );
}
refresh.onclick = () => controller.refresh();
first.onclick = () => controller.first();
next.onclick = () => controller.next();
controller.subscribe(render);
controller.start();
window.addEventListener(
  "pagehide",
  () => {
    controller.dispose();
    for (const [url, timer] of urls) {
      clearTimeout(timer);
      URL.revokeObjectURL(url);
    }
    urls.clear();
  },
  { once: true },
);
// A restored page has no live controller; explicit launch authentication is required.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) location.reload();
});
