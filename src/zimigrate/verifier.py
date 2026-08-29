from __future__ import annotations

import logging

from zimigrate.archive import MigrationArchive
from zimigrate.errors import ArchiveError, Interrupted
from zimigrate.interrupt import WorkerPool, bounded_futures
from zimigrate.models import Artifact
from zimigrate.progress import PhaseProgress

LOGGER = logging.getLogger(__name__)
ENTITY_KINDS = (
    "global_config",
    "server",
    "domain",
    "cos",
    "account",
    "calendar_resource",
    "distribution_list",
    "dynamic_distribution_list",
)


def verify_archive(archive: MigrationArchive, *, deep: bool, workers: int = 1) -> dict[str, int]:
    LOGGER.info("Validating archive manifest and records")
    manifest = archive.manifest()
    if not manifest.get("completed"):
        raise ArchiveError("Archive manifest reports an incomplete export")
    _validate_manifest(manifest)
    counts: dict[str, int] = {}
    artifacts: list[Artifact] = []
    entity_names: set[tuple[str, str]] = set()
    for kind in ENTITY_KINDS:
        records = list(archive.iter_entities(kind))
        if records:
            counts[kind] = len(records)
            LOGGER.info("Found %s archived %s record(s)", len(records), kind)
        for record in records:
            identity_kind = _identity_kind(kind)
            identity = (identity_kind, record.name.casefold())
            if identity in entity_names:
                raise ArchiveError(
                    f"Archive contains duplicate {identity_kind} identity: {record.name}"
                )
            entity_names.add(identity)
            if record.artifacts and kind not in {"account", "calendar_resource"}:
                raise ArchiveError(f"Archived {kind} record unexpectedly contains mailbox data")
            if kind in {"account", "calendar_resource"}:
                for artifact in record.artifacts:
                    expected_path = archive.mailbox_relative_path(
                        record.name, artifact.label, artifact.archive_format
                    )
                    if artifact.path != expected_path:
                        raise ArchiveError(
                            f"Mailbox artifact path does not match its identity: {artifact.path}"
                        )
                    if artifact.encrypted:
                        raise ArchiveError(
                            f"Encrypted mailbox artifacts are not supported: {artifact.path}"
                        )
            artifacts.extend(record.artifacts)
    if artifacts:
        progress = PhaseProgress(
            LOGGER, kind="mailbox-artifact", total=len(artifacts), action="verify"
        )
        with WorkerPool(workers, "verify-mailbox") as executor:
            for artifact, future in bounded_futures(
                executor,
                artifacts,
                lambda item: _verify_artifact(archive, item, deep),
                max_pending=workers * 2,
            ):
                path = artifact.path
                try:
                    future.result()
                    progress.complete(path)
                except Interrupted:
                    raise
                except Exception:
                    progress.complete(path, failed=True)
                    raise
    if artifacts:
        counts["mailbox_artifact"] = len(artifacts)
    artifact_paths = [artifact.path for artifact in artifacts]
    if len(set(artifact_paths)) != len(artifact_paths):
        raise ArchiveError("Archive contains duplicate mailbox artifact references")
    mailbox_root = archive.root / "mailboxes"
    stored_mailboxes = (
        {
            path.relative_to(archive.root).as_posix()
            for path in mailbox_root.rglob("*")
            if path.is_file()
        }
        if mailbox_root.is_dir()
        else set()
    )
    unexpected_mailboxes = stored_mailboxes.difference(artifact_paths)
    if unexpected_mailboxes:
        unexpected = sorted(unexpected_mailboxes)[0]
        raise ArchiveError(f"Archive contains an unreferenced mailbox artifact: {unexpected}")
    expected = manifest["counts"]
    for kind, expected_count in expected.items():
        if counts.get(kind, 0) != expected_count:
            raise ArchiveError(
                f"Archive count mismatch for {kind}: expected {expected_count}, "
                f"found {counts.get(kind, 0)}"
            )
    LOGGER.info("Archive validation passed")
    return counts


def _verify_artifact(archive: MigrationArchive, artifact: Artifact, deep: bool) -> None:
    archive.validate_mailbox_artifact(
        artifact.path,
        artifact.sha256,
        deep=deep,
        archive_format=artifact.archive_format,
        expected_plaintext_checksum=artifact.plaintext_sha256 if deep else None,
        expected_unpacked_size=artifact.unpacked_size if deep and artifact.unpacked_size else None,
    )


def _validate_manifest(manifest: dict[str, object]) -> None:
    if manifest.get("completed") is not True:
        raise ArchiveError("Archive manifest completion marker is invalid")
    if manifest.get("encrypted") is not False:
        raise ArchiveError("Encrypted archives are not supported")
    if not isinstance(manifest.get("source_version"), str):
        raise ArchiveError("Archive manifest source version is invalid")
    for field in ("archive_id", "created_at", "updated_at"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ArchiveError(f"Archive manifest {field} is invalid")
    if not isinstance(manifest.get("export_options"), dict):
        raise ArchiveError("Archive manifest export options are invalid")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ArchiveError("Archive manifest counts are invalid")
    for kind, count in counts.items():
        if kind not in ENTITY_KINDS:
            raise ArchiveError(f"Archive manifest contains an unsupported entity kind: {kind}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ArchiveError(f"Archive manifest count is invalid for {kind}")


def _identity_kind(kind: str) -> str:
    if kind in {"account", "calendar_resource"}:
        return "mailbox account"
    if kind in {"distribution_list", "dynamic_distribution_list"}:
        return "distribution list"
    return kind
