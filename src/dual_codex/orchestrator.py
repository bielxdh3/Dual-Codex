from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .codex import run_codex_exec
from .config import OrchestratorConfig
from .git import ensure_git_repository, status_and_diff, status_porcelain
from .report import dump_json, load_json, render_markdown


@dataclass(frozen=True)
class RunOutcome:
    run_dir: Path
    verdict: str
    correction_cycles: int


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _prompt(config: OrchestratorConfig, name: str, **values: str) -> str:
    template = _read(config.project_root / "prompts" / name)
    return template.format(**values)


def _schema(config: OrchestratorConfig, name: str) -> Path:
    return config.project_root / "schemas" / name


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def execute(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> RunOutcome:
    task_file = task_file.expanduser().resolve()
    task = _read(task_file).strip()
    if not task:
        raise ValueError("Task file is empty")

    ensure_git_repository(config.repository)
    if config.require_clean_git and status_porcelain(config.repository).strip():
        raise RuntimeError(
            "Repository has uncommitted changes. Commit/stash them or set "
            "require_clean_git = false explicitly."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = config.runs_dir / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "task.md").write_text(task + "\n", encoding="utf-8")

    _emit(progress, "[1/3] Architect is inspecting the repository and creating a plan...")
    plan_path = run_dir / "plan.json"
    run_codex_exec(
        codex_command=config.codex_command,
        agent=config.architect,
        repository=config.repository,
        prompt=_prompt(config, "architect.txt", task=task),
        output_path=plan_path,
        schema_path=_schema(config, "plan.schema.json"),
    )
    plan = load_json(plan_path)

    _emit(progress, "[2/3] Executor is implementing the approved plan and running checks...")
    implementation_path = run_dir / "implementation.json"
    run_codex_exec(
        codex_command=config.codex_command,
        agent=config.executor,
        repository=config.repository,
        prompt=_prompt(config, "executor.txt", task=task, plan=dump_json(plan)),
        output_path=implementation_path,
        schema_path=_schema(config, "implementation.schema.json"),
    )
    implementation = load_json(implementation_path)

    correction_cycles = 0
    while True:
        _emit(progress, f"[3/3] Architect is reviewing the Git diff (round {correction_cycles + 1})...")
        diff_text = status_and_diff(config.repository)
        (run_dir / f"diff-{correction_cycles}.md").write_text(diff_text, encoding="utf-8")
        review_path = run_dir / f"review-{correction_cycles}.json"
        run_codex_exec(
            codex_command=config.codex_command,
            agent=config.architect,
            repository=config.repository,
            prompt=_prompt(
                config,
                "reviewer.txt",
                task=task,
                plan=dump_json(plan),
                implementation=dump_json(implementation),
                diff=diff_text,
            ),
            output_path=review_path,
            schema_path=_schema(config, "review.schema.json"),
        )
        review = load_json(review_path)
        if review["verdict"] == "approved":
            _emit(progress, "Review approved.")
            break
        if correction_cycles >= config.max_correction_cycles:
            _emit(progress, "Review requested changes, but the correction limit was reached.")
            break

        correction_cycles += 1
        _emit(
            progress,
            f"[correction {correction_cycles}/{config.max_correction_cycles}] "
            "Executor is addressing review findings...",
        )
        implementation_path = run_dir / f"correction-{correction_cycles}.json"
        run_codex_exec(
            codex_command=config.codex_command,
            agent=config.executor,
            repository=config.repository,
            prompt=_prompt(
                config,
                "correction.txt",
                task=task,
                plan=dump_json(plan),
                review=dump_json(review),
            ),
            output_path=implementation_path,
            schema_path=_schema(config, "implementation.schema.json"),
        )
        implementation = load_json(implementation_path)

    report = render_markdown(
        task_file=task_file,
        plan=plan,
        implementation=implementation,
        review=review,
        correction_cycles=correction_cycles,
    )
    (run_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return RunOutcome(run_dir=run_dir, verdict=review["verdict"], correction_cycles=correction_cycles)
