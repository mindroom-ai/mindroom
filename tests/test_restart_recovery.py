"""Tests for serialized restart-recovery coordination."""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import weakref
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import restart_recovery_operations as restart_recovery_operations_module
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.client_delivery import hydrate_joined_room_for_delivery
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedTargetFreshness,
    InterruptedThread,
)
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.restart_recovery import RestartRecoveryCoordinator
from mindroom.restart_recovery import (
    _OwnerRoomWork as RoomWork,
)
from mindroom.restart_recovery import (
    _restart_recovery_retry_delay as restart_recovery_retry_delay,
)
from mindroom.restart_recovery import (
    _RoomLease as RoomLease,
)
from mindroom.restart_recovery import (
    _TargetSettlement as TargetSettlement,
)
from mindroom.restart_recovery_operations import (
    RecoveryOwner,
    RestartDeliveryOutcome,
    RestartRecoveryOperations,
    RoomRecoveryRequest,
    build_matrix_restart_recovery_operations,
)
from mindroom.restart_recovery_operations import (
    _RoomRecoveryResult as RoomRecoveryResult,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    test_runtime_paths,
)
from tests.identity_helpers import agent_matrix_user

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, ValuesView
    from pathlib import Path

    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

    type _RecoverRoom = Callable[
        [RecoveryOwner, RoomRecoveryRequest, frozenset[str], Config],
        Awaitable[RoomRecoveryResult],
    ]
    type _TargetFreshness = Callable[
        [RecoveryOwner, InterruptedThread, Config],
        Awaitable[InterruptedTargetFreshness],
    ]
    type _DeliverTarget = Callable[
        [RecoveryOwner, InterruptedThread, Config],
        Awaitable[bool],
    ]


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"code": {"display_name": "Code", "rooms": ["!code:example.org"]}},
            defaults={"auto_resume_after_restart": True},
            authorization={"default_room_access": True, "agent_reply_permissions": {}},
            mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
        ),
        test_runtime_paths(tmp_path),
    )


def _owner(
    *,
    user_id: str = "@code:example.org",
    generation: object | None = None,
    rooms: frozenset[str] = frozenset({"!code:example.org"}),
    ready: bool = True,
) -> RecoveryOwner:
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = user_id
    client.rooms = {room_id: nio.MatrixRoom(room_id=room_id, own_user_id=user_id) for room_id in rooms}
    client.encrypted_rooms = set()
    client.sharing_session = {}
    client.store = None
    client.olm = None
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND"),
    )
    return RecoveryOwner(
        user_id=user_id,
        generation=object() if generation is None else generation,
        client=client,
        conversation_cache=cast("ConversationCacheProtocol", MagicMock()),
        desired_room_ids=rooms,
        first_sync_complete=ready,
    )


def _hide_encrypted_room(
    owner: RecoveryOwner,
    room_id: str,
    *,
    members: nio.JoinedMembersResponse | nio.JoinedMembersError | None = None,
) -> None:
    owner.client.rooms = {}
    joined_members = members if isinstance(members, nio.JoinedMembersResponse) else None
    member_events = [
        {
            "content": {"displayname": member.display_name, "membership": "join"},
            "event_id": f"$join-{index}",
            "origin_server_ts": index + 2,
            "sender": member.user_id,
            "state_key": member.user_id,
            "type": "m.room.member",
        }
        for index, member in enumerate(joined_members.members if joined_members is not None else [])
    ]
    owner.client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"algorithm": "m.megolm.v1.aes-sha2"},
            event_type="m.room.encryption",
            state_key="",
            room_id=room_id,
        ),
    )
    owner.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[
                {
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    "event_id": "$encryption",
                    "origin_server_ts": 1,
                    "sender": owner.user_id,
                    "state_key": "",
                    "type": "m.room.encryption",
                },
                *member_events,
            ],
            room_id=room_id,
        ),
    )
    owner.client.joined_members = AsyncMock(
        return_value=(members if members is not None else nio.JoinedMembersResponse(members=[], room_id=room_id)),
    )
    owner.client.should_query_keys = False


def _target(
    target_event_id: str,
    *,
    timestamp_ms: int,
    room_id: str = "!code:example.org",
    thread_id: str = "$thread",
    agent_name: str = "code",
    owner_user_id: str = "@code:example.org",
    original_sender_id: str | None = "@alice:example.org",
    same_timestamp_older_event_ids: frozenset[str] = frozenset(),
) -> InterruptedThread:
    return InterruptedThread(
        room_id=room_id,
        thread_id=thread_id,
        target_event_id=target_event_id,
        partial_text="Partial",
        agent_name=agent_name,
        owner_user_id=owner_user_id,
        original_sender_id=original_sender_id,
        timestamp_ms=timestamp_ms,
        same_timestamp_older_event_ids=same_timestamp_older_event_ids,
    )


def _operations(
    *,
    recover_room: _RecoverRoom,
    freshness: _TargetFreshness | None = None,
    deliver: _DeliverTarget | None = None,
) -> RestartRecoveryOperations:
    async def recover_batch(
        owners: tuple[RecoveryOwner, ...],
        request: RoomRecoveryRequest,
        owner_user_ids: frozenset[str],
        config: Config,
    ) -> RoomRecoveryResult:
        results = [await recover_room(owner, request, owner_user_ids, config) for owner in owners]
        return RoomRecoveryResult(
            interrupted_threads=tuple(target for result in results for target in result.interrupted_threads),
            auto_resume_target_keys=frozenset(key for result in results for key in result.auto_resume_target_keys),
            retry_owner_user_ids=frozenset(
                owner_user_id for result in results for owner_user_id in result.retry_owner_user_ids
            ),
            unjoined_owner_user_ids=frozenset(
                owner_user_id for result in results for owner_user_id in result.unjoined_owner_user_ids
            ),
        )

    async def drain_membership_snapshots() -> None:
        return None

    async def joined_rooms(owner: RecoveryOwner) -> list[str]:
        return list(owner.desired_room_ids)

    async def current(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        return InterruptedTargetFreshness.CURRENT

    async def delivered(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        return True

    async def delivery_outcome(
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> restart_recovery_operations_module.RestartDeliveryOutcome:
        succeeded = await (deliver or delivered)(owner, target, config)
        if succeeded:
            return restart_recovery_operations_module.RestartDeliveryOutcome.DELIVERED
        return restart_recovery_operations_module.RestartDeliveryOutcome.RETRY

    return RestartRecoveryOperations(
        joined_rooms=joined_rooms,
        membership_refresh_delay_seconds=0.0,
        recover_room=recover_batch,
        target_freshness=freshness or current,
        deliver_target=delivery_outcome,
        discard_owner=lambda _owner_user_id: None,
        drain_membership_snapshots=drain_membership_snapshots,
    )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """Yield until one deterministic coordinator condition becomes true."""
    async with asyncio.timeout(1.0):
        while not predicate():  # noqa: ASYNC110 - deterministic scheduler probe
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("attempt", "expected_seconds"),
    [(1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 32.0), (6, 60.0), (20, 60.0)],
)
def test_restart_recovery_retry_delay_caps(attempt: int, expected_seconds: float) -> None:
    """Retry pacing must grow exponentially without exceeding one minute."""
    assert restart_recovery_retry_delay(attempt) == expected_seconds


@pytest.mark.asyncio
async def test_matrix_room_recovery_defers_until_exact_owner_is_joined(tmp_path: Path) -> None:
    """A desired room hidden from the owner client must wait for membership."""
    owner = _owner()
    config = _config(tmp_path)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    request = RoomRecoveryRequest(
        room_id="!missing:example.org",
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!other:example.org"]),
        ),
        patch("mindroom.restart_recovery_operations.cleanup_stale_streaming_room", new=AsyncMock()) as cleanup_room,
    ):
        result = await operations.recover_room(
            (owner,),
            request,
            frozenset({owner.user_id}),
            config,
        )

    assert result == RoomRecoveryResult(
        unjoined_owner_user_ids=frozenset({owner.user_id}),
    )
    cleanup_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_room_scans_joined_owner_and_retries_only_missing_owner(
    tmp_path: Path,
) -> None:
    """One missing membership must not block recovery for a joined owner."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset({room_id}),
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    request = RoomRecoveryRequest(
        room_id=room_id,
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
    )

    async def joined_rooms(client: nio.AsyncClient) -> list[str]:
        return [room_id] if client is owner.client else []

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(side_effect=joined_rooms),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=0,
                    interrupted_threads=(),
                ),
            ),
        ) as cleanup_room,
    ):
        result = await operations.recover_room(
            (owner, router),
            request,
            frozenset({owner.user_id, router.user_id}),
            _config(tmp_path),
        )

    assert result == RoomRecoveryResult(
        unjoined_owner_user_ids=frozenset({router.user_id}),
    )
    cleanup_room.assert_awaited_once()
    assert cleanup_room.await_args.kwargs["owner_user_id"] == owner.user_id


@pytest.mark.asyncio
async def test_matrix_room_recovery_scans_only_exact_owner_messages(tmp_path: Path) -> None:
    """One semantic room job must edit only messages authored by its exact owner."""
    owner = _owner()
    other_owner = _owner(user_id="@other:example.org")
    config = _config(tmp_path)
    interrupted = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    request = RoomRecoveryRequest(
        room_id="!code:example.org",
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=1,
                    interrupted_threads=(interrupted,),
                ),
            ),
        ) as cleanup_room,
    ):
        result = await operations.recover_room(
            (owner,),
            request,
            frozenset({owner.user_id, other_owner.user_id}),
            config,
        )

    assert result == RoomRecoveryResult(interrupted_threads=(interrupted,))
    cleanup_room.assert_awaited_once()
    assert cleanup_room.await_args.args[0].client is owner.client
    assert cleanup_room.await_args.kwargs["owner_user_id"] == owner.user_id
    assert cleanup_room.await_args.kwargs["bot_user_ids"] == {owner.user_id, other_owner.user_id}


@pytest.mark.asyncio
async def test_shared_room_startup_scans_through_each_joined_owner(tmp_path: Path) -> None:
    """Each owner must scan history that Matrix may expose only to that user."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset({room_id}),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=[room_id]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=0,
                    interrupted_threads=(),
                ),
            ),
        ) as cleanup_room,
    ):
        coordinator = RestartRecoveryCoordinator(
            current_config=lambda: _config(tmp_path),
            current_owners=lambda: owners,
            operations=operations,
        )
        coordinator.start(startup_cutoff_ms=123)
        try:
            await _wait_until(
                lambda: not coordinator._room_jobs and all(task.done() for task in coordinator._active_attempts),
            )
        finally:
            await coordinator.stop()

    assert cleanup_room.await_count == 2
    assert {call.args[0].client for call in cleanup_room.await_args_list} == {
        owner.client,
        router.client,
    }
    assert {call.kwargs["owner_user_id"] for call in cleanup_room.await_args_list} == set(owners)


@pytest.mark.asyncio
async def test_shared_room_merges_targets_visible_to_different_owners(tmp_path: Path) -> None:
    """Per-user history visibility must not hide one co-resident owner's stale target."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset({room_id}),
    )
    owner_target = _target("$owner-target", timestamp_ms=10)
    router_target = _target(
        "$router-target",
        timestamp_ms=20,
        agent_name=ROUTER_AGENT_NAME,
        owner_user_id=router.user_id,
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def cleanup(
        actor: stale_stream_cleanup_module.StaleStreamCleanupActor,
        **_kwargs: object,
    ) -> stale_stream_cleanup_module.StaleStreamCleanupResult:
        target = owner_target if actor.client is owner.client else router_target
        return stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=1,
            interrupted_threads=(target,),
        )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=[room_id]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(side_effect=cleanup),
        ) as cleanup_room,
    ):
        result = await operations.recover_room(
            (owner, router),
            RoomRecoveryRequest(room_id, 123, False),
            frozenset({owner.user_id, router.user_id}),
            _config(tmp_path),
        )

    assert cleanup_room.await_count == 2
    assert set(result.interrupted_threads) == {owner_target, router_target}


@pytest.mark.asyncio
async def test_shared_room_scans_ready_owner_and_retries_only_unready_owner(
    tmp_path: Path,
) -> None:
    """One unready owner must not block a ready co-owner's room scan."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset({room_id}),
        ready=False,
    )
    owners = {owner.user_id: owner, router.user_id: router}
    scanned_owner_user_ids: list[str] = []

    async def recover_room(
        scan_owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        scanned_owner_user_ids.append(scan_owner.user_id)
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    request = RoomRecoveryRequest(room_id, 123, False)
    result = await coordinator._process_room(
        RoomLease(
            request,
            (
                RoomWork(request, owner.user_id, owner.generation, due_at=None),
                RoomWork(request, router.user_id, router.generation, due_at=None),
            ),
        ),
    )

    assert scanned_owner_user_ids == [owner.user_id]
    assert {outcome.work.owner_user_id for outcome in result if outcome.readiness_unavailable} == {
        router.user_id,
    }


@pytest.mark.asyncio
async def test_matrix_room_recovery_propagates_cleanup_retry_requirement(
    tmp_path: Path,
) -> None:
    """A room-wide retry must retain already-recovered targets."""
    owner = _owner()
    config = _config(tmp_path)
    interrupted = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    request = RoomRecoveryRequest(
        room_id="!code:example.org",
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
    )
    cleanup_result = stale_stream_cleanup_module.StaleStreamCleanupResult(
        cleaned_count=1,
        interrupted_threads=(interrupted,),
        retry_required=True,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(return_value=cleanup_result),
        ),
    ):
        result = await operations.recover_room(
            (owner,),
            request,
            frozenset({owner.user_id}),
            config,
        )

    assert result == RoomRecoveryResult(
        interrupted_threads=(interrupted,),
        retry_owner_user_ids=frozenset({owner.user_id}),
    )


@pytest.mark.asyncio
async def test_matrix_room_recovery_retries_only_failed_cleanup_owner(
    tmp_path: Path,
) -> None:
    """One owner's failed edit must not retain a healthy co-resident owner."""
    healthy_owner = _owner(
        user_id="@healthy:example.org",
    )
    failed_owner = _owner(
        user_id="@failed:example.org",
    )
    interrupted = _target(
        "$healthy-target",
        timestamp_ms=10,
        agent_name="healthy",
        owner_user_id=healthy_owner.user_id,
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def cleanup(
        actor: stale_stream_cleanup_module.StaleStreamCleanupActor,
        **_kwargs: object,
    ) -> stale_stream_cleanup_module.StaleStreamCleanupResult:
        if actor.client is healthy_owner.client:
            return stale_stream_cleanup_module.StaleStreamCleanupResult(
                cleaned_count=1,
                interrupted_threads=(interrupted,),
            )
        return stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
            retry_required=True,
        )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(side_effect=cleanup),
        ),
        patch("mindroom.restart_recovery_operations.logger.info") as info,
    ):
        result = await operations.recover_room(
            (healthy_owner, failed_owner),
            RoomRecoveryRequest(
                room_id="!code:example.org",
                startup_cutoff_ms=123,
                terminal_interrupted_only=False,
            ),
            frozenset({healthy_owner.user_id, failed_owner.user_id}),
            _config(tmp_path),
        )

    assert result == RoomRecoveryResult(
        interrupted_threads=(interrupted,),
        retry_owner_user_ids=frozenset({failed_owner.user_id}),
    )
    info.assert_called_once_with(
        "Restart recovery room scan completed",
        cleaned_count=1,
        interrupted_count=1,
        retry_owner_count=1,
        room_id="!code:example.org",
    )


@pytest.mark.asyncio
async def test_matrix_room_cleanup_exception_retries_only_failed_owner(
    tmp_path: Path,
) -> None:
    """One owner's cleanup exception must not skip a healthy later owner."""
    failed_owner = _owner(
        user_id="@a-failed:example.org",
    )
    healthy_owner = _owner(
        user_id="@z-healthy:example.org",
    )
    healthy_target = _target(
        "$healthy-target",
        timestamp_ms=10,
        agent_name="healthy",
        owner_user_id=healthy_owner.user_id,
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def cleanup(
        actor: stale_stream_cleanup_module.StaleStreamCleanupActor,
        **_kwargs: object,
    ) -> stale_stream_cleanup_module.StaleStreamCleanupResult:
        if actor.client is failed_owner.client:
            msg = "failed owner history unavailable"
            raise RuntimeError(msg)
        return stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=1,
            interrupted_threads=(healthy_target,),
        )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(side_effect=cleanup),
        ) as cleanup_room,
        patch("mindroom.restart_recovery_operations.logger.warning") as warning,
    ):
        result = await operations.recover_room(
            (failed_owner, healthy_owner),
            RoomRecoveryRequest("!code:example.org", 123, False),
            frozenset({failed_owner.user_id, healthy_owner.user_id}),
            _config(tmp_path),
        )

    assert cleanup_room.await_count == 2
    assert result == RoomRecoveryResult(
        interrupted_threads=(healthy_target,),
        retry_owner_user_ids=frozenset({failed_owner.user_id}),
    )
    warning.assert_called_once_with(
        "Failed to clean owner history during restart recovery",
        owner_user_id=failed_owner.user_id,
        room_id="!code:example.org",
        exc_info=True,
    )


