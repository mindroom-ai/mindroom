"""Contacts with agents of other MindRoom deployments: requests, answers, and the room's membership."""

from __future__ import annotations

import asyncio
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.contacts import federation, flow
from mindroom.contacts.parties import remote_party
from mindroom.contacts.store import (
    ContactBook,
    ContactParty,
    ContactRecord,
    ContactSide,
    ContactStatus,
    contact_room_ids,
    update_contacts,
)
from mindroom.contacts.turns import contact_admission
from mindroom.custom_tools.contacts import ContactsTools
from mindroom.entity_resolution import current_entity_id
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import test_runtime_paths
from tests.contacts.test_accept import _account, _portal, _room_lifecycle
from tests.contacts.test_tools import (
    BOB_ROOM,
    ORIGIN_ROOM,
    ORIGIN_THREAD,
    _context,
    _data,
    _delivered,
    _request,
    _Runtime,
    _runtime,
    _setup,
    _stored,
)
from tests.identity_helpers import persist_entity_accounts
from tests.test_generated_agents import CO_OWNER, OWNER, _load, _personal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

REMOTE = "@mindroom_mind_dana:other.org"
REMOTE_PATTERN = "@mindroom_*:other.org"
REMOTE_ROOM = "!remote:other.org"


def flow_context(config: Config, paths: RuntimePaths, runtime: _Runtime) -> ToolRuntimeContext:
    """Alice asking her agent in the thread where she wants the contact."""
    return _context(config, paths, "mind_alice", OWNER, runtime)


def _remote_setup(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    return _setup(tmp_path, remote_agents=(REMOTE_PATTERN,))


def _store(paths: RuntimePaths, record: ContactRecord) -> None:
    def add(book: ContactBook) -> None:
        book.contacts.append(record)

    update_contacts(paths, add)


def _outgoing(status: ContactStatus = "pending", room_id: str | None = REMOTE_ROOM) -> ContactRecord:
    """Alice's agent's request to another deployment's agent."""
    return ContactRecord(
        id="out",
        status=status,
        requester=ContactParty(kind="agent", id="mind_alice", owner=OWNER),
        target=remote_party(REMOTE),
        reason="Find a slot",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id=ORIGIN_ROOM,
        origin_thread_id=ORIGIN_THREAD,
        room_id=room_id,
    )


def _incoming(status: ContactStatus = "pending") -> ContactRecord:
    """Another deployment's agent's request to Bob's agent."""
    return ContactRecord(
        id="in",
        status=status,
        requester=remote_party(REMOTE),
        target=ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER),
        reason="Find a slot",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id=None,
        origin_thread_id=None,
        room_id=REMOTE_ROOM,
    )


def test_an_agent_keeps_the_room_of_its_pending_request_to_another_deployment(tmp_path: Path) -> None:
    """The invite is the request, so the asking agent stays in its room until the other side answers."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    assert contact_room_ids(config, paths, "mind_alice") == frozenset({REMOTE_ROOM})


def test_an_invited_agent_keeps_no_room_of_a_pending_request(tmp_path: Path) -> None:
    """The asked agent joins only when its owner accepts, so a pending request keeps no room for it."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming())
    assert contact_room_ids(config, paths, "mind_bob") == frozenset()
    assert contact_room_ids(config, paths, "mind_bob", any_status=True) == frozenset({REMOTE_ROOM})


@contextmanager
def _matrix(*, room_id: str | None = REMOTE_ROOM, invited: bool = True) -> Iterator[dict[str, AsyncMock]]:
    """Stand in for the room the requesting agent creates, the invites both sides send, and the room's members."""
    invite = AsyncMock(return_value=invited)
    with (
        patch("mindroom.contacts.flow.create_room", new=AsyncMock(return_value=room_id)) as create,
        patch("mindroom.contacts.flow.invite_to_room", new=invite),
        patch("mindroom.contacts.federation.invite_to_room", new=invite),
        patch("mindroom.contacts.flow.kick_from_room", new=AsyncMock(return_value=True)) as kick,
        patch(
            "mindroom.contacts.federation.ensure_managed_room_power_levels",
            new=AsyncMock(return_value=True),
        ) as power,
        patch(
            "mindroom.contacts.flow.get_room_members",
            new=AsyncMock(return_value={"@mindroom_mind_alice:localhost", "@mindroom_mind_bob:localhost", REMOTE}),
        ),
    ):
        yield {"create": create, "invite": invite, "kick": kick, "power": power}


@pytest.mark.asyncio
async def test_a_request_to_another_deployment_is_an_invite_to_its_room(tmp_path: Path) -> None:
    """The asking agent opens the contact room and invites the other agent with the reason, giving it no power yet.

    Until the other agent joins, the asking agent must be able to withdraw its invite, which needs more power.
    """
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _matrix() as matrix:
        record = await _request(config, paths, runtime, target=REMOTE, reason="Find a slot")

    client = runtime.clients["mind_alice"]
    matrix["create"].assert_awaited_once_with(
        client,
        f"Alice's Mind ↔ {REMOTE}",
        initial_state=flow._CONTACT_ROOM_STATE,
    )
    matrix["invite"].assert_awaited_once_with(client, REMOTE_ROOM, REMOTE, reason="Find a slot")
    assert record.status == "pending"
    assert record.target == remote_party(REMOTE)
    assert record.room_id == REMOTE_ROOM
    assert _stored(paths) == [record]


@pytest.mark.asyncio
async def test_asking_again_returns_the_pending_request_without_another_room(tmp_path: Path) -> None:
    """The other side already holds the invite, so a repeat neither opens a room nor invites again."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _matrix():
        first = await _request(config, paths, runtime, target=REMOTE)
    with _matrix() as matrix:
        again = await _request(config, paths, runtime, target=REMOTE)
    assert again == first
    matrix["create"].assert_not_awaited()
    matrix["invite"].assert_not_awaited()


@pytest.mark.asyncio
async def test_only_an_agent_asks_another_deployment(tmp_path: Path) -> None:
    """A person who asks has no agent to create the room, so party="me" reaches only this deployment."""
    config, paths = _remote_setup(tmp_path)
    with _matrix(), pytest.raises(ValueError, match=r"^Only your agent can contact an agent of another MindRoom"):
        await _request(config, paths, _runtime(), target=REMOTE, party="me")
    assert _stored(paths) == []


@pytest.mark.asyncio
async def test_an_unmatched_matrix_id_points_to_remote_agents(tmp_path: Path) -> None:
    """Another deployment's agent that no pattern names looks like a person here, and the error says how to reach it."""
    config, paths = _setup(tmp_path)
    with pytest.raises(
        ValueError,
        match=rf"^{re.escape(REMOTE)} has no personal agent here, .*contacts\.remote_agents",
    ):
        await _request(config, paths, _runtime(), target=REMOTE)


