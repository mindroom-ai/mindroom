"""Atomic SQLite storage for one canonical OAuth credential scope."""

# Legacy format: generic JSON OAuth credentials and adjacent generation/lock sidecars.
# Last legacy release: v2026.8.79; authoritative SQLite storage introduced in v2026.8.80.
# Handling: adoption ended after v2026.9.48; obsolete files stay untouched and reconnect is required.
# Coverage: tests/test_oauth_credential_store.py::test_json_only_credentials_and_sidecars_are_ignored.

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from cryptography.exceptions import InvalidTag

from mindroom.credentials import scoped_credentials_path
from mindroom.durable_write import fsync_directory_durable
from mindroom.oauth import legacy_credentials
from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from mindroom.oauth.credential_store_types import OAuthCredentialStoreContext


_SCHEMA_VERSION = 1
_LOCK_RETRY_SECONDS = 0.05
_LOCK_WAIT_TIMEOUT_SECONDS = 30.0
_T = TypeVar("_T")


class OAuthCredentialUnreadableError(OAuthProviderError):
    """Signal that a stored OAuth credential exists but cannot be decoded."""


@dataclass(frozen=True, slots=True)
class _OAuthStoredGenerations:
    """Durable revisions for one OAuth credential scope."""

    generation: str
    connection_generation: str


@dataclass(frozen=True, slots=True)
class _OAuthStoredCredentialSnapshot:
    """Decoded credentials and their atomically stored revisions."""

    credentials: dict[str, Any] | None
    generation: str
    connection_generation: str


class _OAuthCredentialReader:
    """One read-only view of a committed per-scope credential state."""

    def __init__(self, context: OAuthCredentialStoreContext, connection: sqlite3.Connection) -> None:
        self._context = context
        self._connection = connection

    def generations(self) -> _OAuthStoredGenerations:
        """Read revisions without decoding credential bytes."""
        return _stored_generations(_state_row(self._connection))

    def snapshot(self) -> _OAuthStoredCredentialSnapshot:
        """Decode the last committed credential snapshot without rewriting it."""
        row = _state_row(self._connection)
        return _OAuthStoredCredentialSnapshot(
            credentials=_decode_credentials(self._context, row),
            generation=str(row["generation"]),
            connection_generation=str(row["connection_generation"]),
        )

    def reset_operation_result(self, operation_id: str) -> bool | None:
        """Return a completed reset receipt without mutating the store."""
        return _reset_operation_result(self._connection, operation_id)


