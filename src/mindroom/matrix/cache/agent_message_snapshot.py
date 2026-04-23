"""Snapshot reads for the latest visible agent message in one cached scope."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import event_cache_events, event_cache_threads
from .thread_cache_helpers import thread_cache_rejection_reason

if TYPE_CHECKING:
    import aiosqlite


@dataclass(frozen=True, slots=True)
class AgentMessageSnapshot:
    """Latest visible message content and timestamp for one sender."""

    content: dict[str, Any]
    origin_server_ts: int


class AgentMessageSnapshotUnavailable(RuntimeError):  # noqa: N818
    """Raised when an existing Matrix event cache cannot be safely read."""


@dataclass(frozen=True, slots=True)
class _SnapshotLookupResult:
    """Outcome for one matching scope event during latest-message lookup."""

    snapshot: AgentMessageSnapshot | None
    stop_scanning: bool = False


_THREAD_CACHE_REJECTION_NONE_REASONS = frozenset({"no_cache_state", "cache_never_validated"})


def _content_dict(content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    return {key: value for key, value in content.items() if isinstance(key, str)}


def _relation_type(event: dict[str, Any]) -> str | None:
    content = _content_dict(event.get("content"))
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, dict):
        return None
    relation_type = relates_to.get("rel_type")
    return relation_type if isinstance(relation_type, str) else None


def _visible_content(event: dict[str, Any]) -> dict[str, Any]:
    content = _content_dict(event.get("content"))
    new_content = _content_dict(content.get("m.new_content"))
    return new_content or content


def _event_matches_scope(
    event: dict[str, Any],
    *,
    thread_id: str | None,
    sender: str,
) -> bool:
    if event.get("type") != "m.room.message" or event.get("sender") != sender:
        return False
    relation_type = _relation_type(event)
    if relation_type == "m.replace":
        return False
    return not (thread_id is None and relation_type == "m.thread")


async def _thread_scope_has_no_snapshot(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    runtime_started_at: float | None,
    now: float | None,
) -> bool:
    if thread_id is None:
        return False

    rejection_reason = thread_cache_rejection_reason(
        await event_cache_threads.load_thread_cache_state(
            db,
            room_id=room_id,
            thread_id=thread_id,
        ),
        runtime_started_at=runtime_started_at,
        now=now,
    )
    if rejection_reason in _THREAD_CACHE_REJECTION_NONE_REASONS:
        return True
    if rejection_reason is not None:
        msg = f"Thread cache snapshot is not usable: {rejection_reason}"
        raise AgentMessageSnapshotUnavailable(msg)
    return False


async def _snapshot_from_event(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    event: dict[str, Any],
    cached_at: float | None,
    runtime_started_at: float | None,
) -> _SnapshotLookupResult:
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return _SnapshotLookupResult(snapshot=None)

    latest_edit = await event_cache_events.load_latest_edit_row(
        db,
        room_id=room_id,
        original_event_id=event_id,
    )
    latest_event = latest_edit.event if latest_edit is not None else event
    visible_cached_at = latest_edit.cached_at if latest_edit is not None else cached_at
    if (
        thread_id is None
        and runtime_started_at is not None
        and (visible_cached_at is None or visible_cached_at < runtime_started_at)
    ):
        return _SnapshotLookupResult(snapshot=None, stop_scanning=True)

    timestamp = latest_event.get("origin_server_ts")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        return _SnapshotLookupResult(snapshot=None)

    return _SnapshotLookupResult(
        snapshot=AgentMessageSnapshot(
            content=_visible_content(latest_event),
            origin_server_ts=timestamp,
        ),
    )


async def _iter_scope_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
) -> aiosqlite.Cursor:
    if thread_id is None:
        return await db.execute(
            """
            SELECT event_json, cached_at
            FROM events
            WHERE room_id = ?
            ORDER BY origin_server_ts DESC, rowid DESC
            """,
            (room_id,),
        )
    return await db.execute(
        """
        SELECT event_json, NULL AS cached_at
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        ORDER BY origin_server_ts DESC, rowid DESC
        """,
        (room_id, thread_id),
    )


async def _load_scope_snapshot(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    cursor = await _iter_scope_events(
        db,
        room_id=room_id,
        thread_id=thread_id,
    )
    try:
        while True:
            row = await cursor.fetchone()
            if row is None:
                return None
            event = json.loads(row[0])
            if not _event_matches_scope(
                event,
                thread_id=thread_id,
                sender=sender,
            ):
                continue
            result = await _snapshot_from_event(
                db,
                room_id=room_id,
                thread_id=thread_id,
                event=event,
                cached_at=None if row[1] is None else float(row[1]),
                runtime_started_at=runtime_started_at,
            )
            if result.stop_scanning:
                return None
            if result.snapshot is not None:
                return result.snapshot
    finally:
        await cursor.close()


async def load_agent_message_snapshot(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
    now: float | None = None,
) -> AgentMessageSnapshot | None:
    """Return the latest visible message from ``sender`` in the given scope."""
    try:
        if await _thread_scope_has_no_snapshot(
            db,
            room_id=room_id,
            thread_id=thread_id,
            runtime_started_at=runtime_started_at,
            now=now,
        ):
            return None
        return await _load_scope_snapshot(
            db,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
    except json.JSONDecodeError as exc:
        msg = "Cached Matrix event JSON is corrupt"
        raise AgentMessageSnapshotUnavailable(msg) from exc
    except sqlite3.Error as exc:
        msg = "Failed to read Matrix event cache snapshot"
        raise AgentMessageSnapshotUnavailable(msg) from exc
