from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import replace
from pathlib import Path

from zimigrate.archive import MigrationArchive
from zimigrate.attributes import (
    SENSITIVE_ATTRIBUTE,
    apply_attributes_resiliently,
    first,
    flatten_operations,
    mutable_attributes,
    remap_values,
)
from zimigrate.capacity import assess_import_disks, format_bytes
from zimigrate.config import AppConfig
from zimigrate.errors import CompatibilityError, Interrupted, ZimigrateError
from zimigrate.interrupt import WorkerPool, bounded_futures
from zimigrate.models import Attributes, EntityRecord
from zimigrate.progress import PhaseProgress, entity_start_fields
from zimigrate.scope import (
    filter_account_records,
    filter_cos_records,
    filter_distribution_records,
    filter_domain_records,
    parse_bound_scope,
    scope_from_transfer,
)
from zimigrate.state import StateRecord
from zimigrate.util import atomic_json, ensure_relative_path, sha256_file, utc_now
from zimigrate.zimbra import ZimbraClient

LOGGER = logging.getLogger(__name__)

SIGNATURE_REFERENCE_ATTRIBUTES = {
    "zimbraPrefDefaultSignatureId",
    "zimbraPrefForwardReplySignatureId",
    "zimbraPrefMailSignatureContactId",
    "zimbraPrefCalendarAutoAcceptSignatureId",
    "zimbraPrefCalendarAutoDeclineSignatureId",
    "zimbraPrefCalendarAutoDenySignatureId",
    "zimbraPrefCalendarAcceptSignatureId",
    "zimbraPrefCalendarTentativeSignatureId",
    "zimbraPrefCalendarDeclineSignatureId",
}
ZIMBRA_ID = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)


