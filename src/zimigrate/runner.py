"""Command execution using argv arrays with shell execution disabled."""

from __future__ import annotations

import getpass
import logging
import secrets
import subprocess  # nosec B404
import time
from pathlib import Path

from zimigrate.config import EndpointConfig
from zimigrate.errors import CommandError, Interrupted
from zimigrate.interrupt import InterruptController, get_interrupt, stop_process
from zimigrate.models import CommandResult
from zimigrate.ssh import SshSession

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 0.25


class CommandRunner:
    def __init__(
        self,
        endpoint: EndpointConfig,
        *,
        retries: int,
        retry_base_seconds: float,
        session: SshSession | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.retries = retries
        self.retry_base_seconds = retry_base_seconds
        self.session = session

    @property
    def is_remote(self) -> bool:
        return self.session is not None

    def run(
        self,
        arguments: list[str],
        *,
        timeout: int | None = None,
        output_path: Path | None = None,
        input_data: bytes | None = None,
        retryable: bool = False,
        sensitive: bool = False,
    ) -> CommandResult:
        interrupt = get_interrupt()
        attempts = self.retries + 1 if retryable else 1
        last_error: CommandError | None = None
        for attempt in range(1, attempts + 1):
            interrupt.check()
            try:
                return self._run_once(
                    arguments,
                    timeout=timeout,
                    output_path=output_path,
                    input_data=input_data,
                    sensitive=sensitive,
                )
            except Interrupted:
                raise
            except CommandError as exc:
                last_error = exc
                if interrupt.is_set():
                    raise Interrupted("Interrupted by user") from exc
                if attempt == attempts or not exc.retryable:
                    raise
                delay = self.retry_base_seconds * (2 ** (attempt - 1))
                # Full jitter keeps parallel workers from retrying LDAP/SOAP in lockstep.
                delay = delay * (secrets.randbits(32) / 0xFFFFFFFF) if delay else 0.0
                LOGGER.warning(
                    "Command failed; retrying",
                    extra={"attempt": attempt, "max_attempts": attempts, "delay": delay},
                )
                interrupt.wait(delay)
        if last_error is None:
            raise CommandError("Command retry loop ended without a result")
        raise last_error

    def _run_once(
        self,
        arguments: list[str],
        *,
        timeout: int | None,
        output_path: Path | None,
        input_data: bytes | None,
        sensitive: bool,
    ) -> CommandResult:
        interrupt = get_interrupt()
        command = self._transport_command(arguments)
        effective_timeout = self.endpoint.command_timeout_seconds if timeout is None else timeout
        output_stream = None
        process: subprocess.Popen[bytes] | None = None
        try:
            if output_path is not None:
                output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                output_stream = output_path.open("wb")
                stdout_target = output_stream
            else:
                stdout_target = subprocess.PIPE
            process = subprocess.Popen(  # nosec B603
                command,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=stdout_target,
                stderr=subprocess.PIPE,
                env=self.session.process_env() if self.session is not None else None,
                start_new_session=True,
            )
            interrupt.register(process)
            stdout_bytes, stderr_bytes = _communicate_until(
                process,
                timeout=effective_timeout,
                interrupt=interrupt,
                input_data=input_data,
            )
        except Interrupted:
            if process is not None:
                stop_process(process)
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                stop_process(process)
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise CommandError(
                f"Zimbra command timed out after {effective_timeout} seconds",
                retryable=True,
            ) from exc
        except OSError as exc:
            if process is not None:
                stop_process(process)
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise CommandError(f"Cannot execute Zimbra command: {exc}", retryable=True) from exc
        finally:
            if process is not None:
                interrupt.unregister(process)
            if output_stream is not None:
                output_stream.close()

        stdout = (
            ""
            if output_path is not None
            else (stdout_bytes or b"").decode("utf-8", errors="replace")
        )
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        if process is None:
            raise CommandError("Zimbra command failed to start")
        if process.returncode != 0:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            if interrupt.is_set():
                raise Interrupted("Interrupted by user")
            detail = "output redacted" if sensitive else _safe_error(stderr or stdout)
            failure = _classify_failure(stderr or stdout)
            raise CommandError(
                f"Zimbra command failed with exit code {process.returncode}: {detail}",
                returncode=process.returncode,
                retryable=failure == "transient",
                attribute_rejection=failure == "attribute",
            )
        return CommandResult(stdout=stdout, stderr=stderr, returncode=process.returncode)

    def _transport_command(self, arguments: list[str]) -> list[str]:
        command = [*self._privilege_prefix(), "env", "LC_ALL=C", *arguments]
        if self.session is None:
            return command
        return self.session.remote_argv(command)

    def _privilege_prefix(self) -> list[str]:
        if self.session is not None:
            if self.session.user == self.endpoint.zimbra_user:
                return []
            return ["sudo", "-n", "-u", self.endpoint.zimbra_user, "--"]
        if getpass.getuser() == self.endpoint.zimbra_user:
            return []
        return ["sudo", "-n", "-u", self.endpoint.zimbra_user, "--"]


def _communicate_until(
    process: subprocess.Popen[bytes],
    *,
    timeout: int,
    interrupt: InterruptController,
    input_data: bytes | None,
) -> tuple[bytes | None, bytes | None]:
    deadline = time.monotonic() + timeout
    pending_input = input_data
    while True:
        interrupt.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_process(process)
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            result = process.communicate(
                input=pending_input,
                timeout=min(POLL_SECONDS, remaining),
            )
            pending_input = None
            return result
        except subprocess.TimeoutExpired:
            # communicate() retains the already supplied input across a timeout;
            # supplying it again would duplicate the batch command.
            pending_input = None
            continue


def _safe_error(value: str) -> str:
    compact = " ".join(value.strip().split())
    return compact[:2000] if compact else "no diagnostic output"


def _classify_failure(value: str) -> str:
    diagnostic = " ".join(value.casefold().split())
    attribute_markers = (
        "invalid_attr_name",
        "invalid_attr_value",
        "invalid attr name",
        "invalid attr value",
        "undefined attribute type",
        "object class violation",
        "object_class_violation",
        "attribute type undefined",
    )
    if any(marker in diagnostic for marker in attribute_markers):
        return "attribute"
    transient_markers = (
        "connection refused",
        "connection reset",
        "connection timed out",
        "connect timed out",
        "service unavailable",
        "temporarily unavailable",
        "ldap server is unavailable",
        "zclient.io_error",
    )
    return "transient" if any(marker in diagnostic for marker in transient_markers) else "permanent"
