"""On-demand steward operations, membership hooks and owner duties."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import nio

from mindroom.background_tasks import create_background_task
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.hooks import (
    EVENT_ROOM_MEMBER_JOINED,
    EVENT_ROOM_MEMBER_LEFT,
    RoomMemberJoinedContext,
    RoomMemberLeftContext,
    emit,
)
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.room_member_joins import (
    RoomMemberJoin,
    RoomMemberLeave,
    emit_room_member_join_at_least_once,
    room_member_left_from_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

    from mindroom.bot import AgentBot
    from mindroom.bot_room_lifecycle import BotRoomLifecycle
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.event_journal import PrincipalStore
    from mindroom.hooks import HookContextSupport
    from mindroom.personal_room_lifecycle import PersonalRoomLifecycle
    from mindroom.runtime_protocols import OrchestratorRuntime


def moderator_rights_message(feature: str) -> str:
    """Return what an agent answers when a person's room refuses the state write a feature needs."""
    return f"I need moderator rights in this room to {feature}."


async def runs_room_hooks(
    orchestrator: OrchestratorRuntime,
    agent_name: str,
    room_id: str,
    event_name: str,
    event_id: str | None,
) -> bool:
    """Return whether the bot of that name runs one emission's room-scoped hooks.

    A Matrix event has one claim, which its first writer keeps on each retry and after a handover: a membership
    event's goes to the steward of that moment, and a message's or reaction's to the first bot that emits it, which
    admitted its sender. Any other emission runs them on the room's steward.
    """
    steward = orchestrator.room_steward(room_id)
    if event_id is None:
        return steward is not None and steward.agent_name == agent_name
    elects = event_name in (EVENT_ROOM_MEMBER_JOINED, EVENT_ROOM_MEMBER_LEFT)
    owner = steward.agent_name if steward is not None and elects else agent_name
    purpose = f"hook:{event_name}"
    return await orchestrator.event_journal_store().claim_room_event(room_id, event_id, purpose, owner) == agent_name


def steward_may_write_state(room_steward: Callable[[str], AgentBot | None], room_id: str, event_type: str) -> bool:
    """Return whether the room's steward may write one state event type, by its own view of the room's power levels."""
    steward = room_steward(room_id)
    client = None if steward is None else steward.client
    room = None if client is None else client.rooms.get(room_id)
    return client is not None and room is not None and room.power_levels.can_user_send_state(client.user_id, event_type)


async def room_state_response_as_steward(
    room_steward: Callable[[str], AgentBot | None],
    room_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    state_key: str | None = "",
) -> nio.RoomPutStateResponse | nio.RoomPutStateError | None:
    """Write one state event through the room steward's client and return the homeserver's answer, None without one.

    A ``state_key`` of None is the steward's own Matrix ID, the one such key Matrix lets it write.
    """
    steward = room_steward(room_id)
    if steward is None or steward.client is None:
        return None
    key = steward.client.user_id if state_key is None else state_key
    return await steward.client.room_put_state(room_id, event_type, content, state_key=key)


