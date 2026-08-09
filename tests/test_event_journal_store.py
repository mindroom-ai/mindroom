"""Backend-neutral contract for the event journal, projection, and outbox.

Every test here runs on SQLite and on PostgreSQL. A rule that holds on only one
backend is a rule MindRoom does not actually have.

The exceptions are the two contention tests, which ask what two connections do
to the same row. SQLite has no second connection *in this process* to ask that
of -- the backend is one process behind one writer -- so running them there
would prove only that the fixture took turns. Both take the PostgreSQL-only
``rival_stores`` fixture, which is where that is spelled out.

SQLite does get a second connection, and not from a fixture: ``mindroom threads
export`` is another process with another writer on the same file. That is the
one thing here no object can stage, so ``TestCrossProcessWriters`` spawns a real
interpreter to be it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
import pytest_asyncio

from mindroom.event_journal import (
    AdmissionResult,
    ConversationCursor,
    DeliveryAcknowledgement,
    DeliveryStage,
    DepartureObservation,
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    HistoryRecoveryOutcome,
    InboundEvent,
    ProjectedEvent,
    TerminalTurnWrite,
    delivery_transaction_id,
)
from mindroom.event_journal.offloading import settled
from mindroom.event_journal.reads import _CONVERSATION_CURSOR_CLAUSE
from mindroom.event_journal.schema import (
    POSTGRES_DIALECT,
    SQLITE_DIALECT,
    render,
    schema_statements,
)
from mindroom.event_journal.sqlite_backend import SqliteBackend
from tests.conftest import postgres_journal_schema_url

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.event_journal import OutboxDelivery, PrincipalStore, RefreshRequest, TurnRecordStore
    from mindroom.event_journal.backend import Backend, Operation, Transaction
    from mindroom.history_recovery import RoomHistoryRecovery

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
# The device an approval card is claimed from. Stored with the row because a
# Matrix transaction ID only deduplicates against the device that used it.
DEVICE = "SENDINGDEVICE"

# How long a claimer that is already inside its transaction waits for a second
# claimer to reach its own first statement. Generous next to the sub-millisecond
# round trip it covers, and only ever paid when the second claimer is blocked --
# which is the fix working.
_CONTENDED_CLAIM_WAIT_SECONDS = 0.5

# How long an await that must not finish yet is given to finish anyway before
# the test concludes it cannot. Paid in full only when the backend is correct,
# which is why it is small: an implementation that releases early gets there in
# microseconds, so no plausible machine turns a real failure into a pass.
_MUST_NOT_FINISH_SECONDS = 0.25
# How long a worker thread waits to be let go. Never reached unless the test is
# already failing, so it only bounds the damage.
_WORKER_WAIT_SECONDS = 5.0
# How long a released holder process is given to exit. It is already unblocked
# by the time this is waited on, so reaching it means that process is wedged.
_HOLDER_EXIT_SECONDS = 30.0
# The pause an operation leaves between the statements it holds a connection
# with. Short next to anything it is racing, long enough to cost no core.
_STATEMENT_GAP_SECONDS = 0.001
# How long every write outstanding at a close is given to be answered once that
# close has returned. Reached only when one was abandoned, so it bounds a
# failure rather than waiting one out.
_SETTLEMENT_WAIT_SECONDS = 5.0
# More writes at once than the writer queue used to be bounded at, because the
# bound is what decided how many a close could reach. A close that answers only
# what it dequeues wakes one parked producer per entry, so any count at or
# below the old bound of 512 passes against the bug that stranded the rest.
_WRITES_OUTNUMBERING_THE_OLD_QUEUE_BOUND = 1_100
# One transaction this small is independent of the one-million-message export
# ceiling. The tests feed one more event than the bound so moving the complete
# loop back into one write is a directly observed failure, not a timing
# inference.
_HYDRATION_TRANSACTION_EVENT_LIMIT = 256
_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS = _HYDRATION_TRANSACTION_EVENT_LIMIT + 1

# `PRAGMA synchronous` reports the mode it is set to as an integer.
_SQLITE_SYNCHRONOUS_NORMAL = 1
_SQLITE_SYNCHRONOUS_FULL = 2

# A statement against a real table, so the connection a test's operation holds
# is genuinely in use rather than merely borrowed.
_INSERT_MEMBERSHIP = "INSERT INTO room_membership (principal_id, room_id, membership_epoch) VALUES (?, ?, ?)"


def _hold_the_connection(
    transaction: Transaction,
    running: threading.Event,
    release: threading.Event,
) -> None:
    """Keep real statements on a real connection until the test lets go.

    Statements rather than a parked thread, because a connection taken away
    from an operation that is between statements only raises, while one taken
    away mid-statement is what ends the process. Spaced rather than in a tight
    loop: pinning a core for the whole grace window destabilizes unrelated
    timing-sensitive tests sharing the machine, and the connection is
    continuously in use either way.
    """
    transaction.fetchall("SELECT 1 AS one")
    running.set()
    while not release.wait(_STATEMENT_GAP_SECONDS):
        transaction.fetchall("SELECT 1 AS one")


def _synchronous_mode(transaction: Transaction) -> int:
    """Return the durability the connection running this commits at."""
    row = transaction.fetchone("PRAGMA synchronous")
    assert row is not None
    return int(row["synchronous"])


def _journal_mode(transaction: Transaction) -> str:
    """Return the rollback journal the connection running this uses."""
    row = transaction.fetchone("PRAGMA journal_mode")
    assert row is not None
    return str(row["journal_mode"])


async def _release_writes_the_store_abandoned(
    backend: SqliteBackend,
    abandoned: set[asyncio.Task[object]],
) -> None:
    """End writes a close left waiting, so a failure reads as one.

    Empty on a healthy close, and reached only when the test using it is
    already failing. It exists because an abandoned write cannot be cancelled
    out of the way: ``settled`` deliberately re-enters its wait rather than
    unwinding while the future it holds is still outstanding, so cancelling the
    task does nothing and the only thing that ends it is settling that future.
    Left alone, one stranded producer turns a clear assertion into a session
    that hangs until the suite timeout fires.
    """
    for task in abandoned:
        task.cancel()
    while any(not task.done() for task in abandoned):
        queue = backend._queue
        while queue is not None and not queue.empty():
            queue.get_nowait().future.cancel()
        await asyncio.sleep(0)


async def _finished_within_grace(work: asyncio.Task[object]) -> bool:
    """Give ``work`` every chance to finish and report whether it did.

    Shielded, so waiting cannot itself cancel the thing being watched, and the
    outcome is read off the task rather than off what the wait raised.
    """
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(asyncio.shield(work), _MUST_NOT_FINISH_SECONDS)
    return work.done()


async def _membership_principals(store: EventJournalStore) -> list[str]:
    """Return the membership rows a backend test wrote, straight off the backend."""
    rows = await store.backend.read(
        lambda transaction: transaction.fetchall("SELECT principal_id FROM room_membership"),
    )
    return sorted(str(row["principal_id"]) for row in rows)


async def _held_edit_bodies(store: EventJournalStore) -> list[str]:
    """Return every replacement body still held in ``unresolved_edits``.

    Read straight off the backend because that is the whole point: a held edit
    for a target that never arrived has no reader above the table, so the only
    way to ask whether its text is still on this host is to look.
    """
    rows = await store.backend.read(
        lambda transaction: transaction.fetchall("SELECT content_json FROM unresolved_edits"),
    )
    return [str(row["content_json"]) for row in rows]


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def text(body: str) -> dict[str, object]:
    """Return a plain text message body."""
    return {"msgtype": "m.text", "body": body}


def edit(target: str, body: str) -> dict[str, object]:
    """Return an edit of ``target`` installing ``body``."""
    return {
        "msgtype": "m.text",
        "body": f"* {body}",
        "m.new_content": {"msgtype": "m.text", "body": body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": target},
    }


class _PausingTransaction:
    """One real transaction that runs a hook after its first matching statement.

    With no matcher, every statement matches and this retains the ordinary
    pause-after-first-statement behavior. A matcher opens a causal window at a
    specific SQL boundary, so a race test cannot silently pause on an earlier
    lock and claim it exercised a later one.
    """

    def __init__(
        self,
        inner: Transaction,
        hook: Callable[[], object],
        statement_matches: Callable[[str], bool] | None = None,
    ) -> None:
        self._inner = inner
        self._hook = hook
        self._statement_matches = statement_matches
        self._paused = False

    def _pause(self, sql: str) -> None:
        if not self._paused and (self._statement_matches is None or self._statement_matches(sql)):
            self._paused = True
            self._hook()

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        """Run one statement, then pause."""
        self._inner.execute(sql, params)
        self._pause(sql)

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> Mapping[str, object] | None:
        """Run one query, then pause."""
        row = self._inner.fetchone(sql, params)
        self._pause(sql)
        return row

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> tuple[Mapping[str, object], ...]:
        """Run one query, then pause."""
        rows = self._inner.fetchall(sql, params)
        self._pause(sql)
        return rows


@dataclass(frozen=True, slots=True)
class _PausingBackend:
    """A real backend whose writes pause partway through their transaction.

    The races the outbox guards against need one caller to still be inside its
    transaction when another starts, and the public API offers nowhere to stand
    between two statements of the same operation. Wrapping the transaction opens
    that window without substituting anything for the SQL under test.
    """

    inner: Backend
    after_first_statement: Callable[[], object]
    statement_matches: Callable[[str], bool] | None = None

    async def write[T](self, operation: Operation[T]) -> T:
        """Run one write, pausing inside its transaction."""
        return await self.inner.write(
            lambda transaction: operation(
                _PausingTransaction(
                    transaction,
                    self.after_first_statement,
                    self.statement_matches,
                ),
            ),
        )

    async def read[T](self, operation: Operation[T]) -> T:
        """Run one read, unpaused."""
        return await self.inner.read(operation)

    async def close(self) -> None:
        """Close the wrapped backend."""
        await self.inner.close()


@dataclass(slots=True)
class _HydrationWriteShape:
    """The hydration work one real backend transaction was asked to commit."""

    membership_claims: int = 0
    projected_messages: int = 0
    hydration_markers: int = 0
    recovery_settlements: int = 0
    completeness_retractions: int = 0


class _HydrationWriteTransaction:
    """Profile hydration statements while executing them on a real transaction."""

    def __init__(self, inner: Transaction) -> None:
        self._inner = inner
        self.shape = _HydrationWriteShape()

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        """Run one statement and record the durable hydration effect it requests."""
        if "INSERT INTO visible_messages" in sql:
            self.shape.projected_messages += 1
        if "INSERT INTO conversation_hydration" in sql:
            self.shape.hydration_markers += 1
        if "UPDATE room_history_recovery SET state = ?" in sql and params[0] == "repaired":
            self.shape.recovery_settlements += 1
        if "UPDATE conversation_hydration SET complete = 0" in sql:
            self.shape.completeness_retractions += 1
        self._inner.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> Mapping[str, object] | None:
        """Run one query and record a materialized membership claim."""
        if "INSERT INTO room_membership" in sql:
            self.shape.membership_claims += 1
        return self._inner.fetchone(sql, params)

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> tuple[Mapping[str, object], ...]:
        """Run one query without changing the write profile."""
        return self._inner.fetchall(sql, params)


@dataclass(slots=True)
class _ObservedHydrationBackend:
    """Observe and control transaction boundaries around the real backend."""

    inner: Backend
    before_write: Callable[[int], Awaitable[None]] | None = None
    after_write: Callable[[int], Awaitable[None]] | None = None
    write_shapes: list[_HydrationWriteShape] = field(default_factory=list)
    writes_started: int = 0

    async def write[T](self, operation: Operation[T]) -> T:
        """Run one profiled write, with optional boundaries for failure races."""
        self.writes_started += 1
        write_number = self.writes_started
        if self.before_write is not None:
            await self.before_write(write_number)
        observed: _HydrationWriteTransaction | None = None

        def profile(transaction: Transaction) -> T:
            nonlocal observed
            observed = _HydrationWriteTransaction(transaction)
            return operation(observed)

        result = await self.inner.write(profile)
        assert observed is not None
        self.write_shapes.append(observed.shape)
        if self.after_write is not None:
            await self.after_write(write_number)
        return result

    async def read[T](self, operation: Operation[T]) -> T:
        """Run reads unchanged on the real backend."""
        return await self.inner.read(operation)

    async def close(self) -> None:
        """Close the wrapped backend."""
        await self.inner.close()


def sidecar(content: dict[str, object]) -> dict[str, object]:
    """Return one edit whose real text lives in an attached file."""
    new_content = dict(cast("dict[str, object]", content["m.new_content"]))
    new_content["io.mindroom.long_text"] = {"version": 2, "encoding": "matrix_event_content_json"}
    new_content["url"] = "mxc://example.org/body"
    return content | {"m.new_content": new_content}


def message(
    event_id: str,
    *,
    sender: str = ALICE,
    ts: int = 1_000,
    content: Mapping[str, object] | None = None,
    thread_id: str | None = None,
    redacts: str | None = None,
    kind: EventKind = EventKind.MESSAGE,
    event_class: EventClass = EventClass.ACTIONABLE,
) -> tuple[InboundEvent, ProjectedEvent]:
    """Return the admission and projection views of one event."""
    body = dict(content) if content is not None else text(event_id)
    inbound = InboundEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        kind=kind,
        event_class=event_class,
        sender=sender,
        origin_server_ts=ts,
        source={"event_id": event_id, "content": body},
    )
    projected = ProjectedEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        sender=sender,
        origin_server_ts=ts,
        content=body,
        replaces_event_id=None,
        redacts_event_id=redacts,
    )
    return inbound, projected


async def admit(store: PrincipalStore, *args: object, **kwargs: object) -> AdmissionResult:
    """Admit one event built by ``message``."""
    inbound, projected = message(*args, **kwargs)  # type: ignore[arg-type]
    return await store.admit(inbound, projected)


async def bodies(store: PrincipalStore, *, thread_id: str | None = None, limit: int = 50) -> list[str]:
    """Return the visible bodies of one conversation, oldest first."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=limit)
    return [str(m.content["body"]) for m in page.messages]


def hydration_messages(count: int) -> tuple[ProjectedEvent, ...]:
    """Return enough distinct projected messages to exercise hydration batching."""
    return tuple(message(f"$hydrated-{index:04d}", ts=1_000 + index)[1] for index in range(count))


async def refreshes(
    store: PrincipalStore,
    *,
    thread_id: str | None = None,
    limit: int = 50,
) -> tuple[RefreshRequest, ...]:
    """Return the refetch debts one conversation read reports, the way production learns them."""
    page = await store.read_conversation(room_id=ROOM, thread_id=thread_id, limit=limit)
    return page.refresh_pending


class TestPrincipalIsolation:
    """One database, many bots, no way to reach across."""

    async def test_bound_views_cannot_see_each_other(self, journal_store: EventJournalStore) -> None:
        """Bound views cannot see each other."""
        first = journal_store.principal("agent@one")
        second = journal_store.principal("agent@two")

        await admit(first, "$only-mine")

        assert await bodies(first) == ["$only-mine"]
        assert await bodies(second) == []
        assert await second.load_event("$only-mine") is None
        assert [event.event_id for event in await first.pending()] == ["$only-mine"]
        assert await second.pending() == ()

    async def test_settling_is_bound_to_its_principal(self, journal_store: EventJournalStore) -> None:
        """Settling is bound to its principal."""
        first = journal_store.principal("agent@one")
        second = journal_store.principal("agent@two")
        await admit(first, "$shared-id")
        await admit(second, "$shared-id")

        await second.settle("$shared-id")

        assert await first.is_pending("$shared-id")
        assert not await second.is_pending("$shared-id")


