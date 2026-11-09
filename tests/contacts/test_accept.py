"""Tests for accepting a contact: who may accept, the contact room and who is in it, and the requester's kickoff."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
import yaml

from mindroom.bot_room_lifecycle import BotRoomLifecycle, BotRoomLifecycleDeps
from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME, SKIP_MENTIONS_KEY
from mindroom.contacts import flow
from mindroom.contacts.store import _contacts_path, contact_room_ids, update_contacts
from mindroom.contacts.turns import _ContactAdmission, contact_admission, contact_room
from mindroom.custom_tools.contacts import ContactsTools
from mindroom.entity_resolution import current_entity_id
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.invited_rooms_store import invited_rooms_path, remember_invited_room
from mindroom.matrix.personal_room_store import personal_room_record_path
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.users import AgentMatrixUser
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import TEST_PASSWORD, make_matrix_client_mock
from tests.contacts.test_tools import (
    BOB_ROOM,
    CAROL,
    CONTACT_ROOM,
    ORIGIN_ROOM,
    ORIGIN_THREAD,
    TOOLS,
    _context,
    _data,
    _delivered,
    _pending,
    _request,
    _Runtime,
    _runtime,
    _setup,
    _stored,
    _terms,
)
from tests.test_generated_agents import CO_OWNER, OWNER, _load, _personal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import ContactRecord

ROOM_STATE = [
    {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
    {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "shared"}},
]
NOT_PENDING = r"^Only a pending contact can be accepted$"
REMOVED_ROOM = "!removed:localhost"
FAILED_ROOM = "!failed:localhost"
ADHOC_ROOM = "!adhoc:localhost"


@dataclass
class _Matrix:
    """The stand-ins for creating the contact room, inviting to it, and withdrawing an invitation."""

    create: AsyncMock
    invite: AsyncMock
    kick: AsyncMock

    def invited(self) -> set[tuple[Any, ...]]:
        """Return each invite as its client, room, and user."""
        return {call.args for call in self.invite.await_args_list}

    def withdrawn(self) -> list[tuple[Any, ...]]:
        """Return each withdrawn invitation as its client, room, and user."""
        return [call.args for call in self.kick.await_args_list]


@contextmanager
def _matrix(room_id: str | None = CONTACT_ROOM, *, invited: bool = True) -> Iterator[_Matrix]:
    """Stand in for the Matrix room creation, which yields ``room_id``, and the invites, which succeed or fail."""
    with (
        patch("mindroom.contacts.flow.create_room", new=AsyncMock(return_value=room_id)) as create,
        patch("mindroom.contacts.flow.invite_to_room", new=AsyncMock(return_value=invited)) as invite,
        patch("mindroom.contacts.flow.kick_from_room", new=AsyncMock(return_value=True)) as kick,
    ):
        yield _Matrix(create, invite, kick)


def _owners_withdrawn(runtime: _Runtime, *owners: str, room_id: str = CONTACT_ROOM) -> list[tuple[Any, ...]]:
    """Return how the room's creator, Bob's agent, withdraws the invitations of ``owners``."""
    return [(runtime.clients["mind_bob"], room_id, owner) for owner in owners]


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
        flow.tool_actor(_context(config, paths, agent_name, requester_id, runtime), agent_name),
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


def _room_lifecycle(
    config: Config,
    paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], AgentBot | None] = lambda _name: None,
) -> BotRoomLifecycle:
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
            refresh_memberships=AsyncMock(),
            on_configured_room_joined=AsyncMock(),
            on_room_joined=AsyncMock(),
            get_conversation_reader=MagicMock(),
            get_agent_bot=agent_bot,
        ),
    )


def _failing_membership(*, fails_on: str) -> Callable[[str, str], Awaitable[bool]]:
    """Return a membership change that raises for ``fails_on`` and succeeds otherwise."""

    async def change(_room_id: str, target_membership: str) -> bool:
        if target_membership == fails_on:
            msg = f"{fails_on} failed"
            raise RuntimeError(msg)
        return True

    return change


def _kept(config: Config, paths: RuntimePaths) -> set[str]:
    """Return the agents that keep the contact room across restarts."""
    return {name for name in TOOLS if CONTACT_ROOM in contact_room_ids(config, paths, name)}


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
    assert _joined(runtime) == _kept(config, paths) == {"mind_alice", "mind_bob"}
    assert (accepted.status, accepted.room_id) == ("active", CONTACT_ROOM)
    assert _stored(paths) == [accepted]
    assert matrix.withdrawn() == []


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
    assert _joined(runtime) == _kept(config, paths) == {"mind_alice", "work_bob"}


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
    assert _joined(runtime) == _kept(config, paths) == {"mind_alice"}


@pytest.mark.asyncio
async def test_accept_saves_rules_and_tools_after_validation(tmp_path: Path) -> None:
    """The target side's tools are checked before a room exists; what accept leaves out keeps what was set before."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, rules="Be brief")
    bob = _context(config, paths, "mind_bob", CO_OWNER, runtime)
    await flow.update_contact(flow.tool_actor(bob, "mind_bob"), record.id, rules=None, tools=["calculator"])

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
    """The requesting agent mentions itself in its thread, for its owner, naming the target by ID, not a chosen name.

    Its logs name that thread, not the accepting turn's.
    """
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    log_contexts = []

    async def send(*_args: object, **_kwargs: object) -> str:
        log_contexts.append(structlog.contextvars.get_contextvars())
        return "$origin-notice"

    runtime.sent("mind_alice").side_effect = send
    with _matrix(), bound_log_context(room_id=BOB_ROOM, thread_id="$accepting"):
        await _accept(config, paths, runtime, record.id)

    assert log_contexts == [{"room_id": ORIGIN_ROOM, "thread_id": ORIGIN_THREAD}]
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

    with _matrix() as matrix, _delivered() as send:
        await _accept(config, paths, runtime, to_bob.id)
        await _accept(config, paths, runtime, to_carol.id, agent_name="mind_carol", requester_id=CAROL)

    first = matrix.create.await_args_list[0]
    assert first.args == (runtime.clients["mind_bob"], f"{OWNER} ↔ Bob's Mind")
    for name in TOOLS:
        runtime.sent(name).assert_not_awaited()
    (call,) = send.await_args_list
    client, room_id, content = call.args
    assert (client, room_id) == (runtime.clients["mind_carol"], ORIGIN_ROOM)
    assert content["body"] == f"mind_carol accepted your contact request. The contact room is {CONTACT_ROOM}."
    assert content["m.relates_to"]["event_id"] == ORIGIN_THREAD
    assert (content["m.mentions"], content[SKIP_MENTIONS_KEY]) == ({}, True)


