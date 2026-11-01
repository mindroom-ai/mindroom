"""Tests for matrix_message as the one way text reaches a contact room: the policy, attachments, and destinations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.custom_tools.matrix_api import MatrixApiTools
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.custom_tools.matrix_message_idempotency import claim_matrix_message_send
from mindroom.entity_resolution import current_entity_id
from mindroom.hooks import EVENT_CONTACT_BEFORE_SEND, ContactBeforeSendContext, HookBindings, HookRegistry, hook
from mindroom.matrix.message_extras import MatrixMessageExtra
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    make_conversation_reader_mock,
    make_latest_thread_event_id_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    make_visible_message,
    serve_conversation_reader,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts
from tests.test_generated_agents import BASE, CO_OWNER, OWNER, _load, _personal
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import _ContactStatus
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

CONTACT_ROOM = "!contact:localhost"
OTHER_ROOM = "!other:localhost"
PERSONAL_ROOM = "!personal-alice:localhost"
THREAD_ID = "$thread:localhost"
EVENT_ID = "$event:localhost"
DESTINATION_ERROR = "Error: in a contact conversation you can only write to this room or to your owner's personal room"
OWNER_ROOM_ERROR = "Error: in a contact conversation you can only send a plain message to your owner's personal room"
OWNER_ROOM_KEY_ERROR = "Error: in a contact conversation you cannot use idempotency_key in your owner's personal room"
OWNER_ROOM_THREAD_ERROR = (
    "Error: in a contact conversation you can only write to your owner's personal room in its timeline or a new thread"
)
TEXT_ONLY_ERROR = "Error: contact rooms accept text only"
NOT_AUTHORIZED = "Not authorized to access the target room."
MENU = MatrixMessageExtra(title="Menu", content="Soup")


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    MatrixMessageTools._recent_actions.clear()


def _setup(tmp_path: Path, *, administrators: tuple[str, ...] = ()) -> tuple[Config, RuntimePaths]:
    """Personal agents of Alice and Bob with contacts, Alice's personal room with her agent, and a limit of 3."""
    data: dict[str, Any] = {
        **BASE,
        "defaults": {**BASE["defaults"], "max_consecutive_agent_replies": 3},
        "administrators": list(administrators),
        "personal_rooms": {"agent": "mind", "onboarding_rooms": ["lobby"]},
        "personal_agents": {
            "mind": {"agent": {"display_name": "{user_display_name}'s Mind", "model": "default", "contacts": {}}},
        },
    }
    config = _load(tmp_path, data, entries=[_personal(), _personal(CO_OWNER)])
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    record = PersonalRoomRecord(
        user_id=OWNER,
        alias="personal-alice",
        source_room_id="!lobby:localhost",
        room_id=PERSONAL_ROOM,
    )
    write_personal_room(personal_room_record_path(paths, "mind_alice", OWNER), record)
    return config, paths


def _contact(paths: RuntimePaths, status: _ContactStatus = "active") -> None:
    """Store contact ``c1`` between Alice's and Bob's agents, whose room is the contact room."""
    record = ContactRecord(
        id="c1",
        status=status,
        requester=ContactParty(kind="agent", id="mind_alice", owner=OWNER),
        target=ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER),
        reason="Plan the dinner",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id="!origin:localhost",
        origin_thread_id=None,
        room_id=CONTACT_ROOM,
        created_at=1.0,
    )
    update_contacts(paths, lambda book: book.contacts.append(record))


def _agent(paths: RuntimePaths, agent_name: str) -> str:
    return current_entity_id(agent_name, paths).full_id


async def _room_send(**kwargs: Any) -> nio.RoomSendResponse:  # noqa: ANN401
    return nio.RoomSendResponse(f"$sent-{kwargs.get('tx_id') or 'plain'}", kwargs["room_id"])


