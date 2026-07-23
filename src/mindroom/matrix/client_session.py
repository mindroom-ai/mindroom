"""Matrix session lifecycle helpers."""

from __future__ import annotations

import os
import ssl as ssl_module
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import nio

from mindroom.constants import STREAM_STATUS_KEY, RuntimePaths, encryption_keys_dir, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import CALL_ENCRYPTION_KEYS_EVENT_TYPE
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

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


@runtime_checkable
class _AsyncRequestHeaders(Protocol):
    async def prepare(self) -> None:
        """Prepare dynamic headers without blocking the event loop."""
        ...


class _MindRoomAsyncClient(nio.AsyncClient):
    """Matrix client for MindRoom-specific encrypted event behavior."""

    async def send(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Prepare dynamic request headers before every transport attempt."""
        headers = self.config.custom_headers
        if isinstance(headers, _AsyncRequestHeaders):
            await headers.prepare()
        return await super().send(*args, **kwargs)

    def encrypt(
        self,
        room_id: str,
        message_type: str,
        content: dict[Any, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Expose only the coarse stream state needed for encrypted push routing."""
        encrypted_message_type, encrypted_content = super().encrypt(room_id, message_type, content)
        stream_status = content.get(STREAM_STATUS_KEY)
        if isinstance(stream_status, str):
            encrypted_content[STREAM_STATUS_KEY] = stream_status
        return encrypted_message_type, encrypted_content

    def _handle_olm_events(self, response: nio.SyncResponse | nio.SlidingSyncResponse) -> None:
        """Preserve an explicit zero OTK count so nio replenishes a drained pool."""
        super()._handle_olm_events(response)
        count = response.device_key_count.signed_curve25519
        if self.olm is not None and count is not None:
            self.olm.uploaded_key_count = count

    def _handle_decrypt_to_device(self, to_device_event: nio.ToDeviceEvent) -> nio.ToDeviceEvent | None:
        decrypted = super()._handle_decrypt_to_device(to_device_event)
        if not isinstance(to_device_event, nio.OlmEvent) or not isinstance(decrypted, nio.UnknownToDeviceEvent):
            return decrypted
        if self.olm is None:
            _log_custom_olm_rejection(decrypted, "missing_olm_machine")
            return decrypted
        matching_devices = [
            device
            for device in self.olm.device_store.active_user_devices(decrypted.sender)
            if device.curve25519 == to_device_event.sender_key
        ]
        if len(matching_devices) != 1:
            if not matching_devices:
                self.olm.users_for_key_query.add(decrypted.sender)
            _log_custom_olm_rejection(
                decrypted,
                "curve25519_device_match_count",
                matching_device_count=len(matching_devices),
                key_query_queued=not matching_devices,
            )
            return decrypted
        device = matching_devices[0]

        # The Olm envelope authenticates possession of ``sender_key`` and nio
        # verifies that the sender in the decrypted payload matches the
        # envelope sender. Matrix clients do not all include nio's optional
        # ``sender_device``/``keys`` fields in custom Olm payloads, so map the
        # authenticated curve25519 key to the uniquely matching device from
        # the signed device-key store. If redundant identity fields are
        # present, continue to enforce them as consistency checks.
        sender_device = decrypted.source.get("sender_device")
        sender_keys = decrypted.source.get("keys")
        sender_ed25519 = sender_keys.get("ed25519") if isinstance(sender_keys, dict) else None
        if sender_device is not None and sender_device != device.id:
            _log_custom_olm_rejection(
                decrypted,
                "signed_sender_identity_mismatch",
                sender_device=sender_device,
                matched_device_id=device.id,
            )
            return decrypted
        if sender_keys is not None and sender_ed25519 != device.ed25519:
            _log_custom_olm_rejection(
                decrypted,
                "signed_sender_identity_mismatch",
                sender_ed25519=sender_ed25519,
                matched_ed25519=device.ed25519,
            )
            return decrypted
        if decrypted.type == CALL_ENCRYPTION_KEYS_EVENT_TYPE:
            logger.info(
                "call_key_olm_authenticated",
                sender=decrypted.sender,
                sender_device=device.id,
            )
        return AuthenticatedToDeviceEvent(
            source=decrypted.source,
            sender=decrypted.sender,
            type=decrypted.type,
            authenticated_device_id=device.id,
        )


def _require_runtime_paths_arg(runtime_paths: object) -> RuntimePaths:
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


def _maybe_ssl_context(homeserver: str, runtime_paths: RuntimePaths) -> ssl_module.SSLContext | None:
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


def matrix_client_config(*, http_headers: Mapping[str, str] | None = None) -> nio.AsyncClientConfig:
    """Return nio config, copying plain headers while preserving request-time mappings."""
    custom_headers = dict(http_headers) if isinstance(http_headers, dict) else http_headers
    return nio.AsyncClientConfig(
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
) -> nio.AsyncClient:
    """Create a Matrix client with consistent configuration."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    ssl_context = _maybe_ssl_context(homeserver, runtime_paths=runtime_paths)

    if store_path is None and user_id:
        store_path = str(olm_store_dir(user_id, runtime_paths=runtime_paths))
        store_dir = Path(store_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            store_dir.chmod(0o700)

    client = _MindRoomAsyncClient(
        homeserver,
        user_id or "",
        store_path=store_path,
        # Agents trust devices on first use and never verify interactively;
        # accept a peer device's re-registered olm identity (trust reset)
        # instead of keeping stale keys that silently break E2EE and calls.
        config=matrix_client_config(http_headers=http_headers),
        ssl=ssl_context,  # ty: ignore[invalid-argument-type]
    )
    if user_id:
        client.user_id = user_id
    if access_token:
        client.access_token = access_token
    return client


def create_authenticated_client(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> nio.AsyncClient:
    """Create a Matrix client from newly issued login credentials."""
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        access_token,
        http_headers=http_headers,
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
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
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
) -> nio.AsyncClient:
    """Login to Matrix and return an authenticated client."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, http_headers=http_headers)

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
) -> nio.AsyncClient:
    """Exchange one short-lived Matrix login token and restore its exact device."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    login_client = _create_matrix_client(homeserver, runtime_paths, http_headers=http_headers)
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
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
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
) -> nio.AsyncClient:
    """Restore one authenticated Matrix session without creating a new device."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(
        homeserver,
        runtime_paths,
        user_id,
        access_token,
        http_headers=http_headers,
    )
    client.restore_login(user_id, device_id, access_token)

    response = await client.whoami()
    if isinstance(response, nio.WhoamiResponse):
        client.user_id = response.user_id
        if response.device_id:
            client.device_id = response.device_id
        logger.info("matrix_login_restored", user_id=response.user_id, device_id=client.device_id)
        return client

    await client.close()
    msg = f"Failed to restore Matrix login for {user_id}: {response}"
    raise matrix_startup_error(msg, response=response)


__all__ = [
    "PermanentMatrixStartupError",
    "create_authenticated_client",
    "login",
    "login_flows",
    "login_with_token",
    "matrix_client",
    "matrix_client_config",
    "matrix_startup_error",
    "olm_store_dir",
    "olm_store_exists",
    "restore_login",
]
