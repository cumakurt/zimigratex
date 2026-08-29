from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import quote

from zimigrate.config import EndpointConfig
from zimigrate.errors import CommandError, CompatibilityError
from zimigrate.models import Attributes
from zimigrate.runner import CommandRunner

LOGGER = logging.getLogger(__name__)
ZMPROV = "/opt/zimbra/bin/zmprov"
ZMMAILBOX = "/opt/zimbra/bin/zmmailbox"
ZMCONTROL = "/opt/zimbra/bin/zmcontrol"
ZMHOSTNAME = "/opt/zimbra/bin/zmhostname"
ZMVOLUME = "/opt/zimbra/bin/zmvolume"
# ProvUtil follows ldapsearch's double-colon convention for base64-encoded binary
# attributes. Keep the separator out of the value so it can be supplied back to
# zmprov as the actual base64 data on the destination.
ATTRIBUTE_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*)(::?)(?: ?)(.*)$")
QUERY_SAFE = ":/*@"


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
    previous: str | None = None
    for raw_line in output.splitlines():
        if raw_line.startswith("# name "):
            previous = None
            continue
        match = ATTRIBUTE_LINE.fullmatch(raw_line)
        if not match:
            if previous is not None and attributes[previous]:
                attributes[previous][-1] += "\n" + raw_line
            continue
        name, _separator, value = match.groups()
        attributes.setdefault(name, []).append(value)
        previous = name
    return attributes


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
    ) -> None:
        self.endpoint = endpoint
        self.runner = CommandRunner(
            endpoint, retries=retries, retry_base_seconds=retry_base_seconds
        )

    def version(self) -> str:
        result = self.runner.run([ZMCONTROL, "-v"], retryable=True)
        value = " ".join((result.stdout + " " + result.stderr).split())
        if not value:
            raise CompatibilityError("zmcontrol -v returned no version")
        return value

    def preflight(self, *, require_mailbox: bool = False) -> str:
        version = self.version()
        self.runner.run([ZMPROV, "help", "commands"], retryable=True)
        if require_mailbox:
            self.runner.run([ZMMAILBOX, "help", "commands"], retryable=True)
        return version

    def hostname(self) -> str:
        result = self.runner.run([ZMHOSTNAME], retryable=True)
        hostname = result.stdout.strip()
        if not hostname or "\n" in hostname:
            raise CompatibilityError("zmhostname returned an invalid host name")
        return hostname

    def list_domains(self) -> list[str]:
        return self._list("gad", ldap=True)

    def list_cos(self) -> list[str]:
        return self._list("gac", ldap=True)

    def list_accounts(self) -> list[str]:
        return self._list("gaa", ldap=True)

    def list_calendar_resources(self) -> list[str]:
        try:
            return self._list("gacr", ldap=True)
        except CommandError:
            LOGGER.warning(
                "Source does not support getAllCalendarResources; using account attributes"
            )
            return []

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

    def get_global_config(self) -> Attributes:
        result = self._zmprov("gacf", ldap=True, retryable=True, sensitive=True)
        return parse_attributes(result)

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

    def exists(self, kind: str, name: str) -> bool:
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
            self._get(command, name, ldap=True, sensitive=False, retryable=False)
        except CommandError as exc:
            diagnostic = str(exc).lower()
            missing_markers = ("no such", "not found", "no_such", "account.no_such")
            if any(marker in diagnostic for marker in missing_markers):
                return False
            raise
        return True

    def create(self, kind: str, name: str, initial_operations: list[str] | None = None) -> None:
        initial_operations = initial_operations or []
        if kind == "domain":
            self._zmprov("cd", name, *initial_operations, ldap=True)
        elif kind == "cos":
            self._zmprov("cc", name, *initial_operations, ldap=True)
        elif kind == "account":
            self._zmprov(
                "ca",
                name,
                "",
                *initial_operations,
                ldap=True,
                sensitive=True,
                protect_arguments=True,
            )
        elif kind == "calendar_resource":
            self._create_calendar_resource(name, initial_operations)
        elif kind == "distribution_list":
            self._zmprov("cdl", name, *initial_operations, ldap=True)
        elif kind == "dynamic_distribution_list":
            self._create_dynamic_distribution_list(name, initial_operations)
        else:
            raise ValueError(f"Unsupported entity kind: {kind}")

    def flush_cache(self, cache_type: str, name: str) -> None:
        """Reload mailboxd LDAP cache after LDAP-direct (`zmprov -l`) writes.

        Password hashes and account status written with `-l` update OpenLDAP
        immediately, but mailboxd keeps cached entries for
        `ldap_cache_<type>_maxage` (default 15 minutes). `flushCache` is a SOAP
        command and must not use `-l`. A flush failure is not fatal: LDAP already
        has the restored values and cache expiry will catch up.
        """
        try:
            self._zmprov("fc", cache_type, name, ldap=False, retryable=True)
        except CommandError as exc:
            LOGGER.warning(
                "Could not flush %s cache for %s; mailboxd may use cached LDAP data "
                "until ldap_cache_%s_maxage expires",
                cache_type,
                name,
                cache_type,
                extra={"detail": str(exc)},
            )

    def modify(self, kind: str, name: str, operations: list[str], *, sensitive: bool) -> None:
        command = {
            "domain": "md",
            "cos": "mc",
            "account": "ma",
            "calendar_resource": "mcr",
            "distribution_list": "mdl",
            "dynamic_distribution_list": "mdl",
            "global_config": "mcf",
            "server": "ms",
        }[kind]
        if kind == "global_config":
            self._zmprov(
                command,
                *operations,
                ldap=True,
                retryable=True,
                sensitive=sensitive,
                protect_arguments=True,
            )
        else:
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
        self._zmprov("aaa", account, alias, ldap=True)

    def add_distribution_alias(self, distribution_list: str, alias: str) -> None:
        self._zmprov("adla", distribution_list, alias, ldap=True)

    def add_distribution_member(self, distribution_list: str, member: str) -> None:
        self._zmprov("adlm", distribution_list, member, ldap=True)

    def create_alias_domain(self, alias_domain: str, target_domain: str) -> None:
        self._zmprov("cad", alias_domain, target_domain, ldap=True)

    def _create_calendar_resource(self, name: str, operations: list[str]) -> None:
        try:
            self._zmprov(
                "ccr",
                name,
                "",
                *operations,
                ldap=True,
                sensitive=True,
                protect_arguments=True,
            )
            return
        except CommandError as exc:
            legacy = _legacy_calendar_resource_arguments(operations)
            if legacy is None or not _looks_like_syntax_error(exc):
                raise
            LOGGER.warning(
                "createCalendarResource rejected attribute syntax; retrying positional form",
                extra={"entity": name},
            )
            self._zmprov(
                "ccr",
                name,
                "",
                *legacy,
                ldap=True,
                sensitive=True,
                protect_arguments=True,
            )

    def _create_dynamic_distribution_list(self, name: str, operations: list[str]) -> None:
        try:
            self._zmprov("cddl", name, *operations, ldap=True)
            return
        except CommandError as exc:
            if not _looks_like_unknown_command(exc):
                raise
            LOGGER.warning(
                "createDynamicDistributionList is unavailable; creating a group with "
                "zimbraIsDynamicGroup",
                extra={"entity": name},
            )
            extra = operations
            if "zimbraIsDynamicGroup" not in operations:
                extra = [*operations, "zimbraIsDynamicGroup", "TRUE"]
            self._zmprov("cdl", name, *extra, ldap=True)

    def create_signature(self, account: str, name: str) -> None:
        self._zmprov("csig", account, name, ldap=True)

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
        self._zmprov("cid", account, name, ldap=True)

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
            sensitive=True,
            protect_arguments=True,
        )

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
        try:
            self._export_mailbox_to_path(options, url, output_path)
        except CommandError as exc:
            if not lock_mailbox or not _looks_like_unsupported_option(exc, "lock"):
                raise
            LOGGER.warning(
                "Mailbox lock is not supported; exporting without lock",
                extra={"account": account},
            )
            unlocked = mailbox_rest_url(
                archive_format,
                query=query,
                lock=False,
                empty_name=f"mailbox.{archive_format}",
            )
            self._export_mailbox_to_path(options, unlocked, output_path)

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
        # zmmailbox writes status to stdout; -o keeps the archive off that stream.
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.runner.run(
                [ZMMAILBOX, *options, "-t", "0", "getRestURL", "-o", str(output_path), url],
                timeout=self.endpoint.mailbox_timeout_seconds,
                retryable=True,
            )
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def _mailbox_options(self, account: str, mailbox_host: str | None) -> list[str]:
        options = ["-z"]
        if mailbox_host:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", mailbox_host):
                raise CompatibilityError(f"Account {account} has an invalid zimbraMailHost value")
            options.extend(
                [
                    "-u",
                    f"{self.endpoint.mailbox_admin_scheme}://{mailbox_host}:"
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
    ) -> str:
        command_line = [ZMPROV]
        if ldap:
            command_line.append("-l")
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


def _looks_like_quota_header(fields: list[str]) -> bool:
    first = fields[0].casefold().strip(":")
    return first in {"account", "name", "quota", "usage", "used", "email"}


def _legacy_calendar_resource_arguments(operations: list[str]) -> list[str] | None:
    if not operations or len(operations) % 2:
        return None
    display = ""
    resource_type = ""
    remaining: list[str] = []
    for name, value in zip(operations[::2], operations[1::2], strict=True):
        if name == "displayName" and not display:
            display = value
        elif name == "zimbraCalResType" and not resource_type:
            resource_type = value
        else:
            remaining.extend([name, value])
    if not display and not resource_type:
        return None
    return [display or "Calendar Resource", resource_type or "Location", *remaining]


def _looks_like_syntax_error(error: CommandError) -> bool:
    text = str(error).casefold()
    return any(
        marker in text
        for marker in (
            "usage",
            "syntax",
            "invalid attribute",
            "unknown attribute",
            "missing required",
            "wrong number of arguments",
            "too many arguments",
        )
    )


def _looks_like_unknown_command(error: CommandError) -> bool:
    text = str(error).casefold()
    return any(
        marker in text
        for marker in ("unknown command", "invalid command", "no such command", "not a command")
    )


def _looks_like_unsupported_option(error: CommandError, option: str) -> bool:
    text = str(error).casefold()
    if option.casefold() not in text:
        return False
    return any(
        marker in text
        for marker in (
            "unknown parameter",
            "unknown option",
            "invalid argument",
            "invalid option",
            "unrecognized",
            "not supported",
        )
    )


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