@pytest.mark.asyncio
async def test_accept_refuses_non_pending(tmp_path: Path) -> None:
    """An accepted, declined, or removed contact cannot be accepted, so a second acceptance opens no second room."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    accepted = await _pending(config, paths, runtime)
    declined = await _pending(config, paths, runtime, target=CAROL)
    removed = await _pending(config, paths, runtime, party="me", target="mind_carol")
    carol = _context(config, paths, "mind_carol", CAROL, runtime)
    await flow.decline_contact(flow.tool_actor(carol, "mind_carol"), declined.id)
    await flow.remove_contact(
        flow.tool_actor(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice"),
        removed.id,
    )

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
    """A stopped agent creates nothing; a failed creation, invite, or join leaves the room and keeps the request.

    The owners' invitations to a room left that way are withdrawn, so none leads to a dead room named like the next.
    """
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    runtime.bots["mind_alice"].running = False
    with _matrix() as matrix, pytest.raises(ValueError, match=r"^mind_alice is not running"):
        await _accept(config, paths, runtime, record.id)
    matrix.create.assert_not_awaited()
    runtime.bots["mind_alice"].running = True

    with _matrix(None) as matrix, pytest.raises(ValueError, match=r"^The contact room could not be created"):
        await _accept(config, paths, runtime, record.id)
    assert matrix.withdrawn() == []

    # The first invitation, Alice's, fails, and may still have reached the server.
    with (
        _matrix(invited=False) as matrix,
        pytest.raises(ValueError, match=r"could not be invited to the contact room"),
    ):
        await _accept(config, paths, runtime, record.id)
    assert matrix.withdrawn() == _owners_withdrawn(runtime, OWNER)
    assert _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    assert _joined(runtime) == set()
    for name in ("mind_alice", "mind_bob"):
        runtime.bots[name].change_local_membership.reset_mock()

    runtime.bots["mind_bob"].change_local_membership.return_value = False
    with _matrix() as matrix, pytest.raises(ValueError, match=r"^mind_bob could not join the contact room"):
        await _accept(config, paths, runtime, record.id)
    # Both owners were invited; the invited agent leaves on its own.
    assert matrix.withdrawn() == _owners_withdrawn(runtime, OWNER, CO_OWNER)
    assert _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    for client in runtime.clients.values():
        client.room_leave.assert_not_awaited()
    assert _kept(config, paths) == set()
    # The last attempt replaced the room of the one before, which stays a removed contact's.
    assert [stored.status for stored in _stored(paths)] == ["pending", "removed"]
    runtime.sent("mind_alice").assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("no ingestion session"), asyncio.CancelledError()])
async def test_a_join_that_fails_unexpectedly_also_leaves_the_room(tmp_path: Path, failure: BaseException) -> None:
    """Any failure while seating the agents, not only a refusal, makes them leave the room before it propagates."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    async def change(_room_id: str, membership: str) -> bool:
        if membership == "join":
            raise failure
        return True

    runtime.bots["mind_bob"].change_local_membership.side_effect = change
    with _matrix() as matrix, pytest.raises(type(failure)):
        await _accept(config, paths, runtime, record.id)

    assert matrix.withdrawn() == _owners_withdrawn(runtime, OWNER, CO_OWNER)
    assert _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    assert _kept(config, paths) == set()
    assert [stored.status for stored in _stored(paths)] == ["pending"]
    runtime.sent("mind_alice").assert_not_awaited()


