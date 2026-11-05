"""Tests for a room's reply mode: its config field, its `!reply_mode` override, and who answers under each mode."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.attachments.storage import _attachment_id_for_event, register_local_attachment
from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, command_parser, get_command_help
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
)
from mindroom.entity_resolution import current_entity_id
from mindroom.llm.media import Audio
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.room_reply_modes import (
    _store_path,
    clear_room_reply_mode_override,
    resolve_room_reply_mode_override,
    set_room_reply_mode_override,
)
from tests.authorization_helpers import make_test_command_handler_context
from tests.bot_helpers import _room_file_event, make_test_agent_bot, unique_room_send_responses
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_journal_support,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_visible_message,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.matrix_state_helpers import save_matrix_state
from tests.response_attempt_helpers import install_direct_response_admission
from tests.runtime_fakes import ready_runtime
from tests.test_turn_policy import _context, _dispatch, _entity_id, _policy_for, _room_with_members, _text_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.agent import RoomReplyMode
    from mindroom.constants import RuntimePaths

ROOM_ID = "!test:localhost"
SENDER = "@user:localhost"
OTHER_HUMAN = "@other:localhost"
ADMIN = "@admin:localhost"
THREAD_ID = "$thread:localhost"


@dataclass(frozen=True)
class _CommandEvent:
    sender: str
    event_id: str
    body: str
    source: dict[str, dict[str, str]]


def _config(tmp_path: Path, *, reply_mode: RoomReplyMode | None = None, room_key: str = ROOM_ID) -> Config:
    rooms = {} if reply_mode is None else {room_key: RoomConfig(reply_mode=reply_mode)}
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General"),
                "research": AgentConfig(display_name="Research"),
            },
            teams={"crew": TeamConfig(display_name="Crew", role="Answers together", agents=["general", "research"])},
            rooms=rooms,
        ),
        test_runtime_paths(tmp_path),
    )


def _command_context(config: Config) -> CommandHandlerContext:
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"users": {ADMIN: 100}, "users_default": 0},
        event_type="m.room.power_levels",
        state_key="",
        room_id=ROOM_ID,
    )
    return make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM_ID, None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
    )


async def _run_reply_mode_command(context: CommandHandlerContext, sender: str, args_text: str) -> str:
    """Run `!reply_mode <args_text>` as ``sender`` and return the reply."""
    body = f"!reply_mode {args_text}".strip()
    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_CommandEvent(sender=sender, event_id="$event", body=body, source={"content": {"body": body}}),
        command=Command(type=CommandType.REPLY_MODE, args={"args_text": args_text}, raw_text=body),
        requester_user_id=sender,
    )
    return context.send_response.await_args.args[0]


def test_room_reply_mode_defaults_to_auto_and_rejects_unknown_modes() -> None:
    """`rooms.<room>.reply_mode` is `auto` unless set to `mentions`, and no other value validates."""
    assert RoomConfig().reply_mode == "auto"
    assert RoomConfig(reply_mode="mentions").reply_mode == "mentions"
    with pytest.raises(ValidationError, match="reply_mode"):
        RoomConfig(reply_mode="never")


def test_room_reply_mode_store_roundtrip_drops_invalid_records(tmp_path: Path) -> None:
    """The override persists outside config.yaml, and a record with an unknown mode reads as no override."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"!other:localhost": {"mode": "never", "set_at": "2026-11-04T00:00:00+00:00"}}),
        encoding="utf-8",
    )

    assert resolve_room_reply_mode_override(runtime_paths, "!other:localhost") is None
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None
    set_room_reply_mode_override(runtime_paths, room_id=ROOM_ID, mode="mentions")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    assert clear_room_reply_mode_override(runtime_paths, ROOM_ID) is True
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None
    assert clear_room_reply_mode_override(runtime_paths, ROOM_ID) is False


def test_room_reply_mode_reads_the_room_entry_by_room_key(tmp_path: Path) -> None:
    """A `rooms` entry keyed by a managed room's key sets that room's mode, and every other room stays `auto`."""
    config = _config(tmp_path, reply_mode="mentions", room_key="lobby")
    runtime_paths = runtime_paths_for(config)
    state = MatrixState()
    state.add_room("lobby", ROOM_ID, "#lobby:localhost", "Lobby")
    save_matrix_state(state, runtime_paths)

    assert config.get_room_reply_mode(runtime_paths, ROOM_ID) == "mentions"
    assert config.get_room_reply_mode(runtime_paths, "!other:localhost") == "auto"