@pytest.mark.asyncio
async def test_an_undelivered_invite_leaves_the_room_and_keeps_no_request(tmp_path: Path) -> None:
    """A request the other side never got is removed, so asking again opens a new room."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with (
        _matrix(invited=False),
        pytest.raises(
            ValueError,
            match=f"^The request to {re.escape(REMOTE)} could not be delivered; ask again to retry",
        ),
    ):
        await _request(config, paths, runtime, target=REMOTE)
    runtime.bots["mind_alice"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")
    assert [record.status for record in _stored(paths)] == ["removed"]
    with _matrix(room_id="!second:localhost"):
        assert (await _request(config, paths, runtime, target=REMOTE)).room_id == "!second:localhost"


@pytest.mark.asyncio
async def test_a_room_that_cannot_be_created_stores_nothing(tmp_path: Path) -> None:
    """Without a room there is no request to keep."""
    config, paths = _remote_setup(tmp_path)
    with (
        _matrix(room_id=None),
        pytest.raises(ValueError, match=r"^The contact room could not be created; ask again to retry"),
    ):
        await _request(config, paths, _runtime(), target=REMOTE)
    assert _stored(paths) == []


@pytest.mark.asyncio
async def test_withdrawing_a_request_takes_back_the_invite_before_leaving(tmp_path: Path) -> None:
    """Removing a pending request withdraws the other agent's invite, so it cannot join a room nobody is left in."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _matrix():
        record = await _request(config, paths, runtime, target=REMOTE)
    actor = flow.tool_actor(flow_context(config, paths, runtime), "mind_alice")
    with _matrix() as matrix:
        removed = await flow.remove_contact(actor, record.id)
    assert removed.status == "removed"
    matrix["kick"].assert_awaited_once_with(runtime.clients["mind_alice"], REMOTE_ROOM, REMOTE)
    runtime.bots["mind_alice"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")


@pytest.mark.asyncio
async def test_the_requester_may_remove_a_request_another_deployment_declined(tmp_path: Path) -> None:
    """The owner who declined is out of reach, so the asking owner clears the decline here, and may ask again."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing(status="declined"))
    runtime = _runtime()
    with pytest.raises(
        ValueError,
        match=f"^{re.escape(REMOTE)} declined contact out; remove it with remove_contact to ask again",
    ):
        await _request(config, paths, runtime, target=REMOTE)
    actor = flow.tool_actor(flow_context(config, paths, runtime), "mind_alice")
    with _matrix():
        assert (await flow.remove_contact(actor, "out")).status == "removed"
        assert (await _request(config, paths, runtime, target=REMOTE)).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_only_the_other_deployment_answers_a_request_sent_there(tmp_path: Path, decision: str) -> None:
    """The asking owner cannot answer their own request, and the refusal names who answers it, not a missing owner."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    actor = flow.tool_actor(flow_context(config, paths, _runtime()), "mind_alice")
    answer = (
        flow.accept_contact(actor, "out", rules=None, tools=None)
        if decision == "accept"
        else flow.decline_contact(actor, "out")
    )
    refusal = rf"^Only the owner of {re.escape(REMOTE)} on another MindRoom deployment may {decision} this contact$"
    with pytest.raises(ValueError, match=refusal):
        await answer


@pytest.mark.asyncio
async def test_the_toolkit_says_who_was_asked(tmp_path: Path) -> None:
    """The result names the other agent by ID, since its owner is unknown here."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    context = flow_context(config, paths, runtime)
    with _matrix(), tool_runtime_context(context):
        result = await ContactsTools("mind_alice").request_contact(target=REMOTE, reason="Find a slot")
    record = _stored(paths)[0]
    assert result.startswith(
        f"Asked {REMOTE}, an agent of another MindRoom deployment, for contact {record.id}; "
        "you are told in this thread when its owner accepts or declines.",
    )


async def _receive(
    config: Config,
    paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], object],
    *,
    room_id: str,
    inviter: str,
    reason: object,
) -> None:
    """Hand an invite to contacts as the room lifecycle does: store the request, then answer it."""
    answer = await federation.store_contact_invite(
        config,
        paths,
        agent_name,
        room_id=room_id,
        inviter=inviter,
        reason=reason,
    )
    if answer is not None:
        await federation.answer_contact_invite(config, paths, agent_name, agent_bot, answer)


async def _invited(
    config: Config,
    paths: RuntimePaths,
    runtime: _Runtime,
    *,
    room_id: str = REMOTE_ROOM,
    inviter: str = REMOTE,
    reason: object = "Find a slot",
) -> None:
    """Bob's agent receives another deployment's invite."""
    await _receive(
        config,
        paths,
        "mind_bob",
        runtime.bots.get,
        room_id=room_id,
        inviter=inviter,
        reason=reason,
    )


def _rejected(runtime: _Runtime, room_id: str = REMOTE_ROOM, agent_name: str = "mind_bob") -> bool:
    """Return whether the agent rejected its invite on the homeserver.

    The bot's membership commands read an invite as already left and would send nothing, so only a direct leave
    rejects it.
    """
    leaves = [call.args for call in runtime.clients[agent_name].room_leave.await_args_list]
    commands = [call.args for call in runtime.bots[agent_name].change_local_membership.await_args_list]
    return leaves == [(room_id,)] and (room_id, "leave") not in commands


def test_only_a_matching_sender_from_elsewhere_invites_with_a_request(tmp_path: Path) -> None:
    """An invite is a request only from an agent the patterns name that this deployment does not run."""
    config, paths = _remote_setup(tmp_path)
    assert federation.is_contact_invite(config, paths, "mind_bob", REMOTE)
    assert not federation.is_contact_invite(config, paths, "mind_bob", "@dana:other.org")
    assert not federation.is_contact_invite(config, paths, "mind_bob", _account(paths, "mind_alice"))
    plain_config, plain_paths = _setup(tmp_path / "plain")
    assert not federation.is_contact_invite(plain_config, plain_paths, "mind_bob", REMOTE)


@pytest.mark.asyncio
async def test_an_invite_from_another_deployment_asks_the_owner_in_their_personal_room(tmp_path: Path) -> None:
    """The request is stored pending with its room, and Bob's personal-room agent posts it for him to answer."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _delivered() as send:
        await _invited(config, paths, runtime)

    [record] = _stored(paths)
    assert record.status == "pending"
    assert record.requester == remote_party(REMOTE)
    assert record.target == ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER)
    assert (record.room_id, record.origin_room_id, record.notice_event_id) == (REMOTE_ROOM, None, "$notice")
    assert record.reason == "Find a slot"
    assert send.await_args.args[1] == BOB_ROOM
    body = send.await_args.args[2]["body"]
    assert body.startswith(f"{REMOTE}, an agent of another MindRoom deployment asks to contact your agent")
    assert "Reason: Find a slot" in body
    assert not _rejected(runtime)


