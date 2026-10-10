"""Metadata-only request audit log for egress broker."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
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
        """Initialize audit log at the given path.

        Args:
            path: Path to SQLite database file (created with mode 0600)
            retention_days: Records older than this are pruned
            max_rows: Maximum rows to keep after pruning by age

        """
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

        # Logs written before refusal codes existed lack the column.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(audit)")}
        if "code" not in columns:
            self._conn.execute("ALTER TABLE audit ADD COLUMN code TEXT")

        # Create indices for common query patterns
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_at ON audit(at DESC)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_name ON audit(agent_name)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_host ON audit(host)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_service ON audit(service)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_requester_at ON audit(requester_id, at DESC)")

        self._conn.commit()

    def record(self, rec: AuditRecord) -> None:
        """Record an audit entry.

        Query strings are stripped from the path before storage.
        Prunes old records every 1000 inserts.

        Args:
            rec: Audit record to store

        """
        # Strip query string from path
        path_without_query = rec.path.split("?", 1)[0]

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit (
                    at, kind, scope, agent_name, requester_id, method,
                    host, path, service, status, bytes_up, bytes_down, duration_ms, code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.at.isoformat(),
                    rec.kind,
                    rec.scope,
                    rec.agent_name,
                    rec.requester_id,
                    rec.method,
                    rec.host,
                    path_without_query,
                    rec.service,
                    rec.status,
                    rec.bytes_up,
                    rec.bytes_down,
                    rec.duration_ms,
                    rec.code,
                ),
            )
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
        limit: int = 200,
    ) -> list[AuditRecord]:
        """Query audit records with optional filters.

        Returns records newest first.

        Args:
            agent_name: Filter by agent name
            requester_id: Filter by the requester the worker token carried, an exact match
            host: Filter by host
            service: Filter by service
            limit: Maximum records to return (clamped to 1..1000)

        Returns:
            List of audit records, newest first

        """
        # Clamp limit to 1..1000
        clamped_limit = max(1, min(limit, 1000))

        # Build query with filters
        conditions = []
        params = []

        if agent_name is not None:
            conditions.append("agent_name = ?")
            params.append(agent_name)

        if requester_id is not None:
            conditions.append("requester_id = ?")
            params.append(requester_id)

        if host is not None:
            conditions.append("host = ?")
            params.append(host)

        if service is not None:
            conditions.append("service = ?")
            params.append(service)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT at, kind, scope, agent_name, requester_id, method,
                   host, path, service, status, bytes_up, bytes_down, duration_ms, code
            FROM audit
            WHERE {where_clause}
            ORDER BY at DESC
            LIMIT ?
        """  # noqa: S608 - where_clause is constructed from hardcoded conditions only
        params.append(clamped_limit)

        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()

        # Convert rows to AuditRecord objects
        return [
            AuditRecord(
                at=datetime.fromisoformat(row[0]).replace(tzinfo=UTC),
                kind=row[1],
                scope=row[2],
                agent_name=row[3],
                requester_id=row[4],
                method=row[5],
                host=row[6],
                path=row[7],
                service=row[8],
                status=row[9],
                bytes_up=row[10],
                bytes_down=row[11],
                duration_ms=row[12],
                code=row[13],
            )
            for row in rows
        ]

    def prune(self) -> int:
        """Prune old records.

        First deletes records older than retention_days,
        then trims to max_rows newest records.

        Returns:
            Number of rows deleted

        """
        with self._lock:
            return self._prune_unlocked()

    def _prune_unlocked(self) -> int:
        """Internal prune implementation (assumes lock is held)."""
        total_deleted = 0

        # Delete records older than retention_days
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        cursor = self._conn.execute(
            "DELETE FROM audit WHERE at < ?",
            (cutoff.isoformat(),),
        )
        total_deleted += cursor.rowcount

        # Trim to max_rows by deleting oldest records
        cursor = self._conn.execute("SELECT COUNT(*) FROM audit")
        row_count = cursor.fetchone()[0]

        if row_count > self._max_rows:
            # Delete oldest records to get down to max_rows
            to_delete = row_count - self._max_rows
            self._conn.execute(
                """
                DELETE FROM audit
                WHERE rowid IN (
                    SELECT rowid FROM audit
                    ORDER BY at ASC
                    LIMIT ?
                )
                """,
                (to_delete,),
            )
            total_deleted += to_delete

        self._conn.commit()
        return total_deleted

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
