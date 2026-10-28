"""What committed approval writes tell the process, published by the journal's writer in commit order.

A writer records signals on its transaction; the store's writer publishes those of each committed operation, in batch
order, before any caller resumes. A listener hears them on its own loop, and nothing after it stops listening.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import EventJournalStore, approval_signals
from mindroom.event_journal.approval_signals import PausedReply
from mindroom.event_journal.sqlite_backend import SqliteBackend

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.event_journal.backend import Transaction

pytestmark = pytest.mark.asyncio

_TIMEOUT_SECONDS = 5.0
_PRINCIPAL = "agent@alice"
_PAUSED = PausedReply(_PRINCIPAL, "approval-1", "!room:localhost", None)


def _recording_backend(tmp_path: Path) -> tuple[SqliteBackend, list[tuple[object, ...]]]:
    """Open a backend whose publications are recorded, one entry per published batch."""
    published: list[tuple[object, ...]] = []

    def publish(signals: Sequence[object]) -> None:
        published.append(tuple(signals))

    return SqliteBackend.open(tmp_path / "journal.db", publish=publish), published


async def _behind_a_running_write(backend: SqliteBackend) -> tuple[asyncio.Task[None], threading.Event]:
    """Start a write that holds the writer until released, so writes queued meanwhile commit in one batch."""
    running, release = threading.Event(), threading.Event()

    def hold(_transaction: Transaction) -> None:
        running.set()
        release.wait(_TIMEOUT_SECONDS)

    holding = asyncio.create_task(backend.write(hold))
    assert await asyncio.to_thread(running.wait, _TIMEOUT_SECONDS)
    return holding, release


async def test_a_caller_cancelled_after_its_commit_still_publishes_its_signals(tmp_path: Path) -> None:
    """The writer, not the caller, publishes, so a caller cancelled while its write commits loses no signal."""
    backend, published = _recording_backend(tmp_path)
    running, release = threading.Event(), threading.Event()

    def claim(transaction: Transaction) -> None:
        transaction.execute("CREATE TABLE claim (value INTEGER)")
        transaction.signal("claimed")
        running.set()
        release.wait(_TIMEOUT_SECONDS)

    caller = asyncio.create_task(backend.write(claim))
    assert await asyncio.to_thread(running.wait, _TIMEOUT_SECONDS)
    caller.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert published == [("claimed",)]
    assert await backend.read(lambda transaction: transaction.fetchall("SELECT value FROM claim")) == ()
    await backend.close()


async def test_a_write_that_rolls_back_publishes_nothing_while_its_batch_commits(tmp_path: Path) -> None:
    """Only an operation whose savepoint held publishes; the batch around a refused one still commits its signals."""
    backend, published = _recording_backend(tmp_path)

    def refused(transaction: Transaction) -> None:
        transaction.signal("rolled back")
        msg = "refused"
        raise ValueError(msg)

    def committed(transaction: Transaction) -> None:
        transaction.signal("committed")

    holding, release = await _behind_a_running_write(backend)
    writes = asyncio.gather(backend.write(refused), backend.write(committed), return_exceptions=True)
    await asyncio.sleep(0)
    release.set()
    await holding
    first, second = await writes

    assert isinstance(first, ValueError)
    assert second is None
    assert published == [("committed",)]
    await backend.close()


async def test_a_batch_that_does_not_commit_publishes_nothing(tmp_path: Path) -> None:
    """A batch whose transaction fails fails every write in it, and none of their signals is published."""
    backend, published = _recording_backend(tmp_path)

    def takes_the_batch_down(transaction: Transaction) -> None:
        transaction.signal("lost")
        # Ending the transaction underneath the batch is an error no savepoint can contain.
        transaction.execute("ROLLBACK")
        msg = "the batch is gone"
        raise ValueError(msg)

    def committed_alone(transaction: Transaction) -> None:
        transaction.signal("committed alone")

    holding, release = await _behind_a_running_write(backend)
    writes = asyncio.gather(backend.write(committed_alone), backend.write(takes_the_batch_down), return_exceptions=True)
    await asyncio.sleep(0)
    release.set()
    await holding
    first, second = await writes

    assert isinstance(first, ValueError)
    assert isinstance(second, ValueError)
    assert published == []
    await backend.close()


async def test_a_listener_on_another_loop_hears_a_pause_and_its_end_in_commit_order(tmp_path: Path) -> None:
    """Signals reach a listener on a loop that is not the writer's through that loop, in the order they committed."""
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    heard: list[str] = []
    subscribed = threading.Event()

    def listen_on_its_own_loop() -> None:
        async def listen() -> None:
            done = asyncio.Event()

            def note(change: str) -> None:
                assert threading.current_thread() is listener
                heard.append(change)
                if len(heard) == 2:
                    done.set()

            store.approval_signals.follow_paused_replies(
                _PRINCIPAL,
                paused=lambda reply: note(f"paused {reply.approval_id}"),
                ended=lambda approval_id: note(f"ended {approval_id}"),
            )
            subscribed.set()
            await asyncio.wait_for(done.wait(), _TIMEOUT_SECONDS)

        asyncio.run(listen())

    listener = threading.Thread(target=listen_on_its_own_loop, name="second-loop-listener", daemon=True)
    listener.start()
    assert await asyncio.to_thread(subscribed.wait, _TIMEOUT_SECONDS)
    await store.backend.write(lambda transaction: approval_signals.paused(transaction, _PAUSED))
    await store.backend.write(lambda transaction: approval_signals.ended(transaction, _PRINCIPAL, "approval-1"))
    await asyncio.to_thread(listener.join, _TIMEOUT_SECONDS)

    assert heard == ["paused approval-1", "ended approval-1"]
    await store.close()


async def test_following_reports_the_replies_paused_now_and_nothing_after_it_stops(tmp_path: Path) -> None:
    """Following hears the current set before it returns, then each change, until it stops."""
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    await store.backend.write(lambda transaction: approval_signals.paused(transaction, _PAUSED))
    heard: list[object] = []
    stop = store.approval_signals.follow_paused_replies(_PRINCIPAL, paused=heard.append, ended=heard.append)
    assert heard == [_PAUSED]

    def end_one_and_one_unknown(transaction: Transaction) -> None:
        approval_signals.ended(transaction, _PRINCIPAL, "approval-1")
        approval_signals.ended(transaction, _PRINCIPAL, "never-paused")

    await store.backend.write(end_one_and_one_unknown)
    assert heard == [_PAUSED, "approval-1"]

    stop()
    await store.backend.write(lambda transaction: approval_signals.paused(transaction, _PAUSED))
    assert heard == [_PAUSED, "approval-1"]
    await store.close()


async def test_a_load_replaces_what_the_signals_held_and_reports_the_difference(tmp_path: Path) -> None:
    """Loading an empty journal ends every paused reply the signals still held."""
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    await store.backend.write(lambda transaction: approval_signals.paused(transaction, _PAUSED))
    ended: list[str] = []
    store.approval_signals.follow_paused_replies(_PRINCIPAL, paused=lambda _reply: None, ended=ended.append)

    await store.load_approval_signals()

    assert ended == ["approval-1"]
    await store.close()
