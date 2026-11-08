"""Plugin routes post to a room as the router: a notice that wakes no agent, or a relay for the signed-in user.

A relay names its user as the original sender, so each agent it mentions answers that user under its access rules, and
it checks the user again just before it sends. Nothing is ever posted from the user's account.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
from fastapi import FastAPI, HTTPException, Request

from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    EXACT_MENTIONS_KEY,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    HOOK_SOURCE_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
)
from mindroom.event_journal.models import EventClass
from mindroom.hooks import HookContextSupport, HookRegistry
from mindroom.hooks.registry import HookRegistryState
from mindroom.logging_config import get_logger
from mindroom.matrix.journal_ingress import ingestion_timeline_views
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.plugin_http import RoomMessenger, require_room_messenger
from mindroom.thread_utils import check_agent_mentioned
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY
from tests.api.test_plugin_room_readers import ALICE, BOB, CAROL, ROOM, _joined, _state
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_conversation_reader_mock,
    publish_test_plugins,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from mindroom.bot import AgentBot

PLUGIN = "asker"
ROUTER = "@mindroom_router:localhost"
OPEN = "@mindroom_open:localhost"
ALICES = "@mindroom_alices:localhost"
ELSEWHERE = "@mindroom_elsewhere:localhost"
CREW = "@mindroom_crew:localhost"
DAVE = "@dave:example.org"
MEMBERS = (ALICE, CAROL, BOB, ROUTER, OPEN, ALICES, CREW)
# Alice administers the room; posting a message takes level 50, which Carol has and Bob lacks.
POWER = {"users": {ALICE: 100, CAROL: 50, ROUTER: 100}, "events": {"m.room.message": 50}}


def _config(tmp_path: Path, reserved: Mapping[str, list[str]] | None = None) -> Config:
    """Return the room's agents and team, an agent outside it, and the loaded plugins ``reserved`` names.

    ``reserved`` maps each plugin to its reserved namespaces; by default ``asker`` and ``cards`` each reserve their own.
    Agent ``open`` answers anyone, ``alices`` only Alice, and the team ``crew`` holds both; ``asker`` serves the routes.
    """
    if reserved is None:
        reserved = {name: [f"com.example.{name}."] for name in (PLUGIN, "cards")}
    for name, namespaces in reserved.items():
        root = tmp_path / "plugins" / name
        root.mkdir(parents=True)
        manifest = {"name": name, "reserved_namespaces": namespaces}
        (root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={
                "open": AgentConfig(
                    display_name="Open",
                    rooms=[ROOM],
                    access=ResponderAccessConfig(users=["*"]),
                ),
                "alices": AgentConfig(
                    display_name="Alices",
                    rooms=[ROOM],
                    access=ResponderAccessConfig(current_room_members=False, members_of_rooms=[], users=[ALICE]),
                ),
                "elsewhere": AgentConfig(display_name="Elsewhere", rooms=["!other:example.org"]),
            },
            teams={"crew": TeamConfig(display_name="Crew", role="Both", agents=["open", "alices"], rooms=[ROOM])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            plugins=[f"./plugins/{name}" for name in reserved],
        ),
        test_runtime_paths(tmp_path),
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    publish_test_plugins(config, runtime_paths_for(config))
    return config


def _reader_client(*, joined: bool = True) -> SimpleNamespace:
    """The router's client as room reads see it: joined to the room with ``MEMBERS`` and ``POWER``."""
    return SimpleNamespace(
        user_id=ROUTER,
        rooms={ROOM: nio.MatrixRoom(ROOM, ROUTER)} if joined else {},
        joined_members=AsyncMock(return_value=_joined(ROOM, dict.fromkeys(MEMBERS))),
        room_get_state=AsyncMock(return_value=_state(power_levels=POWER)),
    )


def _router(config: Config, client: SimpleNamespace) -> SimpleNamespace:
    """A running router bot whose hook sender sends through ``client``."""
    runtime_paths = runtime_paths_for(config)
    support = HookContextSupport(
        runtime=SimpleNamespace(client=client, config=config),
        runtime_paths=runtime_paths,
        agent_name=ROUTER_AGENT_NAME,
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        conversation_reader=make_conversation_reader_mock(),
        logger=get_logger("tests.plugin_room_messenger"),
    )
    return SimpleNamespace(
        client=client,
        running=True,
        config=config,
        runtime_paths=runtime_paths,
        hook_context=support,
    )


