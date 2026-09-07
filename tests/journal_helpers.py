"""Journal fixtures for tests exercising semantic dispatch without a transport."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.matrix.journal_ingress import _inbound_event, _projected_event

if TYPE_CHECKING:
    import nio

    from mindroom.event_journal import AdmissionView, EventClass, EventKind
    from mindroom.journal_dispatch import JournalDispatcher


async def admit_dispatch_event(
    dispatcher: JournalDispatcher,
    room: nio.MatrixRoom,
    event: nio.Event,
    kind: EventKind,
    event_class: EventClass,
) -> None:
    """Persist one fixture through the journal and wake its semantic dispatcher."""
    previous_room_for_id = dispatcher.room_for_id
    dispatcher.room_for_id = lambda room_id: room if room_id == room.room_id else previous_room_for_id(room_id)
    store = cast("AdmissionView", dispatcher.store)
    await store.admit(
        _inbound_event(room.room_id, event, kind, event_class),
        _projected_event(room.room_id, event, kind, self_sender=room.own_user_id),
    )
    dispatcher.wake()
