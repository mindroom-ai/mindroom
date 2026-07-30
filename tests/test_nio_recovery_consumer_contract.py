"""Staged owning-seam contract tests for typed mindroom-nio recovery outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, cast, get_type_hints

import nio
import pytest

from mindroom.logging_config import get_logger
from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncTrustState
from mindroom.matrix.sync_tokens import load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot_runtime_view import BotRuntimeView

_CACHE_GENERATION = "nio-recovery-contract"
_RECOVERED_ROOM = "!recovered:localhost"
_UNRECOVERED_ROOM = "!unrecovered:localhost"


@dataclass
class _EventCache:
    cache_generation: str = _CACHE_GENERATION


@dataclass
class _Runtime:
    event_cache: _EventCache


def _trust(tmp_path: Path, *, state: SyncTrustState) -> SyncCacheTrust:
    runtime = _Runtime(event_cache=_EventCache())
    return SyncCacheTrust(
        storage_path=tmp_path,
        agent_name="code",
        runtime=cast("BotRuntimeView", runtime),
        logger=get_logger(),
        state=state,
    )


def _base_sync_response(
    *,
    limited_room_ids: tuple[str, ...],
    next_batch: str = "s_after",
) -> nio.SyncResponse:
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
    )


def _sync_response(
    *,
    limited_room_ids: tuple[str, ...],
    recovered_room_ids: frozenset[str],
    unrecovered_room_ids: frozenset[str],
) -> nio.SyncResponse:
    """Build a real response and fail first on the unreleased upstream contract."""
    response_fields = {item.name for item in fields(nio.SyncResponse)}
    expected_fields = {"recovered_room_ids", "unrecovered_room_ids"}
    assert expected_fields <= response_fields, (
        "mindroom-nio must release typed recovered_room_ids and "
        "unrecovered_room_ids on SyncResponse before MindRoom consumes them"
    )
    return replace(
        _base_sync_response(limited_room_ids=limited_room_ids),
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


def test_restored_token_recovered_only_first_sync_stays_uncertified_without_callback_success(tmp_path: Path) -> None:
    """Nio 0.32 recovered labels do not prove non-live callback acceptance."""
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=True,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") is None


def test_earlier_recovered_gap_with_failed_cache_write_rewinds_continuity(tmp_path: Path) -> None:
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") is None


def test_earlier_recovered_gap_stays_uncertified_without_callback_success(tmp_path: Path) -> None:
    """A recovered outcome outside the current window remains unsafe on nio 0.32."""
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


@pytest.mark.parametrize(
    ("complete", "errors"),
    [
        (False, ()),
        (True, (RuntimeError("cache write failed"),)),
        (True, (asyncio.CancelledError(),)),
    ],
    ids=["incomplete", "failed", "cancelled"],
)
def test_recovered_gap_fails_closed_when_local_cache_work_does_not_complete(
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


def test_mixed_recovered_and_unrecovered_rooms_reset_continuity(tmp_path: Path) -> None:
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


def test_unrecovered_outcome_is_not_inferred_from_current_limited_rooms(tmp_path: Path) -> None:
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


def test_unclassified_limited_room_fails_closed(tmp_path: Path) -> None:
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

    decision = trust.certify_response(
        next_batch=response.next_batch,
        cache_result=result,
        first_sync=False,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True
