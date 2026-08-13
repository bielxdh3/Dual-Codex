from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


EXECUTOR_REPORT_FIELDS = frozenset(
    {"summary", "files_changed", "commands_run", "tests", "remaining_issues"}
)
EXECUTOR_REPORT_REQUIRED_WITHOUT_TELEMETRY = EXECUTOR_REPORT_FIELDS - {"commands_run"}


def normalise_executor_report(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalise only the optional command telemetry omission.

    Semantic fields and present values are deliberately left untouched so the
    strict validator can reject malformed or incomplete reports afterwards.
    """

    normalised = dict(value)
    if (
        set(normalised).issubset(EXECUTOR_REPORT_FIELDS)
        and EXECUTOR_REPORT_REQUIRED_WITHOUT_TELEMETRY.issubset(normalised)
        and "commands_run" not in normalised
    ):
        normalised["commands_run"] = []
    return normalised


def is_executor_report_shape(value: Mapping[str, Any]) -> bool:
    """Return whether a value is a canonical report after safe normalisation."""

    keys = set(value)
    return (
        keys.issubset(EXECUTOR_REPORT_FIELDS)
        and EXECUTOR_REPORT_FIELDS.issubset(keys)
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON object without leaving a partially written result."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        temporary.write_text(dump_json(data) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
