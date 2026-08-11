from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from .config import AgentConfig


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    metadata: dict[str, Any] = field(default_factory=dict)


_CMD_CONTROL_CHARACTERS = frozenset("&|<>^()%!\r\n")


def _prepare_command(args: list[str]) -> list[str] | str:
    """Run npm .cmd/.bat shims through a constrained command processor."""
    if os.name != "nt" or not args:
        return args

    resolved = shutil.which(args[0])
    if resolved:
        args = [resolved, *args[1:]]

    if Path(args[0]).suffix.lower() in {".cmd", ".bat"}:
        if not resolved:
            raise ValueError("Windows command shim must resolve to an existing .cmd or .bat file")
        unsafe = next((arg for arg in args if any(char in arg for char in _CMD_CONTROL_CHARACTERS)), None)
        if unsafe is not None:
            raise ValueError("Windows command shim arguments cannot contain cmd.exe control characters")
        command_processor = os.environ.get(
            "COMSPEC",
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
        )
        # .cmd/.bat files require cmd.exe; keep shell=False and expose only a
        # validated argv string to the minimum compatibility shim.
        command_line = subprocess.list2cmdline(args)
        return f'{subprocess.list2cmdline([command_processor])} /d /s /v:off /c "{command_line}"'

    return args


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
    process_args = _prepare_command(display_args.copy())

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
            shell=False,
        )
    else:
        completed = _run_with_progress(
            process_args,
            cwd=cwd,
            env=env,
            stdin=stdin,
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
        shell=False,
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
