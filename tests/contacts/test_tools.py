"""Tests for the contacts toolkit and the flow behind it: request, decline, update, remove, and list."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mindroom.agents import create_agent
from mindroom.config.resolution import agent_policy, agent_tool_facts
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.contacts import flow
from mindroom.contacts.store import ContactParty, ContactRecord, contact_room_ids, read_contacts, update_contacts
from mindroom.custom_tools.contacts import ContactsTools
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from mindroom.message_target import MessageTarget
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.test_generated_agents import CO_OWNER, OWNER, _load, _personal, _with_template
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

CAROL = "@carol:localhost"
ORIGIN_ROOM = "!room:example.org"
ORIGIN_THREAD = "$thread"
BOB_ROOM = "!bobroom:localhost"
CAROL_ROOM = "!carolroom:localhost"
CONTACT_ROOM = "!contact:localhost"
TOOLS = ("mind_alice", "mind_bob", "mind_carol", "work_bob")
DECLINED = f"{CO_OWNER} declined this contact; they can remove it to allow a new request"


@dataclass
class _Runtime:
    """What a running orchestrator holds for the contacts: every personal agent's client and bot."""

    orchestrator: MagicMock
    clients: dict[str, AsyncMock] = field(default_factory=dict)
    bots: dict[str, MagicMock] = field(default_factory=dict)

    def sent(self, agent_name: str) -> AsyncMock:
        """Return the hook sender of one agent's bot."""
        return self.bots[agent_name].hook_context.send_message


def _runtime() -> _Runtime:
    orchestrator = MagicMock()
    runtime = _Runtime(orchestrator)
    for name in TOOLS:
        runtime.clients[name] = AsyncMock()
        runtime.bots[name] = bot = MagicMock(running=True, client=runtime.clients[name])
        bot.hook_context.send_message = AsyncMock(return_value="$origin-notice")
        bot.change_local_membership = AsyncMock(return_value=True)
        bot.current_joined_room_ids = AsyncMock(return_value=frozenset())
    orchestrator.running_entity_client.side_effect = runtime.clients.get
    orchestrator.agent_bots = runtime.bots
    return runtime


def _data(*, tools: list[str] | None = None, contacts: bool = True, accept_invites: bool = True) -> dict[str, Any]:
    data = _with_template()
    agent = data["personal_agents"]["mind"]["agent"]
    agent["tools"] = tools or ["calculator", "scheduler", "self_config", "matrix_message"]
    agent["include_default_tools"] = False
    agent["accept_invites"] = accept_invites
    if contacts:
        agent["contacts"] = {"rules": ["Never share keys."]}
    data["personal_rooms"] = {"agent": "mind", "onboarding_rooms": ["lobby"]}
    return data


def _setup(tmp_path: Path, *, work: bool = False, **data_options: Any) -> tuple[Config, RuntimePaths]:  # noqa: ANN401
    """Three personal agents of one template, with personal rooms for Bob and Carol.

    With ``work`` Bob also has ``work_bob`` from a second template, which is not the agent of his personal room.
    """
    data = _data(**data_options)
    entries = [_personal(), _personal(CO_OWNER), _personal(CAROL)]
    if work:
        data["personal_agents"]["work"] = deepcopy(data["personal_agents"]["mind"])
        data["personal_agents"]["work"]["agent"]["display_name"] = "{user_display_name}'s Work"
        entries.append(_personal(CO_OWNER, template="work", agent_name="work_bob"))
    config = _load(tmp_path, data, entries=entries)
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    for user_id, agent_name, room_id in ((CO_OWNER, "mind_bob", BOB_ROOM), (CAROL, "mind_carol", CAROL_ROOM)):
        record = PersonalRoomRecord(
            user_id=user_id,
            alias=f"personal-{agent_name}",
            source_room_id="!lobby:localhost",
            room_id=room_id,
        )
        write_personal_room(personal_room_record_path(paths, agent_name, user_id), record)
    return config, paths


