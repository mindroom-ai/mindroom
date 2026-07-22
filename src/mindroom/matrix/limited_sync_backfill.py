"""Recover Matrix timeline events skipped by a limited sync response.

When a room floods faster than the sync loop drains it, the homeserver
truncates that room's timeline and marks it ``limited: true``. nio only
delivers the events present in the truncated response, so every event in the
gap between the previous sync token and the surviving window is dropped: no
``message`` callback ever fires for it and the agent silently ignores whatever
the user said. The thread cache already detects this and invalidates the room
(``limited_sync_timeline``), but nothing recovers the events for the
message-handling callbacks.

This module closes that gap. For each joined room whose timeline arrived
``limited``, it pages backwards from the response's ``prev_batch`` token via
``room_messages`` until it reaches an event this agent has already delivered
(or a bound), collects the events in the gap, and re-dispatches them through
the same nio fan-out (:meth:`AsyncClient._on_event`) the live sync path uses,
in chronological order. Dedup by event id makes double delivery impossible:
events already present in the sync response, and events already delivered on a
previous round, are never re-dispatched.

Backfill never runs on the first sync (initial sync is limited everywhere by
design) and never raises into the sync loop: any failure is logged and the
loop continues.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import origin_server_ts_from_event_source

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

logger = get_logger(__name__)

# One backfill fetches at most this many pages of history per room. A limited
# window means the sync round fell behind by more than the server timeline
# limit (~10 events); a few pages of headroom recovers realistic floods without
# letting a pathological room walk history forever.
_MAX_BACKFILL_PAGES = 10
# Hard cap on recovered events per room per round, independent of pages, so a
# server returning huge chunks cannot flood the turn pipeline in one go.
_MAX_BACKFILL_EVENTS = 200
# Events requested per ``room_messages`` page.
_BACKFILL_PAGE_SIZE = 50
# Per-room delivered-event-id memory. Bounds the dedup/stop set so a
# long-running process cannot grow it without limit; older ids age out, which
# is safe because the pagination bound stops the walk before reaching them.
_DELIVERED_IDS_PER_ROOM_MAX = 512


@dataclass
class DeliveredEventTracker:
    """Remember recently delivered event ids per room for backfill dedup.

    The tracker records every event id this agent has handled from a live sync
    timeline. Backfill consults it to know where the gap ends (the first
    already-delivered event, walking backwards) and to skip events it has
    already dispatched.
    """

    _by_room: dict[str, OrderedDict[str, None]] = field(default_factory=dict)

    def record(self, room_id: str, event_ids: Iterable[str]) -> None:
        """Mark one room's event ids as delivered, evicting the oldest over the cap."""
        seen = self._by_room.setdefault(room_id, OrderedDict())
        for event_id in event_ids:
            if event_id in seen:
                seen.move_to_end(event_id)
            else:
                seen[event_id] = None
        while len(seen) > _DELIVERED_IDS_PER_ROOM_MAX:
            seen.popitem(last=False)

    def has_seen(self, room_id: str, event_id: str) -> bool:
        """Return whether one event id was already delivered for a room."""
        seen = self._by_room.get(room_id)
        return seen is not None and event_id in seen

    def forget_room(self, room_id: str) -> None:
        """Drop all delivered ids for a departed room."""
        self._by_room.pop(room_id, None)


def _limited_joined_rooms(response: nio.SyncResponse) -> dict[str, nio.responses.RoomInfo]:
    """Return joined rooms in one sync response whose timeline arrived limited."""
    try:
        joined_rooms = response.rooms.join
    except AttributeError:
        return {}
    if not isinstance(joined_rooms, dict):
        return {}
    limited: dict[str, nio.responses.RoomInfo] = {}
    for room_id, room_info in joined_rooms.items():
        if not isinstance(room_id, str) or room_info is None:
            continue
        timeline = getattr(room_info, "timeline", None)
        if timeline is not None and getattr(timeline, "limited", False):
            limited[room_id] = room_info
    return limited


def _timeline_event_ids(room_info: nio.responses.RoomInfo) -> set[str]:
    """Return the event ids already present in one room's sync timeline."""
    timeline = getattr(room_info, "timeline", None)
    events = [] if timeline is None else getattr(timeline, "events", [])
    return {event.event_id for event in events if getattr(event, "event_id", None)}


def _event_source(event: nio.Event) -> object:
    """Return the raw source mapping for one recovered event, if any."""
    return getattr(event, "source", None)


def _maybe_decrypt(client: nio.AsyncClient, room_id: str, event: nio.Event) -> nio.Event:
    """Decrypt one recovered Megolm event, mirroring the live timeline path.

    Undecryptable events are returned unchanged so the Megolm callback fires
    for them exactly as it would for a live sync event, keeping decryption
    failure handling identical between the live and recovered paths.
    """
    if not isinstance(event, nio.MegolmEvent):
        return event
    if getattr(client, "olm", None) is None:
        return event
    event.room_id = room_id
    try:
        decrypted = client.decrypt_event(event)
    except Exception:
        return event
    if isinstance(decrypted, nio.Event):
        return decrypted
    return event


