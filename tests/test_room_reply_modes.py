"""Tests for a room's reply mode: its config field, its `!reply_mode` override, and who answers under each mode."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.attachments.storage import _attachment_id_for_event, register_local_attachment
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.coalescing.batch import active_follow_up_coalescing_key, requester_coalescing_key
from mindroom.commands.handler import CommandHandlerContext, handle_command
from mindroom.commands.parsing import Command, CommandType, command_parser, get_command_help
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME, SOURCE_KIND_KEY, tracking_dir
from mindroom.conversation_resolver import ConversationResolver
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
)
from mindroom.entity_resolution import current_entity_id
from mindroom.llm.media import Audio
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.mid_turn import MID_TURN_MODE_KEY, MidTurnGate, MidTurnMode
from mindroom.response_runner import ResponseRunner
from mindroom.room_reply_modes import (
    observe_room_settings_event,
    refresh_room_reply_mode,
    resolve_room_reply_mode_override,
    write_room_reply_mode,
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
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.agent import RoomReplyMode
    from mindroom.constants import RuntimePaths
    from mindroom.judgment.state import JudgmentRequest
    from mindroom.response_lifecycle import _QueuedMessageState
    from mindroom.turn_policy import TurnPolicy

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


class _Homeserver:
    """The room's state on a fake homeserver: the admin's power levels and whatever a test or a client sets."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, object]] = {
            ("m.room.power_levels", ""): {"users": {ADMIN: 100}, "users_default": 0},
        }

    def client(self) -> AsyncMock:
        client = AsyncMock()

        async def read(room_id: str, event_type: str, state_key: str = "") -> object:
            content = self.events.get((event_type, state_key))
            if content is None:
                return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
            return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

        async def write(room_id: str, event_type: str, content: dict[str, object], state_key: str = "") -> object:
            self.events[event_type, state_key] = dict(content)
            return nio.RoomPutStateResponse("$state", room_id)

        client.room_get_state_event.side_effect = read
        client.room_put_state.side_effect = write
        return client


def _command_context(config: Config, homeserver: _Homeserver | None = None) -> CommandHandlerContext:
    return make_test_command_handler_context(
        client=(homeserver or _Homeserver()).client(),
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


_event_clock = iter(range(1_700_000_000_000, 1_800_000_000_000))


def _setting_event(content: dict[str, str], ts: int | None = None) -> dict[str, object]:
    """A live `reply_mode` event, sent after every one made before unless ``ts`` says when."""
    return {
        "type": ROOM_SETTINGS_EVENT_TYPE,
        "state_key": "reply_mode",
        "content": content,
        "origin_server_ts": next(_event_clock) if ts is None else ts,
    }


async def _set_room_setting(runtime_paths: RuntimePaths, mode: RoomReplyMode | None) -> None:
    """A room admin sets the room's reply mode setting, which the runtime then knows."""
    assert await write_room_reply_mode(_Homeserver().client(), runtime_paths, ROOM_ID, mode)


@pytest.mark.asyncio
async def test_the_override_is_the_rooms_reply_mode_setting(tmp_path: Path) -> None:
    """The runtime reads the room's `reply_mode` setting once and keeps it until a change arrives in sync.

    `{}`, no event, or a value that is no reply mode leave it unset; a failed read keeps what the runtime knew.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = _Homeserver()
    client = homeserver.client()
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None

    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    assert resolve_room_reply_mode_override(runtime_paths, "!other:localhost") is None
    reads = client.room_get_state_event.await_count
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert client.room_get_state_event.await_count == reads

    for content, expected in (({"value": "auto"}, "auto"), ({}, None), ({"value": "paused"}, None)):
        homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = content
        observe_room_settings_event(runtime_paths, ROOM_ID, _setting_event(content))
        assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
        assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == expected
    reads = client.room_get_state_event.await_count
    observe_room_settings_event(runtime_paths, ROOM_ID, {**_setting_event({"value": "auto"}), "state_key": "other"})
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert client.room_get_state_event.await_count == reads

    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID, force=True)
    client.room_get_state_event.side_effect = None
    client.room_get_state_event.return_value = nio.RoomGetStateEventError("unavailable", "M_UNKNOWN")
    with patch("mindroom.room_reply_modes.time.monotonic", return_value=time.monotonic() + 3600):
        assert not await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"


@pytest.mark.asyncio
async def test_a_live_change_applies_at_once_and_a_late_older_copy_changes_nothing(tmp_path: Path) -> None:
    """A change sync brings is the room's value until a read confirms it; a slower bot's older copy is dropped."""
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = _Homeserver()
    client = homeserver.client()
    older = _setting_event({"value": "auto"})
    newer = _setting_event({"value": "mentions"})
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}

    observe_room_settings_event(runtime_paths, ROOM_ID, older)
    observe_room_settings_event(runtime_paths, ROOM_ID, newer)
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    observe_room_settings_event(runtime_paths, ROOM_ID, older)
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)

    # After the runtime's own write, a late copy of a change older than one it saw changes nothing either.
    assert await write_room_reply_mode(client, runtime_paths, ROOM_ID, "auto")
    reads = client.room_get_state_event.await_count
    observe_room_settings_event(runtime_paths, ROOM_ID, newer)
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "auto"
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert client.room_get_state_event.await_count == reads


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["read", "write"])
async def test_a_change_sync_brings_during_a_read_or_write_is_read_again(tmp_path: Path, during: str) -> None:
    """A read or write that overlapped a change confirms nothing: the change's value stays and the next refresh reads."""
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = _Homeserver()
    client = homeserver.client()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "auto"}
    request = client.room_get_state_event if during == "read" else client.room_put_state
    original = request.side_effect

    async def then_change(*args: object, **kwargs: object) -> object:
        response = await original(*args, **kwargs)
        homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}
        observe_room_settings_event(runtime_paths, ROOM_ID, _setting_event({"value": "mentions"}))
        return response

    request.side_effect = then_change
    if during == "read":
        assert not await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    else:
        assert await write_room_reply_mode(client, runtime_paths, ROOM_ID, "auto")
    request.side_effect = original
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    reads = client.room_get_state_event.await_count
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert client.room_get_state_event.await_count == reads + 1
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"


