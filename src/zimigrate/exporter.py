from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from zimigrate.archive import MigrationArchive, validate_mailbox_archive
from zimigrate.attributes import exportable_attributes, first
from zimigrate.capacity import assess_export_disk, format_bytes
from zimigrate.config import AppConfig
from zimigrate.data_source import decrypt_data_source_secrets
from zimigrate.drain import (
    export_drain_enabled,
    mailbox_missing_after_drain,
    request_mailbox_drain,
)
from zimigrate.errors import Interrupted, ZimigrateError
from zimigrate.interrupt import WorkerPool, bounded_futures
from zimigrate.models import Artifact, Attributes, EntityRecord
from zimigrate.progress import PhaseProgress, entity_start_fields
from zimigrate.scope import scope_from_transfer, selected_accounts, selected_names
from zimigrate.util import atomic_json, ensure_relative_path, open_private_temporary
from zimigrate.zimbra import ZimbraClient, required_export_commands

LOGGER = logging.getLogger(__name__)


class Exporter:
    def __init__(self, config: AppConfig, archive: MigrationArchive) -> None:
        self.config = config
        self.archive = archive
        self.client = ZimbraClient(
            config.source,
            retries=config.transfer.retries,
            retry_base_seconds=config.transfer.retry_base_seconds,
        )
        self._validated_entity_checkpoints: set[tuple[str, str]] = set()

    def run(self) -> dict[str, int]:
        transfer = self.config.transfer
        version = self.client.preflight(
            require_mailbox=transfer.include_mailboxes,
            required_provisioning_commands=required_export_commands(transfer),
            required_mailbox_commands={"getRestURL"} if transfer.include_mailboxes else set(),
            require_mailbox_output=transfer.include_mailboxes,
        )
        source_host = self.client.hostname()
        export_options = self._export_options()
        existing_manifest = self.archive.manifest(optional=True)
        if existing_manifest:
            existing_host = existing_manifest.get("source_host")
            existing_version = existing_manifest.get("source_version")
            existing_options = existing_manifest.get("export_options")
            if existing_host and existing_host != source_host:
                raise ZimigrateError(
                    f"Archive belongs to source {existing_host}, not {source_host}"
                )
            if existing_version and existing_version != version:
                raise ZimigrateError(
                    "Source Zimbra version changed since this export started; "
                    "use a new archive directory"
                )
            if existing_options and existing_options != export_options:
                raise ZimigrateError(
                    "Export options differ from the existing archive; use a new archive directory"
                )
        LOGGER.info(
            "Source preflight passed",
            extra={"event": "preflight", "version": version, "host": source_host},
        )

        LOGGER.info("Discovering source inventory")
        if transfer.include_mailboxes:
            LOGGER.info("Listing source servers")
            servers = self.client.list_servers()
            LOGGER.info(
                "Found %s source server(s)",
                len(servers),
                extra={"event": "inventory", "inventory": {"Servers": len(servers)}},
            )
            LOGGER.info("Reading source server attributes")
            server_attributes = {server: self.client.get_server(server) for server in servers}
        else:
            server_attributes = {}
        if transfer.include_accounts:
            LOGGER.info("Listing calendar resources")
            resources = set(self.client.list_calendar_resources())
            LOGGER.info("Listing accounts")
            accounts = self._discover_accounts(resources)
            LOGGER.info(
                "Selected %s account(s) for export",
                len(accounts),
                extra={"event": "inventory", "inventory": {"Accounts": len(accounts)}},
            )
        else:
            resources = set()
            accounts = []
        self._check_disk_capacity(accounts, server_attributes)

        LOGGER.info("Writing export manifest")
        self.archive.write_manifest(
            version,
            completed=False,
            source_host=source_host,
            export_options=export_options,
        )

        if transfer.include_domains:
            LOGGER.info("Listing domains")
            domains = self._discover_domains()
            LOGGER.info(
                "Found %s domain(s)",
                len(domains),
                extra={"event": "inventory", "inventory": {"Domains": len(domains)}},
            )
        else:
            domains = []
        if transfer.include_cos:
            LOGGER.info("Listing classes of service")
            cos_names = self.client.list_cos()
            LOGGER.info(
                "Found %s class(es) of service",
                len(cos_names),
                extra={"event": "inventory", "inventory": {"COS": len(cos_names)}},
            )
        else:
            cos_names = []
        if transfer.include_distribution_lists:
            LOGGER.info("Listing distribution lists")
            distribution_lists = selected_names(
                self.client.list_distribution_lists(),
                scope_from_transfer(transfer),
                kind="distribution_list",
            )
            LOGGER.info(
                "Found %s distribution list(s)",
                len(distribution_lists),
                extra={
                    "event": "inventory",
                    "inventory": {"Lists": len(distribution_lists)},
                },
            )
        else:
            distribution_lists = []

        self._run_parallel(
            "domain",
            domains,
            lambda name: self._export_entity("domain", name, self.client.get_domain),
        )
        self._run_parallel(
            "cos", cos_names, lambda name: self._export_entity("cos", name, self.client.get_cos)
        )
        self._run_parallel(
            "account",
            accounts,
            lambda name: self._export_account(name, name in resources),
        )
        self._run_parallel("distribution-list", distribution_lists, self._export_distribution_list)

        failed = [
            record for record in self.archive.state.failed() if record.phase.startswith("export:")
        ]
        LOGGER.info("Writing final export manifest")
        self.archive.write_manifest(
            version,
            completed=not failed,
            source_host=source_host,
            export_options=export_options,
        )
        if failed:
            raise ZimigrateError(
                f"Export finished with {len(failed)} failed operation(s); "
                "rerun the same command to resume"
            )
        LOGGER.info("Export finished successfully")
        return _summary_counts(self.archive.state.summary())

    def _check_disk_capacity(
        self,
        accounts: list[str],
        server_attributes: dict[str, Attributes],
    ) -> None:
        completed_accounts = {
            account
            for account in accounts
            if self._valid_entity_checkpoint(
                "export:account",
                "account",
                account,
                account_record=True,
            )
        }
        remaining_accounts = [account for account in accounts if account not in completed_accounts]
        mailbox_usage: dict[str, int] = {}
        LOGGER.info("Checking export disk capacity")
        if self.config.transfer.include_mailboxes and remaining_accounts:
            mailbox_servers = [
                server
                for server, attributes in server_attributes.items()
                if _is_mailbox_server(attributes)
            ]
            if not mailbox_servers:
                raise ZimigrateError(
                    "Disk capacity cannot be estimated because no mailbox-enabled "
                    "source server was discovered"
                )
            for server in mailbox_servers:
                LOGGER.info("Querying mailbox quota usage on %s", server)
                for account, used in self.client.get_quota_usage(server).items():
                    normalized = account.casefold()
                    mailbox_usage[normalized] = max(used, mailbox_usage.get(normalized, 0))
            if not mailbox_usage:
                raise ZimigrateError(
                    "Disk capacity cannot be estimated because Zimbra returned no "
                    "mailbox usage records"
                )

        assessment = assess_export_disk(
            self.archive.root,
            remaining_accounts=remaining_accounts,
            mailbox_usage=mailbox_usage,
            include_mailboxes=self.config.transfer.include_mailboxes,
            workers=self.config.transfer.workers,
            drain_completed_artifacts=export_drain_enabled(),
        )
        report_path = ensure_relative_path(self.archive.root, "reports/export-disk-assessment.json")
        atomic_json(report_path, assessment.as_dict())
        log_fields = {
            "status": assessment.status,
            "free": format_bytes(assessment.filesystem_free_bytes),
            "required": format_bytes(assessment.estimated_required_free_bytes),
            "archive_growth": format_bytes(assessment.estimated_archive_growth_bytes),
            "temporary_peak": format_bytes(assessment.estimated_peak_temporary_bytes),
            "remaining_accounts": assessment.remaining_accounts,
            "report": report_path,
        }
        if assessment.unmeasured_accounts:
            LOGGER.warning(
                "Some mailbox sizes were unavailable; average-size fallback was applied",
                extra={
                    "unmeasured_accounts": assessment.unmeasured_accounts,
                    "estimated_unmeasured": format_bytes(
                        assessment.estimated_unmeasured_mailbox_bytes
                    ),
                },
            )
        if assessment.status == "insufficient":
            LOGGER.error("Export disk capacity is insufficient", extra=log_fields)
            raise ZimigrateError(
                "Insufficient export disk space: "
                f"{format_bytes(assessment.filesystem_free_bytes)} available, "
                f"{format_bytes(assessment.estimated_required_free_bytes)} estimated required. "
                f"See {report_path}"
            )
        if assessment.status == "warning":
            LOGGER.warning("Export disk capacity is close to the safe limit", extra=log_fields)
        else:
            LOGGER.info("Export disk capacity check passed", extra=log_fields)

    def _export_options(self) -> dict[str, object]:
        transfer = self.config.transfer
        return {
            "include_domains": transfer.include_domains,
            "include_cos": transfer.include_cos,
            "include_accounts": transfer.include_accounts,
            "include_mailboxes": transfer.include_mailboxes,
            "include_distribution_lists": transfer.include_distribution_lists,
            "include_system_mailboxes": transfer.include_system_mailboxes,
            "include_secrets": transfer.include_secrets,
            "account_include": list(transfer.account_include),
            "account_exclude": list(transfer.account_exclude),
            "target_users": list(transfer.target_users),
            "target_domains": list(transfer.target_domains),
            "mailbox_mode": transfer.mailbox_mode,
            "mailbox_format": transfer.mailbox_format,
            "mailbox_lock": transfer.mailbox_lock,
            "mailbox_start_year": transfer.mailbox_start_year,
            "mailbox_chunk_years": transfer.mailbox_chunk_years,
        }

    def _discover_accounts(self, resources: set[str]) -> list[str]:
        scope = scope_from_transfer(self.config.transfer)
        if scope.users and not scope.domains:
            discovered: list[str] = []
            for user in sorted(scope.users):
                if user in resources or self.client.exists("account", user):
                    discovered.append(user)
                    continue
                if self.client.exists("calendar_resource", user):
                    resources.add(user)
                    discovered.append(user)
                    continue
                raise ZimigrateError(f"Account not found: {user}")
            selected = selected_accounts(discovered, self.config.transfer)
        else:
            selected = selected_accounts(
                sorted(set(self.client.list_accounts()) | resources),
                self.config.transfer,
            )
        if scope.users and not selected:
            raise ZimigrateError("No accounts matched --user/--domain")
        return selected

    def _discover_domains(self) -> list[str]:
        names = self.client.list_domains()
        scope = scope_from_transfer(self.config.transfer)
        selected = selected_names(names, scope, kind="domain")
        missing = sorted(scope.domains.difference(name.casefold() for name in selected))
        if missing:
            raise ZimigrateError(f"Domain not found: {missing[0]}")
        if not scope.domains:
            return selected
        attributes_by_name = self._get_domains_parallel(names)
        selected_ids: set[str] = set()
        chosen = {name.casefold() for name in selected}
        for name in selected:
            if zimbra_id := first(attributes_by_name[name], "zimbraId"):
                selected_ids.add(zimbra_id)
        for name in names:
            if name.casefold() in chosen:
                continue
            attributes = attributes_by_name[name]
            domain_type = (first(attributes, "zimbraDomainType", "") or "").lower()
            target = first(attributes, "zimbraDomainAliasTargetId")
            if (domain_type == "alias" or target) and target in selected_ids:
                selected.append(name)
                chosen.add(name.casefold())
        return selected

    def _get_domains_parallel(self, names: list[str]) -> dict[str, Attributes]:
        if not names:
            return {}
        results: dict[str, Attributes] = {}
        errors: list[tuple[str, Exception]] = []
        with WorkerPool(self.config.transfer.workers, "export-domain-lookup") as executor:
            for name, future in bounded_futures(
                executor,
                names,
                self.client.get_domain,
                max_pending=self.config.transfer.workers * 2,
            ):
                try:
                    results[name] = future.result()
                except Interrupted:
                    raise
                except Exception as exc:
                    errors.append((name, exc))
        if errors:
            raise ZimigrateError(
                f"{len(errors)} domain lookup(s) failed; first failure: "
                f"{errors[0][0]}: {_error_summary(errors[0][1])}"
            )
        return results

    def _export_entity(self, kind: str, name: str, getter: object) -> None:
        phase = f"export:{kind}"
        if self._valid_entity_checkpoint(phase, kind, name):
            return
        LOGGER.info(
            "Exporting %s %s",
            kind,
            name,
            extra=entity_start_fields(kind, name, action="export"),
        )
        self.archive.state.start(phase, name)
        try:
            attributes = self._sanitize(getter(name))  # type: ignore[operator]
            record = EntityRecord(
                kind=kind,
                name=name,
                source_id=first(attributes, "zimbraId"),
                attributes=attributes,
                aliases=list(attributes.get("zimbraMailAlias", [])),
            )
            relative, checksum = self.archive.write_entity(record)
            self.archive.state.succeed(phase, name, artifact_path=relative, checksum=checksum)
        except Exception as exc:
            self.archive.state.fail(phase, name, _error_summary(exc))
            raise

    def _export_account(self, account: str, known_resource: bool) -> None:
        phase = "export:account"
        if self._valid_entity_checkpoint(phase, "account", account, account_record=True):
            return
        LOGGER.info(
            "Exporting account %s",
            account,
            extra=entity_start_fields("account", account, action="export"),
        )
        self.archive.state.start(phase, account)
        try:
            attributes = (
                self.client.get_calendar_resource(account)
                if known_resource
                else self.client.get_account(account)
            )
            is_resource = known_resource or first(attributes, "zimbraCalResType") is not None
            record = EntityRecord(
                kind="calendar_resource" if is_resource else "account",
                name=account,
                source_id=first(attributes, "zimbraId"),
                attributes=self._sanitize(attributes),
                aliases=list(attributes.get("zimbraMailAlias", [])),
                identities=self._sections(self.client.get_identities, account),
                signatures=self._sections(self.client.get_signatures, account),
                data_sources=self._data_source_sections(account),
            )
            if self._should_export_mailbox(record):
                record.artifacts = self._export_mailbox_artifacts(
                    account, first(attributes, "zimbraMailHost")
                )
            relative, checksum = self.archive.write_entity(record)
            self.archive.state.succeed(phase, account, artifact_path=relative, checksum=checksum)
        except Exception as exc:
            self.archive.state.fail(phase, account, _error_summary(exc))
            raise

    def _sections(self, getter: object, account: str) -> list[Attributes]:
        sections = getter(account)  # type: ignore[operator]
        return [self._sanitize(section) for section in sections]

    def _data_source_sections(self, account: str) -> list[Attributes]:
        sections = self.client.get_data_sources(account)
        if self.config.transfer.include_secrets:
            sections = [decrypt_data_source_secrets(section) for section in sections]
        return [self._sanitize(section) for section in sections]

    def _should_export_mailbox(self, record: EntityRecord) -> bool:
        if not self.config.transfer.include_mailboxes:
            return False
        if self.config.transfer.include_system_mailboxes:
            return True
        system_markers = (
            first(record.attributes, "zimbraIsSystemAccount", "FALSE"),
            first(record.attributes, "zimbraIsSystemResource", "FALSE"),
        )
        return not any(value and value.upper() == "TRUE" for value in system_markers)

    def _export_mailbox_artifacts(self, account: str, mailbox_host: str | None) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for label, query in self._mailbox_queries():
            entity = f"{account}:{label}"
            phase = "export:mailbox"
            state = self.archive.state.get(phase, entity)
            if state and state.status == "success" and state.detail:
                try:
                    detail = json.loads(state.detail)
                    if not isinstance(detail, dict):
                        raise ValueError("mailbox checkpoint is not an object")
                    artifact = Artifact.from_dict(detail)
                    if self._mailbox_artifact_reusable(artifact):
                        artifacts.append(artifact)
                        self._drain_mailbox_if_present(artifact)
                        continue
                    raise ValueError("mailbox artifact is not reusable")
                except Exception:
                    LOGGER.warning(
                        "Checkpointed mailbox artifact is invalid; exporting it again",
                        extra={"account": account, "label": label},
                    )

            LOGGER.info(
                "Exporting mailbox %s (%s)",
                account,
                label,
                extra=entity_start_fields("account", f"{account} ({label})", action="export"),
            )
            self.archive.state.start(phase, entity)
            archive_format = self.config.transfer.mailbox_format
            descriptor, plaintext = open_private_temporary(
                ensure_relative_path(self.archive.root, ".tmp"), f".{archive_format}"
            )
            os.close(descriptor)
            stored: tuple[str, str, int] | None = None
            try:
                self.client.export_mailbox(
                    account,
                    query,
                    plaintext,
                    mailbox_host,
                    archive_format,
                    self.config.transfer.mailbox_lock,
                )
                unpacked_size = validate_mailbox_archive(plaintext, archive_format)
                relative = self.archive.mailbox_relative_path(account, label, archive_format)
                checksum, size = self.archive.store_mailbox(plaintext, relative)
                artifact = Artifact(
                    label=label,
                    path=relative,
                    sha256=checksum,
                    size=size,
                    query=query,
                    archive_format=archive_format,
                    unpacked_size=unpacked_size,
                )
                self.archive.state.succeed(
                    phase,
                    entity,
                    artifact_path=relative,
                    checksum=checksum,
                    detail=json.dumps(artifact.as_dict(), sort_keys=True),
                )
                artifacts.append(artifact)
                stored = (relative, checksum, size)
                LOGGER.info(
                    "Stored mailbox %s (%s)",
                    account,
                    label,
                    extra={"bytes": size, "format": archive_format},
                )
            except Exception as exc:
                self.archive.state.fail(phase, entity, _error_summary(exc))
                raise
            finally:
                plaintext.unlink(missing_ok=True)
            if stored is not None:
                request_mailbox_drain(
                    self.archive.root,
                    relative=stored[0],
                    sha256=stored[1],
                    size=stored[2],
                )
        return artifacts

    def _mailbox_queries(self) -> list[tuple[str, str]]:
        if self.config.transfer.mailbox_mode == "full":
            return [("full", "is:anywhere")]

        current_year = datetime.now(UTC).year
        start_year = self.config.transfer.mailbox_start_year
        interval = self.config.transfer.mailbox_chunk_years
        queries: list[tuple[str, str]] = []
        end = min(start_year + interval, current_year + 1)
        queries.append((f"before-{end}", f"is:anywhere AND date:<{_year_epoch(end)}"))
        start = end
        while start < current_year + 1:
            end = min(start + interval, current_year + 1)
            queries.append(
                (
                    f"{start}-{end}",
                    f"is:anywhere AND date:>={_year_epoch(start)} AND date:<{_year_epoch(end)}",
                )
            )
            start = end
        queries.append(
            (
                f"after-{current_year}",
                f"is:anywhere AND date:>={_year_epoch(current_year + 1)}",
            )
        )
        return queries

    def _export_distribution_list(self, name: str) -> None:
        phase = "export:distribution-list"
        if self._valid_entity_checkpoint(
            phase,
            "distribution_list",
            name,
            distribution_record=True,
        ):
            return
        LOGGER.info(
            "Exporting distribution list %s",
            name,
            extra=entity_start_fields("distribution-list", name, action="export"),
        )
        self.archive.state.start(phase, name)
        try:
            attributes = self.client.get_distribution_list(name)
            is_dynamic = (
                first(attributes, "zimbraIsDynamicGroup", "FALSE") or ""
            ).upper() == "TRUE"
            members = [] if is_dynamic else self.client.get_distribution_list_members(name)
            record = EntityRecord(
                kind="dynamic_distribution_list" if is_dynamic else "distribution_list",
                name=name,
                source_id=first(attributes, "zimbraId"),
                attributes=self._sanitize(attributes),
                aliases=list(attributes.get("zimbraMailAlias", [])),
                members=members,
            )
            relative, checksum = self.archive.write_entity(record)
            self.archive.state.succeed(phase, name, artifact_path=relative, checksum=checksum)
        except Exception as exc:
            self.archive.state.fail(phase, name, _error_summary(exc))
            raise

    def _sanitize(self, attributes: Attributes) -> Attributes:
        return exportable_attributes(
            attributes, include_secrets=self.config.transfer.include_secrets
        )

    def _valid_entity_checkpoint(
        self,
        phase: str,
        kind: str,
        name: str,
        *,
        account_record: bool = False,
        distribution_record: bool = False,
    ) -> bool:
        checkpoint_key = (phase, name)
        if checkpoint_key in self._validated_entity_checkpoints:
            return True
        state = self.archive.state.get(phase, name)
        if state is None or state.status != "success":
            return False
        if not state.artifact_path or not state.checksum:
            LOGGER.warning(
                "Completed entity checkpoint has no artifact identity; exporting it again",
                extra={"kind": kind, "entity": name},
            )
            return False
        candidate_kinds = [kind]
        if account_record:
            candidate_kinds = ["account", "calendar_resource"]
        elif distribution_record:
            candidate_kinds = ["distribution_list", "dynamic_distribution_list"]
        try:
            record = next(
                self.archive.validate_entity_artifact(
                    candidate,
                    name,
                    state.artifact_path,
                    state.checksum,
                )
                for candidate in candidate_kinds
                if state.artifact_path == self.archive.entity_relative_path(candidate, name)
            )
            for artifact in record.artifacts:
                if mailbox_missing_after_drain(self.archive.root, artifact.path):
                    continue
                self.archive.validate_mailbox_artifact(
                    artifact.path,
                    artifact.sha256,
                    deep=False,
                    archive_format=artifact.archive_format,
                    expected_size=artifact.size,
                )
        except Exception as exc:
            LOGGER.warning(
                "Completed entity artifact is invalid; exporting it again",
                extra={"kind": kind, "entity": name, "error": _error_summary(exc)},
            )
            return False
        self._validated_entity_checkpoints.add(checkpoint_key)
        return True

    def _mailbox_artifact_reusable(self, artifact: Artifact) -> bool:
        if mailbox_missing_after_drain(self.archive.root, artifact.path):
            return True
        self.archive.validate_mailbox_artifact(
            artifact.path,
            artifact.sha256,
            deep=False,
            archive_format=artifact.archive_format,
            expected_size=artifact.size,
        )
        return True

    def _drain_mailbox_if_present(self, artifact: Artifact) -> None:
        if not export_drain_enabled():
            return
        if mailbox_missing_after_drain(self.archive.root, artifact.path):
            return
        request_mailbox_drain(
            self.archive.root,
            relative=artifact.path,
            sha256=artifact.sha256,
            size=artifact.size,
        )

    def _run_parallel(self, label: str, names: list[str], operation: object) -> None:
        progress = PhaseProgress(LOGGER, kind=label, total=len(names), action="export")
        errors: list[tuple[str, Exception]] = []
        with WorkerPool(self.config.transfer.workers, f"export-{label}") as executor:
            for name, future in bounded_futures(
                executor,
                names,
                operation,  # type: ignore[arg-type]
                max_pending=self.config.transfer.workers * 2,
            ):
                try:
                    future.result()
                    progress.complete(name)
                except Interrupted:
                    raise
                except Exception as exc:
                    progress.complete(name, failed=True)
                    errors.append((name, exc))
                    LOGGER.error(
                        "Entity export failed",
                        extra={"kind": label, "entity": name, "error": _error_summary(exc)},
                    )
        if errors:
            raise ZimigrateError(
                f"{len(errors)} {label} export(s) failed; first failure: "
                f"{errors[0][0]}: {_error_summary(errors[0][1])}"
            )


def _error_summary(error: Exception) -> str:
    return " ".join(str(error).split())[:2000] or type(error).__name__


def _summary_counts(rows: list[dict[str, object]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        if row["status"] == "success":
            result[str(row["phase"])] = int(row["count"])
    return result


def _is_mailbox_server(attributes: Attributes) -> bool:
    services = {
        value.casefold()
        for attribute in ("zimbraServiceEnabled", "zimbraServiceInstalled")
        for value in attributes.get(attribute, [])
    }
    return "mailbox" in services


def _year_epoch(year: int) -> int:
    return int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000)
