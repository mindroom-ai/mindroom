"""Exact-owner Matrix operations for restart recovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol
from uuid import NAMESPACE_URL, uuid5

from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    should_persist_invited_rooms,
)
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedTargetFreshness,
    StaleStreamCleanupActor,
    build_auto_resume_content,
    cleanup_stale_streaming_room,
    interrupted_target_freshness,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import nio

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.matrix.stale_stream_cleanup import InterruptedThread

logger = get_logger(__name__)


@dataclass(frozen=True)
class RecoveryOwner:
    """One current bot generation available for restart recovery."""

    entity_name: str
    user_id: str
    generation: object
    client: nio.AsyncClient
    conversation_cache: ConversationCacheProtocol
    desired_room_ids: frozenset[str]
    first_sync_complete: bool


def build_restart_recovery_owners(
    bots: Mapping[str, AgentBot | TeamBot],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, RecoveryOwner]:
    """Snapshot current exact owner generations and their durable room scope."""
    owners: dict[str, RecoveryOwner] = {}
    for bot in bots.values():
        client = bot.client
        user_id = bot.agent_user.user_id
        if client is None or not user_id:
            continue
        desired_room_ids = set(bot.rooms)
        if should_persist_invited_rooms(config, bot.agent_name):
            desired_room_ids.update(
                load_invited_rooms(
                    invited_rooms_path(runtime_paths.storage_root, bot.agent_name),
                ),
            )
        owners[user_id] = RecoveryOwner(
            entity_name=bot.agent_name,
            user_id=user_id,
            generation=bot,
            client=client,
            conversation_cache=bot._conversation_cache,
            desired_room_ids=frozenset(desired_room_ids),
            first_sync_complete=bot.running and bot.first_sync_complete,
        )
    return owners


@dataclass(frozen=True)
class RoomRecoveryRequest:
    """One semantic owner-room recovery request."""

    room_id: str
    startup_cutoff_ms: int | None
    terminal_interrupted_only: bool


@dataclass(frozen=True)
class _RoomRecoveryResult:
    """Result of one owner-room recovery attempt."""

    interrupted_threads: tuple[InterruptedThread, ...] = ()
    retry: bool = False


class RestartTargetFreshness(Enum):
    """Authoritative freshness state for one interrupted target."""

    CURRENT = auto()
    NEWER_HUMAN = auto()
    UNRECOVERABLE = auto()
    RETRY = auto()


class _RecoverRoom(Protocol):
    async def __call__(
        self,
        owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        owner_user_ids: frozenset[str],
        config: Config,
    ) -> _RoomRecoveryResult:
        """Recover one room through its exact current owner."""
        ...


class _TargetFreshness(Protocol):
    async def __call__(
        self,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> RestartTargetFreshness:
        """Classify one target through its exact current owner."""
        ...


class _DeliverTarget(Protocol):
    async def __call__(
        self,
        router: RecoveryOwner,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> bool:
        """Deliver one target through the current router."""
        ...


@dataclass(frozen=True)
class RestartRecoveryOperations:
    """External operations used by the coordinator."""

    recover_room: _RecoverRoom
    target_freshness: _TargetFreshness
    deliver_target: _DeliverTarget
    discard_owner: Callable[[str], None]


@dataclass(frozen=True)
class _OwnerMembershipSnapshot:
    """One joined-room lookup bound to its retained owner generation."""

    generation: object
    task: asyncio.Task[list[str] | None]


@dataclass
class _OwnerMembershipSnapshots:
    """Share joined-room discovery across one exact owner generation."""

    snapshots: dict[str, _OwnerMembershipSnapshot] = field(default_factory=dict)

    async def joined_rooms(self, owner: RecoveryOwner) -> list[str] | None:
        """Return one generation snapshot, creating it on first use."""
        snapshot = self.snapshots.get(owner.user_id)
        if snapshot is None or snapshot.generation is not owner.generation:
            snapshot = _OwnerMembershipSnapshot(
                generation=owner.generation,
                task=asyncio.create_task(
                    get_joined_rooms(owner.client),
                    name=f"restart_recovery_membership:{owner.user_id}",
                ),
            )
            self.snapshots[owner.user_id] = snapshot
        try:
            return await snapshot.task
        except asyncio.CancelledError:
            self._discard(owner.user_id, snapshot)
            raise
        except Exception:
            self._discard(owner.user_id, snapshot)
            raise

    def invalidate(self, owner: RecoveryOwner) -> None:
        """Discard a snapshot that did not contain one desired room."""
        snapshot = self.snapshots.get(owner.user_id)
        if snapshot is not None and snapshot.generation is owner.generation:
            self.snapshots.pop(owner.user_id)

    def discard_owner(self, owner_user_id: str) -> None:
        """Release one removed owner's retained generation snapshot."""
        snapshot = self.snapshots.pop(owner_user_id, None)
        if snapshot is not None and not snapshot.task.done():
            snapshot.task.cancel()

    def _discard(
        self,
        owner_user_id: str,
        snapshot: _OwnerMembershipSnapshot,
    ) -> None:
        if self.snapshots.get(owner_user_id) is snapshot:
            self.snapshots.pop(owner_user_id)


