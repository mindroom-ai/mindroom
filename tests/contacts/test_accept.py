"""Tests for accepting a contact: who may accept, the contact room and who is in it, and the requester's kickoff."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.constants import ORIGINAL_SENDER_KEY, SKIP_MENTIONS_KEY
from mindroom.contacts import flow
from mindroom.contacts.store import contact_room_ids
from mindroom.custom_tools.contacts import ContactsTools
from mindroom.entity_resolution import current_entity_id
from mindroom.logging_config import get_logger
from mindroom.matrix.invited_rooms_store import invited_rooms_path, remember_invited_room
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.users import AgentMatrixUser
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import TEST_PASSWORD, make_matrix_client_mock
from tests.contacts.test_tools import (
    CAROL,
    CONTACT_ROOM,
    ORIGIN_ROOM,
    ORIGIN_THREAD,
    TOOLS,
    _context,
    _pending,
    _Runtime,
    _runtime,
    _setup,
    _stored,
)
from tests.test_generated_agents import CO_OWNER, OWNER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import ContactRecord

ROOM_STATE = [
    {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
    {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "shared"}},
]
NOT_PENDING = r"^Only a pending contact can be accepted$"
REMOVED_ROOM = "!removed:localhost"
ADHOC_ROOM = "!adhoc:localhost"


@dataclass
class _Matrix:
    """The stand-ins for creating the contact room and inviting to it."""

    create: AsyncMock
    invite: AsyncMock

    def invited(self) -> set[tuple[Any, ...]]:
        """Return each invite as its client, room, and user."""
        return {call.args for call in self.invite.await_args_list}


@contextmanager
def _matrix(room_id: str | None = CONTACT_ROOM, *, invited: bool = True) -> Iterator[_Matrix]:
    """Stand in for the Matrix room creation, which yields ``room_id``, and the invites, which succeed or fail."""
    with (
        patch("mindroom.contacts.flow.create_room", new=AsyncMock(return_value=room_id)) as create,
        patch("mindroom.contacts.flow.invite_to_room", new=AsyncMock(return_value=invited)) as invite,
    ):
        yield _Matrix(create, invite)


async def _accept(
    config: Config,
    paths: RuntimePaths,
    runtime: _Runtime,
    contact_id: str,
    *,
    agent_name: str = "mind_bob",
    requester_id: str = CO_OWNER,
    rules: str | None = None,
    tools: list[str] | None = None,
) -> ContactRecord:
    return await flow.accept_contact(
        _context(config, paths, agent_name, requester_id, runtime),
        agent_name,
        contact_id,
        rules=rules,
        tools=tools,
    )


def _account(paths: RuntimePaths, agent_name: str) -> str:
    return current_entity_id(agent_name, paths).full_id


def _memberships(runtime: _Runtime, target_membership: str) -> set[str]:
    """Return the agents whose bots changed their membership of the contact room to ``target_membership``."""
    changed = set()
    for name in TOOLS:
        calls = [call.args for call in runtime.bots[name].change_local_membership.await_args_list]
        if (CONTACT_ROOM, target_membership) in calls:
            changed.add(name)
    return changed


def _joined(runtime: _Runtime) -> set[str]:
    """Return the agents whose bots joined the contact room."""
    return _memberships(runtime, "join")


def _room_lifecycle(config: Config, paths: RuntimePaths, agent_name: str) -> BotRoomLifecycle:
    """Return one agent's room lifecycle on fakes, with no configured, retained, or invited rooms."""
    user_id = _account(paths, agent_name)
    return BotRoomLifecycle(
        BotRoomLifecycleDeps(
            agent_name=agent_name,
            agent_user=AgentMatrixUser(
                agent_name=agent_name,
                user_id=user_id,
                display_name=agent_name,
                password=TEST_PASSWORD,
            ),
            runtime=SimpleNamespace(client=make_matrix_client_mock(user_id=user_id), config=config),
            runtime_paths=paths,
            continuity_store=SyncContinuityStore(paths.storage_root, agent_name),
            get_logger=lambda: get_logger("tests.contacts"),
            get_configured_rooms=list,
            get_retained_room_ids=set,
            get_cleanup_exclusions=AsyncMock(return_value=set()),
            send_response=AsyncMock(return_value=None),
            change_membership=AsyncMock(return_value=True),
            admit_response=MagicMock(),
            on_configured_room_joined=AsyncMock(),
            on_room_joined=AsyncMock(),
            get_conversation_reader=MagicMock(),
        ),
    )


