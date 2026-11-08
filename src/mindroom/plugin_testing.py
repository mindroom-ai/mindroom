"""Load a plugin in its own tests and drive its routes, tools and hooks as a running MindRoom does.

An installed MindRoom ships none of its own test helpers, so a plugin's test suite uses this module instead. It patches
nothing: client sessions, room membership and the agents' access decide as they do live, from the config a test gives.
MindRoom itself never imports this module.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from itertools import count
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn, Self, cast
from uuid import uuid4

import nio
from anyio.from_thread import start_blocking_portal
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import SESSION_PROTOCOL, client_session_store
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState
from mindroom.api.connection_agents import AgentToolContext
from mindroom.api.plugin_routes import mount_plugin_routes
from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.entity_resolution import current_entity_id
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    EVENT_MESSAGE_RECEIVED,
    EVENT_SYSTEM_ENRICH,
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    FinalResponseDraft,
    FinalResponseTransformContext,
    HookBindings,
    HookRegistryState,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ResponseDraft,
    ResponseResult,
    SystemEnrichContext,
    build_hook_room_state_querier,
    emit,
    emit_collect,
    emit_final_response_transform,
    emit_transform,
    validate_event_name,
)
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.rooms import is_dm_room
from mindroom.matrix.state import MatrixState, update_matrix_state
from mindroom.matrix_identifiers import agent_username_localpart
from mindroom.mcp_gateway.toolkits import run_toolkit_operation
from mindroom.mcp_gateway.tools import may_need_approval
from mindroom.message_target import MessageTarget
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.invocation import CallResult, ToolCallContext, run_tool_call
from mindroom.tool_system.plugins import config_plugins, dispose_plugins, prepare_plugins, publish_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, emit_custom_event, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from mindroom.turn_origin import classify_turn_origin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from anyio.abc import BlockingPortal
    from starlette.testclient import WebSocketTestSession

    from mindroom.api.app_state import EmbeddedRuntime
    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookContext
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.matrix.relation_lookup import RelationLookup
    from mindroom.tool_system.agno_toolkits import AnyToolkit

__all__ = ["LoadedPlugin", "Room", "SentMessage", "TurnRefused", "load_plugin"]

# The origin a plugin's websocket clients connect from; `load_plugin` allows it.
_CLIENT_ORIGIN = "http://localhost"

# Each message event a turn emits: its context, and for a response event the context field and type that hold the envelope.
_TURN_EVENTS: dict[str, tuple[Callable[..., HookContext], tuple[str, Callable[..., object]] | None]] = {
    EVENT_MESSAGE_RECEIVED: (MessageReceivedContext, None),
    EVENT_MESSAGE_ENRICH: (MessageEnrichContext, None),
    EVENT_SYSTEM_ENRICH: (SystemEnrichContext, None),
    EVENT_MESSAGE_BEFORE_RESPONSE: (BeforeResponseContext, ("draft", ResponseDraft)),
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM: (FinalResponseTransformContext, ("draft", FinalResponseDraft)),
    EVENT_MESSAGE_AFTER_RESPONSE: (AfterResponseContext, ("result", ResponseResult)),
    EVENT_MESSAGE_CANCELLED: (CancelledResponseContext, ("info", CancelledResponseInfo)),
}


class TurnRefused(Exception):  # noqa: N818 - plugin tests read it as what happened, as `pytest.raises(TurnRefused)`.
    """The agent would not answer this sender in this room, so nothing ran."""


@dataclass(frozen=True)
class SentMessage:
    """A message a hook or a route of the plugin sent to a room."""

    text: str
    thread_id: str | None
    source: str
    """The plugin and the event or ``http`` that sent it, as ``<plugin>:<event>``."""
    content: Mapping[str, Any]
    """The keys the message adds to its Matrix content."""
    mentions: Sequence[str] | None


@dataclass(frozen=True)
class Room:
    """A room as the plugin's tests set it up, which MindRoom's router is joined to, and what was sent to it."""

    room_id: str
    members: Mapping[str, str | None]
    """Each joined user's display name, None for one without; add the room again to change who is joined."""
    power_levels: dict[str, Any] | None = None
    """The content of the room's ``m.room.power_levels``; None for a room without one."""
    direct: bool = False
    """Whether its members joined it as a direct message; the room has no name, so one member alone with the router counts too."""
    history_visibility: str = "shared"
    key: str | None = None
    """The room key an agent's ``rooms`` or an ``access`` rule names this room by."""
    sent: list[SentMessage] = field(default_factory=list)


