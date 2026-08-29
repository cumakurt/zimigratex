from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from zimigrate.drain import (
    operator_prompts_enabled,
    parse_drain_request,
    request_mailbox_drain,
    validate_mailbox_relative,
    wait_until_removed,
)
from zimigrate.errors import ZimigrateError
from zimigrate.remote_export import join_remote_path


class DrainTests(unittest.TestCase):
    def test_validate_mailbox_relative_rejects_escape(self) -> None:
        validate_mailbox_relative("mailboxes/user/full.tgz")
        with self.assertRaises(ZimigrateError):
            validate_mailbox_relative("../mailboxes/user/full.tgz")
        with self.assertRaises(ZimigrateError):
            validate_mailbox_relative("/mailboxes/user/full.tgz")
        with self.assertRaises(ZimigrateError):
            validate_mailbox_relative("objects/account/user.json")

    def test_join_remote_path_rejects_escape(self) -> None:
        self.assertEqual(
            join_remote_path("/var/tmp/zimigratex/id/archive", "mailboxes/user/full.tgz"),
            "/var/tmp/zimigratex/id/archive/mailboxes/user/full.tgz",
        )
        with self.assertRaises(ZimigrateError):
            join_remote_path("/var/tmp/zimigratex/id/archive", "../etc/passwd")

    def test_parse_drain_request_requires_mailbox_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marker.json"
            path.write_text(
                '{"path":"objects/account/x.json","sha256":"abc","size":1}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ZimigrateError):
                parse_drain_request(path)

    def test_wait_until_removed_returns_after_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.tgz"
            path.write_bytes(b"data")

            def remove() -> None:
                path.unlink()

            threading.Timer(0.05, remove).start()
            wait_until_removed(path)
            self.assertFalse(path.is_file())

    def test_request_mailbox_drain_is_a_no_op_without_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mailboxes" / "user" / "full.tgz"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"data")
            request_mailbox_drain(
                Path(directory),
                relative="mailboxes/user/full.tgz",
                sha256="abc",
                size=4,
            )
            self.assertTrue(path.is_file())
            self.assertFalse((Path(directory) / "reports" / "drain-ready").exists())

    def test_orchestrated_export_disables_operator_prompts(self) -> None:
        self.assertTrue(operator_prompts_enabled())
        with patch.dict("os.environ", {"ZIMIGRATE_EXPORT_DRAIN": "1"}):
            self.assertFalse(operator_prompts_enabled())
        with patch.dict("os.environ", {"ZIMIGRATE_NONINTERACTIVE": "1"}):
            self.assertFalse(operator_prompts_enabled())


if __name__ == "__main__":
    unittest.main()