@pytest.mark.asyncio
async def test_the_same_invite_seen_again_asks_nobody_twice(tmp_path: Path) -> None:
    """An invite delivered again is the same request, so nothing new is stored or posted."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _delivered():
        await _invited(config, paths, runtime)
    with _delivered() as send:
        await _invited(config, paths, runtime)
    assert len(_stored(paths)) == 1
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_request_whose_notice_failed_still_waits(tmp_path: Path) -> None:
    """A notice that cannot be posted leaves the request pending for the Contacts page and list_contacts."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _delivered(None):
        await _invited(config, paths, runtime)
    [record] = _stored(paths)
    assert (record.status, record.notice_event_id) == ("pending", None)
    assert not _rejected(runtime)


@pytest.mark.parametrize("stored", ["declined", "active"])
@pytest.mark.asyncio
async def test_a_second_invite_of_a_known_pair_is_rejected(tmp_path: Path, stored: ContactStatus) -> None:
    """A decline blocks the pair until Bob removes it, and a pair has one contact, so a new room's invite is rejected."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming(status=stored))
    runtime = _runtime()
    with _delivered() as send:
        await _invited(config, paths, runtime, room_id="!second:other.org")
    assert [record.id for record in _stored(paths)] == ["in"]
    assert _rejected(runtime, "!second:other.org")
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_new_invite_replaces_a_request_whose_withdrawal_was_never_seen(tmp_path: Path) -> None:
    """The other side keeps one pending request per pair, so its new room's invite means the earlier one is gone."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with _delivered() as send:
        await _invited(config, paths, runtime, room_id="!second:other.org")
    old, new = _stored(paths)
    assert (old.id, old.status) == ("in", "removed")
    assert (new.room_id, new.status) == ("!second:other.org", "pending")
    assert not _rejected(runtime, "!second:other.org")
    send.assert_awaited_once()


@pytest.mark.parametrize("reason", [None, "  ", 7])
@pytest.mark.asyncio
async def test_an_invite_without_a_reason_is_rejected(tmp_path: Path, reason: object) -> None:
    """A request says why it asks, so an invite without a reason asks nothing and is rejected."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    with _delivered():
        await _invited(config, paths, runtime, reason=reason)
    assert _stored(paths) == []
    assert _rejected(runtime)


@pytest.mark.asyncio
async def test_a_request_nobody_could_answer_is_rejected(tmp_path: Path) -> None:
    """Bob's personal-room agent answers requests, so without contacts there nobody could accept, and the invite is rejected."""
    config, paths = _setup(tmp_path, work=True, remote_agents=(REMOTE_PATTERN,))
    config.agents["mind_bob"].contacts = None
    runtime = _runtime()
    with _delivered():
        await _receive(
            config,
            paths,
            "work_bob",
            runtime.bots.get,
            room_id=REMOTE_ROOM,
            inviter=REMOTE,
            reason="Find a slot",
        )
    assert _stored(paths) == []
    assert _rejected(runtime, agent_name="work_bob")


@pytest.mark.asyncio
async def test_the_room_lifecycle_hands_a_request_to_contacts(tmp_path: Path) -> None:
    """A request never reaches the generic invite handling, so no invite policy can join its room."""
    config, paths = _remote_setup(tmp_path)
    runtime = _runtime()
    lifecycle = _room_lifecycle(config, paths, "mind_bob", runtime.bots.get)
    event = nio.InviteMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "sender": REMOTE,
            "state_key": _account(paths, "mind_bob"),
            "content": {"membership": "invite", "reason": "Find a slot"},
        },
    )
    with _delivered() as send:
        await lifecycle.on_invite(nio.MatrixInvitedRoom(REMOTE_ROOM, _account(paths, "mind_bob")), event)
        # Stored before the handling returns, since nothing hands the invite to it again after a restart.
        assert [record.room_id for record in _stored(paths)] == [REMOTE_ROOM]
        await wait_for_background_tasks(owner=lifecycle.deps.runtime)
    send.assert_awaited_once()
    assert lifecycle._pending_room_invites == {}