def _kept(paths: RuntimePaths) -> set[str]:
    """Return the agents that keep the contact room across restarts."""
    return {name for name in TOOLS if CONTACT_ROOM in contact_room_ids(paths, name)}


@pytest.mark.asyncio
async def test_accept_requires_target_owner(tmp_path: Path) -> None:
    """Only the target's owner accepts; the requesting owner is refused, even when writing in the target's thread."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    with _matrix() as matrix:
        with pytest.raises(ValueError, match=rf"^Only {CO_OWNER} may accept this contact$"):
            await _accept(config, paths, runtime, record.id, agent_name="mind_alice", requester_id=OWNER)
        with pytest.raises(ValueError, match=rf"^Only {CO_OWNER} may use the contacts of mind_bob$"):
            await _accept(config, paths, runtime, record.id, requester_id=OWNER)
        with pytest.raises(ValueError, match=rf"^There is no contact {record.id}$"):
            await _accept(config, paths, runtime, record.id, agent_name="mind_carol", requester_id=CAROL)

    matrix.create.assert_not_awaited()
    assert [stored.status for stored in _stored(paths)] == ["pending"]


@pytest.mark.asyncio
async def test_accept_creates_room_invites_and_joins(tmp_path: Path) -> None:
    """The target agent creates an invite-only room, invites the other agent and both owners, and both agents keep it."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    with _matrix() as matrix:
        accepted = await _accept(config, paths, runtime, record.id)

    client = runtime.clients["mind_bob"]
    matrix.create.assert_awaited_once_with(client, "Alice's Mind ↔ Bob's Mind", initial_state=ROOM_STATE)
    invitees = (_account(paths, "mind_alice"), OWNER, CO_OWNER)
    assert matrix.invited() == {(client, CONTACT_ROOM, user_id) for user_id in invitees}
    assert matrix.invite.await_count == len(invitees)
    assert _joined(runtime) == _kept(paths) == {"mind_alice", "mind_bob"}
    assert (accepted.status, accepted.room_id) == ("active", CONTACT_ROOM)
    assert _stored(paths) == [accepted]


@pytest.mark.asyncio
async def test_the_personal_room_agent_accepts_and_the_target_agent_creates_the_room(tmp_path: Path) -> None:
    """The agent that asked the owner accepts; the target agent creates the room, and the asking agent stays out of it."""
    config, paths = _setup(tmp_path, work=True)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, target="work_bob")

    with _matrix() as matrix:
        refusal = rf"^Only mind_bob, the agent of {CO_OWNER}'s personal room, may accept this contact$"
        with pytest.raises(ValueError, match=refusal):
            await _accept(config, paths, runtime, record.id, agent_name="work_bob")
        matrix.create.assert_not_awaited()
        await _accept(config, paths, runtime, record.id)

    client = runtime.clients["work_bob"]
    matrix.create.assert_awaited_once_with(client, "Alice's Mind ↔ Bob's Work", initial_state=ROOM_STATE)
    invitees = (_account(paths, "mind_alice"), OWNER, CO_OWNER)
    assert matrix.invited() == {(client, CONTACT_ROOM, user_id) for user_id in invitees}
    assert _joined(runtime) == _kept(paths) == {"mind_alice", "work_bob"}


@pytest.mark.asyncio
async def test_a_person_target_accepts_through_their_personal_agent_and_the_requesting_agent_creates_the_room(
    tmp_path: Path,
) -> None:
    """A person has no agent in the room, so the requesting agent creates it and invites the person and its owner."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, target=CAROL)
    carol = {"agent_name": "mind_carol", "requester_id": CAROL}

    with _matrix() as matrix:
        with pytest.raises(ValueError, match=r"^A person has no rules or tools"):
            await _accept(config, paths, runtime, record.id, rules="Be brief", **carol)
        await _accept(config, paths, runtime, record.id, **carol)

    client = runtime.clients["mind_alice"]
    matrix.create.assert_awaited_once_with(client, f"Alice's Mind ↔ {CAROL}", initial_state=ROOM_STATE)
    assert matrix.invited() == {(client, CONTACT_ROOM, OWNER), (client, CONTACT_ROOM, CAROL)}
    assert _joined(runtime) == _kept(paths) == {"mind_alice"}


@pytest.mark.asyncio
async def test_accept_saves_rules_and_tools_after_validation(tmp_path: Path) -> None:
    """The target side's tools are checked before a room exists; what accept leaves out keeps what was set before."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, rules="Be brief")
    bob = _context(config, paths, "mind_bob", CO_OWNER, runtime)
    await flow.update_contact(bob, "mind_bob", record.id, rules=None, tools=["calculator"])

    with _matrix() as matrix:
        refusal = r"^Not among the tools a contact may use: browser; allowed: calculator, matrix_message$"
        with pytest.raises(ValueError, match=refusal):
            await _accept(config, paths, runtime, record.id, tools=["browser"])
        matrix.create.assert_not_awaited()
        assert _stored(paths)[0].status == "pending"
        accepted = await _accept(config, paths, runtime, record.id, rules="  Only dinner ")

    assert (accepted.target_side.rules, accepted.target_side.tools) == ("Only dinner", ["calculator"])
    assert (accepted.requester_side.rules, accepted.requester_side.tools) == ("Be brief", None)
    assert _stored(paths) == [accepted]


