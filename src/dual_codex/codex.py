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
        add_dirs = (task_artifact_path.parent.resolve(),) if task_artifact_path is not None else ()
        session = manager.ensure(
            session_id=session_id,
            agent=agent,
            role="executor" if agent.sandbox == "workspace-write" else "architect",
            repository=repository,
            approval_policy="never" if agent.sandbox == "workspace-write" else "on-request",
            add_dirs=add_dirs,
        )
        cursor = manager.turn_cursor(session.session_id)
        turn_start = manager.send(session.session_id, prompt)
        result = manager.wait_for_turn(session.session_id, cursor=cursor, progress=progress)
        assistant = result.get("assistant", "")
        report = _report_from_message(assistant)
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
