"""Test audio normalization and dispatch through the shared text/media flow."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.handled_turns import HandledTurnState
from mindroom.history.types import HistoryScope
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.identity import MatrixID
from mindroom.message_target import MessageTarget
from mindroom.voice_handler import prepare_voice_message
from tests.conftest import (
    bind_runtime_paths,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    install_send_skill_command_response_mock,
    orchestrator_runtime_paths,
    replace_turn_controller_deps,
    runtime_paths_for,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from pathlib import Path


def _attach_runtime_paths(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"))


def _agent_bot(*, agent_user: object, storage_path: Path, config: Config, rooms: list[str]) -> AgentBot:
    """Construct an agent bot with the explicit runtime bound to the test config."""
    bot = install_runtime_cache_support(
        AgentBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=rooms,
        ),
    )
    wrap_extracted_collaborators(bot)
    return bot


async def _prepare_voice_message_with_runtime(
    client: object,
    storage_path: Path,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
    *,
    sender_domain: str,
    thread_id: str | None,
) -> object:
    """Normalize voice input with the test config's explicit runtime context."""
    return await prepare_voice_message(
        client,
        storage_path,
        room,
        event,
        config,
        runtime_paths=runtime_paths_for(config),
        sender_domain=sender_domain,
        thread_id=thread_id,
    )


def _make_voice_event(
    *,
    sender: str,
    event_id: str = "$voice_event",
    body: str = "voice.ogg",
    source: dict | None = None,
    server_timestamp: int = 1_712_350_000_000,
) -> nio.RoomMessageAudio:
    event = MagicMock(spec=nio.RoomMessageAudio)
    event.sender = sender
    event.event_id = event_id
    event.body = body
    event.server_timestamp = server_timestamp
    event.source = source or {"content": {"body": body}}
    return event


def _make_room(*user_ids: str) -> nio.MatrixRoom:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:example.com"
    room.canonical_alias = None
    room.users = {user_id: MagicMock() for user_id in user_ids}
    room.members_synced = True
    return room


def _install_voice_thread_dispatch_mocks(
    bot: AgentBot,
) -> None:
    """Provide minimal explicit-thread cache reads for normalized voice dispatch."""
    bot._conversation_cache.get_dispatch_thread_snapshot = AsyncMock(
        return_value=thread_history_result([], is_full_history=False),
    )
    bot._conversation_cache.get_dispatch_thread_history = AsyncMock(return_value=[])