@pytest.mark.parametrize(("configured", "override"), [("auto", "mentions"), ("mentions", "auto")])
def test_room_reply_mode_override_beats_config(tmp_path: Path, configured: str, override: str) -> None:
    """The `!reply_mode` override decides over `rooms.<room>.reply_mode` until it is reset."""
    config = _config(tmp_path, reply_mode=configured)
    runtime_paths = runtime_paths_for(config)

    set_room_reply_mode_override(runtime_paths, room_id=ROOM_ID, mode=override)
    assert config.get_room_reply_mode(runtime_paths, ROOM_ID) == override
    clear_room_reply_mode_override(runtime_paths, ROOM_ID)
    assert config.get_room_reply_mode(runtime_paths, ROOM_ID) == configured


def test_reply_mode_command_parsing_and_help() -> None:
    """`!reply_mode` parses with or without a mode in every spelling, and `!help` explains it."""
    for text, args_text in (("!reply_mode", ""), ("!reply-mode mentions", "mentions"), ("!replymode auto", "auto")):
        command = command_parser.parse(text)
        assert command is not None
        assert command.type == CommandType.REPLY_MODE
        assert command.args["args_text"] == args_text
    for topic in ("reply_mode", "reply-mode", "replymode"):
        assert "**Reply Mode Command**" in get_command_help(topic)


@pytest.mark.asyncio
async def test_reply_mode_command_shows_sets_and_resets_the_override(tmp_path: Path) -> None:
    """Anyone sees the room's mode; only a room admin sets or resets it, and an unknown mode changes nothing."""
    context = _command_context(_config(tmp_path, reply_mode="mentions"))
    runtime_paths = context.runtime_paths

    assert "configured `mentions` reply mode" in await _run_reply_mode_command(context, SENDER, "show")
    assert await _run_reply_mode_command(context, SENDER, "auto") == "❌ Room admin only."
    assert (await _run_reply_mode_command(context, ADMIN, "never")).startswith("❌ Unknown reply mode `never`.")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None

    assert "now uses `auto` reply mode" in await _run_reply_mode_command(context, ADMIN, "auto")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "auto"
    assert "`auto` reply mode override" in await _run_reply_mode_command(context, SENDER, "")
    assert await _run_reply_mode_command(context, SENDER, "reset") == "❌ Room admin only."

    assert "override removed" in await _run_reply_mode_command(context, ADMIN, "reset")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None


@dataclass(frozen=True)
class _Turn:
    """One turn in a room: the agents there, where the turn is, whom it mentions, and what sent it."""

    agents: tuple[str, ...] = ("general",)
    with_router: bool = True
    dm: bool = False
    # Who already wrote in the turn's thread, as entity names or Matrix user IDs; None for a room-level message.
    thread: tuple[str, ...] | None = None
    # Whether that thread's history could be read in full.
    full_history: bool = True
    mention: str | None = None
    source_kind: str = MESSAGE_SOURCE_KIND
    # The bot still answering in the turn's thread, if one is.
    answering: str | None = None