@dataclass
class _RoomBackfillResult:
    """Outcome of paginating one limited room's gap."""

    recovered: list[nio.Event]
    pages: int
    reached_seen: bool
    hit_bound: bool


async def _collect_gap_events(
    client: nio.AsyncClient,
    room_id: str,
    prev_batch: str,
    tracker: DeliveredEventTracker,
    already_present: set[str],
) -> _RoomBackfillResult:
    """Page backwards from ``prev_batch`` collecting undelivered gap events.

    Walks history newest-first via ``room_messages`` until it reaches an event
    already delivered for this room, exhausts the room's history, or hits the
    page/event bound. Events already present in the sync response are treated as
    boundary markers, not recovered, so the response's surviving window is never
    re-dispatched.
    """
    recovered: list[nio.Event] = []
    token = prev_batch
    pages = 0
    reached_seen = False
    hit_bound = False
    while pages < _MAX_BACKFILL_PAGES:
        response = await client.room_messages(
            room_id,
            start=token,
            direction=nio.MessageDirection.back,
            limit=_BACKFILL_PAGE_SIZE,
        )
        pages += 1
        if not isinstance(response, nio.RoomMessagesResponse):
            hit_bound = True
            break
        chunk = [event for event in response.chunk if isinstance(event, nio.Event)]
        for event in chunk:
            event_id = getattr(event, "event_id", None)
            if event_id is None:
                continue
            if event_id in already_present or tracker.has_seen(room_id, event_id):
                reached_seen = True
                break
            if event_id in {getattr(e, "event_id", None) for e in recovered}:
                continue
            recovered.append(event)
            if len(recovered) >= _MAX_BACKFILL_EVENTS:
                hit_bound = True
                break
        if reached_seen or hit_bound:
            break
        next_token = response.end
        if not next_token or next_token == token:
            break
        token = next_token
    else:
        hit_bound = True
    return _RoomBackfillResult(recovered=recovered, pages=pages, reached_seen=reached_seen, hit_bound=hit_bound)


def _chronological(events: list[nio.Event]) -> list[nio.Event]:
    """Return recovered events oldest-first for in-order dispatch.

    ``room_messages`` walks backwards, so both within and across pages events
    arrive newest-first; reversing restores arrival order. Origin timestamps
    break ties defensively without reordering the server's own sequence.
    """
    reversed_events = list(reversed(events))
    return sorted(
        reversed_events,
        key=lambda event: origin_server_ts_from_event_source(_event_source(event)) or 0,
    )


async def backfill_limited_sync_gaps(
    response: nio.SyncResponse,
    *,
    client: nio.AsyncClient,
    tracker: DeliveredEventTracker,
    dispatch: Callable[[nio.Event, nio.MatrixRoom], Awaitable[None]],
    agent_name: str,
) -> None:
    """Recover and re-dispatch events skipped by limited timelines in one sync.

    For every joined room whose timeline arrived ``limited``, page backwards
    from its ``prev_batch``, collect the gap, and dispatch each recovered event
    through ``dispatch`` (the live nio fan-out) in chronological order. Failures
    per room are logged and skipped; this never raises into the sync loop.
    """
    limited = _limited_joined_rooms(response)
    if not limited:
        return
    for room_id, room_info in limited.items():
        timeline = getattr(room_info, "timeline", None)
        prev_batch = None if timeline is None else getattr(timeline, "prev_batch", None)
        if not prev_batch:
            logger.warning(
                "limited_sync_gap_backfill_skipped_no_prev_batch",
                agent_name=agent_name,
                room_id=room_id,
            )
            continue
        already_present = _timeline_event_ids(room_info)
        try:
            result = await _collect_gap_events(client, room_id, prev_batch, tracker, already_present)
        except Exception:
            logger.exception(
                "limited_sync_gap_backfill_failed",
                agent_name=agent_name,
                room_id=room_id,
            )
            continue

        room = client.rooms.get(room_id)
        if room is None:
            logger.warning(
                "limited_sync_gap_backfill_skipped_unknown_room",
                agent_name=agent_name,
                room_id=room_id,
                recovered_count=len(result.recovered),
            )
            continue

        dispatched = 0
        for event in _chronological(result.recovered):
            event_id = getattr(event, "event_id", None)
            if event_id is None or tracker.has_seen(room_id, event_id):
                continue
            prepared = _maybe_decrypt(client, room_id, event)
            try:
                await dispatch(prepared, room)
            except Exception:
                logger.exception(
                    "limited_sync_gap_backfill_dispatch_failed",
                    agent_name=agent_name,
                    room_id=room_id,
                    event_id=event_id,
                )
                continue
            tracker.record(room_id, (event_id,))
            dispatched += 1

        logger.info(
            "limited_sync_gap_backfill",
            agent_name=agent_name,
            room_id=room_id,
            recovered_count=dispatched,
            candidate_count=len(result.recovered),
            pages=result.pages,
            reached_seen=result.reached_seen,
            hit_bound=result.hit_bound,
        )