@pytest.mark.asyncio
async def test_bots_reading_the_room_at_once_all_settle_the_copy(tmp_path: Path) -> None:
    """Every bot in a room reads the setting for the same message; their reads leave the copy fresh for the next one."""
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = _Homeserver()
    client = homeserver.client()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}
    read = client.room_get_state_event.side_effect

    async def slow_read(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return await read(*args, **kwargs)

    client.room_get_state_event.side_effect = slow_read
    assert await asyncio.gather(*(refresh_room_reply_mode(client, runtime_paths, ROOM_ID) for _ in range(3))) == [
        True,
        True,
        True,
    ]
    reads = client.room_get_state_event.await_count
    assert await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)
    assert client.room_get_state_event.await_count == reads


def _write_legacy_overrides(runtime_paths: RuntimePaths, modes: dict[str, str]) -> Path:
    legacy = tracking_dir(runtime_paths) / "room_reply_modes.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({room_id: {"mode": mode, "set_at": "2026-11-01T00:00:00+00:00"} for room_id, mode in modes.items()}),
    )
    return legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True], ids=["moved", "room-refuses"])
async def test_an_override_an_older_runtime_stored_moves_into_the_room(tmp_path: Path, *, refused: bool) -> None:
    """A room without the setting takes the override an older runtime stored on disk, which then leaves the file.

    While the room refuses the write, the stored override still applies and stays on disk for the next read.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, {ROOM_ID: "mentions", "!other:localhost": "auto"})
    homeserver = _Homeserver()
    client = homeserver.client()
    if refused:
        client.room_put_state.side_effect = None
        client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    await refresh_room_reply_mode(client, runtime_paths, ROOM_ID)

    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    stored = json.loads(legacy.read_text())
    if refused:
        assert ROOM_ID in stored
        assert (ROOM_SETTINGS_EVENT_TYPE, "reply_mode") not in homeserver.events
    else:
        assert set(stored) == {"!other:localhost"}
        assert homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] == {"value": "mentions"}


@pytest.mark.asyncio
async def test_a_stalled_move_keeps_the_older_override_and_the_turn_moving(tmp_path: Path) -> None:
    """A write that hangs while an older override moves into the room gives up in time and keeps the override."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, {ROOM_ID: "mentions"})
    client = _Homeserver().client()

    async def stall(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError

    client.room_put_state.side_effect = stall

    with patch("mindroom.room_reply_modes._REQUEST_TIMEOUT_SECONDS", 0.05):
        await asyncio.wait_for(refresh_room_reply_mode(client, runtime_paths, ROOM_ID), timeout=2)

    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "mentions"
    assert ROOM_ID in json.loads(legacy.read_text())


@pytest.mark.asyncio
async def test_two_rooms_moving_at_once_both_leave_the_file(tmp_path: Path) -> None:
    """Rooms whose older overrides move at the same time each leave the file, whichever write ends first."""
    runtime_paths = test_runtime_paths(tmp_path)
    other_room = "!other:localhost"
    legacy = _write_legacy_overrides(runtime_paths, {ROOM_ID: "mentions", other_room: "auto"})
    clients = [_Homeserver().client(), _Homeserver().client()]
    for client in clients:
        write = client.room_put_state.side_effect

        async def slow_write(
            *args: object,
            write: Callable[..., Awaitable[object]] = write,
            **kwargs: object,
        ) -> object:
            await asyncio.sleep(0)
            return await write(*args, **kwargs)

        client.room_put_state.side_effect = slow_write

    await asyncio.gather(
        refresh_room_reply_mode(clients[0], runtime_paths, ROOM_ID),
        refresh_room_reply_mode(clients[1], runtime_paths, other_room),
    )

    assert json.loads(legacy.read_text()) == {}


def test_room_reply_mode_reads_the_room_entry_by_room_key(tmp_path: Path) -> None:
    """A `rooms` entry keyed by a managed room's key sets that room's mode, and every other room stays `auto`."""
    config = _config(tmp_path, reply_mode="mentions", room_key="lobby")
    runtime_paths = runtime_paths_for(config)
    state = MatrixState()
    state.add_room("lobby", ROOM_ID, "#lobby:localhost", "Lobby")
    save_matrix_state(state, runtime_paths)

    assert config.get_room_reply_mode(runtime_paths, ROOM_ID) == "mentions"
    assert config.get_room_reply_mode(runtime_paths, "!other:localhost") == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured", "override"), [("auto", "mentions"), ("mentions", "auto")])
async def test_room_reply_mode_override_beats_config(
    tmp_path: Path,
    configured: RoomReplyMode,
    override: RoomReplyMode,
) -> None:
    """The room's reply mode setting decides over `rooms.<room>.reply_mode` until it is cleared."""
    config = _config(tmp_path, reply_mode=configured)
    runtime_paths = runtime_paths_for(config)

    await _set_room_setting(runtime_paths, override)
    assert config.get_room_reply_mode(runtime_paths, ROOM_ID) == override
    await _set_room_setting(runtime_paths, None)
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
    """Anyone sees the room's mode; only a room admin sets or resets it, and an unknown mode changes nothing.

    The command sets and clears the room's reply mode setting, and shows the one a client set.
    """
    homeserver = _Homeserver()
    context = _command_context(_config(tmp_path, reply_mode="mentions"), homeserver)
    runtime_paths = context.runtime_paths
    setting = (ROOM_SETTINGS_EVENT_TYPE, "reply_mode")

    assert "configured `mentions` reply mode" in await _run_reply_mode_command(context, SENDER, "show")
    assert await _run_reply_mode_command(context, SENDER, "auto") == "❌ Room admin only."
    assert (await _run_reply_mode_command(context, ADMIN, "never")).startswith("❌ Unknown reply mode `never`.")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None

    assert setting not in homeserver.events

    assert "now uses `auto` reply mode" in await _run_reply_mode_command(context, ADMIN, "auto")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) == "auto"
    assert homeserver.events[setting] == {"value": "auto"}
    assert "`auto` reply mode override" in await _run_reply_mode_command(context, SENDER, "")
    assert await _run_reply_mode_command(context, SENDER, "reset") == "❌ Room admin only."

    assert "override removed" in await _run_reply_mode_command(context, ADMIN, "reset")
    assert resolve_room_reply_mode_override(runtime_paths, ROOM_ID) is None
    assert homeserver.events[setting] == {}
    assert await _run_reply_mode_command(context, ADMIN, "reset") == "This room has no reply mode override."

    # A client's change the runtime's copy has not seen: the command reads the room itself.
    homeserver.events[setting] = {"value": "mentions"}
    assert "`mentions` reply mode override" in await _run_reply_mode_command(context, SENDER, "show")
    homeserver.events[setting] = {"value": "auto"}
    assert await refresh_room_reply_mode(context.client, runtime_paths, ROOM_ID)
    homeserver.events[setting] = {"value": "mentions"}
    assert "override removed" in await _run_reply_mode_command(context, ADMIN, "reset")
    assert homeserver.events[setting] == {}


