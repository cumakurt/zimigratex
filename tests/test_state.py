from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zimigrate.errors import ZimigrateError
from zimigrate.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_running_and_failed_operations_are_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            state.start("export:account", "a@example.com")
            self.assertFalse(state.is_success("export:account", "a@example.com"))
            state.fail("export:account", "a@example.com", "temporary failure")
            state.start("export:account", "a@example.com")
            state.succeed(
                "export:account",
                "a@example.com",
                artifact_path="objects/account/a",
                checksum="abc",
            )

            record = state.get("export:account", "a@example.com")
            assert record is not None
            self.assertEqual(record.status, "success")
            self.assertEqual(record.attempts, 2)
            self.assertTrue(state.is_success("export:account", "a@example.com"))

            state.start("export:account", "a@example.com")
            retried = state.get("export:account", "a@example.com")
            assert retried is not None
            self.assertIsNone(retried.artifact_path)
            self.assertIsNone(retried.checksum)
            self.assertIsNone(retried.detail)

    def test_succeed_without_start_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            with self.assertRaises(ZimigrateError):
                state.succeed("export:account", "missing@example.com")


if __name__ == "__main__":
    unittest.main()