def _context(
    config: Config,
    paths: RuntimePaths,
    agent_name: str,
    requester_id: str,
    runtime: _Runtime | None = None,
) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(room_id=ORIGIN_ROOM, thread_id=ORIGIN_THREAD, reply_to_event_id="$request"),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=None if runtime is None else runtime.orchestrator,
    )


@contextmanager
def _delivered(event_id: str | None = "$notice") -> Iterator[AsyncMock]:
    """Stand in for the Matrix send of a request notice, which either lands as ``event_id`` or fails."""
    delivery = None if event_id is None else DeliveredMatrixEvent(event_id, {})
    with patch("mindroom.contacts.flow.send_message_result", new=AsyncMock(return_value=delivery)) as send:
        yield send


async def _request(
    config: Config,
    paths: RuntimePaths,
    runtime: _Runtime,
    *,
    agent_name: str = "mind_alice",
    requester_id: str = OWNER,
    target: str = "mind_bob",
    **options: Any,  # noqa: ANN401
) -> ContactRecord:
    fields: dict[str, Any] = {"reason": "Plan the dinner", "rules": None, "tools": None, "party": "agent"}
    return await flow.request_contact(
        _context(config, paths, agent_name, requester_id, runtime),
        agent_name,
        target=target,
        **(fields | options),
    )


async def _pending(config: Config, paths: RuntimePaths, runtime: _Runtime, **options: Any) -> ContactRecord:  # noqa: ANN401
    with _delivered():
        return await _request(config, paths, runtime, **options)


def _activate(paths: RuntimePaths, contact_id: str) -> None:
    def change(book: Any) -> None:  # noqa: ANN401
        record = next(record for record in book.contacts if record.id == contact_id)
        record.status = "active"
        record.room_id = CONTACT_ROOM

    update_contacts(paths, change)


def _stored(paths: RuntimePaths) -> list[ContactRecord]:
    return read_contacts(paths).contacts


def test_confirm_gates() -> None:
    """A model's request, acceptance, and change of rules always ask the owner; the rest only take access away or read."""
    functions = adapt_toolkit(ContactsTools("mind_alice")).functions
    assert {function.name for function in functions} == {
        "list_contacts",
        "request_contact",
        "accept_contact",
        "decline_contact",
        "update_contact",
        "remove_contact",
    }
    assert {function.name for function in functions if function.gate is ToolGate.CONFIRM} == {
        "request_contact",
        "accept_contact",
        "update_contact",
    }


@pytest.mark.asyncio
async def test_request_requires_owner(tmp_path: Path) -> None:
    """Only the owner of the agent may request its contacts, and only a personal agent has any."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with pytest.raises(ValueError, match=rf"^Only {OWNER} may use the contacts of mind_alice"):
        await _request(config, paths, runtime, requester_id=CO_OWNER)
    with pytest.raises(ValueError, match=r"^creator is not a personal agent; only personal agents can have contacts"):
        await _request(config, paths, runtime, agent_name="creator")
    assert _stored(paths) == []


@pytest.mark.asyncio
async def test_a_personal_agent_without_contacts_cannot_request(tmp_path: Path) -> None:
    """Contacts are opt-in, so an agent whose template sets none cannot ask for one."""
    config, paths = _setup(tmp_path, contacts=False)
    with pytest.raises(ValueError, match=r"^contacts are not enabled for mind_alice"):
        await _request(config, paths, _runtime(), target=CAROL)


@pytest.mark.asyncio
async def test_request_records_pending_and_posts_notice_with_txn_id(tmp_path: Path) -> None:
    """The request is stored first, then the agent of the target owner's personal room posts a notice naming it there."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with _delivered() as send:
        record = await _request(config, paths, runtime, rules="Be brief", tools=["calculator"])

    assert record.status == "pending"
    assert record.requester == ContactParty(kind="agent", id="mind_alice", owner=OWNER)
    assert record.target == ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER)
    assert (record.requester_side.rules, record.requester_side.tools) == ("Be brief", ["calculator"])
    assert (record.target_side.rules, record.target_side.tools) == (None, None)
    assert (record.origin_room_id, record.origin_thread_id) == (ORIGIN_ROOM, ORIGIN_THREAD)
    assert record.notice_event_id == "$notice"
    assert _stored(paths) == [record]

    send.assert_awaited_once()
    client, room_id, content = send.await_args.args
    assert (client, room_id) == (runtime.clients["mind_bob"], BOB_ROOM)
    assert send.await_args.kwargs == {"transaction_id": f"contact-request-{record.id}"}
    assert content["msgtype"] == "m.notice"
    assert content[SKIP_MENTIONS_KEY] is True
    assert content["io.mindroom.contact_request"] == record.id
    assert "m.relates_to" not in content
    for text in (
        OWNER,
        "Plan the dinner",
        "- Never share keys.",
        "Reply in this thread to accept, decline, or set rules.",
    ):
        assert text in content["body"]
    assert record.id in content["body"]


