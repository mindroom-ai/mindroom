"""Staged owning-seam contract tests for typed mindroom-nio recovery outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import TYPE_CHECKING, get_type_hints
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.logging_config import get_logger
from mindroom.matrix.sync_certification import SyncRecoveryOutcome, SyncTrustState
from mindroom.matrix.sync_checkpoint_trust import SyncCheckpointTrust
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint
from tests.sync_continuity_helpers import (
    RecordedHistoryRecoveries,
    certify_response,
    load_sync_checkpoint,
    save_sync_token,
)

if TYPE_CHECKING:
    from pathlib import Path


_STORE_GENERATION = "nio-recovery-contract"
_RECOVERED_ROOM = "!recovered:localhost"
_UNRECOVERED_ROOM = "!unrecovered:localhost"


def _trust(tmp_path: Path, *, state: SyncTrustState) -> SyncCheckpointTrust:
    return SyncCheckpointTrust(
        continuity_store=SyncContinuityStore(tmp_path, "code"),
        logger=get_logger(),
        state=state,
        store_generation=_STORE_GENERATION,
        history_recovery_provider=RecordedHistoryRecoveries,
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


@pytest.mark.asyncio
async def test_real_nio_unrecovered_gap_replays_from_mindroom_checkpoint(
    tmp_path: Path,
) -> None:
    """Rejected Classic staging is rebuilt from MindRoom's committed cursor."""
    room_id = "!replay:localhost"
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            store_sync_tokens=False,
            backfill_limited_timelines=True,
            backfill_persist_recovery=False,
        ),
    )
    save_sync_token(
        tmp_path,
        "code",
        "s_committed",
        store_generation=_STORE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)
    client.next_batch = await trust.prepare_startup() or ""
    failed_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_uncommitted",
    )
    replay_response = _sync_response(
        limited_room_ids=(room_id,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_replayed",
    )
    recovery_page = nio.RoomMessagesResponse(
        room_id=room_id,
        chunk=[],
        start="p_0",
        end=None,
    )

    try:
        with patch.object(
            client,
            "_recovery_room_messages",
            AsyncMock(side_effect=asyncio.TimeoutError),
        ):
            await client.receive_response(failed_response)
        failed = await certify_response(
            trust,
            next_batch=failed_response.next_batch,
            recovery=_recovery(failed_response),
        )

        assert failed.reset_client_token is True
        assert failed_response.unrecovered_room_ids == frozenset({room_id})
        await client.reset_classic_sync_state()
        client.next_batch = trust.retry_token() or ""
        assert client.next_batch == "s_committed"

        with patch.object(
            client,
            "_recovery_room_messages",
            AsyncMock(return_value=recovery_page),
        ):
            await client.receive_response(replay_response)
        certified = await certify_response(
            trust,
            next_batch=replay_response.next_batch,
            recovery=_recovery(replay_response),
        )
    finally:
        await client.close()

    assert replay_response.recovered_room_ids == frozenset({room_id})
    assert certified.state is SyncTrustState.CERTIFIED
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_replayed",
        store_generation=_STORE_GENERATION,
    )


@pytest.mark.asyncio
async def test_real_nio_retries_full_state_until_classic_rebuild_succeeds() -> None:
    """A transient error cannot downgrade MindRoom's requested full-state rebuild."""
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            store_sync_tokens=False,
            backfill_limited_timelines=True,
            backfill_persist_recovery=False,
        ),
    )
    client.restore_login("@code:localhost", "DEVICEID", "token")
    full_state_requests: list[bool | None] = []

    async def sync(
        _timeout: int | None,
        _sync_filter: object,
        _since: str | None,
        full_state: bool | None,
        _presence: str | None,
    ) -> nio.SyncResponse | nio.SyncError:
        full_state_requests.append(full_state)
        if len(full_state_requests) == 1:
            return nio.SyncError.from_dict(
                {
                    "errcode": "M_LIMIT_EXCEEDED",
                    "error": "retry the rebuild",
                },
            )
        client.stop_sync_forever()
        return _sync_response(
            limited_room_ids=(),
            recovered_room_ids=frozenset(),
            unrecovered_room_ids=frozenset(),
            next_batch="s_rebuilt",
        )

    try:
        with (
            patch.object(client, "sync", new=sync),
            patch.object(client, "send_to_device_messages", new=AsyncMock(return_value=[])),
        ):
            await client.sync_forever(full_state=True)
    finally:
        await client.close()

    assert full_state_requests == [True, True]


