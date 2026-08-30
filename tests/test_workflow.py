from __future__ import annotations

import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from zimigrate.archive import MigrationArchive
from zimigrate.config import (
    AppConfig,
    EndpointConfig,
    ImportConfig,
    TransferConfig,
)
from zimigrate.errors import CommandError, ZimigrateError
from zimigrate.exporter import Exporter
from zimigrate.importer import Importer
from zimigrate.models import EntityRecord
from zimigrate.scope import apply_scope_to_transfer, parse_target_scope
from zimigrate.target_verifier import TargetVerifier


class WorkflowTests(unittest.TestCase):
    def test_non_strict_attribute_rejections_remain_warnings_during_verification(self) -> None:
        class UnsupportedAttributeSource(FakeSource):
            def get_account(self, name: str) -> dict[str, list[str]]:
                attributes = super().get_account(name)
                attributes["zimbraUnsupportedOnTarget"] = ["source-value"]
                return attributes

        class UnsupportedAttributeTarget(FakeTarget):
            def modify(
                self,
                kind: str,
                name: str,
                operations: list[str],
                *,
                sensitive: bool,
            ) -> None:
                if "zimbraUnsupportedOnTarget" in operations:
                    raise CommandError(
                        "target rejected attribute",
                        attribute_rejection=True,
                    )
                super().modify(kind, name, operations, sensitive=sensitive)

        with tempfile.TemporaryDirectory() as directory:
            base = _config()
            config = replace(
                base,
                import_options=replace(base.import_options, strict_attributes=False),
            )
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = UnsupportedAttributeSource()
            exporter.run()

            target = UnsupportedAttributeTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target
            importer.run()

            verifier = TargetVerifier(config, archive)
            verifier.client = target
            self.assertEqual(verifier.run()["mismatches"], 0)
            warning_report = archive.root / "reports" / "import-warnings.ndjson"
            self.assertIn("zimbraUnsupportedOnTarget", warning_report.read_text(encoding="utf-8"))

            target.objects[("account", "user@example.com")]["displayName"] = ["Changed"]
            drift_verifier = TargetVerifier(config, archive)
            drift_verifier.client = target
            with self.assertRaises(ZimigrateError):
                drift_verifier.run()

    def test_remote_mailhost_capacity_requires_explicit_operator_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = _config()
            config = replace(
                base,
                import_options=replace(
                    base.import_options,
                    default_mailhost="remote-mailbox.example.com",
                ),
            )
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            importer = Importer(config, archive)
            importer.client = FakeTarget(Path(directory))

            with self.assertRaisesRegex(ZimigrateError, "cannot be measured"):
                importer._check_import_capacity([])

    def test_data_source_is_disabled_until_all_attributes_are_restored(self) -> None:
        class DataSourceTarget(FakeTarget):
            def __init__(self, storage_path: Path) -> None:
                super().__init__(storage_path)
                self.created_data_sources: list[tuple[str, str, str, str, str]] = []
                self.data_source_modifications: list[tuple[str, str, list[str]]] = []

            def create_data_source(
                self,
                account: str,
                source_type: str,
                name: str,
                enabled: str,
                folder_id: str,
            ) -> None:
                self.created_data_sources.append((account, source_type, name, enabled, folder_id))

            def modify_data_source(
                self,
                account: str,
                name: str,
                operations: list[str],
                *,
                sensitive: bool,
            ) -> None:
                del sensitive
                self.data_source_modifications.append((account, name, operations))

        with tempfile.TemporaryDirectory() as directory:
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            importer = Importer(_config(), archive)
            target = DataSourceTarget(Path(directory))
            importer.client = target
            record = EntityRecord(
                kind="account",
                name="user@example.com",
                attributes={},
                data_sources=[
                    {
                        "zimbraDataSourceName": ["remote"],
                        "zimbraDataSourceType": ["imap"],
                        "zimbraDataSourceEnabled": ["TRUE"],
                        "zimbraDataSourceFolderId": ["42"],
                        "zimbraDataSourcePassword": ["secret"],
                    }
                ],
            )

            importer._import_data_sources(record)

            self.assertEqual(
                target.created_data_sources,
                [("user@example.com", "imap", "remote", "FALSE", "42")],
            )
            self.assertNotIn(
                "zimbraDataSourceEnabled",
                target.data_source_modifications[0][2],
            )
            self.assertEqual(
                target.data_source_modifications[-1],
                (
                    "user@example.com",
                    "remote",
                    ["zimbraDataSourceEnabled", "TRUE"],
                ),
            )

    def test_minimal_export_import_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            with self.assertLogs("zimigrate.exporter", level="INFO") as export_logs:
                exporter.run()
            export_messages = "\n".join(export_logs.output)
            self.assertIn("Discovering source inventory", export_messages)
            self.assertIn("Querying mailbox quota usage on old.example.com", export_messages)
            self.assertIn("Exporting mailbox user@example.com (full)", export_messages)
            self.assertIn("Export finished successfully", export_messages)
            exported_account = archive.read_entity("account", "user@example.com")
            self.assertGreater(exported_account.artifacts[0].unpacked_size, 0)
            self.assertEqual(list(archive.iter_entities("global_config")), [])
            self.assertEqual(list(archive.iter_entities("server")), [])

            target = FakeTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target
            with self.assertLogs("zimigrate.importer", level="INFO") as import_logs:
                importer.run()
            import_messages = "\n".join(import_logs.output)
            self.assertIn("Importing account user@example.com", import_messages)
            self.assertIn("Importing mailbox user@example.com (full)", import_messages)
            self.assertIn("Import finished successfully", import_messages)
            verification = TargetVerifier(config, archive)
            verification.client = target
            self.assertEqual(verification.run()["mismatches"], 0)
            first_mutation_count = len(target.mutations)
            importer.run()

            self.assertEqual(target.mailbox_imports, 1)
            self.assertEqual(target.mailbox_statuses, [["maintenance"]])
            self.assertIn("member@example.com", target.members["team@example.com"])
            self.assertTrue(any("userPassword" in operation for operation in target.mutations))
            self.assertIn(("account", "user@example.com"), target.cache_flushes)
            self.assertEqual(
                target.objects[("account", "user@example.com")].get("zimbraCOSId"),
                ["target-cos"],
            )
            self.assertEqual(len(target.mutations), first_mutation_count)

            target.objects[("account", "user@example.com")]["displayName"] = ["Changed"]
            mismatched_verification = TargetVerifier(config, archive)
            mismatched_verification.client = target
            with self.assertRaises(ZimigrateError):
                mismatched_verification.run()

    def test_export_resume_rebuilds_a_tampered_successful_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            exporter.run()
            relative = archive.entity_relative_path("account", "user@example.com")
            path = archive.root / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("Test User", "Tampered"),
                encoding="utf-8",
            )

            resumed = Exporter(config, archive)
            resumed.client = FakeSource()
            resumed.run()

            self.assertEqual(
                archive.read_entity("account", "user@example.com").attributes["displayName"],
                ["Test User"],
            )

    def test_export_resume_rejects_a_changed_source_version(self) -> None:
        class UpgradedSource(FakeSource):
            def preflight(self, *, require_mailbox: bool = False, **_: object) -> str:
                if not require_mailbox:
                    raise AssertionError("mailbox preflight was not requested")
                return "Release 10.1.18.GA FOSS"

        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            exporter.run()
            resumed = Exporter(config, archive)
            resumed.client = UpgradedSource()

            with self.assertRaisesRegex(ZimigrateError, "version changed"):
                resumed.run()

            self.assertTrue(archive.manifest()["completed"])

    def test_signature_ids_are_remapped_on_import_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = _SignatureSource()
            exporter.run()

            target = _SignatureTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target
            importer.run()

            account = target.objects[("account", "user@example.com")]
            self.assertEqual(
                account.get("zimbraPrefDefaultSignatureId"),
                ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
            )
            self.assertEqual(
                target.get_identities("user@example.com")[0].get("zimbraPrefDefaultSignatureId"),
                ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
            )
            self.assertNotIn("zimbraPrefMailSignatureContactId", account)
            verification = TargetVerifier(config, archive)
            verification.client = target
            self.assertEqual(verification.run()["mismatches"], 0)

    def test_incomplete_export_cannot_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            archive.write_manifest("Release 10.1.18.GA FOSS", completed=False)
            importer = Importer(config, archive)
            importer.client = FakeTarget(Path(directory))
            with self.assertRaises(ZimigrateError) as ctx:
                importer.run()
            self.assertIn("incomplete", str(ctx.exception).casefold())

    def test_pending_activation_resume_does_not_reimport_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            exporter.run()

            target = FakeTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target
            importer.run()
            mailbox_imports = target.mailbox_imports
            archive.state.succeed(
                "import:account-complete",
                "user@example.com",
                detail="pending-activation",
            )
            target.objects[("account", "user@example.com")]["zimbraAccountStatus"] = ["maintenance"]
            importer.run()
            self.assertEqual(target.mailbox_imports, mailbox_imports)
            self.assertEqual(
                target.objects[("account", "user@example.com")]["zimbraAccountStatus"],
                ["active"],
            )
            completed = archive.state.get("import:account-complete", "user@example.com")
            assert completed is not None
            self.assertIsNone(completed.detail)

    def test_account_metadata_failure_keeps_export_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            source = FakeSource()
            source.get_signatures = lambda _account: (_ for _ in ()).throw(
                RuntimeError("signature lookup failed")
            )
            exporter = Exporter(config, archive)
            exporter.client = source

            with self.assertRaises(ZimigrateError):
                exporter.run()

            self.assertFalse(archive.manifest()["completed"])
            state = archive.state.get("export:account", "user@example.com")
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status, "failed")

    def test_reset_resolution_only_resets_the_first_mailbox_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = _config()
            config = replace(
                base,
                transfer=replace(
                    base.transfer,
                    mailbox_mode="year-chunks",
                    mailbox_start_year=2020,
                    mailbox_chunk_years=5,
                ),
                import_options=replace(
                    base.import_options,
                    mailbox_conflict_resolution="reset",
                ),
            )
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            exporter.run()
            target = FakeTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target

            importer.run()

            self.assertGreater(len(target.mailbox_resolutions), 1)
            self.assertEqual(target.mailbox_resolutions[0], "reset")
            self.assertTrue(all(value == "skip" for value in target.mailbox_resolutions[1:]))

    def test_user_scope_exports_one_account_and_import_can_select_from_full_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _TwoAccountSource()
            config = _config()
            archive = MigrationArchive(Path(directory) / "full", create=True)
            exporter = Exporter(config, archive)
            exporter.client = source
            exporter.run()
            self.assertEqual(
                sorted(record.name for record in archive.iter_entities("account")),
                ["other@example.com", "user@example.com"],
            )

            scoped = replace(
                config,
                transfer=apply_scope_to_transfer(
                    config.transfer, parse_target_scope(["user@example.com"], [])
                ),
            )
            target = FakeTarget(Path(directory))
            importer = Importer(scoped, archive)
            importer.client = target
            importer.run()
            self.assertIn(("account", "user@example.com"), target.objects)
            self.assertNotIn(("account", "other@example.com"), target.objects)

            backup = replace(
                config,
                transfer=apply_scope_to_transfer(
                    config.transfer, parse_target_scope(["user@example.com"], [])
                ),
            )
            backup_archive = MigrationArchive(Path(directory) / "user-backup", create=True)
            backup_exporter = Exporter(backup, backup_archive)
            backup_exporter.client = source
            backup_exporter.run()
            self.assertEqual(
                [record.name for record in backup_archive.iter_entities("account")],
                ["user@example.com"],
            )
            self.assertFalse(list(backup_archive.iter_entities("distribution_list")))

    def test_domain_scope_includes_alias_domains_of_the_selected_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                _config(),
                transfer=apply_scope_to_transfer(
                    _config().transfer, parse_target_scope([], ["example.com"])
                ),
            )
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = _AliasDomainSource()
            exporter.run()
            self.assertEqual(
                sorted(record.name for record in archive.iter_entities("domain")),
                ["alias.example.com", "example.com"],
            )

    def test_apply_invalidates_cached_destination_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            target = _CopyingTarget(Path(directory))
            target.objects[("account", "user@example.com")] = {
                "zimbraId": ["target-user"],
                "zimbraMailHost": ["stale-host.example.com"],
            }
            importer = Importer(config, archive)
            importer.client = target
            snapshot = importer._existing_destination_attributes("account", "user@example.com")
            assert snapshot is not None
            self.assertEqual(snapshot.get("zimbraMailHost"), ["stale-host.example.com"])
            target.objects[("account", "user@example.com")]["zimbraMailHost"] = [
                "live-host.example.com"
            ]
            importer._apply("account", "user@example.com", {"displayName": ["Updated"]})
            refreshed = importer._destination_object_attributes("account", "user@example.com")
            self.assertEqual(refreshed.get("zimbraMailHost"), ["live-host.example.com"])
            self.assertEqual(refreshed.get("displayName"), ["Updated"])

    def test_account_activation_flushes_cache_on_destination_mailhost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = _config()
            config = replace(
                base,
                import_options=replace(
                    base.import_options, default_mailhost="mailbox-1.example.com"
                ),
            )
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            exporter.run()
            target = FakeTarget(Path(directory))
            target.objects[("server", "mailbox-1.example.com")] = {"zimbraId": ["target-server"]}
            importer = Importer(config, archive)
            importer.client = target
            importer.run()
            self.assertEqual(target.cache_flush_servers, ["mailbox-1.example.com"])

    def test_cache_flush_failure_returns_account_to_maintenance(self) -> None:
        class FailingFlushTarget(FakeTarget):
            def flush_cache(
                self,
                cache_type: str,
                name: str,
                *,
                server: str | None = None,
            ) -> None:
                del cache_type, name, server
                raise ZimigrateError("cache flush failed")

        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = FakeSource()
            exporter.run()
            target = FailingFlushTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target

            with self.assertRaises(ZimigrateError):
                importer.run()

            self.assertEqual(
                target.objects[("account", "user@example.com")]["zimbraAccountStatus"],
                ["maintenance"],
            )

    def test_domain_default_cos_is_remapped_to_destination_id(self) -> None:
        class DomainCosSource(FakeSource):
            def get_domain(self, name: str) -> dict[str, list[str]]:
                attributes = super().get_domain(name)
                attributes["zimbraDomainDefaultCOSId"] = ["source-cos"]
                return attributes

        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", create=True)
            exporter = Exporter(config, archive)
            exporter.client = DomainCosSource()
            exporter.run()
            target = FakeTarget(Path(directory))
            importer = Importer(config, archive)
            importer.client = target
            importer.run()
            self.assertEqual(
                target.objects[("domain", "example.com")].get("zimbraDomainDefaultCOSId"),
                ["target-cos"],
            )


