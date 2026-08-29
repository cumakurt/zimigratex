from __future__ import annotations

import base64
import getpass
import logging
import os
import pwd
import re
from binascii import Error as Base64Error
from pathlib import Path
from urllib.parse import quote

from zimigrate.config import EndpointConfig, TransferConfig
from zimigrate.errors import CommandError, CompatibilityError, ZimigrateError
from zimigrate.models import Attributes
from zimigrate.runner import CommandRunner
from zimigrate.ssh import SshSession
from zimigrate.util import is_valid_dns_name

LOGGER = logging.getLogger(__name__)
ZMPROV = "/opt/zimbra/bin/zmprov"
ZMMAILBOX = "/opt/zimbra/bin/zmmailbox"
ZMCONTROL = "/opt/zimbra/bin/zmcontrol"
ZMHOSTNAME = "/opt/zimbra/bin/zmhostname"
ZMVOLUME = "/opt/zimbra/bin/zmvolume"
# Mailbox.ID_FOLDER_INBOX. zmprov cds requires a folder id; import then applies
# the archived zimbraDataSourceFolderId after mailbox REST restore.
ZIMBRA_INBOX_FOLDER_ID = "2"
# ProvUtil.printAttr follows ldapsearch: binary values are "name:: " plus
# commons-codec chunked base64. Decode that payload before storing or applying
# it so zmprov receives the LDAP value ({SSHA}..., plaintext secrets) rather
# than the LDIF alphabet.
ATTRIBUTE_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*)(::?)(?: ?)(.*)$")
HELP_COMMAND = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\(([^)]+)\)", re.MULTILINE)
MAILBOX_OUTPUT_OPTION = re.compile(r"(?:^|\s)(?:-o/--output|--output|-o(?:\s|$))")
QUERY_SAFE = ":/*@"
MISSING_OBJECT_MARKERS = (
    "account.no_such_account",
    "account.no_such_calendar_resource",
    "account.no_such_cos",
    "account.no_such_distribution_list",
    "account.no_such_domain",
    "account.no_such_group",
    "account.no_such_server",
)
ALREADY_EXISTS_MARKERS = (
    "account.account_exists",
    "account.alias_exists",
    "account.cos_exists",
    "account.data_source_exists",
    "account.distribution_list_exists",
    "account.domain_exists",
    "account.identity_exists",
    "account.member_exists",
    "account.server_exists",
    "account.signature_exists",
)


def required_export_commands(transfer: TransferConfig) -> set[str]:
    commands: set[str] = set()
    if transfer.include_domains:
        commands.update({"gad", "gd"})
    if transfer.include_cos:
        commands.update({"gac", "gc"})
    if transfer.include_accounts:
        commands.update({"gacr", "gaa", "ga", "gcr", "gid", "gsig", "gds"})
    if transfer.include_distribution_lists:
        commands.update({"gadl", "gdl", "gdlm"})
    if transfer.include_mailboxes:
        commands.update({"gas", "gs", "gqu"})
    return commands


def required_import_commands(transfer: TransferConfig) -> set[str]:
    commands: set[str] = set()
    if transfer.include_cos:
        commands.update({"gc", "cc", "mc"})
    if transfer.include_domains:
        commands.update({"gd", "cd", "cad", "md"})
    if transfer.include_accounts:
        commands.update(
            {
                "ga",
                "gcr",
                "ca",
                "ccr",
                "ma",
                "mcr",
                "aaa",
                "gid",
                "cid",
                "mid",
                "gsig",
                "csig",
                "msig",
                "gds",
                "cds",
                "mds",
                "fc",
                "gs",
            }
        )
    if transfer.include_distribution_lists:
        commands.update({"gdl", "cdl", "cddl", "mdl", "adla", "gdlm", "adlm"})
    return commands


def required_verification_commands(transfer: TransferConfig) -> set[str]:
    commands: set[str] = set()
    if transfer.include_cos:
        commands.add("gc")
    if transfer.include_domains:
        commands.add("gd")
    if transfer.include_accounts:
        commands.update({"ga", "gcr", "gid", "gsig", "gds"})
    if transfer.include_distribution_lists:
        commands.update({"gdl", "gdlm"})
    return commands