@pytest.mark.asyncio
async def test_matrix_room_recovery_hydrates_hidden_encrypted_room_before_cleanup(
    tmp_path: Path,
) -> None:
    """Stale edits must see encrypted delivery state before cleanup starts."""
    owner = _owner()
    room_id = "!code:example.org"
    _hide_encrypted_room(owner, room_id)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def cleanup(*_args: object, **_kwargs: object) -> stale_stream_cleanup_module.StaleStreamCleanupResult:
        assert owner.client.rooms[room_id].encrypted is True
        return stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=[room_id]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(side_effect=cleanup),
        ) as cleanup_room,
    ):
        result = await operations.recover_room(
            (owner,),
            RoomRecoveryRequest(
                room_id=room_id,
                startup_cutoff_ms=123,
                terminal_interrupted_only=False,
            ),
            frozenset({owner.user_id}),
            _config(tmp_path),
        )

    assert result == RoomRecoveryResult()
    cleanup_room.assert_awaited_once()
    owner.client.joined_members.assert_awaited_once_with(room_id)


@pytest.mark.asyncio
async def test_matrix_target_delivery_by_exact_owner_mentions_intended_responder(
    tmp_path: Path,
) -> None:
    """An owner-authored resume must explicitly target its exact responder account."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
        ),
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ) as send_message,
    ):
        delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is restart_recovery_operations_module.RestartDeliveryOutcome.DELIVERED
    content = send_message.await_args.args[2]
    assert content["body"] == (
        "@Code [System: Previous response was interrupted by service restart. Please continue where you left off.]"
    )
    assert content["m.mentions"] == {"user_ids": [owner.user_id]}


@pytest.mark.asyncio
async def test_matrix_target_delivery_hydrates_hidden_encrypted_room(
    tmp_path: Path,
) -> None:
    """A joined room outside the Sliding Sync window must remain deliverable."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    _hide_encrypted_room(owner, target.room_id)
    owner.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(
            event_id="$resume",
            room_id=target.room_id,
        ),
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with patch(
        "mindroom.restart_recovery_operations.interrupted_target_freshness",
        new=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
    ):
        delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is RestartDeliveryOutcome.DELIVERED
    assert owner.client.rooms[target.room_id].encrypted is True
    assert target.room_id in owner.client.encrypted_rooms
    owner.client.room_get_state_event.assert_awaited_once_with(
        target.room_id,
        "m.room.encryption",
    )
    owner.client.room_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_plaintext_delivery_hydration_does_not_publish_empty_room() -> None:
    """A plaintext send must use cache bypass instead of publishing incomplete membership."""
    owner = _owner()
    room_id = "!hidden-plaintext:example.org"
    owner.client.rooms = {}
    owner.client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError(
            "No encryption state",
            "M_NOT_FOUND",
            room_id=room_id,
        ),
    )

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is not None
    assert hydrated.encrypted is False
    assert room_id not in owner.client.rooms


@pytest.mark.asyncio
async def test_matrix_target_delivery_does_not_publish_incomplete_encrypted_room(
    tmp_path: Path,
) -> None:
    """Failed member hydration must not leave a sendable-looking cache entry."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    _hide_encrypted_room(
        owner,
        target.room_id,
        members=nio.JoinedMembersError(
            "forbidden",
            "M_FORBIDDEN",
            room_id=target.room_id,
        ),
    )
    owner.client.room_send = AsyncMock()
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is RestartDeliveryOutcome.RETRY
    assert target.room_id not in owner.client.rooms
    assert target.room_id not in owner.client.encrypted_rooms
    owner.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_room_hydration_does_not_publish_after_failed_state_query() -> None:
    """Failed full-state hydration must not leave a partial shared cache entry."""
    owner = _owner()
    room_id = "!hidden:example.org"
    _hide_encrypted_room(owner, room_id)
    owner.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateError(
            "unavailable",
            "M_UNKNOWN",
            room_id=room_id,
        ),
    )

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is None
    assert room_id not in owner.client.rooms
    assert room_id not in owner.client.encrypted_rooms


@pytest.mark.parametrize("members_synced", [False, True])
@pytest.mark.asyncio
async def test_encrypted_room_hydration_rejects_partial_room_from_state_query_race(
    *,
    members_synced: bool,
) -> None:
    """A sync room racing full-state hydration must cover authoritative members."""
    owner = _owner()
    room_id = "!hidden:example.org"
    remote_member_id = "@human:remote.example.org"
    _hide_encrypted_room(
        owner,
        room_id,
        members=nio.JoinedMembersResponse(
            members=[nio.RoomMember(remote_member_id, "Human", None)],
            room_id=room_id,
        ),
    )
    state_response = owner.client.room_get_state.return_value
    concurrent_room = nio.MatrixRoom(room_id=room_id, own_user_id=owner.user_id)
    concurrent_room.encrypted = True
    concurrent_room.members_synced = members_synced

    async def room_get_state(_room_id: str) -> nio.RoomGetStateResponse:
        owner.client.rooms[room_id] = concurrent_room
        return state_response

    owner.client.room_get_state = AsyncMock(side_effect=room_get_state)

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is None
    assert owner.client.rooms[room_id] is concurrent_room
    assert remote_member_id not in concurrent_room.users
    assert concurrent_room.members_synced is False
    assert room_id not in owner.client.encrypted_rooms


@pytest.mark.asyncio
async def test_cancelled_encrypted_room_hydration_stops_before_cache_publication() -> None:
    """Cancellation before hydration completes must not publish a room."""
    owner = _owner()
    room_id = "!hidden:example.org"
    _hide_encrypted_room(owner, room_id)
    hydration_started = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        hydration_started.set()
        await asyncio.Event().wait()
        return nio.JoinedMembersResponse(members=[], room_id=room_id)

    owner.client.joined_members.side_effect = joined_members
    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(owner.client, room_id))
    await asyncio.wait_for(hydration_started.wait(), timeout=1.0)

    hydration.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hydration

    assert room_id not in owner.client.rooms
    assert room_id not in owner.client.encrypted_rooms


@pytest.mark.asyncio
async def test_cancelled_hydration_preserves_room_adopted_by_concurrent_sync() -> None:
    """Cancellation before publication must preserve concurrent nio sync state."""
    owner = _owner()
    room_id = "!hidden:example.org"
    _hide_encrypted_room(owner, room_id)
    hydration_started = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        hydration_started.set()
        await asyncio.Event().wait()
        return nio.JoinedMembersResponse(members=[], room_id=room_id)

    owner.client.joined_members.side_effect = joined_members
    hydration = asyncio.create_task(hydrate_joined_room_for_delivery(owner.client, room_id))
    await asyncio.wait_for(hydration_started.wait(), timeout=1.0)

    sync_room = owner.client.rooms.get(room_id)
    if sync_room is None:
        sync_room = nio.MatrixRoom(room_id=room_id, own_user_id=owner.user_id)
        owner.client.rooms[room_id] = sync_room
    sync_room.encrypted = True
    sync_room.add_member("@human:example.org", "Human", None)
    owner.client.encrypted_rooms.add(room_id)

    hydration.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hydration

    assert owner.client.rooms[room_id] is sync_room
    assert "@human:example.org" in sync_room.users
    assert room_id in owner.client.encrypted_rooms


@pytest.mark.asyncio
async def test_encrypted_room_hydration_queries_new_member_device_keys() -> None:
    """Encrypted hydration must query newly tracked member devices before succeeding."""
    owner = _owner()
    room_id = "!hidden:example.org"
    _hide_encrypted_room(owner, room_id)
    owner.client.keys_query = AsyncMock(return_value=nio.KeysQueryResponse(device_keys={}, failures={}))

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        owner.client.should_query_keys = True
        return nio.JoinedMembersResponse(members=[], room_id=room_id)

    owner.client.joined_members.side_effect = joined_members

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is not None
    assert hydrated.encrypted is True
    owner.client.keys_query.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cached_encrypted_room_hydration_retries_pending_member_device_keys() -> None:
    """A cached encrypted room must not bypass pending member device keys."""
    owner = _owner()
    room_id = "!code:example.org"
    remote_member_id = "@human:remote.example.org"
    room = owner.client.rooms[room_id]
    room.encrypted = True
    room.members_synced = True
    room.add_member(remote_member_id, "Human", None)
    owner.client.olm = MagicMock()
    owner.client.should_query_keys = True
    owner.client.users_for_key_query = {remote_member_id}
    owner.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[nio.RoomMember(remote_member_id, "Human", None)],
            room_id=room_id,
        ),
    )
    owner.client.keys_query = AsyncMock(
        return_value=nio.KeysQueryResponse(
            device_keys={},
            failures={"remote.example.org": {"errcode": "M_UNAVAILABLE"}},
        ),
    )

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is None
    owner.client.keys_query.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_encrypted_room_hydration_rejects_concurrent_partial_sync_room() -> None:
    """A room installed during key hydration must be complete before adoption."""
    owner = _owner()
    room_id = "!hidden:example.org"
    remote_member_id = "@human:remote.example.org"
    _hide_encrypted_room(
        owner,
        room_id,
        members=nio.JoinedMembersResponse(
            members=[nio.RoomMember(remote_member_id, "Human", None)],
            room_id=room_id,
        ),
    )
    owner.client.olm = MagicMock()
    owner.client.should_query_keys = True
    owner.client.users_for_key_query = {remote_member_id}
    concurrent_room = nio.MatrixRoom(room_id=room_id, own_user_id=owner.user_id)
    concurrent_room.encrypted = True
    concurrent_room.members_synced = True

    async def keys_query() -> nio.KeysQueryResponse:
        owner.client.rooms[room_id] = concurrent_room
        owner.client.users_for_key_query = set()
        return nio.KeysQueryResponse(device_keys={}, failures={})

    owner.client.keys_query = AsyncMock(side_effect=keys_query)

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is None
    assert owner.client.rooms[room_id] is concurrent_room
    assert concurrent_room.encrypted is True
    assert concurrent_room.members_synced is False
    assert room_id not in owner.client.encrypted_rooms


@pytest.mark.asyncio
async def test_encrypted_room_hydration_publishes_state_with_joined_only_roster() -> None:
    """A hidden encrypted room keeps non-membership state and only joined recipients."""
    owner = _owner()
    room_id = "!hidden:example.org"
    invited_user_id = "@invited:example.org"
    _hide_encrypted_room(owner, room_id)
    owner.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[
                {
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    "event_id": "$encryption",
                    "origin_server_ts": 1,
                    "sender": owner.user_id,
                    "state_key": "",
                    "type": "m.room.encryption",
                },
                {
                    "content": {"name": "Recovery Room"},
                    "event_id": "$name",
                    "origin_server_ts": 2,
                    "sender": owner.user_id,
                    "state_key": "",
                    "type": "m.room.name",
                },
                {
                    "content": {"displayname": "Invited", "membership": "invite"},
                    "event_id": "$invite",
                    "origin_server_ts": 3,
                    "sender": owner.user_id,
                    "state_key": invited_user_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"users": {owner.user_id: 100}},
                    "event_id": "$power-levels",
                    "origin_server_ts": 4,
                    "sender": owner.user_id,
                    "state_key": "",
                    "type": "m.room.power_levels",
                },
            ],
            room_id=room_id,
        ),
    )

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is not None
    assert hydrated.encrypted is True
    room = owner.client.rooms[room_id]
    assert room.encrypted is True
    assert room.name == "Recovery Room"
    assert set(room.users) == set(hydrated.joined_user_ids)
    assert invited_user_id not in room.invited_users
    assert room.power_levels.users[owner.user_id] == 100
    assert room.members_synced is True


@pytest.mark.asyncio
async def test_encrypted_room_hydration_does_not_mutate_unencrypted_sync_room() -> None:
    """A concurrent sync room must become authoritative before encrypted delivery."""
    owner = _owner()
    room_id = "!hidden:example.org"
    _hide_encrypted_room(owner, room_id)
    sync_room = nio.MatrixRoom(room_id=room_id, own_user_id=owner.user_id)

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        owner.client.rooms[room_id] = sync_room
        return nio.JoinedMembersResponse(members=[], room_id=room_id)

    owner.client.joined_members.side_effect = joined_members

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is None
    assert owner.client.rooms[room_id] is sync_room
    assert sync_room.encrypted is False
    assert room_id not in owner.client.encrypted_rooms


@pytest.mark.asyncio
async def test_encrypted_room_hydration_does_not_publish_after_failed_device_key_query() -> None:
    """A failed device-key query must not publish an encrypted room."""
    owner = _owner()
    room_id = "!hidden:example.org"
    _hide_encrypted_room(owner, room_id)
    owner.client.keys_query = AsyncMock(return_value=nio.KeysQueryError("unavailable", "M_UNKNOWN"))

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        owner.client.should_query_keys = True
        return nio.JoinedMembersResponse(members=[], room_id=room_id)

    owner.client.joined_members.side_effect = joined_members

    hydrated = await hydrate_joined_room_for_delivery(owner.client, room_id)

    assert hydrated is None
    assert room_id not in owner.client.rooms
    assert room_id not in owner.client.encrypted_rooms


@pytest.mark.asyncio
async def test_encrypted_delivery_retries_when_member_device_keys_remain_pending(tmp_path: Path) -> None:
    """A partial device-key response must not publish or send through an incomplete room."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    remote_member_id = "@human:remote.example.org"
    _hide_encrypted_room(
        owner,
        target.room_id,
        members=nio.JoinedMembersResponse(
            members=[nio.RoomMember(remote_member_id, "Human", None)],
            room_id=target.room_id,
        ),
    )
    owner.client.olm = MagicMock()
    owner.client.should_query_keys = True
    owner.client.users_for_key_query = {remote_member_id}
    owner.client.keys_query = AsyncMock(
        return_value=nio.KeysQueryResponse(
            device_keys={},
            failures={"remote.example.org": {"errcode": "M_UNAVAILABLE"}},
        ),
    )
    owner.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$resume", room_id=target.room_id),
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is RestartDeliveryOutcome.RETRY
    assert remote_member_id in owner.client.users_for_key_query
    assert target.room_id not in owner.client.rooms
    assert target.room_id not in owner.client.encrypted_rooms
    owner.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_delivery_retries_when_freshness_adds_pending_member_keys(tmp_path: Path) -> None:
    """A member discovered after hydration must block the immediate encrypted send."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    room = owner.client.rooms[target.room_id]
    room.encrypted = True
    room.members_synced = True
    owner.client.olm = MagicMock()
    owner.client.users_for_key_query = set()
    remote_member_id = "@human:remote.example.org"
    send_message = AsyncMock(side_effect=delivered_matrix_side_effect("$resume"))
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def freshness_after_hydration(
        *_args: object,
        **_kwargs: object,
    ) -> InterruptedTargetFreshness:
        room.add_member(remote_member_id, "Human", None)
        owner.client.users_for_key_query = {remote_member_id}
        return InterruptedTargetFreshness.CURRENT

    with (
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(side_effect=freshness_after_hydration),
        ),
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=send_message,
        ),
    ):
        delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is RestartDeliveryOutcome.RETRY
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_delivery_retries_when_freshness_replaces_authoritative_members(tmp_path: Path) -> None:
    """A replacement room must retain every member confirmed during hydration."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    remote_member_id = "@human:remote.example.org"
    room = owner.client.rooms[target.room_id]
    room.encrypted = True
    room.members_synced = True
    room.add_member(remote_member_id, "Human", None)
    owner.client.olm = MagicMock()
    owner.client.users_for_key_query = set()
    owner.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[nio.RoomMember(remote_member_id, "Human", None)],
            room_id=target.room_id,
        ),
    )
    replacement_room = nio.MatrixRoom(room_id=target.room_id, own_user_id=owner.user_id)
    replacement_room.encrypted = True
    replacement_room.members_synced = True
    send_message = AsyncMock(side_effect=delivered_matrix_side_effect("$resume"))
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def freshness_after_hydration(
        *_args: object,
        **_kwargs: object,
    ) -> InterruptedTargetFreshness:
        owner.client.rooms[target.room_id] = replacement_room
        return InterruptedTargetFreshness.CURRENT

    with (
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(side_effect=freshness_after_hydration),
        ),
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=send_message,
        ),
    ):
        delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is RestartDeliveryOutcome.RETRY
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_plaintext_delivery_retries_when_freshness_observes_encryption(tmp_path: Path) -> None:
    """A room becoming encrypted after hydration must restart encrypted hydration."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    room = owner.client.rooms[target.room_id]
    room.members_synced = True
    owner.client.olm = MagicMock()
    owner.client.users_for_key_query = set()
    remote_member_id = "@human:remote.example.org"
    send_message = AsyncMock(side_effect=delivered_matrix_side_effect("$resume"))
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def freshness_after_hydration(
        *_args: object,
        **_kwargs: object,
    ) -> InterruptedTargetFreshness:
        room.encrypted = True
        room.add_member(remote_member_id, "Human", None)
        owner.client.users_for_key_query = {remote_member_id}
        return InterruptedTargetFreshness.CURRENT

    with (
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(side_effect=freshness_after_hydration),
        ),
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=send_message,
        ),
    ):
        delivered = await operations.deliver_target(owner, target, _config(tmp_path))

    assert delivered is RestartDeliveryOutcome.RETRY
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_target_delivery_rechecks_freshness_after_hidden_room_hydration(
    tmp_path: Path,
) -> None:
    """Human activity during hidden-room hydration must prevent a stale resume relay."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    _hide_encrypted_room(owner, target.room_id)
    owner.client.should_query_keys = False
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    async def freshness_after_hydration(*_args: object, **_kwargs: object) -> InterruptedTargetFreshness:
        owner.client.joined_members.assert_awaited_once_with(target.room_id)
        assert owner.client.rooms[target.room_id].encrypted is True
        return InterruptedTargetFreshness.NEWER_HUMAN

    with (
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(side_effect=freshness_after_hydration),
        ) as freshness,
        patch("mindroom.restart_recovery_operations.build_auto_resume_content") as build_content,
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ) as send_message,
    ):
        outcome = await operations.deliver_target(owner, target, _config(tmp_path))

    assert outcome is RestartDeliveryOutcome.TERMINAL
    freshness.assert_awaited_once()
    build_content.assert_not_called()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_target_delivery_paces_owner_sends(tmp_path: Path) -> None:
    """Concurrent-room recovery must not burst visible relays through exact owners."""
    owner = _owner()
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ) as send_message,
        patch(
            "mindroom.restart_recovery_operations.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep,
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(
                side_effect=(
                    stale_stream_cleanup_module.InterruptedTargetFreshness.CURRENT,
                    stale_stream_cleanup_module.InterruptedTargetFreshness.CURRENT,
                    stale_stream_cleanup_module.InterruptedTargetFreshness.NEWER_HUMAN,
                ),
            ),
        ),
    ):
        assert (
            await operations.deliver_target(
                owner,
                _target("$first", timestamp_ms=10, thread_id="$thread-a"),
                _config(tmp_path),
            )
            is restart_recovery_operations_module.RestartDeliveryOutcome.DELIVERED
        )
        assert (
            await operations.deliver_target(
                owner,
                _target("$second", timestamp_ms=20, thread_id="$thread-b"),
                _config(tmp_path),
            )
            is restart_recovery_operations_module.RestartDeliveryOutcome.DELIVERED
        )
        assert (
            await operations.deliver_target(
                owner,
                _target("$third", timestamp_ms=30, thread_id="$thread-c"),
                _config(tmp_path),
            )
            is restart_recovery_operations_module.RestartDeliveryOutcome.TERMINAL
        )

    assert sleep.await_count == 2
    assert all(0 < call.args[0] <= 2.0 for call in sleep.await_args_list)
    assert send_message.await_count == 2