@dataclass
class _World:
    """A room with Alice, Carol and Bob, the router, and agents ``open`` and ``alices``, served by one API app."""

    config: Config
    app: FastAPI
    client: SimpleNamespace
    bots: dict[str, SimpleNamespace]
    tokens: list[str]

    async def messenger(self, user_id: str, plugin: str = PLUGIN) -> RoomMessenger:
        """The messenger a route of ``plugin`` gets, signed in with a client session of ``user_id``."""
        token = client_session_store(self.app).create(user_id).token
        self.tokens.append(token)
        request = Request(
            {
                "type": "http",
                "app": self.app,
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                PLUGIN_NAME_SCOPE_KEY: plugin,
            },
        )
        return await require_room_messenger(request, ROOM)


def _world(config: Config) -> _World:
    """The room, its router and agents, and the API app whose routes post there."""
    client = _reader_client()
    # Agent ``open``'s bot reads the room too, so reads still answer while the router is down.
    bots = {ROUTER_AGENT_NAME: _router(config, client), "open": SimpleNamespace(client=client, running=True)}
    app = FastAPI()
    bind_room_members(app, bots)  # type: ignore[arg-type]
    ensure_app_state(app).embedded = SimpleNamespace(agent_bot=bots.get)  # type: ignore[assignment]
    return _World(config, app, client, bots, [])


@pytest.fixture
def world(tmp_path: Path) -> _World:
    """The room as ``_world`` serves it, with the default plugins of ``_config``."""
    return _world(_config(tmp_path))


@contextmanager
def _transport(event_id: str | None = "$sent") -> Iterator[AsyncMock]:
    """Capture what the router's hook sender sends; it delivers as ``event_id``, or fails when that is None."""
    transport = AsyncMock(side_effect=delivered_matrix_side_effect(event_id) if event_id else None, return_value=None)
    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        yield transport


def _sent(transport: AsyncMock, client: object) -> dict[str, Any]:
    """The content of the one message the router sent, through ``client`` into the room."""
    transport.assert_awaited_once()
    sent_client, room_id, content = transport.await_args.args
    assert (sent_client, room_id) == (client, ROOM)
    return content


