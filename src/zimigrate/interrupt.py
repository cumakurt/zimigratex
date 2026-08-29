"""Cooperative Ctrl+C handling for worker threads and child processes."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar

from zimigrate.errors import Interrupted

T = TypeVar("T")
K = TypeVar("K")


class InterruptController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Interrupted("Interrupted by user")

    def wait(self, timeout: float) -> None:
        if timeout <= 0:
            self.check()
            return
        if self._event.wait(timeout):
            raise Interrupted("Interrupted by user")

    def request(self) -> None:
        self._event.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            signal_stop(process)

    def register(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.add(process)
        if self._event.is_set():
            signal_stop(process)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)


_CONTROLLER = InterruptController()


def get_interrupt() -> InterruptController:
    return _CONTROLLER


def handle_sigint(_signum: int, _frame: object) -> None:
    get_interrupt().request()
    raise KeyboardInterrupt


def signal_stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return


def stop_process(process: subprocess.Popen[bytes]) -> None:
    signal_stop(process)
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return


class WorkerPool:
    def __init__(self, max_workers: int, thread_name_prefix: str) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._executor: ThreadPoolExecutor | None = None

    def __enter__(self) -> ThreadPoolExecutor:
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=self._thread_name_prefix,
        )
        return self._executor

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        executor = self._executor
        if executor is None:
            return
        interrupted = exc_type in {Interrupted, KeyboardInterrupt} or get_interrupt().is_set()
        if interrupted:
            get_interrupt().request()
        executor.shutdown(wait=True, cancel_futures=interrupted)


def bounded_futures(
    executor: ThreadPoolExecutor,
    values: Iterable[K],
    operation: Callable[[K], T],
    *,
    max_pending: int,
) -> Iterator[tuple[K, Future[T]]]:
    if max_pending < 1:
        raise ValueError("max_pending must be positive")
    interrupt = get_interrupt()
    iterator = iter(values)
    pending: dict[Future[T], K] = {}

    def fill() -> None:
        while len(pending) < max_pending:
            try:
                value = next(iterator)
            except StopIteration:
                return
            pending[executor.submit(operation, value)] = value

    fill()
    while pending:
        interrupt.check()
        done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
        if not done:
            continue
        for future in done:
            value = pending.pop(future)
            fill()
            yield value, future
