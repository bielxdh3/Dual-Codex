from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from dual_codex.delegation import _read_report, _report_list


SCHEMA_ROOT = Path(__file__).parents[1] / "schemas"


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    expected_types = [expected] if isinstance(expected, str) else expected
    return any(
        {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "null": value is None,
        }.get(item, False)
        for item in expected_types
    )


def _validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    if "const" in schema and value != schema["const"]:
        raise AssertionError(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{path}: value is not in enum")
    if "type" in schema and not _matches_type(value, schema["type"]):
        raise AssertionError(f"{path}: wrong type")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise AssertionError(f"{path}: string is too short")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise AssertionError(f"{path}: missing {name}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise AssertionError(f"{path}: unexpected {sorted(unexpected)}")
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")


def _assert_array_items(schema: Any, path: str = "$") -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "array":
            if "items" not in schema:
                raise AssertionError(f"{path}: array schema has no items")
            _assert_array_items(schema["items"], f"{path}.items")
        for name, item in schema.items():
            if name != "items":
                _assert_array_items(item, f"{path}.{name}")
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            _assert_array_items(item, f"{path}[{index}]")


class SchemaTests(unittest.TestCase):
    def test_all_repository_array_schemas_define_items(self) -> None:
        for path in sorted(SCHEMA_ROOT.glob("*.schema.json")):
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                _assert_array_items(schema, path.name)

    def test_delegation_report_accepts_runtime_executor_report(self) -> None:
        schema = json.loads(
            (SCHEMA_ROOT / "delegation-report.schema.json").read_text(encoding="utf-8")
        )
        report = {
            "summary": "Implemented safe_divide",
            "files_changed": ["src/tiny_math/core.py"],
            "commands_run": ["python -m unittest discover -s tests -v"],
            "tests": [
                {
                    "command": "python -m unittest discover -s tests -v",
                    "status": "passed",
                    "details": "Ran 4 tests in 0.01s",
                }
            ],
            "remaining_issues": [],
        }
        _validate(report, schema)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "executor-report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            loaded, error = _read_report(path)
            self.assertEqual(error, "")
            self.assertEqual(_report_list(loaded or {}, "tests"), report["tests"])

    def test_delegation_report_rejects_malformed_test_entries(self) -> None:
        schema = json.loads(
            (SCHEMA_ROOT / "delegation-report.schema.json").read_text(encoding="utf-8")
        )
        base = {
            "summary": "done",
            "files_changed": [],
            "commands_run": [],
            "tests": [],
            "remaining_issues": [],
        }
        for malformed in (
            {"command": "python -m unittest", "status": "passed"},
            {"command": 123, "status": "passed", "details": "ok"},
            "not an object",
        ):
            with self.subTest(entry=malformed):
                invalid = {**base, "tests": [malformed]}
                with self.assertRaises(AssertionError):
                    _validate(invalid, schema)

    def test_delegation_request_requires_review_finding_details(self) -> None:
        schema = json.loads(
            (SCHEMA_ROOT / "delegation-request.schema.json").read_text(encoding="utf-8")
        )
        request = {
            "schema_version": 1,
            "request_id": "request-1",
            "action": "correct",
            "repository": "C:/Projects/example",
            "task": "Apply the review corrections.",
            "review_findings": [{"title": "Missing detail"}],
        }
        with self.assertRaises(AssertionError):
            _validate(request, schema)


if __name__ == "__main__":
    unittest.main()