def _bob(config: Config, paths: RuntimePaths, runtime: _Runtime) -> flow.ContactActor:
    """Bob answering through his personal-room agent."""
    return flow.tool_actor(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob")


def _incoming_with_notice() -> ContactRecord:
    return _incoming().model_copy(update={"notice_event_id": "$notice"})


@pytest.mark.asyncio
async def test_accepting_another_deployments_request_joins_its_room_and_invites_the_owner(tmp_path: Path) -> None:
    """Joining is the acceptance the other side sees; then Bob is invited to read along, and his terms are stored."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with _matrix() as matrix:
        accepted = await flow.accept_contact(_bob(config, paths, runtime), "in", rules="Only free or busy.", tools=None)
    runtime.bots["mind_bob"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "join")
    matrix["create"].assert_not_awaited()
    matrix["invite"].assert_awaited_once_with(runtime.clients["mind_bob"], REMOTE_ROOM, CO_OWNER)
    assert (accepted.status, accepted.room_id) == ("active", REMOTE_ROOM)
    assert accepted.target_side.rules == "Only free or busy."
    assert _stored(paths) == [accepted]


@pytest.mark.asyncio
async def test_a_withdrawn_request_cannot_be_joined_and_stays_pending(tmp_path: Path) -> None:
    """A room the agent cannot join is no acceptance, so the request waits to be accepted again or declined."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    runtime.bots["mind_bob"].change_local_membership = AsyncMock(return_value=False)
    with _matrix() as matrix, pytest.raises(ValueError, match=r"^mind_bob could not join the contact room"):
        await flow.accept_contact(_bob(config, paths, runtime), "in", rules=None, tools=None)
    matrix["invite"].assert_not_awaited()
    assert [record.status for record in _stored(paths)] == ["pending"]


@pytest.mark.asyncio
async def test_declining_another_deployments_request_rejects_its_invite(tmp_path: Path) -> None:
    """The rejected invite is the decline the other side sees, and nobody here asked from a thread."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with _delivered() as send:
        declined = await flow.decline_contact(_bob(config, paths, runtime), "in")
    assert declined.status == "declined"
    assert _rejected(runtime)
    send.assert_not_awaited()
    for bot in runtime.bots.values():
        bot.hook_context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_removing_an_active_remote_contact_withdraws_the_unjoined_owner_and_leaves(tmp_path: Path) -> None:
    """Leaving ends the contact for the other side, and Bob's invitation does not lead to a room his agent left."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming(status="active"))
    runtime = _runtime()
    with _matrix() as matrix:
        await flow.remove_contact(_bob(config, paths, runtime), "in")
    matrix["kick"].assert_awaited_once_with(runtime.clients["mind_bob"], REMOTE_ROOM, CO_OWNER)
    runtime.bots["mind_bob"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")


@pytest.mark.asyncio
async def test_removing_a_pending_remote_request_only_rejects_its_invite(tmp_path: Path) -> None:
    """Bob was never invited to a room his agent never joined, so removing the request only rejects the invite."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with _matrix() as matrix:
        await flow.remove_contact(_bob(config, paths, runtime), "in")
    matrix["kick"].assert_not_awaited()
    assert _rejected(runtime)


@pytest.mark.asyncio
async def test_a_message_while_the_acceptance_joins_is_admitted(tmp_path: Path) -> None:
    """The other side reads the join as the acceptance and may write at once, so the contact is active before it."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    admitted: list[str | None] = []

    async def join_while_the_other_agent_writes(_room_id: str, _membership: str) -> bool:
        admission = contact_admission(config, paths, room_id=REMOTE_ROOM, agent_name="mind_bob", sender_ids=[REMOTE])
        admitted.append(None if admission is None else admission.requester_id)
        return True

    runtime.bots["mind_bob"].change_local_membership = AsyncMock(side_effect=join_while_the_other_agent_writes)
    with _matrix(), _delivered():
        await flow.accept_contact(_bob(config, paths, runtime), "in", rules=None, tools=None)
    assert admitted == [CO_OWNER]


@pytest.mark.asyncio
async def test_an_acceptance_cancelled_while_it_activates_puts_the_request_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation during the activation write still finds the contact active, so it is put back before any join."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    writing, release = threading.Event(), threading.Event()

    def slow_first_write(paths_: RuntimePaths, change: Callable[[ContactBook], object]) -> object:
        result = update_contacts(paths_, change)
        if not writing.is_set():
            writing.set()
            release.wait(5)
        return result

    monkeypatch.setattr("mindroom.contacts.flow.update_contacts", slow_first_write)
    with _matrix():
        accepting = asyncio.create_task(flow.accept_contact(_bob(config, paths, runtime), "in", rules=None, tools=None))
        await asyncio.to_thread(writing.wait, 5)
        accepting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await accepting
    assert _statuses(paths) == ["pending"]
    runtime.bots["mind_bob"].change_local_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_putting_a_request_back_keeps_a_change_made_while_it_joined(tmp_path: Path) -> None:
    """The owner may change their side while the join settles, so a failed join keeps that change, not the old side."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()

    async def failed_join_after_an_update(_room_id: str, membership: str) -> bool:
        if membership != "join":
            return True

        def update(book: ContactBook) -> None:
            book.contacts[0].target_side.rules = "Changed meanwhile."

        update_contacts(paths, update)
        return False

    runtime.bots["mind_bob"].change_local_membership = AsyncMock(side_effect=failed_join_after_an_update)
    with _matrix(), pytest.raises(ValueError, match=r"could not join the contact room"):
        await flow.accept_contact(_bob(config, paths, runtime), "in", rules="Accepted rules.", tools=None)
    [stored] = _stored(paths)
    assert stored.status == "pending"
    assert stored.target_side.rules == "Changed meanwhile."


@pytest.mark.asyncio
async def test_a_cancelled_acceptance_puts_the_request_back_and_leaves(tmp_path: Path) -> None:
    """A join cut short may still have landed, so the agent leaves again and the request waits as it did."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()

    async def cancelled_join(_room_id: str, membership: str) -> bool:
        if membership == "join":
            raise asyncio.CancelledError
        return True

    runtime.bots["mind_bob"].change_local_membership = AsyncMock(side_effect=cancelled_join)
    with _matrix() as matrix, pytest.raises(asyncio.CancelledError):
        await flow.accept_contact(_bob(config, paths, runtime), "in", rules="Only scheduling.", tools=None)
    [stored] = _stored(paths)
    assert (stored.status, stored.decided_at, stored.target_side) == ("pending", None, ContactSide())
    assert [call.args for call in runtime.bots["mind_bob"].change_local_membership.await_args_list] == [
        (REMOTE_ROOM, "join"),
        (REMOTE_ROOM, "leave"),
    ]
    matrix["invite"].assert_not_awaited()


@pytest.mark.asyncio
async def test_accepting_on_the_page_answers_the_request_thread(tmp_path: Path) -> None:
    """Bob's personal-room agent says in the request's thread that he accepted on the Connections page."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with _matrix(), _delivered() as send:
        await flow.accept_contact(_portal(config, paths, runtime), "in", rules=None, tools=None)
    [post] = [call for call in send.await_args_list if call.args[1] == BOB_ROOM]
    assert post.args[2]["body"] == f"Accepted on the Connections page. The contact room is {REMOTE_ROOM}."


async def _member(
    config: Config,
    paths: RuntimePaths,
    runtime: _Runtime,
    agent_name: str,
    *,
    user_id: str = REMOTE,
    membership: str,
    room_id: str = REMOTE_ROOM,
) -> None:
    """One membership change in the contact room, as the agent's bot sees it."""
    await federation.contact_member_event(
        config,
        paths,
        agent_name,
        runtime.bots.get,
        room_id=room_id,
        user_id=user_id,
        membership=membership,
    )


def _statuses(paths: RuntimePaths) -> list[str]:
    return [record.status for record in _stored(paths)]


@pytest.mark.asyncio
async def test_the_other_agent_joining_is_the_acceptance(tmp_path: Path) -> None:
    """Alice's agent activates the contact, invites Alice, and continues the task in the thread she asked from."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    runtime = _runtime()
    with _matrix() as matrix:
        await _member(config, paths, runtime, "mind_alice", membership="join")
        await _member(config, paths, runtime, "mind_alice", membership="join")
    assert _statuses(paths) == ["active"]
    assert _stored(paths)[0].decided_at is not None
    # Once joined, the other agent gets the power to invite and withdraw its own owner.
    matrix["power"].assert_awaited_once_with(runtime.clients["mind_alice"], REMOTE_ROOM, [REMOTE])
    matrix["invite"].assert_awaited_once_with(runtime.clients["mind_alice"], REMOTE_ROOM, OWNER)
    kickoff = runtime.sent("mind_alice")
    kickoff.assert_awaited_once()
    assert kickoff.await_args.args[0] == ORIGIN_ROOM
    assert kickoff.await_args.args[1].startswith(f"@mind_alice {REMOTE} accepted your contact request.")
    assert kickoff.await_args.args[2] == ORIGIN_THREAD


@pytest.mark.asyncio
async def test_the_other_agent_rejecting_the_invite_is_the_decline(tmp_path: Path) -> None:
    """Alice's agent records the decline, says so in her thread, and leaves the room nobody else joined."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    runtime = _runtime()
    with _matrix(), _delivered() as send:
        await _member(config, paths, runtime, "mind_alice", membership="leave")
    assert _statuses(paths) == ["declined"]
    [post] = [call for call in send.await_args_list if call.args[1] == ORIGIN_ROOM]
    assert post.args[2]["body"] == f"{REMOTE} declined contact out; remove it with remove_contact to ask again."
    runtime.bots["mind_alice"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")


@pytest.mark.parametrize("membership", ["leave", "ban"])
@pytest.mark.asyncio
async def test_the_other_agent_leaving_ends_the_contact_here(tmp_path: Path, membership: str) -> None:
    """Either side's agent leaving ends the contact for both: this side withdraws its owner's unjoined invite and leaves."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming(status="active"))
    runtime = _runtime()
    with _matrix() as matrix:
        await _member(config, paths, runtime, "mind_bob", membership=membership)
    assert _statuses(paths) == ["removed"]
    matrix["kick"].assert_awaited_once_with(runtime.clients["mind_bob"], REMOTE_ROOM, CO_OWNER)
    runtime.bots["mind_bob"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")


@pytest.mark.asyncio
@pytest.mark.parametrize("contact", ["active", "sent"])
async def test_being_removed_from_the_room_ends_the_contact(tmp_path: Path, contact: str) -> None:
    """An agent removed from the room, as a homeserver administrator can do, can no longer speak there, so it ends."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing(status="active") if contact == "active" else _outgoing())
    runtime = _runtime()
    with _matrix():
        await federation.contact_room_left(config, paths, "mind_alice", runtime.bots.get, room_id=REMOTE_ROOM)
    assert _statuses(paths) == ["removed"]
    runtime.bots["mind_alice"].change_local_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaving_a_request_room_after_a_failed_acceptance_keeps_the_request(tmp_path: Path) -> None:
    """A failed acceptance leaves the room it joined again, and the request still waits for its owner."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with _matrix():
        await federation.contact_room_left(config, paths, "mind_bob", runtime.bots.get, room_id=REMOTE_ROOM)
    assert _statuses(paths) == ["pending"]


@pytest.mark.asyncio
async def test_a_departure_reaches_contacts(tmp_path: Path) -> None:
    """The room lifecycle hands this agent's own departure to contacts, which no member event of the room carries."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing(status="active"))
    runtime = _runtime()
    lifecycle = _room_lifecycle(config, paths, "mind_alice", runtime.bots.get)
    with _matrix():
        await lifecycle.on_contact_room_left(REMOTE_ROOM)
    assert _statuses(paths) == ["removed"]


@pytest.mark.asyncio
async def test_other_membership_changes_change_nothing(tmp_path: Path) -> None:
    """Owners coming and going, the agent's own leave, and other rooms leave the contact as it is."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing(status="active"))
    runtime = _runtime()
    own = _account(paths, "mind_alice")
    with _matrix() as matrix:
        await _member(config, paths, runtime, "mind_alice", user_id=OWNER, membership="leave")
        await _member(config, paths, runtime, "mind_alice", user_id=own, membership="leave")
        await _member(config, paths, runtime, "mind_alice", membership="leave", room_id="!other:localhost")
        await _member(config, paths, runtime, "mind_alice", membership="join")
    assert _statuses(paths) == ["active"]
    matrix["invite"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_room_lifecycle_hands_member_events_to_contacts(tmp_path: Path) -> None:
    """The bot passes each member event of its rooms on, so a join it never saw live still activates the contact."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    runtime = _runtime()
    lifecycle = _room_lifecycle(config, paths, "mind_alice", runtime.bots.get)
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": "$join",
            "sender": REMOTE,
            "state_key": REMOTE,
            "origin_server_ts": 1,
            "content": {"membership": "join"},
        },
    )
    with _matrix():
        await lifecycle.on_contact_member(REMOTE_ROOM, event)
    assert _statuses(paths) == ["active"]


@dataclass
class _Deployment:
    """One MindRoom deployment with one person's personal agent, and its running bots."""

    config: Config
    paths: RuntimePaths
    runtime: _Runtime
    agent_name: str
    agent_id: str


def _deployment(tmp_path: Path, user_id: str, *, room_id: str) -> _Deployment:
    """A deployment where only ``user_id`` has a personal agent, which may contact any other deployment's agent."""
    data = _data(remote_agents=("@mindroom_*",))
    config = _load(tmp_path, data, entries=[_personal(user_id)])
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    agent_name = f"mind_{user_id.removeprefix('@').partition(':')[0]}"
    record = PersonalRoomRecord(user_id=user_id, alias=f"personal-{agent_name}", source_room_id="!lobby:localhost")
    write_personal_room(
        personal_room_record_path(paths, agent_name, user_id),
        record.model_copy(update={"room_id": room_id}),
    )
    agent_id = current_entity_id(agent_name, paths).full_id
    runtime = _runtime()
    runtime.clients[agent_name].user_id = agent_id
    return _Deployment(config, paths, runtime, agent_name, agent_id)


@pytest.mark.asyncio
async def test_two_deployments_ask_accept_talk_and_end_a_contact(tmp_path: Path) -> None:
    """Alice's and Bob's agents run on different deployments of one homeserver and meet only through the room.

    Each step on one side reaches the other only as Matrix membership: the invite asks, the join accepts, and a leave
    ends the contact for both.
    """
    alice = _deployment(tmp_path / "a", OWNER, room_id=ORIGIN_ROOM)
    bob = _deployment(tmp_path / "b", CO_OWNER, room_id=BOB_ROOM)
    joined: set[str] = set()

    async def invite(_client: object, room_id: str, user_id: str, *, reason: str | None = None) -> bool:
        if user_id == bob.agent_id:
            await _receive(
                bob.config,
                bob.paths,
                bob.agent_name,
                bob.runtime.bots.get,
                room_id=room_id,
                inviter=alice.agent_id,
                reason=reason,
            )
        return True

    def membership(side: _Deployment, other: _Deployment) -> AsyncMock:
        async def change(room_id: str, target: str) -> bool:
            (joined.add if target == "join" else joined.discard)(side.agent_id)
            await federation.contact_member_event(
                other.config,
                other.paths,
                other.agent_name,
                other.runtime.bots.get,
                room_id=room_id,
                user_id=side.agent_id,
                membership=target,
            )
            return True

        return AsyncMock(side_effect=change)

    alice.runtime.bots["mind_alice"].change_local_membership = membership(alice, bob)
    bob.runtime.bots["mind_bob"].change_local_membership = membership(bob, alice)
    invited = AsyncMock(side_effect=invite)
    with (
        patch("mindroom.contacts.flow.create_room", new=AsyncMock(return_value=REMOTE_ROOM)),
        patch("mindroom.contacts.flow.invite_to_room", new=invited),
        patch("mindroom.contacts.federation.invite_to_room", new=invited),
        patch("mindroom.contacts.flow.kick_from_room", new=AsyncMock(return_value=True)),
        patch("mindroom.contacts.flow.get_room_members", new=AsyncMock(side_effect=lambda *_: set(joined))),
        _delivered(),
    ):
        joined.add(alice.agent_id)
        asked = await _request(alice.config, alice.paths, alice.runtime, target=bob.agent_id, reason="Find a slot")
        [incoming] = _stored(bob.paths)
        assert (asked.status, incoming.status, incoming.requester.id) == ("pending", "pending", alice.agent_id)

        await flow.accept_contact(_bob(bob.config, bob.paths, bob.runtime), incoming.id, rules=None, tools=None)
        assert [record.status for record in _stored(alice.paths) + _stored(bob.paths)] == ["active", "active"]
        assert joined == {alice.agent_id, bob.agent_id}
        assert contact_room_ids(alice.config, alice.paths, "mind_alice") == frozenset({REMOTE_ROOM})
        assert contact_room_ids(bob.config, bob.paths, "mind_bob") == frozenset({REMOTE_ROOM})
        alice.runtime.sent("mind_alice").assert_awaited_once()

        await flow.remove_contact(_bob(bob.config, bob.paths, bob.runtime), incoming.id)

    assert [record.status for record in _stored(alice.paths) + _stored(bob.paths)] == ["removed", "removed"]
    assert joined == set()
    assert contact_room_ids(alice.config, alice.paths, "mind_alice") == frozenset()
    assert contact_room_ids(bob.config, bob.paths, "mind_bob") == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["none", "contacts_removed", "remote_agents_emptied"])
