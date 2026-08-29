from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
VISUAL_OPERATIONS = {"export", "import", "verify", "verify-target"}
PLAIN_OUTPUT_ENV = "ZIMIGRATE_PLAIN_OUTPUT"
RECENT_EVENT_LIMIT = 6
PHASE_EVENTS = {"phase_start", "phase_empty", "phase_progress", "entity_start"}
QUIET_EVENTS = {"entity_start", "phase_progress"}
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
ACTION_VERBS = {
    "export": "Exporting",
    "import": "Importing",
    "verify": "Verifying",
}
PHASE_NOUNS = {
    "domain": "domains",
    "account": "accounts",
    "server": "servers",
    "cos": "classes of service",
    "mailbox": "mailboxes",
    "mailbox-artifact": "mailbox artifacts",
    "distribution-list": "distribution lists",
    "distribution-members": "distribution members",
    "global_config": "global configuration",
    "global-config": "global configuration",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, object] = {
            "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name, field_value in record.__dict__.items():
            if name not in STANDARD_FIELDS and not name.startswith("_"):
                value[name] = field_value
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        return json.dumps(value, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname.lower()}: {record.getMessage()}"
        extras = [
            f"{name}={field_value}"
            for name, field_value in record.__dict__.items()
            if name not in STANDARD_FIELDS and not name.startswith("_")
        ]
        return f"{base} ({', '.join(extras)})" if extras else base


class ImmediateStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


@dataclass(slots=True)
class PhaseState:
    kind: str
    action: str
    total: int
    completed: int = 0
    failed: int = 0
    current: str = "Waiting"
    active: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    operation: str
    status: str
    current_step: str
    elapsed_seconds: float
    facts: tuple[tuple[str, str], ...]
    capacity: tuple[tuple[str, str], ...]
    phases: tuple[PhaseState, ...]
    recent: tuple[tuple[int, str], ...]
    objects_completed: int
    objects_total: int


@dataclass(slots=True)
class DashboardState:
    operation: str
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    status: str = "running"
    current_step: str = "Preparing migration"
    facts: dict[str, str] = field(default_factory=dict)
    capacity: dict[str, str] = field(default_factory=dict)
    phases: dict[str, PhaseState] = field(default_factory=dict)
    recent: deque[tuple[int, str]] = field(default_factory=lambda: deque(maxlen=RECENT_EVENT_LIMIT))
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def consume(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        event = getattr(record, "event", None)
        with self._lock:
            self._consume_common_fields(record)
            if event in PHASE_EVENTS:
                self._consume_phase(record, str(event))
            elif event == "inventory":
                self._consume_inventory(record)
            self._update_recent(record, event, message)
            self._refresh_current_step(message, event)

    def finish(self, status: str, *, detail: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.finished_at = time.monotonic()
            if detail:
                self.current_step = detail
                level = logging.ERROR if status == "failed" else logging.WARNING
                self.recent.append((level, detail))
            elif status == "success":
                self.current_step = "Operation completed successfully"
                self.recent.append((logging.INFO, self.current_step))

    def snapshot(self) -> DashboardSnapshot:
        with self._lock:
            end = self.finished_at if self.finished_at is not None else time.monotonic()
            phases = tuple(
                PhaseState(
                    kind=phase.kind,
                    action=phase.action,
                    total=phase.total,
                    completed=phase.completed,
                    failed=phase.failed,
                    current=_format_active(phase.active) or phase.current,
                    active=list(phase.active),
                )
                for phase in self.phases.values()
            )
            return DashboardSnapshot(
                operation=self.operation,
                status=self.status,
                current_step=self.current_step,
                elapsed_seconds=max(0.0, end - self.started_at),
                facts=tuple(self.facts.items()),
                capacity=tuple(self.capacity.items()),
                phases=phases,
                recent=tuple(self.recent),
                objects_completed=sum(phase.completed for phase in phases),
                objects_total=sum(phase.total for phase in phases),
            )

    def _consume_common_fields(self, record: logging.LogRecord) -> None:
        if host := getattr(record, "host", None):
            self.facts["Host"] = str(host)
        if version := getattr(record, "version", None):
            self.facts["Version"] = _short_version(str(version))
        if "disk capacity" not in record.getMessage().casefold():
            return
        labels = {
            "status": "Disk",
            "free": "Free",
            "required": "Required",
            "archive_growth": "Archive",
            "temporary_peak": "Temporary",
            "mailbox_bytes": "Mailbox data",
        }
        for field_name, label in labels.items():
            if value := getattr(record, field_name, None):
                self.capacity[label] = str(value)

    def _consume_inventory(self, record: logging.LogRecord) -> None:
        inventory = getattr(record, "inventory", None)
        if not isinstance(inventory, dict):
            return
        for label, value in inventory.items():
            self.facts[str(label)] = str(value)

    def _consume_phase(self, record: logging.LogRecord, event: str) -> None:
        phase = self._ensure_phase(record)
        entity = str(getattr(record, "entity", "") or "")
        if event == "phase_empty":
            phase.current = "No objects"
            phase.active.clear()
            return
        if event == "phase_start":
            phase.current = "Starting"
            return
        if event == "entity_start":
            if entity and entity not in phase.active:
                phase.active.append(entity)
            phase.current = _format_active(phase.active) or entity or "Working"
            return
        phase.completed = min(phase.total, _safe_int(getattr(record, "current", 0)))
        phase.failed += int(bool(getattr(record, "failed", False)))
        if entity in phase.active:
            phase.active = [name for name in phase.active if name != entity]
        phase.current = _format_active(phase.active) or entity or "Working"

    def _ensure_phase(self, record: logging.LogRecord) -> PhaseState:
        kind = str(getattr(record, "phase_kind", "objects"))
        action = str(getattr(record, "phase_action", self.operation))
        key = f"{action}:{kind}"
        total = _safe_int(getattr(record, "total", 0))
        phase = self.phases.get(key)
        if phase is None:
            phase = PhaseState(kind=kind, action=action, total=total)
            self.phases[key] = phase
        elif total:
            phase.total = max(phase.total, total)
        return phase

    def _update_recent(self, record: logging.LogRecord, event: object, message: str) -> None:
        if record.levelno >= logging.WARNING:
            self.recent.append((record.levelno, message))
            return
        if event in {"phase_start", "phase_empty"}:
            self.recent.append((record.levelno, message))

    def _refresh_current_step(self, message: str, event: object) -> None:
        running = [
            phase
            for phase in self.phases.values()
            if phase.total > 0 and phase.completed < phase.total
        ]
        if running:
            self.current_step = _phase_summary(running[-1])
            return
        completed = [
            phase
            for phase in self.phases.values()
            if phase.total > 0 and phase.completed >= phase.total
        ]
        if completed and event in QUIET_EVENTS:
            self.current_step = _phase_summary(completed[-1])
            return
        if event not in QUIET_EVENTS:
            self.current_step = message


class DashboardRenderable:
    def __init__(self, state: DashboardState) -> None:
        self.state = state

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        snapshot = self.state.snapshot()
        yield _dashboard_panel(snapshot, options.max_width)


class DashboardHandler(logging.Handler):
    def __init__(self, stream: TextIO, operation: str) -> None:
        super().__init__()
        self.state = DashboardState(operation=operation)
        self.renderable = DashboardRenderable(self.state)
        self.console = Console(file=stream, stderr=stream is sys.stderr, highlight=False)
        self.live: Live | None = None
        self.started = False
        self.finished = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.finished or self.started:
                return
            self.live = Live(
                self.renderable,
                console=self.console,
                auto_refresh=True,
                refresh_per_second=4,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start(refresh=True)
            self.started = True

    def pause(self) -> None:
        with self._lock:
            self._stop_live()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.consume(record)
        except Exception:
            self.handleError(record)

    def finish(self, status: str, *, detail: str | None = None) -> None:
        with self._lock:
            if self.finished:
                return
            self.state.finish(status, detail=detail)
            if self.live is not None and self.started:
                self.live.refresh()
            self._stop_live()
            self.finished = True

    def close(self) -> None:
        with self._lock:
            self._stop_live()
            self.finished = True
        super().close()

    def _stop_live(self) -> None:
        if self.live is not None and self.started:
            self.live.stop()
        self.live = None
        self.started = False


class LoggingSession:
    def __init__(self, root: logging.Logger, handler: logging.Handler) -> None:
        self.root = root
        self.handler = handler
        self.visual = isinstance(handler, DashboardHandler)
        self.closed = False

    def start(self) -> None:
        if isinstance(self.handler, DashboardHandler):
            self.handler.start()

    def pause(self) -> None:
        if isinstance(self.handler, DashboardHandler):
            self.handler.pause()

    def finish(self, status: str, *, detail: str | None = None) -> None:
        if isinstance(self.handler, DashboardHandler):
            self.handler.finish(status, detail=detail)

    def close(self) -> None:
        if self.closed:
            return
        self.root.removeHandler(self.handler)
        self.handler.close()
        self.closed = True


def configure_logging(
    *,
    verbose: bool,
    json_logs: bool,
    operation: str | None = None,
) -> LoggingSession:
    if _dashboard_supported(verbose=verbose, json_logs=json_logs, operation=operation):
        stream = _select_dashboard_stream()
        _configure_stream(stream)
        handler: logging.Handler = DashboardHandler(stream, operation or "migration")
    else:
        stream = sys.stderr
        _configure_stream(stream)
        handler = ImmediateStreamHandler(stream)
        handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    return LoggingSession(root, handler)


def _dashboard_supported(
    *,
    verbose: bool,
    json_logs: bool,
    operation: str | None,
) -> bool:
    plain_requested = os.environ.get(PLAIN_OUTPUT_ENV, "").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return bool(
        operation in VISUAL_OPERATIONS
        and (_is_tty(sys.stderr) or _is_tty(sys.stdout))
        and os.environ.get("TERM", "").casefold() != "dumb"
        and not plain_requested
        and not verbose
        and not json_logs
    )


def _select_dashboard_stream() -> TextIO:
    if _is_tty(sys.stderr):
        return sys.stderr
    if _is_tty(sys.stdout):
        return sys.stdout
    return sys.stderr


def _is_tty(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _configure_stream(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        with suppress(OSError, ValueError):
            reconfigure(line_buffering=True)


def _dashboard_panel(snapshot: DashboardSnapshot, width: int) -> Panel:
    content: list[Any] = [_status_table(snapshot), _facts_table(snapshot)]
    if snapshot.capacity:
        content.append(_capacity_table(snapshot.capacity))
    if snapshot.phases:
        content.append(_phase_table(snapshot.phases, compact=width < 90))
    content.append(_recent_table(snapshot.recent))
    content.append(
        Text(
            "Ctrl+C stops active commands safely • rerun the same command to resume",
            style="dim",
            justify="center",
        )
    )
    title = Text.assemble(
        (" zimigrate ", "bold white on blue"),
        (f" {snapshot.operation.upper()} ", "bold cyan"),
    )
    return Panel(
        Group(*content),
        title=title,
        title_align="left",
        border_style=_status_style(snapshot.status),
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _status_table(snapshot: DashboardSnapshot) -> Table:
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(justify="right")
    icon, label = {
        "running": ("●", "RUNNING"),
        "success": ("✓", "COMPLETED"),
        "failed": ("✗", "FAILED"),
        "interrupted": ("■", "INTERRUPTED"),
    }.get(snapshot.status, ("●", snapshot.status.upper()))
    status = Text.assemble((icon + " ", _status_style(snapshot.status)), (label, "bold"))
    elapsed = _format_duration(snapshot.elapsed_seconds)
    right = (
        f"{snapshot.objects_completed}/{snapshot.objects_total}  {elapsed}"
        if snapshot.objects_total
        else elapsed
    )
    table.add_row(status, Text(right, style="bold cyan"))
    table.add_row(
        Text(snapshot.current_step, style="white"),
        Text("objects · elapsed" if snapshot.objects_total else "elapsed", style="dim"),
    )
    return table


def _facts_table(snapshot: DashboardSnapshot) -> Table:
    table = Table.grid(expand=True, padding=(0, 2))
    if not snapshot.facts:
        table.add_row(Text("System discovery in progress", style="dim"))
        return table
    for _ in range(min(3, len(snapshot.facts))):
        table.add_column(ratio=1)
    cells = [
        Text.assemble((label + ": ", "dim"), (value, "bold")) for label, value in snapshot.facts
    ]
    for offset in range(0, len(cells), 3):
        table.add_row(*cells[offset : offset + 3])
    return table


def _capacity_table(capacity: tuple[tuple[str, str], ...]) -> Panel:
    status = dict(capacity).get("Disk", "").casefold()
    value_style = {
        "sufficient": "bold green",
        "warning": "bold yellow",
        "insufficient": "bold red",
    }.get(status, "bold cyan")
    border_style = {
        "sufficient": "green",
        "warning": "yellow",
        "insufficient": "red",
    }.get(status, "cyan")
    table = Table.grid(expand=True, padding=(0, 2))
    for _ in capacity:
        table.add_column(ratio=1)
    table.add_row(
        *[Text.assemble((label + " ", "dim"), (value, value_style)) for label, value in capacity]
    )
    return Panel(table, title="Capacity", border_style=border_style, box=box.SIMPLE)


def _phase_table(phases: tuple[PhaseState, ...], *, compact: bool) -> Panel:
    table = Table(expand=True, box=None, show_header=True, header_style="bold blue")
    table.add_column("Phase", style="bold", no_wrap=True, max_width=24)
    table.add_column("Progress", ratio=2)
    table.add_column("State", no_wrap=True, max_width=12)
    if not compact:
        table.add_column("Current", ratio=2, overflow="ellipsis")
    for phase in phases:
        total = max(phase.total, 1)
        progress = Table.grid(expand=True)
        progress.add_column(ratio=1)
        progress.add_column(width=10, justify="right")
        progress.add_row(
            ProgressBar(total=total, completed=min(phase.completed, total), width=None),
            f"{phase.completed}/{phase.total}",
        )
        state, style = _phase_status(phase)
        row: list[Any] = [
            phase.kind.replace("-", " ").replace("_", " ").title(),
            progress,
            Text(state, style=style),
        ]
        if not compact:
            row.append(Text(phase.current, overflow="ellipsis", no_wrap=True))
        table.add_row(*row)
    return Panel(table, title="Progress", border_style="blue", box=box.SIMPLE)


def _recent_table(recent: tuple[tuple[int, str], ...]) -> Panel:
    table = Table.grid(expand=True)
    table.add_column(width=2)
    table.add_column(ratio=1, overflow="ellipsis")
    if not recent:
        table.add_row("·", Text("No warnings yet", style="dim"))
    for level, message in recent:
        if level >= logging.ERROR:
            marker, style = "✗", "bold red"
        elif level >= logging.WARNING:
            marker, style = "!", "yellow"
        else:
            marker, style = "•", "dim"
        table.add_row(Text(marker, style=style), Text(message, style=style, overflow="ellipsis"))
    return Panel(table, title="Recent activity", border_style="bright_black", box=box.SIMPLE)


def _phase_status(phase: PhaseState) -> tuple[str, str]:
    if phase.total == 0:
        return "SKIPPED", "dim"
    if phase.completed >= phase.total:
        return ("FAILED", "bold red") if phase.failed else ("DONE", "bold green")
    if phase.failed:
        return "ATTENTION", "yellow"
    return "RUNNING", "cyan"


def _phase_summary(phase: PhaseState) -> str:
    verb = ACTION_VERBS.get(phase.action, f"{phase.action.capitalize()}ing")
    noun = PHASE_NOUNS.get(phase.kind, phase.kind.replace("-", " ").replace("_", " "))
    return f"{verb} {noun} ({phase.completed}/{phase.total})"


def _format_active(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) <= 2:
        return ", ".join(names)
    return f"{names[0]}, {names[1]} + {len(names) - 2} more"


def _short_version(value: str) -> str:
    match = VERSION_PATTERN.search(value)
    return match.group(0) if match else value


def _status_style(status: str) -> str:
    return {
        "running": "cyan",
        "success": "green",
        "failed": "red",
        "interrupted": "yellow",
    }.get(status, "blue")


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds_value = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"


def _safe_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
