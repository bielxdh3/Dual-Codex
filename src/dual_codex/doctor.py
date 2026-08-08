from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from .config import ConfigError, OrchestratorConfig
from .git import ensure_git_repository
from .process import run_command
from .registry import abbreviate_path, login_status


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    details: str


def _auth_check(name: str, home: Path) -> Check:
    profile_config = home / "config.toml"
    if not home.exists():
        return Check(name, False, "CODEX_HOME does not exist")
    if not profile_config.exists():
        return Check(name, False, "Missing profile config.toml")
    config_text = profile_config.read_bytes().decode("utf-8-sig", errors="replace")
    if 'cli_auth_credentials_store = "file"' not in config_text:
        return Check(name, False, 'Profile config must set cli_auth_credentials_store = "file"')
    if not (home / "auth.json").exists():
        return Check(name, False, "Not authenticated")
    return Check(name, True, abbreviate_path(home))


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

    for name, account in config.accounts.items():
        checks.append(_auth_check(f"account {name}", account.codex_home))

    for role in ("architect", "executor", "reviewer"):
        try:
            account = config.account_for_role(role)
            checks.append(Check(f"role {role}", True, account.name))
        except ConfigError as exc:
            checks.append(Check(f"role {role}", False, str(exc)))

    try:
        ensure_git_repository(config.repository)
        checks.append(Check("repository", True, str(config.repository)))
    except Exception as exc:
        checks.append(Check("repository", False, str(exc)))

    if executable:
        for account in config.accounts.values():
            if (account.codex_home / "auth.json").exists():
                status = login_status(config, account)
                checks.append(Check(f"login {account.name}", status == "OK", status))

    return checks
