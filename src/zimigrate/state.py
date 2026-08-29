from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path

from zimigrate.errors import ZimigrateError
from zimigrate.util import utc_now


@dataclass(frozen=True, slots=True)
class StateRecord:
    phase: str
    entity: str
    status: str
    attempts: int
    artifact_path: str | None
    checksum: str | None
    detail: str | None


class StateStore:
    """Thread-safe, crash-resilient checkpoint storage."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._database = sqlite3.connect(
            path, timeout=30, isolation_level=None, check_same_thread=False
        )
        try:
            integrity = self._database.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            self._database.close()
            raise ZimigrateError(f"Checkpoint database is corrupt: {path}") from exc
        if integrity is None or integrity[0] != "ok":
            self._database.close()
            raise ZimigrateError(f"Checkpoint database is corrupt: {path}")
        # WAL + NORMAL keeps checkpoints crash-safe without fsync on every account.
        self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute("PRAGMA synchronous=NORMAL")
        self._database.row_factory = sqlite3.Row
        self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS operations (
                phase TEXT NOT NULL,
                entity TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                artifact_path TEXT,
                checksum TEXT,
                detail TEXT,
                PRIMARY KEY (phase, entity)
            );
            CREATE INDEX IF NOT EXISTS operations_status_idx
                ON operations(status, phase);
            """
        )
        os.chmod(path, 0o600)
        self._finalizer = weakref.finalize(self, self._database.close)

    def is_success(self, phase: str, entity: str) -> bool:
        record = self.get(phase, entity)
        return record is not None and record.status == "success"

    def get(self, phase: str, entity: str) -> StateRecord | None:
        with self._lock:
            row = self._database.execute(
                "SELECT * FROM operations WHERE phase = ? AND entity = ?",
                (phase, entity),
            ).fetchone()
        return _record(row) if row else None

    def start(self, phase: str, entity: str) -> None:
        with self._lock:
            self._database.execute(
                """
                INSERT INTO operations(phase, entity, status, attempts, started_at,
                                       finished_at, artifact_path, checksum, detail)
                VALUES (?, ?, 'running', 1, ?, NULL, NULL, NULL, NULL)
                ON CONFLICT(phase, entity) DO UPDATE SET
                    status = 'running', attempts = operations.attempts + 1,
                    started_at = excluded.started_at, finished_at = NULL,
                    artifact_path = NULL, checksum = NULL, detail = NULL
                """,
                (phase, entity, utc_now()),
            )

    def succeed(
        self,
        phase: str,
        entity: str,
        *,
        artifact_path: str | None = None,
        checksum: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            updated = self._database.execute(
                """
                UPDATE operations SET status = 'success', finished_at = ?,
                                      artifact_path = ?, checksum = ?, detail = ?
                WHERE phase = ? AND entity = ?
                """,
                (utc_now(), artifact_path, checksum, detail, phase, entity),
            )
            if updated.rowcount != 1:
                raise ZimigrateError(f"Checkpoint was not started: {phase}/{entity}")

    def fail(self, phase: str, entity: str, error: str) -> None:
        with self._lock:
            updated = self._database.execute(
                """
                UPDATE operations SET status = 'failed', finished_at = ?, detail = ?
                WHERE phase = ? AND entity = ?
                """,
                (utc_now(), error[:4000], phase, entity),
            )
            if updated.rowcount != 1:
                raise ZimigrateError(f"Checkpoint was not started: {phase}/{entity}")

    def summary(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._database.execute(
                """
                SELECT phase, status, COUNT(*) AS count, SUM(attempts) AS attempts
                FROM operations GROUP BY phase, status ORDER BY phase, status
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def failed(self) -> list[StateRecord]:
        with self._lock:
            rows = self._database.execute(
                "SELECT * FROM operations WHERE status = 'failed' ORDER BY phase, entity"
            ).fetchall()
        return [_record(row) for row in rows]

    def successful_entities(self, phase: str) -> set[str]:
        with self._lock:
            rows = self._database.execute(
                "SELECT entity FROM operations WHERE phase = ? AND status = 'success'",
                (phase,),
            ).fetchall()
        return {str(row["entity"]) for row in rows}

    def close(self) -> None:
        self._finalizer()


def _record(row: sqlite3.Row) -> StateRecord:
    return StateRecord(
        phase=row["phase"],
        entity=row["entity"],
        status=row["status"],
        attempts=row["attempts"],
        artifact_path=row["artifact_path"],
        checksum=row["checksum"],
        detail=row["detail"],
    )