def _make_visible_router_echo_scenario(
    tmp_path: Path,
    *,
    agents: dict | None = None,
    authorization: dict | None = None,
    send_response_return: str | None = "$voice_echo",
    send_response_side_effect: list[str] | None = None,
) -> tuple[AgentBot, nio.MatrixRoom, nio.RoomMessageAudio]:
    """Build a router bot + room + voice event for visible echo tests."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    configured_agents = agents or {"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}}
    config = _attach_runtime_paths(
        Config(
            agents=configured_agents,
            authorization=authorization or {"default_room_access": True},
            voice={"enabled": True, "visible_router_echo": True},
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    _install_voice_thread_dispatch_mocks(bot)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(False, None, []),
    )
    bot._send_response = AsyncMock()
    if send_response_side_effect is not None:
        bot._send_response.side_effect = send_response_side_effect
    else:
        bot._send_response.return_value = send_response_return
    install_send_response_mock(bot, bot._send_response)

    room_user_ids = [
        "@mindroom_router:localhost",
        *[f"@mindroom_{name}:localhost" for name in configured_agents],
        "@alice:example.com",
    ]
    room = _make_room(*room_user_ids)
    event = _make_voice_event(sender="@alice:example.com")
    return bot, room, event


@pytest.mark.asyncio
async def test_router_processes_own_voice_transcriptions(tmp_path) -> None:  # noqa: ANN001
    """Router should still handle voice-derived commands it sent on behalf of users."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = "@mindroom_router:example.com"
    event.body = "🎤 !schedule daily"
    event.event_id = "test_event"
    event.server_timestamp = 1234567890
    event.source = {"content": {"body": "🎤 !schedule daily", ORIGINAL_SENDER_KEY: "@alice:example.com"}}

    with (
        patch("mindroom.turn_controller.TurnController._execute_command", new_callable=AsyncMock) as mock_handle,
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        patch("mindroom.turn_controller.is_dm_room", return_value=False),
    ):
        bot.client = MagicMock()
        await bot._on_message(room, event)

    mock_handle.assert_called_once()
    command = mock_handle.await_args.kwargs["command"]
    assert command.type.value == "schedule"


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages(tmp_path) -> None:  # noqa: ANN001
    """Router should still ignore its own regular text messages."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)

    room = _make_room("@mindroom_router:example.com", "@bob:example.com")
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = "@mindroom_router:example.com"
    event.body = "Regular message from router"
    event.event_id = "test_event"
    event.server_timestamp = 1234567890
    event.source = {"content": {"body": "Regular message from router"}}

    with (
        patch("mindroom.turn_controller.TurnController._execute_command", new_callable=AsyncMock) as mock_handle,
        patch("mindroom.turn_controller.interactive.handle_text_response", new_callable=AsyncMock, return_value=None),
        patch("mindroom.turn_controller.is_dm_room", return_value=False),
    ):
        bot.client = MagicMock()
        await bot._on_message(room, event)

    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_router_processes_own_sidecar_commands_using_original_sender(tmp_path) -> None:  # noqa: ANN001
    """Self-sent sidecar previews should still use ORIGINAL_SENDER for dispatch prechecks."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": AgentConfig(display_name="Home", rooms=["!test:example.com"])},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "!schedule tomorrow at 9am @mindroom_home:localhost turn off the lights",
                    "m.mentions": {"user_ids": ["@mindroom_home:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    bot._send_response = AsyncMock(return_value="$reply")
    install_send_response_mock(bot, bot._send_response)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(False, None, []),
    )

    room = _make_room("@mindroom_router:example.com", "@mindroom_home:localhost", "@alice:example.com")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-relay",
            "sender": "@mindroom_router:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!schedule tomorrow [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-relay",
                ORIGINAL_SENDER_KEY: "@alice:example.com",
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch(
            "mindroom.commands.handler.schedule_task",
            new_callable=AsyncMock,
            return_value=("task123", "scheduled"),
        ) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)

    mock_interactive.assert_awaited_once()
    assert mock_schedule.await_args.kwargs["scheduled_by"] == "@alice:example.com"


