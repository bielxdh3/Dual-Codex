from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dual_codex.config import AgentConfig, OrchestratorConfig
from dual_codex.terminal import (
    TerminalError,
    TerminalManager,
    TERMINAL_INLINE_MESSAGE_MAX,
    TERMINAL_SUBMIT_DELAY_SECONDS,
    TuiComposerAckDetector,
    TuiReadinessDetector,
    TuiTurnStartDetector,
    find_session_file,
    interactive_command_args,
    session_id_for,
    session_turn_started,
    session_turn_state,
    _rollout_snapshot,
    validate_control_message,
    validate_session_id,
)


def _config(root: Path, repository: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        repository=repository,
        runs_dir=root / "runs",
        max_correction_cycles=1,
        require_clean_git=True,
        codex_command="codex",
        accounts={},
        roles={},
        project_root=Path(__file__).parents[1],
        config_path=root / "config.toml",
    )


def _normal_screen(prompt: str = "Improve documentation in @filename") -> str:
    return (
        "\u2502 >_ OpenAI Codex (v0.146.1)                  \u2502\n"
        "\u2502                                             \u2502\n"
        "\u2502 model:     gpt-5.6-sol   /model to change   \u2502\n"
        "\u2502 directory: ~\\3D Objects\\Dual-Codex-E2E-Test \u2502\n"
        "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\u203a {prompt}\n"
    )


