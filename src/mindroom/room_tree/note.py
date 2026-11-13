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
    from collections.abc import Callable, Collection, Coroutine, Mapping, Sequence

    import nio

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)
_ROOM_TREE_ENRICHMENT_KEY = "room_tree"
# The rooms beside and under the agent's room one note checks and names at most; it counts the rest.
_MAX_LISTED = 15
# Access checks run one at a time under the client's crypto lock, so a slow homeserver drops the note, not the reply.
_TIMEOUT_SECONDS = 5.0
_HEADER = (
    "Sub-rooms around this one, each under the room it is in. "
    "To read one, pass its room ID as room_id to your Matrix tools."
)
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
    can_read: Callable[[str], Coroutine[object, object, bool]],
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
    child_ids = room.children - {room_id, parent_id}
    if parent is None and not child_ids:
        return None
    # The room's own sub-rooms take the cap first, so a crowded room above never pushes them out. A room both list is
    # tried under this room first, then under the room above, and sits under the one it names as canonical parent.
    checked = [*_joined(rooms, child_ids), *_joined(rooms, sibling_ids - child_ids)][:_MAX_LISTED]
    listers = [lister for lister in (room, parent) if lister is not None]
    # Spaces the agent is in are no sub-rooms, so only the other listed rooms count as left out.
    listed_ids = sibling_ids | child_ids
    spaces = {listed_id for listed_id in listed_ids if listed_id in rooms and _chat_room(rooms, listed_id) is None}
    unchecked = len(listed_ids - spaces) - len(checked)
    # A task group stops the other reads when one fails, so none outlives the note.
    async with asyncio.TaskGroup() as group:
        room_read = group.create_task(can_read(room_id))
        parent_read = group.create_task(can_read(parent_id) if parent_id is not None else _false())
        place_reads = [group.create_task(_place(client, listed, listers, can_read)) for listed in checked]
    room_readable, parent_readable = room_read.result(), parent_read.result()
    places = [(listed, read.result()) for listed, read in zip(checked, place_reads, strict=True)]
    placed = [(listed, place) for listed, place in places if place is not None]
    named = {listed.room_id: lister for listed, (lister, readable) in placed if readable}
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
    named_siblings = [listed for listed in checked if named.get(listed.room_id) is parent]
    for peer in sorted([*named_siblings, room], key=_sort_key):
        lines.append(_line(peer, depth) + (_HERE if peer is room else ""))
        if peer is room:
            children = [listed for listed in checked if named.get(listed.room_id) is room]
            lines.extend(_line(child, depth + 1) for child in sorted(children, key=_sort_key))
    refused = len(placed) - len(named)
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
    listers: Sequence[nio.MatrixRoom],
    can_read: Callable[[str], Coroutine[object, object, bool]],
) -> tuple[nio.MatrixRoom, bool] | None:
    """Return where ``listed`` sits and whether the requester may read it, or None when it sits under none.

    It sits under the first of ``listers`` that lists it and that it names as its canonical parent.
    """
    for lister in listers:
        if listed.room_id in lister.children and await names_canonical_parent(client, listed.room_id, lister.room_id):
            return lister, await can_read(listed.room_id)
    return None


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
        return f"1 more sub-room is {reason}."
    return f"{count} more sub-rooms are {reason}."


def _sort_key(room: nio.MatrixRoom) -> tuple[str, str]:
    return (room.display_name.casefold(), room.room_id)


def _line(room: nio.MatrixRoom, depth: int) -> str:
    """Return the room's line; ``!r`` keeps a name holding a line break from adding a line of its own."""
    return f"{'  ' * depth}- {room.display_name!r} ({room.room_id})"
