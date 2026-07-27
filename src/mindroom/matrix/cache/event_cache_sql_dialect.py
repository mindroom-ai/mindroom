"""Rendering differences between the two durable Matrix event-cache backends.

The backends run the same statements against schemas that differ only in table names, the name of
the scope column, the parameter marker, and a short list of expressions. This module holds those
differences so each statement can be written once, in ``event_cache_thread_statements``.

Scope of this shim, deliberately narrow: it renders SQL text and nothing else. It does not open
connections, run statements, or decide transaction boundaries. The things that genuinely differ
between the backends stay where they are and are *not* modelled here:

* the PostgreSQL advisory lock taken alongside the connection lock,
* SQLite's ``PRAGMA busy_timeout=0`` plus ``BEGIN IMMEDIATE`` model, under which reads take the
  write lock and a losing reader reports ``disabled_result`` rather than an empty result,
* the two schema-migration paths, which are per-namespace and version-gated on PostgreSQL and a
  destructive reset on SQLite.

Those are backend semantics. Smoothing them over here would turn a rendering shim into a
correctness surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

# Logical table names used by the shared statements. The physical name each one maps to is the
# only place a backend's table naming is written down.
EVENTS: Final = "events"
EVENT_EDITS: Final = "event_edits"
THREAD_EVENTS: Final = "thread_events"
THREAD_STATE: Final = "thread_state"
ROOM_STATE: Final = "room_state"


@dataclass(frozen=True)
class _ThreadEventUpsertShape:
    """How one backend writes ``write_seq`` on the thread-membership upsert.

    The two backends allocate write sequences differently and that difference reaches into the
    statement itself. SQLite has no sequence object, so ``allocate_write_sequences`` hands out
    values from a counter row and the statement binds them. PostgreSQL defaults the column from
    ``nextval`` and re-draws on conflict, so the statement never names a value.

    ``conflict_assignments`` also carries PostgreSQL's ``event_json = NULL``: its thread-membership
    table has a legacy payload column that SQLite's does not, and the upsert clears it.
    """

    insert_column: str
    insert_value: str
    conflict_assignments: str


@dataclass(frozen=True)
class SqlDialect:
    """One backend's rendering rules for the shared cache statements."""

    name: Literal["sqlite", "postgres"]
    scope_column: str
    tables: Mapping[str, str]
    named_parameter: str
    binary_collation: str
    thread_event_upsert: _ThreadEventUpsertShape

    def table(self, logical: str) -> str:
        """Return the physical table name for one logical table."""
        return self.tables[logical]

    def table_as(self, logical: str, alias: str | None = None) -> str:
        """Return a ``FROM``/``JOIN`` reference, aliased when the physical name differs."""
        physical = self.tables[logical]
        target = logical if alias is None else alias
        return physical if physical == target else f"{physical} AS {target}"

    def parameter(self, name: str) -> str:
        """Return the placeholder for one named bind parameter.

        Every shared statement binds by name. Positional binding is what let one statement pass
        the same value three times in a row and made the argument order load-bearing.
        """
        return self.named_parameter.format(name=name)

    def monotonic_max(self, column: str, incoming: str) -> str:
        """Return an expression advancing ``column`` to ``incoming`` only when that moves forward.

        ``incoming`` must not be NULL. PostgreSQL's ``GREATEST`` ignores NULL arguments while
        SQLite's scalar ``MAX`` returns NULL if any argument is NULL, so the two agree only when
        the incoming value is known present. Every call site passes a freshly read clock, so the
        precondition holds; a NULL there would silently erase a marker on SQLite alone, which no
        production monitoring covers.
        """
        if self.name == "postgres":
            return f"GREATEST({column}, {incoming})"
        return f"MAX(COALESCE({column}, {incoming}), {incoming})"


SQLITE_DIALECT: Final = SqlDialect(
    name="sqlite",
    scope_column="principal_id",
    tables={
        EVENTS: "events",
        EVENT_EDITS: "event_edits",
        THREAD_EVENTS: "thread_events",
        THREAD_STATE: "thread_cache_state",
        ROOM_STATE: "room_cache_state",
    },
    named_parameter=":{name}",
    # SQLite compares TEXT bytewise already, so the tie-break needs no override to agree with the
    # fold and with PostgreSQL's pinned ``COLLATE "C"``.
    binary_collation="",
    thread_event_upsert=_ThreadEventUpsertShape(
        insert_column=",\n            write_seq",
        insert_value=",\n            :write_seq",
        conflict_assignments="            write_seq = excluded.write_seq",
    ),
)

POSTGRES_DIALECT: Final = SqlDialect(
    name="postgres",
    scope_column="namespace",
    tables={
        EVENTS: "mindroom_event_cache_events",
        EVENT_EDITS: "mindroom_event_cache_event_edits",
        THREAD_EVENTS: "mindroom_event_cache_thread_events",
        THREAD_STATE: "mindroom_event_cache_thread_state",
        ROOM_STATE: "mindroom_event_cache_room_state",
    },
    named_parameter="%({name})s",
    # Load-bearing, not decoration: the tie-break has to agree with SQLite and with the fold, and
    # both compare event IDs by byte. Without it a glibc cluster ('a' < 'B') ships a different
    # surviving edit for two edits sharing a timestamp, so the same message renders differently per
    # backend. Invisible to CI, whose fixture pins a musl image where every locale behaves like C.
    binary_collation=' COLLATE "C"',
    thread_event_upsert=_ThreadEventUpsertShape(
        insert_column="",
        insert_value="",
        conflict_assignments=(
            "            event_json = NULL,\n            write_seq = nextval('mindroom_event_cache_write_seq')"
        ),
    ),
)
