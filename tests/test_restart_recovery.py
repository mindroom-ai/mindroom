"""Tests for serialized restart-recovery coordination."""

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.stale_stream_cleanup import InterruptedThread
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.restart_recovery import RestartRecoveryCoordinator
from mindroom.restart_recovery import (
    _restart_recovery_retry_delay as restart_recovery_retry_delay,
)
from mindroom.restart_recovery import (
    _RoomAttemptResult as RoomAttemptResult,
)
from mindroom.restart_recovery import (
    _RoomWork as RoomWork,
)
from mindroom.restart_recovery import (
    _TargetSettlement as TargetSettlement,
)
from mindroom.restart_recovery_operations import (
    RecoveryOwner,
    RestartRecoveryOperations,
    RestartTargetFreshness,
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

    type _RecoverRoom = Callable[
        [RecoveryOwner, RoomRecoveryRequest, frozenset[str], Config],
        Awaitable[RoomRecoveryResult],
    ]
    type _TargetFreshness = Callable[
        [RecoveryOwner, InterruptedThread, Config],
        Awaitable[RestartTargetFreshness],
    ]
    type _DeliverTarget = Callable[
        [RecoveryOwner, RecoveryOwner, InterruptedThread, Config],
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
    entity_name: str = "code",
    user_id: str = "@code:example.org",
    generation: object | None = None,
    rooms: frozenset[str] = frozenset({"!code:example.org"}),
    ready: bool = True,
) -> RecoveryOwner:
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = user_id
    return RecoveryOwner(
        entity_name=entity_name,
        user_id=user_id,
        generation=object() if generation is None else generation,
        client=client,
        conversation_cache=cast("ConversationCacheProtocol", MagicMock()),
        desired_room_ids=rooms,
        first_sync_complete=ready,
    )


def _target(
    target_event_id: str,
    *,
    timestamp_ms: int,
    room_id: str = "!code:example.org",
    thread_id: str = "$thread",
    agent_name: str = "code",
    original_sender_id: str | None = "@alice:example.org",
) -> InterruptedThread:
    return InterruptedThread(
        room_id=room_id,
        thread_id=thread_id,
        target_event_id=target_event_id,
        partial_text="Partial",
        agent_name=agent_name,
        original_sender_id=original_sender_id,
        timestamp_ms=timestamp_ms,
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
            retry_owner_user_ids=frozenset(
                owner_user_id for result in results for owner_user_id in result.retry_owner_user_ids
            ),
        )

    async def close() -> None:
        return None

    async def current(
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> RestartTargetFreshness:
        return RestartTargetFreshness.CURRENT

    async def delivered(
        _router: RecoveryOwner,
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        return True

    return RestartRecoveryOperations(
        recover_room=recover_batch,
        target_freshness=freshness or current,
        deliver_target=deliver or delivered,
        discard_owner=lambda _owner_user_id: None,
        close=close,
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
async def test_matrix_room_recovery_retries_until_exact_owner_is_joined(tmp_path: Path) -> None:
    """A desired room hidden from the owner client must stay in the room ledger."""
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
        retry_owner_user_ids=frozenset({owner.user_id}),
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
        entity_name=ROUTER_AGENT_NAME,
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
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
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
        retry_owner_user_ids=frozenset({router.user_id}),
    )
    cleanup_room.assert_awaited_once()
    assert set(cleanup_room.await_args.kwargs["actors"]) == {owner.user_id}


@pytest.mark.asyncio
async def test_matrix_room_recovery_scans_only_exact_owner_messages(tmp_path: Path) -> None:
    """One semantic room job must edit only messages authored by its exact owner."""
    owner = _owner()
    other_owner = _owner(entity_name="other", user_id="@other:example.org")
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
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
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
    assert cleanup_room.await_args.args == (owner.client,)
    assert cleanup_room.await_args.kwargs["actors"] == {
        owner.user_id: cleanup_room.await_args.kwargs["actors"][owner.user_id],
    }
    assert cleanup_room.await_args.kwargs["actors"][owner.user_id].client is owner.client
    assert cleanup_room.await_args.kwargs["bot_user_ids"] == {owner.user_id, other_owner.user_id}


@pytest.mark.asyncio
async def test_shared_room_startup_scans_once_with_all_joined_owners(tmp_path: Path) -> None:
    """Co-resident bots must share one uncached room-history scan."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
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
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
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

    cleanup_room.assert_awaited_once()
    assert set(cleanup_room.await_args.kwargs["actors"]) == set(owners)


@pytest.mark.asyncio
async def test_shared_room_scans_ready_owner_and_retries_only_unready_owner(
    tmp_path: Path,
) -> None:
    """One unready owner must not block a ready co-owner's room scan."""
    room_id = "!code:example.org"
    owner = _owner(rooms=frozenset({room_id}))
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
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
    result = await coordinator._process_room(
        RoomWork(
            room_id=room_id,
            startup_cutoff_ms=123,
            terminal_interrupted_only=False,
            owner_user_ids=frozenset(owners),
        ),
    )

    assert scanned_owner_user_ids == [owner.user_id]
    assert result.retry_owner_user_ids == frozenset({router.user_id})


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
    cleanup_result = stale_stream_cleanup_module._StaleStreamCleanupResult(
        cleaned_count=1,
        interrupted_threads=(interrupted,),
        room_retry_required=True,
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
        entity_name="healthy",
        user_id="@healthy:example.org",
    )
    failed_owner = _owner(
        entity_name="failed",
        user_id="@failed:example.org",
    )
    interrupted = _target(
        "$healthy-target",
        timestamp_ms=10,
        agent_name=healthy_owner.entity_name,
    )
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery_operations.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
                    cleaned_count=1,
                    interrupted_threads=(interrupted,),
                    retry_bot_user_ids=frozenset({failed_owner.user_id}),
                ),
            ),
        ),
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


