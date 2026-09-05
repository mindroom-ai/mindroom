"""Post-admission lifecycle effects must survive receipt replay without blocking ingestion."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.event_journal import AdmissionFacts, RoomMembershipPosition
from tests.test_bot_ready_hook import (
    _agent_bot,
    _complete_frame,
    _router_bot_with_orchestrator,
    _validated_reported_membership_admission,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_new", [True, False])
@pytest.mark.parametrize("membership", ["leave", "ban", "invite"])
async def test_owned_departure_reconciles_app_state_on_receipt_replay(
    tmp_path: Path,
    receipt_new: bool,
    membership: str,
) -> None:
    """A committed departure must retry call and invite cleanup before settlement."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership=membership,
        previous_epoch=0,
    )
    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=admission.consumer_generation)
    await principal.bind_ingestion_stream(generation=admission.consumer_generation, stream_id=admission.stream_id)
    await principal.note_membership_restarted(room_id)
    facts = await principal.admit_ingestion_batch(admission)
    if not receipt_new:
        facts = await principal.admit_ingestion_batch(admission)
    manager = MagicMock()
    manager.on_sync_room_membership = AsyncMock()
    bot._call_manager = manager
    bot._local_departures_awaiting_sync.add(room_id)
    with (
        patch.object(
            bot._room_lifecycle,
            "forget_invited_room",
        ) as forget,
    ):
        await bot._after_ingestion_admission(admission, facts, None)
    manager.on_sync_room_membership.assert_awaited_once_with(joined_room_ids=set(), left_room_ids={room_id})
    forget.assert_called_once_with(room_id)
    assert room_id not in bot._local_departures_awaiting_sync


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [RoomMembershipPosition("join", 1), RoomMembershipPosition("leave", 2)])
async def test_old_departure_receipt_cannot_clear_a_new_membership(
    tmp_path: Path,
    position: RoomMembershipPosition,
) -> None:
    """Post-commit effects must honor the current epoch and membership."""
    bot = _agent_bot(tmp_path)
    admission = _validated_reported_membership_admission(
        bot,
        "!rejoined:localhost",
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    principal = MagicMock()
    principal.membership_position = AsyncMock(return_value=position)
    with (
        patch.object(bot, "journal_principal", return_value=principal),
        patch.object(
            bot._room_lifecycle,
            "forget_invited_room",
        ) as forget,
    ):
        await bot._after_ingestion_admission(admission, AdmissionFacts(False, False), None)
    forget.assert_not_called()


@pytest.mark.asyncio
async def test_completed_frame_clears_join_notice_fences_for_joined_rooms(tmp_path: Path) -> None:
    """A successful source frame restores later actionable decrypt-failure notices."""
    bot = _agent_bot(tmp_path)
    room_id = "!joined:localhost"
    await bot._room_lifecycle._add_join_decrypt_notice_fence(room_id)
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    client = MagicMock(spec=nio.AsyncClient)
    client.rooms = {room_id: nio.MatrixRoom(room_id, bot.matrix_id.full_id)}
    bot.client = client
    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await _complete_frame(bot)
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    assert bot._sync_continuity_store.load().pending_join_decrypt_fences == frozenset()


@pytest.mark.asyncio
async def test_live_grant_reconciliation_does_not_wait_for_a_local_join(tmp_path: Path) -> None:
    """A pending invite cannot make the pump await work owned by its blocked runner."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    release_join = asyncio.Event()
    join_started = asyncio.Event()

    async def pending_invites() -> None:
        join_started.set()
        await release_join.wait()

    orchestrator.reconcile_pending_invites = AsyncMock(side_effect=pending_invites)
    try:
        await asyncio.wait_for(bot._reconcile_reply_membership_effects(), timeout=1)
        await asyncio.wait_for(join_started.wait(), timeout=1)
        orchestrator.reconcile_reply_authorized_calls.assert_awaited_once()
    finally:
        release_join.set()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)


@pytest.mark.asyncio
@pytest.mark.parametrize("standalone", [False, True])
async def test_live_grant_reconciliation_does_not_wait_for_call_admission(
    tmp_path: Path,
    standalone: bool,
) -> None:
    """The pump must settle control events while startup holds call admission closed."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    call_started = asyncio.Event()
    release_call = asyncio.Event()

    async def reconcile_calls() -> None:
        call_started.set()
        await release_call.wait()

    calls = AsyncMock(side_effect=reconcile_calls)
    revoke = AsyncMock()
    if standalone:
        bot.orchestrator = None
        manager = MagicMock()
        manager.reconcile_reply_authorization = calls
        manager.revoke_reply_authorization = revoke
        bot._call_manager = manager
    else:
        orchestrator.reconcile_reply_authorized_calls = calls
        orchestrator.revoke_reply_authorized_calls = revoke

    with patch.object(bot, "schedule_pending_invite_reconciliation"):
        try:
            await asyncio.wait_for(bot._reconcile_reply_membership_effects(), timeout=1)
            revoke.assert_awaited_once_with()
            await asyncio.wait_for(call_started.wait(), timeout=1)
            assert not release_call.is_set()
        finally:
            release_call.set()
            assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)


@pytest.mark.asyncio
async def test_queued_own_departure_cannot_undo_authoritative_rejoin(tmp_path: Path) -> None:
    """A delayed semantic callback must not apply self-membership a second time."""
    bot = _agent_bot(tmp_path)
    room_id = "!rejoined:localhost"
    room = nio.MatrixRoom(room_id, bot.matrix_id.full_id)
    departed: set[str] = set()

    async def apply_old_leave(_room: nio.MatrixRoom, _event: nio.RoomMemberEvent) -> None:
        departed.add(room_id)

    manager = MagicMock()
    manager.on_room_membership_event = AsyncMock(side_effect=apply_old_leave)
    bot._call_manager = manager
    event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$old-leave",
            "sender": bot.matrix_id.full_id,
            "state_key": bot.matrix_id.full_id,
            "origin_server_ts": 1,
            "type": "m.room.member",
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    await bot._journal_dispatcher.callbacks.on_room_lifecycle(room, event)
    assert departed == set()


@pytest.mark.asyncio
async def test_authoritative_join_requests_call_reconciliation_after_frame_publication(tmp_path: Path) -> None:
    """A state-only rejoin must discover calls after Nio publishes current room state."""
    bot = _agent_bot(tmp_path)
    admission = _validated_reported_membership_admission(
        bot,
        "!rejoined:localhost",
        previous_membership="leave",
        membership="join",
        previous_epoch=1,
    )
    principal = MagicMock()
    principal.membership_position = AsyncMock(return_value=RoomMembershipPosition("join", 1))
    manager = MagicMock()
    manager.on_sync_room_membership = AsyncMock()
    bot._call_manager = manager
    bot._calls_reconcile_pending = False
    with patch.object(bot, "journal_principal", return_value=principal):
        await bot._after_ingestion_admission(admission, AdmissionFacts(True, False), None)
    assert bot._calls_reconcile_pending