def _context(
    config: Config,
    paths: RuntimePaths,
    *callbacks: object,
    room_id: str = CONTACT_ROOM,
    requester_id: str = OWNER,
) -> ToolRuntimeContext:
    """Alice's agent in a turn in ``room_id``, a contact turn by default; Alice was only invited to the contact room."""
    client = make_matrix_client_mock(user_id=_agent(paths, "mind_alice"))
    client.room_send = AsyncMock(side_effect=_room_send)
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[
                nio.RoomMember(user_id, None, None)
                for user_id in (_agent(paths, "mind_alice"), _agent(paths, "mind_bob"), CO_OWNER)
            ],
            room_id=CONTACT_ROOM,
        ),
    )
    reader = make_conversation_reader_mock()
    reader.latest_thread_event_id = make_latest_thread_event_id_mock()
    bindings = HookBindings(registry=HookRegistry.from_plugins([_plugin("contact-policy", list(callbacks))]))
    return make_test_tool_runtime_context(
        agent_name="mind_alice",
        target=MessageTarget.resolve(room_id=room_id, thread_id=THREAD_ID, reply_to_event_id=EVENT_ID),
        requester_id=requester_id,
        client=client,
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=reader,
        hooks=bindings if callbacks else HookBindings(),
    )


def _sent(context: ToolRuntimeContext) -> list[dict[str, Any]]:
    """Return the content of every event the agent's client sent."""
    return [call.kwargs["content"] for call in context.client.room_send.await_args_list]


async def _call(context: ToolRuntimeContext, **kwargs: object) -> dict[str, Any]:
    with tool_runtime_context(context):
        return json.loads(await MatrixMessageTools().matrix_message(**kwargs))


def _recorder(seen: list[str], effect: str = "none") -> object:
    """Return a contact policy hook that records each text it checks and then applies ``effect``."""

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def policy(ctx: ContactBeforeSendContext) -> None:
        seen.append(ctx.text)
        if effect == "rewrite":
            ctx.rewrite("Dinner?")
        elif effect == "blank":
            ctx.rewrite("   ")
        elif effect == "revise":
            ctx.revise("Leave out the budget.")
        elif effect == "deny":
            ctx.deny("Not allowed.")
        elif effect == "budget" and "budget" in ctx.text:
            ctx.revise("Leave out the budget.")

    return policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "sent_body", "outcome"),
    [
        ("none", "Dinner at eight, budget 500?", {"status": "ok"}),
        (
            "rewrite",
            "Dinner?",
            {"status": "ok", "note": "Sent (changed by the contact policy)", "sent_text": "Dinner?"},
        ),
        (
            "revise",
            None,
            {"status": "error", "message": "Not sent: Leave out the budget. Revise the message and send it again."},
        ),
        ("deny", None, {"status": "error", "message": "Not sent: Not allowed."}),
        ("blank", None, {"status": "error", "message": "Not sent: A contact policy hook left the message empty."}),
    ],
)
async def test_send_into_contact_room_follows_the_verdict(
    tmp_path: Path,
    effect: str,
    sent_body: str | None,
    outcome: dict[str, str],
) -> None:
    """The policy sees the agent's text, and only a send or a rewrite reaches the room, with the text the policy left."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen, effect))

    payload = await _call(context, message="Dinner at eight, budget 500?")

    assert seen == ["Dinner at eight, budget 500?"]
    assert {key: payload.get(key) for key in outcome} == outcome
    assert ("note" in payload) == ("sent_text" in payload) == (effect == "rewrite")
    assert [content["body"] for content in _sent(context)] == ([] if sent_body is None else [sent_body])


@pytest.mark.asyncio
async def test_send_is_denied_once_the_thread_ends_with_too_many_agent_messages(tmp_path: Path) -> None:
    """Trailing agent messages up to the first person count toward the limit, read from the target thread."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    context = _context(config, paths)
    alice_agent, bob_agent = _agent(paths, "mind_alice"), _agent(paths, "mind_bob")
    trail = [make_visible_message(sender=sender) for sender in (CO_OWNER, bob_agent, alice_agent, bob_agent)]
    serve_conversation_reader(context.conversation_reader, trail, room_id=CONTACT_ROOM, thread_id=THREAD_ID)

    payload = await _call(context, message="And one more thing.")

    assert payload["status"] == "error"
    assert payload["message"] == (
        "Not sent: The conversation already has 3 agent messages in a row; wait for a person to write."
    )
    assert _sent(context) == []
    context.conversation_reader.read.assert_awaited_once_with(room_id=CONTACT_ROOM, thread_id=THREAD_ID, limit=3)

    serve_conversation_reader(context.conversation_reader, trail[:-1], room_id=CONTACT_ROOM, thread_id=THREAD_ID)
    assert (await _call(context, message="And one more thing."))["status"] == "ok"


