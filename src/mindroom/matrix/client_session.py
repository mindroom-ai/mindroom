"""Matrix session lifecycle helpers."""

from __future__ import annotations

import os
import ssl as ssl_module
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import nio

from mindroom.constants import RuntimePaths, encryption_keys_dir, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.encrypted_event_metadata import encryption_visible_metadata
from mindroom.matrix.event_types import CALL_ENCRYPTION_KEYS_EVENT_TYPE
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from nio.crypto import DeviceStore

logger = get_logger(__name__)

_PERMANENT_MATRIX_STARTUP_ERROR_CODES = frozenset(
    {
        "M_FORBIDDEN",
        "M_USER_DEACTIVATED",
        "M_UNKNOWN_TOKEN",
        "M_INVALID_USERNAME",
    },
)


def _log_custom_olm_rejection(
    event: nio.UnknownToDeviceEvent,
    reason: str,
    **details: object,
) -> None:
    """Log why a security-sensitive custom event failed provenance checks."""
    log_event = "call_key_olm_rejected" if event.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE else "custom_olm_rejected"
    logger.warning(
        log_event,
        sender=event.sender,
        event_type=event.type,
        reason=reason,
        **details,
    )


class PermanentMatrixStartupError(PermanentStartupError):
    """Raised for Matrix startup failures that should not be retried."""


class _MatrixTransportShutdownError(RuntimeError):
    """Raised when process shutdown has permanently fenced Matrix transport."""

    def __init__(self) -> None:
        super().__init__("Matrix transport is fenced for process shutdown")


@dataclass(frozen=True, slots=True)
class MatrixSyncStorage:
    """Select whether nio persists the ordinary sync cursor."""

    store_tokens: bool = True


DEFAULT_MATRIX_SYNC_STORAGE = MatrixSyncStorage()


@runtime_checkable
class _AsyncRequestHeaders(Protocol):
    async def prepare(self) -> None:
        """Prepare dynamic headers without blocking the event loop."""
        ...