@pytest.mark.asyncio
async def test_real_nio_classic_reset_ends_sync_generation_before_reentry() -> None:
    """A Classic reset returns the old loop before reentry from the committed cursor."""
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            store_sync_tokens=False,
            backfill_limited_timelines=True,
            backfill_persist_recovery=False,
        ),
    )
    client.restore_login("@code:localhost", "DEVICEID", "token")
    client.next_batch = "s_live"
    sync_calls: list[tuple[bool | None, str]] = []
    reset_done = False

    async def reset_after_first_response(_response: nio.SyncResponse) -> None:
        nonlocal reset_done
        if reset_done:
            return
        reset_done = True
        await client.reset_classic_sync_state()
        client.next_batch = "s_committed"

    async def sync(
        _timeout: int | None,
        _sync_filter: object,
        _since: str | None,
        full_state: bool | None,
        _presence: str | None,
    ) -> nio.SyncResponse:
        sync_calls.append((full_state, client.next_batch))
        if len(sync_calls) == 2:
            client.stop_sync_forever()
        return _sync_response(
            limited_room_ids=(),
            recovered_room_ids=frozenset(),
            unrecovered_room_ids=frozenset(),
            next_batch=f"s_response_{len(sync_calls)}",
        )

    client.add_response_callback(reset_after_first_response, nio.SyncResponse)
    try:
        with (
            patch.object(client, "sync", new=sync),
            patch.object(client, "send_to_device_messages", new=AsyncMock(return_value=[])),
        ):
            await asyncio.wait_for(client.sync_forever(full_state=False), timeout=1)
            assert sync_calls == [(False, "s_live")]
            assert client.next_batch == "s_committed"

            await asyncio.wait_for(client.sync_forever(full_state=True), timeout=1)
    finally:
        await client.close()

    assert sync_calls == [
        (False, "s_live"),
        (True, "s_committed"),
    ]


@pytest.mark.asyncio
async def test_real_nio_acknowledges_only_fully_applied_classic_state() -> None:
    """A failed response cannot clear nio's dirty state through its advanced cursor."""
    client = nio.AsyncClient(
        "https://localhost",
        "@code:localhost",
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            store_sync_tokens=False,
            backfill_limited_timelines=True,
            backfill_persist_recovery=False,
        ),
    )
    client.next_batch = "s_committed"
    response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_failed",
    )

    try:
        with (
            patch.object(
                client,
                "_handle_to_device",
                new=AsyncMock(side_effect=RuntimeError("response failed")),
            ),
            pytest.raises(RuntimeError, match="response failed"),
        ):
            await client.receive_response(response)

        assert client.next_batch == "s_failed"
        assert client.has_uncommitted_classic_sync_state
        with pytest.raises(nio.LocalProtocolError, match="does not match"):
            client.acknowledge_classic_sync("s_failed")
        assert client.has_uncommitted_classic_sync_state
    finally:
        await client.close()


