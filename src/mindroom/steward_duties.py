"""The invisible work a room's steward does for the room: its MindRoom room state and its member hooks.

Every bot builds the duties, and each duty asks whether its bot is the room's steward at the moment it runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import nio

from mindroom.background_tasks import create_background_task
from mindroom.commands import config_confirmation
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.hooks import (
    EVENT_ROOM_MEMBER_JOINED,
    EVENT_ROOM_MEMBER_LEFT,
    RoomMemberJoinedContext,
    RoomMemberLeftContext,
    emit,
)
from mindroom.matrix.room_accounts import reconcile_room_accounts
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.room_member_joins import (
    RoomMemberJoin,
    RoomMemberLeave,
    emit_room_member_join_at_least_once,
    room_member_left_from_event,
)
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode

if TYPE_CHECKING:
    import structlog

    from mindroom.bot_room_lifecycle import BotRoomLifecycle
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.event_journal import PrincipalStore
    from mindroom.hooks import HookContextSupport
    from mindroom.personal_room_lifecycle import PersonalRoomLifecycle
    from mindroom.runtime_protocols import OrchestratorRuntime


def moderator_rights_message(feature: str) -> str:
    """Return what an agent answers when a person's room refuses the state write a feature needs."""
    return f"I need moderator rights in this room to {feature}."


async def put_room_state_as_steward(
    runtime: OrchestratorRuntime,
    room_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    state_key: str = "",
) -> bool:
    """Write one state event through the room steward's client; False without a steward or when the room refuses."""
    steward = runtime.room_steward(room_id)
    if steward is None or steward.client is None:
        return False
    response = await steward.client.room_put_state(room_id, event_type, content, state_key=state_key)
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
    """Publish the room state of the rooms this bot stewards and emit their room-membership hooks.

    The owner account's administration, orphaned-account cleanup and the welcome, also lives here for now.
    """

    deps: StewardDutiesDeps
    # Serializes at-least-once room-member-joined emission across the bot's callbacks.
    _member_join_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Rooms whose state is due since the last sync frame; a local join's room has no members until that frame.
    _joined_room_ids: set[str] = field(default_factory=set, init=False, repr=False)
    # The one background retry per room that a failed room state reconcile schedules.
    _room_state_retries: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    # Rooms where a trigger failed while their retry ran, so that retry runs once more.
    _room_state_reruns: set[str] = field(default_factory=set, init=False, repr=False)

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

    def stopping(self) -> None:
        """Cancel the room state retries, which a stopping bot discards, without waiting.

        The next steward redoes them, and waiting for them would spend the stop budget its replies need to finish.
        """
        for task in self._room_state_retries.values():
            task.cancel()

    async def room_joined(self, room_id: str) -> None:
        """Restore pending config confirmations in a room the owner account joined, then welcome an empty room."""
        if not self._is_owner:
            return
        client = self.deps.runtime.client
        assert client is not None
        restored_configs = await config_confirmation.restore_pending_changes(client, room_id)
        if restored_configs > 0:
            self.deps.logger.info(
                "restored_pending_config_changes",
                room_id=room_id,
                restored_config_count=restored_configs,
            )
        await self.deps.room_lifecycle.send_welcome_message_if_empty(room_id)

    def mark_joined_for_room_state(self, room_id: str) -> None:
        """Publish the room state of a room this bot joined by any path, once a sync frame carries its members."""
        self._joined_room_ids.add(room_id)

    async def power_levels_event(self, room: nio.MatrixRoom, _event: nio.PowerLevelsEvent) -> None:
        """Publish a room's state after its power levels changed, which can make this bot its steward."""
        self._joined_room_ids.add(room.room_id)

    async def publish_joined_room_state(self) -> None:
        """Reconcile the thread mode and MindRoom accounts of the marked rooms this bot stewards and still holds."""
        room_ids, self._joined_room_ids = self._joined_room_ids, set()
        await asyncio.gather(*(self._publish_room_state(room_id) for room_id in room_ids))

    async def _publish_room_state(self, room_id: str) -> None:
        """Reconcile one held room's thread mode and MindRoom accounts.

        A failure, such as a reconcile that timed out queued behind a slower one, leaves the room to a background retry,
        so the room catches up without another trigger while the caller still waits at most 10 s.
        """
        if await self._reconcile_room_state(room_id):
            return
        pending = self._room_state_retries.get(room_id)
        if pending is None or pending.done():
            self._room_state_retries[room_id] = create_background_task(
                self._retry_room_state(room_id),
                name="steward_room_state_retry",
                owner=self.deps.runtime,
            )
        else:
            # This trigger may have held the room's locks until the retry ran out of time, so the retry runs once more.
            self._room_state_reruns.add(room_id)

    async def _retry_room_state(self, room_id: str) -> None:
        """Reconcile once, and once more if any trigger failed meanwhile; the retry's own failure ends it."""
        while True:
            self._room_state_reruns.discard(room_id)
            await self._reconcile_room_state(room_id)
            if room_id not in self._room_state_reruns:
                return

    async def _reconcile_room_state(self, room_id: str) -> bool:
        """Reconcile one stewarded room's thread mode and MindRoom accounts and return whether both are current."""
        runtime = self.deps.runtime
        client = runtime.client
        if client is None or room_id not in client.rooms or not self.is_steward(room_id):
            return True
        current = await asyncio.gather(
            reconcile_room_thread_mode(client, runtime.config, runtime.runtime_paths, room_id),
            reconcile_room_accounts(client, runtime.config, runtime.runtime_paths, room_id),
        )
        return all(current)

    async def member_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Refresh responder presentation, the room's MindRoom accounts and the welcome, and emit human membership hooks."""
        registry = self.deps.hook_context.registry
        runtime = self.deps.runtime
        if event.membership != event.prev_membership and event.state_key in prepared_entity_names_by_user_id(
            runtime.config,
            runtime.runtime_paths,
        ):
            await self._publish_room_state(room.room_id)
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
        if not registry.has_hooks(EVENT_ROOM_MEMBER_JOINED) and not self.deps.personal_rooms.observes_onboarding_joins:
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
        if not await self._claims(join.room_id, join.event_id, "member_joined"):
            return
        await self.deps.personal_rooms.baseline_join(join)
        if self.deps.hook_context.registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            await self._emit_member_change(EVENT_ROOM_MEMBER_JOINED, RoomMemberJoinedContext, join)

    async def _emit_member_left(self, leave: RoomMemberLeave) -> None:
        """Emit room:member_left for one live human Matrix room self-leave, where this bot holds the event's claim."""
        if await self._claims(leave.room_id, leave.event_id, "member_left"):
            await self._emit_member_change(EVENT_ROOM_MEMBER_LEFT, RoomMemberLeftContext, leave)

    async def _claims(self, room_id: str, event_id: str, purpose: str) -> bool:
        """Claim the event for the room's steward now, unless it is claimed, and return whether this bot holds the claim.

        The first bot to dispatch the event decides, so the claim's holder runs the hook when its own copy arrives,
        on each retry and after a handover, and no other bot does.
        """
        name = self.deps.hook_context.agent_name
        orchestrator = self.deps.runtime.orchestrator
        steward = orchestrator.room_steward(room_id)
        owner = name if steward is None else steward.agent_name
        return await orchestrator.event_journal_store().claim_room_event(room_id, event_id, purpose, owner) == name

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
