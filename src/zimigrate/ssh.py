"""Opt-in SSH session for streaming remote Zimbra commands."""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from zimigrate.errors import ConfigurationError, Interrupted, ZimigrateError
from zimigrate.interrupt import get_interrupt
from zimigrate.util import is_valid_ssh_target

LOGGER = logging.getLogger(__name__)
SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
REMOTE_NAME_PATTERN = re.compile(r"^[\w.*?-]+$")
CONNECT_TIMEOUT_SECONDS = 20


class SshSession:
    def __init__(
        self,
        host: str,
        *,
        user: str = "root",
        password: str | None = None,
        port: int = 22,
    ) -> None:
        if not is_valid_ssh_target(host):
            raise ConfigurationError(f"Invalid SSH host: {host}")
        if not SSH_USER.fullmatch(user):
            raise ConfigurationError("SSH username contains unsafe characters")
        if not 1 <= port <= 65535:
            raise ConfigurationError("SSH port is invalid")
        self.host = host
        self.user = user
        self.port = port
        self._password = password
        self._password_file: Path | None = None
        self._askpass_wrapper: Path | None = None
        self._control_dir: Path | None = None
        self._control_path: Path | None = None
        self._master_started = False
        ssh = shutil.which("ssh")
        if ssh is None:
            raise ZimigrateError("Remote export requires the local ssh command")
        self._ssh = ssh
        self._rsync = shutil.which("rsync")

    @property
    def auth_method(self) -> str:
        return "password" if self._password else "key"

    def connect(self) -> None:
        self._control_dir = Path(
            tempfile.mkdtemp(prefix="zimigrate-ssh-", dir=tempfile.gettempdir())
        )
        os.chmod(self._control_dir, 0o700)
        self._control_path = self._control_dir / "mux"
        if self._password is not None:
            self._password_file = _write_password_file(self._password)
            self._askpass_wrapper = _write_askpass_wrapper()
        try:
            self._invoke(
                [self._ssh, *self._ssh_options(master="yes"), self._destination(), "true"],
                tty=False,
            )
        except Exception:
            self.close()
            raise
        self._master_started = True
        LOGGER.info(
            "SSH session established",
            extra={"host": self.host, "user": self.user, "auth": self.auth_method},
        )

    def run(self, remote_command: Sequence[str], *, tty: bool = False) -> None:
        if not remote_command:
            raise ZimigrateError("Remote command is empty")
        self._invoke(self._remote_argv(remote_command, tty=tty), tty=tty)

    def start(self, remote_command: Sequence[str], *, tty: bool = False) -> subprocess.Popen[bytes]:
        if not remote_command:
            raise ZimigrateError("Remote command is empty")
        interrupt = get_interrupt()
        interrupt.check()
        use_tty = tty and sys.stdin.isatty()
        try:
            process = subprocess.Popen(  # nosec B603
                self._remote_argv(remote_command, tty=tty),
                stdin=None if use_tty else subprocess.DEVNULL,
                env=self._env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise ZimigrateError(f"Cannot execute ssh: {exc}") from exc
        interrupt.register(process)
        return process

    def rsync_file_from_remote(
        self,
        remote_file: str,
        local_file: Path,
    ) -> None:
        local_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._rsync_copy(
            f"{self._rsync_destination()}:{_absolute_remote(remote_file)}",
            str(local_file.resolve()),
            delete=False,
        )

    def rsync_to_remote(
        self,
        local: Path,
        remote_path: str,
        *,
        delete: bool = False,
        excludes: Sequence[str] = (),
    ) -> None:
        self._rsync_copy(
            f"{local.resolve()}/",
            f"{self._rsync_destination()}:{_absolute_remote(remote_path)}",
            delete=delete,
            excludes=excludes,
        )

    def rsync_from_remote(
        self,
        remote_path: str,
        local: Path,
        *,
        delete: bool = False,
        excludes: Sequence[str] = (),
    ) -> None:
        local.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._rsync_copy(
            f"{self._rsync_destination()}:{_absolute_remote(remote_path)}/",
            f"{local.resolve()}/",
            delete=delete,
            excludes=excludes,
        )

    def remote_has_files(self, remote_dir: str, name_pattern: str) -> bool:
        if not REMOTE_NAME_PATTERN.fullmatch(name_pattern):
            raise ZimigrateError("Remote name pattern is invalid")
        interrupt = get_interrupt()
        interrupt.check()
        command = self.remote_argv(
            [
                "find",
                _absolute_remote(remote_dir),
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-name",
                name_pattern,
                "-print",
                "-quit",
            ]
        )
        try:
            completed = subprocess.run(  # nosec B603
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env(),
            )
        except OSError as exc:
            raise ZimigrateError(f"Cannot execute ssh: {exc}") from exc
        if interrupt.is_set():
            raise Interrupted("Interrupted by user")
        return bool((completed.stdout or b"").strip())

    def capture(self, remote_command: Sequence[str]) -> str:
        if not remote_command:
            raise ZimigrateError("Remote command is empty")
        interrupt = get_interrupt()
        interrupt.check()
        try:
            completed = subprocess.run(  # nosec B603
                self._remote_argv(remote_command, tty=False),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=self._env(),
            )
        except OSError as exc:
            raise ZimigrateError(f"Cannot execute ssh: {exc}") from exc
        if interrupt.is_set():
            raise Interrupted("Interrupted by user")
        if completed.returncode != 0:
            raise ZimigrateError(f"SSH command failed on {self.host} (exit {completed.returncode})")
        return (completed.stdout or b"").decode("utf-8", errors="replace")

    def remote_argv(self, remote_command: Sequence[str], *, tty: bool = False) -> list[str]:
        if not remote_command:
            raise ZimigrateError("Remote command is empty")
        use_tty = tty and sys.stdin.isatty()
        # OpenSSH treats every argument after the destination as part of the
        # remote command. A ``--`` here would be sent to the remote shell.
        return [
            self._ssh,
            *self._ssh_options(master="no"),
            "-t" if use_tty else "-T",
            self._destination(),
            " ".join(_shell_quote(part) for part in remote_command),
        ]

    def process_env(self) -> dict[str, str]:
        return self._env()

    def _remote_argv(self, remote_command: Sequence[str], *, tty: bool) -> list[str]:
        return self.remote_argv(remote_command, tty=tty)

    def close(self) -> None:
        if self._master_started and self._control_path is not None:
            with suppress(OSError):
                subprocess.run(  # nosec B603
                    [
                        self._ssh,
                        *self._ssh_options(master="no"),
                        "-O",
                        "exit",
                        self._destination(),
                    ],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._env(),
                )
        self._master_started = False
        if self._password_file is not None:
            _remove_secret_path(self._password_file)
            self._password_file = None
        if self._askpass_wrapper is not None:
            _remove_secret_path(self._askpass_wrapper)
            self._askpass_wrapper = None
        if self._control_dir is not None:
            shutil.rmtree(self._control_dir, ignore_errors=True)
            self._control_dir = None
            self._control_path = None
        self._password = None

    def __enter__(self) -> SshSession:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ssh_options(self, *, master: str) -> list[str]:
        if self._control_path is None:
            raise ZimigrateError("SSH session is not initialized")
        options = [
            "-o",
            "BatchMode=yes" if self._password is None else "BatchMode=no",
            "-o",
            f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
            "-o",
            f"StrictHostKeyChecking={strict_host_key_checking(self._ssh)}",
            "-o",
            "UpdateHostKeys=yes",
            "-o",
            f"ControlPath={self._control_path}",
            "-o",
            "ControlPersist=300",
            "-o",
            f"ControlMaster={master}",
            "-o",
            "Compression=no",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=20",
            "-p",
            str(self.port),
        ]
        if self._password is None:
            options.extend(["-o", "PreferredAuthentications=publickey"])
        else:
            options.extend(
                [
                    "-o",
                    "PreferredAuthentications=password,keyboard-interactive",
                    "-o",
                    "NumberOfPasswordPrompts=1",
                    "-o",
                    "IdentitiesOnly=yes",
                ]
            )
        return options

    def _destination(self) -> str:
        return f"{self.user}@{self.host}"

    def _rsync_destination(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.user}@{host}"

    def _invoke(self, command: list[str], *, tty: bool) -> None:
        interrupt = get_interrupt()
        interrupt.check()
        try:
            completed = subprocess.run(  # nosec B603
                command,
                check=False,
                stdin=None if tty else subprocess.DEVNULL,
                env=self._env(),
            )
        except OSError as exc:
            raise ZimigrateError(f"Cannot execute ssh: {exc}") from exc
        if interrupt.is_set():
            raise Interrupted("Interrupted by user")
        if completed.returncode != 0:
            raise ZimigrateError(f"SSH command failed on {self.host} (exit {completed.returncode})")

    def _rsync_copy(
        self,
        source: str,
        destination: str,
        *,
        delete: bool,
        excludes: Sequence[str] = (),
    ) -> None:
        if self._rsync is None:
            raise ZimigrateError("This copy requires the local rsync command")
        interrupt = get_interrupt()
        interrupt.check()
        remote_shell = " ".join(
            _shell_quote(part) for part in [self._ssh, *self._ssh_options(master="no")]
        )
        command = [self._rsync, "-a", "-e", remote_shell]
        if delete:
            command.append("--delete")
        command.extend(["--exclude", ".lock"])
        for pattern in excludes:
            command.extend(["--exclude", pattern])
        command.extend([source, destination])
        LOGGER.debug("Copying files over SSH")
        try:
            completed = subprocess.run(  # nosec B603
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=self._env(),
            )
        except OSError as exc:
            raise ZimigrateError(f"Cannot execute rsync: {exc}") from exc
        if interrupt.is_set():
            raise Interrupted("Interrupted by user")
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", errors="replace").split()
            summary = " ".join(detail)[:300]
            suffix = f": {summary}" if summary else ""
            raise ZimigrateError(f"rsync failed (exit {completed.returncode}){suffix}")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._password_file is None or self._askpass_wrapper is None:
            return env
        env["DISPLAY"] = env.get("DISPLAY") or ":0"
        env["SSH_ASKPASS"] = str(self._askpass_wrapper)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["ZIMIGRATE_SSH_ASKPASS_FILE"] = str(self._password_file)
        src = str(Path(__file__).resolve().parents[1])
        pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src if not pythonpath else f"{src}{os.pathsep}{pythonpath}"
        return env


def connect_ssh(host: str, *, user: str = "root") -> SshSession:
    """Connect with a key, then prompt for a password only when key login fails."""
    try:
        session = SshSession(host, user=user)
        session.connect()
        return session
    except ZimigrateError as key_error:
        LOGGER.info("SSH key login failed; requesting a password")
        if not sys.stdin.isatty():
            raise ConfigurationError(
                f"SSH key authentication to {user}@{host} failed; "
                "a terminal is required to enter a password"
            ) from key_error
        import getpass

        username = input(f"SSH username [{user}]: ").strip() or user
        password = getpass.getpass(f"Password for {username}@{host}: ")
        if not password:
            raise ConfigurationError("SSH password is empty") from key_error
        session = SshSession(host, user=username, password=password)
        try:
            session.connect()
        except ZimigrateError as password_error:
            raise ZimigrateError(
                f"SSH authentication to {username}@{host} failed"
            ) from password_error
        return session


@functools.lru_cache(maxsize=4)
def strict_host_key_checking(ssh_binary: str) -> str:
    """Return accept-new when this OpenSSH supports it; otherwise require known_hosts."""
    try:
        completed = subprocess.run(  # nosec B603
            [ssh_binary, "-G", "-o", "StrictHostKeyChecking=accept-new", "127.0.0.1"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return "yes"
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace").casefold()
    if "unsupported option" in stderr or "bad configuration option" in stderr:
        return "yes"
    return "accept-new"


def _absolute_remote(remote_path: str) -> str:
    if not remote_path.startswith("/") or "\x00" in remote_path:
        raise ZimigrateError("Remote path must be an absolute POSIX path")
    return remote_path


def _write_password_file(password: str) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="zimigrate-cred-", dir=tempfile.gettempdir()))
    os.chmod(directory, 0o700)
    path = directory / "password"
    path.write_bytes(password.encode("utf-8"))
    os.chmod(path, 0o600)
    return path


def _write_askpass_wrapper() -> Path:
    python = str(Path(sys.executable).resolve())
    script = f"#!/bin/sh\nexec {_shell_quote(python)} -m zimigrate.ssh_askpass\n"
    descriptor, name = tempfile.mkstemp(
        prefix="zimigrate-askpass-",
        suffix=".sh",
        dir=tempfile.gettempdir(),
    )
    path = Path(name)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(script)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _remove_secret_path(path: Path) -> None:
    directory = path.parent
    try:
        if path.is_file():
            size = max(path.stat().st_size, 1)
            with path.open("wb") as stream:
                stream.write(b"\0" * size)
                stream.flush()
                os.fsync(stream.fileno())
            path.unlink()
        if directory.name.startswith("zimigrate-cred-"):
            directory.rmdir()
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