def _state_events(room: Room, joined: Mapping[str, str | None]) -> list[dict[str, Any]]:
    """Return the room's state as a homeserver lists it, each of the ``joined`` joined since the epoch."""
    events: list[tuple[str, str, Mapping[str, Any]]] = [
        ("m.room.history_visibility", "", {"history_visibility": room.history_visibility}),
    ]
    if room.power_levels is not None:
        events.append(("m.room.power_levels", "", room.power_levels))
    events.extend(
        ("m.room.member", user, {"membership": "join", "displayname": name, "is_direct": room.direct})
        for user, name in joined.items()
    )
    return [
        {"type": kind, "state_key": key, "content": content, "origin_server_ts": 0} for kind, key, content in events
    ]


class _StandIn:
    """Answers only the attributes it was given; any other is an operation the kit does not offer, named as such."""

    def __init__(self, name: str, **attributes: object) -> None:
        self.__dict__.update(attributes, _stand_in=name)

    def __getattr__(self, attribute: str) -> NoReturn:
        msg = f"mindroom.plugin_testing's {self._stand_in} does not offer {attribute}"
        raise AttributeError(msg)


class _PluginClient(TestClient):
    """A test client that, given a session token, presents it, and whose websockets connect from an allowed origin."""

    def __init__(self, app: FastAPI, token: str | None, portal: BlockingPortal) -> None:
        super().__init__(app, headers={"Authorization": f"Bearer {token}"} if token is not None else None)
        # One event loop serves every client of a load, as one server does, so sessions and streams can share it.
        self.portal = portal
        self._offers = [SESSION_PROTOCOL, f"{SESSION_PROTOCOL}.{token}"] if token is not None else []

    def __enter__(self) -> Self:
        """Keep serving on the load's event loop, which Starlette's own would replace; the app has no lifespan."""
        return self

    def __exit__(self, *args: object) -> None:
        """Leave the load's event loop to the block, which stops it."""

    def websocket_connect(
        self,
        url: str,
        subprotocols: Sequence[str] | None = None,
        **kwargs: Any,  # noqa: ANN401 - passed on to Starlette's own
    ) -> WebSocketTestSession:
        """Connect from the allowed origin, offering the client session's subprotocols before ``subprotocols``."""
        headers = {"origin": _CLIENT_ORIGIN, **kwargs.pop("headers", {})}
        return super().websocket_connect(url, [*self._offers, *(subprotocols or ())], headers=headers, **kwargs)


