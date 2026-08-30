from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zimigrate.archive import MigrationArchive
from zimigrate.backups import discover_backups, prompt_backup_choice, summarize_backup
from zimigrate.errors import ConfigurationError
from zimigrate.selection import prompt_domain_selection, prompt_import_scope


class BackupDiscoveryTests(unittest.TestCase):
    def test_discover_summarizes_complete_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = MigrationArchive(root / "export_data", create=True)
            archive.write_manifest(
                "Release 10.1.18.GA FOSS",
                completed=True,
                source_host="mail.example.com",
                export_options={
                    "include_domains": True,
                    "include_cos": True,
                    "include_accounts": True,
                    "include_mailboxes": False,
                    "include_distribution_lists": True,
                },
            )
            domain = root / "export_data" / "objects" / "domain"
            domain.mkdir(parents=True)
            (domain / "example.json").write_text(
                '{"kind":"domain","name":"example.com","attributes":{}}',
                encoding="utf-8",
            )
            backups = discover_backups(root)
            self.assertEqual(len(backups), 1)
            self.assertTrue(backups[0].completed)
            self.assertEqual(backups[0].source_host, "mail.example.com")
            self.assertEqual(backups[0].domains, ("example.com",))
            self.assertNotIn("mailboxes", backups[0].categories)

    def test_prompt_selects_numbered_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = summarize_backup(_write_archive(root / "one", host="a.example.com"))
            second = summarize_backup(_write_archive(root / "two", host="b.example.com"))
            assert first is not None
            assert second is not None
            with patch("builtins.input", return_value="2"):
                chosen = prompt_backup_choice([first, second], default=first.path)
            self.assertEqual(chosen, second.path)

    def test_discovery_ignores_inaccessible_unrelated_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = _write_archive(root / "valid", host="mail.example.com")
            blocked = root / "blocked"
            blocked.mkdir()
            blocked.chmod(0)
            try:
                backups = discover_backups(root)
            finally:
                blocked.chmod(0o700)

            self.assertEqual([backup.path for backup in backups], [valid.resolve()])

    def test_import_scope_and_domain_prompts(self) -> None:
        with patch("builtins.input", return_value=""):
            self.assertEqual(prompt_import_scope(has_domains=True), "full")
        with patch("builtins.input", return_value="2"):
            self.assertEqual(prompt_import_scope(has_domains=True), "domains")
        with patch("builtins.input", return_value="2,1"):
            self.assertEqual(
                prompt_domain_selection(["example.com", "other.com"]),
                ["example.com", "other.com"],
            )
        with patch("builtins.input", return_value=""), self.assertRaises(ConfigurationError):
            prompt_domain_selection(["example.com"])


def _write_archive(path: Path, *, host: str) -> Path:
    archive = MigrationArchive(path, create=True)
    archive.write_manifest("Release 8.8.15.GA FOSS", completed=True, source_host=host)
    return path


if __name__ == "__main__":
    unittest.main()
