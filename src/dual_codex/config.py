from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class AgentConfig:
    codex_home: Path
    model: str
    reasoning_effort: str
    sandbox: str


@dataclass(frozen=True)
class OrchestratorConfig:
    repository: Path
    runs_dir: Path
    max_correction_cycles: int
    require_clean_git: bool
    codex_command: str
    architect: AgentConfig
    executor: AgentConfig
    project_root: Path


def _agent(raw: dict, base: Path) -> AgentConfig:
    home = Path(raw["codex_home"]).expanduser()
    if not home.is_absolute():
        home = (base / home).resolve()
    return AgentConfig(
        codex_home=home,
        model=str(raw.get("model", "")).strip(),
        reasoning_effort=str(raw.get("reasoning_effort", "high")).strip(),
        sandbox=str(raw["sandbox"]).strip(),
    )


def load_config(path: Path) -> OrchestratorConfig:
    path = path.expanduser().resolve()
    # utf-8-sig accepts both ordinary UTF-8 and Windows-created UTF-8 files
    # that contain a byte-order mark.
    raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))

    base = path.parent
    project_root = Path(__file__).resolve().parents[2]
    orch = raw["orchestrator"]
    repository = Path(orch["repository"]).expanduser()
    if not repository.is_absolute():
        repository = (base / repository).resolve()
    runs_dir = Path(orch.get("runs_dir", "runs")).expanduser()
    if not runs_dir.is_absolute():
        runs_dir = (base / runs_dir).resolve()

    return OrchestratorConfig(
        repository=repository,
        runs_dir=runs_dir,
        max_correction_cycles=int(orch.get("max_correction_cycles", 1)),
        require_clean_git=bool(orch.get("require_clean_git", True)),
        codex_command=str(orch.get("codex_command", "codex")),
        architect=_agent(raw["architect"], base),
        executor=_agent(raw["executor"], base),
        project_root=project_root,
    )
