"""Room departure is exact durable termination for its unfinished responses."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.event_journal import DeliveryStage, EventClass, EventKind, InboundEvent
from mindroom.handled_turns import TurnRecord
from mindroom.response_runner import ResponseRunner
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from tests.journal_membership_helpers import admit_room_membership
from tests.test_response_delivery_gateway import _response_recovery_bot
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore

ROOM = "!departed:localhost"


def _source(event_id: str, room_id: str = ROOM) -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        room_id=room_id,
        thread_id=None,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender="@human:localhost",
        origin_server_ts=1000,
        source={"event_id": event_id, "content": {"msgtype": "m.text", "body": "request"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rejoin", [False, True])
async def test_departure_during_generation_allows_orderly_shutdown(
    journal_store: EventJournalStore,
    rejoin: bool,
) -> None:
    """Two coalesced sources retired by departure need no FINAL to release their response."""
    principal = journal_store.principal("agent@alice")
    await admit_room_membership(principal, ROOM, "join")
    sources = ("$first", "$second")
    for source_id in sources:
        await principal.admit(_source(source_id))
    turn = TurnRecord.create(sources)
    turn_store = await _store(journal_store, agent_name="agent")
    await turn_store.record_pending_turn(turn)
    bot = _response_recovery_bot(journal_store, turn_store)
    runner = ResponseRunner(deps=MagicMock())
    generating = asyncio.Event()

    async def generate() -> None:
        assert turn_store.try_claim_turn(turn)
        generating.set()
        try:
            await asyncio.Event().wait()
        finally:
            turn_store.release_pending_turn_claim(turn)

    response = runner.track_inbox_response(
        generate(),
        name="generation_during_departure",
        recovery_proof_ready=lambda: bot._response_recovery_ready(turn),
        source_event_ids=sources,
    )
    try:
        await generating.wait()
        await admit_room_membership(principal, ROOM, "leave")
        if rejoin:
            await admit_room_membership(principal, ROOM, "join")
        runner.begin_process_shutdown()
        assert await runner.drain_inbox_responses(cancel_after_seconds=0.05, shutdown_intent=ORDERLY_SHUTDOWN)
        assert response.cancelled()
        assert runner.pending_inbox_response_count == 0
        assert not any(turn_store.has_live_turn_claim(source_id) for source_id in sources)
        assert await principal.load_matrix_delivery(delivery_id=turn.anchor_event_id, stage=DeliveryStage.FINAL) is None
    finally:
        tasks = [response, *(owner.proof_task for owner in runner._inbox_response_tasks.values() if owner.proof_task)]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["settled", "missing", "other_room", "mixed", "live_claim", "no_anchor"])
async def test_departure_proof_rejects_unexplained_or_inexact_sources(
    journal_store: EventJournalStore,
    case: str,
) -> None:
    """Only exact retained sources from an ended membership prove intentional termination."""
    principal = journal_store.principal("agent@alice")
    await admit_room_membership(principal, ROOM, "join")
    await principal.admit(_source("$source"))
    turn = TurnRecord.create(["$source"])
    turn_store = await _store(journal_store, agent_name="agent")
    await turn_store.record_pending_turn(turn)
    bot = _response_recovery_bot(journal_store, turn_store)
    await principal.settle("$source")
    if case == "other_room":
        await admit_room_membership(principal, "!other:localhost", "join")
        await admit_room_membership(principal, "!other:localhost", "leave")
    elif case != "settled":
        await admit_room_membership(principal, ROOM, "leave")
    if case == "missing":
        turn = TurnRecord.create(["$source", "$missing"])
    elif case == "mixed":
        await principal.admit(_source("$current", "!current:localhost"))
        await principal.settle("$current")
        turn = TurnRecord.create(["$source", "$current"])
    elif case == "live_claim":
        assert turn_store.try_claim_turn(turn)
    elif case == "no_anchor":
        turn = replace(turn, anchor_event_id=None)
    try:
        assert await bot._response_recovery_ready(turn) is False
    finally:
        turn_store.release_pending_turn_claim(turn)
