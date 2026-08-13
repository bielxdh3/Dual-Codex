from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .config import AgentConfig
from .process import CommandResult, codex_environment, run_command


def run_codex_app_server(**kwargs) -> CommandResult:
    from .app_server import run_codex_app_server as _run_codex_app_server

    return _run_codex_app_server(**kwargs)


def _report_from_message(message: str) -> dict | None:
    from .app_server import _normalise_report

    normalised = _normalise_report(message)
    candidates = [normalised.strip()]
    if normalised != message:
        candidates.append(message.strip())
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


def run_codex_exec(
    *,
    codex_command: str,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    schema_path: Path,
    check: bool = True,
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        codex_command,
        "exec",
        "--cd",
        str(repository),
        "--sandbox",
        agent.sandbox,
        "-c",
        f'sandbox_mode="{agent.sandbox}"',
        "--output-last-message",
        str(output_path),
        "--output-schema",
        str(schema_path),
        "-c",
        f'model_reasoning_effort="{agent.reasoning_effort}"',
    ]
    if agent.sandbox == "workspace-write":
        command.extend(["--add-dir", str(repository)])
    if agent.model:
        command.extend(["--model", agent.model])
    command.append("-")
    return run_command(
        command,
        cwd=repository,
        env=codex_environment(agent),
        stdin=prompt,
        check=check,
        progress=progress,
    )


def run_codex_terminal(
    *,
    config,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    session_id: str,
    task_artifact_path: Path | None = None,
    task_sha256: str = "",
    reuse_existing: bool = False,
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    from .terminal import TerminalError, TerminalManager

    transport = "file" if task_artifact_path is not None else "inline"
    artifact = str(task_artifact_path.resolve()) if task_artifact_path is not None else ""
    metadata = {
        "terminal_session_id": session_id,
        "task_transport": transport,
        "task_artifact": artifact,
        "task_sha256": task_sha256,
        "reuse_existing": reuse_existing,
    }
    if task_artifact_path is not None and not task_artifact_path.is_file():
        return CommandResult(
            ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
            1,
            "",
            f"Task artifact does not exist: {task_artifact_path}",
            metadata,
        )
    manager = TerminalManager(config)
    try:
        if reuse_existing:
            try:
                manager._load(session_id)
                current = manager.status(session_id)
            except TerminalError as exc:
                return CommandResult(
                    ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
                    1,
                    "",
                    f"Strict reuse requires an existing terminal session: {exc}",
                    metadata,
                )
            if current.get("state") != "running" or current.get("alive") is False:
                return CommandResult(
                    ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
                    1,
                    "",
                    f"Strict reuse requires a running terminal session; observed state '{current.get('state', 'unknown')}'.",
                    metadata,
                )
            # The pre-opened TUI may not have been launched with the task-artifact
            # directory. Reuse it without requesting a new add-dir or spawning a
            # replacement; the short control message still points at the immutable
            # artifact captured by the orchestrator.
            add_dirs = ()
        else:
            add_dirs = (task_artifact_path.parent.resolve(),) if task_artifact_path is not None else ()
        ensure_kwargs = {
            "session_id": session_id,
            "agent": agent,
            "role": "executor" if agent.sandbox == "workspace-write" else "architect",
            "repository": repository,
            "approval_policy": "never" if agent.sandbox == "workspace-write" else "on-request",
            "add_dirs": add_dirs,
            "reuse_existing": reuse_existing,
        }
        if not reuse_existing:
            ensure_kwargs["visible"] = agent.sandbox == "workspace-write"
        session = manager.ensure(
            **ensure_kwargs,
        )
        cursor = manager.turn_cursor(session.session_id)
        lease_owner = manager.begin_automation_turn(session.session_id)
        try:
            turn_start = manager.send(session.session_id, prompt, lease_owner=lease_owner)
            result = manager.wait_for_turn(session.session_id, cursor=cursor, progress=progress)
        finally:
            manager.release_input_lease(session.session_id, lease_owner)
        assistant = result.get("assistant", "")
        report = _report_from_message(assistant)
        status_snapshot = manager.status(session.session_id)
        readiness = getattr(manager, "_last_readiness_diagnostics", {})
        if not isinstance(readiness, dict):
            readiness = {}
        terminal_pid = int(status_snapshot.get("pid") or session.pid)
        host_pid = int(status_snapshot.get("host_pid") or session.pid)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False) if report is not None else assistant,
            encoding="utf-8",
        )
        return CommandResult(
            ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
            0,
            assistant,
            "",
            {
                **metadata,
                "terminal_session_id": session.session_id,
                "codex_session_id": result.get("session_id", ""),
                "terminal_turn_start": json.dumps(turn_start, ensure_ascii=True),
                "reuse_existing": reuse_existing,
                "reuse_provenance": {
                    "mode": "strict_existing" if reuse_existing else "reuse_or_start",
                    "terminal_session_id": session.session_id,
                    "session_record": getattr(session, "session_file", ""),
                    "session_host_pid": session.pid,
                    "session_process_epoch": getattr(session, "process_epoch", ""),
                    "session_process_start_identity": getattr(session, "process_start_identity", ""),
                    "terminal_pid": terminal_pid,
                    "host_pid": host_pid,
                    "account": session.account,
                    "account_label": getattr(session, "label", ""),
                    "role": session.role,
                    "repository_identity": getattr(session, "repository_identity", "") or str(session.repository),
                    "codex_home_identity": getattr(session, "codex_home_identity", "") or str(session.codex_home),
                    "codex_session_id": result.get("session_id", ""),
                    "repository": str(session.repository),
                    "codex_home": str(session.codex_home),
                    "pid": session.pid,
                    "host_pid": host_pid,
                    "process_started_at": getattr(session, "process_started_at", 0.0),
                    "pipe": getattr(session, "pipe", ""),
                    "viewer_attached": bool(
                        isinstance(status_snapshot.get("viewer"), dict)
                        and status_snapshot["viewer"].get("attached") is True
                    ),
                    "viewer_pid": int(status_snapshot.get("viewer_pid") or getattr(session, "viewer_pid", 0) or 0),
                    "viewer_epoch": str(
                        status_snapshot.get("viewer_epoch")
                        or getattr(session, "viewer_epoch", "")
                        or ""
                    ),
                    "target_model": str(readiness.get("target_model") or "unknown"),
                    "target_reasoning": str(readiness.get("target_reasoning") or "unknown"),
                    "model_provenance": str(readiness.get("model_provenance") or "unavailable"),
                    "reasoning_provenance": str(
                        readiness.get("reasoning_provenance") or "unavailable"
                    ),
                },
            },
        )
    except TerminalError as exc:
        return CommandResult(
            ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
            1,
            "",
            str(exc),
            metadata,
        )
