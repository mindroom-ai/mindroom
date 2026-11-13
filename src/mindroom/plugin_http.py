"""Public helpers that plugin HTTP routes and plugin tools import."""

from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated, overload

from fastapi import HTTPException, Path, Request, WebSocket, WebSocketDisconnect, status
from fastapi.requests import HTTPConnection  # noqa: TC002 - FastAPI resolves dependency annotations.

from mindroom.api import plugin_origins
from mindroom.api.app_state import ensure_app_state
from mindroom.api.auth import is_administrator
from mindroom.api.client_sessions import (
    SESSION_PROTOCOL,
    allowed_client_origin,
    client_session_store,
    offered_session,
    presented_session,
)
from mindroom.api.config_lifecycle import (
    bind_current_request_snapshot,
    rebind_current_request_snapshot,
    require_api_state,
)
from mindroom.api.connection_agents import build_connection_agent_target
from mindroom.api.plugin_routes import published_plugins
from mindroom.authorization import is_sender_allowed_for_agent_oauth_connection, is_sender_allowed_for_responder
from mindroom.config.agent import RoomReplyMode  # noqa: TC001 - Pydantic resolves RoomModes when a route returns it.
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.entity_directory import entity_directory
from mindroom.hooks.types import format_hook_source
from mindroom.matrix.history_visibility import RoomHistory
from mindroom.matrix.reserved_content import MINDROOM_RESERVED_PREFIXES, reserved_content_keys
from mindroom.matrix.room_power import ROOM_ADMIN_POWER_LEVEL, send_power_level, user_power_level
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.service import current_oauth_access_token
from mindroom.origins import allowed_client_origins
from mindroom.plugin_context import PluginContext, build_plugin_context
from mindroom.plugin_worker_relay import WorkerRelay, open_worker_relay
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.room_agent_access import (
    AgentWorkspace,
    decide_with_room_grants,
    room_agent_workspaces,
    room_execution_identity,
)
from mindroom.room_modes import change_room_thread_mode
from mindroom.room_reply_modes import (
    clear_room_reply_mode_override,
    resolve_room_reply_mode_override,
    set_room_reply_mode_override,
)
from mindroom.room_thread_modes import (
    RoomThreadMode,
    resolve_room_thread_mode_override,
)
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.terminal.client import WorkerTerminals
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalError
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY, config_plugins

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
    from types import EllipsisType

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.api.client_sessions import ClientSession
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

# Plugin routes import these from outside src/, so they stay public though no module here imports them.
__all__ = [
    "AgentWorkspace",
    "ClientUser",
    "RoomHistory",
    "RoomMessenger",
    "RoomModes",
    "RoomPower",
    "RoomReader",
    "TerminalError",
    "WorkerRelay",
    "WorkerTerminals",
    "client_origins",
    "client_rooms",
    "client_stream",
    "client_user_oauth_access_token",
    "plugin_context",
    "plugin_origin_url",
    "require_client_user",
    "require_platform_administrator",
    "require_room_member",
    "require_room_messenger",
    "require_room_reader",
    "stream_room_reader",
    "worker_relay",
]

# A stream in a room rechecks its user's membership this long after each check.
_MEMBERSHIP_RECHECK_SECONDS = 60.0
# Each stream holds a membership recheck and whatever its route attaches, such as a terminal or a workspace watcher;
# a client opens a few streams for each room it shows, so a user rarely needs more than this many at once.
_STREAMS_PER_USER = 16
# Each user's open streams in this process, across every route on client_stream.
_open_streams: Counter[str] = Counter()
# The websocket counterpart of HTTP 401, from the private-use range: the client signs in again.
_SESSION_ENDED_CLOSE_CODE = 4401
_ROUTER_NOT_RUNNING = "The router is not running in this room."
_NOT_A_MEMBER = "Not a member of this room."


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
async def client_stream(
    websocket: WebSocket,
    room_id: str | None = None,
    *,
    admit: Callable[[ClientUser], bool | Awaitable[bool]] | None = None,
) -> AsyncIterator[ClientUser]:
    """Sign a websocket's caller in with their client session, accept it, and yield their user while it stays signed in.

    The client offers the subprotocols ``mindroom-session`` and ``mindroom-session.<session_token>``, from an origin
    ``MINDROOM_COMPUTER_ALLOWED_ORIGINS`` lists, and given ``room_id`` its user must be joined to that room. Given
    ``admit``, the route's own rule, sync or async, is then asked whether to let the user in. Before accepting, a
    refusal raises ``HTTPException``, which the handshake answers: 401 for no live session, 403 for another origin or a
    non-member, 503 when membership is unavailable, ``admit`` returns false, or the user already holds 16 open streams
    across every route on ``client_stream``. Once accepted with ``mindroom-session``, the block is cancelled where it
    waits and the websocket closed with 4401 when the session expires or is revoked, and given ``room_id``, with 1008
    when a recheck finds the user gone or 1013 when one fails; code after the block runs.
    """
    if not allowed_client_origin(websocket):
        raise HTTPException(403, "Origin is not allowed.")
    session = offered_session(websocket)
    user = ClientUser(session.user_id)
    if room_id is not None:
        await _require_joined(websocket, session.user_id, room_id)
    if admit is not None:
        answer = admit(user)
        if inspect.isawaitable(answer):
            answer = await answer
        if not answer:
            raise HTTPException(503, "The route refused this stream; try again later.")
    # The session may have ended while membership was read or the route's check ran.
    offered_session(websocket)
    with _stream_slot(user.user_id):
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
                yield user
        except TimeoutError:
            if not stream.expired():
                raise
            # The client may be gone already.
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(*watcher.result())
        finally:
            watcher.cancel()


