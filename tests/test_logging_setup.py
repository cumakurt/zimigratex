from __future__ import annotations

import io
import logging
import os
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from rich.console import Console

from zimigrate.logging_setup import (
    PLAIN_OUTPUT_ENV,
    DashboardHandler,
    DashboardRenderable,
    DashboardState,
    ImmediateStreamHandler,
    _dashboard_supported,
    configure_logging,
)


def _record(message: str, level: int = logging.INFO, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="zimigrate.tests.dashboard",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class DashboardSupportTests(unittest.TestCase):
    def test_tty_export_enables_dashboard(self) -> None:
        with _terminal_context():
            self.assertTrue(
                _dashboard_supported(verbose=False, json_logs=False, operation="export")
            )

    def test_tty_remote_export_enables_dashboard(self) -> None:
        with _terminal_context():
            self.assertTrue(
                _dashboard_supported(
                    verbose=False,
                    json_logs=False,
                    operation="remote-export",
                )
            )

    def test_stdout_tty_enables_dashboard_when_stderr_is_piped(self) -> None:
        with _terminal_context(stderr_tty=False, stdout_tty=True):
            self.assertTrue(
                _dashboard_supported(verbose=False, json_logs=False, operation="export")
            )

    def test_verbose_json_plain_status_and_dumb_term_stay_linear(self) -> None:
        with _terminal_context():
            self.assertFalse(
                _dashboard_supported(verbose=True, json_logs=False, operation="export")
            )
            self.assertFalse(
                _dashboard_supported(verbose=False, json_logs=True, operation="export")
            )
            self.assertFalse(
                _dashboard_supported(verbose=False, json_logs=False, operation="status")
            )
        with _terminal_context(term="dumb"):
            self.assertFalse(
                _dashboard_supported(verbose=False, json_logs=False, operation="export")
            )
        with _terminal_context(plain="1"):
            self.assertFalse(
                _dashboard_supported(verbose=False, json_logs=False, operation="export")
            )

    def test_configure_logging_selects_dashboard_or_plain_handler(self) -> None:
        with _terminal_context():
            session = configure_logging(verbose=False, json_logs=False, operation="export")
            try:
                self.assertTrue(session.visual)
                self.assertIsInstance(session.handler, DashboardHandler)
            finally:
                session.close()
        with _terminal_context():
            session = configure_logging(verbose=True, json_logs=False, operation="export")
            try:
                self.assertFalse(session.visual)
                self.assertIsInstance(session.handler, ImmediateStreamHandler)
            finally:
                session.close()


class DashboardStateTests(unittest.TestCase):
    def test_phase_and_inventory_update_progress_without_log_noise(self) -> None:
        state = DashboardState("export")
        state.consume(
            _record(
                "Source preflight passed",
                event="preflight",
                host="mail.example.com",
                version="Release 10.1.18.GA.4200001.UBUNTU24.64 FOSS edition.",
            )
        )
        state.consume(
            _record(
                "Found 31 domain(s)",
                event="inventory",
                inventory={"Domains": 31, "Accounts": 159},
            )
        )
        state.consume(
            _record(
                "Export disk capacity check passed",
                status="sufficient",
                free="1.27 TiB",
                required="1.29 GiB",
                archive_growth="295.82 MiB",
            )
        )
        state.consume(
            _record(
                "Starting domain export (31)",
                event="phase_start",
                phase_kind="domain",
                phase_action="export",
                total=31,
            )
        )
        state.consume(
            _record(
                "Exporting domain example.com",
                event="entity_start",
                phase_kind="domain",
                phase_action="export",
                entity="example.com",
            )
        )
        state.consume(_record("Exporting domain ignored.example.com"))
        state.consume(
            _record(
                "Completed domain 1/31: example.com",
                event="phase_progress",
                phase_kind="domain",
                phase_action="export",
                current=1,
                total=31,
                entity="example.com",
                failed=False,
            )
        )
        state.consume(
            _record(
                "Exporting domain other.com",
                event="entity_start",
                phase_kind="domain",
                phase_action="export",
                entity="other.com",
            )
        )
        snapshot = state.snapshot()
        self.assertEqual(dict(snapshot.facts)["Host"], "mail.example.com")
        self.assertEqual(dict(snapshot.facts)["Version"], "10.1.18")
        self.assertEqual(dict(snapshot.facts)["Domains"], "31")
        self.assertEqual(dict(snapshot.capacity)["Disk"], "sufficient")
        self.assertEqual(dict(snapshot.capacity)["Free"], "1.27 TiB")
        self.assertEqual(snapshot.current_step, "Exporting domains (1/31)")
        self.assertEqual(len(snapshot.phases), 1)
        self.assertEqual(snapshot.phases[0].completed, 1)
        self.assertEqual(snapshot.phases[0].current, "other.com")
        self.assertEqual(snapshot.phases[0].active, ["other.com"])
        self.assertEqual(snapshot.objects_completed, 1)
        self.assertEqual(snapshot.objects_total, 31)
        recent_messages = [message for _level, message in snapshot.recent]
        self.assertEqual(recent_messages, ["Starting domain export (31)"])
        self.assertNotIn("Exporting domain ignored.example.com", recent_messages)
        self.assertNotIn("Exporting domain example.com", recent_messages)

    def test_warnings_are_kept_in_recent_activity(self) -> None:
        state = DashboardState("export")
        state.consume(_record("Working on export"))
        state.consume(_record("Disk is nearly full", logging.WARNING))
        snapshot = state.snapshot()
        self.assertEqual(snapshot.recent, ((logging.WARNING, "Disk is nearly full"),))

    def test_renderable_exports_without_raising(self) -> None:
        state = DashboardState("export")
        state.consume(
            _record(
                "Starting account export (2)",
                event="phase_start",
                phase_kind="account",
                phase_action="export",
                total=2,
            )
        )
        buffer = io.StringIO()
        Console(file=buffer, width=100, force_terminal=True, color_system=None).print(
            DashboardRenderable(state)
        )
        output = buffer.getvalue()
        self.assertIn("EXPORT", output)
        self.assertIn("RUNNING", output)
        self.assertIn("Account", output)
        self.assertIn("0/2", output)


class DashboardHandlerTests(unittest.TestCase):
    def test_emit_updates_state_without_starting_live(self) -> None:
        handler = DashboardHandler(io.StringIO(), "export")
        with patch("zimigrate.logging_setup.Live") as live_cls:
            handler.emit(
                _record(
                    "Starting domain export (2)",
                    event="phase_start",
                    phase_kind="domain",
                    phase_action="export",
                    total=2,
                )
            )
            live_cls.assert_not_called()
            handler.start()
            live_cls.assert_called_once()
            live_cls.return_value.start.assert_called_once()
            handler.pause()
            live_cls.return_value.stop.assert_called_once()
            handler.start()
            self.assertEqual(live_cls.call_count, 2)
            handler.finish("success")
        snapshot = handler.state.snapshot()
        self.assertEqual(snapshot.status, "success")
        self.assertEqual(snapshot.current_step, "Operation completed successfully")


def _terminal_context(
    *,
    term: str = "xterm",
    plain: str = "",
    stderr_tty: bool = True,
    stdout_tty: bool = False,
):
    stderr = MagicMock()
    stderr.isatty.return_value = stderr_tty
    stdout = MagicMock()
    stdout.isatty.return_value = stdout_tty
    stack = ExitStack()
    stack.enter_context(patch("zimigrate.logging_setup.sys.stderr", stderr))
    stack.enter_context(patch("zimigrate.logging_setup.sys.stdout", stdout))
    stack.enter_context(
        patch.dict(os.environ, {"TERM": term, PLAIN_OUTPUT_ENV: plain}, clear=False)
    )
    return stack


if __name__ == "__main__":
    unittest.main()
