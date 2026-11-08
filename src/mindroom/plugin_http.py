"""Public helpers for plugin HTTP routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated

from fastapi import HTTPException, Path, Request, WebSocket, WebSocketDisconnect, status
from fastapi.requests import HTTPConnection  # noqa: TC002 - FastAPI resolves dependency annotations.

from mindroom.api.app_state import ensure_app_state
from mindroom.api.auth import is_administrator
from mindroom.api.client_sessions import (
    SESSION_PROTOCOL,
    allowed_client_origin,
    client_session_store,
    offered_session,
    presented_session,
)
from mindroom.api.config_lifecycle import bind_current_request_snapshot, require_api_state
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.room_power import ROOM_ADMIN_POWER_LEVEL, send_power_level, user_power_level
from mindroom.plugin_context import PluginContext, build_plugin_context
from mindroom.room_modes import change_room_thread_mode
from mindroom.room_reply_modes import (
    clear_room_reply_mode_override,
    resolve_room_reply_mode_override,
    set_room_reply_mode_override,
)
from mindroom.room_thread_modes import resolve_room_thread_mode_override
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
    from types import EllipsisType

    from mindroom.api.client_sessions import ClientSession
    from mindroom.bot import AgentBot
    from mindroom.config.agent import RoomReplyMode
    from mindroom.config.main import Config
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot
    from mindroom.room_thread_modes import RoomThreadMode

# Plugin routes import these from outside src/, so they stay public though no module here imports them.
__all__ = [
    "ClientUser",
    "RoomModes",
    "RoomPower",
    "RoomReader",
    "client_rooms",
    "client_stream",
    "plugin_context",
    "require_client_user",
    "require_platform_administrator",
    "require_room_member",
    "require_room_reader",
]

# A stream in a room rechecks its user's membership this long after each check.
_MEMBERSHIP_RECHECK_SECONDS = 60.0
# The websocket counterpart of HTTP 401, from the private-use range: the client signs in again.
_SESSION_ENDED_CLOSE_CODE = 4401
_ROUTER_NOT_RUNNING = "The router is not running."


@dataclass(frozen=True)
class ClientUser:
    """The Matrix user a client session belongs to."""

    user_id: str


async def require_client_user(request: Request) -> ClientUser:
    """Resolve ``Authorization: Bearer <session_token>`` to its user, or answer 401."""
    return ClientUser(presented_session(request).user_id)


async def require_room_member(request: Request, room_id: Annotated[str, Path()]) -> ClientUser:
    """Return the client session's user once they are joined to ``room_id``, answering 401, 403 or 503 otherwise.

    The user is always the session's: as a dependency, FastAPI fills every parameter from the request, so one naming
    the user would let the caller choose it. For the same reason ``room_id`` comes only from the route's path: a route
    without a ``{room_id}`` answers 422 rather than check a room the query string names.
    """
    user = await require_client_user(request)
    await _require_joined(request, user.user_id, room_id)
    return user


@asynccontextmanager
async def client_stream(websocket: WebSocket, room_id: str | None = None) -> AsyncIterator[ClientUser]:
    """Sign a websocket's caller in with their client session, accept it, and yield their user while it stays signed in.

    The client offers the subprotocols ``mindroom-session`` and ``mindroom-session.<session_token>``, from an origin
    ``MINDROOM_COMPUTER_ALLOWED_ORIGINS`` lists, and given ``room_id`` its user must be joined to that room. Before
    accepting, a refusal raises ``HTTPException``, which the handshake answers: 401 for no live session, 403 for
    another origin or a non-member, 503 when membership is unavailable. Once accepted with ``mindroom-session``, the
    block is cancelled where it waits and the websocket closed with 4401 when the session expires or is revoked, and
    given ``room_id``, with 1008 when a recheck finds the user gone or 1013 when one fails; code after the block runs.
    """
    if not allowed_client_origin(websocket):
        raise HTTPException(403, "Origin is not allowed.")
    session = offered_session(websocket)
    if room_id is not None:
        await _require_joined(websocket, session.user_id, room_id)
        # The session may have ended while membership was read.
        offered_session(websocket)
    await websocket.accept(subprotocol=SESSION_PROTOCOL)
    stream = asyncio.timeout(None)

    async def watch() -> tuple[int, str]:
        end = await _stream_end(websocket, session, room_id)
        # Cancels the block where it waits.
        stream.reschedule(asyncio.get_running_loop().time())
        return end

    watcher = asyncio.create_task(watch())
    try:
        async with stream:
            yield ClientUser(session.user_id)
    except TimeoutError:
        if not stream.expired():
            raise
        # The client may be gone already.
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(*watcher.result())
    finally:
        watcher.cancel()


async def _stream_end(websocket: WebSocket, session: ClientSession, room_id: str | None) -> tuple[int, str]:
    """Wait until the session ends or, given ``room_id``, a membership recheck fails; return the close code and reason."""
    ends = [asyncio.create_task(_session_end(websocket, session))]
    if room_id is not None:
        ends.append(asyncio.create_task(_membership_end(websocket, session.user_id, room_id)))
    try:
        # A revocation must not wait for a membership recheck that is still running.
        done, _ = await asyncio.wait(ends, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in ends:
            task.cancel()
    return done.pop().result()


async def _session_end(websocket: WebSocket, session: ClientSession) -> tuple[int, str]:
    store = client_session_store(websocket.app)
    while store.lookup(session.token) is not None:
        with suppress(TimeoutError):
            async with asyncio.timeout(session.expires_at - store.clock()):
                await session.revoked.wait()
    return _SESSION_ENDED_CLOSE_CODE, "Client session ended; sign in again."


async def _membership_end(websocket: WebSocket, user_id: str, room_id: str) -> tuple[int, str]:
    while True:
        await asyncio.sleep(_MEMBERSHIP_RECHECK_SECONDS)
        try:
            await _require_joined(websocket, user_id, room_id)
        except HTTPException as error:
            gone = error.status_code == status.HTTP_403_FORBIDDEN
            return status.WS_1008_POLICY_VIOLATION if gone else status.WS_1013_TRY_AGAIN_LATER, error.detail


@dataclass(frozen=True)
class RoomPower:
    """Who may do what in a room, by one read of its power levels."""

    power_levels: Mapping[str, object]
    """The content of the room's ``m.room.power_levels``, empty when it has none."""
    creators: frozenset[str]
    """The users a room version 12 room's creation gives unlimited power, empty in earlier versions."""
    encrypted: bool
    """Whether the room is encrypted, so that its messages go out as ``m.room.encrypted``."""

    def level(self, user_id: str) -> float:
        """Return the user's power level: ``math.inf`` for a creator, else their listed level, else ``users_default``."""
        return user_power_level(self.power_levels, user_id, self.creators)

    def is_admin(self, user_id: str) -> bool:
        """Return whether the user holds room admin power: level 100 or more, or a creator."""
        return self.level(user_id) >= ROOM_ADMIN_POWER_LEVEL

    def may_send(self, user_id: str, event_type: str) -> bool:
        """Return whether the user's level lets them send message events, never state events, of ``event_type``.

        That takes the type's level in ``events``, else ``events_default``; in an encrypted room the message goes out
        as ``m.room.encrypted``, so that type's level must allow it too.
        """
        sent_as = {event_type, "m.room.encrypted"} if self.encrypted else {event_type}
        return all(self.level(user_id) >= send_power_level(self.power_levels, sent) for sent in sent_as)