@pytest.mark.asyncio
async def test_a_person_target_is_asked_by_their_own_personal_agent(tmp_path: Path) -> None:
    """A person has no agent to contact, so their personal agent asks them in their personal room."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with _delivered() as send:
        record = await _request(config, paths, runtime, target=CAROL)

    assert record.target == ContactParty(kind="person", id=CAROL, owner=CAROL)
    assert send.await_args.args[:2] == (runtime.clients["mind_carol"], CAROL_ROOM)
    assert "Never share keys." not in send.await_args.args[2]["body"]


@pytest.mark.asyncio
async def test_a_target_agent_outside_its_owners_personal_room_is_asked_through_that_room(tmp_path: Path) -> None:
    """The agent of the owner's personal room posts the request, even when the target is another of the owner's agents."""
    config, paths = _setup(tmp_path, work=True)
    runtime = _runtime()
    with _delivered() as send:
        record = await _request(config, paths, runtime, target="work_bob")

    assert record.target == ContactParty(kind="agent", id="work_bob", owner=CO_OWNER)
    assert send.await_args.args[:2] == (runtime.clients["mind_bob"], BOB_ROOM)
    assert "your agent Bob's Work (work_bob)" in send.await_args.args[2]["body"]


@pytest.mark.asyncio
async def test_a_request_the_target_owner_could_never_accept_is_refused(tmp_path: Path) -> None:
    """The agent of the owner's personal room accepts for them, so without contacts it takes no requests."""
    data = _data()
    data["personal_agents"]["home"] = {"agent": {"display_name": "{user_display_name}'s Home", "model": "default"}}
    data["personal_rooms"]["agent"] = "home"
    entries = [_personal(), _personal(CO_OWNER), _personal(CO_OWNER, template="home", agent_name="home_bob")]
    config = _load(tmp_path, data, entries=entries)
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)

    with _delivered() as send, pytest.raises(ValueError, match=rf"^{CO_OWNER}'s personal agent home_bob does not take"):
        await _request(config, paths, _runtime())
    send.assert_not_awaited()
    assert _stored(paths) == []


