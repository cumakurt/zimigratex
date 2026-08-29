from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import threading
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from zimigrate.errors import ArchiveError, ZimigrateError
from zimigrate.models import EntityRecord
from zimigrate.state import StateStore
from zimigrate.util import (
    HASH_IO_SIZE,
    FileLock,
    atomic_json,
    atomic_output,
    ensure_relative_path,
    place_file,
    read_json,
    safe_entity_filename,
    sha256_file,
    utc_now,
)

SCHEMA_VERSION = 1
ENCRYPTED_ARCHIVE_MARKERS = (".manifest.zmenc", "salt.bin", ".keycheck")


class MigrationArchive:
    def __init__(
        self,
        root: Path,
        *,
        create: bool,
    ) -> None:
        self.root = root.resolve()
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArchiveError(f"Archive directory does not exist: {self.root}")
        os.chmod(self.root, 0o700)
        _reject_encrypted_archive(self.root)
        manifest_path = ensure_relative_path(self.root, "manifest.json")
        if not manifest_path.exists() and any(
            (self.root / name).exists() for name in ("objects", "mailboxes")
        ):
            raise ArchiveError(
                "Archive data exists without a manifest; use a clean archive directory"
            )
        if manifest_path.exists() and self.manifest().get("encrypted"):
            raise ArchiveError("Encrypted archives are not supported")
        state_path = ensure_relative_path(self.root, "state.sqlite3")
        if (not create or manifest_path.exists()) and not state_path.is_file():
            raise ArchiveError("Archive checkpoint database is missing: state.sqlite3")
        try:
            self.state = StateStore(state_path)
        except ZimigrateError as exc:
            raise ArchiveError(str(exc)) from exc
        self._mailbox_validation_cache: dict[
            str,
            tuple[str, int, tuple[int, int, int, int, int], int | None],
        ] = {}
        self._mailbox_validation_lock = threading.Lock()

    def lock(self) -> FileLock:
        return FileLock(self.root / ".lock")

    def close_state(self) -> None:
        self.state.close()

    def reopen_state(self) -> None:
        self.state = StateStore(ensure_relative_path(self.root, "state.sqlite3"))

    def entity_relative_path(self, kind: str, name: str) -> str:
        return f"objects/{kind}/{safe_entity_filename(name)}.json"

    def write_entity(self, record: EntityRecord) -> tuple[str, str]:
        relative = self.entity_relative_path(record.kind, record.name)
        path = ensure_relative_path(self.root, relative)
        payload = json.dumps(
            record.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with atomic_output(path, "wb", durable=False) as stream:
            stream.write(payload)
        return relative, hashlib.sha256(payload).hexdigest()

    def read_entity(self, kind: str, name: str) -> EntityRecord:
        return self._read_entity_path(self.entity_relative_path(kind, name))

    def validate_entity_artifact(
        self,
        kind: str,
        name: str,
        relative: str,
        expected_checksum: str,
    ) -> EntityRecord:
        expected_relative = self.entity_relative_path(kind, name)
        if relative != expected_relative:
            raise ArchiveError(f"Entity checkpoint path does not match {kind} {name}")
        path = ensure_relative_path(self.root, relative)
        if not path.is_file():
            raise ArchiveError(f"Entity artifact is missing: {relative}")
        if sha256_file(path) != expected_checksum:
            raise ArchiveError(f"Entity artifact checksum mismatch: {relative}")
        return self._read_entity_path(relative)

    def iter_entities(self, kind: str) -> Iterator[EntityRecord]:
        directory = ensure_relative_path(self.root, f"objects/{kind}")
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
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
        return f"mailboxes/{account_dir}/{label_name}.{archive_format}"

    def store_mailbox(self, plaintext: Path, relative: str) -> tuple[str, int]:
        destination = ensure_relative_path(self.root, relative)
        digest, size = place_file(plaintext, destination)
        return digest, size

    def validate_mailbox_artifact(
        self,
        relative: str,
        expected_checksum: str,
        *,
        deep: bool,
        archive_format: str = "tgz",
        expected_size: int | None = None,
        expected_unpacked_size: int | None = None,
    ) -> None:
        path = ensure_relative_path(self.root, relative)
        if not path.is_file():
            raise ArchiveError(f"Mailbox artifact is missing: {relative}")
        before = path.stat()
        if expected_size is not None and before.st_size != expected_size:
            raise ArchiveError(f"Mailbox artifact size mismatch: {relative}")
        identity = _file_identity(before)
        with self._mailbox_validation_lock:
            cached = self._mailbox_validation_cache.get(relative)
        cache_matches = bool(
            cached
            and cached[0] == expected_checksum
            and cached[1] == before.st_size
            and cached[2] == identity
        )
        if cache_matches and (not deep or cached[3] is not None):
            if deep and expected_unpacked_size is not None and cached[3] != expected_unpacked_size:
                raise ArchiveError(f"Unpacked mailbox size mismatch: {relative}")
            return
        if not cache_matches and sha256_file(path) != expected_checksum:
            raise ArchiveError(f"Mailbox artifact checksum mismatch: {relative}")
        unpacked_size: int | None = None
        if deep:
            unpacked_size = validate_mailbox_archive(
                path,
                archive_format,
                maximum_unpacked_size=expected_unpacked_size,
            )
            if expected_unpacked_size is not None and unpacked_size != expected_unpacked_size:
                raise ArchiveError(f"Unpacked mailbox size mismatch: {relative}")
        after = path.stat()
        if _file_identity(after) != identity:
            raise ArchiveError(f"Mailbox artifact changed during validation: {relative}")
        with self._mailbox_validation_lock:
            self._mailbox_validation_cache[relative] = (
                expected_checksum,
                after.st_size,
                identity,
                unpacked_size,
            )

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
                        1 for path in directory.glob("*.json") if path.is_file()
                    )
        current = self.manifest(optional=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "archive_id": current.get("archive_id", str(uuid.uuid4())),
            "created_at": current.get("created_at", utc_now()),
            "updated_at": utc_now(),
            "completed": completed,
            "encrypted": False,
            "source_version": source_version,
            "source_host": source_host or current.get("source_host"),
            "export_options": export_options or current.get("export_options", {}),
            "counts": counts,
        }
        atomic_json(ensure_relative_path(self.root, "manifest.json"), manifest)

    def manifest(self, *, optional: bool = False) -> dict[str, Any]:
        path = ensure_relative_path(self.root, "manifest.json")
        if optional and not path.exists():
            return {}
        value = read_json(path)
        if (
            type(value.get("schema_version")) is not int
            or value.get("schema_version") != SCHEMA_VERSION
        ):
            raise ArchiveError("Archive manifest schema is not supported")
        return value


