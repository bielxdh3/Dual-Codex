from __future__ import annotations

from pathlib import Path

from .config import AgentConfig
from .process import CommandResult, codex_environment, run_command


def run_codex_exec(
    *,
    codex_command: str,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    schema_path: Path,
) -> CommandResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        codex_command,
        "exec",
        "--cd",
        str(repository),
        "--sandbox",
        agent.sandbox,
        "--output-last-message",
        str(output_path),
        "--output-schema",
        str(schema_path),
        "-c",
        f'model_reasoning_effort="{agent.reasoning_effort}"',
    ]
    if agent.model:
        command.extend(["--model", agent.model])
    command.append("-")
    return run_command(
        command,
        cwd=repository,
        env=codex_environment(agent),
        stdin=prompt,
    )
