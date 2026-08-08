from __future__ import annotations

from collections import deque
import atexit
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable

from .config import AgentConfig, OrchestratorConfig
from .process import CommandResult, _prepare_command, codex_environment
from .report import atomic_write_json


class AppServerError(RuntimeError):
    """Raised when the local Codex App Server cannot complete a safe request."""


def _report_object(message: str) -> dict[str, Any] | None:
    candidates = [message.strip()]
    if "```" in message:
        candidates.extend(
            part.strip()
            for part in message.split("```")
            if part.strip() and not part.strip().startswith("json")
        )
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalise_report(message: str) -> str:
    """Keep App Server reports compatible with the existing delegation schema."""
    value = _report_object(message)
    if value is None:
        return message
    required = ("summary", "files_changed", "commands_run", "tests", "remaining_issues")
    if all(name in value for name in required) and isinstance(value.get("tests"), list):
        return json.dumps({name: value[name] for name in required}, ensure_ascii=False)
    tests = value.get("tests")
    if not (
        isinstance(tests, dict)
        or "status" in value
        or "commit_created" in value
        or "dependencies_added" in value
    ):
        return message
    normalised_tests: list[dict[str, str]] = []
    if isinstance(tests, dict):
        command = str(tests.get("command", ""))
        result = str(tests.get("result", "")).casefold()
        status = "passed" if result == "passed" else "failed"
        details = f"result={result or 'unknown'}; exit_code={tests.get('exit_code', 'unknown')}; tests_run={tests.get('tests_run', 'unknown')}"
        normalised_tests.append({"command": command, "status": status, "details": details})
    elif isinstance(tests, list):
        for item in tests:
            if isinstance(item, dict):
                normalised_tests.append(
                    {
                        "command": str(item.get("command", "")),
                        "status": str(item.get("status", "not_run")),
                        "details": str(item.get("details", "")),
                    }
                )
    normalised = {
        "summary": str(value.get("summary") or "Executor completed."),
        "files_changed": value.get("files_changed") if isinstance(value.get("files_changed"), list) else [],
        "commands_run": value.get("commands_run") if isinstance(value.get("commands_run"), list) else [],
        "tests": normalised_tests,
        "remaining_issues": value.get("remaining_issues") if isinstance(value.get("remaining_issues"), list) else [],
    }
    return json.dumps(normalised, ensure_ascii=False)


def _json_error(message: str) -> str:
    return message.replace("\r", " ").replace("\n", " ")[:500]


def _error_message(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        return _json_error(str(error.get("message") or error))
    return _json_error(str(error or "App Server request failed."))


_AUTH_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\r\n\s\"']*auth\.json")
_SECRET = re.compile(
    r"(?ix)(authorization\s*:\s*bearer\s+|\b(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\"?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"
)


def _sanitize_stderr(value: str) -> str:
    value = _AUTH_PATH.sub("[REDACTED_AUTH_PATH]", str(value))
    return _SECRET.sub(
        lambda match: f"{match.group(0).split(':', 1)[0].split('=', 1)[0]}=[REDACTED]",
        value,
    )[-8000:]


