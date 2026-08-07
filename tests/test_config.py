from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dual_codex.config import load_config


CONFIG = """
[orchestrator]
repository = "repo"
runs_dir = "runs"
max_correction_cycles = 2
require_clean_git = false
codex_command = "codex"

[architect]
codex_home = "profiles/architect"
model = ""
reasoning_effort = "high"
sandbox = "read-only"

[executor]
codex_home = "profiles/executor"
model = ""
reasoning_effort = "medium"
sandbox = "workspace-write"
""".lstrip()


class ConfigTests(unittest.TestCase):
    def test_relative_paths_resolve_from_config_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(CONFIG, encoding="utf-8")

            config = load_config(config_path)

            self.assertEqual(config.repository, (root / "repo").resolve())
            self.assertEqual(config.runs_dir, (root / "runs").resolve())
            self.assertEqual(config.architect.codex_home, (root / "profiles/architect").resolve())
            self.assertEqual(config.executor.codex_home, (root / "profiles/executor").resolve())
            self.assertEqual(config.max_correction_cycles, 2)
            self.assertFalse(config.require_clean_git)

    def test_utf8_bom_is_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(CONFIG, encoding="utf-8-sig")

            config = load_config(config_path)

            self.assertEqual(config.repository, (root / "repo").resolve())


if __name__ == "__main__":
    unittest.main()