class TerminalTests(unittest.TestCase):
    def test_interactive_command_is_not_exec_and_preserves_spaces(self) -> None:
        command = interactive_command_args(
            Path("C:/Work Tree/target"),
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        self.assertNotIn("exec", command)
        self.assertIn("--no-alt-screen", command)
        self.assertEqual(command[command.index("--cd") + 1], "C:\\Work Tree\\target")

    def test_session_id_rejects_path_traversal(self) -> None:
        with self.assertRaises(TerminalError):
            validate_session_id("../escape")
        self.assertRegex(session_id_for("biel4", Path("C:/Work Tree")), r"^biel4-[a-f0-9]{12}$")

    def test_session_id_is_stable_and_scoped_to_account_and_repository(self) -> None:
        same_path_a = Path(r"C:\Work Tree\target")
        same_path_b = Path("c:/Work Tree/target")
        self.assertEqual(session_id_for("biel4", same_path_a), session_id_for("biel4", same_path_b))
        self.assertNotEqual(session_id_for("biel4", same_path_a), session_id_for("biel3", same_path_a))
        self.assertNotEqual(session_id_for("biel4", same_path_a), session_id_for("biel4", Path(r"C:\Work Tree\other")))

    def test_cosmetic_suggestion_alone_is_not_readiness(self) -> None:
        detector = TuiReadinessDetector()
        self.assertEqual(detector.feed("\u203a Improve documentation in @filename\n"), detector.NOT_READY)
        self.assertEqual(detector.feed("\u203a Explain this codebase\n"), detector.NOT_READY)
        self.assertTrue(detector.seen_placeholder)
        self.assertFalse(detector.seen_ready)

    def test_readiness_requires_stable_normal_idle_markers(self) -> None:
        detector = TuiReadinessDetector()
        startup = "Update now / Skip\n\u2502 model:     loading\n"
        self.assertEqual(detector.feed(startup), detector.NOT_READY)
        normal = _normal_screen()
        self.assertEqual(detector.feed(normal), detector.NOT_READY)
        self.assertEqual(detector.feed(normal), detector.READY)
        self.assertTrue(detector.seen_placeholder)
        self.assertEqual(detector.ready_evidence, "stable Codex banner/model/directory/composer markers")

    def test_readiness_handles_partial_ansi_and_trust_setup(self) -> None:
        setup = TuiReadinessDetector()
        self.assertEqual(
            setup.feed("Do you trust the contents of this directory?"),
            setup.SETUP_REQUIRED,
        )
        self.assertTrue(setup.seen_trust)

        detector = TuiReadinessDetector()
        self.assertEqual(detector.feed("\u2502 >_ OpenAI Codex\n\u2502 model:     gp"), detector.NOT_READY)
        self.assertEqual(
            detector.feed(
                "t-5.6-sol\x1b[0m\r\n\u2502 directory: ~\\3D Objects\\Dual-Codex-E2E-Test\n"
                "\x1b[2K\u203a Use /skills to list available skills\r\n"
            ),
            detector.NOT_READY,
        )
        self.assertEqual(detector.feed(_normal_screen()), detector.NOT_READY)
        self.assertEqual(detector.feed(_normal_screen()), detector.READY)

    def test_startup_loading_screen_cannot_be_ready(self) -> None:
        detector = TuiReadinessDetector()
        self.assertEqual(
            detector.feed(
                "\u2502 >_ OpenAI Codex\n\u2502 model:     loading\n"
                "\u2502 directory: C:\\target\n\u203a Implement {feature}\n"
            ),
            detector.NOT_READY,
        )

    def test_turn_start_detector_ignores_composer_text(self) -> None:
        detector = TuiTurnStartDetector(_normal_screen())
        self.assertFalse(detector.feed("\u203a The requested task is now in the composer\n"))
        self.assertTrue(detector.feed("\u2022 Working (0s \u2022 esc to interrupt)\n"))
        self.assertEqual(detector.evidence, "terminal Working marker")

    def test_composer_ack_rejects_historical_marker_and_reconstructs_wrapped_chunks(self) -> None:
        historical = TuiComposerAckDetector("[DC:old1234]", "old output [DC:old1234]")
        self.assertFalse(historical.feed("old output [DC:old1234]"))

        detector = TuiComposerAckDetector("[DC:new1234]", "idle composer")
        self.assertFalse(detector.feed("\u203a control text [DC:new"))
        self.assertTrue(detector.feed("1234]\n"))
        self.assertEqual(detector.evidence, "unique composer marker observed after text write")

    def test_session_jsonl_discovery_and_completion_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            sessions = root / "sessions" / "2026" / "08" / "07"
            sessions.mkdir(parents=True)
            path = sessions / "rollout-abc.jsonl"
            entries = [
                {"type": "session_meta", "payload": {"session_id": "codex-1", "cwd": str(repository)}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "done"}]}},
                {"type": "event_msg", "payload": {"type": "turn_completed"}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in entries) + "\n", encoding="utf-8")
            found = find_session_file(root, repository)
            self.assertEqual(found, (path, "codex-1"))
            self.assertEqual(session_turn_started(path), "task_started")
            self.assertEqual(session_turn_state(path), ("completed", "done"))

    def test_historical_active_rollout_is_ignored_without_mutating_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            path = root / "sessions" / "2026" / "08" / "07" / "rollout-probe.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {"session_id": "probe-session", "cwd": str(repository)}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
                + "\n",
                encoding="utf-8",
            )
            before = path.read_bytes()
            mtime = path.stat().st_mtime
            session = SimpleNamespace(
                codex_home=root,
                repository=repository,
                baseline_rollout_mtimes={"probe": mtime},
                process_started_at=mtime + 10,
                codex_session_id="",
            )

            activity = TerminalManager._codex_turn_activity(session)

            self.assertFalse(activity["active"])
            self.assertEqual(activity["source"], "historical_stale")
            self.assertEqual(activity["ignored_stale_rollout"][0]["rollout_id"], "probe")
            self.assertEqual(path.read_bytes(), before)

    def test_current_epoch_and_current_session_rollouts_still_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            path = root / "sessions" / "2026" / "08" / "07" / "rollout-current.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {"session_id": "current-session", "cwd": str(repository)}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
                + "\n",
                encoding="utf-8",
            )
            mtime = path.stat().st_mtime
            current_epoch = SimpleNamespace(
                codex_home=root,
                repository=repository,
                baseline_rollout_mtimes={},
                process_started_at=mtime - 1,
                codex_session_id="",
            )
            current_session = SimpleNamespace(
                codex_home=root,
                repository=repository,
                baseline_rollout_mtimes={"current": mtime},
                process_started_at=mtime + 10,
                codex_session_id="current-session",
            )

            self.assertEqual(TerminalManager._codex_turn_activity(current_epoch)["source"], "current_epoch")
            self.assertEqual(TerminalManager._codex_turn_activity(current_session)["source"], "current_session")

    def test_completed_rollout_does_not_block_and_restart_refreshes_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            path = root / "sessions" / "2026" / "08" / "07" / "rollout-complete.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {"session_id": "done-session", "cwd": str(repository)}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "turn_completed"}})
                + "\n",
                encoding="utf-8",
            )
            snapshot = _rollout_snapshot(root, repository)
            self.assertIn("complete", snapshot)
            session = SimpleNamespace(
                codex_home=root,
                repository=repository,
                baseline_rollout_mtimes=snapshot,
                process_started_at=path.stat().st_mtime + 10,
                codex_session_id="",
            )
            self.assertFalse(TerminalManager._codex_turn_activity(session)["active"])

    def test_stale_rollout_does_not_delay_true_idle_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            rollout = root / "sessions" / "2026" / "08" / "07" / "rollout-stale.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"session_id": "stale-session", "cwd": str(repository)}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
                + "\n",
                encoding="utf-8",
            )
            rollout_mtime = rollout.stat().st_mtime
            log_file = root / "terminal.log"
            session = SimpleNamespace(
                session_id="biel4-test",
                pid=123,
                started_at="2026-08-07T17:00:00+00:00",
                log_file=log_file,
                codex_home=root,
                repository=repository,
                baseline_rollout_mtimes={"stale": rollout_mtime},
                process_started_at=rollout_mtime + 10,
                codex_session_id="",
            )
            manager = TerminalManager.__new__(TerminalManager)
            manager.config = _config(root, repository)
            manager._load = lambda _session_id: session
            manager.read = lambda _session_id, _lines: _normal_screen()

            with patch("dual_codex.terminal.time.sleep"):
                result = manager.wait_until_ready("biel4-test")

            self.assertEqual(result["rollout_activity"]["source"], "historical_stale")

    def test_send_waits_for_readiness_and_confirms_turn_start_once(self) -> None:
        manager = TerminalManager.__new__(TerminalManager)
        events: list[str] = []
        session = SimpleNamespace(pipe="\\\\.\\pipe\\test")

        def request(_pipe: str, payload: dict[str, object]) -> dict[str, object]:
            events.append(str(payload["op"]))
            return {"ok": True}

        with patch.object(manager, "_load", return_value=session), patch.object(
            manager, "wait_until_ready", side_effect=lambda _session_id: events.append("ready")
        ), patch.object(manager, "read", return_value=_normal_screen()), patch.object(
            manager, "turn_cursor", return_value=(None, 0)
        ), patch.object(
            manager,
            "wait_for_composer_ack",
            side_effect=lambda *args, **kwargs: events.append("composer_ack")
            or {"state": "composer_acknowledged", "marker": "[DC:test]"},
        ), patch.object(
            manager,
            "wait_until_turn_started",
            side_effect=lambda *args, **kwargs: events.append("turn_started") or {"source": "test"},
        ), patch("dual_codex.terminal._pipe_request", side_effect=request), patch(
            "dual_codex.terminal.time.sleep", side_effect=lambda delay: events.append(f"sleep:{delay}")
        ):
            evidence = manager.send("biel4-test", "hello")

        self.assertEqual(evidence["source"], "test")
        self.assertEqual(evidence["terminal_input_sequence"], "text_then_carriage_return")
        self.assertEqual(evidence["terminal_input_timing"]["configured_delay_ms"], 100)
        self.assertEqual(events[:3], ["ready", "send_text", "composer_ack"])
        self.assertAlmostEqual(float(events[3].split(":", 1)[1]), TERMINAL_SUBMIT_DELAY_SECONDS, places=2)
        self.assertEqual(events[4:], ["submit", "turn_started"])

    def test_submit_delay_is_explicitly_between_text_and_enter(self) -> None:
        manager = TerminalManager.__new__(TerminalManager)
        session = SimpleNamespace(pipe="\\\\.\\pipe\\test")
        operations: list[str] = []

        def request(_pipe: str, payload: dict[str, object]) -> dict[str, object]:
            operations.append(str(payload["op"]))
            return {"ok": True}

        clock = iter((10.0, 10.002, 10.004, 10.104, 10.106, 10.200))
        with patch.object(manager, "_load", return_value=session), patch.object(
            manager, "wait_until_ready"
        ), patch.object(manager, "read", return_value=_normal_screen()), patch.object(
            manager, "turn_cursor", return_value=(None, 0)
        ), patch.object(
            manager, "wait_for_composer_ack", return_value={"state": "composer_acknowledged", "marker": "[DC:test]"}
        ), patch.object(
            manager, "wait_until_turn_started", return_value={"source": "test"}
        ), patch("dual_codex.terminal._pipe_request", side_effect=request), patch(
            "dual_codex.terminal.time.monotonic", side_effect=lambda: next(clock)
        ), patch("dual_codex.terminal.time.sleep") as sleep:
            evidence = manager.send("biel4-test", "hello")

        timing = evidence["terminal_input_timing"]
        self.assertEqual(operations, ["send_text", "submit"])
        self.assertEqual(timing["text_to_ack_delay_ms"], 2.0)
        self.assertEqual(timing["ack_to_enter_delay_ms"], 100.0)
        self.assertEqual(timing["text_to_enter_delay_ms"], 102.0)
        self.assertEqual(timing["enter_to_turn_start_ms"], 94.0)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], TERMINAL_SUBMIT_DELAY_SECONDS - 0.002, places=2)

    def test_composer_ack_timeout_never_sends_enter(self) -> None:
        manager = TerminalManager.__new__(TerminalManager)
        session = SimpleNamespace(pipe="\\\\.\\pipe\\test")
        captured: list[dict[str, object]] = []

        def request(_pipe: str, payload: dict[str, object]) -> dict[str, object]:
            captured.append(payload)
            return {"ok": True}

        with patch.object(manager, "_load", return_value=session), patch.object(
            manager, "wait_until_ready"
        ), patch.object(manager, "read", return_value=_normal_screen()), patch.object(
            manager, "turn_cursor", return_value=(None, 0)
        ), patch.object(
            manager, "wait_for_composer_ack", side_effect=TerminalError("ack timeout")
        ), patch("dual_codex.terminal._pipe_request", side_effect=request), self.assertRaisesRegex(
            TerminalError, "ack timeout"
        ):
            manager.send("biel4-test", "hello")

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["op"], "send_text")

    def test_oversized_followup_uses_file_transport_and_short_control_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp) / "task artifacts"
            artifact_dir.mkdir()
            manager = TerminalManager.__new__(TerminalManager)
            session = SimpleNamespace(pipe="\\\\.\\pipe\\test", add_dirs=(artifact_dir,))
            captured: list[dict[str, object]] = []

            def request(_pipe: str, payload: dict[str, object]) -> dict[str, object]:
                captured.append(payload)
                return {"ok": True}

            long_message = "follow-up instruction\n" + ("x" * (TERMINAL_INLINE_MESSAGE_MAX + 100))
            with patch.object(manager, "_load", return_value=session), patch.object(
                manager, "wait_until_ready"
            ), patch.object(manager, "read", return_value=_normal_screen()), patch.object(
                manager, "turn_cursor", return_value=(None, 0)
            ), patch.object(
                manager, "wait_for_composer_ack", return_value={"state": "composer_acknowledged", "marker": "[DC:test]"}
            ), patch.object(
                manager, "wait_until_turn_started", return_value={"source": "test"}
            ), patch("dual_codex.terminal._pipe_request", side_effect=request):
                evidence = manager.send("biel4-test", long_message)

            self.assertEqual(len(captured), 2)
            control = str(captured[0]["message"])
            self.assertLessEqual(len(control), TERMINAL_INLINE_MESSAGE_MAX)
            self.assertNotRegex(control, r"[\r\n]")
            self.assertNotIn(long_message, control)
            self.assertEqual(captured[1], {"op": "submit"})
            artifact = next(artifact_dir.glob("followup-*.md"))
            self.assertIn(long_message, artifact.read_text(encoding="utf-8"))
            self.assertEqual(evidence["task_transport"], "file")
            self.assertEqual(evidence["task_artifact"], str(artifact))

    def test_control_message_validation_rejects_multiline_empty_oversized_and_task_body(self) -> None:
        with self.assertRaisesRegex(TerminalError, "single physical line"):
            validate_control_message("one\ntwo")
        with self.assertRaisesRegex(TerminalError, "non-empty"):
            validate_control_message("  ")
        with self.assertRaisesRegex(TerminalError, "safe"):
            validate_control_message("x" * (TERMINAL_INLINE_MESSAGE_MAX + 1))
        with self.assertRaisesRegex(TerminalError, "task body"):
            validate_control_message("read task body", forbidden_text="task body")

    def test_turn_start_timeout_does_not_resend_and_writes_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            log_file = root / "session.pty.log"
            config = _config(root, repository)
            session = SimpleNamespace(log_file=log_file, codex_home=root, repository=repository)
            manager = TerminalManager.__new__(TerminalManager)
            manager.config = config
            manager._load = lambda _session_id: session
            manager.read = lambda _session_id, _lines: "\u203a task in composer\n"
            manager.terminate = lambda _session_id: None
            clock = iter((0.0, 0.1, 0.2))

            with patch("dual_codex.terminal.find_session_file", return_value=None), patch(
                "dual_codex.terminal.time.monotonic", side_effect=lambda: next(clock)
            ), patch("dual_codex.terminal.time.sleep"), self.assertRaisesRegex(TerminalError, "sent once"):
                manager.wait_until_turn_started(
                    "biel4-test",
                    baseline_output=_normal_screen(),
                    submitted_at=0.0,
                    timeout=0.01,
                )

            self.assertIn("[turn-start-diagnostics]", log_file.read_text(encoding="utf-8"))
            self.assertIn("resend_attempted", log_file.read_text(encoding="utf-8"))

    def test_readiness_timeout_writes_diagnostics_and_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            log_file = root / "session.pty.log"
            config = _config(root, repository)
            session = SimpleNamespace(log_file=log_file)
            manager = TerminalManager.__new__(TerminalManager)
            manager.config = config
            manager._load = lambda _session_id: session
            manager.read = lambda _session_id, _lines: ""
            terminated: list[str] = []
            manager.terminate = lambda session_id: terminated.append(session_id)
            clock = iter((0.0, 0.1, 0.2))

            with patch("dual_codex.terminal.time.monotonic", side_effect=lambda: next(clock)), patch(
                "dual_codex.terminal.time.sleep"
            ), self.assertRaisesRegex(TerminalError, "Timed out"):
                manager.wait_until_ready("biel4-test", timeout=0.01)

            self.assertEqual(terminated, ["biel4-test"])
            self.assertIn("[readiness-diagnostics]", log_file.read_text(encoding="utf-8"))

    def test_start_sets_account_home_and_rejects_duplicate_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            artifact_dir = root / "task artifacts"
            artifact_dir.mkdir()
            config = _config(root, repository)
            agent = AgentConfig(
                codex_home=root / "executor profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                label="Executor",
            )

            class FakeProcess:
                pid = 1234

                def poll(self):
                    return None

                def terminate(self):
                    return None

            def ready(_pipe, _payload):
                return {"ok": True, "state": {"alive": True}}

            with patch.dict(os.environ, {"OPENAI_API_KEY": "inherited-secret"}), patch(
                "dual_codex.terminal.os.name", "nt"
            ), patch(
                "dual_codex.terminal.shutil.which", return_value="node"
            ), patch("dual_codex.terminal.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
                "dual_codex.terminal._pipe_request", side_effect=ready
            ), patch("dual_codex.terminal.TerminalManager.wait_until_ready"):
                manager = TerminalManager(config)
                session = manager.start(
                    session_id="biel4-test",
                    agent=agent,
                    role="executor",
                    repository=repository,
                    add_dirs=(artifact_dir,),
                )
                self.assertEqual(session.account, "biel4")
                self.assertEqual(session.add_dirs, (artifact_dir.resolve(),))
                command = [str(item) for item in popen.call_args.args[0]]
                self.assertEqual(command[command.index("--add-dir") + 1], str(artifact_dir.resolve()))
                self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], str(agent.codex_home))
                self.assertNotIn("OPENAI_API_KEY", popen.call_args.kwargs["env"])
                self.assertNotIn("exec", [str(item) for item in popen.call_args.args[0]])
                with self.assertRaises(TerminalError):
                    manager.start(
                        session_id="biel4-test",
                        agent=agent,
                        role="executor",
                        repository=repository,
                    )


if __name__ == "__main__":
    unittest.main()