_BOTH = ("general", "research")
# Who answers each turn under each reply mode: each bot that does not ignore it, and how it answers.
_WHO_ANSWERS = [
    pytest.param(_Turn(), {"general": "individual"}, {}, id="untagged-room-message-sole-agent"),
    pytest.param(_Turn(agents=_BOTH), {ROUTER_AGENT_NAME: "route"}, {}, id="untagged-room-message-two-agents"),
    pytest.param(_Turn(agents=_BOTH, with_router=False, dm=True), {"general": "team"}, {}, id="untagged-dm-two-agents"),
    pytest.param(_Turn(thread=(SENDER,)), {"general": "individual"}, {}, id="untagged-thread-of-one-person"),
    pytest.param(
        _Turn(agents=_BOTH, mention="general"),
        {"general": "individual"},
        {"general": "individual"},
        id="mentions-general",
    ),
    pytest.param(
        _Turn(agents=_BOTH, mention=ROUTER_AGENT_NAME),
        {ROUTER_AGENT_NAME: "reject"},
        {ROUTER_AGENT_NAME: "reject"},
        id="mentions-only-the-router",
    ),
    pytest.param(
        _Turn(agents=_BOTH, thread=(SENDER, "general")),
        {"general": "individual"},
        {"general": "individual"},
        id="untagged-follow-up-after-general-replied",
    ),
    pytest.param(
        _Turn(thread=(SENDER,), answering="general"),
        {"general": "individual"},
        {"general": "individual"},
        id="untagged-follow-up-while-general-answers",
    ),
    pytest.param(
        _Turn(agents=("general", "crew"), thread=(SENDER, "crew")),
        {"crew": "individual"},
        {"crew": "individual"},
        id="untagged-follow-up-after-a-team-replied",
    ),
    pytest.param(
        _Turn(thread=(SENDER, ROUTER_AGENT_NAME)),
        {"general": "individual"},
        {},
        id="untagged-follow-up-after-only-the-router-posted",
    ),
    pytest.param(_Turn(thread=(SENDER, OTHER_HUMAN, "general")), {}, {}, id="untagged-follow-up-two-people"),
    pytest.param(
        _Turn(thread=(SENDER, "general"), full_history=False),
        {"general": "individual"},
        {},
        id="untagged-follow-up-in-an-unreadable-thread",
    ),
    pytest.param(
        _Turn(source_kind=SCHEDULED_SOURCE_KIND),
        {"general": "individual"},
        {"general": "individual"},
        id="untagged-scheduled-task",
    ),
    pytest.param(
        _Turn(source_kind=HOOK_DISPATCH_SOURCE_KIND),
        {"general": "individual"},
        {"general": "individual"},
        id="untagged-hook-dispatch",
    ),
    pytest.param(
        _Turn(source_kind=EXTERNAL_TRIGGER_SOURCE_KIND),
        {"general": "individual"},
        {"general": "individual"},
        id="untagged-external-trigger",
    ),
]


async def _answers(config: Config, turn: _Turn) -> dict[str, str]:
    """Plan ``turn`` on every bot in its room and return how each bot that does not ignore it answers."""
    room = _room_with_members(SENDER, *(_entity_id(config, agent).full_id for agent in turn.agents))
    mentioned = [] if turn.mention is None else [_entity_id(config, turn.mention)]
    history = None
    if turn.thread is not None:
        history = [
            make_visible_message(
                sender=sender if sender.startswith("@") else _entity_id(config, sender).full_id,
                body="earlier",
            )
            for sender in turn.thread
        ]
    bots = (ROUTER_AGENT_NAME, *turn.agents) if turn.with_router else turn.agents
    answers: dict[str, str] = {}
    with patch("mindroom.turn_policy.is_dm_room", new=AsyncMock(return_value=turn.dm)):
        for bot in bots:
            context = _context(
                mentioned=mentioned,
                am_i_mentioned=bot == turn.mention,
                thread_id=None if turn.thread is None else THREAD_ID,
                thread_history=history,
                full_history=turn.full_history,
            )
            dispatch = _dispatch(context, agent_name=bot)
            if turn.source_kind != MESSAGE_SOURCE_KIND:
                origin = message_origin(
                    sender_id=_entity_id(config, ROUTER_AGENT_NAME).full_id,
                    requester_id=SENDER,
                    sender_entity_name=ROUTER_AGENT_NAME,
                    source_kind=turn.source_kind,
                )
                dispatch = replace(dispatch, envelope=replace(dispatch.envelope, origin=origin))
            plan = await _policy_for(config, bot).plan_turn(
                room,
                _text_event("hello agents"),
                dispatch,
                has_active_response_for_target=lambda _target, bot=bot: bot == turn.answering,
            )
            if plan.kind == "respond":
                answers[bot] = plan.response_action.kind
            elif plan.kind == "route":
                answers[bot] = "route"
    return answers


