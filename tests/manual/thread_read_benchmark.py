"""Measure what one turn's thread reads cost as the thread grows.

A turn in a thread has three readers of the whole thread: the dispatch snapshot
(`turns/dispatch.py` -> `extract_dispatch_context`), the prompt read once the
turn holds the reply lock (`reply_turn/locked_turn.py` -> `refresh_model_history`)
and the summary gate after the reply (`thread_summary/automatic.py`), each with
`limit=HYDRATED_PROMPT_WINDOW_MESSAGES`. Between them the thread changes: the
user's message lands, then the reply and its stream edits. The prompt read keeps
the dispatch read when `thread_is_unchanged` says the thread is as it was; the
summary gate always loads.

This runs that sequence against a real store and reports, per turn, how many
reads loaded content, how many rows they loaded and how long the reads took.

Run it with::

    uv run python tests/manual/thread_read_benchmark.py [sqlite|postgres://...] [--reload]

``--reload`` makes the prompt read load the thread too, as it did before it shared the dispatch read.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.event_journal import EventClass, EventJournalStore, EventKind, InboundEvent, ProjectedEvent
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES, ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader, complete_thread_history, thread_is_unchanged

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.event_journal.backend import Backend, Operation, Row, Transaction
    from mindroom.matrix.thread_history_result import ThreadHistoryResult

ROOM = "!bench:example.org"
USER = "@tester:example.org"
AGENT = "@mindroom_worker:example.org"
PRINCIPAL = "agent@worker"
LENGTHS = (10, 200, 500)
TURNS = 20
STREAM_EDITS = 3
# The body of a load-test reply: 320-2000 characters, with the HTML Matrix clients send beside it.
BODY = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20


def _content(body: str, root: str | None, replaces: str | None = None) -> dict[str, object]:
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": f"<p>{body}</p>",
    }
    if root is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": root, "is_falling_back": True}
    if replaces is not None:
        return {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": replaces},
        }
    return content


async def _admit(
    store: object,
    event_id: str,
    ts: int,
    sender: str,
    content: dict[str, object],
    *,
    root: str | None,
    replaces: str | None = None,
) -> None:
    await store.admit(  # type: ignore[attr-defined]
        InboundEvent(
            event_id=event_id,
            room_id=ROOM,
            thread_id=root,
            kind=EventKind.MESSAGE,
            event_class=EventClass.CONTEXT_ONLY,
            sender=sender,
            origin_server_ts=ts,
            source={"event_id": event_id, "content": content},
        ),
        ProjectedEvent(
            event_id=event_id,
            room_id=ROOM,
            thread_id=root,
            sender=sender,
            origin_server_ts=ts,
            content=content,
            replaces_event_id=replaces,
            redacts_event_id=None,
        ),
    )


async def _measure(
    store: EventJournalStore,
    length: int,
    loaded: list[int],
    *,
    share: bool,
) -> tuple[float, float, float, float, float]:
    """Seed one thread of ``length`` messages and run ``TURNS`` turns.

    Returns, per turn, how many reads loaded content, how many rows they loaded and the milliseconds all three reads
    took, then the median milliseconds of a read that loaded the thread and of one that found it unchanged.
    """
    principal = store.principal(PRINCIPAL)
    # The marker stands, so the hydrator never walks and needs no homeserver.
    hydrator = ConversationHydrator(store=principal, runtime=cast("Any", None), self_sender=AGENT)
    reader = ConversationReader(store=principal, hydrator=hydrator)
    root = f"$root-{length}"
    ts = 1_000
    await _admit(principal, root, ts, USER, _content(BODY, None), root=None)
    for index in range(1, length):
        ts += 1
        sender = AGENT if index % 2 else USER
        await _admit(principal, f"$m-{length}-{index}", ts, sender, _content(f"{index} {BODY}", root), root=root)
    await principal.install_hydrated_conversation(
        room_id=ROOM,
        thread_id=root,
        events=(),
        complete=True,
        expected_membership_epoch=await principal.membership_epoch(ROOM),
    )

    load_ms: list[float] = []
    check_ms: list[float] = []

    async def read(held: ThreadHistoryResult | None = None, *, own_reply: str | None = None) -> ThreadHistoryResult:
        """Read the thread as one of a turn's three readers does: keep ``held`` if it stands, else load the thread."""
        started = time.perf_counter()
        if (
            share
            and held is not None
            and await thread_is_unchanged(reader, ROOM, root, held, ignoring_event_id=own_reply)
        ):
            check_ms.append((time.perf_counter() - started) * 1000)
            return held
        history = await complete_thread_history(reader, ROOM, root)
        load_ms.append((time.perf_counter() - started) * 1000)
        assert history.is_full_history
        assert history[0].event_id == root
        return history

    await read()  # The read that first sees the thread, as a restart or a cold thread does.
    load_ms.clear()
    reads_before = len(loaded)
    for turn in range(TURNS):
        ts += 1
        await _admit(principal, f"$u-{length}-{turn}", ts, USER, _content(f"follow-up {turn}", root), root=root)
        held = await read()  # dispatch snapshot
        ts += 1
        reply = f"$a-{length}-{turn}"
        await _admit(principal, reply, ts, AGENT, _content("Thinking...", root), root=root)
        # The prompt read, once the turn holds the reply lock and sent its placeholder.
        held = await read(held, own_reply=reply)
        for edit in range(STREAM_EDITS):
            ts += 1
            body = f"{turn}.{edit} {BODY}"
            await _admit(
                principal,
                f"$e-{length}-{turn}-{edit}",
                ts,
                AGENT,
                _content(body, root, reply),
                root=root,
                replaces=reply,
            )
        await read()  # summary gate after the reply
    turn_reads = loaded[reads_before:]
    return (
        # A check that finds the reply's echo loads that one message, to weigh it against the page's bounds.
        sum(1 for rows in turn_reads if rows > 1) / TURNS,
        sum(turn_reads) / TURNS,
        (sum(load_ms) + sum(check_ms)) / TURNS,
        statistics.median(load_ms),
        statistics.median(check_ms) if check_ms else float("nan"),
    )