class _AppServerProcess:
    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        agent: AgentConfig,
        repository: Path,
        progress: Callable[[str], None] | None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.progress = progress
        self._lock = threading.RLock()
        self._next_id = 0
        self._messages: queue.Queue[str | None] = queue.Queue()
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr: deque[str] = deque(maxlen=80)
        self._events: deque[dict[str, Any]] = deque(maxlen=2000)
        self._closed = False
        command = [config.codex_command, "app-server", "--stdio"]
        process_args, use_shell = _prepare_command([str(item) for item in command])
        env = codex_environment(agent)
        self.process = subprocess.Popen(
            process_args,
            cwd=repository,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=use_shell,
        )
        threading.Thread(target=self._read_stdout, name="dual-codex-app-server", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="dual-codex-app-server-stderr", daemon=True).start()
        try:
            response = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "dual-codex",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=config.app_server_initialize_timeout,
            )
            if "error" in response:
                raise AppServerError(f"App Server initialize failed: {_error_message(response)}")
            self.notify("initialized")
        except Exception:
            self.close()
            raise

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-8000:]

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._messages.put(line)
        self._messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self._stderr.append(line)

    def _send(self, message: dict[str, Any]) -> None:
        if self._closed or self.process.poll() is not None:
            raise AppServerError("App Server process is not running.")
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError("App Server stdin closed unexpectedly.") from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        # Never grant an approval or permission implicitly. Normal workspace-write
        # turns use approvalPolicy=never; an unexpected request is a hard denial.
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result: Any = {"decision": "decline"}
        elif method in {"applyPatchApproval", "execCommandApproval"}:
            result = {"decision": {"denied": {"rejection": "Dual Codex does not auto-approve requests."}}}
        else:
            result = None
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        if result is None:
            response["error"] = {"code": -32000, "message": "Unsupported server request; denied by Dual Codex."}
        else:
            response["result"] = result
        self._send(response)

    def _next_message(self, timeout: float) -> dict[str, Any]:
        try:
            line = self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise AppServerError("Timed out waiting for App Server JSON-RPC data.") from exc
        if line is None:
            raise AppServerError("App Server process exited unexpectedly.")
        try:
            message = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise AppServerError("App Server emitted invalid JSON-RPC data.") from exc
        if not isinstance(message, dict):
            raise AppServerError("App Server emitted a non-object JSON-RPC message.")
        if "method" in message and "id" in message:
            self._respond_to_server_request(message)
            return self._next_message(timeout)
        return message

    def _record_notification(self, message: dict[str, Any]) -> None:
        if "method" in message:
            self._events.append(message)

    def request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError(f"Timed out waiting for App Server response to {method}.")
                try:
                    message = self._next_message(min(1.0, remaining))
                except AppServerError as exc:
                    if str(exc).startswith("Timed out waiting for App Server JSON-RPC data"):
                        continue
                    raise
                if message.get("id") == request_id:
                    return message
                self._record_notification(message)
                self._pending.append(message)

    def _take_pending(self) -> dict[str, Any] | None:
        if self._pending:
            return self._pending.popleft()
        return None

    def _read_event(self, timeout: float) -> dict[str, Any]:
        pending = self._take_pending()
        if pending is not None:
            return pending
        return self._next_message(timeout)

    def _thread_id_for_unlocked(self, repository: Path) -> tuple[str, bool]:
        params: dict[str, Any] = {
            "cwd": str(repository),
            "sandbox": self.agent.sandbox,
            "approvalPolicy": "never" if self.agent.sandbox == "workspace-write" else "on-request",
        }
        if self.agent.model:
            params["model"] = self.agent.model
        stored = _load_thread_mapping(self.config, self.agent, repository)
        if stored:
            response = self.request(
                "thread/resume",
                {"threadId": stored, **params},
                timeout=self.config.app_server_thread_timeout,
            )
            if "error" not in response:
                return stored, True
            if not _is_stale_thread_error(response):
                raise AppServerError(f"App Server thread/resume failed: {_error_message(response)}")
        response = self.request("thread/start", params, timeout=self.config.app_server_thread_timeout)
        if "error" in response:
            raise AppServerError(f"App Server thread/start failed: {_error_message(response)}")
        thread = response.get("result", {}).get("thread", {})
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("App Server thread/start returned no thread ID.")
        _save_thread_mapping(self.config, self.agent, repository, thread_id)
        return thread_id, False

    def thread_id_for(self, repository: Path) -> tuple[str, bool]:
        with self._lock:
            return self._thread_id_for_unlocked(repository)

    def _turn_unlocked(self, thread_id: str, prompt: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "approvalPolicy": "never" if self.agent.sandbox == "workspace-write" else "on-request",
        }
        if self.agent.model:
            params["model"] = self.agent.model
        if self.agent.reasoning_effort:
            params["effort"] = self.agent.reasoning_effort
        response = self.request("turn/start", params, timeout=self.config.app_server_turn_start_timeout)
        if "error" in response:
            raise AppServerError(f"App Server turn/start failed: {_error_message(response)}")
        turn = response.get("result", {}).get("turn", {})
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("App Server turn/start returned no turn ID.")

        started = False
        completed: dict[str, Any] | None = None
        deadline = time.monotonic() + self.config.app_server_turn_timeout
        last_progress = time.monotonic()
        while completed is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(f"Timed out waiting for turn/completed ({turn_id}).")
            try:
                message = self._read_event(min(1.0, remaining))
            except AppServerError as exc:
                if str(exc).startswith("Timed out waiting for App Server JSON-RPC data"):
                    continue
                raise
            if "id" in message and "method" not in message:
                self._pending.append(message)
                continue
            self._record_notification(message)
            method = message.get("method")
            event_params = message.get("params") or {}
            event_turn = event_params.get("turn") if isinstance(event_params, dict) else None
            event_turn_id = event_turn.get("id") if isinstance(event_turn, dict) else None
            if method == "turn/started" and event_turn_id == turn_id:
                started = True
            elif method == "turn/completed" and event_turn_id == turn_id:
                completed = event_turn
            elif method == "error":
                raise AppServerError("App Server emitted an error notification.")
            if self.progress and time.monotonic() - last_progress >= 15:
                self.progress(f"app-server turn {turn_id} still running")
                last_progress = time.monotonic()
        if not started:
            raise AppServerError(f"App Server completed turn {turn_id} without turn/started.")
        if completed.get("status") != "completed":
            raise AppServerError(f"App Server turn {turn_id} ended with status {completed.get('status')!r}.")
        items = completed.get("items") or []
        messages = [item.get("text", "") for item in items if isinstance(item, dict) and item.get("type") == "agentMessage"]
        assistant = str(messages[-1]) if messages else ""
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "assistant": assistant,
            "event_count": len(self._events),
            "turn_started": started,
            "turn_status": completed.get("status"),
        }

    def turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        with self._lock:
            return self._turn_unlocked(thread_id, prompt)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[tuple[str, str, str], _AppServerProcess] = {}


