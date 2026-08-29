from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zimigrate.config import AppConfig, EndpointConfig, ImportConfig, TransferConfig
from zimigrate.errors import CommandError, CompatibilityError, ZimigrateError
from zimigrate.exporter import Exporter
from zimigrate.models import CommandResult
from zimigrate.zimbra import (
    ZMMAILBOX,
    ZMPROV,
    ZimbraClient,
    is_already_exists_error,
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

    def test_binary_attribute_ldap_separator_is_decoded(self) -> None:
        output = "userCertificate:: YWJjZA==\n"

        self.assertEqual(parse_attributes(output), {"userCertificate": ["abcd"]})

    def test_chunked_binary_password_hash_is_decoded(self) -> None:
        # ProvUtil.printAttr emits ldapsearch-style chunked base64 after ":: ".
        output = "userPassword:: e1NTSEF9aGFzaA==\n"

        self.assertEqual(parse_attributes(output), {"userPassword": ["{SSHA}hash"]})

    def test_non_utf8_binary_attribute_keeps_compact_base64(self) -> None:
        # DER/JPEG is not UTF-8 text; ProvUtil still prints "::" base64.
        payload = bytes.fromhex("30820100ffd8ffe00010")
        encoded = base64.b64encode(payload).decode("ascii")
        wrapped = encoded[:16] + "\n " + encoded[16:]
        output = f"jpegPhoto:: {wrapped}\n"

        self.assertEqual(parse_attributes(output), {"jpegPhoto": [encoded]})

    def test_invalid_ldap_base64_is_rejected(self) -> None:
        with self.assertRaises(ZimigrateError) as ctx:
            parse_attributes("userPassword:: not valid base64!!\n")
        self.assertIn("base64", str(ctx.exception).casefold())

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
        self.client.runner.is_remote = False
        self.client.runner.run.return_value = CommandResult("", "", 0)

    def test_preflight_validates_required_commands_and_safe_file_output(self) -> None:
        self.client.runner.run.side_effect = [
            CommandResult("Release 10.1.18.GA FOSS", "", 0),
            CommandResult("  createAccount(ca) {name} {password}\n", "", 0),
            CommandResult(
                "  getRestURL(gru) [opts] {url}\n    -o/--output <arg> output file\n",
                "",
                0,
            ),
        ]

        version = self.client.preflight(
            require_mailbox=True,
            required_provisioning_commands={"ca"},
            required_mailbox_commands={"getRestURL"},
            require_mailbox_output=True,
        )

        self.assertEqual(version, "Release 10.1.18.GA FOSS")

    def test_preflight_rejects_a_missing_provisioning_command(self) -> None:
        self.client.runner.run.side_effect = [
            CommandResult("Release 8.8.15.GA FOSS", "", 0),
            CommandResult("  getAccount(ga) {name}\n", "", 0),
        ]

        with self.assertRaisesRegex(CompatibilityError, "required command: ca"):
            self.client.preflight(required_provisioning_commands={"ca"})

    def test_preflight_rejects_rest_export_without_output_option(self) -> None:
        self.client.runner.run.side_effect = [
            CommandResult("Release 8.8.15.GA FOSS", "", 0),
            CommandResult("  getAccount(ga) {name}\n", "", 0),
            CommandResult("  getRestURL(gru) {url}\n", "", 0),
        ]

        with self.assertRaisesRegex(CompatibilityError, "does not support --output"):
            self.client.preflight(
                require_mailbox=True,
                required_mailbox_commands={"getRestURL"},
                require_mailbox_output=True,
            )

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

    def test_dynamic_distribution_list_uses_official_command(self) -> None:
        self.client.create("dynamic_distribution_list", "team@example.com")
        self.assertEqual(
            self.client.runner.run.call_args.args[0],
            [ZMPROV, "-l", "cddl", "team@example.com"],
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

    def test_remote_mailbox_export_streams_stdout_without_output_file(self) -> None:
        session = MagicMock()
        session.user = "root"
        self.client = ZimbraClient(
            EndpointConfig(),
            retries=0,
            retry_base_seconds=0,
            session=session,
        )
        self.client.runner = MagicMock()
        self.client.runner.is_remote = True
        self.client.runner.run.return_value = CommandResult("", "", 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mailbox.tgz"
            output.write_bytes(b"archive")
            self.client.export_mailbox("user@example.com", "is:anywhere", output)
        command = self.client.runner.run.call_args.args[0]
        self.assertNotIn("-o", command)
        self.assertEqual(self.client.runner.run.call_args.kwargs.get("output_path"), output)

    def test_local_mailbox_export_streams_when_output_option_is_missing(self) -> None:
        self.client._mailbox_output_supported = False
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mailbox.tgz"
            output.write_bytes(b"archive")
            self.client.export_mailbox("user@example.com", "is:anywhere", output)
        command = self.client.runner.run.call_args.args[0]
        self.assertNotIn("-o", command)
        self.assertEqual(self.client.runner.run.call_args.kwargs.get("output_path"), output)

    def test_streamed_mailbox_export_rejects_an_empty_download(self) -> None:
        self.client._mailbox_output_supported = False

        def write_empty(*_args: object, output_path: Path | None = None, **_kwargs: object):
            if output_path is not None:
                output_path.write_bytes(b"")
            return CommandResult("", "", 0)

        self.client.runner.run.side_effect = write_empty
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mailbox.tgz"
            with self.assertRaisesRegex(CommandError, "produced no data"):
                self.client.export_mailbox("user@example.com", "is:anywhere", output)
            self.assertFalse(output.is_file())

    def test_mailbox_export_chowns_mailbox_dirs_when_not_zimbra(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mailboxes" / "user@example.com" / "full.tgz"
            owner = SimpleNamespace(pw_uid=107, pw_gid=107)
            with (
                patch("zimigrate.zimbra.getpass.getuser", return_value="root"),
                patch("zimigrate.zimbra.pwd.getpwnam", return_value=owner),
                patch("zimigrate.zimbra.os.chown") as chown,
            ):
                self.client.export_mailbox(
                    "user@example.com",
                    "is:anywhere",
                    output,
                )
            chowned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertIn(output.parent.resolve(), chowned)
            self.assertIn((output.parent.parent).resolve(), chowned)
            self.assertTrue(all(call.args[1:] == (107, 107) for call in chown.call_args_list))

    def test_mailbox_export_does_not_silently_drop_requested_lock(self) -> None:
        self.client.runner.run.side_effect = CommandError("unknown parameter lock")

        with self.assertRaises(CommandError):
            self.client.export_mailbox(
                "user@example.com",
                "is:anywhere",
                Path("/tmp/mailbox.zip"),
                None,
                "zip",
                True,
            )

        self.assertEqual(self.client.runner.run.call_count, 1)

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
        for _, query in queries:
            for token in query.replace(" AND ", " ").split():
                if token.startswith("date:<"):
                    self.assertRegex(token.split("date:<", 1)[1], r"^[0-9]+$")
                elif token.startswith("date:>="):
                    self.assertRegex(token.split("date:>=", 1)[1], r"^[0-9]+$")
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

    def test_flush_cache_targets_mailbox_host_over_soap(self) -> None:
        self.client.flush_cache("account", "user@example.com", server="mailbox-1.example.com")
        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(
            command,
            [
                ZMPROV,
                "-s",
                "mailbox-1.example.com:7071",
                "fc",
                "account",
                "user@example.com",
            ],
        )
        self.assertNotIn("-l", command)
        self.assertTrue(self.client.runner.run.call_args.kwargs.get("retryable"))

    def test_flush_cache_falls_back_to_local_soap_when_mailbox_host_fails(self) -> None:
        self.client.runner.run.side_effect = [
            CommandError("connection refused"),
            CommandResult("", "", 0),
        ]
        with self.assertLogs("zimigrate.zimbra", level="WARNING") as logs:
            self.client.flush_cache("account", "user@example.com", server="mailbox-1.example.com")
        self.assertTrue(any("retrying on the local SOAP server" in line for line in logs.output))
        commands = [call.args[0] for call in self.client.runner.run.call_args_list]
        self.assertEqual(
            commands[0],
            [
                ZMPROV,
                "-s",
                "mailbox-1.example.com:7071",
                "fc",
                "account",
                "user@example.com",
            ],
        )
        self.assertEqual(commands[1], [ZMPROV, "fc", "account", "user@example.com"])

    def test_flush_cache_rejects_unsafe_mailbox_host(self) -> None:
        with self.assertRaises(CompatibilityError):
            self.client.flush_cache("account", "user@example.com", server="host;rm -rf")
        self.client.runner.run.assert_not_called()

    def test_flush_cache_failure_aborts_activation(self) -> None:
        self.client.runner.run.side_effect = CommandError("flushCache is not available")
        with (
            self.assertLogs("zimigrate.zimbra", level="ERROR") as logs,
            self.assertRaises(CommandError),
        ):
            self.client.flush_cache("account", "user@example.com")
        self.assertTrue(any("activation cannot continue" in line for line in logs.output))

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
        self.assertTrue(self.client.runner.run.call_args.kwargs.get("retryable"))

    def test_exists_retries_transient_failures(self) -> None:
        self.client.runner.run.return_value = CommandResult(
            "# name user@example.com\nzimbraId: abc\n", "", 0
        )
        self.assertTrue(self.client.exists("account", "user@example.com"))
        self.assertTrue(self.client.runner.run.call_args.kwargs.get("retryable"))

    def test_get_optional_returns_none_when_zimbra_reports_no_such_account(self) -> None:
        self.client.runner.run.side_effect = CommandError(
            "ERROR: account.NO_SUCH_ACCOUNT (no such account)"
        )
        self.assertIsNone(self.client.get_optional("account", "missing@example.com"))

    def test_get_optional_does_not_treat_network_lookup_failure_as_a_missing_object(self) -> None:
        self.client.runner.run.side_effect = CommandError("mail server host not found")

        with self.assertRaises(CommandError):
            self.client.get_optional("account", "user@example.com")

    def test_create_treats_already_exists_as_success(self) -> None:
        self.client.runner.run.side_effect = CommandError(
            "ERROR: account.ACCOUNT_EXISTS (email address already exists)"
        )
        with self.assertLogs("zimigrate.zimbra", level="INFO"):
            self.client.create("account", "user@example.com")
        self.assertTrue(is_already_exists_error(CommandError("account.ACCOUNT_EXISTS")))

    def test_sensitive_account_modify_uses_ldap_direct_stdin(self) -> None:
        self.client.modify(
            "account",
            "user@example.com",
            ["userPassword", "{SSHA}hash"],
            sensitive=True,
        )
        command = self.client.runner.run.call_args.args[0]
        self.assertEqual(command, [ZMPROV, "-l", "-f", "/dev/stdin"])
        self.assertEqual(
            self.client.runner.run.call_args.kwargs["input_data"],
            b"'ma' 'user@example.com' 'userPassword' '{SSHA}hash'\n",
        )

    def test_calendar_resource_list_does_not_swallow_failures(self) -> None:
        self.client.runner.run.side_effect = CommandError("LDAP server unavailable", retryable=True)
        with self.assertRaises(CommandError):
            self.client.list_calendar_resources()

        self.client.runner.run.side_effect = CommandError("unknown command gacr")
        with self.assertRaises(CommandError):
            self.client.list_calendar_resources()

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
