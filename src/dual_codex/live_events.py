from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .paths import PathLike, path_identity_key


LIVE_EVENT_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 2000
DEFAULT_MAX_RECORD_BYTES = 65536
DEFAULT_MAX_DETAIL_BYTES = 16384
_MAX_SAFE_TEXT_BYTES = 4096
_JOURNAL_DIRECTORY = "live-events"
_IDENTITY = re.compile(r"[^A-Za-z0-9._-]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AUTH_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\r\n\s\"']*auth\.json")
_SECRET = re.compile(
    r"(?ix)(authorization\s*:\s*bearer\s+|\b(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\"?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"
)
_HIDDEN_KEY = re.compile(
    r"(?i)^(?:analysis|analysis_text|chain[_-]?of[_-]?thought|cot|hidden(?:_reasoning)?|internal[_-]?reasoning|reasoning(?:[_-]?(?:content|delta))?|thoughts?)$"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential|(?:^|[_-])token$)"
)
_SENSITIVE_PATH = re.compile(
    r"(?i)(?:auth\.json|(?:^|[\\/])(?:\.env|credentials?|private(?:[_-]?key)?|secrets?|cookies?|backups?|databases?)(?:[\\/]|$)|codexprofiles)"
)
_OUTPUT_KEYS = {"chunk", "delta", "output", "stderr", "stdout", "text"}
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def utc_timestamp() -> str:
    """Return an unambiguous UTC timestamp for a journal record."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_text(value: Any, max_bytes: int) -> str:
    text = html.unescape(_CONTROL.sub(" ", str(value)).replace("\r", " ").replace("\n", " "))
    text = _AUTH_PATH.sub("[REDACTED_AUTH_PATH]", text)
    text = _SECRET.sub("[REDACTED]", text)
    text = html.escape(text, quote=True)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ""
    marker = "...[truncated]"
    marker_bytes = len(marker.encode("utf-8"))
    if marker_bytes >= max_bytes:
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - marker_bytes].decode("utf-8", errors="ignore")
    return prefix + marker


def _safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Make protocol data JSON-safe without retaining hidden reasoning fields."""

    if _HIDDEN_KEY.fullmatch(key):
        return None
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth > 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, str)):
        if not isinstance(value, str):
            return value
        safe = _bounded_text(value, _MAX_SAFE_TEXT_BYTES)
        if _SENSITIVE_PATH.search(safe) and re.search(r"(?i)(?:path|file|cwd|directory|home|repository)", key):
            return "[REDACTED_PATH]"
        return safe
    if isinstance(value, float):
        return value if math.isfinite(value) else "[REDACTED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:64]:
            name = _bounded_text(raw_key, 128)
            if _HIDDEN_KEY.fullmatch(name):
                continue
            if _SENSITIVE_KEY.search(name):
                result[name] = "[REDACTED]"
                continue
            child = _safe_value(raw_value, key=name, depth=depth + 1)
            if child is not None:
                result[name] = child
        return result
    if isinstance(value, (list, tuple)):
        return [
            child
            for child in (_safe_value(item, depth=depth + 1) for item in list(value)[:64])
            if child is not None
        ]
    return _bounded_text(repr(value), _MAX_SAFE_TEXT_BYTES)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _fit_detail(value: Any, max_bytes: int) -> Any:
    safe = _safe_value(value)
    if len(_json_bytes(safe)) <= max_bytes:
        return safe
    if max_bytes < len(_json_bytes(None)):
        return None
    encoded = _json_bytes(safe).decode("utf-8", errors="replace")
    for text_limit in range(max_bytes, -1, -8):
        candidate = {"truncated": True, "text": _bounded_text(encoded, max(0, text_limit))}
        if len(_json_bytes(candidate)) <= max_bytes:
            return candidate
    return None


def chunk_text(value: Any, max_bytes: int) -> list[str]:
    """Split text on UTF-8 boundaries so command output cannot exceed a chunk limit."""

    safe = _bounded_text(value, max(1, len(str(value).encode("utf-8")) + 1))
    if not safe:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in safe:
        size = len(character.encode("utf-8"))
        if current and current_bytes + size > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += size
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _output_chunks(value: Any, max_detail_bytes: int) -> list[dict[str, Any]]:
    """Sanitize/cap output, then fit each complete JSON detail envelope to the limit."""

    text = _bounded_text(value, _MAX_SAFE_TEXT_BYTES)
    chunk_count = 1
    for _ in range(32):
        chunks: list[str] = []
        current: list[str] = []
        for character in text:
            candidate = "".join(current) + character
            detail = {
                "text": candidate,
                "chunk_index": len(chunks),
                "chunk_count": chunk_count,
            }
            if current and len(_json_bytes(detail)) > max_detail_bytes:
                chunks.append("".join(current))
                current = [character]
            elif not current and len(_json_bytes(detail)) > max_detail_bytes:
                raise ValueError("Live event detail limit is too small for output chunks.")
            else:
                current.append(character)
        if current or not chunks:
            chunks.append("".join(current))
        if len(chunks) == chunk_count:
            return [
                {
                    "text": chunk,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                }
                for index, chunk in enumerate(chunks)
            ]
        chunk_count = len(chunks)
    raise ValueError("Live event output chunk sizing did not converge.")