@pytest.mark.asyncio
@pytest.mark.parametrize("set_by", ["config", "override"])
@pytest.mark.parametrize("mode", ["auto", "mentions"])
@pytest.mark.parametrize(("turn", "auto", "mentions"), _WHO_ANSWERS)
async def test_reply_mode_decides_who_answers(
    tmp_path: Path,
    set_by: str,
    mode: RoomReplyMode,
    turn: _Turn,
    auto: dict[str, str],
    mentions: dict[str, str],
) -> None:
    """In `mentions` mode only a mention starts a conversation; threads with an agent, and automation, work as in `auto`.

    The mode decides the same way when config sets it and when a `!reply_mode` override sets it over the other mode.
    """
    if set_by == "config":
        config = _config(tmp_path, reply_mode=mode)
    else:
        config = _config(tmp_path, reply_mode="mentions" if mode == "auto" else "auto")
        set_room_reply_mode_override(runtime_paths_for(config), room_id=ROOM_ID, mode=mode)

    assert await _answers(config, turn) == (auto if mode == "auto" else mentions)


@pytest.mark.asyncio
async def test_mentions_room_flow_from_a_mention_to_an_admin_switching_back(tmp_path: Path) -> None:
    """A `mentions` room answers a mention and its thread's follow-ups, until an admin's `auto` brings routing back."""
    config = _config(tmp_path, reply_mode="mentions")
    two_agents = _Turn(agents=_BOTH)

    assert await _answers(config, two_agents) == {}
    assert await _answers(config, replace(two_agents, mention="general")) == {"general": "individual"}
    followed = replace(two_agents, thread=(SENDER, "general"))
    assert await _answers(config, followed) == {"general": "individual"}
    assert await _answers(config, replace(two_agents, thread=(SENDER,))) == {}

    await _run_reply_mode_command(_command_context(config), ADMIN, "auto")

    assert resolve_room_reply_mode_override(runtime_paths_for(config), ROOM_ID) == "auto"
    assert await _answers(config, two_agents) == {ROUTER_AGENT_NAME: "route"}
    assert await _answers(config, followed) == {"general": "individual"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "says"),
    [
        ("auto", "In a new untagged message, automatic routing can still choose an agent or team"),
        ("mentions", "In this room, a new message gets an answer only when it mentions an agent or team."),
    ],
)
async def test_router_only_mention_guidance_follows_the_reply_mode(
    tmp_path: Path,
    mode: RoomReplyMode,
    says: str,
) -> None:
    """A person who mentions only the router learns how a new message gets an answer in this room."""
    turn = _Turn(agents=_BOTH, mention=ROUTER_AGENT_NAME)
    config = _config(tmp_path, reply_mode=mode)
    room = _room_with_members(SENDER, *(_entity_id(config, agent).full_id for agent in turn.agents))
    dispatch = _dispatch(
        _context(mentioned=[_entity_id(config, ROUTER_AGENT_NAME)], am_i_mentioned=True),
        agent_name=ROUTER_AGENT_NAME,
    )

    plan = await _policy_for(config, ROUTER_AGENT_NAME).plan_turn(
        room,
        _text_event("hello router"),
        dispatch,
        has_active_response_for_target=lambda _target: False,
    )

    assert plan.response_action.kind == "reject"
    assert says in plan.response_action.rejection_message
    assert plan.response_action.rejection_message.endswith(
        "The router is not a conversational AI agent you can tag directly.",
    )


# Who acts on what a person sends, driven through each bot's ingress: the agents that answer, how many times each,
# and the router's relays to the agent it picked.
_RELAY = "router relay"
_GENERAL_ONCE = {"general": 1}


def _text_message(
    paths: RuntimePaths,
    event_id: str,
    body: str,
    *,
    mention: str | None = None,
    **content: object,
) -> nio.RoomMessageText:
    if mention is not None:
        content["m.mentions"] = {"user_ids": [current_entity_id(mention, paths).full_id]}
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": SENDER,
            "origin_server_ts": 1000 if event_id == "$caption" else 1,
            "room_id": ROOM_ID,
            "content": {"msgtype": "m.text", "body": body, **content},
        },
    )


def _upload() -> nio.RoomMessageFile:
    return _room_file_event(sender=SENDER, event_id="$file", room_id=ROOM_ID)


