from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .config import OrchestratorConfig
from .live_events import LiveEvent, journal_path, read_journal, repository_identity


MAX_READER_EVENTS = 128
MAX_SNAPSHOT_EVENTS = 2048
MAX_SSE_REPLAY = 64
MAX_ACTIVITY_ITEMS = 64
MAX_DETAIL_DEPTH = 5
MAX_DETAIL_ITEMS = 32
MAX_DETAIL_TEXT = 2048


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

    def events_after(self, cursor: int = 0, *, limit: int = MAX_SSE_REPLAY) -> list[dict[str, Any]]:
        cursor = int(cursor)
        if cursor < 0:
            raise ValueError("Event cursor must be non-negative.")
        events = [event for event in self._read() if event.sequence > cursor]
        return [_event_dict(event) for event in events[-max(1, min(limit, MAX_SSE_REPLAY)) :]]

    def snapshot(self) -> dict[str, Any]:
        events = self._read(MAX_SNAPSHOT_EVENTS)
        current = events[-1] if events else None
        run_id = current.run_id if current else ""
        run_events = [event for event in events if not run_id or event.run_id == run_id]
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
        if last_failed and last_failed.sequence > max(
            last_started.sequence if last_started else 0,
            last_completed.sequence if last_completed else 0,
        ):
            state = "FAILED"
        elif last_started and last_started.sequence > (last_completed.sequence if last_completed else 0):
            state = "WORKING"
        elif last_completed:
            state = "COMPLETE"
        else:
            state = "IDLE"

        terminal_events = [event for event in (last_completed, last_failed) if event is not None]
        terminal = max(terminal_events, key=lambda event: event.sequence, default=None)
        if last_started and terminal and terminal.sequence < last_started.sequence:
            terminal = None

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
        return {
            "schema_version": 1,
            "state": state,
            "cursor": current.sequence if current else 0,
            "account": account,
            "role": "executor",
            "repository_key": current.repository_key if current else repository_identity(self.repository),
            "request_id": current.request_id if current and current.request_id else None,
            "run_id": run_id or None,
            "thread_id": current.thread_id if current and current.thread_id else None,
            "turn_id": current.turn_id if current and current.turn_id else None,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "service_tier": service_tier,
            "plan": plan,
            "token_usage": token_usage,
            "activity": activity,
            "events": [_event_dict(event) for event in events[-MAX_READER_EVENTS:]],
            "started_at": last_started.timestamp if last_started else None,
            "completed_at": terminal.timestamp if terminal else None,
            "updated_at": current.timestamp if current else None,
        }