@contextmanager
def _stream_slot(user_id: str) -> Iterator[None]:
    """Hold one of ``user_id``'s open streams, answering 503 when they hold the limit already."""
    if _open_streams[user_id] >= _STREAMS_PER_USER:
        raise HTTPException(503, "This user has too many open streams; close one and try again.")
    _open_streams[user_id] += 1
    try:
        yield
    finally:
        _open_streams[user_id] -= 1
        if not _open_streams[user_id]:
            del _open_streams[user_id]


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
    """What a route may read about one room, and change as its admin, once its caller is found joined to it."""

    user: ClientUser
    """The session's user, joined to the room."""
    members: Mapping[str, str | None]
    """Each joined user's display name in the room, None for one without; read by the check that proved membership."""
    _room_state: Callable[[], Awaitable[RoomStateSnapshot]] = field(repr=False)
    _room_direct: Callable[[], Awaitable[bool]] = field(repr=False)
    _room_id: str = field(repr=False)
    _router: Callable[[], AgentBot | None] = field(repr=False)
    _room_history: Callable[[], Awaitable[RoomHistory | None]] = field(repr=False)
    _room_agents: Callable[[Callable[[], Awaitable[RoomPower]], str | None], Awaitable[list[AgentWorkspace]]] = field(
        repr=False,
    )
    _workspace_terminals: Callable[[AgentWorkspace, bool], Awaitable[WorkerTerminals | None]] = field(repr=False)

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

        It is their own answer, which they keep for a few minutes; it answers 503 when the bot's direct-message list
        or the room's state cannot be read and nothing else marks the room as direct, or when the lookup raises or
        times out.
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
        when that read fails, and 503 unless the router, which republishes the thread mode, runs and is in the room.
        A thread mode change the router cannot publish stays saved and answers 503.
        """
        if not (await self.power()).is_admin(self.user.user_id):
            raise HTTPException(403, "Only room admins change the room's modes.")
        router = self._router()
        if router is None or not router.running or router.client is None or self._room_id not in router.client.rooms:
            raise HTTPException(503, _ROUTER_NOT_RUNNING)
        if reply is None:
            clear_room_reply_mode_override(router.runtime_paths, self._room_id)
        elif reply is not ...:
            set_room_reply_mode_override(router.runtime_paths, room_id=self._room_id, mode=reply)
        if thread is not ... and not await change_room_thread_mode(
            router.client,
            router.config,
            router.runtime_paths,
            self._room_id,
            thread,
        ):
            raise HTTPException(
                503,
                "The modes were saved, but the room's thread mode is not published yet; try again.",
            )

    async def history(self) -> RoomHistory:
        """Read the room's history visibility and where the caller's view of its history begins under it.

        It reads the caller's membership again, answering 403 once they are no longer joined, and 503 when the read
        fails or times out.
        """
        history = await self._room_history()
        if history is None:
            raise HTTPException(403, _NOT_A_MEMBER)
        return history

    async def agents(self) -> list[AgentWorkspace]:
        """Return the agents joined to the room that the caller may use there, in config order, with their workspaces.

        It leaves out an agent whose contact room this is, and, logging why, one without its account, with unresolved
        grant rooms, or with unreadable contacts or stored tool settings. It answers 503 while the router's rooms are
        unknown and an agent's access rules wait on them, or while the runtime has not applied the API's membership
        policy.
        """
        return await self._room_agents(self.power, None)

    async def agent(self, agent_user_id: str) -> AgentWorkspace:
        """Return the agent ``agent_user_id`` names with its workspace once the caller may use it there.

        It answers as the Computer does: 403 for a user ID that is no current agent, an agent not joined to the room, or
        one whose access rules refuse the caller, and 503 while those cannot be resolved. It also answers 403 in the
        agent's contact room, and 503 while the runtime has not applied the API's membership policy.
        """
        (workspace,) = await self._room_agents(self.power, agent_user_id)
        return workspace

    async def terminals(self, workspace: AgentWorkspace, *, create: bool = False) -> WorkerTerminals | None:
        """Return the terminals of ``workspace``, which ``agents()`` or ``agent()`` returned, or None while it has none.

        They are those the agent's turns for the caller in this room run beside, which the chat's Terminal tab shows.
        It checks nothing more than ``agent()`` did: a route opens, closes, and types in them only while ``terminal``
        is ``type``. ``create`` makes the agent's workspace first, as a terminal it opens needs.
        """
        return await self._workspace_terminals(workspace, create)


async def require_room_reader(request: Request, room_id: Annotated[str, Path()]) -> RoomReader:
    """Return what a route may read and change about ``room_id``, once the client session's user is joined to it.

    It checks the caller exactly as ``require_room_member`` does, answering 401, 403 or 503, before it reads anything
    else of the room.
    """
    user = await require_client_user(request)
    return await _room_reader(request, user, room_id)


async def stream_room_reader(websocket: HTTPConnection, user: ClientUser, room_id: str) -> RoomReader:
    """Return what ``user`` may read and change about ``room_id`` now, for a route that established ``user`` itself.

    It is ``require_room_reader`` for a route that has no session's ``Authorization``: a ``client_stream`` block, whose
    websocket can't send one, or an origin route, passing its request as ``websocket``, whose own gate names its user.
    It reads the room's members again, answering 403 once ``user`` left and 503 when they can't be read, and answers
    ``agents()`` and ``agent()`` under the config the API serves now, or 409 when the API serves another runtime. A
    stream that keeps checking what its caller may do calls it again for each check.
    """
    rebind_current_request_snapshot(websocket)
    return await _room_reader(websocket, user, room_id)


async def _room_reader(connection: HTTPConnection, user: ClientUser, room_id: str) -> RoomReader:
    members = await _require_joined(connection, user.user_id, room_id)
    state = ensure_app_state(connection.app)
    return RoomReader(
        user,
        members,
        partial(_bound(state.room_state), room_id),
        partial(_bound(state.room_direct), room_id),
        room_id,
        lambda: state.runtime.agent_bot(ROUTER_AGENT_NAME),
        partial(_bound(state.room_history), room_id, user.user_id),
        partial(room_agent_workspaces, connection, user.user_id, room_id, members),
        partial(_workspace_terminals, connection, user.user_id, room_id),
    )


async def _workspace_terminals(
    connection: HTTPConnection,
    user_id: str,
    room_id: str,
    workspace: AgentWorkspace,
    create: bool,
) -> WorkerTerminals | None:
    if workspace.terminal == "none":
        return None
    paths = bind_current_request_snapshot(connection).runtime_paths
    identity = room_execution_identity(workspace.name, user_id, room_id, workspace.config, paths)
    return await asyncio.to_thread(agent_terminals, workspace.name, workspace.config, paths, identity, create=create)


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
        such as ``body`` or ``m.relates_to``, or a key under ``com.mindroom.``, ``io.mindroom.``, ``chat.mindroom.``
        or a namespace another loaded plugin reserves and this one does not, at any depth. It answers 503 when the router
        is not running or not in the room, or when the send fails.
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
        return await self._send(router, f"Sent for {room.user.user_id}:\n\n{text}", thread_id, extra, mentions)

    def _router(self) -> AgentBot:
        """Return the router bot running now, so a restarted router posts with its new client, once it is in the room."""
        embedded = ensure_app_state(self._request.app).embedded
        router = None if embedded is None else embedded.agent_bot(ROUTER_AGENT_NAME)
        if router is None or not router.running or (client := router.client) is None:
            raise HTTPException(503, "MindRoom's router is not running; try again.")
        if self._room_id not in client.rooms:
            raise HTTPException(503, "MindRoom's router is not in this room.")
        return router

    def _content(self, router: AgentBot, content: Mapping[str, object] | None) -> dict[str, object]:
        """Return a copy of ``content``, answering 400 unless its keys are namespaced and none is reserved for another.

        A Matrix key such as ``body``, ``m.relates_to`` or ``m.new_content`` would change what the message says, where
        it goes, or which message it edits; a MindRoom key would claim metadata such as its requester, and another
        plugin's key a card that plugin's clients trust.
        """
        extra = dict(content or {})
        if matrix_keys := sorted(key for key in extra if "." not in key or key.startswith(_MATRIX_KEY_PREFIXES)):
            raise HTTPException(400, f"content must not set Matrix's own keys ({', '.join(matrix_keys)}).")
        plugins = config_plugins(router.config, router.runtime_paths)
        # A namespace this plugin reserves is its own even when another plugin reserves it too, as plugins of one
        # family sharing a namespace do; a narrower one another plugin reserves inside it stays that plugin's.
        own = {
            namespace
            for plugin in plugins.loaded_plugins
            if plugin.name == self._plugin_name
            for namespace in plugin.reserved_namespaces
        }
        others = (namespace for namespace in plugins.reserved_namespaces if namespace not in own)
        if reserved := reserved_content_keys(extra, (*MINDROOM_RESERVED_PREFIXES, *others)):
            msg = f"content must not set keys MindRoom or another plugin reserves ({', '.join(reserved)})."
            raise HTTPException(400, msg)
        return extra

    async def _send(
        self,
        router: AgentBot,
        text: str,
        thread_id: str | None,
        extra: dict[str, object],
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
        raise HTTPException(403, _NOT_A_MEMBER)
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


async def client_user_oauth_access_token(
    request: Request,
    user: ClientUser,
    provider_id: str,
    agent_name: str,
) -> str:
    """Return a current access token of ``user``'s own connection to ``provider_id``, as ``agent_name``'s tools use it for them.

    ``user`` is the ``ClientUser`` a dependency such as ``require_client_user`` or ``require_room_member`` resolved for
    the request; a route never builds one from request data, which would let its caller name someone else.

    It decides in this order, and reads nothing an earlier refusal makes needless. It raises ``LookupError`` for a
    provider or agent the config does not have. It raises ``PermissionError`` unless the agent's ``worker_scope`` is
    ``user`` or ``user_agent``, which keeps a connection for each person alone: a ``shared`` or unscoped agent's
    connection, like a service account, serves everyone who uses the agent. It raises ``PermissionError`` unless
    ``user`` may use the agent and connect its accounts, as the Connections portal and the agent's connect links
    decide; the runtime's room grants are read from the rooms first only when that answer depends on grants not
    resolved under the served config, and then it answers 503 as ``RoomReader.agents()`` does while they cannot be:
    the router is not running, the runtime has not applied the served config's room grants, or a room cannot be read.
    Only then does it open ``user``'s own credential store, refreshing a token about to expire first, as the agent's
    tools refresh it. It raises ``OAuthConnectionRequired`` while ``user`` has no usable connection, whose
    ``connect_url`` signs them in for this agent, or is ``None`` while the stored connection cannot be read and must
    first be reset on the dashboard, and ``OAuthProviderError`` when a token refresh fails for another reason, which
    a retry may fix.
    """
    context = plugin_context(request)
    config, paths = context.config, context.runtime_paths
    provider = config_plugins(config, paths).oauth_providers.get(provider_id)
    if provider is None:
        msg = f"No OAuth provider {provider_id!r} is configured"
        raise LookupError(msg)
    if agent_name not in config.agents:
        msg = f"No agent {agent_name!r} is configured"
        raise LookupError(msg)
    requester_id = resolve_human_requester_alias(user.user_id, config, paths)
    target = build_connection_agent_target(config, paths, requester_id, agent_name)
    identity = target.execution_identity
    if target.worker_scope not in {"user", "user_agent"} or identity is None or identity.requester_id != requester_id:
        msg = f"Agent {agent_name!r} keeps no connection for one person alone"
        raise PermissionError(msg)

    def may_use(memberships: AgentReplyMembershipIndex) -> bool:
        # The portal lists only agents the requester may converse with, and a connect link completes only for a
        # requester who may connect the agent's accounts; the token is the connection both would serve. The connect
        # check comes first, so a refusal no room grant could change, such as a bot account's, is given without one.
        return is_sender_allowed_for_agent_oauth_connection(
            requester_id,
            agent_name,
            config,
            paths,
            memberships,
            requester_owned=True,
            require_resolved_membership=True,
        ) and is_sender_allowed_for_responder(
            requester_id,
            agent_name,
            None,
            config,
            paths,
            memberships,
            require_resolved_membership=True,
        )

    if not await decide_with_room_grants(request, config, may_use):
        msg = f"The caller may not use agent {agent_name!r}"
        raise PermissionError(msg)
    credential_context = resolve_oauth_credential_context(
        provider,
        paths,
        get_runtime_credentials_manager(paths),
        target,
        config=config,
    )

    def refresh_failed(error: OAuthProviderError) -> OAuthProviderError:
        del error  # The credential lifecycle logged it, and its cause may quote the provider.
        return OAuthProviderError(f"{provider.display_name} authorization could not be refreshed; try again shortly.")

    token, _ = await asyncio.to_thread(current_oauth_access_token, credential_context, refresh_failed)
    return token


async def worker_relay(connection: HTTPConnection | PluginContext, target: ResolvedWorkerTarget) -> WorkerRelay:
    """Return the relay into the dedicated worker ``target`` names, for the plugin a route or tool calls it for.

    ``connection`` is the connection a plugin's route serves, or a tool's ``current_plugin_context``. ``target`` is an
    ``AgentWorkspace``'s ``worker_target``, which ``RoomReader.agent()`` and ``agents()`` return once the caller may use
    the agent, or the calling tool's own ``resolve_worker_target()``. By name, the relay reaches only that plugin's own
    worker services. It never starts the worker: it answers 503 while the worker is not running, and without a
    dedicated worker backend.
    """
    if isinstance(connection, PluginContext):
        plugin_name, paths = connection.plugin_name, connection.runtime_paths
    else:
        plugin_name = _route_plugin_name(connection, "worker_relay")
        paths = require_api_state(connection.app).snapshot.runtime_paths
    return await open_worker_relay(paths, target, plugin_name)


@overload
def plugin_origin_url(connection: HTTPConnection, label: str) -> str: ...


@overload
def plugin_origin_url(connection: PluginContext, label: str) -> str | None: ...


def plugin_origin_url(connection: HTTPConnection | PluginContext, label: str) -> str | None:
    """Return the public URL of host ``label`` of the plugin a route or tool calls it for.

    ``connection`` is the connection a plugin's route serves, or a tool's ``current_plugin_context``. ``label`` is one
    DNS label the plugin picks, else ``ValueError``. While plugin origins are off, a route's call answers 503 and a
    tool's returns None. It raises ``RuntimeError`` for a plugin without an ``origin_module``, whose hosts answer only
    404.
    """
    if isinstance(connection, PluginContext):
        plugin_name = connection.plugin_name
        plugins = config_plugins(connection.config, connection.runtime_paths)
    else:
        plugin_name = _route_plugin_name(connection, "plugin_origin_url")
        plugins = published_plugins(connection.app)
    # A staged load sets one of these exactly when the manifest names an origin module.
    if plugins is None or not any(
        plugin.name == plugin_name and (plugin.origin_app is not None or plugin.origin_error is not None)
        for plugin in plugins.loaded_plugins
    ):
        msg = f"Plugin {plugin_name!r} has no origin_module, so plugin_origin_url() has no host of it to name"
        raise RuntimeError(msg)
    url = plugin_origins.plugin_origin_url(plugins.runtime_paths, plugin_name, label)
    if url is None and not isinstance(connection, PluginContext):
        raise HTTPException(503, "Plugin origins are off.")
    return url


def client_origins(connection: HTTPConnection) -> tuple[str, ...]:
    """Return the chat's origins, those ``MINDROOM_COMPUTER_ALLOWED_ORIGINS`` lists, under the config served now."""
    paths = require_api_state(connection.app).snapshot.runtime_paths
    return allowed_client_origins(paths, COMPUTER_ALLOWED_ORIGINS_ENV)


def _route_plugin_name(connection: HTTPConnection, helper: str) -> str:
    """Return the name of the plugin whose route serves ``connection``; ``helper`` serves no other route."""
    plugin_name = connection.scope.get(PLUGIN_NAME_SCOPE_KEY)
    if plugin_name is None:
        msg = f"{helper}() serves only a plugin's own routes"
        raise RuntimeError(msg)
    return plugin_name
