"""Staged owning-seam contract tests for typed mindroom-nio recovery outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, cast, get_type_hints
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.logging_config import get_logger
from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_continuity import SyncContinuityStore
from tests.sync_continuity_helpers import load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot_runtime_view import BotRuntimeView

_CACHE_GENERATION = "nio-recovery-contract"
_RECOVERED_ROOM = "!recovered:localhost"
_UNRECOVERED_ROOM = "!unrecovered:localhost"


@dataclass
class _EventCache:
    cache_generation: str = _CACHE_GENERATION

    async def initialize(self) -> None:
        """Match the production cache startup contract."""

    async def purge_principal(self) -> None:
        """Match cold-start principal cleanup."""

    def disable(self, _reason: str) -> None:
        """Match the production cache disable contract."""


@dataclass
class _Runtime:
    event_cache: _EventCache


def _trust(tmp_path: Path, *, state: SyncTrustState) -> SyncCacheTrust:
    runtime = _Runtime(event_cache=_EventCache())
    return SyncCacheTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        runtime=cast("BotRuntimeView", runtime),
        logger=get_logger(),
        state=state,
    )


def _sync_response(
    *,
    limited_room_ids: tuple[str, ...],
    recovered_room_ids: frozenset[str],
    unrecovered_room_ids: frozenset[str],
    next_batch: str = "s_after",
    leave_room_ids: tuple[str, ...] = (),
) -> nio.SyncResponse:
    """Build a real response carrying authoritative recovery outcomes."""
    joined_rooms = {
        room_id: nio.RoomInfo(
            timeline=nio.Timeline(events=[], limited=True, prev_batch=f"p_{index}"),
            state=[],
            ephemeral=[],
            account_data=[],
        )
        for index, room_id in enumerate(limited_room_ids)
    }
    return nio.SyncResponse(
        next_batch=next_batch,
        rooms=nio.Rooms(
            invite={},
            join=joined_rooms,
            leave={
                room_id: nio.RoomInfo(
                    timeline=nio.Timeline(events=[], limited=False, prev_batch=None),
                    state=[],
                    ephemeral=[],
                    account_data=[],
                )
                for room_id in leave_room_ids
            },
        ),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=0, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        recovered_room_ids=recovered_room_ids,
        unrecovered_room_ids=unrecovered_room_ids,
    )


def _membership_boundary_response(
    room_id: str,
    *,
    boundary: str,
) -> nio.SyncResponse:
    """Build one real Classic response carrying an authoritative membership reset."""
    invited_rooms: dict[str, nio.InviteInfo] = {}
    joined_rooms: dict[str, nio.RoomInfo] = {}
    if boundary == "invite":
        invited_rooms[room_id] = nio.InviteInfo(invite_state=[])
    else:
        own_join = nio.RoomMemberEvent.from_dict(
            {
                "type": "m.room.member",
                "event_id": "$own-rejoin",
                "sender": "@code:localhost",
                "state_key": "@code:localhost",
                "origin_server_ts": 1,
                "content": {"membership": "join"},
                "unsigned": {"prev_content": {"membership": "leave"}},
            },
        )
        assert isinstance(own_join, nio.RoomMemberEvent)
        joined_rooms[room_id] = nio.RoomInfo(
            timeline=nio.Timeline(events=[own_join], limited=False, prev_batch=None),
            state=[],
            ephemeral=[],
            account_data=[],
        )
    return nio.SyncResponse(
        next_batch=f"s_{boundary}",
        rooms=nio.Rooms(invite=invited_rooms, join=joined_rooms, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=0, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["invite", "nonlimited-own-join"])
async def test_real_nio_membership_reset_rewinds_before_clean_replay(
    tmp_path: Path,
    boundary: str,
) -> None:
    """A reset outcome rewinds once before nio's clean replay may certify."""
    room_id = "!reset:localhost"
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            store_sync_tokens=False,
            backfill_limited_timelines=True,
        ),
    )
    client.next_batch = "s_before_gap"
    gap_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_gap",
    )
    boundary_response = _membership_boundary_response(room_id, boundary=boundary)
    save_sync_token(
        tmp_path,
        "code",
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)
    assert await trust.prepare_startup() == "s_before_gap"

    try:
        with patch.object(client, "_recovery_room_messages", AsyncMock(side_effect=asyncio.TimeoutError)):
            await client.receive_response(gap_response)
            await client.receive_response(boundary_response)
    finally:
        await client.close()

    assert gap_response.unrecovered_room_ids == frozenset({room_id})
    assert boundary_response.unrecovered_room_ids == frozenset({room_id})
    gap_decision = await trust.certify_response(
        next_batch=gap_response.next_batch,
        cache_result=_cache_result(gap_response, limited_room_ids=(room_id,), complete=True),
    )
    boundary_result = _cache_result(
        boundary_response,
        limited_room_ids=(),
        complete=True,
    )
    decision = await trust.certify_response(
        next_batch=boundary_response.next_batch,
        cache_result=boundary_result,
    )

    unresolved = await trust.certify_response(
        next_batch="s_clean",
        cache_result=SyncCacheWriteResult(complete=True),
    )
    clean = await trust.certify_response(
        next_batch="s_clean_replay",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert gap_decision.state is SyncTrustState.UNCERTAIN
    assert gap_decision.reset_client_token is False
    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reset_client_token is False
    assert unresolved.reason == "sync_recovery_unresolved"
    assert unresolved.reset_client_token is True
    assert clean.state is SyncTrustState.CERTIFIED


@pytest.mark.asyncio
async def test_authoritative_departure_replays_after_nio_drops_its_outcome(tmp_path: Path) -> None:
    """A purged leave room must replay once after nio drops its reset outcome."""
    room_id = "!departed:localhost"
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)
    gap_response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset({room_id}),
        next_batch="s_gap",
    )
    gap = await trust.certify_response(
        next_batch=gap_response.next_batch,
        cache_result=_cache_result(gap_response, limited_room_ids=(), complete=True),
    )
    departure_response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset({room_id}),
        next_batch="s_leave",
        leave_room_ids=(room_id,),
    )
    departure_result = _cache_result(
        departure_response,
        limited_room_ids=(),
        complete=True,
    )
    departure = await trust.certify_response(
        next_batch=departure_response.next_batch,
        cache_result=departure_result,
    )
    clean_response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_clean",
    )
    clean = await trust.certify_response(
        next_batch=clean_response.next_batch,
        cache_result=_cache_result(clean_response, limited_room_ids=(), complete=True),
    )
    replay = await trust.certify_response(
        next_batch="s_clean_replay",
        cache_result=SyncCacheWriteResult(complete=True),
    )

    assert gap.reset_client_token is False
    assert departure.state is SyncTrustState.UNCERTAIN
    assert departure.reset_client_token is False
    assert clean.state is SyncTrustState.UNCERTAIN
    assert clean.reason == "sync_recovery_unresolved"
    assert clean.reset_client_token is True
    assert replay.state is SyncTrustState.CERTIFIED


