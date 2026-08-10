from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import time

from .config import AgentConfig, ConfigError, SUPPORTED_ROLES, load_config
from .delegation import delegate
from .doctor import run_doctor
from .git import ensure_git_repository, status_porcelain
from .orchestrator import execute
from .process import run_command
from .terminal import TerminalError, TerminalManager, session_id_for
from .registry import (
    abbreviate_path,
    add_account,
    assign_role,
    label_account,
    login_account,
    login_status,
    migrate_legacy_config,
    remove_account,
    rename_account,
    roles_for_account,
    swap_roles,
    unassign_role,
)
from .dashboard import DashboardServer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dual-codex")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Validate Codex, accounts, authentication, and repository")
    status = sub.add_parser("status", help="Show accounts, role assignments, and runtime status")
    status.add_argument("--json", action="store_true", dest="json_output")
    run = sub.add_parser("run", help="Run architect -> executor -> reviewer")
    run.add_argument("task", help="Markdown task file")

    dashboard = sub.add_parser("dashboard", help="Open the local account control dashboard")
    dashboard.add_argument("--port", type=int, default=0, help="Loopback port (0 chooses a safe free port)")
    dashboard.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")

    delegation = sub.add_parser("delegate", help="Delegate one implementation or correction to the executor role")
    request_input = delegation.add_mutually_exclusive_group(required=True)
    request_input.add_argument("--request-file", help="Versioned JSON delegation request")
    request_input.add_argument("--stdin", action="store_true", help="Read the JSON delegation request from standard input")
    delegation.add_argument("--result-file", required=True, help="Atomic JSON result path")
    delegation.add_argument("--repository", help="Explicit target repository override")
    delegation.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Explicitly allow a dirty target repository for this run",
    )
    delegation.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Require and reuse the exact registered live native Executor TUI; never start or fall back",
    )

    terminal = sub.add_parser("terminal", help="Manage native Windows Codex terminal sessions")
    terminal_sub = terminal.add_subparsers(dest="terminal_command", required=True)
    terminal_list = terminal_sub.add_parser("list", help="List active native terminal sessions")
    terminal_list.add_argument("--json", action="store_true", dest="json_output")
    terminal_start = terminal_sub.add_parser("start", help="Start a persistent Codex TUI session")
    terminal_start.add_argument("account")
    terminal_start.add_argument("--repository")
    terminal_start.add_argument("--role", choices=SUPPORTED_ROLES, default="executor")
    terminal_start.add_argument("--approval-policy", choices=("on-request", "never"), default="on-request")
    terminal_start.add_argument("--attach", action="store_true")
    terminal_send = terminal_sub.add_parser("send", help="Send a follow-up message to a session")
    terminal_send.add_argument("session_id")
    terminal_send.add_argument("message")
    terminal_attach = terminal_sub.add_parser("attach", help="Stream captured output from a session")
    terminal_attach.add_argument("session_id")
    terminal_attach.add_argument("--lines", type=int, default=80)
    terminal_attach.add_argument("--interactive", action="store_true", help="Attach to the live ConPTY with raw key forwarding")
    terminal_terminate = terminal_sub.add_parser("terminate", help="Stop a session")
    terminal_terminate.add_argument("session_id")

    migrate = sub.add_parser("migrate-config", help="Migrate fixed profiles to the account registry")
    migrate.add_argument("--architect-name")
    migrate.add_argument("--executor-name")
    migrate.add_argument("--architect-label")
    migrate.add_argument("--executor-label")
    migrate.add_argument("--dry-run", action="store_true")

    account = sub.add_parser("account", help="Manage authenticated Codex accounts")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    add = account_sub.add_parser("add", help="Register and authenticate an account")
    add.add_argument("name", nargs="?")
    add.add_argument("--label")
    add.add_argument("--codex-home")
    add.add_argument("--model", default="")
    add.add_argument("--reasoning-effort", default="high")
    add.add_argument("--role", action="append", dest="roles")
    login = account_sub.add_parser("login", help="Authenticate an existing account")
    login.add_argument("name")
    login.add_argument("--yes", action="store_true", help="Confirm replacing an existing login")
    account_sub.add_parser("list", help="List accounts and assigned roles")
    rename = account_sub.add_parser("rename", help="Rename an account without reauthentication")
    rename.add_argument("old_name")
    rename.add_argument("new_name")
    label = account_sub.add_parser("label", help="Change an account display label")
    label.add_argument("name")
    label.add_argument("label")
    remove = account_sub.add_parser("remove", help="Remove an account registry entry")
    remove.add_argument("name")
    remove.add_argument("--delete-profile", action="store_true")
    remove.add_argument("--confirm-delete", action="store_true")

    role = sub.add_parser("role", help="Manage role assignments")
    role_sub = role.add_subparsers(dest="role_command", required=True)
    role_sub.add_parser("list", help="List role assignments")
    assign = role_sub.add_parser("assign", help="Assign a role to an account")
    assign.add_argument("role")
    assign.add_argument("account")
    unassign = role_sub.add_parser("unassign", help="Unassign a role")
    unassign.add_argument("role")
    swap = role_sub.add_parser("swap", help="Swap two role assignments")
    swap.add_argument("role_a")
    swap.add_argument("role_b")
    return parser


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _display_account_table(config) -> None:
    rows = [("Account", "Label", "Backend", "CODEX_HOME", "Login", "Roles")]
    for name, account in config.accounts.items():
        rows.append(
            (
                name,
                _one_line(account.label) or "(none)",
                account.backend,
                abbreviate_path(account.codex_home),
                login_status(config, account),
                ", ".join(roles_for_account(config.roles, name)) or "(none)",
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for row_index, row in enumerate(rows):
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())
        if row_index == 0:
            print("  ".join("-" * width for width in widths).rstrip())


def _codex_details(config) -> tuple[str, str]:
    executable = shutil.which(config.codex_command)
    if executable is None and not Path(config.codex_command).exists():
        return "not found", "unknown"
    try:
        result = run_command(
            [config.codex_command, "--version"],
            cwd=config.project_root,
            check=False,
        )
        version = _one_line(result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else "unknown"
        return executable or config.codex_command, version
    except OSError:
        return executable or config.codex_command, "unknown"


def _git_state(config) -> str:
    try:
        ensure_git_repository(config.repository)
        return "clean" if not status_porcelain(config.repository).strip() else "dirty"
    except Exception as exc:
        return f"unavailable ({_one_line(str(exc))})"


def _status_payload(config) -> dict:
    accounts = []
    for name, account in config.accounts.items():
        accounts.append(
            {
                "name": name,
                "label": account.label,
                "backend": account.backend,
                "codex_home": abbreviate_path(account.codex_home),
                "login": login_status(config, account),
                "roles": roles_for_account(config.roles, name),
                "configured_model": account.model,
                "configured_reasoning": account.reasoning_effort,
                "configured_service_tier": account.service_tier,
            }
        )
    try:
        executor = config.account_for_role("executor")
        executor_status = {
            "name": executor.name,
            "label": executor.label,
            "backend": executor.backend,
            "login": login_status(config, executor),
        }
    except ConfigError as exc:
        executor_status = {"name": "", "label": "", "login": "UNASSIGNED", "error": str(exc)}
    codex_path, version = _codex_details(config)
    return {
        "schema_version": 1,
        "accounts": accounts,
        "roles": dict(config.roles),
        "executor": executor_status,
        "repository": str(config.repository),
        "git_state": _git_state(config),
        "codex_cli": {"path": codex_path, "version": version},
        "config": str(config.config_path),
    }


def _show_status(config, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(_status_payload(config), ensure_ascii=False, indent=2), flush=True)
        return
    print("Dual Codex Status")
    print()
    _display_account_table(config)
    codex_path, version = _codex_details(config)
    print()
    print(f"Repository: {config.repository}")
    print(f"Git state: {_git_state(config)}")
    print(f"Codex CLI: {codex_path}")
    print(f"Codex CLI version: {version}")
    print(f"Config: {config.config_path}")
    if config.legacy:
        print("Warning: legacy [architect]/[executor] configuration detected; run dual-codex migrate-config.")


def _show_roles(config) -> None:
    roles = dict(config.roles)
    for role in SUPPORTED_ROLES:
        account = roles.get(role, "(unassigned)")
        suffix = " (fallback to architect)" if role == "reviewer" and role not in roles else ""
        print(f"{role}: {account}{suffix}")
    for role in sorted(set(roles) - set(SUPPORTED_ROLES)):
        print(f"{role}: {roles[role]}")


def _account_command(args, config) -> None:
    if args.account_command == "add":
        name = args.name or input("Stable account name: ").strip()
        label = args.label
        if label is None:
            label = input("Friendly label (optional): ").strip()
        add_account(
            config,
            name,
            label=label,
            codex_home=args.codex_home,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            roles=args.roles,
        )
        print(f"Registered account '{name}'.")
    elif args.account_command == "login":
        login_account(config, args.name, assume_yes=args.yes)
        print(f"Login verified for account '{args.name}'.")
    elif args.account_command == "list":
        _display_account_table(config)
        if config.legacy:
            print("Warning: legacy configuration; migrate before changing registry entries.")
    elif args.account_command == "rename":
        rename_account(config, args.old_name, args.new_name)
        print(f"Renamed account '{args.old_name}' to '{args.new_name}'.")
    elif args.account_command == "label":
        label_account(config, args.name, args.label)
        print(f"Updated label for account '{args.name}'.")
    elif args.account_command == "remove":
        remove_account(
            config,
            args.name,
            delete_profile=args.delete_profile,
            confirm_delete=args.confirm_delete,
        )
        print(f"Removed account '{args.name}' from the registry.")


def _role_command(args, config) -> None:
    if args.role_command == "list":
        _show_roles(config)
    elif args.role_command == "assign":
        previous, current = assign_role(config, args.role, args.account)
        print(f"Role '{args.role}': {previous or '(unassigned)'} -> {current}")
    elif args.role_command == "unassign":
        previous = unassign_role(config, args.role)
        print(f"Role '{args.role}': {previous or '(unassigned)'} -> (unassigned)")
    elif args.role_command == "swap":
        previous = swap_roles(config, args.role_a, args.role_b)
        print(
            f"Swapped '{args.role_a}' and '{args.role_b}': "
            f"{previous[0] or '(unassigned)'} / {previous[1] or '(unassigned)'} -> "
            f"{config.roles.get(args.role_b, '(unassigned)')} / {config.roles.get(args.role_a, '(unassigned)')}"
        )


def _account_agent(config, account_name: str, role: str) -> AgentConfig:
    account = config.accounts.get(account_name)
    if account is None:
        raise ConfigError(f"Unknown account '{account_name}'.")
    return AgentConfig(
        codex_home=account.codex_home,
        model=account.model,
        reasoning_effort=account.reasoning_effort,
        sandbox="workspace-write" if role == "executor" else "read-only",
        account_name=account.name,
        label=account.label,
        backend=account.backend,
        service_tier=account.service_tier,
    )


def _interactive_attach(manager: TerminalManager, session_id: str) -> None:
    if sys.platform != "win32":
        raise TerminalError("Interactive ConPTY attach requires Windows.")
    import msvcrt

    attached = manager.attach_interactive(session_id)
    owner = str(attached["owner"])
    cursor = 0
    offset = 0
    try:
        if attached["viewer_only"]:
            print("Attached viewer-only: terminal input is leased by automation.", file=sys.stderr, flush=True)
        print("Interactive attach active; press Ctrl-] to detach safely.", file=sys.stderr, flush=True)
        while manager.status(session_id).get("state") == "running":
            packet = manager.read_since(session_id, cursor, offset=offset)
            if packet.get("behind_cursor"):
                print("[live output cursor fell behind; replaying retained output]", file=sys.stderr, flush=True)
                cursor = max(0, int(packet.get("oldest_seq", 1)) - 1)
                offset = 0
                continue
            text = str(packet.get("output", ""))
            if text:
                print(text, end="", flush=True)
            cursor = int(packet.get("next_seq", cursor))
            offset = int(packet.get("next_offset", 0))
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key == "\x1d":
                    return
                if key in {"\x00", "\xe0"}:
                    extended = msvcrt.getwch()
                    key = {"H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C"}.get(extended, "")
                if not attached["viewer_only"] and key:
                    try:
                        manager.write_input(session_id, key, owner)
                    except TerminalError:
                        attached["viewer_only"] = True
                        print("[terminal input lease lost; continuing viewer-only]", file=sys.stderr, flush=True)
            time.sleep(float(attached["poll_seconds"]))
    finally:
        manager.detach_interactive(session_id, owner)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        config_path = Path(args.config)
        if args.command == "migrate-config":
            migrate_legacy_config(
                config_path,
                architect_name=args.architect_name,
                executor_name=args.executor_name,
                architect_label=args.architect_label,
                executor_label=args.executor_label,
                dry_run=args.dry_run,
            )
            return 0

        config = load_config(config_path)
        if args.command == "doctor":
            checks = run_doctor(config)
            for check in checks:
                marker = "OK" if check.ok else "FAIL"
                print(f"[{marker}] {check.name}: {check.details}")
            return 0 if all(item.ok for item in checks) else 1
        if args.command == "status":
            _show_status(config, json_output=args.json_output)
            return 0
        if args.command == "dashboard":
            if args.port < 0 or args.port > 65535:
                raise ValueError("Dashboard port must be between 0 and 65535.")
            server = DashboardServer(config, port=args.port)
            url = server.start()
            print(f"Dashboard: {url}", flush=True)
            if not args.no_open:
                server.open_browser()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.shutdown()
            return 0
        if args.command == "terminal":
            manager = TerminalManager(config)
            if args.terminal_command == "list":
                rows = manager.list()
                if args.json_output:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    print("Session  Account  Role  State  Repository")
                    for row in rows:
                        print(f"{row['session_id']}  {row['account']}  {row['role']}  {row['state']}  {row['repository']}")
                return 0
            if args.terminal_command == "start":
                repository = Path(args.repository).expanduser().resolve() if args.repository else config.repository
                account = config.accounts.get(args.account)
                if account is None:
                    raise ConfigError(f"Unknown account '{args.account}'.")
                agent = _account_agent(config, args.account, args.role)
                session = manager.start(
                    session_id=session_id_for(account.name, repository),
                    agent=agent,
                    role=args.role,
                    repository=repository,
                    approval_policy=args.approval_policy,
                )
                print(json.dumps(session.as_dict(), ensure_ascii=False))
                if args.attach:
                    while manager.status(session.session_id).get("state") == "running":
                        text = manager.read(session.session_id)
                        if text:
                            print(text, end="", flush=True)
                        time.sleep(0.5)
                return 0
            if args.terminal_command == "send":
                manager.send(args.session_id, args.message)
                return 0
            if args.terminal_command == "attach":
                if args.interactive:
                    _interactive_attach(manager, args.session_id)
                    return 0
                print(manager.read(args.session_id, args.lines), end="")
                return 0
            if args.terminal_command == "terminate":
                manager.terminate(args.session_id)
                return 0
        if args.command == "delegate":
            stdin_text = sys.stdin.read() if args.stdin else None
            outcome = delegate(
                config,
                request_file=Path(args.request_file) if args.request_file else None,
                stdin_text=stdin_text,
                result_file=Path(args.result_file),
                repository_override=args.repository,
                allow_dirty=args.allow_dirty,
                reuse_existing=args.reuse_existing,
                output=lambda message: print(message, flush=True),
            )
            print(
                "DUAL_CODEX_RESULT "
                + json.dumps(
                    {
                        "status": outcome.status,
                        "request_id": outcome.request_id,
                        "result_file": str(outcome.result_file),
                        "run_directory": str(outcome.run_directory or ""),
                        "elapsed_seconds": round(outcome.elapsed_seconds, 3),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0 if outcome.status == "completed" else 1
        if args.command == "account":
            _account_command(args, config)
            return 0
        if args.command == "role":
            _role_command(args, config)
            return 0

        outcome = execute(config, Path(args.task))
        print(f"Run directory: {outcome.run_dir}")
        print(f"Verdict: {outcome.verdict}")
        print(f"Correction cycles: {outcome.correction_cycles}")
        return 0 if outcome.verdict == "approved" else 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (ConfigError, RuntimeError, TerminalError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
