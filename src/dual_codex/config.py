from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
import tomllib


SUPPORTED_ROLES = ("orchestrator", "architect", "reviewer", "executor")
SUPPORTED_BACKENDS = ("app_server", "windows")
_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ROLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SETTING_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{0,200}$")


class ConfigError(ValueError):
    """Raised when the registry configuration is invalid or incomplete."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    label: str
    codex_home: Path
    model: str
    reasoning_effort: str
    backend: str = "windows"
    service_tier: str = ""


@dataclass(frozen=True)
class AgentConfig:
    codex_home: Path
    model: str
    reasoning_effort: str
    sandbox: str
    account_name: str = ""
    label: str = ""
    backend: str = "windows"
    service_tier: str = ""


@dataclass(frozen=True)
class OrchestratorConfig:
    repository: Path
    runs_dir: Path
    max_correction_cycles: int
    require_clean_git: bool
    codex_command: str
    accounts: dict[str, AccountConfig]
    roles: dict[str, str]
    project_root: Path
    config_path: Path
    legacy: bool = False
    node_command: str = "node"
    terminal_readiness_timeout: float = 60.0
    terminal_turn_start_timeout: float = 15.0
    app_server_initialize_timeout: float = 30.0
    app_server_thread_timeout: float = 30.0
    app_server_turn_start_timeout: float = 30.0
    app_server_turn_timeout: float = 600.0
    dashboard_telemetry_timeout: float = 5.0
    live_event_journal_max_records: int = 2000
    live_event_journal_max_record_bytes: int = 65536
    live_event_journal_max_detail_bytes: int = 16384

    @property
    def architect(self) -> AgentConfig:
        return self.agent_for_role("architect")

    @property
    def executor(self) -> AgentConfig:
        return self.agent_for_role("executor")

    def account_for_role(self, role: str) -> AccountConfig:
        if role == "reviewer" and role not in self.roles:
            role = "architect"
        account_name = self.roles.get(role)
        if not account_name:
            raise ConfigError(f"Required role '{role}' is unassigned.")
        account = self.accounts.get(account_name)
        if account is None:
            raise ConfigError(
                f"Role '{role}' refers to unknown account '{account_name}'."
            )
        return account

    def agent_for_role(self, role: str) -> AgentConfig:
        account = self.account_for_role(role)
        sandbox = "workspace-write" if role == "executor" else "read-only"
        return AgentConfig(
            codex_home=account.codex_home,
            model=account.model,
            reasoning_effort=account.reasoning_effort,
            sandbox=sandbox,
            account_name=account.name,
            label=account.label,
            backend=account.backend,
            service_tier=account.service_tier,
        )


def validate_account_name(name: str) -> str:
    name = str(name).strip()
    if not _ACCOUNT_NAME.fullmatch(name):
        raise ConfigError(
            "Account names must start with a letter or number and contain "
            "only letters, numbers, '-' or '_'."
        )
    return name


def validate_role_name(role: str) -> str:
    role = str(role).strip()
    if not _ROLE_NAME.fullmatch(role):
        raise ConfigError(
            "Role names must start with a letter and contain only letters, "
            "numbers or '_'."
        )
    if role not in SUPPORTED_ROLES:
        raise ConfigError(
            f"Unknown role '{role}'. Supported roles: {', '.join(SUPPORTED_ROLES)}."
        )
    return role


def validate_setting_value(value: str, field: str) -> str:
    value = str(value).strip()
    if not _SETTING_VALUE.fullmatch(value):
        raise ConfigError(f"{field} must be a single line of at most 200 characters.")
    return value


def _path(raw: Any, base: Path) -> Path:
    value = Path(str(raw)).expanduser()
    return value if value.is_absolute() else (base / value).resolve()


def _account(name: str, raw: dict[str, Any], base: Path) -> AccountConfig:
    if "codex_home" not in raw:
        raise ConfigError(f"Account '{name}' is missing codex_home.")
    backend = str(raw.get("backend", "windows")).strip() or "windows"
    if backend not in SUPPORTED_BACKENDS:
        raise ConfigError(
            f"Account '{name}' has unsupported backend '{backend}'. "
            f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}."
        )
    return AccountConfig(
        name=validate_account_name(name),
        label=str(raw.get("label", "")).strip(),
        codex_home=_path(raw["codex_home"], base),
        model=validate_setting_value(raw.get("model", ""), "model"),
        reasoning_effort=validate_setting_value(raw.get("reasoning_effort", "high"), "reasoning_effort") or "high",
        backend=backend,
        service_tier=validate_setting_value(raw.get("service_tier", ""), "service_tier"),
    )


def load_raw_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return tomllib.loads(data.decode("utf-8"))


def is_legacy_raw(raw: dict[str, Any]) -> bool:
    return "accounts" not in raw and ("architect" in raw or "executor" in raw)


def load_config(path: Path) -> OrchestratorConfig:
    path = path.expanduser().resolve()
    raw = load_raw_config(path)
    base = path.parent

    try:
        orch = raw["orchestrator"]
        if not isinstance(orch, dict):
            raise TypeError
    except (KeyError, TypeError) as exc:
        raise ConfigError("Missing [orchestrator] configuration.") from exc

    repository = _path(orch["repository"], base)
    runs_dir = _path(orch.get("runs_dir", "runs"), base)

    legacy = is_legacy_raw(raw)
    accounts: dict[str, AccountConfig] = {}
    if legacy:
        if "architect" not in raw or "executor" not in raw:
            raise ConfigError("Legacy config must contain both [architect] and [executor].")
        for name in ("architect", "executor"):
            value = raw[name]
            if not isinstance(value, dict):
                raise ConfigError(f"Legacy [{name}] section is invalid.")
            accounts[name] = _account(name, value, base)
        roles = {
            "orchestrator": "architect",
            "architect": "architect",
            "reviewer": "architect",
            "executor": "executor",
        }
    else:
        raw_accounts = raw.get("accounts", {})
        if not isinstance(raw_accounts, dict):
            raise ConfigError("[accounts] must contain account tables.")
        for name, value in raw_accounts.items():
            if not isinstance(value, dict):
                raise ConfigError(f"Account '{name}' is invalid.")
            account = _account(str(name), value, base)
            if account.name in accounts:
                raise ConfigError(f"Duplicate account '{account.name}'.")
            accounts[account.name] = account

        raw_roles = raw.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise ConfigError("[roles] must contain role assignments.")
        roles = {
            str(role): str(account).strip()
            for role, account in raw_roles.items()
            if str(account).strip()
        }

    terminal_readiness_timeout = float(orch.get("terminal_readiness_timeout", 60.0))
    if terminal_readiness_timeout <= 0:
        raise ConfigError("terminal_readiness_timeout must be positive.")
    terminal_turn_start_timeout = float(orch.get("terminal_turn_start_timeout", 15.0))
    if terminal_turn_start_timeout <= 0:
        raise ConfigError("terminal_turn_start_timeout must be positive.")
    app_server_initialize_timeout = float(orch.get("app_server_initialize_timeout", 30.0))
    app_server_thread_timeout = float(orch.get("app_server_thread_timeout", 30.0))
    app_server_turn_start_timeout = float(orch.get("app_server_turn_start_timeout", 30.0))
    app_server_turn_timeout = float(orch.get("app_server_turn_timeout", 600.0))
    dashboard_telemetry_timeout = float(orch.get("dashboard_telemetry_timeout", 5.0))
    live_event_journal_max_records = int(orch.get("live_event_journal_max_records", 2000))
    live_event_journal_max_record_bytes = int(orch.get("live_event_journal_max_record_bytes", 65536))
    live_event_journal_max_detail_bytes = int(orch.get("live_event_journal_max_detail_bytes", 16384))
    if any(
        value <= 0
        for value in (
            live_event_journal_max_records,
            live_event_journal_max_record_bytes,
            live_event_journal_max_detail_bytes,
        )
    ):
        raise ConfigError("Live event journal limits must be positive.")
    if any(
        value <= 0
        for value in (
            app_server_initialize_timeout,
            app_server_thread_timeout,
            app_server_turn_start_timeout,
            app_server_turn_timeout,
            dashboard_telemetry_timeout,
        )
    ):
        raise ConfigError("App Server timeouts must be positive.")

    return OrchestratorConfig(
        repository=repository,
        runs_dir=runs_dir,
        max_correction_cycles=int(orch.get("max_correction_cycles", 1)),
        require_clean_git=bool(orch.get("require_clean_git", True)),
        codex_command=str(orch.get("codex_command", "codex")),
        accounts=accounts,
        roles=roles,
        project_root=Path(__file__).resolve().parents[2],
        config_path=path,
        legacy=legacy,
        node_command=str(orch.get("node_command", "node")).strip() or "node",
        terminal_readiness_timeout=terminal_readiness_timeout,
        terminal_turn_start_timeout=terminal_turn_start_timeout,
        app_server_initialize_timeout=app_server_initialize_timeout,
        app_server_thread_timeout=app_server_thread_timeout,
        app_server_turn_start_timeout=app_server_turn_start_timeout,
        app_server_turn_timeout=app_server_turn_timeout,
        dashboard_telemetry_timeout=dashboard_telemetry_timeout,
        live_event_journal_max_records=live_event_journal_max_records,
        live_event_journal_max_record_bytes=live_event_journal_max_record_bytes,
        live_event_journal_max_detail_bytes=live_event_journal_max_detail_bytes,
    )