class TestAdmission:
    """The journal decides exactly once what MindRoom accepted."""

    async def test_admitting_twice_creates_one_pending_event(self, alice: PrincipalStore) -> None:
        """Admitting twice creates one pending event."""
        assert await admit(alice, "$one") is AdmissionResult.ADMITTED
        assert await admit(alice, "$one") is AdmissionResult.DUPLICATE

        assert [event.event_id for event in await alice.pending()] == ["$one"]

    async def test_a_second_disagreeing_payload_for_one_event_id_never_projects(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One event ID reaches the projection at most once, whatever it carries.

        This is why the projection needs no tiebreak for two payloads claiming
        one event ID, and the visible-message scan does. That scan merges a
        replacement bundled under `unsigned` with the standalone copy of the
        same event from the same page, so it genuinely holds two payloads for
        one ID and has to order them by content rather than by arrival.

        Nothing writing here can produce that pair. Admission conflicts on the
        `(principal_id, event_id)` primary key and returns before `project()`
        is called at all, so the losing payload is not ranked against the
        stored revision -- it never reaches the comparison. Unifying the two
        rules would move a JSON dump of every replacement onto this path, which
        streaming walks tens to hundreds of times per response, to decide a
        case it cannot observe.

        The second payload is given a later timestamp deliberately. A real
        second copy of one event carries the server's own `origin_server_ts`
        and would tie, and a tie is refused by the ordering rule as well as by
        the key -- so a tied payload would prove only that one of the two
        defences held. A payload that would win on the ordering rule isolates
        the key as the thing that stops it.
        """
        await admit(alice, "$original")
        first = await admit(alice, "$edit", ts=2_000, content=edit("$original", "first payload"))
        assert first is AdmissionResult.ADMITTED

        second = await admit(alice, "$edit", ts=3_000, content=edit("$original", "second payload"))

        assert second is AdmissionResult.DUPLICATE
        assert await bodies(alice) == ["first payload"]

    async def test_pending_replays_in_receipt_order(self, alice: PrincipalStore) -> None:
        """Replay order is admission order, not the senders' clocks."""
        await admit(alice, "$late-clock", ts=9_000)
        await admit(alice, "$early-clock", ts=1_000)

        assert [event.event_id for event in await alice.pending()] == ["$late-clock", "$early-clock"]

    async def test_context_only_events_never_become_pending(self, alice: PrincipalStore) -> None:
        """Cold history populates the conversation without starting work."""
        await admit(alice, "$history", event_class=EventClass.CONTEXT_ONLY)

        assert await alice.pending() == ()
        assert await bodies(alice) == ["$history"]

    async def test_settled_events_stay_out_of_replay(self, alice: PrincipalStore) -> None:
        """Settled events stay out of replay."""
        await admit(alice, "$one")
        await alice.settle("$one")

        assert await alice.pending() == ()
        assert await admit(alice, "$one") is AdmissionResult.DUPLICATE
        assert await alice.pending() == ()

    async def test_settlement_releases_the_replay_payload(self, alice: PrincipalStore) -> None:
        """The row outlives its payload: it is the proof, not the work item."""
        await admit(alice, "$one")
        await alice.settle("$one")

        settled = await alice.load_event("$one")
        assert settled is not None
        assert settled.source == {}

    async def test_replay_payload_survives_until_settlement(self, alice: PrincipalStore) -> None:
        """Replay payload survives until settlement."""
        await admit(alice, "$one")

        pending = await alice.pending()
        assert pending[0].source == {"event_id": "$one", "content": text("$one")}


class TestEditReduction:
    """One row per logical message, whatever order the events arrive in."""

    async def test_edit_replaces_the_visible_body(self, alice: PrincipalStore) -> None:
        """Edit replaces the visible body."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))

        assert await bodies(alice) == ["second"]

    async def test_older_edit_arriving_late_does_not_win(self, alice: PrincipalStore) -> None:
        """Older edit arriving late does not win."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$new", ts=3_000, content=edit("$original", "newest"))
        await admit(alice, "$old", ts=2_000, content=edit("$original", "stale"))

        assert await bodies(alice) == ["newest"]

    async def test_same_timestamp_edits_resolve_by_event_id(self, alice: PrincipalStore) -> None:
        """Timestamps tie; the total order has to come from somewhere."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$aaa", ts=2_000, content=edit("$original", "from-aaa"))
        await admit(alice, "$zzz", ts=2_000, content=edit("$original", "from-zzz"))

        assert await bodies(alice) == ["from-zzz"]

    async def test_same_timestamp_edits_resolve_by_event_id_either_order(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Same timestamp edits resolve by event id either order."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$zzz", ts=2_000, content=edit("$original", "from-zzz"))
        await admit(alice, "$aaa", ts=2_000, content=edit("$original", "from-aaa"))

        assert await bodies(alice) == ["from-zzz"]

    async def test_edit_before_original_applies_when_the_original_lands(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Edit before original applies when the original lands."""
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))
        assert await bodies(alice) == []

        await admit(alice, "$original", content=text("first"))
        assert await bodies(alice) == ["second"]

    async def test_a_stranger_cannot_evict_the_authors_pending_edit(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The unresolved-edit key includes the sender, and this is why.

        Without it, anyone could send an edit for a message that has not
        arrived yet and displace the real author's edit before it could apply.
        """
        await admit(alice, "$alice-edit", sender=ALICE, ts=2_000, content=edit("$original", "authored"))
        await admit(alice, "$bob-edit", sender=BOB, ts=9_000, content=edit("$original", "forged"))

        await admit(alice, "$original", sender=ALICE, content=text("first"))

        assert await bodies(alice) == ["authored"]

    async def test_an_edit_from_another_sender_never_applies(self, alice: PrincipalStore) -> None:
        """An edit from another sender never applies."""
        await admit(alice, "$original", sender=ALICE, content=text("first"))
        await admit(alice, "$forged", sender=BOB, ts=9_000, content=edit("$original", "forged"))

        assert await bodies(alice) == ["first"]

    async def test_only_the_latest_unresolved_edit_is_kept(self, alice: PrincipalStore) -> None:
        """Only the latest unresolved edit is kept."""
        await admit(alice, "$e1", ts=2_000, content=edit("$original", "one"))
        await admit(alice, "$e2", ts=3_000, content=edit("$original", "two"))
        await admit(alice, "$e3", ts=2_500, content=edit("$original", "middle"))

        await admit(alice, "$original", content=text("first"))

        assert await bodies(alice) == ["two"]

    @pytest.mark.parametrize("edit_count", [1, 5, 25])
    async def test_edit_churn_leaves_one_row_and_no_history(
        self,
        alice: PrincipalStore,
        edit_count: int,
    ) -> None:
        """Streaming rewrites the same row; intermediate bodies are not stored."""
        await admit(alice, "$original", content=text("chunk 0"))
        for index in range(1, edit_count + 1):
            await admit(
                alice,
                f"$edit-{index:04d}",
                ts=1_000 + index,
                content=edit("$original", f"chunk {index}"),
            )

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=100)
        assert len(page.messages) == 1
        assert page.messages[0].content["body"] == f"chunk {edit_count}"
        assert page.messages[0].logical_event_id == "$original"


class TestRedaction:
    """Deleted content stops being readable in the transaction that admits it."""

    async def test_redacting_the_original_removes_the_message(self, alice: PrincipalStore) -> None:
        """Redacting the original removes the message."""
        await admit(alice, "$original", content=text("secret"))
        await admit(alice, "$redaction", ts=2_000, redacts="$original", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()

    async def test_a_redacted_original_cannot_be_resurrected(self, alice: PrincipalStore) -> None:
        """Backfill really does deliver a redaction before what it redacts."""
        await admit(alice, "$redaction", ts=2_000, redacts="$original", kind=EventKind.REDACTION)
        await admit(alice, "$original", content=text("secret"))

        assert await bodies(alice) == []

    async def test_a_redacted_edit_cannot_be_resurrected(self, alice: PrincipalStore) -> None:
        """A redacted edit cannot be resurrected."""
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$original", content=text("first"))

        assert await bodies(alice) == ["first"]

    async def test_redacting_a_target_that_never_arrived_drops_its_held_edit(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """The replacement text must not outlive the redaction of what it revises.

        An edit that arrives before its target is parked in ``unresolved_edits``
        with its body, and only the target's arrival collected it. Redacting a
        target that never arrived left the row untouched: the target never
        lands afterwards either, because ``project`` turns it away at the
        tombstone, so nothing was left to collect it for the rest of the
        membership epoch.

        The body is what makes that matter. Settlement blanks the journal's own
        ``source_json``, so this row is the last copy of the replacement text on
        the host -- and `docs/architecture/matrix-event-journal-security.md`
        states the row is deleted the moment its target lands *or is redacted*.
        """
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "secret replacement"))
        assert [body for body in await _held_edit_bodies(journal_store) if "secret replacement" in body]

        await admit(alice, "$redaction", ts=3_000, redacts="$original", kind=EventKind.REDACTION)

        assert await _held_edit_bodies(journal_store) == []

    async def test_a_held_edit_never_coexists_with_a_visible_target(
        self,
        alice: PrincipalStore,
        journal_store: EventJournalStore,
    ) -> None:
        """Why the delete had to move rather than be duplicated.

        An edit is only held when its target has no visible row, and the one
        statement that makes a row visible applies and drops the held edits for
        it in the same transaction. So the two states are mutually exclusive,
        and a delete guarded on the target being visible could only ever match
        nothing -- which is how the guarded one survived without any test
        noticing it did no work.
        """
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$forged", sender=BOB, ts=2_000, content=edit("$original", "forged"))

        assert await _held_edit_bodies(journal_store) == []
        assert await bodies(alice) == ["first"]

    async def test_redacting_a_superseded_edit_changes_nothing(self, alice: PrincipalStore) -> None:
        """Redacting a superseded edit changes nothing."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit1", ts=2_000, content=edit("$original", "second"))
        await admit(alice, "$edit2", ts=3_000, content=edit("$original", "third"))

        await admit(alice, "$redaction", ts=4_000, redacts="$edit1", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert [m.content["body"] for m in page.messages] == ["third"]
        assert page.refresh_pending == ()

    async def test_redacting_the_visible_edit_hides_it_and_asks_for_a_refetch(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Redacting the visible edit hides it and asks for a refetch."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "second"))

        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert [request.logical_event_id for request in page.refresh_pending] == ["$original"]

    async def test_no_read_of_any_kind_returns_the_redacted_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The gate is the cleared body, not the caller's willingness to wait."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        cursor_page = await alice.read_conversation(
            room_id=ROOM,
            thread_id=None,
            limit=50,
            before=ConversationCursor(created_ts=99_999, logical_event_id="$zzzzz"),
        )
        tiny_page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=1)

        for read in (page, cursor_page, tiny_page):
            assert all(m.content["body"] != "deleted" for m in read.messages)

    async def test_the_refresh_token_survives_a_restart(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A pending refetch is durable, so a crash cannot un-hide the content."""
        store = journal_store.principal("agent@alice")
        await admit(store, "$original", content=text("first"))
        await admit(store, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(store, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        reopened = journal_store.principal("agent@alice")
        page = await reopened.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert len(page.refresh_pending) == 1

    async def test_a_failed_refetch_keeps_the_message_hidden(self, alice: PrincipalStore) -> None:
        """A failed refetch keeps the message hidden."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)

        requests = await refreshes(alice)
        assert len(requests) == 1

        still_pending = await refreshes(alice)
        assert still_pending == requests

    async def test_a_thread_read_can_repair_its_own_root(self, alice: PrincipalStore) -> None:
        """A read that can see a message must be able to repair it.

        The root belongs to the room conversation, so a thread read merges it
        in. The refetch debt it owes has to be merged in by that same read, or a
        strict thread read raises forever: it reports the root as needing a
        refetch that nothing will ever be asked to perform.
        """
        await admit(alice, "$root", content=text("first"))
        await admit(alice, "$reply", ts=2_000, thread_id="$root")
        await admit(alice, "$edit", ts=3_000, content=edit("$root", "deleted"))
        await admit(alice, "$redaction", ts=4_000, redacts="$edit", kind=EventKind.REDACTION)

        requests = await refreshes(alice, thread_id="$root")

        assert [request.logical_event_id for request in requests] == ["$root"]

    async def test_a_successful_refetch_installs_the_server_revision(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A successful refetch installs the server revision."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        request = (await refreshes(alice))[0]

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$original",
            revision_ts=1_000,
            content=text("first"),
        )

        assert installed
        assert await bodies(alice) == ["first"]
        assert await refreshes(alice) == ()

    async def test_a_refetch_cannot_install_a_revision_that_was_itself_redacted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A redaction the token cannot see must still stop the install.

        Redacting a revision that is not the one on screen matches no visible
        row, so it correctly moves no refresh token -- but it does record a
        tombstone. A refetch already in flight, having picked that very revision
        off the server, still matches the token it was issued with. Without the
        tombstone check it would install a body the sender deleted, and nothing
        later disturbs it: hydration does not re-run under the same membership,
        so the deleted text would reach every prompt, summary and export of the
        room from then on.
        """
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$e1", ts=2_000, content=edit("$original", "first edit"))
        await admit(alice, "$e2", ts=3_000, content=edit("$original", "second edit"))
        await admit(alice, "$red2", ts=4_000, redacts="$e2", kind=EventKind.REDACTION)
        request = (await refreshes(alice))[0]

        # Redacting the superseded revision the refetch happens to be carrying.
        await admit(alice, "$red1", ts=5_000, redacts="$e1", kind=EventKind.REDACTION)

        installed = await alice.install_refetched_revision(
            request,
            revision_event_id="$e1",
            revision_ts=2_000,
            content=text("first edit"),
        )

        assert not installed
        assert "first edit" not in await bodies(alice)

    async def test_a_newer_edit_beats_an_in_flight_refetch(self, alice: PrincipalStore) -> None:
        """The refetch read the server before the newer edit existed."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        stale_request = (await refreshes(alice))[0]

        await admit(alice, "$newer", ts=4_000, content=edit("$original", "newest"))

        installed = await alice.install_refetched_revision(
            stale_request,
            revision_event_id="$original",
            revision_ts=1_000,
            content=text("first"),
        )

        assert not installed
        assert await bodies(alice) == ["newest"]

    async def test_a_refetch_can_remove_a_message_the_server_lost(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A refetch can remove a message the server lost."""
        await admit(alice, "$original", content=text("first"))
        await admit(alice, "$edit", ts=2_000, content=edit("$original", "deleted"))
        await admit(alice, "$redaction", ts=3_000, redacts="$edit", kind=EventKind.REDACTION)
        request = (await refreshes(alice))[0]

        assert await alice.drop_refetched_message(request)
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        assert page.messages == ()
        assert page.refresh_pending == ()


async def corrupt(store: PrincipalStore, *event_ids: str) -> None:
    """Make some admitted rows' replay payloads undecodable, in one transaction."""
    placeholders = ", ".join("?" for _ in event_ids)
    await store._backend.write(
        lambda transaction: transaction.execute(
            f"UPDATE journal_events SET source_json = ? WHERE event_id IN ({placeholders})",  # noqa: S608
            ("{", *event_ids),
        ),
    )


class TestUnreadableRowsDoNotEndTheBacklog:
    """A short page of pending work has to mean one thing, or paging is a lie."""

    async def test_a_corrupt_row_shortens_its_page_without_ending_the_backlog(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A caller paging on the result length would stop here and never resume.

        Dropping the row is right -- nothing ran, so nothing may claim it did --
        but a page that comes back short for that reason is indistinguishable
        from the end of the backlog *by its length*. Everything behind the
        corrupt row would then be durable work no scan looks at again, so the
        page has to say it stopped short and where to resume.
        """
        for index in range(5):
            await admit(alice, f"$m{index}", ts=1_000 + index)
        await corrupt(alice, "$m1")

        page = await alice.pending(limit=4)

        assert [event.event_id for event in page] == ["$m0", "$m2", "$m3"]
        assert page.unreadable_rows == 1
        assert not page.reached_end
        assert [event.event_id for event in await alice.pending(limit=4, after_receipt_order=page.resume_after)] == [
            "$m4",
        ]

    async def test_a_page_of_nothing_but_corrupt_rows_still_carries_a_cursor(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the scan stalls: no events back, and no cursor to move past."""
        for index in range(3):
            await admit(alice, f"$m{index}", ts=1_000 + index)
        await corrupt(alice, "$m0", "$m1")

        page = await alice.pending(limit=2)

        assert page == ()
        assert page.unreadable_rows == 2
        assert not page.reached_end
        assert [event.event_id for event in await alice.pending(limit=2, after_receipt_order=page.resume_after)] == [
            "$m2",
        ]

    async def test_a_short_page_still_means_the_end_of_the_backlog(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The signal the caller paginates on has to keep working."""
        for index in range(3):
            await admit(alice, f"$m{index}", ts=1_000 + index)

        assert len(await alice.pending(limit=10)) == 3
        assert await alice.pending(limit=10, after_receipt_order=1_000) == ()


class TestAPageIsBoundedByTheRowsItReads:
    """A page's limit counts rows the query read, not events it managed to decode."""

    async def test_a_page_stops_at_its_row_limit_with_readable_work_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The whole bound is this: one query's worth of rows, then report.

        Going back for more rows because too few decoded is what made the read
        unbounded, and it is invisible unless the events behind the corruption
        are readable -- a page that returns them read more rows than it was
        given a limit of.
        """
        for index in range(8):
            await admit(alice, f"$m{index}", ts=1_000 + index)
        await corrupt(alice, "$m0", "$m1")

        page = await alice.pending(limit=4)

        assert [event.event_id for event in page] == ["$m2", "$m3"]
        assert page.unreadable_rows == 2
        assert not page.reached_end
        # Four rows read, so the next pass starts on the fifth and nothing in
        # between is skipped or repeated.
        assert [event.event_id for event in await alice.pending(limit=4, after_receipt_order=page.resume_after)] == [
            "$m4",
            "$m5",
            "$m6",
            "$m7",
        ]

    async def test_a_corrupt_prefix_does_not_cost_one_pass_the_whole_backlog(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A bounded read that keeps querying until something decodes is not bounded.

        The rows are read inside one transaction and every one of them is
        logged, so a corrupt prefix long enough turns each wake of the pending
        scan into a scan of the entire pending table. The pass has to be able
        to stop part way and say so.
        """
        for index in range(12):
            await admit(alice, f"$m{index:02d}", ts=1_000 + index)
        await corrupt(alice, *(f"$m{index:02d}" for index in range(11)))

        page = await alice.pending(limit=4)

        assert page == ()
        assert page.unreadable_rows == 4
        assert not page.reached_end
        assert page.resume_after is not None

    async def test_a_pass_that_ran_out_of_budget_resumes_past_what_it_read(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Bounding the pass may not cost the events behind the corruption.

        The resume point is the last row looked at rather than the last event
        returned, which is the only version of it that exists when a page
        decodes nothing at all.
        """
        for index in range(12):
            await admit(alice, f"$m{index:02d}", ts=1_000 + index)
        await corrupt(alice, *(f"$m{index:02d}" for index in range(11)))

        seen: list[str] = []
        cursor: int | None = None
        for _ in range(12):
            page = await alice.pending(limit=4, after_receipt_order=cursor)
            seen.extend(event.event_id for event in page)
            if page.reached_end:
                break
            cursor = page.resume_after

        assert seen == ["$m11"]


class TestBoundedReads:
    """Reads are paged; there is no call that returns a whole room."""

    async def test_a_read_requires_a_positive_limit(self, alice: PrincipalStore) -> None:
        """A read requires a positive limit."""
        with pytest.raises(ValueError, match="positive limit"):
            await alice.read_conversation(room_id=ROOM, thread_id=None, limit=0)

    async def test_pages_walk_backwards_without_gaps_or_repeats(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Pages walk backwards without gaps or repeats."""
        for index in range(25):
            await admit(alice, f"$m{index:03d}", ts=1_000 + index)

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=7, before=cursor)
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == [f"$m{index:03d}" for index in range(25)]

    async def test_a_page_is_chronological(self, alice: PrincipalStore) -> None:
        """A page is chronological."""
        for index in range(5):
            await admit(alice, f"$m{index}", ts=1_000 + index)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert [m.created_ts for m in page.messages] == [1_000, 1_001, 1_002, 1_003, 1_004]

    async def test_threads_are_separate_conversations(self, alice: PrincipalStore) -> None:
        """Threads are separate conversations."""
        await admit(alice, "$room-message")
        await admit(alice, "$thread-message", thread_id="$root")

        assert await bodies(alice) == ["$room-message"]
        assert await bodies(alice, thread_id="$root") == ["$thread-message"]

    async def test_a_thread_read_includes_its_root(self, alice: PrincipalStore) -> None:
        """The root has no thread relation of its own, but the thread is about it."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice, thread_id="$root") == ["$root", "$reply"]

    async def test_the_root_appears_once_even_when_it_is_also_a_reply(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The root appears once even when it is also a reply."""
        await admit(alice, "$root", ts=1_000, thread_id="$root")
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice, thread_id="$root") == ["$root", "$reply"]

    async def test_a_thread_root_still_belongs_to_the_room_conversation(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread root still belongs to the room conversation."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")

        assert await bodies(alice) == ["$root"]

    async def test_a_thread_page_respects_its_limit_with_the_root_merged(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A thread page respects its limit with the root merged."""
        await admit(alice, "$root", ts=1_000)
        for index in range(5):
            await admit(alice, f"$reply{index}", ts=2_000 + index, thread_id="$root")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=3)

        assert [m.logical_event_id for m in page.messages] == ["$reply2", "$reply3", "$reply4"]

    async def test_paging_a_thread_reaches_the_root_last(self, alice: PrincipalStore) -> None:
        """Paging a thread reaches the root last."""
        await admit(alice, "$root", ts=1_000)
        for index in range(5):
            await admit(alice, f"$reply{index}", ts=2_000 + index, thread_id="$root")

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(
                room_id=ROOM,
                thread_id="$root",
                limit=2,
                before=cursor,
            )
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == ["$root", "$reply0", "$reply1", "$reply2", "$reply3", "$reply4"]

    async def test_ordering_agrees_with_python_for_mixed_case_ids(
        self,
        alice: PrincipalStore,
    ) -> None:
        """SQLite, PostgreSQL, and Python must agree on the cursor's order.

        PostgreSQL's default locale can sort ``'a'`` before ``'B'`` while
        SQLite and Python sort by byte. If the cursor column is not pinned to
        byte order, paging silently skips or repeats rows on one backend only.
        """
        identifiers = ["$aaa", "$BBB", "$aBc", "$Abc", "$zzz", "$ZZZ"]
        for event_id in identifiers:
            await admit(alice, event_id, ts=5_000)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert [m.logical_event_id for m in page.messages] == sorted(identifiers)

    async def test_cursor_paging_matches_byte_order_across_a_timestamp_tie(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Cursor paging matches byte order across a timestamp tie."""
        identifiers = ["$aaa", "$BBB", "$aBc", "$Abc", "$zzz", "$ZZZ"]
        for event_id in identifiers:
            await admit(alice, event_id, ts=5_000)

        seen: list[str] = []
        cursor = None
        while True:
            page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=2, before=cursor)
            seen = [m.logical_event_id for m in page.messages] + seen
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert seen == sorted(identifiers)


class TestStoreGeneration:
    """The identity a sync checkpoint is saved beside."""

    async def test_the_generation_is_minted_once_and_then_kept(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A second open must not rewrite it.

        This is the whole mechanism. If reopening the store minted a new
        generation, every saved checkpoint would be rejected on every restart
        and the bot would resync from scratch forever. If it *overwrote* the
        stored one, no checkpoint would ever be rejected and a replaced
        database would silently resume from a token covering events it never
        saw.
        """
        first = await journal_store.generation(new_generation="born-here")
        second = await journal_store.generation(new_generation="a-later-process")

        assert first == "born-here"
        assert second == "born-here"

    async def test_a_different_database_has_a_different_generation(
        self,
        journal_store: EventJournalStore,
        tmp_path: Path,
    ) -> None:
        """Two stores must not agree, or the check proves nothing."""
        from mindroom.event_journal import EventJournalStore as Store  # noqa: PLC0415

        mine = await journal_store.generation(new_generation="mine")
        replacement = Store.open_sqlite(tmp_path / "replacement.db")
        try:
            theirs = await replacement.generation(new_generation="theirs")
        finally:
            await replacement.close()

        assert mine != theirs

    async def test_the_generation_belongs_to_the_database_not_a_principal(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Every principal in one database lost the same history if it were replaced."""
        generation = await journal_store.generation(new_generation="shared")

        await admit(journal_store.principal(ALICE), "$only-alice")
        assert await journal_store.principal(BOB).load_event("$only-alice") is None
        assert await journal_store.generation(new_generation="ignored") == generation


class TestAdmittedThreadId:
    """What the journal already knows about an event's place in a thread."""

    async def test_an_unseen_event_is_reported_as_unseen(self, alice: PrincipalStore) -> None:
        """An unseen event is reported as unseen, not as thread-less."""
        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$never") == (False, None)

    async def test_a_room_event_is_admitted_and_in_no_thread(self, alice: PrincipalStore) -> None:
        """These two answers are opposite situations and only one is worth a fetch."""
        await admit(alice, "$room-message")

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$room-message") == (True, None)

    async def test_a_thread_reply_reports_its_root(self, alice: PrincipalStore) -> None:
        """A thread reply reports its root."""
        await admit(alice, "$reply", thread_id="$root")

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$reply") == (True, "$root")

    async def test_a_context_only_event_still_answers(self, alice: PrincipalStore) -> None:
        """Settlement clears the replay payload, not the relation the row records."""
        await admit(alice, "$context", thread_id="$root", event_class=EventClass.CONTEXT_ONLY)

        assert await alice.admitted_thread_id(room_id=ROOM, event_id="$context") == (True, "$root")

    async def test_another_room_does_not_answer(self, alice: PrincipalStore) -> None:
        """Another room does not answer."""
        await admit(alice, "$reply", thread_id="$root")

        assert await alice.admitted_thread_id(room_id=OTHER_ROOM, event_id="$reply") == (False, None)


class TestLatestVisibleEvent:
    """The reply target a thread-blind client is pointed at."""

    async def test_an_empty_thread_has_no_latest_event(self, alice: PrincipalStore) -> None:
        """An empty thread has no latest event."""
        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_the_newest_reply_wins(self, alice: PrincipalStore) -> None:
        """The newest reply wins."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$early", ts=2_000, thread_id="$root")
        await admit(alice, "$late", ts=3_000, thread_id="$root")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$late"

    async def test_an_edited_message_answers_with_the_revision_on_screen(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An edit is the event actually in the room, so it is what a reply quotes."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(alice, "$child-edit", ts=3_000, thread_id="$root", content=edit("$child", "revised"))

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child-edit"

    async def test_a_redacted_revision_answers_with_its_logical_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Quoting a deleted edit renders as nothing; the original is still there."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(alice, "$child-edit", ts=3_000, thread_id="$root", content=edit("$child", "revised"))
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$child-edit")

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=10)
        assert [r.logical_event_id for r in page.refresh_pending] == ["$child"]

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child"

    async def test_a_redacted_logical_event_falls_through_to_the_message_behind_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Redacting the message itself removes the row, so the previous one answers."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$early", ts=2_000, thread_id="$root")
        await admit(alice, "$late", ts=3_000, thread_id="$root")
        await admit(alice, "$redaction", ts=4_000, kind=EventKind.REDACTION, redacts="$late")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$early"

    async def test_a_root_only_thread_has_no_latest_event(self, alice: PrincipalStore) -> None:
        """The root is stored in the room conversation, so a childless thread is empty.

        The caller falls back to the thread ID, which is the root's own event ID,
        so merging it here would only arrive at the same answer twice.
        """
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$root-edit", ts=2_000, content=edit("$root", "revised"))

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_another_thread_in_the_room_does_not_answer(self, alice: PrincipalStore) -> None:
        """Another thread in the room does not answer."""
        await admit(alice, "$other", ts=9_000, thread_id="$other-root")

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None

    async def test_a_sidecar_message_answers_with_the_revision_on_screen(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A withheld body is not proof of a redaction.

        A message whose text lives in a sidecar is stored exactly like a
        redacted one -- no body, refresh owed -- but its revision is a live
        event nobody deleted, and it is the right thing to quote.
        """
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$child", ts=2_000, thread_id="$root")
        await admit(
            alice,
            "$child-edit",
            ts=3_000,
            thread_id="$root",
            content=sidecar(edit("$child", "revised")),
        )

        page = await alice.read_conversation(room_id=ROOM, thread_id="$root", limit=10)
        assert [r.logical_event_id for r in page.refresh_pending] == ["$child"]

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$child-edit"

    async def test_a_rejoin_stops_the_previous_membership_from_answering(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A rejoin can expose different history, so the old tail cannot be quoted."""
        await admit(alice, "$root", ts=1_000)
        await admit(alice, "$reply", ts=2_000, thread_id="$root")
        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") == "$reply"

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.latest_visible_event_id(room_id=ROOM, thread_id="$root") is None


class TestDeliveryIsScopedToTheMembershipThatAuthorizedIt:
    """A turn that outlived its membership must not answer into the next one.

    The fence deletes what the previous membership derived. Without this it
    would then write some of it straight back: a turn still running when the
    fence committed reaches enqueue afterwards, and the fence has been and
    gone.
    """

    async def test_a_turn_admitted_under_an_ended_membership_cannot_enqueue(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Fence first, then enqueue: the enqueue is refused."""
        await admit(alice, "$turn")
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        transaction_id = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert transaction_id is None
        assert await alice.load_delivery(turn_id="$turn", stage=DeliveryStage.FINAL) is None

    async def test_an_unattempted_row_enqueued_before_the_fence_is_deleted_by_it(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Enqueue first, then fence: the row goes with the membership."""
        await admit(alice, "$turn")
        assert (
            await alice.enqueue_delivery(
                turn_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("answer"),
            )
            is not None
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.load_delivery(turn_id="$turn", stage=DeliveryStage.FINAL) is None

    async def test_an_attempted_row_still_retries_after_a_fence_under_its_first_transaction(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An attempted delivery is a different object, and refusing it is worse.

        Its outcome is unknown and the homeserver may hold it already. Only
        presenting the identical transaction ID again collapses the retry onto
        the same event; refusing it would strand the row unacknowledged while
        leaving whatever it sent visible, and re-deriving a fresh transaction
        for it would guarantee the second answer rather than prevent it.
        """
        await admit(alice, "$turn")
        first = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_delivery(turn_id="$turn", stage=DeliveryStage.FINAL)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        retried = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("regenerated"),
        )
        claimed = await alice.claim_delivery(turn_id="$turn", stage=DeliveryStage.FINAL)

        assert retried == first
        assert claimed is not None
        assert claimed.transaction_id == first
        assert claimed.payload["body"] == "answer"

    async def test_a_turn_the_journal_never_admitted_still_enqueues(self, alice: PrincipalStore) -> None:
        """A scheduled task is not a turn a membership authorized.

        There is no admission behind it and so no previous membership for its
        work to belong to. Refusing it would silence scheduled delivery in
        every room the bot has ever left and rejoined.
        """
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        transaction_id = await alice.enqueue_delivery(
            turn_id="scheduled-task-7",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("reminder"),
        )

        assert transaction_id is not None

    async def test_a_turn_under_the_current_membership_enqueues(self, alice: PrincipalStore) -> None:
        """The ordinary case still delivers."""
        await admit(alice, "$turn")

        transaction_id = await alice.enqueue_delivery(
            turn_id="$turn",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert transaction_id == delivery_transaction_id("agent@alice", "$turn", "final")

    async def test_in_flight_transport_learns_the_membership_ended(self, alice: PrincipalStore) -> None:
        """Streaming edits never reach the outbox, so they ask this directly."""
        await admit(alice, "$turn")

        assert await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert not await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)

    async def test_one_rooms_fence_does_not_silence_another_room(self, alice: PrincipalStore) -> None:
        """Leaving one room says nothing about a turn running in a different one."""
        await admit(alice, "$turn")

        await alice.fence_departure(OTHER_ROOM, source=DepartureSource.LOCAL)

        assert await alice.turn_membership_is_current(turn_id="$turn", room_id=ROOM)
        assert (
            await alice.enqueue_delivery(
                turn_id="$turn",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload=text("answer"),
            )
            is not None
        )


class TestDepartureBookkeeping:
    """One departure invalidates a room once, whichever observer sees it first."""

    async def test_a_consumed_report_leaves_the_new_projection_alone(self, alice: PrincipalStore) -> None:
        """Absorbing a report must not delete what the membership after it built."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(ROOM)
        await admit(alice, "$fresh", ts=5_000)

        await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert [m.logical_event_id for m in page.messages] == ["$fresh"]

    async def test_a_departure_with_no_report_owed_invalidates(self, alice: PrincipalStore) -> None:
        """A departure the bot never initiated drops what the old membership built."""
        await admit(alice, "$stale", ts=5_000)

        outcome = await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)

        assert outcome.observation is DepartureObservation.FENCED
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=5)
        assert page.messages == ()

    async def test_owed_reports_are_scoped_to_one_principal(self, journal_store: EventJournalStore) -> None:
        """One bot's owed report must not absorb another bot's departure."""
        alice = journal_store.principal("agent@alice")
        bob = journal_store.principal("agent@bob")
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await bob.rooms_owing_departure_reports() == frozenset()
        assert (await bob.fence_departure(ROOM, source=DepartureSource.REPORTED)).fenced

    async def test_retiring_one_room_leaves_another_rooms_report_owed(self, alice: PrincipalStore) -> None:
        """Giving up on one room's report says nothing about any other room."""
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.fence_departure(OTHER_ROOM, source=DepartureSource.LOCAL)

        await alice.retire_owed_departure_reports(ROOM)

        assert await alice.rooms_owing_departure_reports() == frozenset({OTHER_ROOM})
        # The retired room's report is no longer absorbed. It is recognised as
        # the departure this room is already fenced for, which is a different
        # answer from "a report was owed and this was it".
        retired = await alice.fence_departure(ROOM, source=DepartureSource.REPORTED)
        still_owed = await alice.fence_departure(OTHER_ROOM, source=DepartureSource.REPORTED)
        assert retired.observation is DepartureObservation.ALREADY_FENCED
        assert still_owed.observation is DepartureObservation.OWED_REPORT_CONSUMED


class TestByteOrderPinning:
    """Ordering that a cursor depends on must not vary with the server locale."""

    def test_the_cursor_comparison_is_pinned_to_byte_order(self) -> None:
        """turn_id and stage shipped unpinned and cannot be retyped in place.

        A PostgreSQL locale whose collation is not byte order would sort them
        differently from the cursor's own comparison, and recovery would skip
        rows or revisit them. CI cannot catch that: its PostgreSQL image uses
        musl locales, which all behave like C, so the two orderings agree there
        and diverge in a glibc deployment.
        """
        ordering = "ORDER BY created_at_ns, turn_id/*bytes*/, stage/*bytes*/"

        assert render(ordering, SQLITE_DIALECT) == "ORDER BY created_at_ns, turn_id, stage"
        assert render(ordering, POSTGRES_DIALECT) == ('ORDER BY created_at_ns, turn_id COLLATE "C", stage COLLATE "C"')

    def test_a_marker_inside_a_literal_is_refused(self) -> None:
        """Substitution is a plain rewrite and cannot tell a literal from an identifier.

        No statement embeds one today, and values are bound separately by both
        backends, so nothing user-controlled reaches the rewriter. The guard is
        there because the rewriter has no way to check that for itself.
        """
        with pytest.raises(ValueError, match="byte-order marker"):
            render("SELECT '/*bytes*/'", SQLITE_DIALECT)

    def test_a_statement_without_the_marker_is_untouched(self) -> None:
        """The rewrite must not perturb the statements that do not opt in."""
        assert render("SELECT 1", SQLITE_DIALECT) == "SELECT 1"
        assert render("SELECT 1", POSTGRES_DIALECT) == "SELECT 1"


class TestMembershipEpoch:
    """Leaving and rejoining invalidates what the previous membership saw."""

    async def test_hydration_is_recorded_per_membership(self, alice: PrincipalStore) -> None:
        """Hydration is recorded per membership."""
        epoch = await alice.membership_epoch(ROOM)
        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            complete=True,
            expected_membership_epoch=epoch,
        )

        assert installed
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await bodies(alice) == ["$hydrated"]

    async def test_rejoining_invalidates_hydration(self, alice: PrincipalStore) -> None:
        """Rejoining invalidates hydration."""
        epoch = await alice.membership_epoch(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            complete=True,
            expected_membership_epoch=epoch,
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_rejoining_clears_a_conversation_the_last_membership_proved_whole(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Coverage carries forward inside a membership and never across one.

        Inside one, a walk that ran out of conversation proved something about
        the conversation rather than about itself, so a later narrower walk
        cannot take it back. A rejoin is a different membership over a slice of
        history the bot may never have seen, and nothing the previous one
        proved is allowed to speak for it.
        """
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$whole")[1],),
            complete=True,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$suffix")[1],),
            complete=False,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert not await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
        assert await alice.conversation_hydration_was_truncated(room_id=ROOM, thread_id=None)

    async def test_rejoining_drops_answers_the_previous_membership_never_sent(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An unsent answer belongs to the conversation it was written for.

        Delivering it after a leave and rejoin would drop a reply to the old
        membership into the new one, where nothing asked for it.
        """
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.unacknowledged_deliveries() == ()
        assert await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL) is None

    async def test_rejoining_keeps_an_answer_that_may_already_be_visible(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An attempted delivery has an outcome only the homeserver knows.

        Deleting it would free the turn to run again and post a second answer.
        The row is what makes the retry converge instead: it still holds the
        frozen payload and the transaction that goes with it.
        """
        transaction_id = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        kept = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert kept is not None
        assert kept.transaction_id == transaction_id
        assert kept.payload["body"] == "answer"
        assert [delivery.turn_id for delivery in await alice.unacknowledged_deliveries()] == ["turn-1"]

    async def test_rejoining_keeps_an_answer_matrix_already_accepted(
        self,
        alice: PrincipalStore,
    ) -> None:
        """That row is the record that the message is already visible."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL, event_id="$sent")

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$sent"

    async def test_context_only_events_keep_no_payload(self, alice: PrincipalStore) -> None:
        """Otherwise the journal becomes the raw-event cache it replaces.

        A context-only event is projected at admission and never replayed, so
        it is admitted already settled — which means settlement, the step that
        clears a payload, never runs for it. Storing the source anyway retains
        every message the bot has ever seen, forever.
        """
        body = "x" * 500
        admission, projected = message("$history", content=text(body), event_class=EventClass.CONTEXT_ONLY)
        await alice.admit(admission, projected)

        stored = await alice.load_event("$history")

        assert stored is not None
        assert stored.source == {}
        assert await bodies(alice) == [body]

    async def test_actionable_events_keep_their_replay_payload(self, alice: PrincipalStore) -> None:
        """Compaction must not reach the events a crash has to replay."""
        admission, projected = message("$live", content=text("answer me"))
        await alice.admit(admission, projected)

        stored = await alice.load_event("$live")

        assert stored is not None
        assert stored.source != {}

    async def test_rejoining_removes_what_the_previous_membership_projected(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Otherwise the two memberships merge into one conversation.

        Dropping only the hydration marker leaves the old messages readable,
        so the next hydration adds the new membership's view on top of a
        history this membership may not be entitled to see at all.
        """
        epoch = await alice.membership_epoch(ROOM)
        await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$before")[1],),
            complete=True,
            expected_membership_epoch=epoch,
        )
        assert await bodies(alice) == ["$before"]

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await bodies(alice) == []

    async def test_rejoining_keeps_the_proof_that_an_event_was_answered(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The dedup record has to outlive any rejoin, or the turn runs twice."""
        admission, projected = message("$answered")
        await alice.admit(admission, projected)
        await alice.settle("$answered")

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.load_event("$answered") is not None
        assert await alice.admit(*message("$answered")) is AdmissionResult.DUPLICATE

    async def test_hydration_racing_a_rejoin_installs_nothing(self, alice: PrincipalStore) -> None:
        """A partly applied hydration would look complete to the next reader."""
        stale_epoch = await alice.membership_epoch(ROOM)
        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(message("$hydrated")[1],),
            complete=True,
            expected_membership_epoch=stale_epoch,
        )

        assert not installed
        assert await bodies(alice) == []
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestBoundedHydrationInstallation:
    """Strict walks never monopolize one backend write for their full result."""

    @staticmethod
    def _assert_bounded_projection_writes(shapes: list[_HydrationWriteShape], *, expected_events: int) -> None:
        """Assert the externally relevant transaction shape of a hydration install."""
        projection_shapes = [shape for shape in shapes if shape.projected_messages]
        assert sum(shape.projected_messages for shape in projection_shapes) == expected_events
        assert len(projection_shapes) >= 2
        assert max(shape.projected_messages for shape in projection_shapes) <= _HYDRATION_TRANSACTION_EVENT_LIMIT
        assert all(shape.membership_claims == 1 for shape in projection_shapes)

    async def test_ordinary_hydration_projects_in_bounded_writes_then_publishes(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Putting the complete projection loop back in one write breaks this test."""
        observed = _ObservedHydrationBackend(journal_store.backend)
        alice = EventJournalStore(backend=observed).principal("agent@alice")
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=events,
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert installed
        self._assert_bounded_projection_writes(observed.write_shapes, expected_events=len(events))
        publication_shapes = [shape for shape in observed.write_shapes if shape.hydration_markers]
        assert publication_shapes == [observed.write_shapes[-1]]
        assert publication_shapes[0].projected_messages == 0
        assert publication_shapes[0].membership_claims == 1
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await alice.conversation_is_complete(room_id=ROOM, thread_id=None)
        coverage = await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None)
        assert coverage is not None
        assert coverage.attempted_policy_rank == 2

    async def test_empty_hydration_still_claims_the_epoch_when_it_publishes(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Skipping the final write when there are no chunks breaks this test."""
        observed = _ObservedHydrationBackend(journal_store.backend)
        alice = EventJournalStore(backend=observed).principal("agent@alice")

        installed = await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=(),
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert installed
        assert observed.write_shapes == [
            _HydrationWriteShape(membership_claims=1, hydration_markers=1),
        ]
        assert await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_history_recovery_projects_in_bounded_writes_then_settles(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Combining recovery projection and settlement into one giant write breaks this test."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")

        outcome = await recovering.settle_room_history_recovery(
            recovery,
            events=events,
            exhausted_server=True,
            attempted_policy_rank=2,
            expected_membership_epoch=await recovering.membership_epoch(ROOM),
        )

        assert outcome is HistoryRecoveryOutcome.REPAIRED
        self._assert_bounded_projection_writes(observed.write_shapes, expected_events=len(events))
        final_shape = observed.write_shapes[-1]
        assert final_shape.hydration_markers == 1
        assert final_shape.recovery_settlements == 1
        assert final_shape.projected_messages == 0
        assert final_shape.membership_claims == 1
        assert await recovering.room_history_recovery(ROOM) is None
        assert await recovering.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_failure_after_a_recovery_chunk_leaves_the_obligation_retryable(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Publishing or settling before every chunk lands breaks this retry."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)

        async def fail_second_write(write_number: int) -> None:
            if write_number == 2:
                msg = "injected failure after a committed hydration chunk"
                raise RuntimeError(msg)

        observed = _ObservedHydrationBackend(journal_store.backend, before_write=fail_second_write)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")

        with pytest.raises(RuntimeError, match="injected failure"):
            await recovering.settle_room_history_recovery(
                recovery,
                events=events,
                exhausted_server=True,
                attempted_policy_rank=2,
                expected_membership_epoch=await recovering.membership_epoch(ROOM),
            )

        assert observed.write_shapes[0].projected_messages == _HYDRATION_TRANSACTION_EVENT_LIMIT
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None) is None
        assert await alice.room_history_recovery(ROOM) == recovery

        outcome = await alice.settle_room_history_recovery(
            recovery,
            events=events,
            exhausted_server=True,
            attempted_policy_rank=2,
            expected_membership_epoch=await alice.membership_epoch(ROOM),
        )

        assert outcome is HistoryRecoveryOutcome.REPAIRED
        assert await alice.room_history_recovery(ROOM) is None
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=len(events) + 1)
        assert {message.logical_event_id for message in page.messages} == {event.event_id for event in events}

    async def test_truncated_recovery_finalization_does_not_scan_conversation_markers(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """The final bounded transaction must not rewrite every thread marker."""
        alice = journal_store.principal("agent@alice")
        recovery = await alice.record_room_history_recovery(ROOM)
        assert recovery is not None
        observed = _ObservedHydrationBackend(journal_store.backend)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")

        outcome = await recovering.settle_room_history_recovery(
            recovery,
            events=(),
            exhausted_server=False,
            attempted_policy_rank=2,
            expected_membership_epoch=await recovering.membership_epoch(ROOM),
        )

        assert outcome is HistoryRecoveryOutcome.TRUNCATED
        assert observed.write_shapes == [
            _HydrationWriteShape(membership_claims=1, hydration_markers=1),
        ]

    async def test_cancellation_after_a_chunk_leaves_ordinary_hydration_retryable(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Publishing in a projection chunk would make cancellation look complete."""
        first_chunk_committed = asyncio.Event()
        release = asyncio.Event()

        async def pause_after_first_write(write_number: int) -> None:
            if write_number == 1:
                first_chunk_committed.set()
                await release.wait()

        observed = _ObservedHydrationBackend(journal_store.backend, after_write=pause_after_first_write)
        hydrating = EventJournalStore(backend=observed).principal("agent@alice")
        alice = journal_store.principal("agent@alice")
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)
        epoch = await alice.membership_epoch(ROOM)
        install = asyncio.create_task(
            hydrating.install_hydrated_conversation(
                room_id=ROOM,
                thread_id=None,
                events=events,
                complete=True,
                attempted_policy_rank=2,
                expected_membership_epoch=epoch,
            ),
        )
        await first_chunk_committed.wait()
        install.cancel()
        with pytest.raises(asyncio.CancelledError):
            await install
        release.set()

        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)
        assert await alice.conversation_hydration_coverage(room_id=ROOM, thread_id=None) is None
        partial = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=len(events))
        assert len(partial.messages) == _HYDRATION_TRANSACTION_EVENT_LIMIT

        assert await alice.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=events,
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=epoch,
        )
        page = await alice.read_conversation(room_id=ROOM, thread_id=None, limit=len(events))
        assert {message.logical_event_id for message in page.messages} == {event.event_id for event in events}

    async def test_membership_fence_between_chunks_supersedes_the_whole_install(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Omitting the per-chunk epoch claim resurrects rows behind this fence."""
        alice = journal_store.principal("agent@alice")
        stale_epoch = await alice.membership_epoch(ROOM)

        async def fence_after_first_write(write_number: int) -> None:
            if write_number == 1:
                await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        observed = _ObservedHydrationBackend(journal_store.backend, after_write=fence_after_first_write)
        hydrating = EventJournalStore(backend=observed).principal("agent@alice")
        installed = await hydrating.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=None,
            events=hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS),
            complete=True,
            attempted_policy_rank=2,
            expected_membership_epoch=stale_epoch,
        )

        assert not installed
        assert await alice.membership_epoch(ROOM) == stale_epoch + 1
        assert await bodies(alice, limit=_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS) == []
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)

    async def test_membership_fence_during_recovery_does_not_settle_a_newer_obligation(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Settling recovery outside final epoch validation erases this newer obligation."""
        alice = journal_store.principal("agent@alice")
        old_recovery = await alice.record_room_history_recovery(ROOM)
        assert old_recovery is not None
        stale_epoch = await alice.membership_epoch(ROOM)
        newer_recovery: RoomHistoryRecovery | None = None

        async def fence_and_record_new_recovery(write_number: int) -> None:
            nonlocal newer_recovery
            if write_number == 1:
                await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)
                await alice.note_membership_restarted(ROOM)
                await admit(alice, "$new-anchor", ts=10_000)
                newer_recovery = await alice.record_room_history_recovery(ROOM)

        observed = _ObservedHydrationBackend(journal_store.backend, after_write=fence_and_record_new_recovery)
        recovering = EventJournalStore(backend=observed).principal("agent@alice")
        events = hydration_messages(_EVENTS_SPANNING_TWO_HYDRATION_CHUNKS)

        outcome = await recovering.settle_room_history_recovery(
            old_recovery,
            events=events,
            exhausted_server=True,
            attempted_policy_rank=2,
            expected_membership_epoch=stale_epoch,
        )

        assert outcome is HistoryRecoveryOutcome.SUPERSEDED
        assert newer_recovery is not None
        assert await alice.room_history_recovery(ROOM) == newer_recovery
        assert await bodies(alice, limit=len(events)) == ["$new-anchor"]
        assert not await alice.conversation_is_hydrated(room_id=ROOM, thread_id=None)


# How long a racer is given to queue behind the held row before the test calls
# it a failure, and how often that is checked. Generous because the wait is a
# condition rather than a delay: reaching it early costs nothing.
_QUEUE_TIMEOUT_SECONDS = 20.0
_QUEUE_POLL_SECONDS = 0.01

# Racer connections parked on a heavyweight lock. `pg_stat_activity` is
# cluster-wide, so the count is scoped by the application name the two rival
# stores connect under, minted per fixture and shared by nothing else -- not
# the connection holding the row, not the one running this query, not another
# xdist worker. Scoping by database would work today -- `postgres_journal_url`
# gives each worker its own `mindroom_<worker_id>` and a master run its own
# container -- but it would read isolation off the fixture's topology rather
# than off the connections being counted. Should that topology ever narrow to
# one shared database, an unrelated waiter would inflate the count, the row
# would be released before both racers were behind it, and the late one would
# decline against an already-bound row: the test passing for the one reason it
# exists to rule out, and passing silently.
_QUEUED_RACERS = """
    SELECT count(*) FROM pg_stat_activity
    WHERE application_name = %s AND wait_event_type = 'Lock'
"""


@dataclass(frozen=True, slots=True)
class RivalStores:
    """Two stores over one PostgreSQL database, and the DSN that reaches it.

    Two stores rather than two coroutines, because a store serializes its own
    writes behind one connection and one lock. Anything sharing that has
    already been made to take turns, which is the opposite of the situation
    worth testing.
    """

    first: EventJournalStore
    second: EventJournalStore
    database_url: str
    # What both stores report as their `application_name`, and the only handle
    # that tells their connections apart from every other one on the server.
    racer_application_name: str


@pytest_asyncio.fixture
async def rival_stores(postgres_journal_url: str) -> AsyncGenerator[RivalStores, None]:
    """Open two independently connected stores onto one database.

    PostgreSQL only, and deliberately not part of the backend parity sweep.
    Two SQLite stores in one process are two queues onto the same serialized
    write, so racing them here would only prove something about the fixture.

    Not because SQLite has no second connection to race -- it has one whenever
    ``mindroom threads export`` is running, which is its own process with its
    own writer. That race is real, and it is not this fixture's to stage: it
    needs a second interpreter rather than a second object, which is what
    ``TestCrossProcessWriters`` spawns.

    Only the stores carry the application name. The connection that holds the
    row and the one that watches for waiters use the bare DSN, so neither can
    be mistaken for a racer it is supposed to be observing.
    """
    database_url = postgres_journal_schema_url(postgres_journal_url)
    application_name = f"mindroom-journal-race-{uuid.uuid4().hex}"
    racer_url = f"{database_url}&application_name={application_name}"
    first = EventJournalStore.open_postgres(racer_url)
    second = EventJournalStore.open_postgres(racer_url)
    try:
        yield RivalStores(
            first=first,
            second=second,
            database_url=database_url,
            racer_application_name=application_name,
        )
    finally:
        await first.close()
        await second.close()


@contextmanager
def _outbox_row_held(database_url: str, *, principal_id: str, turn_id: str) -> Iterator[None]:
    """Lock one outbox row from a third connection until the block exits.

    The hold point has to be the row rather than anything in Python, because
    the row is the one place both implementations of the write must pass
    through. A caller parked here has finished whatever reading it does and
    has not yet written, which is exactly the interleaving that decides who
    owns the acknowledgement -- and it is the same point whether the caller
    read first or is reading and writing in one statement.
    """
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    with psycopg.connect(database_url) as connection:
        held = connection.execute(
            "SELECT 1 FROM response_outbox WHERE principal_id = %s AND turn_id = %s FOR UPDATE",
            (principal_id, turn_id),
        ).fetchone()
        assert held is not None, "there is no enqueued delivery to hold"
        try:
            yield
        finally:
            connection.rollback()


async def _await_queued_racers(database_url: str, *, application_name: str, expected: int) -> None:
    """Wait until ``expected`` of that application's connections are parked on a lock.

    A condition, not a delay. The row may only be released once every racer is
    behind it, because a racer that has not started yet would read the bound
    row and decline on its own -- which is the losing implementation passing
    for a reason that has nothing to do with what it does under contention.
    """
    await asyncio.to_thread(_watch_queued_racers, database_url, application_name, expected)


def _watch_queued_racers(database_url: str, application_name: str, expected: int) -> None:
    """Poll until enough racers are waiting, or say how many turned up."""
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    deadline = time.monotonic() + _QUEUE_TIMEOUT_SECONDS
    with psycopg.connect(database_url, autocommit=True) as connection:
        while True:
            row = connection.execute(_QUEUED_RACERS, (application_name,)).fetchone()
            queued = 0 if row is None else int(row[0])
            if queued >= expected:
                return
            if time.monotonic() > deadline:
                msg = f"only {queued} of {expected} racers queued on the held outbox row"
                raise AssertionError(msg)
            time.sleep(_QUEUE_POLL_SECONDS)


def _watch_until_queued_or_finished(
    database_url: str,
    application_name: str,
    finished: threading.Event,
) -> None:
    """Block until the other writer is parked on a lock, or got past without one.

    Either answer is the interleaving this is opening a window for, which is
    why both end the wait. A writer that queued was ordered behind the row this
    transaction holds; a writer that finished was not ordered at all, and the
    caller resuming here is precisely the one whose rows then outlive it. The
    wait exists so neither outcome depends on which thread the scheduler
    happens to run first.
    """
    import psycopg  # noqa: PLC0415 - psycopg ships in the optional postgres extra

    deadline = time.monotonic() + _QUEUE_TIMEOUT_SECONDS
    with psycopg.connect(database_url, autocommit=True) as connection:
        while not finished.is_set():
            row = connection.execute(_QUEUED_RACERS, (application_name,)).fetchone()
            if row is not None and int(row[0]) > 0:
                return
            if time.monotonic() > deadline:
                msg = "the second writer neither queued behind the first nor ran to completion"
                raise AssertionError(msg)
            time.sleep(_QUEUE_POLL_SECONDS)


class TestAFenceCannotBeSteppedOverByAConcurrentWalk:
    """What a hydrating writer and a fencing writer do to one PostgreSQL database.

    PostgreSQL only, for the reason the acknowledgement race is: SQLite holds a
    write lock across the whole transaction, so a second store over the same
    file is a second queue onto the same serialized write and there is no
    interleaving left to produce.
    """

    async def test_a_walk_that_read_the_epoch_first_cannot_outlive_the_fence(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A hydration's epoch decision has to be a claim, not an observation.

        `test_hydration_racing_a_rejoin_installs_nothing` proves the other half
        of this rule and cannot reach this one: by the time it installs, the
        fence has already committed, so the epoch it reads is the new one and
        any implementation declines. This half is what happens when the fence
        has not committed yet.

        Two writers on one database is the deployed shape, not a contrivance:
        `mindroom threads export` opens the running install's journal in its own
        process and runs its own `ConversationHydrator` against it, so the store
        lock that serializes writes inside one process orders nothing between
        them. Under ``READ COMMITTED`` the walk's plain epoch ``SELECT`` saw the
        membership it expected, the fence then deleted every row committed at
        that instant, and the walk's rows landed behind it -- a conversation
        from a membership the bot has left, projected under an epoch no reader
        of `visible_messages` filters on, and served into every prompt from then
        on. Nothing takes them back out: the walk that runs under the new
        membership projects over them with `ON CONFLICT DO NOTHING`, so the two
        memberships end up merged into one conversation.

        Modelled with two stores over one database rather than two operating
        system processes: what makes the race possible is two writer connections
        with no lock between them, which is exactly what two stores are. The
        window is opened by pausing the walk after its first statement -- the
        real transaction, the real SQL, only held open -- and the fence is a
        real `fence_departure` on the second store.
        """
        principal_id = "agent@alice"
        reader = rival_stores.first.principal(principal_id)
        fencing = rival_stores.second.principal(principal_id)
        inside_the_walk = threading.Event()
        fence_finished = threading.Event()

        def hold_the_walk_open() -> None:
            inside_the_walk.set()
            _watch_until_queued_or_finished(
                rival_stores.database_url,
                rival_stores.racer_application_name,
                fence_finished,
            )

        hydrating = EventJournalStore(
            backend=_PausingBackend(rival_stores.first.backend, hold_the_walk_open),
        ).principal(principal_id)
        epoch = await reader.membership_epoch(ROOM)

        walk = asyncio.create_task(
            hydrating.install_hydrated_conversation(
                room_id=ROOM,
                thread_id=None,
                events=(message("$resurrected")[1],),
                complete=True,
                expected_membership_epoch=epoch,
            ),
        )
        await asyncio.to_thread(inside_the_walk.wait, _WORKER_WAIT_SECONDS)
        assert inside_the_walk.is_set(), "the walk never reached its epoch decision"
        fence = asyncio.create_task(fencing.fence_departure(ROOM, source=DepartureSource.REPORTED))
        fence.add_done_callback(lambda _: fence_finished.set())
        installed, outcome = await asyncio.gather(walk, fence)

        assert not installed, "the final publication trusted the epoch the fence superseded"
        assert outcome.observation is DepartureObservation.FENCED
        assert outcome.membership_epoch == epoch + 1
        assert await bodies(reader) == [], "the walk's messages survived the departure that was supposed to erase them"
        assert not await reader.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestRecoveryFinalizesOnlyItsExactObligation:
    """The final recovery commit cannot consume a later process's obligation."""

    async def test_replaced_recovery_is_locked_and_refused_before_publication(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """A new gap waits behind the exact recovery claim and survives settlement."""
        principal_id = "agent@alice"
        reader = rival_stores.first.principal(principal_id)
        old_recovery = await reader.record_room_history_recovery(ROOM)
        assert old_recovery is not None
        inside_final = threading.Event()
        release_final = threading.Event()

        def pause_after_recovery_claim() -> None:
            inside_final.set()
            assert release_final.wait(_WORKER_WAIT_SECONDS), "the recovery transaction was never released"

        recovering = EventJournalStore(
            backend=_PausingBackend(
                rival_stores.first.backend,
                pause_after_recovery_claim,
                statement_matches=lambda sql: "UPDATE room_history_recovery SET state = state" in sql,
            ),
        ).principal(principal_id)
        settlement = asyncio.create_task(
            recovering.settle_room_history_recovery(
                old_recovery,
                events=(),
                exhausted_server=True,
                attempted_policy_rank=2,
                expected_membership_epoch=await reader.membership_epoch(ROOM),
            ),
        )
        await asyncio.to_thread(inside_final.wait, _WORKER_WAIT_SECONDS)
        assert inside_final.is_set(), "the repayment never claimed its exact recovery row"
        replacement = asyncio.create_task(
            rival_stores.second.principal(principal_id).record_room_history_recovery(ROOM),
        )
        try:
            await _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=1,
            )
        finally:
            release_final.set()

        outcome, newer_recovery = await asyncio.gather(settlement, replacement)

        assert outcome is HistoryRecoveryOutcome.REPAIRED
        assert newer_recovery is not None
        assert newer_recovery.revision == old_recovery.revision + 1
        assert await reader.room_history_recovery(ROOM) == newer_recovery
        assert not await reader.conversation_is_hydrated(room_id=ROOM, thread_id=None)


class TestOutbox:
    """Delivery survives a crash at every point around the network call."""

    async def test_a_losing_acknowledgement_writes_neither_the_row_nor_the_record(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """First-writer-wins has to cover the record travelling beside the row.

        The acknowledgement is the proof of which event is visible, and the
        terminal record is written from that same proof. If a second caller
        loses the row but still writes its record, the outbox names one event
        and the record names another -- which is the disagreement putting them
        in one transaction was supposed to make impossible.

        Against a real store on both backends, because the guard this pins is a
        property of the SQL statement. A fake outbox cannot observe it: the
        gateway tests inject one, the turn-store tests only build the record,
        and every one of them stays green with the guard removed.
        """
        alice = journal_store.principal("agent@alice")
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        def record(event_id: str) -> TerminalTurnWrite:
            return TerminalTurnWrite(
                agent_name="general",
                index_event_ids=("$source",),
                anchor_event_id="$source",
                record_json=json.dumps({"response_event_id": event_id}),
            )

        await alice.acknowledge_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$first",
            terminal_turn=record("$first"),
        )
        await alice.acknowledge_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$second",
            terminal_turn=record("$second"),
        )

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$first", "the row was rebound by a losing caller"
        rows = await journal_store.turn_records("general").load_all()
        assert [json.loads(row[2])["response_event_id"] for row in rows] == ["$first"], (
            "the losing caller rewrote the terminal record beside the row it did not win"
        )

    async def test_only_one_of_two_concurrent_claims_may_see_an_unattempted_row(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """Being told a row is unattempted is permission to send without asking.

        Delivery reads ``attempted`` off what the claim reports and treats a
        blank row as proof the homeserver has never seen this transaction ID, so
        it sends straight away instead of reading the room first. Tell two
        callers that and both send; if they are logged in as different devices
        the shared transaction ID deduplicates neither, and one message gets two
        visible answers.

        Reading the row and then marking it allowed exactly that. Against one
        PostgreSQL database both claims came back unattempted, every time.
        SQLite never showed it, because ``BEGIN IMMEDIATE`` holds a write lock
        across the whole transaction there -- which is why this runs on both.
        The guarantee has to come from the statement, not from one backend
        happening to serialize.

        The claimer that gets inside first waits for the other to reach its own
        first statement, which under a correct claim never happens: the other is
        blocked on the winner's row lock. So the wait is bounded and expiring is
        the fix working. A machine slow enough to expire it early could only make
        this test pass when it should have failed, never the reverse, because a
        claim that reports ownership from its own write has one winner at every
        interleaving.
        """
        principal = "agent@alice"
        first = journal_database()
        second = journal_database()
        await first.principal(principal).enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        reached_first_statement = threading.Event()

        def contend(store: EventJournalStore, hook: Callable[[], object]) -> Awaitable[OutboxDelivery | None]:
            paused = EventJournalStore(backend=_PausingBackend(store.backend, hook))
            return paused.principal(principal).claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        claims = await asyncio.gather(
            contend(first, lambda: reached_first_statement.wait(_CONTENDED_CLAIM_WAIT_SECONDS)),
            contend(second, reached_first_statement.set),
        )

        assert all(claimed is not None for claimed in claims)
        unattempted = [claimed for claimed in claims if claimed is not None and not claimed.attempted]
        assert len(unattempted) == 1, (
            f"{len(unattempted)} of two concurrent claims were told the row was unattempted, "
            "and every one of them would have sent without reading the room first"
        )

    async def test_two_connections_that_both_find_the_row_unbound_produce_one_winner(
        self,
        rival_stores: RivalStores,
    ) -> None:
        """Ownership of the acknowledgement has to come from the write itself.

        The sequential test above and this one prove different halves of the
        same rule and neither is redundant. That one acknowledges twice in a
        row, so the second caller meets a row that is already bound and is told
        so; what it proves is that being told costs it the terminal record too.
        It cannot prove this half, because by the time it runs there is no race
        left to lose -- the losing implementation below passes it untouched.

        This half is what happens when nobody has been told anything yet. Two
        connections against one PostgreSQL database can both observe an unbound
        row, and an implementation that reads the column and then updates it
        hands each of them a win: the outbox keeps whichever event landed last,
        while the caller that committed first goes on reporting its own. That
        report is what every downstream record is built from, so the outbox and
        the terminal record end up naming different events -- the disagreement
        both of these tests exist to forbid, arrived at from the side the
        sequential one cannot reach.

        Both racers are parked on the row before either may write, so the
        interleaving is produced rather than hoped for. Letting them start
        freely would usually have the second one read a row the first had
        already bound, and the broken implementation would decline correctly
        for a reason that says nothing about contention.
        """
        principal_id = "agent@alice"
        first = rival_stores.first.principal(principal_id)
        second = rival_stores.second.principal(principal_id)
        await first.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        async def acknowledge(store: PrincipalStore, event_id: str) -> DeliveryAcknowledgement:
            return await store.acknowledge_delivery(
                turn_id="turn-1",
                stage=DeliveryStage.FINAL,
                event_id=event_id,
                terminal_turn=TerminalTurnWrite(
                    agent_name="general",
                    index_event_ids=("$source",),
                    anchor_event_id="$source",
                    record_json=json.dumps({"response_event_id": event_id}),
                ),
            )

        with _outbox_row_held(rival_stores.database_url, principal_id=principal_id, turn_id="turn-1"):
            racers = [
                asyncio.create_task(acknowledge(first, "$first")),
                asyncio.create_task(acknowledge(second, "$second")),
            ]
            await _await_queued_racers(
                rival_stores.database_url,
                application_name=rival_stores.racer_application_name,
                expected=len(racers),
            )
        reported = await asyncio.gather(*racers)

        stored = await first.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        winner = stored.acknowledged_event_id
        assert winner in {"$first", "$second"}, "the row names an event neither caller sent"
        assert [acknowledged.settled_event_id for acknowledged in reported] == [winner, winner], (
            "a caller reported an event it did not bind the row to"
        )
        assert [acknowledged.bound for acknowledged in reported].count(True) == 1, (
            "both callers were told their own write is what bound the row"
        )
        rows = await rival_stores.first.turn_records("general").load_all()
        assert [json.loads(row[2])["response_event_id"] for row in rows] == [winner], (
            "the terminal record names an event the outbox row does not"
        )

    async def test_the_transaction_id_is_derived_not_random(self) -> None:
        """The transaction id is derived not random."""
        first = delivery_transaction_id("agent@alice", "turn-1", "final")
        second = delivery_transaction_id("agent@alice", "turn-1", "final")
        other_stage = delivery_transaction_id("agent@alice", "turn-1", "initial")

        assert first == second
        assert first != other_stage

    async def test_enqueue_returns_the_same_transaction_across_restarts(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Enqueue returns the same transaction across restarts."""
        first = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )
        second = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("answer"),
        )

        assert first == second

    async def test_an_unattempted_delivery_can_still_change(self, alice: PrincipalStore) -> None:
        """An unattempted delivery can still change."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("draft"),
        )
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("final"),
        )

        claimed = await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert claimed is not None
        assert claimed.payload["body"] == "final"

    async def test_claiming_freezes_the_payload(self, alice: PrincipalStore) -> None:
        """Claiming freezes the payload.

        The case this closes: Matrix accepted the old text, and the regenerated
        text could never become visible under the same transaction ID.
        """
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("regenerated"),
        )

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.payload["body"] == "sent"

    async def test_reclaiming_sends_the_identical_delivery(self, alice: PrincipalStore) -> None:
        """Everything that goes on the wire is frozen; the claim state is not.

        The payload and the transaction ID are the reason claiming exists, and
        a second claim must reproduce them exactly. The attempt and the device
        are the opposite: they describe who took the row and from where, so the
        second claim reports the first one's work rather than repeating the
        blank state it started from.
        """
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        first = await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.record_sending_device(turn_id="turn-1", stage=DeliveryStage.FINAL, device_id="DEVICE1")
        second = await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert first is not None
        assert second is not None

        assert replace(first, attempted=True, sending_device_id="DEVICE1") == second
        assert first.payload == second.payload
        assert first.transaction_id == second.transaction_id

        assert not first.attempted
        assert first.sending_device_id is None
        assert second.attempted
        assert second.sending_device_id == "DEVICE1"

    async def test_unacknowledged_deliveries_are_replayable(self, alice: PrincipalStore) -> None:
        """Unacknowledged deliveries are replayable."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)

        assert [d.turn_id for d in await alice.unacknowledged_deliveries()] == ["turn-1"]

        await alice.acknowledge_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            event_id="$sent",
        )

        assert await alice.unacknowledged_deliveries() == ()

    async def test_acknowledgement_keeps_the_first_event_id(self, alice: PrincipalStore) -> None:
        """Acknowledgement keeps the first event id."""
        await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload=text("sent"),
        )
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.acknowledge_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL, event_id="$first")
        await alice.acknowledge_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL, event_id="$second")

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$first"


class TestApprovalCards:
    """A card the bot sent stays answerable until its decision lands."""

    @staticmethod
    def card(event_id: str, *, sender: str = ALICE) -> dict[str, object]:
        """Return one approval-card event source."""
        return {
            "event_id": event_id,
            "sender": sender,
            "type": "io.mindroom.tool_approval",
            "content": {"approval_id": event_id.lstrip("$"), "status": "pending"},
        }

    @classmethod
    def transaction(cls, event_id: str) -> str:
        """Return the transaction a card with this event id was sent under."""
        return f"txn{event_id}"

    @classmethod
    async def remember(cls, store: PrincipalStore, event_id: str, *, sender: str = ALICE) -> None:
        """Leave one card in the state a completed send leaves it: claimed, attempted, acknowledged."""
        card = cls.card(event_id, sender=sender)
        await store.claim_approval_card(
            room_id=ROOM,
            transaction_id=cls.transaction(event_id),
            card=card,
        )
        await store.mark_approval_card_attempted(
            transaction_id=cls.transaction(event_id),
            sending_device_id=DEVICE,
        )
        await store.acknowledge_approval_card(
            transaction_id=cls.transaction(event_id),
            card_event_id=event_id,
            card=card,
        )

    async def test_a_remembered_card_reads_back_whole(self, alice: PrincipalStore) -> None:
        """A remembered card reads back whole, and unanswered."""
        await self.remember(alice, "$card")

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.card == self.card("$card")
        assert stored.resolution is None
        assert stored.card_event_id == "$card"

    async def test_a_claim_is_recoverable_before_anything_knows_its_event(self, alice: PrincipalStore) -> None:
        """The window a crash lands in holds a row, not a stranded card.

        Nothing can look this card up by event id yet, because no event id
        exists -- but the room scan startup drives sees it, which is the whole
        point of writing the row before the send.

        It carries no attempt and no device, because neither has happened. That
        is the state that proves nothing reached the room, and it is the only
        one a recovery pass may retire without asking the homeserver anything.
        """
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn",
            card=self.card("$card"),
        )

        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [(entry.transaction_id, entry.card_event_id) for entry in scanned] == [("txn", None)]
        assert [(entry.attempted, entry.sending_device_id) for entry in scanned] == [(False, None)]
        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None

    async def test_an_attempt_records_the_device_the_transaction_belongs_to(self, alice: PrincipalStore) -> None:
        """The marker and the device are one fact, committed before the send.

        A crash after this leaves a row that says something may already be in
        the room and whose namespace it went out under, which is exactly what
        recovery needs to decide between repeating the transaction and reading
        the room.
        """
        await alice.claim_approval_card(room_id=ROOM, transaction_id="txn", card=self.card("$card"))

        assert await alice.mark_approval_card_attempted(transaction_id="txn", sending_device_id=DEVICE) is True

        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [(entry.attempted, entry.sending_device_id) for entry in scanned] == [(True, DEVICE)]

    async def test_an_attempt_on_a_withdrawn_row_reports_that_it_marked_nothing(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A row a fence removed must not be sent under.

        The claim is what accounts for a card in the room, so a send made after
        the row went would put a clickable prompt somewhere nothing owns it --
        the state claiming before sending exists to make impossible.
        """
        await alice.claim_approval_card(room_id=ROOM, transaction_id="txn", card=self.card("$card"))
        await alice.forget_approval_card(transaction_id="txn")

        assert await alice.mark_approval_card_attempted(transaction_id="txn", sending_device_id=DEVICE) is False

    async def test_a_second_claim_cannot_walk_an_attempt_back(self, alice: PrincipalStore) -> None:
        """Claiming again over an attempted row would erase the fact that it may be visible.

        The conflict clause does nothing on purpose. If a retried claim reset
        the marker, recovery would read a row whose card is in the room as one
        that provably never left, and drop it without expiring it.
        """
        await alice.claim_approval_card(room_id=ROOM, transaction_id="txn", card=self.card("$card"))
        await alice.mark_approval_card_attempted(transaction_id="txn", sending_device_id=DEVICE)

        await alice.claim_approval_card(room_id=ROOM, transaction_id="txn", card=self.card("$card"))

        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [(entry.attempted, entry.sending_device_id) for entry in scanned] == [(True, DEVICE)]

    async def test_a_claim_cannot_carry_a_decision_before_its_send_returns(self, alice: PrincipalStore) -> None:
        """A decision is recorded against an event, so there is nothing to record against yet.

        Letting one land would mean answering a card whose place in the room is
        still unknown, and the answer would have no event to be shown on.
        """
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn",
            card=self.card("$card"),
        )

        refused = await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        assert refused.recorded is False
        assert refused.resolution is None

    async def test_acknowledging_twice_keeps_the_first_event(self, alice: PrincipalStore) -> None:
        """Two event ids for one transaction means the repeat was not collapsed.

        The homeserver only guarantees that within a device, so a repeat after
        a re-login can produce a second card. The first is the one the user has
        been looking at, and moving the row onto the second would abandon it.
        """
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn",
            card=self.card("$card"),
        )
        await alice.acknowledge_approval_card(transaction_id="txn", card_event_id="$card", card=self.card("$card"))
        await alice.acknowledge_approval_card(
            transaction_id="txn",
            card_event_id="$second",
            card=self.card("$second"),
        )

        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$second") is None
        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.card == self.card("$card")

    async def test_acknowledging_records_what_the_room_actually_shows(self, alice: PrincipalStore) -> None:
        """The body is corrected once no repeat can present it again.

        Up to the acknowledgement it had to stay frozen; after it, the send may
        have diverged from what was claimed -- oversized arguments become a
        sidecar reference -- and every later read compares the row to the room.
        """
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn",
            card=self.card("$card"),
        )
        sent = {**self.card("$card"), "content": {"approval_id": "card", "status": "pending", "approvable": False}}
        await alice.acknowledge_approval_card(transaction_id="txn", card_event_id="$card", card=sent)

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.card == sent

    async def test_a_card_is_droppable_whether_or_not_it_was_ever_sent(self, alice: PrincipalStore) -> None:
        """A claim whose send failed has no event id and still has to be removable.

        Keying the delete on the event id would silently match nothing here,
        and the row would come back on every startup as a card to resend.
        """
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn-unsent",
            card=self.card("$unsent"),
        )
        await self.remember(alice, "$sent")

        await alice.forget_approval_card(transaction_id="txn-unsent")

        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [entry.card_event_id for entry in scanned] == ["$sent"]

        await alice.forget_approval_card(transaction_id=self.transaction("$sent"))
        assert await alice.pending_approval_cards(room_id=ROOM) == ()

    async def test_a_recorded_decision_reads_back_with_the_card(self, alice: PrincipalStore) -> None:
        """A card keeps its decision until the room is known to show it.

        The decision is written before the Matrix edit is attempted, so this is
        what a crash between the two leaves behind, and it is what tells the
        next startup to redeliver rather than expire.
        """
        await self.remember(alice, "$card")
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.resolution == {"status": "approved"}
        assert stored.card == self.card("$card")
        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [entry.resolution for entry in scanned] == [{"status": "approved"}]

    async def test_recording_a_decision_reports_that_it_committed(self, alice: PrincipalStore) -> None:
        """A caller must not have to infer a commit from the absence of an error.

        Whether the tool runs turns on this answer, and the write is a guarded
        update that can decline silently.
        """
        await self.remember(alice, "$card")

        recorded = await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        assert recorded.recorded is True
        assert recorded.resolution == {"status": "approved"}

    async def test_a_second_decision_does_not_replace_the_first(self, alice: PrincipalStore) -> None:
        """The committed decision is the one that stands.

        A retry after a failed edit resends what was decided; letting a later
        write through would let a second click overwrite a decision whose tool
        already ran. The refusal is reported along with the decision that won,
        because a caller told only that no exception occurred would go on to
        show and act on the decision the row rejected.
        """
        await self.remember(alice, "$card")
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        refused = await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "denied"})

        assert refused.recorded is False
        assert refused.resolution == {"status": "approved"}
        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.resolution == {"status": "approved"}

    async def test_a_decision_on_an_unknown_card_records_nothing(self, alice: PrincipalStore) -> None:
        """Resolving a card that was never stored must not create one.

        The update matches no row and raises nothing, so the only thing that
        can stop the caller from treating this as a commit is being told that
        no row carries the decision.
        """
        unrecorded = await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        assert unrecorded.recorded is False
        assert unrecorded.resolution is None
        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None

    async def test_a_forgotten_cards_decision_is_no_longer_recordable(self, alice: PrincipalStore) -> None:
        """A dropped row is as unrecordable as one that never existed.

        The two zero-row causes stay distinguishable: this one has nothing to
        report back, where a card that already decided reports what it decided.
        """
        await self.remember(alice, "$card")
        await alice.forget_approval_card(transaction_id=self.transaction("$card"))

        unrecorded = await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        assert unrecorded.recorded is False
        assert unrecorded.resolution is None

    async def test_one_principal_cannot_record_a_decision_on_anothers_card(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Another bot's card is not a row this one may answer, or claim to have."""
        alice = journal_store.principal("agent@alice")
        bob = journal_store.principal("agent@bob")
        await self.remember(alice, "$card")

        unrecorded = await bob.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})

        assert unrecorded.recorded is False
        assert unrecorded.resolution is None
        stored = await alice.pending_approval_card(room_id=ROOM, card_event_id="$card")
        assert stored is not None
        assert stored.resolution is None

    async def test_a_forgotten_card_is_gone(self, alice: PrincipalStore) -> None:
        """Resolving a card is what removes it, so presence means pending."""
        await self.remember(alice, "$card")
        await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})
        await alice.forget_approval_card(transaction_id=self.transaction("$card"))

        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None
        assert await alice.pending_approval_cards(room_id=ROOM) == ()

    async def test_a_card_is_not_readable_from_another_room(self, alice: PrincipalStore) -> None:
        """A card belongs to the room it was sent in."""
        await self.remember(alice, "$card")

        assert await alice.pending_approval_card(room_id=OTHER_ROOM, card_event_id="$card") is None
        assert await alice.pending_approval_cards(room_id=OTHER_ROOM) == ()

    async def test_claiming_twice_keeps_the_first_card(self, alice: PrincipalStore) -> None:
        """A repeated claim must not rewrite a body the homeserver may hold.

        The claim is what a repeat send would present again, so replacing it
        would let a retry post different content under a transaction the
        homeserver has already accepted.
        """
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn",
            card=self.card("$card"),
        )
        await alice.claim_approval_card(
            room_id=ROOM,
            transaction_id="txn",
            card={**self.card("$card"), "sender": BOB},
        )

        # Read while it is still frozen. Acknowledging first would rewrite the
        # body with whatever was sent and hide a claim that had been replaced.
        scanned = await alice.pending_approval_cards(room_id=ROOM)
        assert [entry.card["sender"] for entry in scanned] == [ALICE]

    async def test_a_rooms_cards_come_back_oldest_first(self, alice: PrincipalStore) -> None:
        """Startup expiry walks the room's cards in the order they were sent."""
        for index in range(3):
            await self.remember(alice, f"$card-{index}")

        stored = await alice.pending_approval_cards(room_id=ROOM)
        assert [entry.card["event_id"] for entry in stored] == ["$card-0", "$card-1", "$card-2"]

    async def test_the_scan_honors_its_limit(self, alice: PrincipalStore) -> None:
        """A bounded scan is what lets the caller walk a room one page at a time."""
        for index in range(5):
            await self.remember(alice, f"$card-{index}")

        assert len(await alice.pending_approval_cards(room_id=ROOM, limit=2)) == 2

    async def test_the_scan_resumes_past_the_page_it_already_read(self, alice: PrincipalStore) -> None:
        """A card whose settlement failed keeps its row, so paging cannot restart.

        The row is retained on purpose while its decision may be undelivered,
        which leaves it inside this query's window. Without somewhere to resume
        from, a page of such rows is handed back forever and every card behind
        them is never seen.
        """
        for index in range(5):
            await self.remember(alice, f"$card-{index}")

        walked: list[str] = []
        cursor: tuple[int, str] | None = None
        while True:
            page = await alice.pending_approval_cards(room_id=ROOM, limit=2, after=cursor)
            if not page:
                break
            cursor = (page[-1].created_at_ns, page[-1].transaction_id)
            walked.extend(str(entry.card["event_id"]) for entry in page)

        assert walked == [f"$card-{index}" for index in range(5)]

    async def test_rejoining_makes_the_previous_memberships_cards_unrecoverable(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A card asked in a membership the bot has left is not this one's to answer.

        Expiring it would edit a message in a room the bot has since rejoined,
        answering a question nobody in the current membership asked.
        """
        await self.remember(alice, "$card")

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        assert await alice.pending_approval_card(room_id=ROOM, card_event_id="$card") is None
        assert await alice.pending_approval_cards(room_id=ROOM) == ()

    async def test_rejoining_drops_the_previous_memberships_cards(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A card the fence made unanswerable has to go, not merely stop being visible.

        Every read of a card is filtered to the current membership, so a row
        the fence steps over has no reader left and nothing that will ever
        remove it. Nor is it inert while it sits there: a decision is recorded
        against the card's event rather than against the epoch, so the
        stranded row keeps accepting answers that no read can retrieve and no
        startup can redeliver.
        """
        await self.remember(alice, "$card")

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        unanswerable = await alice.resolve_approval_card(card_event_id="$card", resolution={"status": "approved"})
        assert unanswerable.recorded is False
        assert unanswerable.resolution is None

    async def test_rejoining_drops_only_the_room_that_was_left(
        self,
        alice: PrincipalStore,
    ) -> None:
        """One room's fence says nothing about a card waiting in another room."""
        await self.remember(alice, "$card")
        await alice.claim_approval_card(
            room_id=OTHER_ROOM,
            transaction_id="txn-other",
            card=self.card("$other"),
        )
        await alice.acknowledge_approval_card(
            transaction_id="txn-other",
            card_event_id="$other",
            card=self.card("$other"),
        )

        await alice.fence_departure(ROOM, source=DepartureSource.LOCAL)

        stored = await alice.pending_approval_card(room_id=OTHER_ROOM, card_event_id="$other")
        assert stored is not None
        assert stored.transaction_id == "txn-other"

    async def test_one_principals_cards_are_invisible_to_another(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Two bots in one database do not answer each other's approvals."""
        alice = journal_store.principal("agent@alice")
        bob = journal_store.principal("agent@bob")
        await self.remember(alice, "$card")

        assert await bob.pending_approval_card(room_id=ROOM, card_event_id="$card") is None
        assert await bob.pending_approval_cards(room_id=ROOM) == ()
        assert await alice.pending_approval_cards(room_id=ROOM) != ()


class TestConcurrency:
    """Concurrent conversations must not produce lock failures."""

    async def test_fifty_concurrent_conversations_admit_cleanly(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Fifty concurrent conversations admit cleanly."""

        async def conversation(index: int) -> None:
            for step in range(10):
                inbound, projected = message(
                    f"$c{index:02d}-{step}",
                    ts=1_000 + step,
                    thread_id=f"$thread-{index:02d}",
                )
                await alice.admit(inbound, projected)

        await asyncio.gather(*(conversation(index) for index in range(50)))

        for index in range(50):
            assert len(await bodies(alice, thread_id=f"$thread-{index:02d}")) == 10

    async def test_concurrent_admissions_of_one_event_yield_one_pending(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Concurrent admissions of one event yield one pending."""
        inbound, projected = message("$contended")
        results = await asyncio.gather(*(alice.admit(inbound, projected) for _ in range(8)))

        assert results.count(AdmissionResult.ADMITTED) == 1
        assert [event.event_id for event in await alice.pending()] == ["$contended"]


class TestOffloadedStatementsOutliveTheAwaitThatStartedThem:
    """A cancelled await cannot stop a worker thread, so it must not hand on what that thread is using.

    Every backend statement runs on an ``asyncio.to_thread`` worker no
    cancellation can reach. What each rule below pins is one thing the await
    was holding while the thread ran -- the writer lock, a pooled connection,
    a connection about to be closed -- and that none of them may change hands
    until the statement has actually stopped.

    Nothing here substitutes anything for ``to_thread``: the crash these guard
    against is a real connection being taken away from a real statement, and a
    faked worker has no connection to take away.
    """

    async def test_cancellation_retrieves_a_completed_worker_failure(self) -> None:
        """The caller keeps cancellation while the worker's failure remains observable."""
        work: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiting = asyncio.create_task(settled(work))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.sleep(0)
        worker_error = RuntimeError("worker failed after cancellation")
        work.set_exception(worker_error)

        with pytest.raises(asyncio.CancelledError) as cancellation:
            await waiting

        assert cancellation.value.__cause__ is worker_error

    async def test_closing_the_store_waits_for_a_write_already_on_a_worker_thread(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Closing the store must not close the connection a write is executing on.

        Closing is not only a shutdown path -- a bot closes its store on every
        config reload -- so this is a routine edit landing while a turn is
        being written. SQLite answers a connection closed underneath a live
        statement with a segmentation fault rather than an exception, which
        takes the whole process with it.
        """
        running = threading.Event()
        release = threading.Event()

        def busy(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            _hold_the_connection(transaction, running, release)
            return "landed"

        writing = asyncio.create_task(journal_store.backend.write(busy))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        closing = asyncio.create_task(journal_store.close())
        closed_early = await _finished_within_grace(closing)
        release.set()
        await closing

        assert not closed_early, "close() returned while a write was still executing on the connection"
        assert await writing == "landed"

    async def test_closing_the_store_waits_for_a_read_already_on_a_worker_thread(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A close that drains only the writer leaves the same crash reachable from a read.

        Reads run on their own connections, which ``close()`` closes too, so
        the reader path owns this rule separately from the writer path.
        """
        running = threading.Event()
        release = threading.Event()

        def busy(transaction: Transaction) -> str:
            _hold_the_connection(transaction, running, release)
            return "read"

        reading = asyncio.create_task(journal_store.backend.read(busy))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        closing = asyncio.create_task(journal_store.close())
        closed_early = await _finished_within_grace(closing)
        release.set()
        await closing

        assert not closed_early, "close() returned while a read was still executing on the connection"
        assert await reading == "read"

    async def test_a_cancelled_write_does_not_return_while_its_statement_runs(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A cancelled write owns its transaction until the transaction ends.

        Returning early is what lets the caller's next act -- releasing the
        writer lock, or closing the connection -- happen behind a statement
        that is still running.
        """
        running = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            running.set()
            release.wait(_WORKER_WAIT_SECONDS)
            finished.set()
            return "landed"

        writing = asyncio.create_task(journal_store.backend.write(slow))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        writing.cancel()
        returned_early = await _finished_within_grace(writing)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing

        assert not returned_early, "the cancelled write returned while its statement was still running"
        assert finished.is_set()

    async def test_a_cancelled_read_does_not_return_while_its_statement_runs(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A cancelled read owns its connection until its statement ends.

        On a pooled backend the connection goes back to the pool the moment
        this returns, so returning early hands a live connection to whoever
        reads next.
        """
        running = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(transaction: Transaction) -> str:
            transaction.fetchall("SELECT 1 AS one")
            running.set()
            release.wait(_WORKER_WAIT_SECONDS)
            finished.set()
            return "read"

        reading = asyncio.create_task(journal_store.backend.read(slow))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        reading.cancel()
        returned_early = await _finished_within_grace(reading)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reading

        assert not returned_early, "the cancelled read returned while its statement was still running"
        assert finished.is_set()

    async def test_a_cancelled_write_keeps_the_next_write_out_of_its_transaction(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """The next write must not execute inside a cancelled write's open transaction.

        Two writes sharing one transaction corrupt each other in both
        directions, and both were observed: the second write committed the
        first one's statements along with its own, and the first write's
        ``rollback`` threw away a second write that had already reported
        success to its caller. On a durable turn ledger either one means a
        message answered twice or an answer that was never recorded.
        """
        first_running = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def first(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            first_running.set()
            release_first.wait(_WORKER_WAIT_SECONDS)
            msg = "the cancelled write fails"
            raise RuntimeError(msg)

        def second(transaction: Transaction) -> str:
            second_started.set()
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@bob", OTHER_ROOM, 1))
            return "second landed"

        cancelled = asyncio.create_task(journal_store.backend.write(first))
        await asyncio.to_thread(first_running.wait, _WORKER_WAIT_SECONDS)
        cancelled.cancel()
        following = asyncio.create_task(journal_store.backend.write(second))
        await _finished_within_grace(following)
        trespassed = second_started.is_set()
        release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        assert not trespassed, "the next write ran inside the cancelled write's still-open transaction"
        assert await following == "second landed"
        assert await _membership_principals(journal_store) == ["agent@bob"], (
            "the failed write's rows survived, or the successful write's rows did not"
        )


class TestClosingAnswersEveryWriteItWillNotRun:
    """A write is run or refused, never abandoned -- however many are in flight.

    SQLite-only because the enqueue is: the PostgreSQL backend serializes on a
    lock, whose waiters are all woken by the releases that follow it, and has
    no queue for a producer to be stranded outside of. The rule holds on both
    backends; only one of them can be asked this question.
    """

    async def test_no_write_is_left_waiting_when_the_store_closes_under_load(
        self,
        tmp_path: Path,
    ) -> None:
        """Every write outstanding at a close is answered by that close.

        Closing is routine -- a bot closes its store on every config reload --
        so this is an ordinary edit landing while the journal is busy. The
        writer queue was bounded, and ``close()`` freed one slot per entry it
        drained: producers parked in ``put`` woke afterwards, enqueued onto a
        queue whose consumer had already been cancelled, and waited on a future
        nothing would ever settle. Producers past the bound were never woken at
        all, so re-checking the closed flag after the ``put`` would not have
        reached them either. Both outcomes are a reload the bot never returns
        from, holding a turn that Matrix has already been told was accepted.
        """
        backend = SqliteBackend.open(tmp_path / "closing.db")
        running = threading.Event()
        release = threading.Event()

        def busy(transaction: Transaction) -> str:
            transaction.execute(_INSERT_MEMBERSHIP, ("agent@alice", ROOM, 1))
            _hold_the_connection(transaction, running, release)
            return "landed"

        def queued(transaction: Transaction) -> str:
            transaction.fetchall("SELECT 1 AS one")
            return "landed"

        held = asyncio.create_task(backend.write(busy))
        await asyncio.to_thread(running.wait, _WORKER_WAIT_SECONDS)
        outstanding = [
            asyncio.create_task(backend.write(queued)) for _ in range(_WRITES_OUTNUMBERING_THE_OLD_QUEUE_BOUND)
        ]
        # One pass of the loop, so every producer has reached the point where
        # it is either queued or parked before the close starts.
        await asyncio.sleep(0)

        closing = asyncio.create_task(backend.close())
        await _finished_within_grace(closing)
        release.set()
        await closing
        answered, abandoned = await asyncio.wait(outstanding, timeout=_SETTLEMENT_WAIT_SECONDS)
        # Read eagerly rather than inside the assertion below, which stops at
        # the first answer it dislikes and would leave the rest reported as
        # exceptions nobody retrieved.
        refusals = [task.exception() for task in answered]
        await _release_writes_the_store_abandoned(backend, abandoned)

        assert not abandoned, f"{len(abandoned)} writes were left waiting on a store that had closed"
        assert await held == "landed"
        assert all(isinstance(refusal, RuntimeError) for refusal in refusals), (
            "a write the closed store never ran reported something other than a refusal"
        )


class TestTheJournalIsAtLeastAsDurableAsWhatCertifiesIt:
    """A committed row must survive every crash the checkpoint naming it survives.

    SQLite-only because the setting is. PostgreSQL commits durably by default
    and nothing here relaxes that, so its half of this rule is not configured
    but inherited.
    """

    async def test_the_writer_commits_reach_the_disk_and_the_readers_do_not_pay_for_it(
        self,
        tmp_path: Path,
    ) -> None:
        """The one connection that commits fsyncs; the ones that never commit do not.

        A Matrix sync checkpoint goes to disk through ``write_json_file_durable``,
        which fsyncs the record and the directory holding it. Under
        ``synchronous = NORMAL`` the WAL frames that checkpoint certifies do
        not, so a host reset can leave a fsynced token that resumes past events
        the journal no longer holds. Nothing asks for them again: the token
        says they were consumed, its store generation still matches, and a
        history debt is only recorded for a recovery gap the certifier can
        still see. The events are simply never answered.

        Readers are excluded rather than overlooked. Every statement that
        writes runs inside ``write``, on the writer connection, so a reader
        would pay an fsync it has nothing to flush.
        """
        backend = SqliteBackend.open(tmp_path / "durable.db")
        try:
            writer = await backend.write(_synchronous_mode)
            reader = await backend.read(_synchronous_mode)
            wal = await backend.write(_journal_mode)
        finally:
            await backend.close()

        assert writer == _SQLITE_SYNCHRONOUS_FULL, "the journal writer does not fsync the commits it reports as landed"
        assert reader == _SQLITE_SYNCHRONOUS_NORMAL, "a reader is paying for durability it cannot use"
        assert wal == "wal", "the durability this pins is the durability of WAL mode"


class TestConnectionSecretsStayOutOfLogs:
    """A DSN carries a password, so it must not ride along in a repr."""

    async def test_the_postgres_backend_repr_omits_its_connection_string(self) -> None:
        """The Postgres backend repr omits its connection string."""
        pytest.importorskip("psycopg")
        from mindroom.event_journal.postgres_backend import PostgresBackend  # noqa: PLC0415 - keeps psycopg optional

        backend = PostgresBackend(database_url="postgresql://someone:hunter2@db.example:5432/journal")

        rendered = repr(backend)

        assert "hunter2" not in rendered
        assert "someone" not in rendered
        assert "db.example" not in rendered


class TestHotQueriesAreIndexCovered:
    """Every scan a running bot repeats must come from an index, ordering included."""

    # The shapes the code actually issues, ORDER BY included. An earlier
    # measurement of this dropped the trailing ORDER BY terms and reported all
    # of them clean; two were building a temporary b-tree on every pass. An
    # index that matches only the prefix of an ORDER BY cannot satisfy it, on
    # either backend.
    _QUERIES: ClassVar[dict[str, str]] = {
        "worker pending scan": (
            "SELECT * FROM journal_events WHERE principal_id=? AND state='pending' "
            "AND receipt_order>? ORDER BY receipt_order LIMIT 50"
        ),
        "replay guard thread scan": (
            "SELECT * FROM journal_events WHERE principal_id=? AND room_id=? AND thread_id=? "
            "AND state='pending' AND origin_server_ts>? AND event_id<>?"
        ),
        "projection page": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND thread_id=? "
            "ORDER BY created_ts DESC, logical_event_id DESC LIMIT 50"
        ),
        "projection page after a cursor": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND thread_id=?"  # noqa: S608 - the production clause, not input
            f"{_CONVERSATION_CURSOR_CLAUSE} ORDER BY created_ts DESC, logical_event_id DESC LIMIT 50"
        ),
        "revision point lookup": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND revision_event_id=?"
        ),
        "refresh-owing scan": (
            "SELECT * FROM visible_messages WHERE principal_id=? AND room_id=? AND thread_id=? "
            "AND refresh_token IS NOT NULL"
        ),
        "outbox recovery scan": (
            "SELECT * FROM response_outbox WHERE principal_id=? AND acknowledged_event_id IS NULL "
            "ORDER BY created_at_ns, turn_id, stage LIMIT 50"
        ),
        "approval card scan": (
            "SELECT * FROM approval_cards WHERE principal_id=? AND room_id=? "
            "ORDER BY created_at_ns, transaction_id LIMIT 50"
        ),
        "approval card point lookup": ("SELECT * FROM approval_cards WHERE principal_id=? AND card_event_id=?"),
    }

    async def test_no_hot_query_falls_back_to_a_scan_or_a_temporary_sort(self, tmp_path: Path) -> None:
        """No hot query falls back to a table scan or a temporary sort."""
        database = sqlite3.connect(tmp_path / "plans.db")
        for statement in schema_statements(SQLITE_DIALECT):
            database.execute(statement)

        offenders = {}
        for name, sql in self._QUERIES.items():
            params = tuple("x" for _ in range(sql.count("?")))
            plan = " | ".join(row[-1] for row in database.execute("EXPLAIN QUERY PLAN " + sql, params))
            if "TEMP B-TREE" in plan or "SCAN " in plan:
                offenders[name] = plan

        assert offenders == {}

    async def test_a_deep_page_seeks_its_cursor_instead_of_filtering_down_to_it(self, tmp_path: Path) -> None:
        """A deep page seeks its cursor rather than rescanning the conversation to reach it.

        Being on an index is not the same as being bounded by one, and the test
        above cannot tell the two apart: it rejects `SCAN` and `TEMP B-TREE`,
        and a cursor the backend cannot position on is neither. Spelled as the
        disjunction `created_ts < ? OR (created_ts = ? AND logical_event_id <
        ?)`, the cursor is a filter, so every page re-enters the conversation at
        its tip and walks forward through everything newer than it. The plan
        still says SEARCH, the index is still covering, and a full walk is
        quadratic.

        That walk is what an export runs, and its ceiling is 1,000,000
        messages. Measured on this schema at 500 messages a page: 0.75 s
        against 0.037 s at 100,000 messages and 77.1 s against 0.38 s at
        1,000,000, the disjunction growing 102.9x for the last 10x of messages
        where the row value grows 10.1x.

        So the plan has to name the cursor column. Under the disjunction the
        index is entered on the three equality columns alone and `created_ts`
        appears nowhere in it, which is what makes this assertion fail the
        moment the spelling drifts back.
        """
        database = sqlite3.connect(tmp_path / "cursor-plan.db")
        for statement in schema_statements(SQLITE_DIALECT):
            database.execute(statement)
        sql = self._QUERIES["projection page after a cursor"]

        plan = " | ".join(
            row[-1] for row in database.execute("EXPLAIN QUERY PLAN " + sql, tuple("x" for _ in range(sql.count("?"))))
        )

        assert "created_ts" in plan, plan

    async def test_every_ordered_index_column_carries_the_byte_order_pin_on_postgres(self) -> None:
        """Every ordered index column carries the byte-order pin on PostgreSQL.

        A query-plan test can only measure the backend it runs on, and SQLite's
        default text collation is already byte order -- so an index whose pin
        failed to render looks perfect there and is silently unusable for
        ordering on PostgreSQL, which declines it and sorts instead. This checks
        the rendered DDL rather than a plan, because that is the part that
        differs.
        """
        rendered = [" ".join(s.split()) for s in schema_statements(POSTGRES_DIALECT)]
        indexes = [s for s in rendered if "CREATE INDEX" in s]

        assert not [s for s in rendered if "/*bytes*/" in s], "byte-order marker left unexpanded"
        ordered_text_indexes = [s for s in indexes if "turn_id" in s or "card_event_id" in s]
        assert ordered_text_indexes, "expected indexes over the unpinned text columns"
        for statement in ordered_text_indexes:
            assert 'COLLATE "C"' in statement, statement


class TestTurnRecordsLiveBesideTheTurnsTheyDescribe:
    """The first half of collapsing "has this turn finished?" onto one writer.

    Today that question is answered by the journal's pending set and by a
    JSON-file ledger, which cannot share a transaction and therefore settle at
    different moments. These rows are the same records in the database that
    settles the turns, so a future writer can commit both together. Nothing
    reads them yet on purpose: a dedupe substrate that is half migrated is one
    that can answer a message twice.
    """

    @staticmethod
    async def _stored(records: TurnRecordStore) -> dict[str, str]:
        """Return one agent's records keyed by the event that indexes each.

        Read through `load_all`, which is the whole read surface: a warm-up
        rebuilds the map once and answers every later question from memory, so
        the store has no point lookup for anything else to depend on.
        """
        return {index: record for index, _anchor, record in await records.load_all()}

    async def test_a_record_is_reachable_from_every_event_that_indexes_it(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A coalesced turn answers several sources and is found from any of them.

        Storing it by anchor alone would turn "was this source answered?" into
        a scan, and that question is asked on the ingress path for every event.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(
            index_event_ids=("$a", "$b", "$c"),
            anchor_event_id="$a",
            record_json='{"anchor_event_id": "$a"}',
        )

        assert await self._stored(records) == {
            "$a": '{"anchor_event_id": "$a"}',
            "$b": '{"anchor_event_id": "$a"}',
            "$c": '{"anchor_event_id": "$a"}',
        }

    async def test_an_index_the_turn_no_longer_answers_is_dropped(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A shrinking coalesced batch must not leave a source looking answered.

        This is the one direction that silently drops a user's message: a stale
        row would report "already handled" for a source the turn stopped
        accounting for, and nothing would ever answer it.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(
            index_event_ids=("$a", "$b"),
            anchor_event_id="$a",
            record_json='{"v": 1}',
        )

        await records.upsert(index_event_ids=("$a",), anchor_event_id="$a", record_json='{"v": 2}')

        assert await self._stored(records) == {"$a": '{"v": 2}'}

    async def test_a_re_anchored_record_leaves_no_row_under_its_old_anchor(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Dropping the anchor itself is the case a naive delete misses.

        Redaction and conflict projection re-anchor a record when the anchor is
        one of the sources being dropped -- `handled_turns` picks the last
        retained source instead. The rows to clean up are then filed under the
        *old* anchor, so a delete scoped to the anchor being written walks past
        them and the dropped source keeps reporting "already handled". Nothing
        would ever answer it.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(
            index_event_ids=("$a", "$b"),
            anchor_event_id="$a",
            record_json='{"v": 1}',
        )

        # `$a` is redacted away, so the record re-anchors onto `$b`.
        await records.upsert(index_event_ids=("$b",), anchor_event_id="$b", record_json='{"v": 2}')

        assert await self._stored(records) == {"$b": '{"v": 2}'}, "the old anchor's row survived re-anchoring"

    async def test_records_are_scoped_to_the_agent_not_the_matrix_identity(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """A re-login must not lose the proof that a message was answered.

        Every other table here is keyed per (agent, Matrix identity), because
        what it holds is only meaningful beside the sync that produced it. This
        one is not: it records that a turn is finished, which stays true when
        the bot logs in as a new device. Scoping it per principal would make
        such a bot answer its whole backlog a second time.

        Transactionality comes from sharing the database, which two views of
        the same store do regardless of how they are keyed.
        """
        first = journal_store.turn_records("agent")
        other_agent = journal_store.turn_records("other-agent")
        await first.upsert(index_event_ids=("$a",), anchor_event_id="$a", record_json='{"v": 1}')

        assert await self._stored(journal_store.turn_records("agent")) == {"$a": '{"v": 1}'}
        assert await self._stored(other_agent) == {}, "one agent read another's turn records"

    async def test_a_warm_up_reads_every_record_in_a_stable_order(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """Both backends have to rebuild the same map from the same rows.

        The order is pinned to byte order rather than the server's collation,
        for the reason every other scan here is: PostgreSQL's default collation
        does not agree with SQLite's about it, and a restart that rebuilt a
        differently ordered map would be a difference nothing else would catch.
        """
        records = journal_store.turn_records("agent")
        await records.upsert(index_event_ids=("$B",), anchor_event_id="$B", record_json='{"v": "B"}')
        await records.upsert(index_event_ids=("$a",), anchor_event_id="$a", record_json='{"v": "a"}')

        assert [index for index, _anchor, _json in await records.load_all()] == ["$B", "$a"]

    async def test_compaction_forgets_what_it_is_told_to(
        self,
        journal_store: EventJournalStore,
    ) -> None:
        """The ledger compacts terminal history, so these rows have to as well."""
        records = journal_store.turn_records("agent")
        await records.upsert(index_event_ids=("$a", "$b"), anchor_event_id="$a", record_json='{"v": 1}')

        await records.forget(index_event_ids=("$a",))

        assert await self._stored(records) == {"$b": '{"v": 1}'}


# Takes the journal's write lock on a database that is not yet in WAL, which is
# the shape of a bot creating the file, and holds it until told to let go.
# Raw ``sqlite3`` rather than a store, because a store enters WAL as it opens
# and so cannot hold a lock in the state this is here to hold one in.
_HOLD_FRESH_DATABASE_SOURCE = """
import sqlite3
import sys
import time
from pathlib import Path

database, held, release = (Path(argument) for argument in sys.argv[1:4])
database.parent.mkdir(parents=True, exist_ok=True)
connection = sqlite3.connect(database, isolation_level=None)
connection.execute("PRAGMA busy_timeout = 10000")
connection.execute("BEGIN IMMEDIATE")
connection.execute("CREATE TABLE IF NOT EXISTS seed (id INTEGER PRIMARY KEY)")
held.write_text("held")
while not release.exists():
    time.sleep(0.01)
connection.execute("COMMIT")
connection.close()
"""

# Holds a real store's real write transaction open, the way an export installing
# one large hydrated conversation holds it. Only the body of the transaction is
# stand-in: the connection, the queue, the ``BEGIN IMMEDIATE`` and the lock it
# takes are the production ones, and the lock is the whole subject.
_HOLD_STORE_WRITE_SOURCE = """
import asyncio
import sys
import time
from pathlib import Path

from mindroom.event_journal import EventJournalStore

database, held, release = (Path(argument) for argument in sys.argv[1:4])


def hold(transaction):
    transaction.execute("PRAGMA user_version = 7")
    held.write_text("held")
    while not release.exists():
        time.sleep(0.01)


async def main():
    store = EventJournalStore.open_sqlite(database)
    await store.backend.write(hold)
    await store.close()


asyncio.run(main())
"""


class TestCrossProcessWriters:
    """What a second OS process writing this journal does to the one beside it.

    The export is that process. It opens the bot's journal from its own
    interpreter and hydrates through it, so the single-writer queue -- which is
    per process -- is not between them. The busy timeout is, and these are the
    tests that say what that buys, because until they existed the timeout was
    load-bearing and unexercised: every other concurrency test here shares one
    interpreter, where the queue makes the writers take turns before SQLite is
    ever asked to.
    """

    @staticmethod
    @asynccontextmanager
    async def _holding_the_write_lock(source: str, tmp_path: Path) -> AsyncIterator[Path]:
        """Run a real second process holding this journal's write lock.

        Entered once that process has the lock and not before, so a body that
        contends is contending with something. Released on the way out however
        the body ended: a test that fails while another process is sitting on
        the lock should report why it failed, not time out waiting for a
        process only it can free.
        """
        script = tmp_path / "hold.py"
        script.write_text(source)
        database = tmp_path / "tracking" / "event_journal.db"
        held = tmp_path / "held"
        release = tmp_path / "release"
        holder = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            str(database),
            str(held),
            str(release),
        )
        try:
            while not held.exists():
                assert holder.returncode is None, "the second process exited before it took the write lock"
                await asyncio.sleep(0.01)
            yield release
        finally:
            release.write_text("release")
            async with asyncio.timeout(_HOLDER_EXIT_SECONDS):
                await holder.wait()

    async def test_opening_waits_out_a_write_lock_another_process_holds(self, tmp_path: Path) -> None:
        """A journal being written by another process is opened, not refused.

        Entering WAL is the one statement SQLite will not run the busy handler
        for, so before this the ten-second timeout the rest of the backend
        relies on was zero here: an export pass that started while the bot was
        still creating the database died on the spot, in single-digit
        milliseconds, having waited for nothing.

        The wait is proven rather than timed. An open that refuses instead of
        waiting is finished long before the grace window, so the assertion that
        it is still running is what separates the two -- and the lock is only
        released afterwards, which means the open can only have succeeded by
        having waited for it.
        """
        database = tmp_path / "tracking" / "event_journal.db"
        async with self._holding_the_write_lock(_HOLD_FRESH_DATABASE_SOURCE, tmp_path) as release:
            opening = asyncio.create_task(asyncio.to_thread(EventJournalStore.open_sqlite, database))
            if await _finished_within_grace(opening):
                # Awaiting it is how the refusal itself gets reported, rather
                # than a bare "this finished too early".
                await opening
                pytest.fail("opening the journal returned at once instead of waiting for the lock")
            release.write_text("release")
            store = await opening
        try:
            assert await admit(store.principal(ALICE), "$after") is AdmissionResult.ADMITTED
        finally:
            await store.close()

    async def test_admission_survives_a_write_lock_another_process_holds(self, tmp_path: Path) -> None:
        """A bot admits through an export's write, late rather than never.

        The guarantee the backend actually offers two processes, stated where
        it can be checked: contention delays an admission for as long as the
        other process's transaction runs, and the busy timeout is what turns
        that into a delay instead of a refusal. Past ten seconds it is a
        refusal, which ``journal_ingress`` turns into a declined event rather
        than an accepted one -- so what this pins is the bound, not an absence
        of one.
        """
        database = tmp_path / "tracking" / "event_journal.db"
        store = EventJournalStore.open_sqlite(database)
        try:
            principal = store.principal(ALICE)
            assert await admit(principal, "$before") is AdmissionResult.ADMITTED

            async with self._holding_the_write_lock(_HOLD_STORE_WRITE_SOURCE, tmp_path) as release:
                admitting = asyncio.create_task(admit(principal, "$during"))
                if await _finished_within_grace(admitting):
                    await admitting
                    pytest.fail("admission returned at once instead of waiting for the other writer")
                release.write_text("release")
                assert await admitting is AdmissionResult.ADMITTED

            assert await bodies(principal) == ["$before", "$during"]
        finally:
            await store.close()
