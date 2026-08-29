from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zimigrate.capacity import assess_export_disk, assess_import_disks
from zimigrate.config import TransferConfig
from zimigrate.errors import ConfigurationError
from zimigrate.selection import (
    CATEGORIES,
    WITHOUT_MAILBOXES_LABEL,
    normalize_categories,
    prompt_categories,
    transfer_with_categories,
)


class SelectionCapacityTests(unittest.TestCase):
    def test_dependencies_are_added(self) -> None:
        self.assertEqual(
            normalize_categories({"mailboxes"}), {"mailboxes", "accounts", "domains", "cos"}
        )
        transfer = transfer_with_categories(TransferConfig(), {"distribution_lists"})
        self.assertTrue(transfer.include_distribution_lists)
        self.assertTrue(transfer.include_domains)
        self.assertFalse(transfer.include_accounts)

    def test_prompt_rejects_disabled_category(self) -> None:
        with patch("builtins.input", return_value="2"), self.assertRaises(ConfigurationError):
            prompt_categories(
                "import",
                available={"domains", "cos"},
                defaults={"domains"},
                disabled_reasons={"cos": "not available in this archive"},
            )

    def test_without_mailboxes_shortcut_selects_provisioning_only(self) -> None:
        available = {
            "domains",
            "cos",
            "accounts",
            "mailboxes",
            "distribution_lists",
        }
        shortcut = str(len(CATEGORIES) + 1)
        mailbox_number = next(
            str(number)
            for number, category in enumerate(CATEGORIES, start=1)
            if category.key == "mailboxes"
        )
        with (
            patch("builtins.input", return_value=shortcut),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            selected = prompt_categories(
                "export",
                available=available,
                defaults=available,
            )
        self.assertEqual(
            selected,
            {"domains", "cos", "accounts", "distribution_lists"},
        )
        self.assertIn(WITHOUT_MAILBOXES_LABEL, stdout.getvalue())

        with patch("builtins.input", return_value=""):
            selected = prompt_categories(
                "export",
                available=available,
                defaults=available,
            )
        self.assertIn("mailboxes", selected)

        with patch("builtins.input", return_value=f"{shortcut},{mailbox_number}"):
            selected = prompt_categories(
                "export",
                available=available,
                defaults=available,
            )
        self.assertEqual(
            selected,
            {"domains", "cos", "accounts", "mailboxes", "distribution_lists"},
        )

    def test_without_mailboxes_shortcut_uses_archive_categories(self) -> None:
        available = {"domains", "cos", "accounts", "distribution_lists"}
        with patch("builtins.input", return_value=str(len(CATEGORIES) + 1)):
            selected = prompt_categories(
                "import",
                available=available,
                defaults=available,
            )
        self.assertEqual(selected, available)
        self.assertNotIn("mailboxes", selected)

    def test_capacity_assessments_report_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            disk = SimpleNamespace(total=10 * 1024**3, free=10 * 1024**3)
            with patch("zimigrate.capacity.shutil.disk_usage", return_value=disk):
                export = assess_export_disk(
                    path,
                    remaining_accounts=["u@example.com"],
                    mailbox_usage={"u@example.com": 1024},
                    include_mailboxes=True,
                    workers=1,
                )
                self.assertEqual(export.status, "sufficient")
                imported = assess_import_disks(
                    path,
                    volume_paths={"primaryMessage": [path], "index": [path]},
                    remaining_mailbox_sizes=[1024],
                    remaining_accounts=1,
                    workers=1,
                )
                self.assertEqual(imported.status, "sufficient")

    def test_unmeasured_remaining_accounts_use_minimum_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            disk = SimpleNamespace(total=10 * 1024**3, free=10 * 1024**3)
            with patch("zimigrate.capacity.shutil.disk_usage", return_value=disk):
                export = assess_export_disk(
                    path,
                    remaining_accounts=["missing@example.com"],
                    mailbox_usage={"other@example.com": 8 * 1024**3},
                    include_mailboxes=True,
                    workers=1,
                )
            self.assertGreater(export.estimated_unmeasured_mailbox_bytes, 0)
            self.assertGreaterEqual(export.estimated_unmeasured_mailbox_bytes, 64 * 1024**2)

    def test_capacity_assessments_report_insufficient_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            disk = SimpleNamespace(total=10 * 1024**3, free=100 * 1024**2)
            with patch("zimigrate.capacity.shutil.disk_usage", return_value=disk):
                export = assess_export_disk(
                    path,
                    remaining_accounts=["u@example.com"],
                    mailbox_usage={"u@example.com": 8 * 1024**3},
                    include_mailboxes=True,
                    workers=1,
                )
                self.assertEqual(export.status, "insufficient")
                imported = assess_import_disks(
                    path,
                    volume_paths={"primaryMessage": [path], "index": [path]},
                    remaining_mailbox_sizes=[8 * 1024**3],
                    remaining_accounts=1,
                    workers=1,
                )
                self.assertEqual(imported.status, "insufficient")

    def test_drained_export_capacity_ignores_full_archive_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            disk = SimpleNamespace(total=100 * 1024**3, free=40 * 1024**3)
            accounts = [f"u{index}@example.com" for index in range(50)]
            usage = {account: 8 * 1024**3 for account in accounts}
            with patch("zimigrate.capacity.shutil.disk_usage", return_value=disk):
                stored = assess_export_disk(
                    path,
                    remaining_accounts=accounts,
                    mailbox_usage=usage,
                    include_mailboxes=True,
                    workers=1,
                )
                drained = assess_export_disk(
                    path,
                    remaining_accounts=accounts,
                    mailbox_usage=usage,
                    include_mailboxes=True,
                    workers=1,
                    drain_completed_artifacts=True,
                )
            self.assertEqual(stored.status, "insufficient")
            self.assertEqual(drained.status, "sufficient")
            self.assertLess(
                drained.estimated_required_free_bytes,
                stored.estimated_required_free_bytes,
            )


if __name__ == "__main__":
    unittest.main()
