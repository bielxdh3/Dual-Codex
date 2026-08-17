from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.config import AccountConfig, AgentConfig, OrchestratorConfig
from dual_codex.orchestrator import execute
from dual_codex.process import CommandResult


class OrchestratorTests(unittest.TestCase):
    def test_mission_dispatches_architect_and_app_server_executor_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-mission"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            accounts = {
                "biel3": AccountConfig(
                    name="biel3",
                    label="Architect",
                    codex_home=root / "architect-home",
                    model="",
                    reasoning_effort="high",
                    backend="windows",
                ),
                "biel4": AccountConfig(
                    name="biel4",
                    label="Executor",
                    codex_home=root / "executor-home",
                    model="",
                    reasoning_effort="high",
                    backend="app_server",
                    network_access=True,
                ),
            }
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=True,
                codex_command="codex",
                accounts=accounts,
                roles={
                    "architect": "biel3",
                    "executor": "biel4",
                    "reviewer": "biel3",
                },
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("Read this harmless mission brief.", encoding="utf-8")
            seen: list[tuple[str, str, str]] = []

            def fake_runner(**kwargs):
                role = kwargs["role"]
                agent: AgentConfig = kwargs["agent"]
                seen.append((role, agent.account_name, agent.backend))
                self.assertIn("harmless mission brief", kwargs["prompt"])
                if role == "architect":
                    payload = {
                        "summary": "plan",
                        "steps": [],
                        "acceptance_criteria": [],
                        "risks": [],
                        "files_to_inspect": [],
                    }
                elif role == "executor":
                    payload = {
                        "summary": "implementation",
                        "files_changed": [],
                        "commands_run": [],
                        "tests": [],
                        "remaining_issues": [],
                    }
                else:
                    payload = {"verdict": "approved", "summary": "approved", "findings": []}
                import json

                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                return CommandResult(["codex"], 0, "", "")

            with patch("dual_codex.orchestrator.run_codex_for_role", side_effect=fake_runner):
                execute(config, task)

            self.assertEqual(
                seen,
                [
                    ("architect", "biel3", "windows"),
                    ("executor", "biel4", "app_server"),
                    ("reviewer", "biel3", "windows"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
