"""Helpers for Matrix room-member join hook emission."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.durable_write import write_json_file_durable
from mindroom.entity_resolution import entity_identity_registry, mindroom_user_id
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_ROOM_MEMBER_JOIN_LOCKS: WeakValueDictionary[Path, Lock] = WeakValueDictionary()
_ROOM_MEMBER_JOIN_LOCKS_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class RoomMemberJoin:
    """One live human member join that should be exposed to hooks."""

    room_id: str
    event_id: str
    user_id: str
    sender_id: str
    display_name: str | None
    avatar_url: str | None
    membership: str
    prev_membership: str | None


@dataclass(frozen=True, slots=True)
class _RoomMemberSyncPlan:
    """Classified room-member state work for one sync response."""

    dispatch_events: tuple[tuple[nio.MatrixRoom, nio.RoomMemberEvent], ...] = ()
    record_events: tuple[tuple[nio.MatrixRoom, nio.RoomMemberEvent], ...] = ()


def _room_member_join_tracking_path(storage_root: Path) -> Path:
    """Return the durable path for room-member join de-duplication."""
    return storage_root / "tracking" / "room_member_joins.json"


def _lock_for_room_member_join_path(path: Path) -> Lock:
    """Return the in-process lock guarding one tracking file."""
    resolved_path = path.resolve()
    with _ROOM_MEMBER_JOIN_LOCKS_LOCK:
        lock = _ROOM_MEMBER_JOIN_LOCKS.get(resolved_path)
        if lock is None:
            lock = Lock()
            _ROOM_MEMBER_JOIN_LOCKS[resolved_path] = lock
        return lock


def _load_room_member_joins(path: Path) -> dict[str, set[str]]:
    """Load seen room-member joins, failing open on missing or invalid files."""
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_room_member_joins", path=str(path), exc_info=True)
        return {}

    if not isinstance(raw, dict):
        logger.warning("invalid_room_member_joins_file", path=str(path))
        return {}

    seen: dict[str, set[str]] = {}
    for room_id, user_ids in raw.items():
        if not isinstance(room_id, str) or not isinstance(user_ids, list):
            logger.warning("invalid_room_member_joins_file", path=str(path))
            return {}
        room_user_ids: set[str] = set()
        for user_id in user_ids:
            if not isinstance(user_id, str):
                logger.warning("invalid_room_member_joins_file", path=str(path))
                return {}
            room_user_ids.add(user_id)
        seen[room_id] = room_user_ids
    return seen


def _save_room_member_joins(path: Path, seen: dict[str, set[str]]) -> None:
    """Persist seen room-member joins through the shared durable writer."""
    payload = {room_id: sorted(user_ids) for room_id, user_ids in sorted(seen.items())}
    try:
        write_json_file_durable(
            path,
            payload,
            indent=2,
            trailing_newline=True,
        )
    except OSError as exc:
        msg = f"Failed to persist completed room-member join tracking at {path}"
        raise RuntimeError(msg) from exc


def _mark_room_member_joins_seen(
    storage_root: Path,
    room_user_ids: Iterable[tuple[str, str]],
) -> None:
    """Record room/user pairs with one locked read and at most one durable write."""
    path = _room_member_join_tracking_path(storage_root)
    with _lock_for_room_member_join_path(path):
        seen = _load_room_member_joins(path)
        added = 0
        for room_id, user_id in room_user_ids:
            seen_in_room = seen.setdefault(room_id, set())
            if user_id in seen_in_room:
                continue
            seen_in_room.add(user_id)
            added += 1
        if added:
            _save_room_member_joins(path, seen)


def _human_join_user_id(
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return the joined human user ID for one membership event, or None."""
    if event.membership != "join":
        return None

    user_id = event.state_key
    if (
        entity_identity_registry(config, runtime_paths).is_managed_user_id(user_id)
        or user_id in config.bot_accounts
        or user_id == mindroom_user_id(config, runtime_paths)
    ):
        return None
    return user_id


