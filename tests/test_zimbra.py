from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from zimigrate.config import AppConfig, ArchiveConfig, EndpointConfig, ImportConfig, TransferConfig
from zimigrate.errors import CommandError
from zimigrate.exporter import Exporter
from zimigrate.models import CommandResult
from zimigrate.zimbra import (
    ZMMAILBOX,
    ZMPROV,
    ZimbraClient,
    mailbox_rest_url,
    parse_attribute_sections,
    parse_attributes,
    parse_current_volume_paths,
    parse_name_list,
    parse_quota_usage,
)


class ZmprovParsingTests(unittest.TestCase):
    def test_quota_usage_parses_bytes(self) -> None:
        self.assertEqual(parse_quota_usage("u@example.com 0 4096\n"), {"u@example.com": 4096})

    def test_quota_usage_skips_headers_and_extra_columns(self) -> None:
        output = """account quota used
u@example.com 0 4096 extra
name quota usage
v@example.com 100 50
"""
        self.assertEqual(
            parse_quota_usage(output),
            {"u@example.com": 4096, "v@example.com": 50},
        )

    def test_current_volume_parser_accepts_is_current(self) -> None:
        output = """Volume id: 1
type: primaryMessage
path: /opt/zimbra/store
is current: true
"""
        self.assertEqual(
            parse_current_volume_paths(output),
            {"primaryMessage": [Path("/opt/zimbra/store")]},
        )

    def test_attributes_preserve_repeated_and_multiline_values(self) -> None:
        output = """# name user@example.com
zimbraMailAlias: first@example.com
zimbraMailAlias: second@example.com
description: first line
continuation line
empty:
"""

        self.assertEqual(
            parse_attributes(output),
            {
                "zimbraMailAlias": ["first@example.com", "second@example.com"],
                "description": ["first line\ncontinuation line"],
                "empty": [""],
            },
        )

    def test_binary_attribute_ldap_separator_is_not_part_of_value(self) -> None:
        output = "userCertificate:: YWJjZA==\n"

        self.assertEqual(parse_attributes(output), {"userCertificate": ["YWJjZA=="]})

    def test_sections_split_zmprov_named_records(self) -> None:
        output = """# name one
zimbraSignatureName: one
zimbraSignatureId: id-1

# name two
zimbraSignatureName: two
zimbraSignatureId: id-2
"""

        self.assertEqual(
            parse_attribute_sections(output),
            [
                {"zimbraSignatureName": ["one"], "zimbraSignatureId": ["id-1"]},
                {"zimbraSignatureName": ["two"], "zimbraSignatureId": ["id-2"]},
            ],
        )

    def test_name_list_ignores_headers_and_attribute_output(self) -> None:
        output = "# accounts\na@example.com\n\nname: ignored\nb@example.com\n"
        self.assertEqual(parse_name_list(output), ["a@example.com", "b@example.com"])

    def test_sieve_comments_colons_and_blank_lines_remain_in_multiline_value(self) -> None:
        output = """# name user@example.com
zimbraMailSieveScript: require ["fileinto"];
# keep this rule
if header :contains "subject" "project" {

    fileinto "Projects";
}
zimbraMailQuota: 0
"""

        parsed = parse_attributes(output)

        self.assertEqual(
            parsed["zimbraMailSieveScript"],
            [
                'require ["fileinto"];\n# keep this rule\n'
                'if header :contains "subject" "project" {\n\n'
                '    fileinto "Projects";\n}'
            ],
        )
        self.assertEqual(parsed["zimbraMailQuota"], ["0"])


class ZimbraCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ZimbraClient(EndpointConfig(), retries=0, retry_base_seconds=0)
        self.client.runner = MagicMock()
        self.client.runner.run.return_value = CommandResult("", "", 0)

    def test_calendar_resource_creation_uses_current_zmprov_syntax(self) -> None:
        self.client.create(
            "calendar_resource",
            "room@example.com",
            ["displayName", "Room", "zimbraCalResType", "Location"],
        )

        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(
            command,
            [ZMPROV, "-l", "-f", "/dev/stdin"],
        )
        self.assertEqual(
            self.client.runner.run.call_args.kwargs["input_data"],
            b"'ccr' 'room@example.com' '' 'displayName' 'Room' 'zimbraCalResType' 'Location'\n",
        )

    def test_calendar_resource_creation_retries_positional_syntax(self) -> None:
        self.client.runner.run.side_effect = [
            CommandError("usage: zmprov ccr {name} {password} {displayName} {type}"),
            CommandResult("", "", 0),
        ]
        self.client.create(
            "calendar_resource",
            "room@example.com",
            [
                "displayName",
                "Room",
                "zimbraCalResType",
                "Location",
                "zimbraAccountStatus",
                "maintenance",
            ],
        )
        retry = self.client.runner.run.call_args
        self.assertEqual(retry.args[0], [ZMPROV, "-l", "-f", "/dev/stdin"])
        self.assertEqual(
            retry.kwargs["input_data"],
            b"'ccr' 'room@example.com' '' 'Room' 'Location' 'zimbraAccountStatus' 'maintenance'\n",
        )

    def test_dynamic_distribution_list_falls_back_to_cdl(self) -> None:
        self.client.runner.run.side_effect = [
            CommandError("ERROR: unknown command cddl"),
            CommandResult("", "", 0),
        ]
        self.client.create("dynamic_distribution_list", "team@example.com")
        retry = self.client.runner.run.call_args.args[0]
        self.assertEqual(
            retry,
            [
                ZMPROV,
                "-l",
                "cdl",
                "team@example.com",
                "zimbraIsDynamicGroup",
                "TRUE",
            ],
        )

    def test_mailbox_export_routes_to_mailhost_with_zip_metadata_and_lock(self) -> None:
        self.client.export_mailbox(
            "user@example.com",
            "is:anywhere",
            Path("/tmp/mailbox.zip"),
            "mailbox-1.example.com",
            "zip",
            True,
        )

        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(
            command,
            [
                ZMMAILBOX,
                "-z",
                "-u",
                "https://mailbox-1.example.com:7071",
                "-m",
                "user@example.com",
                "-t",
                "0",
                "getRestURL",
                "-o",
                "/tmp/mailbox.zip",
                "//?fmt=zip&meta=1&lock=1&emptyname=mailbox.zip&query=is:anywhere",
            ],
        )
        self.client.runner.run.assert_called_once()
        self.assertTrue(self.client.runner.run.call_args.kwargs.get("retryable"))
        self.assertIsNone(self.client.runner.run.call_args.kwargs.get("output_path"))

    def test_mailbox_export_retries_without_lock_when_unsupported(self) -> None:
        self.client.runner.run.side_effect = [
            CommandError("unknown parameter lock"),
            CommandResult("", "", 0),
        ]
        self.client.export_mailbox(
            "user@example.com",
            "is:anywhere",
            Path("/tmp/mailbox.zip"),
            None,
            "zip",
            True,
        )
        retry = self.client.runner.run.call_args.args[0]
        self.assertEqual(retry[-1], "//?fmt=zip&meta=1&emptyname=mailbox.zip&query=is:anywhere")

    def test_year_chunk_query_encodes_spaces_and_quotes(self) -> None:
        encoded = mailbox_rest_url(
            "zip",
            query='is:anywhere AND before:"2020/01/01"',
            lock=True,
        )
        self.assertEqual(
            encoded,
            "//?fmt=zip&meta=1&lock=1&query=is:anywhere%20AND%20before:%222020/01/01%22",
        )

    def test_query_plus_sign_is_encoded_instead_of_becoming_a_space(self) -> None:
        encoded = mailbox_rest_url("zip", query="after:+1day")

        self.assertIn("query=after:%2B1day", encoded)

    def test_year_chunks_use_locale_independent_non_overlapping_epoch_ranges(self) -> None:
        config = AppConfig(
            source=EndpointConfig(),
            target=EndpointConfig(),
            archive=ArchiveConfig(),
            transfer=TransferConfig(
                mailbox_mode="year-chunks",
                mailbox_start_year=2000,
                mailbox_chunk_years=5,
            ),
            import_options=ImportConfig(),
        )

        queries = Exporter(config, MagicMock())._mailbox_queries()

        self.assertGreater(len(queries), 2)
        self.assertTrue(all("/" not in query for _, query in queries))
        self.assertTrue(
            all("after:" not in query and "before:" not in query for _, query in queries)
        )
        for (_, previous), (_, current) in zip(queries[1:-1], queries[2:-1], strict=False):
            previous_end = previous.rsplit("date:<", 1)[1]
            current_start = current.split("date:>=", 1)[1].split(" ", 1)[0]
            self.assertEqual(previous_end, current_start)

    def test_mailbox_import_retries_only_for_skip_resolution(self) -> None:
        self.client.import_mailbox(
            "user@example.com",
            Path("/tmp/mailbox.zip"),
            "skip",
            "mailbox-1.example.com",
            "zip",
        )
        self.assertTrue(self.client.runner.run.call_args.kwargs.get("retryable"))
        self.assertEqual(
            self.client.runner.run.call_args.args[0][-2],
            "//?fmt=zip&resolve=skip",
        )

        self.client.import_mailbox(
            "user@example.com",
            Path("/tmp/mailbox.zip"),
            "reset",
            "mailbox-1.example.com",
            "zip",
        )
        self.assertFalse(self.client.runner.run.call_args.kwargs.get("retryable"))

    def test_flush_cache_uses_soap_not_ldap_direct(self) -> None:
        self.client.flush_cache("account", "user@example.com")
        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(command, [ZMPROV, "fc", "account", "user@example.com"])
        self.assertTrue(self.client.runner.run.call_args.kwargs.get("retryable"))

    def test_flush_cache_failure_does_not_abort(self) -> None:
        self.client.runner.run.side_effect = CommandError("flushCache is not available")
        with self.assertLogs("zimigrate.zimbra", level="WARNING") as logs:
            self.client.flush_cache("account", "user@example.com")
        self.assertTrue(any("Could not flush account cache" in line for line in logs.output))

    def test_account_create_sends_initial_attributes_over_stdin(self) -> None:
        self.client.create(
            "account",
            "user@example.com",
            ["zimbraAccountStatus", "maintenance"],
        )
        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(command, [ZMPROV, "-l", "-f", "/dev/stdin"])
        self.assertEqual(
            self.client.runner.run.call_args.kwargs["input_data"],
            b"'ca' 'user@example.com' '' 'zimbraAccountStatus' 'maintenance'\n",
        )

    def test_sensitive_modify_values_are_sent_over_stdin_not_process_arguments(self) -> None:
        self.client.modify_data_source(
            "user@example.com",
            "remote",
            ["zimbraDataSourcePassword", "line 1\n'quoted'\\value"],
            sensitive=True,
        )

        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(command, [ZMPROV, "-l", "-f", "/dev/stdin"])
        self.assertNotIn(b"line 1", " ".join(command).encode())
        self.assertEqual(
            self.client.runner.run.call_args.kwargs["input_data"],
            b"'mds' 'user@example.com' 'remote' 'zimbraDataSourcePassword' "
            b"'line 1\\n\\'quoted\\'\\\\value'\n",
        )


if __name__ == "__main__":
    unittest.main()