class OAuthCredentialTransaction:
    """One open per-scope SQLite write transaction."""

    def __init__(
        self,
        context: OAuthCredentialStoreContext,
        connection: sqlite3.Connection,
    ) -> None:
        self._context = context
        self._connection = connection

    def generations(self) -> _OAuthStoredGenerations:
        """Read revisions without decoding credential bytes."""
        return _stored_generations(_state_row(self._connection))

    def snapshot(self) -> _OAuthStoredCredentialSnapshot:
        """Read and decode the current credential snapshot."""
        row = _state_row(self._connection)
        credentials = self._decode_credentials(row)
        return _OAuthStoredCredentialSnapshot(
            credentials=credentials,
            generation=str(row["generation"]),
            connection_generation=str(row["connection_generation"]),
        )

    def publish(
        self,
        credentials: Mapping[str, Any],
        *,
        advance_connection_generation: bool,
    ) -> _OAuthStoredCredentialSnapshot:
        """Replace credentials and advance their authoritative revisions."""
        generations = self.generations()
        generation = secrets.token_hex(32)
        connection_generation = (
            secrets.token_hex(32) if advance_connection_generation else generations.connection_generation
        )
        published = legacy_credentials.without_legacy_publication(credentials)
        payload = self._context.credentials_manager.encode_credentials(
            self._context.provider.credential_service,
            published,
        )
        self._connection.execute(
            """
            UPDATE oauth_credential_state
            SET credential_payload = ?, credential_present = 1,
                credential_unreadable = 0, generation = ?, connection_generation = ?
            WHERE singleton = 1
            """,
            (payload, generation, connection_generation),
        )
        return _OAuthStoredCredentialSnapshot(
            credentials=published,
            generation=generation,
            connection_generation=connection_generation,
        )

    def reset_operation_result(self, operation_id: str) -> bool | None:
        """Return a completed stable reset receipt without mutating credentials."""
        return _reset_operation_result(self._connection, operation_id)

    def reset(
        self,
        operation_id: str | None,
    ) -> bool:
        """Delete credentials and optionally record a reset receipt after lifecycle validation."""
        row = _state_row(self._connection)
        credential_existed = bool(row["credential_present"])
        self._connection.execute(
            """
            UPDATE oauth_credential_state
            SET credential_payload = NULL, credential_present = 0,
                credential_unreadable = 0, generation = ?, connection_generation = ?
            WHERE singleton = 1
            """,
            (secrets.token_hex(32), secrets.token_hex(32)),
        )
        if operation_id is not None:
            self._connection.execute(
                """
                INSERT INTO oauth_reset_operations(operation_id, credential_existed)
                VALUES (?, ?)
                """,
                (operation_id, int(credential_existed)),
            )
        return credential_existed

    async def commit(self) -> None:
        """Durably commit, retrying a reader-blocked commit in this transaction."""
        await _retry_sqlite_lock(
            lambda: self._connection.execute("COMMIT"),
            operation="commit",
        )

    def _decode_credentials(self, row: sqlite3.Row) -> dict[str, Any] | None:
        normalized = _decode_credentials(self._context, row)
        if normalized is None:
            return None
        credentials = self._context.credentials_manager.decode_credentials(
            self._context.provider.credential_service,
            bytes(row["credential_payload"]),
        )
        if bool(row["credential_unreadable"]) or normalized != credentials:
            encoded = self._context.credentials_manager.encode_credentials(
                self._context.provider.credential_service,
                normalized,
            )
            self._connection.execute(
                """
                UPDATE oauth_credential_state
                SET credential_payload = ?, credential_unreadable = 0
                WHERE singleton = 1
                """,
                (encoded,),
            )
        return normalized


def _state_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM oauth_credential_state WHERE singleton = 1",
    ).fetchone()
    if row is None:
        msg = "OAuth credential store state is missing"
        raise OAuthProviderError(msg)
    return row


def _stored_generations(row: sqlite3.Row) -> _OAuthStoredGenerations:
    return _OAuthStoredGenerations(
        generation=str(row["generation"]),
        connection_generation=str(row["connection_generation"]),
    )


