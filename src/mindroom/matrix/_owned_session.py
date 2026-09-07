"""Private owned Matrix-session construction for managed agent accounts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol
from uuid import UUID

import nio
from nio.durable import DurableSync, DurableSyncConfig, open_durable_sync
from nio.store.database import DefaultStore

from mindroom.event_journal.models import IngestionConsumer
from mindroom.logging_config import get_logger
from mindroom.matrix.client_session import (
    MindRoomAsyncClient,
    create_matrix_http_client,
    matrix_client_config,
    matrix_startup_error,
    maybe_ssl_context,
    olm_store_dir,
    olm_store_exists,
    require_runtime_paths_arg,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths

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
    session: DurableSync
    consumer: IngestionConsumer


def _raise_owned_factory_value_error(message: str) -> NoReturn:
    raise ValueError(message)


async def login_password_credentials(
    homeserver: str,
    user_id: str,
    password: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
    device_id: str | None = None,
) -> MatrixCredentials:
    """Obtain password credentials and close HTTP before store construction."""
    temporary = create_matrix_http_client(
        homeserver,
        runtime_paths,
        user_id,
        http_headers=http_headers,
    )
    temporary.device_id = device_id or ""
    try:
        response = await temporary.login(password)
    finally:
        await temporary.close()
    if not isinstance(response, nio.LoginResponse):
        msg = f"Failed to login {user_id}: {response}"
        raise matrix_startup_error(msg, response=response)
    if response.user_id != user_id or (device_id is not None and response.device_id != device_id):
        msg = "Matrix login changed the managed account or device; restore the original device before restarting"
        raise matrix_startup_error(msg, permanent=True)
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
) -> MatrixCredentials | None:
    """Verify persisted credentials; only a soft logout permits device renewal."""
    temporary = create_matrix_http_client(
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
    if isinstance(response, nio.WhoamiError) and response.status_code == "M_UNKNOWN_TOKEN" and response.soft_logout:
        return None
    if not isinstance(response, nio.WhoamiResponse):
        msg = f"Failed to restore Matrix login for {user_id}: {response}"
        raise matrix_startup_error(msg, response=response)
    if response.user_id != user_id or (response.device_id and response.device_id != device_id):
        msg = "Stored Matrix credentials changed account or device; restore the original device before restarting"
        raise matrix_startup_error(msg, permanent=True)
    return MatrixCredentials(
        response.user_id,
        response.device_id or device_id,
        access_token,
    )


def _prepare_owned_store(
    consumer: IngestionConsumer,
    credentials: MatrixCredentials,
    runtime_paths: RuntimePaths,
) -> Path:
    """Validate the durable consumer and secure its per-account crypto directory."""
    if (
        type(consumer) is not IngestionConsumer
        or type(consumer.generation) is not UUID
        or (consumer.stream_id is not None and type(consumer.stream_id) is not UUID)
    ):
        _raise_owned_factory_value_error("ingestion consumer generation is invalid")

    if consumer.stream_id is not None and not olm_store_exists(
        credentials.user_id,
        credentials.device_id,
        runtime_paths,
    ):
        msg = "The bound Matrix device store is missing; restore its backup before restarting. Automatic device replacement is unsupported"
        raise matrix_startup_error(msg, permanent=True)

    store_path = olm_store_dir(credentials.user_id, runtime_paths)
    store_path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        store_path.chmod(0o700)
    return store_path


async def open_owned_matrix_session(
    homeserver: str,
    credentials: MatrixCredentials,
    runtime_paths: RuntimePaths,
    *,
    consumer_store: IngestionConsumerStore,
    new_consumer_generation: UUID,
    config: DurableSyncConfig,
    http_headers: Mapping[str, str] | None = None,
    persist_credentials: Callable[[nio.AsyncClient], None] | None = None,
) -> OwnedMatrixSession:
    """Bind one durable consumer and transfer one exact owned Matrix store."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    if type(credentials) is not MatrixCredentials or type(new_consumer_generation) is not UUID:
        msg = "Owned sessions require MatrixCredentials and a UUID consumer generation"
        raise TypeError(msg)
    consumer = await consumer_store.load_or_create_ingestion_consumer(
        new_generation=new_consumer_generation,
    )
    store_path = _prepare_owned_store(consumer, credentials, runtime_paths)
    database_name = f"{credentials.user_id}_{credentials.device_id}.db"
    client = MindRoomAsyncClient(
        homeserver,
        credentials.user_id,
        device_id=credentials.device_id,
        store_path=None,
        config=matrix_client_config(http_headers=http_headers),
        ssl=maybe_ssl_context(homeserver, runtime_paths=runtime_paths),
    )
    client.user_id = credentials.user_id
    client.device_id = credentials.device_id
    client.access_token = credentials.access_token
    session = None
    try:
        session = open_durable_sync(
            client,
            consumer_id=consumer.generation,
            store_path=store_path,
            database_name=database_name,
            config=config,
            source_store_class=DefaultStore,
        )
        if consumer.stream_id is not None and consumer.stream_id != session.stream_id:
            msg = "Matrix stream changed; restore the bound device store before restarting"
            raise matrix_startup_error(msg, permanent=True)  # noqa: TRY301
        # Persist the exact device after its store exists, before binding can survive a crash.
        if persist_credentials is not None:
            persist_credentials(client)
        bound_consumer = await consumer_store.bind_ingestion_stream(
            generation=consumer.generation,
            stream_id=session.stream_id,
        )
        if bound_consumer != IngestionConsumer(consumer.generation, session.stream_id):
            _raise_owned_factory_value_error("ingestion stream binding is invalid")
        return OwnedMatrixSession(client, session, bound_consumer)
    except BaseException as error:
        if session is not None:
            try:
                await session.close()
            except BaseException:
                logger.exception("owned_matrix_session_cleanup_failed")
        try:
            await client.close()
        except BaseException:
            logger.exception("owned_matrix_http_cleanup_failed")
        if isinstance(error, nio.LocalProtocolError):
            raise matrix_startup_error(str(error), permanent=True) from error
        raise