@pytest.mark.asyncio
async def test_new_thread_send_counts_the_room_conversation_its_root_joins(tmp_path: Path) -> None:
    """A new thread's root is a room-level message, so the room conversation's trailing agent messages decide it."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    context = _context(config, paths)
    alice_agent, bob_agent = _agent(paths, "mind_alice"), _agent(paths, "mind_bob")
    roots = [make_visible_message(sender=sender) for sender in (CO_OWNER, alice_agent, bob_agent, alice_agent)]
    serve_conversation_reader(context.conversation_reader, roots, room_id=CONTACT_ROOM)

    payload = await _call(context, message="A new topic.", new_thread=True)

    assert payload["status"] == "error"
    assert payload["message"] == (
        "Not sent: The conversation already has 3 agent messages in a row; wait for a person to write."
    )
    assert _sent(context) == []
    context.conversation_reader.read.assert_awaited_once_with(room_id=CONTACT_ROOM, thread_id=None, limit=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["rewrite", "revise"])
async def test_edit_in_contact_room_runs_the_policy(tmp_path: Path, effect: str) -> None:
    """An edit is checked like a send, but it adds no message, so the agent message limit does not stop it."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen, effect))
    agents = [make_visible_message(sender=_agent(paths, name)) for name in ("mind_bob", "mind_alice", "mind_bob")]
    serve_conversation_reader(context.conversation_reader, agents, room_id=CONTACT_ROOM, thread_id=THREAD_ID)

    payload = await _call(context, action="edit", event_id="$mine:localhost", message="Dinner at eight, budget 500?")

    assert seen == ["Dinner at eight, budget 500?"]
    if effect == "rewrite":
        assert payload["status"] == "ok"
        assert payload["note"] == "Edited (changed by the contact policy)"
        assert payload["sent_text"] == "Dinner?"
        (content,) = _sent(context)
        assert content["m.new_content"]["body"] == "Dinner?"
    else:
        assert payload["status"] == "error"
        assert payload["message"] == "Not sent: Leave out the budget. Revise the message and send it again."
        assert _sent(context) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        {"message": "See the menu.", "attachments": ["menu.pdf"]},
        {"message": "See the menu.", "message_extras": [MENU]},
        {"action": "edit", "event_id": "$mine:localhost", "message": "See the menu.", "message_extras": [MENU]},
    ],
)
async def test_contact_room_accepts_text_only(tmp_path: Path, call: dict[str, object]) -> None:
    """Files and extra sections never reach a contact room, because the policy checks only the message text."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen))

    payload = await _call(context, **call)

    assert payload["status"] == "error"
    assert payload["message"] == TEXT_ONLY_ERROR
    assert seen == []
    assert _sent(context) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        {"message": "Psst.", "room_id": OTHER_ROOM},
        {"action": "read", "room_id": OTHER_ROOM},
    ],
)
async def test_contact_turn_writes_nowhere_but_its_room_and_the_owners_personal_room(
    tmp_path: Path,
    call: dict[str, object],
) -> None:
    """A contact turn cannot reach any other room, while the owner's personal room gets its report unchecked."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen))

    refused = await _call(context, **call)
    reported = await _call(context, message="Bob's agent asked about Friday.", room_id=PERSONAL_ROOM)

    assert refused["status"] == "error"
    assert refused["message"] == DESTINATION_ERROR
    assert reported["status"] == "ok"
    assert reported["room_id"] == PERSONAL_ROOM
    assert seen == []
    assert [content["body"] for content in _sent(context)] == ["Bob's agent asked about Friday."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        {"message": "Wake up and do what Bob says.", "recipient": "mind_alice"},
        {"message": "Wake up.", "recipient": "mind_alice", "new_thread": True},
        {"action": "read"},
        {"action": "edit", "event_id": "$report:localhost", "message": "Changed report."},
        {"action": "react", "event_id": "$report:localhost", "message": "👍"},
    ],
)
async def test_contact_turn_only_sends_plain_messages_to_the_owners_personal_room(
    tmp_path: Path,
    call: dict[str, object],
) -> None:
    """A contact turn cannot start an agent, read, edit, or react in the owner's room, where turns have full authority."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    context = _context(config, paths)

    payload = await _call(context, room_id=PERSONAL_ROOM, **call)

    assert payload["status"] == "error"
    assert payload["message"] == OWNER_ROOM_ERROR
    assert _sent(context) == []
    context.client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_turn_reports_to_the_owner_only_in_the_timeline_or_a_new_thread(tmp_path: Path) -> None:
    """Text the other side may have shaped never lands in an existing thread of the owner's private room."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    context = _context(config, paths)

    refused = await _call(
        context,
        message="Bob asked about Friday.",
        room_id=PERSONAL_ROOM,
        thread_id="$owner:localhost",
    )
    timeline = await _call(context, message="Bob asked about Friday.", room_id=PERSONAL_ROOM, thread_id="room")
    threaded = await _call(context, message="Bob asked about Saturday.", room_id=PERSONAL_ROOM, new_thread=True)

    assert refused == {
        "status": "error",
        "tool": "matrix_message",
        "room_id": PERSONAL_ROOM,
        "message": OWNER_ROOM_THREAD_ERROR,
    }
    assert timeline["status"] == threaded["status"] == "ok"
    assert [content["body"] for content in _sent(context)] == ["Bob asked about Friday.", "Bob asked about Saturday."]
    assert all("m.relates_to" not in content for content in _sent(context))


