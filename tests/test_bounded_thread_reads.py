"""Bounded thread reads.

Two testing lessons this file exists to carry
---------------------------------------------
1. Test the seam, not one side of it. Every defect this feature shipped lived between the SQL and
   the in-memory fold, and tests that exercised each side separately all passed while the joined
   behaviour was wrong. tests/test_thread_edit_integrity.py asserts the same-sender rule against
   the fold and never executes the query that decides which candidates the fold is handed, so it
   stayed green through two bugs that broke exactly that rule. The guard that actually holds is
   TestWindowAgreesWithTheFullReadOnEveryEdit: for every message in the window, the fold's winner
   over the windowed rows must equal its winner over the full read.
2. Rerouting a read past a monkeypatched seam HANGS a test, it does not fail it. Twice on this
   feature a test kept passing its own setup and then waited out its timeout, which reads as a slow
   test rather than a broken one. If a test that patches a cache method starts timing out, check
   first whether production still calls the method it patched.

A windowed read must never disagree with a full read about the messages it returns; it may only
return fewer of them. The thread root is always pinned into the window so a bounded read still
carries the original question alongside the recent tail.

The bound is expressed in both messages and bytes because neither alone is right: a thousand
one-character messages cost a kilobyte and should come back whole, while twenty twenty-kilobyte
messages should not.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, cast

import nio
import pytest

from mindroom.matrix.cache import (
    postgres_event_cache_events,
    postgres_event_cache_threads,
    sqlite_event_cache_events,
    sqlite_event_cache_threads,
)
from mindroom.matrix.cache.thread_read_window import (
    DEFAULT_THREAD_READ_MAX_BYTES,
    ThreadReadBudget,
    ThreadWindowCandidate,
    select_thread_window_event_ids,
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
# phase-2 regression is a per-message payload lookup against the events table alone, which a
# filter naming only thread_events and event_edits counts as zero.
_THREAD_READ_TABLES = ("events", "thread_events", "event_edits")

_ROOM_ID = "!bounded:localhost"
_THREAD_ID = "$root"


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


def _payload_bytes(events: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(event, separators=(",", ":")).encode()) for event in events)


async def _seed_thread(
    event_cache: ConversationEventCache,
    events: list[dict[str, Any]],
) -> None:
    await replace_thread_unconditionally(event_cache, _ROOM_ID, _THREAD_ID, events)


class TestBoundedThreadReadEquivalence:
    """T1 - a bounded read agrees with the full read on every message it returns."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_messages", [1, 3, 10, 19, 20, 50])
    async def test_bounded_read_covers_the_newest_messages_of_the_full_read(
        self,
        event_cache: ConversationEventCache,
        max_messages: int,
    ) -> None:
        """A message-bounded read covers the newest ``max_messages`` messages plus the root."""
        await _seed_thread(event_cache, _thread_event_sources(20))

        full_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=max_messages),
        )
        assert full_read is not None
        assert bounded_read is not None

        full_messages = _original_event_ids(full_read)
        expected = full_messages[-max_messages:]
        if _THREAD_ID not in expected:
            expected = [_THREAD_ID, *expected]

        assert _original_event_ids(bounded_read) == expected
        assert all(row in full_read for row in bounded_read)

    @pytest.mark.asyncio
    async def test_edit_dominated_thread_bounds_messages_not_rows(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """20 messages x 100 edits: a window of 5 returns 5 messages, not 5 edits of one message."""
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=100))

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=5),
        )
        assert bounded_read is not None

        covered = _original_event_ids(bounded_read)
        assert len(covered) == 6
        assert covered[0] == _THREAD_ID
        assert covered[1:] == [f"$m{index}" for index in range(15, 20)]

    @pytest.mark.asyncio
    async def test_bounded_read_keeps_the_winning_edit_of_every_message_it_returns(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Each returned message carries the same latest edit the full read would collapse to."""
        await _seed_thread(event_cache, _thread_event_sources(10, edits_per_message=8))

        full_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=4),
        )
        assert full_read is not None
        assert bounded_read is not None

        full_winners = _latest_edit_by_original(full_read)
        bounded_winners = _latest_edit_by_original(bounded_read)
        assert bounded_winners
        for original_event_id, winner in bounded_winners.items():
            assert winner == full_winners[original_event_id]

    @pytest.mark.asyncio
    async def test_thousand_tiny_messages_are_returned_whole(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A cheap thread is not truncated: 1,000 one-character messages all come back."""
        events = _thread_event_sources(1_000, body_chars=1)
        await _seed_thread(event_cache, events)

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_bytes=1_000_000),
        )
        assert bounded_read is not None

        assert len(_original_event_ids(bounded_read)) == 1_001
        assert _payload_bytes(bounded_read) <= 1_000_000

    @pytest.mark.asyncio
    async def test_twenty_huge_messages_are_truncated_to_the_byte_budget(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An expensive thread is truncated: 20 x 20,000 chars does not all come back."""
        await _seed_thread(event_cache, _thread_event_sources(20, body_chars=20_000))

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_bytes=100_000),
        )
        assert bounded_read is not None

        covered = _original_event_ids(bounded_read)
        assert 1 < len(covered) < 21
        assert covered[0] == _THREAD_ID
        # The root is pinned in on top of the budget; the tail itself stays inside it.
        assert _payload_bytes([row for row in bounded_read if row["event_id"] != _THREAD_ID]) <= 100_000

    @pytest.mark.asyncio
    async def test_unbounded_budget_returns_every_stored_row(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An unbounded read is unchanged by this feature."""
        events = _thread_event_sources(8, edits_per_message=2)
        await _seed_thread(event_cache, events)

        full_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert full_read is not None
        assert len(full_read) == len(events)

    @pytest.mark.asyncio
    async def test_budget_at_and_above_thread_size_returns_the_whole_thread(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """``max_messages >= N`` degrades to the unbounded read exactly."""
        await _seed_thread(event_cache, _thread_event_sources(8))

        full_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        assert full_read is not None

        for max_messages in (9, 40):
            bounded_read = await event_cache.get_thread_events(
                _ROOM_ID,
                _THREAD_ID,
                budget=ThreadReadBudget(max_messages=max_messages),
            )
            assert bounded_read == full_read, f"max_messages={max_messages}"

    @pytest.mark.asyncio
    async def test_empty_thread_reads_as_a_miss_at_every_budget(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An unknown thread stays an advisory miss whether or not a window was requested."""
        assert await event_cache.get_thread_events(_ROOM_ID, "$absent") is None
        assert (
            await event_cache.get_thread_events(
                _ROOM_ID,
                "$absent",
                budget=ThreadReadBudget(max_messages=10, max_bytes=1_000),
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_root_outside_the_window_is_still_returned_first(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A tail window that excludes the root pins the root back in at the front."""
        await _seed_thread(event_cache, _thread_event_sources(30))

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=5),
        )
        assert bounded_read is not None

        assert bounded_read[0]["event_id"] == _THREAD_ID
        assert len(bounded_read) == 6

    @pytest.mark.asyncio
    async def test_single_message_budget_still_returns_a_message(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A budget too small for even one message still answers with the newest one."""
        await _seed_thread(event_cache, _thread_event_sources(6, body_chars=5_000))

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_bytes=1),
        )
        assert bounded_read is not None

        covered = _original_event_ids(bounded_read)
        assert covered == [_THREAD_ID, "$m5"]

    @pytest.mark.asyncio
    async def test_uniform_timestamps_window_deterministically(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Equal ``origin_server_ts`` rows still window to a stable, repeatable selection."""
        await _seed_thread(event_cache, _thread_event_sources(12, same_timestamp=True))

        first_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=4),
        )
        second_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=4),
        )
        assert first_read is not None
        assert first_read == second_read
        assert len(_original_event_ids(first_read)) == 5


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


