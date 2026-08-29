from __future__ import annotations

import unittest

from zimigrate.attributes import (
    apply_attributes_resiliently,
    exportable_attributes,
    flatten_operations,
    mutable_attributes,
)
from zimigrate.errors import CommandError


class AttributeTests(unittest.TestCase):
    def test_mutable_attributes_remove_ids_and_keep_password_hash(self) -> None:
        result = mutable_attributes(
            "account",
            {
                "zimbraId": ["source-id"],
                "zimbraCOSId": ["source-cos"],
                "zimbraAccountStatus": ["active"],
                "displayName": ["Example User"],
                "userPassword": ["{SSHA}hash"],
            },
        )

        self.assertEqual(
            result,
            {"displayName": ["Example User"], "userPassword": ["{SSHA}hash"]},
        )

    def test_identity_signature_prefs_are_mutable_account_prefs_are_deferred(self) -> None:
        self.assertEqual(
            mutable_attributes(
                "account",
                {
                    "displayName": ["Example User"],
                    "zimbraPrefDefaultSignatureId": ["source-sig"],
                },
            ),
            {"displayName": ["Example User"]},
        )
        self.assertEqual(
            mutable_attributes(
                "identity",
                {
                    "zimbraPrefFromAddress": ["user@example.com"],
                    "zimbraPrefDefaultSignatureId": ["source-sig"],
                },
            ),
            {
                "zimbraPrefFromAddress": ["user@example.com"],
                "zimbraPrefDefaultSignatureId": ["source-sig"],
            },
        )

    def test_ldap_binary_transfer_attributes_are_not_applied_through_zmprov(self) -> None:
        result = mutable_attributes(
            "account",
            {
                "displayName": ["Example User"],
                "jpegPhoto": ["/9j/4AAQ"],
                "userCertificate": ["MIIB"],
            },
        )

        self.assertEqual(result, {"displayName": ["Example User"]})

    def test_flatten_operations_replaces_then_adds_multivalues(self) -> None:
        self.assertEqual(
            flatten_operations([("zimbraMailForwardingAddress", ["a@x", "b@x"])]),
            [
                "zimbraMailForwardingAddress",
                "a@x",
                "+zimbraMailForwardingAddress",
                "b@x",
            ],
        )

    def test_live_authentication_tokens_are_never_exported(self) -> None:
        self.assertEqual(
            exportable_attributes(
                {
                    "userPassword": ["{SSHA}hash"],
                    "zimbraAuthTokens": ["active-session"],
                },
                include_secrets=True,
            ),
            {"userPassword": ["{SSHA}hash"]},
        )

    def test_failed_batch_is_bisected_and_bad_attribute_is_reported(self) -> None:
        applied: list[str] = []
        warnings: list[tuple[str, str]] = []

        def apply(operations: list[str], sensitive: bool) -> None:
            del sensitive
            names = operations[::2]
            if any(name.lstrip("+") == "unsupported" for name in names):
                raise RuntimeError("unsupported")
            applied.extend(name.lstrip("+") for name in names)

        apply_attributes_resiliently(
            {"goodA": ["1"], "unsupported": ["2"], "goodB": ["3"]},
            apply,
            lambda name, reason: warnings.append((name, reason)),
            strict=False,
        )

        self.assertCountEqual(applied, ["goodA", "goodB"])
        self.assertEqual(warnings[0][0], "unsupported")

    def test_non_attribute_command_failure_is_never_downgraded_to_a_warning(self) -> None:
        def apply(_operations: list[str], _sensitive: bool) -> None:
            raise CommandError("LDAP server unavailable", retryable=True)

        with self.assertRaises(CommandError):
            apply_attributes_resiliently(
                {"displayName": ["User"]},
                apply,
                lambda _name, _reason: None,
                strict=False,
            )


if __name__ == "__main__":
    unittest.main()
