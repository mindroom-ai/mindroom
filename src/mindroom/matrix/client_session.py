"""Matrix session lifecycle helpers."""

from __future__ import annotations

import ssl as ssl_module
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import STREAM_STATUS_KEY, RuntimePaths, encryption_keys_dir, runtime_matrix_ssl_verify
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import CALL_ENCRYPTION_KEYS_EVENT_TYPE
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

_PERMANENT_MATRIX_STARTUP_ERROR_CODES = frozenset(
    {
        "M_FORBIDDEN",
        "M_USER_DEACTIVATED",
        "M_UNKNOWN_TOKEN",
        "M_INVALID_USERNAME",
    },
)


def _log_call_key_olm_rejection(
    event: nio.UnknownToDeviceEvent,
    reason: str,
    **details: object,
) -> None:
    """Log why an otherwise decrypted call-key event failed provenance checks."""
    if event.type != CALL_ENCRYPTION_KEYS_EVENT_TYPE:
        return
    logger.warning(
        "call_key_olm_rejected",
        sender=event.sender,
        reason=reason,
        **details,
    )


class PermanentMatrixStartupError(PermanentStartupError):
    """Raised for Matrix startup failures that should not be retried."""


class _MindRoomAsyncClient(nio.AsyncClient):
    """Matrix client for MindRoom-specific encrypted event behavior."""

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

    def _handle_olm_events(self, response: nio.SyncResponse) -> None:
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
            _log_call_key_olm_rejection(decrypted, "missing_olm_machine")
            return decrypted
        matching_devices = [
            device
            for device in self.olm.device_store.active_user_devices(decrypted.sender)
            if device.curve25519 == to_device_event.sender_key
        ]
        if len(matching_devices) != 1:
            _log_call_key_olm_rejection(
                decrypted,
                "curve25519_device_match_count",
                matching_device_count=len(matching_devices),
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
            _log_call_key_olm_rejection(
                decrypted,
                "signed_sender_identity_mismatch",
                sender_device=sender_device,
                matched_device_id=device.id,
            )
            return decrypted
        if sender_keys is not None and sender_ed25519 != device.ed25519:
            _log_call_key_olm_rejection(
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


def _create_matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    access_token: str | None = None,
    store_path: str | None = None,
) -> nio.AsyncClient:
    """Create a Matrix client with consistent configuration."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    ssl_context = _maybe_ssl_context(homeserver, runtime_paths=runtime_paths)

    if store_path is None and user_id:
        store_path = str(olm_store_dir(user_id, runtime_paths=runtime_paths))
        Path(store_path).mkdir(parents=True, exist_ok=True)

    client = _MindRoomAsyncClient(
        homeserver,
        user_id or "",
        store_path=store_path,
        # Agents trust devices on first use and never verify interactively;
        # accept a peer device's re-registered olm identity (trust reset)
        # instead of keeping stale keys that silently break E2EE and calls.
        config=nio.AsyncClientConfig(replace_rotated_device_keys=True),
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
) -> nio.AsyncClient:
    """Create a Matrix client from newly issued login credentials."""
    client = _create_matrix_client(homeserver, runtime_paths, user_id, access_token)
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
) -> nio.AsyncClient:
    """Login to Matrix and return an authenticated client."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id)

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


async def restore_login(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Restore one authenticated Matrix session without creating a new device."""
    runtime_paths = _require_runtime_paths_arg(runtime_paths)
    client = _create_matrix_client(homeserver, runtime_paths, user_id, access_token)
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
    "matrix_client",
    "matrix_startup_error",
    "olm_store_dir",
    "olm_store_exists",
    "restore_login",
]
