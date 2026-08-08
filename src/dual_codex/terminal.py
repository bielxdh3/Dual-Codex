from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from uuid import uuid4

from .config import AgentConfig, OrchestratorConfig
from .paths import path_identity_key, same_path
from .process import codex_environment
from .report import atomic_write_json


_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
TERMINAL_INLINE_MESSAGE_MAX = 500
TERMINAL_SUBMIT_DELAY_SECONDS = 0.1
TERMINAL_COMPOSER_ACK_TIMEOUT_SECONDS = 5.0
_COMPLETED_EVENTS = {"turn_completed", "turn_complete", "task_completed", "task_complete"}
_ABORTED_EVENTS = {"turn_aborted", "task_aborted"}
_TURN_START_EVENTS = {"task_started", "turn_started", "turn_start"}
_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)
_READY_PROMPT = re.compile(r"(?m)^\s*›\s*[^\r\n]*$")


class TuiReadinessDetector:
    """Small state machine for Codex's redraw-heavy startup screen."""

    READY = "READY"
    NOT_READY = "NOT_READY"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    FAILED = "FAILED"

    def __init__(self) -> None:
        self.state = self.NOT_READY
        self.seen_update = False
        self.seen_trust = False
        self.seen_ready = False
        self._raw = ""
        self.sanitized_tail = ""

    @staticmethod
    def sanitize(value: str) -> str:
        return _ANSI_SEQUENCE.sub("", value).replace("\r", "")

    def feed(self, chunk: str) -> str:
        self._raw = (self._raw + str(chunk))[-50000:]
        text = self.sanitize(self._raw)
        self.sanitized_tail = text[-12000:]
        if "Update now" in text and "Skip" in text:
            self.seen_update = True

        last_prompt = text.rfind("›")
        last_trust = text.rfind("Do you trust the contents of this directory?")
        last_disabled = text.rfind("Input disabled until setup completes")
        last_loading = text.rfind("model:     loading")
        last_model = max(text.rfind("model:"), text.rfind("model:     gpt-"))
        last_working = text.rfind("Working (")

        if last_trust > last_prompt or last_disabled > last_prompt or last_loading > last_model:
            self.seen_trust |= last_trust >= 0
            self.state = self.SETUP_REQUIRED if last_trust > last_prompt else self.NOT_READY
            return self.state
        if "Error:" in text[max(0, len(text) - 2000):] and last_prompt < last_working:
            self.state = self.FAILED
            return self.state

        has_prompt = _READY_PROMPT.search(text) is not None
        if has_prompt and last_prompt > last_working and last_model >= 0:
            self.seen_ready = True
            self.state = self.READY
        else:
            self.state = self.NOT_READY
        return self.state

    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "seen_update": self.seen_update,
            "seen_trust": self.seen_trust,
            "seen_ready": self.seen_ready,
            "tail": self.sanitized_tail[-4000:],
        }