async def put_room_state_as_steward(
    room_steward: Callable[[str], AgentBot | None],
    room_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    state_key: str | None = "",
) -> bool | None:
    """Write one state event like `room_state_response_as_steward` and return whether it was written.

    None means refused: the room has no steward, or it lacks the power, which only moderator rights can fix.
    """
    response = await room_state_response_as_steward(room_steward, room_id, event_type, content, state_key=state_key)
    if response is None or (isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"):
        return None
    return isinstance(response, nio.RoomPutStateResponse)


@dataclass(frozen=True)
class StewardDutiesDeps:
    """The bot's own collaborators its steward duties act through."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    hook_context: HookContextSupport
    journal: PrincipalStore
    room_lifecycle: BotRoomLifecycle
    personal_rooms: PersonalRoomLifecycle


@dataclass
class StewardDuties:
    """Emit room-membership hooks for the rooms this bot stewards.

    The owner account's administration, orphaned-account cleanup and the welcome, also lives here for now.
    """

    deps: StewardDutiesDeps
    # Serializes at-least-once room-member-joined emission across the bot's callbacks.
    _member_join_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def _is_owner(self) -> bool:
        """Whether this bot is the owner account, which administers the configured rooms."""
        # Owner-only administration (design decision 8): this tests an entity's role, not who is in a room.
        return self.deps.hook_context.agent_name == ROUTER_AGENT_NAME

    def is_steward(self, room_id: str) -> bool:
        """Return whether this bot is the room's steward now."""
        steward = self.deps.runtime.orchestrator.room_steward(room_id)
        return steward is not None and steward.agent_name == self.deps.hook_context.agent_name

    async def started(self, client: nio.AsyncClient) -> None:
        """Remove managed accounts the config no longer has from the rooms the owner account can see."""
        if not self._is_owner:
            return
        try:
            await cleanup_all_orphaned_bots(client, self.deps.runtime.config, self.deps.runtime.runtime_paths)
        except Exception as e:
            self.deps.logger.warning("orphaned_bot_cleanup_failed", error=str(e))

    async def room_joined(self, room_id: str) -> None:
        """Welcome an empty room the owner account joined."""
        if self._is_owner:
            await self.deps.room_lifecycle.send_welcome_message_if_empty(room_id)

    async def power_levels_event(self, room: nio.MatrixRoom, _event: nio.PowerLevelsEvent) -> None:
        """Mark the room after its power levels changed, which can change its steward."""
        self.deps.runtime.orchestrator.room_state.mark(room.room_id)

    async def member_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Refresh responder presentation, the room's MindRoom accounts and the welcome, and emit human membership hooks."""
        registry = self.deps.hook_context.registry
        runtime = self.deps.runtime
        if event.membership != event.prev_membership and event.state_key in prepared_entity_names_by_user_id(
            runtime.config,
            runtime.runtime_paths,
        ):
            runtime.orchestrator.room_state.mark(room.room_id)
            if self._is_owner:
                # An edit can wait on a slow homeserver, so it holds none of the room's later events.
                create_background_task(
                    self.deps.room_lifecycle.refresh_welcome(room.room_id),
                    name="owner_welcome_refresh",
                    owner=self.deps.runtime,
                )
        if registry.has_hooks(EVENT_ROOM_MEMBER_LEFT):
            leave = room_member_left_from_event(room, event, config=runtime.config, runtime_paths=runtime.runtime_paths)
            if leave is not None:
                await self._emit_member_left(leave)
                return
        if not (registry.has_hooks(EVENT_ROOM_MEMBER_JOINED) or self.deps.personal_rooms.onboards_from(room.room_id)):
            return
        await emit_room_member_join_at_least_once(
            room,
            event,
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            store=self.deps.journal,
            lock=self._member_join_lock,
            emit=self._emit_member_joined,
        )

    async def _emit_member_joined(self, join: RoomMemberJoin) -> None:
        """Emit room:member_joined for one live human Matrix room join, where this bot holds the event's claim."""
        orchestrator, name = self.deps.runtime.orchestrator, self.deps.hook_context.agent_name
        if not await runs_room_hooks(orchestrator, name, join.room_id, EVENT_ROOM_MEMBER_JOINED, join.event_id):
            return
        await self.deps.personal_rooms.baseline_join(join)
        if self.deps.hook_context.registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            await self._emit_member_change(EVENT_ROOM_MEMBER_JOINED, RoomMemberJoinedContext, join)

    async def _emit_member_left(self, leave: RoomMemberLeave) -> None:
        """Emit room:member_left for one live human Matrix room self-leave, where this bot holds the event's claim."""
        orchestrator, name = self.deps.runtime.orchestrator, self.deps.hook_context.agent_name
        if await runs_room_hooks(orchestrator, name, leave.room_id, EVENT_ROOM_MEMBER_LEFT, leave.event_id):
            await self._emit_member_change(EVENT_ROOM_MEMBER_LEFT, RoomMemberLeftContext, leave)

    async def _emit_member_change(
        self,
        event_name: str,
        context_type: type[RoomMemberJoinedContext | RoomMemberLeftContext],
        change: RoomMemberJoin | RoomMemberLeave,
    ) -> None:
        """Emit one membership hook whose context carries the change's fields."""
        hook_context = self.deps.hook_context
        context = context_type(
            **hook_context.base_kwargs(event_name, change.event_id),
            agent_name=hook_context.agent_name,
            **asdict(change),
        )
        await emit(hook_context.registry, event_name, context)
