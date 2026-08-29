from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from zimigrate.config import ArchiveConfig
from zimigrate.crypto import KEYCHECK_AAD, KEYCHECK_VALUE, CryptoBox
from zimigrate.errors import ArchiveError, ConfigurationError
from zimigrate.models import EntityRecord
from zimigrate.state import StateStore
from zimigrate.util import (
    FileLock,
    atomic_json,
    atomic_output,
    ensure_relative_path,
    open_private_temporary,
    read_json,
    safe_entity_filename,
    sha256_file,
    utc_now,
)

SCHEMA_VERSION = 1
MANIFEST_AUTH_NAME = ".manifest.zmenc"
MANIFEST_AAD = b"zimigrate-manifest-v1"


class MigrationArchive:
    def __init__(
        self,
        root: Path,
        config: ArchiveConfig,
        *,
        create: bool,
        passphrase: str | None = None,
    ) -> None:
        self.root = root.resolve()
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArchiveError(f"Archive directory does not exist: {self.root}")
        os.chmod(self.root, 0o700)
        self.config = config
        self._allow_legacy_manifest = create
        self.crypto = self._initialize_crypto(create=create, passphrase=passphrase)
        manifest_path = ensure_relative_path(self.root, "manifest.json")
        authenticated_manifest_path = ensure_relative_path(self.root, MANIFEST_AUTH_NAME)
        if (
            not manifest_path.exists()
            and not authenticated_manifest_path.exists()
            and any((self.root / name).exists() for name in ("objects", "mailboxes"))
        ):
            raise ArchiveError(
                "Archive data exists without a manifest; use a clean archive directory"
            )
        if manifest_path.exists() or authenticated_manifest_path.exists():
            encrypted = bool(self.manifest().get("encrypted"))
            if encrypted != (self.crypto is not None):
                raise ArchiveError(
                    "Archive encryption setting does not match the existing manifest"
                )
        self.state = StateStore(ensure_relative_path(self.root, "state.sqlite3"))

    def lock(self) -> FileLock:
        return FileLock(self.root / ".lock")

    def _initialize_crypto(self, *, create: bool, passphrase: str | None) -> CryptoBox | None:
        if not self.config.encryption_enabled:
            if (self.root / "salt.bin").exists() or (self.root / ".keycheck").exists():
                raise ArchiveError(
                    "Encrypted archive key material exists but encryption is disabled"
                )
            return None
        passphrase = passphrase if passphrase is not None else self.config.passphrase()
        if passphrase is None:
            raise ConfigurationError(
                f"Archive passphrase is missing from environment variable "
                f"{self.config.passphrase_env}"
            )
        salt_path = ensure_relative_path(self.root, "salt.bin")
        if not salt_path.exists():
            if not create:
                raise ArchiveError("Encrypted archive has no salt.bin file")
            with atomic_output(salt_path, "wb") as stream:
                stream.write(os.urandom(16))
        salt = salt_path.read_bytes()
        if len(salt) != 16:
            raise ArchiveError("Archive salt.bin is invalid")
        crypto = CryptoBox.derive(passphrase, salt)
        keycheck_path = ensure_relative_path(self.root, ".keycheck")
        if keycheck_path.exists():
            value = crypto.decrypt_bytes(keycheck_path.read_bytes(), KEYCHECK_AAD)
            if value != KEYCHECK_VALUE:
                raise ArchiveError("Archive key check failed")
        elif create:
            with atomic_output(keycheck_path, "wb") as stream:
                stream.write(crypto.encrypt_bytes(KEYCHECK_VALUE, KEYCHECK_AAD))
        else:
            raise ArchiveError("Encrypted archive has no key check record")
        return crypto

    def entity_relative_path(self, kind: str, name: str) -> str:
        suffix = ".json.zmenc" if self.crypto else ".json"
        return f"objects/{kind}/{safe_entity_filename(name)}{suffix}"

    def write_entity(self, record: EntityRecord) -> tuple[str, str]:
        relative = self.entity_relative_path(record.kind, record.name)
        path = ensure_relative_path(self.root, relative)
        payload = json.dumps(
            record.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if self.crypto:
            payload = self.crypto.encrypt_bytes(payload, relative.encode("utf-8"))
        with atomic_output(path, "wb") as stream:
            stream.write(payload)
        return relative, sha256_file(path)

    def read_entity(self, kind: str, name: str) -> EntityRecord:
        return self._read_entity_path(self.entity_relative_path(kind, name))

    def iter_entities(self, kind: str) -> Iterator[EntityRecord]:
        directory = ensure_relative_path(self.root, f"objects/{kind}")
        if not directory.exists():
            return
        pattern = "*.json.zmenc" if self.crypto else "*.json"
        for path in sorted(directory.glob(pattern)):
            relative = path.relative_to(self.root).as_posix()
            record = self._read_entity_path(relative)
            if record.kind != kind:
                raise ArchiveError(f"Archive record kind does not match its directory: {relative}")
            yield record

    def _read_entity_path(self, relative: str) -> EntityRecord:
        path = ensure_relative_path(self.root, relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ArchiveError(f"Cannot read archive record {relative}: {exc}") from exc
        if self.crypto:
            payload = self.crypto.decrypt_bytes(payload, relative.encode("utf-8"))
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"Archive record is invalid JSON: {relative}") from exc
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != SCHEMA_VERSION
        ):
            raise ArchiveError(f"Archive record has an unsupported schema: {relative}")
        try:
            record = EntityRecord.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError(f"Archive record has an invalid structure: {relative}") from exc
        expected_relative = self.entity_relative_path(record.kind, record.name)
        if relative != expected_relative:
            raise ArchiveError(f"Archive record path does not match its identity: {relative}")
        return record

    def mailbox_relative_path(self, account: str, label: str, archive_format: str = "tgz") -> str:
        account_dir = safe_entity_filename(account)
        label_name = safe_entity_filename(label)
        suffix = f".{archive_format}.zmenc" if self.crypto else f".{archive_format}"
        return f"mailboxes/{account_dir}/{label_name}{suffix}"

    def store_mailbox(self, plaintext: Path, relative: str) -> tuple[str, str, int]:
        destination = ensure_relative_path(self.root, relative)
        if self.crypto:
            plaintext_checksum = self.crypto.encrypt_file(
                plaintext, destination, relative.encode("utf-8")
            )
        else:
            digest = hashlib.sha256()
            with plaintext.open("rb") as input_stream, atomic_output(destination, "wb") as output:
                for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
            plaintext_checksum = digest.hexdigest()
        return sha256_file(destination), plaintext_checksum, destination.stat().st_size

    def materialize_mailbox(self, relative: str) -> Iterator[Path]:
        return _MaterializedMailbox(self, relative)

    def validate_mailbox_artifact(
        self,
        relative: str,
        expected_checksum: str,
        *,
        deep: bool,
        archive_format: str = "tgz",
        expected_plaintext_checksum: str | None = None,
        expected_unpacked_size: int | None = None,
    ) -> None:
        path = ensure_relative_path(self.root, relative)
        if not path.is_file():
            raise ArchiveError(f"Mailbox artifact is missing: {relative}")
        actual = sha256_file(path)
        if actual != expected_checksum:
            raise ArchiveError(f"Mailbox artifact checksum mismatch: {relative}")
        if deep:
            with self.materialize_mailbox(relative) as plaintext:
                if (
                    expected_plaintext_checksum is not None
                    and sha256_file(plaintext) != expected_plaintext_checksum
                ):
                    raise ArchiveError(f"Plaintext mailbox checksum mismatch: {relative}")
                unpacked_size = validate_mailbox_archive(plaintext, archive_format)
                if expected_unpacked_size is not None and unpacked_size != expected_unpacked_size:
                    raise ArchiveError(f"Unpacked mailbox size mismatch: {relative}")

    def write_manifest(
        self,
        source_version: str,
        *,
        completed: bool,
        source_host: str | None = None,
        export_options: dict[str, Any] | None = None,
    ) -> None:
        counts: dict[str, int] = {}
        objects = self.root / "objects"
        if objects.exists():
            for directory in objects.iterdir():
                if directory.is_dir():
                    counts[directory.name] = sum(
                        1 for path in directory.iterdir() if path.is_file()
                    )
        current = self.manifest(optional=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "archive_id": current.get("archive_id", str(uuid.uuid4())),
            "created_at": current.get("created_at", utc_now()),
            "updated_at": utc_now(),
            "completed": completed,
            "encrypted": self.crypto is not None,
            "source_version": source_version,
            "source_host": source_host or current.get("source_host"),
            "export_options": export_options or current.get("export_options", {}),
            "counts": counts,
        }
        if self.crypto:
            payload = _canonical_json(manifest)
            with atomic_output(ensure_relative_path(self.root, MANIFEST_AUTH_NAME), "wb") as stream:
                stream.write(self.crypto.encrypt_bytes(payload, MANIFEST_AAD))
        atomic_json(ensure_relative_path(self.root, "manifest.json"), manifest)

    def manifest(self, *, optional: bool = False) -> dict[str, Any]:
        path = ensure_relative_path(self.root, "manifest.json")
        authenticated_path = ensure_relative_path(self.root, MANIFEST_AUTH_NAME)
        if optional and not path.exists() and not authenticated_path.exists():
            return {}
        if self.crypto:
            if authenticated_path.exists():
                try:
                    payload = self.crypto.decrypt_bytes(
                        authenticated_path.read_bytes(), MANIFEST_AAD
                    )
                    value = json.loads(payload)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ArchiveError("Authenticated archive manifest is invalid") from exc
                if not isinstance(value, dict):
                    raise ArchiveError("Authenticated archive manifest is not an object")
            elif self._allow_legacy_manifest and path.exists():
                value = read_json(path)
            else:
                raise ArchiveError(
                    "Encrypted archive has no authenticated manifest; resume export once "
                    "with the current zimigrate version"
                )
        else:
            value = read_json(path)
        if (
            type(value.get("schema_version")) is not int
            or value.get("schema_version") != SCHEMA_VERSION
        ):
            raise ArchiveError("Archive manifest schema is not supported")
        return value