class MindRoomAsyncClient(nio.AsyncClient):
    """Matrix client for MindRoom-specific encrypted event behavior."""

    _process_shutdown_transport_fenced = False

    @property
    def process_shutdown_transport_fenced(self) -> bool:
        """Return whether orderly shutdown permanently closed new transport."""
        return self._process_shutdown_transport_fenced

    async def send(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Prepare dynamic request headers before every transport attempt."""
        if self._process_shutdown_transport_fenced:
            raise _MatrixTransportShutdownError
        headers = self.config.custom_headers
        if isinstance(headers, _AsyncRequestHeaders):
            await headers.prepare()
        if self._process_shutdown_transport_fenced:
            raise _MatrixTransportShutdownError
        return await super().send(*args, **kwargs)

    def begin_process_shutdown_transport_fence(self) -> None:
        """Permanently refuse new requests before owned work is drained."""
        self._process_shutdown_transport_fenced = True

    def encrypt(
        self,
        room_id: str,
        message_type: str,
        content: dict[Any, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Expose coarse delivery markers needed without decrypting room history."""
        encrypted_message_type, encrypted_content = super().encrypt(
            room_id,
            message_type,
            content,
        )
        encrypted_content.update(encryption_visible_metadata(content))
        return encrypted_message_type, encrypted_content

    def _handle_olm_events(
        self,
        response: object,
    ) -> None:
        """Preserve an explicit zero OTK count so nio replenishes a drained pool."""
        sync_response = cast("nio.SyncResponse", response)
        super()._handle_olm_events(sync_response)
        count = sync_response.device_key_count.signed_curve25519
        if self.olm is not None and count is not None:
            self.olm.uploaded_key_count = count

    def _handle_decrypt_to_device(
        self,
        to_device_event: nio.ToDeviceEvent | nio.BadEvent | nio.UnknownBadEvent,
    ) -> nio.ToDeviceEvent | nio.BadEvent | nio.UnknownBadEvent | None:
        decrypted = super()._handle_decrypt_to_device(to_device_event)
        if not isinstance(to_device_event, nio.OlmEvent):
            return decrypted
        return cast(
            "nio.ToDeviceEvent | nio.BadEvent | nio.UnknownBadEvent | None",
            self.authenticate_to_device(to_device_event.source, decrypted),
        )

    def authenticate_to_device(self, source: dict[str, object], event: object) -> object:
        """Authenticate fresh or restored custom events against current device keys."""
        if not isinstance(event, nio.UnknownToDeviceEvent):
            return event
        result = authenticate_to_device_event(
            source,
            event,
            device_store=self.olm.device_store if self.olm is not None else None,
        )
        if not isinstance(result, AuthenticatedToDeviceEvent):
            if self.olm is not None:
                self.olm.users_for_key_query.add(event.sender)
            _log_custom_olm_rejection(event, "signed_device_authentication_failed")
        elif result.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE:
            logger.info(
                "call_key_olm_authenticated",
                sender=result.sender,
                sender_device=result.authenticated_device_id,
            )
        return result


def authenticate_to_device_event(  # noqa: PLR0911 - fail-closed identity checks
    source: dict[str, object],
    event: object,
    *,
    device_store: DeviceStore | None,
) -> object:
    """Pure post-decrypt authentication shared by live and durable replay paths."""
    if not isinstance(event, nio.UnknownToDeviceEvent):
        return event
    if device_store is None or source.get("type") != "m.room.encrypted" or source.get("sender") != event.sender:
        return event
    content = source.get("content")
    if not isinstance(content, dict):
        return event
    envelope = cast("dict[str, object]", content)
    if envelope.get("algorithm") != "m.olm.v1.curve25519-aes-sha2":
        return event
    sender_key = envelope.get("sender_key")
    matching = [device for device in device_store.active_user_devices(event.sender) if device.curve25519 == sender_key]
    if len(matching) != 1:
        return event
    device = matching[0]
    sender_device = event.source.get("sender_device")
    sender_keys = event.source.get("keys")
    if sender_device is not None and sender_device != device.id:
        return event
    if sender_keys is not None and (not isinstance(sender_keys, dict) or sender_keys.get("ed25519") != device.ed25519):
        return event
    return AuthenticatedToDeviceEvent(
        source=event.source,
        sender=event.sender,
        type=event.type,
        authenticated_device_id=device.id,
    )


def require_runtime_paths_arg(runtime_paths: object) -> RuntimePaths:
    """Reject stale positional call shapes with a clear error."""
    if isinstance(runtime_paths, RuntimePaths):
        return runtime_paths
    msg = (
        "matrix_client() requires RuntimePaths as its second argument. "
        "Call matrix_client(homeserver, runtime_paths, user_id=...)"
    )
    raise TypeError(msg)


def matrix_startup_error(
    message: str,
    *,
    response: object | None = None,
    permanent: bool = False,
) -> ValueError:
    """Return the appropriate startup exception type for a Matrix failure."""
    if permanent:
        return PermanentMatrixStartupError(message)
    if isinstance(response, nio.ErrorResponse) and response.status_code in _PERMANENT_MATRIX_STARTUP_ERROR_CODES:
        return PermanentMatrixStartupError(message)
    return ValueError(message)


def maybe_ssl_context(
    homeserver: str,
    runtime_paths: RuntimePaths,
) -> ssl_module.SSLContext | None:
    """Return the configured Matrix SSL context when HTTPS requires one."""
    if homeserver.startswith("https://"):
        if not runtime_matrix_ssl_verify(runtime_paths=runtime_paths):
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            ssl_context = ssl_module.create_default_context()
        return ssl_context
    return None


def olm_store_dir(user_id: str, runtime_paths: RuntimePaths) -> Path:
    """Return the per-user encryption store directory."""
    safe_user_id = user_id.replace(":", "_").replace("@", "")
    return encryption_keys_dir(runtime_paths=runtime_paths) / safe_user_id


def olm_store_exists(user_id: str, device_id: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether the persisted olm store for one device is present on disk."""
    # nio's SqliteStore names its database {user_id}_{device_id}.db inside store_path.
    return (olm_store_dir(user_id, runtime_paths) / f"{user_id}_{device_id}.db").is_file()


def matrix_client_config(
    *,
    http_headers: Mapping[str, str] | None = None,
    sync_storage: MatrixSyncStorage = DEFAULT_MATRIX_SYNC_STORAGE,
) -> nio.AsyncClientConfig:
    """Return nio config, copying plain headers while preserving request-time mappings."""
    custom_headers = dict(http_headers) if isinstance(http_headers, dict) else http_headers
    return nio.AsyncClientConfig(
        store_sync_tokens=sync_storage.store_tokens,
        custom_headers=cast("dict[str, str] | None", custom_headers),
        replace_rotated_device_keys=True,
    )


def _create_matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
    store_path: str | None = None,
    *,
    http_headers: Mapping[str, str] | None = None,
    sync_storage: MatrixSyncStorage = DEFAULT_MATRIX_SYNC_STORAGE,
) -> nio.AsyncClient:
    """Create a Matrix client with consistent configuration."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    ssl_context = maybe_ssl_context(homeserver, runtime_paths=runtime_paths)

    if store_path is None and user_id:
        store_path = str(olm_store_dir(user_id, runtime_paths=runtime_paths))
        store_dir = Path(store_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            store_dir.chmod(0o700)

    client = MindRoomAsyncClient(
        homeserver,
        user_id or "",
        store_path=store_path,
        # Agents trust devices on first use and never verify interactively;
        # accept a peer device's re-registered olm identity (trust reset)
        # instead of keeping stale keys that silently break E2EE and calls.
        config=matrix_client_config(
            http_headers=http_headers,
            sync_storage=sync_storage,
        ),
        ssl=ssl_context,
    )
    if user_id:
        client.user_id = user_id
    if access_token:
        client.access_token = access_token
    return client


def create_matrix_http_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Create an HTTP-only client that cannot open the managed crypto store."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    client = MindRoomAsyncClient(
        homeserver,
        user_id,
        store_path=None,
        config=replace(matrix_client_config(http_headers=http_headers), encryption_enabled=False),
        ssl=maybe_ssl_context(homeserver, runtime_paths=runtime_paths),
    )
    client.user_id = user_id
    return client


def create_authenticated_client(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
    sync_storage: MatrixSyncStorage = DEFAULT_MATRIX_SYNC_STORAGE,
) -> nio.AsyncClient:
    """Create a Matrix client from newly issued login credentials."""
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        access_token,
        http_headers=http_headers,
        sync_storage=sync_storage,
    )
    client.restore_login(user_id, device_id, access_token)
    return client


@asynccontextmanager
async def matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
) -> AsyncGenerator[nio.AsyncClient, None]:
    """Context manager for Matrix client that ensures proper cleanup."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, access_token)
    try:
        yield client
    finally:
        await client.close()


async def login(
    homeserver: str,
    user_id: str,
    password: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
    sync_storage: MatrixSyncStorage = DEFAULT_MATRIX_SYNC_STORAGE,
) -> nio.AsyncClient:
    """Login to Matrix and return an authenticated client."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        http_headers=http_headers,
        sync_storage=sync_storage,
    )

    response = await client.login(password)
    if isinstance(response, nio.LoginResponse):
        client.user_id = response.user_id
        client.device_id = response.device_id
        client.access_token = response.access_token
        logger.info("matrix_login_succeeded", user_id=response.user_id)
        return client
    await client.close()
    msg = f"Failed to login {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


