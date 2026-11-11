from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from zimigrate.archive import MigrationArchive
from zimigrate.cli import _configure_export_categories, _configure_import_categories
from zimigrate.config import load_config
from zimigrate.errors import ConfigurationError
from zimigrate.scope import apply_scope_to_transfer, parse_target_scope
from zimigrate.selection import WITHOUT_MAILBOXES_LABEL, all_categories, selected_categories


class CategoryStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.archive = MigrationArchive(Path(directory.name) / "archive", create=True)
        self.addCleanup(self.archive.close_state)
        self.config = load_config(None)

    def _write_manifest(self) -> None:
        (self.archive.root / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "export_options": {}}), encoding="utf-8"
        )

    def _bind_import(self, *, mailboxes: bool) -> None:
        self._write_manifest()
        self.archive.state.start("import:configuration", "options")
        self.archive.state.succeed(
            "import:configuration",
            "options",
            detail=json.dumps({"include_mailboxes": mailboxes}),
        )

    def test_startup_offers_without_mailboxes_for_both_operations(self) -> None:
        for action, configure in (
            ("export", _configure_export_categories),
            ("import", _configure_import_categories),
        ):
            with self.subTest(action=action):
                if action == "import":
                    self._write_manifest()
                with (
                    patch("zimigrate.cli._is_interactive", return_value=True),
                    patch(
                        "builtins.input", side_effect=["1", "6"] if action == "import" else ["6"]
                    ),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    config = configure(self.config, self.archive)
                self.assertIn(WITHOUT_MAILBOXES_LABEL, output.getvalue())
                self.assertEqual(
                    selected_categories(config.transfer), all_categories() - {"mailboxes"}
                )

    def test_scoped_import_still_offers_without_mailboxes(self) -> None:
        self._write_manifest()
        for users, domains in ((["user@example.com"], []), ([], ["example.com"])):
            scope = parse_target_scope(users, domains)
            config = replace(
                self.config,
                transfer=apply_scope_to_transfer(self.config.transfer, scope),
            )
            with (
                self.subTest(users=users, domains=domains),
                patch("zimigrate.cli._is_interactive", return_value=True),
                patch("zimigrate.cli.prompt_import_scope") as scope_prompt,
                patch("builtins.input", return_value="6") as category_input,
                redirect_stdout(io.StringIO()),
            ):
                updated = _configure_import_categories(config, self.archive)
                category_input.assert_called_once()
                scope_prompt.assert_not_called()
                self.assertFalse(updated.transfer.include_mailboxes)
                self.assertTrue(updated.transfer.include_accounts)
                self.assertEqual(updated.transfer.target_users, config.transfer.target_users)
                self.assertEqual(updated.transfer.target_domains, config.transfer.target_domains)

    def test_import_resume_shows_menu_with_locked_defaults(self) -> None:
        self._bind_import(mailboxes=False)
        with (
            patch("zimigrate.cli._is_interactive", return_value=True),
            patch("builtins.input", return_value="") as category_input,
            redirect_stdout(io.StringIO()) as output,
        ):
            updated = _configure_import_categories(self.config, self.archive)
        category_input.assert_called_once()
        self.assertIn(WITHOUT_MAILBOXES_LABEL, output.getvalue())
        self.assertFalse(updated.transfer.include_mailboxes)

    def test_import_resume_rejects_category_change(self) -> None:
        self._bind_import(mailboxes=True)
        with (
            patch("zimigrate.cli._is_interactive", return_value=True),
            patch("builtins.input", return_value="6"),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ConfigurationError, "different categories"),
        ):
            _configure_import_categories(self.config, self.archive)

    def test_noninteractive_import_resume_preserves_locked_selection(self) -> None:
        self._bind_import(mailboxes=False)
        with (
            patch("zimigrate.cli._is_interactive", return_value=False),
            patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
        ):
            updated = _configure_import_categories(self.config, self.archive)
        self.assertFalse(updated.transfer.include_mailboxes)
