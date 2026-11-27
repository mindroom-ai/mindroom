"""Journal fixtures for tests exercising semantic dispatch without a transport."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING, cast

from mindroom.event_journal import EventClass, EventKind, InboundEvent, RoomMembershipPosition, journal
from mindroom.event_journal.schema import SQLITE_DIALECT, render, schema_statements
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    import nio

    from mindroom.event_journal import PrincipalStore, RoomHistoryRecovery
    from mindroom.event_journal.backend import Transaction
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
    store = cast("PrincipalStore", dispatcher.store)
    await store.admit(
        _inbound_event(room.room_id, event, kind, event_class),
        _projected_event(room.room_id, event, kind, self_sender=room.own_user_id),
    )
    dispatcher.wake()


async def admit_room_event(store: PrincipalStore, room_id: str, event_id: str) -> None:
    """Record that sync delivered one event in a room, as it does before any redaction of it arrives."""
    await store.admit(
        InboundEvent(
            event_id=event_id,
            room_id=room_id,
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.CONTEXT_ONLY,
            sender="@user:example.org",
            origin_server_ts=0,
            source={},
        ),
    )


async def record_room_history_recovery(store: PrincipalStore, room_id: str) -> RoomHistoryRecovery | None:
    """Record that a room's history could not be proven continuous, serialized with membership fencing as admission is."""
    return await store._write(journal._record_room_history_recovery, room_id)


def _membership_position(transaction: Transaction, principal_id: str, room_id: str) -> RoomMembershipPosition:
    row = transaction.fetchone(
        "SELECT membership_epoch, departure_fenced FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    if row is None:
        return RoomMembershipPosition("leave", 0)
    return RoomMembershipPosition("leave" if row["departure_fenced"] else "join", row["membership_epoch"])


async def membership_position(store: PrincipalStore, room_id: str) -> RoomMembershipPosition:
    """Return the journal tenure that owns this room's events and deliveries, as the membership row records it."""
    return await store._read(_membership_position, room_id)


class PlanRecorder:
    """Transaction stub recording SQLite EXPLAIN QUERY PLAN output for each query executed against it."""

    def __init__(
        self,
        database: sqlite3.Connection,
        *,
        fetchone_result: Mapping[str, object] | None = None,
    ) -> None:
        self._database = database
        self._fetchone_result = fetchone_result
        self.plans: list[str] = []

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> tuple[Mapping[str, object], ...]:
        """Record the query plan for *sql* and return an empty result set."""
        rows = self._database.execute("EXPLAIN QUERY PLAN " + render(sql, SQLITE_DIALECT), tuple(params))
        self.plans.append(" | ".join(row[-1] for row in rows))
        return ()

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> Mapping[str, object] | None:
        """Record the query plan for *sql* and return *fetchone_result*."""
        self.fetchall(sql, params)
        return self._fetchone_result

    def record(self, fn: Callable[[Transaction], object]) -> list[str]:
        """Run *fn* against this recorder and return only the plans it recorded."""
        start = len(self.plans)
        fn(cast("Transaction", self))
        return self.plans[start:]


@contextmanager
def sqlite_plan_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open and close an empty un-ANALYZEd SQLite journal schema for query-plan inspection."""
    with closing(sqlite3.connect(path)) as database:
        for statement in schema_statements(SQLITE_DIALECT):
            database.execute(statement)
        database.commit()
        yield database
