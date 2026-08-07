from __future__ import annotations

from pathlib import Path
import unittest

from dual_codex.report import render_markdown


class ReportTests(unittest.TestCase):
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
