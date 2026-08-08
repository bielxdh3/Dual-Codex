from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .config import OrchestratorConfig
from .delegation import _pid_alive, _process_start_token
from .live_events import LiveEvent, journal_path, read_journal, repository_identity
from .paths import path_identity_key


MAX_READER_EVENTS = 128
MAX_SNAPSHOT_EVENTS = 2048
MAX_SSE_REPLAY = 64
MAX_ACTIVITY_ITEMS = 64
MAX_DETAIL_DEPTH = 5
MAX_DETAIL_ITEMS = 32
MAX_DETAIL_TEXT = 2048
LIVE_EVENT_FRESH_SECONDS = 30.0
_RUN_TERMINAL_STATES = {"completed", "complete", "failed", "failure", "cancelled", "canceled"}


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_DETAIL_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_DETAIL_TEXT]
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _bounded_value(child, depth=depth + 1)
            for key, child in list(value.items())[:MAX_DETAIL_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_value(child, depth=depth + 1) for child in list(value)[:MAX_DETAIL_ITEMS]]
    return str(value)[:MAX_DETAIL_TEXT]


def _detail(event: LiveEvent) -> dict[str, Any]:
    value = event.detail
    return value if isinstance(value, dict) else {}


def _text(value: Mapping[str, Any]) -> str | None:
    for key in ("text", "output", "stdout", "stderr", "delta", "chunk", "message"):
        child = value.get(key)
        if isinstance(child, str) and child:
            return child[:MAX_DETAIL_TEXT]
    return None


def _event_dict(event: LiveEvent) -> dict[str, Any]:
    value = event.as_dict()
    value["detail"] = _bounded_value(value.get("detail"))
    return value


def _activity_item(event: LiveEvent) -> dict[str, Any]:
    detail = _detail(event)
    item: dict[str, Any] = {
        "sequence": event.sequence,
        "state": event.state,
        "method": event.method,
    }
    text = _text(detail)
    if text:
        item["text"] = text
    if detail:
        item["detail"] = _bounded_value(detail)
    return item


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _elapsed_seconds(started_at: str | None, ended_at: str | None) -> float | None:
    started = _parse_timestamp(started_at)
    ended = _parse_timestamp(ended_at)
    if started is None or ended is None:
        return None
    return max(0.0, (ended - started).total_seconds())


