from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zimigrate.archive import MigrationArchive
from zimigrate.config import TransferConfig, load_config
from zimigrate.errors import ConfigurationError
from zimigrate.remote_export import (
    _write_transfer_toml,
    resolve_remote_host,
    run_remote_export,
)
from zimigrate.ssh_askpass import main as askpass_main
from zimigrate.util import is_valid_ssh_target


class RemoteExportTests(unittest.TestCase):
    def test_ssh_targets_accept_ip_and_dns(self) -> None:
        self.assertTrue(is_valid_ssh_target("192.0.2.10"))
        self.assertTrue(is_valid_ssh_target("mail.example.com"))
        self.assertFalse(is_valid_ssh_target("not a host"))
        self.assertFalse(is_valid_ssh_target(""))

    def test_resume_binds_the_original_remote_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            (reports / "remote-export.json").write_text(
                '{"target_ip":"192.0.2.10","ssh_user":"root","archive_id":"abc",'
                '"remote_root":"/var/tmp/zimigratex/abc","auth":"key",'
                '"schema_version":1}\n',
                encoding="utf-8",
            )
            self.assertEqual(resolve_remote_host(root, None), "192.0.2.10")
            self.assertEqual(resolve_remote_host(root, "192.0.2.10"), "192.0.2.10")
            with self.assertRaises(ConfigurationError):
                resolve_remote_host(root, "192.0.2.11")

    def test_transfer_toml_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote-transfer.toml"
            transfer = TransferConfig(
                include_mailboxes=False,
                target_domains=("example.com",),
                workers=4,
            )
            _write_transfer_toml(path, transfer)
            loaded = load_config(path).transfer
            self.assertFalse(loaded.include_mailboxes)
            self.assertEqual(loaded.target_domains, ("example.com",))
            self.assertEqual(loaded.workers, 4)

    def test_askpass_reads_password_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("secret-value", encoding="utf-8")
            with (
                patch.dict("os.environ", {"ZIMIGRATE_SSH_ASKPASS_FILE": str(path)}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(askpass_main(), 0)
            self.assertEqual(stdout.getvalue(), "secret-value")

    def test_remote_export_copies_then_runs_then_pulls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = MigrationArchive(Path(directory) / "export_data", create=True)
            session = MagicMock()
            session.user = "root"
            session.auth_method = "key"
            process = MagicMock()
            process.poll.return_value = 0
            process.returncode = 0
            session.start.return_value = process
            with (
                patch("zimigrate.remote_export.find_tool_root", return_value=Path(directory)),
                patch("zimigrate.remote_export.connect_ssh", return_value=session),
            ):
                result = run_remote_export(
                    archive,
                    TransferConfig(include_mailboxes=False),
                    host="192.0.2.10",
                    ssh_user="root",
                )
            session.run.assert_called()
            self.assertTrue(
                any(
                    call.args and call.args[0][:2] == ["mkdir", "-p"]
                    for call in session.run.call_args_list
                )
            )
            self.assertTrue(session.rsync_to_remote.called)
            self.assertTrue(session.rsync_from_remote.called)
            self.assertTrue(session.start.called)
            started = session.start.call_args[0][0]
            self.assertEqual(started[0], "env")
            self.assertIn("ZIMIGRATE_EXPORT_DRAIN=1", started)
            self.assertTrue(
                any(
                    "mailboxes" in call.kwargs.get("excludes", ())
                    for call in session.rsync_to_remote.call_args_list
                )
            )
            self.assertTrue(session.close.called)
            self.assertEqual(result["host"], "192.0.2.10")
            self.assertTrue((archive.root / "reports" / "remote-export.json").is_file())

    def test_drain_copies_ready_mailbox_then_deletes_remote_copy(self) -> None:
        from zimigrate.remote_export import drain_ready_mailboxes

        with tempfile.TemporaryDirectory() as directory:
            local_root = Path(directory)
            relative = "mailboxes/user/full.tgz"
            ready = local_root / "reports" / "drain-ready"
            ready.mkdir(parents=True)
            marker = ready / "abc.json"
            marker.write_text(
                '{"path":"mailboxes/user/full.tgz","sha256":"abc","size":4}\n',
                encoding="utf-8",
            )
            session = MagicMock()

            def fake_rsync_file(remote_file: str, local_file: Path) -> None:
                local_file.parent.mkdir(parents=True, exist_ok=True)
                local_file.write_bytes(b"data")

            session.rsync_file_from_remote.side_effect = fake_rsync_file
            pulled = drain_ready_mailboxes(session, "/var/tmp/zimigratex/id/archive", local_root)
            self.assertEqual(pulled, 1)
            self.assertEqual((local_root / relative).read_bytes(), b"data")
            session.run.assert_called_with(
                [
                    "rm",
                    "-f",
                    "/var/tmp/zimigratex/id/archive/mailboxes/user/full.tgz",
                    "/var/tmp/zimigratex/id/archive/reports/drain-ready/abc.json",
                ]
            )
            self.assertFalse(marker.is_file())


if __name__ == "__main__":
    unittest.main()
