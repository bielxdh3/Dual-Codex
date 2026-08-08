from __future__ import annotations

from datetime import datetime, timezone
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

from . import __version__
from .app_server import (
    app_server_call,
    app_server_events,
    close_app_server_processes,
    _sanitize_stderr,
    _load_thread_mapping,
)
from .config import AgentConfig, ConfigError, OrchestratorConfig, validate_account_name, validate_setting_value, load_config
from .git import ensure_git_repository, status_porcelain
from .process import codex_environment, run_command
from .registry import abbreviate_path, assign_role, login_status, roles_for_account, update_account_settings


class DashboardError(ValueError):
    """A safe, user-actionable dashboard error."""


_ACCOUNT_PATH = re.compile(r"^/api/accounts/([A-Za-z0-9][A-Za-z0-9_-]*)(?:/(models|usage|settings))?$")
_SAFE_HOSTS = {"127.0.0.1", "localhost"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _one_line(value: Any, limit: int = 500) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:limit]


def _error_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or "Unavailable"
    return _one_line(_sanitize_stderr(str(value or "Unavailable")))


def _agent_for_account(account: Any) -> AgentConfig:
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


def _model_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    data = result.get("data")
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        tiers: list[dict[str, str]] = []
        raw_tiers = item.get("serviceTiers") or []
        if isinstance(raw_tiers, list):
            for tier in raw_tiers:
                if isinstance(tier, dict) and isinstance(tier.get("id"), str):
                    tiers.append({
                        "id": tier["id"],
                        "name": _one_line(tier.get("name") or tier["id"], 120),
                        "description": _one_line(tier.get("description") or "", 240),
                    })
        if not tiers:
            for tier_id in item.get("additionalSpeedTiers") or []:
                if isinstance(tier_id, str):
                    tiers.append({"id": tier_id, "name": tier_id, "description": ""})
        efforts: list[str] = []
        raw_efforts = item.get("supportedReasoningEfforts") or []
        if isinstance(raw_efforts, list):
            for effort in raw_efforts:
                if isinstance(effort, dict) and isinstance(effort.get("reasoningEffort"), str):
                    efforts.append(effort["reasoningEffort"])
                elif isinstance(effort, str):
                    efforts.append(effort)
        rows.append({
            "id": item["id"],
            "model": item.get("model") if isinstance(item.get("model"), str) else item["id"],
            "display_name": _one_line(item.get("displayName") or item["id"], 160),
            "description": _one_line(item.get("description") or "", 240),
            "is_default": bool(item.get("isDefault")),
            "hidden": bool(item.get("hidden")),
            "default_reasoning": item.get("defaultReasoningEffort") if isinstance(item.get("defaultReasoningEffort"), str) else None,
            "reasoning_efforts": list(dict.fromkeys(efforts)),
            "default_service_tier": item.get("defaultServiceTier") if isinstance(item.get("defaultServiceTier"), str) else None,
            "service_tiers": tiers,
        })
    return [row for row in rows if not row["hidden"]]


def _rate_limit_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots = result.get("rateLimitsByLimitId")
    if not isinstance(snapshots, dict):
        snapshots = {}
    if not snapshots and isinstance(result.get("rateLimits"), dict):
        legacy = result["rateLimits"]
        key = legacy.get("limitId") or legacy.get("limitName") or "default"
        snapshots = {str(key): legacy}
    rows: list[dict[str, Any]] = []
    for bucket, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        windows = []
        for key in ("primary", "secondary"):
            window = snapshot.get(key)
            if isinstance(window, dict):
                used = window.get("usedPercent")
                windows.append({
                    "kind": key,
                    "used_percent": int(used) if isinstance(used, (int, float)) else None,
                    "resets_at": window.get("resetsAt") if isinstance(window.get("resetsAt"), (int, float)) else None,
                    "window_minutes": window.get("windowDurationMins") if isinstance(window.get("windowDurationMins"), (int, float)) else None,
                })
        rows.append({
            "id": str(bucket),
            "name": _one_line(snapshot.get("limitName") or bucket, 120),
            "plan_type": snapshot.get("planType"),
            "rate_limit_reached": snapshot.get("rateLimitReachedType"),
            "spend_control_reached": snapshot.get("spendControlReached"),
            "windows": windows,
        })
    return rows


