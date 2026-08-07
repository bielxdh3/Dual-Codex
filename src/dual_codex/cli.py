from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import load_config
from .doctor import run_doctor
from .orchestrator import execute


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dual-codex")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Validate Codex, profiles, authentication, and repository")
    run = sub.add_parser("run", help="Run architect → executor → reviewer")
    run.add_argument("task", help="Markdown task file")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        config = load_config(Path(args.config))
        if args.command == "doctor":
            checks = run_doctor(config)
            for check in checks:
                marker = "OK" if check.ok else "FAIL"
                print(f"[{marker}] {check.name}: {check.details}")
            return 0 if all(item.ok for item in checks) else 1

        outcome = execute(config, Path(args.task), progress=print)
        print(f"Run directory: {outcome.run_dir}")
        print(f"Verdict: {outcome.verdict}")
        print(f"Correction cycles: {outcome.correction_cycles}")
        return 0 if outcome.verdict == "approved" else 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
