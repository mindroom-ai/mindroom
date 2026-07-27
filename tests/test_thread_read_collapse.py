"""Collapsed thread reads: one surviving edit per message, and no message lost.

Two testing lessons this file exists to carry
---------------------------------------------
1. Test the seam, not one side of it. Every defect this feature shipped lived between the SQL and
   the in-memory fold, and tests that exercised each side separately all passed while the joined
   behaviour was wrong. tests/test_thread_edit_integrity.py asserts the same-sender rule against
   the fold and never executes the query that decides which edits the fold is handed, so it stayed
   green through two bugs that broke exactly that rule. The guard that actually holds is
   TestCollapsedReadAgreesWithTheFoldOnEveryEdit: for every message, the fold's winner over the
   collapsed rows must equal its winner over the raw ones.
2. Rerouting a read past a monkeypatched seam HANGS a test, it does not fail it. Twice on this
   feature a test kept passing its own setup and then waited out its timeout, which reads as a slow
   test rather than a broken one. If a test that patches a cache method starts timing out, check
   first whether production still calls the method it patched.

A third lesson, about the shape of the feature rather than its tests
--------------------------------------------------------------------
This read was briefly bounded, and the bound was the wrong idea. Collapsing edits loses nothing:
it returns the same messages with the superseded history stripped. Truncating loses the oldest
half of the caller's context, and no consumer in
the tree can tell a truncated read from a short one. The two got fused because both reduce row
counts, but only one of them is free. Do not reintroduce a bound without a caller that can
explicitly handle partial history.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast

import nio
import pytest

from mindroom.matrix.cache import (
    postgres_event_cache_threads,
    sqlite_event_cache_threads,
)
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage, ThreadEditCandidates
from mindroom.matrix.event_info import EventInfo
from tests.event_cache_test_support import replace_thread_unconditionally

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.matrix.cache import ConversationEventCache

# Cache tables a thread read may touch. Any statement naming one of these counts.
#
# "events" subsumes the other two by substring match and is the one that matters: the canonical
# regression is a per-message payload lookup against the events table alone, which a filter
# naming only thread_events and event_edits counts as zero.
_THREAD_READ_TABLES = ("events", "thread_events", "event_edits")

_ROOM_ID = "!bounded:localhost"
_THREAD_ID = "$root"
_OTHER_THREAD_ID = "$otherroot"


def _message_event(
    event_id: str,
    timestamp: int,
    *,
    body: str = "body",
    sender: str = "@user:localhost",
    thread_id: str | None = None,
    edit_of: str | None = None,
) -> dict[str, Any]:
    """Return one raw thread event source."""
    content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    if edit_of is not None:
        content["m.new_content"] = {"body": body, "msgtype": "m.text"}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": edit_of}
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
    }


def _thread_event_sources(
    message_count: int,
    *,
    edits_per_message: int = 0,
    body_chars: int = 4,
    same_timestamp: bool = False,
) -> list[dict[str, Any]]:
    """Return one thread's raw sources: a root, N messages, and per-message edits."""
    body = "x" * body_chars
    events = [_message_event(_THREAD_ID, 1_000, body=body)]
    for message_index in range(message_count):
        message_id = f"$m{message_index}"
        message_ts = 1_000 if same_timestamp else 2_000 + message_index * 1_000
        events.append(_message_event(message_id, message_ts, body=body, thread_id=_THREAD_ID))
        events.extend(
            _message_event(
                f"$m{message_index}-edit{edit_index}",
                message_ts if same_timestamp else message_ts + 1 + edit_index,
                body=body,
                thread_id=_THREAD_ID,
                edit_of=message_id,
            )
            for edit_index in range(edits_per_message)
        )
    return events


def _is_edit(event: dict[str, Any]) -> bool:
    relates_to = event.get("content", {}).get("m.relates_to") or {}
    return relates_to.get("rel_type") == "m.replace"


def _original_event_ids(events: list[dict[str, Any]]) -> list[str]:
    """Return the distinct messages a read covers, in returned order."""
    covered: list[str] = []
    for event in events:
        relates_to = event.get("content", {}).get("m.relates_to") or {}
        event_id = relates_to["event_id"] if _is_edit(event) else event["event_id"]
        if event_id not in covered:
            covered.append(event_id)
    return covered


