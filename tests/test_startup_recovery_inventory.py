"""Startup discovery follows durable visible debt, not room history."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.event_journal import DeliveryStage, DepartureSource
from mindroom.matrix.stale_stream_cleanup import recover_stale_streaming_messages
from tests.conftest import delivered_matrix_side_effect, runtime_paths_for
from tests.journal_membership_helpers import admit_room_membership
from tests.test_event_journal_store import ROOM, admit
from tests.test_stale_stream_cleanup import (
    BOT_USER_ID,
    NOW_MS,
    STALE_AGE_MS,
    _aiter,
    _make_client,
    _make_config,
    _make_message_event,
    _permitted_recovery_scope,
    _room_get_event_response,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio


async def _initial(principal: PrincipalStore, source: str, *, acknowledged: bool = True, room_id: str = ROOM) -> None:
    await admit(principal, source, room_id=room_id)
    await principal.enqueue_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        room_id=room_id,
        thread_id=None,
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await principal.claim_matrix_delivery(delivery_id=source, stage=DeliveryStage.INITIAL)
    if acknowledged:
        await principal.acknowledge_matrix_delivery(
            delivery_id=source,
            stage=DeliveryStage.INITIAL,
            event_id=f"{source}-response",
            delivered_projections=(),
        )


async def test_inventory_keeps_ack_before_turn_binding_and_excludes_other_owners(
    journal_store: EventJournalStore,
) -> None:
    """Recovery owns acknowledged orphans, not pending sends or FINAL-owned work."""
    principal = journal_store.principal("agent@alice")
    await _initial(principal, "$orphan")
    await _initial(principal, "$unacknowledged", acknowledged=False)
    await _initial(journal_store.principal("other@alice"), "$other")
    for source in ("$owed", "$finished"):
        await _initial(principal, source)
        await principal.enqueue_matrix_delivery(
            delivery_id=source,
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "Answer"},
        )
    await principal.claim_matrix_delivery(delivery_id="$finished", stage=DeliveryStage.FINAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$finished",
        stage=DeliveryStage.FINAL,
        event_id="$answer",
        delivered_projections=(),
    )
    candidates = await principal.recovery_initial_deliveries()
    assert [item.delivery_id for item in candidates] == ["$orphan"]
    assert candidates[0].acknowledged_event_id == "$orphan-response"
    assert await journal_store.turn_records("agent").load_all() == ()


async def test_inventory_advances_past_a_full_page_and_revisits_late_ack(
    journal_store: EventJournalStore,
) -> None:
    """A new pass sees an INITIAL acknowledged after an earlier inventory read."""
    principal = journal_store.principal("agent@alice")
    await _initial(principal, "$late", acknowledged=False)
    for index in range(101):
        await _initial(principal, f"$source-{index:03}")
    first = await principal.recovery_initial_deliveries()
    assert len(first) == 100
    last = first[-1]
    second = await principal.recovery_initial_deliveries(after=(last.created_at_ns, last.delivery_id))
    assert [item.delivery_id for item in second] == ["$source-100"]
    await principal.acknowledge_matrix_delivery(
        delivery_id="$late",
        stage=DeliveryStage.INITIAL,
        event_id="$late-response",
        delivered_projections=(),
    )
    second = await principal.recovery_initial_deliveries(after=(last.created_at_ns, last.delivery_id))
    assert [item.delivery_id for item in second] == ["$source-100"]
    fresh_pass = await principal.recovery_initial_deliveries()
    assert fresh_pass[0].delivery_id == "$late"


@pytest.mark.parametrize("state", ["retired", "departed", "rejoined", "failed_final"])
async def test_inventory_respects_membership_and_delivery_ownership(
    journal_store: EventJournalStore,
    state: str,
) -> None:
    """Obsolete tenure is excluded; a failed FINAL cannot finish visible debt."""
    principal = journal_store.principal("agent@alice")
    await _initial(principal, "$target", acknowledged=state != "retired")
    if state == "retired":
        await principal.retire_matrix_delivery(
            delivery_id="$target",
            stage=DeliveryStage.INITIAL,
            room_id=ROOM,
            membership_epoch=0,
        )
    elif state in {"departed", "rejoined"}:
        await admit_room_membership(principal, ROOM, "leave", source=DepartureSource.LOCAL)
        if state == "rejoined":
            await admit_room_membership(principal, ROOM, "join")
    else:
        await principal.enqueue_matrix_delivery(
            delivery_id="$target",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "Answer"},
        )
        await principal.claim_matrix_delivery(delivery_id="$target", stage=DeliveryStage.FINAL)
        await principal.record_permanent_matrix_delivery_failure(
            delivery_id="$target",
            stage=DeliveryStage.FINAL,
            reason="unrepresentable payload",
        )
    candidates = await principal.recovery_initial_deliveries()
    assert [item.delivery_id for item in candidates] == (["$target"] if state == "failed_final" else [])


async def test_empty_inventory_needs_no_matrix_discovery(journal_store: EventJournalStore, tmp_path: Path) -> None:
    """No durable debt means no remote discovery calls."""
    client = _make_client()
    config = _make_config(tmp_path)
    result = await recover_stale_streaming_messages(
        {BOT_USER_ID: client},
        principals={BOT_USER_ID: journal_store.principal("agent@alice")},
        resume_client=None,
        response_recovery_scope=_permitted_recovery_scope,
        config=config,
        runtime_paths=runtime_paths_for(config),
        startup_cutoff_ms=NOW_MS,
        scanned_room_ids=set(),
    )
    assert result.room_count == result.cleaned_count == result.resumed_count == 0
    client.joined_rooms.assert_not_awaited()
    client.room_messages.assert_not_awaited()
    client.room_get_event.assert_not_awaited()


async def test_busy_room_ownership_does_not_delay_other_rooms(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """One occupied response lock cannot hold up every room's recovery."""
    principal = journal_store.principal("agent@alice")
    await _initial(principal, "$busy")
    await _initial(principal, "$healthy", room_id="!other:localhost")
    busy_entered = asyncio.Event()
    healthy_visited = asyncio.Event()
    release_busy = asyncio.Event()

    @asynccontextmanager
    async def ownership(_agent: str, _room: str, event: str) -> AsyncIterator[bool]:
        if event == "$busy-response":
            busy_entered.set()
            await release_busy.wait()
        else:
            healthy_visited.set()
        yield False

    client = _make_client()
    config = _make_config(tmp_path)
    task = asyncio.create_task(
        recover_stale_streaming_messages(
            {BOT_USER_ID: client},
            principals={BOT_USER_ID: principal},
            resume_client=None,
            response_recovery_scope=ownership,
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=NOW_MS,
            scanned_room_ids=set(),
        ),
    )
    try:
        await asyncio.wait_for(busy_entered.wait(), 2)
        await asyncio.wait_for(healthy_visited.wait(), 2)
    finally:
        release_busy.set()
        await task
    client.room_get_event.assert_not_awaited()