class TestBoundedThreadReadCost:
    """T2 - a bounded read costs a fixed number of queries and returns O(window) rows."""

    @pytest.mark.asyncio
    async def test_statement_count_does_not_grow_with_thread_size(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A 10x larger thread costs the same number of queries: two, not two per message."""
        await _seed_thread(event_cache, _thread_event_sources(20))
        with _count_thread_statements(event_cache) as small_statements:
            await event_cache.get_thread_events(
                _ROOM_ID,
                _THREAD_ID,
                budget=ThreadReadBudget(max_messages=5),
            )

        await _seed_thread(event_cache, _thread_event_sources(200))
        with _count_thread_statements(event_cache) as large_statements:
            await event_cache.get_thread_events(
                _ROOM_ID,
                _THREAD_ID,
                budget=ThreadReadBudget(max_messages=5),
            )

        assert len(small_statements) == len(large_statements) == 2, (
            f"expected selection + payload only, got {len(small_statements)} and {len(large_statements)}"
        )

    @pytest.mark.asyncio
    async def test_statement_count_does_not_grow_with_edit_density(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Re-attaching each message's latest edit must not become a query per message."""
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=5))
        with _count_thread_statements(event_cache) as sparse_statements:
            await event_cache.get_thread_events(
                _ROOM_ID,
                _THREAD_ID,
                budget=ThreadReadBudget(max_messages=10),
            )

        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=50))
        with _count_thread_statements(event_cache) as dense_statements:
            await event_cache.get_thread_events(
                _ROOM_ID,
                _THREAD_ID,
                budget=ThreadReadBudget(max_messages=10),
            )

        assert len(sparse_statements) == len(dense_statements) == 2

    @pytest.mark.asyncio
    async def test_unbounded_read_is_a_single_statement(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An unbounded read keeps its single-query shape."""
        await _seed_thread(event_cache, _thread_event_sources(30))

        with _count_thread_statements(event_cache) as statements:
            await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)

        assert len(statements) == 1

    @pytest.mark.asyncio
    async def test_returned_rows_do_not_grow_with_thread_size(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """The same window over a 10x larger thread returns the same number of rows."""
        await _seed_thread(event_cache, _thread_event_sources(20))
        small_window = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=10),
        )

        await _seed_thread(event_cache, _thread_event_sources(200))
        large_window = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=10),
        )

        assert small_window is not None
        assert large_window is not None
        assert len(small_window) == len(large_window) == 11

    @pytest.mark.asyncio
    async def test_returned_rows_do_not_grow_with_edit_density(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A 10x edit density costs one extra row per message, not ten."""
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=10))
        sparse_window = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=5),
        )

        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=100))
        dense_window = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=5),
        )

        assert sparse_window is not None
        assert dense_window is not None
        assert len(sparse_window) == len(dense_window)


