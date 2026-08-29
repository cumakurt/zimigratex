"""Bind an export archive to a remote Zimbra source reached over SSH."""

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath

from zimigrate.archive import SCHEMA_VERSION, MigrationArchive
from zimigrate.errors import ConfigurationError, ZimigrateError
from zimigrate.selection import normalize_categories
from zimigrate.util import atomic_json, is_valid_ssh_target, read_json

REMOTE_META_RELATIVE = "reports/remote-export.json"


def remote_export_meta(archive_root: Path) -> dict[str, object] | None:
    path = archive_root / REMOTE_META_RELATIVE
    try:
        if not path.is_file():
            return None
        return read_json(path)
    except (OSError, TypeError, ZimigrateError):
        return None


def resolve_remote_host(archive_root: Path, target_ip: str | None) -> str | None:
    stored = remote_export_meta(archive_root)
    stored_host = stored.get("target_ip") if stored else None
    if isinstance(stored_host, str) and stored_host:
        if target_ip and target_ip != stored_host:
            raise ConfigurationError(
                f"Archive is bound to remote host {stored_host}, not {target_ip}"
            )
        return stored_host
    return target_ip


def stored_export_categories(archive_root: Path) -> set[str] | None:
    stored = remote_export_meta(archive_root)
    raw = stored.get("categories") if stored else None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return normalize_categories({str(item) for item in raw})
    except ConfigurationError:
        return None


def bind_remote_export(
    archive: MigrationArchive,
    *,
    host: str,
    ssh_user: str,
    auth: str,
    categories: tuple[str, ...],
) -> None:
    if not is_valid_ssh_target(host):
        raise ConfigurationError(f"Invalid --target-ip value: {host}")
    write_remote_meta(
        archive.root,
        host=host,
        ssh_user=ssh_user,
        archive_id=_archive_id(archive),
        auth=auth,
        categories=categories,
    )


def write_remote_meta(
    archive_root: Path,
    *,
    host: str,
    ssh_user: str,
    archive_id: str,
    auth: str,
    categories: tuple[str, ...] = (),
) -> None:
    path = archive_root / REMOTE_META_RELATIVE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "target_ip": host,
            "ssh_user": ssh_user,
            "archive_id": archive_id,
            "auth": auth,
            "categories": list(categories),
        },
    )


def join_remote_path(root: str, relative: str) -> str:
    if not root.startswith("/") or "\x00" in root:
        raise ZimigrateError("Remote path must be an absolute POSIX path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or not relative or "\x00" in relative:
        raise ZimigrateError("Invalid remote relative path")
    return f"{root.rstrip('/')}/{posix.as_posix()}"


def _archive_id(archive: MigrationArchive) -> str:
    stored = remote_export_meta(archive.root)
    if stored and isinstance(stored.get("archive_id"), str) and stored["archive_id"]:
        return str(stored["archive_id"])
    manifest = archive.manifest(optional=True)
    existing = manifest.get("archive_id")
    if isinstance(existing, str) and existing:
        return existing
    return str(uuid.uuid4())
