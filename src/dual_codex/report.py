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
_EXTENDED_REPORT_FIELDS = frozenset(
    {
        "summary",
        "status",
        "starting_sha",
        "final_sha",
        "files_changed",
        "behavior_changed",
        "validations_run",
        "validations_not_run",
        "remaining_limitations",
        "next_plan_tree_item",
        "commands_run",
        "tests",
        "remaining_issues",
        "push_result",
        "remote_result",
        "pr_result",
    }
)


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def _test_list(value: Any, *, status: str) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            result.append({"command": item, "status": status, "details": "reported by Executor"})
            continue
        if not isinstance(item, Mapping) or set(item) != {"command", "status", "details"}:
            return None
        if not all(isinstance(item[field], str) for field in ("command", "status", "details")):
            return None
        if item["status"] not in {"passed", "failed", "not_run"}:
            return None
        result.append({field: item[field] for field in ("command", "status", "details")})
    return result


def _normalise_extended_report(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adapt the known richer Executor result to the one canonical report shape."""

    if not set(value).issubset(_EXTENDED_REPORT_FIELDS):
        return None
    if not isinstance(value.get("status"), str) or not value["status"].strip():
        return None
    for field in (
        "starting_sha",
        "final_sha",
        "behavior_changed",
        "next_plan_tree_item",
        "summary",
        "push_result",
        "remote_result",
        "pr_result",
    ):
        if field in value and not isinstance(value[field], str):
            return None
    if not any(
        field in value
        for field in (
            "starting_sha",
            "final_sha",
            "behavior_changed",
            "validations_run",
            "validations_not_run",
            "remaining_limitations",
            "push_result",
            "remote_result",
            "pr_result",
        )
    ):
        return None
    files_changed = _string_list(value.get("files_changed"))
    commands_run = _string_list(value.get("commands_run", []))
    if files_changed is None or commands_run is None:
        return None
    validations_run = _test_list(value.get("validations_run", []), status="passed")
    validations_not_run = _test_list(value.get("validations_not_run", []), status="not_run")
    limitations = _string_list(value.get("remaining_limitations", []))
    remaining_issues = _string_list(value.get("remaining_issues", []))
    if validations_run is None or validations_not_run is None or limitations is None or remaining_issues is None:
        return None
    tests = _test_list(value.get("tests", []), status="passed")
    if tests is None:
        return None
    summary = value.get("summary")
    if summary is None:
        status = value["status"]
        behavior = value.get("behavior_changed", "")
        summary = f"{status}: {behavior}".rstrip(": ")
    if not isinstance(summary, str):
        return None
    remaining_issues.extend(limitations)
    for field in ("push_result", "remote_result", "pr_result"):
        result = value.get(field)
        if result is not None and not isinstance(result, str):
            return None
        if result and result.casefold() not in {"passed", "completed", "updated", "not attempted", "not checked", "not updated"}:
            remaining_issues.append(f"{field}: {result}")
    return {
        "summary": summary,
        "files_changed": files_changed,
        "commands_run": commands_run,
        "tests": [*tests, *validations_run, *validations_not_run],
        "remaining_issues": remaining_issues,
    }


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
    extended = _normalise_extended_report(normalised)
    if extended is not None:
        return extended
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