@pytest.mark.asyncio
async def test_matrix_target_delivery_propagates_cache_notification_failure(
    tmp_path: Path,
) -> None:
    """A local cache bug after an idempotent send must remain visible."""
    owner = _owner()
    owner.conversation_cache.notify_outbound_message.side_effect = RuntimeError("cache bug")
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ),
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
        ),
        pytest.raises(RuntimeError, match="cache bug"),
    ):
        await operations.deliver_target(
            owner,
            _target("$target", timestamp_ms=10),
            _config(tmp_path),
        )


@pytest.mark.asyncio
async def test_matrix_target_delivery_records_successful_outbound_message(
    tmp_path: Path,
) -> None:
    """A delivered relay must immediately enter the owner's conversation cache."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ),
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
        ),
    ):
        outcome = await operations.deliver_target(owner, target, _config(tmp_path))

    assert outcome is RestartDeliveryOutcome.DELIVERED
    owner.conversation_cache.notify_outbound_message.assert_called_once()
    room_id, event_id, content = owner.conversation_cache.notify_outbound_message.call_args.args
    assert room_id == target.room_id
    assert event_id == "$resume"
    assert content["body"]


@pytest.mark.asyncio
async def test_matrix_target_delivery_reuses_transaction_id_after_timestamp_advances(
    tmp_path: Path,
) -> None:
    """Rediscovery after a lost response must reuse the same Matrix transaction ID."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    successful_delivery = delivered_matrix_side_effect("$same-event")
    attempt = 0

    async def deliver_once_lost(*args: object, **kwargs: object) -> object | None:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return None
        return await successful_delivery(*args, **kwargs)

    with (
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=deliver_once_lost),
        ) as send_message,
        patch(
            "mindroom.restart_recovery_operations.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch(
            "mindroom.restart_recovery_operations.interrupted_target_freshness",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.InterruptedTargetFreshness.CURRENT,
            ),
        ),
    ):
        first = await operations.deliver_target(owner, target, _config(tmp_path))
        second = await operations.deliver_target(
            owner,
            dataclasses.replace(target, timestamp_ms=target.timestamp_ms + 1),
            _config(tmp_path),
        )

    assert first is RestartDeliveryOutcome.RETRY
    assert second is RestartDeliveryOutcome.DELIVERED
    transaction_ids = [call.kwargs["transaction_id"] for call in send_message.await_args_list]
    assert transaction_ids[0] == transaction_ids[1]


@pytest.mark.asyncio
async def test_matrix_room_recovery_shares_membership_discovery_for_owner_generation(
    tmp_path: Path,
) -> None:
    """Concurrent rooms from one owner generation must share one membership read."""
    room_ids = frozenset({"!first:example.org", "!second:example.org"})
    owner = _owner(rooms=room_ids)
    config = _config(tmp_path)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    requests = [
        RoomRecoveryRequest(
            room_id=room_id,
            startup_cutoff_ms=123,
            terminal_interrupted_only=False,
        )
        for room_id in room_ids
    ]
    membership_started = asyncio.Event()
    release_membership = asyncio.Event()

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        membership_started.set()
        await release_membership.wait()
        return list(room_ids)

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(side_effect=joined_rooms),
        ) as get_rooms,
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=0,
                    interrupted_threads=(),
                ),
            ),
        ),
    ):
        attempts = [
            asyncio.create_task(
                operations.recover_room(
                    (owner,),
                    request,
                    frozenset({owner.user_id}),
                    config,
                ),
            )
            for request in requests
        ]
        await asyncio.wait_for(membership_started.wait(), timeout=1.0)
        await asyncio.sleep(0)
        release_membership.set()
        results = await asyncio.gather(*attempts)
        cached_result = await operations.recover_room(
            (owner,),
            requests[0],
            frozenset({owner.user_id}),
            config,
        )
        same_generation_calls = get_rooms.await_count
        replacement_owner = _owner(
            generation=object(),
            rooms=room_ids,
        )
        replacement_result = await operations.recover_room(
            (replacement_owner,),
            requests[0],
            frozenset({replacement_owner.user_id}),
            config,
        )

    assert results == [RoomRecoveryResult(), RoomRecoveryResult()]
    assert cached_result == RoomRecoveryResult()
    assert replacement_result == RoomRecoveryResult()
    assert same_generation_calls == 1
    assert get_rooms.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_membership_waiter_preserves_shared_lookup(tmp_path: Path) -> None:
    """Cancelling one room lease must not cancel another lease's shared membership read."""
    room_ids = frozenset({"!first:example.org", "!second:example.org"})
    owner = _owner(rooms=room_ids)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        lookup_started.set()
        await release_lookup.wait()
        return list(room_ids)

    cleanup_room = AsyncMock(
        return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        ),
    )
    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(side_effect=joined_rooms),
        ) as get_rooms,
        patch("mindroom.restart_recovery_operations.cleanup_stale_streaming_room", new=cleanup_room),
    ):
        attempts = [
            asyncio.create_task(
                operations.recover_room(
                    (owner,),
                    RoomRecoveryRequest(room_id, 123, False),
                    frozenset({owner.user_id}),
                    _config(tmp_path),
                ),
            )
            for room_id in sorted(room_ids)
        ]
        await asyncio.wait_for(lookup_started.wait(), timeout=1.0)
        await asyncio.sleep(0)
        attempts[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempts[0]
        try:
            assert attempts[1].done() is False
            release_lookup.set()
            assert await attempts[1] == RoomRecoveryResult()
        finally:
            release_lookup.set()
            await operations.drain_membership_snapshots()
            await asyncio.gather(*attempts, return_exceptions=True)

    assert get_rooms.await_count == 1


@pytest.mark.asyncio
async def test_replacement_generation_drains_inflight_membership_lookup(tmp_path: Path) -> None:
    """Replacing an owner must cancel and drain its superseded membership task."""
    room_id = "!code:example.org"
    old_owner = _owner(generation=object(), rooms=frozenset({room_id}))
    new_owner = _owner(generation=object(), rooms=frozenset({room_id}))
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    old_lookup_started = asyncio.Event()
    old_lookup_cancelled = asyncio.Event()
    release_old_cleanup = asyncio.Event()

    async def joined_rooms(client: nio.AsyncClient) -> list[str]:
        if client is not old_owner.client:
            return [room_id]
        old_lookup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_lookup_cancelled.set()
            await release_old_cleanup.wait()
            raise

    cleanup_room = AsyncMock(
        return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        ),
    )
    with (
        patch("mindroom.restart_recovery_operations.get_joined_rooms", new=AsyncMock(side_effect=joined_rooms)),
        patch("mindroom.restart_recovery_operations.cleanup_stale_streaming_room", new=cleanup_room),
    ):
        old_attempt = asyncio.create_task(
            operations.recover_room(
                (old_owner,),
                RoomRecoveryRequest(room_id, 123, False),
                frozenset({old_owner.user_id}),
                _config(tmp_path),
            ),
        )
        await asyncio.wait_for(old_lookup_started.wait(), timeout=1.0)
        replacement_attempt = asyncio.create_task(
            operations.recover_room(
                (new_owner,),
                RoomRecoveryRequest(room_id, 123, False),
                frozenset({new_owner.user_id}),
                _config(tmp_path),
            ),
        )
        try:
            await asyncio.wait_for(old_lookup_cancelled.wait(), timeout=1.0)
            assert replacement_attempt.done() is False
            release_old_cleanup.set()
            assert await replacement_attempt == RoomRecoveryResult()
            with pytest.raises(asyncio.CancelledError):
                await old_attempt
        finally:
            release_old_cleanup.set()
            old_attempt.cancel()
            replacement_attempt.cancel()
            await operations.drain_membership_snapshots()
            await asyncio.gather(old_attempt, replacement_attempt, return_exceptions=True)


@pytest.mark.asyncio
async def test_discarded_owner_lookup_drains_before_replacement_lookup(tmp_path: Path) -> None:
    """Discarding an owner must not overlap its lookup with a replacement generation."""
    room_id = "!code:example.org"
    old_owner = _owner(generation=object(), rooms=frozenset({room_id}))
    new_owner = _owner(generation=object(), rooms=frozenset({room_id}))
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    old_lookup_started = asyncio.Event()
    old_lookup_cancelled = asyncio.Event()
    release_old_cleanup = asyncio.Event()
    new_lookup_started = asyncio.Event()

    async def joined_rooms(client: nio.AsyncClient) -> list[str]:
        if client is not old_owner.client:
            new_lookup_started.set()
            return [room_id]
        old_lookup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_lookup_cancelled.set()
            await release_old_cleanup.wait()
            raise

    cleanup_room = AsyncMock(
        return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        ),
    )
    with (
        patch("mindroom.restart_recovery_operations.get_joined_rooms", new=AsyncMock(side_effect=joined_rooms)),
        patch("mindroom.restart_recovery_operations.cleanup_stale_streaming_room", new=cleanup_room),
    ):
        old_attempt = asyncio.create_task(
            operations.recover_room(
                (old_owner,),
                RoomRecoveryRequest(room_id, 123, False),
                frozenset({old_owner.user_id}),
                _config(tmp_path),
            ),
        )
        await asyncio.wait_for(old_lookup_started.wait(), timeout=1.0)
        operations.discard_owner(old_owner.user_id)
        replacement_attempt = asyncio.create_task(
            operations.recover_room(
                (new_owner,),
                RoomRecoveryRequest(room_id, 123, False),
                frozenset({new_owner.user_id}),
                _config(tmp_path),
            ),
        )
        try:
            await asyncio.wait_for(old_lookup_cancelled.wait(), timeout=1.0)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(new_lookup_started.wait(), timeout=0.05)
            release_old_cleanup.set()
            assert await replacement_attempt == RoomRecoveryResult()
            with pytest.raises(asyncio.CancelledError):
                await old_attempt
        finally:
            release_old_cleanup.set()
            old_attempt.cancel()
            replacement_attempt.cancel()
            await operations.drain_membership_snapshots()
            await asyncio.gather(old_attempt, replacement_attempt, return_exceptions=True)


@pytest.mark.asyncio
async def test_snapshot_drain_waits_for_discarded_owner_membership_lookup(tmp_path: Path) -> None:
    """Snapshot drain must retain and wait for a lookup removed by owner discard."""
    owner = _owner()
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    lookup_started = asyncio.Event()
    lookup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        lookup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            lookup_cancelled.set()
            await release_cleanup.wait()
            raise

    with patch("mindroom.restart_recovery_operations.get_joined_rooms", new=joined_rooms):
        recovery_task = asyncio.create_task(operations.joined_rooms(owner))
        await asyncio.wait_for(lookup_started.wait(), timeout=1.0)
        operations.discard_owner(owner.user_id)
        await asyncio.wait_for(lookup_cancelled.wait(), timeout=1.0)
        drain_task = asyncio.create_task(operations.drain_membership_snapshots())
        try:
            await asyncio.sleep(0)
            assert drain_task.done() is False
            release_cleanup.set()
            await asyncio.wait_for(drain_task, timeout=1.0)
        finally:
            release_cleanup.set()
            recovery_task.cancel()
            drain_task.cancel()
            await asyncio.gather(recovery_task, drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_drains_shared_membership_lookup_before_return(tmp_path: Path) -> None:
    """Config mutation must wait for old-client membership cleanup to finish."""
    owner = _owner(rooms=frozenset())
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: {owner.user_id: owner},
        operations=operations,
    )
    lookup_started = asyncio.Event()
    lookup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        lookup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            lookup_cancelled.set()
            await release_cleanup.wait()
            raise

    with patch("mindroom.restart_recovery_operations.get_joined_rooms", new=joined_rooms):
        coordinator.start(startup_cutoff_ms=123)
        await asyncio.wait_for(lookup_started.wait(), timeout=1.0)
        pause_task = asyncio.create_task(coordinator.pause())
        try:
            await asyncio.wait_for(lookup_cancelled.wait(), timeout=1.0)
            await asyncio.sleep(0)
            assert pause_task.done() is False
            release_cleanup.set()
            await asyncio.wait_for(pause_task, timeout=1.0)
        finally:
            release_cleanup.set()
            pause_task.cancel()
            await asyncio.gather(pause_task, return_exceptions=True)
            await coordinator.stop()


@pytest.mark.asyncio
async def test_missing_rooms_back_off_one_owner_membership_snapshot_refresh(
    tmp_path: Path,
) -> None:
    """Absent desired rooms must share one read until the owner refresh delay expires."""
    room_ids = frozenset(f"!missing-{index}:example.org" for index in range(5))
    owner = _owner(rooms=room_ids)
    config = _config(tmp_path)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    requests = [
        RoomRecoveryRequest(
            room_id=room_id,
            startup_cutoff_ms=123,
            terminal_interrupted_only=False,
        )
        for room_id in sorted(room_ids)
    ]
    membership_visible = False

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        return list(room_ids) if membership_visible else []

    cleanup_room = AsyncMock(
        return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        ),
    )
    loop = asyncio.get_running_loop()
    try:
        with (
            patch(
                "mindroom.restart_recovery_operations.get_joined_rooms",
                new=AsyncMock(side_effect=joined_rooms),
            ) as get_rooms,
            patch(
                "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
                new=cleanup_room,
            ),
            patch.object(loop, "time", return_value=100.0) as monotonic_time,
        ):
            missing_results = [
                await operations.recover_room(
                    (owner,),
                    request,
                    frozenset({owner.user_id}),
                    config,
                )
                for request in requests
            ]
            calls_before_refresh = get_rooms.await_count

            membership_visible = True
            monotonic_time.return_value = 102.0
            refreshed_result = await operations.recover_room(
                (owner,),
                requests[0],
                frozenset({owner.user_id}),
                config,
            )
            calls_after_refresh = get_rooms.await_count
    finally:
        await operations.drain_membership_snapshots()

    assert missing_results == [
        RoomRecoveryResult(unjoined_owner_user_ids=frozenset({owner.user_id})) for _request in requests
    ]
    assert calls_before_refresh == 1
    assert refreshed_result == RoomRecoveryResult()
    assert calls_after_refresh == 2
    cleanup_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_room_recovery_does_not_reuse_snapshot_after_generation_id_collision(
    tmp_path: Path,
) -> None:
    """A replacement generation must never inherit a stale snapshot when object IDs collide."""
    old_room = "!old:example.org"
    new_room = "!new:example.org"
    old_owner = _owner(
        generation=object(),
        rooms=frozenset({old_room}),
    )
    new_owner = _owner(
        generation=object(),
        rooms=frozenset({new_room}),
    )
    config = _config(tmp_path)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(side_effect=[[old_room], [new_room]]),
        ) as get_rooms,
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=0,
                    interrupted_threads=(),
                ),
            ),
        ),
    ):
        old_result = await operations.recover_room(
            (old_owner,),
            RoomRecoveryRequest(
                room_id=old_room,
                startup_cutoff_ms=123,
                terminal_interrupted_only=False,
            ),
            frozenset({old_owner.user_id}),
            config,
        )
        new_result = await operations.recover_room(
            (new_owner,),
            RoomRecoveryRequest(
                room_id=new_room,
                startup_cutoff_ms=123,
                terminal_interrupted_only=False,
            ),
            frozenset({new_owner.user_id}),
            config,
        )

    assert old_result == RoomRecoveryResult()
    assert new_result == RoomRecoveryResult()
    assert get_rooms.await_count == 2