@pytest.mark.asyncio
async def test_reply_mode_command_says_when_it_cannot_read_the_room(tmp_path: Path) -> None:
    """`/reply_mode show` claims no mode it could not read, and reset still clears the room's setting."""
    homeserver = _Homeserver()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}
    context = _command_context(_config(tmp_path), homeserver)
    read = context.client.room_get_state_event.side_effect

    async def settings_unreadable(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == ROOM_SETTINGS_EVENT_TYPE:
            return nio.RoomGetStateEventError("unavailable", "M_UNKNOWN")
        return await read(room_id, event_type, state_key)

    context.client.room_get_state_event.side_effect = settings_unreadable

    assert await _run_reply_mode_command(context, SENDER, "show") == (
        "❌ Could not read this room's reply mode setting. Try again."
    )
    assert "override removed" in await _run_reply_mode_command(context, ADMIN, "reset")
    assert homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] == {}


@pytest.mark.asyncio
async def test_reply_mode_command_says_when_the_room_refuses_the_change(tmp_path: Path) -> None:
    """An admin's change the room refuses from the router leaves the mode as it was and says so."""
    context = _command_context(_config(tmp_path))
    context.client.room_put_state.side_effect = None
    context.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    assert (await _run_reply_mode_command(context, ADMIN, "mentions")).startswith("❌ The room did not take the change")
    assert resolve_room_reply_mode_override(context.runtime_paths, ROOM_ID) is None


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
        await _set_room_setting(runtime_paths_for(config), mode)

    assert await _answers(config, turn) == (auto if mode == "auto" else mentions)


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting", "expected"), [("mentions", {}), (None, {"general": "individual"})])
async def test_a_turn_reads_the_rooms_setting_it_has_not_seen_yet(
    tmp_path: Path,
    setting: str | None,
    expected: dict[str, str],
) -> None:
    """A bot that has not seen the room's reply mode setting reads it before deciding, and the config applies unset."""
    config = _config(tmp_path)
    homeserver = _Homeserver()
    if setting is not None:
        homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": setting}

    policy_for = _policy_for

    def policy_reading_the_homeserver(config: Config, agent_name: str) -> TurnPolicy:
        policy = policy_for(config, agent_name)
        policy.deps.runtime.client = homeserver.client()
        return policy

    with patch(f"{__name__}._policy_for", new=policy_reading_the_homeserver):
        assert await _answers(config, _Turn()) == expected


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
    sender: str = SENDER,
    **content: object,
) -> nio.RoomMessageText:
    if mention is not None:
        content["m.mentions"] = {"user_ids": [current_entity_id(mention, paths).full_id]}
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": sender,
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


