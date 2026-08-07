from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from .config import AgentConfig


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def _prepare_command(args: list[str]) -> tuple[list[str] | str, bool]:
    """Make npm-installed .cmd/.bat launchers executable on Windows."""
    if os.name != "nt" or not args:
        return args, False

    resolved = shutil.which(args[0])
    if resolved:
        args = [resolved, *args[1:]]

    if Path(args[0]).suffix.lower() in {".cmd", ".bat"}:
        return subprocess.list2cmdline(args), True

    return args, False


def run_command(
    command: Iterable[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    check: bool = True,
) -> CommandResult:
    display_args = [str(part) for part in command]
    process_args, use_shell = _prepare_command(display_args.copy())

    completed = subprocess.run(
        process_args,
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=use_shell,
    )
    result = CommandResult(
        display_args,
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )
    if check and completed.returncode != 0:
        raise CommandError(
            f"Command failed ({completed.returncode}): {' '.join(display_args)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return result


def codex_environment(agent: AgentConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(agent.codex_home)
    return env