class LiveExecutorReader:
    """Read the configured Executor journal without accepting filesystem input."""

    def __init__(self, config: OrchestratorConfig, repository: Path | None = None) -> None:
        self.config = config
        self.repository = (repository or config.repository).expanduser().resolve()

    @property
    def _path(self) -> Path:
        account = self.config.account_for_role("executor")
        return journal_path(
            self.config.runs_dir,
            account=account.name,
            role="executor",
            repository=self.repository,
        )

    def _read(self, limit: int = MAX_READER_EVENTS) -> list[LiveEvent]:
        return read_journal(
            self._path,
            max_records=min(
                self.config.live_event_journal_max_records,
                max(1, min(int(limit), MAX_SNAPSHOT_EVENTS)),
            ),
            max_record_bytes=self.config.live_event_journal_max_record_bytes,
        )

    def _lock_snapshot(self, run_id: str) -> dict[str, Any]:
        digest = hashlib.sha256(path_identity_key(self.repository).encode("utf-8")).hexdigest()[:24]
        path = self.config.runs_dir / ".locks" / f"{digest}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("active marker is not an object")
            pid = int(raw.get("pid", 0))
            try:
                live = _pid_alive(pid)
            except Exception:
                live = None
            process_start = raw.get("process_start")
            try:
                identity_valid = bool(
                    raw.get("run_id")
                    and raw.get("repository_key") == repository_identity(self.repository)
                    and raw.get("run_id") == run_id
                    and isinstance(process_start, str)
                    and process_start
                    and _process_start_token(pid) == process_start
                )
            except Exception:
                identity_valid = False
            return {
                "exists": True,
                "request_id": str(raw.get("request_id", "")),
                "run_id": str(raw.get("run_id", "")),
                "pid": pid,
                "live": live,
                "identity_valid": identity_valid,
            }
        except FileNotFoundError:
            return {"exists": False, "request_id": "", "run_id": "", "pid": 0, "live": False, "identity_valid": False}
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return {"exists": True, "request_id": "", "run_id": "", "pid": 0, "live": None, "identity_valid": False, "error": True}

    def events_after(self, cursor: int = 0, *, limit: int = MAX_SSE_REPLAY) -> list[dict[str, Any]]:
        cursor = int(cursor)
        if cursor < 0:
            raise ValueError("Event cursor must be non-negative.")
        events = [event for event in self._read() if event.sequence > cursor]
        return [_event_dict(event) for event in events[-max(1, min(limit, MAX_SSE_REPLAY)) :]]

    def snapshot(self) -> dict[str, Any]:
        events = self._read(MAX_SNAPSHOT_EVENTS)
        current = events[-1] if events else None
        run_id = next((event.run_id for event in reversed(events) if event.run_id), "")
        run_events = [event for event in events if not run_id or event.run_id == run_id]
        run_started = min(
            (event for event in run_events if event.kind == "run" and event.state == "started"),
            key=lambda event: event.sequence,
            default=None,
        )
        last_started = max(
            (event for event in run_events if event.kind == "turn" and event.state == "started"),
            key=lambda event: event.sequence,
            default=None,
        )
        last_completed = max(
            (
                event
                for event in run_events
                if event.kind == "turn" and event.state in {"completed", "complete"}
            ),
            key=lambda event: event.sequence,
            default=None,
        )
        last_failed = max(
            (
                event
                for event in run_events
                if event.kind == "error" or event.state in {"failed", "failure", "error"}
            ),
            key=lambda event: event.sequence,
            default=None,
        )
        run_terminal_events = [
            event
            for event in run_events
            if event.kind == "run" and event.state in _RUN_TERMINAL_STATES
        ]
        run_terminal = max(run_terminal_events, key=lambda event: event.sequence, default=None)
        turn_terminal = max(
            (event for event in (last_completed, last_failed) if event is not None),
            key=lambda event: event.sequence,
            default=None,
        )
        terminal = run_terminal or turn_terminal
        lock = self._lock_snapshot(run_id)
        latest_event_at = current.timestamp if current else None
        latest_event_time = _parse_timestamp(latest_event_at)
        fresh = bool(
            latest_event_time
            and (datetime.now(timezone.utc) - latest_event_time).total_seconds() <= LIVE_EVENT_FRESH_SECONDS
        )
        current_request_id = next((event.request_id for event in reversed(run_events) if event.request_id), "")
        newer_marker = bool(
            (lock.get("request_id") and current_request_id and lock["request_id"] != current_request_id)
            or (lock.get("run_id") and run_id and lock["run_id"] != run_id)
        )
        if newer_marker:
            terminal = None
        elif run_terminal is None and terminal and last_started and terminal.sequence < last_started.sequence:
            terminal = None

        stale_reason: str | None = None
        if terminal is not None:
            state = "COMPLETE" if terminal.state in {"completed", "complete"} else "FAILED"
        elif lock.get("exists") and not newer_marker and lock.get("live") is True and lock.get("identity_valid") is True and fresh:
            state = "WORKING"
        elif run_events:
            state = "DISCONNECTED" if lock.get("exists") and lock.get("live") is not False else "STALE"
            if newer_marker:
                stale_reason = "A newer request owns the active marker."
            elif lock.get("error"):
                stale_reason = "Active marker could not be read safely."
            elif lock.get("live") is True and not lock.get("identity_valid"):
                stale_reason = "Active marker identity is missing or does not match this run."
            elif lock.get("exists") and lock.get("live") is True:
                stale_reason = "Executor writer stopped publishing fresh events."
            else:
                stale_reason = "Executor writer is no longer alive and emitted no terminal event."
        else:
            state = "IDLE"

        model = reasoning_effort = service_tier = None
        plan: Any = None
        token_usage: Any = None
        for event in reversed(run_events):
            detail = _detail(event)
            turn = detail.get("turn") if isinstance(detail.get("turn"), dict) else {}
            sources = (turn, detail)
            for source in sources:
                if model is None and isinstance(source.get("model"), str):
                    model = source["model"][:MAX_DETAIL_TEXT]
                if reasoning_effort is None:
                    value = source.get("reasoningEffort", source.get("reasoning_effort"))
                    if isinstance(value, str):
                        reasoning_effort = value[:MAX_DETAIL_TEXT]
                if service_tier is None:
                    value = source.get("serviceTier", source.get("service_tier", source.get("tier")))
                    if isinstance(value, str):
                        service_tier = value[:MAX_DETAIL_TEXT]
                if plan is None and "plan" in source:
                    plan = _bounded_value(source["plan"])
            if token_usage is None and event.kind == "token_usage":
                token_usage = _bounded_value(
                    detail.get("tokenUsage") or detail.get("usage") or detail
                )
            if model is not None and reasoning_effort is not None and service_tier is not None and token_usage is not None:
                break

        activity: dict[str, list[dict[str, Any]]] = {
            "commands": [],
            "outputs": [],
            "file_changes": [],
            "diffs": [],
            "messages": [],
        }
        for event in run_events[-MAX_ACTIVITY_ITEMS:]:
            item = _activity_item(event)
            detail = _detail(event)
            if event.kind == "command_execution":
                activity["commands"].append(item)
                if event.state == "output" or _text(detail):
                    activity["outputs"].append(item)
            elif event.kind == "file_change":
                activity["file_changes"].append(item)
                if any(key in detail for key in ("diff", "patch")):
                    activity["diffs"].append(item)
            elif event.kind == "agent_message":
                activity["messages"].append(item)
            elif "diff" in event.method.casefold() or "patch" in detail:
                activity["diffs"].append(item)

        account = self.config.account_for_role("executor").name
        started_event = run_started or last_started
        started_detail = _detail(started_event) if started_event else {}
        started_at = started_detail.get("started_at") if isinstance(started_detail.get("started_at"), str) else (started_event.timestamp if started_event else None)
        terminal_detail = _detail(terminal) if terminal else {}
        terminal_ended_at = terminal_detail.get("ended_at") if isinstance(terminal_detail.get("ended_at"), str) else (terminal.timestamp if terminal else None)
        ended_at = terminal_ended_at if terminal else (current.timestamp if state in {"STALE", "DISCONNECTED"} and current else None)
        elapsed_end = ended_at if state != "WORKING" else datetime.now(timezone.utc).isoformat()
        latest_thread_id = next((event.thread_id for event in reversed(run_events) if event.thread_id), "")
        latest_turn_id = next((event.turn_id for event in reversed(run_events) if event.turn_id), "")
        return {
            "schema_version": 1,
            "state": state,
            "cursor": current.sequence if current else 0,
            "account": account,
            "role": "executor",
            "repository_key": current.repository_key if current else repository_identity(self.repository),
            "request_id": current_request_id or None,
            "run_id": run_id or None,
            "thread_id": latest_thread_id or None,
            "turn_id": latest_turn_id or None,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "service_tier": service_tier,
            "plan": plan,
            "token_usage": token_usage,
            "activity": activity,
            "events": [_event_dict(event) for event in events[-MAX_READER_EVENTS:]],
            "started_at": started_at,
            "completed_at": terminal.timestamp if terminal else None,
            "ended_at": ended_at,
            "elapsed_seconds": _elapsed_seconds(started_at, elapsed_end),
            "last_event_at": latest_event_at,
            "stale_reason": stale_reason,
            "updated_at": latest_event_at,
        }
