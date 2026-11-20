"""Matrix session lifecycle helpers."""

from __future__ import annotations

import ssl as ssl_module
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import aiohttp
import nio
from nio.client.base_client import logged_in_async

from mindroom.constants import (
    RuntimePaths,
    encryption_keys_dir,
    runtime_matrix_homeserver,
    runtime_matrix_ssl_verify,
)
from mindroom.failure_text import failure_text
from mindroom.logging_config import get_logger
from mindroom.matrix.encrypted_event_metadata import encryption_visible_metadata
from mindroom.startup_errors import PermanentStartupError
from mindroom.tls_trust import verifying_ssl_context

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path


logger = get_logger(__name__)

_PERMANENT_MATRIX_STARTUP_ERROR_CODES = frozenset(
    {
        "M_FORBIDDEN",
        "M_USER_DEACTIVATED",
        "M_UNKNOWN_TOKEN",
        "M_INVALID_USERNAME",
    },
)


class PermanentMatrixStartupError(PermanentStartupError):
    """Raised for Matrix startup failures that should not be retried."""


class _MatrixTransportShutdownError(RuntimeError):
    """Raised when process shutdown has permanently fenced Matrix transport."""

    def __init__(self) -> None:
        super().__init__("Matrix transport is fenced for process shutdown")


@runtime_checkable
class _AsyncRequestHeaders(Protocol):
    async def prepare(self) -> None:
        """Prepare dynamic headers without blocking the event loop."""
        ...


_STATE_EVENT_STRING_FIELDS = ("type", "state_key", "sender", "event_id")


def _is_state_event(event: object) -> bool:
    """Return whether one room-state entry has the fields nio's room-state schema requires."""
    if not isinstance(event, dict):
        return False
    fields = cast("dict[str, object]", event)
    if not all(isinstance(fields.get(field), str) for field in _STATE_EVENT_STRING_FIELDS):
        return False
    return isinstance(fields.get("origin_server_ts"), int)


class _RoomStateResponse(nio.RoomGetStateResponse):
    """Room state checked field by field instead of by nio's JSON schema validation.

    nio validates every event with jsonschema on the event loop, hundreds of milliseconds per read of a room with
    thousands of state events. A body that is not a list answers with nio's error response, and an entry without the
    required fields is dropped.
    """

    @classmethod
    def from_dict(cls, parsed_dict: Any, room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:  # noqa: ANN401
        """Keep the well-formed state events of one room-state body."""
        if not isinstance(parsed_dict, list):
            return cls.create_error(parsed_dict, room_id)
        return cls([event for event in parsed_dict if _is_state_event(event)], room_id)


class MindRoomAsyncClient(nio.AsyncClient):
    """Matrix client for MindRoom-specific encrypted event behavior."""

    _process_shutdown_transport_fenced = False
    _transport_failure_logged = False

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
        # nio retries connection errors while logging only "Timed out", so name the real cause here.
        import time as _time  # noqa: PLC0415 - temporary timing log

        _t = _time.monotonic()
        try:
            response = await super().send(*args, **kwargs)
        except (aiohttp.ClientConnectionError, TimeoutError) as exc:
            self._log_transport_failure(exc)
            raise
        _path = str(args[1]) if len(args) > 1 else ""
        if "/messages" in _path or "simplified_msc3575/sync" in _path or "/sync" in _path.split("?")[0][-6:]:
            _kind = "messages" if "/messages" in _path else "sync"
            logger.warning("rfs_http", user=self.user_id, kind=_kind, s=round(_time.monotonic() - _t, 3))
        self._transport_failure_logged = False
        return response

    @logged_in_async
    async def room_get_state(self, room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        """Fetch a room's full state through ``_RoomStateResponse``."""
        method, path = nio.Api.room_get_state(self.access_token, room_id)
        return await self._send(_RoomStateResponse, method, path, response_data=(room_id,))

    def _log_transport_failure(self, error: BaseException) -> None:
        """Warn once per outage with the real cause; repeats stay at debug until the homeserver answers."""
        log = logger.debug if self._transport_failure_logged else logger.warning
        self._transport_failure_logged = True
        log(
            "matrix_request_transport_failed",
            homeserver=self.homeserver,
            error_type=type(error).__name__,
            error=failure_text(error),
        )

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
    *,
    shared: bool = True,
) -> ssl_module.SSLContext | None:
    """Return the configured Matrix SSL context when HTTPS requires one.

    MATRIX_SSL_VERIFY=false applies only to the configured MATRIX_HOMESERVER, so any other homeserver,
    such as one the desktop bridge signs in to, keeps certificate verification.
    A verifying context is shared unless ``shared=False``, so a client that changes the context must pass that.
    """
    if homeserver.startswith("https://"):
        configured_homeserver = homeserver.rstrip("/") == runtime_matrix_homeserver(runtime_paths).rstrip("/")
        if configured_homeserver and not runtime_matrix_ssl_verify(runtime_paths=runtime_paths):
            ssl_context = ssl_module.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl_module.CERT_NONE
        else:
            ssl_context = verifying_ssl_context(runtime_paths.process_env, shared=shared)
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
) -> nio.AsyncClientConfig:
    """Return nio config, copying plain headers while preserving request-time mappings."""
    custom_headers = dict(http_headers) if isinstance(http_headers, dict) else http_headers
    return nio.AsyncClientConfig(
        store_sync_tokens=True,
        custom_headers=cast("dict[str, str] | None", custom_headers),
        # Agents trust devices on first use and never verify interactively;
        # accept a peer device's re-registered olm identity (trust reset)
        # instead of keeping stale keys that silently break E2EE and calls.
        replace_rotated_device_keys=True,
        # nio retries a failed request forever by default. Ten retries of a connection failure or timeout
        # span about 100 s of nio's doubling backoff, outlasting a homeserver restart of about a minute; a 429
        # is retried twenty times, each after the wait the server asks for. Sync uses its own bounded transport.
        max_timeouts=10,
        max_limit_exceeded=20,
    )


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


@asynccontextmanager
async def matrix_client(
    homeserver: str,
    runtime_paths: RuntimePaths,
    user_id: str,
) -> AsyncGenerator[nio.AsyncClient]:
    """Open an HTTP-only Matrix client for one account and close it afterwards."""
    client = create_matrix_http_client(homeserver, runtime_paths, user_id)
    try:
        yield client
    finally:
        await client.close()


async def login_flows(
    homeserver: str,
    runtime_paths: RuntimePaths,
    *,
    http_headers: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return login methods advertised by one Matrix homeserver."""
    client = create_matrix_http_client(homeserver, runtime_paths, "", http_headers=http_headers)
    try:
        response = await client.login_info()
    finally:
        await client.close()
    if isinstance(response, nio.LoginInfoResponse):
        return tuple(response.flows)
    msg = f"Failed to query Matrix login methods: {response}"
    raise matrix_startup_error(msg, response=response)


__all__ = [
    "MindRoomAsyncClient",
    "PermanentMatrixStartupError",
    "create_matrix_http_client",
    "login_flows",
    "matrix_client",
    "matrix_client_config",
    "matrix_startup_error",
    "maybe_ssl_context",
    "olm_store_dir",
    "olm_store_exists",
    "require_runtime_paths_arg",
]
