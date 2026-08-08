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
const maxOutputChars = 250000;
const state = {
  session_id: sessionId,
  pid: null,
  alive: true,
  started_at: new Date().toISOString(),
  last_activity: new Date().toISOString(),
  output_chars: 0,
  exit_code: null,
  update_skipped: false,
};
let updatePromptHandled = false;

function appendOutput(data) {
  const text = String(data);
  output.push(text);
  state.output_chars += text.length;
  state.last_activity = new Date().toISOString();
  while (output.join("").length > maxOutputChars) output.shift();
  const buffered = output.join("");
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
  const text = output.join("");
  return text.split(/\r?\n/).slice(-Math.max(1, Math.min(Number(lines) || 80, 1000))).join("\n");
}

function handle(socket, request) {
  if (!request || typeof request !== "object") throw new Error("request must be an object");
  switch (request.op) {
    case "status":
      return response(socket, { ok: true, state });
    case "read":
      return response(socket, { ok: true, output: tail(request.lines) });
    case "send_text":
      if (typeof request.message !== "string" || request.message.length === 0 || request.message.length > 500) throw new Error("message must be non-empty text of at most 500 characters");
      if (/[\r\n]/.test(request.message)) throw new Error("message must be a single physical line");
      if (request.message.includes("\u0000")) throw new Error("message contains NUL");
      terminal.write(request.message);
      state.last_activity = new Date().toISOString();
      return response(socket, { ok: true });
    case "submit":
      terminal.write("\r");
      state.last_activity = new Date().toISOString();
      return response(socket, { ok: true });
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
