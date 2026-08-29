from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from zimigrate.archive import MigrationArchive
from zimigrate.cli import DEFAULT_ARCHIVE, _configure_export_categories, build_parser, main
from zimigrate.config import (
    AppConfig,
    EndpointConfig,
    ImportConfig,
    TransferConfig,
)
from zimigrate.errors import ArchiveError, ConfigurationError


class CliTests(unittest.TestCase):
    def test_export_and_import_default_to_export_data(self) -> None:
        parser = build_parser()

        export_arguments = parser.parse_args(["export"])
        import_arguments = parser.parse_args(["import"])

        self.assertIsNone(export_arguments.config)
        self.assertEqual(export_arguments.archive, Path("export_data"))
        self.assertIsNone(export_arguments.target_ip)
        self.assertEqual(export_arguments.ssh_user, "root")
        self.assertEqual(export_arguments.user, [])
        self.assertEqual(export_arguments.domain, [])
        self.assertIsNone(import_arguments.config)
        self.assertEqual(import_arguments.archive, Path("export_data"))
        scoped = parser.parse_args(
            ["export", "--user", "deneme@deneme.com", "--domain", "other.com"]
        )
        self.assertEqual(scoped.user, ["deneme@deneme.com"])
        self.assertEqual(scoped.domain, ["other.com"])

    def test_export_uses_local_defaults_without_starting_import(self) -> None:
        config = _config()
        archive = MagicMock()
        archive.lock.return_value = nullcontext()
        exporter = MagicMock()
        exporter.run.return_value = {"export:account": 1}

        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=config) as load_config,
            patch("zimigrate.cli.MigrationArchive", return_value=archive) as migration_archive,
            patch("zimigrate.cli.Exporter", return_value=exporter),
            patch("zimigrate.cli.Importer") as importer,
            redirect_stdout(io.StringIO()),
        ):
            result = main(["export"])

        self.assertEqual(result, 0)
        load_config.assert_called_once_with(None)
        migration_archive.assert_called_once_with(
            DEFAULT_ARCHIVE,
            create=True,
        )
        exporter.run.assert_called_once_with()
        importer.assert_not_called()

    def test_export_user_flag_limits_transfer_scope(self) -> None:
        config = _config()
        archive = MagicMock()
        archive.manifest.return_value = None
        archive.lock.return_value = nullcontext()
        captured: list[object] = []

        def capture_exporter(received, *_args: object, **_kwargs: object) -> MagicMock:
            captured.append(received)
            runner = MagicMock()
            runner.run.return_value = {"export:account": 1}
            return runner

        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=config),
            patch("zimigrate.cli.MigrationArchive", return_value=archive),
            patch("zimigrate.cli.Exporter", side_effect=capture_exporter),
            redirect_stdout(io.StringIO()),
        ):
            result = main(["export", "--user", "deneme@deneme.com"])

        self.assertEqual(result, 0)
        scoped = captured[0]
        self.assertEqual(scoped.transfer.target_users, ("deneme@deneme.com",))
        self.assertFalse(scoped.transfer.include_distribution_lists)

    def test_invalid_user_flag_is_rejected(self) -> None:
        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=_config()),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = main(["export", "--user", "not-an-email"])
        self.assertEqual(result, 1)
        self.assertIn("Invalid --user value", stderr.getvalue())

    def test_import_validates_every_artifact_before_starting_import(self) -> None:
        config = _config()
        archive = MagicMock()
        archive.root = DEFAULT_ARCHIVE.resolve()
        archive.lock.return_value = nullcontext()
        archive.manifest.return_value = {"export_options": {}, "completed": True}
        archive.iter_entities.return_value = []
        archive.state.get.return_value = None
        events: list[str] = []
        importer = MagicMock()
        importer.run.side_effect = lambda: events.append("import") or {"import:account": 1}
        target_verifier = MagicMock()
        target_verifier.run.side_effect = lambda: events.append("target") or {"mismatches": 0}

        def verify(*_args: object, **_kwargs: object) -> dict[str, int]:
            events.append("verify")
            return {"account": 1, "mailbox_artifact": 1}

        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=config),
            patch("zimigrate.cli.MigrationArchive", return_value=archive),
            patch("zimigrate.cli.verify_archive", side_effect=verify) as verifier,
            patch("zimigrate.cli.Importer", return_value=importer),
            patch("zimigrate.cli.TargetVerifier", return_value=target_verifier),
            redirect_stdout(io.StringIO()),
        ):
            result = main(["import"])

        self.assertEqual(result, 0)
        self.assertEqual(events, ["verify", "import", "target"])
        verifier.assert_called_once_with(archive, deep=True, workers=config.transfer.workers)

    def test_failed_import_validation_prevents_target_mutation(self) -> None:
        config = _config()
        archive = MagicMock()
        archive.lock.return_value = nullcontext()
        archive.manifest.return_value = {"export_options": {}, "completed": True}
        archive.iter_entities.return_value = []
        archive.state.get.return_value = None

        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=config),
            patch("zimigrate.cli.MigrationArchive", return_value=archive),
            patch(
                "zimigrate.cli.verify_archive",
                side_effect=ArchiveError("corrupt archive"),
            ),
            patch("zimigrate.cli.Importer") as importer,
            redirect_stderr(io.StringIO()),
        ):
            result = main(["import"])

        self.assertEqual(result, 1)
        importer.assert_not_called()

    def test_export_target_ip_runs_local_exporter_over_ssh(self) -> None:
        config = _config()
        archive = MagicMock()
        archive.lock.return_value = nullcontext()
        archive.manifest.return_value = None
        archive.root = Path("export_data")
        session = MagicMock()
        session.user = "root"
        session.auth_method = "key"
        exporter = MagicMock()
        exporter.run.return_value = {"export:account": 1}

        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=config),
            patch("zimigrate.cli.MigrationArchive", return_value=archive),
            patch("zimigrate.cli.connect_ssh", return_value=session) as connect,
            patch("zimigrate.cli.bind_remote_export") as bind,
            patch("zimigrate.cli.Exporter", return_value=exporter) as exporter_cls,
            redirect_stdout(io.StringIO()),
        ):
            result = main(["export", "--target-ip", "192.0.2.10"])

        self.assertEqual(result, 0)
        connect.assert_called_once_with("192.0.2.10", user="root")
        bind.assert_called_once()
        self.assertEqual(bind.call_args.kwargs["host"], "192.0.2.10")
        exporter_cls.assert_called_once()
        self.assertIs(exporter_cls.call_args.kwargs["session"], session)
        exporter.run.assert_called_once_with()
        session.close.assert_called_once()

    def test_drain_mode_does_not_reenter_ssh_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "export_data"
            archive = MigrationArchive(archive_path, create=True)
            reports = archive.root / "reports"
            reports.mkdir(exist_ok=True)
            (reports / "remote-export.json").write_text(
                '{"schema_version":1,"target_ip":"10.1.0.20","ssh_user":"root",'
                '"archive_id":"abc","remote_root":"/var/tmp/zimigratex/abc",'
                '"auth":"key","categories":["domains","cos","accounts","mailboxes"]}\n',
                encoding="utf-8",
            )
            exporter = MagicMock()
            exporter.run.return_value = {"export:account": 1}
            connect = MagicMock()
            with (
                patch("zimigrate.cli.configure_logging"),
                patch("zimigrate.cli.load_config", return_value=_config()),
                patch("zimigrate.cli.Exporter", return_value=exporter),
                patch("zimigrate.cli.connect_ssh", connect),
                patch.dict("os.environ", {"ZIMIGRATE_EXPORT_DRAIN": "1"}),
                redirect_stdout(io.StringIO()),
            ):
                result = main(["export", "--archive", str(archive_path)])
            self.assertEqual(result, 0)
            exporter.run.assert_called_once_with()
            connect.assert_not_called()

    def test_export_categories_resume_from_remote_meta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = MigrationArchive(Path(directory) / "export_data", create=True)
            reports = archive.root / "reports"
            reports.mkdir(exist_ok=True)
            (reports / "remote-export.json").write_text(
                '{"schema_version":1,"target_ip":"10.1.0.20","ssh_user":"root",'
                '"archive_id":"abc","remote_root":"/var/tmp/zimigratex/abc",'
                '"auth":"password","categories":["domains","cos","accounts","mailboxes"]}\n',
                encoding="utf-8",
            )
            with (
                patch("zimigrate.cli._is_interactive", return_value=False),
                patch("zimigrate.cli.prompt_categories", side_effect=AssertionError("prompted")),
            ):
                updated = _configure_export_categories(_config(), archive)
            self.assertTrue(updated.transfer.include_mailboxes)
            self.assertTrue(updated.transfer.include_accounts)
            self.assertFalse(updated.transfer.include_distribution_lists)

    def test_interactive_export_prompts_even_when_remote_categories_are_stored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = MigrationArchive(Path(directory) / "export_data", create=True)
            reports = archive.root / "reports"
            reports.mkdir(exist_ok=True)
            (reports / "remote-export.json").write_text(
                '{"schema_version":1,"target_ip":"10.1.0.20","ssh_user":"root",'
                '"archive_id":"abc","remote_root":"/var/tmp/zimigratex/abc",'
                '"auth":"key","categories":["domains","cos","accounts","mailboxes"]}\n',
                encoding="utf-8",
            )
            with (
                patch("zimigrate.cli._is_interactive", return_value=True),
                patch(
                    "zimigrate.cli.prompt_categories",
                    return_value={"domains", "cos", "accounts"},
                ) as prompt,
            ):
                updated = _configure_export_categories(_config(), archive)
            prompt.assert_called_once()
            self.assertEqual(
                prompt.call_args.kwargs["defaults"],
                {"domains", "cos", "accounts", "mailboxes"},
            )
            self.assertTrue(updated.transfer.include_accounts)
            self.assertFalse(updated.transfer.include_mailboxes)

    def test_interactive_export_rejects_category_change_after_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = MigrationArchive(Path(directory) / "export_data", create=True)
            (archive.root / "manifest.json").write_text(
                '{"schema_version":1,"export_options":{"include_domains":true,'
                '"include_cos":true,"include_accounts":true,"include_mailboxes":true,'
                '"include_distribution_lists":false}}\n',
                encoding="utf-8",
            )
            with (
                patch("zimigrate.cli._is_interactive", return_value=True),
                patch(
                    "zimigrate.cli.prompt_categories",
                    return_value={"domains"},
                ),
                self.assertRaises(ConfigurationError),
            ):
                _configure_export_categories(_config(), archive)


def _config() -> AppConfig:
    return AppConfig(
        source=EndpointConfig(),
        target=EndpointConfig(),
        transfer=TransferConfig(include_secrets=False),
        import_options=ImportConfig(),
    )


if __name__ == "__main__":
    unittest.main()