def record_room_member_joins_seen_from_events(
    events: Iterable[tuple[nio.MatrixRoom, nio.RoomMemberEvent]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
) -> None:
    """Record human room-member events with one durable batch update."""
    room_user_ids: list[tuple[str, str]] = []
    for room, event in events:
        user_id = _human_join_user_id(event, config=config, runtime_paths=runtime_paths)
        if user_id is not None:
            room_user_ids.append((room.room_id, user_id))
    _mark_room_member_joins_seen(storage_root, room_user_ids)


def _room_member_join_from_event(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    require_previous_membership: bool = True,
) -> RoomMemberJoin | None:
    """Return hook payload data for one live human join event, or None when ignored."""
    if event.membership != "join" or event.prev_membership == "join":
        return None
    if require_previous_membership and event.prev_membership is None:
        return None

    user_id = _human_join_user_id(event, config=config, runtime_paths=runtime_paths)
    if user_id is None:
        return None

    return RoomMemberJoin(
        room_id=room.room_id,
        event_id=event.event_id,
        user_id=user_id,
        sender_id=event.sender,
        display_name=_optional_string(event.content, "displayname"),
        avatar_url=_optional_string(event.content, "avatar_url"),
        membership=event.membership,
        prev_membership=event.prev_membership,
    )


def _room_member_join_is_seen(
    storage_root: Path,
    *,
    room_id: str,
    user_id: str,
) -> bool:
    """Return whether one room/user join was durably completed."""
    path = _room_member_join_tracking_path(storage_root)
    with _lock_for_room_member_join_path(path):
        return user_id in _load_room_member_joins(path).get(room_id, set())


def _record_room_member_join_seen(
    storage_root: Path,
    join: RoomMemberJoin,
) -> None:
    """Record one room/user join after its hook emission completes."""
    _mark_room_member_joins_seen(storage_root, ((join.room_id, join.user_id),))


async def emit_room_member_join_at_least_once(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
    lock: asyncio.Lock,
    emit: Callable[[RoomMemberJoin], Awaitable[None]],
) -> bool:
    """Emit an unseen live join, accepting replay until its marker persists."""
    async with lock:
        join = _room_member_join_from_event(
            room,
            event,
            config=config,
            runtime_paths=runtime_paths,
            # Live callbacks are admitted only after startup; prev_content may be absent.
            require_previous_membership=False,
        )
        if join is None:
            return False
        if await asyncio.to_thread(
            _room_member_join_is_seen,
            storage_root,
            room_id=join.room_id,
            user_id=join.user_id,
        ):
            return False

        await emit(join)
        await asyncio.to_thread(_record_room_member_join_seen, storage_root, join)
        return True


def _room_member_events_from_sync_state(
    response: nio.SyncResponse,
    *,
    rooms: Mapping[str, nio.MatrixRoom],
) -> Iterator[tuple[nio.MatrixRoom, nio.RoomMemberEvent]]:
    """Yield room-member events from sync state with their resolved room."""
    for room_id, join_info in response.rooms.join.items():
        room = rooms.get(room_id)
        if room is None:
            continue
        for event in join_info.state:
            if isinstance(event, nio.RoomMemberEvent):
                yield room, event


def _room_member_events_from_sync_timeline(
    response: nio.SyncResponse,
    *,
    rooms: Mapping[str, nio.MatrixRoom],
) -> Iterator[tuple[nio.MatrixRoom, nio.RoomMemberEvent]]:
    """Yield room-member events from sync timelines with their resolved room."""
    for room_id, join_info in response.rooms.join.items():
        room = rooms.get(room_id)
        if room is None:
            continue
        for event in join_info.timeline.events:
            if isinstance(event, nio.RoomMemberEvent):
                yield room, event


def room_member_sync_state_plan(
    response: nio.SyncResponse,
    *,
    rooms: Mapping[str, nio.MatrixRoom],
    config: Config,
    runtime_paths: RuntimePaths,
    record_only: bool = False,
) -> _RoomMemberSyncPlan:
    """Classify state events into durable hook dispatches and baseline markers."""
    dispatch_events: list[tuple[nio.MatrixRoom, nio.RoomMemberEvent]] = []
    record_events: list[tuple[nio.MatrixRoom, nio.RoomMemberEvent]] = []
    for room, event in _room_member_events_from_sync_state(response, rooms=rooms):
        if record_only:
            record_events.append((room, event))
            continue
        if (
            _room_member_join_from_event(
                room,
                event,
                config=config,
                runtime_paths=runtime_paths,
                require_previous_membership=True,
            )
            is not None
        ):
            dispatch_events.append((room, event))
        elif event.prev_membership in {None, "join"}:
            record_events.append((room, event))
    return _RoomMemberSyncPlan(
        dispatch_events=tuple(dispatch_events),
        record_events=tuple(record_events),
    )


def room_member_sync_timeline_events(
    response: nio.SyncResponse,
    *,
    rooms: Mapping[str, nio.MatrixRoom],
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[tuple[nio.MatrixRoom, nio.RoomMemberEvent], ...]:
    """Return eligible live joins carried by a restored-token timeline."""
    return tuple(
        (room, event)
        for room, event in _room_member_events_from_sync_timeline(response, rooms=rooms)
        if _room_member_join_from_event(
            room,
            event,
            config=config,
            runtime_paths=runtime_paths,
            require_previous_membership=False,
        )
        is not None
    )


def _optional_string(content: dict[str, object], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) else None
