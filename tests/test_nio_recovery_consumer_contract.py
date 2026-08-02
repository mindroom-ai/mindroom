"""Staged owning-seam contract tests for typed mindroom-nio recovery outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, cast, get_type_hints
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.logging_config import get_logger
from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncTrustState
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
        rooms=nio.Rooms(invite={}, join=joined_rooms, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(curve25519=0, signed_curve25519=0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        recovered_room_ids=recovered_room_ids,
        unrecovered_room_ids=unrecovered_room_ids,
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
            for index, response in enumerate(responses):
                await client.receive_response(response)
                result = _cache_result(
                    response,
                    limited_room_ids=(_RECOVERED_ROOM,),
                    complete=True,
                )
                decision = await trust.certify_response(
                    next_batch=response.next_batch,
                    cache_result=result,
                    first_sync=index == 0,
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
async def test_unknown_position_baseline_advances_once_then_unrecovered_gap_rewinds(tmp_path: Path) -> None:
    """Unknown-position replay may establish a baseline but may not advance an unrecovered gap."""
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
        first_sync=False,
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
        first_sync=False,
    )

    assert unknown.reset_client_token is True
    assert baseline.state is SyncTrustState.UNCERTAIN
    assert baseline.reset_client_token is False
    assert positioned.state is SyncTrustState.UNCERTAIN
    assert positioned.reset_client_token is True


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
        first_sync=True,
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
        first_sync=False,
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
        first_sync=True,
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
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") is None


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
        first_sync=False,
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
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


@pytest.mark.asyncio
async def test_mixed_recovered_and_unrecovered_rooms_reset_continuity(tmp_path: Path) -> None:
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
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


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
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


@pytest.mark.asyncio
async def test_unclassified_limited_room_fails_closed(tmp_path: Path) -> None:
    """An enabled or disabled recovery path may never turn missing classification into success."""
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
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True