async def _send_text(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
    await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$event", "hello"))


async def _send_upload(bot: AgentBot, room: nio.MatrixRoom, _paths: RuntimePaths) -> None:
    await bot._turn_ingress.handle_media_event(room, _upload())


async def _send_voice(bot: AgentBot, room: nio.MatrixRoom, _paths: RuntimePaths) -> None:
    """Send a voice message; the test patches in its transcript."""
    voice = {"msgtype": "m.audio", "body": "voice.ogg", "url": "mxc://localhost/voice"}
    event = {"event_id": "$voice", "sender": SENDER, "origin_server_ts": 1, "room_id": ROOM_ID, "content": voice}
    await bot._turn_ingress.handle_media_event(room, nio.RoomMessageAudio.from_dict(event))


async def _send_upload_with_caption(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
    """Send an upload and, before it is answered, a caption mentioning general; the two are answered as one turn."""
    await bot._turn_ingress.handle_media_event(room, _upload())
    await bot._turn_ingress.handle_text_event(
        room,
        _text_message(paths, "$caption", "@general summarize this", mention="general"),
    )


async def _edit_a_mention_in(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
    """Send an untagged message, then edit it to mention general."""
    await _send_text(bot, room, paths)
    await drain_coalescing(bot)
    mentioned = {"user_ids": [current_entity_id("general", paths).full_id]}
    edit = {
        "m.new_content": {"msgtype": "m.text", "body": "@general hello", "m.mentions": mentioned},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$event"},
    }
    await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$edit", "* @general hello", **edit))


async def _follow_up_in_room_mode(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
    """Mention general, then send an untagged follow-up, in a room whose agents use `thread_mode: room`."""
    await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$event", "@general hello", mention="general"))
    await drain_coalescing(bot)
    await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$follow-up", "and one more thing"))


_PATHS: dict[str, Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]]] = {
    "untagged text": _send_text,
    "untagged upload": _send_upload,
    "voice whose transcript mentions general": _send_voice,
    "upload whose caption mentions general": _send_upload_with_caption,
    "edit that adds a mention to an untagged message": _edit_a_mention_in,
    "untagged follow-up in a room-mode room": _follow_up_in_room_mode,
}
# fmt: off
_WHO_ACTS = {
    #                                                    one agent: auto,  mentions            two agents: auto,              mentions
    "untagged text":                                   (_GENERAL_ONCE,    {},                 {_RELAY: 1},                   {}),
    "untagged upload":                                 (_GENERAL_ONCE,    {},                 {_RELAY: 1},                   {}),
    "voice whose transcript mentions general":         (_GENERAL_ONCE,    _GENERAL_ONCE,      _GENERAL_ONCE,                 _GENERAL_ONCE),
    "upload whose caption mentions general":           (_GENERAL_ONCE,    _GENERAL_ONCE,      _GENERAL_ONCE,                 _GENERAL_ONCE),
    # The untagged message is answered or routed in `auto`; the edit starts nothing in either mode.
    "edit that adds a mention to an untagged message": (_GENERAL_ONCE,    {},                 {_RELAY: 1},                   {}),
    # Every message is room-level in room mode, so the follow-up is a new message that needs its own mention.
    "untagged follow-up in a room-mode room":          ({"general": 2},   _GENERAL_ONCE,      {"general": 1, _RELAY: 1},     _GENERAL_ONCE),
}
# fmt: on
_INGRESS_CASES = [
    pytest.param(path, agents, mode, expected, id=f"{path}-{len(agents)}-agents-{mode}".replace(" ", "-"))
    for path, outcomes in _WHO_ACTS.items()
    for (agents, mode), expected in zip(
        ((("general",), "auto"), (("general",), "mentions"), (_BOTH, "auto"), (_BOTH, "mentions")),
        outcomes,
        strict=True,
    )
]


def _ingress_bot(config: Config, paths: RuntimePaths, runtime: object, name: str) -> AgentBot:
    user_id = current_entity_id(name, paths).full_id
    bot = make_test_agent_bot(
        AgentMatrixUser(agent_name=name, password=TEST_PASSWORD, display_name=name.title(), user_id=user_id),
        config,
        paths,
        rooms=[ROOM_ID],
        orchestrator=runtime,
    )
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id=user_id)
    unique_room_send_responses(bot.client)
    install_runtime_journal_support(bot)
    return bot


async def _who_acts(
    tmp_path: Path,
    send: Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]],
    agents: tuple[str, ...],
    mode: RoomReplyMode,
    *,
    voice: dict[str, bool],
    transcript: str = "🎤 @general what is on today",
    room_mode: bool = False,
) -> tuple[dict[str, int], AgentBot]:
    """Send through the router's and each agent's ingress; return who acted, how many times, and the router."""
    config = bind_runtime_paths(
        Config(
            agents={
                name: AgentConfig(
                    display_name=name.title(),
                    rooms=[ROOM_ID],
                    thread_mode="room" if room_mode else "thread",
                )
                for name in agents
            },
            rooms={ROOM_ID: RoomConfig(reply_mode=mode)},
            voice=voice,
            # An upload waits this long for its caption, so the two are answered as one turn.
            defaults={"coalescing": {"debounce_ms": 100}},
        ),
        test_runtime_paths(tmp_path),
    )
    paths = runtime_paths_for(config)
    runtime = ready_runtime(config, paths)
    bots = {name: _ingress_bot(config, paths, runtime, name) for name in (ROUTER_AGENT_NAME, *agents)}
    generates = {name: AsyncMock() for name in bots}
    for name, bot in bots.items():
        install_generate_response_mock(bot, generates[name])
    upload = tmp_path / "report.pdf"
    upload.write_bytes(b"pdf")
    attachment = register_local_attachment(
        paths.storage_root,
        upload,
        kind="file",
        attachment_id=_attachment_id_for_event("$file"),
        filename="report.pdf",
        mime_type="application/pdf",
        room_id=ROOM_ID,
        thread_id="$file",
        source_event_id="$file",
        sender=SENDER,
    )
    voice_audio = Audio(content=b"voice", mime_type="audio/ogg")

    with (
        patch("mindroom.turns.dispatch.execute_router_relay", new_callable=AsyncMock) as relay,
        patch(
            "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
            new=AsyncMock(return_value=attachment),
        ),
        patch("mindroom.voice.handler._download_audio", new=AsyncMock(return_value=voice_audio)),
        patch(
            "mindroom.voice.handler._handle_voice_message",
            new=AsyncMock(return_value=transcript),
        ),
    ):
        for bot in bots.values():
            room = nio.MatrixRoom(ROOM_ID, bot.client.user_id)
            for member in (SENDER, *(current_entity_id(name, paths).full_id for name in bots)):
                room.add_member(member, member, None)
            await send(bot, room, paths)
            await drain_coalescing(bot)

    acted = {name: generate.await_count for name, generate in generates.items() if generate.await_count}
    if relay.await_count:
        acted[_RELAY] = relay.await_count
    return acted, bots[ROUTER_AGENT_NAME]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "agents", "mode", "expected"), _INGRESS_CASES)