def _mention_id(paths: RuntimePaths, name: str) -> str:
    """Return the Matrix ID a mention of ``name`` names: an entity's, or a person's own ID."""
    return name if name.startswith("@") else current_entity_id(name, paths).full_id


def _in_thread(
    paths: RuntimePaths,
    event_id: str,
    body: str,
    *mentions: str,
    sender: str = SENDER,
    **content: object,
) -> nio.RoomMessageText:
    """Return a message in THREAD_ID that mentions ``mentions``."""
    content["m.relates_to"] = {"rel_type": "m.thread", "event_id": THREAD_ID}
    if mentions:
        content["m.mentions"] = {"user_ids": [_mention_id(paths, name) for name in mentions]}
    return _text_message(paths, event_id, body, sender=sender, **content)


@contextmanager
def _reading_general_thread(paths: RuntimePaths) -> Iterator[None]:
    """Read THREAD_ID in full: the person's message and general's reply."""
    history = thread_history_result(
        [
            make_visible_message(sender=SENDER, body="hello", event_id=THREAD_ID),
            make_visible_message(
                sender=current_entity_id("general", paths).full_id,
                body="Hi there",
                event_id="$general-reply",
                thread_id=THREAD_ID,
            ),
        ],
        is_full_history=True,
    )
    with (
        patch.object(ConversationResolver, "dispatch_thread_snapshot", new=AsyncMock(return_value=history)),
        patch.object(ConversationResolver, "fetch_thread_history", new=AsyncMock(return_value=history)),
    ):
        yield


