"""Membership fixtures admitted through the durable producer boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from nio.durable import RecordKind, SyncBatch, SyncRecord
from nio.durable.model import OwnMembership

from mindroom.event_journal import DepartureSource
from mindroom.matrix.durable_ingestion import validate_ingestion_batch

if TYPE_CHECKING:
    from mindroom.event_journal import PrincipalStore


async def admit_room_membership(
    principal: PrincipalStore,
    room_id: str,
    membership: Literal["join", "leave"],
    *,
    source: DepartureSource = DepartureSource.REPORTED,
) -> int:
    """Admit a membership transition and return its journal ownership epoch."""
    position = await principal.ingestion_membership_position(room_id)
    if position is None and membership == "leave":
        await admit_room_membership(principal, room_id, "join")
        position = await principal.ingestion_membership_position(room_id)
    if position is not None and position.membership == membership:
        return await principal.membership_epoch(room_id)

    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    if consumer.stream_id is None:
        consumer = await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=uuid4())
    assert consumer.stream_id is not None
    sequence = await principal._backend.read(
        lambda transaction: transaction.fetchone(
            "SELECT next_sequence FROM matrix_sync_consumers WHERE principal_id = ?",
            (principal._principal_id,),
        ),
    )
    assert sequence is not None
    previous_epoch = 0 if position is None else position.membership_epoch
    membership_source: Literal["reported", "local"] = (
        "local" if position is not None and source is DepartureSource.LOCAL else "reported"
    )
    record = SyncRecord(
        RecordKind.ROOM_LIFECYCLE,
        room_id,
        {},
        membership=OwnMembership(
            None if position is None else position.membership,
            membership,
            previous_epoch,
            previous_epoch + int(position is not None and position.membership == "join" and membership == "leave"),
            source=membership_source,
        ),
    )
    batch = SyncBatch(consumer.stream_id, int(sequence["next_sequence"]), (record,))
    await principal.admit_ingestion_batch(validate_ingestion_batch(batch, account_id="@fixture:example.org"))
    return await principal.membership_epoch(room_id)
