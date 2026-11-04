"""Contact flows run to their end state on a homeserver that keeps rooms, members, invitations, and timelines.

Each flow drives the contacts flow functions and the matrix_message contact gate step by step, then checks the
invariants every flow must leave behind, whatever happened on the way.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.bot_room_lifecycle import BotRoomLifecycle
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.contacts import flow
from mindroom.contacts.store import contact_room_ids
from mindroom.hooks.sender import build_hook_message_sender
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_matrix_client_mock, make_relation_lookup
from tests.contacts.test_accept import NOT_PENDING, _accept, _account, _room_lifecycle
from tests.contacts.test_matrix_message_policy import (  # noqa: F401 - _reset_rate_limit is an autouse pytest fixture
    _call,
    _reset_rate_limit,
)
from tests.contacts.test_tools import (
    _ORIGIN,
    BOB_ROOM,
    CAROL,
    CAROL_ROOM,
    _context,
    _data,
    _request,
    _Runtime,
    _runtime,
    _setup,
    _stored,
)
from tests.test_generated_agents import CO_OWNER, OWNER, _load, _personal

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import ContactRecord

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]

RETRY_ROOM = "!retry:example.org"
SECOND_THREAD = MessageTarget.resolve(room_id=_ORIGIN.room_id, thread_id="$second", reply_to_event_id="$again")
RETRY_ORIGIN = MessageTarget.resolve(room_id=RETRY_ROOM, thread_id="$retry", reply_to_event_id="$again")
OWNERS = {"mind_alice": OWNER, "mind_bob": CO_OWNER, "mind_carol": CAROL}
# Where each agent's owner talks to it: Alice asks hers in two rooms, and Bob and Carol have their personal rooms.
HOME_ROOMS = {"mind_alice": {_ORIGIN.room_id, RETRY_ROOM}, "mind_bob": {BOB_ROOM}, "mind_carol": {CAROL_ROOM}}


@dataclass
class _Event:
    """One event on a room's timeline, as every member's client reads it."""

    sender: str
    content: dict[str, Any]

    @property
    def thread_id(self) -> str | None:
        """Return the thread its ``m.thread`` relation files it under; a client shows any other event on the timeline."""
        relation = self.content.get("m.relates_to") or {}
        return relation.get("event_id") if relation.get("rel_type") == "m.thread" else None


@dataclass
class _Room:
    """What the server holds for one room."""

    creator: str
    # Joined users, who read every new event and may send; the creator joins on creation.
    members: set[str] = field(default_factory=set)
    # Pending invitations, each shown in the invitee's client until they join or reject it, or it is withdrawn.
    invited: set[str] = field(default_factory=set)
    timeline: list[_Event] = field(default_factory=list)


@dataclass
class _Homeserver:
    """A Matrix server that keeps rooms, members, invitations, and timelines like a real one.

    Like a real one means: only a member invites or sends, every room is invite-only as contact and personal rooms are,
    so a join needs a pending invitation and uses it up, leaving with a pending invitation rejects it, and only the
    room's creator, the only member with power in a room MindRoom creates, kicks, which also withdraws an invitation.
    A fake that let any member kick, or forgot invitations, would pass a flow that leaves an owner invited to a dead room.
    """

    rooms: dict[str, _Room] = field(default_factory=dict)
    # Users whose next join fails, as when the server has not processed their invitation yet.
    refused_joins: set[str] = field(default_factory=set)
    # Rooms whose next message the server rejects with M_FORBIDDEN, as when a power-level change took the sender's right
    # to post away.
    refused_sends: set[str] = field(default_factory=set)
    _ids: count[int] = field(default_factory=lambda: count(1))

    def joined(self, user_id: str) -> frozenset[str]:
        return frozenset(room_id for room_id, room in self.rooms.items() if user_id in room.members)

    def client(self, user_id: str) -> AsyncMock:
        """Return a client of ``user_id``, whose room calls go to this server."""
        client = make_matrix_client_mock(user_id=user_id)
        # Without a token the client reads no capabilities, so the server picks the room version.
        client.access_token = None
        client.room_create = AsyncMock(side_effect=partial(self._create, user_id))
        client.room_invite = AsyncMock(side_effect=partial(self._invite, user_id))
        client.room_kick = AsyncMock(side_effect=partial(self._kick, user_id))
        client.room_send = AsyncMock(side_effect=partial(self._send, user_id))
        client.joined_rooms = AsyncMock(side_effect=lambda: nio.JoinedRoomsResponse(rooms=sorted(self.joined(user_id))))
        client.joined_members = AsyncMock(side_effect=self._joined_members)
        return client

    async def membership(self, user_id: str, room_id: str, membership: str) -> bool:
        """Join or leave for a user, as an agent bot's local membership change asks the server to."""
        room = self.rooms[room_id]
        if user_id not in room.members | room.invited:
            return False
        if membership == "join" and user_id in self.refused_joins:
            self.refused_joins.remove(user_id)
            return False
        room.invited.discard(user_id)
        if membership == "join":
            room.members.add(user_id)
        else:
            room.members.discard(user_id)
        return True

    async def _create(self, user_id: str, **_room_config: object) -> nio.RoomCreateResponse:
        room_id = f"!created{next(self._ids)}:localhost"
        self.rooms[room_id] = _Room(creator=user_id, members={user_id})
        return nio.RoomCreateResponse(room_id)

    async def _invite(self, inviter: str, room_id: str, user_id: str) -> nio.RoomInviteResponse | nio.RoomInviteError:
        room = self.rooms[room_id]
        if inviter not in room.members or user_id in room.members:
            return nio.RoomInviteError("not allowed", "M_FORBIDDEN")
        room.invited.add(user_id)
        return nio.RoomInviteResponse()

    async def _kick(self, kicker: str, room_id: str, user_id: str, **_kick: object) -> object:
        room = self.rooms[room_id]
        if kicker != room.creator or kicker not in room.members:
            return nio.RoomKickError("not allowed", "M_FORBIDDEN")
        room.members.discard(user_id)
        room.invited.discard(user_id)
        return nio.RoomKickResponse()

    async def _send(self, sender: str, *, room_id: str, content: dict[str, Any], **_send: object) -> object:
        room = self.rooms[room_id]
        if sender not in room.members:
            return nio.RoomSendError("not sent", "M_FORBIDDEN", room_id=room_id)
        if room_id in self.refused_sends:
            self.refused_sends.discard(room_id)
            return nio.RoomSendError("not sent", "M_FORBIDDEN", room_id=room_id)
        room.timeline.append(_Event(sender, content))
        return nio.RoomSendResponse(f"$event{next(self._ids)}", room_id)

    async def _joined_members(self, room_id: str) -> nio.JoinedMembersResponse:
        members = [nio.RoomMember(user_id, "", "") for user_id in sorted(self.rooms[room_id].members)]
        return nio.JoinedMembersResponse(members, room_id)