def _latest_edit_by_original(events: list[dict[str, Any]]) -> dict[str, str]:
    """Return the winning edit event ID for each edited message."""
    latest: dict[str, tuple[int, str]] = {}
    for event in events:
        if not _is_edit(event):
            continue
        original_event_id = event["content"]["m.relates_to"]["event_id"]
        candidate = (event["origin_server_ts"], event["event_id"])
        if original_event_id not in latest or candidate > latest[original_event_id]:
            latest[original_event_id] = candidate
    return {original: winner for original, (_ts, winner) in latest.items()}


async def _folded_messages(rows: list[dict[str, Any]]) -> list[ResolvedVisibleMessage]:
    """Run the real fold and ordering over one read's rows, as thread history resolution does."""
    from mindroom.matrix.client_visible_messages import apply_latest_edits_to_messages  # noqa: PLC0415
    from mindroom.matrix.thread_projection import sort_thread_messages_root_first  # noqa: PLC0415

    candidates = ThreadEditCandidates()
    messages: dict[str, ResolvedVisibleMessage] = {}
    for row in rows:
        event = _nio_text_event(row)
        info = EventInfo.from_event(row)
        if candidates.record(event, event_info=info):
            continue
        messages[event.event_id] = ResolvedVisibleMessage(
            sender=event.sender,
            body=event.body,
            timestamp=event.server_timestamp,
            event_id=event.event_id,
            content=dict(row["content"]),
            thread_id=info.thread_id,
            latest_event_id=event.event_id,
        )
    await apply_latest_edits_to_messages(
        cast("nio.AsyncClient", None),
        messages_by_event_id=messages,
        edit_candidates=candidates,
        required_thread_id=_THREAD_ID,
    )
    ordered = list(messages.values())
    sort_thread_messages_root_first(ordered, thread_id=_THREAD_ID)
    return ordered


async def _seed_thread(
    event_cache: ConversationEventCache,
    events: list[dict[str, Any]],
) -> None:
    await replace_thread_unconditionally(event_cache, _ROOM_ID, _THREAD_ID, events)


async def _seed_other_thread(
    event_cache: ConversationEventCache,
    events: list[dict[str, Any]],
) -> None:
    """Seed a second thread in the same room, for scoping assertions."""
    await replace_thread_unconditionally(event_cache, _ROOM_ID, _OTHER_THREAD_ID, events)


