from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def render_markdown(
    *,
    task_file: Path,
    plan: dict[str, Any],
    implementation: dict[str, Any],
    review: dict[str, Any],
    correction_cycles: int,
) -> str:
    lines = [
        "# Dual Codex Run Report",
        "",
        f"- Task: `{task_file}`",
        f"- Verdict: **{review['verdict']}**",
        f"- Correction cycles: **{correction_cycles}**",
        "",
        "## Architect plan",
        "",
        plan["summary"],
        "",
    ]
    for index, step in enumerate(plan["steps"], start=1):
        lines.append(f"{index}. {step}")
    lines.extend(["", "## Implementation", "", implementation["summary"], ""])
    if implementation["files_changed"]:
        lines.append("### Files changed")
        lines.extend(f"- `{item}`" for item in implementation["files_changed"])
        lines.append("")
    lines.extend(["## Review", "", review["summary"], ""])
    for finding in review["findings"]:
        lines.extend(
            [
                f"### {finding['severity'].upper()}: {finding['title']}",
                "",
                finding["details"],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
