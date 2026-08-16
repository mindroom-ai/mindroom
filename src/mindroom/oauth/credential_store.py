"""Atomic SQLite storage for one canonical OAuth credential scope."""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from cryptography.exceptions import InvalidTag

from mindroom.credentials import scoped_credentials_path
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


logger = get_logger(__name__)

_INITIAL_GENERATION = "initial"
_SCHEMA_VERSION = 1
_LOCK_RETRY_SECONDS = 0.05
_LEGACY_PUBLICATION_KEY = "_mindroom_oauth_publication"


class OAuthCredentialUnreadableError(OAuthProviderError):
    """Signal that a stored OAuth credential exists but cannot be decoded."""


class _OAuthCredentialStoreContext(Protocol):
    """Fields the store needs from the lifecycle's canonical scope."""

    provider: OAuthProvider
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None


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


class OAuthCredentialTransaction:
    """One open per-scope SQLite write transaction."""

    def __init__(
        self,
        context: _OAuthCredentialStoreContext,
        connection: sqlite3.Connection,
    ) -> None:
        self._context = context
        self._connection = connection
        self.committed = False

    def generations(self) -> _OAuthStoredGenerations:
        """Read revisions without decoding credential bytes."""
        row = self._state_row()
        return _OAuthStoredGenerations(
            generation=str(row["generation"]),
            connection_generation=str(row["connection_generation"]),
        )

    def snapshot(self) -> _OAuthStoredCredentialSnapshot:
        """Read and decode the current credential snapshot."""
        row = self._state_row()
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
        published = _without_legacy_publication(credentials)
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
        row = self._connection.execute(
            "SELECT credential_existed FROM oauth_reset_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return None if row is None else bool(row["credential_existed"])

    def reset(
        self,
        operation_id: str,
        *,
        expected_connection_generation: str | None,
        replayable: bool,
    ) -> bool:
        """Atomically delete credentials and record one stable reset receipt."""
        if replayable:
            completed = self.reset_operation_result(operation_id)
            if completed is not None:
                return completed
        row = self._state_row()
        connection_generation = str(row["connection_generation"])
        if expected_connection_generation is not None and connection_generation != expected_connection_generation:
            msg = "OAuth connection state is stale because this credential changed"
            raise OAuthProviderError(msg)
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
        if replayable:
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
        while True:
            try:
                self._connection.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                if not _sqlite_lock_error(exc):
                    raise
                await asyncio.sleep(_LOCK_RETRY_SECONDS)
            else:
                self.committed = True
                return

    def rollback(self) -> None:
        """Roll back this transaction when it has not committed."""
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")

    def _state_row(self) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM oauth_credential_state WHERE singleton = 1",
        ).fetchone()
        if row is None:
            msg = "OAuth credential store state is missing"
            raise OAuthProviderError(msg)
        return row

    def _decode_credentials(self, row: sqlite3.Row) -> dict[str, Any] | None:
        if not bool(row["credential_present"]):
            return None
        payload = row["credential_payload"]
        if payload is None:
            msg = "Stored OAuth credentials could not be loaded"
            raise OAuthCredentialUnreadableError(msg)
        try:
            credentials = self._context.credentials_manager.decode_credentials(
                self._context.provider.credential_service,
                bytes(payload),
            )
        except (OSError, TypeError, ValueError, InvalidTag) as exc:
            msg = "Stored OAuth credentials could not be loaded"
            raise OAuthCredentialUnreadableError(msg) from exc
        normalized = _without_legacy_publication(credentials)
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


def _oauth_credential_database_path(context: _OAuthCredentialStoreContext) -> Path:
    """Return the private SQLite path for one canonical OAuth credential scope."""
    legacy_path = _legacy_credential_path(context)
    return legacy_path.with_name(f"{legacy_path.stem}.sqlite3")