@pytest.mark.asyncio
async def test_real_nio_terminal_gap_retries_from_safe_checkpoint(tmp_path: Path) -> None:
    """An abandoned gap must be replanned after MindRoom rewinds the cursor."""
    room_id = "!forbidden-history:localhost"
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            store_sync_tokens=False,
            backfill_limited_timelines=True,
        ),
    )
    client.next_batch = "s_before_gap"
    gap_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_gap",
    )
    progress_response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_progress",
    )
    replay_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_replay",
    )
    save_sync_token(
        tmp_path,
        "code",
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)
    assert await trust.prepare_startup() == "s_before_gap"

    try:
        with patch.object(
            client,
            "_recovery_room_messages",
            AsyncMock(return_value=nio.RoomMessagesError("denied", room_id=room_id)),
        ):
            await client.receive_response(gap_response)
            await client.receive_response(progress_response)
            client.next_batch = "s_before_gap"
            await client.receive_response(replay_response)
    finally:
        await client.close()

    assert gap_response.unrecovered_room_ids == frozenset({room_id})
    assert progress_response.recovered_room_ids == frozenset()
    assert progress_response.unrecovered_room_ids == frozenset()
    assert replay_response.recovered_room_ids == frozenset()
    assert replay_response.unrecovered_room_ids == frozenset({room_id})
    gap = await trust.certify_response(
        next_batch=gap_response.next_batch,
        cache_result=_cache_result(gap_response, limited_room_ids=(room_id,), complete=True),
    )
    progress = await trust.certify_response(
        next_batch=progress_response.next_batch,
        cache_result=_cache_result(progress_response, limited_room_ids=(), complete=True),
    )
    replay = await trust.certify_response(
        next_batch=replay_response.next_batch,
        cache_result=_cache_result(replay_response, limited_room_ids=(room_id,), complete=True),
    )

    assert gap.state is SyncTrustState.UNCERTAIN
    assert gap.reset_client_token is False
    assert progress.reason == "sync_recovery_unresolved"
    assert progress.reset_client_token is True
    assert replay.state is SyncTrustState.UNCERTAIN
    assert replay.reset_client_token is False
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )


@pytest.mark.asyncio
async def test_real_nio_timeout_can_recover_without_replanning_the_gap(tmp_path: Path) -> None:
    """A transient timeout must leave nio's live cursor free to drain the same gap."""
    room_id = "!transient-history:localhost"
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            store_sync_tokens=False,
            backfill_limited_timelines=True,
        ),
    )
    client.next_batch = "s_before_gap"
    gap_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_gap",
    )
    recovery_response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_recovered",
    )
    recovery_page = nio.RoomMessagesResponse(
        room_id=room_id,
        chunk=[],
        start="p_0",
        end=None,
    )
    save_sync_token(
        tmp_path,
        "code",
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)
    assert await trust.prepare_startup() == "s_before_gap"

    try:
        with patch.object(
            client,
            "_recovery_room_messages",
            AsyncMock(side_effect=[asyncio.TimeoutError, recovery_page]),
        ):
            await client.receive_response(gap_response)
            gap = await trust.certify_response(
                next_batch=gap_response.next_batch,
                cache_result=_cache_result(
                    gap_response,
                    limited_room_ids=(room_id,),
                    complete=True,
                ),
            )
            assert gap.reset_client_token is False
            assert trust.rewind_is_deferred_until_recovery()
            await client.receive_response(recovery_response)
            recovered = await trust.certify_response(
                next_batch=recovery_response.next_batch,
                cache_result=_cache_result(
                    recovery_response,
                    limited_room_ids=(),
                    complete=True,
                ),
            )
    finally:
        await client.close()

    assert gap_response.unrecovered_room_ids == frozenset({room_id})
    assert recovery_response.recovered_room_ids == frozenset({room_id})
    assert recovered.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_recovered",
        cache_generation=_CACHE_GENERATION,
    )


