"""Helpers for Matrix room-member join hook emission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import DispatchView


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
) -> RoomMemberJoin | None:
    """Return hook payload data for one live human join event, or None when ignored."""
    if event.membership != "join" or event.prev_membership == "join":
        return None
    user_id = _human_room_member_user_id(event, config=config, runtime_paths=runtime_paths)
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


async def emit_room_member_join_at_least_once(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    store: DispatchView,
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
        )
        if join is None:
            return False
        if await store.is_room_member_join_suppressed(join.room_id, join.event_id, join.user_id):
            return False

        await emit(join)
        await store.mark_room_member_join_completed(join.room_id, join.user_id)
        return True


def _optional_string(content: dict[str, object], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) else None
