from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zimigrate.archive import MigrationArchive
from zimigrate.config import EndpointConfig
from zimigrate.errors import ConfigurationError
from zimigrate.remote_export import (
    bind_remote_export,
    resolve_remote_host,
    stored_export_categories,
)
from zimigrate.runner import CommandRunner
from zimigrate.ssh import SshSession, strict_host_key_checking
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
                '"auth":"key","schema_version":1}\n',
                encoding="utf-8",
            )
            self.assertEqual(resolve_remote_host(root, None), "192.0.2.10")
            self.assertEqual(resolve_remote_host(root, "192.0.2.10"), "192.0.2.10")
            with self.assertRaises(ConfigurationError):
                resolve_remote_host(root, "192.0.2.11")

    def test_bind_remote_export_writes_host_and_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = MigrationArchive(Path(directory) / "export_data", create=True)
            bind_remote_export(
                archive,
                host="192.0.2.10",
                ssh_user="root",
                auth="key",
                categories=("accounts", "cos", "domains"),
            )
            stored = stored_export_categories(archive.root)
            self.assertEqual(stored, {"accounts", "cos", "domains"})
            meta = (archive.root / "reports" / "remote-export.json").read_text(encoding="utf-8")
            self.assertIn("192.0.2.10", meta)
            self.assertNotIn("remote_root", meta)

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

    def test_runner_wraps_remote_commands_with_sudo_unless_login_is_zimbra(self) -> None:
        session = MagicMock()
        session.user = "root"
        session.remote_argv.side_effect = lambda command, tty=False: ["ssh", "--", *command]
        runner = CommandRunner(
            EndpointConfig(),
            retries=0,
            retry_base_seconds=0,
            session=session,
        )
        self.assertTrue(runner.is_remote)
        transported = runner._transport_command(["/opt/zimbra/bin/zmprov", "help"])
        self.assertEqual(
            transported,
            [
                "ssh",
                "--",
                "sudo",
                "-n",
                "-u",
                "zimbra",
                "--",
                "env",
                "LC_ALL=C",
                "/opt/zimbra/bin/zmprov",
                "help",
            ],
        )
        session.user = "zimbra"
        transported = runner._transport_command(["/opt/zimbra/bin/zmmailbox", "help"])
        self.assertEqual(
            transported,
            ["ssh", "--", "env", "LC_ALL=C", "/opt/zimbra/bin/zmmailbox", "help"],
        )

    def test_ssh_options_disable_compression_and_keep_the_session_alive(self) -> None:
        session = SshSession("192.0.2.10", user="root")
        session._control_path = Path("/tmp/zimigrate-ssh-test/mux")
        options = session._ssh_options(master="no")
        self.assertIn("Compression=no", options)
        self.assertIn("ServerAliveInterval=30", options)
        self.assertIn("ServerAliveCountMax=20", options)

    def test_strict_host_key_checking_falls_back_on_old_openssh(self) -> None:
        completed = MagicMock()
        completed.stderr = b'command-line line 0: unsupported option "accept-new".\n'
        with patch("zimigrate.ssh.subprocess.run", return_value=completed):
            strict_host_key_checking.cache_clear()
            self.assertEqual(strict_host_key_checking("/usr/bin/ssh"), "yes")
            strict_host_key_checking.cache_clear()


if __name__ == "__main__":
    unittest.main()
