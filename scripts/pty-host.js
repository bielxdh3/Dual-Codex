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
const humanIdleLeaseMs = 5000;
const humanConfigurationFallbackMs = 15000;
const humanActiveLeaseMs = 3600000;
const configurationIdleGraceMs = 750;
const maxHumanComposerChars = 512;
const leases = new Map();
const viewers = new Map();
let implicitWriter = "";
let nextLeaseGeneration = 1;
const state = {
  session_id: sessionId,
  pipe: pipePath,
  host_pid: process.pid,
  host_started_at: Date.now() / 1000,
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
  observeConfigurationOutput(text);
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
  const terminalEnvironment = { ...process.env };
  if (!terminalEnvironment.TERM || terminalEnvironment.TERM.toLowerCase() === "dumb") {
    terminalEnvironment.TERM = "xterm-256color";
  }
  terminal = pty.spawn(process.env.ComSpec || "C:\\Windows\\System32\\cmd.exe", ["/d"], {
    name: "xterm-color",
    cols: 160,
    rows: 50,
    cwd,
    env: terminalEnvironment,
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
  for (const [owner, lease] of leases) {
    const ownerAlive = !lease.owner_pid || processAlive(lease.owner_pid);
    const humanIdle = lease.mode === "human"
      && !lease.composition_active
      && !lease.command_pending
      && !lease.configuration_pending
      && now - lease.last_actual_input_at >= humanIdleLeaseMs;
    if (lease.expires_at <= now || !ownerAlive || humanIdle) dropLease(owner);
  }
  const lease = [...leases.values()][0];
  return lease ? {
    active: true,
    owner: lease.owner,
    mode: lease.mode,
    generation: lease.generation,
    acquired_at: new Date(lease.acquired_at).toISOString(),
    last_actual_input_at: new Date(lease.last_actual_input_at).toISOString(),
    last_output_at: new Date(lease.last_output_at).toISOString(),
    expires_at: new Date(lease.expires_at).toISOString(),
    composition_active: Boolean(lease.composition_active),
    configuration_pending: Boolean(lease.configuration_pending),
    command_pending: Boolean(lease.command_pending),
    reason: lease.reason,
  } : { active: false };
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return true;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error && error.code === "EPERM") return true;
    return false;
  }
}

function validViewerEpoch(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{16,96}$/.test(value)) {
    throw new Error("invalid viewer epoch");
  }
  return value;
}

function viewerSnapshot() {
  for (const [owner, viewer] of viewers) {
    if (!processAlive(viewer.pid)) viewers.delete(owner);
  }
  const viewer = [...viewers.values()][0];
  return viewer ? {
    attached: true,
    owner: viewer.owner,
    pid: viewer.pid,
    process_start_identity: viewer.process_start_identity,
    viewer_epoch: viewer.viewer_epoch,
    attached_at: new Date(viewer.attached_at).toISOString(),
  } : { attached: false };
}

function dropLease(owner) {
  leases.delete(owner);
  if (implicitWriter === owner) implicitWriter = "";
}

function createLease(owner, mode, ttl, ownerPid = 0) {
  const now = Date.now();
  const initialTtl = mode === "human" ? humanIdleLeaseMs : ttl;
  const lease = {
    owner,
    mode,
    generation: nextLeaseGeneration++,
    owner_pid: Number.isInteger(ownerPid) && ownerPid > 0 ? ownerPid : 0,
    acquired_at: now,
    last_actual_input_at: now,
    last_output_at: now,
    expires_at: now + initialTtl,
    composition_active: false,
    configuration_pending: false,
    command_pending: false,
    configuration_idle_prompt: false,
    configuration_output: "",
    composer: "",
    reason: mode === "human" ? "human_attach" : "automation_turn",
  };
  leases.set(owner, lease);
  return lease;
}

function validGeneration(value) {
  if (value === undefined || value === null || value === "") return 0;
  const generation = Number(value);
  if (!Number.isInteger(generation) || generation < 1) throw new Error("invalid input lease generation");
  return generation;
}

function validOwnerPid(value) {
  if (value === undefined || value === null || value === "") return 0;
  const pid = Number(value);
  if (!Number.isInteger(pid) || pid < 1 || pid > 0x7fffffff) throw new Error("invalid input lease owner process");
  return pid;
}

function leaseGenerationMatches(lease, request) {
  const generation = validGeneration(request.generation);
  return !generation || lease.generation === generation;
}