class FakeSource:
    def preflight(self, *, require_mailbox: bool = False, **_: object) -> str:
        if not require_mailbox:
            raise AssertionError("mailbox preflight was not requested")
        return "Release 8.8.15.GA FOSS"

    def hostname(self) -> str:
        return "old.example.com"

    def list_servers(self) -> list[str]:
        return ["old.example.com"]

    def get_server(self, name: str) -> dict[str, list[str]]:
        return {
            "name": [name],
            "zimbraServiceEnabled": ["mailbox", "mta", "ldap"],
            "zimbraServiceInstalled": ["mailbox", "mta", "ldap"],
        }

    def get_quota_usage(self, server: str) -> dict[str, int]:
        del server
        return {"user@example.com": 1024}

    def list_domains(self) -> list[str]:
        return ["example.com"]

    def list_cos(self) -> list[str]:
        return ["default"]

    def list_accounts(self) -> list[str]:
        return ["user@example.com"]

    def list_calendar_resources(self) -> list[str]:
        return []

    def list_distribution_lists(self) -> list[str]:
        return ["team@example.com"]

    def get_domain(self, name: str) -> dict[str, list[str]]:
        return {"name": [name], "zimbraId": ["source-domain"]}

    def get_cos(self, name: str) -> dict[str, list[str]]:
        return {"name": [name], "zimbraId": ["source-cos"], "zimbraMailQuota": ["0"]}

    def get_account(self, name: str) -> dict[str, list[str]]:
        return {
            "name": [name],
            "zimbraId": ["source-user"],
            "zimbraCOSId": ["source-cos"],
            "zimbraAccountStatus": ["active"],
            "displayName": ["Test User"],
            "userPassword": ["{SSHA}hash"],
        }

    def get_identities(self, account: str) -> list[dict[str, list[str]]]:
        return []

    def get_signatures(self, account: str) -> list[dict[str, list[str]]]:
        return []

    def get_data_sources(self, account: str) -> list[dict[str, list[str]]]:
        return []

    def export_mailbox(
        self,
        account: str,
        query: str,
        output_path: Path,
        mailbox_host: str | None = None,
        archive_format: str = "tgz",
        lock_mailbox: bool = True,
    ) -> None:
        del account, query, mailbox_host
        if not lock_mailbox:
            raise AssertionError("mailbox locking unexpectedly disabled")
        self.assert_archive_format(archive_format)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Inbox/message.eml", b"Subject: test\n\nbody\n")

    @staticmethod
    def assert_archive_format(archive_format: str) -> None:
        if archive_format != "zip":
            raise AssertionError(f"unexpected archive format: {archive_format}")

    def get_distribution_list(self, name: str) -> dict[str, list[str]]:
        return {
            "name": [name],
            "zimbraId": ["source-list"],
            "zimbraIsDynamicGroup": ["FALSE"],
        }

    def get_distribution_list_members(self, name: str) -> list[str]:
        return ["member@example.com"]