@dataclass
class _World:
    """Alice, Bob, and Carol with their personal agents on one homeserver, and the contacts store they share."""

    config: Config
    paths: RuntimePaths
    server: _Homeserver
    runtime: _Runtime
    # Where Alice last asked her agent for a contact.
    latest_ask: MessageTarget | None = None
    # Each room an acceptance made a contact's active room, which its owners keep with their invitations once it ends.
    activated: set[str] = field(default_factory=set)


def _world(tmp_path: Path) -> _World:
    """Seat each agent and its owner in their home rooms, and run each agent's bot on the server."""
    config, paths = _setup(tmp_path)
    world = _World(config, paths, _Homeserver(), _runtime())
    for name, rooms in HOME_ROOMS.items():
        account = _account(world.paths, name)
        world.server.rooms |= {room_id: _Room(creator=account, members={account, OWNERS[name]}) for room_id in rooms}
        client = world.server.client(account)
        world.runtime.clients[name] = client
        bot = world.runtime.bots[name]
        bot.client = client
        bot.change_local_membership = AsyncMock(side_effect=partial(world.server.membership, account))
        bot.hook_context.send_message = build_hook_message_sender(
            client,
            config,
            paths,
            conversation_reader=bot.conversation_reader,
        )
    return world


async def _ask(world: _World, origin: MessageTarget = _ORIGIN) -> ContactRecord:
    """Alice asks her agent at ``origin`` to get in touch with Bob's agent, which posts the request in Bob's room."""
    world.latest_ask = origin
    return await _request(world.config, world.paths, world.runtime, origin=origin)


async def _bob_accepts(world: _World, contact_id: str) -> str:
    """Bob's agent accepts the request for him; return the room the acceptance made active, which the world notes."""
    accepted = await _accept(world.config, world.paths, world.runtime, contact_id)
    assert accepted.room_id is not None
    world.activated.add(accepted.room_id)
    return accepted.room_id


async def _ask_unseen(world: _World) -> None:
    """Alice asks at the original thread, and the request never reaches Bob's room, so her agent says to ask again."""
    world.server.refused_sends.add(BOB_ROOM)
    with pytest.raises(ValueError, match=r"could not be posted"):
        await _ask(world)


async def _matrix_message(world: _World, agent_name: str, room_id: str, **call: object) -> dict[str, Any]:
    """Call matrix_message in a turn of ``agent_name`` in ``room_id``, run as its owner as every contact turn is."""
    context = make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(room_id=room_id, thread_id=None, reply_to_event_id=None),
        requester_id=OWNERS[agent_name],
        client=world.runtime.clients[agent_name],
        config=world.config,
        runtime_paths=world.paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    return await _call(context, **call)


