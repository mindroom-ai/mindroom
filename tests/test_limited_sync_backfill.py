"""Gap backfill for limited sync timelines: recovery, dedup, bounds, and failure tolerance."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.limited_sync_backfill import (
    _BACKFILL_PAGE_SIZE,
    _MAX_BACKFILL_EVENTS,
    _MAX_BACKFILL_PAGES,
    DeliveredEventTracker,
    _limited_joined_rooms,
    backfill_limited_sync_gaps,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOM_ID = "!flooded:localhost"


def _text_event(event_id: str, *, ts: int) -> nio.RoomMessageText:
    """Build one real nio text event so event_id/source/timestamp are populated."""
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": f"body {event_id}", "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": ts,
            "room_id": ROOM_ID,
            "type": "m.room.message",
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _room_info(events: Sequence[nio.Event], *, limited: bool, prev_batch: str | None) -> MagicMock:
    """Build one joined-room info shaped like nio's RoomInfo for a sync response."""
    return MagicMock(timeline=MagicMock(events=list(events), limited=limited, prev_batch=prev_batch))


def _sync_response(joined_rooms: dict[str, object]) -> MagicMock:
    """Build one SyncResponse-shaped mock carrying the given joined rooms."""
    response = MagicMock()
    response.__class__ = nio.SyncResponse
    response.rooms = MagicMock()
    response.rooms.join = joined_rooms
    return response


def _messages_response(events: Sequence[nio.Event], *, end: str | None) -> nio.RoomMessagesResponse:
    """Build one room_messages page; chunk is newest-first like the server returns."""
    return nio.RoomMessagesResponse(ROOM_ID, list(events), start="tok", end=end)