class _HardenedTuiReadinessDetector:
    """Require a stable normal Codex screen before declaring the TUI idle."""

    READY = "READY"
    NOT_READY = "NOT_READY"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    FAILED = "FAILED"
    _PROMPT = re.compile(r"(?m)^\s*\u203a\s*(?P<text>[^\r\n]*)$")
    _MODEL = re.compile(r"(?m)^\s*(?:\u2502\s*)?model:\s+(?!loading\b)(?P<model>[^\r\n]+)$")
    _DIRECTORY = re.compile(r"(?m)^\s*(?:\u2502\s*)?directory:\s+(?P<directory>[^\r\n]+)$")
    _COSMETIC = frozenset(
        {
            "Explain this codebase",
            "Summarize recent commits",
            "Implement {feature}",
            "Find and fix a bug in @filename",
            "Write tests for @filename",
            "Improve documentation in @filename",
            "Run /review on my current changes",
            "Use /skills to list skills",
            "Use /skills to list available skills",
            "Check recently modified functions for compatibility",
            "How many files have been modified?",
            "Will this algorithm scale well?",
        }
    )
    _COSMETIC_PATTERN = re.compile(
        r"(?:@filename|\{feature\}|^Run\s+/review\b|^Use\s+/skills\b)",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.state = self.NOT_READY
        self.seen_update = False
        self.seen_trust = False
        self.seen_ready = False
        self.seen_model_ready = False
        self.seen_placeholder = False
        self.stable_samples = 0
        self.ready_evidence = ""
        self._fingerprint: tuple[str, str, str] | None = None
        self._raw = ""
        self.sanitized_tail = ""

    @staticmethod
    def sanitize(value: str) -> str:
        return _ANSI_SEQUENCE.sub("", value).replace("\r", "")

    def feed(self, chunk: str) -> str:
        self._raw = (self._raw + str(chunk))[-50000:]
        text = self.sanitize(self._raw)
        self.sanitized_tail = text[-12000:]
        if "Update now" in text and "Skip" in text:
            self.seen_update = True

        prompt_matches = list(self._PROMPT.finditer(text))
        prompt_match = prompt_matches[-1] if prompt_matches else None
        last_prompt = prompt_match.start() if prompt_match else -1
        last_trust = text.rfind("Do you trust the contents of this directory?")
        last_disabled = text.rfind("Input disabled until setup completes")
        loading_matches = list(re.finditer(r"(?m)^\s*(?:\u2502\s*)?model:\s+loading\b", text))
        last_loading = loading_matches[-1].start() if loading_matches else -1
        model_matches = list(self._MODEL.finditer(text))
        model_match = model_matches[-1] if model_matches else None
        last_model = model_match.start() if model_match else -1
        last_banner = text.rfind("OpenAI Codex")
        directory_matches = list(self._DIRECTORY.finditer(text))
        directory_match = directory_matches[-1] if directory_matches else None
        last_directory = directory_match.start() if directory_match else -1
        last_working = text.rfind("Working (")

        if last_trust > max(last_prompt, last_banner) or last_disabled > max(last_prompt, last_banner):
            self.seen_trust |= last_trust >= 0
            self.stable_samples = 0
            self.state = self.SETUP_REQUIRED if last_trust > max(last_prompt, last_banner) else self.NOT_READY
            return self.state
        if "Error:" in text[-2000:] and last_prompt < last_working:
            self.stable_samples = 0
            self.state = self.FAILED
            return self.state
        if last_loading > last_model:
            self.stable_samples = 0
            self.state = self.NOT_READY
            return self.state

        prompt_text = prompt_match.group("text").strip() if prompt_match else ""
        cosmetic = bool(prompt_text) and (
            prompt_text in self._COSMETIC or self._COSMETIC_PATTERN.search(prompt_text) is not None
        )
        self.seen_placeholder |= cosmetic
        normal_markers = (
            last_banner > last_loading
            and last_model >= 0
            and last_directory >= 0
            and last_model > last_loading
            and last_directory > last_loading
        )
        if prompt_match is not None and normal_markers and last_prompt > last_working:
            self.seen_model_ready = True
            fingerprint = (
                model_match.group("model").strip() if model_match else "",
                directory_match.group("directory").strip() if directory_match else "",
                "cosmetic" if cosmetic else "editable",
            )
            self.stable_samples = self.stable_samples + 1 if fingerprint == self._fingerprint else 1
            self._fingerprint = fingerprint
            if self.stable_samples >= 2:
                self.seen_ready = True
                self.ready_evidence = "stable Codex banner/model/directory/composer markers"
                self.state = self.READY
                return self.state
        else:
            self.stable_samples = 0
            self._fingerprint = None
        self.state = self.NOT_READY
        return self.state

    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "seen_update": self.seen_update,
            "seen_trust": self.seen_trust,
            "seen_ready": self.seen_ready,
            "seen_model_ready": self.seen_model_ready,
            "seen_placeholder": self.seen_placeholder,
            "stable_samples": self.stable_samples,
            "ready_evidence": self.ready_evidence,
            "tail": self.sanitized_tail[-4000:],
        }


TuiReadinessDetector = _HardenedTuiReadinessDetector


class TuiTurnStartDetector:
    """Treat a new busy marker as evidence, never composer text alone."""

    def __init__(self, baseline_output: str = "") -> None:
        baseline = TuiReadinessDetector.sanitize(baseline_output)
        self.baseline_working_lines = {
            line.strip() for line in baseline.splitlines() if "Working (" in line
        }
        self.started = False
        self.evidence = ""
        self.sanitized_tail = ""

    def feed(self, chunk: str) -> bool:
        text = TuiReadinessDetector.sanitize(str(chunk))
        self.sanitized_tail = text[-4000:]
        working_lines = {line.strip() for line in text.splitlines() if "Working (" in line}
        if len(working_lines) > len(self.baseline_working_lines) or working_lines - self.baseline_working_lines:
            self.started = True
            self.evidence = "terminal Working marker"
        return self.started

    def diagnostics(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "evidence": self.evidence,
            "tail": self.sanitized_tail,
        }


class TuiComposerAckDetector:
    """Acknowledge only a new, uniquely marked composer render."""

    def __init__(self, marker: str, baseline_output: str = "") -> None:
        self.marker = marker
        self.marker_without_whitespace = re.sub(r"\s+", "", marker)
        self.baseline = re.sub(r"\s+", "", TuiReadinessDetector.sanitize(baseline_output))
        self._raw = ""
        self.acknowledged = False
        self.evidence = ""
        self.sanitized_tail = ""

    def feed(self, chunk: str) -> bool:
        self._raw = (self._raw + str(chunk))[-50000:]
        text = TuiReadinessDetector.sanitize(self._raw)
        self.sanitized_tail = text[-4000:]
        normalized = re.sub(r"\s+", "", text)
        if self.marker_without_whitespace not in self.baseline and self.marker_without_whitespace in normalized:
            self.acknowledged = True
            self.evidence = "unique composer marker observed after text write"
        return self.acknowledged

    def diagnostics(self) -> dict[str, Any]:
        return {
            "acknowledged": self.acknowledged,
            "marker": self.marker,
            "evidence": self.evidence,
            "tail": self.sanitized_tail,
        }