@pytest.mark.asyncio
async def test_an_acceptance_that_cannot_be_stored_leaves_the_room(tmp_path: Path) -> None:
    """A store write that fails after both agents joined leaves the room, and the request stays pending."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    with (
        _matrix() as matrix,
        # The write naming the room passes, and the one activating the contact fails.
        patch("mindroom.contacts.flow.update_contacts", side_effect=[None, OSError("disk full")]),
        pytest.raises(OSError, match=r"^disk full$"),
    ):
        await _accept(config, paths, runtime, record.id)

    assert matrix.withdrawn() == _owners_withdrawn(runtime, OWNER, CO_OWNER)
    assert _joined(runtime) == _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    assert [stored.status for stored in _stored(paths)] == ["pending"]
    runtime.sent("mind_alice").assert_not_awaited()


@pytest.mark.asyncio
async def test_the_contact_names_its_room_before_anyone_is_invited(tmp_path: Path) -> None:
    """The room refuses every turn while it is set up, and the next acceptance replaces a room that failed."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    admissions = []

    async def invite(_client: object, room_id: str, user_id: str) -> bool:
        admissions.append(
            contact_admission(config, paths, room_id=room_id, agent_name="mind_bob", sender_ids=[user_id]),
        )
        return True

    with _matrix(FAILED_ROOM, invited=False), pytest.raises(ValueError, match=r"could not be invited"):
        await _accept(config, paths, runtime, record.id)
    with _matrix() as matrix:
        matrix.invite.side_effect = invite
        accepted = await _accept(config, paths, runtime, record.id)

    assert admissions
    assert set(admissions) == {_ContactAdmission(None)}
    assert accepted.room_id == CONTACT_ROOM