async def login_with_token(
    homeserver: str,
    login_token: str,
    runtime_paths: RuntimePaths,
    *,
    expected_user_id: str | None = None,
    http_headers: Mapping[str, str] | None = None,
    sync_storage: MatrixSyncStorage = DEFAULT_MATRIX_SYNC_STORAGE,
) -> nio.AsyncClient:
    """Exchange one short-lived Matrix login token and restore its exact device."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    login_client = _create_matrix_client(
        homeserver,
        runtime_paths,
        http_headers=http_headers,
        sync_storage=sync_storage,
    )
    try:
        response = await login_client.login(
            token=login_token,
            device_name="MindRoom Desktop Bridge",
        )
        if not isinstance(response, nio.LoginResponse):
            msg = f"Failed to exchange Matrix login token: {response}"
            raise matrix_startup_error(msg, response=response)
        if expected_user_id is not None and response.user_id != expected_user_id:
            await _revoke_unexpected_login(
                login_client,
                expected_user_id=expected_user_id,
                actual_user_id=response.user_id,
            )
            msg = f"Matrix SSO returned {response.user_id}, but {expected_user_id} was requested."
            raise matrix_startup_error(msg, permanent=True)
        credentials = (response.user_id, response.device_id, response.access_token)
    finally:
        await login_client.close()

    user_id, device_id, access_token = credentials
    logger.info("matrix_login_succeeded", user_id=user_id, login_method="token")
    return create_authenticated_client(
        homeserver,
        user_id,
        device_id,
        access_token,
        runtime_paths,
        http_headers=http_headers,
        sync_storage=sync_storage,
    )


async def _revoke_unexpected_login(
    client: nio.AsyncClient,
    *,
    expected_user_id: str,
    actual_user_id: str,
) -> None:
    """Best-effort revoke an SSO session issued for an unexpected identity."""
    try:
        response = await client.logout()
    except Exception:
        logger.warning(
            "matrix_unexpected_sso_session_revoke_failed",
            expected_user_id=expected_user_id,
            actual_user_id=actual_user_id,
            exc_info=True,
        )
        return
    if isinstance(response, nio.ErrorResponse):
        logger.warning(
            "matrix_unexpected_sso_session_revoke_failed",
            expected_user_id=expected_user_id,
            actual_user_id=actual_user_id,
            error=str(response),
        )


async def login_flows(
    homeserver: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return login methods advertised by one Matrix homeserver."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, http_headers=http_headers)
    try:
        response = await client.login_info()
    finally:
        await client.close()
    if isinstance(response, nio.LoginInfoResponse):
        return tuple(response.flows)
    msg = f"Failed to query Matrix login methods: {response}"
    raise matrix_startup_error(msg, response=response)


async def restore_login(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
    sync_storage: MatrixSyncStorage = DEFAULT_MATRIX_SYNC_STORAGE,
) -> nio.AsyncClient:
    """Restore one authenticated Matrix session without creating a new device."""
    runtime_paths = require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        access_token,
        http_headers=http_headers,
        sync_storage=sync_storage,
    )
    client.restore_login(user_id, device_id, access_token)

    response = await client.whoami()
    if isinstance(response, nio.WhoamiResponse):
        client.user_id = response.user_id
        if response.device_id:
            client.device_id = response.device_id
        logger.info(
            "matrix_login_restored",
            user_id=response.user_id,
            device_id=client.device_id,
        )
        return client

    await client.close()
    msg = f"Failed to restore Matrix login for {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


__all__ = [
    "DEFAULT_MATRIX_SYNC_STORAGE",
    "MatrixSyncStorage",
    "MindRoomAsyncClient",
    "PermanentMatrixStartupError",
    "authenticate_to_device_event",
    "create_authenticated_client",
    "create_matrix_http_client",
    "login",
    "login_flows",
    "login_with_token",
    "matrix_client",
    "matrix_client_config",
    "matrix_startup_error",
    "maybe_ssl_context",
    "olm_store_dir",
    "olm_store_exists",
    "require_runtime_paths_arg",
    "restore_login",
]
