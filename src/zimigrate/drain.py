"""Copy completed mailbox artifacts off a remote export host before they accumulate."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path, PurePosixPath

from zimigrate.errors import ZimigrateError
from zimigrate.interrupt import get_interrupt
from zimigrate.util import atomic_json, ensure_relative_path, read_json

LOGGER = logging.getLogger(__name__)
EXPORT_DRAIN_ENV = "ZIMIGRATE_EXPORT_DRAIN"
DRAIN_READY_RELATIVE = "reports/drain-ready"
MAILBOX_PREFIX = "mailboxes/"
WAIT_POLL_SECONDS = 0.5


def export_drain_enabled() -> bool:
    return os.environ.get(EXPORT_DRAIN_ENV) == "1"


def request_mailbox_drain(
    archive_root: Path,
    *,
    relative: str,
    sha256: str,
    size: int,
) -> None:
    """Ask the operator host to copy this artifact, then wait until it is deleted."""
    if not export_drain_enabled():
        return
    validate_mailbox_relative(relative)
    path = ensure_relative_path(archive_root, relative)
    if not path.is_file():
        raise ZimigrateError(f"Cannot drain a missing mailbox artifact: {relative}")
    ready_dir = ensure_relative_path(archive_root, DRAIN_READY_RELATIVE)
    ready_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = ready_dir / f"{uuid.uuid4().hex}.json"
    atomic_json(
        marker,
        {
            "path": relative,
            "sha256": sha256,
            "size": size,
        },
    )
    LOGGER.info(
        "Waiting until mailbox data is copied off this host",
        extra={"path": relative, "bytes": size},
    )
    wait_until_removed(path)


def wait_until_removed(path: Path) -> None:
    interrupt = get_interrupt()
    while path.is_file():
        interrupt.wait(WAIT_POLL_SECONDS)


def parse_drain_request(path: Path) -> dict[str, object]:
    value = read_json(path)
    relative = value.get("path")
    digest = value.get("sha256")
    size = value.get("size")
    if not isinstance(relative, str):
        raise ZimigrateError(f"Drain request path is invalid: {path}")
    validate_mailbox_relative(relative)
    if not isinstance(digest, str) or not digest:
        raise ZimigrateError(f"Drain request checksum is invalid: {path}")
    if type(size) is not int or size < 0:
        raise ZimigrateError(f"Drain request size is invalid: {path}")
    return {"path": relative, "sha256": digest, "size": size}


def validate_mailbox_relative(relative: str) -> None:
    posix = PurePosixPath(relative)
    if (
        not relative.startswith(MAILBOX_PREFIX)
        or posix.is_absolute()
        or ".." in posix.parts
        or posix.as_posix() != relative
    ):
        raise ZimigrateError(f"Mailbox path is not drainable: {relative}")


def mailbox_missing_after_drain(archive_root: Path, relative: str) -> bool:
    if not export_drain_enabled():
        return False
    return not ensure_relative_path(archive_root, relative).is_file()