@pytest.mark.asyncio
async def test_persisted_nio_gap_resumes_before_safe_cache_replay(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Restart must drain NIO's exact stored generation before replaying its window."""
    room_id = "!restart-history:localhost"
    user_id = "@code:localhost"
    device_id = "CODEDEVICE"
    nio_store_path = tmp_path / "nio-store"
    nio_store_path.mkdir()
    config = nio.AsyncClientConfig(
        store_sync_tokens=True,
        backfill_limited_timelines=True,
        backfill_persist_recovery=True,
    )

    def load_client() -> nio.AsyncClient:
        client = nio.AsyncClient(
            "https://localhost",
            user_id,
            device_id=device_id,
            store_path=str(nio_store_path),
            config=config,
        )
        client.restore_login(user_id, device_id, "access-token")
        return client

    save_sync_token(
        tmp_path,
        "code",
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    first = load_client()
    first.next_batch = "s_before_gap"
    limited_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_gap",
    )
    with patch.object(
        first,
        "_recovery_room_messages",
        AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        await first.receive_response(limited_response)
    assert limited_response.unrecovered_room_ids == frozenset({room_id})
    await first.close()
    assert first.store is not None
    cast("Any", first.store).database.close()

    restarted = load_client()
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)
    try:
        recovery = cast("Any", restarted)._recovery
        assert restarted.loaded_sync_token == "s_gap"  # noqa: S105
        assert len(recovery.gaps[room_id]) == 1
        startup_token = await trust.prepare_startup(
            transport_resume_token=restarted.loaded_sync_token,
        )
        assert startup_token == "s_gap"  # noqa: S105
        restarted.next_batch = startup_token
        recovery_response = _sync_response(
            limited_room_ids=(),
            recovered_room_ids=frozenset(),
            unrecovered_room_ids=frozenset(),
            next_batch="s_after_recovery",
        )
        recovery_page = nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[],
            start="p_0",
            end=None,
        )
        with patch.object(
            restarted,
            "_recovery_room_messages",
            AsyncMock(return_value=recovery_page),
        ):
            await restarted.receive_response(recovery_response)

        assert recovery_response.recovered_room_ids == frozenset({room_id})
        assert not recovery.gaps
        replay = await trust.certify_response(
            next_batch=recovery_response.next_batch,
            cache_result=_cache_result(
                recovery_response,
                limited_room_ids=(),
                complete=True,
            ),
        )
        assert replay.reason == "sync_cache_replay_required"
        assert replay.reset_client_token is True
        assert trust.retry_token() == "s_before_gap"

        restarted.next_batch = trust.retry_token()
        restarted.loaded_sync_token = trust.retry_token() or ""
        replay_response = _sync_response(
            limited_room_ids=(room_id,),
            recovered_room_ids=frozenset(),
            unrecovered_room_ids=frozenset(),
            next_batch="s_replay",
        )
        with patch.object(
            restarted,
            "_recovery_room_messages",
            AsyncMock(return_value=recovery_page),
        ):
            await restarted.receive_response(replay_response)
        certified = await trust.certify_response(
            next_batch=replay_response.next_batch,
            cache_result=_cache_result(
                replay_response,
                limited_room_ids=(room_id,),
                complete=True,
            ),
        )
    finally:
        await restarted.close()
        cast("Any", restarted.store).database.close()

    assert replay_response.recovered_room_ids == frozenset({room_id})
    assert certified.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_replay",
        cache_generation=_CACHE_GENERATION,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local_error",
    [RuntimeError("cache unavailable"), asyncio.CancelledError("cache cancelled")],
    ids=["failed", "cancelled"],
)
async def test_real_nio_timeout_settles_before_local_cache_replay(
    tmp_path: Path,
    local_error: BaseException,
) -> None:
    """Local replay debt must not duplicate nio's still-pending gap generation."""
    room_id = "!cache-replay-history:localhost"
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            store_sync_tokens=False,
            backfill_limited_timelines=True,
        ),
    )
    client.next_batch = "s_before_gap"
    gap_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_gap",
    )
    recovery_response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_recovered",
    )
    recovery_page = nio.RoomMessagesResponse(
        room_id=room_id,
        chunk=[],
        start="p_0",
        end=None,
    )
    save_sync_token(
        tmp_path,
        "code",
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)
    assert await trust.prepare_startup() == "s_before_gap"

    try:
        with patch.object(
            client,
            "_recovery_room_messages",
            AsyncMock(side_effect=[asyncio.TimeoutError, recovery_page]),
        ):
            await client.receive_response(gap_response)
            failed = await trust.certify_response(
                next_batch=gap_response.next_batch,
                cache_result=_cache_result(
                    gap_response,
                    limited_room_ids=(room_id,),
                    complete=False,
                    errors=(local_error,),
                ),
            )
            assert failed.reset_client_token is False
            assert failed.replay_required_after_recovery is True

            await client.receive_response(recovery_response)
            replay = await trust.certify_response(
                next_batch=recovery_response.next_batch,
                cache_result=_cache_result(
                    recovery_response,
                    limited_room_ids=(),
                    complete=True,
                ),
            )
    finally:
        await client.close()

    assert gap_response.unrecovered_room_ids == frozenset({room_id})
    assert recovery_response.recovered_room_ids == frozenset({room_id})
    assert replay.reason == "sync_cache_replay_required"
    assert replay.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )


def _cache_result(
    response: nio.SyncResponse,
    *,
    limited_room_ids: tuple[str, ...],
    complete: bool,
    errors: tuple[BaseException, ...] = (),
) -> SyncCacheWriteResult:
    """Build the cache result from the exact typed upstream response."""
    return SyncCacheWriteResult.from_sync_response(
        response,
        complete=complete,
        limited_room_ids=limited_room_ids,
        errors=errors,
    )


@pytest.mark.parametrize("response_type", [nio.SyncResponse, nio.SlidingSyncResponse])
def test_nio_sync_responses_publish_exact_typed_recovery_fields(response_type: type[object]) -> None:
    """Both sync transports must expose immutable authoritative room outcomes."""
    response_fields = {item.name: item for item in fields(response_type)}
    type_hints = get_type_hints(response_type)

    for field_name in ("recovered_room_ids", "unrecovered_room_ids"):
        assert field_name in response_fields
        assert type_hints[field_name] == frozenset[str]
        assert response_fields[field_name].default == frozenset()


