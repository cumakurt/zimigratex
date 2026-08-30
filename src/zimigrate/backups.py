"""Discover local zimigrate export archives for interactive import."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zimigrate.archive import SCHEMA_VERSION
from zimigrate.errors import ArchiveError, ConfigurationError
from zimigrate.selection import exported_categories
from zimigrate.util import read_json

SKIP_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    ".runtime",
    ".cursor",
    "__pycache__",
    "src",
    "tests",
    "scripts",
    "vendor",
    "tasks",
    "docs",
    "canvases",
}


@dataclass(frozen=True, slots=True)
class BackupSummary:
    path: Path
    completed: bool
    source_host: str
    source_version: str
    updated_at: str
    categories: tuple[str, ...]
    counts: dict[str, int]
    domains: tuple[str, ...]

    @property
    def account_count(self) -> int:
        return int(self.counts.get("account", 0)) + int(self.counts.get("calendar_resource", 0))


def discover_backups(root: Path) -> list[BackupSummary]:
    summaries: list[BackupSummary] = []
    seen: set[Path] = set()
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return []
    for child in children:
        if child.name in SKIP_DIRECTORY_NAMES:
            continue
        try:
            if not child.is_dir():
                continue
            summary = summarize_backup(child)
        except OSError:
            # An unrelated root-owned or disconnected mount must not make the
            # interactive archive picker unusable.
            continue
        if summary is None:
            continue
        resolved = summary.path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        summaries.append(summary)
    return summaries


def summarize_backup(path: Path) -> BackupSummary | None:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file() or not (path / "state.sqlite3").is_file():
        return None
    try:
        manifest = read_json(manifest_path)
    except ArchiveError:
        return None
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return None
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    normalized_counts = {
        str(key): int(value)
        for key, value in counts.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }
    categories = tuple(sorted(exported_categories(manifest.get("export_options"))))
    return BackupSummary(
        path=path.resolve(),
        completed=bool(manifest.get("completed")),
        source_host=_string(manifest.get("source_host")),
        source_version=_string(manifest.get("source_version")),
        updated_at=_string(manifest.get("updated_at")),
        categories=categories,
        counts=normalized_counts,
        domains=_domain_names(path),
    )


def prompt_backup_choice(
    backups: list[BackupSummary],
    *,
    default: Path | None = None,
) -> Path:
    if not backups:
        raise ConfigurationError(
            "No export archives were found in the current directory; "
            "pass --archive or copy an export_data directory here"
        )
    print("\nAvailable export archives:")
    default_resolved = default.resolve() if default is not None and default.exists() else None
    for number, backup in enumerate(backups, start=1):
        marker = " [default]" if default_resolved == backup.path else ""
        status = "complete" if backup.completed else "incomplete"
        host = backup.source_host or "unknown host"
        print(f"  {number}. {backup.path.name}{marker}")
        print(f"      {status}; {host}; updated {backup.updated_at or 'unknown'}")
        print(f"      {_inventory_line(backup)}")
        if backup.domains:
            shown = ", ".join(backup.domains[:8])
            extra = "" if len(backup.domains) <= 8 else f" (+{len(backup.domains) - 8} more)"
            print(f"      domains: {shown}{extra}")
    print("Enter a number, or press Enter for the default archive.")
    response = input("Archive: ").strip()
    if not response:
        if default_resolved:
            for backup in backups:
                if backup.path == default_resolved:
                    return backup.path
        return backups[0].path
    if response not in {str(number) for number in range(1, len(backups) + 1)}:
        raise ConfigurationError(f"Invalid archive selection: {response}")
    return backups[int(response) - 1].path


def _inventory_line(backup: BackupSummary) -> str:
    parts = [
        f"domains {backup.counts.get('domain', 0)}",
        f"accounts {backup.account_count}",
        "lists "
        + str(
            backup.counts.get("distribution_list", 0)
            + backup.counts.get("dynamic_distribution_list", 0)
        ),
    ]
    if "mailboxes" in backup.categories:
        parts.append("mailbox data yes")
    else:
        parts.append("mailbox data no")
    if backup.categories:
        parts.append("categories " + ",".join(backup.categories))
    return "; ".join(parts)


def _domain_names(path: Path) -> tuple[str, ...]:
    directory = path / "objects" / "domain"
    if not directory.is_dir():
        return ()
    names: list[str] = []
    for child in sorted(directory.glob("*.json")):
        try:
            payload = read_json(child)
        except ArchiveError:
            continue
        name = payload.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