class _MaterializedMailbox:
    def __init__(self, archive: MigrationArchive, relative: str) -> None:
        self.archive = archive
        self.relative = relative
        self.path: Path | None = None

    def __enter__(self) -> Path:
        source = ensure_relative_path(self.archive.root, self.relative)
        suffix = ".zip" if ".zip" in Path(self.relative).suffixes else ".tgz"
        temporary_root = ensure_relative_path(self.archive.root, ".tmp")
        descriptor, self.path = open_private_temporary(temporary_root, suffix)
        os.close(descriptor)
        try:
            if self.archive.crypto:
                self.archive.crypto.decrypt_file(source, self.path, self.relative.encode("utf-8"))
            else:
                with source.open("rb") as input_stream, self.path.open("wb") as output:
                    for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                        output.write(block)
        except BaseException:
            self.path.unlink(missing_ok=True)
            self.path = None
            raise
        return self.path

    def __exit__(self, *_: object) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None


def validate_mailbox_archive(path: Path, archive_format: str) -> int:
    if archive_format == "zip":
        return _validate_zip(path)
    if archive_format == "tgz":
        return _validate_tgz(path)
    raise ArchiveError(f"Unsupported mailbox archive format: {archive_format}")


def _validate_tgz(path: Path) -> int:
    unpacked_size = 0
    names: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                _validate_member_path(member.name, "TGZ")
                if member.name in names:
                    raise ArchiveError(f"Mailbox TGZ contains a duplicate entry: {member.name}")
                names.add(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArchiveError(f"Mailbox TGZ contains an unsupported entry: {member.name}")
                unpacked_size += member.size
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError(f"Mailbox TGZ member cannot be read: {member.name}")
                with stream:
                    while stream.read(1024 * 1024):
                        continue
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"Mailbox export is not a valid TGZ archive: {path.name}") from exc
    return unpacked_size


def _validate_zip(path: Path) -> int:
    unpacked_size = 0
    names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                _validate_member_path(member.filename, "ZIP")
                if member.filename in names:
                    raise ArchiveError(f"Mailbox ZIP contains a duplicate entry: {member.filename}")
                names.add(member.filename)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise ArchiveError(
                        f"Mailbox ZIP contains an unsupported symlink: {member.filename}"
                    )
                if member.flag_bits & 0x1:
                    raise ArchiveError(
                        f"Mailbox ZIP contains an encrypted member: {member.filename}"
                    )
                if not member.is_dir():
                    unpacked_size += member.file_size
            if corrupt := archive.testzip():
                raise ArchiveError(f"Mailbox ZIP contains a corrupt member: {corrupt}")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"Mailbox export is not a valid ZIP archive: {path.name}") from exc
    return unpacked_size


def _validate_member_path(name: str, archive_label: str) -> None:
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise ArchiveError(f"Mailbox {archive_label} contains an unsafe path: {name}")


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
