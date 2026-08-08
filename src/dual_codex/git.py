from __future__ import annotations

from pathlib import Path

from .process import run_command


def ensure_git_repository(repository: Path) -> None:
    result = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repository)
    if result.stdout.strip() != "true":
        raise RuntimeError(f"Not a Git work tree: {repository}")


def status_porcelain(repository: Path) -> str:
    return run_command(["git", "status", "--porcelain=v1"], cwd=repository).stdout


def head_revision(repository: Path) -> str:
    return run_command(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()


def status_and_diff(repository: Path) -> str:
    status = run_command(["git", "status", "--short"], cwd=repository).stdout
    unstaged = run_command(["git", "diff", "--no-ext-diff"], cwd=repository).stdout
    staged = run_command(["git", "diff", "--cached", "--no-ext-diff"], cwd=repository).stdout
    return (
        "## git status --short\n"
        f"{status or '(clean)'}\n"
        "## git diff\n"
        f"{unstaged or '(no unstaged diff)'}\n"
        "## git diff --cached\n"
        f"{staged or '(no staged diff)'}\n"
    )