@dataclass(frozen=True)
class RoomModes:
    """A room's thread and reply mode overrides, and the reply mode it uses."""

    thread: RoomThreadMode | None
    """The room's thread mode override, None while each agent uses its configured mode."""
    reply: RoomReplyMode | None
    """The room's reply mode override, None while the room uses its configured one."""
    reply_in_effect: RoomReplyMode
    """The reply mode the room uses: its override, else its ``rooms`` entry's, else ``auto``."""


@dataclass(frozen=True)
class RoomReader:
    """What a route may read about one room, once its caller is found joined to it."""

    user: ClientUser
    """The session's user, joined to the room."""
    members: Mapping[str, str | None]
    """Each joined user's display name in the room, None for one without; read by the check that proved membership."""
    _room_state: Callable[[], Awaitable[RoomStateSnapshot]] = field(repr=False)
    _room_direct: Callable[[], Awaitable[bool]] = field(repr=False)
    _room_id: str = field(repr=False)
    _router: Callable[[], AgentBot | None] = field(repr=False)

    async def power(self) -> RoomPower:
        """Read the room's power levels now, answering 503 when they cannot be read."""
        state = await self._room_state()
        return RoomPower(
            state.events.get(("m.room.power_levels", ""), {}),
            state.creators,
            ("m.room.encryption", "") in state.events,
        )

    async def is_direct(self) -> bool:
        """Return whether MindRoom's agents treat the room as a direct message; a hint for display, never a grant.

        It is their own answer, which they keep for a few minutes and which counts a room as not direct when the
        homeserver answers the lookup with an error; it answers 503 when the lookup raises or times out.
        """
        return await self._room_direct()

    def modes(self) -> RoomModes:
        """Return the room's thread and reply mode overrides and the reply mode it uses, answering 503 without a router."""
        router = self._router()
        if router is None:
            raise HTTPException(503, _ROUTER_NOT_RUNNING)
        paths = router.runtime_paths
        return RoomModes(
            resolve_room_thread_mode_override(paths, self._room_id),
            resolve_room_reply_mode_override(paths, self._room_id),
            router.config.get_room_reply_mode(paths, self._room_id),
        )

    async def set_modes(
        self,
        *,
        thread: RoomThreadMode | None | EllipsisType = ...,
        reply: RoomReplyMode | None | EllipsisType = ...,
    ) -> None:
        """Change the room's overrides as ``!thread_mode`` and ``!reply_mode`` do: None clears one, one left out stays.

        Before anything is written it answers 403 unless the caller holds room admin power by a ``power()`` read, 503
        when that read fails, and 503 while the router, which republishes the thread mode, is not running.
        """
        if not (await self.power()).is_admin(self.user.user_id):
            raise HTTPException(403, "Only room admins change the room's modes.")
        router = self._router()
        if router is None or not router.running or router.client is None:
            raise HTTPException(503, _ROUTER_NOT_RUNNING)
        if reply is None:
            clear_room_reply_mode_override(router.runtime_paths, self._room_id)
        elif reply is not ...:
            set_room_reply_mode_override(router.runtime_paths, room_id=self._room_id, mode=reply)
        if thread is not ...:
            await change_room_thread_mode(router.client, router.config, router.runtime_paths, self._room_id, thread)