@pytest.mark.asyncio
async def test_cold_limited_baseline_advances_once_then_real_nio_recovery_certifies(tmp_path: Path) -> None:
    """A tokenless limited baseline must advance once so nio can classify its positioned gap."""
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            store_sync_tokens=False,
            backfill_limited_timelines=True,
        ),
    )
    trust = _trust(tmp_path, state=SyncTrustState.COLD)
    assert await trust.prepare_startup() is None
    responses = (
        _sync_response(
            limited_room_ids=(_RECOVERED_ROOM,),
            recovered_room_ids=frozenset(),
            unrecovered_room_ids=frozenset(),
            next_batch="s_initial",
        ),
        _sync_response(
            limited_room_ids=(_RECOVERED_ROOM,),
            recovered_room_ids=frozenset(),
            unrecovered_room_ids=frozenset(),
            next_batch="s_after",
        ),
    )
    recovery_page = nio.RoomMessagesResponse(
        room_id=_RECOVERED_ROOM,
        chunk=[],
        start="p_initial",
        end=None,
    )
    decisions = []

    try:
        with patch.object(client, "_recovery_room_messages", AsyncMock(return_value=recovery_page)):
            for response in responses:
                await client.receive_response(response)
                result = _cache_result(
                    response,
                    limited_room_ids=(_RECOVERED_ROOM,),
                    complete=True,
                )
                decision = await trust.certify_response(
                    next_batch=response.next_batch,
                    cache_result=result,
                )
                if decision.reset_client_token:
                    client.next_batch = None
                decisions.append(decision)
    finally:
        await client.close()

    assert responses[0].recovered_room_ids == frozenset()
    assert responses[0].unrecovered_room_ids == frozenset()
    assert decisions[0].state is SyncTrustState.UNCERTAIN
    assert decisions[0].reset_client_token is False
    assert responses[1].recovered_room_ids == frozenset({_RECOVERED_ROOM})
    assert responses[1].unrecovered_room_ids == frozenset()
    assert decisions[1].state is SyncTrustState.CERTIFIED
    assert decisions[1].reset_client_token is False
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == "s_after"  # noqa: S105


