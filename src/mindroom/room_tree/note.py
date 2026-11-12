"""Tell an agent replying in a room tree which rooms sit around its room, with the room IDs to read them by.

A room tree is rooms listed under a room with `m.space.child`, each naming it back as its canonical `m.space.parent`,
which MindRoom Chat shows as sub-rooms nested under it. The note rides with the Matrix target item as ``persist=False``
turn context, so the system prompt never changes. It names only rooms the agent is in and the requester may read with
the Matrix tools: the agent is in private rooms of the tree whose names the requester must not learn.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from aiohttp import ClientError

from mindroom.constants import MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY
from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.hooks import EnrichmentItem
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import names_canonical_parent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence

    import nio

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)
_ROOM_TREE_ENRICHMENT_KEY = "room_tree"
# The rooms beside and under the agent's room one note checks and names at most; it counts the rest.
_MAX_LISTED = 15
_HEADER = (
    "This room is in a tree of rooms, each listed under the room above it. "
    "To read one, pass its room ID as room_id to matrix_message or matrix_room."
)
_UNREADABLE_PARENT = "a room the requester is not in"
_HERE = ", the room you are responding in"


async def with_room_tree(items: Sequence[EnrichmentItem], context: ToolRuntimeContext) -> tuple[EnrichmentItem, ...]:
    """Add the tree to a turn told where it responds, which the runtime does only when the turn offers matrix_message.

    The note is optional turn context, so a homeserver that cannot answer leaves the turn without it.
    """
    items = tuple(items)
    if not any(item.key == MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY for item in items):
        return items
    try:
        note = await _room_tree_note(context.client, context.room_id, can_read=partial(room_access_allowed, context))
    except (ClientError, TimeoutError) as error:
        logger.warning("Room tree note unavailable", room_id=context.room_id, error_type=type(error).__name__)
        return items
    if note is None:
        return items
    item = EnrichmentItem(
        key=_ROOM_TREE_ENRICHMENT_KEY,
        text=note,
        cache_policy="stable",
        persist=False,
        minimal_required=True,
    )
    return (*items, item)


async def _room_tree_note(
    client: nio.AsyncClient,
    room_id: str,
    *,
    can_read: Callable[[str], Awaitable[bool]],
) -> str | None:
    """Return the tree around ``room_id``: the room above it, the rooms beside it, and the rooms under it.

    Rooms ``can_read`` refuses are counted, not named, as are rooms past the cap and listed rooms the agent is not in.
    Returns None when the room is in no tree or the requester can read no other room of it.
    """
    rooms = client.rooms
    room = _chat_room(rooms, room_id)
    if room is None:
        return None
    parent_id = await _canonical_parent(client, room_id)
    sibling_ids = rooms[parent_id].children - {room_id, parent_id} if parent_id is not None else set()
    child_ids = room.children - {room_id, parent_id} - sibling_ids
    if parent_id is None and not child_ids:
        return None
    siblings, children = _joined(rooms, sibling_ids), _joined(rooms, child_ids)
    checked = [listed.room_id for listed in [*siblings, *children][:_MAX_LISTED]]
    readable = await _readable_ids(can_read, [room_id, *([parent_id] if parent_id is not None else []), *checked])
    if room_id not in readable or readable == {room_id}:
        return None
    named_siblings = [sibling for sibling in siblings if sibling.room_id in readable]
    named_children = [child for child in children if child.room_id in readable]
    lines = [_HEADER]
    depth = 0
    if parent_id is not None:
        lines.append(_line(rooms[parent_id], 0) if parent_id in readable else f"- {_UNREADABLE_PARENT}")
        depth = 1
    for peer in sorted([*named_siblings, room], key=_sort_key):
        lines.append(_line(peer, depth) + (_HERE if peer is room else ""))
        if peer is room:
            lines.extend(_line(child, depth + 1) for child in named_children)
    # Spaces the agent is in are no sub-rooms, so only the other listed rooms count as left out.
    listed_ids = sibling_ids | child_ids
    spaces = {listed_id for listed_id in listed_ids if listed_id in rooms and _chat_room(rooms, listed_id) is None}
    hidden = len(listed_ids - spaces) - len(named_siblings) - len(named_children)
    if hidden:
        lines.append(_hidden_line(hidden))
    return "\n".join(lines)


async def _canonical_parent(client: nio.AsyncClient, room_id: str) -> str | None:
    """Return the room the agent is in that lists ``room_id`` and that ``room_id`` names as its canonical parent."""
    listers = sorted(
        lister_id
        for lister_id, lister in client.rooms.items()
        if lister_id != room_id and lister.room_type != "m.space" and room_id in lister.children
    )
    for lister_id in listers:
        if await names_canonical_parent(client, room_id, lister_id):
            return lister_id
    return None


def _chat_room(rooms: Mapping[str, nio.MatrixRoom], room_id: str) -> nio.MatrixRoom | None:
    """Return a room the agent is in that is no Space, since only chat rooms sit in a room tree."""
    room = rooms.get(room_id)
    return room if room is not None and room.room_type != "m.space" else None


def _joined(rooms: Mapping[str, nio.MatrixRoom], room_ids: Collection[str]) -> list[nio.MatrixRoom]:
    """Return the chat rooms among ``room_ids`` the agent is in, in the order the note lists them."""
    return sorted((room for room_id in room_ids if (room := _chat_room(rooms, room_id)) is not None), key=_sort_key)


async def _readable_ids(can_read: Callable[[str], Awaitable[bool]], room_ids: Sequence[str]) -> set[str]:
    readable = await asyncio.gather(*(can_read(room_id) for room_id in room_ids))
    return {room_id for room_id, allowed in zip(room_ids, readable, strict=True) if allowed}


def _hidden_line(count: int) -> str:
    if count == 1:
        return "1 more room in this tree is not listed."
    return f"{count} more rooms in this tree are not listed."


def _sort_key(room: nio.MatrixRoom) -> tuple[str, str]:
    return (room.display_name.casefold(), room.room_id)


def _line(room: nio.MatrixRoom, depth: int) -> str:
    return f"{'  ' * depth}- {room.display_name!r} (room ID {room.room_id})"
