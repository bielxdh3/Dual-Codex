from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dual_codex.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_loads_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"
runs_dir = "runs"
max_correction_cycles = 2
require_clean_git = true
codex_command = "codex"

[architect]
codex_home = "profiles/a"
model = ""
reasoning_effort = "high"
sandbox = "read-only"

[executor]
codex_home = "profiles/b"
model = ""
reasoning_effort = "medium"
sandbox = "workspace-write"
""".strip(),
                encoding="utf-8",
            )
            config = load_config(root / "config.toml")
            self.assertEqual(config.repository, (root / "repo").resolve())
            self.assertEqual(config.runs_dir, (root / "runs").resolve())
            self.assertEqual(config.max_correction_cycles, 2)
            self.assertEqual(config.architect.sandbox, "read-only")
            self.assertEqual(config.executor.sandbox, "workspace-write")

    def test_rejects_unknown_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"

[accounts.executor]
codex_home = "profile"
backend = "unsupported"

[roles]
executor = "executor"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "unsupported backend"):
                load_config(root / "config.toml")


if __name__ == "__main__":
    unittest.main()