@pytest.mark.asyncio
async def test_accept_kicks_off_requesting_agent_as_its_owner(tmp_path: Path) -> None:
    """The requesting agent mentions itself in its thread, for its owner, naming the target by ID, not a chosen name."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    with _matrix():
        await _accept(config, paths, runtime, record.id)

    runtime.sent("mind_alice").assert_awaited_once_with(
        ORIGIN_ROOM,
        f"@mind_alice mind_bob accepted your contact request. Continue the task in the contact room {CONTACT_ROOM}: "
        "Plan the dinner",
        ORIGIN_THREAD,
        "native:contacts",
        {ORIGINAL_SENDER_KEY: OWNER},
        trigger_dispatch=True,
    )
    for name in ("mind_bob", "mind_carol", "work_bob"):
        runtime.sent(name).assert_not_awaited()


@pytest.mark.asyncio
async def test_a_kickoff_that_cannot_be_sent_keeps_the_contact_active(tmp_path: Path) -> None:
    """The acceptance stands when the requesting agent cannot post in its thread."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    runtime.sent("mind_alice").return_value = None

    with _matrix():
        accepted = await _accept(config, paths, runtime, record.id)

    runtime.sent("mind_alice").assert_awaited_once()
    assert accepted.status == "active"
    assert _stored(paths) == [accepted]


