"""Copy zimigrate to a Zimbra host, run export there, and keep the archive locally."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path, PurePosixPath

from zimigrate.archive import SCHEMA_VERSION, MigrationArchive
from zimigrate.config import TransferConfig
from zimigrate.drain import DRAIN_READY_RELATIVE, EXPORT_DRAIN_ENV, parse_drain_request
from zimigrate.errors import ConfigurationError, Interrupted, ZimigrateError
from zimigrate.interrupt import get_interrupt, stop_process
from zimigrate.ssh import SshSession, connect_ssh
from zimigrate.util import atomic_json, ensure_relative_path, is_valid_ssh_target, read_json

LOGGER = logging.getLogger(__name__)
REMOTE_META_RELATIVE = "reports/remote-export.json"
REMOTE_TRANSFER_RELATIVE = "reports/remote-transfer.toml"
# UUID-suffixed work dir on the Zimbra host. Created over SSH as the login user;
# not a local shared tempfile.
REMOTE_BASE = "/var/tmp/zimigratex"  # nosec B108
PUSH_EXCLUDES = ("mailboxes", ".tmp")
DRAIN_POLL_SECONDS = 0.25
TOOLKIT_EXCLUDES = (
    ".git",
    ".venv",
    "__pycache__",
    "*.pyc",
    "tests",
    "tasks",
    ".cursor",
    "docs",
    "export_data",
    "*.tar",
    ".pytest_cache",
    "canvases",
)


def remote_export_meta(archive_root: Path) -> dict[str, object] | None:
    path = archive_root / REMOTE_META_RELATIVE
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except ZimigrateError:
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


def run_remote_export(
    archive: MigrationArchive,
    transfer: TransferConfig,
    *,
    host: str,
    ssh_user: str,
    verbose: bool = False,
    json_logs: bool = False,
) -> dict[str, object]:
    if not is_valid_ssh_target(host):
        raise ConfigurationError(f"Invalid --target-ip value: {host}")
    tool_root = find_tool_root()
    archive_id = _archive_id(archive)
    remote_root = f"{REMOTE_BASE}/{archive_id}"
    remote_app = f"{remote_root}/app"
    remote_archive = f"{remote_root}/archive"
    _write_transfer_toml(archive.root / REMOTE_TRANSFER_RELATIVE, transfer)
    _write_remote_meta(
        archive.root,
        host=host,
        ssh_user=ssh_user,
        archive_id=archive_id,
        remote_root=remote_root,
        auth="unknown",
    )
    session = connect_ssh(host, user=ssh_user)
    state_detached = False
    export_process = None
    try:
        _write_remote_meta(
            archive.root,
            host=host,
            ssh_user=session.user,
            archive_id=archive_id,
            remote_root=remote_root,
            auth=session.auth_method,
        )
        LOGGER.info(
            "Starting remote export; mailbox data is copied here as soon as each artifact is ready",
            extra={"host": host, "archive": str(archive.root), "auth": session.auth_method},
        )
        archive.close_state()
        state_detached = True
        session.run(
            [
                "mkdir",
                "-p",
                remote_app,
                remote_archive,
                f"{remote_archive}/{DRAIN_READY_RELATIVE}",
                f"{remote_archive}/mailboxes",
            ]
        )
        LOGGER.info("Copying zimigrate to the remote host")
        session.rsync_to_remote(
            tool_root,
            remote_app,
            delete=True,
            excludes=TOOLKIT_EXCLUDES,
        )
        LOGGER.info("Retrieving leftover archive data from the remote host")
        session.rsync_from_remote(remote_archive, archive.root, delete=False)
        session.run(["rm", "-rf", f"{remote_archive}/mailboxes", f"{remote_archive}/.tmp"])
        _write_transfer_toml(archive.root / REMOTE_TRANSFER_RELATIVE, transfer)
        LOGGER.info("Copying local checkpoints to the remote host")
        session.rsync_to_remote(
            archive.root,
            remote_archive,
            delete=False,
            excludes=PUSH_EXCLUDES,
        )
        remote_command = [
            "env",
            f"{EXPORT_DRAIN_ENV}=1",
            f"{remote_app}/export.sh",
            "--archive",
            remote_archive,
            "--config",
            f"{remote_archive}/{REMOTE_TRANSFER_RELATIVE}",
        ]
        if verbose:
            remote_command.append("--verbose")
        if json_logs:
            remote_command.append("--json-logs")
        LOGGER.info("Running export on the remote Zimbra server")
        export_process = session.start(remote_command, tty=True)
        try:
            while export_process.poll() is None:
                drain_ready_mailboxes(session, remote_archive, archive.root)
                get_interrupt().wait(DRAIN_POLL_SECONDS)
            drain_ready_mailboxes(session, remote_archive, archive.root)
        finally:
            if export_process.poll() is None:
                stop_process(export_process)
            get_interrupt().unregister(export_process)
            LOGGER.info("Copying remaining archive metadata to this machine")
            _pull_remaining_archive(session, remote_archive, archive.root)
        if export_process.returncode != 0:
            if get_interrupt().is_set():
                raise Interrupted("Interrupted by user")
            raise ZimigrateError(
                f"Remote export failed on {host} (exit {export_process.returncode})"
            )
        archive.reopen_state()
        state_detached = False
        completed = _export_completed(archive)
        if completed:
            LOGGER.info("Remote export completed; removing the remote working copy")
            session.run(["rm", "-rf", remote_root])
        return {
            "archive": str(archive.root),
            "host": host,
            "completed": completed,
        }
    finally:
        if state_detached:
            archive.reopen_state()
        session.close()


def drain_ready_mailboxes(session: SshSession, remote_archive: str, local_root: Path) -> int:
    """Copy completed mailbox artifacts locally and delete them on the Zimbra host."""
    ready_remote = join_remote_path(remote_archive, DRAIN_READY_RELATIVE)
    ready_local = local_root / DRAIN_READY_RELATIVE
    session.rsync_from_remote(ready_remote, ready_local, delete=False)
    pulled = 0
    for marker in sorted(ready_local.glob("*.json")):
        try:
            request = parse_drain_request(marker)
        except ZimigrateError:
            LOGGER.warning("Ignoring invalid drain request", extra={"marker": str(marker)})
            continue
        relative = str(request["path"])
        size = int(request["size"])
        local_file = ensure_relative_path(local_root, relative)
        remote_file = join_remote_path(remote_archive, relative)
        remote_marker = join_remote_path(remote_archive, f"{DRAIN_READY_RELATIVE}/{marker.name}")
        try:
            session.rsync_file_from_remote(remote_file, local_file)
        except ZimigrateError:
            LOGGER.warning(
                "Mailbox pull failed; will retry",
                extra={"path": relative},
            )
            continue
        if not local_file.is_file() or local_file.stat().st_size != size:
            LOGGER.warning(
                "Pulled mailbox size mismatch; will retry",
                extra={"path": relative, "expected": size},
            )
            continue
        session.run(["rm", "-f", remote_file, remote_marker])
        marker.unlink(missing_ok=True)
        pulled += 1
        LOGGER.info(
            "Copied mailbox data off the remote host",
            extra={"path": relative, "bytes": size},
        )
    return pulled


def join_remote_path(root: str, relative: str) -> str:
    if not root.startswith("/") or "\x00" in root:
        raise ZimigrateError("Remote path must be an absolute POSIX path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or not relative or "\x00" in relative:
        raise ZimigrateError("Invalid remote relative path")
    return f"{root.rstrip('/')}/{posix.as_posix()}"


def _pull_remaining_archive(session: SshSession, remote_archive: str, local_root: Path) -> None:
    drain_ready_mailboxes(session, remote_archive, local_root)
    session.rsync_from_remote(
        remote_archive,
        local_root,
        delete=False,
        excludes=PUSH_EXCLUDES,
    )
    mailbox_remote = join_remote_path(remote_archive, "mailboxes")
    mailbox_local = local_root / "mailboxes"
    session.run(["mkdir", "-p", mailbox_remote])
    session.rsync_from_remote(mailbox_remote, mailbox_local, delete=False)
    session.run(["rm", "-rf", mailbox_remote, join_remote_path(remote_archive, ".tmp")])


def find_tool_root() -> Path:
    if env := os.environ.get("ZIMIGRATE_ROOT"):
        candidate = Path(env).resolve()
        if _is_tool_root(candidate):
            return candidate
        raise ZimigrateError(f"ZIMIGRATE_ROOT is not a zimigrate repository: {candidate}")
    here = Path(__file__).resolve()
    for candidate in (here.parents[2], Path.cwd()):
        if _is_tool_root(candidate):
            return candidate
    raise ZimigrateError(
        "Cannot locate the zimigrate repository to copy to the remote host; "
        "run export.sh from the repository"
    )


def _is_tool_root(path: Path) -> bool:
    return (path / "export.sh").is_file() and (path / "pyproject.toml").is_file()


def _archive_id(archive: MigrationArchive) -> str:
    stored = remote_export_meta(archive.root)
    if stored and isinstance(stored.get("archive_id"), str) and stored["archive_id"]:
        return str(stored["archive_id"])
    manifest = archive.manifest(optional=True)
    existing = manifest.get("archive_id")
    if isinstance(existing, str) and existing:
        return existing
    return str(uuid.uuid4())


def _export_completed(archive: MigrationArchive) -> bool:
    return bool(archive.manifest(optional=True).get("completed"))


def _write_remote_meta(
    archive_root: Path,
    *,
    host: str,
    ssh_user: str,
    archive_id: str,
    remote_root: str,
    auth: str,
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
            "remote_root": remote_root,
            "auth": auth,
        },
    )


def _write_transfer_toml(path: Path, transfer: TransferConfig) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = [
        "[transfer]",
        f"workers = {transfer.workers}",
        f"retries = {transfer.retries}",
        f"retry_base_seconds = {transfer.retry_base_seconds}",
        f"include_domains = {_toml_bool(transfer.include_domains)}",
        f"include_cos = {_toml_bool(transfer.include_cos)}",
        f"include_accounts = {_toml_bool(transfer.include_accounts)}",
        f"include_mailboxes = {_toml_bool(transfer.include_mailboxes)}",
        f"include_distribution_lists = {_toml_bool(transfer.include_distribution_lists)}",
        f"include_system_mailboxes = {_toml_bool(transfer.include_system_mailboxes)}",
        f"include_secrets = {_toml_bool(transfer.include_secrets)}",
        f"account_include = {_toml_string_array(transfer.account_include)}",
        f"account_exclude = {_toml_string_array(transfer.account_exclude)}",
        f"target_users = {_toml_string_array(transfer.target_users)}",
        f"target_domains = {_toml_string_array(transfer.target_domains)}",
        f"mailbox_mode = {_toml_string(transfer.mailbox_mode)}",
        f"mailbox_format = {_toml_string(transfer.mailbox_format)}",
        f"mailbox_lock = {_toml_bool(transfer.mailbox_lock)}",
        f"mailbox_start_year = {transfer.mailbox_start_year}",
        f"mailbox_chunk_years = {transfer.mailbox_chunk_years}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(item) for item in values) + "]"
