from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Callable, Mapping

from .config import (
    AccountConfig,
    ConfigError,
    OrchestratorConfig,
    SUPPORTED_ROLES,
    _account,
    is_legacy_raw,
    load_raw_config,
    validate_account_name,
    validate_setting_value,
    validate_role_name,
)
from .process import codex_environment, run_command


InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]
_SECTION = re.compile(r"^\s*\[([^]]+)\]\s*(?:#.*)?$")


def abbreviate_path(path: Path) -> str:
    value = str(path)
    home = str(Path.home())
    if any(value.casefold().startswith(home.casefold() + separator) for separator in ("\\", "/")):
        tail = value[len(home) :].lstrip("\\/").splitlines()[0]
        parts = re.split(r"[\\/]", tail)
        value = "~\\...\\" + "\\".join(parts[-2:]) if parts else "~"
    else:
        parts = re.split(r"[\\/]", value)
        if len(parts) > 3:
            value = "...\\" + "\\".join(parts[-2:])
    if len(value) > 72:
        return "..." + value[-69:]
    return value


def roles_for_account(roles: Mapping[str, str], account_name: str) -> list[str]:
    order = {role: index for index, role in enumerate(SUPPORTED_ROLES)}
    return sorted(
        [role for role, account in roles.items() if account == account_name],
        key=lambda role: (order.get(role, len(order)), role),
    )


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _managed_section(header: str) -> bool:
    section = header.strip()
    return (
        section in {"accounts", "roles", "architect", "executor"}
        or section.startswith("accounts.")
    )


def _without_managed_sections(text: str) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        match = _SECTION.match(line.rstrip("\r\n"))
        if match:
            skipping = _managed_section(match.group(1))
        if not skipping:
            kept.append(line)
    return "".join(kept).rstrip()


