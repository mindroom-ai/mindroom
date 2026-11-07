"""Rooms listed under a room: the bots in that room follow them in, and out once a room is taken off the list."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.logging_config import get_logger
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

PARENT = "!parent:localhost"
ROOM = "!room:localhost"
OTHER_ROOM = "!other:localhost"
HELPER = "@mindroom_helper:localhost"
ALICE = "@alice:localhost"
MALLORY = "@mallory:localhost"


def _create(room_type: str | None = None) -> dict:
    content: dict[str, object] = {"room_version": "11"}
    if room_type is not None:
        content["type"] = room_type
    return {"type": "m.room.create", "state_key": "", "sender": ALICE, "content": content}


def _child(room_id: str, *, sender: str = ALICE, listed: bool = True) -> dict:
    content = {"via": ["localhost"]} if listed else {}
    return {"type": "m.space.child", "state_key": room_id, "sender": sender, "content": content}


def _parent(parent_id: str, *, canonical: bool = True, via: tuple[str, ...] = ("localhost",)) -> dict:
    content = {"via": list(via), "canonical": canonical}
    return {"type": "m.space.parent", "state_key": parent_id, "sender": ALICE, "content": content}


def _space_child_event(room_id: str, *, listed: bool = True) -> nio.RoomSpaceChildEvent:
    event = nio.Event.parse_event({"event_id": "$child", "origin_server_ts": 1, **_child(room_id, listed=listed)})
    assert isinstance(event, nio.RoomSpaceChildEvent)
    return event


class _Bot:
    """One bot's room lifecycle on a fake homeserver that keeps each room's state."""

    def __init__(
        self,
        tmp_path: Path,
        state: dict[str, list[dict]],
        *,
        accept_invites: bool | list[str] = True,
        joined: Iterable[str] = (PARENT,),
    ) -> None:
        self.state = state
        config = bind_runtime_paths(
            Config(agents={"helper": AgentConfig(display_name="Helper", accept_invites=accept_invites)}),
            test_runtime_paths(tmp_path),
        )
        self.client = make_matrix_client_mock(user_id=HELPER)
        self.client.rooms = {room_id: self._synced_room(room_id) for room_id in joined}
        self.client.room_get_state = AsyncMock(side_effect=self._room_state)
        self.client.room_get_state_event = AsyncMock(side_effect=self._room_state_event)
        self.client.joined_rooms = AsyncMock(side_effect=lambda: nio.JoinedRoomsResponse(rooms=list(self.client.rooms)))
        self.joinable = True
        self.memberships: list[tuple[str, str]] = []
        self.on_room_joined = AsyncMock()
        self.runtime = SimpleNamespace(client=self.client, config=config, agent_reply_memberships=MagicMock())
        paths = runtime_paths_for(config)
        self.lifecycle = BotRoomLifecycle(
            BotRoomLifecycleDeps(
                agent_name="helper",
                agent_user=AgentMatrixUser("helper", HELPER, "Helper", TEST_PASSWORD),
                runtime=self.runtime,
                runtime_paths=paths,
                continuity_store=SyncContinuityStore(paths.storage_root, "helper"),
                get_logger=lambda: get_logger("tests.listed_rooms"),
                get_configured_rooms=lambda: [PARENT],
                get_retained_room_ids=set,
                get_cleanup_exclusions=AsyncMock(return_value=set()),
                send_response=AsyncMock(return_value=None),
                change_membership=self._change_membership,
                admit_response=MagicMock(),
                on_configured_room_joined=AsyncMock(),
                on_room_joined=self.on_room_joined,
                get_conversation_reader=MagicMock(),
            ),
        )

    def _synced_room(self, room_id: str) -> nio.MatrixRoom:
        """Return a joined room as nio holds it once a sync delivered its state."""
        room = nio.MatrixRoom(room_id, HELPER)
        for event in self.state.get(room_id, []):
            if event["type"] == "m.room.create":
                room.room_type = event["content"].get("type")
            elif event["type"] == "m.space.child" and event["content"].get("via"):
                room.children.add(event["state_key"])
        return room

    async def _room_state(self, room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        if room_id not in self.state:
            return nio.RoomGetStateError("unreadable", "M_FORBIDDEN")
        return nio.RoomGetStateResponse(events=self.state[room_id], room_id=room_id)

    async def _room_state_event(
        self,
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        for event in self.state.get(room_id, []):
            if event["type"] == event_type and event["state_key"] == state_key:
                return nio.RoomGetStateEventResponse(event["content"], event_type, state_key, room_id)
        return nio.RoomGetStateEventError("not found", "M_NOT_FOUND")

    async def _change_membership(self, room_id: str, target_membership: str, **_kwargs: object) -> bool:
        self.memberships.append((room_id, target_membership))
        if target_membership == "leave":
            self.client.rooms.pop(room_id, None)
            return True
        if not self.joinable:
            return False
        self.client.rooms.setdefault(room_id, self._synced_room(room_id))
        return True

    def join(self, room_id: str) -> None:
        self.client.rooms[room_id] = self._synced_room(room_id)

    async def on_space_child(self, room_id: str, *, listed: bool = True) -> None:
        """Deliver one listing event the way nio does, after applying it to the synced room."""
        parent = self.client.rooms[PARENT]
        event = _space_child_event(room_id, listed=listed)
        parent.handle_event(event)
        await self.lifecycle.on_space_child(parent, event)
        await self.settle()

    async def settle(self) -> None:
        assert await wait_for_background_tasks(timeout=5, owner=self.runtime)


@pytest.mark.asyncio
async def test_a_bot_follows_a_room_listed_under_its_room_and_keeps_it(tmp_path: Path) -> None:
    """A room its room lists, naming that room back as its canonical parent, is joined and kept like an invitation."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]})

    await bot.on_space_child(ROOM)

    assert bot.memberships == [(ROOM, "join")]
    bot.on_room_joined.assert_awaited_once_with(ROOM)
    assert bot.lifecycle.invited_room_ids() == {ROOM}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "room_state",
    [[_create()], [_create(), _parent(PARENT, canonical=False)], [_create(), _parent(PARENT, via=())]],
    ids=["no-parent", "not-canonical", "no-servers"],
)
async def test_a_listed_room_that_does_not_name_its_parent_back_is_left_again(
    tmp_path: Path,
    room_state: list[dict],
) -> None:
    """Both links must agree: a listed room without a canonical parent link back is not under that room."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM)], ROOM: room_state})

    await bot.on_space_child(ROOM)

    assert bot.memberships == [(ROOM, "join"), (ROOM, "leave")]
    bot.on_room_joined.assert_not_awaited()
    assert bot.lifecycle.invited_room_ids() == frozenset()


@pytest.mark.asyncio
async def test_a_listed_room_the_bot_cannot_join_is_not_kept(tmp_path: Path) -> None:
    """A join that fails leaves no record that would make a restart retry the room."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]})
    bot.joinable = False

    await bot.on_space_child(ROOM)

    assert bot.memberships == [(ROOM, "join")]
    bot.on_room_joined.assert_not_awaited()
    assert bot.lifecycle.invited_room_ids() == frozenset()


