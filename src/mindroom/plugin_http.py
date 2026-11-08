"""Public helpers for plugin HTTP routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any

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
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.entity_directory import entity_directory
from mindroom.hooks.types import format_hook_source
from mindroom.matrix.reserved_content import MINDROOM_RESERVED_PREFIXES, reserved_content_keys
from mindroom.matrix.room_power import ROOM_ADMIN_POWER_LEVEL, send_power_level, user_power_level
from mindroom.plugin_context import PluginContext, build_plugin_context
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY, config_plugins

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

    from mindroom.api.client_sessions import ClientSession
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot

# Plugin routes import these from outside src/, so they stay public though no module here imports them.
__all__ = [
    "ClientUser",
    "RoomMessenger",
    "RoomPower",
    "RoomReader",
    "client_rooms",
    "client_stream",
    "plugin_context",
    "require_client_user",
    "require_platform_administrator",
    "require_room_member",
    "require_room_messenger",
    "require_room_reader",
]

# A stream in a room rechecks its user's membership this long after each check.
_MEMBERSHIP_RECHECK_SECONDS = 60.0
# The websocket counterpart of HTTP 401, from the private-use range: the client signs in again.
_SESSION_ENDED_CLOSE_CODE = 4401


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
class RoomReader:
    """What a route may read about one room, once its caller is found joined to it."""

    user: ClientUser
    """The session's user, joined to the room."""
    members: Mapping[str, str | None]
    """Each joined user's display name in the room, None for one without; read by the check that proved membership."""
    _room_state: Callable[[], Awaitable[RoomStateSnapshot]] = field(repr=False)
    _room_direct: Callable[[], Awaitable[bool]] = field(repr=False)

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
    )


# Matrix's own content keys are those without a namespace, such as `body` and `msgtype`, and those under these.
_MATRIX_KEY_PREFIXES = ("m.", "org.matrix.")