def _selection_sql() -> tuple[str, str]:
    """Return the window-selection statement for each backend."""
    return (
        sqlite_event_cache_threads._THREAD_WINDOW_CANDIDATES_SQL,
        postgres_event_cache_threads._THREAD_WINDOW_CANDIDATES_SQL,
    )


def test_window_selection_prices_rows_from_a_stored_size_column() -> None:
    """Selection reads ``event_bytes``, never a payload length computed at read time.

    This is the property the whole change rests on: ``length(event_json)`` would detoast every
    candidate on Postgres and read every overflow page on SQLite, which is the cost the bound
    exists to avoid.
    """
    for sql in _selection_sql():
        assert "event_bytes" in sql
        assert "event_json" not in sql
        assert "length(" not in sql.lower()
        assert "pg_column_size" not in sql


def test_postgres_window_selection_does_not_use_distinct_on() -> None:
    """``DISTINCT ON`` cannot push the bound down and measured slower than no bound at all."""
    assert "DISTINCT ON" not in _selection_sql()[1].upper()


def test_both_phases_scope_edits_to_this_thread_and_this_sender() -> None:
    """The two phases must agree on which edits exist, or the window disagrees with the fold.

    Selection prices the edits it expects the payload query to ship. If one phase scopes edits
    differently from the other - by thread, or by sender - the window is priced for one set and
    returns another, which is how both edit-window defects in this change were reachable.
    """
    payload_sql = (
        sqlite_event_cache_threads._THREAD_WINDOW_PAYLOAD_SQL,
        postgres_event_cache_threads._THREAD_WINDOW_PAYLOAD_SQL,
    )
    for sql in _selection_sql() + payload_sql:
        assert "PARTITION BY" in sql, "edit ranking is not grouped at all"
        assert "sender" in sql, "edit ranking is not grouped per sender"
        assert "edit_membership.thread_id = " in sql, "edit ranking is not scoped to this thread"


