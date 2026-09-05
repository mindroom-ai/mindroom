"""Helpers for Matrix room-member join hook emission."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from pathlib import Path

    import nio

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
class RoomMemberLeave:
    """One live human self-leave that should be exposed to hooks."""

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

    return _human_room_member_user_id(event, config=config, runtime_paths=runtime_paths)


def _human_room_member_user_id(
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return the affected human user ID for one membership event, or None."""
    user_id = event.state_key
    if not is_human_requester_id(user_id, config, runtime_paths):
        return None
    return user_id


def room_member_left_from_event(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> RoomMemberLeave | None:
    """Return hook payload data for one live human self-leave, or None."""
    if event.membership != "leave" or event.prev_membership != "join":
        return None
    if event.sender != event.state_key:
        return None
    prev_content = event.prev_content
    if prev_content is None:
        return None

    user_id = _human_room_member_user_id(event, config=config, runtime_paths=runtime_paths)
    if user_id is None:
        return None

    return RoomMemberLeave(
        room_id=room.room_id,
        event_id=event.event_id,
        user_id=user_id,
        sender_id=event.sender,
        display_name=_optional_string(prev_content, "displayname"),
        avatar_url=_optional_string(prev_content, "avatar_url"),
        membership=event.membership,
        prev_membership=event.prev_membership,
    )


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


def _optional_string(content: dict[str, object], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) else None