def validate_mailbox_archive(
    path: Path,
    archive_format: str,
    *,
    maximum_unpacked_size: int | None = None,
) -> int:
    if archive_format == "zip":
        return _validate_zip(path, maximum_unpacked_size)
    if archive_format == "tgz":
        return _validate_tgz(path, maximum_unpacked_size)
    raise ArchiveError(f"Unsupported mailbox archive format: {archive_format}")


def _validate_tgz(path: Path, maximum_unpacked_size: int | None) -> int:
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
                _check_unpacked_size(path, unpacked_size, maximum_unpacked_size)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError(f"Mailbox TGZ member cannot be read: {member.name}")
                with stream:
                    while stream.read(HASH_IO_SIZE):
                        continue
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"Mailbox export is not a valid TGZ archive: {path.name}") from exc
    return unpacked_size


def _validate_zip(path: Path, maximum_unpacked_size: int | None) -> int:
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
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise ArchiveError(
                        f"Mailbox ZIP contains an unsupported symlink: {member.filename}"
                    )
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ArchiveError(
                        f"Mailbox ZIP contains an unsupported entry: {member.filename}"
                    )
                if member.flag_bits & 0x1:
                    raise ArchiveError(
                        f"Mailbox ZIP contains an encrypted member: {member.filename}"
                    )
                if not member.is_dir():
                    unpacked_size += member.file_size
                    _check_unpacked_size(path, unpacked_size, maximum_unpacked_size)
            if corrupt := archive.testzip():
                raise ArchiveError(f"Mailbox ZIP contains a corrupt member: {corrupt}")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"Mailbox export is not a valid ZIP archive: {path.name}") from exc
    return unpacked_size


def _check_unpacked_size(
    path: Path,
    unpacked_size: int,
    maximum_unpacked_size: int | None,
) -> None:
    if maximum_unpacked_size is not None and unpacked_size > maximum_unpacked_size:
        raise ArchiveError(f"Mailbox archive expands beyond its recorded size: {path.name}")


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_member_path(name: str, archive_label: str) -> None:
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise ArchiveError(f"Mailbox {archive_label} contains an unsafe path: {name}")


def _reject_encrypted_archive(root: Path) -> None:
    for name in ENCRYPTED_ARCHIVE_MARKERS:
        if (root / name).exists():
            raise ArchiveError("Encrypted archives are not supported")
    objects = root / "objects"
    if objects.is_dir():
        for directory in objects.iterdir():
            if directory.is_dir() and next(directory.glob("*.zmenc"), None) is not None:
                raise ArchiveError("Encrypted archives are not supported")
    mailboxes = root / "mailboxes"
    if mailboxes.is_dir() and next(mailboxes.glob("*/*.zmenc"), None) is not None:
        raise ArchiveError("Encrypted archives are not supported")
