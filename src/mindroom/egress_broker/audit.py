"""Metadata-only request audit log for egress broker."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - Required at runtime for AuditLog.__init__ parameter
from typing import Literal


@dataclass(frozen=True)
class AuditRecord:
    """Metadata record for a brokered request or tunnel."""

    at: datetime
    kind: Literal["request", "tunnel", "denied"]
    scope: str
    agent_name: str | None
    requester_id: str | None
    method: str
    host: str
    path: str
    service: str | None
    status: int
    bytes_up: int
    bytes_down: int
    duration_ms: int
    code: str | None = None
    """The `error` code of the JSON body a refusal answered with; None for a forwarded request."""


# AuditRecord's fields, in order, so INSERT and SELECT follow the dataclass.
_FIELDS = [field.name for field in fields(AuditRecord)]
_COLUMNS = ", ".join(_FIELDS)
_INSERT = f"INSERT INTO audit ({_COLUMNS}) VALUES ({', '.join(f':{name}' for name in _FIELDS)})"  # noqa: S608


class AuditLog:
    """Audit log for egress broker requests.

    Thread-safe SQLite-backed log for recording request metadata.
    The broker thread writes, API threads read.
    """

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 30,
        max_rows: int = 100_000,
    ) -> None:
        """Open or create the SQLite log at `path` (mode 0600) and prune it; pruning keeps `retention_days` and `max_rows`."""
        self._path = path
        self._retention_days = retention_days
        self._max_rows = max_rows
        self._lock = threading.Lock()
        self._insert_count = 0

        # Create parent directory if needed
        path.parent.mkdir(parents=True, exist_ok=True)

        # Create the file with mode 0600 before opening it
        if not path.exists():
            path.touch(mode=0o600)

        # Open connection with check_same_thread=False (we guard with a lock)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)

        # Set WAL mode for better concurrency
        self._conn.execute("PRAGMA journal_mode=WAL")

        # Create schema
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                agent_name TEXT,
                requester_id TEXT,
                method TEXT NOT NULL,
                host TEXT NOT NULL,
                path TEXT NOT NULL,
                service TEXT,
                status INTEGER NOT NULL,
                bytes_up INTEGER NOT NULL,
                bytes_down INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                code TEXT
            )
        """)

        # Create indices for common query patterns
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_at ON audit(at DESC)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_name ON audit(agent_name)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_host ON audit(host)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_service ON audit(service)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_requester_at ON audit(requester_id, at DESC)")

        # Prune on open too, so a primary that restarts before 1000 inserts still enforces both limits
        with self._lock:
            self._prune_unlocked()

    def record(self, rec: AuditRecord) -> None:
        """Store `rec` without the path's query string; every 1000th insert prunes old records."""
        row = {**asdict(rec), "at": rec.at.isoformat(), "path": rec.path.split("?", 1)[0]}
        with self._lock:
            self._conn.execute(_INSERT, row)
            self._conn.commit()

            # Prune every 1000 inserts
            self._insert_count += 1
            if self._insert_count >= 1000:
                self._insert_count = 0
                self._prune_unlocked()

    def query(
        self,
        *,
        agent_name: str | None = None,
        requester_id: str | None = None,
        host: str | None = None,
        service: str | None = None,
        scope: str | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]:
        """Return up to `limit` records matching every given filter exactly, newest first; callers bound `limit`."""
        filters = {
            "agent_name": agent_name,
            "scope": scope,
            "requester_id": requester_id,
            "host": host,
            "service": service,
        }
        params = {name: value for name, value in filters.items() if value is not None}
        where = " AND ".join(f"{name} = :{name}" for name in params) or "1=1"
        # Column names come only from AuditRecord and the literal keys above.
        query = f"SELECT {_COLUMNS} FROM audit WHERE {where} ORDER BY at DESC LIMIT :limit"  # noqa: S608
        with self._lock:
            rows = self._conn.execute(query, {**params, "limit": limit}).fetchall()
        return [AuditRecord(datetime.fromisoformat(row[0]).replace(tzinfo=UTC), *row[1:]) for row in rows]

    def _prune_unlocked(self) -> None:
        """Delete records older than `retention_days`, then all but the newest `max_rows`; the caller holds the lock."""
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        self._conn.execute("DELETE FROM audit WHERE at < ?", (cutoff.isoformat(),))

        row_count = self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        if row_count > self._max_rows:
            self._conn.execute(
                """
                DELETE FROM audit
                WHERE rowid IN (
                    SELECT rowid FROM audit
                    ORDER BY at ASC
                    LIMIT ?
                )
                """,
                (row_count - self._max_rows,),
            )

        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