class TestTwoPhaseReadTolerance:
    """The gap between selection and payload fetch is normal, not an error."""

    @pytest.mark.asyncio
    async def test_event_redacted_between_the_phases_is_simply_absent(
        self,
        event_cache: ConversationEventCache,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A message selected in phase 1 and redacted before phase 2 comes back short, not broken.

        The redaction has to land *between* the phases for this to mean anything. Redacting before
        the read never creates the gap: selection simply never emits the row, the payload result is
        never short, and the test passes without exercising the tolerance it names.
        """
        await _seed_thread(event_cache, _thread_event_sources(6))
        is_sqlite = type(event_cache).__name__ == "SqliteEventCache"
        module = sqlite_event_cache_threads if is_sqlite else postgres_event_cache_threads
        events_module = sqlite_event_cache_events if is_sqlite else postgres_event_cache_events
        scope_key = "principal_id" if is_sqlite else "namespace"
        original_loader = module._load_thread_window_candidates

        async def redact_after_selection(db: object, **kwargs: str) -> list[ThreadWindowCandidate]:
            candidates = await original_loader(db, **kwargs)
            assert any(candidate.event_id == "$m5" for candidate in candidates), (
                "the message being redacted must be selected, or the gap is never created"
            )
            # Redact on the connection the read already holds. Calling the public redact_event()
            # from here would re-enter the runtime's non-reentrant _db_lock, which this very read
            # is holding for both phases, and deadlock rather than exercise the gap.
            await events_module.redact_event_locked(
                db,
                room_id=kwargs["room_id"],
                event_id="$m5",
                **{scope_key: kwargs[scope_key]},
            )
            return candidates

        monkeypatch.setattr(module, "_load_thread_window_candidates", redact_after_selection)

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=3),
        )
        assert bounded_read is not None

        covered = _original_event_ids(bounded_read)
        assert "$m5" not in covered, "a redacted payload must not be served"
        assert covered[0] == _THREAD_ID


class TestStoredPayloadSizeStaysCurrent:
    """The stored size must track ``event_json``, which is not immutable."""

    @pytest.mark.asyncio
    async def test_opaque_to_clear_upgrade_refreshes_the_stored_size(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """An encrypted payload replaced by a much larger clear one must not keep the small size.

        ``ON CONFLICT DO UPDATE`` re-assigns ``event_json``; if it does not also re-assign
        ``event_bytes`` the budget under-counts and the window silently returns too many rows.
        """
        # Cheap older messages, then a newest message stored first as small ciphertext.
        events = [_message_event(_THREAD_ID, 1_000, body="root")]
        events += [
            _message_event(f"$m{index}", 2_000 + index * 1_000, body="tiny", thread_id=_THREAD_ID) for index in range(5)
        ]
        events.append(
            {
                "event_id": "$newest",
                "sender": "@user:localhost",
                "origin_server_ts": 9_000,
                "type": "m.room.encrypted",
                "content": {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "short"},
            },
        )
        await _seed_thread(event_cache, events)

        clear = _message_event("$newest", 9_000, body="y" * 40_000, thread_id=_THREAD_ID)
        await event_cache.store_event("$newest", _ROOM_ID, clear)

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_bytes=5_000),
        )
        assert bounded_read is not None

        # With a refreshed size the newest message alone busts the budget, so only it and the
        # pinned root come back. A stale ciphertext-sized row would let all five tiny messages in.
        assert _original_event_ids(bounded_read) == [_THREAD_ID, "$newest"]
        stored_clear = next(row for row in bounded_read if row["event_id"] == "$newest")
        assert len(stored_clear["content"]["body"]) == 40_000


class TestWindowTruncationIsReported:
    """A bound that fires is a signal, not routine truncation."""

    def test_selection_reports_which_bound_stopped_the_walk(self) -> None:
        """Callers can tell a byte cut from a message cut, and an untruncated read from either."""
        candidates = [ThreadWindowCandidate(event_id=f"$m{index}", window_bytes=100) for index in range(10)]

        by_bytes = select_thread_window_event_ids(candidates, budget=ThreadReadBudget(max_bytes=250))
        assert by_bytes.stopped_at_max_bytes is True
        assert by_bytes.stopped_at_max_messages is False
        assert by_bytes.truncated is True
        assert by_bytes.selected_bytes == 200

        by_messages = select_thread_window_event_ids(candidates, budget=ThreadReadBudget(max_messages=3))
        assert by_messages.stopped_at_max_messages is True
        assert by_messages.stopped_at_max_bytes is False
        assert by_messages.truncated is True

        untruncated = select_thread_window_event_ids(
            candidates,
            budget=ThreadReadBudget(max_messages=50, max_bytes=1_000_000),
        )
        assert untruncated.truncated is False
        assert untruncated.stopped_at_max_bytes is False
        assert untruncated.stopped_at_max_messages is False

    def test_oversized_newest_message_is_returned_and_reported(self) -> None:
        """The newest-message floor still counts as truncation so it gets logged."""
        candidates = [ThreadWindowCandidate(event_id=f"$m{index}", window_bytes=50_000) for index in range(4)]

        selection = select_thread_window_event_ids(candidates, budget=ThreadReadBudget(max_bytes=1))

        assert selection.event_ids == ["$m0"]
        assert selection.truncated is True
        assert selection.stopped_at_max_bytes is True


class TestRedactionAcrossTheWindow:
    """T3 case 3 - a redacted original must not survive via its own edits, in either order.

    Redaction hard-deletes and tombstones, while windowing changes which rows reach the fold, so
    redaction crossed with a bounded read is an interaction this feature newly creates.
    """

    @pytest.mark.asyncio
    async def test_redacting_an_original_removes_it_from_the_window_despite_its_edits(
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

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=10),
        )
        assert bounded_read is not None

        returned_ids = {row["event_id"] for row in bounded_read}
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
        """A tombstoned original refuses a later edit, so the window cannot show it."""
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

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=10),
        )

        returned_ids = {row["event_id"] for row in bounded_read or ()}
        assert "$victim-late" not in returned_ids
        assert "$victim" not in returned_ids

    @pytest.mark.asyncio
    async def test_redaction_does_not_evict_the_pinned_root_from_the_window(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Redacting a message must not cost the window its root, which would drop the cache."""
        await _seed_thread(event_cache, _thread_event_sources(8))

        assert await event_cache.redact_event(_ROOM_ID, "$m7") is True

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=2),
        )
        assert bounded_read is not None
        assert bounded_read[0]["event_id"] == _THREAD_ID


