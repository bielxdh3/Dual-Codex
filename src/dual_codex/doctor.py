from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from .config import OrchestratorConfig
from .git import ensure_git_repository
from .process import codex_environment, run_command


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    details: str


def _auth_check(name: str, home: Path) -> Check:
    auth = home / "auth.json"
    config = home / "config.toml"
    if not home.exists():
        return Check(name, False, f"CODEX_HOME does not exist: {home}")
    if not config.exists():
        return Check(name, False, f"Missing config.toml in {home}")
    config_text = config.read_text(encoding="utf-8", errors="replace")
    if 'cli_auth_credentials_store = "file"' not in config_text:
        return Check(name, False, "config.toml must set cli_auth_credentials_store = \"file\"")
    if not auth.exists():
        return Check(name, False, f"Missing auth.json. Run login using CODEX_HOME={home}")
    return Check(name, True, str(home))


def run_doctor(config: OrchestratorConfig) -> list[Check]:
    checks: list[Check] = []
    executable = shutil.which(config.codex_command)
    checks.append(Check("codex executable", bool(executable), executable or "not found in PATH"))

    if executable:
        try:
            result = run_command([config.codex_command, "--version"], cwd=config.project_root)
            checks.append(Check("codex version", True, result.stdout.strip() or result.stderr.strip()))
        except Exception as exc:  # diagnostic path
            checks.append(Check("codex version", False, str(exc)))

    checks.append(_auth_check("architect profile", config.architect.codex_home))
    checks.append(_auth_check("executor profile", config.executor.codex_home))

    try:
        ensure_git_repository(config.repository)
        checks.append(Check("repository", True, str(config.repository)))
    except Exception as exc:
        checks.append(Check("repository", False, str(exc)))

    for label, agent in (("architect login", config.architect), ("executor login", config.executor)):
        if executable and (agent.codex_home / "auth.json").exists():
            try:
                result = run_command(
                    [config.codex_command, "login", "status"],
                    cwd=config.project_root,
                    env=codex_environment(agent),
                )
                checks.append(Check(label, True, result.stdout.strip() or result.stderr.strip()))
            except Exception as exc:
                checks.append(Check(label, False, str(exc)))

    return checks
