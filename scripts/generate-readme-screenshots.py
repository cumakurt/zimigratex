#!/usr/bin/env python3
"""Generate deterministic Rich dashboard examples used by the README files."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from zimigrate.logging_setup import DashboardRenderable, DashboardState

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "screenshots"


def event(state: DashboardState, message: str, event_name: str, **fields: object) -> None:
    record = logging.LogRecord("zimigrate", logging.INFO, __file__, 0, message, (), None)
    record.event = event_name  # type: ignore[attr-defined]
    for name, value in fields.items():
        setattr(record, name, value)
    state.consume(record)


def render(name: str, state: DashboardState) -> None:
    console = Console(record=True, width=118, color_system="truecolor")
    console.print(DashboardRenderable(state))
    console.save_svg(str(OUTPUT / name), title="zimigrate dashboard")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)

    export = DashboardState("export")
    event(
        export,
        "Source discovery complete",
        "inventory",
        inventory={"Host": "mail01.example.com", "Version": "10.1.2"},
    )
    event(
        export,
        "Checking disk capacity",
        "capacity",
        status="sufficient",
        free="428.6 GiB",
        required="91.2 GiB",
        mailbox_bytes="76.8 GiB",
    )
    event(
        export,
        "Exporting accounts",
        "phase_start",
        phase_kind="account",
        phase_action="export",
        total=248,
    )
    event(
        export,
        "Exporting accounts",
        "entity_start",
        phase_kind="account",
        phase_action="export",
        total=248,
        entity="user@example.com",
    )
    event(
        export,
        "Exporting accounts",
        "phase_progress",
        phase_kind="account",
        phase_action="export",
        total=248,
        current=137,
        entity="user@example.com",
    )
    event(
        export,
        "Exporting mailboxes",
        "phase_start",
        phase_kind="mailbox",
        phase_action="export",
        total=248,
    )
    event(
        export,
        "Exporting mailboxes",
        "entity_start",
        phase_kind="mailbox",
        phase_action="export",
        total=248,
        entity="user@example.com",
    )
    render("export-dashboard.svg", export)

    imported = DashboardState("import")
    event(
        imported,
        "Destination discovery complete",
        "inventory",
        inventory={"Host": "mail02.example.com", "Version": "10.1.2"},
    )
    event(
        imported,
        "Import disk capacity check passed",
        "capacity",
        status="sufficient",
        free="312.4 GiB",
        required="91.2 GiB",
        mailbox_bytes="76.8 GiB",
    )
    event(
        imported,
        "Importing accounts",
        "phase_start",
        phase_kind="account",
        phase_action="import",
        total=248,
    )
    event(
        imported,
        "Importing accounts",
        "phase_progress",
        phase_kind="account",
        phase_action="import",
        total=248,
        current=248,
        entity="user@example.com",
    )
    event(
        imported,
        "Importing mailboxes",
        "phase_start",
        phase_kind="mailbox",
        phase_action="import",
        total=248,
    )
    event(
        imported,
        "Importing mailboxes",
        "phase_progress",
        phase_kind="mailbox",
        phase_action="import",
        total=248,
        current=248,
        entity="user@example.com",
    )
    imported.finish("success")
    render("import-completed.svg", imported)


if __name__ == "__main__":
    main()