class TestForeignEditCannotStarveTheAuthorsEdit:
    """A foreign replacement must not remove the author's own edit from the window.

    This is the seam tests/test_thread_edit_integrity.py cannot reach. That file hands the fold
    both candidates directly, so it proves the sender rule but never sees the SQL that decides
    which candidates the fold is given. Phase 2 originally shipped one latest edit across all
    senders; the fold then wanted a same-sender one, found none, and rendered the message at its
    pre-edit body - a rollback any room member could pin with a single m.replace.
    """

    @pytest.mark.asyncio
    async def test_window_keeps_the_authors_edit_when_a_foreign_edit_is_newer(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A newer foreign m.replace must not evict the author's own edit from the window."""
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

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=50),
        )
        assert bounded_read is not None

        returned_ids = {row["event_id"] for row in bounded_read}
        assert "$author-edit" in returned_ids, "author's own edit was starved out of the window"
        assert "$victim" in returned_ids

    @pytest.mark.asyncio
    async def test_window_keeps_the_newest_edit_of_each_sender(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Each sender contributes its own newest candidate, and only its newest."""
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

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=50),
        )
        assert bounded_read is not None

        returned_ids = {row["event_id"] for row in bounded_read}
        assert "$author-new" in returned_ids
        assert "$other-new" in returned_ids
        assert "$author-old" not in returned_ids
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


class TestWindowAgreesWithTheFullReadOnEveryEdit:
    """The invariant both edit-window defects violated, stated once.

    Phase 2's ranking universe must be exactly the row set the unbounded read returns, and its
    grouping key must be exactly the fold's grouping key. Ranking over a wider universe lets a row
    the outer query later discards suppress the in-thread runner-up; grouping by a coarser key lets
    a foreign edit suppress the author's own.
    """

    @pytest.mark.asyncio
    async def test_every_windowed_message_resolves_to_the_same_edit_as_the_full_read(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """For each message in the window, the fold picks the same edit it would unbounded."""
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
        # A newer foreign replacement, and a newer same-sender replacement that is not in the thread.
        await event_cache.apply_thread_mutation_append(
            _ROOM_ID,
            _THREAD_ID,
            _message_event("$contested-forged", 8_000, sender=attacker, edit_of="$contested"),
            append_failed_reason="test",
        )
        await event_cache.store_event(
            "$edited-orphan",
            _ROOM_ID,
            _message_event("$edited-orphan", 9_000, sender=author, edit_of="$edited"),
        )

        full_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=50),
        )
        assert full_read is not None
        assert bounded_read is not None

        full_winners = _winning_edit_ids_by_original(full_read)
        bounded_winners = _winning_edit_ids_by_original(bounded_read)

        assert bounded_winners
        for original_event_id, winner in bounded_winners.items():
            assert winner == full_winners[original_event_id], (
                f"{original_event_id}: window resolves to {winner}, full read resolves to "
                f"{full_winners[original_event_id]}"
            )


class TestTruncationIsVisibleToCallers:
    """A window that left messages out must not be reported as full history.

    ``is_full_history`` gates completeness-dependent planning and the model-history refresh, so a
    truncated tail presented as complete silently drops older participants and mentions from the
    context with nothing to notice it by.
    """

    @pytest.mark.asyncio
    async def test_window_reports_truncation_only_when_it_dropped_something(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """``truncated`` is the real answer, not an approximation in either direction."""
        await _seed_thread(event_cache, _thread_event_sources(20))

        cut = await event_cache.get_thread_window(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=5),
        )
        assert cut.truncated is True
        assert cut.events is not None

        # A budget the thread fits inside must not claim truncation, or every read forces a refresh.
        whole = await event_cache.get_thread_window(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=500, max_bytes=10_000_000),
        )
        assert whole.truncated is False
        assert whole.events is not None
        assert len(whole.events) == 21

        unbounded = await event_cache.get_thread_window(_ROOM_ID, _THREAD_ID)
        assert unbounded.truncated is False

    @pytest.mark.asyncio
    async def test_absent_thread_is_not_truncated(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A miss is a miss, not a truncated window."""
        window = await event_cache.get_thread_window(
            _ROOM_ID,
            "$absent",
            budget=ThreadReadBudget(max_messages=5),
        )

        assert window.events is None
        assert window.truncated is False


class TestProductionShapedEditDensity:
    """Production threads run ~94% edits, ~6 edits per edited original, up to 170 on one.

    The failure this guards against is subtle and silent: an anti-join that excluded edited
    ORIGINALS rather than edit EVENTS would still return a plausible-looking window, just a tiny
    one - a handful of messages where the caller asked for fifty. No synthetic thread with a couple
    of edits per message would notice.
    """

    @pytest.mark.asyncio
    async def test_asking_for_fifty_on_a_99_percent_edit_thread_returns_every_message(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """20 originals x 100 edits is 99% edit rows; a budget of 50 must still see all 20."""
        events = _thread_event_sources(20, edits_per_message=100)
        assert len(events) == 2_021
        await _seed_thread(event_cache, events)

        window = await event_cache.get_thread_window(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=50),
        )
        assert window.events is not None

        covered = _original_event_ids(window.events)
        assert covered == [_THREAD_ID, *(f"$m{index}" for index in range(20))], (
            f"asked for 50 messages on a 99%-edit thread and got {len(covered)}"
        )
        assert window.truncated is False, "the whole thread fits in 50 messages; nothing was dropped"

    @pytest.mark.asyncio
    async def test_uneven_edit_density_does_not_bias_which_messages_are_selected(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """One heavily edited message must not crowd out its unedited neighbours."""
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

        window = await event_cache.get_thread_window(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=5),
        )
        assert window.events is not None

        covered = _original_event_ids(window.events)
        assert covered == [_THREAD_ID, "$m5", "$m6", "$m7", "$m8", "$m9"]


class TestTailAgreesAfterALateEdit:
    """Bounded and unbounded reads must pick the same tail after an old message is edited.

    The fold used to move an edited message to its edit timestamp while SQL selection ordered by
    the original timestamp, so the two disagreed about which messages the tail even contained: the
    full read ended ...m199, m200, m0 while the window returned m198, m199, m200. An edit is a
    correction to a message, not a new position in the conversation, so position stays immutable
    and both paths order the same way.
    """

    @pytest.mark.asyncio
    async def test_editing_the_oldest_message_does_not_move_it_into_the_tail(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A late edit of the oldest message leaves the tail unchanged on both paths."""
        events = _thread_event_sources(12)
        # The oldest message is edited long after every later message was sent.
        events.append(_message_event("$m0-late", 999_000, thread_id=_THREAD_ID, edit_of="$m0"))
        await _seed_thread(event_cache, events)

        full_read = await event_cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=3),
        )
        assert full_read is not None
        assert bounded_read is not None

        # The window's tail is the newest three messages by original position, not the edited one.
        assert _original_event_ids(bounded_read) == [_THREAD_ID, "$m9", "$m10", "$m11"]
        assert "$m0" not in _original_event_ids(bounded_read)

        # And the full read agrees: the edited message keeps its original place, near the front.
        full_messages = _original_event_ids(full_read)
        assert full_messages[-3:] == ["$m9", "$m10", "$m11"]
        assert full_messages.index("$m0") < full_messages.index("$m9")

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


class TestDefaultsDoNotWindowRealThreads:
    """The default budget must not act as a sliding window on a normal thread.

    A fixed message count slides - M1..M200 becomes M2..M201 - which changes the prompt prefix
    every turn and defeats provider prefix caching, and it drops messages upstream of compaction
    where nothing can ever summarize them. The reduction this read exists for comes from collapsing
    edits, not from dropping messages, so the count is a pathological guard well above any real
    thread.
    """

    def test_there_is_no_default_message_bound(self) -> None:
        """Nothing on the model-facing path may window by message count.

        A count slides, which changes the prompt prefix every turn and defeats provider prefix
        caching, and it drops messages upstream of compaction where nothing can summarize them.
        """
        from mindroom.matrix.cache import thread_read_window  # noqa: PLC0415

        assert not hasattr(thread_read_window, "DEFAULT_THREAD_READ_MAX_MESSAGES")

    @pytest.mark.asyncio
    async def test_a_thread_past_the_old_cap_is_returned_whole(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """201 messages come back complete and untruncated under the default budget."""
        await _seed_thread(event_cache, _thread_event_sources(201))

        window = await event_cache.get_thread_window(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_bytes=DEFAULT_THREAD_READ_MAX_BYTES),
        )
        assert window.events is not None

        assert window.truncated is False
        assert len(_original_event_ids(window.events)) == 202

    @pytest.mark.asyncio
    async def test_consecutive_reads_keep_a_stable_prefix_as_the_thread_grows(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Appending a message must extend the read, not slide it - or prefix caching cannot hit."""
        budget = ThreadReadBudget(max_bytes=DEFAULT_THREAD_READ_MAX_BYTES)
        await _seed_thread(event_cache, _thread_event_sources(201))
        before = await event_cache.get_thread_window(_ROOM_ID, _THREAD_ID, budget=budget)

        await _seed_thread(event_cache, _thread_event_sources(202))
        after = await event_cache.get_thread_window(_ROOM_ID, _THREAD_ID, budget=budget)

        assert before.events is not None
        assert after.events is not None
        before_messages = _original_event_ids(before.events)
        after_messages = _original_event_ids(after.events)

        assert after_messages[: len(before_messages)] == before_messages, (
            "the read slid instead of extending; a changed prefix defeats provider prompt caching"
        )

    @pytest.mark.asyncio
    async def test_edit_collapse_is_where_the_reduction_comes_from(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """A 99%-edit thread collapses to a fraction of its rows without losing a message."""
        await _seed_thread(event_cache, _thread_event_sources(20, edits_per_message=100))

        window = await event_cache.get_thread_window(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_bytes=DEFAULT_THREAD_READ_MAX_BYTES),
        )
        assert window.events is not None

        assert len(_original_event_ids(window.events)) == 21, "no message may be lost"
        assert len(window.events) <= 42, "one winning edit per message, not every edit ever seen"
        assert window.truncated is False


class TestStaleReadsAreNotPassedOffAsAuthoritative:
    """Completeness and freshness are separate signals, and export needs both.

    A stale fallback sets the degraded diagnostic but still reports is_full_history=True whenever
    its window happened not to truncate, so a caller checking only completeness will write stale
    rows out as authoritative history.
    """

    def test_a_stale_result_can_be_complete_and_must_still_be_refused(self) -> None:
        """The two signals are independent, which is why export checks both."""
        from mindroom.matrix.cache import thread_history_result  # noqa: PLC0415
        from mindroom.matrix.thread_diagnostics import (  # noqa: PLC0415
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
            THREAD_HISTORY_SOURCE_DIAGNOSTIC,
            THREAD_HISTORY_SOURCE_STALE_CACHE,
            is_thread_history_degraded,
        )

        stale_but_untruncated = thread_history_result(
            [],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
            },
        )

        assert stale_but_untruncated.is_full_history is True
        assert is_thread_history_degraded(stale_but_untruncated) is True, (
            "completeness alone cannot tell a caller the rows are current"
        )