@pytest.mark.asyncio
async def test_a_notice_that_cannot_be_posted_is_an_error_and_the_request_stays(tmp_path: Path) -> None:
    """A target owner without a personal room, a stopped poster, or a failed send all say why, and keep the request."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with _delivered(None), pytest.raises(ValueError, match=r"^The request to mind_bob could not be posted"):
        await _request(config, paths, runtime)
    assert [record.status for record in _stored(paths)] == ["pending"]

    runtime.clients["mind_bob"] = None
    runtime.orchestrator.running_entity_client.side_effect = runtime.clients.get
    with pytest.raises(ValueError, match=r"^mind_bob is not running"):
        await _request(config, paths, runtime)

    config, paths = _setup(tmp_path / "other")
    (personal_room_record_path(paths, "mind_bob", CO_OWNER)).unlink()
    with pytest.raises(ValueError, match=rf"^{CO_OWNER} has no personal room"):
        await _request(config, paths, _runtime())


@pytest.mark.asyncio
async def test_repeat_request_returns_existing_and_reposts_missing_notice(tmp_path: Path) -> None:
    """A pending or active pair is returned from either side; a notice that never landed is posted again."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with _delivered(None), pytest.raises(ValueError, match=r"could not be posted"):
        await _request(config, paths, runtime)
    (missing,) = _stored(paths)
    assert missing.notice_event_id is None

    with _delivered("$late") as send:
        again = await _request(config, paths, runtime, reason="Another reason")
        reverse = await _request(
            config,
            paths,
            runtime,
            agent_name="mind_bob",
            requester_id=CO_OWNER,
            target="mind_alice",
        )
    assert (again.id, again.notice_event_id, again.reason) == (missing.id, "$late", "Plan the dinner")
    assert reverse.id == missing.id
    send.assert_awaited_once()
    assert send.await_args.kwargs == {"transaction_id": f"contact-request-{missing.id}"}

    _activate(paths, missing.id)
    with _delivered() as send:
        assert (await _request(config, paths, runtime)).status == "active"
    send.assert_not_awaited()
    assert len(_stored(paths)) == 1


@pytest.mark.asyncio
async def test_declined_pair_refused(tmp_path: Path) -> None:
    """After the target owner declines, a new request waits until that owner, and only that owner, removes the contact."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    await flow.decline_contact(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", record.id)

    with pytest.raises(ValueError, match=rf"^{DECLINED}$"):
        await _request(config, paths, runtime)
    with pytest.raises(ValueError, match=rf"^{DECLINED}$"):
        await _request(config, paths, runtime, agent_name="mind_bob", requester_id=CO_OWNER, target="mind_alice")

    with pytest.raises(ValueError, match=rf"^Only {CO_OWNER} may remove a declined contact$"):
        await flow.remove_contact(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice", record.id)
    assert [stored.status for stored in _stored(paths)] == ["declined"]
    with pytest.raises(ValueError, match=rf"^{DECLINED}$"):
        await _request(config, paths, runtime)

    await flow.remove_contact(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", record.id)
    again = await _pending(config, paths, runtime)
    assert (again.status, again.id != record.id) == ("pending", True)


@pytest.mark.asyncio
async def test_request_refuses_unknown_or_refused_tools(tmp_path: Path) -> None:
    """A contact may offer only tools the agent has, never Matrix room tools or tools that control MindRoom."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    expected = "allowed: calculator, matrix_message"
    for tools, outside in (
        (["browser"], "browser"),
        (["scheduler"], "scheduler"),
        (["calculator", "self_config"], "self_config"),
    ):
        with pytest.raises(ValueError, match=rf"^Not among the tools a contact may use: {outside}; {expected}$"):
            await _request(config, paths, runtime, tools=tools)
    assert _stored(paths) == []

    with _delivered():
        record = await _request(config, paths, runtime, tools=["matrix_message", "calculator"])
    assert record.requester_side.tools == ["matrix_message", "calculator"]


def test_validate_side_tools_keeps_no_choice_as_none(tmp_path: Path) -> None:
    """No list means the agent's default tools, and an empty list means none."""
    config, _ = _setup(tmp_path)
    assert flow._validate_side_tools(config, "mind_alice", None) is None
    assert flow._validate_side_tools(config, "mind_alice", []) == []


