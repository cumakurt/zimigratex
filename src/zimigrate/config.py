from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

from zimigrate.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    zimbra_user: str = "zimbra"
    command_timeout_seconds: int = 300
    mailbox_timeout_seconds: int = 14_400
    mailbox_admin_scheme: str = "https"
    mailbox_admin_port: int = 7071

    def validate(self, label: str) -> None:
        if self.mailbox_admin_scheme not in {"http", "https"}:
            raise ConfigurationError(f"{label}.mailbox_admin_scheme must be 'http' or 'https'")
        if not 1 <= self.mailbox_admin_port <= 65535:
            raise ConfigurationError(f"{label}.mailbox_admin_port is invalid")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", self.zimbra_user):
            raise ConfigurationError(f"{label}.zimbra_user contains unsafe characters")
        if self.command_timeout_seconds < 1:
            raise ConfigurationError(f"{label}.command_timeout_seconds must be positive")
        if self.mailbox_timeout_seconds < 1:
            raise ConfigurationError(f"{label}.mailbox_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    """Archive layout options. Encryption is not supported."""

    def validate(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class TransferConfig:
    workers: int = 8
    retries: int = 3
    retry_base_seconds: float = 1.0
    include_domains: bool = True
    include_cos: bool = True
    include_accounts: bool = True
    include_mailboxes: bool = True
    include_distribution_lists: bool = True
    include_global_config: bool = True
    include_server_config: bool = True
    include_system_mailboxes: bool = False
    include_secrets: bool = True
    account_include: tuple[str, ...] = ("*",)
    account_exclude: tuple[str, ...] = ()
    target_users: tuple[str, ...] = ()
    target_domains: tuple[str, ...] = ()
    mailbox_mode: str = "full"
    mailbox_format: str = "zip"
    mailbox_lock: bool = True
    mailbox_start_year: int = 1970
    mailbox_chunk_years: int = 5

    def validate(self) -> None:
        if not 1 <= self.workers <= 64:
            raise ConfigurationError("transfer.workers must be between 1 and 64")
        if not 0 <= self.retries <= 10:
            raise ConfigurationError("transfer.retries must be between 0 and 10")
        if not isfinite(self.retry_base_seconds) or self.retry_base_seconds < 0:
            raise ConfigurationError("transfer.retry_base_seconds must be finite and non-negative")
        if self.mailbox_mode not in {"full", "year-chunks"}:
            raise ConfigurationError("transfer.mailbox_mode must be 'full' or 'year-chunks'")
        if self.mailbox_format not in {"zip", "tgz"}:
            raise ConfigurationError("transfer.mailbox_format must be 'zip' or 'tgz'")
        if not 1900 <= self.mailbox_start_year <= 2100:
            raise ConfigurationError("transfer.mailbox_start_year must be between 1900 and 2100")
        if not 1 <= self.mailbox_chunk_years <= 25:
            raise ConfigurationError("transfer.mailbox_chunk_years must be between 1 and 25")
        from zimigrate.scope import parse_target_scope

        parse_target_scope(self.target_users, self.target_domains)


@dataclass(frozen=True, slots=True)
class ImportConfig:
    expected_target_version_pattern: str = ""
    existing_policy: str = "merge"
    mailbox_conflict_resolution: str = "skip"
    strict_attributes: bool = True
    apply_global_config: bool = False
    global_attribute_allowlist: tuple[str, ...] = ()
    apply_server_config: bool = False
    server_attribute_allowlist: tuple[str, ...] = ()
    server_map: dict[str, str] = field(default_factory=dict)
    mailhost_map: dict[str, str] = field(default_factory=dict)
    default_mailhost: str | None = None
    import_system_accounts: bool = False
    allow_sensitive_config: bool = False

    def allows_version(self, version: str) -> bool:
        pattern = self.expected_target_version_pattern.strip()
        if not pattern:
            return True
        return re.search(pattern, version) is not None

    def validate(self) -> None:
        try:
            re.compile(self.expected_target_version_pattern)
        except re.error as exc:
            raise ConfigurationError(f"Invalid target version pattern: {exc}") from exc
        if self.existing_policy not in {"merge", "skip", "fail"}:
            raise ConfigurationError("import.existing_policy must be merge, skip, or fail")
        if self.mailbox_conflict_resolution not in {"skip", "modify", "replace", "reset"}:
            raise ConfigurationError(
                "import.mailbox_conflict_resolution must be skip, modify, replace, or reset"
            )
        if self.apply_global_config and not self.global_attribute_allowlist:
            raise ConfigurationError(
                "import.global_attribute_allowlist is required when global config import is enabled"
            )
        if self.apply_server_config and not self.server_attribute_allowlist:
            raise ConfigurationError(
                "import.server_attribute_allowlist is required when server config import is enabled"
            )
        mailhosts = [
            *self.mailhost_map.keys(),
            *self.mailhost_map.values(),
            *self.server_map.keys(),
            *self.server_map.values(),
        ]
        if self.default_mailhost:
            mailhosts.append(self.default_mailhost)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", host) for host in mailhosts):
            raise ConfigurationError("import mailhost names contain unsafe characters")


@dataclass(frozen=True, slots=True)
class AppConfig:
    source: EndpointConfig
    target: EndpointConfig
    archive: ArchiveConfig
    transfer: TransferConfig
    import_options: ImportConfig

    def validate(self) -> None:
        self.source.validate("source")
        self.target.validate("target")
        self.archive.validate()
        self.transfer.validate()
        self.import_options.validate()


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        raw: dict[str, Any] = {}
    else:
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Cannot load configuration {path}: {exc}") from exc

    _reject_unknown(raw, {"source", "target", "archive", "transfer", "import"}, "root")
    config = AppConfig(
        source=_endpoint(_table(raw, "source"), "source"),
        target=_endpoint(_table(raw, "target"), "target"),
        archive=_archive(_table(raw, "archive")),
        transfer=_transfer(_table(raw, "transfer")),
        import_options=_import(_table(raw, "import")),
    )
    config.validate()
    return config


def _endpoint(raw: dict[str, Any], label: str) -> EndpointConfig:
    remote_options = {
        "host",
        "ssh_user",
        "ssh_port",
        "identity_file",
        "known_hosts_file",
        "strict_host_key_checking",
        "connect_timeout_seconds",
    }
    allowed = remote_options | {
        "mode",
        "zimbra_user",
        "command_timeout_seconds",
        "mailbox_timeout_seconds",
        "mailbox_admin_scheme",
        "mailbox_admin_port",
    }
    _reject_unknown(raw, allowed, label)
    mode = _string(raw, "mode", "local", label)
    configured_remote_options = sorted(remote_options.intersection(raw))
    if mode != "local" or configured_remote_options:
        details = ", ".join(configured_remote_options)
        suffix = f" ({details})" if details else ""
        raise ConfigurationError(
            f"Remote endpoint settings are not supported; run zimigrate on each Zimbra "
            f"server and use local mode{suffix}"
        )
    return EndpointConfig(
        zimbra_user=_string(raw, "zimbra_user", "zimbra", label),
        command_timeout_seconds=_integer(raw, "command_timeout_seconds", 300, label),
        mailbox_timeout_seconds=_integer(raw, "mailbox_timeout_seconds", 14_400, label),
        mailbox_admin_scheme=_string(raw, "mailbox_admin_scheme", "https", label),
        mailbox_admin_port=_integer(raw, "mailbox_admin_port", 7071, label),
    )


REMOVED_ARCHIVE_KEYS = {"encryption_enabled", "passphrase_env", "allow_unencrypted"}


def _archive(raw: dict[str, Any]) -> ArchiveConfig:
    if removed := sorted(REMOVED_ARCHIVE_KEYS.intersection(raw)):
        raise ConfigurationError(
            "Archive encryption was removed; drop "
            f"{removed[0]} from the [archive] configuration table"
        )
    _reject_unknown(raw, set(), "archive")
    return ArchiveConfig()


def _transfer(raw: dict[str, Any]) -> TransferConfig:
    allowed = {
        "workers",
        "retries",
        "retry_base_seconds",
        "include_domains",
        "include_cos",
        "include_accounts",
        "include_mailboxes",
        "include_distribution_lists",
        "include_global_config",
        "include_server_config",
        "include_system_mailboxes",
        "include_secrets",
        "account_include",
        "account_exclude",
        "target_users",
        "target_domains",
        "mailbox_mode",
        "mailbox_format",
        "mailbox_lock",
        "mailbox_start_year",
        "mailbox_chunk_years",
    }
    _reject_unknown(raw, allowed, "transfer")
    return TransferConfig(
        workers=_integer(raw, "workers", 8, "transfer"),
        retries=_integer(raw, "retries", 3, "transfer"),
        retry_base_seconds=_number(raw, "retry_base_seconds", 1.0, "transfer"),
        include_domains=_boolean(raw, "include_domains", True, "transfer"),
        include_cos=_boolean(raw, "include_cos", True, "transfer"),
        include_accounts=_boolean(raw, "include_accounts", True, "transfer"),
        include_mailboxes=_boolean(raw, "include_mailboxes", True, "transfer"),
        include_distribution_lists=_boolean(raw, "include_distribution_lists", True, "transfer"),
        include_global_config=_boolean(raw, "include_global_config", True, "transfer"),
        include_server_config=_boolean(raw, "include_server_config", True, "transfer"),
        include_system_mailboxes=_boolean(raw, "include_system_mailboxes", False, "transfer"),
        include_secrets=_boolean(raw, "include_secrets", True, "transfer"),
        account_include=_string_tuple(raw, "account_include", ("*",), "transfer"),
        account_exclude=_string_tuple(raw, "account_exclude", (), "transfer"),
        target_users=_string_tuple(raw, "target_users", (), "transfer"),
        target_domains=_string_tuple(raw, "target_domains", (), "transfer"),
        mailbox_mode=_string(raw, "mailbox_mode", "full", "transfer"),
        mailbox_format=_string(raw, "mailbox_format", "zip", "transfer"),
        mailbox_lock=_boolean(raw, "mailbox_lock", True, "transfer"),
        mailbox_start_year=_integer(raw, "mailbox_start_year", 1970, "transfer"),
        mailbox_chunk_years=_integer(raw, "mailbox_chunk_years", 5, "transfer"),
    )


def _import(raw: dict[str, Any]) -> ImportConfig:
    allowed = {
        "expected_target_version_pattern",
        "existing_policy",
        "mailbox_conflict_resolution",
        "strict_attributes",
        "apply_global_config",
        "global_attribute_allowlist",
        "apply_server_config",
        "server_attribute_allowlist",
        "server_map",
        "mailhost_map",
        "default_mailhost",
        "import_system_accounts",
        "allow_sensitive_config",
    }
    _reject_unknown(raw, allowed, "import")
    server_map = raw.get("server_map", {})
    if not isinstance(server_map, dict):
        raise ConfigurationError("import.server_map must be a TOML table")
    mailhost_map = raw.get("mailhost_map", {})
    if not isinstance(mailhost_map, dict):
        raise ConfigurationError("import.mailhost_map must be a TOML table")
    return ImportConfig(
        expected_target_version_pattern=_string(
            raw,
            "expected_target_version_pattern",
            "",
            "import",
        ),
        existing_policy=_string(raw, "existing_policy", "merge", "import"),
        mailbox_conflict_resolution=_string(raw, "mailbox_conflict_resolution", "skip", "import"),
        strict_attributes=_boolean(raw, "strict_attributes", True, "import"),
        apply_global_config=_boolean(raw, "apply_global_config", False, "import"),
        global_attribute_allowlist=_string_tuple(raw, "global_attribute_allowlist", (), "import"),
        apply_server_config=_boolean(raw, "apply_server_config", False, "import"),
        server_attribute_allowlist=_string_tuple(raw, "server_attribute_allowlist", (), "import"),
        server_map=_string_mapping(server_map, "import.server_map"),
        mailhost_map=_string_mapping(mailhost_map, "import.mailhost_map"),
        default_mailhost=_optional_string(raw.get("default_mailhost"), "import.default_mailhost"),
        import_system_accounts=_boolean(raw, "import_system_accounts", False, "import"),
        allow_sensitive_config=_boolean(raw, "allow_sensitive_config", False, "import"),
    )


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a TOML table")
    return value


def _reject_unknown(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    if unknown := sorted(set(raw).difference(allowed)):
        raise ConfigurationError(f"Unknown {label} configuration key: {unknown[0]}")


def _boolean(raw: dict[str, Any], name: str, default: bool, label: str) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label}.{name} must be a boolean")
    return value


def _integer(raw: dict[str, Any], name: str, default: int, label: str) -> int:
    value = raw.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"{label}.{name} must be an integer")
    return value


def _number(raw: dict[str, Any], name: str, default: float, label: str) -> float:
    value = raw.get(name, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError(f"{label}.{name} must be a number")
    return float(value)


def _string(raw: dict[str, Any], name: str, default: str, label: str) -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise ConfigurationError(f"{label}.{name} must be a string")
    return value


def _string_tuple(
    raw: dict[str, Any], name: str, default: tuple[str, ...], label: str
) -> tuple[str, ...]:
    value = raw.get(name, list(default))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label}.{name} must be an array of strings")
    return tuple(value)


def _string_mapping(value: dict[object, object], label: str) -> dict[str, str]:
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise ConfigurationError(f"{label} keys and values must be strings")
    return dict(value)


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a string")
    return value