def mailbox_rest_url(
    archive_format: str,
    *,
    query: str | None = None,
    lock: bool = False,
    resolve: str | None = None,
    meta: bool = True,
    empty_name: str | None = None,
) -> str:
    """Build a zmmailbox REST path with query values encoded for Zimbra search syntax."""
    parts = [f"fmt={archive_format}"]
    if meta:
        parts.append("meta=1")
    if lock:
        parts.append("lock=1")
    if resolve is not None:
        parts.append(f"resolve={quote(resolve, safe='')}")
    if empty_name is not None:
        parts.append(f"emptyname={quote(empty_name, safe='')}")
    if query is not None:
        parts.append(f"query={quote(query, safe=QUERY_SAFE)}")
    return "//?" + "&".join(parts)


def parse_attributes(output: str) -> Attributes:
    attributes: Attributes = {}
    binary_values: dict[str, list[bool]] = {}
    previous: str | None = None
    previous_binary = False
    for raw_line in output.splitlines():
        if raw_line.startswith("# name "):
            previous = None
            previous_binary = False
            continue
        match = ATTRIBUTE_LINE.fullmatch(raw_line)
        if not match:
            if previous is not None and attributes[previous]:
                if previous_binary:
                    attributes[previous][-1] += "".join(raw_line.split())
                else:
                    attributes[previous][-1] += "\n" + raw_line
            continue
        name, separator, value = match.groups()
        binary = separator == "::"
        if binary:
            value = "".join(value.split())
        attributes.setdefault(name, []).append(value)
        binary_values.setdefault(name, []).append(binary)
        previous = name
        previous_binary = binary
    for name, values in attributes.items():
        flags = binary_values.get(name, [])
        attributes[name] = [
            _decode_ldap_base64(value) if is_binary else value
            for value, is_binary in zip(values, flags, strict=True)
        ]
    return attributes