class TestCollapsedReadLosesNoMessage:
    """A collapsed read returns every message the thread holds, with one edit each."""

    @pytest.mark.asyncio
    async def test_every_message_survives_the_collapse(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Collapsed means fewer rows, never fewer messages."""
        events = _thread_event_sources(8, edits_per_message=2)
        await _seed_thread(event_cache, events)

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert _original_event_ids(read) == _original_event_ids(events), "a message was lost"
        assert len(read) < len(events), "superseded edits were not collapsed"
        assert _latest_edit_by_original(read) == _latest_edit_by_original(events)

    @pytest.mark.asyncio
    async def test_edit_collapse_is_where_the_reduction_comes_from(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A 99%-edit thread collapses to a fraction of its rows without losing a message.

        This is the entire value of the feature, stated once. Dropping messages on top of it buys
        nothing and costs the oldest half of the caller's context.

        The thread built here is synthetic and deliberately extreme. Real workloads measure 53%
        edit rows overall and 6.30 edits per edited original, with one thread at 94.5%; the
        20x100 shape below is that worst case exaggerated, not a typical thread.
        """
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=100))

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert len(_original_event_ids(read)) == 21, "no message may be lost"
        assert len(read) <= 42, "one winning edit per message, not every edit ever seen"

    @pytest.mark.asyncio
    async def test_a_thousand_tiny_messages_all_come_back(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Nothing truncates by count, so a long cheap thread returns whole."""
        await _seed_thread(event_cache, _thread_event_sources(1_000, body_chars=1))

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert len(_original_event_ids(read)) == 1_001

    @pytest.mark.asyncio
    async def test_twenty_huge_messages_all_come_back(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Nothing truncates by size either: an expensive thread is still complete.

        This read was briefly bounded at 2 MiB, which silently dropped the oldest messages of any
        thread past it. No consumer could tell that from a short thread, so the bound is gone.
        """
        await _seed_thread(event_cache, _thread_event_sources(20, body_chars=20_000))

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert len(_original_event_ids(read)) == 21

    @pytest.mark.asyncio
    async def test_appending_a_message_extends_the_read_rather_than_sliding_it(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A stable prefix is what lets provider prompt caching hit the history block."""
        await _seed_thread(event_cache, _thread_event_sources(201))
        before = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        await _seed_thread(event_cache, _thread_event_sources(202))
        after = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        assert before is not None
        assert after is not None
        before_messages = _original_event_ids(before)

        assert _original_event_ids(after)[: len(before_messages)] == before_messages, (
            "the read slid instead of extending; a changed prefix defeats provider prompt caching"
        )

    @pytest.mark.asyncio
    async def test_the_root_is_returned_first(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """The thread root carries the original question and leads the read."""
        await _seed_thread(event_cache, _thread_event_sources(30))

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert read[0]["event_id"] == _THREAD_ID
        assert len(read) == 31

    @pytest.mark.asyncio
    async def test_an_unknown_thread_reads_as_a_miss(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A thread with no cached rows is an advisory miss, not an empty thread."""
        assert await event_cache.get_thread_events(_ROOM_ID, "$absent") is None

    @pytest.mark.asyncio
    async def test_uniform_timestamps_still_return_every_message(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Rows sharing one ``origin_server_ts`` must not collapse into each other.

        Reading twice and comparing was the previous assertion here, which cannot fail: two reads
        of an unchanged database agree under any mutation.
        """
        await _seed_thread(event_cache, _thread_event_sources(12, same_timestamp=True))

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        assert read is not None
        assert len(_original_event_ids(read)) == 13


@contextlib.contextmanager
def _count_thread_statements(event_cache: ConversationEventCache) -> Iterator[list[str]]:
    """Count SQL statements against ``thread_events`` that one read actually issues.

    Real statement counting, not a structural argument: the regression this guards against is a
    locally-correct change quietly reintroducing a per-message query, which only a count catches.
    """
    statements: list[str] = []
    db = event_cache._runtime.require_db()
    original_execute = db.execute

    async def counting_execute(query: object, *args: object, **kwargs: object) -> object:
        # Every cache table a thread read touches, not just thread_events. The most likely
        # regression is a per-message lookup of each survivor's latest edit, which queries
        # event_edits and would go uncounted by a narrower filter.
        if isinstance(query, str) and any(table in query for table in _THREAD_READ_TABLES):
            statements.append(query)
        return await original_execute(query, *args, **kwargs)

    db.execute = counting_execute  # type: ignore[method-assign]
    try:
        yield statements
    finally:
        db.execute = original_execute  # type: ignore[method-assign]


class TestTheSenderRuleSurvivesACrossThreadOriginal:
    """The original is found room-wide, so its sender is always available to compare against.

    Scoping the original lookup to the read's own thread made an original cached in a sibling
    thread of the same room read as absent. The query treats an absent original as "nobody to
    impersonate" and drops the sender filter, so the newest edit across all senders won - which
    is the suppression the edit-side membership join exists to prevent, mirrored. Only the other
    direction, an out-of-thread edit against an in-thread original, was covered.
    """

    @pytest.mark.asyncio
    async def test_a_foreign_edit_cannot_win_because_the_original_sits_in_another_thread(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """The author's own edit survives even when its original is cached elsewhere."""
        author = "@author:localhost"
        attacker = "@attacker:localhost"
        # $victim lives in a sibling thread of the same room.
        await replace_thread_unconditionally(
            event_cache,
            _ROOM_ID,
            _OTHER_THREAD_ID,
            [
                _message_event(_OTHER_THREAD_ID, 500, sender=author),
                _message_event("$victim", 600, sender=author, thread_id=_OTHER_THREAD_ID),
            ],
        )
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$author-edit", 2_500, sender=author, edit_of="$victim"),
                _message_event("$forged", 9_000, sender=attacker, edit_of="$victim"),
            ],
        )

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        returned_ids = {row["event_id"] for row in read}
        assert "$forged" not in returned_ids, (
            "a foreign replacement won because the original was looked up thread-scoped"
        )
        assert "$author-edit" in returned_ids

    @pytest.mark.asyncio
    async def test_a_point_cached_original_still_scopes_edits_to_its_sender(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An original needs no thread-membership row for its sender to protect the read."""
        author = "@author:localhost"
        attacker = "@attacker:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$author-edit", 2_500, sender=author, edit_of="$victim"),
                _message_event("$forged", 9_000, sender=attacker, edit_of="$victim"),
            ],
        )
        await event_cache.store_event(
            "$victim",
            _ROOM_ID,
            _message_event("$victim", 600, sender=author, thread_id=_THREAD_ID),
        )
        assert await event_cache.get_event(_ROOM_ID, "$victim") is not None
        assert "$victim" not in await event_cache.get_thread_event_ids(_ROOM_ID, _THREAD_ID)

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        returned_ids = {row["event_id"] for row in read}
        assert "$forged" not in returned_ids
        assert "$author-edit" in returned_ids


class TestSingleEventReadObeysTheSameSenderRule:
    """``get_latest_edit`` and the collapsed thread read must not disagree about one message.

    They read the same cache and are both used to render the same event. The thread read applies
    the same-sender rule; the single-event projection used to call ``get_latest_edit`` with no
    sender, so the newest edit from anyone won and that path served a foreign body under the
    author's event.
    """

    @pytest.mark.asyncio
    async def test_a_foreign_edit_is_not_served_as_the_latest_edit(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Asking for one event's latest edit as its author excludes everyone else's."""
        author = "@author:localhost"
        attacker = "@attacker:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$victim", 2_000, sender=author, body="real", thread_id=_THREAD_ID),
                _message_event("$author-edit", 3_000, sender=author, body="author edit", edit_of="$victim"),
                _message_event("$forged", 9_000, sender=attacker, body="attacker text", edit_of="$victim"),
            ],
        )

        scoped = await event_cache.get_latest_edit(_ROOM_ID, "$victim", sender=author)
        assert scoped is not None
        assert scoped["event_id"] == "$author-edit", "a foreign replacement was served as the latest edit"

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None
        assert _winning_edit_ids_by_original(read)["$victim"] == scoped["event_id"], (
            "the single-event read and the collapsed thread read disagree about this message"
        )

    @pytest.mark.asyncio
    async def test_equal_timestamps_break_the_same_way_as_the_collapsed_read(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Same-timestamp edits resolve by event ID in both reads, not by insertion order."""
        author = "@author:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$victim", 2_000, sender=author, thread_id=_THREAD_ID),
                # Inserted so write_seq order is the opposite of event-ID order.
                _message_event("$zzz", 3_000, sender=author, body="zzz", edit_of="$victim"),
                _message_event("$aaa", 3_000, sender=author, body="aaa", edit_of="$victim"),
            ],
        )

        latest = await event_cache.get_latest_edit(_ROOM_ID, "$victim", sender=author)
        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert latest is not None
        assert read is not None

        assert latest["event_id"] == "$zzz", "the tie broke on insertion order, not event ID"
        assert _winning_edit_ids_by_original(read)["$victim"] == latest["event_id"]

    @pytest.mark.asyncio
    async def test_the_single_event_projection_does_not_render_a_foreign_edit(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """The seam that actually served the foreign body, not just the cache API beneath it."""
        from mindroom.matrix.conversation_cache import _apply_cached_latest_edit  # noqa: PLC0415

        author = "@author:localhost"
        attacker = "@attacker:localhost"
        original = _message_event("$victim", 2_000, sender=author, body="real", thread_id=_THREAD_ID)
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                original,
                _message_event("$forged", 9_000, sender=attacker, body="attacker text", edit_of="$victim"),
            ],
        )

        projected = await _apply_cached_latest_edit(
            dict(original),
            room_id=_ROOM_ID,
            client=cast("nio.AsyncClient", None),
            event_cache=event_cache,
            expected_membership_epoch=await event_cache.room_membership_epoch(_ROOM_ID),
        )

        assert projected["content"]["body"] == "real", "the single-event projection rendered someone else's replacement"


class TestCollapsedReadCost:
    """One query per read, no matter how large or how edit-dense the thread is."""

    @pytest.mark.asyncio
    async def test_statement_count_does_not_grow_with_thread_size(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A 10x larger thread costs the same one query, not one per message."""
        await _seed_thread(event_cache, _thread_event_sources(20))
        with _count_thread_statements(event_cache) as small_statements:
            await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        await _seed_thread(event_cache, _thread_event_sources(200))
        with _count_thread_statements(event_cache) as large_statements:
            await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        assert len(small_statements) == len(large_statements) == 1, (
            f"expected one collapsed read, got {len(small_statements)} and {len(large_statements)}"
        )

    @pytest.mark.asyncio
    async def test_statement_count_does_not_grow_with_edit_density(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Re-attaching each message's surviving edit must not become a query per message."""
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=5))
        with _count_thread_statements(event_cache) as sparse_statements:
            await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=50))
        with _count_thread_statements(event_cache) as dense_statements:
            await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        assert len(sparse_statements) == len(dense_statements) == 1

    @pytest.mark.asyncio
    async def test_returned_rows_do_not_grow_with_edit_density(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A 10x edit density costs one extra row per message, not ten."""
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=10))
        sparse_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=100))
        dense_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        assert sparse_read is not None
        assert dense_read is not None
        assert len(sparse_read) == len(dense_read)


# Why the ranking is not ``DISTINCT ON`` is recorded where the query is, not as an assert here.
# A test that one string is absent from another cannot fail for any behavioural regression, only
# for the single rewrite its own comment already forbids.


def test_edit_ranking_is_scoped_to_this_thread_and_this_sender() -> None:
    """Both backends must rank edits over the same universe, grouped the same way.

    Ranking over a wider universe lets a row the outer query discards suppress the in-thread
    runner-up; grouping by a coarser key lets a foreign edit suppress the author's own. Both
    shipped as bugs, and both are invisible to a test that only exercises the fold.
    """
    for sql in (
        sqlite_event_cache_threads._THREAD_EVENTS_SQL,
        postgres_event_cache_threads._THREAD_EVENTS_SQL,
    ):
        assert "PARTITION BY" in sql, "edit ranking is not grouped at all"
        assert "sender" in sql, "edit ranking does not compare senders"
        assert "edit_membership.thread_id = " in sql, "edit ranking is not scoped to this thread"

    # Structural, deliberately, and the only guard available. The behavioural divergence needs a
    # collation where 'a' sorts before 'B'; the CI fixture pins postgres:15-alpine and musl has no
    # real locale support, so every libc collation there behaves like C and a seeded read cannot
    # fail whether or not the pin is present. This assertion fails wherever the pin is deleted.
    assert 'edit_event_id COLLATE "C" DESC' in postgres_event_cache_threads._THREAD_EVENTS_SQL, (
        "the Postgres tie-break is back on the database default collation"
    )


class TestEditsWhoseOriginalWasNeverCached:
    """An edit can outlive the message it replaces, and collapsing must not delete it.

    ``event_edits`` holds no foreign key to ``events``, so a thread can carry an edit whose
    original was never cached. The fold synthesizes a message from such an edit under the missing
    original's ID, carrying the editor's own sender. Collapsing anti-joins every edit out of the
    candidate set and re-attaches only those whose original is present to compare senders against,
    so an orphaned edit matches neither path and the message vanishes from the read entirely.
    """

    @pytest.mark.asyncio
    async def test_an_orphaned_edit_survives_the_collapse(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An edit whose original this thread never cached is still handed to the fold."""
        author = "@author:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$kept", 2_000, sender=author, thread_id=_THREAD_ID),
                _message_event("$orphan-edit", 3_000, sender=author, edit_of="$never-cached"),
            ],
        )

        rows = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert rows is not None

        assert "$orphan-edit" in {row["event_id"] for row in rows}, (
            "collapsing dropped an edit the fold would have synthesized a message from"
        )
        assert "$kept" in {row["event_id"] for row in rows}

    @pytest.mark.asyncio
    async def test_only_the_newest_orphaned_edit_per_missing_original_is_returned(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A missing original must not reintroduce the edit pile the collapse removes."""
        author = "@author:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                *[
                    _message_event(f"$orphan-edit{index}", 2_000 + index, sender=author, edit_of="$never-cached")
                    for index in range(5)
                ],
            ],
        )

        rows = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert rows is not None

        returned_orphans = {row["event_id"] for row in rows if row["event_id"].startswith("$orphan-edit")}
        assert returned_orphans == {"$orphan-edit4"}


class TestRedactionAcrossTheCollapsedRead:
    """T3 case 3 - a redacted original must not survive via its own edits, in either order.

    Redaction hard-deletes and tombstones, while collapsing changes which rows reach the fold, so
    redaction crossed with a collapsed read is an interaction this feature newly creates.
    """

    @pytest.mark.asyncio
    async def test_redacting_an_original_removes_it_from_the_read_despite_its_edits(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Edits stored before the redaction must not resurrect the redacted message."""
        events = [
            _message_event(_THREAD_ID, 1_000),
            _message_event("$victim", 2_000, thread_id=_THREAD_ID),
            _message_event("$victim-edit0", 2_100, thread_id=_THREAD_ID, edit_of="$victim"),
            _message_event("$victim-edit1", 2_200, thread_id=_THREAD_ID, edit_of="$victim"),
            _message_event("$survivor", 3_000, thread_id=_THREAD_ID),
        ]
        await _seed_thread(event_cache, events)

        assert await event_cache.redact_event(_ROOM_ID, "$victim") is True

        read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
        )
        assert read is not None

        returned_ids = {row["event_id"] for row in read}
        assert "$victim" not in returned_ids
        assert "$victim-edit0" not in returned_ids
        assert "$victim-edit1" not in returned_ids
        assert "$survivor" in returned_ids
        assert _THREAD_ID in returned_ids

    @pytest.mark.asyncio
    async def test_edit_arriving_after_its_original_was_redacted_is_refused(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A tombstoned original refuses a later edit, so the read cannot show it."""
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000),
                _message_event("$victim", 2_000, thread_id=_THREAD_ID),
            ],
        )
        assert await event_cache.redact_event(_ROOM_ID, "$victim") is True

        late_edit = _message_event("$victim-late", 5_000, thread_id=_THREAD_ID, edit_of="$victim")
        await event_cache.store_event("$victim-late", _ROOM_ID, late_edit)

        read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
        )

        returned_ids = {row["event_id"] for row in read or ()}
        assert "$victim-late" not in returned_ids
        assert "$victim" not in returned_ids

    @pytest.mark.asyncio
    async def test_redaction_does_not_evict_the_root_from_the_read(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Redacting a message must not cost the read its root, which would drop the cache."""
        await _seed_thread(event_cache, _thread_event_sources(8))

        assert await event_cache.redact_event(_ROOM_ID, "$m7") is True

        read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
        )
        assert read is not None
        assert read[0]["event_id"] == _THREAD_ID