@pytest.mark.asyncio
async def test_router_parses_sidecar_schedule_command_from_canonical_body(tmp_path) -> None:  # noqa: ANN001
    """Router should schedule from the hydrated sidecar body and mentions."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": AgentConfig(display_name="Home", rooms=["!test:example.com"])},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "!schedule tomorrow at 9am @mindroom_home:localhost turn off the lights",
                    "m.mentions": {"user_ids": ["@mindroom_home:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    bot._send_response = AsyncMock(return_value="$reply")
    install_send_response_mock(bot, bot._send_response)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(False, None, []),
    )

    room = _make_room("@mindroom_router:example.com", "@mindroom_home:localhost", "@alice:example.com")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-schedule",
            "sender": "@alice:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!schedule tomorrow [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-schedule",
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch(
            "mindroom.commands.handler.schedule_task",
            new_callable=AsyncMock,
            return_value=("task123", "scheduled"),
        ) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)

    mock_interactive.assert_awaited_once()
    assert (
        mock_schedule.await_args.kwargs["full_text"] == "tomorrow at 9am @mindroom_home:localhost turn off the lights"
    )
    mentioned_agents = mock_schedule.await_args.kwargs["mentioned_agents"]
    assert [agent.full_id for agent in mentioned_agents] == ["@mindroom_home:localhost"]


@pytest.mark.asyncio
async def test_router_parses_sidecar_skill_command_mentions_from_canonical_body(tmp_path) -> None:  # noqa: ANN001
    """Router should target the agent mentioned only in hydrated sidecar command content."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={
                    "home": AgentConfig(display_name="Home", rooms=["!test:example.com"], skills=["demo"]),
                    "research": AgentConfig(display_name="Research", rooms=["!test:example.com"], skills=["demo"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "!skill demo summarize the release notes",
                    "m.mentions": {"user_ids": ["@mindroom_home:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    bot._send_skill_command_response = AsyncMock(return_value="$skill-reply")
    bot._send_response = AsyncMock(return_value="$fallback")
    install_send_response_mock(bot, bot._send_response)
    install_send_skill_command_response_mock(bot, bot._send_skill_command_response)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(False, None, []),
    )

    room = _make_room(
        "@mindroom_router:example.com",
        "@mindroom_home:localhost",
        "@mindroom_research:localhost",
        "@alice:example.com",
    )
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-skill",
            "sender": "@alice:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!skill demo [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-skill",
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch(
            "mindroom.commands.handler.resolve_skill_command_spec",
            return_value=SimpleNamespace(
                name="demo",
                user_invocable=True,
                dispatch=None,
                disable_model_invocation=False,
            ),
        ),
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)

    mock_interactive.assert_awaited_once()
    assert bot._send_response.await_count == 0
    assert bot._send_skill_command_response.await_args.kwargs["agent_name"] == "home"
    assert "summarize the release notes" in bot._send_skill_command_response.await_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_router_skips_unauthorized_sidecar_commands_before_hydration(tmp_path) -> None:  # noqa: ANN001
    """Unauthorized sidecar previews should be rejected before download or dispatch."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.client.user_id = bot.matrix_id.full_id
    bot.client.download = AsyncMock()

    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-unauthorized",
            "sender": "@alice:example.com",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!schedule tomorrow [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-unauthorized",
            },
        },
    )

    with (
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_interactive,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=False),
        patch("mindroom.commands.handler.schedule_task", new_callable=AsyncMock) as mock_schedule,
    ):
        assert isinstance(event, nio.RoomMessageFile)
        await bot._on_media_message(room, event)

    bot.client.download.assert_not_awaited()
    mock_interactive.assert_not_awaited()
    mock_schedule.assert_not_awaited()
    turn_store.record_turn.assert_called_once_with(
        HandledTurnState.from_source_event_id(event.event_id),
    )


@pytest.mark.asyncio
async def test_prepare_voice_message_includes_original_sender_and_attachment_metadata(tmp_path) -> None:  # noqa: ANN001
    """Audio normalization should preserve sender identity and attachment IDs."""
    config = _attach_runtime_paths(
        Config(
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )
    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")
    client = MagicMock()

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = "🎤 turn on the lights"
        prepared = await _prepare_voice_message_with_runtime(
            client,
            tmp_path,
            room,
            event,
            config,
            sender_domain="example.com",
            thread_id=None,
        )

    assert prepared is not None
    assert prepared.text == "🎤 turn on the lights"
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert prepared.source["content"][ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert prepared.source["content"][ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert VOICE_RAW_AUDIO_FALLBACK_KEY not in prepared.source["content"]
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.local_path.exists()


@pytest.mark.asyncio
async def test_prepare_voice_message_marks_raw_audio_fallback_and_thread(tmp_path) -> None:  # noqa: ANN001
    """Fallback normalization should keep thread metadata and the raw-audio flag."""
    config = _attach_runtime_paths(Config(authorization={"default_room_access": True}), tmp_path)
    room = _make_room("@mindroom_home:example.com", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        source={
            "content": {
                "body": "voice.ogg",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    client = MagicMock()

    with patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio:
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        prepared = await _prepare_voice_message_with_runtime(
            client,
            tmp_path,
            room,
            event,
            config,
            sender_domain="example.com",
            thread_id="$thread_root",
        )

    assert prepared is not None
    assert prepared.text == f"{VOICE_PREFIX}[Attached voice message]"
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert prepared.source["content"][ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert prepared.source["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] is True
    assert prepared.source["content"][ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert prepared.source["content"]["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$thread_root"}
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_router_ignores_audio_events_from_internal_agents(tmp_path) -> None:  # noqa: ANN001
    """Audio from another agent should be ignored immediately."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    config = _attach_runtime_paths(
        Config(
            agents={"assistant": {"display_name": "Assistant"}},
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = MagicMock()
    bot._send_response = AsyncMock()
    install_send_response_mock(bot, bot._send_response)

    room = _make_room(
        "@mindroom_router:example.com",
        f"@mindroom_assistant:{config.get_domain(runtime_paths_for(config))}",
        "@alice:example.com",
    )
    event = _make_voice_event(
        sender=f"@mindroom_assistant:{config.get_domain(runtime_paths_for(config))}",
        event_id="$agent_audio_event",
        body="generated_audio.ogg",
        source={"content": {"body": "generated_audio.ogg", "msgtype": "m.audio"}},
    )

    with (
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, event)

    mock_voice.assert_not_called()
    mock_download_audio.assert_not_called()
    bot._send_response.assert_not_called()
    turn_store.record_turn.assert_called_once_with(
        HandledTurnState.from_source_event_id("$agent_audio_event"),
    )


@pytest.mark.asyncio
async def test_agent_handles_audio_without_router_when_voice_disabled(tmp_path) -> None:  # noqa: ANN001
    """A single agent should answer audio directly when no router is present."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_home:localhost"
    agent_user.agent_name = "home"
    agent_user.matrix_id = MatrixID.parse("@mindroom_home:localhost")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    bot._generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, bot._generate_response)
    _install_voice_thread_dispatch_mocks(bot)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(True, "$voice_event", []),
    )

    room = _make_room("@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = None
        await bot._on_media_message(room, event)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}[Attached voice message]")
    assert call_kwargs["attachment_ids"] == [expected_attachment_id]
    assert list(call_kwargs["media"].audio)
    turn_store.record_turn.assert_called_once_with(
        HandledTurnState.from_source_event_id(
            "$voice_event",
            response_event_id="$response",
            source_event_prompts={"$voice_event": f"{VOICE_PREFIX}[Attached voice message]"},
        ).with_response_context(
            response_owner="home",
            history_scope=HistoryScope(kind="agent", scope_id="home"),
            conversation_target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$voice_event",
                reply_to_event_id="$voice_event",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_agent_handles_audio_with_router_present_in_single_agent_room(tmp_path) -> None:  # noqa: ANN001
    """Router presence should not block the only visible agent from answering audio."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_home:localhost"
    agent_user.agent_name = "home"
    agent_user.matrix_id = MatrixID.parse("@mindroom_home:localhost")

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=_attach_runtime_paths(
            Config(
                agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        ),
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    bot._generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, bot._generate_response)
    _install_voice_thread_dispatch_mocks(bot)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(True, "$voice_event", []),
    )

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = None
        await bot._on_media_message(room, event)

    mock_download_audio.assert_called_once()
    bot._generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_router_and_agent_share_audio_normalization_when_router_is_present(tmp_path) -> None:  # noqa: ANN001
    """Router-present rooms should still normalize one audio event only once."""
    config = _attach_runtime_paths(
        Config(
            agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    bots: list[AgentBot] = []
    for agent_name in (ROUTER_AGENT_NAME, "home"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = _agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = MagicMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_controller_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = agent_user.user_id
        bot._send_response = AsyncMock(return_value="$router_response")
        bot._generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_send_response_mock(bot, bot._send_response)
        install_generate_response_mock(bot, bot._generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
            return_value=(True, "$voice_event", []),
        )
        bots.append(bot)

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}turn on the lights"
        for bot in bots:
            await bot._on_media_message(room, event)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    bots[0]._send_response.assert_not_called()
    assert bots[1]._generate_response.await_count == 1


@pytest.mark.asyncio
async def test_router_posts_visible_voice_echo_when_enabled(tmp_path) -> None:  # noqa: ANN001
    """Router can optionally post the normalized voice text for user visibility."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._on_media_message(room, event)

    bot._delivery_gateway.send_text.assert_called_once()
    request = bot._delivery_gateway.send_text.call_args.args[0]
    assert request.target.reply_to_event_id == "$voice_event"
    assert request.response_text == f"{VOICE_PREFIX}@home turn on the lights"
    assert request.target.resolved_thread_id == "$voice_event"
    assert request.skip_mentions is True


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_deduplicated_on_redelivery(tmp_path) -> None:  # noqa: ANN001
    """Visible router echoes should be sent once even if the same audio event is redelivered."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._on_media_message(room, event)
        await bot._on_media_message(room, event)

    bot._delivery_gateway.send_text.assert_called_once()
    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bot._turn_store.is_handled(event.event_id)
    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id == "$voice_echo"


@pytest.mark.asyncio
async def test_router_visible_voice_echo_respects_reply_permissions(tmp_path) -> None:  # noqa: ANN001
    """Router should not post visible echoes when it cannot reply to the sender."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        authorization={
            "default_room_access": True,
            "agent_reply_permissions": {ROUTER_AGENT_NAME: ["@bob:example.com"]},
        },
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, event)

    bot._delivery_gateway.send_text.assert_not_called()
    mock_download_audio.assert_not_awaited()
    mock_voice.assert_not_awaited()
    assert bot._turn_store.is_handled(event.event_id)
    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id is None


@pytest.mark.asyncio
async def test_router_visible_voice_echo_keeps_multi_agent_handoff(tmp_path) -> None:  # noqa: ANN001
    """Visible router echoes should not replace the normal multi-agent handoff."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        send_response_side_effect=["$voice_echo", "$route"],
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

    assert bot._delivery_gateway.send_text.await_count == 2
    echo_request = bot._delivery_gateway.send_text.call_args_list[0].args[0]
    handoff_request = bot._delivery_gateway.send_text.call_args_list[1].args[0]
    assert echo_request.target.reply_to_event_id == "$voice_event"
    assert echo_request.response_text == f"{VOICE_PREFIX}summarize this audio"
    assert echo_request.skip_mentions is True
    assert echo_request.extra_content is None
    assert handoff_request.target.reply_to_event_id == "$voice_event"
    assert handoff_request.response_text == "@home could you help with this?"
    assert handoff_request.extra_content == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
    }


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_not_duplicated_when_handoff_retries(tmp_path) -> None:  # noqa: ANN001
    """A failed handoff retry should reuse the prior visible echo instead of reposting it."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        send_response_side_effect=["$voice_echo", None, "$route"],
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

        assert not bot._turn_store.is_handled(event.event_id)
        assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"

        await bot._on_media_message(room, event)

    response_texts = [call.args[0].response_text for call in bot._delivery_gateway.send_text.call_args_list]
    assert response_texts == [
        f"{VOICE_PREFIX}summarize this audio",
        "@home could you help with this?",
        "@home could you help with this?",
    ]
    assert bot._turn_store.is_handled(event.event_id)
    assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_not_duplicated_when_handoff_retries_after_restart(
    tmp_path: Path,
) -> None:
    """A fresh bot should reuse the persisted visible echo after a failed handoff retry."""
    agents = {
        "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
        "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
    }
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents=agents,
        send_response_side_effect=["$voice_echo", None],
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

    assert not bot._turn_store.is_handled(event.event_id)
    assert bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"

    restarted_bot, restarted_room, restarted_event = _make_visible_router_echo_scenario(
        tmp_path,
        agents=agents,
        send_response_return="$route",
    )

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await restarted_bot._on_media_message(restarted_room, restarted_event)

    response_texts = [call.args[0].response_text for call in restarted_bot._delivery_gateway.send_text.call_args_list]
    assert response_texts == ["@home could you help with this?"]
    assert restarted_bot._turn_store.is_handled(event.event_id)
    assert restarted_bot._turn_store.visible_echo_for_source(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_router_routes_transcribed_audio_when_multiple_agents_are_present(tmp_path) -> None:  # noqa: ANN001
    """Router should route normalized audio like any other synthetic text input."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    config = _attach_runtime_paths(
        Config(
            agents={
                "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
            },
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    bot = _agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    turn_store.is_handled = MagicMock(return_value=False)
    turn_store.record_turn = MagicMock(wraps=turn_store.record_turn)
    bot.logger = MagicMock()
    replace_turn_controller_deps(bot, logger=bot.logger)
    bot.client = AsyncMock()
    bot._send_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    _install_voice_thread_dispatch_mocks(bot)
    unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
        return_value=(False, None, []),
    )

    room = _make_room(
        "@mindroom_router:localhost",
        "@mindroom_home:localhost",
        "@mindroom_research:localhost",
        "@alice:example.com",
    )
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

    bot._delivery_gateway.send_text.assert_called_once()
    request = bot._delivery_gateway.send_text.call_args.args[0]
    assert request.target.reply_to_event_id == "$voice_event"
    assert request.target.resolved_thread_id == "$voice_event"
    assert request.target.resolved_thread_id == "$voice_event"
    assert request.response_text == "@home could you help with this?"
    assert request.extra_content == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
    }
    turn_store.record_turn.assert_called_once_with(
        HandledTurnState.from_source_event_id(
            "$voice_event",
            response_event_id="$response",
        ).with_response_context(
            response_owner=ROUTER_AGENT_NAME,
            history_scope=None,
            conversation_target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id="$voice_event",
                reply_to_event_id="$voice_event",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_transcribed_mentions_target_the_mentioned_agent_when_router_absent(tmp_path) -> None:  # noqa: ANN001
    """A transcript mention should make the mentioned agent respond directly."""
    config = _attach_runtime_paths(
        Config(
            agents={
                "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
            },
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    room = _make_room("@mindroom_home:localhost", "@mindroom_research:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    bots: list[AgentBot] = []
    for agent_name in ("home", "research"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = _agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = MagicMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_controller_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        bot._generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_generate_response_mock(bot, bot._generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
            return_value=(True, "$voice_event", []),
        )
        bots.append(bot)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@research summarize this audio"
        for bot in bots:
            await bot._on_media_message(room, event)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bots[0]._generate_response.await_count == 0
    assert bots[1]._generate_response.await_count == 1
    call_kwargs = bots[1]._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}@research summarize this audio")
    assert call_kwargs["attachment_ids"] == [_attachment_id_for_event("$voice_event")]


@pytest.mark.asyncio
async def test_caption_mentions_still_target_agent_when_stt_drops_the_mention(tmp_path) -> None:  # noqa: ANN001
    """Inherited audio-caption mentions should still target the agent when STT omits them."""
    config = _attach_runtime_paths(
        Config(
            agents={
                "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
                "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
            },
            authorization={"default_room_access": True},
            voice={"enabled": True},
        ),
        tmp_path,
    )

    room = _make_room("@mindroom_home:localhost", "@mindroom_research:localhost", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        body="For @research voice note",
        source={
            "content": {
                "body": "For @research voice note",
                "filename": "voice.ogg",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        },
    )

    bots: list[AgentBot] = []
    for agent_name in ("home", "research"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = _agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        turn_store.is_handled = MagicMock(return_value=False)
        bot.logger = MagicMock()
        replace_turn_controller_deps(bot, logger=bot.logger)
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        bot._generate_response = AsyncMock(return_value=f"${agent_name}_response")
        install_generate_response_mock(bot, bot._generate_response)
        _install_voice_thread_dispatch_mocks(bot)
        unwrap_extracted_collaborator(bot._conversation_resolver).derive_conversation_context = AsyncMock(
            return_value=(True, "$voice_event", []),
        )
        bots.append(bot)

    with (
        patch("mindroom.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler._handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch("mindroom.turn_controller.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        for bot in bots:
            await bot._on_media_message(room, event)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bots[0]._generate_response.await_count == 0
    assert bots[1]._generate_response.await_count == 1
    call_kwargs = bots[1]._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}summarize this audio")
    assert call_kwargs["attachment_ids"] == [_attachment_id_for_event("$voice_event")]
