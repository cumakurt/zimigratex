from __future__ import annotations

import io
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from zimigrate.cli import DEFAULT_ARCHIVE, build_parser, main
from zimigrate.config import (
    AppConfig,
    ArchiveConfig,
    EndpointConfig,
    ImportConfig,
    TransferConfig,
)
from zimigrate.errors import ArchiveError


class CliTests(unittest.TestCase):
    def test_export_and_import_default_to_export_data(self) -> None:
        parser = build_parser()

        export_arguments = parser.parse_args(["export"])
        import_arguments = parser.parse_args(["import"])

        self.assertIsNone(export_arguments.config)
        self.assertEqual(export_arguments.archive, Path("export_data"))
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
        config = _unencrypted_config()
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
            config.archive,
            create=True,
        )
        exporter.run.assert_called_once_with()
        importer.assert_not_called()

    def test_export_user_flag_limits_transfer_scope(self) -> None:
        config = _unencrypted_config()
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
        self.assertFalse(scoped.transfer.include_global_config)
        self.assertFalse(scoped.transfer.include_distribution_lists)

    def test_invalid_user_flag_is_rejected(self) -> None:
        with (
            patch("zimigrate.cli.configure_logging"),
            patch("zimigrate.cli.load_config", return_value=_unencrypted_config()),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = main(["export", "--user", "not-an-email"])
        self.assertEqual(result, 1)
        self.assertIn("Invalid --user value", stderr.getvalue())

    def test_import_validates_every_artifact_before_starting_import(self) -> None:
        config = _unencrypted_config()
        archive = MagicMock()
        archive.root = DEFAULT_ARCHIVE.resolve()
        archive.lock.return_value = nullcontext()
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
        config = _unencrypted_config()
        archive = MagicMock()
        archive.lock.return_value = nullcontext()

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


def _unencrypted_config() -> AppConfig:
    return AppConfig(
        source=EndpointConfig(),
        target=EndpointConfig(),
        archive=ArchiveConfig(),
        transfer=TransferConfig(include_secrets=False),
        import_options=ImportConfig(),
    )


if __name__ == "__main__":
    unittest.main()