@pytest.mark.asyncio
async def test_discard_owner_releases_membership_snapshot_generation(tmp_path: Path) -> None:
    """Owner discard must release the generation retained by membership discovery."""

    class Generation:
        pass

    generation = Generation()
    generation_ref = weakref.ref(generation)
    owner = _owner(generation=generation)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=dict,
        operations=operations,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=0,
                    interrupted_threads=(),
                ),
            ),
        ),
    ):
        await operations.recover_room(
            (owner,),
            RoomRecoveryRequest(
                room_id="!code:example.org",
                startup_cutoff_ms=123,
                terminal_interrupted_only=False,
            ),
            frozenset({owner.user_id}),
            _config(tmp_path),
        )

    coordinator.discard_owner(owner.user_id)
    del owner
    del generation
    gc.collect()

    assert generation_ref() is None


@pytest.mark.asyncio
async def test_owner_ready_releases_completed_generation_membership_snapshot(
    tmp_path: Path,
) -> None:
    """A replacement owner must not retain the completed generation's Matrix client."""

    class Generation:
        pass

    room_id = "!code:example.org"
    generation = Generation()
    generation_ref = weakref.ref(generation)
    owner = _owner(generation=generation, rooms=frozenset({room_id}))
    owners = {owner.user_id: owner}
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=operations,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=[room_id]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
                    cleaned_count=0,
                    interrupted_threads=(),
                ),
            ),
        ),
    ):
        coordinator.start(startup_cutoff_ms=123)
        await _wait_until(
            lambda: not coordinator._room_jobs and not coordinator._active_attempts,
        )

        replacement = _owner(generation=Generation(), rooms=frozenset({room_id}))
        owners[owner.user_id] = replacement
        del owner
        del generation
        gc.collect()
        assert generation_ref() is not None

        coordinator.owner_ready(replacement.user_id)
        await asyncio.sleep(0)
        gc.collect()
        try:
            assert generation_ref() is None
        finally:
            await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_drains_historical_membership_snapshots(tmp_path: Path) -> None:
    """Stop must cancel and release snapshots absent from current owners."""

    class Generation:
        pass

    generation = Generation()
    generation_ref = weakref.ref(generation)
    owner = _owner(generation=generation)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=dict,
        operations=operations,
    )
    lookup_started = asyncio.Event()

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        lookup_started.set()
        await asyncio.Event().wait()
        return []

    with patch(
        "mindroom.restart_recovery_operations.get_joined_rooms",
        new=joined_rooms,
    ):
        recovery_task = asyncio.create_task(
            operations.recover_room(
                (owner,),
                RoomRecoveryRequest(
                    room_id="!code:example.org",
                    startup_cutoff_ms=123,
                    terminal_interrupted_only=False,
                ),
                frozenset({owner.user_id}),
                _config(tmp_path),
            ),
        )
        await asyncio.wait_for(lookup_started.wait(), timeout=1.0)
        try:
            await coordinator.stop()
            await asyncio.wait_for(
                asyncio.gather(recovery_task, return_exceptions=True),
                timeout=1.0,
            )
            assert recovery_task.cancelled()
        finally:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)

    del recovery_task
    del owner
    del generation
    await asyncio.sleep(0)
    gc.collect()

    assert generation_ref() is None


@pytest.mark.asyncio
async def test_owner_ready_during_stop_does_not_restore_recovery_work(
    tmp_path: Path,
) -> None:
    """A late sync-readiness callback must be inert after shutdown begins."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}
    current_owners = MagicMock(side_effect=lambda: owners)
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def drain_membership_snapshots() -> None:
        close_started.set()
        await release_close.wait()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=current_owners,
        operations=RestartRecoveryOperations(
            joined_rooms=AsyncMock(side_effect=lambda owner: list(owner.desired_room_ids)),
            membership_refresh_delay_seconds=0.0,
            recover_room=AsyncMock(return_value=RoomRecoveryResult()),
            target_freshness=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
            deliver_target=AsyncMock(return_value=RestartDeliveryOutcome.DELIVERED),
            discard_owner=lambda _owner_user_id: None,
            drain_membership_snapshots=drain_membership_snapshots,
        ),
    )
    coordinator.start(startup_cutoff_ms=123)
    owners[owner.user_id] = _owner(
        generation=owner.generation,
        rooms=frozenset({"!code:example.org"}),
    )

    stop_task = asyncio.create_task(coordinator.stop())
    await asyncio.wait_for(close_started.wait(), timeout=1.0)
    owner_snapshots_before_callback = current_owners.call_count
    try:
        coordinator.owner_ready(owner.user_id)
        restored_work = bool(coordinator._room_jobs)
    finally:
        release_close.set()
        await asyncio.wait_for(stop_task, timeout=1.0)

    assert current_owners.call_count == owner_snapshots_before_callback
    assert not restored_work


@pytest.mark.asyncio
async def test_enqueue_replacement_rooms_is_inert_after_stop(tmp_path: Path) -> None:
    """A late replacement handoff must not retain a stopped owner generation."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )

    await coordinator.stop()
    coordinator.enqueue_replacement_rooms(
        owner.user_id,
        {"!replacement:example.org"},
    )

    assert not coordinator._room_jobs


