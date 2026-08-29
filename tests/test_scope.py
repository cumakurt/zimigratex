from __future__ import annotations

import unittest

from zimigrate.config import TransferConfig
from zimigrate.errors import ConfigurationError, ZimigrateError
from zimigrate.models import EntityRecord
from zimigrate.scope import (
    apply_scope_to_transfer,
    filter_cos_records,
    filter_domain_records,
    parse_bound_scope,
    parse_target_scope,
    selected_accounts,
    selected_names,
)


class TargetScopeTests(unittest.TestCase):
    def test_user_scope_includes_that_mailbox_and_its_domain_only(self) -> None:
        scope = parse_target_scope(["Deneme@deneme.com"], [])
        self.assertTrue(scope.matches_account("deneme@deneme.com"))
        self.assertFalse(scope.matches_account("other@deneme.com"))
        self.assertTrue(scope.matches_domain("deneme.com"))
        self.assertFalse(scope.matches_domain("other.com"))
        self.assertFalse(scope.matches_distribution_list("team@deneme.com"))

    def test_domain_scope_includes_accounts_lists_and_alias_domains(self) -> None:
        scope = parse_target_scope([], ["deneme.com"])
        self.assertTrue(scope.matches_account("user@deneme.com"))
        self.assertFalse(scope.matches_account("user@other.com"))
        self.assertTrue(scope.matches_distribution_list("team@deneme.com"))
        domains = filter_domain_records(
            [
                EntityRecord(kind="domain", name="deneme.com", source_id="id-1", attributes={}),
                EntityRecord(
                    kind="domain",
                    name="alias.example.com",
                    source_id="id-2",
                    attributes={
                        "zimbraDomainType": ["alias"],
                        "zimbraDomainAliasTargetId": ["id-1"],
                    },
                ),
                EntityRecord(kind="domain", name="other.com", source_id="id-3", attributes={}),
            ],
            scope,
        )
        self.assertEqual([record.name for record in domains], ["deneme.com", "alias.example.com"])

    def test_comma_separated_values_and_invalid_input_are_rejected(self) -> None:
        scope = parse_target_scope(["a@example.com,b@example.com"], ["@Example.com"])
        self.assertEqual(scope.users, frozenset({"a@example.com", "b@example.com"}))
        self.assertEqual(scope.domains, frozenset({"example.com"}))
        with self.assertRaises(ConfigurationError):
            parse_target_scope(["not-an-email"], [])
        with self.assertRaises(ConfigurationError):
            parse_target_scope([], ["user@example.com"])

    def test_account_include_still_intersects_with_scope(self) -> None:
        transfer = apply_scope_to_transfer(
            TransferConfig(account_include=("user@example.com",), account_exclude=()),
            parse_target_scope([], ["example.com"]),
        )
        self.assertEqual(
            selected_accounts(
                ["user@example.com", "other@example.com", "skip@other.com"],
                transfer,
            ),
            ["user@example.com"],
        )
        self.assertTrue(transfer.include_accounts)
        self.assertTrue(transfer.include_distribution_lists)

    def test_user_scope_omits_distribution_lists(self) -> None:
        transfer = apply_scope_to_transfer(
            TransferConfig(), parse_target_scope(["u@example.com"], [])
        )
        self.assertFalse(transfer.include_distribution_lists)
        self.assertEqual(
            selected_names(
                ["u@example.com", "v@example.com"],
                parse_target_scope(["u@example.com"], []),
                kind="account",
            ),
            ["u@example.com"],
        )

    def test_cos_filter_keeps_referenced_classes_only(self) -> None:
        accounts = [
            EntityRecord(
                kind="account",
                name="user@example.com",
                attributes={"zimbraCOSId": ["cos-id"]},
            )
        ]
        kept = filter_cos_records(
            [
                EntityRecord(kind="cos", name="default", source_id="cos-id", attributes={}),
                EntityRecord(kind="cos", name="other", source_id="other-id", attributes={}),
            ],
            accounts=accounts,
            domains=[],
            scope=parse_target_scope(["user@example.com"], []),
        )
        self.assertEqual([record.name for record in kept], ["default"])

    def test_bound_scope_rejects_corrupt_checkpoint_json(self) -> None:
        with self.assertRaises(ZimigrateError):
            parse_bound_scope("{")
        with self.assertRaises(ZimigrateError):
            parse_bound_scope("[]")
        self.assertFalse(parse_bound_scope(None).active)
        self.assertTrue(
            parse_bound_scope('{"target_users":["user@example.com"]}').matches_account(
                "user@example.com"
            )
        )


if __name__ == "__main__":
    unittest.main()