@pytest.mark.asyncio
async def test_matrix_target_delivery_uses_router_and_mentions_exact_owner(tmp_path: Path) -> None:
    """Resume delivery must be router-authored while targeting the exact owner account."""
    owner = _owner()
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    target = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with patch(
        "mindroom.restart_recovery_operations.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as send_message:
        delivered = await operations.deliver_target(router, owner, target, _config(tmp_path))

    assert delivered is True
    assert send_message.await_args.args[:2] == (router.client, target.room_id)
    content = send_message.await_args.args[2]
    assert (
        content["body"]
        == "@Code [System: Previous response was interrupted by service restart. Please continue where you left off.]"
    )
    assert content["m.mentions"] == {"user_ids": [owner.user_id]}
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == target.target_event_id
    router.conversation_cache.notify_outbound_message.assert_called_once_with(
        target.room_id,
        "$resume",
        content,
    )


@pytest.mark.asyncio
async def test_matrix_target_delivery_by_exact_owner_omits_self_mention(tmp_path: Path) -> None:
    """An owner-authored resume must not mention its own Matrix account."""
    owner = _owner()
    target = _target("$target", timestamp_ms=10)
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with patch(
        "mindroom.restart_recovery_operations.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as send_message:
        delivered = await operations.deliver_target(owner, owner, target, _config(tmp_path))

    assert delivered is True
    content = send_message.await_args.args[2]
    assert content["body"] == (
        "[System: Previous response was interrupted by service restart. Please continue where you left off.]"
    )
    assert "m.mentions" not in content


@pytest.mark.asyncio
async def test_matrix_target_delivery_paces_router_sends(tmp_path: Path) -> None:
    """Concurrent-room recovery must not burst visible relays through one router."""
    owner = _owner()
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
        user_id="@router:example.org",
        rooms=frozenset(),
    )
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
                    stale_stream_cleanup_module.InterruptedTargetFreshness.NEWER_HUMAN,
                ),
            ),
        ),
    ):
        assert await operations.deliver_target(
            router,
            owner,
            _target("$first", timestamp_ms=10, thread_id="$thread-a"),
            _config(tmp_path),
        )
        assert await operations.deliver_target(
            router,
            owner,
            _target("$second", timestamp_ms=20, thread_id="$thread-b"),
            _config(tmp_path),
        )
        assert not await operations.deliver_target(
            router,
            owner,
            _target("$third", timestamp_ms=30, thread_id="$thread-c"),
            _config(tmp_path),
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
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    router.conversation_cache.notify_outbound_message.side_effect = RuntimeError("cache bug")
    operations = build_matrix_restart_recovery_operations(test_runtime_paths(tmp_path))

    with (
        patch(
            "mindroom.restart_recovery_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ),
        pytest.raises(RuntimeError, match="cache bug"),
    ):
        await operations.deliver_target(
            router,
            owner,
            _target("$target", timestamp_ms=10),
            _config(tmp_path),
        )


@pytest.mark.asyncio
async def test_matrix_target_delivery_reuses_stable_transaction_id_after_lost_response(
    tmp_path: Path,
) -> None:
    """An accepted send with a lost response must retry with the same Matrix transaction ID."""
    owner = _owner()
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
        user_id="@router:example.org",
        rooms=frozenset(),
    )
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
        first = await operations.deliver_target(router, owner, target, _config(tmp_path))
        second = await operations.deliver_target(router, owner, target, _config(tmp_path))

    assert first is False
    assert second is True
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
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
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
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
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
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
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
            await asyncio.sleep(0)
            assert recovery_task.cancelled()
        finally:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)

    del recovery_task
    del owner
    del generation
    gc.collect()

    assert generation_ref() is None


