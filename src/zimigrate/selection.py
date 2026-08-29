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
)
CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
DEPENDENCIES = {
    "accounts": {"domains", "cos"},
    "mailboxes": {"accounts", "domains", "cos"},
    "distribution_lists": {"domains"},
}
MAILBOX_CATEGORY = "mailboxes"
WITHOUT_MAILBOXES_CHOICE = "without_mailboxes"
WITHOUT_MAILBOXES_LABEL = "Everything except mailbox data"


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


def without_mailbox_categories(available: set[str], disabled: set[str] | None = None) -> set[str]:
    blocked = disabled or set()
    return {
        category.key
        for category in CATEGORIES
        if category.key != MAILBOX_CATEGORY
        and category.key in available
        and category.key not in blocked
    }


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
    without_mailboxes = without_mailbox_categories(available, set(disabled_reasons))
    shortcut = str(len(CATEGORIES) + 1)
    if without_mailboxes:
        print(f"  {shortcut}. {WITHOUT_MAILBOXES_LABEL}")
        enabled_numbers[shortcut] = WITHOUT_MAILBOXES_CHOICE
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
        selected = set()
        for token in tokens:
            choice = enabled_numbers[token]
            if choice == WITHOUT_MAILBOXES_CHOICE:
                selected.update(without_mailboxes)
            else:
                selected.add(choice)
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


def prompt_import_scope(*, has_domains: bool) -> str:
    print("\nSelect import scope:")
    print("  1. Entire archive [default]")
    if has_domains:
        print("  2. Selected domain(s)")
    print("Enter a number, or press Enter for the entire archive.")
    response = input("Scope: ").strip()
    if not response or response == "1":
        return "full"
    if has_domains and response == "2":
        return "domains"
    raise ConfigurationError(f"Invalid import scope selection: {response or 'empty'}")


def prompt_domain_selection(domains: list[str]) -> list[str]:
    if not domains:
        raise ConfigurationError("This archive contains no domains to select")
    print("\nSelect domain(s) to import:")
    enabled = {str(number): name for number, name in enumerate(domains, start=1)}
    for number, name in enumerate(domains, start=1):
        print(f"  {number}. {name}")
    print("Enter comma-separated numbers.")
    response = input("Domains: ").strip()
    if not response:
        raise ConfigurationError("At least one domain must be selected")
    tokens = {token.strip() for token in response.split(",") if token.strip()}
    invalid = tokens.difference(enabled)
    if invalid:
        raise ConfigurationError(f"Invalid domain selection: {sorted(invalid)[0]}")
    selected = [enabled[token] for token in sorted(tokens, key=int)]
    return selected