def build_matrix_restart_recovery_operations(runtime_paths: RuntimePaths) -> RestartRecoveryOperations:
    """Build exact-owner Matrix operations for restart recovery."""
    membership_snapshots = _OwnerMembershipSnapshots()

    async def recover_room(
        owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        owner_user_ids: frozenset[str],
        config: Config,
    ) -> _RoomRecoveryResult:
        try:
            joined_room_ids = await membership_snapshots.joined_rooms(owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Failed to list owner rooms during restart recovery",
                owner_user_id=owner.user_id,
                exc_info=True,
            )
            return _RoomRecoveryResult(retry=True)
        if joined_room_ids is None or request.room_id not in joined_room_ids:
            membership_snapshots.invalidate(owner)
            return _RoomRecoveryResult(retry=True)

        cleanup_result = await cleanup_stale_streaming_room(
            owner.client,
            room_id=request.room_id,
            actors={
                owner.user_id: StaleStreamCleanupActor(
                    client=owner.client,
                    conversation_cache=owner.conversation_cache,
                ),
            },
            bot_user_ids=set(owner_user_ids),
            config=config,
            runtime_paths=runtime_paths,
            startup_cutoff_ms=request.startup_cutoff_ms,
            terminal_interrupted_only=request.terminal_interrupted_only,
        )
        return _RoomRecoveryResult(
            interrupted_threads=cleanup_result.interrupted_threads,
            retry=cleanup_result.retry_required,
        )

    async def target_freshness(
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> RestartTargetFreshness:
        freshness = await interrupted_target_freshness(
            target,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=owner.conversation_cache,
        )
        return {
            InterruptedTargetFreshness.CURRENT: RestartTargetFreshness.CURRENT,
            InterruptedTargetFreshness.NEWER_HUMAN: RestartTargetFreshness.NEWER_HUMAN,
            InterruptedTargetFreshness.UNRECOVERABLE: RestartTargetFreshness.UNRECOVERABLE,
            InterruptedTargetFreshness.RETRY: RestartTargetFreshness.RETRY,
        }[freshness]

    async def deliver_target(
        router: RecoveryOwner,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> bool:
        content = build_auto_resume_content(
            target,
            config=config,
            runtime_paths=runtime_paths,
            target_user_id=owner.user_id,
            sender_is_owner=router.user_id == owner.user_id,
        )
        transaction_id = str(
            uuid5(
                NAMESPACE_URL,
                "\x00".join(
                    (
                        "mindroom.restart_recovery.v1",
                        owner.user_id,
                        target.room_id,
                        target.thread_id or "",
                        target.target_event_id,
                        str(target.timestamp_ms),
                    ),
                ),
            ),
        )
        delivered = await send_message_result(
            router.client,
            target.room_id,
            content,
            transaction_id=transaction_id,
        )
        if delivered is None:
            return False
        try:
            router.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
        except Exception:
            logger.warning(
                "Failed to record queued auto-resume message",
                room_id=target.room_id,
                event_id=delivered.event_id,
                exc_info=True,
            )
        logger.info(
            "Queued auto-resume after restart",
            room_id=target.room_id,
            thread_id=target.thread_id,
            target_event_id=target.target_event_id,
            event_id=delivered.event_id,
        )
        return True

    return RestartRecoveryOperations(
        recover_room=recover_room,
        target_freshness=target_freshness,
        deliver_target=deliver_target,
        discard_owner=membership_snapshots.discard_owner,
    )