def _client_with_pages(pages: list[object]) -> MagicMock:
    """Build a client mock whose room_messages returns the given pages in order."""
    client = MagicMock()
    client.olm = None
    client.rooms = {ROOM_ID: nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@mindroom_general:localhost")}
    client.room_messages = AsyncMock(side_effect=pages)
    return client


class TestLimitedJoinedRooms:
    """Detection of limited joined-room timelines within a sync response."""

    def test_returns_only_limited_rooms(self) -> None:
        """Only rooms whose timeline arrived limited are returned."""
        response = _sync_response(
            {
                ROOM_ID: _room_info([], limited=True, prev_batch="p1"),
                "!calm:localhost": _room_info([], limited=False, prev_batch="p2"),
            },
        )
        assert set(_limited_joined_rooms(response)) == {ROOM_ID}

    def test_tolerates_malformed_join_section(self) -> None:
        """A non-dict join section yields no limited rooms instead of raising."""
        response = _sync_response("not-a-dict")  # type: ignore[arg-type]
        assert _limited_joined_rooms(response) == {}


class TestDeliveredEventTracker:
    """Per-room delivered-event memory used for backfill dedup and stop bounds."""

    def test_records_and_reports_seen(self) -> None:
        """Recorded ids report as seen; unrecorded ids and other rooms do not."""
        tracker = DeliveredEventTracker()
        tracker.record(ROOM_ID, ["$a", "$b"])
        assert tracker.has_seen(ROOM_ID, "$a")
        assert not tracker.has_seen(ROOM_ID, "$missing")
        assert not tracker.has_seen("!other:localhost", "$a")

    def test_forget_room_clears_memory(self) -> None:
        """Forgetting a departed room drops its delivered ids."""
        tracker = DeliveredEventTracker()
        tracker.record(ROOM_ID, ["$a"])
        tracker.forget_room(ROOM_ID)
        assert not tracker.has_seen(ROOM_ID, "$a")


class TestBackfill:
    """End-to-end gap recovery: dedup, ordering, bounds, and failure tolerance."""

    @pytest.mark.asyncio
    async def test_limited_room_recovers_and_dispatches_gap_in_order(self) -> None:
        """A limited timeline backfills the gap and dispatches it oldest-first."""
        # Surviving window in the sync response is $new; the gap holds $gap1,$gap2.
        # The boundary $old was delivered on a previous round.
        tracker = DeliveredEventTracker()
        tracker.record(ROOM_ID, ["$old"])
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$new", ts=300)], limited=True, prev_batch="p1")},
        )
        client = _client_with_pages(
            [
                _messages_response(
                    [_text_event("$gap2", ts=200), _text_event("$gap1", ts=100), _text_event("$old", ts=50)],
                    end="p2",
                ),
            ],
        )
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert dispatched == ["$gap1", "$gap2"]
        assert tracker.has_seen(ROOM_ID, "$gap1")
        assert tracker.has_seen(ROOM_ID, "$gap2")
        client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_events_in_sync_response_are_not_redispatched(self) -> None:
        """Events already present in the sync response are boundaries, never re-dispatched."""
        tracker = DeliveredEventTracker()
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$present", ts=300)], limited=True, prev_batch="p1")},
        )
        # room_messages overlaps the sync window: $present must stop the walk.
        client = _client_with_pages(
            [_messages_response([_text_event("$gap1", ts=200), _text_event("$present", ts=300)], end="p2")],
        )
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert dispatched == ["$gap1"]

    @pytest.mark.asyncio
    async def test_already_delivered_events_are_not_redispatched(self) -> None:
        """A gap event delivered on an earlier round is skipped on a later one."""
        tracker = DeliveredEventTracker()
        tracker.record(ROOM_ID, ["$gap1"])
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$new", ts=300)], limited=True, prev_batch="p1")},
        )
        client = _client_with_pages(
            [_messages_response([_text_event("$gap1", ts=100)], end="p2")],
        )
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert dispatched == []

    @pytest.mark.asyncio
    async def test_pagination_stops_at_page_bound(self) -> None:
        """A never-terminating history walk stops at the max-pages bound."""
        tracker = DeliveredEventTracker()
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$new", ts=10_000)], limited=True, prev_batch="p1")},
        )
        # Every page yields a fresh event and a fresh end token, so only the
        # page bound can stop the walk.
        counter = {"n": 0}

        async def endless(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
            counter["n"] += 1
            n = counter["n"]
            return _messages_response([_text_event(f"$gap{n}", ts=1000 - n)], end=f"tok{n}")

        client = MagicMock()
        client.olm = None
        client.rooms = {ROOM_ID: nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@mindroom_general:localhost")}
        client.room_messages = AsyncMock(side_effect=endless)
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert client.room_messages.await_count == _MAX_BACKFILL_PAGES
        assert len(dispatched) == _MAX_BACKFILL_PAGES

    @pytest.mark.asyncio
    async def test_event_bound_caps_recovered_events(self) -> None:
        """A single huge chunk is capped at the max-events bound."""
        tracker = DeliveredEventTracker()
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$new", ts=10**9)], limited=True, prev_batch="p1")},
        )
        huge = [_text_event(f"$gap{i}", ts=10**8 - i) for i in range(_MAX_BACKFILL_EVENTS + 50)]
        client = _client_with_pages([_messages_response(huge, end="p2")])
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert len(dispatched) == _MAX_BACKFILL_EVENTS

    @pytest.mark.asyncio
    async def test_room_messages_failure_is_tolerated(self) -> None:
        """A room_messages error on one room is logged and other rooms still backfill."""
        tracker = DeliveredEventTracker()
        other_room = "!second:localhost"
        response = _sync_response(
            {
                ROOM_ID: _room_info([_text_event("$new", ts=300)], limited=True, prev_batch="p1"),
                other_room: _room_info([_text_event("$new2", ts=300)], limited=True, prev_batch="q1"),
            },
        )
        client = MagicMock()
        client.olm = None
        client.rooms = {
            ROOM_ID: nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@mindroom_general:localhost"),
            other_room: nio.MatrixRoom(room_id=other_room, own_user_id="@mindroom_general:localhost"),
        }

        good_event = nio.RoomMessageText.from_dict(
            {
                "content": {"body": "recovered", "msgtype": "m.text"},
                "event_id": "$recovered",
                "sender": "@user:localhost",
                "origin_server_ts": 100,
                "room_id": other_room,
                "type": "m.room.message",
            },
        )

        backfill_error = RuntimeError("boom")

        async def room_messages(room_id: str, **_kwargs: object) -> nio.RoomMessagesResponse:
            if room_id == ROOM_ID:
                raise backfill_error
            return nio.RoomMessagesResponse(other_room, [good_event], start="q1", end="q2")

        client.room_messages = AsyncMock(side_effect=room_messages)
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert dispatched == ["$recovered"]

    @pytest.mark.asyncio
    async def test_missing_prev_batch_is_skipped(self) -> None:
        """A limited room without a prev_batch token cannot be paged and is skipped."""
        tracker = DeliveredEventTracker()
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$new", ts=300)], limited=True, prev_batch=None)},
        )
        client = _client_with_pages([])
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert dispatched == []
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_limited_rooms_makes_no_requests(self) -> None:
        """A response with no limited rooms never calls room_messages."""
        tracker = DeliveredEventTracker()
        response = _sync_response(
            {ROOM_ID: _room_info([_text_event("$new", ts=300)], limited=False, prev_batch="p1")},
        )
        client = _client_with_pages([])
        dispatched: list[str] = []

        async def dispatch(event: nio.Event, _room: nio.MatrixRoom) -> None:
            dispatched.append(event.event_id)

        await backfill_limited_sync_gaps(
            response,
            client=client,
            tracker=tracker,
            dispatch=dispatch,
            agent_name="general",
        )

        assert dispatched == []
        client.room_messages.assert_not_awaited()

    def test_page_size_within_event_bound(self) -> None:
        """The page size must not exceed the per-round event cap."""
        assert _BACKFILL_PAGE_SIZE <= _MAX_BACKFILL_EVENTS
