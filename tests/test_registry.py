from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.cli import main
from dual_codex.config import ConfigError, load_config
from dual_codex.registry import (
    AccountConfig,
    add_account,
    ensure_codex_profile,
    migrate_legacy_config,
    remove_account,
    rename_account,
    swap_roles,
    unassign_role,
    write_registry_config,
)


def _write_registry(path: Path, *, command: str = "missing-codex") -> None:
    path.write_text(
        f'''[orchestrator]\nrepository = "{path.parent.as_posix()}"\nruns_dir = "runs"\nrequire_clean_git = false\ncodex_command = "{command}"\n\n[accounts."primary"]\nlabel = "Primary"\ncodex_home = "{(path.parent / "profiles" / "primary account").as_posix()}"\nmodel = ""\nreasoning_effort = "high"\n\n[accounts."secondary"]\nlabel = "Secondary"\ncodex_home = "{(path.parent / "profiles" / "secondary account").as_posix()}"\nmodel = ""\nreasoning_effort = "high"\n\n[accounts."spare"]\nlabel = "Unused"\ncodex_home = "{(path.parent / "profiles" / "spare account").as_posix()}"\nmodel = ""\nreasoning_effort = "medium"\n\n[roles]\n orchestrator = "primary"\narchitect = "primary"\nreviewer = "primary"\nexecutor = "secondary"\n''',
        encoding="utf-8",
    )


class RegistryTests(unittest.TestCase):
    def test_loads_three_accounts_and_reviewer_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            config = load_config(path)
            self.assertEqual(len(config.accounts), 3)
            self.assertEqual(config.accounts["spare"].label, "Unused")
            self.assertEqual(config.agent_for_role("reviewer").account_name, "primary")
            self.assertEqual(config.agent_for_role("executor").sandbox, "workspace-write")

            unassign_role(config, "reviewer")
            config = load_config(path)
            self.assertEqual(config.agent_for_role("reviewer").account_name, "primary")
            unassign_role(config, "executor")
            config = load_config(path)
            with self.assertRaises(ConfigError):
                config.agent_for_role("executor")

    def test_role_swap_and_unknown_account_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            config = load_config(path)
            swap_roles(config, "architect", "executor")
            config = load_config(path)
            self.assertEqual(config.roles["architect"], "secondary")
            self.assertEqual(config.roles["executor"], "primary")
            with self.assertRaises(ConfigError):
                from dual_codex.registry import assign_role

                assign_role(config, "executor", "missing")

    def test_rename_and_label_preserve_home_without_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            original_home = load_config(path).accounts["primary"].codex_home
            rename_account(load_config(path), "primary", "lead")
            config = load_config(path)
            self.assertEqual(config.accounts["lead"].codex_home, original_home)
            self.assertEqual(config.roles["architect"], "lead")
            from dual_codex.registry import label_account

            with patch("dual_codex.registry._run_login") as login:
                label_account(config, "lead", "Changed label")
                login.assert_not_called()
            config = load_config(path)
            self.assertEqual(config.accounts["lead"].label, "Changed label")

    def test_add_account_does_not_assign_a_role_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            config = load_config(path)
            new_home = Path(temp) / "profiles" / "third account"
            with patch("dual_codex.registry._run_login"):
                add_account(
                    config,
                    "third",
                    label="Third",
                    codex_home=str(new_home),
                    output=lambda _message: None,
                )
            updated = load_config(path)
            self.assertNotIn("third", updated.roles.values())
            self.assertTrue((new_home / "config.toml").exists())

    def test_remove_requires_unassignment_and_keeps_profile_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            config = load_config(path)
            with self.assertRaises(ConfigError):
                remove_account(config, "secondary")
            unassign_role(config, "executor")
            config = load_config(path)
            home = config.accounts["secondary"].codex_home
            home.mkdir(parents=True)
            remove_account(config, "secondary")
            self.assertTrue(home.exists())
            self.assertNotIn("secondary", load_config(path).accounts)

    def test_bom_input_and_bom_free_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
            config = load_config(path)
            write_registry_config(path, config.accounts, config.roles)
            self.assertNotEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
            self.assertEqual(load_config(path).accounts["primary"].name, "primary")

    def test_migration_dry_run_write_backup_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            architect_home = root / "Codex Profiles" / "architect"
            executor_home = root / "Codex Profiles" / "executor"
            path.write_bytes(
                b"\xef\xbb\xbf"
                + f'''# keep this comment\n[orchestrator]\nrepository = "{root.as_posix()}"\nrequire_clean_git = false\n\n[architect]\ncodex_home = "{architect_home.as_posix()}"\nmodel = ""\nreasoning_effort = "high"\nsandbox = "read-only"\n\n[executor]\ncodex_home = "{executor_home.as_posix()}"\nmodel = ""\nreasoning_effort = "high"\nsandbox = "workspace-write"\n'''.encode()
            )
            original = path.read_bytes()
            result = migrate_legacy_config(
                path,
                architect_name="biel3",
                executor_name="biel4",
                architect_label="Primary",
                executor_label="Secondary",
                dry_run=True,
            )
            self.assertFalse(result.changed)
            self.assertEqual(path.read_bytes(), original)

            result = migrate_legacy_config(
                path,
                architect_name="biel3",
                executor_name="biel4",
                architect_label="Primary",
                executor_label="Secondary",
                now=__import__("datetime").datetime(2026, 8, 6, tzinfo=__import__("datetime").timezone.utc),
            )
            self.assertTrue(result.changed)
            self.assertIsNotNone(result.backup_path)
            config = load_config(path)
            self.assertFalse(config.legacy)
            self.assertEqual(config.roles["reviewer"], "biel3")
            self.assertEqual(config.accounts["biel3"].codex_home, architect_home)
            self.assertNotEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
            self.assertTrue(result.backup_path.exists())
            second = migrate_legacy_config(path, dry_run=False)
            self.assertFalse(second.changed)

    def test_profile_config_is_utf8_without_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profile"
            ensure_codex_profile(path)
            profile_config = path / "config.toml"
            self.assertEqual(profile_config.read_text(encoding="utf-8").strip(), 'cli_auth_credentials_store = "file"')
            self.assertNotEqual(profile_config.read_bytes()[:3], b"\xef\xbb\xbf")

    def test_status_does_not_print_auth_path_or_auth_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            _write_registry(path)
            auth_path = Path(temp) / "profiles" / "primary account" / "auth.json"
            auth_path.parent.mkdir(parents=True)
            auth_path.write_text("placeholder-secret", encoding="utf-8")
            output = StringIO()
            with patch("dual_codex.cli.login_status", return_value="OK"), redirect_stdout(output):
                self.assertEqual(main(["--config", str(path), "status"]), 0)
            rendered = output.getvalue()
            self.assertIn("Primary", rendered)
            self.assertIn("architect", rendered)
            self.assertNotIn("auth.json", rendered)
            self.assertNotIn("placeholder-secret", rendered)


if __name__ == "__main__":
    unittest.main()
