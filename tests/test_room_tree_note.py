"""The note telling an agent in a room tree which rooms sit around its room, with the room IDs to read them by."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from aiohttp import ClientError

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY
from mindroom.hooks import EnrichmentItem
from mindroom.message_target import MessageTarget
from mindroom.room_tree import note as room_tree_note
from mindroom.room_tree.note import _room_tree_note, with_room_tree
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable
    from pathlib import Path

AGENT = "@mindroom_general:localhost"
ALICE = "@alice:localhost"
DESIGN = "!design:localhost"
ROOM = "!room:localhost"
PLANNING = "!planning:localhost"
NOTES = "!notes:localhost"
PRIVATE = "!private:localhost"
SPACE = "!space:localhost"
HEADER = "Nested rooms around this one. To read one, pass its room ID as room_id to your Matrix tools."
DESIGN_LINE = "- 'design' (!design:localhost), main room"
PLANNING_LINE = "  - 'planning' (!planning:localhost)"
HERE_LINE = "  - 'release-prep' (!room:localhost), you are here"
NOTES_LINE = "    - 'notes' (!notes:localhost)"
HIDDEN_ONE = "1 more nested room is hidden because the person you are answering has no access to it."
TARGET = EnrichmentItem(key=MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY, text="You are responding in Matrix room !room.")


def _note(*lines: str) -> str:
    return "\n".join((HEADER, *lines))


def _room(room_id: str, name: str, *, children: Iterable[str] = (), room_type: str | None = None) -> nio.MatrixRoom:
    """Return a room the agent is in, as a sync leaves it."""
    room = nio.MatrixRoom(room_id, AGENT)
    room.name = name
    room.children.update(children)
    room.room_type = room_type
    return room


def _client(*rooms: nio.MatrixRoom, canonical: Iterable[tuple[str, str]] = ()) -> MagicMock:
    """Return a client in ``rooms``, where each ``(room, parent)`` of ``canonical`` names the parent canonical."""
    canonical_pairs = set(canonical)
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = AGENT
    client.rooms = {room.room_id: room for room in rooms}

    async def state_event(
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        if event_type == "m.space.parent" and (room_id, state_key) in canonical_pairs:
            content = {"via": ["localhost"], "canonical": True}
            return nio.RoomGetStateEventResponse(content, event_type, state_key, room_id)
        return nio.RoomGetStateEventError("not found", "M_NOT_FOUND")

    client.room_get_state_event = AsyncMock(side_effect=state_event)
    return client


def _readable(*room_ids: str) -> Callable[[str], Coroutine[object, object, bool]]:
    async def can_read(room_id: str) -> bool:
        return room_id in room_ids

    return can_read


def _design_tree(
    *extra: nio.MatrixRoom,
    design_children: Iterable[str] = (),
    canonical: Iterable[tuple[str, str]] = (),
) -> MagicMock:
    """Return a client in `design`, which lists `release-prep` (this room) and `planning`; `notes` is under this room.

    Every room `design` and this room list names its lister back as canonical parent, as do the pairs of `canonical`.
    """
    return _client(
        _room(DESIGN, "design", children={ROOM, PLANNING, *design_children}),
        _room(ROOM, "release-prep", children={NOTES}),
        _room(PLANNING, "planning"),
        _room(NOTES, "notes"),
        *extra,
        canonical={
            (ROOM, DESIGN),
            (PLANNING, DESIGN),
            (NOTES, ROOM),
            *((child, DESIGN) for child in design_children),
            *canonical,
        },
    )


@pytest.mark.asyncio
async def test_a_room_outside_any_tree_gets_no_note() -> None:
    """A room nothing lists and that lists nothing reads no state at all."""
    client = _client(_room(ROOM, "release-prep"), _room(PLANNING, "planning"))

    assert await _room_tree_note(client, ROOM, can_read=_readable(ROOM, PLANNING)) is None
    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sub_room_names_the_room_above_the_rooms_beside_it_and_its_own_sub_rooms() -> None:
    """The room above comes first, then its rooms in name order, with this room's own sub-rooms under it."""
    client = _design_tree()

    note = await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM, PLANNING, NOTES))

    assert note == _note(DESIGN_LINE, PLANNING_LINE, HERE_LINE, NOTES_LINE)


@pytest.mark.asyncio
async def test_a_room_with_sub_rooms_names_them_under_itself() -> None:
    """A top room is the root of its tree, and the rooms under its sub-rooms are left out."""
    client = _design_tree()

    note = await _room_tree_note(client, DESIGN, can_read=_readable(DESIGN, ROOM, PLANNING, NOTES))

    assert note == _note(
        "- 'design' (!design:localhost), you are here",
        PLANNING_LINE,
        "  - 'release-prep' (!room:localhost)",
    )


