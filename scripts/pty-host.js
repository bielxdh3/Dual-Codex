"use strict";

const net = require("node:net");
const pty = require("node-pty");

const values = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  values.set(process.argv[index], process.argv[index + 1] ?? "");
}

const sessionId = values.get("--session-id");
const pipePath = values.get("--pipe");
const cwd = values.get("--cwd");
const codexCommand = values.get("--codex-command") || "codex";
const sandbox = values.get("--sandbox") || "read-only";
const approvalPolicy = values.get("--approval-policy") || "on-request";
const model = values.get("--model") || "";
const addDir = values.get("--add-dir") || "";

if (!/^[A-Za-z0-9_-]{1,96}$/.test(sessionId || "")) throw new Error("invalid session id");
if (!/^\\\\\.\\pipe\\dual-codex-[A-Za-z0-9_-]{1,160}$/.test(pipePath || "")) throw new Error("invalid named pipe");
if (!cwd || /[\r\n]/.test(cwd)) throw new Error("invalid working directory");
if (!new Set(["read-only", "workspace-write"]).has(sandbox)) throw new Error("invalid sandbox");
if (!new Set(["on-request", "never"]).has(approvalPolicy)) throw new Error("invalid approval policy");
if (addDir && /[\r\n]/.test(addDir)) throw new Error("invalid additional directory");

function quoteCmdArg(value) {
  const text = String(value);
  if (!text || /[\s"&|<>^\r\n]/.test(text)) {
    return `"${text.replace(/(\\*)"/g, "$1$1\\\"").replace(/(\\+)$/g, "$1$1")}"`;
  }
  return text;
}

const launchArgs = [
  codexCommand,
  "--no-alt-screen",
  "--cd",
  cwd,
  "--sandbox",
  sandbox,
  "-a",
  approvalPolicy,
  "--disable",
  "apps",
];
if (model) launchArgs.push("--model", model);
if (addDir) launchArgs.push("--add-dir", addDir);
const launchLine = launchArgs.map(quoteCmdArg).join(" ");
const output = [];
let outputChars = 0;
let nextSequence = 1;
const maxOutputChars = 250000;
const maxOutputRecords = 8192;
const maxInputBytes = 8192;
const leases = new Map();
let implicitWriter = "";
const state = {
  session_id: sessionId,
  pid: null,
  alive: true,
  started_at: new Date().toISOString(),
  last_activity: new Date().toISOString(),
  output_chars: 0,
  exit_code: null,
  update_skipped: false,
  process_epoch: values.get("--process-epoch") || "",
  process_start_identity: values.get("--process-start-identity") || "",
  host_pid: process.pid,
};
let updatePromptHandled = false;

function appendOutput(data) {
  const text = String(data);
  if (!text) return;
  output.push({ seq: nextSequence++, text });
  outputChars += text.length;
  state.output_chars += text.length;
  state.last_activity = new Date().toISOString();
  while (outputChars > maxOutputChars || output.length > maxOutputRecords) outputChars -= output.shift().text.length;
  const buffered = output.map((item) => item.text).join("");
  if (!updatePromptHandled && buffered.includes("Update now") && buffered.includes("Skip")) {
    updatePromptHandled = true;
    state.update_skipped = true;
    setTimeout(() => {
      if (state.alive && terminal) terminal.write("2\r");
    }, 50);
  }
}

function response(socket, value) {
  socket.write(`${JSON.stringify(value)}\n`);
}

let terminal;
try {
  terminal = pty.spawn(process.env.ComSpec || "C:\\Windows\\System32\\cmd.exe", ["/d"], {
    name: "xterm-color",
    cols: 160,
    rows: 50,
    cwd,
    env: process.env,
  });
  state.pid = terminal.pid;
  terminal.onData(appendOutput);
  terminal.onExit(({ exitCode }) => {
    state.alive = false;
    state.exit_code = exitCode;
    state.last_activity = new Date().toISOString();
  });
  terminal.write(`${launchLine}\r`);
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
}

function tail(lines) {
  const text = output.map((item) => item.text).join("");
  return text.split(/\r?\n/).slice(-Math.max(1, Math.min(Number(lines) || 80, 1000))).join("\n");
}

function utf8PrefixLength(bytes, limit) {
  let end = Math.min(bytes.length, limit);
  while (end > 0) {
    try {
      new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, end));
      return end;
    } catch (_) {
      end -= 1;
    }
  }
  if (bytes.length && limit > 0) {
    for (let size = 1; size <= Math.min(4, bytes.length); size += 1) {
      try {
        new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, size));
        return size;
      } catch (_) {}
    }
  }
  return 0;
}

function liveOutput(since, maxBytes, offset = 0) {
  if (!Number.isInteger(since) || since < 0) throw new Error("invalid output cursor");
  if (!Number.isInteger(offset) || offset < 0) throw new Error("invalid output cursor offset");
  if (!Number.isInteger(maxBytes) || maxBytes < 1 || maxBytes > 65536) throw new Error("invalid output read size");
  const oldest = output.length ? output[0].seq : nextSequence;
  const newest = nextSequence - 1;
  const behind = offset > 0 ? since < oldest : since < oldest - 1;
  let text = "";
  let next = since;
  let nextOffset = 0;
  for (const item of output) {
    if (item.seq < since || (item.seq === since && offset === 0)) continue;
    const available = maxBytes - Buffer.byteLength(text, "utf8");
    if (available <= 0) break;
    const bytes = Buffer.from(item.text, "utf8");
    const start = item.seq === since ? offset : 0;
    if (start > bytes.length) throw new Error("invalid output cursor offset");
    const remaining = bytes.subarray(start);
    const count = utf8PrefixLength(remaining, available);
    if (!count) break;
    text += remaining.subarray(0, count).toString("utf8");
    next = item.seq;
    if (count < remaining.length) {
      nextOffset = start + count;
      break;
    }
    nextOffset = 0;
  }
  return { output: text, next_seq: next, next_offset: nextOffset, oldest_seq: oldest, newest_seq: newest, behind_cursor: behind };
}

