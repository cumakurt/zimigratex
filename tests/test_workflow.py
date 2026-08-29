from __future__ import annotations

import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from zimigrate.archive import MigrationArchive
from zimigrate.config import (
    AppConfig,
    ArchiveConfig,
    EndpointConfig,
    ImportConfig,
    TransferConfig,
)
from zimigrate.errors import ZimigrateError
from zimigrate.exporter import Exporter
from zimigrate.importer import Importer
from zimigrate.scope import apply_scope_to_transfer, parse_target_scope
from zimigrate.target_verifier import TargetVerifier


class WorkflowTests(unittest.TestCase):
    def test_minimal_export_import_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", config.archive, create=True)
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
            self.assertEqual(len(target.mutations), first_mutation_count)

            target.objects[("account", "user@example.com")]["displayName"] = ["Changed"]
            mismatched_verification = TargetVerifier(config, archive)
            mismatched_verification.client = target
            with self.assertRaises(ZimigrateError):
                mismatched_verification.run()

    def test_account_metadata_failure_keeps_export_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            archive = MigrationArchive(Path(directory) / "archive", config.archive, create=True)
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
            archive = MigrationArchive(Path(directory) / "archive", config.archive, create=True)
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
            archive = MigrationArchive(Path(directory) / "full", config.archive, create=True)
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
            backup_archive = MigrationArchive(
                Path(directory) / "user-backup", backup.archive, create=True
            )
            backup_exporter = Exporter(backup, backup_archive)
            backup_exporter.client = source
            backup_exporter.run()
            self.assertEqual(
                [record.name for record in backup_archive.iter_entities("account")],
                ["user@example.com"],
            )
            self.assertFalse(list(backup_archive.iter_entities("distribution_list")))


class FakeSource:
    def preflight(self, *, require_mailbox: bool = False) -> str:
        if not require_mailbox:
            raise AssertionError("mailbox preflight was not requested")
        return "Release 8.8.15.GA FOSS"

    def hostname(self) -> str:
        return "old.example.com"

    def get_global_config(self) -> dict[str, list[str]]:
        return {"zimbraDefaultDomainName": ["example.com"]}

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

    def get_current_volume_paths(self) -> dict[str, list[Path]]:
        return {
            "primaryMessage": [self.storage_path],
            "index": [self.storage_path],
        }

    def preflight(self, *, require_mailbox: bool = False) -> str:
        if not require_mailbox:
            raise AssertionError("mailbox preflight was not requested")
        return "Release 10.1.18.GA FOSS"

    def exists(self, kind: str, name: str) -> bool:
        normalized = "distribution_list" if kind == "dynamic_distribution_list" else kind
        return (normalized, name) in self.objects

    def create(self, kind: str, name: str, initial_operations: list[str] | None = None) -> None:
        normalized = "distribution_list" if kind == "dynamic_distribution_list" else kind
        self.objects[(normalized, name)] = {"zimbraId": [f"target-{len(self.objects)}"]}
        if initial_operations:
            self.mutations.append(initial_operations)

    def create_alias_domain(self, alias_domain: str, target_domain: str) -> None:
        del target_domain
        self.create("domain", alias_domain)

    def modify(self, kind: str, name: str, operations: list[str], *, sensitive: bool) -> None:
        del sensitive
        self.mutations.append(operations)
        if kind == "global_config":
            return
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

    def flush_cache(self, cache_type: str, name: str) -> None:
        self.cache_flushes.append((cache_type, name))


def _config() -> AppConfig:
    return AppConfig(
        source=EndpointConfig(),
        target=EndpointConfig(),
        archive=ArchiveConfig(),
        transfer=TransferConfig(workers=2),
        import_options=ImportConfig(),
    )


if __name__ == "__main__":
    unittest.main()