class WarningReport:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entity: str, attribute: str, reason: str) -> None:
        value = {
            "time": utc_now(),
            "entity": entity,
            "attribute": attribute,
            "reason": reason,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(self.path, 0o600)


class Importer:
    def __init__(self, config: AppConfig, archive: MigrationArchive) -> None:
        self.config = config
        self.archive = archive
        self.client = ZimbraClient(
            config.target,
            retries=config.transfer.retries,
            retry_base_seconds=config.transfer.retry_base_seconds,
        )
        self.warnings = WarningReport(
            ensure_relative_path(archive.root, "reports/import-warnings.ndjson")
        )
        self._skipped_distribution_lists: set[str] = set()

    def run(self) -> dict[str, int]:
        manifest = self.archive.manifest()
        if not manifest.get("completed"):
            raise ZimigrateError("The export is incomplete; resume export before importing")
        version = self.client.preflight(require_mailbox=self.config.transfer.include_mailboxes)
        if not self.config.import_options.allows_version(version):
            raise CompatibilityError(
                f"Target version '{version}' does not match required pattern "
                f"'{self.config.import_options.expected_target_version_pattern}'"
            )
        LOGGER.info("Target preflight passed", extra={"event": "preflight", "version": version})
        self._apply_resolved_scope()

        transfer = self.config.transfer
        LOGGER.info("Loading archived records")
        cos_records = list(self.archive.iter_entities("cos")) if transfer.include_cos else []
        domain_records = (
            list(self.archive.iter_entities("domain")) if transfer.include_domains else []
        )
        account_records = (
            [
                *self.archive.iter_entities("account"),
                *self.archive.iter_entities("calendar_resource"),
            ]
            if transfer.include_accounts
            else []
        )
        distribution_records = (
            [
                *self.archive.iter_entities("distribution_list"),
                *self.archive.iter_entities("dynamic_distribution_list"),
            ]
            if transfer.include_distribution_lists
            else []
        )
        scope = scope_from_transfer(transfer)
        account_records = filter_account_records(account_records, transfer)
        domain_records = filter_domain_records(domain_records, scope)
        distribution_records = filter_distribution_records(distribution_records, scope)
        cos_records = filter_cos_records(
            cos_records, accounts=account_records, domains=domain_records, scope=scope
        )
        if scope.active:
            LOGGER.info(
                "Import limited to selected users and domains",
                extra={**scope.as_options(), "event": "inventory"},
            )
            if scope.users and transfer.include_accounts and not account_records:
                raise ZimigrateError("No archived accounts matched --user/--domain")
            if scope.domains and transfer.include_domains and not domain_records:
                raise ZimigrateError("No archived domains matched --domain")
        LOGGER.info(
            "Loaded archive inventory",
            extra={
                "event": "inventory",
                "inventory": {
                    "COS": len(cos_records),
                    "Domains": len(domain_records),
                    "Accounts": len(account_records),
                    "Lists": len(distribution_records),
                },
                "cos": len(cos_records),
                "domains": len(domain_records),
                "accounts": len(account_records),
                "distribution_lists": len(distribution_records),
            },
        )

        LOGGER.info("Checking import disk capacity")
        self._check_import_capacity(account_records)
        LOGGER.info("Validating destination mailhosts")
        self._validate_target_mailhosts()
        self._bind_import_options()

        LOGGER.info("Importing classes of service")
        cos_mapping = self._import_cos(cos_records)
        LOGGER.info("Importing domains")
        domain_mapping = self._import_domains(domain_records, cos_mapping)
        self._run_parallel(
            "account", account_records, lambda record: self._import_account(record, cos_mapping)
        )
        LOGGER.info("Importing distribution lists")
        self._import_distribution_lists(distribution_records)
        LOGGER.info("Resolving destination object IDs")
        object_mapping = {
            **cos_mapping,
            **domain_mapping,
            **self._destination_id_mapping(account_records),
            **self._destination_id_mapping(distribution_records),
        }
        LOGGER.info("Importing domain account references")
        self._import_domain_account_references(domain_records, object_mapping)
        LOGGER.info("Importing embedded object references")
        self._import_embedded_references(
            [*cos_records, *domain_records, *account_records, *distribution_records],
            object_mapping,
        )
        LOGGER.info("Importing access control entries")
        self._import_access_control(
            [*cos_records, *domain_records, *account_records, *distribution_records],
            object_mapping,
        )
        self._run_parallel(
            "distribution-members",
            distribution_records,
            self._import_distribution_members,
        )
        LOGGER.info("Importing optional global and server configuration")
        self._import_optional_configuration()

        failed = [
            record for record in self.archive.state.failed() if record.phase.startswith("import:")
        ]
        if failed:
            raise ZimigrateError(
                f"Import finished with {len(failed)} failed operation(s); rerun to resume"
            )
        LOGGER.info("Import finished successfully")
        return _summary_counts(self.archive.state.summary(), prefix="import:")

    def _check_import_capacity(self, account_records: list[EntityRecord]) -> None:
        transfer = self.config.transfer
        completed_accounts = self.archive.state.successful_entities("import:account-complete")
        completed_mailboxes = self.archive.state.successful_entities("import:mailbox")
        remaining_sizes = (
            [
                max(artifact.size, artifact.unpacked_size)
                for record in account_records
                for artifact in record.artifacts
                if f"{record.name}:{artifact.label}" not in completed_mailboxes
            ]
            if transfer.include_mailboxes
            else []
        )
        if transfer.include_mailboxes or remaining_sizes:
            volume_paths = self.client.get_current_volume_paths()
        else:
            # Metadata-only imports still need a conservative check on the Zimbra
            # installation filesystem, but do not require mailbox volumes to exist.
            volume_paths = {"primaryMessage": [Path("/opt/zimbra")]}
        assessment = assess_import_disks(
            self.archive.root,
            volume_paths=volume_paths,
            remaining_mailbox_sizes=remaining_sizes,
            remaining_accounts=sum(
                1 for record in account_records if record.name not in completed_accounts
            ),
            workers=transfer.workers,
        )
        report_path = ensure_relative_path(self.archive.root, "reports/import-disk-assessment.json")
        atomic_json(report_path, assessment.as_dict())
        if assessment.status == "insufficient":
            LOGGER.error(
                "Import disk capacity is insufficient",
                extra={"report": report_path},
            )
            details = "; ".join(
                f"{filesystem.path}: {format_bytes(filesystem.free_bytes)} available, "
                f"{format_bytes(filesystem.estimated_required_free_bytes)} required"
                for filesystem in assessment.filesystems
                if filesystem.status == "insufficient"
            )
            raise ZimigrateError(f"Insufficient target disk space: {details}. See {report_path}")
        log_method = LOGGER.warning if assessment.status == "warning" else LOGGER.info
        log_method(
            "Import disk capacity check passed"
            if assessment.status == "sufficient"
            else "Import disk capacity is close to the safe limit",
            extra={
                "status": assessment.status,
                "mailbox_bytes": format_bytes(assessment.mailbox_artifact_bytes),
                "report": report_path,
                "filesystems": [
                    {
                        "path": filesystem.path,
                        "free": format_bytes(filesystem.free_bytes),
                        "required": format_bytes(filesystem.estimated_required_free_bytes),
                    }
                    for filesystem in assessment.filesystems
                ],
            },
        )

    def _import_cos(self, records: list[EntityRecord]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        progress = PhaseProgress(LOGGER, kind="cos", total=len(records), action="import")
        for record in records:
            LOGGER.info(
                "Importing COS %s",
                record.name,
                extra=entity_start_fields("cos", record.name, action="import"),
            )
            self._import_basic_entity(record)
            destination = self.client.get_cos(record.name)
            destination_id = first(destination, "zimbraId")
            if record.source_id and destination_id:
                mapping[record.source_id] = destination_id
            progress.complete(record.name)
        return mapping

    def _import_domains(
        self, records: list[EntityRecord], cos_mapping: dict[str, str]
    ) -> dict[str, str]:
        should_merge: dict[str, bool] = {}
        regular = [record for record in records if not _is_alias_domain(record)]
        aliases = [record for record in records if _is_alias_domain(record)]
        progress = PhaseProgress(LOGGER, kind="domain", total=len(records), action="import")
        for record in regular:
            LOGGER.info(
                "Importing domain %s",
                record.name,
                extra=entity_start_fields("domain", record.name, action="import"),
            )
            should_merge[record.name] = self._import_basic_entity(record)
            progress.complete(record.name)
        source_names = {record.source_id: record.name for record in regular if record.source_id}
        for record in aliases:
            LOGGER.info(
                "Importing alias domain %s",
                record.name,
                extra=entity_start_fields("domain", record.name, action="import"),
            )
            target_source_id = first(record.attributes, "zimbraDomainAliasTargetId")
            target_name = source_names.get(target_source_id)
            if not target_name:
                raise ZimigrateError(f"Alias domain {record.name} has no resolvable target domain")
            should_merge[record.name] = self._import_alias_domain(record, target_name)
            progress.complete(record.name)

        for record in records:
            if not should_merge[record.name]:
                continue
            source_default_cos = first(record.attributes, "zimbraDomainDefaultCOSId")
            if source_default_cos and source_default_cos in cos_mapping:
                self._apply(
                    "domain",
                    record.name,
                    {"zimbraDomainDefaultCOSId": [cos_mapping[source_default_cos]]},
                )
            elif source_default_cos:
                self.warnings.write(
                    record.name,
                    "zimbraDomainDefaultCOSId",
                    "source COS ID has no destination mapping",
                )
        return self._destination_id_mapping(records)

    def _existing_object_decision(
        self,
        *,
        exists: bool,
        prior: StateRecord | None,
        label: str,
    ) -> str:
        if not exists:
            return "create"
        if prior is not None:
            return "merge"
        policy = self.config.import_options.existing_policy
        if policy == "fail":
            raise ZimigrateError(f"Destination {label} already exists")
        if policy == "skip":
            return "skip"
        return "merge"

    def _import_alias_domain(self, record: EntityRecord, target_name: str) -> bool:
        phase = "import:domain"
        if self.archive.state.is_success(phase, record.name):
            state = self.archive.state.get(phase, record.name)
            return not (state and state.detail == "skipped-existing")
        prior = self.archive.state.get(phase, record.name)
        exists = self.client.exists("domain", record.name)
        decision = self._existing_object_decision(exists=exists, prior=prior, label=record.name)
        if decision == "skip":
            self.archive.state.start(phase, record.name)
            self.archive.state.succeed(phase, record.name, detail="skipped-existing")
            return False
        self.archive.state.start(phase, record.name)
        try:
            if decision == "create":
                self.client.create_alias_domain(record.name, target_name)
                if not first(record.attributes, "zimbraMailCatchAllForwardingAddress"):
                    self._apply(
                        "domain",
                        record.name,
                        {"zimbraMailCatchAllForwardingAddress": [f"@{target_name}"]},
                    )
            self._apply(
                "domain",
                record.name,
                mutable_attributes("domain", record.attributes),
            )
            self.archive.state.succeed(
                phase,
                record.name,
                detail="merged-existing" if exists else None,
            )
            return True
        except Exception as exc:
            self.archive.state.fail(phase, record.name, _error_summary(exc))
            raise

    def _import_basic_entity(self, record: EntityRecord) -> bool:
        phase = f"import:{record.kind}"
        if self.archive.state.is_success(phase, record.name):
            state = self.archive.state.get(phase, record.name)
            return not (state and state.detail == "skipped-existing")
        prior = self.archive.state.get(phase, record.name)
        exists = self.client.exists(record.kind, record.name)
        decision = self._existing_object_decision(
            exists=exists, prior=prior, label=f"{record.kind} {record.name}"
        )
        if decision == "skip":
            self.archive.state.start(phase, record.name)
            self.archive.state.succeed(phase, record.name, detail="skipped-existing")
            return False
        self.archive.state.start(phase, record.name)
        try:
            if decision == "create":
                initial_attributes: Attributes = {}
                if record.kind == "calendar_resource":
                    for attribute in ("displayName", "zimbraCalResType"):
                        if values := record.attributes.get(attribute):
                            initial_attributes[attribute] = values
                if record.kind in {"account", "calendar_resource"}:
                    # Newly created mailboxes remain unavailable until their content and
                    # metadata have been restored successfully.
                    initial_attributes["zimbraAccountStatus"] = ["maintenance"]
                    source_mailhost = first(record.attributes, "zimbraMailHost")
                    destination_mailhost = self.config.import_options.mailhost_map.get(
                        source_mailhost or "",
                        self.config.import_options.default_mailhost,
                    )
                    if destination_mailhost:
                        initial_attributes["zimbraMailHost"] = [destination_mailhost]
                self.client.create(
                    record.kind,
                    record.name,
                    flatten_operations(list(initial_attributes.items())),
                )
            attributes = mutable_attributes(record.kind, record.attributes)
            self._apply(record.kind, record.name, attributes)
            self.archive.state.succeed(
                phase,
                record.name,
                detail="merged-existing" if exists else None,
            )
            return True
        except Exception as exc:
            self.archive.state.fail(phase, record.name, _error_summary(exc))
            raise

    def _import_account(self, record: EntityRecord, cos_mapping: dict[str, str]) -> None:
        phase = "import:account-complete"
        if self.archive.state.is_success(phase, record.name):
            return
        LOGGER.info(
            "Importing account %s",
            record.name,
            extra=entity_start_fields("account", record.name, action="import"),
        )
        self.archive.state.start(phase, record.name)
        try:
            if not self.config.import_options.import_system_accounts and _is_system_account(record):
                self.archive.state.succeed(phase, record.name, detail="skipped-system-account")
                return
            should_merge = self._import_basic_entity(record)
            if not should_merge:
                self.archive.state.succeed(phase, record.name, detail="skipped-existing")
                return

            # Reassert maintenance on every incomplete attempt, including merges and
            # the narrow crash window after a previous attempt restored the final
            # status but before it committed the account-complete checkpoint.
            self._apply(record.kind, record.name, {"zimbraAccountStatus": ["maintenance"]})

            source_cos = first(record.attributes, "zimbraCOSId")
            if source_cos and source_cos in cos_mapping:
                self._apply(record.kind, record.name, {"zimbraCOSId": [cos_mapping[source_cos]]})
            elif source_cos:
                self.warnings.write(
                    record.name, "zimbraCOSId", "source COS ID has no destination mapping"
                )

            target_attributes = self._destination_account_attributes(record)
            self._import_account_aliases(record, target_attributes)
            if self.config.transfer.include_mailboxes:
                self._import_mailboxes(record, target_attributes)
            signature_mapping = self._import_signatures(record)
            self._import_identities(record, signature_mapping)
            self._import_data_sources(record)
            self._apply_signature_references(record, signature_mapping)

            status = first(record.attributes, "zimbraAccountStatus", "active") or "active"
            self._apply(record.kind, record.name, {"zimbraAccountStatus": [status]})
            self._flush_account_cache(record)
            self.archive.state.succeed(phase, record.name)
        except Exception as exc:
            self.archive.state.fail(phase, record.name, _error_summary(exc))
            raise

    def _destination_account_attributes(self, record: EntityRecord) -> Attributes:
        if record.kind == "calendar_resource":
            return self.client.get_calendar_resource(record.name)
        return self.client.get_account(record.name)

    def _flush_account_cache(self, record: EntityRecord) -> None:
        # LDAP-direct userPassword and status writes are invisible to mailboxd
        # until flushCache or ldap_cache_account_maxage.
        self.client.flush_cache("account", record.name)

    def _import_account_aliases(self, record: EntityRecord, current: Attributes) -> None:
        existing = set(current.get("zimbraMailAlias", []))
        for alias in record.aliases:
            if alias not in existing:
                self.client.add_account_alias(record.name, alias)
                existing.add(alias)

    def _import_mailboxes(self, record: EntityRecord, target_attributes: Attributes) -> None:
        mailbox_host = first(target_attributes, "zimbraMailHost")
        configured_resolution = self.config.import_options.mailbox_conflict_resolution
        for index, artifact in enumerate(record.artifacts):
            phase = "import:mailbox"
            entity = f"{record.name}:{artifact.label}"
            if self.archive.state.is_success(phase, entity):
                continue
            self.archive.state.start(phase, entity)
            try:
                self.archive.validate_mailbox_artifact(
                    artifact.path,
                    artifact.sha256,
                    deep=False,
                    archive_format=artifact.archive_format,
                )
                LOGGER.info(
                    "Importing mailbox %s (%s)",
                    record.name,
                    artifact.label,
                    extra=entity_start_fields(
                        "account", f"{record.name} ({artifact.label})", action="import"
                    ),
                )
                with self.archive.materialize_mailbox(artifact.path) as plaintext:
                    if sha256_file(plaintext) != artifact.plaintext_sha256:
                        raise ZimigrateError(
                            f"Plaintext mailbox checksum mismatch: {artifact.path}"
                        )
                    resolution = (
                        "skip"
                        if configured_resolution == "reset" and index > 0
                        else configured_resolution
                    )
                    self.client.import_mailbox(
                        record.name,
                        plaintext,
                        resolution,
                        mailbox_host,
                        artifact.archive_format,
                    )
                self.archive.state.succeed(
                    phase,
                    entity,
                    artifact_path=artifact.path,
                    checksum=artifact.sha256,
                )
            except Exception as exc:
                self.archive.state.fail(phase, entity, _error_summary(exc))
                raise

    def _import_signatures(self, record: EntityRecord) -> dict[str, str]:
        existing_sections = self.client.get_signatures(record.name)
        existing_names = _section_names(existing_sections, "zimbraSignatureName")
        for attributes in record.signatures:
            name = first(attributes, "zimbraSignatureName")
            if not name:
                self.warnings.write(record.name, "signature", "signature has no name")
                continue
            if name not in existing_names:
                self.client.create_signature(record.name, name)
                existing_names.add(name)
            mutable = mutable_attributes("signature", attributes)
            self._apply_custom(
                f"{record.name}/signature/{name}",
                mutable,
                lambda operations, sensitive, name=name: self.client.modify_signature(
                    record.name, name, operations, sensitive=sensitive
                ),
            )

        destination_sections = self.client.get_signatures(record.name)
        destination_by_name = {
            first(section, "zimbraSignatureName"): first(section, "zimbraSignatureId")
            for section in destination_sections
        }
        mapping: dict[str, str] = {}
        for attributes in record.signatures:
            source_id = first(attributes, "zimbraSignatureId")
            name = first(attributes, "zimbraSignatureName")
            destination_id = destination_by_name.get(name)
            if source_id and destination_id:
                mapping[source_id] = destination_id
        return mapping

    def _import_identities(self, record: EntityRecord, signature_mapping: dict[str, str]) -> None:
        existing = _section_names(self.client.get_identities(record.name), "zimbraPrefIdentityName")
        for attributes in record.identities:
            name = first(attributes, "zimbraPrefIdentityName")
            if not name:
                self.warnings.write(record.name, "identity", "identity has no name")
                continue
            if name not in existing:
                self.client.create_identity(record.name, name)
                existing.add(name)
            mutable = remap_values(mutable_attributes("identity", attributes), signature_mapping)
            self._apply_custom(
                f"{record.name}/identity/{name}",
                mutable,
                lambda operations, sensitive, name=name: self.client.modify_identity(
                    record.name, name, operations, sensitive=sensitive
                ),
            )

    def _import_data_sources(self, record: EntityRecord) -> None:
        existing = _section_names(self.client.get_data_sources(record.name), "zimbraDataSourceName")
        for attributes in record.data_sources:
            name = first(attributes, "zimbraDataSourceName")
            source_type = _data_source_type(attributes)
            if not name or not source_type:
                self.warnings.write(
                    record.name, "data_source", "data source name or type is unavailable"
                )
                continue
            if name not in existing:
                self.client.create_data_source(
                    record.name,
                    source_type,
                    name,
                    first(attributes, "zimbraDataSourceEnabled", "FALSE") or "FALSE",
                    first(attributes, "zimbraDataSourceFolderId", "2") or "2",
                )
                existing.add(name)
            mutable = mutable_attributes("data_source", attributes)
            self._apply_custom(
                f"{record.name}/data-source/{name}",
                mutable,
                lambda operations, sensitive, name=name: self.client.modify_data_source(
                    record.name, name, operations, sensitive=sensitive
                ),
            )

    def _apply_signature_references(
        self, record: EntityRecord, signature_mapping: dict[str, str]
    ) -> None:
        references = {
            name: [signature_mapping.get(value, value) for value in values]
            for name, values in record.attributes.items()
            if name in SIGNATURE_REFERENCE_ATTRIBUTES
        }
        self._apply(record.kind, record.name, references)

    def _import_distribution_lists(self, records: list[EntityRecord]) -> None:
        progress = PhaseProgress(
            LOGGER, kind="distribution-list", total=len(records), action="import"
        )
        for record in records:
            LOGGER.info(
                "Importing distribution list %s",
                record.name,
                extra=entity_start_fields("distribution-list", record.name, action="import"),
            )
            should_merge = self._import_basic_entity(record)
            if not should_merge:
                self._skipped_distribution_lists.add(record.name)
                progress.complete(record.name)
                continue
            current = self.client.get_distribution_list(record.name)
            existing_aliases = set(current.get("zimbraMailAlias", []))
            for alias in record.aliases:
                if alias not in existing_aliases:
                    self.client.add_distribution_alias(record.name, alias)
                    existing_aliases.add(alias)
            progress.complete(record.name)

    def _import_distribution_members(self, record: EntityRecord) -> None:
        phase = "import:distribution-members"
        if self.archive.state.is_success(phase, record.name):
            return
        self.archive.state.start(phase, record.name)
        try:
            if record.name in self._skipped_distribution_lists or not record.members:
                self.archive.state.succeed(phase, record.name)
                return
            existing = set(self.client.get_distribution_list_members(record.name))
            for member in record.members:
                if member not in existing:
                    self.client.add_distribution_member(record.name, member)
                    existing.add(member)
            self.archive.state.succeed(phase, record.name)
        except Exception as exc:
            self.archive.state.fail(phase, record.name, _error_summary(exc))
            raise

    def _destination_id_mapping(self, records: list[EntityRecord]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for record in records:
            if not record.source_id:
                continue
            complete = self.archive.state.get("import:account-complete", record.name)
            if complete and complete.detail == "skipped-system-account":
                continue
            if record.kind == "cos":
                attributes = self.client.get_cos(record.name)
            elif record.kind == "domain":
                attributes = self.client.get_domain(record.name)
            elif record.kind == "calendar_resource":
                attributes = self.client.get_calendar_resource(record.name)
            elif record.kind == "account":
                attributes = self.client.get_account(record.name)
            else:
                attributes = self.client.get_distribution_list(record.name)
            if destination_id := first(attributes, "zimbraId"):
                mapping[record.source_id] = destination_id
        return mapping

    def _import_access_control(
        self, records: list[EntityRecord], object_mapping: dict[str, str]
    ) -> None:
        normalized_mapping = {
            source_id.lower(): destination_id
            for source_id, destination_id in object_mapping.items()
        }
        for record in records:
            if self._was_skipped(record):
                continue
            source_values = record.attributes.get("zimbraACE", [])
            if not source_values:
                continue
            phase = "import:access-control"
            if self.archive.state.is_success(phase, record.name):
                continue
            self.archive.state.start(phase, record.name)
            try:
                values = [
                    _remap_reference(source_value, normalized_mapping)
                    for source_value in source_values
                ]
                self._apply(record.kind, record.name, {"zimbraACE": values})
                self.archive.state.succeed(phase, record.name)
            except Exception as exc:
                self.archive.state.fail(phase, record.name, _error_summary(exc))
                raise

    def _import_embedded_references(
        self, records: list[EntityRecord], object_mapping: dict[str, str]
    ) -> None:
        normalized_mapping = {
            source_id.lower(): destination_id
            for source_id, destination_id in object_mapping.items()
        }
        for record in records:
            if self._was_skipped(record):
                continue
            changed: Attributes = {}
            for attribute, values in mutable_attributes(record.kind, record.attributes).items():
                mapped_values = [_remap_reference(value, normalized_mapping) for value in values]
                if mapped_values != values:
                    changed[attribute] = mapped_values
            if not changed:
                continue
            phase = "import:embedded-references"
            if self.archive.state.is_success(phase, record.name):
                continue
            self.archive.state.start(phase, record.name)
            try:
                self._apply(record.kind, record.name, changed)
                self.archive.state.succeed(phase, record.name)
            except Exception as exc:
                self.archive.state.fail(phase, record.name, _error_summary(exc))
                raise

    def _import_domain_account_references(
        self, records: list[EntityRecord], object_mapping: dict[str, str]
    ) -> None:
        for record in records:
            if self._was_skipped(record):
                continue
            references: Attributes = {}
            for attribute in ("zimbraGalAccountId",):
                source_values = record.attributes.get(attribute, [])
                if source_values:
                    mapped = [
                        object_mapping[value] for value in source_values if value in object_mapping
                    ]
                    if len(mapped) != len(source_values):
                        self.warnings.write(
                            record.name,
                            attribute,
                            "one or more source IDs have no destination mapping",
                        )
                    if mapped:
                        references[attribute] = mapped
            self._apply("domain", record.name, references)

    def _was_skipped(self, record: EntityRecord) -> bool:
        state = self.archive.state.get(f"import:{record.kind}", record.name)
        if state and state.detail == "skipped-existing":
            return True
        complete = self.archive.state.get("import:account-complete", record.name)
        return bool(complete and complete.detail == "skipped-system-account")

    def _import_optional_configuration(self) -> None:
        options = self.config.import_options
        if options.apply_global_config and self.config.transfer.include_global_config:
            records = list(self.archive.iter_entities("global_config"))
            if records:
                phase = "import:global-config"
                if not self.archive.state.is_success(phase, "global"):
                    self.archive.state.start(phase, "global")
                    try:
                        attributes = mutable_attributes(
                            "global_config",
                            records[0].attributes,
                            allowlist=options.global_attribute_allowlist,
                            allow_sensitive=options.allow_sensitive_config,
                        )
                        self._apply("global_config", "global", attributes)
                        self.archive.state.succeed(phase, "global")
                    except Exception as exc:
                        self.archive.state.fail(phase, "global", _error_summary(exc))
                        raise

        if options.apply_server_config and self.config.transfer.include_server_config:
            for record in self.archive.iter_entities("server"):
                destination = options.server_map.get(record.name)
                if not destination:
                    self.warnings.write(
                        record.name, "server", "server has no import.server_map destination"
                    )
                    continue
                if not self.client.exists("server", destination):
                    raise ZimigrateError(f"Mapped destination server does not exist: {destination}")
                phase = "import:server-config"
                if self.archive.state.is_success(phase, record.name):
                    continue
                self.archive.state.start(phase, record.name)
                try:
                    attributes = mutable_attributes(
                        "server",
                        record.attributes,
                        allowlist=options.server_attribute_allowlist,
                        allow_sensitive=options.allow_sensitive_config,
                    )
                    self._apply("server", destination, attributes)
                    self.archive.state.succeed(phase, record.name)
                except Exception as exc:
                    self.archive.state.fail(phase, record.name, _error_summary(exc))
                    raise

    def _validate_target_mailhosts(self) -> None:
        options = self.config.import_options
        destinations = set(options.mailhost_map.values())
        if options.default_mailhost:
            destinations.add(options.default_mailhost)
        for hostname in destinations:
            if not self.client.exists("server", hostname):
                raise ZimigrateError(f"Configured destination mailhost does not exist: {hostname}")

    def _bind_import_options(self) -> None:
        options = self.config.import_options
        value = json.dumps(
            {
                "existing_policy": options.existing_policy,
                "mailbox_conflict_resolution": options.mailbox_conflict_resolution,
                "strict_attributes": options.strict_attributes,
                "apply_global_config": options.apply_global_config,
                "global_attribute_allowlist": list(options.global_attribute_allowlist),
                "apply_server_config": options.apply_server_config,
                "server_attribute_allowlist": list(options.server_attribute_allowlist),
                "server_map": options.server_map,
                "mailhost_map": options.mailhost_map,
                "default_mailhost": options.default_mailhost,
                "import_system_accounts": options.import_system_accounts,
                "allow_sensitive_config": options.allow_sensitive_config,
                "include_domains": self.config.transfer.include_domains,
                "include_cos": self.config.transfer.include_cos,
                "include_accounts": self.config.transfer.include_accounts,
                "include_mailboxes": self.config.transfer.include_mailboxes,
                "include_distribution_lists": (self.config.transfer.include_distribution_lists),
                "include_global_config": self.config.transfer.include_global_config,
                "include_server_config": self.config.transfer.include_server_config,
                "target_users": list(self.config.transfer.target_users),
                "target_domains": list(self.config.transfer.target_domains),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        phase = "import:configuration"
        state = self.archive.state.get(phase, "options")
        if state and state.status == "success" and not _same_import_bind(state.detail, value):
            raise ZimigrateError(
                "Import options differ from the existing checkpoints; "
                "use a copied archive with a fresh state database"
            )
            raise ZimigrateError(
                "Import options differ from the existing checkpoints; "
                "use a copied archive with a fresh state database"
            )
        if not state or state.status != "success":
            self.archive.state.start(phase, "options")
            self.archive.state.succeed(phase, "options", detail=value)

    def _apply_resolved_scope(self) -> None:
        cli_scope = scope_from_transfer(self.config.transfer)
        state = self.archive.state.get("import:configuration", "options")
        bound = parse_bound_scope(state.detail if state and state.status == "success" else None)
        if cli_scope.active and bound.active and cli_scope != bound:
            raise ZimigrateError(
                "Import --user/--domain differ from the existing checkpoints; "
                "use a copied archive with a fresh state database"
            )
        scope = bound if bound.active else cli_scope
        if scope != cli_scope:
            self.config = replace(
                self.config,
                transfer=replace(
                    self.config.transfer,
                    target_users=tuple(sorted(scope.users)),
                    target_domains=tuple(sorted(scope.domains)),
                ),
            )

    def _apply(self, kind: str, name: str, attributes: Attributes) -> None:
        self._apply_custom(
            name,
            attributes,
            lambda operations, sensitive: self.client.modify(
                kind, name, operations, sensitive=sensitive
            ),
        )

    def _apply_custom(
        self,
        entity: str,
        attributes: Attributes,
        operation: object,
    ) -> None:
        apply_attributes_resiliently(
            attributes,
            operation,  # type: ignore[arg-type]
            lambda attribute, reason: self.warnings.write(entity, attribute, reason),
            strict=self.config.import_options.strict_attributes,
        )

    def _run_parallel(self, label: str, records: list[EntityRecord], operation: object) -> None:
        progress = PhaseProgress(LOGGER, kind=label, total=len(records), action="import")
        errors: list[tuple[str, Exception]] = []
        with WorkerPool(self.config.transfer.workers, f"import-{label}") as executor:
            for record, future in bounded_futures(
                executor,
                records,
                operation,  # type: ignore[arg-type]
                max_pending=self.config.transfer.workers * 2,
            ):
                name = record.name
                try:
                    future.result()
                    progress.complete(name)
                except Interrupted:
                    raise
                except Exception as exc:
                    progress.complete(name, failed=True)
                    errors.append((name, exc))
                    LOGGER.error(
                        "Entity import failed",
                        extra={"kind": label, "entity": name, "error": _error_summary(exc)},
                    )
        if errors:
            raise ZimigrateError(
                f"{len(errors)} {label} import(s) failed; first failure: "
                f"{errors[0][0]}: {_error_summary(errors[0][1])}"
            )


def _same_import_bind(previous: str | None, current: str) -> bool:
    try:
        previous_value = json.loads(previous or "")
    except json.JSONDecodeError:
        return False
    try:
        current_value = json.loads(current)
    except json.JSONDecodeError:
        return False
    if not isinstance(previous_value, dict) or not isinstance(current_value, dict):
        return previous == current
    previous_value.setdefault("target_users", [])
    previous_value.setdefault("target_domains", [])
    current_value.setdefault("target_users", [])
    current_value.setdefault("target_domains", [])
    return previous_value == current_value


def _section_names(sections: list[Attributes], attribute: str) -> set[str]:
    return {value for section in sections if (value := first(section, attribute))}


def _is_alias_domain(record: EntityRecord) -> bool:
    domain_type = (first(record.attributes, "zimbraDomainType", "") or "").lower()
    return (
        domain_type == "alias" or first(record.attributes, "zimbraDomainAliasTargetId") is not None
    )


def _is_system_account(record: EntityRecord) -> bool:
    return any(
        (first(record.attributes, attribute, "FALSE") or "").upper() == "TRUE"
        for attribute in ("zimbraIsSystemAccount", "zimbraIsSystemResource")
    )


def _data_source_type(attributes: Attributes) -> str | None:
    explicit = first(attributes, "zimbraDataSourceType")
    if explicit:
        return explicit.lower()
    object_classes = {value.lower() for value in attributes.get("objectClass", [])}
    mapping = {
        "zimbrapop3datasource": "pop3",
        "zimbraimapdatasource": "imap",
        "zimbracaldavdatasource": "caldav",
        "zimbrayabdatasource": "yab",
        "zimbrarssdatasource": "rss",
        "zimbralivedatasource": "live",
        "zimbragaldatasource": "gal",
    }
    return next(
        (kind for class_name, kind in mapping.items() if class_name in object_classes), None
    )


def _remap_reference(value: str, normalized_mapping: dict[str, str]) -> str:
    return ZIMBRA_ID.sub(
        lambda match: normalized_mapping.get(match.group(0).lower(), match.group(0)),
        value,
    )


def _error_summary(error: Exception) -> str:
    value = " ".join(str(error).split())[:2000]
    if SENSITIVE_ATTRIBUTE.search(value):
        return type(error).__name__
    return value or type(error).__name__


def _summary_counts(rows: list[dict[str, object]], *, prefix: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        phase = str(row["phase"])
        if phase.startswith(prefix) and row["status"] == "success":
            result[phase] = int(row["count"])
    return result