def _in_general_thread(
    body: str,
    *mentions: str,
    relayed: bool = False,
) -> Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]]:
    """Return a sender of ``body``, mentioning ``mentions``, in a thread general already replied in.

    A mentioned person joins the room first. ``relayed`` sends it as the router's relay of the person's message.
    """

    async def send(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
        for person in (name for name in mentions if name.startswith("@")):
            room.add_member(person, person, None)
        content: dict[str, object] = {}
        sender = SENDER
        if relayed:
            content = {ORIGINAL_SENDER_KEY: SENDER, SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND}
            sender = current_entity_id(ROUTER_AGENT_NAME, paths).full_id
        with _reading_general_thread(paths):
            await bot._turn_ingress.handle_text_event(
                room,
                _in_thread(paths, "$follow-up", body, *mentions, sender=sender, **content),
            )
            await drain_coalescing(bot)

    return send


_follow_up_in_general_thread = _in_general_thread("and one more thing")


_PATHS: dict[str, Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]]] = {
    "untagged text": _send_text,
    "untagged upload": _send_upload,
    "voice whose transcript mentions general": _send_voice,
    "upload whose caption mentions general": _send_upload_with_caption,
    "edit that adds a mention to an untagged message": _edit_a_mention_in,
    "untagged follow-up in a room-mode room": _follow_up_in_room_mode,
    "untagged follow-up in a thread general replied in": _follow_up_in_general_thread,
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
    "untagged follow-up in a thread general replied in": (_GENERAL_ONCE,  _GENERAL_ONCE,      _GENERAL_ONCE,                 _GENERAL_ONCE),
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
) -> tuple[dict[str, int], dict[str, AgentBot]]:
    """Send through the router's and each agent's ingress; return who acted, how many times, and every bot."""
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
    return acted, bots


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
    acted, _bots = await _who_acts(
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
    acted, bots = await _who_acts(
        tmp_path,
        _send_voice,
        _BOTH,
        "mentions",
        voice={"enabled": True},
        transcript="🎤 what is on today",
    )

    assert acted == {}
    sent = [call.kwargs["content"] for call in bots[ROUTER_AGENT_NAME].client.room_send.await_args_list]
    assert [content.get("m.new_content", content)["body"] for content in sent] == [
        "Router agent is transcribing…",
        "🎤 what is on today",
    ]


async def _mention_general(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
    await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$event", "@general hello", mention="general"))


async def _mention_research(bot: AgentBot, room: nio.MatrixRoom, paths: RuntimePaths) -> None:
    await bot._turn_ingress.handle_text_event(
        room,
        _text_message(paths, "$event", "@research hello", mention="research"),
    )


async def _general_nudged_and_shown_waiting(
    tmp_path: Path,
    send: Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]],
    agents: tuple[str, ...],
    mode: RoomReplyMode,
    *,
    transcript: str,
    room_mode: bool,
) -> tuple[bool, bool]:
    """While general is answering and an approval holds its conversation, send a message.

    Return whether the message asked general's running response to wrap up, and whether general reacted ⏳ to it.
    """
    with (
        patch.object(ResponseRunner, "has_active_response_for_target", return_value=True),
        patch.object(ResponseRunner, "reserve_waiting_human_message", autospec=True, return_value=None) as reserve,
        patch.object(ResponseRunner, "is_held_for_approval", return_value=True),
    ):
        _acted, bots = await _who_acts(
            tmp_path,
            send,
            agents,
            mode,
            voice={"enabled": True, "visible_router_echo": False},
            transcript=transcript,
            room_mode=room_mode,
        )
        general = bots["general"]
        assert await wait_for_background_tasks(5.0, owner=general._runtime_view)

    nudged = [call for call in reserve.call_args_list if call.kwargs["response_envelope"].agent_name == "general"]
    reactions = [
        call.kwargs["content"]["m.relates_to"]["key"]
        for call in general.client.room_send.await_args_list
        if call.kwargs["message_type"] == "m.reaction"
    ]
    return bool(nudged), "⏳" in reactions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "send", "transcript", "nudges"),
    [
        pytest.param("mentions", _send_text, "", False, id="untagged-text-mentions"),
        pytest.param("mentions", _mention_general, "", True, id="text-mentioning-general-mentions"),
        pytest.param("auto", _send_text, "", True, id="untagged-text-auto"),
        pytest.param("mentions", _send_voice, "🎤 what is on today", False, id="untagged-voice-mentions"),
        pytest.param(
            "mentions",
            _send_voice,
            "🎤 @general what is on today",
            True,
            id="voice-mentioning-general-mentions",
        ),
    ],
)
async def test_unanswered_chatter_does_not_nudge_a_running_response(
    tmp_path: Path,
    mode: RoomReplyMode,
    send: Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]],
    transcript: str,
    *,
    nudges: bool,
) -> None:
    """While general is still answering in a `thread_mode: room` room, a message nobody will answer leaves it alone.

    A message the room's reply mode leaves to nobody neither asks general's running response to wrap up nor gets the
    ⏳ that says it waits for an approval; a message general will answer does both.
    """
    nudged, shown_waiting = await _general_nudged_and_shown_waiting(
        tmp_path,
        send,
        ("general",),
        mode,
        transcript=transcript,
        room_mode=True,
    )
    assert nudged is nudges
    assert shown_waiting is nudges


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send", "room_mode", "transcript", "nudges"),
    [
        pytest.param(_in_general_thread("@research look", "research"), False, "", False, id="mentions-only-research"),
        pytest.param(
            _in_general_thread("@research look", "research", relayed=True),
            False,
            "",
            False,
            id="router-relay-to-research",
        ),
        pytest.param(_mention_research, True, "", False, id="mentions-only-research-room-mode"),
        pytest.param(_in_general_thread("@other look", OTHER_HUMAN), False, "", False, id="mentions-only-a-person"),
        pytest.param(_follow_up_in_general_thread, False, "", True, id="untagged-in-general-thread"),
        pytest.param(
            _in_general_thread("@general @research look", "general", "research"),
            False,
            "",
            True,
            id="mentions-general-and-research",
        ),
        pytest.param(
            _in_general_thread("@general look", "general", relayed=True),
            False,
            "",
            True,
            id="router-relay-to-general",
        ),
        pytest.param(_mention_general, True, "", True, id="mentions-general-room-mode"),
    ],
)
async def test_a_message_for_another_agent_does_not_nudge_a_running_response(
    tmp_path: Path,
    send: Callable[[AgentBot, nio.MatrixRoom, RuntimePaths], Awaitable[None]],
    transcript: str,
    *,
    room_mode: bool,
    nudges: bool,
) -> None:
    """While general works through a long task, a message that mentions only research leaves general's response alone.

    It neither asks general to wrap up nor gets general's ⏳, typed or relayed by the router, in a thread or in a
    `thread_mode: room` room, and neither does a message that mentions only another person in the room. A message
    general would answer does both: an untagged follow-up in the thread only general took part in, a message that
    mentions general and research, and the router's relay to general.
    """
    nudged, shown_waiting = await _general_nudged_and_shown_waiting(
        tmp_path,
        send,
        _BOTH,
        "auto",
        transcript=transcript,
        room_mode=room_mode,
    )
    assert nudged is nudges
    assert shown_waiting is nudges


@asynccontextmanager
async def _general_answering_in_its_thread(
    tmp_path: Path,
    *,
    voice: bool = False,
    **agents: AgentConfig,
) -> AsyncIterator[tuple[AgentBot, nio.MatrixRoom, RuntimePaths, _QueuedMessageState]]:
    """Run general's response in THREAD_ID, held by an approval, while the body sends messages to general's ingress.

    Yield general's bot, the room, the runtime paths, and the running response's queue of waiting messages. A message
    waits in its queue until the body ends. ``voice`` turns voice messages on.
    """
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=[ROOM_ID]), **agents},
            voice={"enabled": voice, "visible_router_echo": False},
        ),
        test_runtime_paths(tmp_path),
    )
    paths = runtime_paths_for(config)
    bot = _ingress_bot(config, paths, ready_runtime(config, paths), "general")
    install_generate_response_mock(bot, AsyncMock())
    room = nio.MatrixRoom(ROOM_ID, bot.client.user_id)
    for member in (SENDER, *(current_entity_id(name, paths).full_id for name in config.agents)):
        room.add_member(member, member, None)
    running = bot._response_runner.lifecycle_coordinator._get_or_create_queued_signal(
        MessageTarget.resolve(ROOM_ID, THREAD_ID, "$running"),
    )
    running.begin_response_turn()
    try:
        with (
            _reading_general_thread(paths),
            patch.object(bot._turn_ingress, "_adaptive_text_debounce_seconds", AsyncMock(return_value=60.0)),
            patch.object(bot._response_runner, "is_held_for_approval", return_value=True),
        ):
            yield bot, room, paths, running
    finally:
        running.finish_response_turn()
        await drain_coalescing(bot)


