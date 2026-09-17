import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, chmodSync, unlinkSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import { spawn, spawnSync } from "node:child_process";
import readline from "node:readline";

const MAX = 1024 * 1024;

function arg(name) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : "";
}

function identity(pipe, dispatcher, resource) {
  return createHash("sha256")
    .update(JSON.stringify({ pipe, dispatcher, resource }))
    .digest("hex")
    .slice(0, 24);
}

function relayPaths(pipe, dispatcher, resource) {
  const root = `/tmp/agentbc-desktop-relay-${process.getuid()}`;
  mkdirSync(root, { recursive: true, mode: 0o700 });
  chmodSync(root, 0o700);
  const id = identity(pipe, dispatcher, resource);
  return { root, socket: `${root}/${id}.sock`, token: `${root}/${id}.token` };
}

function requestRelay(socketPath, token, message, timeout = 500) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(socketPath);
    let buffer = "";
    const timer = setTimeout(() => {
      client.destroy();
      reject(new Error("relay timeout"));
    }, timeout);
    client.on("connect", () => client.write(`${JSON.stringify({ token, message })}\n`));
    client.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      clearTimeout(timer);
      client.end();
      try { resolve(JSON.parse(buffer.slice(0, newline))); } catch (error) { reject(error); }
    });
    client.on("error", (error) => { clearTimeout(timer); reject(error); });
  });
}

async function readiness(socketPath, token, timeout = 500) {
  try {
    const response = await requestRelay(
      socketPath,
      token,
      { jsonrpc: "2.0", id: 1, method: "initialize", params: {} },
      timeout,
    );
    return response?.id === 1 && response?.result && typeof response.result === "object"
      ? "ready"
      : "missing";
  } catch (error) {
    return error?.code === "EPERM" || error?.code === "EACCES" ? "blocked" : "missing";
  }
}

async function bootstrap() {
  const pipe = process.env.CODEX_APP_TOOLS_PIPE_PATH || "";
  const dispatcher = process.env.CODEX_THREAD_ID || "";
  const runtime = process.env.CODEX_MCP_NODE_PATH || "";
  const resource = arg("--resource");
  const separator = process.argv.indexOf("--");
  if (!pipe || !dispatcher || !runtime || !resource || separator < 0) process.exit(125);
  const command = process.argv.slice(separator + 1);
  const paths = relayPaths(pipe, dispatcher, resource);
  let token = "";
  try { token = readFileSync(paths.token, "utf8").trim(); } catch {}
  let state = token ? await readiness(paths.socket, token) : "missing";
  if (state === "missing") {
    token = randomBytes(32).toString("hex");
    writeFileSync(paths.token, token, { mode: 0o600 });
    chmodSync(paths.token, 0o600);
    try { unlinkSync(paths.socket); } catch {}
    const child = spawn(process.execPath, [
      new URL(import.meta.url).pathname,
      "--serve",
      "--socket", paths.socket,
      "--token-file", paths.token,
      "--pipe", pipe,
      "--dispatcher", dispatcher,
      "--runtime", runtime,
      "--resource", resource,
    ], { detached: true, stdio: "ignore", env: process.env });
    child.unref();
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline && (state = await readiness(paths.socket, token, 250)) === "missing") {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }
  const environment = {
    ...process.env,
    AGENTBC_DESKTOP_BOOTSTRAPPED: "1",
    ...(state === "ready" ? {
      AGENTBC_DESKTOP_RELAY_SOCKET: paths.socket,
      AGENTBC_DESKTOP_RELAY_TOKEN: token,
    } : { AGENTBC_SKIP_DESKTOP_ROUTE_REGISTER: "1" }),
  };
  const result = spawnSync(command[0], command.slice(1), { stdio: "inherit", env: environment });
  process.exit(result.status ?? 1);
}

async function serve() {
  const socketPath = arg("--socket");
  const token = readFileSync(arg("--token-file"), "utf8").trim();
  const pipe = arg("--pipe");
  const dispatcher = arg("--dispatcher");
  const runtime = arg("--runtime");
  const resource = arg("--resource");
  const child = spawn(runtime, [resource, "--interaction-client-id", dispatcher], {
    stdio: ["pipe", "pipe", "ignore"],
    env: { ...process.env, CODEX_APP_TOOLS_PIPE_PATH: pipe },
  });
  const pending = new Map();
  const lines = readline.createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    const callback = pending.get(message.id);
    if (callback) { pending.delete(message.id); callback(message); }
  });
  let nextId = 10;
  const upstream = (method, params) => new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("upstream timeout")); }, 10000);
    pending.set(id, (message) => { clearTimeout(timer); resolve(message); });
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });
  const initialized = await upstream("initialize", {
    protocolVersion: "2025-11-25",
    capabilities: {},
    clientInfo: { name: "agentbc-desktop-relay", version: "1" },
  });
  if (!initialized?.result) process.exit(2);
  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);
  const listed = await upstream("tools/list", {});
  const tools = listed?.result?.tools || [];
  if (!tools.some((tool) => tool?.name === "set_thread_archived")) process.exit(3);

  const server = net.createServer((client) => {
    let buffer = "";
    client.on("data", async (chunk) => {
      buffer += chunk.toString("utf8");
      while (buffer.includes("\n")) {
        const newline = buffer.indexOf("\n");
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.length > MAX) { client.destroy(); return; }
        let envelope;
        try { envelope = JSON.parse(line); } catch { client.destroy(); return; }
        if (envelope.token !== token || !envelope.message) { client.destroy(); return; }
        const message = envelope.message;
        let response = null;
        if (message.method === "initialize") {
          response = { jsonrpc: "2.0", id: message.id, result: initialized.result };
        } else if (message.method === "tools/list") {
          response = { jsonrpc: "2.0", id: message.id, result: listed.result };
        } else if (message.method === "tools/call" && message.params?.name === "set_thread_archived") {
          try {
            response = await upstream("tools/call", message.params);
            response.id = message.id;
          } catch { client.destroy(); return; }
        } else if (message.method !== "notifications/initialized") {
          response = { jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "method not found" } };
        }
        if (response) client.write(`${JSON.stringify(response)}\n`);
      }
    });
  });
  try { unlinkSync(socketPath); } catch {}
  server.listen(socketPath, () => chmodSync(socketPath, 0o600));
  child.on("exit", () => server.close(() => process.exit(4)));
}

if (process.argv.includes("--serve")) await serve();
else await bootstrap();
