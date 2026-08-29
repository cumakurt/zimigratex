from __future__ import annotations

import getpass
import sys
import unittest
from unittest.mock import patch

from zimigrate.config import EndpointConfig
from zimigrate.errors import CommandError, Interrupted
from zimigrate.interrupt import get_interrupt
from zimigrate.runner import CommandRunner


class InterruptRunnerTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_interrupt().clear()

    def test_retry_stops_when_interrupt_is_requested(self) -> None:
        runner = CommandRunner(EndpointConfig(), retries=3, retry_base_seconds=0)
        get_interrupt().request()
        with (
            patch.object(runner, "_run_once", side_effect=CommandError("failed")) as run_once,
            self.assertRaises(Interrupted),
        ):
            runner.run(["/opt/zimbra/bin/zmprov", "gaa"], retryable=True)
        run_once.assert_not_called()

    def test_stdin_batch_survives_multiple_poll_intervals(self) -> None:
        runner = CommandRunner(
            EndpointConfig(zimbra_user=getpass.getuser(), command_timeout_seconds=3),
            retries=0,
            retry_base_seconds=0,
        )

        result = runner.run(
            [
                sys.executable,
                "-c",
                "import sys,time; data=sys.stdin.buffer.read(); time.sleep(.4); "
                "sys.stdout.buffer.write(data)",
            ],
            input_data=b"batch-secret\n",
        )

        self.assertEqual(result.stdout, "batch-secret\n")