async def _queued(bot: AgentBot, event_id: str) -> None:
    """Wait until a message waits in general's queue for THREAD_ID."""
    keys = (requester_coalescing_key(ROOM_ID, THREAD_ID, SENDER), active_follow_up_coalescing_key(ROOM_ID, THREAD_ID))

    async def queued() -> None:
        while not any(  # noqa: ASYNC110
            pending.event.event_id == event_id
            for key in keys
            for pending in bot._coalescing_gate.queued_pending_events(key)
        ):
            await asyncio.sleep(0.001)

    await asyncio.wait_for(queued(), timeout=5)


async def _shows_waiting(bot: AgentBot, event_id: str) -> bool:
    """Return whether general reacted ⏳ to this message, once any reaction it started is sent."""
    for task in asyncio.all_tasks():
        if task.get_name() == f"approval_wait_reaction_{event_id}":
            await task
    return any(
        call.kwargs["message_type"] == "m.reaction"
        and call.kwargs["content"]["m.relates_to"]["event_id"] == event_id
        and call.kwargs["content"]["m.relates_to"]["key"] == "⏳"
        for call in bot.client.room_send.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentions", "edited_mentions"),
    [
        pytest.param((), ("research",), id="untagged-edited-to-mention-research"),
        pytest.param(("research",), ("general",), id="research-edited-to-mention-general"),
    ],
)
async def test_editing_a_waiting_message_asks_again_whether_it_is_for_general(
    tmp_path: Path,
    mentions: tuple[str, ...],
    edited_mentions: tuple[str, ...],
) -> None:
    """While general is answering in a thread, a person's edit of their waiting message decides again.

    An untagged follow-up asks general's running response to wrap up until it is edited to mention only research; a
    message for research does not, until it is edited to mention general, and then shows ⏳ as it waits. A ⏳ the
    message already shows stays when an edit takes it away from general.
    """
    research = AgentConfig(display_name="Research", rooms=[ROOM_ID])
    async with _general_answering_in_its_thread(tmp_path, research=research) as (bot, room, paths, running):
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$follow-up", "look at this", *mentions))
        await _queued(bot, "$follow-up")
        assert running.has_pending_human_messages() is (not mentions)
        assert await _shows_waiting(bot, "$follow-up") is (not mentions)

        edit = {
            "m.new_content": {
                "body": "look at this",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_mention_id(paths, name) for name in edited_mentions]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$follow-up"},
        }
        await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$edit", "* look at this", **edit))
        assert running.has_pending_human_messages() is ("general" in edited_mentions)
        assert await _shows_waiting(bot, "$follow-up")


@pytest.mark.asyncio
@pytest.mark.parametrize("renewed", [False, True], ids=["notice-updated", "notice-renewed"])
async def test_edit_keeps_original_queued_mode_when_notice_is_renewed(tmp_path: Path, *, renewed: bool) -> None:
    """A waiting message keeps the mode it was sent with, whatever mode key its sender's edits carry.

    An edit without the key keeps the original key on the queued content, so a notice the edit reserves anew (for a
    message first sent to research) is Queue as well; an edit that names another mode does not change it either.
    """
    research = AgentConfig(display_name="Research", rooms=[ROOM_ID])
    async with _general_answering_in_its_thread(tmp_path, research=research) as (bot, room, paths, running):
        mentions = ("research",) if renewed else ()
        message = _in_thread(paths, "$follow-up", "look at this", *mentions, **{MID_TURN_MODE_KEY: "queue"})
        await bot._turn_ingress.handle_text_event(room, message)
        await _queued(bot, "$follow-up")
        if renewed:
            assert running.pending_message_snapshot() == ()
        else:
            assert [(item.event_id, item.mode) for item in running.pending_message_snapshot()] == [
                ("$follow-up", MidTurnMode.QUEUE),
            ]

        keys = (
            requester_coalescing_key(ROOM_ID, THREAD_ID, SENDER),
            active_follow_up_coalescing_key(ROOM_ID, THREAD_ID),
        )
        for edit_id, edited_mode in (("$edit", None), ("$steer-edit", "steer")):
            new_content: dict[str, object] = {"body": "look at this", "msgtype": "m.text"}
            if edited_mode is not None:
                new_content[MID_TURN_MODE_KEY] = edited_mode
            edit = {
                "m.new_content": new_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$follow-up"},
            }
            await bot._turn_ingress.handle_text_event(room, _text_message(paths, edit_id, "* look at this", **edit))
            (pending,) = [
                pending
                for key in keys
                for pending in bot._coalescing_gate.queued_pending_events(key)
                if pending.event.event_id == "$follow-up"
            ]
            assert pending.event.source["content"][MID_TURN_MODE_KEY] == "queue"
            assert [(item.event_id, item.mode) for item in running.pending_message_snapshot()] == [
                ("$follow-up", MidTurnMode.QUEUE),
            ]


