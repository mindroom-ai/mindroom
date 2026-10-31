"""What the router bot does beyond what every bot does."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from mindroom.commands import config_confirmation
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
    import nio
    import structlog

    from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
    from mindroom.bot_room_lifecycle import BotRoomLifecycle
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.event_journal import IngestionRecordAdmission, PrincipalStore
    from mindroom.hooks import HookContextSupport
    from mindroom.matrix.identity import MatrixID
    from mindroom.personal_room_lifecycle import PersonalRoomLifecycle


@dataclass(frozen=True)
class RouterDutiesDeps:
    """The router bot's own collaborators its duties act through."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    matrix_id: MatrixID
    hook_context: HookContextSupport
    journal: PrincipalStore
    room_lifecycle: BotRoomLifecycle
    personal_rooms: PersonalRoomLifecycle


@dataclass
class RouterDuties:
    """Keep room-backed reply grants current, set up the rooms the router joins, and emit room-membership hooks.

    Only the router bot builds one; the runtime owns the reply-membership index and its sync coordinator, which the
    router drives from its own Matrix sync.
    """

    deps: RouterDutiesDeps
    # Serializes at-least-once room-member-joined emission across the router's callbacks.
    _member_join_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def _membership_sync(self) -> AgentReplyMembershipSync:
        return self.deps.runtime.orchestrator.agent_reply_membership_sync

    async def started(self, client: nio.AsyncClient) -> None:
        """Remove managed accounts the config no longer has from the rooms the router can see."""
        try:
            await cleanup_all_orphaned_bots(client, self.deps.runtime.config, self.deps.runtime.runtime_paths)
        except Exception as e:
            self.deps.logger.warning("orphaned_bot_cleanup_failed", error=str(e))

    async def room_joined(self, room_id: str) -> None:
        """Restore pending config confirmations in a room the router joined, then welcome an empty room."""
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

    def invalidate_memberships(self, *, reason: str) -> None:
        """Fail closed when the router's view of Matrix membership is uncertain."""
        self._membership_sync.invalidate(self.deps.runtime.config, reason=reason)

    async def refresh_memberships_if_needed(self) -> None:
        """Rebuild room-backed reply grants after a receive-generation boundary."""
        orchestrator = self.deps.runtime.orchestrator
        await self._membership_sync.refresh_if_needed(orchestrator.config, orchestrator.refresh_agent_reply_memberships)

    def before_ingestion_admission(
        self,
        admission: IngestionRecordAdmission,
        timeline_provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        """Revoke uncertain control-room grants before the router admits more work."""
        runtime = self.deps.runtime
        self._membership_sync.pre_admit_ingestion(runtime.config, runtime.runtime_paths, admission, timeline_provenance)

    async def live_membership_transition(self, room_id: str, event: nio.RoomMemberEvent) -> None:
        """Update reply grants from one durably admitted live Matrix membership transition."""
        await self._membership_sync.apply_live_transition(
            self.deps.runtime.config,
            room_id,
            event,
            control_user_id=self.deps.matrix_id.full_id,
        )

    async def member_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Emit the room-membership hooks of one live human join or self-leave."""
        registry = self.deps.hook_context.registry
        runtime = self.deps.runtime
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
        """Emit room:member_joined for one live human Matrix room join."""
        await self.deps.personal_rooms.baseline_join(join)
        if self.deps.hook_context.registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            await self._emit_member_change(EVENT_ROOM_MEMBER_JOINED, RoomMemberJoinedContext, join)

    async def _emit_member_left(self, leave: RoomMemberLeave) -> None:
        """Emit room:member_left for one live human Matrix room self-leave."""
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
