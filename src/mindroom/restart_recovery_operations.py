"""Exact-owner Matrix operations for restart recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedTargetFreshness,
    StaleStreamCleanupActor,
    StaleStreamCleanupResult,
    build_auto_resume_content,
    cleanup_stale_streaming_room,
    interrupted_target_freshness,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.matrix.stale_stream_cleanup import InterruptedThread

logger = get_logger(__name__)

_AUTO_RESUME_DELIVERY_INTERVAL_SECONDS = 2.0
_OWNER_MEMBERSHIP_REFRESH_BACKOFF_SECONDS = 2.0


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
) -> dict[str, RecoveryOwner]:
    """Snapshot current exact owner generations and their durable room scope."""
    owners: dict[str, RecoveryOwner] = {}
    for bot in bots.values():
        client = bot.client
        user_id = bot.agent_user.user_id
        if client is None or not user_id:
            continue
        owners[user_id] = RecoveryOwner(
            entity_name=bot.agent_name,
            user_id=user_id,
            generation=bot,
            client=client,
            conversation_cache=bot.conversation_cache,
            desired_room_ids=bot.restart_recovery_room_ids,
            first_sync_complete=bot.running and bot.first_sync_complete,
        )
    return owners


@dataclass(frozen=True)
class RoomRecoveryRequest:
    """One semantic room scan request shared by exact owners."""

    room_id: str
    startup_cutoff_ms: int | None
    terminal_interrupted_only: bool


@dataclass(frozen=True)
class RoomRecoveryResult:
    """Result of one shared room recovery attempt."""

    interrupted_threads: tuple[InterruptedThread, ...] = ()
    retry_owner_user_ids: frozenset[str] = frozenset()


class RestartDeliveryOutcome(Enum):
    """Settlement state from one admitted resume delivery."""

    DELIVERED = auto()
    TERMINAL = auto()
    RETRY = auto()


type _RecoverRoom = Callable[
    [tuple[RecoveryOwner, ...], RoomRecoveryRequest, frozenset[str], Config],
    Awaitable[RoomRecoveryResult],
]
type _TargetFreshness = Callable[
    [RecoveryOwner, InterruptedThread, Config],
    Awaitable[InterruptedTargetFreshness],
]
type _DeliverTarget = Callable[
    [RecoveryOwner, RecoveryOwner, InterruptedThread, Config],
    Coroutine[Any, Any, RestartDeliveryOutcome],
]


@dataclass(frozen=True)
class RestartRecoveryOperations:
    """External operations used by the coordinator."""

    recover_room: _RecoverRoom
    target_freshness: _TargetFreshness
    deliver_target: _DeliverTarget
    discard_owner: Callable[[str], None]
    close: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class _OwnerMembershipSnapshot:
    """One joined-room lookup bound to its retained owner generation."""

    generation: object
    task: asyncio.Task[list[str] | None]
    refresh_after: float | None = None


@dataclass
class _OwnerMembershipSnapshots:
    """Share joined-room discovery across one exact owner generation."""

    snapshots: dict[str, _OwnerMembershipSnapshot] = field(default_factory=dict)

    async def joined_rooms(self, owner: RecoveryOwner) -> list[str] | None:
        """Return one generation snapshot, creating it on first use."""
        snapshot = self.snapshots.get(owner.user_id)
        if (
            snapshot is None
            or snapshot.generation is not owner.generation
            or (snapshot.refresh_after is not None and snapshot.refresh_after <= asyncio.get_running_loop().time())
        ):
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
        except BaseException:
            self._discard(owner.user_id, snapshot)
            raise

    def invalidate(self, owner: RecoveryOwner) -> None:
        """Delay one owner-level refresh after a desired room is absent."""
        snapshot = self.snapshots.get(owner.user_id)
        if snapshot is not None and snapshot.generation is owner.generation and snapshot.refresh_after is None:
            self.snapshots[owner.user_id] = replace(
                snapshot,
                refresh_after=(asyncio.get_running_loop().time() + _OWNER_MEMBERSHIP_REFRESH_BACKOFF_SECONDS),
            )

    def discard_owner(self, owner_user_id: str) -> None:
        """Release one removed owner's retained generation snapshot."""
        snapshot = self.snapshots.pop(owner_user_id, None)
        if snapshot is not None and not snapshot.task.done():
            snapshot.task.cancel()

    async def close(self) -> None:
        """Cancel and drain every retained membership snapshot."""
        snapshots = tuple(self.snapshots.values())
        self.snapshots.clear()
        for snapshot in snapshots:
            if not snapshot.task.done():
                snapshot.task.cancel()
        if snapshots:
            await asyncio.gather(
                *(snapshot.task for snapshot in snapshots),
                return_exceptions=True,
            )

    def _discard(
        self,
        owner_user_id: str,
        snapshot: _OwnerMembershipSnapshot,
    ) -> None:
        if self.snapshots.get(owner_user_id) is snapshot:
            self.snapshots.pop(owner_user_id)


