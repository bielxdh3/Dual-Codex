from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterable

from .config import AgentConfig


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    metadata: dict[str, str] = field(default_factory=dict)


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
    progress: Callable[[str], None] | None = None,
    progress_interval: float = 15.0,
) -> CommandResult:
    display_args = [str(part) for part in command]
    process_args, use_shell = _prepare_command(display_args.copy())

    if progress is None:
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
    else:
        completed = _run_with_progress(
            process_args,
            cwd=cwd,
            env=env,
            stdin=stdin,
            shell=use_shell,
            progress=progress,
            progress_interval=progress_interval,
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


def _run_with_progress(
    process_args: list[str] | str,
    *,
    cwd: Path,
    env: dict[str, str] | None,
    stdin: str | None,
    shell: bool,
    progress: Callable[[str], None],
    progress_interval: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        process_args,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
    )
    stop = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(progress_interval):
            progress(f"executor still running ({time.monotonic() - started:.1f}s elapsed)")

    watcher = threading.Thread(target=heartbeat, name="dual-codex-progress", daemon=True)
    watcher.start()
    try:
        stdout, stderr = process.communicate(input=stdin)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        stop.set()
        watcher.join(timeout=1)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def codex_environment(agent: AgentConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(agent.codex_home)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY"):
        env.pop(name, None)
    return env
