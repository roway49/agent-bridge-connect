import { page, snapshot } from "./protocol.ts";
import type { Page, Snapshot } from "./protocol.ts";
export type Group = "active" | "completed" | "failed";
export interface SnapshotTransport {
  list(cursor: string | null, signal: AbortSignal, group: Group): Promise<Page>;
  detail(id: string, signal: AbortSignal): Promise<Snapshot>;
  report(id: string, signal: AbortSignal): Promise<string>;
}
export class ReadError extends Error {
  status: number;
  constructor(code: string, status = 0) {
    super(code);
    this.status = status;
  }
}
export class HttpTransport implements SnapshotTransport {
  private token: string;
  private base: string;
  private fetcher: typeof fetch;
  constructor(token: string, base = "", fetcher: typeof fetch = fetch) {
    this.token = token;
    this.base = base;
    this.fetcher = fetcher;
  }
  private async read(
    path: string,
    signal: AbortSignal,
    report = false,
  ): Promise<string> {
    const response = await this.fetcher.call(globalThis, this.base + path, {
      headers: { Authorization: `Bearer ${this.token}` },
      signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    if (response.status === 401 || response.status === 403)
      throw new ReadError(
        response.status === 401
          ? "authentication_required"
          : "access_forbidden",
        response.status,
      );
    const limit = report ? 1024 * 1024 : 256 * 1024;
    const reader = response.body?.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    if (reader) {
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > limit) {
            await reader.cancel();
            throw new ReadError("response_too_large", 413);
          }
          chunks.push(value);
        }
      } finally {
        reader.releaseLock();
      }
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const c of chunks) {
      bytes.set(c, offset);
      offset += c.length;
    }
    const text = new TextDecoder().decode(bytes);
    if (!response.ok) {
      let code = "request_failed";
      try {
        const error = JSON.parse(text)?.error?.code;
        if (typeof error === "string") code = error;
      } catch {}
      throw new ReadError(code, response.status);
    }
    return text;
  }
  private async get(path: string, signal: AbortSignal): Promise<unknown> {
    const text = await this.read(path, signal);
    try {
      return JSON.parse(text);
    } catch {
      throw new ReadError("protocol_invalid");
    }
  }
  async list(cursor: string | null, signal: AbortSignal, group: Group) {
    return page(
      await this.get(
        "/api/v1/tasks?limit=50&group=" +
          group +
          (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""),
        signal,
      ),
    );
  }
  async detail(id: string, signal: AbortSignal) {
    const result = snapshot(
      await this.get(
        "/api/v1/tasks/" + encodeURIComponent(id) + "/snapshot",
        signal,
      ),
    );
    if (result.task.task_id !== id) throw new ReadError("protocol_invalid");
    return result;
  }
  report(id: string, signal: AbortSignal) {
    return this.read(
      "/api/v1/tasks/" + encodeURIComponent(id) + "/report",
      signal,
      true,
    );
  }
}