@pytest.mark.asyncio
async def test_rooms_the_requester_cannot_read_or_the_agent_is_not_in_are_only_counted() -> None:
    """The agent knows the name of a private room the requester is not in, and the note keeps it to a count."""
    client = _design_tree(
        _room(PRIVATE, "acquisition plans"),
        _room("!board:localhost", "board"),
        design_children={PRIVATE, "!board:localhost", "!elsewhere:localhost"},
    )

    note = await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM, PLANNING, NOTES))

    assert note == _note(
        DESIGN_LINE,
        PLANNING_LINE,
        HERE_LINE,
        NOTES_LINE,
        "2 more nested rooms are hidden because the person you are answering has no access to them.",
        "1 more nested room is not shown.",
    )


@pytest.mark.asyncio
async def test_a_room_above_the_requester_cannot_read_is_not_named() -> None:
    """The rooms beside this one still show when the requester is not in the room above them."""
    client = _design_tree()

    note = await _room_tree_note(client, ROOM, can_read=_readable(ROOM, PLANNING))

    assert note == _note(
        "- main room, hidden because the person you are answering has no access to it",
        PLANNING_LINE,
        HERE_LINE,
        HIDDEN_ONE,
    )


@pytest.mark.asyncio
async def test_no_note_when_the_requester_can_read_no_other_room_of_the_tree() -> None:
    """A note naming only the current room would cost tokens and tell the agent nothing it could use."""
    client = _design_tree()

    assert await _room_tree_note(client, ROOM, can_read=_readable(ROOM)) is None


@pytest.mark.asyncio
async def test_a_room_listing_this_one_is_above_it_only_when_this_room_names_it_canonical() -> None:
    """A listing this room does not name back is no sub-room relation, as in MindRoom Chat."""
    client = _client(_room(DESIGN, "design", children={ROOM}), _room(ROOM, "release-prep"))

    assert await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM)) is None


@pytest.mark.asyncio
async def test_spaces_are_neither_above_a_room_nor_among_its_sub_rooms() -> None:
    """Rooms in a Space are the Space's rooms, not sub-rooms, so a Space makes no tree."""
    client = _client(
        _room(SPACE, "workspace", children={ROOM}, room_type="m.space"),
        _room(ROOM, "release-prep", children={"!inner-space:localhost"}),
        _room("!inner-space:localhost", "inner", room_type="m.space"),
        canonical={(ROOM, SPACE)},
    )

    assert await _room_tree_note(client, ROOM, can_read=_readable(SPACE, ROOM, "!inner-space:localhost")) is None


@pytest.mark.asyncio
async def test_a_big_tree_names_its_own_sub_rooms_and_first_rooms_beside_it_and_counts_the_rest() -> None:
    """At most 15 rooms beside and under this one are checked and named, its own sub-rooms first.

    Rooms past them are never checked.
    """
    siblings = [_room(f"!sibling{index:02}:localhost", f"sibling {index:02}") for index in range(17)]
    client = _client(
        _room(DESIGN, "design", children={ROOM, *(sibling.room_id for sibling in siblings)}),
        _room(ROOM, "release-prep", children={NOTES}),
        _room(NOTES, "notes"),
        *siblings,
        canonical={(ROOM, DESIGN), (NOTES, ROOM), *((sibling.room_id, DESIGN) for sibling in siblings)},
    )
    can_read = AsyncMock(return_value=True)

    note = await _room_tree_note(client, ROOM, can_read=can_read)

    assert note is not None
    assert NOTES_LINE in note
    assert "'sibling 13'" in note
    assert "'sibling 14'" not in note
    assert note.endswith("'sibling 13' (!sibling13:localhost)\n3 more nested rooms are not shown.")
    assert {call.args[0] for call in can_read.await_args_list} == {DESIGN, ROOM, NOTES} | {
        sibling.room_id for sibling in siblings[:14]
    }


@pytest.mark.asyncio
async def test_a_listed_room_that_names_another_room_above_it_is_left_out() -> None:
    """A listing left behind after a move is no sub-room relation, as in MindRoom Chat, so it is neither named nor counted."""
    moved = "!moved:localhost"
    client = _design_tree(_room(moved, "moved"))
    client.rooms[ROOM].children.add(moved)

    note = await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM, PLANNING, NOTES, moved))

    assert note == _note(DESIGN_LINE, PLANNING_LINE, HERE_LINE, NOTES_LINE)