function leaseSnapshot() {
  const now = Date.now();
  for (const [owner, lease] of leases) if (lease.expires_at <= now) leases.delete(owner);
  const lease = [...leases.values()][0];
  return lease ? { active: true, owner: lease.owner, mode: lease.mode, expires_at: new Date(lease.expires_at).toISOString() } : { active: false };
}

function validOwner(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_.:-]{1,96}$/.test(value)) throw new Error("invalid input lease owner");
  return value;
}

function writerFor(request, { autoLease = false } = {}) {
  const requested = validOwner(request.writer || request.owner || implicitWriter || "");
  const current = leaseSnapshot();
  if (!current.active) {
    if (!autoLease) throw new Error("terminal input is not leased");
    leases.set(requested, { owner: requested, mode: "automation", expires_at: Date.now() + 900000 });
    implicitWriter = requested;
  } else if (current.owner !== requested) {
    throw new Error("terminal input is busy");
  }
  return requested;
}

function handle(socket, request) {
  if (!request || typeof request !== "object") throw new Error("request must be an object");
  switch (request.op) {
    case "status":
      return response(socket, { ok: true, state: { ...state, input_lease: leaseSnapshot() } });
    case "read":
      return response(socket, { ok: true, output: tail(request.lines) });
    case "read_since":
      return response(socket, { ok: true, ...liveOutput(request.since, request.max_bytes ?? 65536, request.offset ?? 0) });
    case "acquire_input_lease": {
      const owner = validOwner(request.owner);
      const mode = request.mode;
      const ttl = Number(request.ttl_ms);
      if (!["automation", "human"].includes(mode) || !Number.isInteger(ttl) || ttl < 100 || ttl > 3600000) throw new Error("invalid input lease");
      const current = leaseSnapshot();
      if (current.active && current.owner !== owner) throw new Error("terminal input is busy; attached viewer is watch-only");
      leases.set(owner, { owner, mode, expires_at: Date.now() + ttl });
      return response(socket, { ok: true, lease_acquired: true, input_lease: leaseSnapshot() });
    }
    case "renew_input_lease": {
      const owner = validOwner(request.owner);
      const ttl = Number(request.ttl_ms);
      if (!Number.isInteger(ttl) || ttl < 100 || ttl > 3600000 || !leaseSnapshot().active || leaseSnapshot().owner !== owner) throw new Error("input lease is not owned by this client");
      leases.set(owner, { ...leases.get(owner), expires_at: Date.now() + ttl });
      return response(socket, { ok: true, input_lease: leaseSnapshot() });
    }
    case "release_input_lease": {
      const owner = validOwner(request.owner);
      const current = leaseSnapshot();
      if (current.active && current.owner !== owner) throw new Error("input lease is owned by another client");
      leases.delete(owner);
      if (implicitWriter === owner) implicitWriter = "";
      return response(socket, { ok: true, input_lease: leaseSnapshot() });
    }
    case "send_text":
      if (typeof request.message !== "string" || request.message.length === 0 || request.message.length > 500) throw new Error("message must be non-empty text of at most 500 characters");
      if (/[\r\n]/.test(request.message)) throw new Error("message must be a single physical line");
      if (request.message.includes("\u0000")) throw new Error("message contains NUL");
      writerFor(request, { autoLease: Boolean(request.auto_lease) });
      if (request.auto_lease) implicitWriter = request.writer;
      terminal.write(request.message);
      state.last_activity = new Date().toISOString();
      return response(socket, { ok: true, lease_acquired: Boolean(request.auto_lease), writer: request.writer });
    case "submit":
      writerFor(request);
      terminal.write("\r");
      state.last_activity = new Date().toISOString();
      return response(socket, { ok: true });
    case "write_input": {
      const data = request.data;
      writerFor(request);
      if (typeof data !== "string" || !data || Buffer.byteLength(data, "utf8") > maxInputBytes || data.includes("\u0000")) throw new Error("raw input is invalid or too large");
      terminal.write(data);
      state.last_activity = new Date().toISOString();
      return response(socket, { ok: true, bytes: Buffer.byteLength(data, "utf8") });
    }
    case "resize": {
      const cols = Number(request.cols);
      const rows = Number(request.rows);
      if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 20 || rows < 5 || cols > 500 || rows > 200) throw new Error("invalid terminal size");
      terminal.resize(cols, rows);
      return response(socket, { ok: true });
    }
    case "terminate":
      response(socket, { ok: true });
      terminal.write("\u0003");
      setTimeout(() => {
        if (state.alive) terminal.kill();
        server.close(() => process.exit(0));
      }, 750);
      return;
    default:
      throw new Error(`unknown operation: ${request.op}`);
  }
}

const server = net.createServer((socket) => {
  let pending = "";
  socket.on("data", (chunk) => {
    pending += chunk.toString("utf8");
    let newline = pending.indexOf("\n");
    while (newline >= 0) {
      const line = pending.slice(0, newline).trim();
      pending = pending.slice(newline + 1);
      if (line) {
        try {
          handle(socket, JSON.parse(line));
        } catch (error) {
          response(socket, { ok: false, error: error instanceof Error ? error.message : String(error) });
        }
      }
      newline = pending.indexOf("\n");
    }
  });
});

server.on("error", (error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
server.listen(pipePath);