async def _recover_room(
    membership_snapshots: _OwnerMembershipSnapshots,
    runtime_paths: RuntimePaths,
    owners: tuple[RecoveryOwner, ...],
    request: RoomRecoveryRequest,
    owner_user_ids: frozenset[str],
    config: Config,
) -> RoomRecoveryResult:
    """Recover joined owners now while retaining only unavailable owners."""
    assert owners
    membership_results = await asyncio.gather(
        *(membership_snapshots.joined_rooms(owner) for owner in owners),
        return_exceptions=True,
    )
    joined_owners: list[RecoveryOwner] = []
    retry_owner_user_ids: set[str] = set()
    for owner, joined_room_ids in zip(owners, membership_results):
        if isinstance(joined_room_ids, asyncio.CancelledError):
            raise joined_room_ids
        if isinstance(joined_room_ids, BaseException):
            logger.warning(
                "Failed to list owner rooms during restart recovery",
                owner_user_id=owner.user_id,
                exc_info=(
                    type(joined_room_ids),
                    joined_room_ids,
                    joined_room_ids.__traceback__,
                ),
            )
            retry_owner_user_ids.add(owner.user_id)
            continue
        if joined_room_ids is None or request.room_id not in joined_room_ids:
            membership_snapshots.invalidate(owner)
            retry_owner_user_ids.add(owner.user_id)
            continue
        joined_owners.append(owner)

    if not joined_owners:
        return RoomRecoveryResult(
            retry_owner_user_ids=frozenset(retry_owner_user_ids),
        )

    scan_owner = next(
        (owner for owner in joined_owners if owner.entity_name == ROUTER_AGENT_NAME),
        min(joined_owners, key=lambda owner: owner.user_id),
    )
    cleanup_result: StaleStreamCleanupResult = await cleanup_stale_streaming_room(
        scan_owner.client,
        room_id=request.room_id,
        actors={
            owner.user_id: StaleStreamCleanupActor(
                client=owner.client,
                conversation_cache=owner.conversation_cache,
            )
            for owner in joined_owners
        },
        bot_user_ids=set(owner_user_ids),
        config=config,
        runtime_paths=runtime_paths,
        startup_cutoff_ms=request.startup_cutoff_ms,
        terminal_interrupted_only=request.terminal_interrupted_only,
    )
    retry_cleanup_owner_user_ids = set(cleanup_result.retry_bot_user_ids)
    retry_cleanup_owner_user_ids.update(retry_owner_user_ids)
    if cleanup_result.room_retry_required:
        retry_cleanup_owner_user_ids.update(owner.user_id for owner in joined_owners)
    logger.info(
        "Restart recovery room scan completed",
        cleaned_count=cleanup_result.cleaned_count,
        interrupted_count=len(cleanup_result.interrupted_threads),
        retry_owner_count=len(retry_cleanup_owner_user_ids),
        room_id=request.room_id,
    )
    return RoomRecoveryResult(
        interrupted_threads=cleanup_result.interrupted_threads,
        retry_owner_user_ids=frozenset(retry_cleanup_owner_user_ids),
    )


def build_matrix_restart_recovery_operations(runtime_paths: RuntimePaths) -> RestartRecoveryOperations:
    """Build exact-owner Matrix operations for restart recovery."""
    membership_snapshots = _OwnerMembershipSnapshots()
    next_delivery_at = 0.0

    async def recover_room(
        owners: tuple[RecoveryOwner, ...],
        request: RoomRecoveryRequest,
        owner_user_ids: frozenset[str],
        config: Config,
    ) -> RoomRecoveryResult:
        return await _recover_room(
            membership_snapshots,
            runtime_paths,
            owners,
            request,
            owner_user_ids,
            config,
        )

    async def target_freshness(
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> InterruptedTargetFreshness:
        return await interrupted_target_freshness(
            target,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=owner.conversation_cache,
        )

    async def deliver_target(
        sender: RecoveryOwner,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> RestartDeliveryOutcome:
        nonlocal next_delivery_at
        delay = next_delivery_at - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
            freshness = await target_freshness(owner, target, config)
            if freshness is InterruptedTargetFreshness.RETRY:
                return RestartDeliveryOutcome.RETRY
            if freshness is not InterruptedTargetFreshness.CURRENT:
                return RestartDeliveryOutcome.TERMINAL
        content = build_auto_resume_content(
            target,
            config=config,
            mention_user_id=(None if sender.user_id == owner.user_id else owner.user_id),
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
        try:
            delivered = await send_message_result(
                sender.client,
                target.room_id,
                content,
                transaction_id=transaction_id,
            )
        finally:
            next_delivery_at = asyncio.get_running_loop().time() + _AUTO_RESUME_DELIVERY_INTERVAL_SECONDS
        if delivered is None:
            return RestartDeliveryOutcome.RETRY
        sender.conversation_cache.notify_outbound_message(
            target.room_id,
            delivered.event_id,
            delivered.content_sent,
        )
        logger.info(
            "Queued auto-resume after restart",
            room_id=target.room_id,
            thread_id=target.thread_id,
            target_event_id=target.target_event_id,
            event_id=delivered.event_id,
        )
        return RestartDeliveryOutcome.DELIVERED

    return RestartRecoveryOperations(
        recover_room=recover_room,
        target_freshness=target_freshness,
        deliver_target=deliver_target,
        discard_owner=membership_snapshots.discard_owner,
        close=membership_snapshots.close,
    )