@pytest.mark.asyncio
async def test_eight_unrelated_rooms_run_concurrently_with_bounded_fanout(
    tmp_path: Path,
) -> None:
    """Recovery must retain the established eight-room startup concurrency."""
    room_ids = frozenset(f"!room-{index}:example.org" for index in range(9))
    owner = _owner(rooms=room_ids)
    owners = {owner.user_id: owner}
    eight_started = asyncio.Event()
    release_rooms = asyncio.Event()
    all_finished = asyncio.Event()
    finished = 0
    active = 0
    max_active = 0

    async def recover_room(
        _owner: RecoveryOwner,
        _request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal active, finished, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 8:
            eight_started.set()
        try:
            await release_rooms.wait()
            return RoomRecoveryResult()
        finally:
            active -= 1
            finished += 1
            if finished == len(room_ids):
                all_finished.set()

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(eight_started.wait(), timeout=0.2)
        release_rooms.set()
        await asyncio.wait_for(all_finished.wait(), timeout=1.0)
    finally:
        release_rooms.set()
        await coordinator.stop()

    assert max_active == 8


@pytest.mark.asyncio
async def test_blocked_deliveries_do_not_consume_room_scan_capacity(tmp_path: Path) -> None:
    """A ninth room scan must start while eight earlier deliveries remain blocked."""
    room_ids = frozenset(f"!room-{index}:example.org" for index in range(9))
    owner = _owner(rooms=room_ids)
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
        user_id="@router:example.org",
        rooms=frozenset(),
    )
    owners = {owner.user_id: owner, router.user_id: router}
    scanned_room_ids: set[str] = set()
    eight_deliveries_started = asyncio.Event()
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
        _router: RecoveryOwner,
        _owner: RecoveryOwner,
        _target: InterruptedThread,
        _config: Config,
    ) -> bool:
        nonlocal delivery_count
        delivery_count += 1
        if delivery_count == 8:
            eight_deliveries_started.set()
        await release_deliveries.wait()
        return True

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room, deliver=deliver),
    )
    coordinator.start(startup_cutoff_ms=123)
    try:
        await asyncio.wait_for(eight_deliveries_started.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert scanned_room_ids == set(room_ids)
    finally:
        release_deliveries.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_shared_owner_wait_does_not_fill_both_slots_ahead_of_another_owner(
    tmp_path: Path,
) -> None:
    """One owner's blocked room work must leave a slot for another owner."""
    blocked_owner = _owner(
        user_id="@blocked:example.org",
        rooms=frozenset({"!blocked-a:example.org", "!blocked-b:example.org"}),
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
        await asyncio.wait_for(healthy_finished.wait(), timeout=0.2)
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


def test_oldest_due_room_wins_before_owner_diversity(tmp_path: Path) -> None:
    """Continuous disjoint arrivals must not starve older overlapping work."""

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
    active = RoomWork(
        room_id="!active:example.org",
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
        owner_user_ids=frozenset({busy_owner}),
    )
    oldest = RoomWork(
        room_id="!oldest:example.org",
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
        owner_user_ids=frozenset({busy_owner}),
        due_at=1.0,
    )
    newer = RoomWork(
        room_id="!newer:example.org",
        startup_cutoff_ms=123,
        terminal_interrupted_only=False,
        owner_user_ids=frozenset({"@idle:example.org"}),
        due_at=2.0,
    )
    coordinator._active_attempts[cast("asyncio.Task[object]", MagicMock())] = active
    coordinator._room_jobs = {oldest.key: oldest, newer.key: newer}

    assert coordinator._next_due_work(3.0) is oldest


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
    finally:
        await coordinator.stop()

    assert set(processed_requests) == {startup_request, replacement_request}


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
    coordinator._startup_cutoff_ms = 123
    coordinator.owner_ready(owner.user_id)
    coordinator._paused = False
    work = next(iter(coordinator._room_jobs.values()))
    coordinator._room_jobs.clear()

    async def unavailable_result() -> RoomAttemptResult:
        return RoomAttemptResult(
            owners=dict(owners),
            retry_owner_user_ids=frozenset({owner.user_id}),
            retry_targets=(),
            settlements=(),
        )

    attempt = asyncio.create_task(unavailable_result())
    await attempt
    coordinator._active_attempts[attempt] = work
    coordinator._settle_finished_attempts()

    retained = next(iter(coordinator._room_jobs.values()))
    assert retry_attempts == [1]
    assert retained.attempt == 1
    assert retained.due_at > asyncio.get_running_loop().time()
    assert owner.user_id not in coordinator._ready_generations

    owners[owner.user_id] = _owner(generation=generation, ready=True)
    coordinator.owner_ready(owner.user_id)
    ready_work = next(iter(coordinator._room_jobs.values()))
    assert ready_work.due_at <= asyncio.get_running_loop().time()

    result = await coordinator._process_room(ready_work)

    assert not result.retry_owner_user_ids
    recover_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_requester_retry_does_not_fence_same_target_after_resolution(
    tmp_path: Path,
) -> None:
    """An indeterminate requester must remain unsettled until its retry resolves."""
    owner = _owner()
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
        entity_name=ROUTER_AGENT_NAME,
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
    ) -> RestartTargetFreshness:
        nonlocal freshness_attempts
        freshness_attempts += 1
        if freshness_attempts == 1:
            return RestartTargetFreshness.RETRY
        return RestartTargetFreshness.CURRENT

    async def deliver(
        _router: RecoveryOwner,
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
async def test_room_lease_snapshots_current_owners_once(tmp_path: Path) -> None:
    """One room lease must not rebuild durable owner scope per target."""
    owner = _owner()
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
    coordinator.start(startup_cutoff_ms=123)
    owner_snapshot_calls = 0
    try:
        await asyncio.wait_for(all_delivered.wait(), timeout=1.0)
        await _wait_until(
            lambda: not coordinator._room_jobs and all(task.done() for task in coordinator._active_attempts),
        )
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
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
        entity_name=ROUTER_AGENT_NAME,
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
    ) -> RestartTargetFreshness:
        nonlocal second_freshness_attempts
        if target is second:
            second_freshness_attempts += 1
            if second_freshness_attempts == 1:
                second_freshness_started.set()
                await asyncio.Event().wait()
        return RestartTargetFreshness.CURRENT

    async def deliver(
        _router: RecoveryOwner,
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
        target.target.target_event_id for work in coordinator._room_jobs.values() for target in work.targets
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
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
    ) -> RestartTargetFreshness:
        nonlocal freshness_attempts
        freshness_attempts += 1
        freshness_checked.set()
        return RestartTargetFreshness.UNRECOVERABLE

    async def deliver(
        _router: RecoveryOwner,
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
    bot.agent_user = MagicMock(user_id="@code:example.org")
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

    with patch(
        "mindroom.restart_recovery_operations.load_invited_rooms",
        new=MagicMock(),
        create=True,
    ) as load_invited_rooms:
        owners = orchestrator._restart_recovery_owners()
        refreshed_owners = orchestrator._restart_recovery_owners()

    assert owners["@code:example.org"].desired_room_ids == frozenset(
        {"!configured:example.org", "!invited:example.org"},
    )
    assert refreshed_owners["@code:example.org"].desired_room_ids == owners["@code:example.org"].desired_room_ids
    assert owners["@code:example.org"].generation is bot
    load_invited_rooms.assert_not_called()


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
async def test_orchestrator_cancelled_pause_resumes_coordinator(tmp_path: Path) -> None:
    """A cancelled reload must not leave retained recovery work permanently paused."""
    orchestrator = _MultiAgentOrchestrator(test_runtime_paths(tmp_path))
    orchestrator._restart_recovery.pause = AsyncMock(side_effect=asyncio.CancelledError)
    orchestrator._restart_recovery.resume = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._pause_restart_recovery()

    orchestrator._restart_recovery.resume.assert_called_once_with()


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
        _router: RecoveryOwner,
        _owner: RecoveryOwner,
        target: InterruptedThread,
        _config: Config,
    ) -> bool:
        delivered_targets.append(target.target_event_id)
        delivered.set()
        return True

    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
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
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
async def test_target_watermark_prevents_older_resurrection(tmp_path: Path) -> None:
    """Rescanning an older target after the newest succeeds must not resume it."""
    owner = _owner()
    router = _owner(
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
        entity_name=ROUTER_AGENT_NAME,
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
        _router: RecoveryOwner,
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
            owner_user_id=owner.user_id,
        ),
    )
    coordinator.discard_owner(owner.user_id)

    assert key not in coordinator._target_watermarks