class TerminalError(RuntimeError):
    pass


def validate_control_message(message: str, *, forbidden_text: str = "") -> str:
    if not isinstance(message, str) or not message.strip():
        raise TerminalError("Terminal control message must be non-empty text.")
    if "\r" in message or "\n" in message:
        raise TerminalError("Terminal control message must be a single physical line.")
    if "\x00" in message:
        raise TerminalError("Terminal control message contains NUL.")
    if len(message) > TERMINAL_INLINE_MESSAGE_MAX:
        raise TerminalError(
            f"Terminal control message exceeds the safe {TERMINAL_INLINE_MESSAGE_MAX}-character limit."
        )
    if forbidden_text and forbidden_text in message:
        raise TerminalError("Terminal control message unexpectedly contains the task body.")
    return message


def _new_control_marker() -> str:
    return f"[DC:{uuid4().hex[:8]}]"


def _marked_control_message(message: str, marker: str) -> str:
    return validate_control_message(f"{message} Ignore this final marker {marker}")


@dataclass(frozen=True)
class TerminalSession:
    session_id: str
    account: str
    label: str
    role: str
    repository: Path
    codex_home: Path
    pipe: str
    pid: int
    started_at: str
    log_file: Path
    session_file: str = ""
    state: str = "starting"
    add_dirs: tuple[Path, ...] = ()
    process_started_at: float = 0.0
    baseline_rollout_mtimes: dict[str, float] = field(default_factory=dict)
    codex_session_id: str = ""

    @classmethod
    def from_record(cls, raw: dict[str, Any]) -> "TerminalSession":
        return cls(
            session_id=str(raw["session_id"]),
            account=str(raw.get("account", "")),
            label=str(raw.get("label", "")),
            role=str(raw.get("role", "")),
            repository=Path(raw["repository"]),
            codex_home=Path(raw["codex_home"]),
            pipe=str(raw["pipe"]),
            pid=int(raw["pid"]),
            started_at=str(raw.get("started_at", "")),
            log_file=Path(raw["log_file"]),
            session_file=str(raw.get("session_file", "")),
            state=str(raw.get("state", "unknown")),
            add_dirs=tuple(Path(item) for item in raw.get("add_dirs", [])),
            process_started_at=float(raw.get("process_started_at", 0.0)),
            baseline_rollout_mtimes={
                str(key): float(value)
                for key, value in dict(raw.get("baseline_rollout_mtimes", {})).items()
            },
            codex_session_id=str(raw.get("codex_session_id", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "account": self.account,
            "label": self.label,
            "role": self.role,
            "backend": "windows",
            "repository": str(self.repository),
            "codex_home": str(self.codex_home),
            "pipe": self.pipe,
            "pid": self.pid,
            "started_at": self.started_at,
            "log_file": str(self.log_file),
            "session_file": self.session_file,
            "state": self.state,
            "add_dirs": [str(item) for item in self.add_dirs],
            "process_started_at": self.process_started_at,
            "baseline_rollout_mtimes": self.baseline_rollout_mtimes,
            "codex_session_id": self.codex_session_id,
        }


def validate_session_id(value: str) -> str:
    if not _SESSION_ID.fullmatch(value):
        raise TerminalError("Session IDs may contain only letters, numbers, '_' and '-'.")
    return value


def session_id_for(account: str, repository: Path) -> str:
    digest = hashlib.sha256(path_identity_key(repository).encode("utf-8")).hexdigest()[:12]
    return validate_session_id(f"{account}-{digest}")


def interactive_command_args(
    repository: Path,
    *,
    sandbox: str,
    approval_policy: str,
    model: str = "",
) -> list[str]:
    if sandbox not in {"read-only", "workspace-write"}:
        raise TerminalError(f"Unsupported sandbox '{sandbox}'.")
    if approval_policy not in {"on-request", "never"}:
        raise TerminalError(f"Unsupported approval policy '{approval_policy}'.")
    args = ["--no-alt-screen", "--cd", str(repository), "--sandbox", sandbox, "-a", approval_policy]
    if model:
        args.extend(["--model", model])
    return args


def _session_dir(config: OrchestratorConfig) -> Path:
    return config.runs_dir / "terminal-sessions"


def _record_path(config: OrchestratorConfig, session_id: str) -> Path:
    return _session_dir(config) / f"{validate_session_id(session_id)}.json"


def _node_path(config: OrchestratorConfig) -> str:
    value = shutil.which(config.node_command) or config.node_command
    if not Path(value).exists() and shutil.which(value) is None:
        raise TerminalError(f"Node.js executable not found: {config.node_command}")
    return value


def _helper_path(config: OrchestratorConfig) -> Path:
    helper = config.project_root / "scripts" / "pty-host.js"
    if not helper.is_file():
        raise TerminalError(f"ConPTY host is missing: {helper}")
    return helper


def _terminal_environment(agent: AgentConfig) -> dict[str, str]:
    """Use the account profile, never an API key inherited from the orchestrator."""
    return codex_environment(agent)


def _pipe_request(pipe: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(pipe, "r+b", buffering=0) as connection:
            connection.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = connection.readline()
    except OSError as exc:
        raise TerminalError(f"Could not reach terminal session: {exc}") from exc
    if not line:
        raise TerminalError("Terminal session closed its control pipe.")
    try:
        result = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalError("Terminal session returned invalid control data.") from exc
    if not result.get("ok"):
        raise TerminalError(str(result.get("error", "Terminal session request failed.")))
    return result


def _session_files(codex_home: Path) -> list[Path]:
    root = codex_home / "sessions"
    if not root.is_dir():
        return []
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [item for item in directories if not Path(current, item).is_symlink()]
        files.extend(Path(current, name) for name in names if name.startswith("rollout-") and name.endswith(".jsonl"))
    return files


def find_session_file(codex_home: Path, repository: Path, *, after: float = 0.0) -> tuple[Path, str] | None:
    match: tuple[Path, float, str] | None = None
    for path in _session_files(codex_home):
        try:
            stat = path.stat()
            if stat.st_mtime < after:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                first = json.loads(stream.readline())
            payload = first.get("payload", first) if isinstance(first, dict) else {}
            cwd = str(payload.get("cwd", ""))
            session_id = str(payload.get("session_id") or payload.get("id") or "")
            if same_path(cwd, repository) and session_id and (match is None or stat.st_mtime > match[1]):
                match = (path, stat.st_mtime, session_id)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
    return (match[0], match[2]) if match else None


def _rollout_records(codex_home: Path, repository: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _session_files(codex_home):
        try:
            stat = path.stat()
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                first = json.loads(stream.readline())
            payload = first.get("payload", first) if isinstance(first, dict) else {}
            if not isinstance(payload, dict):
                continue
            cwd = str(payload.get("cwd", ""))
            session_id = str(payload.get("session_id") or payload.get("id") or "")
            if not cwd or not same_path(cwd, repository) or not session_id:
                continue
            records.append(
                {
                    "path": path,
                    "rollout_id": path.stem.removeprefix("rollout-"),
                    "codex_session_id": session_id,
                    "mtime": stat.st_mtime,
                    "timestamp": str(first.get("timestamp") or payload.get("timestamp") or ""),
                }
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
    return records


def _rollout_snapshot(codex_home: Path, repository: Path) -> dict[str, float]:
    return {str(item["rollout_id"]): float(item["mtime"]) for item in _rollout_records(codex_home, repository)}


def _payload_type(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    payload = entry.get("payload", entry)
    return str(payload.get("type", "")) if isinstance(payload, dict) else ""


def session_turn_state(path: Path, offset: int = 0) -> tuple[str, str]:
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            entries = [json.loads(line) for line in stream.read().decode("utf-8", "replace").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown", ""
    state = "active"
    assistant = ""
    for entry in entries:
        event_type = _payload_type(entry)
        payload = entry.get("payload", entry) if isinstance(entry, dict) else {}
        if event_type in _ABORTED_EVENTS:
            state = "aborted"
        elif event_type in _COMPLETED_EVENTS:
            state = "completed"
        if isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "assistant":
            content = payload.get("content", [])
            if isinstance(content, list):
                assistant = "\n".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ).strip()
    return state, assistant


def session_turn_started(path: Path, offset: int = 0) -> str:
    """Return the first non-secret turn-start event after an offset."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            entries = [
                json.loads(line)
                for line in stream.read().decode("utf-8", "replace").splitlines()
                if line.strip()
            ]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    for entry in entries:
        event_type = _payload_type(entry)
        if event_type in _TURN_START_EVENTS:
            return event_type
    return ""


class TerminalManager:
    def __init__(self, config: OrchestratorConfig):
        if os.name != "nt":
            raise TerminalError("The native ConPTY backend requires Windows.")
        self.config = config

    def _load(self, session_id: str) -> TerminalSession:
        path = _record_path(self.config, session_id)
        try:
            return TerminalSession.from_record(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise TerminalError(f"Unknown terminal session '{session_id}'.") from exc

    def status(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        try:
            result = _pipe_request(session.pipe, {"op": "status"})
            state = result["state"]
            return {**session.as_dict(), **state, "state": "running" if state.get("alive") else "exited"}
        except TerminalError:
            return {**session.as_dict(), "state": "unreachable"}

    @staticmethod
    def _codex_turn_activity(session: TerminalSession) -> dict[str, Any]:
        baseline = getattr(session, "baseline_rollout_mtimes", {}) or {}
        process_started_at = float(getattr(session, "process_started_at", 0.0) or 0.0)
        associated_session_id = str(getattr(session, "codex_session_id", "") or "")
        ignored_stale: list[dict[str, Any]] = []
        for record in _rollout_records(session.codex_home, session.repository):
            if session_turn_state(record["path"])[0] != "active":
                continue
            rollout_id = str(record["rollout_id"])
            rollout_mtime = float(record["mtime"])
            baseline_mtime = baseline.get(rollout_id)
            unchanged_from_baseline = (
                baseline_mtime is not None and rollout_mtime <= float(baseline_mtime) + 0.001
            )
            same_current_session = bool(
                associated_session_id and record["codex_session_id"] == associated_session_id
            )
            if same_current_session:
                return {
                    "active": True,
                    "source": "current_session",
                    "rollout_id": rollout_id,
                    "codex_session_id": record["codex_session_id"],
                    "rollout_timestamp": record["timestamp"],
                    "rollout_mtime": rollout_mtime,
                    "predates_process": process_started_at > 0 and rollout_mtime < process_started_at,
                }
            if not unchanged_from_baseline:
                return {
                    "active": True,
                    "source": "current_epoch",
                    "rollout_id": rollout_id,
                    "codex_session_id": record["codex_session_id"],
                    "rollout_timestamp": record["timestamp"],
                    "rollout_mtime": rollout_mtime,
                    "predates_process": process_started_at > 0 and rollout_mtime < process_started_at,
                }
            ignored_stale.append(
                {
                    "rollout_id": rollout_id,
                    "codex_session_id": record["codex_session_id"],
                    "rollout_timestamp": record["timestamp"],
                    "rollout_mtime": rollout_mtime,
                    "predates_process": True,
                }
            )
        if ignored_stale:
            return {
                "active": False,
                "source": "historical_stale",
                "ignored_stale_rollout": ignored_stale,
            }
        return {"active": False, "source": "none"}

    @classmethod
    def _codex_turn_active(cls, session: TerminalSession) -> bool:
        return bool(cls._codex_turn_activity(session)["active"])

    def _associate_codex_session(self, session: TerminalSession, codex_session_id: str) -> TerminalSession:
        if not codex_session_id or codex_session_id == session.codex_session_id:
            return session
        updated = replace(session, codex_session_id=codex_session_id)
        try:
            atomic_write_json(_record_path(self.config, session.session_id), updated.as_dict())
        except OSError:
            pass
        return updated

    def wait_until_ready(self, session_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        session = self._load(session_id)
        detector = TuiReadinessDetector()
        limit = float(timeout if timeout is not None else self.config.terminal_readiness_timeout)
        started = time.monotonic()
        rollout_activity: dict[str, Any] = {"active": False, "source": "none"}
        while time.monotonic() - started < limit:
            try:
                output = self.read(session_id, 1000)
            except TerminalError as exc:
                diagnostics = detector.diagnostics()
                diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 3)
                self._write_readiness_diagnostics(session, diagnostics)
                raise TerminalError(
                    f"Codex TUI exited before becoming ready for session '{session_id}': "
                    f"{json.dumps(diagnostics, ensure_ascii=False)}"
                ) from exc
            state = detector.feed(output)
            rollout_activity = self._codex_turn_activity(session)
            if state == TuiReadinessDetector.READY:
                if rollout_activity["active"]:
                    time.sleep(0.25)
                    continue
                diagnostics = detector.diagnostics()
                diagnostics["rollout_activity"] = rollout_activity
                return diagnostics
            if state == TuiReadinessDetector.SETUP_REQUIRED:
                diagnostics = detector.diagnostics()
                diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 3)
                self._write_readiness_diagnostics(session, diagnostics)
                self._stop_unready_session(session_id)
                raise TerminalError(
                    f"Codex TUI requires explicit repository trust/setup for session '{session_id}': "
                    f"{json.dumps(diagnostics, ensure_ascii=False)}"
                )
            if state == TuiReadinessDetector.FAILED:
                diagnostics = detector.diagnostics()
                diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 3)
                self._write_readiness_diagnostics(session, diagnostics)
                self._stop_unready_session(session_id)
                raise TerminalError(
                    f"Codex TUI failed before becoming ready for session '{session_id}': "
                    f"{json.dumps(diagnostics, ensure_ascii=False)}"
                )
            time.sleep(0.25)

        diagnostics = detector.diagnostics()
        diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        diagnostics["rollout_activity"] = rollout_activity
        diagnostics["terminal_session_id"] = str(getattr(session, "session_id", session_id))
        diagnostics["terminal_pid"] = getattr(session, "pid", None)
        diagnostics["terminal_started_at"] = str(getattr(session, "started_at", ""))
        self._write_readiness_diagnostics(session, diagnostics)
        self._stop_unready_session(session_id)
        raise TerminalError(
            f"Timed out after {limit:.1f}s waiting for Codex TUI readiness for session '{session_id}': "
            f"{json.dumps(diagnostics, ensure_ascii=False)}"
        )

    def wait_for_composer_ack(
        self,
        session_id: str,
        *,
        marker: str,
        baseline_output: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session = self._load(session_id)
        detector = TuiComposerAckDetector(marker, baseline_output)
        limit = float(timeout if timeout is not None else TERMINAL_COMPOSER_ACK_TIMEOUT_SECONDS)
        started = time.monotonic()
        while time.monotonic() - started < limit:
            try:
                output = self.read(session_id, 1000)
            except TerminalError as exc:
                diagnostics = detector.diagnostics()
                diagnostics.update(
                    {
                        "state": "failed",
                        "enter_sent": False,
                        "resend_attempted": False,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
                )
                self._write_composer_ack_diagnostics(session, diagnostics)
                raise TerminalError(
                    f"Codex TUI exited before composer acknowledgement for session '{session_id}': "
                    f"{json.dumps(diagnostics, ensure_ascii=False)}"
                ) from exc
            if detector.feed(output):
                return {
                    "state": "composer_acknowledged",
                    "source": detector.evidence,
                    "marker": marker,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            time.sleep(0.05)
        diagnostics = detector.diagnostics()
        diagnostics.update(
            {
                "state": "composer_ack_timeout",
                "enter_sent": False,
                "resend_attempted": False,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        self._write_composer_ack_diagnostics(session, diagnostics)
        raise TerminalError(
            f"Timed out after {limit:.1f}s waiting for composer acknowledgement for session '{session_id}'; "
            f"Enter was not sent: {json.dumps(diagnostics, ensure_ascii=False)}"
        )

    def wait_until_turn_started(
        self,
        session_id: str,
        *,
        cursor: tuple[Path | None, int] | None = None,
        baseline_output: str = "",
        submitted_at: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Confirm one submitted prompt created a real Codex turn."""
        session = self._load(session_id)
        detector = TuiTurnStartDetector(baseline_output)
        limit = float(timeout if timeout is not None else self.config.terminal_turn_start_timeout)
        started = time.monotonic()
        sent_at = float(submitted_at if submitted_at is not None else time.time())
        cursor_path, cursor_offset = cursor or (None, 0)
        codex_session_id = ""
        while time.monotonic() - started < limit:
            discovery_after = sent_at if cursor_path is None else max(0.0, sent_at - 5.0)
            discovered = find_session_file(
                session.codex_home,
                session.repository,
                after=discovery_after,
            )
            if discovered:
                path, codex_session_id = discovered
                offset = cursor_offset if cursor_path == path else 0
                event = session_turn_started(path, offset)
                if event:
                    session = self._associate_codex_session(session, codex_session_id)
                    return {
                        "state": "turn_started",
                        "source": "session_metadata",
                        "event": event,
                        "session_id": codex_session_id,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
            try:
                output = self.read(session_id, 1000)
            except TerminalError as exc:
                diagnostics = detector.diagnostics()
                diagnostics.update(
                    {
                        "state": "failed",
                        "submitted_once": True,
                        "resend_attempted": False,
                        "session_id": codex_session_id,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
                )
                self._write_turn_start_diagnostics(session, diagnostics)
                self._stop_unready_session(session_id)
                raise TerminalError(
                    f"Codex TUI exited before turn start for session '{session_id}': "
                    f"{json.dumps(diagnostics, ensure_ascii=False)}"
                ) from exc
            if detector.feed(output):
                return {
                    "state": "turn_started",
                    "source": detector.evidence,
                    "session_id": codex_session_id,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            time.sleep(0.25)

        diagnostics = detector.diagnostics()
        diagnostics.update(
            {
                "state": "timeout",
                "submitted_once": True,
                "resend_attempted": False,
                "session_id": codex_session_id,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        self._write_turn_start_diagnostics(session, diagnostics)
        self._stop_unready_session(session_id)
        raise TerminalError(
            f"Timed out after {limit:.1f}s waiting for Codex turn start for session '{session_id}'; "
            f"the prompt was sent once and may remain in the composer: "
            f"{json.dumps(diagnostics, ensure_ascii=False)}"
        )

    @staticmethod
    def _write_readiness_diagnostics(session: TerminalSession, diagnostics: dict[str, Any]) -> None:
        try:
            with session.log_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n[readiness-diagnostics]\n")
                stream.write(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                stream.write("\n")
        except OSError:
            pass

    @staticmethod
    def _write_turn_start_diagnostics(session: TerminalSession, diagnostics: dict[str, Any]) -> None:
        try:
            with session.log_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n[turn-start-diagnostics]\n")
                stream.write(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                stream.write("\n")
        except OSError:
            pass

    @staticmethod
    def _write_composer_ack_diagnostics(session: TerminalSession, diagnostics: dict[str, Any]) -> None:
        try:
            with session.log_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n[composer-ack-diagnostics]\n")
                stream.write(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                stream.write("\n")
        except OSError:
            pass

    def _stop_unready_session(self, session_id: str) -> None:
        try:
            self.terminate(session_id)
        except TerminalError:
            pass

    def start(
        self,
        *,
        session_id: str,
        agent: AgentConfig,
        role: str,
        repository: Path,
        approval_policy: str = "on-request",
        add_dirs: tuple[Path, ...] = (),
    ) -> TerminalSession:
        if agent.backend != "windows":
            raise TerminalError(f"Backend '{agent.backend}' is not implemented by the native terminal host.")
        validate_session_id(session_id)
        repository = repository.resolve()
        if not repository.is_dir():
            raise TerminalError(f"Terminal repository does not exist: {repository}")
        resolved_add_dirs = tuple(Path(item).resolve() for item in add_dirs)
        for add_dir in resolved_add_dirs:
            if not add_dir.is_dir() or add_dir.is_symlink():
                raise TerminalError(f"Task transport directory does not exist or is unsafe: {add_dir}")
        record_path = _record_path(self.config, session_id)
        if record_path.exists():
            current = self.status(session_id)
            if current.get("state") in {"running", "starting"}:
                raise TerminalError(f"Terminal session '{session_id}' already exists.")
            if current.get("state") == "unreachable":
                raise TerminalError(f"Existing terminal session '{session_id}' could not be verified.")
            record_path.unlink()
        sessions = _session_dir(self.config)
        sessions.mkdir(parents=True, exist_ok=True)
        pipe = rf"\\.\pipe\dual-codex-{session_id}-{uuid4().hex}"
        log_file = sessions / f"{session_id}.pty.log"
        node = _node_path(self.config)
        helper = _helper_path(self.config)
        command = [
            node, str(helper), "--session-id", session_id, "--pipe", pipe,
            "--cwd", str(repository), "--codex-command", self.config.codex_command,
            "--sandbox", agent.sandbox, "--approval-policy", approval_policy,
            "--model", agent.model,
        ]
        if resolved_add_dirs:
            command.extend(["--add-dir", str(resolved_add_dirs[0])])
        env = _terminal_environment(agent)
        process_started_at = time.time()
        baseline_rollout_mtimes = _rollout_snapshot(agent.codex_home, repository)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        with log_file.open("a", encoding="utf-8", newline="\n") as log:
            process = subprocess.Popen(
                command, cwd=repository, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=flags, close_fds=os.name != "nt",
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                _pipe_request(pipe, {"op": "status"})
                break
            except TerminalError:
                time.sleep(0.1)
        else:
            if process.poll() is None:
                process.terminate()
            raise TerminalError("ConPTY host did not become ready within 10 seconds.")
        session = TerminalSession(
            session_id=session_id, account=agent.account_name, label=agent.label,
            role=role, repository=repository, codex_home=agent.codex_home,
            pipe=pipe, pid=process.pid, started_at=datetime.now(timezone.utc).isoformat(),
            log_file=log_file, add_dirs=resolved_add_dirs,
            process_started_at=process_started_at,
            baseline_rollout_mtimes=baseline_rollout_mtimes,
        )
        atomic_write_json(record_path, session.as_dict())
        self.wait_until_ready(session_id)
        return session

    def ensure(self, **kwargs: Any) -> TerminalSession:
        session_id = str(kwargs["session_id"])
        if _record_path(self.config, session_id).exists():
            current = self.status(session_id)
            if current.get("state") == "running":
                requested = {Path(item).resolve() for item in kwargs.get("add_dirs", ())}
                available = {Path(item).resolve() for item in self._load(session_id).add_dirs}
                if not requested.issubset(available):
                    raise TerminalError(
                        f"Existing terminal session '{session_id}' was not started with the required task transport directory."
                    )
                self.wait_until_ready(session_id)
                return self._load(session_id)
        return self.start(**kwargs)

    def send(self, session_id: str, message: str) -> dict[str, Any]:
        session = self._load(session_id)
        self.wait_until_ready(session_id)
        transport_metadata: dict[str, Any] = {}
        if not isinstance(message, str) or not message.strip():
            raise TerminalError("Terminal message must be non-empty text.")
        message_to_send = message
        if len(message) > TERMINAL_INLINE_MESSAGE_MAX:
            if not session.add_dirs:
                raise TerminalError(
                    "Long terminal messages require a dedicated file-backed task transport directory."
                )
            artifact_dir = session.add_dirs[0].resolve()
            if not artifact_dir.is_dir() or artifact_dir.is_symlink():
                raise TerminalError(f"Task transport directory is unavailable: {artifact_dir}")
            artifact_path = artifact_dir / f"followup-{uuid4().hex}.md"
            content = f"# Dual Codex follow-up instructions\n\n{message.rstrip()}\n"
            artifact_path.write_text(content, encoding="utf-8", newline="\n")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            message_to_send = (
                f'Read the complete follow-up instructions in "{artifact_path}" in the current repository; '
                "execute them and return the required report."
            )
            transport_metadata = {
                "task_transport": "file",
                "task_artifact": str(artifact_path),
                "task_sha256": digest,
            }
        message_to_send = validate_control_message(message_to_send)
        marker = _new_control_marker()
        message_to_send = _marked_control_message(message_to_send, marker)
        baseline_output = self.read(session_id, 1000)
        cursor = self.turn_cursor(session_id)
        submitted_at = time.time()
        text_write_started = time.monotonic()
        _pipe_request(session.pipe, {"op": "send_text", "message": message_to_send})
        text_write_completed = time.monotonic()
        composer_ack = self.wait_for_composer_ack(
            session_id,
            marker=marker,
            baseline_output=baseline_output,
        )
        composer_acknowledged = time.monotonic()
        remaining_debounce = TERMINAL_SUBMIT_DELAY_SECONDS - (composer_acknowledged - text_write_completed)
        if remaining_debounce > 0:
            time.sleep(remaining_debounce)
        enter_write_started = time.monotonic()
        _pipe_request(session.pipe, {"op": "submit"})
        enter_write_completed = time.monotonic()
        evidence = self.wait_until_turn_started(
            session_id,
            cursor=cursor,
            baseline_output=baseline_output,
            submitted_at=submitted_at,
        )
        turn_start_observed = time.monotonic()
        evidence["terminal_input_sequence"] = "text_then_carriage_return"
        evidence["composer_ack"] = composer_ack
        evidence["terminal_input_timing"] = {
            "text_write_started": text_write_started,
            "text_write_completed": text_write_completed,
            "composer_acknowledged": composer_acknowledged,
            "enter_write_started": enter_write_started,
            "enter_write_completed": enter_write_completed,
            "turn_start_observed": turn_start_observed,
            "text_to_ack_delay_ms": round((composer_acknowledged - text_write_completed) * 1000, 3),
            "ack_to_enter_delay_ms": round((enter_write_started - composer_acknowledged) * 1000, 3),
            "text_to_enter_delay_ms": round((enter_write_started - text_write_completed) * 1000, 3),
            "enter_to_turn_start_ms": round((turn_start_observed - enter_write_completed) * 1000, 3),
            "configured_delay_ms": int(TERMINAL_SUBMIT_DELAY_SECONDS * 1000),
        }
        evidence["control_char_count"] = len(message_to_send)
        evidence["control_byte_count"] = len(message_to_send.encode("utf-8"))
        evidence["delivery_mode"] = "raw"
        evidence["composer_marker"] = marker
        evidence["enter_sent"] = True
        evidence["resend_attempted"] = False
        evidence.update(transport_metadata)
        return evidence

    def read(self, session_id: str, lines: int = 80) -> str:
        session = self._load(session_id)
        return str(_pipe_request(session.pipe, {"op": "read", "lines": lines}).get("output", ""))

    def turn_cursor(self, session_id: str) -> tuple[Path | None, int]:
        session = self._load(session_id)
        discovered = find_session_file(session.codex_home, session.repository, after=time.time() - 30)
        if not discovered:
            return None, 0
        path, _ = discovered
        try:
            return path, path.stat().st_size
        except OSError:
            return path, 0

    def wait_for_turn(
        self,
        session_id: str,
        *,
        cursor: tuple[Path | None, int] | None = None,
        timeout: float = 900,
        progress: Any = None,
    ) -> dict[str, str]:
        session = self._load(session_id)
        discovered = find_session_file(session.codex_home, session.repository, after=time.time() - 30)
        session_path = (cursor[0] if cursor else None) or (discovered[0] if discovered else None)
        session_id_from_codex = discovered[1] if discovered else ""
        offset = cursor[1] if cursor else (session_path.stat().st_size if session_path and session_path.exists() else 0)
        started = time.monotonic()
        last_progress = started
        while time.monotonic() - started < timeout:
            if session_path is None or not session_path.exists():
                discovered = find_session_file(session.codex_home, session.repository, after=time.time() - 30)
                if discovered:
                    session_path, session_id_from_codex = discovered
                    offset = 0
            if session_path and session_path.exists():
                state, assistant = session_turn_state(session_path, offset)
                if state == "completed":
                    return {"state": state, "assistant": assistant, "session_id": session_id_from_codex, "session_file": str(session_path)}
                if state == "aborted":
                    raise TerminalError("Codex interactive turn was aborted.")
            if progress and time.monotonic() - last_progress >= 15:
                progress(f"terminal session {session_id} still running ({int(time.monotonic() - started)}s elapsed)")
                last_progress = time.monotonic()
            time.sleep(0.5)
        raise TerminalError(f"Timed out waiting for terminal session '{session_id}'.")

    def terminate(self, session_id: str) -> None:
        session = self._load(session_id)
        _pipe_request(session.pipe, {"op": "terminate"})
        record_path = _record_path(self.config, session_id)
        if record_path.exists():
            record_path.unlink()

    def list(self) -> list[dict[str, Any]]:
        directory = _session_dir(self.config)
        if not directory.is_dir():
            return []
        rows = []
        for path in sorted(directory.glob("*.json")):
            try:
                session = TerminalSession.from_record(json.loads(path.read_text(encoding="utf-8")))
                rows.append(self.status(session.session_id))
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return rows