def _registry_block(accounts: Mapping[str, AccountConfig], roles: Mapping[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(accounts):
        account = accounts[name]
        lines.extend(
            [
                f"[accounts.{_toml_string(name)}]",
                f"label = {_toml_string(account.label)}",
                f"codex_home = {_toml_string(str(account.codex_home))}",
                f"model = {_toml_string(account.model)}",
                f"reasoning_effort = {_toml_string(account.reasoning_effort)}",
                f"backend = {_toml_string(account.backend)}",
                f"service_tier = {_toml_string(account.service_tier)}",
                "",
            ]
        )
    lines.append("[roles]")
    for role in sorted(roles):
        account_name = roles[role]
        if account_name:
            lines.append(f"{_toml_key(role)} = {_toml_string(account_name)}")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_registry_config(
    path: Path,
    accounts: Mapping[str, AccountConfig],
    roles: Mapping[str, str],
) -> None:
    """Update registry sections while retaining unrelated configuration text."""
    original = path.read_bytes().decode("utf-8-sig")
    preserved = _without_managed_sections(original)
    prefix = f"{preserved}\n\n" if preserved else ""
    _atomic_write(path, prefix + _registry_block(accounts, roles))


def _profile_config_text(path: Path) -> str:
    if path.exists():
        text = path.read_bytes().decode("utf-8-sig")
    else:
        text = ""
    lines = text.splitlines()
    setting = 'cli_auth_credentials_store = "file"'
    for index, line in enumerate(lines):
        if re.match(r"^\s*cli_auth_credentials_store\s*=", line):
            lines[index] = setting
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(setting)
    return "\n".join(lines).rstrip() + "\n"


def ensure_codex_profile(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    _atomic_write(home / "config.toml", _profile_config_text(home / "config.toml"))


def _agent_for_status(account: AccountConfig):
    from .config import AgentConfig

    return AgentConfig(
        codex_home=account.codex_home,
        model=account.model,
        reasoning_effort=account.reasoning_effort,
        sandbox="read-only",
        account_name=account.name,
        label=account.label,
        backend=account.backend,
        service_tier=account.service_tier,
    )


def login_status(config: OrchestratorConfig, account: AccountConfig) -> str:
    """Check login status without reading or displaying authentication data."""
    if shutil.which(config.codex_command) is None and not Path(config.codex_command).exists():
        return "UNKNOWN"
    try:
        result = run_command(
            [config.codex_command, "login", "status"],
            cwd=config.project_root,
            env=codex_environment(_agent_for_status(account)),
            check=False,
        )
    except OSError:
        return "UNKNOWN"
    return "OK" if result.returncode == 0 else "NOT LOGGED IN"


def _verify_login(config: OrchestratorConfig, account: AccountConfig) -> None:
    status = login_status(config, account)
    if status != "OK":
        raise RuntimeError(f"Codex login status failed for account '{account.name}'.")


def _run_login(config: OrchestratorConfig, account: AccountConfig) -> None:
    try:
        run_command(
            [config.codex_command, "login"],
            cwd=config.project_root,
            env=codex_environment(_agent_for_status(account)),
        )
    except Exception as exc:
        raise RuntimeError(f"Codex login failed for account '{account.name}'.") from exc
    _verify_login(config, account)


def _resolve_home(config: OrchestratorConfig, value: str | None, name: str) -> Path:
    if value is None:
        return (Path.home() / "CodexProfiles" / name).resolve()
    home = Path(value).expanduser()
    return home if home.is_absolute() else (config.config_path.parent / home).resolve()


def add_account(
    config: OrchestratorConfig,
    name: str,
    *,
    label: str = "",
    codex_home: str | None = None,
    model: str = "",
    reasoning_effort: str = "high",
    roles: list[str] | None = None,
    output: OutputFn = print,
) -> AccountConfig:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before adding accounts.")
    name = validate_account_name(name)
    if name in config.accounts:
        raise ConfigError(f"Account '{name}' is already registered.")
    requested_roles = [validate_role_name(role) for role in (roles or [])]
    home = _resolve_home(config, codex_home, name)
    if (home / "auth.json").exists():
        raise ConfigError(
            f"An unregistered auth file already exists for '{name}'. "
            "Use account login only after registering it safely."
        )

    account = AccountConfig(
        name=name,
        label=label.strip(),
        codex_home=home,
        model=model.strip(),
        reasoning_effort=reasoning_effort.strip() or "high",
        backend="windows",
        service_tier="",
    )
    output(f"Account: {account.name}")
    output(f"Label: {account.label or '(none)'}")
    output(f"Authenticating with CODEX_HOME: {abbreviate_path(account.codex_home)}")
    ensure_codex_profile(account.codex_home)
    _run_login(config, account)

    accounts = dict(config.accounts)
    accounts[name] = account
    assignments = dict(config.roles)
    for role in requested_roles:
        assignments[role] = name
    write_registry_config(config.config_path, accounts, assignments)
    return account


def login_account(
    config: OrchestratorConfig,
    name: str,
    *,
    assume_yes: bool = False,
    input_fn: InputFn = input,
    output: OutputFn = print,
) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before using account login.")
    account = config.accounts.get(validate_account_name(name))
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    if (account.codex_home / "auth.json").exists() and not assume_yes:
        answer = input_fn(
            f"Replace the existing login for account '{account.name}' "
            f"({account.label or 'no label'})? [y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            raise RuntimeError("Login cancelled.")
    output(f"Account: {account.name}")
    output(f"Label: {account.label or '(none)'}")
    output(f"CODEX_HOME: {abbreviate_path(account.codex_home)}")
    _run_login(config, account)


def rename_account(config: OrchestratorConfig, old_name: str, new_name: str) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before renaming accounts.")
    old_name = validate_account_name(old_name)
    new_name = validate_account_name(new_name)
    if old_name not in config.accounts:
        raise ConfigError(f"Unknown account '{old_name}'.")
    if new_name in config.accounts:
        raise ConfigError(f"Account '{new_name}' is already registered.")
    accounts = dict(config.accounts)
    old = accounts.pop(old_name)
    accounts[new_name] = AccountConfig(
        name=new_name,
        label=old.label,
        codex_home=old.codex_home,
        model=old.model,
        reasoning_effort=old.reasoning_effort,
        backend=old.backend,
        service_tier=old.service_tier,
    )
    roles = {
        role: new_name if account == old_name else account
        for role, account in config.roles.items()
    }
    write_registry_config(config.config_path, accounts, roles)


def label_account(config: OrchestratorConfig, name: str, label: str) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing labels.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    accounts = dict(config.accounts)
    accounts[name] = AccountConfig(
        name=account.name,
        label=label.strip(),
        codex_home=account.codex_home,
        model=account.model,
        reasoning_effort=account.reasoning_effort,
        backend=account.backend,
        service_tier=account.service_tier,
    )
    write_registry_config(config.config_path, accounts, config.roles)


def remove_account(
    config: OrchestratorConfig,
    name: str,
    *,
    delete_profile: bool = False,
    confirm_delete: bool = False,
    input_fn: InputFn = input,
) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before removing accounts.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    assigned = roles_for_account(config.roles, name)
    if assigned:
        raise ConfigError(
            f"Cannot remove account '{name}'; roles still assigned: {', '.join(assigned)}."
        )
    if delete_profile:
        if account.codex_home.resolve() in {Path.home().resolve(), config.config_path.parent.resolve()}:
            raise ConfigError("Refusing to delete a broad profile or configuration directory.")
        if not confirm_delete:
            answer = input_fn(
                f"Type DELETE to remove the profile directory for '{name}': "
            ).strip()
            if answer != "DELETE":
                raise RuntimeError("Profile deletion cancelled.")
    accounts = dict(config.accounts)
    accounts.pop(name)
    write_registry_config(config.config_path, accounts, config.roles)
    if delete_profile and account.codex_home.exists():
        shutil.rmtree(account.codex_home)


def update_account_settings(
    config: OrchestratorConfig,
    name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
    backend: str | None = None,
) -> AccountConfig:
    """Persist validated future-turn settings without touching profile credentials."""
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing account settings.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    new_backend = account.backend if backend is None else str(backend).strip()
    if new_backend not in {"app_server", "windows"}:
        raise ConfigError("backend must be 'app_server' or 'windows'.")
    updated = AccountConfig(
        name=account.name,
        label=account.label,
        codex_home=account.codex_home,
        model=account.model if model is None else validate_setting_value(model, "model"),
        reasoning_effort=(
            account.reasoning_effort
            if reasoning_effort is None
            else (validate_setting_value(reasoning_effort, "reasoning_effort") or "high")
        ),
        backend=new_backend,
        service_tier=(
            account.service_tier
            if service_tier is None
            else validate_setting_value(service_tier, "service_tier")
        ),
    )
    accounts = dict(config.accounts)
    accounts[name] = updated
    write_registry_config(config.config_path, accounts, config.roles)
    return updated


def assign_role(config: OrchestratorConfig, role: str, account_name: str) -> tuple[str | None, str]:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    role = validate_role_name(role)
    account_name = validate_account_name(account_name)
    if account_name not in config.accounts:
        raise ConfigError(f"Unknown account '{account_name}'.")
    roles = dict(config.roles)
    previous = roles.get(role)
    roles[role] = account_name
    write_registry_config(config.config_path, config.accounts, roles)
    return previous, account_name


def unassign_role(config: OrchestratorConfig, role: str) -> str | None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    role = validate_role_name(role)
    roles = dict(config.roles)
    previous = roles.pop(role, None)
    write_registry_config(config.config_path, config.accounts, roles)
    return previous


def swap_roles(config: OrchestratorConfig, first: str, second: str) -> tuple[str | None, str | None]:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    first = validate_role_name(first)
    second = validate_role_name(second)
    roles = dict(config.roles)
    previous = (roles.get(first), roles.get(second))
    roles[first], roles[second] = roles.get(second, ""), roles.get(first, "")
    roles = {role: account for role, account in roles.items() if account}
    write_registry_config(config.config_path, config.accounts, roles)
    return previous


@dataclass(frozen=True)
class MigrationResult:
    changed: bool
    backup_path: Path | None


def migrate_legacy_config(
    path: Path,
    *,
    architect_name: str | None = None,
    executor_name: str | None = None,
    architect_label: str | None = None,
    executor_label: str | None = None,
    dry_run: bool = False,
    input_fn: InputFn = input,
    output: OutputFn = print,
    now: datetime | None = None,
) -> MigrationResult:
    path = path.expanduser().resolve()
    raw = load_raw_config(path)
    if not is_legacy_raw(raw):
        if "accounts" in raw and not ("architect" in raw or "executor" in raw):
            output("Configuration already uses the account registry; nothing to migrate.")
            return MigrationResult(changed=False, backup_path=None)
        raise ConfigError(
            "Configuration is neither a complete legacy format nor a clean registry format. "
            "Remove the partial sections manually and rerun migration."
        )
    if "architect" not in raw or "executor" not in raw:
        raise ConfigError("Legacy config must contain both [architect] and [executor].")

    base = path.parent
    legacy_architect = _account("architect", raw["architect"], base)
    legacy_executor = _account("executor", raw["executor"], base)

    architect_name = validate_account_name(
        architect_name if architect_name is not None else input_fn("Stable name for legacy Architect account: ")
    )
    executor_name = validate_account_name(
        executor_name if executor_name is not None else input_fn("Stable name for legacy Executor account: ")
    )
    if architect_name == executor_name:
        raise ConfigError("Legacy Architect and Executor accounts must have different names.")
    architect_label = (
        architect_label
        if architect_label is not None
        else input_fn("Friendly label for legacy Architect account (optional): ")
    ).strip()
    executor_label = (
        executor_label
        if executor_label is not None
        else input_fn("Friendly label for legacy Executor account (optional): ")
    ).strip()

    accounts = {
        architect_name: AccountConfig(
            name=architect_name,
            label=architect_label,
            codex_home=legacy_architect.codex_home,
            model=legacy_architect.model,
            reasoning_effort=legacy_architect.reasoning_effort,
            backend=legacy_architect.backend,
            service_tier=legacy_architect.service_tier,
        ),
        executor_name: AccountConfig(
            name=executor_name,
            label=executor_label,
            codex_home=legacy_executor.codex_home,
            model=legacy_executor.model,
            reasoning_effort=legacy_executor.reasoning_effort,
            backend=legacy_executor.backend,
            service_tier=legacy_executor.service_tier,
        ),
    }
    roles = {
        "orchestrator": architect_name,
        "architect": architect_name,
        "reviewer": architect_name,
        "executor": executor_name,
    }
    output("Migration preview:")
    output(f"  architect account: {architect_name} ({architect_label or 'no label'})")
    output(f"  executor account: {executor_name} ({executor_label or 'no label'})")
    output(f"  architect CODEX_HOME: {abbreviate_path(legacy_architect.codex_home)}")
    output(f"  executor CODEX_HOME: {abbreviate_path(legacy_executor.codex_home)}")
    output("  roles: orchestrator/architect/reviewer -> architect account; executor -> executor account")
    if dry_run:
        output("Dry run: configuration was not changed and no backup was created.")
        return MigrationResult(changed=False, backup_path=None)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    write_registry_config(path, accounts, roles)
    output(f"Migration complete. Backup: {backup.name}")
    return MigrationResult(changed=True, backup_path=backup)