def _decode_ldap_base64(value: str) -> str:
    compact = "".join(value.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (Base64Error, ValueError) as exc:
        raise ZimigrateError("LDAP binary attribute is not valid base64") from exc
    return decoded.decode("utf-8") if _is_utf8_text(decoded) else compact


def _is_utf8_text(value: bytes) -> bool:
    if not value or b"\x00" in value:
        return False
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(ch == "\t" or ch == "\n" or ch == "\r" or ch.isprintable() for ch in text)


def parse_attribute_sections(output: str) -> list[Attributes]:
    sections: list[Attributes] = []
    buffer: list[str] = []
    saw_header = False
    for line in output.splitlines():
        if line.startswith("# name "):
            saw_header = True
            if buffer:
                parsed = parse_attributes("\n".join(_trim_section(buffer)))
                if parsed:
                    sections.append(parsed)
            buffer = []
            continue
        buffer.append(line)
    if buffer:
        parsed = parse_attributes("\n".join(_trim_section(buffer)))
        if parsed:
            sections.append(parsed)
    if not saw_header and not sections:
        parsed = parse_attributes(output)
        if parsed:
            sections.append(parsed)
    return sections


def _trim_section(lines: list[str]) -> list[str]:
    end = len(lines)
    while end and not lines[end - 1]:
        end -= 1
    return lines[:end]


def parse_name_list(output: str) -> list[str]:
    values: list[str] = []
    for line in output.splitlines():
        value = line.strip()
        if not value or value.startswith("#") or ": " in value:
            continue
        values.append(value)
    return values


def parse_quota_usage(output: str) -> dict[str, int]:
    usage: dict[str, int] = {}
    for line_number, raw_line in enumerate(output.splitlines(), start=1):
        fields = raw_line.split()
        if not fields:
            continue
        parsed = _quota_fields(fields)
        if parsed is None:
            if _looks_like_quota_header(fields):
                continue
            raise CompatibilityError(f"Unexpected getQuotaUsage output on line {line_number}")
        quota, used = parsed
        if quota < 0 or used < 0:
            raise CompatibilityError(f"Negative getQuotaUsage value on line {line_number}")
        usage[fields[0]] = max(used, usage.get(fields[0], 0))
    return usage


def parse_current_volume_paths(output: str) -> dict[str, list[Path]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Volume id:"):
            if current:
                records.append(current)
            current = {"volume id": line.split(":", 1)[1].strip()}
            continue
        if not line or line == "-" or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip().casefold()] = value.strip()
    if current:
        records.append(current)

    paths: dict[str, list[Path]] = {}
    for record in records:
        current_value = record.get("current", record.get("is current", "false"))
        if current_value.casefold() not in {"true", "yes", "1"}:
            continue
        volume_type = record.get("type")
        raw_path = record.get("path")
        if volume_type not in {"primaryMessage", "index"} or not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            raise CompatibilityError(f"Zimbra volume path is not absolute: {raw_path}")
        paths.setdefault(volume_type, []).append(path)
    return paths


class ZimbraClient:
    def __init__(
        self,
        endpoint: EndpointConfig,
        *,
        retries: int,
        retry_base_seconds: float,
        session: SshSession | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.runner = CommandRunner(
            endpoint,
            retries=retries,
            retry_base_seconds=retry_base_seconds,
            session=session,
        )
        self._mailbox_output_supported: bool | None = None

    def version(self) -> str:
        result = self.runner.run([ZMCONTROL, "-v"], retryable=True)
        value = " ".join((result.stdout + " " + result.stderr).split())
        if not value:
            raise CompatibilityError("zmcontrol -v returned no version")
        return value

    def preflight(
        self,
        *,
        require_mailbox: bool = False,
        required_provisioning_commands: set[str] | None = None,
        required_mailbox_commands: set[str] | None = None,
        require_mailbox_output: bool = False,
    ) -> str:
        version = self.version()
        provisioning_help = self.runner.run([ZMPROV, "help", "commands"], retryable=True).stdout
        _require_help_commands(
            provisioning_help,
            required_provisioning_commands or set(),
            utility="zmprov",
        )
        if require_mailbox:
            mailbox_help = self.runner.run([ZMMAILBOX, "help", "commands"], retryable=True).stdout
            _require_help_commands(
                mailbox_help,
                required_mailbox_commands or set(),
                utility="zmmailbox",
            )
            supports_output = bool(MAILBOX_OUTPUT_OPTION.search(mailbox_help))
            self._mailbox_output_supported = supports_output
            if require_mailbox_output and not supports_output:
                raise CompatibilityError("zmmailbox getRestURL does not support --output")
        return version

    def hostname(self) -> str:
        result = self.runner.run([ZMHOSTNAME], retryable=True)
        hostname = result.stdout.strip()
        if not is_valid_dns_name(hostname):
            raise CompatibilityError("zmhostname returned an invalid host name")
        return hostname

    def list_domains(self) -> list[str]:
        return self._list("gad", ldap=True)

    def list_cos(self) -> list[str]:
        return self._list("gac", ldap=True)

    def list_accounts(self) -> list[str]:
        return self._list("gaa", ldap=True)

    def list_calendar_resources(self) -> list[str]:
        return self._list("gacr", ldap=True)

    def list_distribution_lists(self) -> list[str]:
        return self._list("gadl", ldap=True)

    def list_servers(self) -> list[str]:
        return self._list("gas", ldap=True)

    def get_domain(self, name: str) -> Attributes:
        return self._get("gd", name, ldap=True)

    def get_cos(self, name: str) -> Attributes:
        return self._get("gc", name, ldap=True)

    def get_account(self, name: str) -> Attributes:
        return self._get("ga", name, ldap=True, sensitive=True)

    def get_calendar_resource(self, name: str) -> Attributes:
        return self._get("gcr", name, ldap=True, sensitive=True)

    def get_distribution_list(self, name: str) -> Attributes:
        return self._get("gdl", name, ldap=True, sensitive=True)

    def get_distribution_list_members(self, name: str) -> list[str]:
        return self._list("gdlm", name, ldap=True)

    def get_server(self, name: str) -> Attributes:
        return self._get("gs", name, ldap=True, sensitive=True)

    def get_quota_usage(self, server: str) -> dict[str, int]:
        return parse_quota_usage(self._zmprov("gqu", server, retryable=True))

    def get_current_volume_paths(self) -> dict[str, list[Path]]:
        output = self.runner.run([ZMVOLUME, "-l"], retryable=True).stdout
        paths = parse_current_volume_paths(output)
        if not paths.get("primaryMessage"):
            raise CompatibilityError("No current primaryMessage volume was reported by zmvolume")
        return paths

    def get_identities(self, account: str) -> list[Attributes]:
        return self._get_sections("gid", account)

    def get_signatures(self, account: str) -> list[Attributes]:
        return self._get_sections("gsig", account)

    def get_data_sources(self, account: str) -> list[Attributes]:
        return self._get_sections("gds", account, sensitive=True)

    def get_optional(self, kind: str, name: str) -> Attributes | None:
        """Return LDAP attributes, or None when Zimbra reports a missing object."""
        command = {
            "domain": "gd",
            "cos": "gc",
            "account": "ga",
            "calendar_resource": "gcr",
            "distribution_list": "gdl",
            "dynamic_distribution_list": "gdl",
            "server": "gs",
        }[kind]
        try:
            return self._get(command, name, ldap=True, sensitive=True, retryable=True)
        except CommandError as exc:
            if is_missing_object_error(exc):
                return None
            raise

    def exists(self, kind: str, name: str) -> bool:
        return self.get_optional(kind, name) is not None

    def create(self, kind: str, name: str, initial_operations: list[str] | None = None) -> None:
        initial_operations = initial_operations or []
        try:
            if kind == "domain":
                self._zmprov("cd", name, *initial_operations, ldap=True, retryable=True)
            elif kind == "cos":
                self._zmprov("cc", name, *initial_operations, ldap=True, retryable=True)
            elif kind == "account":
                self._zmprov(
                    "ca",
                    name,
                    "",
                    *initial_operations,
                    ldap=True,
                    retryable=True,
                    sensitive=True,
                    protect_arguments=True,
                )
            elif kind == "calendar_resource":
                self._zmprov(
                    "ccr",
                    name,
                    "",
                    *initial_operations,
                    ldap=True,
                    retryable=True,
                    sensitive=True,
                    protect_arguments=True,
                )
            elif kind == "distribution_list":
                self._zmprov("cdl", name, *initial_operations, ldap=True, retryable=True)
            elif kind == "dynamic_distribution_list":
                self._zmprov("cddl", name, *initial_operations, ldap=True, retryable=True)
            else:
                raise ValueError(f"Unsupported entity kind: {kind}")
        except CommandError as exc:
            if is_already_exists_error(exc):
                LOGGER.info(
                    "Create reported an existing object; continuing",
                    extra={"kind": kind, "entity": name},
                )
                return
            raise

    def flush_cache(self, cache_type: str, name: str, *, server: str | None = None) -> None:
        """Reload mailboxd LDAP cache after LDAP-direct (`zmprov -l`) writes.

        Password hashes and account status written with `-l` update OpenLDAP
        immediately, but mailboxd keeps cached entries for
        `ldap_cache_<type>_maxage` (default 15 minutes). `flushCache` is a SOAP
        command and must not use `-l`. The account's `zimbraMailHost` is flushed
        first (`zmprov -s host:port`) so a remote store does not keep the empty
        password or maintenance status. Local SOAP is the fallback. If both
        attempts fail, activation must stop rather than leave cache state unknown.
        """
        try:
            self._flush_cache_once(cache_type, name, server=server)
            return
        except CommandError as exc:
            if server:
                LOGGER.warning(
                    "Could not flush %s cache for %s on %s; retrying on the local SOAP server",
                    cache_type,
                    name,
                    server,
                    extra={"detail": str(exc)},
                )
                try:
                    self._flush_cache_once(cache_type, name, server=None)
                    return
                except CommandError as fallback_error:
                    exc = fallback_error
            LOGGER.error(
                "Could not flush %s cache for %s; account activation cannot continue",
                cache_type,
                name,
                extra={"detail": str(exc)},
            )
            raise exc

    def _flush_cache_once(self, cache_type: str, name: str, *, server: str | None) -> None:
        soap_server = None
        if server:
            soap_server = f"{_require_mailbox_host(server)}:{self.endpoint.mailbox_admin_port}"
        self._zmprov(
            "fc",
            cache_type,
            name,
            ldap=False,
            retryable=True,
            soap_server=soap_server,
        )

    def modify(self, kind: str, name: str, operations: list[str], *, sensitive: bool) -> None:
        command = {
            "domain": "md",
            "cos": "mc",
            "account": "ma",
            "calendar_resource": "mcr",
            "distribution_list": "mdl",
            "dynamic_distribution_list": "mdl",
        }[kind]
        self._zmprov(
            command,
            name,
            *operations,
            ldap=True,
            retryable=True,
            sensitive=sensitive,
            protect_arguments=True,
        )

    def add_account_alias(self, account: str, alias: str) -> None:
        self._add_idempotent("aaa", account, alias)

    def add_distribution_alias(self, distribution_list: str, alias: str) -> None:
        self._add_idempotent("adla", distribution_list, alias)

    def add_distribution_member(self, distribution_list: str, member: str) -> None:
        self._add_idempotent("adlm", distribution_list, member)

    def create_alias_domain(self, alias_domain: str, target_domain: str) -> None:
        try:
            self._zmprov("cad", alias_domain, target_domain, ldap=True, retryable=True)
        except CommandError as exc:
            if is_already_exists_error(exc):
                LOGGER.info(
                    "Alias domain already exists; continuing",
                    extra={"entity": alias_domain},
                )
                return
            raise

    def _add_idempotent(self, command: str, owner: str, value: str) -> None:
        try:
            self._zmprov(command, owner, value, ldap=True, retryable=True)
        except CommandError as exc:
            if is_already_exists_error(exc):
                return
            raise

    def _create_account_child(self, command: str, account: str, name: str) -> None:
        try:
            self._zmprov(command, account, name, ldap=True, retryable=True)
        except CommandError as exc:
            if is_already_exists_error(exc):
                return
            raise

    def create_signature(self, account: str, name: str) -> None:
        self._create_account_child("csig", account, name)

    def modify_signature(
        self, account: str, name: str, operations: list[str], *, sensitive: bool
    ) -> None:
        self._zmprov(
            "msig",
            account,
            name,
            *operations,
            ldap=True,
            retryable=True,
            sensitive=sensitive,
            protect_arguments=True,
        )

    def create_identity(self, account: str, name: str) -> None:
        self._create_account_child("cid", account, name)

    def modify_identity(
        self, account: str, name: str, operations: list[str], *, sensitive: bool
    ) -> None:
        self._zmprov(
            "mid",
            account,
            name,
            *operations,
            ldap=True,
            retryable=True,
            sensitive=sensitive,
            protect_arguments=True,
        )

    def create_data_source(
        self, account: str, source_type: str, name: str, enabled: str, folder_id: str
    ) -> None:
        try:
            self._zmprov(
                "cds",
                account,
                source_type,
                name,
                "zimbraDataSourceEnabled",
                enabled,
                "zimbraDataSourceFolderId",
                folder_id,
                ldap=True,
                retryable=True,
                sensitive=True,
                protect_arguments=True,
            )
        except CommandError as exc:
            if is_already_exists_error(exc):
                return
            raise

    def modify_data_source(
        self, account: str, name: str, operations: list[str], *, sensitive: bool
    ) -> None:
        self._zmprov(
            "mds",
            account,
            name,
            *operations,
            ldap=True,
            retryable=True,
            sensitive=sensitive,
            protect_arguments=True,
        )

    def export_mailbox(
        self,
        account: str,
        query: str,
        output_path: Path,
        mailbox_host: str | None = None,
        archive_format: str = "tgz",
        lock_mailbox: bool = True,
    ) -> None:
        url = mailbox_rest_url(
            archive_format,
            query=query,
            lock=lock_mailbox,
            empty_name=f"mailbox.{archive_format}",
        )
        options = self._mailbox_options(account, mailbox_host)
        self._export_mailbox_to_path(options, url, output_path)

    def import_mailbox(
        self,
        account: str,
        path: Path,
        resolution: str,
        mailbox_host: str | None = None,
        archive_format: str = "tgz",
    ) -> None:
        url = mailbox_rest_url(archive_format, resolve=resolution, meta=False)
        options = self._mailbox_options(account, mailbox_host)
        # skip is idempotent for items already present; reset/replace can destroy
        # a mailbox that a lost response already imported.
        self.runner.run(
            [ZMMAILBOX, *options, "-t", "0", "postRestURL", url, str(path)],
            timeout=self.endpoint.mailbox_timeout_seconds,
            retryable=resolution == "skip",
        )

    def _export_mailbox_to_path(
        self,
        options: list[str],
        url: str,
        output_path: Path,
    ) -> None:
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        command = [ZMMAILBOX, *options, "-t", "0", "getRestURL"]
        if self._uses_mailbox_output_file():
            # Newer zmmailbox writes status to stdout; -o keeps the archive off that stream.
            self._grant_zimbra_write_access(output_path.parent)
            command.extend(["-o", str(output_path), url])
            extra: dict[str, Path] = {}
        else:
            # Remote SSH and Zimbra 8.6 stream the archive on stdout. Never use -o
            # over SSH: that would create the file on the source host.
            command.append(url)
            extra = {"output_path": output_path}
        try:
            self.runner.run(
                command,
                timeout=self.endpoint.mailbox_timeout_seconds,
                retryable=True,
                **extra,
            )
            if extra and (not output_path.is_file() or output_path.stat().st_size == 0):
                output_path.unlink(missing_ok=True)
                raise CommandError("Mailbox export produced no data", retryable=True)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def _uses_mailbox_output_file(self) -> bool:
        if self.runner.is_remote:
            return False
        return self._mailbox_output_supported is not False

    def _grant_zimbra_write_access(self, path: Path) -> None:
        """zmmailbox runs as zimbra and must be able to create the -o file."""
        user = self.endpoint.zimbra_user
        if getpass.getuser() == user:
            return
        resolved = path.resolve()
        if "mailboxes" not in resolved.parts:
            return
        try:
            info = pwd.getpwnam(user)
        except KeyError:
            return
        for candidate in (resolved, *resolved.parents):
            try:
                os.chown(candidate, info.pw_uid, info.pw_gid)
            except OSError:
                break
            if candidate.name == "mailboxes":
                break

    def _mailbox_options(self, account: str, mailbox_host: str | None) -> list[str]:
        options = ["-z"]
        if mailbox_host:
            host = _require_mailbox_host(
                mailbox_host,
                detail=f"Account {account} has an invalid zimbraMailHost value",
            )
            options.extend(
                [
                    "-u",
                    f"{self.endpoint.mailbox_admin_scheme}://{host}:"
                    f"{self.endpoint.mailbox_admin_port}",
                ]
            )
        options.extend(["-m", account])
        return options

    def _list(self, command: str, *arguments: str, ldap: bool = False) -> list[str]:
        return parse_name_list(
            self._zmprov(command, *arguments, ldap=ldap, retryable=True, sensitive=False)
        )

    def _get(
        self,
        command: str,
        name: str,
        *,
        ldap: bool,
        sensitive: bool = False,
        retryable: bool = True,
    ) -> Attributes:
        return parse_attributes(
            self._zmprov(command, name, ldap=ldap, retryable=retryable, sensitive=sensitive)
        )

    def _get_sections(
        self, command: str, account: str, *, sensitive: bool = False
    ) -> list[Attributes]:
        output = self._zmprov(command, account, ldap=True, retryable=True, sensitive=sensitive)
        return parse_attribute_sections(output)

    def _zmprov(
        self,
        command: str,
        *arguments: str,
        ldap: bool = False,
        retryable: bool = False,
        sensitive: bool = False,
        protect_arguments: bool = False,
        soap_server: str | None = None,
    ) -> str:
        command_line = [ZMPROV]
        if ldap:
            command_line.append("-l")
        if soap_server is not None:
            command_line.extend(["-s", soap_server])
        input_data = None
        if protect_arguments:
            command_line.extend(["-f", "/dev/stdin"])
            input_data = (_batch_line([command, *arguments]) + "\n").encode("utf-8")
        else:
            command_line.extend([command, *arguments])
        return self.runner.run(
            command_line,
            input_data=input_data,
            retryable=retryable,
            sensitive=sensitive,
        ).stdout


def _quota_fields(fields: list[str]) -> tuple[int, int] | None:
    numbers: list[int] = []
    for field in reversed(fields[1:]):
        try:
            numbers.append(int(field))
        except ValueError:
            if numbers:
                break
            continue
        if len(numbers) == 2:
            used, quota = numbers
            return quota, used
    return None


def _require_help_commands(output: str, required: set[str], *, utility: str) -> None:
    if not required:
        return
    available: set[str] = set()
    for long_name, aliases in HELP_COMMAND.findall(output):
        available.add(long_name.casefold())
        available.update(alias.strip().casefold() for alias in aliases.split(","))
    missing = sorted(command for command in required if command.casefold() not in available)
    if missing:
        raise CompatibilityError(f"{utility} does not provide required command: {missing[0]}")


def _looks_like_quota_header(fields: list[str]) -> bool:
    first = fields[0].casefold().strip(":")
    return first in {"account", "name", "quota", "usage", "used", "email"}


def _require_mailbox_host(value: str, *, detail: str | None = None) -> str:
    if not is_valid_dns_name(value):
        raise CompatibilityError(detail or f"Invalid zimbraMailHost value: {value}")
    return value


def is_missing_object_error(error: CommandError) -> bool:
    diagnostic = str(error).casefold()
    return any(marker in diagnostic for marker in MISSING_OBJECT_MARKERS)


def is_already_exists_error(error: CommandError) -> bool:
    diagnostic = str(error).casefold()
    return any(marker in diagnostic for marker in ALREADY_EXISTS_MARKERS)


def _batch_line(arguments: list[str]) -> str:
    """Encode argv losslessly for Zimbra StringUtil.parseLine batch syntax."""
    return " ".join(
        "'"
        + value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        + "'"
        for value in arguments
    )