def _reset_operation_result(connection: sqlite3.Connection, operation_id: str) -> bool | None:
    row = connection.execute(
        "SELECT credential_existed FROM oauth_reset_operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    return None if row is None else bool(row["credential_existed"])


def _decode_credentials(
    context: OAuthCredentialStoreContext,
    row: sqlite3.Row,
) -> dict[str, Any] | None:
    """Decode and normalize one stored payload without mutating its transaction."""
    if not bool(row["credential_present"]):
        return None
    payload = row["credential_payload"]
    if payload is None:
        msg = "Stored OAuth credentials could not be loaded"
        raise OAuthCredentialUnreadableError(msg)
    try:
        credentials = context.credentials_manager.decode_credentials(
            context.provider.credential_service,
            bytes(payload),
        )
    except (OSError, TypeError, ValueError, InvalidTag) as exc:
        msg = "Stored OAuth credentials could not be loaded"
        raise OAuthCredentialUnreadableError(msg) from exc
    return legacy_credentials.without_legacy_publication(credentials)


def _oauth_credential_database_path(context: OAuthCredentialStoreContext) -> Path:
    """Return the private SQLite path for one canonical OAuth credential scope."""
    credential_path = scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    return credential_path.with_name(f"{credential_path.stem}.sqlite3")


@asynccontextmanager
async def oauth_credential_transaction(
    context: OAuthCredentialStoreContext,
) -> AsyncIterator[OAuthCredentialTransaction]:
    """Acquire one cancellable cross-process transaction for a credential scope."""
    database_path = _oauth_credential_database_path(context)
    _prepare_database_path(database_path)
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        await _set_synchronous_extra(connection)
        await _enter_delete_journal(connection)
        await _begin_immediate(connection)
        await _initialize_store(context, connection)
        await _begin_immediate(connection)
        transaction = OAuthCredentialTransaction(context, connection)
        yield transaction
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


@asynccontextmanager
async def oauth_credential_reader(
    context: OAuthCredentialStoreContext,
) -> AsyncIterator[_OAuthCredentialReader]:
    """Read the last committed state without contending with an admitted writer."""
    database_path = _oauth_credential_database_path(context)
    _prepare_database_path(database_path)
    if await _reader_requires_write_preparation(context, database_path):
        async with oauth_credential_transaction(context) as transaction:
            await transaction.commit()
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        await _begin_read(connection)
        _validate_initialized_store(context, connection)
        yield _OAuthCredentialReader(context, connection)
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


async def _initialize_store(
    context: OAuthCredentialStoreContext,
    connection: sqlite3.Connection,
) -> None:
    """Create and bind one authoritative SQLite credential database."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_credential_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            provider_id TEXT NOT NULL,
            credential_service TEXT NOT NULL,
            worker_scope TEXT NOT NULL,
            worker_key TEXT NOT NULL,
            routing_agent_name TEXT NOT NULL,
            generation TEXT NOT NULL,
            connection_generation TEXT NOT NULL,
            credential_payload BLOB,
            credential_present INTEGER NOT NULL CHECK (credential_present IN (0, 1)),
            credential_unreadable INTEGER NOT NULL CHECK (credential_unreadable IN (0, 1))
        )
        """,
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_reset_operations (
            operation_id TEXT PRIMARY KEY,
            credential_existed INTEGER NOT NULL CHECK (credential_existed IN (0, 1))
        )
        """,
    )
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, _SCHEMA_VERSION}:
        msg = "OAuth credential store schema is unsupported"
        raise OAuthProviderError(msg)
    if version == 0:
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    expected_binding = _scope_binding(context)
    row = connection.execute(
        "SELECT * FROM oauth_credential_state WHERE singleton = 1",
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO oauth_credential_state(
                singleton, provider_id, credential_service, worker_scope, worker_key,
                routing_agent_name, generation, connection_generation,
                credential_payload, credential_present, credential_unreadable
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expected_binding["provider_id"],
                expected_binding["credential_service"],
                expected_binding["worker_scope"],
                expected_binding["worker_key"],
                expected_binding["routing_agent_name"],
                secrets.token_hex(32),
                secrets.token_hex(32),
                None,
                0,
                0,
            ),
        )
    else:
        _validate_scope_binding(context, connection, row)
    await _commit_connection(connection)


async def _reader_requires_write_preparation(
    context: OAuthCredentialStoreContext,
    database_path: Path,
) -> bool:
    """Return whether a reader needs store creation."""
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        await _begin_read(connection)
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'oauth_credential_state'",
        ).fetchone()
        if table is None:
            return True
        row = connection.execute(
            "SELECT * FROM oauth_credential_state WHERE singleton = 1",
        ).fetchone()
        if row is None:
            return True
        _validate_initialized_store(context, connection, row=row)
        return False
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def _validate_initialized_store(
    context: OAuthCredentialStoreContext,
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row | None = None,
) -> None:
    """Validate schema and scope metadata before exposing a committed reader."""
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != _SCHEMA_VERSION:
        msg = "OAuth credential store schema is unsupported"
        raise OAuthProviderError(msg)
    stored_row = (
        row
        if row is not None
        else connection.execute(
            "SELECT * FROM oauth_credential_state WHERE singleton = 1",
        ).fetchone()
    )
    if stored_row is None:
        msg = "OAuth credential store state is missing"
        raise OAuthProviderError(msg)
    _validate_scope_binding(context, connection, stored_row)


def _validate_scope_binding(
    context: OAuthCredentialStoreContext,
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> None:
    expected_binding = _scope_binding(context)
    actual_binding = {key: str(row[key]) for key in expected_binding}
    if actual_binding == expected_binding:
        return
    legacy_key = legacy_credentials.compatible_legacy_worker_key(context, connection)
    if legacy_key is not None:
        expected_binding["worker_key"] = legacy_key
    if actual_binding != expected_binding:
        msg = "OAuth credential store belongs to a different credential scope"
        raise OAuthProviderError(msg)


async def _commit_connection(connection: sqlite3.Connection) -> None:
    await _retry_sqlite_lock(lambda: connection.execute("COMMIT"), operation="commit")


async def _begin_immediate(connection: sqlite3.Connection) -> None:
    await _retry_sqlite_lock(lambda: connection.execute("BEGIN IMMEDIATE"), operation="write lock")


async def _begin_read(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN")
    await _retry_sqlite_lock(
        lambda: connection.execute("PRAGMA user_version").fetchone(),
        operation="read lock",
    )


async def _set_synchronous_extra(connection: sqlite3.Connection) -> None:
    await _retry_sqlite_lock(
        lambda: connection.execute("PRAGMA synchronous = EXTRA"),
        operation="durability configuration",
    )


async def _enter_delete_journal(connection: sqlite3.Connection) -> None:
    mode = await _retry_sqlite_lock(
        lambda: str(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower(),
        operation="journal-mode configuration",
    )
    if mode != "delete":
        msg = "OAuth credential store requires SQLite rollback-journal mode"
        raise OAuthProviderError(msg)


async def _retry_sqlite_lock(operation_call: Callable[[], _T], *, operation: str) -> _T:
    """Retry transient SQLite contention within one bounded admission window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LOCK_WAIT_TIMEOUT_SECONDS
    while True:
        try:
            return operation_call()
        except sqlite3.OperationalError as exc:
            if not _sqlite_lock_error(exc):
                raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = f"Timed out waiting for OAuth credential store {operation}"
                raise OAuthProviderError(msg) from exc
            await asyncio.sleep(min(_LOCK_RETRY_SECONDS, remaining))


def _prepare_database_path(database_path: Path) -> None:
    try:
        database_path.parent.chmod(0o700)
    except OSError as exc:
        msg = "OAuth credential store could not prepare its private directory"
        raise OAuthProviderError(msg) from exc
    if database_path.is_symlink() or database_path.exists():
        database_stat = _validate_existing_database_path(database_path)
        if database_stat.st_size != 0:
            return
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if os.name != "nt":
        flags |= os.O_NOFOLLOW
    if database_path.exists():
        descriptor = os.open(database_path, flags & ~(os.O_CREAT | os.O_EXCL))
    else:
        try:
            descriptor = os.open(database_path, flags, 0o600)
        except FileExistsError:
            database_stat = _validate_existing_database_path(database_path)
            if database_stat.st_size != 0:
                return
            descriptor = os.open(database_path, flags & ~(os.O_CREAT | os.O_EXCL))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory_durable(database_path.parent)


def _validate_existing_database_path(database_path: Path) -> os.stat_result:
    if database_path.is_symlink():
        msg = "OAuth credential database path cannot be a symlink"
        raise OAuthProviderError(msg)
    try:
        database_stat = database_path.stat()
    except FileNotFoundError as exc:
        msg = "OAuth credential database path disappeared during creation"
        raise OAuthProviderError(msg) from exc
    if not stat.S_ISREG(database_stat.st_mode):
        msg = "OAuth credential database path must be a regular file"
        raise OAuthProviderError(msg)
    try:
        database_path.chmod(0o600)
    except OSError as exc:
        msg = "OAuth credential store could not secure its database file"
        raise OAuthProviderError(msg) from exc
    return database_stat


def _scope_binding(context: OAuthCredentialStoreContext) -> dict[str, str]:
    worker_target = context.worker_target
    worker_scope = (
        worker_target.worker_scope if worker_target is not None and worker_target.worker_scope else "unscoped"
    )
    routing_agent_name = (
        worker_target.routing_agent_name
        if worker_target is not None and worker_scope in {"shared", "user_agent"} and worker_target.routing_agent_name
        else ""
    )
    return {
        "provider_id": context.provider.id,
        "credential_service": context.provider.credential_service,
        "worker_scope": worker_scope,
        "worker_key": worker_target.worker_key if worker_target is not None and worker_target.worker_key else "",
        "routing_agent_name": routing_agent_name,
    }


def _sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    return exc.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
