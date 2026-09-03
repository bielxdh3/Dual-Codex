from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.live_events import _open_journal_lock


class WindowsJournalLockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific lock recovery")
    def test_transient_lock_open_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "shared.jsonl.lock"
            real_open = Path.open
            failures = 0

            def flaky_open(path: Path, *args, **kwargs):
                nonlocal failures
                if path == lock_path and failures == 0:
                    failures += 1
                    raise PermissionError(13, "simulated transient Windows sharing violation")
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=flaky_open):
                handle = _open_journal_lock(lock_path)
                handle.close()

            self.assertEqual(failures, 1)
            self.assertEqual(lock_path.read_bytes(), b"\0")


if __name__ == "__main__":
    unittest.main()