def _usage_data(result: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not result.get("summary") and not result.get("dailyUsageBuckets"):
        return None
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    buckets = []
    for bucket in result.get("dailyUsageBuckets") or []:
        if isinstance(bucket, dict) and isinstance(bucket.get("startDate"), str):
            buckets.append({
                "start_date": bucket["startDate"],
                "tokens": bucket.get("tokens") if isinstance(bucket.get("tokens"), (int, float)) else None,
            })
    return {
        "summary": {key: summary.get(key) for key in (
            "currentStreakDays", "lifetimeTokens", "longestRunningTurnSec", "longestStreakDays", "peakDailyTokens"
        ) if isinstance(summary.get(key), (int, float)) or summary.get(key) is None},
        "daily": buckets,
    }


def _thread_status(thread: dict[str, Any] | None) -> str | None:
    status = thread.get("status") if isinstance(thread, dict) else None
    if isinstance(status, dict) and isinstance(status.get("type"), str):
        return status["type"]
    return status if isinstance(status, str) else None


class DashboardService:
    """Read-only telemetry plus validated future-turn configuration writes."""

    def __init__(self, config: OrchestratorConfig, repository: Path | None = None) -> None:
        self.config = config
        self.repository = (repository or config.repository).expanduser().resolve()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def _reload(self) -> None:
        self.config = load_config(self.config.config_path)

    def _call(self, agent: AgentConfig, method: str, params: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
        result = app_server_call(
            config=self.config,
            agent=agent,
            repository=self.repository,
            method=method,
            params=params,
            timeout=self.config.dashboard_telemetry_timeout,
        )
        error = result.get("error") if isinstance(result, dict) else None
        return (result if isinstance(result, dict) else {}, _error_text(error) if error else None)

    def _thread(self, agent: AgentConfig) -> tuple[dict[str, Any] | None, str | None]:
        mapped = _load_thread_mapping(self.config, agent, self.repository)
        if mapped:
            result, error = self._call(agent, "thread/read", {"threadId": mapped, "includeTurns": False})
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else None
            if thread is not None and error is None:
                return thread, None
        result, error = self._call(agent, "thread/list", {
            "cwd": str(self.repository),
            "limit": 10,
            "archived": False,
            "sortKey": "updated_at",
            "sortDirection": "desc",
        })
        rows = result.get("data") if isinstance(result.get("data"), list) else []
        for row in rows:
            if isinstance(row, dict):
                return row, error
        return None, error

    def _token_usage(self, agent: AgentConfig, thread_id: str | None) -> dict[str, Any] | None:
        if not thread_id:
            return None
        latest = None
        for event in reversed(app_server_events(config=self.config, agent=agent, repository=self.repository)):
            if event.get("method") != "thread/tokenUsage/updated":
                continue
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            if params.get("threadId") == thread_id and isinstance(params.get("tokenUsage"), dict):
                latest = params["tokenUsage"]
                break
        if not isinstance(latest, dict):
            return None
        return {"last": latest.get("last"), "total": latest.get("total"), "model_context_window": latest.get("modelContextWindow")}

    def collect_account(self, name: str, *, force: bool = False) -> dict[str, Any]:
        name = validate_account_name(name)
        with self._lock:
            cached = self._cache.get(name)
            if cached and not force and time.monotonic() - cached[0] < 3:
                return cached[1]
        account = self.config.accounts.get(name)
        if account is None:
            raise DashboardError(f"Unknown account '{name}'.")
        base: dict[str, Any] = {
            "name": account.name,
            "stable_key": account.name,
            "label": account.label,
            "roles": roles_for_account(self.config.roles, account.name),
            "backend": account.backend,
            "codex_home": abbreviate_path(account.codex_home),
            "login": login_status(self.config, account),
            "configured": {
                "model": account.model,
                "model_label": account.model or "Inherit Codex default",
                "reasoning_effort": account.reasoning_effort,
                "service_tier": account.service_tier,
                "service_tier_label": account.service_tier or "Default / not requested",
            },
            "effective": {"model": None, "reasoning_effort": None, "service_tier": None},
            "models": [],
            "rate_limits": [],
            "usage": None,
            "thread": None,
            "token_usage": None,
            "capabilities": {},
            "last_error": None,
            "refreshed_at": _now(),
        }
        if account.backend != "app_server":
            base["runtime_state"] = "Connected" if base["login"] == "OK" else ("Unavailable" if base["login"] == "NOT LOGGED IN" else "Unknown")
            base["capabilities"] = {"app_server": False, "model_list": False, "account_read": False, "rate_limits": False, "usage": False, "thread": False, "thread_token_usage": False, "thread_settings_update": False}
            with self._lock:
                self._cache[name] = (time.monotonic(), base)
            return base

        agent = _agent_for_account(account)
        errors: list[str] = []
        model_result, error = self._call(agent, "model/list", {"includeHidden": False, "limit": 100})
        if error:
            errors.append(f"model/list: {error}")
        models = _model_rows(model_result)
        account_result, error = self._call(agent, "account/read", {})
        if error:
            errors.append(f"account/read: {error}")
        rate_result, error = self._call(agent, "account/rateLimits/read", None)
        if error:
            errors.append(f"account/rateLimits/read: {error}")
        usage_result, error = self._call(agent, "account/usage/read", None)
        if error:
            errors.append(f"account/usage/read: {error}")
        thread, thread_error = self._thread(agent)
        if thread_error:
            errors.append(f"thread: {thread_error}")
        thread_id = thread.get("id") if isinstance(thread, dict) and isinstance(thread.get("id"), str) else None
        token_usage = self._token_usage(agent, thread_id)
        default_model = next((model["id"] for model in models if model["is_default"]), None)
        default_model_row = next((model for model in models if model["is_default"]), None)
        default_service_tier = default_model_row.get("default_service_tier") if isinstance(default_model_row, dict) else None
        event_model = event_reasoning = event_tier = None
        for event in reversed(app_server_events(config=self.config, agent=agent, repository=self.repository)):
            if event.get("method") != "turn/started":
                continue
            turn = (event.get("params") or {}).get("turn") if isinstance(event.get("params"), dict) else None
            if isinstance(turn, dict):
                event_model = turn.get("model") if isinstance(turn.get("model"), str) else event_model
                event_reasoning = turn.get("reasoningEffort") if isinstance(turn.get("reasoningEffort"), str) else event_reasoning
                event_tier = turn.get("serviceTier") if isinstance(turn.get("serviceTier"), str) else event_tier
            if event_model or event_reasoning or event_tier:
                break
        base["models"] = models
        base["account"] = {
            "type": account_result.get("account", {}).get("type") if isinstance(account_result.get("account"), dict) else None,
            "email": account_result.get("account", {}).get("email") if isinstance(account_result.get("account"), dict) and isinstance(account_result.get("account", {}).get("email"), str) else None,
            "plan_type": account_result.get("account", {}).get("planType") if isinstance(account_result.get("account"), dict) else None,
        }
        base["rate_limits"] = _rate_limit_rows(rate_result)
        base["usage"] = _usage_data(usage_result)
        base["thread"] = {
            "id": thread_id,
            "name": thread.get("name") if isinstance(thread, dict) and isinstance(thread.get("name"), str) else None,
            "status": _thread_status(thread),
            "updated_at": thread.get("updatedAt") if isinstance(thread, dict) else None,
            "source": thread.get("source") if isinstance(thread, dict) and isinstance(thread.get("source"), str) else None,
        } if thread is not None else None
        base["token_usage"] = token_usage
        base["effective"] = {
            "model": event_model or (default_model if not account.model else None),
            "reasoning_effort": event_reasoning,
            "service_tier": event_tier or (default_service_tier if not account.service_tier else None),
        }
        base["capabilities"] = {
            "app_server": True,
            "model_list": not bool(model_result.get("error")),
            "account_read": not bool(account_result.get("error")),
            "rate_limits": not bool(rate_result.get("error")),
            "usage": not bool(usage_result.get("error")),
            "thread": thread is not None,
            "thread_token_usage": token_usage is not None,
            "reasoning": any(model["reasoning_efforts"] for model in models),
            "service_tier": any(model["service_tiers"] for model in models),
            "thread_settings_update": True,
        }
        rate_limited = any(row.get("rate_limit_reached") or any((window.get("used_percent") or 0) >= 100 for window in row.get("windows", [])) for row in base["rate_limits"])
        if rate_limited:
            state = "Rate limited"
        elif base["thread"] and base["thread"].get("status") == "active":
            state = "Working"
        elif errors and not models and base["login"] != "OK":
            state = "Unavailable"
        elif errors:
            state = "Unknown"
        else:
            state = "Idle"
        base["runtime_state"] = state
        base["last_error"] = "; ".join(errors)[:800] if errors else None
        with self._lock:
            self._cache[name] = (time.monotonic(), base)
        return base

    def accounts(self, *, force: bool = False) -> list[dict[str, Any]]:
        return [self.collect_account(name, force=force) for name in self.config.accounts]

    def status(self) -> dict[str, Any]:
        try:
            ensure_git_repository(self.repository)
            git_state = "clean" if not status_porcelain(self.repository).strip() else "dirty"
        except Exception:
            git_state = "unavailable"
        executable = shutil.which(self.config.codex_command)
        version = "Unknown"
        if executable or Path(self.config.codex_command).exists():
            try:
                result = run_command([self.config.codex_command, "--version"], cwd=self.config.project_root, env=codex_environment(_agent_for_account(next(iter(self.config.accounts.values())))) if self.config.accounts else None, check=False)
                version = _one_line(result.stdout or result.stderr).splitlines()[0] if result.stdout or result.stderr else "Unknown"
            except OSError:
                pass
        with self._lock:
            cached_accounts = [value[1] for value in self._cache.values() if time.monotonic() - value[0] < 30]
        if not cached_accounts:
            app_server_health = "not_checked"
        elif any(item.get("capabilities", {}).get("app_server") and not item.get("last_error") for item in cached_accounts):
            app_server_health = "healthy"
        else:
            app_server_health = "unavailable"
        return {
            "schema_version": 1,
            "dual_codex_version": __version__,
            "codex_cli": {"path": executable or self.config.codex_command, "version": version},
            "repository": str(self.repository),
            "git_state": git_state,
            "executor_account": self.config.roles.get("executor", ""),
            "app_server_health": app_server_health,
            "last_refresh": _now(),
            "config": str(self.config.config_path),
        }

    def models(self, name: str) -> dict[str, Any]:
        account = self.collect_account(name)
        return {"schema_version": 1, "account": name, "models": account["models"], "capabilities": account["capabilities"]}

    def usage(self, name: str) -> dict[str, Any]:
        account = self.collect_account(name)
        return {"schema_version": 1, "account": name, "rate_limits": account["rate_limits"], "usage": account["usage"], "thread": account["thread"], "token_usage": account["token_usage"], "refreshed_at": account["refreshed_at"]}

    def save_settings(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise DashboardError("Settings body must be a JSON object.")
        allowed = {"model", "reasoning_effort", "service_tier", "backend", "scope"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise DashboardError(f"Unknown setting(s): {', '.join(unknown)}.")
        scope = body.get("scope", "future_turns")
        if scope != "future_turns":
            raise DashboardError("Current-thread changes are not enabled; saved values apply to future turns only.")
        account = self.config.accounts.get(validate_account_name(name))
        if account is None:
            raise DashboardError(f"Unknown account '{name}'.")
        model = body.get("model")
        effort = body.get("reasoning_effort")
        tier = body.get("service_tier")
        backend = body.get("backend")
        if model is not None and not isinstance(model, str):
            raise DashboardError("model must be a string.")
        if effort is not None and not isinstance(effort, str):
            raise DashboardError("reasoning_effort must be a string.")
        if tier is not None and not isinstance(tier, str):
            raise DashboardError("service_tier must be a string.")
        if backend is not None and backend not in {"app_server", "windows"}:
            raise DashboardError("backend must be 'app_server' or 'windows'.")
        current = self.collect_account(name)
        models = current.get("models", [])
        chosen_model = account.model if model is None else validate_setting_value(model, "model")
        selected = (next((item for item in models if item["id"] == chosen_model), None) if chosen_model else next((item for item in models if item["is_default"]), None))
        if chosen_model and models and selected is None:
            raise DashboardError("Selected model is not advertised by the installed App Server.")
        chosen_effort = account.reasoning_effort if effort is None else (validate_setting_value(effort, "reasoning_effort") or "high")
        if selected and selected["reasoning_efforts"] and chosen_effort not in selected["reasoning_efforts"]:
            raise DashboardError("Selected reasoning effort is not supported by the selected model.")
        chosen_tier = account.service_tier if tier is None else validate_setting_value(tier, "service_tier")
        known_tiers = {item["id"] for item in selected["service_tiers"]} if selected else set()
        if chosen_tier and known_tiers and chosen_tier not in known_tiers:
            raise DashboardError("Selected service tier is not supported by the selected model.")
        updated = update_account_settings(
            self.config,
            name,
            model=model,
            reasoning_effort=effort,
            service_tier=tier,
            backend=backend,
        )
        self._reload()
        with self._lock:
            self._cache.pop(name, None)
        return {
            "schema_version": 1,
            "account": name,
            "scope": "future_turns",
            "current_thread_changed": False,
            "saved": {
                "model": updated.model,
                "reasoning_effort": updated.reasoning_effort,
                "service_tier": updated.service_tier,
                "backend": updated.backend,
            },
            "message": "Saved for future Dual Codex turns; current persistent thread unchanged.",
        }

    def assign(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict) or set(body) != {"role", "account"}:
            raise DashboardError("Role assignment requires exactly role and account.")
        previous, current = assign_role(self.config, body["role"], body["account"])
        self._reload()
        return {"schema_version": 1, "role": body["role"], "previous": previous, "account": current}


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dual Codex · Account control</title><link rel="stylesheet" href="/static/styles.css"><script defer src="/static/app.js"></script></head>
<body><main class="shell"><header class="hero"><div><p class="eyebrow">LOCAL CONTROL PLANE</p><h1>Dual Codex</h1><p class="lede">Account control, effective settings, and usage telemetry.</p></div><div id="health" class="health" aria-live="polite">Loading…</div></header>
<section class="summary" id="summary"></section><section><div class="section-head"><div><p class="eyebrow">ACCOUNT REGISTRY</p><h2>Connected accounts</h2></div><button id="refresh" class="button secondary">Refresh</button></div><div id="accounts" class="accounts"><div class="empty">Loading account telemetry…</div></div></section>
<footer><span>Loopback-only dashboard</span><span id="updated"></span></footer></main></body></html>"""

STYLES = """:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2d;--panel2:#18233b;--text:#edf2ff;--muted:#9eacc8;--line:#293653;--accent:#78a9ff;--good:#53d6a3;--warn:#ffc56d;--bad:#ff7f92;--shadow:0 18px 48px #05081180}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#1d2b52 0,#0b1020 42%);font:15px/1.45 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text)}.shell{width:min(1200px,calc(100% - 32px));margin:0 auto;padding:36px 0 28px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:28px}.eyebrow{color:var(--accent);font-size:11px;letter-spacing:.16em;font-weight:700;margin:0 0 8px}.hero h1{font-size:clamp(34px,6vw,64px);line-height:1;margin:0 0 12px;letter-spacing:-.05em}.lede{color:var(--muted);font-size:17px;margin:0}.health{border:1px solid var(--line);background:#101a2eaa;border-radius:999px;padding:9px 14px;color:var(--muted);white-space:nowrap}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:38px}.stat,.card{background:linear-gradient(160deg,#15213a,#0f1729);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}.stat{padding:16px}.stat .label{color:var(--muted);font-size:12px}.stat strong{display:block;margin-top:6px;font-size:18px;overflow-wrap:anywhere}.section-head{display:flex;align-items:end;justify-content:space-between;margin-bottom:16px}.section-head h2{margin:0;font-size:28px;letter-spacing:-.03em}.button{border:0;border-radius:10px;padding:10px 15px;color:var(--text);font-weight:700;cursor:pointer;background:var(--accent);color:#071127}.button.secondary{background:#1c2a47;color:var(--text);border:1px solid var(--line)}.accounts{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}.card{padding:20px}.card-head{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:15px}.card h3{font-size:22px;margin:0;overflow-wrap:anywhere}.sub{color:var(--muted);font-size:13px}.pill{display:inline-flex;align-items:center;border-radius:999px;padding:4px 9px;font-size:11px;font-weight:800;white-space:nowrap}.pill.good{background:#153d35;color:var(--good)}.pill.warn{background:#4a371d;color:var(--warn)}.pill.bad{background:#4a202d;color:var(--bad)}.pill.neutral{background:#24314b;color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}.field{display:flex;flex-direction:column;gap:5px}.field label,.metric-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.field output,.value{font-weight:650;overflow-wrap:anywhere}.field select{width:100%;background:#0c1425;border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.field small,.hint{color:var(--muted);font-size:12px}.control{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}.save-row{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-top:12px}.feedback{font-size:12px;color:var(--muted);min-height:18px}.bars{display:grid;gap:8px;margin-top:8px}.bar-head{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}.bar{height:7px;background:#0a1120;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent);border-radius:99px}.thread{padding:10px;background:#0d1628;border-radius:10px;margin-top:10px}.empty{color:var(--muted);padding:28px;border:1px dashed var(--line);border-radius:14px;text-align:center}.error{color:var(--bad);font-size:12px;margin-top:8px}footer{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:28px}@media(max-width:700px){.shell{width:min(100% - 20px,1200px);padding-top:22px}.hero{display:block}.health{display:inline-flex;margin-top:16px}.summary{grid-template-columns:1fr 1fr}.accounts{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.section-head h2{font-size:24px}}"""

SCRIPT = """const $=s=>document.querySelector(s);const esc=v=>{if(v==null)return 'Not available';return String(v).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))};const fmtTokens=v=>v==null?'Not available':Number(v).toLocaleString();const fmtReset=v=>{if(!v)return 'No reset published';const d=Math.max(0,Math.floor(Number(v)-Date.now()/1000));if(d<60)return `${d}s`;if(d<3600)return `${Math.floor(d/60)}m`;return `${Math.floor(d/3600)}h ${Math.floor(d%3600/60)}m`};
async function get(path,opts){const r=await fetch(path,opts);const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
function pill(state){const c=state==='Idle'||state==='Connected'?'good':state==='Working'||state==='Rate limited'?'warn':state==='Unavailable'?'bad':'neutral';return `<span class="pill ${c}">${esc(state)}</span>`}
function bar(row){return (row.windows||[]).map(w=>{const p=Math.max(0,Math.min(100,Number(w.used_percent)||0));return `<div><div class="bar-head"><span>${esc(w.kind)} · ${p}% used</span><span>resets ${fmtReset(w.resets_at)}</span></div><div class="bar"><i style="width:${p}%"></i></div></div>`}).join('')||'<span class="hint">Not available</span>'}
function options(items,current,inherit){let out=inherit?`<option value="">Inherit Codex default</option>`:'';for(const i of items||[])out+=`<option value="${esc(i.id)}" ${i.id===current?'selected':''}>${esc(i.display_name||i.id)}</option>`;return out}
function card(a){const model=a.configured.model||'';const selected=(a.models||[]).find(x=>x.id===model)||((a.models||[]).find(x=>x.is_default));const efforts=selected?.reasoning_efforts||[];const tiers=selected?.service_tiers||[];const usage=a.usage?.summary?.lifetimeTokens;const thread=a.thread;return `<article class="card"><div class="card-head"><div><h3>${esc(a.label||a.name)}</h3><div class="sub">${esc(a.name)} · ${esc(a.backend)} · ${esc(a.codex_home)}</div><div class="sub">Roles: ${esc((a.roles||[]).join(', ')||'none')} · Login: ${esc(a.login)}</div></div>${pill(a.runtime_state)}</div><div class="grid"><div class="field"><label>Configured model</label><output>${esc(a.configured.model_label)}</output></div><div class="field"><label>Effective model</label><output>${esc(a.effective.model||'Unknown')}</output></div><div class="field"><label>Configured reasoning</label><output>${esc(a.configured.reasoning_effort)}</output></div><div class="field"><label>Effective reasoning</label><output>${esc(a.effective.reasoning_effort||'Unknown')}</output></div><div class="field"><label>Fast requested / tier</label><output>${esc(a.configured.service_tier||'OFF / default')}</output></div><div class="field"><label>Effective service tier</label><output>${esc(a.effective.service_tier||'Unknown')}</output></div></div><div class="control"><div class="field"><label>Save for future turns</label><select data-model>${options(a.models,model,true)}</select><small>Empty means inherit the installed Codex default. Current thread is not changed.</small></div><div class="grid"><div class="field"><label>Reasoning effort</label><select data-effort>${(efforts.length?efforts:[a.configured.reasoning_effort||'high']).map(x=>`<option value="${esc(x)}" ${x===a.configured.reasoning_effort?'selected':''}>${esc(x)}</option>`).join('')}</select></div><div class="field"><label>Service tier / Fast</label><select data-tier><option value="">Default / OFF</option>${tiers.map(x=>`<option value="${esc(x.id)}" ${x.id===a.configured.service_tier?'selected':''}>${esc(x.name||x.id)}</option>`).join('')}</select></div><div class="field"><label>Backend</label><select data-backend><option value="app_server" ${a.backend==='app_server'?'selected':''}>App Server</option><option value="windows" ${a.backend==='windows'?'selected':''}>Native Windows TUI</option></select></div></div><div class="save-row"><span class="feedback" data-feedback>Settings are future-turn only.</span><button class="button" data-save>Save settings</button></div></div><div class="control"><div class="metric-label">Usage / rate-limit capacity</div><div class="bars">${(a.rate_limits||[]).map(bar).join('')||'<span class="hint">Not available</span>'}</div><div class="sub" style="margin-top:9px">Lifetime tokens: ${fmtTokens(usage)}</div>${thread?`<div class="thread"><div class="metric-label">Persistent thread</div><div class="value">${esc(thread.name||thread.id||'Unknown')}</div><div class="sub">${esc(thread.status||'Unknown')} · token usage ${a.token_usage?'available':'Not available'}</div></div>`:'<div class="thread sub">Persistent thread: Not available</div>'}</div>${a.last_error?`<div class="error">Telemetry: ${esc(a.last_error)}</div>`:''}</article>`}
async function refresh(){const [s,as]=await Promise.all([get('/api/status'),get('/api/accounts')]);const healthy=as.accounts.some(a=>a.capabilities&&a.capabilities.app_server&&!a.last_error);$('#health').textContent=`App Server: ${healthy?'healthy':s.app_server_health}`;$('#summary').innerHTML=[['Dual Codex',s.dual_codex_version],['Codex CLI',s.codex_cli.version],['Repository',s.repository],['Git',s.git_state]].map(x=>`<div class="stat"><div class="label">${x[0]}</div><strong>${esc(x[1])}</strong></div>`).join('');$('#accounts').innerHTML=as.accounts.map(card).join('')||'<div class="empty">No accounts configured.</div>';$('#updated').textContent=`Updated ${new Date().toLocaleTimeString()}`;document.querySelectorAll('[data-save]').forEach(btn=>btn.addEventListener('click',async()=>{const c=btn.closest('.card');const name=c.querySelector('.sub').textContent.split(' · ')[0];const f=c.querySelector('[data-feedback]');f.textContent='Saving…';try{await get(`/api/accounts/${encodeURIComponent(name)}/settings`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:c.querySelector('[data-model]').value,reasoning_effort:c.querySelector('[data-effort]').value,service_tier:c.querySelector('[data-tier]').value,backend:c.querySelector('[data-backend]').value,scope:'future_turns'})});f.textContent='Saved for future turns; current thread unchanged.';await refresh()}catch(e){f.textContent=e.message}}))}
$('#refresh').addEventListener('click',()=>refresh().catch(e=>$('#accounts').innerHTML=`<div class="empty">${esc(e.message)}</div>`));refresh().catch(e=>{$('#health').textContent='Unavailable';$('#accounts').innerHTML=`<div class="empty">${esc(e.message)}</div>`});"""


class _Handler(BaseHTTPRequestHandler):
    server: "_HTTPServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0].strip("[]").casefold() if host else ""
        if hostname not in _SAFE_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.hostname and parsed.hostname.casefold() not in _SAFE_HOSTS:
                return False
        return True

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DashboardError("Invalid request body length.") from exc
        if length > 64 * 1024:
            raise DashboardError("Request body is too large.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardError("Request body must be valid UTF-8 JSON.") from exc
        if not isinstance(value, dict):
            raise DashboardError("Request body must be a JSON object.")
        return value

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            self._send(403, {"error": "Loopback Host/Origin required."})
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if ".." in path or "\\" in path or "\x00" in path:
            self._send(400, {"error": "Unsafe path."})
            return
        service = self.server.service
        try:
            if method == "GET" and path == "/":
                self._send(200, HTML, "text/html")
                return
            if method == "GET" and path == "/static/styles.css":
                self._send(200, STYLES, "text/css")
                return
            if method == "GET" and path == "/static/app.js":
                self._send(200, SCRIPT, "application/javascript")
                return
            if method == "GET" and path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if method == "GET" and path == "/healthz":
                self._send(200, {"ok": True})
                return
            if method == "GET" and path == "/api/status":
                self._send(200, service.status())
                return
            if method == "GET" and path == "/api/accounts":
                self._send(200, {"schema_version": 1, "accounts": service.accounts()})
                return
            match = _ACCOUNT_PATH.fullmatch(path)
            if match:
                name, suffix = match.groups()
                validate_account_name(name)
                if method == "GET" and suffix == "models":
                    self._send(200, service.models(name))
                    return
                if method == "GET" and suffix == "usage":
                    self._send(200, service.usage(name))
                    return
                if method in {"POST", "PATCH"} and suffix == "settings":
                    self._send(200, service.save_settings(name, self._body()))
                    return
            if method == "POST" and path == "/api/roles/assign":
                self._send(200, service.assign(self._body()))
                return
            self._send(405 if method == "GET" else 404, {"error": "Route or method not supported."})
        except (DashboardError, ConfigError, ValueError) as exc:
            self._send(400, {"error": _one_line(exc)})
        except Exception:
            self._send(500, {"error": "Dashboard request failed."})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: DashboardService) -> None:
        self.service = service
        super().__init__(address, _Handler)


class DashboardServer:
    """Loopback-only HTTP server with clean child-process shutdown."""

    def __init__(self, config: OrchestratorConfig, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host != "127.0.0.1":
            raise DashboardError("Dashboard binds to 127.0.0.1 only.")
        if not 0 <= int(port) <= 65535:
            raise DashboardError("Dashboard port must be between 0 and 65535.")
        self.service = DashboardService(config)
        self.httpd = _HTTPServer((host, int(port)), self.service)
        self._started = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def start(self) -> str:
        self._started = True
        return self.url

    def open_browser(self) -> None:
        webbrowser.open(self.url)

    def serve_forever(self) -> None:
        if not self._started:
            self.start()
        self.httpd.serve_forever(poll_interval=0.2)

    def serve_in_thread(self) -> threading.Thread:
        self.start()
        thread = threading.Thread(target=self.serve_forever, name="dual-codex-dashboard", daemon=True)
        thread.start()
        return thread

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        close_app_server_processes()