def _agent(config: Config, user_id: str) -> AgentBot:
    """The bot of the agent with ``user_id``, as it reads the room's messages."""
    name = user_id.removeprefix("@mindroom_").split(":")[0]
    bot = make_test_agent_bot(
        AgentMatrixUser(agent_name=name, user_id=user_id, display_name=name.title(), password=TEST_PASSWORD),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = user_id
    bot.client.rooms = {}
    return bot


def _event(content: dict[str, Any]) -> nio.RoomMessageText:
    return nio.Event.parse_event(
        {
            "type": "m.room.message",
            "content": content,
            "sender": ROUTER,
            "event_id": "$sent",
            "room_id": ROOM,
            "origin_server_ts": 1,
        },
    )


def _room() -> nio.MatrixRoom:
    room = nio.MatrixRoom(ROOM, ROUTER)
    for user_id in (ALICE, CAROL, BOB, ROUTER, OPEN, ALICES):
        room.add_member(user_id, None, None)
    room.members_synced = True
    return room


def _event_class(agent_id: str, content: dict[str, Any]) -> EventClass:
    """Whether an agent takes the message as work, or only as context, as it arrives live."""
    views = ingestion_timeline_views(
        room_id=ROOM,
        source=_event(content).source,
        self_sender=agent_id,
        provenance=nio.TimelineEventProvenance.LIVE,
    )
    assert views is not None
    return views[0].event_class


def _answers(config: Config, agent_id: str, content: dict[str, Any]) -> tuple[str, bool]:
    """Who the agent's turn for the message runs as, and whether the agent's access rules let it answer them here."""
    bot = _agent(config, agent_id)
    requester = bot._ingress_validator.classify(_event(content), room_id=ROOM).run_requester_id
    return requester, bot._turn_policy.can_reply_to_sender_in_room(requester, ROOM)


@pytest.mark.asyncio
async def test_notice_is_the_routers_and_wakes_no_agent(world: _World) -> None:
    """A notice is the router's own `m.notice`: it mentions no one, claims no requester, and no agent takes it as work.

    The room has the default `auto` reply mode, where a person's message with no mention reaches an agent.
    """
    messenger = await world.messenger(CAROL)

    with _transport() as transport:
        event_id = await messenger.notice("Build 12 finished", content={"com.example.asker.card": {"build": 12}})

    assert event_id == "$sent"
    content = _sent(transport, world.client)
    assert content["msgtype"] == "m.notice"
    assert content["body"] == "Build 12 finished"
    assert content["m.mentions"] == {"user_ids": []}
    assert content[EXACT_MENTIONS_KEY] is True
    assert content[SOURCE_KIND_KEY] == "hook"
    assert content[HOOK_SOURCE_KEY] == f"{PLUGIN}:http"
    assert content[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] == 1
    assert ORIGINAL_SENDER_KEY not in content
    assert content["com.example.asker.card"] == {"build": 12}
    assert world.config.get_room_reply_mode(runtime_paths_for(world.config), ROOM) == "auto"
    assert _event_class(OPEN, content) is EventClass.CONTEXT_ONLY
    assert _answers(world.config, OPEN, content)[0] == ROUTER


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_relay_runs_as_its_user_for_exactly_the_agent_it_mentions(world: _World) -> None:
    """Carol's relay names her and wakes ``open`` for her; ``alices`` refuses her, and answers Alice's own relay."""
    messenger = await world.messenger(CAROL)

    with _transport() as transport:
        event_id = await messenger.relay(
            "@open what changed?",
            mentions=[OPEN],
            content={"com.example.asker.ask": {"n": 1}},
        )

    assert event_id == "$sent"
    content = _sent(transport, world.client)
    assert content["msgtype"] == "m.text"
    assert content["body"] == f"Sent for {CAROL}:\n\n{OPEN} what changed?"
    assert content[ORIGINAL_SENDER_KEY] == CAROL
    assert content["m.mentions"] == {"user_ids": [OPEN]}
    assert content[EXACT_MENTIONS_KEY] is True
    assert content[SOURCE_KIND_KEY] == "hook"
    assert content[HOOK_SOURCE_KEY] == f"{PLUGIN}:http"
    assert content[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] == 1
    assert content["com.example.asker.ask"] == {"n": 1}
    assert _event_class(OPEN, content) is EventClass.ACTIONABLE
    mentioned, _, _ = check_agent_mentioned(
        {"content": content},
        None,
        world.config,
        runtime_paths_for(world.config),
        room=_room(),
    )
    assert [agent.full_id for agent in mentioned] == [OPEN]
    assert _answers(world.config, OPEN, content) == (CAROL, True)
    assert _answers(world.config, ALICES, content) == (CAROL, False)

    alices_messenger = await world.messenger(ALICE)
    with _transport() as transport:
        await alices_messenger.relay("@alices what changed?", mentions=[ALICES])
    assert _answers(world.config, ALICES, _sent(transport, world.client)) == (ALICE, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mentions",
    [[], [ALICE], [ELSEWHERE], [ROUTER], [OPEN, DAVE]],
    ids=["nobody", "a-person", "an-agent-outside-the-room", "the-router", "an-agent-and-a-stranger"],
)
async def test_relay_mentions_only_agents_and_teams_in_the_room(world: _World, mentions: list[str]) -> None:
    """A relay that would mention a person, an agent outside the room or the router, or no one, sends nothing."""
    messenger = await world.messenger(CAROL)

    with _transport() as transport, pytest.raises(HTTPException) as refused:
        await messenger.relay("hello", mentions=mentions)

    assert refused.value.status_code == 400
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_keeps_the_text_as_written_below_whom_it_is_sent_for(world: _World) -> None:
    """Text that opens with a Markdown block still renders as that block, in the thread the route names."""
    messenger = await world.messenger(CAROL)
    # The router's conversation knows the thread by its root alone.
    world.bots[ROUTER_AGENT_NAME].hook_context.conversation_reader.latest_thread_event_id.return_value = "$root"

    with _transport() as transport:
        await messenger.relay("```python\nprint(1)\n```\nWhy?", mentions=[OPEN], thread_id="$root")

    content = _sent(transport, world.client)
    first, code, rest = content["formatted_body"].partition('</p>\n<pre><code class="language-python">')
    assert first.startswith("<p>Sent for ")
    assert code
    assert rest.endswith("</code></pre>\n<p>Why?</p>\n")
    assert content["m.relates_to"]["rel_type"] == "m.thread"
    assert content["m.relates_to"]["event_id"] == "$root"


@pytest.mark.asyncio
async def test_relay_may_wake_a_team_in_the_room_once(world: _World) -> None:
    """A team joined to the room is mentioned like an agent, and an entity named twice is mentioned once."""
    messenger = await world.messenger(CAROL)

    with _transport() as transport:
        await messenger.relay("@crew what changed?", mentions=[CREW, OPEN, CREW])

    assert _sent(transport, world.client)["m.mentions"] == {"user_ids": [CREW, OPEN]}


def _expire(world: _World) -> None:
    client_session_store(world.app).clock = lambda: float("inf")


def _revoke(world: _World) -> None:
    for token in world.tokens:
        client_session_store(world.app).revoke(token)


def _revoke_while_the_room_is_read(world: _World) -> None:
    state = world.client.room_get_state.return_value

    async def read_and_revoke(*_args: object) -> nio.RoomGetStateResponse:
        _revoke(world)
        return state

    world.client.room_get_state.side_effect = read_and_revoke


def _leave(world: _World) -> None:
    world.client.joined_members.return_value = _joined(ROOM, dict.fromkeys((ALICE, BOB, ROUTER, OPEN)))


def _demote(world: _World) -> None:
    world.client.room_get_state.return_value = _state(power_levels={**POWER, "users": {ALICE: 100, ROUTER: 100}})


def _lose_membership_read(world: _World) -> None:
    world.client.joined_members.side_effect = RuntimeError("homeserver down")


def _lose_power_read(world: _World) -> None:
    world.client.room_get_state.side_effect = RuntimeError("homeserver down")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "status"),
    [
        (_expire, 401),
        (_revoke, 401),
        (_revoke_while_the_room_is_read, 401),
        (_leave, 403),
        (_demote, 403),
        (_lose_membership_read, 503),
        (_lose_power_read, 503),
    ],
    ids=[
        "session-expired",
        "session-revoked",
        "session-revoked-during-the-reads",
        "left-the-room",
        "lost-power-to-post",
        "membership-unreadable",
        "power-unreadable",
    ],
)
async def test_relay_checks_its_user_again_before_it_sends(
    world: _World,
    change: Callable[[_World], None],
    status: int,
) -> None:
    """Between the route's check and the relay, an ended session, a departure or lost power stops the relay."""
    messenger = await world.messenger(CAROL)
    change(world)

    with _transport() as transport, pytest.raises(HTTPException) as refused:
        await messenger.relay("@open what changed?", mentions=[OPEN])

    assert refused.value.status_code == status
    transport.assert_not_awaited()