async def require_room_reader(request: Request, room_id: Annotated[str, Path()]) -> RoomReader:
    """Return what a route may read about ``room_id``, once the client session's user is joined to it.

    It checks the caller exactly as ``require_room_member`` does, answering 401, 403 or 503, before it reads anything
    else of the room.
    """
    user = await require_client_user(request)
    members = await _require_joined(request, user.user_id, room_id)
    state = ensure_app_state(request.app)
    return RoomReader(
        user,
        members,
        partial(_bound(state.room_state), room_id),
        partial(_bound(state.room_direct), room_id),
        room_id,
        lambda: state.runtime.agent_bot(ROUTER_AGENT_NAME),
    )


async def client_rooms(request: Request, candidates: Iterable[str]) -> list[str]:
    """Return those of ``candidates`` the client session's user is joined to now, in their order.

    Each is checked as ``require_room_member`` checks one, all at once, and a room whose check fails is left out. It
    answers 401 without a live session, and 503 when membership is unavailable.
    """
    user = await require_client_user(request)
    room_members = _bound(ensure_app_state(request.app).room_members)
    rooms = list(candidates)
    answers = await asyncio.gather(*map(room_members, rooms), return_exceptions=True)
    return [
        room_id
        for room_id, members in zip(rooms, answers, strict=True)
        if not isinstance(members, BaseException) and user.user_id in members
    ]


def _bound[T](reader: T | None) -> T:
    """Return a room reader `mindroom run` bound, or answer 503 while the API is served without MindRoom's bots."""
    if reader is None:
        raise HTTPException(503, "Room membership is unavailable.")
    return reader


async def _require_joined(connection: HTTPConnection, user_id: str, room_id: str) -> Mapping[str, str | None]:
    """Return the room's joined users with their display names, once ``user_id`` is one of them."""
    members = await _bound(ensure_app_state(connection.app).room_members)(room_id)
    if user_id not in members:
        raise HTTPException(403, "Not a member of this room.")
    return members


async def require_platform_administrator(request: Request) -> ClientUser:
    """Return the client session's user once they administer the config the API serves now, else answer 401 or 403."""
    user = await require_client_user(request)
    if not is_administrator(user.user_id, bind_current_request_snapshot(request)):
        raise HTTPException(403, "Not a platform administrator.")
    return user


def plugin_context(connection: HTTPConnection) -> PluginContext:
    """Return the context of the plugin whose route serves ``connection``, under the config the API serves now.

    Called again, as by a websocket after a config reload, it returns the newer config's context. It raises
    ``PluginInactiveError``, which the route answers with 503, when that config no longer loads the plugin.
    """
    plugin_name = connection.scope.get(PLUGIN_NAME_SCOPE_KEY)
    if plugin_name is None:
        msg = "plugin_context() serves only a plugin's own routes"
        raise RuntimeError(msg)
    state = require_api_state(connection.app)
    snapshot = state.snapshot

    def current_config() -> Config | None:
        latest = state.snapshot
        return latest.runtime_config if latest.runtime_paths == snapshot.runtime_paths else None

    return build_plugin_context(plugin_name, snapshot.runtime_config, snapshot.runtime_paths, current_config)
