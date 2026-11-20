"""Reading a conversation again loads only the rows that changed, and never reads a row as it used to be."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio

from mindroom.event_journal import DepartureSource, EventJournalStore, EventKind, reads
from mindroom.matrix.conversation_hydration import ConversationReadError
from mindroom.matrix.conversation_reads import ConversationReader
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.journal_membership_helpers import admit_room_membership
from tests.test_event_journal_store import ALICE, ROOM, _PausingTransaction, admit, edit, projection, sidecar, text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.event_journal import ConversationPage, PrincipalStore, RefreshRequest
    from mindroom.event_journal.backend import Backend, Operation, Row, Transaction
    from mindroom.matrix.conversation_hydration import ConversationHydrator

pytestmark = pytest.mark.asyncio

ROOT = "$root"
REPLIES = 40
# The prompt path's window, far above every thread here.
WINDOW = 2_000


@dataclass(slots=True)
class _CountingTransaction:
    """One read transaction that counts the rows it returns with their stored content."""

    inner: Transaction
    loaded: list[int]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.inner.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        return self.inner.fetchone(sql, params)

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> tuple[Row, ...]:
        rows = self.inner.fetchall(sql, params)
        self.loaded[-1] += sum("content_json" in row.keys() for row in rows)  # noqa: SIM118 - sqlite3.Row
        return rows

    def signal(self, value: object) -> None:
        self.inner.signal(value)


@dataclass(slots=True)
class _CountingBackend:
    """The real backend, recording per read how many rows came back with their stored content."""

    inner: Backend
    loaded: list[int] = field(default_factory=list)

    async def write[T](self, operation: Operation[T]) -> T:
        return await self.inner.write(operation)

    async def read[T](self, operation: Operation[T]) -> T:
        self.loaded.append(0)
        return await self.inner.read(lambda transaction: operation(_CountingTransaction(transaction, self.loaded)))


@dataclass(slots=True)
class _Thread:
    """One principal's view of the thread under test, and what each of its reads loaded."""

    journal: EventJournalStore
    store: PrincipalStore
    backend: _CountingBackend

    async def read(self, *, limit: int = WINDOW) -> tuple[ConversationPage, int]:
        """Return the thread's newest page and how many rows the read loaded content for."""
        page = await self.store.read_conversation(room_id=ROOM, thread_id=ROOT, limit=limit)
        return page, self.backend.loaded[-1]

    async def bodies(self) -> list[str]:
        """Return the thread's visible bodies, oldest first."""
        page, _ = await self.read()
        return [str(message.content["body"]) for message in page.messages]


def _counted(store: EventJournalStore, *, page_cache: reads.PageCache | None = None) -> EventJournalStore:
    """Return a store over the same database whose reads are counted, reading through ``page_cache`` or none."""
    inner = store.backend
    backend = cast("Backend", _CountingBackend(inner.inner if isinstance(inner, _CountingBackend) else inner))
    return EventJournalStore(backend=backend, approval_signals=store.approval_signals, page_cache=page_cache)


def _thread(store: EventJournalStore, principal_id: str = "agent@alice") -> _Thread:
    return _Thread(store, store.principal(principal_id), cast("_CountingBackend", store.backend))


async def _seed(store: PrincipalStore, replies: int = REPLIES) -> None:
    """Admit a thread root and ``replies`` replies, a millisecond apart."""
    await admit(store, ROOT, ts=1_000, content=text("root"))
    for index in range(1, replies + 1):
        await admit(store, f"$reply-{index:03d}", ts=1_000 + index, thread_id=ROOT, content=text(f"reply {index}"))


@pytest_asyncio.fixture
async def thread(journal_database: Callable[[], EventJournalStore]) -> _Thread:
    """Return a seeded thread that has been read once."""
    opened = journal_database()
    thread = _thread(_counted(opened, page_cache=opened.page_cache))
    await _seed(thread.store)
    page, loaded = await thread.read()
    assert len(page.messages) == loaded == REPLIES + 1
    return thread


