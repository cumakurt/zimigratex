from __future__ import annotations

from dataclasses import dataclass, replace

from zimigrate.config import TransferConfig
from zimigrate.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class Category:
    key: str
    field: str
    label: str


CATEGORIES = (
    Category("domains", "include_domains", "Domains and alias domains"),
    Category("cos", "include_cos", "Classes of service (COS)"),
    Category(
        "accounts",
        "include_accounts",
        "Accounts, passwords, resources, identities, signatures, and preferences",
    ),
    Category("mailboxes", "include_mailboxes", "Mailbox messages and item data"),
    Category(
        "distribution_lists",
        "include_distribution_lists",
        "Static and dynamic distribution lists",
    ),
    Category("global_config", "include_global_config", "Global configuration snapshot"),
    Category("server_config", "include_server_config", "Per-server configuration snapshots"),
)
CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
DEPENDENCIES = {
    "accounts": {"domains", "cos"},
    "mailboxes": {"accounts", "domains", "cos"},
    "distribution_lists": {"domains"},
}


def all_categories() -> set[str]:
    return set(CATEGORY_BY_KEY)


def selected_categories(transfer: TransferConfig) -> set[str]:
    return {category.key for category in CATEGORIES if bool(getattr(transfer, category.field))}


def transfer_with_categories(
    transfer: TransferConfig,
    selected: set[str],
) -> TransferConfig:
    normalized = normalize_categories(selected)
    updates = {category.field: category.key in normalized for category in CATEGORIES}
    return replace(transfer, **updates)


def normalize_categories(selected: set[str]) -> set[str]:
    unknown = selected.difference(CATEGORY_BY_KEY)
    if unknown:
        raise ConfigurationError(f"Unknown migration category: {sorted(unknown)[0]}")
    normalized = set(selected)
    while True:
        dependencies = {
            dependency
            for category in normalized
            for dependency in DEPENDENCIES.get(category, set())
        }
        expanded = normalized | dependencies
        if expanded == normalized:
            return normalized
        normalized = expanded


def exported_categories(export_options: object) -> set[str]:
    options = export_options if isinstance(export_options, dict) else {}
    selected: set[str] = set()
    for category in CATEGORIES:
        if bool(options.get(category.field, True)):
            selected.add(category.key)
    return normalize_categories(selected)


def prompt_categories(
    action: str,
    *,
    available: set[str],
    defaults: set[str],
    disabled_reasons: dict[str, str] | None = None,
) -> set[str]:
    disabled_reasons = disabled_reasons or {}
    print(f"\nSelect data categories to {action}:")
    enabled_numbers: dict[str, str] = {}
    for number, category in enumerate(CATEGORIES, start=1):
        if category.key not in available:
            continue
        suffix = ""
        if reason := disabled_reasons.get(category.key):
            suffix = f" [disabled: {reason}]"
        elif category.key in defaults:
            suffix = " [default]"
            enabled_numbers[str(number)] = category.key
        else:
            enabled_numbers[str(number)] = category.key
        print(f"  {number}. {category.label}{suffix}")
    print("Enter comma-separated numbers, or press Enter for all available defaults.")
    response = input("Selection: ").strip().lower()
    if not response or response == "all":
        selected = set(defaults).difference(disabled_reasons)
    else:
        tokens = {token.strip() for token in response.split(",") if token.strip()}
        invalid = tokens.difference(enabled_numbers)
        if invalid:
            raise ConfigurationError(
                f"Invalid or disabled category selection: {sorted(invalid)[0]}"
            )
        selected = {enabled_numbers[token] for token in tokens}
    if not selected:
        raise ConfigurationError("At least one migration category must be selected")
    normalized = normalize_categories(selected)
    unavailable_dependencies = normalized.difference(available)
    if unavailable_dependencies:
        raise ConfigurationError(
            "Selected categories require unavailable data: "
            + ", ".join(sorted(unavailable_dependencies))
        )
    blocked_dependencies = normalized.intersection(disabled_reasons)
    if blocked_dependencies:
        raise ConfigurationError(
            "Selected categories require disabled data: " + ", ".join(sorted(blocked_dependencies))
        )
    added = normalized.difference(selected)
    if added:
        print("Automatically included dependencies: " + ", ".join(sorted(added)))
    return normalized
