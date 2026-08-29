from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import replace

from zimigrate.archive import MigrationArchive
from zimigrate.attributes import SIGNATURE_REFERENCE_ATTRIBUTES, first, mutable_attributes
from zimigrate.config import AppConfig
from zimigrate.data_source import decrypt_data_source_secrets
from zimigrate.errors import CompatibilityError, ZimigrateError
from zimigrate.models import Attributes, EntityRecord
from zimigrate.scope import (
    filter_cos_records,
    filter_distribution_records,
    filter_domain_records,
    parse_bound_scope,
    scope_from_transfer,
)
from zimigrate.util import atomic_json, ensure_relative_path
from zimigrate.zimbra import ZimbraClient, required_verification_commands

ZIMBRA_ID = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)


class TargetVerifier:
    def __init__(self, config: AppConfig, archive: MigrationArchive) -> None:
        self.config = config
        self.archive = archive
        self.client = ZimbraClient(
            config.target,
            retries=config.transfer.retries,
            retry_base_seconds=config.transfer.retry_base_seconds,
        )
        self.mismatches: list[dict[str, str]] = []
        self.checked = 0
        self.skipped = 0
        self._existence_cache: dict[tuple[str, str], bool] = {}
        self._attribute_cache: dict[tuple[str, str], Attributes] = {}
        self._import_binding: dict[str, object] = {}

    def run(self) -> dict[str, int]:
        manifest = self.archive.manifest()
        if not manifest.get("completed"):
            raise ZimigrateError("The export is incomplete; target verification cannot run")
        self._import_binding = _bound_import_options(self.archive)
        selected = _imported_categories(self._import_binding)
        verification_transfer = replace(
            self.config.transfer,
            **{
                field: selected[field]
                for field in (
                    "include_cos",
                    "include_domains",
                    "include_accounts",
                    "include_mailboxes",
                    "include_distribution_lists",
                )
            },
        )
        version = self.client.preflight(
            required_provisioning_commands=required_verification_commands(verification_transfer)
        )
        if not self.config.import_options.allows_version(version):
            raise CompatibilityError(
                f"Target version '{version}' does not match required pattern "
                f"'{self.config.import_options.expected_target_version_pattern}'"
            )
        cos_records = (
            list(self.archive.iter_entities("cos")) if selected.get("include_cos", True) else []
        )
        domain_records = (
            list(self.archive.iter_entities("domain"))
            if selected.get("include_domains", True)
            else []
        )
        account_records = (
            [
                *self.archive.iter_entities("account"),
                *self.archive.iter_entities("calendar_resource"),
            ]
            if selected.get("include_accounts", True)
            else []
        )
        distribution_records = (
            [
                *self.archive.iter_entities("distribution_list"),
                *self.archive.iter_entities("dynamic_distribution_list"),
            ]
            if selected.get("include_distribution_lists", True)
            else []
        )
        scope = scope_from_transfer(self.config.transfer)
        if not scope.active:
            bound = self.archive.state.get("import:configuration", "options")
            if bound and bound.status == "success":
                scope = parse_bound_scope(bound.detail)
        if scope.active:
            account_records = [
                record for record in account_records if scope.matches_account(record.name)
            ]
            domain_records = filter_domain_records(domain_records, scope)
            distribution_records = filter_distribution_records(distribution_records, scope)
            cos_records = filter_cos_records(
                cos_records, accounts=account_records, domains=domain_records, scope=scope
            )
        import_started = self.archive.state.is_success("import:configuration", "options")
        all_records = [*cos_records, *domain_records, *account_records, *distribution_records]
        object_mapping = self._target_id_mapping(all_records, import_started=import_started)

        for record in cos_records:
            if import_started and not self._was_imported(record):
                continue
            attributes = self._verify_existence(record)
            if attributes is not None and not self._was_skipped(record):
                self._verify_attributes(record, attributes, object_mapping)
        for record in domain_records:
            if import_started and not self._was_imported(record):
                continue
            attributes = self._verify_existence(record)
            if attributes is not None and not self._was_skipped(record):
                self._verify_aliases(record, attributes)
                self._verify_attributes(record, attributes, object_mapping)
                source_cos = first(record.attributes, "zimbraDomainDefaultCOSId")
                if source_cos and first(
                    attributes, "zimbraDomainDefaultCOSId"
                ) != object_mapping.get(source_cos):
                    self._mismatch(record, "default_cos", "destination COS does not match")

        for record in account_records:
            if import_started and not self._was_imported(record):
                continue
            attributes = self._verify_existence(record)
            if attributes is None or self._was_skipped(record):
                continue
            self._verify_aliases(record, attributes)
            signature_mapping = self._verify_account_sections(record)
            self._verify_attributes(
                record,
                attributes,
                {**object_mapping, **signature_mapping},
            )
            source_status = first(record.attributes, "zimbraAccountStatus")
            if source_status is not None and source_status != first(
                attributes, "zimbraAccountStatus"
            ):
                self._mismatch(record, "zimbraAccountStatus", "destination value does not match")
            source_cos = first(record.attributes, "zimbraCOSId")
            if source_cos and first(attributes, "zimbraCOSId") != object_mapping.get(source_cos):
                self._mismatch(record, "zimbraCOSId", "destination COS does not match")
            source_mailhost = first(record.attributes, "zimbraMailHost")
            mailhost_map = _string_mapping_option(
                self._import_binding,
                "mailhost_map",
                self.config.import_options.mailhost_map,
            )
            default_mailhost = _optional_string_option(
                self._import_binding,
                "default_mailhost",
                self.config.import_options.default_mailhost,
            )
            expected_mailhost = mailhost_map.get(source_mailhost or "", default_mailhost)
            imported = self.archive.state.get(f"import:{record.kind}", record.name)
            if (
                expected_mailhost
                and (imported is None or imported.detail != "merged-existing")
                and first(attributes, "zimbraMailHost") != expected_mailhost
            ):
                self._mismatch(
                    record,
                    "zimbraMailHost",
                    "destination mailbox placement does not match",
                )
            if selected.get("include_mailboxes", True):
                for artifact in record.artifacts:
                    state = self.archive.state.get(
                        "import:mailbox", f"{record.name}:{artifact.label}"
                    )
                    if (
                        not state
                        or state.status != "success"
                        or state.artifact_path != artifact.path
                        or state.checksum != artifact.sha256
                    ):
                        self._mismatch(
                            record,
                            f"mailbox:{artifact.label}",
                            "mailbox import has no successful checkpoint",
                        )

        for record in distribution_records:
            if import_started and not self._was_imported(record):
                continue
            attributes = self._verify_existence(record)
            if attributes is None or self._was_skipped(record):
                continue
            self._verify_aliases(record, attributes)
            self._verify_attributes(record, attributes, object_mapping)
            if record.members:
                destination_members = set(self.client.get_distribution_list_members(record.name))
                missing = set(record.members) - destination_members
                if missing:
                    self._mismatch(
                        record,
                        "members",
                        f"{len(missing)} distribution member(s) are missing",
                    )

        report = {
            "checked": self.checked,
            "skipped_existing": self.skipped,
            "mismatch_count": len(self.mismatches),
            "mismatches": self.mismatches,
        }
        report_path = ensure_relative_path(self.archive.root, "reports/target-verification.json")
        atomic_json(report_path, report)
        os.chmod(report_path, 0o600)
        if self.mismatches:
            raise ZimigrateError(
                f"Target verification found {len(self.mismatches)} mismatch(es); see {report_path}"
            )
        return {
            "checked": self.checked,
            "skipped_existing": self.skipped,
            "mismatches": 0,
        }

    def _verify_existence(self, record: EntityRecord) -> Attributes | None:
        self.checked += 1
        complete = self.archive.state.get("import:account-complete", record.name)
        if complete and complete.detail == "skipped-system-account":
            self.skipped += 1
            return None
        if not self._exists(record):
            self._mismatch(record, "existence", "object does not exist")
            return None
        if self._was_skipped(record):
            self.skipped += 1
            return None
        return self._attributes(record)

    def _verify_aliases(self, record: EntityRecord, attributes: Attributes) -> None:
        missing = set(record.aliases) - set(attributes.get("zimbraMailAlias", []))
        if missing:
            self._mismatch(record, "aliases", f"{len(missing)} alias(es) are missing")

    def _verify_attributes(
        self,
        record: EntityRecord,
        target: Attributes,
        object_mapping: dict[str, str],
    ) -> None:
        expected = mutable_attributes(record.kind, record.attributes)
        for attribute in ("zimbraACE",):
            if values := record.attributes.get(attribute):
                expected[attribute] = values
        if record.kind == "domain" and (values := record.attributes.get("zimbraGalAccountId")):
            expected["zimbraGalAccountId"] = values
        if record.kind in {"account", "calendar_resource"}:
            for attribute in SIGNATURE_REFERENCE_ATTRIBUTES:
                if values := record.attributes.get(attribute):
                    expected[attribute] = values
        self._compare_attributes(
            record,
            expected,
            target,
            object_mapping,
            field_prefix="attribute",
        )

    def _verify_account_sections(self, record: EntityRecord) -> dict[str, str]:
        target_signatures = self.client.get_signatures(record.name)
        signature_mapping = self._verify_sections(
            record,
            record.signatures,
            target_signatures,
            kind="signature",
            name_attribute="zimbraSignatureName",
            id_attribute="zimbraSignatureId",
            value_mapping={},
        )
        self._verify_sections(
            record,
            record.identities,
            self.client.get_identities(record.name),
            kind="identity",
            name_attribute="zimbraPrefIdentityName",
            id_attribute="zimbraIdentityId",
            value_mapping=signature_mapping,
        )
        self._verify_sections(
            record,
            record.data_sources,
            self.client.get_data_sources(record.name),
            kind="data_source",
            name_attribute="zimbraDataSourceName",
            id_attribute="zimbraDataSourceId",
            value_mapping={},
        )
        return signature_mapping

    def _verify_sections(
        self,
        record: EntityRecord,
        source_sections: list[Attributes],
        target_sections: list[Attributes],
        *,
        kind: str,
        name_attribute: str,
        id_attribute: str,
        value_mapping: dict[str, str],
    ) -> dict[str, str]:
        target_by_name = {
            name: section for section in target_sections if (name := first(section, name_attribute))
        }
        id_mapping: dict[str, str] = {}
        for source in source_sections:
            name = first(source, name_attribute)
            target = target_by_name.get(name or "")
            if not name or target is None:
                self._mismatch(record, f"{kind}:{name or '<unnamed>'}", "section is missing")
                continue
            source_id = first(source, id_attribute)
            target_id = first(target, id_attribute)
            if source_id and target_id:
                id_mapping[source_id] = target_id
            comparable_target = (
                decrypt_data_source_secrets(target) if kind == "data_source" else target
            )
            self._compare_attributes(
                record,
                mutable_attributes(kind, source),
                comparable_target,
                value_mapping,
                field_prefix=f"{kind}:{name}",
            )
        return id_mapping

    def _compare_attributes(
        self,
        record: EntityRecord,
        expected: Attributes,
        target: Attributes,
        value_mapping: dict[str, str],
        *,
        field_prefix: str,
    ) -> None:
        normalized_mapping = {
            source_id.casefold(): target_id for source_id, target_id in value_mapping.items()
        }
        for attribute, source_values in expected.items():
            if not source_values or all(value == "" for value in source_values):
                continue
            mapped_values = [_remap_reference(value, normalized_mapping) for value in source_values]
            if Counter(mapped_values) != Counter(target.get(attribute, [])):
                self._mismatch(
                    record,
                    f"{field_prefix}:{attribute}",
                    "destination values do not match",
                )

    def _target_id_mapping(
        self, records: list[EntityRecord], *, import_started: bool
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for record in records:
            if (
                not record.source_id
                or (import_started and not self._was_imported(record))
                or not self._exists(record)
            ):
                continue
            if target_id := first(self._attributes(record), "zimbraId"):
                mapping[record.source_id] = target_id
        return mapping

    def _exists(self, record: EntityRecord) -> bool:
        key = (record.kind, record.name)
        if key not in self._existence_cache:
            attributes = self._lookup_destination(record)
            self._existence_cache[key] = attributes is not None
            if attributes is not None:
                self._attribute_cache[key] = attributes
        return self._existence_cache[key]

    def _attributes(self, record: EntityRecord) -> Attributes:
        key = (record.kind, record.name)
        if key not in self._attribute_cache:
            attributes = self._lookup_destination(record)
            if attributes is None:
                raise ZimigrateError(f"Destination {record.kind} {record.name} does not exist")
            self._attribute_cache[key] = attributes
        return self._attribute_cache[key]

    def _lookup_destination(self, record: EntityRecord) -> Attributes | None:
        optional = getattr(self.client, "get_optional", None)
        if callable(optional):
            return optional(record.kind, record.name)
        if not self.client.exists(record.kind, record.name):
            return None
        if record.kind == "cos":
            return self.client.get_cos(record.name)
        if record.kind == "domain":
            return self.client.get_domain(record.name)
        if record.kind == "calendar_resource":
            return self.client.get_calendar_resource(record.name)
        if record.kind == "account":
            return self.client.get_account(record.name)
        return self.client.get_distribution_list(record.name)

    def _was_skipped(self, record: EntityRecord) -> bool:
        state = self.archive.state.get(f"import:{record.kind}", record.name)
        if state and state.detail == "skipped-existing":
            return True
        complete = self.archive.state.get("import:account-complete", record.name)
        return bool(complete and complete.detail == "skipped-system-account")

    def _was_imported(self, record: EntityRecord) -> bool:
        if self._was_skipped(record):
            return True
        if record.kind in {"account", "calendar_resource"}:
            return self.archive.state.is_success("import:account-complete", record.name)
        return self.archive.state.is_success(f"import:{record.kind}", record.name)

    def _mismatch(self, record: EntityRecord, field: str, reason: str) -> None:
        self.mismatches.append(
            {"kind": record.kind, "name": record.name, "field": field, "reason": reason}
        )


def _remap_reference(value: str, normalized_mapping: dict[str, str]) -> str:
    return ZIMBRA_ID.sub(
        lambda match: normalized_mapping.get(match.group(0).casefold(), match.group(0)),
        value,
    )


def _bound_import_options(archive: MigrationArchive) -> dict[str, object]:
    state = archive.state.get("import:configuration", "options")
    if not state or state.status != "success":
        return {}
    if not state.detail:
        raise ZimigrateError("Import configuration checkpoint is incomplete")
    try:
        options = json.loads(state.detail)
    except json.JSONDecodeError as exc:
        raise ZimigrateError("Import configuration checkpoint is invalid JSON") from exc
    if not isinstance(options, dict):
        raise ZimigrateError("Import configuration checkpoint is not an object")
    return options


def _imported_categories(options: dict[str, object]) -> dict[str, bool]:
    return {
        name: _boolean_option(options, name, True)
        for name in (
            "include_cos",
            "include_domains",
            "include_accounts",
            "include_mailboxes",
            "include_distribution_lists",
        )
    }


def _boolean_option(options: dict[str, object], name: str, fallback: bool) -> bool:
    value = options.get(name, fallback)
    if not isinstance(value, bool):
        raise ZimigrateError(f"Import configuration option is invalid: {name}")
    return value


def _string_mapping_option(
    options: dict[str, object],
    name: str,
    fallback: dict[str, str],
) -> dict[str, str]:
    value = options.get(name, fallback)
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ZimigrateError(f"Import configuration option is invalid: {name}")
    return dict(value)


def _optional_string_option(
    options: dict[str, object],
    name: str,
    fallback: str | None,
) -> str | None:
    value = options.get(name, fallback)
    if value is not None and not isinstance(value, str):
        raise ZimigrateError(f"Import configuration option is invalid: {name}")
    return value
