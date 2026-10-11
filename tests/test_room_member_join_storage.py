"""Membership hook markers share the journal's atomic admission and principal scope."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import nio
import pytest
from nio.durable import RecordKind, SyncBatch, SyncRecord

from mindroom.event_journal import membership_hooks
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from tests.test_durable_ingestion_admission import ACCOUNT, ROOM, Session, principal_for
from tests.test_room_member_hooks import _room_member_event

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.event_journal.backend import Transaction

USER = "@alice:localhost"


def _member(event_id: str, *, history: bool = False) -> SyncRecord:
    return SyncRecord(
        RecordKind.TIMELINE,
        ROOM,
        _room_member_event(event_id=event_id).source,
        provenance=nio.TimelineEventProvenance.HISTORY if history else nio.TimelineEventProvenance.LIVE,
    )


async def _admit(principal: PrincipalStore, stream: UUID, sequence: int, record: SyncRecord) -> None:
    await consume_one_ingestion_batch(Session(SyncBatch(stream, sequence, (record,))), principal, account_id=ACCOUNT)


@pytest.mark.asyncio
async def test_duplicate_baseline_preserves_every_older_pending_join(journal_store: EventJournalStore) -> None:
    """An old event observed as a new baseline must use the current receipt frontier."""
    stream = uuid4()
    principal = await principal_for(journal_store, stream)
    await _admit(principal, stream, 1, _member("$first"))
    await _admit(principal, stream, 2, _member("$second"))
    await _admit(principal, stream, 3, _member("$first", history=True))
    assert not await principal.is_room_member_join_suppressed(ROOM, "$first", USER)
    assert not await principal.is_room_member_join_suppressed(ROOM, "$second", USER)
    await _admit(principal, stream, 4, _member("$profile"))
    assert await principal.is_room_member_join_suppressed(ROOM, "$profile", USER)


@pytest.mark.asyncio
async def test_completed_marker_is_idempotent_principal_scoped_and_survives_reopen(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """Completed hooks survive restart without suppressing another principal's work."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    await _admit(principal, stream, 1, _member("$join"))
    assert not await principal.is_room_member_join_suppressed(ROOM, "$join", USER)
    await principal.mark_room_member_join_completed(ROOM, USER)
    await principal.mark_room_member_join_completed(ROOM, USER)
    await store.close()

    reopened = journal_database()
    principal = await principal_for(reopened, stream)
    assert await principal.is_room_member_join_suppressed(ROOM, "$join", USER)
    other = reopened.principal("another-principal")
    consumer = await other.load_or_create_ingestion_consumer(new_generation=uuid4())
    other_stream = uuid4()
    await other.bind_ingestion_stream(generation=consumer.generation, stream_id=other_stream)
    await _admit(other, other_stream, 1, _member("$join"))
    assert not await other.is_room_member_join_suppressed(ROOM, "$join", USER)
    for room_id, event_id, user_id in ((ROOM, "$missing", USER), ("!wrong", "$join", USER), (ROOM, "$join", "@wrong")):
        with pytest.raises(ValueError, match="source is missing or does not match"):
            await principal.is_room_member_join_suppressed(room_id, event_id, user_id)


@pytest.mark.asyncio
async def test_baseline_and_admission_roll_back_together(
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after baseline insertion must leave neither the marker nor batch acceptance."""
    stream = uuid4()
    principal = await principal_for(journal_store, stream)
    record = membership_hooks.record_baseline

    def interrupted(transaction: Transaction, principal_id: str, room_id: str, user_id: str) -> None:
        record(transaction, principal_id, room_id, user_id)
        message = "interrupted baseline transaction"
        raise RuntimeError(message)

    with monkeypatch.context() as failure:
        failure.setattr(membership_hooks, "record_baseline", interrupted)
        with pytest.raises(RuntimeError, match="interrupted baseline"):
            await _admit(principal, stream, 1, _member("$baseline", history=True))
    rows = await principal._backend.read(lambda transaction: transaction.fetchall("SELECT * FROM room_member_joins"))
    assert rows == ()
    await _admit(principal, stream, 1, _member("$baseline", history=True))
    await _admit(principal, stream, 2, _member("$profile"))
    assert await principal.is_room_member_join_suppressed(ROOM, "$profile", USER)
