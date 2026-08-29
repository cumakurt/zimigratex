from __future__ import annotations

import math
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

MIB = 1024**2
GIB = 1024**3
MAILBOX_ARCHIVE_FACTOR = 1.25
METADATA_BASE_BYTES = 256 * MIB
METADATA_PER_ACCOUNT_BYTES = 256 * 1024
MIN_UNMEASURED_MAILBOX_BYTES = 64 * MIB
MIN_OPERATIONAL_RESERVE_BYTES = GIB
MAX_OPERATIONAL_RESERVE_BYTES = 10 * GIB
OPERATIONAL_RESERVE_FACTOR = 0.10
WARNING_HEADROOM_FACTOR = 1.25
IMPORT_STORE_FACTOR = 1.25
IMPORT_INDEX_FACTOR = 0.30
IMPORT_TEMPORARY_FACTOR = 1.05


@dataclass(frozen=True, slots=True)
class DiskCapacityAssessment:
    status: str
    archive_path: str
    filesystem_total_bytes: int
    filesystem_free_bytes: int
    remaining_accounts: int
    measured_mailboxes: int
    unmeasured_accounts: int
    measured_mailbox_bytes: int
    estimated_unmeasured_mailbox_bytes: int
    estimated_archive_growth_bytes: int
    estimated_peak_temporary_bytes: int
    estimated_metadata_bytes: int
    operational_reserve_bytes: int
    estimated_required_free_bytes: int
    estimated_free_after_export_bytes: int

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["assumptions"] = {
            "mailbox_archive_factor": MAILBOX_ARCHIVE_FACTOR,
            "warning_headroom_factor": WARNING_HEADROOM_FACTOR,
        }
        return value


@dataclass(frozen=True, slots=True)
class FilesystemCapacity:
    path: str
    roles: tuple[str, ...]
    total_bytes: int
    free_bytes: int
    estimated_growth_bytes: int
    operational_reserve_bytes: int
    estimated_required_free_bytes: int
    status: str


@dataclass(frozen=True, slots=True)
class ImportCapacityAssessment:
    status: str
    remaining_accounts: int
    remaining_mailbox_artifacts: int
    mailbox_artifact_bytes: int
    filesystems: tuple[FilesystemCapacity, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "remaining_accounts": self.remaining_accounts,
            "remaining_mailbox_artifacts": self.remaining_mailbox_artifacts,
            "mailbox_artifact_bytes": self.mailbox_artifact_bytes,
            "filesystems": [asdict(filesystem) for filesystem in self.filesystems],
            "assumptions": {
                "store_factor": IMPORT_STORE_FACTOR,
                "index_factor": IMPORT_INDEX_FACTOR,
                "temporary_factor": IMPORT_TEMPORARY_FACTOR,
                "warning_headroom_factor": WARNING_HEADROOM_FACTOR,
            },
        }


def assess_export_disk(
    archive_path: Path,
    *,
    remaining_accounts: list[str],
    mailbox_usage: dict[str, int],
    include_mailboxes: bool,
    workers: int,
) -> DiskCapacityAssessment:
    disk = shutil.disk_usage(archive_path)
    normalized_usage = {name.casefold(): size for name, size in mailbox_usage.items()}
    measured_sizes = (
        [
            normalized_usage[account.casefold()]
            for account in remaining_accounts
            if account.casefold() in normalized_usage
        ]
        if include_mailboxes
        else []
    )
    unmeasured_count = (
        sum(1 for account in remaining_accounts if account.casefold() not in normalized_usage)
        if include_mailboxes
        else 0
    )
    measured_bytes = sum(measured_sizes)
    if unmeasured_count:
        if measured_sizes:
            fallback_size = max(
                MIN_UNMEASURED_MAILBOX_BYTES,
                math.ceil(measured_bytes / len(measured_sizes)),
            )
        elif normalized_usage:
            usage_values = list(normalized_usage.values())
            fallback_size = max(
                MIN_UNMEASURED_MAILBOX_BYTES,
                math.ceil(sum(usage_values) / len(usage_values)),
            )
        else:
            fallback_size = MIN_UNMEASURED_MAILBOX_BYTES
    else:
        fallback_size = 0
    estimated_unmeasured_bytes = fallback_size * unmeasured_count
    estimated_mailbox_sizes = [*measured_sizes, *([fallback_size] * unmeasured_count)]
    estimated_mailbox_bytes = measured_bytes + estimated_unmeasured_bytes
    estimated_archive_mailbox_bytes = math.ceil(estimated_mailbox_bytes * MAILBOX_ARCHIVE_FACTOR)
    concurrent_mailboxes = sorted(estimated_mailbox_sizes, reverse=True)[:workers]
    estimated_temporary_bytes = math.ceil(sum(concurrent_mailboxes) * MAILBOX_ARCHIVE_FACTOR)
    estimated_metadata_bytes = (
        METADATA_BASE_BYTES + len(remaining_accounts) * METADATA_PER_ACCOUNT_BYTES
    )
    estimated_archive_growth_bytes = estimated_archive_mailbox_bytes + estimated_metadata_bytes
    reserve_basis = estimated_archive_growth_bytes + estimated_temporary_bytes
    operational_reserve_bytes = min(
        MAX_OPERATIONAL_RESERVE_BYTES,
        max(
            MIN_OPERATIONAL_RESERVE_BYTES,
            math.ceil(reserve_basis * OPERATIONAL_RESERVE_FACTOR),
        ),
    )
    estimated_required_free_bytes = (
        estimated_archive_growth_bytes + estimated_temporary_bytes + operational_reserve_bytes
    )
    if disk.free < estimated_required_free_bytes:
        status = "insufficient"
    elif disk.free < math.ceil(estimated_required_free_bytes * WARNING_HEADROOM_FACTOR):
        status = "warning"
    else:
        status = "sufficient"

    return DiskCapacityAssessment(
        status=status,
        archive_path=str(archive_path.resolve()),
        filesystem_total_bytes=disk.total,
        filesystem_free_bytes=disk.free,
        remaining_accounts=len(remaining_accounts),
        measured_mailboxes=len(measured_sizes),
        unmeasured_accounts=unmeasured_count,
        measured_mailbox_bytes=measured_bytes,
        estimated_unmeasured_mailbox_bytes=estimated_unmeasured_bytes,
        estimated_archive_growth_bytes=estimated_archive_growth_bytes,
        estimated_peak_temporary_bytes=estimated_temporary_bytes,
        estimated_metadata_bytes=estimated_metadata_bytes,
        operational_reserve_bytes=operational_reserve_bytes,
        estimated_required_free_bytes=estimated_required_free_bytes,
        estimated_free_after_export_bytes=max(0, disk.free - estimated_archive_growth_bytes),
    )


