from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import html
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest

from dual_codex.live_events import (
    LiveEventJournal,
    _normalise_path,
    journal_path,
    normalize_notification,
    read_journal,
    repository_identity,
)
from dual_codex.paths import same_path


def _cross_process_writer(runtime_root: str, path: str, index: int) -> None:
    journal = LiveEventJournal(
        runtime_root,
        account="executor",
        role="executor",
        path=path,
        max_records=20,
        max_record_bytes=1024,
    )
    journal.append(kind="notification", state="observed", method=f"process/{index}")


class LiveEventTests(unittest.TestCase):
    def _journal(self, root: Path, **kwargs) -> LiveEventJournal:
        repository = root / "repo"
        repository.mkdir(exist_ok=True)
        return LiveEventJournal(
            root / "runs",
            account="executor",
            role="executor",
            repository=repository,
            run_id="run-1",
            request_id="request-1",
            **kwargs,
        )

    def test_known_and_unknown_notifications_are_normalized(self) -> None:
        turn = normalize_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertEqual((turn["kind"], turn["state"]), ("turn", "started"))
        self.assertEqual((turn["thread_id"], turn["turn_id"]), ("thread-1", "turn-1"))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = self._journal(root, max_records=20)
            methods = (
                ("turn/started", {"threadId": "thread-1", "turn": {"id": "turn-1"}}),
                ("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}),
                ("item/commandExecution/started", {"threadId": "thread-1", "turnId": "turn-1"}),
                ("item/fileChange/updated", {"threadId": "thread-1", "turnId": "turn-1"}),
                ("item/agentMessage/delta", {"threadId": "thread-1", "turnId": "turn-1", "delta": "hello"}),
                ("thread/tokenUsage/updated", {"threadId": "thread-1", "usage": {"total": 12}}),
                ("error", {"threadId": "thread-1", "message": "failed"}),
                ("future/protocol/notice", {"threadId": "thread-1", "value": "kept"}),
            )
            for method, params in methods:
                journal.append_notification(method, params)

            events = journal.read()
            self.assertEqual([event.sequence for event in events], list(range(1, 9)))
            self.assertEqual([event.kind for event in events], [
                "turn", "turn", "command_execution", "file_change",
                "agent_message", "token_usage", "error", "notification",
            ])
            self.assertEqual(events[-1].method, "future/protocol/notice")
            self.assertEqual(events[0].request_id, "request-1")
            self.assertEqual(events[0].account, "executor")
            self.assertEqual(events[0].role, "executor")
            self.assertEqual(events[0].repository_key, repository_identity(root / "repo"))
            datetime.fromisoformat(events[0].timestamp.replace("Z", "+00:00"))

    def test_command_output_is_chunked_and_file_token_events_keep_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = self._journal(root, max_records=40, max_detail_bytes=96, max_record_bytes=1024)
            output = "output-&<" + ("x" * 5000)
            chunks = journal.append_notification(
                "item/commandExecution/outputDelta",
                {"threadId": "thread-1", "turnId": "turn-1", "output": output},
            )
            self.assertGreater(len(chunks), 1)
            self.assertEqual([event.sequence for event in chunks], list(range(1, len(chunks) + 1)))
            marker = "...[truncated]"
            escaped = html.escape(output, quote=True)
            expected = escaped[: 4096 - len(marker)] + marker
            self.assertEqual("".join(event.detail["text"] for event in chunks), expected)
            self.assertTrue(
                all(
                    len(json.dumps(event.detail, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 96
                    for event in chunks
                )
            )
            self.assertEqual(
                journal.append_notification("item/fileChange/updated", {"diff": "diff --git a/a b/a"})[0].kind,
                "file_change",
            )
            self.assertEqual(
                journal.append_notification(
                    "thread/tokenUsage/updated",
                    {"usage": {"inputTokens": 10, "outputTokens": 32, "totalTokens": 42}},
                )[0].kind,
                "token_usage",
            )
            self.assertEqual(journal.read()[-1].detail["usage"]["totalTokens"], 42)

    def test_sanitization_removes_secrets_hidden_reasoning_and_executable_markup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = self._journal(root, max_record_bytes=1024, max_detail_bytes=256)
            journal.append_notification(
                "future/notice",
                {
                    "path": "C:/Users/USER/CodexProfiles/executor/.env",
                    "secret": "do-not-store",
                    "analysis": "hidden chain of thought",
                    "message": "<script>alert('x')</script>",
                    "token": "secret-token",
                },
            )
            raw = journal.path.read_text(encoding="utf-8")
            self.assertNotIn("do-not-store", raw)
            self.assertNotIn("hidden chain of thought", raw)
            self.assertNotIn("secret-token", raw)
            self.assertNotIn("<script", raw)
            self.assertNotIn("CodexProfiles", raw)
            self.assertIn("[REDACTED]", raw)

    def test_detail_and_record_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = self._journal(root, max_records=4, max_record_bytes=700, max_detail_bytes=64)
            event = journal.append(
                kind="notification",
                state="observed",
                method="future/large",
                detail={"text": "x" * 10000},
            )
            self.assertTrue(event.detail.get("truncated"))
            lines = journal.path.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(max(len(line.encode("utf-8")) for line in lines), 700)
            self.assertLessEqual(len(json.dumps(event.detail, ensure_ascii=False).encode("utf-8")), 64)

    def test_partial_final_record_reopen_and_bounded_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = self._journal(root, max_records=2, max_record_bytes=1024)
            journal.append(kind="notification", state="one", method="one")
            journal.append(kind="notification", state="two", method="two")
            with journal.path.open("ab") as handle:
                handle.write(b'{"schema_version":1,"sequence":999')
            self.assertEqual([event.sequence for event in journal.read()], [1, 2])
            reopened = self._journal(root, max_records=2, max_record_bytes=1024)
            reopened.append(kind="notification", state="three", method="three")
            self.assertEqual([event.sequence for event in reopened.read()], [2, 3])
            self.assertEqual(len(reopened.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_concurrent_writers_have_unique_monotonic_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journal = self._journal(root, max_records=40, max_record_bytes=1024)

            def write(index: int) -> int:
                return journal.append(kind="notification", state="observed", method=f"test/{index}").sequence

            with ThreadPoolExecutor(max_workers=8) as executor:
                sequences = list(executor.map(write, range(20)))
            self.assertEqual(sorted(sequences), list(range(1, 21)))
            self.assertEqual([event.sequence for event in journal.read()], list(range(1, 21)))

    def test_cross_process_writers_share_the_journal_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runs"
            path = runtime_root / "shared.jsonl"
            context = multiprocessing.get_context("spawn")
            processes = [
                context.Process(
                    target=_cross_process_writer,
                    args=(
                        (
                            str(runtime_root)
                            if os.name != "nt" or index % 2 == 0
                            else "\\\\?\\" + str(runtime_root)
                        ),
                        (
                            str(path)
                            if os.name != "nt" or index in (0, 3)
                            else "\\\\?\\" + str(path)
                        ),
                        index,
                    ),
                )
                for index in range(4)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            events = read_journal(path, max_records=20, max_record_bytes=1024)
            self.assertEqual([event.sequence for event in events], list(range(1, 5)))
            lock_path = path.with_name(path.name + ".lock")
            if lock_path.exists():
                lock_path.unlink()
            self.assertFalse(list(runtime_root.glob("*.tmp-*")))

    def test_account_repository_identity_and_runtime_root_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            self.assertTrue(same_path(repository, repository.resolve()))
            self.assertEqual(
                journal_path(root / "runs", account="executor", role="executor", repository=repository),
                journal_path(root / "runs", account="executor", role="executor", repository=repository.resolve()),
            )
            if os.name == "nt":
                extended_root = "\\\\?\\" + str(root / "runs")
                normal_path = root / "runs" / "extended-equivalent.jsonl"
                extended_path = "\\\\?\\" + str(normal_path)
                self.assertEqual(
                    _normalise_path(r"\\server\share\runtime"),
                    _normalise_path(r"\\?\UNC\server\share\runtime"),
                )
                normal_journal = LiveEventJournal(
                    root / "runs",
                    account="executor",
                    role="executor",
                    path=normal_path,
                    max_record_bytes=1024,
                )
                extended_journal = LiveEventJournal(
                    extended_root,
                    account="executor",
                    role="executor",
                    path=extended_path,
                    max_record_bytes=1024,
                )
                self.assertEqual(extended_journal.path, normal_journal.path)
                self.assertEqual(
                    journal_path(root / "runs", account="executor", role="executor", repository=repository),
                    journal_path(
                        extended_root,
                        account="executor",
                        role="executor",
                        repository=repository,
                    ),
                )
            other = journal_path(root / "runs", account="architect", role="architect", repository=repository)
            self.assertNotEqual(other, journal_path(root / "runs", account="executor", role="executor", repository=repository))
            with self.assertRaises(ValueError):
                LiveEventJournal(
                    root / "runs",
                    account="executor",
                    role="executor",
                    path=root / "outside.jsonl",
                )
            with self.assertRaises(ValueError):
                LiveEventJournal(
                    root / "runs",
                    account="executor",
                    role="executor",
                    path="..",
                )

            journal = LiveEventJournal(
                root / "runs",
                account="executor",
                role="executor",
                path=root / "runs" / "explicit.jsonl",
                max_record_bytes=1024,
            )
            journal.append(kind="notification", state="observed", method="safe")
            self.assertTrue(journal.path.is_file())
            self.assertEqual(read_journal(journal.path)[0].method, "safe")


if __name__ == "__main__":
    unittest.main()
