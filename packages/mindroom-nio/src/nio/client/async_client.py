# Copyright © 2018, 2019 Damir Jelić <poljar@termina.org.uk>
# Copyright © 2020-2021 Famedly GmbH
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import warnings
from asyncio import Event as AsyncioEvent
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Sequence,
)
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial, wraps
from json.decoder import JSONDecodeError
from pathlib import Path
from ssl import SSLContext
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from uuid import UUID, uuid4

import aiofiles
import aiofiles.os
from aiofiles.threadpool.binary import AsyncBufferedReader
from aiofiles.threadpool.text import AsyncTextIOWrapper
from aiohttp import (
    ClientResponse,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
    TraceConfig,
)
from aiohttp.client_exceptions import ClientConnectionError
from aiohttp.connector import Connection
from aiohttp_socks import ProxyConnector

from ..api import (
    Api,
    EventFormat,
    MessageDirection,
    RelationshipType,
    ResizingMethod,
    RoomPreset,
    RoomVisibility,
    ThreadInclusion,
    _FilterT,
)
from ..crypto import (
    AsyncDataT,
    async_encrypt_attachment,
    async_generator_from_data,
)
from ..crypto.cross_signing import (
    CrossSigningIdentity,
    cross_signing_sidecar_path,
)
from ..event_builders import ToDeviceMessage
from ..events import (
    AccountDataEvent,
    BadEventType,
    EphemeralEvent,
    Event,
    MegolmEvent,
    PresenceEvent,
    ToDeviceEvent,
)
from ..exceptions import (
    InsufficientRecursionDepthError,
    LocalProtocolError,
    RemoteProtocolError,
    TransferCancelledError,
)
from ..monitors import TransferMonitor
from ..responses import (
    ChangePasswordError,
    ChangePasswordResponse,
    ContentRepositoryConfigError,
    ContentRepositoryConfigResponse,
    DeleteDevicesAuthResponse,
    DeleteDevicesError,
    DeleteDevicesResponse,
    DevicesError,
    DevicesResponse,
    DirectRoomsErrorResponse,
    DirectRoomsResponse,
    DiscoveryInfoError,
    DiscoveryInfoResponse,
    DiskDownloadResponse,
    DownloadError,
    ErrorResponse,
    FileResponse,
    GetOpenIDTokenError,
    GetOpenIDTokenResponse,
    JoinedMembersError,
    JoinedMembersResponse,
    JoinedRoomsError,
    JoinedRoomsResponse,
    JoinError,
    JoinResponse,
    KeysClaimError,
    KeysClaimResponse,
    KeysQueryError,
    KeysQueryResponse,
    KeysUploadError,
    KeysUploadResponse,
    LoginError,
    LoginInfoError,
    LoginInfoResponse,
    LoginResponse,
    LogoutError,
    LogoutResponse,
    MemoryDownloadResponse,
    PresenceGetError,
    PresenceGetResponse,
    PresenceSetError,
    PresenceSetResponse,
    ProfileGetAvatarError,
    ProfileGetAvatarResponse,
    ProfileGetDisplayNameError,
    ProfileGetDisplayNameResponse,
    ProfileGetError,
    ProfileGetResponse,
    ProfileSetAvatarError,
    ProfileSetAvatarResponse,
    ProfileSetDisplayNameError,
    ProfileSetDisplayNameResponse,
    RegisterErrorResponse,
    RegisterInteractiveError,
    RegisterInteractiveResponse,
    RegisterResponse,
    Response,
    RoomContextError,
    RoomContextResponse,
    RoomCreateError,
    RoomCreateResponse,
    RoomEventRelationsResponse,
    RoomForgetError,
    RoomForgetResponse,
    RoomGetEventError,
    RoomGetEventResponse,
    RoomGetStateError,
    RoomGetStateEventError,
    RoomGetStateEventResponse,
    RoomGetStateResponse,
    RoomGetVisibilityError,
    RoomGetVisibilityResponse,
    RoomInviteError,
    RoomInviteResponse,
    RoomKeyRequestError,
    RoomKeyRequestResponse,
    RoomKickError,
    RoomKickResponse,
    RoomLeaveError,
    RoomLeaveResponse,
    RoomMessagesError,
    RoomMessagesResponse,
    RoomPutAliasError,
    RoomPutAliasResponse,
    RoomPutStateError,
    RoomPutStateResponse,
    RoomRedactError,
    RoomRedactResponse,
    RoomResolveAliasError,
    RoomResolveAliasResponse,
    RoomSendError,
    RoomSendResponse,
    RoomThreadsResponse,
    RoomTypingError,
    RoomTypingResponse,
    ShareGroupSessionError,
    ShareGroupSessionResponse,
    SlidingSyncError,
    SlidingSyncResponse,
    SyncError,
    SyncResponse,
    ThumbnailError,
    ThumbnailResponse,
    ToDeviceError,
    ToDeviceResponse,
    UploadError,
    UploadFilterError,
    UploadFilterResponse,
    UploadResponse,
    WhoamiError,
    WhoamiResponse,
)
from ..rooms import MatrixRoom
from .base_client import (
    Client,
    ClientCallback,
    ClientConfig,
    _SyncItem,
    logged_in_async,
    store_loaded,
)

if TYPE_CHECKING:
    from ..store import MatrixStore

_ShareGroupSessionT = ShareGroupSessionError | ShareGroupSessionResponse

_ProfileGetDisplayNameT = ProfileGetDisplayNameResponse | ProfileGetDisplayNameError
_ProfileSetDisplayNameT = ProfileSetDisplayNameResponse | ProfileSetDisplayNameError
_ChangePasswordT = ChangePasswordResponse | ChangePasswordError
_RoomMembershipResponseT = TypeVar(
    "_RoomMembershipResponseT",
    RoomLeaveResponse | RoomLeaveError,
    RoomForgetResponse | RoomForgetError,
)

DataProvider = Callable[[int, int], AsyncDataT]
_SendDataProvider = Callable[[int, int], Awaitable[AsyncDataT]]


class _ResponseFactory(Protocol):
    @classmethod
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        *args: Any,
    ) -> Response: ...


SynchronousFile = (
    io.TextIOBase,
    io.BufferedReader,
    io.BufferedRandom,
    io.BytesIO,
    io.FileIO,
)
SynchronousFileType = (
    io.TextIOBase | io.BufferedReader | io.BufferedRandom | io.BytesIO | io.FileIO
)
AsyncFile = (AsyncBufferedReader, AsyncTextIOWrapper)
AsyncFileType = AsyncBufferedReader | AsyncTextIOWrapper

logger = logging.getLogger(__name__)


@dataclass
class _CallbackScope:
    active: bool = True


async def _run_to_completion[RunToCompletionT](
    operation: Coroutine[Any, Any, RunToCompletionT],
) -> RunToCompletionT:
    """Drain owned work through repeated cancellation before propagating it."""
    task = asyncio.create_task(operation)
    cancellations = 0
    deferred_error: BaseException | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:  # noqa: PERF203
            cancellations += 1
        except BaseException as error:
            if deferred_error is None:
                deferred_error = error
    try:
        result = task.result()
    except BaseException:
        caller = asyncio.current_task()
        if caller is not None and sys.version_info >= (3, 11):
            for _ in range(cancellations):
                caller.uncancel()
        raise
    if deferred_error is not None:
        caller = asyncio.current_task()
        if caller is not None and sys.version_info >= (3, 11):
            for _ in range(cancellations):
                caller.uncancel()
        raise deferred_error
    if cancellations:
        raise asyncio.CancelledError
    return result


async def _cancel_and_drain_tasks(tasks: Sequence[asyncio.Future[Any]]) -> None:
    """Cancel owned child tasks and wait until every child is quiescent."""
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def on_request_chunk_sent(session, context, params):
    """TraceConfig callback to run when a chunk is sent for client uploads."""

    context_obj = context.trace_request_ctx

    if isinstance(context_obj, TransferMonitor):
        context_obj.transferred += len(params.chunk)


async def connect_wrapper(self, *args, **kwargs) -> Connection:
    connection = await type(self).connect(self, *args, **kwargs)
    connection.transport.set_write_buffer_limits(16 * 1024)
    return connection


