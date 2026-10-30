"""What the router bot does beyond what every bot does."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
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
    import asyncio

    import nio
    import structlog

    from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync, ReplyMembershipPreAdmission
    from mindroom.bot_room_lifecycle import BotRoomLifecycle
    from mindroom.bot_runtime_view import BotRuntimeState
    from mindroom.event_journal import IngestionRecordAdmission, PrincipalStore
    from mindroom.hooks import HookContextSupport
    from mindroom.matrix.identity import MatrixID
    from mindroom.personal_room_lifecycle import PersonalRoomLifecycle


@dataclass(frozen=True)
class RouterDutiesDeps:
    """The router bot's own collaborators its duties act through."""

    runtime: BotRuntimeState
    logger: structlog.stdlib.BoundLogger
    matrix_id: MatrixID
    hook_context: HookContextSupport
    journal: PrincipalStore
    room_lifecycle: BotRoomLifecycle
    personal_rooms: PersonalRoomLifecycle
    # Serializes at-least-once room-member-joined emission across the router's callbacks.
    member_join_lock: asyncio.Lock


@dataclass
class RouterDuties:
    """Keep room-backed reply grants current, set up the rooms the router joins, and emit room-membership hooks.

    Only the router bot builds one; the runtime owns the reply-membership index and its sync coordinator, which the
    router drives from its own Matrix sync.
    """

    deps: RouterDutiesDeps

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
        self.deps.runtime.orchestrator.invalidate_agent_reply_memberships(reason=reason)

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
        effects = self._membership_sync.pre_admit_ingestion(
            runtime.config,
            runtime.runtime_paths,
            admission,
            timeline_provenance,
        )
        self._publish_pre_admission(effects)

    def _publish_pre_admission(self, effects: ReplyMembershipPreAdmission) -> None:
        """Publish one sync batch's fail-closed effects."""
        if effects.invalidate_reason is not None:
            self.invalidate_memberships(reason=effects.invalidate_reason)
        if effects.authorization_changed:
            create_background_task(
                self._revoke_denied_calls(),
                name="matrix_rtc_reply_authorization_revoke",
                owner=self.deps.runtime,
            )

    async def _revoke_denied_calls(self) -> None:
        """End every bot's calls that the current reply policy no longer permits."""
        await self.deps.runtime.orchestrator.revoke_reply_authorized_calls()

    async def live_membership_transition(self, room_id: str, event: nio.RoomMemberEvent) -> None:
        """Update reply grants from one durably admitted live Matrix membership transition."""
        await self._membership_sync.apply_live_transition(
            self.deps.runtime.config,
            room_id,
            event,
            control_user_id=self.deps.matrix_id.full_id,
            reconcile_effects=self._reconcile_membership_effects,
        )

    async def _reconcile_membership_effects(self) -> None:
        """Run the effects that depend on one committed reply-membership change."""
        orchestrator = self.deps.runtime.orchestrator
        create_background_task(
            orchestrator.reconcile_pending_invites(),
            name="pending_invite_reconciliation",
            owner=self.deps.runtime,
        )
        await orchestrator.revoke_reply_authorized_calls()
        create_background_task(
            orchestrator.reconcile_reply_authorized_calls(),
            name="matrix_rtc_reply_authorization",
            owner=self.deps.runtime,
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
            lock=self.deps.member_join_lock,
            emit=self._emit_member_joined,
        )

    async def _emit_member_joined(self, join: RoomMemberJoin) -> None:
        """Emit room:member_joined for one live human Matrix room join."""
        await self.deps.personal_rooms.baseline_join(join)
        registry = self.deps.hook_context.registry
        if not registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            return
        context = RoomMemberJoinedContext(
            **self.deps.hook_context.base_kwargs(EVENT_ROOM_MEMBER_JOINED, join.event_id),
            agent_name=self.deps.hook_context.agent_name,
            room_id=join.room_id,
            event_id=join.event_id,
            user_id=join.user_id,
            sender_id=join.sender_id,
            display_name=join.display_name,
            avatar_url=join.avatar_url,
            membership=join.membership,
            prev_membership=join.prev_membership,
        )
        await emit(registry, EVENT_ROOM_MEMBER_JOINED, context)

    async def _emit_member_left(self, leave: RoomMemberLeave) -> None:
        """Emit room:member_left for one live human Matrix room self-leave."""
        registry = self.deps.hook_context.registry
        context = RoomMemberLeftContext(
            **self.deps.hook_context.base_kwargs(EVENT_ROOM_MEMBER_LEFT, leave.event_id),
            agent_name=self.deps.hook_context.agent_name,
            room_id=leave.room_id,
            event_id=leave.event_id,
            user_id=leave.user_id,
            sender_id=leave.sender_id,
            display_name=leave.display_name,
            avatar_url=leave.avatar_url,
            membership=leave.membership,
            prev_membership=leave.prev_membership,
        )
        await emit(registry, EVENT_ROOM_MEMBER_LEFT, context)
