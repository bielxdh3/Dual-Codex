from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import json
import hashlib
import os
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.config import load_config
from dual_codex.delegation import (
    InvalidRequestError,
    RepositoryLock,
    _pid_alive,
    delegate,
    parse_request,
    run_codex_exec as run_delegation_codex_exec,
    _read_report,
    TASK_CONTROL_MESSAGE_MAX,
)
from dual_codex.cli import main
from dual_codex.live_events import journal_path, read_journal
from dual_codex.process import CommandResult, run_command
from dual_codex.terminal import TERMINAL_INLINE_MESSAGE_MAX, session_id_for


def _git(repository: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=repository, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)


def _make_repository(root: Path, name: str = "target") -> Path:
    repository = root / name
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Dual Codex Test")
    (repository / "README.md").write_text("clean\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "--quiet", "-m", "initial")
    return repository


def _make_config(root: Path, repository: Path, *, clean: bool = True):
    config_path = root / "config.toml"
    profile_root = root / "Codex Profiles"
    config_path.write_text(
        "\n".join(
            [
                "[orchestrator]",
                f"repository = {json.dumps(str(repository))}",
                'runs_dir = "runs"',
                f"require_clean_git = {'true' if clean else 'false'}",
                'codex_command = "codex"',
                "",
                "[accounts.architect]",
                'label = "Visible"',
                f"codex_home = {json.dumps(str(profile_root / 'architect'))}",
                'model = ""',
                'reasoning_effort = "high"',
                "",
                "[accounts.executor]",
                'label = "Hidden executor"',
                f"codex_home = {json.dumps(str(profile_root / 'executor'))}",
                'model = ""',
                'reasoning_effort = "medium"',
                "",
                "[roles]",
                'orchestrator = "architect"',
                'architect = "architect"',
                'reviewer = "architect"',
                'executor = "executor"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return load_config(config_path)


def _request(repository: Path, *, action: str = "implement") -> dict:
    return {
        "schema_version": 1,
        "request_id": "req-001",
        "action": action,
        "repository": str(repository),
        "task": "Implement the requested change.\n\nUse the real repository.",
        "constraints": ["Do not commit", "Do not modify unrelated files"],
        "context_files": [],
        "review_findings": (
            [{"severity": "blocking", "title": "Fix test", "details": "The test still fails."}]
            if action == "correct"
            else []
        ),
        "max_correction_cycles": 0,
        **({"parent_request_id": "req-original"} if action == "correct" else {}),
    }


class DelegationTests(unittest.TestCase):
    def test_terminal_adapter_drops_legacy_schema_and_check_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            expected = CommandResult(["codex", "--no-alt-screen"], 0, "", "")
            with patch("dual_codex.delegation.run_codex_terminal", return_value=expected) as terminal:
                result = run_delegation_codex_exec(
                    config=config,
                    agent=config.executor,
                    repository=repository,
                    prompt="implement",
                    output_path=root / "report.json",
                    schema_path=root / "schema.json",
                    check=False,
                    session_id="executor-test",
                    progress=None,
                )
            self.assertIs(result, expected)
            self.assertNotIn("schema_path", terminal.call_args.kwargs)
            self.assertNotIn("check", terminal.call_args.kwargs)
            self.assertEqual(
                terminal.call_args.kwargs["session_id"],
                session_id_for("executor", repository),
            )

    def test_legacy_adapter_keeps_schema_and_check_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = replace(_make_config(root, repository), codex_command="mock-codex.cmd")
            expected = CommandResult(["codex", "exec"], 0, "", "")
            with patch("dual_codex.delegation._run_codex_exec_legacy", return_value=expected) as legacy:
                result = run_delegation_codex_exec(
                    config=config,
                    agent=config.executor,
                    repository=repository,
                    prompt="implement",
                    output_path=root / "report.json",
                    schema_path=root / "schema.json",
                    check=True,
                    session_id="executor-test",
                    progress=None,
                )
            self.assertIs(result, expected)
            self.assertEqual(legacy.call_args.kwargs["schema_path"], root / "schema.json")
            self.assertFalse(legacy.call_args.kwargs["check"])

    def test_terminal_report_validation_rejects_invalid_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "executor-report.json"
            path.write_text(
                json.dumps({
                    "summary": "done",
                    "files_changed": [],
                    "commands_run": [],
                    "tests": [{"command": "python -m unittest", "status": "passed"}],
                    "remaining_issues": [],
                }),
                encoding="utf-8",
            )
            report, error = _read_report(path)
            self.assertIsNone(report)
            self.assertIn("schema validation failed", error)

    def test_valid_implement_uses_only_executor_and_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            request_file = root / "request.json"
            result_file = root / "result.json"
            request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")

            def run_executor(**kwargs):
                (repository / "src").mkdir()
                (repository / "src" / "example.py").write_text(
                    "def example():\n    return 1\n", encoding="utf-8"
                )
                kwargs["output_path"].write_text(
                    json.dumps(
                        {
                            "summary": "Executor finished",
                            "files_changed": ["src/example.py"],
                            "commands_run": ["python -m unittest"],
                            "tests": [{"command": "python -m unittest", "status": "passed", "details": "ok"}],
                            "remaining_issues": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return CommandResult(["codex", "exec"], 0, "executor stdout", "")

            progress: list[str] = []
            with patch("dual_codex.delegation.login_status", return_value="OK"), patch(
                "dual_codex.delegation.run_codex_exec", side_effect=run_executor
            ) as execute_mock:
                outcome = delegate(
                    config,
                    request_file=request_file,
                    result_file=result_file,
                    output=progress.append,
                )

            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(result["executor_account"], "executor")
            self.assertEqual(result["executor_label"], "Hidden executor")
            self.assertEqual(result["executor_sandbox"], "workspace-write")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["files_changed"], ["src/example.py"])
            artifact = Path(execute_mock.call_args.kwargs["task_artifact_path"])
            self.assertEqual(artifact.parent, (config.runs_dir / "executor-task-artifacts").resolve())
            control_message = execute_mock.call_args.kwargs["prompt"]
            artifact_text = artifact.read_text(encoding="utf-8")
            self.assertIn("Implement the requested change.", artifact_text)
            self.assertNotIn("Implement the requested change.\n\nUse the real repository.", control_message)
            self.assertNotRegex(control_message, r"[\r\n]")
            self.assertLessEqual(len(control_message), TASK_CONTROL_MESSAGE_MAX)
            self.assertEqual(result["task_transport"], "file")
            self.assertEqual(result["task_artifact"], str(artifact))
            self.assertEqual(result["task_sha256"], hashlib.sha256(artifact_text.encode("utf-8")).hexdigest())
            self.assertNotIn(str(config.accounts["executor"].codex_home), str(artifact))
            self.assertNotIn("OPENAI_API_KEY=", artifact_text)
            transport_metadata = json.loads((Path(result["run_directory"]) / "task-transport.json").read_text(encoding="utf-8"))
            self.assertEqual(transport_metadata["task_artifact"], str(artifact))
            self.assertEqual(execute_mock.call_count, 1)
            self.assertEqual(execute_mock.call_args.kwargs["agent"].account_name, "executor")
            self.assertEqual(
                execute_mock.call_args.kwargs["agent"].codex_home,
                config.accounts["executor"].codex_home,
            )
            self.assertIn("[3/5] Starting executor", "\n".join(progress))
            self.assertTrue(Path(result["diff_file"]).exists())
            events = read_journal(
                journal_path(
                    config.runs_dir,
                    account="executor",
                    role="executor",
                    repository=repository,
                )
            )
            self.assertEqual([event.state for event in events[-2:]], ["started", "completed"])
            self.assertEqual(events[-1].method, "run/completed")
            self.assertLess(events[-2].sequence, events[-1].sequence)

    def test_valid_correct_links_parent_and_includes_findings_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            (repository / "change.txt").write_text("current\n", encoding="utf-8")
            config = _make_config(root, repository, clean=False)
            request_file = root / "correct.json"
            result_file = root / "correct-result.json"
            request_file.write_text(json.dumps(_request(repository, action="correct")), encoding="utf-8")

            def run_executor(**kwargs):
                artifact = Path(kwargs["task_artifact_path"])
                artifact_text = artifact.read_text(encoding="utf-8")
                self.assertIn("parent request: req-original", artifact_text)
                self.assertIn("The test still fails.", artifact_text)
                self.assertIn("change.txt", artifact_text)
                self.assertNotRegex(kwargs["prompt"], r"[\r\n]")
                self.assertLessEqual(len(kwargs["prompt"]), TASK_CONTROL_MESSAGE_MAX)
                (repository / "corrected.txt").write_text("corrected\n", encoding="utf-8")
                kwargs["output_path"].write_text(
                    json.dumps(
                        {
                            "summary": "Correction finished",
                            "files_changed": ["corrected.txt"],
                            "commands_run": [],
                            "tests": [{"command": "validation", "status": "passed", "details": "ok"}],
                            "remaining_issues": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return CommandResult(["codex", "exec"], 0, "", "")

            with patch("dual_codex.delegation.login_status", return_value="OK"), patch(
                "dual_codex.delegation.run_codex_exec", side_effect=run_executor
            ):
                outcome = delegate(config, request_file=request_file, result_file=result_file)

            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(result["parent_request_id"], "req-original")
            self.assertEqual(result["executor_sandbox"], "workspace-write")

    def test_structured_report_with_read_only_blocker_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            request_file = root / "request.json"
            result_file = root / "result.json"
            request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")

            def blocked_executor(**kwargs):
                kwargs["output_path"].write_text(
                    json.dumps(
                        {
                            "summary": "Implementation could not be applied because workspace is read-only.",
                            "files_changed": [],
                            "commands_run": ["apply_patch"],
                            "tests": [{"command": "python -m unittest", "status": "not_run", "details": "blocked"}],
                            "remaining_issues": ["Permission denied by sandbox."],
                        }
                    ),
                    encoding="utf-8",
                )
                return CommandResult(["codex", "exec"], 0, "", "apply_patch rejected by read-only sandbox")

            with patch("dual_codex.delegation.login_status", return_value="OK"), patch(
                "dual_codex.delegation.run_codex_exec", side_effect=blocked_executor
            ):
                outcome = delegate(config, request_file=request_file, result_file=result_file)

            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(result["status"], "failed")
            self.assertIn("not applied", result["error"])
            self.assertEqual(result["executor_sandbox"], "workspace-write")

    def test_valid_json_without_repository_change_is_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            request_file = root / "request.json"
            result_file = root / "result.json"
            request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")

            def no_op_executor(**kwargs):
                kwargs["output_path"].write_text(
                    json.dumps(
                        {
                            "summary": "Executor finished",
                            "files_changed": [],
                            "commands_run": ["python -m unittest"],
                            "tests": [{"command": "python -m unittest", "status": "passed", "details": "ok"}],
                            "remaining_issues": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return CommandResult(["codex", "exec"], 0, "", "")

            with patch("dual_codex.delegation.login_status", return_value="OK"), patch(
                "dual_codex.delegation.run_codex_exec", side_effect=no_op_executor
            ):
                outcome = delegate(config, request_file=request_file, result_file=result_file)

            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(outcome.status, "failed")
            self.assertIn("did not apply any repository changes", result["error"])

    def test_request_validation_rejects_version_malformed_and_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            for bad in (
                {**_request(repository), "schema_version": 2},
                {**_request(repository), "action": "plan"},
                {key: value for key, value in _request(repository).items() if key != "task"},
            ):
                with self.assertRaises(InvalidRequestError):
                    parse_request(bad, config)

    def test_unassigned_and_unavailable_executor_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            config.config_path.write_text(
                config.config_path.read_text(encoding="utf-8").replace('executor = "executor"\n', ""),
                encoding="utf-8",
            )
            config = load_config(config.config_path)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")
            result_file = root / "unassigned.json"
            outcome = delegate(config, request_file=request_file, result_file=result_file)
            self.assertEqual(outcome.status, "executor_unavailable")

            config = _make_config(root, repository)
            with patch("dual_codex.delegation.login_status", return_value="NOT LOGGED IN"):
                outcome = delegate(config, request_file=request_file, result_file=root / "logged-out.json")
            self.assertEqual(outcome.status, "executor_unavailable")

    def test_dirty_repository_and_failed_executor_preserve_evidence_and_redact_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            config = _make_config(root, repository)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")
            outcome = delegate(config, request_file=request_file, result_file=root / "dirty.json")
            self.assertEqual(outcome.status, "failed")
            self.assertIn("uncommitted", json.loads((root / "dirty.json").read_text(encoding="utf-8"))["error"])

            config = _make_config(root, repository, clean=False)

            def failed_executor(**kwargs):
                return CommandResult(["codex", "exec"], 7, "", "TOKEN=do-not-write")

            with patch("dual_codex.delegation.login_status", return_value="OK"), patch(
                "dual_codex.delegation.run_codex_exec", side_effect=failed_executor
            ):
                outcome = delegate(config, request_file=request_file, result_file=root / "failed.json")
            result = json.loads((root / "failed.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome.status, "failed")
            self.assertNotIn("do-not-write", Path(result["stderr_file"]).read_text(encoding="utf-8"))

    def test_lock_rejects_live_request_and_recovers_dead_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository, clean=False)
            live = RepositoryLock(config.runs_dir, repository, "live-request")
            live.acquire()
            try:
                request_file = root / "request.json"
                request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")
                with patch("dual_codex.delegation.login_status", return_value="OK"):
                    outcome = delegate(config, request_file=request_file, result_file=root / "locked.json")
                self.assertEqual(outcome.status, "failed")
                self.assertIn("already delegated", json.loads((root / "locked.json").read_text(encoding="utf-8"))["error"])
            finally:
                live.release()

            stale = RepositoryLock(config.runs_dir, repository, "stale-request")
            stale.path.parent.mkdir(parents=True, exist_ok=True)
            stale.path.write_text(json.dumps({"pid": 999999, "request_id": "stale-request"}), encoding="utf-8")
            with patch("dual_codex.delegation._pid_alive", return_value=False):
                stale.acquire()
            stale.release()

    def test_pid_liveness_recognizes_current_and_dead_processes(self) -> None:
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(999999))

    @unittest.skipUnless(os.name == "nt", "Windows process-query error semantics are platform-specific")
    def test_windows_pid_liveness_is_conservative_for_access_and_unknown_errors(self) -> None:
        with patch("dual_codex.delegation.ctypes.WinDLL") as factory, patch(
            "dual_codex.delegation.ctypes.get_last_error", return_value=5
        ):
            factory.return_value.OpenProcess.return_value = 0
            self.assertTrue(_pid_alive(12345))
        with patch("dual_codex.delegation.ctypes.WinDLL") as factory, patch(
            "dual_codex.delegation.ctypes.get_last_error", return_value=87
        ):
            factory.return_value.OpenProcess.return_value = 0
            self.assertFalse(_pid_alive(12345))
        with patch("dual_codex.delegation.ctypes.WinDLL") as factory, patch(
            "dual_codex.delegation.ctypes.get_last_error", return_value=1234
        ):
            factory.return_value.OpenProcess.return_value = 0
            self.assertTrue(_pid_alive(12345))

    def test_progress_heartbeat_runs_while_subprocess_is_alive(self) -> None:
        progress: list[str] = []
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(0.15)"],
            cwd=Path.cwd(),
            progress=progress.append,
            progress_interval=0.03,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(progress)

    def test_status_json_exposes_executor_without_auth_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            config = _make_config(root, repository)
            output = StringIO()
            with patch("dual_codex.cli.login_status", return_value="OK"), redirect_stdout(output):
                self.assertEqual(main(["--config", str(config.config_path), "status", "--json"]), 0)
            status = json.loads(output.getvalue())
            self.assertEqual(status["executor"]["name"], "executor")
            self.assertEqual(status["executor"]["login"], "OK")
            self.assertNotIn("auth.json", output.getvalue())

    def test_cli_end_to_end_with_disposable_mock_codex_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = _make_repository(root)
            mock_python = root / "mock codex.py"
            mock_cmd = root / "mock codex.cmd"
            mock_python.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "args = sys.argv[1:]",
                        "if args[:2] == ['login', 'status']:",
                        "    raise SystemExit(0)",
                        "if args == ['--version']:",
                        "    print('mock-codex 1.0')",
                        "    raise SystemExit(0)",
                        "if args and args[0] == 'exec':",
                        "    pathlib.Path('mock_change.txt').write_text('changed\\n', encoding='utf-8')",
                        "    output = pathlib.Path(args[args.index('--output-last-message') + 1])",
                        "    output.write_text(json.dumps({'summary': 'mock complete', 'files_changed': ['mock_change.txt'], 'commands_run': [], 'tests': [{'command': 'validation', 'status': 'passed', 'details': 'ok'}], 'remaining_issues': []}), encoding='utf-8')",
                        "    raise SystemExit(0)",
                        "raise SystemExit(2)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            mock_cmd.write_text(f'@echo off\n"{sys.executable}" "{mock_python}" %*\n', encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                _make_config(root, repository).config_path.read_text(encoding="utf-8").replace(
                    'codex_command = "codex"',
                    f"codex_command = {json.dumps(str(mock_cmd))}",
                ),
                encoding="utf-8",
            )
            request_file = root / "request.json"
            result_file = root / "result.json"
            request_file.write_text(json.dumps(_request(repository)), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "dual_codex.cli",
                    "--config",
                    str(config_path),
                    "delegate",
                    "--request-file",
                    str(request_file),
                    "--result-file",
                    str(result_file),
                ],
                cwd=Path.cwd(),
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("DUAL_CODEX_RESULT", completed.stdout)
            self.assertEqual(json.loads(result_file.read_text(encoding="utf-8"))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
