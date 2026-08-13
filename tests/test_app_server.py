from __future__ import annotations

import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dual_codex.app_server import (
    AppServerError,
    _PROCESSES,
    _normalise_report,
    _process_key,
    _sanitize_stderr,
    app_server_call,
    run_codex_app_server,
)
from dual_codex.codex import _report_from_message
from dual_codex.config import AgentConfig, OrchestratorConfig
from dual_codex.live_events import read_journal


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process

    def write(self, value: str) -> None:
        for line in value.splitlines():
            self.process.handle(json.loads(line))

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    next_pid = 4100

    def __init__(self, *args, **kwargs) -> None:
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.stdout = _FakeStdout()
        self.stderr = _FakeStdout()
        self.stdin = _FakeStdin(self)
        self.returncode = None
        self.prompts: list[str] = []
        self.thread_id = "thread-probe"
        self.turn_number = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0
        self.stdout.lines.put(None)

    def kill(self):
        self.terminate()

    def _emit(self, message: dict) -> None:
        self.stdout.lines.put(json.dumps(message) + "\n")

    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "codexHome": "C:/CodexProfiles/executor",
                        "platformFamily": "windows",
                        "platformOs": "windows",
                        "userAgent": "Codex Desktop/test",
                    },
                }
            )
        elif method == "thread/start":
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": self.thread_id}}})
        elif method == "thread/resume":
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": self.thread_id}}})
        elif method == "turn/start":
            self.turn_number += 1
            turn_id = f"turn-{self.turn_number}"
            text = message["params"]["input"][0]["text"]
            self.prompts.append(text)
            report = {
                "summary": "probe",
                "files_changed": ["probe.txt"],
                "commands_run": [],
                "tests": [],
                "remaining_issues": [],
            }
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": {"turn": {"id": turn_id}}})
            self._emit({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": self.thread_id, "turn": {"id": turn_id}}})
            self._emit({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": self.thread_id, "turn": {"id": turn_id, "status": "completed", "items": [{"type": "agentMessage", "text": json.dumps(report)}]}}})


def _config(root: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        repository=root / "repo",
        runs_dir=root / "runs",
        max_correction_cycles=1,
        require_clean_git=True,
        codex_command="codex",
        accounts={},
        roles={},
        project_root=root,
        config_path=root / "config.toml",
        app_server_turn_timeout=5,
    )


class AppServerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for process in list(_PROCESSES.values()):
            process.close()
        _PROCESSES.clear()

    def test_structured_turns_reuse_thread_and_clear_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                label="executor",
                backend="app_server",
            )
            fake_processes: list[_FakeProcess] = []

            def create(*args, **kwargs):
                fake = _FakeProcess(*args, **kwargs)
                fake_processes.append(fake)
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertNotIn("CODEX_API_KEY", kwargs["env"])
                return fake

            long_prompt = "x" * 2201
            with patch.dict("os.environ", {"OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret"}), patch(
                "dual_codex.app_server.subprocess.Popen", side_effect=create
            ):
                first = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt="short",
                    output_path=root / "first.json",
                    session_id="biel4-session",
                    request_id="request-1",
                    run_id="run-1",
                )
                second = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt=long_prompt,
                    output_path=root / "second.json",
                    session_id="biel4-session",
                    request_id="request-1",
                    run_id="run-1",
                )

            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(first.metadata["app_server_thread_id"], "thread-probe")
            self.assertEqual(second.metadata["app_server_thread_id"], "thread-probe")
            self.assertEqual(first.metadata["app_server_turn_id"], "turn-1")
            self.assertEqual(second.metadata["app_server_turn_id"], "turn-2")
            self.assertEqual(second.metadata["task_transport"], "app_server")
            self.assertEqual(len(fake_processes), 1)
            self.assertEqual(fake_processes[0].prompts, ["short", long_prompt])
            journal_path = Path(first.metadata["live_event_journal"])
            deadline = time.monotonic() + 1
            journal_events = read_journal(journal_path)
            while len(journal_events) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
                journal_events = read_journal(journal_path)
            self.assertGreaterEqual(len(journal_events), 4)
            self.assertEqual(journal_events[0].method, "turn/started")
            self.assertIn("turn/completed", [event.method for event in journal_events])
            self.assertTrue(all(event.request_id == "request-1" for event in journal_events))
            self.assertTrue(all(event.run_id == "run-1" for event in journal_events))
            self.assertTrue(all(event.thread_id == "thread-probe" for event in journal_events))
            self.assertTrue(all(event.turn_id in {"turn-1", "turn-2"} for event in journal_events))

    def test_server_requests_are_denied_without_escalation(self) -> None:
        # The real probe used approvalPolicy=never and emitted no requests. This
        # unit seam is covered by the implementation's explicit decline branch.
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        sent: list[dict] = []
        process._send = sent.append
        process._respond_to_server_request({"id": 7, "method": "item/commandExecution/requestApproval"})
        self.assertEqual(sent[0]["result"], {"decision": "decline"})

    def test_event_journal_failure_cannot_change_notification_handling(self) -> None:
        from collections import deque
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._events = deque()
        process._event_journal = Mock()
        process._event_journal.append_notification.side_effect = RuntimeError("journal unavailable")
        process._event_context = {}
        process._record_notification({"jsonrpc": "2.0", "method": "future/notice", "params": {}})
        self.assertEqual(process.events[0]["method"], "future/notice")

    def test_event_journal_contention_cannot_block_notification_handling(self) -> None:
        from collections import deque
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._events = deque()
        process._event_context = {}
        process._event_publications = queue.Queue(maxsize=1)
        process._event_journal = Mock()
        blocked = threading.Event()
        release = threading.Event()

        def append_notification(*args, **kwargs):
            blocked.set()
            release.wait(2)

        process._event_journal.append_notification.side_effect = append_notification
        publisher = threading.Thread(target=process._publish_events, daemon=True)
        publisher.start()
        first = queued = dropped = None

        try:
            first_done = threading.Event()
            first = threading.Thread(
                target=lambda: (process._record_notification({"method": "blocked/notice"}), first_done.set()),
                daemon=True,
            )
            first.start()
            self.assertTrue(blocked.wait(1))
            self.assertTrue(first_done.wait(0.5))

            queued_done = threading.Event()
            queued = threading.Thread(
                target=lambda: (process._record_notification({"method": "queued/notice"}), queued_done.set()),
                daemon=True,
            )
            queued.start()
            self.assertTrue(queued_done.wait(0.5))

            dropped_done = threading.Event()
            dropped = threading.Thread(
                target=lambda: (process._record_notification({"method": "dropped/notice"}), dropped_done.set()),
                daemon=True,
            )
            dropped.start()
            self.assertTrue(dropped_done.wait(0.5))
            self.assertEqual([event["method"] for event in process.events], [
                "blocked/notice", "queued/notice", "dropped/notice",
            ])
        finally:
            release.set()
            if first is not None:
                first.join(1)
            if queued is not None:
                queued.join(1)
            if dropped is not None:
                dropped.join(1)
            deadline = time.monotonic() + 1
            while True:
                try:
                    process._event_publications.put_nowait(None)
                    break
                except queue.Full:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
            publisher.join(1)

    def test_dashboard_rpc_clears_executor_event_context(self) -> None:
        root = Path(".").resolve()
        config = _config(root)
        agent = AgentConfig(
            codex_home=root / "profile",
            model="",
            reasoning_effort="high",
            sandbox="workspace-write",
            account_name="executor",
            backend="app_server",
        )
        process = Mock()
        process.request.return_value = {"id": 1, "result": {"ok": True}}
        with patch("dual_codex.app_server._get_process", return_value=process):
            self.assertEqual(
                app_server_call(
                    config=config,
                    agent=agent,
                    repository=root,
                    method="account/read",
                ),
                {"ok": True},
            )
        process.set_event_context.assert_called_once_with(None)

    def test_dashboard_request_restores_executor_context_after_suppression(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._lock = threading.RLock()
        process._event_journal = "executor-journal"
        process._event_context = {"run_id": "run-1"}
        process.request = Mock(return_value={"id": 1, "result": {}})
        self.assertEqual(
            process.request_without_event_journal("account/read", {}, timeout=1),
            {"id": 1, "result": {}},
        )
        self.assertEqual(process._event_journal, "executor-journal")
        self.assertEqual(process._event_context, {"run_id": "run-1"})

    def test_request_tolerates_an_intermediate_queue_timeout(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._lock = threading.RLock()
        process._next_id = 0
        process._send = Mock()
        process._next_message = Mock(
            side_effect=[
                AppServerError("Timed out waiting for App Server JSON-RPC data."),
                {"jsonrpc": "2.0", "id": 1, "result": {}},
            ]
        )
        response = process.request("thread/start", {}, timeout=1)
        self.assertEqual(response["id"], 1)
        self.assertEqual(process._next_message.call_count, 2)

    def test_report_normalisation_keeps_existing_delegation_shape(self) -> None:
        value = json.loads(
            _normalise_report(
                json.dumps(
                    {
                        "summary": "Executor completed.",
                        "request_id": "x",
                        "status": "completed",
                        "files_changed": ["src/tiny_math/core.py"],
                        "tests": {"command": "python -m unittest", "result": "passed", "exit_code": 0, "tests_run": 4},
                        "remaining_issues": [],
                        "commit_created": False,
                    }
                )
            )
        )
        self.assertEqual(set(value), {"summary", "files_changed", "commands_run", "tests", "remaining_issues"})
        self.assertEqual(value["tests"][0]["status"], "passed")

    def test_report_normalisation_defaults_only_missing_command_telemetry(self) -> None:
        payload = {
            "summary": "Read-only probe completed.",
            "files_changed": [],
            "tests": [],
            "remaining_issues": [],
        }
        app_server = json.loads(_normalise_report(json.dumps(payload)))
        native_tui = _report_from_message(json.dumps(payload))
        self.assertEqual(app_server["commands_run"], [])
        self.assertEqual(native_tui, app_server)

    def test_report_normalisation_does_not_repair_invalid_or_missing_semantics(self) -> None:
        invalid_commands = {
            "summary": "done",
            "files_changed": [],
            "commands_run": "none",
            "tests": [],
            "remaining_issues": [],
        }
        missing_summary = {
            "files_changed": [],
            "commands_run": [],
            "tests": [],
            "remaining_issues": [],
        }
        self.assertEqual(json.loads(_normalise_report(json.dumps(invalid_commands))), invalid_commands)
        self.assertEqual(json.loads(_normalise_report(json.dumps(missing_summary))), missing_summary)

    def test_app_server_stderr_is_sanitized_before_result_return(self) -> None:
        raw = 'auth=C:/Users/USER/.codex/auth.json token="secret-value"'
        sanitized = _sanitize_stderr(raw)
        self.assertNotIn("auth.json", sanitized)
        self.assertNotIn("secret-value", sanitized)
        self.assertIn("[REDACTED_AUTH_PATH]", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_failed_turn_discards_persistent_process_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
            )
            process = object.__new__(type("Process", (), {}))
            process.agent = agent
            process.config = config
            process.thread_id_for = Mock(side_effect=AppServerError("turn timed out"))
            process.close = Mock()
            key = _process_key(agent, config)
            _PROCESSES[key] = process
            with patch("dual_codex.app_server._get_process", return_value=process):
                result = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt="unsafe to replay",
                    output_path=root / "result.json",
                    session_id="session",
                )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(key, _PROCESSES)
            process.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