class _TwoAccountSource(FakeSource):
    def list_accounts(self) -> list[str]:
        return ["user@example.com", "other@example.com"]

    def get_quota_usage(self, server: str) -> dict[str, int]:
        del server
        return {"user@example.com": 1024, "other@example.com": 2048}

    def exists(self, kind: str, name: str) -> bool:
        if kind == "account":
            return name in self.list_accounts()
        if kind == "calendar_resource":
            return False
        return name in self.list_domains()


class FakeTarget:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self.objects: dict[tuple[str, str], dict[str, list[str]]] = {
            ("cos", "default"): {"zimbraId": ["target-cos"]}
        }
        self.members: dict[str, set[str]] = {}
        self.mutations: list[list[str]] = []
        self.mailbox_imports = 0
        self.mailbox_resolutions: list[str] = []
        self.mailbox_statuses: list[list[str]] = []
        self.cache_flushes: list[tuple[str, str]] = []
        self.cache_flush_servers: list[str | None] = []

    def get_current_volume_paths(self) -> dict[str, list[Path]]:
        return {
            "primaryMessage": [self.storage_path],
            "index": [self.storage_path],
        }

    def hostname(self) -> str:
        return "mailbox-1.example.com"

    def preflight(self, *, require_mailbox: bool = False, **_: object) -> str:
        del require_mailbox
        return "Release 10.1.18.GA FOSS"

    def exists(self, kind: str, name: str) -> bool:
        normalized = "distribution_list" if kind == "dynamic_distribution_list" else kind
        return (normalized, name) in self.objects

    def get_optional(self, kind: str, name: str) -> dict[str, list[str]] | None:
        normalized = "distribution_list" if kind == "dynamic_distribution_list" else kind
        return self.objects.get((normalized, name))

    def create(self, kind: str, name: str, initial_operations: list[str] | None = None) -> None:
        normalized = "distribution_list" if kind == "dynamic_distribution_list" else kind
        attributes: dict[str, list[str]] = {"zimbraId": [f"target-{len(self.objects)}"]}
        self.objects[(normalized, name)] = attributes
        if initial_operations:
            self.mutations.append(initial_operations)
            for attribute, value in zip(
                initial_operations[::2], initial_operations[1::2], strict=True
            ):
                key = attribute.lstrip("+")
                if attribute.startswith("+"):
                    attributes.setdefault(key, []).append(value)
                else:
                    attributes[key] = [value]

    def create_alias_domain(self, alias_domain: str, target_domain: str) -> None:
        del target_domain
        self.create("domain", alias_domain)

    def modify(self, kind: str, name: str, operations: list[str], *, sensitive: bool) -> None:
        del sensitive
        self.mutations.append(operations)
        normalized = "distribution_list" if kind == "dynamic_distribution_list" else kind
        attributes = self.objects[(normalized, name)]
        for attribute, value in zip(operations[::2], operations[1::2], strict=True):
            key = attribute.lstrip("+")
            if attribute.startswith("+"):
                attributes.setdefault(key, []).append(value)
            else:
                attributes[key] = [value]

    def get_cos(self, name: str) -> dict[str, list[str]]:
        return self.objects[("cos", name)]

    def get_domain(self, name: str) -> dict[str, list[str]]:
        return self.objects[("domain", name)]

    def get_account(self, name: str) -> dict[str, list[str]]:
        return self.objects[("account", name)]

    def get_calendar_resource(self, name: str) -> dict[str, list[str]]:
        return self.objects[("calendar_resource", name)]

    def get_distribution_list(self, name: str) -> dict[str, list[str]]:
        return self.objects[("distribution_list", name)]

    def add_account_alias(self, account: str, alias: str) -> None:
        self.objects[("account", account)].setdefault("zimbraMailAlias", []).append(alias)

    def import_mailbox(
        self,
        account: str,
        path: Path,
        resolution: str,
        mailbox_host: str | None = None,
        archive_format: str = "tgz",
    ) -> None:
        del path, mailbox_host
        if archive_format != "zip":
            raise AssertionError(f"unexpected archive format: {archive_format}")
        self.mailbox_statuses.append(
            list(self.objects[("account", account)].get("zimbraAccountStatus", []))
        )
        self.mailbox_imports += 1
        self.mailbox_resolutions.append(resolution)

    def get_signatures(self, account: str) -> list[dict[str, list[str]]]:
        return []

    def get_identities(self, account: str) -> list[dict[str, list[str]]]:
        return []

    def get_data_sources(self, account: str) -> list[dict[str, list[str]]]:
        return []

    def add_distribution_alias(self, distribution_list: str, alias: str) -> None:
        self.objects[("distribution_list", distribution_list)].setdefault(
            "zimbraMailAlias", []
        ).append(alias)

    def get_distribution_list_members(self, name: str) -> list[str]:
        return sorted(self.members.get(name, set()))

    def add_distribution_member(self, distribution_list: str, member: str) -> None:
        self.members.setdefault(distribution_list, set()).add(member)

    def flush_cache(self, cache_type: str, name: str, *, server: str | None = None) -> None:
        self.cache_flushes.append((cache_type, name))
        self.cache_flush_servers.append(server)


