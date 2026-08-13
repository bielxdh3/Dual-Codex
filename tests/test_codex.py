from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dual_codex.codex import run_codex_exec, run_codex_terminal
from dual_codex.config import AgentConfig
from dual_codex.process import CommandResult, _prepare_command
from dual_codex.terminal import TerminalError


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
    def test_terminal_capture_normalizes_executor_result_before_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "target"
            repository.mkdir()
            output_path = root / "report.json"
            session = SimpleNamespace(
                session_id="biel4-test",
                account="biel4",
                role="executor",
                repository=repository,
                codex_home=root / "profile",
                pid=123,
                process_started_at=1.0,
                pipe=r"\\.\pipe\dual-codex-biel4-test",
            )
            modern_result = {
                "summary": "Executor completed.",
                "status": "completed",
                "repository": str(repository),
                "files_changed": ["README.md"],
                "commands_run": ["pytest -q"],
                "tests": {
                    "command": "pytest -q",
                    "result": "passed",
                    "exit_code": 0,
                    "tests_run": 1,
                },
                "remaining_issues": [],
            }
            with patch("dual_codex.terminal.TerminalManager") as manager_type:
                manager = manager_type.return_value
                manager._load.return_value = session
                manager.status.return_value = {"state": "running", "alive": True}
                manager.ensure.return_value = session
                manager.turn_cursor.return_value = (None, 0)
                manager.send.return_value = {"state": "turn_started"}
                manager.wait_for_turn.return_value = {
                    "state": "completed",
                    "assistant": json.dumps(modern_result),
                    "session_id": "codex-session",
                }

                result = run_codex_terminal(
                    config=object(),
                    agent=_agent("workspace-write"),
                    repository=repository,
                    prompt="implement",
                    output_path=output_path,
                    session_id="biel4-test",
                    reuse_existing=True,
                )

            self.assertEqual(result.returncode, 0)
            self.assertNotIn("visible", manager.ensure.call_args.kwargs)
            captured = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(captured),
                {"summary", "files_changed", "commands_run", "tests", "remaining_issues"},
            )
            self.assertEqual(captured["tests"][0]["status"], "passed")

    def test_semantic_report_cannot_overwrite_trusted_target_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "target"
            repository.mkdir()
            output_path = root / "report.json"
            session = SimpleNamespace(
                session_id="biel4-test",
                account="biel4",
                label="biel4 Executor",
                role="executor",
                repository=repository,
                codex_home=root / "biel4-profile",
                pid=123,
                process_started_at=1.0,
                process_epoch="target-epoch",
                process_start_identity="target-start",
                session_file=str(root / "session.json"),
                repository_identity="target-repository",
                codex_home_identity="target-home",
                pipe=r"\\.\pipe\dual-codex-biel4-test",
                viewer_pid=456,
                viewer_epoch="target-viewer-epoch",
            )
            report = {
                "summary": "role=architect profile=caller; read-only probe completed",
                "files_changed": [],
                "commands_run": [],
                "tests": [],
                "remaining_issues": [],
            }
            with patch("dual_codex.terminal.TerminalManager") as manager_type:
                manager = manager_type.return_value
                manager._load.return_value = session
                manager.status.return_value = {
                    "state": "running",
                    "alive": True,
                    "pid": 789,
                    "host_pid": 123,
                    "viewer": {"attached": True},
                }
                manager.ensure.return_value = session
                manager.turn_cursor.return_value = (None, 0)
                manager.send.return_value = {"state": "turn_started"}
                manager.wait_for_turn.return_value = {
                    "state": "completed",
                    "assistant": json.dumps(report),
                    "session_id": "target-codex-session",
                }
                result = run_codex_terminal(
                    config=object(),
                    agent=_agent("workspace-write"),
                    repository=repository,
                    prompt="probe",
                    output_path=output_path,
                    session_id="biel4-test",
                    reuse_existing=True,
                )
            provenance = result.metadata["reuse_provenance"]
            self.assertEqual(provenance["account"], "biel4")
            self.assertEqual(provenance["account_label"], "biel4 Executor")
            self.assertEqual(provenance["role"], "executor")
            self.assertNotIn("architect", json.dumps(provenance).casefold())
            self.assertNotIn("caller", json.dumps(provenance).casefold())
            self.assertEqual(provenance["target_model"], "unknown")
            self.assertEqual(provenance["target_reasoning"], "unknown")

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
            self.assertEqual(run_mock.call_args.kwargs["stdin"], "implement")

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
            target = Path(temp) / "target with spaces"
            prepared = _prepare_command(
                [str(launcher), "exec", "--sandbox", "workspace-write", "--add-dir", str(target)]
            )

            self.assertIsInstance(prepared, str)
            self.assertIn("cmd.exe /d /s /v:off /c", prepared.casefold())
            self.assertIn('"' + str(target) + '"', prepared)

    @unittest.skipUnless(os.name == "nt", "Windows launcher regression")
    def test_windows_cmd_launcher_rejects_control_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "codex.cmd"
            launcher.write_text("@echo off\n", encoding="utf-8")
            for metacharacter in ("&", "|", "<", ">", "^", "(", ")", "%", "!", "\r", "\n"):
                with self.subTest(metacharacter=repr(metacharacter)):
                    with self.assertRaisesRegex(ValueError, "control characters"):
                        _prepare_command([str(launcher), "exec", f"target{metacharacter}value"])

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

    def test_strict_reuse_refuses_to_start_a_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "task.md"
            artifact.write_text("task\n", encoding="utf-8")
            with patch("dual_codex.terminal.TerminalManager") as manager_type:
                manager = manager_type.return_value
                manager._load.side_effect = TerminalError("Unknown terminal session 'test-session'.")
                result = run_codex_terminal(
                    config=object(),
                    agent=_agent("workspace-write"),
                    repository=root,
                    prompt="Read the task artifact.",
                    output_path=root / "report.json",
                    session_id="test-session",
                    task_artifact_path=artifact,
                    task_sha256="a" * 64,
                    reuse_existing=True,
                )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Strict reuse requires an existing terminal session", result.stderr)
            manager.ensure.assert_not_called()
            manager.start.assert_not_called()
            self.assertTrue(result.metadata["reuse_existing"])


if __name__ == "__main__":
    unittest.main()