@pytest.mark.asyncio
async def test_unknown_position_baseline_advances_then_unrecovered_gap_blocks_checkpoint(tmp_path: Path) -> None:
    """Unknown-position replay may advance live sync but not persist past an unrecovered gap."""
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)
    unknown = await trust.reject_unknown_pos()
    baseline_response = _sync_response(
        limited_room_ids=(_UNRECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_initial",
    )
    baseline = await trust.certify_response(
        next_batch=baseline_response.next_batch,
        cache_result=_cache_result(
            baseline_response,
            limited_room_ids=(_UNRECOVERED_ROOM,),
            complete=True,
        ),
    )
    positioned_response = _sync_response(
        limited_room_ids=(_UNRECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset({_UNRECOVERED_ROOM}),
    )
    positioned = await trust.certify_response(
        next_batch=positioned_response.next_batch,
        cache_result=_cache_result(
            positioned_response,
            limited_room_ids=(_UNRECOVERED_ROOM,),
            complete=True,
        ),
    )

    assert unknown.reset_client_token is True
    assert baseline.state is SyncTrustState.UNCERTAIN
    assert baseline.reset_client_token is False
    assert positioned.state is SyncTrustState.UNCERTAIN
    assert positioned.reset_client_token is False


@pytest.mark.asyncio
async def test_admission_failure_rearms_baseline_when_no_checkpoint_can_retry(tmp_path: Path) -> None:
    """Rejected positioned work must rewind and permit one fresh tokenless baseline."""
    trust = _trust(tmp_path, state=SyncTrustState.COLD)
    assert await trust.prepare_startup() is None
    baseline_response = _sync_response(
        limited_room_ids=(_RECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_initial",
    )
    first_baseline = await trust.certify_response(
        next_batch=baseline_response.next_batch,
        cache_result=_cache_result(
            baseline_response,
            limited_room_ids=(_RECOVERED_ROOM,),
            complete=True,
        ),
    )
    assert first_baseline.reset_client_token is False

    trust.record_dispatch_persist_failure()
    trust.reject_response_before_certification()
    retry_baseline = await trust.certify_response(
        next_batch="s_retry",
        cache_result=_cache_result(
            baseline_response,
            limited_room_ids=(_RECOVERED_ROOM,),
            complete=True,
        ),
    )

    assert trust.checkpoint is None
    assert retry_baseline.state is SyncTrustState.UNCERTAIN
    assert retry_baseline.reset_client_token is False


@pytest.mark.asyncio
async def test_restored_token_recovered_only_first_sync_certifies_after_callback_success(tmp_path: Path) -> None:
    """Pinned nio recovered labels prove non-live callback acceptance."""
    response = _sync_response(
        limited_room_ids=(_RECOVERED_ROOM,),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    result = _cache_result(
        response,
        limited_room_ids=(_RECOVERED_ROOM,),
        complete=True,
    )
    save_sync_token(
        tmp_path,
        "code",
        "s_before",
        cache_generation=_CACHE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reset_client_token is False
    assert load_sync_checkpoint(tmp_path, "code") is not None


@pytest.mark.asyncio
async def test_earlier_recovered_gap_with_failed_cache_write_rewinds_continuity(tmp_path: Path) -> None:
    """A local durable failure rewinds even when the wire window is no longer limited."""
    response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    result = _cache_result(
        response,
        limited_room_ids=(),
        complete=False,
    )
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)
    save_sync_token(tmp_path, "code", "s_before", cache_generation=_CACHE_GENERATION)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before",
        cache_generation=_CACHE_GENERATION,
    )


@pytest.mark.asyncio
async def test_earlier_recovered_gap_certifies_after_callback_success(tmp_path: Path) -> None:
    """Pinned nio preserves callback-success proof outside the current window."""
    response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    result = _cache_result(
        response,
        limited_room_ids=(),
        complete=True,
    )
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save is not None
    assert decision.reset_client_token is False


@pytest.mark.parametrize(
    ("complete", "errors"),
    [
        (False, ()),
        (True, (RuntimeError("cache write failed"),)),
        (True, (asyncio.CancelledError(),)),
    ],
    ids=["incomplete", "failed", "cancelled"],
)
@pytest.mark.asyncio
async def test_recovered_gap_fails_closed_when_local_cache_work_does_not_complete(
    tmp_path: Path,
    complete: bool,
    errors: tuple[BaseException, ...],
) -> None:
    """A recovery report cannot license continuity after incomplete local durability."""
    response = _sync_response(
        limited_room_ids=(_RECOVERED_ROOM,),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    result = _cache_result(
        response,
        limited_room_ids=(_RECOVERED_ROOM,),
        complete=complete,
        errors=errors,
    )
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


@pytest.mark.asyncio
async def test_mixed_recovered_and_unrecovered_rooms_withhold_continuity(tmp_path: Path) -> None:
    """One authoritative unrecovered room must outweigh another room's recovery."""
    limited_room_ids = (_RECOVERED_ROOM, _UNRECOVERED_ROOM)
    response = _sync_response(
        limited_room_ids=limited_room_ids,
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset({_UNRECOVERED_ROOM}),
    )
    result = _cache_result(
        response,
        limited_room_ids=limited_room_ids,
        complete=True,
    )
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is False


@pytest.mark.asyncio
async def test_unrecovered_outcome_is_not_inferred_from_current_limited_rooms(tmp_path: Path) -> None:
    """An abandoned earlier gap must fail closed even when this wire window is not limited."""
    response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset({_UNRECOVERED_ROOM}),
    )
    result = _cache_result(
        response,
        limited_room_ids=(),
        complete=True,
    )
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is False


@pytest.mark.asyncio
async def test_positioned_limited_room_without_nio_gap_certifies(tmp_path: Path) -> None:
    """Aggregate outcome absence proves nio planned no gap for a positioned window."""
    response = _sync_response(
        limited_room_ids=(_UNRECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
    )
    result = _cache_result(
        response,
        limited_room_ids=(_UNRECOVERED_ROOM,),
        complete=True,
    )
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint(response.next_batch)
    assert decision.reset_client_token is False
