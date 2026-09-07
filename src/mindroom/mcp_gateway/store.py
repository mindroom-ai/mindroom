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


class GatewayOAuthStore:
    """Keep inbound grants durable and serialize their consume/rotate operations."""

    def __init__(self, storage_root: Path) -> None:
        self.path = storage_root / "mcp_gateway" / "oauth.sqlite3"
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
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL
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
            """)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _transaction(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
        """Run one atomic operation off the HTTP event loop."""
        return await asyncio.to_thread(lambda: self._transaction(operation))
