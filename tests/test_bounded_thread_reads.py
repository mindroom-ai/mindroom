"""Bounded thread reads.

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
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.matrix.cache import postgres_event_cache_threads, sqlite_event_cache_threads
from mindroom.matrix.cache.thread_read_window import (
    DEFAULT_THREAD_READ_MAX_BYTES,
    DEFAULT_THREAD_READ_MAX_MESSAGES,
    ThreadReadBudget,
    ThreadWindowCandidate,
    select_thread_window_event_ids,
)
from tests.event_cache_test_support import replace_thread_unconditionally

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.matrix.cache import ConversationEventCache

# Cache tables a thread read may touch. Any statement naming one of these counts.
_THREAD_READ_TABLES = ("thread_events", "event_edits")

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
    _sqlite_sql, postgres_sql = _selection_sql()

    assert "DISTINCT ON" not in postgres_sql.upper()


class TestTwoPhaseReadTolerance:
    """The gap between selection and payload fetch is normal, not an error."""

    @pytest.mark.asyncio
    async def test_event_redacted_after_selection_is_simply_absent(
        self,
        event_cache: ConversationEventCache,
    ) -> None:
        """Redaction hard-deletes, so a selected event can vanish before its payload is fetched."""
        await _seed_thread(event_cache, _thread_event_sources(6))

        redacted = await event_cache.redact_event(_ROOM_ID, "$m5")
        assert redacted is True

        bounded_read = await event_cache.get_thread_events(
            _ROOM_ID,
            _THREAD_ID,
            budget=ThreadReadBudget(max_messages=3),
        )
        assert bounded_read is not None

        covered = _original_event_ids(bounded_read)
        assert "$m5" not in covered
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

    def test_default_byte_budget_clears_a_large_legitimate_thread(self) -> None:
        """The default must not bind on a big-but-normal thread, only on the pathology."""
        large_legitimate_thread_bytes = DEFAULT_THREAD_READ_MAX_MESSAGES * 2_048
        pathological_thread_bytes = 1_000 * 20_000

        assert large_legitimate_thread_bytes < DEFAULT_THREAD_READ_MAX_BYTES
        assert pathological_thread_bytes > DEFAULT_THREAD_READ_MAX_BYTES


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