async def test_invite_reconciliation_never_joins_a_request_room(tmp_path: Path, drift: str) -> None:
    """A request waits for its owner, so even accept_invites: true never joins its room at a reload or start.

    That holds after the config stops naming its inviter too, since the other side would read the join as acceptance.
    """
    config, paths = _remote_setup(tmp_path)
    if drift == "contacts_removed":
        config, _ = _setup(tmp_path, contacts=False)
    elif drift == "remote_agents_emptied":
        config, _ = _setup(tmp_path)
    assert config.agents["mind_bob"].accept_invites is True
    _store(paths, _incoming_with_notice())
    lifecycle = _room_lifecycle(config, paths, "mind_bob")
    invited = nio.MatrixInvitedRoom(REMOTE_ROOM, _account(paths, "mind_bob"))
    invited.inviter = REMOTE
    lifecycle.deps.runtime.client.invited_rooms = {REMOTE_ROOM: invited}
    lifecycle.deps.runtime.client.rooms = {}
    await lifecycle.reconcile_pending_invites()
    lifecycle.deps.change_membership.assert_not_awaited()
    lifecycle.deps.on_room_joined.assert_not_awaited()
    assert lifecycle._pending_room_invites == {}


@pytest.mark.parametrize("settled", ["declined", "removed"])
@pytest.mark.asyncio
async def test_an_invite_to_a_settled_request_room_is_rejected(tmp_path: Path, settled: ContactStatus) -> None:
    """A room whose request was answered or withdrawn is no new request, so a fresh invite to it is rejected."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming(status=settled))
    runtime = _runtime()
    with _delivered() as send:
        await _invited(config, paths, runtime)
    assert _statuses(paths) == [settled]
    assert _rejected(runtime)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepting_a_request_whose_requester_left_fails_and_leaves(tmp_path: Path) -> None:
    """An invite can outlive its request when its withdrawal failed, so a room the asking agent left is no contact."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    with (
        _matrix() as matrix,
        patch("mindroom.contacts.flow.get_room_members", new=AsyncMock(return_value={_account(paths, "mind_bob")})),
        pytest.raises(ValueError, match=r"^mind_bob could not join the contact room; the request may have been"),
    ):
        await flow.accept_contact(_bob(config, paths, runtime), "in", rules=None, tools=None)
    assert [call.args for call in runtime.bots["mind_bob"].change_local_membership.await_args_list] == [
        (REMOTE_ROOM, "join"),
        (REMOTE_ROOM, "leave"),
    ]
    matrix["invite"].assert_not_awaited()
    assert _statuses(paths) == ["pending"]


