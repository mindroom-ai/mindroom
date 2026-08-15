"""Private owned Matrix-session construction for managed agent accounts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol
from uuid import UUID

import nio
from nio.ingest.coordinator import _open_owned_ingestion
from nio.ingest.errors import _MarkedStoreRequiresSqlite
from nio.store.database import DefaultStore, SqliteStore
from nio.store.sync_journal import (
    StoreBootstrap,
    _open_configured_ingestion_store,
    _open_fresh_ingestion_store,
)

from mindroom.constants import RuntimePaths
from mindroom.event_journal.models import IngestionConsumer
from mindroom.logging_config import get_logger
from mindroom.matrix.client_session import (
    MindRoomAsyncClient,
    matrix_client_config,
    matrix_startup_error,
    maybe_ssl_context,
    olm_store_dir,
    require_runtime_paths_arg,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from nio.ingest.config import IngestionConfig
    from nio.ingest.coordinator import _FrameCompletion, _OwnedIngestionSession

logger = get_logger(__name__)

__all__ = [
    "IngestionConsumerStore",
    "MatrixCredentials",
    "OwnedMatrixSession",
    "login_password_credentials",
    "open_owned_matrix_session",
    "restore_credentials",
]


@dataclass(frozen=True, slots=True)
class MatrixCredentials:
    """Exact login result carried across the no-store credential boundary."""

    user_id: str
    device_id: str
    access_token: str


class IngestionConsumerStore(Protocol):
    """The consumer-binding methods needed before owned session transfer."""

    async def load_or_create_ingestion_consumer(
        self,
        *,
        new_generation: UUID,
    ) -> IngestionConsumer: ...

    async def bind_ingestion_stream(
        self,
        *,
        generation: UUID,
        stream_id: UUID,
    ) -> IngestionConsumer: ...


@dataclass(frozen=True, slots=True)
class OwnedMatrixSession:
    """One authenticated client and its separately owned ingestion session."""

    client: nio.AsyncClient
    session: _OwnedIngestionSession
    consumer: IngestionConsumer


def _raise_owned_factory_value_error(message: str) -> NoReturn:
    raise ValueError(message)


def _open_owned_store_bootstrap(
    store_path: Path,
    *,
    database_name: str,
    account_id: str,
    device_id: str,
    consumer_generation: UUID,
    config: IngestionConfig,
    pickle_key: str,
) -> StoreBootstrap:
    """Open one fresh, legacy-adoption, or typed marked-reopen bootstrap."""
    database_path = store_path / database_name
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return _open_fresh_ingestion_store(
            store_path,
            account_id=account_id,
            device_id=device_id,
            consumer_generation=consumer_generation,
            source=config.source,
            pickle_key=pickle_key,
            database_name=database_name,
            sqlite_busy_timeout_ms=config.sqlite_busy_timeout_ms,
        )
    try:
        return _open_configured_ingestion_store(
            store_path,
            source_store_class=DefaultStore,
            owned_store_class=SqliteStore,
            account_id=account_id,
            device_id=device_id,
            consumer_generation=consumer_generation,
            source=config.source,
            pickle_key=pickle_key,
            database_name=database_name,
            sqlite_busy_timeout_ms=config.sqlite_busy_timeout_ms,
        )
    except _MarkedStoreRequiresSqlite:
        return _open_configured_ingestion_store(
            store_path,
            source_store_class=SqliteStore,
            owned_store_class=SqliteStore,
            account_id=account_id,
            device_id=device_id,
            consumer_generation=consumer_generation,
            source=config.source,
            pickle_key=pickle_key,
            database_name=database_name,
            sqlite_busy_timeout_ms=config.sqlite_busy_timeout_ms,
        )


async def _cleanup_failed_owned_matrix_open(
    bootstrap: StoreBootstrap | None,
    client: nio.AsyncClient | None,
) -> None:
    """Run every pre-transfer cleanup lane while preserving the primary error."""
    if bootstrap is not None:
        try:
            bootstrap.close()
        except BaseException:
            logger.exception("owned_matrix_bootstrap_cleanup_failed")
    if client is not None:
        try:
            await client.close()
        except BaseException:
            logger.exception("owned_matrix_http_cleanup_failed")


def _create_credential_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Create the temporary HTTP-only client used before any store lease."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    return MindRoomAsyncClient(
        homeserver,
        user_id,
        store_path=None,
        config=matrix_client_config(http_headers=http_headers),
        ssl=maybe_ssl_context(homeserver, runtime_paths=runtime_paths),  # ty: ignore[invalid-argument-type]
    )


async def login_password_credentials(
    homeserver: str,
    user_id: str,
    password: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> MatrixCredentials:
    """Obtain password credentials and close HTTP before store construction."""
    temporary = _create_credential_client(
        homeserver,
        runtime_paths,
        user_id,
        http_headers=http_headers,
    )
    try:
        response = await temporary.login(password)
    finally:
        await temporary.close()
    if not isinstance(response, nio.LoginResponse):
        msg = f"Failed to login {user_id}: {response}"
        raise matrix_startup_error(msg, response=response)
    return MatrixCredentials(
        response.user_id,
        response.device_id,
        response.access_token,
    )


async def restore_credentials(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> MatrixCredentials:
    """Verify persisted credentials without opening their configured store."""
    temporary = _create_credential_client(
        homeserver,
        runtime_paths,
        user_id,
        http_headers=http_headers,
    )
    temporary.user_id = user_id
    temporary.device_id = device_id
    temporary.access_token = access_token
    try:
        response = await temporary.whoami()
    finally:
        await temporary.close()
    if not isinstance(response, nio.WhoamiResponse):
        msg = f"Failed to restore Matrix login for {user_id}: {response}"
        raise matrix_startup_error(msg, response=response)
    return MatrixCredentials(
        response.user_id,
        response.device_id or device_id,
        access_token,
    )


async def open_owned_matrix_session(
    homeserver: str,
    credentials: MatrixCredentials,
    runtime_paths: RuntimePaths,
    *,
    consumer_store: IngestionConsumerStore,
    new_consumer_generation: UUID,
    config: IngestionConfig,
    http_headers: Mapping[str, str] | None = None,
    completion_sink: Callable[[_FrameCompletion], Awaitable[None]] | None = None,
) -> OwnedMatrixSession:
    """Bind one durable consumer and transfer one exact owned Matrix store."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    if type(credentials) is not MatrixCredentials:
        msg = "credentials must be MatrixCredentials"
        raise TypeError(msg)
    if type(new_consumer_generation) is not UUID:
        msg = "new_consumer_generation must be UUID"
        raise TypeError(msg)
    consumer = await consumer_store.load_or_create_ingestion_consumer(
        new_generation=new_consumer_generation,
    )
    if type(consumer) is not IngestionConsumer or consumer.generation != new_consumer_generation:
        _raise_owned_factory_value_error("ingestion consumer generation is invalid")

    store_path = olm_store_dir(credentials.user_id, runtime_paths)
    database_name = f"{credentials.user_id}_{credentials.device_id}.db"
    client_config = replace(
        matrix_client_config(http_headers=http_headers),
        store=SqliteStore,
    )
    bootstrap = None
    client: nio.AsyncClient | None = None
    try:
        bootstrap = _open_owned_store_bootstrap(
            store_path,
            database_name=database_name,
            account_id=credentials.user_id,
            device_id=credentials.device_id,
            consumer_generation=consumer.generation,
            config=config,
            pickle_key=client_config.pickle_key,
        )

        bound_consumer = await consumer_store.bind_ingestion_stream(
            generation=consumer.generation,
            stream_id=bootstrap.stream_id,
        )
        if bound_consumer != IngestionConsumer(
            consumer.generation,
            bootstrap.stream_id,
        ):
            _raise_owned_factory_value_error("ingestion stream binding is invalid")

        client = MindRoomAsyncClient(
            homeserver,
            credentials.user_id,
            device_id=credentials.device_id,
            store_path=str(store_path),
            config=client_config,
            ssl=maybe_ssl_context(homeserver, runtime_paths=runtime_paths),  # ty: ignore[invalid-argument-type]
        )
        client.user_id = credentials.user_id
        client.device_id = credentials.device_id
        client.access_token = credentials.access_token
        session = _open_owned_ingestion(
            client,
            bootstrap,
            config=config,
            consumer_generation=bound_consumer.generation,
            stream_id=bound_consumer.stream_id,
            _completion_sink=completion_sink,
        )
        return OwnedMatrixSession(client, session, bound_consumer)
    except BaseException:
        await _cleanup_failed_owned_matrix_open(bootstrap, client)
        raise