@pytest.mark.asyncio
async def test_a_bot_follows_only_listings_its_invite_rule_admits(tmp_path: Path) -> None:
    """Whoever listed the room must be someone the bot would accept an invitation from."""
    state = {PARENT: [_create(), _child(ROOM, sender=MALLORY)], ROOM: [_create(), _parent(PARENT)]}
    bot = _Bot(tmp_path, state, accept_invites=[ALICE])

    await bot.on_space_child(ROOM)
    assert bot.memberships == []

    state[PARENT] = [_create(), _child(ROOM, sender=ALICE)]
    await bot.on_space_child(ROOM)
    assert bot.memberships == [(ROOM, "join")]


@pytest.mark.asyncio
async def test_a_bot_that_accepts_no_invitations_follows_nothing(tmp_path: Path) -> None:
    """With invitations off, listings are not even read."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]}, accept_invites=False)

    await bot.on_space_child(ROOM)

    assert bot.memberships == []
    bot.client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_rooms_a_space_lists_are_not_followed(tmp_path: Path) -> None:
    """A Space's rooms are its own: listing one brings no bot in."""
    bot = _Bot(tmp_path, {PARENT: [_create("m.space"), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]})

    await bot.on_space_child(ROOM)

    assert bot.memberships == []


@pytest.mark.asyncio
async def test_a_listing_the_room_no_longer_holds_changes_nothing(tmp_path: Path) -> None:
    """The room's current list decides, so a replayed or superseded listing brings no bot in."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM, listed=False)], ROOM: [_create(), _parent(PARENT)]})

    await bot.on_space_child(ROOM)

    assert bot.memberships == []


@pytest.mark.asyncio
async def test_a_room_taken_off_its_parents_list_is_left(tmp_path: Path) -> None:
    """A room still naming its parent, which no longer lists it, loses the bots in it."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM, listed=False)], ROOM: [_create(), _parent(PARENT)]})
    bot.join(ROOM)

    await bot.on_space_child(ROOM, listed=False)

    assert bot.memberships == [(ROOM, "leave")]


