from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.config import load_config
from dual_codex.live_events import LiveEventJournal, repository_identity
from dual_codex.live_reader import LiveExecutorReader
from dual_codex.paths import path_identity_key


def _config(root: Path, repository: Path):
    config_path = root / "config.toml"
    config_path.write_text(
        f'''[orchestrator]
repository = {json.dumps(str(repository))}
runs_dir = "runs"
codex_command = "codex"

[accounts.executor]
label = "Executor"
codex_home = "profiles/executor"
model = ""
reasoning_effort = "medium"

[roles]
orchestrator = "executor"
architect = "executor"
reviewer = "executor"
executor = "executor"
''',
        encoding="utf-8",
    )
    return load_config(config_path)


class LiveReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.config = _config(self.root, self.repository)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def journal(self, *, run_id: str = "run-1", request_id: str = "request-1") -> LiveEventJournal:
        return LiveEventJournal(
            self.config.runs_dir,
            account="executor",
            role="executor",
            repository=self.repository,
            run_id=run_id,
            request_id=request_id,
            max_records=32,
            max_record_bytes=4096,
            max_detail_bytes=1024,
        )

    def write_lock(
        self,
        request_id: str = "request-1",
        pid: int = 1234,
        run_id: str = "run-1",
        process_start: str = "start-1",
    ) -> None:
        digest = hashlib.sha256(path_identity_key(self.repository).encode()).hexdigest()[:24]
        path = self.config.runs_dir / ".locks" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "repository_key": repository_identity(self.repository),
                    "pid": pid,
                    "process_start": process_start,
                }
            ),
            encoding="utf-8",
        )

    def test_active_writer_is_working_only_with_fresh_evidence(self) -> None:
        journal = self.journal()
        journal.append(kind="run", state="started", method="run/started", detail={"started_at": datetime.now(timezone.utc).isoformat()})
        journal.append(kind="turn", state="started", method="turn/started", thread_id="thread-1", turn_id="turn-1")
        self.write_lock()
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            snapshot = LiveExecutorReader(self.config, self.repository).snapshot()
        self.assertEqual(snapshot["state"], "WORKING")
        self.assertEqual(snapshot["run_id"], "run-1")
        self.assertIsNone(snapshot["ended_at"])

    def test_terminal_event_beats_stale_marker_and_elapsed_is_frozen(self) -> None:
        journal = self.journal()
        started = (datetime.now(timezone.utc) - timedelta(seconds=4)).isoformat()
        ended = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        journal.append(kind="run", state="started", method="run/started", detail={"started_at": started})
        journal.append(
            kind="run",
            state="completed",
            method="run/completed",
            detail={"ended_at": ended},
            thread_id="thread-1",
            turn_id="turn-1",
        )
        self.write_lock()
        reader = LiveExecutorReader(self.config, self.repository)
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            first = reader.snapshot()
            second = reader.snapshot()
        self.assertEqual(first["state"], "COMPLETE")
        self.assertEqual(first["ended_at"], ended)
        self.assertEqual(first["elapsed_seconds"], second["elapsed_seconds"])
        self.assertEqual(first["request_id"], "request-1")
        self.assertEqual(first["thread_id"], "thread-1")
        self.assertEqual(first["turn_id"], "turn-1")

    def test_late_turn_activity_cannot_revive_completed_run(self) -> None:
        journal = self.journal()
        started = (datetime.now(timezone.utc) - timedelta(seconds=4)).isoformat()
        ended = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        journal.append(kind="run", state="started", method="run/started", detail={"started_at": started})
        journal.append(kind="run", state="completed", method="run/completed", detail={"ended_at": ended})
        journal.append(kind="turn", state="started", method="turn/started", turn_id="late-turn")
        self.write_lock()
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            reader = LiveExecutorReader(self.config, self.repository)
            snapshot = reader.snapshot()
            restarted = reader.snapshot()
        self.assertEqual(snapshot["state"], "COMPLETE")
        self.assertEqual(snapshot["ended_at"], ended)
        self.assertEqual(snapshot["elapsed_seconds"], restarted["elapsed_seconds"])
        self.assertEqual(snapshot["turn_id"], "late-turn")

    def test_explicit_failure_is_failed_and_stale_writer_has_reason(self) -> None:
        journal = self.journal(request_id="request-failed")
        journal.append(kind="run", state="started", method="run/started", detail={})
        journal.append(kind="run", state="failed", method="run/failed", detail={"reason": "executor error"})
        self.write_lock("request-failed")
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            reader = LiveExecutorReader(self.config, self.repository)
            snapshot = reader.snapshot()
            restarted = reader.snapshot()
        self.assertEqual(snapshot["state"], "FAILED")
        self.assertIsNone(snapshot["stale_reason"])
        self.assertEqual(snapshot["elapsed_seconds"], restarted["elapsed_seconds"])

        stale = self.journal(run_id="run-stale", request_id="request-stale")
        stale.append(kind="run", state="started", method="run/started", detail={})
        self.write_lock("request-stale", pid=9999, run_id="run-stale")
        with patch("dual_codex.live_reader._pid_alive", return_value=False), patch(
            "dual_codex.live_reader._process_start_token", return_value=None
        ):
            stale_reader = LiveExecutorReader(self.config, self.repository)
            stale_snapshot = stale_reader.snapshot()
            stale_restart = stale_reader.snapshot()
        self.assertEqual(stale_snapshot["state"], "STALE")
        self.assertTrue(stale_snapshot["stale_reason"])
        self.assertIsNotNone(stale_snapshot["ended_at"])
        self.assertEqual(stale_snapshot["elapsed_seconds"], stale_restart["elapsed_seconds"])

    def test_cancelled_terminal_event_is_authoritative_and_frozen(self) -> None:
        journal = self.journal(run_id="run-cancelled", request_id="request-cancelled")
        started = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
        ended = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        journal.append(kind="run", state="started", method="run/started", detail={"started_at": started})
        journal.append(
            kind="run",
            state="cancelled",
            method="run/cancelled",
            detail={"ended_at": ended, "reason": "cancelled"},
        )
        self.write_lock("request-cancelled", run_id="run-cancelled")
        reader = LiveExecutorReader(self.config, self.repository)
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            first = reader.snapshot()
            second = reader.snapshot()
        self.assertEqual(first["state"], "FAILED")
        self.assertEqual(first["ended_at"], ended)
        self.assertEqual(first["elapsed_seconds"], second["elapsed_seconds"])

    def test_partial_record_does_not_revive_completed_run_after_restart(self) -> None:
        journal = self.journal()
        journal.append(kind="run", state="started", method="run/started", detail={})
        journal.append(kind="run", state="completed", method="run/completed", detail={})
        with journal.path.open("ab") as handle:
            handle.write(b'{"schema_version":1,"sequence":999')
        self.write_lock()
        with patch("dual_codex.live_reader._pid_alive", return_value=False), patch(
            "dual_codex.live_reader._process_start_token", return_value=None
        ):
            snapshot = LiveExecutorReader(self.config, self.repository).snapshot()
        self.assertEqual(snapshot["state"], "COMPLETE")
        self.assertEqual(snapshot["cursor"], 2)

    def test_newer_run_supersedes_old_terminal_and_marker(self) -> None:
        old = self.journal(run_id="old-run", request_id="old-request")
        old.append(kind="run", state="started", method="run/started", detail={})
        old.append(kind="run", state="completed", method="run/completed", detail={})
        newer = self.journal(run_id="new-run", request_id="new-request")
        newer.append(kind="run", state="started", method="run/started", detail={})
        newer.append(kind="turn", state="started", method="turn/started", turn_id="new-turn")
        self.write_lock("new-request", run_id="new-run")
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            snapshot = LiveExecutorReader(self.config, self.repository).snapshot()
        self.assertEqual(snapshot["state"], "WORKING")
        self.assertEqual(snapshot["run_id"], "new-run")
        self.assertEqual(snapshot["request_id"], "new-request")
        self.assertEqual(snapshot["turn_id"], "new-turn")
        self.assertIn("ended_at", snapshot)
        self.assertIn("last_event_at", snapshot)

    def test_marker_run_identity_and_process_start_mismatch_never_revive_working(self) -> None:
        journal = self.journal(run_id="run-identity", request_id="request-identity")
        journal.append(kind="run", state="started", method="run/started", detail={})
        self.write_lock("request-identity", run_id="different-run", process_start="start-1")
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="start-1"
        ):
            different_run = LiveExecutorReader(self.config, self.repository).snapshot()
        self.assertNotEqual(different_run["state"], "WORKING")
        self.assertTrue(different_run["stale_reason"])

        self.write_lock("request-identity", run_id="run-identity", process_start="old-start")
        with patch("dual_codex.live_reader._pid_alive", return_value=True), patch(
            "dual_codex.live_reader._process_start_token", return_value="new-start"
        ):
            reused_pid = LiveExecutorReader(self.config, self.repository).snapshot()
        self.assertNotEqual(reused_pid["state"], "WORKING")
        self.assertIn("identity", reused_pid["stale_reason"])


if __name__ == "__main__":
    unittest.main()