@pytest.mark.asyncio
async def test_party_me_makes_person_requester(tmp_path: Path) -> None:
    """With party=me the owner asks as themself: a person requester whose side has no rules or tools."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with _delivered() as send:
        record = await _request(config, paths, runtime, party="me")

    assert record.requester == ContactParty(kind="person", id=OWNER, owner=OWNER)
    assert (record.requester_side.rules, record.requester_side.tools) == (None, None)
    assert OWNER in send.await_args.args[2]["body"]
    with pytest.raises(ValueError, match=r"^A person has no rules or tools"):
        await _request(config, paths, runtime, party="me", target=CAROL, rules="Be brief")


@pytest.mark.asyncio
async def test_contacts_between_people_or_with_oneself_are_refused(tmp_path: Path) -> None:
    """A contact needs an agent on one side and two different owners."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    with pytest.raises(ValueError, match=r"^A contact needs an agent on one side"):
        await _request(config, paths, runtime, party="me", target=CAROL)
    for options in ({"target": OWNER}, {"target": "mind_alice"}, {"party": "me", "target": "mind_alice"}):
        with pytest.raises(ValueError, match=r"^A contact needs two different owners"):
            await _request(config, paths, runtime, **options)
    assert _stored(paths) == []


@pytest.mark.asyncio
async def test_decline_by_target_owner_only_and_notifies_origin(tmp_path: Path) -> None:
    """Only the target's owner declines; the requesting agent tells the origin thread without starting a turn."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    for caller, requester in (("mind_alice", OWNER), ("mind_bob", CAROL)):
        with pytest.raises(ValueError, match=r"^Only .* may"):
            await flow.decline_contact(_context(config, paths, caller, requester, runtime), caller, record.id)
    assert _stored(paths)[0].status == "pending"

    declined = await flow.decline_contact(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", record.id)

    assert declined.status == "declined"
    assert _stored(paths) == [declined]
    runtime.sent("mind_alice").assert_awaited_once()
    args = runtime.sent("mind_alice").await_args
    room_id, body, thread_id, source_hook, extra_content = args.args
    assert (room_id, thread_id) == (ORIGIN_ROOM, ORIGIN_THREAD)
    assert "declined" in body
    assert CO_OWNER in body
    assert (source_hook, extra_content) == ("native:contacts", {SKIP_MENTIONS_KEY: True})
    assert args.kwargs == {"trigger_dispatch": False}
    runtime.sent("mind_bob").assert_not_awaited()
    with pytest.raises(ValueError, match=r"^Only a pending contact can be declined"):
        await flow.decline_contact(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", record.id)


@pytest.mark.asyncio
async def test_a_person_requester_is_told_nothing_on_decline(tmp_path: Path) -> None:
    """A person who asked has no agent to post the decline, so only the stored status changes."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, party="me")
    await flow.decline_contact(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", record.id)

    assert _stored(paths)[0].status == "declined"
    for name in TOOLS:
        runtime.sent(name).assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_contact_is_refused(tmp_path: Path) -> None:
    """A contact ID nobody holds says so, and a contact of two other owners looks the same."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    for call in (flow.decline_contact, flow.remove_contact):
        with pytest.raises(ValueError, match=r"^There is no contact nope"):
            await call(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", "nope")
        with pytest.raises(ValueError, match=rf"^There is no contact {record.id}"):
            await call(_context(config, paths, "mind_carol", CAROL, runtime), "mind_carol", record.id)


@pytest.mark.asyncio
async def test_update_only_own_side(tmp_path: Path) -> None:
    """Each owner sets the rules and tools of their own agent's side only, and only while the contact lives."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, rules="Be brief")

    alice = _context(config, paths, "mind_alice", OWNER, runtime)
    updated = await flow.update_contact(alice, "mind_alice", record.id, rules="Only dinner", tools=["calculator"])
    assert (updated.requester_side.rules, updated.requester_side.tools) == ("Only dinner", ["calculator"])
    assert (updated.target_side.rules, updated.target_side.tools) == (None, None)

    bob = _context(config, paths, "mind_bob", CO_OWNER, runtime)
    updated = await flow.update_contact(bob, "mind_bob", record.id, rules="No dates", tools=None)
    assert (updated.requester_side.rules, updated.requester_side.tools) == ("Only dinner", ["calculator"])
    assert (updated.target_side.rules, updated.target_side.tools) == ("No dates", None)

    cleared = await flow.update_contact(alice, "mind_alice", record.id, rules="  ", tools=None)
    assert cleared.requester_side.rules is None
    assert cleared.requester_side.tools == ["calculator"]

    with pytest.raises(ValueError, match=r"^Not among the tools a contact may use: browser"):
        await flow.update_contact(alice, "mind_alice", record.id, rules=None, tools=["browser"])
    with pytest.raises(ValueError, match=r"^Nothing to change"):
        await flow.update_contact(alice, "mind_alice", record.id, rules=None, tools=None)
    carol = _context(config, paths, "mind_carol", CAROL, runtime)
    with pytest.raises(ValueError, match=rf"^There is no contact {record.id}"):
        await flow.update_contact(carol, "mind_carol", record.id, rules="x", tools=None)
    assert _stored(paths) == [cleared]


@pytest.mark.asyncio
async def test_update_refuses_a_person_side_and_a_finished_contact(tmp_path: Path) -> None:
    """A person's side stays empty, and a declined or removed contact has nothing left to change."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, party="me")
    alice = _context(config, paths, "mind_alice", OWNER, runtime)
    with pytest.raises(ValueError, match=r"^A person has no rules or tools"):
        await flow.update_contact(alice, "mind_alice", record.id, rules="x", tools=None)

    other = await _pending(config, paths, runtime, target=CAROL)
    await flow.remove_contact(alice, "mind_alice", other.id)
    with pytest.raises(ValueError, match=r"^A removed contact cannot be changed"):
        await flow.update_contact(alice, "mind_alice", other.id, rules="x", tools=None)


@pytest.mark.asyncio
async def test_remove_by_either_owner_leaves_room(tmp_path: Path) -> None:
    """Either owner removes a contact; its agents leave the contact room and stop keeping it across restarts."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)
    _activate(paths, record.id)
    assert contact_room_ids(paths, "mind_alice") == contact_room_ids(paths, "mind_bob") == {CONTACT_ROOM}

    removed = await flow.remove_contact(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob", record.id)

    assert removed.status == "removed"
    assert _stored(paths) == [removed]
    for name in ("mind_alice", "mind_bob"):
        runtime.bots[name].change_local_membership.assert_awaited_once_with(CONTACT_ROOM, "leave")
        assert contact_room_ids(paths, name) == frozenset()
    runtime.bots["mind_carol"].change_local_membership.assert_not_awaited()
    for client in runtime.clients.values():
        client.room_leave.assert_not_awaited()
    with pytest.raises(ValueError, match=r"^The contact is already removed"):
        await flow.remove_contact(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice", record.id)


@pytest.mark.asyncio
async def test_the_requesting_owner_removes_a_pending_contact_without_leaving_anything(tmp_path: Path) -> None:
    """A contact with no room has nothing to leave, and a stranger cannot remove it."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime)

    removed = await flow.remove_contact(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice", record.id)

    assert removed.status == "removed"
    for name in TOOLS:
        runtime.bots[name].change_local_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_contacts_shows_own_contacts_and_contactable_agents(tmp_path: Path) -> None:
    """The owner sees their own contacts with their side, and the personal agents of others they can ask."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    record = await _pending(config, paths, runtime, rules="Be brief", tools=["calculator"])
    await _pending(config, paths, runtime, agent_name="mind_bob", requester_id=CO_OWNER, target=CAROL)

    listing = yaml.safe_load(flow.list_contacts(_context(config, paths, "mind_alice", OWNER, runtime), "mind_alice"))

    (contact,) = listing["contacts"]
    assert contact["id"] == record.id
    assert (contact["status"], contact["you"], contact["reason"]) == ("pending", "requester", "Plan the dinner")
    assert contact["with"] == {"kind": "agent", "id": "mind_bob", "owner": CO_OWNER}
    assert contact["your_rules"] == "Be brief"
    assert contact["your_tools"] == ["calculator"]
    assert listing["contactable_agents"] == [
        {"agent": "mind_bob", "owner": CO_OWNER},
        {"agent": "mind_carol", "owner": CAROL},
    ]

    bob_listing = yaml.safe_load(flow.list_contacts(_context(config, paths, "mind_bob", CO_OWNER, runtime), "mind_bob"))
    assert {contact["you"] for contact in bob_listing["contacts"]} == {"target", "requester"}
    assert bob_listing["contactable_agents"] == [
        {"agent": "mind_alice", "owner": OWNER},
        {"agent": "mind_carol", "owner": CAROL},
    ]


@pytest.mark.asyncio
async def test_toolkit_calls_run_the_flow_and_report_errors_as_text(tmp_path: Path) -> None:
    """The toolkit answers in text: a summary on success and the refusal as an Error line."""
    config, paths = _setup(tmp_path)
    runtime = _runtime()
    tool = ContactsTools("mind_alice")

    assert "only works while answering a request" in await tool.list_contacts()

    with tool_runtime_context(_context(config, paths, "mind_alice", OWNER, runtime)), _delivered():
        refused = await tool.request_contact("nobody", "Say hi")
        requested = await tool.request_contact("mind_bob", "Say hi", rules="Be brief", tools=["calculator"])
        listing = yaml.safe_load(await tool.list_contacts())
        updated = await tool.update_contact(listing["contacts"][0]["id"], rules="Be kind")
        removed = await tool.remove_contact(listing["contacts"][0]["id"])
        unknown = await tool.decline_contact("nope")

    (record,) = _stored(paths)
    assert refused.startswith("Error: nobody cannot be contacted")
    assert requested == (
        f"Asked {CO_OWNER} to approve contact {record.id} with mind_bob; "
        "you are told in this thread when they accept or decline."
    )
    assert updated == f"Updated contact {record.id}."
    assert removed == f"Removed contact {record.id}."
    assert unknown == "Error: There is no contact nope"
    assert record.status == "removed"
    assert record.requester_side.rules == "Be kind"


def test_injected_for_agents_with_contacts(tmp_path: Path) -> None:
    """An agent with a contacts object gets the contacts tools and matrix_message without authoring them."""
    config, _ = _setup(tmp_path)
    facts = agent_tool_facts(config, "mind_alice", policy=agent_policy(config, "mind_alice"))
    assert {"contacts", "matrix_message"} <= set(facts.injected)
    names = {entry.name for entry in resolve_agent_tools(config, "mind_alice", builtin_tool_registry()).candidates()}
    assert {"contacts", "matrix_message"} <= names

    without, _ = _setup(tmp_path / "without", contacts=False)
    facts = agent_tool_facts(without, "mind_alice", policy=agent_policy(without, "mind_alice"))
    assert "contacts" not in facts.injected
    assert "contacts" not in {
        entry.name for entry in resolve_agent_tools(without, "mind_alice", builtin_tool_registry()).candidates()
    }


def test_a_personal_agent_with_contacts_is_built_with_the_contacts_tools_and_matrix_message(tmp_path: Path) -> None:
    """The agent a turn builds offers the contacts functions and matrix_message, which no entry of its config names."""
    config, paths = _setup(tmp_path)
    build = create_agent(
        "mind_alice",
        config,
        paths,
        execution_identity=None,
        knowledge=None,
        model=turn_model(config, paths, "mind_alice"),
        supports_native_tool_approval=True,
        plugins=config_plugins(config, paths),
    )
    offered = {toolkit.name: {function.name for function in toolkit.functions} for toolkit in build.spec.toolkits}

    assert offered["contacts"] == {
        "list_contacts",
        "request_contact",
        "accept_contact",
        "decline_contact",
        "update_contact",
        "remove_contact",
    }
    assert "matrix_message" in offered
