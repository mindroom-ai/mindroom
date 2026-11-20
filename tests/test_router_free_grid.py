"""Who answers a person in a room, by every path a message takes, with no router account to do it.

Each room holds one person and the MindRoom accounts its row names, every account runs its own turn owners over one
shared journal, and each event arrives at every account as sync delivers it. One table says which account answers
each path; every cell then checks that exactly one answer exists, that the answering account sent it, that no other
account and never the owner account sent anything visible, and that nothing a person reads mentions a router.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mindroom import constants
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.inbound_turn_normalizer import InboundTurnNormalizer, _VoiceNormalizationResult
from mindroom.routing_judgment import ResponderSelection
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.test_approval_receipt_transport import (  # noqa: F401 - sending_clients is a pytest fixture
    _bot,
    _delivery,
    _transport,
    sending_clients,
)
from tests.test_turns_focused import (
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


def _visible(harness: _Harness) -> list[Any]:
    """Return everything this account showed the room: replies it started, messages it sent, and its edits."""
    return [*harness.runner.requests, *harness.gateway.sent, *harness.gateway.edited]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("room_name", ROOMS)
async def test_one_account_answers_and_no_other_account_shows_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_store: EventJournalStore,
    room_name: str,
    path: str,
) -> None:
    """Each path gets exactly one answer, from the account the table names, and nothing from anyone else."""
    accounts = ROOMS[room_name]
    answerer = ANSWERS[room_name][PATHS.index(path)]
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
        harness.runtime.orchestrator.journal_store = journal_store
        harness.runtime.orchestrator.steward = accounts[0]

    for name, harness in harnesses.items():
        if event.sender != _entity_user_id(config, name) or path == "scheduled prompt":
            await _deliver(harness, room, event)
    # Each hidden route a steward sent reaches every other account, as sync delivers it.
    for steward, harness in harnesses.items():
        for number, content in enumerate(harness.gateway.routes):
            route = _route_as_delivered(config, content, steward=steward, event_id=f"$route-{number}:localhost")
            for name, other in harnesses.items():
                if name != steward:
                    await _deliver(other, room, route)

    shown = {name: _visible(harness) for name, harness in harnesses.items()}
    answers = {
        name: harness.gateway.sent if "command" in path else harness.runner.requests
        for name, harness in harnesses.items()
    }
    assert {name: len(found) for name, found in answers.items() if found} == {answerer: 1}
    assert [name for name, found in shown.items() if found] == [answerer]
    assert [name for name, harness in harnesses.items() if harness.gateway.routes] in ([], [accounts[0]])
    for harness in harnesses.values():
        assert harness.gateway.failed_dispatches == []
        assert not [sent for sent in harness.gateway.sent if "router" in sent.response_text.lower()]


@pytest.mark.parametrize("edits_event_id", [None, "$card"])
@pytest.mark.parametrize("room_name", ROOMS)
async def test_a_gated_tool_call_shows_one_card_from_the_agent_that_asks(
    sending_clients: list[nio.AsyncClient],  # noqa: F811
    room_name: str,
    edits_event_id: str | None,
) -> None:
    """The agent whose tool call waits for approval sends the card and its closing edit; no other account does."""
    accounts = ROOMS[room_name]
    asking = accounts[-1]
    bots = {name: _bot(name) for name in accounts}
    asked_by = f"{asking}@{bots[asking].client.user_id}"

    send, _resolve, _device_id = await _transport(bots, {"card-1": asked_by}).pinned_delivery("card-1")
    await send(_delivery(edits_event_id=edits_event_id))

    assert sending_clients == [bots[asking].client]
