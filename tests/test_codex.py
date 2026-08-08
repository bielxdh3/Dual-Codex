from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.codex import run_codex_exec, run_codex_terminal
from dual_codex.config import AgentConfig
from dual_codex.process import CommandResult, _prepare_command


def _agent(sandbox: str) -> AgentConfig:
    return AgentConfig(
        codex_home=Path("C:/CodexProfiles/test"),
        model="",
        reasoning_effort="high",
        sandbox=sandbox,
        account_name="test-account",
        label="Test account",
    )


class CodexCommandTests(unittest.TestCase):
    def test_executor_command_explicitly_requests_workspace_write_and_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "target"
            repository.mkdir()
            output_path = root / "run" / "report.json"
            schema_path = root / "schema.json"
            expected = CommandResult(["codex"], 0, "", "")
            with patch("dual_codex.codex.run_command", return_value=expected) as run_mock:
                run_codex_exec(
                    codex_command="codex.CMD",
                    agent=_agent("workspace-write"),
                    repository=repository,
                    prompt="implement",
                    output_path=output_path,
                    schema_path=schema_path,
                )

            command = run_mock.call_args.args[0]
            self.assertIn("--sandbox", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
            self.assertIn("--add-dir", command)
            self.assertEqual(command[command.index("--add-dir") + 1], str(repository))
            self.assertIn('sandbox_mode="workspace-write"', command)
            self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_HOME"], str(Path("C:/CodexProfiles/test")))

    def test_read_only_agents_do_not_receive_writable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "target"
            repository.mkdir()
            expected = CommandResult(["codex"], 0, "", "")
            with patch("dual_codex.codex.run_command", return_value=expected) as run_mock:
                run_codex_exec(
                    codex_command="codex",
                    agent=_agent("read-only"),
                    repository=repository,
                    prompt="inspect",
                    output_path=root / "report.json",
                    schema_path=root / "schema.json",
                )

            command = run_mock.call_args.args[0]
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertNotIn("--add-dir", command)
            self.assertIn('sandbox_mode="read-only"', command)

    @unittest.skipUnless(os.name == "nt", "Windows launcher regression")
    def test_windows_cmd_launcher_preserves_sandbox_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "codex.cmd"
            launcher.write_text("@echo off\n", encoding="utf-8")
            prepared, use_shell = _prepare_command(
                [str(launcher), "exec", "--sandbox", "workspace-write", "--add-dir", str(Path(temp) / "target")]
            )

            self.assertTrue(use_shell)
            self.assertIsInstance(prepared, str)
            self.assertIn("--sandbox workspace-write", prepared)
            self.assertIn("--add-dir", prepared)

    def test_file_backed_terminal_reports_missing_artifact_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "task artifact.md"
            result = run_codex_terminal(
                config=object(),
                agent=_agent("workspace-write"),
                repository=root,
                prompt="Read the task artifact.",
                output_path=root / "report.json",
                session_id="test-session",
                task_artifact_path=missing,
                task_sha256="a" * 64,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(str(missing), result.stderr)
            self.assertEqual(result.metadata["task_transport"], "file")
            self.assertEqual(result.metadata["task_artifact"], str(missing.resolve()))


if __name__ == "__main__":
    unittest.main()
