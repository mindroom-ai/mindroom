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
# Access checks run one at a time under the client's crypto lock, so a slow homeserver drops the note, not the reply.
_TIMEOUT_SECONDS = 5.0
_HEADER = "Nested rooms around this one. To read one, pass its room ID as room_id to your Matrix tools."
_NO_ACCESS = "the person you are answering has no access to"
_HERE = ", you are here"


async def with_room_tree(items: Sequence[EnrichmentItem], context: ToolRuntimeContext) -> tuple[EnrichmentItem, ...]:
    """Add the tree to a turn told where it responds, which the runtime does only when the turn offers matrix_message.

    The note is optional turn context, so a homeserver that cannot answer leaves the turn without it.
    """
    items = tuple(items)
    if not any(item.key == MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY for item in items):
        return items
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            note = await _room_tree_note(
                context.client,
                context.room_id,
                can_read=partial(room_access_allowed, context),
            )
    except Exception as error:  # The note never costs the reply; a cancelled turn still stops.
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

    A listed room the agent is in counts only when it names its lister as canonical parent, as in MindRoom Chat.
    Rooms ``can_read`` refuses are counted, not named, as are rooms past the cap and listed rooms the agent is not in.
    Returns None when the room is in no tree or the requester can read no other room of it.
    """
    rooms = client.rooms
    room = _chat_room(rooms, room_id)
    if room is None:
        return None
    # Rooms are held as objects from here on: a sync drops a room the agent leaves from ``client.rooms`` mid-read.
    parent = await _canonical_parent(client, room_id)
    parent_id = parent.room_id if parent is not None else None
    sibling_ids = parent.children - {room_id, parent_id} if parent is not None else set()
    child_ids = room.children - {room_id, parent_id} - sibling_ids
    if parent is None and not child_ids:
        return None
    # The room's own sub-rooms take the cap first, so a crowded room above never pushes them out.
    candidates = [(child, room) for child in _joined(rooms, child_ids)]
    candidates += [(sibling, parent) for sibling in _joined(rooms, sibling_ids) if parent is not None]
    checked = candidates[:_MAX_LISTED]
    # Spaces the agent is in are no sub-rooms, so only the other listed rooms count as left out.
    listed_ids = sibling_ids | child_ids
    spaces = {listed_id for listed_id in listed_ids if listed_id in rooms and _chat_room(rooms, listed_id) is None}
    unchecked = len(listed_ids - spaces) - len(checked)
    room_readable, parent_readable, *places = await asyncio.gather(
        can_read(room_id),
        can_read(parent_id) if parent_id is not None else _false(),
        *(_place(client, listed, lister, can_read) for listed, lister in checked),
    )
    named = {listed.room_id for (listed, _), place in zip(checked, places, strict=True) if place}
    if not room_readable or not (parent_readable or named):
        return None
    lines = [_HEADER]
    depth = 0
    if parent is not None:
        label = "main room" if await _canonical_parent(client, parent.room_id) is None else "room above"
        lines.append(
            f"{_line(parent, 0)}, {label}" if parent_readable else f"- {label}, hidden because {_NO_ACCESS} it",
        )
        depth = 1
    named_siblings = [listed for listed, lister in checked if lister is parent and listed.room_id in named]
    for peer in sorted([*named_siblings, room], key=_sort_key):
        lines.append(_line(peer, depth) + (_HERE if peer is room else ""))
        if peer is room:
            children = [listed for listed, lister in checked if lister is room and listed.room_id in named]
            lines.extend(_line(child, depth + 1) for child in sorted(children, key=_sort_key))
    refused = sum(place is False for place in places)
    if refused:
        lines.append(_left_out_line(refused, f"hidden because {_NO_ACCESS} {'it' if refused == 1 else 'them'}"))
    if unchecked:
        lines.append(_left_out_line(unchecked, "not shown"))
    return "\n".join(lines)


async def _canonical_parent(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return the room the agent is in that lists ``room_id`` and that ``room_id`` names as its canonical parent."""
    listers = sorted(
        (lister for lister_id, lister in client.rooms.items() if lister_id != room_id and room_id in lister.children),
        key=lambda lister: lister.room_id,
    )
    for lister in listers:
        if lister.room_type != "m.space" and await names_canonical_parent(client, room_id, lister.room_id):
            return lister
    return None


async def _place(
    client: nio.AsyncClient,
    listed: nio.MatrixRoom,
    lister: nio.MatrixRoom,
    can_read: Callable[[str], Awaitable[bool]],
) -> bool | None:
    """Return whether the requester may read ``listed``, or None when it names another room as its canonical parent."""
    if not await names_canonical_parent(client, listed.room_id, lister.room_id):
        return None
    return await can_read(listed.room_id)


async def _false() -> bool:
    return False


def _chat_room(rooms: Mapping[str, nio.MatrixRoom], room_id: str) -> nio.MatrixRoom | None:
    """Return a room the agent is in that is no Space, since only chat rooms sit in a room tree."""
    room = rooms.get(room_id)
    return room if room is not None and room.room_type != "m.space" else None


def _joined(rooms: Mapping[str, nio.MatrixRoom], room_ids: Collection[str]) -> list[nio.MatrixRoom]:
    """Return the chat rooms among ``room_ids`` the agent is in, in the order the note lists them."""
    return sorted((room for room_id in room_ids if (room := _chat_room(rooms, room_id)) is not None), key=_sort_key)


def _left_out_line(count: int, reason: str) -> str:
    if count == 1:
        return f"1 more nested room is {reason}."
    return f"{count} more nested rooms are {reason}."


def _sort_key(room: nio.MatrixRoom) -> tuple[str, str]:
    return (room.display_name.casefold(), room.room_id)


def _line(room: nio.MatrixRoom, depth: int) -> str:
    """Return the room's line; ``!r`` keeps a name holding a line break from adding a line of its own."""
    return f"{'  ' * depth}- {room.display_name!r} ({room.room_id})"
