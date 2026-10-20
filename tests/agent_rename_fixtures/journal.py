"""Event journal rows written through MindRoom's journal store: synced batches and handled turns."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from mindroom.event_journal import (
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
    PrincipalStore,
)
from mindroom.handled_turns import HandledTurnLedger, TurnRecord
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    from mindroom.history.types import HistoryScope


ROOM = "!room:x"


def membership_admission(
    previous: str | None,
    current: str,
    previous_epoch: int,
    epoch: int,
) -> IngestionRecordAdmission:
    """One own-membership transition in the room, as the sync stream reports it."""
    return IngestionRecordAdmission(
        IngestionRecordDisposition.ROOM_LIFECYCLE,
        source=DepartureSource.REPORTED,
        room_id=ROOM,
        previous_membership=previous,
        membership=current,
        previous_membership_epoch=previous_epoch,
        membership_epoch=epoch,
    )


def message_admission(event_id: str) -> IngestionRecordAdmission:
    """One message from alice in the room that the bot may answer."""
    event = InboundEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=None,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender="@alice:x",
        origin_server_ts=10,
        source={"event_id": event_id, "sender": "@alice:x"},
    )
    return IngestionRecordAdmission(IngestionRecordDisposition.SEMANTIC_EVENT, event=event)


async def bound_principal(store: EventJournalStore, principal_id: str, stream: UUID) -> PrincipalStore:
    """Bind ``stream`` to the principal, as a device's first start does."""
    principal = store.principal(principal_id)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=stream)
    return principal


def message_target(event_id: str) -> MessageTarget:
    """Return the room-level target of ``event_id``."""
    return MessageTarget.resolve(ROOM, None, event_id)


async def record_turn(store: EventJournalStore, agent: str, event_id: str, scope: HistoryScope) -> None:
    """Record ``agent``'s answer to ``event_id`` the way the bot does, through its handled-turn ledger."""
    ledger = HandledTurnLedger(agent, records=store.turn_records(agent))
    await ledger.load()
    await ledger.record_handled_turn(
        TurnRecord.create(
            [event_id],
            response_event_id=f"$answer-{agent}-{event_id}",
            response_owner=agent,
            history_scope=scope,
            conversation_target=message_target(event_id),
        ),
    )
