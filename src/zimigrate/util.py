from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from zimigrate.errors import ArchiveError

HASH_IO_SIZE = 8 * 1024 * 1024
DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(HASH_IO_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def safe_entity_filename(name: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:72] or "entity"
    suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"{readable}-{suffix}"


def is_valid_dns_name(value: str) -> bool:
    """Return whether value is an ASCII DNS name accepted by Zimbra host fields."""
    return bool(
        value
        and len(value) <= 253
        and not value.endswith(".")
        and all(DNS_LABEL.fullmatch(label) for label in value.split("."))
    )


def ensure_relative_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ArchiveError(f"Archive path escapes its root: {relative}")
    return candidate


@contextlib.contextmanager
def atomic_output(path: Path, mode: str = "wb", *, durable: bool = True) -> Iterator[BinaryIO]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    os.chmod(temporary_path, 0o600)
    try:
        with os.fdopen(descriptor, mode) as stream:
            yield stream
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if durable:
            _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def place_file(source: Path, destination: Path) -> tuple[str, int]:
    """Install source at destination. Same-filesystem sources are renamed after hashing."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = sha256_file(source)
    os.chmod(source, 0o600)
    _fsync_file(source)
    if _same_filesystem(source, destination.parent):
        os.replace(source, destination)
        _fsync_directory(destination.parent)
        return digest, destination.stat().st_size
    with source.open("rb") as input_stream, atomic_output(destination, "wb") as output:
        shutil.copyfileobj(input_stream, output, HASH_IO_SIZE)
    return digest, destination.stat().st_size


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    with atomic_output(path, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"Expected a JSON object in {path}")
    return value


def open_private_temporary(directory: Path, suffix: str = "") -> tuple[int, Path]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".zimigrate-", suffix=suffix, dir=directory)
    os.chmod(name, 0o600)
    return descriptor, Path(name)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _same_filesystem(source: Path, directory: Path) -> bool:
    try:
        return source.stat().st_dev == directory.stat().st_dev
    except OSError:
        return False


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileLock:
    """A process lock preventing concurrent mutation of one archive."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: BinaryIO | None = None

    def __enter__(self) -> FileLock:
        import fcntl

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ArchiveError(f"Archive lock path cannot be a symlink: {self.path}")
        self._stream = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.close()
            self._stream = None
            raise ArchiveError(f"Archive is already in use: {self.path.parent}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