async def test_each_ingress_path_reaches_whom_the_reply_mode_says(
    tmp_path: Path,
    path: str,
    agents: tuple[str, ...],
    mode: RoomReplyMode,
    expected: dict[str, int],
) -> None:
    """Text, uploads, voice, captioned uploads, edits, and room-mode follow-ups reach the router and agents as the mode says."""
    acted, _router = await _who_acts(
        tmp_path,
        _PATHS[path],
        agents,
        mode,
        # The router's visible echo would hold a voice turn; this grid covers who answers it.
        voice={"enabled": True, "visible_router_echo": False},
        room_mode=path == "untagged follow-up in a room-mode room",
    )
    assert acted == expected


@pytest.mark.asyncio
async def test_router_still_echoes_an_unanswered_voice_message_in_a_mentions_room(tmp_path: Path) -> None:
    """With the default visible echo, the router shows the transcript of a voice message nobody answers.

    docs/configuration/threads.md and docs/voice.md say so.
    """
    acted, router = await _who_acts(
        tmp_path,
        _send_voice,
        _BOTH,
        "mentions",
        voice={"enabled": True},
        transcript="🎤 what is on today",
    )

    assert acted == {}
    sent = [call.kwargs["content"] for call in router.client.room_send.await_args_list]
    assert [content.get("m.new_content", content)["body"] for content in sent] == [
        "Router agent is transcribing…",
        "🎤 what is on today",
    ]