def _safe_identity(value: Any, fallback: str = "unknown") -> str:
    value = _IDENTITY.sub("_", str(value).strip()).strip("._-")
    return (value[:96] or fallback)


def _strip_windows_extended_prefix(raw: str) -> str:
    """Convert supported Windows extended paths to ordinary drive/UNC paths."""

    lowered = raw.casefold()
    if lowered.startswith(("\\\\?\\", "//?/")):
        prefix_length = 4
        tail = raw[prefix_length:]
        tail_lowered = tail.casefold()
        if tail_lowered.startswith(("unc\\", "unc/")):
            return "\\\\" + tail[4:]
        if len(tail) >= 2 and tail[1] == ":":
            return tail
        raise ValueError("Unsupported Windows extended path.")
    return raw


def _normalise_path(value: PathLike) -> Path:
    """Resolve normal and Windows extended paths to one safe local form."""

    raw = os.fspath(value)
    if os.name == "nt":
        raw = _strip_windows_extended_prefix(raw)
    resolved = Path(raw).expanduser().resolve(strict=False)
    if os.name == "nt":
        resolved = Path(_strip_windows_extended_prefix(os.fspath(resolved)))
    return resolved


def repository_identity(repository: PathLike) -> str:
    return hashlib.sha256(path_identity_key(_normalise_path(repository)).encode("utf-8")).hexdigest()


def _resolved_under(root: Path, candidate: Path) -> Path:
    root = _normalise_path(root)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = _normalise_path(candidate)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Live event journal path must remain under the runtime root.") from exc
    return candidate


def journal_path(
    runtime_root: PathLike,
    *,
    account: str,
    role: str,
    repository: PathLike,
) -> Path:
    root = _normalise_path(runtime_root)
    candidate = root / _JOURNAL_DIRECTORY / _safe_identity(account) / _safe_identity(role) / f"{repository_identity(repository)}.jsonl"
    return _resolved_under(root, candidate)


def _extract_identifier(params: Mapping[str, Any], name: str, nested: str) -> str:
    value = params.get(name)
    if isinstance(value, str) and value:
        return _safe_identity(value, "")
    child = params.get(nested)
    if isinstance(child, Mapping):
        value = child.get("id")
        if isinstance(value, str) and value:
            return _safe_identity(value, "")
    return ""