@pytest.mark.asyncio
async def test_a_waiting_message_records_whether_it_mentions_anyone_and_an_edit_records_it_again(
    tmp_path: Path,
) -> None:
    """The judge of general's running response learns which waiting messages mention nobody, also after an edit.

    Unjudged, another person's message that mentions nobody need not cut the response short, while one that
    mentions general does, so an edit that adds the mention must count. The edited message keeps its place in the
    queue and the reply progress it arrived to, so the judge reads the messages in the order they were sent.
    """

    async def never_judged(_request: JudgmentRequest) -> bool | None:
        pytest.fail("Nothing waits on this queue")

    async with _general_answering_in_its_thread(tmp_path) as (bot, room, paths, running):
        running.mid_turn_gate = MidTurnGate(active_text="Do the task", evaluate=never_judged)
        running.mid_turn_gate.visible_response_text = "Step 1 done"
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$untagged", "look at this"))
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$tagged", "and this", "general"))
        await _queued(bot, "$untagged")
        await _queued(bot, "$tagged")
        assert {message.event_id: message.mentions_nobody for message in running.pending_message_snapshot()} == {
            "$untagged": True,
            "$tagged": False,
        }

        running.mid_turn_gate.visible_response_text = "Step 2 done"
        edit = {
            "m.new_content": {
                "body": "look at this, general",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_mention_id(paths, "general")]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$untagged"},
        }
        await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$edit", "* look at this", **edit))
        assert [
            (message.event_id, message.text, message.mentions_nobody, message.visible_response)
            for message in running.pending_message_snapshot()
        ] == [
            ("$untagged", "look at this, general", False, "Step 1 done"),
            ("$tagged", "and this", False, "Step 1 done"),
        ]