@pytest.mark.asyncio
async def test_enqueue_replacement_rooms_snapshots_owner_generation_once(tmp_path: Path) -> None:
    """One replacement batch must not rebuild every owner for each room."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}
    current_owners = MagicMock(return_value=owners)
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=current_owners,
        operations=_operations(recover_room=AsyncMock(return_value=RoomRecoveryResult())),
    )
    room_ids = {f"!replacement-{index}:example.org" for index in range(20)}

    coordinator.enqueue_replacement_rooms(owner.user_id, room_ids)

    assert current_owners.call_count == 1
    assert {work.room_id for work in coordinator._room_jobs.values()} == room_ids


@pytest.mark.asyncio
async def test_scan_and_freshness_share_eight_matrix_read_phase_slots(
    tmp_path: Path,
) -> None:
    """Room scans and freshness reads must share one eight-slot phase budget."""
    room_ids = frozenset(f"!room-{index}:example.org" for index in range(10))
    owner = _owner(rooms=room_ids)
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    phase_started: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    release_reads: asyncio.Queue[None] = asyncio.Queue()
    all_delivered = asyncio.Event()
    active_reads = 0
    max_active_reads = 0
    delivered = 0

    async def matrix_read_phase(kind: str, room_id: str) -> None:
        nonlocal active_reads, max_active_reads
        active_reads += 1
        max_active_reads = max(max_active_reads, active_reads)
        phase_started.put_nowait((kind, room_id))
        try:
            await release_reads.get()
        finally:
            active_reads -= 1

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        await matrix_read_phase("scan", request.room_id)
        suffix = request.room_id.removeprefix("!").removesuffix(":example.org")
        return RoomRecoveryResult(
            interrupted_threads=(
                _target(
                    f"$target-{suffix}",
                    timestamp_ms=10,
                    room_id=request.room_id,
                    thread_id=f"$thread-{suffix}",
                ),
            ),
        )

    async def freshness(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        await matrix_read_phase("freshness", target.room_id)
        return InterruptedTargetFreshness.CURRENT

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivered
        delivered += 1
        if delivered == len(room_ids):
            all_delivered.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(
            recover_room=recover_room,
            freshness=freshness,
            deliver=deliver,
        ),
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        first_phases = [await asyncio.wait_for(phase_started.get(), timeout=1.0) for _ in range(8)]
        assert {kind for kind, _room_id in first_phases} == {"scan"}
        assert active_reads == 8

        release_reads.put_nowait(None)
        await asyncio.wait_for(phase_started.get(), timeout=1.0)
        await asyncio.sleep(0)

        assert max_active_reads == 8

        for _ in range(len(room_ids) * 2):
            release_reads.put_nowait(None)
        await asyncio.wait_for(all_delivered.wait(), timeout=1.0)
    finally:
        for _ in range(len(room_ids) * 2):
            release_reads.put_nowait(None)
        await coordinator.stop()

    assert max_active_reads == 8


@pytest.mark.asyncio
async def test_blocked_deliveries_do_not_consume_room_scan_capacity(tmp_path: Path) -> None:
    """All room scans must finish while the one admitted delivery remains blocked."""
    room_ids = frozenset(f"!room-{index}:example.org" for index in range(9))
    owner = _owner(rooms=room_ids)
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    scanned_room_ids: set[str] = set()
    delivery_started = asyncio.Event()
    release_deliveries = asyncio.Event()
    delivery_count = 0

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        scanned_room_ids.add(request.room_id)
        suffix = request.room_id.removeprefix("!").removesuffix(":example.org")
        return RoomRecoveryResult(
            interrupted_threads=(
                _target(
                    f"$target-{suffix}",
                    timestamp_ms=10,
                    room_id=request.room_id,
                    thread_id=f"$thread-{suffix}",
                ),
            ),
        )

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_count
        delivery_count += 1
        delivery_started.set()
        await release_deliveries.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(delivery_started.wait(), timeout=1.0)
        await _wait_until(lambda: scanned_room_ids == set(room_ids))
        assert scanned_room_ids == set(room_ids)
        assert delivery_count == 1
    finally:
        release_deliveries.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_pause_drains_only_one_admitted_delivery_and_restores_waiters(
    tmp_path: Path,
) -> None:
    """Pause must cancel delivery waiters while draining one admitted send."""
    room_ids = frozenset(f"!room-{index}:example.org" for index in range(3))
    owner = _owner(rooms=room_ids)
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivery_calls = 0

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        suffix = request.room_id.removeprefix("!").removesuffix(":example.org")
        return RoomRecoveryResult(
            interrupted_threads=(
                _target(
                    f"$target-{suffix}",
                    timestamp_ms=10,
                    room_id=request.room_id,
                    thread_id=f"$thread-{suffix}",
                ),
            ),
        )

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_calls
        delivery_calls += 1
        delivery_started.set()
        await release_delivery.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)
    await asyncio.sleep(0)
    pause_task = asyncio.create_task(coordinator.pause())
    try:
        await asyncio.sleep(0)
        assert delivery_calls == 1

        release_delivery.set()
        await asyncio.wait_for(pause_task, timeout=1.0)

        retained_target_count = sum(len(work.targets) for work in coordinator._room_jobs.values())
        assert retained_target_count == len(room_ids) - 1
    finally:
        release_delivery.set()
        await asyncio.gather(pause_task, return_exceptions=True)
        await coordinator.stop()


@pytest.mark.asyncio
async def test_due_owner_cohort_gets_read_slot_before_repeat_owner_saturation(
    tmp_path: Path,
) -> None:
    """Eight earlier rooms from one owner must not fill every read slot."""
    blocked_room_ids = frozenset(f"!blocked-{index}:example.org" for index in range(8))
    blocked_owner = _owner(
        user_id="@blocked:example.org",
        rooms=blocked_room_ids,
    )
    healthy_owner = _owner(
        user_id="@healthy:example.org",
        rooms=frozenset({"!healthy:example.org"}),
    )
    owners = {
        blocked_owner.user_id: blocked_owner,
        healthy_owner.user_id: healthy_owner,
    }
    release_blocked = asyncio.Event()
    healthy_finished = asyncio.Event()

    async def recover_room(
        owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        if owner.user_id == blocked_owner.user_id:
            await release_blocked.wait()
        else:
            healthy_finished.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(healthy_finished.wait(), timeout=1.0)
    finally:
        release_blocked.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_future_fair_owner_retry_does_not_hide_due_active_owner_work(
    tmp_path: Path,
) -> None:
    """Fairness must only rank due work, never a future backoff."""
    blocked_room = "!blocked:example.org"
    retry_room = "!retry:example.org"
    replacement_room = "!replacement:example.org"
    blocked_owner = _owner(
        user_id="@blocked:example.org",
        rooms=frozenset({blocked_room}),
    )
    retry_owner = _owner(
        user_id="@retry:example.org",
        rooms=frozenset({retry_room}),
    )
    owners = {
        blocked_owner.user_id: blocked_owner,
        retry_owner.user_id: retry_owner,
    }
    blocked_started = asyncio.Event()
    release_blocked = asyncio.Event()
    retry_backoff_set = asyncio.Event()
    replacement_started = asyncio.Event()

    async def recover_room(
        owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        if request.room_id == blocked_room:
            blocked_started.set()
            await release_blocked.wait()
        elif request.room_id == retry_room:
            return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))
        else:
            replacement_started.set()
        return RoomRecoveryResult()

    def retry_delay(_attempt: int) -> float:
        retry_backoff_set.set()
        return 60.0

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=retry_delay,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(blocked_started.wait(), timeout=1.0)
    await asyncio.wait_for(retry_backoff_set.wait(), timeout=1.0)

    coordinator.enqueue_replacement_rooms(blocked_owner.user_id, {replacement_room})
    try:
        coordinator._start_due_attempts()
        await asyncio.wait_for(replacement_started.wait(), timeout=0.2)
    finally:
        release_blocked.set()
        await coordinator.stop()


def test_due_owner_cohort_precedes_oldest_repeat_owner(tmp_path: Path) -> None:
    """Owner diversity gets one slot before age orders repeated-owner work."""

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=dict,
        operations=_operations(recover_room=recover_room),
    )
    busy_owner = "@busy:example.org"
    active_request = RoomRecoveryRequest("!active:example.org", 123, False)
    oldest_request = RoomRecoveryRequest("!oldest:example.org", 123, False)
    newer_request = RoomRecoveryRequest("!newer:example.org", 123, False)
    active = RoomWork(active_request, busy_owner, None, due_at=None)
    oldest = RoomWork(oldest_request, busy_owner, None, due_at=1.0)
    newer = RoomWork(newer_request, "@idle:example.org", None, due_at=2.0)
    coordinator._active_attempts[cast("asyncio.Task[object]", MagicMock())] = RoomLease(
        active_request,
        (active,),
    )
    coordinator._room_jobs = {oldest.key: oldest, newer.key: newer}

    assert coordinator._due_room_leases(3.0)[0].request is newer_request

    coordinator._active_attempts[cast("asyncio.Task[object]", MagicMock())] = RoomLease(
        newer_request,
        (newer,),
    )

    assert coordinator._due_room_leases(3.0)[0].request is oldest_request


@pytest.mark.asyncio
async def test_startup_scheduling_groups_due_jobs_in_one_values_pass(tmp_path: Path) -> None:
    """Large startup scope must not rescan every retained job per room lease."""

    class CountingRoomJobs(dict[tuple[tuple[str, int | None, bool], str], RoomWork]):
        values_calls = 0

        def values(self) -> ValuesView[RoomWork]:  # ty: ignore[invalid-method-override] - test counter wraps dict
            self.values_calls += 1
            return super().values()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=dict,
        operations=_operations(recover_room=AsyncMock(return_value=RoomRecoveryResult())),
    )
    jobs = CountingRoomJobs()
    for room_index in range(50):
        request = RoomRecoveryRequest(f"!room-{room_index}:example.org", 123, False)
        for owner_index in range(4):
            work = RoomWork(
                request,
                f"@owner-{owner_index}:example.org",
                None,
                due_at=0.0,
            )
            jobs[work.key] = work
    coordinator._room_jobs = jobs

    with patch.object(coordinator, "_process_room", new=AsyncMock(return_value=())):
        coordinator._start_due_attempts()
        tasks = tuple(coordinator._active_attempts)
        await asyncio.gather(*tasks)

    assert len(tasks) == 50
    assert jobs.values_calls <= 2


@pytest.mark.asyncio
async def test_room_failure_retries_without_external_notification(tmp_path: Path) -> None:
    """A transient room failure must retry even when no later ready event fires."""
    owner = _owner()
    owners = {owner.user_id: owner}
    attempts = 0
    recovered = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))
        recovered.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert attempts == 2


@pytest.mark.asyncio
async def test_permanent_room_retry_stops_with_terminal_warning(tmp_path: Path) -> None:
    """A permanently unavailable room must not poll for the process lifetime."""
    owner = _owner()
    owners = {owner.user_id: owner}
    attempts = 0
    sixth_attempt = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal attempts
        attempts += 1
        if attempts == 6:
            sixth_attempt.set()
        return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 0.0,
    )
    with patch("mindroom.restart_recovery.logger.warning") as warning:
        coordinator.start(startup_cutoff_ms=123)
        try:
            await asyncio.wait_for(sixth_attempt.wait(), timeout=1.0)
            await _wait_until(
                lambda: not coordinator._active_attempts and next(iter(coordinator._room_jobs.values())).due_at is None,
            )
        finally:
            await coordinator.stop()

    assert attempts == 6
    warning.assert_called_once_with(
        "Restart recovery exhausted retries; parking retained owner work",
        attempt=6,
        owner_user_id=owner.user_id,
        room_id="!code:example.org",
        target_event_ids=[],
    )


@pytest.mark.asyncio
async def test_unjoined_desired_room_refreshes_once_then_parks_without_warning(
    tmp_path: Path,
) -> None:
    """A missing membership must not spend the Matrix retry budget."""
    owner = _owner()
    owners = {owner.user_id: owner}
    joined = False

    async def joined_rooms(_client: nio.AsyncClient) -> list[str]:
        return ["!code:example.org"] if joined else []

    cleanup_room = AsyncMock(
        return_value=stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        ),
    )
    operations = build_matrix_restart_recovery_operations(
        test_runtime_paths(tmp_path),
        membership_refresh_delay_seconds=0.0,
    )
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=operations,
        retry_delay=lambda _attempt: 0.0,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(side_effect=joined_rooms),
        ) as get_rooms,
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=cleanup_room,
        ),
        patch("mindroom.restart_recovery.logger.warning") as warning,
    ):
        coordinator.start(startup_cutoff_ms=123)
        await _wait_until(
            lambda: (
                bool(coordinator._room_jobs)
                and not coordinator._active_attempts
                and next(iter(coordinator._room_jobs.values())).due_at is None
            ),
        )
        missing_membership_scans = get_rooms.await_count
        parked = next(iter(coordinator._room_jobs.values()))

        joined = True
        coordinator.owner_ready(owner.user_id)
        try:
            await _wait_until(
                lambda: not coordinator._room_jobs and not coordinator._active_attempts,
            )
        finally:
            await coordinator.stop()

    assert missing_membership_scans == 2
    assert parked.matrix_attempt == 0
    assert get_rooms.await_count == 3
    warning.assert_not_called()
    cleanup_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_ready_restarts_a_finished_recovery_worker(tmp_path: Path) -> None:
    """A readiness signal must re-arm recovery after its worker exits."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator._paused = False
    finished_worker = asyncio.create_task(asyncio.sleep(0))
    await finished_worker
    coordinator._worker_task = finished_worker

    coordinator.owner_ready(owner.user_id)
    replacement_worker = coordinator._worker_task
    try:
        assert replacement_worker is not finished_worker
        assert replacement_worker is not None
        assert not replacement_worker.done()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_owner_ready_during_delivery_preserves_closed_watermark(
    tmp_path: Path,
) -> None:
    """A readiness refresh must not discard an admitted delivery settlement."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$target", timestamp_ms=10)
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivered: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def deliver(
        _owner: RecoveryOwner,
        delivered_target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered.append(delivered_target.target_event_id)
        if len(delivered) == 1:
            delivery_started.set()
            await release_delivery.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)
    coordinator.owner_ready(owner.user_id)
    release_delivery.set()
    try:
        await _wait_until(
            lambda: not coordinator._room_jobs and not coordinator._active_attempts,
        )
        watermarks = dict(coordinator._target_watermarks)
    finally:
        release_delivery.set()
        await coordinator.stop()

    assert delivered == [target.target_event_id]
    watermark = watermarks[(owner.user_id, target.room_id, target.thread_id)]
    assert watermark.generation is owner.generation
    assert watermark.closed


@pytest.mark.asyncio
async def test_resume_releases_superseded_generation_watermarks(tmp_path: Path) -> None:
    """Resuming after config mutation must not retain stopped bot generations."""
    old_owner = _owner(generation=object(), rooms=frozenset())
    owner = _owner(generation=object(), rooms=frozenset())
    owners = {owner.user_id: owner}
    stale_target = _target("$stale", timestamp_ms=10, thread_id="$stale-thread")
    current_target = _target("$current", timestamp_ms=20, thread_id="$current-thread")

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator._advance_watermark(old_owner, TargetSettlement(stale_target, closed=True))
    coordinator._advance_watermark(owner, TargetSettlement(current_target, closed=True))
    coordinator._startup_cutoff_ms = 123

    coordinator.resume()
    try:
        watermarks = dict(coordinator._target_watermarks)
    finally:
        await coordinator.stop()

    assert (owner.user_id, stale_target.room_id, stale_target.thread_id) not in watermarks
    current_key = (owner.user_id, current_target.room_id, current_target.thread_id)
    assert watermarks[current_key].generation is owner.generation


@pytest.mark.asyncio
async def test_successful_rescan_discards_retained_ambiguous_delivery_target(
    tmp_path: Path,
) -> None:
    """History dedupe must win over a target retained after an ambiguous send."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$target", timestamp_ms=10)
    scan_attempts = 0
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_attempts
        scan_attempts += 1
        if scan_attempts == 1:
            return RoomRecoveryResult(interrupted_threads=(target,))
        return RoomRecoveryResult(
            auto_resume_target_keys=frozenset({(owner.user_id, target.target_event_id)}),
        )

    async def deliver(
        _owner: RecoveryOwner,
        delivered_target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(delivered_target.target_event_id)
        return False

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(
            lambda: scan_attempts >= 2 and not coordinator._room_jobs and not coordinator._active_attempts,
        )
    finally:
        await coordinator.stop()

    assert scan_attempts == 2
    assert delivered_targets == [target.target_event_id]


@pytest.mark.asyncio
async def test_failed_rescan_defers_retained_ambiguous_delivery_target(
    tmp_path: Path,
) -> None:
    """A retained target must not resend while history cannot prove it is unresumed."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$target", timestamp_ms=10)
    scan_attempts = 0
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_attempts
        scan_attempts += 1
        if scan_attempts == 1:
            return RoomRecoveryResult(interrupted_threads=(target,))
        # History read keeps failing, so this scan proves nothing about whether
        # the ambiguous first relay already landed.
        return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))

    async def deliver(
        _owner: RecoveryOwner,
        delivered_target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(delivered_target.target_event_id)
        # Matrix committed the relay but the response was lost.
        return False

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(lambda: scan_attempts >= 4)
    finally:
        await coordinator.stop()

    assert delivered_targets == [target.target_event_id]


@pytest.mark.asyncio
async def test_ready_owner_immediately_retries_startup_and_replacement_room_intents(
    tmp_path: Path,
) -> None:
    """Readiness must wake both semantic room intents without waiting for backoff."""
    startup_request = (123, False)
    replacement_request = (None, True)
    generation = object()
    owner = _owner(generation=generation, ready=False)
    owners = {owner.user_id: owner}
    processed_requests: list[tuple[int | None, bool]] = []
    all_processed = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        processed_requests.append(
            (request.startup_cutoff_ms, request.terminal_interrupted_only),
        )
        if set(processed_requests) == {startup_request, replacement_request}:
            all_processed.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 60.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    coordinator.enqueue_replacement_rooms(owner.user_id, {"!code:example.org"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    owners[owner.user_id] = _owner(generation=generation, ready=True)
    coordinator.owner_ready(owner.user_id)
    try:
        await asyncio.wait_for(all_processed.wait(), timeout=0.5)
        await _wait_until(
            lambda: not coordinator._room_jobs and not coordinator._active_attempts,
        )
        coordinator.owner_ready(owner.user_id)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    assert set(processed_requests) == {startup_request, replacement_request}
    assert processed_requests.count(replacement_request) == 1


@pytest.mark.asyncio
async def test_owner_ready_reenrolls_dropped_replacement_room_intent(tmp_path: Path) -> None:
    """A capped replacement scan must remain enrollable on later readiness."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}
    attempts = 0
    sixth_attempt = asyncio.Event()
    reenrolled = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal attempts
        assert request.terminal_interrupted_only
        attempts += 1
        if attempts == 6:
            sixth_attempt.set()
        if attempts == 7:
            reenrolled.set()
        return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    coordinator.enqueue_replacement_rooms(owner.user_id, {"!code:example.org"})
    await asyncio.wait_for(sixth_attempt.wait(), timeout=1.0)
    await _wait_until(
        lambda: not coordinator._active_attempts and next(iter(coordinator._room_jobs.values())).due_at is None,
    )

    coordinator.owner_ready(owner.user_id)
    try:
        await asyncio.wait_for(reenrolled.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert attempts == 7


@pytest.mark.asyncio
async def test_early_owner_refresh_backs_off_until_first_sync_then_progresses(
    tmp_path: Path,
) -> None:
    """Room setup before first sync must not spin or hide the later ready edge."""
    generation = object()
    owner = _owner(generation=generation, ready=False)
    owners = {owner.user_id: owner}
    recover_room = AsyncMock(return_value=RoomRecoveryResult())
    retry_attempts: list[int] = []

    def retry_delay(attempt: int) -> float:
        retry_attempts.append(attempt)
        return 60.0

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=retry_delay,
    )
    coordinator.start(startup_cutoff_ms=123)
    await _wait_until(lambda: retry_attempts == [1])
    retained = next(iter(coordinator._room_jobs.values()))
    assert retry_attempts == [1]
    assert retained.matrix_attempt == 0
    assert retained.due_at > asyncio.get_running_loop().time()

    owners[owner.user_id] = _owner(generation=generation, ready=True)
    coordinator.owner_ready(owner.user_id)
    ready_work = next(iter(coordinator._room_jobs.values()))
    assert ready_work.due_at <= asyncio.get_running_loop().time()
    try:
        await _wait_until(lambda: recover_room.await_count == 1)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_owner_readiness_wait_preserves_matrix_retry_budget(tmp_path: Path) -> None:
    """Waiting for first sync must not consume attempts needed by later Matrix retries."""
    generation = object()
    owner = _owner(generation=generation, ready=False)
    owners = {owner.user_id: owner}
    readiness_waited = asyncio.Event()
    recovered = asyncio.Event()
    scan_attempts = 0

    def retry_delay(_attempt: int) -> float:
        if not owners[owner.user_id].first_sync_complete:
            readiness_waited.set()
            return 60.0
        return 0.0

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_attempts
        scan_attempts += 1
        if scan_attempts < 6:
            return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))
        recovered.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=retry_delay,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(readiness_waited.wait(), timeout=1.0)

    owners[owner.user_id] = _owner(generation=generation, ready=True)
    coordinator.owner_ready(owner.user_id)
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert scan_attempts == 6


@pytest.mark.asyncio
async def test_requester_retry_does_not_fence_same_target_after_resolution(
    tmp_path: Path,
) -> None:
    """An indeterminate requester must remain unsettled until its retry resolves."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    unresolved = _target(
        "$target",
        timestamp_ms=10,
        original_sender_id=None,
    )
    resolved = _target("$target", timestamp_ms=10)
    scans = 0
    second_scan_finished = asyncio.Event()
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scans
        scans += 1
        if scans == 1:
            return RoomRecoveryResult(
                interrupted_threads=(unresolved,),
                retry_owner_user_ids=frozenset({owner.user_id}),
            )
        second_scan_finished.set()
        return RoomRecoveryResult(interrupted_threads=(resolved,))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(second_scan_finished.wait(), timeout=1.0)
        await _wait_until(
            lambda: not coordinator._room_jobs and all(task.done() for task in coordinator._active_attempts),
        )
    finally:
        await coordinator.stop()

    assert scans == 2
    assert delivered_targets == ["$target"]


@pytest.mark.asyncio
async def test_requester_retry_preserves_newest_same_thread_target(
    tmp_path: Path,
) -> None:
    """A transiently unresolved newest target must fence an older same-thread target."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    unresolved_newest = _target(
        "$new",
        timestamp_ms=20,
        original_sender_id=None,
    )
    resolved_newest = _target("$new", timestamp_ms=20)
    resolved_older = _target("$old", timestamp_ms=10)
    scans = 0
    second_scan_finished = asyncio.Event()
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scans
        scans += 1
        if scans == 1:
            return RoomRecoveryResult(
                interrupted_threads=(unresolved_newest, resolved_older),
                retry_owner_user_ids=frozenset({owner.user_id}),
            )
        second_scan_finished.set()
        return RoomRecoveryResult(interrupted_threads=(resolved_newest,))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(second_scan_finished.wait(), timeout=1.0)
        await _wait_until(
            lambda: not coordinator._room_jobs and all(task.done() for task in coordinator._active_attempts),
        )
    finally:
        await coordinator.stop()

    assert scans == 2
    assert delivered_targets == ["$new"]


@pytest.mark.asyncio
async def test_terminal_unresolved_requester_warns_before_settlement(
    tmp_path: Path,
) -> None:
    """A permanently unresumable target must remain visible to operators."""
    owner = _owner()
    target = _target(
        "$target",
        timestamp_ms=10,
        original_sender_id=None,
    )
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: {owner.user_id: owner},
        operations=_operations(
            recover_room=AsyncMock(
                return_value=RoomRecoveryResult(interrupted_threads=(target,)),
            ),
        ),
    )

    with patch("mindroom.restart_recovery.logger.warning") as warning:
        coordinator.start(startup_cutoff_ms=123)
        try:
            await _wait_until(
                lambda: not coordinator._room_jobs and not coordinator._active_attempts,
            )
        finally:
            await coordinator.stop()

    warning.assert_called_once_with(
        "Skipping auto-resume because requester identity could not be resolved",
        room_id=target.room_id,
        thread_id=target.thread_id,
        target_event_id=target.target_event_id,
    )


@pytest.mark.asyncio
async def test_unresolved_room_alias_is_terminal_until_owner_refresh(
    tmp_path: Path,
) -> None:
    """Raw aliases must not create permanent retry work before room setup."""
    generation = object()
    owner = _owner(generation=generation, rooms=frozenset({"lobby"}))
    owners = {owner.user_id: owner}
    recover_room = AsyncMock(return_value=RoomRecoveryResult())
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )

    coordinator.start(startup_cutoff_ms=123)
    try:
        assert not coordinator._room_jobs
        owners[owner.user_id] = _owner(
            generation=generation,
            rooms=frozenset({"!lobby:example.org"}),
        )
        coordinator.owner_ready(owner.user_id)
        assert {work.room_id for work in coordinator._room_jobs.values()} == {
            "!lobby:example.org",
        }
        await _wait_until(
            lambda: not coordinator._room_jobs and all(task.done() for task in coordinator._active_attempts),
        )
        coordinator.owner_ready(owner.user_id)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    recover_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_replacement_enqueue_during_same_active_scan_latches_one_rerun(tmp_path: Path) -> None:
    """A newer same-room handoff must survive an already-running recovery scan."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}
    first_scan_started = asyncio.Event()
    release_first_scan = asyncio.Event()
    second_scan_finished = asyncio.Event()
    attempts = 0
    active_attempts = 0
    max_active_attempts = 0

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal active_attempts, attempts, max_active_attempts
        assert request.room_id == room_id
        attempts += 1
        active_attempts += 1
        max_active_attempts = max(max_active_attempts, active_attempts)
        try:
            if attempts == 1:
                first_scan_started.set()
                await release_first_scan.wait()
            else:
                second_scan_finished.set()
            return RoomRecoveryResult()
        finally:
            active_attempts -= 1

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    coordinator.enqueue_replacement_rooms(owner.user_id, {room_id})
    await asyncio.wait_for(first_scan_started.wait(), timeout=1.0)

    coordinator.enqueue_replacement_rooms(owner.user_id, {room_id})
    try:
        coordinator._start_due_attempts()
        await asyncio.sleep(0)
        assert attempts == 1
        release_first_scan.set()
        await asyncio.wait_for(second_scan_finished.wait(), timeout=1.0)
    finally:
        release_first_scan.set()
        await coordinator.stop()

    assert attempts == 2
    assert max_active_attempts == 1


@pytest.mark.asyncio
async def test_startup_and_terminal_room_intents_share_one_owner_room_lease(
    tmp_path: Path,
) -> None:
    """Startup and terminal-only intents for one owner-room must never scan concurrently."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    owners = {owner.user_id: owner}
    first_scan_started = asyncio.Event()
    release_first_scan = asyncio.Event()
    second_scan_finished = asyncio.Event()
    processed_intents: list[bool] = []
    active_attempts = 0
    max_active_attempts = 0

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal active_attempts, max_active_attempts
        processed_intents.append(request.terminal_interrupted_only)
        active_attempts += 1
        max_active_attempts = max(max_active_attempts, active_attempts)
        try:
            if len(processed_intents) == 1:
                first_scan_started.set()
                await release_first_scan.wait()
            else:
                second_scan_finished.set()
            return RoomRecoveryResult()
        finally:
            active_attempts -= 1

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(first_scan_started.wait(), timeout=1.0)

    coordinator.enqueue_replacement_rooms(owner.user_id, {room_id})
    try:
        coordinator._start_due_attempts()
        await asyncio.sleep(0)
        assert processed_intents == [False]
        release_first_scan.set()
        await asyncio.wait_for(second_scan_finished.wait(), timeout=1.0)
    finally:
        release_first_scan.set()
        await coordinator.stop()

    assert processed_intents == [False, True]
    assert max_active_attempts == 1


