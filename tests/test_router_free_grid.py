"""Who answers a person in a room, by every path a message takes, with no router account to do it.

Each room holds one person and the MindRoom accounts its row names, every account runs its own turn owners over one
shared journal, and each event arrives at every account as sync delivers it. One table says which account answers
each path; every cell then checks that exactly one answer exists, that the answering account sent it, that no other
account and never the owner account sent anything visible, and that nothing a person reads mentions a router.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom import constants
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.delivery_gateway import SendTextRequest
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, _VoiceNormalizationResult
from mindroom.response_sources import Owned, SourceFate
from mindroom.routing_judgment import ResponderSelection
from tests.conftest import approval_manager_for_test, bind_runtime_paths, test_runtime_paths
from tests.test_approval_receipt_transport import (  # noqa: F401 - sending_clients is a pytest fixture
    _ROOM,
    _bot,
    _delivery,
    _transport,
    sending_clients,
)
from tests.test_turns_focused import (
    _ROOM_ID,
    _SENDER,
    _build_harness,
    _entity_user_id,
    _Harness,
    _room_with_members,
    _route_as_delivered,
    _scheduled_fire_event,
    _text_event,
    _thread_upload_event,
)

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.event_journal import EventJournalStore
    from mindroom.reply_turn.request import ResponseRequest

pytestmark = pytest.mark.asyncio

OWNER = ROUTER_AGENT_NAME
# The MindRoom accounts of each room, its steward first: the owner account where it is joined, else the agent with
# the lowest Matrix ID.
ROOMS = {
    "one agent, no owner": ("general",),
    "two agents, no owner": ("general", "research"),
    "managed room with the owner": (OWNER, "general", "research"),
}
PATHS = ("text", "voice", "untargeted command", "mentioned command", "scheduled prompt")
# The gated tool call of a path's answering agent has its own row: the approval card is the one visible event, and
# the approval transport, not a turn, sends it.
# The account that answers each path. A room's only agent answers everything. Among two agents the steward routes an
# unaddressed message to the agent its selection names (research here), as it does a scheduled prompt that mentions
# nobody, which general posts; the lowest Matrix ID runs an unaddressed command, and a mention aims a command at the
# agent it names.
# fmt: off
ANSWERS = {
    #                               text        voice       untargeted  mentioned   scheduled
    "one agent, no owner":         ("general",  "general",  "general",  "general",  "general"),
    "two agents, no owner":        ("research", "research", "general",  "research", "research"),
    "managed room with the owner": ("research", "research", "general",  "research", "research"),
}
# fmt: on
_SPOKEN = "what is the weather like"


def _config(tmp_path: Path, accounts: tuple[str, ...]) -> Config:
    agents = {name: AgentConfig(display_name=name.title()) for name in accounts if name != OWNER}
    return bind_runtime_paths(Config(agents=agents), test_runtime_paths(tmp_path / "runtime"))


def _event(config: Config, path: str, accounts: tuple[str, ...]) -> nio.Event:
    """Return the person's event for one path, or the scheduled prompt the agent that saved it sends."""
    if path == "voice":
        return _thread_upload_event("m.audio", event_id="$voice:localhost", origin_server_ts=1_000_000)
    if path == "scheduled prompt":
        return _scheduled_fire_event(config, extra_content={})
    if path == "untargeted command":
        return _text_event("!help")
    if path == "mentioned command":
        aimed_at = "research" if "research" in accounts else "general"
        event = _text_event(f"@mindroom_{aimed_at} !help")
        event.source["content"]["m.mentions"] = {"user_ids": [_entity_user_id(config, aimed_at)]}
        return event
    return _text_event("what is the weather like")


def _transcribed(event: nio.Event) -> _VoiceNormalizationResult:
    """Return the voice note as every account's normalizer transcribes it."""
    content = {
        **event.source["content"],
        "msgtype": "m.text",
        "body": _SPOKEN,
        constants.SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
        constants.ORIGINAL_SENDER_KEY: _SENDER,
        constants.VOICE_TRANSCRIPT_KEY: True,
    }
    transcript = PreparedIngress(
        sender=_SENDER,
        event_id=event.event_id,
        body=_SPOKEN,
        source={**event.source, "content": content},
        payload_trusted=True,
    )
    return _VoiceNormalizationResult(event=transcript)


async def _deliver(harness: _Harness, room: nio.MatrixRoom, event: nio.Event) -> None:
    if event.source["content"].get("msgtype") == "m.audio":
        await harness.ingress.handle_media_event(room, event)
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
    else:
        await harness.deliver(room, event)


# The messages the answering account sends the room on each path: (answers, others). An answer is the one final
# message of its reply; a voice turn also sends the transcript, and a command sends its result and no answer.
SHOWN = {
    "text": (1, 0),
    "voice": (1, 1),
    "untargeted command": (0, 1),
    "mentioned command": (0, 1),
    "scheduled prompt": (1, 0),
}


_ANSWER = "Here is the answer."


def _posts_its_answer(harness: _Harness) -> None:
    """Have the stand-in runner send its reply's final message once the real source gate lets the reply through.

    The harness holds no model, so the answer's text is fixed; a reply the gate ends sends nothing, as in production.
    """
    generate = harness.runner.generate_response

    async def generate_and_post(request: ResponseRequest) -> SourceFate:
        fate = await generate(request)
        if isinstance(fate, Owned):
            target = request.response_envelope.target
            await harness.gateway.send_text(SendTextRequest(target=target, response_text=_ANSWER))
        return fate

    harness.runner.generate_response = generate_and_post