@pytest.mark.asyncio
async def test_editing_a_waiting_message_handed_to_a_later_turn_asks_the_running_response_nothing(
    tmp_path: Path,
) -> None:
    """A waiting message whose notice a split-off turn already released stays released when its sender edits it.

    Another requester's turn that the queue split off now holds the conversation, and this message waits for that turn
    to end; asking it to wrap up would hand its unfinished work to the next requester's turn.
    """
    async with _general_answering_in_its_thread(tmp_path) as (bot, room, paths, running):
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$follow-up", "look at this"))
        await _queued(bot, "$follow-up")
        keys = (
            requester_coalescing_key(ROOM_ID, THREAD_ID, SENDER),
            active_follow_up_coalescing_key(ROOM_ID, THREAD_ID),
        )
        (pending,) = [
            pending
            for key in keys
            for pending in bot._coalescing_gate.queued_pending_events(key)
            if pending.event.event_id == "$follow-up"
        ]
        assert pending.notice is not None
        pending.notice.release()
        assert not running.has_pending_human_messages()

        edit = {
            "m.new_content": {"body": "look at this one", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$follow-up"},
        }
        await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$edit", "* look at this one", **edit))
        assert not running.has_pending_human_messages()


@pytest.mark.asyncio
@pytest.mark.parametrize("released_by_split", [True, False], ids=["released-by-a-split", "still-waiting"])
async def test_editing_a_waiting_message_away_from_general_and_back_keeps_a_released_notice_released(
    tmp_path: Path,
    *,
    released_by_split: bool,
) -> None:
    """A message whose notice a split-off turn released stays released through an edit to research and back.

    Otherwise the edit back would ask the next requester's running turn to wrap up. A message that still waits for
    general's running response, edited to research and back, waits for it again.
    """
    research = AgentConfig(display_name="Research", rooms=[ROOM_ID])
    async with _general_answering_in_its_thread(tmp_path, research=research) as (bot, room, paths, running):
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$follow-up", "look at this"))
        await _queued(bot, "$follow-up")
        keys = (
            requester_coalescing_key(ROOM_ID, THREAD_ID, SENDER),
            active_follow_up_coalescing_key(ROOM_ID, THREAD_ID),
        )
        (pending,) = [
            pending
            for key in keys
            for pending in bot._coalescing_gate.queued_pending_events(key)
            if pending.event.event_id == "$follow-up"
        ]
        assert pending.notice is not None
        if released_by_split:
            pending.notice.release()

        for edit_id, mentions in (("$to-research", ("research",)), ("$back", ())):
            edit = {
                "m.new_content": {
                    "body": "look at this",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": [_mention_id(paths, name) for name in mentions]},
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$follow-up"},
            }
            await bot._turn_ingress.handle_text_event(room, _text_message(paths, edit_id, "* look at this", **edit))
        assert running.has_pending_human_messages() is (not released_by_split)


@pytest.mark.asyncio
async def test_an_edit_that_makes_a_message_wait_records_that_it_mentions_nobody(tmp_path: Path) -> None:
    """A message for research, edited to mention nobody in general's thread, waits for general as mentioning nobody.

    Unjudged, another person's message that mentions nobody need not cut general's running response short.
    """
    research = AgentConfig(display_name="Research", rooms=[ROOM_ID])
    async with _general_answering_in_its_thread(tmp_path, research=research) as (bot, room, paths, running):
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$follow-up", "look at this", "research"))
        await _queued(bot, "$follow-up")
        assert not running.has_pending_human_messages()

        edit = {
            "m.new_content": {"body": "look at this", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$follow-up"},
        }
        await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$edit", "* look at this", **edit))
        assert [(message.event_id, message.mentions_nobody) for message in running.pending_message_snapshot()] == [
            ("$follow-up", True),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transcript", "judged_text", "mentions_nobody"),
    [
        pytest.param("🎤 look at this", "🎤 look at this", True, id="mentions-nobody"),
        pytest.param("🎤 @general look at this", "🎤 @general look at this", False, id="mentions-general"),
        pytest.param(None, None, False, id="not-transcribed"),
    ],
)
async def test_a_voice_message_waits_for_generals_running_response_once_transcribed(
    tmp_path: Path,
    transcript: str | None,
    judged_text: str | None,
    *,
    mentions_nobody: bool,
) -> None:
    """General's running response hears of a voice message once it is transcribed; its judge reads the transcript.

    The message records whom the transcript mentions, as text does. One that cannot be transcribed waits unjudged, and
    whom it mentions is unknown, so it counts as mentioning someone.
    """
    async with _general_answering_in_its_thread(tmp_path, voice=True) as (bot, room, _paths, running):

        async def transcribe(*_args: object, **_kwargs: object) -> str | None:
            assert not running.has_pending_human_messages()
            return transcript

        voice = {
            "msgtype": "m.audio",
            "body": "voice.ogg",
            "url": "mxc://localhost/voice",
            "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD_ID},
        }
        event = {"event_id": "$voice", "sender": SENDER, "origin_server_ts": 1, "room_id": ROOM_ID, "content": voice}
        transcribed = AsyncMock(side_effect=transcribe)
        with (
            patch(
                "mindroom.voice.handler._download_audio",
                new=AsyncMock(return_value=Audio(content=b"voice", mime_type="audio/ogg")),
            ),
            patch("mindroom.voice.handler._handle_voice_message", new=transcribed),
        ):
            await bot._turn_ingress.handle_media_event(room, nio.RoomMessageAudio.from_dict(event))
            await _queued(bot, "$voice")
        transcribed.assert_awaited_once()
        assert [
            (message.event_id, message.text, message.mentions_nobody) for message in running.pending_message_snapshot()
        ] == [("$voice", judged_text, mentions_nobody)]


@pytest.mark.asyncio
async def test_editing_a_waiting_answer_to_a_question_asks_for_no_wrap_up(tmp_path: Path) -> None:
    """An edit of a queued answer to an interactive question reserves no queued-message notice.

    Such an answer never waits as a follow-up, and its dispatch would never release a notice reserved for it, so every
    later response in the thread would keep being asked to wrap up.
    """
    research = AgentConfig(display_name="Research", rooms=[ROOM_ID])
    async with _general_answering_in_its_thread(tmp_path, research=research) as (bot, room, paths, running):
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$answer", "2", "research"))
        await _queued(bot, "$answer")
        keys = (
            requester_coalescing_key(ROOM_ID, THREAD_ID, SENDER),
            active_follow_up_coalescing_key(ROOM_ID, THREAD_ID),
        )
        (pending,) = [
            pending
            for key in keys
            for pending in bot._coalescing_gate.queued_pending_events(key)
            if pending.event.event_id == "$answer"
        ]
        pending.selection = MagicMock()
        assert not running.has_pending_human_messages()

        edit = {
            "m.new_content": {
                "body": "3",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_mention_id(paths, "general")]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$answer"},
        }
        await bot._turn_ingress.handle_text_event(room, _text_message(paths, "$edit", "* 3", **edit))
        assert not running.has_pending_human_messages()
        assert pending.notice is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentions", "waits"),
    [
        pytest.param(("private_one", "private_two"), True, id="team-of-private-agents"),
        pytest.param(("private_one", "research"), False, id="team-research-answers-for"),
        pytest.param(("research",), False, id="research"),
    ],
)
async def test_a_team_of_private_agents_asks_its_busy_shared_owner_to_wrap_up(
    tmp_path: Path,
    mentions: tuple[str, ...],
    *,
    waits: bool,
) -> None:
    """A message that asks for a team of private agents waits for general, the shared agent that answers for it.

    general mentions none of them, yet surfaces that team's answer, so its running response is asked to wrap up and
    the message shows ⏳, as for a message for general. A team that research is in is research's to answer, and a
    message for research alone is not general's either.
    """
    agents = {
        name: AgentConfig(display_name=name.title(), rooms=[ROOM_ID], private=AgentPrivateConfig(per="user"))
        for name in ("private_one", "private_two")
    }
    agents["research"] = AgentConfig(display_name="Research", rooms=[ROOM_ID])
    async with _general_answering_in_its_thread(tmp_path, **agents) as (bot, room, paths, running):
        await bot._turn_ingress.handle_text_event(room, _in_thread(paths, "$follow-up", "look at this", *mentions))
        await _queued(bot, "$follow-up")
        assert running.has_pending_human_messages() is waits
        assert await _shows_waiting(bot, "$follow-up") is waits