def _process_key(agent: AgentConfig, config: OrchestratorConfig) -> tuple[str, str, str]:
    return (
        agent.account_name,
        str(agent.codex_home.expanduser().resolve()),
        str(Path(config.codex_command).resolve()),
    )


def _get_process(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    progress: Callable[[str], None] | None,
) -> _AppServerProcess:
    key = _process_key(agent, config)
    with _PROCESS_LOCK:
        process = _PROCESSES.get(key)
        if process is not None and process.process.poll() is None:
            process.progress = progress
            return process
        if process is not None:
            process.close()
        process = _AppServerProcess(config=config, agent=agent, repository=repository, progress=progress)
        _PROCESSES[key] = process
        return process


def _close_processes() -> None:
    with _PROCESS_LOCK:
        processes = list(_PROCESSES.values())
        _PROCESSES.clear()
    for process in processes:
        process.close()


def _discard_process(process: _AppServerProcess) -> None:
    key = _process_key(process.agent, process.config)
    with _PROCESS_LOCK:
        if _PROCESSES.get(key) is process:
            _PROCESSES.pop(key, None)
    process.close()


atexit.register(_close_processes)


def _mapping_path(config: OrchestratorConfig, agent: AgentConfig, repository: Path) -> Path:
    import hashlib

    key = f"{agent.account_name}|{agent.codex_home.resolve()}|{repository.resolve()}".encode("utf-8")
    return config.runs_dir / "app-server-sessions" / (hashlib.sha256(key).hexdigest() + ".json")


def _load_thread_mapping(config: OrchestratorConfig, agent: AgentConfig, repository: Path) -> str | None:
    path = _mapping_path(config, agent, repository)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        value.get("repository") != str(repository.resolve())
        or value.get("account") != agent.account_name
        or value.get("codex_home") != str(agent.codex_home.resolve())
    ):
        return None
    thread_id = value.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _save_thread_mapping(config: OrchestratorConfig, agent: AgentConfig, repository: Path, thread_id: str) -> None:
    path = _mapping_path(config, agent, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "account": agent.account_name,
            "codex_home": str(agent.codex_home.resolve()),
            "repository": str(repository.resolve()),
            "thread_id": thread_id,
        },
    )


def _is_stale_thread_error(response: dict[str, Any]) -> bool:
    message = _error_message(response).casefold()
    return any(marker in message for marker in ("not found", "unknown thread", "no such thread", "does not exist"))


def run_codex_app_server(
    *,
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    session_id: str,
    task_artifact_path: Path | None = None,
    task_sha256: str = "",
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    command = [config.codex_command, "app-server", "--stdio"]
    metadata: dict[str, str] = {
        "app_server_session_id": session_id,
        "task_transport": "app_server",
        "task_artifact": str(task_artifact_path.resolve()) if task_artifact_path else "",
        "task_sha256": task_sha256,
    }
    process: _AppServerProcess | None = None
    try:
        process = _get_process(config, agent, repository, progress)
        thread_id, resumed = process.thread_id_for(repository)
        turn = process.turn(thread_id, prompt)
        assistant = turn["assistant"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_normalise_report(assistant), encoding="utf-8")
        metadata.update(
            {
                "app_server_thread_id": thread_id,
                "app_server_turn_id": turn["turn_id"],
                "app_server_thread_resumed": str(resumed).lower(),
                "app_server_process_id": str(process.pid),
                "app_server_event_count": str(turn["event_count"]),
            }
        )
        return CommandResult(command, 0, assistant, _sanitize_stderr(process.stderr_tail), metadata)
    except (AppServerError, OSError, ValueError) as exc:
        if process is not None:
            _discard_process(process)
        return CommandResult(command, 1, "", _sanitize_stderr(str(exc)), metadata)