@pytest.mark.asyncio
async def test_a_room_moved_under_this_one_shows_under_it_while_the_room_above_still_lists_it() -> None:
    """A room both list sits under the one it names as canonical parent, so a listing left behind loses it nothing."""
    client = _client(
        _room(DESIGN, "design", children={ROOM, PLANNING}),
        _room(ROOM, "release-prep", children={PLANNING}),
        _room(PLANNING, "planning"),
        canonical={(ROOM, DESIGN), (PLANNING, ROOM)},
    )

    note = await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM, PLANNING))

    assert note == _note(DESIGN_LINE, HERE_LINE, "    - 'planning' (!planning:localhost)")


@pytest.mark.asyncio
async def test_a_room_above_that_is_nested_itself_is_not_called_the_main_room() -> None:
    """The note shows one level up, so a room above with its own room above is the room above, not the main room."""
    client = _design_tree(_room("!top:localhost", "top", children={DESIGN}), canonical={(DESIGN, "!top:localhost")})

    note = await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM, PLANNING, NOTES))

    assert note == _note("- 'design' (!design:localhost), room above", PLANNING_LINE, HERE_LINE, NOTES_LINE)


@pytest.mark.asyncio
async def test_the_note_keeps_a_room_the_agent_leaves_while_it_reads() -> None:
    """A sync drops a room the agent leaves from ``client.rooms``; the note already holds the room and finishes."""
    client = _design_tree()
    read_state = client.room_get_state_event.side_effect

    async def leave_design_while_reading(
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        client.rooms.pop(DESIGN, None)
        return await read_state(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = leave_design_while_reading

    note = await _room_tree_note(client, ROOM, can_read=_readable(DESIGN, ROOM, PLANNING, NOTES))

    assert note == _note(DESIGN_LINE, PLANNING_LINE, HERE_LINE, NOTES_LINE)


def _context(tmp_path: Path, client: MagicMock) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", access={"users": [ALICE]})}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = test_runtime_paths(tmp_path)
    return ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(room_id=ROOM, thread_id="$thread", reply_to_event_id=None),
        requester_id=ALICE,
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        plugins=config_plugins(config, runtime_paths),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )


def _joined_members(members: dict[str, tuple[str, ...]]) -> AsyncMock:
    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        return nio.JoinedMembersResponse(
            [nio.RoomMember(user_id, user_id, None) for user_id in members.get(room_id, ())],
            room_id,
        )

    return AsyncMock(side_effect=joined_members)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_a_turn_told_where_it_responds_gets_the_tree_of_rooms_the_requester_is_in(tmp_path: Path) -> None:
    """The tree follows the room access the Matrix tools check, so it names no room the requester is not in."""
    client = _design_tree(_room(PRIVATE, "acquisition plans"), design_children={PRIVATE})
    client.joined_members = _joined_members(
        {DESIGN: (AGENT, ALICE), PLANNING: (AGENT, ALICE), NOTES: (AGENT, ALICE), PRIVATE: (AGENT,)},
    )

    items = await with_room_tree((TARGET,), _context(tmp_path, client))

    assert items[0] == TARGET
    tree = items[1]
    assert (tree.key, tree.persist, tree.minimal_required, tree.cache_policy) == ("room_tree", False, True, "stable")
    assert tree.text == _note(
        DESIGN_LINE,
        PLANNING_LINE,
        HERE_LINE,
        NOTES_LINE,
        HIDDEN_ONE,
    )


@pytest.mark.asyncio
async def test_a_turn_that_cannot_message_rooms_gets_no_tree(tmp_path: Path) -> None:
    """Without the Matrix target item the turn has no matrix_message to read the rooms with."""
    client = _design_tree()
    other = EnrichmentItem(key="other", text="other")

    assert await with_room_tree((other,), _context(tmp_path, client)) == (other,)
    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_slow_homeserver_leaves_the_turn_without_a_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Access checks run one at a time, so the note gives up after a bound rather than hold the reply."""
    monkeypatch.setattr(room_tree_note, "_TIMEOUT_SECONDS", 0.01)
    client = _design_tree()

    async def hang(*_args: object) -> None:
        await asyncio.Event().wait()

    client.room_get_state_event = AsyncMock(side_effect=hang)

    async with asyncio.timeout(2):
        assert await with_room_tree((TARGET,), _context(tmp_path, client)) == (TARGET,)


@pytest.mark.asyncio
async def test_an_unreachable_homeserver_leaves_the_turn_without_a_tree(tmp_path: Path) -> None:
    """The note is optional context, so a failed read never costs the reply."""
    client = _design_tree()
    client.room_get_state_event = AsyncMock(side_effect=ClientError("down"))

    assert await with_room_tree((TARGET,), _context(tmp_path, client)) == (TARGET,)
