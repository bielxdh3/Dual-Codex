from __future__ import annotations

import json
from pathlib import Path
import unittest

from dual_codex.app_server import _normalise_report
from dual_codex.codex import _report_from_message
from dual_codex.report import render_markdown


class ReportTests(unittest.TestCase):
    def test_blocked_extended_report_is_canonical_across_app_server_and_tui_adapters(self) -> None:
        rich = {
            "status": "BLOCKED_NO_MUTATION",
            "starting_sha": "UNKNOWN",
            "final_sha": "UNKNOWN",
            "files_changed": [],
            "behavior_changed": "None",
            "validations_run": [],
            "validations_not_run": ["remote verification"],
            "remaining_limitations": ["Remote mutation was not attempted."],
            "next_plan_tree_item": "None",
            "commands_run": [],
            "push_result": "Not attempted",
            "remote_result": "Not checked",
            "pr_result": "Not updated",
        }
        payload = json.dumps(rich)
        app_server_report = json.loads(_normalise_report(payload))
        tui_report = _report_from_message(payload)
        self.assertEqual(tui_report, app_server_report)
        self.assertEqual(set(app_server_report), {"summary", "files_changed", "commands_run", "tests", "remaining_issues"})
        self.assertEqual(app_server_report["tests"][0]["status"], "not_run")

    def test_render_contains_verdict(self) -> None:
        text = render_markdown(
            task_file=Path("task.md"),
            plan={
                "summary": "Plan summary",
                "steps": ["Inspect", "Implement"],
                "acceptance_criteria": [],
                "risks": [],
                "files_to_inspect": [],
            },
            implementation={
                "summary": "Done",
                "files_changed": ["src/a.py"],
                "commands_run": [],
                "tests": [],
                "remaining_issues": [],
            },
            review={"verdict": "approved", "summary": "Looks good", "findings": []},
            correction_cycles=0,
        )
        self.assertIn("Verdict: **approved**", text)
        self.assertIn("`src/a.py`", text)


if __name__ == "__main__":
    unittest.main()