function stripAnsi(text) {
  return String(text).replace(/\x1b(?:\[[0-?]*[ -\/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))/g, "");
}

function hasIdleComposer(text) {
  const clean = stripAnsi(text);
  return /(?:^|\n)\s*[›>]\s/.test(clean)
    && /(?:^|\n).*model:\s+\S+/i.test(clean);
}

function scheduleConfigurationRelease(owner, generation) {
  setTimeout(() => {
    const lease = leases.get(owner);
    if (!lease || lease.mode !== "human" || lease.generation !== generation || !lease.configuration_pending) return;
    if (!lease.configuration_idle_prompt) return;
    const remaining = configurationIdleGraceMs - (Date.now() - lease.last_output_at);
    if (remaining > 0) {
      setTimeout(() => scheduleConfigurationRelease(owner, generation), remaining);
      return;
    }
    dropLease(owner);
  }, configurationIdleGraceMs);
}

function observeConfigurationOutput(text) {
  for (const lease of leases.values()) {
    if (lease.mode !== "human" || !lease.configuration_pending) continue;
    lease.configuration_output = (lease.configuration_output + String(text)).slice(-8192);
    lease.last_output_at = Date.now();
    lease.expires_at = lease.last_output_at + humanConfigurationFallbackMs;
    if (hasIdleComposer(lease.configuration_output)) {
      lease.configuration_idle_prompt = true;
      scheduleConfigurationRelease(lease.owner, lease.generation);
    }
  }
}

function recordHumanInput(owner, data) {
  const lease = leases.get(owner);
  if (!lease || lease.mode !== "human") return;
  const now = Date.now();
  lease.last_actual_input_at = now;
  lease.reason = "human_input";
  lease.expires_at = now + (lease.composition_active ? humanActiveLeaseMs : humanIdleLeaseMs);
  let submitted = false;
  let visible = false;
  for (let index = 0; index < data.length; index += 1) {
    const char = data[index];
    if (char === "\x1b") {
      while (index + 1 < data.length) {
        index += 1;
        if (/[A-Za-z~]/.test(data[index])) break;
      }
      continue;
    }
    if (char === "\r" || char === "\n") {
      submitted = true;
      continue;
    }
    if (char === "\b" || char === "\x7f") {
      lease.composer = lease.composer.slice(0, -1);
      visible = true;
      continue;
    }
    if (char >= " ") {
      lease.composer = (lease.composer + char).slice(-maxHumanComposerChars);
      visible = true;
    }
  }
  if (submitted) {
    const command = lease.composer.trim().toLowerCase();
    const configurationCommand = /^\/(?:model|reasoning(?:_effort)?)(?:\s|$)/.test(command);
    if (!lease.configuration_pending) lease.configuration_pending = configurationCommand;
    if (lease.configuration_pending) {
      lease.configuration_output = "";
      lease.configuration_idle_prompt = false;
      lease.last_output_at = now;
      lease.expires_at = now + humanConfigurationFallbackMs;
      lease.reason = "configuration_command_submitted";
    } else {
      lease.reason = "human_command_submitted";
      lease.command_pending = true;
    }
    lease.composer = "";
    lease.composition_active = false;
    return;
  }
  if (visible && !lease.configuration_pending) {
    lease.composition_active = lease.composer.length > 0;
    lease.expires_at = now + (lease.composition_active ? humanActiveLeaseMs : humanIdleLeaseMs);
  }
  if (lease.configuration_pending) lease.expires_at = now + humanConfigurationFallbackMs;
}

function acquireLease(owner, mode, ttl, ownerPid = 0) {
  const current = leaseSnapshot();
  if (current.active && current.owner !== owner) throw new Error("terminal input is busy; attached viewer is watch-only");
  const existing = leases.get(owner);
  if (existing && existing.owner === owner) {
    existing.mode = mode;
    existing.owner_pid = ownerPid || existing.owner_pid;
    existing.expires_at = Date.now() + (mode === "human" ? humanIdleLeaseMs : ttl);
    return existing;
  }
  return createLease(owner, mode, ttl, ownerPid);
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
    acquireLease(requested, request.mode === "human" ? "human" : "automation", 900000, validOwnerPid(request.owner_pid));
    implicitWriter = requested;
  } else if (current.owner !== requested) {
    throw new Error("terminal input is busy");
  } else {
    const lease = leases.get(requested);
    if (lease && !leaseGenerationMatches(lease, request)) throw new Error("terminal input lease generation is stale");
  }
  return requested;
}