def client_session(func):
    """Ensure that the Async client has a valid client session."""

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        self._assert_not_disposed()
        if not self.client_session:
            trace = TraceConfig()
            trace.on_request_chunk_sent.append(on_request_chunk_sent)

            connector = ProxyConnector.from_url(self.proxy) if self.proxy else None
            self.client_session = ClientSession(
                timeout=ClientTimeout(total=self.config.request_timeout),
                trace_configs=[trace],
                connector=connector,
            )

            self.client_session.connector.connect = partial(
                connect_wrapper,
                self.client_session.connector,
            )

        return await func(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class AsyncClientConfig(ClientConfig):
    """Async nio client configuration.

    Attributes:
        max_limit_exceeded (int, optional): How many 429 (Too many requests)
            errors can a request encounter before giving up and returning
            an ErrorResponse.
            Default is None for unlimited.

        max_timeouts (int, optional): How many timeout connection errors can
            a request encounter before giving up and raising the error:
            a ClientConnectionError, TimeoutError, or asyncio.TimeoutError.
            Default is None for unlimited.

        backoff_factor (float): A backoff factor to apply between retries
            for timeouts, starting from the second try.
            nio will sleep for `backoff_factor * (2 ** (total_retries - 1))`
            seconds.
            For example, with the default backoff_factor of 0.1,
            nio will sleep for 0.0, 0.2, 0.4, ... seconds between retries.

        max_timeout_retry_wait_time (float): The maximum time in seconds to
            wait between retries for timeouts, by default 60.

        request_timeout (float): How many seconds a request has to finish,
            before it is retried or raise an `asycio.TimeoutError` depending
            on `max_timeouts`.
            Defaults to 60 seconds, and can be disabled with `0`.
            `AsyncClient.sync()` overrides this option with its
            `timeout` argument.
            The `download()`, `thumbnail()` and `upload()` methods ignore
            this option and use `0`.

        io_chunk_size (int): The size (in bytes) of the chunks to read from the IO
            streams when saving files to disk.
            Defaults to 64 KiB.

    """

    max_limit_exceeded: int | None = None
    max_timeouts: int | None = None
    backoff_factor: float = 0.1
    max_timeout_retry_wait_time: float = 60
    request_timeout: float = 60
    io_chunk_size: int = 64 * 1024


class AsyncClient(Client):
    """An async IO matrix client.

    Args:
        homeserver (str): The URL of the homeserver which we want to connect
            to.
        user (str, optional): The user which will be used when we log in to the
            homeserver.
        device_id (str, optional): An unique identifier that distinguishes
            this client instance. If not set the server will provide one after
            log in.
        store_path (str, optional): The directory that should be used for state
            storage.
        config (AsyncClientConfig, optional): Configuration for the client.
        ssl (bool/ssl.SSLContext, optional): SSL validation mode. None for
            default SSL check (ssl.create_default_context() is used), False
            for skip SSL certificate validation connection.
        proxy (str, optional): The proxy that should be used for the HTTP
            connection. Supports SOCKS4(a), SOCKS5, HTTP (tunneling) via an
            URL like e.g. 'socks5://user:password@127.0.0.1:1080'.

    Attributes:
        synced (Event): An asyncio event that is fired every time the client
            successfully syncs with the server. Note, this event will only be
            fired if the `sync_forever()` method is used.

    A simple example can be found bellow.

    Example:
            >>> client = AsyncClient("https://example.org", "example")
            >>> login_response = loop.run_until_complete(
            >>>     client.login("hunter1")
            >>> )
            >>> asyncio.run(client.sync_forever(30000))

    This example assumes a full sync on every run. If a sync token is provided
    for the `since` parameter of the `sync_forever` method `full_state` should
    be set to `True` as well.

    Example:
            >>> asyncio.run(
            >>>     client.sync_forever(30000, since="token123",
            >>>                         full_state=True)
            >>> )

    The client can also be configured to store and restore the sync token
    automatically. The `full_state` argument should be set to `True` in that
    case as well.

    Example:
            >>> config = ClientConfig(store_sync_tokens=True)
            >>> client = AsyncClient("https://example.org", "example",
            >>>                      store_path="/home/example",
            >>>                      config=config)
            >>> login_response = loop.run_until_complete(
            >>>     client.login("hunter1")
            >>> )
            >>> asyncio.run(client.sync_forever(30000, full_state=True))

    """

    def __init__(
        self,
        homeserver: str,
        user: str = "",
        device_id: str | None = "",
        store_path: str | None = "",
        config: AsyncClientConfig | None = None,
        ssl: SSLContext | bool | None = None,
        proxy: str | None = None,
    ):
        self.homeserver = homeserver
        self.client_session: ClientSession | None = None

        self.ssl = ssl
        self.proxy = proxy

        self._presence: str | None = None

        self.synced = AsyncioEvent()
        self.response_callbacks: list[ClientCallback] = []
        self._encrypted_to_device_lock = asyncio.Lock()
        self._pending_encrypted_to_device: tuple[str, str, ToDeviceMessage] | None = (
            None
        )
        self._event_callback_scope: ContextVar[_CallbackScope | None] = ContextVar(
            f"nio_event_callback_scope_{id(self)}", default=None
        )
        self._response_callback_scope: ContextVar[_CallbackScope | None] = ContextVar(
            f"nio_response_callback_scope_{id(self)}", default=None
        )

        self.sharing_session: dict[str, AsyncioEvent] = {}

        is_config = isinstance(config, ClientConfig)
        is_async_config = isinstance(config, AsyncClientConfig)

        if is_config and not is_async_config:
            warnings.warn(
                "Pass an AsyncClientConfig instead of ClientConfig.",
                DeprecationWarning,
            )
            config = AsyncClientConfig(**config.__dict__)

        self.config: AsyncClientConfig = config or AsyncClientConfig()

        self._stop_sync_forever = False

        super().__init__(user, device_id, store_path, self.config)

    def add_response_callback(
        self,
        func: Callable[..., Awaitable[None] | None],
        cb_filter: tuple[type, ...] | type | None = None,
    ) -> None:
        """Add a callback that will be called if a response is received.

        Args:
            func (Callable): The callback that will be called with the
                response as the argument.
            cb_filter (Type, optional): A type or a tuple of types for which
                the callback should be called.

        Example:

            >>> # A callback that will be called every time our `sync_forever`
            >>> # method successfully syncs with the server.
            >>> async def sync_cb(response):
            ...    print(f"We synced, token: {response.next_batch}")
            ...
            >>> client.add_response_callback(sync_cb, SyncResponse)
            >>> await client.sync_forever(30000)

        """
        cb = ClientCallback(func, cb_filter)
        self.response_callbacks.append(cb)

    async def parse_body(self, transport_response: ClientResponse) -> dict[Any, Any]:
        """Parse the body of the response.

        Low-level function which is normally only used by other methods of
        this class.

        Args:
            transport_response(ClientResponse): The transport response that
                contains the body of the response.

        Returns a dictionary representing the response.
        """
        try:
            return await transport_response.json()
        except (JSONDecodeError, ContentTypeError):
            try:
                # matrix.org return an incorrect content-type for .well-known
                # API requests, which leads to .text() working but not .json()
                return json.loads(await transport_response.text())
            except (JSONDecodeError, ContentTypeError):
                pass

            return {}

    async def create_matrix_response(
        self,
        response_class: type,
        transport_response: ClientResponse,
        data: tuple[Any, ...] | None = None,
        save_to: os.PathLike | None = None,
    ) -> Response:
        """Transform a transport response into a nio matrix response.

        Low-level function which is normally only used by other methods of
        this class.

        Args:
            response_class (Type): The class that the requests belongs to.
            transport_response (ClientResponse): The underlying transport
                response that contains our response body.
            data (Tuple, optional): Extra data that is required to instantiate
                the response class.
            save_to (PathLike, optional): If set, the ``FileResponse`` body will be saved to this file.
        Returns a subclass of `Response` depending on the type of the
        response_class argument.
        """
        factory_args = data or ()

        content_type = transport_response.content_type
        is_json = content_type == "application/json"

        name = None
        if transport_response.content_disposition:
            name = transport_response.content_disposition.filename

        if issubclass(response_class, FileResponse) and is_json:
            if transport_response.status in range(200, 300):
                file_data: (
                    bytes | os.PathLike | dict[Any, Any]
                ) = await transport_response.read()
                # the data was returned fine, so the raw bytes are passed to prevent parsing as an error.
            else:
                file_data = await self.parse_body(transport_response)
                # there was an error in the response, so the data must be a dictionary to parse as an error
            resp = response_class.from_data(file_data, content_type, name)

        elif issubclass(response_class, FileResponse):
            if not save_to:
                file_body: bytes | os.PathLike = await transport_response.read()
            else:
                destination = Path(save_to)
                if await aiofiles.os.path.isdir(destination):
                    if name is None:
                        raise ValueError(
                            "A response filename is required when saving to a directory"
                        )
                    destination /= name

                async with aiofiles.open(destination, "wb") as f:
                    async for chunk in transport_response.content.iter_chunked(
                        self.config.io_chunk_size
                    ):
                        await f.write(chunk)
                file_body = destination
            resp = response_class.from_data(file_body, content_type, name)
        elif (
            issubclass(response_class, RoomGetStateEventResponse)
            and transport_response.status == 404
        ):
            parsed_dict = await self.parse_body(transport_response)
            resp = response_class.create_error(parsed_dict, factory_args[-1])

        elif (
            transport_response.status == 401 and response_class == DeleteDevicesResponse
        ):
            parsed_dict = await self.parse_body(transport_response)
            resp = DeleteDevicesAuthResponse.from_dict(parsed_dict)

        else:
            parsed_dict = await self.parse_body(transport_response)
            response_factory = cast(type[_ResponseFactory], response_class)
            resp = response_factory.from_dict(parsed_dict, *factory_args)

        resp.transport_response = transport_response
        return resp

    def _iter_to_device(
        self, response: SyncResponse | SlidingSyncResponse
    ) -> Iterator[_SyncItem]:
        # Async callbacks historically see decrypted response entries immediately.
        return self._iter_decrypted_to_device(response, replace_immediately=True)

    async def _on_to_device(
        self,
        event: ToDeviceEvent | BadEventType,
    ) -> None:
        await self._run_event_callbacks(self.to_device_callbacks, event)

    async def _on_invited_rooms(self, event: Event, room: MatrixRoom):
        await self._run_event_callbacks(self.event_callbacks, event, room)

    async def _run_event_callbacks(
        self,
        callbacks: Iterable[ClientCallback],
        event: object,
        room: MatrixRoom | None = None,
        *callback_args: object,
    ) -> None:
        scope = _CallbackScope()
        token = self._event_callback_scope.set(scope)
        try:
            for callback in callbacks:
                await callback.async_execute(event, room, *callback_args)
        finally:
            scope.active = False
            self._event_callback_scope.reset(token)

    async def _on_event(self, event: Event | BadEventType, room: MatrixRoom):
        await self._run_event_callbacks(self.event_callbacks, event, room)

    async def _on_ephemeral(self, event: EphemeralEvent, room: MatrixRoom):
        await self._run_event_callbacks(self.ephemeral_callbacks, event, room)

    async def _on_room_account_data(
        self, event: AccountDataEvent | BadEventType, room: MatrixRoom
    ):
        await self._run_event_callbacks(
            self.room_account_data_callbacks,
            event,
            room,
        )

    async def _on_presence(self, event: PresenceEvent):
        await self._run_event_callbacks(self.presence_callbacks, event)

    async def _on_global_account_data(self, event: AccountDataEvent):
        await self._run_event_callbacks(self.global_account_data_callbacks, event)

    async def _on_expired_verifications(self, event: ToDeviceEvent):
        await self._run_event_callbacks(self.to_device_callbacks, event)

    async def _on_response(self, response: Response | ErrorResponse):
        for cb in self.response_callbacks:
            scope = _CallbackScope()
            token = self._response_callback_scope.set(scope)
            try:
                await cb.async_execute(response)
            finally:
                scope.active = False
                self._response_callback_scope.reset(token)

    async def _handle_sync(self, response: SyncResponse) -> None:
        self._claim_sync_transport("classic")

        if self.next_batch == response.next_batch:
            return

        self.next_batch = response.next_batch
        if self.config.store_sync_tokens and self.store:
            self.store.save_sync_token(response.next_batch)

        for item in self._iter_sync(response):
            if item.route is not None:
                await self._dispatch_sync_item(item)

    async def _handle_sliding_sync(self, response: SlidingSyncResponse) -> None:
        for item in self._iter_ordinary_sliding_sync(response):
            if item.route is not None:
                await self._dispatch_sync_item(item)

    async def _save_encrypted_rooms(self, rooms: Iterable[str]) -> None:
        store = self.store
        room_ids = tuple(rooms)
        if store is None or not room_ids:
            return
        if (
            not store.supports_threaded_encrypted_room_writes
            or store.database.in_transaction()
        ):
            store.save_encrypted_rooms(room_ids)
            return
        await _run_to_completion(
            asyncio.to_thread(self._save_encrypted_rooms_in_thread, store, room_ids)
        )

    @staticmethod
    def _save_encrypted_rooms_in_thread(
        store: MatrixStore, rooms: tuple[str, ...]
    ) -> None:
        with store.database.connection_context():
            store.save_encrypted_rooms(rooms)

    async def receive_response(self, response: Response) -> None:
        """Receive a Matrix Response and change the client state accordingly.

        Automatically called for all "high-level" methods of this API (each
        function documents calling it).

        Some responses will get edited for the callers convenience e.g. sync
        responses that contain encrypted messages. The encrypted messages will
        be replaced by decrypted ones if decryption is possible.

        Args:
            response (Response): the response that we wish the client to handle
        """
        if not isinstance(response, Response):
            raise ValueError("Invalid response received")

        if isinstance(response, SyncResponse):
            await self._handle_sync(response)
        elif isinstance(response, SlidingSyncResponse):
            await self._handle_sliding_sync(response)
        else:
            super().receive_response(response)

    async def get_timeout_retry_wait_time(self, got_timeouts: int) -> float:
        if got_timeouts < 2:
            return 0.0

        return min(
            self.config.backoff_factor * (2 ** (min(got_timeouts, 1000) - 1)),
            self.config.max_timeout_retry_wait_time,
        )

    async def _send(
        self,
        response_class: type,
        method: str,
        path: str,
        data: None | str | AsyncDataT = None,
        response_data: tuple[Any, ...] | None = None,
        content_type: str | None = None,
        trace_context: Any | None = None,
        data_provider: _SendDataProvider | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
        content_length: int | None = None,
        save_to: os.PathLike | None = None,
    ):
        headers = (
            {"Content-Type": content_type}
            if content_type
            else {"Content-Type": "application/json"}
        )

        if self.access_token:
            # add authorization header
            headers["Authorization"] = f"Bearer {self.access_token}"
            # remove access_token from query params
            url = list(urlparse(path))
            qs = parse_qs(url[4])
            qs.pop("access_token", None)
            url[4] = urlencode(qs, doseq=True)
            path = urlunparse(url)

        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        got_429 = 0
        max_429 = self.config.max_limit_exceeded

        got_timeouts = 0
        max_timeouts = self.config.max_timeouts

        while True:
            if data_provider:
                data = await data_provider(got_429, got_timeouts)

            try:
                transport_resp = await self.send(
                    method,
                    path,
                    data,
                    headers,
                    trace_context,
                    timeout,
                )

                resp = await self.create_matrix_response(
                    response_class=response_class,
                    transport_response=transport_resp,
                    data=response_data,
                    save_to=save_to,
                )

                if transport_resp.status == 429 or (
                    isinstance(resp, ErrorResponse)
                    and resp.status_code in ("M_LIMIT_EXCEEDED", 429)
                ):
                    got_429 += 1

                    if max_429 is not None and got_429 > max_429:
                        break

                    await self.run_response_callbacks([resp])

                    retry_after_ms = getattr(resp, "retry_after_ms", 0) or 5000
                    logger.warning(
                        "Got 429 response (ratelimited), sleeping for %dms",
                        retry_after_ms,
                    )
                    await asyncio.sleep(retry_after_ms / 1000)
                else:
                    break

            except (ClientConnectionError, TimeoutError):
                got_timeouts += 1

                if max_timeouts is not None and got_timeouts > max_timeouts:
                    raise

                wait = await self.get_timeout_retry_wait_time(got_timeouts)
                logger.warning("Timed out, sleeping for %ds", wait)
                await asyncio.sleep(wait)

        await self.receive_response(resp)
        return resp

    @client_session
    async def send(
        self,
        method: str,
        path: str,
        data: None | str | AsyncDataT = None,
        headers: dict[str, str] | None = None,
        trace_context: Any | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ClientResponse:
        """Send a request to the homeserver.

        This function does not call receive_response().

        Args:
            method (str): The request method that should be used. One of get,
                post, put, delete.
            path (str): The URL path of the request.
            data (str, optional): Data that will be posted with the request.
            headers (Dict[str,str] , optional): Additional request headers that
                should be used with the request. ``custom_headers`` from the
                client config are added afterward and override matching keys.
            trace_context (Any, optional): An object to use for the
                ClientSession TraceConfig context
            timeout (int, optional): How many seconds the request has before
                raising `asyncio.TimeoutError`.
                Overrides `AsyncClient.config.request_timeout` if not `None`.
        """
        assert self.client_session

        request_headers = dict(headers) if headers is not None else {}
        if self.config.custom_headers is not None:
            request_headers.update(self.config.custom_headers)

        request_timeout = ClientTimeout(
            total=self.config.request_timeout if timeout is None else timeout
        )
        request_ssl = self.ssl if self.ssl is not None else True

        return await self.client_session.request(
            method,
            self.homeserver + path,
            data=data,
            ssl=request_ssl,
            headers=request_headers,
            trace_request_ctx=trace_context,
            timeout=request_timeout,
        )

    async def register_interactive(
        self,
        username: str,
        password: str,
        auth_dict: dict[str, Any],
        device_name: str = "",
    ) -> RegisterInteractiveResponse | RegisterInteractiveError:
        """Makes a request to the register endpoint using the provided
        auth dictionary. This is allows for interactive registration flows
        from the homeserver.

        Calls receive_response() to update the client state if necessary.

        Args:
            username (str): Username to register the new user as.
            password (str): New password for the user.
            auth_dict (dict): The auth dictionary.
            device_name (str): A display name to assign to a newly-created
                device. Ignored if the logged in device corresponds to a
                known device.

        Returns a 'RegisterInteractiveResponse' if successful.
        """
        method, path, data = Api.register(
            user=username,
            password=password,
            device_name=device_name,
            device_id=self.device_id,
            auth_dict=auth_dict,
        )

        return await self._send(RegisterInteractiveResponse, method, path, data)

    async def register_with_token(
        self,
        username: str,
        password: str,
        registration_token: str,
        device_name: str = "",
    ) -> RegisterResponse | RegisterErrorResponse:
        """Registers a user using a registration token.
        See https://spec.matrix.org/latest/client-server-api/#token-authenticated-registration

        Returns either a `RegisterResponse` if the request was successful or
        a `RegisterErrorResponse` if there was an error with the request.

        """
        # must first register without token to get a session token
        resp = await self.register_interactive(
            username,
            password,
            auth_dict={"initial_device_display_name": self.device_id or "matrix-nio"},
        )
        if isinstance(resp, RegisterInteractiveError):
            return RegisterErrorResponse(
                resp.message, resp.status_code, resp.retry_after_ms, resp.soft_logout
            )

        # use session token to register with token
        session_token = resp.session
        resp = await self.register_interactive(
            username,
            password,
            auth_dict={
                "type": "m.login.registration_token",
                "token": registration_token,
                "session": session_token,
            },
        )
        if isinstance(resp, RegisterInteractiveError):
            return RegisterErrorResponse(
                resp.message, resp.status_code, resp.retry_after_ms, resp.soft_logout
            )

        # finally call register with dummy auth with original session token
        # to complete registration and acquire access token
        return await self.register(
            username, password, device_name=device_name, session_token=session_token
        )

    async def register(
        self,
        username: str,
        password: str,
        device_name: str = "",
        session_token: str | None = None,
    ) -> RegisterResponse | RegisterErrorResponse:
        """Register with homeserver.

        Calls receive_response() to update the client state if necessary.

        Args:
            username (str): Username to register the new user as.
            password (str): New password for the user.
            device_name (str, optional): A display name to assign to a
                newly-created device. Ignored if the logged in device
                corresponds to a known device.
            session_token (str, optional): The session token the server
                provided during interactive registration. If not provided,
                the session token is not added to the request's auth dict.

        Returns a 'RegisterResponse' if successful.
        """
        auth_dict = {"type": "m.login.dummy"}
        if session_token is not None:
            auth_dict["session"] = session_token

        method, path, data = Api.register(
            user=username,
            password=password,
            device_name=device_name,
            device_id=self.device_id,
            auth_dict=auth_dict,
        )

        return await self._send(RegisterResponse, method, path, data)

    async def discovery_info(
        self,
    ) -> DiscoveryInfoResponse | DiscoveryInfoError:
        """Get discovery information about current `AsyncClient.homeserver`.

        Returns either a `DiscoveryInfoResponse` if the request was successful
        or a `DiscoveryInfoError` if there was an error with the request.

        Some homeservers do not redirect requests to their main domain and
        instead require clients to use a specific URL for communication.

        If the domain specified by the `AsyncClient.homeserver` URL
        implements the
        [.well-known](https://matrix.org/docs/spec/client_server/latest#id178),
        discovery mechanism, this method can be used to retrieve the
        actual homeserver URL from it.

        Example:
            >>> client = AsyncClient(homeserver="https://example.org")
            >>> response = await client.discovery_info()
            >>> if isinstance(response, DiscoveryInfoResponse):
            >>>     client.homeserver = response.homeserver_url
        """
        method, path = Api.discovery_info()
        return await self._send(DiscoveryInfoResponse, method, path)

    async def login_info(self) -> LoginInfoResponse | LoginInfoError:
        """Get the available login methods from the server

        Returns either a `LoginInfoResponse` if the request was successful or
        a `LoginInfoError` if there was an error with the request.

        """
        method, path = Api.login_info()

        return await self._send(LoginInfoResponse, method, path)

    async def login(
        self,
        password: str | None = None,
        device_name: str | None = "",
        token: str | None = None,
    ) -> LoginResponse | LoginError:
        """Login to the homeserver.

        Calls receive_response() to update the client state if necessary.

        Args:
            password (str, optional): The user's password.
            device_name (str): A display name to assign to a newly-created
                device. Ignored if the logged in device corresponds to a
                known device.
            token (str, optional): A login token, for example provided by a
                single sign-on service.

        Either a password or a token needs to be provided.

        Returns either a `LoginResponse` if the request was successful or
        a `LoginError` if there was an error with the request.
        """

        if password is None and token is None:
            raise ValueError("Either a password or a token needs to be provided")

        method, path, data = Api.login(
            self.user,
            password=password,
            device_name=device_name,
            device_id=self.device_id,
            token=token,
        )

        return await self._send(LoginResponse, method, path, data)

    @logged_in_async
    async def logout(self, all_devices: bool = False) -> LogoutResponse | LogoutError:
        """Logout from the homeserver.

        Calls receive_response() to update the client state if necessary.

        Returns either 'LogoutResponse' if the request was successful or
        a `Logouterror` if there was an error with the request.
        """
        method, path = Api.logout(self.access_token, all_devices)

        return await self._send(LogoutResponse, method, path)

    @logged_in_async
    async def change_password(
        self,
        auth: dict[str, Any],
        new_password: str,
    ) -> _ChangePasswordT:
        """Change the account password.

        Calls receive_response() to update the client state if necessary.

        Example ``auth`` dict::

            auth= {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "cheeky_monkey"},
                "password": "ilovebananas"
            }

        :param auth: The authentication dictionary containing the elements for user
            identification and authentication. See the example or
            https://matrix.org/docs/spec/client_server/r0.6.0#authentication-types.
        :param new_password: The new password for the user.

        Returns either a `ChangePasswordResponse` if the request was successful or
        a `ChangePasswordError` if there was an error with the request.
        """
        method, path, data = Api.change_password(self.access_token, auth, new_password)

        return await self._send(ChangePasswordResponse, method, path, data)

    @logged_in_async
    async def sync(
        self,
        timeout: int | None = 0,  # noqa: ASYNC109
        sync_filter: _FilterT | None = None,
        since: str | None = None,
        full_state: bool | None = None,
        set_presence: str | None = None,
    ) -> SyncResponse | SyncError:
        """Synchronise the client's state with the latest state on the server.

        In general you should use sync_forever() which handles additional
        tasks automatically (like sending encryption keys among others).

        Calls receive_response() to update the client state if necessary.

        Args:
            timeout(int, optional): The maximum time that the server should
                wait for new events before it should return the request
                anyways, in milliseconds.
                If ``0``, no timeout is applied.
                If ``None``, use ``AsyncClient.config.request_timeout``.
                If a timeout is applied and the server fails to return after
                15 seconds of expected timeout,
                the client will timeout by itself.
            sync_filter (Union[None, str, Dict[Any, Any]):
                A filter ID that can be obtained from
                ``AsyncClient.upload_filter()`` (preferred),
                or filter dict that should be used for this sync request.
            full_state (bool, optional): Controls whether to include the full
                state for all rooms the user is a member of. If this is set to
                true, then all state events will be returned, even if since is
                non-empty. The timeline will still be limited by the since
                parameter.
            since (str, optional): A token specifying a point in time where to
                continue the sync from. Defaults to the last sync token we
                received from the server using this API call.
            set_presence (str, optional): The presence state.
                One of: ["online", "offline", "unavailable"]

        Returns either a `SyncResponse` if the request was successful or
        a `SyncError` if there was an error with the request.
        """
        self._claim_sync_transport("classic")

        sync_token = since or self.next_batch
        presence = set_presence or self._presence
        method, path = Api.sync(
            self.access_token,
            since=sync_token or self.loaded_sync_token,
            timeout=(
                int(self.config.request_timeout) * 1000
                if timeout is None
                else timeout or None
            ),
            filter=sync_filter,
            full_state=full_state,
            set_presence=presence,
        )

        self._claim_sync_poll()
        try:
            return await self._send(
                SyncResponse,
                method,
                path,
                timeout=(
                    0 if full_state else timeout / 1000 + 15 if timeout else timeout
                ),
            )
        finally:
            self._ordinary_sync_in_flight = False

    @logged_in_async
    async def sliding_sync(
        self,
        conn_id: str | None = None,
        pos: str | None = None,
        timeout: int | None = 0,  # noqa: ASYNC109
        set_presence: str | None = None,
        lists: dict[str, Any] | None = None,
        room_subscriptions: dict[str, Any] | None = None,
        extensions: dict[str, Any] | None = None,
        unstable: bool = True,
    ) -> SlidingSyncResponse | SlidingSyncError:
        """Fetch a Sliding window and run the normal event callbacks.

        The caller threads connection-scoped ``pos`` values. The client owns
        the separate to-device cursor. Only one ordinary poll can be active;
        a client using Sliding to-device data cannot also consume Classic sync.
        """
        request_extensions = self._sliding_request_extensions(extensions)
        method, path, data = Api.sliding_sync(
            self.access_token,
            conn_id=conn_id,
            pos=pos,
            timeout=(
                int(self.config.request_timeout * 1000) if timeout is None else timeout
            ),
            set_presence=set_presence or self._presence,
            lists=lists,
            room_subscriptions=room_subscriptions,
            extensions=request_extensions,
            unstable=unstable,
        )
        self._claim_sync_poll()
        try:
            return await self._send(
                SlidingSyncResponse,
                method,
                path,
                data,
                timeout=timeout / 1000 + 15 if timeout else timeout,
            )
        finally:
            self._ordinary_sync_in_flight = False

    @logged_in_async
    async def sliding_sync_forever(
        self,
        timeout: int = 30_000,  # noqa: ASYNC109
        conn_id: str = "nio",
        lists: dict[str, Any] | None = None,
        room_subscriptions: dict[str, Any] | None = None,
        extensions: dict[str, Any] | None = None,
        set_presence: str | None = None,
        unstable: bool = True,
        loop_sleep_time: int | None = None,
    ) -> None:
        """Poll Sliding sync, restart expired positions, and maintain crypto.

        Connection positions start fresh on each loop invocation. Room state,
        observed windows and the device cursor survive a connection reset.
        Error retries wait between requests with a bounded delay.
        """
        self._begin_ordinary_sync()
        pos = None
        failures = 0
        try:
            while not self._stop_sync_forever:
                result = await self.sliding_sync(
                    conn_id=conn_id,
                    pos=pos,
                    timeout=0 if pos is None else timeout,
                    set_presence=set_presence,
                    lists=lists,
                    room_subscriptions=room_subscriptions,
                    extensions=extensions,
                    unstable=unstable,
                )
                await self.run_response_callbacks([result])
                if isinstance(result, SlidingSyncError):
                    if result.status_code == "M_UNKNOWN_POS":
                        pos = None
                    failures += 1
                    if not self._stop_sync_forever:
                        delay = max(
                            1.0, await self.get_timeout_retry_wait_time(failures)
                        )
                        if result.retry_after_ms is not None:
                            delay = max(delay, result.retry_after_ms / 1000)
                        await asyncio.sleep(min(delay, 60))
                    continue
                failures = 0
                pos = result.pos
                await self.run_response_callbacks(await self.send_to_device_messages())
                if self.should_upload_keys:
                    await self.run_response_callbacks([await self.keys_upload()])
                if self.should_query_keys:
                    await self.run_response_callbacks([await self.keys_query()])
                if self.should_claim_keys:
                    await self.run_response_callbacks([
                        await self.keys_claim(self.get_users_for_key_claiming())
                    ])
                self.synced.set()
                self.synced.clear()
                if loop_sleep_time and not self._stop_sync_forever:
                    await asyncio.sleep(loop_sleep_time / 1000)
        finally:
            self._stop_sync_forever = False

    @logged_in_async
    async def send_to_device_messages(
        self,
    ) -> list[ToDeviceResponse | ToDeviceError]:
        """Send out outgoing to-device messages.

        Automatically called by sync_forever().
        """
        self._assert_not_disposed()
        if not self.outgoing_to_device_messages:
            return []

        tasks = []

        for message in self.outgoing_to_device_messages:
            task = asyncio.ensure_future(self.to_device(message))
            tasks.append(task)

        return await asyncio.gather(*tasks)

    async def run_response_callbacks(self, responses: list[Response | ErrorResponse]):
        """Run the configured response callbacks for the given responses.

        Low-level function which is normally only used by other methods of
        this class. Automatically called by sync_forever() and all functions
        calling receive_response().
        """
        for response in responses:
            await self._on_response(response)

    @logged_in_async
    async def sync_forever(
        self,
        timeout: int | None = None,  # noqa: ASYNC109
        sync_filter: _FilterT | None = None,
        since: str | None = None,
        full_state: bool | None = None,
        loop_sleep_time: int | None = None,
        first_sync_filter: _FilterT | None = None,
        set_presence: str | None = None,
    ):
        """Continuously sync with the configured homeserver.

        This method calls the sync method in a loop. To react to events event
        callbacks should be configured.

        The loop also makes sure to handle other required requests between
        syncs, including to_device messages and sending encryption keys if
        required. To react to the responses a response callback should be
        added.

        Args:
            timeout (int, optional): The maximum time that the server should
                wait for new events before it should return the request
                anyways, in milliseconds.
                If ``0``, no timeout is applied.
                If ``None``, ``AsyncClient.config.request_timeout`` is used.
                In any case, ``0`` is always used for the first sync.
                If a timeout is applied and the server fails to return after
                15 seconds of expected timeout,
                the client will timeout by itself.

            sync_filter (Union[None, str, Dict[Any, Any]):
                A filter ID that can be obtained from
                ``AsyncClient.upload_filter()`` (preferred),
                or filter dict that should be used for sync requests.

            full_state (bool, optional): Controls whether to include the full
                state for all rooms the user is a member of. If this is set to
                true, then all state events will be returned, even if since is
                non-empty. The timeline will still be limited by the since
                parameter. This argument remains active until the first
                successful sync response.

            since (str, optional): A token specifying a point in time where to
                continue the sync from. Defaults to the last sync token we
                received from the server using this API call. This argument
                remains active until the first successful sync response; later
                requests use the token from that response.

            loop_sleep_time (int, optional): The sleep time, if any, between
                successful sync loop iterations in milliseconds.

            first_sync_filter (Union[None, str, Dict[Any, Any]):
                A filter ID that can be obtained from
                ``AsyncClient.upload_filter()`` (preferred),
                or filter dict to use until the first successful sync response.
                If `None` (default), the `sync_filter` parameter's value
                is used.
                To have no filtering for the first sync regardless of
                `sync_filter`'s value, pass `{}`.

            set_presence (str, optional): The presence state.
                One of: ["online", "offline", "unavailable"]
        """
        self._begin_ordinary_sync()

        first_sync = True

        while not self._stop_sync_forever:
            try:
                initial_sync_complete = not first_sync
                use_filter = (
                    first_sync_filter
                    if first_sync and first_sync_filter is not None
                    else sync_filter
                )
                use_timeout = 0 if first_sync else timeout

                tasks = []

                # Make sure that if this is our first sync that the sync happens
                # before the other requests, this helps to ensure that after one
                # fired synced event the state is indeed fully synced.
                if first_sync:
                    presence = set_presence or self._presence
                    sync_response = await self.sync(
                        use_timeout, use_filter, since, full_state, presence
                    )
                    await self.run_response_callbacks([sync_response])
                    initial_sync_complete = isinstance(sync_response, SyncResponse)
                else:
                    presence = set_presence or self._presence
                    tasks = [
                        asyncio.ensure_future(coro)
                        for coro in (
                            self.sync(
                                use_timeout, use_filter, since, full_state, presence
                            ),
                            self.send_to_device_messages(),
                        )
                    ]

                if self.should_upload_keys:
                    tasks.append(asyncio.ensure_future(self.keys_upload()))

                if self.should_query_keys:
                    tasks.append(asyncio.ensure_future(self.keys_query()))

                if self.should_claim_keys:
                    tasks.append(
                        asyncio.ensure_future(
                            self.keys_claim(self.get_users_for_key_claiming()),
                        )
                    )

                for response in asyncio.as_completed(tasks):
                    await self.run_response_callbacks([await response])

                if initial_sync_complete:
                    first_sync = False
                    full_state = None
                    since = None

                    self.synced.set()
                    self.synced.clear()

                if loop_sleep_time:
                    await asyncio.sleep(loop_sleep_time / 1000)

            except BaseException:  # noqa: PERF203
                try:
                    await _run_to_completion(_cancel_and_drain_tasks(tasks))
                except asyncio.CancelledError:
                    pass
                self._stop_sync_forever = False
                raise
        self._stop_sync_forever = False

    @logged_in_async
    @store_loaded
    async def encrypted_to_device(
        self,
        message: ToDeviceMessage,
        *,
        recipient_ed25519: str,
        tx_id: str | None = None,
    ) -> ToDeviceResponse | ToDeviceError:
        """Encrypt and send a custom event to one exact pinned device.

        Every new operation queries fresh signed device keys and claims an Olm
        key when needed. A pin authorizes this send without changing global
        device trust. Invalid/unavailable identities raise LocalProtocolError.

        With DurableSync, encryption and retained HTTP work commit atomically;
        cancellation/restart retries the exact ciphertext. Ordinary clients
        retain pending ciphertext only in memory, without crash durability.
        Durable transport failures raise the durable transport exception;
        ordinary send failures return ToDeviceError or raise a transport error.

        An explicit tx_id identifies one pending operation: changed content
        under that ID is rejected. Use a fresh ID after successful completion;
        completed IDs are not retained. Omitting it lets nio own retry IDs.
        Application request IDs, not Matrix transaction IDs, deduplicate new
        logical delivery attempts after a previously successful send.
        """
        from ..crypto.to_device import EncryptedToDevice

        if tx_id is not None and (not isinstance(tx_id, str) or not tx_id):
            raise LocalProtocolError("transaction ID must be a nonempty string")
        assert self.device_id
        operation = EncryptedToDevice.prepare(
            message, recipient_ed25519, self.user_id, self.device_id
        )
        if self._durable_session is not None:
            return await self._durable_session._outbound.encrypted_to_device(
                operation, tx_id
            )
        assert self.olm
        async with self._encrypted_to_device_lock:
            pending = self._pending_encrypted_to_device
            if pending is not None:
                previous_id, fingerprint, encrypted = pending
                if tx_id == previous_id and fingerprint != operation.fingerprint:
                    raise LocalProtocolError(
                        "transaction ID belongs to different encrypted content"
                    )
                response = await self.to_device(encrypted, previous_id)
                if isinstance(response, ToDeviceError):
                    return response
                self._pending_encrypted_to_device = None
                if tx_id == previous_id or (
                    tx_id is None and fingerprint == operation.fingerprint
                ):
                    return response
            query = await self.keys_query({operation.message.recipient})
            device = operation.resolve(self.olm, query)
            if self.olm.session_store.get(device.curve25519) is None:
                claim = await self.keys_claim({device.user_id: [device.id]})
                if not isinstance(claim, KeysClaimResponse):
                    raise LocalProtocolError("encrypted to-device key claim failed")
            device = operation.resolve(self.olm, query)
            if self.olm.session_store.get(device.curve25519) is None:
                raise LocalProtocolError(
                    "encrypted to-device key claim supplied no valid session"
                )
            request_id = tx_id or str(uuid4())
            try:
                encrypted = operation.encrypt(self.olm, device)
            except BaseException:
                self._dispose()
                raise
            self._pending_encrypted_to_device = (
                request_id,
                operation.fingerprint,
                encrypted,
            )
            response = await self.to_device(encrypted, request_id)
            if isinstance(response, ToDeviceResponse):
                self._pending_encrypted_to_device = None
            return response

    @logged_in_async
    async def to_device(
        self,
        message: ToDeviceMessage,
        tx_id: str | None = None,
    ) -> ToDeviceResponse | ToDeviceError:
        """Send a to-device message.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ToDeviceResponse` if the request was successful or
        a `ToDeviceError` if there was an error with the request.

        Args:
            message (ToDeviceMessage): The message that should be sent out.
            tx_id (str, optional): The transaction ID for this message. Should
                be unique.
        """
        if self._durable_session is not None:
            return await self._durable_session._outbound.to_device(message, tx_id)
        uuid = tx_id or uuid4()

        method, path, data = Api.to_device(
            self.access_token, message.type, message.as_dict(), uuid
        )

        return await self._send(
            ToDeviceResponse, method, path, data, response_data=(message,)
        )

    @property
    def cross_signing_identity(self) -> CrossSigningIdentity | None:
        """The persisted self-managed cross-signing identity, if any.

        This is a mindroom-nio fork feature; see crypto/cross_signing.py.
        This accessor is best-effort for diagnostics and returns None when the
        sidecar is absent or unreadable; ensure_cross_signing() is the path
        that surfaces a corrupt identity.
        """
        if not self.store_path or not self.user_id:
            return None
        try:
            return CrossSigningIdentity.load(
                cross_signing_sidecar_path(self.store_path, self.user_id)
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _own_device_keys_payload(self) -> dict[str, Any]:
        """The unsigned device-keys object for this session's device.

        Built from the same source Olm.share_keys() uses to upload the device
        keys, so the object this signature covers cannot drift from what the
        server actually stores.
        """
        assert self.olm
        assert self.user_id
        assert self.device_id
        return {
            "algorithms": list(self.olm._algorithms),
            "device_id": self.device_id,
            "user_id": self.user_id,
            "keys": {
                f"curve25519:{self.device_id}": self.olm.account.identity_keys[
                    "curve25519"
                ],
                f"ed25519:{self.device_id}": self.olm.account.identity_keys["ed25519"],
            },
        }

    async def _post_cross_signing(
        self, path: str, body: dict[str, Any]
    ) -> ClientResponse:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        return await self.send("POST", path, Api.to_json(body), headers)

    @staticmethod
    async def _response_json_or_empty(response: ClientResponse) -> dict[str, Any]:
        """Decode a response body as a JSON object, tolerating non-JSON bodies.

        A WAF, proxy, or overloaded homeserver can return a plain-text or HTML
        error body; ``content_type=None`` ignores the header and the guard turns
        an undecodable body into an empty mapping instead of an escaping error.
        """
        try:
            parsed = await response.json(content_type=None)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def _upload_cross_signing_keys(
        self, identity: CrossSigningIdentity, password: str | None
    ) -> None:
        body: dict[str, Any] = {
            "master_key": identity.master_key_payload(),
            "self_signing_key": identity.self_signing_key_payload(),
        }
        path = "/_matrix/client/v3/keys/device_signing/upload"
        response = await self._post_cross_signing(path, body)
        if response.status == 200:
            return
        # Servers without MSC3967 demand user-interactive auth even for the
        # first upload; retry once with password auth. Only a 401 body is
        # guaranteed JSON, so parse it after the status/password gate to avoid
        # masking non-JSON gateway errors behind a ContentTypeError.
        if response.status != 401 or not password:
            raise LocalProtocolError(
                f"Cross-signing key upload failed: {response.status} {await response.text()}"
            )
        error_body = await self._response_json_or_empty(response)
        session = error_body.get("session")
        auth: dict[str, Any] = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self.user_id},
            "password": password,
        }
        if session:
            auth["session"] = session
        body["auth"] = auth
        response = await self._post_cross_signing(path, body)
        if response.status != 200:
            raise LocalProtocolError(
                f"Cross-signing key upload failed after auth: "
                f"{response.status} {await response.text()}"
            )

    async def _upload_own_device_signature(
        self, identity: CrossSigningIdentity
    ) -> None:
        assert self.user_id
        assert self.device_id
        signed_device = identity.signed_device_payload(self._own_device_keys_payload())
        body = {self.user_id: {self.device_id: signed_device}}
        response = await self._post_cross_signing(
            "/_matrix/client/v3/keys/signatures/upload", body
        )
        response_body = await self._response_json_or_empty(response)
        failures = response_body.get("failures") or {}
        if response.status != 200 or failures:
            raise LocalProtocolError(
                f"Device signature upload failed: {response.status} "
                f"{failures or await response.text()}"
            )

    @logged_in_async
    async def ensure_cross_signing(self, password: str | None = None) -> str:
        """Ensure this account is cross-signed and this device is self-signed.

        This is a mindroom-nio fork feature for bot-style clients: it creates
        a master and self-signing key on first use, persists their private
        seeds next to the encryption store, uploads them (retrying with
        password-based user-interactive auth when the server requires it),
        and signs the current device with the self-signing key.

        Returns one of "already_signed", "uploaded_and_signed", or
        "device_signed".

        Raises LocalProtocolError when encryption or the store is unavailable
        or when the homeserver rejects an upload.
        """
        if not self.olm:
            raise LocalProtocolError("Cross-signing requires encryption support.")
        if not self.store_path:
            raise LocalProtocolError("Cross-signing requires a store path.")
        assert self.user_id
        assert self.device_id

        sidecar = cross_signing_sidecar_path(self.store_path, self.user_id)
        identity = CrossSigningIdentity.load(sidecar)
        if identity is None:
            identity = CrossSigningIdentity.generate(self.user_id)
            identity.save(sidecar)

        if identity.uploaded and self.device_id in identity.signed_devices:
            return "already_signed"

        freshly_uploaded = False
        if not identity.uploaded:
            await self._upload_cross_signing_keys(identity, password)
            identity.uploaded = True
            identity.save(sidecar)
            freshly_uploaded = True

        if self.device_id not in identity.signed_devices:
            # The server can only sign a device whose own keys it already has,
            # and this may run before the first sync uploads them.
            if self.should_upload_keys:
                await self.keys_upload()
            await self._upload_own_device_signature(identity)
            identity.signed_devices.append(self.device_id)
            identity.save(sidecar)

        return "uploaded_and_signed" if freshly_uploaded else "device_signed"

    @logged_in_async
    @store_loaded
    async def keys_upload(self) -> KeysUploadResponse | KeysUploadError:
        """Upload the E2E encryption keys.

        This uploads the long lived session keys as well as the required amount
        of one-time keys.

        Automatically called by sync_forever().

        Calls receive_response() to update the client state if necessary.

        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded or if no encryption keys need to be uploaded.
        """
        if self._durable_session is not None:
            return await self._durable_session._outbound.keys_upload()
        if not self.should_upload_keys:
            raise LocalProtocolError("No key upload needed.")

        assert self.olm
        keys_dict = self.olm.share_keys()

        method, path, data = Api.keys_upload(self.access_token, keys_dict)

        return await self._send(KeysUploadResponse, method, path, data)

    @logged_in_async
    @store_loaded
    async def keys_query(
        self, user_set: Iterable[str] | None = None
    ) -> KeysQueryResponse | KeysQueryError:
        """Query the server for user keys.

        With no arguments, query users whose device lists need refreshing.
        An explicit user set discovers all devices of those users, including
        users with whom this client shares no encrypted room.

        Automatically called by sync_forever() and room_send().

        Calls receive_response() to update the client state if necessary.

        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded or if no key query needs to be performed.
        """
        user_list = set(self.users_for_key_query if user_set is None else user_set)

        if (user_set is not None and not user_list) or any(
            not isinstance(user, str) or not user for user in user_list
        ):
            raise LocalProtocolError("No key query required.")
        if self._durable_session is not None:
            return await self._durable_session._outbound.keys_query(
                None if user_set is None else user_list
            )
        if not user_list:
            raise LocalProtocolError("No key query required.")

        # TODO pass the sync token here if it's a device update that triggered
        # our need for a key query.
        method, path, data = Api.keys_query(self.access_token, user_list)

        assert self.olm
        self.olm.users_for_key_query.difference_update(user_list)
        try:
            response = await self._send(
                KeysQueryResponse, method, path, data, response_data=(user_list,)
            )
        except BaseException:
            self.olm.users_for_key_query.update(user_list)
            raise
        if isinstance(response, KeysQueryResponse):
            self.olm.users_for_key_query.update(user_list - response.device_keys.keys())
        else:
            self.olm.users_for_key_query.update(user_list)
        return response

    @logged_in_async
    async def devices(self) -> DevicesResponse | DevicesError:
        """Get the list of devices for the current user.

        Calls receive_response() to update the client state if necessary.

        Returns either a `DevicesResponse` if the request was successful
        or a `DevicesError` if there was an error with the request.
        """
        method, path = Api.devices(self.access_token)

        return await self._send(DevicesResponse, method, path)

    @logged_in_async
    async def delete_devices(
        self, devices: list[str], auth: dict[str, str] | None = None
    ) -> DeleteDevicesResponse | DeleteDevicesError:
        """Delete a list of devices.

        This tells the server to delete the given devices and invalidate their
        associated access tokens.

        Calls receive_response() to update the client state if necessary.

        Returns either a `DeleteDevicesResponse` if the request was successful
        or a `DeleteDevicesError` if there was an error with the request.

        This endpoint supports user-interactive auth, calling this method
        without an auth dictionary will return a `DeleteDevicesAuthResponse`
        which can be used to introspect the valid authentication methods that
        the server supports.

        Args:
            devices (List[str]): A list of devices which will be deleted.
            auth (Dict): Additional authentication information for
                the user-interactive authentication API.

        Example:
            >>> devices = ["QBUAZIFURK", "AUIECTSRND"]
            >>> auth = {"type": "m.login.password",
            ...         "user": "example",
            ...         "password": "hunter1"}
            >>> await client.delete_devices(devices, auth)


        """
        method, path, data = Api.delete_devices(
            self.access_token, devices, auth_dict=auth
        )

        return await self._send(DeleteDevicesResponse, method, path, data)

    @logged_in_async
    async def joined_members(
        self, room_id: str
    ) -> JoinedMembersResponse | JoinedMembersError:
        """Get the list of joined members for a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `JoinedMembersResponse` if the request was successful
        or a `JoinedMembersError` if there was an error with the request.

        Args:
            room_id(str): The room id of the room for which we wan't to request
                the joined member list.
        """
        if self._durable_session is not None:
            # DurableSync imports AsyncClient, so defer the reverse dependency.
            from ..durable.transport import HttpError

            try:
                return await self._durable_session._outbound.joined_members(room_id)
            except HttpError as error:
                return JoinedMembersError(
                    str(error), status_code=error.errcode, room_id=room_id
                )
        method, path = Api.joined_members(self.access_token, room_id)

        return await self._send(
            JoinedMembersResponse, method, path, response_data=(room_id,)
        )

    @logged_in_async
    async def joined_rooms(
        self,
    ) -> JoinedRoomsResponse | JoinedRoomsError:
        """Get the list of joined rooms.

        Calls receive_response() to update the client state if necessary.

        Returns either a `JoinedRoomsResponse` if the request was successful
        or a `JoinedRoomsError` if there was an error with the request.
        """
        method, path = Api.joined_rooms(self.access_token)

        return await self._send(JoinedRoomsResponse, method, path)

    @logged_in_async
    async def room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[Any, Any],
        tx_id: str | None = None,
        ignore_unverified_devices: bool = False,
    ) -> RoomSendResponse | RoomSendError:
        """Send a message to a room.

        Calls receive_response() to update the client state if necessary.

        Args:
            room_id(str): The room id of the room where the message should be
                sent to.
            message_type(str): A string identifying the type of the message.
            content(Dict[Any, Any]): A dictionary containing the content of the
                message.
            tx_id(str, optional): The transaction ID of this event used to
                uniquely identify this message.
            ignore_unverified_devices(bool): If the room is encrypted and
                contains unverified devices, the devices can be marked as
                ignored here. Ignored devices will still receive encryption
                keys for messages but they won't be marked as verified.

        If the room where the message should be sent is encrypted the message
        will be encrypted before sending.

        This method also makes sure that the room members are fully synced and
        that keys are queried before sending messages to an encrypted room.

        If encrypted state cannot be fully synced after a couple of retries,
        this raises `SendRetryError`.

        Raises `LocalProtocolError` if the client isn't logged in.
        """
        uuid: str | UUID = tx_id or uuid4()

        request = await self._prepare_room_send(
            room_id,
            message_type,
            content,
            uuid,
            ignore_unverified_devices,
        )

        method, path, data = request
        return await self._send(RoomSendResponse, method, path, data, (room_id,))

    async def _prepare_room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[Any, Any],
        tx_id: str | UUID,
        ignore_unverified_devices: bool,
    ) -> tuple[str, str, str]:
        if self.olm:
            try:
                room = self.rooms[room_id]
            except KeyError:
                raise LocalProtocolError(f"No such room with id {room_id} found.")

            durable = self._durable_session
            if durable is not None and not durable._room_ready(room_id):
                raise LocalProtocolError(
                    "room state must be established before plaintext sending"
                )

            if room.encrypted:
                if not room.members_synced:
                    if durable is not None:
                        await durable._outbound.ensure_members(room_id)
                    else:
                        await self.joined_members(room_id)
                    if self.should_query_keys:
                        await self.keys_query()

                if self.olm.should_share_group_session(room_id):
                    try:
                        event = self.sharing_session[room_id]
                        await event.wait()
                    except KeyError:
                        await self.share_group_session(
                            room_id,
                            ignore_unverified_devices=ignore_unverified_devices,
                        )

                # Reactions do not support encryption yet.
                # https://github.com/matrix-org/matrix-doc/pull/1849
                if message_type != "m.reaction":
                    message_type, content = self.encrypt(room_id, message_type, content)

        return Api.room_send(self.access_token, room_id, message_type, content, tx_id)

    @logged_in_async
    @client_session
    async def list_direct_rooms(
        self,
    ) -> DirectRoomsResponse | DirectRoomsErrorResponse:
        """
        Lists all rooms flagged with m.direct that the client is participating in.

        Returns a DirectRoomListResponse if the request was successful, or DirectRoomListErrorResponse if there was an error, or the current user has never marked any rooms marked with m.direct
        """
        method, path = Api.direct_room_list(self.access_token, self.user_id)
        return await self._send(DirectRoomsResponse, method, path)

    @logged_in_async
    async def room_get_event(
        self, room_id: str, event_id: str
    ) -> RoomGetEventResponse | RoomGetEventError:
        """Get a single event based on roomId/eventId.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomGetEventResponse` if the request was successful
        or a `RoomGetEventError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room where the event is in.
            event_id (str): The event id to get.
        """
        method, path = Api.room_get_event(self.access_token, room_id, event_id)

        return await self._send(RoomGetEventResponse, method, path)

    @logged_in_async
    async def room_get_event_relations(
        self,
        room_id: str,
        event_id: str,
        rel_type: RelationshipType | None = None,
        event_type: str | None = None,
        direction: MessageDirection = MessageDirection.back,
        limit: int | None = None,
        recurse: bool = False,
        minimum_recursion_depth: int | None = None,
    ) -> AsyncIterator[Event]:
        """Iterate through all related events of a given parent event.

        Args:
            room_id (str): The room id of the room where the event is in.
            event_id (str): The event id to get.
            rel_type (RelationshipType, optional): The relationship type to search for.
                Required if event_type is provided.
            event_type: (str, optional): The event type of child events to search for.
            direction (MessageDirection, optional): The direction to return
                events from. Defaults to MessageDirection.back.
            limit (int, optional): The maximum events per request that will be
                fetched per chunk while iterating. Changing this value can affect performance.
                Homeservers will apply a default value, and override this with a maximum value.
            recurse (bool, optional): Whether to ask the server for indirectly
                related events as well, as described by the Matrix 1.10
                recursive relations extension.
            minimum_recursion_depth (int, optional): The recursion depth the
                caller requires. Every non-empty page must report at least this
                depth in ``recursion_depth`` or
                ``InsufficientRecursionDepthError`` is raised before any of
                that page's events are yielded. Requires ``recurse``.

                Servers disagree about what the reported number means. Some
                report the depth they are willing to traverse, which is a
                constant capability. Others report the depth of the deepest
                event they actually returned, which for a shallow relation tree
                is legitimately ``0``. A caller that wants only "the server
                honored ``recurse``" should therefore pass ``0``, and a caller
                that imposes a higher floor should know which servers it talks
                to. An empty page is never rejected, because it has no depth to
                report and nothing that could have been truncated.
        """
        if minimum_recursion_depth is not None and not recurse:
            raise LocalProtocolError("minimum_recursion_depth requires recurse=True")

        paginate_from, paginate_to = None, None
        while True:
            method, path = Api.room_get_event_relations(
                self.access_token,
                room_id,
                event_id,
                rel_type,
                event_type,
                direction,
                paginate_from,
                paginate_to,
                limit,
                recurse,
            )
            response = await self._send(
                RoomEventRelationsResponse,
                method,
                path,
                response_data=(room_id, event_id),
            )

            if not isinstance(response, RoomEventRelationsResponse):
                raise RemoteProtocolError(str(response))

            # An empty page carries no depth information: servers that
            # report the deepest event they returned have nothing to
            # report, and there is nothing that could have been truncated.
            if (
                minimum_recursion_depth is not None
                and response.events
                and (
                    response.recursion_depth is None
                    or response.recursion_depth < minimum_recursion_depth
                )
            ):
                raise InsufficientRecursionDepthError(
                    minimum_recursion_depth, response.recursion_depth
                )
            for event in response.events:
                yield event
            if response.next_batch is None:
                return
            paginate_from = response.next_batch

    @logged_in_async
    async def room_get_threads(
        self,
        room_id: str,
        include: ThreadInclusion = ThreadInclusion.all,
        limit: int | None = None,
    ) -> AsyncIterator[Event]:
        """Iterate through the thread roots of a given room.

        Args:
            room_id (str): The room id of the room where the event is in.
            include (ThreadInclusion, optional): Whether to filter only for threads in which
                the user has participated in. Defaults to all threads.
            limit (int, optional): The maximum events per request that will be
                fetched per chunk while iterating. Changing this value can affect performance.
                Homeservers will apply a default value, and override this with a maximum value.
        """
        paginate_from = None
        while True:
            method, path = Api.room_get_threads(
                self.access_token,
                room_id,
                include,
                paginate_from,
                limit,
            )
            response = await self._send(
                RoomThreadsResponse,
                method,
                path,
                response_data=(room_id,),
            )

            if isinstance(response, RoomThreadsResponse):
                for event in response.thread_roots:
                    yield event
            if response.next_batch is None:
                return
            paginate_from = response.next_batch

    @logged_in_async
    async def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[Any, Any],
        state_key: str = "",
    ) -> RoomPutStateResponse | RoomPutStateError:
        """Send a state event to a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomPutStateResponse` if the request was successful
        or a `RoomPutStateError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room to send the event to.
            event_type (str): The type of the state to send.
            content (Dict[Any, Any]): The content of the event to be sent.
            state_key (str): The key of the state event to send.
        """

        method, path, data = Api.room_put_state(
            self.access_token,
            room_id,
            event_type,
            content,
            state_key=state_key,
        )

        return await self._send(
            RoomPutStateResponse,
            method,
            path,
            data,
            response_data=(room_id,),
        )

    @logged_in_async
    async def room_get_state(
        self,
        room_id: str,
    ) -> RoomGetStateResponse | RoomGetStateError:
        """Fetch state for a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomGetStateResponse` if the request was successful
        or a `RoomGetStateError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room to fetch state from.
        """

        method, path = Api.room_get_state(
            self.access_token,
            room_id,
        )

        return await self._send(
            RoomGetStateResponse,
            method,
            path,
            response_data=(room_id,),
        )

    @logged_in_async
    async def room_get_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> RoomGetStateEventResponse | RoomGetStateEventError:
        """Fetch a state event from a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomGetStateEventResponse` if the request was
        successful or a `RoomGetStateEventError` if there was an error with
        the request.

        Args:
            room_id (str): The room id of the room to fetch the event from.
            event_type (str): The type of the state to fetch.
            state_key (str): The key of the state event to fetch.
        """

        method, path = Api.room_get_state_event(
            self.access_token, room_id, event_type, state_key=state_key
        )

        return await self._send(
            RoomGetStateEventResponse,
            method,
            path,
            response_data=(
                event_type,
                state_key,
                room_id,
            ),
        )

    @logged_in_async
    async def room_redact(
        self,
        room_id: str,
        event_id: str,
        reason: str | None = None,
        tx_id: None | str | UUID = None,
    ) -> RoomRedactResponse | RoomRedactError:
        """Strip information out of an event.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomRedactResponse` if the request was successful or
        a `RoomRedactError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room that contains the event that
                will be redacted.
            event_id (str): The ID of the event that will be redacted.
            tx_id (str/UUID, optional): A transaction ID for this event.
            reason(str, optional): A description explaining why the
                event was redacted.
        """
        method, path, data = Api.room_redact(
            self.access_token,
            room_id,
            event_id,
            tx_id=tx_id or uuid4(),
            reason=reason,
        )

        return await self._send(
            RoomRedactResponse,
            method,
            path,
            data,
            response_data=(room_id,),
        )

    async def room_resolve_alias(
        self,
        room_alias: str,
    ) -> RoomResolveAliasResponse | RoomResolveAliasError:
        """Resolve a room alias to a room ID.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomResolveAliasResponse` if the request was
        successful or a `RoomResolveAliasError if there was an error
        with the request.

        Args:
            room_alias (str): The alias to resolve
        """
        method, path = Api.room_resolve_alias(room_alias)

        return await self._send(
            RoomResolveAliasResponse,
            method,
            path,
            response_data=(room_alias,),
        )

    @logged_in_async
    async def room_put_alias(
        self,
        room_alias: str,
        room_id: str,
    ) -> RoomPutAliasResponse | RoomPutAliasError:
        """Add a room alias.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomPutAliasResponse` if the request was
        successful or a `RoomPutAliasError if there was an error
        with the request.

        Args:
            room_alias (str): The alias to add
            room_id (str): The room ID to map to
        """
        method, path, data = Api.room_put_alias(
            self.access_token,
            room_alias,
            room_id,
        )

        return await self._send(
            RoomPutAliasResponse,
            method,
            path,
            data=data,
            response_data=(room_alias, room_id),
        )

    async def room_get_visibility(
        self,
        room_id: str,
    ) -> RoomGetVisibilityResponse | RoomGetVisibilityError:
        """Get visibility for a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomGetVisibilityResponse` if the request was
        successful or a `RoomGetVisibilityError if there was an error
        with the request.

        Args:
            room_id (str): The room ID to get visibility for
        """
        method, path = Api.room_get_visibility(room_id)

        return await self._send(
            RoomGetVisibilityResponse,
            method,
            path,
            response_data=(room_id,),
        )

    @logged_in_async
    @store_loaded
    async def keys_claim(
        self, user_set: dict[str, Iterable[str]]
    ) -> KeysClaimResponse | KeysClaimError:
        """Claim one-time keys for a set of user and device pairs.

        Automatically called by sync_forever() and room_send().

        Calls receive_response() to update the client state if necessary.

        Args:
            user_set(Dict[str, Iterator[str]]): A dictionary mapping from a user
                id to a iterator of device ids. If a user set for a specific
                room is required it can be obtained using the
                `get_missing_sessions()` method.

        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded, no room with the given room id exists or the room
        isn't an encrypted room.
        """
        if self._durable_session is not None:
            return await self._durable_session._outbound.keys_claim(user_set)
        method, path, data = Api.keys_claim(self.access_token, user_set)

        return await self._send(KeysClaimResponse, method, path, data)

    @logged_in_async
    @store_loaded
    async def share_group_session(
        self,
        room_id: str,
        ignore_unverified_devices: bool = False,
    ) -> ShareGroupSessionResponse | ShareGroupSessionError:
        """Share a group session with a room.

        This method sends a group session to members of a room.

        Automatically called by room_send().

        Calls receive_response() to update the client state if necessary.

        Args:
            room_id(str): The room id of the room where the message should be
                sent to.
            ignore_unverified_devices(bool): Mark unverified devices as
                ignored. Ignored devices will still receive encryption
                keys for messages but they won't be marked as verified.


        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded, no room with the given room id exists, the room
        isn't an encrypted room or a key sharing request is already in flight
        for this room.
        """
        assert self.olm
        if self._durable_session is not None:
            return await self._durable_session._outbound.share_group_session(
                room_id, ignore_unverified_devices
            )

        try:
            room = self.rooms[room_id]
        except KeyError:
            raise LocalProtocolError(f"No such room with id {room_id}")

        if not room.encrypted:
            raise LocalProtocolError(f"Room with id {room_id} is not encrypted")

        if room_id in self.sharing_session:
            raise LocalProtocolError(f"Already sharing a group session for {room_id}")

        self.sharing_session[room_id] = AsyncioEvent()

        missing_sessions = self.get_missing_sessions(room_id)

        if missing_sessions:
            await self.keys_claim(missing_sessions)

        shared_with = set()

        try:
            requests = []

            for sharing_with, to_device_dict in self.olm.share_group_session_parallel(
                room_id,
                list(room.users.keys()),
                ignore_unverified_devices=ignore_unverified_devices,
            ):
                method, path, data = Api.to_device(
                    self.access_token, "m.room.encrypted", to_device_dict, uuid4()
                )

                requests.append(
                    self._send(
                        ShareGroupSessionResponse,
                        method,
                        path,
                        data,
                        response_data=(room_id, sharing_with),
                    )
                )

            for response in await asyncio.gather(*requests, return_exceptions=True):
                if isinstance(response, ShareGroupSessionResponse):
                    shared_with.update(response.users_shared_with)

            # Mark the session as shared, usually the olm machine will do this
            # for us, but if there was no-one to share the session with it we
            # need to do it ourselves.
            self.olm.outbound_group_sessions[room_id].shared = True

        except ClientConnectionError:
            raise
        finally:
            event = self.sharing_session.pop(room_id)
            event.set()

        return ShareGroupSessionResponse(room_id, shared_with)

    @logged_in_async
    @store_loaded
    async def request_room_key(
        self,
        event: MegolmEvent,
        tx_id: str | None = None,
    ) -> RoomKeyRequestResponse | RoomKeyRequestError:
        """Request a missing room key.

        This sends out a message to other devices requesting a room key from
        them.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomKeyRequestResponse` if the request was successful
        or a `RoomKeyRequestError` if there was an error with the request.

        Raises a LocalProtocolError if the room key was already requested.

        Args:
            event (MegolmEvent): An undecrypted MegolmEvent for which we would
                like to request the decryption key.
        """
        if self._durable_session is not None:
            return await self._durable_session._outbound.request_room_key(event, tx_id)
        uuid = tx_id or uuid4()

        if event.session_id in self.outgoing_key_requests:
            raise LocalProtocolError(
                "A key sharing request is already sent out for this session id."
            )

        assert self.user_id
        assert self.device_id

        message = event.as_key_request(self.user_id, self.device_id)

        method, path, data = Api.to_device(
            self.access_token, message.type, message.as_dict(), uuid
        )

        return await self._send(
            RoomKeyRequestResponse,
            method,
            path,
            data,
            (
                event.session_id,
                event.session_id,
                event.room_id,
                event.algorithm,
            ),
        )

    async def close(self):
        """Close the underlying HTTP session."""
        if self.client_session:
            await self.client_session.close()
            self.client_session = None

    @logged_in_async
    async def room_create(
        self,
        visibility: RoomVisibility = RoomVisibility.private,
        alias: str | None = None,
        name: str | None = None,
        topic: str | None = None,
        room_version: str | None = None,
        room_type: str | None = None,
        federate: bool = True,
        is_direct: bool = False,
        preset: RoomPreset | None = None,
        invite: Sequence[str] = (),
        initial_state: Sequence[dict[str, Any]] = (),
        power_level_override: dict[str, Any] | None = None,
        predecessor: dict[str, Any] | None = None,
        space: bool = False,
        additional_creators: list[str] | None = None,
    ) -> RoomCreateResponse | RoomCreateError:
        """Create a new room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomCreateResponse` if the request was successful or
        a `RoomCreateError` if there was an error with the request.

        Args:
            visibility (RoomVisibility): whether to have the room published in
                the server's room directory or not.
                Defaults to ``RoomVisibility.private``.

            alias (str, optional): The desired canonical alias local part.
                For example, if set to "foo" and the room is created on the
                "example.com" server, the room alias will be
                "#foo:example.com".

            name (str, optional): A name to set for the room.

            topic (str, optional): A topic to set for the room.

            room_version (str, optional): The room version to set.
                If not specified, the homeserver will use its default setting.
                If a version not supported by the homeserver is specified,
                a 400 ``M_UNSUPPORTED_ROOM_VERSION`` error will be returned.

            room_type (str, optional): The room type to set.
                If not specified, the homeserver will use its default setting.
                In spec v1.2 the following room types are specified:
                    - ``m.space``
                Unspecified room types are permitted through the use of Namespaced Identifiers.

            federate (bool): Whether to allow users from other homeservers from
                joining the room. Defaults to ``True``.
                Cannot be changed later.

            is_direct (bool): If this should be considered a
                direct messaging room.
                If ``True``, the server will set the ``is_direct`` flag on
                ``m.room.member events`` sent to the users in ``invite``.
                Defaults to ``False``.

            preset (RoomPreset, optional): The selected preset will set various
                rules for the room.
                If unspecified, the server will choose a preset from the
                ``visibility``: ``RoomVisibility.public`` equates to
                ``RoomPreset.public_chat``, and
                ``RoomVisibility.private`` equates to a
                ``RoomPreset.private_chat``.

            invite (list): A list of user id to invite to the room.

            initial_state (list): A list of state event dicts to send when
                the room is created.
                For example, a room could be made encrypted immediately by
                having a ``m.room.encryption`` event dict.

            power_level_override (dict): A ``m.room.power_levels content`` dict
                to override the default.
                The dict will be applied on top of the generated
                ``m.room.power_levels`` event before it is sent to the room.

            predecessor (dict): A reference to the room this room replaces, if the previous room was upgraded.
                Containing the event ID of the last known event in the old room (for room versions < 12).
                And the ID of the old room.
                ``event_id``: ``$something:example.org``,
                ``room_id``: ``!oldroom:example.org``

            space (bool): Create as a Space (defaults to False).

            additional_creators (list): a list of user id to give the same
                (infinite) power level as the sender of the event.
                Applicable to room versions >= 12.
        """

        method, path, data = Api.room_create(
            self.access_token,
            visibility=visibility,
            alias=alias,
            name=name,
            topic=topic,
            room_version=room_version,
            room_type=room_type,
            federate=federate,
            is_direct=is_direct,
            preset=preset,
            invite=invite,
            initial_state=initial_state,
            power_level_override=power_level_override,
            predecessor=predecessor,
            space=space,
            additional_creators=additional_creators,
        )

        return await self._send(RoomCreateResponse, method, path, data)

    @logged_in_async
    async def join(self, room_id: str) -> JoinResponse | JoinError:
        """Join a room.

        This tells the server to join the given room.
        If the room is not public, the user must be invited.

        Calls receive_response() to update the client state if necessary.

        Returns either a `JoinResponse` if the request was successful or
        a `JoinError` if there was an error with the request.

        Args:
            room_id: The room id or alias of the room to join.
        """
        method, path = Api.join(self.access_token, room_id)
        return await self._send(JoinResponse, method, path)

    @logged_in_async
    async def room_invite(
        self,
        room_id: str,
        user_id: str,
    ) -> RoomInviteResponse | RoomInviteError:
        """Invite a user to a room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomInviteResponse` if the request was successful or
        a `RoomInviteError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room that the user will be
                invited to.
            user_id (str): The user id of the user that should be invited.
        """
        method, path, data = Api.room_invite(
            self.access_token,
            room_id,
            user_id,
        )
        return await self._send(RoomInviteResponse, method, path, data)

    @logged_in_async
    async def room_leave(self, room_id: str) -> RoomLeaveResponse | RoomLeaveError:
        """Leave a room or reject an invite.

        This tells the server to leave the given room.
        If the user was only invited, the invite is rejected.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomLeaveResponse` if the request was successful or
        a `RoomLeaveError` if there was an error with the request.

        Args:
            room_id: The room id of the room to leave.
        """
        method, path = Api.room_leave(self.access_token, room_id)
        return await self._send(RoomLeaveResponse, method, path)

    @logged_in_async
    async def room_kick(
        self,
        room_id: str,
        user_id: str,
        reason: str | None = None,
    ) -> RoomKickResponse | RoomKickError:
        """Kick a user from a room, or withdraw their invitation.

        Kicking a user adjusts their membership to "leave" with an optional
        reason.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomKickResponse` if the request was successful or
        a `RoomKickError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room that the user will be
                kicked from.
            user_id (str): The user_id of the user that should be kicked.
            reason (str, optional): A reason for which the user is kicked.
        """

        method, path, data = Api.room_kick(
            self.access_token,
            room_id,
            user_id,
            reason,
        )
        return await self._send(RoomKickResponse, method, path, data)

    @logged_in_async
    async def room_context(
        self,
        room_id: str,
        event_id: str,
        limit: int | None = None,
    ) -> RoomContextResponse | RoomContextError:
        """Fetch a number of events that happened before and after an event.

        This allows clients to get the context surrounding an event.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomContextResponse` if the request was successful or
        a `RoomContextError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room that contains the event and
                its context.
            event_id (str): The event_id of the event that we wish to get the
                context for.
            limit(int, optional): The maximum number of events to request.
        """

        method, path = Api.room_context(self.access_token, room_id, event_id, limit)

        return await self._send(
            RoomContextResponse, method, path, response_data=(room_id,)
        )

    @logged_in_async
    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        end: str | None = None,
        direction: MessageDirection = MessageDirection.back,
        limit: int = 10,
        message_filter: dict[Any, Any] | None = None,
    ) -> RoomMessagesResponse | RoomMessagesError:
        """Fetch a list of message and state events for a room.

        It uses pagination query parameters to paginate history in the room.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomMessagesResponse` if the request was successful or
        a `RoomMessagesError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room for which we would like to
                fetch the messages.
            start (str): The token to start returning events from. This token
                can be obtained from a prev_batch token returned for each room
                by the sync API, or from a start or end token returned by a
                previous request to this endpoint.
            end (str, optional): The token to stop returning events at. This
                token can be obtained from a prev_batch token returned for
                each room by the sync endpoint, or from a start or end token
                returned by a previous request to this endpoint.
            direction (MessageDirection, optional): The direction to return
                events from. Defaults to MessageDirection.back.
            limit (int, optional): The maximum number of events to return.
                Defaults to 10.
            message_filter (Optional[Dict[Any, Any]]):
                A filter dict that should be used for this room messages
                request.

        Example:
            >>> response = await client.room_messages(room_id, previous_batch)
            >>> next_response = await client.room_messages(room_id,
            ...                                            response.end)


        """
        method, path = Api.room_messages(
            self.access_token,
            room_id,
            start=start,
            end=end,
            direction=direction,
            limit=limit,
            message_filter=message_filter,
        )

        return await self._send(
            RoomMessagesResponse, method, path, response_data=(room_id,)
        )

    @logged_in_async
    async def room_typing(
        self,
        room_id: str,
        typing_state: bool = True,
        timeout: int = 30_000,  # noqa: ASYNC109
    ) -> RoomTypingResponse | RoomTypingError:
        """Send a typing notice to the server.

        This tells the server that the user is typing for the next N
        milliseconds or that the user has stopped typing.

        Calls receive_response() to update the client state if necessary.

        Returns either a `RoomTypingResponse` if the request was successful or
        a `RoomTypingError` if there was an error with the request.

        Args:
            room_id (str): The room id of the room where the user is typing.
            typing_state (bool): A flag representing whether the user started
                or stopped typing.
            timeout (int): For how long should the new typing notice be
                valid for in milliseconds.
        """
        method, path, data = Api.room_typing(
            self.access_token, room_id, self.user_id, typing_state, timeout
        )

        return await self._send(
            RoomTypingResponse, method, path, data, response_data=(room_id,)
        )

    @logged_in_async
    async def content_repository_config(
        self,
    ) -> ContentRepositoryConfigResponse | ContentRepositoryConfigError:
        """Get the content repository configuration, such as upload limits.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ContentRepositoryConfigResponse` if the request
        was successful or a `ContentRepositoryConfigError` if there was an
        error with the request.
        """
        method, path = Api.content_repository_config(self.access_token)

        return await self._send(ContentRepositoryConfigResponse, method, path)

    @staticmethod
    async def _process_data_chunk(chunk, monitor: TransferMonitor | None = None):
        if monitor and monitor.cancel:
            raise TransferCancelledError

        while monitor and monitor.pause:  # noqa: ASYNC110
            await asyncio.sleep(0.1)

        return chunk

    async def _plain_data_generator(self, data, monitor=None):
        """Yield chunks of bytes from data.

        If a monitor is passed, update its ``transferred`` property and
        suspend yielding chunks while its ``pause`` attribute is ``True``.

        Raise ``TransferCancelledError`` if ``monitor.cancel`` is ``True``.
        """

        async for value in async_generator_from_data(data):
            yield await self._process_data_chunk(value, monitor)

    async def _encrypted_data_generator(
        self,
        data,
        decryption_dict,
        monitor=None,
    ):
        """Yield encrypted chunks of bytes from data.

        If a monitor is passed, update its ``transferred`` property and
        suspend yielding chunks while its ``pause`` attribute is ``True``.

        The last yielded value will be the decryption dict.

        Raise ``TransferCancelledError`` if ``monitor.cancel`` is ``True``.
        """

        async for value in async_encrypt_attachment(data):
            if isinstance(value, dict):  # last yielded value
                decryption_dict.update(value)
            else:
                yield await self._process_data_chunk(value, monitor)

    @logged_in_async
    async def upload(
        self,
        data_provider: DataProvider | SynchronousFileType | AsyncFileType,
        content_type: str = "application/octet-stream",
        filename: str | None = None,
        encrypt: bool = False,
        monitor: TransferMonitor | None = None,
        filesize: int | None = None,
    ) -> tuple[UploadResponse | UploadError, dict[str, Any] | None]:
        """Upload a file to the content repository.

        This method ignores `AsyncClient.config.request_timeout` and uses `0`.

        Calls receive_response() to update the client state if necessary.

        Returns a tuple containing:

        - Either a `UploadResponse` if the request was successful, or a
          `UploadError` if there was an error with the request

        - A dict with file decryption info if encrypt is ``True``,
          else ``None``.

        Raises a ``TransferCancelledError`` if a monitor is passed and its
        ``cancelled`` property becomes set to ``True``.

        Args:
            data_provider (Callable, SynchronousFile, AsyncFile): A function
                returning the data to upload or a file object. File objects
                must be opened in binary mode (``mode="r+b"``). Callables
                returning a path string, Path, async iterable or aiofiles
                open binary file object allow the file data to be read in an
                asynchronous and lazy way (without reading the entire file
                into memory). Returning a synchronous iterable or standard
                open binary file object will still allow the data to be read
                lazily, but not asynchronously.

                The function will be called again if the upload fails
                due to a server timeout, in which case it must restart
                from the beginning.
                Callables receive two arguments: the total number of
                429 "Too many request" errors that occurred, and the total
                number of server timeout exceptions that occurred, thus
                cleanup operations can be performed for retries if necessary.

            content_type (str): The content MIME type of the file,
                e.g. "image/png".
                Defaults to "application/octet-stream", corresponding to a
                generic binary file.
                Custom values are ignored if encrypt is ``True``.

            filename (str, optional): The file's original name.

            encrypt (bool): If the file's content should be encrypted,
                necessary for files that will be sent to encrypted rooms.
                Defaults to ``False``.

            monitor (TransferMonitor, optional): If a ``TransferMonitor``
                object is passed, it will be updated by this function while
                uploading.
                From this object, statistics such as currently
                transferred bytes or estimated remaining time can be gathered
                while the upload is running as a task; it also allows
                for pausing and cancelling.

            filesize (int, optional): Size in bytes for the file to transfer.
                If left as ``None``, some servers might refuse the upload.

        It's common to use this alongside :py:meth:`room_send`. An example of
        uploading a plain text file follows, but the principle is the same for
        media, you just need to add an additional "info" key to the content.
        See `the Matrix client-server spec <https://matrix.org/docs/spec/client_server/r0.6.0#m-room-message-msgtypes>`_
        for more details.

        Example:
            >>> file_stat = await aiofiles.os.stat("sample.py")
            >>> async with aiofiles.open("sample.py", "r+b") as f:
            >>>    resp, maybe_keys = await client.upload(
            ...        f,
            ...        content_type="text/plain",
            ...        filename="hello.py",
            ...        filesize=file_stat.st_size()
            ...    )

            >>>    await client.room_send(
            ...        room_id="!myfaveroom:example.org",
            ...        message_type="m.room.message",
            ...        content = {
            ...            "msgtype": "m.file",
            ...            "url": resp.content_uri,
            ...            "body": "descriptive title (like the filename)"
            ...        }
            ...    )
        """

        http_method, path = Api.upload(self.access_token, filename)

        decryption_dict: dict[str, Any] = {}

        initial_file_pos = 0

        async def provider(got_429: int, got_timeouts: int) -> AsyncDataT:
            nonlocal initial_file_pos
            if monitor and (got_429 or got_timeouts):
                # We have to restart from scratch
                monitor.transferred = 0

            upload_data: AsyncDataT | SynchronousFileType | AsyncFileType
            if callable(data_provider):
                upload_data = data_provider(got_429, got_timeouts)

            elif isinstance(data_provider, SynchronousFile):
                if got_429 or got_timeouts:
                    data_provider.seek(initial_file_pos)
                else:
                    initial_file_pos = data_provider.tell()

                upload_data = data_provider

            elif isinstance(data_provider, AsyncFile):
                if got_429 or got_timeouts:
                    await data_provider.seek(initial_file_pos)
                else:
                    initial_file_pos = await data_provider.tell()

                upload_data = data_provider

            else:
                raise TypeError(
                    f"data_provider type {type(data_provider)} "
                    "is not of a usable type "
                    f"(Callable, {SynchronousFile}, {AsyncFile})"
                )

            if encrypt:
                return self._encrypted_data_generator(
                    upload_data,
                    decryption_dict,
                    monitor,
                )

            return self._plain_data_generator(upload_data, monitor)

        response = await self._send(
            UploadResponse,
            http_method,
            path,
            data_provider=provider,
            content_type="application/octet-stream" if encrypt else content_type,
            trace_context=monitor,
            timeout=0,
            content_length=filesize,
        )

        # After the upload finished and we get the response above, if encrypt
        # is True, decryption_dict will have been updated from inside the
        # self._encrypted_data_generator().
        return (response, decryption_dict if encrypt else None)

    @client_session
    async def download(
        self,
        mxc: str | None = None,
        filename: str | None = None,
        allow_remote: bool = True,
        server_name: str | None = None,
        media_id: str | None = None,
        save_to: os.PathLike | None = None,
    ) -> DiskDownloadResponse | MemoryDownloadResponse | DownloadError:
        """Get the content of a file from the content repository.

        This method ignores `AsyncClient.config.request_timeout` and uses `0`.

        Calls receive_response() to update the client state if necessary.

        Returns either a `MemoryDownloadResponse` or `DiskDownloadResponse` if the request was successful or
        a `DownloadError` if there was an error with the request.

        The parameters `server_name` and `media_id` are deprecated and will be removed in a future release.
        Use `mxc` instead.

        Args:
            mxc (str, optional): The mxc:// URI.
            filename (str, optional): A filename to be returned in the response
                by the server. If None (default), the original name of the
                file will be returned instead, if there is one.
            allow_remote (bool): Indicates to the server that it should not
                attempt to fetch the media if it is deemed remote.
                This is to prevent routing loops where the server contacts
                itself.
            server_name (str, optional): [deprecated] The server name from the mxc:// URI.
            media_id (str, optional): [deprecated] The media ID from the mxc:// URI.
            save_to (PathLike, optional): If set, the downloaded file will be saved to this path,
                instead of being saved in-memory.
        """
        # TODO: support TransferMonitor

        if mxc is None:
            if server_name is None or media_id is None:
                # Too few parameters are passed.
                raise TypeError(
                    "Either `mxc` or both the `server_name` and `media_id` are required"
                )
            if server_name is not None or media_id is not None:
                # Deprecated parameters are passed.
                warnings.warn(
                    "The parameters `server_name` and `media_id` are deprecated "
                    "and will be removed in a future release. Use `mxc` instead",
                    DeprecationWarning,
                )
        else:
            if server_name is not None or media_id is not None:
                # Potentially clashing parameters are passed.
                raise TypeError(
                    "The parameters `server_name` and `media_id` are deprecated "
                    "and will be removed in a future release. Use `mxc` instead"
                )
            else:
                # `mxc` is passed; expected behavior
                url = urlparse(mxc)
                server_name = url.netloc
                media_id = url.path.replace("/", "")

        http_method, path = Api.download(
            server_name,
            media_id,
            filename,
            allow_remote,
            access_token=self.access_token,
        )

        response_class: type[DiskDownloadResponse] | type[MemoryDownloadResponse] = (
            MemoryDownloadResponse
        )
        if save_to is not None:
            response_class = DiskDownloadResponse

        return await self._send(
            response_class,
            http_method,
            path,
            timeout=0,
            save_to=save_to,
        )

    @client_session
    async def thumbnail(
        self,
        server_name: str,
        media_id: str,
        width: int,
        height: int,
        method: ResizingMethod = ResizingMethod.scale,
        allow_remote: bool = True,
    ) -> ThumbnailResponse | ThumbnailError:
        """Get the thumbnail of a file from the content repository.

        The actual thumbnail may be larger than the size specified.
        This method ignores `AsyncClient.config.request_timeout` and uses `0`.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ThumbnailResponse` if the request was successful or
        a `ThumbnailError` if there was an error with the request.

        Args:
            server_name (str): The server name from the mxc:// URI.
            media_id (str): The media ID from the mxc:// URI.
            width (int): The desired width of the thumbnail.
            height (int): The desired height of the thumbnail.
            method (ResizingMethod): The desired resizing method.
            allow_remote (bool): Indicates to the server that it should not
                attempt to fetch the media if it is deemed remote.
                This is to prevent routing loops where the server contacts
                itself.
        """
        http_method, path = Api.thumbnail(
            server_name,
            media_id,
            width,
            height,
            method,
            allow_remote,
            access_token=self.access_token,
        )

        return await self._send(
            ThumbnailResponse,
            http_method,
            path,
            timeout=0,
        )

    @client_session
    async def get_profile(
        self, user_id: str | None = None
    ) -> ProfileGetResponse | ProfileGetError:
        """Get a user's combined profile information.

        This queries the display name and avatar matrix content URI of a user
        from the server. Additional profile information may be present.
        The currently logged in user is queried if no user is specified.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileGetResponse` if the request was
        successful or a `ProfileGetError` if there was an error
        with the request.

        Args:
            user_id (str): User id of the user to get the profile for.
        """
        method, path = Api.profile_get(
            user_id or self.user_id, access_token=self.access_token or None
        )

        return await self._send(
            ProfileGetResponse,
            method,
            path,
        )

    @client_session
    async def get_presence(
        self, user_id: str
    ) -> PresenceGetResponse | PresenceGetError:
        """Get a user's presence state.

        This queries the presence state of a user from the server.

        Calls receive_response() to update the client state if necessary.

        Returns either a `PresenceGetResponse` if the request was
        successful or a `PresenceGetError` if there was an error
        with the request.

        Args:
            user_id (str): User id of the user to get the presence state for.
        """

        method, path = Api.get_presence(self.access_token, user_id)

        return await self._send(
            PresenceGetResponse, method, path, response_data=(user_id,)
        )

    @client_session
    async def set_presence(
        self, presence: str, status_msg: str | None = None
    ) -> PresenceSetResponse | PresenceSetError:
        """Set our user's presence state.

        This tells the server to set presence state of the currently logged
        in user to the supplied string.

        Calls receive_response() to update the client state if necessary.

        Returns either a `PresenceSetResponse` if the request was
        successful or a `PresenceSetError` if there was an error
        with the request.

        Args:
            presence (str): The new presence state. One of: ["online", "offline", "unavailable"]
            status_msg (str, optional): The status message to attach to this state.
        """

        method, path, data = Api.set_presence(
            self.access_token, self.user_id, presence, status_msg
        )

        resp = await self._send(PresenceSetResponse, method, path, data)
        if isinstance(resp, PresenceSetResponse):
            self._presence = presence

        return resp

    @client_session
    async def get_displayname(
        self, user_id: str | None = None
    ) -> _ProfileGetDisplayNameT:
        """Get a user's display name.

        This queries the display name of a user from the server.
        The currently logged in user is queried if no user is specified.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileGetDisplayNameResponse` if the request was
        successful or a `ProfileGetDisplayNameError` if there was an error
        with the request.

        Args:
            user_id (str): User id of the user to get the display name for.
        """
        method, path = Api.profile_get_displayname(
            user_id or self.user_id, access_token=self.access_token or None
        )

        return await self._send(
            ProfileGetDisplayNameResponse,
            method,
            path,
        )

    @logged_in_async
    async def set_displayname(self, displayname: str) -> _ProfileSetDisplayNameT:
        """Set user's display name.

        This tells the server to set display name of the currently logged
        in user to the supplied string.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileSetDisplayNameResponse` if the request was
        successful or a `ProfileSetDisplayNameError` if there was an error
        with the request.

        Args:
            displayname (str): Display name to set.
        """
        method, path, data = Api.profile_set_displayname(
            self.access_token, self.user_id, displayname
        )

        return await self._send(
            ProfileSetDisplayNameResponse,
            method,
            path,
            data,
        )

    @client_session
    async def get_avatar(
        self, user_id: str | None = None
    ) -> ProfileGetAvatarResponse | ProfileGetAvatarError:
        """Get a user's avatar URL.

        This queries the avatar matrix content URI of a user from the server.
        The currently logged in user is queried if no user is specified.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileGetAvatarResponse` if the request was
        successful or a `ProfileGetAvatarError` if there was an error
        with the request.

        Args:
            user_id (str): User id of the user to get the avatar for.
        """
        method, path = Api.profile_get_avatar(
            user_id or self.user_id, access_token=self.access_token or None
        )

        return await self._send(
            ProfileGetAvatarResponse,
            method,
            path,
        )

    @logged_in_async
    async def set_avatar(
        self, avatar_url: str
    ) -> ProfileSetAvatarResponse | ProfileSetAvatarError:
        """Set the user's avatar URL.

        This tells the server to set the avatar of the currently logged
        in user to supplied matrix content URI.

        Calls receive_response() to update the client state if necessary.

        Returns either a `ProfileSetAvatarResponse` if the request was
        successful or a `ProfileSetAvatarError` if there was an error
        with the request.

        Args:
            avatar_url (str): matrix content URI of the avatar to set.
        """
        method, path, data = Api.profile_set_avatar(
            self.access_token, self.user_id, avatar_url
        )

        return await self._send(
            ProfileSetAvatarResponse,
            method,
            path,
            data,
        )

    @logged_in_async
    async def get_openid_token(
        self, user_id: str
    ) -> GetOpenIDTokenResponse | GetOpenIDTokenError:
        """Gets an OpenID token object that the requester may supply to another service
        to verify their identity in matrix.

        Returns either a `GetOpenIDTokenResponse` if the request was
        successful or a `GetOpenIDTokenError` if there was an error
        with the request.

        Args:
            user_id (str): The user who requested the OpenID token
        """

        method, path, data = Api.get_openid_token(self.access_token, user_id)

        return await self._send(GetOpenIDTokenResponse, method, path, data)

    @logged_in_async
    async def upload_filter(
        self,
        user_id: str | None = None,
        event_fields: list[str] | None = None,
        event_format: EventFormat = EventFormat.client,
        presence: dict[str, Any] | None = None,
        account_data: dict[str, Any] | None = None,
        room: dict[str, Any] | None = None,
    ) -> UploadFilterResponse | UploadFilterError:
        """Upload a new filter definition to the homeserver.

        Returns either a `UploadFilterResponse` if the request was
        successful or a `UploadFilterError` if there was an error
        with the request.

        The filter ID from the successful responses can be used for
        the ``AsyncClient.sync()``, ``AsyncClient.sync_forever()`` and
        ``AsyncClient.room_messages()`` methods.

        Args:
            user_id (Optional[str]):  ID of the user uploading the filter.
                If not provider, the current logged in user's ID is used.

            event_fields (Optional[List[str]]): List of event fields to
                include. If this list is absent then all fields are included.
                The entries may include '.' characters to indicate sub-fields.
                A literal '.' character in a field name may be escaped
                using a '\'.

            event_format (EventFormat): The format to use for events.

            presence (Dict[str, Any]): The presence updates to include.
                The dict corresponds to the `EventFilter` type described
                in https://matrix.org/docs/spec/client_server/latest#id240

            account_data (Dict[str, Any]): The user account data that isn't
                associated with rooms to include.
                The dict corresponds to the `EventFilter` type described
                in https://matrix.org/docs/spec/client_server/latest#id240

            room (Dict[str, Any]): Filters to be applied to room data.
                The dict corresponds to the `RoomFilter` type described
                in https://matrix.org/docs/spec/client_server/latest#id240
        """
        method, path, data = Api.upload_filter(
            self.access_token,
            user_id or self.user_id,
            event_fields,
            event_format,
            presence,
            account_data,
            room,
        )

        return await self._send(UploadFilterResponse, method, path, data)

    async def whoami(self) -> WhoamiResponse | WhoamiError:
        """Get information about the logged-in user from the homeserver.

        Returns either a `WhoamiResponse` if the request was successful
        or a `WhoamiError` if there was an error with the request.

        On a successful response, the client's state will be updated with
        the user_id and device_id returned, if different from the current state.
        """
        if self.access_token is None:
            raise ValueError("No access_token is set.")

        method, path = Api.whoami(self.access_token)
        return await self._send(WhoamiResponse, method, path)