@pytest.mark.asyncio
async def test_a_start_catches_up_on_an_acceptance_it_never_saw(tmp_path: Path) -> None:
    """A join that reached no live event still activates the request, read from the room's members at start."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    runtime = _runtime()
    with (
        _matrix() as matrix,
        patch("mindroom.contacts.federation.get_room_membership", new=AsyncMock(return_value="join")),
    ):
        await federation.reconcile_remote_contacts(config, paths, "mind_alice", runtime.bots.get)
    assert _statuses(paths) == ["active"]
    matrix["invite"].assert_awaited_once_with(runtime.clients["mind_alice"], REMOTE_ROOM, OWNER)
    runtime.sent("mind_alice").assert_awaited_once()


@pytest.mark.asyncio
async def test_a_start_catches_up_on_a_leave_it_never_saw(tmp_path: Path) -> None:
    """The other agent gone from an active contact's room ends the contact here too, read at start."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming(status="active"))
    runtime = _runtime()
    with _matrix(), patch("mindroom.contacts.federation.get_room_membership", new=AsyncMock(return_value="leave")):
        await federation.reconcile_remote_contacts(config, paths, "mind_bob", runtime.bots.get)
    assert _statuses(paths) == ["removed"]
    runtime.bots["mind_bob"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")


@pytest.mark.asyncio
async def test_a_start_catches_up_on_a_decline_it_never_saw(tmp_path: Path) -> None:
    """A rejected invite that reached no live event still declines the request, read from the room's state at start."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    runtime = _runtime()
    read = AsyncMock(return_value="leave")
    with _matrix(), _delivered() as send, patch("mindroom.contacts.federation.get_room_membership", new=read):
        await federation.reconcile_remote_contacts(config, paths, "mind_alice", runtime.bots.get)
    assert _statuses(paths) == ["declined"]
    read.assert_awaited_once_with(runtime.clients["mind_alice"], REMOTE_ROOM, REMOTE)
    [post] = [call for call in send.await_args_list if call.args[1] == ORIGIN_ROOM]
    assert post.args[2]["body"] == f"{REMOTE} declined contact out; remove it with remove_contact to ask again."
    runtime.bots["mind_alice"].change_local_membership.assert_awaited_once_with(REMOTE_ROOM, "leave")


@pytest.mark.asyncio
async def test_a_start_changes_nothing_when_the_room_is_as_stored_or_unreadable(tmp_path: Path) -> None:
    """A request whose invite is still pending, an active contact the other agent is in, and unreadable state stay."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    _store(paths, _incoming(status="active").model_copy(update={"room_id": "!active:other.org"}))
    runtime = _runtime()
    membership = {REMOTE_ROOM: "invite", "!active:other.org": "join"}
    for read in (
        AsyncMock(side_effect=lambda _client, room_id, _user: membership[room_id]),
        AsyncMock(return_value=None),
    ):
        with _matrix(), patch("mindroom.contacts.federation.get_room_membership", new=read):
            for name in ("mind_alice", "mind_bob"):
                await federation.reconcile_remote_contacts(config, paths, name, runtime.bots.get)
        assert _statuses(paths) == ["pending", "active"]


@pytest.mark.asyncio
async def test_joining_rooms_at_start_catches_up_on_remote_contacts(tmp_path: Path) -> None:
    """The bot's start-time room pass reads its remote contacts' rooms, so an acceptance it missed still lands."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    runtime = _runtime()
    lifecycle = _room_lifecycle(config, paths, "mind_alice", runtime.bots.get)
    with (
        _matrix(),
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.contacts.federation.get_room_membership", new=AsyncMock(return_value="join")),
    ):
        await lifecycle.join_configured_rooms()
    assert _statuses(paths) == ["active"]


@pytest.mark.asyncio
async def test_the_other_agent_asking_clears_its_decline_of_our_request(tmp_path: Path) -> None:
    """Each side enforces only its own owner's decline, so the agent that declined our request may still ask us."""
    config, paths = _remote_setup(tmp_path)
    ours = ContactRecord(
        id="ours",
        status="declined",
        requester=ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER),
        target=remote_party(REMOTE),
        reason="Lunch?",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id=BOB_ROOM,
        origin_thread_id=None,
        room_id="!ours:localhost",
    )
    _store(paths, ours)
    runtime = _runtime()
    with _delivered():
        await _invited(config, paths, runtime)
    cleared, asked = _stored(paths)
    assert (cleared.id, cleared.status) == ("ours", "removed")
    assert (asked.requester, asked.status) == (remote_party(REMOTE), "pending")
    assert not _rejected(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["accept", "decline"])