function handle(socket, request) {
  if (!request || typeof request !== "object") throw new Error("request must be an object");
  switch (request.op) {
    case "status":
      {
        const viewer = viewerSnapshot();
        return response(socket, {
          ok: true,
          state: {
            ...state,
            input_lease: leaseSnapshot(),
            viewer,
            viewer_attached: Boolean(viewer.attached),
            viewer_pid: viewer.pid || null,
            viewer_epoch: viewer.viewer_epoch || "",
          },
        });
      }
    case "read":
      return response(socket, { ok: true, output: tail(request.lines) });
    case "read_since":
      return response(socket, { ok: true, ...liveOutput(request.since, request.max_bytes ?? 65536, request.offset ?? 0) });
    case "acquire_input_lease": {
      const owner = validOwner(request.owner);
      const mode = request.mode;
      const ttl = Number(request.ttl_ms);
      if (!["automation", "human"].includes(mode) || !Number.isInteger(ttl) || ttl < 100 || ttl > 3600000) throw new Error("invalid input lease");
      acquireLease(owner, mode, ttl, validOwnerPid(request.owner_pid));
      return response(socket, { ok: true, lease_acquired: true, input_lease: leaseSnapshot() });
    }
    case "renew_input_lease": {
      const owner = validOwner(request.owner);
      const ttl = Number(request.ttl_ms);
      const current = leaseSnapshot();
      const lease = leases.get(owner);
      if (!Number.isInteger(ttl) || ttl < 100 || ttl > 3600000 || !current.active || current.owner !== owner || !lease || !leaseGenerationMatches(lease, request)) throw new Error("input lease is not owned by this client");
      lease.expires_at = Date.now() + ttl;
      return response(socket, { ok: true, input_lease: leaseSnapshot() });
    }
    case "release_input_lease": {
      const owner = validOwner(request.owner);
      const current = leaseSnapshot();
      const lease = leases.get(owner);
      if (current.active && current.owner !== owner) throw new Error("input lease is owned by another client");
      if (lease && !leaseGenerationMatches(lease, request)) throw new Error("input lease generation is stale");
      dropLease(owner);
      return response(socket, { ok: true, input_lease: leaseSnapshot() });
    }
    case "register_viewer": {
      const owner = validOwner(request.owner);
      const pid = validOwnerPid(request.viewer_pid);
      if (!pid) throw new Error("viewer process is required");
      const viewerEpoch = validViewerEpoch(request.viewer_epoch);
      const processStartIdentity = typeof request.process_start_identity === "string"
        && /^[A-Za-z0-9_.:-]{0,160}$/.test(request.process_start_identity)
        ? request.process_start_identity
        : "";
      for (const [currentOwner, current] of viewers) {
        if (currentOwner !== owner && processAlive(current.pid)) {
          throw new Error("terminal viewer is already attached");
        }
        viewers.delete(currentOwner);
      }
      viewers.set(owner, {
        owner,
        pid,
        process_start_identity: processStartIdentity,
        viewer_epoch: viewerEpoch,
        attached_at: Date.now(),
      });
      return response(socket, { ok: true, viewer: viewerSnapshot() });
    }
    case "unregister_viewer": {
      const owner = validOwner(request.owner);
      const current = viewers.get(owner);
      if (current && request.viewer_epoch && current.viewer_epoch !== validViewerEpoch(request.viewer_epoch)) {
        throw new Error("viewer epoch is stale");
      }
      viewers.delete(owner);
      return response(socket, { ok: true, viewer: viewerSnapshot() });
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
      const writer = writerFor(request, { autoLease: Boolean(request.auto_lease) });
      if (typeof data !== "string" || !data || Buffer.byteLength(data, "utf8") > maxInputBytes || data.includes("\u0000")) throw new Error("raw input is invalid or too large");
      if (request.mode === "human" || leases.get(writer)?.mode === "human") recordHumanInput(writer, data);
      terminal.write(data);
      state.last_activity = new Date().toISOString();
      return response(socket, { ok: true, bytes: Buffer.byteLength(data, "utf8"), generation: leases.get(writer)?.generation || 0, lease_acquired: Boolean(request.auto_lease) });
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
  socket.setEncoding("utf8");
  socket.on("data", (chunk) => {
    pending += chunk;
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