@pytest.mark.asyncio
async def test_a_retried_acceptance_keeps_the_room_it_replaces_as_a_removed_contacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent whose leave failed stays in the failed attempt's room, which starts no turns there and is left later."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    runtime.bots["mind_alice"].change_local_membership.side_effect = _failing_membership(fails_on="leave")
    runtime.bots["mind_bob"].change_local_membership.side_effect = _failing_membership(fails_on="join")
    with _matrix(FAILED_ROOM), pytest.raises(RuntimeError, match=r"^join failed$"):
        await _accept(config, paths, runtime, record.id)
    for name in ("mind_alice", "mind_bob"):
        runtime.bots[name].change_local_membership.side_effect = None
    with _matrix():
        accepted = await _accept(config, paths, runtime, record.id)

    found = contact_room(config, paths, FAILED_ROOM, "mind_alice")
    assert found is not None
    failed, _side = found
    assert failed.status == "removed"
    assert failed.id != accepted.id
    assert [stored.id for stored in _stored(paths)] == [accepted.id, failed.id]
    admission = contact_admission(config, paths, room_id=FAILED_ROOM, agent_name="mind_alice", sender_ids=[OWNER])
    assert admission == _ContactAdmission(None)
    # The agent accepted the room's invite, which would otherwise keep the room across restarts.
    remember_invited_room(invited_rooms_path(paths, "mind_alice"), FAILED_ROOM)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[CONTACT_ROOM, FAILED_ROOM]),
    )
    assert await _room_lifecycle(config, paths, "mind_alice")._rooms_to_leave() == [FAILED_ROOM]