def assess_import_disks(
    archive_path: Path,
    *,
    volume_paths: dict[str, list[Path]],
    remaining_mailbox_sizes: list[int],
    remaining_accounts: int,
    workers: int,
) -> ImportCapacityAssessment:
    mailbox_bytes = sum(remaining_mailbox_sizes)
    metadata_bytes = METADATA_BASE_BYTES + remaining_accounts * METADATA_PER_ACCOUNT_BYTES
    requirements: list[tuple[str, Path, int]] = []
    primary_paths = volume_paths.get("primaryMessage", [])
    index_paths = volume_paths.get("index", [])
    target_fallback = primary_paths[0] if primary_paths else Path("/opt/zimbra")
    requirements.append(
        (
            "message-store-and-metadata",
            target_fallback,
            math.ceil(mailbox_bytes * IMPORT_STORE_FACTOR) + metadata_bytes,
        )
    )
    if index_paths:
        requirements.append(
            (
                "mailbox-index",
                index_paths[0],
                math.ceil(mailbox_bytes * IMPORT_INDEX_FACTOR),
            )
        )
    temporary_bytes = math.ceil(
        sum(sorted(remaining_mailbox_sizes, reverse=True)[:workers]) * IMPORT_TEMPORARY_FACTOR
    )
    requirements.append(("archive-temporary", archive_path, temporary_bytes))

    grouped: dict[int, dict[str, object]] = {}
    for role, path, required in requirements:
        resolved = path.resolve(strict=True)
        device = os.stat(resolved).st_dev
        item = grouped.setdefault(
            device,
            {"path": resolved, "roles": [], "growth": 0},
        )
        roles = item["roles"]
        if not isinstance(roles, list):
            raise TypeError("filesystem role accumulator is not a list")
        roles.append(role)
        item["growth"] = int(item["growth"]) + required

    filesystems: list[FilesystemCapacity] = []
    for item in grouped.values():
        path = item["path"]
        if not isinstance(path, Path):
            raise TypeError("filesystem path accumulator is not a Path")
        growth = int(item["growth"])
        disk = shutil.disk_usage(path)
        reserve = min(
            MAX_OPERATIONAL_RESERVE_BYTES,
            max(
                MIN_OPERATIONAL_RESERVE_BYTES,
                math.ceil(growth * OPERATIONAL_RESERVE_FACTOR),
            ),
        )
        required = growth + reserve
        if disk.free < required:
            status = "insufficient"
        elif disk.free < math.ceil(required * WARNING_HEADROOM_FACTOR):
            status = "warning"
        else:
            status = "sufficient"
        roles = item["roles"]
        if not isinstance(roles, list):
            raise TypeError("filesystem role accumulator is not a list")
        filesystems.append(
            FilesystemCapacity(
                path=str(path),
                roles=tuple(sorted(str(role) for role in roles)),
                total_bytes=disk.total,
                free_bytes=disk.free,
                estimated_growth_bytes=growth,
                operational_reserve_bytes=reserve,
                estimated_required_free_bytes=required,
                status=status,
            )
        )
    overall_status = (
        "insufficient"
        if any(filesystem.status == "insufficient" for filesystem in filesystems)
        else "warning"
        if any(filesystem.status == "warning" for filesystem in filesystems)
        else "sufficient"
    )
    return ImportCapacityAssessment(
        status=overall_status,
        remaining_accounts=remaining_accounts,
        remaining_mailbox_artifacts=len(remaining_mailbox_sizes),
        mailbox_artifact_bytes=mailbox_bytes,
        filesystems=tuple(sorted(filesystems, key=lambda filesystem: filesystem.path)),
    )


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