@pytest.mark.asyncio
async def test_target_freshness_and_delivery_failures_retry_autonomously(tmp_path: Path) -> None:
    """Final target reads and sends must retry without another readiness event."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    freshness_attempts = 0
    delivery_attempts = 0
    delivered = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(
            interrupted_threads=(_target("$target", timestamp_ms=10),),
        )

    async def freshness(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        nonlocal freshness_attempts
        freshness_attempts += 1
        if freshness_attempts == 1:
            return InterruptedTargetFreshness.RETRY
        return InterruptedTargetFreshness.CURRENT

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_attempts
        delivery_attempts += 1
        if delivery_attempts == 2:
            delivered.set()
            return True
        return False

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(
            recover_room=recover_room,
            freshness=freshness,
            deliver=deliver,
        ),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert freshness_attempts == 3
    assert delivery_attempts == 2


@pytest.mark.asyncio
async def test_agent_only_room_delivers_resume_through_exact_owner(tmp_path: Path) -> None:
    """An interrupted owner must deliver its own relay when no router transport exists."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$target", timestamp_ms=10)
    delivery_senders: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def deliver(
        sender: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivery_senders.append(sender.user_id)
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(lambda: not coordinator._room_jobs and not coordinator._active_attempts)
    finally:
        await coordinator.stop()

    assert delivery_senders == [owner.user_id]


@pytest.mark.asyncio
async def test_permanent_target_freshness_retry_stops_with_terminal_warning(
    tmp_path: Path,
) -> None:
    """A permanently unreadable target must not poll for the process lifetime."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    target = _target("$target", timestamp_ms=10)
    freshness_attempts = 0
    sixth_attempt = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def freshness(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        nonlocal freshness_attempts
        freshness_attempts += 1
        if freshness_attempts == 6:
            sixth_attempt.set()
        return InterruptedTargetFreshness.RETRY

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, freshness=freshness),
        retry_delay=lambda _attempt: 0.0,
    )
    with patch("mindroom.restart_recovery.logger.warning") as warning:
        coordinator.start(startup_cutoff_ms=123)
        try:
            await asyncio.wait_for(sixth_attempt.wait(), timeout=1.0)
            await _wait_until(
                lambda: not coordinator._active_attempts and next(iter(coordinator._room_jobs.values())).due_at is None,
            )
        finally:
            await coordinator.stop()

    assert freshness_attempts == 6
    warning.assert_called_once_with(
        "Restart recovery exhausted retries; parking retained owner work",
        attempt=6,
        owner_user_id=owner.user_id,
        room_id=target.room_id,
        target_event_ids=[target.target_event_id],
    )


@pytest.mark.asyncio
async def test_owner_ready_reenrolls_parked_startup_target(tmp_path: Path) -> None:
    """Fresh owner readiness must rescan a startup target parked after retries."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$target", timestamp_ms=10)
    scan_attempts = 0
    freshness_attempts = 0
    retry_parked = asyncio.Event()
    target_retried = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_attempts
        scan_attempts += 1
        if scan_attempts == 7:
            target_retried.set()
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def freshness(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        nonlocal freshness_attempts
        freshness_attempts += 1
        if freshness_attempts == 6:
            retry_parked.set()
        return InterruptedTargetFreshness.RETRY

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, freshness=freshness),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(retry_parked.wait(), timeout=1.0)
    await _wait_until(
        lambda: not coordinator._active_attempts and next(iter(coordinator._room_jobs.values())).due_at is None,
    )

    coordinator.owner_ready(owner.user_id)
    try:
        await asyncio.wait_for(target_retried.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert scan_attempts == 7


@pytest.mark.asyncio
async def test_terminal_delivery_freshness_settles_without_retry(tmp_path: Path) -> None:
    """Post-pacing terminal freshness must settle instead of becoming send failure."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    target = _target("$target", timestamp_ms=10)
    delivery_attempts = 0

    async def recover_room(
        _owners: tuple[RecoveryOwner, ...],
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def freshness(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        return InterruptedTargetFreshness.CURRENT

    async def terminal_delivery(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> restart_recovery_operations_module.RestartDeliveryOutcome:
        nonlocal delivery_attempts
        delivery_attempts += 1
        return restart_recovery_operations_module.RestartDeliveryOutcome.TERMINAL

    async def drain_membership_snapshots() -> None:
        return None

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=RestartRecoveryOperations(
            joined_rooms=AsyncMock(side_effect=lambda owner: list(owner.desired_room_ids)),
            membership_refresh_delay_seconds=0.0,
            recover_room=recover_room,
            target_freshness=freshness,
            deliver_target=terminal_delivery,
            discard_owner=lambda _owner_user_id: None,
            drain_membership_snapshots=drain_membership_snapshots,
        ),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(lambda: not coordinator._room_jobs and not coordinator._active_attempts)
    finally:
        await coordinator.stop()

    assert delivery_attempts == 1


@pytest.mark.asyncio
async def test_queued_delivery_rechecks_freshness_after_delivery_lock(
    tmp_path: Path,
) -> None:
    """A queued target must not send after human activity while waiting for delivery."""
    owner = _owner()
    first_target = _target("$first", timestamp_ms=10, thread_id="$first-thread")
    queued_target = _target("$queued", timestamp_ms=20, thread_id="$queued-thread")
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()
    queued_processing_started = asyncio.Event()
    freshness_checks: dict[str, int] = {}
    delivered: list[str] = []
    human_activity = False

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult()

    async def freshness(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        attempt = freshness_checks.get(target.target_event_id, 0) + 1
        freshness_checks[target.target_event_id] = attempt
        if target is queued_target and human_activity:
            return InterruptedTargetFreshness.NEWER_HUMAN
        return InterruptedTargetFreshness.CURRENT

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered.append(target.target_event_id)
        if target is first_target:
            first_delivery_started.set()
            await release_first_delivery.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: {owner.user_id: owner},
        operations=_operations(
            recover_room=recover_room,
            freshness=freshness,
            deliver=deliver,
        ),
        retry_delay=lambda _attempt: 0.0,
    )
    config = _config(tmp_path)
    first_task = asyncio.create_task(coordinator._process_target(owner, first_target, config))
    await asyncio.wait_for(first_delivery_started.wait(), timeout=1.0)

    async def process_queued_target() -> TargetSettlement | None:
        queued_processing_started.set()
        return await coordinator._process_target(owner, queued_target, config)

    queued_task = asyncio.create_task(process_queued_target())
    await asyncio.wait_for(queued_processing_started.wait(), timeout=1.0)
    await asyncio.sleep(0)
    human_activity = True
    release_first_delivery.set()

    try:
        first_settlement, queued_settlement = await asyncio.gather(first_task, queued_task)
    finally:
        await coordinator.stop()

    assert first_settlement == TargetSettlement(first_target, closed=True)
    assert queued_settlement == TargetSettlement(queued_target, closed=False)
    assert freshness_checks[queued_target.target_event_id] == 1
    assert delivered == [first_target.target_event_id]


@pytest.mark.asyncio
async def test_room_lease_snapshots_current_owners_once(tmp_path: Path) -> None:
    """One room lease must not rebuild durable owner scope per target."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    owner_snapshot_calls = 0
    delivered_count = 0
    all_delivered = asyncio.Event()

    def current_owners() -> dict[str, RecoveryOwner]:
        nonlocal owner_snapshot_calls
        owner_snapshot_calls += 1
        return owners

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(
            interrupted_threads=tuple(
                _target(
                    f"$target-{index}",
                    timestamp_ms=index,
                    thread_id=f"$thread-{index}",
                )
                for index in range(10)
            ),
        )

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivered_count
        delivered_count += 1
        if delivered_count == 10:
            all_delivered.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=current_owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    try:
        request = RoomRecoveryRequest(
            room_id="!code:example.org",
            startup_cutoff_ms=123,
            terminal_interrupted_only=False,
        )
        await coordinator._process_room(
            RoomLease(
                request,
                (RoomWork(request, owner.user_id, owner.generation, due_at=None),),
            ),
        )
        await asyncio.wait_for(all_delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert owner_snapshot_calls == 1


@pytest.mark.asyncio
async def test_cancelled_pause_drains_successful_delivery_and_settles_target(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """An ambiguous cancellation must not resend a delivery that eventually succeeds."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    target = _target("$target", timestamp_ms=10)
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    second_scan_finished = asyncio.Event()
    delivery_cancelled = False
    delivery_attempts = 0
    scan_count = 0

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_count
        scan_count += 1
        if scan_count == 2:
            second_scan_finished.set()
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_attempts, delivery_cancelled
        delivery_attempts += 1
        delivery_started.set()
        try:
            await release_delivery.wait()
        except asyncio.CancelledError:
            delivery_cancelled = True
            raise
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)

    pause_task = asyncio.create_task(coordinator.pause())
    try:
        await asyncio.sleep(0)
        assert not pause_task.done()
        pause_task.cancel()
        await asyncio.sleep(0)
        assert not pause_task.done()
        pause_task.cancel()
        await asyncio.sleep(0)
        assert not pause_task.done()
    finally:
        release_delivery.set()
    with pytest.raises(asyncio.CancelledError):
        await pause_task

    coordinator.resume()
    coordinator.enqueue_replacement_rooms(owner.user_id, {target.room_id})
    try:
        await asyncio.wait_for(second_scan_finished.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    assert delivery_cancelled is False
    assert delivery_attempts == 1


@pytest.mark.asyncio
async def test_pause_drains_current_delivery_without_starting_later_target(
    tmp_path: Path,
) -> None:
    """Pause must settle the current delivery and retain every later target."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    first = _target("$first", timestamp_ms=10, thread_id="$thread-a")
    second = _target("$second", timestamp_ms=20, thread_id="$thread-b")
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    second_delivered = asyncio.Event()
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(first, second))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        if target is first:
            delivery_started.set()
            await release_delivery.wait()
        else:
            second_delivered.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)

    pause_task = asyncio.create_task(coordinator.pause())
    await asyncio.sleep(0)
    release_delivery.set()
    await pause_task
    assert delivered_targets == ["$first"]

    coordinator.resume()
    try:
        await asyncio.wait_for(second_delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivered_targets == ["$first", "$second"]


@pytest.mark.asyncio
async def test_pause_during_later_freshness_settles_prior_delivery_and_retains_target(
    tmp_path: Path,
) -> None:
    """Cancellation outside delivery must preserve partial attempt progress."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    first = _target("$first", timestamp_ms=10, thread_id="$thread-a")
    second = _target("$second", timestamp_ms=20, thread_id="$thread-b")
    second_freshness_started = asyncio.Event()
    second_delivered = asyncio.Event()
    second_freshness_attempts = 0
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(first, second))

    async def freshness(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        nonlocal second_freshness_attempts
        if target is second:
            second_freshness_attempts += 1
            if second_freshness_attempts == 1:
                second_freshness_started.set()
                await asyncio.Event().wait()
        return InterruptedTargetFreshness.CURRENT

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        if target is second:
            second_delivered.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(
            recover_room=recover_room,
            freshness=freshness,
            deliver=deliver,
        ),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(second_freshness_started.wait(), timeout=1.0)

    await coordinator.pause()

    first_key = (owner.user_id, first.room_id, first.thread_id)
    retained_targets = tuple(
        target.target_event_id for work in coordinator._room_jobs.values() for target in work.targets
    )
    assert delivered_targets == ["$first"]
    assert coordinator._target_watermarks[first_key].closed is True
    assert retained_targets == ("$second",)

    coordinator.resume()
    try:
        await asyncio.wait_for(second_delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivered_targets == ["$first", "$second"]


@pytest.mark.asyncio
async def test_cancelled_pause_drains_failed_delivery_and_restores_target(
    tmp_path: Path,
) -> None:
    """A drained delivery failure must retry the exact target after resume."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    target = _target("$target", timestamp_ms=10)
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivered = asyncio.Event()
    delivery_cancelled = False
    delivery_attempts = 0

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_attempts, delivery_cancelled
        delivery_attempts += 1
        if delivery_attempts == 1:
            delivery_started.set()
            try:
                await release_delivery.wait()
            except asyncio.CancelledError:
                delivery_cancelled = True
                raise
            return False
        delivered.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)

    pause_task = asyncio.create_task(coordinator.pause())
    try:
        await asyncio.sleep(0)
        assert not pause_task.done()
    finally:
        release_delivery.set()
    await pause_task

    coordinator.resume()
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivery_cancelled is False
    assert delivery_attempts == 2


@pytest.mark.asyncio
async def test_unrecoverable_target_is_settled_without_future_retry(tmp_path: Path) -> None:
    """Authoritative target absence must settle the exact version permanently."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$missing", timestamp_ms=10)
    second_scan = asyncio.Event()
    freshness_checked = asyncio.Event()
    scan_count = 0
    freshness_attempts = 0

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_count
        scan_count += 1
        if scan_count == 2:
            second_scan.set()
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def freshness(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> InterruptedTargetFreshness:
        nonlocal freshness_attempts
        freshness_attempts += 1
        freshness_checked.set()
        return InterruptedTargetFreshness.UNRECOVERABLE

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        pytest.fail("unrecoverable target must not be delivered")

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(
            recover_room=recover_room,
            freshness=freshness,
            deliver=deliver,
        ),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)

    try:
        await asyncio.wait_for(freshness_checked.wait(), timeout=1.0)
        key = (owner.user_id, target.room_id, target.thread_id)
        active_attempts = tuple(coordinator._active_attempts)
        if active_attempts:
            await asyncio.gather(*active_attempts)
        coordinator._settle_finished_attempts()
        assert key in coordinator._target_watermarks
        coordinator.enqueue_replacement_rooms(owner.user_id, {target.room_id})
        await asyncio.wait_for(second_scan.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    assert freshness_attempts == 1


def test_orchestrator_recovery_owner_uses_live_room_scope_without_disk_read(
    tmp_path: Path,
) -> None:
    """Repeated owner snapshots must reuse the bot lifecycle's loaded room scope."""
    config = _config(tmp_path)
    config.agents["code"].accept_invites = True
    runtime_paths = test_runtime_paths(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    bot = MagicMock(spec=AgentBot)
    bot.agent_name = "code"
    bot.agent_user = agent_matrix_user("code")
    bot.client = client
    bot.rooms = ["!configured:example.org"]
    bot.running = True
    bot.first_sync_complete = True
    bot.conversation_cache = MagicMock()
    bot.restart_recovery_room_ids = frozenset(
        {"!configured:example.org", "!invited:example.org"},
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths)
    orchestrator.config = config
    orchestrator.agent_bots = {"code": bot}

    owners = orchestrator._restart_recovery_owners()
    refreshed_owners = orchestrator._restart_recovery_owners()

    assert owners["@code:example.org"].desired_room_ids == frozenset(
        {"!configured:example.org", "!invited:example.org"},
    )
    assert refreshed_owners["@code:example.org"].desired_room_ids == owners["@code:example.org"].desired_room_ids
    assert owners["@code:example.org"].generation is bot


def test_restart_recovery_room_hints_include_current_joined_rooms() -> None:
    """Local recovery hints must retain known DMs and spaces."""
    bot = MagicMock(spec=AgentBot)
    bot.rooms = ["!configured:example.org"]
    bot.client = MagicMock(spec=nio.AsyncClient)
    bot.client.rooms = {
        "!configured:example.org": MagicMock(),
        "!dm:example.org": MagicMock(),
        "!root-space:example.org": MagicMock(),
    }
    bot._room_lifecycle = MagicMock()
    bot._room_lifecycle.desired_room_ids = frozenset(
        {"!configured:example.org", "!invited:example.org"},
    )
    bot._room_lifecycle.should_persist_invited_rooms.side_effect = AssertionError("duplicate policy")

    room_ids = AgentBot.restart_recovery_room_ids.fget(bot)

    assert room_ids == frozenset(
        {
            "!configured:example.org",
            "!dm:example.org",
            "!root-space:example.org",
            "!invited:example.org",
        },
    )


@pytest.mark.asyncio
async def test_failed_lease_log_includes_room_and_owner_context(tmp_path: Path) -> None:
    """Unexpected lease failures must identify the affected room and owners."""
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=dict,
        operations=_operations(recover_room=AsyncMock(return_value=RoomRecoveryResult())),
    )
    request = RoomRecoveryRequest("!failed:example.org", 123, False)
    work = RoomWork(request, "@code:example.org", None, due_at=0.0)
    lease = RoomLease(request, (work,))
    task = MagicMock(spec=asyncio.Task)
    task.result.side_effect = RuntimeError("boom")
    coordinator._room_jobs = {work.key: work}

    with patch("mindroom.restart_recovery.logger.warning") as warning:
        coordinator._settle_attempt(lease, cast("asyncio.Task[object]", task))

    warning.assert_called_once_with(
        "Restart recovery attempt failed",
        room_id=request.room_id,
        owner_user_ids=[work.owner_user_id],
        exc_info=True,
    )


@pytest.mark.asyncio
async def test_startup_discovers_server_joined_rooms_outside_local_scope(
    tmp_path: Path,
) -> None:
    """Startup recovery must scan joined rooms omitted from the local sync window."""
    hidden_room_id = "!outside-sliding-window:example.org"
    owner = _owner(rooms=frozenset())
    owner.client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError(
            "not found",
            "M_NOT_FOUND",
            room_id=hidden_room_id,
        ),
    )
    owners = {owner.user_id: owner}
    scanned = asyncio.Event()

    async def cleanup_room(
        _client: nio.AsyncClient,
        *,
        room_id: str,
        **_kwargs: object,
    ) -> stale_stream_cleanup_module.StaleStreamCleanupResult:
        assert room_id == hidden_room_id
        scanned.set()
        return stale_stream_cleanup_module.StaleStreamCleanupResult(
            cleaned_count=0,
            interrupted_threads=(),
        )

    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=operations,
    )

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=[hidden_room_id]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(side_effect=cleanup_room),
        ),
    ):
        coordinator.start(startup_cutoff_ms=123)
        try:
            await asyncio.wait_for(scanned.wait(), timeout=1.0)
        finally:
            await coordinator.stop()


