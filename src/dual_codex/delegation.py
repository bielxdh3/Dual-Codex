from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from .codex import run_codex_exec as _run_codex_exec_legacy
from .codex import run_codex_app_server
from .codex import run_codex_terminal
from .config import ConfigError, OrchestratorConfig
from .git import ensure_git_repository, head_revision, status_and_diff, status_porcelain
from .live_events import LiveEventJournal, repository_identity
from .paths import path_identity_key
from .process import CommandResult
from .registry import login_status
from .report import atomic_write_json, dump_json


def run_codex_exec(**kwargs):
    """Select the configured structured backend while preserving the TUI seam."""
    config = kwargs.get("config")
    agent = kwargs.get("agent")
    if kwargs.get("reuse_existing") and agent is not None and agent.backend != "windows":
        raise DelegationError("Strict reuse-existing requires a registered native Windows Executor TUI.")
    if agent is not None and agent.backend == "app_server":
        app_server_kwargs = dict(kwargs)
        app_server_kwargs.pop("schema_path", None)
        app_server_kwargs.pop("check", None)
        app_server_kwargs.pop("reuse_existing", None)
        from .terminal import session_id_for

        app_server_kwargs["session_id"] = session_id_for(
            agent.account_name,
            kwargs["repository"],
        )
        app_server_kwargs["request_id"] = kwargs.get("request_id", "")
        app_server_kwargs["run_id"] = kwargs.get("run_id", app_server_kwargs["session_id"])
        app_server_kwargs["role"] = kwargs.get("role", "executor")
        return run_codex_app_server(**app_server_kwargs)
    command_name = Path(config.codex_command).stem.casefold() if config else "codex"
    if kwargs.get("reuse_existing") and not command_name.startswith("codex"):
        raise DelegationError("Strict reuse-existing refuses non-Codex or non-native fallback backends.")
    if config is not None and not command_name.startswith("codex"):
        return _run_codex_exec_legacy(
            codex_command=config.codex_command,
            agent=kwargs["agent"],
            repository=kwargs["repository"],
            prompt=kwargs["prompt"],
            output_path=kwargs["output_path"],
            schema_path=kwargs["schema_path"],
            check=False,
            progress=kwargs.get("progress"),
        )
    terminal_kwargs = dict(kwargs)
    terminal_kwargs.pop("schema_path", None)
    terminal_kwargs.pop("check", None)
    terminal_kwargs.pop("request_id", None)
    terminal_kwargs.pop("run_id", None)
    terminal_kwargs.pop("role", None)
    from .terminal import session_id_for

    terminal_kwargs["session_id"] = session_id_for(
        kwargs["agent"].account_name,
        kwargs["repository"],
    )
    terminal_kwargs["reuse_existing"] = bool(kwargs.get("reuse_existing", False))
    return run_codex_terminal(**terminal_kwargs)


REQUEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET = re.compile(
    r"""(?ix)(
        (?:authorization\s*:\s*bearer\s+)
        |(?:\b(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\"?\s*[:=]\s*)
    )(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"""
)
_AUTH_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\r\n\s\"']*auth\.json")
TASK_CONTROL_MESSAGE_MAX = 500


class DelegationError(RuntimeError):
    """Raised for a delegation that cannot be safely started."""


class InvalidRequestError(DelegationError):
    pass


@dataclass(frozen=True)
class DelegationRequest:
    schema_version: int
    request_id: str
    action: str
    repository: Path
    task: str
    constraints: tuple[str, ...]
    context_files: tuple[str, ...]
    review_findings: tuple[dict[str, Any], ...]
    max_correction_cycles: int
    parent_request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "action": self.action,
            "repository": str(self.repository),
            "task": self.task,
            "constraints": list(self.constraints),
            "context_files": list(self.context_files),
            "review_findings": [dict(item) for item in self.review_findings],
            "max_correction_cycles": self.max_correction_cycles,
            **({"parent_request_id": self.parent_request_id} if self.parent_request_id else {}),
        }


@dataclass(frozen=True)
class DelegationOutcome:
    status: str
    request_id: str
    result_file: Path
    run_directory: Path | None
    elapsed_seconds: float


def sanitize_text(value: str) -> str:
    value = _AUTH_PATH.sub("[REDACTED_AUTH_PATH]", str(value))
    return _SECRET.sub(lambda match: f"{match.group(0).split(':', 1)[0].split('=', 1)[0]}=[REDACTED]", value)


