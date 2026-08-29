from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zimigrate.config import load_config
from zimigrate.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_defaults_are_ready_for_local_execution(self) -> None:
        config = load_config()

        self.assertEqual(config.source.zimbra_user, "zimbra")
        self.assertEqual(config.target.zimbra_user, "zimbra")
        self.assertEqual(config.transfer.account_include, ("*",))
        self.assertEqual(config.transfer.workers, 8)
        self.assertEqual(config.import_options.expected_target_version_pattern, "")
        self.assertTrue(config.import_options.allows_version("Release 8.8.15.GA FOSS"))
        self.assertTrue(config.import_options.allows_version("Release 9.0.0.GA FOSS"))
        self.assertTrue(config.import_options.allows_version("Release 10.1.18.GA FOSS"))
        self.assertFalse(config.import_options.allow_unverified_remote_capacity)

    def test_removed_archive_encryption_settings_are_rejected(self) -> None:
        config_text = """
[archive]
encryption_enabled = true
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(config_text, encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_remote_endpoint_settings_are_rejected(self) -> None:
        config_text = """
[source]
mode = "ssh"
host = "mail.example.com"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(config_text, encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_unknown_and_wrongly_typed_settings_are_rejected(self) -> None:
        values = (
            "[transfer]\nworker = 8\n",
            '[transfer]\ninclude_mailboxes = "false"\n',
            "[source]\ncommand_timeout_seconds = true\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, config_text in enumerate(values):
                with self.subTest(config_text=config_text):
                    path = Path(directory) / f"config-{index}.toml"
                    path.write_text(config_text, encoding="utf-8")
                    with self.assertRaises(ConfigurationError):
                        load_config(path)

    def test_optional_target_version_pattern_can_pin_a_release(self) -> None:
        config_text = r"""
[import]
expected_target_version_pattern = '\b10\.1\.18(?:\b|\.)'
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(config_text, encoding="utf-8")
            config = load_config(path)
        self.assertTrue(config.import_options.allows_version("Release 10.1.18.GA FOSS"))
        self.assertFalse(config.import_options.allows_version("Release 8.8.15.GA FOSS"))


if __name__ == "__main__":
    unittest.main()