def _post(messenger: RoomMessenger, kind: str, content: dict[str, Any] | None = None) -> Awaitable[str]:
    """Post a notice, or a relay that mentions ``open``, with ``content``."""
    if kind == "notice":
        return messenger.notice("hello", content=content)
    return messenger.relay("@open hello", mentions=[OPEN], content=content)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notice", "relay"])
@pytest.mark.parametrize(
    "content",
    [
        {ORIGINAL_SENDER_KEY: ALICE},
        {"io.mindroom.exact_mentions": False},
        {"com.example.card": {"m.new_content": {ORIGINAL_SENDER_KEY: ALICE}}},
        {"com.example.cards": [{"io.mindroom.wake_mentioned": False}]},
        {"body": f"Sent for {ALICE}: hello"},
        {"msgtype": "m.text"},
        {"m.relates_to": {"rel_type": "m.replace", "event_id": "$alices_relay"}},
        {"m.new_content": {"msgtype": "m.text", "body": "forged"}},
        {"org.matrix.msc1767.text": "forged"},
        {"com.example.cards.card": {"approved": True}},
        {"com.example.asker.card": {"com.example.cards.card": {"approved": True}}},
        {"com.example.asker.card": {"chat.mindroom.call_failure": {"version": 1}}},
    ],
    ids=[
        "original-sender",
        "io-key",
        "nested",
        "in-a-list",
        "body",
        "msgtype",
        "edit",
        "new-content",
        "unstable",
        "another-plugins-key",
        "another-plugins-key-nested",
        "chat-client-key-nested",
    ],
)
async def test_content_holds_only_the_plugins_own_keys(world: _World, kind: str, content: dict[str, Any]) -> None:
    """A Matrix key, or a key MindRoom or another plugin reserves at any depth, is refused before anything is sent.

    So content a client passes through cannot say the message is someone else's, turn it into an edit of another, or
    forge another plugin's card.
    """
    messenger = await world.messenger(CAROL)

    with _transport() as transport, pytest.raises(HTTPException) as refused:
        await _post(messenger, kind, content)

    assert refused.value.status_code == 400
    transport.assert_not_awaited()