async def test_answers_to_one_request_from_elsewhere_run_one_at_a_time(tmp_path: Path, second: str) -> None:
    """Every answer joins or leaves the request's one room, so a second answer waits and then finds it accepted."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    paused, resume = asyncio.Event(), asyncio.Event()

    async def slow_join(_room_id: str, _membership: str) -> bool:
        paused.set()
        await resume.wait()
        return True

    runtime.bots["mind_bob"].change_local_membership = AsyncMock(side_effect=slow_join)
    actor = _bob(config, paths, runtime)
    with _matrix(), _delivered():
        first = asyncio.create_task(flow.accept_contact(actor, "in", rules=None, tools=None))
        await paused.wait()
        answer = (
            flow.accept_contact(actor, "in", rules=None, tools=None)
            if second == "accept"
            else flow.decline_contact(actor, "in")
        )
        late = asyncio.create_task(answer)
        await asyncio.sleep(0)
        resume.set()
        assert (await first).status == "active"
        with pytest.raises(ValueError, match=r"^Only a pending contact can be (accepted|declined)$"):
            await late
    assert _statuses(paths) == ["active"]
    assert [call.args for call in runtime.bots["mind_bob"].change_local_membership.await_args_list] == [
        (REMOTE_ROOM, "join"),
    ]
    runtime.clients["mind_bob"].room_leave.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_request_from_an_agent_no_longer_listed_cannot_be_accepted(tmp_path: Path) -> None:
    """An operator who stops naming an agent in remote_agents revokes it, so its waiting request can only be declined."""
    _, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    config, _ = _setup(tmp_path)
    runtime = _runtime()
    refusal = rf"^{re.escape(REMOTE)} is no longer among the agents remote_agents names; decline the request$"
    with _matrix(), pytest.raises(ValueError, match=refusal):
        await flow.accept_contact(_bob(config, paths, runtime), "in", rules=None, tools=None)
    runtime.bots["mind_bob"].change_local_membership.assert_not_awaited()
    assert _statuses(paths) == ["pending"]


@pytest.mark.asyncio
async def test_a_decline_whose_rejection_fails_leaves_the_request_pending(tmp_path: Path) -> None:
    """The other side learns of a decline only from the rejected invite, so one it cannot see is no decline here."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    runtime.clients["mind_bob"].room_leave = AsyncMock(return_value=nio.RoomLeaveError("unreachable"))
    refusal = (
        rf"^The invite from {re.escape(REMOTE)} could not be rejected, so the request still waits; decline again later$"
    )
    with _delivered() as send, pytest.raises(ValueError, match=refusal):
        await flow.decline_contact(_bob(config, paths, runtime), "in")
    assert _statuses(paths) == ["pending"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_agent_that_loses_contacts_withdraws_its_unanswered_request_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the asking agent leaves, its request's room has nobody to let the other agent in, so the request ends."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    assert contact_room_ids(config, paths, "mind_alice") == frozenset({REMOTE_ROOM})
    without, _ = _setup(tmp_path, contacts=False)
    left: list[str] = []

    async def record_left(_client: object, room_ids: list[str], **_kwargs: object) -> None:
        left.extend(room_ids)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[REMOTE_ROOM]))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", record_left)
    lifecycle = _room_lifecycle(without, paths, "mind_alice")
    with _matrix() as matrix:
        await lifecycle.leave_unconfigured_rooms()
    assert _statuses(paths) == ["removed"]
    matrix["kick"].assert_awaited_once_with(lifecycle.deps.runtime.client, REMOTE_ROOM, REMOTE)
    assert left == [REMOTE_ROOM]