def _notification_text(value: Any) -> str:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _OUTPUT_KEYS and isinstance(child, str):
                return child
        for child in value.values():
            found = _notification_text(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _notification_text(child)
            if found:
                return found
    return ""


def normalize_notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize known App Server notifications while retaining unknown methods."""

    params = params if isinstance(params, Mapping) else {}
    raw_method = str(method)
    lowered = raw_method.casefold()
    if lowered.startswith("turn/"):
        kind = "turn"
        state = lowered.rsplit("/", 1)[-1]
        turn = params.get("turn")
        if isinstance(turn, Mapping) and isinstance(turn.get("status"), str) and state == "completed":
            state = turn["status"]
    elif "commandexecution" in lowered or "command_execution" in lowered:
        kind = "command_execution"
        state = lowered.rsplit("/", 1)[-1].replace("outputdelta", "output").replace("delta", "output")
    elif "filechange" in lowered or "file_change" in lowered:
        kind = "file_change"
        state = lowered.rsplit("/", 1)[-1].replace("delta", "updated")
    elif "agentmessage" in lowered or "agent_message" in lowered:
        kind = "agent_message"
        state = lowered.rsplit("/", 1)[-1].replace("delta", "updated")
    elif "tokenusage" in lowered or "token_usage" in lowered:
        kind = "token_usage"
        state = "updated"
    elif lowered == "error" or lowered.endswith("/error") or "failed" in lowered:
        kind = "error"
        state = "failed"
    else:
        kind = "notification"
        state = "observed"
    return {
        "kind": _safe_identity(kind),
        "state": _safe_identity(state),
        "method": _bounded_text(raw_method, 256),
        "thread_id": _extract_identifier(params, "threadId", "thread"),
        "turn_id": _extract_identifier(params, "turnId", "turn"),
        "detail": _safe_value(params),
    }


@dataclass(frozen=True)
class LiveEvent:
    schema_version: int
    sequence: int
    timestamp: str
    run_id: str
    request_id: str
    account: str
    role: str
    repository_key: str
    thread_id: str
    turn_id: str
    kind: str
    state: str
    method: str
    detail: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "account": self.account,
            "role": self.role,
            "repository_key": self.repository_key,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "state": self.state,
            "method": self.method,
            "detail": self.detail,
        }

    to_dict = as_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LiveEvent":
        return cls(
            schema_version=int(value["schema_version"]),
            sequence=int(value["sequence"]),
            timestamp=str(value["timestamp"]),
            run_id=str(value.get("run_id", "")),
            request_id=str(value.get("request_id", "")),
            account=str(value.get("account", "")),
            role=str(value.get("role", "")),
            repository_key=str(value.get("repository_key", "")),
            thread_id=str(value.get("thread_id", "")),
            turn_id=str(value.get("turn_id", "")),
            kind=str(value["kind"]),
            state=str(value["state"]),
            method=str(value["method"]),
            detail=value.get("detail"),
        )


def _record_from_payload(
    payload: Mapping[str, Any],
    *,
    sequence: int,
    run_id: str,
    request_id: str,
    account: str,
    role: str,
    repository_key: str,
    thread_id: str,
    turn_id: str,
    max_detail_bytes: int,
    max_record_bytes: int,
) -> dict[str, Any]:
    record = {
        "schema_version": LIVE_EVENT_SCHEMA_VERSION,
        "sequence": sequence,
        "timestamp": utc_timestamp(),
        "run_id": _safe_identity(run_id, ""),
        "request_id": _safe_identity(request_id, ""),
        "account": _safe_identity(account),
        "role": _safe_identity(role),
        "repository_key": repository_key,
        "thread_id": _safe_identity(thread_id, ""),
        "turn_id": _safe_identity(turn_id, ""),
        "kind": _safe_identity(payload.get("kind")),
        "state": _safe_identity(payload.get("state")),
        "method": _bounded_text(payload.get("method", ""), 256),
        "detail": _fit_detail(payload.get("detail"), max_detail_bytes),
    }
    if len(_json_bytes(record)) + 1 <= max_record_bytes:
        return record
    record["detail"] = {"truncated": True}
    if len(_json_bytes(record)) + 1 <= max_record_bytes:
        return record
    for key in ("method", "state", "kind", "thread_id", "turn_id", "run_id", "request_id"):
        record[key] = _bounded_text(record[key], 32)
    if len(_json_bytes(record)) + 1 > max_record_bytes:
        raise ValueError("Live event record limit is too small for the contract.")
    return record


def _thread_lock(path: Path) -> threading.RLock:
    key = path_identity_key(path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _journal_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock = _thread_lock(lock_path)
    with lock:
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_dicts(path: Path, *, max_record_bytes: int, max_records: int) -> list[dict[str, Any]]:
    if max_record_bytes <= 0 or max_records <= 0:
        raise ValueError("Live event journal limits must be positive.")
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        while True:
            raw = handle.readline(max_record_bytes + 2)
            if not raw:
                break
            if len(raw) > max_record_bytes + 1:
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(max_record_bytes + 2)
                continue
            raw = raw.rstrip(b"\r\n")
            if len(raw) > max_record_bytes:
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict) or int(value.get("schema_version")) != LIVE_EVENT_SCHEMA_VERSION:
                    continue
                int(value["sequence"])
                records.append(value)
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError, KeyError):
                # A crashed writer can leave a partial final line. Invalid lines
                # are ignored so a dashboard reader never sees untrusted data.
                continue
            if len(records) > max_records:
                records = records[-max_records:]
    return records[-max_records:]


def _write_dicts(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        with temporary.open("wb") as handle:
            for record in records:
                handle.write(_json_bytes(record) + b"\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class LiveEventJournal:
    """A bounded, account/repository-isolated JSONL journal for live events."""

    def __init__(
        self,
        runtime_root: PathLike,
        *,
        account: str,
        role: str,
        repository: PathLike | None = None,
        path: PathLike | None = None,
        run_id: str = "",
        request_id: str = "",
        max_records: int = DEFAULT_MAX_RECORDS,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_detail_bytes: int = DEFAULT_MAX_DETAIL_BYTES,
    ) -> None:
        self.runtime_root = _normalise_path(runtime_root)
        self.max_records = int(max_records)
        self.max_record_bytes = int(max_record_bytes)
        self.max_detail_bytes = int(max_detail_bytes)
        if self.max_records <= 0 or self.max_record_bytes <= 0 or self.max_detail_bytes <= 0:
            raise ValueError("Live event journal limits must be positive.")
        if path is None:
            if repository is None:
                raise ValueError("A repository is required when journal path is omitted.")
            path = journal_path(
                self.runtime_root,
                account=account,
                role=role,
                repository=repository,
            )
        self.path = _resolved_under(self.runtime_root, Path(path))
        if self.path == self.runtime_root or self.path.is_dir():
            raise ValueError("Live event journal path must be a file under the runtime root.")
        self.account = _safe_identity(account)
        self.role = _safe_identity(role)
        self.repository_key = repository_identity(repository) if repository is not None else ""
        self.run_id = _safe_identity(run_id, "")
        self.request_id = _safe_identity(request_id, "")

    def _append_payloads(self, payloads: list[dict[str, Any]], *, context: Mapping[str, str]) -> list[LiveEvent]:
        if not payloads:
            return []
        with _journal_lock(self.path):
            records = _read_dicts(
                self.path,
                max_record_bytes=self.max_record_bytes,
                max_records=self.max_records,
            )
            sequence = max((int(item["sequence"]) for item in records), default=0)
            added: list[dict[str, Any]] = []
            for payload in payloads:
                sequence += 1
                added.append(
                    _record_from_payload(
                        payload,
                        sequence=sequence,
                        run_id=context.get("run_id", self.run_id),
                        request_id=context.get("request_id", self.request_id),
                        account=context.get("account", self.account),
                        role=context.get("role", self.role),
                        repository_key=context.get("repository_key", self.repository_key),
                        thread_id=payload.get("thread_id", context.get("thread_id", "")),
                        turn_id=payload.get("turn_id", context.get("turn_id", "")),
                        max_detail_bytes=self.max_detail_bytes,
                        max_record_bytes=self.max_record_bytes,
                    )
                )
            all_records = (records + added)[-self.max_records :]
            _write_dicts(self.path, all_records)
        return [LiveEvent.from_dict(item) for item in added]

    def append(
        self,
        *,
        kind: str,
        state: str,
        method: str,
        detail: Any = None,
        run_id: str | None = None,
        request_id: str | None = None,
        thread_id: str = "",
        turn_id: str = "",
    ) -> LiveEvent:
        payload = {
            "kind": kind,
            "state": state,
            "method": method,
            "detail": detail,
            "thread_id": thread_id,
            "turn_id": turn_id,
        }
        context = {
            "run_id": self.run_id if run_id is None else run_id,
            "request_id": self.request_id if request_id is None else request_id,
            "account": self.account,
            "role": self.role,
            "repository_key": self.repository_key,
        }
        return self._append_payloads([payload], context=context)[0]

    def append_notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        account: str | None = None,
        role: str | None = None,
    ) -> list[LiveEvent]:
        normalized = normalize_notification(method, params)
        payloads: list[dict[str, Any]] = []
        output = _notification_text(params or {})
        if normalized["kind"] == "command_execution" and output:
            for detail in _output_chunks(output, self.max_detail_bytes):
                payloads.append(
                    {
                        **normalized,
                        "detail": detail,
                    }
                )
        else:
            payloads.append(normalized)
        context = {
            "run_id": self.run_id if run_id is None else run_id,
            "request_id": self.request_id if request_id is None else request_id,
            "account": self.account if account is None else account,
            "role": self.role if role is None else role,
            "repository_key": self.repository_key,
        }
        return self._append_payloads(payloads, context=context)

    def read(self) -> list[LiveEvent]:
        return [
            LiveEvent.from_dict(item)
            for item in _read_dicts(
                self.path,
                max_record_bytes=self.max_record_bytes,
                max_records=self.max_records,
            )
        ]


def read_journal(
    path: PathLike,
    *,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
) -> list[LiveEvent]:
    """Read a journal snapshot, ignoring a partial final record."""

    if int(max_records) <= 0 or int(max_record_bytes) <= 0:
        raise ValueError("Live event journal limits must be positive.")
    return [
        LiveEvent.from_dict(item)
        for item in _read_dicts(
            _normalise_path(path),
            max_record_bytes=int(max_record_bytes),
            max_records=int(max_records),
        )
    ]