def _answers(world: _World) -> list[tuple[str, _Event]]:
    """Return what Alice's agent posted outside contact rooms, which only ever answers her request."""
    contact_rooms = {record.room_id for record in _stored(world.paths)}
    return [
        (room_id, event)
        for room_id, room in world.server.rooms.items()
        if room_id not in contact_rooms
        for event in room.timeline
        if event.sender == _account(world.paths, "mind_alice")
    ]


def _assert_world_invariants(world: _World) -> None:
    """Check what every flow leaves behind: no stranded invitation or membership, answers in place, and quiet notices."""
    for room_id, room in world.server.rooms.items():
        if room_id not in world.activated:
            # A room that never became active leaves nobody invited to it.
            assert not room.invited, f"{sorted(room.invited)} stay invited to dead {room_id}"
    for name, home in HOME_ROOMS.items():
        kept = home | contact_room_ids(world.config, world.paths, name)
        stray = world.server.joined(_account(world.paths, name)) - kept
        assert not stray, f"{name} stays in rooms nothing keeps it in: {sorted(stray)}"
    latest = world.latest_ask
    asked = None if latest is None else (latest.room_id, latest.resolved_thread_id)
    for room_id, event in _answers(world):
        assert (room_id, event.thread_id) == asked, event.content["body"]
    for room in world.server.rooms.values():
        for event in room.timeline:
            if event.content.get(SKIP_MENTIONS_KEY):
                # A notice that skips mentions pings nobody: no mentioned user and no mention pill.
                assert not event.content.get("m.mentions", {}).get("user_ids"), event.content
                assert "matrix.to" not in event.content.get("formatted_body", ""), event.content


async def test_a_declined_retry_is_answered_in_the_thread_that_retried(tmp_path: Path) -> None:
    """A request whose notice failed and was asked again from another thread tells its decline in that thread.

    The notice names the owner who declined by Matrix ID as plain text, so it pings nobody though it reads like one.
    """
    world = _world(tmp_path)
    await _ask_unseen(world)
    record = await _ask(world, SECOND_THREAD)

    bob = _context(world.config, world.paths, "mind_bob", CO_OWNER, world.runtime)
    await flow.decline_contact(flow.tool_actor(bob, "mind_bob"), record.id)

    [(_room_id, notice)] = _answers(world)
    assert notice.content["body"].startswith(f"{CO_OWNER} declined contact {record.id}")
    assert notice.content[SKIP_MENTIONS_KEY] is True
    assert [stored.status for stored in _stored(world.paths)] == ["declined"]
    _assert_world_invariants(world)


async def test_an_accepted_retry_kicks_off_in_the_thread_that_retried(tmp_path: Path) -> None:
    """A request asked again from another room starts the requesting agent's task in the room and thread that retried."""
    world = _world(tmp_path)
    await _ask_unseen(world)
    record = await _ask(world, RETRY_ORIGIN)

    room_id = await _bob_accepts(world, record.id)

    room = world.server.rooms[room_id]
    assert (room.members, room.invited) == (
        {_account(world.paths, "mind_alice"), _account(world.paths, "mind_bob")},
        {OWNER, CO_OWNER},
    )
    [(_room_id, kickoff)] = _answers(world)
    # Only a mentioned agent answers a message in a room with other responders.
    assert kickoff.content["m.mentions"] == {"user_ids": [_account(world.paths, "mind_alice")]}
    _assert_world_invariants(world)


async def test_a_seating_that_fails_after_the_invitations_strands_no_owner(tmp_path: Path) -> None:
    """An agent that cannot join leaves its owners no invitation to the abandoned room, and the next acceptance works."""
    world = _world(tmp_path)
    record = await _ask(world)
    world.server.refused_joins.add(_account(world.paths, "mind_alice"))

    with pytest.raises(ValueError, match=r"^mind_alice could not join the contact room"):
        await _bob_accepts(world, record.id)

    [pending] = _stored(world.paths)
    assert pending.status == "pending"
    assert pending.room_id is not None
    abandoned = world.server.rooms[pending.room_id]
    assert (abandoned.members, abandoned.invited) == (set(), set())
    _assert_world_invariants(world)

    room_id = await _bob_accepts(world, record.id)

    assert [stored.status for stored in _stored(world.paths)] == ["active", "removed"]
    assert world.server.rooms[room_id].invited == {OWNER, CO_OWNER}
    _assert_world_invariants(world)


