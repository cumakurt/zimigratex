from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from zimigrate.archive import MigrationArchive, validate_mailbox_archive
from zimigrate.config import ArchiveConfig
from zimigrate.errors import ArchiveError
from zimigrate.models import Artifact, EntityRecord
from zimigrate.util import sha256_file
from zimigrate.verifier import verify_archive


class ArchiveTests(unittest.TestCase):
    def test_entity_and_mailbox_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            archive = MigrationArchive(root, ArchiveConfig(), create=True)
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
            checksum, plaintext_checksum, _ = archive.store_mailbox(plaintext, mailbox_relative)
            self.assertEqual(checksum, plaintext_checksum)
            self.assertFalse(plaintext.exists())
            archive.validate_mailbox_artifact(
                mailbox_relative,
                checksum,
                deep=True,
                expected_plaintext_checksum=plaintext_checksum,
            )
            with archive.materialize_mailbox(mailbox_relative) as materialized:
                self.assertEqual(materialized, root / mailbox_relative)
                self.assertEqual(sha256_file(materialized), plaintext_checksum)
                validate_mailbox_archive(materialized, "tgz")

            with self.assertRaises(ArchiveError):
                archive.validate_mailbox_artifact(
                    mailbox_relative,
                    checksum,
                    deep=True,
                    expected_plaintext_checksum="0" * 64,
                )

            record.artifacts = [
                Artifact(
                    label="full",
                    path=mailbox_relative,
                    sha256=checksum,
                    plaintext_sha256=plaintext_checksum,
                    size=(root / mailbox_relative).stat().st_size,
                    query="is:anywhere",
                )
            ]
            archive.write_entity(record)
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

    def test_legacy_encrypted_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            MigrationArchive(root, ArchiveConfig(), create=True)
            (root / ".manifest.zmenc").write_bytes(b"legacy")
            with self.assertRaises(ArchiveError):
                MigrationArchive(root, ArchiveConfig(), create=False)

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


def _write_tgz(path: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        payload = b"Subject: test\n\nbody\n"
        item = tarfile.TarInfo("Inbox/message.eml")
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))


if __name__ == "__main__":
    unittest.main()