class TestRowsLoaded:
    """How much content a read loads is bounded by what changed, not by the thread's length."""

    async def test_reading_an_unchanged_thread_again_loads_no_content(self, thread: _Thread) -> None:
        """A second read of the same thread returns the same page without loading a row's content."""
        first, _ = await thread.read()
        second, loaded = await thread.read()

        assert loaded == 0
        assert second == first
        assert [message.content["body"] for message in second.messages][:2] == ["root", "reply 1"]

    async def test_two_reads_share_no_decoded_content(self, thread: _Thread) -> None:
        """Each page decodes its own content, so a reader that changes what it was handed changes nobody else's."""
        first, _ = await thread.read()
        cast("dict[str, object]", first.messages[0].content)["body"] = "changed by a reader"

        second, _ = await thread.read()
        assert second.messages[0].content["body"] == "root"

    async def test_a_new_reply_loads_only_itself(self, thread: _Thread) -> None:
        """A turn's new message costs one row, however long the thread is."""
        await admit(thread.store, "$new", ts=5_000, thread_id=ROOT, content=text("new reply"))

        page, loaded = await thread.read()
        assert loaded == 1
        assert [message.logical_event_id for message in page.messages[-2:]] == [f"$reply-{REPLIES:03d}", "$new"]
        assert page.messages[-1].content["body"] == "new reply"

    async def test_a_shorter_page_of_a_read_thread_loads_no_content(self, thread: _Thread) -> None:
        """A caller reading only the thread's tail, or the page behind a cursor, is served the same rows."""
        tail, loaded = await thread.read(limit=5)
        assert loaded == 0
        assert [message.content["body"] for message in tail.messages] == [f"reply {index}" for index in range(36, 41)]

        behind = await thread.store.read_conversation(room_id=ROOM, thread_id=ROOT, limit=5, before=tail.next_cursor)
        assert thread.backend.loaded[-1] == 0
        assert [message.content["body"] for message in behind.messages] == [f"reply {index}" for index in range(31, 36)]