def _recovery(
    response: nio.SyncResponse,
    *,
    admission_refused: bool = False,
) -> SyncRecoveryOutcome:
    """Build the recovery outcome from the exact typed upstream response."""
    return SyncRecoveryOutcome.from_sync_response(
        response,
        admission_refused=admission_refused,
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
async def test_cold_limited_initial_snapshot_certifies(tmp_path: Path) -> None:
    """A complete tokenless initial snapshot is a valid durable baseline."""
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
    response = _sync_response(
        limited_room_ids=(_RECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
        next_batch="s_initial",
    )

    try:
        await client.receive_response(response)
        decision = await certify_response(
            trust,
            next_batch=response.next_batch,
            recovery=_recovery(response),
        )
    finally:
        await client.close()

    assert response.recovered_room_ids == frozenset()
    assert response.unrecovered_room_ids == frozenset()
    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reset_client_token is False
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == "s_initial"  # noqa: S105


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
    baseline = await certify_response(
        trust,
        next_batch=baseline_response.next_batch,
        recovery=_recovery(baseline_response),
    )
    positioned_response = _sync_response(
        limited_room_ids=(_UNRECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset({_UNRECOVERED_ROOM}),
    )
    positioned = await certify_response(
        trust,
        next_batch=positioned_response.next_batch,
        recovery=_recovery(positioned_response),
    )

    assert unknown.reset_client_token is True
    assert baseline.state is SyncTrustState.CERTIFIED
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
    first_baseline = trust.plan_response(
        next_batch=baseline_response.next_batch,
        recovery=_recovery(baseline_response),
    )
    assert first_baseline.reset_client_token is False

    trust.record_dispatch_persist_failure()
    trust.reject_response_before_certification()
    retry_baseline = await certify_response(
        trust,
        next_batch="s_retry",
        recovery=_recovery(baseline_response),
    )

    assert trust.checkpoint == SyncCheckpoint("s_retry")
    assert retry_baseline.state is SyncTrustState.CERTIFIED
    assert retry_baseline.reset_client_token is False


@pytest.mark.asyncio
async def test_restored_token_recovered_only_first_sync_certifies_after_callback_success(tmp_path: Path) -> None:
    """Pinned nio recovered labels prove non-live callback acceptance."""
    response = _sync_response(
        limited_room_ids=(_RECOVERED_ROOM,),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    recovery = _recovery(response)
    save_sync_token(
        tmp_path,
        "code",
        "s_before",
        store_generation=_STORE_GENERATION,
    )
    trust = _trust(tmp_path, state=SyncTrustState.PENDING)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
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
    recovery = _recovery(response, admission_refused=True)
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)
    save_sync_token(tmp_path, "code", "s_before", store_generation=_STORE_GENERATION)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True
    assert load_sync_checkpoint(tmp_path, "code") == SyncCheckpoint(
        "s_before",
        store_generation=_STORE_GENERATION,
    )


@pytest.mark.asyncio
async def test_earlier_recovered_gap_certifies_after_callback_success(tmp_path: Path) -> None:
    """Pinned nio preserves callback-success proof outside the current window."""
    response = _sync_response(
        limited_room_ids=(),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    recovery = _recovery(response)
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save is not None
    assert decision.reset_client_token is False


@pytest.mark.asyncio
async def test_recovered_gap_fails_closed_when_admission_refused_an_event(tmp_path: Path) -> None:
    """A recovery report cannot license continuity over an event nobody owns."""
    response = _sync_response(
        limited_room_ids=(_RECOVERED_ROOM,),
        recovered_room_ids=frozenset({_RECOVERED_ROOM}),
        unrecovered_room_ids=frozenset(),
    )
    recovery = _recovery(response, admission_refused=True)
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
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
    recovery = _recovery(response)
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
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
    recovery = _recovery(response)
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
    )

    assert decision.state is SyncTrustState.UNCERTAIN
    assert decision.checkpoint_to_save is None
    assert decision.reset_client_token is True


@pytest.mark.asyncio
async def test_positioned_limited_room_without_nio_gap_certifies(tmp_path: Path) -> None:
    """Aggregate outcome absence proves nio planned no gap for a positioned window."""
    response = _sync_response(
        limited_room_ids=(_UNRECOVERED_ROOM,),
        recovered_room_ids=frozenset(),
        unrecovered_room_ids=frozenset(),
    )
    recovery = _recovery(response)
    trust = _trust(tmp_path, state=SyncTrustState.CERTIFIED)

    decision = await certify_response(
        trust,
        next_batch=response.next_batch,
        recovery=recovery,
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.checkpoint_to_save == SyncCheckpoint(response.next_batch)
    assert decision.reset_client_token is False