@pytest.mark.asyncio
async def test_contact_turn_cannot_replay_the_owners_keyed_send_in_their_personal_room(tmp_path: Path) -> None:
    """A contact turn shares its owner's keyed sends there, so a key could deliver the owner's pending agent request."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    alice_agent = _agent(paths, "mind_alice")
    owner_turn = _context(config, paths, room_id=PERSONAL_ROOM)
    async with claim_matrix_message_send(owner_turn, PERSONAL_ROOM, "report-1") as claim:
        await claim.prepare(
            {"msgtype": "m.text", "body": f"{alice_agent} Book the table.", ORIGINAL_SENDER_KEY: OWNER},
            recipient="mind_alice",
            recipient_user_id=alice_agent,
            thread_id=None,
            starts_thread=True,
        )
    contact = _context(config, paths)
    personal_room = nio.MatrixRoom(PERSONAL_ROOM, alice_agent)
    personal_room.add_member(alice_agent, "Alice's Mind", None)
    personal_room.add_member(OWNER, "Alice", None)
    personal_room.members_synced = True
    contact.client.rooms[PERSONAL_ROOM] = personal_room

    payload = await _call(contact, message="Bob asked about Friday.", room_id=PERSONAL_ROOM, idempotency_key="report-1")

    assert payload["status"] == "error"
    assert payload["message"] == OWNER_ROOM_KEY_ERROR
    assert _sent(contact) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("room_id", "effect", "outcome"),
    [
        (CONTACT_ROOM, "none", {"status": "ok", "reaction": "🎉"}),
        (
            CONTACT_ROOM,
            "rewrite",
            {"status": "ok", "reaction": "Dinner?", "note": "Reacted (changed by the contact policy)"},
        ),
        (
            CONTACT_ROOM,
            "revise",
            {"status": "error", "message": "Not sent: Leave out the budget. Revise the message and send it again."},
        ),
        (CONTACT_ROOM, "deny", {"status": "error", "message": "Not sent: Not allowed."}),
        (PERSONAL_ROOM, "deny", {"status": "error", "message": "Not sent: Not allowed."}),
    ],
)
async def test_reaction_in_contact_room_passes_the_policy(
    tmp_path: Path,
    room_id: str,
    effect: str,
    outcome: dict[str, str],
) -> None:
    """A reaction key is free text, so from a contact turn or a turn elsewhere it goes out only as the policy allows."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen, effect), room_id=room_id)

    payload = await _call(context, action="react", room_id=CONTACT_ROOM, event_id="$theirs:localhost", message="🎉")

    assert seen == ["🎉"]
    assert {key: payload.get(key) for key in outcome} == outcome
    keys = [content["m.relates_to"]["key"] for content in _sent(context)]
    assert keys == ([outcome["reaction"]] if outcome["status"] == "ok" else [])


