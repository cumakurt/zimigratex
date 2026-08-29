from __future__ import annotations

import io
import stat
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from zimigrate.archive import MigrationArchive, validate_mailbox_archive
from zimigrate.errors import ArchiveError
from zimigrate.models import Artifact, EntityRecord
from zimigrate.util import sha256_file
from zimigrate.verifier import verify_archive


class ArchiveTests(unittest.TestCase):
    def test_entity_and_mailbox_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            archive = MigrationArchive(root, create=True)
            record = EntityRecord(
                kind="account",
                name="user@example.com",
                attributes={"userPassword": ["{SSHA}sensitive"]},
            )

            relative, _ = archive.write_entity(record)
            stored = (root / relative).read_bytes()
            self.assertIn(b"sensitive", stored)
            self.assertEqual(
                archive.read_entity("account", "user@example.com").attributes,
                record.attributes,
            )

            plaintext = root / ".tmp" / "mailbox.tgz"
            plaintext.parent.mkdir()
            _write_tgz(plaintext)
            mailbox_relative = archive.mailbox_relative_path("user@example.com", "full")
            checksum, _ = archive.store_mailbox(plaintext, mailbox_relative)
            self.assertFalse(plaintext.exists())
            archive.validate_mailbox_artifact(
                mailbox_relative,
                checksum,
                deep=True,
            )
            materialized = root / mailbox_relative
            self.assertEqual(sha256_file(materialized), checksum)
            validate_mailbox_archive(materialized, "tgz")

            record.artifacts = [
                Artifact(
                    label="full",
                    path=mailbox_relative,
                    sha256=checksum,
                    size=(root / mailbox_relative).stat().st_size,
                    query="is:anywhere",
                )
            ]
            relative, entity_checksum = archive.write_entity(record)
            archive.state.start("export:account", record.name)
            archive.state.succeed(
                "export:account",
                record.name,
                artifact_path=relative,
                checksum=entity_checksum,
            )
            archive.write_manifest("Release 8.8.15.GA FOSS", completed=True)
            self.assertFalse(archive.manifest()["encrypted"])
            self.assertEqual(
                verify_archive(archive, deep=True, workers=2)["mailbox_artifact"],
                1,
            )

            orphan = root / "mailboxes" / "orphan.tgz"
            orphan.write_bytes(b"unreferenced")
            with self.assertRaises(ArchiveError):
                verify_archive(archive, deep=True, workers=2)

    def test_entity_checksum_detects_record_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            archive = MigrationArchive(root, create=True)
            record = EntityRecord(
                kind="domain",
                name="example.com",
                attributes={"description": ["original"]},
            )
            relative, checksum = archive.write_entity(record)
            archive.state.start("export:domain", record.name)
            archive.state.succeed(
                "export:domain",
                record.name,
                artifact_path=relative,
                checksum=checksum,
            )
            archive.write_manifest("Release 10.1.18.GA FOSS", completed=True)

            (root / relative).write_text(
                (root / relative).read_text(encoding="utf-8").replace("original", "tampered"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ArchiveError, "checksum mismatch"):
                verify_archive(archive, deep=False)

    def test_archive_requires_its_checkpoint_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            archive = MigrationArchive(root, create=True)
            archive.write_manifest("Release 10.1.18.GA FOSS", completed=False)
            archive.state.close()
            (root / "state.sqlite3").unlink()

            with self.assertRaisesRegex(ArchiveError, "checkpoint database is missing"):
                MigrationArchive(root, create=False)

    def test_archive_rejects_a_corrupt_checkpoint_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            archive = MigrationArchive(root, create=True)
            archive.write_manifest("Release 10.1.18.GA FOSS", completed=False)
            archive.state.close()
            (root / "state.sqlite3").write_bytes(b"not a sqlite database")

            with self.assertRaisesRegex(ArchiveError, "Checkpoint database is corrupt"):
                MigrationArchive(root, create=False)

    def test_archive_rejects_unreferenced_entity_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            archive = MigrationArchive(root, create=True)
            archive.write_manifest("Release 10.1.18.GA FOSS", completed=True)
            orphan = root / "objects" / "unknown" / "orphan.json"
            orphan.parent.mkdir(parents=True)
            orphan.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ArchiveError, "unreferenced entity artifact"):
                verify_archive(archive, deep=False)

    def test_legacy_encrypted_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            MigrationArchive(root, create=True)
            (root / ".manifest.zmenc").write_bytes(b"legacy")
            with self.assertRaises(ArchiveError):
                MigrationArchive(root, create=False)

    def test_mailbox_archive_rejects_link_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mailbox.tgz"
            with tarfile.open(path, "w:gz") as archive:
                item = tarfile.TarInfo("Inbox/link")
                item.type = tarfile.SYMTYPE
                item.linkname = "../../outside"
                archive.addfile(item)

            with self.assertRaises(ArchiveError):
                validate_mailbox_archive(path, "tgz")

    def test_mailbox_zip_rejects_symlink_encrypted_and_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mailbox.zip"
            with zipfile.ZipFile(path, "w") as archive:
                info = zipfile.ZipInfo("Inbox/link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"target")
            with self.assertRaises(ArchiveError):
                validate_mailbox_archive(path, "zip")

            encrypted = Path(directory) / "encrypted.zip"
            with zipfile.ZipFile(encrypted, "w") as archive:
                archive.writestr("Inbox/message.eml", b"Subject: test\n\nbody\n")
            payload = bytearray(encrypted.read_bytes())
            local = payload.find(b"PK\x03\x04")
            central = payload.find(b"PK\x01\x02")
            payload[local + 6] |= 0x1
            payload[central + 8] |= 0x1
            encrypted.write_bytes(payload)
            with self.assertRaises(ArchiveError):
                validate_mailbox_archive(encrypted, "zip")

            duplicate = Path(directory) / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("Inbox/message.eml", b"one")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "a") as archive:
                    archive.writestr("Inbox/message.eml", b"two")
            with self.assertRaises(ArchiveError):
                validate_mailbox_archive(duplicate, "zip")

            valid = Path(directory) / "valid.zip"
            with zipfile.ZipFile(valid, "w") as archive:
                archive.writestr("Inbox/message.eml", b"Subject: test\n\nbody\n")
            self.assertGreater(validate_mailbox_archive(valid, "zip"), 0)
            with self.assertRaisesRegex(ArchiveError, "beyond its recorded size"):
                validate_mailbox_archive(valid, "zip", maximum_unpacked_size=1)


def _write_tgz(path: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        payload = b"Subject: test\n\nbody\n"
        item = tarfile.TarInfo("Inbox/message.eml")
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))


if __name__ == "__main__":
    unittest.main()
