from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zimigrate.capacity import assess_export_disk, assess_import_disks
from zimigrate.config import TransferConfig
from zimigrate.errors import ConfigurationError
from zimigrate.selection import normalize_categories, prompt_categories, transfer_with_categories


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
                available={"domains", "global_config"},
                defaults={"domains"},
                disabled_reasons={"global_config": "allowlist required"},
            )

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


if __name__ == "__main__":
    unittest.main()