@pytest.mark.asyncio
async def test_pause_requeues_active_job_and_keeps_work_enqueued_while_paused(tmp_path: Path) -> None:
    """Config pause must retain both the cancelled lease and concurrent notifications."""
    first_room = "!first:example.org"
    second_room = "!second:example.org"
    owner = _owner(rooms=frozenset({first_room}))
    owners = {owner.user_id: owner}
    first_attempt_started = asyncio.Event()
    processed_rooms: list[str] = []
    first_attempt = True
    all_processed = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            first_attempt_started.set()
            await asyncio.Event().wait()
        processed_rooms.append(request.room_id)
        if set(processed_rooms) == {first_room, second_room}:
            all_processed.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(first_attempt_started.wait(), timeout=1.0)

    await coordinator.pause()
    coordinator.enqueue_replacement_rooms(owner.user_id, {second_room})
    await asyncio.sleep(0)
    assert processed_rooms == []

    coordinator.resume()
    try:
        await asyncio.wait_for(all_processed.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert set(processed_rooms) == {first_room, second_room}


@pytest.mark.asyncio
async def test_repeated_pause_cancellation_drains_worker_before_propagating(
    tmp_path: Path,
) -> None:
    """Repeated caller cancellation must not orphan the leased recovery job."""
    owner = _owner()
    owners = {owner.user_id: owner}
    first_attempt_started = asyncio.Event()
    cancellation_cleanup_started = asyncio.Event()
    release_cancellation_cleanup = asyncio.Event()
    recovered = asyncio.Event()
    attempts = 0

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancellation_cleanup_started.set()
                while not release_cancellation_cleanup.is_set():
                    try:
                        await asyncio.shield(release_cancellation_cleanup.wait())
                    except asyncio.CancelledError:
                        continue
        recovered.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(first_attempt_started.wait(), timeout=1.0)

    pause_task = asyncio.create_task(coordinator.pause())
    await asyncio.wait_for(cancellation_cleanup_started.wait(), timeout=1.0)
    pause_task.cancel()
    await asyncio.sleep(0)
    assert not pause_task.done()
    pause_task.cancel()
    await asyncio.sleep(0)
    assert not pause_task.done()
    release_cancellation_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await pause_task

    coordinator.resume()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert attempts == 2


@pytest.mark.asyncio
async def test_pause_cancels_discovery_and_active_lease_together(
    tmp_path: Path,
) -> None:
    """Slow discovery cleanup must not leave a lease free to start another send."""
    owner = _owner()
    owners = {owner.user_id: owner}
    first_target = _target("$first", timestamp_ms=10, thread_id="$first-thread")
    second_target = _target("$second", timestamp_ms=20, thread_id="$second-thread")
    discovery_started = asyncio.Event()
    discovery_cleanup_started = asyncio.Event()
    release_discovery_cleanup = asyncio.Event()
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()
    second_delivery_started = asyncio.Event()

    async def joined_rooms(_owner: RecoveryOwner) -> list[str]:
        discovery_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            discovery_cleanup_started.set()
            while not release_discovery_cleanup.is_set():
                try:
                    await asyncio.shield(release_discovery_cleanup.wait())
                except asyncio.CancelledError:
                    continue
        return []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(first_target, second_target))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        if target is first_target:
            first_delivery_started.set()
            await release_first_delivery.wait()
        else:
            second_delivery_started.set()
        return True

    operations = dataclasses.replace(
        _operations(recover_room=recover_room, deliver=deliver),
        joined_rooms=joined_rooms,
    )
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=operations,
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(discovery_started.wait(), timeout=1.0)
    await asyncio.wait_for(first_delivery_started.wait(), timeout=1.0)

    pause_task = asyncio.create_task(coordinator.pause())
    try:
        await asyncio.wait_for(discovery_cleanup_started.wait(), timeout=1.0)
        release_first_delivery.set()
        await _wait_until(
            lambda: second_delivery_started.is_set() or all(task.done() for task in coordinator._active_attempts),
        )
        assert not second_delivery_started.is_set()

        release_discovery_cleanup.set()
        await asyncio.wait_for(pause_task, timeout=1.0)
        assert not coordinator._active_attempts
    finally:
        release_discovery_cleanup.set()
        release_first_delivery.set()
        await asyncio.gather(pause_task, return_exceptions=True)
        await coordinator.stop()


@pytest.mark.asyncio
async def test_orchestrator_cancelled_pause_resumes_coordinator(tmp_path: Path) -> None:
    """A cancelled reload must not leave retained recovery work permanently paused."""
    orchestrator = _MultiAgentOrchestrator(test_runtime_paths(tmp_path))
    orchestrator.running = True
    orchestrator.config = _config(tmp_path)
    orchestrator._restart_recovery.pause = AsyncMock(side_effect=asyncio.CancelledError)
    orchestrator._restart_recovery.resume = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._pause_restart_recovery()

    orchestrator._restart_recovery.resume.assert_called_once_with()


@pytest.mark.asyncio
async def test_orchestrator_cancelled_pause_does_not_resume_during_shutdown(tmp_path: Path) -> None:
    """Shutdown cancellation must not restart recovery before its stop phase."""
    orchestrator = _MultiAgentOrchestrator(test_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    orchestrator._restart_recovery.pause = AsyncMock(side_effect=asyncio.CancelledError)
    orchestrator._restart_recovery.resume = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._pause_restart_recovery()

    orchestrator._restart_recovery.resume.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_restart_finally_does_not_resume_recovery_during_shutdown(tmp_path: Path) -> None:
    """Catalog-restart cleanup must not reactivate recovery after shutdown starts."""
    orchestrator = _MultiAgentOrchestrator(test_runtime_paths(tmp_path))
    orchestrator.running = True
    orchestrator.config = MagicMock(spec=Config)
    orchestrator.config.get_entities_referencing_tools.return_value = {"code"}
    orchestrator._restart_recovery.pause = AsyncMock()
    orchestrator._restart_recovery.resume = MagicMock()
    orchestrator._replacement_bots = MagicMock(return_value={})
    orchestrator._external_trigger_runtime.unbind_for_entity_changes = MagicMock()

    async def shutdown_during_restart(_entity_name: str) -> None:
        orchestrator.running = False
        raise asyncio.CancelledError

    orchestrator._cancel_bot_start_task = AsyncMock(side_effect=shutdown_during_restart)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._apply_mcp_catalog_change("server")

    orchestrator._restart_recovery.resume.assert_not_called()


@pytest.mark.asyncio
async def test_config_reload_finally_does_not_resume_recovery_during_shutdown(tmp_path: Path) -> None:
    """Config-reload cleanup must not reactivate recovery after shutdown starts."""
    orchestrator = _MultiAgentOrchestrator(test_runtime_paths(tmp_path))
    config = _config(tmp_path)
    orchestrator.running = True
    orchestrator.config = config
    orchestrator._restart_recovery.pause = AsyncMock()
    orchestrator._restart_recovery.resume = MagicMock()
    plan = ConfigUpdatePlan(
        new_config=config,
        changed_mcp_servers=set(),
        configured_entities={ROUTER_AGENT_NAME, "code"},
        entities_to_restart=set(),
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )

    async def shutdown_during_reload(_new_config: Config, _plan: ConfigUpdatePlan) -> None:
        orchestrator.running = False
        raise asyncio.CancelledError

    orchestrator._prepare_accounts_for_config_update = AsyncMock(side_effect=shutdown_during_reload)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._apply_config_update_plan(config, plan, ())

    orchestrator._restart_recovery.resume.assert_not_called()


@pytest.mark.asyncio
async def test_generation_change_discards_stale_room_result(tmp_path: Path) -> None:
    """Pause must drain the old generation before replacement recovery resumes."""
    old_generation = object()
    new_generation = object()
    old_owner = _owner(generation=old_generation)
    new_owner = _owner(generation=new_generation)
    owners = {old_owner.user_id: old_owner}
    old_scan_started = asyncio.Event()
    delivered_targets: list[str] = []
    delivered = asyncio.Event()

    async def recover_room(
        owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        if owner.generation is old_generation:
            old_scan_started.set()
            await asyncio.Event().wait()
        return RoomRecoveryResult(interrupted_threads=(_target("$current", timestamp_ms=2),))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        delivered.set()
        return True

    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners[router.user_id] = router
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(old_scan_started.wait(), timeout=1.0)
    await coordinator.pause()
    owners[old_owner.user_id] = new_owner
    coordinator.resume()
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivered_targets == ["$current"]


@pytest.mark.asyncio
async def test_generation_change_allows_new_interruption_in_same_thread(
    tmp_path: Path,
) -> None:
    """Replacement intent must run without repeating the completed startup scan."""
    old_generation = object()
    new_generation = object()
    room_id = "!code:example.org"
    old_owner = _owner(generation=old_generation, rooms=frozenset({room_id}))
    new_owner = _owner(generation=new_generation, rooms=frozenset({room_id}))
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {old_owner.user_id: old_owner, router.user_id: router}
    delivered_targets: list[str] = []
    requests: list[RoomRecoveryRequest] = []
    old_delivered = asyncio.Event()
    new_delivered = asyncio.Event()

    async def recover_room(
        owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        requests.append(request)
        target = (
            _target("$old-generation", timestamp_ms=10)
            if owner.generation is old_generation
            else _target("$new-generation", timestamp_ms=20)
        )
        return RoomRecoveryResult(interrupted_threads=(target,))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        if target.target_event_id == "$old-generation":
            old_delivered.set()
        else:
            new_delivered.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(old_delivered.wait(), timeout=1.0)
    active_attempts = tuple(coordinator._active_attempts)
    if active_attempts:
        await asyncio.gather(*active_attempts)
    coordinator._settle_finished_attempts()

    owners[old_owner.user_id] = new_owner
    coordinator.owner_ready(new_owner.user_id)
    assert not coordinator._room_jobs
    coordinator.enqueue_replacement_rooms(new_owner.user_id, {room_id})
    try:
        await asyncio.wait_for(new_delivered.wait(), timeout=0.2)
    finally:
        await coordinator.stop()

    assert delivered_targets == ["$old-generation", "$new-generation"]
    assert [(request.startup_cutoff_ms, request.terminal_interrupted_only) for request in requests] == [
        (123, False),
        (None, True),
    ]


@pytest.mark.asyncio
async def test_equal_timestamp_matrix_order_selects_newest_target(tmp_path: Path) -> None:
    """Opaque event IDs must not override exact same-timestamp Matrix ordering proof."""
    owner = _owner()
    old_target = _target("$z-old", timestamp_ms=10)
    new_target = _target(
        "$a-new",
        timestamp_ms=10,
        same_timestamp_older_event_ids=frozenset({old_target.target_event_id}),
    )
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(new_target, old_target))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: {owner.user_id: owner},
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(lambda: not coordinator._room_jobs and not coordinator._active_attempts)
    finally:
        await coordinator.stop()

    assert delivered_targets == [new_target.target_event_id]


@pytest.mark.asyncio
async def test_recovered_equal_timestamp_target_supersedes_retained_target(tmp_path: Path) -> None:
    """A current scan may replace retained work only with exact predecessor proof."""
    owner = _owner()
    old_target = _target("$z-old", timestamp_ms=10)
    new_target = _target(
        "$a-new",
        timestamp_ms=10,
        same_timestamp_older_event_ids=frozenset({old_target.target_event_id}),
    )
    delivered_targets: list[str] = []

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: {owner.user_id: owner},
        operations=_operations(
            recover_room=AsyncMock(return_value=RoomRecoveryResult(interrupted_threads=(new_target,))),
            deliver=deliver,
        ),
    )
    request = RoomRecoveryRequest(old_target.room_id, 123, False)
    work = RoomWork(
        request,
        owner.user_id,
        owner.generation,
        targets=(old_target,),
        due_at=None,
    )

    await coordinator._process_room(RoomLease(request, (work,)))

    assert delivered_targets == [new_target.target_event_id]


def test_open_watermark_requires_exact_equal_timestamp_predecessor_proof(tmp_path: Path) -> None:
    """Ambiguous equal-timestamp targets cannot advance a monotonic watermark."""
    owner = _owner()
    old_target = _target("$z-old", timestamp_ms=10)
    ambiguous_target = _target("$ambiguous", timestamp_ms=10)
    new_target = _target(
        "$a-new",
        timestamp_ms=10,
        same_timestamp_older_event_ids=frozenset({old_target.target_event_id}),
    )
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: {owner.user_id: owner},
        operations=_operations(recover_room=AsyncMock(return_value=RoomRecoveryResult())),
    )
    coordinator._advance_watermark(owner, TargetSettlement(old_target, closed=False))

    assert coordinator._eligible_targets(owner, (ambiguous_target,)) == ()
    assert coordinator._eligible_targets(owner, (new_target,)) == (new_target,)

    coordinator._advance_watermark(owner, TargetSettlement(new_target, closed=False))

    assert coordinator._eligible_targets(owner, (old_target,)) == ()