@pytest.mark.asyncio
async def test_room_tools_refuse_a_contact_room_that_matrix_message_may_use(tmp_path: Path) -> None:
    """Only callers that opt in reach the agent's contact room, whether it is the current room or a named one."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    contact_turn = _context(config, paths)
    elsewhere = _context(config, paths, room_id=OTHER_ROOM)

    assert not await room_access_allowed(contact_turn, CONTACT_ROOM)
    assert not await room_access_allowed(elsewhere, CONTACT_ROOM)
    assert await room_access_allowed(contact_turn, CONTACT_ROOM, contact_room_ok=True)
    assert await room_access_allowed(elsewhere, CONTACT_ROOM, contact_room_ok=True)
    assert await room_access_allowed(elsewhere, PERSONAL_ROOM)

    with tool_runtime_context(elsewhere):
        payload = json.loads(
            await MatrixApiTools().matrix_api(
                action="send_event",
                room_id=CONTACT_ROOM,
                event_type="m.room.message",
                content={"msgtype": "m.text", "body": "Unchecked"},
            ),
        )
    assert payload["status"] == "error"
    assert payload["message"] == NOT_AUTHORIZED
    assert _sent(elsewhere) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("idempotency_key", [None, "report-1"])
async def test_normal_turn_sends_into_its_contact_room_for_an_owner_who_only_was_invited(
    tmp_path: Path,
    idempotency_key: str | None,
) -> None:
    """The agent speaks for its owner in its contact room, so the owner need not have joined it, and the policy runs."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen), room_id=PERSONAL_ROOM)

    payload = await _call(
        context,
        message="Alice is free on Friday.",
        room_id=CONTACT_ROOM,
        idempotency_key=idempotency_key,
    )

    assert payload["status"] == "ok"
    assert payload["room_id"] == CONTACT_ROOM
    assert seen == ["Alice is free on Friday."]
    assert [content["body"] for content in _sent(context)] == ["Alice is free on Friday."]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_only_the_owner_drives_the_agent_into_its_contact_room(tmp_path: Path) -> None:
    """Someone else allowed to use the agent cannot send through it into its contact room, even when joined there."""
    config, paths = _setup(tmp_path, administrators=(CO_OWNER,))
    _contact(paths)
    context = _context(config, paths, room_id=OTHER_ROOM, requester_id=CO_OWNER)

    payload = await _call(context, message="Alice says yes.", room_id=CONTACT_ROOM)

    assert payload["status"] == "error"
    assert payload["message"] == NOT_AUTHORIZED
    assert _sent(context) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_send_after_removal_is_refused(tmp_path: Path) -> None:
    """A removed contact's room is an ordinary room the owner never joined, so the agent cannot send there for them."""
    config, paths = _setup(tmp_path)
    _contact(paths, status="removed")
    context = _context(config, paths, room_id=PERSONAL_ROOM)

    payload = await _call(context, message="Still there?", room_id=CONTACT_ROOM)

    assert payload["status"] == "error"
    assert payload["message"] == NOT_AUTHORIZED
    assert _sent(context) == []


@pytest.mark.asyncio
async def test_keyed_send_the_policy_stopped_stores_nothing_to_replay(tmp_path: Path) -> None:
    """A retry with the same key after a revise sends the revised text, not the stopped one."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    seen: list[str] = []
    context = _context(config, paths, _recorder(seen, "budget"))

    stopped = await _call(context, message="The budget is 500.", idempotency_key="answer-1")
    revised = await _call(context, message="Dinner at eight?", idempotency_key="answer-1")
    replayed = await _call(context, message="Something else.", idempotency_key="answer-1")

    assert stopped["status"] == "error"
    assert stopped["message"] == "Not sent: Leave out the budget. Revise the message and send it again."
    assert revised["status"] == "ok"
    assert replayed == revised
    assert seen == ["The budget is 500.", "Dinner at eight?"]
    assert [content["body"] for content in _sent(context)] == ["Dinner at eight?"]