def sanitize_value(value: Any, *, key: str = "") -> Any:
    lowered = key.casefold()
    if any(
        marker in lowered
        for marker in ("token", "secret", "password", "credential", "api_key", "api-key", "authorization")
    ):
        return "[REDACTED]"
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {str(name): sanitize_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value


def _safe_diff(value: str) -> str:
    lines: list[str] = []
    redact_section = False
    for line in value.splitlines(keepends=True):
        if line.startswith("diff --git "):
            redact_section = any(
                marker in line.casefold()
                for marker in ("auth.json", ".env", "credentials", "secret")
            )
            if redact_section:
                lines.append("diff section redacted by Dual Codex\n")
                continue
        if not redact_section:
            lines.append(line)
    return sanitize_text("".join(lines))


def _required_string(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"Request field '{name}' must be a non-empty string.")
    return value.strip()


def _string_list(raw: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidRequestError(f"Request field '{name}' must be an array of strings.")
    return tuple(item.strip() for item in value if item.strip())


def _request_id(raw: Mapping[str, Any]) -> str:
    value = _required_string(raw, "request_id")
    if not _REQUEST_ID.fullmatch(value):
        raise InvalidRequestError(
            "Request field 'request_id' must contain only letters, numbers, '.', '_' or '-'."
        )
    return value


def _repository(value: str, config: OrchestratorConfig) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else config.config_path.parent / path).resolve()


def parse_request(
    raw: Any,
    config: OrchestratorConfig,
    *,
    repository_override: str | None = None,
) -> DelegationRequest:
    if not isinstance(raw, dict):
        raise InvalidRequestError("Delegation request must be a JSON object.")
    allowed = {
        "schema_version",
        "request_id",
        "action",
        "repository",
        "task",
        "constraints",
        "context_files",
        "review_findings",
        "max_correction_cycles",
        "parent_request_id",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise InvalidRequestError(f"Unknown request field(s): {', '.join(unknown)}.")
    version = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != REQUEST_SCHEMA_VERSION:
        raise InvalidRequestError(
            f"Unsupported request schema_version {version!r}; expected {REQUEST_SCHEMA_VERSION}."
        )
    request_id = _request_id(raw)
    action = _required_string(raw, "action")
    if action not in {"implement", "correct"}:
        raise InvalidRequestError(
            f"Unknown delegation action '{action}'; supported actions: implement, correct."
        )
    repository_value = repository_override or raw.get("repository")
    if not isinstance(repository_value, str) or not repository_value.strip():
        raise InvalidRequestError(
            "A target repository is required in the request or through --repository."
        )
    task = _required_string(raw, "task")
    constraints = _string_list(raw, "constraints")
    context_files = _string_list(raw, "context_files")
    findings_raw = raw.get("review_findings", [])
    if not isinstance(findings_raw, list) or any(not isinstance(item, dict) for item in findings_raw):
        raise InvalidRequestError("Request field 'review_findings' must be an array of objects.")
    findings: list[dict[str, Any]] = []
    for finding in findings_raw:
        title = finding.get("title")
        details = finding.get("details")
        if not isinstance(title, str) or not title.strip() or not isinstance(details, str) or not details.strip():
            raise InvalidRequestError(
                "Each review finding must contain non-empty string fields 'title' and 'details'."
            )
        severity = finding.get("severity", "important")
        if severity not in {"blocking", "important", "optional"}:
            raise InvalidRequestError("Review finding severity must be blocking, important, or optional.")
        findings.append(dict(finding))
    max_cycles = raw.get("max_correction_cycles", config.max_correction_cycles)
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 0:
        raise InvalidRequestError("Request field 'max_correction_cycles' must be a non-negative integer.")
    if max_cycles > config.max_correction_cycles:
        raise InvalidRequestError(
            "Request max_correction_cycles exceeds the configured maximum "
            f"({config.max_correction_cycles})."
        )
    parent_request_id = raw.get("parent_request_id")
    if parent_request_id is not None:
        if not isinstance(parent_request_id, str) or not _REQUEST_ID.fullmatch(parent_request_id.strip()):
            raise InvalidRequestError("Request field 'parent_request_id' is invalid.")
        parent_request_id = parent_request_id.strip()
    if action == "correct":
        if not parent_request_id:
            raise InvalidRequestError("Correct requests must link to a parent_request_id.")
        if not findings:
            raise InvalidRequestError("Correct requests require actionable review_findings.")
    return DelegationRequest(
        schema_version=version,
        request_id=request_id,
        action=action,
        repository=_repository(repository_value.strip(), config),
        task=task,
        constraints=constraints,
        context_files=context_files,
        review_findings=tuple(findings),
        max_correction_cycles=max_cycles,
        parent_request_id=parent_request_id,
    )


def load_request(
    config: OrchestratorConfig,
    *,
    request_file: Path | None = None,
    stdin_text: str | None = None,
    repository_override: str | None = None,
) -> DelegationRequest:
    if (request_file is None) == (stdin_text is None):
        raise InvalidRequestError("Provide exactly one of --request-file or --stdin.")
    try:
        text = request_file.read_text(encoding="utf-8-sig") if request_file else stdin_text or ""
        raw = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidRequestError(f"Could not read delegation request JSON: {exc}") from exc
    return parse_request(raw, config, repository_override=repository_override)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Query a Windows PID without terminating or otherwise affecting it."""

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # PROCESS_QUERY_LIMITED_INFORMATION is sufficient and avoids requiring
    # broad process rights for a lock held by another user.
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True

    error = ctypes.get_last_error()
    if error == 5:  # ERROR_ACCESS_DENIED: the process exists but is protected.
        return True
    if error in {6, 87, 1168}:  # invalid handle/parameter or process not found.
        return False
    # Unknown query failures are treated as live to preserve the lock safety
    # property; stale locks can still be recovered explicitly.
    return True


def _process_start_token(pid: int) -> str | None:
    """Return a process-creation token when the platform exposes one."""

    if pid <= 0:
        return None
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0400, False, pid)
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return str(value)
        finally:
            kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[-1].split()
        return fields[19]
    except (OSError, UnicodeError, IndexError):
        return None


def _safe_process_start_token(pid: int) -> str | None:
    try:
        return _process_start_token(pid)
    except Exception:
        return None


class RepositoryLock:
    """A conservative, repository-scoped lock for local executor runs."""

    def __init__(self, runs_dir: Path, repository: Path, request_id: str, run_id: str = "") -> None:
        digest = hashlib.sha256(path_identity_key(repository).encode("utf-8")).hexdigest()[:24]
        self.path = runs_dir / ".locks" / f"{digest}.json"
        self.repository = repository
        self.request_id = request_id
        self.run_id = run_id
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "repository_key": repository_identity(self.repository),
            "repository": str(self.repository),
            "pid": os.getpid(),
            "process_start": _safe_process_start_token(os.getpid()),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    os.write(descriptor, (json.dumps(payload) + "\n").encode("utf-8"))
                finally:
                    os.close(descriptor)
                self._held = True
                return
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                    pid = int(existing.get("pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise DelegationError(
                        f"Repository lock exists and cannot be inspected: {self.path}"
                    ) from exc
                if _pid_alive(pid):
                    raise DelegationError(
                        "Repository is already delegated; active request "
                        f"'{existing.get('request_id', 'unknown')}'."
                    )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise DelegationError(f"Could not recover stale repository lock: {self.path}") from exc
        raise DelegationError(f"Repository lock acquisition raced: {self.path}")

    def release(self) -> None:
        if not self._held:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if existing.get("request_id") == self.request_id and int(existing.get("pid", 0)) == os.getpid():
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        self._held = False

    def __enter__(self) -> "RepositoryLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _publish_run_event(
    journal: LiveEventJournal | None,
    *,
    method: str,
    state: str,
    detail: Mapping[str, Any],
    thread_id: str = "",
    turn_id: str = "",
) -> None:
    if journal is None:
        return
    try:
        journal.append(
            kind="run",
            state=state,
            method=method,
            detail=dict(detail),
            thread_id=thread_id,
            turn_id=turn_id,
        )
    except Exception:
        # A telemetry failure must not change delegation or repository safety.
        pass


def _request_id_hint(raw_text: str) -> str:
    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return "invalid-" + uuid4().hex[:12]
    value = raw.get("request_id") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else "invalid-" + uuid4().hex[:12]


def _result(
    *,
    request_id: str,
    status: str,
    started_at: str,
    finished_at: str,
    summary: str,
    executor_account: str = "",
    executor_label: str = "",
    executor_sandbox: str = "",
    exit_code: int | None = None,
    parent_request_id: str | None = None,
    files_changed: list[str] | None = None,
    commands_run: list[str] | None = None,
    tests: list[Any] | None = None,
    remaining_issues: list[str] | None = None,
    git_status: str = "",
    diff_file: str = "",
    run_directory: str = "",
    executor_report_file: str = "",
    stderr_file: str = "",
    terminal_session_id: str = "",
    terminal_turn_start: str = "",
    app_server_thread_id: str = "",
    app_server_turn_id: str = "",
    app_server_process_id: str = "",
    task_transport: str = "",
    task_artifact: str = "",
    task_sha256: str = "",
    reuse_existing: bool = False,
    reuse_provenance: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return sanitize_value(
        {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request_id,
            "parent_request_id": parent_request_id,
            "status": status,
            "executor_account": executor_account,
            "executor_label": executor_label,
            "executor_sandbox": executor_sandbox,
            "exit_code": exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "summary": summary,
            "files_changed": files_changed or [],
            "commands_run": commands_run or [],
            "tests": tests or [],
            "remaining_issues": remaining_issues or [],
            "git_status": git_status,
            "diff_file": diff_file,
            "run_directory": run_directory,
            "executor_report_file": executor_report_file,
            "stderr_file": stderr_file,
            "terminal_session_id": terminal_session_id,
            "terminal_turn_start": terminal_turn_start,
            "app_server_thread_id": app_server_thread_id,
            "app_server_turn_id": app_server_turn_id,
            "app_server_process_id": app_server_process_id,
            "task_transport": task_transport,
            "task_artifact": task_artifact,
            "task_sha256": task_sha256,
            "reuse_existing": reuse_existing,
            "reuse_provenance": dict(reuse_provenance or {"mode": "reuse_or_start"}),
            "error": error,
        }
    )


def _write_text(path: Path, value: str) -> None:
    path.write_text(sanitize_text(value), encoding="utf-8", newline="\n")


def _files_from_status(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if len(line) >= 4:
            files.append(line[3:].strip())
    return files


def _run_id(config: OrchestratorConfig, request: DelegationRequest) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = f"{stamp}-{request.request_id}"
    if (config.runs_dir / candidate).exists():
        candidate = f"{candidate}-{uuid4().hex[:8]}"
    return candidate


def _run_directory(config: OrchestratorConfig, request: DelegationRequest, run_id: str | None = None) -> Path:
    candidate = config.runs_dir / (run_id or _run_id(config, request))
    if candidate.exists():
        raise DelegationError(f"Run directory already exists: {candidate}")
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _write_task_artifact(
    config: OrchestratorConfig,
    run_dir: Path,
    request: DelegationRequest,
    diff: str = "",
) -> tuple[Path, str]:
    """Write the bulk task once in a dedicated, non-secret transport directory."""
    from .terminal import TerminalError, executor_task_artifact_dir

    try:
        artifact_dir = executor_task_artifact_dir(config, create=True)
    except TerminalError as exc:
        raise DelegationError(str(exc)) from exc
    artifact_path = artifact_dir / f"{run_dir.name}.md"
    task = sanitize_text(request.task)
    lines = [
        "# Dual Codex executor task artifact",
        "",
        f"Request ID: {request.request_id}",
        f"Action: {request.action}",
        f"Repository: {request.repository}",
        "",
        "## Task instructions",
        task,
    ]
    if request.constraints:
        lines.extend(["", "## Constraints", *[f"- {sanitize_text(item)}" for item in request.constraints]])
    if request.context_files:
        lines.extend(
            [
                "",
                "## Context files",
                *[f"- {sanitize_text(item)}" for item in request.context_files],
            ]
        )
    if request.action == "correct":
        lines.extend(
            [
                "",
                f"## Correction context (parent request: {request.parent_request_id})",
                "### Review findings",
                dump_json(sanitize_value({"findings": list(request.review_findings)})),
                "",
                "### Current Git status and diff",
                diff,
            ]
        )
    lines.extend(
        [
            "",
            "## Safety and response contract",
            "Do not commit, push, open a pull request, merge, publish, or release.",
            "Do not use WSL, credentials, auth.json, or dangerous sandbox bypasses.",
            "Return only the required structured JSON report without Markdown.",
        ]
    )
    content = "\n".join(lines).rstrip() + "\n"
    artifact_path.write_text(content, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    atomic_write_json(
        run_dir / "task-transport.json",
        {
            "request_id": request.request_id,
            "task_transport": "file",
            "task_artifact": str(artifact_path),
            "task_sha256": digest,
            "artifact_directory": str(artifact_dir),
        },
    )
    return artifact_path, digest


def _control_message(request: DelegationRequest, artifact_path: Path) -> str:
    message = (
        f'Read and execute the complete task instructions in "{artifact_path.resolve()}" '
        f"for request {request.request_id} in the current repository; do not commit; "
        "when finished, return only the required structured JSON report."
    )
    if "\r" in message or "\n" in message:
        raise DelegationError("File-backed control message must be a single physical line.")
    if request.task in message:
        raise DelegationError("File-backed control message unexpectedly contains the task body.")
    if len(message) > TASK_CONTROL_MESSAGE_MAX:
        raise DelegationError(
            f"File-backed control message exceeds the safe {TASK_CONTROL_MESSAGE_MAX}-character limit."
        )
    return message


def _prompt(request: DelegationRequest, diff: str = "") -> str:
    lines = [
        "You are the hidden Dual Codex executor. Implement the requested change in the current repository.",
        "The visible Codex App is the architect and reviewer; do not invoke or simulate architect/reviewer CLI accounts.",
        "Do not commit, push, open a pull request, merge, or release.",
        "Inspect the real repository before editing and run relevant validation.",
        "",
        f"ACTION: {request.action}",
        "TASK:",
        request.task,
    ]
    if request.constraints:
        lines.extend(["", "CONSTRAINTS:", *[f"- {item}" for item in request.constraints]])
    if request.context_files:
        lines.extend(["", "CONTEXT FILES (inspect only when relevant):", *[f"- {item}" for item in request.context_files]])
    if request.action == "correct":
        lines.extend(
            [
                "",
                f"PARENT REQUEST: {request.parent_request_id}",
                "REVIEW FINDINGS:",
                dump_json({"findings": list(request.review_findings)}),
                "",
                "CURRENT GIT STATUS AND DIFF:",
                diff,
            ]
        )
    lines.extend(
        [
            "",
            "Return only a JSON object, without Markdown, with these keys: summary (string), files_changed (array of strings), commands_run (array of strings), tests (array of objects with command/status/details), and remaining_issues (array of strings).",
        ]
    )
    return "\n".join(lines)


_REPORT_FIELDS = {"summary", "files_changed", "commands_run", "tests", "remaining_issues"}
_REPORT_TEST_STATUSES = {"passed", "failed", "not_run"}


def _validate_executor_report(value: Mapping[str, Any]) -> str:
    missing = sorted(_REPORT_FIELDS - set(value))
    extra = sorted(set(value) - _REPORT_FIELDS)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unknown fields: {', '.join(extra)}")
        return "; ".join(details)
    if not isinstance(value["summary"], str):
        return "summary must be a string"
    for field in ("files_changed", "commands_run", "remaining_issues"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            return f"{field} must be an array of strings"
    tests = value["tests"]
    if not isinstance(tests, list):
        return "tests must be an array"
    for index, test in enumerate(tests):
        if not isinstance(test, Mapping):
            return f"tests[{index}] must be an object"
        if set(test) != {"command", "status", "details"}:
            return f"tests[{index}] must contain only command, status and details"
        if not all(isinstance(test[field], str) for field in ("command", "status", "details")):
            return f"tests[{index}] fields must be strings"
        if test["status"] not in _REPORT_TEST_STATUSES:
            return f"tests[{index}] has unsupported status '{test['status']}'"
    return ""


def _read_report(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "Executor did not produce a structured report."
    try:
        raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return None, f"Executor report could not be read: {exc}"
    try:
        raw = json.loads(raw_text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _write_text(path.with_suffix(".invalid.log"), raw_text)
        return None, f"Executor report is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return None, "Executor report must be a JSON object."
    sanitized = sanitize_value(raw)
    validation_error = _validate_executor_report(sanitized)
    if validation_error:
        _write_text(path.with_suffix(".invalid.log"), raw_text)
        return None, f"Executor report schema validation failed: {validation_error}."
    atomic_write_json(path, sanitized)
    return sanitized, ""


def _report_list(report: Mapping[str, Any], name: str) -> list[Any]:
    value = report.get(name, [])
    return list(value) if isinstance(value, list) else []


_EXECUTOR_FAILURE_MARKERS = (
    "read-only",
    "blocked",
    "rejected",
    "not applied",
    "could not apply",
    "could not be applied",
    "unable to apply",
    "failed to apply",
    "permission denied by sandbox",
    "no required changes",
    "no changes required",
    "nothing to change",
    "already complete",
    "already implemented",
)


def _classify_executor_result(
    *,
    report: Mapping[str, Any] | None,
    report_error: str,
    changed: list[str],
    command_result: CommandResult,
) -> tuple[str, str, str]:
    """Classify execution by its repository effect and report semantics."""
    if command_result.returncode != 0:
        return (
            "failed",
            "Codex executor failed; repository modifications, if any, were preserved.",
            f"Executor exited with code {command_result.returncode}.",
        )
    if report is None:
        return (
            "failed",
            "Codex executor completed without a valid structured report.",
            report_error,
        )

    summary = str(report.get("summary", "Executor completed."))
    remaining_issues = _report_list(report, "remaining_issues")
    tests = _report_list(report, "tests")
    semantic_parts = [summary, *[str(item) for item in remaining_issues]]
    semantic_text = "\n".join(semantic_parts).casefold()
    if any(marker in semantic_text for marker in _EXECUTOR_FAILURE_MARKERS):
        return (
            "failed",
            summary,
            "Executor report indicates that the requested change was not applied.",
        )

    for test in tests:
        if not isinstance(test, Mapping):
            return "failed", summary, "Executor report contains an invalid test entry."
        test_status = str(test.get("status", "")).casefold()
        if test_status in {"failed", "not_run"}:
            return "failed", summary, f"Executor reported a test with status '{test_status}'."
        if test_status != "passed":
            return "failed", summary, f"Executor reported an unsupported test status '{test_status}'."

    if not changed:
        return (
            "failed",
            summary,
            "Executor produced a valid report but did not apply any repository changes.",
        )

    return "completed", summary, report_error


def _capture_git(repository: Path, run_dir: Path) -> tuple[str, str, list[str]]:
    status = status_porcelain(repository)
    diff = status_and_diff(repository)
    diff_path = run_dir / "git-diff.md"
    _write_text(diff_path, _safe_diff(diff))
    return status, str(diff_path), _files_from_status(status)


def _emit(output: Callable[[str], None], started: float, message: str) -> None:
    rendered = f"{message} (elapsed {time.monotonic() - started:.1f}s)"
    if output is print:
        print(rendered, flush=True)
    else:
        output(rendered)


def _failed_outcome(
    *,
    result_file: Path,
    request_id: str,
    started_at: str,
    started: float,
    status: str,
    summary: str,
    error: str,
    executor_account: str = "",
    executor_label: str = "",
    executor_sandbox: str = "",
    exit_code: int | None = None,
    parent_request_id: str | None = None,
    reuse_existing: bool = False,
) -> DelegationOutcome:
    finished_at = _timestamp()
    atomic_write_json(
        result_file,
        _result(
            request_id=request_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            summary=summary,
            executor_account=executor_account,
            executor_label=executor_label,
            executor_sandbox=executor_sandbox,
            exit_code=exit_code,
            parent_request_id=parent_request_id,
            reuse_existing=reuse_existing,
            error=error,
        ),
    )
    return DelegationOutcome(status, request_id, result_file, None, time.monotonic() - started)


def delegate(
    config: OrchestratorConfig,
    *,
    result_file: Path,
    request_file: Path | None = None,
    stdin_text: str | None = None,
    repository_override: str | None = None,
    allow_dirty: bool = False,
    reuse_existing: bool = False,
    output: Callable[[str], None] = print,
) -> DelegationOutcome:
    result_file = result_file.expanduser().resolve()
    started = time.monotonic()
    started_at = _timestamp()
    request_text = ""
    if request_file is not None:
        try:
            request_text = request_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            return _failed_outcome(
                result_file=result_file,
                request_id="invalid-" + uuid4().hex[:12],
                started_at=started_at,
                started=started,
                status="invalid_request",
                summary="Delegation request could not be read.",
                error=str(exc),
            )
    elif stdin_text is not None:
        request_text = stdin_text
    try:
        request = load_request(
            config,
            request_file=None if stdin_text is not None else request_file,
            stdin_text=stdin_text,
            repository_override=repository_override,
        )
    except Exception as exc:
        return _failed_outcome(
            result_file=result_file,
            request_id=_request_id_hint(request_text),
            started_at=started_at,
            started=started,
            status="invalid_request",
            summary="Delegation request validation failed.",
            error=sanitize_text(str(exc)),
        )

    request_id = request.request_id
    executor_account = ""
    executor_label = ""
    executor_sandbox = ""
    run_journal: LiveEventJournal | None = None
    run_id = ""
    try:
        _emit(output, started, "[1/5] Validating request")
        ensure_git_repository(request.repository)
        dirty = bool(status_porcelain(request.repository).strip())
        if dirty:
            if config.require_clean_git and not allow_dirty:
                raise DelegationError(
                    "Target repository has uncommitted changes. Commit/stash them or use "
                    "--allow-dirty / require_clean_git = false explicitly."
                )
            output("[warning] Target repository is dirty; continuing by explicit policy.")
        try:
            agent = config.agent_for_role("executor")
        except ConfigError as exc:
            return _failed_outcome(
                result_file=result_file,
                request_id=request_id,
                started_at=started_at,
                started=started,
                status="executor_unavailable",
                summary="The executor role is unassigned.",
                error=str(exc),
                parent_request_id=request.parent_request_id,
                reuse_existing=reuse_existing,
            )
        executor_account = agent.account_name
        executor_label = agent.label
        executor_sandbox = agent.sandbox
        _emit(output, started, f"[2/5] Resolving executor account: {executor_account}")
        if login_status(config, config.account_for_role("executor")) != "OK":
            return _failed_outcome(
                result_file=result_file,
                request_id=request_id,
                started_at=started_at,
                started=started,
                status="executor_unavailable",
                summary="The executor role is not logged in or cannot be reached.",
                error=f"Codex login status failed for account '{executor_account}'.",
                executor_account=executor_account,
                executor_label=executor_label,
                executor_sandbox=executor_sandbox,
                parent_request_id=request.parent_request_id,
                reuse_existing=reuse_existing,
            )
        output(f"Target repository: {request.repository}")
        run_id = _run_id(config, request)
        with RepositoryLock(config.runs_dir, request.repository, request.request_id, run_id):
            run_dir = _run_directory(config, request, run_id)
            try:
                run_journal = LiveEventJournal(
                    config.runs_dir,
                    account=executor_account,
                    role="executor",
                    repository=request.repository,
                    run_id=run_id,
                    request_id=request.request_id,
                    max_records=config.live_event_journal_max_records,
                    max_record_bytes=config.live_event_journal_max_record_bytes,
                    max_detail_bytes=config.live_event_journal_max_detail_bytes,
                )
            except (OSError, ValueError):
                # Observability is best-effort; delegation and its safety gates remain authoritative.
                run_journal = None
            _publish_run_event(
                run_journal,
                method="run/started",
                state="started",
                detail={"request_id": request.request_id, "started_at": started_at},
            )
            atomic_write_json(run_dir / "request.json", sanitize_value(request.as_dict()))
            initial_diff = _safe_diff(status_and_diff(request.repository)) if request.action == "correct" else ""
            initial_head = head_revision(request.repository)
            task_artifact, task_sha256 = _write_task_artifact(config, run_dir, request, initial_diff)
            control_message = _control_message(request, task_artifact)
            executor_prompt = control_message
            if agent.backend == "app_server":
                executor_prompt = task_artifact.read_text(encoding="utf-8")
            output(f"Run directory: {run_dir}")
            _emit(
                output,
                started,
                f"[3/5] Starting executor: {executor_account} (sandbox={executor_sandbox})",
            )
            report_path = run_dir / "executor-report.json"
            command_result: CommandResult = run_codex_exec(
                config=config,
                agent=agent,
                repository=request.repository,
                prompt=executor_prompt,
                output_path=report_path,
                schema_path=config.project_root / "schemas" / "delegation-report.schema.json",
                check=False,
                task_artifact_path=task_artifact,
                task_sha256=task_sha256,
                request_id=request.request_id,
                run_id=run_dir.name,
                role="executor",
                progress=lambda message: _emit(output, started, f"[3/5] {message}"),
                reuse_existing=reuse_existing,
            )
            stdout_path = run_dir / "executor.stdout.log"
            stderr_path = run_dir / "executor.stderr.log"
            _write_text(stdout_path, command_result.stdout)
            _write_text(stderr_path, command_result.stderr)
            report, report_error = _read_report(report_path)
            _emit(output, started, "[4/5] Capturing diff and validation results")
            try:
                git_status, diff_file, changed = _capture_git(request.repository, run_dir)
                head_changed = bool(initial_head and head_revision(request.repository) != initial_head)
            except Exception as exc:
                git_status, diff_file, changed = "unavailable", "", []
                head_changed = False
                report_error = f"{report_error} Git capture failed: {exc}".strip()
            status, summary, error = _classify_executor_result(
                report=report,
                report_error=report_error,
                changed=changed,
                command_result=command_result,
            )
            if head_changed:
                status = "failed"
                error = (
                    f"{error} Executor changed Git HEAD; no rollback was attempted."
                ).strip()
            remaining_issues = _report_list(report or {}, "remaining_issues")
            if head_changed:
                remaining_issues.append("Git HEAD changed during delegation; inspect the commit manually.")
            finished_at = _timestamp()
            result = _result(
                request_id=request.request_id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                summary=summary,
                executor_account=executor_account,
                executor_label=executor_label,
                executor_sandbox=executor_sandbox,
                exit_code=command_result.returncode,
                parent_request_id=request.parent_request_id,
                files_changed=_report_list(report or {}, "files_changed") or changed,
                commands_run=_report_list(report or {}, "commands_run"),
                tests=_report_list(report or {}, "tests"),
                remaining_issues=remaining_issues,
                git_status=git_status,
                diff_file=diff_file,
                run_directory=str(run_dir),
                executor_report_file=str(report_path) if report_path.exists() else "",
                stderr_file=str(stderr_path),
                terminal_session_id=command_result.metadata.get("terminal_session_id", ""),
                terminal_turn_start=command_result.metadata.get("terminal_turn_start", ""),
                app_server_thread_id=command_result.metadata.get("app_server_thread_id", ""),
                app_server_turn_id=command_result.metadata.get("app_server_turn_id", ""),
                app_server_process_id=command_result.metadata.get("app_server_process_id", ""),
                task_transport=command_result.metadata.get("task_transport", "file"),
                task_artifact=command_result.metadata.get("task_artifact", str(task_artifact)),
                task_sha256=command_result.metadata.get("task_sha256", task_sha256),
                reuse_existing=reuse_existing,
                reuse_provenance=command_result.metadata.get("reuse_provenance", {}),
                error=error,
            )
            terminal_state = "completed" if status == "completed" else "cancelled" if status == "cancelled" else "failed"
            _publish_run_event(
                run_journal,
                method=f"run/{terminal_state}",
                state=terminal_state,
                detail={
                    "request_id": request.request_id,
                    "status": status,
                    "started_at": started_at,
                    "ended_at": finished_at,
                    "reason": error or summary,
                },
                thread_id=command_result.metadata.get("app_server_thread_id", ""),
                turn_id=command_result.metadata.get("app_server_turn_id", ""),
            )
            _emit(output, started, "[5/5] Writing result")
            atomic_write_json(result_file, result)
            return DelegationOutcome(status, request_id, result_file, run_dir, time.monotonic() - started)
    except KeyboardInterrupt:
        finished_at = _timestamp()
        _publish_run_event(
            run_journal,
            method="run/cancelled",
            state="cancelled",
            detail={"request_id": request_id, "started_at": started_at, "ended_at": finished_at, "reason": "Interrupted by user."},
        )
        return _failed_outcome(
            result_file=result_file,
            request_id=request_id,
            started_at=started_at,
            started=started,
            status="cancelled",
            summary="Delegation cancelled; repository modifications, if any, were preserved.",
            error="Interrupted by user.",
            executor_account=executor_account,
            executor_label=executor_label,
            executor_sandbox=executor_sandbox,
            parent_request_id=request.parent_request_id,
            reuse_existing=reuse_existing,
        )
    except (ConfigError, DelegationError, OSError, ValueError) as exc:
        finished_at = _timestamp()
        _publish_run_event(
            run_journal,
            method="run/failed",
            state="failed",
            detail={"request_id": request_id, "started_at": started_at, "ended_at": finished_at, "reason": sanitize_text(str(exc))},
        )
        return _failed_outcome(
            result_file=result_file,
            request_id=request_id,
            started_at=started_at,
            started=started,
            status="failed",
            summary="Delegation did not start or did not finish safely.",
            error=sanitize_text(str(exc)),
            executor_account=executor_account,
            executor_label=executor_label,
            executor_sandbox=executor_sandbox,
            parent_request_id=request.parent_request_id,
            reuse_existing=reuse_existing,
        )