async def test_the_acceptance_that_loses_a_race_strands_no_owner(tmp_path: Path) -> None:
    """Of two acceptances at once, the one whose room the store no longer names withdraws its owners' invitations."""
    world = _world(tmp_path)
    record = await _ask(world)
    paused, resume = asyncio.Event(), asyncio.Event()
    join = world.runtime.bots["mind_alice"].change_local_membership.side_effect

    async def join_after_the_other_acceptance(room_id: str, membership: str) -> bool:
        if not paused.is_set():
            paused.set()
            await resume.wait()
        return await join(room_id, membership)

    world.runtime.bots["mind_alice"].change_local_membership.side_effect = join_after_the_other_acceptance
    first = asyncio.create_task(_bob_accepts(world, record.id))
    await paused.wait()
    second = await _bob_accepts(world, record.id)
    resume.set()
    with pytest.raises(ValueError, match=NOT_PENDING):
        await first

    assert [stored.status for stored in _stored(world.paths)] == ["active", "removed"]
    assert world.server.rooms[second].members == {
        _account(world.paths, "mind_alice"),
        _account(world.paths, "mind_bob"),
    }
    assert len(_answers(world)) == 1
    _assert_world_invariants(world)


async def _accepted_and_relayed(world: _World) -> str:
    """Return the room of a contact Bob accepted, where Alice's agent already wrote to Bob's."""
    record = await _ask(world)
    room_id = await _bob_accepts(world, record.id)
    # Bob's agent never admits Alice by access, yet her agent can name it as recipient there and so wake it.
    sent = await _matrix_message(world, "mind_alice", room_id, message="Free Friday?", recipient="mind_bob")
    assert sent["status"] == "ok", sent
    [relay] = world.server.rooms[room_id].timeline
    assert relay.content["m.mentions"] == {"user_ids": [_account(world.paths, "mind_bob")]}
    return room_id


async def test_a_removed_contact_takes_nothing_more_and_its_agents_leave(tmp_path: Path) -> None:
    """After Alice removes the contact, its agents are gone from the room and her agent's matrix_message sends nothing there."""
    world = _world(tmp_path)
    room_id = await _accepted_and_relayed(world)
    alice = _context(world.config, world.paths, "mind_alice", OWNER, world.runtime)

    await flow.remove_contact(flow.tool_actor(alice, "mind_alice"), _stored(world.paths)[0].id)
    refused = await _matrix_message(world, "mind_alice", room_id, message="Still there?")

    assert refused["message"].endswith("was removed, so nothing more can be sent into its room")
    room = world.server.rooms[room_id]
    # The owners keep the room and its history.
    assert (room.members, room.invited) == (set(), {OWNER, CO_OWNER})
    assert len(room.timeline) == 1
    _assert_world_invariants(world)


async def test_removing_contacts_from_an_agent_ends_its_contact_for_both_sides(tmp_path: Path) -> None:
    """The agent that loses contacts leaves its contact's room, and neither agent sends anything more there."""
    world = _world(tmp_path)
    room_id = await _accepted_and_relayed(world)
    data = _data()
    data["personal_agents"]["mind"]["users"] = {OWNER: {"contacts": None}}
    world.config = _load(tmp_path, data, entries=[_personal(), _personal(CO_OWNER), _personal(CAROL)])

    # The reload reconciles the rooms of only the agent whose contacts changed.
    await _reconcile_rooms(world, "mind_alice")

    for name in ("mind_alice", "mind_bob"):
        refused = await _matrix_message(world, name, room_id, message="Still there?")
        assert refused["message"].endswith("was removed, so nothing more can be sent into its room"), name
    assert [stored.status for stored in _stored(world.paths)] == ["removed"]
    room = world.server.rooms[room_id]
    # Bob's agent stays in the removed contact's room, where it starts and sends nothing, until its own restart: an
    # accepted limitation in docs/dev/2026-11-01-agent-contacts-design.md.
    assert (room.members, room.invited) == ({_account(world.paths, "mind_bob")}, {OWNER, CO_OWNER})

    # Its restart reconciles its rooms, and it leaves.
    await _reconcile_rooms(world, "mind_bob")
    assert (room.members, room.invited) == (set(), {OWNER, CO_OWNER})
    assert len(room.timeline) == 1
    _assert_world_invariants(world)


async def _reconcile_rooms(world: _World, name: str) -> None:
    """Leave the rooms nothing keeps an agent in, as a reload or restart does for each agent it reconciles."""
    deps = _room_lifecycle(world.config, world.paths, name).deps
    await BotRoomLifecycle(
        replace(
            deps,
            runtime=SimpleNamespace(client=world.runtime.clients[name], config=world.config),
            change_membership=world.runtime.bots[name].change_local_membership,
            get_configured_rooms=partial(sorted, HOME_ROOMS[name]),
        ),
    ).leave_unconfigured_rooms()