# Plugins ``asker`` and ``twin`` share a namespace, ``twin`` reserves one more, and ``narrow`` a narrower one inside it.
_SHARED = {
    PLUGIN: ["com.example.shared."],
    "twin": ["com.example.shared.", "com.example.twin."],
    "narrow": ["com.example.shared.narrow."],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notice", "relay"])
@pytest.mark.parametrize("plugin", [PLUGIN, "twin"])
async def test_plugins_sharing_a_namespace_each_post_their_keys_in_it(tmp_path: Path, kind: str, plugin: str) -> None:
    """A namespace the posting plugin reserves is its own, though another loaded plugin reserves it as well."""
    world = _world(_config(tmp_path, _SHARED))
    messenger = await world.messenger(CAROL, plugin)

    with _transport() as transport:
        await _post(messenger, kind, {"com.example.shared.card": {"plugin": plugin}})

    assert _sent(transport, world.client)["com.example.shared.card"] == {"plugin": plugin}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notice", "relay"])
@pytest.mark.parametrize(
    ("plugin", "key"),
    [
        (PLUGIN, "com.example.twin.card"),
        (PLUGIN, "com.example.shared.narrow.card"),
        ("narrow", "com.example.shared.narrow.card"),
    ],
    ids=["only-another-plugins", "narrower-inside-own", "own-inside-broader"],
)
async def test_sharing_a_namespace_leaves_the_others_refused(tmp_path: Path, kind: str, plugin: str, key: str) -> None:
    """A namespace only another plugin reserves stays refused, whether inside the poster's own or around it."""
    world = _world(_config(tmp_path, _SHARED))
    messenger = await world.messenger(CAROL, plugin)

    with _transport() as transport, pytest.raises(HTTPException) as refused:
        await _post(messenger, kind, {key: {}})

    assert refused.value.status_code == 400
    assert refused.value.detail == f"content must not set keys MindRoom or another plugin reserves ({key})."
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_member_gets_no_messenger(world: _World) -> None:
    """The route's dependency refuses a caller who is not joined to the room, as `require_room_member` does."""
    with pytest.raises(HTTPException) as refused:
        await world.messenger(DAVE)

    assert refused.value.status_code == 403


def _stop_router(world: _World) -> None:
    world.bots[ROUTER_AGENT_NAME].running = False


def _remove_router(world: _World) -> None:
    del world.bots[ROUTER_AGENT_NAME]


def _router_out_of_the_room(world: _World) -> None:
    world.bots[ROUTER_AGENT_NAME] = _router(world.config, _reader_client(joined=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notice", "relay"])
@pytest.mark.parametrize(
    "change",
    [_stop_router, _remove_router, _router_out_of_the_room],
    ids=["router-stopped", "router-gone", "router-not-in-the-room"],
)
async def test_messages_need_a_running_router(world: _World, kind: str, change: Callable[[_World], None]) -> None:
    """With no running router in the room, a notice or a relay answers 503 and sends nothing."""
    messenger = await world.messenger(CAROL)
    change(world)

    with _transport() as transport, pytest.raises(HTTPException) as refused:
        await _post(messenger, kind)

    assert refused.value.status_code == 503
    transport.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notice", "relay"])
async def test_failed_send_answers_503(world: _World, kind: str) -> None:
    """A send Matrix does not deliver answers 503 rather than an event ID."""
    messenger = await world.messenger(CAROL)

    with _transport(None) as transport, pytest.raises(HTTPException) as refused:
        await _post(messenger, kind)

    assert refused.value.status_code == 503
    transport.assert_awaited_once()


@pytest.mark.asyncio
async def test_messages_go_through_the_router_running_when_they_are_sent(world: _World) -> None:
    """A router restarted after the route's check, as by a reload, sends with its new client; nothing keeps the old."""
    messenger = await world.messenger(CAROL)
    new_client = _reader_client()
    world.bots[ROUTER_AGENT_NAME] = _router(world.config, new_client)

    with _transport() as transport:
        await messenger.notice("hello")

    _sent(transport, new_client)