class TestForeignEditCannotStarveTheAuthorsEdit:
    """A foreign replacement must not remove the author's own edit from the read.

    This is the seam tests/test_thread_edit_integrity.py cannot reach. That file hands the fold
    both candidates directly, so it proves the sender rule but never sees the SQL that decides
    which candidates the fold is given. Phase 2 originally shipped one latest edit across all
    senders; the fold then wanted a same-sender one, found none, and rendered the message at its
    pre-edit body - a rollback any room member could pin with a single m.replace.
    """

    @pytest.mark.asyncio
    async def test_read_keeps_the_authors_edit_when_a_foreign_edit_is_newer(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A newer foreign m.replace must not evict the author's own edit from the read."""
        author = "@author:localhost"
        attacker = "@attacker:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$victim", 2_000, sender=author, thread_id=_THREAD_ID),
                _message_event("$author-edit", 3_000, sender=author, edit_of="$victim"),
            ],
        )
        forged = _message_event("$forged", 9_000, sender=attacker, edit_of="$victim")
        assert (
            await event_cache.apply_thread_mutation_append(
                _ROOM_ID,
                _THREAD_ID,
                forged,
                append_failed_reason="test",
            )
        ).wrote_event

        read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
        )
        assert read is not None

        returned_ids = {row["event_id"] for row in read}
        assert "$author-edit" in returned_ids, "author's own edit was starved out of the read"
        assert "$victim" in returned_ids

    @pytest.mark.asyncio
    async def test_read_keeps_only_the_authors_newest_edit(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Only the original author's newest replacement survives selection.

        Earlier this shipped one winner per sender and left the fold to discard the foreign ones.
        The query now compares the edit's sender against the original's, so a foreign
        replacement is never returned at all.
        """
        author = "@author:localhost"
        other = "@other:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$victim", 2_000, sender=author, thread_id=_THREAD_ID),
                _message_event("$author-old", 3_000, sender=author, edit_of="$victim"),
                _message_event("$author-new", 4_000, sender=author, edit_of="$victim"),
                _message_event("$other-old", 5_000, sender=other, edit_of="$victim"),
                _message_event("$other-new", 6_000, sender=other, edit_of="$victim"),
            ],
        )

        read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
        )
        assert read is not None

        returned_ids = {row["event_id"] for row in read}
        assert "$author-new" in returned_ids
        assert "$author-old" not in returned_ids
        assert "$other-new" not in returned_ids, "a foreign edit must not be shipped or charged"
        assert "$other-old" not in returned_ids


def _winning_edit_ids_by_original(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    """Return, per original in ``rows``, the edit the fold would apply to it.

    Runs the real fold selection - candidates keyed per sender, winner matched against the
    original's own sender - over whichever row set it is handed.
    """
    candidates = ThreadEditCandidates()
    senders: dict[str, str] = {}
    for row in rows:
        if _is_edit(row):
            candidates.record(_nio_text_event(row), event_info=EventInfo.from_event(row))
        else:
            senders[row["event_id"]] = row["sender"]
    winners: dict[str, str | None] = {}
    for original_event_id, sender in senders.items():
        winner = candidates.winner_for(original_event_id, sender=sender)
        winners[original_event_id] = None if winner is None else winner[0].event_id
    return winners


def _nio_text_event(source: dict[str, Any]) -> nio.RoomMessageText:
    """Return the parsed nio event the fold would have been handed for one raw source."""
    return cast("nio.RoomMessageText", nio.RoomMessageText.from_dict({**source, "room_id": _ROOM_ID}))


class TestCollapsedReadAgreesWithTheFoldOnEveryEdit:
    """The invariant both edit-collapse defects violated, stated once.

    The query's ranking universe must be exactly the rows the read returns, and its grouping key
    must be exactly the fold's grouping key. Ranking over a wider universe lets a row the outer
    query later discards suppress the in-thread runner-up; grouping by a coarser key lets a foreign
    edit suppress the author's own.

    Expectations here are written out by hand rather than derived from a second read. A guard that
    compares one collapsed read against another cannot fail: both sides move together under any
    change to the query, which is exactly the change this is meant to catch.
    """

    @pytest.mark.asyncio
    async def test_the_query_hands_the_fold_the_edit_the_fold_should_pick(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Four messages, four different ways an edit can win, lose, or not count."""
        author = "@author:localhost"
        attacker = "@attacker:localhost"
        await _seed_thread(
            event_cache,
            [
                _message_event(_THREAD_ID, 1_000, sender=author),
                _message_event("$plain", 2_000, sender=author, thread_id=_THREAD_ID),
                _message_event("$edited", 3_000, sender=author, thread_id=_THREAD_ID),
                _message_event("$edited-e1", 3_100, sender=author, edit_of="$edited"),
                _message_event("$edited-e2", 3_200, sender=author, edit_of="$edited"),
                _message_event("$contested", 4_000, sender=author, thread_id=_THREAD_ID),
                _message_event("$contested-own", 4_100, sender=author, edit_of="$contested"),
            ],
        )
        # A foreign replacement, newer than the author's own.
        await event_cache.apply_thread_mutation_append(
            _ROOM_ID,
            _THREAD_ID,
            _message_event("$contested-forged", 8_000, sender=attacker, edit_of="$contested"),
            append_failed_reason="test",
        )
        # A same-sender replacement that is newer still but lives in a DIFFERENT thread of the
        # same room. It has to be in another thread rather than in none: an edit belonging to no
        # thread is excluded by the membership join whatever the thread predicate says, so a
        # fixture built that way cannot tell a correctly scoped ranking from an unscoped one.
        await _seed_other_thread(
            event_cache,
            [
                _message_event(_OTHER_THREAD_ID, 8_500, sender=author),
                _message_event("$edited-elsewhere", 9_000, sender=author, edit_of="$edited"),
            ],
        )

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert _winning_edit_ids_by_original(read) == {
            _THREAD_ID: None,
            "$plain": None,
            # The newest of the author's own in-thread edits. Not $edited-elsewhere, which is
            # newer and same-sender but sits in another thread, so ranking must never see it.
            "$edited": "$edited-e2",
            # The author's own, despite $contested-forged being newer: a replacement from anyone
            # but the original's sender is not an edit of that message.
            "$contested": "$contested-own",
        }

        returned_ids = {row["event_id"] for row in read}
        assert "$contested-forged" not in returned_ids, "a foreign replacement was returned"
        assert "$edited-elsewhere" not in returned_ids, "an out-of-thread replacement was returned"
        assert "$edited-e1" not in returned_ids, "a superseded edit was returned"


class TestRealisticEditDensity:
    """Real workloads measure 6.30 edits per edited original, max 170, one thread 94.5% edits.

    The failure this guards against is subtle and silent: an anti-join that excluded edited
    ORIGINALS rather than edit EVENTS would still return a plausible-looking read, just a tiny
    one - a handful of messages where the thread has twenty. No synthetic thread with a couple of
    edits per message would notice.
    """

    @pytest.mark.asyncio
    async def test_a_99_percent_edit_thread_still_returns_every_message(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """20 originals x 100 edits is 2,021 synthetic rows, 99% edits; all 20 messages return."""
        events = _thread_event_sources(20, edits_per_message=100)
        assert len(events) == 2_021
        await _seed_thread(event_cache, events)

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        covered = _original_event_ids(read)
        assert covered == [_THREAD_ID, *(f"$m{index}" for index in range(20))], (
            f"a 99%-edit thread of 20 messages returned {len(covered)}"
        )
        assert len(read) == 41, "one root, twenty messages, twenty surviving edits"

    @pytest.mark.asyncio
    async def test_one_heavily_edited_message_does_not_crowd_out_its_neighbours(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """170 edits on a single message collapse to one row and cost the others nothing."""
        events = [
            _message_event(_THREAD_ID, 1_000),
            *(_message_event(f"$m{index}", 2_000 + index * 1_000, thread_id=_THREAD_ID) for index in range(10)),
        ]
        events.extend(
            _message_event(
                f"$m3-edit{edit_index}",
                5_000 + edit_index,
                thread_id=_THREAD_ID,
                edit_of="$m3",
            )
            for edit_index in range(170)
        )
        await _seed_thread(event_cache, events)

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert _original_event_ids(read) == [_THREAD_ID, *(f"$m{index}" for index in range(10))]
        assert len(read) == 12, "the 170 edits of $m3 collapsed to one"


class TestAnEditDoesNotMoveItsMessage:
    """An edit is a correction to a message, not a new position in the conversation.

    The fold used to move an edited message to its edit timestamp while SQL ordered by the
    original timestamp, so the two disagreed about the order of the thread itself: one path put a
    late-edited first message last. Position stays immutable and the edit time is recorded
    separately.
    """

    @pytest.mark.asyncio
    async def test_a_late_edit_of_the_oldest_message_keeps_it_at_the_front(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Editing the oldest message long after the newest must not reorder the thread."""
        events = _thread_event_sources(12)
        # The oldest message is edited long after every later message was sent.
        events.append(_message_event("$m0-late", 999_000, thread_id=_THREAD_ID, edit_of="$m0"))
        await _seed_thread(event_cache, events)

        read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert read is not None

        assert _original_event_ids(read)[-3:] == ["$m9", "$m10", "$m11"], "the query reordered"

        # And the fold agrees. Asserting only the query would pass with apply_edit reverted, since
        # the SQL orders by the original timestamp either way - the disagreement this change exists
        # to fix is between the two, so both sides have to be run.
        folded = await _folded_messages(read)
        folded_ids = [message.event_id for message in folded]
        assert folded_ids[-3:] == ["$m9", "$m10", "$m11"], "the fold moved the late-edited message"
        assert folded_ids.index("$m0") < folded_ids.index("$m9")
        edited = next(message for message in folded if message.event_id == "$m0")
        assert edited.timestamp == 2_000, "an edit moved its message in the thread"
        assert edited.edited_timestamp == 999_000, "the edit's own time was not recorded"

    def test_applying_an_edit_keeps_the_original_position(self) -> None:
        """apply_edit records the edit's time separately instead of moving the message."""
        message = ResolvedVisibleMessage(
            sender="@user:localhost",
            body="original",
            timestamp=1_000,
            event_id="$m0",
            content={"body": "original", "msgtype": "m.text"},
            thread_id=None,
            latest_event_id="$m0",
        )

        message.apply_edit(
            body="edited",
            timestamp=999_000,
            latest_event_id="$m0-late",
            thread_id=None,
            content={"body": "edited", "msgtype": "m.text"},
        )

        assert message.timestamp == 1_000, "an edit must not move the message in the thread"
        assert message.edited_timestamp == 999_000
        assert message.body == "edited"
        assert message.latest_event_id == "$m0-late"


# The stale-export guard is asserted where it can actually fail, through the real export in
# tests/test_thread_export_execution.py: test_export_refuses_a_stale_cached_read_that_reports_
# itself_complete. The version that lived here only rebuilt two diagnostic helpers and compared
# them to what it had just constructed, so no change to export could have broken it.
