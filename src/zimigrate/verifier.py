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
EXPORT_PHASE_BY_KIND = {
    "global_config": "export:global-config",
    "server": "export:server",
    "domain": "export:domain",
    "cos": "export:cos",
    "account": "export:account",
    "calendar_resource": "export:account",
    "distribution_list": "export:distribution-list",
    "dynamic_distribution_list": "export:distribution-list",
}
BOOLEAN_EXPORT_OPTIONS = {
    "include_domains",
    "include_cos",
    "include_accounts",
    "include_mailboxes",
    "include_distribution_lists",
    "include_global_config",
    "include_server_config",
    "include_system_mailboxes",
    "include_secrets",
    "mailbox_lock",
}
LIST_EXPORT_OPTIONS = {
    "account_include",
    "account_exclude",
    "target_users",
    "target_domains",
}
INTEGER_EXPORT_OPTIONS = {"mailbox_start_year", "mailbox_chunk_years"}
STRING_EXPORT_OPTIONS = {"mailbox_mode", "mailbox_format"}
KNOWN_EXPORT_OPTIONS = (
    BOOLEAN_EXPORT_OPTIONS | LIST_EXPORT_OPTIONS | INTEGER_EXPORT_OPTIONS | STRING_EXPORT_OPTIONS
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
    source_ids: set[str] = set()
    expected_record_paths: set[str] = set()
    for kind in ENTITY_KINDS:
        records = list(archive.iter_entities(kind))
        if records:
            counts[kind] = len(records)
            LOGGER.info("Found %s archived %s record(s)", len(records), kind)
        for record in records:
            relative = archive.entity_relative_path(kind, record.name)
            expected_record_paths.add(relative)
            _verify_entity_checkpoint(archive, kind, record.name, relative)
            identity_kind = _identity_kind(kind)
            identity = (identity_kind, record.name.casefold())
            if identity in entity_names:
                raise ArchiveError(
                    f"Archive contains duplicate {identity_kind} identity: {record.name}"
                )
            entity_names.add(identity)
            if record.source_id:
                normalized_source_id = record.source_id.casefold()
                if normalized_source_id in source_ids:
                    raise ArchiveError(
                        f"Archive contains a duplicate source object ID: {record.source_id}"
                    )
                source_ids.add(normalized_source_id)
            if kind == "global_config" and record.name != "global":
                raise ArchiveError("Global configuration record has an invalid identity")
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
    object_root = archive.root / "objects"
    stored_records = (
        {
            path.relative_to(archive.root).as_posix()
            for path in object_root.rglob("*")
            if path.is_file()
        }
        if object_root.is_dir()
        else set()
    )
    if unexpected_records := stored_records.difference(expected_record_paths):
        unexpected = sorted(unexpected_records)[0]
        raise ArchiveError(f"Archive contains an unreferenced entity artifact: {unexpected}")
    expected = manifest["counts"]
    for kind in ENTITY_KINDS:
        expected_count = expected.get(kind, 0)
        if counts.get(kind, 0) != expected_count:
            raise ArchiveError(
                f"Archive count mismatch for {kind}: expected {expected_count}, "
                f"found {counts.get(kind, 0)}"
            )
    export_options = manifest["export_options"]
    if export_options.get("include_global_config") is True and counts.get("global_config", 0) != 1:
        raise ArchiveError("A complete global configuration export must contain one record")
    if export_options.get("include_server_config") is True and counts.get("server", 0) < 1:
        raise ArchiveError("A complete server configuration export must contain a server record")
    LOGGER.info("Archive validation passed")
    return counts


def _verify_artifact(archive: MigrationArchive, artifact: Artifact, deep: bool) -> None:
    archive.validate_mailbox_artifact(
        artifact.path,
        artifact.sha256,
        deep=deep,
        archive_format=artifact.archive_format,
        expected_size=artifact.size,
        expected_unpacked_size=artifact.unpacked_size if deep and artifact.unpacked_size else None,
    )


def _verify_entity_checkpoint(
    archive: MigrationArchive,
    kind: str,
    name: str,
    relative: str,
) -> None:
    phase = EXPORT_PHASE_BY_KIND[kind]
    checkpoint = archive.state.get(phase, name)
    if checkpoint is None or checkpoint.status != "success":
        raise ArchiveError(f"Entity has no successful export checkpoint: {kind} {name}")
    if checkpoint.artifact_path != relative or not checkpoint.checksum:
        raise ArchiveError(f"Entity export checkpoint does not match its artifact: {kind} {name}")
    archive.validate_entity_artifact(kind, name, relative, checkpoint.checksum)


def _validate_manifest(manifest: dict[str, object]) -> None:
    if manifest.get("completed") is not True:
        raise ArchiveError("Archive manifest completion marker is invalid")
    if manifest.get("encrypted") is not False:
        raise ArchiveError("Encrypted archives are not supported")
    if not isinstance(manifest.get("source_version"), str) or not manifest["source_version"]:
        raise ArchiveError("Archive manifest source version is invalid")
    source_host = manifest.get("source_host")
    if source_host is not None and (not isinstance(source_host, str) or not source_host):
        raise ArchiveError("Archive manifest source host is invalid")
    for field in ("archive_id", "created_at", "updated_at"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ArchiveError(f"Archive manifest {field} is invalid")
    export_options = manifest.get("export_options")
    if not isinstance(export_options, dict):
        raise ArchiveError("Archive manifest export options are invalid")
    if unknown := set(export_options).difference(KNOWN_EXPORT_OPTIONS):
        raise ArchiveError(
            f"Archive manifest contains an unknown export option: {sorted(unknown)[0]}"
        )
    for name in BOOLEAN_EXPORT_OPTIONS.intersection(export_options):
        if not isinstance(export_options[name], bool):
            raise ArchiveError(f"Archive manifest export option is invalid: {name}")
    for name in LIST_EXPORT_OPTIONS.intersection(export_options):
        value = export_options[name]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ArchiveError(f"Archive manifest export option is invalid: {name}")
    for name in INTEGER_EXPORT_OPTIONS.intersection(export_options):
        value = export_options[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ArchiveError(f"Archive manifest export option is invalid: {name}")
    for name in STRING_EXPORT_OPTIONS.intersection(export_options):
        if not isinstance(export_options[name], str):
            raise ArchiveError(f"Archive manifest export option is invalid: {name}")
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