class _CopyingTarget(FakeTarget):
    def get_optional(self, kind: str, name: str) -> dict[str, list[str]] | None:
        attributes = super().get_optional(kind, name)
        if attributes is None:
            return None
        return {key: list(values) for key, values in attributes.items()}

    def get_account(self, name: str) -> dict[str, list[str]]:
        attributes = super().get_account(name)
        return {key: list(values) for key, values in attributes.items()}


class _AliasDomainSource(FakeSource):
    def list_domains(self) -> list[str]:
        return ["example.com", "alias.example.com", "other.com"]

    def get_domain(self, name: str) -> dict[str, list[str]]:
        if name == "alias.example.com":
            return {
                "name": [name],
                "zimbraDomainType": ["alias"],
                "zimbraDomainAliasTargetId": ["source-domain"],
            }
        if name == "other.com":
            return {"name": [name], "zimbraId": ["other-domain"]}
        return super().get_domain(name)


class _SignatureSource(FakeSource):
    def get_account(self, name: str) -> dict[str, list[str]]:
        attributes = super().get_account(name)
        attributes["zimbraPrefDefaultSignatureId"] = ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
        attributes["zimbraPrefMailSignatureContactId"] = ["cccccccc-cccc-cccc-cccc-cccccccccccc"]
        return attributes

    def get_signatures(self, account: str) -> list[dict[str, list[str]]]:
        del account
        return [
            {
                "zimbraSignatureName": ["Work"],
                "zimbraSignatureId": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
                "zimbraPrefMailSignature": ["Thanks"],
            }
        ]

    def get_identities(self, account: str) -> list[dict[str, list[str]]]:
        del account
        return [
            {
                "zimbraPrefIdentityName": ["Work"],
                "zimbraIdentityId": ["dddddddd-dddd-dddd-dddd-dddddddddddd"],
                "zimbraPrefDefaultSignatureId": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            }
        ]