@pytest.mark.asyncio
async def test_a_leave_that_fails_during_cleanup_still_lets_the_other_agent_leave(tmp_path: Path) -> None:
    """A failed withdrawal or leave keeps no agent in the room and does not hide the failure that stopped the setup."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    runtime.bots["mind_alice"].change_local_membership.side_effect = _failing_membership(fails_on="leave")
    runtime.bots["mind_bob"].change_local_membership.side_effect = _failing_membership(fails_on="join")

    with _matrix() as matrix:
        matrix.kick.side_effect = [RuntimeError("kick failed"), False]
        with pytest.raises(RuntimeError, match=r"^join failed$"):
            await _accept(config, paths, runtime, record.id)

    assert matrix.withdrawn() == _owners_withdrawn(runtime, OWNER, CO_OWNER)
    assert _memberships(runtime, "leave") == {"mind_alice", "mind_bob"}
    assert [stored.status for stored in _stored(paths)] == ["pending"]


@pytest.mark.asyncio
async def test_a_cancellation_during_the_store_write_keeps_the_contact_it_activated(tmp_path: Path) -> None:
    """The store write finishes before a cancellation propagates, so its active contact keeps its agents in the room."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    writing, release = threading.Event(), threading.Event()
    store = flow.update_contacts

    def slow_update(runtime_paths: RuntimePaths, change: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        # The write that activates the contact comes once the room is named.
        if _stored(runtime_paths)[0].room_id is not None:
            writing.set()
            release.wait(timeout=10)
        return store(runtime_paths, change)

    with _matrix() as matrix, patch("mindroom.contacts.flow.update_contacts", side_effect=slow_update):
        accepting = asyncio.create_task(_accept(config, paths, runtime, record.id))
        assert await asyncio.to_thread(writing.wait, 10)
        accepting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await accepting

    assert [(stored.status, stored.room_id) for stored in _stored(paths)] == [("active", CONTACT_ROOM)]
    assert _joined(runtime) == _kept(config, paths) == {"mind_alice", "mind_bob"}
    assert _memberships(runtime, "leave") == set()
    assert matrix.withdrawn() == []


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
    await flow.remove_contact(
        flow.tool_actor(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice"),
        removed.id,
    )

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
async def test_removing_then_restoring_contacts_revives_none_of_the_agents_contacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent that loses contacts leaves its contact rooms and ends those contacts for both sides.

    Its contacts read as removed already before its rooms are reconciled. Once contacts are back, the other agent still
    takes no turns in the room, the listing shows no contact, and the owners can ask for a new one.
    """
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    with _matrix():
        await _accept(config, paths, runtime, record.id)
    without, _ = _setup(tmp_path, contacts=False)
    left: list[str] = []

    async def record_left(
        _client: object,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]],
    ) -> None:
        del leave_room_action
        left.extend(room_ids)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[CONTACT_ROOM]))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", record_left)
    found = contact_room(without, paths, CONTACT_ROOM, "mind_alice")
    assert found is not None
    assert found[0].status == "removed"
    assert await _room_lifecycle(config, paths, "mind_alice")._rooms_to_leave() == []

    await _room_lifecycle(without, paths, "mind_alice").leave_unconfigured_rooms()

    assert left == [CONTACT_ROOM]
    assert [stored.status for stored in _stored(paths)] == ["removed"]
    # With contacts restored, nothing of the old contact comes back.
    admission = contact_admission(config, paths, room_id=CONTACT_ROOM, agent_name="mind_bob", sender_ids=[OWNER])
    assert admission == _ContactAdmission(None)
    listing = yaml.safe_load(flow.list_contacts(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice"))
    assert listing["contacts"] == []
    again = await _pending(config, paths, runtime)
    assert again.id != record.id
    assert [stored.status for stored in _stored(paths)] == ["removed", "pending"]


@pytest.mark.asyncio
async def test_a_request_whose_agent_lost_contacts_cannot_be_accepted(tmp_path: Path) -> None:
    """No contact room is set up for an agent party without contacts, and the request stays pending."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    data = _data()
    data["personal_agents"]["mind"]["users"] = {OWNER: {"contacts": None}}
    without_alices = _load(tmp_path, data, entries=[_personal(), _personal(CO_OWNER), _personal(CAROL)])

    with (
        _matrix() as matrix,
        pytest.raises(
            ValueError,
            match=r"^contacts are not enabled for mind_alice, so the contact room cannot be set up$",
        ),
    ):
        await _accept(without_alices, paths, runtime, record.id)

    matrix.create.assert_not_awaited()
    assert [stored.status for stored in _stored(paths)] == ["pending"]


@pytest.mark.asyncio
async def test_room_cleanup_reads_the_contacts_store_only_for_a_personal_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt contacts store stops a personal agent's room cleanup, not the router's or an authored agent's."""
    config, paths = _setup(tmp_path)
    _contacts_path(paths).parent.mkdir(parents=True, exist_ok=True)
    _contacts_path(paths).write_text("not json", encoding="utf-8")
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=["!stale:localhost"]))

    for name in (ROUTER_AGENT_NAME, "creator"):
        assert await _room_lifecycle(config, paths, name)._rooms_to_leave() == ["!stale:localhost"]
    with pytest.raises(ValueError, match="Contacts file"):
        await _room_lifecycle(config, paths, "mind_alice")._rooms_to_leave()


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

    await flow.remove_contact(
        flow.tool_actor(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice"),
        record.id,
    )
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
    assert accepted == f"Accepted. Contact room: {CONTACT_ROOM}." + _terms(
        "mind_bob",
        "- Never share keys.",
        "matrix_message",
    )


@pytest.mark.asyncio
async def test_accepting_dates_the_decision(tmp_path: Path) -> None:
    """An accepted contact records when it was accepted."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    with _matrix():
        accepted = await _accept(config, paths, runtime, record.id)
    assert accepted.decided_at is not None
    assert record.requested_at is not None
    assert accepted.decided_at >= record.requested_at
    assert _stored(paths) == [accepted]


def _portal(config: Config, paths: RuntimePaths, runtime: _Runtime, user_id: str = CO_OWNER) -> flow.ContactActor:
    """Return the owner acting on the Connections page, with the runtime's bots."""
    return flow.ContactActor(
        owner=user_id,
        agent_name=None,
        config=config,
        runtime_paths=paths,
        agent_bot=runtime.bots.get,
    )


def _thread_posts(send: AsyncMock, room_id: str) -> list[tuple[Any, str, str]]:
    """Return each message sent into ``room_id`` as its client, thread, and body."""
    return [
        (call.args[0], call.args[2]["m.relates_to"]["event_id"], call.args[2]["body"])
        for call in send.await_args_list
        if call.args[1] == room_id
    ]


@pytest.mark.asyncio
async def test_an_owner_accepts_on_the_portal_and_the_request_thread_says_so(tmp_path: Path) -> None:
    """The owner accepts without their personal-room agent, which then answers the request's thread."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    with _matrix(), _delivered("$decided") as send:
        accepted = await flow.accept_contact(_portal(config, paths, runtime), record.id, rules=None, tools=None)

    assert (accepted.status, accepted.room_id) == ("active", CONTACT_ROOM)
    assert _thread_posts(send, BOB_ROOM) == [
        (
            runtime.clients["mind_bob"],
            "$notice",
            f"Accepted on the Connections page. The contact room is {CONTACT_ROOM}.",
        ),
    ]
    runtime.sent("mind_alice").assert_awaited_once()


@pytest.mark.asyncio
async def test_an_owner_declines_on_the_portal_and_both_threads_hear_it(tmp_path: Path) -> None:
    """A decline on the page answers the request's thread and still tells the requesting agent's thread."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    with _delivered("$decided") as send:
        declined = await flow.decline_contact(_portal(config, paths, runtime), record.id)

    assert declined.status == "declined"
    assert _thread_posts(send, BOB_ROOM) == [
        (runtime.clients["mind_bob"], "$notice", "Declined on the Connections page."),
    ]
    assert [thread for _client, thread, _body in _thread_posts(send, ORIGIN_ROOM)] == [ORIGIN_THREAD]


@pytest.mark.asyncio
async def test_a_portal_decision_on_a_request_never_posted_answers_no_thread(tmp_path: Path) -> None:
    """A request whose notice never reached its owner has no thread to answer, and the decision stands."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with _delivered(None), pytest.raises(ValueError, match=r"could not be posted"):
        await _request(config, paths, runtime)
    (record,) = _stored(paths)

    with _matrix(), _delivered("$decided") as send:
        accepted = await flow.accept_contact(_portal(config, paths, runtime), record.id, rules=None, tools=None)

    assert accepted.status == "active"
    assert _thread_posts(send, BOB_ROOM) == []


@pytest.mark.asyncio
async def test_a_decision_without_the_owners_personal_room_says_so(tmp_path: Path) -> None:
    """With the owner's personal room gone, the request has nowhere to be answered, and nothing changes."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    personal_room_record_path(paths, "mind_bob", CO_OWNER).unlink()
    for decide in (
        lambda actor: flow.accept_contact(actor, record.id, rules=None, tools=None),
        lambda actor: flow.decline_contact(actor, record.id),
    ):
        with pytest.raises(ValueError, match=rf"^{CO_OWNER} has no personal room to answer this request in$"):
            await decide(_portal(config, paths, runtime))
    assert [stored.status for stored in _stored(paths)] == ["pending"]


@pytest.mark.asyncio
async def test_updating_a_side_whose_agent_has_no_contacts_is_refused(tmp_path: Path) -> None:
    """A side whose agent lost contacts or left the config cannot be changed."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, target=CAROL)

    def orphan(book: Any) -> None:  # noqa: ANN401
        stored = next(stored for stored in book.contacts if stored.id == record.id)
        stored.requester = stored.requester.model_copy(update={"id": "gone_alice"})

    update_contacts(paths, orphan)
    with pytest.raises(ValueError, match=r"^contacts are not enabled for gone_alice$"):
        await flow.update_contact(_portal(config, paths, runtime, OWNER), record.id, rules="Be brief.", tools=None)
