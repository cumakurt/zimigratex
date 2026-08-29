from __future__ import annotations

import logging
import unittest

from zimigrate.progress import PhaseProgress, entity_start_fields


class PhaseProgressTests(unittest.TestCase):
    def test_reports_start_and_completion_counts(self) -> None:
        logger = logging.getLogger("zimigrate.tests.progress")
        with self.assertLogs(logger, level="INFO") as captured:
            progress = PhaseProgress(logger, kind="account", total=2, action="export")
            progress.complete("one@example.com")
            progress.complete("two@example.com", failed=True)
        messages = "\n".join(captured.output)
        self.assertIn("Starting account export (2)", messages)
        self.assertIn("Completed account 1/2: one@example.com", messages)
        self.assertIn("Failed account 2/2: two@example.com", messages)

    def test_empty_phase_is_reported(self) -> None:
        logger = logging.getLogger("zimigrate.tests.progress.empty")
        with self.assertLogs(logger, level="INFO") as captured:
            PhaseProgress(logger, kind="server", total=0, action="export")
        self.assertTrue(any("No server objects to export" in line for line in captured.output))

    def test_entity_start_fields_identify_in_flight_work(self) -> None:
        self.assertEqual(
            entity_start_fields("domain", "example.com", action="export"),
            {
                "event": "entity_start",
                "phase_kind": "domain",
                "phase_action": "export",
                "entity": "example.com",
            },
        )


if __name__ == "__main__":
    unittest.main()