class _SignatureTarget(FakeTarget):
    def __init__(self, storage_path: Path) -> None:
        super().__init__(storage_path)
        self.signatures: dict[str, list[dict[str, list[str]]]] = {}
        self.identities: dict[str, list[dict[str, list[str]]]] = {}

    def create_signature(self, account: str, name: str) -> None:
        self.signatures.setdefault(account, []).append(
            {
                "zimbraSignatureName": [name],
                "zimbraSignatureId": ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
            }
        )

    def modify_signature(
        self, account: str, name: str, operations: list[str], *, sensitive: bool
    ) -> None:
        del sensitive
        self.mutations.append(operations)
        _apply_section_operations(
            self.signatures.get(account, []), "zimbraSignatureName", name, operations
        )

    def get_signatures(self, account: str) -> list[dict[str, list[str]]]:
        return self.signatures.get(account, [])

    def create_identity(self, account: str, name: str) -> None:
        self.identities.setdefault(account, []).append(
            {
                "zimbraPrefIdentityName": [name],
                "zimbraIdentityId": ["eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"],
            }
        )

    def modify_identity(
        self, account: str, name: str, operations: list[str], *, sensitive: bool
    ) -> None:
        del sensitive
        self.mutations.append(operations)
        _apply_section_operations(
            self.identities.get(account, []), "zimbraPrefIdentityName", name, operations
        )

    def get_identities(self, account: str) -> list[dict[str, list[str]]]:
        return self.identities.get(account, [])


def _apply_section_operations(
    sections: list[dict[str, list[str]]], name_attribute: str, name: str, operations: list[str]
) -> None:
    for section in sections:
        if section.get(name_attribute) == [name]:
            for attribute, value in zip(operations[::2], operations[1::2], strict=True):
                section[attribute.lstrip("+")] = [value]
            return


def _config() -> AppConfig:
    return AppConfig(
        source=EndpointConfig(),
        target=EndpointConfig(),
        transfer=TransferConfig(workers=2),
        import_options=ImportConfig(),
    )


if __name__ == "__main__":
    unittest.main()