class TestNothingStale:
    """A row that changed is read as it is now, whatever an earlier read loaded."""

    async def test_an_edit_is_read_and_loads_only_the_edited_message(self, thread: _Thread) -> None:
        """An edit of an old message replaces its body in the next read."""
        await admit(thread.store, "$edit", ts=6_000, content=edit("$reply-007", "edited seven"))

        page, loaded = await thread.read()
        assert loaded == 1
        edited = page.messages[7]
        assert (edited.logical_event_id, edited.revision_event_id, edited.revision_ts) == ("$reply-007", "$edit", 6_000)
        assert edited.content["body"] == "edited seven"

    async def test_an_edit_storing_exactly_as_many_bytes_is_read(self, thread: _Thread) -> None:
        """A body is told from the one before it by its revision, not by its size."""
        await admit(thread.store, "$edit", ts=6_000, content=edit("$reply-007", "reply 8"))

        assert (await thread.bodies())[7] == "reply 8"

    async def test_a_redacted_revision_is_never_read_again(self, thread: _Thread) -> None:
        """Redacting the visible edit withholds the message until a refetch names its body."""
        await admit(thread.store, "$edit", ts=6_000, content=edit("$reply-007", "secret"))
        assert (await thread.bodies())[7] == "secret"

        await admit(thread.store, "$redaction", ts=7_000, redacts="$edit", kind=EventKind.REDACTION)

        page, _ = await thread.read()
        assert "$reply-007" not in [message.logical_event_id for message in page.messages]
        assert "secret" not in [message.content["body"] for message in page.messages]
        assert [request.logical_event_id for request in page.refresh_pending] == ["$reply-007"]

        assert await thread.store.install_refetched_revision(
            page.refresh_pending[0],
            revision_event_id="$earlier-edit",
            revision_ts=5_000,
            revision_sender=ALICE,
            content=text("sECRET"),
        )
        page, loaded = await thread.read()
        assert loaded == 1
        assert page.refresh_pending == ()
        assert page.messages[7].content["body"] == "sECRET"

    async def test_a_refetch_back_to_the_original_reads_the_original(self, thread: _Thread) -> None:
        """Once the only edit is redacted the server names the original again, which is what the read returns."""
        await admit(thread.store, "$edit", ts=6_000, content=edit("$reply-007", "secret"))
        assert (await thread.bodies())[7] == "secret"
        await admit(thread.store, "$redaction", ts=7_000, redacts="$edit", kind=EventKind.REDACTION)
        page, _ = await thread.read()

        assert await thread.store.install_refetched_revision(
            page.refresh_pending[0],
            revision_event_id="$reply-007",
            revision_ts=1_007,
            revision_sender=ALICE,
            content=text("reply 7"),
        )
        page, _ = await thread.read()
        assert page.refresh_pending == ()
        assert (page.messages[7].revision_event_id, page.messages[7].content["body"]) == ("$reply-007", "reply 7")

    async def test_a_refetch_storing_another_body_under_a_read_revision_is_read(self, thread: _Thread) -> None:
        """A refetch may store a revision a read already loaded with other content of the same size and time."""
        await admit(thread.store, "$edit", ts=6_000, content=edit("$reply-007", "secret"))
        assert (await thread.bodies())[7] == "secret"
        await admit(thread.store, "$redaction", ts=7_000, redacts="$edit", kind=EventKind.REDACTION)
        page, _ = await thread.read()

        assert await thread.store.install_refetched_revision(
            page.refresh_pending[0],
            revision_event_id="$reply-007",
            revision_ts=1_007,
            revision_sender=ALICE,
            content=text("REPLY 7"),
        )
        warm, _ = await thread.read()
        cold, _ = await _thread(_counted(thread.journal)).read()
        assert warm.messages[7].content["body"] == "REPLY 7"
        assert warm == cold

    async def test_a_redacted_message_is_gone(self, thread: _Thread) -> None:
        """Redacting the original removes the message from the next read."""
        await admit(thread.store, "$redaction", ts=7_000, redacts="$reply-007", kind=EventKind.REDACTION)

        page, loaded = await thread.read()
        assert loaded == 0
        assert len(page.messages) == REPLIES
        assert "reply 7" not in [message.content["body"] for message in page.messages]

    async def test_a_sidecar_body_is_withheld_then_read_once_resolved(self, thread: _Thread) -> None:
        """An edit whose text lives in an attached file replaces a read body with a debt, then with its text."""
        await admit(thread.store, "$long", ts=6_000, content=sidecar(edit("$reply-007", "preview")))

        page, _ = await thread.read()
        assert "$reply-007" not in [message.logical_event_id for message in page.messages]
        assert [request.revision_event_id for request in page.refresh_pending] == ["$long"]

        assert await thread.store.install_refetched_revision(
            page.refresh_pending[0],
            revision_event_id="$long",
            revision_ts=6_000,
            revision_sender=ALICE,
            content=text("the whole text"),
        )
        assert (await thread.bodies())[7] == "the whole text"

    async def test_a_late_older_message_is_read_in_its_place(self, thread: _Thread) -> None:
        """A message stamped inside the thread that arrives after a read takes its place in the next one."""
        await admit(thread.store, "$late", ts=1_003, thread_id=ROOT, content=text("late"))

        page, loaded = await thread.read()
        assert loaded == 1
        assert [message.content["body"] for message in page.messages[2:6]] == ["reply 2", "late", "reply 3", "reply 4"]

    async def test_a_write_through_another_store_is_read(
        self,
        thread: _Thread,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """Another runtime on the same database changes what this one reads."""
        other = journal_database().principal("agent@alice")
        await admit(other, "$edit", ts=6_000, content=edit("$reply-007", "edited elsewhere"))
        await admit(other, "$redaction", ts=7_000, redacts="$reply-009", kind=EventKind.REDACTION)

        bodies = await thread.bodies()
        assert bodies[7] == "edited elsewhere"
        assert "reply 9" not in bodies

    async def test_a_page_cut_by_what_it_may_decode_to_is_cut_the_same_again(
        self,
        thread: _Thread,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A read that loads nothing still stops where the decoded budget stops it, and says more is behind it."""
        monkeypatch.setattr(reads, "_PAGE_DECODED_BUDGET_BYTES", 2_000)
        cold, cold_loaded = await _thread(_counted(thread.journal)).read()

        page, loaded = await thread.read()
        assert (cold_loaded, loaded) == (REPLIES + 1, 0)
        assert 0 < len(page.messages) < REPLIES
        assert page.next_cursor is not None
        assert page == cold


# Bodies of one stored size, so a changed row is rarely told apart by its size.
_BODIES = ("alpha", "bravo", "delta", "gamma", "omega")


@pytest.mark.parametrize("seed", range(4))
async def test_a_store_that_read_before_reads_what_a_store_reading_first_does(
    journal_database: Callable[[], EventJournalStore],
    seed: int,
) -> None:
    """Whatever order replies, edits, redactions and refetches of any body land in, every page equals a first read's."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible order, not a secret
    journal = journal_database()
    warm = journal.principal("agent@alice")
    events: dict[str, dict[str, object]] = {ROOT: text("root")}
    targets = [ROOT]
    await admit(warm, ROOT, ts=1_000, content=events[ROOT])
    for step in range(100):
        event_id, ts, body = f"$event-{step:03d}", rng.randint(900, 1_300), rng.choice(_BODIES)
        action = rng.choice(("reply", "reply", "edit", "edit", "sidecar", "redact", "refetch"))
        if action == "reply":
            events[event_id] = text(body)
            targets.append(event_id)
            await admit(warm, event_id, ts=ts, thread_id=ROOT, content=events[event_id])
        elif action in {"edit", "sidecar"}:
            events[event_id] = text(body)
            content = edit(rng.choice(targets), body)
            await admit(warm, event_id, ts=ts, content=sidecar(content) if action == "sidecar" else content)
        elif action == "redact":
            await admit(warm, event_id, ts=ts, redacts=rng.choice(list(events)), kind=EventKind.REDACTION)
        else:
            page = await warm.read_conversation(room_id=ROOM, thread_id=ROOT, limit=WINDOW)
            for request in page.refresh_pending:
                revision = rng.choice((request.revision_event_id, request.logical_event_id))
                await warm.install_refetched_revision(
                    request,
                    revision_event_id=revision,
                    revision_ts=ts,
                    revision_sender=ALICE,
                    content=text(rng.choice(_BODIES)),
                )
        cold = EventJournalStore(backend=journal.backend, approval_signals=journal.approval_signals)
        for thread_id, limit in ((ROOT, WINDOW), (ROOT, rng.randint(1, 8)), (None, WINDOW)):
            read = {"room_id": ROOM, "thread_id": thread_id, "limit": limit}
            assert await warm.read_conversation(**read) == await cold.principal("agent@alice").read_conversation(**read)


async def _owe_a_refetch(store: PrincipalStore, uncached: PrincipalStore) -> RefreshRequest:
    """Edit reply 7 and redact the edit, with no read through a cache in between, and return the refetch owed."""
    await admit(store, "$edit", ts=6_000, content=edit("$reply-007", "secret"))
    await admit(store, "$redaction", ts=7_000, redacts="$edit", kind=EventKind.REDACTION)
    return (await uncached.read_conversation(room_id=ROOM, thread_id=ROOT, limit=WINDOW)).refresh_pending[0]


async def _refetch_another_body(store: PrincipalStore, request: RefreshRequest) -> None:
    """Answer a refetch with the original revision of reply 7 under another body of the same size."""
    assert await store.install_refetched_revision(
        request,
        revision_event_id="$reply-007",
        revision_ts=1_007,
        revision_sender=ALICE,
        content=text("REPLY 7"),
    )


class TestReadsRacingAWrite:
    """A read that overlaps a write storing another body under a read revision neither takes nor keeps the old one."""

    async def test_a_read_between_a_commit_and_its_publish_reads_the_stored_body(
        self,
        thread: _Thread,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A room left and delivered again stores other bodies under read revisions, and the read's snapshot holds them."""
        writes = thread.backend.inner._writes  # type: ignore[attr-defined]
        apply, loop = writes.apply, asyncio.get_running_loop()
        read_before_publish: list[ConversationPage] = []

        def apply_then_read(operations: list[Operation[Any]]) -> object:
            outcomes = apply(operations)
            page, _ = asyncio.run_coroutine_threadsafe(thread.read(), loop).result(SETTLE_TIMEOUT_SECONDS)
            read_before_publish.append(page)
            return outcomes

        await admit_room_membership(thread.store, ROOM, "leave", source=DepartureSource.LOCAL)
        epoch = await admit_room_membership(thread.store, ROOM, "join")
        monkeypatch.setattr(writes, "apply", apply_then_read)
        assert await thread.store.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=ROOT,
            events=(
                projection(ROOT, ts=1_000, content=text("ROOT")),
                projection("$reply-001", ts=1_001, thread_id=ROOT, content=text("REPLY 1")),
            ),
            complete=True,
            expected_membership_epoch=epoch,
        )

        bodies = [[message.content["body"] for message in page.messages] for page in read_before_publish]
        assert bodies[-1] == ["ROOT", "REPLY 1"]
        assert not {"root", "reply 1"}.intersection(body for page in bodies for body in page)
        assert await thread.bodies() == ["ROOT", "REPLY 1"]

    async def test_a_read_that_outlives_a_commit_does_not_keep_the_body_it_replaced(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """A page sized before the write and loaded after it returns its own snapshot and leaves nothing behind."""
        journal = journal_database()
        alice = journal.principal("agent@alice")
        uncached = EventJournalStore(backend=journal.backend, approval_signals=journal.approval_signals)
        await _seed(alice)
        loop = asyncio.get_running_loop()

        def land() -> None:
            async def rewrite() -> None:
                await _refetch_another_body(alice, await _owe_a_refetch(alice, uncached.principal("agent@alice")))

            asyncio.run_coroutine_threadsafe(rewrite(), loop).result(SETTLE_TIMEOUT_SECONDS)

        outlived = await journal.backend.read(
            lambda transaction: reads.read_conversation(
                _PausingTransaction(transaction, land, lambda sql: reads._PAGE_SIZE_COLUMNS in sql),
                "agent@alice",
                room_id=ROOM,
                thread_id=ROOT,
                limit=WINDOW,
                cache=journal.page_cache,
            ),
        )

        assert outlived.messages[7].content["body"] == "reply 7"
        page = await alice.read_conversation(room_id=ROOM, thread_id=ROOT, limit=WINDOW)
        assert page.messages[7].content["body"] == "REPLY 7"


class TestWhoseRows:
    """A row is read only by the principal it belongs to, under the membership that holds it."""

    async def test_each_principal_reads_its_own_rows(self, thread: _Thread) -> None:
        """Two principals of one store each read their own row of a message, even one of the same revision and size."""
        bob = _thread(thread.journal, "agent@bob")
        assert await bob.bodies() == []

        await admit(bob.store, ROOT, ts=1_000, content=text("root"))
        await admit(bob.store, "$reply-001", ts=1_001, thread_id=ROOT, content=text("reply 1"))
        await admit(bob.store, "$reply-002", ts=1_002, thread_id=ROOT, content=text("bob's 2"))
        await admit(bob.store, "$edit", ts=6_000, content=edit("$reply-001", "only bob saw this edit"))

        assert await bob.bodies() == ["root", "only bob saw this edit", "bob's 2"]
        assert (await thread.bodies())[:3] == ["root", "reply 1", "reply 2"]
        assert await bob.bodies() == ["root", "only bob saw this edit", "bob's 2"]

    async def test_a_departure_empties_the_thread_and_a_rejoin_reads_what_it_delivers(self, thread: _Thread) -> None:
        """Rows a membership held are not read under the next one, even delivered again at the same revision and size."""
        await admit_room_membership(thread.store, ROOM, "leave", source=DepartureSource.LOCAL)
        assert await thread.bodies() == []

        epoch = await admit_room_membership(thread.store, ROOM, "join")
        assert await thread.store.install_hydrated_conversation(
            room_id=ROOM,
            thread_id=ROOT,
            events=(
                projection(ROOT, ts=1_000, content=text("root")),
                projection("$reply-001", ts=1_001, thread_id=ROOT, content=text("REPLY 1")),
                projection("$reply-002", ts=1_002, thread_id=ROOT, content=text("reply 2")),
            ),
            complete=True,
            expected_membership_epoch=epoch,
        )
        await admit(thread.store, "$long", ts=6_000, content=sidecar(edit("$reply-002", "preview")))

        page, _ = await thread.read()
        assert [message.content["body"] for message in page.messages] == ["root", "REPLY 1"]
        assert [message.revision_event_id for message in page.messages] == [ROOT, "$reply-001"]
        assert [request.membership_epoch for request in page.refresh_pending] == [epoch]


class TestMemoryBound:
    """The cache holds a fixed amount of stored content, for every principal and conversation together."""

    async def test_content_past_the_budget_is_loaded_again(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """A thread larger than the budget keeps only what fits, and still reads whole."""
        row_bytes = len('{"body":"reply 10","msgtype":"m.text"}') + reads._CACHED_ROW_OVERHEAD_BYTES
        thread = _thread(_counted(journal_database(), page_cache=reads.PageCache(budget_bytes=10 * row_bytes)))
        await _seed(thread.store)
        first, _ = await thread.read()

        second, loaded = await thread.read()
        assert REPLIES + 1 - 10 <= loaded <= REPLIES + 1
        assert second == first

    async def test_the_least_recently_read_conversation_is_dropped_first(
        self,
        journal_database: Callable[[], EventJournalStore],
    ) -> None:
        """Reading one conversation past the budget costs the one read longest ago, not the one being read."""
        row_bytes = len('{"body":"reply 10","msgtype":"m.text"}') + reads._CACHED_ROW_OVERHEAD_BYTES
        thread = _thread(
            _counted(journal_database(), page_cache=reads.PageCache(budget_bytes=(REPLIES + 5) * row_bytes)),
        )
        await _seed(thread.store)
        for index in range(REPLIES):
            await admit(thread.store, f"$room-{index:03d}", ts=9_000 + index, content=text(f"reply {index + 10}"))
        await thread.read()
        assert (await thread.read())[1] == 0

        await thread.store.read_conversation(room_id=ROOM, thread_id=None, limit=REPLIES)
        await thread.store.read_conversation(room_id=ROOM, thread_id=None, limit=REPLIES)
        assert thread.backend.loaded[-1] == 0
        assert (await thread.read())[1] > 0


@dataclass(slots=True)
class _Refetcher:
    """A hydrator whose server answers each refetch with ``body``, or with nothing when ``body`` is None."""

    store: PrincipalStore
    body: str | None

    async def ensure_hydrated(self, *, room_id: str, thread_id: str | None) -> None:
        """Every conversation here is already walked."""

    async def resolve_refreshes(self, requests: Sequence[RefreshRequest]) -> None:
        """Install the server's revision for each debt."""
        for request in requests:
            if self.body is not None:
                await self.store.install_refetched_revision(
                    request,
                    revision_event_id=request.logical_event_id,
                    revision_ts=1_007,
                    revision_sender=ALICE,
                    content=text(self.body),
                )


class TestReadContracts:
    """A strict read waits for a refetch and a non-blocking read skips the message, after a read that held it."""

    async def _redact_a_read_edit(self, thread: _Thread) -> None:
        await admit(thread.store, "$edit", ts=6_000, content=edit("$reply-007", "secret"))
        assert (await thread.bodies())[7] == "secret"
        await admit(thread.store, "$redaction", ts=7_000, redacts="$edit", kind=EventKind.REDACTION)

    def _reader(self, thread: _Thread, body: str | None) -> ConversationReader:
        return ConversationReader(thread.store, cast("ConversationHydrator", _Refetcher(thread.store, body)))

    async def test_a_nonblocking_read_skips_a_message_awaiting_its_refetch(self, thread: _Thread) -> None:
        """The message is absent, not the body an earlier read loaded."""
        await self._redact_a_read_edit(thread)

        page = await self._reader(thread, "reply 7").read(room_id=ROOM, thread_id=ROOT, limit=WINDOW)
        assert len(page.messages) == REPLIES
        assert "secret" not in [message.content["body"] for message in page.messages]
        assert len(page.refresh_pending) == 1

    async def test_a_strict_read_waits_for_the_refetch_and_reads_its_body(self, thread: _Thread) -> None:
        """The page holds what the server answered."""
        await self._redact_a_read_edit(thread)

        page = await self._reader(thread, "reply 7").read_strict(room_id=ROOM, thread_id=ROOT, limit=WINDOW)
        assert page.refresh_pending == ()
        assert page.messages[7].content["body"] == "reply 7"

    async def test_a_strict_read_fails_while_the_refetch_is_unanswered(self, thread: _Thread) -> None:
        """No page is returned with the message missing, or with the body it had."""
        await self._redact_a_read_edit(thread)

        with pytest.raises(ConversationReadError, match="awaiting a server refetch"):
            await self._reader(thread, None).read_strict(room_id=ROOM, thread_id=ROOT, limit=WINDOW)
