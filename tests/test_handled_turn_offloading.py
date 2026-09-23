"""Ledger maintenance keeps readers responsive while workers rebuild records."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom import handled_turns
from mindroom.handled_turns import HandledTurnLedger, TurnRecord, TurnRecordCodec, _reset_handled_turn_ledger_runtime
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore


pytestmark = pytest.mark.ledger_loads_from_disk


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "cleanup"])
async def test_ledger_rebuild_keeps_event_loop_responsive(
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Slow decoding or retention must let another loop task release its worker."""
    agent = "responsive_rebuild"
    ledger = HandledTurnLedger(agent, records=journal_store.turn_records(agent))
    await ledger.load()
    target = MessageTarget.resolve("!room:example.org", "$thread", "$source")
    await ledger.record_handled_turn(
        TurnRecord.create(
            ["$source"],
            discovery_event_ids=["$alias"],
            conversation_target=target,
            redacted_source_event_ids=["$source"],
            pending_redaction_cleanup_event_ids=["$source"],
            completed=False,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    timed_out = threading.Event()

    def pause() -> None:
        started.set()
        if not release.wait(2):
            timed_out.set()

    if operation == "load":
        _reset_handled_turn_ledger_runtime()
        ledger = HandledTurnLedger(agent, records=journal_store.turn_records(agent))
        original_decode = TurnRecordCodec._from_ledger_record

        def slow_decode(event_id: str, raw_record: object) -> TurnRecord | None:
            pause()
            return original_decode(event_id, raw_record)

        monkeypatch.setattr(TurnRecordCodec, "_from_ledger_record", slow_decode)
    else:
        original_cleanup = handled_turns._cleaned_responses

        def slow_cleanup(*args: object, **kwargs: object) -> dict[str, TurnRecord]:
            pause()
            return original_cleanup(*args, **kwargs)

        monkeypatch.setattr(handled_turns, "_cleaned_responses", slow_cleanup)

    async def heartbeat() -> None:
        assert await asyncio.to_thread(started.wait, 5)
        release.set()

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        if operation == "load":
            await ledger.load()
        else:
            await ledger.cleanup()
        await heartbeat_task
        assert not timed_out.is_set(), "Ledger reconstruction blocked the event loop"
        assert ledger.get_turn_record("$source") is not None
        assert ledger.get_turn_record("$alias") is not None
        assert len(ledger.turn_records_for_conversation(session_id=target.session_id)) == 1
        assert ledger.pending_redaction_cleanup_event_ids() == ("$source",)
    finally:
        release.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "cleanup"])
async def test_cancelled_rebuild_keeps_write_ownership_until_worker_finishes(
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Repeated cancellation cannot let sibling writes pass an unfinished rebuild."""
    agent = "cancelled_rebuild"
    ledger = HandledTurnLedger(agent, records=journal_store.turn_records(agent))
    await ledger.load()
    await ledger.record_handled_turn(TurnRecord.create(["$original"], response_event_id="$reply"))
    started = threading.Event()
    release = threading.Event()

    def pause() -> None:
        started.set()
        if not release.wait(5):
            pytest.fail("Rebuild worker was not released")

    if operation == "load":
        _reset_handled_turn_ledger_runtime()
        ledger = HandledTurnLedger(agent, records=journal_store.turn_records(agent))
        original_decode = TurnRecordCodec._from_ledger_record

        def slow_decode(event_id: str, raw_record: object) -> TurnRecord | None:
            pause()
            return original_decode(event_id, raw_record)

        monkeypatch.setattr(TurnRecordCodec, "_from_ledger_record", slow_decode)
        rebuilding = asyncio.create_task(ledger.load())
    else:
        original_cleanup = handled_turns._cleaned_responses

        def slow_cleanup(*args: object, **kwargs: object) -> dict[str, TurnRecord]:
            pause()
            return original_cleanup(*args, **kwargs)

        monkeypatch.setattr(handled_turns, "_cleaned_responses", slow_cleanup)
        rebuilding = asyncio.create_task(ledger._cleanup_old_events(max_events=0))

    sibling = HandledTurnLedger(agent, records=journal_store.turn_records(agent))

    async def record_after_load() -> None:
        await sibling.load()
        await sibling.record_handled_turn(TurnRecord.create(["$new"], response_event_id="$new-reply"))

    writing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(2):
            rebuilding.cancel()
            await asyncio.sleep(0)
        writing = asyncio.create_task(record_after_load())
        await asyncio.sleep(0)
        assert not rebuilding.done()
        assert not writing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await rebuilding
        await writing
        assert sibling.get_turn_record("$original") is not None
        assert sibling.get_turn_record("$new") is not None
        persisted = {event_id for event_id, _, _ in await journal_store.turn_records(agent).load_all()}
        assert persisted == {"$original", "$new"}
    finally:
        release.set()
        await asyncio.gather(rebuilding, *([writing] if writing is not None else []), return_exceptions=True)