@pytest.mark.asyncio
async def test_a_room_taken_off_that_the_bot_is_not_in_is_not_read(tmp_path: Path) -> None:
    """Taking a room off a list concerns only the bots in that room."""
    bot = _Bot(tmp_path, {PARENT: [_create()], ROOM: [_create(), _parent(PARENT)]})

    await bot.on_space_child(ROOM, listed=False)

    assert bot.memberships == []
    bot.client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_room_taken_off_a_space_keeps_its_bots(tmp_path: Path) -> None:
    """Spaces keep their rooms' members: taking a room off a Space never takes a bot out of it."""
    bot = _Bot(tmp_path, {PARENT: [_create("m.space")], ROOM: [_create(), _parent(PARENT)]})
    bot.join(ROOM)

    await bot.on_space_child(ROOM, listed=False)

    assert bot.memberships == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {PARENT: [_create(), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]},
        {PARENT: [_create()], ROOM: [_create(), _parent(OTHER_ROOM)]},
        {PARENT: [_create()], ROOM: [_create(), _parent(PARENT, canonical=False)]},
        {ROOM: [_create(), _parent(PARENT)]},
    ],
    ids=["listed-again", "under-another-room", "not-canonical", "parent-unreadable"],
)
async def test_a_room_not_taken_off_its_parent_keeps_its_bots(tmp_path: Path, state: dict[str, list[dict]]) -> None:
    """Only a room its canonical parent stopped listing is left; anything else, or doubt, keeps the bot."""
    bot = _Bot(tmp_path, state)
    bot.join(ROOM)

    await bot.on_space_child(ROOM, listed=False)

    assert bot.memberships == []


@pytest.mark.asyncio
async def test_joining_a_configured_room_follows_the_rooms_it_already_lists(tmp_path: Path) -> None:
    """A bot that joins a room follows every room that room already lists once its state is synced."""
    bot = _Bot(
        tmp_path,
        {
            PARENT: [_create(), _child(ROOM), _child(OTHER_ROOM)],
            ROOM: [_create(), _parent(PARENT)],
            OTHER_ROOM: [_create(), _parent(PARENT)],
        },
        joined=(),
    )

    await bot.lifecycle.join_configured_rooms()
    await bot.settle()

    assert bot.memberships[0] == (PARENT, "join")
    assert sorted(bot.memberships[1:]) == [(OTHER_ROOM, "join"), (ROOM, "join")]
    assert bot.lifecycle.invited_room_ids() == {ROOM, OTHER_ROOM}


@pytest.mark.asyncio
async def test_accepting_an_invitation_follows_the_rooms_the_room_lists(tmp_path: Path) -> None:
    """A room joined by invitation brings the bot into the rooms it lists too."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]}, joined=())
    invite = nio.MatrixInvitedRoom(PARENT, HELPER)
    invite.inviter = ALICE
    bot.client.invited_rooms = {PARENT: invite}

    await bot.lifecycle._handle_recorded_invite(invite, ALICE)
    await bot.settle()

    assert bot.memberships == [(PARENT, "join"), (ROOM, "join")]


@pytest.mark.asyncio
async def test_a_followed_room_brings_the_bot_into_the_rooms_it_lists_in_turn(tmp_path: Path) -> None:
    """Rooms under a room under a room are followed too."""
    bot = _Bot(
        tmp_path,
        {
            PARENT: [_create(), _child(ROOM)],
            ROOM: [_create(), _parent(PARENT), _child(OTHER_ROOM)],
            OTHER_ROOM: [_create(), _parent(ROOM)],
        },
    )

    await bot.on_space_child(ROOM)

    assert bot.memberships == [(ROOM, "join"), (OTHER_ROOM, "join")]


@pytest.mark.asyncio
async def test_a_restart_follows_missed_listings_reading_only_rooms_that_list_one(tmp_path: Path) -> None:
    """Rejoining its rooms at startup, the bot reads again only a room whose synced list names a room it is not in."""
    bot = _Bot(
        tmp_path,
        {
            PARENT: [_create(), _child(ROOM), _child(OTHER_ROOM)],
            ROOM: [_create(), _parent(PARENT)],
            OTHER_ROOM: [_create(), _parent(PARENT), _child(PARENT)],
        },
        joined=(PARENT, OTHER_ROOM),
    )
    bot.lifecycle.deps = replace(bot.lifecycle.deps, get_configured_rooms=lambda: [PARENT, OTHER_ROOM])

    await bot.lifecycle.join_configured_rooms()
    await bot.settle()

    assert sorted(bot.memberships) == [(OTHER_ROOM, "join"), (PARENT, "join"), (ROOM, "join")]
    assert [call.args[0] for call in bot.client.room_get_state.await_args_list] == [PARENT]


@pytest.mark.asyncio
async def test_room_cleanup_during_a_follow_keeps_the_room(tmp_path: Path) -> None:
    """A cleanup pass that runs while the bot joins a listed room never leaves the room it is joining."""
    bot = _Bot(tmp_path, {PARENT: [_create(), _child(ROOM)], ROOM: [_create(), _parent(PARENT)]})
    rooms_to_leave: list[list[str]] = []
    change_membership = bot.lifecycle.deps.change_membership

    async def join_during_cleanup(room_id: str, target_membership: str, **kwargs: object) -> bool:
        joined = await change_membership(room_id, target_membership, **kwargs)
        rooms_to_leave.append(await bot.lifecycle._rooms_to_leave())
        return joined

    bot.lifecycle.deps = replace(bot.lifecycle.deps, change_membership=join_during_cleanup)

    await bot.on_space_child(ROOM)

    assert rooms_to_leave == [[]]
    assert bot.lifecycle.invited_room_ids() == {ROOM}