@asynccontextmanager
async def oauth_credential_transaction(
    context: _OAuthCredentialStoreContext,
) -> AsyncIterator[OAuthCredentialTransaction]:
    """Acquire one cancellable cross-process transaction for a credential scope."""
    database_path = _oauth_credential_database_path(context)
    _prepare_database_path(database_path)
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 0")
        await _set_synchronous_extra(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        await _enter_delete_journal(connection)
        await _begin_immediate(connection)
        await _initialize_store(context, connection)
        _cleanup_legacy_files(context)
        await _begin_immediate(connection)
        transaction = OAuthCredentialTransaction(context, connection)
        try:
            yield transaction
        finally:
            if not transaction.committed:
                transaction.rollback()
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


async def _initialize_store(
    context: _OAuthCredentialStoreContext,
    connection: sqlite3.Connection,
) -> None:
    """Create and bind one database, adopting legacy credentials exactly once."""
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
        payload, credential_present, credential_unreadable = _legacy_credential_payload(context)
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
                _INITIAL_GENERATION,
                _INITIAL_GENERATION,
                payload,
                int(credential_present),
                int(credential_unreadable),
            ),
        )
    else:
        actual_binding = {key: str(row[key]) for key in expected_binding}
        if actual_binding != expected_binding:
            msg = "OAuth credential store belongs to a different credential scope"
            raise OAuthProviderError(msg)
    await _commit_connection(connection)


async def _commit_connection(connection: sqlite3.Connection) -> None:
    while True:
        try:
            connection.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if not _sqlite_lock_error(exc):
                raise
            await asyncio.sleep(_LOCK_RETRY_SECONDS)
        else:
            return


async def _begin_immediate(connection: sqlite3.Connection) -> None:
    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if not _sqlite_lock_error(exc):
                raise
            await asyncio.sleep(_LOCK_RETRY_SECONDS)
        else:
            return


async def _set_synchronous_extra(connection: sqlite3.Connection) -> None:
    while True:
        try:
            connection.execute("PRAGMA synchronous = EXTRA")
        except sqlite3.OperationalError as exc:
            if not _sqlite_lock_error(exc):
                raise
            await asyncio.sleep(_LOCK_RETRY_SECONDS)
        else:
            return


async def _enter_delete_journal(connection: sqlite3.Connection) -> None:
    while True:
        try:
            mode = str(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if not _sqlite_lock_error(exc):
                raise
            await asyncio.sleep(_LOCK_RETRY_SECONDS)
            continue
        if mode != "delete":
            msg = "OAuth credential store requires SQLite rollback-journal mode"
            raise OAuthProviderError(msg)
        return


def _prepare_database_path(database_path: Path) -> None:
    database_path.parent.chmod(0o700)
    if database_path.is_symlink() or database_path.exists():
        _validate_existing_database_path(database_path)
        return
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(database_path, flags, 0o600)
    except FileExistsError:
        _validate_existing_database_path(database_path)
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(database_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _validate_existing_database_path(database_path: Path) -> None:
    if database_path.is_symlink():
        msg = "OAuth credential database path cannot be a symlink"
        raise OAuthProviderError(msg)
    try:
        mode = database_path.stat().st_mode
    except FileNotFoundError as exc:
        msg = "OAuth credential database path disappeared during creation"
        raise OAuthProviderError(msg) from exc
    if not stat.S_ISREG(mode):
        msg = "OAuth credential database path must be a regular file"
        raise OAuthProviderError(msg)
    database_path.chmod(0o600)


def _legacy_credential_payload(context: _OAuthCredentialStoreContext) -> tuple[bytes | None, bool, bool]:
    legacy_path = _legacy_credential_path(context)
    try:
        raw = legacy_path.read_bytes()
    except FileNotFoundError:
        return None, False, False
    manager = context.credentials_manager
    try:
        credentials = manager.decode_credentials(context.provider.credential_service, raw)
    except (OSError, TypeError, ValueError, InvalidTag):
        retain_payload = not manager.credentials_encryption_enabled or manager.payload_is_encrypted(raw)
        return (raw if retain_payload else None), True, True
    normalized = _without_legacy_publication(credentials)
    return manager.encode_credentials(context.provider.credential_service, normalized), True, False


def _legacy_credential_path(context: _OAuthCredentialStoreContext) -> Path:
    return scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )


def _cleanup_legacy_files(context: _OAuthCredentialStoreContext) -> None:
    credential_path = _legacy_credential_path(context)
    paths = (
        credential_path,
        credential_path.with_name(f"{credential_path.name}.oauth-generation.json"),
        credential_path.with_name(f"{credential_path.name}.oauth-operation.lock"),
        credential_path.with_name(f"{credential_path.name}.oauth-refresh.lock"),
    )
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "oauth_legacy_credential_cleanup_failed",
                path=str(path),
                error_type=type(exc).__name__,
            )


def _scope_binding(context: _OAuthCredentialStoreContext) -> dict[str, str]:
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


def _without_legacy_publication(credentials: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(credentials)
    result.pop(_LEGACY_PUBLICATION_KEY, None)
    return result


def _sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    return exc.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
