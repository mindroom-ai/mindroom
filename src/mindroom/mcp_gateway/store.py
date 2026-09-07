"""Private SQLite transactions for inbound MCP authorization state."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_T = TypeVar("_T")
_REGISTRATION_TTL = 86_400
_ONBOARDING_ROW_OVERHEAD = 1024


class GatewayOAuthCapacityError(Exception):
    """Public onboarding state has reached its configured storage budget."""


class GatewayOAuthStore:
    """Keep inbound grants durable and serialize their consume/rotate operations."""

    def __init__(
        self,
        storage_root: Path,
        *,
        onboarding_max_bytes: int,
        clock: Callable[[], float],
    ) -> None:
        self.path = storage_root / "mcp_gateway" / "oauth.sqlite3"
        self.onboarding_max_bytes = onboarding_max_bytes
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        connection = self._connect()
        try:
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending (
                    state_hash TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    expires_at REAL NOT NULL, requester_id TEXT, authenticated_user_id TEXT, agent_name TEXT, csrf_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS capabilities (
                    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
                    payload TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (grant_id) REFERENCES grants(grant_id)
                );
                CREATE INDEX IF NOT EXISTS capabilities_grant ON capabilities(grant_id);
                CREATE INDEX IF NOT EXISTS grants_client ON grants(json_extract(payload, '$.client_id'));
                CREATE INDEX IF NOT EXISTS pending_client ON pending(json_extract(payload, '$.client_id'));
                CREATE INDEX IF NOT EXISTS pending_expiry ON pending(expires_at);
            """)
            if "expires_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(clients)")}:
                connection.execute("ALTER TABLE clients ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")
                connection.execute("UPDATE clients SET expires_at = ?", (self.registration_expires_at(),))
            connection.execute("CREATE INDEX IF NOT EXISTS clients_expiry ON clients(expires_at)")
            connection.execute("COMMIT")
        finally:
            connection.close()

    def registration_expires_at(self) -> float:
        """Give new or migrated abandoned registrations one fixed day of retention."""
        return self._clock() + _REGISTRATION_TTL

    def _connect(self, *, timeout: float = 10) -> sqlite3.Connection:
        # Do not enable WAL: runtime storage may use network filesystems or single-file backups.
        connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _prune(connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM pending WHERE expires_at <= ?", (now,))
        # A revocation may have loaded an access token before concurrent rotation.
        # Keep access and refresh bindings until the whole family is removed.
        connection.execute(
            """DELETE FROM capabilities
               WHERE grant_id IN (SELECT grant_id FROM grants WHERE expires_at <= ? OR revoked = 1)
                  OR (kind = 'code' AND (expires_at <= ? OR consumed = 1))""",
            (now, now),
        )
        connection.execute(
            """DELETE FROM grants WHERE expires_at <= ? OR revoked = 1
               OR NOT EXISTS (SELECT 1 FROM capabilities WHERE capabilities.grant_id = grants.grant_id)""",
            (now,),
        )
        connection.execute(
            """DELETE FROM clients WHERE expires_at <= ?
               AND NOT EXISTS (SELECT 1 FROM grants WHERE json_extract(grants.payload, '$.client_id') = clients.client_id)
               AND NOT EXISTS (SELECT 1 FROM pending WHERE json_extract(pending.payload, '$.client_id') = clients.client_id)""",
            (now,),
        )

    def has_onboarding_capacity(self, connection: sqlite3.Connection, *, payload: str, identifier: str) -> bool:
        """Reserve UTF-8 payload bytes plus row/index allowance within the write transaction."""
        client_bytes = connection.execute(
            """SELECT COALESCE(SUM(length(CAST(metadata AS BLOB)) + length(CAST(client_id AS BLOB)) + ?), 0)
               FROM clients
               WHERE NOT EXISTS (
                   SELECT 1 FROM grants WHERE json_extract(grants.payload, '$.client_id') = clients.client_id
               )""",
            (_ONBOARDING_ROW_OVERHEAD,),
        ).fetchone()[0]
        pending_bytes = connection.execute(
            "SELECT COALESCE(SUM(length(CAST(payload AS BLOB)) + length(CAST(state_hash AS BLOB)) + ?), 0) FROM pending",
            (_ONBOARDING_ROW_OVERHEAD,),
        ).fetchone()[0]
        additional_bytes = len(payload.encode("utf-8")) + len(identifier.encode("utf-8")) + _ONBOARDING_ROW_OVERHEAD
        return client_bytes + pending_bytes + additional_bytes <= self.onboarding_max_bytes

    def _transaction(self, operation: Callable[[sqlite3.Connection], _T], *, read_only: bool = False) -> _T:
        connection = self._connect(timeout=1 if read_only else 10)
        try:
            if read_only:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN DEFERRED")
            else:
                connection.execute("BEGIN IMMEDIATE")
                self._prune(connection, self._clock())
            result = operation(connection)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        else:
            return result
        finally:
            connection.close()

    async def transact(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run one atomic operation off the HTTP event loop, collecting expired state."""
        return await asyncio.to_thread(lambda: self._transaction(operation))

    async def read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Read committed state without reserving a writer lock or running retention."""
        return await asyncio.to_thread(lambda: self._transaction(operation, read_only=True))