@pytest.mark.asyncio
async def test_target_watermark_prevents_older_resurrection(tmp_path: Path) -> None:
    """Rescanning an older target after the newest succeeds must not resume it."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    scan_count = 0
    delivered_targets: list[str] = []
    first_delivery = asyncio.Event()
    second_scan = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_count
        scan_count += 1
        if scan_count == 2:
            second_scan.set()
        return RoomRecoveryResult(
            interrupted_threads=(
                _target("$new", timestamp_ms=20),
                _target("$old", timestamp_ms=10),
            ),
        )

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        first_delivery.set()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(first_delivery.wait(), timeout=1.0)
    coordinator.enqueue_replacement_rooms(owner.user_id, {"!code:example.org"})
    try:
        await asyncio.wait_for(second_scan.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    assert delivered_targets == ["$new"]


@pytest.mark.asyncio
async def test_newer_same_room_scan_does_not_send_second_resume_while_old_target_is_active(
    tmp_path: Path,
) -> None:
    """One owner-room recovery lease must not deliver two target versions for one thread."""
    owner = _owner()
    router = _owner(
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    old_target = _target("$old", timestamp_ms=10)
    new_target = _target("$new", timestamp_ms=20)
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()
    second_scan_finished = asyncio.Event()
    scan_count = 0
    delivered_targets: list[str] = []

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_count
        scan_count += 1
        if scan_count == 1:
            return RoomRecoveryResult(interrupted_threads=(old_target,))
        second_scan_finished.set()
        return RoomRecoveryResult(interrupted_threads=(new_target,))

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        if target.target_event_id == old_target.target_event_id:
            first_delivery_started.set()
            await release_first_delivery.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(first_delivery_started.wait(), timeout=1.0)
    coordinator.enqueue_replacement_rooms(owner.user_id, {old_target.room_id})
    release_first_delivery.set()
    try:
        await asyncio.wait_for(second_scan_finished.wait(), timeout=1.0)
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        release_first_delivery.set()
        await coordinator.stop()

    assert delivered_targets == ["$old"]


@pytest.mark.asyncio
async def test_discard_owner_clears_target_watermarks(tmp_path: Path) -> None:
    """Removing an owner must not suppress the same target after a later re-add."""
    owner = _owner()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=dict,
        operations=_operations(recover_room=recover_room),
    )
    target = _target("$target", timestamp_ms=10)

    key = (owner.user_id, target.room_id, target.thread_id)
    coordinator._advance_watermark(
        owner,
        TargetSettlement(
            target=target,
            closed=False,
        ),
    )
    coordinator.discard_owner(owner.user_id)

    assert key not in coordinator._target_watermarks


@pytest.mark.asyncio
async def test_discard_owner_fences_inflight_startup_scan_settlement(tmp_path: Path) -> None:
    """A discarded generation must not restore a completed-scan fence after removal."""
    room_id = "!code:example.org"
    old_generation = object()
    new_generation = object()
    old_owner = _owner(
        generation=old_generation,
        rooms=frozenset({room_id}),
    )
    new_owner = _owner(
        generation=new_generation,
        rooms=frozenset({room_id}),
    )
    owners = {old_owner.user_id: old_owner}
    old_scan_started = asyncio.Event()
    release_old_scan = asyncio.Event()
    new_scan_started = asyncio.Event()

    async def recover_room(
        owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        if owner.generation is old_generation:
            old_scan_started.set()
            await release_old_scan.wait()
        else:
            new_scan_started.set()
        return RoomRecoveryResult()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(old_scan_started.wait(), timeout=1.0)

    owners.clear()
    coordinator.discard_owner(old_owner.user_id)
    release_old_scan.set()
    await _wait_until(lambda: not coordinator._room_jobs and not coordinator._active_attempts)

    owners[new_owner.user_id] = new_owner
    coordinator.owner_ready(new_owner.user_id)
    try:
        await asyncio.wait_for(new_scan_started.wait(), timeout=1.0)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_discard_owner_fences_inflight_replacement_intent_settlement(
    tmp_path: Path,
) -> None:
    """A stale generation must not erase a replacement intent enrolled after removal."""
    room_id = "!code:example.org"
    old_generation = object()
    new_generation = object()
    old_owner = _owner(generation=old_generation, rooms=frozenset())
    new_owner = _owner(generation=new_generation, rooms=frozenset())
    owners = {old_owner.user_id: old_owner}
    old_scan_started = asyncio.Event()
    release_old_scan = asyncio.Event()
    sixth_new_scan = asyncio.Event()
    reenrolled_scan = asyncio.Event()
    new_scan_attempts = 0

    async def recover_room(
        owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal new_scan_attempts
        assert request.terminal_interrupted_only
        if owner.generation is old_generation:
            old_scan_started.set()
            await release_old_scan.wait()
            return RoomRecoveryResult()
        new_scan_attempts += 1
        if new_scan_attempts == 6:
            sixth_new_scan.set()
        if new_scan_attempts == 7:
            reenrolled_scan.set()
        return RoomRecoveryResult(retry_owner_user_ids=frozenset({owner.user_id}))

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    coordinator.enqueue_replacement_rooms(old_owner.user_id, {room_id})
    await asyncio.wait_for(old_scan_started.wait(), timeout=1.0)

    owners.clear()
    coordinator.discard_owner(old_owner.user_id)
    owners[new_owner.user_id] = new_owner
    coordinator.enqueue_replacement_rooms(new_owner.user_id, {room_id})
    release_old_scan.set()
    await asyncio.wait_for(sixth_new_scan.wait(), timeout=1.0)
    await _wait_until(
        lambda: not coordinator._active_attempts and next(iter(coordinator._room_jobs.values())).due_at is None,
    )

    coordinator.owner_ready(new_owner.user_id)
    try:
        await asyncio.wait_for(reenrolled_scan.wait(), timeout=1.0)
    finally:
        release_old_scan.set()
        await coordinator.stop()

    assert new_scan_attempts == 7


@pytest.mark.asyncio
async def test_unready_coowner_does_not_preserve_ready_owner_matrix_budget(
    tmp_path: Path,
) -> None:
    """One unready owner must not stop another owner from exhausting retries."""
    ready = _owner(user_id="@code:example.org")
    waiting = _owner(
        user_id="@other:example.org",
        ready=False,
    )
    owners = {ready.user_id: ready, waiting.user_id: waiting}
    delivery_attempts = 0
    sixth_delivery = asyncio.Event()
    seventh_delivery = asyncio.Event()

    async def recover_room(
        owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        if owner.user_id == ready.user_id:
            return RoomRecoveryResult(interrupted_threads=(_target("$target", timestamp_ms=10),))
        pytest.fail("unready owner must not scan")

    async def deliver(
        owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_attempts
        assert owner.user_id == ready.user_id
        delivery_attempts += 1
        if delivery_attempts == 6:
            sixth_delivery.set()
        if delivery_attempts == 7:
            seventh_delivery.set()
        return False

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(sixth_delivery.wait(), timeout=1.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not seventh_delivery.is_set()
    finally:
        await coordinator.stop()

    assert delivery_attempts == 6


@pytest.mark.asyncio
async def test_ready_owners_share_one_room_scan_and_settle_independently(
    tmp_path: Path,
) -> None:
    """Same-request owners share discovery but retain exact delivery ownership."""
    code = _owner(user_id="@code:example.org")
    other = _owner(user_id="@other:example.org")
    owners = {code.user_id: code, other.user_id: other}
    scan_count = 0
    delivered: list[tuple[str, str]] = []

    async def recover_batch(
        scan_owners: tuple[RecoveryOwner, ...],
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_count
        scan_count += 1
        assert {owner.user_id for owner in scan_owners} == set(owners)
        return RoomRecoveryResult(
            interrupted_threads=(
                _target(
                    "$code",
                    timestamp_ms=10,
                    agent_name="stale-code-name",
                    owner_user_id=code.user_id,
                ),
                _target(
                    "$other",
                    timestamp_ms=10,
                    thread_id="$other",
                    agent_name="other",
                    owner_user_id=other.user_id,
                ),
            ),
        )

    async def deliver(
        owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> RestartDeliveryOutcome:
        delivered.append((owner.user_id, target.target_event_id))
        return RestartDeliveryOutcome.DELIVERED

    async def drain_membership_snapshots() -> None:
        return None

    operations = RestartRecoveryOperations(
        joined_rooms=AsyncMock(side_effect=lambda owner: list(owner.desired_room_ids)),
        membership_refresh_delay_seconds=0.0,
        recover_room=recover_batch,
        target_freshness=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
        deliver_target=deliver,
        discard_owner=lambda _owner_user_id: None,
        drain_membership_snapshots=drain_membership_snapshots,
    )
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=operations,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(lambda: not coordinator._room_jobs and not coordinator._active_attempts)
    finally:
        await coordinator.stop()

    assert scan_count == 1
    assert set(delivered) == {
        (code.user_id, "$code"),
        (other.user_id, "$other"),
    }


@pytest.mark.asyncio
async def test_target_exception_retries_only_failed_target_and_continues_shared_lease(
    tmp_path: Path,
) -> None:
    """One target exception must not skip later targets or a healthy co-owner."""
    failed_owner = _owner(user_id="@a-failed:example.org")
    healthy_owner = _owner(user_id="@z-healthy:example.org")
    owners = {failed_owner.user_id: failed_owner, healthy_owner.user_id: healthy_owner}
    failed_target = _target(
        "$failed-target",
        timestamp_ms=10,
        thread_id="$a-failed-thread",
        agent_name="failed",
        owner_user_id=failed_owner.user_id,
    )
    later_target = _target(
        "$later-target",
        timestamp_ms=20,
        thread_id="$b-later-thread",
        agent_name="failed",
        owner_user_id=failed_owner.user_id,
    )
    healthy_target = _target(
        "$healthy-target",
        timestamp_ms=30,
        thread_id="$healthy-thread",
        agent_name="healthy",
        owner_user_id=healthy_owner.user_id,
    )
    delivered: list[str] = []

    async def recover_batch(
        _owners: tuple[RecoveryOwner, ...],
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(
            interrupted_threads=(failed_target, later_target, healthy_target),
        )

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> RestartDeliveryOutcome:
        delivered.append(target.target_event_id)
        if target is failed_target:
            msg = "target delivery failed"
            raise RuntimeError(msg)
        return RestartDeliveryOutcome.DELIVERED

    async def drain_membership_snapshots() -> None:
        return None

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=RestartRecoveryOperations(
            joined_rooms=AsyncMock(side_effect=lambda owner: list(owner.desired_room_ids)),
            membership_refresh_delay_seconds=0.0,
            recover_room=recover_batch,
            target_freshness=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
            deliver_target=deliver,
            discard_owner=lambda _owner_user_id: None,
            drain_membership_snapshots=drain_membership_snapshots,
        ),
        retry_delay=lambda _attempt: 10.0,
    )
    request = RoomRecoveryRequest("!code:example.org", 123, False)
    failed_work = RoomWork(request, failed_owner.user_id, failed_owner.generation, due_at=None)
    healthy_work = RoomWork(request, healthy_owner.user_id, healthy_owner.generation, due_at=None)
    coordinator._room_jobs = {
        failed_work.key: failed_work,
        healthy_work.key: healthy_work,
    }

    with patch("mindroom.restart_recovery.logger.warning") as warning:
        outcomes = await coordinator._process_room(
            RoomLease(request, (failed_work, healthy_work)),
        )
        for outcome in outcomes:
            coordinator._settle_owner_outcome(outcome)

    assert delivered == [
        failed_target.target_event_id,
        later_target.target_event_id,
        healthy_target.target_event_id,
    ]
    assert coordinator._room_jobs[failed_work.key].targets == (failed_target,)
    assert coordinator._room_jobs[failed_work.key].matrix_attempt == 1
    assert healthy_work.key not in coordinator._room_jobs
    warning.assert_called_once_with(
        "Restart recovery target failed",
        owner_user_id=failed_owner.user_id,
        room_id=failed_target.room_id,
        thread_id=failed_target.thread_id,
        target_event_id=failed_target.target_event_id,
        exc_info=True,
    )


@pytest.mark.asyncio
async def test_exhausted_target_is_parked_and_owner_ready_grants_fresh_budget(
    tmp_path: Path,
) -> None:
    """Exhaustion retains its exact target when bounded rediscovery no longer finds it."""
    owner = _owner()
    owners = {owner.user_id: owner}
    target = _target("$target", timestamp_ms=10)
    scan_attempts = 0
    delivery_attempts = 0
    seventh_delivery = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal scan_attempts
        scan_attempts += 1
        return RoomRecoveryResult(interrupted_threads=(target,) if scan_attempts == 1 else ())

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_attempts
        delivery_attempts += 1
        if delivery_attempts == 7:
            seventh_delivery.set()
            return True
        return False

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
        retry_delay=lambda _attempt: 0.0,
    )
    coordinator.start(startup_cutoff_ms=123)
    await _wait_until(
        lambda: delivery_attempts >= 6 and not coordinator._active_attempts,
    )
    parked = tuple(coordinator._room_jobs.values())
    assert len(parked) == 1
    assert parked[0].due_at is None
    assert parked[0].targets == (target,)

    coordinator.owner_ready(owner.user_id)
    try:
        await asyncio.wait_for(seventh_delivery.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivery_attempts == 7
    assert scan_attempts == 7


@pytest.mark.asyncio
async def test_unavailable_owner_uses_bounded_readiness_probes_then_parks(
    tmp_path: Path,
) -> None:
    """Readiness polling has its own bounded counter and production sequence."""
    owner = _owner(ready=False)
    owners = {owner.user_id: owner}
    delays: list[int] = []

    def retry_delay(attempt: int) -> float:
        delays.append(attempt)
        return 0.0

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=AsyncMock()),
        retry_delay=retry_delay,
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await _wait_until(
            lambda: bool(coordinator._room_jobs) and next(iter(coordinator._room_jobs.values())).due_at is None,
        )
    finally:
        await coordinator.stop()

    assert delays == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_disabled_auto_resume_discards_paused_replacement_room_on_resume(
    tmp_path: Path,
) -> None:
    """Resume must discard a retained handoff when the published policy stays disabled."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}
    config = _config(tmp_path)
    config.defaults.auto_resume_after_restart = False
    recover_room = AsyncMock(return_value=RoomRecoveryResult())
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: config,
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    await coordinator.pause()
    coordinator.enqueue_replacement_rooms(owner.user_id, {"!code:example.org"})
    coordinator.resume()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    recover_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_to_enabled_resume_retains_paused_replacement_room(
    tmp_path: Path,
) -> None:
    """A paused handoff must survive until resume reads the newly published policy."""
    owner = _owner(rooms=frozenset())
    owners = {owner.user_id: owner}
    config = _config(tmp_path)
    config.defaults.auto_resume_after_restart = False
    recover_room = AsyncMock(return_value=RoomRecoveryResult())
    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: config,
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    await coordinator.pause()
    coordinator.enqueue_replacement_rooms(owner.user_id, {"!interrupted:example.org"})
    config.defaults.auto_resume_after_restart = True
    coordinator.resume()
    try:
        await _wait_until(lambda: recover_room.await_count == 1)
    finally:
        await coordinator.stop()

    request = recover_room.await_args.args[1]
    assert request.room_id == "!interrupted:example.org"
    assert request.terminal_interrupted_only is True


@pytest.mark.asyncio
async def test_pause_does_not_start_next_owner_delivery_in_shared_lease(
    tmp_path: Path,
) -> None:
    """Pause drains one admitted owner send without starting a co-owner send."""
    code = _owner(user_id="@code:example.org")
    other = _owner(user_id="@other:example.org")
    owners = {code.user_id: code, other.user_id: other}
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    other_delivered = asyncio.Event()
    delivered: list[str] = []

    async def recover_batch(
        _owners: tuple[RecoveryOwner, ...],
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(
            interrupted_threads=(
                _target("$code", timestamp_ms=10, agent_name="code", owner_user_id=code.user_id),
                _target(
                    "$other",
                    timestamp_ms=10,
                    thread_id="$other",
                    agent_name="other",
                    owner_user_id=other.user_id,
                ),
            ),
        )

    async def deliver(
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> RestartDeliveryOutcome:
        delivered.append(target.target_event_id)
        if target.target_event_id == "$code":
            delivery_started.set()
            await release_delivery.wait()
        else:
            other_delivered.set()
        return RestartDeliveryOutcome.DELIVERED

    async def drain_membership_snapshots() -> None:
        return None

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=RestartRecoveryOperations(
            joined_rooms=AsyncMock(side_effect=lambda owner: list(owner.desired_room_ids)),
            membership_refresh_delay_seconds=0.0,
            recover_room=recover_batch,
            target_freshness=AsyncMock(return_value=InterruptedTargetFreshness.CURRENT),
            deliver_target=deliver,
            discard_owner=lambda _owner_user_id: None,
            drain_membership_snapshots=drain_membership_snapshots,
        ),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)
    pause_task = asyncio.create_task(coordinator.pause())
    await asyncio.sleep(0)
    release_delivery.set()
    await asyncio.wait_for(pause_task, timeout=1.0)

    assert delivered == ["$code"]
    assert not other_delivered.is_set()
    assert all(work.matrix_attempt == 0 for work in coordinator._room_jobs.values())

    coordinator.resume()
    try:
        await asyncio.wait_for(other_delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivered == ["$code", "$other"]


@pytest.mark.asyncio
async def test_resume_before_start_does_not_snapshot_owners(
    tmp_path: Path,
) -> None:
    """A pre-start config resume must not materialize recovery owners."""
    current_owners = MagicMock(return_value={})

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=current_owners,
        operations=_operations(recover_room=AsyncMock()),
    )
    coordinator._stopped = False
    coordinator.resume()
    try:
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    current_owners.assert_not_called()


@pytest.mark.asyncio
async def test_begin_shutdown_fences_delivery_released_from_inflight_scan(
    tmp_path: Path,
) -> None:
    """A scan finishing after shutdown starts must not admit a new relay."""
    owner = _owner()
    owners = {owner.user_id: owner}
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    deliver = AsyncMock(return_value=True)

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        scan_started.set()
        await release_scan.wait()
        return RoomRecoveryResult(interrupted_threads=(_target("$after-shutdown", timestamp_ms=10),))

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(scan_started.wait(), timeout=1.0)

    coordinator.begin_shutdown()
    release_scan.set()
    try:
        await _wait_until(lambda: not coordinator._active_attempts)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_begin_shutdown_preserves_later_drain_of_admitted_delivery(
    tmp_path: Path,
) -> None:
    """The admission fence must not abandon a relay admitted before shutdown."""
    owner = _owner()
    owners = {owner.user_id: owner}
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        return RoomRecoveryResult(interrupted_threads=(_target("$admitted", timestamp_ms=10),))

    async def deliver(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivery_started.set()
        await release_delivery.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(delivery_started.wait(), timeout=1.0)

    coordinator.begin_shutdown()
    stop_task = asyncio.create_task(coordinator.stop())
    try:
        await asyncio.sleep(0)
        assert not stop_task.done()
        release_delivery.set()
        await asyncio.wait_for(stop_task, timeout=1.0)
    finally:
        release_delivery.set()
        await asyncio.gather(stop_task, return_exceptions=True)