def _shown(harness: _Harness) -> tuple[int, int, int, int]:
    """Return what this account sent the room: its answers, its other messages, its edits and its reactions."""
    gateway = harness.gateway
    answers = [sent for sent in gateway.sent if sent.response_text == _ANSWER]
    return (len(answers), len(gateway.sent) - len(answers), len(gateway.edited), len(gateway.reactions))


async def _run_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_store: EventJournalStore,
    room_name: str,
    path: str,
) -> tuple[dict[str, _Harness], nio.Event]:
    """Deliver one path's event to every account of the room, and each route to every account but its steward."""
    accounts = ROOMS[room_name]
    config = _config(tmp_path, accounts)
    room = _room_with_members(config, *accounts)
    room.members_synced = True
    event = _event(config, path, accounts)
    monkeypatch.setattr(InboundTurnNormalizer, "prepare_voice_event", AsyncMock(return_value=_transcribed(event)))
    select = AsyncMock(return_value=ResponderSelection("research"))
    monkeypatch.setattr("mindroom.router.relay.suggest_responder_for_message", select)
    harnesses: dict[str, _Harness] = {}
    for name in accounts:
        harness = harnesses[name] = _build_harness(config, tmp_path / name, agent_name=name)
        _posts_its_answer(harness)
        harness.runtime.orchestrator.journal_store = journal_store
        harness.runtime.orchestrator.steward = accounts[0]

    for harness in harnesses.values():
        await _deliver(harness, room, event)
    # Each hidden route a steward sent reaches every other account, as sync delivers it.
    for steward, harness in harnesses.items():
        for number, content in enumerate(harness.gateway.routes):
            route = _route_as_delivered(config, content, steward=steward, event_id=f"$route-{number}:localhost")
            for name, other in harnesses.items():
                if name != steward:
                    await _deliver(other, room, route)
    return harnesses, event


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("room_name", ROOMS)
async def test_one_account_answers_and_no_other_account_shows_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_store: EventJournalStore,
    room_name: str,
    path: str,
) -> None:
    """Each path gets exactly what the table says from the account it names, and nothing from anyone else."""
    accounts = ROOMS[room_name]
    answerer = ANSWERS[room_name][PATHS.index(path)]

    harnesses, event = await _run_cell(tmp_path, monkeypatch, journal_store, room_name, path)

    # Every message sent, edit and reaction is counted, so one more of any of them from any account fails the cell.
    assert {name: _shown(harness) for name, harness in harnesses.items()} == {
        name: (*SHOWN[path], 0, 0) if name == answerer else (0, 0, 0, 0) for name in accounts
    }
    answering = harnesses[answerer]
    # Each answer sent is one reply's, so no reply started without sending its answer.
    assert len(answering.runner.requests) == SHOWN[path][0]
    for request in answering.runner.requests:
        envelope = request.response_envelope
        assert (envelope.agent_name, envelope.requester_id, envelope.room_id) == (answerer, _SENDER, _ROOM_ID)
        assert envelope.target.reply_to_event_id == event.event_id
    for sent in answering.gateway.sent:
        assert sent.target.room_id == _ROOM_ID
        if sent.response_text == _ANSWER:
            assert sent.target.reply_to_event_id == event.event_id
            continue
        assert "router" not in sent.response_text.lower()
        # The one message a voice turn sends is the transcript, marked so that no account takes it as a turn.
        marked = (sent.extra_content or {}).get(constants.VISIBLE_ROUTER_VOICE_ECHO_KEY) is True
        assert (marked, _SPOKEN in sent.response_text) == ((path == "voice"),) * 2
    assert [name for name, harness in harnesses.items() if harness.gateway.routes] in ([], [accounts[0]])
    assert [harness.gateway.failed_dispatches for harness in harnesses.values()] == [[]] * len(accounts)


@pytest.mark.parametrize("edits_event_id", [None, "$card"])
@pytest.mark.parametrize("room_name", ROOMS)
async def test_a_gated_tool_call_shows_one_card_from_the_agent_whose_turn_asks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_store: EventJournalStore,
    sending_clients: list[nio.AsyncClient],  # noqa: F811
    room_name: str,
    edits_event_id: str | None,
) -> None:
    """The agent whose turn answers a person sends the card its gated tool call needs, and the card's closing edit."""
    harnesses, _ = await _run_cell(tmp_path, monkeypatch, journal_store, room_name, "text")
    [envelope] = [request.response_envelope for harness in harnesses.values() for request in harness.runner.requests]
    bots = {name: _bot(name) for name in ROOMS[room_name]}
    manager = approval_manager_for_test(
        test_runtime_paths(tmp_path / "approvals"),
        prepare_approval_event=AsyncMock(side_effect=lambda _room_id, _thread_id, content, _sender: content),
        responder_principal_id=lambda entity: f"{entity}@{bots[entity].client.user_id}",
    )

    # The turn's own entity asks, as the reply that pauses on a gated call does.
    card = await manager.prepare_reply_card(
        approval_id="card-1",
        continuation_id="card-1",
        continuation_generation=0,
        entity_name=envelope.agent_name,
        response_event_id="$reply",
        tool_call_id="call-1",
        tool_name="shell",
        arguments={"command": "ls"},
        room_id=_ROOM,
        requester_id=envelope.requester_id,
        approver_user_id=envelope.requester_id,
        expires_at_ns=time.time_ns() + 60_000_000_000,
        agent_name=envelope.agent_name,
        thread_id=None,
        grant_operation=None,
    )
    assert card is not None
    transport = _transport(bots, card.sender_principal_id)
    send, _resolve, _device_id = await transport.pinned_delivery("card-1")
    await send(_delivery(edits_event_id=edits_event_id))

    assert sending_clients == [bots[envelope.agent_name].client]
