"""Public snapshot reads for the latest visible agent message in one scope."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .thread_cache_helpers import thread_cache_rejection_reason

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class AgentMessageSnapshot:
    """Latest visible message content and timestamp for one sender."""

    content: dict[str, Any]
    origin_server_ts: int


class CacheUnavailable(RuntimeError):  # noqa: N818
    """Raised when an existing Matrix event cache cannot be safely read."""


@dataclass(frozen=True, slots=True)
class _ThreadCacheStateSnapshot:
    """Freshness metadata read directly from the SQLite cache."""

    validated_at: float | None
    invalidated_at: float | None
    invalidation_reason: str | None
    room_invalidated_at: float | None
    room_invalidation_reason: str | None


@dataclass(frozen=True, slots=True)
class _CachedEventRow:
    """One cached event payload plus the write timestamp for its visible version."""

    event: dict[str, Any]
    cached_at: float | None


@dataclass(frozen=True, slots=True)
class _SnapshotLookupResult:
    """Outcome for one matching scope event during latest-message lookup."""

    snapshot: AgentMessageSnapshot | None
    stop_scanning: bool = False


def _relation_type(event: dict[str, Any]) -> str | None:
    content = _content_dict(event.get("content"))
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, dict):
        return None
    relation_type = relates_to.get("rel_type")
    return relation_type if isinstance(relation_type, str) else None


def _content_dict(content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    return {key: value for key, value in content.items() if isinstance(key, str)}


def _visible_content(event: dict[str, Any]) -> dict[str, Any]:
    content = _content_dict(event.get("content"))
    new_content = _content_dict(content.get("m.new_content"))
    return new_content or content


def _load_latest_edit(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    original_event_id: str,
) -> _CachedEventRow | None:
    row = conn.execute(
        """
        SELECT events.event_json, events.cached_at
        FROM event_edits
        JOIN events ON events.event_id = event_edits.edit_event_id
        WHERE event_edits.room_id = ? AND event_edits.original_event_id = ?
        ORDER BY event_edits.origin_server_ts DESC, event_edits.edit_event_id DESC
        LIMIT 1
        """,
        (room_id, original_event_id),
    ).fetchone()
    if row is None:
        return None
    return _CachedEventRow(
        event=json.loads(row[0]),
        cached_at=None if row[1] is None else float(row[1]),
    )


def _load_thread_cache_state(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> _ThreadCacheStateSnapshot | None:
    row = conn.execute(
        """
        SELECT
            thread_cache_state.validated_at,
            thread_cache_state.invalidated_at,
            thread_cache_state.invalidation_reason,
            room_cache_state.invalidated_at,
            room_cache_state.invalidation_reason
        FROM (SELECT ? AS requested_room_id, ? AS requested_thread_id) AS requested
        LEFT JOIN thread_cache_state
            ON thread_cache_state.room_id = requested.requested_room_id
            AND thread_cache_state.thread_id = requested.requested_thread_id
        LEFT JOIN room_cache_state
            ON room_cache_state.room_id = requested.requested_room_id
        """,
        (room_id, thread_id),
    ).fetchone()
    if row is None or all(value is None for value in row):
        return None
    return _ThreadCacheStateSnapshot(
        validated_at=None if row[0] is None else float(row[0]),
        invalidated_at=None if row[1] is None else float(row[1]),
        invalidation_reason=row[2] if isinstance(row[2], str) else None,
        room_invalidated_at=None if row[3] is None else float(row[3]),
        room_invalidation_reason=row[4] if isinstance(row[4], str) else None,
    )


_THREAD_CACHE_REJECTION_NONE_REASONS = frozenset({"no_cache_state", "cache_never_validated"})


def _thread_scope_rejection_reason(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    thread_id: str,
    runtime_started_at: float | None,
    now: float | None,
) -> str | None:
    return thread_cache_rejection_reason(
        _load_thread_cache_state(
            conn,
            room_id=room_id,
            thread_id=thread_id,
        ),
        runtime_started_at=runtime_started_at,
        now=now,
    )


def _iter_scope_events(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    thread_id: str | None,
) -> sqlite3.Cursor:
    if thread_id is None:
        return conn.execute(
            """
            SELECT event_json, cached_at
            FROM events
            WHERE room_id = ?
            ORDER BY origin_server_ts DESC, event_id DESC
            """,
            (room_id,),
        )
    return conn.execute(
        """
        SELECT event_json, NULL AS cached_at
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        ORDER BY origin_server_ts DESC, rowid DESC
        """,
        (room_id, thread_id),
    )


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


def _snapshot_from_event(
    conn: sqlite3.Connection,
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
    latest_edit = _load_latest_edit(
        conn,
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


def _open_readonly_cache(db_path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        msg = f"Failed to open Matrix event cache at {db_path}"
        raise CacheUnavailable(msg) from exc


def _thread_scope_has_no_snapshot(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    runtime_started_at: float | None,
    now: float | None,
) -> bool:
    if thread_id is None:
        return False

    rejection_reason = _thread_scope_rejection_reason(
        conn,
        room_id=room_id,
        thread_id=thread_id,
        runtime_started_at=runtime_started_at,
        now=now,
    )
    if rejection_reason in _THREAD_CACHE_REJECTION_NONE_REASONS:
        return True
    if rejection_reason is not None:
        msg = f"Thread cache snapshot is not usable: {rejection_reason}"
        raise CacheUnavailable(msg)
    return False


def _load_scope_snapshot(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    rows = _iter_scope_events(
        conn,
        room_id=room_id,
        thread_id=thread_id,
    )
    for event_json, cached_at in rows:
        event = json.loads(event_json)
        if not _event_matches_scope(
            event,
            thread_id=thread_id,
            sender=sender,
        ):
            continue
        result = _snapshot_from_event(
            conn,
            room_id=room_id,
            thread_id=thread_id,
            event=event,
            cached_at=None if cached_at is None else float(cached_at),
            runtime_started_at=runtime_started_at,
        )
        if result.stop_scanning:
            return None
        if result.snapshot is not None:
            return result.snapshot
    return None


def get_latest_agent_message_snapshot(
    *,
    db_path: Path,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
    now: float | None = None,
) -> AgentMessageSnapshot | None:
    """Return the latest visible message from ``sender`` in the given scope.

    Used by the workloop plugin to gate auto-poke on stream status. Returns ``None``
    when the cache file is missing or the sender has no cached message in the
    requested scope. Raises ``CacheUnavailable`` when an existing cache file cannot
    be safely read or a cached thread snapshot is present but rejected by the cache
    freshness contract.
    """
    if not db_path.exists():
        return None

    conn = _open_readonly_cache(db_path)

    try:
        if _thread_scope_has_no_snapshot(
            conn,
            room_id=room_id,
            thread_id=thread_id,
            runtime_started_at=runtime_started_at,
            now=now,
        ):
            return None
        return _load_scope_snapshot(
            conn,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
    except json.JSONDecodeError as exc:
        msg = f"Cached Matrix event JSON is corrupt in {db_path}"
        raise CacheUnavailable(msg) from exc
    except sqlite3.Error as exc:
        msg = f"Failed to read Matrix event cache at {db_path}"
        raise CacheUnavailable(msg) from exc
    finally:
        conn.close()