class LoadedPlugin:
    """The plugin `load_plugin` loaded: its config, its rooms, and the routes, tools and hooks it serves there."""

    def __init__(self, config: Config, runtime_paths: RuntimePaths, portal: BlockingPortal) -> None:
        self.config = config
        self.runtime_paths = runtime_paths
        self._portal = portal
        # The router is joined to every room, as the bot whose client reads it live is.
        router_id = current_entity_id(ROUTER_AGENT_NAME, runtime_paths).full_id
        self._router_member = {router_id: config.entity_display_name(ROUTER_AGENT_NAME)}
        self._rooms: dict[str, Room] = {}
        self._event_ids = count(1)
        self._tokens: dict[str, list[str]] = {}
        self._memberships = AgentReplyMembershipIndex()
        # Agents keep whether a room is direct for minutes, by their user ID and the room's.
        self._direct_user = f"@plugin-testing-{uuid4().hex}:localhost"
        self._client = cast(
            "nio.AsyncClient",
            _StandIn(
                "Matrix client",
                rooms=self._rooms,
                joined_rooms=self._joined_rooms,
                joined_members=self._joined_members,
                room_get_state=self._room_get_state,
                room_get_state_event=self._room_get_state_event,
            ),
        )
        router = _StandIn(
            "router",
            running=True,
            client=self._client,
            config=config,
            runtime_paths=runtime_paths,
            hook_context=_StandIn("router hooks", send_message=self._send),
        )
        self._app = FastAPI()
        state = ensure_app_state(self._app)
        state.api_state = ApiState(threading.Lock(), ApiSnapshot(1, runtime_paths, runtime_config=config))
        bots = {ROUTER_AGENT_NAME: router}
        state.embedded = cast(
            "EmbeddedRuntime",
            _StandIn(
                "MindRoom runtime",
                agent_bot=bots.get,
                agent_reply_memberships=self._memberships,
                refresh_agent_reply_memberships_when_needed=self._refresh_memberships_when_needed,
            ),
        )
        bind_room_members(self._app, cast("Mapping[str, AgentBot]", bots))
        state.room_direct = self._room_direct
        mount_plugin_routes(self._app)

    def add_room(
        self,
        room_id: str,
        members: Mapping[str, str | None],
        *,
        power_levels: Mapping[str, Any] | None = None,
        direct: bool = False,
        history_visibility: str = "shared",
        key: str | None = None,
    ) -> Room:
        """Add a room the router is joined to, with ``members`` joined by display name, and return it.

        ``key`` names the room as an agent's ``rooms`` or an ``access`` rule does. Adding a room again replaces it, as
        a membership change would. A room never added is one no MindRoom bot is in.
        """
        levels = dict(power_levels) if power_levels is not None else None
        room = Room(room_id, MappingProxyType(dict(members)), levels, direct, history_visibility, key)
        self._rooms[room_id] = room
        if key is not None:
            update_matrix_state(self.runtime_paths, lambda state: state.add_room(key, room_id, "", key))
        # The next decision reads every room again, as membership events make the router do live.
        self._memberships.invalidate(self.config, reason="plugin_testing_room_added")
        return room

    def client(self, user_id: str | None = None) -> TestClient:
        """Return a test client of the plugin's routes, signed in as ``user_id`` with a client session when given."""
        token = None
        if user_id is not None:
            token = self._portal.call(client_session_store(self._app).create, user_id).token
            self._tokens.setdefault(user_id, []).append(token)
        return _PluginClient(self._app, token, self._portal)

    def sign_out(self, user_id: str) -> None:
        """End every client session of ``user_id``, as signing out does."""
        store = client_session_store(self._app)
        for token in self._tokens.pop(user_id, []):
            if store.lookup(token) is not None:
                self._portal.call(store.revoke, token)

    async def call_tool(
        self,
        tool: str,
        function: str,
        arguments: Mapping[str, Any],
        *,
        agent: str,
        sender: str,
        room: str,
    ) -> CallResult:
        """Call ``function`` of the agent's ``tool`` for ``sender`` in ``room`` and return what the call came to.

        It raises ``TurnRefused`` when the agent would not answer the sender there, ``LookupError`` when the agent has
        no such tool or function, and ``PermissionError`` for a function that may need approval. The call runs through
        the plugins' ``tool:*`` hooks and inside the conversation ``current_plugin_context`` reads.
        """
        context = await self._turn_context(agent, sender, room, f"$message{next(self._event_ids)}")
        agent_tools = resolve_agent_tools(self.config, agent, context.plugins.tools)
        entry = next((entry for entry in agent_tools.candidates() if entry.name == tool), None)
        if entry is None:
            msg = f"Agent {agent!r} has no tool {tool!r}"
            raise LookupError(msg)
        gateway = AgentToolContext(
            agent,
            context.requester_id,
            self.config,
            self.runtime_paths,
            context.execution_identity,
            context.resolve_worker_target(),
        )

        async def call(toolkit: AnyToolkit) -> CallResult:
            target = toolkit_function(toolkit, function)
            if target is None:
                msg = f"Tool {tool!r} has no function {function!r}"
                raise LookupError(msg)
            if may_need_approval(gateway, tool, target):
                msg = f"{tool}.{function} may need approval, which mindroom.plugin_testing cannot give"
                raise PermissionError(msg)
            policy = build_tool_hook_middleware(context.hooks, self.config, self.runtime_paths)
            call_context = ToolCallContext(
                run_id=f"plugin-testing:{uuid4().hex}",
                session_id=context.session_id,
                user_id=context.requester_id,
            )
            return await run_tool_call(
                replace(target, registered_toolkit=tool),
                arguments,
                call_context,
                middleware=(policy,),
            )

        with tool_runtime_context(context):
            return await run_toolkit_operation(gateway, entry, None, call)

    async def fire(self, event: str, *, agent: str, sender: str, room: str, body: str = "", **fields: Any) -> object:  # noqa: ANN401
        """Run the plugins' hooks for ``event`` on ``sender``'s message ``body`` to ``agent`` in ``room``.

        ``fields`` are the event's own; for ``message:before_response``, ``message:final_response_transform``,
        ``message:after_response`` and ``message:cancelled`` they are those of its draft, result or info, and for a
        custom event its ``payload`` and ``source_plugin``. It raises ``TurnRefused`` when the agent would not answer
        the sender there, and returns what the event's hooks returned to MindRoom: the enrichment items, the
        transformed draft, the context the hooks changed, or None for a custom event.
        """
        event = validate_event_name(event)
        if event in BUILTIN_EVENT_NAMES and event not in _TURN_EVENTS:
            msg = f"mindroom.plugin_testing fires message, system:enrich and custom events, not {event}"
            raise ValueError(msg)
        event_id = f"$message{next(self._event_ids)}"
        context = await self._turn_context(agent, sender, room, event_id)
        if event not in _TURN_EVENTS:
            # A custom event comes from a tool call, so it runs as that call emits it, in the call's conversation.
            with tool_runtime_context(context):
                return await emit_custom_event(fields["source_plugin"], event, fields["payload"])
        base = context.hooks.context_kwargs(
            event,
            config=self.config,
            runtime_paths=self.runtime_paths,
            correlation_id=event_id,
        )
        origin = classify_turn_origin(
            transport_sender_id=sender,
            requester_id=context.requester_id,
            sender_entity_name=None,
            requester_entity_name=None,
            source_kind=MESSAGE_SOURCE_KIND,
            original_sender=None,
            trusted_user_relay=False,
        )
        envelope = MessageEnvelope(
            event_id,
            context.target,
            body,
            (),
            (),
            agent,
            origin=origin,
        )
        context_type, holder = _TURN_EVENTS[event]
        if holder is None:
            hook_context = context_type(**base, envelope=envelope, **fields)
        else:
            hook_context = context_type(**base, **{holder[0]: holder[1](envelope=envelope, **fields)})
        registry = context.hooks.registry
        if isinstance(hook_context, MessageEnrichContext | SystemEnrichContext):
            return await emit_collect(registry, event, hook_context)
        if isinstance(hook_context, BeforeResponseContext):
            return await emit_transform(registry, event, hook_context)
        if isinstance(hook_context, FinalResponseTransformContext):
            return await emit_final_response_transform(registry, event, hook_context)
        await emit(registry, event, hook_context)
        return hook_context

    async def _turn_context(self, agent: str, sender: str, room_id: str, event_id: str) -> ToolRuntimeContext:
        """Return the conversation of ``sender`` with ``agent`` in ``room_id``, once the agent would answer them there.

        A turn decides as live ones do, from the membership the rooms show now, and the sender must be joined. Its
        requester is the person the sender is, through any bridge alias of theirs.
        """
        if not is_human_requester_id(sender, self.config, self.runtime_paths):
            msg = f"mindroom.plugin_testing sends messages from people, not from {sender}"
            raise ValueError(msg)
        await self._memberships.refresh(self.config, self.runtime_paths, self._client)
        room = self._rooms.get(room_id)
        if (
            room is None
            or sender not in room.members
            or not is_sender_allowed_for_agent_reply_in_room(
                sender,
                agent,
                self.config,
                room_id,
                self.runtime_paths,
                self._memberships,
            )
        ):
            msg = f"{agent} does not answer {sender} in {room_id}"
            raise TurnRefused(msg)
        plugins = config_plugins(self.config, self.runtime_paths)
        room_mode = self.config.get_entity_thread_mode(agent, self.runtime_paths, room_id=room_id) == "room"
        return ToolRuntimeContext(
            agent_name=agent,
            # The sender's message ``event_id`` starts the turn and, in the agent's thread mode, roots its thread.
            target=MessageTarget.resolve(room_id, None, event_id, event_id, room_mode=room_mode),
            correlation_id=event_id,
            requester_id=resolve_human_requester_alias(sender, self.config, self.runtime_paths),
            client=self._client,
            config=self.config,
            runtime_paths=self.runtime_paths,
            plugins=plugins,
            conversation_reader=cast("ConversationReader", _StandIn("conversation reader")),
            relations=cast("RelationLookup", _StandIn("relation lookup")),
            agent_reply_memberships=self._memberships,
            storage_path=self.runtime_paths.storage_root,
            hooks=HookBindings(
                registry=plugins.hooks,
                registry_state=HookRegistryState(plugins.hooks),
                message_sender=self._send,
                room_state_querier=build_hook_room_state_querier(self._client),
            ),
        )

    async def _send(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None,
        *,
        trigger_dispatch: bool = False,  # noqa: ARG002 - nothing dispatches in a test
        mentions: list[str] | None = None,
        wake_mentioned: bool = True,  # noqa: ARG002 - nothing dispatches in a test
    ) -> str | None:
        room = self._rooms.get(room_id)
        if room is None:
            return None
        room.sent.append(SentMessage(body, thread_id, source_hook, dict(extra_content or {}), mentions))
        return f"$sent{next(self._event_ids)}"

    async def _refresh_memberships_when_needed(self) -> None:
        """Refresh the membership index from the rooms when it needs it, as the runtime does for an access check."""
        if self._memberships.needs_refresh(self.config):
            await self._memberships.refresh(self.config, self.runtime_paths, self._client)

    def _joined(self, room: Room) -> dict[str, str | None]:
        return {**self._router_member, **room.members}

    async def _joined_rooms(self) -> nio.JoinedRoomsResponse:
        return nio.JoinedRoomsResponse(list(self._rooms))

    async def _joined_members(self, room_id: str) -> nio.JoinedMembersResponse | nio.JoinedMembersError:
        room = self._rooms.get(room_id)
        if room is None:
            return nio.JoinedMembersError("Not in this room", "M_FORBIDDEN")
        return nio.JoinedMembersResponse(
            [nio.RoomMember(user, name or "", "") for user, name in self._joined(room).items()],
            room_id,
        )

    async def _room_get_state(self, room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        room = self._rooms.get(room_id)
        if room is None:
            return nio.RoomGetStateError("Not in this room", "M_FORBIDDEN")
        return nio.RoomGetStateResponse(_state_events(room, self._joined(room)), room_id)

    async def _room_get_state_event(
        self,
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        room = self._rooms.get(room_id)
        for event in _state_events(room, self._joined(room)) if room is not None else []:
            if (event["type"], event["state_key"]) == (event_type, state_key):
                return nio.RoomGetStateEventResponse(event["content"], event_type, state_key, room_id)
        return nio.RoomGetStateEventError("Not found", "M_NOT_FOUND")

    async def _room_direct(self, room_id: str) -> bool:
        """Answer as running agents do, through a client of this load's own, whose answers no other load shares."""
        room = nio.MatrixRoom(room_id, self._direct_user)
        for user, name in self._joined(self._rooms[room_id]).items():
            room.add_member(user, name, None)
        client = _StandIn(
            "Matrix client",
            user_id=self._direct_user,
            rooms={room_id: room},
            list_direct_rooms=self._no_direct_rooms,
            room_get_state=self._room_get_state,
        )
        # A failed `m.direct` read is the only unknown answer, and this client's never fails.
        return await is_dm_room(cast("nio.AsyncClient", client), room_id) is True

    async def _no_direct_rooms(self) -> nio.DirectRoomsErrorResponse:
        return nio.DirectRoomsErrorResponse("No m.direct account data", "M_NOT_FOUND")


@contextmanager
def load_plugin(
    path: str | Path,
    tmp_dir: str | Path,
    *,
    config: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> Iterator[LoadedPlugin]:
    """Load the plugin at ``path`` with ``settings`` under ``config``, as MindRoom starts it, for the block.

    ``config`` is what a ``config.yaml`` holds, such as ``agents``, ``access`` and ``administrators``, without
    ``plugins``; a config without ``models`` gets a ``default`` model, which nothing calls. Its data lives under
    ``tmp_dir``, and the environment is left alone. A config that does not validate, or a plugin that fails to load,
    even only its routes or lifecycle module, raises, naming why. When the block ends, the plugins published before
    it are published again.
    """
    root = Path(tmp_dir)
    runtime_paths = resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "mindroom_data",
        process_env={COMPUTER_ALLOWED_ORIGINS_ENV: json.dumps([_CLIENT_ORIGIN])},
    )
    entry = {"path": str(Path(path).resolve()), "settings": dict(settings or {})}
    data = {"models": {"default": {"provider": "openai", "id": "unused"}}, **(config or {}), "plugins": [entry]}
    validated = Config.validate_with_runtime(data, runtime_paths)
    domain = validated.get_domain(runtime_paths)

    def save_accounts(state: MatrixState) -> None:
        for entity in (ROUTER_AGENT_NAME, *validated.agents, *validated.teams):
            username = agent_username_localpart(entity, runtime_paths)
            state.add_account(managed_account_key(entity), username, None, domain=domain)

    update_matrix_state(runtime_paths, save_accounts)
    published = config_plugins(validated, runtime_paths).generation
    generation = prepare_plugins(validated, runtime_paths)
    # MindRoom serves a plugin whose routes or lifecycle module fails without them; a test fails on it instead.
    partly = (error for plugin in generation.plugins.values() for error in (plugin.routes_error, plugin.lifespan_error))
    if errors := [*(plugin.error for plugin in generation.skipped), *filter(None, partly)]:
        dispose_plugins(generation)
        raise errors[0]
    publish_plugins(generation)
    try:
        with start_blocking_portal() as portal:
            yield LoadedPlugin(validated, runtime_paths, portal)
    finally:
        publish_plugins(published)
