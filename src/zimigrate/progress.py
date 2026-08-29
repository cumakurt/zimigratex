"""Thread-safe live progress messages for long-running export and import work."""

from __future__ import annotations

import logging
import threading


def entity_start_fields(kind: str, name: str, *, action: str) -> dict[str, str]:
    return {
        "event": "entity_start",
        "phase_kind": kind,
        "phase_action": action,
        "entity": name,
    }


class PhaseProgress:
    def __init__(
        self,
        logger: logging.Logger,
        *,
        kind: str,
        total: int,
        action: str,
    ) -> None:
        self._logger = logger
        self.kind = kind
        self.action = action
        self.total = total
        self._done = 0
        self._lock = threading.Lock()
        if total == 0:
            logger.info(
                "No %s objects to %s",
                kind,
                action,
                extra={
                    "event": "phase_empty",
                    "phase_kind": kind,
                    "phase_action": action,
                    "total": total,
                },
            )
            return
        logger.info(
            "Starting %s %s (%s)",
            kind,
            action,
            total,
            extra={
                "event": "phase_start",
                "phase_kind": kind,
                "phase_action": action,
                "total": total,
            },
        )

    def complete(self, name: str, *, failed: bool = False) -> None:
        with self._lock:
            self._done += 1
            current = self._done
        status = "Failed" if failed else "Completed"
        self._logger.info(
            "%s %s %s/%s: %s",
            status,
            self.kind,
            current,
            self.total,
            name,
            extra={
                "event": "phase_progress",
                "phase_kind": self.kind,
                "phase_action": self.action,
                "current": current,
                "total": self.total,
                "entity": name,
                "failed": failed,
            },
        )
