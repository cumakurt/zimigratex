"""CLI and transfer filters for account- or domain-scoped backup and restore."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase

from zimigrate.config import TransferConfig
from zimigrate.errors import ConfigurationError
from zimigrate.models import EntityRecord
from zimigrate.selection import transfer_with_categories

USER_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")
DOMAIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class TargetScope:
    users: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return bool(self.users or self.domains)

    def inferred_domains(self) -> frozenset[str]:
        return frozenset(part for user in self.users if (part := domain_of(user)))

    def all_domains(self) -> frozenset[str]:
        return self.domains | self.inferred_domains()

    def matches_account(self, name: str) -> bool:
        if not self.active:
            return True
        folded = name.casefold()
        if folded in self.users:
            return True
        domain = domain_of(folded)
        return bool(domain and domain in self.domains)

    def matches_domain(self, name: str) -> bool:
        return (not self.active) or name.casefold() in self.all_domains()

    def matches_distribution_list(self, name: str) -> bool:
        if not self.active:
            return True
        folded = name.casefold()
        if folded in self.users:
            return True
        domain = domain_of(folded)
        return bool(domain and domain in self.domains)

    def as_options(self) -> dict[str, list[str]]:
        return {
            "target_users": sorted(self.users),
            "target_domains": sorted(self.domains),
        }


def domain_of(address: str) -> str | None:
    if "@" not in address:
        return None
    _, _, domain = address.rpartition("@")
    return domain.casefold() if domain else None


def expand_cli_values(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or ():
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result


def parse_target_scope(
    users: Iterable[str] | None = None,
    domains: Iterable[str] | None = None,
) -> TargetScope:
    parsed_users = frozenset(_parse_users(expand_cli_values(users)))
    parsed_domains = frozenset(_parse_domains(expand_cli_values(domains)))
    return TargetScope(users=parsed_users, domains=parsed_domains)


def scope_from_transfer(transfer: TransferConfig) -> TargetScope:
    return TargetScope(
        users=frozenset(name.casefold() for name in transfer.target_users),
        domains=frozenset(name.casefold() for name in transfer.target_domains),
    )


def scope_from_mapping(value: object) -> TargetScope:
    if not isinstance(value, dict):
        return TargetScope()
    return parse_target_scope(value.get("target_users"), value.get("target_domains"))


def apply_scope_to_transfer(transfer: TransferConfig, scope: TargetScope) -> TransferConfig:
    updated = replace(
        transfer,
        target_users=tuple(sorted(scope.users)),
        target_domains=tuple(sorted(scope.domains)),
    )
    if not scope.active:
        return updated
    selected = {"domains", "cos", "accounts"}
    if updated.include_mailboxes:
        selected.add("mailboxes")
    if scope.domains and updated.include_distribution_lists:
        selected.add("distribution_lists")
    return transfer_with_categories(updated, selected)


def selected_accounts(
    accounts: Iterable[str],
    transfer: TransferConfig,
) -> list[str]:
    scope = scope_from_transfer(transfer)
    includes = transfer.account_include or ("*",)
    excludes = transfer.account_exclude
    return [
        account
        for account in accounts
        if scope.matches_account(account)
        and any(fnmatchcase(account, pattern) for pattern in includes)
        and not any(fnmatchcase(account, pattern) for pattern in excludes)
    ]


def selected_names(names: Iterable[str], scope: TargetScope, *, kind: str) -> list[str]:
    matcher = {
        "account": scope.matches_account,
        "domain": scope.matches_domain,
        "distribution_list": scope.matches_distribution_list,
    }[kind]
    return [name for name in names if matcher(name)]


def filter_account_records(
    records: list[EntityRecord],
    transfer: TransferConfig,
) -> list[EntityRecord]:
    allowed = set(selected_accounts((record.name for record in records), transfer))
    return [record for record in records if record.name in allowed]


def filter_distribution_records(
    records: list[EntityRecord],
    scope: TargetScope,
) -> list[EntityRecord]:
    return [record for record in records if scope.matches_distribution_list(record.name)]


def filter_domain_records(records: list[EntityRecord], scope: TargetScope) -> list[EntityRecord]:
    if not scope.active:
        return records
    primary = [record for record in records if scope.matches_domain(record.name)]
    primary_ids = {record.source_id for record in primary if record.source_id}
    selected = list(primary)
    seen = {record.name.casefold() for record in selected}
    for record in records:
        if record.name.casefold() in seen:
            continue
        target_id = _alias_target_id(record)
        if target_id and target_id in primary_ids:
            selected.append(record)
            seen.add(record.name.casefold())
    return selected


def filter_cos_records(
    records: list[EntityRecord],
    *,
    accounts: list[EntityRecord],
    domains: list[EntityRecord],
    scope: TargetScope,
) -> list[EntityRecord]:
    if not scope.active:
        return records
    needed: set[str] = set()
    for record in (*accounts, *domains):
        for attribute in ("zimbraCOSId", "zimbraDomainDefaultCOSId"):
            values = record.attributes.get(attribute) or []
            needed.update(values)
    if not needed:
        return []
    return [
        record
        for record in records
        if (record.source_id and record.source_id in needed) or record.name in needed
    ]


def restore_scope_from_export_options(
    transfer: TransferConfig, export_options: object
) -> TransferConfig:
    options = export_options if isinstance(export_options, dict) else {}
    scope = scope_from_mapping(options)
    account_include = options.get("account_include")
    account_exclude = options.get("account_exclude")
    return replace(
        transfer,
        target_users=tuple(sorted(scope.users)),
        target_domains=tuple(sorted(scope.domains)),
        account_include=(
            tuple(account_include)
            if isinstance(account_include, list)
            else transfer.account_include
        ),
        account_exclude=(
            tuple(account_exclude)
            if isinstance(account_exclude, list)
            else transfer.account_exclude
        ),
    )


def parse_bound_scope(detail: str | None) -> TargetScope:
    if not detail:
        return TargetScope()
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return TargetScope()
    return scope_from_mapping(payload)


def _parse_users(values: list[str]) -> list[str]:
    users: list[str] = []
    for value in values:
        folded = value.casefold()
        if not USER_PATTERN.fullmatch(folded):
            raise ConfigurationError(f"Invalid --user value: {value}")
        users.append(folded)
    return users


def _parse_domains(values: list[str]) -> list[str]:
    domains: list[str] = []
    for value in values:
        folded = value.casefold().removeprefix("@")
        if USER_PATTERN.fullmatch(folded) or not DOMAIN_PATTERN.fullmatch(folded):
            raise ConfigurationError(f"Invalid --domain value: {value}")
        domains.append(folded)
    return domains


def _alias_target_id(record: EntityRecord) -> str | None:
    domain_type = (record.attributes.get("zimbraDomainType") or [""])[0].lower()
    target = (record.attributes.get("zimbraDomainAliasTargetId") or [None])[0]
    if domain_type == "alias" or target:
        return target
    return None