@dataclass(frozen=True)
class RoomMessenger:
    """Posts to one room as the router for a route whose caller is joined to it, never from or as any person's account.

    Each message carries its plugin's name as its hook source.
    """

    user: ClientUser
    """The session's user, joined to the room."""
    _request: Request = field(repr=False)
    _room_id: str = field(repr=False)
    _plugin_name: str = field(repr=False)

    async def notice(
        self,
        text: str,
        *,
        thread_id: str | None = None,
        content: Mapping[str, object] | None = None,
    ) -> str:
        """Post ``text`` as the router's ``m.notice``, which mentions no one and wakes no agent; return its event ID.

        It checks no power level: the route decides who may have the router post. ``content`` adds namespaced keys of
        the plugin's own, and it answers 400 before anything is sent when ``content`` sets one of Matrix's own keys,
        such as ``body`` or ``m.relates_to``, or a key under ``com.mindroom.``, ``io.mindroom.`` or another loaded
        plugin's reserved namespaces at any depth. It answers 503 when the router is not running or not in the room,
        or when the send fails.
        """
        router = self._router()
        extra = self._content(router, content)
        extra["msgtype"] = "m.notice"
        return await self._send(router, text, thread_id, extra, ())

    async def relay(
        self,
        text: str,
        *,
        mentions: Sequence[str],
        thread_id: str | None = None,
        content: Mapping[str, object] | None = None,
    ) -> str:
        """Post ``text`` as the router for the caller, waking exactly the agents and teams ``mentions`` names.

        It names the caller as the message's original sender, so each agent it wakes answers and authorizes them, and
        its text says it is sent for them. ``mentions`` must name at least one agent or team, and only agents and teams
        joined to the room, else it answers 400. Just before sending it checks again that the session is live (401),
        that the caller is joined (403) and may send ``m.room.message`` (403), answering 503 when either cannot be read.
        ``content`` and the other answers are as for ``notice()``. It returns the message's event ID.
        """
        if not mentions:
            raise HTTPException(400, "A relay mentions at least one agent or team.")
        room = await require_room_reader(self._request, self._room_id)
        if not (await room.power()).may_send(room.user.user_id, "m.room.message"):
            raise HTTPException(403, "Not allowed to send messages in this room.")
        # The session may have ended while the room was read.
        await require_client_user(self._request)
        router = self._router()
        extra = self._content(router, content)
        directory = entity_directory(router.config, router.runtime_paths)
        for mentioned in mentions:
            entity = directory.get(mentioned)
            if entity is None or entity.kind == "router" or mentioned not in room.members:
                raise HTTPException(400, f"A relay mentions only agents and teams in this room, not {mentioned}.")
        extra[ORIGINAL_SENDER_KEY] = room.user.user_id
        return await self._send(router, f"Sent for {room.user.user_id}: {text}", thread_id, extra, mentions)

    def _router(self) -> AgentBot:
        """Return the router bot running now, so a restarted router posts with its new client, once it is in the room."""
        embedded = ensure_app_state(self._request.app).embedded
        router = None if embedded is None else embedded.agent_bot(ROUTER_AGENT_NAME)
        client = None if router is None or not router.running else router.client
        if router is None or client is None:
            raise HTTPException(503, "MindRoom's router is not running; try again.")
        if self._room_id not in client.rooms:
            raise HTTPException(503, "MindRoom's router is not in this room.")
        return router

    def _content(self, router: AgentBot, content: Mapping[str, object] | None) -> dict[str, Any]:
        """Return a copy of ``content``, answering 400 unless it holds only namespaced keys of the plugin's own.

        A Matrix key such as ``body``, ``m.relates_to`` or ``m.new_content`` would change what the message says, where
        it goes, or which message it edits; a MindRoom key would claim metadata such as its requester, and another
        plugin's key a card that plugin's clients trust.
        """
        extra = dict(content or {})
        if matrix_keys := sorted(key for key in extra if "." not in key or key.startswith(_MATRIX_KEY_PREFIXES)):
            raise HTTPException(400, f"content must not set Matrix's own keys ({', '.join(matrix_keys)}).")
        plugins = config_plugins(router.config, router.runtime_paths).loaded_plugins
        others = (
            namespace
            for plugin in plugins
            if plugin.name != self._plugin_name
            for namespace in plugin.reserved_namespaces
        )
        if reserved := reserved_content_keys(extra, (*MINDROOM_RESERVED_PREFIXES, *others)):
            msg = f"content must not set keys MindRoom or another plugin reserves ({', '.join(reserved)})."
            raise HTTPException(400, msg)
        return extra

    async def _send(
        self,
        router: AgentBot,
        text: str,
        thread_id: str | None,
        extra: dict[str, Any],
        mentions: Sequence[str],
    ) -> str:
        # One hop into the synthetic chain, as a hook's own send outside a message is, so hooks reacting to it cannot loop.
        extra[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] = 1
        event_id = await router.hook_context.send_message(
            self._room_id,
            text,
            thread_id,
            format_hook_source(self._plugin_name, "http"),
            extra,
            mentions=list(dict.fromkeys(mentions)),
        )
        if event_id is None:
            raise HTTPException(503, "The message could not be sent; try again.")
        return event_id


async def require_room_messenger(request: Request, room_id: Annotated[str, Path()]) -> RoomMessenger:
    """Return a ``RoomMessenger`` for ``room_id``, once the client session's user is joined to it.

    It checks the caller exactly as ``require_room_member`` does, answering 401, 403 or 503, and serves only a
    plugin's own routes.
    """
    plugin_name = _route_plugin_name(request, "require_room_messenger")
    user = await require_room_member(request, room_id)
    return RoomMessenger(user, request, room_id, plugin_name)


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
    plugin_name = _route_plugin_name(connection, "plugin_context")
    state = require_api_state(connection.app)
    snapshot = state.snapshot

    def current_config() -> Config | None:
        latest = state.snapshot
        return latest.runtime_config if latest.runtime_paths == snapshot.runtime_paths else None

    return build_plugin_context(plugin_name, snapshot.runtime_config, snapshot.runtime_paths, current_config)


def _route_plugin_name(connection: HTTPConnection, helper: str) -> str:
    """Return the name of the plugin whose route serves ``connection``; ``helper`` serves no other route."""
    plugin_name = connection.scope.get(PLUGIN_NAME_SCOPE_KEY)
    if plugin_name is None:
        msg = f"{helper}() serves only a plugin's own routes"
        raise RuntimeError(msg)
    return plugin_name