@pytest.mark.asyncio
async def test_removing_a_pending_request_whose_rejection_fails_keeps_it(tmp_path: Path) -> None:
    """Removing a request from another deployment rejects its invite first, so a failed rejection removes nothing."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    runtime.clients["mind_bob"].room_leave = AsyncMock(return_value=nio.RoomLeaveError("unreachable"))
    refusal = rf"^The invite from {re.escape(REMOTE)} could not be rejected, so the request still waits; remove it again later$"
    with _matrix(), pytest.raises(ValueError, match=refusal):
        await flow.remove_contact(_bob(config, paths, runtime), "in")
    assert _statuses(paths) == ["pending"]
    runtime.bots["mind_bob"].change_local_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_declining_a_request_whose_invite_is_gone_declines_it(tmp_path: Path) -> None:
    """A leave the homeserver refuses finds no invite left, as after the other side withdrew it, so nothing waits."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    gone = nio.RoomLeaveError("You are not invited to this room.", status_code="M_FORBIDDEN")
    runtime.clients["mind_bob"].room_leave = AsyncMock(return_value=gone)
    with _delivered():
        declined = await flow.decline_contact(_bob(config, paths, runtime), "in")
    assert declined.status == "declined"


@pytest.mark.asyncio
async def test_a_stopped_agent_cannot_decline_another_deployments_request(tmp_path: Path) -> None:
    """A stopped agent's client cannot reject the invite, so the decline waits for the agent to run."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _incoming_with_notice())
    runtime = _runtime()
    runtime.bots["mind_bob"].running = False
    with _delivered(), pytest.raises(ValueError, match=r"^The invite from .* could not be rejected"):
        await flow.decline_contact(_bob(config, paths, runtime), "in")
    runtime.clients["mind_bob"].room_leave.assert_not_awaited()
    assert _statuses(paths) == ["pending"]


@pytest.mark.asyncio
async def test_a_contact_whose_catch_up_fails_stops_no_other(tmp_path: Path) -> None:
    """Each contact moves only from the status it is in, so one that fails at start is retried next time."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    _store(paths, _outgoing().model_copy(update={"id": "later", "room_id": "!later:other.org"}))
    runtime = _runtime()

    async def read(_client: object, room_id: str, _user_id: str) -> str:
        if room_id == REMOTE_ROOM:
            msg = "unreadable"
            raise RuntimeError(msg)
        return "join"

    with _matrix(), patch("mindroom.contacts.federation.get_room_membership", new=AsyncMock(side_effect=read)):
        await federation.reconcile_remote_contacts(config, paths, "mind_alice", runtime.bots.get)
    assert _statuses(paths) == ["pending", "active"]


@pytest.mark.asyncio
async def test_a_withdrawal_that_raises_still_lets_the_agent_leave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A withdrawal that fails is only logged, so an agent that loses contacts still leaves its rooms."""
    _, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    without, _ = _setup(tmp_path, contacts=False)
    left: list[str] = []

    async def record_left(_client: object, room_ids: list[str], **_kwargs: object) -> None:
        left.extend(room_ids)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[REMOTE_ROOM]))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", record_left)
    with _matrix() as matrix:
        matrix["kick"].side_effect = RuntimeError("homeserver unreachable")
        await _room_lifecycle(without, paths, "mind_alice").leave_unconfigured_rooms()
    assert _statuses(paths) == ["removed"]
    assert left == [REMOTE_ROOM]


@pytest.mark.asyncio
async def test_a_request_back_while_ours_waits_names_why_it_is_rejected(tmp_path: Path) -> None:
    """The other side reads the rejection as a decline, so the log names this agent's own request still waiting."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing())
    answer = await federation.store_contact_invite(
        config,
        paths,
        "mind_alice",
        room_id="!theirs:other.org",
        inviter=REMOTE,
        reason="Lunch?",
    )
    assert answer is not None
    assert (answer.rejection, answer.notice) == ("own_request_pending", None)
    assert _statuses(paths) == ["pending"]


@pytest.mark.asyncio
async def test_a_request_while_a_contact_is_active_names_the_contact(tmp_path: Path) -> None:
    """Nothing waits once the pair's contact is active, so the rejection names that contact instead."""
    config, paths = _remote_setup(tmp_path)
    _store(paths, _outgoing(status="active"))
    answer = await federation.store_contact_invite(
        config,
        paths,
        "mind_alice",
        room_id="!theirs:other.org",
        inviter=REMOTE,
        reason="Lunch?",
    )
    assert answer is not None
    assert answer.rejection == "pair_has_a_contact"
