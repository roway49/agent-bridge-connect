import { ReadError } from "./client.ts";
import type { Group, SnapshotTransport } from "./client.ts";
import type { Page, Snapshot } from "./protocol.ts";
export interface Clock {
  set(fn: () => void, ms: number): unknown;
  clear(id: unknown): void;
}
const clock: Clock = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
};
export interface State {
  group: Group;
  cursor: string | null;
  selected: string | null;
  source: string | null;
  page: Page | null;
  detail: Snapshot | null;
  listError: string | null;
  detailError: string | null;
  listBusy: boolean;
  detailBusy: boolean;
  reportBusy: boolean;
  reportError: string | null;
  authError: string | null;
  notice: string | null;
}
type Channel = "list" | "detail" | "report";
type Flight = {
  abort: AbortController;
  epoch: number;
  timer: unknown;
  cancel: () => void;
};
export function taskGroup(status: string | null): Group {
  if (status === "completed") return "completed";
  return [
    "pending",
    "running",
    "assigned",
    "working",
    "in_progress",
    "input_required",
    "pause_pending",
    "paused",
    "review_required",
  ].includes(status ?? "")
    ? "active"
    : "failed";
}
export function selectionNotice(s: State): string | null {
  if (s.detail && taskGroup(s.detail.task.status) !== s.group)
    return `当前任务已移入${taskGroup(s.detail.task.status) === "completed" ? "已完成" : taskGroup(s.detail.task.status) === "active" ? "进行中" : "失败 / 待恢复"}分类，继续显示详情。`;
  if (
    s.selected &&
    s.page &&
    !s.page.items.some((r) => r.task_id === s.selected)
  )
    return "当前选中任务不在本页，继续显示详情。";
  return null;
}
export class TaskController {
  state: State = {
    group: "active",
    cursor: null,
    selected: null,
    source: null,
    page: null,
    detail: null,
    listError: null,
    detailError: null,
    listBusy: false,
    detailBusy: false,
    reportBusy: false,
    reportError: null,
    authError: null,
    notice: null,
  };
  private transport: SnapshotTransport;
  private clock: Clock;
  private listeners = new Set<(s: State) => void>();
  private active = false;
  private disposed = false;
  private epoch = 0;
  private flights: Partial<Record<Channel, Flight>> = {};
  private timers: Partial<Record<"list" | "detail", unknown>> = {};
  private failures = { list: 0, detail: 0 };
  constructor(
    transport: SnapshotTransport,
    options: { clock?: Clock; authenticated?: boolean } = {},
  ) {
    this.transport = transport;
    this.clock = options.clock ?? clock;
    if (options.authenticated === false)
      this.state.authError = "authentication_required";
  }
  subscribe(fn: (s: State) => void) {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }
  private emit() {
    for (const fn of this.listeners) fn(this.state);
  }
  start() {
    if (this.active || this.disposed || this.state.authError) return;
    this.active = true;
    void this.loadList();
  }
  dispose() {
    this.disposed = true;
    this.active = false;
    this.resetRequests();
    this.listeners.clear();
  }
  private allowed() {
    return this.active && !this.disposed && !this.state.authError;
  }
  private cancel(channel: Channel) {
    const f = this.flights[channel];
    delete this.flights[channel];
    if (f) {
      this.clock.clear(f.timer);
      f.abort.abort();
      f.cancel();
    }
    if (channel === "list" || channel === "detail") {
      this.clock.clear(this.timers[channel]);
      delete this.timers[channel];
    }
    this.state[`${channel}Busy`] = false;
  }
  private resetRequests() {
    ++this.epoch;
    for (const ch of ["list", "detail", "report"] as Channel[]) this.cancel(ch);
  }
  private valid(channel: Channel, f: Flight) {
    return (
      this.allowed() && f.epoch === this.epoch && this.flights[channel] === f
    );
  }
  private async request<T>(
    channel: Channel,
    read: (signal: AbortSignal) => Promise<T>,
    success: (v: T) => void,
  ): Promise<T | undefined> {
    this.cancel(channel);
    if (!this.allowed()) return;
    const abort = new AbortController();
    let reject!: (e: Error) => void;
    const stopped = new Promise<never>((_, r) => {
      reject = r;
    });
    const f: Flight = {
      abort,
      epoch: this.epoch,
      timer: null,
      cancel: () => reject(new ReadError("cancelled")),
    };
    this.flights[channel] = f;
    this.state[`${channel}Busy`] = true;
    f.timer = this.clock.set(() => {
      abort.abort();
      reject(new ReadError("request_timeout"));
    }, 5000);
    this.emit();
    try {
      const result = await Promise.race([
        Promise.resolve().then(() => read(abort.signal)),
        stopped,
      ]);
      if (!this.valid(channel, f)) return;
      if (channel !== "report") {
        this.failures[channel] = 0;
        this.state[channel === "list" ? "listError" : "detailError"] = null;
      } else this.state.reportError = null;
      success(result);
      return result;
    } catch (error) {
      if (!this.valid(channel, f)) return;
      const e = error instanceof Error ? error : new Error("network_error");
      if (e instanceof ReadError && (e.status === 401 || e.status === 403)) {
        this.state.authError = e.message;
        this.resetRequests();
      } else if (channel === "report") this.state.reportError = e.message;
      else if (channel === "list" && e.message === "cursor_expired") {
        this.state.cursor = null;
        this.state.notice = "分页已过期，已返回当前分类首页。";
        this.cancel("list");
        void this.loadList();
      } else {
        this.state[channel === "list" ? "listError" : "detailError"] =
          e.message;
        if (channel === "detail" && e instanceof ReadError && e.status === 404)
          this.state.detail = null;
        this.failures[channel]++;
      }
    } finally {
      this.clock.clear(f.timer);
      if (this.flights[channel] === f) {
        delete this.flights[channel];
        this.state[`${channel}Busy`] = false;
        if (channel !== "report" && this.allowed()) this.schedule(channel);
      }
      if (!this.disposed) this.emit();
    }
  }
  private schedule(channel: "list" | "detail") {
    this.clock.clear(this.timers[channel]);
    if (channel === "detail" && !this.state.selected) return;
    const delay = this.failures[channel]
      ? Math.min(2000 * 2 ** Math.min(this.failures[channel] - 1, 4), 30000)
      : 2000;
    this.timers[channel] = this.clock.set(() => {
      delete this.timers[channel];
      if (channel === "list") void this.loadList();
      else void this.loadDetail();
    }, delay);
  }
  private acceptSource(source: string): boolean {
    if (!this.state.source) {
      this.state.source = source;
      return true;
    }
    if (this.state.source === source) return true;
    this.resetRequests();
    this.failures = { list: 0, detail: 0 };
    Object.assign(this.state, {
      source,
      cursor: null,
      selected: null,
      page: null,
      detail: null,
      listError: null,
      detailError: null,
      reportError: null,
      notice: "数据源已变化，已重新加载当前分类。",
    });
    void this.loadList();
    return false;
  }
  private loadList() {
    const { cursor, group } = this.state;
    return this.request(
      "list",
      (signal) => this.transport.list(cursor, signal, group),
      (p) => {
        if (!this.acceptSource(p.source_id)) return;
        this.state.page = p;
        if (!this.state.selected && p.items.length)
          this.select(p.items[0].task_id);
      },
    );
  }
  private loadDetail() {
    const id = this.state.selected;
    if (!id) return;
    return this.request(
      "detail",
      (signal) => this.transport.detail(id, signal),
      (s) => {
        if (s.task.task_id !== id) throw new ReadError("protocol_invalid");
        if (!this.acceptSource(s.source_id)) return;
        this.state.detail = s;
      },
    );
  }
  select(id: string) {
    if (!this.allowed()) return;
    this.cancel("detail");
    this.cancel("report");
    this.failures.detail = 0;
    Object.assign(this.state, {
      selected: id,
      detail: null,
      detailError: null,
      reportError: null,
    });
    this.emit();
    void this.loadDetail();
  }
  setGroup(group: Group) {
    if (!this.allowed() || group === this.state.group) return;
    this.resetRequests();
    this.failures = { list: 0, detail: 0 };
    Object.assign(this.state, {
      group,
      cursor: null,
      selected: null,
      page: null,
      detail: null,
      listError: null,
      detailError: null,
      reportError: null,
      notice: null,
    });
    this.emit();
    void this.loadList();
  }
  next() {
    if (!this.allowed() || !this.state.page?.next_cursor) return;
    this.state.cursor = this.state.page.next_cursor;
    this.state.notice = null;
    void this.loadList();
  }
  first() {
    if (!this.allowed()) return;
    this.state.cursor = null;
    this.state.notice = null;
    void this.loadList();
  }
  refresh() {
    if (!this.allowed()) return;
    void this.loadList();
    void this.loadDetail();
  }
  async openReport(): Promise<{ taskId: string; text: string } | undefined> {
    const id = this.state.selected;
    if (
      !id ||
      !this.allowed() ||
      this.state.reportBusy ||
      this.state.detail?.text.report.state !== "available"
    )
      return;
    const epoch = this.epoch;
    const text = await this.request(
      "report",
      (signal) => this.transport.report(id, signal),
      () => {},
    );
    return text === undefined ||
      !this.allowed() ||
      epoch !== this.epoch ||
      id !== this.state.selected
      ? undefined
      : { taskId: id, text };
  }
}
