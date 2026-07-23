"""Helpers for Matrix room-member join hook emission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4
from weakref import WeakValueDictionary

import nio

from mindroom.constants import safe_replace
from mindroom.entity_resolution import entity_identity_registry, mindroom_user_id
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
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


def _save_room_member_joins(path: Path, seen: dict[str, set[str]]) -> bool:
    """Persist seen room-member joins atomically."""
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    payload = {room_id: sorted(user_ids) for room_id, user_ids in sorted(seen.items())}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            f"{json.dumps(payload, ensure_ascii=True, indent=2)}\n",
            encoding="utf-8",
        )
        safe_replace(temp_path, path)
    except OSError:
        logger.exception("failed_to_save_room_member_joins", path=str(path))
        return False
    else:
        return True
    finally:
        temp_path.unlink(missing_ok=True)


def _mark_room_member_join_seen(storage_root: Path, *, room_id: str, user_id: str) -> bool:
    """Record one room/user pair and return whether it was first seen."""
    path = _room_member_join_tracking_path(storage_root)
    with _lock_for_room_member_join_path(path):
        seen = _load_room_member_joins(path)
        room_user_ids = seen.setdefault(room_id, set())
        if user_id in room_user_ids:
            return False

        room_user_ids.add(user_id)
        return _save_room_member_joins(path, seen)


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


def _record_room_member_join_seen_from_event(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
) -> bool:
    """Record one human room-member join event without emitting hook payload data."""
    user_id = _human_join_user_id(event, config=config, runtime_paths=runtime_paths)
    if user_id is None:
        return False

    return _mark_room_member_join_seen(storage_root, room_id=room.room_id, user_id=user_id)


def room_member_join_from_event(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
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

    if not _mark_room_member_join_seen(storage_root, room_id=room.room_id, user_id=user_id):
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


def room_member_joins_from_sync_state(
    response: nio.SyncResponse,
    *,
    rooms: Mapping[str, nio.MatrixRoom],
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
    record_only: bool = False,
) -> tuple[RoomMemberJoin, ...]:
    """Return hook payloads for human joins delivered through sync room state."""
    joins: list[RoomMemberJoin] = []
    for room, event in _room_member_events_from_sync_state(response, rooms=rooms):
        if record_only:
            _record_room_member_join_seen_from_event(
                room,
                event,
                config=config,
                runtime_paths=runtime_paths,
                storage_root=storage_root,
            )
            continue

        join = room_member_join_from_event(
            room,
            event,
            config=config,
            runtime_paths=runtime_paths,
            storage_root=storage_root,
            require_previous_membership=True,
        )
        if join is not None:
            joins.append(join)
        elif event.prev_membership in {None, "join"}:
            _record_room_member_join_seen_from_event(
                room,
                event,
                config=config,
                runtime_paths=runtime_paths,
                storage_root=storage_root,
            )
    return tuple(joins)


def room_member_joins_from_sync_timeline(
    response: nio.SyncResponse,
    *,
    rooms: Mapping[str, nio.MatrixRoom],
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
) -> tuple[RoomMemberJoin, ...]:
    """Return hook payloads for human joins delivered through sync timeline events."""
    joins: list[RoomMemberJoin] = []
    for room_id, join_info in response.rooms.join.items():
        room = rooms.get(room_id)
        if room is None:
            continue
        for event in join_info.timeline.events:
            if not isinstance(event, nio.RoomMemberEvent):
                continue
            join = room_member_join_from_event(
                room,
                event,
                config=config,
                runtime_paths=runtime_paths,
                storage_root=storage_root,
                # Timeline events are a live event stream, not a full-state snapshot.
                require_previous_membership=False,
            )
            if join is not None:
                joins.append(join)
    return tuple(joins)


def _optional_string(content: dict[str, object], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) else None
