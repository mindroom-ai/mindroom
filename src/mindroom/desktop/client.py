"""Cloud-side request/response client for one Matrix desktop device."""

from __future__ import annotations

import asyncio
import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.desktop.protocol import (
    DESKTOP_COMMAND_EVENT_TYPE,
    DESKTOP_RESPONSE_EVENT_TYPE,
    DesktopCommand,
    DesktopProtocolError,
    DesktopResponse,
    event_content,
)
from mindroom.matrix.olm_to_device import (
    PinnedMatrixDevice,
    authenticated_sender_matches,
    send_encrypted_to_device,
)
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

if TYPE_CHECKING:
    import nio


class DesktopRequestError(RuntimeError):
    """One remote desktop request failed before producing a valid result."""


@dataclass(frozen=True, slots=True)
class _PendingDesktopResponse:
    target: PinnedMatrixDevice
    session_id: str
    future: asyncio.Future[DesktopResponse]


class DesktopResponseRouter:
    """Correlate authenticated desktop responses arriving on one live Matrix client."""

    def __init__(self, client: nio.AsyncClient) -> None:
        self._client = client
        self._pending: dict[str, _PendingDesktopResponse] = {}
        client.add_to_device_callback(self.on_to_device_event, AuthenticatedToDeviceEvent)

    async def request(
        self,
        target: PinnedMatrixDevice,
        command: DesktopCommand,
        *,
        timeout_seconds: float,
    ) -> DesktopResponse:
        """Send one command and wait for its exact pinned response."""
        if command.request_id in self._pending:
            msg = f"Desktop request ID is already pending: {command.request_id}."
            raise DesktopRequestError(msg)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DesktopResponse] = loop.create_future()
        self._pending[command.request_id] = _PendingDesktopResponse(
            target=target,
            session_id=command.session_id,
            future=future,
        )
        try:
            await send_encrypted_to_device(
                self._client,
                target,
                event_type=DESKTOP_COMMAND_EVENT_TYPE,
                content=command.to_content(),
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout_seconds)
            except TimeoutError as exc:
                msg = f"Desktop device did not answer within {timeout_seconds:g} seconds."
                raise DesktopRequestError(msg) from exc
        finally:
            self._pending.pop(command.request_id, None)

    def on_to_device_event(self, event: nio.ToDeviceEvent) -> None:
        """Resolve a waiter only for a valid response from its exact pinned device."""
        if not isinstance(event, AuthenticatedToDeviceEvent):
            return
        if event.type != DESKTOP_RESPONSE_EVENT_TYPE:
            return
        try:
            response = DesktopResponse.from_content(event_content(event.source))
        except DesktopProtocolError:
            return
        pending = self._pending.get(response.request_id)
        if pending is None or pending.future.done():
            return
        if response.session_id != pending.session_id:
            return
        if not authenticated_sender_matches(self._client, event, pending.target):
            return
        pending.future.set_result(response)


_ROUTERS: weakref.WeakKeyDictionary[nio.AsyncClient, DesktopResponseRouter] = weakref.WeakKeyDictionary()
_ROUTERS_LOCK = threading.Lock()


def desktop_response_router(client: nio.AsyncClient) -> DesktopResponseRouter:
    """Return the one callback router registered for this Matrix client."""
    with _ROUTERS_LOCK:
        router = _ROUTERS.get(client)
        if router is None:
            router = DesktopResponseRouter(client)
            _ROUTERS[client] = router
        return router


__all__ = ["DesktopRequestError", "DesktopResponseRouter", "desktop_response_router"]