@pytest.mark.asyncio
async def test_a_person_requester_hears_from_the_accepting_agent_only_in_a_room_it_is_in(tmp_path: Path) -> None:
    """No turn starts for a person who asked; the accepting agent tells their thread when it is in that room."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    to_bob = await _pending(config, paths, runtime, party="me")
    to_carol = await _pending(config, paths, runtime, party="me", target="mind_carol")
    runtime.bots["mind_carol"].current_joined_room_ids.return_value = frozenset({ORIGIN_ROOM})

    with _matrix() as matrix:
        await _accept(config, paths, runtime, to_bob.id)
        await _accept(config, paths, runtime, to_carol.id, agent_name="mind_carol", requester_id=CAROL)

    first = matrix.create.await_args_list[0]
    assert first.args == (runtime.clients["mind_bob"], f"{OWNER} ↔ Bob's Mind")
    for name in ("mind_alice", "mind_bob"):
        runtime.sent(name).assert_not_awaited()
    runtime.sent("mind_carol").assert_awaited_once_with(
        ORIGIN_ROOM,
        f"mind_carol accepted your contact request. The contact room is {CONTACT_ROOM}.",
        ORIGIN_THREAD,
        "native:contacts",
        {SKIP_MENTIONS_KEY: True},
        trigger_dispatch=False,
    )


@pytest.mark.asyncio
async def test_accept_refuses_non_pending(tmp_path: Path) -> None:
    """An accepted, declined, or removed contact cannot be accepted, so a second acceptance opens no second room."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    accepted = await _pending(config, paths, runtime)
    declined = await _pending(config, paths, runtime, target=CAROL)
    removed = await _pending(config, paths, runtime, party="me", target="mind_carol")
    carol = _context(config, paths, "mind_carol", CAROL, runtime)
    await flow.decline_contact(carol, "mind_carol", declined.id)
    await flow.remove_contact(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice", removed.id)

    with _matrix() as matrix:
        await _accept(config, paths, runtime, accepted.id)
        with pytest.raises(ValueError, match=NOT_PENDING):
            await _accept(config, paths, runtime, accepted.id)
        for record in (declined, removed):
            with pytest.raises(ValueError, match=NOT_PENDING):
                await _accept(config, paths, runtime, record.id, agent_name="mind_carol", requester_id=CAROL)

    matrix.create.assert_awaited_once()
    assert [record.status for record in _stored(paths)] == ["active", "declined", "removed"]


@pytest.mark.asyncio
async def test_a_room_that_cannot_be_set_up_is_left_and_the_request_stays_pending(tmp_path: Path) -> None:
    """A stopped agent creates nothing; a failed creation, invite, or join leaves the room and keeps the request."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    runtime.bots["mind_alice"].running = False
    with _matrix() as matrix, pytest.raises(ValueError, match=r"^mind_alice is not running"):
        await _accept(config, paths, runtime, record.id)
    matrix.create.assert_not_awaited()
    runtime.bots["mind_alice"].running = True

    with _matrix(None), pytest.raises(ValueError, match=r"^The contact room could not be created"):
        await _accept(config, paths, runtime, record.id)

    with _matrix(invited=False), pytest.raises(ValueError, match=r"could not be invited to the contact room"):
        await _accept(config, paths, runtime, record.id)
    assert _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    assert _joined(runtime) == set()
    for name in ("mind_alice", "mind_bob"):
        runtime.bots[name].change_local_membership.reset_mock()

    runtime.bots["mind_bob"].change_local_membership.return_value = False
    with _matrix(), pytest.raises(ValueError, match=r"^mind_bob could not join the contact room"):
        await _accept(config, paths, runtime, record.id)
    assert _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    for client in runtime.clients.values():
        client.room_leave.assert_not_awaited()
    assert _kept(paths) == set()
    assert [stored.status for stored in _stored(paths)] == ["pending"]
    runtime.sent("mind_alice").assert_not_awaited()


@pytest.mark.asyncio
async def test_room_cleanup_keeps_an_active_contact_room_for_its_agents_and_leaves_a_removed_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent party keeps its active contact's room across restarts, even accepting no invites, until removal."""
    config, paths = _setup(tmp_path, accept_invites=False)
    runtime = _runtime()
    active = await _pending(config, paths, runtime)
    removed = await _pending(config, paths, runtime, target=CAROL)
    with _matrix():
        await _accept(config, paths, runtime, active.id)
    with _matrix(REMOVED_ROOM):
        await _accept(config, paths, runtime, removed.id, agent_name="mind_carol", requester_id=CAROL)
    await flow.remove_contact(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice", removed.id)

    left: list[str] = []

    async def record_left(
        _client: object,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]],
    ) -> list[str]:
        del leave_room_action
        left.extend(room_ids)
        return room_ids

    joined = [CONTACT_ROOM, REMOVED_ROOM, "!stale:localhost"]
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=joined))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", record_left)
    left_by_agent = {}
    for name in ("mind_alice", "mind_bob", "mind_carol"):
        await _room_lifecycle(config, paths, name).leave_unconfigured_rooms()
        left_by_agent[name], left[:] = sorted(left), []

    assert left_by_agent == {
        "mind_alice": [REMOVED_ROOM, "!stale:localhost"],
        "mind_bob": [REMOVED_ROOM, "!stale:localhost"],
        "mind_carol": [CONTACT_ROOM, REMOVED_ROOM, "!stale:localhost"],
    }


@pytest.mark.asyncio
async def test_a_contact_room_is_never_kept_as_an_invited_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent that accepts invites records the contact room's invite, yet never rejoins the room once it is removed."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    with _matrix():
        await _accept(config, paths, runtime, record.id)
    # The invite handler of an agent that accepts invites records every room whose invite it accepted.
    for room_id in (CONTACT_ROOM, ADHOC_ROOM):
        remember_invited_room(invited_rooms_path(paths, "mind_alice"), room_id)
    assert _room_lifecycle(config, paths, "mind_alice").invited_room_ids() == {ADHOC_ROOM}

    await flow.remove_contact(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice", record.id)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    lifecycle = _room_lifecycle(config, paths, "mind_alice")
    await lifecycle.join_configured_rooms()

    assert lifecycle.invited_room_ids() == {ADHOC_ROOM}
    lifecycle.deps.change_membership.assert_awaited_once_with(ADHOC_ROOM, "join")


@pytest.mark.asyncio
async def test_toolkit_accept_contact_names_the_room(tmp_path: Path) -> None:
    """The toolkit answers with the contact room, or with the refusal as an Error line."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    tool = ContactsTools("mind_bob")

    with tool_runtime_context(_context(config, paths, "mind_bob", CO_OWNER, runtime)), _matrix():
        refused = await tool.accept_contact(record.id, tools=["browser"])
        accepted = await tool.accept_contact(record.id)

    assert refused.startswith("Error: Not among the tools a contact may use: browser")
    assert accepted == f"Accepted. Contact room: {CONTACT_ROOM}."