class _CountingTransaction:
    """One transaction that counts the rows it returns with their stored content."""

    def __init__(self, inner: Transaction, loaded: list[int]) -> None:
        self._inner = inner
        self._loaded = loaded

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._inner.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        return self._inner.fetchone(sql, params)

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> tuple[Row, ...]:
        rows = self._inner.fetchall(sql, params)
        self._loaded[-1] += sum("content_json" in row.keys() for row in rows)  # noqa: SIM118 - sqlite3.Row
        return rows

    def signal(self, value: object) -> None:
        self._inner.signal(value)


class _CountingBackend:
    """The real backend, counting per read how many rows came back with their stored content."""

    def __init__(self, inner: Backend, loaded: list[int]) -> None:
        self._inner = inner
        self._loaded = loaded
        self.write = inner.write

    async def read[T](self, operation: Operation[T]) -> T:
        self._loaded.append(0)
        return await self._inner.read(lambda transaction: operation(_CountingTransaction(transaction, self._loaded)))


async def main() -> None:
    """Print one row per thread length."""
    arguments = [argument for argument in sys.argv[1:] if argument != "--reload"]
    share = "--reload" not in sys.argv
    target = arguments[0] if arguments else "sqlite"
    loaded: list[int] = []
    with tempfile.TemporaryDirectory() as directory:
        opened = (
            EventJournalStore.open_sqlite(Path(directory) / "journal.db")
            if target == "sqlite"
            else EventJournalStore.open_postgres(target)
        )
        store = EventJournalStore(
            backend=cast("Backend", _CountingBackend(opened.backend, loaded)),
            approval_signals=opened.approval_signals,
        )
        try:
            mode = "shared" if share else "reload"
            print(f"backend={target.split(':')[0]} turns={TURNS} window={HYDRATED_PROMPT_WINDOW_MESSAGES} mode={mode}")
            print("| L | loads/turn | rows loaded/turn | read ms/turn | load ms p50 | unchanged check ms p50 |")
            print("|---|---|---|---|---|---|")
            for length in LENGTHS:
                loads, rows, ms, load_p50, check_p50 = await _measure(store, length, loaded, share=share)
                print(f"| {length} | {loads:.0f} | {rows:.0f} | {ms:.2f} | {load_p50:.2f} | {check_p50:.2f} |")
        finally:
            await opened.close()


if __name__ == "__main__":
    asyncio.run(main())