@pytest.mark.parametrize("router_unavailable", [False, True])
async def test_exact_inventory_repairs_response_without_room_scan(
    journal_store: EventJournalStore,
    tmp_path: Path,
    router_unavailable: bool,
) -> None:
    """Known delivery IDs suffice even when the resume identity is unavailable."""
    principal = journal_store.principal("agent@alice")
    await _initial(principal, "$orphan")
    client = _make_client()
    config = _make_config(tmp_path)
    router = _make_client() if router_unavailable else None
    if router is not None:
        router.joined_rooms.side_effect = ConnectionError("router membership unavailable")
    client.room_get_event.side_effect = None
    client.room_get_event.return_value = _room_get_event_response(
        _make_message_event(
            event_id="$orphan-response",
            body="Thinking...",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            room_id=ROOM,
            extra_content={"io.mindroom.stream_status": "pending"},
        ),
    )
    client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter())
    with (
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            AsyncMock(
                side_effect=delivered_matrix_side_effect("$edit"),
            ),
        ) as edit,
    ):
        result = await recover_stale_streaming_messages(
            {BOT_USER_ID: client},
            principals={BOT_USER_ID: principal},
            resume_client=router,
            response_recovery_scope=_permitted_recovery_scope,
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=NOW_MS,
            scanned_room_ids=set(),
        )
    assert result.cleaned_count == 1
    assert edit.await_args.args[2] == "$orphan-response"
    client.room_messages.assert_not_awaited()
    client.room_get_event.assert_awaited_once_with(ROOM, "$orphan-response")


async def test_failed_ownership_read_does_not_block_other_candidates(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A broken ownership record does not abort later candidate checks."""
    principal = journal_store.principal("agent@alice")
    await _initial(principal, "$broken")
    await _initial(principal, "$healthy")
    client = _make_client()
    config = _make_config(tmp_path)
    visited = []

    @asynccontextmanager
    async def ownership(_agent: str, _room: str, event: str) -> AsyncIterator[bool]:
        visited.append(event)
        if event == "$broken-response":
            message = "one unavailable ownership record"
            raise RuntimeError(message)
        yield False

    await recover_stale_streaming_messages(
        {BOT_USER_ID: client},
        principals={BOT_USER_ID: principal},
        resume_client=None,
        response_recovery_scope=ownership,
        config=config,
        runtime_paths=runtime_paths_for(config),
        startup_cutoff_ms=NOW_MS,
        scanned_room_ids=set(),
    )
    assert visited == ["$broken-response", "$healthy-response"]
    client.room_get_event.assert_not_awaited()
