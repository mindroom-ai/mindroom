"""Tests for serialized restart-recovery coordination."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.invited_rooms_store import invited_rooms_path, save_invited_rooms
from mindroom.matrix.stale_stream_cleanup import InterruptedThread
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.restart_recovery import (
    RecoveryOwner,
    RestartRecoveryCoordinator,
    build_matrix_restart_recovery_operations,
)
from mindroom.restart_recovery import (
    _restart_recovery_retry_delay as restart_recovery_retry_delay,
)
from mindroom.restart_recovery import (
    _RestartRecoveryOperations as RestartRecoveryOperations,
)
from mindroom.restart_recovery import (
    _RestartTargetFreshness as RestartTargetFreshness,
)
from mindroom.restart_recovery import (
    _RoomRecoveryRequest as RoomRecoveryRequest,
)
from mindroom.restart_recovery import (
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
) -> InterruptedThread:
    return InterruptedThread(
        room_id=room_id,
        thread_id=thread_id,
        target_event_id=target_event_id,
        partial_text="Partial",
        agent_name="code",
        original_sender_id="@alice:example.org",
        timestamp_ms=timestamp_ms,
    )


def _operations(
    *,
    recover_room: _RecoverRoom,
    freshness: _TargetFreshness | None = None,
    deliver: _DeliverTarget | None = None,
) -> RestartRecoveryOperations:
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
        recover_room=recover_room,
        target_freshness=freshness or current,
        deliver_target=deliver or delivered,
    )


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
            "mindroom.restart_recovery.get_joined_rooms",
            new=AsyncMock(return_value=["!other:example.org"]),
        ),
        patch("mindroom.restart_recovery.cleanup_stale_streaming_room", new=AsyncMock()) as cleanup_room,
    ):
        result = await operations.recover_room(
            owner,
            request,
            frozenset({owner.user_id}),
            config,
        )

    assert result == RoomRecoveryResult(retry=True)
    cleanup_room.assert_not_awaited()


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
            "mindroom.restart_recovery.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery.cleanup_stale_streaming_room",
            new=AsyncMock(
                return_value=stale_stream_cleanup_module._StaleStreamCleanupResult(
                    cleaned_count=1,
                    interrupted_threads=(interrupted,),
                ),
            ),
        ) as cleanup_room,
    ):
        result = await operations.recover_room(
            owner,
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
async def test_matrix_room_recovery_propagates_cleanup_retry_requirement(
    tmp_path: Path,
) -> None:
    """Any failed room edit must keep the semantic room job retryable."""
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
        retry_required=True,
    )

    with (
        patch(
            "mindroom.restart_recovery.get_joined_rooms",
            new=AsyncMock(return_value=["!code:example.org"]),
        ),
        patch(
            "mindroom.restart_recovery.cleanup_stale_streaming_room",
            new=AsyncMock(return_value=cleanup_result),
        ),
    ):
        result = await operations.recover_room(
            owner,
            request,
            frozenset({owner.user_id}),
            config,
        )

    assert result == RoomRecoveryResult(
        interrupted_threads=(interrupted,),
        retry=True,
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
        "mindroom.restart_recovery.send_message_result",
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

    with patch(
        "mindroom.restart_recovery.send_message_result",
        new=AsyncMock(side_effect=deliver_once_lost),
    ) as send_message:
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
            "mindroom.restart_recovery.get_joined_rooms",
            new=AsyncMock(side_effect=joined_rooms),
        ) as get_rooms,
        patch(
            "mindroom.restart_recovery.cleanup_stale_streaming_room",
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
                    owner,
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
            owner,
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
            replacement_owner,
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
async def test_stalled_room_does_not_block_other_rooms_and_concurrency_is_bounded(
    tmp_path: Path,
) -> None:
    """One stalled room must leave one bounded slot available for healthy rooms."""
    blocked_room = "!a-blocked:example.org"
    healthy_rooms = {"!b-healthy:example.org", "!c-healthy:example.org"}
    owner = _owner(rooms=frozenset({blocked_room, *healthy_rooms}))
    owners = {owner.user_id: owner}
    blocked_started = asyncio.Event()
    release_blocked = asyncio.Event()
    healthy_finished = asyncio.Event()
    finished_rooms: set[str] = set()
    active = 0
    max_active = 0

    async def recover_room(
        _owner: RecoveryOwner,
        request: RoomRecoveryRequest,
        _owner_user_ids: frozenset[str],
        _config: Config,
    ) -> RoomRecoveryResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            if request.room_id == blocked_room:
                blocked_started.set()
                await release_blocked.wait()
            else:
                finished_rooms.add(request.room_id)
                if finished_rooms == healthy_rooms:
                    healthy_finished.set()
            return RoomRecoveryResult()
        finally:
            active -= 1

    coordinator = RestartRecoveryCoordinator(
        current_config=lambda: _config(tmp_path),
        current_owners=lambda: owners,
        operations=_operations(recover_room=recover_room),
    )
    coordinator.start(startup_cutoff_ms=123)
    await asyncio.wait_for(blocked_started.wait(), timeout=1.0)
    try:
        await asyncio.wait_for(healthy_finished.wait(), timeout=1.0)
    finally:
        release_blocked.set()
        await coordinator.stop()

    assert finished_rooms == healthy_rooms
    assert 1 <= max_active <= 2


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
            return RoomRecoveryResult(retry=True)
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
        assert key in coordinator._settled_target_versions
        coordinator.enqueue_replacement_rooms(owner.user_id, {target.room_id})
        await asyncio.wait_for(second_scan.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        await coordinator.stop()

    assert freshness_attempts == 1


def test_orchestrator_recovery_owner_includes_persisted_accepted_invites(
    tmp_path: Path,
) -> None:
    """Startup recovery scope must include durable ad-hoc accepted rooms."""
    config = _config(tmp_path)
    config.agents["code"].accept_invites = True
    runtime_paths = test_runtime_paths(tmp_path)
    save_invited_rooms(
        invited_rooms_path(runtime_paths.storage_root, "code"),
        {"!invited:example.org"},
    )
    client = MagicMock(spec=nio.AsyncClient)
    bot = MagicMock(spec=AgentBot)
    bot.agent_name = "code"
    bot.agent_user = MagicMock(user_id="@code:example.org")
    bot.client = client
    bot.rooms = ["!configured:example.org"]
    bot.running = True
    bot.first_sync_complete = True
    bot._conversation_cache = MagicMock()
    orchestrator = _MultiAgentOrchestrator(runtime_paths)
    orchestrator.config = config
    orchestrator.agent_bots = {"code": bot}

    owners = orchestrator._restart_recovery_owners()

    assert owners["@code:example.org"].desired_room_ids == frozenset(
        {"!configured:example.org", "!invited:example.org"},
    )
    assert owners["@code:example.org"].generation is bot


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
    """A room result from a replaced bot generation must never publish targets."""
    old_generation = object()
    new_generation = object()
    old_owner = _owner(generation=old_generation)
    new_owner = _owner(generation=new_generation)
    owners = {old_owner.user_id: old_owner}
    old_scan_started = asyncio.Event()
    release_old_scan = asyncio.Event()
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
            await release_old_scan.wait()
            return RoomRecoveryResult(interrupted_threads=(_target("$stale", timestamp_ms=1),))
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
    owners[old_owner.user_id] = new_owner
    release_old_scan.set()
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1.0)
    finally:
        await coordinator.stop()

    assert delivered_targets == ["$current"]


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

    coordinator._enqueue_target(owner.user_id, target)
    coordinator.discard_owner(owner.user_id)
    coordinator._enqueue_target(owner.user_id, target)

    assert tuple(coordinator._target_jobs) == ((owner.user_id, target.room_id, target.thread_id),)
